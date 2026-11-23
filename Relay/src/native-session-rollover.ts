/** Physical session controller. Git core readback alone settles the handover. */
import { relayChildEnv } from './relay-secrets.js';
import {execFile} from 'node:child_process';
import type {AgentConfig,StateManager,ConversationRuntimeId,SpawnConfig} from './types.js';
import type {CommandRuntime} from './commands.js';
import {agyConversationBinding,conversationLaunch,switchedSessionRuntime} from './conversation-launch.js';
import {nodeWorkItemCore} from './native-work-item-command.js';
import {underlyingRuntime} from './runtime-identity.js';
import {conversationNativeIds,nativeCommandSource,type NativeSourceCandidate} from './session-identity.js';
import {agyRolloverPrompt,bindAgyWorkItem} from './agy-work-item.js';

/** standInSessionId: the one session a human named to stand in for the source, by the /reset that made it
 * or by a later /wi rollover in the same channel. No other session is ever replaced.
 * origin 'agent': the holder session's own committed request_rollover, not a human /wi rollover. */
export type NativeRolloverPlan={ref:string;sourceSessionId:string;runtime:ConversationRuntimeId;targetSessionId?:string;
  standInSessionId?:string;origin?:'agent'};
type Handover={ref:string;wi:string;source_native_id:string;target_session_id:string;runtime:string;prepared:boolean;accepted:boolean;target_session_exists?:boolean|null;target_native_id?:string;target_bootstrap_observed?:boolean};
type Readback={status:string;session_handover?:Handover;error?:{code?:string;reason?:string;retryable?:boolean};work_item?:{generation:number};
  retiredUnprepared?:true};
type Control=(request:{stage:'prepare'|'inspect'|'accept';ref:string;pid?:number;native_id?:string;tenant?:string})=>Promise<Readback>;
type Resume={ref:string;wi?:string;isCurrent:()=>boolean};
type Input={poolKey:string;agent:AgentConfig;runtime:CommandRuntime;stateManager:StateManager;
  channel:'telegram'|'feishu';ref?:string;control?:Control;resume?:Resume;origin?:'agent';
  /** The Agent request's source; lets a persisted source with no live instance start the handover. */
  sourceNativeId?:string};
const queues=new WeakMap<CommandRuntime,Map<string,Promise<Readback>>>();
export function nativeRolloverActiveCount(runtime:CommandRuntime){return queues.get(underlyingRuntime(runtime))?.size??0;}

export async function sessionControl(request:Parameters<Control>[0]):Promise<Readback>{
  const host=nodeWorkItemCore();
  if(!host)throw new Error('native_session_core_binding_unavailable');
  return new Promise(resolve=>{
    const child=execFile(host.python,['-I','-B',host.entrypoint,'relay-session','--input','-'],
      {encoding:'utf8',timeout:120_000,maxBuffer:2*1024*1024,env:relayChildEnv({keepInternalToken:true})},(error,stdout,stderr)=>{
        resolve(readSessionControl(request.stage,error,stdout,stderr));
      });
    child.stdin?.end(JSON.stringify(request));
  });
}

type ChildFailure=Error&{code?:unknown;signal?:unknown;killed?:boolean};

const controlled=(value:unknown,pattern:RegExp)=>typeof value==='number'||(typeof value==='string'&&pattern.test(value))
  ?value:undefined;

/** The core answers on stdout. stderr text is never read; only controlled fields reach the log. */
export function readSessionControl(stage:string,error:ChildFailure|null,stdout:string,stderr:string):Readback{
  let answer:Readback;
  try {
    const parsed:unknown=JSON.parse(stdout);
    if(!parsed||typeof parsed!=='object'||!('status' in parsed)||typeof parsed.status!=='string')
      throw new Error('Invalid native session response');
    answer=parsed as Readback;
  }
  catch {answer={status:'unknown',error:{code:'native_session_result_unknown'}};}
  const failed=answer.error as {code?:unknown;exception_type?:unknown}|undefined;
  if(error||stderr.length||answer.status==='unknown'||failed?.exception_type!==undefined)
    console.error({msg:'Native session core diagnostic',stage,
      code:controlled(failed?.code,/^[a-z][a-z_]{0,63}$/),
      exception:controlled(failed?.exception_type,/^[A-Z][A-Za-z]{0,63}(?:Error|Exception|Exit|Interrupt)$/),
      exitCode:controlled(error?.code,/^ERR_[A-Z_]{1,64}$/),signal:controlled(error?.signal,/^SIG[A-Z0-9]{1,16}$/),
      killed:error?.killed===true,stdoutBytes:Buffer.byteLength(stdout),stderrBytes:Buffer.byteLength(stderr)});
  return answer;
}

function assertResumeCurrent(resume?:Resume):void{
  if(resume&&!resume.isCurrent())throw new Error('A later human input superseded this command');
}

function verifyResume(resume:Resume,result:Readback):Readback|undefined{
  assertResumeCurrent(resume);
  if(result.error||result.status==='unknown'||result.status==='rejected')return result;
  const handover=result.session_handover;
  if(handover?.ref!==resume.ref||typeof handover.wi!=='string')
    return {status:'unknown',error:{code:'native_rollover_original_request_unverified'}};
  if(resume.wi&&handover.wi!==resume.wi)
    return {status:'rejected',error:{code:'native_rollover_work_item_mismatch',reason:`待接续的工作项是 ${handover.wi}。`}};
  return undefined;
}

export async function rolloverNativeSession(input:Input):Promise<Readback>{
  const owner=underlyingRuntime(input.runtime);
  let byAddress=queues.get(owner);
  if(!byAddress){byAddress=new Map();queues.set(owner,byAddress);}
  let active=byAddress.get(input.poolKey);
  while(active&&input.resume){
    // Read back this command's exact request after any in-flight recovery settles.
    try {await active;} catch {/* The explicit retry still needs its own readback. */}
    active=byAddress.get(input.poolKey);
  }
  if(active)return active;
  const operation=advance(input);
  byAddress.set(input.poolKey,operation);
  try {return await operation;} finally {byAddress.delete(input.poolKey);}
}

async function advance(input:Input):Promise<Readback>{
  const {runtime,stateManager,poolKey,agent}=input;
  const control=input.control??sessionControl;
  const saved=()=>stateManager.getSession(poolKey);
  let plan=saved()?.pendingNativeRollover;
  let live=runtime.get(poolKey);
  if(input.resume&&plan?.ref!==input.resume.ref){
    if(plan)throw new Error('native_rollover_original_request_changed');
    const result=await control({stage:'inspect',ref:input.resume.ref});
    return verifyResume(input.resume,result)??result;
  }
  if(!plan){
    const stored=saved();
    // No instance (Relay restarted, or the process exited), but this host's session still names the source:
    // the plan is persisted first and the source is recovered below before the core re-checks it at prepare.
    const dormant=!!input.sourceNativeId&&(!live||live.state==='dead')&&!!stored
      &&conversationNativeIds(stored).includes(input.sourceNativeId)?stored:undefined;
    if(!input.ref||!dormant&&(!live||['busy','starting','dead'].includes(live.state)))
      throw new Error('native_rollover_requires_idle_original_session');
    const previous=stored?.nativeSessionHistory?.find(h=>h.rolloverRef===input.ref);
    const source=dormant?{sessionId:dormant.sessionId,runtime:dormant.runtime??'cc'}
      :{sessionId:live!.sessionId,runtime:live&&'runtime' in live?live.runtime:'cc'};
    plan={ref:input.ref,sourceSessionId:previous?.sessionId??source.sessionId,runtime:previous?.runtime??source.runtime,
      ...(input.origin?{origin:input.origin}:{})};
    stateManager.updateSession(poolKey,{pendingNativeRollover:plan});
    stateManager.flushStrict();
  }
  // Only the session named by the authenticated retry or its own /reset may stand in for the source.
  const resetAway=(sessionId?:string)=>{
    const session=saved();
    return !!session&&sessionId===session.sessionId&&sessionId!==plan?.sourceSessionId
      &&sessionId!==plan?.targetSessionId&&sessionId===session.pendingNativeRollover?.standInSessionId
      &&!session.activeWorkItemRuntimeBinding&&!session.pendingWorkItemEntry;
  };
  let observed=await control({stage:'inspect',ref:plan.ref});
  if(input.resume){
    const failure=verifyResume(input.resume,observed);
    if(failure)return failure;
    const session=saved();
    const current=session?.pendingNativeRollover;
    if(!session||current?.ref!==plan.ref)throw new Error('native_rollover_original_request_changed');
    plan=current;
    if(session.sessionId!==plan.sourceSessionId&&session.sessionId!==plan.targetSessionId
      &&!session.activeWorkItemRuntimeBinding&&!session.pendingWorkItemEntry){
      plan={...plan,standInSessionId:session.sessionId};
      stateManager.updateSession(poolKey,{pendingNativeRollover:plan});
      stateManager.flushStrict();
    }
  }
  if(!observed.session_handover?.prepared){
    if(resetAway(saved()?.sessionId)&&observed.session_handover?.prepared===false
      &&!observed.error&&!['unknown','rejected'].includes(observed.status)){
      // The ledger read back no source decision, and the source this rollover names is gone.
      stateManager.updateSession(poolKey,{pendingNativeRollover:undefined});stateManager.flushStrict();
      return {...observed,retiredUnprepared:true};
    }
    // A lost response is read first, then the same source request may be repeated.
    if((!live||live.state==='dead') && saved()?.sessionId===plan.sourceSessionId){
      live=await runtime.spawn(conversationLaunch({poolKey,agent,session:saved(),channel:input.channel,intent:'recovery'}));
    }
    if(!live||live.sessionId!==plan.sourceSessionId)return observed;
    if(['busy','starting'].includes(live.state))throw new Error('native_rollover_source_busy');
    assertResumeCurrent(input.resume);
    const source=nativeCommandSource(live as NativeSourceCandidate,poolKey);
    if(!source)return {status:'rejected',error:{code:'native_rollover_source_unidentified',
      reason:'agy 会话还没有对话，内核认不出原会话；原会话保留。'}};
    observed=await control({stage:'prepare',ref:plan.ref,...source});
    if(plan.origin==='agent'&&!observed.session_handover?.prepared&&observed.status==='rejected'
      &&observed.error?.retryable!==true){
      // The core refused the Agent's own request (its frontier moved on, an effect is in doubt):
      // nothing was fenced, so the source conversation simply continues.
      stateManager.updateSession(poolKey,{pendingNativeRollover:undefined});stateManager.flushStrict();
      return {...observed,retiredUnprepared:true};
    }
  }
  const handover=observed.session_handover;
  if(!handover?.prepared)return observed;
  if(handover.target_session_exists===null)throw new Error('native_rollover_target_presence_unknown');
  if(handover.accepted && handover.target_session_exists===false)throw new Error('native_rollover_accepted_source_unavailable');
  if(plan.targetSessionId&&plan.targetSessionId!==handover.target_session_id)
    throw new Error('native_rollover_original_target_changed');
  assertResumeCurrent(input.resume);
  const currentPlan=saved()?.pendingNativeRollover;
  if(currentPlan?.ref!==plan.ref)throw new Error('native_rollover_original_request_changed');
  plan={...currentPlan,targetSessionId:handover.target_session_id};
  stateManager.updateSession(poolKey,{pendingNativeRollover:plan});
  stateManager.flushStrict();
  live=runtime.get(poolKey);
  // Judged once before any stop: stopping a process may itself refresh the persisted session.
  const standIn=saved()?.sessionId,standInProved=resetAway(standIn);
  const replaceable=(sessionId:string)=>standInProved&&sessionId===standIn;
  if(live&&live.sessionId!==plan.sourceSessionId&&live.sessionId!==plan.targetSessionId&&!replaceable(live.sessionId))
    throw new Error('native_rollover_conversation_changed');
  if(live&&live.sessionId!==plan.targetSessionId&&live.state!=='dead'){
    if(['busy','starting'].includes(live.state))throw new Error('native_rollover_source_busy');
    await runtime.kill(poolKey);
    assertResumeCurrent(input.resume);
    live=undefined;
  }
  const source=saved();
  if(!source)throw new Error('native_rollover_original_channel_unavailable');
  if(source.pendingNativeRollover?.ref!==plan.ref)throw new Error('native_rollover_original_request_changed');
  if(source.sessionId!==plan.sourceSessionId&&source.sessionId!==plan.targetSessionId&&!replaceable(source.sessionId))
    throw new Error('native_rollover_persisted_conversation_changed');
  const launch=conversationLaunch({poolKey,agent,session:source,channel:input.channel,intent:'recovery'});
  if(source.sessionId!==plan.targetSessionId){
    // This is a channel projection after the source's committed relinquishment.
    // Historical bindings remain as provenance; no new grant is manufactured.
    // A source a /wi switch saved for its WI hands that place to the successor, which then keeps
    // the source's runtime on later launches as the source did (conversation-launch.ts).
    const switched=switchedSessionRuntime(source)!==undefined;
    stateManager.updateSession(poolKey,{sessionId:plan.targetSessionId,runtime:plan.runtime,
      ...(switched?{workItemSessions:{...source.workItemSessions,[handover.wi]:{at:new Date().toISOString(),
        unit:{sessionId:handover.target_session_id,runtime:plan.runtime}}}}:{}),
      codexThreadId:undefined,activeWorkItemRuntimeBinding:undefined,pendingWorkItemEntry:undefined,
      interruptedUserMessage:undefined,wasBusyAtShutdown:undefined,
      nativeSessionHistory:[...(source.nativeSessionHistory??[]),{sessionId:source.sessionId,
        runtime:plan.runtime,rolloverRef:plan.ref,activeWorkItemRuntimeBinding:source.activeWorkItemRuntimeBinding}],
      pendingNativeRollover:plan});
    stateManager.flushStrict();
  }
  if(!live||live.state==='dead'){
    // A successor rebuilt after a restart keeps the WI binding already saved for it (bound below only when missing).
    const kept=plan.runtime==='agy'?agyConversationBinding(saved(),plan.targetSessionId!):undefined;
    live=await runtime.spawn({...launch,runtime:plan.runtime,conversationRuntimePreference:[plan.runtime],
      sessionId:plan.targetSessionId,resume:handover.target_session_exists===true,
      codexThreadId:plan.runtime==='codex'?handover.target_native_id:undefined,
      runtimeSessionBinding:undefined,activeWorkItemRuntimeBinding:undefined,runtimeUsageBinding:undefined,
      hostRecoveryDecisionPath:undefined,hostRecoveryNativeSessionFile:undefined,
      canonicalResourceNativeProofPath:undefined,
      computeCostBinding:kept,measurementBaselineReference:undefined,nativeHandoverRef:plan.ref} as SpawnConfig);
  }
  if(live.sessionId!==plan.targetSessionId)throw new Error('native_rollover_actual_target_changed');
  const agy=plan.runtime==='agy';
  // agy cannot accept from inside its successor (no tools) and runs no process between turns:
  // Relay accepts it by the conversation its bootstrap turn started, on the pass after that turn.
  const successor=agy?nativeCommandSource(live as NativeSourceCandidate,poolKey)
    :live.process?.pid?{pid:live.process.pid}:undefined;
  if(!handover.accepted&&successor){
    observed=await control({stage:'accept',ref:plan.ref,...successor});
  }
  // The successor's turns, its bootstrap included, count toward the WI it continues.
  if(agy&&saved()?.agyWorkItemBinding?.sessionId!==plan.targetSessionId)
    bindAgyWorkItem({poolKey,agent,runtime,stateManager},handover.wi,plan.targetSessionId);
  if(!handover.target_bootstrap_observed && !['busy','starting'].includes(live.state)){
    if(!runtime.sendIdempotent)throw new Error('native_rollover_idempotent_dispatch_unavailable');
    await runtime.sendIdempotent(poolKey,agy?{type:'human',content:agyRolloverPrompt(plan.ref,handover.wi)}:{type:'human',content:
      '[Host session continuation; this is not a new human decision.]\n'+
      '[Host continuation operation: '+plan.ref+']\n'+
      'Read and resume the original WorkItem '+handover.wi+' with pulse_work_item. '+
      'The original rollover request is '+plan.ref+'. The Host connects this actual successor. '+
      'Preserve the original scope, approvals, candidate and completed work. Do not create or reinitialize a WI. '+
      'Continue only the previously authorized work; retain any outstanding human decision.'},'operation_v1_native_rollover_'+plan.ref);
  }
  const final=await control({stage:'inspect',ref:plan.ref});
  if(final.session_handover?.accepted && final.session_handover.target_bootstrap_observed
    &&saved()?.pendingNativeRollover?.ref===plan.ref){
    stateManager.updateSession(poolKey,{pendingNativeRollover:undefined});stateManager.flushStrict();
  }
  return final;
}
