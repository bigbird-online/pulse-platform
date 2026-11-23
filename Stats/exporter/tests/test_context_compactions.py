"""Relay's CC compaction counts and the core's rollovers, as Stats reads them."""
from datetime import datetime, timedelta, timezone
import contextlib
import importlib.util
import io
import json
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'exporter'))
sys.path.insert(0, str(ROOT.parent / 'ComputeCost'))
from pulse_stats import context_compactions, query_cli  # noqa: E402
from pulse_compute_cost.receipts import reference  # noqa: E402

PUBLICATION = ROOT / 'exporter/tests/fixtures/published-tenant-seven-v1.json'
SESSION = '5f4add7e-4eff-4878-9c67-5c09e94f17f2'
WI = 'wi_20261118_988'


def session(ref, wi=WI, count=3, at='2026-11-18T04:00:00.000Z', by_wi=None):
    return {'session_ref': ref, 'runtime': 'cc', 'wi': wi, 'compactions': count,
            'by_wi': [{'wi': wi, 'compactions': count}] if by_wi is None else by_wi, 'context_ratio': 0.31,
            'first_compaction_at': at, 'last_compaction_at': at}


def document(*rows, updated='2026-11-18T04:00:00.000Z', started='2026-11-01T00:00:00.000Z', dropped=None,
             observed=None):
    value = {'schema': 'pulse.stats.context_compactions/v1', 'source': 'relay_cc_compact_boundary',
             'updated_at': updated, 'recording_started_at': started, 'dropped_through': dropped, 'sessions': list(rows)}
    return value if observed is None else {**value, 'observed_at': observed}


def private(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value))
    path.chmod(0o600)


class CompactionDocumentTests(unittest.TestCase):
    now = datetime(2026, 11, 18, 5, tzinfo=timezone.utc)

    def test_session_ref_is_the_hashed_scope_relay_and_computecost_use(self):
        self.assertEqual(context_compactions.session_ref(SESSION), reference('native_session', ['cc', SESSION]))
        with self.assertRaises(ValueError):
            context_compactions.session_ref('../x')

    def test_validation_rejects_raw_identities_bad_counts_and_future_documents(self):
        ref = context_compactions.session_ref(SESSION)
        self.assertTrue(context_compactions.validate(document(session(ref)), self.now))
        for bad in (document({**session(ref), 'session_id': SESSION}), document(session(ref, count=0)),
                    document(session(ref, wi='wi_bad')), document(session(ref), session(ref)),
                    document(session(ref), updated='2026-11-19T00:00:00Z'),
                    {k: v for k, v in document(session(ref)).items() if k != 'recording_started_at'},
                    document(session(ref), started='2026-11-18T05:00:00Z'),
                    document(session(ref), dropped='2026-11-18T05:00:00Z'),
                    document(session(ref), observed='2026-11-18T03:00:00Z'),
                    {**document(session(ref)), 'schema': 'other'},
                    {k: v for k, v in document(session(ref)).items() if k != 'dropped_through'},
                    document({k: v for k, v in session(ref).items() if k != 'by_wi'}),
                    document(session(ref, by_wi=[{'wi': WI, 'compactions': 2}])),
                    document(session(ref, by_wi=[{'wi': WI, 'compactions': 2}, {'wi': WI, 'compactions': 1}])),
                    document(session(ref, by_wi=[{'wi': None, 'compactions': 3, 'extra': 1}])),
                    document(session(ref, by_wi=[{'wi': 'wi_bad', 'compactions': 3}])),
                    document(session(ref, by_wi=[{'wi': WI, 'compactions': 3}, {'wi': None, 'compactions': 0}]))):
            with self.assertRaises(context_compactions.CompactionsError):
                context_compactions.validate(bad, self.now)

    def test_validation_and_coverage_agree_on_a_missing_dropped_through(self):
        ref = context_compactions.session_ref(SESSION)
        bare = {k: v for k, v in document(session(ref)).items() if k != 'dropped_through'}
        with self.assertRaises(context_compactions.CompactionsError) as caught:
            context_compactions.validate(bare, self.now)
        self.assertEqual(str(caught.exception), 'invalid_compactions_coverage')
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'c.json'
            private(path, bare)
            self.assertEqual(context_compactions.load(path, self.now), (None, 'invalid_compactions_coverage'))
        explicit = context_compactions.validate(document(session(ref)), self.now)
        self.assertEqual(context_compactions.coverage(explicit, self.now)['dropped_through'], None)

    def test_load_requires_a_private_regular_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'c.json'
            self.assertEqual(context_compactions.load(path, self.now)[1], 'compactions_source_unavailable')
            private(path, document())
            self.assertEqual(context_compactions.load(path, self.now)[1], None)
            path.chmod(0o644)
            self.assertEqual(context_compactions.load(path, self.now)[1], 'compactions_source_unavailable')


class CompactionQueryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        self.state = self.home / '.local/state/pulse/stats'
        self.publication = self.home / 'cost.json'
        shutil.copyfile(PUBLICATION, self.publication)
        self.publication.chmod(0o600)
        cost = json.loads(self.publication.read_bytes())
        self.now = datetime.fromisoformat(cost['generated_at']) + timedelta(minutes=1)
        self.app = self.home / 'snapshot.json'
        self.app.write_text('{}')
        self.ref = context_compactions.session_ref(SESSION)
        at = self.now.isoformat()
        self.started = (self.now - timedelta(days=3)).isoformat()
        self.publish()
        facts = {'schema': 'harness.stats_facts/v1', 'generated_at': at, 'read_only': True, 'source': 'journal',
                 'namespace': 'n', 'coverage': 'complete', 'unavailable_items': [], 'collaborations': [],
                 'items': [{'id': WI, 'status': 'active', 'title': 't', 'owner': 'pulse',
                            'created_at': at, 'updated_at': at,
                            'rollovers': [{'at': at, 'generation': 2, 'requested_by': 'agent'},
                                          {'at': at, 'generation': 3, 'requested_by': 'host'}]},
                           {'id': 'wi_20261118_001', 'status': 'active', 'title': 'old', 'owner': 'pulse',
                            'created_at': at, 'updated_at': at}]}
        private(self.state / 'wi-facts-v1.json', facts)

    def publish(self, *, observed=None, dropped=None, started=None):
        at = self.now.isoformat()
        private(self.state / 'context-compactions-v1.json', document(
            session(self.ref, at=at), session('ccf1_' + 'b' * 64, wi=None, count=1, at=at), updated=at,
            started=started or self.started, dropped=dropped, observed=observed or at))

    def run_query(self, *argv):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(query_cli.main([*argv, '--snapshot', str(self.app), '--cost-dashboard',
                                             str(self.publication)], now=self.now, home=self.home), 0)
        return json.loads(out.getvalue())

    def test_compactions_by_session_and_by_wi(self):
        by_session = self.run_query('compactions', '--session-id', SESSION)
        self.assertEqual(by_session['sources']['context_compactions']['status'], 'ok')
        self.assertEqual([(r['session_ref'], r['compactions']) for r in by_session['result']['sessions']],
                         [(self.ref, 3)])
        self.assertEqual(by_session['arguments'], {'session_id': SESSION})
        by_wi = self.run_query('compactions', '--work-item-id', WI)['result']
        self.assertEqual([r['wi'] for r in by_wi['sessions']], [WI])
        self.assertEqual(len(self.run_query('compactions')['result']['sessions']), 2)

    def test_wi_reports_its_rollovers_and_compacted_sessions(self):
        result = self.run_query('wi', '--work-item-id', WI)['result']
        self.assertEqual(result['rollovers']['count'], 2)
        self.assertEqual(result['rollovers']['by_agent'], 1)
        self.assertEqual([r['compactions'] for r in result['context_compactions']['sessions']], [3])
        self.assertEqual(result['context_compactions']['total_compactions'], 3)

    def test_a_wi_counts_only_the_compactions_under_it_not_a_later_relabel(self):
        # Three compactions under the old WI; a later declaration of WI B relabels the session without any new one.
        old, new = 'wi_20261118_001', WI
        at = self.now.isoformat()
        private(self.state / 'context-compactions-v1.json', document(
            session(self.ref, wi=new, count=3, at=at, by_wi=[{'wi': old, 'compactions': 3}]),
            updated=at, started=self.started, observed=at))
        b = self.run_query('compactions', '--work-item-id', new)['result']
        self.assertEqual((b['status'], b['reason'], b['total_compactions'], b['sessions']),
                         ('unknown', 'not_in_published_record', None, []))
        self.assertIsNone(self.run_query('wi', '--work-item-id', new)['result']['context_compactions']['total_compactions'])
        a = self.run_query('compactions', '--work-item-id', old)['result']
        self.assertEqual(a['total_compactions'], 3)
        self.assertEqual([(r['session_ref'], r['wi'], r['wi_compactions']) for r in a['sessions']], [(self.ref, new, 3)])
        self.assertEqual(self.run_query('compactions', '--session-id', SESSION)['result']['total_compactions'], 3)
        # Compactions under both WIs split between them; the session still totals all.
        private(self.state / 'context-compactions-v1.json', document(
            session(self.ref, wi=new, count=5, at=at, by_wi=[{'wi': None, 'compactions': 1}, {'wi': old, 'compactions': 3},
                                                            {'wi': new, 'compactions': 1}]),
            session('ccf1_' + 'c' * 64, wi=new, count=2, at=at), updated=at, started=self.started, observed=at))
        self.assertEqual(self.run_query('compactions', '--work-item-id', old)['result']['total_compactions'], 3)
        b = self.run_query('compactions', '--work-item-id', new)['result']
        self.assertEqual((b['total_compactions'], sorted(r['wi_compactions'] for r in b['sessions'])), (3, [1, 2]))
        self.assertEqual(self.run_query('compactions', '--session-id', SESSION)['result']['total_compactions'], 5)
        self.assertEqual(self.run_query('compactions')['result']['total_compactions'], 7)

    def test_an_old_core_export_leaves_the_rollover_count_unknown(self):
        result = self.run_query('wi', '--work-item-id', 'wi_20261118_001')['result']
        self.assertEqual(result['rollovers'], {'count': None, 'by_agent': None, 'items': [],
                                               'reason': 'core_export_without_rollovers'})

    def test_a_session_or_wi_outside_the_record_is_unknown_never_zero(self):
        for argv in (('--session-id', '0a1b2c3d-1111-2222-3333-444455556666'), ('--work-item-id', 'wi_20261118_001')):
            answer = self.run_query('compactions', *argv)
            self.assertEqual(answer['status'], 'partial')
            self.assertIn('context_compactions:unknown', answer['errors'])
            self.assertEqual(answer['result']['sessions'], [])
            self.assertIsNone(answer['result']['total_compactions'])
            self.assertEqual(answer['result']['reason'], 'not_in_published_record')
        self.assertIsNone(self.run_query('wi', '--work-item-id', 'wi_20261118_001')['result']
                          ['context_compactions']['total_compactions'])
        found = self.run_query('compactions', '--session-id', SESSION)
        self.assertEqual((found['status'], found['result']['total_compactions']), ('ok', 3))

    def test_the_answer_states_the_records_coverage(self):
        result = self.run_query('compactions')['result']
        coverage = result['coverage']
        self.assertEqual(result['observed_at'], self.now.isoformat())
        self.assertEqual((coverage['last_compaction_from'], coverage['last_compaction_to']),
                         (self.started, self.now.isoformat()))
        self.assertEqual((coverage['recording_started_at'], coverage['dropped_through']), (self.started, None))
        self.assertEqual((coverage['stale'], coverage['uncovered_from'], coverage['uncovered_to']), (False, None, None))
        self.assertEqual((coverage['sessions_recorded'], coverage['at_capacity'], coverage['cc_mode']), (2, False, 'print'))
        self.assertTrue(coverage['compacted_sessions_only'])
        self.assertEqual(result['status'], 'complete')

    def test_a_stale_publication_is_partial_and_names_its_gap(self):
        observed = (self.now - timedelta(days=37)).isoformat()
        private(self.state / 'context-compactions-v1.json', document(
            session(self.ref, at=observed), updated=observed, started=(self.now - timedelta(days=40)).isoformat(),
            observed=observed))
        for argv in (('--session-id', SESSION), ('--work-item-id', WI)):
            answer = self.run_query('compactions', *argv)
            self.assertEqual(answer['status'], 'partial')
            self.assertTrue(answer['sources']['context_compactions']['stale'])
            self.assertIn('context_compactions:stale', answer['errors'])
            self.assertIn('context_compactions:partial', answer['errors'])
            result = answer['result']
            self.assertEqual((result['status'], result['reason'], result['total_compactions']), ('partial', 'record_stale', 3))
            self.assertEqual((result['coverage']['uncovered_from'], result['coverage']['uncovered_to']),
                             (observed, self.now.isoformat()))
        wi = self.run_query('wi', '--work-item-id', WI)
        self.assertIn('context_compactions:stale', wi['errors'])
        self.assertEqual(wi['result']['context_compactions']['status'], 'partial')

    def test_a_wi_total_is_complete_only_when_the_coverage_spans_the_wi(self):
        def wi_status():
            answer = self.run_query('compactions', '--work-item-id', WI)
            return answer['status'], answer['result']['status'], answer['result']['reason']
        facts = json.loads((self.state / 'wi-facts-v1.json').read_bytes())
        facts['items'][0]['created_at'] = (self.now - timedelta(days=1)).isoformat()
        private(self.state / 'wi-facts-v1.json', facts)
        self.assertEqual(wi_status(), ('ok', 'complete', None))
        # The WI is older than the record.
        self.publish(started=(self.now - timedelta(hours=12)).isoformat())
        self.assertEqual(wi_status(), ('partial', 'partial', 'work_item_predates_record'))
        # The window dropped a session whose last compaction came after the WI began.
        self.publish(dropped=(self.now - timedelta(hours=1)).isoformat())
        self.assertEqual(wi_status(), ('partial', 'partial', 'record_dropped_sessions_since_work_item_start'))
        all_rows = self.run_query('compactions')
        self.assertEqual((all_rows['result']['status'], all_rows['result']['reason']), ('partial', 'record_dropped_sessions'))
        # A drop before the WI began leaves it whole.
        self.publish(dropped=(self.now - timedelta(days=2)).isoformat())
        self.assertEqual(wi_status(), ('ok', 'complete', None))
        self.assertEqual(self.run_query('compactions', '--work-item-id', WI)['result']['coverage']['last_compaction_from'],
                         (self.now - timedelta(days=2)).isoformat())
        # Without the core's record of the WI its start is unknown.
        (self.state / 'wi-facts-v1.json').unlink()
        answer = self.run_query('compactions', '--work-item-id', WI)
        self.assertEqual((answer['result']['status'], answer['result']['reason']), ('partial', 'work_item_start_unknown'))

    def test_a_publication_without_its_observation_time_is_partial(self):
        at = self.now.isoformat()
        private(self.state / 'context-compactions-v1.json', document(session(self.ref, at=at), updated=at,
                                                                      started=self.started))
        answer = self.run_query('compactions', '--session-id', SESSION)
        self.assertEqual(answer['status'], 'partial')
        self.assertIsNone(answer['sources']['context_compactions']['stale'])
        self.assertEqual(answer['result']['reason'], 'observation_time_unknown')

    def test_a_missing_relay_record_is_unavailable_never_zero(self):
        (self.state / 'context-compactions-v1.json').unlink()
        answer = self.run_query('compactions', '--work-item-id', WI)
        self.assertEqual(answer['status'], 'unavailable')
        self.assertIn('context_compactions:compactions_source_unavailable', answer['errors'])


class CompactionRefreshTests(unittest.TestCase):
    def setUp(self):
        spec = importlib.util.spec_from_file_location('refresh_under_test', ROOT.parent / 'Scripts/pulse-stats-cost-refresh.py')
        self.refresh = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.refresh)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def test_refresh_publishes_relay_record_locally_and_to_the_destination_and_keeps_the_newest(self):
        refresh, state, source = self.refresh, self.root / 'state', self.root / 'relay.json'
        now = datetime.now(timezone.utc)
        destination = {'python': '/python', 'script': '/script'}
        config = {'destination': destination, 'context_compactions': {'path': str(source)}}
        sent = []
        with patch.object(refresh, 'STATE', state), \
                patch.object(refresh, 'ssh', side_effect=lambda peer, argv, data=None: sent.append((peer, argv, data))):
            state.mkdir(mode=0o700)
            self.assertEqual(refresh.refresh_context_compactions({'destination': destination}), 'source_not_configured')
            self.assertEqual(refresh.refresh_context_compactions(config), 'read_or_publish_failed')
            ref = context_compactions.session_ref(SESSION)
            private(source, document(session(ref), updated=now.isoformat()))
            self.assertEqual(refresh.refresh_context_compactions(config), 'published')
            self.assertEqual(sent[0][1], ['/python', '/script', 'receive-context-compactions'])
            published = (state / 'context-compactions-v1.json').read_bytes()
            self.assertEqual(sent[0][2], published)
            reading = json.loads(published)
            self.assertLessEqual(abs(datetime.fromisoformat(reading['observed_at']) - datetime.now(timezone.utc)),
                                 timedelta(minutes=1))
            # A relayed copy must say when it was read, and an older reading of the same record is refused.
            with self.assertRaises(ValueError):
                refresh.receive_context_compactions(json.dumps(document(session(ref), updated=now.isoformat())).encode())
            with self.assertRaises(ValueError):
                refresh.receive_context_compactions(json.dumps({**reading, 'observed_at': now.isoformat()}).encode())
            with self.assertRaises(ValueError):
                refresh.receive_context_compactions(json.dumps(document(
                    updated=(now - timedelta(minutes=1)).isoformat(), observed=now.isoformat())).encode())
            self.assertEqual((state / 'context-compactions-v1.json').read_bytes(), published)
