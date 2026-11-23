import { relayChildEnv } from './relay-secrets.js';
import {rolloverNativeSession,sessionControl} from './native-session-rollover.js';
import {abandonWiSwitch,abandonedText,settleInterruptedSwitch,switchInProgress,switchRecordCurrent,switchWorkItem,type PendingWiSwitch,type SwitchOutcome} from './native-wi-switch.js';
import {execFile} from 'node:child_process';
import type { AgentConfig, StateManager, StreamJsonMessage } from './types.js';
import type { CommandRuntime } from './commands.js';
import { annotatedHumanMessage, recordHumanInput, latestHumanInput, type HumanCommandSource } from './scl-human-decision.js';
import { managedRelayPath } from './managed-paths.js';
import { loadPiRuntimeManifest, reviewedWorkItemCore } from './pi-runtime-config.js';
import { nativeCommandSource, type NativeSourceCandidate } from './session-identity.js';
import { AGY_NO_CONVERSATION, agySwitchPrompt, agyTakeoverPrompt, bindAgyWorkItem } from './agy-work-item.js';

/** The installed node selects one core for every runtime and channel. */
export function nodeWorkItemCore(): {extension:string;python:string;entrypoint:string} | undefined {
  const path=managedRelayPath('piManifest',process.env.RELAY_PI_RUNTIME_MANIFEST_PATH?.trim());
  if(!path) {
    if(process.env.PULSE_RELAY_PATHS)throw new Error('Installed Relay requires its native core manifest');
    return undefined;
  }
  const manifest=loadPiRuntimeManifest(path);
  const selected=manifest.workItemCoreHost;
  if(!selected) {
    if(process.env.PULSE_RELAY_PATHS)throw new Error('Installed Relay has no selected native core; legacy fallback is retired');
    return undefined;
  }
  const profiles=Object.values(manifest.profiles).filter(p=>p.workItemCoreHost);
  if(!profiles.length) throw new Error('Installed node core differs from its reviewed profiles');
  // Both sides use the same reviewed identity. A directory alias such as
  // /root/.local -> /home/hatch/.local must not create a second Python/core.
  const reviewed=reviewedWorkItemCore({...profiles[0]!,workItemCoreHost:selected});
  if(!reviewed || profiles.some(p=>(['extension','python','entrypoint'] as const).some(key=>reviewedWorkItemCore(p)?.[key]!==reviewed[key])))
    throw new Error('Installed node core differs from its reviewed profiles');
  return reviewed;
}

export const NATIVE_WI_UNAVAILABLE =
  '新核心命令入口暂不可用；原会话和原请求保留，未回退到旧控制器。';
export const NATIVE_WI_HANDOVER_FAILED =
  '工作项命令已生效，但接续指令未能交给 Agent；请重发这条 /wi 命令。';

export type NativeChannelResult = {status?:string;request_id?:string;error?:{code?:string;reason?:string};
  left?:string|null;no_switch?:string;agent_request?:boolean;committed?:boolean;
  items?:Array<{id:string;business_progress?:string}>;
  work_item?:{id?:string;business_progress?:string};result?:{items?:unknown;
    current_observation?:{terminal?:{termination?:string}}};commands?:string[]};
type ChannelRequest={ref:string;pid?:number;native_id?:string;tenant?:string;phase?:'leave'|'enter'|'readback'};
type ChildFailure=Error&{code?:unknown;signal?:unknown;killed?:boolean};

/** The last few stderr lines, bounded, for the Relay log only. */
function stderrTail(stderr:string):string[]{
  return stderr.split('\n').map(line=>line.trimEnd()).filter(Boolean).slice(-5).map(line=>line.slice(0,300));
}

/**
 * The core answers on stdout. A timeout, an overflow, a non-zero exit or a non-JSON answer is
 * logged with its cause; when no answer can be read the result is unknown with a readable code,
 * and the caller reads the same request back.
 */
export function readNativeChannel(request:ChannelRequest,error:ChildFailure|null,stdout:string,stderr:string):NativeChannelResult{
  let answer:NativeChannelResult|undefined;
  try {
    const parsed:unknown=JSON.parse(stdout);
    if(parsed&&typeof parsed==='object')answer=parsed as NativeChannelResult;
  } catch {/* judged below */}
  const code=answer?undefined
    :error?.code==='ERR_CHILD_PROCESS_STDIO_MAXBUFFER'?'native_channel_output_overflow'
    :error?.killed?'native_channel_timeout'
    :typeof error?.code==='number'?'native_channel_exit_'+error.code
    :error?.signal?'native_channel_signal_'+String(error.signal).toLowerCase()
    :'native_channel_result_unknown';
  if(!answer||error)console.error({ts:new Date().toISOString(),level:'error',msg:'Native channel core diagnostic',
    phase:request.phase,ref:request.ref,code:code??answer?.error?.code,exitCode:error?.code,signal:error?.signal,
    killed:error?.killed===true,stdoutBytes:Buffer.byteLength(stdout),stderrTail:stderrTail(stderr)});
  return answer??{status:'unknown',request_id:'channel.'+request.ref+(request.phase==='leave'?'.leave':''),error:{code}};
}

export async function runNativeChannel(request:ChannelRequest):Promise<NativeChannelResult> {
  const host=nodeWorkItemCore();
  if(!host)throw new Error('native_channel_core_unavailable');
  return new Promise(resolve=>{
    const child=execFile(host.python,['-I','-B',host.entrypoint,'relay-channel','--input','-'],
      {encoding:'utf8',timeout:120_000,maxBuffer:8*1024*1024,env:relayChildEnv({keepInternalToken:true})},(error,stdout,stderr)=>{
        resolve(readNativeChannel(request,error as ChildFailure|null,stdout??'',stderr??''));
      });
    child.stdin?.end(JSON.stringify(request));
  });
}

/** A path the kernel opened; `standing` marks an Agent's standing read grant. */
export interface SessionAccess { path: string; mode: 'read' | 'write'; standing?: true }

/**
 * One row per path, sorted: the form Relay compares and applies. Write wins over
 * read, and a path is standing only when no session grant also opens it.
 */
export function accessRows(grants: Array<{paths: string[]; mode?: string; standing?: boolean}>): SessionAccess[] {
  const rows = new Map<string, SessionAccess>();
  for (const grant of grants) for (const path of grant.paths) {
    const prior = rows.get(path);
    const mode = prior?.mode === 'write' || grant.mode === 'write' ? 'write' : 'read';
    const standing = (prior ? prior.standing === true : true) && grant.standing === true;
    rows.set(path, standing ? { path, mode, standing: true } : { path, mode });
  }
  return [...rows.values()].sort((a, b) => a.path.localeCompare(b.path));
}

function runAccessCore(action: string, request: object, timeout: number): Promise<Record<string, any>> {
  const host = nodeWorkItemCore();
  if (!host) return Promise.resolve({});
  return new Promise<Record<string, any>>((resolve, reject) => {
    const child = execFile(host.python, ['-I', '-B', host.entrypoint, action, '--input', '-'],
      { encoding: 'utf8', timeout, maxBuffer: 4 * 1024 * 1024, env: relayChildEnv({ keepInternalToken: true }) }, (_error, stdout) => {
        try { resolve(JSON.parse(stdout)); } catch { reject(new Error('session_access_result_unavailable')); }
      });
    child.stdin?.end(JSON.stringify(request));
  });
}

/**
 * One kernel readback for this native child: `access` is what the session opens
 * (its session grants with its Agent's standing grants), `standing` is the
 * Agent's standing grants alone, as read, for Relay's kept read.
 */
export interface SessionReadback { access: SessionAccess[]; standing: SessionAccess[]; workspaces: AllocatedWorkspace[] }

/**
 * A ready workspace the kernel allocated to a WI this session holds (children
 * included): `write` is the worktree and the Git paths a commit there writes,
 * `deny` stays closed inside them (see harness_rewrite/workspaces.py
 * `session_write`).
 */
export interface AllocatedWorkspace { workItemId: string; resourceId: string; path: string; write: string[]; deny: string[] }

const absolutePaths = (value: unknown): value is string[] =>
  Array.isArray(value) && value.every((path) => typeof path === 'string' && path.startsWith('/'));

/** The kernel's workspace rows, sorted; a malformed row fails the whole readback. */
export function allocatedWorkspaces(value: unknown): AllocatedWorkspace[] {
  if (value === undefined) return [];
  if (!Array.isArray(value)) throw new Error('session_workspaces_unavailable');
  return value.map((row) => {
    if (!row || typeof row.work_item_id !== 'string' || typeof row.resource_id !== 'string'
      || typeof row.path !== 'string' || !absolutePaths(row.write) || !absolutePaths(row.deny)
      || row.write[0] !== row.path) throw new Error('session_workspaces_unavailable');
    return { workItemId: row.work_item_id, resourceId: row.resource_id, path: row.path,
      write: [...row.write], deny: [...row.deny] };
  }).sort((a, b) => a.path.localeCompare(b.path));
}

export async function readSessionAccess(pid:number): Promise<SessionReadback> {
  if (!nodeWorkItemCore()) return { access: [], standing: [], workspaces: [] };
  const result = await runAccessCore('relay-access', { pid }, 30_000);
  if (result.schema !== 'harness.session_access/v1' || !Array.isArray(result.grants))
    throw new Error(result.error?.code ?? 'session_access_unavailable');
  const standing = (Array.isArray(result.standing) ? result.standing : []).map((grant: {paths: string[]}) =>
    ({ paths: grant.paths, mode: 'read', standing: true }));
  return { access: accessRows([...result.grants, ...standing]), standing: accessRows(standing),
    workspaces: allocatedWorkspaces(result.workspaces) };
}

/**
 * The same readback for an idle agy conversation, named by Relay's retained record
 * (harness_rewrite/relay_channel.py `access_host`): agy runs no process between turns
 * and fixes each turn's permissions when it launches.
 */
export async function readRetainedSessionAccess(nativeId: string, tenant: string): Promise<SessionReadback> {
  if (!nodeWorkItemCore()) return { access: [], standing: [], workspaces: [] };
  const result = await runAccessCore('relay-access', { native_id: nativeId, tenant }, 30_000);
  if (result.schema !== 'harness.session_access/v1' || !Array.isArray(result.grants))
    throw new Error(result.error?.code ?? 'session_access_unavailable');
  const standing = (Array.isArray(result.standing) ? result.standing : []).map((grant: {paths: string[]}) =>
    ({ paths: grant.paths, mode: 'read', standing: true }));
  return { access: accessRows([...result.grants, ...standing]), standing: accessRows(standing),
    workspaces: allocatedWorkspaces(result.workspaces) };
}

/** Every Agent's standing grants in one kernel read (it replays every WI journal). */
export async function readStandingAccess(): Promise<Map<string, SessionAccess[]>> {
  const result = await runAccessCore('relay-standing-access', {}, 120_000);
  if (result.schema !== 'harness.standing_access/v1' || !Array.isArray(result.grants))
    throw new Error(result.error?.code ?? 'standing_access_unavailable');
  const byActor = new Map<string, Array<{paths: string[]; mode: string; standing: boolean}>>();
  for (const grant of result.grants) {
    if (typeof grant?.actor !== 'string' || !Array.isArray(grant.paths)) continue;
    byActor.set(grant.actor, [...(byActor.get(grant.actor) ?? []),
      { paths: grant.paths, mode: 'read', standing: true }]);
  }
  return new Map([...byActor].map(([actor, grants]) => [actor, accessRows(grants)]));
}

export function nativeChannelReply(result:NativeChannelResult):string {
  if(result.error)return '新核心命令'+(result.status==='unknown'?'结果待核对':'未完成')+'：'+
    result.error.code+'。'+(result.error.reason??'原请求保留，请查询原请求结果。');
  if(result.commands)return result.commands.join('\n');
  if(Array.isArray(result.items))return result.items.map(row=>row.id+(row.business_progress?' · '+row.business_progress:'')).join('\n')||'当前没有匹配的 WI。';
  return [result.work_item?.id, result.result?.current_observation?.terminal?.termination??result.work_item?.business_progress, result.status,
    result.request_id?'请求：'+result.request_id:undefined].filter(Boolean).join('\n');
}

function nativeRolloverReply(result:Awaited<ReturnType<typeof rolloverNativeSession>>):string{
  if(result.error?.code==='native_rollover_work_item_mismatch')return result.error.reason??'待接续的工作项与本次请求不同。';
  if(result.retiredUnprepared)return '内核确认原会话未提交转交，已清除旧轮换计划，当前会话可继续使用。';
  if(result.session_handover?.accepted&&result.session_handover.target_bootstrap_observed)
    return '已在原 WI 接续新会话，原会话历史保留。';
  if(result.session_handover?.prepared)return '原 WI 的转交已保存，后继接续尚待确认；原请求保留。';
  return '会话轮换尚未完成；原会话和原请求保留。诊断：'+(result.error?.code??'native_rollover_pending');
}

/** Capture the original boundary before acquisition; a later human input wins. */
export async function dispatchNativeWorkItemCommand(options: {
  agent: AgentConfig; source?: HumanCommandSource; poolKey: string;
  runtime: CommandRuntime; stateManager?:StateManager; reply?:(text:string)=>Promise<unknown>; acquire: () => Promise<string | null>;
  control?:typeof runNativeChannel; sessionControl?:typeof sessionControl; newSessionId?:()=>string;
}): Promise<void> {
  // The installed native core is the only WI controller; there is no fallback.
  if (!nodeWorkItemCore()) throw new Error('native_work_item_core_unavailable');
  const {source,poolKey,runtime} = options;
  if (!source) throw new Error('Native WI command requires authenticated channel ingress');
  const ref = recordHumanInput(source,poolKey);
  if (!ref) throw new Error('Native WI provenance unavailable');
  const isLatest=()=>latestHumanInput(poolKey)===ref;
  const rollover=/^\/wi(?:@[A-Za-z0-9_]+)?\s+rollover(?:\s+([\s\S]*))?$/.exec(source.message.trim());
  const firstArgument=rollover?.[1]?.trim().split(/\s+/,1)[0];
  const requestedWi=firstArgument?.startsWith('wi_')?firstArgument:undefined;
  if(requestedWi&&!/^wi_[A-Za-z0-9_]+$/.test(requestedWi)){
    await options.reply?.('工作项 ID 无效。');
    return;
  }
  const stateManager=options.stateManager;
  const pending=rollover?stateManager?.getSession(poolKey)?.pendingNativeRollover:undefined;
  if(rollover&&stateManager&&pending){
    const result=await rolloverNativeSession({poolKey,agent:options.agent,runtime,stateManager,
      channel:source.channel,control:options.sessionControl,
      resume:{ref:pending.ref,wi:requestedWi,isCurrent:isLatest}});
    if(!isLatest())throw new Error('A later human input superseded this command');
    await options.reply?.(nativeRolloverReply(result));
    return;
  }
  const acquired = await options.acquire();
  const process = runtime.get(poolKey);
  if (acquired !== poolKey || !process)
    throw new Error('Native WI command requires the original conversation');
  const agy=('runtime' in process?(process as {runtime?:string}).runtime:undefined)==='agy';
  // agy has no process between turns: the core recognizes it by the conversation it named.
  const native=nativeCommandSource(process as NativeSourceCandidate,poolKey);
  if(!native){
    await options.reply?.(AGY_NO_CONVERSATION);
    return;
  }
  if (!isLatest()) throw new Error('A later human input superseded this command');
  if(rollover&&(switchInProgress(poolKey)||stateManager?.getSession(poolKey)?.pendingWiSwitch)){
    await options.reply?.('一次 /wi switch 尚未完成，完成后再 /wi rollover；未做任何改动。');
    return;
  }
  if(rollover) {
    if(!stateManager)throw new Error('Native rollover requires durable channel transport');
    const result=await rolloverNativeSession({poolKey,agent:options.agent,runtime,stateManager,
      channel:source.channel,ref,control:options.sessionControl});
    await options.reply?.(nativeRolloverReply(result));
    return;
  }
  const switching=/^\/wi(?:@[A-Za-z0-9_]+)?\s+switch\s+(wi_[A-Za-z0-9_]+)\s*$/.exec(source.message.trim());
  if(switching) {
    if(!options.stateManager)throw new Error('Native switch requires durable channel transport');
    const outcome=await switchWorkItem({poolKey,target:switching[1]!,ref,runtime,stateManager:options.stateManager,
      acquire:options.acquire,control:options.control??runNativeChannel,newSessionId:options.newSessionId,
      isLatest:()=>latestHumanInput(poolKey)===ref});
    // The continuation goes first and clears the switch record once admitted; see deliverSwitchContinuation.
    if(outcome.target)deliverSwitchContinuation({poolKey,runtime,stateManager:options.stateManager,agent:options.agent,
      reply:text=>options.reply?.(text)??Promise.resolve(),message:text=>annotatedHumanMessage(text,source,poolKey)});
    await options.reply?.(outcome.text);
    return;
  }
  const result=await (options.control??runNativeChannel)({ref,...native});
  const target=handoffWorkItem(source.message,result);
  // The core has committed: the binding and the takeover go out before any chat reply, so a
  // failed reply cannot leave the WI unbound or the Agent uninstructed.
  let notice:string|undefined;
  if(target&&agy&&'native_id' in native){
    try{notice=bindAgyWorkItem(options,target,process.sessionId,native.native_id);}
    catch(error){
      // A failed state write must not hold back the takeover; the turns run unattributed until a /wi command binds again.
      console.error({ts:new Date().toISOString(),level:'error',msg:'agy WI usage binding not saved',poolKey,error:String(error)});
      notice='注意：'+target+' 已交给本会话，但用量绑定未能写盘，本会话的用量暂不计入该 WI；可稍后再发一次这条 /wi 命令。';
    }
  }
  // Taking over work is the human's instruction to the Agent, not only a lifecycle
  // readback: hand the same original input to the runtime in plain text.
  const readback=target?nativeChannelReply(result).replace(/\n/g,' · '):'';
  if(target)handOver(options,annotatedHumanMessage(agy
    ? agyTakeoverPrompt(target,readback)
    : '接手 '+target+'：人类已用 /wi 命令接手这个工作项（'+readback+'）。'
    +'请用 pulse_work_item 恢复 '+target+'，按当前状态继续推进；标准工作项若还没有计划，先起草计划请人类确认。',
    source,poolKey));
  await options.reply?.(notice?nativeChannelReply(result)+'\n'+notice:nativeChannelReply(result));
}

/**
 * The continuation text a settled switch hands the session it entered, chosen by that session's
 * runtime (`pending.dest.runtime`, the unit the conversation now stands on), never by the session
 * it left: an agy target cannot call pulse_work_item and is told so; every other runtime resumes
 * the WI with it. The one choice for the immediate continuation and for every later delivery.
 */
export function switchContinuation(pending:Pick<PendingWiSwitch,'target'|'resumed'|'dest'>,by:'agent'|'human'):{agy:boolean;text:string} {
  const {target,resumed}=pending;
  const who=by==='agent'?'你用 request_switch':'人类用 /wi switch';
  if(pending.dest.runtime==='agy')return {agy:true,text:agySwitchPrompt(target,who,resumed)};
  return {agy:false,text:resumed
    ? '切回 '+target+'：'+who+' 回到本会话。请用 pulse_work_item 恢复 '+target+'，按当前状态继续推进。'
    : '接手 '+target+'：'+who+' 在新会话接手这个工作项。请用 pulse_work_item 恢复 '+target
      +'，先读其检查点再继续推进；标准工作项若还没有计划，先起草计划请人类确认。'};
}

/** The Host's wrapper for a continuation no human input carries (a recovery pass, an Agent's own switch). */
function hostSwitchMessage(text:string,by:'agent'|'human'):StreamJsonMessage {
  return {type:'human',content:'[Host WI switch'+(by==='agent'?': your own request_switch':'')
    +'; this is not a new human decision.]\n'+text};
}

/** Continuations in flight in this process, by conversation and switch reference. */
const delivering=new Set<string>();

/**
 * A switch that settled on its target (`pendingWiSwitch.phase === 'entered'`) hands the Agent its
 * continuation under `operation_v1_wi_switch_<ref>`; only once the runtime admits it is the record
 * cleared. Not awaited: the turn it starts would hold the caller for its whole length. A restart
 * before admission leaves the record, and the handover pass delivers again with the same key.
 * `message` wraps the text instead of the Host header (a human /wi switch carries the human's own input).
 * An agy target is bound to the WI first, when `agent` is given (see switchContinuation).
 * The continuation goes only to the recorded target session: when the conversation stands on
 * another one (a human /reset or /new, a rollover), the record is abandoned and nothing is sent.
 * Returns 'delivering' when the record waits on a delivery (started now or already in flight),
 * 'abandoned' when it was dropped, 'none' when no continuation is due, 'failed' when this attempt
 * could not start (binding or a state write threw): the in-flight mark is cleared and the record
 * stays, so the handover pass retries the same committed switch.
 */
export function deliverSwitchContinuation(options:{poolKey:string;runtime:CommandRuntime;stateManager:StateManager;
  reply:(text:string)=>Promise<unknown>;message?:(text:string)=>StreamJsonMessage;agent?:AgentConfig}):'delivering'|'abandoned'|'none'|'failed' {
  const {poolKey,runtime,stateManager}=options;
  const saved=stateManager.getSession(poolKey);
  const pending=saved?.pendingWiSwitch;
  if(pending?.phase!=='entered')return 'none';
  const key=poolKey+'\0'+pending.ref;
  if(!switchRecordCurrent(pending,saved,runtime.get(poolKey))){
    delivering.delete(key);
    abandonWiSwitch(stateManager,poolKey,'session_replaced');
    stateManager.flushStrict();
    return 'abandoned';
  }
  if(delivering.has(key))return 'delivering';
  delivering.add(key);
  const notSent=async(error:unknown)=>{
    delivering.delete(key);
    console.error({ts:new Date().toISOString(),level:'error',msg:'WI switch continuation not sent',poolKey,
      error:String(error)});
    try{await options.reply('切换已生效，接续提示暂未交给 Agent；Relay 会再投递，也可发一条消息让它恢复 '+pending.target+'。');}
    catch{/* best effort */}
  };
  // Binding and the state writes may throw (a full disk): the attempt then ends here, the mark
  // is cleared and the record stays for the next handover pass.
  try{
    const by=pending.agent?'agent':'human';
    const chosen=switchContinuation(pending,by);
    // Bound before the turn starts, so the target's usage counts under the WI from its first turn.
    const notice=chosen.agy&&options.agent
      ? bindAgyWorkItem({poolKey,agent:options.agent,runtime,stateManager},pending.target,pending.dest.sessionId,
        pending.dest.agyConversation?.sessionId===pending.dest.sessionId?pending.dest.agyConversation.conversationId:undefined)
      : undefined;
    if(notice)void Promise.resolve().then(()=>options.reply(notice)).catch(()=>{/* best effort */});
    const message=options.message?options.message(chosen.text):hostSwitchMessage(chosen.text,by);
    const accepted=()=>{
      try{
        const now=stateManager.getSession(poolKey)?.pendingWiSwitch;
        if(now?.ref===pending.ref&&now.phase==='entered'){
          stateManager.updateSession(poolKey,{pendingWiSwitch:undefined});
          stateManager.flushStrict();
        }
      }catch(error){console.error({ts:new Date().toISOString(),level:'error',msg:'WI switch record not cleared',
        poolKey,error:String(error)});}
      finally{delivering.delete(key);}
    };
    Promise.resolve(runtime.sendIdempotent
      ? runtime.sendIdempotent(poolKey,message,'operation_v1_wi_switch_'+pending.ref,{onAccepted:accepted})
      : runtime.send(poolKey,message,{onAccepted:accepted})).catch(notSent);
    return 'delivering';
  }catch(error){
    void notSent(error);
    return 'failed';
  }
}

/**
 * The handover pass for a switch left in `pendingWiSwitch`: an unsettled enter is read back
 * (`settleInterruptedSwitch`); a switch that stands on its target gets its continuation. The
 * continuation goes out before the chat notice, so a slow notice cannot hold it. Returns
 * whether anything settled or was delivered.
 */
export async function settleWiSwitch(options:{poolKey:string;runtime:CommandRuntime;stateManager:StateManager;
  acquire:()=>Promise<string|null>;reply:(text:string)=>Promise<unknown>;control?:typeof runNativeChannel;
  agent?:AgentConfig}):Promise<boolean>{
  const {poolKey,runtime,stateManager}=options;
  const pending=stateManager.getSession(poolKey)?.pendingWiSwitch;
  if(!pending)return false;
  let text:string|undefined;
  if(pending.phase!=='entered'){
    const outcome=await settleInterruptedSwitch({poolKey,runtime,stateManager,acquire:options.acquire,
      control:options.control??runNativeChannel,boundary:()=>latestHumanInput(poolKey)});
    if(!outcome)return false;
    text=(outcome.agent?'Agent 自主切换：':'')+outcome.text;
  }
  const entered=stateManager.getSession(poolKey)?.pendingWiSwitch;
  if(entered?.phase==='entered'&&!switchInProgress(poolKey)){
    const prefix=entered.agent?'Agent 自主切换：':'';
    let live=runtime.get(poolKey);
    // Checked before starting anything: a replaced conversation is never started for an old record.
    if(!switchRecordCurrent(entered,stateManager.getSession(poolKey),live)){
      if(deliverSwitchContinuation({poolKey,runtime,stateManager,reply:options.reply,agent:options.agent})==='abandoned')
        text=prefix+abandonedText(entered);
    }else{
      if((!live||live.state==='dead')&&await options.acquire()!==poolKey)return text!==undefined;
      const fresh=!delivering.has(poolKey+'\0'+entered.ref);
      const delivered=deliverSwitchContinuation({poolKey,runtime,stateManager,reply:options.reply,agent:options.agent});
      if(delivered==='abandoned')text=prefix+abandonedText(entered);
      else if(text===undefined&&fresh&&delivered==='delivering')text=prefix+'切换已生效：'+entered.target
        +' 的接续提示重新交给 Agent。';
    }
  }
  if(text!==undefined){
    try{await options.reply(text);}
    catch(error){console.error({ts:new Date().toISOString(),level:'error',msg:'WI switch notice not sent',
      poolKey,error:String(error)});}
  }
  return text!==undefined;
}

/**
 * An Agent's committed request_switch, run after its turn ended: the same switch as /wi switch,
 * with the notice ref in place of a human input ref. A switch that stands gets its continuation
 * first; the outcome then goes to the chat. A later human input during the switch stops it
 * before the session is replaced.
 */
export async function runAgentSwitch(options:{ref:string;target:string;poolKey:string;runtime:CommandRuntime;
  stateManager:StateManager;acquire:()=>Promise<string|null>;reply:(text:string)=>Promise<unknown>;
  control?:typeof runNativeChannel;newSessionId?:()=>string;agent?:AgentConfig}):Promise<SwitchOutcome> {
  const {poolKey,runtime,target,ref}=options;
  // Relay restarted or the process exited: the source session is resumed to leave from it.
  if(!runtime.get(poolKey)&&await options.acquire()!==poolKey)throw new Error('agent_switch_source_unavailable');
  const latest=latestHumanInput(poolKey);
  const outcome=await switchWorkItem({poolKey,target,ref,runtime,stateManager:options.stateManager,
    acquire:options.acquire,control:options.control??runNativeChannel,newSessionId:options.newSessionId,
    isLatest:()=>latestHumanInput(poolKey)===latest,agentRequest:true});
  if(outcome.target)deliverSwitchContinuation({poolKey,runtime,stateManager:options.stateManager,reply:options.reply,
    agent:options.agent});
  try{await options.reply('Agent 自主切换：'+outcome.text);}
  catch(error){console.error({ts:new Date().toISOString(),level:'error',msg:'Agent switch notice not sent',poolKey,
    error:String(error)});}
  return outcome;
}

/**
 * Hand the takeover instruction to the runtime without waiting for its
 * dispatch: behind a busy turn that would hold the channel's intake lane for
 * the whole turn. The runtime queues keep call order.
 */
function handOver(options:{poolKey:string;runtime:CommandRuntime;reply?:(text:string)=>Promise<unknown>},
  message:StreamJsonMessage):void {
  let sent:Promise<void>;
  try{sent=Promise.resolve(options.runtime.send(options.poolKey,message));}catch(error){sent=Promise.reject(error);}
  sent.catch(async(error:unknown)=>{
    console.error({ts:new Date().toISOString(),level:'error',msg:'Native WI takeover instruction not sent',
      poolKey:options.poolKey,error:String(error)});
    try{await options.reply?.(NATIVE_WI_HANDOVER_FAILED);}catch{/* the failure notice is best effort */}
  });
}

/** `/wi WI` and `/wi continue [WI]` hand an accepted work item to the Agent. */
export function handoffWorkItem(message:string,result:Record<string,any>):string|undefined {
  if(result.error || typeof result.work_item?.id!=='string')return undefined;
  if(!/^\/wi(?:@[A-Za-z0-9_]+)?\s+(?:continue(?:\s+wi_[A-Za-z0-9_]+)?|wi_[A-Za-z0-9_]+)\s*$/.test(message.trim()))return undefined;
  return result.work_item.id;
}
