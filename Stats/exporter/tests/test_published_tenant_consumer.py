"""Untouched published six/seven-dimensional documents through the real reader."""
from copy import deepcopy
from datetime import datetime
import json
from pathlib import Path
import tempfile
import unittest

from Stats.exporter.tests.test_cost_dashboard import ROOT
from pulse_compute_cost.receipts import digest
from pulse_compute_cost.stats_dashboard import publish_dashboard
from pulse_stats.cost_dashboard import _validate, load_cost_dashboard, attach_cost_dashboard, usage_metrics

FIXTURES = Path(__file__).parent / 'fixtures'


class PublishedTenantConsumerTests(unittest.TestCase):
    def test_exact_published_documents_survive_exporter_and_snapshot_without_reprojection(self):
        examples = [('published-legacy-six-v1.json', 1235, [515, 1128, 3870],
                     '4271124ab029ff047210d0c5f6429bdc9951221750140bff7aaed9838a558020'),
                    ('published-tenant-seven-v1.json', 1244, [563, 1176, 3918],
                     '8ea31f8b6d3641d0cf3d6deafad0c07adedd3c00466da839158963dd43e14a3d')]
        for name, water, counts, publication in examples:
            with self.subTest(publication=publication), tempfile.TemporaryDirectory() as directory:
                raw = (FIXTURES/name).read_bytes(); doc = json.loads(raw)
                self.assertEqual(doc['sha256'], publication)
                self.assertEqual(doc['sha256'], digest({k:v for k,v in doc.items() if k != 'sha256'}))
                self.assertEqual([v['period'] for v in doc['views']], ['today', 'week', 'month'])
                self.assertEqual([v['summary']['usage']['event_count'] for v in doc['views']], counts)
                self.assertEqual({v['watermark'] for v in doc['views']}, {water})
                path = Path(directory)/'private'/'cost.json'; publish_dashboard(path, doc)
                now = datetime.fromisoformat(doc['generated_at'])
                self.assertEqual(load_cost_dashboard(path, now=now), doc)
                base = json.loads((ROOT/'Stats/PulseStats/Resources/Fixtures/ok-full.json').read_bytes())
                attached = json.loads(attach_cost_dashboard(json.dumps(base), path, now=now))
                self.assertEqual(attached['cost_dashboard'], doc)
                for key in set(base)-{'metrics', 'cost_dashboard'}:
                    self.assertEqual(attached[key], base[key])
                metrics = {m['id']:m for m in attached['metrics']}
                self.assertEqual(metrics['usage.today.source_note']['value'], counts[0])
                tokens = doc['views'][0]['summary']['usage']['tokens']
                self.assertEqual(metrics['usage.today.output_tokens']['value'], tokens['output']['known'])
                self.assertEqual(metrics['usage.by_agent']['value'],
                                 next(m['value'] for m in usage_metrics(doc, now=now) if m['id']=='usage.by_agent'))
                if water == 1244:
                    expected = json.loads((FIXTURES/'published-tenant-seven-snapshot-v2.json').read_bytes())
                    self.assertEqual(attached, expected)
                    self.assertEqual(sum(g['usage']['event_count'] for g in doc['views'][0]['groups']
                                         if g['key']['tenant']=='owner'), 479)
                    self.assertEqual(sum(g['usage']['event_count'] for g in doc['views'][0]['groups']
                                         if g['key']['agent']=='unassigned'), 0)

    def test_closed_tenant_contract_rejects_new_dimension_fields_and_invalid_evidence(self):
        original = json.loads((FIXTURES/'published-tenant-seven-v1.json').read_bytes())
        mutations = {
            'schema': lambda d,v,g: d.update(schema_version='pulse_stats_cost_dashboard/v2'),
            'view field': lambda d,v,g: v.update(extra=True),
            'query field': lambda d,v,g: v['query'].update(extra=True),
            'eighth dimension': lambda d,v,g: v['query']['group_by'].append('pool_key'),
            'eighth key': lambda d,v,g: g['key'].update(pool_key='private'),
            'group field': lambda d,v,g: g.update(extra=True),
            'missing population': lambda d,v,g: v.pop('population_scope'),
            'invalid population': lambda d,v,g: v.update(population_scope='all_users'),
            'filtered population mismatch': lambda d,v,g: v['query'].update(tenant='owner'),
            'missing tenant metadata': lambda d,v,g: g.pop('tenant'),
            'null tenant metadata': lambda d,v,g: g.update(tenant=None),
            'tenant id mismatch': lambda d,v,g: g['tenant'].update(id='other'),
            'tenant unknown field': lambda d,v,g: g['tenant'].update(extra=True),
            'unsafe name': lambda d,v,g: g['tenant'].update(name='raw:route'),
            'empty proof': lambda d,v,g: g['tenant'].update(binding_refs=[]),
            'invalid proof': lambda d,v,g: g['tenant'].update(binding_refs=['raw-route']),
            'duplicate proof': lambda d,v,g: g['tenant']['binding_refs'].append(g['tenant']['binding_refs'][0]),
            'bool watermark': lambda d,v,g: v.update(watermark=True),
            'mixed watermark': lambda d,v,g: v.update(watermark=v['watermark']+1),
            'invalid report digest': lambda d,v,g: v.update(report_sha256='bad'),
            'bool events': lambda d,v,g: g['usage'].update(event_count=True),
            'negative tokens': lambda d,v,g: g['usage']['tokens']['output'].update(known=-1),
            'invalid money': lambda d,v,g: g['cost'].update(machine_cash_usd='NaN'),
            'wrong period': lambda d,v,g: v['query'].update(from_date='2000-01-01'),
            'future price': lambda d,v,g: v.update(catalog_observed_at='2099-01-01T00:00:00Z'),
        }
        for period in range(3):
            for name, mutate in mutations.items():
                with self.subTest(period=period, mutation=name):
                    doc=deepcopy(original); view=doc['views'][period]
                    group=next(g for g in view['groups'] if 'tenant' in g)
                    mutate(doc, view, group)
                    doc['sha256']=digest({k:v for k,v in doc.items() if k!='sha256'})
                    with self.assertRaises((ValueError, TypeError, KeyError)):
                        _validate(doc)

    def test_tenant_sources_and_registry_conflicts_are_a_closed_optional_pair(self):
        original = json.loads((FIXTURES/'published-tenant-seven-v1.json').read_bytes())
        def checked(mutate):
            doc = deepcopy(original); group = next(g for g in doc['views'][0]['groups'] if 'tenant' in g)
            mutate(group['tenant']); doc['sha256'] = digest({k:v for k,v in doc.items() if k!='sha256'})
            _validate(doc)
        checked(lambda t: t.update(sources=['registry'], registry_conflicts=[]))
        checked(lambda t: t.update(sources=['registry', 'session'], registry_conflicts=['other']))
        for name, mutate in {
                'one of the pair': lambda t: t.update(sources=['registry']),
                'unknown source': lambda t: t.update(sources=['route'], registry_conflicts=[]),
                'empty sources': lambda t: t.update(sources=[], registry_conflicts=[]),
                'unsorted sources': lambda t: t.update(sources=['session', 'registry'], registry_conflicts=[]),
                'registry-only conflict': lambda t: t.update(sources=['registry'], registry_conflicts=['other']),
                'self conflict': lambda t: t.update(sources=['session'], registry_conflicts=[t['id']]),
                'unsafe conflict': lambda t: t.update(sources=['session'], registry_conflicts=['raw:route'])}.items():
            with self.subTest(mutation=name), self.assertRaises((ValueError, TypeError, KeyError)):
                checked(mutate)

    def test_outcome_only_rows_cannot_bypass_dimension_or_tenant_guards(self):
        original=json.loads((FIXTURES/'published-tenant-seven-v1.json').read_bytes())
        for mutate in (lambda g:g['key'].update(extra='bad'),
                       lambda g:g['tenant'].update(id='other'),
                       lambda g:g.update(extra=True)):
            with self.subTest(mutation=mutate):
                doc=deepcopy(original); v=doc['views'][0]
                g=deepcopy(next(g for g in v['groups'] if 'tenant' in g))
                g.pop('detail'); mutate(g); v['outcome_only_groups']=[g]
                doc['sha256']=digest({k:x for k,x in doc.items() if k!='sha256'})
                with self.assertRaisesRegex(ValueError, 'invalid (Outcome-only group|tenant identity)'):
                    _validate(doc)

    def test_published_tenant_refs_are_a_bounded_valid_witness(self):
        from unittest.mock import patch
        from ComputeCost.tests.test_tenant_context import TenantContextTests, END, CATALOG
        from pulse_compute_cost import stats_dashboard
        from pulse_compute_cost.tenant_context import collect_tenant_context
        fixture=TenantContextTests(); fixture.setUp(); self.addCleanup(fixture.doCleanups)
        second=fixture.make_usage('two','scope-two')
        facts=fixture.facts+[second,fixture.actor(second)]
        registry=fixture.registry([fixture.binding(),fixture.binding(second,tenant_name='Another declared label')])
        fixture.ledger.ingest_receipts(facts)
        fixture.ledger.ingest_receipts(collect_tenant_context(facts,registry,END))
        with patch.object(stats_dashboard,'TENANT_REF_LIMIT',1):
            document=stats_dashboard.build_dashboard(fixture.ledger,subscriptions={'schema_version':'pulse_stats_procurement_facts/v1','subscriptions':[]},
                source_revision='a'*40,now=datetime.fromisoformat('2026-09-15T04:00:00+00:00'),
                catalog_snapshot={'catalog':CATALOG,'retrieved_at':END},activity_table=True,activity_only=True,tenant_identity=True)
        _validate(document)
        for view in document['views']:
            self.assertEqual(len(view['groups'][0]['tenant']['binding_refs']),1)
            self.assertEqual(view['summary']['usage']['event_count'],2)

    def test_formal_producer_tenant_conflicts_and_missing_labels_preserve_consumption(self):
        from ComputeCost.tests.test_tenant_context import TenantContextTests, END, CATALOG
        from pulse_compute_cost.tenant_context import collect_tenant_context
        from pulse_compute_cost.stats_dashboard import build_dashboard
        for conflicting_membership in (True, False):
            with self.subTest(conflicting_membership=conflicting_membership):
                fixture=TenantContextTests(); fixture.setUp(); self.addCleanup(fixture.doCleanups)
                facts=fixture.facts
                if conflicting_membership:
                    bindings=[fixture.binding(),fixture.binding(tenant_id='team-b',tenant_name='Declared team B')]
                else:
                    second=fixture.make_usage('two','scope-two')
                    facts=facts+[second,fixture.actor(second)]
                    bindings=[fixture.binding(),fixture.binding(second,tenant_name='Another declared label')]
                registry=fixture.registry(bindings)
                fixture.ledger.ingest_receipts(facts)
                fixture.ledger.ingest_receipts(collect_tenant_context(facts,registry,END))
                document=build_dashboard(fixture.ledger,subscriptions={'schema_version':'pulse_stats_procurement_facts/v1','subscriptions':[]},
                    source_revision='a'*40,now=datetime.fromisoformat('2026-09-15T04:00:00+00:00'),
                    catalog_snapshot={'catalog':CATALOG,'retrieved_at':END},activity_table=True,activity_only=True,tenant_identity=True)
                _validate(document)
                for view in document['views']:
                    self.assertEqual(view['summary']['usage']['event_count'],1 if conflicting_membership else 2)
                    self.assertEqual(view['summary']['usage']['known_total_tokens'],130 if conflicting_membership else 260)
                    self.assertEqual(view['groups'][0]['tenant']['name'],None)
                    self.assertEqual(view['groups'][0]['tenant']['id'],'ambiguous' if conflicting_membership else 'team-a')
                    self.assertEqual(len(view['groups'][0]['tenant']['binding_refs']),2)
