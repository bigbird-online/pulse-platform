"""Observation bundles: a node's parsed native sources, judged by the stream's collector.

A node that only observes (ownermac under an Oracle publisher) reads its own
native files and sends, per source, the rows its readers produced (before any
version decision), the coverage it measured over all of that source's files,
and its native inputs. The judging collector registers one ``native_push``
source per pushing node; it spools each bundle, then runs every bundled source
through the same per-row version decisions, actor settlement, coverage
versions and attribution stages as a source it read itself. A bundle leaves
the spool only after the collection that read it is ingested, so a failed
collection reads it again; its rows are content-addressed, so a repeat is a
duplicate. Source identity (``reference("source", id)``) is the observing
node's, so coverage entities continue across the switch.
"""
from __future__ import annotations

import gzip
import hashlib
import io
import json
import os
import re
import stat
from pathlib import Path
from typing import Any, Iterable, Mapping

from .events import parse_timestamp
from .receipts import Receipt, ReceiptError, canonical, reference

SCHEMA = "pulse_native_observations/v1"
KIND = "native_push"
MAX_WIRE_BYTES = 64 * 1024 * 1024
MAX_DOCUMENT_BYTES = 512 * 1024 * 1024
SOURCE_KINDS = {"relay_host", "pi", "cc", "codex", "harness_bundle", "harness_root", "harness_core", "receipts"}
# Source configuration the judging collector uses; paths and private inputs stay home.
CONFIG_FIELDS = {"id", "kind", "node", "label", "domain", "work_items", "actor_attribution",
                 "principal_alias_history", "verification_workspace_registry"}
STATUSES = {"complete", "partial", "unknown", "unreachable", "idle"}
_NAME = re.compile(r"^(\d{20})\.json\.gz$")
CONSUMED = ".consumed.json"
CONSUMED_SCHEMA = "pulse_native_observations_consumed/v1"
# Consumed bundle digests kept for exact-duplicate answers; older ones are refused by sequence.
CONSUMED_KEEP = 1024
_PEER = re.compile(r"^[a-z0-9_-]{1,40}$")


def bundle_config(source: Mapping[str, Any]) -> dict[str, Any]:
    """The pushed form of a registered source (writer side)."""
    config = {k: v for k, v in source.items() if k in CONFIG_FIELDS}
    if config.get("verification_workspace_registry") is not None:
        config["verification_workspace_registry"] = True
    return config


def encode(document: Mapping[str, Any]) -> bytes:
    data = gzip.compress(canonical(document), mtime=0)
    if len(data) > MAX_WIRE_BYTES:
        raise ReceiptError("observation bundle exceeds its wire bound")
    return data


def _coverage(value: object) -> dict[str, Any]:
    if (not isinstance(value, Mapping) or set(value) != {"status", "reason", "expected", "observed", "watermark",
            "raw_count", "detail", "diagnostics"} or value["status"] not in STATUSES
            or not isinstance(value["detail"], Mapping) or not isinstance(value["diagnostics"], Mapping)
            or type(value["raw_count"]) is not int or value["raw_count"] < 0
            or any(value[k] is not None and (type(value[k]) is not int or value[k] < 0) for k in ("expected", "observed"))
            or any(value[k] is not None and not isinstance(value[k], str) for k in ("reason", "watermark"))):
        raise ReceiptError("invalid bundled source coverage")
    return dict(value)


def decode(data: bytes, peer: str) -> dict[str, Any]:
    """Validate a bundle completely; never partially accept one."""
    if len(data) > MAX_WIRE_BYTES:
        raise ReceiptError("observation bundle exceeds its wire bound")
    try:
        with gzip.GzipFile(fileobj=io.BytesIO(data)) as stream:
            raw = stream.read(MAX_DOCUMENT_BYTES + 1)
        document = json.loads(raw)
    except (OSError, EOFError, ValueError) as exc:
        raise ReceiptError("observation bundle is not one gzip JSON document") from exc
    if len(raw) > MAX_DOCUMENT_BYTES:
        raise ReceiptError("observation bundle exceeds its document bound")
    if (not isinstance(document, Mapping) or set(document) != {"schema", "peer", "sequence", "created_at", "sources", "inputs"}
            or document["schema"] != SCHEMA or document["peer"] != peer
            or type(document["sequence"]) is not int or document["sequence"] <= 0
            or not isinstance(document["sources"], list) or not isinstance(document["inputs"], list)):
        raise ReceiptError("invalid observation bundle")
    parse_timestamp(document["created_at"], "bundle time")
    sources, seen = [], set()
    for entry in document["sources"]:
        if not isinstance(entry, Mapping) or set(entry) != {"config", "rows", "inventories", "appended", "coverage"}:
            raise ReceiptError("invalid bundled source")
        config = entry["config"]
        if (not isinstance(config, Mapping) or not set(config) <= CONFIG_FIELDS
                or not isinstance(config.get("id"), str) or not config["id"] or config["id"] in seen
                or config.get("kind") not in SOURCE_KINDS):
            raise ReceiptError("invalid bundled source configuration")
        seen.add(config["id"])
        rows = [Receipt.from_dict(r) for r in entry["rows"]]
        inventories = [Receipt.from_dict(r) for r in entry["inventories"]]
        # Rows a full collection appends after its own decisions (CC input-route actors).
        appended = [Receipt.from_dict(r) for r in entry["appended"]]
        if any(r.kind == "native_input" for r in rows + appended):
            raise ReceiptError("native inputs travel beside source rows")
        sources.append({"config": dict(config), "rows": rows, "inventories": inventories, "appended": appended,
                        "coverage": _coverage(entry["coverage"])})
    inputs = [Receipt.from_dict(r) for r in document["inputs"]]
    if any(r.kind != "native_input" for r in inputs):
        raise ReceiptError("bundle inputs must be native inputs")
    return {**document, "sources": sources, "inputs": inputs}


def spool_path(spool: Path, sequence: int) -> Path:
    return spool / f"{sequence:020d}.json.gz"


def _private_spool(spool: Path) -> None:
    info = spool.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise ReceiptError("observation spool must be a private directory")


LOCK = ".spool.lock"


class _SpoolLock:
    """One receiver or consumer at a time per spool: sequence checks and writes are atomic together."""

    def __init__(self, spool: Path):
        self.spool = spool

    def __enter__(self):
        import fcntl
        self.fd = os.open(self.spool / LOCK, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        fcntl.flock(self.fd, fcntl.LOCK_EX)
        return self

    def __exit__(self, *exc):
        os.close(self.fd)  # Closing releases the lock.


def consumed(spool: Path) -> dict[str, Any]:
    """{"last": highest consumed sequence, "digests": {sequence: sha256}} of ingested bundles."""
    path = spool / CONSUMED
    if not path.exists():
        return {"last": 0, "digests": {}}
    value = json.loads(path.read_bytes())
    if (not isinstance(value, Mapping) or value.get("schema") != CONSUMED_SCHEMA or type(value.get("last")) is not int
            or not isinstance(value.get("digests"), Mapping)):
        raise ReceiptError("invalid consumed bundle record")
    return {"last": value["last"], "digests": dict(value["digests"])}


def _write_consumed(spool: Path, record: Mapping[str, Any]) -> None:
    kept = dict(sorted(record["digests"].items(), key=lambda item: int(item[0]))[-CONSUMED_KEEP:])
    temporary = spool / f".consumed-{os.getpid()}"
    with open(temporary, "wb") as handle:
        handle.write(canonical({"schema": CONSUMED_SCHEMA, "last": record["last"], "digests": kept}))
        handle.flush()
        os.fsync(handle.fileno())
    os.chmod(temporary, 0o600)
    os.replace(temporary, spool / CONSUMED)


def store(spool: Path, peer: str, data: bytes) -> int:
    """Receiver side: validate, then write the bundle once under its sequence.

    A bundle already ingested is answered as a duplicate when its bytes are
    the same and refused otherwise; a sequence at or below the last ingested
    one is refused, so a late copy can never replace newer source state.
    """
    document = decode(data, peer)
    spool.mkdir(mode=0o700, parents=True, exist_ok=True)
    _private_spool(spool)
    sequence = document["sequence"]
    digest_hex = hashlib.sha256(data).hexdigest()
    with _SpoolLock(spool):
        record = consumed(spool)
        known = record["digests"].get(str(sequence))
        if known is not None:
            if known != digest_hex:
                raise ReceiptError("a different bundle already used this sequence")
            return sequence
        if sequence <= record["last"]:
            raise ReceiptError("bundle sequence is older than the ingested ones")
        target = spool_path(spool, sequence)
        temporary = spool / f".pending-{os.getpid()}-{sequence}"
        try:
            with open(temporary, "xb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(temporary, 0o600)
            try:
                os.link(temporary, target)  # Never replaces: one bundle per sequence.
            except FileExistsError:
                if target.read_bytes() != data:
                    raise ReceiptError("a different bundle already holds this sequence") from None
        finally:
            if temporary.exists():
                temporary.unlink()
        directory = os.open(spool, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    return document["sequence"]


def expand_pushed(sources: Iterable[Mapping[str, Any]], retirements: Mapping[str, Any]) -> dict[str, Any]:
    """Registered sources with every spooled bundle expanded into its bundled sources.

    Bundles of one peer apply in sequence order: rows concatenate, coverage and
    configuration come from the last bundle naming the source. Consumed
    bundles are tracked apart from unresolved ones: a bundle that does not
    validate, or that sits at or below an already consumed sequence, stays in
    the spool and is reported every collection until someone resolves it; it
    is never read, and later valid bundles are still read.
    """
    expanded, inputs, paths, summaries = [], [], [], []
    for source in sources:
        if not isinstance(source, Mapping) or source.get("kind") != KIND:
            expanded.append(source)
            continue
        if source.get("id") in retirements:
            continue
        peer, allowed = source.get("peer"), source.get("source_ids")
        if (not isinstance(peer, str) or not _PEER.fullmatch(peer) or not isinstance(allowed, list)
                or not isinstance(source.get("path"), str) or not Path(source["path"]).is_absolute()):
            raise ReceiptError("invalid native_push source registration")
        spool, merged, invalid = Path(source["path"]), {}, 0
        names, done = [], {"last": 0, "digests": {}}
        if spool.exists():
            _private_spool(spool)
            with _SpoolLock(spool):
                done = consumed(spool)
                names = sorted(p for p in spool.iterdir() if _NAME.fullmatch(p.name))
        for path in names:
            sequence = int(_NAME.fullmatch(path.name).group(1))
            try:
                data = path.read_bytes()
                if done["digests"].get(str(sequence)) == hashlib.sha256(data).hexdigest():
                    continue  # Consumed already; its removal was interrupted.
                if sequence <= done["last"]:
                    raise ReceiptError("an unconsumed bundle below a consumed sequence")
                document = decode(data, peer)
                if spool_path(spool, document["sequence"]) != path:
                    raise ReceiptError("bundle sequence differs from its spool name")
                if any(entry["config"]["id"] not in allowed for entry in document["sources"]):
                    raise ReceiptError("bundle names a source this peer is not registered for")
            except (OSError, ReceiptError, ValueError, TypeError, KeyError):
                invalid += 1
                continue
            for entry in document["sources"]:
                prior = merged.get(entry["config"]["id"])
                merged[entry["config"]["id"]] = {**entry, "rows": (prior["rows"] if prior else []) + entry["rows"],
                    "inventories": (prior["inventories"] if prior else []) + entry["inventories"],
                    "appended": (prior["appended"] if prior else []) + entry["appended"]}
            inputs.extend(document["inputs"])
            paths.append(path)
        for entry in merged.values():
            expanded.append({**entry["config"], "_pushed": entry})
        summaries.append({"source_ref": reference("source", source["id"]), "kind": KIND,
                          "status": "partial" if invalid else "complete" if paths else "idle",
                          "fact_count": len(paths), "reason": "observation_bundle_invalid" if invalid else None,
                          "source_detail": {"bundles": len(paths), "invalid_bundles": invalid,
                                            "sources": sorted(merged)}, "diagnostics": {}})
    return {"sources": expanded, "inputs": inputs, "paths": paths, "summaries": summaries}


def source_heads(active: Iterable[Receipt], source_ref: str) -> set[str]:
    """Usage/execution/attribution/human entities whose current head this source witnessed."""
    return {r.entity_ref for r in active if r.kind in {"usage", "execution", "attribution", "human"}
            and source_ref in r.evidence}


def commit_pushed(paths: Iterable[Path]) -> None:
    """After ingest: record the bundles this collection read as consumed, then remove them."""
    by_spool: dict[Path, list[Path]] = {}
    for path in paths:
        by_spool.setdefault(path.parent, []).append(path)
    for spool, read in by_spool.items():
        with _SpoolLock(spool):
            record = consumed(spool)
            for path in read:
                sequence = int(_NAME.fullmatch(path.name).group(1))
                record["digests"][str(sequence)] = hashlib.sha256(path.read_bytes()).hexdigest()
                record["last"] = max(record["last"], sequence)
            _write_consumed(spool, record)
            for path in read:
                try:
                    path.unlink()
                except FileNotFoundError:
                    pass
