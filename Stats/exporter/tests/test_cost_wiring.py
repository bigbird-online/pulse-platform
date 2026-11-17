"""Pure read-only tests for loaded publisher and Git-blob attestation."""
import hashlib
import importlib.util
import json
import os
import pwd
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('wi300_wiring', Path(__file__).with_name('inspect_cost_wiring.py'))
wiring = importlib.util.module_from_spec(spec)
spec.loader.exec_module(wiring)


class CostWiringTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def release(self, name, revision):
        root = self.root / name
        root.mkdir()
        manifest = {}
        for key, path in wiring.FILES.items():
            dest = root / path
            dest.parent.mkdir(parents=True, exist_ok=True)
            data = (name + ':' + key).encode()
            dest.write_bytes(data)
            manifest[path] = hashlib.sha256(data).hexdigest()
        (root / '.stats-source-revision').write_text(revision)
        (root / '.stats-files.json').write_text(json.dumps(manifest))
        return root

    def authorization(self, root):
        return {'source_layout': 'flat', 'component_sha256': {
            key: hashlib.sha256((root / path).read_bytes()).hexdigest()
            for key, path in wiring.FILES.items()}}

    def test_fixed_invocation_does_not_adopt_neighbouring_current_pointer(self):
        old = self.release('old', 'a' * 40)
        old_authorization = self.authorization(old)
        new = self.release('new', 'b' * 40)
        current = self.root / 'current'
        current.symlink_to(new, target_is_directory=True)
        actual = wiring.read_runtime(old / 'Scripts/pulse-stats-cost-refresh.py', old_authorization)
        advertised = wiring.read_runtime(current / 'Scripts/pulse-stats-cost-refresh.py', old_authorization)
        self.assertEqual(actual['source_revision'], 'a' * 40)
        self.assertEqual(advertised['source_revision'], 'b' * 40)
        self.assertNotEqual(actual['component_sha256'], advertised['component_sha256'])
        self.assertTrue(actual['authorized_component_matches'])
        self.assertFalse(advertised['authorized_component_matches'])

    def test_consistent_manifest_cannot_authorize_a_stale_component(self):
        root = self.release('release', 'a' * 40)
        authorized = self.authorization(root)
        changed = b'changed without authorized source'
        path = root / wiring.FILES['stats_dashboard']
        path.write_bytes(changed)
        manifest = json.loads((root / '.stats-files.json').read_text())
        manifest[wiring.FILES['stats_dashboard']] = hashlib.sha256(changed).hexdigest()
        (root / '.stats-files.json').write_text(json.dumps(manifest))
        actual = wiring.read_runtime(root / 'Scripts/pulse-stats-cost-refresh.py', authorized)
        self.assertTrue(actual['installed_manifest_matches'])
        self.assertFalse(actual['authorized_component_matches'])

    def test_missing_runtime_component_is_not_a_success(self):
        root = self.release('release', 'a' * 40)
        authorized = self.authorization(root)
        (root / wiring.FILES['stats_dashboard']).unlink()
        with self.assertRaises(ValueError):
            wiring.read_runtime(root / 'Scripts/pulse-stats-cost-refresh.py', authorized)

    def test_complete_legacy_layout_is_reported_not_guessed(self):
        root = self.release('legacy', 'a' * 40)
        authorized = self.authorization(root)
        for key, path in wiring.LEGACY_FILES.items():
            destination = root / path
            destination.parent.mkdir(parents=True, exist_ok=True)
            source = root / wiring.FILES[key]
            destination.write_bytes(source.read_bytes())
            if destination != source:
                source.unlink()
        manifest = {path: hashlib.sha256((root / path).read_bytes()).hexdigest()
                    for path in wiring.LEGACY_FILES.values()}
        (root / '.stats-files.json').write_text(json.dumps(manifest))
        actual = wiring.read_runtime(root / 'Scripts/pulse-stats-cost-refresh.py', authorized)
        self.assertEqual(actual['runtime_layout'], 'legacy')
        self.assertTrue(actual['authorized_component_matches'])

    def test_authorized_hashes_come_from_exact_git_blobs(self):
        revision = subprocess.run(['git', 'rev-parse', 'HEAD'], capture_output=True, text=True,
                                  check=True).stdout.strip()
        hashes = wiring.authorized_component_hashes(revision)
        self.assertEqual(set(hashes['component_sha256']), set(wiring.FILES))
        blob = subprocess.run(['git', 'show', f'{revision}:{wiring.FILES["stats_dashboard"]}'],
                              capture_output=True, check=True).stdout
        self.assertEqual(hashes['component_sha256']['stats_dashboard'], hashlib.sha256(blob).hexdigest())

    def test_stale_loaded_arguments_fail_reconciliation(self):
        configured = ['/usr/bin/python3', '/releases/new/Scripts/pulse-stats-cost-refresh.py',
                      '--config', '/state/new-refresh.json']
        loaded = '''\
service = {
    arguments = {
        /usr/bin/python3
        /releases/old/Scripts/pulse-stats-cost-refresh.py
        --config
        /state/old-refresh.json
    }
}
'''
        result = wiring.reconcile_invocations(configured, loaded)
        self.assertFalse(result['matches_loaded'])
        self.assertEqual(result['loaded']['script'], '/releases/old/Scripts/pulse-stats-cost-refresh.py')

    def test_alternate_loaded_config_fails_reconciliation(self):
        configured = ['/usr/bin/python3', '/release/Scripts/pulse-stats-cost-refresh.py',
                      '--config', '/state/refresh.json']
        loaded = '''\
    arguments = {
        /usr/bin/python3
        /release/Scripts/pulse-stats-cost-refresh.py
        --config
        /state/alternate.json
    }
'''
        result = wiring.reconcile_invocations(configured, loaded)
        self.assertFalse(result['matches_loaded'])
        self.assertEqual(result['loaded']['config'], '/state/alternate.json')

    def test_registration_without_parseable_loaded_arguments_fails_closed(self):
        result = wiring.reconcile_invocations(
            ['/usr/bin/python3', '/release/Scripts/pulse-stats-cost-refresh.py', '--config', '/state/refresh.json'],
            'service = {\n  state = running\n}', 0)
        self.assertFalse(result['matches_loaded'])
        self.assertEqual(result['loaded_error'], 'loaded job arguments unavailable')

    def test_wiring_readback_uses_the_os_account_not_spoofed_home(self):
        with patch.dict(os.environ, {'HOME': str(self.root / 'spoofed')}, clear=False):
            self.assertEqual(wiring.actual_account_home(), Path(pwd.getpwuid(os.getuid()).pw_dir))


if __name__ == '__main__':
    unittest.main()
