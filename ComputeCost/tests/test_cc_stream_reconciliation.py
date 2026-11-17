"""Final CC attempt counters and partial streamed messages remain one bill."""

import pytest
from pulse_compute_cost.receipts import Receipt, reference
from pulse_compute_cost.receipt_sources import usage_receipt
from pulse_compute_cost.collaboration_query import reconcile_usage

START = "2026-01-01T01:00:00Z"
END = "2026-01-01T01:00:30Z"
MODEL = "anthropic/test"


def execution(key, start=START):
    return Receipt.make(
        "execution",
        key,
        {
            "wi": None,
            "executor": "pulse",
            "responsible": None,
            "delegator": None,
            "runtime": "cc",
            "assignment_ref": None,
            "parent_ref": None,
            "state": "completed",
            "expected_usage": True,
        },
        occurred_at=start,
        observed_at=END,
        producer="relay_host.v1",
        evidence=(reference("host", key),),
    )


def usage(
    key,
    ex,
    *,
    output=3,
    final=False,
    aggregate=False,
    covers=(),
    model=MODEL,
    start=START,
    end=END,
):
    return usage_receipt(
        "cc",
        key,
        {
            "input_tokens": 2,
            "output_tokens": output,
            "cache_read_input_tokens": 80,
            "cache_creation_input_tokens": 10,
        },
        at=end,
        observed=end,
        model=model,
        execution_ref=ex.entity_ref,
        coverage_ref=reference("coverage", [ex.entity_ref, model]),
        common={
            "wi": None,
            "executor": "pulse",
            "responsible": None,
            "delegator": None,
        },
        granularity="turn_aggregate" if aggregate else "request",
        mapping="cc_sdk_result.v1" if aggregate else "cc_message.v1",
        covers=list(covers),
        final=final,
        interval=(start, end),
        native_scope_ref=reference("native_session", ["cc", "native-test-session"]),
    )


def changed(row, **payload):
    return Receipt.make(
        row.kind,
        None,
        {**row.payload, **payload},
        entity_ref=row.entity_ref,
        occurred_at=row.occurred_at,
        observed_at=row.observed_at,
        producer=row.producer,
        evidence=row.evidence,
    )


def pair():
    ex = execution("attempt")
    detail = usage("message", ex)
    total = usage(
        "result", ex, output=219, final=True, aggregate=True, covers=[detail.entity_ref]
    )
    return ex, detail, total


def test_final_cc_result_replaces_covered_partial_stream_not_conflict():
    ex, detail, total = pair()
    before = [r.to_dict() for r in (detail, total)]
    rows, result = reconcile_usage([detail, total], executions=[ex])
    assert [r.entity_ref for r in rows] == [total.entity_ref]
    assert result["conflicting_refs"] == []
    assert detail.entity_ref in result["suppressed_aggregate_refs"]
    assert [r.to_dict() for r in (detail, total)] == before


def test_same_session_separate_host_attempt_does_not_quarantine_later_result():
    ex, detail, total = pair()
    earlier = execution("earlier", "2026-01-01T00:00:00Z")
    failed = usage(
        "empty-error-result",
        earlier,
        output=0,
        final=True,
        aggregate=True,
        model=None,
        start="2026-01-01T00:00:00Z",
        end="2026-01-01T00:01:00Z",
    )
    failed = changed(failed, mapping="cc_result.v1")
    failed = Receipt.make(
        failed.kind,
        None,
        failed.payload,
        entity_ref=failed.entity_ref,
        occurred_at=failed.occurred_at,
        observed_at=failed.observed_at,
        producer="cc_result.v1",
        evidence=failed.evidence,
    )
    rows, result = reconcile_usage([failed, detail, total], executions=[earlier, ex])
    assert {r.entity_ref for r in rows} == {failed.entity_ref, total.entity_ref}
    assert result["conflicting_refs"] == []


@pytest.mark.parametrize(
    "mutation",
    [
        "final_detail",
        "decreasing",
        "missing_quantity",
        "other_execution",
        "other_coverage",
        "other_session",
        "missing_cover",
        "other_wi",
        "not_final",
        "missing_execution_proof",
    ],
)
def test_unproven_or_contradictory_stream_remains_conflict(mutation):
    ex, detail, total = pair()
    executions = [ex]
    if mutation == "final_detail":
        detail = changed(detail, final=True)
    elif mutation == "decreasing":
        total = changed(total, tokens={**total.payload["tokens"], "output": 2})
    elif mutation == "missing_quantity":
        detail = changed(detail, tokens={**detail.payload["tokens"], "output": None})
    elif mutation == "other_execution":
        detail = changed(detail, execution_ref=reference("execution", "other"))
    elif mutation == "other_coverage":
        detail = changed(detail, coverage_ref=reference("coverage", "other"))
    elif mutation == "other_session":
        detail = changed(detail, native_scope_ref=reference("session", "other"))
    elif mutation == "missing_cover":
        total = changed(
            total, covers=[detail.entity_ref, reference("usage", "missing")]
        )
    elif mutation == "other_wi":
        detail = changed(detail, wi="wi_20260101_001")
        total = changed(total, wi="wi_20260101_002")
    elif mutation == "not_final":
        total = changed(total, final=False)
    elif mutation == "missing_execution_proof":
        executions = []
    _, result = reconcile_usage([detail, total], executions=executions)
    assert total.entity_ref in result["conflicting_refs"]


def test_multiple_final_results_remain_a_conflict():
    ex, detail, total = pair()
    second = usage(
        "other-final",
        ex,
        output=220,
        final=True,
        aggregate=True,
        covers=[detail.entity_ref],
    )
    _, result = reconcile_usage([detail, total, second], executions=[ex])
    assert {total.entity_ref, second.entity_ref} <= set(result["conflicting_refs"])


def test_exact_complete_detail_still_wins_without_mutating_receipts():
    ex, detail, total = pair()
    detail = changed(detail, final=True, tokens=total.payload["tokens"])
    rows, result = reconcile_usage([detail, total], executions=[ex])
    assert [r.entity_ref for r in rows] == [detail.entity_ref]
    assert result["conflicting_refs"] == []


def test_final_summary_keeps_the_original_ledger_and_old_watermark(tmp_path):
    from pulse_compute_cost.ledger import UsageLedger
    from pulse_compute_cost.collaboration_query import (
        CollaborationQuery,
        query_collaboration,
    )

    ex, detail, total = pair()
    ledger = UsageLedger(tmp_path / "ledger")
    ledger.ingest_receipts([ex, detail])
    old = ledger.receipt_snapshot()
    ledger.ingest_receipts([total])
    before = ledger.receipt_snapshot()
    result = query_collaboration(ledger, CollaborationQuery(scope="activity", from_date="2026-01-01", to_date="2026-01-02", timezone="UTC"))
    assert result["summary"]["usage"]["event_count"] == 1
    assert result["summary"]["usage"]["known_total_tokens"] == 311
    assert result["summary"]["usage"]["conflicted_event_count"] == 0
    assert ledger.receipt_snapshot()["sha256"] == before["sha256"]
    assert (
        ledger.receipt_snapshot(watermark=old["watermark"])["sha256"] == old["sha256"]
    )
    assert ledger.ingest_receipts([total])["inserted"] == 0
