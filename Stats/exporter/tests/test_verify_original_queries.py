"""Deployment acceptance recomputes each published view from the frozen ledger, full or incremental engine (WI754)."""
from datetime import datetime, timedelta, timezone
from pathlib import Path
import tempfile
import unittest

from pulse_compute_cost.collaboration_query import CollaborationQuerySession
from pulse_compute_cost.incremental_views import IncrementalViews, rebuild
from pulse_compute_cost.ledger import UsageLedger
from pulse_compute_cost.receipt_sources import usage_receipt
from pulse_compute_cost.stats_dashboard import build_dashboard
from Stats.exporter.tests.verify_installed_dashboard import verify_original_queries

NOW = datetime(2026, 11, 20, 4, tzinfo=timezone.utc)
CATALOG = {'retrieved_at': NOW.isoformat(), 'catalog': {'data': [{
    'id': 'openai/gpt-4o', 'canonical_slug': 'openai/gpt-4o',
    'pricing': {'prompt': '0.000002', 'completion': '0.00001',
                'input_cache_read': '0.0000002', 'input_cache_write': '0.000002'}}]}}
FACTS = {'schema_version': 'pulse_stats_procurement_facts/v1', 'subscriptions': []}


class VerifyOriginalQueriesTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        clock = [NOW - timedelta(days=3)]

        def tick():
            clock[0] += timedelta(seconds=1)
            return clock[0]

        self.ledger = UsageLedger(root / 'ledger', clock=tick)
        for n in range(12):  # Ten priced, two unpriced (unknown model): one batch each.
            at = (NOW - timedelta(hours=60 - 5 * n)).isoformat()
            self.ledger.ingest_receipts([usage_receipt('pi', ['request', n], {
                'input': 100 + n, 'output': 10, 'cacheRead': 30, 'cacheWrite': 0}, at=at, observed=at,
                model='openai/gpt-4o' if n % 6 else 'unknown/model',
                common={'wi': f'wi_20261120_{100 + n % 3:03d}', 'executor': 'pulse', 'responsible': 'pulse',
                        'delegator': None})])
        self.derived = root / 'derived'
        self.derived.mkdir(mode=0o700)
        rebuild(self.ledger, None, derived=self.derived)

    def publish(self, engine):
        incremental = (lambda upto: IncrementalViews.attach(self.ledger, None, upto=upto, derived=self.derived)
                       if engine != 'full' else None)
        return build_dashboard(self.ledger, subscriptions=FACTS, source_revision='a' * 40, now=NOW,
                               catalog_snapshot=CATALOG, identity_dimensions=True, activity_table=True,
                               engine=engine, incremental=incremental)

    def frozen_for(self):
        sessions = {}

        def frozen_for(query, canonical_order=False):
            key = (query.get('watermark'), canonical_order)
            if key not in sessions:
                sessions[key] = CollaborationQuerySession(self.ledger.receipt_snapshot(watermark=key[0]),
                                                          canonical_order=canonical_order)
            return sessions[key]
        return frozen_for

    def test_full_and_incremental_publications_both_match_the_original_queries(self):
        full, incremental = self.publish('full'), self.publish('incremental')
        self.assertEqual(len(incremental['views']), 21)
        for document in (full, incremental):
            verify_original_queries(document, self.frozen_for(), CATALOG)
        # The incremental report is the original query read in canonical order, digest for digest.
        ordered = self.frozen_for()
        for view in incremental['views']:
            q = dict(view['query'], group_by=tuple(view['query']['group_by']),
                     work_items=tuple(view['query']['work_items']))
            from pulse_compute_cost.collaboration_query import CollaborationQuery
            raw = ordered(q, True).query(CollaborationQuery(**q), catalog=CATALOG['catalog'],
                                         catalog_observed_at=view['catalog_observed_at'])
            self.assertEqual(raw['report_sha256'], view['report_sha256'])
            self.assertEqual(raw['facts_sha256'], self.ledger.receipt_snapshot(watermark=q['watermark'])['sha256'])

    def test_a_publication_that_differs_from_its_query_is_rejected(self):
        for engine in ('full', 'incremental'):
            document = self.publish(engine)
            view = document['views'][0]
            view['report_sha256'] = '0' * 64
            with self.assertRaisesRegex(AssertionError, 'original query and publisher differ'):
                verify_original_queries(document, self.frozen_for(), CATALOG)


if __name__ == '__main__':
    unittest.main()
