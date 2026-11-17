"""Subcall source evidence never substitutes for task authority or measurements."""
from copy import deepcopy
from pathlib import Path
import json
import tempfile
import unittest
from pulse_compute_cost.receipts import ReceiptError, reference
from pulse_compute_cost.receipt_sources import usage_receipt, actor_receipt
from pulse_compute_cost.runtime_subcalls import (reviewed_subcalls, native_launches, launch_attributions,
    validate_subcall, _command_runtime, _commands)
from pulse_compute_cost.collaboration_query import query_collaboration, CollaborationQuery
from pulse_compute_cost.ledger import UsageLedger
from pulse_compute_cost.stats_dashboard import _view
from test_collaboration_receipts import CATALOG, AT

class RuntimeSubcallsTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.home=Path(self.tmp.name);self.ledger=UsageLedger(self.home/'ledger')
        self.child='11111111-1111-4111-8111-111111111111'
        self.row=usage_receipt('cc','child-request',{'input_tokens':100,'output_tokens':2,
            'cache_read_input_tokens':0,'cache_creation_input_tokens':0},at=AT,observed=AT,
            model='openai/test',native_scope_ref=reference('native_session',['cc',self.child]))
        self.context={'parent_scope_ref':reference('native_session',['codex','parent']),
            'child_scope_ref':self.row.payload['native_scope_ref'],'call_ref':reference('call','one'),
            'parent_runtime':'codex','child_runtime':'cc','basis':'reviewed_launch'}
        self.document={'schema':'computecost_reviewed_activity/v1','items':[],'subcalls':[{
            'context':self.context,'target_fact_refs':[self.row.fact_ref],'node':'ownermac',
            'entrypoint':'codex_app','evidence_refs':[reference('proof','reviewed-launch')]}]}
    def query(self,watermark=None):
        return query_collaboration(self.ledger,CollaborationQuery(scope='activity',from_date='2026-09-13',
            to_date='2026-09-14',group_by=('agent','wi','entrypoint','node','runtime','model'),watermark=watermark),
            catalog=CATALOG,catalog_observed_at=AT)
    def classify(self):
        return reviewed_subcalls([self.row],self.document,AT)
    def test_reviewed_source_is_finite_idempotent_and_conserves_old_watermark(self):
        self.ledger.ingest_receipts([self.row]);mark=self.ledger.receipt_snapshot()['watermark'];before=self.query(mark)
        annotations=self.classify();self.ledger.ingest_receipts(annotations);after=self.query()
        g=after['groups'][0]
        self.assertEqual((g['key']['entrypoint'],g['key']['runtime'],g['key']['wi']),('codex_app','cc','unassigned'))
        self.assertEqual(g['key']['agent'],'unassigned')
        self.assertEqual(after['summary']['usage']['tokens'],before['summary']['usage']['tokens'])
        self.assertEqual(after['summary']['cost']['partial_machine_cash_usd'],before['summary']['cost']['partial_machine_cash_usd'])
        self.assertEqual(g['usage']['subcalls']['event_count'],1)
        self.assertEqual(self.query(mark),before)
        self.assertEqual(self.ledger.ingest_receipts(annotations)['inserted'],0)
        self.assertEqual(reviewed_subcalls([],self.document,AT),[])
    def test_wrong_child_scope_runtime_self_parent_and_raw_fields_rejected(self):
        for update in ({'child_scope_ref':reference('native_session','other')},{'child_runtime':'pi'},
                       {'parent_scope_ref':self.context['child_scope_ref']},{'path':'/private/raw'}):
            doc=deepcopy(self.document);doc['subcalls'][0]['context'].update(update)
            with self.assertRaises(ReceiptError):reviewed_subcalls([self.row],doc,AT)
    def test_subcall_does_not_replace_child_agent(self):
        actor=actor_receipt(self.row,node='ownermac',executor='hestia',observed=AT)
        self.ledger.ingest_receipts([self.row,actor,*self.classify()])
        self.assertEqual(self.query()['groups'][0]['key']['agent'],'hestia')
    def test_mixed_joint_group_retains_only_actual_subcall_count(self):
        direct=usage_receipt('cc','direct',{'input_tokens':7,'output_tokens':0,'cache_read_input_tokens':0,
            'cache_creation_input_tokens':0},at=AT,observed=AT,model='openai/test')
        self.ledger.ingest_receipts([self.row,direct,*self.classify(),
            actor_receipt(direct,node='ownermac',entrypoint='codex_app',observed=AT)])
        view=_view(self.query(),'today','activity');g=view['groups'][0]
        self.assertEqual(g['usage']['event_count'],2)
        self.assertEqual(g['detail']['subcalls']['event_count'],1)
        self.assertEqual(g['detail']['subcalls']['item_count'],1)
    def test_disputed_parent_does_not_get_a_successful_subcall_label(self):
        other=deepcopy(self.document);other['subcalls'][0]['context']['parent_scope_ref']=reference('parent','other')
        self.ledger.ingest_receipts([self.row,*self.classify(),*reviewed_subcalls([self.row],other,AT)])
        q=self.query();self.assertNotIn('subcalls',q['groups'][0]['usage'])
        self.assertIn('actor_attribution_conflict',q['summary']['cost']['reasons'])
    def transcript(self, command='claude -p --output-format json "fixture"', output=None, wrapper=False):
        output=output if output is not None else json.dumps({'type':'result','session_id':self.child,'is_error':False})
        call={'type':'function_call','name':'exec_command','call_id':'call1','arguments':json.dumps({'cmd':command})}
        if wrapper:
            call={'type':'custom_tool_call','name':'exec','call_id':'call1',
                'input':'const r=await tools.exec_command({cmd:'+json.dumps(command)+',max_output_tokens:2000});text(r);'}
        rows=[{'type':'session_meta','payload':{'id':'parent','originator':'Codex Desktop','source':'vscode'}},
            {'type':'response_item','timestamp':'2026-09-13T00:59:59Z','payload':call},
            {'type':'response_item','timestamp':'2026-09-13T01:00:01Z','payload':{
                'type':'custom_tool_call_output' if wrapper else 'function_call_output','call_id':'call1','output':output}}]
        p=self.home/'rollout.jsonl';p.write_text(''.join(json.dumps(r)+'\n' for r in rows));return p
    def test_actual_cli_result_links_persisted_child_without_inheriting_wi(self):
        launches=native_launches(self.transcript(),{'node':'ownermac'})
        self.assertEqual(len(launches),1)
        rows=launch_attributions([self.row],launches,AT);self.assertEqual(len(rows),1)
        self.assertIsNone(rows[0].payload['wi']);self.assertIsNone(rows[0].payload['executor'])
        self.assertEqual(rows[0].payload['context']['entrypoint'],'codex_app')
    def test_custom_exec_literal_is_parsed_not_executed(self):
        self.assertEqual(len(native_launches(self.transcript(wrapper=True),{'node':'ownermac'})),1)
        self.assertEqual(_commands({'type':'custom_tool_call','name':'exec','input':
            'const r=await tools.exec_command({cmd:secretVariable});text(r);'}),[])
    def test_numeric_tool_session_is_not_a_native_uuid(self):
        p=self.transcript(output=json.dumps({'session_id':36203,'output':'0.84.2'}))
        self.assertEqual(native_launches(p,{'node':'ownermac'}),[])
    def test_prompt_or_echo_is_not_a_launch(self):
        for command in ('echo "claude -p"','claude -p "x"; echo unsafe','pi --list-models openai-codex'):
            self.assertIsNone(_command_runtime(command))
    def test_missing_usage_and_wrong_interval_are_not_fabricated(self):
        launches=native_launches(self.transcript(),{'node':'ownermac'})
        self.assertEqual(launch_attributions([],launches,AT),[])
        launches[0]['started_at']='2026-09-13T01:00:00.001Z'
        self.assertEqual(launch_attributions([self.row],launches,AT),[])
    def test_multiple_child_uuids_are_ambiguous(self):
        output='\n'.join(json.dumps({'type':'result','session_id':sid}) for sid in
            (self.child,'22222222-2222-4222-8222-222222222222'))
        self.assertEqual(native_launches(self.transcript(output=output),{'node':'ownermac'}),[])
    def test_interrupted_model_call_retains_original_tokens(self):
        p=self.transcript(output=json.dumps({'type':'result','session_id':self.child,'is_error':True}))
        self.ledger.ingest_receipts([self.row,*launch_attributions([self.row],native_launches(p,{'node':'ownermac'}),AT)])
        self.assertEqual(self.query()['summary']['usage']['known_total_tokens'],102)
