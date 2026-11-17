"""Actual subprocess kill/retry tests for the native Pi publisher, no SCL mutation."""
from dataclasses import asdict
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest

from pulse_compute_cost.pi_capture import capture_pi_session_snapshot
from pulse_compute_cost.pi_usage import PiUsageBinding, PiUsageError

CHILD = r'''
import json, sys
from pathlib import Path
import pulse_compute_cost.pi_capture as capture
from pulse_compute_cost.pi_usage import PiUsageBinding
config = json.loads(Path(sys.argv[1]).read_text())
mode = sys.argv[2]
if mode == 'sampling':
    original = capture.build_pi_session_snapshot
    def blocked(*args, **kwargs):
        print('LOCK_HELD', flush=True)
        sys.stdin.readline()
        return original(*args, **kwargs)
    capture.build_pi_session_snapshot = blocked
elif mode == 'pre_publish':
    original = capture.os.replace
    def blocked(*args, **kwargs):
        print('LOCK_HELD', flush=True)
        sys.stdin.readline()
        return original(*args, **kwargs)
    capture.os.replace = blocked
result = capture.capture_pi_session_snapshot(
    config['source'], PiUsageBinding(**config['binding']),
    output_path=config['target'], lifecycle_state='sealed', measurement_role='final',
    baseline_snapshot=config['baseline'])
print(json.dumps(result), flush=True)
'''


class NativePiCaptureTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.binding = PiUsageBinding(
            work_item_id='wi_20260820_568', phase='computecost_program', epoch=12,
            procurement_source='chatgpt_subscription', agent_id='pulse',
            execution_surface='surface_v1_local_macos',
            authority_ref='session_v1_' + 'a' * 64, runtime_binding_sha256='b' * 64,
        )
        self.source = self.root / 'source.jsonl'
        header = {'type': 'session', 'id': 'private-fixture', 'version': 3,
                  'timestamp': '2026-09-11T00:00:00Z', 'cwd': '/private/fixture'}
        self.source.write_text(json.dumps(header) + '\n')
        name = f'{self.binding.work_item_id}-{self.binding.runtime_binding_sha256}'
        self.baseline = capture_pi_session_snapshot(
            self.source, self.binding, output_path=self.root / f'{name}-baseline.json',
            lifecycle_state='open', measurement_role='baseline')['snapshot']
        row = {'type': 'message', 'id': 'private-final', 'timestamp': '2026-09-11T00:00:01Z',
               'message': {'role': 'assistant', 'provider': 'openai-codex', 'model': 'gpt-fixture',
                           'usage': {'input': 100, 'cacheRead': 50, 'cacheWrite': 0,
                                     'output': 5, 'totalTokens': 155}}}
        with self.source.open('a') as stream:
            stream.write(json.dumps(row) + '\n')
        self.target = self.root / f'{name}-sealed.json'
        self.config = self.root / 'config.json'
        self.config.write_text(json.dumps({'source': str(self.source), 'target': str(self.target),
                                           'binding': asdict(self.binding), 'baseline': self.baseline}))

    def child(self, mode='normal'):
        process = subprocess.Popen([sys.executable, '-u', '-c', CHILD, str(self.config), mode],
                                   stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE, text=True)
        def cleanup():
            if process.poll() is None:
                process.kill()
            process.communicate(timeout=10)
        self.addCleanup(cleanup)
        return process

    def result(self, process):
        out, err = process.communicate(timeout=10)
        self.assertEqual(process.returncode, 0, err)
        return json.loads(out)

    def capture(self):
        return capture_pi_session_snapshot(
            self.source, self.binding, output_path=self.target, lifecycle_state='sealed',
            measurement_role='final', baseline_snapshot=self.baseline)

    def test_killed_sampler_releases_lock_without_stealing_live_owner(self):
        holder = self.child('sampling')
        self.assertEqual(holder.stdout.readline().strip(), 'LOCK_HELD')
        contender = self.child()
        time.sleep(0.2)
        self.assertIsNone(contender.poll(), 'a live owner must not be bypassed')
        self.assertFalse(self.target.exists())
        holder.kill()
        holder.wait(timeout=10)
        first = self.result(contender)
        self.assertFalse(first['idempotent_replay'])
        for _ in range(2):
            replay = self.result(self.child())
            self.assertTrue(replay['idempotent_replay'])
            self.assertEqual(replay['snapshot']['snapshot_sha256'], first['snapshot']['snapshot_sha256'])
        self.assertEqual(first['snapshot']['summary']['totals']['total_tokens'], 155)
        self.assertEqual(self.target.stat().st_mode & 0o777, 0o600)

    def test_killed_writer_before_publish_is_recoverable(self):
        holder = self.child('pre_publish')
        self.assertEqual(holder.stdout.readline().strip(), 'LOCK_HELD')
        self.assertFalse(self.target.exists())
        holder.kill()
        holder.wait(timeout=10)
        result = self.result(self.child())
        self.assertFalse(result['idempotent_replay'])
        self.assertEqual(result['snapshot']['summary']['totals']['total_tokens'], 155)

    def test_two_real_processes_publish_one_immutable_winner(self):
        left, right = self.child(), self.child()
        results = [self.result(left), self.result(right)]
        self.assertEqual(sum(result['idempotent_replay'] for result in results), 1)
        self.assertEqual(results[0]['snapshot']['snapshot_sha256'], results[1]['snapshot']['snapshot_sha256'])
        lock = self.target.with_name(self.target.name + '.capture-lock')
        self.assertTrue(lock.is_file())  # Its existence is not a live lock.
        self.assertTrue(self.capture()['idempotent_replay'])

    def test_replay_excludes_suffix_and_rejects_changed_prefix(self):
        captured = self.capture()
        original = self.source.read_bytes()
        self.source.write_bytes(original + b'post-seal suffix\n')
        self.assertEqual(self.capture()['snapshot'], captured['snapshot'])
        self.source.write_bytes(b'X' + original[1:])
        with self.assertRaises(PiUsageError):
            self.capture()

    def test_symlink_artifact_is_rejected_without_modifying_target(self):
        other = self.root / 'other.json'
        other.write_text('untouched')
        self.target.symlink_to(other)
        with self.assertRaises(PiUsageError):
            self.capture()
        self.assertEqual(other.read_text(), 'untouched')

    def test_symlink_lock_is_rejected(self):
        other = self.root / 'other-lock'
        other.write_text('untouched')
        self.target.with_name(self.target.name + '.capture-lock').symlink_to(other)
        with self.assertRaises(PiUsageError):
            self.capture()
        self.assertEqual(other.read_text(), 'untouched')
