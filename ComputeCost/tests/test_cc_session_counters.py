"""Real counter shapes, isolated ledgers; no runtime/provider or production writes."""
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from copy import deepcopy
import json
import tempfile
import unittest

from pulse_compute_cost.receipts import reference, canonical, TOKEN_FIELDS, digest
from pulse_compute_cost.receipt_sources import usage_receipt, collect_registered_sources
from pulse_compute_cost.collaboration_query import query_collaboration, CollaborationQuery
from pulse_compute_cost.ledger import UsageLedger

WI = 'wi_20261001_297'
SCOPE = reference('native_session', ['cc', 'native-test'])
AT = '2026-10-01T01:00:00Z'
AT2 = '2026-10-02T01:00:00Z'
CATALOG = {'data': [{'id':'openai/test','pricing': {'prompt':'1','completion':'1',
    'input_cache_read':'0.1','input_cache_write':'1'}}]}

def row(identity, n, output, at, *, counter=False, wi=WI, scope=SCOPE):
    r=usage_receipt('cc', identity, {'input_tokens':n,'output_tokens':output,
        'cache_read_input_tokens':0,'cache_creation_input_tokens':0},
        at=at, observed=at, model='openai/test', final=True,
        mapping='cc_sdk_result.v1' if counter else 'cc_message.v1',
        granularity='turn_aggregate' if counter else 'request',
        common={'wi':wi,'executor':'pulse','responsible':None,'delegator':None},
        native_scope_ref=scope, execution_ref=reference('execution',identity) if counter else None)
    if not counter:r=replace(r,evidence=(reference('cc_persisted_session_counter.v1',scope),))
    return r

def fixture():
    return [row('native1',10,5,AT),row('counter1',10,5,'2026-10-01T01:01:00Z',counter=True),
            row('child',6,2,AT2),row('counter2',16,10,'2026-10-02T01:01:00Z',counter=True)]

class CounterTests(unittest.TestCase):
    def test_native_counter_evidence_is_a_valid_receipt_evidence_set(self):
        from pulse_compute_cost.cc_session_counters import native_counter_evidence
        from pulse_compute_cost.receipts import Receipt
        for session in ('native-test', 'other', 'third', 'fourth'):
            scope = reference('native_session', ['cc', session])
            evidence = native_counter_evidence(scope, '2.1.300')
            self.assertEqual(list(evidence), sorted(set(evidence)))
            native = replace(row('m-'+session, 1, 1, AT, scope=scope), evidence=evidence)
            Receipt.from_dict(native.to_dict())
        self.assertEqual(native_counter_evidence(SCOPE, '2.1.276'), ())

    def query(self, rows):
        class Memory:
            def receipt_snapshot(self,**kw):
                return {'facts':rows,'watermark':1,'recorded_at':AT2,'sha256':digest([r.to_dict() for r in rows])}
        return query_collaboration(Memory(),CollaborationQuery(from_date='2026-09-28',
            to_date='2026-10-05',scope='activity',group_by=('wi',)),catalog=CATALOG,catalog_observed_at=AT2)

    def test_session_not_turn_counter_restores_one_wi_amount(self):
        facts=fixture();before=[r.to_dict() for r in facts];report=self.query(facts)
        self.assertEqual(report['summary']['usage']['known_total_tokens'],26)
        self.assertEqual(report['summary']['usage']['conflicted_event_count'],0)
        self.assertIsNotNone(report['summary']['cost']['partial_machine_cash_usd'])
        self.assertEqual(before,[r.to_dict() for r in facts])
        proof=report['reconciliation']['cc_session_counters'][0]
        self.assertEqual(proof['unitemized_tokens']['output'],3)
        self.assertEqual(proof['native_observation_count'],2)

    def reconcile(self, rows, **kw):
        from pulse_compute_cost.cc_session_counters import reconcile_session_counters
        native={r.entity_ref for r in rows if r.payload['mapping']=='cc_message.v1'}
        return reconcile_session_counters(rows,{SCOPE},native,**kw)

    def test_unitemized_usage_belongs_to_the_interval_between_counters(self):
        # A day view measures each increase since the previous counter, not
        # one remainder stretched back to the last exact match.
        rows=[row('native1',10,5,AT),row('counter1',10,5,'2026-10-01T01:01:00Z',counter=True),
              row('counter2',10,8,'2026-10-02T01:01:00Z',counter=True),
              row('counter3',10,8,'2026-10-03T00:30:00Z',counter=True),
              row('counter4',10,10,'2026-10-03T02:00:00Z',counter=True)]
        kept,residuals,_,proofs=self.reconcile(rows)
        self.assertEqual(sum(sum(r.payload['tokens'].values()) for r in kept+residuals),20)
        self.assertEqual([(r.payload['interval_start'],r.payload['tokens']['output']) for r in residuals],
                         [('2026-10-01T01:01:00Z',3),('2026-10-03T00:30:00Z',2)])
        self.assertEqual(proofs[0]['residual_entity_refs'],[r.entity_ref for r in residuals])
        class Memory:
            def receipt_snapshot(self,**kw):
                return {'facts':rows,'watermark':1,'recorded_at':AT2,'sha256':digest([r.to_dict() for r in rows])}
        day=query_collaboration(Memory(),CollaborationQuery(from_date='2026-10-03',to_date='2026-10-04',
            timezone='UTC',scope='activity',group_by=('wi',)),catalog=CATALOG,catalog_observed_at=AT2)
        self.assertEqual(day['summary']['usage']['known_total_tokens'],2)
        self.assertNotIn('time_unallocated',day['summary']['cost']['reasons'])
        # Detail recorded after its counter: later counters bound the earlier remainder.
        rows=fixture()+[row('late',0,5,'2026-10-02T02:00:00Z'),row('counter3',16,15,'2026-10-02T03:00:00Z',counter=True)]
        rows[3]=row('counter2',16,13,'2026-10-02T01:01:00Z',counter=True)
        _,residuals,_,_=self.reconcile(rows)
        # The remainder is spent no earlier than the session's next activity (the child at AT2).
        self.assertEqual([(r.payload['interval_start'],r.payload['interval_end'],r.payload['tokens']['output']) for r in residuals],
                         [(AT2,'2026-10-02T01:01:00Z',3)])
        # Detail ahead of its counter is caught up by the later counter.
        rows=fixture()+[row('extra',0,9,'2026-10-02T01:00:30Z'),row('counter3',16,22,'2026-10-02T03:00:00Z',counter=True)]
        _,residuals,_,_=self.reconcile(rows)
        self.assertEqual([(r.payload['interval_start'],r.payload['interval_end'],r.payload['tokens']['output']) for r in residuals],
                         [('2026-10-02T01:01:00Z','2026-10-02T03:00:00Z',6)])

    def test_duplicate_sample_is_not_an_additional_charge(self):
        rows=fixture(); rows.append(row('duplicate-last',16,10,'2026-10-02T01:01:00Z',counter=True))
        kept,residuals,_,proofs=self.reconcile(rows)
        self.assertEqual(sum(sum(r.payload['tokens'].values()) for r in kept+residuals),26)
        self.assertEqual(len(proofs),1)

    def test_regression_unknown_fork_baseline_or_cross_wi_refuses(self):
        for mode in ('regression','baseline','wi','missing','unsupported','rejected'):
            with self.subTest(mode=mode):
                rows=fixture();kw={}
                if mode=='regression':rows[-1]=row('counter2',9,10,'2026-10-02T01:01:00Z',counter=True)
                elif mode=='baseline':rows[1]=row('counter1',11,5,'2026-10-01T01:01:00Z',counter=True)
                elif mode=='wi':rows[2]=row('child',6,2,AT2,wi='wi_20261001_001')
                elif mode=='missing':rows[-1]=replace(rows[-1],_payload_json=canonical({**rows[-1].payload,'tokens':dict.fromkeys(TOKEN_FIELDS,None)}))
                elif mode=='unsupported':
                    rows=[replace(r,evidence=()) for r in rows]
                    self.assertFalse(self.query(rows)['reconciliation']['cc_session_counters']);continue
                else:kw['rejected_scopes']={SCOPE}
                kept,residuals,suppressed,proofs=self.reconcile(rows,**kw)
                self.assertEqual(kept,rows);self.assertFalse(residuals or suppressed or proofs)

    def test_after_last_native_usage_is_preserved(self):
        rows=fixture()+[row('ongoing',2,1,'2026-10-03T01:00:00Z')]
        kept,residuals,_,_=self.reconcile(rows)
        self.assertEqual(sum(sum(r.payload['tokens'].values()) for r in kept+residuals),29)

    def test_residual_starts_at_the_sessions_next_activity_without_inventing_a_child(self):
        # No CC activity between the counters before the child's message: an
        # idle gap does not stretch the remainder across the earlier day.
        kept,residuals,_,_=self.reconcile(fixture())
        r=residuals[0];self.assertEqual(r.payload['interval_start'],AT2)
        self.assertEqual(r.payload['interval_end'],'2026-10-02T01:01:00Z')
        self.assertEqual(r.payload['mapping'],'cc_cumulative_residual.v1')
        self.assertEqual(len(kept),2)

    def test_native_engine_support_is_explicit(self):
        from pulse_compute_cost.cc_session_counters import native_counter_evidence
        self.assertFalse(native_counter_evidence(SCOPE,'2.1.276'))
        self.assertFalse(native_counter_evidence(SCOPE,None))
        self.assertTrue(native_counter_evidence(SCOPE,'2.1.285'))

class SourceFanoutTests(unittest.TestCase):
    def test_one_cc_append_does_not_revise_every_old_usage(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);source=root/'source';source.mkdir();path=source/'session.jsonl'
            def native(i):
                return {'type':'assistant','version':'2.1.285','sessionId':'native-test','uuid':str(i),
                    'timestamp':AT,'message':{'id':str(i),'model':'openai/test','stop_reason':'end_turn',
                    'usage':{'input_tokens':10,'output_tokens':2,'cache_read_input_tokens':0,'cache_creation_input_tokens':0}}}
            path.write_text(''.join(json.dumps(native(i))+'\n' for i in range(40)))
            ledger=UsageLedger(root/'ledger')
            registry={'schema':'computecost_source_registry/v1','sources':[{'id':'native','kind':'cc','path':str(source)}]}
            collect_registered_sources(registry,ledger)
            first=ledger.receipt_snapshot();before={r.entity_ref:r.fact_ref for r in first['facts'] if r.kind=='usage'}
            with path.open('a') as f:f.write(json.dumps(native(40))+'\n')
            result=collect_registered_sources(registry,ledger)
            self.assertLessEqual(result['inserted'],3)
            from pulse_compute_cost.receipts import current_receipts
            active,_=current_receipts(ledger.receipt_snapshot()['facts'])
            after={r.entity_ref:r.fact_ref for r in active if r.kind=='usage'}
            self.assertTrue(all(after[k]==v for k,v in before.items()))
            self.assertEqual(collect_registered_sources(registry,ledger)['inserted'],0)

class CounterSafetyTests(CounterTests):
    def test_unresolved_native_final_fork_blocks_residual(self):
        facts=fixture()+[row('child',7,2,AT2)]
        report=self.query(facts)
        self.assertFalse(report['reconciliation']['cc_session_counters'])
        self.assertGreater(report['summary']['usage']['conflicted_event_count'],0)

    def test_counter_observation_order_does_not_change_amount(self):
        forward=self.query(fixture())
        reverse=self.query(list(reversed(fixture())))
        self.assertEqual(forward['summary']['usage']['tokens'],reverse['summary']['usage']['tokens'])
        self.assertEqual(forward['summary']['cost'],reverse['summary']['cost'])

    def test_different_native_session_is_never_joined(self):
        facts=fixture();facts[2]=row('child',6,2,AT2,scope=reference('native_session',['cc','other']))
        kept,residuals,_,proofs=self.reconcile(facts)
        self.assertEqual(len(kept),2)
        # The other child's consumption is separate, never subtracted from this counter.
        self.assertEqual(proofs[0]['native_observation_count'],1)
        self.assertEqual(residuals[0].payload['tokens']['uncached_input'],6)

    def test_actor_fanout_remains_bounded_and_changed_quantity_is_retained(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);source=root/'source';source.mkdir();path=source/'session.jsonl'
            def native(i, count=10):
                return {'type':'assistant','version':'2.1.285','sessionId':'native-test','uuid':str(i),
                    'timestamp':AT,'message':{'id':str(i),'model':'openai/test','stop_reason':'end_turn',
                    'usage':{'input_tokens':count,'output_tokens':2,'cache_read_input_tokens':0,'cache_creation_input_tokens':0}}}
            path.write_text(''.join(json.dumps(native(i))+'\n' for i in range(40)))
            ledger=UsageLedger(root/'ledger')
            registry={'schema':'computecost_source_registry/v1','sources':[{'id':'native','kind':'cc',
                'path':str(source),'actor_attribution':True,'node':'ownermac'}]}
            collect_registered_sources(registry,ledger)
            with path.open('a') as f:f.write(json.dumps(native(40))+'\n')
            self.assertLessEqual(collect_registered_sources(registry,ledger)['inserted'],4)
            self.assertEqual(collect_registered_sources(registry,ledger)['inserted'],0)
            with path.open('a') as f:f.write(json.dumps(native(40,11))+'\n')
            self.assertGreater(collect_registered_sources(registry,ledger)['inserted'],0)
            from pulse_compute_cost.receipts import current_receipts
            _,conflicts=current_receipts(ledger.receipt_snapshot()['facts'])
            self.assertTrue(any(any(r.kind=='usage' for r in versions) for versions in conflicts.values()))

class ResidualFamilyContextTests(CounterTests):
    def test_result_without_own_wi_inherits_unanimous_family(self):
        from pulse_compute_cost.receipt_sources import actor_receipt
        facts=fixture();facts[-1]=row('counter2',16,10,'2026-10-02T01:01:00Z',counter=True,wi=None)
        facts += [actor_receipt(r,node='ownermac',entrypoint='telegram',observed=AT2) for r in list(facts)]
        report=self.query(facts)
        groups=[g for g in report['groups'] if g['usage']['event_count']]
        self.assertEqual(len(groups),1)
        self.assertEqual(groups[0]['key']['wi'],WI)
        self.assertEqual(groups[0]['usage']['known_total_tokens'],26)
        self.assertEqual(groups[0]['usage']['event_count'],3)
        self.assertNotIn('actor_attribution_conflict',groups[0]['cost']['reasons'])

    def test_unproved_family_request_does_not_dispute_the_counters_own_route(self):
        # An itemized request without channel proof is unknown, not contrary.
        from pulse_compute_cost.receipt_sources import actor_receipt
        facts=fixture();facts += [actor_receipt(r,node='ownermac',entrypoint=None if i==2 else 'telegram',observed=AT2)
                                  for i,r in enumerate(list(facts))]
        report=self.query(facts)
        self.assertFalse(report['actor_attribution_conflicts'])
        # Requests of the residual's own interval that prove and do not prove
        # a route leave the unproved counter disputed.
        facts=fixture();facts.insert(3,row('child-2',0,1,'2026-10-02T01:00:30Z'))
        facts += [actor_receipt(r,node='ownermac',entrypoint=None if i in (2,4) else 'telegram',observed=AT2)
                  for i,r in enumerate(list(facts))]
        self.assertTrue(self.query(facts)['actor_attribution_conflicts'])

    def test_an_earlier_intervals_route_is_no_evidence_for_a_later_residual(self):
        # Production 2026-10-26: a session's earlier Telegram requests made the
        # residual of a later, unproved turn "ambiguous". Only the requests
        # between its two counters spent it: unknown, not disputed.
        from pulse_compute_cost.receipt_sources import actor_receipt
        facts=fixture();facts += [actor_receipt(r,node='ownermac',entrypoint=None if i in (2,3) else 'telegram',observed=AT2)
                                  for i,r in enumerate(list(facts))]
        report=self.query(facts)
        self.assertFalse(report['actor_attribution_conflicts'])

    def test_contrary_counter_route_is_not_overridden_by_native_family(self):
        from pulse_compute_cost.receipt_sources import actor_receipt
        facts=fixture();facts += [actor_receipt(r,node='ownermac',entrypoint='feishu' if i==3 else 'telegram',observed=AT2) for i,r in enumerate(list(facts))]
        report=self.query(facts)
        self.assertTrue(report['actor_attribution_conflicts'])
