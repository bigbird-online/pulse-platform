import {describe,it,expect,vi} from 'vitest';
import {mkdtempSync,writeFileSync,existsSync,chmodSync} from 'node:fs';
import {tmpdir} from 'node:os';
import {join} from 'node:path';
import {switchWorkItem,settleInterruptedSwitch} from '../src/native-wi-switch.js';
import {runAgentSwitch,settleWiSwitch} from '../src/native-work-item-command.js';
import {adoptAgentSwitchRequests,readAgentSwitchRequests} from '../src/agent-switch-request.js';
import {acquireConversation} from '../src/conversation-acquisition.js';
import type {AgentConfig} from '../src/types.js';

const agent:AgentConfig={id:'pulse',name:'Pulse',emoji:'P',workspace:'/default',runtime:'cc',sessionId:'configured',
  linuxUser:'tenant-pulse',userHome:'/home/tenant-pulse',ccSdkProviderMode:'subscription'};

/**
 * A core that keeps what relay-channel commits, by the same rules: enter commits once per
 * reference for the session that made it; the same session reads it back, another session
 * gets request_conflict (agent-harness test_recovery_reads_a_committed_enter_back_and_reenters_only_as_the_same_target).
 */
function core(sessionOf:(pid:number)=>string){
  const entered=new Map<string,string>();
  let readbackFailures=0;
  const control=vi.fn(async(request:any):Promise<any>=>{
    const session=sessionOf(request.pid);
    if(request.phase==='leave')return {schema:'pulse.wi_switch/v1',phase:'leave',left:'wi_a',recorded:false,agent_request:true};
    if(request.phase==='readback'){
      if(readbackFailures>0){readbackFailures--;return {status:'unknown',error:{code:'native_channel_timeout'}};}
      return {schema:'pulse.wi_switch/v1',phase:'readback',wi:'wi_b',committed:entered.has(request.ref)};
    }
    const prior=entered.get(request.ref);
    if(prior===undefined){entered.set(request.ref,session);return {status:'local_committed',work_item:{id:'wi_b'}};}
    return prior===session ? {status:'read_only',work_item:{id:'wi_b'}}
      : {status:'rejected',error:{code:'request_conflict'}};
  });
  return {control,entered,failReadback:(n:number)=>{readbackFailures=n;}};
}

/** One chat; `restart()` drops every process and keeps only what was flushed to disk. */
function relay(){
  let disk:any={poolKey:'tg:1',agentId:'pulse',sessionId:'sess-a',runtime:'cc',status:'active',lastActivity:1,
    turnCount:1,maxTurns:100};
  let state:any=structuredClone(disk);
  let pid=100;
  const pids=new Map<number,string>([[100,'sess-a']]);
  let live:any={sessionId:'sess-a',state:'ready',process:{pid}};
  const start=(sessionId:string)=>{live={sessionId,state:'ready',process:{pid:++pid}};pids.set(pid,sessionId);return live;};
  const runtime:any={get:vi.fn(()=>live&&{...live}),kill:vi.fn(async()=>{live=undefined;}),
    spawn:vi.fn(async(launch:any)=>start(launch.sessionId)),
    sendIdempotent:vi.fn(async(_k:string,_m:any,_key:string,o?:any)=>{o?.onAccepted?.();}),
    send:vi.fn(async(_k:string,_m:any,o?:any)=>{o?.onAccepted?.();})};
  const acquire=vi.fn(async()=>{if(!live)start(state.sessionId);return 'tg:1';});
  const stateManager:any={getSession:()=>state,updateSession:(_k:string,p:any)=>Object.assign(state,p),
    flush:()=>{},flushStrict:vi.fn(()=>{disk=structuredClone(state);})};
  /** A channel's inbound acquisition, through the real shared slot. */
  const inbound=()=>acquireConversation({poolKey:'tg:1',agent,runtime,stateManager,channel:'telegram',intent:'inbound'});
  const k=core(p=>pids.get(p)!);
  let ids=0;
  const run=()=>switchWorkItem({poolKey:'tg:1',target:'wi_b',ref:'r'.repeat(64),runtime,stateManager,acquire,
    control:k.control,newSessionId:()=>'sess-new-'+(++ids),agentRequest:true});
  const settle=()=>settleInterruptedSwitch({poolKey:'tg:1',runtime,stateManager,acquire,control:k.control});
  return {k,run,settle,acquire,runtime,stateManager,inbound,
    state:()=>state,live:()=>live,
    /** Relay stops now: only the flushed state survives. */
    restart(){state=structuredClone(disk);live=undefined;},
    /** The disk as of now, for a crash at this point. */
    snapshot(){return structuredClone(disk);},
    crashWith(saved:any){disk=saved;state=structuredClone(saved);live=undefined;}};
}

describe('/wi switch recovery against a core that keeps its commits',()=>{
  it('enter committed, then Relay stopped before saving it: the restart settles on the same target session',async()=>{
    const r=relay();
    let atEnter:any;
    const real=r.k.control.getMockImplementation()!;
    r.k.control.mockImplementation(async(request:any)=>{
      const answer=await real(request);
      if(request.phase==='enter'&&!atEnter)atEnter=r.snapshot();  // Committed; the answer never arrives.
      return answer;
    });
    await r.run();
    r.crashWith(atEnter);
    expect(r.state().pendingWiSwitch).toMatchObject({dest:{sessionId:'sess-new-1'},origin:{sessionId:'sess-a'}});
    const settled=await r.settle();
    expect(settled).toMatchObject({settled:'entered',target:'wi_b'});
    expect(r.state().sessionId).toBe('sess-new-1');
    expect(r.state().pendingWiSwitch.phase).toBe('entered');  // Its continuation is still due.
    expect(r.state().workItemSessions.wi_b.unit.sessionId).toBe('sess-new-1');
    expect(r.state().workItemSessions.wi_a.unit.sessionId).toBe('sess-a');
    // The target session is the one that entered: a repeated enter reads back, never conflicts.
    const again=await r.k.control({ref:'r'.repeat(64),pid:r.live().process.pid,phase:'enter'});
    expect(again.status).toBe('read_only');
    // The pass hands the continuation over; once admitted, nothing is left for a later restart.
    const reply=vi.fn(async()=>{});
    expect(await settleWiSwitch({poolKey:'tg:1',runtime:r.runtime,stateManager:r.stateManager,acquire:r.acquire,
      reply,control:r.k.control as any})).toBe(true);
    expect(r.runtime.sendIdempotent.mock.calls.at(-1)[2]).toBe('operation_v1_wi_switch_'+'r'.repeat(64));
    expect(r.state().pendingWiSwitch).toBeUndefined();
    r.restart();
    expect(r.state().pendingWiSwitch).toBeUndefined();
  });

  it('Relay stopped after replacing the session but before enter: the restart returns to the source',async()=>{
    const r=relay();
    let beforeEnter:any;
    const real=r.k.control.getMockImplementation()!;
    r.k.control.mockImplementation(async(request:any)=>{
      if(request.phase==='enter'&&!beforeEnter)beforeEnter=r.snapshot();
      return real(request);
    });
    await r.run();
    r.k.entered.clear();  // The crash came before the core saw enter.
    r.crashWith(beforeEnter);
    expect(r.state().sessionId).toBe('sess-new-1');
    const settled=await r.settle();
    expect(settled).toMatchObject({settled:'returned'});
    expect(r.state().sessionId).toBe('sess-a');
    expect(r.state().pendingWiSwitch).toBeUndefined();
    // The same switch then runs again from the source and completes.
    expect((await r.run()).target).toBe('wi_b');
    expect(r.state().sessionId).toBe('sess-new-2');
  });

  it('a readback that stays unknown changes nothing; a later pass settles it',async()=>{
    const r=relay();
    let atEnter:any;
    const real=r.k.control.getMockImplementation()!;
    r.k.control.mockImplementation(async(request:any)=>{
      const answer=await real(request);
      if(request.phase==='enter'&&!atEnter)atEnter=r.snapshot();
      return answer;
    });
    await r.run();
    r.crashWith(atEnter);
    r.k.failReadback(3);
    expect(await r.settle()).toBeUndefined();
    expect(r.state().pendingWiSwitch).toBeDefined();
    expect(r.state().sessionId).toBe('sess-new-1');
    expect(await r.settle()).toMatchObject({settled:'entered'});
    expect(r.state().sessionId).toBe('sess-new-1');
  });

  it('an enter whose answer stays unknown keeps the record at the source, and the pass settles it on the target',async()=>{
    const r=relay();
    const real=r.k.control.getMockImplementation()!;
    let lost=true;
    r.k.control.mockImplementation(async(request:any)=>{
      const answer=await real(request);
      // The enter committed but no answer arrives, and the readback times out while the core is slow.
      if(lost&&request.phase!=='leave')return {status:'unknown',error:{code:'native_channel_timeout'}};
      return answer;
    });
    const outcome=await r.run();
    expect(outcome.retained).toBe(true);
    expect(r.state().sessionId).toBe('sess-a');
    expect(r.state().pendingWiSwitch.dest.sessionId).toBe('sess-new-1');
    lost=false;
    const reply=vi.fn(async()=>{});
    expect(await settleWiSwitch({poolKey:'tg:1',runtime:r.runtime,stateManager:r.stateManager,acquire:r.acquire,
      reply,control:r.k.control as any})).toBe(true);
    expect(r.state().sessionId).toBe('sess-new-1');
    expect(r.live().sessionId).toBe('sess-new-1');
    expect(reply).toHaveBeenCalledWith(expect.stringContaining('Agent 自主切换：中断的切换已按原请求读回：已切换到 wi_b'));
    expect(r.runtime.sendIdempotent.mock.calls[0][1].content).toContain('接手 wi_b');
  });

  it('a turn that starts while the readback runs is never killed; the record waits for a later pass',async()=>{
    const r=relay();
    const real=r.k.control.getMockImplementation()!;
    let lost=true;
    r.k.control.mockImplementation(async(request:any)=>{
      const answer=await real(request);
      if(lost&&request.phase!=='leave')return {status:'unknown',error:{code:'native_channel_timeout'}};
      return answer;
    });
    expect((await r.run()).retained).toBe(true);  // Committed, but every answer was lost: back on sess-a.
    expect(r.state().sessionId).toBe('sess-a');
    lost=false;
    let resume:()=>void=()=>{};
    const paused=new Promise<void>(resolve=>{resume=resolve;});
    let reading:()=>void=()=>{};
    const inReadback=new Promise<void>(resolve=>{reading=resolve;});
    r.k.control.mockImplementation(async(request:any)=>{
      if(request.phase==='readback'){reading();await paused;}
      return real(request);
    });
    const settling=r.settle();
    await inReadback;
    // A human message arrives through the channel's real acquisition and starts a turn.
    const proc=await r.inbound();
    expect(proc.sessionId).toBe('sess-a');
    r.live().state='busy';
    r.runtime.kill.mockClear();
    resume();
    expect(await settling).toBeUndefined();
    expect(r.runtime.kill).not.toHaveBeenCalled();
    expect(r.live().sessionId).toBe('sess-a');
    expect(r.state().pendingWiSwitch).toMatchObject({dest:{sessionId:'sess-new-1'}});
    expect(r.state().pendingWiSwitch.phase).toBeUndefined();
    // When the turn has ended, a later pass settles on the target session that entered.
    r.live().state='ready';
    expect(await r.settle()).toMatchObject({settled:'entered'});
    expect(r.state().sessionId).toBe('sess-new-1');
  });

  it('a restart after enter settled but before its continuation was admitted delivers it again, then retires the notice',async()=>{
    const dir=mkdtempSync(join(tmpdir(),'agent-switch-recovery-'));
    const ref='e'.repeat(64);
    const notice=join(dir,ref+'.json');
    writeFileSync(notice,JSON.stringify({schema:'harness.agent-switch-request/v1',ref,wi:'wi_a',target:'wi_b',
      request_id:'ask',tenant:'tg:1',source:{},source_native_id:'sess-a'}));
    chmodSync(notice,0o600);
    const r=relay();
    // The first continuation never reaches admission: Relay stops while the chat notice is still on its way.
    r.runtime.sendIdempotent.mockImplementationOnce(async()=>{throw new Error('relay_exit');});
    const reply=vi.fn(()=>new Promise<void>(()=>{}));
    void runAgentSwitch({ref,target:'wi_b',poolKey:'tg:1',runtime:r.runtime,stateManager:r.stateManager,
      acquire:r.acquire,reply,control:r.k.control as any,newSessionId:()=>'sess-new-1'});
    await vi.waitFor(()=>expect(r.runtime.sendIdempotent).toHaveBeenCalledTimes(1));
    r.restart();
    expect(r.state()).toMatchObject({sessionId:'sess-new-1',pendingWiSwitch:{phase:'entered',agent:true}});
    const start=vi.fn();
    expect(readAgentSwitchRequests(dir)).toHaveLength(1);
    // The notice waits while the switch record is open; it is not taken for a moved-on conversation.
    await adoptAgentSwitchRequests({dir,stateManager:r.stateManager,runtime:r.runtime,start});
    expect(existsSync(notice)).toBe(true);
    const notified=vi.fn(async()=>{});
    expect(await settleWiSwitch({poolKey:'tg:1',runtime:r.runtime,stateManager:r.stateManager,acquire:r.acquire,
      reply:notified,control:r.k.control as any})).toBe(true);
    const [, message, key]=r.runtime.sendIdempotent.mock.calls.at(-1);
    expect(key).toBe('operation_v1_wi_switch_'+ref);
    expect(message.content).toContain('接手 wi_b');
    expect(r.state().pendingWiSwitch).toBeUndefined();
    expect(notified).toHaveBeenCalledWith(expect.stringContaining('wi_b 的接续提示重新交给 Agent'));
    await adoptAgentSwitchRequests({dir,stateManager:r.stateManager,runtime:r.runtime,start});
    expect(start).not.toHaveBeenCalled();
    expect(existsSync(notice)).toBe(false);
  });
});
