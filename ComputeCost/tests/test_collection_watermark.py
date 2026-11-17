"""A collector must not publish derivatives of a stale ledger snapshot."""

import json
import pytest
from pulse_compute_cost.ledger import UsageLedger, LedgerError, SnapshotChanged
from pulse_compute_cost.receipts import Receipt
from pulse_compute_cost.receipt_sources import collect_registered_sources

AT = "2026-01-01T00:00:00Z"


def fact(label):
    return Receipt.make(
        "execution",
        label,
        {
            "wi": None,
            "executor": None,
            "responsible": None,
            "delegator": None,
            "runtime": "codex",
            "assignment_ref": None,
            "parent_ref": None,
            "state": "accepted",
            "expected_usage": False,
        },
        occurred_at=AT,
        observed_at=AT,
        producer="test.v1",
    )


class ConcurrentLedger(UsageLedger):
    """Another writer appends right after each of the first ``races`` reads."""
    races = 1

    def receipt_snapshot(self, **kwargs):
        snapshot = super().receipt_snapshot(**kwargs)
        if self.races:
            self.races -= 1
            UsageLedger(self.root).ingest_receipts([fact(f"other-writer-{self.races}")])
        return snapshot


def collector_registry(tmp_path):
    path = tmp_path / "source.jsonl"
    path.write_text(json.dumps(fact("collector").to_dict()) + "\n")
    return {
        "schema": "computecost_source_registry/v1",
        "sources": [{"id": "fixture", "kind": "receipts", "path": str(path)}],
    }


def test_stale_collection_appends_nothing_and_recollects_from_the_current_stream(tmp_path):
    ledger = ConcurrentLedger(tmp_path / "ledger")
    timings = {}
    result = collect_registered_sources(collector_registry(tmp_path), ledger, timings=timings)
    assert timings["attempts"] == 2
    # Batch 1 is the other writer; the stale attempt wrote nothing.
    assert result["watermark"] == 2 and result["inserted"] > 0
    batches = [UsageLedger(ledger.root).receipt_snapshot(watermark=w)["facts"] for w in (1, 2)]
    assert {r.entity_ref for r in batches[0]} == {fact("other-writer-0").entity_ref}
    assert fact("collector").entity_ref in {r.entity_ref for r in batches[1]}
    again = collect_registered_sources(collector_registry(tmp_path), ledger)
    assert again["inserted"] == 0 and again["watermark"] == 2


def test_recollection_is_bounded(tmp_path):
    from pulse_compute_cost.receipt_sources import COLLECTION_ATTEMPTS
    ledger = ConcurrentLedger(tmp_path / "ledger")
    ledger.races = COLLECTION_ATTEMPTS
    timings = {}
    with pytest.raises(SnapshotChanged, match="snapshot changed"):
        collect_registered_sources(collector_registry(tmp_path), ledger, timings=timings)
    assert timings["attempts"] == COLLECTION_ATTEMPTS
    current = UsageLedger(ledger.root).receipt_snapshot()
    assert current["watermark"] == COLLECTION_ATTEMPTS
    assert fact("collector").entity_ref not in {r.entity_ref for r in current["facts"]}


def test_expected_watermark_checked_inside_existing_transaction(tmp_path):
    ledger = UsageLedger(tmp_path / "ledger")
    other = UsageLedger(ledger.root)
    ledger.ingest_receipts([fact("first")], expected_watermark=0)
    original = ledger.receipt_snapshot()
    other.ingest_receipts([fact("second")], expected_watermark=1)
    before = other.receipt_snapshot()
    with pytest.raises(LedgerError, match="snapshot changed"):
        ledger.ingest_receipts([fact("stale")], expected_watermark=1)
    assert other.receipt_snapshot()["sha256"] == before["sha256"]
    assert ledger.receipt_snapshot(watermark=1)["sha256"] == original["sha256"]
    assert (
        ledger.ingest_receipts([fact("second")], expected_watermark=2)["inserted"] == 0
    )


@pytest.mark.parametrize("bad", [True, False, -1, 1.0, "1"])
def test_invalid_expected_watermark_never_creates_ledger(tmp_path, bad):
    ledger = UsageLedger(tmp_path / "ledger")
    with pytest.raises(LedgerError, match="watermark"):
        ledger.ingest_receipts([fact("invalid")], expected_watermark=bad)
    assert not ledger.root.exists()
