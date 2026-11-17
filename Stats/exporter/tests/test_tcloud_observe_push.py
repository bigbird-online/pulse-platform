"""A pushing peer's failed or previous-round collection is never read; TCloud observes and pushes."""
import json
import os
from pathlib import Path
import unittest
from unittest.mock import patch

from test_cost_refresh_push import Fixture, PublicationPullTests, PushedPeerTests, receipt, refresh

OBSERVED = refresh.canonical({'schema': refresh.OBSERVE_RECEIPT_SCHEMA, 'bundles': 2, 'pushed': 2})
FAILED = refresh.canonical({'schema': refresh.COLLECTION_FAILED_SCHEMA, 'reason': 'out_of_memory'})
TCLOUD_SOURCES = ['tcloud-relay', 'native-pi-4f7771e5f2fea4ad']
OWNERMAC_SOURCES = ['relay-host', 'native-cc-main']


class PushResultTests(PushedPeerTests):
    def test_a_push_counts_for_one_completed_publication_only(self):
        self.write_config(peers=[self.PEER])
        refresh.receive_shared_collection(self.config, 'ownermac', receipt())
        self.assertEqual(self.run_refresh()[1], [{'id': 'ownermac', 'status': 'read'}])
        # Nothing pushed since: the previous round's data is not read again.
        self.assertEqual(self.run_refresh()[1][0], {'id': 'ownermac', 'status': 'unreachable_or_invalid',
                                                    'reason': 'peer_push_stale'})
        self.assertFalse(self.marker_at_publication)
        refresh.receive_shared_collection(self.config, 'ownermac', receipt(watermark=942))
        self.assertEqual(self.run_refresh()[1][0]['status'], 'read')

    def test_a_failed_publication_does_not_spend_the_push(self):
        self.write_config(peers=[self.PEER])
        refresh.receive_shared_collection(self.config, 'ownermac', receipt())
        with patch.object(refresh, 'publish', return_value=1), \
             patch.object(refresh, 'fetch_openrouter_models', side_effect=OSError), \
             self.assertRaises(RuntimeError):
            refresh.refresh(self.config)
        self.assertEqual(self.run_refresh()[1][0]['status'], 'read')

    def test_a_pushed_failure_replaces_the_previous_success(self):
        self.write_config(peers=[self.PEER])
        refresh.receive_shared_collection(self.config, 'ownermac', receipt())
        refresh.receive_shared_collection(self.config, 'ownermac', FAILED)
        self.assertEqual(self.run_refresh()[1][0], {'id': 'ownermac', 'status': 'unreachable_or_invalid',
                                                    'reason': 'peer_collection_failed'})
        refresh.receive_shared_collection(self.config, 'ownermac', receipt(watermark=942))
        self.assertEqual(self.run_refresh()[1][0]['status'], 'read')

    def test_ownermac_observing_is_read_from_its_first_push_and_judged_from_its_spool(self):
        self.PEER = {'id': 'ownermac', 'transport': 'shared_ledger_push', 'observe_sources': OWNERMAC_SOURCES}
        self.write_config(peers=[self.PEER])
        refresh.receive_shared_collection(self.config, 'ownermac', OBSERVED)
        args, peers = self.run_refresh()
        self.assertEqual(peers, [{'id': 'ownermac', 'status': 'read'}])
        selected = json.loads(Path(args[args.index('--source-registry') + 1]).read_bytes())
        self.assertEqual(selected['sources'][-1], {
            'id': 'native-push-ownermac', 'kind': 'native_push', 'peer': 'ownermac',
            'path': str(refresh.observation_spool('ownermac')), 'source_ids': OWNERMAC_SOURCES})
        # The same rules as any pusher: no newer result, or a failed one, is not read.
        self.assertEqual(self.run_refresh()[1][0]['reason'], 'peer_push_stale')
        refresh.receive_shared_collection(self.config, 'ownermac', FAILED)
        self.assertEqual(self.run_refresh()[1][0]['reason'], 'peer_collection_failed')
        refresh.receive_shared_collection(self.config, 'ownermac', OBSERVED)
        self.assertEqual(self.run_refresh()[1][0]['status'], 'read')

    def test_a_result_counts_only_under_the_mode_the_publisher_registered(self):
        observing = {'id': 'ownermac', 'transport': 'shared_ledger_push', 'observe_sources': OWNERMAC_SOURCES}
        for peer, pushed in ((self.PEER, OBSERVED), (observing, receipt())):
            with self.subTest(observing='observe_sources' in peer):
                self.PEER = peer
                self.write_config(peers=[peer])
                refresh.receive_shared_collection(self.config, 'ownermac', pushed)
                self.assertEqual(self.run_refresh()[1][0], {'id': 'ownermac', 'status': 'unreachable_or_invalid',
                                                            'reason': 'peer_push_mode_mismatch'})

    def test_push_results_are_closed(self):
        self.write_config(peers=[self.PEER])
        for bad in ({'schema': refresh.OBSERVE_RECEIPT_SCHEMA, 'bundles': 2, 'pushed': 1},
                    {'schema': refresh.OBSERVE_RECEIPT_SCHEMA, 'bundles': 1, 'pushed': 1, 'extra': 0},
                    {'schema': refresh.COLLECTION_FAILED_SCHEMA, 'reason': 'Traceback /home/x'},
                    {'schema': 'other/v1'}):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                refresh.receive_shared_collection(self.config, 'ownermac', refresh.canonical(bad))
        self.assertFalse(refresh.push_receipt_path('ownermac').exists())

    def test_an_observing_pusher_is_judged_from_its_spool(self):
        peer = {'id': 'tcloud', 'transport': 'shared_ledger_push', 'host': 'tcloud-node2', 'python': '/p',
                'script': '/s', 'observe_sources': TCLOUD_SOURCES}
        self.PEER = peer
        self.write_config(peers=[peer])
        refresh.receive_shared_collection(self.config, 'tcloud', OBSERVED)
        args, peers = self.run_refresh(paused_peers={})  # Resumed: TCloud is paused by default.
        self.assertEqual(peers, [{'id': 'tcloud', 'status': 'read'}])
        selected = json.loads(Path(args[args.index('--source-registry') + 1]).read_bytes())
        self.assertEqual(selected['sources'][-1], {
            'id': 'native-push-tcloud', 'kind': 'native_push', 'peer': 'tcloud',
            'path': str(refresh.observation_spool('tcloud')), 'source_ids': TCLOUD_SOURCES})
        for bad in ([], ['a', 'a'], ['../x'], 'tcloud-relay'):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                refresh.observing_sources([{**peer, 'observe_sources': bad}])
        with self.assertRaises(ValueError):
            refresh.observing_sources([{'id': 'oracle', 'transport': 'shared_ledger', 'observe_sources': ['x']}])

    def test_a_paused_observer_adds_no_source(self):
        peer = {'id': 'tcloud', 'transport': 'shared_ledger_push', 'observe_sources': TCLOUD_SOURCES}
        self.PEER = peer
        args, peers = self.run_refresh(paused_peers={'tcloud': 'paused_by_decision'})
        self.assertEqual(peers[0]['status'], 'paused')
        selected = json.loads(Path(args[args.index('--source-registry') + 1]).read_bytes())
        self.assertNotIn('native_push', [s['kind'] for s in selected['sources']])


class FullModeFailureTests(Fixture):
    def test_a_failed_full_collection_pushes_its_failure(self):
        self.write_config(peer_id='ownermac', **PublicationPullTests.CONFIG)
        pushed = []

        def transport(peer, command, data=None):
            pushed.append((command[2], data and json.loads(data)))
            if command[2] == 'fetch-publication':
                return refresh.canonical({'schema': refresh.PUBLICATION_BUNDLE_SCHEMA, 'dashboard': None,
                                          'last_week': None, 'refresh_status': None})
            return b''
        with patch.object(refresh, 'ssh', side_effect=transport), \
             patch.object(refresh, 'shared_collection_receipt', side_effect=MemoryError()), \
             patch.multiple(refresh, refresh_quota=lambda config: 'source_not_configured',
                            refresh_cc_quota=lambda config: 'source_not_configured',
                            refresh_grok_quota=lambda config: 'source_not_configured',
                            refresh_wi_facts=lambda config: 'published'):
            os.environ.pop(refresh.SCHEDULE_VARIABLE, None)
            with self.assertRaisesRegex(RuntimeError, 'local collection failed'):
                refresh.collect_local(self.config)
        self.assertEqual(pushed[0], ('receive-shared-collection',
                                     {'schema': refresh.COLLECTION_FAILED_SCHEMA, 'reason': 'out_of_memory'}))


class ObservePushTests(Fixture):
    CONFIG = {'peer_id': 'tcloud', 'collect_local_mode': 'observe', **PublicationPullTests.CONFIG}

    def run_push(self, *, observe_error=None, **config):
        self.write_config(**{**self.CONFIG, **config})
        commands, observed = [], []

        def observe(registry, *, peer, index_path, outbox, read_at, backfill=False, force_recompute=False):
            observed.append((peer, backfill))
            if observe_error is not None:
                raise observe_error
            outbox.mkdir(mode=0o700, parents=True, exist_ok=True)
            bundle = outbox / '00000000000000000001.json.gz'
            bundle.write_bytes(b'bundle')
            return [bundle]

        def transport(peer, command, data=None):
            commands.append((command[2:], data if command[2] != 'receive-shared-collection' else json.loads(data)))
            return b''
        from pulse_compute_cost import native_observe
        no = lambda *a, **k: self.fail('observe-push reads no quota, GitHub, provider or stream')
        with patch.object(refresh, 'ssh', side_effect=transport), \
             patch.object(native_observe, 'observe', side_effect=observe), \
             patch.object(native_observe, 'acknowledge', side_effect=lambda index, bundle: bundle.unlink()), \
             patch.multiple(refresh, shared_collection_receipt=no, refresh_quota=no, refresh_cc_quota=no,
                            refresh_grok_quota=no, refresh_wi_facts=no, refresh_pr_audit=no,
                            refresh_pr_bodies=no, push_openrouter_snapshot=no, pull_publication=no):
            os.environ.pop(refresh.SCHEDULE_VARIABLE, None)
            try:
                refresh.observe_push(self.config)
            finally:
                path = self.state / refresh.COLLECT_LOCAL_STATUS
                status = json.loads(path.read_bytes()) if path.exists() else None
        return commands, status, observed

    def test_observe_push_sends_bundles_then_the_result_and_nothing_else(self):
        commands, status, observed = self.run_push()
        self.assertEqual(observed, [('tcloud', False)])
        self.assertEqual(commands, [(['receive-native-observations', '--peer', 'tcloud'], b'bundle'),
                                    (['receive-shared-collection', '--peer', 'tcloud'],
                                     {'schema': refresh.OBSERVE_RECEIPT_SCHEMA, 'bundles': 1, 'pushed': 1})])
        self.assertEqual((status['collection']['state'], status['collection']['bundles']), ('completed', 1))

    def test_out_of_memory_is_pushed_as_a_failure(self):
        with self.assertRaisesRegex(RuntimeError, 'observe push failed'):
            self.run_push(observe_error=MemoryError())
        status = json.loads((self.state / refresh.COLLECT_LOCAL_STATUS).read_bytes())
        self.assertEqual((status['collection']['state'], status['collection']['reason'], status['failure_push']),
                         ('failed', 'out_of_memory', 'pushed'))

    def test_only_an_observing_peer_runs_it(self):
        with self.assertRaisesRegex(ValueError, 'collect_local_mode observe'):
            self.run_push(collect_local_mode='full')

    def test_a_running_collection_is_not_doubled(self):
        held = refresh._collect_shared_lock()
        self.addCleanup(os.close, held)
        commands, status, observed = self.run_push()
        self.assertEqual((commands, status, observed), ([], None, []))


if __name__ == '__main__':
    unittest.main()
