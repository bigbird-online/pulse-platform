/** Native model evidence for one Host dispatch, never routing defaults. */
export type NativeRuntime = 'pi' | 'codex' | 'agy';
type Row = Record<string, unknown>;
export type Counts = Record<string, number>;
export interface ModelQuantity { model: string; provider: string | null; usage: Counts }
export interface NativeModelUsage {
  schema: 'pulse_native_model_usage/v1';
  runtime: NativeRuntime;
  models: ModelQuantity[];
}
// agy CLI reports uncached input, cache reads and output (thinking included); it reports no cache writes.
export const usageKeys = (runtime: NativeRuntime): string[] => runtime === 'pi'
  ? ['input_tokens', 'cache_read_input_tokens', 'cache_creation_input_tokens', 'output_tokens']
  : runtime === 'agy' ? ['input_tokens', 'cache_read_input_tokens', 'output_tokens']
  : ['input_tokens', 'cached_input_tokens', 'cache_write_input_tokens', 'output_tokens'];
export function nativeObject(value: unknown): Row {
  return value !== null && typeof value === 'object' && !Array.isArray(value) ? value as Row : {};
}
export function nativeLabel(value: unknown): string | null {
  return typeof value === 'string' && value.length > 0 && value.length <= 256
    && !value.startsWith('/') && !value.startsWith('sk-') ? value : null;
}
export function nativeCounts(value: unknown, runtime: NativeRuntime): Counts | undefined {
  const raw = nativeObject(value), result: Counts = {};
  for (const key of usageKeys(runtime)) {
    // Explicit Codex exec has no cache-write class. This is not the generic
    // rollout adapter's policy for an absent native field.
    const n = runtime === 'codex' && key === 'cache_write_input_tokens' && !Object.hasOwn(raw, key) ? 0 : raw[key];
    if (typeof n !== 'number' || !Number.isSafeInteger(n) || n < 0) return;
    result[key] = n;
  }
  if (runtime === 'codex' && result.input_tokens! < result.cached_input_tokens! + result.cache_write_input_tokens!) return;
  return result;
}
function sum(runtime: NativeRuntime, models: ModelQuantity[]): Counts | undefined {
  const total = Object.fromEntries(usageKeys(runtime).map(k => [k, 0]));
  for (const model of models) {
    for (const key of usageKeys(runtime)) {
      total[key] = total[key]! + model.usage[key]!;
      if (!Number.isSafeInteger(total[key])) return;
    }
  }
  return total;
}
/** Recheck the entire disjoint partition at the actual Host capture boundary. */
export function validateNativeModelUsage(value: unknown, expected: unknown, runtime: NativeRuntime): NativeModelUsage | undefined {
  const proof = nativeObject(value), observed = nativeCounts(expected, runtime);
  if (!observed || proof.schema !== 'pulse_native_model_usage/v1' || proof.runtime !== runtime
      || !Array.isArray(proof.models) || !proof.models.length || proof.models.length > 10000) return;
  const models: ModelQuantity[] = [];
  for (const item of proof.models) {
    const row = nativeObject(item), model = nativeLabel(row.model), usage = nativeCounts(row.usage, runtime);
    const provider = row.provider === null ? null : nativeLabel(row.provider);
    if (!model || !usage || (row.provider !== null && !provider)) return;
    models.push({model, provider, usage});
  }
  const total = sum(runtime, models);
  if (!total || usageKeys(runtime).some(key => total[key] !== observed[key])) return;
  const grouped = new Map<string, ModelQuantity>();
  for (const row of models) {
    const key = JSON.stringify([row.provider, row.model]);
    const prior = grouped.get(key);
    if (!prior) grouped.set(key, {...row, usage: {...row.usage}});
    else {
      const usage = sum(runtime, [prior, row]);
      if (!usage) return;
      prior.usage = usage;
    }
  }
  return {schema: 'pulse_native_model_usage/v1', runtime, models: [...grouped.values()]};
}

/** Host-side shadow evidence. No prompts, raw identities, prices or authority grants.
 * Each process writes its own durable spool; ComputeCost imports it into UsageLedger.
 * Opt-in until the registered-source/real-WI rollout gates have been met.
 */
import { createHash, randomUUID } from 'node:crypto';
import { closeSync, constants, fchmodSync, fsyncSync, lstatSync, mkdirSync, openSync, readFileSync, readdirSync, writeSync } from 'node:fs';
import { dirname, join } from 'node:path';
import type { ConversationRuntimeId, SpawnConfig, StreamJsonMessage } from './types.js';

const SCHEMA = 'pulse_collaboration_host_event/v1';
type ObjectRecord = Record<string, unknown>;
function object(value: unknown): ObjectRecord {
  return value !== null && typeof value === 'object' && !Array.isArray(value) ? value as ObjectRecord : {};
}
export function collaborationRef(domain: string, identity: unknown): string {
  // Identities here are arrays of strings/scalars, so canonical JSON is unambiguous across runtimes.
  return 'ccf1_' + createHash('sha256').update(JSON.stringify([domain, identity])).digest('hex');
}
function text(value: unknown): string | null {
  return typeof value === 'string' && value.length > 0 && value.length <= 256
    && !value.startsWith('/') && !value.startsWith('sk-') ? value : null;
}
/** Codex exec reports thread-cumulative counters. A turn is its total minus
 * the Host-recorded total before it; an absent or larger baseline (another
 * writer, reset, lost watermark) leaves every count unknown, never cumulative. */
export function codexTurnUsage(usage: unknown, baseline: unknown): ObjectRecord {
  const total = nativeCounts(usage, 'codex'), base = nativeCounts(baseline, 'codex');
  const keys = usageKeys('codex');
  // An all-zero total needs no baseline: no earlier turn can have spent tokens.
  if (total && keys.every(k => total[k] === 0)) return total;
  if (total && base && keys.every(k => total[k]! >= base[k]!)) return Object.fromEntries(keys.map(k => [k, total[k]! - base[k]!]));
  const observed = safeUsage(usage);
  return Object.fromEntries(Object.keys(observed).map(k => [k, null]));
}
/** ComputeCost's agy mapping (agy_result.v1) reads agy's own field names: the
 * Relay-internal cache_read_input_tokens is agy's cache_read_tokens. */
function agyHostUsage(usage: ObjectRecord): ObjectRecord {
  return Object.fromEntries(Object.entries(usage).map(([k, v]) => [k === 'cache_read_input_tokens' ? 'cache_read_tokens' : k, v]));
}
const TOKEN_KEYS = ['input_tokens','output_tokens','cache_read_input_tokens','cache_creation_input_tokens',
  'cached_input_tokens','cache_write_input_tokens','inputTokens','outputTokens','cacheReadInputTokens',
  'cacheCreationInputTokens','cachedInputTokens','cacheWriteInputTokens'];
function safeUsage(value: unknown): ObjectRecord {
  const source = object(value), result: ObjectRecord = {};
  for (const key of TOKEN_KEYS) {
    if (Object.hasOwn(source, key)) {
      const count = source[key];
      // Invalid fields remain null and make completeness unavailable, not a zero.
      result[key] = typeof count === 'number' && Number.isSafeInteger(count) && count >= 0 ? count : null;
    }
  }
  return result;
}

/** Process-local provenance bound to this exact outgoing message object.
 * Never parse a prompt/reference string or trust a serialized user property.
 * Keeping this separate from current tenant state survives concurrent inputs.
 */
export interface AuthenticatedInputProof { readonly ref: string; readonly observedAtMs: number }
const humanMessageProofs = new WeakMap<StreamJsonMessage, Readonly<AuthenticatedInputProof>>();
export function bindHumanMessageProof(message: StreamJsonMessage, proof: AuthenticatedInputProof): void {
  if (!/^[a-f0-9]{64}$/.test(proof.ref) || !Number.isSafeInteger(proof.observedAtMs)
      || proof.observedAtMs < 1 || !Number.isFinite(new Date(proof.observedAtMs).getTime())) return;
  humanMessageProofs.set(message, Object.freeze({...proof}));
}
export function humanMessageProof(message: StreamJsonMessage): Readonly<AuthenticatedInputProof> | undefined {
  return humanMessageProofs.get(message);
}

/** A CC result whose own turn counters are all zero, before any message of this attempt. */
function noRequestResult(attempt: CapturedAttempt, message: StreamJsonMessage): boolean {
  if ((attempt.runtime !== 'cc' && attempt.runtime !== 'cc-sdk') || attempt.details.size) return false;
  const usage = object(message.usage);
  return ['input_tokens', 'output_tokens', 'cache_read_input_tokens', 'cache_creation_input_tokens']
    .every(key => usage[key] === 0);
}

export interface CapturedAttempt {
  readonly ref: string;
  /** Stable opaque identity for one Host dispatch, unlike an attempt ref. */
  readonly dispatchRef: string;
  readonly startedAt: string;
  readonly runtime: ConversationRuntimeId;
  nativeSession: string | undefined;
  readonly common: Readonly<ObjectRecord>;
  readonly assignmentRef: string | null;
  readonly parentRef: string | null;
  /** Cleared when the turn settles without any provider request. */
  expectedUsage: boolean;
  readonly details: Map<string, string | null>;
  /** Immutable wire identity/time for each observed usage content version. */
  readonly usageEvents: Map<string, string>;
  finished: boolean;
}
export interface ScheduledSessionRecord {
  runtime: 'cc' | 'codex' | 'agy';
  sessionId: string;
  jobId: string;
  jobName: string;
  runId: string;
  startedAt: string;
  wi: string | null;
}
export interface CollaborationCapture {
  begin(runtime: ConversationRuntimeId, config: SpawnConfig | undefined, correlation: string, parent?: string, expectedUsage?: boolean): CapturedAttempt;
  response(attempt: CapturedAttempt, message: StreamJsonMessage): void;
  finish(attempt: CapturedAttempt, state: 'completed' | 'failed' | 'cancelled'): void;
  gap(attempt: CapturedAttempt | undefined, reason: 'native_retry' | 'steering' | 'automatic_compaction' | 'unpaired_result' | 'missing_usage' | 'provider_fallback'): void;
  /** Read durable dispatch disposition before replaying an operation after restart. */
  dispatchDisposition?(runtime: ConversationRuntimeId, config: SpawnConfig | undefined, correlation: string): 'new' | 'accepted' | 'settled' | 'recovery_unknown';
  markRecoveryUnknown?(runtime: ConversationRuntimeId, config: SpawnConfig | undefined, correlation: string): void;
  human(inputRef: string, observedAtMs: number): void;
  /** The native session a scheduler run started; see captureScheduledSession. */
  scheduled?(run: ScheduledSessionRecord): void;
  /** A Relay-originated turn delivered into a conversation; see internalDeliveryPrompt. */
  internalDelivery?(ref: string, at: string): void;
  linkHuman(attempt: CapturedAttempt, proof: Readonly<AuthenticatedInputProof> | undefined): void;
}

export function createCollaborationCapture(directory: string | undefined,
  options: { now?: () => string; nonce?: () => string; onError?: () => void } = {}): CollaborationCapture | undefined {
  if (!directory) return;
  const now = options.now ?? (() => new Date().toISOString());
  const nonce = options.nonce ?? randomUUID;
  const writer = collaborationRef('writer', nonce());
  const path = join(directory, writer + '.jsonl');
  let sequence = 0, lost = 0, directoryDurable = false;
  const dispatches = new Map<string, 'accepted' | 'settled' | 'recovery_unknown'>();
  let recoveryReadFailure = false;
  function dispatchRef(runtime: ConversationRuntimeId, config: SpawnConfig | undefined, correlation: string): string {
    // This is Host-owned routing metadata, never prompt/model text.  A bound
    // session wins; otherwise the configured native session is the only safe
    // scope available before the runtime reports its actual native identity.
    const session = config?.runtimeSessionBinding?.sessionId
      ?? config?.computeCostBinding?.authority_ref ?? config?.sessionId ?? null;
    return collaborationRef('dispatch', [runtime, correlation, session]);
  }
  function restoreDispatches(): void {
    // Receipts are independent writer files. Read only regular jsonl files in
    // the owner-only spool; malformed/partial tail records prove nothing.
    try {
      if (lstatSync(directory!).isSymbolicLink()) return;
      for (const entry of readdirSync(directory!, {withFileTypes:true})) {
        if (!entry.isFile() || !entry.name.endsWith('.jsonl')) continue;
        const file = join(directory!, entry.name);
        if (lstatSync(file).isSymbolicLink()) continue;
        for (const line of readFileSync(file, 'utf8').split('\n')) {
          if (!line) continue;
          let row: ObjectRecord;
          try { row = object(JSON.parse(line)); } catch { recoveryReadFailure = true; continue; }
          if (row.schema !== SCHEMA || typeof row.dispatch_ref !== 'string') continue;
          const ref = row.dispatch_ref;
          if (!/^ccf1_[a-f0-9]{64}$/.test(ref)) continue;
          if (row.kind === 'gap' && row.disposition === 'recovery_unknown') {
            dispatches.set(ref, 'recovery_unknown');
          } else if (row.kind === 'execution' && typeof row.state === 'string') {
            if (row.state === 'accepted') dispatches.set(ref, dispatches.get(ref) ?? 'accepted');
            else if (['completed','failed','cancelled'].includes(row.state)) dispatches.set(ref, 'settled');
          }
        }
      }
    } catch (error) {
      // A first enabled Host has no spool yet: ENOENT is a clean empty ledger,
      // not a failed recovery read. Permission/I/O errors remain uncertainty.
      if (!(error && typeof error === 'object' && 'code' in error && error.code === 'ENOENT')) {
        recoveryReadFailure = true;
      }
    }
  }
  restoreDispatches();
  function emit(body: ObjectRecord): void {
    sequence += 1;
    let fd: number | undefined;
    try {
      const created = mkdirSync(directory!, { recursive: true, mode: 0o700 });
      if (lstatSync(directory!).isSymbolicLink()) throw new Error('unsafe receipt directory');
      // A file fsync alone does not make a newly-created spool directory's
      // name durable after power loss. Sync both the directory and its parent
      // once before the first append; subsequent appends only add to this file.
      if (!directoryDurable && created !== undefined) {
        for (const target of [directory!, dirname(directory!)]) {
          const dirFd = openSync(target, constants.O_RDONLY | constants.O_DIRECTORY | constants.O_NOFOLLOW);
          try { fsyncSync(dirFd); } finally { closeSync(dirFd); }
        }
      }
      directoryDurable = true;
      fd = openSync(path, constants.O_WRONLY | constants.O_CREAT | constants.O_APPEND | constants.O_NOFOLLOW, 0o600);
      fchmodSync(fd, 0o600);
      const wire = JSON.stringify({ schema: SCHEMA, ...body, writer_ref: writer, sequence, lost_events: lost }) + '\n';
      const buffer = Buffer.from(wire);
      let offset = 0;
      while (offset < buffer.length) {
        const written = writeSync(fd, buffer, offset, buffer.length - offset);
        if (!written) throw new Error('short receipt write');
        offset += written;
      }
      fsyncSync(fd);
      // The append is not durable until its directory entry is durable too.
      // This also covers a newly-created writer file (mkdir may have existed).
      const dirFd = openSync(directory!, constants.O_RDONLY | constants.O_DIRECTORY | constants.O_NOFOLLOW);
      try { fsyncSync(dirFd); } finally { closeSync(dirFd); }
    } catch {
      lost += 1;
      // Do not expose errors/paths or introduce a new business admission gate.
      try { options.onError?.(); } catch { /* telemetry cannot throw into dispatch */ }
    } finally {
      if (fd !== undefined) { try { closeSync(fd); } catch { lost += 1; } }
    }
  }
  function emitUsage(attempt: CapturedAttempt, body: ObjectRecord): void {
    // The native usage identity remains usage_ref.  Each different observed
    // content version gets its own immutable Host event identity; an exact
    // duplicate reuses its original timestamp and event ref.
    const fingerprint = JSON.stringify(body);
    const previousAt = attempt.usageEvents.get(fingerprint);
    const usageRef = body.usage_ref;
    const eventRef = collaborationRef('host_event', [usageRef, 'usage_version', fingerprint]);
    if (previousAt) return;
    const at = now();
    attempt.usageEvents.set(fingerprint, at);
    emit({...body, at, started_at: attempt.startedAt, event_ref: eventRef});
  }

  const capture: CollaborationCapture = {
    begin(runtime, config, correlation, parent, expectedUsage = true) {
      const binding = config?.computeCostBinding;
      const meteringAgentId = text(config?.meteringAgentId);
      const purpose = config?.meteringWorkItemId;
      const workItem = typeof purpose === 'string' && /^wi_\d{8}_\d{3}$/.test(purpose) ? purpose : null;
      const common = Object.freeze({wi: binding?.work_item_id ?? workItem, executor: binding?.agent_id ?? meteringAgentId,
        // Execution binding proves the executor, not business ownership or benefit allocation.
        responsible: null, delegator: null});
      const stableDispatchRef = dispatchRef(runtime, config, correlation);
      const attempt: CapturedAttempt = { ref: collaborationRef('attempt', [writer, correlation, nonce()]), dispatchRef: stableDispatchRef,
        // An agy conversation is named by agy itself; the Relay session ID is never its native scope.
        startedAt: now(), runtime, nativeSession: runtime === 'agy' ? undefined : config?.sessionId, common,
        assignmentRef: binding ? collaborationRef('assignment', binding.binding_sha256) : null,
        parentRef: parent ?? null, expectedUsage,
        details: new Map(), usageEvents: new Map(), finished: false };
      emit({kind:'execution', at:attempt.startedAt, started_at:attempt.startedAt,
        event_ref:collaborationRef('host_event',[attempt.ref,'accepted']), attempt_ref:attempt.ref, dispatch_ref:attempt.dispatchRef,
        runtime:runtime==='cc-sdk'?'cc_sdk':runtime, ...common, assignment_ref:attempt.assignmentRef, parent_ref:attempt.parentRef,
        state:'accepted', expected_usage:expectedUsage});
      dispatches.set(attempt.dispatchRef, 'accepted');
      return attempt;
    },
    response(attempt, message) {
      // Settlement can arrive after cancellation/failure.  Do not let terminal
      // execution disposition erase already-known provider usage; routing the
      // event to the right attempt is the manager's responsibility.
      // Native init/assistant metadata, never a user message or model text. An
      // actual session may only become available after the fresh process starts.
      // agy names its conversation apart from session_id, which is the Relay session to the entry.
      const session = text(attempt.runtime === 'agy' ? message.agy_conversation_id : message.session_id);
      if (session && (message.type === 'system' || message.type === 'assistant' || message.type === 'usage_observation' || message.type === 'result')) {
        if (attempt.details.size && attempt.nativeSession && attempt.nativeSession !== session) {
          capture.gap(attempt, 'unpaired_result');
          return;
        }
        attempt.nativeSession = session;
      }
      const inner = object(message.message);
      if ((message.type === 'assistant' || message.type === 'usage_observation') && (attempt.runtime === 'cc' || attempt.runtime === 'cc-sdk')) {
        // An observed assistant without its native usage is evidence of an
        // incomplete source, not evidence that every omitted field was zero.
        if (!Object.hasOwn(inner, 'usage')) {
          if (message.type === 'assistant') capture.gap(attempt, 'missing_usage');
        } else {
          const model = text(inner.model);
          // A native message identity is preferable; otherwise this is an observed fragment,
          // not an invented request. Missing IDs deliberately prevent an exact detail claim.
          const nativeId = text(inner.id);
          if (!nativeId) { capture.gap(attempt,'unpaired_result'); return; }
          const usageRef = collaborationRef('usage',[attempt.nativeSession ?? attempt.ref, nativeId]);
          attempt.details.set(usageRef, model);
          const usage = safeUsage(inner.usage);
          emitUsage(attempt, {kind:'usage', ...attempt.common,
            attempt_ref:attempt.ref, runtime:attempt.runtime === 'cc-sdk' ? 'cc_sdk' : attempt.runtime,
            usage_ref:usageRef, model, provider:null, usage, mapping:'cc_message.v1', granularity:'request',
            ...(attempt.nativeSession ? {native_scope_ref: collaborationRef('native_session', [attempt.runtime === 'cc-sdk' ? 'cc' : attempt.runtime, attempt.nativeSession])} : {}),
            coverage_ref:collaborationRef('coverage',[attempt.ref,model]), covers:[], final:!!inner.stop_reason});
        }
      }
      if (message.type !== 'result') return;
      if (message.usage_scope === 'final_message') { capture.finish(attempt, 'completed'); return; }
      if (attempt.runtime === 'pi' || attempt.runtime === 'codex' || attempt.runtime === 'agy') {
        // Codex exec counters are thread-cumulative; the native partition is this turn's.
        const proof = validateNativeModelUsage(message.native_model_usage, attempt.runtime === 'codex'
          ? codexTurnUsage(message.usage, message.thread_usage_baseline) : message.usage, attempt.runtime);
        if (proof) {
          for (const {model, provider, usage} of proof.models) {
            const usageRef = collaborationRef('usage', [attempt.ref, 'native_model_result', provider, model]);
            emitUsage(attempt, {kind: 'usage', ...attempt.common, attempt_ref: attempt.ref,
              usage_ref: usageRef, runtime: attempt.runtime, model, provider,
              usage: attempt.runtime === 'agy' ? agyHostUsage(usage) : usage,
              mapping: attempt.runtime === 'pi' ? 'relay_pi.v1' : attempt.runtime === 'agy' ? 'agy_result.v1' : 'codex_cli.v1',
              granularity: 'turn_aggregate',
              ...(attempt.nativeSession ? {native_scope_ref: collaborationRef('native_session', [attempt.runtime, attempt.nativeSession])} : {}),
              coverage_ref: collaborationRef('coverage', [attempt.ref, provider, model]), covers: [], final: true});
          }
          capture.finish(attempt, message.is_error ? 'failed' : 'completed');
          return;
        }
        if (message.native_model_usage !== undefined) capture.gap(attempt, 'missing_usage');
      }
      const models = object((attempt.runtime === 'cc' || attempt.runtime === 'cc-sdk') ? message.modelUsage : undefined);
      const entries = Object.entries(models);
      if (entries.length) {
        // A resumed CC process can first answer a queued turn (for example a
        // task notification) without a provider request. Its cumulative model
        // counters predate this attempt's requests, whose own result follows.
        // Recording it would give one attempt two different final results.
        // Such a turn made no provider request at all: no usage is owed.
        if (noRequestResult(attempt, message)) attempt.expectedUsage = false;
        else for (const [model, native] of entries) {
          const usageRef = collaborationRef('usage',[attempt.ref,'model_result',model]);
          emitUsage(attempt, {kind:'usage', ...attempt.common, attempt_ref:attempt.ref,
            usage_ref:usageRef, runtime:attempt.runtime === 'cc-sdk' ? 'cc_sdk' : attempt.runtime,
            model:text(model), provider:null, usage:safeUsage(native), mapping:'cc_sdk_result.v1',
            granularity:'turn_aggregate', ...(attempt.nativeSession ? {native_scope_ref: collaborationRef('native_session', [attempt.runtime === 'cc-sdk' ? 'cc' : attempt.runtime, attempt.nativeSession])} : {}), coverage_ref:collaborationRef('coverage',[attempt.ref,model]),
            covers:[...attempt.details].filter(([,m])=>m===model).map(([ref])=>ref), final:true});
        }
      } else {
        // CLI/session result can be mixed-model; do not inherit the configured model.
        // A Codex exec turn runs exactly the model on its own command line
        // (`-m`), so that per-turn launch fact names the result. Absent it the
        // model stays null (unknown), never a guessed default.
        const observedModels = [...new Set(attempt.details.values())];
        const model = observedModels.length === 1 ? observedModels[0] ?? null
          : attempt.runtime === 'codex' && !observedModels.length ? text(message.requested_model) : null;
        const usageRef = collaborationRef('usage',[attempt.ref,'result']);
        emitUsage(attempt, {kind:'usage', ...attempt.common, attempt_ref:attempt.ref,
          usage_ref:usageRef, runtime:attempt.runtime === 'cc-sdk' ? 'cc_sdk' : attempt.runtime,
          model, provider:null, usage:attempt.runtime === 'codex' ? codexTurnUsage(message.usage, message.thread_usage_baseline)
            : attempt.runtime === 'agy' ? agyHostUsage(safeUsage(message.usage)) : safeUsage(message.usage),
          mapping:attempt.runtime==='pi'?'relay_pi.v1':attempt.runtime==='codex'?'codex_cli.v1':attempt.runtime==='agy'?'agy_result.v1':'cc_result.v1',
          granularity:'turn_aggregate', ...(attempt.nativeSession ? {native_scope_ref: collaborationRef('native_session', [attempt.runtime === 'cc-sdk' ? 'cc' : attempt.runtime, attempt.nativeSession])} : {}), coverage_ref:collaborationRef('coverage',[attempt.ref,model]),
          covers:[...attempt.details.keys()], final:true});
      }
      capture.finish(attempt, message.is_error === true ? 'failed' : 'completed');
    },
    finish(attempt, state) {
      if (attempt.finished) return;
      attempt.finished = true;
      emit({kind:'execution', at:now(), started_at:attempt.startedAt, ...attempt.common,
        event_ref:collaborationRef('host_event',[attempt.ref,state]), attempt_ref:attempt.ref, dispatch_ref:attempt.dispatchRef,
        runtime:attempt.runtime === 'cc-sdk' ? 'cc_sdk' : attempt.runtime,
        assignment_ref:attempt.assignmentRef, parent_ref:attempt.parentRef, state, expected_usage:attempt.expectedUsage});
      dispatches.set(attempt.dispatchRef, 'settled');
    },
    gap(attempt, reason) {
      emit({kind:'gap', at:now(), event_ref:collaborationRef('gap',[writer,sequence+1]),
        attempt_ref:attempt?.ref ?? null, dispatch_ref:attempt?.dispatchRef ?? null, wi:attempt?.common.wi ?? null, reason});
    },
    dispatchDisposition(runtime, config, correlation) {
      return dispatches.get(dispatchRef(runtime, config, correlation)) ?? (recoveryReadFailure ? 'recovery_unknown' : 'new');
    },
    markRecoveryUnknown(runtime, config, correlation) {
      const ref = dispatchRef(runtime, config, correlation);
      if (dispatches.get(ref) === 'recovery_unknown') return;
      dispatches.set(ref, 'recovery_unknown');
      emit({kind:'gap', at:now(), event_ref:collaborationRef('gap',[writer,sequence+1]),
        attempt_ref:null, dispatch_ref:ref, wi:config?.computeCostBinding?.work_item_id ?? null, reason:'unpaired_result',
        disposition:'recovery_unknown'});
    },
    linkHuman(attempt, proof) {
      if (!proof || attempt.finished) return;
      if (!/^[a-f0-9]{64}$/.test(proof.ref) || !Number.isSafeInteger(proof.observedAtMs)
          || proof.observedAtMs < 1 || !Number.isFinite(new Date(proof.observedAtMs).getTime())) {
        capture.gap(attempt, 'unpaired_result'); return;
      }
      const humanRef = collaborationRef('human', proof.ref);
      // A routed input is an observation, not a SCL grant. Preserve its edge
      // even for ordinary dialogue or a dispatch awaiting an assignment.
      // Existing readers see the same accepted execution, not another usage.
      emit({kind:'execution', at:attempt.startedAt, started_at:attempt.startedAt,
        event_ref:collaborationRef('host_event',[attempt.ref,'authenticated_input',humanRef]),
        attempt_ref:attempt.ref, dispatch_ref:attempt.dispatchRef,
        runtime:attempt.runtime === 'cc-sdk' ? 'cc_sdk' : attempt.runtime,
        ...attempt.common, assignment_ref:attempt.assignmentRef, parent_ref:attempt.parentRef,
        state:'accepted', expected_usage:attempt.expectedUsage,
        human_ref:humanRef, human_input_at:new Date(proof.observedAtMs).toISOString()});
      // WI participation remains a separate, originally authorized assertion.
      if (!attempt.assignmentRef || !attempt.common.wi) return;
      const at = now();
      emit({kind:'attribution', at,
        event_ref:collaborationRef('host_event',['human_assignment',attempt.ref,humanRef]),
        attribution_ref:collaborationRef('attribution',['human_assignment',humanRef,attempt.assignmentRef]),
        target_ref:humanRef, ...attempt.common, state:'assigned',
        policy:'authenticated_dispatch_assignment.v1', effective_at:new Date(proof.observedAtMs).toISOString()});
    },
    scheduled(run) {
      const scope = collaborationRef('native_session', [run.runtime, run.sessionId]);
      emit({kind:'scheduled_session', at:now(), event_ref:collaborationRef('host_event',['scheduled_session',scope]),
        run_ref:collaborationRef('scheduler_run',[run.jobId, run.runId]), native_scope_ref:scope,
        started_at:run.startedAt, job_id:run.jobId, job_name:run.jobName, wi:run.wi});
    },
    internalDelivery(ref, at) {
      // Not a human input and not a task grant: an inert claim on the
      // delivery's own reference, which ComputeCost joins to the native input
      // that carries the same reference in the same session.
      emit({kind:'attribution', at, event_ref:collaborationRef('host_event',['internal_delivery',ref]),
        attribution_ref:collaborationRef('attribution',[INTERNAL_DELIVERY_POLICY,ref]), target_ref:ref,
        wi:null, executor:null, responsible:null, delegator:null, state:'unassigned',
        policy:INTERNAL_DELIVERY_POLICY, effective_at:at});
    },
    human(inputRef, observedAtMs) {
      if (!Number.isFinite(observedAtMs) || !Number.isFinite(new Date(observedAtMs).getTime())) {
        capture.gap(undefined, 'unpaired_result');
        return;
      }
      const ref = collaborationRef('human',inputRef);
      emit({kind:'human', at:new Date(observedAtMs).toISOString(),
        event_ref:collaborationRef('host_event',['human',ref]), human_ref:ref,
        wi:null, executor:null, responsible:null, delegator:null, attempt_ref:null});
    },
  };
  return capture;
}

const processCaptures = new Map<string, CollaborationCapture>();
function processCapture(env: NodeJS.ProcessEnv): CollaborationCapture | undefined {
  const directory = env['PULSE_COMPUTECOST_RECEIPT_DIR'];
  if (!directory) return undefined;
  let capture = processCaptures.get(directory);
  if (!capture) {
    capture = createCollaborationCapture(directory, {onError:()=>console.warn('ComputeCost Host evidence write failed')});
    if (capture) processCaptures.set(directory, capture);
  }
  return capture;
}
/**
 * The Host capture a scheduled run records its own turn into, as a conversation
 * does (begin/response/finish); undefined where no receipt spool is configured.
 */
export function scheduledTurnCapture(env: NodeJS.ProcessEnv): CollaborationCapture | undefined {
  return processCapture(env);
}
/** Called only at the authenticated ingress, never by parsing a model/user prompt. */
export function captureHumanParticipation(inputRef: string, observedAtMs: number, env: NodeJS.ProcessEnv): void {
  processCapture(env)?.human(inputRef, observedAtMs);
}

export const INTERNAL_DELIVERY_POLICY = 'relay_internal_delivery.v1';
/**
 * Mark a turn Relay itself sends into a conversation (a collaboration inbox
 * delivery), so ComputeCost can tell it from a human message: the prompt gets
 * a one-off reference, and the Host spool records that reference bound to the
 * conversation's native session. The native input is then provably this
 * delivery, with no interactive entrypoint. Evidence loss only: without a
 * spool the prompt is unchanged.
 */
export function internalDeliveryPrompt(prompt: string, runtime: 'cc' | 'codex', nativeId: string,
  env: NodeJS.ProcessEnv, now: () => Date = () => new Date()): string {
  const capture = processCapture(env);
  if (!capture?.internalDelivery || !nativeId) return prompt;
  const at = now().toISOString();
  const token = createHash('sha256')
    .update(JSON.stringify(['internal_delivery_input', runtime, nativeId, at, randomUUID()])).digest('hex');
  const scope = collaborationRef('native_session', [runtime, nativeId]);
  capture.internalDelivery(collaborationRef('internal_delivery', [token, scope]), at);
  return `${prompt}\n\n[Host internal delivery reference: ${token}. Relay delivery, not a human message.]`;
}

const JOB_ID = /^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$/;
/**
 * Record, for ComputeCost, the native session a scheduler run started: its
 * hashed session scope, run, start, job ID and public name, and the WI the job
 * configures. The native session ID and the prompt are not written. Evidence
 * loss only: a missing field or failed write never fails the run.
 */
export function captureScheduledSession(runtime: 'cc' | 'codex' | 'agy', sessionId: string,
  run: { jobId: string; jobName?: string; runId?: string; startedAt?: string; wi?: string },
  env: NodeJS.ProcessEnv): void {
  // The public name stays one bounded line, as ComputeCost requires.
  const jobName = (run.jobName ?? '').replace(/[\u0000-\u001f\u007f]+/g, ' ').trim().slice(0, 120).trim();
  if (!JOB_ID.test(run.jobId) || !jobName || !run.startedAt || !sessionId) return;
  processCapture(env)?.scheduled?.({ runtime, sessionId, jobId: run.jobId, jobName,
    runId: run.runId ?? sessionId, startedAt: run.startedAt, wi: run.wi ?? null });
}
