"""Outcome is upstream delivery evidence, never a token or review counter."""
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
import json
import sys
import unittest

from pulse_compute_cost.receipts import Receipt, digest, reference
from pulse_compute_cost.ledger import UsageLedger
from pulse_compute_cost.collaboration_query import CollaborationQuery, query_collaboration
from pulse_compute_cost.receipt_sources import collect_registered_sources, actor_receipt
from pulse_compute_cost.stats_dashboard import build_dashboard
from test_collaboration_receipts import delivery, review, usage, coverage, WI, AT, CATALOG
from test_delivery_value import review_snapshot
from test_harness_measurement import measurement_snapshot
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'Stats/exporter'))
from pulse_stats.cost_dashboard import _validate

NOW = datetime(2026, 9, 17, 8, tzinfo=timezone.utc)
REV = 'a' * 40


class OutcomeDashboardTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.ledger = UsageLedger(self.root / 'ledger', clock=lambda: NOW)

    def query(self, start='2026-09-14', end='2026-09-18', **kwargs):
        return query_collaboration(self.ledger, CollaborationQuery(scope='activity', from_date=start,
            to_date=end, **kwargs), catalog=CATALOG, catalog_observed_at=AT)

    def dashboard(self):
        result = build_dashboard(self.ledger, subscriptions={'schema_version':'pulse_stats_procurement_facts/v1','subscriptions':[]},
            source_revision=REV, now=NOW, catalog_snapshot={'retrieved_at':AT,'catalog':CATALOG},
            identity_dimensions=True, activity_table=True)
        _validate(result)
        return result

    def test_review_in_period_does_not_recount_prior_outcome(self):
        d = delivery(at='2026-09-13T01:00:00Z')
        r = review(d)
        r = Receipt.make('review', 'late-review', r.payload, occurred_at='2026-09-17T01:00:00Z', observed_at=NOW.isoformat(), producer='test')
        self.ledger.ingest_receipts([d, r])
        report = self.query()
        self.assertEqual(report['summary']['quality']['delivery_count'], 1)
        self.assertIsNone(report['summary']['outcomes']['count'])
        self.assertEqual(report['summary']['outcomes']['items'], [])
        self.assertEqual(self.query('2026-09-01')['summary']['outcomes']['count'], 1)

    def test_missing_review_only_and_known_empty_are_distinct(self):
        self.ledger.ingest_receipts([usage(at='2026-09-17T00:00:00Z')])
        self.assertIsNone(self.query()['summary']['outcomes']['count'])
        self.ledger.ingest_receipts([coverage('quality')])
        self.assertEqual(self.query()['summary']['outcomes']['count'], 0)
        self.ledger.ingest_receipts([delivery(status='unknown', at='2026-09-17T01:00:00Z')])
        result = self.query()['summary']['outcomes']
        self.assertIsNone(result['count']); self.assertFalse(result['items'][0]['counted'])

    def test_outcome_is_counted_once_across_two_entrypoints(self):
        d = delivery(at='2026-09-17T01:00:00Z')
        rows = [d, review(d)]
        for i, entry in enumerate(('feishu','telegram')):
            u = usage(identity=str(i), at='2026-09-17T02:00:00Z')
            rows += [u, actor_receipt(u,node='tcloud',executor='pulse',entrypoint=entry, observed=NOW.isoformat())]
        self.ledger.ingest_receipts(rows)
        doc = self.dashboard()
        joint = next(v for v in doc['views'] if v['dimension']=='activity' and v['period']=='today')
        self.assertEqual(joint['outcomes']['count'], 1)
        self.assertEqual(len(joint['groups']), 2)
        self.assertEqual([g['outcomes']['count'] for g in joint['groups']], [1,1])
        self.assertEqual({ref for g in joint['groups'] for ref in g['outcomes']['refs']}, {d.entity_ref})
        self.assertEqual(joint['summary']['cost']['partial_machine_cash_usd'], '200')
        self.assertEqual(joint['active_agents'], ['pulse'])
        self.assertEqual(joint['outcome_only_groups'], [])

    def test_outcome_producer_is_active_when_another_agent_has_wi_usage(self):
        d=delivery(at='2026-09-17T01:00:00Z')
        d=Receipt.make('delivery',None,{**d.payload,'responsible':'duke'},entity_ref=d.entity_ref,
            occurred_at=d.occurred_at,observed_at=d.observed_at,producer=d.producer)
        u=usage(at='2026-09-17T02:00:00Z')
        self.ledger.ingest_receipts([d,u,actor_receipt(u,node='tcloud',executor='pulse',entrypoint='feishu',observed=NOW.isoformat())])
        view=next(v for v in self.dashboard()['views'] if v['period']=='today' and v['dimension']=='activity')
        self.assertEqual(view['active_agents'],['duke','pulse'])
        self.assertEqual(view['outcomes']['count'],1)
        self.assertEqual(len(view['groups']),1)

    def test_outcome_only_wi_stays_visible_without_invented_cost(self):
        d = delivery(at='2026-09-17T01:00:00Z')
        self.ledger.ingest_receipts([d, review(d)])
        doc = self.dashboard()
        joint = next(v for v in doc['views'] if v['dimension']=='activity' and v['period']=='today')
        self.assertEqual(joint['groups'], [])
        self.assertEqual(joint['summary']['usage']['event_count'], 0)
        self.assertEqual(joint['outcomes']['count'], 1)
        row = joint['outcome_only_groups'][0]
        self.assertEqual(row['key']['wi'], WI)
        self.assertIsNone(row['cost']['partial_machine_cash_usd'])
        self.assertEqual(row['usage']['event_count'], 0)

    def test_late_revision_preserves_old_watermark(self):
        d = delivery(at='2026-09-17T01:00:00Z')
        self.ledger.ingest_receipts([d]); old = self.ledger.receipt_snapshot()
        changed = Receipt.make('delivery', None, {**d.payload, 'status':'rejected', 'accepted_at':None},
            entity_ref=d.entity_ref, supersedes=d.fact_ref, occurred_at=d.occurred_at,
            observed_at=NOW.isoformat(), producer='test')
        self.ledger.ingest_receipts([changed])
        self.assertEqual(self.query()['summary']['outcomes']['items'][0]['status'], 'rejected')
        self.assertEqual(self.query(watermark=old['watermark'])['summary']['outcomes']['items'][0]['status'], 'accepted')
        self.assertEqual(self.query()['summary']['outcomes']['count'], 1)
        self.assertEqual(self.ledger.receipt_snapshot(watermark=old['watermark'])['sha256'], old['sha256'])

    def test_registered_harness_bundle_to_actual_publisher(self):
        # Real import schema; synthetic values, no direct fabricated report fields.
        r, m = review_snapshot(), measurement_snapshot()
        m['efficiency']['delivery_accepted_at']='2026-09-17T01:00:00Z'
        m['evidence']['readouts']['efficiency.delivery_accepted_at']['value']='2026-09-17T01:00:00Z'
        m['evidence']['as_of']='2026-09-17T01:10:00Z'
        for readout in m['evidence']['readouts'].values():
            readout['as_of']='2026-09-17T01:10:00Z'
            readout['window']['end']='2026-09-17T01:10:00Z'
        r['reviewed_at']='2026-09-17T01:10:00Z'
        bundle = {'schema':'computecost_harness_import/v1','work_item_id':r['work_item_id'],
                  'review':r, 'measurement':m, 'responsible':'pulse'}
        source = self.root/'bundle.json'; source.write_text(json.dumps(bundle))
        registry={'schema':'computecost_source_registry/v1','ledger_root':str(self.ledger.root),
                  'sources':[{'id':'outcome-fixture','kind':'harness_bundle','path':str(source)}]}
        collect_registered_sources(registry,self.ledger)
        doc = self.dashboard()
        view = next(v for v in doc['views'] if v['period']=='today' and v['dimension']=='activity')
        self.assertEqual(view['outcomes']['count'], 1)
        self.assertEqual(view['outcomes']['items'][0]['status'], 'accepted')
        self.assertEqual(view['outcomes']['items'][0]['wi'], r['work_item_id'])
        self.assertEqual(view['summary']['usage']['event_count'], 0)
        self.assertEqual(view['outcome_only_groups'][0]['key']['agent'], 'pulse')
        before=self.ledger.receipt_snapshot();collect_registered_sources(registry,self.ledger)
        self.assertEqual(self.query()['summary']['outcomes']['count'], 1)
        self.assertEqual(self.ledger.receipt_snapshot(watermark=before['watermark'])['sha256'],before['sha256'])

    def test_foreign_outcome_or_duplicated_total_rejected(self):
        self.ledger.ingest_receipts([delivery(at='2026-09-17T01:00:00Z')])
        doc=self.dashboard()
        for mutation in ('count','reference','zero_cost'):
            changed=deepcopy(doc)
            view=next(v for v in changed['views'] if v['period']=='today' and v['dimension']=='activity')
            if mutation=='count':view['outcomes']['count']=999
            elif mutation=='reference':view['outcome_only_groups'][0]['outcomes']['refs']=['foreign']
            else:view['outcome_only_groups'][0]['cost']['partial_machine_cash_usd']='0'
            changed['sha256']=digest({k:v for k,v in changed.items() if k!='sha256'})
            with self.assertRaises(ValueError):_validate(changed)


if __name__ == '__main__':unittest.main()
