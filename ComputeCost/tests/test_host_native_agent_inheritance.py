"""The priced native replacement must retain a proven Host execution's identity.

Regression for WI300's 2026-09-15 Aki report. No directory-name inference,
legacy snapshot backfill, new observations, or weakening of overlap proof.
"""
from dataclasses import replace
import json
from pathlib import Path
import tempfile
import unittest

from pulse_compute_cost.collaboration_query import CollaborationQuery, query_collaboration, reconcile_usage
from pulse_compute_cost.ledger import UsageLedger
from pulse_compute_cost.native_inventory import pi_prefix_inventory
from pulse_compute_cost.receipt_sources import usage_receipt
from pulse_compute_cost.receipts import Receipt, reference, canonical

WI='wi_20260915_001'
START='2026-09-15T01:00:00Z'; AT='2026-09-15T01:00:30Z'; END='2026-09-15T01:01:00Z'
CATALOG={'data':[{'id':'openai/test','pricing':{'prompt':'1','completion':'1','input_cache_read':'0.1','input_cache_write':'2'}}]}


class HostNativeAgentInheritanceTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.ledger=UsageLedger(Path(self.tmp.name)/'ledger')
        self.scope=reference('native_session',['pi','session'])
        common={'wi':WI,'executor':'aki','responsible':None,'delegator':None}
        self.execution=Receipt.make('execution','dispatch',dict(common,runtime='pi',
            assignment_ref=reference('assignment','binding'),parent_ref=None,state='completed',expected_usage=True),
            occurred_at=START,observed_at=END,producer='relay_host.v1')
        self.native=usage_receipt('pi',['session','request','assistant'],
            {'input':100,'output':10,'cacheRead':20,'cacheWrite':0},at=AT,observed=END,model='openai/test',native_scope_ref=self.scope)
        self.host=usage_receipt('pi','host',{'input_tokens':100,'output_tokens':10,'cache_read_input_tokens':20,'cache_creation_input_tokens':0},
            at=END,observed=END,model='openai/test',execution_ref=self.execution.entity_ref,common=common,
            granularity='turn_aggregate',mapping='relay_pi.v1',interval=(START,END),native_scope_ref=self.scope)
        raw=[{'type':'session','id':'session'},{'type':'message','id':'request','message':{'role':'assistant'}}]
        data=b''.join(json.dumps(r).encode()+b'\n' for r in raw)
        self.inventory=pi_prefix_inventory(data,[self.native],reference('source','input'),END)

    def read(self,**kwargs):
        return query_collaboration(self.ledger,CollaborationQuery(from_date='2026-09-15',to_date='2026-09-16',scope='activity',**kwargs),
            catalog=CATALOG,catalog_observed_at=END)

    def put(self):self.ledger.ingest_receipts([self.execution,self.host,self.native,self.inventory])

    def test_exact_replacement_keeps_agent_without_v2_snapshot(self):
        self.put();result=self.read(group_by=('agent',))
        groups={g['key']['agent']:g for g in result['groups']}
        self.assertEqual(groups['aki']['usage']['event_count'],1)
        self.assertEqual(groups['aki']['cost']['partial_machine_cash_usd'],'112')
        self.assertEqual(result['summary']['usage']['unassigned_event_count'],0)
        self.assertEqual(result['summary']['usage']['known_total_tokens'],130)
        self.assertEqual(len(result['reconciliation']['native_boundary_supersession']),1)
        self.assertNotIn('join_conflict',result['summary']['cost']['reasons'])

    def test_wi_and_agent_filters_run_after_proven_attribution(self):
        self.put()
        for options in ({'work_items':(WI,)},{'agent':'aki'},{'work_items':(WI,),'agent':'aki'}):
            result=self.read(**options)
            self.assertEqual(result['summary']['usage']['event_count'],1)
            self.assertEqual(result['summary']['cost']['partial_machine_cash_usd'],'112')

    def test_quantity_mismatch_is_not_attribution_proof(self):
        p=self.host.payload;p['tokens']['uncached_input']=101
        p['native_tokens']['input_tokens']=101
        self.host=Receipt.make('usage',None,p,entity_ref=self.host.entity_ref,
            occurred_at=self.host.occurred_at,observed_at=self.host.observed_at,producer=self.host.producer)
        self.put()
        result=self.read()
        self.assertFalse(result['reconciliation']['native_boundary_supersession'])
        self.assertIn('join_conflict',result['summary']['cost']['reasons'])
        self.assertIsNone(result['summary']['cost']['partial_machine_cash_usd'])

    def test_missing_execution_or_assignment_cannot_supply_actor(self):
        for executions in ([],[replace(self.execution,_payload_json=canonical({**self.execution.payload,'assignment_ref':None}))]):
            rows,_=reconcile_usage([self.native,self.host],executions=executions,inventories=[self.inventory])
            native=next(r for r in rows if r.entity_ref==self.native.entity_ref)
            self.assertIsNone(native.payload['executor'])
            self.assertIsNone(native.payload['wi'])

    def test_assignment_after_native_request_does_not_backfill(self):
        execution=replace(self.execution,occurred_at=END)
        rows,_=reconcile_usage([self.native,self.host],executions=[execution],inventories=[self.inventory])
        native=next(r for r in rows if r.entity_ref==self.native.entity_ref)
        self.assertIsNone(native.payload['executor'])

    def test_conflicting_known_actor_is_not_overwritten(self):
        native=replace(self.native,_payload_json=canonical({**self.native.payload,'executor':'other'}))
        rows,meta=reconcile_usage([native,self.host],executions=[self.execution],inventories=[self.inventory])
        self.assertFalse(meta['native_boundary_supersession'])
        self.assertTrue(meta['conflicting_refs'])

    def test_host_execution_disagreement_is_not_identity_authority(self):
        execution=replace(self.execution,_payload_json=canonical({**self.execution.payload,'executor':'other'}))
        rows,meta=reconcile_usage([self.native,self.host],executions=[execution],inventories=[self.inventory])
        self.assertFalse(meta['native_boundary_supersession'])
        self.assertTrue(meta['conflicting_refs'])

    def test_query_is_read_only_and_repeatable(self):
        self.put();before=self.ledger.receipt_snapshot()
        first=self.read(group_by=('agent',));second=self.read(group_by=('agent',))
        self.assertEqual(first,second)
        self.assertEqual(self.ledger.receipt_snapshot()['sha256'],before['sha256'])
        original=next(r for r in before['facts'] if r.entity_ref==self.native.entity_ref)
        self.assertIsNone(original.payload['executor'])


if __name__=='__main__':unittest.main()
