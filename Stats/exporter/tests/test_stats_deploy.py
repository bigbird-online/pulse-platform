import subprocess
import sys
"""Pure deployment contracts; no installed App, scheduler or remote writes."""
import importlib.util
import io
import json
from pathlib import Path
import plistlib
import tarfile
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

spec=importlib.util.spec_from_file_location('stats_deploy',Path(__file__).parents[2]/'deploy.py')
d=importlib.util.module_from_spec(spec);spec.loader.exec_module(d)
A='a'*40;B='b'*40


def payload(name='Scripts/pulse-stats-cost-refresh.py',body=b'print(1)\n',kind=None,*,surfaces=None):
    data=io.BytesIO()
    with tarfile.open(fileobj=data,mode='w') as t:
        m=tarfile.TarInfo(name);m.size=len(body);m.mode=0o644
        if kind:m.type=kind;m.linkname='/etc/passwd';m.size=0
        t.addfile(m,io.BytesIO(body))
        if name == 'Scripts/pulse-stats-cost-refresh.py' and kind is None:
            for target in ('Stats/publisher.py','Stats/deploy.py','Stats/publisher_retention.py'):
                body=(Path(__file__).parents[2]/Path(target).name).read_bytes()
                m=tarfile.TarInfo(target);m.size=len(body);m.mode=0o600
                t.addfile(m,io.BytesIO(body))
        if surfaces is not None:
            body=json.dumps({'surfaces':surfaces}).encode()
            m=tarfile.TarInfo('config/scl-surfaces.yaml');m.size=len(body);m.mode=0o600
            t.addfile(m,io.BytesIO(body))
    return data.getvalue()


class DeploymentTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        # macOS /var is an alias; immutable release checks require the real home.
        self.home=Path(self.tmp.name).resolve()

    def run_peer_receiver(self, data):
        import os
        import sys
        scratch=self.home/'bootstrap-tmp'
        scratch.mkdir(exist_ok=True)
        # Redirect only the destination home; execute the exact production
        # receiver in a fresh interpreter, including its real installer file.
        program=('import pathlib,sys;from unittest.mock import patch\n'
                 'with patch("pathlib.Path.home",return_value=pathlib.Path(sys.argv[2])):\n'
                 '    exec('+repr(d.peer_stage_receiver())+')\n')
        result=subprocess.run([sys.executable,'-I','-c',program,B,str(self.home.resolve())],
            input=data,capture_output=True,timeout=30,
            env={**os.environ,'TMPDIR':str(scratch.resolve())})
        self.assertEqual(list(scratch.iterdir()),[], 'temporary installer was retained')
        return result

    def test_real_archived_peer_installer_stages_without_selecting(self):
        result=self.run_peer_receiver(payload())
        self.assertEqual(result.returncode,0,result.stderr.decode())
        target=self.home.resolve()/'.local/share/pulse/stats/releases'/B
        self.assertEqual(result.stdout.decode().strip(),str(target))
        installer=Path(d.__file__).read_bytes()
        self.assertEqual((target/'Stats/deploy.py').read_bytes(),installer)
        manifest=json.loads((target/'.stats-files.json').read_bytes())
        self.assertEqual(manifest['Stats/deploy.py'],d.hashlib.sha256(installer).hexdigest())
        self.assertEqual((target/'.stats-source-revision').read_text().strip(),B)
        self.assertFalse((target.parent.parent/'current').exists())

    def test_first_oracle_selector_creates_state_and_requires_registered_release(self):
        state = self.home / '.local/state/pulse'
        registry = state / 'compute-cost/sources.json'
        registry.parent.mkdir(parents=True)
        original = b'{"observation_core_entrypoint":"/native/reader"}'
        registry.write_bytes(original)
        registry.chmod(0o600)
        d.stage_runtime(payload(surfaces=[]), self.home, A)
        target = d.stage_runtime(payload(surfaces=[{'deployment':{'site':'oracle-node-1'}}]), self.home, B)
        current = target.parent.parent / 'current'
        with patch.object(d, 'ORACLE_HOME', self.home), patch.object(d, 'canonical_python'), \
                patch.object(d.os, 'uname', return_value=SimpleNamespace(nodename='oracle-node-1')):
            with self.assertRaisesRegex(ValueError, 'Oracle surface registration'):
                d.select_first_oracle_runtime(self.home, A)
            self.assertFalse(current.is_symlink())
            self.assertEqual(d.select_first_oracle_runtime(self.home, B)['revision'], B)
            self.assertEqual(d.select_first_oracle_runtime(self.home, B)['revision'], B)
        self.assertEqual(current.resolve(), target)
        self.assertEqual((state / 'stats').stat().st_mode & 0o777, 0o700)
        self.assertEqual(registry.read_bytes(), original)

    def test_oracle_enrollment_recovers_committed_peer_without_collecting_twice(self):
        state = self.home / '.local/state/pulse/stats'
        state.mkdir(parents=True)
        config = state / 'refresh.json'
        original = b'{"source_revision":"' + A.encode() + b'","peers":[],"retained":"original"}'
        d.atomic(config, original)
        registry = state.parent / 'compute-cost/sources.json'
        registry.parent.mkdir()
        d.atomic(registry, b'{}')
        plist = self.home / 'Library/LaunchAgents' / f'{d.LABEL}.plist'
        plist.parent.mkdir(parents=True)
        plist.write_bytes(plistlib.dumps({'ProgramArguments':['original-publisher']}))
        args = SimpleNamespace(source=self.home, revision=B, expected=A)
        collection = {'watermark':13,'inserted':2}
        api = {'shared_ledger_identity':lambda _: {'identity':'original'},
               'validate_shared_collection':lambda data, ledger: collection}
        receipt_path = state / 'peer-enrollment' / (B + '.oracle.json')
        retention_once = d.retention_once

        def interrupt_final_receipt(path, data):
            if path == receipt_path:
                raise RuntimeError('injected after publisher configuration')
            return retention_once(path, data)

        with patch.object(d, 'PUBLISHER_HOME', self.home), patch.object(Path, 'home', return_value=self.home), \
                patch.object(d, 'canonical_python', return_value=Path('/canonical/python')), \
                patch.object(d, 'require_publisher_arguments'), patch.object(d, 'native_publisher', return_value=self.home), \
                patch('runpy.run_path', return_value=api), patch.object(d, 'archive', return_value=payload()), \
                patch.object(d, 'run', side_effect=[b'[42]',b'staged',json.dumps({'revision':B}).encode(),b'collected']):
            with patch.object(d, 'retention_once', side_effect=interrupt_final_receipt):
                with self.assertRaisesRegex(RuntimeError, 'injected after'):
                    d.enroll_oracle_peer(args)
        selected = config.read_bytes()
        self.assertEqual(len(json.loads(selected)['peers']), 1)
        self.assertFalse(receipt_path.exists())
        backup = state / 'peer-enrollment' / ('before-' + d.hashlib.sha256(original).hexdigest() + '.json')
        self.assertEqual(backup.read_bytes(), original)
        with patch.object(d, 'PUBLISHER_HOME', self.home), patch.object(Path, 'home', return_value=self.home), \
                patch.object(d, 'canonical_python', return_value=Path('/canonical/python')), \
                patch.object(d, 'run', return_value=b'[42]') as invoke:
            recovered = d.enroll_oracle_peer(args)
            self.assertEqual(recovered['status'], 'enrolled')
            self.assertEqual(recovered['collection'], collection)
            invoke.assert_called_once()
        self.assertEqual(config.read_bytes(), selected)
        self.assertEqual(json.loads(receipt_path.read_bytes()), recovered)

    def test_peer_receiver_preserves_archive_checks_and_cleans_failed_bootstrap(self):
        for name in ('../outside','Stats/deploy.py'):
            with self.subTest(member=name):
                data=io.BytesIO(payload())
                with tarfile.open(fileobj=data,mode='a') as archive:
                    body=Path(d.__file__).read_bytes() if name=='Stats/deploy.py' else b'unsafe'
                    member=tarfile.TarInfo(name);member.size=len(body)
                    archive.addfile(member,io.BytesIO(body))
                result=self.run_peer_receiver(data.getvalue())
                self.assertNotEqual(result.returncode,0)
                self.assertIn(b'ValueError',result.stderr)
                self.assertFalse((self.home/'.local/share/pulse/stats/releases'/B).exists())
                self.assertFalse((self.home/'.local/share/pulse/stats/current').exists())
                self.assertFalse((self.home/'outside').exists())

    def test_canonical_python_requires_the_exact_environment_and_isolation(self):
        from unittest.mock import patch
        python=self.home/'.local/share/coevolution/python-env/bin/python'
        expected={'executable':str(python),'prefix':str(python.parent.parent)}
        with patch.object(d,'run',return_value=json.dumps(expected).encode()) as execute:
            self.assertEqual(d.canonical_python(self.home),python)
            self.assertEqual(execute.call_args.args[0][:3],[str(python),'-I','-c'])
        for field in expected:
            wrong={**expected,field:'/foreign/python'}
            with patch.object(d,'run',return_value=json.dumps(wrong).encode()):
                with self.assertRaises(ValueError):d.canonical_python(self.home)

    def test_weekly_gate_accepts_confirmed_cron_but_not_unknown_or_failed_sources(self):
        from copy import deepcopy
        from Stats.exporter.tests.verify_installed_dashboard import verify_weekly_activity
        source={'files_invalid':0,'status':'unknown'}
        usage={'event_count':1,'time_unallocated_event_count':0,'conflicted_event_count':0,'source_details':[source]}
        view={'period':'week','dimension':'activity','summary':{'usage':usage,
            'cost':{'unpriced':[],'reasons':['wi_execution_inventory_unknown']}},
            'groups':[{'key':{'agent':'duke','wi':'cron','entrypoint':'automation','node':'ownermac','runtime':'codex','model':'test'},
                       'usage':{'event_count':1}}]}
        self.assertEqual(verify_weekly_activity(view)['missing'],dict.fromkeys(('agent','wi','entrypoint','node','runtime','model'),0))
        for column in ('agent','wi','entrypoint','node','runtime','model'):
            broken=deepcopy(view);broken['groups'][0]['key'][column]='unassigned'
            with self.assertRaises(AssertionError):verify_weekly_activity(broken)
        for changes in ('wrong_period','source_error','unpriced','time','conflict','missing_row'):
            broken=deepcopy(view)
            if changes=='wrong_period':broken['period']='today'
            elif changes=='source_error':broken['summary']['usage']['source_details'][0]['status']='unreachable'
            elif changes=='unpriced':broken['summary']['cost']['unpriced']=[{'reason':'missing'}]
            elif changes=='time':broken['summary']['usage']['time_unallocated_event_count']=1
            elif changes=='conflict':broken['summary']['usage']['conflicted_event_count']=1
            else:broken['groups']=[]
            with self.assertRaises(AssertionError):verify_weekly_activity(broken)

    def test_user_visible_publisher_uses_standard_not_background_priority(self):
        old = {'ProcessType':'Background', 'Label':d.LABEL, 'RunAtLoad':True, 'StartInterval':900,
               'ProgramArguments':['python','publisher.py'], 'EnvironmentVariables':{'PYTHONDONTWRITEBYTECODE':'1'}}
        changed = d.publisher_launchagent(old)
        self.assertEqual(changed['ProcessType'], 'Standard')
        self.assertEqual(changed['StartInterval'], 1800)
        self.assertEqual(old['StartInterval'], 900)
        self.assertEqual(old['ProcessType'], 'Background')
        self.assertEqual({k:v for k,v in changed.items() if k not in {'ProcessType','StartInterval'}},
                         {k:v for k,v in old.items() if k not in {'ProcessType','StartInterval'}})
        self.assertEqual(d.publisher_launchagent(changed), changed)
        self.assertNotIn('Nice',changed)

    def test_reuses_original_source_and_procurement_settings(self):
        peer={'id':'tcloud','host':'tcloud-node2','prefix':['runuser','-u','pulse','--'],'python':'/usr/bin/python3',
              'script':f'/home/pulse/.local/share/pulse/stats/releases/{A}/Scripts/pulse-stats-cost-refresh.py'}
        original={'source_revision':A,'peers':[peer],'paused_peers':{},'openrouter_env':'/private/owner.env','extra':'preserved'}
        before=json.dumps(original)
        changed=d.refresh_config(original,B,['/runtime/node','/runtime/codex.js'],'/owner/.codex',d.cc_quota_source('/owner'))
        self.assertEqual(json.dumps(original),before)
        self.assertEqual(changed['openrouter_env'],original['openrouter_env'])
        self.assertEqual(changed['extra'],'preserved')
        self.assertIn(B,changed['peers'][0]['script'])
        self.assertEqual(changed['peers'][0]['python'], d.PEER_PYTHON)
        self.assertNotIn('destination', changed)
        self.assertEqual(changed['peers'][0]['prefix'], peer['prefix'])
        self.assertEqual(changed['peers'][0]['host'], peer['host'])
        self.assertEqual(changed['codex_quota']['home'],'/owner/.codex')
        self.assertEqual(changed['cc_quota'],{'path':'/owner/.local/share/pulse/data/relay/quota/cc-quota-v1.json'})
        with self.assertRaises(ValueError):
            d.refresh_config(original,B,['/codex'],'/owner/.codex','relative/cc-quota-v1.json')

    def test_deployment_retires_the_vultr_peer_and_destination_only(self):
        script=f'/home/pulse/.local/share/pulse/stats/releases/{A}/Scripts/pulse-stats-cost-refresh.py'
        vultr={'id':'vultr','host':'vultr-pulse','prefix':[],'python':d.PEER_PYTHON,'script':script}
        tcloud={'id':'tcloud','host':'tcloud-node2','prefix':['runuser','-u','pulse','--'],'python':d.PEER_PYTHON,'script':script}
        original={'source_revision':A,'peers':[vultr,tcloud],'destination':dict(vultr),'openrouter_env':'/private/owner.env',
                  'paused_peers':{}}
        changed=d.peer_scripts(original,B)
        self.assertEqual(changed['peers'],[{**tcloud,'script':script.replace(A,B)}])
        self.assertNotIn('destination',changed)
        self.assertEqual(changed['openrouter_env'],'/private/owner.env')
        self.assertEqual(d.configured_peers(changed),changed['peers'])
        self.assertEqual(original['peers'],[vultr,tcloud])
        # An already migrated configuration is unchanged apart from the release.
        self.assertEqual(d.peer_scripts(changed,B),changed)

    def test_unknown_execution_path_refused(self):
        peer={'id':'tcloud','host':'tcloud-node2','script':'/tmp/execute.py'}
        with self.assertRaises(ValueError):
            d.refresh_config({'peers':[peer]},B,['/codex'],'/home','/home/cc-quota-v1.json')
        other={'id':'other','host':'other-host','script':f'/home/pulse/.local/share/pulse/stats/releases/{A}/Scripts/pulse-stats-cost-refresh.py'}
        with self.assertRaisesRegex(ValueError,'unapproved peer'):
            d.refresh_config({'peers':[other]},B,['/codex'],'/home','/home/cc-quota-v1.json')

    def test_oracle_peer_preserves_direct_account_and_shared_ledger(self):
        script = f'/home/pulse/.local/share/pulse/stats/releases/{A}/Scripts/pulse-stats-cost-refresh.py'
        peer = {'id': 'oracle-node-1', 'host': 'pulse@oracle-node-1', 'python': d.PEER_PYTHON,
                'script': script, 'transport': 'shared_ledger'}
        self.assertEqual([{**peer, 'script': script.replace(A, B)}], d.peer_scripts({'peers': [peer]}, B)['peers'])
        for override in ({'id': 'other'}, {'transport': 'receipts'}, {'prefix': ['runuser', '-u', 'pulse', '--']}):
            with self.subTest(override=override), self.assertRaisesRegex(ValueError, 'original shared ledger'):
                d.peer_scripts({'peers': [{**peer, **override}]}, B)

    def test_peer_toolchain_migration_preserves_account_and_rejects_unknown_python(self):
        peer={'id':'tcloud','host':'tcloud-node2','prefix':['runuser','-u','pulse','--'],
              'python':'/home/pulse/.local/state/pulse/compute-cost/venv/bin/python',
              'script':f'/home/pulse/.local/share/pulse/stats/releases/{A}/Scripts/pulse-stats-cost-refresh.py',
              'extra':{'retained':True}}
        original={'peers':[peer],'destination':dict(peer),'paused_peers':{}}
        changed=d.refresh_config(original,B,['/codex'],'/home','/home/cc-quota-v1.json')
        expected={**peer,'python':d.PEER_PYTHON,'script':peer['script'].replace(A,B)}
        self.assertEqual(changed['peers'],[expected])
        self.assertEqual(changed['destination'],expected)
        self.assertEqual(original['peers'][0],peer)
        peer['python']='/unregistered/bin/python'
        with self.assertRaises(ValueError):
            d.refresh_config(original,B,['/codex'],'/home','/home/cc-quota-v1.json')

    def test_archive_is_immutable_and_retry_is_idempotent(self):
        path=d.stage_runtime(payload(),self.home,A)
        self.assertEqual(d.stage_runtime(payload(),self.home,A),path)
        with self.assertRaises(ValueError):d.stage_runtime(payload(body=b'changed'),self.home,A)
        self.assertEqual((path/'Scripts/pulse-stats-cost-refresh.py').read_bytes(),b'print(1)\n')

    def test_unsafe_archive_paths_and_links_are_rejected(self):
        for data in (payload('../outside'),payload('/absolute'),payload(kind=tarfile.SYMTYPE)):
            with self.assertRaises(ValueError):d.stage_runtime(data,self.home,A)

    def test_select_requires_staged_and_expected_runtime(self):
        d.stage_runtime(payload(),self.home,A);d.stage_runtime(payload(),self.home,B)
        current=self.home/'.local/share/pulse/stats/current'
        current.symlink_to(current.parent/'releases'/A)
        with self.assertRaises(ValueError):d.select_runtime(self.home,B,'c'*40)
        self.assertEqual(d.select_runtime(self.home,B,A),A)
        self.assertEqual(d.select_runtime(self.home,B,A),B)
        with self.assertRaises(FileNotFoundError):d.select_runtime(self.home,'c'*40,B)

    def test_app_must_be_built_from_requested_source(self):
        app=self.home/'candidate.app';(app/'Contents/MacOS').mkdir(parents=True)
        (app/'Contents/MacOS/PulseStats').write_bytes(b'fake-test-binary')
        (app/'Contents/Info.plist').write_bytes(plistlib.dumps({'PulseSourceRevision':A,'PulseBuildId':'test','CFBundleShortVersionString':'0.2.1'}))
        self.assertEqual(d.validate_app(app,A)['source_revision'],A)
        with self.assertRaises(ValueError):d.validate_app(app,B)

    def test_atomic_target_symlink_is_refused(self):
        target=self.home/'real';target.write_text('before')
        alias=self.home/'alias';alias.symlink_to(target)
        with self.assertRaises(ValueError):d.atomic(alias,b'after')
        self.assertEqual(target.read_text(),'before')

    def test_actor_activation_precedes_publisher_first_start(self):
        # Execute the workflow's actual activation shell with stand-in tools.
        import textwrap
        workflow = (Path(d.__file__).parents[1]/'.github/workflows/stats-macos-ci.yml').read_text()
        section = workflow.split('- name: Enable existing actor attribution',1)[1].split('- name: Verify natural',1)[0]
        shell = textwrap.dedent(section.split('run: |',1)[1])
        self.assertIn('SOURCE_ARGS+=(--defer-refresh)',workflow)
        events = self.home/'events'
        script = self.home/'activate'; script.write_text('#!/bin/sh\necho activation >> "$EVENTS"\nexit "$ACTIVATION_EXIT"\n'); script.chmod(0o700)
        start = self.home/'bootstrap'; start.write_text('#!/bin/sh\necho bootstrap >> "$EVENTS"\n');start.chmod(0o700)
        shell = shell.replace('"$HOME/.local/share/coevolution/python-env/bin/python" "$RELEASE/Stats/activate_actor_sources.py"',str(script))
        shell = shell.replace('/bin/launchctl',str(start))
        for status in (0,2):
            events.write_text('')
            result = subprocess.run(['bash','-c',shell],capture_output=True,text=True,
                env={'PATH':'/usr/bin:/bin','HOME':str(self.home),'WORK':str(self.home),
                     'APPROVED_REVISION':A,'EVENTS':str(events),'ACTIVATION_EXIT':str(status)})
            self.assertEqual(result.returncode,status,result.stderr)
            self.assertEqual(events.read_text().splitlines(),['activation','bootstrap'])

    def wait_for_publisher(self, release_after, *, declaration=True):
        """Hold the publisher lock until a simulated clock passes release_after."""
        import contextlib
        from unittest.mock import patch
        path=self.home/'cost-refresh.lock'
        if declaration:
            (self.home/'cost-refresh-cycle.json').write_text(json.dumps({'started_at':'2026-10-31T01:00:00+00:00'}))
        clock=[0.0];real=d.fcntl.flock;stderr=io.StringIO()
        def flock(fd,op):
            if op & d.fcntl.LOCK_EX and clock[0] < release_after:raise BlockingIOError
            return real(fd,op)
        def sleep(seconds):clock[0]+=seconds
        error=None
        with patch.object(d.fcntl,'flock',flock),patch.object(d.time,'monotonic',lambda:clock[0]),\
                patch.object(d.time,'sleep',sleep),contextlib.redirect_stderr(stderr):
            try:
                with d.writer_lock(path):pass
            except TimeoutError as exc:error=exc
        events=[json.loads(line) for line in stderr.getvalue().splitlines()]
        return error,clock[0],events

    def test_install_waits_for_a_publisher_cycle_that_ends_at_minute_20(self):
        error,elapsed,events=self.wait_for_publisher(20*60)
        self.assertIsNone(error)
        self.assertGreaterEqual(elapsed,20*60)
        # Progress is reported about once a minute with the running cycle's start.
        self.assertGreaterEqual(len(events),19)
        self.assertEqual({e['lock'] for e in events},{'cost-refresh.lock'})
        self.assertEqual({e['cycle_started_at'] for e in events},{'2026-10-31T01:00:00+00:00'})
        self.assertEqual({e['limit_seconds'] for e in events},{40*60})

    def test_install_waits_for_the_longest_observed_cycle(self):
        error,_,_=self.wait_for_publisher(1476,declaration=False)
        self.assertIsNone(error)

    def test_install_times_out_only_after_the_new_limit(self):
        error,elapsed,events=self.wait_for_publisher(float('inf'),declaration=False)
        self.assertIsInstance(error,TimeoutError)
        self.assertIn('current publisher did not finish',str(error))
        self.assertGreaterEqual(elapsed,40*60)
        self.assertLess(elapsed,40*60+1)
        self.assertNotIn('cycle_started_at',events[0])

    def test_writer_lock_never_skips_inflight_publication(self):
        path=self.home/'refresh.lock'
        with d.writer_lock(path):
            with self.assertRaises(TimeoutError):
                with d.writer_lock(path,timeout=0):pass
        with d.writer_lock(path,timeout=0):pass

    def test_first_publication_refreshes_early_started_consumer_exactly_once(self):
        from Stats.exporter.tests.verify_installed_dashboard import first_publication_requires_refresh
        document={'views':[{'period':'today','dimension':'agent','watermark':2,'report_sha256':'new',
                            'summary':{'cost':{}},'groups':[]}]}
        self.assertTrue(first_publication_requires_refresh([],False,{},document))
        self.assertFalse(first_publication_requires_refresh([],True,{},document))
        self.assertFalse(first_publication_requires_refresh([{'generated_at':'accepted'}],False,{},document))

    def test_matching_consumer_is_not_restarted_to_manufacture_progress(self):
        from Stats.exporter.tests.verify_installed_dashboard import first_publication_requires_refresh
        document={'views':[{'period':'today','dimension':'agent','watermark':2,'report_sha256':'new',
                            'summary':{'cost':{}},'groups':[]}]}
        self.assertFalse(first_publication_requires_refresh([],False,{'cost_dashboard':document},document))

    def test_gzip_snapshot_carrier_matches_its_publication(self):
        from Stats.exporter.tests.verify_installed_dashboard import first_publication_requires_refresh
        from pulse_stats.cost_dashboard import transport_cost_dashboard
        document={'schema_version':1,'generated_at':'2026-11-14T08:52:17+00:00','expires_at':'2026-11-14T09:37:17+00:00',
                  'source_revision':A,'status':'available','sha256':'s',
                  'views':[{'period':'today','dimension':'activity','watermark':2,'report_sha256':'new',
                            'summary':{'cost':{}},'groups':[]}]}
        cache={'cost_dashboard':transport_cost_dashboard(document)}
        self.assertNotIn('views',cache['cost_dashboard'])
        self.assertFalse(first_publication_requires_refresh([],False,cache,document))
        changed={**document,'views':[{**document['views'][0],'report_sha256':'other'}]}
        self.assertTrue(first_publication_requires_refresh([],False,cache,changed))

    def test_generation_alone_is_not_a_completed_publication(self):
        from Stats.exporter.tests.verify_installed_dashboard import publication_committed
        document={'source_revision':A,'generated_at':'2026-09-15T01:32:41+00:00'}
        previous={'source_revision':A,'destination':'published','generated_at':'2026-09-15T01:14:15+00:00'}
        self.assertFalse(publication_committed(document,previous))
        self.assertFalse(publication_committed(document,{}))
        self.assertFalse(publication_committed(document,{**previous,'generated_at':'invalid'}))
        self.assertTrue(publication_committed(document,{**previous,'generated_at':'2026-09-15T01:33:51+00:00'}))
        # Without a destination the local publication is the completed one.
        self.assertTrue(publication_committed(document,{**previous,'destination':'not_configured',
                                                        'generated_at':'2026-09-15T01:33:51+00:00'}))

    def test_newer_failure_or_other_revision_does_not_acknowledge_publication(self):
        from Stats.exporter.tests.verify_installed_dashboard import publication_committed
        document={'source_revision':A,'generated_at':'2026-09-15T01:32:41+00:00'}
        receipt={'source_revision':A,'destination':'published','generated_at':'2026-09-15T01:33:51+00:00'}
        self.assertFalse(publication_committed(document,{**receipt,'source_revision':B}))
        self.assertFalse(publication_committed(document,{**receipt,'destination':'failed'}))


class IdlePublisherTests(unittest.TestCase):
    RUNNING = b'gui/501/com.pulse.stats-cost-refresh = {\n\tstate = running\n\tpid = 50384\n}\n'
    IDLE = b'gui/501/com.pulse.stats-cost-refresh = {\n\tstate = not running\n}\n'

    def test_waits_for_a_run_that_just_released_the_lock_to_exit(self):
        seen = iter([self.RUNNING, self.RUNNING, self.IDLE])
        with patch.object(d, 'run', side_effect=lambda argv: next(seen)), patch.object(d.time, 'sleep') as sleep:
            d.require_idle_publisher('gui/501')
        self.assertEqual(sleep.call_count, 2)

    def test_refuses_a_publisher_still_running_after_the_grace(self):
        clock = iter([0, 10, 20, 31])
        with patch.object(d, 'run', return_value=self.RUNNING), patch.object(d.time, 'sleep'), \
                patch.object(d.time, 'monotonic', side_effect=lambda: next(clock)):
            with self.assertRaisesRegex(ValueError, 'publisher is active'):
                d.require_idle_publisher('gui/501')


class SourcePlanTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.home=Path(self.tmp.name)
        self.registry,self.before,self.after,self.plan=d._source_plan_paths(self.home,B)
        self.registry.parent.mkdir(parents=True,mode=0o700);self.plan.parent.mkdir(parents=True,mode=0o700)
        self.manifest=self.home/'manifest.json';self.manifest.write_bytes(b'{}')
        self.old=json.dumps({'ledger_root':str(self.home/'ledger'),'sources':[]}).encode()
        self.new=json.dumps({'ledger_root':str(self.home/'ledger'),'sources':[{'id':'native'}]}).encode()
        self.metadata={'revision':B,'before_sha256':d.hashlib.sha256(self.old).hexdigest(),
            'after_sha256':d.hashlib.sha256(self.new).hexdigest(),'manifest':str(self.manifest),
            'manifest_sha256':d.hashlib.sha256(b'{}').hexdigest()}
        for path,body in ((self.registry,self.old),(self.before,self.old),(self.after,self.new),(self.plan,json.dumps(self.metadata).encode())):
            d.atomic(path,body)

    def test_sources_activate_and_restore_without_ledger_mutation(self):
        self.assertTrue(d.select_sources(self.home,B)['changed'])
        self.assertFalse(d.select_sources(self.home,B)['changed'])
        self.assertEqual(self.registry.read_bytes(),self.new)
        self.assertTrue(d.select_sources(self.home,B,restore=True)['changed'])
        self.assertEqual(self.registry.read_bytes(),self.old)
        self.assertFalse((self.home/'ledger').exists())

    def test_concurrent_registry_change_is_preserved(self):
        d.atomic(self.registry,b'{"concurrent":true}')
        with self.assertRaises(ValueError):d.select_sources(self.home,B)
        self.assertEqual(self.registry.read_bytes(),b'{"concurrent":true}')

    def test_changed_manifest_or_plan_cannot_activate(self):
        self.manifest.write_bytes(b'{"changed":true}')
        with self.assertRaises(ValueError):d.select_sources(self.home,B)
        self.assertEqual(self.registry.read_bytes(),self.old)
        self.manifest.write_bytes(b'{}');d.atomic(self.after,b'{}')
        with self.assertRaises(ValueError):d.select_sources(self.home,B)
        self.assertEqual(self.registry.read_bytes(),self.old)

    def test_even_valid_plan_cannot_move_existing_ledger(self):
        changed=json.dumps({'ledger_root':str(self.home/'other'),'sources':[]}).encode()
        self.metadata['after_sha256']=d.hashlib.sha256(changed).hexdigest()
        d.atomic(self.after,changed);d.atomic(self.plan,json.dumps(self.metadata).encode())
        with self.assertRaises(ValueError):d.select_sources(self.home,B)
        self.assertEqual(self.registry.read_bytes(),self.old)


    def test_persistent_app_does_not_inherit_runner_cleanup_tag(self):
        from unittest.mock import patch
        from types import SimpleNamespace
        with patch.dict(d.os.environ, {'RUNNER_TRACKING_ID':'job-test', 'PATH':'original'}):
            with patch.object(d.subprocess, 'run', return_value=SimpleNamespace(returncode=0)) as invoke:
                d.start_installed_app(Path('/Applications/PulseStats.app'))
                self.assertNotIn('RUNNER_TRACKING_ID', invoke.call_args.kwargs['env'])
                self.assertEqual(invoke.call_args.kwargs['env']['PATH'],'original')
                self.assertEqual(d.os.environ['RUNNER_TRACKING_ID'],'job-test')
        with self.assertRaises(ValueError):
            d.start_installed_app(Path('/tmp/Unreviewed.app'))


class RetentionContractTests(unittest.TestCase):
    """Real private producer files; scheduler readback is the only stand-in."""
    def setUp(self):
        from unittest.mock import patch
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.state = self.home/'.local/state/pulse/stats'
        self.state.mkdir(parents=True, mode=0o700)
        for revision in (A, B): d.stage_runtime(payload(), self.home, revision)
        self.current = self.home/'.local/share/pulse/stats/current'
        self.current.symlink_to(self.current.parent/'releases'/B)
        self.config = self.state/'refresh.json'
        d.atomic(self.config, json.dumps({'source_revision': B, 'secret': 'test-private-value',
            'peers': [{'script': f'/home/pulse/.local/share/pulse/stats/releases/{A}/reader.py'}]}).encode())
        self.job = self.home/'Library/LaunchAgents'/f'{d.LABEL}.plist'
        self.job.parent.mkdir(parents=True)
        d.atomic(self.job, plistlib.dumps({'ProgramArguments': [
            str(self.home/'.local/share/coevolution/python-env/bin/python'),
            str(self.current.parent/'releases'/B/'Scripts/pulse-stats-cost-refresh.py'),
            'refresh', '--config', str(self.config)]}))
        self.receipt = self.state/'attempt/receipt.json'
        self.receipt.parent.mkdir(mode=0o700)
        self.document = {'scope': 'local_cost_refresh', 'revision': B, 'previous': A,
            'status': 'installed', 'completed_at': '2026-10-03T00:00:00+00:00'}
        d.atomic(self.receipt, json.dumps(self.document).encode())
        scheduler = patch.object(d, 'require_publisher_arguments')
        scheduler.start(); self.addCleanup(scheduler.stop)

    def snapshot(self):
        return {str(p.relative_to(self.home)): (p.read_bytes(), p.lstat().st_ino)
                for p in self.home.rglob('*') if p.is_file() and not p.is_symlink()}

    def test_actual_adoption_preserves_an_immutable_original_without_granting_expiry(self):
        path = d.record_retention_adoption(self.home, self.receipt)
        record = json.loads(path.read_bytes())
        archived = Path(record['original_receipt']['immutable_path'])
        self.assertEqual(archived.read_bytes(), self.receipt.read_bytes())
        self.assertEqual(d.hashlib.sha256(archived.read_bytes()).hexdigest(), path.stem)
        self.assertNotIn('test-private-value', path.read_text())
        self.assertEqual(record['acceptance'], 'unverified')
        self.assertEqual(record['payload_expiry'], 'not_authorized')
        self.assertEqual(record['audit_retention'], 'permanent')
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        inode = path.stat().st_ino
        self.assertEqual(d.record_retention_adoption(self.home, self.receipt), path)
        self.assertEqual(path.stat().st_ino, inode)
        # The producer's original mutable receipt may advance; this audit cannot.
        d.atomic(self.receipt, json.dumps({**self.document, 'status': 'later_state'}).encode())
        plan = d.plan_retention(self.home, B)
        self.assertEqual(plan['eligible_count'], 0)
        self.assertEqual(len(plan['adoption_records']), 1)
        self.assertEqual(json.loads(archived.read_bytes())['status'], 'installed')

    def test_failed_staged_or_incomplete_receipts_do_not_create_an_index(self):
        for changes in ({'status': 'failed'}, {'status': 'staged'}, {'completed_at': None},
                        {'previous': 'unknown'}, {'scope': 'full', 'status': 'installed'}):
            d.atomic(self.receipt, json.dumps({**self.document, **changes}).encode())
            with self.assertRaises(ValueError): d.record_retention_adoption(self.home, self.receipt)
            self.assertFalse((self.state/'retention-adoptions').exists())

    def test_unsafe_receipt_symlink_public_file_and_oversize_fail_without_index(self):
        original = self.receipt.read_bytes()
        alias = self.state/'alias.json'; alias.symlink_to(self.receipt)
        with self.assertRaises(ValueError): d.record_retention_adoption(self.home, alias)
        self.receipt.chmod(0o644)
        with self.assertRaises(ValueError): d.record_retention_adoption(self.home, self.receipt)
        d.atomic(self.receipt, b'x' * (2*1024*1024+1))
        with self.assertRaises(ValueError): d.record_retention_adoption(self.home, self.receipt)
        d.atomic(self.receipt, original)
        self.assertFalse((self.state/'retention-adoptions').exists())

    def test_index_symlink_and_changed_immutable_record_are_never_overwritten(self):
        outside = self.home/'outside'; outside.mkdir()
        index = self.state/'retention-adoptions'; index.symlink_to(outside)
        with self.assertRaises(ValueError): d.record_retention_adoption(self.home, self.receipt)
        self.assertEqual(list(outside.iterdir()), [])
        index.unlink()
        path = d.record_retention_adoption(self.home, self.receipt)
        d.atomic(path, b'{"foreign":true}')
        with self.assertRaises(ValueError): d.record_retention_adoption(self.home, self.receipt)
        self.assertEqual(path.read_bytes(), b'{"foreign":true}')

    def test_plan_preserves_unknown_legacy_and_does_not_backfill_or_write(self):
        before = self.snapshot()
        plan = d.plan_retention(self.home, B)
        self.assertEqual(self.snapshot(), before)
        self.assertFalse((self.state/'retention-adoptions').exists())
        self.assertFalse(plan['apply_supported'])
        self.assertFalse(plan['automatic_deletion_performed'])
        self.assertEqual(plan['live_process_fd_closure'], 'not_performed')
        legacy = 'c'*40; d.stage_runtime(payload(), self.home, legacy)
        item = next(x for x in d.plan_retention(self.home, B)['runtime_items'] if x['revision'] == legacy)
        self.assertEqual(item['reason'], 'legacy_authority_unknown')
        self.assertTrue(item['protected'])

    def test_full_app_rollback_and_native_hook_mcp_are_distinct_protection_roles(self):
        attempt = self.state/'refresh-local-current'; attempt.mkdir(mode=0o700)
        d.atomic(attempt/'receipt.json', json.dumps(self.document).encode())
        full, rollback, hook, mcp = ('c'*40, 'd'*40, 'e'*40, 'f'*40)
        d.atomic(self.state/'deployment.json', json.dumps({'revision': full, 'previous': rollback,
            'peer_previous': {'vultr-pulse': A}, 'status': 'activated_pending_business_readback'}).encode())
        hooks = self.home/'.codex/hooks.json'; hooks.parent.mkdir()
        d.atomic(hooks, json.dumps({'hook': f'{self.current.parent}/releases/{hook}/Stats/request_purpose.py'}).encode())
        d.atomic(self.home/'.codex/config.toml',
                 f'script = "{self.current.parent}/releases/{mcp}/Stats/native_mcp.py"'.encode())
        protected = d.plan_retention(self.home, B)['protected_revisions']
        for revision, role in ((B, 'current_selector'), (A, 'local_publisher_previous'), (full, 'full_deployment'),
                               (rollback, 'full_rollback'), (hook, 'native_hook'), (mcp, 'native_mcp')):
            self.assertIn(role, protected[revision])

    def test_current_config_drift_and_loaded_argv_drift_stop_the_plan(self):
        from unittest.mock import patch
        with self.assertRaises(ValueError): d.plan_retention(self.home, A)
        d.atomic(self.config, json.dumps({'source_revision': A}).encode())
        with self.assertRaises(ValueError): d.plan_retention(self.home, B)
        d.atomic(self.config, json.dumps({'source_revision': B}).encode())
        with patch.object(d, 'require_publisher_arguments', side_effect=ValueError('changed')):
            with self.assertRaises(ValueError): d.plan_retention(self.home, B)

    def test_reference_appearing_during_plan_invalidates_it(self):
        from unittest.mock import patch
        original=d.require_publisher_arguments;calls=0
        def late_reference(*args,**kwargs):
            nonlocal calls
            calls+=1
            if calls==2:d.atomic(self.state/'deployment.json',b'{}')
            return original(*args,**kwargs)
        with patch.object(d,'require_publisher_arguments',side_effect=late_reference):
            with self.assertRaises(ValueError):d.plan_retention(self.home,B)

    def test_changed_archived_receipt_and_unbound_record_fail_closed(self):
        path = d.record_retention_adoption(self.home, self.receipt)
        record = json.loads(path.read_bytes())
        archived = Path(record['original_receipt']['immutable_path'])
        d.atomic(archived, b'{"foreign":true}')
        with self.assertRaises(ValueError): d.plan_retention(self.home, B)

    def test_archive_parent_symlink_or_public_directory_is_rejected(self):
        d.record_retention_adoption(self.home, self.receipt)
        archive = self.state/'retention-receipts'
        moved = self.home/'moved-archive'; archive.rename(moved)
        archive.symlink_to(moved)
        with self.assertRaises(ValueError): d.plan_retention(self.home, B)
        archive.unlink(); moved.rename(archive); archive.chmod(0o755)
        with self.assertRaises(ValueError): d.plan_retention(self.home, B)

    def test_archive_directory_replacement_during_plan_invalidates_it(self):
        from unittest.mock import patch
        d.record_retention_adoption(self.home, self.receipt)
        archive = self.state/'retention-receipts'
        def replacement(*args):
            moved = self.home/'moved-archive'
            if not moved.exists():
                archive.rename(moved); archive.symlink_to(moved)
        with patch.object(d, 'require_publisher_arguments', side_effect=[None, None]) as loaded:
            def at_end(*args):
                if loaded.call_count == 2: replacement()
            loaded.side_effect = at_end
            with self.assertRaises(ValueError): d.plan_retention(self.home, B)

    def test_plan_cli_uses_account_home_and_other_modes_still_require_revision(self):
        import pwd
        from types import SimpleNamespace
        from unittest.mock import patch
        with patch.object(d.sys, 'argv', ['deploy.py', 'plan-retention', '--expected', B]), \
             patch.object(pwd, 'getpwuid', return_value=SimpleNamespace(pw_dir=str(self.home))), \
             patch.dict(d.os.environ, {'HOME': str(self.home/'foreign')}), \
             patch('builtins.print'):
            d.main()
        with patch.object(d.sys, 'argv', ['deploy.py', 'select', '--expected', A]), patch('sys.stderr', io.StringIO()):
            with self.assertRaises(SystemExit) as stopped: d.main()
            self.assertEqual(stopped.exception.code, 2)

    def test_persistent_app_launch_failure_is_not_reported_as_success(self):
        from unittest.mock import patch
        from types import SimpleNamespace
        with patch.object(d.subprocess, 'run', return_value=SimpleNamespace(returncode=1)):
            with self.assertRaises(RuntimeError):
                d.start_installed_app(Path('/Applications/PulseStats.app'))



class SourcePlanRetryTests(unittest.TestCase):
    """Same release, new run-attempt; real selectors on private synthetic roots."""
    def setUp(self):
        from unittest.mock import patch
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        self.home.chmod(0o700)
        self.registry = self.home / '.local/state/pulse/compute-cost/sources.json'
        self.registry.parent.mkdir(parents=True, mode=0o700)
        (self.home / '.local/state/pulse/stats').mkdir(mode=0o700)
        self.platform = self.home / 'PULSE/data'
        for folder in ('sessions', 'harness', 'host-receipts'):
            (self.platform / folder).mkdir(parents=True, mode=0o700)
        self.manifest = self.platform / 'manifest.json'
        self.manifest_body = {'profiles': {'resident': {
            'sessionRoot': str(self.platform/'sessions'),
            'computeCostHost': {'sclHarnessRoot': str(self.platform/'harness')}}}}
        d.atomic(self.manifest, json.dumps(self.manifest_body).encode())
        original = {'schema':'computecost_source_registry/v1',
            'ledger_root':str(self.home/'unchanged-ledger'), 'sources':[
                {'id':'host','kind':'relay_host','path':str(self.platform/'host-receipts')},
                {'id':'harness','kind':'harness_root','path':str(self.platform/'harness')}]}
        self.original = json.dumps(original).encode(); d.atomic(self.registry,self.original)
        d.sys.path.insert(0,str(Path(__file__).resolve().parents[3]/'ComputeCost/uat'))
        import effective_source_replay
        observed = {'HARNESS_ROOT':str(self.platform/'harness'),
            'RELAY_PI_RUNTIME_MANIFEST_PATH':str(self.manifest),
            'PULSE_COMPUTECOST_RECEIPT_DIR':str(self.platform/'host-receipts')}
        self.observe = patch.object(effective_source_replay,'runtime_environment',return_value=observed)
        self.observe.start(); self.addCleanup(self.observe.stop)

    def paths(self, attempt):
        return d._source_plan_paths(self.home,B,attempt)[1:]

    def prepare(self, attempt):
        return d.prepare_sources(self.home,B,'ownermac',attempt=attempt)

    def test_new_attempt_revalidates_changed_manifest_without_overwriting_old_plan(self):
        self.prepare('100-1')
        saved = {p:p.read_bytes() for p in self.paths('100-1')}
        # Relay upgrade changed unrelated metadata, not cost source selection.
        self.manifest_body['release']='new-relay-release'
        d.atomic(self.manifest,json.dumps(self.manifest_body).encode())
        with self.assertRaisesRegex(ValueError,'different evidence'):
            self.prepare('100-1')
        result=self.prepare('100-2')
        self.assertEqual(result['attempt'],'100-2')
        self.assertEqual(saved,{p:p.read_bytes() for p in saved})
        self.assertEqual(self.registry.read_bytes(),self.original)
        with self.assertRaisesRegex(ValueError,'manifest changed'):
            d.select_sources(self.home,B,attempt='100-1')
        self.assertTrue(d.select_sources(self.home,B,attempt='100-2')['changed'])
        self.assertFalse(d.select_sources(self.home,B,attempt='100-2')['changed'])
        self.assertTrue(d.select_sources(self.home,B,attempt='100-2',restore=True)['changed'])
        self.assertEqual(self.registry.read_bytes(),self.original)
        self.assertFalse((self.home/'unchanged-ledger').exists())

    def test_identical_attempt_reuses_exact_plan_bytes(self):
        first=self.prepare('100-1'); paths=self.paths('100-1')
        saved=[(p.read_bytes(),p.stat().st_mtime_ns) for p in paths]
        self.assertEqual(self.prepare('100-1'),first)
        self.assertEqual(saved,[(p.read_bytes(),p.stat().st_mtime_ns) for p in paths])

    def oracle_installation(self):
        home = self.home.resolve()
        core = home / '.local/share/pulse/tools/scl-core'
        program = core / 'releases/registered/harness.pyz'
        program.parent.mkdir(parents=True)
        d.atomic(program, b'native bundle')
        (core / 'active').symlink_to('releases/registered')
        entry = core / 'active/harness.pyz'
        self.manifest_body['workItemCoreHost'] = {'entrypoint': str(entry)}
        d.atomic(self.manifest, json.dumps(self.manifest_body).encode())
        original = json.loads(self.original)
        original['observation_core_entrypoint'] = str(entry)
        d.atomic(self.registry, json.dumps(original).encode())
        roots = {'codex': home/'.codex/sessions', 'cc': home/'.claude/projects',
                 'pi': home/'.pi/agent/sessions'}
        archive = home/'.codex/archived_sessions'
        for directory in (*roots.values(), archive):
            directory.mkdir(parents=True)
        policy = {'profiles': [
            {'role': 'producer', 'runtime': kind, 'session_root': str(path), **({'standalone': True} if kind == 'pi' else {})}
            for kind, path in roots.items()
        ]}
        policy_path = program.with_name('host-policy.json')
        d.atomic(policy_path, json.dumps(policy).encode())
        return home, entry, original, roots, archive, policy_path

    def prepare_oracle(self, home, attempt='oracle-run', workspace_registry=None):
        # The registered Oracle surface, with its checkout root under this test home.
        surfaces = d.load_surfaces(Path(d.__file__).resolve().parents[1])
        deployment, = [s['deployment'] for s in surfaces['surfaces'] if s.get('deployment', {}).get('site') == d.ORACLE_SITE]
        deployment['team_registry_root']['checkout_root'] = str(home/'Coevolution')
        with patch.object(d, 'ORACLE_HOME', home), patch.object(d, 'load_surfaces', return_value=surfaces), \
                patch.object(d.os, 'uname', return_value=SimpleNamespace(nodename='oracle-node-1')):
            d.prepare_sources(home, B, d.ORACLE_SITE, attempt=attempt, workspace_registry=workspace_registry)
        _, after, _ = self.paths(attempt)
        return json.loads(after.read_bytes())

    def test_oracle_source_plan_uses_observed_native_roots_without_moving_ledger(self):
        home, entry, original, roots, archive, policy_path = self.oracle_installation()
        candidate = self.prepare_oracle(home)
        self.assertEqual(original['ledger_root'], candidate['ledger_root'])
        self.assertEqual(str(entry), candidate['observation_core_entrypoint'])
        enrolled = [row for row in candidate['sources'] if row.get('node') == d.ORACLE_SITE]
        self.assertCountEqual([row['kind'] for row in enrolled], ['codex', 'cc', 'pi', 'pi', 'relay_host'])
        codex = next(row for row in enrolled if row['kind'] == 'codex')
        self.assertCountEqual(codex['paths'], [str(roots['codex']), str(archive)])
        self.assertEqual(json.loads(self.registry.read_bytes()), original)
        d.atomic(policy_path, b'{"profiles":[]}')
        with self.assertRaisesRegex(ValueError, 'native source bindings changed'):
            d.select_sources(home, B, attempt='oracle-run')

    # The shared team registry as committed: it names its author's repo_root.
    CANONICAL_TEAM_REGISTRY = (b"schema_version: '2.3.0'\nrepo_root: /Users/owner/Coevolution\n\n"
                               b"owners:\n  owner:\n    tg_chat_id: '1'\n\nagents:\n  aki:\n    tenant: owner\n    workspace: PARA/aki-space/\n")

    def oracle_actor_plan(self, team_bytes):
        import effective_source_replay
        home = self.oracle_installation()[0]
        repo = home/'Coevolution'
        (repo/'ssot').mkdir(parents=True); (repo/'PARA/aki-space').mkdir(parents=True)
        team = repo/'ssot/registry.yaml'
        team.write_bytes(team_bytes)
        relay = home/'relay.json'
        d.atomic(relay, json.dumps({'users': [{'agents': [{'id': 'pulse'}, {'id': 'aki'}, {'id': 'asobi'}]}]}).encode())
        inputs = home/'.local/state/agent-harness/scl-host/human-inputs'
        inputs.mkdir(parents=True, mode=0o700)
        observed = {**effective_source_replay.runtime_environment(), 'RELAY_CONFIG_PATH': str(relay)}
        with patch.object(effective_source_replay, 'runtime_environment', return_value=observed):
            candidate = self.prepare_oracle(home)
        return home, repo, team, inputs, candidate

    def test_oracle_host_and_relay_pi_get_the_tcloud_actor_wiring(self):
        import hashlib
        home, repo, team, inputs, candidate = self.oracle_actor_plan(self.CANONICAL_TEAM_REGISTRY)
        self.assertEqual(candidate['workspace_registry_path'], str(team))
        self.assertEqual(candidate['workspace_registry_root'],
                         {'repo_root': '/Users/owner/Coevolution', 'checkout_root': str(repo)})
        self.assertEqual(candidate['workspace_registry_sha256'], hashlib.sha256(team.read_bytes()).hexdigest())
        host, = [s for s in candidate['sources'] if s['kind'] == 'relay_host']
        pi, = [s for s in candidate['sources'] if s['kind'] == 'pi' and s.get('runtime_manifest')]
        for source in (host, pi):
            self.assertEqual((source['node'], source['actor_attribution'], source['host_native_input'],
                              source['human_input_directory']), (d.ORACLE_SITE, True, True, str(inputs)))
        self.assertEqual(pi['scl_surface_id'], 'surface_v1_cloud_linux_c')
        self.assertEqual({r['alias'] for r in host['principal_alias_history']}, {'pulse', 'aki', 'asobi'})
        self.assertIn((str(repo/'PARA/aki-space'), 'aki', d.ORACLE_SITE, 'pi'),
                      {(r['workspace_root'], r['agent'], r['node'], r['runtime']) for r in pi['workspace_actors']})
        # Selection adopts the same plan; the adopted registry reads its checkout.
        d.select_sources(home, B, attempt='oracle-run')
        from pulse_compute_cost.agent_tenants import load_agent_tenants
        from pulse_compute_cost.source_registration import workspace_actor_drift
        adopted = json.loads(self.registry.read_bytes())
        self.assertEqual(adopted, candidate)
        self.assertEqual(set(load_agent_tenants(adopted)), {'aki'})
        self.assertEqual(workspace_actor_drift(adopted), [])

    def test_explicit_registry_path_replaces_an_adopted_mapping(self):
        import effective_source_replay, hashlib
        home, repo, team, _, _ = self.oracle_actor_plan(self.CANONICAL_TEAM_REGISTRY)
        d.select_sources(home, B, attempt='oracle-run')
        # Another checkout whose file states its own location.
        other = home/'Other'
        (other/'ssot').mkdir(parents=True); (other/'PARA/aki-space').mkdir(parents=True)
        moved = other/'ssot/registry.yaml'
        moved.write_bytes(self.CANONICAL_TEAM_REGISTRY.replace(b'/Users/owner/Coevolution', str(other).encode()))
        observed = {**effective_source_replay.runtime_environment(), 'RELAY_CONFIG_PATH': str(home/'relay.json')}
        with patch.object(effective_source_replay, 'runtime_environment', return_value=observed):
            switched = self.prepare_oracle(home, 'oracle-run-2', moved)
            d.select_sources(home, B, attempt='oracle-run-2')
            self.assertEqual(switched['workspace_registry_path'], str(moved))
            self.assertNotIn('workspace_registry_root', switched)
            self.assertEqual(switched['workspace_registry_sha256'], hashlib.sha256(moved.read_bytes()).hexdigest())
            pi, = [s for s in switched['sources'] if s['kind'] == 'pi' and s.get('runtime_manifest')]
            self.assertIn(str(other/'PARA/aki-space'), {r['workspace_root'] for r in pi['workspace_actors']})
            # Naming the registered checkout again restores its mapping.
            back = self.prepare_oracle(home, 'oracle-run-3', team)
        self.assertEqual(back['workspace_registry_root'], {'repo_root': '/Users/owner/Coevolution', 'checkout_root': str(repo)})
        self.assertEqual(back['workspace_registry_sha256'], hashlib.sha256(team.read_bytes()).hexdigest())

    @unittest.skipUnless(Path('/Users/owner/Coevolution/ssot/registry.yaml').is_file(), 'shared team registry checkout')
    def test_oracle_plan_reads_the_committed_team_registry_bytes(self):
        raw = subprocess.run(['git', '-C', '/Users/owner/Coevolution', 'show', 'HEAD:ssot/registry.yaml'],
                             capture_output=True, check=True).stdout
        candidate = self.oracle_actor_plan(raw)[4]
        pi, = [s for s in candidate['sources'] if s['kind'] == 'pi' and s.get('runtime_manifest')]
        self.assertIn('aki', {r['agent'] for r in pi['workspace_actors']})

    def test_team_registry_of_another_root_is_refused(self):
        other = self.CANONICAL_TEAM_REGISTRY.replace(b'/Users/owner/Coevolution', b'/Users/other/Coevolution')
        with self.assertRaisesRegex(ValueError, 'root identity differs'):
            self.oracle_actor_plan(other)

    def test_registered_oracle_team_registry_is_the_publisher_one(self):
        root = d.team_registry_root(d.load_surfaces(Path(d.__file__).resolve().parents[1]), d.ORACLE_SITE)
        self.assertEqual(Path(root['checkout_root'])/'ssot/registry.yaml', d.ORACLE_WORKSPACE_REGISTRY)

    def test_new_attempt_does_not_erase_concurrent_registry_change(self):
        self.prepare('100-1')
        changed=json.loads(self.original); changed['external_setting']='preserved'
        d.atomic(self.registry,json.dumps(changed).encode())
        self.prepare('100-2')
        with self.assertRaisesRegex(ValueError,'changed concurrently'):
            d.select_sources(self.home,B,attempt='100-1')
        d.select_sources(self.home,B,attempt='100-2')
        self.assertEqual(json.loads(self.registry.read_bytes())['external_setting'],'preserved')

    def test_plan_from_another_attempt_is_not_accepted(self):
        self.prepare('100-1'); self.prepare('100-2')
        plan=self.paths('100-2')[-1]; value=json.loads(plan.read_bytes())
        value['attempt']='100-1'; d.atomic(plan,json.dumps(value).encode())
        with self.assertRaisesRegex(ValueError,'different deployment attempt'):
            d.select_sources(self.home,B,attempt='100-2')
        self.assertEqual(self.registry.read_bytes(),self.original)

    def test_legacy_plan_is_preserved_but_never_selected_for_new_attempt(self):
        self.prepare(None); legacy={p:p.read_bytes() for p in self.paths(None)}
        self.prepare('100-1'); d.select_sources(self.home,B,attempt='100-1')
        self.assertEqual(legacy,{p:p.read_bytes() for p in legacy})

    def test_unsafe_attempt_and_same_attempt_registry_change_are_rejected(self):
        for attempt in ('', '../escape', '/absolute', 'x'*81, 12):
            with self.subTest(attempt=attempt), self.assertRaises(ValueError):
                d._source_plan_paths(self.home,B,attempt)
        self.prepare('100-1'); d.atomic(self.registry,b'{"changed":true}')
        with self.assertRaises(ValueError):
            d.select_sources(self.home,B,attempt='100-1')


class CoreOutcomeEnrollmentTests(unittest.TestCase):
    def setUp(self):
        import hashlib
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.home=Path(self.tmp.name).resolve()
        release=self.home/'.local/share/pulse/stats/releases'/A
        (release/'ComputeCost/pulse_compute_cost').mkdir(parents=True)
        (release/'.stats-source-revision').write_text(A)
        (release.parent.parent/'current').symlink_to(release)
        self.reader=release/'ComputeCost/pulse_compute_cost/kernel_outcomes.py'
        self.reader.write_text('')
        self.state=self.home/'.local/state/pulse/stats';self.state.mkdir(parents=True,mode=0o700)
        self.registry=self.home/'.local/state/pulse/compute-cost/sources.json';self.registry.parent.mkdir(mode=0o700)
        self.original={'schema':'computecost_source_registry/v1','ledger_root':'/unchanged-ledger',
                       'sources':[{'id':'peer','kind':'receipts','path':'/existing/transport'}]}
        self.registry.write_text(json.dumps(self.original));self.registry.chmod(0o600)
        self.sha=lambda:hashlib.sha256(self.registry.read_bytes()).hexdigest()

    def args(self, expected=None):
        from types import SimpleNamespace
        return SimpleNamespace(revision=A,expected=A,expected_native_registry=expected or self.sha(),site='ownermac')

    def test_enrollment_adds_one_core_source_and_is_idempotent(self):
        first=d.enroll_core_outcomes(self.args(),home=self.home)
        sources=json.loads(self.registry.read_text())['sources']
        self.assertEqual(sources[0],self.original['sources'][0])
        self.assertEqual(sources[1],{'id':'harness-core-outcomes','kind':'harness_core',
            'path':str(self.state/'wi-facts-v1.json'),'label':'ownermac.harness_core'})
        self.assertTrue(first['changed']);self.assertFalse(first['ledger_rewritten'])
        again=d.enroll_core_outcomes(self.args(),home=self.home)
        self.assertFalse(again['changed'])

    def test_enrollment_rejects_a_changed_registry_moved_source_or_incapable_reader(self):
        with self.assertRaises(ValueError):
            d.enroll_core_outcomes(self.args('0'*64),home=self.home)
        moved={**self.original,'sources':[*self.original['sources'],
            {'id':'harness-core-outcomes','kind':'harness_core','path':'/elsewhere.json','label':'x'}]}
        with self.assertRaises(ValueError):
            d.core_outcome_source_registry(moved,self.state/'wi-facts-v1.json','ownermac')
        self.reader.unlink()
        with self.assertRaises(ValueError):
            d.enroll_core_outcomes(self.args(),home=self.home)
        self.assertEqual(json.loads(self.registry.read_text()),self.original)

    def test_holder_supplement_enrollment_sets_path_surface_and_feature_once(self):
        from types import SimpleNamespace
        (self.reader.parent/'holder_supplement.py').write_text('')
        (self.reader.parent/'__init__.py').write_text('')
        (self.reader.parent/'receipts.py').write_text("SUPPORTED_RECEIPT_FEATURES = frozenset({'holder_supplement.v1'})\n")
        args=lambda: SimpleNamespace(revision=A,expected=A,expected_native_registry=self.sha(),surface='surface_v1_local_macos')
        first=d.enroll_holder_supplement(args(),home=self.home)
        registry=json.loads(self.registry.read_text())
        self.assertEqual(registry['core_session_writes'],{'path':str(self.state/'wi-facts-v1.json'),'surface':'surface_v1_local_macos'})
        self.assertEqual(registry['required_receipt_features'],['holder_supplement.v1'])
        self.assertEqual(registry['sources'],self.original['sources'])
        self.assertTrue(first['changed']);self.assertFalse(d.enroll_holder_supplement(args(),home=self.home)['changed'])
        with self.assertRaises(ValueError):
            d.holder_supplement_registry(registry,self.state/'wi-facts-v1.json','surface_v1_muse_linux')
        (self.reader.parent/'holder_supplement.py').unlink()
        with self.assertRaises(ValueError):
            d.verify_reader_features(self.reader.parent.parent.parent,registry)

if __name__=='__main__':unittest.main()


class PendingHostWeekGateTests(unittest.TestCase):
    def test_pending_is_visible_but_not_a_read_failure(self):
        from copy import deepcopy
        from Stats.exporter.tests.verify_installed_dashboard import verify_weekly_activity
        source={'label':'fixture','kind':'relay_host','files_invalid':0,'errors':{},
                'status':'partial','reason':'host_usage_pending'}
        usage={'event_count':1,'time_unallocated_event_count':0,'conflicted_event_count':0,'source_details':[source]}
        view={'period':'week','dimension':'activity','summary':{'usage':usage,'cost':{'unpriced':[],'reasons':[]}},
              'groups':[{'key':dict(agent='duke',wi='cron',entrypoint='automation',node='ownermac',runtime='codex',model='test'),
                         'usage':{'event_count':1}}]}
        before=deepcopy(view); result=verify_weekly_activity(view)
        self.assertEqual(result['pending_host_sources'], 1)
        self.assertEqual(result['source_read_errors'], 0)
        self.assertEqual(view,before)
        for change in ({'reason':None},{'kind':'pi'},{'status':'unreachable'},{'files_invalid':1},{'errors':{'source_unreadable':1}}):
            broken=deepcopy(view);broken['summary']['usage']['source_details'][0].update(change)
            with self.assertRaisesRegex(AssertionError,'source_errors'): verify_weekly_activity(broken)


class ActiveActorPlanTests(unittest.TestCase):
    setUp=SourcePlanRetryTests.setUp
    prepare=SourcePlanRetryTests.prepare
    paths=SourcePlanRetryTests.paths

    def test_source_plan_refreshes_enabled_rules_without_enabling_another_reader(self):
        from copy import deepcopy
        from unittest.mock import patch
        import pulse_compute_cost.source_registration as registration
        original=json.loads(self.original)
        rule={'workspace_root':'/registered/local','agent':'pulse','node':'ownermac','runtime':'codex','registry_ref':'ccf1_'+'a'*64}
        original['sources'].append({'id':'codex','kind':'codex','path':str(self.platform/'sessions'),
            'actor_attribution':True,'workspace_actors':[rule]})
        before=json.dumps(original).encode();d.atomic(self.registry,before)
        def mapped(candidate,*args,**kw):
            value=deepcopy(candidate)
            for source in value['sources']:
                source['actor_attribution']=True
                if source['id']=='codex':source['workspace_actors']=[rule,{**rule,'workspace_root':'/registered/cloud'}]
            return value
        with patch.object(registration,'actor_candidate_registry',side_effect=mapped):
            self.prepare('rules-1')
        candidate=json.loads(self.paths('rules-1')[1].read_bytes())
        codex=next(s for s in candidate['sources'] if s['id']=='codex')
        self.assertEqual({r['workspace_root'] for r in codex['workspace_actors']},{'/registered/local','/registered/cloud'})
        self.assertTrue(all(s.get('actor_attribution') is not True for s in candidate['sources'] if s['id']!='codex'))
        self.assertEqual(self.registry.read_bytes(),before)
        self.assertEqual(candidate['ledger_root'],original['ledger_root'])

    def test_no_actor_reader_means_no_implicit_actor_activation(self):
        from unittest.mock import patch
        import pulse_compute_cost.source_registration as registration
        with patch.object(registration,'actor_candidate_registry',side_effect=AssertionError('implicit activation')):
            self.prepare('disabled-1')
        self.assertEqual(self.registry.read_bytes(),self.original)


class ApprovedDeploymentLineageTests(unittest.TestCase):
    def test_original_dispatch_sha_can_survive_unrelated_main_advances_but_not_divergence(self):
        import os, subprocess, tempfile
        from pathlib import Path
        import yaml
        root=Path(__file__).resolve().parents[3]
        job=yaml.safe_load((root/'.github/workflows/stats-macos-ci.yml').read_text())['jobs']['deploy']
        steps=job['steps']
        build=next(s['run'] for s in steps if s.get('name')=='Pin approved main and build a fresh production Release')
        install=next(s['run'] for s in steps if s.get('name')=='Install exact App and all actually invoked Stats runtime paths')
        self.assertNotIn('test "$PULSE_SOURCE_REVISION" = "$APPROVED_REVISION"',build)
        self.assertIn('--expected "$EXPECTED_ACTIVE"',install)
        self.assertIn('HEAD^{tree}',build)
        prefixes=[build.split('          WORK=',1)[0] if '          WORK=' in build else build.split('WORK=',1)[0],install.split('PY=',1)[0]]
        with tempfile.TemporaryDirectory() as tmp:
            gh=Path(tmp)/'gh'
            gh.write_text('#!/bin/sh\ncase "$2" in repos/bigbird-online/PULSE/compare/*...main) ;; *) exit 91;; esac\nprintf "%s\\n" "$FIXTURE_STATUS"\nexit "${FIXTURE_EXIT:-0}"\n')
            gh.chmod(0o700)
            env={**os.environ,'PATH':tmp+':/usr/bin:/bin','PULSE_SOURCE_REVISION':'a'*40,'APPROVED_REVISION':'a'*40}
            for prefix in prefixes:
                for status in ('identical','ahead','behind','diverged','unknown',''):
                    with self.subTest(status=status,build=prefix is prefixes[0]):
                        result=subprocess.run(['/bin/bash','-c',prefix],env={**env,'FIXTURE_STATUS':status},capture_output=True)
                        self.assertEqual(result.returncode==0,status in {'identical','ahead'},result.stderr)
                failure=subprocess.run(['/bin/bash','-c',prefix],env={**env,'FIXTURE_STATUS':'ahead','FIXTURE_EXIT':'7'},capture_output=True)
                self.assertNotEqual(failure.returncode,0)
            malformed=subprocess.run(['/bin/bash','-c',prefixes[0]],env={**env,'FIXTURE_STATUS':'ahead','APPROVED_REVISION':'main'},capture_output=True)
            self.assertNotEqual(malformed.returncode,0)


class ApprovedLedgerLagTests(unittest.TestCase):
    """Run the real build-step lineage gate against a temporary origin; no GitHub."""

    def setUp(self):
        import os, yaml
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name).resolve()
        self.home=self.root/'home';self.repo=self.home/'Coevolution/Bigbird/PULSE'
        self.repo.mkdir(parents=True)
        self.git_env={'PATH':'/usr/bin:/bin','HOME':str(self.home),'GIT_CONFIG_NOSYSTEM':'1',
            'GIT_AUTHOR_NAME':'t','GIT_AUTHOR_EMAIL':'t@example.invalid',
            'GIT_COMMITTER_NAME':'t','GIT_COMMITTER_EMAIL':'t@example.invalid'}
        self.git('init','-q','-b','main');self.git('config','uploadpack.allowAnySHA1InWant','true')
        self.approved=self.commit({'Stats/deploy.py':'v1\n','.harness/ledger.jsonl':'0\n'},'approved')
        workflow=Path(d.__file__).parents[1]/'.github/workflows/stats-macos-ci.yml'
        steps=yaml.safe_load(workflow.read_text())['jobs']['deploy']['steps']
        build=next(s['run'] for s in steps if s.get('name')=='Pin approved main and build a fresh production Release')
        # Everything up to and including the pinned-tree check; skip the Xcode build.
        gate=build.split('\ncd "$WORK/source"\n',1)[0]
        self.assertIn('HEAD^{tree}',gate)
        self.gate=gate.replace('https://github.com/bigbird-online/PULSE.git','file://'+str(self.repo))
        bin_=self.root/'bin';bin_.mkdir()
        (bin_/'gh').write_text('#!/bin/sh\ncase "$2" in\n'
            '  repos/bigbird-online/PULSE/compare/*) echo ahead ;;\n'
            '  repos/bigbird-online/PULSE/git/commits/*) git -C "$FIXTURE_REPO" rev-parse "${2##*/}^{tree}" ;;\n'
            '  *) exit 91 ;;\nesac\n')
        (bin_/'gh').chmod(0o700)
        self.bin=bin_

    def git(self,*argv):
        return subprocess.run(['git','-C',str(self.repo),*argv],check=True,capture_output=True,text=True,
            env=self.git_env).stdout.strip()

    def commit(self,files,message,*,remove=()):
        for name,body in files.items():
            path=self.repo/name;path.parent.mkdir(parents=True,exist_ok=True);path.write_text(body)
        for name in remove:self.git('rm','-q',name)
        self.git('add','-A');self.git('commit','-q','-m',message)
        return self.git('rev-parse','HEAD')

    def run_gate(self,approved,tip):
        import os
        runner=self.root/('runner-'+tip[:8]+'-'+approved[:8]);runner.mkdir()
        env={**self.git_env,'PATH':str(self.bin)+':/usr/bin:/bin','RUNNER_TEMP':str(runner),
             'GITHUB_RUN_ID':'1','GITHUB_RUN_ATTEMPT':'1','GITHUB_ENV':str(runner/'github-env'),
             'GH_TOKEN':'fixture','FIXTURE_REPO':str(self.repo),
             'APPROVED_REVISION':approved,'PULSE_SOURCE_REVISION':tip}
        result=subprocess.run(['/bin/bash','-c',self.gate],env=env,capture_output=True,text=True,timeout=60)
        source=runner/'wi300-deploy-1-1/source'
        return result,source

    def assert_deploys_approved_tree(self,approved,tip):
        result,source=self.run_gate(approved,tip)
        self.assertEqual(result.returncode,0,result.stderr)
        head=subprocess.run(['git','-C',str(source),'rev-parse','HEAD'],capture_output=True,text=True,env=self.git_env).stdout.strip()
        self.assertEqual(head,approved)
        return source

    def assert_rejected(self,approved,tip):
        result,_=self.run_gate(approved,tip)
        self.assertNotEqual(result.returncode,0,result.stdout)
        return result

    def test_approved_equal_to_dispatch_tip_still_deploys(self):
        self.assert_deploys_approved_tree(self.approved,self.approved)

    def test_approved_trailing_only_ledger_commits_deploys_the_approved_tree(self):
        self.approved=self.commit({'.harness/core/objects/aa/object':'history\n'},'approved history')
        self.commit({'.harness/ledger.jsonl':'0\n1\n'},'SCL: record native production decision')
        tip=self.commit({'.harness/ledger.jsonl':'0\n1\n2\n','.harness/state/x.json':'{}\n'},'SCL: record native production decision')
        source=self.assert_deploys_approved_tree(self.approved,tip)
        self.assertEqual((source/'.harness/ledger.jsonl').read_text(),'0\n')
        self.assertEqual((source/'Stats/deploy.py').read_text(),'v1\n')
        self.assertFalse((source/'.harness/core').exists())
        history=subprocess.check_output(
            ['git','-C',str(source),'show','HEAD:.harness/core/objects/aa/object'],env=self.git_env)
        self.assertEqual(history,b'history\n')

    def test_any_change_outside_harness_between_approved_and_tip_is_rejected(self):
        self.commit({'.harness/ledger.jsonl':'0\n1\n'},'SCL: record native production decision')
        code=self.commit({'Stats/deploy.py':'v2\n'},'unreviewed code')
        tip=self.commit({'.harness/ledger.jsonl':'0\n1\n2\n'},'SCL: record native production decision')
        result=self.assert_rejected(self.approved,tip)
        self.assertIn('outside .harness/',result.stderr)
        self.assertIn('Stats/deploy.py',result.stderr)
        # Reverting the change later still leaves an unreviewed commit in between.
        self.git('revert','--no-edit',code)
        self.assert_rejected(self.approved,self.git('rev-parse','HEAD'))

    def test_mixed_commit_and_rename_out_of_harness_are_rejected(self):
        mixed=self.commit({'.harness/ledger.jsonl':'0\n1\n','Scripts/new.py':'x\n'},'mixed')
        self.assert_rejected(self.approved,mixed)
        self.git('reset','-q','--hard',self.approved)
        self.git('mv','Stats/deploy.py','.harness/deploy.py');self.git('commit','-q','-m','rename')
        self.assert_rejected(self.approved,self.git('rev-parse','HEAD'))

    def test_approved_that_is_not_an_ancestor_of_the_tip_is_rejected(self):
        self.git('checkout','-q','-b','side')
        side=self.commit({'Stats/deploy.py':'side\n'},'side branch')
        self.git('checkout','-q','main')
        tip=self.commit({'.harness/ledger.jsonl':'0\n1\n'},'SCL: record native production decision')
        self.assertIn('is not an ancestor',self.assert_rejected(side,tip).stderr)
        # A newer tip is never an approved ancestor of an older dispatch.
        self.assertIn('is not an ancestor',self.assert_rejected(tip,self.approved).stderr)


class FailedCommandTests(unittest.TestCase):
    """A failed step names its cause without exposing URL credentials."""
    def test_failure_carries_masked_stderr_tail(self):
        script = "import sys; sys.stderr.write('x' * 900 + 'fatal: https://user:secret@github.com/o/r gone'); sys.exit(3)"
        with self.assertRaises(RuntimeError) as raised:
            d.run([sys.executable, '-c', script])
        message = str(raised.exception)
        self.assertIn('failed (3); stderr sha256=', message)
        self.assertIn('https://***@github.com/o/r gone', message)
        self.assertNotIn('secret', message)
        self.assertLess(len(message.split('stderr tail: ', 1)[1]), 801)

    def test_credentials_are_masked_before_the_tail_is_cut(self):
        userinfo = 'user:synthetic-secret@example.invalid/x\n'
        long_userinfo = 'user:' + 's' * 1200 + '@example.invalid/y'
        for stderr in ('fatal: https://' + userinfo + 'x' * (800 - len(userinfo)),
                       'fatal: https://' + long_userinfo,
                       'fatal: https://user:first@synthetic-secret@example.invalid/repo',
                       'fatal: user:first@synthetic-secret@example.invalid:repo'):
            script = 'import sys; sys.stderr.write(sys.argv[1]); sys.exit(3)'
            with self.assertRaises(RuntimeError) as raised:
                d.run([sys.executable, '-c', script, stderr])
            tail = str(raised.exception).split('stderr tail: ', 1)[1]
            self.assertNotIn('synthetic-secret', tail)
            self.assertNotIn('sss', tail)
            self.assertNotIn('user:', tail)
