"""A recorded human waiver is review evidence, never independent acceptance."""
from contextlib import redirect_stdout
from copy import deepcopy
import io
import json
from pathlib import Path
import tempfile
import unittest

from pulse_compute_cost.cli import main
from pulse_compute_cost.delivery_value import DeliveryValueError, validate_harness_review_snapshot
from pulse_compute_cost.receipt_sources import import_harness_bundle, collect_registered_sources, usage_receipt
from pulse_compute_cost.receipts import Receipt, current_receipts, reference
from pulse_compute_cost.ledger import UsageLedger
from test_delivery_value import review_snapshot, WI

AT = '2026-08-25T04:00:00Z'


def waived_review(status='blocked'):
    value = review_snapshot()
    value['schema_version'] = 'work_item_review/v2'
    value['outcome_waiver'] = {
        'human_confirmed': True,
        'expected_candidate_fingerprint': value['candidate_fingerprint'],
        'decision_source_ref': '.harness/evidence/waiver.md',
        'decision_source_sha256': 'a' * 64,
        'reason': 'Use the direct human acceptance for this candidate.',
        'accepted_risks': ['No new independent runtime inspection.'],
        'frozen_claim': 'The exact submitted artifact is accepted by the human.',
        'frozen_producer': 'pulse', 'frozen_change_type': 'mixed',
        'frozen_acceptance_fingerprint': value['acceptance_fingerprint'],
        'artifact_refs': ['.harness/evidence/result.md'],
        'submission_events_sha256': 'b' * 64,
        'outcome_status': status,
        'outcome_receipt_sha256': None if status == 'missing' else 'c' * 64,
        'outcome_evidence_sha256': {} if status == 'missing' else {'.harness/evidence/old-outcome.md': 'c' * 64},
    }
    return value


class ReviewV2Tests(unittest.TestCase):
    def test_v1_wire_and_digest_stay_unchanged(self):
        value = validate_harness_review_snapshot(review_snapshot(), expected_work_item_id=WI)
        self.assertNotIn('outcome_waiver', value)
        self.assertEqual(value['snapshot_sha256'], 'c43bf3c1ce0f74e5aaec9f83d8fd9215488df4c94998bfb1c70691a63c8e21cc')

    def test_all_original_outcome_states_are_preserved_not_promoted(self):
        for status in ('missing', 'blocked', 'partial', 'rejected', 'accepted'):
            with self.subTest(status=status):
                value = waived_review(status); original = deepcopy(value)
                out = validate_harness_review_snapshot(value, expected_work_item_id=WI)
                self.assertEqual(out['outcome_waiver'], value['outcome_waiver'])
                self.assertEqual(out['schema_version'], 'work_item_review/v2')
                rows = import_harness_bundle({'schema':'computecost_harness_import/v1', 'work_item_id':WI, 'review':value}, AT)
                self.assertEqual([r.kind for r in rows], ['review'])
                self.assertEqual(rows[0].payload['decision'], 'approved')
                self.assertEqual(rows[0].payload['verification'], 'human_waiver')
                self.assertTrue(rows[0].evidence)
                self.assertEqual(value, original)

    def test_malformed_or_unbound_waiver_is_not_silently_dropped(self):
        mutations = [
            lambda r:r.update(schema_version='work_item_review/v3'),
            lambda r:r.update(schema_version='work_item_review/v1'),
            lambda r:r.pop('outcome_waiver'),
            lambda r:r.update(decision='changes_requested'),
            lambda r:r['outcome_waiver'].update(human_confirmed=False),
            lambda r:r['outcome_waiver'].update(human_confirmed=1),
            lambda r:r['outcome_waiver'].update(expected_candidate_fingerprint='sha256:'+'0'*64),
            lambda r:r['outcome_waiver'].update(frozen_acceptance_fingerprint='sha256:'+'0'*64),
            lambda r:r['outcome_waiver'].update(reason='  '),
            lambda r:r['outcome_waiver'].update(accepted_risks=[]),
            lambda r:r['outcome_waiver'].update(accepted_risks=['x'*2001]),
            lambda r:r['outcome_waiver'].update(decision_source_ref='../outside'),
            lambda r:r['outcome_waiver'].update(decision_source_ref='~/outside'),
            lambda r:r['outcome_waiver'].update(decision_source_ref='C:/outside'),
            lambda r:r['outcome_waiver'].update(submission_events_sha256='broken'),
            lambda r:r['outcome_waiver'].update(outcome_status='missing'),
            lambda r:r['outcome_waiver'].update(outcome_receipt_sha256=None),
            lambda r:r['outcome_waiver'].update(outcome_evidence_sha256={}),
            lambda r:r['outcome_waiver'].update(outcome_evidence_sha256={'/outside':'c'*64}),
            lambda r:r['outcome_waiver'].update(extra=True),
        ]
        for index, mutate in enumerate(mutations):
            with self.subTest(index=index):
                value = waived_review(); mutate(value)
                with self.assertRaises(DeliveryValueError):
                    validate_harness_review_snapshot(value, expected_work_item_id=WI)

    def test_waiver_provenance_is_in_the_snapshot_digest(self):
        old = waived_review()
        new = deepcopy(old); new['outcome_waiver']['reason'] = 'A different explicit human reason.'
        self.assertNotEqual(validate_harness_review_snapshot(old,expected_work_item_id=WI)['snapshot_sha256'],
                            validate_harness_review_snapshot(new,expected_work_item_id=WI)['snapshot_sha256'])

    def test_normal_source_collection_preserves_usage_and_canonical_outcome(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); source = root/'harness'; (source/'reviews').mkdir(parents=True)
            value = waived_review(); raw = json.dumps(value)
            path = source/'reviews'/f'{WI}.json'; path.write_text(raw)
            ledger = UsageLedger(root/'ledger')
            accepted = Receipt.make('delivery', WI, {'wi':WI, 'responsible':'pulse',
                'candidate_ref':reference('candidate','original'), 'artifact_ref':reference('artifact','original'),
                'acceptance_ref':reference('acceptance','original'), 'status':'accepted', 'accepted_at':AT},
                occurred_at=AT, observed_at=AT, producer='canonical_outcome.v1')
            usage = usage_receipt('cc','real',{'input_tokens':10,'output_tokens':2,'cache_read_input_tokens':0,'cache_creation_input_tokens':0},at=AT,observed=AT,model='test',final=True)
            ledger.ingest_receipts([accepted,usage]); before = ledger.receipt_snapshot()
            registry = {'schema':'computecost_source_registry/v1', 'sources':[{'id':'local','kind':'harness_root','path':str(source)}]}
            result = collect_registered_sources(registry, ledger)
            self.assertEqual(result['sources'][0]['source_detail']['files_invalid'], 0)
            self.assertFalse(result['sources'][0]['source_detail']['errors'])
            active, conflicts = current_receipts(ledger.receipt_snapshot()['facts'])
            self.assertFalse(conflicts)
            self.assertEqual([r.to_dict() for r in active if r.kind=='delivery'], [accepted.to_dict()])
            self.assertEqual([r.to_dict() for r in active if r.kind=='usage'], [usage.to_dict()])
            self.assertEqual(ledger.receipt_snapshot(watermark=before['watermark'])['sha256'], before['sha256'])
            self.assertEqual(collect_registered_sources(registry,ledger)['inserted'], 0)
            self.assertEqual(path.read_text(), raw)
            bad = waived_review(); bad['outcome_waiver']['human_confirmed'] = False
            path.write_text(json.dumps(bad))
            self.assertEqual(collect_registered_sources(registry,ledger)['sources'][0]['source_detail']['files_invalid'],1)

    def test_cli_accepts_direct_and_wrapped_review_and_exposes_waiver_basis(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); ledger = root/'usage.jsonl'; ledger.write_text('')
            value = waived_review(); path = root/'review.json'
            for document in (value, {'ok':True,'error':None,'review_state':'approved','review':value}):
                path.write_text(json.dumps(document)); output = io.StringIO()
                with redirect_stdout(output):
                    code = main([WI,'--app-server-ledger',str(ledger),'--review-snapshot',str(path),'--no-auto-discover'])
                self.assertEqual(code,0)
                artifacts = json.loads(output.getvalue())['outcome']['artifacts']
                self.assertEqual(artifacts['outcome_decision'], 'human_waiver')
                self.assertEqual(artifacts['captured_outcome_status'], 'blocked')

if __name__ == '__main__':
    unittest.main()
