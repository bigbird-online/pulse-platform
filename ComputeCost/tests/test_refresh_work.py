"""Refresh work must scale with relevant evidence, not every row per group."""
from copy import deepcopy
from datetime import timedelta
import sys
import unittest
from unittest.mock import patch
from pulse_compute_cost import collaboration_query as cq, stats_dashboard as sd
from pulse_compute_cost.receipts import digest
from test_calculation_cache import CalculationCacheTests
from test_collaboration_receipts import usage, coverage, CATALOG, AT

class RefreshWorkTests(unittest.TestCase):
    def test_clean_rows_do_not_repeat_conflict_dependency_joins(self):
        facts=[usage(identity=f'u{i}',wi=f'wi_20260913_{101+i%3:03}') for i in range(90)]
        class Memory:
            def receipt_snapshot(self,**kwargs):
                return dict(facts=facts,watermark=1,sha256='fixed',recorded_at=AT)
        calls=[]
        def count(frame,event,arg):
            if event=='call' and frame.f_code.co_name=='diagnostic_matches':calls.append(1)
        prior=sys.getprofile();sys.setprofile(count)
        try:
            report=cq.query_collaboration(Memory(),cq.CollaborationQuery(group_by=('wi','runtime')),catalog=CATALOG,catalog_observed_at=AT)
        finally:sys.setprofile(prior)
        self.assertEqual(report['summary']['usage']['event_count'],90)
        self.assertEqual(report['summary']['usage']['conflicted_event_count'],0)
        self.assertEqual(len(calls),0,'An empty rejected set must not join every clean usage')

    def test_view_normalizes_query_without_roundtripping_entire_audit_graph(self):
        class Memory:
            def receipt_snapshot(self,**kwargs):
                return dict(facts=[usage()],watermark=1,sha256='fixed',recorded_at=AT)
        report=cq.query_collaboration(Memory(),cq.CollaborationQuery(group_by=('agent','wi','entrypoint','node','runtime','model')),catalog=CATALOG,catalog_observed_at=AT)
        report['reconciliation']['test_retained_history']=['opaque']*10000
        report['report_sha256']=digest({k:v for k,v in report.items() if k!='report_sha256'})
        original=deepcopy(report);encode=sd.canonical
        def bounded(value):
            self.assertFalse(isinstance(value,dict) and 'reconciliation' in value,
                'Do not JSON encode and decode a full history just to normalize query tuples')
            return encode(value)
        with patch.object(sd,'canonical',side_effect=bounded):view=sd._view(report,'week','activity')
        self.assertEqual(view['query']['group_by'],['agent','wi','entrypoint','node','runtime','model'])
        self.assertEqual(report,original)
        view['groups'][0]['key']['wi']='changed'
        self.assertEqual(report,original)

    def test_unmodified_projection_reuses_immutable_receipt(self):
        row=coverage()
        prepared=cq._prepare_collaboration({'facts':[row]})
        self.assertIs(next(r for r in prepared[7] if r.fact_ref==row.fact_ref),row)

class CatalogRefreshWorkTests(CalculationCacheTests):
    def test_only_retrieval_time_change_reuses_exact_calculation_witness(self):
        first=self.build(activity_only=True);self.now+=timedelta(seconds=60)
        catalog=deepcopy(__import__('test_stats_dashboard').CATALOG)
        catalog['retrieved_at']=self.now.isoformat()
        with patch.object(cq.CollaborationQuerySession,'query',side_effect=AssertionError('same prices must reuse')):
            warm=self.build(activity_only=True,catalog_snapshot=catalog)
        self.assertEqual(warm['views'],first['views'])
        self.assertNotEqual(warm['generated_at'],first['generated_at'])
        forced=self.build(activity_only=True,catalog_snapshot=catalog,force_recompute=True)
        for a,b in zip(warm['views'],forced['views']):
            self.assertEqual({k:v for k,v in a.items() if k not in ('catalog_observed_at','report_sha256')},
                             {k:v for k,v in b.items() if k not in ('catalog_observed_at','report_sha256')})
        # Never attach a later observation to a historical/earlier price read.
        with patch.object(cq.CollaborationQuerySession,'query',autospec=True,side_effect=cq.CollaborationQuerySession.query) as run:
            earlier=self.build(activity_only=True)
        self.assertEqual(run.call_count,3)
        self.assertEqual(earlier['status'],'available')

class SharedRefreshInputsTests(unittest.TestCase):
    def test_diagnostic_projection_is_once_per_frozen_publication(self):
        frozen=cq.CollaborationQuerySession(dict(facts=[usage()],watermark=1,sha256='fixed',recorded_at=AT))
        with patch.object(cq,'_actor_projection',wraps=cq._actor_projection) as project:
            for dimensions in (('wi',),('runtime',),('agent',)):
                frozen.query(cq.CollaborationQuery(group_by=dimensions,watermark=1),catalog=CATALOG,catalog_observed_at=AT)
        self.assertEqual(project.call_count,2,'One normal + one rejected projection, not per view')

    def test_background_cadence_migrates_old_intervals_without_mutating_input(self):
        from Stats.exporter.tests.test_stats_deploy import d
        for interval in (600,900,1800):
            old={'StartInterval':interval,'ProcessType':'Background','Label':d.LABEL,'RunAtLoad':True,'keep':{'nested':[1]}}
            before=deepcopy(old);new=d.publisher_launchagent(old)
            self.assertEqual(new['StartInterval'],1800)
            self.assertEqual(old,before)
            self.assertEqual(new['keep'],old['keep'])
            self.assertEqual(d.publisher_launchagent(new),new)
