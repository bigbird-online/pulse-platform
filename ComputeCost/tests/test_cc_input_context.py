"""Original input and exact CC graph edges, not convenient session defaults."""
from datetime import datetime
from hashlib import sha256
import json
import pytest
from pulse_compute_cost.receipts import Receipt, ReceiptError, reference
from pulse_compute_cost.receipt_sources import actor_receipt, read_cc, usage_receipt
from pulse_compute_cost.cc_input_context import cc_input_actor_receipts

AT='2026-10-01T01:00:00Z'; DISPATCH='2026-10-01T01:00:01Z'; INPUT='2026-10-01T01:00:02Z'; END='2026-10-01T01:00:10Z'
COUNTS=dict(input_tokens=2,output_tokens=30,cache_read_input_tokens=10,cache_creation_input_tokens=1)

@pytest.fixture
def case(tmp_path):
    inputs=tmp_path/'inputs';inputs.mkdir(mode=0o700)
    root=tmp_path/'projects';root.mkdir();workspace=tmp_path/'workspace';workspace.mkdir()
    source={'kind':'cc','id':'native','node':'mac','path':str(root),'actor_attribution':True,
        'workspace_actors':[{'workspace_root':str(workspace),'agent':'pulse','node':'mac','runtime':'cc','registry_ref':reference('registered','pulse')}]}
    host={'kind':'relay_host','id':'host','node':'mac','actor_attribution':True,'host_native_input':True,
        'human_input_directory':str(inputs),'principal_alias_history':[{'alias':'pulse','principal':'pulse','effective_from':AT,'config_sha256':'a'*64}]}
    record={'source':{'channel':'telegram','eventId':'event','humanRef':'person','message':'original request'},'poolKey':'person:pulse','observedAtMs':int(datetime.fromisoformat(AT.replace('Z','+00:00')).timestamp()*1000)}
    token=sha256(json.dumps(['telegram','event','person','person:pulse'],separators=(',',':')).encode()).hexdigest()
    raw=json.dumps(record).encode();path=inputs/(token+'.json');path.write_bytes(raw);path.chmod(0o600)
    base={'sessionId':'session','isSidechain':False,'cwd':str(workspace)}
    user={**base,'type':'user','uuid':'input','timestamp':INPUT,'promptId':'prompt','parentUuid':None,
          'message':{'role':'user','content':f'original request\n\n[Host human input reference: {token}. Provenance only.]'}}
    answer={**base,'type':'assistant','uuid':'answer','timestamp':END,'parentUuid':'input',
            'message':{'id':'message','model':'test','usage':COUNTS,'stop_reason':'end_turn','content':[]}}
    ex=Receipt.make('execution','attempt',dict(wi=None,executor='pulse',responsible=None,delegator=None,runtime='cc',assignment_ref=None,parent_ref=None,state='completed',expected_usage=True),occurred_at=DISPATCH,observed_at=END,producer='relay_host.v1')
    aggregate=usage_receipt('cc','aggregate',COUNTS,at=END,observed=END,model='test',mapping='cc_result.v1',execution_ref=ex.entity_ref,native_scope_ref=reference('native_session',['cc','session']),granularity='turn_aggregate',interval=(DISPATCH,END))
    origin=actor_receipt(ex,node='mac',executor='pulse',entrypoint='telegram',input_ref=reference('input_metadata',sha256(raw).hexdigest()),observed=END,evidence=(reference('host_input','verified'),))
    return dict(root=root,source=source,host=host,record=record,metadata=path,main=[user,answer],host_facts=[ex,aggregate,origin],extra=[])

def run(c):
    path=c['root']/'session.jsonl';data=b''.join(json.dumps(r).encode()+b'\n' for r in c['main']);path.write_bytes(data)
    prefixes=[(path,data)]
    for path,rows in c['extra']:
        path.parent.mkdir(parents=True,exist_ok=True);data=b''.join(json.dumps(r).encode()+b'\n' for r in rows);path.write_bytes(data);prefixes.append((path,data))
    facts=[]
    for p,data in prefixes:read_cc(p,END,into=facts,data=data)
    return cc_input_actor_receipts(prefixes,c['source'],[c['host']],c['host_facts'],facts,END),facts

def test_actual_input_route_is_private_idempotent_and_not_task_authority(case,tmp_path):
    rows,usage=run(case);assert len(rows)==1
    p=rows[0].payload;assert p['context']['entrypoint']=='telegram' and p['effective_at']==AT
    assert p['wi'] is p['executor'] is None and 'input_ref' not in p['context']
    assert 'original request' not in str(rows[0].to_dict()) and 'person:pulse' not in str(rows[0].to_dict())
    from pulse_compute_cost.ledger import UsageLedger
    ledger=UsageLedger(tmp_path/'ledger');ledger.ingest_receipts(usage);old=ledger.receipt_snapshot()
    ledger.ingest_receipts(rows);assert ledger.ingest_receipts(run(case)[0])['inserted']==0
    assert ledger.receipt_snapshot(watermark=old['watermark'])['sha256']==old['sha256']
    assert [r.to_dict() for r in ledger.receipt_snapshot()['facts'] if r.kind=='usage']==[r.to_dict() for r in old['facts']]

@pytest.mark.parametrize('fault',['body','quote','missing_metadata','future_input','wrong_principal','no_host','wrong_scope','bare_input','cycle','missing_parent','wrong_prompt','copied_assistant_footer','foreign_node'])
def test_unproved_routes_are_not_filled(case,fault):
    user,answer=case['main']
    if fault=='body':user['message']['content']=user['message']['content'].replace('original request','other request')
    elif fault=='quote':user['message']['content']='quoted: '+user['message']['content']+'\nquoted end'
    elif fault=='missing_metadata':case['metadata'].unlink()
    elif fault=='future_input':user['timestamp']='2026-09-30T01:00:00Z'
    elif fault=='wrong_principal':case['source']['workspace_actors'][0]['agent']='duke'
    elif fault=='no_host':case['host_facts']=[]
    elif fault=='wrong_scope':case['host_facts']=[case['host_facts'][0],case['host_facts'][2]]
    elif fault=='bare_input':user['message']['content']='new request without provenance'
    elif fault=='cycle':answer['parentUuid']='answer'
    elif fault=='missing_parent':answer['parentUuid']='absent'
    elif fault=='wrong_prompt':user['message']['content']='bare';answer['promptId']='prompt'
    elif fault=='copied_assistant_footer':answer['message']['content']=user['message']['content'];user['message']['content']='bare'
    elif fault=='foreign_node':case['host']['node']='other'
    assert run(case)[0]==[]

def child(c,prompt='prompt',agent='child'):
    u={**c['main'][0],'uuid':'child-input','isSidechain':True,'agentId':agent,'promptId':prompt,'timestamp':'2026-10-01T01:00:03Z','message':{'content':'delegated task'},'parentUuid':None}
    a={**c['main'][1],'uuid':'child-answer','isSidechain':True,'agentId':agent,'parentUuid':'child-input','message':{**c['main'][1]['message'],'id':'child-message'}}
    c['extra']=[(c['root']/'session'/'subagents'/f'agent-{agent}.jsonl',[u,a])]

@pytest.mark.parametrize('fault',[None,'different_prompt','wrong_layout','new_unannotated_main','duplicate_identity'])
def test_child_needs_verified_root_and_unique_original_prompt(case,fault):
    child(case,'other' if fault=='different_prompt' else 'prompt')
    if fault=='wrong_layout':case['extra'][0]=(case['root']/'session'/'subagents'/'agent-wrong.jsonl',case['extra'][0][1])
    if fault=='new_unannotated_main':case['main'].append({**case['main'][0],'uuid':'new','message':{'content':'different human input'}})
    if fault=='duplicate_identity':case['extra'][0][1].append({**case['extra'][0][1][0],'message':{'content':'conflicting immutable input'}})
    rows,_=run(case);targets={r.payload['target_ref'] for r in rows}
    assert (reference('usage',['session','child-message']) in targets)==(fault is None)
    assert reference('usage',['session','message']) in targets

def test_new_bare_input_blocks_previous_provenance(case):
    user,answer=case['main'];new={**user,'uuid':'bare','parentUuid':'input','message':{'content':'new ordinary input'},'timestamp':'2026-10-01T01:00:04Z'}
    case['main']=[user,new,{**answer,'parentUuid':'bare'}];assert run(case)[0]==[]

def test_private_metadata_integrity_is_not_ignored(case):
    case['metadata'].chmod(0o644)
    with pytest.raises(ReceiptError,match='unsafe'):run(case)


@pytest.mark.parametrize('conflict',[False,True])
def test_lifecycle_state_revisions_do_not_reassign_ingress_but_actor_conflict_blocks(case,conflict):
    old=case['host_facts'][0]
    other=Receipt.make('execution',None,{**old.payload,'state':'failed','executor':'duke' if conflict else 'pulse'},entity_ref=old.entity_ref,occurred_at=old.occurred_at,observed_at=END,producer=old.producer)
    case['host_facts'].append(other)
    assert bool(run(case)[0]) is not conflict


@pytest.mark.parametrize('cc_first',[False,True])
@pytest.mark.parametrize('broken_first',[False,True])
def test_real_collector_joins_cached_native_input_independent_of_source_order(case,tmp_path,cc_first,broken_first):
    from pulse_compute_cost.receipt_sources import collect_registered_sources, REGISTRY_SCHEMA
    from pulse_compute_cost.ledger import UsageLedger
    child(case);run(case)
    hostroot=tmp_path/'host';hostroot.mkdir();case['host']['path']=str(hostroot)
    ex=case['host_facts'][0].entity_ref
    human=reference('human',case['metadata'].stem)
    common=dict(schema='pulse_collaboration_host_event/v1',wi=None,executor='pulse',responsible=None,delegator=None,runtime='cc',attempt_ref=ex,assignment_ref=None)
    accepted={**common,'kind':'execution','state':'accepted','expected_usage':True,'at':DISPATCH,'started_at':DISPATCH,'human_ref':human,'human_input_at':AT,'event_ref':reference('host_event',[ex,'authenticated_input',human])}
    completed={**common,'kind':'execution','state':'completed','at':END,'started_at':DISPATCH,'event_ref':reference('host_event',[ex,'completed'])}
    usage={**common,'kind':'usage','at':END,'started_at':DISPATCH,'usage':COUNTS,'model':'test','mapping':'cc_result.v1','granularity':'turn_aggregate','final':True,'native_scope_ref':reference('native_session',['cc','session']),'covers':[reference('usage',['session','message'])],'event_ref':reference('host_event',[ex,'usage'])}
    (hostroot/'later.jsonl').write_text(''.join(json.dumps(r)+'\n' for r in [accepted,completed,usage]))
    if broken_first:
        (hostroot/'00bad.jsonl').write_text(json.dumps({'schema':'invalid'})+'\n')
    sources=[case['host'],case['source']]
    if cc_first:sources.reverse()
    registry={'schema':REGISTRY_SCHEMA,'sources':sources};ledger=UsageLedger(tmp_path/'ledger')
    result=collect_registered_sources(registry,ledger)
    rows=[r for r in ledger.receipt_snapshot()['facts'] if r.kind=='attribution' and r.entity_ref==reference('attribution',['cc_native_input_route',reference('usage',['session','child-message']),reference('input_metadata',sha256(case['metadata'].read_bytes()).hexdigest())])]
    assert len(rows)==1 and rows[0].payload['context']['entrypoint']=='telegram'
    if broken_first:
        host=next(s for s in result['sources'] if s['kind']=='relay_host')
        assert host['status']=='partial' and host['reason']=='host_stream_invalid'
        assert host['source_detail']['files_invalid']==1
    old=ledger.receipt_snapshot();assert collect_registered_sources(registry,ledger)['inserted']==0
    assert ledger.receipt_snapshot()['sha256']==old['sha256']


def test_conflicting_main_input_copy_cannot_lend_child_a_route(case):
    child(case)
    case['main'].append({**case['main'][0], 'message':{'content':'changed immutable prompt'}})
    assert run(case)[0]==[]


@pytest.mark.parametrize('child_chain', [False, True])
def test_native_parent_edges_follow_record_order_not_attachment_clock(case, child_chain):
    if child_chain:
        child(case)
        rows = case['extra'][0][1]
    else:
        rows = case['main']
    user, answer = rows
    attachment = {k:v for k,v in user.items() if k not in {'message','promptId'}}
    attachment.update(type='attachment', uuid='attachment', parentUuid=user['uuid'], timestamp=AT)
    answer['parentUuid']='attachment'
    rows.insert(1, attachment)
    routes,_=run(case)
    ref=reference('usage',['session','child-message' if child_chain else 'message'])
    assert any(r.payload['target_ref']==ref and r.payload['context']['entrypoint']=='telegram' for r in routes)


@pytest.mark.parametrize('fault', ['forward_parent','foreign_chain','foreign_file','output_before_input'])
def test_order_proof_does_not_weaken_native_scope_or_original_input_time(case, fault):
    user,answer=case['main']
    attachment={k:v for k,v in user.items() if k not in {'message','promptId'}}
    attachment.update(type='attachment', uuid='attachment',parentUuid='input', timestamp=INPUT)
    answer['parentUuid']='attachment'
    if fault=='forward_parent':
        case['main']=[user,answer,attachment]
    elif fault=='foreign_chain':
        attachment.update(isSidechain=True,agentId='other')
        path=case['root']/'session'/'subagents'/'agent-other.jsonl'
        case['extra']=[(path,[attachment])]
    elif fault=='foreign_file':
        path=case['root']/'other'/'session.jsonl'
        case['extra']=[(path,[attachment])]
    else:
        answer['timestamp']='2026-09-30T23:59:59Z'
        case['main']=[user,attachment,answer]
    assert run(case)[0]==[]


def test_ordered_attachment_does_not_cross_a_new_bare_input(case):
    user,answer=case['main']
    bare={**user,'uuid':'bare','parentUuid':'input','message':{'content':'new unproved request'}}
    attachment={k:v for k,v in bare.items() if k not in {'message','promptId'}}
    attachment.update(type='attachment',uuid='attachment',parentUuid='bare',timestamp=AT)
    answer['parentUuid']='attachment'
    case['main']=[user,bare,attachment,answer]
    assert run(case)[0]==[]


def test_native_user_boundary_cannot_postdate_provider_message(case):
    # Host input is old, but this native input has not yet happened.
    case['main'][1]['timestamp']='2026-10-01T01:00:01.500Z'
    assert run(case)[0]==[]


def background_completion(c):
    user, answer = c['main']
    tool = {**answer,'uuid':'tool','parentUuid':'input','timestamp':'2026-10-01T01:00:03Z',
        'message':{**answer['message'],'id':'tool-message','content':[{'type':'tool_use','id':'tool-id','name':'Bash','input':{'run_in_background':True}}]}}
    path='/private/tmp/claude/session/tasks/task-id.output'
    result = {**user,'uuid':'result','parentUuid':'tool','sourceToolAssistantUUID':'tool','timestamp':'2026-10-01T01:00:04Z',
        'toolUseResult':{'backgroundTaskId':'task-id'},'message':{'content':[{'type':'tool_result','tool_use_id':'tool-id','content':'Output: '+path,'is_error':False}]}}
    notification={**user,'uuid':'notification','parentUuid':'result','promptId':'notification-prompt','timestamp':'2026-10-01T01:00:05Z',
        'origin':{'kind':'task-notification','producer':'session-task'},'promptSource':'system','turnOrigin':'task_notification',
        'message':{'content':'<task-notification>\n<task-id>task-id</task-id>\n<tool-use-id>tool-id</tool-use-id>\n<output-file>'+path+'</output-file>\n<status>completed</status>\n<summary>done</summary>\n</task-notification>'}}
    answer['parentUuid']='notification'
    c['main']=[user,tool,result,notification,answer]
    return user,tool,result,notification,answer


@pytest.mark.parametrize('run_id',[False,True])
@pytest.mark.parametrize('intervening_human',[False,True])
def test_background_completion_keeps_the_original_task_input(case,intervening_human,run_id):
    user,tool,result,notification,answer=background_completion(case)
    if run_id:notification['origin']['runId']='0mvc2mhj4-bca2be6f'  # newer CC
    if intervening_human:
        bare={**user,'uuid':'new-human','parentUuid':'result','timestamp':'2026-10-01T01:00:04.500Z','message':{'content':'an independent new request'}}
        notification['parentUuid']='new-human';case['main'].insert(3,bare)
    rows,_=run(case)
    route=next(r for r in rows if r.payload['target_ref']==reference('usage',['session','message']))
    assert route.payload['context']['entrypoint']=='telegram'
    assert route.payload['wi'] is None


@pytest.mark.parametrize('fault',['human_xml','wrong_origin','extra_origin','wrong_task','wrong_tool','not_background','wrong_output','forward_result','foreign_chain'])
def test_background_route_requires_native_system_and_exact_task_proof(case,fault):
    user,tool,result,notification,answer=background_completion(case)
    if fault=='human_xml':
        for key in ('origin','promptSource','turnOrigin'):notification.pop(key)
    elif fault=='wrong_origin':notification['origin']['producer']='human'
    elif fault=='extra_origin':notification['origin']['route']='forged'
    elif fault=='wrong_task':result['toolUseResult']['backgroundTaskId']='other'
    elif fault=='wrong_tool':result['message']['content'][0]['tool_use_id']='other'
    elif fault=='not_background':
        # A foreground shell has no native background task id.
        tool['message']['content'][0]['input']['run_in_background']=False;result['toolUseResult']={'stdout':''}
    elif fault=='wrong_output':notification['message']['content']=notification['message']['content'].replace('/private/tmp/claude','/other')
    elif fault=='forward_result':case['main']=[user,tool,notification,answer,result]
    elif fault=='foreign_chain':result['agentId']='other'
    rows,_=run(case)
    assert all(r.payload['target_ref']!=reference('usage',['session','message']) for r in rows)


@pytest.mark.parametrize('contradiction',['notification_after_usage','non_assistant_tool_source'])
def test_background_completion_cannot_override_contradictory_role_or_time(case,contradiction):
    user,tool,result,notification,answer=background_completion(case)
    if contradiction=='notification_after_usage':
        notification['timestamp']='2026-10-01T01:00:11Z'
        answer['timestamp']='2026-10-01T01:00:10Z'
    else:
        tool['type']='attachment'
    rows,_=run(case)
    # A valid earlier tool-use observation keeps its original route. Only the
    # answer descending from contradictory completion evidence stays unknown.
    assert all(r.payload['target_ref']!=reference('usage',['session','message']) for r in rows)

def test_unregistered_task_worktree_keeps_the_host_declared_route(case,tmp_path):
    # A task worktree outside the registered workspace is not contrary
    # evidence; the Relay execution still names the executor.
    worktree=tmp_path/'task-worktree';worktree.mkdir()
    for row in case['main']:row['cwd']=str(worktree)
    rows,_=run(case);assert len(rows)==1
    assert rows[0].payload['context']['entrypoint']=='telegram'


def subagent(c, agent, prompt, at, parent_answer=None):
    """A Task subagent transcript: its first input carries the launching request's promptId."""
    base={k:v for k,v in c['main'][0].items() if k not in {'message','uuid','parentUuid','promptId','timestamp'}}
    u={**base,'uuid':agent+'-input','isSidechain':True,'agentId':agent,'promptId':prompt,'timestamp':at,
       'parentUuid':None,'message':{'content':'delegated task'}}
    a={**c['main'][-1],'uuid':agent+'-answer','isSidechain':True,'agentId':agent,'parentUuid':agent+'-input',
       'timestamp':END,'message':{**c['main'][-1]['message'],'id':agent+'-message'}}
    c['extra'].append((c['root']/'session'/'subagents'/f'agent-{agent}.jsonl',[u,a]))
    return reference('usage',['session',agent+'-message'])


@pytest.mark.parametrize('layers',[1,2])
def test_subagent_launched_by_a_task_completion_inherits_the_original_input(case,layers):
    # The completion turn has its own promptId; the subagent carries it. The
    # walk continues through the completion to the human input that started
    # the background task, at any subagent depth.
    background_completion(case)
    targets=[subagent(case,f'layer{n}','notification-prompt',f'2026-10-01T01:00:0{5+n}Z') for n in range(1,layers+1)]
    rows,_=run(case)
    routes={r.payload['target_ref']:r.payload['context']['entrypoint'] for r in rows}
    assert all(routes.get(t)=='telegram' for t in targets)


def compaction(c):
    user,answer=c['main']
    summary={**user,'uuid':'summary','parentUuid':None,'timestamp':'2026-10-01T01:00:05Z','isCompactSummary':True,
             'isVisibleInTranscriptOnly':True,'message':{'content':'This session is being continued from a previous conversation.'}}
    later={**answer,'uuid':'later','parentUuid':'summary','timestamp':END,'message':{**answer['message'],'id':'later-message'}}
    c['main']=[user,answer,summary,later]
    return reference('usage',['session','later-message'])


def test_compaction_summary_continues_its_request_for_main_and_subagent(case):
    later=compaction(case)
    child=subagent(case,'after-compaction','prompt','2026-10-01T01:00:06Z')
    rows,_=run(case)
    routes={r.payload['target_ref']:r.payload['context']['entrypoint'] for r in rows}
    assert routes.get(later)=='telegram' and routes.get(child)=='telegram'


@pytest.mark.parametrize('fault',['unknown_prompt','summary_before_input','footer_in_summary_only'])
def test_compaction_summary_is_never_its_own_input_proof(case,fault):
    later=compaction(case)
    user,answer,summary,_=case['main']
    if fault=='unknown_prompt':summary['promptId']='other'
    elif fault=='summary_before_input':summary['timestamp']='2026-10-01T01:00:01Z'
    else:
        # A summary quoting the original footer cannot stand in for the input.
        summary['message']={'content':user['message']['content']};summary['promptId']='other'
    rows,_=run(case)
    assert later not in {r.payload['target_ref'] for r in rows}


@pytest.mark.parametrize('history,routed',[('absent',True),('later',True),('other',False),('malformed_pool',False)])
def test_route_alias_without_history_uses_the_host_named_executor(case,history,routed):
    # A Relay route alias enrolled after the input (or not yet) has no
    # history entry; the authenticated Host dispatch still names the executor.
    # A history entry in effect must agree, and a malformed pool never routes.
    entry=case['host']['principal_alias_history'][0]
    if history=='absent':case['host']['principal_alias_history']=[]
    elif history=='later':entry['effective_from']='2026-10-02T00:00:00Z'
    elif history=='other':entry['principal']='duke'
    else:
        case['record']['poolKey']='person'
        token=sha256(json.dumps(['telegram','event','person','person'],separators=(',',':')).encode()).hexdigest()
        raw=json.dumps(case['record']).encode();case['metadata'].unlink()
        path=case['metadata'].parent/(token+'.json');path.write_bytes(raw);path.chmod(0o600)
        case['main'][0]['message']['content']=f'original request\n\n[Host human input reference: {token}. Provenance only.]'
        case['host_facts'][2]=actor_receipt(case['host_facts'][0],node='mac',executor='pulse',entrypoint='telegram',
            input_ref=reference('input_metadata',sha256(raw).hexdigest()),observed=END,evidence=(reference('host_input','verified'),))
    rows,_=run(case)
    assert bool(rows) is routed


@pytest.mark.parametrize('variant',['auto_background','killed'])
def test_background_shell_moved_by_timeout_or_killed_keeps_the_original_input(case,variant):
    user,tool,result,notification,answer=background_completion(case)
    if variant=='auto_background':
        # CC moved a timed-out shell to the background; only its result names the task.
        tool['message']['content'][0]['input']={'command':'long'}
    else:
        notification['message']['content']=notification['message']['content'].replace('completed','killed')
    rows,_=run(case)
    assert any(r.payload['target_ref']==reference('usage',['session','message'])
               and r.payload['context']['entrypoint']=='telegram' for r in rows)


WORKTREE = ('<worktree><worktreePath>/repo/.claude/worktrees/agent-a1</worktreePath>'
            '<worktreeBranch>worktree-agent-a1</worktreeBranch></worktree>')


@pytest.mark.parametrize('worktree,routed',[
    (WORKTREE, True),
    # Native shape only: a relative path, a missing branch, an attribute or
    # a nested element is not the completion CC writes.
    (WORKTREE.replace('/repo', 'repo'), False),
    ('<worktree><worktreePath>/repo</worktreePath></worktree>', False),
    (WORKTREE.replace('<worktree>', '<worktree kind="x">'), False),
    (WORKTREE.replace('agent-a1</worktreeBranch>', '<x/>agent-a1</worktreeBranch>'), False)])
def test_completion_of_a_worktree_isolated_subagent_keeps_the_original_input(case,worktree,routed):
    # CC ends the completion of a subagent launched with worktree isolation
    # with the worktree it used (production 2026-10-26).
    user,tool,result,notification,answer=background_completion(case)
    notification['message']['content']=notification['message']['content'].replace(
        '</task-notification>', worktree+'\n</task-notification>')
    rows,_=run(case)
    assert any(r.payload['target_ref']==reference('usage',['session','message'])
               and r.payload['context']['entrypoint']=='telegram' for r in rows) is routed


DELIVERY_TOKEN = 'd' * 64


def delivery(c, *, session='session', at=AT, token=DELIVERY_TOKEN,
             reminder='[Relay] 本请求尚未声明 WI：先调用 declare_session_wi 登记本请求所属 WI，再继续工作。'):
    """A collaboration inbox turn as Relay delivers and records it (Relay/src/collaboration-capture.ts)."""
    from pulse_compute_cost.receipt_sources import read_host
    ref = reference('internal_delivery', [token, reference('native_session', ['cc', session])])
    event = {'schema': 'pulse_collaboration_host_event/v1', 'kind': 'attribution', 'at': at,
             'event_ref': reference('host_event', ['internal_delivery', ref]),
             'attribution_ref': reference('attribution', ['relay_internal_delivery.v1', ref]), 'target_ref': ref,
             'wi': None, 'executor': None, 'responsible': None, 'delegator': None, 'state': 'unassigned',
             'policy': 'relay_internal_delivery.v1', 'effective_at': at}
    c['host_facts'] = [*c['host_facts'][:2], *read_host(c['root'].parent/'host.jsonl', END,
                                                          data=json.dumps(event).encode()+b'\n')]
    user = c['main'][0]
    user['message']['content'] = ('[系统投递：协作合同答复，不是人类消息]\n…\n\n'
        f'[Host internal delivery reference: {DELIVERY_TOKEN}. Relay delivery, not a human message.]\n\n'
        + reminder)
    return user


def test_the_current_reminder_wording_still_marks_a_relay_delivery(case):
    from pulse_compute_cost.pi_context import SESSION_WI_REMINDER
    delivery(case, reminder=SESSION_WI_REMINDER)
    rows,_=run(case)
    assert [r.payload['context']['entrypoint'] for r in rows]==['automation']


def test_relay_delivered_turn_has_no_interactive_entrypoint(case):
    # Production 2026-10-26: collaboration inbox deliveries to Aki, Amanda,
    # Emi, Hestia and Pulse had no proven entrypoint at all.
    delivery(case)
    rows,usage=run(case)
    assert [r.payload['context']['entrypoint'] for r in rows]==['automation']
    assert rows[0].payload['effective_at']==AT and rows[0].payload['executor'] is None
    # The Host's delivery record targets no usage: the query shows the turn
    # with entrypoint "automation" and adds no claim or dispute of its own.
    from pulse_compute_cost.collaboration_query import CollaborationQuery, query_collaboration
    from test_collaboration_receipts import CATALOG
    class Memory:
        def receipt_snapshot(self, **kw):
            facts=usage+case['host_facts']+rows
            return {'facts':facts,'watermark':1,'recorded_at':END,'sha256':'0'*64}
    report=query_collaboration(Memory(),CollaborationQuery(scope='activity',from_date='2026-09-30',to_date='2026-10-02',
        group_by=('entrypoint',)),catalog=CATALOG,catalog_observed_at=END)
    native=[g for g in report['groups'] if g['usage']['event_count']]
    assert {g['key']['entrypoint'] for g in native}=={'automation'}
    assert not report['actor_attribution_conflicts']


@pytest.mark.parametrize('fault',['other_session','late_record','no_record','twice','quoted','token'])
def test_relay_delivery_route_needs_its_own_record(case,fault):
    if fault=='other_session':delivery(case,session='other')
    elif fault=='late_record':delivery(case,at='2026-10-01T01:00:03Z')
    elif fault=='token':delivery(case,token='e'*64)
    else:
        user=delivery(case)
        if fault=='no_record':case['host_facts']=case['host_facts'][:2]
        elif fault=='twice':
            case['main'].insert(1,{**user,'uuid':'again','parentUuid':'input','promptId':'prompt-2'})
        elif fault=='quoted':
            # A human message quoting a delivery keeps its own (here absent) proof.
            user['message']['content']+='\n\nquoted by a person'
    rows,_=run(case)
    assert rows==[]


def test_subagent_out_of_order_context_attachment_and_fork_copy_keep_the_route(case):
    # CC writes a subagent's context attachment before the attachment it
    # names as parent. A forked subagent's file starts with a copy of its
    # parent agent's records, re-stamped with the fork's own agentId.
    subagent(case,'parent-agent','prompt','2026-10-01T01:00:03Z')
    path,rows=case['extra'][0]
    first,answer=rows
    listing={k:v for k,v in first.items() if k not in {'message','promptId'}}
    listing.update(type='attachment',uuid='listing',parentUuid=first['uuid'],timestamp='2026-10-01T01:00:03.100Z')
    context={**listing,'uuid':'context','parentUuid':'listing','timestamp':'2026-10-01T01:00:03.200Z'}
    answer['parentUuid']='context'
    case['extra'][0]=(path,[context,first,listing,answer])
    fork_answer={**answer,'uuid':'fork-answer','agentId':'fork','parentUuid':'context','message':{**answer['message'],'id':'fork-message'}}
    copies=[{**r,'agentId':'fork'} for r in (first,listing,context)]
    case['extra'].append((path.parent/'agent-fork.jsonl',[*copies,fork_answer]))
    routes={r.payload['target_ref']:r.payload['context']['entrypoint'] for r in run(case)[0]}
    assert routes.get(reference('usage',['session','parent-agent-message']))=='telegram'
    assert routes.get(reference('usage',['session','fork-message']))=='telegram'


@pytest.mark.parametrize('fault',['not_copied','changed_copy'])
def test_fork_copy_must_precede_in_the_childs_own_file_unchanged(case,fault):
    subagent(case,'parent-agent','prompt','2026-10-01T01:00:03Z')
    path,(first,answer)=case['extra'][0]
    fork_answer={**answer,'uuid':'fork-answer','agentId':'fork','parentUuid':first['uuid'],'message':{**answer['message'],'id':'fork-message'}}
    rows=[fork_answer] if fault=='not_copied' else [{**first,'agentId':'fork','message':{'content':'changed task'}},fork_answer]
    case['extra'].append((path.parent/'agent-fork.jsonl',rows))
    routes={r.payload['target_ref'] for r in run(case)[0]}
    assert reference('usage',['session','fork-message']) not in routes


@pytest.mark.parametrize('copied',['streaming','changed_content'])
def test_fork_copy_of_a_still_streaming_reply_is_the_same_record(case,copied):
    # A fork taken while its parent's reply streams copies that reply before
    # CC settles its usage and stop reason. Its content cannot differ.
    subagent(case,'parent-agent','prompt','2026-10-01T01:00:03Z')
    path,(first,answer)=case['extra'][0]
    next_answer={**answer,'uuid':'next-answer','parentUuid':answer['uuid'],'message':{**answer['message'],'id':'next-message'}}
    case['extra'][0]=(path,[first,answer,next_answer])
    partial={**answer['message'],'usage':{**COUNTS,'output_tokens':1},'stop_reason':None}
    if copied=='changed_content':
        partial['content']=[{'type':'text','text':'different reply'}]
    fork_answer={**answer,'uuid':'fork-answer','agentId':'fork','parentUuid':answer['uuid'],'message':{**answer['message'],'id':'fork-message'}}
    case['extra'].append((path.parent/'agent-fork.jsonl',[{**first,'agentId':'fork'},{**answer,'agentId':'fork','message':partial},fork_answer]))
    routes={r.payload['target_ref'] for r in run(case)[0]}
    assert (reference('usage',['session','next-message']) in routes)==(copied=='streaming')


@pytest.mark.parametrize('copy',['relinked','relinked_main'])
def test_fork_may_relink_the_subagent_records_it_copies(case,copy):
    # The original writes a late attachment after the reply it precedes; the
    # fork copies the same records in order, naming a different parent.
    subagent(case,'parent-agent','prompt','2026-10-01T01:00:03Z')
    path,(first,answer)=case['extra'][0]
    late={k:v for k,v in first.items() if k not in {'message','promptId'}}
    late.update(type='attachment',uuid='late',parentUuid=answer['uuid'],timestamp='2026-10-01T01:00:04Z',attachment={'type':'prompt_snapshot'})
    following={**answer,'uuid':'following','parentUuid':'late','message':{**answer['message'],'id':'following-message'}}
    case['extra'][0]=(path,[first,answer,late,following])
    copies=[{**first,'agentId':'fork'},{**late,'agentId':'fork','parentUuid':first['uuid']},
            {**answer,'agentId':'fork','parentUuid':'late'}]
    fork_answer={**answer,'uuid':'fork-answer','agentId':'fork','parentUuid':answer['uuid'],'message':{**answer['message'],'id':'fork-message'}}
    case['extra'].append((path.parent/'agent-fork.jsonl',[*copies,fork_answer]))
    if copy=='relinked_main':
        # Main-session records are never copied; a re-linked one is contrary.
        user,main_answer=case['main']
        case['main']=[user,main_answer,{**main_answer,'parentUuid':None}]
    routes={r.payload['target_ref'] for r in run(case)[0]}
    assert {reference('usage',['session',m]) for m in ('fork-message','following-message')} <= routes
    assert (reference('usage',['session','message']) in routes)==(copy=='relinked')


@pytest.mark.parametrize('fault',[None,'reply_blocks','other_attachment','not_next','other_reply'])
def test_deferred_tools_record_written_right_after_its_reply_is_its_parent(case,fault):
    user,answer=case['main']
    record={k:v for k,v in user.items() if k not in {'message','promptId'}}
    record.update(type='attachment',uuid='deferred',parentUuid='input',timestamp=END,
                  attachment={'type':'skill_listing' if fault=='other_attachment' else 'deferred_tools_record','entries':[]})
    answer['parentUuid']='deferred'
    between={**record,'uuid':'between','parentUuid':'input','attachment':{'type':'environment'}}
    # CC streams one reply as several records under one message id.
    block={**answer,'uuid':'answer-block','parentUuid':answer['uuid']}
    other={**block,'message':{**answer['message'],'id':'another-reply'}}
    middle={'not_next':[between],'reply_blocks':[block],'other_reply':[other]}.get(fault,[])
    case['main']=[user,answer,*middle,record]
    assert bool(run(case)[0])==(fault in (None,'reply_blocks'))


@pytest.mark.parametrize('original',[True,False])
def test_relay_continuation_redispatch_keeps_the_original_input_route(case,original):
    # A Relay continuation turn re-dispatches the same input after the native
    # input was recorded. The original dispatch, before it, still proves it.
    ex,aggregate,origin=case['host_facts']
    later=Receipt.make('execution','attempt-2',{**ex.payload},occurred_at='2026-10-01T01:00:05Z',observed_at=END,producer='relay_host.v1')
    again=usage_receipt('cc','aggregate-2',COUNTS,at=END,observed=END,model='test',mapping='cc_result.v1',execution_ref=later.entity_ref,
        native_scope_ref=aggregate.payload['native_scope_ref'],granularity='turn_aggregate',interval=('2026-10-01T01:00:05Z',END))
    route=actor_receipt(later,node='mac',executor='pulse',entrypoint='telegram',input_ref=origin.payload['context']['input_ref'],
        observed=END,evidence=(reference('host_input','verified'),))
    case['host_facts']=([ex,aggregate,origin] if original else [])+[later,again,route]
    rows,_=run(case)
    assert bool(rows) is original

@pytest.mark.parametrize('layout',['string','block'])
def test_relay_wi_reminder_after_the_reference_keeps_the_route(case,layout):
    from pathlib import Path
    from pulse_compute_cost.pi_context import SESSION_WI_REMINDER
    user=case['main'][0];text=user['message']['content']
    user['message']['content']=(f'{text}\n\n{SESSION_WI_REMINDER}' if layout=='string'
        else [{'type':'text','text':text},{'type':'text','text':SESSION_WI_REMINDER}])
    rows,_=run(case);assert [r.payload['context']['entrypoint'] for r in rows]==['telegram']
    # Any other trailing text still hides the reference.
    user['message']['content']=f'{text}\n\n[Relay] something else'
    assert run(case)[0]==[]
    relay=(Path(__file__).resolve().parents[2]/'Relay/src/session-wi-guard.ts').read_text()
    assert f"SESSION_WI_REMINDER = '{SESSION_WI_REMINDER}'" in relay


@pytest.mark.parametrize('layout',['caption','photo_only','file_then_caption'])
def test_media_message_keeps_its_host_input_route(case,layout):
    # Relay closes a photo/file message with the person's words and its provenance.
    user=case['main'][0]; text=user['message']['content']
    words,marker=text.split('\n\n[Host human input reference:',1)
    marker='\n\n[Host human input reference:'+marker
    image={'type':'image','source':{'type':'base64','media_type':'image/png','data':'AA=='}}
    if layout=='caption':
        blocks=[image,{'type':'text','text':words+marker}]
    elif layout=='photo_only':
        case['record']['source']['message']='[图片]'
        raw=json.dumps(case['record']).encode();case['metadata'].write_bytes(raw);case['metadata'].chmod(0o600)
        ex=case['host_facts'][0]
        case['host_facts'][2]=actor_receipt(ex,node='mac',executor='pulse',entrypoint='telegram',
            input_ref=reference('input_metadata',sha256(raw).hexdigest()),observed=END,evidence=(reference('host_input','verified'),))
        blocks=[image,{'type':'text','text':'[图片]'+marker}]
    else:
        blocks=[{'type':'text','text':'[文件: a.txt]\ncontent'},{'type':'text','text':'\n\n'+words+marker}]
    user['message']['content']=blocks
    rows,_=run(case)
    assert [r.payload['context']['entrypoint'] for r in rows]==['telegram']
