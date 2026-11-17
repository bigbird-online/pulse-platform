"""Native input enrollment is reachable only through explicit installer admission."""
import importlib.util
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch
ROOT=Path(__file__).resolve().parents[2]
spec=importlib.util.spec_from_file_location('stats_enroll_test',ROOT/'Stats/deploy.py')
deploy=importlib.util.module_from_spec(spec);spec.loader.exec_module(deploy)
class NativeInstallerEntryTests(unittest.TestCase):
    def args(self):
        return SimpleNamespace(revision='a'*40,expected='a'*40,expected_native_registry='b'*64,
            expected_native_hooks='c'*64,site='ownermac')
    def test_installer_calls_exact_enrollment_module_with_reviewed_hashes(self):
        fake=SimpleNamespace(install=lambda *a,**k:{'status':'configured_pending_native_trust_and_invocation','args':a,'kwargs':k})
        loader=SimpleNamespace(exec_module=lambda m:None)
        with patch.object(importlib.util,'spec_from_file_location',return_value=SimpleNamespace(loader=loader)), \
                patch.object(importlib.util,'module_from_spec',return_value=fake), \
                patch.object(deploy,'canonical_python',return_value=Path('/native-home/.local/share/coevolution/python-env/bin/python')):
            home=Path('/native-home');result=deploy.enroll_native_purpose(self.args(),home=home)
        self.assertEqual(result['status'],'configured_pending_native_trust_and_invocation')
        self.assertEqual(result['args'][0],home)
        self.assertEqual(result['args'][2],home/'.local/share/coevolution/python-env/bin/python')
        self.assertEqual(result['kwargs']['expected_registry_sha256'],'b'*64)
        self.assertEqual(result['kwargs']['expected_hooks_sha256'],'c'*64)
    def test_inexact_or_unreviewed_enrollment_is_rejected_before_write(self):
        for field,value in [('revision','main'),('expected','d'*40),('expected_native_registry',None),('expected_native_hooks','anything')]:
            args=self.args();setattr(args,field,value)
            with self.assertRaises(ValueError):deploy.enroll_native_purpose(args,home=Path('/native-home'))
    def test_reader_downgrade_cannot_ignore_the_active_native_purpose_source(self):
        with tempfile.TemporaryDirectory() as d:
            runtime=Path(d)
            registry={'sources':[{'kind':'codex','request_purpose_directory':'/private/inputs'}]}
            with self.assertRaises(ValueError):deploy.verify_reader_features(runtime,registry)
            deploy.verify_reader_features(runtime,{'sources':[{'kind':'codex'}]})
    def test_cli_exposes_only_explicit_enrollment_mode(self):
        import subprocess,sys
        result=subprocess.run([sys.executable,str(ROOT/'Stats/deploy.py'),'--help'],text=True,capture_output=True,timeout=15)
        self.assertEqual(result.returncode,0)
        self.assertIn('enroll-native-purpose',result.stdout)
        self.assertIn('--expected-native-registry',result.stdout)
        self.assertIn('--expected-native-hooks',result.stdout)
if __name__=='__main__':unittest.main()
