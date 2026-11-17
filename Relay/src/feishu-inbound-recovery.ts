import { createHash, randomUUID } from 'node:crypto';
import { closeSync, existsSync, fsyncSync, mkdirSync, openSync, readFileSync, readdirSync, renameSync, statSync, writeFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { userInfo } from 'node:os';
import { guardAccountStatePath } from './test-account-path-guard.js';
import { humanInputContext, latestHumanInput, readHumanInput, type HumanCommandSource } from './scl-human-decision.js';
import { parseConversationPoolKey } from './conversation-address.js';
import type { PersistedSession } from './types.js';

/** Channel-level recovery failure with a public code. */
export class FeishuRecoveryError extends Error {
  constructor(readonly code: string) { super(code); }
}

function hostRoot(env: NodeJS.ProcessEnv): string {
  return guardAccountStatePath(dirname(env.SCL_HUMAN_DECISION_KEY_FILE ?? join(userInfo().homedir, '.local/state/agent-harness/scl-host/human-decision.key')));
}

function durableHostProjection(path: string, value: unknown): void {
  const missing: string[] = [];
  for (let directory = dirname(path); !existsSync(directory); directory = dirname(directory)) missing.push(directory);
  mkdirSync(dirname(path), { recursive: true, mode: 0o700 });
  for (const directory of missing.reverse()) {
    const parent = openSync(dirname(directory), 'r');
    try { fsyncSync(parent); } finally { closeSync(parent); }
  }
  const tmp = path + '.' + randomUUID() + '.tmp', fd = openSync(tmp, 'wx', 0o600);
  try { writeFileSync(fd, JSON.stringify(value)); fsyncSync(fd); } finally { closeSync(fd); }
  renameSync(tmp, path);
  const directory = openSync(dirname(path), 'r');
  try { fsyncSync(directory); } finally { closeSync(directory); }
}

const CONSUMER_CONTROL_COMMANDS = new Set(['stop', 'kill', 'new', 'reset', 'restart', 'runtime', 'wi', 'compact', 'usage_seal']);
/** Match the channel's explicit slash/action commands, never prose keywords. */
export function feishuRecoveryControlCommand(text: string): string | undefined {
  if (!text.startsWith('/')) return;
  const command = text.slice(1).split(/\s+/)[0]!;
  return CONSUMER_CONTROL_COMMANDS.has(command) ? command : undefined;
}
interface RecoveryBoundary {
  poolKey: string; eventId: string; observedAtMs: number; kind?: 'input' | 'control' | 'unknown';
  consumerControl?: { eventId: string; observedAtMs: number; command: string };
}
/** Bind a foreground startup retry to the ingress that requested it. */
export function feishuRecoveryBoundaryToken(poolKey: string, env: NodeJS.ProcessEnv = process.env): string | undefined {
  try {
    return createHash('sha256').update(readFileSync(join(hostRoot(env), 'feishu-input-boundaries',
      createHash('sha256').update(poolKey).digest('hex') + '.json'))).digest('hex');
  } catch (error) { if ((error as NodeJS.ErrnoException).code === 'ENOENT') return; throw error; }
}
/** Durable invalidation only, never an authorization receipt. This also
 * covers non-text input and Stop buttons that do not produce an Agent turn. */
export function recordFeishuRecoveryBoundary(poolKey: string, eventId: string, env: NodeJS.ProcessEnv = process.env,
  input?: { kind: 'input' | 'control'; control?: string }): void {
  const path = join(hostRoot(env), 'feishu-input-boundaries', createHash('sha256').update(poolKey).digest('hex') + '.json');
  let previous: RecoveryBoundary | undefined;
  try { previous = JSON.parse(readFileSync(path, 'utf8')) as RecoveryBoundary; }
  catch (error) { if ((error as NodeJS.ErrnoException).code !== 'ENOENT') throw error; }
  const cardControl = (value: string) => value.startsWith('card-menu:')
    ? feishuRecoveryControlCommand('/' + value.slice('card-menu:'.length)) : undefined;
  const control = input?.control ?? cardControl(eventId);
  const observedAtMs = Date.now();
  const priorControl = previous?.consumerControl ?? (previous && cardControl(previous.eventId)
    ? { eventId: previous.eventId, observedAtMs: previous.observedAtMs, command: cardControl(previous.eventId)! } : undefined);
  const value: RecoveryBoundary = { poolKey, eventId, observedAtMs, kind: control ? 'control' : input?.kind ?? 'unknown',
    ...((control || input?.kind === 'control') ? { consumerControl: { eventId, observedAtMs, command: control ?? 'control' } }
      : priorControl ? { consumerControl: priorControl } : {}) };
  durableHostProjection(path, value);
}

export interface FeishuInboundRecoveryRequest {
  poolKey: string;
  sessionId: string;
  humanInputRef: string;
  code?: string;
}
export type FeishuInboundDispatchEvidence = 'accepted' | 'proven_unsent' | 'unknown';
export interface FeishuInboundRecoveryReadback {
  requestId: string;
  state: 'pending' | 'dispatching' | 'accepted' | 'uncertain' | 'superseded';
  error?: string;
  startupAttempts?: number;
  nextAttemptAt?: number;
  requiresRuntimeRepair?: boolean;
}
interface InboundRecord extends FeishuInboundRecoveryRequest, FeishuInboundRecoveryReadback {
  schema: 'pulse_feishu_inbound_recovery/v1';
  agentId: string;
  eventId: string;
  sourceSha256: string;
  consumerSha256: string;
  consumerVersion?: 2;
  runtimeRevision?: string;
  boundaryToken: string;
  attempts: number;
  nextAttemptAt: number;
  origin: 'observed_startup_failure' | 'verified_legacy_startup_failure';
}
export interface FeishuInboundRecoveryDependencies {
  agentId: string;
  env?: NodeJS.ProcessEnv;
  session: (poolKey: string) => PersistedSession | undefined;
  owns: (poolKey: string) => boolean;
  busy: (poolKey: string) => boolean;
  /** Exact deployed code/config identity. A repair reopens the same input's
   * bounded startup budget; metering state must never be part of this identity. */
  runtimeRevision?: () => string;
  ensureRuntime: (poolKey: string, sessionId: string, boundaryToken: string) => Promise<void>;
  dispatch: (poolKey: string, content: string, idempotencyKey: string) => Promise<void>;
  /** proven_unsent requires positive Host startup-failure evidence and a
   * complete original native history. A missing marker alone is unknown. */
  readDispatchEvidence: (poolKey: string, sessionId: string, inputRef: string,
    marker: string) => Promise<FeishuInboundDispatchEvidence>;
}
export class FeishuInboundRecoveryError extends Error {
  constructor(readonly code: string) { super(code); }
}
const digest = (value: unknown) => createHash('sha256').update(JSON.stringify(value)).digest('hex');
const safeCode = (error: unknown, fallback: string) => {
  const code = (error as { code?: unknown; publicCode?: unknown } | null)?.publicCode
    ?? (error as { code?: unknown } | null)?.code;
  return typeof code === 'string' && /^[a-z][a-z0-9_]{0,63}$/.test(code) ? code : fallback;
};

/** Recover an authenticated text that failed before dispatch. Runtime recovery
 * and business dispatch are separate receipts; uncertain dispatch never replays. */
export class FeishuInboundRecovery {
  private readonly root: string;
  private readonly env: NodeJS.ProcessEnv;
  private readonly inFlight = new Map<string, Promise<FeishuInboundRecoveryReadback>>();
  constructor(private readonly deps: FeishuInboundRecoveryDependencies) {
    this.env = { ...process.env, ...deps.env };
    this.root = guardAccountStatePath(dirname(this.env.SCL_HUMAN_DECISION_KEY_FILE
      ?? join(userInfo().homedir, '.local/state/agent-harness/scl-host/human-decision.key')));
  }
  private id(input: FeishuInboundRecoveryRequest): string {
    return 'operation_v1_feishu_inbound_' + digest([input.poolKey, input.sessionId, input.humanInputRef]);
  }
  private path(id: string): string { return join(this.root, 'feishu-inbound-recoveries', id + '.json'); }
  private read(id: string): InboundRecord | undefined {
    try {
      const value = JSON.parse(readFileSync(this.path(id), 'utf8')) as InboundRecord;
      if (value.schema !== 'pulse_feishu_inbound_recovery/v1' || value.requestId !== id || this.id(value) !== id)
        throw new FeishuInboundRecoveryError('inbound_recovery_record_invalid');
      return value;
    } catch (error) { if ((error as NodeJS.ErrnoException).code === 'ENOENT') return; throw error; }
  }
  private save(record: InboundRecord): void {
    const path = this.path(record.requestId);
    mkdirSync(dirname(path), { recursive: true, mode: 0o700 });
    const temp = path + '.' + randomUUID() + '.tmp';
    const fd = openSync(temp, 'wx', 0o600);
    try { writeFileSync(fd, JSON.stringify(record)); fsyncSync(fd); } finally { closeSync(fd); }
    renameSync(temp, path);
    const directory = openSync(dirname(path), 'r');
    try { fsyncSync(directory); } finally { closeSync(directory); }
  }
  private result(record: InboundRecord): FeishuInboundRecoveryReadback {
    return { requestId: record.requestId, state: record.state, ...(record.error ? { error: record.error } : {}),
      startupAttempts: record.startupAttempts ?? 0, nextAttemptAt: record.nextAttemptAt,
      requiresRuntimeRepair: record.requiresRuntimeRepair === true };
  }
  private source(input: FeishuInboundRecoveryRequest): { source: HumanCommandSource; boundary: string } {
    const address = parseConversationPoolKey(input.poolKey);
    if (!address || address.agentId !== this.deps.agentId
      || !/^[a-f0-9]{64}$/.test(input.humanInputRef) || !this.deps.owns(input.poolKey))
      throw new FeishuInboundRecoveryError('inbound_identity_mismatch');
    const source = readHumanInput(input.humanInputRef, input.poolKey, 'feishu', address.userId, this.env);
    if (digest([source.channel, source.eventId, source.humanRef, input.poolKey]) !== input.humanInputRef
      || !source.message.trim() || source.message.trimStart().startsWith('/') || !Number.isSafeInteger(source.observedAtMs))
      throw new FeishuInboundRecoveryError('inbound_source_invalid');
    for (const name of readdirSync(join(this.root, 'human-inputs'))) {
      if (!/^[a-f0-9]{64}\.json$/.test(name) || name === input.humanInputRef + '.json') continue;
      const other = JSON.parse(readFileSync(join(this.root, 'human-inputs', name), 'utf8'));
      if (other.poolKey === input.poolKey && (!Number.isSafeInteger(other.observedAtMs)
        || other.observedAtMs >= source.observedAtMs!))
        throw new FeishuInboundRecoveryError('human_input_superseded');
    }
    const live = latestHumanInput(input.poolKey, this.env);
    if (live && live !== input.humanInputRef) throw new FeishuInboundRecoveryError('human_input_superseded');
    const boundaryPath = join(this.root, 'feishu-input-boundaries', createHash('sha256').update(input.poolKey).digest('hex') + '.json');
    const boundary = JSON.parse(readFileSync(boundaryPath, 'utf8'));
    if (!Number.isSafeInteger(boundary.observedAtMs)
      || (boundary.consumerControl && !Number.isSafeInteger(boundary.consumerControl.observedAtMs)))
      throw new FeishuInboundRecoveryError('inbound_boundary_unavailable');
    if (boundary.poolKey !== input.poolKey || boundary.eventId !== source.eventId || boundary.kind === 'control'
      || (boundary.consumerControl && boundary.consumerControl.eventId !== source.eventId
        && boundary.consumerControl.observedAtMs >= source.observedAtMs!))
      throw new FeishuInboundRecoveryError('human_input_superseded');
    const token = feishuRecoveryBoundaryToken(input.poolKey, this.env);
    if (!token) throw new FeishuInboundRecoveryError('inbound_boundary_unavailable');
    return { source, boundary: token };
  }
  private consumer(input: FeishuInboundRecoveryRequest, legacy = false): string {
    const session = this.deps.session(input.poolKey);
    const binding = session?.activeWorkItemRuntimeBinding;
    if (!session || session.poolKey !== input.poolKey || session.agentId !== this.deps.agentId
      || session.sessionId !== input.sessionId || session.runtime !== 'pi' || !binding
      || binding.runtime !== 'pi' || binding.sessionId !== input.sessionId || !binding.workspace
      || session.pendingWorkItemEntry || session.terminalWorkItemRuntimeBinding)
      throw new FeishuInboundRecoveryError('inbound_session_changed');
    // Metering revisions cannot veto recovery of the original execution.
    // Retain the old hash reader only to validate an existing receipt once.
    return digest({ agent: session.agentId, native: session.sessionId, runtime: session.runtime,
      workItemId: binding.workItemId, sessionRef: binding.sessionRef, workspace: binding.workspace,
      ...(legacy ? { usageBinding: binding.usageBinding } : {}) });
  }
  private check(record: InboundRecord, busy = true): HumanCommandSource {
    const { source, boundary } = this.source(record);
    if (record.agentId !== this.deps.agentId
      || this.consumer(record, record.consumerVersion !== 2) !== record.consumerSha256)
      throw new FeishuInboundRecoveryError('inbound_session_changed');
    if (boundary !== record.boundaryToken || source.eventId !== record.eventId || digest(source) !== record.sourceSha256)
      throw new FeishuInboundRecoveryError('human_input_superseded');
    if (busy && this.deps.busy(record.poolKey)) throw new FeishuInboundRecoveryError('conversation_busy');
    return source;
  }
  private make(input: FeishuInboundRecoveryRequest, origin: InboundRecord['origin']): InboundRecord {
    const { source, boundary } = this.source(input);
    return { ...input, schema: 'pulse_feishu_inbound_recovery/v1', requestId: this.id(input),
      agentId: this.deps.agentId, eventId: source.eventId, sourceSha256: digest(source),
      consumerSha256: this.consumer(input), consumerVersion: 2, boundaryToken: boundary, state: 'pending',
      attempts: 0, nextAttemptAt: 0, origin };
  }
  recordStartupFailure(input: FeishuInboundRecoveryRequest): FeishuInboundRecoveryReadback {
    const existing = this.read(this.id(input));
    if (existing) return this.result(existing);
    const record = this.make(input, 'observed_startup_failure');
    this.save(record); return this.result(record);
  }
  private marker(record: InboundRecord): string { return `FEISHU_INBOUND_RECOVERY_REQUEST=${record.requestId}`; }
  async recover(input: FeishuInboundRecoveryRequest): Promise<FeishuInboundRecoveryReadback> {
    const active = this.inFlight.get(input.poolKey);
    if (active) return active.then(result => {
      if (result.requestId !== this.id(input)) throw new FeishuInboundRecoveryError('inbound_recovery_busy');
      return result;
    });
    const task = this.attempt(input);
    this.inFlight.set(input.poolKey, task);
    try { return await task; } finally { if (this.inFlight.get(input.poolKey) === task) this.inFlight.delete(input.poolKey); }
  }
  private async attempt(input: FeishuInboundRecoveryRequest): Promise<FeishuInboundRecoveryReadback> {
    const record = this.read(this.id(input));
    if (!record) throw new FeishuInboundRecoveryError('inbound_recovery_record_required');
    if (record.state === 'accepted' || record.state === 'superseded') return this.result(record);
    let expectedRecord = JSON.stringify(record);
    const owns = () => JSON.stringify(this.read(record.requestId)) === expectedRecord;
    const check = () => {
      if (!owns()) throw new FeishuInboundRecoveryError('inbound_recovery_conflict');
      return this.check(record);
    };
    const save = () => {
      if (!owns()) throw new FeishuInboundRecoveryError('inbound_recovery_conflict');
      this.save(record); expectedRecord = JSON.stringify(record);
    };
    try {
      check();
      if (record.consumerVersion !== 2) {
        record.consumerSha256 = this.consumer(record); record.consumerVersion = 2; save();
      }
      const evidence = await this.deps.readDispatchEvidence(record.poolKey, record.sessionId,
        record.humanInputRef, this.marker(record));
      check();
      if (evidence === 'accepted') {
        record.state = 'accepted'; delete record.error; save(); return this.result(record);
      }
      if (record.state !== 'pending') {
        record.state = 'uncertain'; record.error = 'inbound_dispatch_unproven';
        save(); return this.result(record);
      }
      const revision = digest([import.meta.url, this.deps.runtimeRevision?.() ?? 'installed']);
      if (record.runtimeRevision !== revision) {
        record.runtimeRevision = revision; record.startupAttempts = 0;
        record.requiresRuntimeRepair = false; record.nextAttemptAt = 0; save();
      }
      if (record.requiresRuntimeRepair) return this.result(record);
      // Persist before acquisition: a pre-spawn failure is still an attempt.
      record.startupAttempts = (record.startupAttempts ?? 0) + 1; save();
      await this.deps.ensureRuntime(record.poolKey, record.sessionId, record.boundaryToken);
      const source = check();
      // pending is positive evidence: its originating Host branch returned
      // before send, or a legacy startup failure was independently proven.
      record.state = 'dispatching'; record.attempts++; save();
      const content = source.message + humanInputContext(record.humanInputRef) + `\n\n[${this.marker(record)}]`;
      await this.deps.dispatch(record.poolKey, content, record.requestId);
      record.state = 'accepted'; delete record.error;
    } catch (error) {
      if (!owns()) {
        const current = this.read(record.requestId);
        if (current) return this.result(current);
        throw new FeishuInboundRecoveryError('inbound_recovery_conflict');
      }
      const code = safeCode(error, 'inbound_recovery_retry_pending');
      if (record.state === 'dispatching') {
        // Dispatch may have reached the native process despite a failed reply.
        // A later pass may reconcile acceptance, but must never send it again.
        record.state = 'uncertain'; record.error = 'inbound_dispatch_unproven';
      } else if (['human_input_superseded', 'inbound_session_changed', 'inbound_identity_mismatch'].includes(code)) {
        record.state = 'superseded'; record.error = code;
      } else { record.error = code; }
      record.nextAttemptAt = Date.now() + Math.min(15 * 60_000,
        15_000 * 2 ** Math.min(6, Math.max(0, (record.startupAttempts ?? 0) - 1)));
      if (record.state === 'pending' && (record.startupAttempts ?? 0) >= 6)
        record.requiresRuntimeRepair = true;
      // Log the original receipt identity and public error, never exception
      // text or input. Stats/operations can distinguish waiting from repair.
      console.warn('Feishu inbound recovery observation', this.result(record));
    }
    if (!owns()) {
      const current = this.read(record.requestId);
      if (current) return this.result(current);
      throw new FeishuInboundRecoveryError('inbound_recovery_conflict');
    }
    save(); return this.result(record);
  }
  async resumePending(): Promise<void> {
    let names: string[];
    try { names = readdirSync(join(this.root, 'feishu-inbound-recoveries')); }
    catch (error) { if ((error as NodeJS.ErrnoException).code === 'ENOENT') return; throw error; }
    for (const name of names.sort()) {
      if (!/^operation_v1_feishu_inbound_[a-f0-9]{64}\.json$/.test(name)) continue;
      const requestId = name.slice(0, -5);
      try {
        const record = this.read(requestId);
        if (!record || record.agentId !== this.deps.agentId || !this.deps.owns(record.poolKey)
          || ['accepted', 'superseded'].includes(record.state)) continue;
        const revision = digest([import.meta.url, this.deps.runtimeRevision?.() ?? 'installed']);
        if (record.runtimeRevision === revision
          && (record.requiresRuntimeRepair || record.nextAttemptAt > Date.now())) continue;
        await this.recover(record);
      } catch (error) {
        // One damaged receipt must not strand another conversation. Retain the
        // original bytes and log only the validated identifier and public code.
        console.warn('Feishu inbound recovery deferred', {
          requestId, code: safeCode(error, 'inbound_recovery_record_unavailable'),
        });
      }
    }
  }
}

// --- Startup-failure evidence ---

export function latestDurableFeishuInput(poolKey:string,env:NodeJS.ProcessEnv=process.env){
  const address=parseConversationPoolKey(poolKey);
  if(!address)throw new Error('Recovery pool key is invalid');
  const root=guardAccountStatePath(dirname(env.SCL_HUMAN_DECISION_KEY_FILE??join(userInfo().homedir,'.local/state/agent-harness/scl-host/human-decision.key')));
  let names:string[];
  try{names=readdirSync(join(root,'human-inputs'));}catch(error){if((error as NodeJS.ErrnoException).code==='ENOENT')return;throw error;}
  let latest:{ref:string;source:HumanCommandSource}|undefined;
  for(const name of names){
    if(!/^[a-f0-9]{64}\.json$/.test(name))continue;
    const value=JSON.parse(readFileSync(join(root,'human-inputs',name),'utf8'));
    if(value.poolKey!==poolKey)continue;
    const source=readHumanInput(name.slice(0,-5),poolKey,'feishu',address.userId,env);
    if(!Number.isSafeInteger(source.observedAtMs))throw new Error('Recovery input time is unavailable');
    if(latest&&source.observedAtMs===latest.source.observedAtMs)throw new Error('Recovery input order is ambiguous');
    if(!latest||source.observedAtMs!>latest.source.observedAtMs!)latest={ref:name.slice(0,-5),source};
  }
  return latest;
}

/** Complete, stable native history is evidence; a tail or a partial write is not. */
export function readFeishuRecoveryHistory(path:string,sessionId:string,inputRef:string,marker?:string){
  const before=statSync(path);const raw=readFileSync(path,'utf8');const after=statSync(path);
  if(before.ino!==after.ino||before.size!==after.size||before.mtimeMs!==after.mtimeMs||!raw.endsWith('\n'))
    throw new Error('Native recovery history changed during read');
  const lines=raw.trimEnd().split('\n').map(line=>JSON.parse(line));
  if(lines[0]?.type!=='session'||lines[0]?.id!==sessionId)throw new Error('Native recovery history identity mismatch');
  let accepted=false;let marked=false;
  for(const entry of lines){
    const message=entry.type==='message'?entry.message:undefined;
    if(message?.role!=='user'||!Array.isArray(message.content))continue;
    for(const block of message.content){
      if(block.type!=='text'||typeof block.text!=='string')continue;
      if(block.text.includes(`[Host human input reference: ${inputRef}.`))accepted=true;
      if(marker&&block.text.includes(marker))marked=true;
    }
  }
  return {accepted,marked};
}

interface Message {message_id?:string;chat_id?:string;create_time?:string;deleted?:boolean;
  msg_type?:string;message_type?:string;sender?:{sender_type?:string;id?:string};body?:{content?:string}}
interface MessageReader {
  get(input:{path:{message_id:string}}):Promise<unknown>;
  list(input:{params:{container_id_type:'chat';container_id:string;start_time:string;end_time:string;
    sort_type:'ByCreateTimeAsc';page_size:number;page_token?:string}}):Promise<unknown>;
}
/** Read the original authenticated message and a complete subsequent platform
 * window. Old releases did not durably journal slash/button control events. */
async function followingPlatformMessages(reader:MessageReader,input:HumanCommandSource,
  expectedOpenId:string):Promise<Message[]|undefined>{
  const response=await reader.get({path:{message_id:input.eventId}}) as {code?:number;data?:{items?:Message[]}};
  const original=response.data?.items?.[0];
  if(response.code!==0||!original||original.message_id!==input.eventId||original.deleted||!original.chat_id
    ||original.sender?.sender_type!=='user'||original.sender.id!==expectedOpenId
    ||(original.msg_type??original.message_type)!=='text')return;
  if(JSON.parse(original.body?.content??'{}').text!==input.message)return;
  const timestamp=Number(original.create_time);
  if(!Number.isSafeInteger(timestamp)||Math.abs(timestamp-(input.observedAtMs??0))>60_000)return;
  let token:string|undefined;const messages:Message[]=[];
  for(let page=0;page<10;page++){
    const result=await reader.list({params:{container_id_type:'chat',container_id:original.chat_id,
      start_time:String(Math.floor(timestamp/1000)),end_time:String(Math.ceil(Date.now()/1000)),
      sort_type:'ByCreateTimeAsc',page_size:50,...(token?{page_token:token}:{})}}) as
      {code?:number;data?:{items?:Message[];has_more?:boolean;page_token?:string}};
    if(result.code!==0||!Array.isArray(result.data?.items))return;
    messages.push(...result.data.items);
    if(result.data.has_more===false){
      if(messages.some(message=>!Number.isSafeInteger(Number(message.create_time))))return;
      return messages.filter(message=>Number(message.create_time)>=timestamp&&message.message_id!==input.eventId);
    }
    if(result.data.has_more!==true)return;
    token=result.data.page_token;if(!token)return;
  }
}

/** Missing transcript markers alone never prove that work was not dispatched. */
export async function platformProvesStartupRejection(reader:MessageReader,input:HumanCommandSource,
  expectedOpenId:string,expectedAppId:string):Promise<boolean>{
  const following=await followingPlatformMessages(reader,input,expectedOpenId);
  if(following?.length!==1)return false;
  const failure=following[0]!;
  if(failure.deleted||failure.sender?.sender_type!=='app'||failure.sender.id!==expectedAppId
    ||(failure.msg_type??failure.message_type)!=='text')return false;
  const text=JSON.parse(failure.body?.content??'{}').text;
  return typeof text==='string'&&/^本次消息未派发：(?:运行会话启动失败|会话计量准备失败) \[[a-z0-9_]+\]/.test(text);
}
