"""Finite reviewed originals: no session default, new usage, or price change."""

import json
from pathlib import Path
from collections import Counter
from datetime import datetime, timezone

from pulse_compute_cost.activity_classification import reviewed_activity_receipts
from pulse_compute_cost.collaboration_query import _prepare_collaboration
from pulse_compute_cost.ledger import UsageLedger
from pulse_compute_cost.receipts import Receipt, reference

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = Path(__file__).with_name("fixtures") / "reviewed-history-20261001.json"
AT = "2026-10-01T03:00:00Z"


def inputs():
    fixture = json.loads(FIXTURE.read_text())
    facts = [Receipt.from_dict(row) for row in fixture["facts"]]
    document = json.loads((ROOT / "reviewed-activity.json").read_text())
    targets = {r["fact_ref"] for r in fixture["expected"]}
    items = [r for r in document["items"] if r["target_fact_ref"] in targets]
    assert len(items) == len(targets) == 29
    return fixture, facts, {"schema": document["schema"], "items": items}


def test_original_cohort_is_settled_without_changing_usage_or_actor():
    fixture, facts, document = inputs()
    claims = reviewed_activity_receipts(facts, document, AT)
    before = _prepare_collaboration({"facts": facts})
    after = _prepare_collaboration({"facts": facts + claims})
    old = {r.entity_ref: r for r in before[10]}
    new = {r.entity_ref: r for r in after[10]}
    assert set(old) == set(new)
    assert not after[4]
    assert all(r.kind == "attribution" for r in claims)
    for expected in fixture["expected"]:
        key = expected["entity_ref"]
        assert old[key].payload["wi"] is None and key not in before[6]
        assert new[key].fact_ref == old[key].fact_ref
        assert new[key].payload["tokens"] == old[key].payload["tokens"]
        assert new[key].payload["executor"] == expected["agent"]
        assert after[12][key] == before[12][key]
        purpose = expected["purpose"]
        assert (
            after[6].get(key) if purpose == "none" else new[key].payload["wi"]
        ) == purpose
    assert Counter(r["purpose"] for r in fixture["expected"]) == {
        "none": 24,
        "wi_20260825_300": 1,
        "wi_20260928_219": 4,
    }


def test_replay_preserves_original_facts_and_old_watermark(tmp_path):
    _, facts, document = inputs()
    ledger = UsageLedger(tmp_path / "ledger", clock=lambda: datetime.now(timezone.utc))
    ledger.ingest_receipts(facts)
    original = ledger.receipt_snapshot()
    claims = reviewed_activity_receipts(original["facts"], document, AT)
    ledger.ingest_receipts(claims)
    current = ledger.receipt_snapshot()
    repeated = reviewed_activity_receipts(current["facts"], document, AT)
    assert ledger.ingest_receipts(repeated)["inserted"] == 0
    assert (
        ledger.receipt_snapshot(watermark=original["watermark"])["sha256"]
        == original["sha256"]
    )
    assert [r.to_dict() for r in current["facts"] if r.kind != "attribution"] == [
        r.to_dict() for r in original["facts"] if r.kind != "attribution"
    ]


def test_review_does_not_classify_new_observation_in_same_session():
    _, facts, document = inputs()
    prior = next(r for r in facts if r.kind == "usage")
    new = Receipt.make(
        "usage",
        ["future-observation", prior.entity_ref],
        prior.payload,
        occurred_at=prior.occurred_at,
        observed_at=AT,
        producer=prior.producer,
    )
    claims = reviewed_activity_receipts(facts + [new], document, AT)
    assert all(r.payload["target_ref"] != new.entity_ref for r in claims)
    assert reviewed_activity_receipts([new], document, AT) == []


def test_independent_contrary_wi_remains_a_conflict():
    fixture, facts, document = inputs()
    target = next(r for r in fixture["expected"] if r["purpose"].startswith("wi_"))
    old = next(r for r in facts if r.fact_ref == target["fact_ref"])
    contrary = Receipt.make(
        "attribution",
        ["independent-claim", old.entity_ref],
        {
            "target_ref": old.entity_ref,
            "wi": "wi_20260101_999",
            "executor": None,
            "responsible": None,
            "delegator": None,
            "state": "assigned",
            "policy": "reviewed_activity.v1",
            "effective_at": old.payload["interval_start"],
        },
        occurred_at=old.payload["interval_start"],
        observed_at=AT,
        producer="reviewed_activity.v1",
        evidence=(reference("independent-evidence", "fixture"),),
    )
    claims = reviewed_activity_receipts(facts + [contrary], document, AT)
    assert (
        old.entity_ref
        in _prepare_collaboration({"facts": facts + [contrary] + claims})[4]
    )
