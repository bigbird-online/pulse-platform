"""Saved-price parity, with the real CLI and publisher on isolated fixture data."""
from contextlib import redirect_stdout, redirect_stderr
from copy import deepcopy
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from pulse_compute_cost import catalog_source, cli, stats_dashboard
from pulse_compute_cost.ledger import UsageLedger
from pulse_compute_cost.receipts import ReceiptError
from test_collaboration_receipts import AT, CATALOG, WI, usage


class CatalogSourceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.state = self.root / 'state'
        self.registry = self.state / 'compute-cost/sources.json'
        self.catalog = self.state / 'stats/openrouter-catalog.json'
        self.ledger = UsageLedger(self.root / 'ledger')
        self.ledger.ingest_receipts([usage()])  # No inventory: known cost is partial.
        self.data = {'schema': 'computecost_source_registry/v1',
                     'ledger_root': str(self.ledger.root), 'sources': []}
        self.save(self.registry, self.data)
        self.snapshot = {'retrieved_at': AT, 'catalog': CATALOG}
        self.save(self.catalog, self.snapshot)
        self.addCleanup(patch.stopall)
        patch('pulse_compute_cost.catalog_source.managed_state_root', return_value=self.state).start()
        patch.dict('os.environ', {'PULSE_COMPUTECOST_SOURCES': ''}).start()
        self.collect = patch('pulse_compute_cost.cli.collect_registered_sources',
                             side_effect=AssertionError('historical query must not collect')).start()

    @staticmethod
    def save(path, data):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data)); path.chmod(0o600)

    def command(self, extra=(), registry=None, env=False):
        args = [WI, '--watermark', '1']
        if env:
            args += list(extra)
        else:
            args += ['--source-registry', str(registry or self.registry), *extra]
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = cli.main(args)
        return code, json.loads(out.getvalue()) if out.getvalue() else None, err.getvalue()

    def test_default_wi_reuses_existing_stats_prices_without_changing_evidence(self):
        before = {p: p.read_bytes() for p in (self.registry, self.catalog)}
        ledger_before = self.ledger.receipt_snapshot()['sha256']
        code, default, _ = self.command()
        _, explicit, _ = self.command(['--catalog-snapshot', str(self.catalog)])
        self.assertEqual(code, 0)
        self.assertEqual(default, explicit)
        self.assertEqual(default['summary']['cost']['partial_machine_cash_usd'], '100')
        self.assertIsNone(default['summary']['cost']['machine_cash_usd'])
        self.assertIsNone(default['summary']['roi']['ratio'])
        self.assertEqual(default['catalog_observed_at'], AT)
        self.assertEqual(default['summary']['cost']['unpriced'], [])
        self.assertEqual(self.ledger.receipt_snapshot()['sha256'], ledger_before)
        for p, content in before.items(): self.assertEqual(p.read_bytes(), content)
        self.collect.assert_not_called()

    def test_environment_configured_alias_needs_no_extra_price_argument(self):
        with patch.dict('os.environ', {'PULSE_COMPUTECOST_SOURCES': str(self.registry)}):
            code, result, _ = self.command(env=True)
        self.assertEqual(code, 0)
        self.assertEqual(result['summary']['cost']['partial_machine_cash_usd'], '100')

    def test_foreign_registry_and_ledger_only_reads_do_not_borrow_managed_prices(self):
        foreign = self.root / 'other/sources.json'; self.save(foreign, self.data)
        code, result, _ = self.command(registry=foreign)
        self.assertEqual(code, 0)
        self.assertIsNone(result['catalog_sha256'])
        self.assertEqual(len(result['summary']['cost']['unpriced']), 1)
        self.assertIsNone(catalog_source.resolve_catalog_path())

    def test_explicit_then_registry_precedence(self):
        alternate = self.root / 'alternate.json'
        snapshot = deepcopy(self.snapshot); snapshot['catalog']['data'][0]['pricing']['prompt'] = '2'
        self.save(alternate, snapshot)
        self.save(self.registry, {**self.data, 'catalog_snapshot': str(alternate)})
        self.assertEqual(self.command()[1]['summary']['cost']['partial_machine_cash_usd'], '200')
        result = self.command(['--catalog-snapshot', str(self.catalog)])[1]
        self.assertEqual(result['summary']['cost']['partial_machine_cash_usd'], '100')

    def test_missing_explicit_or_registered_price_is_unknown_not_fallback(self):
        absent = self.root / 'absent.json'
        code, result, err = self.command(['--catalog-snapshot', str(absent)])
        self.assertEqual(code, 0)
        self.assertIsNone(result['summary']['cost']['partial_machine_cash_usd'])
        self.assertIn('catalog_snapshot_unavailable', err)
        self.save(self.registry, {**self.data, 'catalog_snapshot': str(absent)})
        code, result, err = self.command()
        self.assertEqual(code, 0)
        self.assertIsNone(result['summary']['cost']['partial_machine_cash_usd'])
        self.assertIn('catalog_snapshot_unavailable', err)
        self.collect.assert_not_called()

    def test_invalid_or_linked_default_is_not_silently_unpriced(self):
        for invalid in ([], {'catalog': {}}, {**self.snapshot, 'retrieved_at': 1},
                        {**self.snapshot, 'retrieved_at': 'not-a-date'}):
            self.save(self.catalog, invalid)
            code, result, err = self.command()
            self.assertEqual(code, 0)
            self.assertIsNone(result['summary']['cost']['partial_machine_cash_usd'])
            self.assertIn('catalog_snapshot_unavailable', err)
        self.catalog.unlink(); target = self.root / 'other.json'; self.save(target, self.snapshot)
        self.catalog.symlink_to(target)
        code, result, err = self.command()
        self.assertEqual(code, 0)
        self.assertIsNone(result['summary']['cost']['partial_machine_cash_usd'])
        self.assertIn('catalog_snapshot_unavailable', err)

    def test_missing_default_retains_unknown_not_zero(self):
        self.catalog.unlink()
        code, result, _ = self.command()
        self.assertEqual(code, 0)
        self.assertIsNone(result['summary']['cost']['partial_machine_cash_usd'])
        self.assertEqual(len(result['summary']['cost']['unpriced']), 1)

    def test_no_model_price_does_not_disappear_after_source_resolution(self):
        self.save(self.catalog, {'retrieved_at': AT, 'catalog': {'data': []}})
        code, result, _ = self.command()
        self.assertEqual(code, 0)
        self.assertEqual(len(result['summary']['cost']['unpriced']), 1)
        self.assertEqual(result['summary']['usage']['event_count'], 1)

    def test_empty_declared_path_is_not_permission_to_fallback(self):
        for value in ('', None, False, 123):
            with self.subTest(value=value), self.assertRaises(ReceiptError):
                catalog_source.resolve_catalog_path(registry={'catalog_snapshot': value},
                                                    registry_path=self.registry)

    def test_publisher_uses_the_same_default_and_timestamp(self):
        captured = []
        def build(ledger, **kwargs):
            captured.append(kwargs['catalog_snapshot'])
            return {'status': 'unavailable', 'generated_at': AT, 'sha256': 'fixture'}
        args = ['--source-registry', str(self.registry), '--source-revision', 'a'*40,
                '--output', str(self.root / 'dashboard.json')]
        with patch.object(stats_dashboard, 'build_dashboard', side_effect=build), \
             patch.object(stats_dashboard, 'publish_dashboard'), redirect_stdout(io.StringIO()):
            self.assertEqual(stats_dashboard.main(args), 0)
        self.assertEqual(captured, [self.snapshot])

    def test_invalid_catalog_does_not_block_publisher_collection(self):
        self.save(self.catalog, {'catalog': {}})
        with patch('pulse_compute_cost.receipt_sources.collect_registered_sources') as collect, \
             patch.object(stats_dashboard, 'build_dashboard', return_value={'status':'unavailable','generated_at':AT,'sha256':'fixture'}), \
             patch.object(stats_dashboard, 'publish_dashboard'), redirect_stdout(io.StringIO()), \
             redirect_stderr(io.StringIO()):
            code = stats_dashboard.main(['--source-registry', str(self.registry), '--collect',
                    '--source-revision', 'a'*40, '--output', str(self.root / 'dashboard.json')])
        self.assertEqual(code, 0)
        collect.assert_called_once()


if __name__ == '__main__':
    unittest.main()
