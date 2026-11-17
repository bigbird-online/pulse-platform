"""Read-only continuation attribution and shadow reporting for ComputeCost.

This module deliberately consumes the existing ``UsageEvent`` / ``TokenUsage`` /
``Lineage`` facts.  It does not write usage facts, own rollover policy, or infer
producer stages from timing/model heuristics.  Missing producer evidence remains
``unknown``.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
import hashlib
from typing import Mapping, Sequence

from .events import Lineage, TokenUsage, UNKNOWN, UsageEvent, UsageEventError


CONTINUATION_STAGE_OLD_SESSION_TAIL = "old_session_tail"
CONTINUATION_STAGE_BUNDLE_COMPILE = "bundle_compile"
CONTINUATION_STAGE_FRESH_BOOTSTRAP = "fresh_bootstrap"
CONTINUATION_STAGE_RECOVERY_TO_FIRST_ACTION = "recovery_to_first_action"
CONTINUATION_STAGES = frozenset(
    {
        CONTINUATION_STAGE_OLD_SESSION_TAIL,
        CONTINUATION_STAGE_BUNDLE_COMPILE,
        CONTINUATION_STAGE_FRESH_BOOTSTRAP,
        CONTINUATION_STAGE_RECOVERY_TO_FIRST_ACTION,
    }
)


def _domain_ref(prefix: str, domain: str, *parts: str) -> str:
    if any(not isinstance(part, str) or not part.strip() for part in parts):
        raise UsageEventError("continuation reference inputs must be non-empty strings")
    digest = hashlib.sha256()
    digest.update(domain.encode("utf-8"))
    for part in parts:
        digest.update(b"\0")
        digest.update(part.strip().encode("utf-8"))
    return f"{prefix}{digest.hexdigest()}"


def continuation_work_item_ref(work_item_id: str) -> str:
    return _domain_ref("work_item_v1_", "pulse-work-item-v1", work_item_id)


def continuation_task_ref(work_item_id: str, to_phase: str, phase_attempt: int) -> str:
    if isinstance(phase_attempt, bool) or not isinstance(phase_attempt, int) or phase_attempt < 1:
        raise UsageEventError("phase_attempt must be a positive integer")
    return _domain_ref(
        "task_v1_",
        "pulse-phase-v1",
        work_item_id,
        to_phase,
        str(phase_attempt),
    )


def continuation_outcome_ref(continuation_id: str) -> str:
    return _domain_ref(
        "outcome_v1_",
        "pulse-continuation-v1",
        continuation_id,
    )


@dataclass(frozen=True)
class ContinuationAttribution:
    """Privacy-safe producer mapping onto the existing UsageEvent fields."""

    work_item_ref: str
    task_ref: str
    outcome_ref: str
    root_run_ref: str
    parent_run_ref: str

    @classmethod
    def from_continuation(
        cls,
        *,
        work_item_id: str,
        continuation_id: str,
        to_phase: str,
        phase_attempt: int,
        root_run_ref: str,
        source_run_ref: str,
    ) -> "ContinuationAttribution":
        # Let Lineage perform the canonical privacy-safe ref validation instead
        # of growing a second ref validator here.
        validated = Lineage(
            root_run_ref=root_run_ref,
            parent_run_ref=source_run_ref,
        )
        return cls(
            work_item_ref=continuation_work_item_ref(work_item_id),
            task_ref=continuation_task_ref(work_item_id, to_phase, phase_attempt),
            outcome_ref=continuation_outcome_ref(continuation_id),
            root_run_ref=validated.root_run_ref,
            parent_run_ref=validated.parent_run_ref,
        )

    def target_lineage(
        self,
        *,
        root_agent: str,
        execution_mode: str,
        session_role: str,
    ) -> Lineage:
        return Lineage(
            root_agent=root_agent,
            execution_mode=execution_mode,
            session_role=session_role,
            root_run_ref=self.root_run_ref,
            parent_run_ref=self.parent_run_ref,
        )


@dataclass(frozen=True)
class ContinuationProducerEvidence:
    """Optional producer evidence for one existing UsageEvent.

    ``stage`` is never inferred by ComputeCost.  If the producer did not emit it,
    the event remains unattributed to a rollover stage in the shadow report.
    """

    stage: str = UNKNOWN
    rollover_failed: bool | None = None
    accepted_outcome: bool | None = None
    rework: bool | None = None
    orphan_wi_ho_delta: int | None = None

    def __post_init__(self) -> None:
        if self.stage != UNKNOWN and self.stage not in CONTINUATION_STAGES:
            raise UsageEventError(f"unsupported continuation producer stage: {self.stage}")
        for name in ("rollover_failed", "accepted_outcome", "rework"):
            value = getattr(self, name)
            if value is not None and not isinstance(value, bool):
                raise UsageEventError(f"{name} must be boolean or null")
        if self.orphan_wi_ho_delta is not None:
            if isinstance(self.orphan_wi_ho_delta, bool) or not isinstance(
                self.orphan_wi_ho_delta, int
            ):
                raise UsageEventError("orphan_wi_ho_delta must be an integer or null")


@dataclass(frozen=True)
class ContinuationContextAreas:
    raw_input_area: int
    uncached_input_area: int
    cache_read_area: int
    cost_weighted_area: str

    def __post_init__(self) -> None:
        if self.raw_input_area != self.uncached_input_area + self.cache_read_area:
            raise UsageEventError("context areas do not conserve total input")


@dataclass(frozen=True)
class ContinuationShadowReport:
    continuation_outcome_ref: str
    event_count: int
    old_session_tail_cost: str
    bundle_compile_cost: str
    fresh_bootstrap_cost: str
    recovery_to_first_action_cost: str
    rollover_failure_count: int | str
    human_correction: int
    rework: int | str
    accepted_outcome: int | str
    orphan_wi_ho_delta: int | str
    context: ContinuationContextAreas


def continuation_context_areas(
    events: Sequence[UsageEvent],
    *,
    priced_event_costs: Mapping[str, Decimal | str] | None = None,
) -> ContinuationContextAreas:
    raw_input = sum(event.tokens.input for event in events)
    cache_read = sum(event.tokens.cache_read for event in events)
    uncached_input = sum(event.tokens.input - event.tokens.cache_read for event in events)

    cost_area = UNKNOWN
    if priced_event_costs is not None and all(
        event.event_id in priced_event_costs for event in events
    ):
        total = Decimal("0")
        try:
            for event in events:
                total += Decimal(str(priced_event_costs[event.event_id]))
        except (InvalidOperation, ValueError) as exc:
            raise UsageEventError("priced_event_costs contains an invalid decimal") from exc
        cost_area = format(total, "f")

    return ContinuationContextAreas(
        raw_input_area=raw_input,
        uncached_input_area=uncached_input,
        cache_read_area=cache_read,
        cost_weighted_area=cost_area,
    )


def _sum_stage_cost(
    events: Sequence[UsageEvent],
    *,
    evidence_by_event: Mapping[str, ContinuationProducerEvidence],
    priced_event_costs: Mapping[str, Decimal | str] | None,
    stage: str,
) -> str:
    stage_events = [
        event
        for event in events
        if evidence_by_event.get(event.event_id, ContinuationProducerEvidence()).stage
        == stage
    ]
    if not stage_events:
        return UNKNOWN
    if priced_event_costs is None or any(
        event.event_id not in priced_event_costs for event in stage_events
    ):
        return UNKNOWN
    total = Decimal("0")
    try:
        for event in stage_events:
            total += Decimal(str(priced_event_costs[event.event_id]))
    except (InvalidOperation, ValueError) as exc:
        raise UsageEventError("priced_event_costs contains an invalid decimal") from exc
    return format(total, "f")


def _producer_count(
    evidence: Sequence[ContinuationProducerEvidence],
    field_name: str,
) -> int | str:
    values = [getattr(item, field_name) for item in evidence]
    observed = [value for value in values if value is not None]
    if not observed:
        return UNKNOWN
    return sum(1 for value in observed if value is True)


def _producer_delta(evidence: Sequence[ContinuationProducerEvidence]) -> int | str:
    values = [
        item.orphan_wi_ho_delta
        for item in evidence
        if item.orphan_wi_ho_delta is not None
    ]
    if not values:
        return UNKNOWN
    return sum(values)


def build_continuation_shadow_report(
    events: Sequence[UsageEvent],
    *,
    continuation_id: str,
    evidence_by_event: Mapping[str, ContinuationProducerEvidence] | None = None,
    priced_event_costs: Mapping[str, Decimal | str] | None = None,
) -> ContinuationShadowReport:
    """Build a read-only report from already-ingested usage events.

    Events are selected by the privacy-safe ``outcome_ref``.  Producer-only
    measures remain ``unknown`` unless explicit evidence is supplied; this
    function never invents ``AvoidedMain`` or infers phase boundaries.
    """

    outcome_ref = continuation_outcome_ref(continuation_id)
    selected = [event for event in events if event.outcome_ref == outcome_ref]
    evidence_by_event = evidence_by_event or {}
    selected_evidence = [
        evidence_by_event[event.event_id]
        for event in selected
        if event.event_id in evidence_by_event
    ]

    return ContinuationShadowReport(
        continuation_outcome_ref=outcome_ref,
        event_count=len(selected),
        old_session_tail_cost=_sum_stage_cost(
            selected,
            evidence_by_event=evidence_by_event,
            priced_event_costs=priced_event_costs,
            stage=CONTINUATION_STAGE_OLD_SESSION_TAIL,
        ),
        bundle_compile_cost=_sum_stage_cost(
            selected,
            evidence_by_event=evidence_by_event,
            priced_event_costs=priced_event_costs,
            stage=CONTINUATION_STAGE_BUNDLE_COMPILE,
        ),
        fresh_bootstrap_cost=_sum_stage_cost(
            selected,
            evidence_by_event=evidence_by_event,
            priced_event_costs=priced_event_costs,
            stage=CONTINUATION_STAGE_FRESH_BOOTSTRAP,
        ),
        recovery_to_first_action_cost=_sum_stage_cost(
            selected,
            evidence_by_event=evidence_by_event,
            priced_event_costs=priced_event_costs,
            stage=CONTINUATION_STAGE_RECOVERY_TO_FIRST_ACTION,
        ),
        rollover_failure_count=_producer_count(selected_evidence, "rollover_failed"),
        human_correction=sum(1 for event in selected if event.human_correction is True),
        rework=_producer_count(selected_evidence, "rework"),
        accepted_outcome=_producer_count(selected_evidence, "accepted_outcome"),
        orphan_wi_ho_delta=_producer_delta(selected_evidence),
        context=continuation_context_areas(
            selected,
            priced_event_costs=priced_event_costs,
        ),
    )


__all__ = [
    "CONTINUATION_STAGE_BUNDLE_COMPILE",
    "CONTINUATION_STAGE_FRESH_BOOTSTRAP",
    "CONTINUATION_STAGE_OLD_SESSION_TAIL",
    "CONTINUATION_STAGE_RECOVERY_TO_FIRST_ACTION",
    "ContinuationAttribution",
    "ContinuationContextAreas",
    "ContinuationProducerEvidence",
    "ContinuationShadowReport",
    "build_continuation_shadow_report",
    "continuation_context_areas",
    "continuation_outcome_ref",
    "continuation_task_ref",
    "continuation_work_item_ref",
]
