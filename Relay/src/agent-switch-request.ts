/**
 * An Agent's own `/wi switch`. After the holder session commits `request_switch`, the core
 * leaves a private notice naming that request and its target. The notice only tells Relay
 * which conversation to move; the core reads the committed request back in both switch
 * phases, so the notice grants nothing.
 */
import {lstatSync,readdirSync,readFileSync,unlinkSync} from 'node:fs';
import {dirname,join} from 'node:path';
import {guardAccountStatePath} from './test-account-path-guard.js';
import {humanInputsDir} from './scl-human-decision.js';
import {switchInProgress} from './native-wi-switch.js';
import type {StateManager} from './types.js';
import type {CommandRuntime} from './commands.js';

export type AgentSwitchRequest={ref:string;wi:string;target:string;tenant:string;sourceNativeId:string};

export function agentSwitchRequestDir(env:NodeJS.ProcessEnv=process.env):string{
  return guardAccountStatePath(join(dirname(humanInputsDir(env)),'native-session-handovers','agent-switches'));
}

/** Private, bounded notices only; anything else is left for the core to judge. */
export function readAgentSwitchRequests(dir:string):AgentSwitchRequest[]{
  let names:string[];
  try{names=readdirSync(dir);}catch(error){if((error as NodeJS.ErrnoException).code==='ENOENT')return [];throw error;}
  const found:AgentSwitchRequest[]=[];
  for(const name of names.sort()){
    if(!/^[a-f0-9]{64}\.json$/.test(name))continue;
    const path=join(dir,name);
    try{
      const info=lstatSync(path);
      if(!info.isFile()||info.uid!==process.getuid?.()||info.mode&0o077||info.size>4096)continue;
      const row=JSON.parse(readFileSync(path,'utf8'));
      if(row?.schema!=='harness.agent-switch-request/v1'||row.ref+'.json'!==name||typeof row.wi!=='string'
        ||typeof row.target!=='string'||!/^wi_[A-Za-z0-9_]+$/.test(row.target)
        ||typeof row.tenant!=='string'||typeof row.source_native_id!=='string')continue;
      found.push({ref:row.ref,wi:row.wi,target:row.target,tenant:row.tenant,sourceNativeId:row.source_native_id});
    }catch{/* An unreadable notice is skipped; the core never relies on it. */}
  }
  return found;
}

type Start=(request:AgentSwitchRequest)=>Promise<{retained?:true}|unknown>;

/**
 * Run each pending Agent switch once its source conversation is idle.
 * - While the conversation has an unsettled switch (`pendingWiSwitch`), its notices wait:
 *   `settleInterruptedSwitch` reads that switch back first. When it returns to the session
 *   it left, the notice is still here and runs again from that known state.
 * - A notice whose source session is no longer this conversation's session is removed unrun.
 * - A busy source, or a rollover or WI entry in flight, waits for a later pass.
 * After a run the notice is removed unless the run left it unsettled (`retained`, or a
 * `pendingWiSwitch` for this notice); a thrown run keeps it.
 */
export async function adoptAgentSwitchRequests(input:{dir:string;stateManager:StateManager;
  runtime:Pick<CommandRuntime,'get'>;start:Start}):Promise<string[]>{
  const started:string[]=[];
  for(const request of readAgentSwitchRequests(input.dir)){
    const {tenant}=request;
    const session=input.stateManager.getSession(tenant);
    if(!session||switchInProgress(tenant)||session.pendingWiSwitch)continue;
    const retire=()=>{try{unlinkSync(join(input.dir,request.ref+'.json'));}catch{/* already gone */}};
    const live=input.runtime.get(tenant);
    if(live&&['busy','starting'].includes(String(live.state)))continue;
    if(session.pendingNativeRollover||session.pendingWorkItemEntry)continue;
    const nativeId=live&&live.state!=='dead'
      ? ('codexThreadId' in live?live.codexThreadId:undefined)??live.sessionId
      : undefined;
    const current=nativeId!==undefined ? nativeId===request.sourceNativeId
      : session.sessionId===request.sourceNativeId||session.codexThreadId===request.sourceNativeId;
    if(!current){retire();continue;}
    try{
      const outcome=await input.start(request) as {retained?:true}|undefined;
      started.push(request.ref);
      if(!outcome?.retained&&input.stateManager.getSession(tenant)?.pendingWiSwitch?.ref!==request.ref)retire();
    }catch(error){
      console.error({ts:new Date().toISOString(),level:'error',msg:'Agent switch request retained',poolKey:tenant,
        code:error instanceof Error?error.message:'agent_switch_unknown'});
    }
  }
  return started;
}
