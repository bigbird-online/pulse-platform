from datetime import datetime, timedelta
from decimal import Decimal
import contextlib
import io
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'exporter'))
from pulse_stats import query, query_cli  # noqa: E402

SCRIPT = ROOT / 'exporter/pulse-stats-query.py'
APP_FIXTURE = ROOT / 'PulseStats/Resources/Fixtures/cost-dashboard-v1.json'
PUBLICATION = ROOT / 'exporter/tests/fixtures/published-tenant-seven-v1.json'


def metric(mid, value, unit='count', **extra):
    return {'id': mid, 'value': value, 'unit': unit, 'status': 'available', 'quality': 'validated',
            'reason': None, 'observed_at': '2026-10-06T10:20:00+00:00', 'scope': 'fixture', **extra}


def app_snapshot(generated):
    expires = generated + timedelta(minutes=45)
    return {
        'schema_version': 'pulse.monitoring.snapshot.v2', 'generated_at': generated.isoformat(),
        'expires_at': expires.isoformat(), 'baseline_version': '0.2.1',
        'overall': {'coverage': 'partial', 'headline': '0 open incidents', 'open_incident_count': 0,
                    'runtime_health': 'ok'},
        'alert_tiers': {'act': 1, 'watch': 0, 'done': 0},
        'alerts': [{'tier': 'act', 'title': 'fixture act', 'work_item_id': 'wi_20261001_001'}],
        'capabilities': [{'id': 'oikos.platform_health', 'health': 'ok', 'implementation_status': 'active_validated',
                          'freshness': {'state': 'fresh'}, 'reason': 'fixture'}],
        'incidents': [], 'provenance': {'producer': {'name': 'fixture', 'version': '1'}, 'sources': []},
        'metrics': [
            metric('harness.wi.day_created', 2), metric('harness.wi.day_closed', 1),
            metric('harness.wi.iso_week_created', 5), metric('harness.wi.iso_week_closed', 3),
            metric('harness.wi.month_created', 9), metric('harness.wi.month_closed', 7),
            metric('relay.cc.used_percent', 55, 'percent'), metric('relay.cc.resets_at', 1793599200, 'unix_seconds'),
            metric('relay.cc.5h_used_percent', 33, 'percent'), metric('relay.codex.used_percent', 19, 'percent'),
            metric('relay.codex.resets_at', 1793603597, 'unix_seconds'),
            metric('work.board', [{'id': 'board', 'kind': 'board', 'wi_total': 2},
                                  {'id': 'wi_20261001_001', 'kind': 'wi', 'agent': 'pulse', 'work_item_id': 'wi_20261001_001'},
                                  {'id': 'wi_20261001_002', 'kind': 'wi', 'agent': 'duke', 'work_item_id': 'wi_20261001_002'}],
                   'list'),
            metric('automation.tasks', [{'id': 'morning-news', 'status': '已送达'}], 'tasks'),
            metric('oikos.platform.nodes', [{'id': 'C1-ownermac', 'status': 'ok'}], 'nodes'),
        ],
    }


class QueryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        self.publication = self.dir / 'cost.json'
        shutil.copyfile(PUBLICATION, self.publication)
        self.publication.chmod(0o600)
        self.cost = json.loads(self.publication.read_bytes())
        self.now = datetime.fromisoformat(self.cost['generated_at']) + timedelta(minutes=1)
        self.app = self.dir / 'snapshot-v2.json'
        self.app.write_text(json.dumps(app_snapshot(self.now - timedelta(minutes=5))))
        self.app.chmod(0o600)

    def run_query(self, *argv, now=None):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(query_cli.main([*argv, '--snapshot', str(self.app), '--cost-dashboard', str(self.publication)],
                                        now=now or self.now, home=self.dir), 0)
        return json.loads(out.getvalue())

    def test_every_answer_carries_schema_and_source_freshness(self):
        for name in query.QUERIES:
            if name == 'agent_day':
                continue  # Reads Git checkouts, not the App or cost publication: test_agent_day.
            argv = [name] + (['--work-item-id', 'wi_20261001_001'] if name == 'wi' else [])
            document = self.run_query(*argv)
            self.assertEqual(document['schema'], 'pulse_stats_query/v1')
            self.assertTrue(document['sources'], name)
            for entry in document['sources'].values():
                self.assertIn('generated_at', entry)
                self.assertIn('stale', entry)

    def test_cost_rows_and_group_totals_match_published_summary(self):
        document = self.run_query('cost', '--period', 'month', '--group', 'agent')
        view = next(v for v in self.cost['views'] if v['period'] == 'month')
        groups = document['result']['groups']
        self.assertEqual(sum(g['event_count'] for g in groups), view['summary']['usage']['event_count'])
        rows = self.run_query('cost', '--period', 'month')['result']['rows']
        self.assertEqual(len(rows), len(view['groups']))
        self.assertEqual(sum(Decimal(g['amount_usd']) for g in groups),
                         sum(Decimal(r['amount_usd'] or 0) for r in rows))
        self.assertEqual(document['result']['summary']['amount_usd'], query.amount(view['summary']['cost'])[0])

    def test_cost_filters_by_agent(self):
        agent = self.cost['views'][2]['groups'][0]['key']['agent']
        rows = self.run_query('cost', '--period', 'month', '--agent', agent)['result']['rows']
        self.assertTrue(rows)
        self.assertTrue(all(r['key']['agent'] == agent for r in rows))

    def test_displayed_amount_follows_app_rule(self):
        self.assertEqual(query.amount({'machine_cash_usd': '2', 'partial_machine_cash_usd': '1'}), ('2', True))
        self.assertEqual(query.amount({'machine_cash_usd': None, 'partial_machine_cash_usd': '1',
                                       'priced_machine_cash_usd': '1.5', 'priced_event_count': 3}), ('1.5', False))
        self.assertEqual(query.amount({'machine_cash_usd': None, 'partial_machine_cash_usd': '1'}), ('1', False))

    def test_overview_uses_period_metrics_and_cost(self):
        result = self.run_query('overview', '--period', 'week')['result']
        self.assertEqual(result['wi_created']['value'], 5)
        self.assertEqual(result['wi_closed']['value'], 3)
        self.assertEqual(result['platform_runtime'], 'ok')
        self.assertEqual(result['quota_used_percent'], {'cc': 55, 'codex': 19, 'grok': None})
        # This publication has no priced total: the App shows a dash, not zero.
        self.assertIsNone(result['total_cost_usd'])
        self.assertFalse(result['total_cost_complete'])
        self.assertTrue(result['active_agents'])

    def test_quotas_board_automation_health_and_wi(self):
        quotas = self.run_query('quotas')['result']
        self.assertEqual(quotas['cc']['5h_used_percent']['value'], 33)
        self.assertIsNone(quotas['grok']['used_percent'])
        board = self.run_query('board', '--agent', 'duke')['result']
        self.assertEqual([r['id'] for r in board['board']['value']], ['board', 'wi_20261001_002'])
        self.assertEqual(len(board['act']), 1)
        self.assertEqual(self.run_query('automation')['result']['value'][0]['id'], 'morning-news')
        self.assertEqual(self.run_query('health')['result']['oikos_nodes']['value'][0]['status'], 'ok')
        wi = self.run_query('wi', '--work-item-id', 'wi_20261001_001')['result']
        self.assertEqual(len(wi['board']), 1)
        self.assertEqual(len(wi['alerts']), 1)
        self.assertIsNone(wi['project_page'])
        self.assertEqual(set(wi['cost_by_period']), {'today', 'week', 'month'})

    def test_wi_with_cost_rows_reports_each_period(self):
        view = next(v for v in self.cost['views'] if v['period'] == 'month')
        target = next(g['key']['wi'] for g in view['groups'] if g['key']['wi'].startswith('wi_'))
        month = self.run_query('wi', '--work-item-id', target)['result']['cost_by_period']['month']
        self.assertTrue(month['rows'])
        self.assertEqual(month['total']['wi'], target)

    def test_metric_lookup_and_unknown_metric(self):
        self.assertEqual(self.run_query('metric', '--metric-id', 'relay.cc.used_percent')['result']['value'], 55)
        document = self.run_query('metric', '--metric-id', 'no.such.metric')
        self.assertEqual(document['status'], 'unavailable')
        self.assertIn('unknown_metric_id', document['errors'])

    def test_stale_and_missing_sources_are_reported_not_hidden(self):
        stale = self.run_query('board', now=self.now + timedelta(hours=1))
        self.assertTrue(stale['sources']['app_snapshot']['stale'])
        self.assertEqual(stale['status'], 'partial')
        self.assertIn('app_snapshot:stale', stale['errors'])
        stale_cost = self.run_query('cost', now=self.now + timedelta(hours=1))
        self.assertIn('cost_publication:snapshot_stale', stale_cost['errors'])
        self.assertEqual(stale_cost['sources']['cost_publication']['generated_at'], self.cost['generated_at'])
        self.assertTrue(stale_cost['sources']['cost_publication']['stale'])
        self.assertIsNone(stale_cost['result'])
        self.app.unlink()
        missing = self.run_query('quotas')
        self.assertIn('app_snapshot:snapshot_missing', missing['errors'])
        self.assertIsNone(missing['result']['cc'])
        self.assertIsNotNone(missing['result']['subscriptions'])

    def test_unsafe_or_unsupported_snapshots_are_rejected(self):
        self.app.chmod(0o644)
        self.assertEqual(query.read_snapshot(self.app), (None, 'snapshot_not_private'))
        self.app.chmod(0o600)
        self.app.write_text(json.dumps({'schema_version': 'pulse.monitoring.snapshot.v3'}))
        self.assertEqual(query.read_snapshot(self.app), (None, 'unsupported_snapshot_schema'))
        link = self.dir / 'link.json'
        link.symlink_to(self.app)
        self.assertEqual(query.read_snapshot(link), (None, 'symlink_snapshot'))

    def test_embedded_cost_from_app_fixture_is_reverified(self):
        target = self.dir / 'fixture.json'
        shutil.copyfile(APP_FIXTURE, target)
        target.chmod(0o600)
        out = io.StringIO()
        snapshot = json.loads(target.read_bytes())
        now = datetime.fromisoformat(snapshot['cost_dashboard']['generated_at']) + timedelta(minutes=1)
        with contextlib.redirect_stdout(out):
            query_cli.main(['cost', '--group', 'agent', '--snapshot', str(target)], now=now, home=self.dir)
        document = json.loads(out.getvalue())
        self.assertEqual(document['sources']['cost_publication']['kind'], 'snapshot_embedded')
        self.assertEqual(document['result']['dimension'], 'agent')
        self.assertTrue(document['result']['groups'])
        snapshot['cost_dashboard']['views'][0]['summary']['cost']['partial_machine_cash_usd'] = '999'
        self.assertEqual(query.embedded_cost(snapshot), (None, 'invalid_cost_snapshot'))

    def test_the_refresh_jobs_newer_snapshot_serves_queries_overnight(self):
        # The App is quiet overnight; the refresh job's copy keeps queries fresh.
        app = query_cli.default_app_snapshot(self.dir)
        agent = query_cli.default_agent_snapshot(self.dir)
        for path in (app, agent):
            path.parent.mkdir(parents=True, exist_ok=True)
        def write(path, age):
            path.write_text(json.dumps(app_snapshot(self.now - age)));path.chmod(0o600)
        write(app, timedelta(hours=3))
        self.assertEqual(query_cli.current_snapshot(self.dir), (app, 'app_cache'))
        write(agent, timedelta(minutes=10))
        self.assertEqual(query_cli.current_snapshot(self.dir), (agent, 'refresh_cache'))
        write(app, timedelta(minutes=1))
        self.assertEqual(query_cli.current_snapshot(self.dir), (app, 'app_cache'))
        agent.chmod(0o644)  # An unverifiable copy is never preferred.
        write(app, timedelta(hours=3));agent.chmod(0o644)
        self.assertEqual(query_cli.current_snapshot(self.dir), (app, 'app_cache'))

    def test_arguments_are_validated(self):
        for argv in (['wi', '--work-item-id', 'wi_bad'], ['cost', '--period', 'year'], ['cost', '--group', 'x'],
                     ['cost', '--agent', '../x'], ['nope'], ['cost', '--snapshot', 'relative.json']):
            with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
                query_cli.main(argv, now=self.now, home=self.dir)
        document = self.run_query('wi')
        self.assertEqual(document['errors'][0], 'work_item_id_required')

    def test_queries_do_not_write_sources(self):
        before = {p.name: (p.stat().st_mtime_ns, p.read_bytes()) for p in self.dir.iterdir()}
        for name in ('overview', 'cost', 'quotas', 'health', 'board', 'automation', 'snapshot'):
            self.run_query(name)
        self.assertEqual(before, {p.name: (p.stat().st_mtime_ns, p.read_bytes()) for p in self.dir.iterdir()})

    def test_script_runs_isolated(self):
        result = subprocess.run([sys.executable, '-I', '-B', str(SCRIPT), 'board', '--snapshot', str(self.app),
                                 '--format', 'table'], capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('app_snapshot', result.stdout)


if __name__ == '__main__':
    unittest.main()


def _group(key, cost=None, tokens=None, refs=()):
    return {'key': key, 'cost': cost or {}, 'usage': {'event_count': 1 if cost else 0,
                                                       'known_total_tokens': tokens},
            'outcomes': {'status': 'known', 'known_count': len(refs), 'refs': list(refs)} if refs else {}}


GENERATED = '2026-10-07T02:00:00+00:00'  # 10:00 in Asia/Shanghai.


def month_view(groups, only=(), outcomes=None, summary=None):
    return {'period': 'month', 'dimension': 'activity', 'summary': summary or {},
            'query': {'from_date': '2026-10-01', 'to_date': '2026-10-08', 'timezone': 'Asia/Shanghai'},
            'groups': list(groups), 'outcome_only_groups': list(only), 'outcomes': outcomes}


def stub(*views):
    class Src:
        def cost(self):
            return {'generated_at': GENERATED, 'views': list(views), 'work_items': []}
    return Src


def cost_args(**filters):
    return type('A', (), {'period': 'month', 'group': None, 'agent': None, 'work_item_id': None,
                          'runtime': None, **filters})()


class AggregateTests(unittest.TestCase):
    """Unknown values stay null in group totals; outcome-only rows join their group."""

    def rows(self, *groups):
        return [query._row(g, {}) for g in groups]

    def test_unpriced_group_has_no_amount_or_tokens(self):
        rows = self.rows(_group({'agent': 'a'}, {'priced_event_count': 0, 'priced_machine_cash_usd': None}))
        [g] = query.aggregate(rows, 'agent')
        self.assertIsNone(g['amount_usd'])
        self.assertIsNone(g['known_total_tokens'])
        self.assertFalse(g['amount_complete'])

    def test_partial_group_sums_only_known_values(self):
        rows = self.rows(_group({'agent': 'a'}, {'machine_cash_usd': '1.5'}, tokens=10),
                         _group({'agent': 'a'}, {'priced_event_count': 0, 'priced_machine_cash_usd': None}))
        [g] = query.aggregate(rows, 'agent')
        self.assertEqual(g['amount_usd'], '1.5')
        self.assertEqual(g['known_total_tokens'], 10)
        self.assertFalse(g['amount_complete'])

    def test_known_zero_stays_zero(self):
        rows = self.rows(_group({'agent': 'a'}, {'machine_cash_usd': '0'}, tokens=0))
        [g] = query.aggregate(rows, 'agent')
        self.assertEqual(Decimal(g['amount_usd']), 0)
        self.assertEqual(g['known_total_tokens'], 0)
        self.assertTrue(g['amount_complete'])

    def test_grouped_cost_keeps_outcome_only_rows(self):
        view = month_view([_group({'agent': 'a', 'wi': 'wi_1'}, {'machine_cash_usd': '2'}, tokens=5)],
                          [_group({'agent': 'b', 'wi': 'wi_2'}, refs=['pr#1'])])
        Src = stub(view)

        args = type('A', (), {'period': 'month', 'group': 'wi', 'agent': None, 'work_item_id': None,
                              'runtime': None})()
        groups = {g['wi']: g for g in query.q_cost(Src(), args)['groups']}
        self.assertEqual(set(groups), {'wi_1', 'wi_2'})
        self.assertEqual(groups['wi_2']['outcome_refs'], ['pr#1'])
        self.assertEqual(groups['wi_2']['outcome_only_rows'], 1)
        self.assertIsNone(groups['wi_2']['amount_usd'])
        self.assertIsNone(groups['wi_2']['amount_complete'])
        self.assertEqual(groups['wi_1']['amount_usd'], '2')


class ScopeAndDayTests(unittest.TestCase):
    """Filtered answers carry filtered totals; a past cost day never answers for today."""

    def test_filters_scope_summary_and_outcomes_and_name_platform_totals(self):
        outcomes = {'status': 'available', 'metric_scope': 'wi_outcomes_in_period',
                    'items': [{'outcome_ref': 'o_a', 'wi': 'wi_1'}, {'outcome_ref': 'o_b', 'wi': 'wi_2'}]}
        view = month_view([_group({'agent': 'a', 'wi': 'wi_1'}, {'machine_cash_usd': '10'}, refs=['o_a']),
                           _group({'agent': 'b', 'wi': 'wi_2'}, {'machine_cash_usd': '20'}, refs=['o_b'])],
                          outcomes=outcomes, summary={'cost': {'machine_cash_usd': '30'}})
        for group in (None, 'agent'):
            result = query.q_cost(stub(view)(), cost_args(agent='a', group=group))
            self.assertEqual(result['scope'], 'filtered')
            self.assertEqual(result['summary']['amount_usd'], '10')
            self.assertEqual(result['summary']['event_count'], 1)
            self.assertEqual([o['outcome_ref'] for o in result['outcomes']['items']], ['o_a'])
            self.assertEqual(result['platform']['scope'], 'global')
            self.assertEqual(result['platform']['summary']['amount_usd'], '30')
            self.assertNotIn('stages', result)
        unfiltered = query.q_cost(stub(view)(), cost_args())
        self.assertEqual((unfiltered['scope'], unfiltered['summary']['amount_usd']), ('global', '30'))

    def test_a_view_not_covering_its_own_day_is_not_used(self):
        stale = month_view([])
        stale['query'] = {**stale['query'], 'from_date': '2026-09-01', 'to_date': '2026-10-07'}
        self.assertEqual(query.q_cost(stub(stale)(), cost_args()), {'period': 'month', 'status': 'view_unavailable'})
        self.assertEqual(query.period_bounds(query.cost_day(GENERATED), 'week'), ('2026-10-05', '2026-10-08'))

    def test_yesterdays_publication_is_unavailable_after_midnight(self):
        published = {'status': 'available', 'generated_at': '2026-10-06T15:59:00+00:00',
                     'expires_at': '2026-10-06T16:29:00+00:00', 'views': []}
        after_midnight = datetime.fromisoformat('2026-10-06T16:01:00+00:00')  # 00:01 in Shanghai.
        from unittest import mock
        with mock.patch.object(query, 'load_cost_dashboard', return_value=published):
            src = query.Sources(app_snapshot=Path('/nonexistent'), cost_publication=Path('/x'),
                                now=after_midnight)
            self.assertIsNone(src.cost())
            self.assertEqual(src.used['cost_publication']['reason'], 'cost_day_rolled_over')
            same_day = query.Sources(app_snapshot=Path('/nonexistent'), cost_publication=Path('/x'),
                                     now=datetime.fromisoformat('2026-10-06T15:59:30+00:00'))
            self.assertIs(same_day.cost(), published)


class WorkItemPageValidationTests(unittest.TestCase):
    """Published WI display may name its own kernel page; nothing else passes."""

    def test_project_page_is_optional_and_bound_to_its_wi(self):
        from pulse_stats.cost_dashboard import _validate_work_items
        wi = 'wi_20261028_253'
        base = {'id': wi, 'title': 't', 'summary': None, 'status': 'active'}
        _validate_work_items([base])
        _validate_work_items([{**base, 'project_page': None}])
        _validate_work_items([{**base, 'project_page': '/v/' + wi + '.html'}])
        for bad in ('v/' + wi + '.html', '/v/../' + wi + '.html', '/v/wi_20990101_001.html', 3):
            with self.assertRaises(ValueError):
                _validate_work_items([{**base, 'project_page': bad}])
        with self.assertRaises(ValueError):
            _validate_work_items([{**base, 'url': '/v/x'}])


class PullRequestOutputQueryTests(unittest.TestCase):
    """The Stats “产出” is kernel-merged PRs; queries return them, unknown never as 0."""

    OUTPUTS = {'status': 'partial', 'count': None, 'known_count': 3,
               'work_items': [{'wi': 'wi_1', 'count': 2}, {'wi': 'wi_2', 'count': 1}],
               'unreadable_work_items': ['wi_9']}

    def test_wi_scope(self):
        self.assertEqual(query.scoped_pull_requests(self.OUTPUTS, {'wi_1'})['count'], 2)
        self.assertEqual(query.scoped_pull_requests(self.OUTPUTS, {'wi_3'})['count'], 0)
        self.assertIsNone(query.scoped_pull_requests(self.OUTPUTS, {'wi_1', 'wi_9'})['count'])
        self.assertIsNone(query.scoped_pull_requests(None, {'wi_1'}))

    def test_agent_filter_counts_only_the_wis_of_its_rows(self):
        # Review repro: agent a (USD10, one PR) vs agent b (USD20, two PRs).
        outputs = {'status': 'complete', 'count': 3, 'known_count': 3, 'unreadable_work_items': [],
                   'work_items': [{'wi': 'wi_a', 'count': 1}, {'wi': 'wi_b', 'count': 2}]}
        view = month_view([_group({'agent': 'a', 'wi': 'wi_a'}, {'machine_cash_usd': '10'}),
                           _group({'agent': 'b', 'wi': 'wi_b'}, {'machine_cash_usd': '20'})])
        view['pull_request_outputs'] = outputs
        result = query.q_cost(stub(view)(), cost_args(agent='a'))
        self.assertEqual(result['summary']['amount_usd'], '10')
        self.assertEqual(result['pull_requests']['count'], 1)
        self.assertEqual([w['wi'] for w in result['pull_requests']['work_items']], ['wi_a'])
        self.assertEqual(result['platform']['pull_requests']['count'], 3)
        self.assertEqual(query.q_cost(stub(view)(), cost_args())['pull_requests']['count'], 3)

    def test_wi_combined_with_unmatched_filter_counts_nothing(self):
        # Review repro: WI filter plus an agent or runtime with no matching rows.
        outputs = {'status': 'complete', 'count': 2, 'known_count': 2, 'unreadable_work_items': [],
                   'work_items': [{'wi': 'wi_253', 'count': 2}]}
        view = month_view([_group({'agent': 'pulse', 'runtime': 'cc', 'wi': 'wi_253'},
                                  {'machine_cash_usd': '7'})])
        view['pull_request_outputs'] = outputs
        for extra in ({'agent': 'aki'}, {'runtime': 'codex'}):
            result = query.q_cost(stub(view)(), cost_args(work_item_id='wi_253', **extra))
            self.assertEqual(result['pull_requests']['count'], 0)
            self.assertEqual(result['pull_requests']['work_items'], [])
        matched = query.q_cost(stub(view)(), cost_args(work_item_id='wi_253', agent='pulse'))
        self.assertEqual(matched['pull_requests']['count'], 2)
        alone = query.q_cost(stub(view)(), cost_args(work_item_id='wi_253'))
        self.assertEqual(alone['pull_requests']['count'], 2)

    def test_overview_count_is_exact_bounded_or_unknown(self):
        self.assertEqual(query._merged_pr_count({'pull_request_outputs': self.OUTPUTS}),
                         {'status': 'partial', 'count': None, 'known_count': 3})
        self.assertEqual(query._merged_pr_count({}), {'status': 'unavailable', 'count': None})


class PullRequestOnlyRowTests(unittest.TestCase):
    """Review repro: a WI with one merged PR and no usage or Outcome this period."""

    def view(self):
        outputs = {'status': 'complete', 'count': 1, 'known_count': 1, 'unreadable_work_items': [],
                   'work_items': [{'wi': 'wi_pr', 'count': 1}]}
        view = month_view([_group({'agent': 'b', 'wi': 'wi_b'}, {'machine_cash_usd': '5'})])
        view['pull_request_outputs'] = outputs
        view['pull_request_only_groups'] = [{'key': {'agent': 'aki', 'wi': 'wi_pr'}, 'cost': {},
                                             'usage': {'event_count': 0, 'known_total_tokens': None}}]
        return view

    def test_agent_filter_keeps_the_pr_only_wi(self):
        result = query.q_cost(stub(self.view())(), cost_args(agent='aki'))
        self.assertEqual(result['pull_requests']['count'], 1)
        self.assertEqual([r['key']['wi'] for r in result['pull_request_only_rows']], ['wi_pr'])
        self.assertIsNone(result['summary']['amount_usd'])
        grouped = query.q_cost(stub(self.view())(), cost_args(agent='aki', group='wi'))
        [g] = grouped['groups']
        self.assertEqual((g['wi'], g['pull_request_only_rows'], g['amount_usd']), ('wi_pr', 1, None))

    def test_wi_query_counts_its_prs(self):
        class Src(stub(self.view())):
            def app(self):
                return None
        args = type('A', (), {'work_item_id': 'wi_pr'})()
        month = query.q_wi(Src(), args)['cost_by_period']['month']
        self.assertEqual(month['pull_requests']['count'], 1)
        self.assertEqual(month['total']['pull_request_only_rows'], 1)
