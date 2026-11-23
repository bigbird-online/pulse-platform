"""The publisher's incremental views: fact index plus resolved store (WI754 step 3).

`IncrementalViews.attach` binds the existing derived state under the ledger
root to the ledger prefix a publication reads: it catches the fact index up
and refreshes the changed packages. It never builds either from scratch in a
publication cycle; a missing index or a store that must be rebuilt (new code,
new tenant registry, rebuilt index) is reported as unavailable, and
`pulse-compute-cost resolved rebuild` rebuilds it on the publisher.

`compare_views` is the shadow comparison: the views equal apart from
`report_sha256` and row order (see `_order_free`), or the first differing
field paths.
"""
from __future__ import annotations

from pathlib import Path
from time import perf_counter
from typing import Any, Mapping

from .calculation_cache import engine_digest
from .collaboration_query import CollaborationQuery, _query_snapshot
from .fact_index import FactIndex, IndexMismatch, stream_identity
from .incremental_modes import cache_mib as default_cache_mib
from .ledger import LedgerError, UsageLedger
from .receipts import canonical, digest
from .resolved_store import ResolvedStore, StoreMismatch, store_path

DIFF_PATHS = 5


class IncrementalUnavailable(LedgerError):
    def __init__(self, reason: str):
        super().__init__("incremental views unavailable")
        self.reason = reason


def derived_paths(ledger: UsageLedger, derived: Path | None = None) -> tuple[Path, Path]:
    """(fact index, resolved store): under `<ledger root>/derived`, or in `derived` (offline work)."""
    if derived is not None:
        return Path(derived) / "fact-index.sqlite", Path(derived) / "resolved.sqlite"
    root = Path(ledger.root)
    return root / "derived" / "fact-index.sqlite", store_path(root)


def open_store(ledger: UsageLedger, index: FactIndex, agent_tenants: Mapping[str, Any] | None,
               *, cache_mib: int = 256, derived: Path | None = None) -> ResolvedStore:
    from .agent_tenants import agent_tenants_digest
    return ResolvedStore.open(derived_paths(ledger, derived)[1], index_identity=index.identity(), engine=engine_digest(),
                              agent_tenants_sha256=agent_tenants_digest(agent_tenants), cache_mib=cache_mib)


class IncrementalViews:
    def __init__(self, index: FactIndex, store: ResolvedStore, timings: dict[str, Any]):
        """Pins the store's read snapshot: watermark, globals and every slice a query loads
        then come from one store state, whatever another connection refreshes meanwhile."""
        self.index, self.store, self.timings = index, store, timings
        store.begin_read()
        self._snapshot = store.snapshot(index)
        self._globals = store.global_values()
        self._report = store.report_values()

    @classmethod
    def attach(cls, ledger: UsageLedger, agent_tenants: Mapping[str, Any] | None, *,
               upto: int | None = None, cache_mib: int | None = None, derived: Path | None = None) -> "IncrementalViews":
        """Bring the existing index and store to the ledger prefix (to `upto`); never rebuild them."""
        started = perf_counter()
        cache_mib = cache_mib or default_cache_mib()
        index_path, resolved = derived_paths(ledger, derived)
        if not index_path.is_file() or not resolved.is_file():
            raise IncrementalUnavailable("derived_state_missing")
        index = FactIndex.open(index_path, stream_identity(ledger), cache_mib=cache_mib)
        if index.binding().sequence == 0:
            raise IncrementalUnavailable("fact_index_empty")
        try:
            applied = index.catch_up(ledger, upto=upto)
        except IndexMismatch as exc:
            raise IncrementalUnavailable("fact_index_mismatch") from exc
        if upto is not None and index.binding().sequence != upto:
            raise IncrementalUnavailable("fact_index_short")
        indexed = perf_counter()
        store = open_store(ledger, index, agent_tenants, cache_mib=cache_mib, derived=derived)
        if int(store.meta()["sequence"]) == 0:
            raise IncrementalUnavailable("resolved_store_rebuild_required")
        try:
            refreshed = store.refresh(index, agent_tenants)
        except StoreMismatch as exc:
            raise IncrementalUnavailable("resolved_store_rebuild_required") from exc
        timings = {"index_seconds": round(indexed - started, 3), "batches_applied": applied,
                   "refresh_seconds": round(perf_counter() - indexed, 3), **refreshed}
        return cls(index, store, timings)

    def snapshot(self) -> dict[str, Any]:
        """watermark, recorded_at and the head batch digest (as `facts_sha256`) of the bound prefix."""
        return dict(self._snapshot)

    def window_key(self, query: CollaborationQuery) -> dict[str, Any]:
        """What a view of this query reads: its window's slices, the public slice, all globals."""
        return self.store.digests(self.store.window_components(*query.window))

    def query(self, query: CollaborationQuery, *, catalog: Mapping[str, Any] | None,
              catalog_observed_at: str | None, price_cache: dict | None = None) -> dict[str, Any]:
        if query.watermark != self._snapshot["watermark"] or query.window is None:
            raise ValueError("incremental query escaped its watermark or window")
        prepared, history = self.store.load(*query.window, self._globals, self._report)
        return _query_snapshot({**self._snapshot, "facts": history}, query, prepared, catalog=catalog,
                               catalog_observed_at=catalog_observed_at, price_cache=price_cache,
                               canonical_order=True)

    def close(self) -> None:
        self.store.close()
        self.index.close()


_ABSENT = object()  # A missing key differs from a key holding null.


def _paths(left: Any, right: Any, path: str, found: list[str]) -> None:
    if len(found) >= DIFF_PATHS or (left is not _ABSENT and right is not _ABSENT and left == right):
        return
    if isinstance(left, dict) and isinstance(right, dict):
        for key in sorted(set(left) | set(right)):
            _paths(left.get(key, _ABSENT), right.get(key, _ABSENT), f"{path}/{key}", found)
    elif isinstance(left, list) and isinstance(right, list) and len(left) == len(right):
        for i, (a, b) in enumerate(zip(left, right)):
            _paths(a, b, f"{path}[{i}]", found)
    else:
        found.append(path or "/")


def _stable(rows: list) -> list:
    return sorted(rows, key=canonical)


def unpriced_witness(report: Mapping[str, Any]) -> dict[str, str]:
    """Per cost of a report (summary, each group by key): the digest of its complete unpriced list, stably sorted.

    A published view keeps two unpriced samples per reason and the digest of
    the list in row order; both depend on the engine's row order. This digest
    is taken before sampling and in a stable order: equal for the same facts
    in any order, different when any fact differs.
    """
    result = {"summary": digest(_stable(report["summary"]["cost"]["unpriced"]))}
    for group in report["groups"]:
        result["group:" + canonical(group["key"]).decode()] = digest(_stable(group["cost"]["unpriced"]))
    return result


def _order_free(view: Mapping[str, Any], witness: Mapping[str, str] | None) -> dict[str, Any]:
    """The view with each cost's unpriced list in a stable order.

    With the report's witness every listed cost carries the digest of its
    complete sorted list in place of the samples and row-order digest. Without
    one, an unsampled list is sorted and a sampled one stays as published.
    """
    def cost(value: Mapping[str, Any], location: str) -> dict[str, Any]:
        value = dict(value)
        if witness is not None and location in witness:
            value.pop("unpriced", None)
            value.pop("unpriced_sha256", None)
            value["unpriced_witness"] = witness[location]
        elif isinstance(value.get("unpriced"), list) and "unpriced_count" not in value:
            value["unpriced"] = _stable(value["unpriced"])
        return value

    result = {k: v for k, v in view.items() if k != "report_sha256"}
    if "summary" in result:
        result["summary"] = {**result["summary"], "cost": cost(result["summary"]["cost"], "summary")}
    for name in ("groups", "outcome_only_groups"):
        if name in result:
            result[name] = [{**g, "cost": cost(g["cost"], "group:" + canonical(g["key"]).decode())}
                            for g in result[name]]
    return result


def compare_views(full: Mapping[str, Any], incremental: Mapping[str, Any],
                  full_witness: Mapping[str, str] | None = None,
                  incremental_witness: Mapping[str, str] | None = None) -> list[str]:
    """Field paths (at most five) where two views differ, `report_sha256` and row order aside.

    Pass both reports' `unpriced_witness` to compare sampled unpriced lists by
    their complete content; without them a sampled list must match as published.
    """
    witnesses = (full_witness, incremental_witness) if full_witness is not None and incremental_witness is not None \
        else (None, None)
    found: list[str] = []
    _paths(_order_free(full, witnesses[0]), _order_free(incremental, witnesses[1]), "", found)
    return found


# -- rebuild and audit (publisher only, heavy) -----------------------------
def open_readonly(ledger: UsageLedger, agent_tenants: Mapping[str, Any] | None, *,
                  derived: Path | None = None) -> tuple[FactIndex, ResolvedStore]:
    """Index and store as they are, read-only; any binding that differs raises, nothing is reset."""
    import sqlite3
    from .agent_tenants import agent_tenants_digest
    from .fact_index import SCHEMA_VERSION as INDEX_SCHEMA
    from .resolved_store import SCHEMA_VERSION as STORE_SCHEMA
    from .session_packages import LINK_LOGIC
    index_path, resolved = derived_paths(ledger, derived)
    if not index_path.is_file() or not resolved.is_file():
        raise StoreMismatch("derived state is missing; run resolved rebuild first")

    def connect(path: Path) -> sqlite3.Connection:
        return sqlite3.connect(f"file:{path}?mode=ro", uri=True, isolation_level=None)

    def check(meta: Mapping[str, str], wanted: Mapping[str, str], what: str) -> None:
        if any(meta.get(k) != v for k, v in wanted.items()):
            raise StoreMismatch(f"{what} binds another ledger, code or registry; run resolved rebuild first")

    index_db = connect(index_path)
    meta = dict(index_db.execute("SELECT key, value FROM meta"))
    check(meta, {"schema_version": INDEX_SCHEMA, "stream_identity": stream_identity(ledger),
                 "link_logic": LINK_LOGIC}, "fact index")
    if "build_id" not in meta:
        raise StoreMismatch("fact index has no build identity; run resolved rebuild first")
    index = FactIndex(index_db, index_path)
    store_db = connect(resolved)
    check(dict(store_db.execute("SELECT key, value FROM meta")),
          {"schema_version": STORE_SCHEMA, "index_identity": index.identity(), "engine": engine_digest(),
           "agent_tenants_sha256": agent_tenants_digest(agent_tenants) or ""}, "resolved store")
    return index, ResolvedStore(store_db, resolved)

def rebuild(ledger: UsageLedger, agent_tenants: Mapping[str, Any] | None, *, derived: Path | None = None,
            watermark: int | None = None, cache_mib: int | None = None) -> dict[str, Any]:
    """Catch the fact index up (rebuilding it if it is not a ledger prefix), then recompute every package."""
    started = perf_counter()
    cache_mib = cache_mib or default_cache_mib()
    index = FactIndex.open(derived_paths(ledger, derived)[0], stream_identity(ledger), cache_mib=cache_mib)
    try:
        applied = index.catch_up(ledger, upto=watermark)
    except IndexMismatch:
        applied = index.rebuild(ledger, upto=watermark)
    if watermark is not None and index.binding().sequence != watermark:
        raise LedgerError("ledger is shorter than the watermark")
    indexed = perf_counter()
    store = open_store(ledger, index, agent_tenants, cache_mib=cache_mib, derived=derived)
    result = store.rebuild(index, agent_tenants)
    store.close()
    stats = index.stats()
    index.close()
    return {**result, "batches_applied": applied, "index": stats, "index_seconds": round(indexed - started, 3),
            "rebuild_seconds": round(perf_counter() - indexed, 3)}


def audit(ledger: UsageLedger, agent_tenants: Mapping[str, Any] | None, *, watermark: int,
          derived: Path | None = None, now=None, catalog: Mapping[str, Any] | None = None,
          tenant_identity: bool = False) -> dict[str, Any]:
    """Whole-ledger preparation at `watermark` against the store: every row, the globals, then the views.

    The store must have been refreshed to exactly this watermark (`rebuild --watermark`).
    Index and store are opened read-only: a binding that differs is refused, never cleared.
    """
    from datetime import datetime, timezone
    from .collaboration_query import CollaborationQuerySession, Prepared, _prepare_collaboration
    from .fact_index import PUBLIC
    from .locality_check import outcome
    from .package_slices import ROW_FIELDS
    from .receipts import canonical
    from .stats_dashboard import (ACTIVITY_DIMENSIONS, CURRENT_PERIODS, DISPLAY_ZONE, LAST_WEEK, _view,
                                  period_window)
    started = perf_counter()
    index, store = open_readonly(ledger, agent_tenants, derived=derived)
    if index.binding().sequence != watermark or int(store.meta()["sequence"]) != watermark:
        raise LedgerError("the resolved store is not at this watermark; run resolved rebuild --watermark first")
    snapshot = ledger.receipt_snapshot(watermark=watermark)
    if snapshot["watermark"] != watermark or snapshot["recorded_at"] != index.batch(watermark)[0]:
        raise LedgerError("ledger prefix differs from the indexed prefix")
    rows, full_globals = outcome(_prepare_collaboration(snapshot, agent_tenants))
    expected = {ref: canonical(record) for ref, record in rows}
    store_globals = store.global_values()
    row_diffs: list[dict[str, Any]] = []
    extra = 0
    for (component,) in store._db.execute("SELECT component FROM package_slice ORDER BY component").fetchall():
        piece = store.piece(component)
        prepared = Prepared(active=[], all_wis=set(), attribution_conflict_wis=set(),
                            observation_attribution_conflicts={}, accepted_at={},
                            **{k: piece[k] for k in ROW_FIELDS})
        produced, _ = outcome(prepared)
        for ref, record in produced:
            theirs = expected.pop(ref, None)
            if theirs is None:
                extra += 1
            if theirs != canonical(record) and len(row_diffs) < 100:
                row_diffs.append({"entity": ref, "package": component, "missing_in_full": theirs is None})
    missing = sorted(expected)[:100]
    mine = {"all_wis": sorted(store_globals["all_wis"]),
            "attribution_conflict_wis": sorted(store_globals["attribution_conflict_wis"]),
            "observation_attribution_conflicts": {k: sorted(v) for k, v in
                                                  store_globals["observation_attribution_conflicts"].items()},
            "accepted_at": dict(store_globals["accepted_at"])}
    global_diffs = sorted(k for k in full_globals if full_globals[k] != mine[k])
    del expected, rows
    now = now or datetime.now(timezone.utc)
    today = now.astimezone(DISPLAY_ZONE).date()
    session = CollaborationQuerySession(snapshot, agent_tenants=agent_tenants, canonical_order=True)
    views = IncrementalViews(index, store, {})
    compared = []
    for period in (*CURRENT_PERIODS, *LAST_WEEK):
        start, end = period_window(today, period)
        query = CollaborationQuery(from_date=start.isoformat(), to_date=end.isoformat(), timezone="Asia/Shanghai",
                                   scope="activity", watermark=watermark,
                                   group_by=(("tenant",) if tenant_identity else ()) + ACTIVITY_DIMENSIONS)
        full = _view(session.query(query, catalog=catalog, catalog_observed_at=None), period, "activity")
        incremental = _view(views.query(query, catalog=catalog, catalog_observed_at=None), period, "activity")
        compared.append({"period": period, "diff_paths": compare_views(full, incremental),
                         "reconcile": store.reconcile(incremental)[:DIFF_PATHS]})
    views.close()
    passed = (not row_diffs and not missing and not extra and not global_diffs
              and all(not c["diff_paths"] and not c["reconcile"] for c in compared))
    return {"watermark": watermark, "passed": passed, "row_diffs": row_diffs, "missing_rows": missing,
            "extra_rows": extra, "global_diffs": global_diffs, "views": compared,
            "public_component": PUBLIC, "seconds": round(perf_counter() - started, 3)}
