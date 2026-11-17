"""Private explicit tenant declarations; no product names inferred from routes."""
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
import json, tempfile, unittest

from pulse_compute_cost.receipts import Receipt, ReceiptError, reference, canonical
from pulse_compute_cost.receipt_sources import usage_receipt, actor_receipt, collect_registered_sources
from pulse_compute_cost.ledger import UsageLedger
from pulse_compute_cost.collaboration_query import CollaborationQuery, query_collaboration
from pulse_compute_cost.tenant_context import collect_tenant_context, load_tenant_registry, FEATURE, SCHEMA
from pulse_compute_cost.source_registration import tenant_candidate_registry
from pulse_compute_cost.stats_dashboard import build_dashboard

AT='2026-09-15T01:00:00Z';END='2026-09-15T01:01:00Z'
CATALOG={'data':[{'id':'openai/test','pricing':{'prompt':'1','completion':'1','input_cache_read':'0.1','input_cache_write':'2'}}]}
DIMS=('tenant','agent','wi','entrypoint','node','runtime','model')

class TenantContextTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name);self.path=self.root/'tenant-registry.json'
        self.ledger=UsageLedger(self.root/'ledger',clock=lambda:datetime(2026,9,15,4,tzinfo=timezone.utc))
        self.usage=self.make_usage('one','scope-one')
        self.facts=[self.usage,self.actor(self.usage)]
    def make_usage(self,identity,scope):
        return usage_receipt('codex',identity,{'input_tokens':120,'cached_input_tokens':20,'cache_write_input_tokens':0,'output_tokens':10},
            at=END,observed=END,model='openai/test',mapping='codex_cli.v1',granularity='request',interval=(AT,END),
            native_scope_ref=reference('native_session',scope))
    def actor(self,target):
        return actor_receipt(target,node='ownermac',executor='shared',entrypoint='codex_app',observed=END,
            evidence=(reference('workspace_registry','private-fixture'),))
    def binding(self,target=None,**changes):
        target=target or self.usage
        return {'tenant_id':'team-a','tenant_name':'Declared team A','agent':'shared','node':'ownermac','runtime':'codex',
            'native_scope_ref':target.payload['native_scope_ref'],'effective_from':AT,'effective_to':'2026-09-16T00:00:00Z',**changes}
    def registry(self,bindings):
        self.path.write_text(json.dumps({'schema':SCHEMA,'bindings':bindings}));self.path.chmod(0o600)
        return {'schema':'computecost_source_registry/v1','ledger_root':str(self.ledger.root), 'sources':[],
            'required_receipt_features':[FEATURE],'tenant_registry_path':str(self.path)}
    def query(self,watermark=None,groups=DIMS):
        return query_collaboration(self.ledger,CollaborationQuery(from_date='2026-09-15',to_date='2026-09-16',scope='activity',
            group_by=groups,watermark=watermark),catalog=CATALOG,catalog_observed_at=END)
    def test_missing_registry_is_unknown_and_never_excludes_platform_activity(self):
        self.assertEqual(collect_tenant_context(self.facts,{},END),[])
        self.ledger.ingest_receipts(self.facts);report=self.query()
        self.assertEqual(report['groups'][0]['key']['tenant'],'unassigned')
        self.assertEqual(report['summary']['usage']['event_count'],1)
        self.assertEqual(report['summary']['cost']['partial_machine_cash_usd'],'112')
    def test_registration_requires_private_file_feature_and_closed_schema(self):
        registry=self.registry([self.binding()]);original=deepcopy(registry)
        registry['required_receipt_features']=[]
        with self.assertRaises(ReceiptError):collect_tenant_context(self.facts,registry,END)
        candidate=tenant_candidate_registry(original,self.path)
        self.assertIn(FEATURE,candidate['required_receipt_features']);self.assertEqual(original['sources'],candidate['sources'])
        self.path.chmod(0o644)
        with self.assertRaises(ReceiptError):load_tenant_registry(self.path)
        self.path.chmod(0o600);doc=json.loads(self.path.read_text());doc['bindings'][0]['poolKey']='route:agent:feishu';self.path.write_text(json.dumps(doc))
        with self.assertRaises(ReceiptError):load_tenant_registry(self.path)
    def test_only_exact_declared_actor_node_runtime_scope_and_window_match(self):
        for change in ({'agent':'other'},{'node':'vultr'},{'runtime':'pi'},
                       {'native_scope_ref':reference('native_session','other')},
                       {'effective_from':'2026-09-15T01:00:01Z'},
                       {'effective_to':'2026-09-15T01:00:59Z'}):
            with self.subTest(change=change):self.assertEqual(collect_tenant_context(self.facts,self.registry([self.binding(**change)]),END),[])
        rows=collect_tenant_context(self.facts,self.registry([self.binding()]),END)
        self.assertEqual(len(rows),1);self.assertIsNone(rows[0].payload['wi'])
        self.assertEqual(rows[0].payload['context']['tenant']['id'],'team-a')
        self.assertNotIn('poolKey',canonical(rows[0].to_dict()).decode())
    def test_same_named_agent_in_two_tenants_has_distinct_joint_keys_and_conserved_totals(self):
        second=self.make_usage('two','scope-two');facts=self.facts+[second,self.actor(second)]
        registry=self.registry([self.binding(),self.binding(second,tenant_id='team-b',tenant_name='Declared team B')])
        self.ledger.ingest_receipts(facts);before=self.query(groups=DIMS[1:])
        mark=self.ledger.receipt_snapshot()['watermark'];old=self.query(watermark=mark,groups=DIMS[1:])
        memberships=collect_tenant_context(facts,registry,END);self.ledger.ingest_receipts(memberships)
        report=self.query();self.assertEqual({g['key']['tenant'] for g in report['groups']},{'team-a','team-b'})
        self.assertEqual({g['key']['agent'] for g in report['groups']},{'shared'})
        self.assertEqual(report['summary']['cost'],before['summary']['cost'])
        self.assertEqual(report['summary']['usage']['known_total_tokens'],260)
        self.assertEqual(sum(g['usage']['event_count'] for g in report['groups']),2)
        self.assertEqual(self.query(watermark=mark,groups=DIMS[1:])['report_sha256'],old['report_sha256'])
        self.assertEqual(self.ledger.ingest_receipts(memberships)['inserted'],0)
    def test_conflicting_membership_remains_explicit_without_repricing_or_dropping_usage(self):
        self.ledger.ingest_receipts(self.facts);before=self.query()
        registry=self.registry([self.binding(),self.binding(tenant_id='team-b',tenant_name='Declared team B')])
        self.ledger.ingest_receipts(collect_tenant_context(self.facts,registry,END));after=self.query()
        self.assertEqual(after['groups'][0]['key']['tenant'],'ambiguous')
        self.assertEqual(len(after['groups'][0]['tenant']['binding_refs']),2)
        self.assertEqual(after['summary']['cost'],before['summary']['cost'])
        self.assertEqual(after['summary']['usage']['event_count'],1)
    def test_unknown_start_is_not_named_from_an_end_timestamp(self):
        p=self.usage.payload;p.update(interval_start=None,interval_end=None,granularity='turn_aggregate')
        coarse=replace(self.usage,_payload_json=canonical(p))
        self.assertEqual(collect_tenant_context([coarse,self.actor(coarse)],self.registry([self.binding()]),END),[])
    def test_malformed_identity_or_display_proof_is_rejected(self):
        for change in ({'tenant_id':'unassigned'},{'tenant_name':'route:agent'},{'tenant_name':'/private/path'},
                       {'tenant_name':'bad\nname'},{'native_scope_ref':'raw-session'},
                       {'effective_from':'2026-09-17T00:00:00Z'}):
            with self.subTest(change=change):
                self.registry([self.binding(**change)])
                with self.assertRaises(ReceiptError):load_tenant_registry(self.path)
        row=collect_tenant_context(self.facts,self.registry([self.binding()]),END)[0]
        p=row.payload;p['context']['tenant']['binding_ref']='unproved'
        with self.assertRaises(ReceiptError):Receipt.from_dict({**row.to_dict(),'payload':p})
    def test_publication_optin_preserves_platform_summary_and_legacy_shape(self):
        self.ledger.ingest_receipts(self.facts)
        args=dict(subscriptions={'schema_version':'pulse_stats_procurement_facts/v1','subscriptions':[]},source_revision='a'*40,
            now=datetime(2026,9,15,4,tzinfo=timezone.utc),catalog_snapshot={'catalog':CATALOG,'retrieved_at':END},activity_table=True,activity_only=True)
        legacy=build_dashboard(self.ledger,**args);new=build_dashboard(self.ledger,tenant_identity=True,**args)
        for old,view in zip(legacy['views'],new['views']):
            self.assertEqual(view['query']['group_by'],list(DIMS));self.assertEqual(old['summary']['cost'],view['summary']['cost'])
            self.assertEqual(view['population_scope'],'all_registered_tenants')
            self.assertEqual(view['groups'][0]['key']['tenant'],'unassigned')
            self.assertEqual(view['summary']['usage']['event_count'],old['summary']['usage']['event_count'])
    def test_native_collector_repeated_membership_is_idempotent_and_old_watermark_preserved(self):
        import test_codex_paginated_actor as native
        fixture=native.CodexPaginatedActorTests();fixture.setUp();self.addCleanup(fixture.doCleanups)
        ledger=UsageLedger(self.root/'native-ledger',clock=lambda:datetime(2026,9,15,4,tzinfo=timezone.utc))
        registry={'schema':'computecost_source_registry/v1','ledger_root':str(ledger.root),'sources':[fixture.source]}
        collect_registered_sources(registry,ledger);before=ledger.receipt_snapshot()
        target=next(r for r in before['facts'] if r.kind=='usage')
        prepared=self.registry([self.binding(target,agent='pulse')]);registry.update(tenant_registry_path=prepared['tenant_registry_path'],required_receipt_features=[FEATURE])
        collect_registered_sources(registry,ledger)
        rows=ledger.receipt_snapshot()['facts'];membership=[r for r in rows if r.kind=='attribution' and r.payload.get('context',{}).get('tenant')]
        self.assertEqual(len(membership),1)
        self.assertEqual(collect_registered_sources(registry,ledger)['inserted'],0)
        self.assertEqual(ledger.receipt_snapshot(watermark=before['watermark'])['sha256'],before['sha256'])

if __name__=='__main__':unittest.main()
