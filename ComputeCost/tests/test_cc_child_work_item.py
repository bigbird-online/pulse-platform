"""A CC subagent dispatched with `[WI: <child>]` spends the child WI only when the core proves it."""
import json
import os
import unittest

from pulse_compute_cost.child_work_items import dispatch_marker, read_children
import test_cc_request_purpose as base
from test_mcp_cc_session_wi import SESSION, WI, OTHER_WI, usage

CHILD = 'wi_20260101_201'


class ChildWorkItemTests(base.RequestPurposeTests):
    def setUp(self):
        super().setUp()
        self.facts = self.root/'wi-facts-v1.json'
        self.registry['core_session_writes'] = {'path': str(self.facts), 'surface': 'surface_v1_local_macos'}

    def export(self, *items):
        self.facts.write_text(json.dumps({'schema': 'harness.stats_facts/v1', 'read_only': True,
                                          'source': 'journal', 'namespace': 'test', 'items': list(items)}))
        os.chmod(self.facts, 0o600)

    def item(self, wi, parent=WI, status='active', closed_at=None, **extra):
        return {'id': wi, 'parent': parent, 'status': status, 'created_at': '2026-01-01T00:30:00Z',
                'closed_at': closed_at, **extra}

    def child(self, wi=WI, first_line=f'[WI: {CHILD}]'):
        user, _ = self.request('a', 0, wi)
        prompt = {**user, 'uuid': 'child-input', 'parentUuid': None, 'isSidechain': True, 'agentId': 'child',
                  'timestamp': '2026-01-01T01:02:00Z', 'message': {'content': first_line+'\nprivate delegated task'}}
        answer = usage(SESSION, 25, 'child', sidechain=True); answer.update(agentId='child', parentUuid='child-input')
        self.children[self.logs/SESSION/'subagents/agent-child.jsonl'] = [prompt, answer]

    def assert_child(self, expected):
        self.collect(); states = self.states()
        self.assertEqual(states[self.target('child')], ('assigned', expected))
        self.assertEqual(states[self.target('a-after')], ('assigned', WI))  # The main request never moves.

    def test_marker_naming_an_open_child_of_the_declared_wi_takes_the_child(self):
        self.export(self.item(CHILD)); self.child()
        self.assert_child(CHILD)
        self.assertEqual(self.collect()['inserted'], 0)
        # Closing the child later does not move usage spent while it was open.
        self.export(self.item(CHILD, status='completed', closed_at='2026-01-01T03:00:00Z'))
        self.assert_child(CHILD)

    def test_a_request_declaring_a_sibling_leaves_a_marked_child_its_usage(self):
        sibling = 'wi_20260101_202'
        self.export(self.item(CHILD), self.item(sibling)); self.child(wi=sibling)
        self.collect(); states = self.states()
        self.assertEqual(states[self.target('child')], ('assigned', CHILD))
        self.assertEqual(states[self.target('a-after')], ('assigned', sibling))
        # A declared WI under another parent is no sibling: the subagent inherits it.
        self.main, self.children = [], {}
        self.export(self.item(CHILD), self.item(sibling, parent=OTHER_WI)); self.child(wi=sibling)
        self.collect(); self.assertEqual(self.states()[self.target('child')], ('assigned', sibling))

    def test_marker_naming_a_child_of_another_wi_inherits(self):
        self.export(self.item(CHILD, parent=OTHER_WI)); self.child()
        self.assert_child(WI)

    def test_marker_naming_an_ended_child_inherits(self):
        for name, item in (('closed_before', self.item(CHILD, status='completed', closed_at='2026-01-01T01:10:00Z')),
                           ('closure_unknown', self.item(CHILD, status='terminated'))):
            with self.subTest(name):
                self.main, self.children = [], {}
                self.export(item); self.child()
                self.assert_child(WI)

    def test_old_export_without_parent_or_no_export_inherits(self):
        old = self.item(CHILD); del old['parent']
        self.export(old); self.child()
        self.assert_child(WI)
        self.facts.unlink()
        self.assert_child(WI)

    def test_without_marker_or_with_a_marker_off_the_first_line_inherits(self):
        self.export(self.item(CHILD)); self.child(first_line='private delegated task')
        self.assert_child(WI)
        self.main, self.children = [], {}
        self.child(first_line=f'note\n[WI: {CHILD}]')
        self.assert_child(WI)

    def test_undeclared_or_none_request_never_takes_a_marked_child(self):
        self.export(self.item(CHILD)); self.child(wi=None); self.request('b', 30, WI)
        self.collect(); self.assertEqual(self.states()[self.target('child')], ('unassigned', None))
        self.main, self.children = [], {}
        self.child(wi='none')
        self.collect(); self.assertEqual(self.states()[self.target('child')], ('assigned', None))

    def test_marker_reader(self):
        self.assertEqual(dispatch_marker(f'[WI: {CHILD}]\nrest'), CHILD)
        self.assertIsNone(dispatch_marker(f' [WI: {CHILD}]'))
        self.assertIsNone(dispatch_marker('[WI: wi_1]'))
        self.assertEqual(read_children({'sources': []}), {})


# Reuse the transcript fixture only; its own cases run in their module.
for _name in dir(base.RequestPurposeTests):
    if _name.startswith('test_'):
        setattr(ChildWorkItemTests, _name, None)


if __name__ == '__main__':
    unittest.main()
