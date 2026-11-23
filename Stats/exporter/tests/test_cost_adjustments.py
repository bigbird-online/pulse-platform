"""Read side of closed weeks and adjustment entries (WI754 step 5, PR-5a)."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / 'ComputeCost'))
sys.path.insert(0, str(ROOT / 'Stats/exporter'))
from pulse_compute_cost.ledger import UsageLedger  # noqa: E402
from pulse_compute_cost.receipt_sources import usage_receipt  # noqa: E402
from pulse_compute_cost.stats_dashboard import build_dashboard  # noqa: E402
from pulse_stats import adjustments, query  # noqa: E402
from pulse_stats.cost_dashboard import _canonical, _validate  # noqa: E402

NOW = datetime(2026, 9, 13, 8, tzinfo=timezone.utc)
FACTS = {'schema_version': 'pulse_stats_procurement_facts/v1', 'subscriptions': []}
CLASSES = adjustments.TOKEN_CLASSES


def entries(count, output, amount, week='2026-08-31', reason='late_usage'):
    tokens = {**dict.fromkeys(CLASSES, 0), 'output': output}
    part = {'entry_count': count, 'unpriced_entry_count': 0, 'tokens': tokens,
            'known_total_tokens': output, 'priced_machine_cash_usd': amount}
    return {**part, 'by_origin': [{**part, 'week_start': week, 'reason': reason}]}


def total_of(owner, block):
    usage = owner['usage']
    tokens = {k: usage['tokens'][k]['known'] + block['tokens'][k] for k in CLASSES}
    amount = adjustments.own_priced(owner['cost']) + adjustments.Decimal(block['priced_machine_cash_usd'])
    return {'tokens': tokens, 'known_total_tokens': usage['known_total_tokens'] + block['known_total_tokens'],
            'priced_machine_cash_usd': str(amount)}


def seal(document):
    document['sha256'] = hashlib.sha256(_canonical({k: v for k, v in document.items() if k != 'sha256'})).hexdigest()
    return document


class AdjustmentReaderTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(); self.addCleanup(tmp.cleanup)
        ledger = UsageLedger(Path(tmp.name) / 'ledger', clock=lambda: NOW)
        ledger.ingest_receipts([usage_receipt(
            'pi', 'adjusted', {'input': 1, 'output': 1, 'cacheRead': 0, 'cacheWrite': 0},
            at=NOW.isoformat(), observed=NOW.isoformat(), model='openai/test',
            common={'wi': 'wi_20260825_300', 'executor': 'pulse', 'responsible': 'pulse', 'delegator': None})])
        self.old = build_dashboard(ledger, subscriptions=FACTS, source_revision='c' * 40, now=NOW,
                                   identity_dimensions=True, activity_table=True, activity_only=True)

    def adjusted(self):
        """The week view gains +3 output tokens on its row and -3 on a key without own usage."""
        document = deepcopy(self.old)
        view = next(v for v in document['views'] if v['period'] == 'week')
        row = view['groups'][0]
        row['adjustments'] = entries(1, 3, '0.3', reason='reattributed')
        row['total'] = total_of(row, row['adjustments'])
        zero = {k: {'known': 0, 'unknown_event_count': 0} for k in CLASSES}
        only = {'key': {**row['key'], 'agent': 'duke'}, 'usage': {'tokens': zero, 'event_count': 0, 'known_total_tokens': 0},
                'cost': {'machine_cash_usd': '0', 'partial_machine_cash_usd': None,
                         'cash_basis': 'openrouter_standard_token_cash', 'reasons': [], 'unpriced': []},
                'adjustments': entries(1, -3, '-0.3', reason='reattributed')}
        only['total'] = total_of(only, only['adjustments'])
        view['adjustment_only_groups'] = [only]
        view['adjustments'] = entries(2, 0, '0.0', reason='reattributed')
        view['total'] = total_of(view['summary'], view['adjustments'])
        return seal(document)

    def test_old_and_new_publications_both_validate(self):
        _validate(self.old)
        _validate(self.adjusted())

    def test_wrong_totals_and_broken_breakdowns_are_rejected(self):
        def bad(change):
            document = self.adjusted()
            change(next(v for v in document['views'] if v['period'] == 'week'))
            return seal(document)
        cases = {
            'row total off': lambda v: v['groups'][0]['total'].update(priced_machine_cash_usd='9'),
            'view total tokens off': lambda v: v['total']['tokens'].update(output=0),
            'origins do not partition': lambda v: v['groups'][0]['adjustments']['by_origin'][0].update(entry_count=2),
            'origin week not a Monday': lambda v: v['groups'][0]['adjustments']['by_origin'][0].update(week_start='2026-09-01'),
            'unknown reason': lambda v: v['groups'][0]['adjustments']['by_origin'][0].update(reason='other'),
            'rows do not add up': lambda v: v.update(adjustment_only_groups=[]),
            'adjustment-only row has usage': lambda v: v['adjustment_only_groups'][0]['usage'].update(event_count=1),
            'row total without view adjustments': lambda v: [v.pop('adjustments'), v.pop('total')],
            'closed week on the current week': lambda v: v.update(closed_week=None),
            'view origin reason differs from the rows': lambda v: v['adjustments']['by_origin'][0].update(reason='withdrawn'),
            'adjustment-only row has an own amount': self.own_amount_on_adjustment_only_row,
            'adjustment-only row has own tokens': lambda v: [
                v['adjustment_only_groups'][0]['usage']['tokens']['output'].update(unknown_event_count=1)],
            'adjustment-only row repeats an own row': lambda v: v['adjustment_only_groups'][0].update(
                key=dict(v['groups'][0]['key'])),
            'adjustment-only rows repeat a key': self.repeated_adjustment_only_row,
            'a boolean as a total token count': lambda v: v['groups'][0]['total']['tokens'].update(uncached_input=True),
        }
        for name, change in cases.items():
            with self.subTest(name), self.assertRaises((ValueError, KeyError, TypeError)):
                _validate(bad(change))

    @staticmethod
    def own_amount_on_adjustment_only_row(view):
        """$5 of own cost on a row without usage: the rows would add $5 the view total lacks."""
        only = view['adjustment_only_groups'][0]
        only['cost']['machine_cash_usd'] = '5'
        only['total'] = total_of(only, only['adjustments'])

    @staticmethod
    def repeated_adjustment_only_row(view):
        view['adjustment_only_groups'].append(deepcopy(view['adjustment_only_groups'][0]))
        view['adjustments'] = entries(3, -3, '-0.3', reason='reattributed')
        view['total'] = total_of(view['summary'], view['adjustments'])

    _ABSENT = object()

    def last_week(self, closed_week=_ABSENT):
        document = deepcopy(self.old)
        view = next(v for v in document['views'] if v['period'] == 'week')
        view['period'] = 'last_week'
        if closed_week is not self._ABSENT:
            view['closed_week'] = closed_week
        view['query'].update(from_date='2026-08-31', to_date='2026-09-07')
        document['views'] = [view]
        return seal(document), view

    def test_closed_week_names_its_close_and_gaps(self):
        for closed in (None, {'week_start': '2026-08-31', 'closed_at': '2026-09-07T06:05:00+08:00',
                              'watermark': 1, 'gaps': [{'peer': 'ownermac', 'reason': 'peer_unread'}]}):
            with self.subTest(closed=closed):
                _validate(self.last_week(closed)[0])
        for wrong in ({'week_start': '2026-09-07', 'closed_at': '2026-09-07T06:05:00+08:00', 'watermark': 1, 'gaps': []},
                      {'week_start': '2026-08-31', 'closed_at': '2026-09-07T06:05:00+08:00', 'watermark': 2, 'gaps': []},
                      {'week_start': '2026-08-31', 'closed_at': '2026-09-07T06:05:00', 'watermark': 1, 'gaps': []},
                      {'week_start': '2026-08-31', 'closed_at': '2026-09-07T06:05:00+08:00', 'watermark': True, 'gaps': []},
                      # Six days before the week ends, and one second before it does.
                      {'week_start': '2026-08-31', 'closed_at': '2026-09-01T06:05:00+08:00', 'watermark': 1, 'gaps': []},
                      {'week_start': '2026-08-31', 'closed_at': '2026-09-06T15:59:59+00:00', 'watermark': 1, 'gaps': []},
                      {'week_start': '2026-08-31', 'closed_at': '2026-09-07T06:05:00+08:00', 'watermark': 1,
                       'gaps': [{'peer': 'b', 'reason': 'x'}, {'peer': 'a', 'reason': 'x'}]}):
            with self.subTest(wrong=wrong), self.assertRaises(ValueError):
                _validate(self.last_week(wrong)[0])

    def test_query_returns_own_adjustment_and_total(self):
        document = self.adjusted()
        args = SimpleNamespace(period='week', group=None, agent=None, work_item_id=None, runtime=None)
        with mock.patch.object(query, 'load_cost_dashboard', return_value=document):
            src = query.Sources(app_snapshot=Path('/nonexistent'), cost_publication=Path('/x'),
                                now=NOW + timedelta(minutes=1))
            result = query.q_cost(src, args)
            self.assertEqual(result['summary']['adjustment']['entry_count'], 2)
            self.assertEqual(result['rows'][0]['adjustment']['amount_usd'], '0.3')
            self.assertEqual(result['adjustment_only_rows'][0]['total']['amount_usd'], '-0.3')
            grouped = query.q_cost(src, SimpleNamespace(**{**vars(args), 'group': 'agent'}))
        totals = {g['agent']: (g['amount_usd'], g['adjustment']['amount_usd'], g['total']['amount_usd'])
                  for g in grouped['groups']}
        self.assertEqual(totals, {'pulse': (None, '0.3', '0.3'), 'duke': (None, '-0.3', '-0.3')})

    def test_filtered_summary_and_groups_keep_the_whole_adjustment(self):
        document = self.adjusted()
        args = SimpleNamespace(period='week', group='wi', agent='duke', work_item_id=None, runtime=None)
        with mock.patch.object(query, 'load_cost_dashboard', return_value=document):
            src = query.Sources(app_snapshot=Path('/nonexistent'), cost_publication=Path('/x'),
                                now=NOW + timedelta(minutes=1))
            result = query.q_cost(src, args)
            row = query.q_cost(src, SimpleNamespace(**{**vars(args), 'group': None}))['adjustment_only_rows'][0]
        tokens = {**dict.fromkeys(CLASSES, 0), 'output': -3}
        origin = {'week_start': '2026-08-31', 'reason': 'reattributed', 'entry_count': 1, 'unpriced_entry_count': 0,
                  'tokens': tokens, 'known_total_tokens': -3, 'priced_machine_cash_usd': '-0.3'}
        expected = {'adjustment': {'entry_count': 1, 'unpriced_entry_count': 0, 'amount_usd': '-0.3',
                                   'known_total_tokens': -3, 'tokens': tokens, 'by_origin': [origin]},
                    'total': {'amount_usd': '-0.3', 'known_total_tokens': -3, 'tokens': tokens}}
        for name, owner in (('row', row), ('summary', result['summary']), ('group', result['groups'][0])):
            with self.subTest(name):
                self.assertEqual({k: owner.get(k) for k in expected}, expected)
        self.assertEqual(result['platform']['summary']['adjustment']['entry_count'], 2)

    def ask_wi(self, document, wi):
        with mock.patch.object(query, 'load_cost_dashboard', return_value=document):
            src = query.Sources(app_snapshot=Path('/nonexistent'), cost_publication=Path('/x'),
                                now=NOW + timedelta(minutes=1))
            by_wi = query.q_wi(src, SimpleNamespace(work_item_id=wi))['cost_by_period']['week']
            filtered = query.q_cost(src, SimpleNamespace(period='week', group=None, agent=None, work_item_id=wi,
                                                         runtime=None))
        return by_wi, filtered

    def test_one_wi_counts_its_adjustment_only_rows(self):
        # Mixed: the WI's own row +$0.30 and another key of it -$0.30 net to $0, as `cost --wi` says.
        by_wi, filtered = self.ask_wi(self.adjusted(), 'wi_20260825_300')
        self.assertEqual(filtered['summary']['total']['amount_usd'], '0.0')
        self.assertEqual((by_wi['total']['adjustment']['amount_usd'], by_wi['total']['total']['amount_usd']),
                         ('0.0', '0.0'))
        self.assertEqual([r['key']['agent'] for r in by_wi['adjustment_only_rows']], ['duke'])
        # Adjustment only: a WI whose sole entry is -$0.30 has that total, not none.
        document = self.adjusted()
        view = next(v for v in document['views'] if v['period'] == 'week')
        view['adjustment_only_groups'][0]['key']['wi'] = 'wi_20260913_001'
        by_wi, filtered = self.ask_wi(seal(document), 'wi_20260913_001')
        self.assertEqual(by_wi['rows'], [])
        self.assertEqual(by_wi['total']['total']['amount_usd'], '-0.3')
        self.assertEqual(filtered['summary']['total']['amount_usd'], '-0.3')

    def test_a_null_adjustment_only_field_is_rejected_and_never_breaks_a_query(self):
        for document in (seal({**deepcopy(self.old), 'views': [{**v, 'adjustment_only_groups': None}
                                                                 for v in self.old['views']]}),):
            with self.assertRaises(ValueError):
                _validate(document)
        broken = self.adjusted()
        next(v for v in broken['views'] if v['period'] == 'week')['adjustment_only_groups'] = None
        with self.assertRaises(ValueError):
            _validate(seal(broken))
        # A reader handed such a view anyway answers instead of raising.
        for group in (None, 'agent'):
            with mock.patch.object(query, 'load_cost_dashboard', return_value=broken):
                src = query.Sources(app_snapshot=Path('/nonexistent'), cost_publication=Path('/x'),
                                    now=NOW + timedelta(minutes=1))
                result = query.q_cost(src, SimpleNamespace(period='week', group=group, agent=None,
                                                           work_item_id=None, runtime=None))
                self.assertEqual(result['period'], 'week')
                self.assertIn('cost_by_period', query.q_wi(src, SimpleNamespace(work_item_id='wi_20260825_300')))

    def test_groups_and_filters_add_entries_of_rows_whose_own_tokens_are_null(self):
        """Own 1 output plus 3 adjusted, and -3 on a row without events whose own tokens are null: the total is 1."""
        document = self.adjusted()
        view = next(v for v in document['views'] if v['period'] == 'week')
        only = view['adjustment_only_groups'][0]
        only['usage'].update(known_total_tokens=None, tokens={k: {'known': None, 'unknown_event_count': 0} for k in CLASSES})
        only['total'].update(known_total_tokens=None, tokens=dict.fromkeys(CLASSES))
        _validate(seal(document))

        def ask(**filters):
            args = SimpleNamespace(**{'period': 'week', 'group': 'runtime', 'agent': None, 'work_item_id': None,
                                      'runtime': None, **filters})
            with mock.patch.object(query, 'load_cost_dashboard', return_value=document):
                src = query.Sources(app_snapshot=Path('/nonexistent'), cost_publication=Path('/x'),
                                    now=NOW + timedelta(minutes=1))
                return query.q_cost(src, args)
        whole = ask()
        (group,) = whole['groups']
        published = whole['summary']['total']
        self.assertEqual((published['tokens']['output'], published['known_total_tokens']), (1, 2))
        for name, owner in (('group', group), ('filtered summary', ask(runtime='pi')['summary'])):
            with self.subTest(name):
                self.assertEqual(owner['adjustment']['tokens']['output'], 0)
                self.assertEqual(owner['total'], published)
        # Only the row without events: its null is zero, so its total is its entries.
        self.assertEqual(ask(agent='duke')['summary']['total']['tokens']['output'], -3)
        # A class a row with events does not know stays null; the known classes still add up.
        row = view['groups'][0]
        row['usage']['tokens']['cache_read'] = {'known': None, 'unknown_event_count': 1}
        row['total']['tokens']['cache_read'] = None
        for name, owner in (('group', ask()['groups'][0]), ('filtered summary', ask(runtime='pi')['summary'])):
            with self.subTest('mixed ' + name):
                self.assertIsNone(owner['total']['tokens']['cache_read'])
                self.assertEqual(owner['total']['tokens']['output'], 1)
                self.assertEqual(owner['adjustment']['tokens']['cache_read'], 0)

    def test_query_of_last_week_names_its_close(self):
        document, _ = self.last_week(None)
        args = SimpleNamespace(period='last_week', group=None, agent=None, work_item_id=None, runtime=None)
        with mock.patch.object(query, 'load_cost_dashboard', return_value=document):
            src = query.Sources(app_snapshot=Path('/nonexistent'), cost_publication=Path('/x'),
                                now=NOW + timedelta(minutes=1))
            self.assertIn('closed_week', query.q_cost(src, args))
            self.assertIsNone(query.q_cost(src, args)['closed_week'])
        # A publication that does not close weeks says nothing; null would read as "not yet closed".
        with mock.patch.object(query, 'load_cost_dashboard', return_value=self.last_week()[0]):
            src = query.Sources(app_snapshot=Path('/nonexistent'), cost_publication=Path('/x'),
                                now=NOW + timedelta(minutes=1))
            result = query.q_cost(src, args)
        self.assertNotIn('closed_week', result)
        self.assertNotIn('closed_week', result['summary'])

    def test_snapshot_exporter_declares_the_capability(self):
        self.assertTrue(adjustments.probe())
        result = subprocess.run([sys.executable, '-I', '-B', str(ROOT / 'Stats/exporter/pulse-stats-snapshot.py'),
                                 '--probe-cost-adjustments', '--json'], capture_output=True, check=True, timeout=30)
        self.assertEqual(json.loads(result.stdout), {'schema': adjustments.PROBE_SCHEMA})


if __name__ == '__main__':
    unittest.main()
