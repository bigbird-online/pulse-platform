"""Bounded compressed frames, not a size ceiling on an ever-growing receipt history.

One proven prefix plus independently bounded gzip frames reconstructs the original
canonical bytes in a private staging file. Only a complete hash-verified result
replaces the live input. Original ledger records are never edited or pruned.
"""
from __future__ import annotations
import gzip
from hashlib import sha256
import io
import json
import os
from pathlib import Path
import shutil
import struct
import tempfile
import zlib
from typing import Sequence
from pulse_compute_cost.receipts import Receipt, canonical

MAGIC = b"PULSE-RECEIPTS/2\n"
SCHEMA = "pulse_stats_receipt_frames/v2"
FRAME_BYTES = 4 * 1024 * 1024
# A full replay after a lost or changed prefix must fit; TCloud's is ~150 MiB.
WIRE_BYTES = 256 * 1024 * 1024
DISK_RESERVE = 512 * 1024 * 1024


def _line(receipt: Receipt) -> bytes:
    raw = canonical(receipt.to_dict()) + b"\n"
    if len(raw) > FRAME_BYTES:
        raise ValueError("individual receipt exceeds frame limit")
    return raw


def _private_file(path: Path):
    st = path.lstat()
    if path.is_symlink() or not path.is_file() or st.st_uid != os.getuid() or st.st_mode & 0o077:
        raise ValueError("unsafe receipt input")
    return st


def cursor(target: Path) -> tuple[Path | None, int, str]:
    path = target if target.exists() or target.is_symlink() else target.with_suffix(".last-good")
    if not path.exists() and not path.is_symlink():
        return None, 0, sha256(b"").hexdigest()
    before = _private_file(path); digest = sha256(); size = 0
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(FRAME_BYTES), b""):
            digest.update(chunk); size += len(chunk)
    after = _private_file(path)
    if (before.st_ino, before.st_size, before.st_mtime_ns) != (after.st_ino, size, after.st_mtime_ns):
        raise ValueError("receipt prefix changed during read")
    return path, size, digest.hexdigest()


def encode(rows: Sequence[Receipt], prefix_bytes: int, prefix_sha256: str) -> bytes:
    """Serialize a verified append once; replay rows only when the prefix differs."""
    import re
    if type(prefix_bytes) is not int or not 0 <= prefix_bytes <= 2**63-1 or not re.fullmatch(r"[a-f0-9]{64}", prefix_sha256):
        raise ValueError("invalid receipt cursor")
    empty = sha256(b"").hexdigest()
    total = 0; whole = sha256(); prefix = sha256()
    matched = prefix_bytes == 0 and prefix_sha256 == empty
    boundary = prefix_bytes == 0
    frames = io.BytesIO(); block = bytearray()

    def flush() -> None:
        if not block:
            return
        wire = gzip.compress(bytes(block), mtime=0)
        frames.write(struct.pack(">I", len(wire))); frames.write(wire); block.clear()
        if frames.tell()+4 > WIRE_BYTES:
            raise ValueError("receipt wire exceeds transfer limit")

    for row in rows:
        raw = _line(row); total += len(raw); whole.update(raw)
        if total <= prefix_bytes:
            prefix.update(raw)
        if total == prefix_bytes:
            boundary = True; matched = prefix.hexdigest() == prefix_sha256
            continue
        if matched and total > prefix_bytes:
            if len(block)+len(raw) > FRAME_BYTES: flush()
            block.extend(raw)
    if not boundary or total < prefix_bytes:
        matched = False
    if matched:
        flush(); base = prefix_bytes
    else:
        frames = io.BytesIO(); block = bytearray()
        for row in rows:
            raw = _line(row)
            if len(block)+len(raw) > FRAME_BYTES: flush()
            block.extend(raw)
        flush(); base = 0
    header = dict(schema=SCHEMA, mode="append" if matched else "replace", base_bytes=base,
        base_sha256=prefix_sha256 if matched else empty, full_bytes=total, full_sha256=whole.hexdigest())
    output = io.BytesIO(); output.write(MAGIC + canonical(header) + b"\n")
    output.write(frames.getvalue()); output.write(struct.pack(">I",0))
    if output.tell() > WIRE_BYTES: raise ValueError("receipt wire exceeds transfer limit")
    return output.getvalue()

def _inflate(data: bytes) -> bytes:
    decoder = zlib.decompressobj(16+zlib.MAX_WBITS)
    try: raw = decoder.decompress(data, FRAME_BYTES+1)
    except zlib.error as error: raise ValueError("invalid receipt frame") from error
    if len(raw)>FRAME_BYTES or not decoder.eof or decoder.unused_data or decoder.unconsumed_tail:
        raise ValueError("invalid or oversized receipt frame")
    return raw


def _validate_lines(raw: bytes) -> None:
    if not raw.endswith(b"\n"): raise ValueError("incomplete receipt frame")
    for line in raw.splitlines(keepends=True):
        if not line.strip() or _line(Receipt.from_dict(json.loads(line))) != line:
            raise ValueError("noncanonical receipt frame")


def install(target: Path, wire: bytes, base_path: Path | None) -> dict:
    if len(wire)>WIRE_BYTES or not wire.startswith(MAGIC):
        raise ValueError("invalid receipt frame stream")
    source=io.BytesIO(wire[len(MAGIC):]); header_line=source.readline(4097)
    if len(header_line)>4096 or not header_line.endswith(b"\n"):
        raise ValueError("invalid receipt header")
    h=json.loads(header_line)
    fields={"schema","mode","base_bytes","base_sha256","full_bytes","full_sha256"}
    if not isinstance(h,dict) or set(h)!=fields or h["schema"]!=SCHEMA or h["mode"] not in {"append","replace"}:
        raise ValueError("unsupported receipt stream")
    import re
    if any(type(h[k]) is not int or not 0<=h[k]<=2**63-1 for k in ("base_bytes","full_bytes")) or h["base_bytes"]>h["full_bytes"]:
        raise ValueError("invalid receipt size")
    if any(not isinstance(h[k],str) or not re.fullmatch(r"[a-f0-9]{64}",h[k]) for k in ("base_sha256","full_sha256")):
        raise ValueError("invalid receipt identity")
    if h["mode"]=="replace" and (h["base_bytes"] or h["base_sha256"]!=sha256(b"").hexdigest()):
        raise ValueError("replace cannot retain a prefix")
    target.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
    if target.is_symlink() or target.parent.is_symlink() or target.parent.stat().st_mode&0o077:
        raise ValueError("unsafe receipt destination")
    if h["full_bytes"] > max(0,shutil.disk_usage(target.parent).free-DISK_RESERVE):
        raise ValueError("insufficient receipt staging capacity")
    fd,name=tempfile.mkstemp(prefix=".receipt-stage-",dir=target.parent)
    size=0;whole=sha256();frames=0
    try:
        with os.fdopen(fd,"wb") as output:
            if h["base_bytes"]:
                if base_path is None: raise ValueError("missing receipt prefix")
                before=_private_file(base_path)
                # The cursor proves an immutable PREFIX, not that no later
                # append occurred. A natural publisher may append after a
                # read-only UAT captured its cursor. Reuse exactly the proved
                # first N bytes and ignore later local suffix bytes; the remote
                # final digest below still governs the replacement.
                if before.st_size < h["base_bytes"]: raise ValueError("changed receipt prefix")
                last=b""
                with base_path.open("rb") as base:
                    remaining=h["base_bytes"]
                    while remaining:
                        raw=base.read(min(FRAME_BYTES,remaining))
                        if not raw: raise ValueError("truncated receipt prefix")
                        last=raw[-1:];output.write(raw);whole.update(raw);size+=len(raw);remaining-=len(raw)
                after=_private_file(base_path)
                if before.st_ino != after.st_ino or after.st_size < h["base_bytes"]:
                    raise ValueError("receipt prefix changed during copy")
                if size!=h["base_bytes"] or whole.hexdigest()!=h["base_sha256"] or last!=b"\n":
                    raise ValueError("receipt prefix identity mismatch")
            while True:
                length=source.read(4)
                if len(length)!=4: raise ValueError("truncated receipt stream")
                count=struct.unpack(">I",length)[0]
                if count==0:break
                if count>WIRE_BYTES:raise ValueError("oversized receipt frame")
                compressed=source.read(count)
                if len(compressed)!=count:raise ValueError("truncated receipt frame")
                raw=_inflate(compressed);_validate_lines(raw);size+=len(raw)
                if size>h["full_bytes"]:raise ValueError("receipt size exceeds declared archive")
                whole.update(raw);output.write(raw);frames+=1
            if source.read(1) or size!=h["full_bytes"] or whole.hexdigest()!=h["full_sha256"]:
                raise ValueError("receipt archive identity mismatch")
            output.flush();os.fsync(output.fileno())
        os.replace(name,target)
    finally:
        Path(name).unlink(missing_ok=True)
    return {"frames":frames,"retained_bytes":h["base_bytes"],"received_facts_bytes":size}
