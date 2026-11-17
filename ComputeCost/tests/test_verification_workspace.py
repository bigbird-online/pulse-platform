"""Native metadata + real Git fixture; no provider, current authority or secrets."""
from pathlib import Path
from copy import deepcopy
from datetime import datetime,timezone
import hashlib,json,socket,subprocess,tempfile,unittest
import yaml
from pulse_compute_cost.receipts import Receipt,ReceiptError,reference
from pulse_compute_cost.receipt_sources import read_codex,actor_receipt,collect_registered_sources
from pulse_compute_cost.verification_workspace import candidate_sha256,accepted_window
from pulse_compute_cost.collaboration_query import CollaborationQuery,query_collaboration,_actor_projection
from pulse_compute_cost.ledger import UsageLedger

WI='wi_20260924_219'; HO='ho_20261006_531'
SID='11111111-1111-4111-8111-111111111111'; CHILD='22222222-2222-4222-8222-222222222222'
FREEZE='2026-10-01T00:00:00Z'; CREATED='2026-10-01T00:01:00Z'; ACK='2026-10-01T00:02:00Z'
REG='2026-10-01T00:03:00Z'; START='2026-10-01T00:04:00Z'; END='2026-10-01T00:05:00Z'; EXPIRED='2026-10-01T00:10:00Z'
OBS='2026-10-02T00:00:00Z'

def metadata():
 criteria=['independent verification']
 fp='sha256:'+hashlib.sha256(json.dumps(criteria,ensure_ascii=False,separators=(',',':')).encode()).hexdigest()
 item={'work_item_id':WI,'status':'awaiting_verification','owner':'hestia','outcome_producer':'hestia',
       'outcome_claim':'frozen fixture','outcome_change_type':'mixed','outcome_acceptance_fingerprint':fp,
       'acceptance_criteria':criteria,'updated_at':FREEZE}
 binding={'assignment_id':HO,'host':socket.gethostname(),'owner':'hestia','producer':'hestia',
          'verifier':'outcome-verifier','candidate_sha256':candidate_sha256(item)}
 contract={'handoff_id':HO,'work_item_id':WI,'from_owner':'hestia','to_owner':'outcome-verifier',
       'to_owners':['outcome-verifier'],'purpose':'outcome_verification','responsibility_transfer':'not_transferred',
       'original_responsible_owner':'hestia','created_at':CREATED,'ack_state':'accepted'}
 def event(eid,kind,at,operator,p):return {'event_id':eid,'event_type':kind,'timestamp':at,'operator':operator,'work_item_id':WI,'payload':p}
 audits=[event('created','handoff.created',CREATED,'hestia',{k:contract[k] for k in ('handoff_id','to_owner','purpose','responsibility_transfer','original_responsible_owner')}),
         event('accepted','handoff.accepted',ACK,'outcome-verifier',{'handoff_id':HO,'from_state':'pending','to_state':'accepted'}),
         event('ended','work_item.blocked',EXPIRED,'outcome-verifier',{'to_status':'blocked'})]
 return item,binding,contract,audits


class HistoricalWindowTests(unittest.TestCase):
 def test_exact_past_candidate_uses_audit_ack_and_end(self):
  item,b,c,a=metadata(); first,last,proof=accepted_window(item,c,a,b)
  self.assertEqual(first.isoformat(),'2026-10-01T00:02:00+00:00');self.assertEqual(last.isoformat(),'2026-10-01T00:10:00+00:00')
  self.assertEqual(len(proof),3)
 def test_missing_acceptance_wrong_caller_or_fingerprint_never_backfills(self):
  for change in ('missing_ack','wrong_caller','fingerprint','candidate_updated','receiver','criteria'):
   with self.subTest(change=change):
    item,b,c,a=metadata()
    if change=='missing_ack':a.pop(1)
    if change=='wrong_caller':a[1]['operator']='hestia'
    if change=='fingerprint':b['candidate_sha256']='0'*64
    if change=='candidate_updated':item['updated_at']=EXPIRED
    if change=='receiver':c['to_owners']=['outcome-verifier','another']
    if change=='criteria':item['acceptance_criteria']=['changed']
    with self.assertRaises(ReceiptError):accepted_window(item,c,a,b)
 def test_second_live_assignment_and_duplicate_audit_fail_closed(self):
  item,b,c,a=metadata(); other=deepcopy(a[0]);other['event_id']='other';other['payload']['handoff_id']='ho_20261006_019'
  with self.assertRaises(ReceiptError):accepted_window(item,c,[*a,other],b)
  with self.assertRaises(ReceiptError):accepted_window(item,c,[*a,a[1]],b)
 def test_revoked_assignment_ends_its_window(self):
  item,b,c,a=metadata(); r=deepcopy(a[1]);r.update(event_id='revoked',event_type='handoff.withdrawn',timestamp=END)
  r['payload'].update(from_state='accepted',to_state='withdrawn')
  self.assertEqual(accepted_window(item,c,[*a,r],b)[1].isoformat(),'2026-10-01T00:05:00+00:00')


class VerificationCollectorTests(unittest.TestCase):
 def setUp(self):
  tmp=tempfile.TemporaryDirectory();self.addCleanup(tmp.cleanup);self.root=Path(tmp.name).resolve()
  self.repo=self.root/'repo';self.repo.mkdir(mode=0o700)
  def git(*args):return subprocess.check_output(['git','-C',str(self.repo),*args],stderr=subprocess.DEVNULL,text=True).strip()
  self.git=git;git('init');git('config','user.email','fixture@example.invalid');git('config','user.name','Fixture')
  self.authority=self.repo/'.harness';(self.authority/'work-items').mkdir(parents=True)
  self.item,self.binding,self.contract,self.audits=metadata()
  (self.authority/'work-items'/(WI+'.yaml')).write_text(yaml.safe_dump(self.item))
  git('add','.');git('commit','-m','immutable fixture');base=git('rev-parse','HEAD')
  self.cwd=self.root/'registered-verification';git('worktree','add','-b','verification-fixture',str(self.cwd))
  self.registration_root=self.root/'registry';self.registration_root.mkdir(mode=0o700)
  rp=self.registration_root/WI/'verification'/HO/'surface_v1_local_macos.yaml';rp.parent.mkdir(parents=True)
  self.registration=rp
  reg={'schema_version':'work_item_workspace_registration/v1','work_item_id':WI,'agent_id':'outcome-verifier',
       'registered_by':'outcome-verifier','surface_id':'surface_v1_local_macos','created_at':REG,'verification':self.binding,
       'repositories':[{'shared_root':str(self.repo),'worktree_path':str(self.cwd),'git_common_dir':str(self.repo/'.git'),
                        'base_sha':base,'head_sha_at_registration':base}]}
  rp.write_text(yaml.safe_dump(reg));self.write_authority()
  self.native=self.root/'native';self.native.mkdir(mode=0o700)
  self.source={'id':'native-fixture','kind':'codex','path':str(self.native),'actor_attribution':True,'node':'ownermac',
      'scl_surface_id':'surface_v1_local_macos','workspace_actors':[],
      'verification_workspace_registry':{'registration_root':str(self.registration_root),'authority_root':str(self.authority),
                                        'host':socket.gethostname(),'surface_id':'surface_v1_local_macos'}}
 def write_authority(self):
  (self.authority/'handoffs').mkdir(exist_ok=True);(self.authority/'events').mkdir(exist_ok=True)
  (self.authority/'handoffs'/(HO+'.yaml')).write_text(yaml.safe_dump(self.contract))
  (self.authority/'events'/(WI+'.events.yaml')).write_text(yaml.safe_dump_all(self.audits))
 def native_stream(self,sid=SID,*,cwd=None,start=START,end=END,parent=None):
  source='exec' if parent is None else {'subagent':{'thread_spawn':{'parent_thread_id':parent}}}
  rows=[{'type':'session_meta','timestamp':start,'payload':{'id':sid,'timestamp':start,'cwd':str(cwd or self.cwd),'originator':'Codex Desktop','source':source}},
        {'type':'event_msg','timestamp':start,'payload':{'type':'task_started','turn_id':'one'}},
        {'type':'turn_context','timestamp':start,'payload':{'model':'gpt-6-astra'}},
        {'type':'event_msg','timestamp':end,'payload':{'type':'token_count','info':{'total_token_usage':{'input_tokens':120,'cached_input_tokens':20,'cache_write_input_tokens':0,'output_tokens':10,'reasoning_output_tokens':0}}}},
        {'type':'event_msg','timestamp':end,'payload':{'type':'task_complete'}}]
  path=self.native/(sid+'.jsonl');path.write_text(''.join(json.dumps(r)+'\n' for r in rows));path.chmod(0o600)
  return path
 def read(self,diagnostics=None):return read_codex(self.native,OBS,source_identity=self.source,diagnostic_out=diagnostics)
 def query(self,ledger,watermark=None):
  return query_collaboration(ledger,CollaborationQuery(scope='activity',from_date='2026-10-01',to_date='2026-10-02',timezone='UTC',group_by=('agent','wi','entrypoint'),watermark=watermark))
 def test_official_source_enrollment_is_separate_and_preserves_unrelated_tenant_features(self):
  from pulse_compute_cost.source_registration import verification_candidate_registry
  registry={'schema':'computecost_source_registry/v1','ledger_root':str(self.root/'candidate-ledger'),
    'sources':[{'id':'harness','kind':'harness_root','path':str(self.authority)},deepcopy(self.source)],
    'required_receipt_features':['tenant_context.v1','tenant_relay_input.v1']}
  del registry['sources'][1]['verification_workspace_registry']
  original=deepcopy(registry)
  result=verification_candidate_registry(registry,registration_root=self.registration_root,authority_root=self.authority,
       host=socket.gethostname(),surface_id='surface_v1_local_macos')
  self.assertEqual(registry,original)
  self.assertEqual(result['ledger_root'],registry['ledger_root'])
  self.assertEqual(set(result['required_receipt_features']),{'tenant_context.v1','tenant_relay_input.v1','verification_workspace.v1'})
  self.assertEqual(result['sources'][1]['verification_workspace_registry'],self.source['verification_workspace_registry'])

 def test_real_collector_root_and_child_use_own_historical_workspace(self):
  self.native_stream();self.native_stream(CHILD,parent=SID)
  before=[r.to_dict() for r in read_codex(self.native,OBS) if r.kind=='usage']
  # Today's changed candidate is not substituted for the immutable base.
  changed={**self.item,'updated_at':EXPIRED,'status':'blocked'}
  (self.authority/'work-items'/(WI+'.yaml')).write_text(yaml.safe_dump(changed))
  rows=self.read();usages=[r for r in rows if r.kind=='usage'];tasks=[r for r in rows if r.producer=='verification_workspace.v1']
  projected,contexts,_,_= _actor_projection(usages,rows,{})
  self.assertEqual(before,[r.to_dict() for r in usages]);self.assertEqual(len(tasks),2)
  self.assertEqual({r.payload['executor'] for r in projected},{'outcome-verifier'})
  self.assertEqual({c['entrypoint'] for c in contexts.values()},{'codex_app'})
  self.assertEqual({r.payload['wi'] for r in tasks},{WI})
  self.assertTrue(all(r.payload['usage_fact_ref'] in {u.fact_ref for u in usages} for r in tasks))
 def test_ack_registration_and_expiry_boundaries_never_grant_actor_or_task(self):
  for start,end in ((CREATED,END),(ACK,END),(START,EXPIRED),(START,'2026-10-01T00:11:00Z')):
   with self.subTest(start=start,end=end):
    self.native_stream(start=start,end=end);diagnostics=[];rows=self.read(diagnostics)
    self.assertEqual(sum(r.kind=='usage' for r in rows),1)
    self.assertFalse(any(r.producer=='verification_workspace.v1' or r.payload.get('executor')=='outcome-verifier' for r in rows))
    self.assertIn('verification_workspace_interval_unproven',diagnostics)
 def test_child_other_cwd_keeps_parent_origin_without_parent_actor_or_wi(self):
  self.native_stream();self.native_stream(CHILD,parent=SID,cwd=self.repo)
  rows=self.read();usages=[r for r in rows if r.kind=='usage'];p,ctx,_,_=_actor_projection(usages,rows,{})
  by={r.payload['native_scope_ref']:r for r in p};child=by[reference('native_session',['codex',CHILD])]
  self.assertIsNone(child.payload['executor']);self.assertEqual(ctx[child.entity_ref]['entrypoint'],'codex_app')
  self.assertFalse(any(r.producer=='verification_workspace.v1' and r.payload['target_ref']==child.entity_ref for r in rows))
 def test_invalid_host_git_or_candidate_preserves_usage(self):
  self.native_stream()
  for kind in ('host','common','candidate','audit'):
   with self.subTest(kind=kind):
    original=self.registration.read_bytes();source=deepcopy(self.source)
    if kind=='host':self.source['verification_workspace_registry']['host']='wrong-host'
    else:
     reg=yaml.safe_load(original)
     if kind=='common':reg['repositories'][0]['git_common_dir']=str(self.root/'other')
     if kind=='candidate':reg['verification']['candidate_sha256']='0'*64
     if kind=='audit':self.audits[1]['operator']='hestia';self.write_authority()
     self.registration.write_text(yaml.safe_dump(reg))
    diagnostics=[];rows=self.read(diagnostics)
    self.assertEqual(sum(r.kind=='usage' for r in rows),1);self.assertFalse(any(r.producer=='verification_workspace.v1' for r in rows));self.assertTrue(diagnostics)
    self.registration.write_bytes(original);self.source=source
    self.item,self.binding,self.contract,self.audits=metadata();self.write_authority()
 def test_canonical_version_reanchor_historical_read_repeat_and_later_expiry(self):
  self.native_stream();plain=read_codex(self.native,OBS);u=next(r for r in plain if r.kind=='usage')
  source_proof=(reference('source',self.source['id']),)
  old=Receipt.make('usage',None,{**u.payload,'final':False},entity_ref=u.entity_ref,occurred_at=u.occurred_at,observed_at=OBS,producer=u.producer,evidence=source_proof)
  final=Receipt.make('usage',None,u.payload,entity_ref=u.entity_ref,supersedes=old.fact_ref,occurred_at=u.occurred_at,observed_at=OBS,producer=u.producer,evidence=source_proof)
  ledger=UsageLedger(self.root/'ledger');ledger.ingest_receipts([old,final,*[r for r in plain if r.kind!='usage']])
  frozen=ledger.receipt_snapshot();before=self.query(ledger,frozen['watermark'])
  registry={'schema':'computecost_source_registry/v1','ledger_root':str(ledger.root),'required_receipt_features':['verification_workspace.v1'],'sources':[self.source]}
  collect_registered_sources(registry,ledger);after=self.query(ledger)
  self.assertEqual({g['key']['agent'] for g in after['groups']},{'outcome-verifier'});self.assertEqual({g['key']['wi'] for g in after['groups']},{WI})
  for key in ('event_count','tokens','known_total_tokens','conflicted_event_count'):
   self.assertEqual(after['summary']['usage'][key],before['summary']['usage'][key])
  self.assertEqual(self.query(ledger,frozen['watermark']),before)
  self.assertEqual([r.to_dict() for r in ledger.receipt_snapshot()['facts'] if r.kind=='usage'],[r.to_dict() for r in frozen['facts'] if r.kind=='usage'])
  self.assertEqual(collect_registered_sources(registry,ledger)['inserted'],0)
  later=Receipt.make('usage',None,{**u.payload,'interval_end':EXPIRED},entity_ref=u.entity_ref,supersedes=final.fact_ref,occurred_at=u.occurred_at,observed_at=OBS,producer=u.producer)
  ledger.ingest_receipts([later]);late=self.query(ledger)
  self.assertEqual({g['key']['agent'] for g in late['groups']},{'unassigned'});self.assertEqual({g['key']['wi'] for g in late['groups']},{'unassigned'})
 def test_independent_actor_and_task_claims_remain_conflicts(self):
  self.native_stream();rows=self.read();u=next(r for r in rows if r.kind=='usage')
  contra_actor=actor_receipt(u,node='ownermac',executor='other-agent',observed=OBS,evidence=(reference('fixture','other-actor'),))
  contra_task=Receipt.make('attribution','other-task',{'target_ref':u.entity_ref,'wi':'wi_20261005_904','executor':None,'responsible':None,'delegator':None,'state':'assigned','policy':'independent_fixture.v1','effective_at':START},occurred_at=START,observed_at=OBS,producer='independent_fixture.v1')
  ledger=UsageLedger(self.root/'ledger');ledger.ingest_receipts([*rows,contra_actor,contra_task]);report=self.query(ledger)
  self.assertEqual({g['key']['agent'] for g in report['groups']},{'ambiguous'});self.assertEqual({g['key']['wi'] for g in report['groups']},{'ambiguous'})

if __name__=='__main__':unittest.main()
