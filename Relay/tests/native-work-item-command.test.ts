import {describe,it,expect,vi} from 'vitest';
import {mkdtempSync,rmSync,readFileSync,writeFileSync,realpathSync,symlinkSync} from 'node:fs';
import {join} from 'node:path';
import {tmpdir} from 'node:os';
import {allocatedWorkspaces,dispatchNativeWorkItemCommand,nodeWorkItemCore,readNativeChannel} from '../src/native-work-item-command.js';
import {recordHumanInput} from '../src/scl-human-decision.js';
import {nativeCoreFixture} from './helpers/native-work-item-core.js';
import {isolateHumanDecisions} from './helpers/scl-human-decision.js';
import type {AgentConfig} from '../src/types.js';
import type {CommandRuntime} from '../src/commands.js';
isolateHumanDecisions();

describe('runtime independent original channel commands',()=>{
  it('fails explicitly without an installed native core and never records or controls',async()=>{
    vi.stubEnv('RELAY_PI_RUNTIME_MANIFEST_PATH','');
    const control=vi.fn(),acquire=vi.fn(async()=>'human:pulse');
    await expect(dispatchNativeWorkItemCommand({agent:{id:'pulse',runtime:'pi'} as AgentConfig,
      source:{channel:'telegram',eventId:'no-core',humanRef:'human',message:'/wi status wi_original'},
      poolKey:'human:pulse',runtime:{get:()=>undefined} as unknown as CommandRuntime,acquire,control}))
      .rejects.toThrow('native_work_item_core_unavailable');
    expect(acquire).not.toHaveBeenCalled();expect(control).not.toHaveBeenCalled();
  });
  for(const channel of ['telegram','feishu'] as const)for(const runtime of ['cc','pi','codex'] as const){
    it(channel+' '+runtime+' executes once through core without a model message',async()=>{
      const dir=mkdtempSync(join(tmpdir(),'core-channel-'));
      try {
        vi.stubEnv('RELAY_PI_RUNTIME_MANIFEST_PATH',nativeCoreFixture(dir));
        const source={channel,eventId:'original',humanRef:'human',message:'/wi cancel wi_original'};
        const poolKey='human:pulse';
        const live={runtime,sessionId:'original',codexThreadId:runtime==='codex'?'native-codex':undefined,
          state:'ready',...(runtime!=='codex'?{process:{pid:101}}:{})};
        const pool={get:()=>live,send:vi.fn()};
        const control=vi.fn(async(request:any)=>({status:'local_committed',request_id:'channel.'+request.ref,
          work_item:{id:'wi_original',business_progress:'terminated'},result:{current_observation:{terminal:{termination:'cancelled'}}}}));
        const reply=vi.fn();
        const input={agent:{id:'pulse',runtime} as AgentConfig,source,poolKey,runtime:pool as unknown as CommandRuntime,
          acquire:async()=>poolKey,control,reply};
        await dispatchNativeWorkItemCommand(input);await dispatchNativeWorkItemCommand(input);
        expect(pool.send).not.toHaveBeenCalled();
        expect(control.mock.calls[0]).toEqual(control.mock.calls[1]);
        const request=control.mock.calls[0][0];
        expect(request).toEqual(runtime==='codex'?{ref:expect.any(String),native_id:'native-codex',tenant:poolKey}
          :{ref:expect.any(String),pid:101});
        expect(reply).toHaveBeenCalledWith(expect.stringContaining('cancelled'));
        const original=JSON.parse(readFileSync(join(process.env.PULSE_CORE_HUMAN_INPUTS!,request.ref+'.json'),'utf8'));
        expect(original.source).toEqual(source);
        input.acquire=async()=>{recordHumanInput({...source,eventId:'later',message:'new input'},poolKey);return poolKey;};
        await expect(dispatchNativeWorkItemCommand(input)).rejects.toThrow('superseded');
        expect(control).toHaveBeenCalledTimes(2);
      } finally {rmSync(dir,{recursive:true,force:true});}
    });
  }
  for(const [command,error] of [['/wi wi_draft',false],['/wi continue wi_draft',false],['/wi wi_draft',true]] as const)
  it('hands '+command+(error?' after a rejection':'')+' to the Agent only when the core accepted it',async()=>{
    const dir=mkdtempSync(join(tmpdir(),'core-handoff-'));
    try {
      vi.stubEnv('RELAY_PI_RUNTIME_MANIFEST_PATH',nativeCoreFixture(dir));
      const source={channel:'feishu' as const,eventId:'take-'+command+error,humanRef:'human',message:command};
      const poolKey='human:emi';
      const pool={get:()=>({runtime:'cc',sessionId:'s',state:'ready',process:{pid:101}}),send:vi.fn()};
      const control=vi.fn(async(request:any)=>error
        ?{status:'rejected',error:{code:'authority_denied',reason:'denied'}}
        :{status:'local_committed',request_id:'channel.'+request.ref,work_item:{id:'wi_draft',business_progress:'active'}});
      await dispatchNativeWorkItemCommand({agent:{id:'emi',runtime:'cc'} as AgentConfig,source,poolKey,
        runtime:pool as unknown as CommandRuntime,acquire:async()=>poolKey,control,reply:vi.fn()});
      if(error){expect(pool.send).not.toHaveBeenCalled();return;}
      expect(pool.send).toHaveBeenCalledTimes(1);
      const [key,message]=pool.send.mock.calls[0];
      expect(key).toBe(poolKey);
      expect(message.content).toMatch(/^接手 wi_draft：/);
      expect(message.content).toContain('Host human input reference: '+control.mock.calls[0][0].ref+'.');
    } finally {rmSync(dir,{recursive:true,force:true});}
  });
  it.each([
    {name:'same WI and observed bootstrap',wi:'wi_20261015_422',prepared:true,observed:true},
    {name:'a different WI',wi:'wi_other',prepared:true,observed:false},
    {name:'no committed preparation',wi:'wi_20261015_422',prepared:false,observed:false},
    {name:'bootstrap still unobserved',wi:'wi_20261015_422',prepared:true,observed:false},
  ])('reads back a re-issued /wi rollover with $name',async({wi,prepared,observed})=>{
    const dir=mkdtempSync(join(tmpdir(),'core-rollover-resume-'));
    try {
      vi.stubEnv('RELAY_PI_RUNTIME_MANIFEST_PATH',nativeCoreFixture(dir));
      const poolKey='human:pulse',ref='f'.repeat(64),originalWi='wi_20261015_422';
      const plan={ref,sourceSessionId:'source',runtime:'cc'};
      const state:any={poolKey,agentId:'pulse',sessionId:'reset',runtime:'cc',pendingNativeRollover:plan};
      let live:any={runtime:'cc',sessionId:'reset',state:'ready',process:{pid:101}};
      const stateManager={getSession:()=>state,updateSession:(_k:string,patch:any)=>Object.assign(state,patch),flushStrict:vi.fn()};
      const runtime={get:()=>live,kill:vi.fn(async()=>{live=undefined;}),
        spawn:vi.fn(async config=>(live={runtime:'cc',sessionId:config.sessionId,state:'ready',process:{pid:102}})),
        sendIdempotent:vi.fn(async()=>{})};
      const acquire=vi.fn(),reply=vi.fn();
      let accepted=false;
      const sessionControl=vi.fn(async request=>{
        expect(request.ref).toBe(ref);
        if(request.stage==='accept')accepted=true;
        return {status:'read_only',session_handover:{ref,wi:originalWi,source_native_id:'source',
          target_session_id:'target',runtime:'cc',prepared,accepted,target_session_exists:accepted,
          target_bootstrap_observed:accepted&&observed}};
      });
      await dispatchNativeWorkItemCommand({agent:{id:'pulse',runtime:'cc'} as AgentConfig,
        source:{channel:'telegram',eventId:'again',humanRef:'human',message:'/wi rollover '+wi},
        poolKey,runtime:runtime as unknown as CommandRuntime,stateManager:stateManager as never,
        acquire,sessionControl,reply});
      expect(acquire).not.toHaveBeenCalled();
      if(wi!==originalWi){
        expect(sessionControl.mock.calls.map(([r])=>r.stage)).toEqual(['inspect']);
        expect(runtime.kill).not.toHaveBeenCalled();
        expect(state.pendingNativeRollover).toEqual(plan);
        expect(reply).toHaveBeenCalledWith(expect.stringContaining('待接续的工作项是 '+originalWi));
      } else if(!prepared){
        expect(runtime.spawn).not.toHaveBeenCalled();
        expect(state.pendingNativeRollover).toBeUndefined();
        expect(reply).toHaveBeenCalledWith(expect.stringContaining('已清除旧轮换计划'));
      } else {
        expect(sessionControl.mock.calls.map(([r])=>r.stage)).toEqual(['inspect','accept','inspect']);
        expect(state.sessionId).toBe('target');
        expect(Boolean(state.pendingNativeRollover)).toBe(!observed);
        expect(reply).toHaveBeenCalledWith(expect.stringContaining(observed?'已在原 WI 接续':'后继接续尚待确认'));
      }
    } finally {rmSync(dir,{recursive:true,force:true});}
  });
  it('/wi switch leaves, starts the target session, enters it and briefs the new session',async()=>{
    const dir=mkdtempSync(join(tmpdir(),'core-switch-'));
    try {
      vi.stubEnv('RELAY_PI_RUNTIME_MANIFEST_PATH',nativeCoreFixture(dir));
      const poolKey='human:pulse';
      const state:any={poolKey,agentId:'pulse',sessionId:'old',runtime:'cc'};
      let live:any={runtime:'cc',sessionId:'old',state:'ready',process:{pid:101}};
      const pool={get:()=>live,send:vi.fn(),kill:vi.fn(async()=>{live=undefined;})};
      const acquire=async()=>{live=live??{runtime:'cc',sessionId:state.sessionId,state:'ready',process:{pid:202}};return poolKey;};
      const control=vi.fn(async(request:any)=>({status:'local_committed',
        work_item:{id:request.phase==='leave'?'wi_here':'wi_there',business_progress:'active'}}));
      await dispatchNativeWorkItemCommand({agent:{id:'pulse',runtime:'cc'} as AgentConfig,
        source:{channel:'telegram',eventId:'switch-1',humanRef:'human',message:'/wi switch wi_there'},poolKey,
        runtime:pool as unknown as CommandRuntime,acquire,control,reply:vi.fn(),newSessionId:()=>'fresh',
        stateManager:{getSession:()=>state,updateSession:(_k:string,p:any)=>Object.assign(state,p),flushStrict:vi.fn()} as any});
      expect(control.mock.calls.map(c=>[c[0].phase,c[0].pid])).toEqual([['leave',101],['enter',202]]);
      expect(state.sessionId).toBe('fresh');
      expect(state.workItemSessions.wi_here.unit.sessionId).toBe('old');
      expect(pool.send).toHaveBeenCalledTimes(1);
      expect(pool.send.mock.calls[0][1].content).toMatch(/^接手 wi_there：/);
    } finally {rmSync(dir,{recursive:true,force:true});}
  });
  it('/wi switch starts no business turn when the move stays unconfirmed',async()=>{
    const dir=mkdtempSync(join(tmpdir(),'core-switch-unknown-'));
    try {
      vi.stubEnv('RELAY_PI_RUNTIME_MANIFEST_PATH',nativeCoreFixture(dir));
      const poolKey='human:pulse';
      const state:any={poolKey,agentId:'pulse',sessionId:'old',runtime:'cc'};
      let live:any={runtime:'cc',sessionId:'old',state:'ready',process:{pid:101}};
      const pool={get:()=>live,send:vi.fn(),kill:vi.fn(async()=>{live=undefined;})};
      const acquire=async()=>{live=live??{runtime:'cc',sessionId:state.sessionId,state:'ready',process:{pid:202}};return poolKey;};
      const control=vi.fn(async(request:any)=>request.phase==='leave'
        ?{status:'local_committed',work_item:{id:'wi_here'}}
        :{status:'unknown',error:{code:'native_channel_result_unknown'}});
      const reply=vi.fn();
      await dispatchNativeWorkItemCommand({agent:{id:'pulse',runtime:'cc'} as AgentConfig,
        source:{channel:'telegram',eventId:'switch-unknown',humanRef:'human',message:'/wi switch wi_there'},poolKey,
        runtime:pool as unknown as CommandRuntime,acquire,control,reply,newSessionId:()=>'fresh',
        stateManager:{getSession:()=>state,updateSession:(_k:string,p:any)=>Object.assign(state,p),flushStrict:vi.fn()} as any});
      expect(reply.mock.calls[0][0]).toContain('结果未知');
      expect(pool.send).not.toHaveBeenCalled();
      expect(state.sessionId).toBe('old');
    } finally {rmSync(dir,{recursive:true,force:true});}
  });
  for(const profileUsesAlias of [false,true])it('directory aliases preserve one reviewed core: '+profileUsesAlias,()=>{
    const dir=realpathSync(mkdtempSync(join(tmpdir(),'core-alias-')));
    try {
      const manifest=nativeCoreFixture(dir),alias=join(dir,'home-alias');
      symlinkSync(dir,alias,'dir');
      const value=JSON.parse(readFileSync(manifest,'utf8'));
      const canonical={...value.workItemCoreHost};
      value.workItemCoreHost.python=join(alias,'python');
      if(profileUsesAlias)value.profiles.daily.workItemCoreHost.python=join(alias,'python');
      writeFileSync(manifest,JSON.stringify(value));
      vi.stubEnv('RELAY_PI_RUNTIME_MANIFEST_PATH',manifest);
      expect(nodeWorkItemCore()).toEqual(canonical);
      const other=join(dir,'other-python');writeFileSync(other,'different',{mode:0o755});
      value.workItemCoreHost.python=other;writeFileSync(manifest,JSON.stringify(value));
      expect(()=>nodeWorkItemCore()).toThrow('Installed node core differs');
    } finally {rmSync(dir,{recursive:true,force:true});}
  });
  it('an installed node missing its core cannot reopen legacy controllers',()=>{
    const dir=realpathSync(mkdtempSync(join(tmpdir(),'core-required-')));
    try {
      const manifest=nativeCoreFixture(dir),map=join(dir,'paths.json');
      const value=JSON.parse(readFileSync(manifest,'utf8'));delete value.workItemCoreHost;
      writeFileSync(manifest,JSON.stringify(value));
      writeFileSync(map,JSON.stringify({schema:'pulse_relay_paths/v1',root:dir,paths:{relayConfig:join(dir,'relay.json'),cronConfig:join(dir,'cron.json'),dataDir:dir,piManifest:manifest}}));
      vi.stubEnv('PULSE_RELAY_PATHS',map);vi.stubEnv('RELAY_PI_RUNTIME_MANIFEST_PATH',manifest);
      expect(()=>nodeWorkItemCore()).toThrow('legacy fallback is retired');
    } finally {rmSync(dir,{recursive:true,force:true});}
  });
});

describe('allocated workspace readback',()=>{
  const row={work_item_id:'wi_1',resource_id:'workspace.op_1.0',path:'/m/wi_1/ws',
    write:['/m/wi_1/ws','/r/.git/worktrees/ws'],deny:['/m/wi_1/ws/.git']};
  it('keeps the kernel rows and opens none when an older kernel names none',()=>{
    expect(allocatedWorkspaces([row])).toEqual([{workItemId:'wi_1',resourceId:'workspace.op_1.0',path:'/m/wi_1/ws',
      write:row.write,deny:row.deny}]);
    expect(allocatedWorkspaces(undefined)).toEqual([]);
  });
  it('fails the whole readback on a malformed row instead of opening part of it',()=>{
    for(const bad of [{...row,write:['relative']},{...row,deny:'x'},{...row,write:['/other',row.path]},{...row,path:1}])
      expect(()=>allocatedWorkspaces([row,bad])).toThrow('session_workspaces_unavailable');
    expect(()=>allocatedWorkspaces({})).toThrow('session_workspaces_unavailable');
  });
});

describe('native channel failures', () => {
  const request={ref:'a'.repeat(64),pid:101,phase:'leave' as const};
  it.each([
    ['timeout',Object.assign(new Error('t'),{killed:true,signal:'SIGTERM',code:null}),'native_channel_timeout'],
    ['overflow',Object.assign(new Error('o'),{killed:true,code:'ERR_CHILD_PROCESS_STDIO_MAXBUFFER'}),'native_channel_output_overflow'],
    ['exit',Object.assign(new Error('e'),{code:1}),'native_channel_exit_1'],
    ['non-JSON',null,'native_channel_result_unknown'],
  ] as const)('logs the cause and answers unknown with a readable code: %s', (_name,error,code) => {
    const log=vi.spyOn(console,'error').mockImplementation(()=>{});
    try {
      const result=readNativeChannel(request,error as any,'{"status":"local_comm','one\nTraceback\nValueError: bad\n');
      expect(result).toEqual({status:'unknown',request_id:'channel.'+'a'.repeat(64)+'.leave',error:{code}});
      expect(log).toHaveBeenCalledTimes(1);
      expect(log.mock.calls[0]![0]).toMatchObject({msg:'Native channel core diagnostic',code,phase:'leave',
        stderrTail:['one','Traceback','ValueError: bad']});
    } finally {log.mockRestore();}
  });

  it('a JSON answer is returned as it is; a refusal with its exit code is also logged', () => {
    const log=vi.spyOn(console,'error').mockImplementation(()=>{});
    try {
      expect(readNativeChannel(request,null,'{"status":"local_committed"}','')).toEqual({status:'local_committed'});
      expect(log).not.toHaveBeenCalled();
      const refused=readNativeChannel(request,Object.assign(new Error('x'),{code:2}) as any,
        '{"status":"rejected","error":{"code":"domain_conflict"}}','');
      expect(refused.error?.code).toBe('domain_conflict');
      expect(log.mock.calls[0]![0]).toMatchObject({code:'domain_conflict',exitCode:2});
    } finally {log.mockRestore();}
  });
});
