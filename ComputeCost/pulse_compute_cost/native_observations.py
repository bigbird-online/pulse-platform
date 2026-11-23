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
from dataclasses import replace
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping

from .events import parse_timestamp
from .receipts import Receipt, ReceiptError, canonical, reference

SCHEMA = "pulse_native_observations/v1"
KIND = "native_push"
# The native core's WI facts export: every node exports the same shared journal.
CORE_KIND = "harness_core"
MAX_WIRE_BYTES = 64 * 1024 * 1024
MAX_DOCUMENT_BYTES = 512 * 1024 * 1024
# Decoded bundle bytes one collection judges per spool. The collector holds every
# row it judges in memory; an unbounded backlog swaps the publisher until the
# core's Git reads time out (a ledger failure, not a memory one).
# Later bundles wait in sequence order for the next collection; the first one
# is always read, so the spool always advances.
MAX_COLLECTION_DOCUMENT_BYTES = 256 * 1024 * 1024
SOURCE_KINDS = {"relay_host", "pi", "cc", "codex", "agy", "harness_bundle", "harness_root", "harness_core", "receipts"}
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


def _decoded_size(data: bytes) -> int:
    """The decoded length of every gzip member, counted while streaming.

    Nothing is kept: a backlog is sized without holding its documents. A
    stream that does not decode counts as its wire length; ``decode`` then
    refuses it. Counting stops past MAX_DOCUMENT_BYTES, which ``decode`` refuses too.
    """
    size = 0
    try:
        with gzip.GzipFile(fileobj=io.BytesIO(data)) as stream:
            while size <= MAX_DOCUMENT_BYTES:
                chunk = stream.read(1024 * 1024)
                if not chunk:
                    break
                size += len(chunk)
    except (OSError, EOFError):
        return len(data)
    return size


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


def expand_pushed(sources: Iterable[Mapping[str, Any]], retirements: Mapping[str, Any], *,
                  limits: Mapping[str, int] | None = None,
                  visit: Callable[[str, Mapping[str, Any]], bool] | None = None) -> dict[str, Any]:
    """Registered sources with every spooled bundle expanded into its bundled sources.

    Bundles of one peer apply in sequence order: rows concatenate, coverage and
    configuration come from the last bundle naming the source. Consumed
    bundles are tracked apart from unresolved ones: a bundle that does not
    validate, or that sits at or below an already consumed sequence, stays in
    the spool and is reported every collection until someone resolves it; it
    is never read, and later valid bundles are still read.

    A source keeps one kind across bundles: a bundle that names a source
    with another kind than an earlier bundle of this collection is invalid.

    One collection reads bundles up to MAX_COLLECTION_DOCUMENT_BYTES of decoded
    JSON (counted by streaming every gzip member, before decoding); the rest stay spooled for
    the next collection, which reads them after these are consumed. The
    summary is then partial (``observation_backlog``) and names how many wait.

    A bundled core facts source (``harness_core``) whose ID the collector
    registers itself is not a second source: both copies are a native core's
    export of the one shared WI journal, so an Outcome has the same identity
    in both. Its rows go to ``core`` (source ID -> [(row, bundle watermark)])
    and the collector reads them with its own copy of that source
    (``merge_pushed_core``); the summary names them (``merged_into_local``).
    Any other ID a bundle shares with a registered source stays a
    registration conflict.

    ``limits`` (native_push source ID -> bundles) reads at most that many
    bundles of a spool; ``visit`` sees each decoded bundle before it is read
    and, returning False, leaves it and the later ones of that spool waiting.
    Either way the rest wait as an ``observation_backlog``. The first bundle
    of a spool is always read.
    """
    expanded, inputs, paths, summaries = [], [], [], []
    core: dict[str, list[tuple[Receipt, str | None]]] = {}
    sources = list(sources)
    local_core = {s.get("id") for s in sources if isinstance(s, Mapping) and s.get("kind") == CORE_KIND
                  and s.get("id") not in retirements}
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
        spool, merged, invalid, deferred, budget = Path(source["path"]), {}, 0, 0, 0
        names, done, read, taken = [], {"last": 0, "digests": {}}, False, 0
        if spool.exists():
            _private_spool(spool)
            with _SpoolLock(spool):
                done = consumed(spool)
                names = sorted(p for p in spool.iterdir() if _NAME.fullmatch(p.name))
        for index, path in enumerate(names):
            sequence = int(_NAME.fullmatch(path.name).group(1))
            try:
                data = path.read_bytes()
                if done["digests"].get(str(sequence)) == hashlib.sha256(data).hexdigest():
                    continue  # Consumed already; its removal was interrupted.
                size = _decoded_size(data)
                if read and (budget + size > MAX_COLLECTION_DOCUMENT_BYTES
                             or limits is not None and taken >= limits.get(source["id"], len(names))):
                    deferred = len(names) - index
                    break
                if sequence <= done["last"]:
                    raise ReceiptError("an unconsumed bundle below a consumed sequence")
                document = decode(data, peer)
                if spool_path(spool, document["sequence"]) != path:
                    raise ReceiptError("bundle sequence differs from its spool name")
                if any(entry["config"]["id"] not in allowed for entry in document["sources"]):
                    raise ReceiptError("bundle names a source this peer is not registered for")
                if any(entry["config"]["id"] in merged
                       and merged[entry["config"]["id"]]["config"]["kind"] != entry["config"]["kind"]
                       for entry in document["sources"]):
                    raise ReceiptError("bundle changes the kind of a bundled source")
                if any(entry["config"]["id"] in local_core and entry["config"]["kind"] == CORE_KIND
                       and (entry["inventories"] or entry["appended"]) for entry in document["sources"]):
                    raise ReceiptError("a core facts source carries only its rows")
            except (OSError, ReceiptError, ValueError, TypeError, KeyError):
                invalid += 1
                continue
            if visit is not None and not visit(source["id"], document) and read:
                deferred = len(names) - index
                break
            read, budget = True, budget + size
            for entry in document["sources"]:
                if entry["config"]["id"] in local_core and entry["config"]["kind"] == CORE_KIND and entry["rows"]:
                    core.setdefault(entry["config"]["id"], []).extend(
                        (row, entry["coverage"]["watermark"]) for row in entry["rows"])
                prior = merged.get(entry["config"]["id"])
                if prior is not None:
                    # Extend in place: copying the joined lists per bundle is quadratic in a backlog.
                    for field in ("rows", "inventories", "appended"):
                        prior[field].extend(entry[field])
                        entry[field] = prior[field]
                merged[entry["config"]["id"]] = entry
            inputs.extend(document["inputs"])
            paths.append(path)
            taken += 1
        local = sorted(i for i, entry in merged.items()
                       if i in local_core and entry["config"]["kind"] == CORE_KIND)
        for source_id, entry in merged.items():
            if source_id not in local:
                expanded.append({**entry["config"], "_pushed": entry})
        detail = {"bundles": len(paths), "invalid_bundles": invalid,
                  "sources": sorted(merged), "merged_into_local": local}
        if deferred:
            detail["deferred_bundles"] = deferred
        summaries.append({"source_ref": reference("source", source["id"]), "kind": KIND,
                          "status": "partial" if invalid or deferred else "complete" if paths else "idle",
                          "fact_count": len(paths),
                          "reason": ("observation_bundle_invalid" if invalid else
                                     "observation_backlog" if deferred else None),
                          "source_detail": detail, "diagnostics": {}})
    return {"sources": expanded, "inputs": inputs, "paths": paths, "summaries": summaries, "core": core}


def pushed_core_marker(source_ref: str) -> str:
    """Evidence that a core facts row reached the stream from a pushed copy: every
    such row carries it, whether it filled a gap beside the collector's own
    source or was judged as the pushed source while the collector had none."""
    return reference("pushed_core_row", source_ref)


def merge_pushed_core(source_ref: str, local: Iterable[Receipt], recorded: Callable[[str], bool],
                      pushed: Iterable[tuple[Receipt, str | None]],
                      filled: Callable[[str], Receipt | None] = lambda entity: None) -> list[tuple[Receipt, str | None]]:
    """Pushed core facts rows for Outcomes the local export does not decide.

    The local export decides every entity it has ever supplied: a pushed row
    is dropped when this read (``local``) holds its entity. Otherwise the last
    pushed row per entity counts, so the result never depends on how a backlog
    of bundles is split across collections:
    - the stream does not hold the entity (``recorded(entity_ref)`` false,
      asked once per entity): the row fills it;
    - the stream's single current head (``filled``) is a fill pushes alone
      supplied (``pushed_fill``): a different row supersedes it, the same
      content keeps it;
    - any other head (the local export's version or confirmation, or
      concurrent heads): dropped.
    Kept rows carry ``pushed_core_marker``, ``pushed_fill_marker`` and their own snapshot witness (the
    bundle's watermark of that source), never the local file's. A later local
    read of that entity supersedes the marked head (``supersedes_pushed_core``).
    The bundle is consumed after this collection, so every Outcome it named is
    then in the stream, either as the local version or as a filled row.
    """
    pushed = list(pushed)
    if not pushed:
        return []
    held = {row.entity_ref for row in local}
    last: dict[str, tuple[Receipt, str | None]] = {}
    for row, watermark in pushed:
        if row.entity_ref not in held:
            last.pop(row.entity_ref, None)
            last[row.entity_ref] = (row, watermark)
    kept = []
    for entity, (row, watermark) in last.items():
        row = mark_pushed_core(source_ref, row)
        row = replace(row, evidence=tuple(sorted(set(row.evidence) | {pushed_fill_marker(source_ref)})))
        if recorded(entity):
            prior = filled(entity)
            if not pushed_fill(source_ref, prior):
                continue
            if (row.kind, row.payload, row.occurred_at, row.producer) == (
                    prior.kind, prior.payload, prior.occurred_at, prior.producer):
                continue
            row = Receipt.make(row.kind, None, row.payload, occurred_at=row.occurred_at,
                               observed_at=row.observed_at, producer=row.producer, evidence=row.evidence,
                               entity_ref=entity, supersedes=prior.fact_ref)
        kept.append((row, reference("source_snapshot", [source_ref, watermark]) if watermark else None))
    return kept


def mark_pushed_core(source_ref: str, row: Receipt) -> Receipt:
    return replace(row, evidence=tuple(sorted(set(row.evidence) | {pushed_core_marker(source_ref)})))


def supersedes_pushed_core(source_ref: str, prior: Receipt | None, row: Receipt) -> bool:
    """A local core facts row replaces a head only a pushed copy supplied, unless they are the same fact."""
    return (prior is not None and pushed_core_marker(source_ref) in prior.evidence
            and row.fact_ref != prior.fact_ref and row.supersedes != prior.fact_ref)


def pushed_fill_marker(source_ref: str) -> str:
    """Evidence that ``merge_pushed_core`` filled this head from pushed copies.

    Only fills carrying it can be shown to come from pushes alone; a head
    filled before it existed may since have been confirmed by the local export
    without a record of that, so a pushed copy never replaces it.
    """
    return reference("pushed_core_fill", source_ref)


def local_core_marker(source_ref: str) -> str:
    """Evidence that the collector's own core export confirmed a head a pushed copy filled."""
    return reference("local_core_row", source_ref)


def pushed_fill(source_ref: str, prior: Receipt | None) -> bool:
    """The head is provably from pushed copies alone: filled by ``merge_pushed_core``
    (``pushed_fill_marker``) and never confirmed by the local export."""
    return (prior is not None and pushed_fill_marker(source_ref) in prior.evidence
            and local_core_marker(source_ref) not in prior.evidence)


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
