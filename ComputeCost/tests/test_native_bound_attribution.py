"""Actual native snapshot builders -> canonical binding -> receipt query."""
from __future__ import annotations
from hashlib import sha256
import json
import os
from pathlib import Path
import tempfile
import unittest
import yaml
from pulse_compute_cost.pi_usage import PiUsageBinding, build_pi_session_snapshot
from pulse_compute_cost.ledger import UsageLedger
from pulse_compute_cost.receipt_sources import REGISTRY_SCHEMA, collect_registered_sources
from pulse_compute_cost.collaboration_query import CollaborationQuery, query_collaboration
from pulse_compute_cost.source_registration import runtime_candidate_registry

WI="wi_20260914_123"; AT="2026-09-14T10:00:00Z"; LATER="2026-09-14T11:00:00Z"

class NativeBoundAttributionTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.root=Path(self.tmp.name)
        self.native=self.root/'sessions'/'tenant';self.native.mkdir(parents=True)
        self.file=self.native/'session.jsonl';self.session='native-session'
        self.file.write_text(json.dumps({'type':'session','id':self.session,'version':3,'timestamp':AT})+'\n');self.file.chmod(0o600)
        self.authority='session_v1_'+sha256(('pulse-session-v1\0'+self.session).encode()).hexdigest()
        self.binding=PiUsageBinding(WI,'work_item',1,'chatgpt_subscription', 'a'*64, 'pulse', 'surface_v1_local_macos', self.authority)
        self.baseline=build_pi_session_snapshot(self.file,self.binding,generated_at=AT,measurement_role='baseline')
        self.harness=self.root/'harness';(self.harness/'executions').mkdir(parents=True)
        self.ledger=UsageLedger(self.root/'ledger')
        self.registry={'schema':REGISTRY_SCHEMA,'ledger_root':str(self.ledger.root),'sources':[
            {'id':'native','kind':'pi','path':str(self.native.parent),'layout':'pi_session_root.v1'},
            {'id':'authority','kind':'harness_root','path':str(self.harness)}]}
    def append(self,identity='m'):
        row={'type':'message','id':identity,'timestamp':LATER,'message':{'role':'assistant','model':'openai/test','provider':'openai',
             'usage':{'input':100,'cacheRead':20,'cacheWrite':0,'output':10,'totalTokens':130}}}
        with self.file.open('a') as out:out.write(json.dumps(row)+'\n')
    def bind(self):
        record={'schema_version':'work_item_execution_binding/v1','work_item_id':WI,'agent_id':'pulse','epoch':1,
                'phase':'work_item','runtime':'pi','session_ref':self.authority,'lifecycle_state':'active','created_at':AT,'updated_at':AT}
        (self.harness/'executions'/f'{WI}.yaml').write_text(yaml.safe_dump(record))
        folder=self.native/'compute-cost';folder.mkdir(exist_ok=True)
        final=build_pi_session_snapshot(self.file,self.binding,generated_at='2026-09-14T11:01:00Z',measurement_role='progress',baseline_snapshot=self.baseline)
        for role,value in [('baseline',self.baseline),('open',final)]:
            p=folder/f"{WI}-{'a'*64}-{role}.json";p.write_text(json.dumps(value));p.chmod(0o600)
    def query(self,**kw):
        return query_collaboration(self.ledger,CollaborationQuery(from_date='2026-09-14',to_date='2026-09-15',scope='activity',**kw),
            catalog={'data':[{'id':'openai/test','pricing':{'prompt':'1','completion':'1','input_cache_read':'0.1','input_cache_write':'2'}}]},catalog_observed_at=LATER)
    def test_late_binding_assigns_exact_requests_without_backdating_old_watermark(self):
        self.append();collect_registered_sources(self.registry,self.ledger)
        old=self.ledger.receipt_snapshot()['watermark']
        self.assertEqual(self.query()['summary']['usage']['unassigned_event_count'],1)
        self.bind();collect_registered_sources(self.registry,self.ledger)
        out=self.query(work_items=(WI,),group_by=('agent',))
        self.assertEqual(out['summary']['usage']['known_total_tokens'],130)
        self.assertEqual(out['summary']['cost']['partial_machine_cash_usd'],'112')
        self.assertEqual(out['groups'][0]['key'],{'agent':'pulse'})
        self.assertEqual(self.query(watermark=old)['summary']['usage']['unassigned_event_count'],1)
        self.assertEqual(collect_registered_sources(self.registry,self.ledger)['inserted'],0)
    def test_orphan_open_snapshot_without_baseline_affects_only_itself(self):
        self.append();self.bind()
        # An abandoned capture: an open snapshot of another binding, no baseline or lock.
        other=PiUsageBinding(WI,'work_item',1,'chatgpt_subscription','b'*64,'pulse','surface_v1_local_macos',self.authority)
        base=build_pi_session_snapshot(self.file,other,generated_at=AT,measurement_role='baseline')
        orphan=build_pi_session_snapshot(self.file,other,generated_at='2026-09-14T11:01:00Z',measurement_role='progress',baseline_snapshot=base)
        p=self.native/'compute-cost'/f"{WI}-{'b'*64}-open.json";p.write_text(json.dumps(orphan));p.chmod(0o600)
        result=collect_registered_sources(self.registry,self.ledger)
        native=next(s for s in result['sources'] if s['kind']=='pi')
        self.assertEqual(native['diagnostics'].get('pi_snapshot_baseline_missing'),1)
        self.assertNotIn('native_attribution_boundary_invalid',native['diagnostics'])
        self.assertNotEqual(native['reason'],'pi_snapshot_baseline_missing')
        self.assertEqual(self.query(work_items=(WI,))['summary']['usage']['known_total_tokens'],130)
    def test_new_native_request_waits_for_matching_progress_boundary(self):
        self.append();self.bind();collect_registered_sources(self.registry,self.ledger)
        self.append('m2');collect_registered_sources(self.registry,self.ledger)
        self.assertEqual(self.query(work_items=(WI,))['summary']['usage']['event_count'],1)
        self.bind();collect_registered_sources(self.registry,self.ledger)
        self.assertEqual(self.query(work_items=(WI,))['summary']['usage']['event_count'],2)
        self.assertEqual(collect_registered_sources(self.registry,self.ledger)['inserted'],0)
    def test_source_rewrite_or_wrong_canonical_session_never_grants_attribution(self):
        self.append();self.bind()
        p=self.harness/'executions'/f'{WI}.yaml';raw=yaml.safe_load(p.read_text());raw['session_ref']='session_v1_'+'b'*64;p.write_text(yaml.safe_dump(raw))
        collect_registered_sources(self.registry,self.ledger)
        self.assertEqual(self.query()['summary']['usage']['unassigned_event_count'],1)
        self.assertEqual(self.query(work_items=(WI,))['summary']['usage']['event_count'],0)
    def test_harness_surface_class_joins_only_matching_native_surface(self):
        self.append();self.bind()
        path=self.harness/'executions'/f'{WI}.yaml'
        raw=yaml.safe_load(path.read_text());raw['runtime']='resident_harness@surface_v1_local_macos'
        path.write_text(yaml.safe_dump(raw))
        collect_registered_sources(self.registry,self.ledger)
        self.assertEqual(self.query(work_items=(WI,))['summary']['usage']['event_count'],1)

    def test_wrong_harness_surface_does_not_grant_native_attribution(self):
        self.append();self.bind()
        path=self.harness/'executions'/f'{WI}.yaml'
        raw=yaml.safe_load(path.read_text());raw['runtime']='resident_harness@surface_v1_cloud_linux_b'
        path.write_text(yaml.safe_dump(raw))
        collect_registered_sources(self.registry,self.ledger)
        self.assertEqual(self.query(work_items=(WI,))['summary']['usage']['event_count'],0)

    def test_same_source_resolves_host_authority_once_but_next_collection_rereads(self):
        from unittest.mock import patch
        from pulse_compute_cost import receipt_sources as source
        self.append(); self.bind(); self.add_host()
        for n in range(3):
            other = self.native / ('idle-' + str(n) + '.jsonl')
            other.write_text(json.dumps({'type':'session','id':'idle-'+str(n)})+'\n')
        with patch.object(source, '_host_authorities', wraps=source._host_authorities) as resolver:
            collect_registered_sources(self.registry, self.ledger)
            self.assertEqual(resolver.call_count, 1)
            first = self.query(work_items=(WI,))
            collect_registered_sources(self.registry, self.ledger)
            self.assertEqual(resolver.call_count, 2)
            self.assertEqual(first['summary']['usage']['tokens'], self.query(work_items=(WI,))['summary']['usage']['tokens'])

    def test_supplied_empty_authority_is_not_recomputed(self):
        from unittest.mock import patch
        from pulse_compute_cost.native_attribution import bind_pi_boundaries
        from pulse_compute_cost.receipt_sources import read_pi
        self.append(); self.bind()
        with patch('pulse_compute_cost.native_attribution._host_authorities', side_effect=AssertionError('duplicate scan')):
            result = bind_pi_boundaries(self.file, self.file.read_bytes(), read_pi(self.file, LATER), {}, LATER,
                                        host_authorities={})
        self.assertEqual(result, [])

    def add_host(self, assignment="a"*64, amount=100):
        from pulse_compute_cost.receipts import Receipt, reference
        from pulse_compute_cost.receipt_sources import usage_receipt
        common={'wi':WI,'executor':'pulse','responsible':None,'delegator':None}
        ex=Receipt.make('execution','host-execution',dict(common,runtime='pi',assignment_ref=reference('assignment',assignment),
            parent_ref=None,state='completed',expected_usage=True),occurred_at=AT,observed_at=LATER,producer='relay_host.v1')
        usage=usage_receipt('pi','host',{'input_tokens':amount,'output_tokens':10,'cache_read_input_tokens':20,'cache_creation_input_tokens':0},
            at=LATER,observed=LATER,model='openai/test',execution_ref=ex.entity_ref,common=common,
            granularity='turn_aggregate',mapping='relay_pi.v1',interval=('2026-09-14T10:59:00Z','2026-09-14T11:00:10Z'),
            native_scope_ref=reference('native_session',['pi',self.session]))
        self.ledger.ingest_receipts([ex,usage])
    def test_native_bound_prefix_accounts_for_host_summary_once(self):
        self.append();self.bind();self.add_host()
        collect_registered_sources(self.registry,self.ledger)
        out=self.query(work_items=(WI,))
        self.assertEqual(out['summary']['usage']['known_total_tokens'],130)
        self.assertEqual(out['summary']['cost']['partial_machine_cash_usd'],'112')
        self.assertEqual(len(out['reconciliation']['native_boundary_supersession']),1)
        self.assertNotIn('join_conflict',out['summary']['cost']['reasons'])
        self.assertNotIn('execution_link_unknown',out['summary']['cost']['reasons'])
    def test_native_compaction_quantity_is_reconciled_without_inventing_model(self):
        self.append();self.bind();self.add_host()
        # This observed compaction is part of the same actual Host counters.
        with self.file.open('a') as out:
            out.write(json.dumps({'type':'compaction','id':'compact','timestamp':LATER,
                'usage':{'input':7,'output':3,'cacheRead':0,'cacheWrite':0,'totalTokens':10}})+'\n')
        from pulse_compute_cost.receipts import Receipt,current_receipts
        active,_=current_receipts(self.ledger.receipt_snapshot()['facts'])
        host=next(r for r in active if r.kind=='usage')
        p=host.payload;p['tokens']['uncached_input']+=7;p['tokens']['output']+=3
        p['native_tokens']['input_tokens']+=7;p['native_tokens']['output_tokens']+=3;p['model']=None
        self.ledger.ingest_receipts([Receipt.make('usage',None,p,occurred_at=host.occurred_at,
            observed_at=LATER,producer=host.producer,entity_ref=host.entity_ref,supersedes=host.fact_ref)])
        collect_registered_sources(self.registry,self.ledger)
        out=self.query()
        self.assertEqual(out['summary']['usage']['known_total_tokens'],140)
        self.assertNotIn('join_conflict',out['summary']['cost']['reasons'])
        self.assertEqual(out['summary']['cost']['partial_machine_cash_usd'],'112')
        self.assertEqual(len(out['summary']['cost']['unpriced']),1)
        self.assertEqual(len(out['reconciliation']['native_boundary_supersession']),1)

    def test_counter_excess_is_not_time_based_deduplication(self):
        self.append();self.bind();self.add_host(amount=999)
        collect_registered_sources(self.registry,self.ledger)
        out=self.query(work_items=(WI,))
        self.assertEqual(out['reconciliation']['native_boundary_supersession'],{})
        self.assertIn('join_conflict',out['summary']['cost']['reasons'])
        self.assertIsNone(out['summary']['cost']['partial_machine_cash_usd'])

    def test_frozen_host_assignment_joins_live_native_without_legacy_yaml_copy(self):
        self.append();self.bind();self.add_host()
        (self.harness/'executions'/f'{WI}.yaml').unlink()
        collect_registered_sources(self.registry,self.ledger)
        out=self.query(work_items=(WI,))
        self.assertEqual(out['summary']['usage']['known_total_tokens'],130)
        self.assertEqual(out['summary']['usage']['unassigned_event_count'],0)
        self.assertEqual(out['summary']['cost']['partial_machine_cash_usd'],'112')
        self.assertEqual(len(out['reconciliation']['native_boundary_supersession']),1)
        self.assertEqual(collect_registered_sources(self.registry,self.ledger)['inserted'],0)
    def test_host_quantity_exceeding_native_boundary_is_not_suppressed(self):
        self.append();self.bind();self.add_host(amount=999)
        collect_registered_sources(self.registry,self.ledger)
        out=self.query(work_items=(WI,))
        self.assertEqual(out['reconciliation']['native_boundary_supersession'],{})
        self.assertIn('join_conflict',out['summary']['cost']['reasons'])
        self.assertIsNone(out['summary']['cost']['partial_machine_cash_usd'])

    def test_host_result_published_after_snapshot_requires_exact_native_counter_proof(self):
        self.append();self.bind();self.add_host()
        from pulse_compute_cost.receipts import Receipt
        rows=self.ledger.receipt_snapshot()['facts'];host=next(r for r in rows if r.kind=='usage')
        payload=host.payload;payload['interval_end']='2026-09-14T11:02:00Z'
        successor=Receipt.make('usage',None,payload,occurred_at='2026-09-14T11:02:00Z',observed_at='2026-09-14T11:02:00Z',
            producer=host.producer,entity_ref=host.entity_ref,supersedes=host.fact_ref)
        self.ledger.ingest_receipts([successor])
        collect_registered_sources(self.registry,self.ledger)
        out=self.query(work_items=(WI,))
        self.assertEqual(out['summary']['cost']['partial_machine_cash_usd'],'112')
        self.assertEqual(out['summary']['usage']['known_total_tokens'],130)
        self.assertEqual(collect_registered_sources(self.registry,self.ledger)['inserted'],0)

    def test_conflicting_assignment_is_not_hidden_by_matching_native_tokens(self):
        self.append();self.bind();self.add_host(assignment='b'*64)
        collect_registered_sources(self.registry,self.ledger)
        out=self.query(work_items=(WI,))
        self.assertEqual(out['reconciliation']['native_boundary_supersession'],{})
        self.assertIn('join_conflict',out['summary']['cost']['reasons'])
    def test_unbound_session_can_reconcile_quantity_without_inventing_wi(self):
        self.append()
        from pulse_compute_cost.receipt_sources import usage_receipt
        from pulse_compute_cost.receipts import reference
        host=usage_receipt('pi','unbound-host',{'input_tokens':100,'output_tokens':10,'cache_read_input_tokens':20,'cache_creation_input_tokens':0},
            at=LATER,observed=LATER,model=None,granularity='turn_aggregate',mapping='relay_pi.v1',
            interval=(AT,'2026-09-14T11:02:00Z'),native_scope_ref=reference('native_session',['pi',self.session]))
        self.ledger.ingest_receipts([host]);collect_registered_sources(self.registry,self.ledger)
        out=self.query();self.assertEqual(out['summary']['usage']['known_total_tokens'],130)
        self.assertEqual(out['summary']['usage']['unassigned_event_count'],1)
        self.assertEqual(out['summary']['cost']['partial_machine_cash_usd'],'112')
    def test_equal_counter_outside_dispatch_interval_does_not_reconcile(self):
        self.append()
        from pulse_compute_cost.receipt_sources import usage_receipt
        from pulse_compute_cost.receipts import reference
        host=usage_receipt('pi','outside-host',{'input_tokens':100,'output_tokens':10,'cache_read_input_tokens':20,'cache_creation_input_tokens':0},
            at=LATER,observed=LATER,model=None,granularity='turn_aggregate',mapping='relay_pi.v1',
            interval=('2026-09-14T12:00:00Z','2026-09-14T12:02:00Z'),native_scope_ref=reference('native_session',['pi',self.session]))
        self.ledger.ingest_receipts([host]);collect_registered_sources(self.registry,self.ledger)
        out=self.query();self.assertEqual(out['reconciliation']['native_boundary_supersession'],{})
        self.assertIn('join_conflict',out['summary']['cost']['reasons'])


    def test_runtime_registration_records_explicit_relay_principal_history_without_rewriting_prior_boundary(self):
        host=self.root/'host';host.mkdir()
        manifest=self.root/'manifest.json'
        manifest.write_text(json.dumps({'profiles':{'resident':{
            'sessionRoot':str(self.native.parent),
            'computeCostHost':{'sclHarnessRoot':str(self.harness)}}}}))
        config=self.root/'relay.json'
        config.write_text(json.dumps({'users':[{'userId':'u','agents':[{
            'id':'tcloud-pulse','harnessAgentId':'pulse'}]}]}))
        config.chmod(0o600)
        first=1_790_000_000
        os.utime(config,(first,first))
        registry={**self.registry,'sources':self.registry['sources']+[
            {'id':'host','kind':'relay_host','path':str(host)}]}
        candidate=runtime_candidate_registry(registry,manifest,harness_root=self.harness,
            receipt_root=host,relay_config=config)
        source=next(s for s in candidate['sources'] if s['kind']=='relay_host')
        self.assertEqual([(r['alias'],r['principal']) for r in source['principal_alias_history']],
                         [('tcloud-pulse','pulse')])
        self.assertEqual(runtime_candidate_registry(candidate,manifest,harness_root=self.harness,
            receipt_root=host,relay_config=config),candidate)

        config.write_text(json.dumps({'users':[{'userId':'u','agents':[{
            'id':'tcloud-pulse'}]}]}))
        config.chmod(0o600); os.utime(config,(first+60,first+60))
        changed=runtime_candidate_registry(candidate,manifest,harness_root=self.harness,
            receipt_root=host,relay_config=config)
        source=next(s for s in changed['sources'] if s['kind']=='relay_host')
        self.assertEqual([(r['alias'],r['principal']) for r in source['principal_alias_history']],
                         [('tcloud-pulse','pulse'),('tcloud-pulse','tcloud-pulse')])

    def test_manifest_registration_follows_root_changes_and_preserves_history(self):
        host=self.root/'host';host.mkdir();manifest=self.root/'manifest.json'
        def configure(root):manifest.write_text(json.dumps({'profiles':{'resident':{'sessionRoot':str(root),
            'computeCostHost':{'sclHarnessRoot':str(self.harness)}}}}))
        configure(self.native.parent)
        registry={**self.registry,'sources':self.registry['sources']+[{'id':'host','kind':'relay_host','path':str(host)}]}
        candidate=runtime_candidate_registry(registry,manifest,harness_root=self.harness,receipt_root=host)
        self.assertEqual(runtime_candidate_registry(candidate,manifest,harness_root=self.harness,receipt_root=host),candidate)
        new=self.root/'new-sessions';new.mkdir();configure(new)
        from pulse_compute_cost.source_layout import manifest_paths
        source=next(s for s in candidate['sources'] if s['kind']=='pi')
        self.assertEqual(manifest_paths(source),[new])

if __name__=='__main__':unittest.main()
