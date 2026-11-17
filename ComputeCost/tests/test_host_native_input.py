"""End-to-end fixtures start at authentic Host input + physical Pi records."""
from copy import deepcopy
from datetime import datetime
from pathlib import Path
import hashlib,json,tempfile,unittest
from pulse_compute_cost.receipts import canonical,reference,current_receipts
from pulse_compute_cost.receipt_sources import collect_registered_sources,HOST_SCHEMA
from pulse_compute_cost.ledger import UsageLedger
from pulse_compute_cost.pi_context import tenant_directory
from pulse_compute_cost.host_native_input import authenticated_input_links
from pulse_compute_cost.collaboration_query import CollaborationQuery,query_collaboration
from test_collaboration_receipts import CATALOG

class HostNativeInputTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name);self.inputs=self.root/'inputs';self.inputs.mkdir(mode=0o700)
        self.pool='fixture:pulse';folder=self.root/tenant_directory(self.pool);folder.mkdir()
        self.native=folder/'fixture.jsonl';self.host=self.root/'host.jsonl'
        self.ledger=UsageLedger(self.root/'ledger');self.wi='wi_20260913_101'
        self.start='2026-09-13T01:00:01Z';self.end='2026-09-13T01:00:04Z'
        self.ex=reference('execution','host');self.assignment=reference('assignment','one')
        values=['feishu','input-one','fixture-human',self.pool]
        token=hashlib.sha256(json.dumps(values,separators=(',',':')).encode()).hexdigest()
        self.token=token
        original={'source':dict(zip(('channel','eventId','humanRef'),values[:3]),message='continue'),
                  'poolKey':self.pool,'observedAtMs':int(datetime.fromisoformat('2026-09-13T01:00:00+00:00').timestamp()*1000)}
        p=self.inputs/(token+'.json');p.write_bytes(canonical(original));p.chmod(0o600)
        common={'wi':self.wi,'executor':'pulse','responsible':None,'delegator':None}
        self.events=[{'schema':HOST_SCHEMA,'kind':'execution','event_ref':reference('event','begin'),
                      'attempt_ref':self.ex,'at':self.start,'runtime':'pi','state':'accepted','expected_usage':True,
                      'assignment_ref':self.assignment,**common},
          {'schema':HOST_SCHEMA,'kind':'attribution','event_ref':reference('host_event',['human_assignment',self.ex,reference('human',token)]),
           'at':self.start,'target_ref':reference('human',token),
           'attribution_ref':reference('attribution',['human_assignment',reference('human',token),self.assignment]),
           'policy':'authenticated_dispatch_assignment.v1','state':'assigned','effective_at':'2026-09-13T01:00:00Z',**common},
          {'schema':HOST_SCHEMA,'kind':'execution','event_ref':reference('event','end'),
           'attempt_ref':self.ex,'at':self.end,'started_at':self.start,'runtime':'pi','state':'failed',
           'expected_usage':True,'assignment_ref':self.assignment,**common}]
        self.records=[{'type':'session','id':'session-one','timestamp':self.start},
          {'type':'message','id':'input','parentId':'session-one','timestamp':'2026-09-13T01:00:01.010Z',
           'message':{'role':'user','content':'continue\n\n[Host human input reference: '+token+'. provenance only.]'}}]
        for n in (2,3):
            self.records.append({'type':'message','id':'answer-'+str(n),'parentId':'input' if n==2 else 'answer-2',
              'timestamp':'2026-09-13T01:00:0'+str(n)+'Z','message':{'role':'assistant','model':'openai/test','provider':'fixture',
              'stopReason':'toolUse' if n==2 else 'stop','usage':{'input':3,'output':2,'cacheRead':7,'cacheWrite':0,'totalTokens':12}}})
        self.registry={'schema':'computecost_source_registry/v1','ledger_root':str(self.ledger.root),
          'required_receipt_features':['actor_context.v1','host_native_input.v1'],'sources':[
            {'id':'host','kind':'relay_host','path':str(self.host),'node':'vultr','actor_attribution':True,'human_input_directory':str(self.inputs),'host_native_input':True},
            {'id':'pi','kind':'pi','path':str(folder),'node':'vultr','actor_attribution':True,'human_input_directory':str(self.inputs),'host_native_input':True}]}
    def collect(self):
        self.host.write_bytes(b'\n'.join(canonical(x) for x in self.events)+b'\n')
        self.native.write_bytes(b'\n'.join(canonical(x) for x in self.records)+b'\n')
        return collect_registered_sources(self.registry,self.ledger)
    def report(self):
        return query_collaboration(self.ledger,CollaborationQuery(group_by=('wi',)),catalog=CATALOG)
    def test_failed_host_uses_real_native_requests_without_new_usage(self):
        r=self.collect();self.assertEqual(r['sources'][0]['status'],'complete')
        rows,_=current_receipts(self.ledger.receipt_snapshot()['facts'])
        self.assertEqual(len([x for x in rows if x.kind=='usage']),2)
        links,_=authenticated_input_links(rows);self.assertEqual(len(links),2)
        self.assertEqual({next(iter(x)) for x in links.values()},{self.ex})
        self.assertEqual(self.report()['groups'][0]['key']['wi'],self.wi)
    def test_proven_input_join_does_not_retain_decoded_payloads_for_the_whole_ledger(self):
        from unittest.mock import patch
        from pulse_compute_cost.receipts import Receipt
        self.collect();facts=self.ledger.receipt_snapshot()['facts']
        expected=authenticated_input_links(facts)
        base=next(r for r in facts if r.kind=='attribution' and r.payload.get('scope')=='actor')
        unrelated=[Receipt.make('attribution',['unrelated',i],
            {**base.payload,'target_ref':reference('unrelated-target',i)},
            occurred_at=base.occurred_at,observed_at=base.observed_at,producer=base.producer,
            evidence=base.evidence) for i in range(1000)]
        live=peak=0
        class Decoded(dict):
            def __init__(self,value):
                nonlocal live,peak
                super().__init__(value);live+=1;peak=max(peak,live)
            def __del__(self):
                nonlocal live
                live-=1
        original=Receipt.payload.fget
        with patch.object(Receipt,'payload',property(lambda row:Decoded(original(row)))):
            self.assertEqual(authenticated_input_links(facts+unrelated),expected)
        self.assertLess(peak,32)

    def test_pending_is_not_finished_by_partial_native_output(self):
        self.events.pop();r=self.collect()
        self.assertEqual((r['sources'][0]['status'],r['sources'][0]['reason']),('partial','host_usage_pending'))
    def test_wrong_body_or_node_or_missing_input_never_clears_alarm(self):
        for mode in ('body','node','absent'):
            with self.subTest(mode=mode):
                self.setUp()
                if mode=='body':self.records[1]['message']['content']='forged '+self.records[1]['message']['content']
                if mode=='node':self.registry['sources'][1]['node']='tcloud'
                if mode=='absent':(self.inputs/(self.token+'.json')).unlink()
                self.assertEqual(self.collect()['sources'][0]['status'],'partial')
    def placeholder(self):
        self.events[-1]['state']='completed'
        self.events.insert(-1,{'schema':HOST_SCHEMA,'kind':'usage','event_ref':reference('event','empty'),
            'usage_ref':reference('usage','empty'),'attempt_ref':self.ex,'at':self.end,'started_at':self.start,
            'runtime':'pi','mapping':'relay_pi.v1','granularity':'turn_aggregate','final':True,
            'native_scope_ref':reference('native_session',['pi','session-one']),
            'wi':self.wi,'executor':'pulse','responsible':None,'delegator':None,'usage':{}})
    def test_empty_summary_replaced_only_by_complete_original_turn(self):
        self.placeholder();self.collect();r=self.report()
        self.assertIn(reference('usage','empty'),r['reconciliation']['suppressed_aggregate_refs'])
        self.assertEqual(r['summary']['usage']['event_count'],2)
        self.assertEqual(r['summary']['usage']['tokens']['uncached_input']['known'],6)
    def test_no_final_native_response_keeps_unknown_placeholder(self):
        self.placeholder();self.records[-1]['message']['stopReason']='error';self.collect();r=self.report()
        self.assertNotIn(reference('usage','empty'),r['reconciliation']['suppressed_aggregate_refs'])
    def later_turn(self):
        # The next turn's request, appended after the Host interval ends.
        self.records.append({'type':'message','id':'answer-later','parentId':self.records[-1]['id'],
            'timestamp':'2026-09-13T01:00:10Z','message':{'role':'assistant','model':'openai/test','provider':'fixture',
            'stopReason':'stop','usage':{'input':1,'output':1,'cacheRead':0,'cacheWrite':0,'totalTokens':2}}})
    def assert_placeholder_unknown(self,r):
        self.assertNotIn(reference('usage','empty'),r['reconciliation']['suppressed_aggregate_refs'])
        self.assertEqual([u['reason'] for u in r['summary']['cost']['unpriced']],['unknown_token_quantity'])
        self.assertTrue(all(v['unknown_event_count']==1 for v in r['summary']['usage']['tokens'].values()))
    def test_errored_turn_followed_by_a_later_request_is_backfilled_by_native_history(self):
        self.placeholder();self.records[-1]['message']['stopReason']='error';self.later_turn();self.collect();r=self.report()
        self.assertIn(reference('usage','empty'),r['reconciliation']['suppressed_aggregate_refs'])
        self.assertEqual(r['summary']['cost']['unpriced'],[])
        self.assertEqual(r['summary']['usage']['tokens']['uncached_input'],{'known':7,'unknown_event_count':0})
    def test_turn_read_only_up_to_a_tool_use_keeps_unknown_placeholder(self):
        # The final answer has not reached the native file (or this copy) yet.
        self.placeholder();self.records.pop();self.collect();r=self.report()
        self.assert_placeholder_unknown(r)
    def test_original_watermark_and_usage_survive_recovery(self):
        for s in self.registry['sources']:s['host_native_input']=False
        self.collect();old=self.ledger.receipt_snapshot()
        usages={r.entity_ref:r.payload for r in old['facts'] if r.kind=='usage'}
        for s in self.registry['sources']:s['host_native_input']=True
        self.collect();new=self.ledger.receipt_snapshot()
        self.assertEqual(usages,{r.entity_ref:r.payload for r in new['facts'] if r.kind=='usage'})
        self.assertEqual(old,self.ledger.receipt_snapshot(watermark=old['watermark']))

    def test_unchanged_collection_is_idempotent_including_host_head(self):
        self.collect();before=self.ledger.receipt_snapshot()
        self.collect();after=self.ledger.receipt_snapshot()
        self.assertEqual(before,after)
    def test_lost_input_proof_cannot_reuse_a_previous_green_source(self):
        self.collect();old=self.ledger.receipt_snapshot()
        (self.inputs/(self.token+'.json')).unlink()
        result=self.collect();self.assertEqual(result['sources'][0]['status'],'partial')
        self.assertEqual(old,self.ledger.receipt_snapshot(watermark=old['watermark']))
    def test_reused_human_input_in_another_attempt_is_ambiguous(self):
        other=reference('execution','other')
        extra=deepcopy(self.events)
        for row in extra:
            if row['kind']=='execution':
                row['attempt_ref']=other;row['event_ref']=reference('other',row['event_ref'])
            else:
                row['event_ref']=reference('host_event',['human_assignment',other,row['target_ref']])
        self.events.extend(extra)
        result=self.collect();self.assertEqual(result['sources'][0]['status'],'partial')
        self.assertFalse(authenticated_input_links(self.ledger.receipt_snapshot()['facts'])[0])
    def test_same_input_copied_into_another_native_session_is_not_a_winner(self):
        copied=deepcopy(self.records);copied[0]['id']='other-session';copied[1]['parentId']='other-session'
        self.native.with_name('other.jsonl').write_bytes(b'\n'.join(canonical(x) for x in copied)+b'\n')
        self.assertEqual(self.collect()['sources'][0]['status'],'partial')
    def test_contradictory_known_host_tokens_are_not_recovered(self):
        self.placeholder();self.events[-2]['usage']={'input':100,'output':2,'cacheRead':7,'cacheWrite':0}
        self.collect();r=self.report()
        self.assertNotIn(reference('usage','empty'),r['reconciliation']['suppressed_aggregate_refs'])
    def test_a_real_capture_gap_remains_partial(self):
        self.events.append({'schema':HOST_SCHEMA,'kind':'gap','event_ref':reference('event','gap'),
                            'at':self.end,'reason':'capture_failed'})
        self.assertEqual(self.collect()['sources'][0]['status'],'partial')
    def test_native_final_flag_is_not_a_default_model_or_token_substitute(self):
        for key in ('model','usage'):
            with self.subTest(key=key):
                self.setUp();self.placeholder();self.records[-1]['message'].pop(key)
                self.collect();r=self.report()
                self.assertNotIn(reference('usage','empty'),r['reconciliation']['suppressed_aggregate_refs'])

    def test_conflicting_actor_or_channel_cannot_recover_a_dispatch(self):
        from pulse_compute_cost.receipt_sources import actor_receipt
        self.collect();facts=self.ledger.receipt_snapshot()['facts']
        native=next(r for r in facts if r.kind=='usage')
        for extra in (actor_receipt(native,node='vultr',executor='other',observed=self.end),
                      actor_receipt(native,node='vultr',entrypoint='telegram',observed=self.end)):
            with self.subTest(extra=extra.payload):
                links,_=authenticated_input_links(facts+[extra])
                self.assertNotIn(native.entity_ref,links)
    def test_partial_counter_is_not_a_wholly_empty_summary(self):
        self.placeholder();self.events[-2]['usage']={'input':6};self.collect()
        self.assertNotIn(reference('usage','empty'),self.report()['reconciliation']['suppressed_aggregate_refs'])
    def test_incomplete_physical_native_prefix_does_not_clear_source(self):
        self.collect();self.native.write_bytes(self.native.read_bytes()+b'{')
        result=collect_registered_sources(self.registry,self.ledger)
        self.assertEqual(result['sources'][0]['status'],'partial')
    def test_old_context_format_remains_valid_and_feature_is_reader_gated(self):
        from pulse_compute_cost.receipt_sources import actor_receipt
        from pulse_compute_cost.receipts import ReceiptError,Receipt
        self.collect();native=next(r for r in self.ledger.receipt_snapshot()['facts'] if r.kind=='usage')
        old=actor_receipt(native,node='vultr',observed=self.end)
        self.assertEqual(set(old.payload['context']),{'node','entrypoint','workspace_ref'})
        for bad in ({'turn_final':True},{'input_ref':'raw-input-id'},{'input_ref':reference('input','one'),'turn_final':'yes'}):
            with self.subTest(bad=bad):
                p=old.payload;p['context'].update(bad)
                with self.assertRaises(ReceiptError):
                    Receipt.make('attribution','bad',p,occurred_at=self.start,observed_at=self.end,producer='test')



    def durable_recovery(self):
        from pulse_compute_cost.pi_context import _RECOVERY_PREFIX
        renamed=self.root/'human-inputs';self.inputs.rename(renamed);self.inputs=renamed
        for source in self.registry['sources']:source['human_input_directory']=str(renamed)
        request='operation_v1_feishu_recovery_'+hashlib.sha256(json.dumps(
            [self.pool,self.wi,self.token],separators=(',',':')).encode()).hexdigest()
        context={'machine':{'work_item_id':self.wi,'status':'degraded'}}
        record={'schema':'pulse_feishu_host_recovery/v1','requestId':request,'poolKey':self.pool,
            'workItemId':self.wi,'humanInputRef':self.token,'sessionId':'session-one',
            'state':'dispatched','dispatching':True,'result':{'lifecycleContext':context}}
        directory=self.root/'feishu-recoveries';directory.mkdir(mode=0o700)
        journal=directory/(request+'.json');journal.write_bytes(canonical(record));journal.chmod(0o600)
        self.events=[r for r in self.events if r['kind']!='attribution']
        for row in self.events:
            if row['kind']=='execution':row['dispatch_ref']=reference('dispatch',['pi','turn:entry:'+request,'session-one'])
        self.records[1]['message']['content']=_RECOVERY_PREFIX+'\nhuman_input_ref='+self.token+'\n'+json.dumps(context)+'\nSCL_HOST_RECOVERY_REQUEST='+request
        return journal,record

    def test_durable_recovery_dispatch_restores_existing_context_and_empty_summary(self):
        self.placeholder();self.durable_recovery();self.collect()
        report=query_collaboration(self.ledger,CollaborationQuery(group_by=('agent','wi','entrypoint')),catalog=CATALOG)
        groups=[g for g in report['groups'] if g['usage']['event_count']]
        self.assertEqual(sum(g['usage']['event_count'] for g in groups),2)
        self.assertEqual({g['key']['entrypoint'] for g in groups},{'feishu'})
        self.assertEqual({g['key']['wi'] for g in groups},{self.wi})
        self.assertEqual({g['key']['agent'] for g in groups},{'pulse'})
        self.assertIn(reference('usage','empty'),report['reconciliation']['suppressed_aggregate_refs'])
        before=self.ledger.receipt_snapshot();self.collect();self.assertEqual(before,self.ledger.receipt_snapshot())

    def test_durable_recovery_never_uses_a_different_or_missing_dispatch_journal(self):
        for kind in ('absent','pending','wrong_session','wrong_dispatch','duplicate_dispatch','changed_input'):
            with self.subTest(kind=kind):
                self.setUp();self.placeholder();journal,record=self.durable_recovery()
                if kind=='absent':journal.unlink()
                elif kind=='pending':journal.write_bytes(canonical({**record,'state':'pending'}))
                elif kind=='wrong_session':journal.write_bytes(canonical({**record,'sessionId':'other'}))
                elif kind=='wrong_dispatch':
                    for row in self.events:
                        if row['kind']=='execution':row['dispatch_ref']=reference('dispatch','wrong')
                elif kind=='duplicate_dispatch':
                    row=deepcopy(self.events[0]);row['attempt_ref']=reference('execution','another');row['event_ref']=reference('event','another');self.events.append(row)
                elif kind=='changed_input':
                    p=self.inputs/(self.token+'.json');value=json.loads(p.read_bytes());value['poolKey']='another';p.write_bytes(canonical(value))
                self.collect();r=self.report()
                self.assertNotIn(reference('usage','empty'),r['reconciliation']['suppressed_aggregate_refs'])


class UngrantedInputTests(unittest.TestCase):
    setUp=HostNativeInputTests.setUp
    collect=HostNativeInputTests.collect
    report=HostNativeInputTests.report
    def direct(self,wi=None):
        self.events=[r for r in self.events if r['kind']!='attribution']
        for row in self.events:
            row['wi']=wi;row['assignment_ref']=None
        human=reference('human',self.token)
        self.events.insert(1,{**self.events[0],
            'event_ref':reference('host_event',[self.ex,'authenticated_input',human]),
            'started_at':self.start,'human_ref':human,'human_input_at':'2026-09-13T01:00:00Z'})
    def test_direct_route_identifies_agent_without_inventing_wi(self):
        self.direct();self.collect()
        facts=self.ledger.receipt_snapshot()['facts']
        links,_=authenticated_input_links(facts);self.assertEqual(len(links),2)
        r=query_collaboration(self.ledger,CollaborationQuery(group_by=('agent','wi','entrypoint')),catalog=CATALOG)
        self.assertEqual(r['summary']['usage']['event_count'],2)
        self.assertEqual({g['key']['agent'] for g in r['groups']},{'pulse'})
        self.assertEqual({g['key']['entrypoint'] for g in r['groups']},{'feishu'})
        self.assertFalse(any(g['key']['wi'] in {self.wi,'none'} for g in r['groups']))
        self.assertFalse(any(x.kind=='execution' and x.payload['assignment_ref'] for x in facts))
    def test_recorded_purpose_survives_without_an_execution_grant(self):
        self.direct(self.wi);self.collect()
        self.assertEqual(self.report()['groups'][0]['key']['wi'],self.wi)
    def test_changed_input_edge_cannot_attribute_native(self):
        for key,value in [('event_ref',reference('event','forged')),('human_input_at',self.end),
                          ('started_at',self.end),('at',self.end),('executor','other'),
                          ('assignment_ref',self.assignment),('runtime','codex')]:
            with self.subTest(key=key):
                self.setUp();self.direct();self.events[1][key]=value
                self.collect()
                self.assertFalse(authenticated_input_links(self.ledger.receipt_snapshot()['facts'])[0])
    def test_original_no_input_execution_still_parses_without_new_feature(self):
        from pulse_compute_cost.receipt_sources import read_host
        self.direct();self.collect()
        data=self.host.read_bytes();with_edge=read_host(self.host,self.end,data=data)
        without_edge=read_host(self.host,self.end,data=b'\n'.join(canonical(r) for r in self.events if not r.get('human_ref'))+b'\n')
        self.assertEqual([r.to_dict() for r in with_edge],[r.to_dict() for r in without_edge])

if __name__=='__main__':unittest.main()


class InputPrincipalTests(unittest.TestCase):
    def setUp(self):
        HostNativeInputTests.setUp(self)
        self.events = []
        self.registry['sources'][0]['principal_alias_history'] = [
            {'alias':'pulse', 'principal':'hestia', 'effective_from':'2026-09-12T00:00:00Z', 'config_sha256':'a'*64}]
        self.records[0]['cwd'] = '/old/machine/hestia-space'
    collect = HostNativeInputTests.collect
    def actor_report(self):
        return query_collaboration(self.ledger,CollaborationQuery(group_by=('agent','wi','entrypoint')),
            catalog=CATALOG)
    def test_actor_is_known_before_host_usage_and_without_wi(self):
        self.collect();group=self.actor_report()['groups'][0]
        self.assertEqual(group['key'], {'agent':'hestia','wi':'unassigned','entrypoint':'feishu'})
        self.assertEqual(group['usage']['event_count'],2)
        self.assertEqual(group['usage']['known_total_tokens'],24)
    def test_missing_future_or_other_node_mapping_does_not_guess_actor(self):
        from copy import deepcopy
        baseline=deepcopy(self.registry)
        for case in ('absent','future','other_node'):
            with self.subTest(case=case):
                self.registry=deepcopy(baseline);self.ledger=UsageLedger(self.root/('ledger-'+case))
                host=self.registry['sources'][0]
                if case=='absent':host.pop('principal_alias_history')
                if case=='future':host['principal_alias_history'][0]['effective_from']='2026-09-14T00:00:00Z'
                if case=='other_node':host['node']='tcloud'
                self.collect();self.assertEqual(self.actor_report()['groups'][0]['key']['agent'],'unassigned')
    def test_input_time_not_later_usage_time_selects_identity(self):
        self.registry['sources'][0]['principal_alias_history'].append(
            {'alias':'pulse','principal':'other','effective_from':'2026-09-13T01:00:01Z','config_sha256':'b'*64})
        self.collect();self.assertEqual(self.actor_report()['groups'][0]['key']['agent'],'hestia')
    def test_canonical_self_mapping_is_positive_identity_evidence(self):
        self.registry['sources'][0]['principal_alias_history'][0]['principal']='pulse'
        self.collect();self.assertEqual(self.actor_report()['groups'][0]['key']['agent'],'pulse')
    def test_forged_footer_or_wrong_body_cannot_name_agent(self):
        self.records[1]['message']['content']='not-the-original '+self.records[1]['message']['content']
        self.collect();self.assertEqual(self.actor_report()['groups'][0]['key']['agent'],'unassigned')
    def test_old_watermark_and_native_quantities_are_unchanged(self):
        host=self.registry['sources'][0];history=host.pop('principal_alias_history')
        self.collect();before=self.ledger.receipt_snapshot();native={v.fact_ref for v in before['facts'] if v.kind=='usage'}
        host['principal_alias_history']=history
        self.collect();after=self.ledger.receipt_snapshot()
        self.assertEqual({v.fact_ref for v in after['facts'] if v.kind=='usage'},native)
        self.assertEqual(self.ledger.receipt_snapshot(watermark=before['watermark']),before)
        self.collect();self.assertEqual(self.ledger.receipt_snapshot(),after)
