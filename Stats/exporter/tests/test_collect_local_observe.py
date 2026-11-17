"""collect-local in observe mode: bundles to the publisher, never the observation stream."""
import json
import os
from pathlib import Path
import unittest
from unittest.mock import patch

from test_cost_refresh_push import Fixture, PublicationPullTests, refresh


class ObserveModeTests(Fixture):
    def run_local(self, *, refuse=False, backfill=False, mode='observe', switched_to=None):
        self.write_config(peer_id='ownermac', collect_local_mode=mode, **PublicationPullTests.CONFIG)
        commands, observed, streamed = self.commands, self.observed, self.streamed = [], [], []
        bundle_dir = self.root / 'bundles'
        bundle_dir.mkdir(exist_ok=True)

        def observe(registry, *, peer, index_path, outbox, read_at, backfill=False, force_recompute=False):
            observed.append(backfill)
            outbox.mkdir(mode=0o700, parents=True, exist_ok=True)
            bundle = outbox / f'{len(observed):020d}.json.gz'
            bundle.write_bytes(b'bundle'); bundle.with_suffix('.delta').write_bytes(b'{}')
            return [bundle]

        def transport(peer, command, data=None):
            commands.append(command[2:])
            if command[2] == 'receive-native-observations' and refuse:
                raise refresh.PeerTransportError(2)
            if command[2] == 'fetch-publication':
                return refresh.canonical({'schema': refresh.PUBLICATION_BUNDLE_SCHEMA, 'dashboard': None,
                                          'last_week': None, 'refresh_status': None})
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
                                    ['receive-shared-collection', '--peer', 'ownermac'], ['fetch-publication']])
        self.assertEqual((status['collection']['state'], status['collection']['mode'],
                          status['collection']['bundles']), ('completed', 'observe', 1))
        self.assertEqual((observed, acknowledged), ([False], ['00000000000000000001.json.gz']))

    def test_a_refused_bundle_is_dropped_so_its_sessions_are_read_again(self):
        with self.assertRaisesRegex(RuntimeError, 'local collection failed'):
            self.run_local(refuse=True)
        outbox = self.state / refresh.OBSERVE_DIRECTORY / 'outbox'
        self.assertEqual(list(outbox.iterdir()), [])
        status = json.loads((self.state / refresh.COLLECT_LOCAL_STATUS).read_bytes())
        self.assertEqual((status['collection']['state'], status['failure_push']), ('failed', 'pushed'))

    def test_backfill_is_an_observe_mode_run(self):
        _, _, observed, _ = self.run_local(backfill=True)
        self.assertEqual(observed, [True])
        with self.assertRaisesRegex(ValueError, 'backfill needs observe'):
            self.run_local(backfill=True, mode='full')

    def test_the_cycle_takes_the_mode_observe_mode_left_before_it_got_the_lock(self):
        for started, switched in (('full', 'observe'), ('observe', 'full')):
            with self.subTest(started=started, switched=switched):
                commands, status, observed, _ = self.run_local(mode=started, switched_to=switched)
                self.assertEqual(status['collection']['mode'], switched)
                # Full appends the stream itself and pushes its position; observe only bundles.
                self.assertEqual((bool(observed), bool(self.streamed)), (switched == 'observe', switched == 'full'))
                self.assertEqual(commands[-2:], [['receive-shared-collection', '--peer', 'ownermac'],
                                                 ['fetch-publication']])

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
                    self.assertEqual(self.commands, [['receive-shared-collection', '--peer', 'ownermac'],
                                                     ['fetch-publication']])
        (self.state / refresh.OBSERVE_MODE_RECEIPT).write_text(json.dumps({'status': 'completed'}))
        self.assertEqual(self.run_local()[1]['collection']['state'], 'completed')


if __name__ == '__main__':
    unittest.main()
