import {randomUUID} from 'node:crypto';
import type {StateManager, WorkItemSession, SessionIdentityUnit, PersistedSession} from './types.js';
import type {CommandRuntime} from './commands.js';
import type {NativeChannelResult} from './native-work-item-command.js';
import {replaceConversation} from './conversation-acquisition.js';
import {nativeCommandSource,type NativeSourceCandidate} from './session-identity.js';

type Control = (request:{ref:string;pid?:number;native_id?:string;tenant?:string;phase:'leave'|'enter'|'readback'}) =>
  Promise<NativeChannelResult & {left?:string|null}>;
type Live = NonNullable<ReturnType<CommandRuntime['get']>>;

/** Every field that identifies a native session to acquisition and usage binding, with its /think level. */
const UNIT_KEYS = ['sessionId','runtime','codexThreadId','codexThreadUsage','agyConversation','agyWorkItemBinding',
  'reasoningLevel','activeWorkItemRuntimeBinding','pendingWorkItemEntry','terminalWorkItemRuntimeBinding'] as const;

function unitOf(saved:PersistedSession|undefined, live:Live): SessionIdentityUnit {
  const unit:any={};
  for(const key of UNIT_KEYS)unit[key]=saved?.[key];
  unit.sessionId=live.sessionId || saved?.sessionId || '';
  unit.runtime=saved?.runtime ?? 'cc';
  const thread=('codexThreadId' in live ? live.codexThreadId : undefined) ?? saved?.codexThreadId;
  unit.codexThreadId=unit.runtime==='codex' ? thread : undefined;
  return unit as SessionIdentityUnit;
}

/** The actual native source of a live conversation, as `/wi continue` passes it. An agy session
 * without a conversation names none; the core then refuses the request as unidentified. */
function nativeSource(live:Live, poolKey:string) {
  return nativeCommandSource(live as NativeSourceCandidate,poolKey) ?? {native_id:'',tenant:poolKey};
}

/** Adapters return a fresh wrapper on every get(); compare the native process, not the object. */
function nativeIdentity(live:Live|undefined):string {
  if(!live)return '';
  return JSON.stringify(['runtime' in live ? live.runtime : undefined,live.sessionId,live.spawnedAt,live.process?.pid,
    'codexThreadId' in live ? live.codexThreadId : undefined]);
}

/** How many times enter is read back before its result counts as unknown. */
const ENTER_READBACKS=3;
/** Leave carries the same input reference, so a repeat reads the committed checkpoint back. */
const LEAVE_READBACKS=3;

/** `retained`: nothing settled yet (leave result unknown, or enter still unconfirmed and recorded
 * in `pendingWiSwitch`); an Agent's notice is kept for a later pass. */
export type SwitchOutcome = {text:string; target?:string; resumed?:boolean; retained?:true};

/** One switch per conversation at a time; a second one is refused, never queued. */
const switching = new Set<string>();
export function switchInProgress(poolKey:string):boolean {return switching.has(poolKey);}

/**
 * `/wi switch WI`: one chat, many native sessions, one WI each.
 * 1. The kernel records where the work held here stands (leave).
 * 2. The chat's whole session identity unit is saved under the left WI and replaced by
 *    the target WI's saved unit, or a new session; the original session file is kept.
 * 3. The kernel moves the target's execution into the started session (enter, as /wi continue).
 * A failure before enter commits restores the original unit; after enter commits the
 * switch stands and a local bookkeeping failure is reported, never rolled back.
 */
export async function switchWorkItem(options:{
  poolKey:string; target:string; ref:string; runtime:CommandRuntime; stateManager:StateManager;
  acquire:()=>Promise<string|null>; control:Control; newSessionId?:()=>string;
  /** Whether this command is still the conversation's latest human input. */
  isLatest?:()=>boolean;
  /** An Agent's own request_switch (recorded in `pendingWiSwitch.agent`). */
  agentRequest?:boolean;
}):Promise<SwitchOutcome> {
  if(switching.has(options.poolKey))return {text:'另一个 /wi switch 正在进行；未做任何改动。'};
  switching.add(options.poolKey);
  try {return await switchLocked(options);}
  finally {switching.delete(options.poolKey);}
}

async function switchLocked(options:Parameters<typeof switchWorkItem>[0]):Promise<SwitchOutcome> {
  const {poolKey,target,ref,runtime,stateManager}=options;
  const isLatest=options.isLatest ?? (()=>true);
  const live=runtime.get(poolKey);
  const saved=stateManager.getSession(poolKey);
  if(!live)return {text:'切换未开始：当前会话不可用；原会话保留。'};
  if(['busy','starting'].includes(String(live.state)))
    return {text:'当前会话正在工作，完成后再 /wi switch；未做任何改动。'};
  if(saved?.pendingNativeRollover || saved?.pendingWorkItemEntry || saved?.pendingWiSwitch)
    return {text:'当前会话有未完成的会话轮换、WI 进入或未落定的切换，完成后再 /wi switch；未做任何改动。'};
  const sessions={...(saved?.workItemSessions ?? {})};
  const known=sessions[target];
  const runtimeId=saved?.runtime ?? 'cc';
  if(!known && (runtimeId==='codex' || runtimeId==='agy'))
    // Codex and agy have no native identity before their first turn, so the kernel cannot hand it work.
    return {text:(runtimeId==='codex'?'Codex':'agy')+' 运行时下 '+target+' 还没有会话：请先 /new 并发一条消息，再 /wi continue '+target
      +'；之后可用 /wi switch 往返。未做任何改动。'};
  let left:NativeChannelResult={status:'unknown',error:{code:'leave_failed'}};
  for(let attempt=0;attempt<LEAVE_READBACKS&&left.status==='unknown';attempt++){
    try {left=await options.control({ref,...nativeSource(live,poolKey),phase:'leave'});}
    catch(error) {left={status:'unknown',error:{code:error instanceof Error ? error.message : 'leave_failed'}};}
  }
  if(left.error)return left.status==='unknown'
    ? {retained:true,text:'切换未开始：记录当前工作的结果未知（'+left.error.code+'）；原会话保留，可再次 /wi switch '+target+'。'}
    : {text:'切换未开始：记录当前工作失败（'+left.error.code+'）；原会话保留。'};
  if(typeof left.no_switch==='string')return {text:left.no_switch+'。'};
  const leftWi=typeof left.work_item?.id==='string' ? left.work_item.id : left.left ?? undefined;
  const origin=unitOf(saved,live);
  const now=new Date().toISOString();
  if(leftWi)sessions[leftWi]={unit:origin,at:now};
  if(leftWi===target){
    stateManager.updateSession(poolKey,{workItemSessions:sessions}); stateManager.flushStrict();
    return {text:'已在 '+target+' 的会话中，无需切换。'};
  }
  const dest:SessionIdentityUnit=known?.unit ?? {sessionId:(options.newSessionId ?? randomUUID)(),runtime:runtimeId};
  // Written before the session is replaced: a restart or an unknown enter is read back with the
  // same target identity and reference (settleInterruptedSwitch) before anything is cleared.
  const pending:PendingWiSwitch={ref,target,origin,dest,resumed:Boolean(known),...(leftWi?{left:leftWi}:{}),
    ...(options.agentRequest?{agent:true as const}:{}),at:new Date().toISOString()};
  const point=(unit:SessionIdentityUnit,keep?:boolean)=>{
    const patch:any={workItemSessions:sessions,pendingWiSwitch:keep ? pending : undefined};
    for(const key of UNIT_KEYS)patch[key]=unit[key];
    stateManager.updateSession(poolKey,patch);
    stateManager.flushStrict();
  };
  // Inside the slot: no other inbound turn can acquire either session meanwhile.
  // `keep`: enter may have committed; the record stays for the readback.
  const restore=async(reason:string,keep?:boolean):Promise<SwitchOutcome>=>{
    try {
      if(runtime.get(poolKey))await runtime.kill(poolKey);
      point(origin,keep);
      await options.acquire();
    } catch {/* The original unit is persisted first; the next inbound message resumes it. */}
    return {text:'切换未完成（'+reason+'）；已回到原会话'+(leftWi?'，'+leftWi+' 的执行权不变':'')+'。'};
  };
  // The leave call awaited the kernel. From the re-check to the end, the whole switch holds
  // the conversation's acquisition slot: inbound turns wait and then use whichever session stands.
  return replaceConversation(runtime as any,poolKey,async():Promise<SwitchOutcome>=>{
    const now=runtime.get(poolKey);
    if(nativeIdentity(now)!==nativeIdentity(live) || ['busy','starting'].includes(String(now?.state)) || !isLatest()){
      // Nothing was replaced; keep the left WI's session record for a later switch.
      stateManager.updateSession(poolKey,{workItemSessions:sessions}); stateManager.flushStrict();
      return {text:'切换未进行：原会话已有新的动作或新的指令；'+(leftWi?leftWi+' 已记检查点，':'')+'原会话保留。'};
    }
    let started:Live|undefined;
    try {
      point(dest,true);
      await runtime.kill(poolKey);
      if(await options.acquire()!==poolKey)return restore('target_session_unavailable');
      started=runtime.get(poolKey);
    } catch(error) {
      return restore(error instanceof Error ? error.message : 'target_session_unavailable');
    }
    if(!started)return restore('target_session_unavailable');
    let entered:NativeChannelResult|undefined;
    // Enter carries the original input reference, so a retry reads the same request back.
    for(let attempt=0;attempt<ENTER_READBACKS;attempt++){
      try {
        entered=await options.control({ref,...nativeSource(started,poolKey),phase:'enter'});
      } catch(error) {
        if(attempt+1<ENTER_READBACKS)continue;
        entered={status:'unknown',error:{code:error instanceof Error ? error.message : 'enter_failed'}} as NativeChannelResult;
      }
      if(entered.status!=='unknown')break;
    }
    if(entered!.error&&entered!.status==='unknown'){
      // Ask the kernel whether the original enter request committed, as the same target session.
      const committed=await readBack(options.control,{ref,...nativeSource(started,poolKey),phase:'readback'});
      if(committed===false)return restore('enter_not_committed：'+entered!.error.code);
      if(committed===true)entered={status:'local_committed',work_item:{id:target}};
      else
        // Never start business in a session whose authority is unconfirmed: return to the
        // original session and keep the record; the handover pass reads it back until it settles.
        return {...await restore('执行权转移结果未知：'+entered!.error.code,true),retained:true,
          text:'切换未完成：'+target+' 的执行权转移结果未知（'+entered!.error.code+'）；已回到原会话，'
            +'Relay 会按原请求继续读回，落定后在此告知。'};
    }
    if(entered!.error)return restore(entered!.error.code ?? 'enter_failed');
    // Committed: from here the switch stands; bookkeeping failures are reported only.
    let note='';
    try {
      const entered=unitOf(stateManager.getSession(poolKey),started);
      sessions[target]={unit:entered,at:new Date().toISOString()};
      // Settled on the target; the record stays until its continuation is admitted (deliverSwitchContinuation).
      stateManager.updateSession(poolKey,{workItemSessions:sessions,pendingWiSwitch:{...pending,dest:entered,phase:'entered'}});
      stateManager.flushStrict();
    } catch(error) {
      note='\n本地会话对照表写入失败（'+(error instanceof Error ? error.message : 'unknown')+'），切换本身已生效。';
    }
    return {target,resumed:Boolean(known),
      text:'已切换到 '+target+'（'+(known?'恢复其原会话':'新会话')+'）'
        +(leftWi?'；'+leftWi+' 已记检查点，可 /wi switch '+leftWi+' 回来':'')+'。\n'
        +[entered!.work_item?.business_progress, entered!.status].filter(Boolean).join(' · ')+note};
  });
}

/** `phase` absent: the session may be replaced and enter is not settled. `entered`: the conversation
 * stands on `dest` with the target's authority; the continuation is not yet admitted. */
export type PendingWiSwitch = {ref:string;target:string;origin:SessionIdentityUnit;dest:SessionIdentityUnit;
  resumed:boolean;left?:string;agent?:true;at:string;phase?:'entered'};

/** true or false once the kernel answers whether enter committed; undefined while unknown. */
async function readBack(control:Control,request:Parameters<Control>[0]):Promise<boolean|undefined>{
  for(let attempt=0;attempt<ENTER_READBACKS;attempt++){
    try {
      const answer=await control(request);
      if(!answer.error&&typeof answer.committed==='boolean')return answer.committed;
    } catch {/* read again */}
  }
  return undefined;
}

export type SettleOutcome = SwitchOutcome & {settled:'entered'|'returned'|'abandoned'; agent?:true; ref:string};

/** The sessions a switch record may still stand on: origin or dest before enter settled, dest after. */
function recordSessions(pending:PendingWiSwitch):string[] {
  return pending.phase==='entered' ? [pending.dest.sessionId] : [pending.origin.sessionId,pending.dest.sessionId];
}

/**
 * Whether the conversation still stands on a session the record names: the persisted session and,
 * when one runs, the live process. A human /reset or /new, a rollover or any other replacement
 * makes the record stale; it must never be applied to the session that replaced it.
 */
export function switchRecordCurrent(pending:PendingWiSwitch,saved:{sessionId?:string}|undefined,
  live:{sessionId?:string;state?:unknown}|undefined):boolean {
  const names=recordSessions(pending);
  return !!saved?.sessionId&&names.includes(saved.sessionId)
    &&(!live||live.state==='dead'||names.includes(String(live.sessionId)));
}

/**
 * Drop the conversation's switch record without touching its sessions, and log what was dropped:
 * the human replaced the session (/reset, /new), or the record no longer names the current
 * session. Execution authority stays as the kernel recorded it; an Agent's notice for the record
 * is retired by the handover pass because its source session is no longer current.
 */
export function abandonWiSwitch(stateManager:StateManager,poolKey:string,reason:string):PendingWiSwitch|undefined {
  const saved=stateManager.getSession(poolKey);
  const pending=saved?.pendingWiSwitch;
  if(!pending)return undefined;
  stateManager.updateSession(poolKey,{pendingWiSwitch:undefined});
  console.error({ts:new Date().toISOString(),level:'warn',msg:'WI switch record abandoned',poolKey,reason,
    ref:pending.ref,target:pending.target,left:pending.left,phase:pending.phase??'entering',agent:pending.agent===true,
    originSessionId:pending.origin.sessionId,destSessionId:pending.dest.sessionId,currentSessionId:saved?.sessionId});
  return pending;
}

export function abandonedText(pending:PendingWiSwitch):string {
  return '切换到 '+pending.target+' 的未完成记录已作废：对话已换成别的会话，当前会话未改动；'
    +pending.target+' 的执行权以内核为准，可 /wi status '+pending.target+' 核对。';
}

/**
 * A switch left `pendingWiSwitch` before enter settled (Relay stopped after replacing the session,
 * or enter stayed unknown): read back from the conversation's current session whether the
 * original enter request committed. Committed: the conversation settles on the recorded target
 * session, the same identity that entered, and the record moves to `entered` for its
 * continuation. Not committed: it returns to the session it left and the record is cleared.
 * The readback holds no acquisition slot, so inside the slot the record, the native process,
 * its idle state and the latest human input are checked again; any change leaves everything
 * for a later pass. Returns undefined when nothing settled.
 */
export async function settleInterruptedSwitch(options:{poolKey:string;runtime:CommandRuntime;
  stateManager:StateManager;acquire:()=>Promise<string|null>;control:Control;
  /** The conversation's latest human input; a new one stops the settlement. */
  boundary?:()=>string|undefined}):Promise<SettleOutcome|undefined>{
  const {poolKey,runtime,stateManager}=options;
  const pending=stateManager.getSession(poolKey)?.pendingWiSwitch;
  if(!pending||pending.phase==='entered'||switching.has(poolKey))return undefined;
  switching.add(poolKey);
  try {
    let live=runtime.get(poolKey);
    if(!switchRecordCurrent(pending,stateManager.getSession(poolKey),live)){
      abandonWiSwitch(stateManager,poolKey,'session_replaced');
      stateManager.flushStrict();
      return {settled:'abandoned',ref:pending.ref,...(pending.agent?{agent:true as const}:{}),text:abandonedText(pending)};
    }
    if(live&&['busy','starting'].includes(String(live.state)))return undefined;
    if(!live||live.state==='dead'){
      if(await options.acquire()!==poolKey)return undefined;
      live=runtime.get(poolKey);
      if(!live)return undefined;
    }
    const identity=nativeIdentity(live),boundary=options.boundary?.();
    const committed=await readBack(options.control,{ref:pending.ref,...nativeSource(live,poolKey),phase:'readback'});
    if(committed===undefined)return undefined;
    const unit=committed?pending.dest:pending.origin;
    return await replaceConversation(runtime as any,poolKey,async():Promise<SettleOutcome|undefined>=>{
      const saved=stateManager.getSession(poolKey);
      const current=saved?.pendingWiSwitch;
      const now=runtime.get(poolKey);
      if(current?.ref!==pending.ref||current.at!==pending.at||current.phase==='entered'
        ||!switchRecordCurrent(current,saved,now)||nativeIdentity(now)!==identity||['busy','starting'].includes(String(now?.state))
        ||options.boundary&&options.boundary()!==boundary)
        return undefined; // A new turn or input arrived during the readback: keep it all for a later pass.
      const sessions={...(saved?.workItemSessions ?? {})};
      if(committed)sessions[pending.target]={unit:pending.dest,at:new Date().toISOString()};
      const patch:any={workItemSessions:sessions,pendingWiSwitch:committed?{...pending,phase:'entered'}:undefined};
      for(const key of UNIT_KEYS)patch[key]=unit[key];
      if(now&&now.sessionId!==unit.sessionId)await runtime.kill(poolKey);
      stateManager.updateSession(poolKey,patch);
      stateManager.flushStrict();
      if(!runtime.get(poolKey))await options.acquire();
      const base={ref:pending.ref,...(pending.agent?{agent:true as const}:{})};
      return committed
        ? {...base,settled:'entered',target:pending.target,resumed:pending.resumed,
          text:'中断的切换已按原请求读回：已切换到 '+pending.target+'（'+(pending.resumed?'恢复其原会话':'新会话')+'）'
            +(pending.left?'；'+pending.left+' 已记检查点':'')+'。'}
        : {...base,settled:'returned',
          text:'中断的切换未生效（'+pending.target+' 的进入未提交）；已回到原会话'
            +(pending.left?'，'+pending.left+' 的执行权不变':'')+'。'};
    });
  } finally {switching.delete(poolKey);}
}
