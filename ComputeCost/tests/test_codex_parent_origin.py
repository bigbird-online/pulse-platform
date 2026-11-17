import copy
import unittest
import test_codex_startup_actor as fixture
END=fixture.END
from pulse_compute_cost.receipt_sources import read_codex
from pulse_compute_cost.codex_origin import codex_entrypoint_origins

class CodexParentOriginTests(unittest.TestCase):
    setUp=fixture.CodexStartupActorTests.setUp
    records=fixture.CodexStartupActorTests.records
    write=fixture.CodexStartupActorTests.write
    def family(self,parent='parent'):
        root=self.records();root[0]['payload']['id']='parent'
        self.write(root,filename='parent.jsonl')
        child=copy.deepcopy(root);child[0]['payload'].update(id='child',session_id='parent',parent_thread_id=parent,
            source={'subagent':{'thread_spawn':{'parent_thread_id':parent}}})
        for row in child:row['timestamp']=row['timestamp'].replace('01:00','01:02').replace('01:01','01:03')
        child[1]['payload']['turn_id']='child-turn'
        child[3]['payload']['info']['total_token_usage']['output_tokens']=11
        return self.write(child,filename='child.jsonl'),child
    def child_actor(self,path):
        rows=read_codex(path.parent,END,source_identity=self.source)
        target=next(r for r in rows if r.kind=='usage' and r.payload['tokens']['output']==11)
        return next(r for r in rows if r.kind=='attribution' and r.payload['target_ref']==target.entity_ref)
    def test_real_parent_trigger_reaches_child_without_wi_or_quantity_inheritance(self):
        path,_=self.family();plain=read_codex(path.parent,END); enriched=read_codex(path.parent,END,source_identity=self.source)
        self.assertEqual([r.to_dict() for r in plain],[r.to_dict() for r in enriched if r.kind=='usage'])
        actor=self.child_actor(path);self.assertEqual(actor.payload['context']['entrypoint'],'codex_app')
        self.assertIsNone(actor.payload['wi']);self.assertGreater(len(actor.evidence),1)
        self.assertNotIn('/registered',str(actor.to_dict()))
    def test_missing_parent_remains_unknown(self):
        path,_=self.family('not-present');self.assertIsNone(self.child_actor(path).payload['context']['entrypoint'])
    def test_disagreeing_native_parent_fields_never_inherit(self):
        path,child=self.family();child[0]['payload']['parent_thread_id']='foreign';self.write(child,filename=path.name)
        self.assertIsNone(self.child_actor(path).payload['context']['entrypoint'])
    def test_later_conflicting_parent_metadata_never_selects_last(self):
        path,child=self.family();other=copy.deepcopy(child[0]);other['payload']['source']['subagent']['thread_spawn']['parent_thread_id']='foreign'
        other['payload']['parent_thread_id']='foreign';child.insert(1,other);self.write(child,filename=path.name)
        self.assertIsNone(self.child_actor(path).payload['context']['entrypoint'])
    def test_origin_does_not_supply_an_unregistered_actor(self):
        path,child=self.family();child[0]['payload']['cwd']='/other/agent';self.write(child,filename=path.name)
        actor=self.child_actor(path);self.assertIsNone(actor.payload['executor']);self.assertEqual(actor.payload['context']['entrypoint'],'codex_app')

    def test_paginated_parent_startup_is_recovered_before_child_origin(self):
        import test_codex_paginated_actor as paginated
        import json
        case=paginated.CodexPaginatedActorTests();case.setUp();self.addCleanup(case.doCleanups)
        child=self.records();sid='00000000-0000-4000-8000-000000000003'
        child[0]['payload'].update(id=sid,session_id=paginated.THREAD,parent_thread_id=paginated.THREAD,
            source={'subagent':{'thread_spawn':{'parent_thread_id':paginated.THREAD}}})
        for row in child:row['timestamp']=row['timestamp'].replace('01:00','01:02').replace('01:01','01:03')
        child[1]['payload']['turn_id']='child-turn';child[3]['payload']['info']['total_token_usage']['output_tokens']=11
        path=case.logs/('rollout-2026-09-15T01-02-00-'+sid+'.jsonl')
        path.write_text(''.join(json.dumps(r)+'\n' for r in child))
        result=read_codex(case.logs,END,source_identity=case.source)
        usage=next(r for r in result if r.kind=='usage' and r.payload['tokens']['output']==11)
        actor=next(r for r in result if r.kind=='attribution' and r.payload['target_ref']==usage.entity_ref)
        self.assertEqual(actor.payload['context']['entrypoint'],'codex_app');self.assertIsNone(actor.payload['wi'])

    def test_consistent_physical_startup_proofs_do_not_conflict(self):
        from types import SimpleNamespace
        from pulse_compute_cost.receipts import reference
        def obs(sid,source,parent,proof):
            return SimpleNamespace(session_id=sid,originator='Codex Desktop',native_source=source,
                parent_session_id=parent,startup_ref=reference('startup',proof))
        first=obs('parent','vscode',None,'physical-first');second=obs('parent','vscode',None,'physical-second')
        child=obs('child',None,'parent','child')
        result=codex_entrypoint_origins([first,second,child])
        self.assertEqual(result['child'][0],'codex_app')
        self.assertTrue({first.startup_ref,second.startup_ref,child.startup_ref}<=set(result['child'][1]))
        second.native_source='cli'
        self.assertNotIn('child',codex_entrypoint_origins([first,second,child]))

    def test_desktop_exec_origin_reaches_verified_child(self):
        path, child = self.family()
        root = self.records(); root[0]['payload'].update(id='parent', source='exec')
        self.write(root, filename='parent.jsonl')
        actor = self.child_actor(path)
        self.assertEqual(actor.payload['context']['entrypoint'], 'codex_app')
        parent_rows = read_codex(path.parent, END, source_identity=self.source)
        parent_usage = next(r for r in parent_rows if r.kind == 'usage' and r.payload['tokens']['output'] != 11)
        parent_actor = next(r for r in parent_rows if r.kind == 'attribution' and r.payload['target_ref'] == parent_usage.entity_ref)
        self.assertEqual(parent_actor.payload['context']['entrypoint'], 'codex_app')
        self.assertIsNone(actor.payload['wi'])

    def test_exact_native_software_origin_is_shared_and_never_any_exec(self):
        from pulse_compute_cost.codex_origin import native_codex_entrypoint
        cases = [(('Codex Desktop','vscode'),'codex_app'), (('Codex Desktop','exec'),'codex_app'),
                 (('codex_exec','exec'),'cli'), (('other','cli'),'cli'),
                 (('other','exec'),None), (('Codex Desktop',None),None)]
        for identity, expected in cases:
            with self.subTest(identity=identity):
                self.assertEqual(native_codex_entrypoint(*identity), expected)
