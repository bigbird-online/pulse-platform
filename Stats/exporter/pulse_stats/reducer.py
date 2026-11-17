"""Canonical incident projection with no I/O or production dependencies."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import Iterable, Literal, Optional

Severity = Literal["info", "warning", "critical"]
EventKind = Literal["breach", "recovery", "event_only"]

_SEVERITY_RANK: dict[str, int] = {"info": 0, "warning": 1, "critical": 2}

# --- Alert tiers ---
# The three buckets an alert can land in. They live here, the one module with
# no internal imports, so both the collector (which assigns them) and the
# producer (which renders them) can reach them without an import cycle. The
# label whitelists that decide *which* tier applies are policy and live in
# live_contract.py.
TIER_ACT = "act"
TIER_WATCH = "watch"
TIER_DONE = "done"
TIERS = (TIER_ACT, TIER_WATCH, TIER_DONE)
Tier = Literal["act", "watch", "done"]


def incident_key(capability_id: str, condition_key: str, normalized_scope_key: str) -> str:
    allowed = set("abcdefghijklmnopqrstuvwxyz0123456789._-")
    if not normalized_scope_key or len(normalized_scope_key) > 128:
        raise ValueError("normalized_scope_key must contain 1-128 safe characters")
    if normalized_scope_key[0] not in set("abcdefghijklmnopqrstuvwxyz0123456789"):
        raise ValueError("normalized_scope_key must start with a lowercase letter or digit")
    if any(character not in allowed for character in normalized_scope_key):
        raise ValueError("normalized_scope_key must be pre-normalized or caller-hashed")
    return f"{capability_id}:{condition_key}:{normalized_scope_key}"


@dataclass(frozen=True)
class IncidentEvent:
    event_id: str
    kind: EventKind
    capability_id: str
    condition_key: str
    scope: str
    normalized_scope_key: str
    severity: Severity
    title: str
    occurred_at: datetime
    reason: str
    impact: str = "unknown"
    suggested_action: str = "Inspect source evidence"
    # Defaults to the quietest tier: an event that never had one assigned must
    # not be able to reach the owner by omission.
    tier: Tier = TIER_WATCH
    work_item_id: Optional[str] = None

    @property
    def key(self) -> str:
        return incident_key(self.capability_id, self.condition_key, self.normalized_scope_key)


@dataclass(frozen=True)
class AlertProjection:
    event_id: str
    projection_status: Literal["event_only"]
    severity: Severity
    capability_id: str
    title: str
    occurred_at: datetime
    reason: str
    tier: Tier = TIER_WATCH
    work_item_id: Optional[str] = None


@dataclass(frozen=True)
class IncidentProjection:
    incident_key: str
    episode_id: str
    state: Literal["open", "resolved"]
    severity: Severity
    capability_id: str
    scope: str
    title: str
    what_happened: str
    impact: str
    suggested_action: str
    opened_at: datetime
    updated_at: datetime
    resolved_at: Optional[datetime]
    occurrence_count: int
    reopen_count: int
    evidence: tuple[str, ...]
    recovery_evidence: tuple[str, ...]
    seen_event_ids: frozenset[str] = field(repr=False)
    last_breach_at: datetime = field(repr=False)

    def as_snapshot_dict(self) -> dict[str, object]:
        return {
            "incident_key": self.incident_key,
            "episode_id": self.episode_id,
            "state": self.state,
            "severity": self.severity,
            "capability_id": self.capability_id,
            "scope": self.scope,
            "title": self.title,
            "what_happened": self.what_happened,
            "impact": self.impact,
            "suggested_action": self.suggested_action,
            "opened_at": self.opened_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
            "resolved_at": self.resolved_at.isoformat() if self.resolved_at else None,
            "occurrence_count": self.occurrence_count,
            "reopen_count": self.reopen_count,
            "evidence": list(self.evidence),
            "recovery_evidence": list(self.recovery_evidence),
        }


def _new_projection(event: IncidentEvent) -> IncidentProjection:
    return IncidentProjection(
        incident_key=event.key,
        episode_id=f"{event.key}#episode-1",
        state="open",
        severity=event.severity,
        capability_id=event.capability_id,
        scope=event.scope,
        title=event.title,
        what_happened=event.reason,
        impact=event.impact,
        suggested_action=event.suggested_action,
        opened_at=event.occurred_at,
        updated_at=event.occurred_at,
        resolved_at=None,
        occurrence_count=1,
        reopen_count=0,
        evidence=(f"event:{event.event_id}",),
        recovery_evidence=(),
        seen_event_ids=frozenset((event.event_id,)),
        last_breach_at=event.occurred_at,
    )


def _apply_breach(current: Optional[IncidentProjection], event: IncidentEvent) -> IncidentProjection:
    if current is None:
        return _new_projection(event)
    if event.event_id in current.seen_event_ids:
        return current

    seen = current.seen_event_ids | {event.event_id}
    if current.state == "resolved" and current.resolved_at and event.occurred_at > current.resolved_at:
        next_reopen_count = current.reopen_count + 1
        return replace(
            current,
            episode_id=f"{current.incident_key}#episode-{next_reopen_count + 1}",
            state="open",
            severity=event.severity,
            title=event.title,
            what_happened=event.reason,
            impact=event.impact,
            suggested_action=event.suggested_action,
            opened_at=event.occurred_at,
            updated_at=event.occurred_at,
            resolved_at=None,
            occurrence_count=1,
            reopen_count=next_reopen_count,
            evidence=(f"event:{event.event_id}",),
            recovery_evidence=(),
            seen_event_ids=seen,
            last_breach_at=event.occurred_at,
        )

    severity = current.severity
    if _SEVERITY_RANK[event.severity] > _SEVERITY_RANK[severity]:
        severity = event.severity
    is_newer = event.occurred_at > current.updated_at
    return replace(
        current,
        severity=severity,
        title=event.title if is_newer else current.title,
        what_happened=event.reason if is_newer else current.what_happened,
        impact=event.impact if is_newer else current.impact,
        suggested_action=event.suggested_action if is_newer else current.suggested_action,
        updated_at=max(current.updated_at, event.occurred_at),
        occurrence_count=current.occurrence_count + 1,
        evidence=current.evidence + (f"event:{event.event_id}",),
        seen_event_ids=seen,
        last_breach_at=max(current.last_breach_at, event.occurred_at),
    )


def _apply_recovery(current: Optional[IncidentProjection], event: IncidentEvent) -> Optional[IncidentProjection]:
    if current is None or event.event_id in current.seen_event_ids:
        return current
    seen = current.seen_event_ids | {event.event_id}
    recovery_evidence = current.recovery_evidence + (f"event:{event.event_id}",)
    if current.state == "open" and event.occurred_at > current.last_breach_at:
        return replace(
            current,
            state="resolved",
            updated_at=max(current.updated_at, event.occurred_at),
            resolved_at=event.occurred_at,
            recovery_evidence=recovery_evidence,
            seen_event_ids=seen,
        )
    return replace(current, recovery_evidence=recovery_evidence, seen_event_ids=seen)


def reduce_incident_events(
    events: Iterable[IncidentEvent],
    existing: Iterable[IncidentProjection] = (),
) -> tuple[list[IncidentProjection], list[AlertProjection]]:
    """Project events deterministically; event-only records never become incidents."""

    incidents = {item.incident_key: item for item in existing}
    alerts: list[AlertProjection] = []
    seen_alert_ids: set[str] = set()
    ordered = sorted(events, key=lambda event: (event.occurred_at, event.event_id))
    for event in ordered:
        if event.kind == "event_only":
            if event.event_id in seen_alert_ids:
                continue
            seen_alert_ids.add(event.event_id)
            alerts.append(
                AlertProjection(
                    event_id=event.event_id,
                    projection_status="event_only",
                    severity=event.severity,
                    capability_id=event.capability_id,
                    title=event.title,
                    occurred_at=event.occurred_at,
                    reason=event.reason,
                    tier=event.tier,
                    work_item_id=event.work_item_id,
                )
            )
            continue
        current = incidents.get(event.key)
        projected = _apply_breach(current, event) if event.kind == "breach" else _apply_recovery(current, event)
        if projected is not None:
            incidents[event.key] = projected
    return sorted(incidents.values(), key=lambda item: item.incident_key), alerts
