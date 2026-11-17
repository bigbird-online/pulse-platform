import {describe,it,expect,vi} from 'vitest';
import {rolloverNativeSession} from '../src/native-session-rollover.js';
import type {AgentConfig,StateManager,PersistedSession} from '../src/types.js';
import type {CommandRuntime} from '../src/commands.js';

function fixture(){
  const poolKey='human:pulse',ref='a'.repeat(64);
  const state:PersistedSession={poolKey,agentId:'pulse',runtime:'pi',sessionId:'source',
    status:'active',lastActivity:1,turnCount:1,maxTurns:100};
  let live:any={sessionId:'source',state:'ready',runtime:'pi',process:{pid:101}};
  const dispatched=new Set<string>();
  const runtime={get:vi.fn(()=>live),kill:vi.fn(async()=>{live=undefined;}),
    spawn:vi.fn(async(config:any)=>{live={sessionId:config.sessionId,state:'ready',runtime:config.runtime,process:{pid:102}};return live;}),
    sendIdempotent:vi.fn(async(_key:string,_message:any,id:string)=>{dispatched.add(id);})};
  const stateManager={getSession:()=>state,updateSession:(_key:string,patch:any)=>Object.assign(state,patch),
    flushStrict:vi.fn()} as unknown as StateManager;
  let prepared=false,accepted=false;
  const control=vi.fn(async(request:any)=>{
    if(request.stage==='prepare')prepared=true;
    if(request.stage==='accept') {expect(prepared).toBe(true);expect(live.sessionId).toBe('target');accepted=true;}
    return {status:'read_only',session_handover:{ref,wi:'wi_fixture',source_native_id:'source',
      target_session_id:'target',runtime:'pi',prepared,accepted,target_session_exists:live?.sessionId==='target',target_bootstrap_observed:dispatched.size>0}};
  });
  const input={poolKey,ref,agent:{id:'pulse',name:'Pulse',emoji:'P',workspace:'/workspace',runtime:'pi'} as AgentConfig,
    runtime:runtime as unknown as CommandRuntime,stateManager,channel:'telegram' as const,control};
  return {input,runtime,control,state,dispatched,get live(){return live;}};
}

describe('core-owned native session rollover transport',()=>{
  it('prepares before stop, verifies actual successor, and redelivery reuses the original handover',async()=>{
    const f=fixture();
    expect((await rolloverNativeSession(f.input)).session_handover?.accepted).toBe(true);
    expect(f.state.sessionId).toBe('target');
    expect(f.state.pendingNativeRollover).toBeUndefined();
    expect(f.state.nativeSessionHistory?.[0].sessionId).toBe('source');
    const calls=f.control.mock.calls.map(([r])=>r.stage);
    expect(calls).toEqual(['inspect','prepare','accept','inspect']);
    expect(f.runtime.spawn.mock.calls[0][0].nativeHandoverRef).toBe(f.input.ref);
    await rolloverNativeSession(f.input);
    expect(f.runtime.kill).toHaveBeenCalledTimes(1);
    expect(f.runtime.spawn).toHaveBeenCalledTimes(1);
    expect(f.dispatched.size).toBe(1);
  });

  it('an unknown source commit preserves the original process and the same request',async()=>{
    const f=fixture();
    const control=vi.fn(async()=>({status:'unknown'}));
    const input={...f.input,control};
    await rolloverNativeSession(input);await rolloverNativeSession(input);
    expect(f.runtime.kill).not.toHaveBeenCalled();expect(f.runtime.spawn).not.toHaveBeenCalled();
    expect(f.state.pendingNativeRollover?.ref).toBe(f.input.ref);
    expect(new Set(control.mock.calls.map((args:any)=>(args[0] as any).ref)).size).toBe(1);
  });

  it('a failed physical spawn keeps one allocation and recovers without repeating the source decision',async()=>{
    const f=fixture();
    f.runtime.spawn.mockRejectedValueOnce(new Error('fixture launch failed'));
    await expect(rolloverNativeSession(f.input)).rejects.toThrow('fixture launch failed');
    expect(f.state.pendingNativeRollover?.targetSessionId).toBe('target');
    expect((await rolloverNativeSession({...f.input,ref:undefined})).session_handover?.accepted).toBe(true);
    expect(f.control.mock.calls.filter(([r])=>r.stage==='prepare')).toHaveLength(1);
    expect(f.runtime.spawn.mock.calls.map(([config])=>config.sessionId)).toEqual(['target','target']);
    expect(f.runtime.spawn.mock.calls.map(([config])=>config.resume)).toEqual([false,false]);
  });

  it('a bootstrap dispatch failure remains resumable even after acceptance has committed',async()=>{
    const f=fixture();
    f.runtime.sendIdempotent.mockRejectedValueOnce(new Error('dispatch unavailable'));
    await expect(rolloverNativeSession(f.input)).rejects.toThrow('dispatch unavailable');
    expect(f.state.pendingNativeRollover?.ref).toBe(f.input.ref);
    await rolloverNativeSession({...f.input,ref:undefined});
    expect(f.runtime.kill).toHaveBeenCalledTimes(1);
    expect(f.runtime.spawn).toHaveBeenCalledTimes(1);
    expect(f.dispatched.size).toBe(1);
  });
});

describe('native session core answer',()=>{
  it('never logs stderr text, even a single token line, and logs only controlled fields',async()=>{
    const {readSessionControl}=await import('../src/native-session-rollover.js');
    const log=vi.spyOn(console,'error').mockImplementation(()=>{});
    expect(readSessionControl('inspect',null,'{"status":"read_only"}','ghp_fixturesecret\n')).toEqual({status:'read_only'});
    expect(readSessionControl('inspect',Object.assign(new Error('exit'),{code:2}),
      JSON.stringify({status:'rejected',error:{code:'source_unavailable',exception_type:'AttributeError'}}),'Traceback /Users/x\n'))
      .toMatchObject({status:'rejected'});
    expect(log.mock.calls.at(-1)?.[0]).toEqual({msg:'Native session core diagnostic',stage:'inspect',code:'source_unavailable',
      exception:'AttributeError',exitCode:2,signal:undefined,killed:false,stdoutBytes:expect.any(Number),stderrBytes:19});
    readSessionControl('accept',null,JSON.stringify({status:'rejected',error:{code:'ghp_Secret',exception_type:'ghp_fixturesecret'}}),'');
    expect(log.mock.calls.at(-1)?.[0]).toMatchObject({code:undefined,exception:undefined});
    expect(JSON.stringify(log.mock.calls)).not.toMatch(/ghp_|Secret|\/Users|Traceback/);
    const overflow=Object.assign(new Error('stdout maxBuffer length exceeded'),{code:'ERR_CHILD_PROCESS_STDIO_MAXBUFFER'});
    expect(readSessionControl('prepare',overflow,'{"status":"local_comm','')).toEqual({status:'unknown',error:{code:'native_session_result_unknown'}});
    expect(log.mock.calls.at(-1)?.[0]).toMatchObject({stage:'prepare',code:'native_session_result_unknown',exitCode:'ERR_CHILD_PROCESS_STDIO_MAXBUFFER'});
    log.mockRestore();
  });
});

describe('native session rollover after a human /reset',()=>{
  function afterReset(prepared:boolean,{standInSessionId='reset' as string|null,current='reset'}={}){
    const f=fixture();
    Object.assign(f.state,{sessionId:current,forcedSessionResets:[{sessionId:'source',resetAt:'2026-11-12T01:31:49Z'}],
      pendingNativeRollover:{ref:f.input.ref,sourceSessionId:'source',runtime:'pi',...(standInSessionId?{standInSessionId}:{})}});
    let live:any={sessionId:current,state:'ready',runtime:'pi',process:{pid:103}};
    f.runtime.get.mockImplementation(()=>live);
    f.runtime.kill.mockImplementation(async()=>{live=undefined;});
    f.runtime.spawn.mockImplementation(async(config:any)=>{live={sessionId:config.sessionId,state:'ready',runtime:config.runtime,process:{pid:102}};return live;});
    let accepted=false;
    const handover=()=>({ref:f.input.ref,wi:'wi_fixture',source_native_id:'source',target_session_id:'target',runtime:'pi',
      prepared,accepted,target_session_exists:live?.sessionId==='target',target_bootstrap_observed:f.dispatched.size>0});
    const control=vi.fn(async(request:any):Promise<any>=>{
      if(request.stage==='accept')accepted=true;
      return {status:'read_only',session_handover:handover()};
    });
    return {...f,control,handover,input:{...f.input,ref:undefined,control}};
  }

  it('carries a prepared original rollover past the session that the reset made',async()=>{
    const f=afterReset(true);
    expect((await rolloverNativeSession(f.input)).session_handover?.accepted).toBe(true);
    expect(f.runtime.kill).toHaveBeenCalledTimes(1);
    expect(f.state.sessionId).toBe('target');
    expect(f.state.pendingNativeRollover).toBeUndefined();
    expect(f.state.nativeSessionHistory?.at(-1)?.sessionId).toBe('reset');
    expect(f.control.mock.calls.map(([r])=>r.stage)).toEqual(['inspect','accept','inspect']);
  });

  it('retires an unprepared rollover only when the ledger reads back no source decision',async()=>{
    const f=afterReset(false);
    expect((await rolloverNativeSession(f.input)).retiredUnprepared).toBe(true);
    expect(f.state.pendingNativeRollover).toBeUndefined();
    expect(f.state.sessionId).toBe('reset');
    expect(f.runtime.kill).not.toHaveBeenCalled();expect(f.runtime.spawn).not.toHaveBeenCalled();
  });

  it.each([
    ['rejected',{status:'rejected',error:{code:'source_unavailable'}}],
    ['unknown',{status:'unknown',error:{code:'native_session_result_unknown'}}],
  ])('keeps the original plan when the ledger read is %s',async(_name,answer)=>{
    const f=afterReset(false);
    f.control.mockImplementation(async()=>answer);
    await rolloverNativeSession(f.input);
    expect(f.state.pendingNativeRollover?.ref).toBe('a'.repeat(64));
    expect(f.runtime.kill).not.toHaveBeenCalled();
  });

  it('never stops a session that the recorded reset did not make',async()=>{
    for(const options of [{standInSessionId:null},{standInSessionId:'reset',current:'unrelated'}]){
      const f=afterReset(true,options);
      await expect(rolloverNativeSession(f.input)).rejects.toThrow('native_rollover_conversation_changed');
      expect(f.runtime.kill).not.toHaveBeenCalled();
      expect(f.state.pendingNativeRollover?.ref).toBe('a'.repeat(64));
    }
  });
});

describe('the WI422 rollover prepared before the upgrade',()=>{
  // The original prepare committed before /reset, which saved no stand-in or target in the plan.
  const ref='b'.repeat(64),source='original',reset='reset',target='reserved-successor';
  function production({current=reset,liveCurrent=false,lastActivity=Date.parse('2026-11-12T01:31:49.367Z')+798}={}){
    const state:any={poolKey:'owner:pulse',agentId:'pulse',runtime:'cc',sessionId:current,lastActivity,status:'active',
      turnCount:0,maxTurns:100,
      forcedSessionResets:[{sessionId:'earlier-session',resetAt:'2026-10-05T05:35:45.776Z'},
        {sessionId:source,resetAt:'2026-11-12T01:31:49.367Z'}],
      pendingNativeRollover:{ref,sourceSessionId:source,runtime:'cc'}};
    let live:any=liveCurrent?{sessionId:current,state:'ready',runtime:'cc',process:{pid:103}}:undefined;
    const dispatched=new Set<string>();
    const runtime={get:vi.fn(()=>live),kill:vi.fn(async()=>{live=undefined;}),
      spawn:vi.fn(async(config:any)=>{live={sessionId:config.sessionId,state:'ready',runtime:config.runtime,process:{pid:104}};return live;}),
      sendIdempotent:vi.fn(async(_key:string,_message:any,id:string)=>{dispatched.add(id);})};
    const stateManager={getSession:()=>state,updateSession:(_key:string,patch:any)=>Object.assign(state,patch),
      flushStrict:vi.fn()} as unknown as StateManager;
    let accepted=false;
    const control=vi.fn(async(request:any)=>{
      if(request.stage==='accept'){expect(live.sessionId).toBe(target);accepted=true;}
      return {status:'read_only',session_handover:{ref,wi:'wi_20261015_422',source_native_id:source,target_session_id:target,
        runtime:'cc',prepared:true,accepted,target_session_exists:live?.sessionId===target,target_bootstrap_observed:dispatched.size>0}};
    });
    const input={poolKey:'owner:pulse',agent:{id:'pulse',name:'Pulse',emoji:'P',workspace:'/workspace',runtime:'cc'} as AgentConfig,
      runtime:runtime as unknown as CommandRuntime,stateManager,channel:'telegram' as const,control};
    return {input,runtime,control,state,dispatched,stateManager};
  }

  it('replaces nothing on its own: the pre-upgrade /reset left no binding of old and new session',async()=>{
    const f=production({liveCurrent:true});
    await expect(rolloverNativeSession(f.input)).rejects.toThrow('native_rollover_conversation_changed');
    expect(f.runtime.kill).not.toHaveBeenCalled();expect(f.runtime.spawn).not.toHaveBeenCalled();
    expect(f.state.pendingNativeRollover).toEqual({ref,sourceSessionId:source,runtime:'cc',targetSessionId:target});
  });

  it('does not take a session opened by /new 30 s after the reset',async()=>{
    const f=production({current:'opened-by-new',liveCurrent:true,lastActivity:Date.parse('2026-11-12T01:32:19.367Z')});
    await expect(rolloverNativeSession(f.input)).rejects.toThrow('native_rollover_conversation_changed');
    expect(f.runtime.kill).not.toHaveBeenCalled();
  });

  it.each([false,true])('a human /wi rollover names the stand-in; the original request then accepts its reserved successor (live: %s)',
    async liveCurrent=>{
    const f=production({liveCurrent});
    const result=await rolloverNativeSession({...f.input,resume:{ref,wi:'wi_20261015_422',isCurrent:()=>true}});
    expect(result.session_handover?.accepted).toBe(true);
    expect(f.control.mock.calls.map(([r])=>r.stage)).toEqual(['inspect','accept','inspect']);
    expect(f.control.mock.calls.every(([r])=>r.ref===ref)).toBe(true);
    expect(f.runtime.kill).toHaveBeenCalledTimes(liveCurrent?1:0);
    expect(f.runtime.spawn.mock.calls.map(([c])=>[c.sessionId,c.nativeHandoverRef])).toEqual([[target,ref]]);
    expect(f.state.sessionId).toBe(target);
    expect(f.state.pendingNativeRollover).toBeUndefined();
    expect(f.state.nativeSessionHistory.at(-1)).toMatchObject({sessionId:reset,rolloverRef:ref});
    expect([...f.dispatched]).toEqual(['operation_v1_native_rollover_'+ref]);
  });

  it('never names a session that holds or enters a WI',async()=>{
    for(const patch of [{activeWorkItemRuntimeBinding:{workItemId:'wi_other',sessionId:reset,runtime:'cc'}},
      {pendingWorkItemEntry:{workItemId:'wi_other',sessionId:reset,state:'prepared'}}]){
      const f=production();Object.assign(f.state,patch);
      await expect(rolloverNativeSession({...f.input,resume:{ref,wi:'wi_20261015_422',isCurrent:()=>true}}))
        .rejects.toThrow('native_rollover_persisted_conversation_changed');
      expect(f.state.pendingNativeRollover.standInSessionId).toBeUndefined();
    }
  });

  it('does not bind or spawn the successor if a newer input arrives while stopping the stand-in',async()=>{
    const f=production({liveCurrent:true});
    let current=true;
    f.runtime.kill.mockImplementationOnce(async()=>{current=false;});
    await expect(rolloverNativeSession({...f.input,resume:{ref,wi:'wi_20261015_422',isCurrent:()=>current}}))
      .rejects.toThrow('superseded');
    expect(f.state.sessionId).toBe(reset);
    expect(f.state.pendingNativeRollover.ref).toBe(ref);
    expect(f.runtime.spawn).not.toHaveBeenCalled();
    expect(f.control.mock.calls.map(([request])=>request.stage)).toEqual(['inspect']);
  });
});

describe('an Agent-requested native session rollover',()=>{
  it('runs the same fenced handover and keeps the agent origin on the plan',async()=>{
    const f=fixture();
    const seen:any[]=[];
    const control=vi.fn(async(request:any)=>{seen.push(f.state.pendingNativeRollover?.origin);return f.input.control(request);});
    expect((await rolloverNativeSession({...f.input,control,origin:'agent'})).session_handover?.accepted).toBe(true);
    expect(seen[0]).toBe('agent');
    expect(f.state.sessionId).toBe('target');
    expect(f.state.pendingNativeRollover).toBeUndefined();
  });

  it('a core refusal of the request clears the plan and leaves the source conversation running',async()=>{
    const f=fixture();
    const control=vi.fn(async(request:any)=>request.stage==='prepare'
      ?{status:'rejected',error:{code:'domain_conflict',retryable:false},session_handover:{prepared:false}}
      :{status:'rejected',error:{code:'source_unavailable'}});
    const result=await rolloverNativeSession({...f.input,control:control as any,origin:'agent'});
    expect(result.retiredUnprepared).toBe(true);
    expect(f.state.pendingNativeRollover).toBeUndefined();
    expect(f.runtime.kill).not.toHaveBeenCalled();
    expect(f.state.sessionId).toBe('source');
  });

  it.each([['no instance',undefined],['a dead instance',{sessionId:'source',state:'dead'}]])(
    'after a Relay restart leaves the persisted source with %s, recovers it and runs the fenced handover',async(_name,gone)=>{
    const f=fixture();
    f.runtime.get.mockReturnValueOnce(gone as any);
    const result=await rolloverNativeSession({...f.input,origin:'agent',sourceNativeId:'source'});
    expect(result.session_handover?.accepted).toBe(true);
    expect(f.runtime.spawn.mock.calls.map(([config])=>config.sessionId)).toEqual(['source','target']);
    expect(f.control.mock.calls.find(([r])=>r.stage==='prepare')?.[0].pid).toBe(102);
    expect(f.state.sessionId).toBe('target');
  });

  it('a persisted session that no longer names the source starts nothing',async()=>{
    const f=fixture();
    f.runtime.get.mockReturnValueOnce(undefined);
    await expect(rolloverNativeSession({...f.input,origin:'agent',sourceNativeId:'other'}))
      .rejects.toThrow('native_rollover_requires_idle_original_session');
    expect(f.state.pendingNativeRollover).toBeUndefined();
    expect(f.runtime.spawn).not.toHaveBeenCalled();
    expect(f.control).not.toHaveBeenCalled();
  });

  it('a retryable refusal keeps the plan for the same request',async()=>{
    const f=fixture();
    const control=vi.fn(async()=>({status:'rejected',error:{code:'ledger_unavailable',retryable:true}}));
    await rolloverNativeSession({...f.input,control:control as any,origin:'agent'});
    expect(f.state.pendingNativeRollover?.ref).toBe(f.input.ref);
    expect(f.runtime.kill).not.toHaveBeenCalled();
  });
});
