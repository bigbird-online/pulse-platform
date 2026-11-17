"""Reviewed native timing survives ordinary feature-off collection/publication."""
from copy import deepcopy
from dataclasses import replace
from unittest.mock import patch
from datetime import datetime, timezone, timedelta
import json
import unittest

import test_codex_paginated_actor as native
from pulse_compute_cost.receipts import Receipt, canonical, current_receipts, reference
from pulse_compute_cost.receipt_sources import read_codex, collect_registered_sources, _revision
from pulse_compute_cost.native_metering import metering_refines, metered_observation
from pulse_compute_cost.ledger import UsageLedger
from pulse_compute_cost.stats_dashboard import build_dashboard, publish_dashboard
from pulse_compute_cost.collaboration_query import _request_observation_projection, CollaborationQuery, query_collaboration

class NativeMeteringPersistenceTests(unittest.TestCase):
    setUp = native.CodexPaginatedActorTests.setUp
    write = native.CodexPaginatedActorTests.write

    def prepare(self):
        # Native paginated baseline and token notification cross local midnight.
        for i, row in enumerate(self.rows):
            row['timestamp'] = '2026-09-15T15:59:00Z' if i < 3 else '2026-09-15T16:01:00Z'
        self.write()
        coarse = next(r for r in read_codex(self.logs, native.END, source_identity=self.source) if r.kind == 'usage')
        metered = next(r for r in read_codex(self.logs, native.END, source_identity=self.source, metering=True) if r.kind == 'usage')
        self.assertTrue(metering_refines(coarse, metered))
        # Retained original adapter lacked interval metadata. A reviewed append
        # recovers that interval plus timing without rewriting the old receipt.
        old = Receipt.make('usage', None, {**coarse.payload,'interval_start':None,'interval_end':None},
            entity_ref=coarse.entity_ref, occurred_at=coarse.occurred_at, observed_at=coarse.observed_at,
            producer=coarse.producer, evidence=coarse.evidence)
        self.ledger = UsageLedger(self.root/'ledger', clock=lambda:datetime(2026,9,16,3,tzinfo=timezone.utc))
        self.registry = {'schema':'computecost_source_registry/v1','sources':[deepcopy(self.source)]}
        self.ledger.ingest_receipts([old])
        self.original = self.ledger.receipt_snapshot()
        self.original_query = CollaborationQuery(scope='activity',from_date='2026-09-16',to_date='2026-09-17',timezone='Asia/Shanghai',watermark=self.original['watermark'])
        self.original_report = query_collaboration(self.ledger,self.original_query)
        approved = Receipt.make('usage',None,metered.payload,entity_ref=metered.entity_ref,
            occurred_at=metered.occurred_at,observed_at=metered.observed_at,producer=metered.producer,
            evidence=metered.evidence,supersedes=old.fact_ref)
        self.ledger.ingest_receipts([approved])
        return old, coarse, approved

    def publish(self):
        self.publication_number=getattr(self,'publication_number',0)+1
        doc=build_dashboard(self.ledger, subscriptions={'schema_version':'pulse_stats_procurement_facts/v1','subscriptions':[]},
            source_revision='a'*40, now=datetime(2026,9,16,3,10,tzinfo=timezone.utc)+timedelta(seconds=self.publication_number),
            identity_dimensions=True,activity_table=True,activity_only=True,
            catalog_snapshot={'retrieved_at':'2026-09-16T03:00:00Z','catalog':{'data':[{
                'id':'openai/gpt-6-astra','pricing':{'prompt':'1','completion':'1','input_cache_read':'0.1','input_cache_write':'1'}}]}})
        path=self.root/'dashboard.json';publish_dashboard(path,doc)
        self.assertEqual(json.loads(path.read_text()),doc)
        self.assertEqual(doc['status'],'available',doc['reason'])
        return next(v for v in doc['views'] if v['period']=='today')

    def test_natural_feature_off_collect_publish_repeats_preserve_reviewed_metering(self):
        old, coarse, approved=self.prepare()
        registry_before=canonical(self.registry);approved_before=self.ledger.receipt_snapshot()
        for force in (False,True,False):
            collect_registered_sources(self.registry,self.ledger,force_recompute=force)
            active, conflicts=current_receipts(self.ledger.receipt_snapshot()['facts'])
            self.assertNotIn(approved.entity_ref,conflicts)
            selected=next(r for r in active if r.entity_ref==approved.entity_ref)
            self.assertEqual(selected.fact_ref,approved.fact_ref)
            view=self.publish()
            self.assertEqual(view['summary']['usage']['known_total_tokens'],130)
            self.assertEqual(view['summary']['usage']['event_count'],1)
            self.assertEqual(view['summary']['usage']['time_unallocated_event_count'],0)
            self.assertEqual(view['summary']['usage']['conflicted_event_count'],0)
            self.assertIsNotNone(view['summary']['cost']['partial_machine_cash_usd'])
        self.assertEqual(query_collaboration(self.ledger,self.original_query),self.original_report)
        self.assertEqual(canonical(self.registry),registry_before)
        self.assertNotIn('required_receipt_features',self.registry)
        self.assertEqual(self.ledger.receipt_snapshot(watermark=self.original['watermark'])['sha256'],self.original['sha256'])
        self.assertEqual(self.ledger.receipt_snapshot(watermark=approved_before['watermark'])['sha256'],approved_before['sha256'])
        # A genuinely new turn is still collected while the earlier proof stays.
        total={k:native.TOTAL[k]+native.LAST[k] for k in native.TOTAL}
        self.rows.extend([{'type':'event_msg','timestamp':'2026-09-16T00:00:00Z','ordinal':7,'payload':{'type':'task_started','turn_id':'new-real-turn'}},
            {'type':'turn_context','timestamp':'2026-09-16T00:00:00Z','ordinal':8,'payload':{'model':'gpt-6-astra'}},
            {'type':'event_msg','timestamp':'2026-09-16T00:01:00Z','ordinal':9,'payload':{'type':'token_count','info':{'total_token_usage':total,'last_token_usage':native.LAST}}},
            {'type':'event_msg','timestamp':'2026-09-16T00:01:00Z','ordinal':10,'payload':{'type':'task_complete'}}])
        self.write();collect_registered_sources(self.registry,self.ledger)
        self.assertEqual(self.publish()['summary']['usage']['known_total_tokens'],260)

    def test_existing_parallel_coarse_head_is_auditable_and_repeats_converge(self):
        old, coarse, approved=self.prepare()
        collect_registered_sources(self.registry,self.ledger)  # Seed the actual actor/source metadata before comparing cycles.
        self.ledger.ingest_receipts([coarse])
        fork=self.ledger.receipt_snapshot()
        self.assertIn(approved.entity_ref,current_receipts(fork['facts'])[1])
        active,conflicts,groups,_=_request_observation_projection(*current_receipts(fork['facts']))
        self.assertFalse(conflicts)
        self.assertEqual(groups[approved.entity_ref],sorted([coarse.fact_ref,approved.fact_ref]))
        self.assertEqual(next(r for r in active if r.entity_ref==approved.entity_ref).fact_ref,approved.fact_ref)
        first=self.publish()
        for force in (False,True):
            collect_registered_sources(self.registry,self.ledger,force_recompute=force)
            after=self.publish()
            self.assertEqual(first['summary']['usage'],after['summary']['usage'])
            self.assertEqual(first['summary']['cost'],after['summary']['cost'])
        self.assertEqual(self.ledger.receipt_snapshot(watermark=fork['watermark'])['sha256'],fork['sha256'])
        self.assertEqual(query_collaboration(self.ledger,self.original_query),self.original_report)
        facts={r.fact_ref:r.to_dict() for r in self.ledger.receipt_snapshot()['facts']}
        for r in (old,coarse,approved):self.assertEqual(facts[r.fact_ref],r.to_dict())

    def test_changed_quantities_identities_or_timeline_are_never_hidden(self):
        _,coarse,approved=self.prepare()
        for change in ('tokens','model','interval','scope','wi','source','producer','time'):
            with self.subTest(change=change):
                p=deepcopy(coarse.payload);kw={}
                if change=='tokens':
                    p['tokens']['output']+=1;p['native_tokens']['output_tokens']+=1;p['reported_total_tokens']+=1
                if change=='model':p['model']='different-model'
                if change=='interval':p['interval_start']='2026-09-15T15:58:00Z'
                if change=='scope':p['native_scope_ref']=reference('scope','another')
                if change=='wi':p['wi']='wi_20260915_001'
                if change=='source':kw['evidence']=(reference('source','another'),)
                if change=='producer':kw['producer']='another-producer'
                if change=='time':kw['occurred_at']='2026-09-15T16:02:00Z'
                row=Receipt.make('usage',None,p,entity_ref=coarse.entity_ref,
                    occurred_at=kw.get('occurred_at',coarse.occurred_at),observed_at=coarse.observed_at,
                    producer=kw.get('producer',coarse.producer),evidence=kw.get('evidence',coarse.evidence))
                self.assertIsNone(metered_observation([approved,row]))
                if change!='source':
                    revised=[approved];_revision(revised,row);self.assertEqual(len(revised),2)
                    self.assertIsNone(revised[-1].supersedes)
        other=deepcopy(approved.payload);other['metering']['points'][0]['at']='2026-09-15T16:00:00Z'
        rival=Receipt.make('usage',None,other,entity_ref=approved.entity_ref,
            occurred_at=approved.occurred_at,observed_at=approved.observed_at,producer=approved.producer,evidence=approved.evidence)
        self.assertIsNone(metered_observation([approved,rival]))


    def test_revision_disjoint_source_coarse_observation_is_retained(self):
        _,coarse,approved=self.prepare()
        row=replace(coarse,evidence=(reference('source','independent-baseline'),))
        revised=[approved];_revision(revised,row)
        self.assertEqual(len(revised),2)
        self.assertEqual(revised[-1].fact_ref,row.fact_ref)
        self.assertIsNone(revised[-1].supersedes)
        self.assertIn(row.entity_ref,current_receipts(revised)[1])

    def test_revision_empty_source_coarse_observation_is_retained(self):
        _,coarse,approved=self.prepare();row=replace(coarse,evidence=())
        revised=[approved];_revision(revised,row)
        self.assertEqual(len(revised),2)
        self.assertEqual(revised[-1].fact_ref,row.fact_ref)
        self.assertIsNone(revised[-1].supersedes)
        self.assertIn(row.entity_ref,current_receipts(revised)[1])

    def test_registered_collector_retains_disjoint_and_empty_source_heads(self):
        base_root=self.root
        for i,evidence in enumerate(((reference('source','independent-baseline'),),())):
            with self.subTest(evidence=evidence):
                self.root=base_root/str(i);self.root.mkdir()
                _,coarse,approved=self.prepare();row=replace(coarse,evidence=evidence)
                with patch('pulse_compute_cost.receipt_sources.read_codex',return_value=[row]):
                    collect_registered_sources(self.registry,self.ledger,force_recompute=True)
                active,conflicts=current_receipts(self.ledger.receipt_snapshot()['facts'])
                self.assertIn(approved.entity_ref,conflicts)
                self.assertEqual({r.fact_ref for r in conflicts[approved.entity_ref]},
                                 {row.fact_ref,approved.fact_ref})
                projected,unresolved,groups,_=_request_observation_projection(active,conflicts)
                self.assertIn(approved.entity_ref,unresolved)
                self.assertNotIn(approved.entity_ref,groups)

    def test_forward_append_reverse_discard_and_projection_source_contracts(self):
        _,coarse,approved=self.prepare()
        for name,evidence in (('shared',coarse.evidence),('disjoint',(reference('source','other'),)),('empty',())):
            with self.subTest(source=name):
                row=replace(coarse,evidence=evidence)
                # The existing forward contract adds a validated timing fact
                # with an explicit prior edge, including legacy empty evidence.
                upgraded=[row];_revision(upgraded,replace(approved,supersedes=None))
                self.assertEqual(len(upgraded),2)
                self.assertEqual(upgraded[-1].supersedes,row.fact_ref)
                self.assertEqual(upgraded[0].to_dict(),row.to_dict())
                # Reverse discard and read-side join share the stronger source
                # contract because otherwise an incoming observation vanishes.
                reversed_rows=[approved];_revision(reversed_rows,row)
                self.assertEqual(len(reversed_rows),1 if name=='shared' else 2)
                self.assertEqual(metered_observation([approved,row]) is not None,name=='shared')

if __name__=='__main__':unittest.main()
