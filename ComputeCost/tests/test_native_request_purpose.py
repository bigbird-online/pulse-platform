"""Fresh native inputs -> explicit declarations -> the actual collector/query."""
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import test_scl_session_attribution as f
from pulse_compute_cost.native_request_purpose import hook,read_inputs,POLICY
from native_purpose_fixture import capture,declare
from pulse_compute_cost.receipt_sources import collect_registered_sources,read_codex
from pulse_compute_cost.receipts import Receipt,ReceiptError,reference,current_receipts

class NativePurposeTests(unittest.TestCase):
    query=f.SessionAttributionTests.query
    collect=f.SessionAttributionTests.collect
    bind=f.SessionAttributionTests.bind
    native=f.SessionAttributionTests.native
    def setUp(self):
        f.SessionAttributionTests.setUp(self)
        for table in ('source_attachments','source_attachment_history','measurement_epochs','checkpoints','work_items'):
            self.db.execute('DELETE FROM '+table)
        self.db.commit()
        self.spool=(self.root/'request-inputs').resolve()
        self.spool.mkdir(mode=0o700)
        self.source['request_purpose_directory']=str(self.spool)
        self.index=0
    def submit(self,purpose=None,prompt='ordinary input',turn=None):
        turn=turn or 'turn-'+f.THREAD
        payload={'hook_event_name':'UserPromptSubmit','session_id':f.THREAD,'turn_id':turn,
                 'cwd':'/registered/local-space','prompt':prompt}
        original=capture(payload,self.spool)
        if purpose is not None:
            declare(self.spool,original['target_ref'],original['request_ref'],purpose,session=f.THREAD)
        return original,payload
    def more_native(self,purpose):
        self.index+=1;turn='fresh-'+str(self.index)
        total={'input_tokens':120*(self.index+1),'cached_input_tokens':20*(self.index+1),
               'cache_write_input_tokens':0,'output_tokens':10*(self.index+1)}
        last={'input_tokens':120,'cached_input_tokens':20,'cache_write_input_tokens':0,'output_tokens':10}
        start=f'2026-01-01T01:{self.index+1:02d}:00Z';end=f'2026-01-01T01:{self.index+1:02d}:40Z'
        rows=[{'type':'event_msg','timestamp':start,'payload':{'type':'task_started','turn_id':turn}},
              {'type':'turn_context','timestamp':start,'payload':{'turn_id':turn,'model':'openai/test'}},
              {'type':'event_msg','timestamp':end,'payload':{'type':'token_count','info':{'total_token_usage':total,'last_token_usage':last}}},
              {'type':'event_msg','timestamp':end,'payload':{'type':'task_complete'}}]
        path=next(self.logs.glob('*.jsonl'))
        with path.open('a') as out:
            for row in rows:out.write(json.dumps(row)+'\n')
        return self.submit(purpose,prompt='fresh business input '+str(self.index),turn=turn)
    def test_new_ordinary_request_requires_no_historical_review(self):
        self.submit('none')
        result=self.collect()
        self.assertEqual(result['groups'][0]['key']['wi'],'none')
        self.assertEqual(result['summary']['usage']['known_total_tokens'],130)
        self.assertEqual(result['summary']['cost']['partial_machine_cash_usd'],'112')
        self.assertEqual(result['summary']['usage']['source_coverage'],'unknown')
        for i in range(3):
            self.more_native('none')
            result=self.collect()
            self.assertEqual(result['groups'][0]['key']['wi'],'none')
            self.assertEqual(result['summary']['usage']['event_count'],i+2)
            self.assertEqual(result['summary']['usage']['known_total_tokens'],130*(i+2))
    def test_two_open_wis_use_each_request_not_latest_session_binding(self):
        self.bind(f.WI,f.THREAD,f.BEFORE)
        self.bind(f.OTHER_WI,f.THREAD,f.BEFORE)
        before_kernel=self.database.read_bytes()
        self.submit(f.WI);self.more_native(f.OTHER_WI)
        result=self.collect()
        self.assertEqual({g['key']['wi'] for g in result['groups'] if g['usage']['event_count']},{f.WI,f.OTHER_WI})
        self.assertEqual(result['summary']['usage']['known_total_tokens'],260)
        self.assertEqual(self.database.read_bytes(),before_kernel)
        self.assertFalse(current_receipts(self.ledger.receipt_snapshot()['facts'])[1])
        mark=self.ledger.receipt_snapshot()['watermark'];old=self.query(watermark=mark)
        self.assertEqual(collect_registered_sources(self.registry,self.ledger,force_recompute=True)['inserted'],0)
        self.assertEqual(self.query(watermark=mark),old)
    def test_no_declaration_is_not_none_even_without_any_binding(self):
        self.submit()
        self.assertEqual(self.collect()['groups'][0]['key']['wi'],'unassigned')
    def test_retry_is_idempotent_and_old_watermark_is_preserved(self):
        original,payload=self.submit('none');self.collect()
        mark=self.ledger.receipt_snapshot()['watermark'];before=self.query(watermark=mark)
        self.assertEqual(capture(payload,self.spool),original)
        declare(self.spool,original['target_ref'],original['request_ref'],'none',session=f.THREAD)
        self.assertEqual(collect_registered_sources(self.registry,self.ledger)['inserted'],0)
        self.more_native('none');self.collect()
        self.assertEqual(self.query(watermark=mark),before)
    def test_changed_native_declarations_remain_conflicting_not_latest_wins(self):
        original,_=self.submit('none');self.collect()
        mark=self.ledger.receipt_snapshot()['watermark'];before=self.query(watermark=mark)
        declare(self.spool,original['target_ref'],original['request_ref'],f.WI,session=f.THREAD)
        self.assertEqual(self.collect()['groups'][0]['key']['wi'],'ambiguous')
        self.assertEqual(self.query(watermark=mark),before)
        self.assertEqual(self.query()['summary']['usage']['known_total_tokens'],130)
    def test_wrong_host_cannot_declare_for_another_native_input(self):
        original,_=self.submit()
        with self.assertRaises(ReceiptError):
            declare(self.spool,original['target_ref'],original['request_ref'],'none',session=f.SECOND)
        self.assertEqual(self.collect()['groups'][0]['key']['wi'],'unassigned')
    def test_new_unclassified_steering_withdraws_prior_whole_turn_claim(self):
        self.submit('none');self.collect()
        mark=self.ledger.receipt_snapshot()['watermark'];old=self.query(watermark=mark)
        original,_=self.submit(prompt='new steering within this same native turn')
        self.assertEqual(self.collect()['groups'][0]['key']['wi'],'unassigned')
        declare(self.spool,original['target_ref'],original['request_ref'],'none',session=f.THREAD)
        self.assertEqual(self.collect()['groups'][0]['key']['wi'],'none')
        self.assertEqual(self.query(watermark=mark),old)
    def test_two_different_purposes_in_one_aggregate_remain_ambiguous(self):
        self.submit(f.WI)
        self.submit(f.OTHER_WI,prompt='different purpose in same aggregate')
        self.assertEqual(self.collect()['groups'][0]['key']['wi'],'ambiguous')
    def test_independent_contrary_assignment_is_not_removed(self):
        self.submit(f.WI)
        usage=next(r for r in read_codex(self.logs,f.END) if r.kind=='usage')
        other=Receipt.make('attribution','independent',{'target_ref':usage.entity_ref,'wi':f.OTHER_WI,
            'executor':None,'responsible':None,'delegator':None,'state':'assigned',
            'policy':'independent_review.v1','effective_at':f.START},occurred_at=f.START,
            observed_at=f.END,producer='independent_review.v1',evidence=(reference('proof','independent'),))
        self.ledger.ingest_receipts([usage,other])
        self.assertEqual(self.collect()['groups'][0]['key']['wi'],'ambiguous')
    def test_bad_private_input_degrades_source_without_dropping_tokens(self):
        original,_=self.submit('none')
        path=self.spool/original['target_ref']/(original['request_ref']+'.input.json')
        path.chmod(0o644)
        result=collect_registered_sources(self.registry,self.ledger)
        self.assertEqual(result['sources'][0]['status'],'partial')
        self.assertEqual(self.query()['summary']['usage']['known_total_tokens'],130)
        self.assertEqual(self.query()['groups'][0]['key']['wi'],'unassigned')
    def test_plain_prompt_and_native_ids_are_not_persisted(self):
        self.submit('none',prompt='PRIVATE_TEXT_NEVER_STORE')
        content=''.join(p.read_text() for p in self.spool.rglob('*.json'))
        self.assertNotIn('PRIVATE_TEXT_NEVER_STORE',content)
        self.assertNotIn(f.THREAD,content)
        self.assertNotIn('turn-'+f.THREAD,content)
    def test_hook_does_not_declare_or_block_by_itself(self):
        _,payload=self.submit()
        result=hook(payload,self.spool,['python','/tested/runtime/codex-request-purpose.py'])
        self.assertIn('declare',result['hookSpecificOutput']['additionalContext'])
        self.assertNotIn('decision',result)
        self.assertEqual(self.collect()['groups'][0]['key']['wi'],'unassigned')
        self.assertIsNone(hook({'hook_event_name':'Stop'},self.spool,['python','script']))

    def _rewrite_native(self, mutate):
        path = next(self.logs.glob('*.jsonl'))
        rows = [json.loads(line) for line in path.read_text().splitlines()]
        rows = mutate(rows)
        path.write_text(''.join(json.dumps(row)+'\n' for row in rows))
        return path

    def test_declaration_is_a_pure_output_without_private_filesystem_access(self):
        from pulse_compute_cost import native_request_purpose as n
        from pulse_compute_cost.receipts import digest
        original, _ = self.submit()
        with patch.object(n, '_read', side_effect=AssertionError('Agent read the private spool')), \
             patch.object(n, '_directory', side_effect=AssertionError('Agent inspected the private spool')), \
             patch.object(n, '_once', side_effect=AssertionError('Agent wrote the private spool')):
            result = n.declaration_message(original['target_ref'], original['request_ref'],
                original['native_scope_ref'], digest(original), 'none', session=f.THREAD)
        self.assertEqual(result['origin'], 'agent_declaration')
        self.assertEqual(list(self.spool.rglob('*.purpose.json')), [])
        # A returned Python value alone is not a native tool observation.
        self.assertEqual(self.collect()['groups'][0]['key']['wi'], 'unassigned')

    def test_private_purpose_file_alone_is_not_native_evidence(self):
        from pulse_compute_cost.receipts import digest
        original, _ = self.submit()
        path = self.spool/original['target_ref']/(original['request_ref']+'.purpose.json')
        path.write_text(json.dumps({'schema': 'codex_request_declaration/v1',
            'request_ref': original['request_ref'], 'input_sha256': digest(original),
            'purpose': 'none', 'origin': 'agent_declaration', 'declared_at': f.END}))
        path.chmod(0o600)
        self.assertEqual(self.collect()['groups'][0]['key']['wi'], 'unassigned')

    def test_unpaired_tool_output_cannot_attribute_usage(self):
        self.submit('none')
        self._rewrite_native(lambda rows: [r for r in rows if r['payload'].get('type') != 'function_call'])
        result = self.collect()
        self.assertEqual(result['groups'][0]['key']['wi'], 'unassigned')
        self.assertEqual(result['summary']['usage']['known_total_tokens'], 130)

    def test_assistant_or_user_prose_is_not_a_tool_result(self):
        self.submit('none')
        def rewrite(rows):
            expanded = []
            for row in rows:
                if row['payload'].get('type') == 'function_call_output':
                    for role in ('assistant', 'user'):
                        expanded.append({**row, 'payload': {'type': 'message', 'role': role,
                            'content': [{'type': 'text', 'text': row['payload']['output']}]}})
                else:
                    expanded.append(row)
            return expanded
        self._rewrite_native(rewrite)
        self.assertEqual(self.collect()['groups'][0]['key']['wi'], 'unassigned')

    def test_wrong_captured_input_digest_is_not_accepted(self):
        self.submit('none')
        def rewrite(rows):
            for row in rows:
                p = row['payload']
                if p.get('type') == 'function_call':
                    args = json.loads(p['arguments'])
                    import re
                    args['cmd'] = re.sub(r'--input-sha256 [0-9a-f]{64}', '--input-sha256 '+'a'*64, args['cmd'])
                    p['arguments'] = json.dumps(args)
                elif p.get('type') == 'function_call_output':
                    out = json.loads(p['output']);out['input_sha256'] = 'a'*64;p['output'] = json.dumps(out)
            return rows
        self._rewrite_native(rewrite)
        self.assertEqual(self.collect()['groups'][0]['key']['wi'], 'unassigned')

    def test_command_and_result_must_declare_the_same_purpose(self):
        self.submit('none')
        def rewrite(rows):
            for row in rows:
                p = row['payload']
                if p.get('type') == 'function_call_output':
                    out = json.loads(p['output']);out['purpose'] = f.WI;p['output'] = json.dumps(out)
            return rows
        self._rewrite_native(rewrite)
        self.assertEqual(self.collect()['groups'][0]['key']['wi'], 'unassigned')

    def test_shell_program_is_not_the_native_declaration_command(self):
        self.submit('none')
        def rewrite(rows):
            for row in rows:
                p = row['payload']
                if p.get('type') == 'function_call':
                    args = json.loads(p['arguments']);args['cmd'] += '; echo unrelated';p['arguments'] = json.dumps(args)
            return rows
        self._rewrite_native(rewrite)
        self.assertEqual(self.collect()['groups'][0]['key']['wi'], 'unassigned')

    def test_literal_code_mode_preserves_the_same_native_statement(self):
        self.submit('none')
        def rewrite(rows):
            for row in rows:
                p = row['payload']
                if p.get('type') == 'function_call':
                    args = json.loads(p.pop('arguments'))
                    p.update(type='custom_tool_call', name='exec', input='text(await tools.exec_command('+json.dumps(args)+'));')
                elif p.get('type') == 'function_call_output':
                    p['type'] = 'custom_tool_call_output'
            return rows
        self._rewrite_native(rewrite)
        self.assertEqual(self.collect()['groups'][0]['key']['wi'], 'none')

    def test_paginated_physical_file_uses_its_logical_native_request(self):
        self.submit('none')
        def rewrite(rows):
            rows[0]['payload'].update(history_mode='paginated', session_id=f.THREAD)
            for i, row in enumerate(rows):row['ordinal'] = i
            return rows
        path = self._rewrite_native(rewrite)
        path.rename(path.with_name(path.stem+'_00000000-0000-4000-8000-000000000099.jsonl'))
        result = self.collect()
        self.assertEqual(result['groups'][0]['key']['wi'], 'none')
        self.assertEqual(result['summary']['usage']['known_total_tokens'], 130)


    def test_uncaptured_usage_does_not_open_a_purpose_transcript(self):
        usages = read_codex(self.logs, f.END)
        with patch('pulse_compute_cost.native_request_purpose.native_declarations',
                   side_effect=AssertionError('uncaptured requests have no purpose source')):
            self.assertEqual(read_inputs(self.spool, usages, paths=list(self.logs.glob('*.jsonl'))), [])
        self.assertEqual(self.collect()['groups'][0]['key']['wi'], 'unassigned')

    def test_current_thread_can_have_a_distinct_root_session(self):
        self.submit('none')
        path = next(self.logs.glob('*.jsonl'))
        rows = [json.loads(line) for line in path.read_text().splitlines()]
        rows[0]['payload'].update(session_id=f.SECOND, parent_thread_id=f.SECOND,
                                  history_mode='paginated')
        path.write_text(''.join(json.dumps(row)+'\n' for row in rows))
        result = self.collect()
        self.assertEqual(result['groups'][0]['key']['wi'], 'none')
        self.assertEqual(result['summary']['usage']['known_total_tokens'], 130)
        self.assertEqual(result['summary']['cost']['partial_machine_cash_usd'], '112')

    def test_unrelated_child_context_cannot_poison_new_root_purpose(self):
        self.submit('none')
        before = self.collect()
        old_mark = self.ledger.receipt_snapshot()['watermark']
        old_view = self.query(watermark=old_mark)
        foreign = self.logs / ('rollout-2026-01-01T00-00-00-'+f.SECOND+'.jsonl')
        foreign.write_text(''.join(json.dumps(row)+'\n' for row in [
            {'type':'session_meta', 'timestamp':f.START, 'payload':{
                'id':f.SECOND, 'session_id':f.THREAD, 'parent_thread_id':f.THREAD,
                'history_mode':'paginated'}},
            {'type':'session_meta', 'timestamp':f.START, 'payload':{'id':f.THREAD}}
        ]))
        after = self.collect()
        self.assertEqual(after['groups'][0]['key']['wi'], 'none')
        source_health = lambda view: [(s['status'],s.get('reason')) for s in view['summary']['usage']['source_details'] if s.get('kind')=='codex']
        self.assertEqual(source_health(after), source_health(before))
        self.assertEqual(after['summary']['usage']['known_total_tokens'],
                         before['summary']['usage']['known_total_tokens'])
        self.assertEqual(after['summary']['cost']['partial_machine_cash_usd'],
                         before['summary']['cost']['partial_machine_cash_usd'])
        self.assertEqual(self.query(watermark=old_mark), old_view)
        self.assertEqual(collect_registered_sources(self.registry,self.ledger)['inserted'], 0)

    def test_current_thread_must_still_match_filename_and_usage(self):
        from pulse_compute_cost.native_request_purpose import native_declarations
        self.submit('none')
        usages = read_codex(self.logs, f.END)
        path = next(self.logs.glob('*.jsonl'))
        rows = [json.loads(line) for line in path.read_text().splitlines()]
        rows[0]['payload']['id'] = f.SECOND
        path.write_text(''.join(json.dumps(row)+'\n' for row in rows))
        with self.assertRaises(ReceiptError):
            native_declarations([path], usages)

    def test_duplicate_applicable_thread_header_is_not_last_writer_wins(self):
        from pulse_compute_cost.native_request_purpose import native_declarations
        self.submit('none')
        usages = read_codex(self.logs, f.END)
        path = next(self.logs.glob('*.jsonl'))
        rows = [json.loads(line) for line in path.read_text().splitlines()]
        rows.insert(1, rows[0])
        path.write_text(''.join(json.dumps(row)+'\n' for row in rows))
        with self.assertRaises(ReceiptError):
            native_declarations([path], usages)

    def test_only_captured_targets_are_passed_to_declaration_enrichment(self):
        from pulse_compute_cost.native_request_purpose import native_declarations
        self.submit('none')
        captured_target = next(self.spool.iterdir()).name
        self.index += 1
        self.more_native(None)
        # The new request is captured, too. Removing only its synthetic fixture
        # input models the unconfigured native ingress, not production cleanup.
        other = [p for p in self.spool.iterdir() if p.name != captured_target]
        self.assertEqual(len(other), 1)
        for p in other[0].glob('*.input.json'):
            p.unlink()
        other[0].rmdir()
        usages = read_codex(self.logs, f.END)
        with patch('pulse_compute_cost.native_request_purpose.native_declarations',
                   wraps=native_declarations) as observed:
            joined = read_inputs(self.spool, usages, paths=list(self.logs.glob('*.jsonl')))
        self.assertEqual({r.entity_ref for r in observed.call_args.args[1]}, {captured_target})
        self.assertEqual({t for t, _, _ in joined}, {captured_target})

if __name__=='__main__':unittest.main()
