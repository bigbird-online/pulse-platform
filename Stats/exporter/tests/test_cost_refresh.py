"""Production transport contracts; synthetic receipts, no SSH or provider calls."""
from datetime import datetime, timedelta, timezone
import contextlib
import gzip
import importlib.util
import json
import os
import subprocess
from types import SimpleNamespace
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / 'ComputeCost/tests'))
spec = importlib.util.spec_from_file_location('stats_cost_refresh', ROOT / 'Scripts/pulse-stats-cost-refresh.py')
refresh = importlib.util.module_from_spec(spec)
spec.loader.exec_module(refresh)
# The PR audit reads GitHub; cycles here never do. Its own test drives a fake reader.
REAL_PR_AUDIT = refresh.refresh_pr_audit
refresh.refresh_pr_audit = lambda: 'not_read_in_tests'
REAL_PR_BODIES = refresh.refresh_pr_bodies
refresh.refresh_pr_bodies = lambda: 'not_read_in_tests'
from test_collaboration_receipts import complete, CATALOG, AT, PERIOD, WI
from pulse_compute_cost.collaboration_query import CollaborationQuery, query_collaboration
from pulse_compute_cost.ledger import UsageLedger
from pulse_compute_cost.receipt_sources import collect_registered_sources
from pulse_compute_cost.receipts import canonical, digest


def query_snapshot(expires):
    """The smallest snapshot the query accepts."""
    return {'schema_version': 'pulse.monitoring.snapshot.v2', 'metrics': [],
            'generated_at': (expires - timedelta(minutes=45)).isoformat(), 'expires_at': expires.isoformat()}


class AgentSnapshotTests(unittest.TestCase):
    """The App is quiet overnight; checks every tick, also during a cycle, keep queries fresh."""
    NIGHT = datetime(2026, 11, 15, 14, 0, tzinfo=timezone.utc)  # 22:00 Beijing
    DAY = datetime(2026, 11, 16, 2, 0, tzinfo=timezone.utc)  # 10:00 Beijing

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        self.state = self.home/'state'
        self.state.mkdir(mode=0o700)
        self.app = self.home/'snapshot-v2.json'
        self.agent = self.state/'agent-snapshot-v1.json'
        self.runs = 0

    def write(self, path, expires):
        path.write_text(json.dumps(query_snapshot(expires)))
        path.chmod(0o600)

    def exporter(self, now, document=None):
        def run(*_, **__):
            self.runs += 1
            value = document if document is not None else query_snapshot(now + timedelta(minutes=45))
            return SimpleNamespace(stdout=json.dumps(value).encode())
        return run

    def patched(self, run):
        return [patch.object(refresh, 'APP_SNAPSHOT', self.app), patch.object(refresh, 'AGENT_SNAPSHOT', self.agent),
                patch.object(refresh, 'STATE', self.state), patch.object(refresh.Path, 'home', return_value=self.home),
                patch.object(refresh.subprocess, 'run', side_effect=run)]

    def tick(self, now, run=None):
        with contextlib.ExitStack() as stack:
            for item in self.patched(run or self.exporter(now)):
                stack.enter_context(item)
            return refresh.refresh_agent_snapshot(now)

    def test_overnight_two_minute_checks_never_let_queries_go_stale(self):
        self.write(self.app, self.NIGHT + timedelta(minutes=12))  # App last refreshed 19:57
        now = self.NIGHT
        for _ in range(180):  # Two-minute checks until 04:00 Beijing.
            self.tick(now)
            expiry = refresh._snapshot_expiry(self.agent) or refresh._snapshot_expiry(self.app)
            self.assertGreater(expiry - now, timedelta(minutes=2))  # Valid until the next check.
            now += timedelta(minutes=2)
        self.assertEqual(self.agent.stat().st_mode & 0o777, 0o600)
        self.assertLessEqual(self.runs, 12)  # About one exporter run per 35 minutes.

    def test_by_day_a_running_app_keeps_its_own_snapshot(self):
        self.write(self.app, self.DAY + timedelta(minutes=15))
        self.assertEqual((self.tick(self.DAY), self.runs), ('fresh', 0))
        self.write(self.app, self.DAY + timedelta(minutes=3))  # The App has stopped refreshing.
        self.assertEqual(self.tick(self.DAY), 'written')

    def test_only_a_snapshot_the_query_accepts_is_written_or_counted(self):
        self.assertEqual(self.tick(self.NIGHT, lambda *_, **__: SimpleNamespace(stdout=b'not json')), 'invalid')
        unsupported = {'expires_at': (self.NIGHT + timedelta(minutes=45)).isoformat()}
        self.assertEqual(self.tick(self.NIGHT, self.exporter(self.NIGHT, unsupported)), 'invalid')
        self.assertFalse(self.agent.exists())
        self.agent.write_text(json.dumps(unsupported));self.agent.chmod(0o600)
        self.assertEqual(self.tick(self.NIGHT), 'written')  # A cached file the query rejects is not fresh.

    def test_checks_continue_through_a_long_local_cycle(self):
        calls = []
        with patch.object(refresh, 'maintain_agent_snapshot', side_effect=lambda: calls.append(1)):
            with refresh.agent_snapshot_kept(tick=0.01):
                time.sleep(0.2)  # Stands in for a cycle longer than the snapshot margin.
        self.assertGreater(len(calls), 3)
        settled = len(calls);time.sleep(0.05)
        self.assertEqual(len(calls), settled)  # Stops with the cycle.

    def test_both_local_modes_keep_it_through_the_cycle(self):
        for mode, target in (('refresh', 'refresh'), ('collect-local', 'collect_local')):
            kept = []
            @contextlib.contextmanager
            def keeper():
                kept.append('enter');yield;kept.append('exit')
            with patch.object(sys, 'argv', ['refresh', mode]), \
                    patch.object(refresh, 'agent_snapshot_kept', keeper), \
                    patch.object(refresh, target, side_effect=lambda *a, **k: kept.append('cycle')):
                self.assertEqual(refresh.main(), 0)
            self.assertEqual(kept, ['enter', 'cycle', 'exit'], mode)


class CostRowContextCapabilityTests(unittest.TestCase):
    def test_selected_exporter_and_destination_must_both_accept_shared_context(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            exporter = home/'.local/share/pulse/stats/current/Stats/exporter/pulse-stats-snapshot.py'
            exporter.parent.mkdir(parents=True)
            exporter.touch()
            supported = b'{"schema":"pulse_stats_cost_row_contexts/v1"}'
            destination = {'python': '/private/python', 'script': '/private/refresh'}
            with patch.object(refresh.Path, 'home', return_value=home), \
                    patch.object(refresh.subprocess, 'run', return_value=SimpleNamespace(stdout=supported)) as run, \
                    patch.object(refresh, 'ssh', return_value=b'{}') as peer:
                self.assertTrue(refresh.cost_row_contexts_ready())
                self.assertFalse(refresh.cost_row_contexts_ready(destination))
                peer.return_value = supported
                self.assertTrue(refresh.cost_row_contexts_ready(destination))
                peer.assert_called_with(destination, ['/private/python', '/private/refresh', 'probe-cost-row-contexts'])
                self.assertEqual(run.call_args.args[0][-2:], ['--probe-cost-row-contexts', '--json'])
                peer.reset_mock()
                run.return_value = SimpleNamespace(stdout=b'{}')
                self.assertFalse(refresh.cost_row_contexts_ready(destination))
                peer.assert_not_called()

    def test_gzip_requires_a_separate_reader_capability(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            exporter = home/'.local/share/pulse/stats/current/Stats/exporter/pulse-stats-snapshot.py'
            exporter.parent.mkdir(parents=True)
            exporter.touch()
            old = b'{"schema":"pulse_stats_cost_row_contexts/v1"}'
            current = b'{"schema":"pulse_stats_cost_compression/v1"}'
            destination = {'python': '/private/python', 'script': '/private/refresh'}
            with patch.object(refresh.Path, 'home', return_value=home), \
                    patch.object(refresh.subprocess, 'run', return_value=SimpleNamespace(stdout=old)) as run, \
                    patch.object(refresh, 'ssh', return_value=current) as peer:
                self.assertFalse(refresh.cost_row_contexts_ready(destination, encoding=refresh.COMPRESSED_FIELD))
                peer.assert_not_called()
                run.return_value = SimpleNamespace(stdout=current)
                self.assertTrue(refresh.cost_row_contexts_ready(destination, encoding=refresh.COMPRESSED_FIELD))
                peer.assert_called_once_with(destination, ['/private/python', '/private/refresh', 'probe-cost-compression'])


class CodexQuotaPublicationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state = Path(self.tmp.name)
        self.state.chmod(0o700)
        self.destination = {'host': 'fixture', 'python': '/python', 'script': '/script'}
        self.config = {'destination': self.destination,
                       'codex_quota': {'command': ['/fixture/codex'], 'home': str(self.state)}}
        now = datetime.now(timezone.utc)
        self.document = {'schema': 'pulse.stats.codex_quota/v1', 'status': 'available',
                         'observed_at': now.isoformat(), 'account_ref': 'a'*64,
                         'limit_id': 'codex', 'plan_type': 'pro',
                         'windows': [{'used_percent': 51, 'window_minutes': 10080,
                                      'resets_at': int(now.timestamp()) + 86400}]}
        self.document['weekly'] = self.document['windows'][0]

    def failed_publication(self):
        pending = []
        with patch.object(refresh, 'read_account_quota', return_value=self.document), \
             patch.object(refresh, 'ssh', side_effect=RuntimeError('private transport error')):
            self.assertEqual(refresh.refresh_quota(self.config, pending=pending), 'read_or_publish_failed')
        self.assertEqual(len(pending), 1)
        return pending

    def test_failed_publish_retries_same_bytes_once_without_reading_quota_again(self):
        with patch.object(refresh, 'STATE', self.state):
            pending = self.failed_publication()
            original = (self.state/'codex-quota-v1.json').read_bytes()
            with patch.object(refresh, 'read_account_identity', return_value='a'*64), \
                 patch.object(refresh, 'read_account_quota', side_effect=AssertionError('second quota read')), \
                 patch.object(refresh, 'ssh', return_value=b'') as ssh:
                self.assertEqual(refresh.retry_quota_publication(self.config, pending), 'published')
                self.assertEqual(ssh.call_args.args[2], original)
                self.assertIsNone(refresh.retry_quota_publication(self.config, pending))
                self.assertEqual(ssh.call_count, 1)
            self.assertEqual((self.state/'codex-quota-v1.json').read_bytes(), original)

    def test_retry_refuses_expired_account_changed_and_superseded_observations(self):
        from datetime import timedelta
        for case in ('stale', 'account_changed', 'superseded'):
            with self.subTest(case=case), patch.object(refresh, 'STATE', self.state):
                pending = self.failed_publication()
                identity = 'b'*64 if case == 'account_changed' else 'a'*64
                if case == 'stale':
                    pending[0] = {**pending[0], 'observed_at': (datetime.now(timezone.utc)-timedelta(minutes=45)).isoformat()}
                if case == 'superseded':
                    newer = {**self.document, 'observed_at': (datetime.now(timezone.utc)+timedelta(seconds=1)).isoformat()}
                    refresh.receive_quota(json.dumps(newer).encode())
                with patch.object(refresh, 'read_account_identity', return_value=identity), \
                     patch.object(refresh, 'ssh', side_effect=AssertionError('unsafe publication')):
                    self.assertEqual(refresh.retry_quota_publication(self.config, pending), 'read_or_publish_failed')
                    self.assertEqual(pending, [])
                (self.state/'codex-quota-v1.json').unlink()

    def test_cycle_retries_under_lock_even_when_cost_publication_fails(self):
        import fcntl
        for cost_failure in (False, True):
            with self.subTest(cost_failure=cost_failure):
                revision = 'a'*40
                (self.state/'.stats-source-revision').write_text(revision)
                config = self.state/'refresh.json'
                refresh.private_write(config, json.dumps({**self.config, 'source_revision': revision, 'peers': []}).encode())
                registry = self.state/'sources.json'
                refresh.private_write(registry, json.dumps({'sources': [], 'ledger_root': str(self.state/'ledger')}).encode())
                calls = []
                def transport(peer, command, data=None):
                    with (self.state/'cost-refresh.lock').open('r+') as check:
                        with self.assertRaises(BlockingIOError):
                            fcntl.flock(check, fcntl.LOCK_EX|fcntl.LOCK_NB)
                    calls.append((command[-1], data))
                    if len(calls) == 1:
                        raise refresh.PeerTransportError(255)
                    return b''
                def publication(args, *, row_contexts_ready):
                    if cost_failure:
                        return 1
                    refresh.private_write(self.state/'cost-dashboard-v1.json', b'{}')
                    return 0
                with patch.multiple(refresh, STATE=self.state, ROOT=self.state, REGISTRY=registry), \
                     patch.object(refresh, 'read_account_quota', return_value=self.document) as read, \
                     patch.object(refresh, 'read_account_identity', return_value='a'*64) as identity, \
                     patch.object(refresh, 'fetch_openrouter_models', return_value=CATALOG), \
                     patch.object(refresh, 'publish', side_effect=publication), \
                     patch.object(refresh, 'ssh', side_effect=transport):
                    if cost_failure:
                        with self.assertRaisesRegex(RuntimeError, 'cost publication failed'):
                            refresh.refresh(config)
                    else:
                        refresh.refresh(config)
                        self.assertEqual(json.loads((self.state/'last-refresh.json').read_bytes())['codex_quota'], 'published')
                    self.assertEqual(read.call_count, 1)
                    self.assertEqual(identity.call_count, 1)
                quota_calls = [data for mode, data in calls if mode == 'receive-quota']
                self.assertEqual(len(quota_calls), 2)
                self.assertEqual(quota_calls[0], quota_calls[1])

    def test_read_failure_has_no_retry_and_safe_stage_code(self):
        import io
        captured = io.StringIO()
        pending = []
        with patch.object(refresh, 'STATE', self.state), patch.object(refresh.sys, 'stderr', captured), \
             patch.object(refresh, 'read_account_quota', side_effect=ValueError('secret token fixture')), \
             patch.object(refresh, 'ssh', side_effect=AssertionError('read failed')):
            self.assertEqual(refresh.refresh_quota(self.config, pending=pending), 'read_or_publish_failed')
        self.assertEqual(pending, [])
        self.assertFalse((self.state/'codex-quota-v1.json').exists())
        record = json.loads(captured.getvalue())
        self.assertEqual(record['stage'], 'codex_quota_read_failed')
        self.assertEqual(record['error_code'], 'quota_validation_failed')
        self.assertNotIn('secret', captured.getvalue())

    def test_timeout_retry_is_consumed_once_and_diagnostics_are_safe(self):
        import io
        for failure in ('identity', 'publication'):
            with self.subTest(failure=failure), patch.object(refresh, 'STATE', self.state):
                pending = self.failed_publication()
                captured = io.StringIO()
                identity_error = refresh.QuotaError('quota_timeout') if failure == 'identity' else None
                with patch.object(refresh.sys, 'stderr', captured), \
                     patch.object(refresh, 'read_account_identity', return_value='a'*64, side_effect=identity_error), \
                     patch.object(refresh, 'ssh', side_effect=subprocess.TimeoutExpired('private command fixture', 180)) as ssh:
                    self.assertEqual(refresh.retry_quota_publication(self.config, pending), 'read_or_publish_failed')
                    self.assertIsNone(refresh.retry_quota_publication(self.config, pending))
                self.assertEqual(ssh.call_count, 0 if failure == 'identity' else 1)
                record = json.loads(captured.getvalue())
                self.assertEqual(record['error_code'], 'quota_timeout' if failure == 'identity' else 'peer_timeout')
                self.assertNotIn('private command fixture', captured.getvalue())
                (self.state/'codex-quota-v1.json').unlink()

    def test_publish_exit_code_is_logged_without_stderr(self):
        import io
        captured = io.StringIO()
        pending = []
        with patch.object(refresh, 'STATE', self.state), patch.object(refresh.sys, 'stderr', captured), \
             patch.object(refresh, 'read_account_quota', return_value=self.document), \
             patch.object(refresh.subprocess, 'run', return_value=SimpleNamespace(returncode=255, stderr=b'secret')):
            self.assertEqual(refresh.refresh_quota(self.config, pending=pending), 'read_or_publish_failed')
        record = json.loads(captured.getvalue().splitlines()[-1])
        self.assertEqual(record['stage'], 'codex_quota_publish_failed')
        self.assertEqual(record['error_code'], 'peer_transport_failed')
        self.assertEqual(record['exit_code'], 255)
        self.assertNotIn('secret', captured.getvalue())


class CostRefreshTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.root.chmod(0o700)
        self.data = b''.join(canonical(r.to_dict()) + b'\n' for r in complete())
        self.ledger = UsageLedger(self.root / 'existing-ledger')

    def registry(self, paths):
        return {'schema': 'computecost_source_registry/v1',
                'ledger_root': str(self.ledger.root), 'sources': [
                    {'id': 'peer-' + str(i), 'kind': 'receipts', 'path': str(p)}
                    for i, p in enumerate(paths)]}

    def query(self):
        return query_collaboration(self.ledger, CollaborationQuery(work_items=(WI,), **PERIOD),
                                   catalog=CATALOG, catalog_observed_at=AT)

    def test_collection_transport_budget_is_separate_and_still_bounded(self):
        from types import SimpleNamespace
        peer={'host':'test-peer'}
        for operation, expected in [('export-framed-gzip',900),('export-gzip',900),('export',900),('receive',180),('receive-quota',180),('receive-cc-quota',180)]:
            with self.subTest(operation=operation), patch.object(refresh.subprocess,'run',return_value=SimpleNamespace(returncode=0)) as run:
                self.assertEqual(refresh.ssh(peer,['python','registered-script',operation]),b'')
                self.assertEqual(run.call_args.kwargs['timeout'],expected)
        self.assertEqual(refresh.MAX_RECEIPTS,64*1024*1024)
        # A framed full replay (TCloud ~150 MiB) has its own larger wire bound.
        self.assertEqual(refresh.receipt_transport.WIRE_BYTES,256*1024*1024)
        big=SimpleNamespace(returncode=0)
        def oversized(*args, **kwargs):
            kwargs['stdout'].write(b'x'*(64*1024*1024+1)); return big
        with patch.object(refresh.subprocess,'run',side_effect=oversized):
            self.assertEqual(len(refresh.ssh(peer,['python','registered-script','export-framed-gzip'])),64*1024*1024+1)
        with patch.object(refresh.subprocess,'run',side_effect=oversized), self.assertRaises(refresh.PeerTransportError):
            refresh.ssh(peer,['python','registered-script','export-gzip'])

    def cc_document(self, observed, used=4.0):
        from datetime import timedelta
        now=datetime.now(timezone.utc)
        return {'schema':'pulse.stats.cc_quota/v1','status':'available','source':'relay_cc_rate_limit_event',
                'observed_at':observed.isoformat().replace('+00:00','Z'),
                'weekly':{'used_percent':used,'window_minutes':10080,'resets_at':int((now+timedelta(days=2)).timestamp())},
                'five_hour':None,'rate_limit_status':'allowed'}

    def test_cc_quota_receiver_keeps_newest_valid_publication(self):
        from datetime import timedelta
        now=datetime.now(timezone.utc)
        state=self.root/'cc-state';target=state/'cc-quota-v1.json'
        with patch.object(refresh,'STATE',state):
            refresh.receive_cc_quota(json.dumps(self.cc_document(now-timedelta(minutes=5))).encode())
            first=target.read_bytes()
            self.assertEqual(json.loads(first)['weekly']['used_percent'],4.0)
            self.assertEqual(target.stat().st_mode&0o777,0o600)
            with self.assertRaisesRegex(ValueError,'older'):
                refresh.receive_cc_quota(json.dumps(self.cc_document(now-timedelta(minutes=6),used=3.0)).encode())
            for invalid in (b'{broken', json.dumps({**self.cc_document(now),'schema':'other'}).encode(), b' '*65537):
                with self.assertRaises(ValueError):
                    refresh.receive_cc_quota(invalid)
            self.assertEqual(target.read_bytes(),first)
            refresh.receive_cc_quota(json.dumps(self.cc_document(now-timedelta(minutes=5))).encode())
            refresh.receive_cc_quota(json.dumps(self.cc_document(now,used=5.0)).encode())
            self.assertEqual(json.loads(target.read_bytes())['weekly']['used_percent'],5.0)

    def test_cc_quota_refresh_publishes_relay_file_locally_and_to_destination(self):
        from datetime import timedelta
        state=self.root/'cc-refresh';source=self.root/'relay-cc-quota.json'
        destination={'host':'destination','python':'/python','script':'/script'}
        config={'destination':destination,'cc_quota':{'path':str(source)}}
        sent=[]
        with patch.object(refresh,'STATE',state), \
             patch.object(refresh,'ssh',side_effect=lambda peer,command,data=None:sent.append((peer,command,data)) or b''):
            self.assertEqual(refresh.refresh_cc_quota({'destination':destination}),'source_not_configured')
            self.assertEqual(refresh.refresh_cc_quota(config),'read_or_publish_failed')
            self.assertEqual(refresh.refresh_cc_quota({**config,'cc_quota':{'path':'relative.json'}}),'read_or_publish_failed')
            self.assertEqual(sent,[])
            source.write_text(json.dumps(self.cc_document(datetime.now(timezone.utc)-timedelta(minutes=1))))
            source.chmod(0o600)
            self.assertEqual(refresh.refresh_cc_quota(config),'published')
            self.assertEqual(sent[0][1],['/python','/script','receive-cc-quota'])
            self.assertEqual(sent[0][2],(state/'cc-quota-v1.json').read_bytes())
            published=(state/'cc-quota-v1.json').read_bytes()
            source.write_text(json.dumps(self.cc_document(datetime.now(timezone.utc),used=9.0)))
            with patch.object(refresh,'ssh',side_effect=RuntimeError('peer transport failed')):
                self.assertEqual(refresh.refresh_cc_quota(config),'read_or_publish_failed')
            source.write_text('{broken')
            self.assertEqual(refresh.refresh_cc_quota(config),'read_or_publish_failed')
            self.assertNotEqual((state/'cc-quota-v1.json').read_bytes(),published,
                                'local publication follows the newest valid Relay observation')

    def test_diagnostics_never_echo_private_error_text(self):
        import io
        captured=io.StringIO()
        with patch.object(refresh,'REGISTRY',self.root/'missing'), \
             patch.object(refresh.sys,'argv',['refresh-test','export']), \
             patch.object(refresh.sys,'stderr',captured):
            self.assertEqual(refresh.main(),2)
        first=json.loads(captured.getvalue().splitlines()[0])
        self.assertEqual(first,{'schema':'stats_refresh_progress/v1','stage':'command_failed',
                               'mode':'export','error_type':'FileNotFoundError','reason':'publication_failed'})
        self.assertNotIn(str(self.root),captured.getvalue())

    def test_incremental_ingest_switch_attaches_the_fact_index_only_when_on(self):
        source = self.root / 'a.jsonl'
        refresh.install_peer_input(source, self.data)
        timings = {}
        with refresh.ingest_index({}):
            self.assertIsNone(UsageLedger.index_provider)
            collect_registered_sources(self.registry([source]), self.ledger, timings=timings)
        self.assertFalse((self.ledger.root / 'derived').exists())
        self.assertNotIn('ingest_lookup', timings)
        self.assertEqual(timings['inserted'], len(self.ledger.receipt_snapshot()['facts']))
        indexed = UsageLedger(self.root / 'indexed-ledger')
        timings = {}
        with refresh.ingest_index({'incremental': {'ingest': 'on'}}):
            collect_registered_sources({**self.registry([source]), 'ledger_root': str(indexed.root)}, indexed,
                                       timings=timings)
        self.assertIsNone(UsageLedger.index_provider)
        self.assertEqual(timings['ingest_lookup'], 'index')
        self.assertTrue((indexed.root / 'derived' / 'fact-index.sqlite').is_file())
        self.assertEqual(indexed.receipt_snapshot()['facts'], self.ledger.receipt_snapshot()['facts'])
        with self.assertRaises(ValueError), refresh.ingest_index({'incremental': {'ingest': 'shadow'}}):
            pass

    def test_same_receipts_from_two_nodes_are_not_two_costs(self):
        a, b = self.root / 'a.jsonl', self.root / 'b.jsonl'
        refresh.install_peer_input(a, self.data)
        collect_registered_sources(self.registry([a]), self.ledger)
        before = self.query()['summary']
        refresh.install_peer_input(b, self.data)
        collect_registered_sources(self.registry([a, b]), self.ledger)
        after = self.query()['summary']
        self.assertEqual(after['usage']['known_total_tokens'], before['usage']['known_total_tokens'])
        self.assertEqual(after['usage']['event_count'], before['usage']['event_count'])
        self.assertEqual(after['cost'], before['cost'])

    def test_missing_peer_preserves_facts_but_withholds_cost_then_recovers(self):
        a = self.root / 'a.jsonl'
        refresh.install_peer_input(a, self.data)
        registry = self.registry([a])
        collect_registered_sources(registry, self.ledger)
        before = self.query()['summary']
        self.assertIsNotNone(before['cost']['machine_cash_usd'])
        refresh.install_peer_input(a, None)
        self.assertFalse(a.exists())
        self.assertTrue(a.with_suffix('.last-good').exists())
        collect_registered_sources(registry, self.ledger)
        missing = self.query()['summary']
        self.assertEqual(missing['usage']['known_total_tokens'], before['usage']['known_total_tokens'])
        self.assertIsNone(missing['cost']['machine_cash_usd'])
        refresh.install_peer_input(a, self.data)
        collect_registered_sources(registry, self.ledger)
        self.assertEqual(self.query()['summary']['cost'], before['cost'])

    def test_shared_marker_keeps_cost_and_coverage_of_the_transport_copy(self):
        # Shared ledger: the peer's facts are already in this ledger. Replacing
        # the full transport copy by the empty marker must not change a number,
        # and a missing marker must still withhold cost exactly like before.
        copy, marker = self.root / 'copy.jsonl', self.root / 'peer.shared.jsonl'
        refresh.install_peer_input(copy, self.data)
        collect_registered_sources(self.registry([copy]), self.ledger)
        before = self.query()['summary']
        self.assertIsNotNone(before['cost']['machine_cash_usd'])
        refresh.private_write(marker, b'')
        registry = self.registry([marker])  # same source id as the transport copy
        collect_registered_sources(registry, self.ledger)
        self.assertEqual(self.query()['summary'], before)
        marker.unlink()
        collect_registered_sources(registry, self.ledger)
        missing = self.query()['summary']
        for key in ('known_total_tokens', 'event_count'):
            self.assertEqual(missing['usage'][key], before['usage'][key])
        self.assertIsNone(missing['cost']['machine_cash_usd'])
        refresh.private_write(marker, b'')
        collect_registered_sources(registry, self.ledger)
        self.assertEqual(self.query()['summary']['cost'], before['cost'])

    def test_malformed_transport_cannot_partially_replace_good_input(self):
        a = self.root / 'a.jsonl'
        refresh.install_peer_input(a, self.data)
        with self.assertRaises(ValueError):
            refresh.install_peer_input(a, self.data + b'{broken\n')
        self.assertEqual(a.read_bytes(), self.data)
        self.assertEqual(a.stat().st_mode & 0o777, 0o600)

    def test_raw_export_remains_canonical_and_export_gzip_round_trips_wire_bytes(self):
        import io
        from types import SimpleNamespace
        a = self.root / 'a.jsonl'
        refresh.install_peer_input(a, self.data)
        registry = self.root / 'sources.json'
        refresh.private_write(registry, json.dumps(self.registry([a])).encode())
        raw, compressed, repeated = io.BytesIO(), io.BytesIO(), io.BytesIO()
        with patch.object(refresh, 'REGISTRY', registry), \
             patch.object(refresh.sys, 'stdout', SimpleNamespace(buffer=raw)):
            refresh.export_receipts()
        with patch.object(refresh, 'REGISTRY', registry), \
             patch.object(refresh.sys, 'stdout', SimpleNamespace(buffer=compressed)):
            refresh.export_gzip_receipts()
        with patch.object(refresh, 'REGISTRY', registry), \
             patch.object(refresh.sys, 'stdout', SimpleNamespace(buffer=repeated)):
            refresh.export_gzip_receipts()
        self.assertEqual(refresh.validate_receipts(raw.getvalue()), raw.getvalue())
        self.assertEqual(len(raw.getvalue().splitlines()), len(complete()))
        self.assertEqual(refresh.decompress_gzip_receipts(compressed.getvalue()), raw.getvalue())
        self.assertEqual(compressed.getvalue(), repeated.getvalue())

    def test_gzip_transport_rejects_corruption_truncation_trailing_and_members(self):
        wire = gzip.compress(self.data, mtime=0)
        corrupt = bytearray(wire)
        corrupt[-8] ^= 1
        for invalid in (wire[:-1], bytes(corrupt), wire + b'trailing', wire + wire):
            with self.subTest(invalid=invalid[-10:]):
                with self.assertRaises(ValueError):
                    refresh.decompress_gzip_receipts(invalid)

    def test_gzip_transport_has_strict_compressed_and_expanded_bounds(self):
        with patch.object(refresh, 'MAX_RECEIPTS', 64):
            with self.assertRaises(ValueError):
                refresh.decompress_gzip_receipts(b'x' * 65)
            with self.assertRaises(ValueError):
                refresh.decompress_gzip_receipts(gzip.compress(b'x' * 65, mtime=0))

    def test_transport_size_and_ssh_configuration_are_bounded(self):
        with patch.object(refresh, 'MAX_RECEIPTS', 2):
            with self.assertRaises(ValueError):
                refresh.validate_receipts(b'123')
        for host in ['-oProxyCommand=bad', 'host;echo bad', 'host\nother']:
            with self.assertRaises(ValueError):
                refresh.ssh({'host': host}, ['true'])
        with self.assertRaises(ValueError):
            refresh.ssh({'host': 'fixture', 'prefix': ['sh', '-c']}, ['true'])

    def test_native_refresh_collects_local_and_peer_receipts_into_the_authority_ledger(self):
        state=self.root/'native-state';state.mkdir(mode=0o700)
        registry=self.root/'native-sources.json'
        original={**self.registry([]),'observation_core_entrypoint':'/installed/active/harness.pyz'}
        refresh.private_write(registry,json.dumps(original).encode())
        revision='d'*40
        (self.root/'.pulse-release-manifest.json').write_text(json.dumps({
            'schema':'pulse_relay_immutable_release/v1','build_provenance':'exact_git_archive','source_sha':revision}))
        config=state/'refresh.json'
        refresh.private_write(config,json.dumps({'source_revision':revision,
            'peers':[{'id':'peer','host':'peer','python':'/python','script':'/script'}],
            'destination':{'host':'dest','python':'/python','script':'/script'}}).encode())
        seen=[]
        def publish(args, *, row_contexts_ready):
            self.assertIn('--collect',args)
            selected=json.loads(Path(args[args.index('--source-registry')+1]).read_bytes())
            self.assertEqual(selected['observation_core_entrypoint'],original['observation_core_entrypoint'])
            self.assertEqual([s['id'] for s in selected['sources']][-1],'stats-transport-peer')
            Path(args[args.index('--output')+1]).write_text('{}')
            seen.append(args);return 0
        def transport(peer,command,data=None):
            if 'export-framed-gzip' in command:
                return refresh.receipt_transport.encode(list(complete()),0,refresh.sha256(b'').hexdigest())
            self.assertEqual(peer['host'],'dest')
            return b''
        with patch.multiple(refresh,STATE=state,REGISTRY=registry,ROOT=self.root), \
             patch.object(refresh,'ssh',side_effect=transport), \
             patch.object(refresh,'fetch_openrouter_models',return_value=CATALOG), \
             patch.object(refresh,'publish',side_effect=publish):
            refresh.refresh(config)
        self.assertEqual(len(seen),1)
        self.assertEqual(json.loads(registry.read_bytes()),original)
        report=json.loads((state/'last-refresh.json').read_bytes())
        self.assertEqual(report['peers'],[{'id':'peer','status':'read'}])

    def test_two_peer_exports_start_concurrently_and_keep_registry_order(self):
        state=self.root/'parallel-state';state.mkdir(mode=0o700)
        registry=self.root/'parallel-sources.json'
        refresh.private_write(registry,json.dumps(self.registry([])).encode())
        revision='b'*40;(self.root/'.stats-source-revision').write_text(revision)
        config=state/'refresh.json'
        peers=[{'id':'one','host':'one','python':'/python','script':'/script'},
               {'id':'two','host':'two','python':'/python','script':'/script'}]
        refresh.private_write(config,json.dumps({'source_revision':revision,'peers':peers,
            'destination':{'host':'dest','python':'/python','script':'/script'}}).encode())
        barrier=threading.Barrier(2,timeout=2)
        def transport(peer,command,data=None):
            if 'export-framed-gzip' in command:
                barrier.wait()
                return refresh.receipt_transport.encode(list(complete()),
                    int(command[command.index('--prefix-bytes')+1]),
                    command[command.index('--prefix-sha256')+1])
            return b''
        with patch.multiple(refresh,STATE=state,REGISTRY=registry,ROOT=self.root),              patch.object(refresh,'ssh',side_effect=transport),              patch.object(refresh,'fetch_openrouter_models',return_value=CATALOG):
            refresh.refresh(config)
        derived=json.loads((state/'collector-sources.json').read_text())
        transport_ids=[row['id'] for row in derived['sources'] if row['kind']=='receipts']
        self.assertEqual(transport_ids,['stats-transport-one','stats-transport-two'])

    LEDGER={'identity':'github.com/example/repo','ref':'refs/heads/main','namespace':'production'}

    def installed_core(self, ledger=None):
        release=self.root/'core'/'releases'/'one';release.mkdir(parents=True,exist_ok=True)
        (release/'harness.pyz').write_bytes(b'sealed')
        ledger=ledger or self.LEDGER
        (release/'host-policy.json').write_text(json.dumps({'namespace':ledger['namespace'],
            'production_ledger':{'identity':ledger['identity'],'ref':ledger['ref'],'remote':'origin'}}))
        return str(release/'harness.pyz')

    def shared_receipt(self, ledger=None, watermark=941, inserted=12):
        return refresh.canonical({'schema':refresh.SHARED_COLLECTION_SCHEMA,'ledger':ledger or self.LEDGER,
                                  'watermark':watermark,'inserted':inserted})+b'\n'

    def shared_refresh(self, transport, *, installed=True, during=None, peers=None, extra=None):
        state=self.root/'shared-state';state.mkdir(mode=0o700,exist_ok=True)
        registry=self.root/'shared-sources.json'
        original={**self.registry([]),**({'observation_core_entrypoint':self.installed_core()} if installed else {})}
        refresh.private_write(registry,json.dumps(original).encode())
        revision='e'*40;(self.root/'.stats-source-revision').write_text(revision)
        config=state/'refresh.json'
        refresh.private_write(config,json.dumps({'source_revision':revision,**(extra or {}),
            'peers':peers or [{'id':'peer','host':'peer','python':'/python','script':'/script',
                               'transport':'shared_ledger'}]}).encode())
        seen=[]
        def publish(args, *, row_contexts_ready):
            selected=json.loads(Path(args[args.index('--source-registry')+1]).read_bytes())
            seen.append(selected['sources'][-1] if selected['sources'] else None)
            if during is not None:
                during(state)
            Path(args[args.index('--output')+1]).write_text('{}')
            return 0
        with patch.multiple(refresh,STATE=state,REGISTRY=registry,ROOT=self.root), \
             patch.object(refresh,'ssh',side_effect=transport), \
             patch.object(refresh,'fetch_openrouter_models',return_value=CATALOG), \
             patch.object(refresh,'publish',side_effect=publish):
            refresh.refresh(config)
        return state, seen

    def test_shared_ledger_peer_collects_in_place_and_ships_no_facts_back(self):
        commands=[]
        def transport(peer,command,data=None):
            commands.append(command)
            return self.shared_receipt()
        state,seen=self.shared_refresh(transport)
        self.assertEqual(commands,[['/python','/script','collect-shared']])
        marker=state/'imports'/'peer.shared.jsonl'
        # Coverage continuity: the same transport source id, now an empty input.
        self.assertEqual(seen,[{'id':'stats-transport-peer','kind':'receipts','path':str(marker)}])
        self.assertEqual(marker.read_bytes(),b'')
        self.assertFalse((state/'imports'/'peer.jsonl').exists())
        report=json.loads((state/'last-refresh.json').read_bytes())
        self.assertEqual(report['peers'],[{'id':'peer','status':'read'}])

    def test_paused_peer_is_not_reached_covered_or_failed(self):
        tcloud={'id':'tcloud','host':'tcloud-node2','python':'/python','script':'/script','transport':'shared_ledger'}
        peer={'id':'peer','host':'peer','python':'/python','script':'/script','transport':'shared_ledger'}
        for extra in ({}, {'paused_peers':{'tcloud':'paused_by_decision'}}):
            with self.subTest(extra=extra):
                commands=[]
                def transport(target,command,data=None):
                    commands.append((target['id'],command[-1]))
                    return self.shared_receipt()
                state,seen=self.shared_refresh(transport,peers=[tcloud,peer],extra=extra)
                # No collect-shared or WI-fact push reaches TCloud; the other peer is read as before.
                self.assertEqual([c for c in commands if c[0]=='tcloud'],[])
                self.assertIn(('peer','collect-shared'),commands)
                # Coverage names only the collected peer's transport source.
                self.assertEqual(seen,[{'id':'stats-transport-peer','kind':'receipts',
                                        'path':str(state/'imports'/'peer.shared.jsonl')}])
                peers=[{'id':'tcloud','status':'paused','reason':'paused_by_decision'},{'id':'peer','status':'read'}]
                report=json.loads((state/'last-refresh.json').read_bytes())
                self.assertEqual(report['peers'],peers)
                status=json.loads((state/refresh.REFRESH_STATUS).read_bytes())
                self.assertEqual((status['state'],status['peers']),('completed',peers))
        # A running or failed cycle names the pause too, never as a failure of TCloud.
        observed=[]
        def fail(state):
            observed.append(json.loads((state/refresh.REFRESH_STATUS).read_bytes()))
            raise RuntimeError('publication failed')
        with self.assertRaises(RuntimeError):
            self.shared_refresh(lambda *a, **k: self.shared_receipt(),peers=[tcloud,peer],during=fail)
        status=json.loads((self.root/'shared-state'/refresh.REFRESH_STATUS).read_bytes())
        paused=[{'id':'tcloud','status':'paused','reason':'paused_by_decision'}]
        self.assertEqual([(s['state'],s['peers']) for s in (observed[0],status)],
                         [('running',paused),('failed',paused)])
        # The explicit switch resumes it: collect-shared runs there again.
        commands=[]
        def resumed(target,command,data=None):
            commands.append((target['id'],command[-1]))
            return self.shared_receipt()
        state,_=self.shared_refresh(resumed,peers=[tcloud],extra={'paused_peers':{}})
        self.assertEqual(commands,[('tcloud','collect-shared')])
        self.assertEqual(json.loads((state/'last-refresh.json').read_bytes())['peers'],[{'id':'tcloud','status':'read'}])

    def test_failed_or_foreign_shared_collection_reports_unreachable_coverage(self):
        def failed(peer,command,data=None):
            raise refresh.PeerTransportError(255)
        foreign=lambda peer,command,data=None: self.shared_receipt({**self.LEDGER,'namespace':'other'})
        for transport, reason in ((failed, 'peer_ssh_failed'), (foreign, 'peer_receipt_invalid')):
            with self.subTest(transport=transport):
                state,seen=self.shared_refresh(transport)
                marker=state/'imports'/'peer.shared.jsonl'
                self.assertEqual(seen[-1]['path'],str(marker))
                self.assertFalse(marker.exists())
                report=json.loads((state/'last-refresh.json').read_bytes())
                peers=[{'id':'peer','status':'unreachable_or_invalid','reason':reason}]
                self.assertEqual(report['peers'],peers)
                # The local publication still landed; the cycle reports the peer's cause.
                status=json.loads((state/refresh.REFRESH_STATUS).read_bytes())
                self.assertEqual((status['state'],status['peers']),('completed',peers))

    def test_publication_does_not_wait_for_the_shared_peer(self):
        released, published = threading.Event(), threading.Event()
        def slow_peer(peer,command,data=None):
            # Returns only after the local publication has run.
            released.wait(10)
            return self.shared_receipt()
        def during(state):
            published.set(); released.set()
        state,_=self.shared_refresh(slow_peer,during=during)
        self.assertTrue(published.is_set())
        report=json.loads((state/'last-refresh.json').read_bytes())
        self.assertEqual(report['peers'],[{'id':'peer','status':'read'}])

    def test_shared_marker_changes_only_between_publications(self):
        # Each publication reports the peer collection completed when it
        # began; a failure this cycle is reported unreachable by the next.
        observed=[]
        marker=lambda state: state/'imports'/'peer.shared.jsonl'
        during=lambda state: observed.append(marker(state).exists())
        ok=lambda peer,command,data=None: self.shared_receipt()
        def failed(peer,command,data=None):
            raise refresh.PeerTransportError(255)
        for transport in (ok, failed, failed, ok):
            state,_=self.shared_refresh(transport,during=during)
        self.assertEqual(observed,[False,True,False,False])
        self.assertTrue(marker(state).exists())

    def test_shared_peer_is_joined_and_settled_even_when_publication_fails(self):
        finished=threading.Event()
        def peer(peer,command,data=None):
            finished.wait(10)
            return self.shared_receipt()
        def during(state):
            finished.set()
            raise RuntimeError('publication failed')
        with self.assertRaises(RuntimeError):
            self.shared_refresh(peer,during=during)
        self.assertTrue((self.root/'shared-state'/'imports'/'peer.shared.jsonl').exists())

    def test_shared_ledger_peer_requires_the_installed_authority(self):
        with self.assertRaises(ValueError):
            self.shared_refresh(lambda *a, **k: self.fail('no peer call'), installed=False)

    def test_shared_collection_receipt_is_closed_and_bounded(self):
        good=json.loads(self.shared_receipt(watermark=3,inserted=0))
        self.assertEqual(refresh.validate_shared_collection(refresh.canonical(good),self.LEDGER),good)
        for bad in ({**good,'facts':[]},{**good,'watermark':-1},{**good,'inserted':True},
                    {**good,'schema':'other'},{**good,'ledger':{**self.LEDGER,'ref':'refs/heads/x'}},[good]):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                refresh.validate_shared_collection(json.dumps(bad).encode(),self.LEDGER)
        with self.assertRaises(ValueError):
            refresh.validate_shared_collection(b' '*4097,self.LEDGER)

    def test_peer_side_shared_collection_returns_only_its_position(self):
        registry=self.root/'peer-sources.json'
        refresh.private_write(registry,json.dumps(self.registry([])).encode())
        with patch.object(refresh,'REGISTRY',registry), self.assertRaises(ValueError):
            refresh.shared_collection_receipt()
        refresh.private_write(registry,json.dumps({**self.registry([]),
            'observation_core_entrypoint':self.installed_core()}).encode())
        with patch.object(refresh,'REGISTRY',registry), \
             patch.object(refresh,'registered_ledger',return_value=self.ledger), \
             patch.object(refresh,'collect_registered_sources',
                          return_value={'watermark':7,'inserted':2,'duplicates':5}):
            data=refresh.shared_collection_receipt()
        self.assertEqual(refresh.validate_shared_collection(data,self.LEDGER),
                         json.loads(self.shared_receipt(watermark=7,inserted=2)))

    def test_retirement_requires_all_reads_destination_and_durable_success_under_lock(self):
        import fcntl
        for failed in (None, 'one', 'destination'):
            with self.subTest(failed=failed):
                state=self.root/('retention-'+str(failed));state.mkdir(mode=0o700)
                registry=state/'sources.json'
                refresh.private_write(registry,json.dumps(self.registry([])).encode())
                revision='c'*40;(self.root/'.stats-source-revision').write_text(revision)
                peers=[{'id':p,'host':p,'python':'/python','script':'/script'} for p in ('one','two')]
                config=state/'refresh.json'
                refresh.private_write(config,json.dumps({'source_revision':revision,'peers':peers,
                    'destination':{'host':'destination','python':'/python','script':'/script'}}).encode())
                calls=[]
                def transport(peer,command,data=None):
                    if peer['host']==failed: raise RuntimeError('fixture failure')
                    if 'export-framed-gzip' in command:
                        return refresh.receipt_transport.encode(list(complete()),0,refresh.sha256(b'').hexdigest())
                    return b''
                def publication(args, *, row_contexts_ready):
                    refresh.private_write(state/'cost-dashboard-v1.json',b'{}');return 0
                def retirement(peer):
                    self.assertEqual(json.loads((state/'last-refresh.json').read_text())['destination'],'published')
                    with (state/'cost-refresh.lock').open('r+') as check:
                        with self.assertRaises(BlockingIOError):fcntl.flock(check,fcntl.LOCK_EX|fcntl.LOCK_NB)
                    calls.append(peer)
                    return {'status':'protected'}  # Cleanup failure does not flip successful publication.
                with patch.multiple(refresh,STATE=state,REGISTRY=registry,ROOT=self.root), \
                     patch.object(refresh,'ssh',side_effect=transport), \
                     patch.object(refresh,'publish',side_effect=publication), \
                     patch.object(refresh,'retire_duplicate_fallback',side_effect=retirement), \
                     patch.object(refresh,'fetch_openrouter_models',return_value=CATALOG):
                    if failed=='destination':
                        with self.assertRaises(RuntimeError):refresh.refresh(config)
                        self.assertFalse((state/'last-refresh.json').exists())
                    else:
                        refresh.refresh(config)
                        report=json.loads((state/'last-refresh.json').read_text())
                        self.assertEqual(report['destination'],'published')
                        self.assertEqual(report['cc_quota'],'source_not_configured')
                self.assertEqual(calls,['one','two'] if failed is None else [])

    def test_refresh_reuses_existing_ledger_and_publishes_one_frozen_dashboard(self):
        state = self.root / 'state'
        state.mkdir(mode=0o700)
        registry = self.root / 'sources.json'
        refresh.private_write(registry, json.dumps(self.registry([])).encode())
        original = registry.read_bytes()
        revision = 'a' * 40
        (self.root / '.stats-source-revision').write_text(revision)
        config = state / 'refresh.json'
        refresh.private_write(config, json.dumps({'source_revision': revision,
            'peers': [{'id': 'fixture', 'host': 'fixture', 'python': '/python', 'script': '/script'}],
            'destination': {'host': 'fixture', 'python': '/python', 'script': '/script'}}).encode())
        sent = []
        def transport(peer, command, data=None):
            if 'export-framed-gzip' in command:
                return refresh.receipt_transport.encode(list(complete()), int(command[command.index('--prefix-bytes')+1]),
                    command[command.index('--prefix-sha256')+1])
            sent.append(json.loads(data))
            return b''
        with patch.multiple(refresh, STATE=state, REGISTRY=registry, ROOT=self.root), \
             patch.object(refresh, 'ssh', side_effect=transport), \
             patch.object(refresh, 'fetch_openrouter_models', return_value=CATALOG):
            refresh.refresh(config)
        self.assertEqual(registry.read_bytes(), original)
        derived = json.loads((state / 'collector-sources.json').read_text())
        self.assertEqual(derived['ledger_root'], str(self.ledger.root))
        self.assertEqual(len(sent), 1)
        self.assertEqual({(v['period'],v['dimension']) for v in sent[0]['views']},
            {(p,'activity') for p in ('today','week','month')})
        self.assertEqual(len({v['watermark'] for v in sent[0]['views']}), 1)
        self.assertEqual(sent[0]['sha256'], digest({k:v for k,v in sent[0].items() if k != 'sha256'}))

    def test_refresh_without_destination_publishes_locally_and_succeeds(self):
        state = self.root / 'local-state'
        state.mkdir(mode=0o700)
        registry = self.root / 'local-sources.json'
        refresh.private_write(registry, json.dumps(self.registry([])).encode())
        revision = 'e' * 40
        (self.root / '.stats-source-revision').write_text(revision)
        config = state / 'refresh.json'
        refresh.private_write(config, json.dumps({'source_revision': revision, 'paused_peers': {},
            'peers': [{'id': 'tcloud', 'host': 'tcloud-node2', 'python': '/python', 'script': '/script'}]}).encode())
        commands = []
        def transport(peer, command, data=None):
            commands.append((peer['host'], 'export' if 'export-framed-gzip' in command else command[-1]))
            return refresh.receipt_transport.encode(list(complete()), int(command[command.index('--prefix-bytes')+1]),
                command[command.index('--prefix-sha256')+1])
        with patch.multiple(refresh, STATE=state, REGISTRY=registry, ROOT=self.root), \
             patch.object(refresh, 'ssh', side_effect=transport), \
             patch.object(refresh, 'fetch_openrouter_models', return_value=CATALOG):
            refresh.refresh(config)
        # Only the peer receipt read crosses SSH; nothing is pushed anywhere.
        self.assertEqual(commands, [('tcloud-node2', 'export')])
        report = json.loads((state / 'last-refresh.json').read_text())
        self.assertEqual(report['destination'], 'not_configured')
        self.assertEqual(report['peers'], [{'id': 'tcloud', 'status': 'read'}])
        dashboard = json.loads((state / 'cost-dashboard-v1.json').read_text())
        self.assertEqual(dashboard['source_revision'], revision)

    def test_destination_push_is_skipped_only_when_unconfigured(self):
        with patch.object(refresh, 'ssh', side_effect=AssertionError('no destination')):
            self.assertEqual(refresh.push_destination({'peers': []}, 'receive', b'{}'), 'not_configured')
        with patch.object(refresh, 'ssh', return_value=b'') as ssh:
            self.assertEqual(refresh.push_destination({'destination': {'host': 'h', 'python': '/p', 'script': '/s'}},
                                                      'receive', b'{}'), 'published')
        self.assertEqual(ssh.call_args.args[1], ['/p', '/s', 'receive'])

    def price_refresh(self, declared=None, fail_fetch=False):
        state = self.root / 'price-state'; state.mkdir(mode=0o700)
        registry = self.root / 'price-sources.json'
        value = self.registry([])
        if declared is not None: value['catalog_snapshot'] = str(declared)
        refresh.private_write(registry, json.dumps(value).encode())
        cache = state / 'openrouter-catalog.json'
        old = {'retrieved_at': AT, 'catalog': CATALOG}
        refresh.private_write(cache, json.dumps(old).encode())
        revision = 'a' * 40; (self.root / '.stats-source-revision').write_text(revision)
        config = state / 'refresh.json'
        refresh.private_write(config, json.dumps({'source_revision': revision, 'peers': [],
            'destination': {'host': 'fixture', 'python': '/python', 'script': '/script'}}).encode())
        before = registry.read_bytes(), cache.read_bytes()
        def publish(args, *, row_contexts_ready):
            self.assertNotIn('--catalog-snapshot', args)
            selected = json.loads(Path(args[args.index('--source-registry')+1]).read_text())
            self.assertEqual(selected['catalog_snapshot'], str(declared or cache))
            Path(args[args.index('--output')+1]).write_text('{}')
            return 0
        if fail_fetch:
            import os
            os.utime(cache, (0, 0))
        with patch.multiple(refresh, STATE=state, REGISTRY=registry, ROOT=self.root), \
             patch.object(refresh, 'ssh', return_value=b'') as transport, \
             patch.object(refresh, 'publish', side_effect=publish) as publisher, \
             patch.object(refresh, 'fetch_openrouter_models', side_effect=ValueError('offline')) as fetch:
            if declared is not None and not declared.exists():
                with self.assertRaises(ValueError): refresh.refresh(config)
                transport.assert_not_called(); publisher.assert_not_called()
            else:
                refresh.refresh(config)
                publisher.assert_called_once()
            if declared is None: fetch.assert_called_once()
            else: fetch.assert_not_called()
        self.assertEqual((registry.read_bytes(), cache.read_bytes()), before)

    def test_refresh_honors_registered_catalog_without_repricing_it(self):
        pinned = self.root / 'approved-price.json'
        refresh.private_write(pinned, json.dumps({'retrieved_at': AT, 'catalog': CATALOG}).encode())
        before = pinned.read_bytes()
        self.price_refresh(declared=pinned)
        self.assertEqual(pinned.read_bytes(), before)

    def test_missing_registered_catalog_does_not_fall_back_to_managed_cache(self):
        self.price_refresh(declared=self.root / 'missing-price.json')

    def test_fresh_managed_catalog_still_attempts_current_model_prices(self):
        self.price_refresh()  # Cache mtime is now; the normal cycle must fetch once.

    def test_offline_refresh_preserves_saved_price_time_and_registers_it_for_consumer(self):
        self.price_refresh(fail_fetch=True)


class PeerInputRetirementTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state = Path(self.tmp.name)
        self.state.chmod(0o700)
        self.target = self.state / 'imports' / 'peer.jsonl'
        self.fallback = self.target.with_suffix('.last-good')
        refresh.private_write(self.target, b'same canonical transport\n' * 10)
        refresh.private_write(self.fallback, self.target.read_bytes())
        self.publication = {'destination':'published','source_revision':'a'*40,
                            'generated_at':'2026-09-30T06:00:00+00:00',
                            'peers':[{'id':'peer','status':'read'}]}
        refresh.private_write(self.state / 'last-refresh.json', json.dumps(self.publication).encode())
        self.state_patch = patch.object(refresh, 'STATE', self.state)
        self.state_patch.start(); self.addCleanup(self.state_patch.stop)
        self.refs_patch = patch.object(refresh, '_no_input_references')
        self.references = self.refs_patch.start(); self.addCleanup(self.refs_patch.stop)

    def stages(self):
        return list(self.target.parent.glob('.peer-retire-peer-*'))

    def test_identical_copy_retired_with_durable_receipt_and_current_cursor(self):
        before = refresh._input_identity(self.target)
        result = refresh.retire_duplicate_fallback('peer')
        self.assertEqual(result['status'], 'retired')
        self.assertFalse(self.fallback.exists())
        self.assertEqual(refresh._input_identity(self.target), before)
        self.assertEqual(refresh.receipt_transport.cursor(self.target)[0], self.target)
        self.assertEqual(self.stages(), [])
        saved = list((self.state / 'peer-input-retirement').glob('*.json'))
        self.assertEqual(len(saved), 1)
        self.assertEqual(json.loads(saved[0].read_text()), json.loads(json.dumps(result)))
        self.assertEqual(self.references.call_count, 2)
        self.assertEqual(result['publication'], self.publication)

    def test_different_fallback_is_preserved(self):
        refresh.private_write(self.fallback, b'different\n')
        self.assertEqual(refresh.retire_duplicate_fallback('peer')['status'], 'different')
        self.assertEqual(self.fallback.read_bytes(), b'different\n')
        self.assertEqual(self.stages(), [])

    def test_symlinks_and_hardlinks_are_preserved(self):
        self.fallback.unlink(); self.fallback.symlink_to(self.target)
        self.assertEqual(refresh.retire_duplicate_fallback('peer')['status'], 'protected')
        self.assertTrue(self.fallback.is_symlink())
        self.fallback.unlink(); os.link(self.target, self.fallback)
        self.assertEqual(refresh.retire_duplicate_fallback('peer')['status'], 'protected')
        self.assertTrue(self.fallback.exists())
        self.assertEqual(self.stages(), [])

    def test_active_or_unknown_references_protect(self):
        self.references.side_effect = ValueError('active or unknown')
        self.assertEqual(refresh.retire_duplicate_fallback('peer')['status'], 'protected')
        self.assertTrue(self.fallback.exists())
        self.assertEqual(self.stages(), [])

    def test_mutation_during_reference_check_protects(self):
        self.references.side_effect = lambda *p: refresh.private_write(self.fallback, b'changed')
        self.assertEqual(refresh.retire_duplicate_fallback('peer')['status'], 'protected')
        self.assertEqual(self.fallback.read_bytes(), b'changed')
        self.assertEqual(self.stages(), [])

    def test_failure_after_staging_preserves_and_blocks_next_retirement(self):
        self.references.side_effect = [None, ValueError('unknown after staging')]
        result = refresh.retire_duplicate_fallback('peer')
        self.assertEqual(result['status'], 'protected')
        self.assertFalse(self.fallback.exists())
        self.assertEqual(len(self.stages()), 1)
        self.assertEqual((self.stages()[0] / 'fallback').read_bytes(), self.target.read_bytes())
        self.references.side_effect = None
        self.assertEqual(refresh.retire_duplicate_fallback('peer')['status'], 'protected')
        refresh.private_write(self.fallback, self.target.read_bytes())
        self.references.side_effect = None
        self.assertEqual(refresh.retire_duplicate_fallback('peer')['status'], 'protected')
        self.assertTrue(self.fallback.exists())

    def test_reappearing_original_path_cannot_be_removed_or_overwritten(self):
        calls = 0
        def check(*paths):
            nonlocal calls
            calls += 1
            if calls == 2: refresh.private_write(self.fallback, b'new input')
        self.references.side_effect = check
        self.assertEqual(refresh.retire_duplicate_fallback('peer')['status'], 'protected')
        self.assertEqual(self.fallback.read_bytes(), b'new input')
        self.assertTrue((self.stages()[0] / 'fallback').exists())

    def test_next_failure_recreates_fallback_without_losing_current_bytes(self):
        data = self.target.read_bytes()
        self.assertEqual(refresh.retire_duplicate_fallback('peer')['status'], 'retired')
        refresh.install_peer_input(self.target, None)
        self.assertFalse(self.target.exists())
        self.assertEqual(self.fallback.read_bytes(), data)
        self.assertEqual(refresh.receipt_transport.cursor(self.target)[0], self.fallback)

    def test_lsof_requires_known_no_reference_result(self):
        self.refs_patch.stop()
        for code, out, err in [(0,b'p123',b''),(0,b'',b''),(1,b'',b'warning'),(2,b'',b'')]:
            with self.subTest(code=code,out=out,err=err), patch.object(refresh.subprocess, 'run', return_value=SimpleNamespace(returncode=code,stdout=out,stderr=err)):
                with self.assertRaises(ValueError): refresh._no_input_references(self.target)
        with patch.object(refresh.subprocess,'run',return_value=SimpleNamespace(returncode=1,stdout=b'',stderr=b'')):
            refresh._no_input_references(self.target)
        with patch.object(refresh.subprocess,'run',side_effect=subprocess.TimeoutExpired('lsof',15)):
            self.assertEqual(refresh.retire_duplicate_fallback('peer')['status'], 'protected')

    def test_private_directory_and_peer_identity_are_required(self):
        for peer in ('../peer', 'peer.jsonl', ''):
            self.assertEqual(refresh.retire_duplicate_fallback(peer)['status'], 'protected')
        self.target.parent.chmod(0o755)
        self.assertEqual(refresh.retire_duplicate_fallback('peer')['status'], 'protected')
        self.assertTrue(self.fallback.exists())
        self.assertEqual(self.stages(), [])

    def test_stage_directory_replacement_does_not_overwrite_other_assets(self):
        victim = self.state / 'protected'; victim.mkdir(mode=0o700)
        (victim/'fallback').write_bytes(b'protected unrelated bytes')
        original_write = refresh.private_write
        def intent(path, data):
            original_write(path, data)
            if path.parent.name == 'peer-input-retirement':
                stage = self.stages()[0]
                if not stage.is_symlink():
                    stage.rename(stage.with_name(stage.name+'-saved'))
                    stage.symlink_to(victim, target_is_directory=True)
        with patch.object(refresh,'private_write',side_effect=intent):
            self.assertEqual(refresh.retire_duplicate_fallback('peer')['status'],'protected')
        self.assertEqual((victim/'fallback').read_bytes(),b'protected unrelated bytes')
        self.assertTrue(self.fallback.exists())

    def test_exclusive_rename_cannot_overwrite_inserted_stage_entry(self):
        original_rename = refresh._rename_exclusive
        def insertion(name, parent_fd, stage_fd):
            fd = os.open('fallback',os.O_CREAT|os.O_EXCL|os.O_WRONLY,0o600,dir_fd=stage_fd)
            with os.fdopen(fd,'wb') as f:f.write(b'protected stage entry')
            original_rename(name,parent_fd,stage_fd)
        with patch.object(refresh,'_rename_exclusive',side_effect=insertion):
            self.assertEqual(refresh.retire_duplicate_fallback('peer')['status'],'protected')
        self.assertTrue(self.fallback.exists())
        self.assertEqual((self.stages()[0]/'fallback').read_bytes(),b'protected stage entry')

    def test_unsuccessful_publication_never_authorizes_retirement(self):
        self.publication['peers'][0]['status']='unreachable_or_invalid'
        refresh.private_write(self.state/'last-refresh.json',json.dumps(self.publication).encode())
        self.assertEqual(refresh.retire_duplicate_fallback('peer')['status'],'protected')
        self.assertTrue(self.fallback.exists())
        self.assertEqual(self.stages(),[])

    def test_unknown_publication_shapes_protect_without_raising(self):
        for publication in (None, [], {**self.publication,'peers':[None]},
                            {**self.publication,'peers':{}},
                            {**self.publication,'source_revision':None}):
            with self.subTest(publication=publication):
                refresh.private_write(self.state/'last-refresh.json',json.dumps(publication).encode())
                self.assertEqual(refresh.retire_duplicate_fallback('peer')['status'],'protected')
                self.assertTrue(self.fallback.exists())
                self.assertEqual(self.stages(),[])



if __name__ == '__main__':
    unittest.main()


class ReceiptDeltaTests(unittest.TestCase):
    def setUp(self):
        from hashlib import sha256
        self.sha = lambda value: sha256(value).hexdigest()
        self.data = b''.join(canonical(r.to_dict())+b'\n' for r in complete())
        self.first = self.data.splitlines(keepends=True)[0]

    def test_framed_append_serializes_once_and_validates_only_new_frames(self):
        rows=list(complete()); first=canonical(rows[0].to_dict())+b'\n'
        with tempfile.TemporaryDirectory() as temp:
            target=Path(temp)/'framed.jsonl';refresh.private_write(target,first)
            base_path,size,fingerprint=refresh.receipt_transport.cursor(target)
            original_line=refresh.receipt_transport._line
            with patch.object(refresh.receipt_transport,'_line',wraps=original_line) as line:
                wire=refresh.receipt_transport.encode(rows,size,fingerprint)
            self.assertEqual(line.call_count,len(rows),'healthy append must serialize each source Receipt once')
            original_validate=refresh.receipt_transport._validate_lines
            with patch.object(refresh.receipt_transport,'_validate_lines',wraps=original_validate) as validate:
                result=refresh.receipt_transport.install(target,wire,base_path)
            self.assertEqual(result['retained_bytes'],len(first))
            self.assertEqual(validate.call_count,result['frames'],
                             'already verified prefix bytes must not be reparsed as Receipt JSON')
            self.assertEqual(target.read_bytes(),self.data)

    def test_framed_install_accepts_append_after_proved_cursor_but_not_prefix_mutation(self):
        rows=list(complete()); first=canonical(rows[0].to_dict())+b'\n'
        with tempfile.TemporaryDirectory() as temp:
            target=Path(temp)/'framed.jsonl';refresh.private_write(target,first)
            base_path,size,fingerprint=refresh.receipt_transport.cursor(target)
            wire=refresh.receipt_transport.encode(rows,size,fingerprint)
            # Simulate an independent natural refresh appending after the UAT
            # captured its cursor. The first N bytes are still the same proof.
            refresh.private_write(target,self.data)
            result=refresh.receipt_transport.install(target,wire,base_path)
            self.assertEqual(result['retained_bytes'],len(first))
            self.assertEqual(target.read_bytes(),self.data)

            refresh.private_write(target,b'X'+self.data[1:])
            with self.assertRaises(ValueError):
                refresh.receipt_transport.install(target,wire,target)

    def test_existing_full_export_bootstraps_without_resending_prefix(self):
        wire=refresh.receipt_delta(self.data,len(self.first),self.sha(self.first))
        self.assertEqual(refresh.apply_receipt_delta(self.first,wire),self.data)
        head,_,tail=refresh.decompress_gzip_receipts(wire).partition(b'\n')
        self.assertEqual(tail,self.data[len(self.first):])
        self.assertEqual(json.loads(head)['mode'],'append')

    def test_unchanged_source_transmits_no_receipts(self):
        wire=refresh.receipt_delta(self.data,len(self.data),self.sha(self.data))
        self.assertEqual(refresh.apply_receipt_delta(self.data,wire),self.data)
        head,_,tail=refresh.decompress_gzip_receipts(wire).partition(b'\n')
        self.assertEqual(tail,b'');self.assertLess(len(wire),512)

    def test_wrong_prefix_gets_full_validated_reset(self):
        wire=refresh.receipt_delta(self.data,len(self.first),'f'*64)
        self.assertEqual(refresh.apply_receipt_delta(self.first,wire),self.data)
        self.assertEqual(json.loads(refresh.decompress_gzip_receipts(wire).splitlines()[0])['mode'],'replace')

    def test_append_cannot_apply_to_another_base(self):
        wire=refresh.receipt_delta(self.data,len(self.first),self.sha(self.first))
        with self.assertRaises(ValueError):refresh.apply_receipt_delta(b'wrong',wire)

    def test_truncated_delta_never_changes_previous_file(self):
        wire=refresh.receipt_delta(self.data,len(self.first),self.sha(self.first))
        with tempfile.TemporaryDirectory() as temp:
            target=Path(temp)/'peer.jsonl';refresh.private_write(target,self.first)
            with self.assertRaises(ValueError):
                refresh.install_peer_input(target,refresh.apply_receipt_delta(target.read_bytes(),wire[:-8]))
            self.assertEqual(target.read_bytes(),self.first)

    def test_failure_then_resume_uses_last_good_without_claiming_success(self):
        with tempfile.TemporaryDirectory() as temp:
            target=Path(temp)/'peer.jsonl';refresh.private_write(target,self.first)
            refresh.install_peer_input(target,None)
            self.assertFalse(target.exists())
            base=refresh.peer_prefix(target);self.assertEqual(base,self.first)
            full=refresh.apply_receipt_delta(base,refresh.receipt_delta(self.data,len(base),self.sha(base)))
            refresh.private_write(target,full);self.assertEqual(target.read_bytes(),self.data)

    def test_bad_delta_digest_and_noncanonical_facts_rejected(self):
        wire=refresh.receipt_delta(self.data,0,self.sha(b''))
        frame=refresh.decompress_gzip_receipts(wire);head,_,tail=frame.partition(b'\n')
        h=json.loads(head);h['full_sha256']='0'*64
        with self.assertRaises(ValueError):refresh.apply_receipt_delta(b'',gzip.compress(canonical(h)+b'\n'+tail))
        bad=b'{"not":"receipt"}\n'
        with self.assertRaises(ValueError):refresh.apply_receipt_delta(b'',refresh.receipt_delta(bad,0,self.sha(b'')))

    def test_deleted_or_corrupt_import_requires_full_rebuild(self):
        with tempfile.TemporaryDirectory() as temp:
            target=Path(temp)/'peer.jsonl';self.assertEqual(refresh.peer_prefix(target),b'')
            refresh.private_write(target,b'{broken')
            self.assertEqual(refresh.peer_prefix(target),b'')


class AbortableCycleTests(unittest.TestCase):
    """A native switch waits only for durable shared writes, never the whole cycle."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state = Path(self.tmp.name)
        self.state.chmod(0o700)

    def held(self, name):
        import fcntl
        with (self.state/name).open('a+b') as probe:
            try:
                fcntl.flock(probe, fcntl.LOCK_EX|fcntl.LOCK_NB)
            except BlockingIOError:
                return True
            fcntl.flock(probe, fcntl.LOCK_UN)
            return False

    def test_cycle_declares_itself_and_holds_commit_lock_only_for_shared_writes(self):
        revision = 'a'*40
        (self.state/'.stats-source-revision').write_text(revision)
        config = self.state/'refresh.json'
        peer = {'id': 'tcloud', 'host': 'fixture', 'python': '/python', 'script': '/script'}
        refresh.private_write(config, json.dumps({'source_revision': revision, 'peers': [peer],
                                                  'paused_peers': {}}).encode())
        registry = self.state/'sources.json'
        refresh.private_write(registry, json.dumps({'sources': [], 'ledger_root': str(self.state/'ledger')}).encode())
        (self.state/'imports').mkdir(mode=0o700)
        stale = self.state/'imports'/'.receipt-stage-stopped'
        stale.write_bytes(b'partial copy of a stopped cycle')
        observed = []
        def phase(name):
            observed.append((name, self.held('cost-refresh.lock'), self.held('cost-refresh-commit.lock')))
        def transport(peer, command, data=None):
            phase('peer_read'); return b'wire'
        def install(target, data, base):
            phase('peer_install'); return {'frames': 0, 'retained_bytes': 0, 'received_facts_bytes': 0}
        def publication(args, *, row_contexts_ready):
            phase('collection')
            self.assertIs(UsageLedger.append_section, refresh.commit_section)
            with UsageLedger.append_section():
                phase('ledger_append')
            phase('views')
            refresh.private_write(self.state/'cost-dashboard-v1.json', b'{}')
            return 0
        def retirement(peer_id):
            phase('report_and_retirement'); return {'status': 'absent'}
        with patch.multiple(refresh, STATE=self.state, ROOT=self.state, REGISTRY=registry), \
             patch.object(refresh, 'fetch_openrouter_models', return_value=CATALOG), \
             patch.object(refresh, 'publish', side_effect=publication), \
             patch.object(refresh, 'ssh', side_effect=transport), \
             patch.object(refresh.receipt_transport, 'install', side_effect=install), \
             patch.object(refresh, 'retire_duplicate_fallback', side_effect=retirement):
            refresh.refresh(config)
        self.assertEqual(observed, [('peer_read', True, False), ('peer_install', True, True),
            ('collection', True, False), ('ledger_append', True, True), ('views', True, False),
            ('report_and_retirement', True, True)])
        declaration = json.loads((self.state/'cost-refresh-cycle.json').read_bytes())
        self.assertEqual(declaration['schema'], 'pulse_stats_refresh_cycle/v1')
        self.assertEqual(declaration['pid'], os.getpid())
        self.assertFalse(stale.exists())
        self.assertIsNone(UsageLedger.append_section)
        self.assertFalse(self.held('cost-refresh.lock') or self.held('cost-refresh-commit.lock'))



class WorkItemFactsPeerTests(unittest.TestCase):
    """A shared-ledger peer gets the session writes its holder supplement needs."""

    def run_refresh(self, ssh_effect=None, paused=None):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        registry = Path(tmp.name) / 'sources.json'
        registry.write_text(json.dumps({'observation_core_entrypoint': '/installed/harness.pyz'}))
        shared = {'id': 'tcloud', 'host': 'peer', 'python': '/python', 'script': '/script', 'transport': 'shared_ledger'}
        receipts = {'id': 'old', 'host': 'peer2', 'python': '/python', 'script': '/script'}
        done = subprocess.CompletedProcess([], 0, stdout=b'{"facts": true}')
        with patch.object(refresh, 'REGISTRY', registry), \
             patch.object(refresh.subprocess, 'run', return_value=done), \
             patch.object(refresh, 'receive_wi_facts') as receive, \
             patch.object(refresh, 'progress') as progress, \
             patch.object(refresh, 'ssh', side_effect=ssh_effect, return_value=b'') as ssh:
            status = refresh.refresh_wi_facts({'peers': [shared, receipts],
                                               **({'paused_peers': {}} if paused is None else paused)})
        return status, receive, ssh, progress

    def test_shared_ledger_peer_receives_the_same_facts(self):
        status, receive, ssh, _ = self.run_refresh()
        self.assertEqual(status, 'published')
        receive.assert_called_once_with(b'{"facts": true}')
        self.assertEqual([c.args[0]['id'] for c in ssh.call_args_list], ['tcloud'])
        self.assertEqual(ssh.call_args.args[1][-1], 'receive-wi-facts')
        self.assertEqual(ssh.call_args.args[2], b'{"facts": true}')

    def test_paused_peer_gets_no_facts(self):
        # Absent `paused_peers` is the 2026-11-15 default: TCloud is not reached.
        for paused in ({}, {'paused_peers': {'tcloud': 'paused_by_decision'}}):
            with self.subTest(paused=paused):
                status, receive, ssh, _ = self.run_refresh(paused=paused)
                self.assertEqual(status, 'published')
                receive.assert_called_once_with(b'{"facts": true}')
                ssh.assert_not_called()

    def test_peer_failure_keeps_the_local_publication(self):
        status, _, _, progress = self.run_refresh(ssh_effect=RuntimeError('peer down'))
        self.assertEqual(status, 'published')
        progress.assert_called_once_with('peer_wi_facts_failed', peer='tcloud', error_type='RuntimeError')


class PullRequestAuditRefreshTests(unittest.TestCase):
    """GitHub is read at most once per interval; an unreadable GitHub keeps the old cache."""

    def test_cache_is_written_privately_then_reused(self):
        from pulse_compute_cost import pull_request_audit
        tmp = tempfile.TemporaryDirectory(); self.addCleanup(tmp.cleanup)
        state = Path(tmp.name) / 'state'
        row = {'repository': 'bigbird-online/PULSE', 'merges': 'read', 'commits': 'read', 'reason': None,
               'merged_pull_requests': [], 'pr_commits': [], 'direct_commits': []}
        calls = []

        def collect(start, now):
            calls.append(now)
            return {'schema': pull_request_audit.SCHEMA, 'observed_at': now.isoformat(), 'since': now.isoformat(),
                    'window_days': 7, 'repositories': [row]}
        with patch.object(refresh, 'STATE', state), patch.object(pull_request_audit, 'collect', side_effect=collect):
            self.assertEqual(REAL_PR_AUDIT(), 'read')
            self.assertEqual(REAL_PR_AUDIT(), 'cached')
            self.assertEqual(len(calls), 1)
            target = state / refresh.PR_AUDIT
            self.assertEqual(target.stat().st_mode & 0o077, 0)
            before = target.read_bytes()
            target.write_text('{}')  # Unreadable cache: read again.
            target.chmod(0o600)
            down = {**row, 'merges': 'unverified', 'commits': 'unverified'}
            with patch.object(pull_request_audit, 'collect', return_value={'repositories': [down]}):
                self.assertEqual(REAL_PR_AUDIT(), 'github_unavailable')
            self.assertEqual(target.read_text(), '{}')
            self.assertNotEqual(before, b'{}')

    def test_pr_bodies_merge_into_the_previous_cache_and_keep_it_when_github_is_down(self):
        from pulse_compute_cost import pull_request_bodies
        tmp = tempfile.TemporaryDirectory(); self.addCleanup(tmp.cleanup)
        state = Path(tmp.name) / 'state'
        seen = []

        def collect(start, now, previous):
            seen.append(previous)
            stamp = now.strftime('%Y-%m-%dT%H:%M:%SZ')
            return {'schema': pull_request_bodies.SCHEMA, 'observed_at': stamp, 'repositories': [
                {'repository': 'bigbird-online/PULSE', 'status': 'read', 'reason': None, 'observed_at': stamp,
                 'covered_since': stamp, 'pull_requests': []}]}
        with patch.object(refresh, 'STATE', state), patch.object(pull_request_bodies, 'collect', side_effect=collect):
            self.assertEqual(REAL_PR_BODIES(), 'read')
            self.assertEqual(REAL_PR_BODIES(), 'cached')
            target = state / refresh.PR_BODIES
            self.assertEqual(target.stat().st_mode & 0o077, 0)
            self.assertIsNone(seen[0])
            written = json.loads(target.read_text())
            # Due again: the previous cache is handed to the collector to merge into.
            written['observed_at'] = '2026-01-01T00:00:00Z'
            target.write_text(json.dumps(written))
            self.assertEqual(REAL_PR_BODIES(), 'read')
            self.assertEqual(seen[1]['observed_at'], '2026-01-01T00:00:00Z')
            target.write_text(json.dumps(written))
            down = {'schema': pull_request_bodies.SCHEMA, 'observed_at': '2026-11-10T00:00:00Z', 'repositories': [
                {'repository': 'bigbird-online/PULSE', 'status': 'unverified', 'reason': 'github_unavailable',
                 'observed_at': '2026-01-01T00:00:00Z', 'covered_since': '2026-01-01T00:00:00Z',
                 'pull_requests': []}]}
            with patch.object(pull_request_bodies, 'collect', return_value=down):
                self.assertEqual(REAL_PR_BODIES(), 'github_unavailable')
            self.assertEqual(json.loads(target.read_text()), written)


# Runs the real collect-shared entry in its own process: the guard installs
# signal handlers, an alarm and resource limits that must not touch this one.
COLLECT_DRIVER = r'''
import importlib.util, os, sys, time
from pathlib import Path
script, registry, behavior, deadline = sys.argv[1:5]
spec = importlib.util.spec_from_file_location('stats_cost_refresh', script)
refresh = importlib.util.module_from_spec(spec); spec.loader.exec_module(refresh)
refresh.REGISTRY = Path(registry)
refresh.COLLECT_SHARED_DEADLINE_SECONDS = int(deadline)
refresh.COLLECT_SHARED_WATCH_SECONDS = 0.1
refresh.COLLECT_SHARED_DATA_BYTES = 512 * 1024 * 1024
refresh.COLLECT_SHARED_DATA_CEILING_BYTES = 512 * 1024 * 1024
started = Path(registry).parent / ('started-%d' % os.getpid())
def collect(*, force_recompute=False):
    started.write_text('')
    if behavior == 'hog':
        hold = []
        while True:
            hold.append(bytearray(64 * 1024 * 1024))
    if behavior == 'wrapped-memory':
        try:
            raise MemoryError()
        except MemoryError as exc:
            raise refresh.ReceiptError('production observations unavailable') from exc
    if behavior == 'sleep':
        time.sleep(60)
    return b'{"receipt":true}\n'
refresh.ReceiptError = __import__('pulse_compute_cost.receipts', fromlist=['ReceiptError']).ReceiptError
refresh.shared_collection_receipt = collect
sys.argv = ['pulse-stats-cost-refresh.py', 'collect-shared']
sys.exit(refresh.main())
'''

# Spawns the collection and exits once it runs: the collection loses its
# parent while its stdout (a file) stays open.
ORPHAN_DRIVER = r'''
import subprocess, sys, time
from pathlib import Path
with open(sys.argv[1], 'wb') as out, open(sys.argv[2], 'wb') as err:
    child = subprocess.Popen(sys.argv[4:], stdout=out, stderr=err, stdin=subprocess.DEVNULL)
marker = Path(sys.argv[3]) / ('started-%d' % child.pid)
for _ in range(300):
    if marker.exists():
        break
    time.sleep(0.1)
print(child.pid, flush=True)
'''


class SharedCollectionGuardTests(unittest.TestCase):
    """Peer side of collect-shared: one instance, bounded, never orphaned."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory(); self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name); self.root.chmod(0o700)
        self.state = self.root / 'compute-cost'; self.state.mkdir(mode=0o700)
        self.registry = self.state / 'sources.json'
        self.driver = self.root / 'driver.py'; self.driver.write_text(COLLECT_DRIVER)
        self.processes = []

    def tearDown(self):
        for process in self.processes:
            if process.poll() is None:
                process.kill()
            process.wait(10)
            for stream in (process.stdout, process.stderr):
                if stream is not None:
                    stream.close()

    def argv(self, behavior, deadline=60):
        return [sys.executable, '-I', '-B', str(self.driver),
                str(ROOT / 'Scripts/pulse-stats-cost-refresh.py'), str(self.registry), behavior, str(deadline)]

    def start(self, behavior, deadline=60):
        process = subprocess.Popen(self.argv(behavior, deadline), stdin=subprocess.DEVNULL,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.processes.append(process)
        marker = self.state / ('started-%d' % process.pid)
        for _ in range(300):
            if marker.exists() or process.poll() is not None:
                break
            threading.Event().wait(0.1)
        self.assertTrue(marker.exists(), 'collection did not start')
        return process

    def run_once(self, behavior, deadline=60, timeout=20):
        return subprocess.run(self.argv(behavior, deadline), stdin=subprocess.DEVNULL,
                              capture_output=True, timeout=timeout)

    @staticmethod
    def stages(stderr):
        return [json.loads(line)['stage'] for line in stderr.decode().splitlines() if line.startswith('{')]

    def test_second_collection_is_busy_at_once_and_mac_reports_peer_busy(self):
        first = self.start('sleep')
        second = self.run_once('fast', timeout=20)
        self.assertEqual(second.returncode, refresh.COLLECT_SHARED_BUSY_EXIT)
        self.assertEqual(second.stdout, b'')
        self.assertIn('collect_shared_busy', self.stages(second.stderr))
        lock = self.state / refresh.COLLECT_SHARED_LOCK
        self.assertEqual(lock.stat().st_mode & 0o777, 0o600)
        # The Mac side sees the remote exit status through ssh().
        def transport(peer, command, data=None):
            result = self.run_once('fast')
            if result.returncode:
                raise refresh.PeerTransportError(result.returncode)
            return result.stdout
        cycle = CostRefreshTests('test_shared_ledger_peer_collects_in_place_and_ships_no_facts_back')
        cycle.setUp(); self.addCleanup(cycle.doCleanups)
        import contextlib, io
        log = io.StringIO()
        with contextlib.redirect_stderr(log):
            state, _ = cycle.shared_refresh(transport)
        stages = self.stages(log.getvalue().encode())
        self.assertIn('peer_busy', stages)
        self.assertNotIn('peer_read_failed', stages)
        report = json.loads((state / 'last-refresh.json').read_bytes())
        self.assertEqual(report['peers'], [{'id': 'peer', 'status': 'unreachable_or_invalid', 'reason': 'peer_busy'}])
        # The lock dies with its holder; the next collection runs.
        first.kill(); first.wait(10)
        third = self.run_once('fast')
        self.assertEqual((third.returncode, third.stdout), (0, b'{"receipt":true}\n'))

    def test_lock_refuses_symlink(self):
        (self.state / refresh.COLLECT_SHARED_LOCK).symlink_to(self.root / 'elsewhere')
        result = self.run_once('fast')
        self.assertEqual(result.returncode, 2)
        self.assertFalse((self.root / 'elsewhere').exists())

    def test_wrapped_memory_error_is_an_explicit_failure(self):
        result = self.run_once('wrapped-memory')
        self.assertEqual(result.returncode, refresh.COLLECT_SHARED_MEMORY_EXIT)
        self.assertEqual(result.stdout, b'')
        self.assertIn('command_failed', self.stages(result.stderr))
        self.assertNotIn(b'Traceback', result.stderr)

    def test_data_limit_follows_the_node_memory(self):
        module = refresh
        gib = 1024 ** 3
        def limit(text):
            path = self.root / 'meminfo'; path.write_text(text)
            return module.collect_shared_data_bytes(path)
        self.assertEqual(limit('MemTotal:        3827544 kB\n'), 3328 * 1024 ** 2)   # TCloud keeps the floor
        self.assertEqual(limit('MemTotal:       12211700 kB\n'), int(12211700 * 1024 * 0.6))  # Oracle: ~7 GiB
        self.assertEqual(limit('MemTotal:       33554432 kB\n'), 7 * gib)            # larger nodes stop at the ceiling
        self.assertEqual(limit('MemFree: 1 kB\n'), 3328 * 1024 ** 2)
        self.assertEqual(module.collect_shared_data_bytes(self.root / 'missing'), 3328 * 1024 ** 2)

    @unittest.skipUnless(sys.platform.startswith('linux'), 'RLIMIT_DATA is enforced only on Linux')
    def test_data_limit_stops_runaway_collection(self):
        result = self.run_once('hog', timeout=60)
        self.assertEqual(result.returncode, refresh.COLLECT_SHARED_MEMORY_EXIT)
        self.assertNotIn(b'Traceback', result.stderr)

    def test_deadline_ends_the_collection(self):
        result = self.run_once('sleep', deadline=1, timeout=20)
        self.assertEqual(result.returncode, refresh.COLLECT_SHARED_DEADLINE_EXIT)
        self.assertEqual(result.stdout, b'')
        self.assertIn('collect_shared_stopped', self.stages(result.stderr))

    def test_closed_ssh_reader_ends_the_collection(self):
        process = self.start('sleep')
        process.stdout.close()  # sshd exited: nobody can receive the receipt.
        self.assertEqual(process.wait(20), refresh.COLLECT_SHARED_DETACHED_EXIT)

    def test_lost_parent_ends_the_collection(self):
        orphan = self.root / 'orphan.py'; orphan.write_text(ORPHAN_DRIVER)
        out, err = self.root / 'out', self.root / 'err'
        parent = subprocess.run([sys.executable, '-I', '-B', str(orphan), str(out), str(err), str(self.state),
                                 *self.argv('sleep')], capture_output=True, timeout=20, check=True)
        pid = int(parent.stdout)
        for _ in range(200):
            if b'parent_exited' in err.read_bytes():
                break
            threading.Event().wait(0.1)
        else:
            os.kill(pid, 9)
            self.fail('orphaned collection kept running')
        for _ in range(100):
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                break
            threading.Event().wait(0.1)
        else:
            self.fail('orphaned collection did not exit')
        self.assertEqual(out.read_bytes(), b'')


class RefreshScheduleAndStatusTests(unittest.TestCase):
    """Keep-last presentation: when cycles start and what the App is told."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state = Path(self.tmp.name)
        self.state.chmod(0o700)
        self.now = datetime(2026, 11, 4, 10, 0, tzinfo=timezone.utc)

    def publish(self, minutes_left, status='available'):
        from datetime import timedelta
        refresh.private_write(self.state / 'cost-dashboard-v1.json', json.dumps({
            'status': status, 'expires_at': (self.now + timedelta(minutes=minutes_left)).isoformat()}).encode())

    def status(self, **fields):
        refresh.private_write(self.state / refresh.REFRESH_STATUS, json.dumps({
            'schema': refresh.REFRESH_STATUS_SCHEMA, 'state': 'completed', **fields}).encode())

    def due(self):
        with patch.object(refresh, 'STATE', self.state):
            return refresh.refresh_due(self.now)

    def test_cycle_starts_when_the_snapshot_would_expire_before_it_lands(self):
        # Measured 2026-11-04: 23.6 min start-to-completion; lead 23.6 + 4 min.
        self.status(last_completed_seconds=1415)
        for left, expected in ((40, False), (28, False), (27, True), (5, True), (-10, True)):
            with self.subTest(minutes_left=left):
                self.publish(left)
                self.assertEqual(self.due(), expected)
        # Unknown length assumes the longest bounded cycle (70 + 4 min).
        (self.state / refresh.REFRESH_STATUS).unlink()
        self.publish(73)
        self.assertTrue(self.due())
        self.publish(75)
        self.assertFalse(self.due())

    def test_missing_or_amountless_publication_is_due_and_failure_backs_off(self):
        self.assertTrue(self.due())
        self.publish(40, status='unavailable')
        self.assertTrue(self.due())
        from datetime import timedelta
        self.publish(-5)
        self.status(state='failed', finished_at=(self.now - timedelta(minutes=3)).isoformat())
        self.assertFalse(self.due())
        self.status(state='failed', finished_at=(self.now - timedelta(minutes=11)).isoformat())
        self.assertTrue(self.due())

    def test_scheduled_tick_returns_before_any_work_when_not_due(self):
        self.status(last_completed_seconds=1200)
        self.publish(40)
        with patch.object(refresh, 'STATE', self.state), \
             patch.dict(os.environ, {refresh.SCHEDULE_VARIABLE: refresh.SCHEDULE_BEFORE_EXPIRY}), \
             patch.object(refresh, 'refresh_due', return_value=False) as due:
            # The configuration is never read: a missing file would raise.
            refresh.refresh(self.state / 'absent.json')
            self.assertEqual(due.call_count, 1)
            with self.assertRaises(FileNotFoundError):
                refresh.refresh(self.state / 'absent.json', force_recompute=True)

    def test_status_records_running_completion_and_carries_the_last_length(self):
        with patch.object(refresh, 'STATE', self.state):
            refresh.write_refresh_status('completed', self.now.isoformat(), duration_seconds=1300.5, peers=[])
            refresh.write_refresh_status('running', self.now.isoformat())
            running = refresh.read_refresh_status()
            self.assertEqual((running['state'], running['finished_at'], running['last_completed_seconds']),
                             ('running', None, 1300.5))
            refresh.write_refresh_status('failed', self.now.isoformat(), reason='production_ledger_unavailable')
            failed = refresh.read_refresh_status()
        self.assertEqual((failed['state'], failed['reason'], failed['last_completed_seconds']),
                         ('failed', 'production_ledger_unavailable', 1300.5))
        self.assertIsNotNone(failed['finished_at'])
        self.assertEqual(os.stat(self.state / refresh.REFRESH_STATUS).st_mode & 0o077, 0)

    def test_failed_cycle_names_the_publication_reason_and_keeps_the_old_snapshot(self):
        revision = 'f' * 40
        (self.state / '.stats-source-revision').write_text(revision)
        config = self.state / 'refresh.json'
        refresh.private_write(config, json.dumps({'source_revision': revision, 'peers': []}).encode())
        registry = self.state / 'sources.json'
        refresh.private_write(registry, json.dumps({'sources': [], 'ledger_root': str(self.state / 'ledger')}).encode())
        self.publish(-30)
        before = (self.state / 'cost-dashboard-v1.json').read_bytes()
        with patch.multiple(refresh, STATE=self.state, ROOT=self.state, REGISTRY=registry), \
             patch.object(refresh, 'fetch_openrouter_models', return_value=CATALOG), \
             patch.object(refresh, 'publish', return_value=2), \
             patch.object(refresh, 'last_failure_reason', return_value='production_ledger_unavailable'):
            with self.assertRaisesRegex(RuntimeError, 'cost publication failed'):
                refresh.refresh(config)
            status = refresh.read_refresh_status()
        self.assertEqual((status['state'], status['reason']), ('failed', 'production_ledger_unavailable'))
        self.assertEqual((self.state / 'cost-dashboard-v1.json').read_bytes(), before)

    def test_remote_stderr_summary_keeps_only_allow_listed_progress(self):
        stderr = b'\n'.join([
            b'{"schema": "stats_refresh_progress/v1", "stage": "command_failed", "mode": "collect-shared",'
            b' "error_type": "ReceiptError", "reason": "production_ledger_unavailable", "path": "/home/pulse/secret"}',
            b'{"schema": "stats_refresh_progress/v1", "stage": "x", "reason": "has space /etc/passwd"}',
            b'Traceback (most recent call last):',
            b'  File "/home/pulse/.local/share/x.py", line 1, in <module>',
            b'harness_rewrite.model.Rejected: Production Git operation failed https://token@github.com',
            b'fatal: unable to access https://x-access-token:SECRET@github.com/',
        ])
        summary = refresh.remote_summary(stderr)
        self.assertEqual(summary['records'][0], {'stage': 'command_failed', 'mode': 'collect-shared',
            'error_type': 'ReceiptError', 'reason': 'production_ledger_unavailable'})
        self.assertEqual(summary['records'][1], {'stage': 'x'})
        self.assertEqual((summary['exception'], summary['other_lines']), ('Rejected', 4))
        self.assertNotIn('SECRET', json.dumps(summary))
        self.assertNotIn('/home/pulse', json.dumps(summary))
        error = refresh.PeerTransportError(2, summary)
        self.assertEqual(refresh.peer_reason(error), 'peer_production_ledger_unavailable')
        self.assertEqual(refresh.peer_reason(refresh.PeerTransportError(1, {'records': [], 'exception': 'Rejected'})),
                         'peer_production_ledger_unavailable')
        self.assertEqual(refresh.peer_reason(refresh.PeerTransportError(255)), 'peer_ssh_failed')
        self.assertEqual(refresh.peer_reason(refresh.PeerTransportError(refresh.COLLECT_SHARED_BUSY_EXIT)), 'peer_busy')
        self.assertEqual(refresh.peer_reason(subprocess.TimeoutExpired('ssh', 1)), 'peer_timeout')

    def test_ssh_failure_logs_the_bounded_remote_summary(self):
        import io
        captured = io.StringIO()
        # Only the tail is read; the peer's failure record is its last line.
        noisy = b'\n'.join([b'private line %d' % i for i in range(5000)]
                           + [b'{"schema": "stats_refresh_progress/v1", "stage": "command_failed", "reason": "timeout"}'])
        with patch.object(refresh.subprocess, 'run', return_value=SimpleNamespace(returncode=2, stderr=noisy)), \
             patch.object(refresh.sys, 'stderr', captured):
            with self.assertRaises(refresh.PeerTransportError) as raised:
                refresh.ssh({'host': 'tcloud'}, ['/python', '/script', 'collect-shared'])
        logged = json.loads(captured.getvalue().splitlines()[0])
        self.assertEqual(logged['remote']['records'], [{'stage': 'command_failed', 'reason': 'timeout'}])
        self.assertNotIn('private line', captured.getvalue())
        self.assertLess(len(captured.getvalue()), 1024)
        self.assertEqual(refresh.peer_reason(raised.exception), 'peer_timeout')
