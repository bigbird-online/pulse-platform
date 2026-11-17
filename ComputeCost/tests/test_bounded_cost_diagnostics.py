"""Large missing-price cohorts must not erase Stats activity views."""
import json
import unittest
from copy import deepcopy
from pulse_compute_cost.receipt_sources import usage_receipt
from pulse_compute_cost.receipts import canonical, digest
from pulse_compute_cost.stats_dashboard import MAX_BYTES
import test_stats_dashboard as fixture

class BoundedCostDiagnosticsTests(unittest.TestCase):
    setUp = fixture.StatsDashboardTests.setUp
    def build(self, **kwargs):
        from datetime import timedelta
        from pulse_compute_cost.stats_dashboard import build_dashboard
        return build_dashboard(self.ledger, catalog_snapshot=fixture.CATALOG,
            subscriptions=fixture.SUBSCRIPTIONS, source_revision=fixture.REV,
            now=fixture.NOW + timedelta(seconds=1), **kwargs)
    def test_large_unpriced_cohort_keeps_all_counts_and_complete_query_binding(self):
        rows = [usage_receipt('pi', ['missing-price', i], {'input': 10, 'output': 2},
            at=fixture.NOW.isoformat(), observed=fixture.NOW.isoformat(), model='unpriced/model',
            common={'wi': 'wi_20260825_300', 'executor': 'pulse', 'responsible': None, 'delegator': None})
            for i in range(1500)]
        self.ledger.ingest_receipts(rows)
        before = self.ledger.receipt_snapshot()
        result = self.build(activity_table=True, activity_only=True)
        self.assertEqual(result['status'], 'available')
        self.assertLess(len(canonical(result)), MAX_BYTES)
        self.assertEqual(len(result['views']), 3)
        for view in result['views']:
            cost = view['summary']['cost']
            self.assertEqual(view['summary']['usage']['event_count'], 1501)
            self.assertEqual(cost['unpriced_count'], 1500)
            self.assertEqual(sum(cost['unpriced_reason_counts'].values()), 1500)
            self.assertLessEqual(len(cost['unpriced']), 4)
            self.assertIsNone(cost['machine_cash_usd'])
            self.assertEqual(len(cost['unpriced_sha256']), 64)
            from pulse_compute_cost.collaboration_query import CollaborationQuery, query_collaboration
            from pulse_compute_cost.stats_dashboard import project_cost
            q = dict(view['query']); q['group_by'] = tuple(q['group_by']); q['work_items'] = tuple(q['work_items'])
            raw = query_collaboration(self.ledger, CollaborationQuery(**q), catalog=fixture.CATALOG['catalog'], catalog_observed_at=fixture.CATALOG['retrieved_at'])
            self.assertEqual(view['report_sha256'], raw['report_sha256'])
            self.assertEqual(cost, project_cost(raw['summary']['cost']))
            self.assertEqual(cost['unpriced_sha256'], digest(raw['summary']['cost']['unpriced']))
            self.assertEqual(cost['partial_machine_cash_usd'], raw['summary']['cost']['partial_machine_cash_usd'])
        self.assertEqual(self.ledger.receipt_snapshot()['sha256'], before['sha256'])
        self.assertEqual(result, self.build(activity_table=True, activity_only=True))

    def test_samples_keep_every_reason_and_input_unchanged(self):
        from pulse_compute_cost.stats_dashboard import project_cost
        cost = self.build()['views'][0]['summary']['cost']
        cost['unpriced'] = [{'fact_ref': 'ccf1_' + str(i).zfill(64), 'reason': 'quantity_unknown' if i % 7 == 0 else 'missing_or_invalid_price'} for i in range(70)]
        original = deepcopy(cost); compact = project_cost(cost)
        self.assertEqual(cost, original)
        self.assertEqual(compact['unpriced_reason_counts'], {'quantity_unknown': 10, 'missing_or_invalid_price': 60})
        self.assertEqual({x['reason'] for x in compact['unpriced']}, set(compact['unpriced_reason_counts']))
        self.assertEqual(compact['unpriced_sha256'], digest(original['unpriced']))
        for field in ('machine_cash_usd', 'partial_machine_cash_usd', 'reasons', 'cash_basis'):
            self.assertEqual(compact[field], original[field])
