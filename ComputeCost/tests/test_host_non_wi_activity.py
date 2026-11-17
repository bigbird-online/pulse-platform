"""A native null WI alone is unknown; an exact unbound Host dispatch is not."""
from dataclasses import replace
from pathlib import Path
import tempfile
import unittest

from pulse_compute_cost.receipts import HOST_FALLBACK, Receipt, canonical, non_wi_attribution, reference
from pulse_compute_cost.receipt_sources import read_pi, usage_receipt
from pulse_compute_cost.native_inventory import pi_prefix_inventory
from pulse_compute_cost.activity_classification import collect_reviewed_activity
from pulse_compute_cost.ledger import UsageLedger
from pulse_compute_cost.collaboration_query import query_collaboration, CollaborationQuery
from test_collaboration_receipts import CATALOG

START='2026-09-16T08:03:55Z'
END='2026-09-16T08:04:25Z'
WI='wi_20260901_001'
class HostNonWiTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name);self.ledger=UsageLedger(self.root/'ledger')
        self.registry={'required_receipt_features':['non_wi_activity.v1']}
        records=[{'type':'session','id':'native-session','version':3,'timestamp':START},
                 {'type':'message','id':'native-call','parentId':None,'timestamp':END,'message':
                  {'role':'assistant','model':'test','provider':'fixture','usage':
                   {'input':10,'output':2,'cacheRead':0,'cacheWrite':0,'totalTokens':12}}}]
        data=b''.join(canonical(row)+b'\n' for row in records)
        self.native=read_pi(self.root/'native.jsonl',END,data=data)[0]
        self.execution=Receipt.make('execution','dispatch',{'wi':None,'executor':'asobi','responsible':None,'delegator':None,
            'runtime':'pi','assignment_ref':None,'parent_ref':None,'state':'completed','expected_usage':True},
            occurred_at=START,observed_at=END,producer='relay_host.v1',evidence=(reference('host_record','dispatch'),))
        self.host=usage_receipt('pi','host',{'input_tokens':10,'output_tokens':2,'cache_read_input_tokens':0,'cache_creation_input_tokens':0},
            at=END,observed=END,model='test',provider='fixture',mapping='relay_pi.v1',granularity='turn_aggregate',
            common={'wi':None,'executor':'asobi','responsible':None,'delegator':None},
            execution_ref=self.execution.entity_ref,interval=(START,END),
            native_scope_ref=self.native.payload['native_scope_ref'])
        self.inventory=pi_prefix_inventory(data,[self.native],reference('source','fixture'),END)

    def classify(self, facts=None):
        return collect_reviewed_activity(facts if facts is not None else self.facts(),self.registry,END)

    def facts(self):return [self.native,self.host,self.execution,self.inventory]

    def test_exact_host_native_pair_records_confirmed_none_without_new_usage(self):
        rows=self.classify();self.assertEqual(len(rows),1)
        self.assertEqual(rows[0].payload['work_kind'],'none')
        self.assertEqual(rows[0].payload['target_ref'],self.native.entity_ref)
        self.assertIn(self.execution.fact_ref,rows[0].evidence)
        self.assertIn(self.host.fact_ref,rows[0].evidence)
        self.assertIn(self.inventory.fact_ref,rows[0].evidence)
        self.assertIsNone(rows[0].payload['wi']);self.assertIsNone(rows[0].payload['executor'])
        self.ledger.ingest_receipts(self.facts());mark=self.ledger.receipt_snapshot()['watermark']
        q=CollaborationQuery(scope='activity',from_date='2026-09-14',to_date='2026-09-17',group_by=('wi',),watermark=mark)
        before=query_collaboration(self.ledger,q,catalog=CATALOG,catalog_observed_at=END)
        self.ledger.ingest_receipts(rows)
        after=query_collaboration(self.ledger,replace(q,watermark=None),catalog=CATALOG,catalog_observed_at=END)
        self.assertEqual(after['summary']['usage']['known_total_tokens'],12)
        self.assertEqual(after['summary']['usage']['wi_unassigned_event_count'],0)
        self.assertEqual(after['summary']['usage']['no_wi_event_count'],1)
        self.assertEqual(after['summary']['cost']['partial_machine_cash_usd'],before['summary']['cost']['partial_machine_cash_usd'])
        self.assertEqual(query_collaboration(self.ledger,q,catalog=CATALOG,catalog_observed_at=END),before)
        self.assertEqual(self.ledger.ingest_receipts(rows)['inserted'],0)

    def test_missing_host_or_inventory_does_not_turn_null_into_none(self):
        for facts in ([self.native],[self.native,self.execution],[self.native,self.host,self.execution]):
            self.assertFalse(self.classify(facts))
        self.registry={};self.assertFalse(self.classify())

    def test_task_bound_or_uncertain_execution_cannot_become_none(self):
        for change in ({'wi':WI},{'assignment_ref':reference('assignment','real')},{'state':'unknown'},
                       {'executor':None},{'expected_usage':False}):
            modified=Receipt.make('execution',None,{**self.execution.payload,**change},entity_ref=self.execution.entity_ref,
                occurred_at=START,observed_at=END,producer='relay_host.v1',evidence=self.execution.evidence)
            self.assertFalse(self.classify([self.native,self.host,modified,self.inventory]))

    def test_actual_task_attribution_and_host_history_conflicts_are_preserved(self):
        task=Receipt.make('attribution','real-wi',{'target_ref':self.native.entity_ref,'wi':WI,'executor':'asobi',
            'responsible':None,'delegator':None,'state':'assigned','policy':'fixture','effective_at':START},
            occurred_at=START,observed_at=END,producer='fixture')
        self.assertFalse(self.classify(self.facts()+[task]))
        old=Receipt.make('execution',None,{**self.execution.payload,'wi':WI},entity_ref=self.execution.entity_ref,
            occurred_at=START,observed_at=END,producer='relay_host.v1',evidence=self.execution.evidence)
        current=Receipt.make('execution',None,self.execution.payload,entity_ref=self.execution.entity_ref,
            occurred_at=START,observed_at=END,producer='relay_host.v1',evidence=self.execution.evidence,supersedes=old.fact_ref)
        self.assertFalse(self.classify([self.native,self.host,self.inventory,old,current]))

    def test_classification_is_a_marked_fallback_and_old_ones_are_revised_once(self):
        rows=self.classify();self.assertIn(HOST_FALLBACK,rows[0].evidence)
        self.assertEqual(self.classify(self.facts()+rows),[])
        legacy=non_wi_attribution(self.native,work_kind='none',observed_at=END,
            evidence=tuple(r for r in rows[0].evidence if r!=HOST_FALLBACK))
        revised=self.classify(self.facts()+[legacy])
        self.assertEqual([(r.supersedes,HOST_FALLBACK in r.evidence) for r in revised],[(legacy.fact_ref,True)])

    def test_a_held_wi_of_the_same_request_decides_before_the_fallback(self):
        rows=self.classify()
        held=Receipt.make('attribution','held',{'target_ref':self.native.entity_ref,'wi':WI,'executor':None,
            'responsible':None,'delegator':None,'state':'assigned','policy':'holder_supplement.v1','effective_at':START},
            occurred_at=START,observed_at=END,producer='holder_supplement.v1',evidence=(reference('core_session_write','x'),))
        self.ledger.ingest_receipts(self.facts()+rows+[held])
        q=CollaborationQuery(scope='activity',from_date='2026-09-14',to_date='2026-09-17',group_by=('wi',))
        out=query_collaboration(self.ledger,q,catalog=CATALOG,catalog_observed_at=END)
        counted={g['key']['wi']:g['usage']['event_count'] for g in out['groups'] if g['usage']['event_count']}
        self.assertEqual(counted,{WI:1})
        self.assertEqual(out['summary']['usage']['no_wi_event_count'],0)

if __name__=='__main__':unittest.main()
