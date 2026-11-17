"""A withheld amount names why each excluded usage is a conflict."""
from pulse_compute_cost.collaboration_query import CollaborationQuery, query_collaboration, reconcile_usage
from pulse_compute_cost.ledger import UsageLedger
from pulse_compute_cost.receipts import Receipt
from pulse_compute_cost.stats_dashboard import _view
from test_cc_native_dedupe import CATALOG, END, execution, host_result, native


def mismatch():
    ex = execution()
    message = native("m1", "2026-09-13T01:01:00Z", 40)
    result = host_result(ex, output=10)
    result = Receipt.make("usage", None, {**result.payload, "covers": [message.entity_ref]},
                          entity_ref=result.entity_ref, occurred_at=result.occurred_at,
                          observed_at=result.observed_at, producer=result.producer)
    return ex, message, result


def test_reconciliation_records_the_reason_of_each_conflict():
    ex, message, result = mismatch()
    _, out = reconcile_usage([message, result], executions=[ex])
    assert out["conflict_reasons"] == {ref: ["summary_detail_mismatch"]
                                       for ref in sorted([message.entity_ref, result.entity_ref])}


def test_report_counts_conflict_reasons_and_keeps_the_amount_withheld(tmp_path):
    ex, message, result = mismatch()
    ledger = UsageLedger(tmp_path / "ledger")
    ledger.ingest_receipts([ex, message, result])
    report = query_collaboration(ledger, CollaborationQuery(scope="activity", from_date="2026-09-13",
        to_date="2026-09-14", timezone="UTC", group_by=("agent",)), catalog=CATALOG, catalog_observed_at=END)
    assert report["summary"]["usage"]["conflict_reasons"] == {"summary_detail_mismatch": 2}
    assert report["summary"]["cost"]["partial_machine_cash_usd"] is None
    view = _view(report, "today", "activity")
    assert view["summary"]["usage"]["conflict_reasons"] == {"summary_detail_mismatch": 2}
    assert all("conflict_reasons" in group["usage"] for group in view["groups"])
