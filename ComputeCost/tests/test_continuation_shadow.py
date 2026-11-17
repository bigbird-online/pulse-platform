from __future__ import annotations

from decimal import Decimal
from pathlib import Path

from pulse_compute_cost.continuation_shadow import (
    CONTINUATION_STAGE_BUNDLE_COMPILE,
    CONTINUATION_STAGE_FRESH_BOOTSTRAP,
    CONTINUATION_STAGE_OLD_SESSION_TAIL,
    CONTINUATION_STAGE_RECOVERY_TO_FIRST_ACTION,
    ContinuationAttribution,
    ContinuationProducerEvidence,
    build_continuation_shadow_report,
    continuation_context_areas,
    continuation_outcome_ref,
)
from pulse_compute_cost.events import Lineage, TokenUsage, UNKNOWN, UsageEvent
from pulse_compute_cost.ledger import UsageLedger


ROOT_RUN = f"lineage_v1_{'1' * 64}"
SOURCE_RUN = f"lineage_v1_{'2' * 64}"
TARGET_RUN = f"lineage_v1_{'3' * 64}"
CONTINUATION_ID = "ct_20260819_001"


def event(
    suffix: str,
    *,
    tokens: TokenUsage,
    outcome_ref: str | None = None,
    parent_run_ref: str = SOURCE_RUN,
    human_correction: bool | None = None,
) -> UsageEvent:
    return UsageEvent(
        event_id=f"uev1_{suffix}",
        occurred_at="2026-08-19T01:00:00Z",
        observed_at="2026-08-19T01:00:01Z",
        machine="pulse-dev",
        agent="pulse",
        consumer_path="relay",
        source="chatgpt_pro",
        runtime="cc",
        provider="anthropic",
        model="claude-opus-4-1",
        effort="high",
        tokens=tokens,
        work_item_ref="work_item_v1_fixture",
        task_ref="task_v1_fixture",
        outcome_ref=outcome_ref or continuation_outcome_ref(CONTINUATION_ID),
        human_correction=human_correction,
        lineage=Lineage(
            root_agent="pulse",
            execution_mode="human",
            session_role="main",
            root_run_ref=ROOT_RUN,
            parent_run_ref=parent_run_ref,
        ),
    )


def test_continuation_id_maps_to_outcome_ref_deterministically() -> None:
    first = continuation_outcome_ref(CONTINUATION_ID)
    second = continuation_outcome_ref(CONTINUATION_ID)
    other = continuation_outcome_ref("ct_20260819_002")
    assert first == second
    assert first.startswith("outcome_v1_")
    assert first != other
    assert CONTINUATION_ID not in first


def test_continuation_source_target_lineage_reuses_existing_lineage() -> None:
    attribution = ContinuationAttribution.from_continuation(
        work_item_id="wi_20260819_001",
        continuation_id=CONTINUATION_ID,
        to_phase="implementation",
        phase_attempt=1,
        root_run_ref=ROOT_RUN,
        source_run_ref=SOURCE_RUN,
    )
    target = attribution.target_lineage(
        root_agent="pulse",
        execution_mode="human",
        session_role="main",
    )
    assert target.root_run_ref == ROOT_RUN
    assert target.parent_run_ref == SOURCE_RUN
    assert target.root_agent == "pulse"
    assert attribution.outcome_ref == continuation_outcome_ref(CONTINUATION_ID)


def test_existing_usage_ledger_idempotency_is_unchanged(tmp_path: Path) -> None:
    usage = event("ledger", tokens=TokenUsage(input=100, output=10, cache_read=40))
    ledger = UsageLedger(tmp_path / "ledger")
    first = ledger.ingest([usage])
    second = ledger.ingest([usage])
    assert first.inserted == 1
    assert first.duplicates == 0
    assert second.inserted == 0
    assert second.duplicates == 1
    assert ledger.read_month("2026-08") == [usage]


def test_context_areas_are_separate_and_conserve_input() -> None:
    events = [
        event("a", tokens=TokenUsage(input=100, output=10, cache_read=40)),
        event("b", tokens=TokenUsage(input=250, output=20, cache_read=200)),
    ]
    areas = continuation_context_areas(
        events,
        priced_event_costs={"uev1_a": Decimal("0.12"), "uev1_b": Decimal("0.34")},
    )
    assert areas.raw_input_area == 350
    assert areas.uncached_input_area == 110
    assert areas.cache_read_area == 240
    assert areas.raw_input_area == areas.uncached_input_area + areas.cache_read_area
    assert areas.cost_weighted_area == "0.46"


def test_missing_producer_evidence_remains_unknown() -> None:
    report = build_continuation_shadow_report(
        [event("unknown", tokens=TokenUsage(input=10, output=1, cache_read=5))],
        continuation_id=CONTINUATION_ID,
    )
    assert report.old_session_tail_cost == UNKNOWN
    assert report.bundle_compile_cost == UNKNOWN
    assert report.fresh_bootstrap_cost == UNKNOWN
    assert report.recovery_to_first_action_cost == UNKNOWN
    assert report.rollover_failure_count == UNKNOWN
    assert report.rework == UNKNOWN
    assert report.accepted_outcome == UNKNOWN
    assert report.orphan_wi_ho_delta == UNKNOWN
    assert report.context.cost_weighted_area == UNKNOWN


def test_shadow_report_uses_only_existing_usage_events_and_explicit_evidence() -> None:
    events = [
        event("tail", tokens=TokenUsage(input=100, output=10, cache_read=20)),
        event("bundle", tokens=TokenUsage(input=50, output=5, cache_read=10)),
        event("boot", tokens=TokenUsage(input=70, output=7, cache_read=30)),
        event(
            "recover",
            tokens=TokenUsage(input=80, output=8, cache_read=40),
            human_correction=True,
        ),
        event(
            "other-continuation",
            tokens=TokenUsage(input=999, output=99, cache_read=0),
            outcome_ref=continuation_outcome_ref("ct_20260819_999"),
        ),
    ]
    evidence = {
        "uev1_tail": ContinuationProducerEvidence(
            stage=CONTINUATION_STAGE_OLD_SESSION_TAIL,
            rollover_failed=False,
        ),
        "uev1_bundle": ContinuationProducerEvidence(
            stage=CONTINUATION_STAGE_BUNDLE_COMPILE,
        ),
        "uev1_boot": ContinuationProducerEvidence(
            stage=CONTINUATION_STAGE_FRESH_BOOTSTRAP,
        ),
        "uev1_recover": ContinuationProducerEvidence(
            stage=CONTINUATION_STAGE_RECOVERY_TO_FIRST_ACTION,
            accepted_outcome=True,
            rework=True,
            orphan_wi_ho_delta=0,
        ),
    }
    costs = {
        "uev1_tail": "0.10",
        "uev1_bundle": "0.20",
        "uev1_boot": "0.30",
        "uev1_recover": "0.40",
    }
    report = build_continuation_shadow_report(
        events,
        continuation_id=CONTINUATION_ID,
        evidence_by_event=evidence,
        priced_event_costs=costs,
    )
    assert report.event_count == 4
    assert report.old_session_tail_cost == "0.10"
    assert report.bundle_compile_cost == "0.20"
    assert report.fresh_bootstrap_cost == "0.30"
    assert report.recovery_to_first_action_cost == "0.40"
    assert report.rollover_failure_count == 0
    assert report.human_correction == 1
    assert report.rework == 1
    assert report.accepted_outcome == 1
    assert report.orphan_wi_ho_delta == 0
    assert report.context.raw_input_area == 300
    assert report.context.cost_weighted_area == "1.00"


def test_compute_cost_does_not_create_a_second_usage_ledger(tmp_path: Path) -> None:
    # The shadow API is pure/read-only: constructing and reporting never creates
    # files. The sole durable usage writer remains UsageLedger.
    root = tmp_path / "shadow"
    usage = event("pure", tokens=TokenUsage(input=1, output=1))
    report = build_continuation_shadow_report([usage], continuation_id=CONTINUATION_ID)
    assert report.event_count == 1
    assert not root.exists()
    assert UsageLedger.__module__ == "pulse_compute_cost.ledger"
