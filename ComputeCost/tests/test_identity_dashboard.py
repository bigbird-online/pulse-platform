"""Synthetic three-entry fixture through original query, publisher and exporter."""
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
import json
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'Stats/exporter'))
from pulse_compute_cost.ledger import UsageLedger
from pulse_compute_cost.receipt_sources import usage_receipt, actor_receipt
from pulse_compute_cost.receipts import digest
from pulse_compute_cost.stats_dashboard import build_dashboard, publish_dashboard, DEFAULT_FACTS
from pulse_stats.cost_dashboard import load_cost_dashboard, _validate

AT = '2026-09-15T01:00:00Z'
NOW = datetime(2026, 9, 15, 4, tzinfo=timezone.utc)
CATALOG = {'retrieved_at': AT, 'catalog': {'data': [{'id': 'openai/test', 'pricing': {
    'prompt': '1', 'completion': '1', 'input_cache_read': '0.1', 'input_cache_write': '2'}}]}}


class IdentityDashboardTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.ledger = UsageLedger(self.root / 'ledger', clock=lambda: NOW)
        self.facts = json.loads(DEFAULT_FACTS.read_text())
        rows = []
        for index, (agent, node, runtime, entrypoint) in enumerate([
            ('pulse', 'ownermac', 'codex', 'codex_app'),
            ('pulse', 'tcloud', 'pi', 'feishu'),
            ('aki', 'tcloud', 'pi', 'feishu'),
        ]):
            usage = {'input_tokens': 120, 'cached_input_tokens': 20,
                     'cache_write_input_tokens': 0, 'output_tokens': 10} if runtime == 'codex' else {
                     'input': 100, 'cacheRead': 20, 'cacheWrite': 0, 'output': 10}
            row = usage_receipt(runtime, ['identity-fixture', index], usage, at=AT, observed=AT,
                model='openai/test', mapping='codex_cli.v1' if runtime == 'codex' else 'pi_native.v1',
                common={'wi': None if index == 0 else f'wi_20260915_{index:03d}',
                        'executor': None, 'responsible': None, 'delegator': None})
            rows.extend([row, actor_receipt(row, node=node, executor=agent,
                                           entrypoint=entrypoint, observed=AT)])
        self.ledger.ingest_receipts(rows)

    def document(self, extended=True):
        return build_dashboard(self.ledger, subscriptions=self.facts, source_revision='a'*40,
                               now=NOW, catalog_snapshot=CATALOG, identity_dimensions=extended)

    def test_all_six_views_per_period_preserve_one_amount_and_distinct_missing_axes(self):
        document = self.document()
        self.assertEqual(document['status'], 'available')
        self.assertEqual(len(document['views']), 18)
        self.assertEqual(len({v['watermark'] for v in document['views']}), 1)
        for view in document['views']:
            self.assertEqual(view['summary']['cost']['partial_machine_cash_usd'], '336')
            usage = view['summary']['usage']
            self.assertEqual(usage['agent_unassigned_event_count'], 0)
            self.assertEqual(usage['agent_unassigned_priced_usd'], '0')
            self.assertEqual(usage['wi_unassigned_event_count'], 1)
            self.assertEqual(usage['wi_unassigned_priced_usd'], '112')
            cost = view['summary']['cost']
            self.assertEqual((cost['priced_event_count'], cost['priced_machine_cash_usd']), (3, '336'))
        by_dimension = {v['dimension']: v for v in document['views'] if v['period'] == 'today'}
        self.assertEqual({g['key']['agent']: g['cost']['partial_machine_cash_usd']
                          for g in by_dimension['agent']['groups']}, {'pulse': '224', 'aki': '112'})
        self.assertEqual({g['key']['node']: g['cost']['partial_machine_cash_usd']
                          for g in by_dimension['node']['groups']}, {'ownermac': '112', 'tcloud': '224'})
        publish_dashboard(self.root / 'dashboard.json', document)
        self.assertEqual(load_cost_dashboard(self.root / 'dashboard.json', now=NOW), document)

    def test_legacy_matrix_remains_readable_and_shared_queries_are_identical(self):
        extended, legacy = self.document(), self.document(False)
        self.assertEqual(len(legacy['views']), 9)
        _validate(legacy)
        new = {(v['period'], v['dimension']): v for v in extended['views']}
        for view in legacy['views']:
            self.assertEqual(new[(view['period'], view['dimension'])], view)

    def test_incomplete_extended_matrix_and_invalid_missing_counts_are_not_accepted(self):
        document = self.document()
        document['views'].pop()
        document['sha256'] = digest({k:v for k,v in document.items() if k != 'sha256'})
        with self.assertRaises(ValueError): _validate(document)
        document = self.document()
        document['views'][0]['summary']['usage']['agent_unassigned_event_count'] = 4
        document['sha256'] = digest({k:v for k,v in document.items() if k != 'sha256'})
        with self.assertRaises(ValueError): _validate(document)

    def test_priced_total_must_match_the_same_query_rows(self):
        for field, value in (('priced_machine_cash_usd', '335'), ('priced_event_count', 2),
                             ('priced_machine_cash_usd', None)):
            with self.subTest(field=field, value=value):
                document = self.document()
                document['views'][0]['summary']['cost'][field] = value
                document['sha256'] = digest({k:v for k,v in document.items() if k != 'sha256'})
                with self.assertRaises(ValueError): _validate(document)
        document = self.document()
        for view in document['views']:
            for cost in [view['summary']['cost']] + [g['cost'] for g in view['groups']]:
                del cost['priced_event_count'], cost['priced_machine_cash_usd']
        document['sha256'] = digest({k:v for k,v in document.items() if k != 'sha256'})
        _validate(document)


if __name__ == '__main__': unittest.main()
