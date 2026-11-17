"""Recollection reports unresolved coverage without manufacturing new heads."""
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from pulse_compute_cost.ledger import UsageLedger
from pulse_compute_cost.receipt_sources import REGISTRY_SCHEMA, collect_registered_sources
from pulse_compute_cost.receipts import Receipt, current_receipts, reference

AT = '2026-10-05T12:00:00Z'


class CoverageRevisionConflictTests(unittest.TestCase):
    def setup_case(self, root):
        source = root / 'sessions'; source.mkdir(mode=0o700)
        native = source / 'session.jsonl'
        self.message = {'type': 'message', 'id': 'first-message', 'timestamp': AT, 'message': {
            'role': 'assistant', 'model': 'openai/synthetic', 'provider': 'openai',
            'usage': {'input': 100, 'cacheRead': 20, 'cacheWrite': 0, 'output': 10, 'totalTokens': 130}}}
        native.write_text('\n'.join(json.dumps(r) for r in [
            {'type': 'session', 'id': 'synthetic', 'timestamp': AT, 'version': 3}, self.message]) + '\n')
        ledger = UsageLedger(root / 'ledger')
        registry = {'schema': REGISTRY_SCHEMA, 'sources': [
            {'id': 'isolated-pi', 'kind': 'pi', 'path': str(source)}]}
        source_ref = reference('source', 'isolated-pi')
        entity = reference('coverage', [source_ref, 'usage', 'global'])
        def coverage(watermark, status='partial'):
            return Receipt.make('coverage', [source_ref, 'usage', 'global'], {
                'domain': 'usage', 'wi': None, 'source_ref': source_ref, 'status': status,
                'expected': None, 'observed': None, 'watermark': watermark,
                'reason': 'synthetic-source-gap', 'source_detail': None},
                occurred_at=None, observed_at=AT, producer='registered_collector.v1')
        return native, ledger, registry, source_ref, entity, coverage

    def test_conflict_reports_fresh_state_and_retains_exact_historical_heads(self):
        with TemporaryDirectory() as directory:
            native, ledger, registry, source_ref, entity, coverage = self.setup_case(Path(directory))
            old = [coverage('old-a'), coverage('old-b', 'unknown')]
            ledger.ingest_receipts(old); before = ledger.receipt_snapshot()
            first = collect_registered_sources(registry, ledger)
            native.write_bytes(native.read_bytes() + b'\n')
            second = collect_registered_sources(registry, ledger)
            for result in (first, second):
                self.assertEqual(len(result['coverage_revision_conflicts']), 1)
                diagnostic = result['coverage_revision_conflicts'][0]
                self.assertEqual(diagnostic['entity_ref'], entity)
                self.assertEqual(diagnostic['source_ref'], source_ref)
                self.assertEqual(diagnostic['head_fact_refs'], sorted(r.fact_ref for r in old))
                self.assertEqual(diagnostic['reason'], 'coverage_revision_conflict')
                self.assertTrue(diagnostic['recovery_required'])
                summary = result['sources'][0]
                self.assertTrue(summary['coverage_recovery_required'])
                self.assertEqual(summary['coverage_revision_conflicts'], [diagnostic])
                self.assertEqual(diagnostic['fresh_snapshot']['status'], summary['status'])
                self.assertEqual(diagnostic['fresh_snapshot']['reason'], summary['reason'])
                self.assertEqual(diagnostic['fresh_snapshot']['source_detail'], summary['source_detail'])
            self.assertNotEqual(first['coverage_revision_conflicts'][0]['fresh_snapshot']['watermark'],
                                second['coverage_revision_conflicts'][0]['fresh_snapshot']['watermark'])
            after = ledger.receipt_snapshot()
            self.assertEqual(after['facts'][:len(before['facts'])], before['facts'])
            self.assertEqual(ledger.receipt_snapshot(watermark=before['watermark'])['sha256'], before['sha256'])
            self.assertEqual({r.fact_ref for r in current_receipts(after['facts'])[1][entity]},
                             {r.fact_ref for r in old})
            self.assertEqual(collect_registered_sources(registry, ledger)['inserted'], 0)

    def test_new_usage_is_ingested_while_coverage_conflict_stays_unresolved(self):
        with TemporaryDirectory() as directory:
            native, ledger, registry, _, entity, coverage = self.setup_case(Path(directory))
            ledger.ingest_receipts([coverage('old-a'), coverage('old-b', 'unknown')])
            collect_registered_sources(registry, ledger)
            before = ledger.receipt_snapshot()
            with native.open('a') as stream:
                stream.write(json.dumps({**self.message, 'id': 'second-message'}) + '\n')
            result = collect_registered_sources(registry, ledger)
            after = ledger.receipt_snapshot()
            old_usage = [r for r in before['facts'] if r.kind == 'usage']
            new_usage = [r for r in after['facts'] if r.kind == 'usage']
            self.assertEqual(len(old_usage), 1); self.assertEqual(len(new_usage), 2)
            self.assertEqual(new_usage[:1], old_usage)
            self.assertEqual(new_usage[1].payload['tokens'], old_usage[0].payload['tokens'])
            self.assertEqual(len(current_receipts(after['facts'])[1][entity]), 2)
            self.assertTrue(result['sources'][0]['coverage_recovery_required'])
            self.assertEqual(ledger.receipt_snapshot(watermark=before['watermark'])['sha256'], before['sha256'])

    def test_single_prior_revision_and_unchanged_recollection_stay_normal(self):
        with TemporaryDirectory() as directory:
            _, ledger, registry, _, entity, coverage = self.setup_case(Path(directory))
            old = coverage('old-snapshot'); ledger.ingest_receipts([old])
            result = collect_registered_sources(registry, ledger)
            active, conflicts = current_receipts(ledger.receipt_snapshot()['facts'])
            self.assertNotIn(entity, conflicts)
            heads = [r for r in active if r.entity_ref == entity]
            self.assertEqual(len(heads), 1); self.assertEqual(heads[0].supersedes, old.fact_ref)
            self.assertEqual(result['coverage_revision_conflicts'], [])
            self.assertNotIn('coverage_recovery_required', result['sources'][0])
            before = ledger.receipt_snapshot()
            self.assertEqual(collect_registered_sources(registry, ledger)['inserted'], 0)
            self.assertEqual(ledger.receipt_snapshot()['sha256'], before['sha256'])


if __name__ == '__main__':
    unittest.main()
