"""Publisher on Oracle, pushing peer on ownermac: synthetic files, no SSH or provider calls."""
from datetime import datetime, timedelta, timezone
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / 'ComputeCost/tests'))
spec = importlib.util.spec_from_file_location('stats_cost_refresh_push', ROOT / 'Scripts/pulse-stats-cost-refresh.py')
refresh = importlib.util.module_from_spec(spec)
spec.loader.exec_module(refresh)
refresh.refresh_pr_audit = lambda: 'not_read_in_tests'
refresh.refresh_pr_bodies = lambda: 'not_read_in_tests'
from test_collaboration_receipts import CATALOG
from pulse_compute_cost.stats_dashboard import DEFAULT_FACTS, build_dashboard

LEDGER = {'identity': 'github.com/example/repo', 'ref': 'refs/heads/main', 'namespace': 'production'}
REVISION = 'c' * 40


def receipt(ledger=None, watermark=941, inserted=3):
    return refresh.canonical({'schema': refresh.SHARED_COLLECTION_SCHEMA, 'ledger': ledger or LEDGER,
                              'watermark': watermark, 'inserted': inserted}) + b'\n'


def dashboard(now, minutes=75):
    return build_dashboard(None, subscriptions=json.loads(DEFAULT_FACTS.read_text()), source_revision=REVISION,
                           now=now, validity_minutes=minutes)


class Fixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.root.chmod(0o700)
        self.state = self.root / 'state'
        self.state.mkdir(mode=0o700)
        release = self.root / 'core'
        release.mkdir()
        (release / 'harness.pyz').write_bytes(b'sealed')
        (release / 'host-policy.json').write_text(json.dumps({'namespace': LEDGER['namespace'],
            'production_ledger': {'identity': LEDGER['identity'], 'ref': LEDGER['ref'], 'remote': 'origin'}}))
        self.registry = self.root / 'sources.json'
        refresh.private_write(self.registry, json.dumps({
            'schema': 'computecost_source_registry/v1', 'ledger_root': str(self.root / 'ledger'), 'sources': [],
            'observation_core_entrypoint': str(release / 'harness.pyz')}).encode())
        (self.root / '.stats-source-revision').write_text(REVISION)
        self.config = self.state / 'refresh.json'
        self.patches = patch.multiple(refresh, STATE=self.state, REGISTRY=self.registry, ROOT=self.root)
        self.patches.start()
        self.addCleanup(self.patches.stop)

    def write_config(self, **fields):
        refresh.private_write(self.config, json.dumps({'source_revision': REVISION, **fields}).encode())


class PushedPeerTests(Fixture):
    PEER = {'id': 'ownermac', 'transport': 'shared_ledger_push'}

    def run_refresh(self, **config):
        self.write_config(peers=[self.PEER], **config)
        seen = []

        def publish(args, *, row_contexts_ready):
            seen.append(args)
            self.marker_at_publication = (self.state / 'imports' / 'ownermac.shared.jsonl').exists()
            Path(args[args.index('--output') + 1]).write_text('{}')
            return 0
        with patch.object(refresh, 'ssh', side_effect=lambda *a, **k: self.fail('no SSH to a pushing peer')), \
             patch.object(refresh, 'fetch_openrouter_models', return_value=CATALOG), \
             patch.object(refresh, 'publish', side_effect=publish):
            refresh.refresh(self.config)
        return seen[0], json.loads((self.state / 'last-refresh.json').read_bytes())['peers']

    def test_fresh_push_reads_the_peer_without_any_ssh(self):
        self.write_config(peers=[self.PEER])
        refresh.receive_shared_collection(self.config, 'ownermac', receipt())
        args, peers = self.run_refresh()
        self.assertEqual(peers, [{'id': 'ownermac', 'status': 'read'}])
        selected = json.loads(Path(args[args.index('--source-registry') + 1]).read_bytes())
        marker = self.state / 'imports' / 'ownermac.shared.jsonl'
        self.assertEqual(selected['sources'][-1], {'id': 'stats-transport-ownermac', 'kind': 'receipts',
                                                   'path': str(marker)})
        self.assertEqual(marker.read_bytes(), b'')

    def test_a_push_whose_bundles_wait_is_a_partly_read_peer(self):
        from pulse_stats.cost_dashboard import load_refresh_status
        self.write_config(peers=[self.PEER])
        refresh.receive_shared_collection(self.config, 'ownermac', receipt())
        with patch.object(refresh, 'last_collection_backlog', return_value={'ownermac': 3}):
            _, peers = self.run_refresh()
        expected = {'id': 'ownermac', 'status': 'partial', 'reason': 'observation_backlog', 'deferred_bundles': 3}
        self.assertEqual(peers, [expected])
        status = json.loads((self.state / refresh.REFRESH_STATUS).read_bytes())
        self.assertEqual((status['state'], status['peers']), ('completed', [expected]))
        # Readers keep the peer and its count; a cleared backlog is read again.
        self.assertEqual(load_refresh_status(self.state / refresh.REFRESH_STATUS,
                                             now=datetime.now(timezone.utc))['peers'], [expected])
        refresh.receive_shared_collection(self.config, 'ownermac', receipt())
        self.assertEqual(self.run_refresh()[1], [{'id': 'ownermac', 'status': 'read'}])

    def test_waiting_bundles_stay_reported_beside_a_stale_push(self):
        from pulse_stats.cost_dashboard import load_refresh_status
        self.write_config(peers=[self.PEER])
        refresh.receive_shared_collection(self.config, 'ownermac', receipt())
        with patch.object(refresh, 'last_collection_backlog', return_value={'ownermac': 3}):
            self.assertEqual(self.run_refresh()[1], [{'id': 'ownermac', 'status': 'partial',
                                                      'reason': 'observation_backlog', 'deferred_bundles': 3}])
        # The next cycle has no newer push, yet two bundles still wait: both facts stay.
        with patch.object(refresh, 'last_collection_backlog', return_value={'ownermac': 2}):
            _, peers = self.run_refresh()
        expected = {'id': 'ownermac', 'status': 'unreachable_or_invalid', 'reason': 'peer_push_stale',
                    'deferred_bundles': 2}
        self.assertEqual(peers, [expected])
        status = json.loads((self.state / refresh.REFRESH_STATUS).read_bytes())
        self.assertEqual(status['peers'], [expected])
        self.assertEqual(load_refresh_status(self.state / refresh.REFRESH_STATUS,
                                             now=datetime.now(timezone.utc))['peers'], [expected])

    def test_missing_stale_or_damaged_push_is_an_unread_peer(self):
        args, peers = self.run_refresh()
        self.assertEqual(peers[0]['reason'], 'peer_push_missing')
        self.assertFalse((self.state / 'imports' / 'ownermac.shared.jsonl').exists())
        refresh.receive_shared_collection(self.config, 'ownermac', receipt())
        path = refresh.push_receipt_path('ownermac')
        record = json.loads(path.read_bytes())
        old = datetime.now(timezone.utc) - timedelta(seconds=refresh.PUSH_FRESH_SECONDS + 60)
        refresh.private_write(path, refresh.canonical({**record, 'received_at': old.isoformat()}))
        self.assertEqual(self.run_refresh()[1][0]['reason'], 'peer_push_stale')
        refresh.private_write(path, refresh.canonical({**record, 'receipt': {**record['receipt'], 'ledger': {
            **LEDGER, 'namespace': 'other'}}}))
        self.assertEqual(self.run_refresh()[1][0]['reason'], 'peer_receipt_invalid')

    def test_each_publication_reports_the_push_known_when_it_is_built(self):
        # Coverage and the peer report agree in the same publication: first
        # success, a stale push, and recovery.
        self.write_config(peers=[self.PEER])
        refresh.receive_shared_collection(self.config, 'ownermac', receipt())
        self.assertEqual(self.run_refresh()[1][0]['status'], 'read')
        self.assertTrue(self.marker_at_publication)
        path = refresh.push_receipt_path('ownermac')
        record = json.loads(path.read_bytes())
        old = datetime.now(timezone.utc) - timedelta(hours=1)
        refresh.private_write(path, refresh.canonical({**record, 'received_at': old.isoformat()}))
        self.assertEqual(self.run_refresh()[1][0]['reason'], 'peer_push_stale')
        self.assertFalse(self.marker_at_publication)
        refresh.receive_shared_collection(self.config, 'ownermac', receipt())
        self.assertEqual(self.run_refresh()[1][0]['status'], 'read')
        self.assertTrue(self.marker_at_publication)

    def test_receiver_accepts_only_a_registered_pusher_on_its_own_stream(self):
        self.write_config(peers=[self.PEER, {'id': 'tcloud', 'host': 'tcloud-node2', 'python': '/p', 'script': '/s',
                                             'transport': 'shared_ledger'}])
        for peer, data in (('tcloud', receipt()), ('other', receipt()),
                           ('ownermac', receipt({**LEDGER, 'ref': 'refs/heads/x'})), ('ownermac', b' ' * 4097)):
            with self.subTest(peer=peer), self.assertRaises(ValueError):
                refresh.receive_shared_collection(self.config, peer, data)
        self.assertFalse(refresh.push_receipt_path('ownermac').exists())

    def test_publisher_passes_the_pushed_provider_summary_and_its_validity(self):
        now = datetime.now(timezone.utc)
        day = (now - timedelta(days=1)).date().isoformat()
        summary = {'generated_at': now.isoformat(), 'activity': {
            'source': 'openrouter.activity', 'coverage': 'last_30_completed_utc_days',
            'by_day': [{'date': day, 'usage_usd': '1.25', 'requests': 3}]}}
        refresh.receive_openrouter_snapshot(json.dumps(summary).encode())
        args, _ = self.run_refresh(publication_validity_minutes=75)
        self.assertEqual(args[args.index('--openrouter-snapshot') + 1], str(self.state / refresh.OPENROUTER_SNAPSHOT))
        self.assertNotIn('--openrouter-env', args)
        self.assertEqual(args[args.index('--validity-minutes') + 1], '75')
        with self.assertRaises(ValueError):
            self.run_refresh(publication_validity_minutes=60)

    def test_publisher_passes_the_incremental_views_engine(self):
        args, _ = self.run_refresh()
        self.assertNotIn('--engine', args)  # Absent: the full calculation, as before.
        args, _ = self.run_refresh(incremental={'views': 'off'})
        self.assertNotIn('--engine', args)
        args, _ = self.run_refresh(incremental={'views': 'shadow'})
        self.assertEqual(args[args.index('--engine') + 1], 'shadow')
        args, _ = self.run_refresh(incremental={'ingest': 'off', 'views': 'on', 'collection': 'off'})
        self.assertEqual(args[args.index('--engine') + 1], 'incremental')
        self.assertNotIn('--close-weeks', args)

    def test_the_week_close_names_each_peer_and_when_its_data_was_collected(self):
        self.write_config(peers=[self.PEER])
        refresh.receive_shared_collection(self.config, 'ownermac', receipt())
        args, _ = self.run_refresh(incremental={'views': 'on', 'close': 'on'})
        self.assertIn('--close-weeks', args)
        states = {p['id']: p for p in json.loads(Path(args[args.index('--peer-state') + 1]).read_bytes())}
        self.assertEqual(states['tcloud']['status'], 'paused')  # Paused by default; never a gap.
        state = states['ownermac']
        self.assertEqual(state['status'], 'read')
        self.assertIsNotNone(state['collected_from'])
        args, _ = self.run_refresh(incremental={'views': 'off', 'close': 'on'})
        self.assertNotIn('--close-weeks', args)  # Closing reads the resolved store.

    def test_the_week_close_reads_a_shared_collection_confirmed_before_the_publication(self):
        from concurrent.futures import Future

        class Inline:
            """Runs each job at submit: the peer's collection is done when the publication begins."""
            def __init__(self, max_workers=None):
                pass

            def submit(self, job, *args, **kwargs):
                future = Future()
                try:
                    future.set_result(job(*args, **kwargs))
                except BaseException as exc:
                    future.set_exception(exc)
                return future

            def shutdown(self, wait=True):
                pass

        peer = {'id': 'studio', 'transport': 'shared_ledger', 'host': 'studio', 'python': 'python3', 'script': 's.py'}
        seen = []

        def publish(args, *, row_contexts_ready):
            seen.append(json.loads(Path(args[args.index('--peer-state') + 1]).read_bytes()))
            Path(args[args.index('--output') + 1]).write_text('{}')
            return 0
        for answer, expected in ((receipt(), {'status': 'read'}),
                                 (OSError('down'), {'status': 'unreachable_or_invalid'})):
            self.write_config(peers=[peer], incremental={'views': 'on', 'close': 'on'})
            with patch.object(refresh, 'ssh', side_effect=[answer]), \
                 patch.object(refresh, 'fetch_openrouter_models', return_value=CATALOG), \
                 patch.object(refresh, 'ThreadPoolExecutor', Inline), \
                 patch.object(refresh, 'publish', side_effect=publish):
                refresh.refresh(self.config)
            state = {p['id']: p for p in seen.pop()}['studio']
            # The close judges the same result this cycle reports, not a fixed "pending".
            reported = json.loads((self.state / 'last-refresh.json').read_bytes())['peers']
            self.assertEqual({k: v for k, v in state.items() if k != 'collected_from'},
                             next(p for p in reported if p['id'] == 'studio'))
            self.assertEqual(state['status'], expected['status'])
            self.assertIsNotNone(state['collected_from'])

    def test_publisher_passes_the_incremental_collection_engine(self):
        args, _ = self.run_refresh(incremental={'collection': 'off'})
        self.assertNotIn('--collection-engine', args)  # Off: the arguments of the full collection, unchanged.
        args, _ = self.run_refresh(incremental={'collection': 'shadow'})
        self.assertEqual(args[args.index('--collection-engine') + 1], 'shadow')
        args, _ = self.run_refresh(incremental={'collection': 'on'})
        self.assertEqual(args[args.index('--collection-engine') + 1], 'incremental')

    def test_provider_summary_receiver_is_closed_and_keeps_the_newest(self):
        now = datetime.now(timezone.utc)
        good = {'generated_at': now.isoformat(), 'activity': {
            'source': 'openrouter.activity', 'coverage': 'last_30_completed_utc_days', 'by_day': []}}
        refresh.receive_openrouter_snapshot(json.dumps(good).encode())
        for bad in ({**good, 'key': 'sk-or-secret'}, {**good, 'activity': {'source': 'other'}},
                    {**good, 'generated_at': 'yesterday'},
                    {**good, 'generated_at': (now - timedelta(minutes=5)).isoformat()}):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                refresh.receive_openrouter_snapshot(json.dumps(bad).encode())
        self.assertEqual(json.loads((self.state / refresh.OPENROUTER_SNAPSHOT).read_bytes()), good)


class PublicationPullTests(Fixture):
    PUBLISHER = {'id': 'oracle-node-1', 'host': 'pulse@oracle-node-1', 'python': '/python', 'script': '/script',
                 'transport': 'shared_ledger'}
    # The pushing peer names its publisher among the peers it installs readers on.
    CONFIG = {'publisher': 'oracle-node-1', 'peers': [PUBLISHER]}

    def setUp(self):
        super().setUp()
        self.publisher_state = self.root / 'publisher'
        self.publisher_state.mkdir(mode=0o700)

    def publish_remote(self, document, name='cost-dashboard-v1.json'):
        refresh.private_write(self.publisher_state / name, refresh.canonical(document) + b'\n')

    def pull(self):
        def fetch(peer, command, data=None):
            self.assertEqual((peer, command), (self.PUBLISHER, ['/python', '/script', 'fetch-publication']))
            with patch.object(refresh, 'STATE', self.publisher_state):
                return refresh.publication_bundle()
        with patch.object(refresh, 'ssh', side_effect=fetch):
            return refresh.pull_publication(self.CONFIG)

    def test_reader_installs_newer_publications_and_the_cycle_status(self):
        now = datetime.now(timezone.utc) - timedelta(minutes=10)
        current, closed = dashboard(now), dashboard(now - timedelta(minutes=1))
        self.publish_remote(current)
        self.publish_remote(closed, 'cost-last-week-v1.json')
        status = {'schema': refresh.REFRESH_STATUS_SCHEMA, 'state': 'completed', 'started_at': now.isoformat()}
        refresh.private_write(self.publisher_state / refresh.REFRESH_STATUS, json.dumps(status).encode())
        self.assertEqual(self.pull(), {'dashboard': 'installed', 'last_week': 'installed',
                                       'refresh_status': 'installed'})
        self.assertEqual(json.loads((self.state / 'cost-dashboard-v1.json').read_bytes()), current)
        self.assertEqual(json.loads((self.state / 'cost-last-week-v1.json').read_bytes()), closed)
        self.assertEqual(json.loads((self.state / refresh.REFRESH_STATUS).read_bytes()), status)
        self.assertEqual(os.stat(self.state / 'cost-dashboard-v1.json').st_mode & 0o077, 0)
        self.assertEqual(self.pull()['dashboard'], 'unchanged')
        newer = dashboard(now + timedelta(minutes=5))
        refresh.private_write(self.state / 'cost-dashboard-v1.json', refresh.canonical(newer) + b'\n')
        self.assertEqual(self.pull()['dashboard'], 'older')
        self.assertEqual(json.loads((self.state / 'cost-dashboard-v1.json').read_bytes()), newer)

    def test_absent_files_stay_absent_and_damaged_bundles_change_nothing(self):
        self.assertEqual(self.pull(), {'dashboard': 'absent', 'last_week': 'absent', 'refresh_status': 'absent'})
        for bad in (b'[]', b'{"schema":"pulse_stats_publication_bundle/v1"}', b'x' * (3 * 512 * 1024)):
            with self.subTest(bad=bad[:40]), patch.object(refresh, 'ssh', return_value=bad), \
                    self.assertRaises(ValueError):
                refresh.pull_publication(self.CONFIG)
        self.assertEqual(list(self.state.glob('cost-*')), [])


    def test_the_whole_bundle_is_checked_before_either_publication_is_written(self):
        now = datetime.now(timezone.utc) - timedelta(minutes=10)
        old_current, old_closed = dashboard(now - timedelta(minutes=30)), dashboard(now - timedelta(minutes=31))
        refresh.private_write(self.state / 'cost-dashboard-v1.json', refresh.canonical(old_current) + b'\n')
        refresh.private_write(self.state / 'cost-last-week-v1.json', refresh.canonical(old_closed) + b'\n')
        before = {n: (self.state / n).read_bytes() for n in ('cost-dashboard-v1.json', 'cost-last-week-v1.json')}
        self.publish_remote(dashboard(now))
        closed = dashboard(now - timedelta(minutes=1))
        for bad in ({**closed, 'sha256': '0' * 64}, {**closed, 'schema_version': 'other'},
                    {**closed, 'generated_at': 'yesterday'},
                    {**closed, 'generated_at': old_closed['generated_at']}):  # Same time, other digest.
            with self.subTest(bad={k: v for k, v in bad.items() if bad.get(k) != closed.get(k)}):
                self.publish_remote(bad, 'cost-last-week-v1.json')
                with self.assertRaises(ValueError):
                    self.pull()
                # A valid dashboard is not installed beside the old last week.
                self.assertEqual({n: (self.state / n).read_bytes() for n in before}, before)
        self.publish_remote(closed, 'cost-last-week-v1.json')
        self.assertEqual(self.pull(), {'dashboard': 'installed', 'last_week': 'installed', 'refresh_status': 'absent'})

    def test_a_release_entry_runs_only_while_the_publisher_selects_its_release(self):
        script = '/home/pulse/.local/share/pulse/stats/releases/{}/Scripts/pulse-stats-cost-refresh.py'
        a, b = 'a' * 40, 'b' * 40
        config = {'publisher': 'oracle-node-1', 'peers': [{**self.PUBLISHER, 'script': script.format(a)}]}
        for answer, outcome in (({'selected': a, 'entry': True}, None),
                                ({'selected': b, 'entry': True}, 'publisher_entry_stale'),
                                ({'selected': None, 'entry': False}, 'publisher_entry_stale'),
                                ({'selected': a, 'entry': False}, 'publisher_entry_missing')):
            ran = []

            def oracle(peer, command, data=None):
                if refresh.PUBLISHER_ENTRY_PROBE in command:
                    self.assertEqual(command, ['/python', '-I', '-B', '-c', refresh.PUBLISHER_ENTRY_PROBE,
                                               '/home/pulse/.local/share/pulse/stats'])
                    return json.dumps(answer).encode()
                ran.append(command)
                return refresh.canonical({'schema': refresh.PUBLICATION_BUNDLE_SCHEMA, 'dashboard': None,
                                          'last_week': None, 'refresh_status': None})
            with self.subTest(answer=answer), patch.object(refresh, 'ssh', side_effect=oracle):
                if outcome is None:
                    self.assertEqual(refresh.pull_publication(config)['dashboard'], 'absent')
                    self.assertEqual(ran, [['/python', script.format(a), 'fetch-publication']])
                else:
                    with self.assertRaises(refresh.PublisherEntryStale) as raised:
                        refresh.pull_publication(config)
                    self.assertEqual((refresh.failure_reason(raised.exception), ran), (outcome, []))

    def test_the_probe_reads_the_selection_on_the_publisher(self):
        import subprocess
        root = self.root / 'stats'
        for revision in ('a' * 40, 'b' * 40):
            (root / 'releases' / revision / 'Scripts').mkdir(parents=True)
            (root / 'releases' / revision / '.stats-source-revision').write_text(revision)
        (root / 'releases' / ('b' * 40) / 'Scripts/pulse-stats-cost-refresh.py').write_text('')
        (root / 'current').symlink_to(root / 'releases' / ('b' * 40))
        probe = lambda: json.loads(subprocess.run([sys.executable, '-I', '-B', '-c', refresh.PUBLISHER_ENTRY_PROBE,
                                                   str(root)], check=True, capture_output=True).stdout)
        self.assertEqual(probe(), {'selected': 'b' * 40, 'entry': True})
        (root / 'current').unlink()
        (root / 'current').symlink_to(root / 'releases' / ('a' * 40))
        self.assertEqual(probe(), {'selected': 'a' * 40, 'entry': False})


class CollectLocalTests(Fixture):
    PUBLISHER = PublicationPullTests.PUBLISHER

    def run_local(self, *, scheduled=False, collect_error=None):
        self.write_config(peer_id='ownermac', **PublicationPullTests.CONFIG)
        commands = []

        def transport(peer, command, data=None):
            commands.append(command[2:])
            if command[2] == 'fetch-publication':
                return refresh.canonical({'schema': refresh.PUBLICATION_BUNDLE_SCHEMA, 'dashboard': None,
                                          'last_week': None, 'refresh_status': None})
            return b''

        def collected(**_):
            if collect_error is not None:
                raise collect_error
            return receipt()
        environment = {refresh.SCHEDULE_VARIABLE: refresh.SCHEDULE_BEFORE_EXPIRY} if scheduled else {}
        with patch.object(refresh, 'ssh', side_effect=transport), \
             patch.object(refresh, 'shared_collection_receipt', side_effect=collected), \
             patch.multiple(refresh, refresh_quota=lambda config: 'source_not_configured',
                            refresh_cc_quota=lambda config: 'source_not_configured',
                            refresh_grok_quota=lambda config: 'source_not_configured',
                            refresh_wi_facts=lambda config: 'published'), \
             patch.dict(os.environ, environment, clear=False):
            if not scheduled:
                os.environ.pop(refresh.SCHEDULE_VARIABLE, None)
            refresh.collect_local(self.config)
        return commands, json.loads((self.state / refresh.COLLECT_LOCAL_STATUS).read_bytes())

    def test_cycle_pushes_its_position_and_leaves_the_pull_to_its_own_job(self):
        commands, status = self.run_local()
        self.assertEqual(commands, [['receive-shared-collection', '--peer', 'ownermac']])
        self.assertEqual((status['collection']['state'], status['collection']['watermark']), ('completed', 941))
        self.assertNotIn('publication', status)
        self.assertEqual(status['openrouter'], 'source_not_configured')

    def test_failed_collection_reports(self):
        with self.assertRaisesRegex(RuntimeError, 'local collection failed'):
            self.run_local(collect_error=MemoryError())
        status = json.loads((self.state / refresh.COLLECT_LOCAL_STATUS).read_bytes())
        self.assertEqual(status['collection']['state'], 'failed')

    def test_local_collection_shares_the_collect_shared_guard(self):
        held = refresh._collect_shared_lock()
        self.addCleanup(os.close, held)
        with self.assertRaisesRegex(RuntimeError, 'local collection failed'):
            self.run_local(collect_error=AssertionError('collected without the guard'))
        status = json.loads((self.state / refresh.COLLECT_LOCAL_STATUS).read_bytes())
        self.assertEqual((status['collection']['state'], status['collection']['reason']), ('failed', 'peer_busy'))

    def test_schedule_collects_each_interval(self):
        now = datetime(2026, 11, 13, 10, 0, tzinfo=timezone.utc)
        at = lambda minutes: (now - timedelta(minutes=minutes)).isoformat()
        due = refresh.collect_local_due
        self.assertTrue(due({}, now))
        done = {'state': 'completed', 'finished_at': at(3)}
        self.assertFalse(due({'collection': done}, now))
        self.assertTrue(due({'collection': {**done, 'finished_at': at(16)}}, now))
        failed = {'state': 'failed', 'finished_at': at(11)}
        self.assertTrue(due({'collection': failed}, now))
        self.assertFalse(due({'collection': {**failed, 'finished_at': at(9)}}, now))

    def test_scheduled_tick_that_is_not_due_reads_nothing(self):
        self.run_local()
        commands, _ = self.run_local(scheduled=True)
        self.assertEqual(commands, [])



class PublisherInputTests(Fixture):
    def test_pr_audit_cache_travels_from_the_github_reader_and_keeps_the_newest(self):
        from pulse_compute_cost import pull_request_audit as audit
        now = datetime.now(timezone.utc)
        cache = {'schema': audit.SCHEMA, 'observed_at': now.isoformat(), 'repositories': []}
        refresh.private_write(self.state / refresh.PR_AUDIT, json.dumps(cache).encode())
        commands, status = CollectLocalTests.run_local(self)
        self.assertIn(['receive-pr-audit'], commands)
        self.assertEqual(status['pull_request_audit_push'], 'published')
        older = {**cache, 'observed_at': (now - timedelta(hours=1)).isoformat()}
        for bad in (older, {**cache, 'schema': 'other'}, {**cache, 'observed_at': 'soon'}):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                refresh.receive_pr_audit(json.dumps(bad).encode())
        refresh.receive_pr_audit(json.dumps({**cache, 'observed_at': (now + timedelta(seconds=1)).isoformat()}).encode())
        self.assertFalse(audit.due(self.state / refresh.PR_AUDIT, now + timedelta(minutes=1)))

    def test_overlay_adds_publication_inputs_but_never_replaces_a_source(self):
        base = json.loads(self.registry.read_bytes())
        core = {'id': 'harness-core-outcomes', 'kind': 'harness_core', 'path': '/state/wi-facts-v1.json'}
        result = refresh.publisher_registry(base, {
            'required_receipt_features': ['tenant_context.v1', 'actor_context.v1'],
            'tenant_registry_path': '/state/tenants.json', 'workspace_registry_path': '/ssot/registry.yaml',
            'sources': [core]})
        self.assertEqual(result['required_receipt_features'], ['actor_context.v1', 'tenant_context.v1'])
        self.assertEqual((result['tenant_registry_path'], result['sources'][-1]), ('/state/tenants.json', core))
        self.assertEqual(json.loads(self.registry.read_bytes()), base)
        self.assertIs(refresh.publisher_registry(base, None), base)
        for bad in ({'ledger_root': '/other'}, {'tenant_registry_path': 'relative.json'},
                    {'sources': [{**core, 'kind': 'cc'}]}, {'sources': [core, core]}):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                refresh.publisher_registry(base, bad)

    def test_publisher_waits_for_a_running_peer_collection_then_holds_the_lock(self):
        held = refresh._collect_shared_lock()
        self.assertIsNotNone(held)
        with self.assertRaisesRegex(RuntimeError, 'collection lock'):
            refresh._collect_shared_lock_waiting(0)
        os.close(held)
        fd = refresh._collect_shared_lock_waiting(0)
        self.assertIsNone(refresh._collect_shared_lock())
        os.close(fd)

    def test_cycle_reads_its_configuration_only_under_the_cycle_lock(self):
        # A cycle started from a release a reader switch has since replaced.
        self.write_config(source_revision='d' * 40)
        cycle = {}
        with self.assertRaisesRegex(ValueError, 'release identity'):
            refresh._refresh(self.config, force_recompute=True, cycle=cycle)
        self.assertNotIn('started_at', cycle)
        self.assertEqual(refresh.read_refresh_status(), {})
        # While the switch holds the cycle lock, a new cycle reads nothing.
        held = os.open(self.state / refresh.CYCLE_LOCK, os.O_CREAT | os.O_RDWR, 0o600)
        self.addCleanup(os.close, held)
        refresh.fcntl.flock(held, refresh.fcntl.LOCK_EX)
        with patch.object(refresh, 'release_config', side_effect=AssertionError('read before the lock')), \
                self.assertRaises(BlockingIOError):
            refresh._refresh(self.config, force_recompute=True, cycle={})

    def test_publisher_must_be_one_registered_ssh_peer(self):
        peer = PublicationPullTests.PUBLISHER
        self.assertEqual(refresh.publisher_peer({'publisher': 'oracle-node-1', 'peers': [peer]}), peer)
        for config in ({'publisher': 'oracle-node-1', 'peers': []}, {'publisher': 'oracle-node-1', 'peers': [peer, peer]},
                       {'publisher': 'ownermac', 'peers': [{'id': 'ownermac', 'transport': 'shared_ledger_push'}]},
                       {'peers': [peer]}):
            with self.subTest(config=config), self.assertRaises(ValueError):
                refresh.publisher_peer(config)


class PublicationValidityTests(unittest.TestCase):
    def test_readers_accept_the_oracle_validity_and_no_longer(self):
        from pulse_stats.cost_dashboard import _validate
        from pulse_compute_cost.receipts import digest
        now = datetime(2026, 11, 13, 2, 0, tzinfo=timezone.utc)
        for minutes in refresh.VALIDITY_MINUTES:
            generated, expires = _validate(dashboard(now, minutes))
            self.assertEqual(expires - generated, timedelta(minutes=minutes))
        longer = dashboard(now)
        longer['expires_at'] = (now + timedelta(minutes=76)).isoformat()
        longer['sha256'] = digest({k: v for k, v in longer.items() if k != 'sha256'})
        with self.assertRaisesRegex(ValueError, 'validity'):
            _validate(longer)
        with self.assertRaisesRegex(ValueError, 'validity'):
            dashboard(now, 60)

    def test_validity_ends_at_the_beijing_day_boundary(self):
        late = datetime(2026, 11, 13, 15, 30, tzinfo=timezone.utc)  # 23:30 Beijing
        document = dashboard(late)
        self.assertEqual(document['expires_at'], datetime(2026, 11, 13, 16, 0, tzinfo=timezone.utc).isoformat())


if __name__ == '__main__':
    unittest.main()
