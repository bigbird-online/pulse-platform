"""Known zero Token consumption does not require an invented model price."""
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
from pulse_compute_cost.collaboration_query import _value_cost, CollaborationQuery, query_collaboration
from pulse_compute_cost.receipt_sources import usage_receipt
from pulse_compute_cost.ledger import UsageLedger
from pulse_compute_cost.stats_dashboard import build_dashboard
from test_stats_dashboard import NOW, REV, SUBSCRIPTIONS


class ZeroTokenPriceTests(unittest.TestCase):
    def usage(self, counts=None, model='model-not-in-catalog'):
        return usage_receipt('pi', 'zero-test', counts if counts is not None else {
            'input': 0, 'output': 0, 'cacheRead': 0, 'cacheWrite': 0},
            at=NOW.isoformat(), observed=NOW.isoformat(), model=model)

    def test_explicit_zero_needs_no_catalog_or_model_lookup(self):
        for catalog in (None, {}, {'data': []}):
            for model in ('model-not-in-catalog', None, 'unknown'):
                with self.subTest(catalog=catalog, model=model), patch(
                        'pulse_compute_cost.collaboration_query._model',
                        side_effect=AssertionError('zero must not look up prices')):
                    self.assertEqual(_value_cost(self.usage(model=model), catalog), (Decimal(0), None))

    def test_unknown_quantity_is_never_filled_with_zero(self):
        for field in ('input', 'output', 'cacheRead', 'cacheWrite'):
            counts = dict(input=0, output=0, cacheRead=0, cacheWrite=0)
            del counts[field]
            with self.subTest(field=field):
                amount, reason = _value_cost(self.usage(counts), {'data': []})
                self.assertIsNone(amount)
                self.assertIsNotNone(reason)

    def test_unknown_quantity_is_reported_before_a_missing_model_or_catalog(self):
        # A Host result without any counter is a measurement gap, not a price gap.
        for catalog in (None, {'data': []}):
            for model in (None, 'unknown', 'model-not-in-catalog'):
                with self.subTest(catalog=catalog, model=model):
                    self.assertEqual(_value_cost(self.usage({}, model=model), catalog),
                                     (None, 'unknown_token_quantity'))
        self.assertEqual(_value_cost(self.usage({'input': 1, 'output': 0, 'cacheRead': 0, 'cacheWrite': 0},
                                                model=None), {'data': []}), (None, 'missing_price_or_model'))

    def test_any_nonzero_class_still_requires_real_price(self):
        for field in ('input', 'output', 'cacheRead', 'cacheWrite'):
            counts = dict(input=0, output=0, cacheRead=0, cacheWrite=0)
            counts[field] = 1
            with self.subTest(field=field):
                self.assertEqual(_value_cost(self.usage(counts), {'data': []}),
                                 (None, 'missing_or_invalid_price'))

    def test_zero_row_remains_in_query_and_all_published_views(self):
        with TemporaryDirectory() as temporary:
            ledger = UsageLedger(Path(temporary) / 'ledger', clock=lambda: NOW)
            row = self.usage()
            ledger.ingest_receipts([row])
            before = ledger.receipt_snapshot()
            query = CollaborationQuery(scope='activity', from_date='2026-09-13',
                                       to_date='2026-09-14', group_by=('model',))
            report = query_collaboration(ledger, query, catalog={'data': []})
            self.assertEqual(report['summary']['usage']['event_count'], 1)
            self.assertEqual(report['summary']['cost']['partial_machine_cash_usd'], '0')
            self.assertEqual(report['summary']['cost']['unpriced'], [])
            self.assertEqual(report['groups'][0]['usage']['event_count'], 1)
            self.assertEqual(report['groups'][0]['cost']['partial_machine_cash_usd'], '0')
            document = build_dashboard(ledger, subscriptions=SUBSCRIPTIONS,
                source_revision=REV, now=NOW, identity_dimensions=True, activity_table=True)
            self.assertEqual(document['status'], 'available')
            self.assertEqual(len(document['views']), 21)
            for view in document['views']:
                self.assertEqual(view['summary']['usage']['event_count'], 1)
                self.assertEqual(view['summary']['cost']['partial_machine_cash_usd'], '0')
                self.assertEqual(view['summary']['cost']['unpriced'], [])
            self.assertEqual(ledger.receipt_snapshot()['sha256'], before['sha256'])
            self.assertIsNone(report['summary']['cost']['machine_cash_usd'])
            # Known cost never manufactures unknown identity or complete coverage.
            self.assertEqual(report['summary']['usage']['agent_unassigned_event_count'], 1)


if __name__ == '__main__':
    unittest.main()
