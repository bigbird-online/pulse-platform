"""Local publisher adoption: real files/locks, simulated launchd only."""
from contextlib import contextmanager, nullcontext
from copy import deepcopy
import json
from pathlib import Path
import plistlib
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from Stats.exporter.tests.test_stats_deploy import d, A, B, payload
import tempfile

class LocalRefreshDeploymentTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.home=Path(self.temp.name).resolve(); self.state=self.home/'.local/state/pulse/stats'
        self.state.mkdir(parents=True,mode=0o700)
        self.config=self.state/'refresh.json'
        self.registry=self.home/'.local/state/pulse/compute-cost/sources.json'
        self.registry.parent.mkdir(parents=True,mode=0o700)
        d.atomic(self.registry,b'{"sources":[],"ledger_root":"unchanged","extra":true}')
        self.reg_before=self.registry.read_bytes()
        self.original={'source_revision':A,'peers':[{'script':'remote-old'}],
            'destination':{'script':'remote-old'},'codex_quota':{'home':'keep'},'future_field':{'keep':[1]}}
        d.atomic(self.config,json.dumps(self.original).encode())
        self.config_before=self.config.read_bytes()
        self.job_path=self.home/'Library/LaunchAgents'/f'{d.LABEL}.plist'
        self.job_path.parent.mkdir(parents=True)
        self.job={'Label':d.LABEL,'StartInterval':900,'RunAtLoad':True,'ProcessType':'Standard',
            'EnvironmentVariables':{'unchanged':'yes'},'ProgramArguments':[
                str(self.home/'.local/state/pulse/compute-cost/venv/bin/python'),
                str(self.home/'.local/share/pulse/stats/releases'/A/'Scripts/pulse-stats-cost-refresh.py'),
                'refresh','--config',str(self.config)]}
        d.atomic(self.job_path,plistlib.dumps(self.job));self.job_before=self.job_path.read_bytes()
        d.stage_runtime(payload(),self.home,A)
        self.current=self.home/'.local/share/pulse/stats/current'
        self.current.symlink_to(self.current.parent/'releases'/A)
        self.args=SimpleNamespace(source=self.home,revision=B,expected=A)
        self.loaded=True;self.loaded_job=deepcopy(self.job);self.active=False;self.calls=[];self.bootstrap_failure=False;self.partial_bootstrap=False
        self.bootout_failure=False
        self.python_check=patch.object(d,'canonical_python',return_value=self.home/'.local/share/coevolution/python-env/bin/python')
        self.python_check.start();self.addCleanup(self.python_check.stop)
        self.archive=patch.object(d,'archive',return_value=payload());self.archive.start();self.addCleanup(self.archive.stop)
        self.invoke=patch.object(d,'run',side_effect=self.launchctl);self.invoke.start();self.addCleanup(self.invoke.stop)
        self.reconcile=patch.object(d.subprocess,'run',side_effect=lambda *a,**k:SimpleNamespace(returncode=0 if self.loaded else 113,stderr=('Could not find service "'+d.LABEL+'"').encode()))
        self.reconcile.start();self.addCleanup(self.reconcile.stop)

    def launchctl(self,argv,**kwargs):
        self.calls.append(argv)
        if argv[0]!='/bin/launchctl':raise AssertionError(argv)
        if argv[1]=='print':
            if not self.loaded:raise RuntimeError('not loaded')
            job=self.loaded_job
            return ('state = '+('running\npid = 123' if self.active else 'not running')+
                '\narguments = {\n'+'\n'.join(job['ProgramArguments'])+'\n}\n').encode()
        if argv[1]=='bootout':
            self.assertFalse(self.active,'in-flight refresh terminated')
            self.loaded=False
            if self.bootout_failure:
                self.bootout_failure=False;raise TimeoutError('bootout uncertain')
        elif argv[1]=='bootstrap':
            self.loaded_job=plistlib.loads(self.job_path.read_bytes())
            if self.bootstrap_failure:
                self.bootstrap_failure=False
                self.loaded=self.partial_bootstrap;self.active=self.partial_bootstrap
                raise TimeoutError('bootstrap uncertain')
            self.loaded=True
        else:raise AssertionError(argv)
        return b''

    def receipts(self):
        return [json.loads(p.read_bytes()) for p in self.state.glob('refresh-local-*/receipt.json')]

    def select_native_fixture(self):
        for revision in (A, B):
            release = self.home/'.local/share/pulse/releases'/('PULSE-'+revision)
            (release/'Scripts').mkdir(parents=True)
            (release/'Scripts/pulse-stats-cost-refresh.py').write_text('# sealed publisher\n')
        self.job['ProgramArguments'] = d.native_publisher_arguments(self.home, A)
        self.loaded_job = deepcopy(self.job)
        d.atomic(self.job_path, plistlib.dumps(self.job))
        self.job_before = self.job_path.read_bytes()
        self.args.native_publisher_only = True

        def invoke(argv, **kwargs):
            if '--probe-cost-row-contexts' in argv:
                return b'{"schema":"pulse_stats_cost_row_contexts/v1"}'
            if '--probe-cost-compression' in argv:
                return b'{"schema":"pulse_stats_cost_compression/v1"}'
            return self.launchctl(argv, **kwargs)

        self.invoke.stop()
        for target, name, kwargs in (
            (d, 'PUBLISHER_HOME', {'new': self.home}),
            ('runpy', 'run_path', {'return_value': {'operation_lock': lambda _: nullcontext()}}),
            (d, 'checked_native_publisher',
             {'return_value': (self.home/'.local/share/pulse/releases'/('PULSE-'+B), 'github:42:1')}),
            (d, 'run', {'side_effect': invoke}),
        ):
            mutation = patch(target+'.'+name, **kwargs) if isinstance(target, str) else patch.object(target, name, **kwargs)
            mutation.start()
            self.addCleanup(mutation.stop)

    def test_native_publisher_only_keeps_cadence_peers_reader_and_registry(self):
        self.select_native_fixture()
        d.deploy_refresh_local(self.args, home=self.home)
        self.assertEqual(self.loaded_job, {**self.job, 'ProgramArguments': d.native_publisher_arguments(self.home, B)})
        self.assertEqual(json.loads(self.config.read_bytes()), {**self.original, 'source_revision': B})
        self.assertEqual(self.current.resolve().name, A)
        self.assertEqual(self.registry.read_bytes(), self.reg_before)
        receipt = self.receipts()[0]
        self.assertEqual((receipt['status'], receipt['publisher_owner'], receipt['authorization']),
                         ('installed', 'pulse_release', 'github:42:1'))
        self.assertFalse((self.state/'publisher-enrollments').exists())

    def test_native_publisher_only_restores_original_after_failed_bootstrap(self):
        self.select_native_fixture()
        self.bootstrap_failure = True
        with self.assertRaises(TimeoutError):
            d.deploy_refresh_local(self.args, home=self.home)
        self.assertTrue(self.loaded)
        self.assertEqual(self.loaded_job, self.job)
        self.assertEqual(self.config.read_bytes(), self.config_before)
        self.assertEqual(self.job_path.read_bytes(), self.job_before)
        self.assertFalse(self.receipts()[0]['rollback_pending'])

    def test_verified_native_publisher_checks_main_provenance_and_sealed_bytes(self):
        self.invoke.stop()
        release = self.home/'.local/share/pulse/releases'/('PULSE-'+B)
        script = release/'Scripts/pulse-stats-cost-refresh.py'
        script.parent.mkdir(parents=True)
        row = {'head_sha': B, 'head_branch': 'main', 'event': 'push', 'conclusion': 'success',
               'path': '.github/workflows/default-branch-verification.yml', 'id': 42, 'run_attempt': 2}

        def observed(result):
            return patch.object(d, 'run', return_value=json.dumps({'workflow_runs': [result]}).encode())

        def verify(provenance):
            return {'verify_release': lambda target, revision: {'provenance': provenance}}

        with observed({**row, 'head_branch': 'untrusted'}):
            with self.assertRaisesRegex(ValueError, 'exact-main verification'):
                d.checked_native_publisher(self.home, self.home, B, payload(), verify('managed'))
        with observed(row):
            script.write_bytes(b'print(1)\n')
            script.chmod(0o444)
            with self.assertRaisesRegex(ValueError, 'sealed program'):
                d.checked_native_publisher(self.home, self.home, B, payload(), verify('legacy'))
            with self.assertRaisesRegex(ValueError, 'asset differs'):
                d.checked_native_publisher(self.home, self.home, B, payload(body=b'print(2)\n'), verify('managed'))
            self.assertEqual(d.checked_native_publisher(self.home, self.home, B, payload(), verify('managed')),
                             (release, 'github:42:2'))

    def test_only_local_identity_changes(self):
        d.deploy_refresh_local(self.args,home=self.home)
        config=json.loads(self.config.read_bytes());job=plistlib.loads(self.job_path.read_bytes())
        self.assertEqual(config,{**self.original,'source_revision':B})
        expected=d.publisher_launchagent(deepcopy(self.job))
        expected['ProgramArguments']=d.publisher_arguments(self.home,self.home/'.local/share/pulse/stats/publisher-releases'/B)
        self.assertEqual(job,expected)
        self.assertEqual(job['StartInterval'],1800)
        self.assertEqual(self.loaded_job['StartInterval'],1800)
        self.assertEqual(self.registry.read_bytes(),self.reg_before)
        self.assertEqual(self.current.resolve().name,A)
        self.assertEqual(json.loads(self.config.read_bytes())['source_revision'],B)
        self.assertEqual(self.receipts()[0]['status'],'installed')
        indexed=list((self.state/'retention-adoptions').glob('*.json'))
        self.assertEqual(len(indexed),1)
        retention=json.loads(indexed[0].read_bytes())
        self.assertEqual(retention['component'],'local_cost_refresh')
        self.assertEqual(retention['previous'],A)
        self.assertEqual(retention['acceptance'],'unverified')
        self.assertEqual(retention['payload_expiry'],'not_authorized')
        self.assertEqual(retention['publisher']['policy'],d.PUBLISHER_POLICY)
        self.assertFalse((self.home/'unchanged').exists())

    def test_canonical_interpreter_migration_preserves_other_job_fields(self):
        changed = d.local_refresh_inputs(self.home, B, A, self.original, self.job)[1]
        self.assertEqual(changed['ProgramArguments'][0],
            str(self.home/'.local/share/coevolution/python-env/bin/python'))
        self.assertEqual({k:v for k,v in changed.items() if k not in {'ProgramArguments','StartInterval'}},
            {k:v for k,v in self.job.items() if k not in {'ProgramArguments','StartInterval'}})
        canonical = deepcopy(self.job)
        canonical['ProgramArguments'][0] = changed['ProgramArguments'][0]
        self.assertEqual(d.local_refresh_inputs(self.home,B,A,self.original,canonical)[1],changed)
        canonical['ProgramArguments'][0] = '/tmp/foreign/bin/python'
        with self.assertRaises(ValueError):
            d.local_refresh_inputs(self.home,B,A,self.original,canonical)

    def test_missing_canonical_environment_fails_before_any_runtime_write(self):
        with patch.object(d,'canonical_python',side_effect=FileNotFoundError('missing')):
            with self.assertRaises(FileNotFoundError):d.deploy_refresh_local(self.args,home=self.home)
        self.assertEqual(self.calls,[])
        self.assertEqual(self.config.read_bytes(),self.config_before)
        self.assertEqual(self.job_path.read_bytes(),self.job_before)
        self.assertEqual(self.current.resolve().name,A)
        self.assertEqual(self.receipts(),[])
        self.assertFalse((self.current.parent/'releases'/B).exists())

    def test_cross_version_installer_rejected_before_staging_or_service_change(self):
        with patch.object(d,'EXECUTING_INSTALLER_SHA256',new='0'*64):
            with self.assertRaises(ValueError):d.deploy_refresh_local(self.args,home=self.home)
        self.assertEqual(self.calls,[])
        self.assertFalse((self.home/'.local/share/pulse/stats/publisher-releases').exists())
        self.assertEqual(self.config.read_bytes(),self.config_before)

    def test_active_job_and_locked_collector_are_preserved(self):
        self.active=True
        with self.assertRaises(ValueError):d.deploy_refresh_local(self.args,home=self.home)
        self.assertFalse(any(c[1]=='bootout' for c in self.calls))
        self.active=False
        with d.writer_lock(self.state/'cost-refresh.lock',timeout=0):
            with self.assertRaises(TimeoutError):d.deploy_refresh_local(self.args,home=self.home)
        self.assertEqual(self.config.read_bytes(),self.config_before)
        self.assertEqual(self.job_path.read_bytes(),self.job_before)
        self.assertEqual(self.current.resolve().name,A)
        self.assertFalse((self.state/'retention-adoptions').exists())

    def test_bootout_timeout_reconciles_and_restores(self):
        self.bootout_failure=True
        with self.assertRaises(TimeoutError):d.deploy_refresh_local(self.args,home=self.home)
        self.assertTrue(self.loaded)
        self.assertEqual(self.config.read_bytes(),self.config_before)
        self.assertEqual(self.current.resolve().name,A)
        self.assertFalse(self.receipts()[0]['rollback_pending'])

    def test_failed_bootstrap_restores_idle_original(self):
        self.bootstrap_failure=True
        with self.assertRaises(TimeoutError):d.deploy_refresh_local(self.args,home=self.home)
        self.assertTrue(self.loaded)
        self.assertEqual(self.config.read_bytes(),self.config_before)
        self.assertEqual(self.job_path.read_bytes(),self.job_before)
        self.assertEqual(self.current.resolve().name,A)
        self.assertFalse(self.receipts()[0]['rollback_pending'])

    def test_uncertain_bootstrap_never_kills_new_active_refresh(self):
        self.bootstrap_failure=True;self.partial_bootstrap=True
        with self.assertRaises(TimeoutError):d.deploy_refresh_local(self.args,home=self.home)
        self.assertTrue(self.active)
        self.assertEqual(json.loads(self.config.read_bytes())['source_revision'],B)
        self.assertEqual(self.current.resolve().name,A)
        self.assertEqual(json.loads(self.config.read_bytes())['source_revision'],B)
        self.assertTrue(self.receipts()[0]['rollback_pending'])
        self.assertEqual(sum(c[1]=='bootout' for c in self.calls),1)

    def test_stale_configuration_does_not_switch(self):
        self.args.expected='c'*40
        with self.assertRaises(ValueError):d.deploy_refresh_local(self.args,home=self.home)
        self.assertEqual(self.calls,[])
        self.assertEqual(self.config.read_bytes(),self.config_before)

    def test_mid_switch_write_failure_restores_bytes(self):
        original=d.atomic;failed=False
        def write(path,data,*a,**k):
            nonlocal failed
            if path==self.job_path and data!=self.job_before and not failed:
                failed=True;raise OSError('injected')
            return original(path,data,*a,**k)
        with patch.object(d,'atomic',side_effect=write),self.assertRaises(OSError):
            d.deploy_refresh_local(self.args,home=self.home)
        self.assertTrue(self.loaded)
        self.assertEqual(self.config.read_bytes(),self.config_before)
        self.assertEqual(self.job_path.read_bytes(),self.job_before)
        self.assertEqual(self.current.resolve().name,A)

    def test_current_drift_refuses_before_bootout(self):
        c='c'*40;d.stage_runtime(payload(),self.home,c)
        self.current.unlink();self.current.symlink_to(self.current.parent/'releases'/c)
        with self.assertRaises(ValueError):d.deploy_refresh_local(self.args,home=self.home)
        self.assertTrue(self.loaded)
        self.assertFalse(any(c[1]=='bootout' for c in self.calls))
        self.assertEqual(self.current.resolve().name,c)

    def test_loaded_entrypoint_drift_refuses_before_bootout(self):
        self.loaded_job['ProgramArguments'][1]='unknown.py'
        with self.assertRaises(ValueError):d.deploy_refresh_local(self.args,home=self.home)
        self.assertTrue(self.loaded)
        self.assertFalse(any(c[1]=='bootout' for c in self.calls))

    def test_unknown_scheduler_failure_is_not_absence(self):
        self.bootstrap_failure=True
        with patch.object(d.subprocess,'run',return_value=SimpleNamespace(returncode=5,stderr=b'permission denied')):
            with self.assertRaises(TimeoutError):d.deploy_refresh_local(self.args,home=self.home)
        self.assertEqual(json.loads(self.config.read_bytes())['source_revision'],B)
        self.assertEqual(self.current.resolve().name,A)
        self.assertEqual(json.loads(self.config.read_bytes())['source_revision'],B)
        self.assertTrue(self.receipts()[0]['rollback_pending'])

    def test_deployment_lock_busy_preserves_every_entrypoint(self):
        with d.writer_lock(self.state/'deployment.lock',timeout=0):
            with self.assertRaises(TimeoutError):d.deploy_refresh_local(self.args,home=self.home)
        self.assertEqual(self.calls,[])
        self.assertEqual(self.config.read_bytes(),self.config_before)
        self.assertEqual(self.current.resolve().name,A)

    def test_failed_receipt_write_does_not_claim_rollback(self):
        original=d.atomic;failed=False
        def write(path,data,*a,**k):
            nonlocal failed
            if path.name=='receipt.json' and json.loads(data)['status']=='installed' and not failed:
                failed=True;raise OSError('receipt disk failure')
            return original(path,data,*a,**k)
        with patch.object(d,'atomic',side_effect=write),self.assertRaises(OSError):
            d.deploy_refresh_local(self.args,home=self.home)
        self.assertEqual(self.current.resolve().name,A)
        self.assertEqual(json.loads(self.config.read_bytes())['source_revision'],B)
        self.assertEqual(self.receipts()[0]['status'],'installed_receipt_failed')
        self.assertTrue(self.receipts()[0]['activation_confirmed'])

    def test_failed_bootstrap_same_original_target_retry_runs_business_without_discarding_audit(self):
        self.bootstrap_failure=True
        with self.assertRaises(TimeoutError):d.deploy_refresh_local(self.args,home=self.home)
        self.assertEqual(len(list((self.state/'publisher-enrollments').iterdir())),1)
        d.deploy_refresh_local(self.args,home=self.home)
        self.assertEqual(len(list((self.state/'publisher-enrollments').iterdir())),2)
        from Stats import publisher as actual_entry
        actual_entry.publish(self.home,self.config)
        self.assertEqual(d.selected_publisher_runtime(self.home,B,self.loaded_job).name,B)
        self.assertTrue((self.state/'publisher-enrollments').is_dir())

    def test_critical_enrollment_failure_restores_old_consumer_before_new_bootstrap(self):
        with patch.object(d,'record_retention_adoption',side_effect=OSError('critical seed unavailable')):
            with self.assertRaises(OSError):d.deploy_refresh_local(self.args,home=self.home)
        self.assertEqual(self.config.read_bytes(),self.config_before)
        self.assertEqual(self.job_path.read_bytes(),self.job_before)
        self.assertEqual(self.loaded_job,self.job)
        self.assertTrue(self.loaded)
        self.assertFalse(self.receipts()[0].get('activation_confirmed'))
        self.assertFalse((self.state/'publisher-enrollments').exists())

    def test_failed_retention_write_preserves_the_adopted_active_consumer(self):
        original_index=d.record_retention_adoption
        def final_index_fails(*args,**kwargs):
            if kwargs.get('enrollment'):return original_index(*args,**kwargs)
            raise OSError('index disk failure')
        with patch.object(d,'record_retention_adoption',side_effect=final_index_fails):
            with self.assertRaises(OSError): d.deploy_refresh_local(self.args, home=self.home)
        self.assertEqual(self.current.resolve().name, A)
        self.assertEqual(json.loads(self.config.read_bytes())['source_revision'],B)
        self.assertTrue(self.loaded)
        self.assertEqual(sum(c[1]=='bootout' for c in self.calls), 1)
        self.assertEqual(self.receipts()[0]['status'], 'installed_receipt_failed')
        self.assertTrue(self.receipts()[0]['activation_confirmed'])
        self.assertFalse(self.receipts()[0]['rollback_pending'])
        from Stats import publisher as actual_entry
        from contextlib import redirect_stdout
        import io
        output=io.StringIO()
        with redirect_stdout(output):actual_entry.publish(self.home,self.config)
        self.assertIn('1\n',output.getvalue())
        self.assertTrue((self.state/'publisher-enrollments').is_dir())
        self.assertFalse((self.state/'publisher-retirements').exists())



if __name__=='__main__':unittest.main()
