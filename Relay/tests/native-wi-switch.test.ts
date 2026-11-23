import {describe, expect, it, vi} from 'vitest';
import {switchWorkItem} from '../src/native-wi-switch.js';
import {acquireConversation} from '../src/conversation-acquisition.js';
import {conversationLaunch} from '../src/conversation-launch.js';
import {buildRuntimeUsageBinding, runtimeAuthorityRef, usageRuntimeForConversation} from '../src/runtime-usage-binding.js';
import type {AgentConfig} from '../src/types.js';

const agent:AgentConfig={id:'pulse',name:'Pulse',emoji:'P',workspace:'/default',runtime:'cc',sessionId:'configured',
  linuxUser:'tenant-pulse',userHome:'/home/tenant-pulse',ccSdkProviderMode:'subscription'};

/** A binding as the WI runtime layer persists it for the session it names. */
function binding(sessionId:string, workItemId='wi_20990101_271') {
  const usageBinding=buildRuntimeUsageBinding({workItemId,agentId:'pulse',phase:'work_item',epoch:1,
    runtime:usageRuntimeForConversation('cc'),procurementSource:'claude_subscription',
    authorityRef:runtimeAuthorityRef(sessionId),issuedAt:'2026-09-01T00:00:00Z'});
  return {schema:'pulse_active_work_item_runtime_binding/v1' as const,workItemId:usageBinding.work_item_id,
    sessionId,sessionRef:runtimeAuthorityRef(sessionId),runtime:'cc' as const,workspace:'/wt',usageBinding};
}

/** One chat: the persisted identity unit, one live process, the real launch config, a kernel stub. */
function fixture(options:{leave?:Record<string,unknown>;enter?:Record<string,unknown>;state?:string;
  runtime?:'cc'|'codex'|'agy';extra?:Record<string,unknown>;flush?:()=>void}={}) {
  const state:Record<string,any>={poolKey:'tg:1',agentId:'pulse',sessionId:'sess-a',runtime:options.runtime??'cc',
    status:'active',lastActivity:1,turnCount:1,maxTurns:100,...options.extra};
  let pid=100;
  let live:any={sessionId:'sess-a',state:options.state??'ready',process:{pid}};
  // Like the real adapters, every get() returns a fresh wrapper around the same native process.
  const runtime:any={get:vi.fn(()=>live && {...live}),kill:vi.fn(async()=>{live=undefined;}),
    spawn:vi.fn(async(launch:any)=>(live={sessionId:launch.sessionId,state:'ready',process:{pid:++pid}}))};
  const launches:any[]=[];
  const acquire=vi.fn(async()=>{
    if(!live){
      // The real launch path: it throws when the persisted identity unit is inconsistent.
      const launch=conversationLaunch({poolKey:'tg:1',agent,session:state as any,channel:'telegram',intent:'inbound'});
      launches.push(launch);
      live={sessionId:launch.sessionId,state:'ready',process:{pid:++pid}};
    }
    return 'tg:1';
  });
  const stateManager:any={getSession:()=>state,updateSession:(_k:string,p:any)=>Object.assign(state,p),
    flushStrict:vi.fn(()=>options.flush?.())};
  const control=vi.fn(async(request:any)=>request.phase==='leave'
    ? options.leave ?? {status:'local_committed',work_item:{id:'wi_a'}}
    : options.enter ?? {status:'local_committed',work_item:{id:'wi_b',business_progress:'active'}});
  let latest=true;
  const run=(target:string,agentRequest?:boolean)=>switchWorkItem({poolKey:'tg:1',target,ref:'r'.repeat(64),runtime,stateManager,acquire,
    control,newSessionId:()=>'sess-new',isLatest:()=>latest,agentRequest});
  return {state,runtime,acquire,control,run,launches,live:()=>live,setLatest:(v:boolean)=>{latest=v;}};
}

describe('/wi switch', () => {
  it('saves the whole identity unit, starts unseen work fresh, and restores it on return', async () => {
    const f=fixture({extra:{activeWorkItemRuntimeBinding:binding('sess-a'),
      terminalWorkItemRuntimeBinding:undefined}});
    const first=await f.run('wi_b');
    expect(first.resumed).toBe(false);
    expect(first.text).toContain('wi_a 已记检查点');
    expect(f.control.mock.calls.map(c=>[c[0].phase,c[0].pid])).toEqual([['leave',100],['enter',101]]);
    expect(f.state.sessionId).toBe('sess-new');
    expect(f.state.activeWorkItemRuntimeBinding).toBeUndefined();  // Not carried into another session.
    expect(f.state.workItemSessions.wi_a.unit.activeWorkItemRuntimeBinding.sessionId).toBe('sess-a');
    expect(f.launches[0]).toMatchObject({sessionId:'sess-new'});
    f.control.mockImplementation(async(request:any)=>request.phase==='leave'
      ? {status:'local_committed',work_item:{id:'wi_b'}} : {status:'local_committed',work_item:{id:'wi_a'}});
    expect(f.state.pendingWiSwitch).toMatchObject({target:'wi_b',phase:'entered'});
    f.state.pendingWiSwitch=undefined;  // Its continuation was admitted (deliverSwitchContinuation).
    const back=await f.run('wi_a');
    expect(back.resumed).toBe(true);
    expect(f.state.sessionId).toBe('sess-a');
    expect(f.state.activeWorkItemRuntimeBinding.sessionId).toBe('sess-a');
    expect(f.launches[1]).toMatchObject({sessionId:'sess-a',resume:true,workspace:'/wt'});
  });

  it('keeps each session its own /think level: a new target starts at the default, a return restores its level', async () => {
    const f=fixture({extra:{reasoningLevel:'max'}});
    await f.run('wi_b');
    expect(f.state.sessionId).toBe('sess-new');
    expect(f.state.reasoningLevel).toBeUndefined();
    expect(f.launches[0].reasoningLevel).toBeUndefined();
    expect(f.state.workItemSessions.wi_a.unit.reasoningLevel).toBe('max');
    f.state.reasoningLevel='low';  // /think low in the target session.
    f.state.pendingWiSwitch=undefined;
    f.control.mockImplementation(async(request:any)=>request.phase==='leave'
      ? {status:'local_committed',work_item:{id:'wi_b'}} : {status:'local_committed',work_item:{id:'wi_a'}});
    await f.run('wi_a');
    expect(f.state.sessionId).toBe('sess-a');
    expect(f.launches[1]).toMatchObject({sessionId:'sess-a',reasoningLevel:'max'});
    expect(f.state.workItemSessions.wi_b.unit.reasoningLevel).toBe('low');
  });

  it('refuses while working, mid-entry, or for Codex or agy work that has no session yet', async () => {
    for(const f of [fixture({state:'busy'}),fixture({extra:{pendingWorkItemEntry:{workItemId:'wi_x'}}}),
      fixture({runtime:'codex'}),fixture({runtime:'agy'})]){
      expect((await f.run('wi_b')).text).toContain('未做任何改动');
      expect(f.control).not.toHaveBeenCalled();
      expect(f.runtime.kill).not.toHaveBeenCalled();
    }
  });

  it('moves between agy conversations by their own ids, each with its WI binding', async () => {
    const bound=(sessionId:string,wi:string)=>({sessionId,conversationId:'agy-'+sessionId.slice(5),usageBinding:buildRuntimeUsageBinding({workItemId:wi,agentId:'pulse',
      phase:'conversation',epoch:1,runtime:'agy',procurementSource:'unknown',issuedAt:'2026-11-22T00:00:00Z'})});
    const state:Record<string,any>={poolKey:'tg:1',agentId:'pulse',sessionId:'sess-a',runtime:'agy',status:'active',
      lastActivity:1,turnCount:1,maxTurns:100,agyConversation:{sessionId:'sess-a',conversationId:'agy-a'},
      agyWorkItemBinding:bound('sess-a','wi_20990101_001'),
      workItemSessions:{wi_20990101_002:{at:'t',unit:{sessionId:'sess-b',runtime:'agy',
        agyConversation:{sessionId:'sess-b',conversationId:'agy-b'},agyWorkItemBinding:bound('sess-b','wi_20990101_002')}}}};
    let live:any={sessionId:'sess-a',runtime:'agy',state:'idle',agyConversationId:'agy-a'};
    const launches:any[]=[];
    const runtime:any={get:vi.fn(()=>live&&{...live}),kill:vi.fn(async()=>{live=undefined;})};
    const acquire=vi.fn(async()=>{
      if(!live){
        const launch=conversationLaunch({poolKey:'tg:1',agent:{...agent,runtime:'agy'},session:state as any,channel:'telegram',intent:'inbound'});
        launches.push(launch);
        live={sessionId:launch.sessionId,runtime:'agy',state:'idle',agyConversationId:state.agyConversation?.conversationId};
      }
      return 'tg:1';
    });
    const control=vi.fn(async(request:any)=>request.phase==='leave'
      ? {status:'local_committed',work_item:{id:'wi_20990101_001'}}
      : {status:'local_committed',work_item:{id:'wi_20990101_002',business_progress:'active'}});
    const stateManager:any={getSession:()=>state,updateSession:(_k:string,p:any)=>Object.assign(state,p),flushStrict:vi.fn()};
    const outcome=await switchWorkItem({poolKey:'tg:1',target:'wi_20990101_002',ref:'r'.repeat(64),runtime,stateManager,acquire,control});
    expect(outcome.resumed).toBe(true);
    expect(control.mock.calls.map(([r])=>[r.phase,r.native_id,r.pid])).toEqual([['leave','agy-a',undefined],['enter','agy-b',undefined]]);
    expect(launches[0].computeCostBinding.work_item_id).toBe('wi_20990101_002');
    expect(state.workItemSessions.wi_20990101_001.unit.agyWorkItemBinding.usageBinding.work_item_id).toBe('wi_20990101_001');
  });

  it('keeps everything when recording the current work fails', async () => {
    const f=fixture({leave:{status:'rejected',error:{code:'authority_changed'}}});
    expect((await f.run('wi_b')).text).toContain('原会话保留');
    expect(f.runtime.kill).not.toHaveBeenCalled();
    expect(f.state.sessionId).toBe('sess-a');
  });

  it('returns to the original unit when entering is refused', async () => {
    const f=fixture({enter:{status:'rejected',error:{code:'authority_denied'}}});
    const outcome=await f.run('wi_b');
    expect(outcome.target).toBeUndefined();
    expect(outcome.text).toContain('已回到原会话');
    expect(f.state.sessionId).toBe('sess-a');
    expect(f.live().sessionId).toBe('sess-a');
  });

  it('never rolls back a committed enter; a later bookkeeping failure is only reported', async () => {
    let flushes=0;
    const f=fixture({flush:()=>{if(++flushes===2)throw new Error('disk_full');}});
    const outcome=await f.run('wi_b');
    expect(outcome.target).toBe('wi_b');
    expect(outcome.text).toContain('切换本身已生效');
    expect(f.state.sessionId).toBe('sess-new');
    expect(f.runtime.kill).toHaveBeenCalledTimes(1);
  });

  it('reads an unknown enter back, and returns to the original session keeping the record while it stays unknown', async () => {
    const f=fixture({enter:{status:'unknown',error:{code:'native_channel_result_unknown'}}});
    const outcome=await f.run('wi_b');
    expect(f.control.mock.calls.filter(c=>c[0].phase==='enter')).toHaveLength(3);
    // The readback asks as the same target session, by the same reference.
    expect(f.control.mock.calls.filter(c=>c[0].phase==='readback').map(c=>[c[0].ref,c[0].pid]))
      .toEqual(Array(3).fill(['r'.repeat(64),101]));
    expect(outcome.target).toBeUndefined();
    expect(outcome.retained).toBe(true);
    expect(outcome.text).toContain('结果未知');
    expect(f.state.sessionId).toBe('sess-a');
    expect(f.live().sessionId).toBe('sess-a');
    expect(f.state.pendingWiSwitch).toMatchObject({ref:'r'.repeat(64),target:'wi_b',dest:{sessionId:'sess-new'},
      origin:{sessionId:'sess-a'}});
  });

  it('an unknown enter that reads back as committed completes; as absent it returns and clears', async () => {
    for(const committed of [true,false]){
      const f=fixture();
      f.control.mockImplementation(async(request:any)=>request.phase==='leave'
        ? {status:'local_committed',work_item:{id:'wi_a'}}
        : request.phase==='readback' ? {schema:'pulse.wi_switch/v1',phase:'readback',wi:'wi_b',committed}
        : {status:'unknown',error:{code:'native_channel_timeout'}});
      const outcome=await f.run('wi_b');
      expect(outcome.target).toBe(committed ? 'wi_b' : undefined);
      expect(f.state.sessionId).toBe(committed ? 'sess-new' : 'sess-a');
      // Entered: the record waits for the continuation; returned: it is cleared.
      expect(f.state.pendingWiSwitch?.phase).toBe(committed ? 'entered' : undefined);
    }
  });

  it('a readback that settles the unknown enter completes the switch', async () => {
    const f=fixture();
    let enters=0;
    f.control.mockImplementation(async(request:any)=>request.phase==='leave'
      ? {status:'local_committed',work_item:{id:'wi_a'}}
      : ++enters===1 ? {status:'unknown',error:{code:'native_channel_result_unknown'}}
      : {status:'local_committed',work_item:{id:'wi_b'}});
    const outcome=await f.run('wi_b');
    expect(outcome.target).toBe('wi_b');
    expect(f.state.sessionId).toBe('sess-new');
  });

  it('switching to the work already here changes nothing', async () => {
    const f=fixture({leave:{schema:'pulse.wi_switch/v1',phase:'leave',left:'wi_b',recorded:false}});
    expect((await f.run('wi_b')).text).toContain('无需切换');
    expect(f.runtime.kill).not.toHaveBeenCalled();
  });

  it('does not replace a session that started a turn while the work was being recorded', async () => {
    const f=fixture();
    f.control.mockImplementation(async(request:any)=>{
      if(request.phase==='leave'){f.live().state='busy';return {status:'local_committed',work_item:{id:'wi_a'}};}
      return {status:'local_committed',work_item:{id:'wi_b'}};
    });
    const outcome=await f.run('wi_b');
    expect(outcome.target).toBeUndefined();
    expect(outcome.text).toContain('切换未进行');
    expect(f.runtime.kill).not.toHaveBeenCalled();
    expect(f.state.sessionId).toBe('sess-a');
    expect(f.state.workItemSessions.wi_a.unit.sessionId).toBe('sess-a');
  });

  it('does not replace the session when a later human input arrived', async () => {
    const f=fixture();
    f.control.mockImplementation(async(request:any)=>{
      f.setLatest(false);
      return {status:'local_committed',work_item:{id:request.phase==='leave'?'wi_a':'wi_b'}};
    });
    expect((await f.run('wi_b')).text).toContain('切换未进行');
    expect(f.runtime.kill).not.toHaveBeenCalled();
  });

  it('refuses a second switch while one is in progress', async () => {
    const f=fixture();
    let release:()=>void=()=>{};
    f.control.mockImplementationOnce(()=>new Promise<any>(resolve=>{release=()=>resolve({status:'local_committed',work_item:{id:'wi_a'}});}));
    const first=f.run('wi_b');
    expect((await f.run('wi_c')).text).toContain('另一个 /wi switch 正在进行');
    release();
    expect((await first).target).toBe('wi_b');
  });
  it('the same idle process behind a fresh wrapper is switched, not refused', async () => {
    const f=fixture();
    const outcome=await f.run('wi_b');
    expect(outcome.target).toBe('wi_b');
    expect(f.runtime.kill).toHaveBeenCalledTimes(1);
  });

  it('a process restarted during leave is not replaced', async () => {
    const f=fixture();
    f.control.mockImplementationOnce(async()=>{
      await f.runtime.kill(); await f.acquire();  // Same session id, new native process.
      return {status:'local_committed',work_item:{id:'wi_a'}};
    });
    f.runtime.kill.mockClear();
    expect((await f.run('wi_b')).text).toContain('切换未进行');
  });

  for(const enter of [{status:'local_committed',work_item:{id:'wi_b'}},{status:'rejected',error:{code:'authority_denied'}}])
  it('an inbound turn waits for the whole switch and then gets the session that stands: '+enter.status, async () => {
    const f=fixture();
    const inbound=()=>acquireConversation({poolKey:'tg:1',agent,runtime:f.runtime,stateManager:f.state && {
      getSession:()=>f.state,updateSession:(_k:string,p:any)=>Object.assign(f.state,p),flush:()=>{}} as any,
      channel:'telegram',intent:'inbound'});
    // The real acquisition path for the switch's own target start: it runs inside the switch's slot.
    f.acquire.mockImplementation(async()=>{await inbound(); return 'tg:1';});
    let release:()=>void=()=>{}, entering:()=>void=()=>{};
    const gate=new Promise<void>(resolve=>{release=resolve;});
    const inEnter=new Promise<void>(resolve=>{entering=resolve;});
    let got:any;
    f.control.mockImplementation(async(request:any)=>{
      if(request.phase==='leave')return {status:'local_committed',work_item:{id:'wi_a'}};
      entering();
      await gate;
      return enter;
    });
    const switched=f.run('wi_b');
    await inEnter;
    // While the kernel moves authority, a human message arrives on its own event.
    const arriving=inbound().then(proc=>{got=proc;});
    await new Promise(resolve=>setTimeout(resolve,5));
    expect(got).toBeUndefined();  // Not handed the unconfirmed target.
    release();
    await switched;
    await arriving;
    expect(got.sessionId).toBe(enter.status==='rejected' ? 'sess-a' : 'sess-new');
    expect(got.state).not.toBe('dead');
  });
});

describe('/wi switch leave and Agent requests', () => {
  it('a target that is a child of the held WI is answered before anything changes', async () => {
    const f=fixture({leave:{schema:'pulse.wi_switch/v1',phase:'leave',left:'wi_a',recorded:false,
      no_switch:'wi_c 是当前 WI wi_a 的子 WI，执行权已在本会话，无需切换'}});
    const outcome=await f.run('wi_c');
    expect(outcome.text).toBe('wi_c 是当前 WI wi_a 的子 WI，执行权已在本会话，无需切换。');
    expect(outcome.target).toBeUndefined();
    expect(f.runtime.kill).not.toHaveBeenCalled();
    expect(f.state.workItemSessions).toBeUndefined();
  });

  it('reads an unknown leave back with the same reference and continues once it is committed', async () => {
    const f=fixture();
    let leaves=0;
    f.control.mockImplementation(async(request:any)=>request.phase==='leave'
      ? ++leaves===1 ? {status:'unknown',error:{code:'native_channel_timeout'}} : {status:'local_committed',work_item:{id:'wi_a'}}
      : {status:'local_committed',work_item:{id:'wi_b'}});
    const outcome=await f.run('wi_b');
    expect(f.control.mock.calls.filter(c=>c[0].phase==='leave').map(c=>c[0].ref)).toEqual(['r'.repeat(64),'r'.repeat(64)]);
    expect(outcome.target).toBe('wi_b');
  });

  it('a leave that stays unknown changes nothing and names its cause', async () => {
    const f=fixture({leave:{status:'unknown',error:{code:'native_channel_timeout'}}});
    const outcome=await f.run('wi_b');
    expect(f.control.mock.calls.filter(c=>c[0].phase==='leave')).toHaveLength(3);
    expect(outcome.text).toContain('结果未知（native_channel_timeout）');
    expect(f.runtime.kill).not.toHaveBeenCalled();
    expect(f.state.sessionId).toBe('sess-a');
  });

  it('a switch records the unit it left and the target identity until its continuation is due', async () => {
    const f=fixture();
    let during:any;
    f.control.mockImplementation(async(request:any)=>{
      if(request.phase==='leave')return {schema:'pulse.wi_switch/v1',phase:'leave',left:'wi_a',recorded:false,agent_request:true};
      during=structuredClone(f.state.pendingWiSwitch);
      return {status:'local_committed',work_item:{id:'wi_b'}};
    });
    const outcome=await f.run('wi_b',true);
    expect(outcome.target).toBe('wi_b');
    expect(during).toMatchObject({ref:'r'.repeat(64),target:'wi_b',origin:{sessionId:'sess-a'},dest:{sessionId:'sess-new'},agent:true});
    expect(f.state.pendingWiSwitch).toMatchObject({phase:'entered',dest:{sessionId:'sess-new'}});
    expect(f.state.workItemSessions.wi_a.unit.sessionId).toBe('sess-a');
  });

  it('an Agent switch whose enter is refused returns to the unit it left and clears the mark', async () => {
    const f=fixture({enter:{status:'rejected',error:{code:'authority_denied'}}});
    const outcome=await f.run('wi_b',true);
    expect(outcome.text).toContain('已回到原会话');
    expect(f.state.sessionId).toBe('sess-a');
    expect(f.state.pendingWiSwitch).toBeUndefined();
  });

  it('a new switch waits while an earlier one is unsettled', async () => {
    const f=fixture({extra:{pendingWiSwitch:{ref:'x',target:'wi_b',origin:{sessionId:'sess-a'},dest:{sessionId:'sess-b'},resumed:false,at:'t'}}});
    expect((await f.run('wi_c')).text).toContain('未做任何改动');
    expect(f.control).not.toHaveBeenCalled();
  });
});
