"""Private verified-prefix checkpoint of the production observation stream.

A disposable acceleration of `UsageLedger` reads, never a source of facts.
It records how far a complete read verified the Git stream (sequence, last
digest, the Git object identity of batches 1..N with the chunks they named)
plus the derived receipt rows, each fact as its exact canonical bytes.

A later process trusts it only when all of these hold:

* manifest schema, logic version, ledger binding and self digest are equal;
* the current Git tree yields the same object identity for 1..N (checked by
  the core before it skips those batches) and the core chains batch N+1, or
  the head, to the recorded digest;
* every segment file has its recorded size and SHA-256, every batch chains to
  its predecessor, and SHA-256 over its own fact bytes equals its recorded
  digest. Facts therefore are the Git batches' bytes, not a parallel copy.

Anything else is a miss: the caller reads and validates the whole stream and
rebuilds the checkpoint. A failed write only costs the next read its speed.
"""
from __future__ import annotations

from contextlib import contextmanager
import fcntl
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import stat
import tempfile
import time
from typing import Any, Callable, Iterator
import zlib

from .receipts import Receipt, _read_receipt, canonical, digest

SCHEMA = "computecost_observation_checkpoint/v1"
DIRECTORY = "observation-checkpoint"
MANIFEST = "manifest.json"
SEGMENT = re.compile(r"seg-([0-9a-f]{64})\.z")
MAX_SEGMENTS = 24
READ_SIZE = 256 * 1024
OUTPUT_SIZE = 256 * 1024
FACT_KEYS = frozenset({"schema", "kind", "entity_ref", "fact_ref", "occurred_at", "observed_at",
                       "producer", "evidence", "supersedes", "payload"})
_SOURCES = ("ledger.py", "receipts.py", "observation_checkpoint.py", "observation_authority.py")


class CheckpointMiss(ValueError):
    """The checkpoint cannot be trusted; read the complete stream instead."""


def logic_version(core_source: bytes) -> str:
    """Derivation identity: this reader's code plus the core's validation code.

    Any change to how batches are validated or rows derived voids old
    checkpoints, so a release re-verifies the whole stream once.
    """
    root = Path(__file__).parent
    return digest([SCHEMA, sha256(core_source).hexdigest(),
                   [(name, sha256((root / name).read_bytes()).hexdigest()) for name in _SOURCES]])


def _private_directory(path: Path, *, create: bool) -> bool:
    try:
        if create:
            try:
                path.mkdir(mode=0o700)
            except FileExistsError:
                pass
        info = path.lstat()
    except OSError:
        return False
    return (stat.S_ISDIR(info.st_mode) and info.st_uid == os.getuid()
            and not info.st_mode & 0o077)


def _open_private(path: Path) -> int:
    """A regular, account-owned 0600 file; never through a symlink."""
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    info = os.fstat(fd)
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        os.close(fd)
        raise CheckpointMiss("checkpoint file is not private")
    return fd


def _open_and_close(path: Path) -> None:
    os.close(_open_private(path))


def _fsync_directory(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def batch_digest(meta: dict[str, Any], facts: list[bytes]) -> str:
    """digest(batch without sha256) from the facts' canonical bytes, without re-encoding."""
    result = sha256(b'{"facts":[')
    for i, line in enumerate(facts):
        if i:
            result.update(b",")
        result.update(line)
    result.update(b'],"previous":' + canonical(meta["previous"]) + b',"recorded_at":'
                  + canonical(meta["recorded_at"]) + b',"schema":' + canonical(meta["schema"])
                  + b',"sequence":' + canonical(meta["sequence"]) + b"}")
    return result.hexdigest()


def receipt_from_canonical(line: bytes, pool: Any) -> Receipt:
    """Receipt.from_verified of a fact given as its exact canonical encoding.

    Only for lines whose batch digest the loader verified, so each line is the
    canonical (key-sorted) encoding of a fact the core validated. The keys
    before "payload" hold only refs, a kind, timestamps or null and the keys
    after it only strings or null; an unescaped `,"` cannot occur inside a JSON
    string. So the first `,"payload":` and the last `,"producer":` delimit the
    payload's own canonical bytes, and nothing is re-encoded.
    """
    raw = json.loads(line)
    if not isinstance(raw, dict) or set(raw) != FACT_KEYS:
        raise CheckpointMiss("checkpoint fact differs")
    start, end = line.find(b',"payload":'), line.rfind(b',"producer":')
    if start < 0 or end <= start + 11:
        raise CheckpointMiss("checkpoint fact is not canonical")
    return _read_receipt(Receipt, raw, line[start + 11:end], pool)


class SegmentWriter:
    """Stream (batch metadata, canonical facts) into one compressed private temp file."""

    def __init__(self, directory: Path):
        self.directory = directory
        fd, name = tempfile.mkstemp(prefix=".tmp-", suffix=".z", dir=directory)
        self.path = Path(name)
        self.stream = os.fdopen(fd, "wb")
        self.compressor = zlib.compressobj(1)
        self.hash = sha256()
        self.size = 0
        self.first: int | None = None
        self.last: int | None = None

    def _write(self, data: bytes) -> None:
        if data:
            self.stream.write(data)
            self.hash.update(data)
            self.size += len(data)

    def add(self, meta: dict[str, Any], facts: list[bytes]) -> None:
        record = {k: meta[k] for k in ("schema", "sequence", "recorded_at", "previous", "sha256")}
        self._write(self.compressor.compress(canonical({**record, "count": len(facts)}) + b"\n"))
        for line in facts:
            self._write(self.compressor.compress(line + b"\n"))
        if self.first is None:
            self.first = meta["sequence"]
        self.last = meta["sequence"]

    def finish(self) -> dict[str, Any]:
        self._write(self.compressor.flush())
        self.stream.flush()
        os.fsync(self.stream.fileno())
        self.stream.close()
        return {"sha256": self.hash.hexdigest(), "size": self.size, "first": self.first, "last": self.last}

    def abort(self) -> None:
        try:
            self.stream.close()
        finally:
            self.path.unlink(missing_ok=True)


class Checkpoint:
    def __init__(self, root: Path, binding: dict[str, str], logic: str):
        self.root = Path(root)
        self.directory = self.root / DIRECTORY
        self.binding, self.logic = binding, logic

    # -- reading -----------------------------------------------------------
    def available(self, *, create: bool = False) -> bool:
        try:
            info = self.root.lstat()
        except OSError:
            return False
        if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o022):
            return False
        return _private_directory(self.directory, create=create)

    def _read_manifest(self) -> dict[str, Any]:
        fd = _open_private(self.directory / MANIFEST)
        with os.fdopen(fd, "rb") as stream:
            raw = stream.read(64 * 1024 * 1024)
        manifest = json.loads(raw)
        if (not isinstance(manifest, dict) or set(manifest) != {"schema", "logic", "binding", "prefix",
                "segments", "digest"} or manifest["digest"] != digest({k: v for k, v in manifest.items()
                if k != "digest"})):
            raise CheckpointMiss("checkpoint manifest differs")
        return manifest

    def manifest(self) -> dict[str, Any] | None:
        """The current valid manifest for this binding and logic, or None."""
        if not self.available():
            return None
        try:
            manifest = self._read_manifest()
            prefix, segments = manifest["prefix"], manifest["segments"]
            if (manifest["schema"] != SCHEMA or manifest["logic"] != self.logic
                    or manifest["binding"] != self.binding or not isinstance(prefix, dict)
                    or set(prefix) != {"sequence", "watermark", "objects", "chunks"}
                    or type(prefix["sequence"]) is not int or prefix["sequence"] < 1
                    or not isinstance(prefix["chunks"], list) or not isinstance(segments, list)
                    or not segments):
                return None
            expected = 1
            for segment in segments:
                if (not isinstance(segment, dict) or set(segment) != {"sha256", "size", "first", "last"}
                        or segment["first"] != expected or type(segment["last"]) is not int
                        or segment["last"] < segment["first"] or not isinstance(segment["sha256"], str)
                        or not re.fullmatch(r"[0-9a-f]{64}", segment["sha256"])
                        or type(segment["size"]) is not int or segment["size"] < 1):
                    return None
                expected = segment["last"] + 1
            if expected != prefix["sequence"] + 1:
                return None
            return manifest
        except (OSError, ValueError, TypeError, KeyError):
            return None

    @contextmanager
    def _segments(self, manifest: dict[str, Any]) -> Iterator[list[tuple[dict[str, Any], Any]]]:
        # Open every file first: a concurrent writer's cleanup can then no
        # longer remove what this read already selected.
        opened: list[tuple[dict[str, Any], Any]] = []
        try:
            for segment in manifest["segments"]:
                fd = _open_private(self.directory / f"seg-{segment['sha256']}.z")
                opened.append((segment, os.fdopen(fd, "rb")))
            yield opened
        finally:
            for _, stream in opened:
                stream.close()

    def load(self, manifest: dict[str, Any],
             accept: Callable[[dict[str, Any], list[bytes]], None]) -> None:
        """Feed each verified batch (metadata, canonical facts) in order; raise CheckpointMiss.

        `accept` must keep nothing it cannot discard: a later failure means the
        caller restarts from a complete read.
        """
        previous: str | None = None
        expected = 1
        try:
            with self._segments(manifest) as opened:
                for segment, stream in opened:
                    hasher, size = sha256(), 0
                    decompressor = zlib.decompressobj()
                    pending = b""
                    meta: dict[str, Any] | None = None
                    facts: list[bytes] = []

                    def lines(data: bytes) -> Iterator[bytes]:
                        nonlocal pending
                        pending += data
                        *complete, pending = pending.split(b"\n")
                        yield from complete

                    def consume(line: bytes) -> None:
                        nonlocal meta, facts, previous, expected
                        if meta is None:
                            meta = json.loads(line)
                            if (not isinstance(meta, dict) or set(meta) != {"schema", "sequence", "recorded_at",
                                    "previous", "sha256", "count"} or meta["sequence"] != expected
                                    or meta["previous"] != previous or type(meta["count"]) is not int
                                    or meta["count"] < 1
                                    or meta["schema"] != "computecost_ledger_transaction/v1"):
                                raise CheckpointMiss("checkpoint batch order differs")
                            facts = []
                            return
                        facts.append(line)
                        if len(facts) == meta["count"]:
                            if batch_digest(meta, facts) != meta["sha256"]:
                                raise CheckpointMiss("checkpoint batch digest differs")
                            accept({k: v for k, v in meta.items() if k != "count"}, facts)
                            previous, expected = meta["sha256"], expected + 1
                            meta, facts = None, []

                    while True:
                        chunk = stream.read(READ_SIZE)
                        if not chunk:
                            break
                        hasher.update(chunk)
                        size += len(chunk)
                        data = chunk
                        while data:
                            # Bounded output keeps transient buffers small and reusable.
                            for line in lines(decompressor.decompress(data, OUTPUT_SIZE)):
                                consume(line)
                            if decompressor.eof:
                                # Compacted segments are concatenated streams.
                                data = decompressor.unused_data
                                decompressor = zlib.decompressobj()
                            else:
                                data = decompressor.unconsumed_tail
                    if (size != segment["size"] or hasher.hexdigest() != segment["sha256"]
                            or pending or meta is not None or expected != segment["last"] + 1):
                        raise CheckpointMiss("checkpoint segment differs")
        except CheckpointMiss:
            raise
        except (OSError, ValueError, TypeError, KeyError, zlib.error) as exc:
            raise CheckpointMiss("checkpoint unreadable") from exc
        if previous != manifest["prefix"]["watermark"] or expected != manifest["prefix"]["sequence"] + 1:
            raise CheckpointMiss("checkpoint watermark differs")

    # -- writing -----------------------------------------------------------
    def writer(self) -> SegmentWriter | None:
        if not self.available(create=True):
            return None
        try:
            return SegmentWriter(self.directory)
        except OSError:
            return None

    def commit(self, writer: SegmentWriter | None, prefix: dict[str, Any],
               base: dict[str, Any] | None, *, rejected: str | None = None) -> dict[str, Any] | None:
        """Publish base segments + this writer's batches as the verified `prefix`.

        Returns the written manifest, or None (nothing changes on failure).
        `rejected` is the digest of a manifest the caller found untrustworthy;
        any other newer manifest is never replaced by an older prefix.
        """
        segments = list(base["segments"]) if base is not None else []
        try:
            added = writer.finish() if writer is not None and writer.first is not None else None
            if writer is not None and added is None:
                writer.abort()
            if added is not None:
                segments.append(added)
            if not segments or segments[-1]["last"] != prefix["sequence"]:
                raise CheckpointMiss("checkpoint does not cover the verified prefix")
            with self._lock():
                current = self.manifest()
                if (current is not None and current["digest"] != rejected
                        and current["prefix"]["sequence"] > prefix["sequence"]):
                    if writer is not None:
                        writer.abort()
                    return None
                for segment in segments[:-1] if added is not None else segments:
                    # A base older than the last two generations may be cleaned.
                    _open_and_close(self.directory / f"seg-{segment['sha256']}.z")
                if added is not None:
                    os.replace(writer.path, self.directory / f"seg-{added['sha256']}.z")  # type: ignore[union-attr]
                if len(segments) > MAX_SEGMENTS:
                    segments = [self._compact(segments)]
                manifest: dict[str, Any] = {"schema": SCHEMA, "logic": self.logic, "binding": self.binding,
                                            "prefix": prefix, "segments": segments}
                manifest["digest"] = digest(manifest)
                fd, name = tempfile.mkstemp(prefix=".tmp-", dir=self.directory)
                try:
                    with os.fdopen(fd, "wb") as stream:
                        stream.write(canonical(manifest))
                        stream.flush()
                        os.fsync(stream.fileno())
                    os.replace(name, self.directory / MANIFEST)
                finally:
                    if os.path.exists(name):
                        os.unlink(name)
                _fsync_directory(self.directory)
                self._clean({s["sha256"] for s in segments}
                            | ({s["sha256"] for s in current["segments"]} if current else set()))
                return manifest
        except (OSError, ValueError, TypeError, KeyError):
            if writer is not None:
                writer.abort()
            return None

    @contextmanager
    def _lock(self) -> Iterator[None]:
        fd = os.open(self.directory / ".lock", os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            os.close(fd)

    def _compact(self, segments: list[dict[str, Any]]) -> dict[str, Any]:
        """Concatenate segment files (each a sequence of zlib streams) into one."""
        writer = SegmentWriter(self.directory)
        try:
            with self._segments({"segments": segments}) as opened:
                for segment, stream in opened:
                    hasher = sha256()
                    while chunk := stream.read(READ_SIZE):
                        hasher.update(chunk)
                        writer._write(chunk)
                    if hasher.hexdigest() != segment["sha256"]:
                        raise CheckpointMiss("checkpoint segment differs")
            writer.stream.flush()
            os.fsync(writer.stream.fileno())
            writer.stream.close()
        except BaseException:
            writer.abort()
            raise
        merged = {"sha256": writer.hash.hexdigest(), "size": writer.size,
                  "first": segments[0]["first"], "last": segments[-1]["last"]}
        os.replace(writer.path, self.directory / f"seg-{merged['sha256']}.z")
        return merged

    def _clean(self, keep: set[str]) -> None:
        now = time.time()
        for path in self.directory.iterdir():
            try:
                match = SEGMENT.fullmatch(path.name)
                if match and match.group(1) not in keep:
                    path.unlink()
                elif path.name.startswith(".tmp-") and now - path.lstat().st_mtime > 3600:
                    path.unlink()
            except OSError:
                pass
