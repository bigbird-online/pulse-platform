import json
import tempfile
import unittest
from pathlib import Path
from pulse_compute_cost.receipts import Receipt, TOKEN_FIELDS, canonical, reference
from pulse_compute_cost.receipt_sources import usage_receipt
from pulse_compute_cost.collaboration_query import reconcile_usage, query_collaboration, CollaborationQuery
from pulse_compute_cost.ledger import UsageLedger
from pulse_compute_cost.native_inventory import pi_prefix_inventory

START='2026-09-23T07:03:10Z'
AT='2026-09-23T07:03:48Z'
END='2026-09-23T07:03:58Z'
LATER='2026-09-23T07:05:00Z'
WI='wi_20260823_184'

class MissingHostMeasurementTests(unittest.TestCase):
    def setUp(self):
        self.scope=reference('native_session',['pi','observed-session'])
        self.native=usage_receipt('pi',['observed-session','request','assistant'],
            {'input':100,'output':10,'cacheRead':20,'cacheWrite':0},at=AT,observed=END,
            model='openai/test',native_scope_ref=self.scope)
        # The next request of the session, after the Host interval.
        self.later=self.request('later',LATER)
        self.host=usage_receipt('pi','empty-host',{},at=END,observed=END,model=None,
            common={'wi':WI,'executor':'pulse','responsible':None,'delegator':None},
            granularity='turn_aggregate',mapping='relay_pi.v1',interval=(START,END),
            native_scope_ref=self.scope)
        self.catalog={'data':[{'id':'openai/test','pricing':{'prompt':'1','completion':'1','input_cache_read':'0.1','input_cache_write':'2'}}]}
    def changed_host(self,**changes):
        return Receipt.make('usage',None,{**self.host.payload,**changes},entity_ref=self.host.entity_ref,
            occurred_at=END,observed_at=END,producer=self.host.producer)
    def query(self,ledger):
        return query_collaboration(ledger,CollaborationQuery(from_date='2026-09-23',to_date='2026-09-24',scope='activity'),
            catalog=self.catalog,catalog_observed_at=END)
    def test_missing_summary_preserves_both_evidence_rows_without_false_conflict(self):
        rows,meta=reconcile_usage([self.native,self.host])
        self.assertEqual(meta['conflicting_refs'],[])
        self.assertEqual(meta['suppressed_aggregate_refs'],[])
        self.assertEqual({r.entity_ref for r in rows},{self.native.entity_ref,self.host.entity_ref})
        self.assertEqual(next(r for r in rows if r.entity_ref==self.native.entity_ref),self.native)
    def test_missing_measurement_keeps_unknowns_and_does_not_invent_a_complete_total(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger=UsageLedger(Path(tmp)/'ledger');ledger.ingest_receipts([self.native]);before=self.query(ledger)
            ledger.ingest_receipts([self.host]);original=ledger.receipt_snapshot()['sha256'];after=self.query(ledger)
            self.assertEqual(after['summary']['usage']['conflicted_event_count'],0)
            self.assertNotIn('join_conflict',after['summary']['cost']['reasons'])
            self.assertEqual(after['summary']['cost']['partial_machine_cash_usd'],before['summary']['cost']['partial_machine_cash_usd'])
            self.assertIsNone(after['summary']['cost']['machine_cash_usd'])
            self.assertTrue(after['summary']['cost']['unpriced'])
            self.assertTrue(any(v['unknown_event_count'] for v in after['summary']['usage']['tokens'].values()))
            self.assertEqual(self.query(ledger),after)
            self.assertEqual(ledger.receipt_snapshot()['sha256'],original)
    def request(self,ident,at):
        return usage_receipt('pi',['observed-session',ident,'assistant'],
            {'input':100,'output':10,'cacheRead':20,'cacheWrite':0},at=at,observed=LATER,
            model='openai/test',native_scope_ref=self.scope)
    def prefix(self,*rows):
        # A complete parsed native file holding exactly these requests.
        names={reference('usage',['observed-session',i,'assistant']):i for i in ('request','later','second')}
        records=[{'type':'session','id':'observed-session'}]+[
            {'type':'message','id':names[r.entity_ref],'message':{'role':'assistant'}} for r in rows]
        data=b''.join(canonical(r)+b'\n' for r in records)
        return pi_prefix_inventory(data,list(rows),reference('source','pi'),LATER)
    def test_native_prefix_past_the_turn_backfills_the_missing_turn(self):
        inventory=self.prefix(self.native,self.later)
        rows,meta=reconcile_usage([self.native,self.later,self.host],inventories=[inventory])
        self.assertEqual(meta['conflicting_refs'],[])
        self.assertEqual(meta['suppressed_aggregate_refs'],[self.host.entity_ref])
        self.assertEqual({r.entity_ref for r in rows},{self.native.entity_ref,self.later.entity_ref})
        with tempfile.TemporaryDirectory() as tmp:
            ledger=UsageLedger(Path(tmp)/'ledger');ledger.ingest_receipts([self.native,self.later,inventory]);before=self.query(ledger)
            ledger.ingest_receipts([self.host]);after=self.query(ledger)
            self.assertEqual(after['summary']['cost'],before['summary']['cost'])
            self.assertEqual(after['summary']['cost']['unpriced'],[])
    def assert_unknown(self,rows,inventories):
        rows,meta=reconcile_usage(rows,inventories=inventories)
        self.assertEqual(meta['suppressed_aggregate_refs'],[])
        self.assertIn(self.host.entity_ref,{r.entity_ref for r in rows})
    def test_prefix_ending_inside_the_turn_keeps_it_unknown(self):
        # Only the tool-use request so far: the turn's end is not yet read.
        self.assert_unknown([self.native,self.host],[self.prefix(self.native)])
    def test_lagging_copy_without_the_later_request_keeps_it_unknown(self):
        # Another file shows the later request; this prefix does not reach it.
        self.assert_unknown([self.native,self.later,self.host],[self.prefix(self.native)])
    def test_partially_collected_turn_keeps_it_unknown(self):
        second=self.request('second','2026-09-23T07:03:50Z')
        self.assert_unknown([self.native,second,self.later,self.host],[self.prefix(self.native,self.later)])
    def test_requests_without_prefix_evidence_or_of_another_session_keep_it_unknown(self):
        self.assert_unknown([self.native,self.later,self.host],[])
        other=reference('native_session',['pi','other-session'])
        moved=Receipt.make('usage',None,{**self.host.payload,'native_scope_ref':other},entity_ref=self.host.entity_ref,
            occurred_at=END,observed_at=END,producer=self.host.producer)
        rows,meta=reconcile_usage([self.native,self.later,moved],inventories=[self.prefix(self.native,self.later)])
        self.assertEqual(meta['suppressed_aggregate_refs'],[])
    def test_partial_or_zero_numeric_measurements_are_not_missing(self):
        for numbers in ({k:0 for k in TOKEN_FIELDS},{**{k:None for k in TOKEN_FIELDS},'output':1}):
            host=self.changed_host(tokens=numbers)
            rows,meta=reconcile_usage([self.native,host]);self.assertTrue(meta['conflicting_refs'])
    def test_raw_native_counter_evidence_is_not_discarded(self):
        host=self.changed_host(native_tokens={'output_tokens':1})
        rows,meta=reconcile_usage([self.native,host]);self.assertTrue(meta['conflicting_refs'])
    def test_explicit_coverage_with_missing_quantities_is_still_unproven(self):
        host=self.changed_host(covers=[self.native.entity_ref])
        rows,meta=reconcile_usage([self.native,host]);self.assertTrue(meta['conflicting_refs'])
    def test_missing_summary_does_not_grant_its_wi_or_actor_to_native_request(self):
        rows,meta=reconcile_usage([self.native,self.host])
        native=next((r for r in rows if r.entity_ref==self.native.entity_ref),None)
        self.assertIsNotNone(native)
        self.assertIsNone(native.payload['wi']);self.assertIsNone(native.payload['executor'])

if __name__=='__main__':unittest.main()
