import {describe,it,expect,vi} from 'vitest';
import {mkdtempSync,writeFileSync,existsSync,chmodSync} from 'node:fs';
import {tmpdir} from 'node:os';
import {join} from 'node:path';
import {adoptAgentSwitchRequests,agentSwitchRequestDir,readAgentSwitchRequests} from '../src/agent-switch-request.js';
import {runAgentSwitch} from '../src/native-work-item-command.js';
import type {StateManager,PersistedSession} from '../src/types.js';

const REF='d'.repeat(64);
function rig(overrides:Partial<PersistedSession>={},live:any={sessionId:'source',state:'ready'}){
  const dir=mkdtempSync(join(tmpdir(),'agent-switch-'));
  const path=join(dir,REF+'.json');
  writeFileSync(path,JSON.stringify({schema:'harness.agent-switch-request/v1',ref:REF,wi:'wi_a',target:'wi_b',
    request_id:'ask',tenant:'human:pulse',source:{},source_native_id:'source'}));
  chmodSync(path,0o600);
  const session:any={poolKey:'human:pulse',agentId:'pulse',runtime:'cc',sessionId:'source',
    status:'active',lastActivity:1,turnCount:1,maxTurns:100,...overrides};
  const stateManager={getSession:(key:string)=>key==='human:pulse'?session:undefined,
    updateSession:(_k:string,p:any)=>Object.assign(session,p),flushStrict:vi.fn()} as unknown as StateManager;
  let current=live;
  const runtime={get:()=>current,kill:vi.fn(async()=>{current=undefined;})};
  return {dir,path,session,stateManager,runtime};
}

describe('Agent switch requests left by the core',()=>{
  it('the notice directory sits beside the core human-input custody',()=>{
    expect(agentSwitchRequestDir({PULSE_CORE_HUMAN_INPUTS:'/tmp/host/human-inputs'} as NodeJS.ProcessEnv))
      .toBe('/tmp/host/native-session-handovers/agent-switches');
  });

  it('runs an idle current source once and retires the notice',async()=>{
    const r=rig();
    expect(readAgentSwitchRequests(r.dir)).toEqual([{ref:REF,wi:'wi_a',target:'wi_b',tenant:'human:pulse',sourceNativeId:'source'}]);
    const start=vi.fn(async()=>({text:'done'}));
    expect(await adoptAgentSwitchRequests({dir:r.dir,stateManager:r.stateManager,runtime:r.runtime as any,start})).toEqual([REF]);
    expect(start).toHaveBeenCalledTimes(1);
    expect(existsSync(r.path)).toBe(false);
  });

  it('waits while the source turn is busy or a rollover is pending, and keeps a notice whose run failed',async()=>{
    const start=vi.fn(async()=>{throw new Error('agent_switch_source_unavailable');});
    const busy=rig({}, {sessionId:'source',state:'busy'});
    await adoptAgentSwitchRequests({dir:busy.dir,stateManager:busy.stateManager,runtime:busy.runtime as any,start});
    const pending=rig({pendingNativeRollover:{ref:'c'.repeat(64),sourceSessionId:'source',runtime:'cc'}});
    await adoptAgentSwitchRequests({dir:pending.dir,stateManager:pending.stateManager,runtime:pending.runtime as any,start});
    expect(start).not.toHaveBeenCalled();
    const failing=rig();
    const log=vi.spyOn(console,'error').mockImplementation(()=>{});
    try {await adoptAgentSwitchRequests({dir:failing.dir,stateManager:failing.stateManager,runtime:failing.runtime as any,start});}
    finally {log.mockRestore();}
    expect(start).toHaveBeenCalledTimes(1);
    expect(existsSync(busy.path)&&existsSync(pending.path)&&existsSync(failing.path)).toBe(true);
  });

  it('a conversation already on another session retires the notice without a run',async()=>{
    const r=rig({}, {sessionId:'another',state:'ready'});
    const start=vi.fn();
    expect(await adoptAgentSwitchRequests({dir:r.dir,stateManager:r.stateManager,runtime:r.runtime as any,start})).toEqual([]);
    expect(start).not.toHaveBeenCalled();
    expect(existsSync(r.path)).toBe(false);
  });

  it('waits while the conversation has an unsettled switch, then runs once it returned to the source',async()=>{
    const r=rig({pendingWiSwitch:{ref:REF,target:'wi_b',at:'t',resumed:false,agent:true,
      origin:{sessionId:'source',runtime:'cc'},dest:{sessionId:'target-session',runtime:'cc'}}});
    const start=vi.fn(async()=>({text:'done'}));
    await adoptAgentSwitchRequests({dir:r.dir,stateManager:r.stateManager,runtime:r.runtime as any,start});
    expect(start).not.toHaveBeenCalled();
    expect(existsSync(r.path)).toBe(true);
    r.session.pendingWiSwitch=undefined;  // settleInterruptedSwitch read it back as not entered.
    await adoptAgentSwitchRequests({dir:r.dir,stateManager:r.stateManager,runtime:r.runtime as any,start});
    expect(start).toHaveBeenCalledTimes(1);
    expect(existsSync(r.path)).toBe(false);
  });

  it('keeps the notice when the run left it unsettled',async()=>{
    for(const leave of ['retained','pending'] as const){
      const r=rig();
      const start=vi.fn(async()=>{
        if(leave==='pending')r.session.pendingWiSwitch={ref:REF,target:'wi_b',at:'t',resumed:false,
          origin:{sessionId:'source'},dest:{sessionId:'target'}};
        return leave==='retained'?{text:'unknown',retained:true}:{text:'unknown'};
      });
      await adoptAgentSwitchRequests({dir:r.dir,stateManager:r.stateManager,runtime:r.runtime as any,start});
      expect(existsSync(r.path)).toBe(true);
    }
  });
});

/** One chat with a live source process; the kernel answers both phases. */
function conversation(options:{known?:boolean;live?:boolean}={}){
  const state:any={poolKey:'human:pulse',agentId:'pulse',sessionId:'sess-a',runtime:'cc',status:'active',
    lastActivity:1,turnCount:1,maxTurns:100,
    ...(options.known?{workItemSessions:{wi_b:{unit:{sessionId:'sess-b',runtime:'cc'},at:'t'}}}:{})};
  let pid=100;
  let live:any=options.live===false?undefined:{sessionId:'sess-a',state:'ready',process:{pid}};
  const runtime:any={get:vi.fn(()=>live&&{...live}),kill:vi.fn(async()=>{live=undefined;}),
    sendIdempotent:vi.fn(async(_k:string,_m:any,_key:string,o?:any)=>{o?.onAccepted?.();}),
    send:vi.fn(async(_k:string,_m:any,o?:any)=>{o?.onAccepted?.();})};
  const acquire=vi.fn(async()=>{if(!live)live={sessionId:state.sessionId,state:'ready',process:{pid:++pid}};return 'human:pulse';});
  const stateManager:any={getSession:()=>state,updateSession:(_k:string,p:any)=>Object.assign(state,p),flushStrict:vi.fn()};
  const control=vi.fn(async(request:any)=>request.phase==='leave'
    ? {schema:'pulse.wi_switch/v1',phase:'leave',left:'wi_a',recorded:false,agent_request:true}
    : {status:'local_committed',work_item:{id:'wi_b',business_progress:'active'}});
  const reply=vi.fn(async()=>{});
  const run=()=>runAgentSwitch({ref:REF,target:'wi_b',poolKey:'human:pulse',runtime,stateManager,acquire,reply,
    control,newSessionId:()=>'sess-new'});
  return {state,runtime,acquire,control,reply,run};
}

describe('running an Agent switch after its turn',()=>{
  it('enters a new session, tells the chat and hands the Agent its continuation',async()=>{
    const c=conversation();
    const outcome=await c.run();
    expect(outcome.target).toBe('wi_b');
    expect(c.control.mock.calls.map(call=>[call[0].phase,call[0].ref])).toEqual([['leave',REF],['enter',REF]]);
    expect(c.state.sessionId).toBe('sess-new');
    expect(c.state.workItemSessions.wi_a.unit.sessionId).toBe('sess-a');
    expect(c.reply).toHaveBeenCalledWith(expect.stringContaining('Agent 自主切换：已切换到 wi_b（新会话）'));
    const [poolKey,message,key]=c.runtime.sendIdempotent.mock.calls[0];
    expect(poolKey).toBe('human:pulse');
    expect(key).toBe('operation_v1_wi_switch_'+REF);
    expect(message.content).toContain('接手 wi_b');
    expect(message.content).toContain('not a new human decision');
  });

  it('returns to the target\'s own session when it has one',async()=>{
    const c=conversation({known:true});
    const outcome=await c.run();
    expect(outcome.resumed).toBe(true);
    expect(c.state.sessionId).toBe('sess-b');
    expect(c.runtime.sendIdempotent.mock.calls[0][1].content).toContain('切回 wi_b');
  });

  it('resumes the source first when Relay has no instance of it',async()=>{
    const c=conversation({live:false});
    expect((await c.run()).target).toBe('wi_b');
    expect(c.acquire).toHaveBeenCalled();
    expect(c.control.mock.calls[0][0]).toMatchObject({phase:'leave',pid:101});
  });

  it('a refused enter returns to the source and only tells the chat',async()=>{
    const c=conversation();
    c.control.mockImplementation(async(request:any)=>request.phase==='leave'
      ? {schema:'pulse.wi_switch/v1',phase:'leave',left:'wi_a',recorded:false,agent_request:true}
      : {status:'rejected',error:{code:'authority_denied'}});
    const outcome=await c.run();
    expect(outcome.target).toBeUndefined();
    expect(c.state.sessionId).toBe('sess-a');
    expect(c.state.pendingWiSwitch).toBeUndefined();
    expect(c.reply).toHaveBeenCalledWith(expect.stringContaining('已回到原会话'));
    expect(c.runtime.sendIdempotent).not.toHaveBeenCalled();
  });
});
