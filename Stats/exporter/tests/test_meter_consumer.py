"""合同第 5 节：新生成器（pulse_meter.publish）产出的发布物过 load_cost_dashboard 与 pulse_stats.query。"""
from __future__ import annotations

import contextlib
from datetime import timedelta
from decimal import Decimal
import importlib.util
import io
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / 'ComputeCost'))
sys.path.insert(0, str(ROOT / 'Stats/exporter'))


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


# 同名测试模块分散在两个目录，按文件路径载入，避免与 ComputeCost/tests/test_query.py 混淆。
_views = _load('meter_views_fixture', ROOT / 'ComputeCost/tests/test_meter_views.py')
_publish_tests = _load('meter_publish_fixture', ROOT / 'ComputeCost/tests/test_meter_publish.py')
_stats_query_tests = _load('stats_query_fixture', ROOT / 'Stats/exporter/tests/test_query.py')

from pulse_meter import publish, schema_sql  # noqa: E402
from pulse_stats import query, query_cli  # noqa: E402
from pulse_stats.cost_dashboard import load_cost_dashboard, usage_metrics  # noqa: E402

NOW, REV, WI = _publish_tests.NOW, _publish_tests.REV, _publish_tests.WI


class MeterConsumerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        f = _views.Fixture(self.dir)
        db = sqlite3.connect(f.path)
        db.execute("INSERT INTO transfer_cursors VALUES('ownermac','e','usage_events',5,?,0,0)",
                   ((NOW - timedelta(minutes=1)).strftime('%Y-%m-%dT%H:%M:%S.000Z'),))
        db.commit()
        db.close()
        f.workspace('/w/pulse', 'pulse')
        f.claim('t', WI)
        f.usage('e1', cwd='/w/pulse')
        f.usage('e2', cwd='/w/pulse', runtime='codex', sid='c1', turn_id='')
        f.usage('e3', cwd='/w/pulse', model='nope', request_id='r3')
        catalog = self.dir / 'catalog.json'
        catalog.write_text(json.dumps(_publish_tests.catalog_snapshot()))
        facts = self.dir / 'wi-facts.json'
        facts.write_text(json.dumps(_publish_tests.wi_facts()))
        self.document = publish.build(f.path, now=NOW, source_revision=REV, catalog_path=catalog,
                                      wi_facts_path=facts)
        self.publication = self.dir / 'private' / 'cost.json'
        publish.publish(self.document, self.publication)
        self.app = self.dir / 'snapshot-v2.json'
        self.app.write_text(json.dumps(_stats_query_tests.app_snapshot(NOW - timedelta(minutes=5))))
        self.app.chmod(0o600)

    def run_query(self, *argv):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(query_cli.main([*argv, '--snapshot', str(self.app), '--cost-dashboard',
                                             str(self.publication)], now=NOW + timedelta(minutes=1),
                                            home=self.dir), 0)
        return json.loads(out.getvalue())

    def test_reader_accepts_new_publication(self):
        loaded = load_cost_dashboard(self.publication, now=NOW + timedelta(minutes=1))
        self.assertEqual(loaded['status'], 'available')
        metrics = {m['id']: m for m in usage_metrics(loaded, now=NOW + timedelta(minutes=1))}
        self.assertEqual(metrics['usage.today.source_note']['value'], 3)
        self.assertEqual([r['agent'] for r in metrics['usage.by_agent']['value']], ['pulse', 'pulse', 'pulse'])

    def test_query_rows_and_groups_match_summary(self):
        for period in ('today', 'week', 'month'):
            view = next(v for v in self.document['views'] if v['period'] == period)
            grouped = self.run_query('cost', '--period', period, '--group', 'agent')['result']
            self.assertEqual(sum(g['event_count'] for g in grouped['groups']),
                             view['summary']['usage']['event_count'])
            rows = self.run_query('cost', '--period', period)['result']['rows']
            self.assertEqual(len(rows), len(view['groups']))
            self.assertEqual(sum(Decimal(g['amount_usd'] or 0) for g in grouped['groups']),
                             sum(Decimal(r['amount_usd'] or 0) for r in rows))
            self.assertEqual(grouped['summary']['amount_usd'], query.amount(view['summary']['cost'])[0])

    def test_query_by_work_item_and_agent(self):
        result = self.run_query('cost', '--period', 'today', '--work-item-id', WI)['result']
        self.assertTrue(result['rows'])
        self.assertTrue(all(r['key']['wi'] == WI for r in result['rows']))
        self.assertEqual(len(result['outcomes']['items']), 1)
        wi = self.run_query('wi', '--work-item-id', WI)
        self.assertEqual(wi['schema'], 'pulse_stats_query/v1')
        agent = self.run_query('cost', '--period', 'month', '--agent', 'pulse')['result']['rows']
        self.assertEqual({r['key']['agent'] for r in agent}, {'pulse'})

    def test_undeclared_rows_pass_the_reader_with_their_own_source(self):
        (self.dir / 'undeclared').mkdir()
        f = _views.Fixture(self.dir / 'undeclared')
        f.origin('h1', 'human', '2026-11-20T01:00:00.000Z', sid='hs')
        f.usage('u1', sid='hs', turn_id='h1', at='2026-11-20T01:10:00.000Z', cwd='/w/x')
        document = publish.build(f.path, now=NOW, source_revision=REV, catalog_path=None, expected_nodes=[])
        target = self.dir / 'private' / 'undeclared.json'
        publish.publish(document, target)
        loaded = load_cost_dashboard(target, now=NOW + timedelta(minutes=1))
        self.assertEqual(loaded['status'], 'available')
        row = next(g for g in loaded['views'][0]['groups'] if g['key']['wi'] == 'undeclared')
        self.assertEqual(list(row['attribution']['sources']), ['undeclared'])
        self.assertEqual(row['cost']['reasons'], [])  # 未声明 WI 不报归属异常
        # 来源枚举仍是封闭的：没登记的来源键整份不收
        from pulse_stats.cost_dashboard import _validate
        bad = json.loads(json.dumps(document))
        bad['views'][0]['groups'][0]['attribution'] = {'sources': {'made_up': {'event_count': 1,
                                                                            'priced_machine_cash_usd': '0'}}}
        from pulse_compute_cost.receipts import digest
        bad['sha256'] = digest({k: v for k, v in bad.items() if k != 'sha256'})
        with self.assertRaises(ValueError):
            _validate(bad)

    def test_every_query_answers(self):
        for name in query.QUERIES:
            if name == 'agent_day':
                continue
            argv = [name] + (['--work-item-id', WI] if name == 'wi' else [])
            self.assertEqual(self.run_query(*argv)['schema'], 'pulse_stats_query/v1', name)


_refresh = _load('stats_cost_refresh_meter', ROOT / 'Scripts/pulse-stats-cost-refresh.py')


class MeterSwitchTests(unittest.TestCase):
    """refresh.json `meter_mode` 开关：缺省走旧链路；打开后 refresh 由汇总库发布。"""

    def setUp(self):
        from unittest.mock import patch
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        self.state = self.dir / 'stats'
        self.state.mkdir(mode=0o700)
        self.central = _views.Fixture(self.dir)          # 汇总库，夹具灌数
        from datetime import datetime, timezone
        self.now_text = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.000Z')
        self.central.claim('t', WI)
        self.central.usage('e1', at=self.now_text)
        self.node = self.dir / 'node.sqlite'
        db = sqlite3.connect(self.node)
        db.executescript(schema_sql())
        db.executemany('INSERT INTO meta VALUES(?, ?)', [('schema_version', '1'), ('db_epoch', 'n'),
                                                         ('next_seq', '1'), ('node', 'oracle-node-1')])
        db.commit()
        db.close()
        registry = self.dir / 'sources.json'
        registry.write_text(json.dumps({'schema': 'computecost_source_registry/v1', 'sources': []}))
        registry.chmod(0o600)
        self.config = self.state / 'refresh.json'
        self.write_config(meter_mode='pulse_meter')
        from pulse_meter import store
        self.patches = [patch.object(_refresh, 'STATE', self.state), patch.object(_refresh, 'REGISTRY', registry),
                        patch.object(store, 'node_db_path', lambda: self.node),
                        patch.object(store, 'central_db_path', lambda: self.central.path),
                        patch.object(_refresh, 'release_config', lambda path: json.loads(path.read_text())),
                        patch.object(_refresh, 'cost_row_contexts_ready', lambda *a, **k: False)]
        for p in self.patches:
            p.start()
            self.addCleanup(p.stop)

    def write_config(self, **extra):
        self.config.write_text(json.dumps({'source_revision': REV, 'peers': [], **extra}))
        self.config.chmod(0o600)

    def test_switch_is_off_by_default(self):
        self.write_config()
        self.assertFalse(_refresh.meter_enabled(self.config))
        self.write_config(meter_mode='other')
        self.assertFalse(_refresh.meter_enabled(self.config))

    def test_publisher_round_publishes_when_watermark_moves(self):
        first = _refresh.meter_publisher_round(self.config)
        self.assertEqual((first['state'], first['published']), ('completed', 'available'))
        out = self.state / 'cost-dashboard-v1.json'
        loaded = load_cost_dashboard(out)
        self.assertEqual(loaded['status'], 'available')
        self.assertEqual(loaded['views'][0]['summary']['usage']['event_count'], 1)
        # 只启用本机：本轮刚并入、待确认量 0，算收齐
        self.assertEqual(loaded['views'][0]['summary']['usage']['source_coverage'], 'complete')
        self.assertTrue((self.state / 'cost-last-week-v1.json').is_file())
        self.assertTrue((self.state / _refresh.METER_DIAGNOSTICS).is_file())
        status = json.loads((self.state / _refresh.REFRESH_STATUS).read_text())
        self.assertEqual(status['state'], 'completed')
        again = _refresh.meter_publisher_round(self.config)
        self.assertNotIn('published', again)                 # 水位没动、发布物还新：不重发
        self.central.usage('e2', request_id='r2', at=self.now_text)
        third = _refresh.meter_publisher_round(self.config)
        self.assertEqual(third['published'], 'available')
        self.assertEqual(load_cost_dashboard(out)['views'][0]['summary']['usage']['event_count'], 2)

    def test_configured_peer_that_never_reported_blocks_complete_coverage(self):
        self.write_config(meter_mode='pulse_meter', peers=[{'id': 'ownermac', 'host': 'x'},
                                                           {'id': 'tcloud', 'host': 'y'}],
                          paused_peers={'tcloud': 'paused_by_decision'})
        _refresh.meter_publisher_round(self.config, force=True)
        loaded = load_cost_dashboard(self.state / 'cost-dashboard-v1.json')
        self.assertEqual(loaded['views'][0]['summary']['usage']['source_coverage'], 'unknown')
        self.assertEqual([p['id'] for p in loaded['collection_backlog']['peers']], ['ownermac'])  # 暂停的 tcloud 不算

    def test_refresh_in_meter_mode_skips_the_ledger_cycle_between_duties(self):
        from unittest.mock import patch
        from datetime import datetime, timezone
        (self.state / 'last-refresh.json').write_text(json.dumps({'generated_at': datetime.now(timezone.utc).isoformat()}))
        with patch.object(_refresh, '_refresh', side_effect=AssertionError('ledger cycle ran')):
            _refresh.refresh(self.config)
        self.assertTrue((self.state / 'cost-dashboard-v1.json').is_file())

    def test_node_round_tails_and_ships(self):
        from unittest.mock import patch
        from pulse_meter import ship
        self.write_config(meter_mode='pulse_meter', peer_id='ownermac', publisher='oracle-node-1',
                          peers=[{'id': 'oracle-node-1', 'host': 'pulse@oracle-node-1'}])
        calls = []
        with patch.object(ship, 'send', lambda db, peer, peer_id, **k: calls.append((peer['id'], peer_id))
                          or {'sent': 0, 'applied': 0}):
            record = _refresh.meter_local_round(self.config)
        self.assertEqual(record['state'], 'completed')
        self.assertEqual(calls, [('oracle-node-1', 'ownermac')])
        self.assertEqual(json.loads((self.state / _refresh.METER_STATUS).read_text())['role'], 'node')


if __name__ == '__main__':
    unittest.main()


def test_stats_release_ships_pulse_meter_entry():
    # ship.ssh_transport calls <release>/Scripts/pulse-meter.py receive on the publisher.
    import runpy
    root = Path(__file__).resolve().parents[3]
    components = runpy.run_path(str(root / 'Stats' / 'deploy.py'), run_name='stats_deploy_probe')['COMPONENTS']
    assert 'Scripts/pulse-meter.py' in components
    assert (root / 'Scripts' / 'pulse-meter.py').is_file()
