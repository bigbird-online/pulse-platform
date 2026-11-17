"""Whole-graph replay must retain and retire the actual weak predecessor."""
import unittest
from pulse_compute_cost.receipts import Receipt, reference
from pulse_compute_cost.activity_classification import reviewed_activity_receipts
import test_non_wi_activity as fixtures

WI='wi_20260922_001'
class CompletePurposeGraphTests(unittest.TestCase):
    setUp=fixtures.NonWiActivityTests.setUp
    query=fixtures.NonWiActivityTests.query
    def prepare(self):
        original=self.row
        self.row=Receipt.make('usage',None,{**original.payload,'interval_start':fixtures.AT,
            'interval_end':fixtures.AT},entity_ref=original.entity_ref,occurred_at=fixtures.AT,
            observed_at=fixtures.AT,producer=original.producer)
        self.weak=Receipt.make('attribution',['scl_host_session_binding.v1',self.row.entity_ref],{
            'target_ref':self.row.entity_ref,'wi':None,'executor':None,'responsible':None,'delegator':None,
            'state':'ambiguous','policy':'scl_host_session_binding.v1','effective_at':fixtures.AT},
            occurred_at=fixtures.AT,observed_at=fixtures.AT,producer='scl_host_session_binding.v1',evidence=(self.proof,))
        self.item={'target_fact_ref':self.row.fact_ref,'work_item_id':WI,'agent':None,'node':None,'entrypoint':None,
            'usage_interval':[fixtures.AT,fixtures.AT],'review_basis':'native_user_request','evidence_refs':[self.proof]}
        self.document={'schema':'computecost_reviewed_activity/v1','items':[self.item]}
    def correct(self):
        return reviewed_activity_receipts(self.ledger.receipt_snapshot()['facts'],self.document,fixtures.AT)
    def test_complete_graph_discovers_exact_weak_predecessor(self):
        self.prepare();self.ledger.ingest_receipts([self.row,self.weak]);mark=self.ledger.receipt_snapshot()['watermark'];before=self.query(mark)
        corrections=self.correct();self.assertEqual(corrections[0].supersedes,self.weak.fact_ref)
        self.ledger.ingest_receipts(corrections);after=self.query()
        self.assertEqual(after['groups'][0]['key']['wi'],WI)
        self.assertEqual(after['summary']['usage']['tokens'],before['summary']['usage']['tokens'])
        self.assertEqual(after['summary']['cost']['partial_machine_cash_usd'],before['summary']['cost']['partial_machine_cash_usd'])
        self.assertEqual(self.query(mark),before)
        self.assertEqual(self.ledger.ingest_receipts(self.correct())['inserted'],0)
        self.ledger.ingest_receipts([self.weak]);self.assertEqual(self.query(),after)
    def test_late_weak_inference_does_not_undo_reviewed_purpose(self):
        self.prepare();self.ledger.ingest_receipts([self.row]);self.ledger.ingest_receipts(self.correct())
        self.ledger.ingest_receipts([self.weak]);mark=self.ledger.receipt_snapshot()['watermark'];before=self.query(mark)
        self.ledger.ingest_receipts(self.correct());self.assertEqual(self.query()['groups'][0]['key']['wi'],WI)
        self.assertEqual(self.query(mark),before)
    def test_an_unrelated_inference_is_never_retired(self):
        self.prepare();unrelated=Receipt.make('attribution','other-inference',self.weak.payload,
            occurred_at=fixtures.AT,observed_at=fixtures.AT,producer=self.weak.producer,evidence=(self.proof,))
        self.ledger.ingest_receipts([self.row,unrelated]);corrections=self.correct()
        self.assertTrue(all(r.supersedes is None for r in corrections))
        self.ledger.ingest_receipts(corrections);self.assertNotEqual(self.query()['groups'][0]['key']['wi'],WI)
    def test_a_strong_or_competing_assignment_is_not_rewritten(self):
        self.prepare();strong=Receipt.make('attribution',None,{**self.weak.payload,'wi':'wi_20260922_002','state':'assigned'},
            entity_ref=self.weak.entity_ref,occurred_at=fixtures.AT,observed_at=fixtures.AT,producer=self.weak.producer,evidence=(self.proof,))
        self.ledger.ingest_receipts([self.row,strong]);corrections=self.correct()
        self.assertTrue(all(r.supersedes is None for r in corrections))
        self.ledger.ingest_receipts(corrections);self.assertNotEqual(self.query()['groups'][0]['key']['wi'],WI)
    def test_other_request_in_same_scope_remains_unknown(self):
        self.prepare();other=Receipt.make('usage','other-call',self.row.payload,
            occurred_at=fixtures.AT,observed_at=fixtures.AT,producer=self.row.producer)
        self.ledger.ingest_receipts([self.row,self.weak,other]);self.ledger.ingest_receipts(self.correct())
        self.assertEqual(self.query()['summary']['usage']['wi_unassigned_event_count'],1)
        self.assertEqual(self.query()['summary']['usage']['event_count'],2)
    def test_concurrent_heads_are_not_silently_selected(self):
        self.prepare();other=Receipt.make('attribution',None,{**self.weak.payload,'policy':'independent-policy'},
            entity_ref=self.weak.entity_ref,occurred_at=fixtures.AT,observed_at=fixtures.AT,producer='independent.v1',evidence=(self.proof,))
        self.ledger.ingest_receipts([self.row,self.weak,other]);corrections=self.correct()
        self.assertTrue(all(r.supersedes is None for r in corrections))
        self.ledger.ingest_receipts(corrections);self.assertNotEqual(self.query()['groups'][0]['key']['wi'],WI)

    def test_growing_usage_reviews_merge_proof_without_repeated_new_facts(self):
        from datetime import datetime,timedelta
        self.prepare()
        end=(datetime.fromisoformat(fixtures.AT.replace('Z','+00:00'))+timedelta(seconds=1)).isoformat().replace('+00:00','Z')
        grown=Receipt.make('usage',None,{**self.row.payload,'interval_end':end},
            entity_ref=self.row.entity_ref,supersedes=self.row.fact_ref,occurred_at=end,
            observed_at=end,producer=self.row.producer)
        extra=reference('reviewed-late-proof','same-request-new-native-prefix')
        self.document['items'].append({**self.item,'target_fact_ref':grown.fact_ref,
            'usage_interval':[fixtures.AT,end],'evidence_refs':[self.proof,extra]})
        self.ledger.ingest_receipts([self.row,grown,self.weak]);mark=self.ledger.receipt_snapshot()['watermark'];before=self.query(mark)
        self.ledger.ingest_receipts(self.correct());after=self.query()
        self.assertEqual(after['groups'][0]['key']['wi'],WI)
        self.assertEqual(after['summary']['usage']['tokens'],before['summary']['usage']['tokens'])
        self.assertEqual(self.query(mark),before)
        self.assertEqual(self.ledger.ingest_receipts(self.correct())['inserted'],0)
        newest=reference('another-proof','same-purpose-enrichment')
        self.document['items'][-1]['evidence_refs'].append(newest)
        enrichment=self.correct();self.assertTrue(any(newest in r.evidence for r in enrichment))
        self.ledger.ingest_receipts(enrichment)
        self.assertEqual(self.ledger.ingest_receipts(self.correct())['inserted'],0)
        self.assertEqual(self.query()['summary']['usage']['tokens'],before['summary']['usage']['tokens'])

if __name__=='__main__':unittest.main()
