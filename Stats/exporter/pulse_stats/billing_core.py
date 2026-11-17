"""Reviewed subscription config and exact integer-cent allocation."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
import json
from pathlib import Path
from typing import Dict, Mapping, Optional, Sequence, Tuple

from .live_contract import DISPLAY_TZ


ATTRIBUTABLE = "cc_mtd_output_share"
UNATTRIBUTED = "unattributed_no_agent_source"
VALID_ATTRIBUTIONS = frozenset((ATTRIBUTABLE, UNATTRIBUTED))
COMPONENT_KEYS = frozenset((
    "component_id", "amount_cents", "currency", "effective_from",
    "effective_until", "source_ref", "attribution",
))
UNATTRIBUTED_SORT_KEY = "\uffff__unattributed__"


class BillingConfigError(ValueError):
    """The reviewed subscription configuration cannot be used."""


@dataclass(frozen=True)
class SubscriptionComponent:
    component_id: str
    amount_cents: int
    currency: str
    effective_from: str
    effective_until: Optional[str]
    source_ref: str
    attribution: str


@dataclass(frozen=True)
class AttributionEvidence:
    month: str
    days_covered: int
    days_expected: int
    coverage_complete: bool
    valid_turn_count: int
    agent_output_tokens: Tuple[Tuple[str, int], ...]
    unattributed_output_tokens: int
    outcome: str
    reason: Optional[str] = None


@dataclass(frozen=True)
class AgentAttribution:
    agent: str
    output_tokens: int
    attributed_cents: int


@dataclass(frozen=True)
class BillingResult:
    status: str
    quality: str
    period: Optional[Mapping[str, object]]
    by_agent: Optional[Tuple[AgentAttribution, ...]]
    attributed_cents: Optional[int]
    unattributed_cents: Optional[int]
    sample_count: Optional[int]
    reason: Optional[str]


def _date(value: object, field: str) -> date:
    if not isinstance(value, str):
        raise BillingConfigError(f"{field} must be an ISO date")
    try:
        parsed = date.fromisoformat(value)
    except ValueError as error:
        raise BillingConfigError(f"{field} must be an ISO date") from error
    if parsed.isoformat() != value:
        raise BillingConfigError(f"{field} must use YYYY-MM-DD")
    return parsed


def load_subscription_components(
    path: Path, now: datetime,
) -> Tuple[SubscriptionComponent, ...]:
    """Validate all rows, then return rows active on the inclusive CST date."""
    if now.tzinfo is None or now.utcoffset() is None:
        raise BillingConfigError("collection time must be timezone-aware")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise BillingConfigError(f"subscription config unreadable: {error}") from error
    if not isinstance(payload, Mapping) or set(payload) != {
        "schema_version", "components",
    }:
        raise BillingConfigError("subscription config has invalid top-level fields")
    version, rows = payload["schema_version"], payload["components"]
    if not isinstance(version, int) or isinstance(version, bool) or version != 1:
        raise BillingConfigError("subscription config schema_version must be 1")
    if not isinstance(rows, list):
        raise BillingConfigError("subscription config components must be a list")

    current_day, seen, active = now.astimezone(DISPLAY_TZ).date(), set(), []
    for index, raw in enumerate(rows):
        if not isinstance(raw, Mapping) or set(raw) != COMPONENT_KEYS:
            raise BillingConfigError(f"component {index} has invalid fields")
        component_id = raw["component_id"]
        if (
            not isinstance(component_id, str)
            or not component_id
            or component_id != component_id.strip()
        ):
            raise BillingConfigError(f"component {index} has invalid component_id")
        if component_id in seen:
            raise BillingConfigError(f"duplicate component_id: {component_id}")
        seen.add(component_id)
        cents = raw["amount_cents"]
        if not isinstance(cents, int) or isinstance(cents, bool) or cents < 0:
            raise BillingConfigError(
                f"component {component_id} amount_cents must be a nonnegative integer"
            )
        if raw["currency"] != "USD":
            raise BillingConfigError(f"component {component_id} currency must be USD")
        start = _date(raw["effective_from"], f"{component_id} effective_from")
        end_raw = raw["effective_until"]
        end = None if end_raw is None else _date(end_raw, f"{component_id} effective_until")
        if end is not None and end < start:
            raise BillingConfigError(f"component {component_id} has an inverted period")
        source = raw["source_ref"]
        if (
            not isinstance(source, str)
            or not source
            or source != source.strip()
        ):
            raise BillingConfigError(f"component {component_id} source_ref is required")
        attribution = raw["attribution"]
        if attribution not in VALID_ATTRIBUTIONS:
            raise BillingConfigError(f"component {component_id} has unsupported attribution")
        component = SubscriptionComponent(
            component_id, cents, "USD", start.isoformat(),
            end.isoformat() if end else None, source, attribution,
        )
        if start <= current_day and (end is None or current_day <= end):
            active.append(component)
    if not active:
        raise BillingConfigError("subscription config has no active components")
    return tuple(active)


def _unknown(quality: str, reason: Optional[str]) -> BillingResult:
    return BillingResult(
        "unknown", quality, None, None, None, None, None,
        reason or "MTD attribution evidence is insufficient",
    )


def allocate_subscription_costs(
    components: Sequence[SubscriptionComponent], evidence: AttributionEvidence,
) -> BillingResult:
    """Allocate attributable components by largest remainder and conserve cents."""
    if evidence.outcome != "usable":
        return _unknown(
            "unavailable" if evidence.outcome == "unavailable" else "insufficient",
            evidence.reason,
        )
    pool = sum(c.amount_cents for c in components if c.attribution == ATTRIBUTABLE)
    total_cents = sum(c.amount_cents for c in components)
    agents: Dict[str, int] = {}
    for agent, tokens in evidence.agent_output_tokens:
        if (
            not isinstance(agent, str) or not agent
            or not isinstance(tokens, int) or isinstance(tokens, bool) or tokens < 0
        ):
            return _unknown("unavailable", "Internal evidence has invalid agent tokens")
        agents[agent] = agents.get(agent, 0) + tokens
    missing = evidence.unattributed_output_tokens
    if not isinstance(missing, int) or isinstance(missing, bool) or missing < 0:
        return _unknown("unavailable", "Internal evidence has invalid unattributed tokens")
    total_tokens = sum(agents.values()) + missing

    if pool == 0:
        rows, attributed, unattributed = (), 0, total_cents
    elif total_tokens == 0:
        if not evidence.coverage_complete:
            return _unknown(
                "insufficient",
                evidence.reason or "Partial coverage cannot prove zero usage",
            )
        rows, attributed, unattributed = (), 0, total_cents
    else:
        weights = dict(agents)
        if missing:
            weights[UNATTRIBUTED_SORT_KEY] = missing
        shares = {key: pool * weight // total_tokens for key, weight in weights.items()}
        remainders = sorted(
            ((pool * weight % total_tokens, key) for key, weight in weights.items()),
            key=lambda item: (-item[0], item[1]),
        )
        for _, key in remainders[:pool - sum(shares.values())]:
            shares[key] += 1
        rows = tuple(
            AgentAttribution(agent, tokens, shares.get(agent, 0))
            for agent, tokens in sorted(agents.items(), key=lambda item: (-item[1], item[0]))
        )
        attributed = sum(row.attributed_cents for row in rows)
        unattributed = total_cents - pool + shares.get(UNATTRIBUTED_SORT_KEY, 0)
    if attributed + unattributed != total_cents:
        return _unknown("unavailable", "Integer billing allocation failed conservation")
    quality = "validated" if evidence.coverage_complete else "descriptive"
    return BillingResult(
        "available", quality,
        {
            "month": evidence.month, "timezone": "Asia/Shanghai",
            "days_covered": evidence.days_covered,
            "days_expected": evidence.days_expected,
            "coverage": "full" if evidence.coverage_complete else "partial",
        },
        rows, attributed, unattributed, evidence.valid_turn_count,
        None if evidence.coverage_complete else evidence.reason,
    )
