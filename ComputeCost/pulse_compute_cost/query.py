"""Conserving, evidence-first cost queries for weekly and ad-hoc windows."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
import hashlib
import json
from typing import Any, Callable, Iterable, Mapping

from .drivers import build_cost_driver_bridge
from .events import BEIJING, UNKNOWN, UsageEvent, utc_text
from .ledger import UsageLedger
from .openrouter import OpenRouterPriceSnapshot
from .valuation import ValuationError, estimate_reference_cost


QUERY_SCHEMA = "cost_query.v1"
WEEKLY_CAPACITY_CENTS = 5_000
MONTHLY_ACCOUNTING_CENTS = 20_000
UNATTRIBUTED = "unattributed"


class QueryError(ValueError):
    """Raised when a query would be ambiguous or non-conserving."""


def _decimal_text(value: Decimal) -> str:
    if value == 0:
        return "0"
    rendered = format(value, "f")
    return rendered.rstrip("0").rstrip(".") if "." in rendered else rendered


def _parse_date(value: str, field: str) -> date:
    if not isinstance(value, str):
        raise QueryError(f"{field} must be an ISO date")
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise QueryError(f"{field} must be an ISO date (YYYY-MM-DD)") from exc
    if parsed.isoformat() != value:
        raise QueryError(f"{field} must be a normalized ISO date")
    return parsed


@dataclass(frozen=True)
class QueryWindow:
    """A Beijing-local half-open date window [from, to)."""

    from_date: date
    to_date: date

    def __post_init__(self) -> None:
        if self.to_date <= self.from_date:
            raise QueryError("query --to must be after --from")

    @classmethod
    def from_text(cls, from_text: str, to_text: str) -> "QueryWindow":
        return cls(_parse_date(from_text, "--from"), _parse_date(to_text, "--to"))

    @property
    def start_utc(self) -> datetime:
        return datetime.combine(self.from_date, time.min, tzinfo=BEIJING).astimezone(
            timezone.utc
        )

    @property
    def end_utc(self) -> datetime:
        return datetime.combine(self.to_date, time.min, tzinfo=BEIJING).astimezone(
            timezone.utc
        )

    @property
    def duration_days(self) -> int:
        return (self.to_date - self.from_date).days

    @property
    def is_iso_week(self) -> bool:
        return self.duration_days == 7 and self.from_date.isoweekday() == 1

    def previous(self) -> "QueryWindow":
        return QueryWindow(
            self.from_date - timedelta(days=self.duration_days),
            self.from_date,
        )

    def to_dict(self) -> dict[str, Any]:
        iso = self.from_date.isocalendar()
        return {
            "from": self.from_date.isoformat(),
            "to": self.to_date.isoformat(),
            "semantics": "half_open",
            "timezone": "Asia/Shanghai",
            "duration_days": self.duration_days,
            "iso_week": f"{iso.year}-W{iso.week:02d}" if self.is_iso_week else None,
        }


def _month_keys(window: QueryWindow) -> list[str]:
    cursor = date(window.from_date.year, window.from_date.month, 1)
    last = window.to_date - timedelta(days=1)
    result: list[str] = []
    while cursor <= last:
        result.append(cursor.strftime("%Y-%m"))
        if cursor.month == 12:
            cursor = date(cursor.year + 1, 1, 1)
        else:
            cursor = date(cursor.year, cursor.month + 1, 1)
    return result


def read_ledger_window(ledger: UsageLedger, window: QueryWindow) -> list[UsageEvent]:
    """Read all product-month partitions touched by a window, including corrections."""

    rows: list[UsageEvent] = []
    for month in _month_keys(window):
        rows.extend(ledger.read_month(month))
    result = [
        event
        for event in rows
        if window.start_utc <= event.occurred_utc < window.end_utc
    ]
    ids = [event.event_id for event in result]
    if len(ids) != len(set(ids)):
        raise QueryError("duplicate event id across query window")
    return sorted(result, key=lambda event: (event.occurred_utc, event.event_id))


def _safe(value: object) -> str:
    if value is None or value == UNKNOWN or value == "":
        return UNATTRIBUTED
    return str(value)


def _root_task(event: UsageEvent) -> str:
    return _safe(event.lineage.root_run_ref)


def _cron_job(event: UsageEvent) -> str:
    return _safe(event.lineage.cron_job_ref)


def _scan(event: UsageEvent) -> str:
    return _safe(event.lineage.scan_ref)


def _root_origin(event: UsageEvent) -> str:
    if event.lineage.cron_job_ref != UNKNOWN:
        return f"cron:{event.lineage.cron_job_ref}"
    if event.lineage.scan_ref != UNKNOWN:
        return f"scan:{event.lineage.scan_ref}"
    if event.lineage.root_run_ref != UNKNOWN:
        return f"root:{event.lineage.root_run_ref}"
    return UNATTRIBUTED


def _day(event: UsageEvent) -> str:
    return event.occurred_utc.astimezone(BEIJING).date().isoformat()


def _week(event: UsageEvent) -> str:
    iso = event.occurred_utc.astimezone(BEIJING).date().isocalendar()
    return f"{iso.year}-W{iso.week:02d}"


def _month(event: UsageEvent) -> str:
    return event.occurred_utc.astimezone(BEIJING).strftime("%Y-%m")


_DIMENSIONS: dict[str, Callable[[UsageEvent], str]] = {
    "agent": lambda event: _safe(event.agent),
    "consumer": lambda event: _safe(event.consumer_path),
    "task": lambda event: _safe(event.task_ref),
    "model": lambda event: _safe(event.model),
    "effort": lambda event: _safe(event.effort),
    "retry": lambda event: _safe(event.retry_count),
    "escalation": lambda event: _safe(event.escalation_count),
    "outcome": lambda event: _safe(event.outcome_status),
    "root_agent": lambda event: _safe(event.lineage.root_agent),
    "execution_mode": lambda event: _safe(event.lineage.execution_mode),
    "session_role": lambda event: _safe(event.lineage.session_role),
    "root_task": _root_task,
    "cron_job": _cron_job,
    "scan": _scan,
    "root_origin": _root_origin,
    "day": _day,
    "week": _week,
    "month": _month,
}


def supported_dimensions() -> tuple[str, ...]:
    return tuple(_DIMENSIONS)


def _reference_price(
    prices: Iterable[OpenRouterPriceSnapshot],
) -> tuple[OpenRouterPriceSnapshot | None, list[dict[str, Any]]]:
    """Resolve the one comparison price used for every known-source event.

    Reference cost intentionally does not price each event at its actual model.
    S0/S2 define one OpenRouter comparison model so heterogeneous LLM usage is
    measured on one scale.  Zero snapshots is allowed as incomplete evidence;
    multiple snapshots are ambiguous and fail closed.
    """

    rows = list(prices)
    if any(not isinstance(price, OpenRouterPriceSnapshot) for price in rows):
        raise QueryError("prices must contain OpenRouterPriceSnapshot values")
    if len(rows) > 1:
        raise QueryError("query accepts at most one reference price snapshot")
    if not rows:
        return None, []
    price = rows[0]
    payload = price.to_dict()
    digest = hashlib.sha256(
        json.dumps(
            payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False
        ).encode("utf-8")
    ).hexdigest()
    return price, [{"snapshot_id": f"orprice1_{digest}", **payload}]


def _event_cost(
    event: UsageEvent,
    price: OpenRouterPriceSnapshot | None,
) -> Decimal | None:
    if event.source == UNKNOWN or price is None:
        return None
    try:
        return estimate_reference_cost(
            input_tokens=event.tokens.input,
            output_tokens=event.tokens.output,
            cache_read_tokens=event.tokens.cache_read,
            price=price,
        )
    except ValuationError as exc:
        raise QueryError(f"event {event.event_id} cannot be valued: {exc}") from exc


def _aggregate(
    events: Iterable[UsageEvent],
    price: OpenRouterPriceSnapshot | None,
) -> dict[str, Any]:
    event_count = 0
    priced_event_count = 0
    unpriced_event_count = 0
    partial_event_count = 0
    input_tokens = output_tokens = cache_read_tokens = cache_write_tokens = 0
    reference_cost = Decimal("0")
    outcome_linked_event_count = 0
    for event in events:
        event_count += 1
        input_tokens += event.tokens.input
        output_tokens += event.tokens.output
        cache_read_tokens += event.tokens.cache_read
        cache_write_tokens += event.tokens.cache_write
        if event.has_missing_fields or event.quality_flags:
            partial_event_count += 1
        if event.outcome_status != UNKNOWN or event.outcome_ref != UNKNOWN:
            outcome_linked_event_count += 1
        cost = _event_cost(event, price)
        if cost is None:
            unpriced_event_count += 1
        else:
            priced_event_count += 1
            reference_cost += cost
    return {
        "event_count": event_count,
        "priced_event_count": priced_event_count,
        "unpriced_event_count": unpriced_event_count,
        "partial_event_count": partial_event_count,
        "outcome_linked_event_count": outcome_linked_event_count,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cache_read_tokens": cache_read_tokens,
        "cache_write_tokens": cache_write_tokens,
        "total_tokens": input_tokens + output_tokens,
        "reference_cost_usd": _decimal_text(reference_cost),
        "reference_cost_complete": unpriced_event_count == 0,
    }


def _share(numerator: int, denominator: int) -> str:
    if denominator == 0:
        return "0"
    return _decimal_text(Decimal(numerator) / Decimal(denominator))


def _delta(current: Mapping[str, Any], baseline: Mapping[str, Any]) -> dict[str, Any]:
    current_reference = Decimal(str(current["reference_cost_usd"]))
    baseline_reference = Decimal(str(baseline["reference_cost_usd"]))
    cost_complete = bool(current["reference_cost_complete"]) and bool(
        baseline["reference_cost_complete"]
    )
    return {
        "event_count": int(current["event_count"]) - int(baseline["event_count"]),
        "total_tokens": int(current["total_tokens"]) - int(baseline["total_tokens"]),
        "reference_cost_usd": (
            _decimal_text(current_reference - baseline_reference)
            if cost_complete
            else None
        ),
        "reference_cost_relative": (
            _decimal_text((current_reference - baseline_reference) / baseline_reference)
            if cost_complete and baseline_reference != 0
            else None
        ),
    }


def _group_rows(
    current_events: list[UsageEvent],
    baseline_events: list[UsageEvent],
    price: OpenRouterPriceSnapshot | None,
    dimension: str,
    current_totals: Mapping[str, Any],
) -> list[dict[str, Any]]:
    key_fn = _DIMENSIONS[dimension]
    current_buckets: dict[str, list[UsageEvent]] = {}
    baseline_buckets: dict[str, list[UsageEvent]] = {}
    for event in current_events:
        current_buckets.setdefault(key_fn(event), []).append(event)
    for event in baseline_events:
        baseline_buckets.setdefault(key_fn(event), []).append(event)

    rows: list[dict[str, Any]] = []
    total_reference = Decimal(str(current_totals["reference_cost_usd"]))
    current_complete = bool(current_totals["reference_cost_complete"])
    for key in sorted(set(current_buckets) | set(baseline_buckets)):
        current = _aggregate(current_buckets.get(key, ()), price)
        baseline = _aggregate(baseline_buckets.get(key, ()), price)
        row_reference = Decimal(str(current["reference_cost_usd"]))
        rows.append(
            {
                "key": key,
                **current,
                "event_share": _share(
                    int(current["event_count"]), int(current_totals["event_count"])
                ),
                "token_share": _share(
                    int(current["total_tokens"]), int(current_totals["total_tokens"])
                ),
                "reference_cost_share": (
                    _decimal_text(row_reference / total_reference)
                    if current_complete and total_reference != 0
                    else ("0" if current_complete else None)
                ),
                "baseline": baseline,
                "delta": _delta(current, baseline),
            }
        )
    return rows


def _conservation(
    groups: Mapping[str, list[Mapping[str, Any]]],
    current_totals: Mapping[str, Any],
    baseline_totals: Mapping[str, Any],
) -> dict[str, Any]:
    fields = (
        "event_count",
        "priced_event_count",
        "unpriced_event_count",
        "input_tokens",
        "output_tokens",
        "cache_read_tokens",
        "cache_write_tokens",
        "total_tokens",
    )
    current_dimensions: dict[str, bool] = {}
    baseline_dimensions: dict[str, bool] = {}
    current_reference = Decimal(str(current_totals["reference_cost_usd"]))
    baseline_reference = Decimal(str(baseline_totals["reference_cost_usd"]))
    for dimension, rows in groups.items():
        current_counts_ok = all(
            sum(int(row[field]) for row in rows) == int(current_totals[field])
            for field in fields
        )
        current_reference_ok = (
            sum(Decimal(str(row["reference_cost_usd"])) for row in rows)
            == current_reference
        )
        baseline_counts_ok = all(
            sum(int(row["baseline"][field]) for row in rows)
            == int(baseline_totals[field])
            for field in fields
        )
        baseline_reference_ok = (
            sum(
                Decimal(str(row["baseline"]["reference_cost_usd"]))
                for row in rows
            )
            == baseline_reference
        )
        current_dimensions[dimension] = current_counts_ok and current_reference_ok
        baseline_dimensions[dimension] = baseline_counts_ok and baseline_reference_ok
    passed = all(current_dimensions.values()) and all(baseline_dimensions.values())
    return {
        "status": "pass" if passed else "failed",
        "dimensions": current_dimensions,
        "baseline_dimensions": baseline_dimensions,
    }


def query_events(
    *,
    events: Iterable[UsageEvent],
    window: QueryWindow,
    prices: Iterable[OpenRouterPriceSnapshot] = (),
    bridge_baseline_prices: Iterable[OpenRouterPriceSnapshot] = (),
    bridge_current_prices: Iterable[OpenRouterPriceSnapshot] = (),
    baseline_events: Iterable[UsageEvent] = (),
    baseline_window: QueryWindow | None = None,
    generated_at: datetime | None = None,
) -> dict[str, Any]:
    """Aggregate one evidence window across every required partition."""

    rows = list(events)
    baseline_rows = list(baseline_events)
    if any(not isinstance(event, UsageEvent) for event in rows + baseline_rows):
        raise QueryError("events must contain UsageEvent values")
    outside = [
        event.event_id
        for event in rows
        if not (window.start_utc <= event.occurred_utc < window.end_utc)
    ]
    if outside:
        raise QueryError(f"events outside query window: {sorted(outside)}")
    if baseline_window is None:
        baseline_window = window.previous()
    baseline_outside = [
        event.event_id
        for event in baseline_rows
        if not (
            baseline_window.start_utc <= event.occurred_utc < baseline_window.end_utc
        )
    ]
    if baseline_outside:
        raise QueryError(f"events outside baseline window: {sorted(baseline_outside)}")

    reference_price, price_evidence = _reference_price(prices)
    totals = _aggregate(rows, reference_price)
    baseline_totals = _aggregate(baseline_rows, reference_price)
    groups = {
        dimension: _group_rows(
            rows,
            baseline_rows,
            reference_price,
            dimension,
            totals,
        )
        for dimension in _DIMENSIONS
    }
    conservation = _conservation(groups, totals, baseline_totals)
    if conservation["status"] != "pass":
        raise QueryError("partition conservation failed")

    flags: list[str] = []
    if totals["unpriced_event_count"]:
        flags.append("unpriced_events")
    if totals["partial_event_count"]:
        flags.append("event_evidence_partial")
    if totals["outcome_linked_event_count"] != totals["event_count"]:
        flags.append("outcome_coverage_incomplete")
    if baseline_totals["unpriced_event_count"]:
        flags.append("baseline_unpriced_events")
    if baseline_totals["partial_event_count"]:
        flags.append("baseline_event_evidence_partial")
    if (
        baseline_totals["outcome_linked_event_count"]
        != baseline_totals["event_count"]
    ):
        flags.append("baseline_outcome_coverage_incomplete")
    if not price_evidence:
        flags.append("price_evidence_unavailable")

    produced_at = generated_at or datetime.now(timezone.utc)
    if produced_at.tzinfo is None or produced_at.utcoffset() is None:
        raise QueryError("generated_at must be timezone-aware")
    weekly_capacity = None
    if window.is_iso_week:
        weekly_capacity = {
            "chatgpt_pro_capacity_cents": WEEKLY_CAPACITY_CENTS,
            "quota_pressure": UNKNOWN,
            "quota_reset": UNKNOWN,
            "quota_slope": UNKNOWN,
            "note": "capacity_only_not_monthly_cost_allocation",
        }

    document: dict[str, Any] = {
        "schema": QUERY_SCHEMA,
        "schema_version": 1,
        "mode": "shadow",
        "generated_at": utc_text(produced_at),
        "window": window.to_dict(),
        "price_evidence": price_evidence,
        "totals": totals,
        "groups": groups,
        "by_agent": groups["agent"],
        "by_consumer": groups["consumer"],
        "by_outcome": groups["outcome"],
        "baseline": {
            "window": baseline_window.to_dict(),
            "totals": baseline_totals,
            "delta": _delta(totals, baseline_totals),
        },
        "driver_delta": build_cost_driver_bridge(
            baseline_events=baseline_rows,
            current_events=rows,
            baseline_prices=bridge_baseline_prices,
            current_prices=bridge_current_prices,
        ),
        "managed_cost": {
            "status": "not_settled_in_query_window",
            "monthly_accounting_cents": MONTHLY_ACCOUNTING_CENTS,
            "reason": "managed_cost_cash_settlement_is_monthly_only",
        },
        "weekly_capacity": weekly_capacity,
        "conservation": conservation,
        "quality": {
            "status": "complete" if not flags else "partial",
            "flags": sorted(flags),
        },
    }
    return document


def query_ledger(
    *,
    ledger: UsageLedger,
    window: QueryWindow,
    prices: Iterable[OpenRouterPriceSnapshot] = (),
    bridge_baseline_prices: Iterable[OpenRouterPriceSnapshot] = (),
    bridge_current_prices: Iterable[OpenRouterPriceSnapshot] = (),
    generated_at: datetime | None = None,
) -> dict[str, Any]:
    """Run the same query engine directly over ledger evidence."""

    baseline_window = window.previous()
    return query_events(
        events=read_ledger_window(ledger, window),
        window=window,
        prices=prices,
        bridge_baseline_prices=bridge_baseline_prices,
        bridge_current_prices=bridge_current_prices,
        baseline_events=read_ledger_window(ledger, baseline_window),
        baseline_window=baseline_window,
        generated_at=generated_at,
    )
