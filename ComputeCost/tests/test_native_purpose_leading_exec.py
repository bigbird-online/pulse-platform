"""Only an executed leading declaration may use its first native result."""
import json
import unittest
import test_native_request_purpose as original
from native_purpose_fixture import declare
from test_scl_session_attribution import THREAD, WI, OTHER_WI, BEFORE, START, END
from pulse_compute_cost.receipt_sources import collect_registered_sources, read_codex
from pulse_compute_cost.receipts import Receipt, reference


class LeadingExecPurposeTests(unittest.TestCase):
    setUp = original.NativePurposeTests.setUp
    query = original.NativePurposeTests.query
    collect = original.NativePurposeTests.collect
    bind = original.NativePurposeTests.bind
    native = original.NativePurposeTests.native
    submit = original.NativePurposeTests.submit
    _rewrite_native = original.NativePurposeTests._rewrite_native

    def compound(self, *, prefix='', suffix='text(await tools.exec_command({cmd:"true",max_output_tokens:10}));',
                 exit_code=0, later_lookalike=False, dynamic=False, missing_frame=False, joined=False):
        def rewrite(rows):
            for row in rows:
                p = row['payload']
                if p.get('type') == 'function_call':
                    args = json.loads(p.pop('arguments'))
                    encoded = json.dumps(args)
                    if dynamic:
                        encoded = encoded.replace(json.dumps(args['cmd']), 'command_from_elsewhere')
                    p.update(type='custom_tool_call', name='exec',
                             input=prefix+'text(await tools.exec_command('+encoded+'));\n'+suffix)
                elif p.get('type') == 'function_call_output':
                    declaration = p['output']
                    result = {'chunk_id':'fixture-leading', 'exit_code':exit_code, 'output':declaration,
                              'wall_time_seconds':0.01, 'original_token_count':100}
                    blocks = [{'type':'input_text', 'text':'Native execution framing'},
                              {'type':'input_text', 'text':json.dumps(result)}]
                    if later_lookalike:
                        blocks[1]['text'] = json.dumps({**result, 'exit_code':1, 'output':'failed'})
                        blocks.append({'type':'input_text', 'text':json.dumps(result)})
                    else:
                        blocks.append({'type':'input_text', 'text':json.dumps({'i':0, 'status':'fulfilled', 'value':{'output':'unrelated'}})})
                    if missing_frame:
                        blocks.pop(0)
                    p.update(type='custom_tool_call_output', output='\n'.join(b['text'] for b in blocks) if joined else blocks)
            return rows
        self._rewrite_native(rewrite)

    def test_leading_declaration_survives_unrelated_sequential_commands(self):
        self.submit('none')
        self.compound(suffix='\n'.join('text(await tools.exec_command({cmd:"true",max_output_tokens:10}));' for _ in range(3)))
        r = self.collect()
        self.assertEqual(r['groups'][0]['key']['wi'], 'none')
        self.assertEqual(r['summary']['usage']['known_total_tokens'], 130)
        self.assertEqual(r['summary']['cost']['partial_machine_cash_usd'], '112')

    def test_leading_request_purpose_precedes_mcp_and_parallel_work(self):
        self.bind(WI, THREAD, BEFORE)
        self.bind(OTHER_WI, THREAD, BEFORE)
        self.submit(WI)
        self.compound(suffix='text(await tools.mcp__codex_app__send_message_to_thread({threadId:"fixture"}));\n'
                      'const rs=await Promise.allSettled([tools.exec_command({cmd:"true"})]);rs.forEach((r,i)=>text({i,...r}));')
        self.assertEqual(self.collect()['groups'][0]['key']['wi'], WI)

    def test_leading_completed_declaration_does_not_require_later_commands_to_succeed(self):
        self.submit('none')
        self.compound(suffix='throw new Error("later operation failed");')
        self.assertEqual(self.collect()['groups'][0]['key']['wi'], 'none')

    def test_later_matching_print_cannot_replace_failed_first_result(self):
        self.submit('none')
        self.compound(later_lookalike=True)
        self.assertEqual(self.collect()['groups'][0]['key']['wi'], 'unassigned')

    def test_nonzero_exit_does_not_certify_declaration(self):
        self.submit('none')
        self.compound(exit_code=1)
        self.assertEqual(self.collect()['groups'][0]['key']['wi'], 'unassigned')

    def test_prior_statement_is_not_a_leading_declaration(self):
        self.submit('none')
        self.compound(prefix='text("other first result");\n')
        self.assertEqual(self.collect()['groups'][0]['key']['wi'], 'unassigned')

    def test_dynamic_command_is_not_executed_or_guessed(self):
        self.submit('none')
        self.compound(dynamic=True)
        self.assertEqual(self.collect()['groups'][0]['key']['wi'], 'unassigned')

    def test_missing_native_result_framing_is_not_guessed(self):
        self.submit('none')
        self.compound(missing_frame=True)
        self.assertEqual(self.collect()['groups'][0]['key']['wi'], 'unassigned')

    def test_concatenated_output_cannot_supply_positional_proof(self):
        self.submit('none')
        self.compound(joined=True)
        self.assertEqual(self.collect()['groups'][0]['key']['wi'], 'unassigned')

    def test_late_declaration_preserves_old_watermark_and_replay_quantity(self):
        record, _ = self.submit()
        self.collect()
        mark = self.ledger.receipt_snapshot()['watermark']
        before = self.query(watermark=mark)
        declare(self.spool, record['target_ref'], record['request_ref'], 'none', session=THREAD)
        self.compound()
        after = self.collect()
        self.assertEqual(after['groups'][0]['key']['wi'], 'none')
        self.assertEqual(before['summary']['usage']['tokens'], after['summary']['usage']['tokens'])
        self.assertEqual(before['summary']['cost']['partial_machine_cash_usd'], after['summary']['cost']['partial_machine_cash_usd'])
        self.assertEqual(self.query(watermark=mark), before)
        self.assertEqual(collect_registered_sources(self.registry, self.ledger, force_recompute=True)['inserted'], 0)

    def test_independent_contrary_wi_still_conflicts(self):
        self.submit('none')
        self.compound()
        usage = next(r for r in read_codex(self.logs, END) if r.kind == 'usage')
        contrary = Receipt.make('attribution', 'separate-owner', {
            'target_ref':usage.entity_ref, 'wi':OTHER_WI, 'executor':None, 'responsible':None,
            'delegator':None, 'state':'assigned', 'policy':'independent_review.v1', 'effective_at':START},
            occurred_at=START, observed_at=END, producer='independent_review.v1',
            evidence=(reference('proof', 'independent fixture'),))
        self.ledger.ingest_receipts([usage, contrary])
        self.assertEqual(self.collect()['groups'][0]['key']['wi'], 'ambiguous')


if __name__ == '__main__':
    unittest.main()
