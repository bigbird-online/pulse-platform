/**
 * /wi switch between sessions of different runtimes, through the real acquisition path:
 * acquireConversation, conversationLaunch, the conversation runtime manager and the
 * persisted state manager. Only the runtime adapters and the kernel answer are synthetic.
 */
import {describe,it,expect,vi,afterEach} from 'vitest';
import {mkdtempSync,rmSync} from 'node:fs';
import {join} from 'node:path';
import {tmpdir} from 'node:os';
import {acquireConversation} from '../src/conversation-acquisition.js';
import {createConversationRuntimeManager,type ConversationProcess,type ConversationRuntimeAdapter} from '../src/conversation-runtime.js';
import {dispatchNativeWorkItemCommand} from '../src/native-work-item-command.js';
import {createStateManager} from '../src/state.js';
import {nativeCoreFixture} from './helpers/native-work-item-core.js';
import {isolateHumanDecisions} from './helpers/scl-human-decision.js';
import type {AgentConfig,PersistedSession,StateManager} from '../src/types.js';
import type {CommandRuntime} from '../src/commands.js';
isolateHumanDecisions();

const POOL='owner:main';
const HERE='wi_20261122_001',THERE='wi_20261122_002';

/** A runtime adapter that records its launches; agy takes a WI usage binding, cc does not. */
function adapter(id:'cc'|'agy',state:()=>StateManager){
  const processes=new Map<string,ConversationProcess&Record<string,any>>();
  const launches:any[]=[];const sent:any[]=[];const bound:any[]=[];
  const base:ConversationRuntimeAdapter={
    id,admitsTurns:true,
    spawn:vi.fn(async(config:any)=>{
      launches.push(config);
      const saved=state().getSession(config.tenantId)?.agyConversation;
      const proc:any={tenantId:config.tenantId,runtime:id,sessionId:config.sessionId,state:'idle',lastActivity:1,spawnedAt:launches.length,
        restartCount:0,restartWindowStart:1,
        ...(id==='agy'?{agyConversationId:saved?.sessionId===config.sessionId?saved.conversationId:undefined,
          computeCostBinding:config.computeCostBinding}:{process:{pid:4000+launches.length}})};
      processes.set(config.tenantId,proc);
      return proc;
    }),
    send:vi.fn(async(_t:string,message:unknown,turn?:any)=>{sent.push(message);turn?.arrival?.accepted();}),
    kill:vi.fn(async(t:string)=>{processes.delete(t);}),
    killAll:vi.fn(async()=>{processes.clear();}),
    get:vi.fn((t:string)=>processes.get(t)),
    getAll:vi.fn(()=>[...processes.values()]),
    getStatus:vi.fn(()=>[]),
    on:vi.fn() as any,
    emitShutdown:vi.fn(),
    dispose:vi.fn(),
  } as any;
  if(id==='agy')(base as any).bindRuntimeUsage=vi.fn((t:string,binding:any)=>{
    bound.push(binding);const proc=processes.get(t);if(proc)proc.computeCostBinding=binding;});
  return {adapter:base,launches,sent,bound,processes};
}

function unit(runtime:'cc'|'agy',sessionId:string){
  return runtime==='agy'?{sessionId,runtime,agyConversation:{sessionId,conversationId:'conv-'+sessionId}}:{sessionId,runtime};
}

let dirs:string[]=[];
afterEach(()=>{for(const d of dirs)rmSync(d,{recursive:true,force:true});dirs=[];});

async function scenario(from:'cc'|'agy',to:'cc'|'agy'){
  const dir=mkdtempSync(join(tmpdir(),'wi-switch-xrt-'));dirs.push(dir);
  vi.stubEnv('RELAY_PI_RUNTIME_MANIFEST_PATH',nativeCoreFixture(dir));
  const stateManager=createStateManager(join(dir,'state.json'));stateManager.load();
  // The channel is configured for the runtime the conversation starts in.
  const agent={id:'main',name:'Main',emoji:'M',workspace:'/ws',runtime:from,conversationRuntimePreference:[from]} as unknown as AgentConfig;
  stateManager.updateSession(POOL,{poolKey:POOL,agentId:'main',status:'active',lastActivity:1,turnCount:1,maxTurns:100,
    ...unit(from,'here'),reasoningLevel:'high',
    workItemSessions:{[THERE]:{at:'t',unit:{...unit(to,'there'),reasoningLevel:'low'} as any}}} as PersistedSession);
  const cc=adapter('cc',()=>stateManager),agy=adapter('agy',()=>stateManager);
  const manager=createConversationRuntimeManager({});manager.register(cc.adapter);manager.register(agy.adapter);
  const acquire=async()=>{
    await acquireConversation({poolKey:POOL,agent,runtime:manager as any,stateManager,channel:'telegram',intent:'inbound'});
    return POOL;
  };
  await acquire();
  const control=vi.fn(async(request:any)=>({status:'local_committed',request_id:'channel.'+request.ref,
    work_item:{id:request.phase==='leave'?HERE:THERE,business_progress:'active'}}));
  const reply=vi.fn(async(_text:string)=>{});
  await dispatchNativeWorkItemCommand({agent,source:{channel:'telegram',eventId:'xrt-'+from+'-'+to,humanRef:'human',
    message:'/wi switch '+THERE},poolKey:POOL,runtime:manager as unknown as CommandRuntime,stateManager,acquire,control,reply});
  await new Promise(resolve=>setImmediate(resolve));
  return {stateManager,manager,cc,agy,control,reply,agent,acquire};
}

describe('/wi switch across runtimes through acquireConversation', () => {
  for(const [from,to] of [['agy','cc'],['cc','agy'],['agy','agy'],['cc','cc']] as const)
  it(from+'→'+to+' starts the target in its own runtime, binds and briefs by it', async () => {
    const s=await scenario(from,to);
    expect(s.control.mock.calls.map(c=>c[0].phase)).toEqual(['leave','enter']);
    const target=s.manager.get(POOL)!;
    expect(target).toMatchObject({runtime:to,sessionId:'there'});
    const launched=(to==='agy'?s.agy:s.cc).launches.at(-1);
    expect(launched).toMatchObject({sessionId:'there',runtime:to,conversationRuntimePreference:[to],resume:true,
      reasoningLevel:'low'});
    // enter names the target by its own runtime's identity.
    expect(s.control.mock.calls[1]![0]).toEqual(to==='agy'
      ? {ref:expect.any(String),native_id:'conv-there',tenant:POOL,phase:'enter'}
      : {ref:expect.any(String),pid:expect.any(Number),phase:'enter'});
    const sent=(to==='agy'?s.agy:s.cc).sent;
    expect(sent).toHaveLength(1);
    const content=String(sent[0].content);
    const saved=s.stateManager.getSession(POOL)!;
    if(to==='agy'){
      expect(s.agy.bound).toHaveLength(1);
      expect(s.agy.bound[0]).toMatchObject({work_item_id:THERE,runtime:'agy'});
      expect(saved.agyWorkItemBinding).toMatchObject({sessionId:'there',conversationId:'conv-there',usageBinding:{work_item_id:THERE}});
      expect(content).toContain('本运行时（agy）不能调用 pulse_work_item');
      expect(content).toContain('cd <字面绝对路径> && git commit --no-verify -m');
      expect(content).toContain('本会话终端的权限与本 Agent 的 CC 会话相同');
      expect(content).not.toContain('请用 pulse_work_item');
    }else{
      expect(s.agy.bound).toHaveLength(0);
      expect(content).toContain('请用 pulse_work_item 恢复 '+THERE);
    }
    const replies=s.reply.mock.calls.map(c=>c[0]).join('\n');
    expect(replies).toContain('已切换到 '+THERE);
    expect(replies).not.toMatch(/用量绑定未能发出|切换未完成/);
    expect(saved.pendingWiSwitch).toBeUndefined();
    // Each session keeps its own /think level.
    expect(saved.reasoningLevel).toBe('low');
    expect(saved.workItemSessions![HERE]!.unit).toMatchObject({sessionId:'here',runtime:from,reasoningLevel:'high'});

    // After the switch settled, a dead process is relaunched by an ordinary inbound in the target's runtime.
    await s.manager.kill(POOL);
    await s.acquire();
    expect(s.manager.get(POOL)).toMatchObject({runtime:to,sessionId:'there'});

    // Switching back resumes the origin in its own runtime and level.
    await dispatchNativeWorkItemCommand({agent:s.agent,source:{channel:'telegram',eventId:'xrt-back-'+from+'-'+to,humanRef:'human',
      message:'/wi switch '+HERE},poolKey:POOL,runtime:s.manager as unknown as CommandRuntime,stateManager:s.stateManager,
      acquire:s.acquire,control:vi.fn(async(request:any)=>({status:'local_committed',
        work_item:{id:request.phase==='leave'?THERE:HERE}})),reply:vi.fn(async()=>{})});
    expect(s.manager.get(POOL)).toMatchObject({runtime:from,sessionId:'here'});
    expect((from==='agy'?s.agy:s.cc).launches.at(-1)).toMatchObject({sessionId:'here',reasoningLevel:'high'});
  });
});
