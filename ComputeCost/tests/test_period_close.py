"""Closed weeks and adjustment entries (WI754 step 5): invariants against a synthetic resolved store."""
from collections import Counter
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
import json
from pathlib import Path
import random
import sqlite3
import sys
import tempfile
import unittest
from unittest import mock
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'Stats/exporter'))
from pulse_compute_cost import period_close  # noqa: E402
from pulse_compute_cost.period_close import PeriodClose, adjustments_for, publisher_round, with_adjustments  # noqa: E402
from pulse_compute_cost.resolved_store import _TABLES, DIMENSIONS  # noqa: E402
from pulse_compute_cost.receipts import TOKEN_FIELDS, digest  # noqa: E402

ZONE = ZoneInfo('Asia/Shanghai')
BINDING = {'sequence': 1, 'head_sha256': 'h', 'engine': 'e', 'catalog_sha256': None}


def at(day, hour=9):
    return datetime.combine(date.fromisoformat(day), datetime.min.time(), ZONE) + timedelta(hours=hour)



def _timing(stderr):
    """The calculation's timing record; other stderr lines (warnings, logs) are not it."""
    for line in stderr.splitlines():
        if line.startswith('{'):
            record = json.loads(line)
            if record.get('schema') == 'stats_cost_calculation_timing/v1':
                return record
    raise AssertionError(f'no timing record on stderr: {stderr[:500]!r}')

class Prices:
    """Per-model price of one token; a missing model is unpriced."""
    def __init__(self, **rates):
        self.rates = {k: Decimal(v) for k, v in rates.items()}

    def __call__(self, model, tokens):
        if model not in self.rates or None in tokens:
            return None, 'missing_price_or_model'
        return str(self.rates[model] * sum(tokens)), None


class Store:
    """usage_resolved rows by (entity, week, part), as the resolved store writes them."""
    def __init__(self, path):
        self.path = path
        self.db = sqlite3.connect(path, isolation_level=None)
        self.db.executescript(_TABLES)
        self.db.executemany('INSERT INTO meta VALUES (?, ?)', [('sequence', '1'), ('head_sha256', 'h')])

    def put(self, entity, day, *, agent='pulse', wi='wi_20261101_001', model='m', tokens=(1, 0, 0, 1), included=1,
            week=None, hour=9):
        moment = at(day, hour).astimezone(timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.%fZ')
        monday = date.fromisoformat(day) - timedelta(days=date.fromisoformat(day).weekday())
        self.db.execute('DELETE FROM usage_resolved WHERE entity_ref = ?', (bytes([entity]) * 32,))
        keys = {'tenant': 'unassigned', 'agent': agent, 'wi': wi, 'entrypoint': 'cli', 'node': 'n', 'runtime': 'pi',
                'model': model}
        self.db.execute('INSERT INTO usage_resolved VALUES (' + ','.join('?' * 21) + ')', (
            bytes([entity]) * 32, monday.isoformat() if week is None else week, '', 0, included, 'point', moment, moment,
            *(keys[d] for d in DIMENSIONS), *tokens, sum(tokens), 1))

    def put_metered(self, entity, days, *, included=1):
        """A metered entity: one row per local day of its metering points, `days` maps a day to output tokens."""
        self.db.execute('DELETE FROM usage_resolved WHERE entity_ref = ?', (bytes([entity]) * 32,))
        keys = {'tenant': 'unassigned', 'agent': 'pulse', 'wi': 'wi_20261101_001', 'entrypoint': 'cli', 'node': 'n',
                'runtime': 'pi', 'model': 'm'}
        for day, output in days.items():
            moment = at(day).astimezone(timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.%fZ')
            monday = date.fromisoformat(day) - timedelta(days=date.fromisoformat(day).weekday())
            self.db.execute('INSERT INTO usage_resolved VALUES (' + ','.join('?' * 21) + ')', (
                bytes([entity]) * 32, monday.isoformat(), day, 0, included, 'metered', moment, moment,
                *(keys[d] for d in DIMENSIONS), 0, 0, 0, output, None, 1))

    def drop(self, entity):
        self.db.execute('DELETE FROM usage_resolved WHERE entity_ref = ?', (bytes([entity]) * 32,))

    def totals(self, before):
        """Included tokens by key over the weeks before `before`."""
        result = Counter()
        for row in self.db.execute('SELECT ' + ', '.join((*DIMENSIONS, *TOKEN_FIELDS)) + ' FROM usage_resolved '
                                   "WHERE included = 1 AND week_start != '' AND week_start < ?", (before,)):
            for i, k in enumerate(TOKEN_FIELDS):
                result[(row[:len(DIMENSIONS)], k)] += row[len(DIMENSIONS) + i]
        return result


def booked_totals(close):
    result = Counter()
    for row in close._db.execute('SELECT ' + ', '.join((*DIMENSIONS, *TOKEN_FIELDS)) + ' FROM booked WHERE included = 1'):
        for i, k in enumerate(TOKEN_FIELDS):
            result[(row[:len(DIMENSIONS)], k)] += row[len(DIMENSIONS) + i]
    return result


def entry_totals(entries):
    result = Counter()
    for e in entries:
        for i, k in enumerate(TOKEN_FIELDS):
            result[(tuple(e['key'][d] for d in DIMENSIONS), k)] += e['sign'] * e['tokens'][i]
    return result


def nonzero(counter):
    return {k: v for k, v in counter.items() if v}


class PeriodCloseTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(); self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        self.store = Store(self.dir / 'resolved.sqlite')
        self.close = PeriodClose.open(self.dir / 'period-close.sqlite', self.dir / 'resolved.sqlite')
        self.addCleanup(self.close.close_connection)
        self.prices = Prices(m='0.5', n='2')

    def enabled(self):
        # Three weeks of usage: 11-02, 11-09 and the open week of 11-16.
        for entity, day in ((1, '2026-11-03'), (2, '2026-11-04'), (3, '2026-11-10'), (4, '2026-11-12'),
                            (5, '2026-11-17')):
            self.store.put(entity, day)
        self.store.put(6, '2026-11-05', week='')  # A cross-week interval: no week.
        self.assertTrue(self.close.enable(at('2026-11-18'), self.prices, BINDING))
        return self.close.refresh_adjustments(date(2026, 11, 18), self.prices)

    def test_enable_books_earlier_weeks_without_entries_or_undated_rows(self):
        self.assertEqual(self.enabled(), [])
        self.assertEqual(self.close.closed_through(), date(2026, 11, 16))
        self.assertEqual(self.close._db.execute('SELECT COUNT(*) FROM booked').fetchone()[0], 4)
        weeks = [w for (w,) in self.close._db.execute('SELECT week_start FROM closed_week ORDER BY 1')]
        self.assertEqual(weeks, ['2026-11-02', '2026-11-09'])
        self.assertFalse(self.close.enable(at('2026-11-18'), self.prices, BINDING))

    def test_each_kind_of_difference_and_the_conservation(self):
        self.enabled()
        self.store.put(1, '2026-11-03', agent='duke')               # reattributed
        self.store.put(2, '2026-11-04', tokens=(5, 0, 0, 1))        # quantity revised
        self.store.put(7, '2026-11-11')                             # late usage of a closed week
        self.store.put(3, '2026-11-10', included=0)                 # withdrawn
        self.store.put(8, '2026-11-19')                             # open week: own usage, no entry
        self.store.put(9, '2026-11-06', week='')                    # no week: never an entry
        entries = self.close.refresh_adjustments(date(2026, 11, 19), self.prices)
        reasons = Counter((e['reason'], e['sign']) for e in entries)
        self.assertEqual(reasons, Counter({('reattributed', -1): 1, ('reattributed', 1): 1, ('quantity_revised', -1): 1,
                                           ('quantity_revised', 1): 1, ('late_usage', 1): 1, ('withdrawn', -1): 1}))
        reattributed = [e['amount'] for e in entries if e['reason'] == 'reattributed']
        self.assertEqual(sum(reattributed), 0)
        self.assertEqual(reattributed[1], Decimal('1.0'))
        revised = {e['sign']: e['amount'] for e in entries if e['reason'] == 'quantity_revised'}
        self.assertEqual(revised, {-1: Decimal('-1.0'), 1: Decimal('3.0')})
        self.assertEqual(booked_totals(self.close) + entry_totals(entries), +self.store.totals('2026-11-16'))
        # A revision changed back leaves no entry.
        self.store.put(1, '2026-11-03')
        later = self.close.refresh_adjustments(date(2026, 11, 20), self.prices)
        self.assertNotIn('reattributed', {e['reason'] for e in later})
        self.assertTrue(all(e['booked_on'] == '2026-11-19' for e in later), 'an entry keeps its first day')

    def test_random_revisions_conserve_every_round(self):
        rng = random.Random(754)
        self.enabled()
        for round_ in range(30):
            entity = rng.randrange(1, 12)
            day = rng.choice(['2026-11-03', '2026-11-05', '2026-11-10', '2026-11-13', '2026-11-17'])
            choice = rng.random()
            if choice < 0.2:
                self.store.drop(entity)
            else:
                self.store.put(entity, day, agent=rng.choice(['pulse', 'duke']), model=rng.choice(['m', 'n', 'x']),
                               tokens=(rng.randrange(3), 0, 0, rng.randrange(3)), included=int(choice > 0.3))
            entries = self.close.refresh_adjustments(date(2026, 11, 18), self.prices)
            self.assertEqual(booked_totals(self.close) + entry_totals(entries), +self.store.totals('2026-11-16'),
                             f'round {round_}')
            by_entity = {}
            for e in entries:
                if e['reason'] == 'reattributed' and e['amount'] is not None:
                    by_entity.setdefault(e['week_start'], []).append(e['amount'])
            self.assertTrue(all(sum(v) == 0 for v in by_entity.values()))

    def test_a_price_change_alone_books_nothing(self):
        self.enabled()
        self.assertEqual(self.close.refresh_adjustments(date(2026, 11, 19), Prices(m='9', n='9')), [])

    def test_a_metered_entity_is_judged_day_by_day(self):
        """Late usage metered on 11-30 (2 tokens) and 12-01 (5): December owns the five, so its entries add two."""
        self.store.put(1, '2026-12-02')
        self.assertTrue(self.close.enable(at('2026-12-08'), self.prices, BINDING))  # The week of 11-30 is closed.
        self.store.put_metered(7, {'2026-11-30': 2, '2026-12-01': 5})
        entries = self.close.refresh_adjustments(date(2026, 12, 8), self.prices)
        self.assertEqual(sorted((e['origin_day'], e['tokens'][3], e['reason']) for e in entries),
                         [('2026-11-30', 2, 'late_usage'), ('2026-12-01', 5, 'late_usage')])

        def output(lo, hi):
            return sum(e['sign'] * e['tokens'][3] for e in adjustments_for(entries, lo, hi))
        self.assertEqual(output('2026-12-01', '2027-01-01'), 2, 'December: 5 own plus 2 adjusted is 7')
        self.assertEqual(output('2026-12-07', '2026-12-14'), 7, 'the open week owns neither day')
        self.assertEqual(booked_totals(self.close) + entry_totals(entries), +self.store.totals('2026-12-07'))
        # One day revised: only that day's entry changes; the close books each day.
        self.store.put_metered(7, {'2026-11-30': 2, '2026-12-01': 6})
        self.assertEqual(output('2026-12-01', '2027-01-01'), 2)
        self.close.close('2026-12-07', {'period': 'last_week', 'watermark': 1}, [], at('2026-12-14', 1), BINDING,
                         self.dir / 'closed-weeks', self.prices)
        self.assertEqual(self.close.refresh_adjustments(date(2026, 12, 14), self.prices), [])
        self.store.put_metered(7, {'2026-11-30': 2})
        (withdrawn,) = self.close.refresh_adjustments(date(2026, 12, 14), self.prices)
        self.assertEqual((withdrawn['reason'], withdrawn['origin_day'], withdrawn['sign'] * withdrawn['tokens'][3]),
                         ('withdrawn', '2026-12-01', -6))

    def test_a_rebooked_row_carries_the_amount_its_entry_booked(self):
        """Excluded at $1, included at $2: the close books $2, so a later withdrawal takes back $2."""
        self.store.put(1, '2026-11-10', tokens=(1, 0, 0, 0), included=0)
        self.assertTrue(self.close.enable(at('2026-11-18'), Prices(m='1'), BINDING))
        dearer = Prices(m='2')
        self.store.put(1, '2026-11-10', tokens=(1, 0, 0, 0))
        (late,) = self.close.refresh_adjustments(date(2026, 11, 19), dearer)
        self.assertEqual((late['reason'], late['amount']), ('late_usage', Decimal('2')))
        self.close.close('2026-11-16', {'period': 'last_week', 'watermark': 1}, [], at('2026-11-23', 1), BINDING,
                         self.dir / 'closed-weeks', dearer)
        self.store.put(1, '2026-11-10', tokens=(1, 0, 0, 0), included=0)
        (withdrawn,) = self.close.refresh_adjustments(date(2026, 11, 24), Prices(m='3'))
        self.assertEqual((withdrawn['reason'], withdrawn['amount']), ('withdrawn', Decimal('-2')))
        self.assertEqual(late['amount'] + withdrawn['amount'], 0)

    def test_due_closes_complete_at_once_and_incomplete_from_six(self):
        self.enabled()
        read = {'id': 'oracle', 'status': 'read', 'collected_from': at('2026-11-23', 0).isoformat()}
        early = {**read, 'collected_from': at('2026-11-22', 23).isoformat()}
        paused = {'id': 'tcloud', 'status': 'paused'}
        self.assertIsNone(self.close.due(at('2026-11-22', 23), [read]), 'not before Monday')
        self.assertEqual(self.close.due(at('2026-11-23', 1), [read, paused]), ('2026-11-16', []))
        self.assertIsNone(self.close.due(at('2026-11-23', 1), [early]))
        self.assertIsNone(self.close.due(at('2026-11-23', 1), [read], local_pending=True))
        self.assertEqual(self.close.due(at('2026-11-23', 6), [early, {'id': 'mac', 'status': 'unreachable_or_invalid',
                                                                       'reason': 'peer_push_stale'}]),
                         ('2026-11-16', [{'peer': 'mac', 'reason': 'peer_push_stale'},
                                         {'peer': 'oracle', 'reason': 'collected_before_week_end'}]))

    def close_week(self):
        view = {'period': 'last_week', 'watermark': 1}
        return self.close.close('2026-11-16', view, [], at('2026-11-23', 1), BINDING, self.dir / 'closed-weeks',
                                self.prices)

    def test_close_archives_once_rebooks_and_never_closes_twice(self):
        self.enabled()
        self.store.put(7, '2026-11-11')
        self.assertEqual(len(self.close.refresh_adjustments(date(2026, 11, 20), self.prices)), 1)
        archive = self.close_week()
        self.assertEqual(self.close.closed_through(), date(2026, 11, 23))
        self.assertEqual(self.close.refresh_adjustments(date(2026, 11, 23), self.prices), [])
        self.assertEqual(self.close.archive('2026-11-16', self.dir / 'closed-weeks')['view'],
                         {'period': 'last_week', 'watermark': 1})
        self.assertIsNone(self.close.due(at('2026-11-23', 9), []))
        with self.assertRaises(ValueError):
            self.close_week()
        archive.write_text('{}')
        with self.assertRaises(ValueError):
            self.close.archive('2026-11-16', self.dir / 'closed-weeks')

    def test_a_close_stopped_midway_repeats_with_the_same_result(self):
        self.enabled()
        with mock.patch.object(PeriodClose, '_rebook', side_effect=RuntimeError('stopped')):
            with self.assertRaises(RuntimeError):
                self.close_week()
        self.assertEqual(self.close.closed_through(), date(2026, 11, 16))
        self.assertFalse((self.dir / 'closed-weeks/cost-week-2026-11-16.json').exists())
        with mock.patch.object(period_close.os, 'replace', side_effect=OSError('stopped')):
            with self.assertRaises(OSError):
                self.close_week()
        # Committed, not yet renamed: reading the archive finishes the rename.
        self.assertEqual(self.close.archive('2026-11-16', self.dir / 'closed-weeks')['view'],
                         {'period': 'last_week', 'watermark': 1})
        self.assertTrue((self.dir / 'closed-weeks/cost-week-2026-11-16.json').exists())

    def test_a_rebuilt_store_with_the_same_rows_books_nothing(self):
        self.enabled()
        rows = self.store.db.execute('SELECT * FROM usage_resolved').fetchall()
        self.store.db.execute('DELETE FROM usage_resolved')
        self.store.db.executemany('INSERT INTO usage_resolved VALUES (' + ','.join('?' * 21) + ')', rows)
        self.assertEqual(self.close.refresh_adjustments(date(2026, 11, 19), self.prices), [])


class ViewAdjustmentTests(unittest.TestCase):
    """The publisher's adjusted views pass the exporter's validator."""

    def test_views_with_entries_and_the_closed_week_archive_validate(self):
        from pulse_compute_cost.ledger import UsageLedger
        from pulse_compute_cost.receipt_sources import usage_receipt
        from pulse_compute_cost.stats_dashboard import LAST_WEEK, build_dashboard
        from pulse_stats.cost_dashboard import _validate
        tmp = tempfile.TemporaryDirectory(); self.addCleanup(tmp.cleanup)
        directory = Path(tmp.name)
        now = at('2026-11-23', 7).astimezone(timezone.utc)
        ledger = UsageLedger(directory / 'ledger', clock=lambda: at('2026-11-18', 10))
        moment = at('2026-11-18').astimezone(timezone.utc).isoformat()
        ledger.ingest_receipts([usage_receipt('pi', 'u', {'input': 1, 'output': 1, 'cacheRead': 0, 'cacheWrite': 0},
            at=moment, observed=moment, model='openai/test',
            common={'wi': 'wi_20261101_001', 'executor': 'pulse', 'responsible': 'pulse', 'delegator': None})])
        options = dict(subscriptions={'schema_version': 'pulse_stats_procurement_facts/v1', 'subscriptions': []},
                       source_revision='c' * 40, now=now, identity_dimensions=True, activity_table=True,
                       activity_only=True)
        current = build_dashboard(ledger, **options)
        store = Store(directory / 'resolved.sqlite')
        store.put(1, '2026-11-10')
        store.put(2, '2026-11-18')
        timings = {}
        # The week before: enable; Monday 07:00: close the week of 11-16.
        publisher_round(directory / 'resolved.sqlite', build_dashboard(ledger, **{**options, 'now': at('2026-11-20').astimezone(timezone.utc)}),
                        now=at('2026-11-20'), catalog=None, peers=[], local_pending=False,
                        archive_dir=directory / 'closed-weeks', last_week=None, timings=timings)
        store.put(1, '2026-11-10', agent='duke')  # A late attribution of a closed week.
        store.put(3, '2026-11-12')                # Late usage of a closed week.
        result = publisher_round(directory / 'resolved.sqlite', current, now=now, catalog=None, peers=[],
                                 local_pending=False, archive_dir=directory / 'closed-weeks',
                                 last_week=lambda: build_dashboard(ledger, **options, periods=LAST_WEEK),
                                 timings=timings)
        self.assertEqual(timings['period_close'].get('closed'), '2026-11-16', timings)
        archived = result['last_week_view']
        self.assertEqual(archived['closed_week']['week_start'], '2026-11-16')
        self.assertEqual(archived['adjustments']['entry_count'], 3)
        self.assertEqual({o['reason'] for o in archived['adjustments']['by_origin']}, {'reattributed', 'late_usage'})
        closed = {**build_dashboard(None, **{**options, 'periods': LAST_WEEK}), 'status': 'available', 'reason': None,
                  'views': [archived]}
        closed['sha256'] = digest({k: v for k, v in closed.items() if k != 'sha256'})
        _validate(closed)
        _validate(result['document'])
        # A later entry of this week shows in today's and this week's view, not as own usage.
        store.put(3, '2026-11-12', tokens=(2, 0, 0, 2))
        later = publisher_round(directory / 'resolved.sqlite', current, now=now, catalog=None, peers=[],
                                local_pending=False, archive_dir=directory / 'closed-weeks', last_week=None,
                                timings=timings)
        week = next(v for v in later['document']['views'] if v['period'] == 'week')
        self.assertEqual(week['adjustments']['tokens']['output'], 1)
        self.assertEqual(week['summary'], next(v for v in current['views'] if v['period'] == 'week')['summary'])
        _validate(later['document'])
        self.assertEqual(adjustments_for([{'booked_on': '2026-11-23', 'origin_day': '2026-11-12'}],
                                         '2026-11-01', '2026-12-01'), [])

    def test_the_previous_week_before_its_close_shows_the_entries_its_close_would_book(self):
        from pulse_compute_cost.ledger import UsageLedger
        from pulse_compute_cost.period_close import provisional_last_week
        from pulse_compute_cost.receipt_sources import usage_receipt
        from pulse_compute_cost.stats_dashboard import LAST_WEEK, build_dashboard
        from pulse_stats.cost_dashboard import _validate
        tmp = tempfile.TemporaryDirectory(); self.addCleanup(tmp.cleanup)
        directory = Path(tmp.name)
        ledger = UsageLedger(directory / 'ledger', clock=lambda: at('2026-11-18', 10))
        moment = at('2026-11-18').astimezone(timezone.utc).isoformat()
        ledger.ingest_receipts([usage_receipt('pi', 'u', {'input': 1, 'output': 1, 'cacheRead': 0, 'cacheWrite': 0},
            at=moment, observed=moment, model='openai/test',
            common={'wi': 'wi_20261101_001', 'executor': 'pulse', 'responsible': 'pulse', 'delegator': None})])
        options = dict(subscriptions={'schema_version': 'pulse_stats_procurement_facts/v1', 'subscriptions': []},
                       source_revision='c' * 40, identity_dimensions=True, activity_table=True, activity_only=True)
        store = Store(directory / 'resolved.sqlite')
        store.put(1, '2026-11-10')
        store.put(2, '2026-11-18')

        def round_at(moment, peers=()):
            timings = {}
            now = moment.astimezone(timezone.utc)
            result = publisher_round(
                directory / 'resolved.sqlite', build_dashboard(ledger, **options, now=now), now=moment, catalog=None,
                peers=list(peers), local_pending=False, archive_dir=directory / 'closed-weeks',
                last_week=lambda: build_dashboard(ledger, **options, now=now, periods=LAST_WEEK), timings=timings)
            return result, timings['period_close'], build_dashboard(ledger, **options, now=now, periods=LAST_WEEK)

        round_at(at('2026-11-20'))                 # Enable.
        store.put(3, '2026-11-12')                 # Friday: late usage of a closed week.
        self.assertEqual(round_at(at('2026-11-20', 10))[1]['open_entries'], 1)
        store.put(4, '2026-11-11')                 # Found on Monday, before the close.
        waiting = [{'id': 'mac', 'status': 'pending', 'reason': 'peer_collection_pending'}]
        result, record, last = round_at(at('2026-11-23', 1), waiting)
        self.assertNotIn('closed', record)
        self.assertEqual(record['open_entries'], 2)
        self.assertIsNone(result['last_week_view'])
        plain = last['views'][0]
        view = provisional_last_week(plain, result)
        self.assertIsNone(view['closed_week'])
        self.assertEqual(view['adjustments']['entry_count'], 2)
        last['views'] = [view]
        last['sha256'] = digest({k: v for k, v in last.items() if k != 'sha256'})
        _validate(last)
        # The open week does not count them as well, and the close archives the same entries.
        week = next(v for v in result['document']['views'] if v['period'] == 'week')
        self.assertEqual(week['adjustments']['entry_count'], 0)
        closed, record, _ = round_at(at('2026-11-23', 7), waiting)
        self.assertEqual(record['closed'], '2026-11-16')
        self.assertEqual(closed['last_week_view']['adjustments'], view['adjustments'])
        # A view at another watermark is only marked as not closed.
        self.assertNotIn('adjustments', provisional_last_week({**plain, 'watermark': 2}, result))

    def rounds(self, work_items=None, wis=('wi_20261101_001',)):
        """A ledger with usage on 11-18 and a store enabled on 11-20; returns round_at(moment) and the store."""
        from pulse_compute_cost.ledger import UsageLedger
        from pulse_compute_cost.receipt_sources import usage_receipt
        from pulse_compute_cost.stats_dashboard import LAST_WEEK, build_dashboard
        tmp = tempfile.TemporaryDirectory(); self.addCleanup(tmp.cleanup)
        directory = Path(tmp.name)
        ledger = UsageLedger(directory / 'ledger', clock=lambda: at('2026-11-18', 10))
        moment = at('2026-11-18').astimezone(timezone.utc).isoformat()
        ledger.ingest_receipts([usage_receipt(
            'pi', 'u' + wi, {'input': 1, 'output': 1, 'cacheRead': 0, 'cacheWrite': 0}, at=moment, observed=moment,
            model='openai/test', common={'wi': wi, 'executor': 'pulse', 'responsible': 'pulse', 'delegator': None})
            for wi in wis])
        options = dict(subscriptions={'schema_version': 'pulse_stats_procurement_facts/v1', 'subscriptions': []},
                       source_revision='c' * 40, identity_dimensions=True, activity_table=True, activity_only=True)
        store = Store(directory / 'resolved.sqlite')
        store.put(2, '2026-11-18')

        def round_at(moment):
            now = moment.astimezone(timezone.utc)
            result = publisher_round(
                directory / 'resolved.sqlite', build_dashboard(ledger, **options, now=now, work_items=work_items),
                now=moment, catalog=None, peers=[], local_pending=False, archive_dir=directory / 'closed-weeks',
                last_week=lambda: build_dashboard(ledger, **options, now=now, periods=LAST_WEEK, work_items=work_items),
                timings={})
            template = build_dashboard(None, **options, now=now, periods=LAST_WEEK)
            return result, template
        round_at(at('2026-11-20'))
        return round_at, store, directory

    def test_a_month_keeps_the_entries_booked_in_it_after_their_week_closes(self):
        """Usage of 10-30 arrives on 11-20: November shows its 5 output before and after the close of 11-23."""
        round_at, store, _ = self.rounds()
        store.put(3, '2026-10-30', tokens=(0, 0, 0, 5))

        def output(result, period):
            block = next(v for v in result['document']['views'] if v['period'] == period)['adjustments']
            return block['entry_count'], block['tokens']['output']
        self.assertEqual(output(round_at(at('2026-11-20', 10))[0], 'month'), (1, 5))
        closed = round_at(at('2026-11-23', 7))[0]
        self.assertEqual(closed['last_week_view']['adjustments']['tokens']['output'], 5)
        for result in (closed, round_at(at('2026-11-24'))[0]):
            self.assertEqual(output(result, 'month'), (1, 5))
            self.assertEqual(output(result, 'week'), (0, 0))
            from pulse_stats.cost_dashboard import _validate
            _validate(result['document'])
        # Withdrawn in the next week: the month nets to nothing, the open week shows the withdrawal.
        store.put(3, '2026-10-30', tokens=(0, 0, 0, 5), included=0)
        later = round_at(at('2026-11-25'))[0]
        self.assertEqual(output(later, 'month'), (2, 0))
        self.assertEqual(output(later, 'week'), (1, -5))

    def test_the_closed_week_publication_keeps_the_work_items_its_families_need(self):
        from pulse_compute_cost.period_close import closed_week_publication
        from pulse_compute_cost.stats_dashboard_wire import dashboard_bytes
        from pulse_stats.cost_dashboard import _validate, load_cost_dashboard
        parent, child = 'wi_20261101_001', 'wi_20261101_002'
        display = {parent: {'title': 'parent', 'summary': None, 'status': None},
                   child: {'title': 'child', 'summary': None, 'status': None, 'parent': parent}}
        round_at, _, directory = self.rounds(work_items=display, wis=(parent, child))
        now = at('2026-11-23', 7)
        closing, template = round_at(now)
        self.assertEqual(closing['last_week_view']['work_item_families'][0]['children'], [child])
        closed = closed_week_publication(template, closing)
        self.assertEqual([w.get('parent') for w in closed['work_items']], [None, parent])
        _validate(closed)
        path = directory / 'cost-last-week-v1.json'
        path.write_bytes(dashboard_bytes(closed)); path.chmod(0o600)
        loaded = load_cost_dashboard(path, now=now.astimezone(timezone.utc))
        self.assertEqual((loaded['status'], loaded.get('reason')), ('available', None))
        # The archive, not this round's registry, is what later rounds publish.
        again = closed_week_publication(template, round_at(at('2026-11-24'))[0])
        self.assertEqual((again['views'], again['work_items']), (closed['views'], closed['work_items']))

    def test_main_closes_only_for_readers_that_declare_adjustments(self):
        import contextlib
        import io
        from pulse_compute_cost.ledger import UsageLedger
        from pulse_compute_cost.receipt_sources import usage_receipt
        from pulse_compute_cost.stats_dashboard import main
        tmp = tempfile.TemporaryDirectory(); self.addCleanup(tmp.cleanup)
        directory = Path(tmp.name)
        moment = datetime.now(timezone.utc).isoformat()
        UsageLedger(directory / 'ledger').ingest_receipts([usage_receipt(
            'pi', 'u', {'input': 1, 'output': 1, 'cacheRead': 0, 'cacheWrite': 0}, at=moment, observed=moment,
            model='openai/test', common={'wi': 'wi_20261101_001', 'executor': 'pulse', 'responsible': 'pulse',
                                         'delegator': None})])
        state = directory / 'state'; state.mkdir(mode=0o700)
        for ready, expected in ((lambda encoding='row_contexts': encoding != 'adjustments', 'reader_not_ready'),
                                (lambda encoding='row_contexts': True, 'not_available')):
            errors = io.StringIO()
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(errors):
                code = main(['--ledger-root', str(directory / 'ledger'), '--source-revision', 'a' * 40,
                             '--output', str(state / 'cost-dashboard-v1.json'), '--close-weeks'], row_contexts_ready=ready)
            self.assertEqual(code, 0)
            timing = _timing(errors.getvalue())
            self.assertEqual(timing['period_close'], expected)
            self.assertNotIn('adjustments', json.loads((state / 'cost-dashboard-v1.json').read_text())['views'][0])

    def test_a_closed_week_stays_published_from_its_archive_when_a_reader_probe_fails(self):
        """Real main -> cost-last-week-v1.json -> reader: a failed probe after the close never recomputes the week."""
        import contextlib
        import io
        from pulse_compute_cost import stats_dashboard
        from pulse_compute_cost.incremental_views import rebuild
        from pulse_compute_cost.ledger import UsageLedger
        from pulse_compute_cost.receipt_sources import usage_receipt
        from pulse_stats.cost_dashboard import load_cost_dashboard
        tmp = tempfile.TemporaryDirectory(); self.addCleanup(tmp.cleanup)
        directory = Path(tmp.name)
        state = directory / 'state'; state.mkdir(mode=0o700)
        closed_path = state / 'cost-last-week-v1.json'

        def ingest(identity, day, clock):
            moment = at(day).astimezone(timezone.utc).isoformat()
            UsageLedger(directory / 'ledger', clock=lambda: at(clock)).ingest_receipts([usage_receipt(
                'pi', identity, {'input': 1, 'output': 1, 'cacheRead': 0, 'cacheWrite': 0}, at=moment,
                observed=moment, model='openai/test', common={'wi': 'wi_20261101_001', 'executor': 'pulse',
                                                              'responsible': 'pulse', 'delegator': None})])

        def round_at(moment, ready=True):
            class Clock(datetime):
                @classmethod
                def now(cls, tz=None):
                    return moment.astimezone(tz)
            errors = io.StringIO()
            probe = lambda encoding='row_contexts': ready or encoding != 'adjustments'
            with mock.patch.object(stats_dashboard, 'datetime', Clock), \
                    contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(errors):
                code = stats_dashboard.main(
                    ['--ledger-root', str(directory / 'ledger'), '--source-revision', 'a' * 40, '--engine', 'incremental',
                     '--output', str(state / 'cost-dashboard-v1.json'), '--last-week-output', str(closed_path),
                     '--close-weeks'], row_contexts_ready=probe)
            self.assertEqual(code, 0)
            timing = _timing(errors.getvalue())
            [view] = load_cost_dashboard(closed_path, now=moment.astimezone(timezone.utc))['views']
            return timing, view

        ingest('u1', '2026-11-18', '2026-11-18')
        rebuild(UsageLedger(directory / 'ledger'), None)
        round_at(at('2026-11-20', 10))                       # Enables.
        timing, closed = round_at(at('2026-11-23', 7))      # Closes the week of 11-16.
        self.assertEqual(timing['period_close']['closed'], '2026-11-16')
        self.assertEqual((closed['summary']['usage']['event_count'], closed['watermark']), (1, 1))
        self.assertEqual(closed['closed_week']['week_start'], '2026-11-16')
        ingest('u2', '2026-11-19', '2026-11-24')             # Late usage of the closed week.
        for moment, ready in ((at('2026-11-24', 9), False), (at('2026-11-25', 9), True)):
            with self.subTest(ready=ready):
                timing, view = round_at(moment, ready)
                self.assertEqual(view, closed, 'the archived week, unchanged')
        current = json.loads((state / 'cost-dashboard-v1.json').read_text())
        week = next(v for v in current['views'] if v['period'] == 'week')
        self.assertEqual(week['adjustments']['by_origin'][0]['reason'], 'late_usage')
        # An archive that cannot be read keeps the published week rather than recomputing it.
        before = closed_path.read_bytes()
        (state / 'closed-weeks/cost-week-2026-11-16.json').write_text('{}')
        timing, _ = round_at(at('2026-11-25', 9) + timedelta(minutes=20), False)
        self.assertEqual((timing['last_week'], closed_path.read_bytes()), ('kept_closed_week', before))

    def test_a_named_tenant_without_published_identity_falls_back_to_unassigned(self):
        view = {'query': {'group_by': ['tenant', 'agent'], 'from_date': '2026-11-23', 'to_date': '2026-11-24'},
                'groups': [], 'summary': {'usage': {}, 'cost': {'machine_cash_usd': '0'}}}
        entry = {'booked_on': '2026-11-23', 'origin_day': '2026-11-12', 'week_start': '2026-11-09',
                 'reason': 'late_usage', 'sign': 1, 'tokens': (1, 0, 0, 0), 'amount': None,
                 'key': {**dict.fromkeys(DIMENSIONS, 'x'), 'tenant': 'oikos'}}
        adjusted = with_adjustments(view, [entry])
        self.assertEqual(adjusted['adjustment_only_groups'][0]['key'], {'tenant': 'unassigned', 'agent': 'x'})
        self.assertEqual(adjusted['adjustments']['unpriced_entry_count'], 1)


if __name__ == '__main__':
    unittest.main()
