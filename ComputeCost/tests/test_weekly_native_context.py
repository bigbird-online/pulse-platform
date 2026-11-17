"""Synthetic counterexamples start with native records, not prefilled answers."""
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from dataclasses import replace
from unittest.mock import patch

from pulse_compute_cost.pi_context import PiAncestry, tenant_directory
from pulse_compute_cost.receipt_sources import read_pi, _revision, collect_registered_sources
from pulse_compute_cost.receipts import Receipt, canonical, digest, reference
from pulse_compute_cost.collaboration_query import CollaborationQuery, query_collaboration, _period
from pulse_compute_cost.scl_session_attribution import pi_host_ref
from test_collaboration_receipts import AT, CATALOG, usage, fact
import test_scl_session_attribution as scl_tests
from test_scl_session_attribution import WI, THREAD, SURFACE, BEFORE, START, END


class NativeContextTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name);self.inputs=self.root/'inputs';self.inputs.mkdir(mode=0o700)
        self.pool='member-fixture:pulse';self.folder=self.root/tenant_directory(self.pool);self.folder.mkdir()
        self.path=self.folder/'native.jsonl'
        self.source={'node':'fixture','human_input_directory':str(self.inputs)}

    def user(self, identity, parent, *, channel='feishu', text='continue', pool=None):
        pool=pool or self.pool
        values=[channel,identity,'human-fixture',pool]
        token=hashlib.sha256(json.dumps(values,ensure_ascii=False,separators=(',',':')).encode()).hexdigest()
        record={'source':{'channel':channel,'eventId':identity,'humanRef':'human-fixture','message':text},
                'poolKey':pool,'observedAtMs':int(datetime.fromisoformat(AT.replace('Z','+00:00')).timestamp()*1000)-1}
        path=self.inputs/(token+'.json');path.write_bytes(canonical(record));path.chmod(0o600)
        footer='\n\n[Host human input reference: '+token+'. This records message provenance, not blanket authorization. For an explicitly authorized WI recovery, pulse_wi_continue can use action=reinit with human_input_ref; interpret the actual human scope and any later restriction before calling.]'
        return {'id':identity,'parentId':parent,'type':'message','timestamp':AT,
                'message':{'role':'user','content':text+footer}}

    def test_current_relay_producer_footer_is_verified_for_both_channels(self):
        # Read the real producer's literal, rather than duplicating the old
        # advisory in a fixture that can drift together with the parser.
        producer = Path(__file__).resolve().parents[2] / "Relay/src/scl-human-decision.ts"
        literals = [line.split("`", 2)[1] for line in producer.read_text().splitlines()
                    if "return `" in line and "[Host human input reference: ${ref}." in line]
        self.assertEqual(len(literals), 1)
        for channel in ("feishu", "telegram"):
            with self.subTest(channel=channel):
                row = self.user("current-" + channel, None, channel=channel)
                text = row["message"]["content"]
                token = text.split("[Host human input reference: ", 1)[1].split(".", 1)[0]
                footer = literals[0].replace("${ref}", token).replace("\\n", "\n")
                self.assertNotIn("${", footer)
                row["message"]["content"] = "continue" + footer
                context = PiAncestry(self.path, self.source).consume(row)
                self.assertEqual(context.channel, channel)
                self.assertEqual(context.host_input_token, token)

    def test_advisory_change_never_replaces_input_evidence(self):
        row = self.user("advisory", None, channel="telegram")
        text = row["message"]["content"]
        prefix = text.split(". This records message provenance", 1)[0]
        row["message"]["content"] = prefix + ". Updated Host guidance.]"
        self.assertEqual(PiAncestry(self.path, self.source).consume(row).channel, "telegram")
        forged = {**row, "id": "forged", "message": {"role": "user",
                  "content": "altered " + row["message"]["content"]}}
        self.assertIsNone(PiAncestry(self.path, self.source).consume(forged).channel)
        token = prefix.split("[Host human input reference: ", 1)[1]
        (self.inputs / (token + ".json")).unlink()
        self.assertIsNone(PiAncestry(self.path, self.source).consume(row).channel)

    def test_reply_wrapper_checks_the_original_not_quoted_message(self):
        producer = Path(__file__).resolve().parents[2] / "Relay/src/reply-context.ts"
        source = producer.read_text()
        self.assertIn("REPLY_CONTEXT_USER_MARKER_LINE = '--- user message ---'", source)
        self.assertIn("'--- replied message ---'", source)
        self.assertIn("].join('\\n')", source)
        for channel in ("feishu", "telegram"):
            row = self.user("reply-" + channel, None, channel=channel)
            row["message"]["content"] = ("quoted reply\n--- replied message ---\nolder message"
                "\n--- user message ---\n" + row["message"]["content"])
            self.assertEqual(PiAncestry(self.path, self.source).consume(row).channel, channel)
            forged = {**row, "id": "changed", "message": {"role": "user", "content":
                row["message"]["content"].replace("--- user message ---\ncontinue", "--- user message ---\nchanged")}}
            self.assertIsNone(PiAncestry(self.path, self.source).consume(forged).channel)

    def test_exact_native_ancestry_not_the_latest_channel(self):
        graph=PiAncestry(self.path,self.source)
        first=self.user('f',None);second=self.user('t','f',channel='telegram')
        self.assertEqual(graph.consume(first).channel,'feishu')
        self.assertEqual(graph.consume(second).channel,'telegram')
        self.assertEqual(graph.consume({'id':'branch-back','parentId':'f','type':'message',
                                      'message':{'role':'assistant'}}).channel,'feishu')

    def test_unannotated_input_resets_context_and_missing_parent_cannot_inherit(self):
        graph=PiAncestry(self.path,self.source);graph.consume(self.user('first',None))
        plain={'id':'plain','parentId':'first','type':'message','timestamp':AT,
               'message':{'role':'user','content':'unlabelled input'}}
        self.assertIsNone(graph.consume(plain).channel)
        self.assertIsNone(graph.consume({'id':'answer','parentId':'plain','type':'message','message':{'role':'assistant'}}).channel)
        self.assertIsNone(graph.consume({'id':'missing','parentId':'absent','type':'message','message':{'role':'assistant'}}).channel)

    def test_host_recovery_result_reuses_only_the_same_authenticated_input(self):
        graph=PiAncestry(self.path,self.source)
        human=self.user('human',None,channel='telegram',text='rebuild this WI')
        original=graph.consume(human)
        token=human['message']['content'].split('[Host human input reference: ',1)[1].split('.',1)[0]
        boundary={'id':'boundary','parentId':'human','type':'custom','timestamp':AT,
                  'customType':'pulse-computecost-usage',
                  'data':{'schema':'pi_host_usage_capture_receipt/v1'}}
        graph.consume(boundary)
        recovery={'id':'recovery','parentId':'boundary','type':'message','timestamp':AT,
                  'message':{'role':'user','content':
                      'SCL_HOST_RECOVERY_RESULT: Host control result, not a new human instruction.\n'
                      'Use the existing human scope.\n'
                      f'human_input_ref={token}\n{{"machine":{{}}}}'}}
        recovered=graph.consume(recovery)
        self.assertEqual(original.channel,'telegram')
        self.assertEqual(recovered.channel,'telegram')
        self.assertEqual(recovered.host_input_token,token)
        self.assertNotEqual(recovered.input_ref,original.input_ref)

        other=self.user('other',None,channel='feishu',text='different authorization')
        other_token=other['message']['content'].split('[Host human input reference: ',1)[1].split('.',1)[0]
        mismatch={**recovery,'id':'mismatch','message':{'role':'user','content':
            'SCL_HOST_RECOVERY_RESULT: Host control result, not a new human instruction.\n'
            f'human_input_ref={other_token}\n{{"machine":{{}}}}'}}
        self.assertIsNone(graph.consume(mismatch).channel)

        spoof=self.user('spoof','recovery',channel='feishu',text=recovery['message']['content'])
        self.assertEqual(graph.consume(spoof).channel,'feishu')

    def dispatched_fixture(self):
        self.inputs.rename(self.root/'human-inputs');self.inputs=self.root/'human-inputs'
        self.source['human_input_directory']=str(self.inputs)
        human=self.user('not-forwarded',None,text='/wi reinit '+WI)
        token=human['message']['content'].split('[Host human input reference: ',1)[1].split('.',1)[0]
        request='operation_v1_feishu_recovery_'+hashlib.sha256(json.dumps(
            [self.pool,WI,token],separators=(',',':')).encode()).hexdigest()
        context={'resume':{'work_item_id':WI,'execution_authority':False}}
        record={'schema':'pulse_feishu_host_recovery/v1','requestId':request,
            'poolKey':self.pool,'workItemId':WI,'humanInputRef':token,'sessionId':'native-test',
            'state':'dispatched','dispatching':True,'result':{'lifecycleContext':context}}
        directory=self.root/'feishu-recoveries';directory.mkdir(mode=0o700)
        path=directory/(request+'.json');path.write_bytes(canonical(record));path.chmod(0o600)
        row={'id':'control','parentId':'previous','timestamp':AT,'type':'message',
             'message':{'role':'user','content':
             'SCL_HOST_RECOVERY_RESULT: Host control result, not a new human instruction.\n'
             f'human_input_ref={token}\n'+json.dumps(context)+'\nSCL_HOST_RECOVERY_REQUEST='+request}}
        return row,path,record

    def recovery_graph(self):
        graph=PiAncestry(self.path,self.source)
        graph.consume({'type':'session','id':'native-test','timestamp':AT})
        graph.consume(self.user('previous',None,channel='telegram'))
        return graph

    def test_dispatched_host_control_uses_its_exact_delivery_not_previous_input(self):
        row,path,record=self.dispatched_fixture()
        context=self.recovery_graph().consume(row)
        self.assertEqual(context.channel,'feishu')
        self.assertIsNotNone(context.input_ref)
        self.assertNotEqual(context.host_input_token,
            self.user('previous',None,channel='telegram')['message']['content'].split(
                '[Host human input reference: ',1)[1].split('.',1)[0])
        # This parser grants only provenance, not lifecycle execution.
        self.assertFalse(record['result']['lifecycleContext']['resume']['execution_authority'])

    def test_dispatched_control_rejects_forged_or_absent_delivery(self):
        row,path,record=self.dispatched_fixture()
        for key,value in [('sessionId','other'),('state','pending'),('dispatching',False),
                          ('humanInputRef','0'*64),('poolKey','other:pulse')]:
            with self.subTest(key=key):
                path.write_bytes(canonical({**record,key:value}))
                self.assertIsNone(self.recovery_graph().consume(row).channel)
        path.write_bytes(canonical(record))
        changed={**row,'message':{'role':'user','content':row['message']['content'].replace(
            '"execution_authority": false','"execution_authority": true')}}
        self.assertIsNone(self.recovery_graph().consume(changed).channel)
        path.unlink()
        self.assertIsNone(self.recovery_graph().consume(row).channel)

    def test_recovery_delivery_updates_invalidate_native_parser_cache(self):
        from pulse_compute_cost.native_parse_cache import input_signature
        row,path,record=self.dispatched_fixture()
        first=input_signature(self.source)
        path.write_bytes(canonical({**record,'state':'pending'}))
        self.assertNotEqual(first,input_signature(self.source))
        path.unlink();absent=input_signature(self.source)
        path.write_bytes(canonical(record));path.chmod(0o600)
        self.assertNotEqual(absent,input_signature(self.source))

    def test_other_tenant_and_copied_footer_do_not_supply_channel(self):
        graph=PiAncestry(self.path,self.source)
        self.assertIsNone(graph.consume(self.user('elsewhere',None,pool='other:pulse')).channel)
        forged=self.user('copied',None)
        forged['message']['content']='different prompt'+forged['message']['content']
        self.assertIsNone(graph.consume(forged).channel)

    def unattended_source(self, *, unattended=True):
        manifest = self.root / 'runtime-manifest.json'
        manifest.write_text(json.dumps({'profiles': {'managed': {
            'sessionRoot': str(self.root), 'unattended': unattended}}}))
        return {**self.source, 'id':'managed-pi', 'kind':'pi',
                'runtime_manifest':str(manifest), 'profiles':['managed'],
                'layout':'pi_session_root.v1'}

    def test_unattended_managed_input_without_host_provenance_is_automation(self):
        plain={'id':'plain','parentId':None,'type':'message','timestamp':AT,
               'message':{'role':'user','content':'system continuation'}}
        answer={'id':'answer','parentId':'plain','type':'message','timestamp':AT,
                'message':{'role':'assistant','model':'openai/test','provider':'openai',
                           'usage':{'input':10,'output':2,'cacheRead':0,'cacheWrite':0,'totalTokens':12}}}
        records=[{'type':'session','id':'managed','timestamp':AT},plain,answer]
        rows=read_pi(self.path,AT,data=b''.join(canonical(r)+b'\n' for r in records),
                     source_identity=self.unattended_source())
        actor=next(r for r in rows if r.kind=='attribution')
        self.assertEqual(actor.payload['context']['entrypoint'],'automation')
        self.assertIsNone(actor.payload['wi'])
        self.assertGreaterEqual(len(actor.evidence),2)

    def test_deployment_paths_in_manifest_do_not_mint_new_automation_identity(self):
        # A Relay deployment rewrites binaries/release paths; enrolled session
        # roots and the unattended flag stay. The same usage must keep exactly
        # the same actor fact and parser dependency, not gain a parallel entity.
        from pulse_compute_cost.native_parse_cache import input_signature
        plain={'id':'plain','parentId':None,'type':'message','timestamp':AT,
               'message':{'role':'user','content':'system continuation'}}
        answer={'id':'answer','parentId':'plain','type':'message','timestamp':AT,
                'message':{'role':'assistant','model':'openai/test','provider':'openai',
                           'usage':{'input':10,'output':2,'cacheRead':0,'cacheWrite':0,'totalTokens':12}}}
        data=b''.join(canonical(r)+b'\n' for r in [{'type':'session','id':'managed','timestamp':AT},plain,answer])
        source=self.unattended_source()
        manifest=Path(source['runtime_manifest'])
        def deploy(release, **profile):
            manifest.write_text(json.dumps({'schemaVersion':'relay_pi_runtime_manifest/v1',
                'workItemCoreHost':{'extension':f'/releases/{release}/ext.ts'},
                'profiles':{'managed':{'sessionRoot':str(self.root),'unattended':True,
                    'piBinary':f'/releases/{release}/pi','toolchainPaths':[f'/releases/{release}/cc'],
                    **profile}}}))
            actor=next(r for r in read_pi(self.path,AT,data=data,source_identity=source) if r.kind=='attribution')
            return actor, input_signature(source)
        first, first_signature = deploy('a'*40)
        second, second_signature = deploy('b'*40)
        self.assertEqual(first.payload['context']['entrypoint'],'automation')
        self.assertEqual((first.entity_ref, first.fact_ref, first.evidence),
                         (second.entity_ref, second.fact_ref, second.evidence))
        self.assertEqual(first_signature, second_signature)
        # A collector-relevant change is still new evidence and a cache miss.
        changed, changed_signature = deploy('b'*40, computeCostHost={'sclKernelRoot':'/kernel'})
        self.assertNotEqual(first.evidence, changed.evidence)
        self.assertNotEqual(first_signature, changed_signature)
        # The Host binding's release paths move with every deployment; its
        # Harness and kernel roots stay. Only the roots decide collection.
        def bound(release, kernel='/kernel'):
            return deploy(release, computeCostHost={'sclKernelRoot':kernel,'sclHarnessRoot':'/harness',
                'extension':f'/releases/{release}/ComputeCost/runtime/pi-usage-binding.mjs',
                'pythonPath':f'/releases/{release}/ComputeCost','python':'/env/bin/python',
                'sclExecutable':f'/tools/{release}/bin/harness-scl'})
        bound_a, bound_a_signature = bound('a'*40)
        bound_b, bound_b_signature = bound('b'*40)
        self.assertEqual((bound_a.entity_ref, bound_a.fact_ref, bound_a.evidence),
                         (bound_b.entity_ref, bound_b.fact_ref, bound_b.evidence))
        self.assertEqual(bound_a_signature, bound_b_signature)
        moved, moved_signature = bound('b'*40, kernel='/other-kernel')
        self.assertNotEqual(bound_b.evidence, moved.evidence)
        self.assertNotEqual(bound_b_signature, moved_signature)

    def test_authenticated_recovery_keeps_human_channel_not_automation(self):
        human=self.user('human',None,channel='telegram',text='rebuild this WI')
        token=human['message']['content'].split('[Host human input reference: ',1)[1].split('.',1)[0]
        boundary={'id':'boundary','parentId':'human','type':'custom','timestamp':AT,
                  'customType':'pulse-computecost-usage',
                  'data':{'schema':'pi_host_usage_capture_receipt/v1'}}
        recovery={'id':'recovery','parentId':'boundary','type':'message','timestamp':AT,
                  'message':{'role':'user','content':
                      'SCL_HOST_RECOVERY_RESULT: Host control result, not a new human instruction.\n'
                      f'human_input_ref={token}\n{{"machine":{{}}}}'}}
        answer={'id':'answer','parentId':'recovery','type':'message','timestamp':AT,
                'message':{'role':'assistant','model':'openai/test','provider':'openai',
                           'usage':{'input':10,'output':2,'cacheRead':0,'cacheWrite':0,'totalTokens':12}}}
        records=[{'type':'session','id':'managed','timestamp':AT},human,boundary,recovery,answer]
        rows=read_pi(self.path,AT,data=b''.join(canonical(r)+b'\n' for r in records),
                     source_identity=self.unattended_source())
        actors=[r for r in rows if r.kind=='attribution' and r.payload['target_ref']
                ==reference('usage',['managed','answer','assistant'])]
        self.assertEqual(len(actors),1)
        self.assertEqual(actors[0].payload['context']['entrypoint'],'telegram')

        bad={**recovery,'id':'bad','message':{'role':'user','content':
            'SCL_HOST_RECOVERY_RESULT: Host control result, not a new human instruction.\n'
            f'human_input_ref={"0"*64}\n{{"machine":{{}}}}'}}
        bad_answer={**answer,'id':'bad-answer','parentId':'bad'}
        bad_rows=read_pi(self.path,AT,data=b''.join(canonical(r)+b'\n' for r in
            [records[0],human,boundary,bad,bad_answer]),source_identity=self.unattended_source())
        self.assertFalse([r for r in bad_rows if r.kind=='attribution' and r.payload['target_ref']
                          ==reference('usage',['managed','bad-answer','assistant'])])

    def test_multiple_selected_profiles_sharing_one_unattended_root_are_still_proven(self):
        manifest = self.root / 'runtime-manifest.json'
        manifest.write_text(json.dumps({'profiles': {
            'one': {'sessionRoot': str(self.root), 'unattended': True},
            'two': {'sessionRoot': str(self.root), 'unattended': True}}}))
        source={**self.source,'id':'managed-pi','kind':'pi','runtime_manifest':str(manifest),
                'profiles':['one','two'],'layout':'pi_session_root.v1'}
        plain={'id':'plain','parentId':None,'type':'message','timestamp':AT,
               'message':{'role':'user','content':'system continuation'}}
        answer={'id':'answer','parentId':'plain','type':'message','timestamp':AT,
                'message':{'role':'assistant','model':'openai/test','provider':'openai',
                           'usage':{'input':10,'output':2,'cacheRead':0,'cacheWrite':0,'totalTokens':12}}}
        records=[{'type':'session','id':'managed','timestamp':AT},plain,answer]
        rows=read_pi(self.path,AT,data=b''.join(canonical(r)+b'\n' for r in records),source_identity=source)
        actor=next(r for r in rows if r.kind=='attribution')
        self.assertEqual(actor.payload['context']['entrypoint'],'automation')

    def test_interactive_or_claimed_human_input_is_not_relabelled_automation(self):
        plain={'id':'plain','parentId':None,'type':'message','timestamp':AT,
               'message':{'role':'user','content':'direct input'}}
        answer={'id':'answer','parentId':'plain','type':'message','timestamp':AT,
                'message':{'role':'assistant','model':'openai/test','provider':'openai',
                           'usage':{'input':10,'output':2,'cacheRead':0,'cacheWrite':0,'totalTokens':12}}}
        records=[{'type':'session','id':'managed','timestamp':AT},plain,answer]
        data=b''.join(canonical(r)+b'\n' for r in records)
        self.assertFalse([r for r in read_pi(self.path,AT,data=data,
                         source_identity=self.unattended_source(unattended=False)) if r.kind=='attribution'])
        claimed={**plain,'message':{'role':'user','content':
            'bad [Host human input reference: not-a-valid-ref. This records message provenance, not blanket authorization.]'}}
        rows=read_pi(self.path,AT,data=b''.join(canonical(r)+b'\n' for r in
            [records[0],claimed,answer]),source_identity=self.unattended_source())
        self.assertFalse([r for r in rows if r.kind=='attribution'])

    def test_hook_and_nested_tool_do_not_inherit_native_model(self):
        totals={'input':10,'output':2,'cacheRead':0,'cacheWrite':0,'totalTokens':12}
        records=[{'type':'session','id':THREAD,'version':3,'timestamp':AT},
                 {'id':'model','parentId':None,'type':'model_change','timestamp':AT,'modelId':'model-a','provider':'openai'},
                 {'id':'native','parentId':'model','type':'compaction','timestamp':AT,'usage':totals},
                 {'id':'hook','parentId':'native','type':'compaction','timestamp':AT,'fromHook':True,'usage':totals},
                 {'id':'tool','parentId':'hook','type':'message','timestamp':AT,'message':{'role':'toolResult','usage':totals}}]
        rows=read_pi(self.path,AT,data=b''.join(canonical(r)+b'\n' for r in records))
        known={r.entity_ref:r for r in rows if r.kind=='usage'}
        self.assertEqual(known[reference('usage',[THREAD,'native','compaction'])].payload['model'],'model-a')
        self.assertIsNone(known[reference('usage',[THREAD,'hook','compaction'])].payload['model'])
        self.assertIsNone(known[reference('usage',[THREAD,'tool','tool_summary'])].payload['model'])
        self.assertEqual(sum(sum(r.payload['tokens'].values()) for r in known.values()),36)

    def test_model_is_taken_from_the_selected_branch(self):
        g=PiAncestry(self.path)
        g.consume({'id':'one','parentId':None,'type':'model_change','modelId':'first','provider':'p'})
        g.consume({'id':'two','parentId':'one','type':'model_change','modelId':'second','provider':'p'})
        self.assertEqual(g.consume({'id':'branch','parentId':'one','type':'compaction'}).model,'first')

    def test_old_unknown_model_refines_without_changing_quantity_or_rewriting_known_model(self):
        from pulse_compute_cost.receipt_sources import usage_receipt
        row=usage_receipt('pi','native',{'input':10,'output':2,'cacheRead':0,'cacheWrite':0},at=AT,observed=AT,model=None)
        row=replace(row,evidence=(reference('native_record','same-native-event'),))
        fixed=Receipt.make(row.kind,None,{**row.payload,'model':'first'},entity_ref=row.entity_ref,
                           occurred_at=AT,observed_at=AT,producer=row.producer,evidence=row.evidence)
        result=[row];_revision(result,fixed)
        self.assertEqual(result[-1].supersedes,row.fact_ref)
        changed=Receipt.make(row.kind,None,{**row.payload,'model':'other'},entity_ref=row.entity_ref,
                           occurred_at=AT,observed_at=AT,producer=row.producer,evidence=row.evidence)
        result=[fixed];_revision(result,changed)
        self.assertIsNone(result[-1].supersedes)

    def test_past_unknown_start_does_not_contaminate_a_later_week(self):
        row=usage(at='2026-09-11T00:00:00Z',granularity='turn_aggregate')
        window=(datetime(2026,9,14,tzinfo=timezone.utc),datetime(2026,9,21,tzinfo=timezone.utc))
        self.assertEqual(_period(row,window),'outside')
        row=replace(row,occurred_at='2026-09-22T00:00:00Z')
        self.assertEqual(_period(row,window),'time_unallocated')

    def test_distinct_node_source_ids_are_not_execution_conflicts(self):
        from pulse_compute_cost.collaboration_query import _qualified_collector_sources
        from pulse_compute_cost.receipts import current_receipts
        def cover(label,reason='execution_inventory_not_observed'):
            detail=dict(label=label,kind='harness_root',layout='registered_recursive.v1',files_seen=1,
                        files_read=1,files_invalid=0,excluded_directories=0,latest_usage_at=None,errors={})
            return Receipt.make('coverage','old-node-local-id',dict(domain='execution',wi=None,
                source_ref=reference('source','canonical-harness'),status='unknown',expected=None,observed=None,
                watermark=None,reason=reason,source_detail=detail),occurred_at=None,observed_at=AT,producer='registered_collector.v1')
        left,right=cover('one.harness_root'),cover('two.harness_root')
        original=digest([left.to_dict(),right.to_dict()])
        active,conflicts=current_receipts(_qualified_collector_sources([left,right]))
        self.assertEqual(len(active),2);self.assertFalse(conflicts)
        self.assertEqual(digest([left.to_dict(),right.to_dict()]),original)
        _,conflicts=current_receipts(_qualified_collector_sources([left,cover('one.harness_root','different')]))
        self.assertEqual(len(conflicts),1)


class PiExistingKernelTests(unittest.TestCase):
    def setUp(self):
        self.f=scl_tests.SessionAttributionTests('test_raw_native_to_active_lineage_to_existing_query');self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        host=pi_host_ref(THREAD)
        self.f.db.execute('UPDATE source_attachments SET logical_source_ref=?,host_session_ref=?',(host,host))
        self.f.db.commit()
        data=[{'type':'session','id':THREAD,'timestamp':START},
              {'id':'u','type':'message','parentId':None,'timestamp':END,
               'message':{'role':'assistant','model':'openai/test','provider':'openai',
                          'usage':{'input':100,'output':0,'cacheRead':0,'cacheWrite':0}}}]
        self.path=self.f.root/'pi.jsonl';self.path.write_bytes(b''.join(canonical(r)+b'\n' for r in data))
        self.source={'id':'pi','kind':'pi','path':str(self.path),'node':'testmac','actor_attribution':True,
                     'scl_kernel_root':str(self.f.kernel),'scl_surface_id':SURFACE}
        self.registry={'schema':'computecost_source_registry/v1','sources':[self.source]}

    def test_native_pi_request_uses_the_existing_kernel_binding(self):
        before=self.f.database.read_bytes()
        collect_registered_sources(self.registry,self.f.ledger)
        report=query_collaboration(self.f.ledger,CollaborationQuery(work_items=(WI,)),catalog=CATALOG,catalog_observed_at=AT)
        self.assertEqual(report['summary']['usage']['event_count'],1)
        self.assertEqual(report['summary']['usage']['wi_unassigned_event_count'],0)
        self.assertEqual(report['summary']['usage']['agent_unassigned_event_count'],0)
        self.assertEqual(self.f.database.read_bytes(),before)

    def test_request_before_binding_is_not_assigned(self):
        self.f.db.execute('UPDATE source_attachments SET created_at=?',('2026-01-02T00:00:00Z',));self.f.db.commit()
        collect_registered_sources(self.registry,self.f.ledger)
        report=query_collaboration(self.f.ledger,CollaborationQuery(),catalog=CATALOG,catalog_observed_at=AT)
        self.assertEqual(report['summary']['usage']['wi_unassigned_event_count'],1)


if __name__=='__main__':unittest.main()
