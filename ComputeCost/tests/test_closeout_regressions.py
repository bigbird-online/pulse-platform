"""Independent WI568 review counterexamples; no production workload is created."""
from hashlib import sha256
import unittest

from pulse_compute_cost.wi_report import WiReportError, build_wi_report
from pulse_compute_cost.wi_pricing import price_wi_usage_cohort
import test_cash_policy as cash
import test_wi_report as fixtures

WI = cash.WI


def redigest(value):
    value['cohort_sha256'] = sha256(cash._canonical({
        key: item for key, item in value.items() if key != 'cohort_sha256'
    })).hexdigest()
    return value


class CloseoutRegressionTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.WiReportTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)

    def ledger(self):
        for epoch, model, delta in [
            (1, 'gpt-5.6-sol', (250000, 230000, 1000, 0, 251000)),
            (2, 'gpt-5.6-terra', (100000, 90000, 500, 0, 100500)),
        ]:
            self.fixture.seal_epoch(
                self.fixture.observer(work_item=WI, epoch=epoch, model=model),
                thread=f'private-review-{epoch}', baseline=(0, 0, 0, 0, 0),
                delta=delta, minute=epoch,
            )
        return self.fixture.ledger

    def test_price_without_usage_is_rejected(self):
        with self.assertRaises(WiReportError):
            build_wi_report(None, WI, openrouter_reference_cost=cash.valuation())

    def test_same_wi_old_price_cannot_price_new_usage(self):
        with self.assertRaises(WiReportError):
            build_wi_report(None, WI, usage_cohort=cash.cohort(above_272k=True),
                            openrouter_reference_cost=cash.valuation())

    def test_equal_quantities_from_other_evidence_cannot_reuse_price(self):
        newer = cash.cohort()
        newer['window']['source_ref'] = 'evidence/different-boundary.json'
        redigest(newer)
        with self.assertRaises(WiReportError):
            build_wi_report(None, WI, usage_cohort=newer, openrouter_reference_cost=cash.valuation())

    def test_equal_subtotals_never_hide_missing_producer(self):
        cohort = cash.cohort()
        cohort['status'] = 'partial'
        cohort['producer_attempts'][0]['status'] = 'partial'
        redigest(cohort)
        value = price_wi_usage_cohort(cohort, cash.models(), retrieved_at='2026-09-11T00:00:00Z')
        report = build_wi_report(self.ledger(), WI, usage_cohort=cohort, openrouter_reference_cost=value)
        self.assertEqual(report['usage']['status'], 'partial')
        self.assertEqual(report['cost']['machine_cash_usd'], 'unavailable')
        self.assertEqual(report['cost']['partial_machine_cash_usd'], '0.14')

    def test_equal_subtotals_never_hide_open_epoch(self):
        ledger = self.ledger()
        self.fixture.observer(work_item=WI, epoch=3).bind(
            fixtures.usage('private-open', 'baseline', (0, 0, 0, 0, 0), (0, 0, 0, 0, 0)),
            observed_at='2026-08-25T00:03:00Z',
        )
        report = build_wi_report(ledger, WI, usage_cohort=cash.cohort(),
                                openrouter_reference_cost=cash.valuation())
        self.assertEqual(report['usage']['status'], 'partial')
        self.assertEqual(report['cost']['machine_cash_usd'], 'unavailable')

    def test_discovery_cannot_hide_only_open_ledger_epoch(self):
        self.fixture.observer(work_item=WI, epoch=1).bind(
            fixtures.usage('private-open-only', 'baseline', (0, 0, 0, 0, 0), (0, 0, 0, 0, 0)),
            observed_at='2026-08-25T00:03:00Z',
        )
        report = build_wi_report(self.fixture.ledger, WI, usage_cohort=cash.cohort(),
                                openrouter_reference_cost=cash.valuation())
        self.assertEqual(report['usage']['status'], 'partial')
        self.assertIn('unsealed_epoch_excluded', report['warnings'])
        self.assertEqual(report['cost']['machine_cash_usd'], 'unavailable')
        self.assertEqual(report['cost']['partial_machine_cash_usd'], '0.14')
        self.assertEqual(report['cost']['cash_coverage'], 'partial_usage')

    def test_conflicting_sources_cannot_publish_canonical_cash(self):
        report = build_wi_report(self.ledger(), WI, usage_cohort=cash.cohort(above_272k=True),
                                openrouter_reference_cost=cash.valuation(above_272k=True))
        self.assertEqual(report['cost']['machine_cash_usd'], 'unavailable')
        self.assertEqual(report['cost']['cash_coverage'], 'join_conflict')
        self.assertIn('producer_join_conflict', report['warnings'])

    def test_equal_totals_do_not_hide_model_conflict(self):
        cohort = cash.cohort()
        for request in cohort['request_partitions']:
            request['model'] = ('gpt-5.6-terra' if request['model'] == 'gpt-5.6-sol' else 'gpt-5.6-sol')
        cohort['by_model'] = dict(zip(reversed(list(cohort['by_model'])), cohort['by_model'].values()))
        redigest(cohort)
        value = price_wi_usage_cohort(cohort, cash.models(), retrieved_at='2026-09-11T00:00:00Z')
        report = build_wi_report(self.ledger(), WI, usage_cohort=cohort, openrouter_reference_cost=value)
        self.assertEqual(report['usage']['status'], 'partial')
        self.assertEqual(report['cost']['cash_coverage'], 'join_conflict')

    def test_complete_matching_sources_still_price_exactly(self):
        report = build_wi_report(self.ledger(), WI, usage_cohort=cash.cohort(),
                                openrouter_reference_cost=cash.valuation())
        self.assertEqual(report['usage']['status'], 'exact')
        self.assertEqual(report['cost']['machine_cash_usd'], '0.14')

    def test_missing_model_partition_retains_incurred_tokens(self):
        cohort = cash.cohort()
        cohort['by_model'] = {}
        redigest(cohort)
        report = build_wi_report(None, WI, usage_cohort=cohort)
        self.assertEqual(report['usage']['tokens']['total_tokens'], 351500)
        self.assertEqual(report['usage']['status'], 'partial')
        self.assertEqual(report['cost']['machine_cash_usd'], 'unavailable')
        self.assertIn('usage_model_partitions_unavailable', report['warnings'])
