import { bindHumanMessageProof, captureHumanParticipation } from './collaboration-capture.js';
import type { StreamJsonMessage } from './types.js';
import { createHash } from 'node:crypto';
import { mkdirSync, readFileSync, writeFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { userInfo } from 'node:os';
import { guardAccountStatePath } from './test-account-path-guard.js';

/** Authenticated channel ingress evidence, captured before Agent admission. */
export interface HumanCommandSource {channel:'feishu'|'telegram';eventId:string;humanRef:string;message:string;observedAtMs?:number}
const inputBoundaries = new Map<string,string>();
function boundaryKey(poolKey:string,env:NodeJS.ProcessEnv):string {return (env["SCL_HUMAN_DECISION_KEY_FILE"] ?? "host")+":"+poolKey;}

/** Persist provenance, not permission. The session Agent interprets the human's
 * decision and supplies this reference to the native WorkItem core. */
function persistHumanInput(source: HumanCommandSource, poolKey: string,
  env: NodeJS.ProcessEnv): {ref:string;observedAtMs:number} {
  if (!source.eventId || !source.humanRef || !source.message.trim()) throw new Error('Human input identity is required');
  const ref = createHash('sha256').update(JSON.stringify([source.channel, source.eventId, source.humanRef, poolKey])).digest('hex');
  const path = humanInputPath(ref, env);
  mkdirSync(dirname(path), {recursive:true, mode:0o700});
  let observedAtMs = Date.now();
  const value = JSON.stringify({source, poolKey, observedAtMs});
  try { writeFileSync(path, value, {flag:'wx', mode:0o600}); }
  catch (error) {
    if ((error as NodeJS.ErrnoException).code !== 'EEXIST') throw error;
    const existing = JSON.parse(readFileSync(path, 'utf8'));
    if (JSON.stringify(existing.source) !== JSON.stringify(source) || existing.poolKey !== poolKey) throw new Error('Human event conflicts with its recorded source');
    observedAtMs = existing.observedAtMs;
  }
  inputBoundaries.set(boundaryKey(poolKey,env),ref);
  captureHumanParticipation(ref, observedAtMs, env);
  return {ref,observedAtMs};
}
export function recordHumanInput(source: HumanCommandSource, poolKey: string,
  env: NodeJS.ProcessEnv = process.env): string {
  return persistHumanInput(source,poolKey,env).ref;
}
/** Retain the proof out of band until the real manager fixes this dispatch's WI.
 * The legacy string helper remains for consumers that do not dispatch a turn.
 */
export function annotatedHumanMessage(content:string,source:HumanCommandSource|undefined,poolKey:string,
  env:NodeJS.ProcessEnv=process.env):StreamJsonMessage {
  invalidateHumanInput(poolKey,env);
  const message:StreamJsonMessage={type:'human',content};
  if(!source)return message;
  try {
    const proof=persistHumanInput(source,poolKey,env);
    message.content=content+humanInputContext(proof.ref);
    bindHumanMessageProof(message,proof);
  } catch {
    message.content=content+'\n\n[SCL_HOST_ADAPTER_DEGRADED: human input provenance was not saved; conversation remains available, no recovery receipt was issued.]';
  }
  return message;
}
/** A photo, voice, audio or file message carries the same provenance as text.
 * The person's own words close the message, then the reference, so the
 * provenance reader sees exactly what the person sent before it. */
export function annotatedMediaMessage(message:StreamJsonMessage,words:string,source:HumanCommandSource|undefined,
  poolKey:string,env:NodeJS.ProcessEnv=process.env):StreamJsonMessage {
  invalidateHumanInput(poolKey,env);
  let proof:ReturnType<typeof persistHumanInput>|undefined;
  let suffix='';
  if(source){
    try { proof=persistHumanInput(source,poolKey,env); suffix=humanInputContext(proof.ref); }
    catch { suffix='\n\n[SCL_HOST_ADAPTER_DEGRADED: human input provenance was not saved; conversation remains available, no recovery receipt was issued.]'; }
  }
  let result:StreamJsonMessage;
  if(Array.isArray(message.content)){
    const blocks=[...message.content];
    const prior=blocks.some(block=>!!block&&typeof block==='object'&&(block as {type?:unknown}).type==='text');
    const text=(prior?'\n\n':'')+words+suffix;
    if(text)blocks.push({type:'text',text});
    result={...message,content:blocks};
  } else {
    result={...message,content:String(message.content??'')+suffix};
  }
  if(proof)bindHumanMessageProof(result,proof);
  return result;
}
export function latestHumanInput(poolKey:string,env:NodeJS.ProcessEnv=process.env):string|undefined {
  return inputBoundaries.get(boundaryKey(poolKey,env));
}
export function invalidateHumanInput(poolKey:string,env:NodeJS.ProcessEnv=process.env):void {
  inputBoundaries.delete(boundaryKey(poolKey,env));
}
function humanInputPath(ref: string, env: NodeJS.ProcessEnv): string {
  if (!/^[a-f0-9]{64}$/.test(ref)) throw new Error('Invalid human input reference');
  return guardAccountStatePath(join(humanInputsDir(env), ref+'.json'));
}
/** The core's human-input custody; its sibling `native-session-handovers` belongs to the core's session controller. */
export function humanInputsDir(env: NodeJS.ProcessEnv = process.env): string {
  if (env.PULSE_CORE_HUMAN_INPUTS) return env.PULSE_CORE_HUMAN_INPUTS;
  const keyPath = env['SCL_HUMAN_DECISION_KEY_FILE'] || join(userInfo().homedir, '.local/state/agent-harness/scl-host/human-decision.key');
  return join(dirname(keyPath), 'human-inputs');
}
export function readHumanInput(ref: string, poolKey: string, channel: HumanCommandSource['channel'],
  humanRef: string, env: NodeJS.ProcessEnv = process.env): HumanCommandSource {
  const record = JSON.parse(readFileSync(humanInputPath(ref, env), 'utf8'));
  if (record.poolKey !== poolKey || record.source?.channel !== channel || record.source?.humanRef !== humanRef)
    throw new Error('Human input belongs to another conversation');
  return {...record.source, observedAtMs:record.observedAtMs};
}
/** Restore an authenticated durable boundary after Host restart. The caller
 * must first verify it is the latest durable input for the unchanged Session.
 * A concurrently received message always wins; no receipt or timestamp changes.
 */
export function restoreHumanInputBoundary(ref:string,poolKey:string,channel:HumanCommandSource['channel'],
  humanRef:string,env:NodeJS.ProcessEnv=process.env):HumanCommandSource{
  const source=readHumanInput(ref,poolKey,channel,humanRef,env);
  const current=latestHumanInput(poolKey,env);
  if(current&&current!==ref)throw new Error('A newer live human input supersedes the durable recovery boundary');
  inputBoundaries.set(boundaryKey(poolKey,env),ref);
  return source;
}
/** States where the message came from and nothing more: it neither grants nor
 * withdraws the Agent's authority to decide matters within its own role. Readers
 * match only the `[Host human input reference: <ref>.` prefix. */
export function humanInputContext(ref: string): string {
  return `\n\n[Host human input reference: ${ref}. This records message provenance.]`;
}
