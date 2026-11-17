"""The compact actor view never changes a report, publication or collection.

One synthetic ledger holds the shapes that fold: equal actor heads differing
only in evidence, executor disputes, revision chains and concurrent heads,
tenant, input and subcall contexts, long native input routes, Host dispatch
identity and a task claim on an actor head itself. Every output is compared
byte for byte with the fold on and off. Isolated fixtures only.
"""
from contextlib import redirect_stdout
from datetime import datetime, timezone
import io
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'Stats/exporter'))

from pulse_compute_cost import receipt_sources
from pulse_compute_cost.actor_fold import ACTOR_FOLD_ENV, fold_actor_heads
from pulse_compute_cost.cli import main as cli_main
from pulse_compute_cost.collaboration_query import (CollaborationQuery, CollaborationQuerySession,
                                                     query_collaboration)
from pulse_compute_cost.ledger import UsageLedger
from pulse_compute_cost.receipt_sources import actor_receipt as stable_actor_receipt
from pulse_compute_cost.receipt_sources import collect_registered_sources, usage_receipt
from pulse_compute_cost.receipts import Receipt, canonical, current_receipts, reference
from pulse_compute_cost.stats_dashboard import DEFAULT_FACTS, build_dashboard

AT = '2026-09-15T01:00:00Z'
LATER = '2026-09-15T01:05:00Z'
END = '2026-09-15T02:00:00Z'
NOW = datetime(2026, 9, 15, 4, tzinfo=timezone.utc)
RECORDED = datetime(2026, 9, 15, 3, tzinfo=timezone.utc)
WI_A, WI_B, WI_C, WI_D = 'wi_20260915_001', 'wi_20260915_002', 'wi_20260915_003', 'wi_20260915_004'
RRA = 'registered_runtime_actor.v1'
CATALOG = {'retrieved_at': AT, 'catalog': {'data': [{'id': 'openai/test', 'pricing': {
    'prompt': '1', 'completion': '1', 'input_cache_read': '0.1', 'input_cache_write': '2'}}]}}
PI = {'input': 100, 'cacheRead': 20, 'cacheWrite': 0, 'output': 10}
QUERIES = [
    CollaborationQuery(),
    CollaborationQuery(group_by=('agent',)),
    CollaborationQuery(work_items=(WI_A,), group_by=('agent', 'node', 'entrypoint')),
    CollaborationQuery(work_items=(WI_B,)),
    CollaborationQuery(work_items=(WI_C,), group_by=('tenant',)),
    CollaborationQuery(from_date='2026-09-15', to_date='2026-09-16', timezone='UTC', scope='activity',
                       group_by=('agent', 'node', 'runtime', 'entrypoint', 'tenant')),
    CollaborationQuery(from_date='2026-09-15', to_date='2026-09-16', timezone='UTC', scope='activity',
                       node='tcloud', group_by=('wi',)),
    CollaborationQuery(from_date='2026-09-15', to_date='2026-09-16', timezone='UTC', scope='activity',
                       entrypoint='feishu', group_by=('agent',)),
]


def refs(label, count):
    return tuple(sorted(reference('evidence', [label, i]) for i in range(count)))


def common(wi=None):
    return {'wi': wi, 'executor': None, 'responsible': None, 'delegator': None}


def pi_usage(name, wi=None, at=AT, **kwargs):
    return usage_receipt('pi', ['fold', name], PI, at=at, observed=at, model='openai/test',
                         mapping='pi_native.v1', common=common(wi), **kwargs)


def actor_receipt(target, **kwargs):
    """A head as the previous producer wrote it: evidence and time in its identity.

    The ledgers this view reads still hold these heads; the current producer
    no longer writes them (see actor_identity).
    """
    row = stable_actor_receipt(target, **kwargs)
    p = row.payload
    identity = [p['target_ref'], p['executor'], p['context'], p['effective_at'], sorted(row.evidence)]
    if p.get('usage_fact_ref') is not None:
        identity.append(p['usage_fact_ref'])
    return Receipt.make('attribution', ['runtime_actor', identity], p, occurred_at=row.occurred_at,
                        observed_at=row.observed_at, producer=row.producer, evidence=row.evidence)


def revision(row, *, evidence=(), observed=LATER, payload=None, supersedes=None):
    """Same entity; same fact identity unless the payload or predecessor differs."""
    return Receipt.make(row.kind, None, payload or row.payload, entity_ref=row.entity_ref,
                        occurred_at=row.occurred_at, observed_at=observed, producer=row.producer,
                        evidence=evidence, supersedes=supersedes)


def input_route(target, node, channel, evidence, when=AT):
    """The long-evidence native input route (cc_input_context) shape."""
    return Receipt.make('attribution', ['cc_native_input_route', target.entity_ref, evidence[0]], {
        'target_ref': target.entity_ref, 'wi': None, 'executor': None, 'responsible': None, 'delegator': None,
        'state': 'unassigned', 'policy': RRA, 'effective_at': when, 'scope': 'actor',
        'context': {'node': node, 'entrypoint': channel, 'workspace_ref': None}},
        occurred_at=when, observed_at=when, producer=RRA, evidence=evidence)


def history():
    """Batches of facts in ledger order."""
    batches = []
    # Unattended Pi path: one target, many heads differing only in evidence.
    automation = pi_usage('automation')
    batches.append([automation] + [
        actor_receipt(automation, node='tcloud', executor='pulse', entrypoint='automation',
                      observed=AT, evidence=refs(f'automation-{i}', 3)) for i in range(12)])
    # Executor dispute, both sides repeated.
    disputed = pi_usage('disputed', WI_A)
    batches.append([disputed]
        + [actor_receipt(disputed, node='tcloud', executor='pulse', entrypoint='feishu', observed=AT,
                         evidence=refs(f'pulse-{i}', 2)) for i in range(5)]
        + [actor_receipt(disputed, node='tcloud', executor='aki', entrypoint='feishu', observed=AT,
                         evidence=refs(f'aki-{i}', 2)) for i in range(4)])
    # Tenant membership bound to the exact usage version.
    tenanted = usage_receipt('codex', ['fold', 'tenant'], {
        'input_tokens': 120, 'cached_input_tokens': 20, 'cache_write_input_tokens': 0, 'output_tokens': 10},
        at=AT, observed=AT, model='openai/test', mapping='codex_cli.v1', common=common(WI_C))
    tenant = {'id': 'acme', 'name': 'Acme', 'binding_ref': reference('binding', 'acme'),
              'registry_ref': reference('registry', 'acme'), 'usage_fact_ref': tenanted.fact_ref}
    batches.append([tenanted] + [
        actor_receipt(tenanted, node='ownermac', executor='pulse', entrypoint='codex_app', observed=AT,
                      tenant=tenant, evidence=refs(f'tenant-{i}', 4)) for i in range(3)])
    # Authenticated Host input on a Pi request, final turn.
    routed = pi_usage('routed', WI_A, at=LATER)
    input_ref = reference('input', 'routed')
    batches.append([routed] + [
        actor_receipt(routed, node='tcloud', executor='pulse', entrypoint='feishu', observed=LATER,
                      input_ref=input_ref, turn_final=True, evidence=refs(f'input-{i}', 3)) for i in range(3)])
    # Subcall context on a child request.
    child = pi_usage('child', WI_A, native_scope_ref=reference('scope', 'child'))
    subcall = {'parent_scope_ref': reference('scope', 'parent'), 'child_scope_ref': reference('scope', 'child'),
               'call_ref': reference('call', 'child'), 'parent_runtime': 'codex', 'child_runtime': 'pi',
               'basis': 'native_tool_result'}
    batches.append([child] + [
        actor_receipt(child, node='tcloud', executor='pulse', entrypoint='automation', observed=AT,
                      subcall=subcall, evidence=refs(f'subcall-{i}', 2)) for i in range(2)])
    # Native CC message routed by a long input chain.
    message = usage_receipt('cc', ['fold', 'cc-message'], {
        'input_tokens': 50, 'output_tokens': 5, 'cache_read_input_tokens': 0, 'cache_creation_input_tokens': 0},
        at=AT, observed=AT, model='openai/test', mapping='cc_message.v1', common=common(WI_B))
    batches.append([message] + [input_route(message, 'ownermac', 'telegram', refs(f'route-{i}', 150 + i))
                                for i in range(4)])
    # Host dispatch: identity on the execution covers its requests.
    execution = Receipt.make('execution', ['fold', 'execution'], {
        **common(WI_B), 'runtime': 'pi', 'assignment_ref': None, 'parent_ref': None,
        'state': 'completed', 'expected_usage': True},
        occurred_at=AT, observed_at=END, producer='relay_host.v1')
    request = pi_usage('dispatched', WI_B, at=LATER, execution_ref=execution.entity_ref)
    batches.append([execution, request] + [
        actor_receipt(execution, node='tcloud', executor='aki', entrypoint='telegram', observed=AT,
                      evidence=refs(f'dispatch-{i}', 2)) for i in range(3)])
    # Revision chain of one actor head: evidence merge, then a new assertion.
    revised = pi_usage('revised')
    base = actor_receipt(revised, node='tcloud', executor='pulse', entrypoint='automation', observed=AT,
                         evidence=refs('revised', 1))
    batches.append([revised, base, actor_receipt(revised, node='tcloud', executor='pulse',
                                                  entrypoint='automation', observed=AT,
                                                  evidence=refs('revised-twin', 1))])
    batches.append([revision(base, evidence=refs('revised', 2))])  # The ledger supersedes base.
    # Concurrent heads of one actor entity remain a revision conflict.
    forked = pi_usage('forked')
    head = actor_receipt(forked, node='tcloud', executor='pulse', entrypoint='automation', observed=AT,
                         evidence=refs('forked', 1))
    other = revision(head, payload={**head.payload, 'executor': 'aki'}, evidence=refs('forked-other', 1))
    batches.append([forked, head, other] + [
        actor_receipt(forked, node='tcloud', executor='pulse', entrypoint='automation', observed=AT,
                      evidence=refs(f'forked-{i}', 1)) for i in range(2)])
    # A task claim whose target is an actor head itself, and a repeated
    # non-actor claim of the actor producer: neither may be folded away.
    claimed = batches[0][3]
    batches.append([Receipt.make('attribution', ['fold', 'claim-on-actor'], {
        'target_ref': claimed.entity_ref, 'wi': WI_D, 'executor': 'pulse', 'responsible': None,
        'delegator': None, 'state': 'assigned', 'policy': 'test_explicit_task.v1', 'effective_at': AT},
        occurred_at=AT, observed_at=LATER, producer='test')] + [
        Receipt.make('attribution', ['fold', 'bound', i], {
            'target_ref': automation.entity_ref, 'wi': WI_C, 'executor': 'pulse', 'responsible': None,
            'delegator': None, 'state': 'assigned', 'policy': RRA, 'effective_at': AT,
            'usage_fact_ref': automation.fact_ref}, occurred_at=AT, observed_at=LATER, producer=RRA)
        for i in range(2)])
    # Unassigned heads without evidence; the representative keeps the evidence flag.
    bare = pi_usage('bare')
    def unproven(evidence):
        return actor_receipt(bare, node='tcloud', executor=None, entrypoint='feishu', observed=AT,
                             input_ref=reference('input', 'bare'), turn_final=True, evidence=evidence)
    # The head without evidence sorts first in its group.
    first = unproven(())
    proven = next(row for row in (unproven(refs(f'bare-{i}', 1)) for i in range(64))
                  if row.entity_ref > first.entity_ref)
    batches.append([bare, first, proven])
    return batches


class ActorFoldEquivalenceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.ledger = UsageLedger(self.root / 'ledger', clock=lambda: RECORDED)
        for batch in history():
            self.ledger.ingest_receipts(batch)

    def fold_env(self, enabled):
        return mock.patch.dict(os.environ, {ACTOR_FOLD_ENV: '1' if enabled else '0'})

    def test_fixture_actually_folds_and_keeps_claimed_heads(self):
        facts = self.ledger.receipt_snapshot()['facts']
        active, conflicts = current_receipts(facts)
        folded, members = fold_actor_heads(active, (r for rs in conflicts.values() for r in rs))
        self.assertLess(len(folded), len(active) - 20)
        self.assertTrue(conflicts)
        claimed = history()[0][3]
        self.assertIn(claimed.fact_ref, {r.fact_ref for r in folded})
        self.assertNotIn(claimed.fact_ref, {ref for group in members.values() for ref in group})
        kept = {r.fact_ref for r in folded}
        dropped = {r.fact_ref for r in active} - kept
        self.assertEqual(dropped, {ref for rep, group in members.items() for ref in group if ref != rep})
        self.assertTrue(all(rep in kept for rep in members))

    def test_input_links_see_the_same_heads(self):
        from pulse_compute_cost.host_native_input import authenticated_input_links
        from pulse_compute_cost.native_inventory import resolved_session_inferences
        active, conflicts = current_receipts(self.ledger.receipt_snapshot()['facts'])
        folded, _ = fold_actor_heads(active, (r for rs in conflicts.values() for r in rs))
        links, finals = authenticated_input_links(active)
        self.assertIn(history()[-1][0].entity_ref, finals)
        self.assertEqual(authenticated_input_links(folded), (links, finals))
        self.assertEqual(resolved_session_inferences(folded), resolved_session_inferences(active))

    def test_collaboration_queries_and_sessions_are_byte_identical(self):
        snapshot = self.ledger.receipt_snapshot()
        for query in QUERIES:
            with self.subTest(query=query):
                on = query_collaboration(self.ledger, query, catalog=CATALOG['catalog'], catalog_observed_at=AT,
                                         fold_actors=True)
                off = query_collaboration(self.ledger, query, catalog=CATALOG['catalog'],
                                          catalog_observed_at=AT, fold_actors=False)
                self.assertEqual(canonical(on), canonical(off))
        sessions = [CollaborationQuerySession(snapshot, fold_actors=flag) for flag in (True, False)]
        for query in QUERIES:
            query = CollaborationQuery(**{**query.__dict__, 'watermark': snapshot['watermark']})
            on, off = (s.query(query, catalog=CATALOG['catalog'], catalog_observed_at=AT) for s in sessions)
            self.assertEqual(canonical(on), canonical(off))
        evidence = query_collaboration(self.ledger, QUERIES[0], fold_actors=True)['actor_evidence']
        listed = {ref for values in evidence.values() for ref in values}
        automation = history()[0]
        self.assertLessEqual({r.fact_ref for r in automation[1:]}, listed)

    def test_environment_switch_controls_the_default(self):
        with self.fold_env(True):
            on = query_collaboration(self.ledger, QUERIES[1])
        with self.fold_env(False), mock.patch('pulse_compute_cost.collaboration_query.fold_actor_heads') as fold:
            off = query_collaboration(self.ledger, QUERIES[1])
            fold.assert_not_called()
        self.assertEqual(canonical(on), canonical(off))

    def test_wi_report_command_is_byte_identical(self):
        outputs = []
        for enabled in (True, False):
            for wi in (WI_A, WI_B, WI_C):
                stream = io.StringIO()
                with self.fold_env(enabled), redirect_stdout(stream):
                    code = cli_main(['wi-report', wi, '--collaboration', '--no-auto-discover', '--no-auto-price',
                                     '--ledger-root', str(self.root / 'ledger')])
                self.assertEqual(code, 0)
                outputs.append(stream.getvalue())
        self.assertEqual(outputs[:3], outputs[3:])

    def test_stats_publication_is_byte_identical(self):
        subscriptions = json.loads(DEFAULT_FACTS.read_text())
        documents = []
        for enabled in (True, False):
            with self.fold_env(enabled):
                documents.append(canonical(build_dashboard(
                    self.ledger, subscriptions=subscriptions, source_revision='a' * 40, now=NOW,
                    catalog_snapshot=CATALOG, identity_dimensions=True, activity_table=True,
                    tenant_identity=True)))
        self.assertEqual(documents[0], documents[1])
        self.assertIn(b'"status":"available"', documents[0])

    def test_collection_batch_and_ingest_are_byte_identical(self):
        registry = {'schema': 'computecost_source_registry/v1', 'sources': [
            {'id': 'missing-harness', 'kind': 'harness_root', 'path': str(self.root / 'absent')}]}

        class Frozen(datetime):
            @classmethod
            def now(cls, tz=None):
                return NOW

        results = []
        for enabled in (True, False):
            root = self.root / f'collect-{enabled}'
            shutil.copytree(self.root / 'ledger', root)
            ledger = UsageLedger(root, clock=lambda: RECORDED)
            with self.fold_env(enabled), mock.patch.object(receipt_sources, 'datetime', Frozen):
                result = collect_registered_sources(registry, ledger)
            files = {p.name: p.read_bytes() for p in sorted((root / 'receipts').glob('*.json'))}
            results.append((canonical(result), files))
        self.assertEqual(results[0], results[1])


if __name__ == '__main__':
    unittest.main()
