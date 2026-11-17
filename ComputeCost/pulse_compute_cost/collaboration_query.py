"""Natural-grain WI/period/Agent queries over the existing ledger's receipts.

Usage, deliveries, reviews and benefits are aggregated BEFORE joining. No query
manufactures provider coverage, a quality score, human hours or a value split.
"""
from __future__ import annotations

from collections import Counter, defaultdict, OrderedDict
from dataclasses import dataclass, asdict, replace
from datetime import datetime, date, time, timedelta
from decimal import Decimal
from typing import Any, Mapping, Iterable
from zoneinfo import ZoneInfo

from .events import parse_timestamp, utc_text
from .ledger import UsageLedger
from .runtime_samples import CC_SYNTHETIC_MODEL
from .native_inventory import reconcile_host_inventory
from .receipts import (Receipt, ReceiptError, TOKEN_FIELDS, ROLES, POLICY, SCHEDULED_JOB_POLICY, EXPLORATION_POLICY,
                       HOST_FALLBACK, supplement_source,
                       current_receipts, decimal, decimal_text, digest, canonical, reference,
                       is_source_retirement)
from .wi_pricing import _model, _or_model_id, WiPricingError
from .actor_fold import actor_fold_enabled, fold_actor_heads

UNKNOWN = "unassigned"
CC_RESULT_MAPPINGS = frozenset({"cc_result.v1", "cc_sdk_result.v1"})
BANDS = {"micro": Decimal("0.25"), "bounded": Decimal(1), "judgment": Decimal(2), "framing": Decimal(4)}
# Producers of an Agent's own purpose declaration. Their `none` says that no
# WI was declared; unlike a reviewed classification or a cron label, it is no
# counter-evidence to a holder supplement or stage membership naming a WI.
PURPOSE_DECLARATION_PRODUCERS = frozenset({
    "pi_native_request_purpose.v1", "codex_request_purpose.v1",
    "codex_native_root_request.v1", "session_wi_declaration.v1"})
# Cost reasons that only say completeness is unproven. No current source emits
# coverage or execution-inventory receipts, and native usage carries no
# execution reference, so these hold for almost every group without any data
# defect. Every other reason, including codes added later, is an anomaly.
PROOF_GAP_REASONS = frozenset({
    "source_coverage_unknown", "wi_execution_inventory_unknown", "execution_link_unknown",
    "context_inventory_unknown", "provisional_usage", "no_usage_observations",
    "execution_in_progress"})


def classify_cost_reasons(reasons: Iterable[str]) -> dict[str, list[str]]:
    """Split cost reasons for presentation; amounts and `reasons` stay unchanged."""
    codes = set(reasons)
    return {"anomalies": sorted(codes - PROOF_GAP_REASONS),
            "proof_gaps": sorted(codes & PROOF_GAP_REASONS)}

@dataclass(frozen=True)
class CollaborationQuery:
    from_date: str | None = None
    to_date: str | None = None
    timezone: str = "Asia/Shanghai"
    work_items: tuple[str, ...] = ()
    agent: str | None = None
    agent_role: str = "executor"
    runtime: str | None = None
    model: str | None = None
    group_by: tuple[str, ...] = ()
    scope: str = "wi_lifetime"
    as_of: str | None = None
    watermark: int | None = None
    value_from: str | None = None
    value_to: str | None = None
    cost_basis: str = "machine_standard"
    human_hourly_rate: str | None = None
    human_rate_policy: str | None = None
    node: str | None = None
    entrypoint: str | None = None
    tenant: str | None = None
    context_ref: str | None = None

    def __post_init__(self) -> None:
        ZoneInfo(self.timezone)
        if self.context_ref is not None:
            from .receipts import _ref
            _ref(self.context_ref, "query context_ref", False)
            if self.scope == "accepted_wi":
                raise ReceiptError("context selection cannot use a legacy accepted WI cohort")
        if any(not isinstance(w, str) or not __import__("re").fullmatch(r"wi_\d{8}_\d{3}", w) for w in self.work_items):
            raise ReceiptError("query work items must be canonical identities")
        if self.agent_role not in ROLES:
            raise ReceiptError("unknown Agent role")
        if len(set(self.group_by)) != len(self.group_by) or any(g not in {"wi", "agent", "node", "runtime", "model", "entrypoint", "tenant", "day", "week", "month"} for g in self.group_by):
            raise ReceiptError("unsupported or repeated grouping dimension")
        if self.scope not in {"wi_lifetime", "activity", "accepted_wi"}:
            raise ReceiptError("unknown cohort scope")
        if bool(self.from_date) != bool(self.to_date):
            raise ReceiptError("period requires both half-open boundaries")
        if self.from_date:
            if date.fromisoformat(self.to_date) <= date.fromisoformat(self.from_date):
                raise ReceiptError("query window is reversed or empty")
        if self.scope == "wi_lifetime" and self.from_date:
            raise ReceiptError("use activity or accepted_wi for a dated query")
        if self.scope != "wi_lifetime" and not self.from_date:
            raise ReceiptError("dated cohort requires a window")
        if any(g in {"day", "week", "month"} for g in self.group_by) and not self.from_date:
            raise ReceiptError("calendar grouping requires an explicit period/cohort")
        if bool(self.value_from) != bool(self.value_to):
            raise ReceiptError("value period requires two boundaries")
        if self.value_from and parse_timestamp(self.value_from, "value_from") >= parse_timestamp(self.value_to, "value_to"):
            raise ReceiptError("value period is reversed")
        if self.cost_basis not in {"machine_standard", "machine_plus_measured_human"}:
            raise ReceiptError("unsupported cost basis")
        if self.human_hourly_rate is not None:
            decimal(self.human_hourly_rate, "human_hourly_rate", nonnegative=True)
            if not self.human_rate_policy:
                raise ReceiptError("human rate requires an approved policy reference")

    @property
    def window(self) -> tuple[datetime, datetime] | None:
        if not self.from_date:
            return None
        tz = ZoneInfo(self.timezone)
        return (datetime.combine(date.fromisoformat(self.from_date), time.min, tzinfo=tz),
                datetime.combine(date.fromisoformat(self.to_date), time.min, tzinfo=tz))


_MISSING = object()


def _field(value: object, name: str) -> object:
    """A pre-read payload field; absent raises KeyError, as reading it directly would."""
    if value is _MISSING:
        raise KeyError(name)
    return value


def _in_window(timestamp: str | None, window: tuple[datetime, datetime] | None) -> bool:
    return window is None or (timestamp is not None and window[0] <= parse_timestamp(timestamp, "time") < window[1])


def _period(row: Receipt, window: tuple[datetime, datetime] | None,
            payload: Mapping[str, Any] | None = None) -> str:
    if window is None:
        return "inside"
    p = row.payload if payload is None else payload
    start, end = p.get("interval_start"), p.get("interval_end")
    if start is not None:
        a, b = parse_timestamp(start, "interval_start"), parse_timestamp(end, "interval_end")
        if a == b:
            return "inside" if window[0] <= a < window[1] else "outside"
        if b <= window[0] or a >= window[1]:
            return "outside"
        return "inside" if a >= window[0] and b <= window[1] else "time_unallocated"
    if p.get("granularity") in {"turn_aggregate", "session_counter"}:
        # Its start may be unknown, but an observed cumulative counter is an
        # upper time bound. It cannot contain usage from a later period.
        if row.occurred_at and parse_timestamp(row.occurred_at, "counter upper bound") <= window[0]:
            return "outside"
        return "time_unallocated"
    if row.occurred_at is None:
        return "time_unallocated"
    return "inside" if _in_window(row.occurred_at, window) else "outside"


def _counts(rows: Iterable[Receipt]) -> dict[str, Any]:
    values = {key: [] for key in TOKEN_FIELDS}
    for row in rows:
        for key in TOKEN_FIELDS:
            values[key].append(row.payload["tokens"][key])
    result = {}
    for key, vals in values.items():
        known = [v for v in vals if v is not None]
        result[key] = {"known": sum(known) if known or not vals else None,
                       "unknown_event_count": len(vals) - len(known)}
    return result


def _cc_host_attempt(row: Receipt, executions: dict[str, Receipt]) -> tuple | None:
    """Verify the existing Host's per-attempt/model partition, not session time alone."""
    p = row.payload
    if (p['runtime'] not in {'cc', 'cc_sdk'} or row.producer != p['mapping']
            or p['mapping'] not in {'cc_message.v1', 'cc_result.v1', 'cc_sdk_result.v1'}
            or not p.get('native_scope_ref') or not p.get('execution_ref')
            or p.get('coverage_ref') != reference('coverage', [p['execution_ref'], p['model']])):
        return None
    ex = executions.get(p['execution_ref'])
    if (ex is None or ex.producer != 'relay_host.v1' or not ex.evidence
            or ex.payload['runtime'] != p['runtime'] or not ex.occurred_at
            or not p['interval_start'] or not p['interval_end']):
        return None
    low = parse_timestamp(p['interval_start'], 'CC attempt start')
    high = parse_timestamp(p['interval_end'], 'CC observation end')
    if low != parse_timestamp(ex.occurred_at, 'Host attempt') or low > high:
        return None
    for key in ('wi', 'executor', 'responsible', 'delegator'):
        if p.get(key) is not None and ex.payload.get(key) is not None and p[key] != ex.payload[key]:
            return None
    return p['execution_ref'], p['native_scope_ref'], low, high


def _distinct_cc_attempts(left: Receipt, right: Receipt, executions: dict[str, Receipt]) -> bool:
    a, b = _cc_host_attempt(left, executions), _cc_host_attempt(right, executions)
    return bool(a and b and a[0] != b[0] and a[1] == b[1]
                and (a[3] < b[2] or b[3] < a[2]))

def _complete_cc_result_over_stream(aggregate: Receipt, targets: list[Receipt], executions: dict[str, Receipt]) -> bool:
    """Prefer the observed final total to its explicitly covered unfinished fragments.

    No quantity is reconstructed or assigned to a message. Complete, disagreeing
    messages remain conflicts; unproven covers cannot suppress observations.
    """
    p = aggregate.payload
    owner = _cc_host_attempt(aggregate, executions)
    if (owner is None or p['mapping'] not in {'cc_result.v1', 'cc_sdk_result.v1'}
            or not p['final'] or not targets or not p['covers']
            or len(targets) != len(p['covers']) or not any(not r.payload['final'] for r in targets)):
        return False
    for target in targets:
        q = target.payload
        child = _cc_host_attempt(target, executions)
        if (child is None or child[:2] != owner[:2] or child[3] > owner[3]
                or q['granularity'] != 'request' or q['mapping'] != 'cc_message.v1'
                or q['coverage_ref'] != p['coverage_ref'] or q['model'] != p['model']):
            return False
    for key in ('wi', 'executor', 'responsible', 'delegator'):
        if len({r.payload[key] for r in [aggregate, *targets] if r.payload.get(key) is not None}) > 1:
            return False
    for key in TOKEN_FIELDS:
        values = [r.payload['tokens'][key] for r in targets]
        if p['tokens'][key] is None or any(v is None for v in values) or sum(values) > p['tokens'][key]:
            return False
    return True


def _cc_non_provider_observations(rows: list[Receipt], executions: dict[str, Receipt]) -> set[str]:
    """Keep CC local protocol notices in history, not in provider usage totals.

    The reserved model marker is shared with bounded CC sampling. Legacy ledger
    selection additionally requires explicit zero counters; unknown/contrary
    measurements and unproven Host failures are not silently discarded.
    """
    def zero_cc(row):
        p = row.payload
        return (p["runtime"] in {"cc", "cc_sdk"} and row.producer == p["mapping"]
                and p["mapping"] in {"cc_message.v1", *CC_RESULT_MAPPINGS} and p["final"]
                and all(type(p["tokens"].get(k)) is int and p["tokens"][k] == 0 for k in TOKEN_FIELDS)
                and all(type(p["native_tokens"].get(k)) is int and p["native_tokens"][k] == 0
                        for k in ("input_tokens", "output_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")))
    notices = {r.entity_ref for r in rows if zero_cc(r) and r.payload["mapping"] == "cc_message.v1"
               and r.payload["granularity"] == "request" and not r.payload["covers"]
               and r.payload["model"] == CC_SYNTHETIC_MODEL}
    by_ref = {r.entity_ref: r for r in rows}
    excluded = set(notices)
    for row in rows:
        p = row.payload
        if not zero_cc(row) or p["mapping"] not in CC_RESULT_MAPPINGS or p["granularity"] != "turn_aggregate":
            continue
        if p["model"] == CC_SYNTHETIC_MODEL and p["covers"] and set(p["covers"]) <= notices:
            if p.get("native_scope_ref") and all(
                    by_ref[k].payload["runtime"] == p["runtime"]
                    and by_ref[k].payload.get("native_scope_ref") == p["native_scope_ref"] for k in p["covers"]):
                excluded.add(row.entity_ref)
        elif p["model"] is None and not p["covers"]:
            owner = _cc_host_attempt(row, executions)
            if owner is None or executions[owner[0]].payload["state"] not in {"failed", "cancelled"}:
                continue
            possible = [r for r in rows if r.entity_ref not in notices
                        and r.payload["granularity"] == "request" and r.payload["runtime"] == p["runtime"]
                        and (r.payload.get("execution_ref") == owner[0]
                             or (r.payload.get("native_scope_ref") == owner[1]
                                 and _may_overlap(r, owner[2], owner[3], True)))]
            if not possible:
                excluded.add(row.entity_ref)
    return excluded


def reconcile_usage(rows: list[Receipt], *, executions: list[Receipt] | None = None, inventories: list[Receipt] | None = None, assignment_aliases: dict | None = None, input_links: dict[str, set[str]] | None = None, final_native_refs: set[str] | None = None, native_settled_refs: set[str] | None = None, native_observed_refs: set[str] | None = None, cumulative_scopes: set[str] | None = None, rejected_cc_scopes: set[str] | None = None) -> tuple[list[Receipt], dict[str, Any]]:
    """Remove only proven complete overlaps; unresolved components aren't summed."""
    execution_by_ref = {r.entity_ref: r for r in executions or []}
    non_provider = _cc_non_provider_observations(rows, execution_by_ref)
    rows = [r for r in rows if r.entity_ref not in non_provider]
    proven_links = {key: set(value) for key, value in (input_links or {}).items()}
    boundary_suppressed = reconcile_host_inventory(rows, inventories or [], executions or [], execution_links=proven_links, assignment_aliases=assignment_aliases, input_links=input_links, final_native_refs=final_native_refs)
    by_ref = {r.entity_ref: r for r in rows}
    rows = [r for r in rows if r.entity_ref not in boundary_suppressed]
    # CC requests whose settled counters come from the native transcript.
    native_settled = set(native_settled_refs or ()) | {r.entity_ref for r in rows if _native_cc_settled(r)}
    native_observed = set(native_observed_refs or ()) | {r.entity_ref for r in rows if _native_cc_observed(r)}
    from .cc_session_counters import reconcile_session_counters, reconcile_unitemized_counters
    rows, cc_residuals, cc_suppressed, cc_proofs = reconcile_session_counters(
        rows, cumulative_scopes or set(), native_observed, rejected_scopes=rejected_cc_scopes or set())
    rows, unitemized_residuals, unitemized_suppressed, counter_fields_missing, counter_regressed, unitemized_proofs = \
        reconcile_unitemized_counters(rows)
    cc_residuals += unitemized_residuals
    by_ref = {r.entity_ref: r for r in rows}
    suppressed, conflicting = set(boundary_suppressed) | cc_suppressed | unitemized_suppressed, set()
    conflict_reasons: dict[str, set[str]] = defaultdict(set)
    def conflict(reason: str, refs: Iterable[str]) -> None:
        for ref in refs:
            conflicting.add(ref)
            conflict_reasons[ref].add(reason)
    conflict("cc_counter_fields_missing", counter_fields_missing)
    conflict("cc_counter_regressed", counter_regressed)
    # A Host can finish without a usage measurement. An entirely absent
    # counter set is not a competing numeric assertion. Keep that row and
    # its unknown/unpriced diagnostics, but do not let it quarantine
    # independently measured native requests or invent a deduplication.
    missing_measurements = {r.entity_ref for r in rows
        if r.payload["mapping"] == "relay_pi.v1"
        and r.payload["granularity"] == "turn_aggregate"
        and not r.payload["covers"] and not r.payload["native_tokens"]
        and r.payload.get("reported_total_tokens") is None
        and all(r.payload["tokens"][k] is None for k in TOKEN_FIELDS)}
    # Relay omits the turn total when the Pi turn compacted; durable native
    # history then itemizes the same turn. The empty Host row is suppressed
    # only when a complete parsed native prefix of that session (its
    # `pi_native_prefix_inventory`) holds every native request inside the Host
    # interval and also a later request: the append-only file then covers the
    # whole turn. A lagging copy, a partial read or a turn not yet followed by
    # another request stays unknown, as do requests lacking a model or a count.
    # It only drops the empty row: no identity moves to the requests.
    native_rows: dict[str, list[tuple[datetime, Receipt]]] = defaultdict(list)
    for row in rows:
        p = row.payload
        if (p["mapping"] == "pi_native.v1" and p["granularity"] == "request"
                and p.get("native_scope_ref") and row.occurred_at):
            native_rows[p["native_scope_ref"]].append((parse_timestamp(row.occurred_at, "native time"), row))
    prefixes: dict[str, list[tuple[set[str], datetime]]] = defaultdict(list)
    for inventory in inventories or []:
        p = inventory.payload
        if (inventory.producer != "pi_native_inventory.v1" or p.get("reason") != "pi_native_prefix_inventory"
                or p.get("observed") != len(inventory.evidence)):
            continue
        listed = set(inventory.evidence)
        times = [at for at, row in native_rows.get(p["source_ref"], ()) if row.entity_ref in listed]
        if times:
            prefixes[p["source_ref"]].append((listed, max(times)))
    for row in rows:
        p = row.payload
        scope = p.get("native_scope_ref")
        if row.entity_ref not in missing_measurements or not scope or not p["interval_start"] or not p["interval_end"]:
            continue
        low = parse_timestamp(p["interval_start"], "Host start")
        high = parse_timestamp(p["interval_end"], "Host end")
        inside = [r for at, r in native_rows.get(scope, ()) if low <= at <= high]
        if inside and all(r.payload["final"] and r.payload["model"] is not None
                          and all(r.payload["tokens"][k] is not None for k in TOKEN_FIELDS) for r in inside) \
                and any(latest > high and {r.entity_ref for r in inside} <= listed
                        for listed, latest in prefixes.get(scope, ())):
            suppressed.add(row.entity_ref)
    details = [r for r in rows if r.payload["granularity"] == "request"]
    # `native_scope_ref` is an opaque producer correlation, not a quantity or
    # time heuristic.  New producers can therefore say that a native detail
    # and summary describe the same session without pretending that they have
    # an exact cover list.  That is an unresolved overlap, not an additive
    # reconciled total.  Rows without it retain the historical contract.
    scoped_details: dict[str, list[Receipt]] = defaultdict(list)
    scoped_aggregates: dict[str, list[Receipt]] = defaultdict(list)
    for row in rows:
        scope = row.payload.get("native_scope_ref")
        if not scope or row.entity_ref in missing_measurements:
            continue
        if row.payload["granularity"] == "request":
            scoped_details[scope].append(row)
        else:
            scoped_aggregates[scope].append(row)
    result_history = _cc_result_history(rows)
    for aggregate in rows:
        p = aggregate.payload
        if p["granularity"] == "request" or aggregate.entity_ref in missing_measurements:
            continue
        targets = [by_ref[k] for k in p["covers"] if k in by_ref]
        native_scope = p.get("native_scope_ref")
        # An explicit cover list remains proof.  In its absence, a shared
        # native scope establishes possible overlap only; do not infer a
        # subtraction from matching counts or nearby timestamps.
        if not p["covers"] and p["mapping"] == "pi_tool_summary.v1":
            # Without child coverage identity, do not add this summary, but do
            # not quarantine unrelated independently observed native requests
            # across the entire Pi fleet either. Retain them as a known subset.
            conflict("summary_without_cover_list", [aggregate.entity_ref])
            continue
        if not p["covers"] and native_scope:
            # Requests and summaries of different known models are different
            # provider consumption; only an unknown model may overlap them.
            possible = [r for r in scoped_details.get(native_scope, [])
                        if not _distinct_cc_attempts(aggregate, r, execution_by_ref)
                        and _same_or_unknown_model(aggregate, r)]
            # Known Host dispatch bounds cannot cover native requests outside
            # that interval. Unknown bounds remain conservatively session-wide.
            cc_result = p["mapping"] in CC_RESULT_MAPPINGS
            if (p["mapping"] in {"relay_pi.v1", "codex_cli.v1"} or cc_result) and p["interval_start"] and p["interval_end"]:
                low = parse_timestamp(p["interval_start"], "Host start")
                high = parse_timestamp(p["interval_end"], "Host end")
                candidates = possible
                possible = [r for r in candidates if _may_overlap(r, low, high, cc_result)]
                if candidates and not possible:
                    if cc_result and _repeats_previous_cc_counter(aggregate, result_history):
                        # The attempt made no request of this model; its result
                        # repeats the session counter an earlier result recorded.
                        suppressed.add(aggregate.entity_ref)
                        continue
                    # Equal counters outside the producer interval are not
                    # proof of either duplication or independent consumption.
                    # Keep the unmatched summary unresolved, not every request.
                    conflict("summary_without_detail_in_interval", [aggregate.entity_ref])
                if cc_result and possible and (all(
                        r.entity_ref in native_settled and r.payload["final"]
                        and all(r.payload["tokens"][k] is not None for k in TOKEN_FIELDS)
                        for r in possible) or _cc_result_over_message_detail(aggregate, possible, native_observed)):
                    # A CC result without a cover list is a Host summary of
                    # requests the native transcript already itemizes (often a
                    # cumulative session counter). The message detail in the
                    # attempt interval wins; the summary is never added.
                    suppressed.add(aggregate.entity_ref)
                    continue
            # A scope only correlates observations; it is not an assertion that
            # two producer streams are disjoint.  In particular Codex Host and
            # rollout both publish turn aggregates.  Same-producer aggregates
            # may be distinct turns (their durable identities say so), whereas
            # cross-producer aggregates sharing the scope are unresolved absent
            # an explicit cover list.
            peers = [r for r in scoped_aggregates[native_scope]
                     if r.entity_ref != aggregate.entity_ref and r.producer != aggregate.producer
                     and not _distinct_cc_attempts(aggregate, r, execution_by_ref)
                     and _same_or_unknown_model(aggregate, r)]
            if possible or peers:
                conflict("unproven_session_overlap", [aggregate.entity_ref, *(r.entity_ref for r in possible),
                                                      *(r.entity_ref for r in peers)])
            # Continue into the execution fallback: distinct opaque scopes do
            # not prove independence either.
        if not p["covers"] and p["coverage_ref"]:
            targets = [r for r in details if r.payload["coverage_ref"] == p["coverage_ref"]
                       and r.payload["model"] == p["model"]]
        if not targets:
            if p["coverage_ref"] is None and p["execution_ref"] is not None:
                possible = [r for r in details if r.payload["execution_ref"] == p["execution_ref"]]
                if possible:
                    conflict("unproven_execution_overlap", [aggregate.entity_ref, *(r.entity_ref for r in possible)])
            continue
        exact = not p["covers"] or len(targets) == len(p["covers"])
        exact = exact and all(r.entity_ref != aggregate.entity_ref and r.payload["granularity"] == "request"
                                  and r.payload["model"] == p["model"] and r.payload["runtime"] == p["runtime"]
                                  for r in targets)
        for key in TOKEN_FIELDS:
            vals = [r.payload["tokens"][key] for r in targets]
            if p["tokens"][key] is None or any(v is None for v in vals) or sum(vals) != p["tokens"][key]:
                exact = False
        if exact:
            suppressed.add(aggregate.entity_ref)
        elif _complete_cc_result_over_stream(aggregate, targets, execution_by_ref):
            suppressed.update(r.entity_ref for r in targets)
        elif _native_detail_under_cc_result(aggregate, targets, native_settled):
            suppressed.add(aggregate.entity_ref)
        elif _cc_result_over_message_detail(aggregate, targets, native_observed):
            suppressed.add(aggregate.entity_ref)
        else:
            conflict("summary_detail_mismatch", [aggregate.entity_ref, *(r.entity_ref for r in targets)])
    aggregate_scopes: dict[str, list[Receipt]] = defaultdict(list)
    for row in rows:
        if row.entity_ref not in missing_measurements and row.payload["granularity"] != "request" and row.payload["coverage_ref"]:
            aggregate_scopes[row.payload["coverage_ref"]].append(row)
    for group in aggregate_scopes.values():
        remaining = [r for r in group if r.entity_ref not in suppressed]
        if len(remaining) > 1:
            conflict("repeated_summary", [r.entity_ref for r in remaining])
    from .pi_history import reconcile_copies
    copied = reconcile_copies(rows, inventories or [])
    active = [r for r in rows if r.entity_ref not in suppressed | conflicting | set(copied)]
    linked = []
    identity_fields = ("wi", "executor", "responsible", "delegator")
    for row in active + cc_residuals:
        links = proven_links.get(row.entity_ref, set())
        payload = row.payload
        if payload["execution_ref"] is None and len(links) == 1:
            # The exact Host/native conservation witness also proves this
            # execution edge. This is a query projection, never new usage.
            payload["execution_ref"] = next(iter(links))
        execution = execution_by_ref.get(next(iter(links))) if len(links) == 1 else None
        if execution is not None:
            authority = execution.payload
            boundary = payload.get("interval_start") or row.occurred_at
            # Quantity deduplication must not discard the identity already
            # frozen by the matching Host dispatch. No legacy snapshot, path,
            # current configuration or late assignment supplies this identity.
            # A recorded Host purpose is accounting evidence even before a
            # grantee has a SCL grant. Requiring assignment_ref here lost that
            # already-known purpose after exact Host/native deduplication.
            if (execution.producer == "relay_host.v1" and (authority.get("assignment_ref") or execution.evidence
                    or (input_links or {}).get(row.entity_ref) == {execution.entity_ref})
                    and authority.get("wi") and authority.get("executor")
                    and authority["runtime"] == payload["runtime"]
                    and execution.occurred_at and boundary
                    and parse_timestamp(execution.occurred_at, "assignment") <= parse_timestamp(boundary, "native boundary")
                    and all(payload.get(k) is None or authority.get(k) is None or payload[k] == authority[k]
                            for k in identity_fields)):
                for key in identity_fields:
                    if payload.get(key) is None:
                        payload[key] = authority.get(key)
        if payload != row.payload:
            row = replace(row, _payload_json=canonical(payload))
        linked.append(row)
    return linked, {"suppressed_aggregate_refs": sorted(suppressed),
                    "conflicting_refs": sorted(conflicting), "status": "conflict" if conflicting else "reconciled",
                    "conflict_reasons": {ref: sorted(conflict_reasons[ref]) for ref in sorted(conflicting)},
                    "native_boundary_supersession": boundary_suppressed, "copied_native_observations": copied,
                    "non_provider_observation_refs": sorted(non_provider), "cc_session_counters": cc_proofs,
                    "cc_unitemized_counters": unitemized_proofs}


def _same_or_unknown_model(left: Receipt, right: Receipt) -> bool:
    a, b = left.payload["model"], right.payload["model"]
    return a is None or b is None or a == b


def _native_cc_observed(row: Receipt) -> bool:
    p = row.payload
    return (row.producer == p["mapping"] == "cc_message.v1" and p["granularity"] == "request"
            and p["execution_ref"] is None)


def _native_cc_settled(row: Receipt) -> bool:
    return _native_cc_observed(row) and row.payload["final"]


def _may_overlap(row: Receipt, low: datetime, high: datetime, use_interval: bool) -> bool:
    """Unknown time stays possible. CC joins may carry an interval, not a point."""
    p = row.payload
    if row.occurred_at is not None:
        return low <= parse_timestamp(row.occurred_at, "native time") <= high
    if use_interval and p.get("interval_start") and p.get("interval_end"):
        return (parse_timestamp(p["interval_start"], "native start") <= high
                and low <= parse_timestamp(p["interval_end"], "native end"))
    return True


def _native_detail_under_cc_result(aggregate: Receipt, targets: list[Receipt], native_settled: set[str]) -> bool:
    """A Host CC result listing natively settled messages is not added to them.

    The Host result counter may be cumulative over the resumed session, so it
    can exceed its listed messages. Every listed message must be present and
    settled by the native transcript, and no counter may exceed the result;
    otherwise the listed quantities contradict and stay a conflict.
    """
    p = aggregate.payload
    if (p["mapping"] not in CC_RESULT_MAPPINGS or not p["final"] or not p["covers"]
            or len(targets) != len(p["covers"])):
        return False
    for target in targets:
        q = target.payload
        if (target.entity_ref not in native_settled or not q["final"] or q["granularity"] != "request"
                or q["runtime"] != p["runtime"] or q["model"] != p["model"]
                or q.get("native_scope_ref") != p.get("native_scope_ref")):
            return False
    for key in TOKEN_FIELDS:
        values = [r.payload["tokens"][key] for r in targets]
        if p["tokens"][key] is None or any(v is None for v in values) or sum(values) > p["tokens"][key]:
            return False
    return True


def _cc_result_over_message_detail(aggregate: Receipt, targets: list[Receipt], native_observed: set[str]) -> bool:
    """A CC result counter is never added to the messages it summarizes.

    A completed CC message can remain a streamed snapshot: the Host observes it
    while streaming and the transcript (notably a subagent transcript) keeps
    only content-block entries without a stop reason or final output count.
    That message is provisional detail, a lower bound, not a contradiction of
    the cumulative result. Every message must be in the native transcript
    with known counters of the same runtime, model, session, Host attempt and
    identity, and no counter may exceed the result; otherwise the summary and
    its detail stay unresolved.
    """
    p = aggregate.payload
    if (aggregate.producer != p['mapping'] or p['mapping'] not in CC_RESULT_MAPPINGS
            or not p['final'] or not p.get('native_scope_ref') or not targets
            or (p['covers'] and len(targets) != len(p['covers']))):
        return False
    for target in targets:
        q = target.payload
        if (target.entity_ref not in native_observed or q['mapping'] != 'cc_message.v1'
                or q['granularity'] != 'request' or q['runtime'] != p['runtime'] or q['model'] != p['model']
                or q.get('native_scope_ref') != p['native_scope_ref']):
            return False
        if q['execution_ref'] is not None and (q['execution_ref'] != p['execution_ref']
                                               or q['coverage_ref'] != p['coverage_ref']):
            return False
    for key in ('wi', 'executor', 'responsible', 'delegator'):
        known = {r.payload.get(key) for r in [aggregate, *targets] if r.payload.get(key) is not None}
        if len(known) > 1 or 'ambiguous' in known or 'excluded' in known:
            return False
    for key in TOKEN_FIELDS:
        values = [r.payload['tokens'][key] for r in targets]
        if p['tokens'][key] is None or any(v is None for v in values) or sum(values) > p['tokens'][key]:
            return False
    return True


def _settled_cc_counters(payloads: list[Mapping[str, Any]]) -> dict[str, dict] | None:
    """Final CC message counters that dominate every unfinished observation.

    The Host records a CC message while it is still streaming (final=False);
    the native transcript later records the same message id with its settled
    counters. This is the counter refinement `_revision` accepts within one
    producer, applied across the two observations of one request. Two
    different final counters, or a partial counter above the final one, are
    still contradictions and return None.
    """
    if any(p["mapping"] != "cc_message.v1" for p in payloads):
        return None
    finals = [p for p in payloads if p["final"]]
    partials = [p for p in payloads if not p["final"]]
    if not finals or not partials:
        return None
    answer = {field: finals[0][field] for field in ("tokens", "native_tokens")}
    if any(p[field] != answer[field] for p in finals for field in answer):
        return None
    if any(answer["tokens"].get(k) is None for k in TOKEN_FIELDS):
        return None
    for p in partials:
        for field in answer:
            for key, value in p[field].items():
                if value is None:
                    continue
                settled = answer[field].get(key)
                if settled is None or value > settled:
                    return None
    return answer


def _codex_progress_observation(observations: list[Receipt]) -> Receipt | None:
    """Resolve missing edges within one evidenced cumulative native turn.

    A common source, exact origin and ordered nonfinal counter progression are
    required. Final disagreements never become revisions. This projection does
    not rewrite the version graph or mark an interrupted turn as settled.
    """
    if not observations or any(r.kind != 'usage' or r.producer != 'codex_rollout.v1'
                               for r in observations):
        return None
    if len({r.entity_ref for r in observations}) != 1 or not set.intersection(
            *(set(r.evidence) for r in observations)):
        return None
    payloads = [r.payload for r in observations]
    changing = {'tokens', 'native_tokens', 'reported_total_tokens', 'interval_end', 'final'}
    stable = {canonical({k:v for k,v in p.items() if k not in changing}) for p in payloads}
    p = payloads[0]
    if (len(stable) != 1 or p.get('runtime') != 'codex' or
            p.get('mapping') != 'codex_rollout_cumulative.v1' or
            p.get('granularity') != 'turn_aggregate' or not p.get('native_scope_ref') or
            not p.get('interval_start') or p.get('model') is None or p.get('metering') is not None):
        return None
    native_keys = ('input_tokens','cached_input_tokens','cache_write_input_tokens','output_tokens')
    baselines, ordered = set(), []
    for row, p in zip(observations, payloads):
        native, tokens = p['native_tokens'], p['tokens']
        if (set(native) != set(native_keys) or
                any(type(native[k]) is not int or native[k] < 0 for k in native_keys) or
                any(type(tokens[k]) is not int or tokens[k] < 0 for k in TOKEN_FIELDS) or
                p.get('reported_total_tokens') != sum(tokens.values())):
            return None
        delta = (tokens['uncached_input'] + tokens['cache_read'] + tokens['cache_write'],
                 tokens['cache_read'], tokens['cache_write'], tokens['output'])
        baseline = tuple(native[k] - amount for k,amount in zip(native_keys, delta))
        if min(baseline) < 0 or baseline[0] < baseline[1] + baseline[2]:
            return None
        baselines.add(baseline)
        try:
            at = parse_timestamp(row.occurred_at, 'native counter')
            if at != parse_timestamp(p['interval_end'], 'counter end') or at < parse_timestamp(p['interval_start'], 'counter start'):
                return None
        except (ValueError, TypeError):
            return None
        ordered.append((at, row.fact_ref, row, p))
    if len(baselines) != 1:
        return None
    ordered.sort(key=lambda item: item[:2])
    for before, after in zip(ordered, ordered[1:]):
        a, b = before[3], after[3]
        if (any(b['native_tokens'][k] < a['native_tokens'][k] for k in native_keys) or
                any(b['tokens'][k] < a['tokens'][k] for k in TOKEN_FIELDS) or
                (before[0] == after[0] and (a['tokens'] != b['tokens'] or a['native_tokens'] != b['native_tokens']))):
            return None
    # The native terminal assertion is unique; no later/greater final wins.
    finals = [item for item in ordered if item[3]['final']]
    choices = finals or [item for item in ordered if item[0] == ordered[-1][0]]
    chosen = min(choices, key=lambda item: item[1])
    if finals and any(item[0] != chosen[0] or item[3] != chosen[3] for item in finals):
        return None
    if chosen[0] != ordered[-1][0]:
        return None
    return replace(chosen[2], evidence=tuple(sorted(set().union(*(r.evidence for r in observations)))))


def _cc_result_history(rows: Iterable[Receipt]) -> dict[tuple, list[tuple]]:
    history: dict[tuple, list[tuple]] = defaultdict(list)
    for row in rows:
        if row.kind != "usage" or row.producer not in CC_RESULT_MAPPINGS or not row.occurred_at:
            continue
        p = row.payload
        if row.producer == p["mapping"] and p["final"] and p.get("native_scope_ref"):
            history[(p["native_scope_ref"], p["model"])].append(
                (parse_timestamp(row.occurred_at, "CC result"), p["tokens"], p.get("execution_ref"), row.entity_ref))
    return history


def _request_times(rows: Iterable[Receipt]) -> dict[str, list[datetime]]:
    times: dict[str, list[datetime]] = defaultdict(list)
    for row in rows:
        if row.kind == "usage" and row.producer == "cc_message.v1":
            p = row.payload
            at = row.occurred_at or p.get("interval_end")
            if at and p["granularity"] == "request":
                times[row.entity_ref].append(parse_timestamp(at, "CC message"))
    return times


def _repeats_previous_cc_counter(row: Receipt, history: Mapping[tuple, list[tuple]]) -> bool:
    """The latest earlier CC result of the same session and model has equal counters."""
    p = row.payload
    if not row.occurred_at or p["covers"]:
        return False
    at = parse_timestamp(row.occurred_at, "CC result")
    earlier = [h for h in history.get((p.get("native_scope_ref"), p["model"]), ())
               if h[0] < at and h[3] != row.entity_ref]
    return bool(earlier) and max(earlier, key=lambda h: h[0])[1] == p["tokens"]


def _replayed_cc_result(observations: list[Receipt], history: Mapping[tuple, list[tuple]],
                        request_times: Mapping[str, list[datetime]]) -> Receipt | None:
    """Resolve one Host attempt's repeated CC final results.

    Equal results are one fact. A resumed CC process can first deliver the
    result of a queued turn that made no provider request. That result lists
    no message and carries the session counter from before this attempt's
    first request: it repeats the counter an earlier result recorded, or the
    attempt's own result lists messages that all began later and its counters
    are not smaller. Such a version is not this attempt's result. Any other
    disagreement remains a conflict.
    """
    if any(r.kind != "usage" or r.producer not in CC_RESULT_MAPPINGS or not r.occurred_at for r in observations):
        return None
    payloads = [r.payload for r in observations]
    stable = ("mapping", "runtime", "model", "native_scope_ref", "execution_ref", "coverage_ref", "granularity",
              "wi", "executor", "responsible", "delegator")
    if any(p["mapping"] != r.producer or not p["final"] for r, p in zip(observations, payloads)):
        return None
    if len({canonical([p.get(k) for k in stable]) for p in payloads}) != 1 or not payloads[0].get("native_scope_ref"):
        return None
    def content(p):
        return canonical({k: v for k, v in p.items() if k != "interval_end"})
    def precedes_attempt(row, p):
        if p["covers"] or any(v is None for v in p["tokens"].values()):
            return False
        at = parse_timestamp(row.occurred_at, "CC result")
        if any(tokens == p["tokens"] and execution != p["execution_ref"] and entity != row.entity_ref
               and earlier < at for earlier, tokens, execution, entity
               in history.get((p["native_scope_ref"], p["model"]), ())):
            return True
        return any(q["covers"] and all(q["tokens"][k] is not None and q["tokens"][k] >= p["tokens"][k]
                                       for k in TOKEN_FIELDS)
                   and all(request_times.get(ref) and min(request_times[ref]) > at for ref in q["covers"])
                   for q in payloads)
    kept = [r for r, p in zip(observations, payloads)
            if len({content(q) for q in payloads}) == 1 or not precedes_attempt(r, p)]
    if not kept or len({content(r.payload) for r in kept}) != 1:
        return None
    chosen = min(kept, key=lambda r: r.fact_ref)
    return replace(chosen, evidence=tuple(sorted(set().union(*(r.evidence for r in observations)))))


def _request_observation_projection(active: list[Receipt], conflicts: dict[str, list[Receipt]]) -> tuple[
        list[Receipt], dict[str, list[Receipt]], dict[str, list[str]], dict[str, set[str]]]:
    """Join observations of one proven request, not revisions or nearby requests.

    A Host records assignment/dispatch while the native transcript records the
    provider message. The same identity must not become two billable requests or
    disappear solely because one source lacks assignment. Counts/models that
    actually disagree remain conflicts. This is an ephemeral projection: every
    source fact and its original recorded-at barrier stays in UsageLedger.
    """
    from .native_metering import metered_observation
    result, unresolved, groups, ambiguous = list(active), dict(conflicts), {}, {}
    history, request_times = None, {}
    for entity, observations in sorted(conflicts.items()):
        metered = metered_observation(observations)
        if metered is not None:
            result.append(metered)
            groups[entity] = sorted(r.fact_ref for r in observations)
            unresolved.pop(entity, None)
            continue
        progress = _codex_progress_observation(observations)
        if progress is not None:
            result.append(progress)
            groups[entity] = sorted(r.fact_ref for r in observations)
            unresolved.pop(entity, None)
            continue
        # A retained native baseline witness can reconcile two historical
        # interpretations of the same Codex counter. Do not pick a larger,
        # later or final value generically; every older interpretation must
        # pass the exact existing correction validator against one answer.
        if all(r.kind == "usage" and r.producer == "codex_rollout.v1" for r in observations):
            corrected = [r for r in observations
                         if r.payload.get("mapping") == "codex_rollout_paginated.v1" and r.evidence]
            answers = {(r._payload_json, r.occurred_at) for r in corrected}
            if len(answers) == 1:
                from .receipt_sources import paginated_counter_correction
                chosen = min(corrected, key=lambda r: r.fact_ref)
                if all((r._payload_json == chosen._payload_json and r.occurred_at == chosen.occurred_at)
                       or paginated_counter_correction(r, chosen) is not None for r in observations):
                    result.append(replace(chosen, evidence=tuple(sorted(set().union(*(r.evidence for r in observations))))))
                    groups[entity] = sorted(r.fact_ref for r in observations)
                    unresolved.pop(entity, None)
                    continue
        if history is None and all(r.producer in CC_RESULT_MAPPINGS for r in observations):
            evidence = [*active, *(r for rs in conflicts.values() for r in rs)]
            history, request_times = _cc_result_history(evidence), _request_times(evidence)
        replayed = _replayed_cc_result(observations, history or {}, request_times)
        if replayed is not None:
            result.append(replayed)
            groups[entity] = sorted(r.fact_ref for r in observations)
            unresolved.pop(entity, None)
            continue
        if not all(r.kind == "usage" and r.payload["granularity"] == "request" for r in observations):
            continue
        payloads = [r.payload for r in observations]
        # A CC projects file records the message protocol, not whether the
        # Host used CLI or SDK. Only the identical native request identity and
        # one proven Host execution may refine that source-format label.
        if len({p["mapping"] for p in payloads}) != 1:
            continue
        runtimes = {p["runtime"] for p in payloads}
        runtime = payloads[0]["runtime"]
        if len(runtimes) > 1:
            executed = {p["runtime"] for p in payloads if p["execution_ref"] is not None}
            native = [p for p in payloads if p["execution_ref"] is None]
            if (payloads[0]["mapping"] != "cc_message.v1" or runtimes != {"cc", "cc_sdk"}
                    or executed != {"cc_sdk"} or not native
                    or any(p["runtime"] != "cc" for p in native)):
                continue
            runtime = "cc_sdk"
        if any(p["covers"] for p in payloads):
            continue
        merged = {**payloads[0], "runtime": runtime}
        incompatible = False
        for field in ("model", "provider", "native_scope_ref", "reported_total_tokens"):
            # Older Host receipts have no native_scope_ref at all; a newer
            # native observation may safely supplement that absent/null value.
            # Two distinct opaque scopes, however, cannot identify one exact
            # request and must remain a revision conflict.
            known = {p.get(field) for p in payloads if p.get(field) is not None}
            if len(known) > 1:
                incompatible = True
            merged[field] = next(iter(known)) if len(known) == 1 else None
        settled = _settled_cc_counters(payloads)
        for field in ("tokens", "native_tokens"):
            if settled is not None:
                merged[field] = dict(settled[field])
                continue
            fields = set().union(*(p[field] for p in payloads))
            merged[field] = {}
            for key in sorted(fields):
                known = {p[field].get(key) for p in payloads if p[field].get(key) is not None}
                if len(known) > 1:
                    incompatible = True
                merged[field][key] = next(iter(known)) if len(known) == 1 else None
        total = merged.get("reported_total_tokens")
        parts = [merged["tokens"].get(k) for k in TOKEN_FIELDS]
        if total is not None and all(v is not None for v in parts) and sum(parts) != total:
            incompatible = True
        if incompatible:
            continue
        attribution_conflict = False
        for field in ("wi", "executor", "responsible", "delegator", "execution_ref", "coverage_ref"):
            known = {p[field] for p in payloads if p[field] is not None}
            merged[field] = next(iter(known)) if len(known) == 1 else None
            if len(known) > 1:
                attribution_conflict = True
        if attribution_conflict:
            ambiguous[entity] = {p["wi"] for p in payloads if p["wi"]}
            merged.update(wi="ambiguous", responsible=None, delegator=None)
        merged["final"] = any(p["final"] for p in payloads)
        intervals = [(parse_timestamp(p["interval_start"], "start"), parse_timestamp(p["interval_end"], "end"))
                     for p in payloads if p["interval_start"] is not None]
        points = {parse_timestamp(r.occurred_at, "occurred_at") for r in observations
                  if r.occurred_at and r.payload["interval_start"] is None}
        if points and all(a <= min(points) <= b for a, b in intervals):
            # The native request timestamps refine an encompassing Host interval.
            # Several are the records CC writes for one streamed reply: the Host
            # observed the reply's start; its later blocks may follow that end.
            at = utc_text(next(iter(points))) if len(points) == 1 else None
            merged.update(interval_start=utc_text(min(points)), interval_end=utc_text(max(points)))
        else:
            boundaries = [t for interval in intervals for t in interval] + list(points)
            at = None
            merged.update(interval_start=utc_text(min(boundaries)) if boundaries else None,
                          interval_end=utc_text(max(boundaries)) if boundaries else None)
        refs = sorted(r.fact_ref for r in observations)
        base = min(observations, key=lambda r: r.fact_ref)
        result.append(replace(base, occurred_at=at, observed_at=max(r.observed_at for r in observations),
                              evidence=tuple(sorted(set().union(*(r.evidence for r in observations)))),
                              _payload_json=canonical(merged)))
        groups[entity] = refs
        del unresolved[entity]
    return result, unresolved, groups, ambiguous


def _value_cost(row: Receipt, catalog: Mapping[str, Any] | None, model_cache: dict | None = None) -> tuple[Decimal | None, str | None]:
    p = row.payload
    # This metric prices the four Token classes, not provider request fees.
    # Explicit zero consumption needs no rate. Missing quantities are not zero;
    # identity/coverage diagnostics remain independent of this cost result.
    if all(p["tokens"][key] == 0 for key in TOKEN_FIELDS):
        return Decimal(0), None
    counts = p["tokens"]
    # An unknown quantity cannot be priced by any model or catalog; report it
    # before the price lookup so a measurement gap is not a price gap.
    if any(counts[k] is None for k in TOKEN_FIELDS):
        return None, "unknown_token_quantity"
    if not catalog or not p["model"] or p["model"] == "unknown":
        return None, "missing_price_or_model"
    try:
        model_id = _or_model_id(p["model"])
        if model_cache is not None and model_id in model_cache:
            model = model_cache[model_id]
        else:
            try:
                model = _model(catalog, model_id)
            except WiPricingError:
                if model_cache is not None:
                    model_cache[model_id] = {}
                raise
            if model_cache is not None:
                model_cache[model_id] = model
        pricing = model["pricing"]
        if not isinstance(pricing, Mapping):
            return None, "missing_or_invalid_price"
        rates = {"uncached_input": "prompt", "cache_read": "input_cache_read",
                 "cache_write": "input_cache_write", "output": "completion"}
        total = Decimal(0)
        for key, field in rates.items():
            if counts[key] == 0:
                continue
            total += counts[key] * decimal(pricing.get(field), field, nonnegative=True)
        return total, None
    except (WiPricingError, ReceiptError, KeyError, TypeError, AttributeError):
        return None, "missing_or_invalid_price"


def _ratio(n: int, d: int, eligible: int) -> dict[str, Any]:
    return {"numerator": n, "denominator": d, "eligible": eligible,
            "value": decimal_text(Decimal(n) / d) if d else None,
            "decision_coverage": decimal_text(Decimal(d) / eligible) if eligible else None,
            "status": "known_subset" if d else "unavailable", "unknown": max(eligible - d, 0)}


def quality_summary(deliveries: list[Receipt], reviews: list[Receipt]) -> dict[str, Any]:
    approved = decided = accepted = delivered_decisions = first_yes = first_known = 0
    pending = waived = conflicts = 0
    rounds, corrections, critical = [], [], []
    scores: dict[str, list[Decimal]] = defaultdict(list)
    activity_refs = set()
    for delivery in deliveries:
        d = delivery.payload
        accepted += d["status"] == "accepted"
        delivered_decisions += d["status"] in {"accepted", "partial", "rejected"}
        history = [r for r in reviews if r.payload["delivery_ref"] == delivery.entity_ref]
        activity_refs.update(r.entity_ref for r in history)
        current = [r.payload for r in history if r.payload["candidate_ref"] == d["candidate_ref"]]
        decisions = {r["decision"] for r in current if r["decision"] in {"approved", "changes_requested"}}
        if len(decisions) > 1:
            # A disputed review is still a reviewed delivery. Retain it in
            # the decision denominator rather than reporting a flattering
            # zero-decision subset and only exposing the conflict in status.
            conflicts += 1
            decided += 1
        elif len(decisions) == 1:
            decided += 1
            approved += "approved" in decisions
        else:
            pending += 1
        waived += any(r["decision"] in {"waived", "not_executed"} for r in current)
        first = {r.payload["first_pass"] for r in history if r.payload["first_pass"] is not None}
        if len(first) == 1:
            first_known += 1
            first_yes += True in first
        rs = [r.payload["rework_rounds"] for r in history if r.payload["rework_rounds"] is not None]
        cs = [r.payload["post_approval_corrections"] for r in history if r.payload["post_approval_corrections"] is not None]
        if rs:
            rounds.append(max(rs))  # snapshots are not additive review activities
        if cs:
            corrections.append(max(cs))
        for r in current:
            critical.extend(r["critical"])
        same_rubric: dict[str, set[str]] = defaultdict(set)
        for r in current:
            if r["score"] is not None:
                same_rubric[str(r["rubric"])].add(r["score"])
        for rubric, vals in same_rubric.items():
            if len(vals) == 1 and len(decisions) <= 1:
                scores[rubric].append(decimal(next(iter(vals)), "score"))
    count = len(deliveries)
    approved_ratio = _ratio(approved, decided, count)
    if conflicts:
        # Its denominator records that the delivery reached a decision review,
        # but an internally disputed decision has no reportable approval rate.
        approved_ratio.update(value=None, status="unavailable")
    return {"delivery_count": count, "review_activity_count": len(activity_refs),
            "approved": approved_ratio, "accepted": _ratio(accepted, delivered_decisions, count),
            "first_pass": _ratio(first_yes, first_known, count), "pending": pending,
            "waived_or_not_executed": waived, "conflicts": conflicts,
            "rework_rounds": sum(rounds) if rounds else None, "rework_known_deliveries": len(rounds),
            "post_approval_corrections": sum(corrections) if corrections else None,
            "critical": sorted(set(critical)), "scores": {
                rubric: {"count": len(vals), "mean": decimal_text(sum(vals) / len(vals)),
                         "values": [decimal_text(v) for v in sorted(vals)]}
                for rubric, vals in sorted(scores.items())}}


def human_summary(rows: list[Receipt], summaries: list[Receipt], *, calendar: bool) -> dict[str, Any]:
    inputs = [r for r in rows if r.payload["origin"] == "human"]
    bands = Counter(r.payload["band"] for r in inputs if r.payload["band"] is not None)
    known_hau = sum((BANDS[key] * n for key, n in bands.items()), Decimal(0))
    measured = [r.payload["focused_seconds"] for r in inputs if r.payload["focused_seconds"] is not None]
    # Summary-only sources cannot be turned into per-day/per-message facts. Do
    # not add summaries to events without explicit disjoint coverage evidence.
    summary_count = sum(r.payload["count"] or 0 for r in summaries)
    return {"interaction_count": len(inputs) if inputs else None,
            "event_coverage": "observed_subset" if inputs else "unavailable", "classification_unknown": len(inputs) - sum(bands.values()),
            "bands": dict(sorted(bands.items())), "hau_known": decimal_text(known_hau) if bands else None,
            "focused_seconds_known": sum(measured) if measured else None,
            "focused_seconds_unknown": len(inputs) - len(measured),
            "summary_only": [{"fact_ref": r.fact_ref, **r.payload} for r in summaries],
            "summary_interaction_count": summary_count if summaries else None,
            "summary_status": "time_unallocated" if calendar and summaries else "not_added_to_events" if summaries else "absent"}


def _benefits(rows: list[Receipt], deliveries: list[Receipt], reviews: list[Receipt],
              wis: set[str], q: CollaborationQuery) -> dict[str, Any]:
    valid, reasons = [], set()
    if not q.value_from:
        reasons.add("value_period_required")
    by_delivery = {r.entity_ref: r.payload for r in deliveries}
    for row in rows:
        p = row.payload
        d = by_delivery.get(p["delivery_ref"])
        if not d or d["status"] != "accepted" or p["artifact_ref"] != d["artifact_ref"]:
            reasons.add("unaccepted_value_artifact")
            continue
        review_decisions = {r.payload["decision"] for r in reviews if r.payload["delivery_ref"] == p["delivery_ref"]
                            and r.payload["candidate_ref"] == d["candidate_ref"]
                            and r.payload["decision"] in {"approved", "changes_requested"}}
        if review_decisions != {"approved"}:
            reasons.add("unverified_value_artifact")
            continue
        if p["unit"] != "USD" or p["realization"] != "observed" or p["amount"] is None:
            reasons.add("non_monetary_or_unobserved_value")
            continue
        if not p["period_start"] or not p["period_end"]:
            reasons.add("value_period_unknown")
            continue
        if q.value_from:
            a, b = parse_timestamp(p["period_start"], "start"), parse_timestamp(p["period_end"], "end")
            low, high = parse_timestamp(q.value_from, "value_from"), parse_timestamp(q.value_to, "value_to")
            if b <= low or a >= high:
                continue
            if a < low or b > high:
                reasons.add("value_time_unallocated")
                continue
        valid.append(row)
    effects: dict[str, list[Receipt]] = defaultdict(list)
    for r in valid:
        effects[r.payload["effect_ref"]].append(r)
    chosen = []
    for rows in effects.values():
        modes = {r.payload["mode"] for r in rows}
        ordered = sorted(rows, key=lambda r: (r.payload["period_end"], r.payload["period_start"]))
        if modes == {"cumulative"} and len({r.payload["period_start"] for r in rows}) == 1:
            end = ordered[-1].payload["period_end"]
            last = [r for r in rows if r.payload["period_end"] == end]
            if len({r.payload["amount"] for r in last}) != 1:
                reasons.add("overlapping_value_conflict")
                continue
            chosen.append(last[0])
        elif modes == {"increment"}:
            if any(parse_timestamp(a.payload["period_end"], "end") > parse_timestamp(b.payload["period_start"], "start")
                   for a, b in zip(ordered, ordered[1:])):
                reasons.add("overlapping_value_conflict")
            else:
                chosen.extend(ordered)
        elif len(rows) == 1:
            chosen.extend(rows)
        else:
            reasons.add("overlapping_value_conflict")
    if q.value_from and chosen:
        low, high = parse_timestamp(q.value_from, "value_from"), parse_timestamp(q.value_to, "value_to")
        for effect in {r.payload["effect_ref"] for r in chosen}:
            spans = sorted((parse_timestamp(r.payload["period_start"], "start"),
                            parse_timestamp(r.payload["period_end"], "end"))
                           for r in chosen if r.payload["effect_ref"] == effect)
            cursor = low
            for a, b in spans:
                if a > cursor:
                    reasons.add("incomplete_value_period")
                cursor = max(cursor, b)
            if cursor < high:
                reasons.add("incomplete_value_period")
    total = Decimal(0)
    valued_count = 0
    known_wis, periods = set(), set()
    for row in chosen:
        p = row.payload
        amount = decimal(p["amount"], "amount")
        if q.agent:
            allocation = [a for a in p["allocations"] if a["role"] == q.agent_role and a["agent"] == q.agent]
            if not allocation:
                reasons.add("allocation_unknown")
                continue
            amount *= decimal(allocation[0]["share"], "share")
        total += amount
        valued_count += 1
        if p["complete"]:
            known_wis.add(p["wi"])
        else:
            reasons.add("incomplete_value")
        periods.add((p["period_start"], p["period_end"]))
    if wis - known_wis:
        reasons.add("incomplete_value")
    return {"known_usd": decimal_text(total) if valued_count else None, "complete": not reasons and bool(wis),
            "reasons": sorted(reasons), "known_wi_count": len(known_wis),
            "periods": [list(p) for p in sorted(periods)], "fact_refs": sorted(r.fact_ref for r in chosen)}



def _source_diagnostics(rows: list[Receipt], selected_wis: set[str]) -> list[dict[str, Any]]:
    sources = {}
    priority = {"unreachable": 5, "partial": 4, "unknown": 3, "complete": 2, "idle": 1, "excluded": 0}
    for row in rows:
        if row.kind != "coverage":
            continue
        p = row.payload
        if not p.get("source_detail") or (p.get("wi") is not None and p["wi"] not in selected_wis):
            continue
        detail = dict(p["source_detail"], status=p["status"], reason=p["reason"], source_ref=p["source_ref"])
        old = sources.get(p["source_ref"])
        if old is None or priority[detail["status"]] > priority[old["status"]]:
            sources[p["source_ref"]] = detail
    return [sources[key] for key in sorted(sources)]


def _actor_projection(rows: list[Receipt], active: list[Receipt],
                      conflicts: dict[str, list[Receipt]], *, counter_residuals: set[str] | None = None,
                      observation_groups: Mapping[str, list[str]] | None = None,
                      actor_members: Mapping[str, tuple[str, ...]] | None = None) -> tuple[
        list[Receipt], dict[str, dict[str, str | None]], dict[str, list[str]], dict[str, list[str]]]:
    """Join recorded identity in a report; do not change task or usage facts.

    Run after quantity reconciliation. Context follows exact usage references
    or proven execution links. Unknown fields do not erase known fields.
    A folded actor head (see actor_fold) contributes all its group's fact refs.
    """
    folded = actor_members or {}
    by_target: dict[str, list[Receipt]] = defaultdict(list)
    for fact in active + [r for versions in conflicts.values() for r in versions]:
        if fact.kind == "attribution" and fact.payload.get("scope") == "actor":
            by_target[fact.payload["target_ref"]].append(fact)
    result, contexts, evidence, ambiguous = [], {}, {}, {}
    dispatched = {r.entity_ref: r.occurred_at for r in active
                  if r.kind == "execution" and r.occurred_at}
    for row in rows:
        p = row.payload
        boundary = (row.occurred_at if row.entity_ref in (counter_residuals or set())
                    else p.get("interval_start") or row.occurred_at)
        end = p.get("interval_end") or row.occurred_at
        choices: dict[str, set[str]] = {k: set() for k in ("executor", "node", "entrypoint")}
        if p.get("executor") is not None:
            choices["executor"].add(p["executor"])
        disputed, refs = set(), set()
        late_values = []
        subcall_choices = {}
        tenant_choices = {}
        for target in {row.entity_ref, p.get("execution_ref")} - {None}:
            for fact in by_target.get(target, []):
                a = fact.payload
                # A projected request merges its Host and native observations;
                # proof bound to any of them covers exactly that same request.
                versions = {row.fact_ref, *(observation_groups or {}).get(row.entity_ref, ())}
                if a.get('usage_fact_ref') is not None and a['usage_fact_ref'] not in versions:
                    continue  # Historical authority covers exactly this usage version.
                tenant = a['context'].get('tenant')
                if tenant is not None and tenant['usage_fact_ref'] not in versions:
                    # Membership establishes exactly the verified usage version.
                    # It cannot carry a name or actor identity into a later head;
                    # a new version needs a newly confirmed append-only proof.
                    continue
                effective = a["effective_at"]
                if boundary is None or effective is None:
                    continue
                started = parse_timestamp(boundary, "usage start")
                when = parse_timestamp(effective, "identity boundary")
                values = {"executor": a["executor"], "node": a["context"]["node"],
                          "entrypoint": a["context"]["entrypoint"]}
                # Identity the Host declared at its dispatch covers every request
                # of that execution; a native twin recorded a few milliseconds
                # earlier must not turn it into a mid-request transfer.
                declared = (target == p.get("execution_ref") and target in dispatched
                            and when == parse_timestamp(dispatched[target], "dispatch")
                            and end is not None and when <= parse_timestamp(end, "usage end"))
                if when > started and not declared:
                    # A native message's directly recorded location describes
                    # that message, not an executor transfer at Host dispatch.
                    # Keep transfer timing for roles/routes and inherited context.
                    if (target == row.entity_ref and row.kind == "usage"
                            and p["granularity"] == "request" and p["mapping"] == "cc_message.v1"
                            and p["runtime"] in {"cc", "cc_sdk"}
                            and fact.producer == a["policy"] == "registered_runtime_actor.v1"
                            and end and when <= parse_timestamp(end, "usage end")
                            and values["node"] is not None):
                        choices["node"].add(values["node"])
                        refs.update(folded.get(fact.fact_ref, (fact.fact_ref,)))
                    if end and when < parse_timestamp(end, "usage end"):
                        late_values.append(values)
                        refs.update(folded.get(fact.fact_ref, (fact.fact_ref,)))
                    continue
                refs.update(folded.get(fact.fact_ref, (fact.fact_ref,)))
                tenant = a["context"].get("tenant")
                if tenant is not None:
                    tenant_choices[digest([tenant, values])] = (tenant, values)
                subcall = a["context"].get("subcall")
                if subcall is not None:
                    if subcall["child_scope_ref"] != p.get("native_scope_ref") or subcall["child_runtime"] != p["runtime"]:
                        disputed.add("subcall")
                    else:
                        subcall_choices[digest(subcall)] = subcall
                for key, value in values.items():
                    if value is not None:
                        choices[key].add(value)
        # A later repetition of an already-proven identity is not a transfer.
        # Missing early identity and any different late value remain ambiguous.
        for values in late_values:
            disputed.update(k for k, value in values.items()
                            if value is not None and choices[k] != {value})
        disputed.update(k for k, values in choices.items() if len(values) > 1)
        selected = {k: ("ambiguous" if k in disputed else next(iter(values)) if values else None)
                    for k, values in choices.items()}
        if selected["executor"] != p.get("executor"):
            p["executor"] = selected["executor"]
            row = replace(row, _payload_json=canonical(p))
        contexts[row.entity_ref] = {k: selected[k] for k in ("node", "entrypoint")}
        if tenant_choices:
            members = list(tenant_choices.values())
            identity_proven = (selected['executor'] not in {None, 'ambiguous'}
                               and selected['node'] not in {None, 'ambiguous'}
                               and all(values['executor'] == selected['executor']
                                       and values['node'] == selected['node']
                                       for _, values in members))
            names = {(t['id'], t['name']) for t, _ in members}
            tenant_id, name = (next(iter(names)) if identity_proven and len(names) == 1
                               else ('ambiguous', None))
            contexts[row.entity_ref]['tenant'] = {'id': tenant_id, 'name': name,
                'binding_refs': sorted({t['binding_ref'] for t, _ in members})}
        if len(subcall_choices) > 1:
            disputed.add("subcall")
        elif subcall_choices and "subcall" not in disputed:
            contexts[row.entity_ref]["subcall"] = next(iter(subcall_choices.values()))
        if refs:
            evidence[row.entity_ref] = sorted(refs)
        if disputed:
            ambiguous[row.entity_ref] = sorted(disputed)
        result.append(row)
    return result, contexts, evidence, ambiguous


def _qualified_collector_sources(facts: list[Receipt]) -> list[Receipt]:
    """Read-side repair of legacy node-local source IDs in a central ledger.

    Distinct, retained node-qualified source labels identify distinct physical
    sources. Same-label competing assertions remain conflicts. No stored fact,
    supersession edge or historical watermark is rewritten. A retired entity
    keeps its stored identity: its retirement is the explicit successor of all
    labels' heads, and splitting by label would separate it from them.
    """
    labels: dict[str, set[str]] = defaultdict(set)
    retired = set()
    for row in facts:
        if row.kind == "coverage" and row.producer == "registered_collector.v1":
            detail = row.payload.get("source_detail") or {}
            label = detail.get("label", "")
            if "." in label and detail.get("kind") == "harness_root":
                labels[row.entity_ref].add(label)
        elif is_source_retirement(row):
            retired.add(row.entity_ref)
    collisions = {key for key, values in labels.items() if len(values) > 1} - retired
    result = []
    for row in facts:
        if row.entity_ref in collisions and isinstance(row.payload.get("source_detail"), Mapping):
            p = row.payload
            label = p["source_detail"]["label"]
            key = reference("qualified_collector_source", [p["source_ref"], label])
            row = replace(row, entity_ref=reference("coverage", [key, p["domain"], p.get("wi")]),
                          _payload_json=canonical({**p, "source_ref": key}))
        result.append(row)
    return result


def _non_wi_key(key: object) -> str | None:
    """The non-WI class of a work key: a scheduler job ("cron:<job>") is cron."""
    if key in {"cron", "none"}:
        return key
    return "cron" if isinstance(key, str) and key.startswith("cron:") else None


def _compatible_task_attribution(rows: list[Receipt]) -> Receipt | None:
    """Collapse independent task evidence only when every material claim agrees.

    Multiple producers can observe the same SCL boundary (for example Host and
    native binding). Their mere multiplicity is not a conflict. Null optional
    links are incomplete evidence and may be filled by an agreeing source; two
    different known values, states, work kinds, or effective boundaries remain
    a conflict. This is a read-side projection and never rewrites the ledger.
    """
    if not rows:
        return None
    payloads = [r.payload for r in rows]
    fields = ("wi", "executor", "responsible", "delegator", "execution_ref",
              "assignment_ref", "work_kind")
    merged: dict[str, Any] = {}
    for field in fields:
        known = {p.get(field) for p in payloads if p.get(field) is not None}
        if len(known) > 1:
            return None
        merged[field] = next(iter(known)) if known else None
    states = {p.get("state") for p in payloads}
    if len(states) != 1:
        return None
    merged["state"] = next(iter(states))
    # One boundary may be written in different text by different sources.
    try:
        instants = {parse_timestamp(p["effective_at"], "effective_at") if p.get("effective_at") else None
                    for p in payloads}
    except (ReceiptError, TypeError, ValueError):
        return None
    if len(instants) != 1:
        return None
    merged["effective_at"] = min((p.get("effective_at") for p in payloads), key=lambda t: (t is None, t or ""))
    # An explicit ambiguous/unassigned state is itself a material result; do not
    # upgrade it merely because another producer happened to name a WI.
    if merged["state"] != "assigned" and any(p.get("wi") is not None for p in payloads):
        return None
    # work_kind is mutually exclusive with a canonical WI by the wire contract.
    if merged["work_kind"] is not None and merged["wi"] is not None:
        return None
    base = min(rows, key=lambda r: r.fact_ref)
    projected = dict(base.payload)
    for field, value in merged.items():
        if field in projected or value is not None:
            projected[field] = value
    # Policy records provenance, not assignment semantics. Keep all source fact
    # refs as evidence instead of pretending one producer authored the merger.
    return replace(base, observed_at=max(r.observed_at for r in rows),
                   evidence=tuple(sorted(set(base.evidence) | {r.fact_ref for r in rows})),
                   _payload_json=canonical(projected))


def _supplement_rank(row: Receipt) -> int:
    p = row.payload
    if p["policy"] == SCHEDULED_JOB_POLICY:
        return 0 if p.get("wi") is not None else 2
    if p["policy"] == EXPLORATION_POLICY:
        return 4
    if HOST_FALLBACK in row.evidence:
        return 5
    return 1 if p["state"] == "assigned" else 3


def select_supplement(candidates: list[Receipt], boundary: str | None) -> Receipt | None:
    """The applicable supplement for one usage, or None.

    Only supplements effective at or before the usage boundary qualify. The
    best rank wins: an explicit job WI, a single held WI, the job itself,
    several held WIs. Disagreeing supplements of that rank decide nothing.
    """
    if not boundary:
        return None
    at = parse_timestamp(boundary, "usage boundary")
    # A withdrawn supplement ("unassigned") decides nothing.
    ready = [r for r in candidates if r.payload.get("effective_at") and r.payload.get("state") != "unassigned"
             and parse_timestamp(r.payload["effective_at"], "effective_at") <= at]
    if not ready:
        return None
    best = min(_supplement_rank(r) for r in ready)
    return _compatible_task_attribution([r for r in ready if _supplement_rank(r) == best])


def _prepare_collaboration(snapshot: dict[str, Any], agent_tenants: Mapping[str, Any] | None = None, *,
                           fold_actors: bool | None = None) -> tuple:
    """One read-side reconciliation, independent of period, grouping and price.

    Query sessions own reuse. Individual queries, new watermarks and new
    publications retain their own preparation. `agent_tenants` (see
    `agent_tenants.py`) completes the tenant of rows whose Agent is known.
    `fold_actors` (default: environment, on) reads equal actor heads as one
    representative; the report is byte-identical either way.
    """
    snap = snapshot
    from .native_inputs import reportable
    active, conflicts = current_receipts(_qualified_collector_sources(reportable(snap["facts"])))
    actor_members: dict[str, tuple[str, ...]] = {}
    if actor_fold_enabled(fold_actors):
        active, actor_members = fold_actor_heads(active, (r for rs in conflicts.values() for r in rs))
    native_rows = [r for r in [*active, *(v for vs in conflicts.values() for v in vs)]
                   if r.kind == "usage" and _native_cc_observed(r)]
    from .cc_session_counters import supported_scopes
    cumulative_scopes = supported_scopes(native_rows)
    native_observed = {r.entity_ref for r in native_rows}
    native_settled = {r.entity_ref for r in native_rows if r.payload["final"]}
    del native_rows
    active, conflicts, observation_groups, observation_attribution_conflicts = _request_observation_projection(active, conflicts)
    # Only unresolved quantity forks veto a session-counter reconciliation;
    # compatible Host/native observations were joined immediately above.
    rejected_cc_scopes = {r.payload.get("native_scope_ref") for versions in conflicts.values() for r in versions
        if r.kind == "usage" and r.payload["runtime"] in {"cc", "cc_sdk"}}
    from .native_inventory import resolved_session_inferences
    resolved_inferences = resolved_session_inferences(active)
    from .pi_binding_alias import assignment_equivalences
    assignment_aliases = assignment_equivalences(active)
    attr_groups: dict[str, list[Receipt]] = defaultdict(list)
    # Supplements (holder writes, scheduler jobs) never join a declaration's
    # group: they apply only where no primary claim exists.
    supplement_groups: dict[str, list[Receipt]] = defaultdict(list)
    task_rows = list(active) + [r for versions in conflicts.values() for r in versions]
    usage_versions = defaultdict(set)
    for row in task_rows:
        if row.kind == 'usage':
            usage_versions[row.entity_ref].add(row.fact_ref)
    for row in task_rows:
        if (row.kind == 'attribution' and row.payload.get('usage_fact_ref') is not None
                and row.payload['usage_fact_ref'] not in usage_versions.get(row.payload['target_ref'], set())):
            continue
        if row.kind == "attribution" and row.payload.get("scope") != "actor" and row.fact_ref not in resolved_inferences:
            if supplement_source(row) is not None:
                supplement_groups[row.payload["target_ref"]].append(row)
            else:
                attr_groups[row.payload["target_ref"]].append(row)
    attrs: dict[str, Receipt] = {}
    attr_conflicts: set[str] = set()
    for target, rows in attr_groups.items():
        merged = _compatible_task_attribution(rows)
        if merged is None:
            attr_conflicts.add(target)
        else:
            attrs[target] = merged
    declared_none_targets = {target for target, rows in attr_groups.items()
                             if target in attrs and attrs[target].payload.get("work_kind") == "none"
                             and all(row.producer in PURPOSE_DECLARATION_PRODUCERS for row in rows)}
    # A normal attribution conflict can be attached to a usage/execution
    # target, so propagate every candidate WI rather than only marking the
    # opaque target reference.  This also makes an explicit ambiguous state
    # block every potentially affected cohort.
    by_entity = {r.entity_ref: r for r in active}
    attribution_conflict_wis: set[str] = set()
    for target in attr_conflicts:
        target_row = by_entity.get(target)
        if target_row and target_row.payload.get("wi"):
            attribution_conflict_wis.add(target_row.payload["wi"])
        for row in attr_groups.get(target, []):
            if row.payload.get("wi"):
                attribution_conflict_wis.add(row.payload["wi"])
    for rows in conflicts.values():
        claims = [r for r in rows if r.kind == "attribution"]
        if claims and all(r.payload.get("scope") != "actor" for r in claims) and _compatible_task_attribution(claims):
            continue  # Concurrent heads asserting the same assignment are no dispute.
        for row in claims:
            if row.payload.get("wi"):
                attribution_conflict_wis.add(row.payload["wi"])
    projected = []
    non_wi_kinds: dict[str, str] = {}
    # Usage whose WI came from a supplement, by source; job names by job ID.
    attribution_sources: dict[str, Any] = {"sources": {}, "jobs": {}}
    chosen_supplements: dict[str, Receipt] = {}
    # Usage whose applied claim is only a declared `none` (no WI declared),
    # with every applicable WI-naming supplement (holder or scheduled job).
    declared_none: set[str] = set()
    none_claims: dict[str, list[Receipt]] = {}
    for r in active:
        p = r.payload
        original_payload = p
        a = attrs.get(r.entity_ref) or attrs.get(p.get("execution_ref"))
        explicit_assignment_applied = False
        boundary = p.get("interval_start") or r.occurred_at
        applies = bool(a and boundary and a.payload["effective_at"]
                       and parse_timestamp(a.payload["effective_at"], "effective_at") <= parse_timestamp(boundary, "occurred_at"))
        none_declared = (applies and r.kind == "usage" and p.get("wi") is None
                         and r.entity_ref in declared_none_targets and attrs.get(r.entity_ref) is a)
        undeclared = a is None or none_declared or (a.payload["state"] == "unassigned" and a.payload.get("wi") is None
                                                    and a.payload.get("work_kind") is None)
        if (r.kind == "usage" and undeclared and p.get("wi") is None
                and r.entity_ref not in attr_conflicts and p.get("execution_ref") not in attr_conflicts):
            supplement = select_supplement(
                supplement_groups.get(r.entity_ref, []) + supplement_groups.get(p.get("native_scope_ref"), []),
                p.get("interval_start") or r.occurred_at)
            if supplement is not None:
                chosen_supplements[r.entity_ref] = supplement
            if none_declared:
                at = parse_timestamp(boundary, "usage boundary")
                none_claims[r.entity_ref] = [
                    c for c in supplement_groups.get(r.entity_ref, []) + supplement_groups.get(p.get("native_scope_ref"), [])
                    if supplement_source(c) in {"holder_supplement", "scheduled_job"}
                    and c.payload.get("work_kind") is None and c.payload.get("state") != "unassigned"
                    and c.payload.get("effective_at") and parse_timestamp(c.payload["effective_at"], "effective_at") <= at]
        if a:
            ap = a.payload
            if applies:
                explicit_assignment_applied = True
                if none_declared:
                    declared_none.add(r.entity_ref)
                p = dict(p)
                if ap.get("work_kind") in {"cron", "none"}:
                    if p.get("wi") is not None:
                        # A non-WI label cannot erase an existing task claim.
                        attr_conflicts.add(r.entity_ref)
                    else:
                        # A scheduler job without a WI is named by its job.
                        non_wi_kinds[r.entity_ref] = ("cron:" + ap["job"]["id"] if ap.get("job")
                                                      else ap["work_kind"])
                else:
                    for key in ("wi", "executor", "responsible", "delegator"):
                        # A purpose-only declaration does not erase a known
                        # Host actor. Null is absence of identity, not revocation.
                        if key in p and (key == "wi" or ap[key] is not None):
                            p[key] = ap[key]
                if r.kind == "usage":
                    for field in ("execution_ref", "assignment_ref"):
                        if ap.get(field) is not None:
                            p[field] = ap[field]
                if ap["state"] in {"ambiguous", "unassigned", "excluded"}:
                    p["wi"] = ap["state"] if ap["state"] != "unassigned" else None
        if r.entity_ref in observation_attribution_conflicts:
            if explicit_assignment_applied and r.entity_ref not in attr_conflicts:
                # A later explicit attribution can resolve differing source
                # metadata, but it cannot resolve inconsistent Token facts.
                del observation_attribution_conflicts[r.entity_ref]
            else:
                attr_conflicts.add(r.entity_ref)
        # Conflicting or mid-aggregate assignment is not a new exact attribution.
        target_conflict = r.entity_ref in attr_conflicts or p.get("execution_ref") in attr_conflicts
        interval_crosses = bool(a and p.get("interval_start") and a.payload["effective_at"]
            and parse_timestamp(p["interval_start"], "start") < parse_timestamp(a.payload["effective_at"], "effective")
            < parse_timestamp(p["interval_end"], "end"))
        if (target_conflict or interval_crosses) and r.kind in {"usage", "execution", "human"}:
            p = {**p, "wi": "ambiguous", "responsible": None, "delegator": None}
        # Encode one projection at a time. Retaining every decoded payload
        # alongside the immutable facts and projections doubles the peak.
        # Projections carry the original fact ref and are never persisted.
        projected.append(r if p is original_payload else
                         Receipt(r.kind, r.entity_ref, r.fact_ref, r.occurred_at, r.observed_at,
                                 r.producer, r.evidence, r.supersedes, canonical(p)))
    # A recovered receipt transport withdraws only its former transport gap.
    # It does not assert a complete business denominator or exclude consumption.
    projected = [r for r in projected if not (r.kind == "coverage"
        and r.producer == "registered_collector.v1" and r.payload["status"] == "excluded"
        and r.payload["reason"] == "transport_recovered_coverage_delegated")]
    from .stage_facts import project_membership
    projected, stage_membership = project_membership(projected, conflicts)
    attr_conflicts.update(stage_membership["conflicts"])
    for candidates in stage_membership["conflicts"].values():
        attribution_conflict_wis.update(candidates)
    # A declared `none` is the absence of a WI: a membership naming one wins,
    # unless the session's own WI write disagrees with it.
    for row in projected:
        if row.entity_ref in declared_none and row.kind == "usage" and row.payload.get("wi") not in {None, "ambiguous"}:
            claims = none_claims.get(row.entity_ref, [])
            if any(c.payload["state"] != "assigned" or c.payload.get("wi") != row.payload["wi"] for c in claims):
                attr_conflicts.add(row.entity_ref)
                attribution_conflict_wis.update({row.payload["wi"]} | {c.payload["wi"] for c in claims if c.payload.get("wi")})
                continue
            non_wi_kinds.pop(row.entity_ref, None)
    all_wis = {r.payload.get("wi") for r in projected
               if r.kind != "stage_link" and r.payload.get("wi")}
    accepted_at: dict[str, str] = {}
    for row in projected:
        if row.kind == "delivery" and row.payload["status"] == "accepted" and row.payload["accepted_at"]:
            wi, timestamp = row.payload["wi"], row.payload["accepted_at"]
            if wi and (wi not in accepted_at or parse_timestamp(timestamp, "accepted_at") < parse_timestamp(accepted_at[wi], "accepted_at")):
                accepted_at[wi] = timestamp
    from .host_native_input import authenticated_input_links
    input_links, finals = authenticated_input_links(active)
    # Explicit, resolved exclusions apply to observations, not merely to the
    # final WI bucket. Otherwise a reviewed non-observation can still quarantine
    # real usage during overlap reconciliation even though it is not displayed.
    # Keep the original facts and projected evidence; unknown/disputed labels
    # never enter this set and independent quantity conflicts remain unchanged.
    excluded_usage = {r.entity_ref for r in projected
                      if r.kind == "usage" and r.payload.get("wi") == "excluded"}
    all_reconciled, reconciliation = reconcile_usage([r for r in projected if r.kind == "usage" and r.entity_ref not in excluded_usage], executions=[r for r in projected if r.kind == "execution"], inventories=[r for r in projected if r.kind == "coverage" and r.producer in {"pi_native_inventory.v1", "pi_parent_history.v1"}], assignment_aliases=assignment_aliases, input_links=input_links, final_native_refs=finals, native_settled_refs=native_settled, native_observed_refs=native_observed, cumulative_scopes=cumulative_scopes, rejected_cc_scopes=rejected_cc_scopes)
    reconciliation["stage_membership"] = stage_membership
    reconciliation["excluded_observation_refs"] = sorted(excluded_usage)
    reconciliation["observation_groups"] = observation_groups
    reconciliation["resolved_session_attribution_refs"] = sorted(resolved_inferences)
    residual_refs = {ref for key in ('cc_session_counters', 'cc_unitemized_counters')
                     for p in reconciliation[key] for ref in p['residual_entity_refs']}
    identified, actor_context, actor_evidence, actor_conflicts = _actor_projection(
        all_reconciled + [r for r in projected if r.kind == "execution"], active, conflicts,
        counter_residuals=residual_refs, observation_groups=observation_groups, actor_members=actor_members)
    for proof in reconciliation['cc_session_counters']:
        if not proof['residual_entity_refs']:
            continue
        native_family = [r for r in identified if r.kind == 'usage' and r.entity_ref in native_observed
            and r.payload.get('native_scope_ref') == proof['native_scope_ref'] and r.payload['model'] == proof['model']
            and parse_timestamp(r.payload.get('interval_end') or r.occurred_at, 'native') <= parse_timestamp(proof['latest_at'], 'counter')]
        residual_rows = {r.entity_ref: r for r in identified if r.entity_ref in proof['residual_entity_refs']}

        def interval_family(ref):
            """The itemized requests inside this residual's own counter interval.

            Each residual is the increase between two consecutive counters, so
            only requests of that interval spent it; earlier requests of the
            session (another channel, another trigger) are not its evidence.
            Without any such request the whole family still decides.
            """
            row = residual_rows.get(ref)
            low = row and (row.payload.get('interval_start') or row.occurred_at)
            high = row and (row.payload.get('interval_end') or row.occurred_at)
            if not low or not high:
                return native_family
            low, high = parse_timestamp(low, 'residual start'), parse_timestamp(high, 'residual end')
            inside = [r for r in native_family
                      if low <= parse_timestamp(r.payload.get('interval_start') or r.occurred_at, 'native')
                      and parse_timestamp(r.payload.get('interval_end') or r.occurred_at, 'native') <= high]
            return inside or native_family

        for ref, field in ((ref, field) for ref in proof['residual_entity_refs'] for field in ('node', 'entrypoint')):
            family = interval_family(ref)
            family_evidence = {w for r in family for w in actor_evidence.get(r.entity_ref, [])}
            values = {actor_context.get(r.entity_ref, {}).get(field) for r in family}
            own = actor_context.get(ref, {}).get(field)
            if own is None and values <= {None}:
                continue  # Nothing proved either way: unknown, not disputed.
            # The residual's own dispatch proof stands unless an itemized
            # request proves a different value; an unproved one is no contrary
            # evidence. Without its own proof, every request must agree.
            agreed = (own if own != 'ambiguous' and values - {None} <= {own} else
                      next(iter(values)) if own is None and len(values) == 1 else None)
            if agreed not in {None, 'ambiguous'} and own in {None, agreed}:
                actor_context.setdefault(ref, {})[field] = agreed
                actor_evidence[ref] = sorted(set(actor_evidence.get(ref, [])) | family_evidence)
            else:
                actor_context.setdefault(ref, {})[field] = 'ambiguous' if own or any(values) else None
                actor_conflicts.setdefault(ref, []).append(field)
    # A session Relay's scheduler started has no human ingress of its own;
    # a request in it without a proven channel was the scheduler's.
    for row in identified:
        context = actor_context.setdefault(row.entity_ref, {})
        boundary = row.payload.get("interval_start") or row.occurred_at
        if (row.kind == "usage" and context.get("entrypoint") is None and boundary
                and any(r.payload["policy"] == SCHEDULED_JOB_POLICY and r.payload["effective_at"]
                        and parse_timestamp(r.payload["effective_at"], "scheduled run") <= parse_timestamp(boundary, "usage")
                        for r in supplement_groups.get(row.payload.get("native_scope_ref"), ()))):
            context["entrypoint"] = "automation"
    if agent_tenants:
        from .agent_tenants import apply_agent_tenants
        apply_agent_tenants(identified, actor_context, agent_tenants)
    identified_by_ref = {r.entity_ref: r for r in identified}
    # Supplements apply only after reconciliation: Host/native joins compare
    # the original claims, never a supplemented WI.
    # Disagreeing WI writes never let a declared `none` pass for one of them.
    for ref, claims in none_claims.items():
        row = identified_by_ref.get(ref)
        if (row is not None and row.payload.get("wi") is None and ref not in attr_conflicts
                and (any(c.payload["state"] != "assigned" for c in claims)
                     or len({c.payload.get("wi") for c in claims}) > 1)):
            attr_conflicts.add(ref)
            attribution_conflict_wis.update(c.payload["wi"] for c in claims if c.payload.get("wi"))
    for ref, supplement in chosen_supplements.items():
        row = identified_by_ref.get(ref)
        if row is None or row.kind != "usage" or row.payload.get("wi") is not None or ref in attr_conflicts:
            continue
        ap = supplement.payload
        if ref in declared_none:
            # Only the session's own WI writes outweigh its declared `none`,
            # even when a job write outranks them; exploration or non-WI
            # supplements leave the declaration.
            held = next((c for c in none_claims.get(ref, []) if supplement_source(c) == "holder_supplement"), None)
            if held is None:
                continue
            supplement, ap = held, held.payload
            non_wi_kinds.pop(ref, None)
        if ap.get("work_kind") == "cron":
            non_wi_kinds[ref] = "cron:" + ap["job"]["id"]
        elif ap.get("work_kind") == "none":
            non_wi_kinds[ref] = "none"
        else:
            wi = "ambiguous" if ap["state"] == "ambiguous" else ap["wi"]
            identified_by_ref[ref] = replace(row, _payload_json=canonical({**row.payload, "wi": wi}))
            all_wis.add(wi)
        attribution_sources["sources"][ref] = supplement_source(supplement)
        if ap.get("job"):
            attribution_sources["jobs"][ap["job"]["id"]] = ap["job"]["name"]
    all_reconciled = [identified_by_ref[r.entity_ref] for r in all_reconciled]
    projected = [identified_by_ref.get(r.entity_ref, r) for r in projected]
    # Rejected quantity evidence retains its identity for cohort diagnostics.
    # Like reconciliation, this belongs to the immutable query snapshot rather
    # than attributes installed by a dashboard caller on the ledger.
    rejected_refs = set(reconciliation['conflicting_refs'])
    rejected = [r for versions in conflicts.values() for r in versions
                if r.kind in {'usage', 'execution'}]
    rejected += [r for r in projected if r.kind == 'usage'
                 and r.entity_ref in rejected_refs]
    diagnostic_rows, diagnostic_context, _, _ = _actor_projection(rejected, active, conflicts,
                                                                  actor_members=actor_members)
    diagnostic_by_fact = {r.fact_ref: r for r in diagnostic_rows}
    task_evidence: dict[str, list[Receipt]] = defaultdict(list)
    for row in active + [r for versions in conflicts.values() for r in versions]:
        if row.kind == 'attribution' and row.payload.get('scope') != 'actor':
            task_evidence[row.payload['target_ref']].append(row)
    return (active, conflicts, observation_groups, observation_attribution_conflicts, attr_conflicts, attribution_conflict_wis, non_wi_kinds, projected, all_wis, accepted_at, all_reconciled, reconciliation, actor_context, actor_evidence, actor_conflicts, diagnostic_by_fact, diagnostic_context, task_evidence, attribution_sources)


def _copy_preparation(prepared: tuple) -> tuple:
    from copy import deepcopy
    # These domain facts are frozen dataclasses of strings/bytes/tuples; only
    # the surrounding report containers need isolation between sibling views.
    facts = list(prepared[0]) + list(prepared[7]) + list(prepared[10])
    facts.extend(r for versions in prepared[1].values() for r in versions)
    facts.extend(prepared[15].values())
    return deepcopy(prepared, {id(row): row for row in facts})


class _PayloadReads:
    """Read-only, query-local decoded views after all fact projection is done."""
    MAX_ROWS = 4096
    MAX_ENCODED_BYTES = 8 * 1024 * 1024

    def __init__(self):
        self._rows: OrderedDict[int, tuple[Receipt, dict[str, Any]]] = OrderedDict()
        self._bytes = 0

    def read(self, row: Receipt) -> Mapping[str, Any]:
        key = id(row)
        entry = self._rows.get(key)
        if entry is None:
            entry = (row, row.payload)
            size = len(row._payload_json)
            if size <= self.MAX_ENCODED_BYTES:
                self._rows[key] = entry  # Keep the object alive; no reused id alias.
                self._bytes += size
                while len(self._rows) > self.MAX_ROWS or self._bytes > self.MAX_ENCODED_BYTES:
                    _, (evicted, _) = self._rows.popitem(last=False)
                    self._bytes -= len(evicted._payload_json)
        else:
            self._rows.move_to_end(key)
        return entry[1]


class CollaborationQuerySession:
    """Own one immutable evidence snapshot and its reusable calculation state.

    Callers select views, never install cache fields on a ledger. A session
    cannot silently cross watermarks; a new snapshot requires a new session.
    Returned reports and exported price entries are owned by their caller.
    """
    def __init__(self, snapshot: Mapping[str, Any], *, prices: Mapping | None = None,
                 agent_tenants: Mapping[str, Any] | None = None, fold_actors: bool | None = None):
        from copy import deepcopy
        self._snapshot = {**snapshot, "facts": tuple(snapshot["facts"])}
        self._prepared = None
        self._fold_actors = actor_fold_enabled(fold_actors)
        self._prices = deepcopy(dict(prices)) if prices is not None else {}
        self._agent_tenants = deepcopy(dict(agent_tenants)) if agent_tenants else None

    def query(self, q: CollaborationQuery, *, catalog: Mapping[str, Any] | None = None,
              catalog_observed_at: str | None = None) -> dict[str, Any]:
        if q.as_of is not None or q.watermark != self._snapshot["watermark"]:
            raise ValueError("query escaped session watermark")
        if self._prepared is None:
            self._prepared = _prepare_collaboration(self._snapshot, self._agent_tenants,
                                                    fold_actors=self._fold_actors)
        return _query_snapshot(self._snapshot, q, _copy_preparation(self._prepared),
                               catalog=catalog, catalog_observed_at=catalog_observed_at,
                               price_cache=self._prices)

    def export_prices(self, *, limit: int = 50000) -> dict:
        from copy import deepcopy
        if limit < 0:
            raise ValueError("price export limit must be nonnegative")
        return deepcopy(dict(list(self._prices.items())[-limit:])) if limit else {}


def query_collaboration(ledger: UsageLedger, q: CollaborationQuery, *, catalog: Mapping[str, Any] | None = None,
                        catalog_observed_at: str | None = None, fold_actors: bool | None = None) -> dict[str, Any]:
    """One fresh ledger read. Use a session explicitly for sibling frozen views."""
    snapshot = ledger.receipt_snapshot(as_of=q.as_of, watermark=q.watermark)
    return _query_snapshot(snapshot, q, _prepare_collaboration(snapshot, fold_actors=fold_actors),
                           catalog=catalog, catalog_observed_at=catalog_observed_at)


def _query_snapshot(snap: Mapping[str, Any], q: CollaborationQuery, prepared: tuple, *,
                    catalog: Mapping[str, Any] | None,
                    catalog_observed_at: str | None, price_cache: dict | None = None) -> dict[str, Any]:
    (active, conflicts, observation_groups, observation_attribution_conflicts, attr_conflicts, attribution_conflict_wis, non_wi_kinds, projected, all_wis, accepted_at, all_reconciled, reconciliation, actor_context, actor_evidence, actor_conflicts, diagnostic_by_fact, diagnostic_context, task_evidence, attribution_sources) = prepared
    stage_rows, stage_conflicts = projected, conflicts
    if q.context_ref is not None:
        from .stage_facts import context_selection
        selected_refs, related_refs = context_selection(projected, conflicts,
            reconciliation["stage_membership"], q.context_ref)
        projected = [r for r in projected if r.entity_ref in selected_refs]
        all_reconciled = [r for r in all_reconciled if r.entity_ref in selected_refs]
        conflicts = {ref: rows for ref, rows in conflicts.items() if ref in related_refs}
        attr_conflicts = attr_conflicts & related_refs
        observation_attribution_conflicts = {ref: values for ref, values in observation_attribution_conflicts.items()
                                             if ref in related_refs}
        all_wis = {r.payload.get("wi") for r in projected if r.kind != "stage_link" and r.payload.get("wi")}
        attribution_conflict_wis &= all_wis
        actor_conflicts = {ref: value for ref, value in actor_conflicts.items() if ref in related_refs}
        actor_evidence = {ref: value for ref, value in actor_evidence.items() if ref in related_refs}
        reconciliation = {**reconciliation, "context_selection": {
            "context_ref": q.context_ref, "scope": "exact_links_after_global_reconciliation",
            "selected_refs": sorted(selected_refs),
            "unavailable_usage_refs": sorted({r.entity_ref for r in projected if r.kind == "usage"}
                                             - {r.entity_ref for r in all_reconciled})}}
    # Projection is finished. Grouping/reporting below only READ these maps.
    read_payload = _PayloadReads().read
    window = q.window
    period_cache: dict[tuple[int, tuple[datetime, datetime] | None], str] = {}
    def period_for(row: Receipt, selected_window: tuple[datetime, datetime] | None) -> str:
        cache_key = (id(row), selected_window)
        value = period_cache.get(cache_key)
        if value is None:
            value = _period(row, selected_window, read_payload(row))
            period_cache[cache_key] = value
        return value
    wis = set(q.work_items) if q.work_items else all_wis - {"excluded", "ambiguous"}
    if q.scope == "accepted_wi":
        wis &= {wi for wi, timestamp in accepted_at.items() if _in_window(timestamp, window)}
    # Attribution is already projected and separately retained for diagnostics;
    # it is not another reportable event to rescan for each period/group.
    scoped = [r for r in projected if r.kind not in {"attribution", "stage_link"} and read_payload(r).get("wi") != "excluded"
              and ((not q.work_items and q.scope != "accepted_wi") or read_payload(r).get("wi") in wis)]
    def context_matches(row: Receipt, query: CollaborationQuery) -> bool:
        context = actor_context.get(row.entity_ref, {})
        return ((query.node is None or context.get("node") == query.node)
                and (query.entrypoint is None or context.get("entrypoint") == query.entrypoint)
                and (query.tenant is None or ((context.get("tenant") or {}).get("id") or UNKNOWN) == query.tenant))
    # Native identity may only become provable during Host/native reconciliation.
    # Apply WI filters to those reconciled identities, not the earlier null rows.
    usage_all = [r for r in all_reconciled if ((not q.work_items and q.scope != "accepted_wi") or read_payload(r).get("wi") in wis)
                 and read_payload(r)["wi"] != "excluded"
                 and (not q.agent or read_payload(r).get(q.agent_role) == q.agent)
                 and (not q.runtime or read_payload(r)["runtime"] == q.runtime)
                 and (not q.model or read_payload(r)["model"] == q.model)
                 and context_matches(r, q)]
    if q.scope == "activity":
        from .native_metering import metered_window
        usage_all = [selected for row in usage_all if (selected := metered_window(row, window)) is not None]
    usage = usage_all
    usage = [r for r in usage if q.scope != "activity" or period_for(r, window) != "outside"]
    exact_time = [r for r in usage if q.scope != "activity" or period_for(r, window) == "inside"]
    exact_time_refs = {r.entity_ref for r in exact_time}
    unallocated = [r for r in usage if r.entity_ref not in exact_time_refs]

    def work_key(row: Receipt) -> str:
        p = read_payload(row)
        kinds = {non_wi_kinds[k] for k in (row.entity_ref, p.get("execution_ref"))
                 if k in non_wi_kinds}
        if row.entity_ref in attr_conflicts or p.get("execution_ref") in attr_conflicts:
            return "ambiguous"
        if len(kinds) > 1 or (kinds and p.get("wi") is not None):
            return "ambiguous"
        return p.get("wi") or (next(iter(kinds)) if kinds else UNKNOWN)

    # Catalog identity is immutable for this query. Check uniqueness once per
    # actual model, not once per request and again in every group. No cache is
    # shared across queries or pricing versions.
    model_cache: dict = {}
    catalog_key = digest(catalog) if catalog else None

    def cost_for(row: Receipt) -> tuple[Decimal | None, str | None]:
        if price_cache is None:
            return _value_cost(row, catalog, model_cache)
        p = read_payload(row)
        key = digest([catalog_key, p["model"], p["tokens"]])
        cached = price_cache.get(key)
        if cached is None:
            amount, reason = _value_cost(row, catalog, model_cache)
            cached = [decimal_text(amount) if amount is not None else None, reason]
            price_cache[key] = cached
        return (Decimal(cached[0]) if cached[0] is not None else None, cached[1])

    rejected_refs = set(reconciliation['conflicting_refs'])
    rejected_reasons = reconciliation.get('conflict_reasons', {})

    def conflict_reason_counts(rejected: set[str], revised: set[str]) -> dict[str, int]:
        # Why each excluded usage is a conflict; one usage may have several.
        found = Counter(reason for ref in rejected for reason in rejected_reasons.get(ref, ()))
        found["revision_conflict"] += len(revised)
        return {reason: n for reason, n in sorted(found.items()) if n}

    scoped_kinds: dict[str, list[Receipt]] = defaultdict(list)
    for row in scoped:
        scoped_kinds[row.kind].append(row)
    # Relay records an execution before any request declares its WI, so the
    # execution's own WI is usually empty. Its purpose is that of the usage it
    # produced; only an execution without any usage keeps its own WI. A turn
    # aggregate names its execution; the native messages it covers carry the
    # declared WI and replace it in totals, so they stand for it here too.
    usage_by_ref = {row.entity_ref: row for row in scoped_kinds["usage"]}
    usage_by_execution: dict[str, list[Receipt]] = defaultdict(list)
    for row in scoped_kinds["usage"]:
        p = read_payload(row)
        ref = p.get("execution_ref")
        if ref:
            covered = [usage_by_ref[c] for c in p.get("covers") or () if c in usage_by_ref]
            usage_by_execution[ref].extend(covered or [row])
    coverage_rows = [r for r in projected if r.kind == 'coverage']
    # Decoded once per query, not in every group's report: each coverage row's
    # (domain, wi, status), and each execution's usage WIs and the work keys of
    # its usage without a WI. Missing fields still raise where they are read.
    coverage_fields = {id(r): tuple(read_payload(r).get(k, _MISSING) for k in ("domain", "wi", "status"))
                       for r in coverage_rows}
    execution_usage_keys: dict[str, tuple[frozenset, frozenset]] = {}

    def coverage_of(r: Receipt) -> tuple:
        found = coverage_fields.get(id(r))
        return found if found is not None else tuple(read_payload(r).get(k, _MISSING) for k in ("domain", "wi", "status"))

    def usage_keys(ref: str, produced: list[Receipt]) -> tuple[frozenset, frozenset]:
        found = execution_usage_keys.get(ref)
        if found is None:
            found = execution_usage_keys[ref] = (
                frozenset(read_payload(u).get("wi") for u in produced),
                frozenset(work_key(u) for u in produced if read_payload(u).get("wi") is None))
        return found

    def attribution_breakdown(rows: list[Receipt], bucket: str | None) -> dict[str, Any]:
        """How the group's usage got its WI: declared, holder supplement,
        scheduled job, reviewed non-WI classification, or not at all."""
        sources: dict[str, dict[str, Any]] = {}
        for row in rows:
            if row.kind != "usage":
                continue
            key = work_key(row)
            source = (attribution_sources["sources"].get(row.entity_ref)
                      or ("unattributed" if key in {UNKNOWN, "ambiguous"}
                          else "classified" if _non_wi_key(key) else "declared"))
            entry = sources.setdefault(source, {"event_count": 0, "priced_machine_cash_usd": Decimal(0)})
            entry["event_count"] += 1
            amount, _ = cost_for(row)
            if amount is not None:
                entry["priced_machine_cash_usd"] += amount
        result: dict[str, Any] = {"sources": {k: {**v, "priced_machine_cash_usd": decimal_text(v["priced_machine_cash_usd"])}
                                              for k, v in sorted(sources.items())}}
        if isinstance(bucket, str) and bucket.startswith("cron:"):
            job = bucket[len("cron:"):]
            result["job"] = {"id": job, "name": attribution_sources["jobs"].get(job)}
        return result

    def report(rows: list[Receipt], selected_wis: set[str], *, q: CollaborationQuery = q,
               grouped: bool = False, include_unassigned: bool = False, time_uncertain: bool = False, wi_bucket: str | None = None) -> dict[str, Any]:
        report_window = q.window
        def member(row: Receipt) -> bool:
            return read_payload(row).get("wi") in selected_wis or (include_unassigned and read_payload(row).get("wi") is None
                and (wi_bucket is None or work_key(row) == wi_bucket))
        def execution_member(row: Receipt) -> bool:
            # any(member(u) for u in produced), as set tests on the query's keys.
            produced = usage_by_execution.get(row.entity_ref)
            if not produced:
                return member(row)
            wis_, unassigned = usage_keys(row.entity_ref, produced)
            return bool(wis_ & selected_wis) or (include_unassigned and bool(unassigned)
                                                 and (wi_bucket is None or wi_bucket in unassigned))
        executions = [r for r in scoped_kinds["execution"] if execution_member(r)
                      and (not q.agent or read_payload(r).get(q.agent_role) == q.agent)
                      and (not q.runtime or read_payload(r)["runtime"] == q.runtime)
                      and context_matches(r, q)
                      and (q.scope != "activity" or _in_window(r.occurred_at, report_window))]
        # A request on this day may belong to an execution that began yesterday.
        # Keep that execution solely for linkage; its own activity is not added
        # to the dated usage bucket.
        linked_execution_refs = {read_payload(r)["execution_ref"] for r in rows if read_payload(r).get("execution_ref")}
        executions_by_ref = {r.entity_ref: r for r in scoped_kinds["execution"] if execution_member(r)}
        executions = list({r.entity_ref: r for r in executions +
                           [executions_by_ref[ref] for ref in linked_execution_refs if ref in executions_by_ref]}.values())
        def diagnostic_matches(original: Receipt) -> bool:
            row = diagnostic_by_fact.get(original.fact_ref, original)
            p = read_payload(row)
            # A direct execution dependency remains relevant even when it began
            # before this calendar window or has disputed identity metadata.
            if row.kind == 'execution' and row.entity_ref in linked_execution_refs:
                return True
            candidates = {work_key(row)}
            boundary = p.get('interval_start') or row.occurred_at
            for target in {row.entity_ref, p.get('execution_ref')} - {None}:
                for claim in task_evidence.get(target, []):
                    a = read_payload(claim)
                    if (boundary and a.get('effective_at') and
                            parse_timestamp(a['effective_at'], 'assignment') <= parse_timestamp(boundary, 'usage')):
                        candidate = ((a.get('wi') or a.get('work_kind')) if a['state'] == 'assigned'
                                     else 'ambiguous' if a['state'] == 'ambiguous' else None)
                        if candidate:
                            candidates.discard(UNKNOWN)
                            candidates.add(candidate)
            if not (not grouped and not q.work_items and q.scope != 'accepted_wi'):
                wanted = selected_wis | ({wi_bucket} if wi_bucket is not None else
                    {work_key(r) for r in rows} if grouped else {UNKNOWN} if include_unassigned else set())
                if not candidates & wanted:
                    return False
            if q.scope == 'activity':
                if row.kind == 'usage' and period_for(row, report_window) == 'outside':
                    return False
                if row.kind == 'execution' and not _in_window(row.occurred_at, report_window):
                    return False
                if row.kind == 'execution_expectation' and not (
                        (p['ended_at'] is None or parse_timestamp(p['ended_at'], 'end') >= report_window[0])
                        and parse_timestamp(p['started_at'], 'start') < report_window[1]):
                    return False
            for field, selected in ((q.agent_role, q.agent), ('runtime', q.runtime), ('model', q.model)):
                # Execution evidence has no model partition; it belongs to the
                # selected WI/runtime cohort, not a fabricated unknown model.
                if selected is not None and field in p and (p[field] or UNKNOWN) != selected:
                    return False
            context = diagnostic_context.get(row.entity_ref, actor_context.get(row.entity_ref, {}))
            return all(selected is None or (context.get(field) or UNKNOWN) == selected
                       for field, selected in (('node', q.node), ('entrypoint', q.entrypoint))) and (q.tenant is None or ((context.get('tenant') or {}).get('id') or UNKNOWN) == q.tenant)

        expected_epochs = [r for r in scoped_kinds["execution_expectation"] if member(r)
                           and (not q.agent or q.agent_role != "executor" or read_payload(r)["executor"] == q.agent)
                           and (not q.runtime or read_payload(r)["runtime"] == q.runtime)
                           and context_matches(r, q)
                           and (q.scope != "activity" or read_payload(r)["ended_at"] is None
                                or parse_timestamp(read_payload(r)["ended_at"], "end") >= report_window[0])
                           and (q.scope != "activity" or parse_timestamp(read_payload(r)["started_at"], "start") < report_window[1])]
        missing_epochs = [r.entity_ref for r in expected_epochs if read_payload(r)["status"] == "missing"]
        # Only an explicit import of the exact source usage evidence can join
        # this epoch. A same-WI Host count or matching Token total is not proof.
        selected_evidence = {e for r in rows for e in r.evidence}
        unjoined_epochs = [r.entity_ref for r in expected_epochs
                           if read_payload(r)["status"] == "sealed" and read_payload(r)["usage_ref"] not in selected_evidence]
        # One execution's usage spans models and WIs, i.e. several groups.
        # It is missing only when no usage of it was observed at all.
        missing = [r.entity_ref for r in executions if read_payload(r)["expected_usage"]
                   and r.entity_ref not in usage_by_execution]
        inflight = [r.entity_ref for r in executions if read_payload(r)["state"] in {"accepted", "running", "unknown"}]
        # A global idle/excluded registry source has no active cohort and is
        # not evidence against every WI.  Every other global state has an
        # unspecified source scope, so it remains conservatively relevant.
        def coverage_applies(r: Receipt, domains: set[str]) -> bool:
            domain, wi, status = coverage_of(r)
            return _field(domain, "domain") in domains and (_field(wi, "wi") in selected_wis or
                (wi is None and _field(status, "status") not in {"idle", "excluded"}))
        def applicable_coverage(domain: str) -> list[Receipt]:
            return [r for r in coverage_rows if coverage_applies(r, {domain})]
        cover = applicable_coverage("usage")
        # Coverage is a WI×domain assertion: every selected WI needs its own
        # complete receipt. A global complete source is an additional health
        # assertion, never a substitute denominator; a global gap affects all
        # WI queries that rely on this domain.
        covered_wis = {_field(coverage_of(r)[1], "wi") for r in cover
                       if _field(coverage_of(r)[1], "wi") in selected_wis
                       and _field(coverage_of(r)[2], "status") == "complete"}
        denom = (bool(selected_wis) and selected_wis <= covered_wis
                 and all(_field(coverage_of(r)[2], "status") == "complete" for r in cover))
        # Native logs/host turn counts alone cannot prove the entire WI inventory.
        inventory = applicable_coverage("execution")
        wi_inventory = {_field(coverage_of(r)[1], "wi") for r in inventory
                        if _field(coverage_of(r)[2], "status") == "complete"}
        wi_complete = (bool(selected_wis) and selected_wis <= wi_inventory
                       and all(_field(coverage_of(r)[2], "status") == "complete" for r in inventory))
        usage_conflicts = {key for key, rs in conflicts.items()
                           if any(r.kind == "usage" and diagnostic_matches(r) for r in rs)}
        counts = _counts(rows)
        # A producer's inclusive input/output delta can establish the total
        # without establishing its cache partitions. Count this total ONCE,
        # not in addition to classified fields; pricing still requires classes.
        known_quantities = [read_payload(r).get("reported_total_tokens")
            if read_payload(r).get("reported_total_tokens") is not None else
            (sum(v for v in read_payload(r)["tokens"].values() if v is not None)
             if any(v is not None for v in read_payload(r)["tokens"].values()) else None)
            for r in rows]
        known_total = (sum(v for v in known_quantities if v is not None)
                       if not rows or any(v is not None for v in known_quantities) else None)
        reasons = set()
        if q.context_ref is not None:
            # WI-wide coverage is not a denominator for a selected subset of
            # activities; no automatic allocation or complete ROI is implied.
            reasons.add("context_inventory_unknown")
            if reconciliation["context_selection"]["unavailable_usage_refs"]:
                reasons.add("context_linked_usage_unavailable")
        if any(r.entity_ref in actor_conflicts for r in rows):
            reasons.add("actor_attribution_conflict")
        if missing:
            reasons.add("incomplete_execution")
        if inflight:
            # A resident conversation stays running all day; its usage so far
            # is real, only its final total is not yet known.
            reasons.add("execution_in_progress")
        if missing_epochs:
            reasons.add("expected_measurement_missing")
        if unjoined_epochs:
            reasons.add("expected_measurement_unjoined")
        # A coverage fork is relevant exactly where one of its heads would be
        # an applicable usage/execution denominator for this group.
        if any((r.kind in {"execution", "execution_expectation"} and diagnostic_matches(r))
               or (r.kind == "coverage" and coverage_applies(r, {"usage", "execution"}))
               for group in conflicts.values() for r in group):
            reasons.add("execution_evidence_conflict")
        if (attribution_conflict_wis & selected_wis or
                any(not q.work_items or candidates & selected_wis
                    for candidates in observation_attribution_conflicts.values())):
            reasons.add("attribution_unknown")
        if not denom:
            reasons.add("source_coverage_unknown")
        known_non_wi = bool(rows) and not selected_wis and all(_non_wi_key(work_key(r)) for r in rows)
        if not wi_complete and not known_non_wi:
            reasons.add("wi_execution_inventory_unknown")
        if not executions or any(read_payload(r)["execution_ref"] not in {e.entity_ref for e in executions} for r in rows):
            reasons.add("execution_link_unknown")
        if catalog and not catalog_observed_at:
            reasons.add("price_timestamp_unknown")
        relevant_conflicts = {r.entity_ref for r in scoped
            if r.kind == 'usage' and r.entity_ref in rejected_refs and diagnostic_matches(r)}
        if relevant_conflicts or usage_conflicts:
            reasons.add("join_conflict")
        scoped_unallocated = [r for r in unallocated if diagnostic_matches(r)]
        if scoped_unallocated:
            reasons.add("time_unallocated")
        if any(not read_payload(r)["final"] for r in rows):
            reasons.add("provisional_usage")
        costs, unpriced = [], []
        priced_by_ref: dict[str, Decimal] = {}
        for r in rows:
            amount, reason = cost_for(r)
            if amount is None:
                unpriced.append({"fact_ref": r.fact_ref, "reason": reason})
            else:
                costs.append(amount)
                priced_by_ref[r.entity_ref] = amount
        if unpriced:
            reasons.add("incomplete_price_or_quantity")
        if not rows:
            reasons.add("no_usage_observations")
        if time_uncertain:
            reasons.add("time_unallocated")
        if any(work_key(r) in {UNKNOWN, "ambiguous"} for r in rows):
            reasons.add("attribution_unknown")
        total = sum(costs, Decimal(0))
        cost_complete = not reasons
        ds = [r for r in scoped_kinds["delivery"] if member(r)
              and (not q.agent or q.agent_role != "responsible" or read_payload(r)["responsible"] == q.agent)]
        if q.scope == "activity":
            # A delivery accepted earlier still participates in quality when
            # its review activity is inside this day/window.
            reviewed_here: dict[str, str] = {}
            for review_row in scoped:
                if review_row.kind != "review" or not _in_window(review_row.occurred_at, report_window):
                    continue
                prior = reviewed_here.get(read_payload(review_row)["delivery_ref"])
                if prior is None or parse_timestamp(review_row.occurred_at, "reviewed_at") > parse_timestamp(prior, "reviewed_at"):
                    reviewed_here[read_payload(review_row)["delivery_ref"]] = review_row.occurred_at
            ds = [r for r in ds if _in_window(read_payload(r)["accepted_at"] or r.occurred_at, report_window)
                  or (r.entity_ref in reviewed_here and (read_payload(r)["accepted_at"] is None
                      or parse_timestamp(read_payload(r)["accepted_at"], "accepted_at")
                      <= parse_timestamp(reviewed_here[r.entity_ref], "reviewed_at")))]
        rs = [r for r in scoped_kinds["review"] if member(r)
              and (not q.agent or q.agent_role != "reviewer" or read_payload(r)["reviewer"] == q.agent)
              and (q.scope != "activity" or _in_window(r.occurred_at, report_window))]
        if q.agent and q.agent_role == "reviewer":
            ds = [d for d in ds if any(read_payload(r)["delivery_ref"] == d.entity_ref for r in rs)]
        hs = [r for r in scoped_kinds["human"] if member(r)
              and (not q.agent or read_payload(r).get(q.agent_role) == q.agent)
              and (q.scope != "activity" or _in_window(r.occurred_at, report_window))]
        ss = [r for r in scoped_kinds["human_summary"] if member(r)]
        human = human_summary(hs, ss, calendar=q.scope == "activity")
        selected_cost = total
        roi_reasons = set(reasons)
        if q.cost_basis == "machine_plus_measured_human":
            hc = applicable_coverage("human")
            human_covered_wis = {read_payload(r)["wi"] for r in hc
                                 if read_payload(r)["wi"] in selected_wis and read_payload(r)["status"] == "complete"}
            human_complete = (bool(selected_wis) and selected_wis <= human_covered_wis
                              and all(read_payload(r)["status"] == "complete" for r in hc))
            human_conflict = any(any(r.kind in {"human", "human_summary"}
                                     and read_payload(r).get("wi") in selected_wis
                                     for r in candidates) for candidates in conflicts.values())
            if (human["focused_seconds_known"] is None or human["focused_seconds_unknown"] or ss or human_conflict
                    or q.human_hourly_rate is None or not human_complete):
                roi_reasons.add("incomplete_measured_human_cost")
            else:
                selected_cost += Decimal(human["focused_seconds_known"]) * decimal(q.human_hourly_rate, "rate") / 3600
        vs = [r for r in scoped_kinds["value"] if read_payload(r)["wi"] in selected_wis]
        benefit = _benefits(vs, ds, rs, selected_wis, q)
        roi_reasons.update(benefit["reasons"])
        if q.scope == "activity":
            roi_reasons.add("basis_mismatch_activity_vs_lifetime")
        if q.runtime or q.model or q.node or q.entrypoint or any(g in {"runtime", "model", "node", "entrypoint"} for g in q.group_by):
            roi_reasons.add("unsupported_dimension")
        if q.agent and not q.work_items:
            roi_reasons.add("agent_cohort_requires_explicit_wis")
        value_sources = applicable_coverage("value")
        value_inventory = {read_payload(r)["wi"] for r in value_sources if read_payload(r)["status"] == "complete"}
        if selected_wis - value_inventory or any(read_payload(r)["status"] != "complete" for r in value_sources):
            roi_reasons.add("value_inventory_unknown")
        if any(any(r.kind in {"delivery", "review", "value", "attribution", "execution", "coverage", "human", "human_summary"}
                       and (read_payload(r).get("wi") in selected_wis or read_payload(r).get("wi") is None) for r in rs)
               for rs in conflicts.values()):
            roi_reasons.add("evidence_revision_conflict")
        if selected_cost == 0:
            roi_reasons.add("zero_denominator")
        if not selected_wis:
            roi_reasons.add("no_cohort")
        value = decimal(benefit["known_usd"], "value") if benefit["known_usd"] is not None else None
        ratio = (value - selected_cost) / selected_cost if not roi_reasons and value is not None else None
        conflict_reviews = [r for candidates in conflicts.values() for r in candidates
                            if r.kind == "review" and member(r)
                            and (not q.agent or q.agent_role != "reviewer" or read_payload(r)["reviewer"] == q.agent)
                            and (q.scope != "activity" or _in_window(r.occurred_at, report_window))]
        from .outcome_projection import outcomes_for_period
        # Original cohort/quality stays intact. Output counts use the delivery's
        # own occurrence, never a later review date. Historical/as-of selection
        # has already happened above; the GUI never reinterprets these facts.
        outcome_deliveries = [r for r in scoped_kinds["delivery"] if member(r)
            and (not q.agent or q.agent_role != "responsible" or read_payload(r)["responsible"] == q.agent)]
        outcomes = outcomes_for_period(outcome_deliveries, [r for r in scoped_kinds["review"] if member(r)],
            window=report_window if q.scope == "activity" else None,
            coverage=applicable_coverage("quality"), wis=selected_wis,
            conflicts=[r for versions in conflicts.values() for r in versions])
        quality = quality_summary(ds, rs + conflict_reviews)
        # current_receipts removes conflicting revisions from rs. Keep their
        # affected delivery in the metric conflict count/denominator instead
        # of merely painting an otherwise zero-conflict summary as conflicted.
        conflicted_deliveries = {read_payload(r)["delivery_ref"] for r in conflict_reviews
                                 if read_payload(r)["delivery_ref"] in {d.entity_ref for d in ds}}
        quality["conflicts"] = max(quality["conflicts"], len(conflicted_deliveries))
        quality["metric_scope"] = "wi_cohort_not_runtime_contribution" if q.runtime or q.model else "delivery_cohort"
        quality["status"] = "conflict" if quality["conflicts"] else "observed" if ds else "unavailable"
        identity_missing = {
            "agent": [r for r in rows if read_payload(r).get("executor") in {None, "ambiguous", UNKNOWN}],
            "wi": [r for r in rows if work_key(r) in {"ambiguous", UNKNOWN}],
        }
        identity_statistics = {}
        for dimension, missing_rows in identity_missing.items():
            known = [priced_by_ref[r.entity_ref] for r in missing_rows if r.entity_ref in priced_by_ref]
            identity_statistics[dimension + "_unassigned_event_count"] = len(missing_rows)
            identity_statistics[dimension + "_unassigned_priced_usd"] = (
                decimal_text(sum(known, Decimal(0))) if known or not missing_rows else None)
        from .runtime_subcalls import summarize_subcalls
        return {"work_items": sorted(selected_wis), "usage": {**identity_statistics, **summarize_subcalls(rows, actor_context), "tokens": counts,
                "known_total_tokens": known_total,
                "event_count": len(rows), "expected_execution_count": len(executions),
                "first_usage_at": min((read_payload(r).get("interval_start") or r.occurred_at for r in rows
                    if read_payload(r).get("interval_start") or r.occurred_at), default=None,
                    key=lambda t: parse_timestamp(t, "usage time")),
                "attribution_refs": sorted({ref for r in rows for ref in actor_evidence.get(r.entity_ref, [])}),
                "last_usage_at": max((read_payload(r).get("interval_end") or r.occurred_at for r in rows
                    if (read_payload(r).get("interval_end") or r.occurred_at) and
                    (read_payload(r).get("reported_total_tokens") is not None or any(v is not None for v in read_payload(r)["tokens"].values()))), default=None,
                    key=lambda t: parse_timestamp(t, "usage time")),
                "unassigned_event_count": sum(work_key(r) == UNKNOWN for r in rows),
                "non_wi_event_count": sum(_non_wi_key(work_key(r)) is not None for r in rows),
                "cron_event_count": sum(_non_wi_key(work_key(r)) == "cron" for r in rows),
                "no_wi_event_count": sum(work_key(r) == "none" for r in rows),
                "unassigned_known_total_tokens": sum(n for r, n in zip(rows, known_quantities)
                    if work_key(r) == UNKNOWN and n is not None),
                "source_details": _source_diagnostics(coverage_rows, selected_wis),
                "conflicted_event_count": len(relevant_conflicts | usage_conflicts),
                "conflict_reasons": conflict_reason_counts(relevant_conflicts, usage_conflicts),
                "time_unallocated_event_count": len(scoped_unallocated),
                "missing_execution_refs": sorted(missing), "in_flight_refs": sorted(inflight),
                "expected_measurement_epoch_count": len(expected_epochs),
                "missing_measurement_epoch_refs": sorted(missing_epochs),
                "unjoined_measurement_epoch_refs": sorted(unjoined_epochs),
                "source_coverage": "complete" if denom else "unknown",
                "wi_inventory_coverage": "not_applicable" if known_non_wi else "complete" if wi_complete else "unknown",
                "fact_refs": sorted({ref for r in rows for ref in observation_groups.get(r.entity_ref, [r.fact_ref])})},
                "cost": {"machine_cash_usd": decimal_text(total) if cost_complete else None,
                         "partial_machine_cash_usd": decimal_text(total) if costs and not cost_complete and "join_conflict" not in reasons else None,
                         # Every priced row regardless of reasons: what is
                         # priced so far, never a complete-cost claim.
                         "priced_machine_cash_usd": decimal_text(total) if costs else None,
                         "priced_event_count": len(costs),
                         "cash_basis": "openrouter_standard_token_cash", "reasons": sorted(reasons), "unpriced": unpriced},
                "human_attention": human, "quality": quality, "outcomes": outcomes, "business_value": benefit,
                "roi": {"ratio": decimal_text(ratio) if ratio is not None else None,
                        "numerator": decimal_text(value - selected_cost) if value is not None and not roi_reasons else None,
                        "denominator": decimal_text(selected_cost) if not (reasons or "incomplete_measured_human_cost" in roi_reasons) else None,
                        "known_value_usd": benefit["known_usd"], "cost_basis": q.cost_basis,
                        "status": "calculated" if ratio is not None else "unavailable", "reasons": sorted(roi_reasons),
                        "cohort_count": len(selected_wis), "policy": POLICY}}

    summary = report(exact_time, wis, include_unassigned=not q.work_items)
    groups = []
    if q.group_by:
        buckets: dict[tuple[str, ...], list[Receipt]] = defaultdict(list)
        calendar_dimensions = [d for d in q.group_by if d in {"day", "week", "month"}]
        def calendar_key(timestamp: str | None, dimension: str) -> str:
            if timestamp is None:
                return "time_unallocated"
            local = parse_timestamp(timestamp, "time").astimezone(ZoneInfo(q.timezone))
            iso = local.isocalendar()
            return local.date().isoformat() if dimension == "day" else f"{iso.year}-W{iso.week:02}" if dimension == "week" else local.strftime("%Y-%m")
        def key_for(row: Receipt) -> tuple[str, ...]:
            p = read_payload(row)
            keys = []
            for dimension in q.group_by:
                if dimension in calendar_dimensions:
                    timestamp = accepted_at.get(p.get("wi")) if q.scope == "accepted_wi" else p.get("accepted_at") or row.occurred_at
                    key = calendar_key(timestamp, dimension)
                    if q.scope == "activity" and row.kind == "usage" and p["interval_start"]:
                        a, b = parse_timestamp(p["interval_start"], "start"), parse_timestamp(p["interval_end"], "end")
                        left = calendar_key(utc_text(a), dimension)
                        right = calendar_key(utc_text(b - timedelta(microseconds=1) if b > a else b), dimension)
                        key = left if left == right else "time_unallocated"
                elif dimension == "wi":
                    key = work_key(row)
                else:
                    key = ((actor_context.get(row.entity_ref, {}).get("tenant") or {}).get("id") if dimension == "tenant"
                           else actor_context.get(row.entity_ref, {}).get(dimension) if dimension in {"node", "entrypoint"}
                           else p.get(q.agent_role if dimension == "agent" else dimension)) or UNKNOWN
                keys.append(key)
            return tuple(keys)
        for row in exact_time:
            buckets[key_for(row)].append(row)
        observed_usage_runtime = {(read_payload(r).get("wi"), read_payload(r)["runtime"]) for r in exact_time}
        # Executions/expectations are cohort evidence even when no usage was
        # collected.  Seed runtime/model buckets from them so a missing WI is
        # not erased; executions without a model belong to `unassigned`.
        def in_group_window(row: Receipt) -> bool:
            if q.scope != "activity":
                return True
            if row.kind == "execution_expectation":
                return (read_payload(row)["ended_at"] is None or
                        parse_timestamp(read_payload(row)["ended_at"], "end") >= window[0]) and \
                       parse_timestamp(read_payload(row)["started_at"], "start") < window[1]
            return _in_window(row.occurred_at, window)
        for row in scoped:
            if row.kind not in {"execution", "execution_expectation"}:
                continue
            if not in_group_window(row):
                continue
            # An execution has no native model dimension.  Do not manufacture
            # an extra unassigned-model cohort for a WI already observed in
            # this runtime; retain it only for the execution-only gap.
            if "model" in q.group_by and (read_payload(row).get("wi"), read_payload(row)["runtime"]) in observed_usage_runtime:
                continue
            if q.agent and read_payload(row).get(q.agent_role) != q.agent:
                continue
            if q.runtime and read_payload(row)["runtime"] != q.runtime:
                continue
            if not context_matches(row, q):
                continue
            buckets.setdefault(key_for(row), [])
        # Deliveries/humans with no usage still deserve a group and an unavailable cost.
        # Runtime/model are not native dimensions of these domains.
        if not (q.node or q.entrypoint or any(d in {"runtime", "model", "node", "entrypoint"} for d in q.group_by)):
            for row in scoped:
                if row.kind not in {"delivery", "human", "review"}:
                    continue
                if row.kind == "review" and ("agent" not in q.group_by or q.agent_role != "reviewer"):
                    continue
                if "agent" in q.group_by and not read_payload(row).get(q.agent_role):
                    continue
                if q.scope == "activity" and not _in_window(read_payload(row).get("accepted_at") or row.occurred_at, window):
                    continue
                if q.agent and read_payload(row).get(q.agent_role) != q.agent:
                    continue
                buckets.setdefault(key_for(row), [])
        for key, rows in sorted(buckets.items()):
            named = dict(zip(q.group_by, key))
            group_wis = ({named["wi"]} if named.get("wi") not in {None, UNKNOWN, "ambiguous"} and not _non_wi_key(named["wi"])
                         else {read_payload(r)["wi"] for r in rows if read_payload(r)["wi"] not in {None, "ambiguous"}})
            # A bucket's denominator is its complete expected WI cohort, not
            # merely the WIs that happened to emit usage.  This preserves a
            # missing execution as an unavailable group instead of deleting it.
            if "wi" not in named:
                # Never reconstruct a composite cohort from usage alone:
                # execution expectations are precisely the evidence that a
                # no-usage WI still belongs to this agent/runtime bucket.
                group_wis.update(read_payload(r)["wi"] for r in scoped
                                 if r.kind in {"usage", "execution", "execution_expectation", "delivery", "human", "review"}
                                 and read_payload(r).get("wi") not in {None, "ambiguous", "excluded"}
                                 and not ("model" in q.group_by and r.kind in {"execution", "execution_expectation"}
                                          and (read_payload(r).get("wi"), read_payload(r)["runtime"]) in observed_usage_runtime)
                                 and key_for(r) == key
                                 and in_group_window(r))
            if not group_wis and not rows and "wi" not in named:
                group_wis = {read_payload(r)["wi"] for r in scoped if r.kind in {"delivery", "human", "review"}
                             and key_for(r) == key and read_payload(r).get("wi")}
            group_args: dict[str, Any] = {"work_items": tuple(sorted(group_wis))}
            for dimension, param in (("agent", "agent"), ("runtime", "runtime"), ("model", "model"), ("node", "node"), ("entrypoint", "entrypoint"), ("tenant", "tenant")):
                if dimension in named:
                    group_args[param] = named[dimension]
            uncertain = any(named[d] == "time_unallocated" for d in calendar_dimensions)
            if calendar_dimensions and not uncertain:
                dimension = "day" if "day" in named else "week" if "week" in named else "month"
                label = named[dimension]
                if dimension == "day":
                    low = date.fromisoformat(label)
                    high = low + timedelta(days=1)
                elif dimension == "week":
                    year, week = label.split("-W")
                    low = date.fromisocalendar(int(year), int(week), 1)
                    high = low + timedelta(days=7)
                else:
                    low = date.fromisoformat(label + "-01")
                    high = date(low.year + 1, 1, 1) if low.month == 12 else date(low.year, low.month + 1, 1)
                group_args.update(from_date=max(low, date.fromisoformat(q.from_date)).isoformat(),
                                  to_date=min(high, date.fromisoformat(q.to_date)).isoformat())
            item = report(rows, group_wis, q=replace(q, **group_args), grouped=True,
                          include_unassigned=named.get("wi") == UNKNOWN or bool(_non_wi_key(named.get("wi"))), time_uncertain=uncertain,
                          wi_bucket=named.get("wi"))
            if uncertain:
                item["quality"] = {**item["quality"], "status":"time_unallocated", "metric_scope":"not_an_exact_calendar_bucket"}
            elif any(g in {"runtime", "model"} for g in q.group_by) or ("agent" in named and q.agent_role == "executor"):
                item["quality"]["metric_scope"] = "wi_cohort_nonadditive_not_causal_contribution"
            item["business_value"]["metric_scope"] = "selected_value_period"  # not this activity bucket's earned value
            item["attribution"] = attribution_breakdown(rows, named.get("wi"))
            if 'tenant' in named:
                tenants = [actor_context.get(r.entity_ref, {}).get('tenant') for r in rows]
                tenants = [t for t in tenants if t]
                if tenants:
                    names = {t['name'] for t in tenants if t['name']}
                    item['tenant'] = {'id': named['tenant'], 'name': next(iter(names)) if len(names) == 1 else None,
                        'binding_refs': sorted({ref for t in tenants for ref in t['binding_refs']})}
                    if any('source' in t for t in tenants):
                        # Registry-completed tenants say where each name came from.
                        item['tenant'].update(sources=sorted({t['source'] for t in tenants}),
                            registry_conflicts=sorted({t['registry_conflict'] for t in tenants if t['registry_conflict']}))
            groups.append({"key": named, **item})
    parameters = asdict(q)
    if q.context_ref is None:
        parameters.pop('context_ref')
    if q.tenant is None and 'tenant' not in q.group_by:
        parameters.pop('tenant')  # Preserve old query witnesses without the new dimension.
    result = {"schema": "computecost_collaboration_query/v1", "query": parameters,
              "watermark": snap["watermark"], "recorded_at": snap["recorded_at"],
              "facts_sha256": snap["sha256"], "policy": POLICY,
              "cohort_anchor": "first_accepted_delivery_at_as_of",
              "catalog_sha256": digest(catalog) if catalog else None,
              "catalog_observed_at": catalog_observed_at,
              "summary": summary, "groups": groups, "reconciliation": reconciliation,
              "revision_conflicts": sorted(conflicts), "attribution_conflicts": sorted(attr_conflicts),
              "actor_attribution_conflicts": actor_conflicts, "actor_evidence": actor_evidence,
              "time_unallocated": {"fact_refs": sorted(r.fact_ref for r in unallocated), "tokens": _counts(unallocated)}}
    from .stage_facts import stage_report
    unsupported_stage_filters = tuple(name for name in ("agent", "runtime", "model", "node", "entrypoint", "tenant")
                                      if getattr(q, name) is not None)
    # accepted_wi is a legacy delivery cohort, not proof of stage acceptance.
    if q.scope == "accepted_wi":
        unsupported_stage_filters += ("accepted_wi",)
    try:
        result["stages"] = stage_report(stage_rows, stage_conflicts, reconciliation["stage_membership"],
            work_items=q.work_items, window=window, unsupported_filters=unsupported_stage_filters,
            context_ref=q.context_ref, history=snap["facts"])
    except (ValueError, KeyError, TypeError, ArithmeticError):
        # An optional stage projection must not suppress verified machine usage.
        # Preserve the same source watermark; unknown output is never zero.
        result['stages'] = {k: None for k in ('outputs', 'membership_conflicts',
            'revision_conflicts', 'shared_refs', 'unknown_refs', 'unresolved_link_targets',
            'time_unallocated_refs')}
        result['stages'].update(status='unavailable', coverage='recorded_declarations_only',
            execution_authority=False, unsupported_filters=list(unsupported_stage_filters),
            reason='stage_projection_failed')
    result["report_sha256"] = digest(result)
    return result
