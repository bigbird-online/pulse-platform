/** Host transport only. No WI decisions, metering gates or lifecycle cache. */
import {execFileSync} from 'node:child_process';
import {relayChildEnv} from './relay-secrets.js';
import {readFileSync,readdirSync,mkdirSync,statSync,realpathSync,writeFileSync,renameSync,rmSync,existsSync} from 'node:fs';
import {dirname,join} from 'node:path';
import {randomUUID} from 'node:crypto';
import {fileURLToPath} from 'node:url';
import {managedRelayPath} from './managed-paths.js';
import {nodeWorkItemCore} from './native-work-item-command.js';
import {parseConversationPoolKey} from './conversation-address.js';
import {issueAgentJobsKey} from './agent-jobs-auth.js';

export function nativeCoreRuntime(tenant: string, handoverRef?:string) {
  const host=nodeWorkItemCore();
  if(!host) return undefined;
  const data=managedRelayPath('dataDir');
  if(!data) throw new Error('Native core requires the registered Relay data directory');
  // pulse_jobs signs with this conversation's key (agent-jobs-auth.ts); only the path travels.
  return coreBridge(host,{...coreEnvironment(host,data),PULSE_AGENT_JOBS_KEY_FILE:issueAgentJobsKey(data,tenant),
    PULSE_PI_TENANT_ID:tenant,...(handoverRef?{PULSE_NATIVE_HANDOVER_REF:handoverRef}:{})});
}

type CoreHost=NonNullable<ReturnType<typeof nodeWorkItemCore>>;

function coreEnvironment(host:CoreHost,data:string) {
  // pulse_jobs reaches this Relay's internal endpoint; address only, never the internal token.
  const relay=Object.fromEntries(['RELAY_HTTP_HOST','RELAY_HTTP_PORT']
    .flatMap(name=>process.env[name]?[[name,process.env[name]!]]:[]));
  return {PULSE_WORK_ITEM_CORE_HOST:JSON.stringify(host),PULSE_CORE_REQUEST_STATE_ROOT:join(data,'native-requests'),...relay};
}

function coreBridge<E extends ReturnType<typeof coreEnvironment>>(host:CoreHost,env:E) {
  return {env,server:{command:process.execPath,
    args:[join(dirname(host.extension),'cc-work-item-core.mjs')],env}};
}

/** This release's ComputeCost request-purpose server (the one CC sessions also start). */
export const REQUEST_PURPOSE_SERVER = join(dirname(dirname(dirname(fileURLToPath(import.meta.url)))),
  'ComputeCost', 'runtime', 'codex-mcp-request-purpose.py');

/** Codex reads `declare_session_wi` from this server; Relay registers it on every
 * host instead of relying on a hand-edited ~/.codex/config.toml. */
export function codexPurposeArguments(python: string, server: string = REQUEST_PURPOSE_SERVER): string[] {
  if (!existsSync(server)) return [];
  return Object.entries({command:python,args:[server],enabled_tools:['declare_session_wi'],
    // A declaration writes nothing and grants nothing; it only names the request's WI.
    'tools.declare_session_wi.approval_mode':'approve'})
    .flatMap(([key,value])=>['-c','mcp_servers.pulse-request-purpose.'+key+'='+JSON.stringify(value)]);
}

export function codexCoreArguments(tenant: string, handoverRef?:string): string[] {
  const binding=nativeCoreRuntime(tenant,handoverRef);
  return binding?codexBridgeArguments(binding.server):[];
}

/**
 * A scheduled Codex run registers the same two servers as a conversation, so a
 * reviewer job can declare its WI on a node without a hand-written
 * ~/.codex/config.toml. The bridge carries the core environment itself and
 * claims no tenant: the core reads the run from its scheduler proof. Without
 * the Relay data directory the run starts without either server.
 */
export function scheduledCodexCoreArguments(): string[] {
  const host=nodeWorkItemCore();
  const data=host&&managedRelayPath('dataDir');
  return host&&data?codexBridgeArguments(coreBridge(host,coreEnvironment(host,data)).server):[];
}

function codexBridgeArguments({command,args,env}:{command:string;args:string[];
  env:{PULSE_WORK_ITEM_CORE_HOST:string}&Record<string,string>}): string[] {
  return [...Object.entries({command,args,enabled_tools:['pulse_work_item','pulse_stats','pulse_jobs'],tool_timeout_sec:180,
    // The installed bridge is pre-approved; the core still adjudicates every WI intent.
    'tools.pulse_work_item.approval_mode':'approve',
    // Read-only Stats query; listed by the server only where Stats is installed.
    'tools.pulse_stats.approval_mode':'approve',
    // Only the caller's own jobs; Relay enforces owner and limits.
    'tools.pulse_jobs.approval_mode':'approve',...Object.fromEntries(Object.entries(env).map(([k,v])=>['env.'+k,v]))})
    .flatMap(([key,value])=>['-c','mcp_servers.pulse.'+key+'='+JSON.stringify(value)]),
    ...codexPurposeArguments(JSON.parse(env.PULSE_WORK_ITEM_CORE_HOST).python)];
}

export function nativeProcessStamp(pid:number) {
  if(process.platform==='linux') {
    const stat=readFileSync(`/proc/${pid}/stat`,'utf8').split(')').at(-1)!.trim().split(/\s+/);
    return {pid,start_ticks:stat[19],boot:readFileSync('/proc/sys/kernel/random/boot_id','utf8').trim()};
  }
  if(process.platform==='darwin') {
    const started=execFileSync('ps',['-p',String(pid),'-o','lstart='],{encoding:'utf8',timeout:5000,env:relayChildEnv()}).trim();
    if(!started) throw new Error('Native runtime ended before session source observation');
    return {pid,started};
  }
  throw new Error('Unsupported native process source');
}

function proofRoot():string {
  return join(managedRelayPath('dataDir')!,'native-session-proofs');
}

function proofPath(pid:number):string {
  const root=proofRoot();
  mkdirSync(root,{recursive:true,mode:0o700});
  const info=statSync(root);
  if(realpathSync(root)!==root || info.uid!==process.getuid?.() || info.mode&0o077)
    throw new Error('Native source proof directory must be private');
  return join(root,String(pid)+'.json');
}

function writeProof(pid:number,sessionId:string,fields:Record<string,unknown>):{path:string;process:string} {
  if(!/^[A-Za-z0-9-]{1,128}$/.test(sessionId)) throw new Error('Invalid native session source');
  const target=proofPath(pid);
  const stamp=nativeProcessStamp(pid);
  const record={schema:'pulse.native-session-source/v1',...fields,process:stamp,
    relay_pid:process.pid,session_id:sessionId};
  const temporary=target+'.'+randomUUID();
  writeFileSync(temporary,JSON.stringify(record)+'\n',{flag:'wx',mode:0o600});
  renameSync(temporary,target);
  return {path:target,process:JSON.stringify(stamp)};
}

/** The latest proof this Relay wrote for each live conversation process. */
const conversationProofs=new Map<number,{path:string;sessionId:string;process:string}>();

/** The original runtime's stdout names its thread; process birth prevents PID reuse. */
export function observeNativeSession(pid:number,sessionId:string,tenant:string,runtime:'codex'|'cc'|'pi',allocationId=sessionId) {
  if(!nativeCoreRuntime(tenant)) return;
  // The entry is read from Relay's own pool key; the core never parses it.
  const address=parseConversationPoolKey(tenant);
  const entry=address?(address.channelScope==='feishu'?'feishu':'telegram'):'relay';
  const written=writeProof(pid,sessionId,{runtime,allocation_id:allocationId,tenant,entry});
  conversationProofs.set(pid,{...written,sessionId});
}

/**
 * Remove a conversation process's proof once it exits. Call once at spawn;
 * a process that was never observed has nothing to remove. A failed removal is
 * logged: the core ignores a proof whose process birth differs, and the next
 * Relay start sweeps it.
 */
export function forgetNativeProofOnExit(child:{pid?:number;once(event:'exit',listener:()=>void):unknown}):void {
  child.once('exit',()=>{
    const pid=child.pid;
    const written=pid===undefined?undefined:conversationProofs.get(pid);
    if(!written) return;
    conversationProofs.delete(pid!);
    try { forgetNativeSession(written.path,written.sessionId,written.process); }
    catch(error) {
      console.log({ts:new Date().toISOString(),level:'warn',msg:'Native session proof not removed',pid,error:String(error)});
    }
  });
}

/** The scheduler run that started a native runtime; non-secret and never a pool key. */
export interface ScheduledNativeSession {
  jobId: string;
  owner: string;
  runId?: string;
  /** Public job name, run start and configured WI for the ComputeCost record. */
  jobName?: string;
  startedAt?: string;
  wi?: string;
}

/**
 * A scheduler run is not a conversation. Its proof names the process the
 * scheduler started (the runtime itself or its launcher), the job and its
 * owner; the core takes only the session ID from it and never matches its
 * tenant against a conversation profile. Returns the proof path, if written.
 */
export function observeScheduledSession(pid:number,sessionId:string,runtime:'codex'|'cc',run:ScheduledNativeSession):string|undefined {
  if(!nodeWorkItemCore()) return undefined;
  if(!managedRelayPath('dataDir')) throw new Error('Native core requires the registered Relay data directory');
  return writeProof(pid,sessionId,{runtime,allocation_id:run.runId??sessionId,
    tenant:'scheduler:'+run.jobId,entry:'scheduler',owner:run.owner}).path;
}

/** Remove this Relay's own proof for a finished process; a reused PID's newer proof stays. */
export function forgetNativeSession(path:string,sessionId:string,processStamp?:string):void {
  try {
    const record=JSON.parse(readFileSync(path,'utf8')) as {relay_pid?:unknown;session_id?:unknown;process?:unknown};
    if(record.relay_pid===process.pid && record.session_id===sessionId
      && (processStamp===undefined || JSON.stringify(record.process)===processStamp)) rmSync(path,{force:true});
  } catch(error) {
    if((error as NodeJS.ErrnoException).code!=='ENOENT') throw error;
  }
}

function processAlive(pid:number):boolean {
  try { process.kill(pid,0); return true; }
  catch(error) { return (error as NodeJS.ErrnoException).code==='EPERM'; }
}

/**
 * At Relay start, remove proofs that have plainly no owner: the named process
 * has exited or is another process under a reused PID, or the Relay that wrote
 * it is neither this Relay nor alive. A proof whose process and Relay are both
 * alive may belong to another Relay on this data directory and stays. Each
 * failure is logged and never blocks start. Returns the number removed.
 */
export function sweepNativeSessionProofs():number {
  if(!managedRelayPath('dataDir')) return 0;
  const root=proofRoot();
  const warn=(msg:string,fields:Record<string,unknown>)=>
    console.log({ts:new Date().toISOString(),level:'warn',msg,...fields});
  let names:string[];
  try { names=readdirSync(root); }
  catch(error) {
    if((error as NodeJS.ErrnoException).code!=='ENOENT') warn('Native session proof sweep skipped',{error:String(error)});
    return 0;
  }
  let removed=0;
  for(const name of names) {
    const match=/^([1-9][0-9]*)\.json$/.exec(name);
    if(!match) continue;
    const path=join(root,name);
    try {
      const pid=Number(match[1]);
      const record=JSON.parse(readFileSync(path,'utf8')) as {relay_pid?:unknown;process?:unknown};
      let owned=processAlive(pid);
      if(owned) {
        try { owned=JSON.stringify(nativeProcessStamp(pid))===JSON.stringify(record.process); }
        catch { owned=processAlive(pid); }
      }
      if(owned && record.relay_pid!==process.pid)
        owned=typeof record.relay_pid==='number' && processAlive(record.relay_pid);
      if(owned) continue;
      rmSync(path,{force:true});
      removed++;
    } catch(error) {
      warn('Native session proof not swept',{proof:name,error:String(error)});
    }
  }
  return removed;
}
