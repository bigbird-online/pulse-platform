"""A runtime-notification attempt inherits its declared parent dispatch route."""
import unittest

from pulse_compute_cost.receipts import Receipt, reference
from pulse_compute_cost.receipt_sources import _continuation_routes, actor_receipt, usage_receipt

OBSERVED = '2026-10-17T05:00:00Z'
SCOPE = reference('native_session', ['cc', 'session-one'])


def execution(name, at, *, parent=None, executor='hestia', runtime='cc'):
    return Receipt.make('execution', ['host', name], {
        'wi': None, 'executor': executor, 'responsible': None, 'delegator': None, 'runtime': runtime,
        'assignment_ref': None, 'parent_ref': parent, 'state': 'accepted', 'expected_usage': True},
        occurred_at=at, observed_at=OBSERVED, producer='relay_host.v1', entity_ref=reference('attempt', name))


def usage(name, attempt, at, scope=SCOPE):
    return usage_receipt('cc', ['usage', name], {'input_tokens': 1, 'output_tokens': 1}, at=at, observed=OBSERVED,
                         model='claude', execution_ref=attempt.entity_ref, mapping='cc_message.v1',
                         native_scope_ref=scope, interval=(at, at))


class ContinuationRouteTests(unittest.TestCase):
    def setUp(self):
        self.parent = execution('dispatch', '2026-10-17T04:00:00Z')
        self.child = execution('notice', '2026-10-17T04:20:00Z', parent=self.parent.entity_ref)
        self.route = actor_receipt(self.parent, node='ownermac', executor='hestia', entrypoint='telegram',
                                   observed=OBSERVED, evidence=(reference('input_metadata', 'x'),), input_ref=reference('input_metadata', 'x'))
        self.rows = [self.parent, self.child, usage('a', self.parent, '2026-10-17T04:00:05Z'),
                     usage('b', self.child, '2026-10-17T04:20:05Z')]

    def routes(self, rows=None, executions=None):
        rows = rows or self.rows
        executions = executions or {r.entity_ref: r for r in rows if r.kind == 'execution'}
        return _continuation_routes([self.route], executions, rows, {'node': 'ownermac'}, OBSERVED)

    def test_child_carries_parent_route_and_input(self):
        [route] = self.routes()
        p = route.payload
        self.assertEqual((p['target_ref'], p['executor']), (self.child.entity_ref, 'hestia'))
        self.assertEqual(p['context']['entrypoint'], 'telegram')
        self.assertEqual(p['context']['input_ref'], self.route.payload['context']['input_ref'])
        self.assertIn(self.route.fact_ref, route.evidence)

    def test_different_executor_runtime_or_session_stays_unknown(self):
        other = execution('notice', '2026-10-17T04:20:00Z', parent=self.parent.entity_ref, executor='pulse')
        self.assertEqual(self.routes([self.parent, other, *self.rows[2:]]), [])
        codex = execution('notice', '2026-10-17T04:20:00Z', parent=self.parent.entity_ref, runtime='codex')
        self.assertEqual(self.routes([self.parent, codex, *self.rows[2:]]), [])
        elsewhere = usage('b', self.child, '2026-10-17T04:20:05Z', reference('native_session', ['cc', 'other']))
        self.assertEqual(self.routes([*self.rows[:3], elsewhere]), [])

    def test_no_parent_or_unproved_parent_gives_nothing(self):
        orphan = execution('notice', '2026-10-17T04:20:00Z')
        self.assertEqual(self.routes([self.parent, orphan, *self.rows[2:]]), [])
        early = execution('notice', '2026-10-17T03:00:00Z', parent=self.parent.entity_ref)
        self.assertEqual(self.routes([self.parent, early, *self.rows[2:]]), [])
        self.assertEqual(_continuation_routes([], {r.entity_ref: r for r in self.rows if r.kind == 'execution'},
                                              self.rows, {'node': 'ownermac'}, OBSERVED), [])


class DispatchDeclaredIdentityTests(unittest.TestCase):
    """A native twin a few milliseconds before dispatch keeps the Host route."""

    def project(self, effective):
        from dataclasses import replace
        from pulse_compute_cost.collaboration_query import _actor_projection
        child = execution('notice', '2026-10-17T09:48:16.220Z')
        merged = usage('b', child, '2026-10-17T09:48:16.217Z')
        merged = replace(merged, _payload_json=__import__('json').dumps(
            {**merged.payload, 'interval_end': '2026-10-17T09:48:16.235Z'}, sort_keys=True, separators=(',', ':')))
        route = actor_receipt(child, node='ownermac', executor='hestia', entrypoint='telegram', observed=OBSERVED)
        if effective != route.payload['effective_at']:
            p = {**route.payload, 'effective_at': effective}
            route = Receipt.make('attribution', ['moved', effective], p, occurred_at=effective,
                                 observed_at=OBSERVED, producer=route.producer)
        _, contexts, _, conflicts = _actor_projection([merged], [child, route], {})
        return contexts[merged.entity_ref]['entrypoint'], conflicts

    def test_dispatch_route_covers_an_earlier_native_twin(self):
        self.assertEqual(self.project('2026-10-17T09:48:16.220Z'), ('telegram', {}))

    def test_route_declared_after_dispatch_is_still_a_transfer(self):
        entry, conflicts = self.project('2026-10-17T09:48:16.230Z')
        self.assertEqual(entry, 'ambiguous')
        self.assertTrue(conflicts)
