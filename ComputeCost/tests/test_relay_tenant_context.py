"""Authenticated product identity metadata; no current session membership backfill."""
from pathlib import Path
from dataclasses import replace
from datetime import datetime,timezone
from hashlib import sha256
import json,tempfile,unittest
from pulse_compute_cost.receipts import Receipt,ReceiptError,reference,canonical,current_receipts
from pulse_compute_cost.receipt_sources import actor_receipt,usage_receipt
from pulse_compute_cost.tenant_context import collect_tenant_context,load_tenant_registry
from pulse_compute_cost.relay_tenant_context import relay_user_declaration,validate_user_definitions,SCHEMA,FEATURE
from pulse_compute_cost.source_registration import tenant_candidate_registry
from pulse_compute_cost.collaboration_query import CollaborationQuery,query_collaboration
from pulse_compute_cost.ledger import UsageLedger

AT='2026-09-15T01:00:00Z';END='2026-09-15T01:01:00Z';NAMED='2026-10-06T07:00:00Z'
CATALOG={'data':[{'id':'openai/test','pricing':{'prompt':'1','completion':'1','input_cache_read':'0.1','input_cache_write':'2'}}]}

class RelayTenantTests(unittest.TestCase):
 def setUp(self):
  tmp=tempfile.TemporaryDirectory();self.addCleanup(tmp.cleanup);self.root=Path(tmp.name)
  self.inputs=self.root/'human-inputs';self.inputs.mkdir(mode=0o700)
  self.host={'id':'real-host','kind':'relay_host','path':str(self.root/'host'),'node':'ownermac','actor_attribution':True,'host_native_input':True,'human_input_directory':str(self.inputs)}
  self.registry={'schema':'computecost_source_registry/v1','ledger_root':str(self.root/'ledger'),'sources':[self.host]}
  self.config=self.root/'config.json';self.config.write_text(json.dumps({'users':[{'userId':'owner','agents':[{'id':'shared','credential':'private-never-export'}]},{'userId':'xb','agents':[{'id':'shared'}]}]}));self.config.chmod(0o600)
  self.document=relay_user_declaration(self.config,self.registry,names={'owner':'owner','xb':'xb'},observed_at=NAMED)
  self.declaration=self.root/'tenants.json';self.save()
  self.registry=tenant_candidate_registry(self.registry,self.declaration)
  self.usage=self.make_usage('one');self.input_ref=self.input('owner');self.actor=self.native_actor(self.usage,self.input_ref)
 def save(self):
  self.declaration.write_text(json.dumps(self.document));self.declaration.chmod(0o600)
 def input(self,user,channel='feishu',at=AT,pool='opaque-shared-route'):
  source={'channel':channel,'eventId':'event-'+user,'humanRef':user,'message':'private body must never appear in receipt'}
  value={'source':source,'poolKey':pool,'observedAtMs':int(datetime.fromisoformat(at.replace('Z','+00:00')).timestamp()*1000)}
  token=sha256(json.dumps([channel,source['eventId'],user,pool],ensure_ascii=False,separators=(',',':')).encode()).hexdigest()
  path=self.inputs/(token+'.json');raw=json.dumps(value).encode();path.write_bytes(raw);path.chmod(0o600)
  return reference('input_metadata',sha256(raw).hexdigest())
 def make_usage(self,name,runtime='pi'):
  return usage_receipt(runtime,name,({'input':120,'cacheRead':20,'cacheWrite':0,'output':10} if runtime=='pi' else {'input_tokens':120,'cache_read_input_tokens':20,'cache_creation_input_tokens':0,'output_tokens':10}),at=END,observed=END,model='openai/test',mapping='pi_native.v1' if runtime=='pi' else 'cc_message.v1',granularity='request',interval=(AT,END),native_scope_ref=reference('native_session',name))
 def native_actor(self,usage,ref,channel='feishu',executor='shared',node='ownermac'):
  return actor_receipt(usage,node=node,executor=executor,entrypoint=channel,input_ref=ref,observed=END,evidence=(ref,reference('authenticated_native_input',usage.entity_ref)))
 def collect(self,facts=None):return collect_tenant_context(facts or [self.usage,self.actor],self.registry,NAMED)
 def test_historical_identity_is_from_authenticated_event_not_naming_time_or_current_agent(self):
  self.config.write_text(json.dumps({'users':[{'userId':'owner','agents':[]},{'userId':'xb','agents':[]}]}))
  rows=self.collect();self.assertEqual(len(rows),1);self.assertEqual(rows[0].payload['context']['tenant']['id'],'owner')
  self.assertIsNone(rows[0].payload['wi']);self.assertEqual(rows[0].payload['context']['tenant']['usage_fact_ref'],self.usage.fact_ref)
  exported=canonical([r.to_dict() for r in rows]).decode();self.assertNotIn('private body',exported);self.assertNotIn('opaque-shared-route',exported);self.assertNotIn('credential',exported)
 def test_same_actor_pool_and_host_can_charge_two_explicit_product_users(self):
  second=self.make_usage('two');ref=self.input('xb');rows=self.collect([self.usage,self.actor,second,self.native_actor(second,ref)])
  self.assertEqual({r.payload['context']['tenant']['id'] for r in rows},{'owner','xb'})
 def test_no_input_proof_or_incomplete_binding_cannot_name_tenant(self):
  cases=[[],[actor_receipt(self.usage,node='ownermac',executor='shared',entrypoint='feishu',observed=END)],
   [self.native_actor(self.usage,reference('input_metadata','absent'))],
   [self.native_actor(self.usage,self.input_ref,channel='telegram')],
   [self.native_actor(self.usage,self.input_ref,node='foreign')],
   [replace(self.actor,producer='foreign.v1')]]
  for actors in cases:
   with self.subTest(actors=len(actors)):self.assertEqual(self.collect([self.usage,*actors]),[])
  self.assertEqual(self.collect([self.usage,self.native_actor(self.usage,self.input('owner',at='2026-09-15T01:00:01Z'))]),[])
 def test_direct_codex_automation_and_unregistered_user_stay_unknown(self):
  direct=self.make_usage('desktop','cc')
  self.assertEqual(self.collect([direct,actor_receipt(direct,node='ownermac',executor='shared',entrypoint='codex_app',observed=END)]),[])
  self.assertEqual(self.collect([self.usage,self.native_actor(self.usage,self.input('unknown-product-user'))]),[])
 def test_cc_route_binds_exact_original_dispatch_version_even_after_revision(self):
  usage=self.make_usage('cc','cc');ref=self.input('owner','telegram')
  original=actor_receipt(usage,node='ownermac',executor='shared',entrypoint='telegram',input_ref=ref,observed=END,evidence=(ref,))
  revised=Receipt.make('attribution',None,original.payload,entity_ref=original.entity_ref,producer=original.producer,occurred_at=original.occurred_at,observed_at=NAMED,supersedes=original.fact_ref,evidence=original.evidence)
  actor=actor_receipt(usage,node='ownermac',executor='shared',observed=END)
  route=Receipt.make('attribution',['cc_native_input_route',usage.entity_ref,ref],{'target_ref':usage.entity_ref,'wi':None,'executor':None,'responsible':None,'delegator':None,'state':'unassigned','policy':'registered_runtime_actor.v1','effective_at':AT,'scope':'actor','context':{'node':'ownermac','entrypoint':'telegram','workspace_ref':None}},producer='registered_runtime_actor.v1',occurred_at=AT,observed_at=END,evidence=(ref,original.fact_ref))
  # The original dispatch actor is on a separate execution identity.
  execution=reference('execution','original');p=original.payload;p['target_ref']=execution
  original=Receipt.make('attribution','host-input',p,producer=original.producer,occurred_at=AT,observed_at=END,evidence=(ref,))
  revised=Receipt.make('attribution',None,p,entity_ref=original.entity_ref,producer=original.producer,occurred_at=AT,observed_at=NAMED,supersedes=original.fact_ref,evidence=original.evidence)
  route=Receipt.make('attribution',None,route.payload,entity_ref=route.entity_ref,producer=route.producer,occurred_at=AT,observed_at=END,evidence=(ref,original.fact_ref))
  rows=self.collect([usage,actor,route,original,revised]);self.assertEqual(len(rows),1)
  bad=Receipt.make('attribution','foreign-route',route.payload,producer=route.producer,occurred_at=AT,observed_at=END,evidence=route.evidence)
  self.assertEqual(self.collect([usage,actor,bad,original,revised]),[])
 def test_opposite_actor_proof_and_stale_usage_version_cannot_gain_context(self):
  other=self.native_actor(self.usage,self.input('xb'),executor='other')
  self.assertEqual(self.collect([self.usage,self.actor,other]),[])
  payload=self.actor.payload;payload['usage_fact_ref']=reference('fact','stale')
  stale=Receipt.make('attribution','stale',payload,producer=self.actor.producer,occurred_at=AT,observed_at=END,evidence=self.actor.evidence)
  self.assertEqual(self.collect([self.usage,stale]),[])
 def test_compatible_native_point_can_refine_host_window_but_not_a_contrary_quantity(self):
  point=self.usage.payload;point.update(interval_start=None,interval_end=None)
  native=Receipt.make('usage',None,point,entity_ref=self.usage.entity_ref,producer=self.usage.producer,occurred_at=END,observed_at=END)
  p=native.payload;p.update(interval_start='2026-09-15T00:59:59Z',interval_end=END,executor='shared')
  host=Receipt.make('usage',None,p,entity_ref=native.entity_ref,producer='relay_host.v1',occurred_at=END,observed_at=END)
  rows=self.collect([native,host,self.actor]);self.assertTrue(rows)
  ledger=UsageLedger(self.root/'joined');ledger.ingest_receipts([native,host,self.actor,*rows])
  q=lambda:query_collaboration(ledger,CollaborationQuery(scope='activity',from_date='2026-09-15',to_date='2026-09-16',group_by=('tenant',)),catalog=CATALOG)
  self.assertEqual(q()['groups'][0]['key']['tenant'],'owner')
  self.assertEqual(q()['summary']['usage']['event_count'],1)
  coarse=self.collect([host,self.actor]);self.assertEqual(coarse,[])
  p=host.payload;p['tokens']['uncached_input']+=1
  contrary=Receipt.make('usage',None,p,entity_ref=host.entity_ref,producer=host.producer,occurred_at=END,observed_at=END)
  # Each original fact remains; a conflicting counter cannot borrow a native
  # point to backfill the coarse Host version's membership.
  conflicting=self.collect([native,contrary,self.actor])
  self.assertFalse([r for r in conflicting if r.payload['context']['tenant']['usage_fact_ref']==contrary.fact_ref])

 def test_tenant_survives_a_later_observation_version_of_the_same_request(self):
  point=self.usage.payload;point.update(interval_start=None,interval_end=None)
  native=Receipt.make('usage',None,point,entity_ref=self.usage.entity_ref,producer=self.usage.producer,occurred_at=END,observed_at=END)
  p=native.payload;p.update(interval_start='2026-09-15T00:59:59Z',interval_end=END,executor='shared')
  host=Receipt.make('usage',None,p,entity_ref=native.entity_ref,producer='relay_host.v1',occurred_at=END,observed_at=END)
  rows=self.collect([native,host,self.actor]);self.assertTrue(rows)
  bound=rows[0].payload['context']['tenant']['usage_fact_ref']
  # A later collection re-reads the transcript: the same request gains another
  # observation version, which may become the projected row's representative.
  for n in range(200):
   later=Receipt.make('usage',None,{**native.payload,'coverage_ref':reference('coverage',['transcript',n])},entity_ref=native.entity_ref,producer=native.producer,occurred_at=END,observed_at=END)
   if later.fact_ref<min(bound,native.fact_ref,host.fact_ref):break
  else:self.skipTest('no lower observation version in range')
  ledger=UsageLedger(self.root/'versions');ledger.ingest_receipts([native,host,self.actor,*rows,later])
  q=query_collaboration(ledger,CollaborationQuery(scope='activity',from_date='2026-09-15',to_date='2026-09-16',group_by=('tenant',)),catalog=CATALOG)
  self.assertEqual([g['key']['tenant'] for g in q['groups']],['owner'])
 def test_dispatch_route_names_tenant_for_a_slightly_earlier_native_twin(self):
  started='2026-09-15T01:00:00.020Z'
  execution=Receipt.make('execution',['host','notice'],{'wi':None,'executor':'shared','responsible':None,'delegator':None,
   'runtime':'pi','assignment_ref':None,'parent_ref':None,'state':'accepted','expected_usage':True},
   occurred_at=started,observed_at=END,producer='relay_host.v1',entity_ref=reference('attempt','notice'))
  p=self.usage.payload;p.update(interval_start='2026-09-15T01:00:00.017Z',execution_ref=execution.entity_ref)
  usage=Receipt.make('usage',None,p,entity_ref=self.usage.entity_ref,producer=self.usage.producer,occurred_at=END,observed_at=END)
  route=actor_receipt(execution,node='ownermac',executor='shared',entrypoint='feishu',input_ref=self.input_ref,observed=END,
   evidence=(self.input_ref,))
  rows=self.collect([execution,usage,route])
  self.assertEqual([r.payload['context']['tenant']['id'] for r in rows],['owner'])
  late=actor_receipt(execution,node='ownermac',executor='shared',
   entrypoint='feishu',input_ref=self.input_ref,observed=END,evidence=(self.input_ref,))
  moved=Receipt.make('attribution',['moved'],{**late.payload,'effective_at':'2026-09-15T01:00:00.030Z'},
   occurred_at='2026-09-15T01:00:00.030Z',observed_at=END,producer=late.producer)
  self.assertEqual(self.collect([execution,usage,moved]),[])

 def test_closed_schema_private_source_and_duplicate_metadata_fail_closed(self):
  self.assertIn(FEATURE,self.registry['required_receipt_features'])
  for change in ({'poolKey':'guess'},{'user_id':'unsafe/name'},{'source_ref':'raw-path'}):
   doc=json.loads(json.dumps(self.document));doc['relay_users'][0].update(change)
   with self.assertRaises(ReceiptError):validate_user_definitions(doc)
  self.document['relay_users'].append(dict(self.document['relay_users'][0]))
  with self.assertRaises(ReceiptError):self.save();load_tenant_registry(self.declaration)
  self.config.chmod(0o644)
  with self.assertRaises(ReceiptError):relay_user_declaration(self.config,self.registry,names={'owner':'owner','xb':'xb'},observed_at=NAMED)
 def test_private_ledger_amounts_old_query_raw_usage_and_repeat_preserved(self):
  ledger=UsageLedger(self.root/'ledger');facts=[self.usage,self.actor];ledger.ingest_receipts(facts);old=ledger.receipt_snapshot()
  q=lambda m=None:query_collaboration(ledger,CollaborationQuery(scope='activity',from_date='2026-09-15',to_date='2026-09-16',group_by=('tenant','agent'),watermark=m),catalog=CATALOG,catalog_observed_at=END)
  before=q(old['watermark']);rows=self.collect();ledger.ingest_receipts(rows);after=q()
  self.assertGreater(before['summary']['usage']['known_total_tokens'],0);self.assertIsNotNone(before['summary']['cost']['partial_machine_cash_usd'])
  self.assertEqual(before['summary']['usage']['tokens'],after['summary']['usage']['tokens']);self.assertEqual(before['summary']['cost'],after['summary']['cost'])
  self.assertEqual(q(old['watermark']),before);self.assertEqual(after['groups'][0]['key']['tenant'],'owner')
  self.assertEqual([r.to_dict() for r in ledger.receipt_snapshot()['facts'] if r.kind=='usage'],[self.usage.to_dict()]);self.assertEqual(ledger.ingest_receipts(rows)['inserted'],0)

if __name__=='__main__':unittest.main()
