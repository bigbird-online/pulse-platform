"""Fixed-order D→R→E→P cost-driver bridge for ComputeCost S4."""

from __future__ import annotations

from collections import Counter, defaultdict
from decimal import Decimal
from typing import Any, Iterable, Mapping

from .events import UNKNOWN, UsageEvent
from .openrouter import OpenRouterPriceSnapshot


BRIDGE_SCHEMA = "cost_driver_bridge.v1"


def _decimal_text(value: Decimal) -> str:
    if value == 0:
        return "0"
    rendered = format(value, "f")
    return rendered.rstrip("0").rstrip(".") if "." in rendered else rendered


def _price_map(prices: Iterable[OpenRouterPriceSnapshot]) -> dict[str, OpenRouterPriceSnapshot]:
    result: dict[str, OpenRouterPriceSnapshot] = {}
    for price in prices:
        if not isinstance(price, OpenRouterPriceSnapshot):
            raise ValueError("bridge prices must contain OpenRouterPriceSnapshot values")
        if price.model_id in result:
            raise ValueError(f"duplicate bridge price for model: {price.model_id}")
        result[price.model_id] = price
    return result


def _event_cost(event: UsageEvent, prices: Mapping[str, OpenRouterPriceSnapshot]) -> Decimal | None:
    price = prices.get(event.model)
    if event.source != "openrouter_api" or price is None:
        return None
    cache_price = price.input_cache_read_per_token
    if event.tokens.cache_read and cache_price is None:
        return None
    uncached = event.tokens.input - event.tokens.cache_read
    return (
        Decimal(uncached) * price.prompt_per_token
        + Decimal(event.tokens.cache_read) * (cache_price or Decimal("0"))
        + Decimal(event.tokens.output) * price.completion_per_token
    )


def _cohort(event: UsageEvent) -> tuple[str, str, str] | None:
    # Cohort axes are producer-owned evidence. S4 never substitutes task_ref,
    # model, cwd, or route fields when task_family/risk_class are unknown.
    values = (event.consumer_path, event.task_family, event.risk_class)
    if any(value == UNKNOWN for value in values):
        return None
    return values


def _route(event: UsageEvent) -> tuple[str, str, str, str, str, str] | None:
    values = (
        event.runtime,
        event.model,
        event.effort,
        event.base_route,
        event.risk_floor,
        event.effective_route,
    )
    if any(value == UNKNOWN for value in values):
        return None
    return values


def _build_state(events: list[UsageEvent]) -> tuple[
    dict[tuple[str, str, str], Decimal],
    dict[tuple[str, str, str], dict[tuple[str, str, str, str, str, str], Decimal]],
    dict[tuple[str, str, str], dict[str, Decimal]],
    set[str],
]:
    demand: Counter[tuple[str, str, str]] = Counter()
    routes: dict[tuple[str, str, str], Counter[tuple[str, str, str, str, str, str]]] = defaultdict(Counter)
    execution_totals: dict[tuple[str, str, str], Counter[str]] = defaultdict(Counter)
    missing: set[str] = set()

    for event in events:
        cohort = _cohort(event)
        route = _route(event)
        if cohort is None:
            missing.add("cohort_evidence_incomplete")
            continue
        if route is None:
            missing.add("route_evidence_incomplete")
            continue
        if event.retry_count is None or event.escalation_count is None:
            missing.add("execution_evidence_incomplete")
            continue
        demand[cohort] += 1
        routes[cohort][route] += 1
        bucket = execution_totals[cohort]
        bucket["input_tokens"] += event.tokens.input
        bucket["output_tokens"] += event.tokens.output
        bucket["cache_read_tokens"] += event.tokens.cache_read
        bucket["cache_write_tokens"] += event.tokens.cache_write
        bucket["retry_count"] += event.retry_count
        bucket["escalation_count"] += event.escalation_count

    route_mix: dict[
        tuple[str, str, str],
        dict[tuple[str, str, str, str, str, str], Decimal],
    ] = {}
    execution: dict[tuple[str, str, str], dict[str, Decimal]] = {}
    for cohort, count in demand.items():
        denominator = Decimal(count)
        route_mix[cohort] = {
            route: Decimal(route_count) / denominator
            for route, route_count in routes[cohort].items()
        }
        execution[cohort] = {
            field: Decimal(value) / denominator
            for field, value in execution_totals[cohort].items()
        }
    return (
        {cohort: Decimal(count) for cohort, count in demand.items()},
        route_mix,
        execution,
        missing,
    )


def _unit_cost(execution: Mapping[str, Decimal], price: OpenRouterPriceSnapshot) -> Decimal | None:
    cache_read = execution["cache_read_tokens"]
    if cache_read and price.input_cache_read_per_token is None:
        return None
    uncached = execution["input_tokens"] - cache_read
    return (
        uncached * price.prompt_per_token
        + cache_read * (price.input_cache_read_per_token or Decimal("0"))
        + execution["output_tokens"] * price.completion_per_token
    )


def _evaluate(
    demand: Mapping[tuple[str, str, str], Decimal],
    route_mix: Mapping[
        tuple[str, str, str],
        Mapping[tuple[str, str, str, str, str, str], Decimal],
    ],
    execution: Mapping[tuple[str, str, str], Mapping[str, Decimal]],
    prices: Mapping[str, OpenRouterPriceSnapshot],
    cohorts: set[tuple[str, str, str]],
) -> Decimal | None:
    total = Decimal("0")
    for cohort in cohorts:
        count = demand.get(cohort, Decimal("0"))
        if count == 0:
            continue
        mixes = route_mix.get(cohort)
        exec_values = execution.get(cohort)
        if mixes is None or exec_values is None:
            return None
        for route, share in mixes.items():
            model = route[1]
            price = prices.get(model)
            if price is None:
                return None
            unit = _unit_cost(exec_values, price)
            if unit is None:
                return None
            total += count * share * unit
    return total


def _observed_cost(
    events: list[UsageEvent], prices: Mapping[str, OpenRouterPriceSnapshot]
) -> Decimal | None:
    total = Decimal("0")
    for event in events:
        cost = _event_cost(event, prices)
        if cost is None:
            return None
        total += cost
    return total


def _outcome_counts(events: list[UsageEvent]) -> dict[str, int]:
    result = {"accepted": 0, "rejected": 0, "unknown": 0, "human_correction": 0}
    for event in events:
        if event.outcome_status == "accepted":
            result["accepted"] += 1
        elif event.outcome_status == "rejected":
            result["rejected"] += 1
        else:
            result["unknown"] += 1
        if event.human_correction is True:
            result["human_correction"] += 1
    return result


def _execution_summary(
    events: list[UsageEvent],
) -> dict[str, int]:
    return {
        "event_count": len(events),
        "total_tokens": sum(event.tokens.input + event.tokens.output for event in events),
        "cache_read_tokens": sum(event.tokens.cache_read for event in events),
        "retry_count": sum(event.retry_count or 0 for event in events),
        "escalation_count": sum(event.escalation_count or 0 for event in events),
    }


def build_cost_driver_bridge(
    *,
    baseline_events: Iterable[UsageEvent],
    current_events: Iterable[UsageEvent],
    baseline_prices: Iterable[OpenRouterPriceSnapshot] = (),
    current_prices: Iterable[OpenRouterPriceSnapshot] = (),
) -> dict[str, Any]:
    """Build a fixed-order counterfactual bridge without inventing evidence.

    Complete S4 bridge valuation is intentionally limited to API-priced events.
    A weekly query cannot allocate the fixed monthly Pro settlement, so Pro or
    unknown-source events fail closed instead of being priced as if they were
    cash/API usage. Missing cohort/counterfactual facts are captured by residual
    when the observed endpoints remain valu-able.
    """

    baseline = list(baseline_events)
    current = list(current_events)
    if any(not isinstance(event, UsageEvent) for event in baseline + current):
        raise ValueError("bridge events must contain UsageEvent values")
    p0 = _price_map(baseline_prices)
    p1 = _price_map(current_prices)
    reasons: set[str] = set()
    if not p0 or not p1:
        reasons.add("bridge_price_evidence_unavailable")
    if any(event.source == "chatgpt_pro" for event in baseline + current):
        reasons.add("pro_settlement_unavailable_for_window")
    if any(event.source == "claude_subscription" for event in baseline + current):
        reasons.add("claude_subscription_settlement_unavailable_for_window")
    if any(event.source == UNKNOWN for event in baseline + current):
        reasons.add("source_evidence_incomplete")

    d0, r0, e0, missing0 = _build_state(baseline)
    d1, r1, e1, missing1 = _build_state(current)
    reasons.update(missing0)
    reasons.update(missing1)

    observed0 = _observed_cost(baseline, p0) if p0 else None
    observed1 = _observed_cost(current, p1) if p1 else None
    if observed0 is None or observed1 is None:
        reasons.add("observed_cost_evidence_incomplete")

    # Current-only cohorts lack R0/E0 and therefore cannot be attributed to D
    # without fabrication. They remain in observed_delta and are absorbed by
    # residual. Baseline-only cohorts are valid: D1=0 removes them in D step.
    attributable = set(d0)
    current_only = set(d1) - set(d0)
    if current_only:
        reasons.add("counterfactual_cohort_missing")

    union_models = {
        route[1]
        for cohort in attributable
        for mixes in (r0.get(cohort, {}), r1.get(cohort, {}))
        for route in mixes
    }
    if any(model not in p0 or model not in p1 for model in union_models):
        reasons.add("counterfactual_price_missing")

    stages: list[Decimal] | None = None
    if p0 and p1 and "counterfactual_price_missing" not in reasons:
        c0000 = _evaluate(d0, r0, e0, p0, attributable)
        c1000 = _evaluate(d1, r0, e0, p0, attributable)
        c1100 = _evaluate(d1, r1, e0, p0, attributable)
        c1110 = _evaluate(d1, r1, e1, p0, attributable)
        c1111 = _evaluate(d1, r1, e1, p1, attributable)
        if all(value is not None for value in (c0000, c1000, c1100, c1110, c1111)):
            stages = [c0000, c1000, c1100, c1110, c1111]  # type: ignore[list-item]
        else:
            reasons.add("counterfactual_execution_unavailable")

    deltas: dict[str, str | None] = {
        "demand_delta_usd": None,
        "route_delta_usd": None,
        "execution_delta_usd": None,
        "price_delta_usd": None,
        "residual_delta_usd": None,
        "observed_delta_usd": (
            _decimal_text(observed1 - observed0)
            if observed0 is not None and observed1 is not None
            else None
        ),
    }
    if stages is not None and observed0 is not None and observed1 is not None:
        demand_delta = stages[1] - stages[0]
        route_delta = stages[2] - stages[1]
        execution_delta = stages[3] - stages[2]
        price_delta = stages[4] - stages[3]
        observed_delta = observed1 - observed0
        residual = observed_delta - (
            demand_delta + route_delta + execution_delta + price_delta
        )
        deltas.update(
            {
                "demand_delta_usd": _decimal_text(demand_delta),
                "route_delta_usd": _decimal_text(route_delta),
                "execution_delta_usd": _decimal_text(execution_delta),
                "price_delta_usd": _decimal_text(price_delta),
                "residual_delta_usd": _decimal_text(residual),
            }
        )
        recomposed = demand_delta + route_delta + execution_delta + price_delta + residual
        if recomposed != observed_delta:
            return {
                "schema": BRIDGE_SCHEMA,
                "schema_version": 1,
                "status": "invalid",
                "metric": "managed_cost_usd",
                "ordering": ["demand", "route", "execution", "price", "residual"],
                **deltas,
                "reasons": ["bridge_arithmetic_nonconserving"],
                "conservation": "failed",
            }

    baseline_outcomes = _outcome_counts(baseline)
    current_outcomes = _outcome_counts(current)
    outcome = {
        "baseline": baseline_outcomes,
        "current": current_outcomes,
        "accepted_change": current_outcomes["accepted"] - baseline_outcomes["accepted"],
        "human_correction_change": (
            current_outcomes["human_correction"] - baseline_outcomes["human_correction"]
        ),
        "baseline_cost_per_accepted_outcome_usd": (
            _decimal_text(observed0 / Decimal(baseline_outcomes["accepted"]))
            if observed0 is not None and baseline_outcomes["accepted"]
            else None
        ),
        "current_cost_per_accepted_outcome_usd": (
            _decimal_text(observed1 / Decimal(current_outcomes["accepted"]))
            if observed1 is not None and current_outcomes["accepted"]
            else None
        ),
        "note": "outcome_is_quality_constraint_not_fifth_cost_driver",
    }
    baseline_execution = _execution_summary(baseline)
    current_execution = _execution_summary(current)
    execution_evidence = {
        "baseline": baseline_execution,
        "current": current_execution,
        "delta": {
            key: current_execution[key] - baseline_execution[key]
            for key in baseline_execution
        },
    }

    return {
        "schema": BRIDGE_SCHEMA,
        "schema_version": 1,
        "status": "complete" if not reasons and stages is not None else "insufficient_evidence",
        "metric": "managed_cost_usd",
        "valuation_scope": "api_reference_equals_managed_cost_without_pro_settlement",
        "cohort_axes": ["consumer_path", "task_family", "risk_class"],
        "ordering": ["demand", "route", "execution", "price", "residual"],
        **deltas,
        "execution_evidence": execution_evidence,
        "outcome_constraint": outcome,
        "reasons": sorted(reasons),
        "conservation": (
            "pass"
            if stages is not None and observed0 is not None and observed1 is not None
            else "not_evaluable"
        ),
    }


def validate_cost_driver_bridge(value: object) -> None:
    """Validate S4 bridge identity, ordering, and arithmetic conservation."""

    if not isinstance(value, Mapping):
        raise ValueError("S4 driver bridge must be an object")
    if value.get("schema") != BRIDGE_SCHEMA or value.get("schema_version") != 1:
        raise ValueError("S4 driver bridge schema is invalid")
    if value.get("ordering") != ["demand", "route", "execution", "price", "residual"]:
        raise ValueError("S4 driver bridge ordering must be D-R-E-P-residual")
    status = value.get("status")
    if status not in {"complete", "insufficient_evidence"}:
        raise ValueError("S4 driver bridge status is unsafe")
    if value.get("metric") != "managed_cost_usd":
        raise ValueError("S4 driver bridge metric is invalid")
    reasons = value.get("reasons")
    if not isinstance(reasons, list) or any(not isinstance(item, str) or not item for item in reasons):
        raise ValueError("S4 driver bridge reasons must be strings")
    if status == "complete" and reasons:
        raise ValueError("complete S4 driver bridge cannot carry evidence gaps")
    if status == "insufficient_evidence" and not reasons:
        raise ValueError("insufficient S4 driver bridge must name evidence gaps")
    if value.get("conservation") not in {"pass", "not_evaluable"}:
        raise ValueError("S4 driver bridge conservation is invalid")

    keys = (
        "demand_delta_usd",
        "route_delta_usd",
        "execution_delta_usd",
        "price_delta_usd",
        "residual_delta_usd",
        "observed_delta_usd",
    )
    parsed: dict[str, Decimal | None] = {}
    for key in keys:
        raw = value.get(key)
        if raw is None:
            parsed[key] = None
            continue
        if not isinstance(raw, str):
            raise ValueError(f"S4 driver bridge {key} must be a decimal string or null")
        try:
            decimal = Decimal(raw)
        except Exception as exc:
            raise ValueError(f"S4 driver bridge {key} is invalid") from exc
        if not decimal.is_finite() or _decimal_text(decimal) != raw:
            raise ValueError(f"S4 driver bridge {key} is not canonical")
        parsed[key] = decimal

    if status == "complete" or value.get("conservation") == "pass":
        if any(parsed[key] is None for key in keys):
            raise ValueError("conserving S4 driver bridge cannot contain null deltas")
        recomposed = sum(
            (parsed[key] for key in keys[:-1] if parsed[key] is not None),
            Decimal("0"),
        )
        if recomposed != parsed["observed_delta_usd"]:
            raise ValueError("S4 driver bridge arithmetic does not conserve")

    execution = value.get("execution_evidence")
    outcome = value.get("outcome_constraint")
    if not isinstance(execution, Mapping) or not isinstance(outcome, Mapping):
        raise ValueError("S4 driver bridge execution/outcome evidence is missing")
    if outcome.get("note") != "outcome_is_quality_constraint_not_fifth_cost_driver":
        raise ValueError("S4 driver bridge outcome must remain a constraint layer")
