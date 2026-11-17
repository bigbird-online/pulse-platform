"""Approved seven-column view: actual usage, not the open execution cohort."""
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
import json
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'Stats/exporter'))
from pulse_compute_cost.ledger import UsageLedger
from pulse_compute_cost.receipts import Receipt, digest, reference
from pulse_compute_cost.receipt_sources import usage_receipt, actor_receipt
from pulse_compute_cost.stats_dashboard import build_dashboard, DEFAULT_FACTS
from pulse_stats.cost_dashboard import _validate

NOW = datetime(2026, 9, 16, 2, tzinfo=timezone.utc)
AT = NOW.isoformat()
DIMS = ['agent', 'wi', 'entrypoint', 'node', 'runtime', 'model']
CATALOG = {'retrieved_at': AT, 'catalog': {'data': [{'id': 'openai/test', 'pricing': {
    'prompt': '1', 'completion': '1', 'input_cache_read': '0.1', 'input_cache_write': '2'}}]}}


def active(identity, agent='hestia', wi='wi_20260826_825', node='ownermac',
           entrypoint='codex_app', at=AT, amount=1, model='openai/test'):
    row = usage_receipt('pi', identity, {'input': amount, 'output': 0,
        'cacheRead': 0, 'cacheWrite': 0}, at=at, observed=AT, model=model,
        common={'wi': wi, 'executor': agent, 'responsible': agent, 'delegator': None})
    return [row, actor_receipt(row, node=node, executor=agent, entrypoint=entrypoint, observed=AT)]


def expectation():
    return Receipt.make('execution', 'idle-pulse', {
        'wi': 'wi_20260820_568', 'executor': 'pulse', 'responsible': 'pulse',
        'delegator': None, 'runtime': 'codex', 'assignment_ref': reference('assignment', 'idle'),
        'parent_ref': None, 'state': 'running', 'expected_usage': True},
        occurred_at=AT, observed_at=AT, producer='synthetic_test.v1')


class ActivityTableTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.ledger = UsageLedger(Path(self.tmp.name) / 'ledger', clock=lambda: NOW)
        self.facts = json.loads(DEFAULT_FACTS.read_text())

    def build(self, rows=(), **kwargs):
        self.ledger.ingest_receipts(rows)
        return build_dashboard(self.ledger, subscriptions=self.facts, source_revision='a'*40,
            now=NOW, identity_dimensions=True, activity_table=True, catalog_snapshot=CATALOG, **kwargs)

    def table(self, doc, period='today'):
        return next(v for v in doc['views'] if v['dimension']=='activity' and v['period']==period)

    def test_idle_execution_does_not_become_activity_or_filter_option(self):
        doc = self.build([*active('real'), expectation()])
        table = self.table(doc)
        self.assertEqual(table['query']['group_by'], DIMS)
        self.assertEqual([g['key']['agent'] for g in table['groups']], ['hestia'])
        self.assertEqual([g['key']['wi'] for g in table['groups']], ['wi_20260826_825'])
        self.assertEqual(table['summary']['usage']['event_count'], 1)
        self.assertEqual(table['summary']['cost']['partial_machine_cash_usd'], '1')
        # Completeness is retained, not painted green by excluding an empty row.
        self.assertIn('incomplete_execution', table['summary']['cost']['reasons'])
        generic = next(v for v in doc['views'] if v['dimension']=='wi' and v['period']=='today')
        self.assertTrue(any(g['key']['wi']=='wi_20260820_568' for g in generic['groups']))
        _validate(doc)

    def test_joint_keys_never_cross_join_independent_dimension_groups(self):
        doc = self.build([*active('a', 'pulse', 'wi_20260827_282', 'ownermac'),
            *active('b', 'pulse', 'wi_20260823_184', 'tcloud', 'feishu'),
            *active('c', 'aki', 'wi_20260827_520', 'tcloud', 'feishu')])
        groups = self.table(doc)['groups']
        self.assertEqual(len(groups), 3)
        self.assertEqual({(g['key']['agent'],g['key']['wi'],g['key']['node']) for g in groups},
            {('pulse','wi_20260827_282','ownermac'), ('pulse','wi_20260823_184','tcloud'),
             ('aki','wi_20260827_520','tcloud')})
        self.assertEqual(sum(g['usage']['event_count'] for g in groups), 3)
        _validate(doc)

    def test_actual_zero_and_unpriced_activity_are_not_dropped(self):
        doc = self.build([*active('zero', amount=0),
                          *active('unpriced', 'aki', 'wi_20260916_101', model='unknown/test')])
        groups = self.table(doc)['groups']
        self.assertEqual(len(groups), 2)
        self.assertTrue(any(g['cost']['partial_machine_cash_usd']=='0' for g in groups))
        self.assertTrue(any(g['cost']['unpriced'] for g in groups))
        _validate(doc)

    def test_row_evidence_is_bounded_but_counts_remain_exact(self):
        rows=[]
        for i in range(9):
            rows.extend(active(f'evidence-{i}'))
        doc=self.build(rows)
        row=self.table(doc)['groups'][0]
        self.assertEqual(len(row['detail']['fact_refs']),1)
        self.assertEqual(len(row['detail']['attribution_refs']),1)
        self.assertGreater(row['detail']['fact_ref_count'],len(row['detail']['fact_refs']))
        self.assertGreater(row['detail']['attribution_ref_count'],len(row['detail']['attribution_refs']))

    def test_periods_do_not_inherit_other_days_or_empty_cohorts(self):
        doc = self.build([*active('today'), *active('yesterday','pulse','wi_20260827_282',
            at='2026-09-15T01:00:00Z'), expectation()])
        self.assertEqual(len(self.table(doc)['groups']), 1)
        self.assertEqual(len(self.table(doc, 'week')['groups']), 2)
        self.assertEqual({v['watermark'] for v in doc['views']}, {1})
        self.assertEqual(len(doc['views']), 21)
        _validate(doc)

    def test_empty_period_is_empty_not_unknown_cost_rows(self):
        doc = self.build([expectation()])
        self.assertEqual(self.table(doc)['groups'], [])
        self.assertIsNone(self.table(doc)['summary']['cost']['machine_cash_usd'])
        _validate(doc)

    def test_exporter_rejects_fabricated_zero_activity_and_duplicate_keys(self):
        doc = self.build(active('real'))
        for change in ('zero','missing-key','duplicate','missing-event'):
            bad = deepcopy(doc); table = self.table(bad)
            if change=='zero': table['groups'][0]['usage']['event_count']=0
            elif change=='missing-key': del table['groups'][0]['key']['wi']
            elif change=='duplicate': table['groups'].append(deepcopy(table['groups'][0]))
            else: table['summary']['usage']['event_count']=2
            bad['sha256']=digest({k:v for k,v in bad.items() if k!='sha256'})
            with self.subTest(change=change), self.assertRaises(ValueError): _validate(bad)

    def test_legacy_carrier_is_still_readable(self):
        self.ledger.ingest_receipts(active('legacy'))
        doc = build_dashboard(self.ledger, subscriptions=self.facts, source_revision='a'*40,
                              now=NOW, catalog_snapshot=CATALOG, identity_dimensions=True)
        self.assertEqual(len(doc['views']), 18)
        _validate(doc)

    def test_undeclared_execution_joins_the_group_of_its_declared_usage(self):
        execution = Receipt.make('execution', 'turn', {
            'wi': None, 'executor': 'hestia', 'responsible': None, 'delegator': None,
            'runtime': 'pi', 'assignment_ref': None, 'parent_ref': None,
            'state': 'completed', 'expected_usage': True},
            occurred_at=AT, observed_at=AT, producer='relay_host.v1')
        row = usage_receipt('pi', 'declared-turn', {'input': 1, 'output': 0, 'cacheRead': 0, 'cacheWrite': 0},
            at=AT, observed=AT, model='openai/test', execution_ref=execution.entity_ref,
            common={'wi': 'wi_20260826_825', 'executor': 'hestia', 'responsible': None, 'delegator': None})
        # Relay also names the execution's executor and entrypoint.
        rows = [execution, actor_receipt(execution, node='ownermac', executor='hestia', entrypoint='telegram', observed=AT),
                row, actor_receipt(row, node='ownermac', executor='hestia', entrypoint='telegram', observed=AT)]
        table = self.table(self.build([*rows, *active('other', wi=None, entrypoint='telegram')]))
        self.assertEqual(sorted(g['key']['wi'] for g in table['groups']), ['unassigned', 'wi_20260826_825'])
        for group in table['groups']:
            self.assertNotIn('incomplete_execution', group['cost']['reasons'], group['key'])


    def test_execution_is_missing_only_without_any_observed_usage(self):
        def turn(name, agent='hestia'):
            return Receipt.make('execution', name, {
                'wi': None, 'executor': agent, 'responsible': None, 'delegator': None,
                'runtime': 'pi', 'assignment_ref': None, 'parent_ref': None,
                'state': 'completed', 'expected_usage': True},
                occurred_at=AT, observed_at=AT, producer='relay_host.v1')
        seen, lost = turn('seen'), turn('lost', 'aki')
        # The declared native message replaces the turn aggregate that names the execution.
        message = usage_receipt('pi', 'message', {'input': 1, 'output': 0, 'cacheRead': 0, 'cacheWrite': 0},
            at=AT, observed=AT, model='openai/test',
            common={'wi': 'wi_20260826_825', 'executor': 'hestia', 'responsible': None, 'delegator': None})
        aggregate = usage_receipt('pi', 'aggregate', {'input': 1, 'output': 0, 'cacheRead': 0, 'cacheWrite': 0},
            at=AT, observed=AT, model='openai/test', execution_ref=seen.entity_ref,
            covers=[message.entity_ref], granularity='turn_aggregate',
            common={'wi': None, 'executor': 'hestia', 'responsible': None, 'delegator': None})
        context = dict(node='ownermac', executor='hestia', entrypoint='telegram', observed=AT)
        rows = [seen, lost, *(actor_receipt(r, **context) for r in (seen, message, aggregate)),
                actor_receipt(lost, **{**context, 'executor': 'aki'}), message, aggregate,
                *active('other', wi=None, entrypoint='telegram'),
                *active('aki-other', agent='aki', wi=None, entrypoint='telegram')]
        table = self.table(self.build(rows))
        flagged = sorted((g['key']['agent'], g['key']['wi']) for g in table['groups']
                         if 'incomplete_execution' in g['cost']['reasons'])
        self.assertEqual(flagged, [('aki', 'unassigned')])


    def test_rows_carry_the_name_and_purpose_of_the_wi_they_name(self):
        info = {'wi_20260826_825': {'title': '无界工作环境', 'summary': '让 Agent 在任何节点工作。', 'status': 'active'},
                'wi_20260101_001': {'title': 'unrelated', 'summary': None, 'status': 'completed'}}
        doc = self.build(active('named'), work_items=info)
        self.assertEqual(doc['work_items'], [{'id': 'wi_20260826_825', **info['wi_20260826_825']}])
        _validate(doc)
        self.assertNotIn('work_items', self.build())

    def test_work_item_display_reads_the_enrolled_core_export(self):
        from pulse_compute_cost.stats_dashboard import work_item_display
        path = Path(self.tmp.name) / 'wi-facts.json'
        path.write_text(json.dumps({'schema': 'harness.stats_facts/v1', 'read_only': True, 'items': [
            {'id': 'wi_20261017_632', 'title': '无界工作环境\n第二行', 'summary': '一句话。',
             'business_progress': 'active', 'status': 'active'},
            {'id': 'not-a-wi', 'title': 'x'}]}))
        path.chmod(0o600)
        registry = {'sources': [{'kind': 'harness_core', 'path': str(path)}]}
        self.assertEqual(work_item_display(registry), {'wi_20261017_632':
            {'title': '无界工作环境', 'summary': '一句话。', 'status': 'active', 'project_page': None}})
        path.chmod(0o644)
        self.assertIsNone(work_item_display(registry))
        self.assertIsNone(work_item_display({'sources': []}))

    def test_work_item_display_keeps_only_the_wis_own_kernel_page(self):
        from pulse_compute_cost.stats_dashboard import project_page_path
        wi = 'wi_20261028_253'
        good = '/Users/me/.local/share/pulse/tools/scl-core/views/' + wi + '.html'
        self.assertEqual(project_page_path(good, wi), good)
        for bad in (None, 3, 'views/' + wi + '.html', '/x/../' + wi + '.html', '/x/wi_20990101_001.html',
                    '/x/' + wi + '.txt', '/' + 'a' * 1100 + '/' + wi + '.html'):
            self.assertIsNone(project_page_path(bad, wi), bad)


if __name__ == '__main__': unittest.main()
