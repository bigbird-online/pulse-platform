"""Oracle publisher installation and the ownermac switch; no SSH, scheduler or remote writes."""
import importlib.util
import fcntl
import json
import os
from pathlib import Path
import plistlib
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from Stats.exporter.tests.test_cost_refresh import refresh as cost_refresh

spec = importlib.util.spec_from_file_location('stats_deploy_oracle', Path(__file__).parents[2]/'deploy.py')
d = importlib.util.module_from_spec(spec)
spec.loader.exec_module(d)
A, B = 'a'*40, 'b'*40
SCRIPT = '/home/pulse/.local/share/pulse/stats/releases/{}/Scripts/pulse-stats-cost-refresh.py'
TCLOUD = {'id': 'tcloud', 'host': 'tcloud-node2', 'prefix': ['runuser', '-u', 'pulse', '--'], 'python': d.PEER_PYTHON,
          'script': SCRIPT.format(A), 'transport': 'shared_ledger'}
ORACLE = {'id': 'oracle-node-1', 'host': 'pulse@oracle-node-1', 'python': d.PEER_PYTHON,
          'script': SCRIPT.format(A), 'transport': 'shared_ledger'}
OWNERMAC_SOURCES = ['relay-host', 'native-cc-main']
TENANTS = {'schema': 'pulse_stats_tenant_registry/v2', 'bindings': [], 'relay_users': [],
           'definition_observed_at': '2026-10-06T07:40:34+00:00'}


class Home(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.home = Path(tmp.name).resolve()
        self.state = self.home/'.local/state/pulse/stats'
        self.state.mkdir(parents=True, mode=0o700)

    def reader(self, revision):
        runtime = self.home/'.local/share/pulse/stats/releases'/revision
        (runtime/'Scripts').mkdir(parents=True)
        (runtime/'Scripts/pulse-stats-cost-refresh.py').write_text('')
        (runtime/'.stats-source-revision').write_text(revision)
        (runtime/'Stats/systemd').mkdir(parents=True)
        for name in d.ORACLE_PUBLISHER_UNITS:
            (runtime/'Stats/systemd'/name).write_bytes((Path(__file__).parents[2]/'systemd'/name).read_bytes())
        current = self.home/'.local/share/pulse/stats/current'
        current.unlink(missing_ok=True)
        current.symlink_to(runtime)
        return runtime


class OraclePublisherConfigTests(Home):
    def test_oracle_config_reaches_peers_directly_and_waits_for_the_pushing_peer(self):
        payload = {'peers': [TCLOUD, ORACLE], 'required_receipt_features': ['tenant_context.v1', 'actor_context.v1'],
                   'kernel_merge_enforced_since': '2026-11-01'}
        config = d.oracle_publisher_config({**payload, 'paused_peers': {}}, B, self.state)
        self.assertEqual(config['peers'], [{**{k: v for k, v in TCLOUD.items() if k != 'prefix'},
                                            'script': SCRIPT.format(B)},
                                           {'id': 'ownermac', 'transport': 'shared_ledger_push'}])
        self.assertEqual((config['source_revision'], config['publication_validity_minutes'],
                          config['hold_collect_shared_lock'], config['kernel_merge_enforced_since']),
                         (B, 75, True, '2026-11-01'))
        overlay = config['registry_overlay']
        self.assertEqual(overlay['required_receipt_features'], ['actor_context.v1', 'tenant_context.v1'])
        self.assertEqual(overlay['tenant_registry_path'], str(self.state/'tenant-registry.json'))
        self.assertEqual(overlay['sources'][0]['kind'], 'harness_core')
        self.assertNotIn('codex_quota', config)
        self.assertNotIn('openrouter_env', config)

    def test_pushing_peer_carries_only_its_identity(self):
        for peer in ({'id': 'ownermac', 'transport': 'shared_ledger_push', 'host': 'ownermac'},
                     {'id': 'other', 'transport': 'shared_ledger_push'}):
            with self.subTest(peer=peer), self.assertRaises(ValueError):
                d.peer_scripts({'peers': [peer]}, B)

    def test_tcloud_may_push_observations_and_keeps_its_reader(self):
        observing = {**TCLOUD, 'transport': 'shared_ledger_push', 'observe_sources': ['tcloud-relay']}
        config = d.peer_scripts({'peers': [observing], 'paused_peers': {}}, B)
        self.assertEqual(config['peers'], [{**observing, 'script': SCRIPT.format(B)}])
        published = d.oracle_publisher_config({'peers': [observing, ORACLE], 'paused_peers': {},
                                               'required_receipt_features': []}, B, self.state)
        self.assertEqual(published['peers'][0], {**{k: v for k, v in observing.items() if k != 'prefix'},
                                                 'script': SCRIPT.format(B)})
        for peer in ({**observing, 'observe_sources': []}, {k: v for k, v in observing.items() if k != 'observe_sources'},
                     {**observing, 'id': 'other'}, {**observing, 'host': 'elsewhere'}):
            with self.subTest(peer=peer), self.assertRaises(ValueError):
                d.peer_scripts({'peers': [peer], 'paused_peers': {}}, B)

    def test_ownermac_observing_registration_reaches_oracle_and_survives_reader_switches(self):
        payload = {'peers': [TCLOUD, ORACLE], 'paused_peers': {}, 'required_receipt_features': [],
                   'observe_sources': OWNERMAC_SOURCES}
        config = d.oracle_publisher_config(payload, A, self.state)
        self.assertEqual(config['peers'][-1], {'id': 'ownermac', 'transport': 'shared_ledger_push',
                                               'observe_sources': OWNERMAC_SOURCES})
        self.assertEqual(d.observing_registration(config['peers'], config['paused_peers']),
                         {'ownermac': OWNERMAC_SOURCES})
        # A reader switch on Oracle retargets the reader and keeps the registration.
        switched = json.loads(d.publisher_config_for(json.dumps(config).encode(), B))
        self.assertEqual((switched['peers'][-1], switched['source_revision']), (config['peers'][-1], B))
        self.assertEqual(cost_refresh.observing_sources(switched['peers'])[-1]['id'], 'native-push-ownermac')
        for bad in ([], ['a', 'a'], ['../x'], 'relay-host'):
            with self.subTest(bad=bad), self.assertRaisesRegex(ValueError, 'observe_sources'):
                d.oracle_publisher_config({**payload, 'observe_sources': bad}, A, self.state)

    def test_units_name_this_account_and_the_selected_reader(self):
        units = d.oracle_publisher_units(self.reader(B), d.ORACLE_HOME)
        service = units['pulse-stats-refresh.service'].decode()
        self.assertIn('ExecStart='+d.PEER_PYTHON+' /home/pulse/.local/share/pulse/stats/current/Scripts/'
                      'pulse-stats-cost-refresh.py refresh --config /home/pulse/.local/state/pulse/stats/refresh.json',
                      service)
        self.assertIn('MemoryMax=9G', service)
        self.assertNotIn('@', service.replace('@HOME', '').replace('@PYTHON', ''))
        self.assertIn('OnUnitInactiveSec=120s', units['pulse-stats-refresh.timer'].decode())

    def test_receiver_writes_private_inputs_and_units_but_starts_nothing(self):
        self.reader(B)
        (self.home/'.config/systemd/user').mkdir(parents=True)
        commands = []
        payload = json.dumps({'peers': [TCLOUD, ORACLE], 'required_receipt_features': [],
                              'tenant_registry': TENANTS}).encode()
        with patch.object(d, 'ORACLE_HOME', self.home), patch.object(d.os, 'uname',
                return_value=SimpleNamespace(nodename='oracle-node-1')), \
                patch.object(d, 'run', side_effect=lambda argv, **k: commands.append(argv) or b''):
            result = d.receive_oracle_publisher(self.home, B, payload)
            self.assertEqual(set(result['files'].values()), {'written'})
            self.assertEqual((result['paused_peers'], result['observing_peers']), (d.DEFAULT_PAUSED_PEERS, []))
            again = d.receive_oracle_publisher(self.home, B, payload)
        self.assertEqual(set(again['files'].values()), {'unchanged'})
        self.assertEqual(commands, [['systemctl', '--user', 'daemon-reload']]*2)
        for name in ('tenant-registry.json', 'refresh.json'):
            self.assertEqual(os.stat(self.state/name).st_mode & 0o077, 0)
        self.assertEqual(json.loads((self.state/'tenant-registry.json').read_bytes()), TENANTS)
        self.assertTrue((self.home/'.config/systemd/user/pulse-stats-refresh.timer').is_file())
        with patch.object(d, 'ORACLE_HOME', self.home), patch.object(d.os, 'uname',
                return_value=SimpleNamespace(nodename='oracle-node-1')), patch.object(d, 'run', return_value=b''):
            with self.assertRaises(ValueError):
                d.receive_oracle_publisher(self.home, A, payload)
        with patch.object(d.os, 'uname', return_value=SimpleNamespace(nodename='ownermac')), \
                self.assertRaises(ValueError):
            d.receive_oracle_publisher(self.home, B, payload)

    def test_reader_switch_retargets_only_an_oracle_publisher(self):
        config = d.oracle_publisher_config({'peers': [TCLOUD], 'required_receipt_features': [],
                                            'paused_peers': {}}, A, self.state)
        d.atomic(self.state/'refresh.json', json.dumps(config).encode())
        self.assertIsNone(d.retarget_publisher_config(self.home, B))
        with patch.object(d, 'ORACLE_HOME', self.home):
            self.assertEqual(d.retarget_publisher_config(self.home, B), B)
        updated = json.loads((self.state/'refresh.json').read_bytes())
        self.assertEqual((updated['source_revision'], updated['peers'][0]['script']), (B, SCRIPT.format(B)))


class PausedPeerTests(Home):
    """2026-11-15: by default only Oracle computes Stats; TCloud stays registered but paused."""
    PAUSED = {'tcloud': 'paused_by_decision'}

    def test_every_rewrite_keeps_the_pause_and_never_retargets_the_paused_peer(self):
        original = {'source_revision': A, 'peers': [TCLOUD, ORACLE]}
        targets = {'peer_scripts': d.peer_scripts(original, B),
                   'refresh_config': d.refresh_config(original, B, ['/codex'], '/home', '/home/cc-quota-v1.json'),
                   'publisher_config_for': json.loads(d.publisher_config_for(json.dumps(original).encode(), B))}
        for name, changed in targets.items():
            with self.subTest(writer=name):
                self.assertEqual(changed['paused_peers'], self.PAUSED)
                self.assertEqual([p['script'] for p in changed['peers']], [SCRIPT.format(A), SCRIPT.format(B)])
                self.assertEqual([p['id'] for p in d.collected_peers(changed)], ['oracle-node-1'])
                # A second deployment changes only the release, never the pause.
                self.assertEqual(d.peer_scripts(changed, B), changed)
        resumed = d.peer_scripts({**original, 'paused_peers': {}}, B)
        self.assertEqual((resumed['paused_peers'], [p['id'] for p in d.collected_peers(resumed)]),
                         ({}, ['tcloud', 'oracle-node-1']))
        for bad in ({'tcloud': 'retired'}, ['tcloud'], {'TCloud!': 'paused_by_decision'}):
            with self.subTest(bad=bad), self.assertRaisesRegex(ValueError, 'paused peers'):
                d.peer_scripts({**original, 'paused_peers': bad}, B)

    def test_oracle_publisher_registers_tcloud_paused_and_collects_none_of_it(self):
        payload = {'peers': [TCLOUD, ORACLE], 'required_receipt_features': []}
        for given in ({}, {'paused_peers': self.PAUSED}):
            with self.subTest(given=given):
                config = d.oracle_publisher_config({**payload, **given}, B, self.state)
                self.assertEqual(config['paused_peers'], self.PAUSED)
                self.assertEqual([p['id'] for p in d.collected_peers(config)], ['ownermac'])
                self.assertEqual(cost_refresh.paused_peers(config), self.PAUSED)
                self.assertEqual([p['id'] for p in cost_refresh.collected_peers(config)], ['ownermac'])

    def test_install_hands_oracle_the_pause_of_this_publisher(self):
        config = {'source_revision': A, 'peers': [TCLOUD, ORACLE]}
        d.atomic(self.state/'refresh.json', json.dumps(config).encode())
        registry = self.home/'.local/state/pulse/compute-cost/sources.json'
        registry.parent.mkdir(parents=True)
        d.atomic(self.state/'tenant-registry.json', json.dumps(TENANTS).encode())
        d.atomic(registry, json.dumps({'tenant_registry_path': str(self.state/'tenant-registry.json')}).encode())
        sent, answer = [], {}
        def run(argv, data=None, **kwargs):
            sent.append(json.loads(data))
            return json.dumps(answer or {'paused_peers': sent[-1]['paused_peers'], 'observing_peers': []}).encode()
        with patch.object(d, 'run', side_effect=run):
            d.install_oracle_publisher(self.home, B)
            d.atomic(self.state/'refresh.json', json.dumps({**config, 'paused_peers': {}}).encode())
            d.install_oracle_publisher(self.home, B)
            # An Oracle that kept another pause table, or misses the observing pusher, fails the install.
            answer.update(paused_peers=self.PAUSED, observing_peers=[])
            with self.assertRaisesRegex(ValueError, 'readback differs'):
                d.install_oracle_publisher(self.home, B)
            observing = {**TCLOUD, 'transport': 'shared_ledger_push', 'observe_sources': ['tcloud-relay']}
            d.atomic(self.state/'refresh.json', json.dumps({**config, 'peers': [observing, ORACLE],
                                                            'paused_peers': {}}).encode())
            answer.update(paused_peers={}, observing_peers=['tcloud'])
            self.assertEqual(d.install_oracle_publisher(self.home, B)['observing_peers'], ['tcloud'])
            answer.update(observing_peers=[])
            with self.assertRaisesRegex(ValueError, 'readback differs'):
                d.install_oracle_publisher(self.home, B)
        self.assertEqual([p['paused_peers'] for p in sent[:2]], [self.PAUSED, {}])

    def test_peer_collection_switch_pauses_and_resumes_between_cycles(self):
        config = {'source_revision': A, 'peers': [TCLOUD, ORACLE], 'extra': 'kept'}
        d.atomic(self.state/'refresh.json', json.dumps(config).encode())
        self.assertEqual(d.set_peer_collection(self.home, 'tcloud', 'active')['paused_peers'], {})
        self.assertEqual(json.loads((self.state/'refresh.json').read_bytes()), {**config, 'paused_peers': {}})
        # The before-image of the default configuration is retained.
        self.assertEqual(len(list(self.state.glob('refresh-before-peer-collection-*.json'))), 1)
        self.assertEqual(d.set_peer_collection(self.home, 'tcloud', 'paused')['paused_peers'], self.PAUSED)
        self.assertEqual(json.loads((self.state/'refresh.json').read_bytes())['paused_peers'], self.PAUSED)
        for peer, collection in (('vultr', 'paused'), ('tcloud', 'retired')):
            with self.subTest(peer=peer), self.assertRaises(ValueError):
                d.set_peer_collection(self.home, peer, collection)
        d.atomic(self.state/'refresh.json', json.dumps({**config, 'publisher': 'oracle-node-1'}).encode())
        with self.assertRaisesRegex(ValueError, 'publisher itself'):
            d.set_peer_collection(self.home, 'oracle-node-1', 'paused')


class OracleHome(Home):
    """An Oracle account on reader A with an installed publisher configuration."""
    def setUp(self):
        super().setUp()
        self.reader(A)
        self.reader(B)
        self.select(A)
        registry = self.home/'.local/state/pulse/compute-cost/sources.json'
        registry.parent.mkdir(parents=True)
        d.atomic(registry, json.dumps({'observation_core_entrypoint': '/core/harness.pyz'}).encode())
        config = d.oracle_publisher_config({'peers': [TCLOUD], 'required_receipt_features': [],
                                            'paused_peers': {}}, A, self.state)
        d.atomic(self.state/'refresh.json', json.dumps(config).encode())
        for target in (patch.object(d, 'ORACLE_HOME', self.home), patch.object(d, 'PUBLISHER_WAIT_SECONDS', 0.5),
                       patch.object(d.os, 'uname', return_value=SimpleNamespace(nodename='oracle-node-1'))):
            target.start()
            self.addCleanup(target.stop)

    def select(self, revision):
        current = self.home/'.local/share/pulse/stats/current'
        current.unlink()
        current.symlink_to(current.parent/'releases'/revision)

    def running_cycle(self):
        held = os.open(self.state/'cost-refresh.lock', os.O_CREAT | os.O_RDWR, 0o600)
        self.addCleanup(os.close, held)
        fcntl.flock(held, fcntl.LOCK_EX)
        return held

    def selected(self):
        return d.current_revision(self.home), json.loads((self.state/'refresh.json').read_bytes())['source_revision']

    def switch(self, **kwargs):
        return d.switch_peer_runtime(self.home, B, A, attempt='build-1', **kwargs)


class OracleCycleTests(OracleHome):
    """Oracle reader switch, rollback and installer wait for the publisher's running cycle."""
    def test_switch_and_rollback_wait_for_the_running_cycle(self):
        held = self.running_cycle()
        with self.assertRaises(TimeoutError):
            self.switch()
        self.assertEqual(self.selected(), (A, A))
        self.assertFalse(list(self.state.glob('runtime-switch-*')))
        fcntl.flock(held, fcntl.LOCK_UN)
        self.assertEqual(self.switch(), {'previous': A, 'selected': B})
        self.assertEqual(self.selected(), (B, B))
        fcntl.flock(held, fcntl.LOCK_EX)
        with self.assertRaises(TimeoutError):
            self.switch(restore=True)
        self.assertEqual(self.selected(), (B, B))
        fcntl.flock(held, fcntl.LOCK_UN)
        self.switch(restore=True)
        self.assertEqual(self.selected(), (A, A))

    def test_switch_waits_for_a_cycle_that_ends(self):
        held = self.running_cycle()
        order = []

        def finish():
            order.append('cycle_finished')
            fcntl.flock(held, fcntl.LOCK_UN)
        timer = threading.Timer(0.2, finish)
        timer.start()
        self.addCleanup(timer.cancel)
        with patch.object(d, 'PUBLISHER_WAIT_SECONDS', 10):
            self.switch()
        order.append('selected')
        self.assertEqual((order, self.selected()), (['cycle_finished', 'selected'], (B, B)))

    def test_installer_waits_for_the_cycle_and_binds_the_selector_it_reads_there(self):
        (self.home/'.config/systemd/user').mkdir(parents=True)
        payload = json.dumps({'peers': [TCLOUD, ORACLE], 'required_receipt_features': [],
                              'tenant_registry': TENANTS}).encode()
        before = (self.state/'refresh.json').read_bytes()
        held = self.running_cycle()
        with patch.object(d, 'run', return_value=b''), self.assertRaises(TimeoutError):
            d.receive_oracle_publisher(self.home, A, payload)
        self.assertEqual((self.state/'refresh.json').read_bytes(), before)
        fcntl.flock(held, fcntl.LOCK_UN)
        self.select(B)  # A reader switch completed while the installer waited.
        with patch.object(d, 'run', return_value=b''), self.assertRaisesRegex(ValueError, 'selected reader'):
            d.receive_oracle_publisher(self.home, A, payload)
        self.assertEqual((self.state/'refresh.json').read_bytes(), before)
        self.assertFalse((self.home/'.config/systemd/user/pulse-stats-refresh.service').exists())

    def test_ownermac_selector_is_not_bound_to_a_publisher_cycle(self):
        self.running_cycle()
        with patch.object(d, 'ORACLE_HOME', Path('/home/pulse')):
            self.assertEqual(self.switch()['selected'], B)


class OracleSwitchTransactionTests(OracleHome):
    """Selector and refresh.json move as one recorded transaction; every partial pair is recoverable."""
    real_atomic = staticmethod(d.atomic)
    real_select = staticmethod(d.select_runtime)

    def setUp(self):
        super().setUp()
        self.config_before = (self.state/'refresh.json').read_bytes()
        self.fail, self.skip = None, 0
        for target in (patch.object(d, 'atomic', side_effect=self.write),
                       patch.object(d, 'select_runtime', side_effect=self.select_reader)):
            target.start()
            self.addCleanup(target.stop)

    def write(self, path, data, **kwargs):
        if self.fail == ('write', path.name):
            if self.skip:
                self.skip -= 1
            else:
                self.fail = None
                raise OSError('disk full')
        return self.real_atomic(path, data, **kwargs)

    def select_reader(self, home, revision, expected, **kwargs):
        if self.fail == ('select', revision):
            self.fail = None
            raise OSError('selector replace failed')
        return self.real_select(home, revision, expected, **kwargs)

    def receipt(self, attempt='build-1'):
        return json.loads((self.state/('runtime-switch-'+B+'--'+attempt+'.json')).read_bytes())

    def assert_pre_image(self):
        self.assertEqual(self.selected(), (A, A))
        self.assertEqual((self.state/'refresh.json').read_bytes(), self.config_before)
        self.assertEqual(self.receipt()['status'], 'restored')

    def assert_target(self):
        self.assertEqual(self.selected(), (B, B))
        self.assertEqual(self.receipt()['status'], 'selected')
        self.assertEqual(self.receipt()['selector'], d.selector_identity(self.home))

    def interrupted(self, fail, pair):
        self.fail = fail
        with self.assertRaises(OSError):
            self.switch()
        self.assertEqual((self.selected(), self.receipt()['status']), (pair, 'switching'))
        config = self.receipt()['config']
        self.assertEqual((config['before'].encode(), config['after_sha256']),
                         (self.config_before, d.hashlib.sha256(
                             d.publisher_config_for(self.config_before, B)).hexdigest()))

    def test_config_write_failure_then_retry_completes_both(self):
        self.interrupted(('write', 'refresh.json'), (A, A))
        self.assertEqual(self.switch(), {'previous': A, 'selected': B})
        self.assert_target()

    def test_config_write_failure_then_restore_returns_the_pre_image(self):
        self.interrupted(('write', 'refresh.json'), (A, A))
        self.assertEqual(self.switch(restore=True)['selected'], A)
        self.assert_pre_image()

    def test_selector_failure_after_config_then_retry_completes_both(self):
        self.interrupted(('select', B), (A, B))
        self.switch()
        self.assert_target()

    def test_selector_failure_after_config_then_restore_returns_the_pre_image(self):
        self.interrupted(('select', B), (A, B))
        self.switch(restore=True)
        self.assert_pre_image()

    def test_lost_selected_receipt_then_retry_and_restore(self):
        self.skip = 1  # The intent lands; the 'selected' receipt write fails.
        self.interrupted(('write', 'runtime-switch-'+B+'--build-1.json'), (B, B))
        self.switch()
        self.assert_target()
        self.switch(restore=True)
        self.assert_pre_image()

    def test_restore_interrupted_after_the_selector_finishes_on_the_next_restore(self):
        self.switch()
        self.fail = ('write', 'refresh.json')
        with self.assertRaises(OSError):
            self.switch(restore=True)
        self.assertEqual((self.selected(), self.receipt()['status']), ((A, B), 'restoring'))
        with self.assertRaisesRegex(ValueError, 'being restored'):
            self.switch()
        self.switch(restore=True)
        self.assert_pre_image()

    def test_partial_pair_never_publishes(self):
        self.interrupted(('select', B), (A, B))
        with patch.object(cost_refresh, 'ROOT', self.home/'.local/share/pulse/stats/current'), \
                self.assertRaisesRegex(ValueError, 'runtime release identity mismatch'):
            cost_refresh.release_config(self.state/'refresh.json')

    def test_unfinished_switch_fences_every_other_selector_writer(self):
        self.interrupted(('write', 'refresh.json'), (A, A))
        with self.assertRaisesRegex(ValueError, 'unfinished runtime switch'):
            d.switch_peer_runtime(self.home, B, A, attempt='build-2')
        payload = json.dumps({'peers': [TCLOUD, ORACLE], 'required_receipt_features': [],
                              'tenant_registry': TENANTS}).encode()
        with patch.object(d, 'run', return_value=b''), self.assertRaisesRegex(ValueError, 'unfinished runtime switch'):
            d.receive_oracle_publisher(self.home, A, payload)
        (self.state/'refresh.json').unlink()  # Even without a publisher, the public select is fenced.
        with self.assertRaisesRegex(ValueError, 'unfinished runtime switch'):
            d.select_without_attempt(self.home, B, A)
        self.assertEqual(d.current_revision(self.home), A)

    def test_configuration_changed_by_another_writer_is_not_overwritten(self):
        self.interrupted(('select', B), (A, B))
        d.atomic(self.state/'refresh.json', b'{"source_revision": "' + A.encode() + b'", "peers": []}')
        changed = (self.state/'refresh.json').read_bytes()
        for restore in (False, True):
            with self.subTest(restore=restore), self.assertRaisesRegex(ValueError, 'configuration changed'):
                self.switch(restore=restore)
        self.assertEqual(((self.state/'refresh.json').read_bytes(), d.current_revision(self.home)), (changed, A))

    def test_public_select_refuses_an_installed_publisher_before_any_write(self):
        lock = self.state/'deployment.lock'
        self.assertFalse(lock.exists())
        argv = ['deploy.py', 'select', '--revision', B, '--expected', A]
        with patch.object(d.sys, 'argv', argv), patch.object(d.Path, 'home', return_value=self.home), \
                self.assertRaisesRegex(ValueError, 'source-plan-attempt'):
            d.main()
        self.assertEqual(self.selected(), (A, A))
        self.assertEqual((self.state/'refresh.json').read_bytes(), self.config_before)
        self.assertFalse(lock.exists())
        (self.state/'refresh.json').unlink()  # Without a publisher the selector alone moves.
        self.assertEqual(d.select_without_attempt(self.home, B, A), {'previous': A, 'selected': B})


class PublisherSwitchTests(Home):
    """ownermac switch over a simulated launchd: the job, its loaded arguments and idle state."""
    def setUp(self):
        super().setUp()
        release = self.home/'.local/share/pulse/releases'/('PULSE-'+A)
        (release/'Scripts').mkdir(parents=True)
        (release/'Scripts/pulse-stats-cost-refresh.py').write_text('')
        self.job = self.home/'Library/LaunchAgents'/(d.LABEL+'.plist')
        self.job.parent.mkdir(parents=True)
        self.job.write_bytes(plistlib.dumps({'Label': d.LABEL, 'RunAtLoad': True,
                                             'ProgramArguments': d.native_publisher_arguments(self.home, A)}))
        self.job_before = self.job.read_bytes()
        self.original = {'source_revision': A, 'peers': [TCLOUD, ORACLE], 'openrouter_env': '/x/.env'}
        d.atomic(self.state/'refresh.json', json.dumps(self.original).encode())
        self.config_before = (self.state/'refresh.json').read_bytes()
        self.loaded, self.loaded_job, self.active = True, plistlib.loads(self.job_before), False
        self.failures, self.calls = {}, []
        for target in (patch.object(d, 'run', side_effect=self.launchctl),
                       patch.object(d.subprocess, 'run', side_effect=self.probe),
                       patch.object(d, 'atomic', side_effect=self.write)):
            target.start()
            self.addCleanup(target.stop)

    real_atomic = staticmethod(d.atomic)

    def write(self, path, data, **kwargs):
        if self.failures.get('write') == path.name:
            del self.failures['write']
            raise OSError('disk full')
        return self.real_atomic(path, data, **kwargs)

    def probe(self, argv, **kwargs):
        self.assertEqual(argv[:2], ['/bin/launchctl', 'print'])
        return SimpleNamespace(returncode=0 if self.loaded else 113,
                               stderr=('Could not find service "'+d.LABEL+'"').encode())

    def launchctl(self, argv, **kwargs):
        self.assertEqual(argv[0], '/bin/launchctl')
        self.calls.append(argv[1])
        if argv[1] == 'print':
            if not self.loaded:
                raise RuntimeError('launchctl failed (113)')
            return ('state = '+('running\npid = 7' if self.active else 'not running')+'\narguments = {\n'
                    + '\n'.join(self.loaded_job['ProgramArguments'])+'\n}\n').encode()
        if argv[1] == 'bootout':
            if not self.loaded:
                raise RuntimeError('launchctl failed (113)')
            self.assertFalse(self.active, 'running cycle stopped')
            self.loaded = False
        elif argv[1] == 'bootstrap':
            if self.loaded:
                raise RuntimeError('launchctl failed (37)')
            if self.failures.pop('bootstrap', 0):
                raise TimeoutError('bootstrap failed')
            self.loaded, self.loaded_job = True, plistlib.loads(self.job.read_bytes())
        else:
            raise AssertionError(argv)
        return b''

    def switch(self, target):
        result = d.switch_publisher(self.home, A, target)
        return result, json.loads((self.state/'refresh.json').read_bytes()), plistlib.loads(self.job.read_bytes())

    def receipt(self):
        return json.loads((self.state/d.PUBLISHER_SWITCH_RECEIPT).read_bytes())

    def assert_original(self, loaded=True):
        self.assertEqual(((self.state/'refresh.json').read_bytes(), self.job.read_bytes()),
                         (self.config_before, self.job_before))
        self.assertEqual(self.loaded, loaded)
        if loaded:
            self.assertEqual(self.loaded_job['ProgramArguments'], d.native_publisher_arguments(self.home, A))

    def test_switch_to_oracle_and_back_restores_the_full_refresh(self):
        result, config, job = self.switch('oracle-node-1')
        self.assertEqual(result['mode'], 'collect-local')
        self.assertEqual([c for c in self.calls if c != 'print'], ['bootout', 'bootstrap'])
        self.assertEqual(config, {**self.original, 'publisher': 'oracle-node-1', 'peer_id': 'ownermac'})
        self.assertEqual(job['ProgramArguments'], d.native_publisher_arguments(self.home, A, 'collect-local'))
        self.assertEqual(self.loaded_job['ProgramArguments'], job['ProgramArguments'])
        self.assertEqual(self.receipt()['status'], 'switched')
        _, config, job = self.switch('ownermac')
        self.assertEqual(config, self.original)
        self.assertEqual(job['ProgramArguments'], d.native_publisher_arguments(self.home, A))
        self.assertTrue(list(self.state.glob('refresh-before-switch-*.json')))

    def test_switch_keeps_the_observe_mode_and_refuses_ownermac_while_observing(self):
        _, config, _ = self.switch('oracle-node-1')
        d.atomic(self.state/'refresh.json', json.dumps({**config, 'collect_local_mode': 'observe'}).encode())
        _, config, _ = self.switch('oracle-node-1')
        self.assertEqual(config['collect_local_mode'], 'observe')
        observing = (self.state/'refresh.json').read_bytes()
        with self.assertRaisesRegex(ValueError, 'observe-mode --mode full'):
            d.switch_publisher(self.home, A, 'ownermac')
        self.assertEqual((self.state/'refresh.json').read_bytes(), observing)
        self.assertEqual(self.loaded_job['ProgramArguments'][2], 'collect-local')

    def test_switch_refuses_a_publisher_that_is_not_the_selected_release(self):
        d.atomic(self.state/'refresh.json', json.dumps({**self.original, 'source_revision': B}).encode())
        with self.assertRaises(ValueError):
            d.switch_publisher(self.home, A, 'oracle-node-1')
        self.assertEqual(plistlib.loads(self.job.read_bytes())['ProgramArguments'][2], 'refresh')
        self.assertNotIn('bootout', self.calls)

    def test_running_cycle_or_unloaded_job_changes_nothing(self):
        self.active = True
        with self.assertRaisesRegex(ValueError, 'active'):
            d.switch_publisher(self.home, A, 'oracle-node-1')
        self.assert_original()
        self.active, self.loaded = False, False
        with self.assertRaisesRegex(ValueError, 'not loaded'):
            d.switch_publisher(self.home, A, 'oracle-node-1')
        self.assert_original(loaded=False)
        self.assertNotIn('bootout', self.calls)

    def test_config_write_failure_restores_the_job_and_a_retry_switches(self):
        self.failures['write'] = 'refresh.json'
        with self.assertRaisesRegex(OSError, 'disk full'):
            d.switch_publisher(self.home, A, 'oracle-node-1')
        self.assert_original()
        self.assertEqual((self.receipt()['status'], self.receipt()['rollback_pending']), ('restored', False))
        result, config, _ = self.switch('oracle-node-1')
        self.assertEqual((result['mode'], config['publisher'], self.receipt()['continued']),
                         ('collect-local', 'oracle-node-1', False))
        self.assertEqual(self.loaded_job['ProgramArguments'][2], 'collect-local')

    def test_bootstrap_failure_restores_the_original_job(self):
        self.failures['bootstrap'] = 1
        with self.assertRaises(TimeoutError):
            d.switch_publisher(self.home, A, 'oracle-node-1')
        self.assert_original()
        self.assertEqual(self.receipt()['status'], 'restored')
        self.assertEqual(self.switch('oracle-node-1')[0]['mode'], 'collect-local')

    def test_failed_recovery_is_continued_by_the_next_run_without_a_second_bootout(self):
        self.failures['bootstrap'] = 2  # The switch and its recovery both fail to load a job.
        def fail_twice(argv, **kwargs):
            if argv[1] == 'bootstrap' and self.failures.get('bootstrap'):
                self.failures['bootstrap'] -= 1
                raise TimeoutError('bootstrap failed')
            return self.launchctl(argv, **kwargs)
        with patch.object(d, 'run', side_effect=fail_twice), self.assertRaises(TimeoutError):
            d.switch_publisher(self.home, A, 'oracle-node-1')
        self.assert_original(loaded=False)
        receipt = self.receipt()
        self.assertEqual((receipt['status'], receipt['rollback_pending'], receipt['job_was_loaded']),
                         ('failed', True, True))
        # A retry finds no loaded job: it continues the same switch instead of failing on bootout.
        self.calls.clear()
        self.failures['bootstrap'] = 1
        with self.assertRaises(TimeoutError):
            d.switch_publisher(self.home, A, 'ownermac')
        self.assertNotIn('bootout', self.calls)
        # Its own failure restores the first switch's before-image, job loaded again.
        self.assert_original()
        self.assertEqual((self.receipt()['status'], self.receipt()['continued']), ('restored', True))
        _, config, job = self.switch('oracle-node-1')
        self.assertEqual((config['publisher'], self.loaded_job['ProgramArguments']),
                         ('oracle-node-1', job['ProgramArguments']))

    def test_continued_switch_completes_to_the_requested_publisher(self):
        d.atomic(self.state/d.PUBLISHER_SWITCH_RECEIPT, json.dumps({
            'revision': A, 'status': 'switching', 'job_was_loaded': True,
            'config_before_sha256': d.hashlib.sha256(self.config_before).hexdigest(),
            'job_before_sha256': d.hashlib.sha256(self.job_before).hexdigest()}).encode())
        with self.assertRaisesRegex(ValueError, 'before-image'):
            d.switch_publisher(self.home, A, 'oracle-node-1')  # The interrupted run kept no before-image.
        self.assert_original()
        d.atomic(self.state/('refresh-before-switch-'+d.hashlib.sha256(self.config_before).hexdigest()[:16]+'.json'),
                 self.config_before)
        d.atomic(self.state/('launchagent-before-switch-'+d.hashlib.sha256(self.job_before).hexdigest()[:16]+'.plist'),
                 self.job_before)
        self.loaded = False  # Interrupted after bootout.
        result, config, _ = self.switch('oracle-node-1')
        self.assertEqual((result['mode'], config['peer_id'], self.receipt()['continued'], self.loaded),
                         ('collect-local', 'ownermac', True, True))
        self.assertNotIn('bootout', self.calls)



class ObserveModeTests(Home):
    """ownermac collect-local between full and observe-only over a simulated Oracle installer."""
    def setUp(self):
        super().setUp()
        self.original = {'source_revision': A, 'peers': [TCLOUD, ORACLE], 'publisher': 'oracle-node-1',
                         'peer_id': 'ownermac', 'extra': 'kept'}
        d.atomic(self.state/'refresh.json', json.dumps(self.original).encode())
        self.before = (self.state/'refresh.json').read_bytes()
        registry = self.home/'.local/state/pulse/compute-cost/sources.json'
        registry.parent.mkdir(parents=True)
        d.atomic(self.state/'tenant-registry.json', json.dumps(TENANTS).encode())
        d.atomic(registry, json.dumps({'tenant_registry_path': str(self.state/'tenant-registry.json'),
                                       'sources': [{'id': i, 'kind': 'cc'} for i in OWNERMAC_SOURCES]}).encode())
        self.calls, self.failures, self.answer, self.during = [], 0, None, None
        self.oracle_config, self.outcomes = None, []
        target = patch.object(d, 'run', side_effect=self.oracle)
        target.start()
        self.addCleanup(target.stop)

    def local(self):
        return json.loads((self.state/'refresh.json').read_bytes())

    def oracle(self, argv, data=None, **kwargs):
        """receive-oracle-publisher on Oracle: install the configuration, answer its readback."""
        self.assertIn('receive-oracle-publisher', argv[-1])
        payload = json.loads(data)
        self.calls.append((self.local().get('collect_local_mode', 'full'), payload.get('observe_sources')))
        during, self.during = self.during, None
        if during is not None:
            during()
        if self.failures:
            self.failures -= 1
            raise RuntimeError('ssh failed')
        # 'down': unreachable; 'lost': installed, reply lost; 'exit': installed, this process ends.
        outcome = self.outcomes.pop(0) if self.outcomes else 'ok'
        if outcome == 'down':
            raise RuntimeError('ssh failed')
        self.oracle_config = d.oracle_publisher_config(payload, A, self.home/'oracle')
        if outcome == 'lost':
            raise RuntimeError('ssh failed')
        if outcome == 'exit':
            raise KeyboardInterrupt
        observing = d.observing_registration(self.oracle_config['peers'], self.oracle_config['paused_peers'])
        answer = {'paused_peers': self.oracle_config['paused_peers'], 'observing_peers': sorted(observing),
                  'observe_sources': observing}
        return json.dumps(self.answer(answer) if self.answer else answer).encode()

    def judged(self):
        return d.observing_registration(self.oracle_config['peers'], self.oracle_config['paused_peers'])

    def test_observe_registers_oracle_first_and_full_ends_it_first(self):
        result = d.set_observe_mode(self.home, 'ownermac', 'observe')
        self.assertEqual(result, {'peer': 'ownermac', 'collect_local_mode': 'observe', 'observing_peers': ['ownermac']})
        # Oracle took the registration while ownermac still collected in full.
        self.assertEqual(self.calls, [('full', OWNERMAC_SOURCES)])
        self.assertEqual(self.judged(), {'ownermac': OWNERMAC_SOURCES})
        self.assertEqual(self.local(), {**self.original, 'collect_local_mode': 'observe'})
        self.assertEqual(len(list(self.state.glob('refresh-before-observe-mode-*.json'))), 1)
        # Any later install hands Oracle the same registration, so a redeployment keeps it.
        d.install_oracle_publisher(self.home, A)
        self.assertEqual(self.calls[-1], ('observe', OWNERMAC_SOURCES))
        result = d.set_observe_mode(self.home, 'ownermac', 'full')
        self.assertEqual(result['observing_peers'], [])
        self.assertEqual(self.calls[-1], ('observe', None))  # Oracle stopped judging before full resumed.
        self.assertEqual((self.judged(), self.local()), ({}, {**self.original, 'collect_local_mode': 'full'}))

    def test_every_deployment_writer_keeps_the_observe_mode(self):
        observing = {**self.original, 'collect_local_mode': 'observe'}
        job = {'Label': d.LABEL, 'ProgramArguments': []}
        with patch.object(d, 'selected_publisher_runtime', return_value=self.home/'releases'/A):
            local, _ = d.local_refresh_inputs(self.home, B, A, observing, job)
        for name, changed in (('peer_scripts', d.peer_scripts(observing, B)),
                              ('refresh_config', d.refresh_config(observing, B, ['/codex'], '/home', '/home/cc.json')),
                              ('local_refresh_inputs', local)):
            with self.subTest(writer=name):
                # Only the release moves; the mode and the publisher fields stay as they were.
                kept = ('collect_local_mode', 'publisher', 'peer_id', 'extra')
                self.assertEqual({k: changed[k] for k in kept}, {k: observing[k] for k in kept})

    def test_cli_runs_the_switch_for_the_resident_account(self):
        argv = ['deploy.py', 'observe-mode', '--peer', 'ownermac', '--mode', 'observe']
        with patch.object(d.sys, 'argv', argv), patch.object(d, 'set_observe_mode', return_value={}) as switch:
            d.main()
        switch.assert_called_once_with(d.PUBLISHER_HOME, 'ownermac', 'observe')

    def test_refuses_without_the_oracle_publisher_another_peer_or_mode(self):
        d.atomic(self.state/'refresh.json', json.dumps({**self.original, 'publisher': None}).encode())
        before = (self.state/'refresh.json').read_bytes()
        with self.assertRaisesRegex(ValueError, 'Oracle publisher'):
            d.set_observe_mode(self.home, 'ownermac', 'observe')
        for peer, mode in (('tcloud', 'observe'), ('vultr', 'full'), ('ownermac', 'partial')):
            with self.subTest(peer=peer, mode=mode), self.assertRaises(ValueError):
                d.set_observe_mode(self.home, peer, mode)
        self.assertEqual(((self.state/'refresh.json').read_bytes(), self.calls), (before, []))

    def test_an_unresolved_publisher_switch_or_running_cycle_changes_nothing(self):
        d.atomic(self.state/d.PUBLISHER_SWITCH_RECEIPT, json.dumps({'status': 'failed', 'rollback_pending': True}).encode())
        with self.assertRaisesRegex(ValueError, 'publisher switch'):
            d.set_observe_mode(self.home, 'ownermac', 'observe')
        (self.state/d.PUBLISHER_SWITCH_RECEIPT).unlink()
        held = os.open(self.state/'cost-refresh.lock', os.O_CREAT | os.O_RDWR, 0o600)
        self.addCleanup(os.close, held)
        fcntl.flock(held, fcntl.LOCK_EX)
        with patch.object(d, 'PUBLISHER_WAIT_SECONDS', 0.3), self.assertRaises(TimeoutError):
            d.set_observe_mode(self.home, 'ownermac', 'observe')
        self.assertEqual(((self.state/'refresh.json').read_bytes(), self.calls), (self.before, []))

    def test_an_oracle_readback_that_differs_restores_the_before_image(self):
        for answer in (lambda a: {**a, 'observing_peers': []},
                       lambda a: {**a, 'observe_sources': {'ownermac': ['relay-host']}}):
            with self.subTest():
                self.calls.clear()
                self.answer = answer
                with self.assertRaisesRegex(ValueError, 'readback differs'):
                    d.set_observe_mode(self.home, 'ownermac', 'observe')
                self.answer = None
                self.assertEqual(self.calls[-1], ('full', None))  # Oracle given the before-image again.
                self.assertEqual(((self.state/'refresh.json').read_bytes(), self.judged()), (self.before, {}))

    def test_a_local_write_failure_restores_oracle(self):
        real = d.atomic
        def atomic(path, data, **kwargs):
            if path.name == 'refresh.json':
                raise OSError('disk full')
            return real(path, data, **kwargs)
        with patch.object(d, 'atomic', side_effect=atomic), self.assertRaisesRegex(OSError, 'disk full'):
            d.set_observe_mode(self.home, 'ownermac', 'observe')
        self.assertEqual([c[1] for c in self.calls], [OWNERMAC_SOURCES, None])
        self.assertEqual(((self.state/'refresh.json').read_bytes(), self.judged()), (self.before, {}))

    def test_another_writers_change_is_never_replaced(self):
        changed = json.dumps({**self.original, 'extra': 'theirs'}).encode()
        self.during = lambda: d.atomic(self.state/'refresh.json', changed)
        with self.assertRaisesRegex(ValueError, 'another writer'):
            d.set_observe_mode(self.home, 'ownermac', 'observe')
        self.assertEqual(((self.state/'refresh.json').read_bytes(), self.judged()), (changed, {}))

    def receipt(self):
        return json.loads((self.state/d.OBSERVE_MODE_RECEIPT).read_bytes())

    def writers(self):
        """Every public writer of refresh.json or the Oracle registration on ownermac."""
        job = self.home/'Library/LaunchAgents'/(d.LABEL+'.plist')
        job.parent.mkdir(parents=True, exist_ok=True)
        job.write_bytes(plistlib.dumps({'Label': d.LABEL, 'ProgramArguments': []}))
        args = SimpleNamespace(revision=B, expected=A, source=self.home, native_publisher_only=False)

        def deploy():
            with patch('pwd.getpwuid', return_value=SimpleNamespace(pw_dir=str(self.home))):
                d.deploy(args)
        return (('install-oracle-publisher', lambda: d.install_oracle_publisher(self.home, A)),
                ('peer-collection', lambda: d.set_peer_collection(self.home, 'tcloud', 'active')),
                ('switch-publisher', lambda: d.switch_publisher(self.home, A, 'ownermac')),
                ('deploy', deploy),
                ('deploy-refresh-local', lambda: d.deploy_refresh_local(args, home=self.home)))

    def assert_fenced(self):
        """Every writer of either registration refuses while observe-mode.json is unresolved."""
        calls, before = list(self.calls), (self.state/'refresh.json').read_bytes()
        for name, write in self.writers():
            with self.subTest(writer=name), self.assertRaisesRegex(ValueError, 'unresolved observe-mode'):
                write()
        self.assertEqual((self.calls, (self.state/'refresh.json').read_bytes()), (calls, before))

    def in_flight(self, call):
        """Run ``call`` on another thread, held at its Oracle install until released."""
        started, release, errors = threading.Event(), threading.Event(), []
        self.during = lambda: (started.set(), release.wait(10))

        def target():
            try:
                call()
            except BaseException as exc:
                errors.append(exc)
        thread = threading.Thread(target=target)
        thread.start()
        self.addCleanup(thread.join, 10)
        self.addCleanup(release.set)
        self.assertTrue(started.wait(10))

        def finish():
            release.set()
            thread.join(10)
            return errors
        return finish

    def test_an_install_in_flight_holds_off_observe_mode_both_ways(self):
        for mode, judged in (('observe', {'ownermac': OWNERMAC_SOURCES}), ('full', {})):
            with self.subTest(mode=mode):
                # An install that read the other mode reaches Oracle only before the switch, never after it.
                path = self.state/d.OBSERVE_MODE_RECEIPT
                receipt = path.read_bytes() if path.exists() else None
                finish = self.in_flight(lambda: d.install_oracle_publisher(self.home, A))
                with self.assertRaises(TimeoutError):
                    d.set_observe_mode(self.home, 'ownermac', mode)
                self.assertEqual(path.read_bytes() if path.exists() else None, receipt)
                self.assertEqual(finish(), [])
                d.set_observe_mode(self.home, 'ownermac', mode)
                self.assertEqual((self.local()['collect_local_mode'], self.judged(), self.receipt()['status']),
                                 (mode, judged, 'completed'))

    def test_observe_mode_in_flight_fences_every_writer_through_its_recovery(self):
        for outcomes, status in (([], 'completed'), (['lost', 'down'], 'failed')):
            with self.subTest(outcomes=outcomes):
                d.atomic(self.state/'refresh.json', self.before)
                (self.state/d.OBSERVE_MODE_RECEIPT).unlink(missing_ok=True)
                self.calls.clear()
                self.outcomes = list(outcomes)
                finish = self.in_flight(lambda: d.set_observe_mode(self.home, 'ownermac', 'observe'))
                for name, write in self.writers():
                    with self.subTest(writer=name), self.assertRaises(TimeoutError):
                        write()
                self.assertEqual(len(self.calls), 1)  # Only observe-mode reached Oracle.
                errors = finish()
                self.assertEqual(([type(e) for e in errors], self.receipt()['status']),
                                 ([RuntimeError] if outcomes else [], status))
                if outcomes:
                    self.assert_fenced()
                    d.set_observe_mode(self.home, 'ownermac', 'full')
                    self.assertEqual((self.receipt()['status'], self.judged()), ('restored', {}))
                else:
                    self.assertEqual(self.judged(), {'ownermac': OWNERMAC_SOURCES})

    def test_a_lost_reply_and_an_unreachable_restore_fence_until_a_rerun_settles_both(self):
        for rerun, settled, judged in (('observe', 'completed', {'ownermac': OWNERMAC_SOURCES}),
                                       ('full', 'restored', {})):
            with self.subTest(rerun=rerun):
                d.atomic(self.state/'refresh.json', self.before)
                (self.state/d.OBSERVE_MODE_RECEIPT).unlink(missing_ok=True)
                self.outcomes = ['lost', 'down']
                with self.assertRaisesRegex(RuntimeError, 'unconfirmed'):
                    d.set_observe_mode(self.home, 'ownermac', 'observe')
                # Oracle judges ownermac while it still collects in full: the receipt holds that open.
                self.assertEqual((self.judged(), (self.state/'refresh.json').read_bytes()),
                                 ({'ownermac': OWNERMAC_SOURCES}, self.before))
                receipt = self.receipt()
                self.assertEqual((receipt['status'], receipt['rollback_pending'], receipt['from'], receipt['to']),
                                 ('failed', True, 'full', 'observe'))
                self.assert_fenced()
                d.set_observe_mode(self.home, 'ownermac', rerun)
                self.assertEqual((self.receipt()['status'], self.judged(), self.local().get('collect_local_mode', 'full')),
                                 (settled, judged, rerun))
                self.assertEqual(self.calls[-1][1], judged.get('ownermac'))

    def test_back_to_full_with_a_lost_reply_is_finished_by_the_rerun(self):
        d.set_observe_mode(self.home, 'ownermac', 'observe')
        observing = (self.state/'refresh.json').read_bytes()
        self.outcomes = ['lost', 'down']
        with self.assertRaisesRegex(RuntimeError, 'unconfirmed'):
            d.set_observe_mode(self.home, 'ownermac', 'full')
        # Oracle stopped judging ownermac while it still observes: nobody writes its sources until settled.
        self.assertEqual((self.judged(), (self.state/'refresh.json').read_bytes()), ({}, observing))
        self.assert_fenced()
        d.set_observe_mode(self.home, 'ownermac', 'full')
        self.assertEqual((self.receipt()['status'], self.judged(), self.local()['collect_local_mode']),
                         ('completed', {}, 'full'))

    def test_a_process_ending_after_the_oracle_write_leaves_the_switch_open(self):
        self.outcomes = ['exit']
        with self.assertRaises(KeyboardInterrupt):
            d.set_observe_mode(self.home, 'ownermac', 'observe')
        self.assertEqual((self.receipt()['status'], (self.state/'refresh.json').read_bytes()),
                         ('switching', self.before))
        self.assert_fenced()
        d.set_observe_mode(self.home, 'ownermac', 'observe')
        self.assertEqual((self.receipt()['status'], self.receipt()['continued']), ('completed', True))
        self.assertEqual((self.judged(), self.local()['collect_local_mode']), ({'ownermac': OWNERMAC_SOURCES}, 'observe'))

    def test_a_failed_rollback_says_the_oracle_registration_is_unconfirmed(self):
        self.failures = 2
        with self.assertRaisesRegex(RuntimeError, 'unconfirmed'):
            d.set_observe_mode(self.home, 'ownermac', 'observe')
        self.assertEqual((self.state/'refresh.json').read_bytes(), self.before)


if __name__ == '__main__':
    unittest.main()
