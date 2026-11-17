"""WI300 real-query projection tests. All ledgers below are synthetic/private."""
from datetime import datetime, timezone, timedelta
from decimal import Decimal
from pathlib import Path
import json
import tempfile
import unittest
from unittest.mock import patch
from pulse_compute_cost.ledger import UsageLedger
from pulse_compute_cost.receipt_sources import usage_receipt
from pulse_compute_cost.collaboration_query import CollaborationQuery, classify_cost_reasons, query_collaboration
from pulse_compute_cost.stats_dashboard import build_dashboard, publish_dashboard, project_openrouter

NOW = datetime(2026, 9, 13, 8, 0, tzinfo=timezone.utc)
REV = 'c' * 40
CATALOG = {'retrieved_at': NOW.isoformat(), 'catalog': {'data': [{
    'id': 'openai/gpt-4o', 'canonical_slug': 'openai/gpt-4o',
    'pricing': {'prompt': '0.000002', 'completion': '0.00001',
                'input_cache_read': '0.0000002', 'input_cache_write': '0.000002'}}]}}
SUBSCRIPTIONS = {'schema_version': 'pulse_stats_procurement_facts/v1', 'subscriptions': [
    {'id': 'chatgpt_pro_max', 'name': 'ChatGPT Pro Max', 'status': 'active_user_confirmed',
     'monthly_amount_usd': '200', 'billing_day': 14, 'confirmed_on': '2026-09-13'},
    {'id': 'claude_code', 'name': 'Claude Code', 'status': 'stopped_user_confirmed',
     'monthly_amount_usd': None, 'billing_day': None, 'confirmed_on': '2026-09-13'}]}

class StatsDashboardTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.ledger = UsageLedger(Path(self.temp.name) / 'ledger', clock=lambda: NOW)
        self.ledger.ingest_receipts([usage_receipt('pi', 'synthetic-request', {
            'input': 100, 'output': 10, 'cacheRead': 30, 'cacheWrite': 0},
            at=NOW.isoformat(), observed=NOW.isoformat(), model='openai/gpt-4o',
            common={'wi': 'wi_20260825_300', 'executor': 'pulse', 'responsible': 'pulse', 'delegator': None})])

    def build(self, **kwargs):
        return build_dashboard(self.ledger, catalog_snapshot=CATALOG,
            subscriptions=SUBSCRIPTIONS, source_revision=REV, now=NOW, **kwargs)

    def test_compact_encoding_mismatch_preserves_current_publication(self):
        output = Path(self.temp.name) / 'publication' / 'cost.json'
        original = self.build()
        publish_dashboard(output, original)
        before = output.read_bytes()
        mismatched = {**original, 'views': []}
        with patch('pulse_compute_cost.stats_dashboard.dashboard_bytes', return_value=json.dumps(mismatched).encode()):
            with self.assertRaisesRegex(ValueError, 'failed its roundtrip'):
                publish_dashboard(output, original, allow_row_contexts=True)
        self.assertEqual(output.read_bytes(), before)

    def test_projection_is_exact_query_amount_not_subscription_or_group_sum(self):
        result = self.build()
        self.assertEqual(len(result['views']), 9)
        view = next(x for x in result['views'] if x['period'] == 'month' and x['dimension'] == 'agent')
        direct = query_collaboration(self.ledger, CollaborationQuery(from_date='2026-09-01',
            to_date='2026-09-14', scope='activity', group_by=('agent',), watermark=1),
            catalog=CATALOG['catalog'], catalog_observed_at=CATALOG['retrieved_at'])
        published = dict(view['summary']['cost'])
        self.assertEqual(classify_cost_reasons(direct['summary']['cost']['reasons']),
                         {k: published.pop(k) for k in ('anomalies', 'proof_gaps')})
        self.assertEqual(published, direct['summary']['cost'])
        self.assertEqual(view['report_sha256'], direct['report_sha256'])
        self.assertIsNone(view['summary']['cost']['machine_cash_usd'])
        self.assertIsNotNone(view['summary']['cost']['partial_machine_cash_usd'])
        self.assertEqual(result['subscriptions'][0]['monthly_amount_usd'], '200')
        self.assertEqual(result['subscriptions'][0]['billing_day'], 14)
        self.assertNotIn('procurement_total_usd', result)
        self.assertNotIn('settled_cost_usd', json.dumps(result))

    def test_reason_classes_separate_proof_gaps_from_anomalies(self):
        self.assertEqual(classify_cost_reasons(['source_coverage_unknown', 'attribution_unknown',
            'execution_link_unknown', 'provisional_usage', 'join_conflict', 'future_code']),
            {'anomalies': ['attribution_unknown', 'future_code', 'join_conflict'],
             'proof_gaps': ['execution_link_unknown', 'provisional_usage', 'source_coverage_unknown']})
        result = self.build(activity_table=True, activity_only=True)
        costs = [c for v in result['views'] for c in [v['summary']['cost']]
                 + [g['cost'] for g in v['groups'] + v['outcome_only_groups']]]
        self.assertTrue(costs)
        for cost in costs:
            self.assertEqual(sorted(cost['anomalies'] + cost['proof_gaps']), cost['reasons'])
        # A native row without coverage receipts has only proof gaps; its amount is unchanged.
        group = result['views'][0]['groups'][0]['cost']
        self.assertEqual(group['anomalies'], [])
        self.assertIn('source_coverage_unknown', group['proof_gaps'])
        self.assertIsNotNone(group['partial_machine_cash_usd'])

    def test_activity_only_publication_keeps_three_joint_views(self):
        compact = self.build(identity_dimensions=True, activity_table=True, activity_only=True)
        self.assertEqual([(v['period'], v['dimension']) for v in compact['views']],
                         [('today','activity'),('week','activity'),('month','activity')])
        full = self.build(identity_dimensions=True, activity_table=True)
        for period in ('today','week','month'):
            left = next(v for v in compact['views'] if v['period'] == period)
            right = next(v for v in full['views'] if v['period'] == period and v['dimension'] == 'activity')
            self.assertEqual(left, right)

    def test_all_views_freeze_one_ingestion_watermark(self):
        with patch.object(self.ledger, 'receipt_snapshot', wraps=self.ledger.receipt_snapshot) as spy:
            result = self.build()
        self.assertEqual(spy.call_count, 1)
        self.assertEqual({x['watermark'] for x in result['views']}, {1})
        self.assertTrue(all(x['query']['scope'] == 'activity' for x in result['views']))

    def test_no_ledger_does_not_manufacture_zero(self):
        result = build_dashboard(None, subscriptions=SUBSCRIPTIONS, source_revision=REV, now=NOW)
        self.assertEqual(result['status'], 'unavailable')
        self.assertEqual(result['reason'], 'source_not_configured')
        self.assertEqual(result['views'], [])
        self.assertEqual(result['subscriptions'][1]['status'], 'stopped_user_confirmed')
        self.assertIsNone(result['subscriptions'][1]['monthly_amount_usd'])

    def test_restored_subscription_keeps_unknown_terms_and_usage_cost_unchanged(self):
        restored = json.loads(json.dumps(SUBSCRIPTIONS))
        restored['subscriptions'][1].update(status='active_user_confirmed', confirmed_on='2026-10-04')
        before = self.build()
        after = build_dashboard(self.ledger, catalog_snapshot=CATALOG,
            subscriptions=restored, source_revision=REV, now=NOW)
        self.assertEqual(after['views'], before['views'])
        for ledger in (self.ledger, None):
            result = build_dashboard(ledger, catalog_snapshot=CATALOG,
                subscriptions=restored, source_revision=REV, now=NOW)
            cc = next(row for row in result['subscriptions'] if row['id'] == 'claude_code')
            self.assertEqual(cc['status'], 'active_user_confirmed')
            self.assertEqual(cc['confirmed_on'], '2026-10-04')
            self.assertIsNone(cc['monthly_amount_usd'])
            self.assertIsNone(cc['billing_day'])
            self.assertNotIn('procurement_total_usd', result)

    def test_midnight_bounds_expiry_and_calendar_window(self):
        now = datetime(2026, 9, 30, 15, 59, tzinfo=timezone.utc)
        result = build_dashboard(None, subscriptions=SUBSCRIPTIONS, source_revision=REV, now=now)
        self.assertEqual(datetime.fromisoformat(result['expires_at']),
                         datetime(2026, 9, 30, 16, tzinfo=timezone.utc))

    def test_missing_prices_preserve_tokens_without_fake_amount(self):
        result = build_dashboard(self.ledger, subscriptions=SUBSCRIPTIONS, source_revision=REV, now=NOW)
        month = next(v for v in result['views'] if v['period']=='month')
        self.assertIsNone(month['summary']['cost']['machine_cash_usd'])
        self.assertIsNone(month['summary']['cost']['partial_machine_cash_usd'])
        self.assertIn('incomplete_price_or_quantity', month['summary']['cost']['reasons'])

    def test_openrouter_filter_utc_month_and_never_fill_missing_days(self):
        payload = {'generated_at': NOW.isoformat(), 'activity': {'source': 'openrouter.activity',
          'coverage': 'last_30_completed_utc_days', 'by_day': [
            {'date':'2026-08-31','usage_usd':'100','requests':100},
            {'date':'2026-09-09','usage_usd':'2.950298','requests':677}]}}
        result = project_openrouter(payload, now=NOW)
        self.assertEqual(result['usage_usd'], '2.950298')
        self.assertEqual(result['requests'], 677)
        self.assertEqual(result['coverage'], 'returned_rows_only')
        self.assertEqual(result['first_returned_day'], '2026-09-09')
        self.assertEqual(result['last_returned_day'], '2026-09-09')
        self.assertEqual(result['month_utc'], '2026-09')
        self.assertNotIn('by_model', result)

    def test_empty_and_stale_openrouter_are_not_zero(self):
        payload = {'generated_at': NOW.isoformat(), 'activity': {'source':'openrouter.activity',
          'coverage':'last_30_completed_utc_days','by_day':[]}}
        self.assertIsNone(project_openrouter(payload, now=NOW)['usage_usd'])
        payload['generated_at'] = (NOW-timedelta(days=2)).isoformat()
        self.assertEqual(project_openrouter(payload, now=NOW)['status'], 'stale')
        payload['generated_at'] = (NOW+timedelta(seconds=1)).isoformat()
        self.assertEqual(project_openrouter(payload, now=NOW)['status'], 'unavailable')

    def test_atomic_private_publication_rejects_rollback(self):
        result = self.build()
        path = Path(self.temp.name)/'published'/'dashboard.json'
        publish_dashboard(path, result)
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(path.parent.stat().st_mode & 0o777, 0o700)
        self.assertEqual(json.loads(path.read_text()), result)
        older = build_dashboard(None, subscriptions=SUBSCRIPTIONS, source_revision=REV,
                                 now=NOW-timedelta(minutes=1))
        with self.assertRaises(ValueError):
            publish_dashboard(path, older)
        self.assertEqual(json.loads(path.read_text()), result)
        self.assertFalse(list(path.parent.glob('*.pending')))

    def test_same_timestamp_different_publication_is_rejected(self):
        path = Path(self.temp.name)/'published'/'equal.json'
        original = self.build()
        publish_dashboard(path, original)
        changed = build_dashboard(None, subscriptions=SUBSCRIPTIONS, source_revision=REV, now=NOW)
        with self.assertRaises(ValueError):
            publish_dashboard(path, changed)
        publish_dashboard(path, original)
        self.assertEqual(json.loads(path.read_text()), original)

    def test_future_ingestion_does_not_become_current_amount(self):
        with patch.object(self.ledger, 'receipt_snapshot', return_value={
                'recorded_at':(NOW+timedelta(seconds=1)).isoformat(),'facts':[],
                'watermark':1,'sha256':'f'*64}):
            result = self.build()
        self.assertEqual(result['status'], 'unavailable')
        self.assertEqual(result['views'], [])

    def test_cli_collect_calls_existing_registered_collector(self):
        from contextlib import redirect_stdout
        import io
        from pulse_compute_cost.stats_dashboard import main
        from pulse_compute_cost.receipt_sources import collect_registered_sources
        registry = Path(self.temp.name)/'sources.json'
        doc = {'schema':'computecost_source_registry/v1',
               'ledger_root':str(Path(self.temp.name)/'cli-ledger'),'sources':[]}
        registry.write_text(json.dumps(doc)); registry.chmod(0o600)
        facts = Path(self.temp.name)/'facts.json'; facts.write_text(json.dumps(SUBSCRIPTIONS))
        output = Path(self.temp.name)/'cli-published'/'cost.json'
        with patch('pulse_compute_cost.receipt_sources.collect_registered_sources',
                   wraps=collect_registered_sources) as collect,              patch('pulse_compute_cost.stats_dashboard.build_dashboard',
                   wraps=build_dashboard) as build, redirect_stdout(io.StringIO()):
            code = main(['--source-registry',str(registry),'--collect','--procurement-facts',
                         str(facts),'--source-revision',REV,'--output',str(output)])
        self.assertEqual(code,0)
        self.assertEqual(collect.call_args.args[0],doc)
        self.assertIsInstance(collect.call_args.args[1],UsageLedger)
        self.assertIs(build.call_args.args[0], collect.call_args.args[1],
                      'collection and publication must reuse one verified ledger instance')
        self.assertIsNone(json.loads(output.read_text())['views'][0]['summary']['cost']['machine_cash_usd'])

    def test_amountless_result_never_replaces_published_amounts(self):
        from contextlib import redirect_stderr, redirect_stdout
        import io
        from pulse_compute_cost import stats_dashboard
        output = Path(self.temp.name)/'keep'/'cost.json'
        published = self.build()
        publish_dashboard(output, published)
        before = output.read_bytes()
        registry = Path(self.temp.name)/'keep-sources.json'
        registry.write_text(json.dumps({'schema':'computecost_source_registry/v1',
            'ledger_root':str(Path(self.temp.name)/'keep-ledger'),'sources':[]})); registry.chmod(0o600)
        facts = Path(self.temp.name)/'keep-facts.json'; facts.write_text(json.dumps(SUBSCRIPTIONS))
        later = {**published, 'generated_at': (NOW+timedelta(minutes=50)).isoformat(),
                 'status': 'unavailable', 'reason': 'query_source_unavailable', 'views': []}
        errors = io.StringIO()
        with patch.object(stats_dashboard, 'build_dashboard', return_value=later), \
             redirect_stdout(io.StringIO()), redirect_stderr(errors):
            code = stats_dashboard.main(['--source-registry',str(registry),'--procurement-facts',str(facts),
                                         '--source-revision',REV,'--output',str(output)])
        self.assertEqual(code, 2)
        self.assertEqual(output.read_bytes(), before)
        self.assertEqual(stats_dashboard.last_failure_reason(), 'query_source_unavailable')
        self.assertEqual(json.loads(errors.getvalue().splitlines()[0]),
            {'schema':'stats_cost_publication_failure/v1','reason':'query_source_unavailable',
             'error_type':'PublicationWithheld'})
        self.assertNotIn(self.temp.name, errors.getvalue())

    def test_failure_reasons_are_machine_codes_never_messages(self):
        from subprocess import TimeoutExpired
        from pulse_compute_cost.stats_dashboard import failure_reason
        from pulse_compute_cost.ledger import SnapshotChanged
        from pulse_compute_cost.receipts import ReceiptError
        class Rejected(Exception):
            code = 'ledger_unavailable'
        wrapped = ReceiptError('production observations unavailable; preserve source facts and retry')
        wrapped.__cause__ = Rejected('/private/path')
        self.assertEqual(failure_reason(wrapped), 'production_ledger_unavailable')
        self.assertEqual(failure_reason(SnapshotChanged('x')), 'production_ledger_changed')
        self.assertEqual(failure_reason(TimeoutExpired('git', 60)), 'timeout')
        self.assertEqual(failure_reason(ValueError('/Users/someone/secret')), 'publication_failed')

class WireExpansionTests(unittest.TestCase):
    """The 4MiB bound is for in-memory expansion; the publication stays within 512KiB."""
    def document(self, rows):
        from pulse_compute_cost.stats_dashboard_wire import COMPRESSION_METADATA
        return {**dict.fromkeys(COMPRESSION_METADATA, 'fixture'),
                'views': [{'groups': [{'key': {'i': i}, 'note': 'x' * 1000} for i in range(rows)]}]}

    def test_expansion_past_one_mebibyte_publishes_within_the_transport_limit(self):
        from pulse_compute_cost.receipts import canonical
        from pulse_compute_cost.stats_dashboard_wire import (COMPRESSED_FIELD, MAX_BYTES, MAX_EXPANDED_BYTES,
                                                             dashboard_bytes, expand_dashboard)
        document = self.document(1500)
        self.assertLess(1024 * 1024, len(canonical(document)) + 1)
        self.assertLessEqual(len(canonical(document)) + 1, MAX_EXPANDED_BYTES)
        data = dashboard_bytes(document)
        self.assertLessEqual(len(data), MAX_BYTES)
        self.assertIn(COMPRESSED_FIELD, json.loads(data))
        self.assertEqual(expand_dashboard(json.loads(data)), document)

    def test_expansion_past_the_in_memory_bound_is_not_packed(self):
        from pulse_compute_cost.receipts import canonical
        from pulse_compute_cost.stats_dashboard_wire import MAX_BYTES, MAX_EXPANDED_BYTES, dashboard_bytes
        document = self.document(4200)
        self.assertGreater(len(canonical(document)) + 1, MAX_EXPANDED_BYTES)
        self.assertGreater(len(dashboard_bytes(document)), MAX_BYTES)

if __name__ == '__main__':
    unittest.main()
