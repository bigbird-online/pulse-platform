"""Strict, read-only projection of ComputeCost's shadow snapshot."""

from __future__ import annotations

from datetime import date, datetime, timezone
import calendar
from decimal import Decimal, InvalidOperation
import hashlib
import json
from pathlib import Path
import re
from typing import Any, Mapping, Optional
from zoneinfo import ZoneInfo

from .live_contract import _metric


SCHEMA = "managed_cost_snapshot.v1"
SUMMARY_METRIC_ID = "compute_cost.shadow.summary"
BY_AGENT_METRIC_ID = "compute_cost.shadow.by_agent"
MAX_SNAPSHOT_BYTES = 2 * 1024 * 1024
MAX_PROVISIONAL_AGE_SECONDS = 45 * 60
MAX_CLOCK_SKEW_SECONDS = 5 * 60
_MONTH_RE = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")
_BEIJING = ZoneInfo("Asia/Shanghai")


class ManagedCostShadowError(ValueError):
    """A shadow snapshot is absent or cannot be trusted as a whole."""


def _strict_keys(value: Mapping[str, Any], expected: set[str], field: str) -> None:
    if set(value) != expected:
        raise ManagedCostShadowError(f"{field} has invalid fields")


def _count(value: object, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ManagedCostShadowError(f"{field} must be a non-negative integer")
    return value


def _text(value: object, field: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise ManagedCostShadowError(f"{field} must be a trimmed non-empty string")
    return value


def _decimal(value: object, field: str, *, negative: bool = False) -> Decimal:
    if not isinstance(value, str):
        raise ManagedCostShadowError(f"{field} must be a canonical decimal string")
    try:
        parsed = Decimal(value)
    except InvalidOperation as exc:
        raise ManagedCostShadowError(f"{field} must be a decimal string") from exc
    if not parsed.is_finite() or (not negative and parsed < 0):
        raise ManagedCostShadowError(f"{field} must be finite and non-negative")
    canonical = format(parsed, "f")
    if parsed == 0:
        canonical = "0"
    elif "." in canonical:
        canonical = canonical.rstrip("0").rstrip(".")
    if value != canonical:
        raise ManagedCostShadowError(f"{field} must be canonical fixed decimal")
    return parsed


def _timestamp(value: object, field: str) -> datetime:
    raw = _text(value, field)
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ManagedCostShadowError(f"{field} must be an ISO timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ManagedCostShadowError(f"{field} must be timezone-aware")
    normalized = parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    if raw != normalized:
        raise ManagedCostShadowError(f"{field} must be normalized UTC")
    return parsed.astimezone(timezone.utc)


def _canonical_digest(prefix: str, value: Mapping[str, Any]) -> str:
    payload = json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
    ).encode("utf-8")
    return prefix + hashlib.sha256(payload).hexdigest()


def validate_managed_cost_shadow(value: object) -> Mapping[str, Any]:
    """Validate the consumer boundary without importing ComputeCost code."""

    if not isinstance(value, Mapping):
        raise ManagedCostShadowError("snapshot must be an object")
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
        raise ManagedCostShadowError("unsupported snapshot schema")
    if value.get("settlement_status") not in {"provisional", "closed"}:
        raise ManagedCostShadowError("invalid settlement status")
    month = value.get("product_month")
    if not isinstance(month, str) or _MONTH_RE.fullmatch(month) is None:
        raise ManagedCostShadowError("invalid product month")
    if value.get("timezone") != "Asia/Shanghai":
        raise ManagedCostShadowError("snapshot timezone must be Asia/Shanghai")
    generated_at = _timestamp(value.get("generated_at"), "generated_at")

    input_identity = value.get("input_identity")
    if not isinstance(input_identity, Mapping):
        raise ManagedCostShadowError("input_identity must be an object")
    _strict_keys(
        input_identity,
        {"schema", "event_count", "event_set_digest"},
        "input_identity",
    )
    if input_identity.get("schema") != "usage_event.v1":
        raise ManagedCostShadowError("input identity schema is invalid")
    input_event_count = _count(
        input_identity.get("event_count"), "input_identity.event_count"
    )
    input_digest = input_identity.get("event_set_digest")
    if not isinstance(input_digest, str) or re.fullmatch(r"uevset1_[0-9a-f]{64}", input_digest) is None:
        raise ManagedCostShadowError("input event-set digest is invalid")

    price = value.get("price_snapshot")
    if not isinstance(price, Mapping):
        raise ManagedCostShadowError("price_snapshot must be an object")
    _strict_keys(
        price,
        {"snapshot_id", "source", "model_id", "canonical_slug", "retrieved_at", "currency", "unit", "pricing"},
        "price_snapshot",
    )
    if price.get("source") != "openrouter.models" or price.get("currency") != "USD" or price.get("unit") != "per_token":
        raise ManagedCostShadowError("price snapshot identity is invalid")
    _text(price.get("model_id"), "price_snapshot.model_id")
    _text(price.get("canonical_slug"), "price_snapshot.canonical_slug")
    if _timestamp(price.get("retrieved_at"), "price_snapshot.retrieved_at") > generated_at:
        raise ManagedCostShadowError("price snapshot cannot be newer than cost snapshot")
    pricing = price.get("pricing")
    if not isinstance(pricing, Mapping):
        raise ManagedCostShadowError("price_snapshot.pricing must be an object")
    _strict_keys(pricing, {"prompt", "completion", "input_cache_read"}, "price_snapshot.pricing")
    _decimal(pricing.get("prompt"), "price_snapshot.pricing.prompt")
    _decimal(pricing.get("completion"), "price_snapshot.pricing.completion")
    if pricing.get("input_cache_read") is not None:
        _decimal(pricing.get("input_cache_read"), "price_snapshot.pricing.input_cache_read")
    price_content = dict(price)
    price_id = price_content.pop("snapshot_id", None)
    if price_id != _canonical_digest("orprice1_", price_content):
        raise ManagedCostShadowError("price snapshot identity does not match content")

    coverage = value.get("coverage")
    if not isinstance(coverage, Mapping):
        raise ManagedCostShadowError("coverage must be an object")
    _strict_keys(
        coverage,
        {"source_complete", "event_count", "priced_event_count", "unpriced_event_count", "partial_event_count", "source_event_counts", "actual_openrouter"},
        "coverage",
    )
    if not isinstance(coverage.get("source_complete"), bool):
        raise ManagedCostShadowError("coverage.source_complete must be boolean")
    event_count = _count(coverage.get("event_count"), "coverage.event_count")
    priced_count = _count(coverage.get("priced_event_count"), "coverage.priced_event_count")
    unpriced_count = _count(coverage.get("unpriced_event_count"), "coverage.unpriced_event_count")
    partial_count = _count(coverage.get("partial_event_count"), "coverage.partial_event_count")
    if priced_count + unpriced_count != event_count or partial_count > event_count:
        raise ManagedCostShadowError("coverage event counts do not conserve")
    if input_event_count != event_count:
        raise ManagedCostShadowError("input identity and coverage disagree")
    source_counts = coverage.get("source_event_counts")
    if not isinstance(source_counts, Mapping):
        raise ManagedCostShadowError("coverage.source_event_counts must be an object")
    _strict_keys(source_counts, {"chatgpt_pro", "openrouter_api", "unknown"}, "coverage.source_event_counts")
    source_total = sum(
        _count(source_counts.get(source), f"coverage.source_event_counts.{source}")
        for source in ("chatgpt_pro", "openrouter_api", "unknown")
    )
    if source_total != event_count or source_counts.get("unknown") != unpriced_count:
        raise ManagedCostShadowError("coverage source counts do not conserve")
    cash_coverage = coverage.get("actual_openrouter")
    if cash_coverage not in {"unavailable", "partial", "complete"}:
        raise ManagedCostShadowError("invalid OpenRouter cash coverage")

    totals = value.get("totals")
    if not isinstance(totals, Mapping):
        raise ManagedCostShadowError("totals must be an object")
    _strict_keys(
        totals,
        {
            "gross_reference_cost_usd", "api_reference_cost_usd",
            "pro_reference_cost_usd", "pro_settled_cost_usd",
            "pro_settlement_adjustment_usd", "managed_cost_usd",
            "actual_openrouter_usd", "pro_allocated_cents", "unallocated_cents",
        },
        "totals",
    )
    gross = _decimal(totals.get("gross_reference_cost_usd"), "totals.gross_reference_cost_usd")
    api = _decimal(totals.get("api_reference_cost_usd"), "totals.api_reference_cost_usd")
    pro = _decimal(totals.get("pro_reference_cost_usd"), "totals.pro_reference_cost_usd")
    settled = _decimal(totals.get("pro_settled_cost_usd"), "totals.pro_settled_cost_usd")
    adjustment = _decimal(
        totals.get("pro_settlement_adjustment_usd"),
        "totals.pro_settlement_adjustment_usd",
        negative=True,
    )
    managed = _decimal(totals.get("managed_cost_usd"), "totals.managed_cost_usd")
    actual = totals.get("actual_openrouter_usd")
    if actual is not None:
        _decimal(actual, "totals.actual_openrouter_usd")
    if (actual is None) != (cash_coverage == "unavailable"):
        raise ManagedCostShadowError("cash amount and coverage disagree")
    cash_evidence = value.get("actual_openrouter_evidence")
    if actual is None:
        if cash_evidence is not None:
            raise ManagedCostShadowError("cash evidence requires an actual amount")
    else:
        if not isinstance(cash_evidence, Mapping):
            raise ManagedCostShadowError("cash evidence is required")
        _strict_keys(
            cash_evidence,
            {"source", "snapshot_digest", "window_start_utc_day", "window_end_utc_day", "coverage"},
            "actual_openrouter_evidence",
        )
        if cash_evidence.get("source") != "openrouter.activity":
            raise ManagedCostShadowError("cash evidence source is invalid")
        cash_digest = cash_evidence.get("snapshot_digest")
        if not isinstance(cash_digest, str) or re.fullmatch(r"oractivity1_[0-9a-f]{64}", cash_digest) is None:
            raise ManagedCostShadowError("cash evidence digest is invalid")
        try:
            cash_start = date.fromisoformat(str(cash_evidence.get("window_start_utc_day")))
            cash_end = date.fromisoformat(str(cash_evidence.get("window_end_utc_day")))
        except ValueError as exc:
            raise ManagedCostShadowError("cash evidence window is invalid") from exc
        if cash_start > cash_end or cash_evidence.get("coverage") != cash_coverage:
            raise ManagedCostShadowError("cash evidence window or coverage is inconsistent")
        if cash_coverage == "complete":
            if value.get("settlement_status") != "closed":
                raise ManagedCostShadowError("complete cash evidence requires closed settlement")
            from datetime import timedelta
            year, product_month_number = (int(part) for part in str(month).split("-"))
            required_start = date(year, product_month_number, 1) - timedelta(days=1)
            required_end = date(
                year,
                product_month_number,
                calendar.monthrange(year, product_month_number)[1],
            )
            if cash_start > required_start or cash_end < required_end:
                raise ManagedCostShadowError("complete cash evidence misses the full month")
    allocated = _count(totals.get("pro_allocated_cents"), "totals.pro_allocated_cents")
    unallocated = _count(totals.get("unallocated_cents"), "totals.unallocated_cents")
    if allocated + unallocated != 20_000:
        raise ManagedCostShadowError("Pro cents do not conserve 20000")
    if gross != api + pro or settled != Decimal("200"):
        raise ManagedCostShadowError("reference or settled totals do not conserve")
    if adjustment != settled - pro or managed != api + settled:
        raise ManagedCostShadowError("managed cost formula does not conserve")

    by_agent = value.get("by_agent")
    if not isinstance(by_agent, list):
        raise ManagedCostShadowError("by_agent must be a list")
    seen: set[str] = set()
    previous: Optional[str] = None
    sum_events = sum_priced = sum_unpriced = sum_allocated = sum_unallocated = 0
    sum_api = Decimal("0")
    sum_pro = Decimal("0")
    sum_managed = Decimal("0")
    for index, row in enumerate(by_agent):
        if not isinstance(row, Mapping):
            raise ManagedCostShadowError(f"by_agent[{index}] must be an object")
        _strict_keys(
            row,
            {"agent", "event_count", "priced_event_count", "unpriced_event_count", "api_reference_cost_usd", "pro_reference_cost_usd", "pro_allocated_cents", "unallocated_cents", "managed_cost_usd"},
            f"by_agent[{index}]",
        )
        agent = _text(row.get("agent"), f"by_agent[{index}].agent")
        if agent in seen or previous == "unallocated":
            raise ManagedCostShadowError("by_agent keys must be unique with unallocated last")
        if agent != "unallocated" and previous is not None and agent <= previous:
            raise ManagedCostShadowError("by_agent keys must be stably sorted")
        seen.add(agent)
        previous = agent
        row_events = _count(row.get("event_count"), f"by_agent[{index}].event_count")
        row_priced = _count(row.get("priced_event_count"), f"by_agent[{index}].priced_event_count")
        row_unpriced = _count(row.get("unpriced_event_count"), f"by_agent[{index}].unpriced_event_count")
        if row_priced + row_unpriced != row_events:
            raise ManagedCostShadowError("by_agent event counts do not conserve")
        row_api = _decimal(row.get("api_reference_cost_usd"), f"by_agent[{index}].api_reference_cost_usd")
        row_pro = _decimal(row.get("pro_reference_cost_usd"), f"by_agent[{index}].pro_reference_cost_usd")
        row_allocated = _count(row.get("pro_allocated_cents"), f"by_agent[{index}].pro_allocated_cents")
        row_unallocated = _count(row.get("unallocated_cents"), f"by_agent[{index}].unallocated_cents")
        row_managed = _decimal(row.get("managed_cost_usd"), f"by_agent[{index}].managed_cost_usd")
        if agent == "unallocated":
            if row_events or row_api or row_pro or row_allocated or row_managed != Decimal(row_unallocated) / 100:
                raise ManagedCostShadowError("unallocated partition is invalid")
        elif row_unallocated or row_managed != row_api + Decimal(row_allocated) / 100:
            raise ManagedCostShadowError("agent managed cost does not conserve")
        sum_events += row_events
        sum_priced += row_priced
        sum_unpriced += row_unpriced
        sum_api += row_api
        sum_pro += row_pro
        sum_allocated += row_allocated
        sum_unallocated += row_unallocated
        sum_managed += row_managed
    if (
        sum_events != event_count or sum_priced != priced_count or sum_unpriced != unpriced_count
        or sum_api != api or sum_pro != pro or sum_allocated != allocated
        or sum_unallocated != unallocated or sum_managed != managed
    ):
        raise ManagedCostShadowError("by_agent partitions do not conserve totals")

    quality = value.get("quality")
    if not isinstance(quality, Mapping):
        raise ManagedCostShadowError("quality must be an object")
    _strict_keys(quality, {"status", "flags"}, "quality")
    flags = quality.get("flags")
    if not isinstance(flags, list) or any(not isinstance(flag, str) or not flag for flag in flags):
        raise ManagedCostShadowError("quality flags are invalid")
    if flags != sorted(set(flags)) or quality.get("status") != ("complete" if not flags else "partial"):
        raise ManagedCostShadowError("quality status and flags disagree")
    required_flags = {
        "source_coverage_incomplete": not coverage["source_complete"],
        "event_evidence_partial": partial_count > 0,
        "unpriced_events": unpriced_count > 0,
        "actual_openrouter_cash_unavailable": actual is None,
        "actual_openrouter_cash_partial": cash_coverage == "partial",
    }
    for flag, required in required_flags.items():
        if (flag in flags) != required:
            raise ManagedCostShadowError(f"quality flag {flag} is inconsistent")

    content = dict(value)
    snapshot_id = content.pop("snapshot_id", None)
    if snapshot_id != _canonical_digest("mcs1_", content):
        raise ManagedCostShadowError("snapshot identity does not match content")
    return value


def load_managed_cost_shadow(path: Path) -> Mapping[str, Any]:
    if not path.is_absolute():
        raise ManagedCostShadowError("shadow path must be absolute")
    try:
        size = path.stat().st_size
        if size > MAX_SNAPSHOT_BYTES:
            raise ManagedCostShadowError("shadow snapshot exceeds size limit")
        parsed = json.loads(path.read_text(encoding="utf-8"))
    except ManagedCostShadowError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ManagedCostShadowError("shadow snapshot is unreadable") from exc
    return validate_managed_cost_shadow(parsed)


def build_managed_cost_shadow_metrics(
    *,
    enabled: bool,
    path: Optional[Path],
    now: datetime,
) -> list[Mapping[str, object]]:
    """Return no metrics when off; when on, fail soft as one atomic bundle."""

    if not enabled:
        return []
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now must be timezone-aware")
    if path is None:
        error = "shadow feature is enabled but its absolute snapshot path is unavailable"
        snapshot = None
    else:
        try:
            snapshot = load_managed_cost_shadow(path)
            error = None
        except ManagedCostShadowError as exc:
            snapshot = None
            error = str(exc)
    if snapshot is not None:
        generated = _timestamp(snapshot["generated_at"], "generated_at")
        expected_month = now.astimezone(_BEIJING).strftime("%Y-%m")
        age = (now.astimezone(timezone.utc) - generated).total_seconds()
        if snapshot["product_month"] != expected_month:
            snapshot = None
            error = "shadow snapshot is not for the current Beijing product month"
        elif snapshot["settlement_status"] != "provisional":
            snapshot = None
            error = "closed historical snapshot cannot feed the current-month shadow"
        elif age > MAX_PROVISIONAL_AGE_SECONDS:
            snapshot = None
            error = "provisional shadow snapshot is stale"
        elif age < -MAX_CLOCK_SKEW_SECONDS:
            snapshot = None
            error = "shadow snapshot timestamp is in the future"
    if snapshot is None:
        return [
            _metric(
                metric_id, "compute_cost.shadow", value=None, unit="list",
                observed_at=None, window="month_cst", quality="unavailable",
                scope="ComputeCost explicit shadow snapshot",
                reason=error,
            )
            for metric_id in (SUMMARY_METRIC_ID, BY_AGENT_METRIC_ID)
        ]

    coverage = snapshot["coverage"]
    totals = snapshot["totals"]
    price = snapshot["price_snapshot"]
    quality = snapshot["quality"]
    observed_at = _timestamp(snapshot["generated_at"], "generated_at")
    metric_quality = "validated" if quality["status"] == "complete" else "descriptive"
    summary = {
        "schema": snapshot["schema"],
        "snapshot_id": snapshot["snapshot_id"],
        "settlement_status": snapshot["settlement_status"],
        "product_month": snapshot["product_month"],
        "timezone": snapshot["timezone"],
        "generated_at": snapshot["generated_at"],
        "usage_event_set_digest": snapshot["input_identity"]["event_set_digest"],
        "price_snapshot_id": price["snapshot_id"],
        "price_model_id": price["model_id"],
        "price_canonical_slug": price["canonical_slug"],
        "price_retrieved_at": price["retrieved_at"],
        "source_complete": coverage["source_complete"],
        "event_count": coverage["event_count"],
        "priced_event_count": coverage["priced_event_count"],
        "unpriced_event_count": coverage["unpriced_event_count"],
        "partial_event_count": coverage["partial_event_count"],
        "actual_openrouter_coverage": coverage["actual_openrouter"],
        "quality_status": quality["status"],
        "quality_flags": ",".join(quality["flags"]),
        **totals,
    }
    cash_evidence = snapshot["actual_openrouter_evidence"]
    if cash_evidence is not None:
        summary.update({
            "actual_openrouter_snapshot_digest": cash_evidence["snapshot_digest"],
            "actual_openrouter_window_start_utc_day": cash_evidence["window_start_utc_day"],
            "actual_openrouter_window_end_utc_day": cash_evidence["window_end_utc_day"],
        })
    reason = (
        None
        if quality["status"] == "complete"
        else "shadow snapshot is partial: " + ",".join(quality["flags"])
    )
    common = {
        "capability_id": "compute_cost.shadow",
        "unit": "list",
        "observed_at": observed_at,
        "window": "month_cst",
        "sample_count": coverage["event_count"],
        "quality": metric_quality,
        "scope": "ComputeCost explicit shadow snapshot; legacy metrics remain authoritative",
        "reason": reason,
        "now": now,
    }
    return [
        _metric(SUMMARY_METRIC_ID, value=[summary], **common),
        _metric(BY_AGENT_METRIC_ID, value=snapshot["by_agent"], **common),
    ]
