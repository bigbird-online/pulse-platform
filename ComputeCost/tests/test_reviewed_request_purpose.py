import copy
from dataclasses import replace
import unittest
import test_non_wi_activity as fixtures
from pulse_compute_cost.activity_classification import reviewed_activity_receipts, reviewed_purpose_receipt
from pulse_compute_cost.receipts import Receipt, ReceiptError, reference, canonical
from pulse_compute_cost.receipt_sources import actor_receipt

WI='wi_20260922_001'
class ReviewedRequestPurposeTests(unittest.TestCase):
    setUp=fixtures.NonWiActivityTests.setUp
    query=fixtures.NonWiActivityTests.query
    def prepare(self):
        old=self.row
        self.row=Receipt.make('usage',None,{**old.payload,'interval_start':fixtures.AT,
            'interval_end':fixtures.AT},entity_ref=old.entity_ref,occurred_at=old.occurred_at,
            observed_at=old.observed_at,producer=old.producer)
        self.weak=Receipt.make('attribution','weak',{'target_ref':self.row.entity_ref,'wi':None,'executor':None,
            'responsible':None,'delegator':None,'state':'ambiguous','policy':'scl_host_session_binding.v1',
            'effective_at':fixtures.AT},occurred_at=fixtures.AT,observed_at=fixtures.AT,
            producer='scl_host_session_binding.v1',evidence=(self.proof,))
        self.item={'target_fact_ref':self.row.fact_ref,'work_item_id':WI,'agent':None,'node':None,'entrypoint':None,
            'source_attribution_fact_ref':self.weak.fact_ref,'usage_interval':[fixtures.AT,fixtures.AT],
            'review_basis':'native_user_request','evidence_refs':[self.proof]}
        self.document={'schema':'computecost_reviewed_activity/v1','items':[self.item]}
        self.ledger.ingest_receipts([self.row,self.weak])
    def corrections(self,facts=None):
        return reviewed_activity_receipts(facts or self.ledger.receipt_snapshot()['facts'],self.document,fixtures.AT)
    def test_exact_correction_conserves_usage_old_watermark_and_actor(self):
        self.prepare();actor=actor_receipt(self.row,node='ownermac',executor='hestia',entrypoint='codex_app',observed=fixtures.AT,evidence=(self.proof,))
        self.ledger.ingest_receipts([actor]);before=self.ledger.receipt_snapshot();old=self.query(before['watermark'])
        correction=self.corrections();self.assertEqual(len(correction),1)
        self.assertEqual(correction[0].supersedes,self.weak.fact_ref)
        self.ledger.ingest_receipts(correction);after=self.query()
        self.assertEqual(after['groups'][0]['key']['wi'],WI)
        self.assertEqual(after['groups'][0]['key']['agent'],'hestia')
        self.assertEqual(after['summary']['usage']['known_total_tokens'],old['summary']['usage']['known_total_tokens'])
        self.assertEqual(self.query(before['watermark']),old)
        self.assertEqual(self.ledger.receipt_snapshot(watermark=before['watermark'])['sha256'],before['sha256'])
        self.assertEqual(self.ledger.ingest_receipts(self.corrections())['inserted'],0)
        self.ledger.ingest_receipts([self.weak]);self.assertEqual(self.query(),after)
    def test_missing_original_proof_or_foreign_usage_is_not_corrected(self):
        self.prepare();self.assertEqual(self.corrections([self.row]),[])
        foreign=Receipt.make('usage','foreign',self.row.payload,occurred_at=fixtures.AT,observed_at=fixtures.AT,producer=self.row.producer)
        with self.assertRaises(ReceiptError):reviewed_purpose_receipt(foreign,self.weak,self.item,fixtures.AT)
    def test_interval_or_review_basis_cannot_be_invented(self):
        self.prepare()
        for delta in ({'usage_interval':['2026-01-01T00:00:00Z',fixtures.AT]},{'review_basis':'latest_WI_guess'}):
            with self.assertRaises(ReceiptError):reviewed_purpose_receipt(self.row,self.weak,dict(self.item,**delta),fixtures.AT)
    def test_new_actor_or_node_assertions_are_refused(self):
        self.prepare()
        for field in ('agent','node','entrypoint'):
            document=copy.deepcopy(self.document);document['items'][0][field]='pulse'
            with self.assertRaises(ReceiptError):reviewed_activity_receipts([self.row,self.weak],document,fixtures.AT)
    def test_a_newer_or_competing_decision_is_not_overwritten(self):
        self.prepare()
        other=Receipt.make('attribution',None,{**self.weak.payload,'wi':'wi_20260922_002','state':'assigned'},
            entity_ref=self.weak.entity_ref,supersedes=self.weak.fact_ref,occurred_at=fixtures.AT,observed_at=fixtures.AT,producer='independent_review.v1')
        self.ledger.ingest_receipts([other]);self.assertEqual(self.corrections(),[])
        self.assertEqual(self.query()['groups'][0]['key']['wi'],'wi_20260922_002')
    def test_known_task_and_strong_assignments_remain_protected(self):
        self.prepare()
        strong=replace(self.weak,_payload_json=canonical({**self.weak.payload,'wi':WI,'state':'assigned'}))
        known=replace(self.row,_payload_json=canonical({**self.row.payload,'wi':WI}))
        for row,prior in ((known,self.weak),(self.row,strong)):
            with self.assertRaises(ReceiptError):reviewed_purpose_receipt(row,prior,self.item,fixtures.AT)

if __name__=='__main__':unittest.main()
