import {describe,it,expect,vi} from 'vitest';
import {mkdtempSync,writeFileSync,existsSync,chmodSync} from 'node:fs';
import {tmpdir} from 'node:os';
import {join} from 'node:path';
import {adoptAgentRolloverRequests,agentRolloverRequestDir,readAgentRolloverRequests} from '../src/agent-rollover-request.js';
import type {StateManager,PersistedSession} from '../src/types.js';

const REF='b'.repeat(64);
function rig(overrides:Partial<PersistedSession>={},live:any={sessionId:'source',state:'ready'}){
  const dir=mkdtempSync(join(tmpdir(),'agent-rollover-'));
  const path=join(dir,REF+'.json');
  writeFileSync(path,JSON.stringify({schema:'harness.agent-rollover-request/v1',ref:REF,wi:'wi_fixture',
    request_id:'ask',tenant:'human:pulse',source:{},source_native_id:'source'}));
  chmodSync(path,0o600);
  const session:PersistedSession={poolKey:'human:pulse',agentId:'pulse',runtime:'cc',sessionId:'source',
    status:'active',lastActivity:1,turnCount:1,maxTurns:100,...overrides};
  const stateManager={getSession:(key:string)=>key==='human:pulse'?session:undefined} as unknown as StateManager;
  const runtime:{get:()=>any}={get:()=>live};
  return {dir,path,session,stateManager,runtime};
}

describe('Agent rollover requests left by the core',()=>{
  it('the notice directory sits beside the core human-input custody',()=>{
    expect(agentRolloverRequestDir({PULSE_CORE_HUMAN_INPUTS:'/tmp/host/human-inputs'} as NodeJS.ProcessEnv))
      .toBe('/tmp/host/native-session-handovers/agent-requests');
  });

  it('starts an idle current source once and retires the notice after the successor holds the WI',async()=>{
    const r=rig();
    const start=vi.fn(async()=>{r.session.nativeSessionHistory=[{sessionId:'source',runtime:'cc',rolloverRef:REF}];
      r.session.sessionId='target';return {};});
    expect(await adoptAgentRolloverRequests({dir:r.dir,stateManager:r.stateManager,runtime:r.runtime as any,start}))
      .toEqual([REF]);
    expect(start).toHaveBeenCalledWith({ref:REF,wi:'wi_fixture',tenant:'human:pulse',sourceNativeId:'source'});
    expect(existsSync(r.path)).toBe(false);
  });

  it('waits while the source turn is busy and while another rollover is pending',async()=>{
    const busy=rig({}, {sessionId:'source',state:'busy'});
    const start=vi.fn();
    await adoptAgentRolloverRequests({dir:busy.dir,stateManager:busy.stateManager,runtime:busy.runtime as any,start});
    const pending=rig({pendingNativeRollover:{ref:'c'.repeat(64),sourceSessionId:'source',runtime:'cc'}});
    await adoptAgentRolloverRequests({dir:pending.dir,stateManager:pending.stateManager,runtime:pending.runtime as any,start});
    expect(start).not.toHaveBeenCalled();
    expect(existsSync(busy.path)&&existsSync(pending.path)).toBe(true);
  });

  it.each([['no instance',undefined],['a dead instance',{sessionId:'source',state:'dead'}]])(
    'starts the rollover when a Relay restart leaves the idle persisted source with %s',async(_name,gone)=>{
    const r=rig();
    r.runtime.get=()=>gone;
    const start=vi.fn(async()=>({}));
    await adoptAgentRolloverRequests({dir:r.dir,stateManager:r.stateManager,runtime:r.runtime as any,start});
    await adoptAgentRolloverRequests({dir:r.dir,stateManager:r.stateManager,runtime:r.runtime as any,start});
    expect(start).toHaveBeenCalledWith({ref:REF,wi:'wi_fixture',tenant:'human:pulse',sourceNativeId:'source'});
    expect(existsSync(r.path)).toBe(true); // Kept until the successor holds the WI.
  });

  it('with no instance, a persisted session that names another source is retired without a start',async()=>{
    const r=rig({sessionId:'later'});
    r.runtime.get=()=>undefined;
    const start=vi.fn();
    await adoptAgentRolloverRequests({dir:r.dir,stateManager:r.stateManager,runtime:r.runtime as any,start});
    expect(start).not.toHaveBeenCalled();
    expect(existsSync(r.path)).toBe(false);
  });

  it('a notice for a conversation that already moved on is retired without a start',async()=>{
    const r=rig({sessionId:'later'},{sessionId:'later',state:'ready'});
    const start=vi.fn();
    await adoptAgentRolloverRequests({dir:r.dir,stateManager:r.stateManager,runtime:r.runtime as any,start});
    expect(start).not.toHaveBeenCalled();
    expect(existsSync(r.path)).toBe(false);
  });

  it('a failed start keeps the notice for the next pass, a refusal retires it',async()=>{
    const r=rig();
    await adoptAgentRolloverRequests({dir:r.dir,stateManager:r.stateManager,runtime:r.runtime as any,
      start:async()=>{throw new Error('native_rollover_source_busy');}});
    expect(existsSync(r.path)).toBe(true);
    await adoptAgentRolloverRequests({dir:r.dir,stateManager:r.stateManager,runtime:r.runtime as any,
      start:async()=>({retiredUnprepared:true})});
    expect(existsSync(r.path)).toBe(false);
  });

  it('ignores notices that are not private or not well formed',()=>{
    const r=rig();
    chmodSync(r.path,0o644);
    expect(readAgentRolloverRequests(r.dir)).toEqual([]);
  });

  it('waits while an unfinished /wi switch owns the conversation',async()=>{
    const r=rig({pendingWiSwitch:{ref:'e'.repeat(64),target:'wi_b',resumed:false,at:'t',
      origin:{sessionId:'source'},dest:{sessionId:'target'}} as never});
    const start=vi.fn();
    expect(await adoptAgentRolloverRequests({dir:r.dir,stateManager:r.stateManager,runtime:r.runtime as any,start})).toEqual([]);
    expect(start).not.toHaveBeenCalled();
    expect(existsSync(r.path)).toBe(true);
  });
});
