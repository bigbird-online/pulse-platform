import unittest
from datetime import datetime,timedelta
import test_non_wi_activity as fixture
from pulse_compute_cost.activity_classification import reviewed_activity_receipts
from pulse_compute_cost.receipt_sources import actor_receipt
from pulse_compute_cost.receipts import Receipt
from test_collaboration_receipts import AT

class PurposeOnlyReviewTests(unittest.TestCase):
    setUp=fixture.NonWiActivityTests.setUp
    query=fixture.NonWiActivityTests.query
    def test_exact_native_request_repairs_only_wi_and_preserves_old_watermark(self):
        start=(datetime.fromisoformat(AT.replace('Z','+00:00'))-timedelta(minutes=1)).isoformat().replace('+00:00','Z')
        self.row=Receipt.make('usage','aggregate-review-fixture',{**self.row.payload,'granularity':'turn_aggregate','interval_start':start,'interval_end':AT},occurred_at=AT,observed_at=AT,producer=self.row.producer)
        actor=actor_receipt(self.row,node='ownermac',executor='pulse',entrypoint='codex_app',observed=AT,evidence=(self.proof,))
        self.ledger.ingest_receipts([self.row,actor]);mark=self.ledger.receipt_snapshot()['watermark'];before=self.query(mark)
        doc={'schema':'computecost_reviewed_activity/v1','items':[{'target_fact_ref':self.row.fact_ref,
             'work_item_id':'wi_20260901_101','usage_interval':[start,AT],'review_basis':'native_user_request','agent':None,'node':None,'entrypoint':None,'evidence_refs':[self.proof]}]}
        rows=reviewed_activity_receipts([self.row,actor],doc,AT);self.ledger.ingest_receipts(rows)
        after=self.query();group=after['groups'][0]
        self.assertEqual(group['key']['wi'],'wi_20260901_101');self.assertEqual(group['key']['agent'],'pulse')
        self.assertEqual(group['key']['node'],'ownermac');self.assertEqual(group['key']['entrypoint'],'codex_app')
        self.assertEqual(before['summary']['usage']['tokens'],after['summary']['usage']['tokens'])
        self.assertEqual(before['summary']['cost']['partial_machine_cash_usd'],after['summary']['cost']['partial_machine_cash_usd'])
        self.assertEqual(self.query(mark),before);self.assertEqual(self.ledger.ingest_receipts(rows)['inserted'],0)
        future=Receipt.make('usage','later-independent-request',self.row.payload,occurred_at=AT,observed_at=AT,producer=self.row.producer)
        self.assertEqual(reviewed_activity_receipts([future],doc,AT),[])
