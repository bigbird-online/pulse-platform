"""Observe-only collection: an observing node's native sources as observation bundles.

The node never reads or writes the observation stream. Each run:

1. reads the small sources (Relay Host, Pi, Codex, receipt bundles, Harness)
   through the ordinary readers, from the native parse cache;
2. reads only the CC sessions (a main transcript with its subagents) whose
   files changed since the last acknowledged bundle, or whose reading
   dependencies changed (reader code, source and workspace configuration,
   holder actors, this session's Host facts, the Host input metadata its
   transcripts name), one session at a time, so the largest transcript
   bounds memory instead of the whole history;
3. measures each source's coverage over all its files from a private index
   (file signature, digest and per-session detail);
4. writes bundles of at most ``CHUNK_BYTES`` canonical bytes each to a
   private outbox, splitting by row. Small sources leave out immutable rows
   an acknowledged bundle already carried; a snapshot input is sent when it
   differs from the value last acknowledged for its key; a changed CC
   session is sent whole.

The index changes only when the judging collector acknowledges a bundle
(``acknowledge``); an unacknowledged or refused bundle leaves its sessions
changed, so the next run reads them again. A caller that delivers each bundle
as it is written (``deliver``) keeps what was acknowledged when a run ends
early: before each CC session is read, what is already read goes out as a
bundle and is acknowledged, so a run ended at any moment loses at most the
session it was reading. With a ``deadline`` a run stops starting new CC
sessions once it passes and reports how many changed sessions it left
(``Observed.remaining``); those
sources carry ``partial`` coverage (``observe_incomplete``) and the next run
continues with them. A backfill (``backfill`` token) spans runs the same way:
a session read under the token is not read again for it.
"""
from __future__ import annotations

import hashlib
import json
import re
import os
import stat
import time
from collections import Counter
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping

from .native_observations import SCHEMA, bundle_config, encode
from .receipts import Receipt, ReceiptError, canonical, digest

INDEX_SCHEMA = "pulse_native_observe_index/v1"
OUTBOX_SCHEMA = "pulse_native_observe_outbox/v1"
CHUNK_BYTES = 32 * 1024 * 1024
SMALL_KINDS = {"relay_host", "pi", "codex", "agy", "receipts", "harness_bundle", "harness_root", "harness_core"}
# Coverage reason of a CC source a run stopped reading before its changed sessions were all read.
INCOMPLETE_REASON = "observe_incomplete"


class Observed(list):
    """The run's bundles in sequence order, and the changed CC sessions it left for the next run."""
    remaining = 0


def _private_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}")
    with open(temporary, "wb") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
    os.chmod(temporary, 0o600)
    os.replace(temporary, path)


def load_index(path: Path) -> dict[str, Any]:
    """The acknowledged state; a missing or foreign index means everything is new."""
    try:
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise ReceiptError("observe index must be a private regular file")
        value = json.loads(path.read_bytes())
    except FileNotFoundError:
        value = None
    if not isinstance(value, Mapping) or value.get("schema") != INDEX_SCHEMA:
        return {"schema": INDEX_SCHEMA, "sequence": 0, "groups": {}, "sent": {}, "snapshots": {}}
    return {"schema": INDEX_SCHEMA, "sequence": int(value["sequence"]), "groups": dict(value["groups"]),
            "sent": {k: set(v) for k, v in value["sent"].items()}, "snapshots": dict(value.get("snapshots", {}))}


def save_index(path: Path, index: Mapping[str, Any]) -> None:
    _private_write(path, canonical({"schema": INDEX_SCHEMA, "sequence": index["sequence"], "groups": index["groups"],
                                    "sent": {k: sorted(v) for k, v in index["sent"].items()},
                                    "snapshots": index["snapshots"]}))


def cc_groups(source: Mapping[str, Any]) -> dict[str, list[Path]]:
    """CC files by native session: a main transcript with its subagent transcripts.

    Raises OSError when any directory under the root cannot be listed: an
    unlisted directory is not an empty one (``rglob`` would skip it silently).
    """
    root = Path(source["path"])
    if root.is_dir():
        def unlisted(error: OSError) -> None:
            raise error
        files = sorted(Path(directory) / name for directory, _, names in os.walk(root, onerror=unlisted)
                       for name in names if name.endswith(".jsonl"))
    else:
        files = [root]
    groups: dict[str, list[Path]] = {}
    for f in files:
        if f.parent.name == "subagents":
            key = str(f.parent.parent)
        elif f.name == "main.jsonl":
            key = str(f.parent)
        else:
            key = str(f.with_suffix(""))
        groups.setdefault(key, []).append(f)
    return groups


def _signature(files: Iterable[Path]) -> dict[str, list[int]]:
    result = {}
    for f in files:
        info = f.lstat()
        result[str(f)] = [info.st_size, info.st_mtime_ns, info.st_ino]
    return result


def _sent_key(row: Receipt) -> str:
    """An immutable row's identity apart from when it was read: what an acknowledged bundle carried."""
    return digest([row.kind, row.entity_ref, row.supersedes, row.occurred_at, row.producer, row.payload])[:24]


def _snapshot(row: Receipt) -> bool:
    from .request_inputs import KEYS
    return row.kind == "native_input" and row.payload["input_class"] in KEYS


def _inputs(entry: Mapping[str, Any], read_at: str) -> list[Receipt]:
    from .native_inputs import launch_inputs
    from .request_inputs import (CC_REQUEST, CODEX_REQUEST, HOLDER_SESSION, PI_REQUEST, ROOT_LINK,
                                 SESSION_DECLARATION, cc_values, codex_values, holder_values, pi_values,
                                 root_values, snapshot_inputs)
    ref = entry["source_ref"]
    rows = launch_inputs(entry["launches"], source_ref=ref, observed=read_at)
    for input_class, values in ((CODEX_REQUEST, codex_values(entry["request_inputs"])),
                                (SESSION_DECLARATION, entry["session_binding"] or {}),
                                (CC_REQUEST, cc_values(entry["cc_requests"] or {})),
                                (PI_REQUEST, pi_values(entry["pi_requests"] or {})),
                                (HOLDER_SESSION, holder_values(entry.get("holder", []))),
                                (ROOT_LINK, root_values(entry.get("root_links", {})))):
        rows += snapshot_inputs(input_class, values, source_ref=ref, read_at=read_at)
    return rows


_TOKEN = re.compile(rb"\[Host (?:human input|internal delivery) reference: ([a-f0-9]{64})\.")


def _tokens(files: Iterable[Path]) -> list[str]:
    """Host input references a session's transcripts name; their metadata is a reading dependency."""
    found: set[str] = set()
    for f in files:
        with f.open("rb") as stream:
            for line in stream:
                if b"Host " in line:
                    found.update(m.decode() for m in _TOKEN.findall(line))
    return sorted(found)


def _scope_facts(decided: Iterable[Receipt]) -> dict[str, list[str]]:
    """Per native scope, the Host facts this run read that CC readers consult (authority, executor, input)."""
    from collections import defaultdict
    rows = list(decided)
    scopes: dict[str, set[str]] = defaultdict(set)
    for r in rows:
        if r.kind == "usage" and r.payload.get("native_scope_ref"):
            scopes[r.entity_ref].add(r.payload["native_scope_ref"])
            if r.payload.get("execution_ref"):
                scopes[r.payload["execution_ref"]].add(r.payload["native_scope_ref"])
    result: dict[str, list[str]] = defaultdict(list)
    for r in rows:
        target = r.payload.get("target_ref") if r.kind == "attribution" else r.entity_ref
        for scope in scopes.get(target, ()):
            result[scope].append(r.fact_ref)
    return {scope: sorted(refs) for scope, refs in result.items()}


def _metadata_state(sources: Iterable[Mapping[str, Any]], tokens: Iterable[str]) -> list:
    from .pi_context import input_directories
    directories = [d for s in sources if s.get("kind") in {"relay_host", "pi"} for d in input_directories(s)]
    state = []
    for token in tokens:
        for directory in directories:
            try:
                info = (directory / (token + ".json")).lstat()
                state.append([token, str(directory), info.st_size, info.st_mtime_ns])
            except FileNotFoundError:
                state.append([token, str(directory), None])
    return state


def _snapshot_files(folder: Path, cache: dict) -> list[list]:
    """Each wi_*.json beside the transcripts: name, authority it names, content digest, and the
    file type, owner and permission bits the snapshot reader checks before trusting it."""
    if folder not in cache:
        entries = []
        for candidate in sorted(folder.glob("wi_*.json")):
            try:
                info = candidate.lstat()
                access = [stat.S_IFMT(info.st_mode), info.st_uid, stat.S_IMODE(info.st_mode)]
            except OSError:
                access = None
            try:
                data = candidate.read_bytes()
                authority = json.loads(data).get("authority_ref")
            except (OSError, ValueError, AttributeError):
                data, authority = b"", None
            entries.append([candidate.name, authority, hashlib.sha256(data).hexdigest(), access])
        cache[folder] = entries
    return cache[folder]


def _boundary_state(files: Iterable[Path], cache: dict) -> list:
    """The bounded CC snapshots and baselines bind_cc_boundaries would read for these transcripts.

    Snapshots are often written after a session's last transcript line, so a
    new, updated or restored snapshot must read the session again.
    """
    from .cc_usage import cc_authority_ref
    state = []
    for f in files:
        folder = f.parent / "compute-cost"
        if folder.is_symlink():
            state.append([str(folder), "unsafe"])
            continue
        if not folder.is_dir():
            continue
        entries = _snapshot_files(folder, cache)
        authority = cc_authority_ref(f.stem)
        bindings = {e[0].rsplit("-", 1)[0] for e in entries if e[1] == authority}
        state.append([str(folder), [e for e in entries if e[1] == authority or e[0].rsplit("-", 1)[0] in bindings]])
    return state


def _dependencies(base: str, scope_facts: Mapping[str, list[str]], sources: list, key: str,
                  tokens: Iterable[str], files: Iterable[Path], cache: dict) -> str:
    """Everything besides its own bytes that a session's reading depends on."""
    from .receipts import reference
    scope = reference("native_session", ["cc", Path(key).name])
    return digest([base, scope_facts.get(scope, []), _metadata_state(sources, tokens), _boundary_state(files, cache)])


def _base_dependencies(registry: Mapping[str, Any], source: Mapping[str, Any]) -> str:
    """Reader code, the source's configuration, workspace registry and holder actors."""
    from .calculation_cache import engine_digest
    parts: list[Any] = [engine_digest(), source]
    for field in ("workspace_registry_path", "tenant_registry_path"):
        path = registry.get(field)
        parts.append(hashlib.sha256(Path(path).read_bytes()).hexdigest() if path and Path(path).is_file() else None)
    holder = registry.get("core_session_writes")
    if holder is not None:
        try:
            from .holder_supplement import read_session_writes
            namespace, _, actors = read_session_writes(Path(holder["path"]))
            parts.append([namespace, sorted(actors), holder.get("surface")])
        except (OSError, ValueError, TypeError, KeyError):
            parts.append("holder_unavailable")
    return digest(parts)


def _group_record(files: Mapping[str, list[int]], entry: Mapping[str, Any], *, tokens: list[str],
                  dependencies: str) -> dict[str, Any]:
    coverage = entry["coverage"]
    detail = coverage["detail"]
    # A reading that failed outright is kept as such and retried until one succeeds.
    failed = coverage["reason"] == "source_unreadable_or_invalid"
    return {"files": None if failed else dict(files), "tokens": tokens, "dependencies": dependencies,
            "status": coverage["status"], "reason": coverage["reason"],
            "digests": entry["digests"], "raw_count": coverage["raw_count"],
            "detail": {k: detail.get(k) for k in ("files_seen", "files_read", "files_invalid", "errors",
                                                   "latest_usage_at")},
            "diagnostics": [d for d, n in coverage["diagnostics"].items() for _ in range(n)]}


def _source_coverage(source: Mapping[str, Any], groups: Mapping[str, Mapping[str, Any]]) -> dict[str, Any]:
    """The coverage a full collection measures, from every session's latest record."""
    from .events import parse_timestamp
    from .receipt_sources import LOCAL_DIAGNOSTICS
    from .source_layout import source_detail
    detail = source_detail(source)
    diagnostics: list[str] = []
    digests: list[str] = []
    raw = 0
    for key in sorted(groups):
        record = groups[key]
        for field in ("files_seen", "files_read", "files_invalid"):
            detail[field] += record["detail"][field] or 0
        for category, count in (record["detail"]["errors"] or {}).items():
            detail["errors"][category] = detail["errors"].get(category, 0) + count
        latest = record["detail"]["latest_usage_at"]
        if latest and (detail["latest_usage_at"] is None
                       or parse_timestamp(latest, "usage") > parse_timestamp(detail["latest_usage_at"], "usage")):
            detail["latest_usage_at"] = latest
        diagnostics += record["diagnostics"]
        digests += record["digests"]
        raw += record["raw_count"]
    status, reason = ("idle", None) if not groups else ("unknown", "execution_inventory_not_observed")
    degrading = [d for d in diagnostics if d not in LOCAL_DIAGNOSTICS]
    if degrading:
        status, reason = "partial", degrading[0]
    failures = [groups[k] for k in sorted(groups) if groups[k].get("reason") == "source_unreadable_or_invalid"]
    if failures:
        # As a full collection: one failed reading makes the source unreachable or partial.
        worst = next((r for r in failures if r["status"] == "unreachable"), failures[0])
        status, reason = worst["status"], worst["reason"]
    return {"status": status, "reason": reason, "expected": None, "observed": None,
            "watermark": digest(sorted(digests)) if digests else None, "raw_count": raw, "detail": detail,
            "diagnostics": dict(sorted(Counter(diagnostics).items()))}


class _Outbox:
    """Bundles of at most ``CHUNK_BYTES`` canonical bytes, each with the index change its acknowledgement applies.

    Rows are split across bundles one by one; a session's index record rides
    in the bundle that carries its last rows, so it advances only when every
    bundle with its rows was acknowledged in order. A snapshot input is sent
    when it differs from the value last acknowledged for its key; an
    immutable row once.
    """

    def __init__(self, path: Path, peer: str, index: dict[str, Any], read_at: str,
                 deliver: Callable[[Path], None] | None = None):
        self.path, self.peer, self.index, self.read_at, self.deliver = path, peer, index, read_at, deliver
        self.written: list[Path] = []
        # Queued in this run (any chunk), so one run never sends a row or value twice.
        self.queued: dict[str, set[str]] = {}
        self.queued_snapshots: dict[str, str] = {}
        self._reset()

    def _reset(self) -> None:
        self.sources: dict[str, dict[str, Any]] = {}
        self.inputs: list[Receipt] = []
        self.groups: dict[str, Any] = {}
        self.sent: dict[str, set[str]] = {}
        self.snapshots: dict[str, str] = {}
        self.size = 0

    def _source(self, entry: Mapping[str, Any], coverage: Mapping[str, Any]) -> dict[str, Any]:
        source_id = entry["config"]["id"]
        if source_id not in self.sources:
            self.sources[source_id] = {"config": entry["config"], "rows": [], "inventories": [], "appended": [],
                                       "coverage": dict(coverage)}
            self.size += len(canonical({"config": entry["config"], "coverage": coverage})) + 64
        return self.sources[source_id]

    def _put(self, entry, coverage, field: str, row: Receipt, *, tracked: bool) -> None:
        size = len(canonical(row.to_dict())) + 1
        if self.size + size > CHUNK_BYTES and self.pending():
            self.flush()
        source = self._source(entry, coverage)
        (self.inputs if field == "inputs" else source[field]).append(row)
        self.size += size
        # What this row's acknowledgement proves travels with the bundle that carries it.
        if _snapshot(row):
            self.snapshots[row.entity_ref] = digest(row.payload)
        elif tracked:
            self.sent.setdefault(entry["config"]["id"], set()).add(_sent_key(row))

    def add(self, entry: Mapping[str, Any], coverage: Mapping[str, Any], rows: list[Receipt],
            inputs: list[Receipt], groups: Mapping[str, Any] | None = None, *, filtered: bool = True) -> None:
        source_id = entry["config"]["id"]
        sent = self.index["sent"].get(source_id, set())
        queued = self.queued.setdefault(source_id, set())

        def fresh(row: Receipt) -> bool:
            if _snapshot(row):
                value = digest(row.payload)
                if self.queued_snapshots.get(row.entity_ref, self.index["snapshots"].get(row.entity_ref)) == value:
                    return False  # Equal to the value last acknowledged (or queued) for this key.
                self.queued_snapshots[row.entity_ref] = value
                return True
            key = _sent_key(row)
            if key in sent or key in queued:
                return False
            queued.add(key)
            return True
        rows = [r for r in rows if fresh(r)] if filtered else list(rows)
        kept = [r for r in inputs if fresh(r)]
        self._source(entry, coverage)["coverage"] = dict(coverage)
        for field, items, tracked in (("rows", rows, filtered), ("inventories", entry["inventories"], False),
                                      ("appended", entry.get("appended", []), False), ("inputs", kept, True)):
            for row in items:
                self._put(entry, coverage, field, row, tracked=tracked)
        self._source(entry, coverage)["coverage"] = dict(coverage)
        self.groups.update({f"{source_id}\0{k}": v for k, v in (groups or {}).items()})

    def pending(self) -> bool:
        return bool(self.sources or self.inputs)

    def flush(self) -> None:
        if not self.pending():
            return
        sequence = max(self.index["sequence"] + 1, time.time_ns() // 1000)
        self.index["sequence"] = sequence
        document = {"schema": SCHEMA, "peer": self.peer, "sequence": sequence, "created_at": self.read_at,
                    "sources": [{"config": s["config"], "rows": [r.to_dict() for r in s["rows"]],
                                 "inventories": [r.to_dict() for r in s["inventories"]],
                                 "appended": [r.to_dict() for r in s["appended"]],
                                 "coverage": s["coverage"]} for s in self.sources.values()],
                    "inputs": [r.to_dict() for r in self.inputs]}
        bundle = self.path / f"{sequence:020d}.json.gz"
        _private_write(bundle, encode(document))
        _private_write(bundle.with_suffix(".delta"), canonical({"schema": OUTBOX_SCHEMA, "groups": self.groups,
            "sent": {k: sorted(v) for k, v in self.sent.items()}, "snapshots": self.snapshots}))
        self.written.append(bundle)
        self._reset()
        if self.deliver is not None:
            self.deliver(bundle)  # Pushed and acknowledged before the next session is read.


def pending_bundles(outbox: Path) -> list[Path]:
    return sorted(outbox.glob("*.json.gz")) if outbox.is_dir() else []


def acknowledge(index_path: Path, bundle: Path) -> None:
    """The judging collector holds this bundle: apply its index change, then drop it."""
    index = load_index(index_path)
    delta = json.loads(bundle.with_suffix(".delta").read_bytes())
    for key, record in delta["groups"].items():
        if record is None:
            index["groups"].pop(key, None)
        else:
            index["groups"][key] = record
    for source_id, keys in delta["sent"].items():
        index["sent"].setdefault(source_id, set()).update(keys)
    index["snapshots"].update(delta.get("snapshots", {}))
    index["sequence"] = max(index["sequence"], int(bundle.name.split(".")[0]))
    save_index(index_path, index)
    bundle.unlink()
    bundle.with_suffix(".delta").unlink()


def discard(bundle: Path) -> None:
    """A refused bundle: its sessions stay changed in the index and are read again."""
    for path in (bundle, bundle.with_suffix(".delta")):
        try:
            path.unlink()
        except FileNotFoundError:
            pass


def observe(registry: Mapping[str, Any], *, peer: str, index_path: Path, outbox: Path, read_at: str,
            backfill: bool | str = False, force_recompute: bool = False, deadline: float | None = None,
            deliver: Callable[[Path], None] | None = None) -> Observed:
    """Write this run's bundles to the outbox; return them in sequence order.

    ``backfill`` reads every CC session again, once, so that inputs of
    sessions that no longer change are recorded too; as a string it names a
    backfill that spans runs, and a session already read under it is not read
    again for it; a CC source unreachable during a backfill counts its known
    sessions (at least one) in ``remaining``, so the backfill continues once
    the source is back. ``deadline`` (``time.monotonic()``) stops reading further
    changed sessions once passed; each run still reads at least one.
    ``deliver`` receives each bundle as soon as it is written; with it, every
    CC session starts with nothing unacknowledged.
    """
    from .receipt_sources import observe_registered_sources
    from .source_retirement import load_retirements
    if pending_bundles(outbox):
        raise ReceiptError("unacknowledged bundles must be pushed before observing again")
    index = load_index(index_path)
    retirements = load_retirements()
    cache_root = Path(registry["ledger_root"])
    sources = [s for s in registry["sources"] if s.get("id") not in retirements]
    box = _Outbox(outbox, peer, index, read_at, deliver)
    token = backfill if isinstance(backfill, str) else None
    result, read = Observed(), 0
    small = observe_registered_sources(registry, cache_root=cache_root, retirements=retirements,
                                       run={s["id"] for s in sources if s.get("kind") in SMALL_KINDS},
                                       force_recompute=force_recompute)
    for entry in small["sources"]:
        box.add(entry, entry["coverage"], entry["rows"], _inputs(entry, read_at))
    scope_facts = _scope_facts(small["decided"])
    for source in sources:
        if source.get("kind") != "cc":
            continue
        known = {k.split("\0", 1)[1]: v for k, v in index["groups"].items() if k.split("\0", 1)[0] == source["id"]}
        root = Path(source["path"])
        try:
            groups = None if root.is_symlink() or not root.exists() else cc_groups(source)
        except OSError:
            groups = None  # Present but not listable (permissions): unknown, never empty.
        if groups is None:
            # A full collection refuses the whole source; nothing is read, nothing advances,
            # and no known session is taken as removed.
            box.add({"config": bundle_config(source), "inventories": []},
                    {**_source_coverage(source, {}), "status": "unreachable",
                     "reason": "source_unreadable_or_invalid"}, [], [], filtered=False)
            if backfill:
                # Its sessions are still owed to the backfill: the run is incomplete until the source is back.
                result.remaining += max(1, len(known))
            continue
        records = {k: v for k, v in known.items() if k in groups}
        removed = {k: None for k in known if k not in groups}
        base = _base_dependencies(registry, source)
        snapshot_cache: dict = {}

        def stale(key: str) -> bool:
            record = known.get(key)
            try:
                if token is not None and record is not None and record.get("backfill") != token:
                    return True
                if (backfill and token is None) or record is None or record.get("files") != _signature(groups[key]):
                    return True
                # Unchanged bytes still need a new reading when what they were read against changed.
                return record.get("dependencies") != _dependencies(base, scope_facts, sources, key,
                    record.get("tokens", []), groups[key], snapshot_cache)
            except OSError:
                return True  # Its reading decides; a failure there is recorded and retried.
        changed = sorted(k for k in groups if stale(k))
        config = bundle_config(source)
        if not changed:
            box.add({"config": config, "inventories": []}, _source_coverage(source, records), [], [], removed,
                    filtered=False)
            continue
        for position, key in enumerate(changed):
            if deadline is not None and read and time.monotonic() >= deadline:
                # Out of time: what was read is delivered; the rest stays changed for the next run.
                result.remaining += len(changed) - position
                coverage = _source_coverage(source, records)
                if coverage["status"] != "unreachable":
                    coverage.update(status="partial", reason=INCOMPLETE_REASON)
                box.add({"config": config, "inventories": []}, coverage, [], [], removed, filtered=False)
                break
            if box.deliver is not None:
                # A confirmation point before work that may outlast the run: what was read is acknowledged.
                box.flush()
            read += 1
            try:
                signature = _signature(groups[key])
            except OSError:
                signature = {str(f): None for f in groups[key]}  # A file vanished or cannot be examined.
            run = observe_registered_sources(registry, cache_root=cache_root, retirements=retirements,
                run={source["id"]}, facts=small["decided"], host_observations=small["host_observations"],
                files={source["id"]: set(signature)}, force_recompute=force_recompute)
            entry, = run["sources"]
            try:
                tokens = _tokens(groups[key])
                dependencies = _dependencies(base, scope_facts, sources, key, tokens, groups[key], snapshot_cache)
            except OSError:
                tokens, dependencies = [], None
            records[key] = _group_record(signature, entry, tokens=tokens, dependencies=dependencies)
            if token is not None:
                records[key]["backfill"] = token
            if dependencies is None or None in signature.values():
                # One session's unreadable file fails that session only; it is kept failed and retried.
                records[key].update(files=None, status="unreachable", reason="source_unreadable_or_invalid")
            box.add(entry, _source_coverage(source, records), entry["rows"], _inputs(entry, read_at),
                    {key: records[key], **removed}, filtered=False)
            removed = {}
    box.flush()
    result.extend(box.written)
    return result
