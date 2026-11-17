"""Actual literal exec shape, startup receipt and finite reviewed attribution."""
from pathlib import Path
import json
import tempfile
import unittest
from pulse_compute_cost.runtime_subcalls import native_launches, launch_attributions, _commands, _command_runtime
from pulse_compute_cost.receipt_sources import usage_receipt
from pulse_compute_cost.receipts import reference
from pulse_compute_cost.activity_classification import reviewed_activity_receipts
from pulse_compute_cost.ledger import UsageLedger
from pulse_compute_cost.collaboration_query import CollaborationQuery, query_collaboration
from test_collaboration_receipts import AT, CATALOG

class NativeExecRegression(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        self.child = '11111111-1111-4111-8111-111111111111'
        self.command = 'cd /tmp && codex exec --sandbox read-only --skip-git-repo-check --color never "fixture"'
        self.header = ('OpenAI Codex v0.156.1\n--------\nworkdir: /private/tmp\nmodel: openai/test\n'
                       'provider: openai\napproval: never\nsandbox: read-only\nreasoning effort: xhigh\n'
                       'reasoning summaries: none\nsession id: '+self.child+'\n--------\nuser\nfixture\ncodex\nOK\n')
    def transcript(self, *, text=None, command=None, program=None):
        program = program if program is not None else 'text(await tools.exec_command({cmd:'+json.dumps(command or self.command)+',max_output_tokens:2000,yield_time_ms:30000}))'
        rows = [{'type':'session_meta','payload':{'id':'parent','originator':'Codex Desktop','source':'vscode'}},
            {'type':'response_item','timestamp':'2026-09-13T00:59:59Z','payload':{
                'type':'custom_tool_call','name':'exec','call_id':'c1','input':program}},
            {'type':'response_item','timestamp':'2026-09-13T01:00:01Z','payload':{
                'type':'custom_tool_call_output','call_id':'c1','output':[
                    {'type':'input_text','text':'Script completed\nOutput:\n'},
                    {'type':'input_text','text':json.dumps({'exit_code':0,'output':self.header if text is None else text})}]}}]
        p = self.home/'rollout.jsonl'; p.write_text(''.join(json.dumps(r)+'\n' for r in rows)); return p
    def test_actual_wrapped_cd_exec_and_text_header(self):
        rows = native_launches(self.transcript(), {'node':'ownermac'})
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['context']['child_scope_ref'], reference('native_session',['codex',self.child]))
        self.assertEqual(rows[0]['entrypoint'], 'codex_app')
    def test_spawned_subagent_source_object_is_read_without_own_entrypoint(self):
        # Codex 0.162 subagents record source as an object naming the parent thread.
        path = self.transcript()
        rows = [json.loads(line) for line in path.read_text().splitlines()]
        rows[0]['payload']['source'] = {'subagent': {'thread_spawn': {
            'parent_thread_id': 'root-thread', 'depth': 1, 'agent_path': '/root/x', 'agent_role': None}}}
        path.write_text(''.join(json.dumps(r)+'\n' for r in rows))
        launches = native_launches(path, {'node':'ownermac'})
        self.assertEqual(len(launches), 1)
        self.assertIsNone(launches[0]['entrypoint'])
    def test_literal_program_is_never_evaluated(self):
        self.assertEqual(_commands({'type':'custom_tool_call','name':'exec','input':'text(await tools.exec_command({cmd:secretVariable}))'}), [])
        self.assertEqual(native_launches(self.transcript(program='text(await tools.exec_command({cmd:"codex exec x"})); text(runOther())'), {'node':'ownermac'}), [])
    def test_dynamic_or_compound_shell_is_not_recognized(self):
        for command in ['cd $HOME && codex exec x','cd relative && codex exec x','cd /tmp; codex exec x',
                        'cd /tmp && codex exec x && echo done','echo "codex exec x"','cd /tmp && $(codex exec x)']:
            self.assertIsNone(_command_runtime(command), command)
    def test_text_from_prompt_body_is_not_a_session_receipt(self):
        for text in ['user\n'+self.header, 'codex\n'+self.header, self.header+self.header,
                     self.header.replace('sandbox: read-only\n',''), self.header.replace('session id: '+self.child,'session id: 36203')]:
            self.assertEqual(native_launches(self.transcript(text=text), {'node':'ownermac'}), [])
    def test_duplicate_startup_identity_is_not_accepted(self):
        text=self.header.replace('session id: '+self.child,'session id: '+self.child+'\nsession id: '+self.child)
        self.assertEqual(native_launches(self.transcript(text=text), {'node':'ownermac'}), [])
    def test_prompt_label_cannot_replace_header_identity(self):
        text=self.header+'session id: 22222222-2222-4222-8222-222222222222\n'
        launch=native_launches(self.transcript(text=text), {'node':'ownermac'})[0]
        self.assertEqual(launch['context']['child_scope_ref'], reference('native_session',['codex',self.child]))
    def test_launch_and_review_are_separate_and_preserve_original_watermark(self):
        row=usage_receipt('codex','native-child',{'input_tokens':100,'cached_input_tokens':0,'cache_write_input_tokens':0,'output_tokens':2},
            at=AT,observed=AT,model='openai/test',native_scope_ref=reference('native_session',['codex',self.child]))
        ledger=UsageLedger(self.home/'ledger');ledger.ingest_receipts([row])
        def query(mark=None):
            return query_collaboration(ledger,CollaborationQuery(scope='activity',from_date='2026-09-13',to_date='2026-09-14',
                group_by=('agent','wi','entrypoint','node','runtime','model'),watermark=mark),catalog=CATALOG,catalog_observed_at=AT)
        mark=ledger.receipt_snapshot()['watermark'];before=query(mark)
        source=launch_attributions([row],native_launches(self.transcript(),{'node':'ownermac'}),AT)
        self.assertIsNone(source[0].payload['executor']);self.assertIsNone(source[0].payload['wi'])
        document={'schema':'computecost_reviewed_activity/v1','items':[{'target_fact_ref':row.fact_ref,'agent':'hestia',
            'node':'ownermac','entrypoint':'codex_app','work_item_id':'wi_20260924_219','evidence_refs':[reference('review','exact-launch-task')]}]}
        annotations=source+reviewed_activity_receipts([row],document,AT)
        ledger.ingest_receipts(annotations);after=query()
        self.assertEqual(after['groups'][0]['key']['agent'],'hestia')
        self.assertEqual(after['groups'][0]['key']['wi'],'wi_20260924_219')
        self.assertEqual(after['summary']['usage']['tokens'],before['summary']['usage']['tokens'])
        self.assertEqual(after['summary']['cost']['partial_machine_cash_usd'],before['summary']['cost']['partial_machine_cash_usd'])
        self.assertEqual(query(mark),before)
        self.assertEqual(ledger.ingest_receipts(annotations)['inserted'],0)
        self.assertEqual(reviewed_activity_receipts([],document,AT),[])
