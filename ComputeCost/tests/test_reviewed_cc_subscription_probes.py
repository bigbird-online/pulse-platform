"""Subscription probes are real WI430 costs, not unassigned chat or discarded mocks."""
import json
from pathlib import Path
from pulse_compute_cost.activity_classification import reviewed_activity_receipts
from pulse_compute_cost.collaboration_query import CollaborationQuery, query_collaboration
from pulse_compute_cost.ledger import UsageLedger
from test_cc_native_dedupe import native, END
from test_collaboration_receipts import CATALOG


def test_only_three_original_probe_facts_are_reviewed():
    root = Path(__file__).resolve().parents[1]
    proof = json.loads((root / 'uat/evidence/wi300-cc-subscription-probes-20261003.json').read_text())
    document = json.loads((root / 'reviewed-activity.json').read_text())
    targets = {p['target_fact_ref'] for p in proof['items']}
    items = [p for p in document['items'] if p['target_fact_ref'] in targets]
    assert len(items) == len(targets) == 3
    assert all(p['work_item_id'] == 'wi_20260930_430' and p['entrypoint'] == 'cli' for p in items)
    assert sum(sum(p['tokens'].values()) for p in proof['items']) == 62068
    assert all(p['native_input_ref'] in i['evidence_refs'] and p['probe_ref'] in i['evidence_refs']
               for p in proof['items'] for i in items if p['target_fact_ref'] == i['target_fact_ref'])
    assert reviewed_activity_receipts([], {'schema': document['schema'], 'items': items}, END) == []


def test_wi_probe_review_preserves_actual_cost_and_does_not_tag_its_sibling(tmp_path):
    measured = native('probe', '2026-09-13T01:01:00Z', 40)
    sibling = native('not-reviewed', '2026-09-13T01:02:00Z', 50)
    facts = [measured, sibling]
    ledger = UsageLedger(tmp_path / 'ledger')
    ledger.ingest_receipts(facts)
    old = ledger.receipt_snapshot()
    def report(mark=None):
        return query_collaboration(ledger, CollaborationQuery(scope='activity',
            from_date='2026-09-13', to_date='2026-09-14', timezone='UTC',
            group_by=('wi',), watermark=mark), catalog=CATALOG, catalog_observed_at=END)
    before = report()
    item = {'target_fact_ref': measured.fact_ref, 'work_item_id': 'wi_20260930_430',
            'agent': 'pulse', 'node': 'ownermac', 'entrypoint': 'cli', 'evidence_refs': [measured.fact_ref]}
    rows = reviewed_activity_receipts(facts, {'schema': 'computecost_reviewed_activity/v1', 'items': [item]}, END)
    ledger.ingest_receipts(rows)
    after = report()
    assert after['summary']['usage']['event_count'] == 2
    assert after['summary']['usage']['wi_unassigned_event_count'] == 1
    assert after['summary']['usage']['known_total_tokens'] == before['summary']['usage']['known_total_tokens']
    assert after['summary']['cost']['partial_machine_cash_usd'] == before['summary']['cost']['partial_machine_cash_usd']
    assert report(old['watermark'])['summary'] == before['summary']
    assert ledger.receipt_snapshot(watermark=old['watermark'])['sha256'] == old['sha256']
    assert ledger.ingest_receipts(rows)['inserted'] == 0
