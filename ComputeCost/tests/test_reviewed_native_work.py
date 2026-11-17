"""Real native probes and an independent verifier are consumption, never mock exclusions."""
import json
import tempfile
import unittest
from pathlib import Path
from pulse_compute_cost.activity_classification import reviewed_activity_receipts
from pulse_compute_cost.collaboration_query import CollaborationQuery, query_collaboration
from pulse_compute_cost.ledger import UsageLedger
from pulse_compute_cost.receipts import Receipt, reference

ROOT = Path(__file__).resolve().parents[1]
PROOF = ROOT / 'uat/evidence/wi300-native-work-20261004.json'
AT = '2026-10-04T15:30:00Z'


def inputs():
    proof = json.loads(PROOF.read_text())
    document = json.loads((ROOT / 'reviewed-activity.json').read_text())
    targets = {i['target_fact_ref'] for i in proof['items']}
    selected = [i for i in document['items'] if i['target_fact_ref'] in targets]
    return proof, {'schema': document['schema'], 'items': selected}


def check_exact_six_original_observations_and_distinct_verifier_principal():
    proof, doc = inputs()
    assert len(doc['items']) == len(proof['items']) == 6
    assert {i['agent'] for i in doc['items']} == {'pulse', 'outcome-verifier'}
    assert sum(i['agent'] == 'outcome-verifier' for i in doc['items']) == 2
    assert all(i['work_item_id'] == 'wi_20260827_282' and i['entrypoint'] == 'cli'
               and i['node'] == 'ownermac' for i in doc['items'])
    assert all(set(i) == {'target_fact_ref', 'work_item_id', 'agent', 'node', 'entrypoint', 'evidence_refs'}
               for i in doc['items'])
    assert all(p['basis_ref'] in i['evidence_refs'] and p['native_startup_ref'] in i['evidence_refs']
               for p in proof['items'] for i in doc['items'] if p['target_fact_ref'] == i['target_fact_ref'])
    assert reviewed_activity_receipts([], doc, AT) == []


def check_real_cost_raw_history_idempotency_and_unrelated_observations(tmp_path):
    proof, doc = inputs()
    rows = [Receipt.from_dict(p['original']) for p in proof['items']]
    unrelated = Receipt.make('usage', 'unrelated-real-native-request', rows[0].payload,
        occurred_at=rows[0].occurred_at, observed_at=AT, producer=rows[0].producer)
    history = [Receipt.from_dict(r) for r in proof['history']]
    ledger = UsageLedger(tmp_path / 'ledger'); ledger.ingest_receipts([*history, unrelated])
    old = ledger.receipt_snapshot()
    catalog = {'data': [{'id': 'openai/gpt-6-astra', 'pricing':
        {'prompt': '0.000001', 'completion': '0.000002', 'input_cache_read': '0.0000001', 'input_cache_write': '0.000001'}}]}
    def query(mark=None):
        return query_collaboration(ledger, CollaborationQuery(scope='activity', from_date='2026-09-28',
            to_date='2026-10-05', timezone='Asia/Shanghai', group_by=('wi', 'agent'), watermark=mark),
            catalog=catalog, catalog_observed_at=AT)
    before = query(old['watermark']); corrections = reviewed_activity_receipts(old['facts'], doc, AT)
    assert len(corrections) == 12 and all(r.kind == 'attribution' for r in corrections)
    assert all(r.payload.get('wi') != 'excluded' for r in corrections)
    ledger.ingest_receipts(corrections); after = query()
    assert after['summary']['usage']['event_count'] == 7
    assert after['summary']['usage']['agent_unassigned_event_count'] == 1
    assert after['summary']['usage']['wi_unassigned_event_count'] == 1
    assert after['summary']['usage']['tokens'] == before['summary']['usage']['tokens']
    assert before['summary']['cost']['partial_machine_cash_usd'] is not None
    assert after['summary']['cost']['partial_machine_cash_usd'] == before['summary']['cost']['partial_machine_cash_usd']
    assert ledger.receipt_snapshot(watermark=old['watermark'])['sha256'] == old['sha256']
    assert query(old['watermark']) == before
    assert ledger.ingest_receipts(reviewed_activity_receipts(ledger.receipt_snapshot()['facts'], doc, AT))['inserted'] == 0
    assert [r.to_dict() for r in ledger.receipt_snapshot()['facts'] if r.kind == 'usage'] == [r.to_dict() for r in old['facts']]
    contrary = Receipt.make('attribution', 'independent-contrary-task', {
        'target_ref': rows[0].entity_ref, 'wi': 'wi_20261004_999', 'executor': None,
        'responsible': None, 'delegator': None, 'state': 'assigned', 'policy': 'reviewed_activity.v1',
        'effective_at': rows[0].payload['interval_start']}, occurred_at=rows[0].payload['interval_start'],
        observed_at=AT, producer='independent.v1', evidence=(reference('proof', 'contrary'),))
    ledger.ingest_receipts([contrary])
    assert query()['summary']['usage']['wi_unassigned_event_count'] > 1


class ReviewedNativeWorkTests(unittest.TestCase):
    def test_exact_six_original_observations_and_distinct_verifier_principal(self):
        check_exact_six_original_observations_and_distinct_verifier_principal()

    def test_real_cost_raw_history_idempotency_and_unrelated_observations(self):
        with tempfile.TemporaryDirectory() as tmp:
            check_real_cost_raw_history_idempotency_and_unrelated_observations(Path(tmp))
