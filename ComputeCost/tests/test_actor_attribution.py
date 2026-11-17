"""Five-dimensional identity is evidence, not a replacement quantity or a WI.

Only isolated fixtures are used. No production data or account credentials.
"""
from dataclasses import replace
from pathlib import Path
import tempfile
import unittest

from pulse_compute_cost.ledger import UsageLedger
from pulse_compute_cost.receipts import Receipt, ReceiptError, reference
from pulse_compute_cost.receipt_sources import usage_receipt, actor_receipt, workspace_actor
from pulse_compute_cost.collaboration_query import CollaborationQuery, query_collaboration
from pulse_compute_cost.adapters import CodexNativeObservation
from pulse_compute_cost.source_registration import actor_candidate_registry

AT = '2026-09-15T01:00:00Z'
END = '2026-09-15T01:01:00Z'
WI = 'wi_20260915_001'
CATALOG = {'data': [{'id': 'openai/test', 'pricing': {
    'prompt': '1', 'completion': '1', 'input_cache_read': '0.1', 'input_cache_write': '2'}}]}


class ActorAttributionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.ledger = UsageLedger(Path(self.tmp.name) / 'ledger')
        self.usage = usage_receipt('codex', 'request', {
            'input_tokens': 120, 'cached_input_tokens': 20,
            'cache_write_input_tokens': 0, 'output_tokens': 10},
            at=END, observed=END, model='openai/test', mapping='codex_cli.v1',
            granularity='turn_aggregate', interval=(AT, END))

    def query(self, **kwargs):
        return query_collaboration(self.ledger, CollaborationQuery(
            from_date='2026-09-15', to_date='2026-09-16', scope='activity', **kwargs),
            catalog=CATALOG, catalog_observed_at=END)

    def actor(self, executor='pulse', node='ownermac', entrypoint='codex_app'):
        return actor_receipt(self.usage, node=node, executor=executor, entrypoint=entrypoint,
                             workspace_ref=reference('workspace', 'registered'), observed=END)

    def test_agent_known_with_unknown_wi_is_not_agent_unassigned(self):
        self.ledger.ingest_receipts([self.usage, self.actor()])
        result = self.query(group_by=('agent',))
        self.assertEqual(result['groups'][0]['key'], {'agent': 'pulse'})
        self.assertEqual(result['groups'][0]['cost']['partial_machine_cash_usd'], '112')
        usage = result['summary']['usage']
        self.assertEqual(usage['agent_unassigned_event_count'], 0)
        self.assertEqual(usage['unassigned_event_count'], 1)  # legacy key still means WI
        self.assertEqual(usage['wi_unassigned_event_count'], 1)
        self.assertEqual(usage['wi_unassigned_priced_usd'], '112')
        self.assertEqual(self.query(group_by=('wi',))['groups'][0]['key'], {'wi': 'unassigned'})
        self.assertEqual(self.query(agent='pulse')['summary']['usage']['event_count'], 1)

    def test_all_five_dimensions_share_one_quantity(self):
        self.ledger.ingest_receipts([self.usage, self.actor()])
        dims = ('agent', 'node', 'runtime', 'model', 'entrypoint')
        result = self.query(group_by=dims)
        self.assertEqual(result['groups'][0]['key'], dict(zip(dims,
            ('pulse', 'ownermac', 'codex', 'openai/test', 'codex_app'))))
        for dimension, value in zip(dims, ('pulse', 'ownermac', 'codex', 'openai/test', 'codex_app')):
            out = self.query(group_by=(dimension,))
            self.assertEqual(out['groups'][0]['key'], {dimension: value})
            self.assertEqual(out['summary']['usage']['known_total_tokens'], 130)
            self.assertEqual(out['summary']['cost']['partial_machine_cash_usd'], '112')
        self.assertEqual(self.query(node='tcloud')['summary']['usage']['event_count'], 0)
        self.assertEqual(self.query(entrypoint='codex_app')['summary']['usage']['event_count'], 1)

    def test_actor_and_task_attributions_coexist_without_clearing_task(self):
        task = Receipt.make('attribution', 'task', {
            'target_ref': self.usage.entity_ref, 'wi': WI, 'executor': 'pulse',
            'responsible': None, 'delegator': None, 'state': 'assigned',
            'policy': 'test_explicit_task.v1', 'effective_at': AT}, observed_at=END,
            occurred_at=AT, producer='test')
        self.ledger.ingest_receipts([self.usage, self.actor(), task])
        result = self.query(work_items=(WI,), group_by=('agent', 'node'))
        self.assertEqual(result['summary']['usage']['event_count'], 1)
        self.assertEqual(result['summary']['usage']['wi_unassigned_event_count'], 0)
        self.assertEqual(result['groups'][0]['key'], {'agent': 'pulse', 'node': 'ownermac'})
        self.assertFalse(result['attribution_conflicts'])

    def test_conflicting_actor_does_not_relabel_as_last_writer(self):
        self.ledger.ingest_receipts([self.usage, self.actor(), self.actor(executor='aki')])
        result = self.query(group_by=('agent',))
        self.assertEqual(result['groups'][0]['key'], {'agent': 'ambiguous'})
        self.assertIn('actor_attribution_conflict', result['summary']['cost']['reasons'])
        self.assertEqual(result['summary']['usage']['agent_unassigned_event_count'], 1)
        self.assertEqual(result['summary']['usage']['known_total_tokens'], 130)

    def test_late_observation_preserves_old_watermark_and_source_receipts(self):
        self.ledger.ingest_receipts([self.usage]); before = self.ledger.receipt_snapshot()
        self.ledger.ingest_receipts([self.actor()])
        self.assertEqual(self.query(watermark=before['watermark'])['summary']['usage']['agent_unassigned_event_count'], 1)
        self.assertEqual(self.query()['summary']['usage']['agent_unassigned_event_count'], 0)
        self.assertEqual(self.ledger.receipt_snapshot(watermark=before['watermark'])['sha256'], before['sha256'])
        self.assertEqual(self.ledger.ingest_receipts([self.actor()])['inserted'], 0)
        self.assertEqual(self.usage.payload['executor'], None)

    def test_source_only_context_preserves_independent_unknowns(self):
        self.ledger.ingest_receipts([self.usage, self.actor(executor=None, entrypoint=None)])
        result = self.query(group_by=('agent', 'node', 'entrypoint'))
        self.assertEqual(result['groups'][0]['key'], {
            'agent': 'unassigned', 'node': 'ownermac', 'entrypoint': 'unassigned'})
        self.assertEqual(result['summary']['usage']['known_total_tokens'], 130)

    def test_actor_receipt_cannot_grant_wi_or_accept_private_raw_context(self):
        actor = self.actor(); payload = actor.payload
        payload['wi'] = WI
        with self.assertRaises(ReceiptError):
            Receipt.make('attribution', 'invalid', payload, occurred_at=AT, observed_at=END, producer='test')
        with self.assertRaises(ReceiptError):
            self.actor(node='/private/raw/path')


class WorkspaceActorTests(unittest.TestCase):
    def source(self):
        return {'node': 'ownermac', 'workspace_actors': [{
            'workspace_root': '/registered/PULSE/local-pulse-space', 'agent': 'pulse',
            'node': 'ownermac', 'runtime': 'codex', 'registry_ref': reference('registry', 'fixed')}]}

    def observation(self, cwd='/registered/PULSE/local-pulse-space', **kwargs):
        return CodexNativeObservation(session_id='fixture', source_path=Path('/isolated/fixture.jsonl'),
            source_position=3, counter={}, counter_fields=frozenset(), previous_counter={},
            previous_fields=frozenset(), startup_cwd=cwd, originator='Codex Desktop',
            native_source='vscode', startup_ref=reference('metadata', 'fixture'), **kwargs)

    def test_exact_registered_startup_workspace_identifies_agent(self):
        actor = workspace_actor(self.source(), self.observation())
        self.assertEqual(actor['executor'], 'pulse')
        self.assertEqual(actor['node'], 'ownermac')
        self.assertEqual(actor['entrypoint'], 'codex_app')
        self.assertNotIn('wi', actor)
        self.assertTrue(actor['workspace_ref'].startswith('ccf1_'))
        self.assertNotIn('/registered', str(actor))

    def test_similar_path_project_cwd_and_other_node_do_not_match(self):
        for cwd in ('/other/local-pulse-space', '/registered/PULSE/local-pulse-space-copy',
                    '/registered/PULSE/local-pulse-space/project'):
            self.assertIsNone(workspace_actor(self.source(), self.observation(cwd))['executor'])
        source = self.source(); source['node'] = 'tcloud'
        self.assertIsNone(workspace_actor(source, self.observation())['executor'])

    def test_conflicting_rules_are_not_last_match_wins(self):
        source = self.source(); source['workspace_actors'].append({**source['workspace_actors'][0], 'agent': 'aki'})
        with self.assertRaises(ReceiptError): workspace_actor(source, self.observation())

    def test_registration_reuses_declared_workspace_and_does_not_change_ledger(self):
        registry = {'schema': 'computecost_source_registry/v1', 'ledger_root': '/unchanged',
                    'sources': [{'id': 'codex', 'kind': 'codex', 'paths': ['/native']},
                                {'id': 'peer', 'kind': 'receipts', 'path': '/transport'}]}
        surfaces = {'agent_id': 'pulse', 'surfaces': [{
            'surface_id': 'surface_v1_local_macos', 'workspace': 'local-pulse-space',
            'deployment': {'site': 'ownermac', 'pulse_home': '/registered/PULSE'}}]}
        result = actor_candidate_registry(registry, surfaces, site='ownermac')
        self.assertEqual(result['ledger_root'], '/unchanged')
        self.assertEqual(result['sources'][0]['workspace_actors'][0]['agent'], 'pulse')
        self.assertEqual(result['sources'][0]['node'], 'ownermac')
        self.assertNotIn('node', result['sources'][1])  # collection location is not execution node
        self.assertNotIn('node', registry['sources'][0])


if __name__ == '__main__': unittest.main()


class PiWorkspaceActorTests(unittest.TestCase):
    def test_registered_pi_workspace_fills_actor_without_guessing_wi_or_entry(self):
        import json
        from pulse_compute_cost.receipt_sources import collect_registered_sources
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp); native=root/'native';native.mkdir()
            cwd=str(root/'PULSE/local-pulse-space')
            rows=[{'type':'session','id':'unrelated-native-id','cwd':cwd,'timestamp':AT},
                  {'type':'message','id':'first','timestamp':END,'parentId':None,
                   'message':{'role':'assistant','model':'openai/test','provider':'openai',
                              'usage':{'input':1734,'output':8,'cacheRead':0,'cacheWrite':0}}}]
            path=native/'session.jsonl';path.write_text(''.join(json.dumps(r)+'\n' for r in rows))
            ledger=UsageLedger(root/'ledger');ledger.root.mkdir(mode=0o700)
            registry={'schema':'computecost_source_registry/v1','ledger_root':str(ledger.root),
                      'sources':[{'id':'pi','kind':'pi','path':str(native),'node':'ownermac','actor_attribution':True}]}
            collect_registered_sources(registry,ledger); old=ledger.receipt_snapshot()
            surfaces={'agent_id':'pulse','surfaces':[{'surface_id':'surface_v1_local_macos','workspace':'local-pulse-space',
                'deployment':{'site':'ownermac','pulse_home':str(root/'PULSE')}}]}
            enrolled=actor_candidate_registry(registry,surfaces,site='ownermac')
            self.assertEqual(enrolled['sources'][0]['workspace_actors'][0]['runtime'],'pi')
            collect_registered_sources(enrolled,ledger)
            q=CollaborationQuery(scope='activity',from_date='2026-09-15',to_date='2026-09-16',group_by=('agent','wi','entrypoint','node','runtime','model'))
            report=query_collaboration(ledger,q,catalog=CATALOG,catalog_observed_at=END)
            group=report['groups'][0]
            self.assertEqual(group['key']['agent'],'pulse')
            self.assertEqual(group['key']['wi'],'unassigned')
            self.assertEqual(group['key']['entrypoint'],'unassigned')
            self.assertEqual(group['usage']['known_total_tokens'],1742)
            self.assertEqual(report['summary']['usage']['agent_unassigned_event_count'],0)
            self.assertEqual(ledger.receipt_snapshot(watermark=old['watermark'])['sha256'],old['sha256'])
            warm=collect_registered_sources(enrolled,ledger)
            self.assertEqual(warm['parser_cache']['parsed_files'],0)
            self.assertEqual(warm['inserted'],0)

    def test_pi_identity_does_not_use_codex_rule_or_similar_workspace(self):
        from pulse_compute_cost.receipt_sources import registered_workspace_actor
        source=WorkspaceActorTests().source();cwd=source['workspace_actors'][0]['workspace_root']
        self.assertIsNone(registered_workspace_actor(source,cwd,'pi')['executor'])
        source['workspace_actors'][0]['runtime']='pi'
        self.assertEqual(registered_workspace_actor(source,cwd,'pi')['executor'],'pulse')
        self.assertIsNone(registered_workspace_actor(source,cwd+'-copy','pi')['executor'])
        source['node']='tcloud'
        self.assertIsNone(registered_workspace_actor(source,cwd,'pi')['executor'])


class CrossSurfaceWorkspaceTests(unittest.TestCase):
    def test_logical_cloud_workspace_does_not_move_a_mac_execution_or_invent_wi(self):
        registry={'schema':'computecost_source_registry/v1','ledger_root':'/unchanged','sources':[{'id':'native','kind':'codex','paths':['/native']}]}
        surfaces={'agent_id':'pulse','surfaces':[
            {'surface_id':'surface_v1_local_macos','workspace':'local-space',
             'deployment':{'site':'ownermac','pulse_home':'/registered/PULSE','kernel_root':'/registered/kernel'}},
            {'surface_id':'surface_v1_cloud_linux_b','workspace':'cloud-space','deployment':{'site':'vultr'}}]}
        source=actor_candidate_registry(registry,surfaces,site='ownermac')['sources'][0]
        observation=WorkspaceActorTests().observation('/registered/PULSE/cloud-space')
        actor=workspace_actor(source,observation)
        self.assertEqual(actor['executor'],'pulse');self.assertEqual(actor['node'],'ownermac')
        self.assertEqual(source['scl_surface_id'],'surface_v1_local_macos')
        self.assertEqual(source['scl_kernel_root'],'/registered/kernel');self.assertNotIn('wi',actor)
        for path in ('/different/PULSE/cloud-space','/registered/PULSE/cloud-space/nested'):
            self.assertIsNone(workspace_actor(source,WorkspaceActorTests().observation(path))['executor'])


class CodexExecOriginTests(unittest.TestCase):
    def test_native_exec_is_cli_without_guessing_its_executor(self):
        from dataclasses import replace

        helper = WorkspaceActorTests()
        observation = replace(
            helper.observation(),
            originator="codex_exec",
            native_source="exec",
            startup_cwd="/unregistered/test",
        )
        actor = workspace_actor(helper.source(), observation)
        self.assertEqual(actor["entrypoint"], "cli")
        self.assertIsNone(actor["executor"])
        unknown = replace(observation, originator="unverified")
        self.assertIsNone(workspace_actor(helper.source(), unknown)["entrypoint"])
