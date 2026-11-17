"""Finite reviewed CC ordinary work; absent declarations are never defaults."""
import json
from pathlib import Path

from pulse_compute_cost.activity_classification import reviewed_activity_receipts
from pulse_compute_cost.collaboration_query import CollaborationQuery, query_collaboration
from pulse_compute_cost.ledger import UsageLedger
from test_cc_native_dedupe import execution, host_message, native, END
from test_collaboration_receipts import CATALOG


def test_finite_cc_input_review_matches_only_reviewed_facts():
    root = Path(__file__).resolve().parents[1]
    proof = json.loads((root / 'uat/evidence/wi300-cc-ordinary-inputs-20261003.json').read_text())
    targets = {item['target_fact_ref'] for item in proof['items']}
    document = json.loads((root / 'reviewed-activity.json').read_text())
    items = [item for item in document['items'] if item['target_fact_ref'] in targets]
    assert len(items) == len(targets) == 5
    assert all(item['work_kind'] == 'none' and item['node'] == 'ownermac' for item in items)
    assert sorted(item['entrypoint'] for item in items) == ['feishu'] * 4 + ['telegram']
    for item in proof['items']:
        review = next(r for r in items if r['target_fact_ref'] == item['target_fact_ref'])
        assert item['input_ref'] in review['evidence_refs']
    assert reviewed_activity_receipts([], {'schema': document['schema'], 'items': items}, END) == []


def test_cc_review_keeps_usage_history_and_unreviewed_session_unknown(tmp_path):
    ex = execution()
    host = host_message('ordinary', ex, '2026-09-13T01:01:01Z', 8)
    measured = native('ordinary', '2026-09-13T01:01:00Z', 40)
    other = native('unreviewed', '2026-09-13T01:02:00Z', 50)
    facts = [ex, host, measured, other]
    ledger = UsageLedger(tmp_path / 'ledger')
    ledger.ingest_receipts(facts)
    old = ledger.receipt_snapshot()
    def report(watermark=None):
        return query_collaboration(ledger, CollaborationQuery(scope='activity',
            from_date='2026-09-13', to_date='2026-09-14', timezone='UTC',
            group_by=('agent', 'wi', 'entrypoint'), watermark=watermark),
            catalog=CATALOG, catalog_observed_at=END)
    before = report()
    item = {'target_fact_ref': host.fact_ref, 'work_kind': 'none', 'agent': 'pulse',
            'node': 'ownermac', 'entrypoint': 'telegram', 'evidence_refs': [measured.fact_ref]}
    rows = reviewed_activity_receipts(facts,
        {'schema': 'computecost_reviewed_activity/v1', 'items': [item]}, END)
    ledger.ingest_receipts(rows)
    after = report()
    assert after['summary']['usage']['no_wi_event_count'] == 1
    assert after['summary']['usage']['wi_unassigned_event_count'] == 1
    assert after['summary']['usage']['known_total_tokens'] == before['summary']['usage']['known_total_tokens']
    assert after['summary']['cost']['partial_machine_cash_usd'] == before['summary']['cost']['partial_machine_cash_usd']
    assert ledger.receipt_snapshot(watermark=old['watermark'])['sha256'] == old['sha256']
    assert report(old['watermark'])['summary'] == before['summary']
    assert ledger.ingest_receipts(rows)['inserted'] == 0
