"""The publisher spools an observing peer's bundles; only a registered pusher may send them."""
import gzip
import json
import unittest

from test_cost_refresh_push import Fixture, refresh
from pulse_compute_cost.native_observations import SCHEMA, encode, spool_path

AT = '2026-11-15T00:00:00Z'


def bundle(peer='ownermac', sequence=1):
    return encode({'schema': SCHEMA, 'peer': peer, 'sequence': sequence, 'created_at': AT,
                   'sources': [], 'inputs': []})


class ObservationPushTests(Fixture):
    PEER = {'id': 'ownermac', 'transport': 'shared_ledger_push'}

    def test_registered_pusher_spools_each_bundle_once(self):
        self.write_config(peers=[self.PEER])
        self.assertEqual(refresh.receive_native_observations(self.config, 'ownermac', bundle()), 1)
        self.assertEqual(refresh.receive_native_observations(self.config, 'ownermac', bundle()), 1)
        spool = refresh.observation_spool('ownermac')
        self.assertTrue(spool_path(spool, 1).exists())
        self.assertEqual(spool.stat().st_mode & 0o777, 0o700)
        different = encode({**json.loads(gzip.decompress(bundle())), 'created_at': '2026-11-15T00:00:01Z'})
        with self.assertRaises(ValueError):
            refresh.receive_native_observations(self.config, 'ownermac', different)

    def test_unregistered_or_mismatched_pushers_are_refused(self):
        self.write_config(peers=[self.PEER, {'id': 'tcloud', 'host': 'tcloud-node2', 'python': '/p', 'script': '/s',
                                             'transport': 'shared_ledger'}])
        for peer, data in (('tcloud', bundle('tcloud')), ('other', bundle('other')),
                           ('ownermac', bundle('tcloud')), ('ownermac', b'not gzip')):
            with self.subTest(peer=peer), self.assertRaises(ValueError):
                refresh.receive_native_observations(self.config, peer, data)
        self.assertFalse(refresh.observation_spool('ownermac').exists()
                         and any(refresh.observation_spool('ownermac').iterdir()))


if __name__ == '__main__':
    unittest.main()
