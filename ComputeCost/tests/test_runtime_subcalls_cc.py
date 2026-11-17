"""A CC Bash launch names its Codex child only through the CLI's own returned id."""
from pathlib import Path
import json
import tempfile
import unittest
from pulse_compute_cost.receipts import Receipt, reference
from pulse_compute_cost.receipt_sources import usage_receipt, actor_receipt
from pulse_compute_cost.runtime_subcalls import (cc_native_launches, launch_attributions,
    subcall_purpose_attributions, _command_runtime, PURPOSE_POLICY, REQUEST_POLICY)
from pulse_compute_cost.ledger import UsageLedger
from pulse_compute_cost.collaboration_query import CollaborationQuery, query_collaboration
from test_collaboration_receipts import AT, CATALOG

SESSION = 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa'
CHILD = '11111111-1111-4111-8111-111111111111'
WI = 'wi_20261015_422'
COMMAND = 'cd /work/cover && codex exec --json --skip-git-repo-check -s workspace-write -C /work/cover - < prompt.md'
STARTED = json.dumps({'type': 'thread.started', 'thread_id': CHILD})


def assistant(uid, call, command, at, background=None, msg='msg_launch'):
    args = {'command': command, 'description': 'cover'}
    if background is not None:
        args['run_in_background'] = background
    return {'type': 'assistant', 'uuid': uid, 'sessionId': SESSION, 'isSidechain': False, 'timestamp': at,
            'message': {'id': msg, 'role': 'assistant', 'model': 'claude-test',
                        'content': [{'type': 'tool_use', 'id': call, 'name': 'Bash', 'input': args}],
                        'usage': {'input_tokens': 1, 'output_tokens': 1}}}


def result(uid, call, owner, content, at):
    return {'type': 'user', 'uuid': uid, 'sessionId': SESSION, 'isSidechain': False, 'timestamp': at,
            'sourceToolAssistantUUID': owner,
            'message': {'role': 'user', 'content': [{'type': 'tool_result', 'tool_use_id': call,
                                                     'content': content, 'is_error': False}]}}


class CcParentLaunchTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        self.parent = usage_receipt('cc', [SESSION, 'msg_launch'], {'input_tokens': 1, 'output_tokens': 1,
            'cache_read_input_tokens': 0, 'cache_creation_input_tokens': 0}, at='2026-09-13T00:59:59Z',
            observed=AT, model='openai/test', native_scope_ref=reference('native_session', ['cc', SESSION]))
        self.child = usage_receipt('codex', 'native-child', {'input_tokens': 100, 'cached_input_tokens': 0,
            'cache_write_input_tokens': 0, 'output_tokens': 2}, at=AT, observed=AT, model='openai/test',
            native_scope_ref=reference('native_session', ['codex', CHILD]))
        self.actor = actor_receipt(self.parent, node='ownermac', executor='ken', observed=AT)

    def claim(self, wi=WI, state='assigned', producer=REQUEST_POLICY):
        return Receipt.make('attribution', None, {'target_ref': self.parent.entity_ref, 'wi': wi, 'executor': None,
            'responsible': None, 'delegator': None, 'state': state, 'policy': producer,
            'effective_at': '2026-09-13T00:59:59Z'}, entity_ref=reference('attribution', [producer, self.parent.entity_ref]),
            occurred_at='2026-09-13T00:59:59Z', observed_at=AT, producer=producer)

    def transcript(self, rows=None, *, command=COMMAND, content=STARTED, background=None, owner='u-launch'):
        rows = rows if rows is not None else [
            assistant('u-launch', 'toolu_1', command, '2026-09-13T00:59:59Z', background),
            result('u-result', 'toolu_1', owner, content, '2026-09-13T01:00:01Z')]
        path = self.home / f'{SESSION}.jsonl'
        path.write_text(''.join(json.dumps(r) + '\n' for r in rows)); return path

    def launches(self, **kwargs):
        return cc_native_launches(self.transcript(**kwargs), {'node': 'ownermac'})

    def query(self, ledger):
        return query_collaboration(ledger, CollaborationQuery(scope='activity', from_date='2026-09-13',
            to_date='2026-09-14', group_by=('agent', 'wi', 'runtime')), catalog=CATALOG, catalog_observed_at=AT)

    def test_foreground_launch_links_child_to_parent_executor_and_wi(self):
        launches = self.launches()
        self.assertEqual(len(launches), 1)
        context = launches[0]['context']
        self.assertEqual((context['parent_runtime'], context['child_runtime']), ('cc', 'codex'))
        self.assertEqual(context['child_scope_ref'], self.child.payload['native_scope_ref'])
        self.assertEqual(launches[0]['parent_usage_ref'], self.parent.entity_ref)
        facts = [self.parent, self.child, self.actor, self.claim()]
        actors = launch_attributions(facts, launches, AT)
        self.assertEqual([a.payload['executor'] for a in actors], ['ken'])
        purposes = subcall_purpose_attributions(facts + actors, launches, AT)
        self.assertEqual([(p.payload['target_ref'], p.payload['wi']) for p in purposes], [(self.child.entity_ref, WI)])
        ledger = UsageLedger(self.home / 'ledger'); ledger.ingest_receipts(facts + actors + purposes)
        groups = {g['key']['runtime']: g for g in self.query(ledger)['groups']}
        self.assertEqual((groups['codex']['key']['agent'], groups['codex']['key']['wi']), ('ken', WI))
        self.assertEqual(groups['codex']['usage']['subcalls']['event_count'], 1)
        # Repeated collection is finite.
        self.assertEqual(subcall_purpose_attributions(facts + actors + purposes, launches, AT), [])

    def test_text_header_in_result_also_names_child(self):
        header = ('OpenAI Codex v0.159.2\n--------\nworkdir: /work/cover\nmodel: gpt-test\nprovider: openai\n'
                  'approval: never\nsandbox: workspace-write\nsession id: ' + CHILD + '\n--------\n')
        command = 'cd /work/cover && codex exec --skip-git-repo-check - < prompt.md'
        self.assertEqual(len(self.launches(command=command, content=[{'type': 'text', 'text': header}])), 1)

    def test_parent_without_declared_wi_lends_no_purpose(self):
        launches = self.launches()
        facts = [self.parent, self.child, self.actor]
        self.assertEqual(subcall_purpose_attributions(facts, launches, AT), [])
        legacy = self.claim(producer='mcp_session_purpose.v1')
        self.assertEqual(subcall_purpose_attributions(facts + [legacy], launches, AT), [])
        contrary = [self.claim(), Receipt.make('attribution', None, {**self.claim().payload, 'wi': 'wi_20261015_999'},
            entity_ref=reference('attribution', ['other', self.parent.entity_ref]), occurred_at=AT, observed_at=AT,
            producer='other.v1')]
        self.assertEqual(subcall_purpose_attributions(facts + contrary, launches, AT), [])

    def test_lost_parent_purpose_withdraws_only_own_claim(self):
        launches = self.launches()
        old = self.claim()
        prior = subcall_purpose_attributions([self.parent, self.child, old], launches, AT)
        revised = Receipt.make('attribution', None, {**old.payload, 'wi': None, 'state': 'unassigned'},
            entity_ref=old.entity_ref, supersedes=old.fact_ref, occurred_at=old.occurred_at, observed_at=AT,
            producer=REQUEST_POLICY)
        rows = subcall_purpose_attributions([self.parent, self.child, old, revised, *prior], launches, AT)
        self.assertEqual([(r.payload['state'], r.supersedes) for r in rows], [('unassigned', prior[0].fact_ref)])

    def test_child_with_own_claim_keeps_it(self):
        own = Receipt.make('attribution', None, {'target_ref': self.child.entity_ref, 'wi': 'wi_20261015_999',
            'executor': None, 'responsible': None, 'delegator': None, 'state': 'assigned', 'policy': 'other.v1',
            'effective_at': AT}, occurred_at=AT, observed_at=AT, producer='other.v1')
        self.assertEqual(subcall_purpose_attributions([self.parent, self.child, self.claim(), own],
                                                      self.launches(), AT), [])

    def test_unproved_forms_are_not_launches(self):
        # Ken's actual 10-25 form: dynamic prompt and output sent to a file.
        ken = ('cd /work/cover; codex exec --skip-git-repo-check -s workspace-write -C "$PWD" '
               '"$(cat prompt.md)" < /dev/null > codex.log 2>&1; echo "exit $?"; tail -c 800 codex.log')
        for command in (ken, 'cd /work/cover && codex exec --json "$(cat prompt.md)"',
                        'cd /work/cover && codex exec --json - < prompt.md > codex.log',
                        'echo "codex exec --json - < prompt.md"', 'cat prompt.md | codex exec --json -',
                        'codex exec --json - << EOF', 'codex exec --json - < <(cat prompt.md)'):
            self.assertIsNone(_command_runtime(command), command)
            self.assertEqual(self.launches(command=command), [], command)

    def test_id_only_in_later_log_read_is_not_proof(self):
        rows = [assistant('u-launch', 'toolu_1', 'cd /work/cover && codex exec --json - < prompt.md > codex.log',
                          '2026-09-13T00:59:59Z'),
                result('u-result', 'toolu_1', 'u-launch', '', '2026-09-13T01:00:01Z'),
                assistant('u-tail', 'toolu_2', 'tail codex.log', '2026-09-13T01:00:02Z', msg='msg_tail'),
                result('u-tail-result', 'toolu_2', 'u-tail', STARTED, '2026-09-13T01:00:03Z')]
        self.assertEqual(cc_native_launches(self.transcript(rows), {'node': 'ownermac'}), [])

    def test_background_run_and_foreign_result_are_not_proof(self):
        self.assertEqual(self.launches(background=True), [])
        self.assertEqual(self.launches(owner='u-other'), [])

    def host_twin(self, executor='amanda'):
        """The Relay Host's observation of the same launching request."""
        return usage_receipt('cc', 'host-event', {'input_tokens': 1, 'output_tokens': 1,
            'cache_read_input_tokens': 0, 'cache_creation_input_tokens': 0}, at='2026-09-13T00:59:59Z',
            observed=AT, model='openai/test', execution_ref=reference('attempt', 'relay'), mapping='cc_message.v1',
            common={'wi': None, 'executor': executor, 'responsible': None, 'delegator': None},
            entity_ref=self.parent.entity_ref, native_scope_ref=self.parent.payload['native_scope_ref'])

    def test_host_and_transcript_versions_of_the_launch_both_name_the_parent(self):
        # Production 2026-10-26: Amanda's launching request had a Host head
        # (executor amanda) and a transcript head (no executor); neither is
        # current alone, and the child lost both executor and WI.
        launches = self.launches()
        facts = [self.parent, self.host_twin(), self.child, self.claim()]
        actors = launch_attributions(facts, launches, AT)
        self.assertEqual([a.payload['executor'] for a in actors], ['amanda'])
        purposes = subcall_purpose_attributions(facts + actors, launches, AT)
        self.assertEqual([p.payload['wi'] for p in purposes], [WI])
        # Versions naming different executors prove none.
        disputed = [self.parent, self.host_twin(), self.child, actor_receipt(self.parent, node='ownermac',
                    executor='ken', observed=AT)]
        self.assertEqual([a.payload['executor'] for a in launch_attributions(disputed, launches, AT)], [None])

    def test_read_only_git_steps_before_the_cli_keep_the_launch(self):
        # Amanda's 10-26 form: print the reviewed commit, then run the CLI.
        command = 'cd /work/cover && git log -1 --format=%h && ' + COMMAND.split(' && ', 1)[1]
        self.assertEqual(_command_runtime(command), 'codex')
        self.assertEqual(len(self.launches(command=command)), 1)
        for other in ('cd /work/cover && git push && ' + COMMAND.split(' && ', 1)[1],
                      'cd work && ' + COMMAND.split(' && ', 1)[1],
                      'cat old.jsonl && ' + COMMAND.split(' && ', 1)[1],
                      'cd /work/cover && codex exec --json - < a.md && codex exec --json - < b.md',
                      'git log -1 && cd /work/cover'):
            self.assertIsNone(_command_runtime(other), other)

    def test_child_outside_the_call_interval_is_not_linked(self):
        late = usage_receipt('codex', 'late-child', {'input_tokens': 1, 'cached_input_tokens': 0,
            'cache_write_input_tokens': 0, 'output_tokens': 1}, at='2026-09-13T01:00:02Z', observed=AT,
            model='openai/test', native_scope_ref=self.child.payload['native_scope_ref'])
        launches = self.launches()
        self.assertEqual(launch_attributions([self.parent, late, self.actor], launches, AT), [])
        self.assertEqual(subcall_purpose_attributions([self.parent, late, self.claim()], launches, AT), [])


if __name__ == '__main__':
    unittest.main()
