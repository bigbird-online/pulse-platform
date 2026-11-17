"""The pricing UAT may not hide diagnostic changes without retained evidence."""
from pathlib import Path
import sys
import unittest
from copy import deepcopy
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'uat'))
from catalog_source_replay import verify_restored_source_diagnostics, verify_published_projection
from pulse_compute_cost.receipts import Receipt, reference

class SourceDiagnosticReplayTests(unittest.TestCase):
    def records(self, same_label=False):
        result = []
        for index, label in enumerate(('node1.harness_root', 'node1.harness_root' if same_label else 'node2.harness_root')):
            detail = dict(label=label, kind='harness_root', layout='registered_recursive.v1', files_seen=1,
                          files_read=1, files_invalid=0, excluded_directories=0, latest_usage_at=None, errors={})
            result.append(Receipt.make('coverage', 'same-legacy-identity', dict(domain='execution', wi=None,
                source_ref=reference('source', 'canonical-harness'), status='unknown', expected=None, observed=None,
                watermark=None, reason='one' if index == 0 else 'two', source_detail=detail),
                occurred_at=None, observed_at='2026-09-14T00:00:00Z', producer='registered_collector.v1'))
        return result

    def restored(self, records):
        return [dict(r.payload['source_detail'], status=r.payload['status'], reason=r.payload['reason'],
                     source_ref=reference('qualified_collector_source', [r.payload['source_ref'], r.payload['source_detail']['label']])) for r in records]

    def test_only_exact_retained_distinct_node_conflicts_may_reappear(self):
        records = self.records()
        self.assertEqual(verify_restored_source_diagnostics([], self.restored(records), records),
                         ['node1.harness_root', 'node2.harness_root'])

    def test_unproven_added_source_or_changed_existing_source_is_rejected(self):
        records = self.records(); rows = self.restored(records)
        for before, after, facts in (([], rows, []), (rows, [dict(rows[0], status='complete'), rows[1]], records)):
            with self.assertRaises(AssertionError): verify_restored_source_diagnostics(before, after, facts)

    def test_missing_one_source_and_same_node_conflicts_are_not_accepted(self):
        records = self.records()
        with self.assertRaises(AssertionError): verify_restored_source_diagnostics([], self.restored(records)[:1], records)
        records = self.records(same_label=True)
        with self.assertRaises(AssertionError): verify_restored_source_diagnostics([], self.restored(records), records)

    def test_projection_must_keep_money_quantities_and_real_gaps(self):
        before = {'usage': {'event_count': 1, 'known_total_tokens': 12, 'tokens': {'input': 10, 'output': 2},
                            'time_unallocated_event_count': 9, 'source_details': []},
                  'cost': {'machine_cash_usd': None, 'partial_machine_cash_usd': '1.25',
                           'cash_basis': 'fixture', 'unpriced': [], 'reasons': ['time_unallocated']}}
        after = deepcopy(before); after['usage']['time_unallocated_event_count'] = 0; after['cost']['reasons'] = []
        self.assertEqual(verify_published_projection(before, after, []), ['time_unallocated'])
        for domain, field, value in (('usage', 'known_total_tokens', 13), ('cost', 'partial_machine_cash_usd', '1.24'),
                                     ('usage', 'time_unallocated_event_count', 1)):
            bad = deepcopy(after); bad[domain][field] = value
            with self.assertRaises(AssertionError): verify_published_projection(before, bad, [])
        bad = deepcopy(before); bad['cost']['reasons'] = ['attribution_unknown']
        with self.assertRaises(AssertionError): verify_published_projection(bad, after, [])

if __name__ == '__main__': unittest.main()
