"""Native completion routes require the successful trigger's original input."""
from copy import deepcopy
from hashlib import sha256
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

import test_cc_input_context as original
from pulse_compute_cost.receipts import reference


class NativeTaskRouteTests(unittest.TestCase):
    def case(self, root, kind='SendMessage'):
        c = original.case.__wrapped__(root)
        original.background_completion(c)
        tool, result, notification = c['main'][1:4]
        tool['message']['content'][0].update(name=kind, input={
            'to': 'agent-one', 'recipient': 'agent-one', 'type': 'message'}
            if kind == 'SendMessage' else {'run_in_background': True})
        output = '/private/tmp/claude/session/tasks/agent-one.output'
        result['toolUseResult'] = ({'success': True, 'resumedAgentId': 'agent-one'}
            if kind == 'SendMessage' else {'isAsync': True, 'status': 'async_launched',
                                          'agentId': 'agent-one', 'outputFile': output})
        result['message']['content'][0]['content'] = 'native acknowledgement'
        notification['message']['content'] = notification['message']['content'].replace(
            '<task-id>task-id</task-id>', '<task-id>agent-one</task-id>').replace(
            '/tasks/task-id.output', '/tasks/agent-one.output').replace(
            '</task-notification>', '<note>runtime note</note><result>private child reply</result>'
            '<usage><subagent_tokens>100</subagent_tokens><tool_uses>1</tool_uses>'
            '<duration_ms>2000</duration_ms></usage></task-notification>')
        return c

    def routes(self, c):
        rows, usages = original.run(c)
        target = reference('usage', ['session', 'message'])
        return [r for r in rows if r.payload['target_ref'] == target], usages

    def test_native_resume_and_async_launch_keep_exact_input_and_usage(self):
        for kind in ('SendMessage', 'Agent'):
            with self.subTest(kind=kind), TemporaryDirectory() as directory:
                c = self.case(Path(directory), kind)
                _, before = self.routes(c)
                user, _, result, notification, _ = c['main']
                bare = {**user, 'uuid': 'later-human', 'parentUuid': result['uuid'],
                        'timestamp': '2026-10-01T01:00:04.500Z',
                        'message': {'content': 'different independent request'}}
                notification['parentUuid'] = bare['uuid']
                c['main'].insert(3, bare)
                routes, after = self.routes(c)
                self.assertEqual(len(routes), 1)
                route = routes[0]
                self.assertEqual(route.payload['context']['entrypoint'], 'telegram')
                self.assertEqual(route.payload['effective_at'], original.AT)
                self.assertIsNone(route.payload['wi'])
                self.assertIsNone(route.payload['executor'])
                self.assertIn(reference('input_metadata', sha256(c['metadata'].read_bytes()).hexdigest()), route.evidence)
                self.assertNotIn('private child reply', str(route.to_dict()))
                self.assertEqual([r.to_dict() for r in after], [r.to_dict() for r in before])
                self.assertEqual([r.to_dict() for r in self.routes(c)[0]], [r.to_dict() for r in routes])

    def test_resume_requires_success_and_exact_recipients(self):
        for fault in ('failed', 'wrong_to', 'wrong_recipient', 'no_recipient', 'wrong_type', 'wrong_output'):
            with self.subTest(fault=fault), TemporaryDirectory() as directory:
                c = self.case(Path(directory))
                tool, result = c['main'][1:3]
                inputs = tool['message']['content'][0]['input']
                if fault == 'failed': result['toolUseResult']['success'] = False
                elif fault == 'wrong_to': inputs['to'] = 'another-agent'
                elif fault == 'wrong_recipient': inputs['recipient'] = 'another-agent'
                elif fault == 'no_recipient': inputs.pop('to'); inputs.pop('recipient')
                elif fault == 'wrong_type': inputs['type'] = 'broadcast'
                else: result['toolUseResult']['outputFile'] = '/other/tasks/agent-one.output'
                self.assertEqual(self.routes(c)[0], [])

    def test_agent_launch_requires_async_identity_and_declared_output(self):
        for change in ({'isAsync': False}, {'status': 'done'}, {'agentId': 'other'},
                       {'outputFile': '/other/tasks/agent-one.output'}, {'outputFile': None}):
            with self.subTest(change=change), TemporaryDirectory() as directory:
                c = self.case(Path(directory), 'Agent')
                c['main'][2]['toolUseResult'].update(change)
                self.assertEqual(self.routes(c)[0], [])

    def test_default_background_agent_launch_keeps_its_input(self):
        for background, routed in ((None, 1), (True, 1), (False, 0)):
            with self.subTest(background=background), TemporaryDirectory() as directory:
                c = self.case(Path(directory), 'Agent')
                inputs = c['main'][1]['message']['content'][0]['input']
                if background is None: inputs.pop('run_in_background')
                else: inputs['run_in_background'] = background
                self.assertEqual(len(self.routes(c)[0]), routed)

    def test_repeat_completion_without_tool_id_follows_its_unique_launch(self):
        for launches, routed in ((1, 1), (2, 0)):
            with self.subTest(launches=launches), TemporaryDirectory() as directory:
                c = self.case(Path(directory), 'Agent')
                notification = c['main'][3]
                notification['message']['content'] = notification['message']['content'].replace(
                    '<tool-use-id>tool-id</tool-use-id>\n', '')
                if launches == 2:
                    # Another launch of the same task ID leaves the repeat ambiguous.
                    tool, result = deepcopy(c['main'][1]), deepcopy(c['main'][2])
                    tool['uuid'], tool['message']['content'][0]['id'] = 'tool-2', 'tool-id-2'
                    result.update(uuid='result-2', parentUuid='tool-2', sourceToolAssistantUUID='tool-2')
                    result['message']['content'][0]['tool_use_id'] = 'tool-id-2'
                    c['main'][3:3] = [tool, result]
                self.assertEqual(len(self.routes(c)[0]), routed)

    def test_duplicate_tool_or_result_identity_cannot_choose_a_route(self):
        for fault in ('duplicate_tool', 'duplicate_result'):
            with self.subTest(fault=fault), TemporaryDirectory() as directory:
                c = self.case(Path(directory))
                if fault == 'duplicate_tool':
                    c['main'][1]['message']['content'].append(deepcopy(c['main'][1]['message']['content'][0]))
                else:
                    duplicate = deepcopy(c['main'][2]); duplicate['uuid'] = 'second-result'
                    c['main'].insert(3, duplicate)
                self.assertEqual(self.routes(c)[0], [])

    def test_envelope_is_bounded_and_identity_fields_cannot_be_injected(self):
        for fault in ('dtd', 'processing_instruction', 'oversized', 'duplicate_task',
                      'unknown_field', 'nested_identity', 'traversal', 'invalid_counter'):
            with self.subTest(fault=fault), TemporaryDirectory() as directory:
                c = self.case(Path(directory)); n = c['main'][3]
                text = n['message']['content']
                if fault == 'dtd': text = '<!DOCTYPE task-notification>' + text
                elif fault == 'processing_instruction': text = '<?route injected?>' + text
                elif fault == 'oversized': text = text.replace('private child reply', 'x' * 65536)
                elif fault == 'duplicate_task': text = text.replace('</task-notification>', '<task-id>agent-one</task-id></task-notification>')
                elif fault == 'unknown_field': text = text.replace('</task-notification>', '<input-ref>invented</input-ref></task-notification>')
                elif fault == 'nested_identity': text = text.replace('<task-id>agent-one</task-id>', '<task-id><id>agent-one</id></task-id>')
                elif fault == 'traversal': text = text.replace('/session/tasks/', '/session/../tasks/')
                else: text = text.replace('<tool_uses>1</tool_uses>', '<tool_uses>-1</tool_uses>')
                n['message']['content'] = text
                self.assertEqual(self.routes(c)[0], [])


    def sidechain_resume(self, root):
        import json
        c = self.case(root)
        user, tool, result, notification, answer = c['main']
        child_input = {**user, 'uuid': 'creator-input', 'parentUuid': None,
            'timestamp': '2026-10-01T01:00:02.500Z', 'isSidechain': True,
            'agentId': 'creator', 'message': {'content': 'delegated creator input'}}
        tool.update(isSidechain=True, agentId='creator', parentUuid='creator-input')
        result.update(isSidechain=True, agentId='creator')
        result['message']['content'][0]['content'] = json.dumps(result.pop('toolUseResult'))
        unrelated = {**user, 'uuid': 'intervening-input', 'promptId': 'independent',
            'timestamp': '2026-10-01T01:00:04.500Z',
            'message': {'content': 'later independent human request'}}
        notification['parentUuid'] = unrelated['uuid']
        c['main'] = [user, unrelated, notification, answer]
        c['extra'] = [(c['root']/'session'/'subagents'/'agent-creator.jsonl',
                       [child_input, tool, result])]
        return c

    def test_native_json_sidechain_resume_routes_to_original_input(self):
        with TemporaryDirectory() as directory:
            c = self.sidechain_resume(Path(directory))
            routes, usage = self.routes(c)
            self.assertEqual(len(routes), 1)
            self.assertEqual(routes[0].payload['context']['entrypoint'], 'telegram')
            self.assertEqual(routes[0].payload['effective_at'], original.AT)
            self.assertIsNone(routes[0].payload['wi'])
            self.assertIsNone(routes[0].payload['executor'])
            self.assertEqual([r.to_dict() for r in self.routes(c)[1]], [r.to_dict() for r in usage])
            self.assertNotIn('delegated creator input', str(routes[0].to_dict()))

    def test_cross_sidechain_resume_requires_unique_successful_native_proof(self):
        import json
        for fault in ('failed', 'wrong_recipient', 'duplicate_result', 'duplicate_json',
                      'oversized_result', 'foreign_root', 'missing_prompt', 'unproved_input',
                      'future_trigger', 'human_notification', 'reverse_sidechain'):
            with self.subTest(fault=fault), TemporaryDirectory() as directory:
                c = self.sidechain_resume(Path(directory))
                child, tool, result = c['extra'][0][1]
                block = result['message']['content'][0]
                if fault == 'failed': block['content'] = json.dumps({'success': False, 'resumedAgentId': 'agent-one'})
                elif fault == 'wrong_recipient': tool['message']['content'][0]['input']['recipient'] = 'foreign'
                elif fault == 'duplicate_result':
                    duplicate = deepcopy(result); duplicate['uuid'] = 'duplicate-result'
                    c['extra'][0][1].append(duplicate)
                elif fault == 'duplicate_json': block['content'] = '{"success":false,"success":true,"resumedAgentId":"agent-one"}'
                elif fault == 'oversized_result': block['content'] = ' ' * 65537 + block['content']
                elif fault == 'foreign_root': c['extra'][0] = (c['root']/'foreign'/'subagents'/'agent-creator.jsonl', c['extra'][0][1])
                elif fault == 'missing_prompt': child['promptId'] = 'unregistered-prompt'
                elif fault == 'unproved_input': c['main'][0]['message']['content'] = 'bare input'
                elif fault == 'future_trigger': result['timestamp'] = '2026-10-01T01:00:06Z'
                elif fault == 'human_notification': c['main'][2].pop('origin')
                elif fault == 'reverse_sidechain': c['main'][2]['agentId'] = 'creator'
                self.assertEqual(self.routes(c)[0], [])


if __name__ == '__main__':
    unittest.main()
