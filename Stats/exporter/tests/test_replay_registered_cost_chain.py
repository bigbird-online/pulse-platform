"""Regression coverage for the read-only production-ledger mixed replay."""
from __future__ import annotations

import importlib.util
import os
import subprocess
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[3]
sys.path[:0] = [str(ROOT / 'ComputeCost'), str(ROOT / 'Stats/exporter')]
spec = importlib.util.spec_from_file_location(
    'replay_registered_cost_chain', ROOT / 'Stats/exporter/tests/replay_registered_cost_chain.py')
replay = importlib.util.module_from_spec(spec)
spec.loader.exec_module(replay)

from pulse_compute_cost.ledger import UsageLedger
from pulse_compute_cost.receipts import canonical, current_receipts
from pulse_compute_cost.receipt_sources import collect_registered_sources, usage_receipt


class ReadonlyLedgerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / 'copied-ledger'
        self.ledger = UsageLedger(self.root)
        self.ledger.ingest_receipts([usage_receipt(
            'cc', ['copied', 'one'],
            {'input_tokens': 10, 'output_tokens': 2, 'cache_read_input_tokens': 0,
             'cache_creation_input_tokens': 0},
            at='2026-09-14T00:00:00+00:00', observed='2026-09-14T00:00:01+00:00',
            model='openai/gpt-5', final=True)])

    def tree(self):
        entries = [self.root, *self.root.rglob('*')]
        return {path.relative_to(self.root): (path.stat().st_mode & 0o777,
                path.read_bytes() if path.is_file() else None) for path in entries}

    def test_snapshot_uses_existing_shared_read_lock_without_mutating_fixture(self):
        before = self.tree()
        snapshot = replay.readonly_receipt_snapshot(self.root)
        self.assertEqual(snapshot['watermark'], 1)
        self.assertEqual(len(snapshot['facts']), 1)
        self.assertEqual(self.tree(), before)

    def test_missing_or_symlink_lock_is_rejected_without_creating_a_lock(self):
        absent = Path(self.tmp.name) / 'absent-ledger'
        with self.assertRaisesRegex(ValueError, 'existing ledger directory'):
            replay.readonly_receipt_snapshot(absent)
        self.assertFalse(absent.exists())
        missing = Path(self.tmp.name) / 'missing-lock-ledger'
        missing.mkdir(mode=0o700)
        with self.assertRaisesRegex(ValueError, 'existing non-symlink lock'):
            replay.readonly_receipt_snapshot(missing)
        self.assertFalse((missing / '.usage-ledger.lock').exists())
        link = Path(self.tmp.name) / 'symlink-lock-ledger'
        link.mkdir(mode=0o700)
        (link / '.usage-ledger.lock').symlink_to(self.root / '.usage-ledger.lock')
        with self.assertRaisesRegex(ValueError, 'existing non-symlink lock'):
            replay.readonly_receipt_snapshot(link)
        self.assertTrue((link / '.usage-ledger.lock').is_symlink())

    def test_missing_safe_reader_is_rejected_before_opening_any_lock(self):
        before = self.tree()
        with patch.object(replay.UsageLedger, '_receipt_batches', None):
            with self.assertRaisesRegex(ValueError, 'API is unavailable'):
                replay.readonly_receipt_snapshot(self.root)
        self.assertEqual(self.tree(), before)

    def test_synthetic_collector_failure_uses_current_coverage_and_preserves_old_watermark(self):
        transport = Path(self.tmp.name) / 'peer.jsonl'
        fact = usage_receipt('cc', ['synthetic', 'one'],
            {'input_tokens': 10, 'output_tokens': 2, 'cache_read_input_tokens': 0,
             'cache_creation_input_tokens': 0}, at='2026-09-14T00:00:00+00:00',
            observed='2026-09-14T00:00:01+00:00', model='openai/gpt-5', final=True)
        transport.write_bytes(canonical(fact.to_dict()) + b'\n')
        transport.chmod(0o600)
        target = UsageLedger(Path(self.tmp.name) / 'target')
        registry = {'schema': 'computecost_source_registry/v1', 'sources': [
            {'id': 'peer', 'kind': 'receipts', 'path': str(transport)}]}
        complete = collect_registered_sources(registry, target)
        source_ref = complete['sources'][0]['source_ref']
        frozen = target.receipt_snapshot()
        transport.unlink()
        failed = collect_registered_sources(registry, target)
        self.assertEqual(failed['sources'][0]['status'], 'unreachable')
        bad = replay.active_coverage(target.receipt_snapshot()['facts'], source_ref)
        self.assertEqual(bad.payload['status'], 'unreachable')
        self.assertEqual(target.receipt_snapshot(watermark=frozen['watermark'])['sha256'], frozen['sha256'])
        transport.write_bytes(canonical(fact.to_dict()) + b'\n')
        transport.chmod(0o600)
        collect_registered_sources(registry, target)
        good = replay.active_coverage(target.receipt_snapshot()['facts'], source_ref)
        self.assertEqual(good.payload['status'], 'excluded')
        self.assertEqual(good.supersedes, bad.fact_ref)
        active, conflicts = current_receipts(target.receipt_snapshot()['facts'])
        self.assertEqual(conflicts, {})
        self.assertIn(good, active)

    def test_consumer_coverage_transition_never_claims_loss_or_recovery_when_already_unknown(self):
        def document(unknown):
            return {'views': [{'summary': {'cost': {'reasons': ['source_coverage_unknown'] if unknown else []}}}
                              for _ in range(9)]}
        already_unknown = replay.consumer_coverage_transition(document(True), document(True))
        self.assertEqual(already_unknown['failure'], 'not_observable_already_unknown')
        degraded = replay.consumer_coverage_transition(document(False), document(True))
        self.assertEqual(degraded['failure'], 'degraded')
        with self.assertRaises(AssertionError):
            replay.consumer_coverage_transition(document(True), document(False))

    def test_cli_keeps_the_original_two_required_arguments_and_ignores_spoofed_home(self):
        result = subprocess.run([sys.executable, str(ROOT / 'Stats/exporter/tests/replay_registered_cost_chain.py'),
                                 '--help'], text=True, capture_output=True, check=True)
        self.assertIn('--output', result.stdout)
        self.assertIn('--source-revision', result.stdout)
        self.assertNotIn('--source-copies', result.stdout)
        with patch.dict(os.environ, {'HOME': str(Path(self.tmp.name) / 'spoofed')}, clear=False):
            self.assertEqual(replay.actual_account_home(), Path(__import__('pwd').getpwuid(os.getuid()).pw_dir))


if __name__ == '__main__':
    unittest.main()
