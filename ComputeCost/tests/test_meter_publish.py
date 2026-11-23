"""合同第 5 节：汇总库 → pulse_stats_cost_dashboard/v1；价表装入；WI 产出；用例 10 检查。"""
from __future__ import annotations

from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
import io
from decimal import Decimal
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'ComputeCost'))
sys.path.insert(0, str(ROOT / 'Stats/exporter'))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from pulse_meter import outcomes, prices, publish  # noqa: E402
from pulse_stats.cost_dashboard import _validate, load_cost_dashboard  # noqa: E402
from test_meter_views import AT, OPUS, OPUS_KEY, Fixture  # noqa: E402

NOW = datetime(2026, 11, 20, 6, tzinfo=timezone.utc)   # 北京 11-20 14:00
REV = 'a' * 40
WI = 'wi_20261121_001'


def catalog_snapshot(retrieved_at='2026-11-18T02:00:00+00:00', opus_prompt='0.000004'):
    return {'retrieved_at': retrieved_at, 'catalog': {'data': [
        {'id': OPUS_KEY, 'pricing': {'prompt': opus_prompt, 'completion': '0.00002',
                                     'input_cache_read': '0.0000002', 'input_cache_write': '0.000005'}},
        {'id': 'openai/gpt-6', 'pricing': {'prompt': '0.00000125', 'completion': '0.00001',
                                           'input_cache_read': '0.000000125'}},
        {'id': 'openrouter/auto', 'pricing': {'prompt': '-1', 'completion': '-1'}},
        {'id': 'dup', 'pricing': {'prompt': '0.1', 'completion': '0.1'}},
        {'id': 'dup', 'pricing': {'prompt': '0.2', 'completion': '0.2'}},
    ]}}


def wi_facts(generated_at=NOW.isoformat(), coverage='complete'):
    return {'schema': 'harness.stats_facts/v1', 'read_only': True, 'namespace': 'pulse-production',
            'generated_at': generated_at, 'coverage': coverage, 'source': 'journal', 'unavailable_items': [],
            'items': [
                {'id': WI, 'title': '计量', 'summary': None, 'status': 'completed', 'owner': 'pulse', 'parent': None,
                 'business_progress': 'completed', 'project_page': None,
                 'completions': [{'revision': 7, 'profile': 'lightweight', 'actor': 'pulse',
                                  'at': '2026-11-20T03:00:00+00:00', 'title': '完成计量',
                                  'verification': 'human', 'candidate': '1' * 64, 'artifacts': '2' * 64,
                                  'acceptance': '3' * 64}],
                 'merged_pull_requests': [{'repository': 'org/PULSE', 'number': 9, 'merged_at': '2026-11-20T02:00:00Z',
                                           'merge_commit': 'f' * 40, 'head_sha': 'e' * 40, 'decision': 'review',
                                           'observed_at': '2026-11-20T02:00:10+00:00', 'reviewed_rounds': 1,
                                           'score': 90}]},
                {'id': 'wi_20261121_002', 'title': '只有产出', 'summary': None, 'status': 'completed',
                 'owner': 'emi', 'parent': None, 'business_progress': 'completed', 'project_page': None,
                 'completions': [{'revision': 3, 'profile': 'standard', 'actor': 'emi',
                                  'at': '2026-11-19T03:00:00+00:00', 'title': '验收', 'verification': 'agent_review',
                                  'candidate': '4' * 64, 'artifacts': '5' * 64, 'acceptance': '6' * 64}],
                 'merged_pull_requests': []},
            ]}


class PublishTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.f = Fixture(self.root)
        self.catalog = self.root / 'catalog.json'
        self.catalog.write_text(json.dumps(catalog_snapshot()))
        self.facts = self.root / 'wi-facts.json'
        self.facts.write_text(json.dumps(wi_facts()))
        db = sqlite3.connect(self.f.path)
        db.execute("INSERT INTO transfer_cursors VALUES('ownermac','e','usage_events',5,?,0,0)",
                   ((NOW - timedelta(minutes=1)).strftime('%Y-%m-%dT%H:%M:%S.000Z'),))
        db.execute("INSERT INTO meta VALUES('pending_unacked:ownermac', '0')")
        db.commit()
        db.close()

    def build(self, **extra):
        extra.setdefault('expected_nodes', ['ownermac'])
        return publish.build(self.f.path, now=NOW, source_revision=REV, catalog_path=self.catalog,
                             wi_facts_path=self.facts, **extra)

    def coverage(self, **extra):
        doc = self.build(**extra)
        _validate(doc)
        return (self.view(doc)['summary']['usage']['source_coverage'],
                [p['id'] for p in doc.get('collection_backlog', {}).get('peers', [])])

    def meta(self, key, value):
        db = sqlite3.connect(self.f.path)
        db.execute('INSERT OR REPLACE INTO meta VALUES(?, ?)', (key, value))
        db.commit()
        db.close()

    def test_coverage_requires_every_enabled_node(self):
        self.assertEqual(self.coverage(), ('complete', []))
        # 配置了却从未上送的远端：不算收齐，并列出来
        self.assertEqual(self.coverage(expected_nodes=['ownermac', 'oracle-node-1']), ('unknown', ['oracle-node-1']))
        # 不知道该有哪些节点：无从判定
        self.assertEqual(self.coverage(expected_nodes=None)[0], 'unknown')
        # 待确认传输量未报、或不为 0
        self.meta('pending_unacked:ownermac', '3')
        self.assertEqual(self.coverage(), ('unknown', ['ownermac']))
        db = sqlite3.connect(self.f.path)
        db.execute("DELETE FROM meta WHERE key='pending_unacked:ownermac'")
        db.commit()
        db.close()
        self.assertEqual(self.coverage(), ('unknown', ['ownermac']))

    def test_coverage_stale_node(self):
        db = sqlite3.connect(self.f.path)
        db.execute("UPDATE transfer_cursors SET last_received_at='2026-11-20T05:00:00.000Z'")  # 60 分钟前
        db.commit()
        db.close()
        self.assertEqual(self.coverage(), ('unknown', ['ownermac']))

    def published(self, document):
        out = self.root / 'private' / 'cost-dashboard-v1.json'
        publish.publish(document, out)
        loaded = load_cost_dashboard(out, now=NOW + timedelta(minutes=1))
        self.assertEqual(loaded['status'], document['status'], loaded.get('reason'))
        return loaded

    def view(self, document, period='today'):
        return next(v for v in document['views'] if v['period'] == period)

    def test_document_validates_and_loads(self):
        f = self.f
        f.claim('t', WI)
        f.usage('e1')
        f.usage('e2', turn_id='t2')
        f.usage('e3', model='nope')
        doc = self.build()
        _validate(doc)
        self.assertEqual(doc['status'], 'available')
        self.assertEqual([(v['period'], v['dimension']) for v in doc['views']],
                         [('today', 'activity'), ('week', 'activity'), ('month', 'activity')])
        self.published(doc)
        today = self.view(doc)
        self.assertEqual(today['summary']['usage']['event_count'], 3)
        self.assertEqual(today['summary']['usage']['source_coverage'], 'complete')
        self.assertNotIn('collection_backlog', doc)
        self.assertEqual(today['watermark'], f.seq)
        self.assertEqual(today['catalog_sha256'], prices.digest(catalog_snapshot()['catalog']))

    def test_amounts_are_exact_decimals(self):
        self.f.claim('t', WI)
        self.f.usage('e1', tokens=(3, 7, 11, 13))   # 3*4e-6 + 7*2e-7 + 11*5e-6 + 13*2e-5
        self.f.usage('e2', tokens=(1, 0, 0, 0), turn_id='t1', request_id='r2')
        doc = self.build()
        group = next(g for g in self.view(doc)['groups'] if g['key']['wi'] == WI)
        expected = Decimal('0.000012') + Decimal('0.0000014') + Decimal('0.000055') + Decimal('0.00026') \
            + Decimal('0.000004')
        self.assertEqual(Decimal(group['cost']['machine_cash_usd']), expected)
        self.assertEqual(group['cost']['machine_cash_usd'], '0.0003324')
        self.assertEqual(group['cost']['reasons'], [])
        self.assertEqual(publish.money(1), '0.000000000001')
        self.assertEqual(publish.money(5 * 10 ** 12), '5')

    def test_unpriced_over_eight_is_summarised(self):
        self.f.claim('t', WI)
        for i in range(11):
            self.f.usage(f'u{i}', model='nope' if i % 2 else None)
        self.f.usage('ok')
        doc = self.build()
        _validate(doc)
        summary = self.view(doc)['summary']['cost']
        self.assertEqual(summary['unpriced_count'], 11)
        self.assertEqual(summary['unpriced_reason_counts'], {'model_unknown': 6, 'price_unavailable': 5})
        self.assertEqual(len(summary['unpriced']), 4)
        self.assertEqual(summary['priced_event_count'], 1)
        self.assertIsNone(summary['machine_cash_usd'])
        self.assertEqual(summary['reasons'], ['incomplete_price_or_quantity'])
        self.published(doc)

    def test_empty_database_is_a_valid_document(self):
        (self.root / 'empty').mkdir()
        empty = Fixture(self.root / 'empty')
        doc = publish.build(empty.path, now=NOW, source_revision=REV, catalog_path=None)
        _validate(doc)
        self.assertEqual(doc['status'], 'available')
        self.assertEqual(self.view(doc)['groups'], [])
        self.assertEqual(self.view(doc)['outcomes']['status'], 'unavailable')
        self.assertEqual(self.view(doc)['summary']['usage']['source_coverage'], 'unknown')
        self.published(doc)

    def test_missing_database_is_unavailable(self):
        doc = publish.build(self.root / 'none.sqlite', now=NOW, source_revision=REV, catalog_path=None)
        _validate(doc)
        self.assertEqual((doc['status'], doc['views']), ('unavailable', []))

    def test_backlog_and_stale_peer(self):
        db = sqlite3.connect(self.f.path)
        db.execute("INSERT INTO transfer_cursors VALUES('oracle-node-1','e','usage_events',5,?,10,3)",
                   ((NOW - timedelta(minutes=1)).strftime('%Y-%m-%dT%H:%M:%S.000Z'),))
        db.execute("INSERT INTO transfer_cursors VALUES('tcloud','e','usage_events',5,'2026-11-19T00:00:00.000Z',0,0)")
        db.commit()
        db.close()
        doc = self.build(expected_nodes=['ownermac', 'oracle-node-1', 'tcloud'])
        _validate(doc)
        self.assertEqual(doc['collection_backlog']['peers'],
                         [{'id': 'oracle-node-1', 'deferred_bundles': 3}, {'id': 'tcloud', 'deferred_bundles': 1}])
        self.assertEqual(self.view(doc)['summary']['usage']['source_coverage'], 'unknown')

    def test_attribution_sources_and_cron_job(self):
        f = self.f
        f.claim('t', WI)
        f.usage('e1')
        f.launch('j', 'scheduled_session', sid='s9', job_id='nightly', job_name='夜跑', entrypoint='automation')
        f.usage('e2', sid='s9', turn_id='')
        f.usage('e3', sid='s8', turn_id='')
        doc = self.build()
        groups = {g['key']['wi']: g for g in self.view(doc)['groups']}
        self.assertEqual(groups['cron:nightly']['attribution'],
                         {'sources': {'scheduled_job': {'event_count': 1,
                                                        'priced_machine_cash_usd': groups['cron:nightly']['cost'][
                                                            'machine_cash_usd']}},
                          'job': {'id': 'nightly', 'name': '夜跑'}})
        self.assertEqual(groups['unassigned']['cost']['reasons'], ['attribution_unknown'])
        self.assertIsNotNone(groups['unassigned']['cost']['partial_machine_cash_usd'])
        usage = self.view(doc)['summary']['usage']
        self.assertEqual((usage['cron_event_count'], usage['wi_unassigned_event_count']), (1, 1))

    def test_outcomes_beside_cost(self):
        self.f.claim('t', WI)
        self.f.usage('e1')
        self.f.usage('e2', request_id='r', node='oracle-node-1')
        doc = self.build()
        _validate(doc)
        today = self.view(doc)
        self.assertEqual(today['outcomes']['status'], 'complete')
        self.assertEqual(today['outcomes']['count'], 1)       # 今日只有 WI 的完成；另一 WI 的验收在昨天
        rows = [g for g in today['groups'] if g['key']['wi'] == WI]
        self.assertEqual(sum(g['outcomes']['known_count'] for g in rows), 1)  # 只挂一行，不重复
        week = self.view(doc, 'week')
        self.assertEqual(week['outcomes']['count'], 2)
        only = week['outcome_only_groups']
        self.assertEqual([(g['key']['wi'], g['key']['agent'], g['outcomes']['known_count']) for g in only],
                         [('wi_20261121_002', 'emi', 1)])
        self.assertIn('emi', week['active_agents'])
        prs = today['pull_request_outputs']
        self.assertEqual(prs['status'], 'complete')
        self.published(doc)

    def test_prices_load_appends_on_change_only(self):
        conn = publish.connect(self.f.path)
        try:
            conn.execute('DELETE FROM prices')
            first = prices.load(conn, catalog_snapshot())
            self.assertEqual((first['models'], first['inserted']), (2, 2))
            self.assertEqual(prices.load(conn, catalog_snapshot('2026-11-19T02:00:00+00:00'))['inserted'], 0)
            changed = prices.load(conn, catalog_snapshot('2026-11-25T20:00:00+00:00', opus_prompt='0.000005'))
            self.assertEqual(changed['inserted'], 1)
            rows = conn.execute('SELECT effective_from, prompt, cache_write FROM prices WHERE model_key=? '
                                'ORDER BY effective_from', (OPUS_KEY,)).fetchall()
            self.assertEqual(rows, [('2026-11-01', 4_000_000, 5_000_000), ('2026-11-26', 5_000_000, 5_000_000)])
            gpt = conn.execute("SELECT cache_read, cache_write FROM prices WHERE model_key='openai/gpt-6'").fetchone()
            self.assertEqual(gpt, (125_000, prices.MISSING))
        finally:
            conn.close()

    def test_last_week_publication(self):
        self.f.claim('t', WI)
        self.f.usage('e1', at='2026-11-12T04:00:00.000Z')
        doc = self.build(last_week=True)
        _validate(doc)
        self.assertEqual([(v['period'], v['dimension']) for v in doc['views']], [('last_week', 'activity')])
        self.assertEqual(doc['views'][0]['summary']['usage']['event_count'], 1)
        self.assertNotIn('pull_request_anomalies', doc)

    def test_diagnostics_and_case10(self):
        f = self.f
        f.claim('t', WI)
        for i in range(97):
            f.usage(f'k{i}')
        f.usage('u1', sid='lost', turn_id='')
        f.usage('u2', sid='lost', turn_id='', request_id='x')
        f.usage('p1', model='nope')
        rows = f.events()
        diag = publish.diagnostics(rows)
        self.assertEqual(diag['unlinked_top'][0]['session_id'], 'lost')
        self.assertEqual(diag['unlinked_top'][0]['event_count'], 2)
        self.assertEqual(diag['unlinked_top'][0]['reason'], 'no_turn')
        self.assertEqual(diag['unpriced_top'][0]['reason'], 'price_unavailable')
        result = publish.anomaly_ratios(rows)
        self.assertEqual(result['event_ratio'], '0.030000')
        self.assertFalse(result['event_pass'])
        self.assertEqual(result['amount_ratio'], str((Decimal(2) / Decimal(99)).quantize(Decimal('0.000001'))))
        self.assertTrue(result['amount_ratio_is_lower_bound'])
        self.assertFalse(result['amount_pass'])
        # 缺口按 (runtime, model) 净额：旧值里 claude-opus-5-5 归 hestia 的 1 美元，新链路归到别的 Agent 不算缺口；
        # 旧值里有、新链路整个模型都没有的 2 美元才算，给出解释后不再算无解释缺口。
        baseline = {'generated_at': NOW.isoformat(), 'views': [{'period': 'month', 'dimension': 'activity', 'groups': [
            {'key': {'agent': 'hestia', 'runtime': 'cc', 'model': OPUS}, 'cost': {'priced_machine_cash_usd': '0.001'},
             'usage': {'event_count': 1}},
            {'key': {'agent': 'hestia', 'runtime': 'codex', 'model': 'gpt-x'}, 'cost': {'priced_machine_cash_usd': '2'},
             'usage': {'event_count': 4}}]}]}
        gap = publish.anomaly_ratios(rows, baseline=baseline)
        self.assertEqual((gap['gap_unexplained_usd'], gap['gap_unexplained_events']), ('2', 4))
        self.assertEqual([g['model'] for g in gap['gaps']], ['gpt-x'])
        self.assertIn('hestia', [a['agent'] for a in gap['agents']])
        explained = publish.anomaly_ratios(rows, baseline=baseline,
                                           explanations={'codex/gpt-x': {'usd': '2', 'events': 4, 'reason': '缺原始记录'}})
        self.assertEqual((explained['gap_unexplained_usd'], explained['gap_unexplained_events']), ('0', 0))

    def test_undeclared_is_reported_apart_and_not_an_anomaly(self):
        f = self.f
        f.origin('h1', 'human', '2026-11-20T01:00:00.000Z', sid='hs')
        f.usage('u1', sid='hs', turn_id='h1', at='2026-11-20T01:10:00.000Z')          # 人类请求未声明
        f.usage('x1', sid='lost', turn_id='')                                          # 真正未关联
        f.claim('t', WI)
        f.usage('k1')
        doc = self.build()
        _validate(doc)
        groups = {g['key']['wi']: g for g in self.view(doc)['groups']}
        undeclared = groups['undeclared']
        self.assertEqual(undeclared['attribution']['sources']['undeclared']['event_count'], 1)
        self.assertEqual(undeclared['cost']['reasons'], [])               # 不算异常：金额完整
        self.assertIsNotNone(undeclared['cost']['machine_cash_usd'])
        self.assertEqual(groups['unassigned']['cost']['reasons'], ['attribution_unknown'])
        usage = self.view(doc)['summary']['usage']
        self.assertEqual((usage['wi_undeclared_event_count'], usage['wi_unassigned_event_count'], usage['event_count']),
                         (1, 1, 3))
        self.published(doc)
        rows = f.events()
        result = publish.anomaly_ratios(rows)
        self.assertEqual((result['unlinked_event_count'], result['undeclared_event_count']), (1, 1))
        self.assertEqual(result['event_ratio'], str((Decimal(1) / Decimal(3)).quantize(Decimal('0.000001'))))
        diag = publish.diagnostics(rows)
        self.assertEqual(list(diag['undeclared_by_agent']), ['unassigned'])
        self.assertEqual([x['session_id'] for x in diag['unlinked_top']], ['lost'])

    def test_case10_without_baseline_is_unverified(self):
        self.f.claim('t', WI)
        self.f.usage('k1')
        rows = self.f.events()
        result = publish.anomaly_ratios(rows)
        self.assertEqual((result['status'], result['baseline']), ('unverified', 'missing'))
        for argv in ([], ['--baseline', str(self.root / 'absent.json')]):
            out = io.StringIO()
            with redirect_stdout(out):
                code = publish.main(['check', '--central-db', str(self.f.path), '--month', '2026-11', *argv])
            self.assertEqual(code, 2, argv)
            self.assertEqual(json.loads(out.getvalue())['status'], 'unverified')
        broken = self.root / 'broken.json'
        broken.write_text(json.dumps({'views': []}))
        out = io.StringIO()
        with redirect_stdout(out):
            code = publish.main(['check', '--central-db', str(self.f.path), '--month', '2026-11', '--baseline', str(broken)])
        self.assertEqual((code, json.loads(out.getvalue())['baseline']), (2, 'unreadable'))
        baseline = self.root / 'baseline.json'

        def check(generated_at):
            baseline.write_text(json.dumps({'generated_at': generated_at, 'views': [
                {'period': 'month', 'dimension': 'activity', 'groups': []}]}))
            out = io.StringIO()
            with redirect_stdout(out):
                code = publish.main(['check', '--central-db', str(self.f.path), '--month', '2026-11',
                                     '--baseline', str(baseline)])
            return code, json.loads(out.getvalue())

        code, result = check(AT)
        self.assertEqual((code, result['status'], result['baseline_after_data']), (0, 'passed', False))
        # 基线比新数据晚 2 小时：两边不是同一时段，不作数
        code, result = check(NOW.isoformat())
        self.assertEqual((code, result['status'], result['baseline_after_data']), (2, 'unverified', True))

    def test_case10_compares_only_the_selected_nodes(self):
        self.f.claim('t', WI)
        self.f.usage('k1')                                  # ownermac，约 0.00165 美元
        self.f.usage('o1', node='oracle-node-1', sid='o')    # 新链路里 Oracle 的事件，选了 ownermac 就不算
        opus = self.f.one('k1')['usd_e12']
        baseline = {'generated_at': AT, 'views': [{'period': 'month', 'dimension': 'activity', 'groups': [
            {'key': {'agent': 'pulse', 'node': 'ownermac', 'runtime': 'cc', 'model': OPUS},
             'cost': {'priced_machine_cash_usd': publish.money(opus)}, 'usage': {'event_count': 1}},
            {'key': {'agent': 'bb', 'node': 'tcloud', 'runtime': 'pi', 'model': 'gpt-x'},
             'cost': {'priced_machine_cash_usd': '5'}, 'usage': {'event_count': 9}}]}]}
        whole = publish.anomaly_ratios(self.f.events(), baseline=baseline)
        self.assertEqual((whole['status'], whole['gap_unexplained_usd']), ('failed', '5'))   # TCloud 的旧值算成了缺口
        mine = publish.anomaly_ratios(self.f.events(), baseline=baseline, nodes=['ownermac'])
        self.assertEqual((mine['status'], mine['gap_unexplained_usd'], mine['event_count'], mine['nodes']),
                         ('passed', '0', 1, ['ownermac']))
        self.assertEqual(mine['baseline_by_node'], {'tcloud': {'usd': '5', 'events': 9},
                                                    'ownermac': {'usd': publish.money(opus), 'events': 1}})
        path = self.root / 'baseline-nodes.json'
        path.write_text(json.dumps(baseline))
        out = io.StringIO()
        with redirect_stdout(out):
            code = publish.main(['check', '--central-db', str(self.f.path), '--month', '2026-11',
                                 '--baseline', str(path), '--node', 'ownermac'])
        self.assertEqual((code, json.loads(out.getvalue())['status']), (0, 'passed'))

    def test_case10_unpriced_with_known_tokens_stays_unverified(self):
        self.f.claim('t', WI)
        for i in range(200):
            self.f.usage(f'k{i}', request_id=str(i))
        self.f.usage('g', model='gemini-x', tokens=(10, 0, 0, 1))     # 无价，但数量都已知
        rows = self.f.events()
        base = {'generated_at': AT, 'views': [{'period': 'month', 'dimension': 'activity', 'groups': []}]}
        rates = (4_000_000, 200_000, 5_000_000, 20_000_000)
        ok = publish.anomaly_ratios(rows, baseline=base, max_rates=rates)
        # 估算只作诊断：已观测最高单价不是该模型的硬上界，不能据此判通过
        self.assertEqual((ok['status'], ok['unpriced_estimate_usd'], ok['unpriced_estimate_basis'], ok['amount_pass'],
                          ok['amount_ratio_is_lower_bound']), ('unverified', '0.00006', 'max_observed_rate', None, True))
        no_rates = publish.anomaly_ratios(rows, baseline=base)
        self.assertEqual((no_rates['status'], no_rates['unpriced_estimate_usd']), ('unverified', None))
        # 估算再大也不改判定：下界没越线，仍是 unverified
        big = publish.anomaly_ratios(rows, baseline=base, max_rates=(10 ** 12, 0, 0, 0))
        self.assertEqual((big['status'], big['amount_pass']), ('unverified', None))
        # 可比模型进不进价表，估算会变，判定不变
        cheap = publish.anomaly_ratios(rows, baseline=base, max_rates=(1, 1, 1, 1))
        self.assertEqual(cheap['status'], ok['status'])
        # 有数量未知的未计价事件：没有上界
        self.f.usage('q', model='gemini-x', tokens=(10, 0, None, 1), request_id='q')
        unknown = publish.anomaly_ratios(self.f.events(), baseline=base, max_rates=rates)
        self.assertEqual((unknown['status'], unknown['unpriced_estimate_usd']), ('unverified', None))
        # 命令行从价表取最高单价
        conn = publish.connect(self.f.path)
        try:
            self.assertEqual(prices.max_rates(conn), (4_000_000, 200_000, 5_000_000, 20_000_000))
        finally:
            conn.close()

    def test_case10_no_upper_bound_when_a_class_has_no_known_rate(self):
        self.f.claim('t', WI)
        for i in range(200):
            self.f.usage(f'k{i}', request_id=str(i))
        db = sqlite3.connect(self.f.path)
        db.execute("UPDATE prices SET cache_write = -1")   # 价表里缓存写整列没有已知单价
        db.commit()
        db.close()
        conn = publish.connect(self.f.path)
        try:
            rates = prices.max_rates(conn)
        finally:
            conn.close()
        self.assertEqual(rates, (4_000_000, 200_000, None, 20_000_000))
        base = {'generated_at': AT, 'views': [{'period': 'month', 'dimension': 'activity', 'groups': []}]}
        # 缺缓存写单价的未计价事件带 1000 万缓存写 Token：没有上界，不能判通过
        self.f.usage('w', model='gemini-x', tokens=(10, 0, 10_000_000, 1), request_id='w')
        unknown = publish.anomaly_ratios(self.f.events(), baseline=base, max_rates=rates)
        self.assertEqual((unknown['status'], unknown['unpriced_estimate_usd'], unknown['unpriced_estimate_basis']),
                         ('unverified', None, None))

    def test_case10_estimate_uses_max_observed_rate_when_the_class_is_unused(self):
        self.f.claim('t', WI)
        for i in range(200):
            self.f.usage(f'k{i}', request_id=str(i))
        self.f.usage('w', model='gemini-x', tokens=(10, 0, 0, 1), request_id='w')   # 缓存写为 0：缺价不影响
        base = {'generated_at': AT, 'views': [{'period': 'month', 'dimension': 'activity', 'groups': []}]}
        result = publish.anomaly_ratios(self.f.events(), baseline=base, max_rates=(4_000_000, 200_000, None, 20_000_000))
        self.assertEqual((result['status'], result['unpriced_estimate_usd'], result['unpriced_estimate_basis']),
                         ('unverified', '0.00006', 'max_observed_rate'))

    def _check(self, baseline_path):
        out = io.StringIO()
        with redirect_stdout(out):
            code = publish.main(['check', '--central-db', str(self.f.path), '--month', '2026-11',
                                 '--baseline', str(baseline_path)])
        return code, json.loads(out.getvalue())

    def test_check_reads_events_and_max_rates_from_one_snapshot(self):
        """真实 check 入口：读事件与读价表最高单价之间，另一连接提交价表；结果须与开始快照一致。"""
        from unittest.mock import patch
        self.f.claim('t', WI)
        for i in range(200):
            self.f.usage(f'k{i}', request_id=str(i))
        # 无价模型、数量已知：结果不作数；诊断估算按开始快照的最高单价
        self.f.usage('g', model='gemini-x', tokens=(2000, 0, 0, 1000), request_id='g')
        baseline = self.root / 'baseline-snap.json'
        baseline.write_text(json.dumps({'generated_at': AT, 'views': [
            {'period': 'month', 'dimension': 'activity', 'groups': []}]}))
        code, start = self._check(baseline)
        self.assertEqual((code, start['status']), (2, 'unverified'))
        original = publish._view_body
        fired = []

        def body(name):
            if name == 'v_event_base' and not fired:
                fired.append(1)
                db = sqlite3.connect(self.f.path)
                db.execute('UPDATE prices SET prompt = 1, cache_read = 1, cache_write = 1, completion = 1')
                db.commit()
                db.close()
            return original(name)

        with patch.object(publish, '_view_body', body):
            code, during = self._check(baseline)
        self.assertEqual(fired, [1])
        self.assertEqual((code, during), (2, start))      # 估算与判定都出自开始快照
        code, after = self._check(baseline)                 # 新价表在下一次读取才生效
        self.assertNotEqual(after['unpriced_estimate_usd'], start['unpriced_estimate_usd'])

    def test_build_reads_one_snapshot(self):
        """发布：事件读完、产出读之前另一连接提交新产出与新用量；发布物仍只含开始快照里的。"""
        from unittest.mock import patch
        self.f.claim('t', WI)
        self.f.usage('e1')
        start = self.build()
        original = publish._view_body
        fired = []

        def body(name):
            if name == 'v_event_base' and not fired:
                fired.append(1)
                self.f.usage('late', request_id='late')
                db = sqlite3.connect(self.f.path)
                db.execute("INSERT INTO wi_outcomes VALUES(?, 'wi_completed', 'rev:99', ?, 'pulse', ?)",
                           (WI, '2026-11-20T05:00:00.000Z', json.dumps({'namespace': 'ns', 'title': 'x',
                            'verification': 'human', 'candidate': '7' * 64, 'artifacts': '8' * 64,
                            'acceptance': '9' * 64})))
                db.commit()
                db.close()
            return original(name)

        with patch.object(publish, '_view_body', body):
            during = self.build()
        self.assertEqual(fired, [1])
        today, before = self.view(during, 'today'), self.view(start, 'today')
        self.assertEqual((today['watermark'], today['summary']['usage']['event_count'], today['outcomes']['count']),
                         (before['watermark'], before['summary']['usage']['event_count'], before['outcomes']['count']))
        after = self.view(self.build(), 'today')
        self.assertEqual(after['outcomes']['count'], before['outcomes']['count'] + 1)

    def test_case10_zero_token_unpriced_is_not_a_lower_bound(self):
        self.f.claim('t', WI)
        self.f.usage('k1')
        self.f.usage('z', model=None, tokens=(0, 0, 0, 0))
        result = publish.anomaly_ratios(self.f.events(), baseline={'generated_at': AT, 'views': [
            {'period': 'month', 'dimension': 'activity', 'groups': []}]})
        self.assertEqual((result['amount_ratio_is_lower_bound'], result['amount_pass']), (False, True))
        self.assertEqual(result['status'], 'failed')   # 事件比例 1/2 仍超线

    def test_build_hands_its_rows_to_the_same_round(self):
        self.f.claim('t', WI)
        self.f.usage('e1')
        self.f.usage('old', at='2026-10-31T04:00:00.000Z', request_id='o')   # 上月：不进本月诊断
        rows: list = []
        self.build(events_out=rows)
        self.assertEqual(sorted(r['event_id'] for r in publish.month_rows(rows, NOW)), ['e1'])
        conn = publish.connect(self.f.path)
        try:
            direct = publish.events(conn, *publish.period_window(NOW.astimezone(publish.ZONE).date(), 'month'))
            self.assertEqual(publish.diagnostics(publish.month_rows(rows, NOW)), publish.diagnostics(direct))
            self.assertEqual(publish.read_watermark(self.f.path), publish.watermark(conn))
        finally:
            conn.close()

    def test_tenant_dimension_from_registered_agent_tenants(self):
        from pulse_compute_cost.receipts import reference as ref
        self.f.claim('t', WI)
        self.f.workspace('/w/pulse', 'pulse')
        self.f.workspace('/w/cc', 'cc')
        self.f.usage('p1', cwd='/w/pulse')
        self.f.usage('c1', cwd='/w/cc', sid='cc1')
        self.f.usage('x1', cwd='/q', sid='nobody', turn_id='')
        tenants = {'pulse': {'id': 'owner', 'name': 'owner', 'binding_ref': ref('agent_tenant_binding', ['pulse', 'owner'])},
                   'cc': {'id': 'xb', 'name': 'xb', 'binding_ref': ref('agent_tenant_binding', ['cc', 'xb'])}}
        doc = self.build(agent_tenants=tenants)
        _validate(doc)
        view = self.view(doc)
        self.assertEqual(view['query']['group_by'], ['tenant', *publish.DIMENSIONS])
        self.assertEqual(view['population_scope'], 'all_registered_tenants')
        rows = {g['key']['agent']: g for g in view['groups']}
        self.assertEqual((rows['pulse']['key']['tenant'], rows['pulse']['tenant']['sources']), ('owner', ['registry']))
        self.assertEqual(rows['cc']['key']['tenant'], 'xb')
        self.assertEqual((rows['unassigned']['key']['tenant'], 'tenant' in rows['unassigned']), ('unassigned', False))
        self.published(doc)
        # 没有团队登记：不出租户维度（App 不显示该列）
        plain = self.view(self.build())
        self.assertEqual(plain['query']['group_by'], list(publish.DIMENSIONS))
        self.assertNotIn('population_scope', plain)
        self.assertIsNone(publish.load_tenants(None))
        self.assertIsNone(publish.load_tenants({'workspace_registry_path': '/no/such/ssot/registry.yaml'}))
        # 登记是合法 JSON 但不是对象：降级为六维并告警，不抛错
        err = io.StringIO()
        from contextlib import redirect_stderr
        with redirect_stderr(err):
            self.assertIsNone(publish.load_tenants(['not', 'an', 'object']))
            self.assertIsNone(publish.load_tenants({'workspace_registry_path': ['x']}))
        self.assertIn('tenant_registry_not_an_object', err.getvalue())
        self.assertIn('tenant_registry_unreadable', err.getvalue())

    def test_cli_publish(self):
        self.f.claim('t', WI)
        self.f.usage('e1')
        out = self.root / 'state' / 'cost-dashboard-v1.json'
        diag = self.root / 'state' / 'diag.json'
        code = publish.main(['publish', '--central-db', str(self.f.path), '--output', str(out),
                             '--last-week-output', str(self.root / 'state' / 'cost-last-week-v1.json'),
                             '--diagnostics-output', str(diag), '--source-revision', REV,
                             '--catalog-snapshot', str(self.catalog), '--wi-facts', str(self.facts)])
        self.assertEqual(code, 0)
        self.assertEqual(os.stat(out).st_mode & 0o077, 0)
        self.assertEqual(load_cost_dashboard(out)['status'], 'available')
        self.assertEqual(json.loads(diag.read_text())['schema'], 'pulse_meter_attribution_diagnostics/v1')


if __name__ == '__main__':
    unittest.main()
