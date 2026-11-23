"""Per-session-package preparation and its merge back into one (WI754 step 3).

`_prepare_collaboration` runs unchanged on one package's facts (or the public
package's). A slice keeps what `_query_snapshot` reads, minus `active`, plus:

* `globals`: the values a package contributes to every window: WIs seen,
  WIs in attribution conflicts, observation conflict candidates, first
  acceptance per WI and the stage membership diagnostics;
* `history`: its stage declarations (`stage_report` reads their revisions);
* `bounds`: the instants its rows may occupy, to select it for a window.

The row fields split in two. Window fields (`WINDOW_FIELDS`: rows, their
contexts and diagnostics) are read for the window's packages only. Report
fields (`REPORT_FIELDS`: revision conflicts, attribution and actor conflicts,
actor evidence, the reconciliation diagnostics) appear in every report over
the whole ledger, so they are merged from every package, like the globals.

`merge_slices` concatenates the window's slices, takes the report fields of
every package and unions every package's globals into one `Prepared`;
`_query_snapshot(canonical_order=True)` then reads it like a whole-ledger
preparation and gives the same report, `report_sha256` included, as the
whole ledger read in canonical order. A slice encodes to JSON that keeps
facts byte-exact and sets and tuples typed, so a stored slice decodes to the
same values it was computed as.
"""
from __future__ import annotations

from datetime import datetime, timezone
import json
from typing import Any, Iterable, Mapping
import zlib

from .collaboration_query import Prepared, _prepare_collaboration
from .events import parse_timestamp
from .receipts import Receipt
from .session_packages import PUBLIC_KINDS
from .stage_facts import SEMANTIC_KINDS

WINDOW_FIELDS = ("observation_groups", "non_wi_kinds", "projected", "all_reconciled", "actor_context",
                 "diagnostic_by_fact", "diagnostic_context", "task_evidence", "attribution_sources")
REPORT_FIELDS = ("conflicts", "attr_conflicts", "actor_conflicts", "actor_evidence", "reconciliation")
ROW_FIELDS = WINDOW_FIELDS + REPORT_FIELDS
GLOBAL_FIELDS = ("all_wis", "attribution_conflict_wis", "observation_attribution_conflicts", "accepted_at")
COUNTER_GRANULARITIES = frozenset({"turn_aggregate", "session_counter"})
_RECEIPT, _SET, _TUPLE = "\x00r", "\x00s", "\x00t"


# -- encoding ----------------------------------------------------------------
def _encode(value: Any) -> Any:
    if isinstance(value, Receipt):
        return {_RECEIPT: [value.kind, value.entity_ref, value.fact_ref, value.occurred_at, value.observed_at,
                           value.producer, list(value.evidence), value.supersedes,
                           value._payload_json.decode()]}
    if isinstance(value, dict):
        if not all(isinstance(k, str) for k in value):
            raise TypeError("slice maps are keyed by strings")
        return {k: _encode(v) for k, v in value.items()}
    if isinstance(value, (set, frozenset)):
        return {_SET: sorted((_encode(v) for v in value), key=lambda v: json.dumps(v, sort_keys=True))}
    if isinstance(value, tuple):
        return {_TUPLE: [_encode(v) for v in value]}
    if isinstance(value, list):
        return [_encode(v) for v in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    raise TypeError(f"unsupported slice value: {type(value).__name__}")


def _decode_object(value: dict) -> Any:
    if len(value) == 1:
        if _RECEIPT in value:
            kind, entity, fact, occurred, observed, producer, evidence, supersedes, payload = value[_RECEIPT]
            return Receipt(kind, entity, fact, occurred, observed, producer, tuple(evidence), supersedes,
                           payload.encode())
        if _SET in value:
            return set(value[_SET])
        if _TUPLE in value:
            return tuple(value[_TUPLE])
    return value


def encode_slice(piece: Mapping[str, Any]) -> bytes:
    """Compressed JSON of a slice, deterministic for equal slices."""
    return zlib.compress(json.dumps(_encode(dict(piece)), ensure_ascii=False, separators=(",", ":"))
                         .encode(), 6)


def decode_slice(data: bytes) -> dict[str, Any]:
    return json.loads(zlib.decompress(data), object_hook=_decode_object)


# -- time bounds -----------------------------------------------------------
def instant_text(value: str | datetime) -> str:
    """A fixed-width UTC text, so SQLite compares instants as text."""
    moment = parse_timestamp(value, "time") if isinstance(value, str) else value
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def bounds(rows: Iterable[Receipt]) -> tuple[str | None, str | None, bool]:
    """(earliest, latest, open_ended) over every instant these rows name.

    Earliest is None when a counter without a start bounds usage only from
    above. Open-ended packages hold a public-kind fact (coverage, deliveries,
    stage declarations: windows read them whatever their time), a usage or
    execution without any time, or no time at all; every window loads them.
    """
    low: str | None = None
    high: str | None = None
    unbounded_low = open_ended = False
    seen = False
    for row in rows:
        if row.kind in PUBLIC_KINDS:
            open_ended = True
        p = row.payload
        times = [row.occurred_at, p.get("interval_start"), p.get("interval_end")]
        metering = p.get("metering") if row.kind == "usage" else None
        if isinstance(metering, Mapping):
            times += [point.get("at") for point in metering.get("points", ()) if isinstance(point, Mapping)]
        times = [instant_text(t) for t in times if isinstance(t, str)]
        if row.kind in {"usage", "execution"} and not times:
            open_ended = True
        if (row.kind == "usage" and p.get("interval_start") is None
                and p.get("granularity") in COUNTER_GRANULARITIES):
            unbounded_low = True
        for t in times:
            seen = True
            low = t if low is None or t < low else low
            high = t if high is None or t > high else high
    if not seen:
        open_ended = True
    return (None if unbounded_low else low), high, open_ended


# -- slices ------------------------------------------------------------------
def prepare_package(facts: list[Receipt], agent_tenants: Mapping[str, Any] | None = None, *,
                    shared: list[Receipt] = ()) -> dict[str, Any]:
    """One session package's slice: its preparation, global contributions, history and bounds.

    `facts` are the package's own facts in ledger order with the shared facts
    (`session_packages.is_shared`) merged in at their positions, as
    `locality_check.package_facts` gives them; `shared` names those shared
    facts. They are judged here but belong to the public slice, so their own
    rows are left out of this one.
    """
    prepared = _prepare_collaboration({"facts": facts}, agent_tenants)
    entities = {r.entity_ref for r in shared}
    if not entities:
        return slice_of(prepared, facts)
    return slice_of(_without(Prepared(*prepared), entities), [r for r in facts if r.entity_ref not in entities])


def _without(prepared: Prepared, entities: set[str]) -> Prepared:
    """The preparation minus the rows of these entities (the shared facts judged beside a package)."""
    def keep(row: Receipt) -> bool:
        return row.entity_ref not in entities

    def keys(mapping: dict) -> dict:
        return {k: v for k, v in mapping.items() if k not in entities}

    evidence = {}
    for target, claims in prepared.task_evidence.items():
        kept = [c for c in claims if keep(c)]
        if kept:
            evidence[target] = kept
    return prepared._replace(
        projected=[r for r in prepared.projected if keep(r)],
        all_reconciled=[r for r in prepared.all_reconciled if keep(r)],
        conflicts=keys(prepared.conflicts), observation_groups=keys(prepared.observation_groups),
        non_wi_kinds=keys(prepared.non_wi_kinds), actor_context=keys(prepared.actor_context),
        actor_evidence=keys(prepared.actor_evidence), actor_conflicts=keys(prepared.actor_conflicts),
        diagnostic_context=keys(prepared.diagnostic_context),
        diagnostic_by_fact={k: v for k, v in prepared.diagnostic_by_fact.items() if keep(v)},
        task_evidence=evidence)


def prepare_public(facts: list[Receipt], agent_tenants: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """The public package's slice; every window reads it."""
    piece = prepare_package(facts, agent_tenants)
    piece["bounds"] = (piece["bounds"][0], piece["bounds"][1], True)
    return piece


def slice_of(prepared: Prepared, facts: list[Receipt]) -> dict[str, Any]:
    prepared = Prepared(*prepared)
    piece: dict[str, Any] = {name: getattr(prepared, name) for name in ROW_FIELDS}
    piece["globals"] = {name: getattr(prepared, name) for name in GLOBAL_FIELDS}
    piece["globals"]["stage_membership"] = prepared.reconciliation["stage_membership"]
    piece["history"] = [r for r in facts if r.kind in SEMANTIC_KINDS]
    rows = [*facts, *prepared.projected, *prepared.all_reconciled,
            *(r for versions in prepared.conflicts.values() for r in versions)]
    piece["bounds"] = bounds(rows)
    return piece


def _merge(left: Any, right: Any) -> Any:
    """Union of two packages' diagnostics; disjoint keys keep their own values."""
    if isinstance(left, dict) and isinstance(right, dict):
        result = dict(left)
        for key, value in right.items():
            result[key] = _merge(result[key], value) if key in result else value
        return result
    if isinstance(left, set) and isinstance(right, set):
        return left | right
    if isinstance(left, list) and isinstance(right, list):
        if all(isinstance(v, str) for v in (*left, *right)):
            return sorted(set(left) | set(right))
        return left + right
    if left == "conflict" or right == "conflict":
        return "conflict"
    return left


def merge_globals(pieces: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    total: dict[str, Any] = {"all_wis": set(), "attribution_conflict_wis": set(),
                             "observation_attribution_conflicts": {}, "accepted_at": {}, "stage_membership": None}
    for piece in pieces:
        part = piece["globals"] if "globals" in piece else piece
        total["all_wis"] |= set(part["all_wis"])
        total["attribution_conflict_wis"] |= set(part["attribution_conflict_wis"])
        conflicts = total["observation_attribution_conflicts"]
        for ref, values in part["observation_attribution_conflicts"].items():
            conflicts[ref] = (conflicts[ref] | set(values)) if ref in conflicts else set(values)
        accepted = total["accepted_at"]
        for wi, at in part["accepted_at"].items():
            if wi not in accepted or parse_timestamp(at, "accepted_at") < parse_timestamp(accepted[wi], "accepted_at"):
                accepted[wi] = at
        membership = part["stage_membership"]
        total["stage_membership"] = (membership if total["stage_membership"] is None
                                     else _merge(total["stage_membership"], membership))
    if total["stage_membership"] is None:
        total["stage_membership"] = _prepare_collaboration({"facts": []}).reconciliation["stage_membership"]
    for key in ("candidates", "contexts", "conflicts"):
        total["stage_membership"][key] = dict(sorted(total["stage_membership"].get(key, {}).items()))
    return total


def merge_report(pieces: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    """The report fields of every package, merged."""
    total: dict[str, Any] = {"conflicts": {}, "attr_conflicts": set(), "actor_conflicts": {}, "actor_evidence": {},
                             "reconciliation": None}
    for piece in pieces:
        part = piece["report"] if "report" in piece else piece
        total["conflicts"].update(part["conflicts"])
        total["attr_conflicts"] |= set(part["attr_conflicts"])
        total["actor_conflicts"].update(part["actor_conflicts"])
        total["actor_evidence"].update(part["actor_evidence"])
        total["reconciliation"] = (part["reconciliation"] if total["reconciliation"] is None
                                   else _merge(total["reconciliation"], part["reconciliation"]))
    if total["reconciliation"] is None:
        total["reconciliation"] = _prepare_collaboration({"facts": []}).reconciliation
    total["conflicts"] = dict(sorted(total["conflicts"].items()))
    return total


def merge_slices(pieces: Iterable[Mapping[str, Any]], global_values: Mapping[str, Any],
                 report: Mapping[str, Any] | None = None) -> tuple[Prepared, list]:
    """One preparation from the window's slices, every package's report fields and globals; and the stage history.

    Without `report` the pieces are taken to be every package and give the report fields too.
    """
    pieces = list(pieces)
    rows: dict[str, Any] = {"observation_groups": {}, "non_wi_kinds": {}, "projected": [], "all_reconciled": [],
                            "actor_context": {}, "diagnostic_by_fact": {}, "diagnostic_context": {},
                            "task_evidence": {}, "attribution_sources": {"sources": {}, "jobs": {}}}
    history: list[Receipt] = []
    for piece in pieces:
        for name in ("projected", "all_reconciled"):
            rows[name].extend(piece[name])
        for name in ("observation_groups", "non_wi_kinds", "actor_context", "diagnostic_by_fact",
                     "diagnostic_context"):
            rows[name].update(piece[name])
        for target, claims in piece["task_evidence"].items():
            rows["task_evidence"].setdefault(target, []).extend(claims)
        for name in ("sources", "jobs"):
            rows["attribution_sources"][name].update(piece["attribution_sources"][name])
        history.extend(piece["history"])
    report = dict(report if report is not None else merge_report(pieces))
    report["reconciliation"] = {**report["reconciliation"], "stage_membership": global_values["stage_membership"]}
    prepared = Prepared(active=[], all_wis=set(global_values["all_wis"]),
                        attribution_conflict_wis=set(global_values["attribution_conflict_wis"]),
                        observation_attribution_conflicts={k: set(v) for k, v in
                                                           global_values["observation_attribution_conflicts"].items()},
                        accepted_at=dict(global_values["accepted_at"]),
                        conflicts=dict(report["conflicts"]), attr_conflicts=set(report["attr_conflicts"]),
                        actor_conflicts=dict(report["actor_conflicts"]), actor_evidence=dict(report["actor_evidence"]),
                        reconciliation=report["reconciliation"], **rows)
    return prepared, history
