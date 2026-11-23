"""Incremental collection: judge only the session packages this round touches (WI754 step 4).

A round is the full collection (`receipt_sources._collect_once`) on a smaller
input. The judging code is unchanged; only the ledger snapshot is replaced by
the working set:

1. observe: this node's own sources become observation bundles in its local
   spool (`native_observe.observe`, the observe-only reading), except the
   core facts export, which stays a directly read source as in a full
   collection; other nodes' bundles stay in their spools;
2. dirty packages: those the spooled rows name (`session_packages.link_nodes`
   and the row's entity, looked up in the fact index), those a ledger batch
   after `judged_sequence` changed, packages a previous round deferred, and on
   the publisher the rejudge queue within its budget;
3. working set: the dirty packages' facts and the public package, in ledger
   order. Deferred packages first take up to half the fact limit (the oldest
   always); then a round reads spooled bundles while their packages fit the
   rest (one bundle at least; the rest wait as a backlog) and admits all of
   their packages; other dirty packages fill the limit, deferred ones first,
   then smallest first; the rest wait for the next round (`deferred_components`);
4. judge: `_collect_once(working=...)`;
5. settle: a decided row naming a package outside the working set grows the
   set and the round is judged again, at most `GROWTHS` times; packages still
   outside are `working_set_unsettled`. Rows naming a deferred or unsettled
   package are kept back and the spooled bundles stay for the next round;
6. ingest at the index's sequence (`SnapshotChanged`: catch up, start again,
   at most three times); `judged_sequence` then moves to that sequence, so
   the packages the round's own batch touched are judged once more next round
   (the tenant stage proves a usage version a round after it is ingested).

Rule files (`RULES`) are kept as per-entry digests in `rule_state`. A changed
entry that names a package queues that package; one that names none queues
every package with usage. Only the publisher keeps the queue
(`rejudge_queue`, `REJUDGE_BUDGET` facts per round); another node only
records the digests.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from heapq import merge
from pathlib import Path
from time import perf_counter
from typing import Any, Callable, Iterable, Mapping

from . import session_packages
from .fact_index import PUBLIC, FactIndex, IndexMismatch, index_path, stream_identity
from .ledger import SnapshotChanged, UsageLedger
from .events import utc_text
from .receipts import REF, Receipt, digest

# Working-set fact limits (development plan, section 9).
LIMIT_PEER = 400_000
LIMIT_PUBLISHER = 1_500_000
REJUDGE_BUDGET = 300_000
# Of the fact limit, the share deferred packages take before any bundle is read.
RESERVED_SHARE = 0.5
GROWTHS = 3
ATTEMPTS = 3
LOCAL_PEER = "local"
LOCAL_SOURCE_ID = "local-observations"
# Read directly as in a full collection: the core facts export (every node
# exports the one shared journal; a local copy is never a pushed one) and other
# nodes' spools.
DIRECT_KINDS = frozenset({"harness_core", "native_push"})
# rule_entries' holder entry naming every recorded session key's sessions; not a rule entry.
HOLDER_PLACES = "\0places"
RULES = ("reviewed_activity", "standing_non_wi", "tenant_registry", "principal_history",
         "retirements", "recoveries", "holder_writes")


class Grow(Exception):
    """The decided batch names packages outside the working set."""

    def __init__(self, components: set[int]):
        super().__init__("working set grows")
        self.components = components


def row_components(index: FactIndex, row: Receipt) -> set[int]:
    """The packages a row joins: those of its link nodes and of its entity (None: new)."""
    nodes = {*session_packages.link_nodes(row), session_packages.node(row.entity_ref)}
    found = {index.component_of(n) for n in nodes}
    found.discard(None)
    return found


@dataclass
class Working:
    """What `_collect_once` judges against instead of the ledger snapshot.

    `settle` sees the decided batch: rows naming packages outside the set
    raise `Grow` until `final`; then rows naming `held` (deferred) or
    unsettled packages are kept back, and with unsettled ones the bundles stay.
    `bundle_limits` bounds the bundles read per spool.
    """
    facts: list[Receipt]
    sequence: int
    index: FactIndex
    components: set[int] = field(default_factory=set)
    held: set[int] = field(default_factory=set)
    judge_only: bool = False
    final: bool = False
    commit_bundles: bool = True
    unsettled: set[int] = field(default_factory=set)
    batch: list[Receipt] | None = None
    bundle_limits: dict[str, int] | None = None

    def settle(self, batch: list[Receipt]) -> list[Receipt] | None:
        named = [(row, row_components(self.index, row) - {PUBLIC}) for row in batch]
        outside = set().union(*(c for _, c in named)) - self.components
        grow = outside - self.held
        if grow and not self.final:
            raise Grow(grow)
        self.unsettled = grow
        kept = self.held | grow
        if any(c & kept for _, c in named):
            batch = [row for row, c in named if not c & kept]
        if grow:
            self.commit_bundles = False  # Bundle rows may be among them: read again when they settle.
        self.batch = batch
        return None if self.judge_only else batch


def working_facts(index: FactIndex, components: Iterable[int]) -> list[Receipt]:
    """The packages' facts and the public package, in ledger order."""
    streams = [index.facts_of(PUBLIC, ids=True), *(index.facts_of(c, ids=True) for c in sorted(set(components)))]
    return [row for _, row in merge(*streams)]


def new_refs(index: FactIndex, batch: Iterable[Receipt]) -> set[str]:
    """The fact refs ingest would append for `batch` at the index's prefix (deduplicated)."""
    from .ingest_lookup import IndexLookup
    from .ledger import _decide_ingest
    index.begin_read()
    try:
        pending, _, _ = _decide_ingest(list(batch), IndexLookup(index))
    finally:
        index.end_read()
    return {row.fact_ref for row in pending}


def table_new_refs(facts: list[Receipt], batch: Iterable[Receipt]) -> set[str]:
    """`new_refs` against a whole snapshot (the full side of a comparison)."""
    from .ingest_lookup import TableLookup
    from .ledger import _decide_ingest
    pending, _, _ = _decide_ingest(list(batch), TableLookup([facts]))
    return {row.fact_ref for row in pending}


# -- rule files -----------------------------------------------------------------
def _loaded(load: Callable[[], Any]) -> Any:
    try:
        return load()
    except (OSError, ValueError):
        return None


def rule_entries(registry: Mapping[str, Any], retirements: Mapping[str, Any],
                 recoveries: Mapping[str, Any], facts: Iterable[Receipt] = ()) -> dict[str, dict[str, tuple[str, list[str]]]]:
    """rule -> entry key -> (digest, refs that place it in packages); key '*' is a whole file.

    `facts` are the public facts, where recorded holder session candidates
    name the native sessions a core session write can attribute.
    """
    from .activity_classification import load_confirmations
    from .standing_non_wi import load_rules
    from .tenant_context import load_tenant_document
    result: dict[str, dict[str, tuple[str, list[str]]]] = {}
    reviewed = _loaded(load_confirmations)
    entries: dict[str, tuple[str, list[str]]] = {}
    if isinstance(reviewed, Mapping):
        for item in reviewed.get("items") or []:
            target = item.get("target_fact_ref") if isinstance(item, Mapping) else None
            entries[str(target) if target else digest(item)] = (digest(item), [target] if target else [])
        for item in reviewed.get("subcalls") or []:
            targets = item.get("target_fact_refs") if isinstance(item, Mapping) else None
            targets = [t for t in targets if isinstance(t, str)] if isinstance(targets, list) else []
            entries[digest(["subcall", item])] = (digest(item), targets)
    else:
        entries["*"] = (digest(reviewed), [])
    result["reviewed_activity"] = entries
    result["standing_non_wi"] = {"*": (digest(_loaded(load_rules)), [])}
    path = registry.get("tenant_registry_path")
    tenant = _loaded(lambda: load_tenant_document(Path(path))) if isinstance(path, str) else None
    result["tenant_registry"] = {"*": (digest([path, tenant]), [])}
    result["principal_history"] = {"*": (digest(sorted(
        ([s.get("id"), s.get("principal_alias_history")] for s in registry.get("sources", [])
         if isinstance(s, Mapping) and s.get("principal_alias_history")), key=str)), [])}
    # Retirements and recoveries decide coverage facts: the public package, judged every round.
    result["retirements"] = {k: (digest(v), [PUBLIC]) for k, v in retirements.items()}
    result["recoveries"] = {k: (digest(v), [PUBLIC]) for k, v in recoveries.items()}
    result["holder_writes"] = _holder_entries(registry, facts)
    return result


def _holder_entries(registry: Mapping[str, Any], facts: Iterable[Receipt]) -> dict[str, tuple[str, list[str]]]:
    """Per core session key: its writes and the creation of their WIs, placed by the sessions recorded with it."""
    holder = registry.get("core_session_writes")
    if holder is None or "holder_supplement.v1" not in registry.get("required_receipt_features", []):
        return {}
    from .holder_supplement import read_creations, read_session_writes
    from .request_inputs import HOLDER_SESSION, recorded
    try:
        _, writes, _ = read_session_writes(Path(holder["path"]))
        created = read_creations(Path(holder["path"]))
    except (OSError, ValueError, TypeError, KeyError):
        return {"*": ("unreadable", [])}
    scopes: dict[str, list[str]] = {}
    for scope, value in recorded(facts, HOLDER_SESSION).items():
        for key in value.get("keys", []):
            scopes.setdefault(key, []).append(scope)
    result: dict[str, Any] = {HOLDER_PLACES: ("", scopes)}
    for key, items in writes.items():
        wis = sorted({wi for _, wi in items})
        value = [[utc_text(at), wi] for at, wi in items] + [[wi, utc_text(created[wi]) if wi in created else None]
                                                           for wi in wis]
        # A key no recorded session names attributes nothing: it places in no package.
        result[key] = (digest(value), scopes.get(key, []))
    return result


def _place(index: FactIndex, ref: Any) -> int | None:
    if ref == PUBLIC:
        return PUBLIC
    return index.component_of(ref) if isinstance(ref, str) and REF.fullmatch(ref) else None


def rule_changes(index: FactIndex, entries: Mapping[str, Mapping[str, tuple[str, list[str]]]]
                 ) -> tuple[set[int], list[str]]:
    """(packages changed entries name, rules with a changed entry that cannot be placed); records nothing.

    A removed entry is placed by its key when the key is a fact ref (a
    reviewed target); a removed core session key by the sessions recorded
    with it (`HOLDER_PLACES`); anything else cannot be placed.
    """
    components: set[int] = set()
    unplaced: list[str] = []
    for rule, current in entries.items():
        stored = index.rule_state(rule)
        places = current.get(HOLDER_PLACES, ("", []))[1] if rule == "holder_writes" else {}
        for key in (set(stored) | set(current)) - {HOLDER_PLACES}:
            if stored.get(key) == (current[key][0] if key in current else None):
                continue
            if key in current:
                refs, placeable = current[key][1], key != "*"
            else:
                refs = places.get(key, []) if rule == "holder_writes" else [key]
                placeable = rule == "holder_writes" or bool(REF.fullmatch(key))
            if not placeable:
                unplaced.append(rule)
                continue
            components |= {c for c in (_place(index, ref) for ref in refs) if c is not None}
    return components - {PUBLIC}, sorted(set(unplaced))


def record_rules(index: FactIndex, entries: Mapping[str, Mapping[str, tuple[str, list[str]]]]) -> None:
    for rule, current in entries.items():
        index.set_rule_state(rule, {k: v[0] for k, v in current.items() if k != HOLDER_PLACES})


# -- observation ----------------------------------------------------------------
def observed_sources(registry: Mapping[str, Any], retirements: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    """This node's own sources that its local spool carries."""
    return [s for s in registry["sources"] if isinstance(s, Mapping) and s.get("kind") not in DIRECT_KINDS
            and s.get("id") not in retirements]


def judging_registry(registry: Mapping[str, Any], spool: Path, retirements: Mapping[str, Any], *,
                     peer: str = LOCAL_PEER) -> dict[str, Any]:
    """The registry a round judges: own sources replaced by the spool of `peer`'s bundles
    (the local spool's own observations, or a trial's copies of what this node pushed)."""
    own = observed_sources(registry, retirements)
    kept = [s for s in registry["sources"] if not any(s is o for o in own)]
    if own:
        kept.append({"id": LOCAL_SOURCE_ID, "kind": "native_push", "peer": peer, "path": str(spool),
                     "source_ids": [s["id"] for s in own]})
    return {**registry, "sources": kept}


def observe_into_spool(registry: Mapping[str, Any], *, spool: Path, state: Path, read_at: str,
                       retirements: Mapping[str, Any], force_recompute: bool = False,
                       deadline: float | None = None, keep: Callable[[Path], None] | None = None) -> int:
    """Observe own sources (`native_observe.observe`) into the local spool; the bundles written.

    Each bundle is spooled and acknowledged as soon as it is written, so the
    observe index only moves for bundles the spool holds. `keep` gets each
    bundle first (the trial's copy).
    """
    from . import native_observe
    from .native_observations import store
    own = observed_sources(registry, retirements)
    if not own:
        return 0
    state.mkdir(mode=0o700, parents=True, exist_ok=True)
    spool.mkdir(mode=0o700, parents=True, exist_ok=True)
    index, outbox = state / "index.json", state / "outbox"
    for stale in native_observe.pending_bundles(outbox):
        native_observe.discard(stale)

    def deliver(bundle: Path) -> None:
        if keep is not None:
            keep(bundle)
        store(spool, LOCAL_PEER, bundle.read_bytes())
        native_observe.acknowledge(index, bundle)
    try:
        bundles = native_observe.observe({**registry, "sources": list(own)}, peer=LOCAL_PEER, index_path=index,
                                         outbox=outbox, read_at=read_at, force_recompute=force_recompute,
                                         deadline=deadline, deliver=deliver)
    finally:
        for rest in native_observe.pending_bundles(outbox):
            native_observe.discard(rest)
    return len(bundles)


def spooled_components(index: FactIndex, registry: Mapping[str, Any], retirements: Mapping[str, Any],
                       limit: int) -> tuple[set[int], dict[str, int]]:
    """(packages the spooled rows this round reads name, bundles to read per spool;
    a spool the limit cut reads fewer than it holds).

    Bundles are taken in spool order while their packages and the public
    package fit `limit` facts (the first bundle of a spool always); the rest
    wait for the next round as an observation backlog. A bundle is judged
    whole, so its rows are never split across rounds.
    """
    from .native_observations import expand_pushed
    found: set[int] = set()
    used = [index.package_size(PUBLIC)]
    taken: dict[str, int] = {}

    def visit(source_id: str, document: Mapping[str, Any]) -> bool:
        rows: list[Receipt] = list(document["inputs"])
        for entry in document["sources"]:
            rows += [*entry["rows"], *entry["inventories"], *entry["appended"]]
        named = set().union(*(row_components(index, row) for row in rows)) - {PUBLIC} - found
        size = sum(index.package_size(c) for c in named)
        if source_id in taken and used[0] + size > limit:
            return False
        found.update(named)
        used[0] += size
        taken[source_id] = taken.get(source_id, 0) + 1
        return True
    expand_pushed(registry["sources"], retirements, visit=visit)
    return found, taken


# -- one round --------------------------------------------------------------------
@dataclass
class Plan:
    admitted: list[int]
    deferred: list[int]
    queued: list[int]
    sizes: dict[str, int]


def reserved(index: FactIndex, limit: int) -> list[int]:
    """The deferred packages this round takes before any bundle, oldest first, within
    RESERVED_SHARE of `limit` (the oldest always), so busy spools never starve them."""
    chosen, used = [], 0
    for c in dict.fromkeys(index.deferred()):
        size = index.package_size(c)
        if not size:
            continue  # Absorbed or gone; its successor is changed since and judged as such.
        if chosen and used + size > limit * RESERVED_SHARE:
            break
        chosen.append(c)
        used += size
    return chosen


def plan(index: FactIndex, spooled: set[int], *, limit: int, queued: list[int], first: list[int]) -> Plan:
    """The `first` deferred packages (`reserved`) and the packages of the spooled rows
    this round reads (`spooled_components` sized them within the rest of the limit),
    then up to `limit` facts (public package included): the other deferred ones
    oldest first, then changed and queued ones, each group smallest first.
    Deferred packages keep their turn, so every waiting package is judged in some round."""
    carried = [c for c in index.deferred() if index.package_size(c)]
    changed = set(index.components_changed_since(index.judged_sequence()))
    admitted = list(dict.fromkeys([*first, *sorted(spooled - {PUBLIC})]))
    used = index.package_size(PUBLIC) + sum(index.package_size(c) for c in admitted)
    seen = set(admitted) | set(carried)
    order = [c for c in dict.fromkeys(carried) if c not in admitted]
    for group in (changed, set(queued)):
        for c in sorted(group - seen - {PUBLIC}, key=lambda c: (index.package_size(c), c)):
            order.append(c)
            seen.add(c)
    deferred = []
    for c in order:
        size = index.package_size(c)
        if admitted and used + size > limit:
            deferred.append(c)
            continue
        admitted.append(c)
        used += size
    return Plan(admitted, deferred, [c for c in queued if c in admitted],
                {"spooled": len(spooled), "changed": len(changed), "carried": len(carried),
                 "reserved": len(first), "queued": len(queued), "facts": used})


def judge(registry: Mapping[str, Any], ledger: UsageLedger, index: FactIndex, components: Iterable[int],
          bundles: Mapping[str, int], *,
          held: set[int], judge_only: bool, retirements, recoveries, timings: dict[str, Any] | None,
          now: str | None, force_recompute: bool = False) -> tuple[dict[str, Any], Working, int]:
    """`_collect_once` on the working set, grown until its batch stays inside (at most GROWTHS times);
    the result, the last working set and how often it grew."""
    from .receipt_sources import _collect_once
    components = set(components)
    for growth in range(GROWTHS + 1):
        sequence = index.binding().sequence
        working = Working(working_facts(index, components), sequence, index, components, set(held),
                          judge_only=judge_only, final=growth == GROWTHS, bundle_limits=dict(bundles))
        try:
            result = _collect_once(registry, ledger, force_recompute=force_recompute, timings=timings,
                                   retirements=retirements, recoveries=recoveries, working=working, now=now)
        except Grow as grown:
            components |= grown.components
            continue
        return result, working, growth
    raise AssertionError("bounded growth loop exhausted")


def _representative(index: FactIndex, component: int) -> bytes | None:
    found = index._db.execute("SELECT entity_ref FROM fact INDEXED BY fact_component WHERE component = ? LIMIT 1",
                              (component,)).fetchone()
    return found[0] if found else None


def collect_incremental(registry: Mapping[str, Any], ledger: UsageLedger, index: FactIndex, *,
                        publisher: bool, limit: int, judge_only: bool = False,
                        retirements: Mapping[str, Any] | None = None, recoveries: Mapping[str, Any] | None = None,
                        timings: dict[str, Any] | None = None, now: str | None = None,
                        force_recompute: bool = False,
                        rules_registry: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """One incremental round over the sources of `registry` (local ones already spooled).

    `rules_registry` is the node's own registry when `registry` replaced its
    sources by a spool (`judging_registry`): rule digests (principal alias
    histories, the tenant registry, core session writes) come from it.

    `judge_only` decides without ingesting or consuming anything and records
    the round as judged at the index's prefix; the decided batch and its new
    fact refs are in the result (`batch`, `new_refs`).
    """
    from .coverage_recovery import load_recoveries
    from .source_retirement import load_retirements
    retirements = load_retirements() if retirements is None else retirements
    recoveries = load_recoveries() if recoveries is None else recoveries
    if ledger._index is None:
        ledger._index = index  # Ingest point-reads this index and appends its batch to it.
    sink = timings if timings is not None else {}
    for attempt in range(1, ATTEMPTS + 1):
        sink["attempts"] = attempt
        started = perf_counter()
        try:
            index.catch_up(ledger)
        except IndexMismatch:
            index.rebuild(ledger)
        sequence = index.binding().sequence
        entries = rule_entries(rules_registry if rules_registry is not None else registry, retirements,
                               recoveries, index.public_facts())
        if not index.judgement_started():
            # Until now a full collection judged every batch, under the current rules.
            index.record_judgement(sequence, [])
            record_rules(index, entries)
        changed, unplaced = rule_changes(index, entries)
        if publisher:
            if unplaced:
                changed |= set(index.usage_packages())
            index.enqueue(changed, "rule_changed", sequence)
        queued = index.queued(REJUDGE_BUDGET) if publisher else []
        first = reserved(index, limit)
        spooled, bundles = spooled_components(index, registry, retirements,
                                              limit - sum(index.package_size(c) for c in first))
        chosen = plan(index, spooled, limit=limit, queued=queued, first=first)
        sink["incremental"] = {"sequence": sequence, "judged_sequence": index.judged_sequence(), **chosen.sizes,
                               "bundles": dict(sorted(bundles.items())),
                               "admitted": len(chosen.admitted), "deferred_components": len(chosen.deferred),
                               "rules_changed": unplaced, "plan_seconds": round(perf_counter() - started, 3)}
        try:
            result, working, growths = judge(
                registry, ledger, index, chosen.admitted, bundles, held=set(chosen.deferred),
                judge_only=judge_only, retirements=retirements, recoveries=recoveries, timings=timings,
                now=now, force_recompute=force_recompute)
        except SnapshotChanged:
            if attempt == ATTEMPTS:
                raise
            continue
        sink["incremental"]["growths"] = growths
        if working.unsettled:
            sink["incremental"]["working_set_unsettled"] = len(working.unsettled)
        waiting = chosen.deferred + ([] if publisher else sorted(working.unsettled))
        representatives = [r for r in (_representative(index, c) for c in waiting) if r is not None]
        unsettled = [r for r in (_representative(index, c) for c in working.unsettled) if r is not None]
        if judge_only:
            result = {**result, "batch": working.batch, "new_refs": sorted(new_refs(index, working.batch or []))}
        else:
            index.catch_up(ledger, upto=result["watermark"])
        # Judged through the prefix it read: the packages its own batch touched are
        # judged again next round, where the tenant stage proves this round's new usage versions.
        judged = sequence
        # Package ids may have merged in the ingested batch: carry them by a member entity.
        carried = [c for c in (index.component_of(r) for r in representatives) if c is not None]
        index.record_judgement(judged, carried)
        if publisher:
            index.dequeue(chosen.queued)
            index.enqueue({index.component_of(r) for r in unsettled} - {None}, "working_set_unsettled", judged)
        record_rules(index, entries)
        sink["incremental"].update(judged_sequence=judged, queue=index.queue_length())
        return result
    raise AssertionError("bounded collection loop exhausted")


def open_index(ledger: UsageLedger, *, cache_mib: int, path: Path | None = None) -> FactIndex:
    """This ledger's fact index (`<ledger root>/derived/fact-index.sqlite`), built when empty."""
    index = FactIndex.open(path or index_path(ledger), stream_identity(ledger), cache_mib=cache_mib)
    try:
        if index.binding().sequence == 0:
            index.catch_up(ledger)
    except BaseException:
        index.close()
        raise
    return index
