"""An original ordinary-work review can be explicitly corrected, not hidden."""
import unittest
import test_non_wi_activity as f
from pulse_compute_cost.activity_classification import reviewed_activity_receipts,reviewed_purpose_receipt
from pulse_compute_cost.receipts import Receipt,ReceiptError,non_wi_attribution

class ReviewedNoneCorrectionTests(unittest.TestCase):
    setUp=f.NonWiActivityTests.setUp
    query=f.NonWiActivityTests.query
    def prepare(self,kind='none'):
        prior=non_wi_attribution(self.row,work_kind=kind,observed_at=f.AT,evidence=(self.proof,))
        item={'target_fact_ref':self.row.fact_ref,'work_item_id':'wi_20260924_219',
            'agent':None,'node':None,'entrypoint':None,'source_attribution_fact_ref':prior.fact_ref,
            'usage_interval':[f.AT,f.AT],'review_basis':'native_lifecycle_request','evidence_refs':[self.proof]}
        self.ledger.ingest_receipts([self.row,prior])
        return prior,item,{'schema':'computecost_reviewed_activity/v1','items':[item]}
    def test_exact_corrected_none_preserves_prior_watermark_and_quantity(self):
        prior,item,doc=self.prepare();mark=self.ledger.receipt_snapshot()['watermark'];before=self.query(mark)
        changes=reviewed_activity_receipts(self.ledger.receipt_snapshot()['facts'],doc,f.AT)
        self.assertEqual(len(changes),1);self.assertEqual(changes[0].supersedes,prior.fact_ref)
        self.ledger.ingest_receipts(changes);after=self.query()
        self.assertEqual(after['groups'][0]['key']['wi'],'wi_20260924_219')
        self.assertEqual(before['summary']['usage']['tokens'],after['summary']['usage']['tokens'])
        self.assertEqual(before['summary']['cost']['partial_machine_cash_usd'],after['summary']['cost']['partial_machine_cash_usd'])
        self.assertEqual(self.query(mark),before)
        self.assertEqual(self.ledger.ingest_receipts(reviewed_activity_receipts(self.ledger.receipt_snapshot()['facts'],doc,f.AT))['inserted'],0)
    def test_cron_and_unspecified_previous_review_are_not_overwritten(self):
        prior,item,doc=self.prepare('cron')
        with self.assertRaises(ReceiptError):reviewed_purpose_receipt(self.row,prior,item,f.AT)
        prior,item,doc=self.prepare()
        for field,value in [('review_basis','native_user_request'),('source_attribution_fact_ref',self.proof)]:
            with self.assertRaises(ReceiptError):reviewed_purpose_receipt(self.row,prior,{**item,field:value},f.AT)
    def test_strong_independent_task_remains_a_conflict(self):
        prior,item,doc=self.prepare()
        strong=Receipt.make('attribution','other-task',{'target_ref':self.row.entity_ref,'wi':'wi_20260924_220','executor':None,
            'responsible':None,'delegator':None,'state':'assigned','policy':'independent_review.v1','effective_at':f.AT},
            occurred_at=f.AT,observed_at=f.AT,producer='independent_review.v1',evidence=(self.proof,))
        self.ledger.ingest_receipts([strong]);self.ledger.ingest_receipts(reviewed_activity_receipts(self.ledger.receipt_snapshot()['facts'],doc,f.AT))
        self.assertEqual(self.query()['groups'][0]['key']['wi'],'ambiguous')

if __name__=='__main__':unittest.main()
