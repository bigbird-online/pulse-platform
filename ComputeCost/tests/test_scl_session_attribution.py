"""No prefilled task receipts: native logs + a synthetic existing SCL kernel go in."""
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest

from pulse_compute_cost.collaboration_query import CollaborationQuery, query_collaboration
from pulse_compute_cost.ledger import UsageLedger
from pulse_compute_cost.receipt_sources import collect_registered_sources, read_codex
from pulse_compute_cost.receipts import canonical, reference
from pulse_compute_cost.scl_session_attribution import codex_host_ref, POLICY
from pulse_compute_cost.source_registration import actor_candidate_registry

THREAD = '00000000-0000-4000-8000-000000000011'
SECOND = '00000000-0000-4000-8000-000000000012'
WI = 'wi_20260101_101'
OTHER_WI = 'wi_20260101_102'
BEFORE = '2026-01-01T00:00:00Z'
START = '2026-01-01T01:00:00Z'
END = '2026-01-01T01:01:00Z'
AFTER = '2026-01-01T02:00:00Z'
SURFACE = 'surface_v1_test_macos'
CATALOG = {'data': [{'id': 'openai/test', 'pricing': {
    'prompt': '1', 'completion': '1', 'input_cache_read': '0.1', 'input_cache_write': '2'}}]}


class SessionAttributionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.kernel = self.root / 'kernel'; self.kernel.mkdir()
        self.lineage_id = 'lineage_v1_' + 'a' * 64
        self.active = self.kernel / 'lineages' / self.lineage_id; self.active.mkdir(parents=True)
        self.pointer = self.kernel / 'active-lineage.json'
        self.pointer.write_bytes(canonical({'schema_version': 'scl_active_lineage/v1',
            'lineage_id': self.lineage_id, 'kernel_path': 'lineages/' + self.lineage_id,
            'generation': 1, 'runtime_revision': 'a' * 40, 'retired_manifest': 'retired/old.json',
            'activated_at': BEFORE}))
        self.database = self.active / 'kernel.sqlite3'
        self.db = sqlite3.connect(self.database); self.addCleanup(self.db.close)
        self.db.executescript('''
            CREATE TABLE work_items (work_item_id TEXT PRIMARY KEY, state_json TEXT, updated_at TEXT);
            CREATE TABLE source_attachments (work_item_id TEXT, epoch INTEGER, logical_source_ref TEXT,
                host_session_ref TEXT, surface_id TEXT, rationale_digest TEXT, operation_id TEXT, created_at TEXT);
            CREATE TABLE source_attachment_history (work_item_id TEXT, epoch INTEGER, logical_source_ref TEXT,
                host_session_ref TEXT, surface_id TEXT, rationale_digest TEXT, operation_id TEXT, created_at TEXT,
                superseded_at TEXT, superseded_by_operation_id TEXT);
            CREATE TABLE measurement_epochs (work_item_id TEXT, execution_epoch INTEGER, host_session_ref TEXT,
                surface_id TEXT, agent_id TEXT, attribution_json TEXT, attribution_digest TEXT);
            CREATE TABLE checkpoints (work_item_id TEXT, epoch INTEGER, revision INTEGER,
                content_json TEXT, content_digest TEXT);
        ''')
        self.logs = self.root / 'logs'; self.logs.mkdir()
        self.native(THREAD, START, END)
        self.bind(WI, THREAD, BEFORE)
        self.source = {'id': 'codex', 'kind': 'codex', 'path': str(self.logs),
            'actor_attribution': True, 'node': 'testmac',
            'workspace_actors': [{'workspace_root': '/registered/local-space', 'agent': 'pulse',
                'node': 'testmac', 'runtime': 'codex', 'registry_ref': reference('registry', 'fixture')}],
            'scl_kernel_root': str(self.kernel), 'scl_surface_id': SURFACE}
        self.registry = {'schema': 'computecost_source_registry/v1', 'sources': [self.source]}
        self.ledger = UsageLedger(self.root / 'ledger', clock=lambda: datetime(2026, 1, 2, tzinfo=timezone.utc))

    def native(self, session, start, end):
        path = self.logs / ('rollout-2026-01-01T01-00-00-' + session + '.jsonl')
        totals = {'input_tokens': 120, 'cached_input_tokens': 20, 'cache_write_input_tokens': 0, 'output_tokens': 10}
        rows = [
            {'type': 'session_meta', 'timestamp': start, 'payload': {'id': session,
                'cwd': '/registered/local-space', 'originator': 'Codex Desktop', 'source': 'vscode'}},
            {'type': 'event_msg', 'timestamp': start, 'payload': {'type': 'task_started', 'turn_id': 'turn-' + session}},
            {'type': 'turn_context', 'timestamp': start, 'payload': {'model': 'openai/test'}},
            {'type': 'event_msg', 'timestamp': end, 'payload': {'type': 'token_count',
                'info': {'total_token_usage': totals, 'last_token_usage': totals}}},
            {'type': 'event_msg', 'timestamp': end, 'payload': {'type': 'task_complete'}},
        ]
        path.write_text(''.join(json.dumps(row) + '\n' for row in rows))
        return path

    def bind(self, wi, session, at, epoch=1, *, agent='pulse', until=None, surface=SURFACE):
        host = codex_host_ref(session)
        state = {'work_item_id': wi, 'active_executor': agent, 'epoch': epoch,
                 'session_ref': host, 'surface_id': surface, 'lifecycle_status': 'in_progress'}
        self.db.execute('INSERT OR REPLACE INTO work_items VALUES (?,?,?)', (wi, json.dumps(state), at))
        values = (wi, epoch, host, host, surface, 'a' * 64, 'operation-' + session + str(epoch), at)
        if until:
            self.db.execute('INSERT INTO source_attachment_history VALUES (?,?,?,?,?,?,?,?,?,?)', (*values, until, 'replacement'))
        else:
            self.db.execute('INSERT INTO source_attachments VALUES (?,?,?,?,?,?,?,?)', values)
        cp = canonical({'work_item_id': wi, 'epoch': epoch, 'agent_id': agent, 'surface_id': surface}).decode()
        self.db.execute('INSERT INTO checkpoints VALUES (?,?,?,?,?)', (wi, epoch, 1, cp, hashlib.sha256(cp.encode()).hexdigest()))
        self.db.commit()

    def query(self, **kwargs):
        return query_collaboration(self.ledger, CollaborationQuery(from_date='2026-01-01',
            to_date='2026-01-02', scope='activity', timezone='UTC', group_by=('wi', 'agent'), **kwargs), catalog=CATALOG)

    def collect(self):
        collect_registered_sources(self.registry, self.ledger)
        return self.query()

    def test_raw_native_to_active_lineage_to_existing_query(self):
        before = self.database.read_bytes()
        result = self.collect()
        self.assertEqual(result['summary']['usage']['wi_unassigned_event_count'], 0)
        self.assertEqual(result['groups'][0]['key'], {'wi': WI, 'agent': 'pulse'})
        self.assertEqual(result['summary']['usage']['known_total_tokens'], 130)
        self.assertEqual(result['summary']['cost']['partial_machine_cash_usd'], '112')
        self.assertEqual(self.database.read_bytes(), before)
        facts = self.ledger.receipt_snapshot()['facts']
        self.assertIsNone(next(r for r in facts if r.kind == 'usage').payload['wi'])
        self.assertTrue(any(r.producer == POLICY and r.payload['wi'] == WI for r in facts))

    def test_cached_codex_bytes_still_recheck_real_scl_bindings(self):
        from unittest.mock import patch
        from pulse_compute_cost import scl_session_attribution as scl
        self.ledger.root.mkdir(mode=0o700)
        first = self.collect()
        with patch('pulse_compute_cost.receipt_sources.collect_usage_events', side_effect=AssertionError('unchanged native parser called')), \
             patch.object(scl, 'attribution_receipts', wraps=scl.attribution_receipts) as bindings:
            second = self.collect()
        self.assertEqual(bindings.call_count, 1)
        self.assertEqual(first, second)
        with patch('pulse_compute_cost.receipt_sources.collect_usage_events', wraps=__import__(
                'pulse_compute_cost.adapters', fromlist=['collect_usage_events']).collect_usage_events) as parser:
            collect_registered_sources(self.registry, self.ledger, force_recompute=True)
        self.assertEqual(parser.call_count, 1)
        self.assertEqual(first, self.query())

    def test_exactly_same_hash_as_the_existing_codex_hook_protocol(self):
        sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'agent-harness'))
        try:
            from harness_rewrite.session_sources import old_session_ref
        finally:
            sys.path.pop(0)
        self.assertEqual(codex_host_ref(THREAD), old_session_ref('codex', THREAD))
        self.assertNotEqual(codex_host_ref(THREAD), 'session_v1_' + hashlib.sha256(b'pulse-session-v1\0' + THREAD.encode()).hexdigest())

    def test_old_root_database_and_stale_legacy_binding_are_never_fallbacks(self):
        (self.kernel / 'kernel.sqlite3').write_bytes(b'old root is not a database')
        self.assertEqual(self.collect()['groups'][0]['key']['wi'], WI)
        self.pointer.unlink()
        rows = read_codex(self.logs, END, source_identity=self.source, diagnostic_out=[])
        self.assertFalse(any(r.producer == POLICY for r in rows))
        self.assertEqual(next(r for r in rows if r.kind == 'usage').payload['reported_total_tokens'], 130)

    def test_unrelated_codex_session_is_not_assigned_to_the_only_active_wi(self):
        self.native(SECOND, START, END)
        result = self.collect()
        self.assertEqual(result['summary']['usage']['wi_unassigned_event_count'], 1)
        self.assertEqual(result['summary']['usage']['agent_unassigned_event_count'], 0)

    def test_attribution_does_not_backfill_before_attachment(self):
        self.db.execute('UPDATE source_attachments SET created_at=?', (AFTER,)); self.db.commit()
        self.assertEqual(self.collect()['summary']['usage']['wi_unassigned_event_count'], 1)

    def test_mid_turn_attachment_remains_ambiguous_without_splitting_tokens(self):
        self.db.execute('UPDATE source_attachments SET created_at=?', ('2026-01-01T01:00:30Z',)); self.db.commit()
        result = self.collect()
        self.assertEqual(result['groups'][0]['key']['wi'], 'ambiguous')
        self.assertEqual(result['summary']['usage']['known_total_tokens'], 130)

    def test_same_epoch_rebind_keeps_old_host_history(self):
        self.db.execute('DELETE FROM source_attachments'); self.db.commit()
        self.bind(WI, THREAD, BEFORE, until=AFTER)
        self.bind(WI, SECOND, AFTER)
        self.assertEqual(self.collect()['groups'][0]['key']['wi'], WI)

    def test_later_epoch_closes_the_previous_session_interval(self):
        self.bind(WI, SECOND, '2026-01-01T00:30:00Z', epoch=2)
        self.assertEqual(self.collect()['summary']['usage']['wi_unassigned_event_count'], 1)

    def test_two_work_items_attached_to_same_host_do_not_become_last_writer_wins(self):
        self.bind(OTHER_WI, THREAD, BEFORE)
        self.assertEqual(self.collect()['groups'][0]['key']['wi'], 'ambiguous')

    def test_other_surface_cannot_supply_identity(self):
        self.source['scl_surface_id'] = 'surface_v1_other'
        self.assertEqual(self.collect()['summary']['usage']['wi_unassigned_event_count'], 1)

    def test_invalid_pointer_and_checkpoint_preserve_usage_and_actor(self):
        self.db.execute("UPDATE checkpoints SET content_digest='broken'"); self.db.commit()
        diagnostics = []
        rows = read_codex(self.logs, END, source_identity=self.source, diagnostic_out=diagnostics)
        self.assertIn('scl_session_binding_unavailable', diagnostics)
        self.assertFalse(any(r.producer == POLICY for r in rows))
        self.assertEqual(next(r for r in rows if r.kind == 'usage').payload['reported_total_tokens'], 130)
        self.assertEqual(next(r for r in rows if r.kind == 'attribution').payload['executor'], 'pulse')

    def test_collection_is_idempotent_and_old_watermark_remains_queryable(self):
        legacy = {k: v for k, v in self.source.items() if not k.startswith('scl_')}
        collect_registered_sources({**self.registry, 'sources': [legacy]}, self.ledger)
        old = self.ledger.receipt_snapshot()
        old_usage = [r.to_dict() for r in old['facts'] if r.kind == 'usage']
        self.assertEqual(self.query()['summary']['usage']['wi_unassigned_event_count'], 1)
        self.assertEqual(self.collect()['summary']['usage']['wi_unassigned_event_count'], 0)
        self.assertEqual(collect_registered_sources(self.registry, self.ledger)['inserted'], 0)
        self.assertEqual(self.ledger.receipt_snapshot(watermark=old['watermark'])['sha256'], old['sha256'])
        self.assertEqual([r.to_dict() for r in self.ledger.receipt_snapshot()['facts'] if r.kind == 'usage'], old_usage)

    def test_same_wi_rollover_boundary_does_not_make_task_unknown(self):
        self.bind(WI, THREAD, '2026-01-01T01:00:30Z', epoch=2)
        self.assertEqual(self.collect()['groups'][0]['key']['wi'], WI)

    def test_gap_between_attachments_is_not_assumed_to_be_the_same_task(self):
        self.db.execute('DELETE FROM source_attachments'); self.db.commit()
        self.bind(WI, THREAD, BEFORE, until='2026-01-01T01:00:20Z')
        self.bind(WI, THREAD, '2026-01-01T01:00:40Z', epoch=2)
        self.assertEqual(self.collect()['groups'][0]['key']['wi'], 'ambiguous')

    def test_host_specific_measurement_identity_does_not_require_token_seal(self):
        self.db.execute('DELETE FROM checkpoints')
        a = canonical({'work_item_id': WI, 'execution_epoch': 1, 'agent_id': 'pulse'}).decode()
        self.db.execute('INSERT INTO measurement_epochs VALUES (?,?,?,?,?,?,?)',
            (WI, 1, codex_host_ref(THREAD), SURFACE, 'pulse', a, hashlib.sha256(a.encode()).hexdigest()))
        self.db.commit()
        self.assertEqual(self.collect()['groups'][0]['key']['wi'], WI)

    def test_terminal_boundary_prevents_later_reuse_from_inheriting_old_wi(self):
        state = json.loads(self.db.execute('SELECT state_json FROM work_items').fetchone()[0])
        state['lifecycle_status'] = 'completed'
        self.db.execute('UPDATE work_items SET state_json=?,updated_at=?',
            (json.dumps(state), '2026-01-01T00:30:00Z')); self.db.commit()
        self.assertEqual(self.collect()['summary']['usage']['wi_unassigned_event_count'], 1)

    def test_active_pointer_cannot_escape_to_another_directory(self):
        pointer = json.loads(self.pointer.read_text()); pointer['kernel_path'] = '../other'
        self.pointer.write_bytes(canonical(pointer))
        diagnostics = []
        rows = read_codex(self.logs, END, source_identity=self.source, diagnostic_out=diagnostics)
        self.assertIn('scl_session_binding_unavailable', diagnostics)
        self.assertFalse(any(r.producer == POLICY for r in rows))

    def test_paginated_physical_rollout_keeps_thread_binding_and_increment(self):
        for path in self.logs.glob('*.jsonl'):
            path.unlink()
        parent = self.logs / ('rollout-2025-12-31T01-00-00-' + THREAD + '.jsonl')
        base = {'input_tokens': 1000, 'cached_input_tokens': 900, 'cache_write_input_tokens': 0, 'output_tokens': 100}
        delta = {'input_tokens': 120, 'cached_input_tokens': 20, 'cache_write_input_tokens': 0, 'output_tokens': 10}
        rows = [{'type': 'session_meta', 'timestamp': '2025-12-31T01:00:00Z', 'ordinal': 0,
                 'payload': {'id': THREAD, 'session_id': THREAD, 'history_mode': 'paginated'}},
                {'type': 'event_msg', 'timestamp': '2025-12-31T01:01:00Z', 'ordinal': 1,
                 'payload': {'type': 'token_count', 'info': {'total_token_usage': base}}}]
        parent.write_text(''.join(json.dumps(r) + '\n' for r in rows))
        child = self.native(THREAD, START, END)
        rows = [json.loads(line) for line in child.read_text().splitlines()]
        for ordinal, row in enumerate(rows, 2):
            row['ordinal'] = ordinal
        rows[0]['payload'].update(session_id=THREAD, history_mode='paginated', history_base={
            'thread_id': THREAD, 'end_ordinal_exclusive': 2, 'end_byte_offset': parent.stat().st_size})
        rows[3]['payload']['info'] = {'total_token_usage': {k: base[k] + delta[k] for k in base}, 'last_token_usage': delta}
        child.unlink()
        segment = self.logs / ('rollout-2026-01-01T01-00-00-' + THREAD + '_' + SECOND + '.jsonl')
        segment.write_text(''.join(json.dumps(r) + '\n' for r in rows))
        result = self.collect()
        self.assertEqual(result['groups'][0]['key']['wi'], WI)
        self.assertEqual(result['summary']['usage']['known_total_tokens'], 130)

    def test_actor_replay_selects_joint_weekly_activity_not_retired_marginals(self):
        import importlib.util
        path = Path(__file__).resolve().parents[1] / 'uat/actor_identity_replay.py'
        spec = importlib.util.spec_from_file_location('actor_replay_view_fixture', path)
        replay = importlib.util.module_from_spec(spec); spec.loader.exec_module(replay)
        activity = {'period': 'week', 'dimension': 'activity', 'watermark': 9}
        marginal = {'period': 'week', 'dimension': 'agent', 'watermark': 8}
        self.assertIs(replay.weekly_monetary_view({'views': [marginal, activity]}), activity)
        self.assertIs(replay.weekly_monetary_view({'views': [activity]}), activity)
        with self.assertRaisesRegex(ValueError, 'weekly joint activity'):
            replay.weekly_monetary_view({'views': [marginal]})

    def test_wi_replay_keeps_identity_gap_visible_and_full_expectations_strict(self):
        import importlib.util
        path = Path(__file__).resolve().parents[1] / 'uat/actor_identity_replay.py'
        spec = importlib.util.spec_from_file_location('actor_replay_fixture', path)
        replay = importlib.util.module_from_spec(spec); spec.loader.exec_module(replay)
        key = {'agent': 'pulse', 'runtime': 'pi', 'wi': WI, 'node': 'unassigned',
               'entrypoint': 'unassigned', 'model': 'native-model'}
        groups = [{'key': key, 'usage': {'event_count': 2}}]
        expected = {'agent': 'pulse', 'runtime': 'pi', 'wi': WI}
        proof = replay.verify_task_expectations(groups, [expected])
        self.assertEqual(proof[0]['observed'][0], key)
        for changed in ({'wi': OTHER_WI}, {'node': 'tcloud'}, {'model': 'configured-model'}, {'entrypoint': 'feishu'}):
            with self.assertRaises(AssertionError):
                replay.verify_task_expectations(groups, [{**expected, **changed}])

    def test_registration_reuses_kernel_root_without_moving_legacy_sources(self):
        surfaces = {'agent_id': 'pulse', 'surfaces': [{'surface_id': SURFACE, 'workspace': 'local-space',
            'deployment': {'site': 'ownermac', 'pulse_home': '/registered', 'kernel_root': str(self.kernel)}}]}
        registry = {**self.registry, 'ledger_root': '/unchanged',
                    'sources': [{k: v for k, v in self.source.items() if not k.startswith('scl_')}]}
        # This fixture tests kernel/source retention, not an ownership move.
        registry['sources'][0]['workspace_actors'] = [
            {**rule, 'node': 'ownermac'} for rule in self.source['workspace_actors']]
        result = actor_candidate_registry(registry, surfaces, site='ownermac')
        self.assertEqual(result['sources'][0]['scl_kernel_root'], str(self.kernel))
        self.assertEqual(result['sources'][0]['scl_surface_id'], SURFACE)
        self.assertEqual(result['sources'][0]['path'], self.source['path'])
        self.assertEqual(result['ledger_root'], '/unchanged')


if __name__ == '__main__':
    unittest.main()
