"""Full installer with real private files/locks and simulated external actors."""
from copy import deepcopy
import json
import os
from pathlib import Path
import plistlib
import pwd
import shlex
import shutil
import subprocess
import sys
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch

from Stats.exporter.tests.test_stats_deploy import d, A, B, payload

C = 'c' * 40
D = 'd' * 40


class DeploymentTransactionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.home = self.root / 'mac'
        self.state = self.home / '.local/state/pulse/stats'
        self.state.mkdir(parents=True, mode=0o700)
        # The retired Vultr peer stays in the original refresh.json; it has no
        # home here, so any contact with it fails the test.
        self.peer_homes = {'tcloud-node2': self.root / 'tcloud'}
        for home, revision in [(self.home, A), *[(p, C) for p in self.peer_homes.values()]]:
            (home / '.local/state/pulse/stats').mkdir(parents=True, exist_ok=True)
            registry = home / '.local/state/pulse/compute-cost/sources.json'
            registry.parent.mkdir(parents=True)
            d.atomic(registry, b'{"sources":[],"ledger_root":"retained"}')
            d.stage_runtime(payload(), home, revision)
            current = home / '.local/share/pulse/stats/current'
            current.symlink_to(current.parent / 'releases' / revision)
        peers = [{'id': name, 'host': host, 'python': '/usr/bin/python3', 'prefix': prefix,
                  'script': f'/home/pulse/.local/share/pulse/stats/releases/{C}/Scripts/pulse-stats-cost-refresh.py'}
                 for name, host, prefix in [('vultr', 'vultr-pulse', []),
                                            ('tcloud', 'tcloud-node2', ['runuser', '-u', 'pulse', '--'])]]
        # TCloud is collected here (explicitly resumed); PausedPeerDeploymentTests covers the default.
        self.original = {'source_revision': A, 'peers': peers, 'destination': deepcopy(peers[0]),
                         'openrouter_env': '/private/retained.env', 'extra': 'retained', 'paused_peers': {}}
        self.config = self.state / 'refresh.json'
        d.atomic(self.config, json.dumps(self.original).encode())
        self.old_config = self.config.read_bytes()
        self.job_path = self.home / 'Library/LaunchAgents' / (d.LABEL + '.plist')
        self.job_path.parent.mkdir(parents=True)
        self.job = {'Label': d.LABEL, 'StartInterval': 900, 'RunAtLoad': True,
                    'ProgramArguments': [str(self.home / '.local/share/coevolution/python-env/bin/python'),
                                         str(self.home / '.local/share/pulse/stats/releases' / A / 'Scripts/pulse-stats-cost-refresh.py'),
                                         'refresh', '--config', str(self.config)]}
        d.atomic(self.job_path, plistlib.dumps(self.job))
        self.old_job = self.job_path.read_bytes()
        self.installed = self.root / 'Applications/PulseStats.app'
        self.candidate = self.root / 'candidate.app'
        for app, revision in [(self.installed, C), (self.candidate, B)]:
            (app / 'Contents/MacOS').mkdir(parents=True)
            (app / 'Contents/MacOS/PulseStats').write_bytes(revision.encode())
            (app / 'Contents/Info.plist').write_bytes(plistlib.dumps({
                'PulseSourceRevision': revision, 'PulseBuildId': 'run-2', 'CFBundleShortVersionString': 'test'}))
        self.args = SimpleNamespace(source=self.root, revision=B, expected=A, app=self.candidate,
                                    codex_command=['/node', '/codex'], codex_home=self.home / '.codex',
                                    adopt_runtime_sources=False, defer_refresh=False)
        installed = self.installed

        def fixture_path(*args):
            if args == ('/Applications/PulseStats.app',):
                args = (str(installed),)
            return Path(*args)

        self.loaded = True
        self.active = False
        self.loaded_job = deepcopy(self.job)
        self.events = []
        self.real_run = subprocess.run
        self.fail_preflight = None
        self.fail_select = None
        self.fail_restore = None
        self.drift = None
        self.preflight_drift = None
        self.bootout_timeout = False
        self.bootstrap_timeout = False
        self.partial_bootstrap = False
        self.fail_source = None
        self.immutable_source = False
        for target, name, kwargs in [
            (d, 'Path', {'new': fixture_path}),
            (pwd, 'getpwuid', {'return_value': SimpleNamespace(pw_dir=str(self.home))}),
            (d, 'archive', {'return_value': payload()}),
            (d, 'canonical_python', {'return_value': Path(self.job['ProgramArguments'][0])}),
            (d, 'run', {'side_effect': self.execute}),
            (d, 'stop_installed_app', {'side_effect': lambda app: self.events.append('app-stop')}),
            (d, 'start_installed_app', {'side_effect': lambda app: self.events.append('app-start')}),
            (d.subprocess, 'run', {'side_effect': self.scheduler_readback}),
        ]:
            p = patch.object(target, name, **kwargs)
            p.start()
            self.addCleanup(p.stop)

    def scheduler_readback(self, argv, **kwargs):
        if argv[:2] == ['/bin/launchctl', 'bootout']:
            self.loaded = False
            return SimpleNamespace(returncode=0)
        self.assertEqual(argv[:2], ['/bin/launchctl', 'print'])
        return SimpleNamespace(returncode=0 if self.loaded else 113,
                               stderr=('Could not find service "' + d.LABEL + '"').encode())

    def execute(self, argv, **kwargs):
        if argv[0] == '/usr/bin/ssh':
            host = argv[-2]
            args = shlex.split(argv[-1])
            if args[:4] == ['runuser', '-u', 'pulse', '--']:
                args = args[4:]
            home = self.peer_homes[host]
            if args[1] == '-c':
                d.stage_runtime(kwargs['data'], home, args[-1])
                return b'staged'
            mode = args[2]
            self.events.append(host + ':' + mode)
            revision = args[args.index('--revision') + 1]
            expected = args[args.index('--expected') + 1] if '--expected' in args else None
            attempt = args[args.index('--source-plan-attempt') + 1] if '--source-plan-attempt' in args else None
            if mode == 'preflight':
                if self.fail_preflight == host:
                    raise RuntimeError('incompatible peer reader')
                if self.preflight_drift == host and expected is not None:
                    self.set_current(home, D)
                return json.dumps(d.preflight_runtime(home, revision, expected, attempt=attempt)).encode()
            if mode == 'prepare-sources':
                return json.dumps(self.prepare(home, revision, attempt=attempt)).encode()
            if mode in ('select-sources', 'restore-sources'):
                result = d.select_sources(home, revision, attempt=attempt, restore=mode == 'restore-sources')
                if mode == 'select-sources' and self.fail_source == host:
                    raise TimeoutError('source switch reply lost')
                return json.dumps(result).encode()
            if mode == 'select':
                if self.drift == host:
                    self.set_current(home, D)
                if attempt is not None:
                    result = d.switch_peer_runtime(home, revision, expected, attempt=attempt)
                else:
                    result = {'previous': d.select_runtime(home, revision, expected)}
                if self.fail_select == host:
                    raise TimeoutError('reply lost after peer switch')
                return json.dumps(result).encode()
            if mode == 'restore-runtime':
                if self.fail_restore == host:
                    raise RuntimeError('peer restore unavailable')
                return json.dumps(d.switch_peer_runtime(home, revision, expected, attempt=attempt, restore=True)).encode()
            raise AssertionError(args)
        if argv[0] == '/bin/launchctl':
            self.events.append(argv[1])
            if argv[1] == 'print':
                return ('state = ' + ('running\npid = 123' if self.active else 'not running') +
                        '\narguments = {\n' + '\n'.join(self.loaded_job['ProgramArguments']) + '\n}\n').encode()
            if argv[1] == 'bootout':
                self.assertFalse(self.active)
                self.loaded = False
                if self.bootout_timeout:
                    self.bootout_timeout = False
                    raise TimeoutError('bootout reply lost')
            elif argv[1] == 'bootstrap':
                if self.bootstrap_timeout:
                    self.bootstrap_timeout = False
                    self.loaded = self.partial_bootstrap
                    self.active = self.partial_bootstrap
                    raise TimeoutError('bootstrap reply lost')
                self.loaded = True
                self.loaded_job = plistlib.loads(self.job_path.read_bytes())
            return b''
        if argv[0] == '/usr/bin/ditto':
            shutil.copytree(argv[1], argv[2])
            return b''
        if argv[0] == self.job['ProgramArguments'][0]:
            return b'available'
        raise AssertionError(argv)

    def prepare(self, home, revision, site=None, *, attempt=None, **kwargs):
        registry, before, after, plan = d._source_plan_paths(home, revision, attempt)
        old = registry.read_bytes()
        candidate = json.loads(old)
        candidate['prepared'] = True
        if self.immutable_source:
            candidate['sources'] = [{'id': 'native', 'kind': 'codex', 'session_wi_mcp_since': '2026-01-01T00:00:00Z'}]
        new = json.dumps(candidate).encode()
        manifest = home / 'manifest.json'
        d.atomic(manifest, b'{}')
        metadata = {'revision': revision, 'attempt': attempt, 'manifest': str(manifest),
                    'manifest_sha256': d.hashlib.sha256(b'{}').hexdigest(),
                    'before_sha256': d.hashlib.sha256(old).hexdigest(),
                    'after_sha256': d.hashlib.sha256(new).hexdigest()}
        for path, data in ((before, old), (after, new), (plan, json.dumps(metadata).encode())):
            d.atomic(path, data)
        return metadata

    def set_current(self, home, revision):
        d.stage_runtime(payload(), home, revision)
        current = home / '.local/share/pulse/stats/current'
        current.unlink()
        current.symlink_to(current.parent / 'releases' / revision)

    def revision(self, home):
        return (home / '.local/share/pulse/stats/current/.stats-source-revision').read_text().strip()

    def receipt(self):
        return json.loads((self.state / 'deployment.json').read_bytes())

    def assert_restored(self):
        self.assertTrue(self.loaded)
        self.assertEqual(self.config.read_bytes(), self.old_config)
        self.assertEqual(self.job_path.read_bytes(), self.old_job)
        self.assertEqual(self.revision(self.home), A)
        self.assertEqual(plistlib.loads((self.installed / 'Contents/Info.plist').read_bytes())['PulseSourceRevision'], C)

    def test_mixed_initial_versions_use_each_peers_observed_previous(self):
        d.deploy(self.args)
        self.assertEqual(self.revision(self.home), B)
        self.assertEqual([self.revision(p) for p in self.peer_homes.values()], [B])
        self.assertEqual(self.receipt()['peer_previous'], dict.fromkeys(self.peer_homes, C))
        index = list((self.state/'retention-adoptions').glob('*.json'))
        self.assertEqual(len(index), 1)
        record = json.loads(index[0].read_bytes())
        self.assertEqual(record['component'], 'full_app_peer_deployment')
        self.assertEqual(record['peer_previous'], dict.fromkeys(self.peer_homes, C))
        self.assertEqual({app['source_revision'] for app in record['app_paths']}, {B, C})
        self.assertEqual(record['acceptance'], 'unverified')

    def test_deployment_migrates_refresh_json_off_the_retired_vultr_node(self):
        d.deploy(self.args)
        config = json.loads(self.config.read_bytes())
        self.assertEqual([p['id'] for p in config['peers']], ['tcloud'])
        self.assertNotIn('destination', config)
        self.assertEqual({k: v for k, v in config.items() if k in ('openrouter_env', 'extra')},
                         {'openrouter_env': '/private/retained.env', 'extra': 'retained'})
        # The original bytes are retained privately for rollback and audit.
        before = self.state / ('refresh-before-' + B + '.json')
        self.assertEqual(before.read_bytes(), self.old_config)
        self.assertEqual(before.stat().st_mode & 0o077, 0)
        self.assertFalse(any(event.startswith('vultr-pulse:') for event in self.events))

    def test_default_paused_tcloud_gets_the_reader_but_no_collection_steps(self):
        # The 2026-11-15 default: no field means TCloud is paused for collection.
        # The automation view still reads its task state, so its reader advances
        # with every deployment; only collection-side steps skip it.
        original = {k: v for k, v in self.original.items() if k != 'paused_peers'}
        d.atomic(self.config, json.dumps(original).encode())
        self.args.adopt_runtime_sources = True
        with patch.object(d, 'prepare_sources', side_effect=self.prepare), \
                patch.object(d, 'select_sources', return_value={'selected': B}):
            d.deploy(self.args)
        config = json.loads(self.config.read_bytes())
        self.assertEqual(config['paused_peers'], {'tcloud': 'paused_by_decision'})
        # Its collection entrypoint stays on the release last used for collection.
        self.assertEqual([(p['id'], p['script']) for p in config['peers']],
                         [('tcloud', self.original['peers'][1]['script'])])
        self.assertEqual(self.revision(self.peer_homes['tcloud-node2']), B)
        self.assertEqual(self.receipt()['peer_previous'], {'tcloud-node2': C})
        self.assertIn('tcloud-node2:select', self.events)
        self.assertFalse(any(event in ('tcloud-node2:prepare-sources', 'tcloud-node2:select-sources')
                             for event in self.events))

    def native_publisher(self):
        release = self.home / '.local/share/pulse/releases' / ('PULSE-' + D)
        (release / 'Scripts').mkdir(parents=True)
        (release / 'Scripts/pulse-stats-cost-refresh.py').write_text('# native publisher\n')
        self.job['ProgramArguments'][1] = str(release / 'Scripts/pulse-stats-cost-refresh.py')
        self.loaded_job = deepcopy(self.job)
        d.atomic(self.job_path, plistlib.dumps(self.job))
        d.atomic(self.config, json.dumps({**self.original, 'source_revision': D}).encode())
        self.old_job, self.old_config = self.job_path.read_bytes(), self.config.read_bytes()
        self.args.expected = D

    def test_native_release_publisher_is_kept_while_readers_peer_scripts_and_app_advance(self):
        self.native_publisher()
        d.deploy(self.args)
        self.assertEqual(self.revision(self.home), B)
        self.assertEqual([self.revision(p) for p in self.peer_homes.values()], [B])
        self.assertEqual(self.job_path.read_bytes(), self.old_job)
        config = json.loads(self.config.read_bytes())
        self.assertEqual(config['source_revision'], D)
        self.assertEqual({p['script'] for p in d.configured_peers(config)},
                         {f'/home/pulse/.local/share/pulse/stats/releases/{B}/Scripts/pulse-stats-cost-refresh.py'})
        self.assertEqual({k: v for k, v in config.items() if k not in ('peers', 'destination')},
                         {k: v for k, v in json.loads(self.old_config).items() if k not in ('peers', 'destination')})
        self.assertFalse((self.home / '.local/share/pulse/stats/publisher-releases').exists())
        receipt = self.receipt()
        self.assertEqual((receipt['previous'], receipt['shared_previous'], receipt['publisher_owner']), (D, A, 'pulse_release'))
        self.assertNotIn('publisher_policy', receipt)
        self.assertEqual(plistlib.loads((self.installed / 'Contents/Info.plist').read_bytes())['PulseSourceRevision'], B)
        self.assertFalse((self.state / 'publisher-enrollments').exists())

    def test_deployment_moves_the_entry_of_a_paused_oracle_publisher_with_the_reader_it_selects(self):
        # A paused peer keeps its entry, but this deployment still selects B on Oracle;
        # the entry ownermac pulls and pushes through must follow it.
        self.peer_homes = {'pulse@oracle-node-1': self.peer_homes['tcloud-node2']}
        oracle = {'id': 'oracle-node-1', 'host': 'pulse@oracle-node-1', 'python': d.PEER_PYTHON,
                  'transport': 'shared_ledger',
                  'script': f'/home/pulse/.local/share/pulse/stats/releases/{C}/Scripts/pulse-stats-cost-refresh.py'}
        self.original = {'source_revision': A, 'peers': [oracle], 'publisher': 'oracle-node-1', 'peer_id': 'ownermac',
                         'paused_peers': {'oracle-node-1': 'paused_by_decision'}}
        self.native_publisher()
        d.deploy(self.args)
        self.assertEqual(self.revision(self.peer_homes['pulse@oracle-node-1']), B)
        config = json.loads(self.config.read_bytes())
        self.assertEqual(config['peers'], [{**oracle, 'script': oracle['script'].replace(C, B)}])
        self.assertEqual(config['paused_peers'], {'oracle-node-1': 'paused_by_decision'})

    def test_native_core_node_does_not_require_stats_selectors_to_read_receipts(self):
        self.native_publisher()
        registry = self.home / '.local/state/pulse/compute-cost/sources.json'
        d.atomic(registry, json.dumps({**json.loads(registry.read_bytes()),
                                       'observation_core_entrypoint': '/core/active/harness.pyz'}).encode())
        checked = []
        with patch.object(d, 'verify_reader_features', side_effect=lambda runtime, registry: checked.append(runtime)):
            d.deploy(self.args)
        releases = self.home / '.local/share/pulse/stats/releases'
        self.assertNotIn(releases / A, checked)
        self.assertIn(self.home / '.local/share/pulse/releases' / ('PULSE-' + D), checked)

    def test_failed_native_publisher_deployment_restores_readers_without_touching_publisher(self):
        self.native_publisher()
        self.bootstrap_timeout = True
        with self.assertRaises(TimeoutError):
            d.deploy(self.args)
        self.assert_restored()
        self.assertEqual([self.revision(p) for p in self.peer_homes.values()], [C])

    def local_then_full(self, *, fail_bootstrap=False):
        d.deploy_refresh_local(SimpleNamespace(source=self.root,revision=B,expected=A),home=self.home)
        self.assertEqual(self.revision(self.home),A)
        self.assertEqual(json.loads(self.config.read_bytes())['source_revision'],B)
        self.assertNotIn('publisher_policy',json.loads(self.config.read_bytes()))
        self.publisher_before=self.config.read_bytes();self.publisher_job_before=self.job_path.read_bytes()
        self.args.revision=D;self.args.expected=B
        d.atomic(self.candidate/'Contents/Info.plist',plistlib.dumps({
            'PulseSourceRevision':D,'PulseBuildId':'next-full','CFBundleShortVersionString':'test'}))
        self.bootstrap_timeout=fail_bootstrap

    def test_local_publisher_then_full_deployment_keeps_two_original_rollback_identities(self):
        self.local_then_full()
        d.deploy(self.args)
        receipt=self.receipt()
        self.assertEqual(receipt['previous'],B);self.assertEqual(receipt['shared_previous'],A)
        self.assertEqual(self.revision(self.home),D)
        self.assertEqual(json.loads(self.config.read_bytes())['source_revision'],D)
        self.assertNotIn('publisher_policy',json.loads(self.config.read_bytes()))
        self.assertEqual(d.selected_publisher_runtime(self.home,D,self.loaded_job).parent.name,'publisher-releases')
        self.assertTrue((self.home/'.local/share/pulse/stats/publisher-releases'/B).exists())
        for home in self.peer_homes.values():self.assertEqual(self.revision(home),D)

    def test_failed_full_after_local_restores_each_actual_original_consumer(self):
        self.local_then_full(fail_bootstrap=True)
        with self.assertRaises(TimeoutError):d.deploy(self.args)
        self.assertEqual(self.revision(self.home),A)
        self.assertEqual(self.config.read_bytes(),self.publisher_before)
        self.assertEqual(self.job_path.read_bytes(),self.publisher_job_before)
        self.assertEqual(json.loads(self.config.read_bytes())['source_revision'],B)
        self.assertEqual(d.selected_publisher_runtime(self.home,B,self.loaded_job).parent.name,'publisher-releases')
        for home in self.peer_homes.values():self.assertEqual(self.revision(home),C)
        self.assertFalse(self.receipt().get('rollback_errors'))
        from Stats import publisher as actual_entry
        actual_entry.publish(self.home,self.config)

    def test_failed_bootstrap_same_original_target_retry_runs_business_without_discarding_audit(self):
        self.bootstrap_timeout=True
        with self.assertRaises(TimeoutError):d.deploy(self.args)
        self.assertEqual(len(list((self.state/'publisher-enrollments').iterdir())),1)
        d.deploy(self.args)
        self.assertEqual(len(list((self.state/'publisher-enrollments').iterdir())),2)
        from Stats import publisher as actual_entry
        actual_entry.publish(self.home,self.config)
        self.assertEqual(d.selected_publisher_runtime(self.home,B,self.loaded_job).name,B)
        self.assertTrue((self.state/'publisher-enrollments').is_dir())

    def test_critical_full_enrollment_failure_restores_every_original_consumer(self):
        with patch.object(d,'record_retention_adoption',side_effect=OSError('critical seed unavailable')):
            with self.assertRaises(OSError):d.deploy(self.args)
        self.assert_restored()
        self.assertEqual([self.revision(p) for p in self.peer_homes.values()],[C])
        self.assertFalse(self.receipt().get('activation_confirmed'))
        self.assertFalse((self.state/'publisher-enrollments').exists())

    def test_index_failure_does_not_roll_back_successfully_activated_app_and_peers(self):
        original_index=d.record_retention_adoption
        def final_index_fails(*args,**kwargs):
            if kwargs.get('enrollment'):return original_index(*args,**kwargs)
            raise OSError('index unavailable')
        with patch.object(d,'record_retention_adoption',side_effect=final_index_fails):
            with self.assertRaises(OSError): d.deploy(self.args)
        self.assertEqual(self.revision(self.home), B)
        self.assertEqual([self.revision(p) for p in self.peer_homes.values()], [B])
        self.assertEqual(self.receipt()['status'], 'activated_receipt_failed')
        self.assertTrue(self.receipt()['activation_confirmed'])
        self.assertFalse(self.receipt()['rollback_pending'])
        self.assertEqual(self.events.count('bootout'), 1)
        from Stats import publisher as actual_entry
        from contextlib import redirect_stdout
        import io
        output=io.StringIO()
        with redirect_stdout(output):actual_entry.publish(self.home,self.config)
        self.assertIn('1\n',output.getvalue())
        self.assertTrue((self.state/'publisher-enrollments').is_dir())
        self.assertFalse((self.state/'publisher-retirements').exists())


    def test_cross_version_full_installer_rejected_before_peer_or_app_change(self):
        with patch.object(d,'EXECUTING_INSTALLER_SHA256',new='0'*64):
            with self.assertRaises(ValueError):d.deploy(self.args)
        self.assertEqual(self.events,[])
        self.assertEqual(self.config.read_bytes(),self.old_config)
        self.assertFalse((self.home/'.local/share/pulse/stats/publisher-releases').exists())

    def test_peer_preflight_failure_never_stops_publisher(self):
        self.fail_preflight = 'tcloud-node2'
        with self.assertRaises(RuntimeError):
            d.deploy(self.args)
        self.assertNotIn('bootout', self.events)
        self.assert_restored()
        self.assertEqual([self.revision(p) for p in self.peer_homes.values()], [C])

    def test_partial_switch_and_lost_reply_restore_original_peer_versions(self):
        self.fail_select = 'tcloud-node2'
        with self.assertRaises(TimeoutError):
            d.deploy(self.args)
        self.assert_restored()
        self.assertEqual([self.revision(p) for p in self.peer_homes.values()], [C])
        self.assertEqual(self.receipt()['rollback_errors'], [])
        self.fail_select = None
        d.deploy(self.args)
        self.assertEqual(self.revision(self.home), B)

    def test_rollback_failure_is_retained_and_blocks_retry_before_bootout(self):
        self.fail_select = 'tcloud-node2'
        self.fail_restore = 'tcloud-node2'
        with self.assertRaises(TimeoutError):
            d.deploy(self.args)
        self.assertTrue(self.receipt()['rollback_pending'])
        before = (self.state / 'deployment.json').read_bytes()
        self.events.clear()
        self.fail_select = None
        with self.assertRaises(ValueError):
            d.deploy(self.args)
        self.assertNotIn('bootout', self.events)
        self.assertEqual((self.state / 'deployment.json').read_bytes(), before)

    def test_concurrent_peer_change_is_preserved(self):
        self.drift = 'tcloud-node2'
        with self.assertRaises(ValueError):
            d.deploy(self.args)
        self.assert_restored()
        self.assertEqual(self.revision(self.peer_homes['tcloud-node2']), D)
        self.assertTrue(self.receipt()['rollback_pending'])

    def test_preflight_rechecks_all_peers_before_bootout(self):
        self.preflight_drift = 'tcloud-node2'
        with self.assertRaises(ValueError):
            d.deploy(self.args)
        self.assertNotIn('bootout', self.events)
        self.assert_restored()
        self.assertEqual(self.revision(self.peer_homes['tcloud-node2']), D)

    def test_local_drift_and_loaded_arguments_are_checked_before_bootout(self):
        for drift in ('reader', 'publisher'):
            with self.subTest(drift=drift):
                if drift == 'reader':
                    self.set_current(self.home, D)
                else:
                    self.set_current(self.home, A)
                    self.loaded_job['ProgramArguments'][1] = '/foreign/publisher.py'
                with self.assertRaises(ValueError):
                    d.deploy(self.args)
                self.assertNotIn('bootout', self.events)

    def test_target_or_rollback_reader_failure_precedes_bootout(self):
        for revision in (B, C):
            def incompatible(runtime, registry):
                if runtime.name == revision:
                    raise ValueError('retained format unsupported')
            with self.subTest(revision=revision), patch.object(d, 'verify_reader_features', side_effect=incompatible):
                with self.assertRaises(ValueError):
                    d.deploy(self.args)
                self.assertNotIn('bootout', self.events)
                self.assert_restored()

    def test_missing_rollback_publisher_precedes_bootout(self):
        home = self.peer_homes['tcloud-node2']
        (home / '.local/share/pulse/stats/releases' / C / 'Scripts/pulse-stats-cost-refresh.py').unlink()
        with self.assertRaises(ValueError):
            d.deploy(self.args)
        self.assertNotIn('bootout', self.events)

    def test_legacy_failure_uses_retained_peer_paths_and_preserves_receipt(self):
        legacy = {'revision': D, 'previous': A, 'status': 'deployment_failed',
                  'rollback_errors': ['vultr:RuntimeError'], 'source_plan_attempt': 'old-run'}
        raw = json.dumps(legacy).encode()
        d.atomic(self.state / 'deployment.json', raw)
        d.atomic(self.state / ('refresh-before-' + D + '.json'), self.old_config)
        d.deploy(self.args)
        self.assertEqual(self.receipt()['peer_previous'], dict.fromkeys(self.peer_homes, C))
        self.assertIn(raw, [p.read_bytes() for p in self.state.glob('deployment-history-*.json')])

    def test_legacy_failure_without_peer_evidence_is_preserved(self):
        legacy = json.dumps({'revision': D, 'previous': A, 'status': 'deployment_failed',
                             'rollback_errors': ['vultr:RuntimeError']}).encode()
        d.atomic(self.state / 'deployment.json', legacy)
        with self.assertRaises(FileNotFoundError):
            d.deploy(self.args)
        self.assertNotIn('bootout', self.events)
        self.assertEqual((self.state / 'deployment.json').read_bytes(), legacy)

    def test_peer_ownership_prevents_rollback_of_concurrent_same_target(self):
        home = self.peer_homes['tcloud-node2']
        d.stage_runtime(payload(), home, B)
        d.switch_peer_runtime(home, B, C, attempt='other')
        with self.assertRaises(ValueError):
            d.switch_peer_runtime(home, B, C, attempt='ours')
        with self.assertRaises(ValueError):
            d.switch_peer_runtime(home, B, C, attempt='ours', restore=True)
        self.assertEqual(self.revision(home), B)

    def test_restored_attempt_cannot_undo_a_later_same_target_switch(self):
        home = self.peer_homes['tcloud-node2']
        d.stage_runtime(payload(), home, B)
        d.switch_peer_runtime(home, B, C, attempt='ours')
        d.switch_peer_runtime(home, B, C, attempt='ours', restore=True)
        d.switch_peer_runtime(home, B, C, attempt='other')
        with self.assertRaises(ValueError):
            d.switch_peer_runtime(home, B, C, attempt='ours', restore=True)
        self.assertEqual(self.revision(home), B)

    def test_same_revision_aba_rejects_stale_rollback_and_retry(self):
        home = self.peer_homes['tcloud-node2']
        d.stage_runtime(payload(), home, B)
        d.switch_peer_runtime(home, B, C, attempt='ours')
        d.switch_peer_runtime(home, C, B, attempt='other-back')
        d.switch_peer_runtime(home, B, C, attempt='other-forward')
        for restore in (True, False):
            with self.subTest(restore=restore), self.assertRaises(ValueError):
                d.switch_peer_runtime(home, B, C, attempt='ours', restore=restore)
        self.assertEqual(self.revision(home), B)

    def oracle_peer(self):
        """Run the TCloud fixture as the Oracle account, with an installed publisher configuration."""
        home = self.peer_homes['tcloud-node2']
        config = home / '.local/state/pulse/stats/refresh.json'
        d.atomic(config, json.dumps({'source_revision': C, 'peers': [
            {'id': 'tcloud', 'host': 'tcloud-node2', 'python': d.PEER_PYTHON, 'transport': 'shared_ledger',
             'script': f'/home/pulse/.local/share/pulse/stats/releases/{C}/Scripts/pulse-stats-cost-refresh.py'}]}).encode())
        p = patch.object(d, 'ORACLE_HOME', home)
        p.start()
        self.addCleanup(p.stop)
        return home, config, config.read_bytes()

    def assert_oracle_peer_restored(self, home, config, before):
        self.assertEqual((self.revision(home), config.read_bytes()), (C, before))
        switch = json.loads((home / '.local/state/pulse/stats' /
                             ('runtime-switch-' + B + '--run-2.json')).read_bytes())
        self.assertEqual(switch['status'], 'restored')
        self.assertFalse(self.receipt()['rollback_pending'])

    def test_deployment_rollback_restores_an_oracle_peer_left_between_config_and_selector(self):
        home, config, before = self.oracle_peer()
        real = d.select_runtime
        def selector(target_home, revision, expected, **kwargs):
            if target_home == home and revision == B:
                raise OSError('selector replace failed')
            return real(target_home, revision, expected, **kwargs)
        with patch.object(d, 'select_runtime', side_effect=selector), self.assertRaises(OSError):
            d.deploy(self.args)
        self.assert_restored()
        self.assert_oracle_peer_restored(home, config, before)

    def test_deployment_rollback_restores_an_oracle_peer_whose_reply_was_lost(self):
        home, config, before = self.oracle_peer()
        self.fail_select = 'tcloud-node2'
        with self.assertRaises(TimeoutError):
            d.deploy(self.args)
        self.assert_restored()
        self.assert_oracle_peer_restored(home, config, before)

    def test_bootout_uncertainty_restores_scheduler(self):
        self.bootout_timeout = True
        with self.assertRaises(TimeoutError):
            d.deploy(self.args)
        self.assert_restored()
        self.assertFalse(self.receipt()['rollback_pending'])

    def test_failed_bootstrap_restores_every_node_and_app(self):
        self.bootstrap_timeout = True
        with self.assertRaises(TimeoutError):
            d.deploy(self.args)
        self.assert_restored()
        self.assertEqual([self.revision(p) for p in self.peer_homes.values()], [C])
        self.assertFalse(self.receipt()['rollback_pending'])

    def test_active_uncertain_bootstrap_is_preserved(self):
        self.bootstrap_timeout = self.partial_bootstrap = True
        with self.assertRaises(TimeoutError):
            d.deploy(self.args)
        self.assertTrue(self.active)
        self.assertEqual(self.revision(self.home), B)
        self.assertTrue(self.receipt()['rollback_pending'])
        self.assertEqual(self.events.count('bootout'), 1)

    def test_source_plan_without_safe_rollback_never_stops_publisher(self):
        self.args.adopt_runtime_sources = self.immutable_source = True
        with patch.object(d, 'prepare_sources', side_effect=self.prepare), self.assertRaises(ValueError):
            d.deploy(self.args)
        self.assertNotIn('bootout', self.events)
        self.assert_restored()

    def test_partial_source_switch_restores_registries_before_old_readers(self):
        self.args.adopt_runtime_sources = True
        self.fail_source = 'tcloud-node2'
        homes = [self.home, *self.peer_homes.values()]
        before = {home: (home / '.local/state/pulse/compute-cost/sources.json').read_bytes() for home in homes}
        with patch.object(d, 'prepare_sources', side_effect=self.prepare), self.assertRaises(TimeoutError):
            d.deploy(self.args)
        self.assert_restored()
        self.assertFalse(self.receipt()['rollback_pending'])
        for home in homes:
            self.assertEqual((home / '.local/state/pulse/compute-cost/sources.json').read_bytes(), before[home])
        self.assertEqual([self.revision(p) for p in self.peer_homes.values()], [C])

    def test_deployment_fence_is_held_during_peer_rollback(self):
        real = d.switch_peer_runtime
        def fenced(*args, **kwargs):
            if kwargs.get('restore'):
                with self.assertRaises(TimeoutError):
                    with d.writer_lock(self.state / 'deployment.lock', timeout=0):
                        pass
            return real(*args, **kwargs)
        self.fail_select = 'tcloud-node2'
        with patch.object(d, 'switch_peer_runtime', side_effect=fenced):
            with self.assertRaises(TimeoutError):
                d.deploy(self.args)
        self.assertFalse(self.receipt()['rollback_pending'])

    def test_actual_peer_cli_preflight_is_read_only_and_select_is_locked(self):
        home = self.peer_homes['tcloud-node2']
        d.stage_runtime(payload(), home, B)
        before = set(home.rglob('*'))
        command = [sys.executable, '-B', str(d.__file__)]
        env = {**os.environ, 'HOME': str(home)}
        result = self.real_run(command + ['preflight', '--revision', B], env=env, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)['previous'], C)
        self.assertEqual(set(home.rglob('*')), before)
        select = command + ['select', '--revision', B, '--expected', C, '--source-plan-attempt', 'cli']
        with d.writer_lock(home / '.local/state/pulse/stats/deployment.lock', timeout=0):
            result = self.real_run(select, env=env, capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.revision(home), C)
        result = self.real_run(select, env=env, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.revision(home), B)

    def test_feature_preflight_imports_never_write_release_bytecode(self):
        home = self.peer_homes['tcloud-node2']
        d.stage_runtime(payload(), home, B)
        for revision in (B, C):
            package = home / '.local/share/pulse/stats/releases' / revision / 'ComputeCost/pulse_compute_cost'
            package.mkdir(parents=True)
            for name, body in {'__init__.py': '', 'native_request_purpose.py': '',
                               'mcp_request_purpose.py': "TRANSPORT_SCHEMA='codex_session_wi/v1';CC_TRANSPORT_SCHEMA='cc_session_wi/v1'",
                               'source_layout.py': "CODEX_CALENDAR_LAYOUT='codex_calendar_months.v1'",
                               'receipts.py': "SUPPORTED_RECEIPT_FEATURES={'fixture'}"}.items():
                (package / name).write_text(body)
        registry = {'sources': [{'kind': 'codex', 'request_purpose_directory': '/retained',
                                'session_wi_mcp_since': '2026-01-01T00:00:00Z',
                                'layout': 'codex_calendar_months.v1'},
                                {'kind': 'cc', 'session_wi_mcp_since': '2026-01-01T00:00:00Z'}],
                    'required_receipt_features': ['fixture']}
        d.atomic(home / '.local/state/pulse/compute-cost/sources.json', json.dumps(registry).encode())
        before = set(home.rglob('*'))
        result = self.real_run([sys.executable, '-B', str(d.__file__), 'preflight', '--revision', B],
                               env={**os.environ, 'HOME': str(home)}, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(set(home.rglob('*')), before)


if __name__ == '__main__':
    unittest.main()
