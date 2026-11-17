"""A native partial message must not quarantine settled sibling messages."""

import tempfile
from pathlib import Path

import pytest

from pulse_compute_cost.collaboration_query import (
    CollaborationQuery,
    query_collaboration,
)
from pulse_compute_cost.ledger import UsageLedger
from pulse_compute_cost.receipts import Receipt, reference
from test_cc_native_dedupe import (
    CATALOG,
    END,
    execution,
    host_message,
    host_result,
    native,
    total,
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


def cohort():
    ex = execution()
    settled = native("settled", "2026-09-13T01:01:00Z", 40)
    partial = native("partial", "2026-09-13T01:02:00Z", 8, final=False)
    aggregate = changed(
        host_result(ex), covers=[settled.entity_ref, partial.entity_ref]
    )
    return ex, settled, partial, aggregate


def read(rows):
    with tempfile.TemporaryDirectory() as tmp:
        ledger = UsageLedger(Path(tmp) / "ledger")
        ledger.ingest_receipts(rows)
        before = ledger.receipt_snapshot()
        result = query_collaboration(
            ledger,
            CollaborationQuery(
                scope="activity",
                from_date="2026-09-13",
                to_date="2026-09-14",
                timezone="UTC",
                group_by=("wi",),
                watermark=before["watermark"],
            ),
            catalog=CATALOG,
            catalog_observed_at=END,
        )
        assert ledger.receipt_snapshot()["sha256"] == before["sha256"]
        assert (
            ledger.receipt_snapshot(watermark=before["watermark"])["sha256"]
            == before["sha256"]
        )
        return result


def test_unfinished_native_sibling_is_provisional_detail_of_its_result():
    ex, settled, partial, aggregate = cohort()
    out = read([ex, settled, partial, aggregate])
    assert out["summary"]["usage"]["event_count"] == 2
    assert out["summary"]["usage"]["known_total_tokens"] == total(40) + total(8)
    assert out["reconciliation"]["conflicting_refs"] == []
    assert aggregate.entity_ref in out["reconciliation"]["suppressed_aggregate_refs"]
    assert out["summary"]["usage"]["conflicted_event_count"] == 0
    assert "provisional_usage" in out["summary"]["cost"]["reasons"]
    assert "join_conflict" not in out["summary"]["cost"]["reasons"]


def test_native_proof_survives_host_message_projection():
    ex, settled, partial, aggregate = cohort()
    host = host_message("settled", ex, "2026-09-13T01:01:01Z", 8)
    streaming = host_message("partial", ex, "2026-09-13T01:02:01Z", 8)
    out = read([ex, host, streaming, settled, partial, aggregate])
    assert out["summary"]["usage"]["event_count"] == 2
    assert out["summary"]["usage"]["known_total_tokens"] == total(40) + total(8)
    assert out["reconciliation"]["conflicting_refs"] == []


def test_coverless_result_over_native_detail_is_not_added():
    ex, settled, partial, aggregate = cohort()
    aggregate = changed(aggregate, covers=[])
    out = read([ex, settled, partial, aggregate])
    assert out["summary"]["usage"]["event_count"] == 2
    assert out["reconciliation"]["conflicting_refs"] == []


@pytest.mark.parametrize(
    "case",
    [
        "missing",
        "host_only",
        "scope",
        "model",
        "quantity",
        "wi",
        "unknown",
        "no_scope",
        "runtime",
    ],
)
def test_incomplete_or_contradictory_proof_keeps_component_unresolved(case):
    ex, settled, partial, aggregate = cohort()
    if case == "missing":
        aggregate = changed(
            aggregate,
            covers=[
                settled.entity_ref,
                partial.entity_ref,
                reference("usage", "missing"),
            ],
        )
    elif case == "host_only":
        partial = host_message("partial", ex, "2026-09-13T01:02:00Z", 8)
    elif case == "scope":
        partial = changed(
            partial, native_scope_ref=reference("native_session", "other")
        )
    elif case == "model":
        partial = changed(partial, model="other/model")
    elif case == "quantity":
        aggregate = changed(
            aggregate, tokens={**aggregate.payload["tokens"], "output": 1}
        )
    elif case == "wi":
        settled = changed(settled, wi="wi_20260825_300")
        partial = changed(partial, wi="wi_20261001_297")
    elif case == "no_scope":
        aggregate = changed(aggregate, native_scope_ref=None)
    elif case == "runtime":
        partial = changed(partial, runtime="pi")
    elif case == "unknown":
        partial = changed(partial, tokens={**partial.payload["tokens"], "output": None})
    out = read([ex, settled, partial, aggregate])
    assert aggregate.entity_ref in out["reconciliation"]["conflicting_refs"]
    assert settled.entity_ref in out["reconciliation"]["conflicting_refs"]
    assert out["summary"]["usage"]["event_count"] == 0
