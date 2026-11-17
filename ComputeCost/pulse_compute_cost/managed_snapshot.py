"""Exact, conserving monthly managed-cost snapshots."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
import calendar
from decimal import Decimal, InvalidOperation
import hashlib
import json
import re
from typing import Any, Iterable, Mapping

from .events import UNKNOWN, UsageEvent, parse_timestamp, utc_text
from .openrouter import OpenRouterPriceSnapshot
from .valuation import (
    ValuationError,
    allocate_subscription_cents,
    estimate_reference_cost,
    settle_managed_cost,
)


SCHEMA = "managed_cost_snapshot.v1"
PRO_SUBSCRIPTION_CENTS = 20_000
_MONTH_RE = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")
_EVENT_ENCODER = json.JSONEncoder(
    separators=(",", ":"),
    ensure_ascii=False,
).encode


class ManagedCostSnapshotError(ValueError):
    """Raised when a cost snapshot would be ambiguous or non-conserving."""


def _decimal(value: object, field: str) -> Decimal:
    if isinstance(value, bool):
        raise ManagedCostSnapshotError(f"{field} must be a non-negative decimal string")
    try:
        parsed = Decimal(value) if isinstance(value, str) else Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ManagedCostSnapshotError(
            f"{field} must be a non-negative decimal string"
        ) from exc
    if not parsed.is_finite() or parsed < 0:
        raise ManagedCostSnapshotError(f"{field} must be non-negative and finite")
    return parsed


def _decimal_text(value: Decimal) -> str:
    if value == 0:
        return "0"
    rendered = format(value, "f")
    if "." in rendered:
        rendered = rendered.rstrip("0").rstrip(".")
    return rendered


def _canonical_bytes(value: Mapping[str, Any]) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")


def _digest(prefix: str, value: Mapping[str, Any]) -> str:
    return prefix + hashlib.sha256(_canonical_bytes(value)).hexdigest()


def _event_set_digest(events: Iterable[UsageEvent]) -> str:
    """Hash ordered immutable facts without materializing a giant JSON tree."""

    digest = hashlib.sha256(b"usage_event_set.v1\0")
    for event in events:
        # This compact ordered codec contains the same immutable base facts as
        # UsageEvent.semantic_dict(). Observation placement and local source
        # position are intentionally excluded. Derived month/quality fields do
        # not need duplication because their inputs are committed here.
        semantic_facts = [
            event.event_id,
            utc_text(event.occurred_utc),
            event.machine,
            event.agent,
            event.consumer_path,
            event.task_ref,
            event.work_item_ref,
            event.outcome_ref,
            event.source,
            event.runtime,
            event.provider,
            event.model,
            event.effort,
            event.tokens.input,
            event.tokens.output,
            event.tokens.cache_read,
            event.tokens.cache_write,
            event.tokens.reasoning,
            event.retry_count,
            event.escalation_count,
            event.base_route,
            event.risk_floor,
            event.effective_route,
            event.verification_result,
            event.outcome_status,
            event.human_correction,
            event.high_risk_error,
            sorted(set(event.quality_flags)),
        ]
        payload = _EVENT_ENCODER(semantic_facts).encode("utf-8")
        digest.update(len(payload).to_bytes(8, "big"))
        digest.update(payload)
    return "uevset1_" + digest.hexdigest()


def _strict_keys(value: Mapping[str, Any], expected: set[str], field: str) -> None:
    if set(value) != expected:
        raise ManagedCostSnapshotError(f"{field} has invalid fields")


def _count(value: object, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ManagedCostSnapshotError(f"{field} must be a non-negative integer")
    return value


def _nonempty(value: object, field: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise ManagedCostSnapshotError(f"{field} must be a trimmed non-empty string")
    return value


@dataclass(frozen=True)
class ManagedCostSnapshot:
    """A validated ``managed_cost_snapshot.v1`` document."""

    document: Mapping[str, Any]

    def __post_init__(self) -> None:
        validate_managed_cost_snapshot(self.document)

    @property
    def snapshot_id(self) -> str:
        return str(self.document["snapshot_id"])

    def to_dict(self) -> dict[str, Any]:
        # JSON round-trip provides a defensive copy and is safe because the
        # contract contains only JSON scalars, lists, and objects.
        return json.loads(json.dumps(self.document, ensure_ascii=False))

    @classmethod
    def from_dict(cls, value: object) -> "ManagedCostSnapshot":
        if not isinstance(value, Mapping):
            raise ManagedCostSnapshotError("managed cost snapshot must be an object")
        return cls(document=dict(value))


def _price_identity(price: OpenRouterPriceSnapshot) -> dict[str, Any]:
    try:
        retrieved_at = utc_text(parse_timestamp(price.retrieved_at, "price.retrieved_at"))
    except ValueError as exc:
        raise ManagedCostSnapshotError(str(exc)) from exc
    value = {
        "source": "openrouter.models",
        "model_id": _nonempty(price.model_id, "price.model_id"),
        "canonical_slug": _nonempty(price.canonical_slug, "price.canonical_slug"),
        "retrieved_at": retrieved_at,
        "currency": "USD",
        "unit": "per_token",
        "pricing": {
            "prompt": _decimal_text(_decimal(price.prompt_per_token, "price.pricing.prompt")),
            "completion": _decimal_text(
                _decimal(price.completion_per_token, "price.pricing.completion")
            ),
            "input_cache_read": (
                _decimal_text(
                    _decimal(
                        price.input_cache_read_per_token,
                        "price.pricing.input_cache_read",
                    )
                )
                if price.input_cache_read_per_token is not None
                else None
            ),
        },
    }
    return {"snapshot_id": _digest("orprice1_", value), **value}


def build_managed_cost_snapshot(
    *,
    product_month: str,
    events: Iterable[UsageEvent],
    price: OpenRouterPriceSnapshot,
    generated_at: str | datetime,
    settlement_status: str = "provisional",
    actual_openrouter_usd: Decimal | int | str | None = None,
    actual_openrouter_coverage: str | None = None,
    actual_openrouter_evidence: Mapping[str, object] | None = None,
    source_coverage_complete: bool = False,
) -> ManagedCostSnapshot:
    """Value one Beijing product month without hiding unknown-source usage."""

    if not isinstance(product_month, str) or _MONTH_RE.fullmatch(product_month) is None:
        raise ManagedCostSnapshotError("product_month must be YYYY-MM")
    if settlement_status not in {"provisional", "closed"}:
        raise ManagedCostSnapshotError(
            "settlement_status must be provisional or closed"
        )
    if not isinstance(source_coverage_complete, bool):
        raise ManagedCostSnapshotError("source_coverage_complete must be boolean")
    if actual_openrouter_usd is None:
        if actual_openrouter_coverage not in {None, "unavailable"}:
            raise ManagedCostSnapshotError(
                "actual_openrouter_coverage requires a cash amount"
            )
        cash_coverage = "unavailable"
        if actual_openrouter_evidence is not None:
            raise ManagedCostSnapshotError(
                "actual_openrouter_evidence requires a cash amount"
            )
        cash_evidence = None
    else:
        cash_coverage = actual_openrouter_coverage or "partial"
        if cash_coverage not in {"partial", "complete"}:
            raise ManagedCostSnapshotError(
                "actual_openrouter_coverage must be partial or complete when cash is present"
            )
        cash_evidence = _validate_cash_evidence(
            actual_openrouter_evidence,
            expected_coverage=cash_coverage,
            product_month=product_month,
            settlement_status=settlement_status,
        )
    try:
        if isinstance(generated_at, datetime):
            generated_dt = parse_timestamp(utc_text(generated_at), "generated_at")
        else:
            generated_dt = parse_timestamp(generated_at, "generated_at")
        generated = utc_text(generated_dt)
    except ValueError as exc:
        raise ManagedCostSnapshotError(str(exc)) from exc

    rows = list(events)
    if any(not isinstance(event, UsageEvent) for event in rows):
        raise ManagedCostSnapshotError("events must contain UsageEvent values")
    # v1 settles only the ChatGPT Pro subscription. A Claude subscription has
    # no settlement here and must not be counted as API or Pro reference cost.
    if any(event.source == "claude_subscription" for event in rows):
        raise ManagedCostSnapshotError(
            "claude_subscription events have no managed_cost_snapshot.v1 settlement"
        )
    ordered_rows = sorted(rows, key=lambda item: item.event_id)
    event_ids = [event.event_id for event in rows]
    if len(set(event_ids)) != len(event_ids):
        raise ManagedCostSnapshotError("duplicate event_id in snapshot input")
    reserved_agents = sorted({
        event.agent for event in rows if event.agent in {"unattributed", "unallocated"}
    })
    if reserved_agents:
        raise ManagedCostSnapshotError(
            f"event agent uses reserved cost partition: {reserved_agents}"
        )
    outside = sorted({event.product_month for event in rows if event.product_month != product_month})
    if outside:
        raise ManagedCostSnapshotError(
            f"all events must belong to product_month {product_month}; found {outside}"
        )
    try:
        price_retrieved_at = parse_timestamp(price.retrieved_at, "price.retrieved_at")
    except ValueError as exc:
        raise ManagedCostSnapshotError(str(exc)) from exc
    future_evidence = [
        event.event_id
        for event in rows
        if event.observed_utc > generated_dt
    ]
    if price_retrieved_at > generated_dt or future_evidence:
        raise ManagedCostSnapshotError(
            "generated_at cannot precede price or event observation evidence"
        )

    agents: dict[str, dict[str, Any]] = {}
    source_counts = {"chatgpt_pro": 0, "openrouter_api": 0, "unknown": 0}
    priced_event_count = 0
    unpriced_event_count = 0
    partial_event_count = 0
    api_reference = Decimal("0")
    pro_reference = Decimal("0")

    def agent_row(agent: str) -> dict[str, Any]:
        return agents.setdefault(
            agent,
            {
                "event_count": 0,
                "priced_event_count": 0,
                "unpriced_event_count": 0,
                "api_reference": Decimal("0"),
                "pro_reference": Decimal("0"),
            },
        )

    for event in ordered_rows:
        source_counts[event.source] += 1
        partition = "unattributed" if event.agent == UNKNOWN else event.agent
        bucket = agent_row(partition)
        bucket["event_count"] += 1
        if event.has_missing_fields or event.quality_flags:
            partial_event_count += 1
        if event.source == UNKNOWN:
            unpriced_event_count += 1
            bucket["unpriced_event_count"] += 1
            continue
        try:
            reference = estimate_reference_cost(
                input_tokens=event.tokens.input,
                output_tokens=event.tokens.output,
                cache_read_tokens=event.tokens.cache_read,
                price=price,
            )
        except ValuationError as exc:
            raise ManagedCostSnapshotError(
                f"event {event.event_id} cannot be priced: {exc}"
            ) from exc
        priced_event_count += 1
        bucket["priced_event_count"] += 1
        if event.source == "chatgpt_pro":
            pro_reference += reference
            bucket["pro_reference"] += reference
        else:
            api_reference += reference
            bucket["api_reference"] += reference

    pro_weights = {
        agent: bucket["pro_reference"]
        for agent, bucket in agents.items()
        if bucket["pro_reference"] != 0
    }
    try:
        allocation = allocate_subscription_cents(
            pro_weights,
            total_cents=PRO_SUBSCRIPTION_CENTS,
        )
        settlement = settle_managed_cost(
            api_reference_cost_usd=api_reference,
            pro_reference_cost_usd=pro_reference,
            pro_allocation=allocation,
            actual_openrouter_usd=actual_openrouter_usd,
        )
    except ValuationError as exc:
        raise ManagedCostSnapshotError(str(exc)) from exc

    by_agent: list[dict[str, Any]] = []
    for agent, bucket in sorted(agents.items()):
        allocated_cents = allocation.allocations_cents.get(agent, 0)
        managed = bucket["api_reference"] + Decimal(allocated_cents) / Decimal(100)
        by_agent.append({
            "agent": agent,
            "event_count": bucket["event_count"],
            "priced_event_count": bucket["priced_event_count"],
            "unpriced_event_count": bucket["unpriced_event_count"],
            "api_reference_cost_usd": _decimal_text(bucket["api_reference"]),
            "pro_reference_cost_usd": _decimal_text(bucket["pro_reference"]),
            "pro_allocated_cents": allocated_cents,
            "unallocated_cents": 0,
            "managed_cost_usd": _decimal_text(managed),
        })
    if allocation.unallocated_cents:
        by_agent.append({
            "agent": "unallocated",
            "event_count": 0,
            "priced_event_count": 0,
            "unpriced_event_count": 0,
            "api_reference_cost_usd": "0",
            "pro_reference_cost_usd": "0",
            "pro_allocated_cents": 0,
            "unallocated_cents": allocation.unallocated_cents,
            "managed_cost_usd": _decimal_text(
                Decimal(allocation.unallocated_cents) / Decimal(100)
            ),
        })

    quality_flags: list[str] = []
    if not source_coverage_complete:
        quality_flags.append("source_coverage_incomplete")
    if partial_event_count:
        quality_flags.append("event_evidence_partial")
    if unpriced_event_count:
        quality_flags.append("unpriced_events")
    if actual_openrouter_usd is None:
        quality_flags.append("actual_openrouter_cash_unavailable")
    elif cash_coverage == "partial":
        quality_flags.append("actual_openrouter_cash_partial")

    quality_flags.sort()
    document: dict[str, Any] = {
        "schema": SCHEMA,
        "schema_version": 1,
        "settlement_status": settlement_status,
        "product_month": product_month,
        "timezone": "Asia/Shanghai",
        "generated_at": generated,
        "input_identity": {
            "schema": "usage_event.v1",
            "event_count": len(rows),
            "event_set_digest": _event_set_digest(ordered_rows),
        },
        "actual_openrouter_evidence": cash_evidence,
        "price_snapshot": _price_identity(price),
        "coverage": {
            "source_complete": source_coverage_complete,
            "event_count": len(rows),
            "priced_event_count": priced_event_count,
            "unpriced_event_count": unpriced_event_count,
            "partial_event_count": partial_event_count,
            "source_event_counts": source_counts,
            "actual_openrouter": cash_coverage,
        },
        "totals": {
            "gross_reference_cost_usd": _decimal_text(settlement.gross_reference_cost_usd),
            "api_reference_cost_usd": _decimal_text(settlement.api_reference_cost_usd),
            "pro_reference_cost_usd": _decimal_text(settlement.pro_reference_cost_usd),
            "pro_settled_cost_usd": _decimal_text(settlement.pro_settled_cost_usd),
            "pro_settlement_adjustment_usd": _decimal_text(
                settlement.pro_settlement_adjustment_usd
            ),
            "managed_cost_usd": _decimal_text(settlement.managed_cost_usd),
            "actual_openrouter_usd": (
                _decimal_text(settlement.actual_openrouter_usd)
                if settlement.actual_openrouter_usd is not None
                else None
            ),
            "pro_allocated_cents": sum(allocation.allocations_cents.values()),
            "unallocated_cents": allocation.unallocated_cents,
        },
        "by_agent": by_agent,
        "quality": {
            "status": "complete" if not quality_flags else "partial",
            "flags": quality_flags,
        },
    }
    document["snapshot_id"] = _digest("mcs1_", document)
    return ManagedCostSnapshot(document=document)


def validate_managed_cost_snapshot(value: object) -> None:
    """Strictly validate shape, identity, and all conservation equations."""

    if not isinstance(value, Mapping):
        raise ManagedCostSnapshotError("managed cost snapshot must be an object")
    _strict_keys(
        value,
        {
            "schema", "schema_version", "snapshot_id", "settlement_status",
            "product_month", "timezone", "generated_at", "input_identity",
            "actual_openrouter_evidence", "price_snapshot",
            "coverage", "totals", "by_agent", "quality",
        },
        "snapshot",
    )
    if value.get("schema") != SCHEMA or value.get("schema_version") != 1:
        raise ManagedCostSnapshotError("unsupported managed cost snapshot schema")
    if value.get("settlement_status") not in {"provisional", "closed"}:
        raise ManagedCostSnapshotError("invalid settlement_status")
    month = value.get("product_month")
    if not isinstance(month, str) or _MONTH_RE.fullmatch(month) is None:
        raise ManagedCostSnapshotError("invalid product_month")
    if value.get("timezone") != "Asia/Shanghai":
        raise ManagedCostSnapshotError("timezone must be Asia/Shanghai")
    try:
        validated_generated_at = parse_timestamp(value.get("generated_at"), "generated_at")
        normalized_generated = utc_text(validated_generated_at)
    except ValueError as exc:
        raise ManagedCostSnapshotError(str(exc)) from exc
    if value.get("generated_at") != normalized_generated:
        raise ManagedCostSnapshotError("generated_at must be normalized UTC")

    input_identity = value.get("input_identity")
    if not isinstance(input_identity, Mapping):
        raise ManagedCostSnapshotError("input_identity must be an object")
    _strict_keys(
        input_identity,
        {"schema", "event_count", "event_set_digest"},
        "input_identity",
    )
    if input_identity.get("schema") != "usage_event.v1":
        raise ManagedCostSnapshotError("input_identity schema is invalid")
    input_event_count = _count(
        input_identity.get("event_count"), "input_identity.event_count"
    )
    input_digest = input_identity.get("event_set_digest")
    if (
        not isinstance(input_digest, str)
        or re.fullmatch(r"uevset1_[0-9a-f]{64}", input_digest) is None
    ):
        raise ManagedCostSnapshotError("input_identity.event_set_digest is invalid")

    raw_cash_evidence = value.get("actual_openrouter_evidence")

    price = value.get("price_snapshot")
    if not isinstance(price, Mapping):
        raise ManagedCostSnapshotError("price_snapshot must be an object")
    _strict_keys(
        price,
        {"snapshot_id", "source", "model_id", "canonical_slug", "retrieved_at", "currency", "unit", "pricing"},
        "price_snapshot",
    )
    if price.get("source") != "openrouter.models" or price.get("currency") != "USD" or price.get("unit") != "per_token":
        raise ManagedCostSnapshotError("price_snapshot identity is invalid")
    _nonempty(price.get("model_id"), "price_snapshot.model_id")
    _nonempty(price.get("canonical_slug"), "price_snapshot.canonical_slug")
    try:
        normalized_retrieved = utc_text(parse_timestamp(price.get("retrieved_at"), "price_snapshot.retrieved_at"))
    except ValueError as exc:
        raise ManagedCostSnapshotError(str(exc)) from exc
    if price.get("retrieved_at") != normalized_retrieved:
        raise ManagedCostSnapshotError("price_snapshot.retrieved_at must be normalized UTC")
    if parse_timestamp(normalized_retrieved, "price_snapshot.retrieved_at") > validated_generated_at:
        raise ManagedCostSnapshotError("generated_at cannot precede price evidence")
    pricing = price.get("pricing")
    if not isinstance(pricing, Mapping):
        raise ManagedCostSnapshotError("price_snapshot.pricing must be an object")
    _strict_keys(pricing, {"prompt", "completion", "input_cache_read"}, "price_snapshot.pricing")
    for field in ("prompt", "completion"):
        raw = pricing.get(field)
        if not isinstance(raw, str) or _decimal_text(_decimal(raw, f"price_snapshot.pricing.{field}")) != raw:
            raise ManagedCostSnapshotError(f"price_snapshot.pricing.{field} is not canonical")
    cache_raw = pricing.get("input_cache_read")
    if cache_raw is not None and (
        not isinstance(cache_raw, str)
        or _decimal_text(_decimal(cache_raw, "price_snapshot.pricing.input_cache_read")) != cache_raw
    ):
        raise ManagedCostSnapshotError("price_snapshot.pricing.input_cache_read is not canonical")
    price_without_id = dict(price)
    price_id = price_without_id.pop("snapshot_id", None)
    if price_id != _digest("orprice1_", price_without_id):
        raise ManagedCostSnapshotError("price_snapshot.snapshot_id does not match content")

    coverage = value.get("coverage")
    if not isinstance(coverage, Mapping):
        raise ManagedCostSnapshotError("coverage must be an object")
    _strict_keys(
        coverage,
        {"source_complete", "event_count", "priced_event_count", "unpriced_event_count", "partial_event_count", "source_event_counts", "actual_openrouter"},
        "coverage",
    )
    if not isinstance(coverage.get("source_complete"), bool):
        raise ManagedCostSnapshotError("coverage.source_complete must be boolean")
    event_count = _count(coverage.get("event_count"), "coverage.event_count")
    priced_count = _count(coverage.get("priced_event_count"), "coverage.priced_event_count")
    unpriced_count = _count(coverage.get("unpriced_event_count"), "coverage.unpriced_event_count")
    partial_count = _count(coverage.get("partial_event_count"), "coverage.partial_event_count")
    if priced_count + unpriced_count != event_count or partial_count > event_count:
        raise ManagedCostSnapshotError("coverage event counts do not conserve")
    if input_event_count != event_count:
        raise ManagedCostSnapshotError("input identity and coverage counts disagree")
    source_counts = coverage.get("source_event_counts")
    if not isinstance(source_counts, Mapping):
        raise ManagedCostSnapshotError("coverage.source_event_counts must be an object")
    _strict_keys(source_counts, {"chatgpt_pro", "openrouter_api", "unknown"}, "coverage.source_event_counts")
    parsed_source_counts = {
        key: _count(source_counts.get(key), f"coverage.source_event_counts.{key}")
        for key in ("chatgpt_pro", "openrouter_api", "unknown")
    }
    if sum(parsed_source_counts.values()) != event_count or parsed_source_counts["unknown"] != unpriced_count:
        raise ManagedCostSnapshotError("coverage source counts do not conserve")
    if coverage.get("actual_openrouter") not in {"unavailable", "partial", "complete"}:
        raise ManagedCostSnapshotError("coverage.actual_openrouter is invalid")

    totals = value.get("totals")
    if not isinstance(totals, Mapping):
        raise ManagedCostSnapshotError("totals must be an object")
    decimal_total_fields = {
        "gross_reference_cost_usd", "api_reference_cost_usd",
        "pro_reference_cost_usd", "pro_settled_cost_usd",
        "pro_settlement_adjustment_usd", "managed_cost_usd",
    }
    _strict_keys(
        totals,
        decimal_total_fields | {"actual_openrouter_usd", "pro_allocated_cents", "unallocated_cents"},
        "totals",
    )
    decimals: dict[str, Decimal] = {}
    for field in decimal_total_fields:
        raw = totals.get(field)
        # The settlement adjustment may be negative; validate separately.
        if field == "pro_settlement_adjustment_usd":
            if not isinstance(raw, str):
                raise ManagedCostSnapshotError(f"totals.{field} must be a decimal string")
            try:
                parsed = Decimal(raw)
            except InvalidOperation as exc:
                raise ManagedCostSnapshotError(f"totals.{field} must be a decimal string") from exc
            if not parsed.is_finite() or _decimal_text(parsed) != raw:
                raise ManagedCostSnapshotError(f"totals.{field} is not canonical")
            decimals[field] = parsed
        else:
            if not isinstance(raw, str):
                raise ManagedCostSnapshotError(f"totals.{field} must be a decimal string")
            parsed = _decimal(raw, f"totals.{field}")
            if _decimal_text(parsed) != raw:
                raise ManagedCostSnapshotError(f"totals.{field} is not canonical")
            decimals[field] = parsed
    actual = totals.get("actual_openrouter_usd")
    if actual is not None and (
        not isinstance(actual, str)
        or _decimal_text(_decimal(actual, "totals.actual_openrouter_usd")) != actual
    ):
        raise ManagedCostSnapshotError("totals.actual_openrouter_usd is not canonical")
    if (actual is None) != (coverage.get("actual_openrouter") == "unavailable"):
        raise ManagedCostSnapshotError("actual OpenRouter coverage does not match totals")
    if actual is None:
        if raw_cash_evidence is not None:
            raise ManagedCostSnapshotError("cash evidence requires an actual amount")
    else:
        _validate_cash_evidence(
            raw_cash_evidence,
            expected_coverage=str(coverage["actual_openrouter"]),
            product_month=str(month),
            settlement_status=str(value["settlement_status"]),
        )
    pro_allocated = _count(totals.get("pro_allocated_cents"), "totals.pro_allocated_cents")
    unallocated = _count(totals.get("unallocated_cents"), "totals.unallocated_cents")
    if pro_allocated + unallocated != PRO_SUBSCRIPTION_CENTS:
        raise ManagedCostSnapshotError("Pro cents must conserve exactly 20000")
    if decimals["gross_reference_cost_usd"] != decimals["api_reference_cost_usd"] + decimals["pro_reference_cost_usd"]:
        raise ManagedCostSnapshotError("gross reference cost does not conserve")
    if decimals["pro_settled_cost_usd"] != Decimal(PRO_SUBSCRIPTION_CENTS) / Decimal(100):
        raise ManagedCostSnapshotError("Pro settled cost must equal 200 USD")
    if decimals["pro_settlement_adjustment_usd"] != decimals["pro_settled_cost_usd"] - decimals["pro_reference_cost_usd"]:
        raise ManagedCostSnapshotError("Pro settlement adjustment is inconsistent")
    if decimals["managed_cost_usd"] != decimals["api_reference_cost_usd"] + decimals["pro_settled_cost_usd"]:
        raise ManagedCostSnapshotError("managed cost does not conserve")

    agent_rows = value.get("by_agent")
    if not isinstance(agent_rows, list):
        raise ManagedCostSnapshotError("by_agent must be a list")
    seen: set[str] = set()
    sums = {
        "event_count": 0, "priced_event_count": 0, "unpriced_event_count": 0,
        "api": Decimal("0"), "pro": Decimal("0"), "allocated": 0,
        "unallocated": 0, "managed": Decimal("0"),
    }
    previous_agent: str | None = None
    for index, row in enumerate(agent_rows):
        if not isinstance(row, Mapping):
            raise ManagedCostSnapshotError(f"by_agent[{index}] must be an object")
        _strict_keys(
            row,
            {"agent", "event_count", "priced_event_count", "unpriced_event_count", "api_reference_cost_usd", "pro_reference_cost_usd", "pro_allocated_cents", "unallocated_cents", "managed_cost_usd"},
            f"by_agent[{index}]",
        )
        agent = _nonempty(row.get("agent"), f"by_agent[{index}].agent")
        if agent in seen or (previous_agent == "unallocated"):
            raise ManagedCostSnapshotError("by_agent keys must be unique with unallocated last")
        if agent != "unallocated" and previous_agent is not None and agent <= previous_agent:
            raise ManagedCostSnapshotError("by_agent keys must be sorted with unallocated last")
        seen.add(agent)
        previous_agent = agent
        row_events = _count(row.get("event_count"), f"by_agent[{index}].event_count")
        row_priced = _count(row.get("priced_event_count"), f"by_agent[{index}].priced_event_count")
        row_unpriced = _count(row.get("unpriced_event_count"), f"by_agent[{index}].unpriced_event_count")
        if row_priced + row_unpriced != row_events:
            raise ManagedCostSnapshotError(f"by_agent[{index}] event counts do not conserve")
        api = _decimal(row.get("api_reference_cost_usd"), f"by_agent[{index}].api_reference_cost_usd")
        pro = _decimal(row.get("pro_reference_cost_usd"), f"by_agent[{index}].pro_reference_cost_usd")
        managed = _decimal(row.get("managed_cost_usd"), f"by_agent[{index}].managed_cost_usd")
        for field, parsed in (
            ("api_reference_cost_usd", api), ("pro_reference_cost_usd", pro),
            ("managed_cost_usd", managed),
        ):
            if not isinstance(row.get(field), str) or _decimal_text(parsed) != row.get(field):
                raise ManagedCostSnapshotError(f"by_agent[{index}].{field} is not canonical")
        allocated = _count(row.get("pro_allocated_cents"), f"by_agent[{index}].pro_allocated_cents")
        row_unallocated = _count(row.get("unallocated_cents"), f"by_agent[{index}].unallocated_cents")
        if agent == "unallocated":
            if row_events or api or pro or allocated or managed != Decimal(row_unallocated) / Decimal(100):
                raise ManagedCostSnapshotError("unallocated partition is invalid")
        elif row_unallocated or managed != api + Decimal(allocated) / Decimal(100):
            raise ManagedCostSnapshotError(f"by_agent[{index}] managed cost is inconsistent")
        sums["event_count"] += row_events
        sums["priced_event_count"] += row_priced
        sums["unpriced_event_count"] += row_unpriced
        sums["api"] += api
        sums["pro"] += pro
        sums["allocated"] += allocated
        sums["unallocated"] += row_unallocated
        sums["managed"] += managed
    if (
        sums["event_count"] != event_count
        or sums["priced_event_count"] != priced_count
        or sums["unpriced_event_count"] != unpriced_count
        or sums["api"] != decimals["api_reference_cost_usd"]
        or sums["pro"] != decimals["pro_reference_cost_usd"]
        or sums["allocated"] != pro_allocated
        or sums["unallocated"] != unallocated
        or sums["managed"] != decimals["managed_cost_usd"]
    ):
        raise ManagedCostSnapshotError("by_agent partitions do not conserve totals")

    quality = value.get("quality")
    if not isinstance(quality, Mapping):
        raise ManagedCostSnapshotError("quality must be an object")
    _strict_keys(quality, {"status", "flags"}, "quality")
    flags = quality.get("flags")
    if not isinstance(flags, list) or any(not isinstance(flag, str) or not flag for flag in flags):
        raise ManagedCostSnapshotError("quality.flags must be non-empty strings")
    if flags != sorted(set(flags)):
        raise ManagedCostSnapshotError("quality.flags must be sorted and unique")
    expected_quality = "complete" if not flags else "partial"
    if quality.get("status") != expected_quality:
        raise ManagedCostSnapshotError("quality.status does not match flags")
    required_flags = {
        "source_coverage_incomplete": not coverage["source_complete"],
        "event_evidence_partial": partial_count > 0,
        "unpriced_events": unpriced_count > 0,
        "actual_openrouter_cash_unavailable": actual is None,
        "actual_openrouter_cash_partial": coverage["actual_openrouter"] == "partial",
    }
    for flag, required in required_flags.items():
        if (flag in flags) != required:
            raise ManagedCostSnapshotError(f"quality flag {flag} is inconsistent")

    snapshot_without_id = dict(value)
    snapshot_id = snapshot_without_id.pop("snapshot_id", None)
    if snapshot_id != _digest("mcs1_", snapshot_without_id):
        raise ManagedCostSnapshotError("snapshot_id does not match content")


def _validate_cash_evidence(
    value: object,
    *,
    expected_coverage: str,
    product_month: str,
    settlement_status: str,
) -> dict[str, object]:
    if not isinstance(value, Mapping):
        raise ManagedCostSnapshotError(
            "actual_openrouter_evidence is required when cash is present"
        )
    _strict_keys(
        value,
        {"source", "snapshot_digest", "window_start_utc_day", "window_end_utc_day", "coverage"},
        "actual_openrouter_evidence",
    )
    if value.get("source") != "openrouter.activity":
        raise ManagedCostSnapshotError("cash evidence source must be openrouter.activity")
    digest = value.get("snapshot_digest")
    if not isinstance(digest, str) or re.fullmatch(r"oractivity1_[0-9a-f]{64}", digest) is None:
        raise ManagedCostSnapshotError("cash evidence snapshot_digest is invalid")
    try:
        start = date.fromisoformat(str(value.get("window_start_utc_day")))
        end = date.fromisoformat(str(value.get("window_end_utc_day")))
    except ValueError as exc:
        raise ManagedCostSnapshotError("cash evidence window must use ISO UTC days") from exc
    if start > end:
        raise ManagedCostSnapshotError("cash evidence window is reversed")
    if value.get("coverage") != expected_coverage or expected_coverage not in {"partial", "complete"}:
        raise ManagedCostSnapshotError("cash evidence coverage is inconsistent")
    if expected_coverage == "complete":
        if settlement_status != "closed":
            raise ManagedCostSnapshotError(
                "complete cash coverage requires a closed monthly settlement"
            )
        year, month = (int(part) for part in product_month.split("-"))
        required_start = date(year, month, 1)
        # Beijing midnight is 16:00 on the preceding UTC day. Full UTC-day
        # activity must therefore start one day earlier to cover the boundary.
        from datetime import timedelta
        required_start -= timedelta(days=1)
        required_end = date(year, month, calendar.monthrange(year, month)[1])
        if start > required_start or end < required_end:
            raise ManagedCostSnapshotError(
                "complete cash evidence does not cover the full Beijing month"
            )
    return {
        "source": "openrouter.activity",
        "snapshot_digest": digest,
        "window_start_utc_day": start.isoformat(),
        "window_end_utc_day": end.isoformat(),
        "coverage": expected_coverage,
    }
