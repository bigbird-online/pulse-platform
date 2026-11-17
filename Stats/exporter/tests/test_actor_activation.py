"""Installation ordering and metadata-only activation; isolated fixtures only."""
from contextlib import nullcontext
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[3]
sys.path[:0]=[str(ROOT/'Stats'),str(ROOT/'ComputeCost/uat')]
import activate_actor_sources as a
import deploy as d

SHA='a'*40
class ActorActivationTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.home=Path(self.tmp.name)
        self.release=self.home/'.local/share/pulse/stats/releases'/SHA
        self.release.mkdir(parents=True)
        (self.release/'.stats-source-revision').write_text(SHA)
        (self.release.parent.parent/'current').symlink_to(self.release)
        self.state=self.home/'.local/state/pulse/stats';self.state.mkdir(parents=True,mode=0o700)
        self.registry=self.home/'.local/state/pulse/compute-cost/sources.json'
        self.registry.parent.mkdir(mode=0o700)
        self.original={'schema':'computecost_source_registry/v1','ledger_root':'/unchanged-ledger',
            'sources':[{'id':'codex','kind':'codex','paths':['/registered/native']},
                       {'id':'peer','kind':'receipts','path':'/existing/transport'}]}
        self.registry.write_text(json.dumps(self.original));self.registry.chmod(0o600)
        self.surfaces={'schema_version':'test','agent_id':'pulse','surfaces':[{
            'workspace':'local-pulse-space','deployment':{'site':'ownermac','pulse_home':'/registered/PULSE'}}]}
        (self.release/'config').mkdir()
        (self.release/'config/scl-surfaces.yaml').write_text(json.dumps(self.surfaces))

    def test_activation_preserves_ledger_paths_peer_identity_and_is_idempotent(self):
        with patch.object(a,'ROOT',self.release),patch('effective_source_replay.runtime_environment',return_value={}):
            first=a.activate(self.home,SHA,'ownermac')
            data=self.registry.read_bytes()
            second=a.activate(self.home,SHA,'ownermac')
        self.assertTrue(first['changed']);self.assertFalse(second['changed'])
        candidate=json.loads(data)
        self.assertEqual(candidate['ledger_root'],self.original['ledger_root'])
        self.assertEqual(candidate['sources'][1],self.original['sources'][1])
        self.assertEqual(candidate['sources'][0]['paths'],['/registered/native'])
        self.assertEqual(candidate['sources'][0]['workspace_actors'][0]['agent'],'pulse')
        self.assertIn(a.FEATURE,candidate['required_receipt_features'])
        self.assertEqual(json.loads((self.state/('sources-before-actors-'+SHA+'.json')).read_bytes()),self.original)
        self.assertEqual(self.registry.stat().st_mode & 0o777,0o600)

    def test_history_enabled_registry_cannot_revert_to_a_reader_that_double_counts(self):
        from pulse_compute_cost.receipts import SUPPORTED_RECEIPT_FEATURES
        self.assertIn(a.HISTORY_FEATURE,SUPPORTED_RECEIPT_FEATURES)
        plan=a.plan(self.original,self.surfaces,'ownermac')
        self.assertIn(a.HISTORY_FEATURE,plan['required_receipt_features'])
        with patch.object(d,'run',return_value=b'["actor_context.v1"]'):
            with self.assertRaises(ValueError):d.verify_reader_features(self.release,plan)

    def test_current_migrated_input_directory_is_selected_idempotently(self):
        current=self.home/'PULSE/data/agent-harness/scl-host/human-inputs'
        original={**self.original,'sources':[{'id':'pi','kind':'pi','path':'/native',
                  'human_input_directory':'/registered-before-migration'}]}
        first=a.plan(original,self.surfaces,'ownermac',current)
        self.assertEqual(first['sources'][0]['human_input_directory'],str(current))
        self.assertEqual(first['sources'][0]['human_input_directories'],['/registered-before-migration'])
        self.assertEqual(a.plan(first,self.surfaces,'ownermac',current),first)

    def test_wrong_release_is_rejected_before_source_mutation(self):
        before=self.registry.read_bytes()
        with patch.object(a,'ROOT',self.release):
            with self.assertRaises(ValueError):a.activate(self.home,'b'*40,'ownermac')
        self.assertEqual(self.registry.read_bytes(),before)

    def test_uninstalled_copy_cannot_activate_sources(self):
        with patch.object(a,'ROOT',self.home/'arbitrary-checkout'):
            with self.assertRaises(ValueError):a.check_installed(self.home,SHA,'ownermac')

    def test_public_or_symlink_metadata_is_rejected(self):
        self.registry.chmod(0o644)
        with self.assertRaises(ValueError):a.private_bytes(self.registry)
        self.registry.chmod(0o600)
        link=self.registry.with_name('link');link.symlink_to(self.registry)
        with self.assertRaises(ValueError):a.private_bytes(link)

    def test_all_peers_must_pass_before_first_activation(self):
        peer={'host':'tcloud-node2','id':'tcloud','prefix':['runuser','-u','pulse','--'],'python':'/usr/bin/python3',
            'script':f'/home/pulse/.local/share/pulse/stats/releases/{SHA}/Scripts/pulse-stats-cost-refresh.py'}
        config={'source_revision':SHA,'peers':[peer],'paused_peers':{}}
        path=self.state/'refresh.json';path.write_text(json.dumps(config));path.chmod(0o600)
        with patch.object(a,'ROOT',self.release),patch.object(a,'run',side_effect=RuntimeError('old reader')),patch.object(a,'activate') as enable:
            with self.assertRaises(RuntimeError):a.activate_all(self.home,SHA)
        enable.assert_not_called()
        self.assertEqual(json.loads(self.registry.read_bytes()),self.original)

    def test_paused_peer_is_never_contacted(self):
        # By default TCloud is paused: its older reader is neither checked nor activated.
        peer={'host':'tcloud-node2','id':'tcloud','prefix':['runuser','-u','pulse','--'],'python':'/usr/bin/python3',
            'script':'/home/pulse/.local/share/pulse/stats/releases/'+'c'*40+'/Scripts/pulse-stats-cost-refresh.py'}
        path=self.state/'refresh.json';path.write_text(json.dumps({'source_revision':SHA,'peers':[peer]}));path.chmod(0o600)
        with patch.object(a,'ROOT',self.release),patch.object(a,'run',side_effect=AssertionError('peer contacted')), \
             patch.object(a,'check_installed'),patch.object(a,'activate',return_value={'site':'ownermac'}) as enable:
            report=a.activate_all(self.home,SHA)
        enable.assert_called_once()
        self.assertEqual(report['nodes'],[{'site':'ownermac'}])

    def test_older_reader_cannot_be_selected_after_actor_data_is_enabled(self):
        with patch.object(d,'run',return_value=b'[]'):
            with self.assertRaises(ValueError):d.verify_reader_features(self.release,{'required_receipt_features':[a.FEATURE]})
        with patch.object(d,'run',return_value=b'["actor_context.v1"]'):
            d.verify_reader_features(self.release,{'required_receipt_features':[a.FEATURE]})
        with patch.object(d,'run') as probe:
            d.verify_reader_features(self.release,{})
        probe.assert_not_called()

    def test_macos_bash_keeps_optional_argument_arrays_valid_when_flags_are_false(self):
        import subprocess
        import yaml
        workflow=yaml.safe_load((ROOT/'.github/workflows/stats-macos-ci.yml').read_text())
        steps=workflow['jobs']['deploy']['steps']
        for variable,step_name,expected_base in (
            ('SOURCE_ARGS','Install exact App and all actually invoked Stats runtime paths',['--codex-home','/fixture with spaces/.codex']),
            ('BUSINESS_ARGS','Verify a triggered publication and installed App restart against original queries',
             ['--trigger','--observations','1','--timeout','2400','--publisher-revision','p'*40])):
            body=next(step['run'] for step in steps if step['name']==step_name)
            selected=[];capturing=False
            for line in body.splitlines():
                if line.strip().startswith(variable+'='):capturing=True
                if capturing:
                    if line.strip().startswith('"$'):break
                    selected.append(line)
            self.assertTrue(selected)
            shell='set -euo pipefail\n'+'\n'.join(selected)+'\nprintf "%s\n" "${'+variable+'[@]}"\n'
            for enabled in ('false','true'):
                env=dict(os.environ,HOME='/fixture with spaces',ADOPT_RUNTIME_SOURCES=enabled,ACTIVATE_ACTOR_SOURCES=enabled,PUBLISHER='p'*40,
                         REQUIRED_WI='wi_20261015_422')
                result=subprocess.run(['/bin/bash','--noprofile','--norc','-c',shell],env=env,capture_output=True,text=True,check=True)
                actual=result.stdout.splitlines()
                self.assertEqual(actual[:len(expected_base)],expected_base)
                if enabled=='false':self.assertEqual(actual,expected_base)
                elif variable=='SOURCE_ARGS':self.assertIn('--adopt-runtime-sources',actual)
                else:
                    self.assertIn('--require-actors',actual)
                    self.assertEqual(actual[actual.index('--required-wi')+1],'wi_20261015_422')
            if variable=='BUSINESS_ARGS':
                # The required WI is a dispatch input, never a stale constant; a malformed one stops.
                env=dict(os.environ,HOME='/fixture with spaces',ADOPT_RUNTIME_SOURCES='true',ACTIVATE_ACTOR_SOURCES='true',
                         PUBLISHER='p'*40,REQUIRED_WI='wi_184')
                refused=subprocess.run(['/bin/bash','--noprofile','--norc','-c',shell],env=env,capture_output=True,text=True)
                self.assertNotEqual(refused.returncode,0);self.assertIn('required_wi must be a WI id',refused.stderr)

if __name__=='__main__':unittest.main()
