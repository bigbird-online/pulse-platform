"""Reuse parsed native bytes, never task authority or source availability.

Every hit requires current enrolled files and exact content/dependency hashes.
SCL bindings, Host/native reconciliation and receipt ingestion run afterwards.
This disposable cache contains no raw prompts and is never a second ledger.
"""
from __future__ import annotations
from collections import Counter
from dataclasses import asdict
from hashlib import sha256
import os
from pathlib import Path
import re
from typing import Any, Mapping

from .calculation_cache import CalculationCache, engine_digest
from .receipts import Receipt, digest


def file_signature(paths: list[Path]) -> list[list[str]]:
    result = []
    for path in sorted(set(paths)):
        if path.is_symlink() or not path.is_file():
            raise ValueError("native cache dependency unavailable")
        result.append([str(path), sha256(path.read_bytes()).hexdigest()])
    return result


def input_signature(source: Mapping[str, Any] | None) -> list:
    from .pi_context import input_directories, recovery_directories
    result = []
    recoveries = recovery_directories(source or {})
    for folder in input_directories(source or {}) + recoveries:
        if folder in recoveries and not folder.exists() and not folder.is_symlink():
            result.append([str(folder), None])
            continue
        st = folder.lstat()
        if folder.is_symlink() or not folder.is_dir() or st.st_uid != os.getuid() or st.st_mode & 0o077:
            raise ValueError("native input dependency unavailable")
        files = []
        for path in sorted(folder.glob("*.json")):
            pattern = r"operation_v1_feishu_recovery_[a-f0-9]{64}" if folder in recoveries else r"[a-f0-9]{64}"
            if not re.fullmatch(pattern, path.stem):
                continue
            st = path.lstat()
            if path.is_symlink() or not path.is_file() or st.st_uid != os.getuid() or st.st_mode & 0o077 or st.st_size > 1024*1024:
                raise ValueError("native input dependency is unsafe")
            files.append([path.name, sha256(path.read_bytes()).hexdigest()])
        result.append([str(folder), files])
    if source and source.get("runtime_manifest"):
        from .source_layout import manifest_identity, manifest_paths
        manifest_paths(source)
        # Parsing depends only on the selected profiles' collector fields.
        result.append(["runtime_manifest_profiles.v2", manifest_identity(source)])
    return result


def _bundle_lines(data: bytes, start: int = 0):
    """Match bytes.splitlines without copying the whole prefix or line list."""
    position = start
    for boundary in re.compile(rb"\r\n|\r|\n").finditer(data, start):
        yield data[position:boundary.start()]
        position = boundary.end()
    if position < len(data):
        yield data[position:]


class NativeParseCache:
    def __init__(self, root: Path, *, force: bool = False):
        self.cache = CalculationCache(root, force=force)
        self.parsed = 0
        self.reused = 0
        # Receipt-bundle cursors differ from ordinary parse caches: a hit omits
        # the validated prefix instead of replaying cached rows. Stage cursor
        # updates in memory until the matching ledger ingest is durable.
        self._pending_receipt_bundles: dict[str, tuple[object, dict[str, Any]]] = {}
        # Per accumulating list: the (kind, entity_ref) histories `_revision`
        # would otherwise rescan the whole list for on every row.
        self._histories: dict[int, tuple[list[Receipt], int, dict]] = {}

    def _history_index(self, into: list[Receipt]) -> dict:
        """Index `into` by (kind, entity_ref), adding rows other readers appended since."""
        entry = self._histories.get(id(into))
        if entry is None or entry[0] is not into:
            entry = (into, 0, {})
        _, synced, histories = entry
        for row in into[synced:]:
            histories.setdefault((row.kind, row.entity_ref), []).append(row)
        return histories

    def _revise(self, into: list[Receipt], rows: list[Receipt]) -> None:
        from .receipt_sources import _revision
        histories = self._history_index(into)
        try:
            for row in rows:
                _revision(into, row, histories=histories)
        finally:
            self._histories[id(into)] = (into, len(into), histories)

    def key(self, path: Path, data: bytes, context: object = None) -> tuple[str, list]:
        return "native-"+digest(str(path)), [engine_digest(), sha256(data).hexdigest(), context]

    def receipts(self, reader, path: Path, observed: str, data: bytes, into: list[Receipt]) -> None:
        slot, key = self.key(path, data, reader.__name__)
        cached = self.cache.get(slot, key)
        if isinstance(cached, list):
            try:
                rows = [Receipt.from_dict(r) for r in cached]
                self.reused += 1
                self._revise(into, rows)
                return
            except (ValueError, TypeError, KeyError):
                pass
        rows = []
        try:
            reader(path, observed, data=data, into=rows)
        finally:
            # Preserve an already verified prefix even when the last line tears.
            self._revise(into, rows)
        self.parsed += 1
        self.cache.put(slot, key, [r.to_dict() for r in rows])

    def pi_read(self, path: Path, data: bytes, source, input_key, source_ref,
                observed: str, into: list[Receipt], reader):
        from .native_inventory import pi_prefix_inventory
        from .pi_history import history_index
        slot, key = self.key(path, data, ["pi", source, input_key, source_ref])
        cached = self.cache.get(slot, key)
        if isinstance(cached, dict):
            try:
                rows = [Receipt.from_dict(r) for r in cached["rows"]]
                inventory = Receipt.from_dict(cached["inventory"])
                history = dict(cached["history"])
                history["entries"] = {(a,b):c for a,b,c in history["entries"]}
            except (ValueError, TypeError, KeyError):
                pass
            else:
                into.extend(rows)
                self.reused += 1
                return inventory, history
        start = len(into)
        # Keep the reader's valid prefix in `into` if its final line is partial.
        reader(path, observed, into=into, data=data, source_identity=source)
        rows = into[start:]
        from .receipts import reference
        inventory = pi_prefix_inventory(data, rows, source_ref, observed,
                                        native_file_ref=reference('native_file', str(path)))
        history = history_index(path, data)
        self.parsed += 1
        self.cache.put(slot, key, {"rows":[r.to_dict() for r in rows], "inventory":inventory.to_dict(),
            "history":{**history,"entries":[[a,b,c] for (a,b),c in sorted(history["entries"].items())]}})
        return inventory, history

    def codex(self, roots, paths, observed: str, reader):
        from .adapters import UsageCollection, CollectionDiagnostics, CodexNativeObservation
        from .events import UsageEvent
        signatures = file_signature(paths)
        slot = "codex-"+digest([str(p) for p in roots])
        key = [engine_digest(), signatures]
        cached = self.cache.get(slot, key)
        if isinstance(cached, dict):
            try:
                events = [UsageEvent.from_dict(r) for r in cached["events"]]
                diag = CollectionDiagnostics(**{k:Counter(v) if isinstance(v,dict) else v
                                                for k,v in cached["diagnostics"].items()})
                observations = {k:CodexNativeObservation(**{**v,"source_path":Path(v["source_path"]),
                    "counter_fields":frozenset(v["counter_fields"]),
                    "previous_fields":frozenset(v["previous_fields"])}) for k,v in cached["observations"].items()}
            except (ValueError, TypeError, KeyError):
                pass
            else:
                self.reused += len(paths)
                return UsageCollection(events, diag), observations
        observations = {}
        collection = reader(machine="registered-source", observed_at=observed,
                            codex_sessions_dir=roots, _codex_native_observations=observations)
        self.parsed += len(paths)
        # A writer can append while parsing. Never cache that under old bytes.
        if file_signature(paths) == signatures:
            serialized = {}
            for k,v in observations.items():
                serialized[k] = {**asdict(v), "source_path":str(v.source_path),
                    "counter_fields":sorted(v.counter_fields), "previous_fields":sorted(v.previous_fields)}
            self.cache.put(slot, key, {"events":[r.to_dict() for r in collection.events],
                "diagnostics":collection.diagnostics.to_dict(), "observations":serialized})
        return collection, observations

    def receipt_bundle(self, path: Path, data: bytes, *, ledger_watermark: int,
                       known_fact_refs: set[str] | frozenset[str] = frozenset()) -> tuple[list[Receipt], int, str | None]:
        """Parse an append-only receipt bundle without making cache state authoritative.

        v1 could advance before ledger ingestion. v2 fixed that transaction
        boundary but still assumed every source fact in a committed prefix
        existed under the exact same fact_ref in the central ledger. Ledger
        evidence rebasing can legitimately change that ref. v3 therefore
        replays the full bundle whenever a delta depends on an omitted source
        predecessor that the current ledger cannot address.
        """
        import json
        from .events import parse_timestamp
        if type(ledger_watermark) is not int or ledger_watermark < 0:
            raise ValueError("invalid ledger watermark for receipt cache")
        slot = "receipt-bundle-" + digest(str(path))
        key = [engine_digest(), "receipt-bundle-prefix/v3"]
        cached = self.cache.get(slot, key)
        start = 0; count = 0; latest = None
        fields = {"bytes","sha256","count","latest_usage_at","ledger_watermark"}
        if isinstance(cached, dict) and set(cached) == fields:
            try:
                start = cached["bytes"]; count = cached["count"]; latest = cached["latest_usage_at"]
                committed = cached["ledger_watermark"]
                if (type(start) is not int or start < 0 or start > len(data)
                        or type(count) is not int or count < 0
                        or type(committed) is not int or committed < 0 or committed > ledger_watermark
                        or (latest is not None and not isinstance(latest,str))
                        or sha256(memoryview(data)[:start]).hexdigest() != cached["sha256"]
                        or (start and data[start-1:start] != b"\n")):
                    raise ValueError("receipt prefix changed")
            except (ValueError,TypeError,KeyError):
                start = 0; count = 0; latest = None
            else:
                self.reused += 1
        if start == 0:
            self.parsed += 1
        rows = []
        for raw in _bundle_lines(data, start):
            if raw.strip():
                rows.append(Receipt.from_dict(json.loads(raw)))
        if start:
            # A committed transaction may have rebased a transported revision
            # onto a local evidence successor. In that case the source fact_ref
            # is intentionally absent from the ledger even though its assertion
            # is durable. A later source child still names that original ref.
            # Never let an omission cursor turn that legal rebase into a
            # dangling revision: replay the full source graph so the ledger can
            # reconstruct the same transaction-local rewrite chain.
            available = set(known_fact_refs)
            dangling = False
            for row in rows:
                if row.supersedes and row.supersedes not in available:
                    dangling = True
                    break
                available.add(row.fact_ref)
            if dangling:
                start = 0
                count = 0
                latest = None
                rows = [Receipt.from_dict(json.loads(raw)) for raw in _bundle_lines(data) if raw.strip()]
                self.reused -= 1
                self.parsed += 1
        count += len(rows)
        times = [r.occurred_at for r in rows if r.kind == "usage"
                 and r.payload["mapping"] != "pi_bounded_native.v1" and r.occurred_at
                 and (r.payload.get("reported_total_tokens") is not None
                      or any(v is not None for v in r.payload["tokens"].values()))]
        if times:
            current = max(times,key=lambda t:parse_timestamp(t,"usage time"))
            if latest is None or parse_timestamp(current,"usage time") > parse_timestamp(latest,"usage time"):
                latest = current
        self._pending_receipt_bundles[slot] = (key, {
            "bytes":len(data),"sha256":sha256(data).hexdigest(),
            "count":count,"latest_usage_at":latest})
        return rows, count, latest

    def commit_receipt_bundles(self, ledger_watermark: int) -> None:
        """Advance omission cursors only after the ledger transaction commits."""
        if type(ledger_watermark) is not int or ledger_watermark < 0:
            raise ValueError("invalid committed ledger watermark")
        pending, self._pending_receipt_bundles = self._pending_receipt_bundles, {}
        for slot, (key, value) in pending.items():
            self.cache.put(slot, key, {**value, "ledger_watermark":ledger_watermark})

    def context(self, path: Path, session: str, dependencies: object, reader):
        slot = "context-"+digest([str(path),session])
        key = [engine_digest(), dependencies]
        cached = self.cache.get(slot,key)
        if isinstance(cached,dict) and set(cached)=={"context"}:
            return cached["context"]
        value = reader()
        self.cache.put(slot,key,{"context":value})
        return value

    def summary(self) -> dict[str, int]:
        return {"parsed_files":self.parsed, "reused_files":self.reused}
