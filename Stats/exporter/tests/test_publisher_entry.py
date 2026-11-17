"""The real loader must serialize before opening versioned code, until exit."""
import fcntl
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from Stats import publisher as p
from Stats.exporter.tests.test_stats_deploy import d, A, B, payload


class PublisherEntryTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.home=Path(self.temp.name).resolve()
        self.state=self.home/'.local/state/pulse/stats';self.state.mkdir(parents=True,mode=0o700)
        self.config=self.state/'refresh.json';d.atomic(self.config,json.dumps({'source_revision':A}).encode())
        self.runtime=d.stage_runtime(payload(),self.home,A)
        self.current=self.runtime.parent.parent/'current';self.current.symlink_to(self.runtime)

    def test_lock_busy_cannot_open_versioned_code(self):
        with d.writer_lock(self.state/'deployment.lock',timeout=0), patch.object(p,'runpy') as load:
            with self.assertRaises(TimeoutError):p.publish(self.home,self.config,lock_timeout=0)
            load.run_path.assert_not_called()

    def test_entire_import_and_exit_hold_lock_and_restore_argv(self):
        argv=sys.argv
        def execute(path,run_name):
            self.assertEqual(path,str(self.runtime/'Scripts/pulse-stats-cost-refresh.py'))
            self.assertEqual(run_name,'__main__')
            with self.assertRaises(TimeoutError):
                with d.writer_lock(self.state/'deployment.lock',timeout=0):pass
            raise SystemExit(7)
        with patch.object(p.runpy,'run_path',side_effect=execute),self.assertRaises(SystemExit) as result:
            p.publish(self.home,self.config,exclusive=False)
        self.assertEqual(result.exception.code,7);self.assertIs(sys.argv,argv)
        with d.writer_lock(self.state/'deployment.lock',timeout=0):pass

    def test_foreign_config_marker_selector_and_bytes_are_rejected_before_load(self):
        for failure in ('config','marker','selector','script'):
            with self.subTest(failure=failure), patch.object(p.runpy,'run_path') as load:
                if failure=='config':d.atomic(self.config,json.dumps({'source_revision':B}).encode())
                elif failure=='marker':d.atomic(self.runtime/'.stats-source-revision',B.encode())
                elif failure=='selector':self.current.unlink();self.current.symlink_to(self.runtime.parent/B)
                else:d.atomic(self.runtime/'Scripts/pulse-stats-cost-refresh.py',b'changed')
                with self.assertRaises((ValueError,FileNotFoundError)):p.publish(self.home,self.config,exclusive=False)
                load.assert_not_called()
                d.atomic(self.config,json.dumps({'source_revision':A}).encode())
                d.atomic(self.runtime/'.stats-source-revision',A.encode())
                self.current.unlink();self.current.symlink_to(self.runtime)

    def test_symlink_lock_and_script_are_rejected(self):
        target=self.state/'outside';d.atomic(target,b'')
        lock=self.state/'deployment.lock';lock.symlink_to(target)
        with self.assertRaises(OSError):p.publish(self.home,self.config,exclusive=False)
        lock.unlink()
        script=self.runtime/'Scripts/pulse-stats-cost-refresh.py';script.unlink();script.symlink_to(target)
        with self.assertRaises(ValueError):p.publish(self.home,self.config,exclusive=False)

    def test_immutable_entry_is_outside_release_and_existing_bytes_cannot_be_replaced(self):
        args=d.publisher_arguments(self.home,self.runtime,install=True)
        entry=Path(args[1]);before=entry.stat()
        self.assertEqual(entry.parent,self.runtime.parent.parent/'publishers')
        self.assertEqual(entry.name,hashlib.sha256(entry.read_bytes()).hexdigest()+'.py')
        self.assertEqual(args[-2:],['--config',str(self.config)])
        self.assertEqual(d.publisher_arguments(self.home,self.runtime,install=True),args)
        self.assertEqual(entry.stat().st_ino,before.st_ino)
        d.atomic(entry,b'changed')
        with self.assertRaises(ValueError):d.publisher_arguments(self.home,self.runtime,install=True)

    def test_next_adoption_accepts_the_original_legacy_stable_entry(self):
        # A real old writer installs its old immutable launcher, not this new
        # exclusive-capability launcher into an unregistered shared pool.
        d.atomic(self.runtime/'Stats/publisher.py',b'# legacy shared entry fixture\n')
        args=d.publisher_arguments(self.home,self.runtime,install=True)
        original=json.loads(self.config.read_bytes())
        job={'Label':d.LABEL,'ProgramArguments':args}
        job_path=self.home/'Library/LaunchAgents'/f'{d.LABEL}.plist'
        job_path.parent.mkdir(parents=True);d.atomic(job_path,__import__('plistlib').dumps(job))
        config,changed=d.local_refresh_inputs(self.home,B,A,original,job)
        self.assertEqual(config,{'source_revision':B})
        self.assertEqual(job['ProgramArguments'],args)
        self.assertIn(B,changed['ProgramArguments'][1])
        d.atomic(self.runtime/'Stats/publisher.py',Path(p.__file__).read_bytes())
        job['ProgramArguments']=d.publisher_arguments(self.home,self.runtime,install=True)
        d.atomic(job_path,__import__('plistlib').dumps(job))
        with self.assertRaises(ValueError):d.local_refresh_inputs(self.home,B,A,original,job)

    def test_real_git_archive_permissions_support_the_actual_loader(self):
        source=self.home/'source';source.mkdir()
        marker=self.state/'fixture-business-ran'
        files={
            'Scripts/pulse-stats-cost-refresh.py':
                'from pathlib import Path\nPath('+repr(str(marker))+').write_text("loaded")\n',
            'Stats/publisher.py':Path(p.__file__).read_text(),
            'ComputeCost/fixture.py':'# fixture\n',
            'config/scl-surfaces.yaml':'{}\n',
            'Stats/executable-fixture':'#!/bin/sh\nexit 0\n',
        }
        for name,body in files.items():
            path=source/name;path.parent.mkdir(parents=True,exist_ok=True);path.write_text(body)
            path.chmod(0o755 if name=='Stats/executable-fixture' else 0o644)
        env={**os.environ,'GIT_CONFIG_NOSYSTEM':'1','GIT_CONFIG_GLOBAL':os.devnull,
             'GIT_NO_LAZY_FETCH':'1'}
        def git(*args):
            return subprocess.check_output(['git','-C',str(source),*args],env=env,stderr=subprocess.PIPE,timeout=15)
        git('init','--quiet');git('config','tar.umask','0002');git('add','.')
        git('-c','core.hooksPath='+os.devnull,'-c','user.name=Fixture','-c','user.email=fixture@example.invalid',
            '-c','commit.gpgsign=false','commit','--quiet','-m','fixture')
        revision=git('rev-parse','HEAD').decode().strip()
        data=d.archive(source,revision)
        with tarfile.open(fileobj=io.BytesIO(data),mode='r:') as tar:
            self.assertEqual(tar.getmember('Scripts/pulse-stats-cost-refresh.py').mode,0o664)
            self.assertEqual(tar.getmember('Stats/executable-fixture').mode,0o775)
        runtime=d.stage_runtime(data,self.home,revision)
        self.assertEqual((runtime/'Scripts/pulse-stats-cost-refresh.py').stat().st_mode & 0o777,0o644)
        self.assertEqual((runtime/'Stats/executable-fixture').stat().st_mode & 0o777,0o755)
        self.current.unlink();self.current.symlink_to(runtime)
        d.atomic(self.config,json.dumps({'source_revision':revision}).encode())
        # Real loader and versioned fixture; no production config or network.
        p.publish(self.home,self.config,exclusive=False)
        self.assertEqual(marker.read_text(),'loaded')
        self.assertEqual(d.stage_runtime(data,self.home,revision),runtime)

    def test_group_writable_payload_is_rejected_without_mutating_existing_release(self):
        script=self.runtime/'Scripts/pulse-stats-cost-refresh.py';script.chmod(0o664)
        before=script.stat()
        with patch.object(p.runpy,'run_path') as load:
            with self.assertRaises(ValueError):p.publish(self.home,self.config,exclusive=False)
            load.assert_not_called()
        with self.assertRaises(ValueError):d.stage_runtime(payload(),self.home,A)
        after=script.stat()
        self.assertEqual((before.st_ino,before.st_mode,before.st_ctime_ns),
                         (after.st_ino,after.st_mode,after.st_ctime_ns))

    def test_main_refuses_public_alias_or_hardlinked_entry_before_loading(self):
        args=d.publisher_arguments(self.home,self.runtime,install=True)
        entry=Path(args[1]);directory=entry.parent
        for failure in ('file_mode','directory_mode','directory_link','hardlink'):
            with self.subTest(failure=failure):
                moved=directory.with_name('publisher-before');alias=directory/'duplicate.py'
                if failure=='file_mode':entry.chmod(0o644)
                elif failure=='directory_mode':directory.chmod(0o755)
                elif failure=='directory_link':directory.rename(moved);directory.symlink_to(moved)
                else:os.link(entry,alias)
                try:
                    with patch.object(p,'__file__',str(entry)), patch.object(p.pwd,'getpwuid',return_value=SimpleNamespace(pw_dir=str(self.home))), \
                            patch.object(p.sys,'executable',args[0]), patch.object(p.sys,'argv',[str(entry),'--config',str(self.config)]), \
                            patch.object(p.runpy,'run_path') as load:
                        self.assertEqual(p.main(),2);load.assert_not_called()
                    with self.assertRaises(ValueError):d.publisher_arguments(self.home,self.runtime)
                finally:
                    if failure=='directory_link':directory.unlink();moved.rename(directory)
                    elif failure=='hardlink':alias.unlink()
                    else:entry.chmod(0o600);directory.chmod(0o700)

    def test_running_real_program_blocks_deployment_and_sigterm_releases(self):
        # Disposable child and fixture only; never signal production processes.
        source=Path(p.__file__).resolve()
        script=self.runtime/'Scripts/pulse-stats-cost-refresh.py'
        script.write_text('from pathlib import Path\nimport time\nPath('+repr(str(self.state/'loaded'))+').touch()\ntime.sleep(60)\n')
        d.atomic(self.runtime/'.stats-files.json',json.dumps({'Scripts/pulse-stats-cost-refresh.py':hashlib.sha256(script.read_bytes()).hexdigest()}).encode())
        bootstrap='import runpy,pathlib;ns=runpy.run_path('+repr(str(source))+');ns["publish"](pathlib.Path('+repr(str(self.home))+'),pathlib.Path('+repr(str(self.config))+'),exclusive=False)'
        child=subprocess.Popen([sys.executable,'-B','-c',bootstrap],stdout=subprocess.PIPE,stderr=subprocess.PIPE)
        try:
            deadline=time.monotonic()+8
            while not (self.state/'loaded').exists() and child.poll() is None and time.monotonic()<deadline:time.sleep(.02)
            self.assertTrue((self.state/'loaded').exists())
            with self.assertRaises(TimeoutError):
                with d.writer_lock(self.state/'deployment.lock',timeout=0):pass
            child.terminate();child.wait(timeout=5)
            with d.writer_lock(self.state/'deployment.lock',timeout=0):pass
        finally:
            if child.poll() is None:child.kill();child.wait(timeout=5)
            child.communicate(timeout=5)


if __name__=='__main__':unittest.main()
