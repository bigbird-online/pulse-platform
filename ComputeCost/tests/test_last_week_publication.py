"""The previous complete Monday..Sunday is its own publication beside the current views."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import contextlib
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'Stats/exporter'))
from pulse_compute_cost.ledger import UsageLedger
from pulse_compute_cost.receipts import digest
from pulse_compute_cost.receipt_sources import usage_receipt, actor_receipt
from pulse_compute_cost.stats_dashboard import (
    CURRENT_PERIODS, DEFAULT_FACTS, LAST_WEEK, build_dashboard, main, period_window)
from pulse_stats import query_cli
from pulse_stats.cost_dashboard import _validate, load_cost_dashboard

ZONE = ZoneInfo('Asia/Shanghai')
# Monday 2026-11-09 08:20 in Asia/Shanghai, just before the weekly review.
NOW = datetime(2026, 11, 9, 0, 20, tzinfo=timezone.utc)
CATALOG = {'retrieved_at': NOW.isoformat(), 'catalog': {'data': [{'id': 'openai/test', 'pricing': {
    'prompt': '1', 'completion': '1', 'input_cache_read': '0.1', 'input_cache_write': '2'}}]}}


def used(identity, local, wi='wi_20261028_222', amount=1):
    at = local.replace(tzinfo=ZONE).astimezone(timezone.utc).isoformat()
    row = usage_receipt('pi', identity, {'input': amount, 'output': 0, 'cacheRead': 0, 'cacheWrite': 0},
        at=at, observed=at, model='openai/test',
        common={'wi': wi, 'executor': 'duke', 'responsible': 'duke', 'delegator': None})
    return [row, actor_receipt(row, node='ownermac', executor='duke', entrypoint='cli', observed=at)]


class LastWeekPublicationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        self.ledger = UsageLedger(self.dir / 'ledger', clock=lambda: NOW)
        self.facts = json.loads(DEFAULT_FACTS.read_text())
        self.ledger.ingest_receipts([
            *used('before', datetime(2026, 11, 1, 23, 59)),       # Sunday of the week before
            *used('monday', datetime(2026, 11, 2, 0, 0)),          # first instant of last week
            *used('sunday', datetime(2026, 11, 8, 23, 59), amount=2),  # last minute of last week
            *used('today', datetime(2026, 11, 9, 0, 10)),          # this week, not last week
        ])

    def build(self, periods):
        return build_dashboard(self.ledger, subscriptions=self.facts, source_revision='a' * 40, now=NOW,
            identity_dimensions=True, activity_table=True, activity_only=True, catalog_snapshot=CATALOG,
            periods=periods)

    def test_window_is_previous_monday_to_this_monday(self):
        for day in ('2026-11-09', '2026-11-12', '2026-11-15'):
            start, end = period_window(datetime.fromisoformat(day).date(), 'last_week')
            self.assertEqual((start.isoformat(), end.isoformat()), ('2026-11-02', '2026-11-09'))
        start, end = period_window(datetime(2026, 11, 9).date(), 'week')
        self.assertEqual((start.isoformat(), end.isoformat()), ('2026-11-09', '2026-11-10'))

    def test_closed_week_counts_exactly_its_seven_days(self):
        doc = self.build(LAST_WEEK)
        self.assertEqual(doc['status'], 'available')
        [view] = doc['views']
        self.assertEqual((view['period'], view['dimension']), ('last_week', 'activity'))
        self.assertEqual((view['query']['from_date'], view['query']['to_date']), ('2026-11-02', '2026-11-09'))
        self.assertEqual(view['summary']['usage']['event_count'], 2)
        self.assertEqual(view['summary']['cost']['partial_machine_cash_usd'], '3')
        _validate(doc)
        # The current publication keeps its own three windows.
        current = self.build(CURRENT_PERIODS)
        self.assertEqual([v['period'] for v in current['views']], ['today', 'week', 'month'])
        week = next(v for v in current['views'] if v['period'] == 'week')
        self.assertEqual((week['query']['from_date'], week['query']['to_date']), ('2026-11-09', '2026-11-10'))
        _validate(current)

    def test_exporter_rejects_a_mislabelled_or_mixed_week(self):
        doc = self.build(LAST_WEEK)
        mislabelled = deepcopy(doc)
        mislabelled['views'][0]['query']['from_date'] = '2026-11-03'
        mixed = deepcopy(doc)
        mixed['views'].append(deepcopy(self.build(CURRENT_PERIODS)['views'][0]))
        for bad in (mislabelled, mixed):
            bad['sha256'] = digest({k: v for k, v in bad.items() if k != 'sha256'})
            with self.assertRaises(ValueError):
                _validate(bad)

    def test_closed_week_is_only_the_joint_activity_view(self):
        with self.assertRaises(ValueError):
            build_dashboard(self.ledger, subscriptions=self.facts, source_revision='a' * 40, now=NOW,
                            identity_dimensions=True, catalog_snapshot=CATALOG, periods=LAST_WEEK)
        with self.assertRaises(ValueError):
            self.build(('week', 'last_week'))

    def test_main_publishes_both_and_the_query_reads_last_week(self):
        # The command publishes as of the real clock: one fact inside its last week.
        monday, _ = period_window(datetime.now(ZONE).date(), 'last_week')
        ledger = UsageLedger(self.dir / 'live')
        ledger.ingest_receipts(used('live', datetime.combine(monday, datetime.min.time()) + timedelta(hours=9)))
        catalog = self.dir / 'catalog.json'
        catalog.write_text(json.dumps({**CATALOG, 'retrieved_at': datetime.now(timezone.utc).isoformat()}))
        catalog.chmod(0o600)
        state = self.dir / 'state'; state.mkdir(mode=0o700)
        current, closed = state / 'cost-dashboard-v1.json', state / 'cost-last-week-v1.json'
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            code = main(['--ledger-root', str(self.dir / 'live'), '--catalog-snapshot', str(catalog),
                         '--source-revision', 'a' * 40, '--output', str(current),
                         '--last-week-output', str(closed)])
        self.assertEqual(code, 0)
        published = load_cost_dashboard(closed)
        self.assertEqual(published['status'], 'available')
        today = datetime.fromisoformat(published['generated_at']).astimezone(ZONE).date()
        start, end = period_window(today, 'last_week')
        [view] = published['views']
        self.assertEqual((view['query']['from_date'], view['query']['to_date']),
                         (start.isoformat(), end.isoformat()))
        self.assertEqual(view['summary']['usage']['event_count'], 1)
        self.assertEqual(load_cost_dashboard(current)['status'], 'available')
        # The Agent query takes last_week from its own publication by default.
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            query_cli.main(['cost', '--period', 'last_week'], home=self.dir / 'home',
                           now=datetime.fromisoformat(published['generated_at']) + timedelta(minutes=1))
        self.assertEqual(json.loads(out.getvalue())['sources']['cost_publication']['status'], 'unavailable')
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            query_cli.main(['cost', '--period', 'last_week', '--cost-dashboard', str(closed)], home=self.dir,
                           now=datetime.fromisoformat(published['generated_at']) + timedelta(minutes=1))
        answer = json.loads(out.getvalue())
        self.assertEqual(answer['result']['period'], 'last_week')
        self.assertEqual((answer['result']['summary']['from_date'], answer['result']['summary']['to_date']),
                         (start.isoformat(), end.isoformat()))
        self.assertEqual(query_cli.default_cost_publication(self.dir, 'last_week').name, 'cost-last-week-v1.json')
        self.assertEqual(query_cli.default_cost_publication(self.dir, 'week').name, 'cost-dashboard-v1.json')


if __name__ == '__main__':
    unittest.main()
