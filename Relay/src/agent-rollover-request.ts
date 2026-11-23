/**
 * An Agent's own rollover. After the holder session commits `request_rollover`,
 * the core leaves a private notice naming that request. The notice only tells
 * Relay which conversation to change; the core's session controller reads the
 * committed request back and re-checks it before it fences the source.
 */
import {lstatSync,readdirSync,readFileSync,unlinkSync} from 'node:fs';
import {dirname,join} from 'node:path';
import {guardAccountStatePath} from './test-account-path-guard.js';
import {humanInputsDir} from './scl-human-decision.js';
import {switchInProgress} from './native-wi-switch.js';
import {conversationNativeIds,coreNativeId,type NativeIdSource} from './session-identity.js';
import type {StateManager} from './types.js';
import type {CommandRuntime} from './commands.js';

export type AgentRolloverRequest={ref:string;wi:string;tenant:string;sourceNativeId:string};

export function agentRolloverRequestDir(env:NodeJS.ProcessEnv=process.env):string{
  return guardAccountStatePath(join(dirname(humanInputsDir(env)),'native-session-handovers','agent-requests'));
}

/** Private, bounded notices only; anything else is left for the core to judge. */
export function readAgentRolloverRequests(dir:string):AgentRolloverRequest[]{
  let names:string[];
  try{names=readdirSync(dir);}catch(error){if((error as NodeJS.ErrnoException).code==='ENOENT')return [];throw error;}
  const found:AgentRolloverRequest[]=[];
  for(const name of names.sort()){
    if(!/^[a-f0-9]{64}\.json$/.test(name))continue;
    const path=join(dir,name);
    try{
      const info=lstatSync(path);
      if(!info.isFile()||info.uid!==process.getuid?.()||info.mode&0o077||info.size>4096)continue;
      const row=JSON.parse(readFileSync(path,'utf8'));
      if(row?.schema!=='harness.agent-rollover-request/v1'||row.ref+'.json'!==name||typeof row.wi!=='string'
        ||typeof row.tenant!=='string'||typeof row.source_native_id!=='string')continue;
      found.push({ref:row.ref,wi:row.wi,tenant:row.tenant,sourceNativeId:row.source_native_id});
    }catch{/* An unreadable notice is skipped; the core never relies on it. */}
  }
  return found;
}

type Start=(request:AgentRolloverRequest)=>Promise<{retiredUnprepared?:true}|unknown>;

/**
 * Start each pending Agent rollover whose source conversation is current and idle, or has no
 * live instance while this host's persisted session still names it.
 * A notice whose rollover already happened, or whose source conversation is gone,
 * is removed. A busy source waits for its turn to end.
 */
export async function adoptAgentRolloverRequests(input:{dir:string;stateManager:StateManager;
  runtime:Pick<CommandRuntime,'get'>;start:Start}):Promise<string[]>{
  const started:string[]=[];
  for(const request of readAgentRolloverRequests(input.dir)){
    const session=input.stateManager.getSession(request.tenant);
    if(!session)continue; // Another Relay, or not this host's conversation.
    const retire=()=>{try{unlinkSync(join(input.dir,request.ref+'.json'));}catch{/* already gone */}};
    // A /wi switch in flight or unsettled owns the conversation's session; the rollover waits for it.
    if(switchInProgress(request.tenant)||session.pendingWiSwitch)continue;
    if(session.pendingNativeRollover){
      continue; // One rollover at a time; this one or another is in flight.
    }
    if(session.nativeSessionHistory?.some(h=>h.rolloverRef===request.ref)){retire();continue;}
    const live=input.runtime.get(request.tenant);
    const nativeId=live&&coreNativeId(live as NativeIdSource);
    if(!live||live.state==='dead'){
      // A Relay restart or exit can leave the idle source with no instance; its persisted identity
      // still names the source, so the rollover recovers it the way a pending handover would.
      if(!conversationNativeIds(session).includes(request.sourceNativeId)){retire();continue;}
    }else{
      if(nativeId!==request.sourceNativeId){retire();continue;}
      if(['busy','starting'].includes(live.state))continue;
    }
    try{
      const result=await input.start(request);
      started.push(request.ref);
      const after=input.stateManager.getSession(request.tenant);
      // Settled either way: the successor holds the WI, or the core refused and nothing was fenced.
      if((result as {retiredUnprepared?:true}|undefined)?.retiredUnprepared
        ||after?.nativeSessionHistory?.some(h=>h.rolloverRef===request.ref)&&!after.pendingNativeRollover)retire();
    }catch(error){
      console.error({msg:'Agent rollover request retained',poolKey:request.tenant,
        code:error instanceof Error?error.message:'agent_rollover_unknown'});
    }
  }
  return started;
}
