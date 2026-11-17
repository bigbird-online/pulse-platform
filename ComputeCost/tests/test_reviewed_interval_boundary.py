"""Finite reviewed usage keeps its full interval and immutable revision history."""
from pathlib import Path
import tempfile
import unittest
from pulse_compute_cost.receipts import Receipt, digest, reference
from pulse_compute_cost.receipt_sources import usage_receipt
from pulse_compute_cost.activity_classification import reviewed_activity_receipts
from pulse_compute_cost.collaboration_query import CollaborationQuery, query_collaboration
from pulse_compute_cost.ledger import UsageLedger
from test_collaboration_receipts import CATALOG

START='2026-09-13T00:59:56Z'
END='2026-09-13T01:00:00Z'
class ReviewedIntervalBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.ledger=UsageLedger(Path(self.tmp.name)/'ledger')
        self.row=usage_receipt('codex','real-shaped-turn',{'input_tokens':19682,'cached_input_tokens':12416,'cache_write_input_tokens':0,'output_tokens':11},
            at=END,observed=END,model='openai/test',interval=(START,END))
        self.item={'target_fact_ref':self.row.fact_ref,'agent':'hestia','node':'ownermac','entrypoint':'codex_app',
            'work_item_id':'wi_20260924_219','evidence_refs':[reference('review','original-launch-and-task')]}
        self.document={'schema':'computecost_reviewed_activity/v1','items':[self.item]}
        self.ledger.ingest_receipts([self.row])
    def query(self,mark=None):
        return query_collaboration(self.ledger,CollaborationQuery(scope='activity',from_date='2026-09-13',to_date='2026-09-14',
            group_by=('agent','wi','entrypoint','node','runtime','model'),watermark=mark),catalog=CATALOG,catalog_observed_at=END)
    def annotations(self):
        return reviewed_activity_receipts(self.ledger.receipt_snapshot()['facts'],self.document,END)
    def legacy(self,wi='wi_20260924_219'):
        return Receipt.make('attribution',['reviewed_work_item',self.row.entity_ref,digest(self.item)],{
            'target_ref':self.row.entity_ref,'wi':wi,'executor':'hestia','responsible':None,'delegator':None,
            'state':'assigned','policy':'reviewed_activity.v1','effective_at':END},occurred_at=END,observed_at=END,
            producer='reviewed_activity.v1',evidence=(reference('reviewed_activity',digest(self.item)),self.row.fact_ref))
    def test_whole_real_interval_is_assigned_without_changing_usage(self):
        mark=self.ledger.receipt_snapshot()['watermark'];before=self.query(mark)
        rows=self.annotations();self.ledger.ingest_receipts(rows);after=self.query()
        self.assertEqual(after['groups'][0]['key']['wi'],'wi_20260924_219')
        self.assertEqual(after['groups'][0]['key']['agent'],'hestia')
        self.assertEqual(after['summary']['usage']['tokens'],before['summary']['usage']['tokens'])
        self.assertEqual(after['summary']['cost']['partial_machine_cash_usd'],before['summary']['cost']['partial_machine_cash_usd'])
        self.assertEqual(before,self.query(mark))
        self.assertTrue(all(row.kind=='attribution' for row in rows))
    def test_legacy_end_boundary_is_revised_once_and_old_query_is_preserved(self):
        old=self.legacy();self.ledger.ingest_receipts([old]);mark=self.ledger.receipt_snapshot()['watermark'];before=self.query(mark)
        rows=self.annotations();corrected=next(r for r in rows if r.entity_ref==old.entity_ref)
        self.assertEqual(corrected.supersedes,old.fact_ref)
        self.assertEqual(corrected.payload['effective_at'],START)
        self.ledger.ingest_receipts(rows)
        self.assertEqual(self.query()['groups'][0]['key']['wi'],'wi_20260924_219')
        self.assertEqual(self.query(mark),before)
        self.assertEqual(self.ledger.ingest_receipts(self.annotations())['inserted'],0)
    def test_contrary_wi_is_not_silently_replaced(self):
        old=self.legacy('wi_20260924_999');self.ledger.ingest_receipts([old])
        candidate=next(r for r in self.annotations() if r.entity_ref==old.entity_ref)
        self.assertIsNone(candidate.supersedes)
        self.ledger.ingest_receipts(self.annotations())
        self.assertTrue(self.query()['revision_conflicts'])
