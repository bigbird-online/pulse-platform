"""collect-local in observe mode: bundles to the publisher, never the observation stream."""
import json
import os
from pathlib import Path
import unittest
from unittest.mock import patch

from datetime import datetime, timedelta, timezone

from test_cost_refresh_push import Fixture, PublicationPullTests, dashboard, refresh


class ObserveModeTests(Fixture):
    def run_local(self, *, refuse=False, backfill=False, mode='observe', switched_to=None, killed=False, remaining=0,
                  during=None):
        self.write_config(peer_id='ownermac', collect_local_mode=mode, **PublicationPullTests.CONFIG)
        commands, observed, streamed = self.commands, self.observed, self.streamed = [], [], []
        self.deadlines, self.pushed = [], []
        bundle_dir = self.root / 'bundles'
        bundle_dir.mkdir(exist_ok=True)

        def observe(registry, *, peer, index_path, outbox, read_at, backfill=False, force_recompute=False,
                    deadline=None, deliver=None):
            observed.append(backfill)
            if during is not None:
                during()
            if killed:
                raise KeyboardInterrupt  # Stands for the deadline's SIGALRM ending the process.
            outbox.mkdir(mode=0o700, parents=True, exist_ok=True)
            bundle = outbox / f'{len(observed):020d}.json.gz'
            bundle.write_bytes(b'bundle'); bundle.with_suffix('.delta').write_bytes(b'{}')
            deliver(bundle)
            self.deadlines.append(deadline)
            result = native_observe.Observed([bundle])
            result.remaining = remaining
            return result

        def transport(peer, command, data=None):
            if refresh.PUBLISHER_ENTRY_PROBE in command:
                commands.append(['entry-probe'])
                return json.dumps(self.selection).encode()
            commands.append(command[2:])
            if command[2] == 'receive-shared-collection':
                self.pushed.append(json.loads(data))
            if command[2] == 'receive-native-observations' and refuse:
                raise refresh.PeerTransportError(2)
            if command[2] == 'fetch-publication':
                with patch.object(refresh, 'STATE', self.root / 'publisher'):
                    return refresh.publication_bundle()
            return b''
        def stream(force_recompute=False):
            streamed.append(force_recompute)
            return json.dumps({'watermark': 1, 'inserted': 0}).encode()
        real_flock = refresh.fcntl.flock

        def flock(lock, operation):
            # observe-mode finishes and releases the cycle lock just before this cycle takes it.
            nonlocal switched_to
            if switched_to is not None:
                self.write_config(peer_id='ownermac', collect_local_mode=switched_to, **PublicationPullTests.CONFIG)
                switched_to = None
            return real_flock(lock, operation)
        acknowledged = []
        from pulse_compute_cost import native_observe
        with patch.object(refresh, 'ssh', side_effect=transport), \
             patch.object(refresh.fcntl, 'flock', side_effect=flock), \
             patch.object(refresh, 'shared_collection_receipt', side_effect=stream), \
             patch.object(refresh, 'incremental_collection_receipt',
                          side_effect=lambda deadline, force_recompute=False: (
                              self.deadlines.append(deadline), stream(force_recompute))[1]), \
             patch.object(native_observe, 'observe', side_effect=observe), \
             patch.object(native_observe, 'acknowledge', side_effect=lambda index, bundle: (
                 acknowledged.append(bundle.name), bundle.unlink(), bundle.with_suffix('.delta').unlink())), \
             patch.multiple(refresh, refresh_quota=lambda config: 'source_not_configured',
                            refresh_cc_quota=lambda config: 'source_not_configured',
                            refresh_grok_quota=lambda config: 'source_not_configured',
                            refresh_wi_facts=lambda config: 'published'):
            os.environ.pop(refresh.SCHEDULE_VARIABLE, None)
            try:
                refresh.collect_local(self.config, observe_backfill=backfill)
            finally:
                status = json.loads((self.state / refresh.COLLECT_LOCAL_STATUS).read_bytes())
        return commands, status, observed, acknowledged

    def test_observe_cycle_pushes_bundles_and_never_touches_the_stream(self):
        commands, status, observed, acknowledged = self.run_local()
        self.assertEqual(self.streamed, [])
        self.assertEqual(commands, [['receive-native-observations', '--peer', 'ownermac'],
                                    ['receive-shared-collection', '--peer', 'ownermac']])
        self.assertEqual((status['collection']['state'], status['collection']['mode'],
                          status['collection']['bundles']), ('completed', 'observe', 1))
        self.assertEqual((observed, acknowledged), ([False], ['00000000000000000001.json.gz']))

    def test_only_a_publisher_acknowledgement_moves_its_cursor(self):
        with self.assertRaisesRegex(RuntimeError, 'local collection failed'):
            self.run_local(refuse=True)
        self.assertEqual(refresh._publisher_acknowledged(), 0)
        self.run_local()
        record = json.loads((self.state / refresh.OBSERVE_DIRECTORY / refresh.OBSERVE_PUSHED).read_bytes())
        self.assertEqual((record['sequence'], refresh._publisher_acknowledged()), (1, 1))

    def test_an_entry_on_a_release_the_publisher_left_fails_before_any_reading(self):
        script = '/home/pulse/.local/share/pulse/stats/releases/{}/Scripts/pulse-stats-cost-refresh.py'
        config = {'publisher': 'oracle-node-1', 'peers': [{**PublicationPullTests.PUBLISHER, 'script': script.format('a' * 40)}]}
        self.selection = {'selected': 'b' * 40, 'entry': True}
        with patch.object(PublicationPullTests, 'CONFIG', config):
            with self.assertRaisesRegex(RuntimeError, 'local collection failed'):
                self.run_local(backfill=True)
            self.assertEqual((self.observed, self.commands[0], self.pushed[-1]['reason']),
                             ([], ['entry-probe'], 'publisher_entry_stale'))
            status = json.loads((self.state / refresh.COLLECT_LOCAL_STATUS).read_bytes())
            self.assertEqual(status['collection']['reason'], 'publisher_entry_stale')
            # On the selected release the same cycle runs.
            self.selection = {'selected': 'a' * 40, 'entry': True}
            _, status, observed, _ = self.run_local(backfill=True)
            self.assertEqual((status['collection']['state'], observed[0] is not False), ('completed', True))

    def test_a_refused_bundle_is_dropped_so_its_sessions_are_read_again(self):
        with self.assertRaisesRegex(RuntimeError, 'local collection failed'):
            self.run_local(refuse=True)
        outbox = self.state / refresh.OBSERVE_DIRECTORY / 'outbox'
        self.assertEqual(list(outbox.iterdir()), [])
        status = json.loads((self.state / refresh.COLLECT_LOCAL_STATUS).read_bytes())
        self.assertEqual((status['collection']['state'], status['failure_push']), ('failed', 'pushed'))

    def test_a_publication_made_while_the_collection_runs_reaches_the_app_before_the_collection_ends(self):
        publisher = self.root / 'publisher'
        publisher.mkdir(mode=0o700)
        local = self.state / 'cost-dashboard-v1.json'
        now = datetime.now(timezone.utc) - timedelta(minutes=10)
        old, new = dashboard(now - timedelta(minutes=40)), dashboard(now)
        refresh.private_write(local, refresh.canonical(old) + b'\n')
        pulls = []

        def oracle_publishes_then_the_pull_job_ticks():
            # The cycle holds its lock and is mid-collection.
            with open(self.state / refresh.CYCLE_LOCK, 'rb') as probe, self.assertRaises(BlockingIOError):
                refresh.fcntl.flock(probe, refresh.fcntl.LOCK_EX | refresh.fcntl.LOCK_NB)
            refresh.private_write(publisher / 'cost-dashboard-v1.json', refresh.canonical(new) + b'\n')
            pulls.append(refresh.pull_publication_job(self.config))
        # The deadline's SIGALRM ends the collection; the publication had already changed.
        with self.assertRaises(KeyboardInterrupt):
            self.run_local(killed=True, during=oracle_publishes_then_the_pull_job_ticks)
        self.assertEqual(pulls[0]['publication']['dashboard'], 'installed')
        self.assertEqual(json.loads(local.read_bytes())['sha256'], new['sha256'])
        self.assertNotEqual(new['sha256'], old['sha256'])
        status = json.loads((self.state / refresh.PULL_STATUS).read_bytes())
        self.assertEqual((status['state'], status['publication']['dashboard']), ('pulled', 'installed'))
        # The one fetch is the pull job's; the collection itself pulled nothing.
        self.assertEqual(self.commands, [['fetch-publication']])
        self.assertEqual(json.loads((self.state / refresh.COLLECT_LOCAL_STATUS).read_bytes())['collection']['state'],
                         'running')

    def test_the_pull_job_is_a_no_op_on_a_self_publishing_node_and_while_another_pull_runs(self):
        self.write_config(peers=[])
        self.assertEqual(refresh.pull_publication_job(self.config)['state'], 'not_a_reader')
        self.write_config(peer_id='ownermac', **PublicationPullTests.CONFIG)
        with open(self.state / refresh.PULL_LOCK, 'a+b') as held:
            refresh.fcntl.flock(held, refresh.fcntl.LOCK_EX | refresh.fcntl.LOCK_NB)
            with patch.object(refresh, 'ssh', side_effect=AssertionError('pulled twice at once')):
                self.assertEqual(refresh.pull_publication_job(self.config)['state'], 'busy')
        with patch.object(refresh, 'ssh', side_effect=refresh.PeerTransportError(255)), \
                self.assertRaisesRegex(RuntimeError, 'publication pull failed'):
            refresh.pull_publication_job(self.config)
        status = json.loads((self.state / refresh.PULL_STATUS).read_bytes())
        self.assertEqual(status['state'], 'failed')
        self.assertIsNotNone(status['pulled_at'])

    def test_a_pull_writes_nothing_once_the_publisher_changed_during_its_fetch(self):
        self.write_config(peers=[])
        status = {'schema': refresh.REFRESH_STATUS_SCHEMA, 'state': 'oracle_cycle'}
        bundle = json.dumps({'schema': refresh.PUBLICATION_BUNDLE_SCHEMA, 'dashboard': dashboard(
            datetime.now(timezone.utc)), 'last_week': None, 'refresh_status': status}).encode()
        written = []

        def fetch(peer, command, data=None):
            # A writer that skipped publication-pull.lock makes this node its own publisher mid-pull.
            self.write_config(peers=[])
            return bundle

        self.assertEqual(refresh.pull_publication_job(self.config)['state'], 'not_a_reader')
        self.write_config(peer_id='ownermac', **PublicationPullTests.CONFIG)
        real_write = refresh.private_write
        with patch.object(refresh, 'ssh', side_effect=fetch), \
                patch.object(refresh, 'private_write', side_effect=lambda path, data: (
                    written.append(path.name), real_write(path, data))):
            self.assertEqual(refresh.pull_publication_job(self.config)['state'], 'superseded')
        # Only the test's own configuration writes; no publication, cycle status or pull status.
        self.assertEqual(set(written), {self.config.name})
        for name in ('cost-dashboard-v1.json', refresh.REFRESH_STATUS, refresh.PULL_STATUS):
            self.assertFalse((self.state / name).exists(), name)

    def test_a_run_out_of_time_says_so_and_the_backfill_continues_next_run(self):
        started = refresh.time.monotonic()
        _, status, observed, _ = self.run_local(backfill=True, remaining=7)
        # Reading stops well before the 45-minute SIGALRM, so the result is still pushed.
        self.assertLess(self.deadlines[0] - started, refresh.COLLECT_LOCAL_DEADLINE_SECONDS - 10 * 60)
        self.assertEqual(self.pushed[-1], {'schema': refresh.OBSERVE_RECEIPT_SCHEMA, 'bundles': 1, 'pushed': 1,
                                           'remaining': 7})
        self.assertEqual((status['collection']['state'], status['collection']['remaining']), ('completed', 7))
        marker = self.state / refresh.OBSERVE_DIRECTORY / refresh.OBSERVE_BACKFILL
        self.assertTrue(marker.exists())
        # The scheduled run that follows continues the same backfill until it leaves nothing.
        _, status, again, _ = self.run_local()
        self.assertEqual((again, status['collection']['remaining']), (observed, 0))
        self.assertNotIn('remaining', self.pushed[-1])
        self.assertFalse(marker.exists())

    def test_backfill_is_an_observe_mode_run_that_continues_until_it_completes(self):
        _, _, observed, _ = self.run_local(backfill=True)
        self.assertEqual(len(observed), 1)
        self.assertIsInstance(observed[0], str)  # The backfill's token.
        # It read every session: the next run is an ordinary one.
        self.assertFalse((self.state / refresh.OBSERVE_DIRECTORY / refresh.OBSERVE_BACKFILL).exists())
        self.assertEqual(self.run_local()[2], [False])
        with self.assertRaisesRegex(ValueError, 'backfill needs observe'):
            self.run_local(backfill=True, mode='full')

    def test_the_cycle_takes_the_mode_observe_mode_left_before_it_got_the_lock(self):
        for started, switched in (('full', 'observe'), ('observe', 'full')):
            with self.subTest(started=started, switched=switched):
                commands, status, observed, _ = self.run_local(mode=started, switched_to=switched)
                self.assertEqual(status['collection']['mode'], switched)
                # Full appends the stream itself and pushes its position; observe only bundles.
                self.assertEqual((bool(observed), bool(self.streamed)), (switched == 'observe', switched == 'full'))
                self.assertEqual(commands[-1:], [['receive-shared-collection', '--peer', 'ownermac']])

    def test_an_unresolved_observe_mode_switch_collects_nothing(self):
        for status_word in ('switching', 'failed', None):
            with self.subTest(status=status_word):
                receipt = self.state / refresh.OBSERVE_MODE_RECEIPT
                receipt.write_text(json.dumps({'status': status_word}) if status_word else '{')
                for mode in ('observe', 'full'):
                    with self.assertRaisesRegex(RuntimeError, 'local collection failed'):
                        self.run_local(mode=mode)
                    status = json.loads((self.state / refresh.COLLECT_LOCAL_STATUS).read_bytes())
                    # Neither appended nor observed: only the failure goes to the publisher.
                    self.assertEqual((status['collection']['reason'], status['failure_push'], self.streamed,
                                      self.observed), ('observe_mode_unresolved', 'pushed', [], []))
                    self.assertEqual(self.commands, [['receive-shared-collection', '--peer', 'ownermac']])
        (self.state / refresh.OBSERVE_MODE_RECEIPT).write_text(json.dumps({'status': 'completed'}))
        self.assertEqual(self.run_local()[1]['collection']['state'], 'completed')


if __name__ == '__main__':
    unittest.main()


class IncrementalModeTests(Fixture):
    """collect-local in incremental mode (WI754): judged and appended here, the position pushed."""
    run_local = ObserveModeTests.run_local

    def test_incremental_cycle_appends_itself_and_pushes_its_position(self):
        commands, status, observed, _ = self.run_local(mode='incremental')
        self.assertEqual((self.streamed, observed), ([False], []))  # No bundle leaves this node.
        self.assertEqual(commands, [['receive-shared-collection', '--peer', 'ownermac']])
        self.assertEqual(self.pushed, [{'watermark': 1, 'inserted': 0}])
        self.assertEqual((status['collection']['state'], status['collection']['mode']), ('completed', 'incremental'))
        self.assertEqual(len(self.deadlines), 1)  # Observing stops starting sessions at the read budget.

    def test_the_watchdog_ends_a_run_past_its_memory(self):
        import threading
        stopped = threading.Event()
        done = refresh.watch_memory(100, stopped.set, every=0.01, measure=lambda: 101)
        self.assertTrue(stopped.wait(2))
        done.set()
        quiet = threading.Event()
        done = refresh.watch_memory(100, quiet.set, every=0.01, measure=lambda: 100)
        self.assertFalse(quiet.wait(0.1))
        done.set()
        self.assertGreater(refresh.resident_bytes(), 0)

    def test_an_observing_trial_keeps_a_copy_of_each_pushed_bundle(self):
        from pulse_compute_cost import native_observations
        copies = []
        real = self.write_config
        with patch.object(self, 'write_config', side_effect=lambda **fields: real(incremental_trial=True, **fields)), \
                patch.object(native_observations, 'store',
                             side_effect=lambda spool, peer, data: copies.append((spool, peer, data))):
            commands, status, _, _ = self.run_local()
        trial = self.state / refresh.OBSERVE_DIRECTORY / refresh.TRIAL_DIRECTORY
        self.assertEqual(copies, [(trial / 'spool', 'ownermac', b'bundle')])
        self.assertEqual(oct(trial.stat().st_mode)[-3:], '700')
        self.assertEqual(status['collection']['state'], 'completed')
        self.assertEqual(commands[-1], ['receive-shared-collection', '--peer', 'ownermac'])

    def test_a_trial_needs_observe_mode_and_the_switch(self):
        self.write_config(peer_id='ownermac', collect_local_mode='incremental', **PublicationPullTests.CONFIG)
        with patch.object(refresh, 'release_config', side_effect=lambda path: json.loads(Path(path).read_bytes())):
            with self.assertRaisesRegex(ValueError, 'incremental_trial'):
                refresh.incremental_trial(self.config)
