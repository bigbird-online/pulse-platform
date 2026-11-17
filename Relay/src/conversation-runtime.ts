import { nextTurnSeq, type MaintenanceBarrier } from './maintenance-barrier.js';
import { ccToolUses, publicWorkEvents, toolTurnEffect } from './work-process-events.js';
import { redactRuntimeDiagnostic } from './public-runtime-failure.js';
import { humanMessageProof, createCollaborationCapture, type CapturedAttempt, type CollaborationCapture } from './collaboration-capture.js';
import { isBoundConversationRuntime, runtimeAuthorityRef, usageRuntimeForConversation } from './runtime-usage-binding.js';
import { EventEmitter } from 'node:events';
import { randomUUID } from 'node:crypto';
import { resolvePulseRuntimePriority } from './codex-defaults.js';
import { RuntimeUsageSealError, type RuntimeUsageBaselineReadback, type RuntimeUsageSealReadback } from './runtime-usage-seal.js';
import type { CcUsageMeasurement } from './cc-usage-measurement.js';
import { isRuntimeNotificationTurnMessage, RUNTIME_NOTIFICATION_TURN_ORIGIN } from './runtime-background-work.js';
import { ContextRolloverHints, carriesRolloverHint, withRolloverHint } from './context-rollover-hint.js';
import type {
  AgentConfig,
  BusyProgressEvent,
  CCProcess,
  ContextSoftWarningEvent,
  ConversationModelRoute,
  ConversationRuntimeFallbackEvent,
  ConversationRuntimeId,
  ConversationTurnContext,
  TurnArrival,
  TurnSubmitOptions,
  ConversationTurnEffect,
  Pool,
  ProcessState,
  ProcessStatusInfo,
  SessionEndReason,
  SpawnConfig,
  StreamJsonMessage,
  PoolExitMeta,
} from './types.js';

// CC triggers auto-compact at 80%; mirror the same threshold for Relay-injected /compact.
const CC_AUTO_COMPACT_THRESHOLD = 80;

/**
 * Extract context usage directly from CC's `result.context_window` field.
 * CC computes: total_input_tokens = input + cache_creation + cache_read,
 * used_percentage = min(100, round(total / context_window_size * 100)).
 * This is the same number shown in CC's own /context command, so it is the
 * authoritative source — no need for Relay to recompute or approximate.
 */
function extractCcContextUsage(msg: StreamJsonMessage): { usedPct: number; windowSize: number } | undefined {
  if (msg.type !== 'result') return undefined;
  const cw = (msg as StreamJsonMessage & {
    context_window?: { used_percentage?: number; context_window_size?: number };
  }).context_window;
  if (!cw) return undefined;
  const usedPct = Number(cw.used_percentage);
  const windowSize = Number(cw.context_window_size);
  if (!Number.isFinite(usedPct) || !Number.isFinite(windowSize) || windowSize <= 0) return undefined;
  return { usedPct, windowSize };
}

/** Percent string for UI. */
export function formatContextSoftWarningPercent(ratio: number): string {
  if (!Number.isFinite(ratio) || ratio <= 0) return '?';
  return `${Math.round(ratio * 100)}%`;
}

export type ConversationProcessState = ProcessState;

export interface ConversationAttachment {
  kind: 'image';
  path: string;
  mediaType?: string;
  cleanup?: boolean;
  cleanupRoot?: string;
}

export interface ConversationMessage {
  type: 'human';
  content: string | unknown[];
  attachments?: ConversationAttachment[];
}

export interface ConversationSpawnConfig extends SpawnConfig {
  runtime?: ConversationRuntimeId;
  conversationRuntimePreference?: ConversationRuntimeId[];
  codexThreadId?: string;
}

export interface ConversationProcess {
  /** Existing native conversation only; not WI execution admission. */
  conversationOnly?: boolean;
  /** Native tool guard verified, independently of lifecycle readiness. */
  conversationGuarded?: boolean;
  tenantId: string;
  runtime: ConversationRuntimeId;
  state: ConversationProcessState;
  sessionId: string;
  /** Digest of the usage authority actually injected into this live process. */
  usageBindingSha256?: string;
  /** Exact Host process admission; this does not reopen canonical usage. */
  measurementBaselineReference?:import('./canonical-measurement-baseline.js').CanonicalMeasurementBaselineReference;
  codexThreadId?: string;
  lastActivity: number;
  spawnedAt: number;
  restartCount: number;
  restartWindowStart: number;
  process?: CCProcess['process'];
}

export interface ConversationStatusInfo extends ProcessStatusInfo {
  runtime: ConversationRuntimeId;
}

export interface ConversationRuntimeAdapter {
  readonly id: ConversationRuntimeId;
  spawn(config: ConversationSpawnConfig): Promise<ConversationProcess>;
  send(tenantId: string, message: StreamJsonMessage, turn?: ConversationTurnContext): Promise<void>;
  /** True when the adapter calls `turn.arrival.accepted()` itself, as soon as
   *  the turn sits where its shutdown hands it over. Otherwise the manager
   *  calls it once `send` has resolved. */
  readonly admitsTurns?: boolean;
  /** Deliver a user correction into an already-running native turn. */
  steer?(tenantId: string, message: StreamJsonMessage): Promise<void>;
  /** Inject raw keystrokes into an interactive CC PTY (AskUserQuestion answers). */
  injectInput?(tenantId: string, text: string): Promise<void>;
  /** Cancel only the active turn while preserving the runtime session. */
  abortTurn?(tenantId: string): Promise<void>;
  /** Switch the active runtime's native model without discarding its session. */
  setModel?(tenantId: string, model: ConversationModelRoute): Promise<void>;
  /** List models that the active native runtime can actually use. */
  listModels?(tenantId: string): Promise<ConversationRuntimeModelOption[]>;
  /** Switch native reasoning level without discarding the session. */
  setThinkingLevel?(tenantId: string, level: import('./types.js').ConversationReasoningLevel): Promise<void>;
  /** List reasoning levels supported by the current native model. */
  listThinkingLevels?(tenantId: string): Promise<import('./types.js').ConversationReasoningLevel[]>;
  /** Run runtime-native context compaction. */
  compact?(tenantId: string, customInstructions?: string): Promise<Record<string, unknown>>;
  /** Read runtime-native state for user-visible diagnostics. */
  getNativeState?(tenantId: string): Promise<Record<string, unknown>>;
  /** Read runtime-native session/context statistics. */
  getNativeSessionStats?(tenantId: string): Promise<Record<string, unknown>>;
  /** Persist the host-bound prospective baseline after SCL authority adoption. */
  activateRuntimeUsageBaseline?(tenantId: string): Promise<RuntimeUsageBaselineReadback>;
  /** Seal host-bound usage and return a digest-validated artifact readback. */
  sealRuntimeUsage?(tenantId: string): Promise<RuntimeUsageSealReadback>;
  kill(tenantId: string): Promise<void>;
  killAll(): Promise<void>;
  get(tenantId: string): ConversationProcess | undefined;
  getAll(): ConversationProcess[];
  getStatus(): ConversationStatusInfo[];
  setYoloMode?(tenantId: string, enabled: boolean): void;
  clearYoloMode?(tenantId: string): void;
  setYoloDefaultMode?(tenantId: string, enabled: boolean): void;
  getYoloOverrideMode?(tenantId: string): boolean | undefined;
  getYoloMode?(tenantId: string): boolean;
  on(event: 'turn-accepted', handler: (tenantId: string, meta?: import('./runtime-background-work.js').TurnAcceptedMeta) => void): void;
  on(event: 'response', handler: (tenantId: string, msg: StreamJsonMessage) => void): void;
  on(event: 'exit', handler: (tenantId: string, code: number | null, meta?: PoolExitMeta) => void): void;
  on(event: 'error', handler: (tenantId: string, error: Error) => void): void;
  on(event: 'turn-warning', handler: (tenantId: string, currentTurn: number, maxTurns: number) => void): void;
  on(event: 'session-end', handler: (tenantId: string, reason: SessionEndReason) => void): void;
  on(event: 'crash-retry', handler: (tenantId: string) => void): void;
  /** Native adapter restarted and replayed the active turn; a new provider attempt. */
  on(event: 'turn-replay', handler: (tenantId: string) => void): void;
  on(event: 'tool-files', handler: (tenantId: string, filePaths: string[], workspace: string) => void): void;
  on(event: 'busy-progress', handler: (tenantId: string, progress: BusyProgressEvent) => void): void;
  on(event: 'provider-fallback', handler: (tenantId: string, event: import('./types.js').ProviderFallbackEvent) => void): void;
  on(event: 'session-chain-recovery', handler: (tenantId: string, event: import('./types.js').SessionChainRecoveryEvent) => void): void;
  on(event: 'rate-limit-notice', handler: (tenantId: string, info: { retryCount: number; nextDelayMs: number }) => void): void;
  on(event: 'turn-effect', handler: (tenantId: string, effect: ConversationTurnEffect) => void): void;
  on(event: 'stream-delta', handler: (tenantId: string, delta: string, correlationId?: string) => void): void;
  on(event: 'native-trace', handler: (tenantId: string, trace: Record<string, unknown>) => void): void;
  emitShutdown(): Set<string>;
  /** Drain busy sessions until all transition out of busy state, or timeout.
   *  Returns the set of tenantIds still busy at timeout. Optional — adapters
   *  that don't track busy state (e.g. codex) may return immediately. */
  drainBusySessions?(timeoutMs: number): Promise<Set<string>>;
  dispose(): void;
}

export interface ConversationRuntimeManager extends Omit<ConversationRuntimeAdapter, 'send'> {
  register(adapter: ConversationRuntimeAdapter): void;
  send(tenantId: string, message: StreamJsonMessage, options?: TurnSubmitOptions): Promise<void>;
  sendIdempotent(tenantId: string, message: StreamJsonMessage, idempotencyKey: string, options?: TurnSubmitOptions): Promise<void>;
  abortTurn(tenantId: string): Promise<void>;
  setModel(tenantId: string, model: ConversationModelRoute): Promise<void>;
  listModels(tenantId: string): Promise<ConversationRuntimeModelOption[]>;
  setThinkingLevel(tenantId: string, level: import('./types.js').ConversationReasoningLevel): Promise<void>;
  listThinkingLevels(tenantId: string): Promise<import('./types.js').ConversationReasoningLevel[]>;
  compact(tenantId: string, customInstructions?: string): Promise<Record<string, unknown>>;
  getNativeState(tenantId: string): Promise<Record<string, unknown>>;
  getNativeSessionStats(tenantId: string): Promise<Record<string, unknown>>;
  activateRuntimeUsageBaseline(tenantId: string): Promise<RuntimeUsageBaselineReadback>;
  sealRuntimeUsage(tenantId: string): Promise<RuntimeUsageSealReadback>;
  resolveRuntime(config: Pick<AgentConfig, 'runtime' | 'conversationRuntime'>): ConversationRuntimeId;
  resolveRuntimePreference(config: Pick<AgentConfig, 'runtime' | 'conversationRuntimePreference' | 'conversationRuntime'>): ConversationRuntimeId[];
  onRuntimeFallback(handler: (event: ConversationRuntimeFallbackEvent) => void): void;
  onContextSoftWarning(handler: (event: ContextSoftWarningEvent) => void): void;
}

export interface ConversationRuntimeModelOption {
  provider: string;
  model: string;
  displayName: string;
}

export function resolveConversationRuntimeId(
  config: Pick<AgentConfig, 'runtime' | 'conversationRuntime'>,
): ConversationRuntimeId {
  return config.conversationRuntime?.runtime ?? config.runtime ?? resolvePulseRuntimePriority()[0];
}

export function resolveConversationRuntimePreference(
  config: Pick<AgentConfig, 'runtime' | 'conversationRuntimePreference' | 'conversationRuntime'>,
): ConversationRuntimeId[] {
  const requested = config.conversationRuntime
    ? [config.conversationRuntime.runtime]
    : config.conversationRuntimePreference?.length
    ? config.conversationRuntimePreference
    : [resolveConversationRuntimeId(config)];
  // cc-sdk is a first-class conversation runtime alongside cc/codex; keep it in
  // the preference list so agents configured with `runtime: "cc-sdk"` route to
  // the SDK adapter instead of falling back to legacy cc.
  const preference = requested.filter(
    (runtime): runtime is ConversationRuntimeId => runtime === 'cc' || runtime === 'codex' || runtime === 'cc-sdk' || runtime === 'pi',
  );
  const deduped = [...new Set(preference.length ? preference : [resolveConversationRuntimeId(config)])];
  if (deduped.includes('pi') && (deduped.length !== 1 || deduped[0] !== 'pi')) {
    throw new Error('Pi runtime is opt-in only and cannot participate in automatic runtime fallback');
  }
  return deduped;
}

function inferTurnEffect(msg: StreamJsonMessage): ConversationTurnEffect | undefined {
  const tools = ccToolUses(msg);
  if (tools.length === 0) return undefined;
  return tools.every((tool) => toolTurnEffect(tool.name) === 'read-only-effect')
    ? 'read-only-effect'
    : 'side-effect-observed';
}

function sanitizeRuntimeError(error: unknown): string {
  return redactRuntimeDiagnostic(error instanceof Error ? error.message : String(error));
}

function fromCcProcess(process: CCProcess, runtime: ConversationRuntimeId = 'cc'): ConversationProcess {
  return { ...process, runtime };
}

function fromStatusInfo(status: ProcessStatusInfo, runtime: ConversationRuntimeId): ConversationStatusInfo {
  return { ...status, runtime };
}

export function createPoolConversationAdapter(
  pool: Pool,
  options: { usageMeasurement?: CcUsageMeasurement } = {},
): ConversationRuntimeAdapter {
  // CC 的有界用量只在 settled 边界由 Host 读取 transcript；运行中拒绝，避免切开同一请求。
  function measurementContext(tenantId: string) {
    const process = pool.get(tenantId);
    if (!process || process.state === 'dead') {
      throw new RuntimeUsageSealError('measurement_authority_unavailable', 'CC runtime is not live');
    }
    if (process.state === 'busy' || process.state === 'starting') {
      throw new RuntimeUsageSealError('not_settled', 'CC usage cannot be measured during a runtime turn', true);
    }
    const context = pool.usageMeasurementContext?.(tenantId);
    if (!context || !options.usageMeasurement) {
      throw new RuntimeUsageSealError('measurement_authority_unavailable', 'CC usage measurement is unavailable');
    }
    return { context, measurement: options.usageMeasurement };
  }
  return {
    id: 'cc',
    async spawn(config: ConversationSpawnConfig): Promise<ConversationProcess> {
      const process = await pool.spawn(config);
      return fromCcProcess(process, 'cc');
    },
    async activateRuntimeUsageBaseline(tenantId: string): Promise<RuntimeUsageBaselineReadback> {
      const { context, measurement } = measurementContext(tenantId);
      return measurement.activateBaseline(context);
    },
    async sealRuntimeUsage(tenantId: string): Promise<RuntimeUsageSealReadback> {
      const { context, measurement } = measurementContext(tenantId);
      return measurement.seal(context);
    },
    send: pool.send,
    admitsTurns: pool.admitsTurns === true,
    injectInput: pool.injectInput,
    kill: pool.kill,
    killAll: pool.killAll,
    get(tenantId: string): ConversationProcess | undefined {
      const process = pool.get(tenantId);
      return process ? fromCcProcess(process, 'cc') : undefined;
    },
    getAll(): ConversationProcess[] {
      return pool.getAll().map((process) => fromCcProcess(process, 'cc'));
    },
    getStatus(): ConversationStatusInfo[] {
      return pool.getStatus().map((status) => fromStatusInfo(status, 'cc'));
    },
    on: pool.on,
    emitShutdown: pool.emitShutdown,
    drainBusySessions: pool.drainBusySessions,
    dispose: pool.dispose,
  };
}

export function createConversationRuntimeManager(options: { collaborationCapture?: CollaborationCapture; env?: NodeJS.ProcessEnv; resolveMeteringAgent?: (poolKey: string) => string | undefined;
  /** Deployment maintenance barrier: new turns wait while it is held. */
  barrier?: Pick<MaintenanceBarrier, 'admit'> } = {}): ConversationRuntimeManager {
  const capture = options.collaborationCapture ?? createCollaborationCapture(
    (options.env ?? process.env)['PULSE_COMPUTECOST_RECEIPT_DIR'],
    {onError:()=>console.warn('ComputeCost execution evidence write failed')});
  const adapters = new Map<ConversationRuntimeId, ConversationRuntimeAdapter>();
  const tenantRuntime = new Map<string, ConversationRuntimeId>();
  const tenantConfig = new Map<string, ConversationSpawnConfig>();
  const emitter = new EventEmitter();
  emitter.setMaxListeners(0);

  type ActiveTurn = {
    receipt?: CapturedAttempt;
    receiptConfig?: ConversationSpawnConfig;
    message: StreamJsonMessage;
    sawResponse: boolean;
    fallbackInProgress: boolean;
    recoveryRequiredEmitted: boolean;
    attemptedFallbacks: Set<ConversationRuntimeId>;
    context: ConversationTurnContext;
  };
  const activeTurns = new Map<string, ActiveTurn[]>();
  // Explicit Host attempt refs retain late-settlement routing without allowing
  // a shared native session to poison later turns.
  // Late provider settlements are retained for a bounded 5 minute/128 attempt
  // window. Once reclaimed, an arriving opaque ref becomes an explicit gap.
  const attemptsByRef = new Map<string, {attempt: CapturedAttempt; tenantId: string; timer: ReturnType<typeof setTimeout>}>();
  const LATE_ATTEMPT_LIMIT = 128;
  const LATE_ATTEMPT_WINDOW_MS = 5 * 60_000;
  function rememberAttempt(tenantId: string, attempt: CapturedAttempt): void {
    const old = attemptsByRef.get(attempt.ref); if (old) clearTimeout(old.timer);
    while (attemptsByRef.size >= LATE_ATTEMPT_LIMIT) {
      const ref = attemptsByRef.keys().next().value as string | undefined;
      if (!ref) break;
      const evicted = attemptsByRef.get(ref); if (evicted) clearTimeout(evicted.timer);
      attemptsByRef.delete(ref);
    }
    const timer = setTimeout(() => {
      if (attemptsByRef.get(attempt.ref)?.attempt === attempt) attemptsByRef.delete(attempt.ref);
    }, LATE_ATTEMPT_WINDOW_MS);
    timer.unref?.();
    attemptsByRef.set(attempt.ref, {attempt, tenantId, timer});
  }
  function clearAttempts(): void {
    for (const entry of attemptsByRef.values()) clearTimeout(entry.timer);
    attemptsByRef.clear();
  }
  // A runtime-notification turn (background work completing) is no Relay
  // turn, but its usage is the same session's work. It gets its own attempt
  // whose parent is the latest Relay-dispatched attempt of that session, so
  // the executor and its human dispatch stay declared by the Host. The turn
  // starts at its first frame: the runtime announces it only once it has text,
  // and a silent turn is never announced.
  const dispatchedTurns = new Map<string, {runtime: ConversationRuntimeAdapter['id']; config: ConversationSpawnConfig | undefined; ref: string}>();
  const notificationAttempts = new Map<string, CapturedAttempt>();
  function beginNotificationAttempt(tenantId: string, runtime: ConversationRuntimeAdapter['id']): CapturedAttempt | undefined {
    const previous = notificationAttempts.get(tenantId);
    if (previous) capture?.finish(previous, 'completed');
    notificationAttempts.delete(tenantId);
    const dispatched = dispatchedTurns.get(tenantId);
    if (!capture || !dispatched || dispatched.runtime !== runtime) return undefined;
    const attempt = capture.begin(runtime, dispatched.config, 'runtime-notification', dispatched.ref);
    notificationAttempts.set(tenantId, attempt);
    rememberAttempt(tenantId, attempt);
    return attempt;
  }
  // Codex emits a factual failed result before its process error. That error
  // belongs to the just-settled child, never to the next queued turn.
  const terminalResultErrors = new Set<string>();

  // Dedup auto-compact injection per (tenantId, sessionId) so we only inject once
  // per conversation. /new resets the sessionId naturally; explicit kill clears below.
  const compactInjectedSessions = new Set<string>();
  // Context facts per conversation; the next human message may carry a rollover hint.
  const rolloverHints = new ContextRolloverHints();
  const hintKey = (tenantId: string) => `${tenantId}:${tenantConfig.get(tenantId)?.sessionId ?? 'unknown-session'}`;

  function evaluateContextAutoCompact(adapter: ConversationRuntimeAdapter, tenantId: string, msg: StreamJsonMessage): void {
    if (adapter.id !== 'cc') return;
    const usage = extractCcContextUsage(msg);
    if (!usage) return;
    if (usage.usedPct < CC_AUTO_COMPACT_THRESHOLD) return;

    const config = tenantConfig.get(tenantId);
    const sessionId = config?.sessionId ?? 'unknown-session';
    const dedupKey = `${tenantId}:${sessionId}`;
    if (compactInjectedSessions.has(dedupKey)) return;
    compactInjectedSessions.add(dedupKey);

    console.warn({
      ts: new Date().toISOString(),
      level: 'warn',
      msg: 'Context threshold reached, injecting /compact',
      tenantId,
      sessionId: sessionId.slice(0, 8),
      usedPct: usage.usedPct,
      windowSize: usage.windowSize,
    });

    // Inject /compact directly via the adapter (bypasses activeTurns so the
    // in-flight user turn is not overwritten). /compact is supportsNonInteractive
    // in CC so it runs in both print and interactive modes.
    capture?.gap(activeTurns.get(tenantId)?.[0]?.receipt, 'automatic_compaction');
    void adapter.send(tenantId, { type: 'human', content: '/compact' }).catch(() => {
      // Best-effort: if the process is gone the session will end naturally.
      compactInjectedSessions.delete(dedupKey);
    });
  }

  function adapterFor(runtime: ConversationRuntimeId): ConversationRuntimeAdapter {
    const adapter = adapters.get(runtime);
    if (!adapter) {
      throw new Error(`Conversation runtime adapter not registered: ${runtime}`);
    }
    return adapter;
  }

  function adapterForTenant(tenantId: string): ConversationRuntimeAdapter {
    const runtime = tenantRuntime.get(tenantId);
    if (runtime) return adapterFor(runtime);

    for (const adapter of adapters.values()) {
      if (adapter.get(tenantId)) {
        tenantRuntime.set(tenantId, adapter.id);
        return adapter;
      }
    }

    throw new Error(`No conversation runtime process for tenant ${tenantId}`);
  }

  function isActiveAdapterEvent(adapter: ConversationRuntimeAdapter, tenantId: string): boolean {
    const runtime = tenantRuntime.get(tenantId);
    return !runtime || runtime === adapter.id;
  }

  function nextRuntimeForTenant(tenantId: string, currentRuntime: ConversationRuntimeId): ConversationRuntimeId | undefined {
    const config = tenantConfig.get(tenantId);
    if (!config) return undefined;
    const preference = resolveConversationRuntimePreference(config);
    const currentIndex = preference.indexOf(currentRuntime);
    if (currentIndex < 0) return undefined;
    for (const runtime of preference.slice(currentIndex + 1)) {
      if (adapters.has(runtime)) return runtime;
    }
    return undefined;
  }

  /** 用量绑定只属于签发它的运行时；回退到其他运行时时不携带，避免 spawn 拒绝或错计。 */
  function usageBindingForRuntime(config: ConversationSpawnConfig, runtime: ConversationRuntimeId)
    : Partial<Pick<ConversationSpawnConfig, 'computeCostBinding' | 'measurementBaselineReference'>> {
    const bindingRuntime = config.computeCostBinding?.runtime;
    if (!bindingRuntime || (isBoundConversationRuntime(runtime) && usageRuntimeForConversation(runtime) === bindingRuntime)) {
      return {};
    }
    console.warn(JSON.stringify({ level: 'warn', msg: 'Runtime usage binding dropped for fallback runtime',
      tenantId: config.tenantId, bindingRuntime, runtime }));
    return { computeCostBinding: undefined, measurementBaselineReference: undefined };
  }

  async function fallbackActiveTurn(
    adapter: ConversationRuntimeAdapter,
    tenantId: string,
    error: Error,
  ): Promise<boolean> {
    const queuedTurns = activeTurns.get(tenantId);
    const turn = queuedTurns?.[0];
    const currentRuntime = tenantRuntime.get(tenantId) ?? adapter.id;
    const config = tenantConfig.get(tenantId);
    const nextRuntime = nextRuntimeForTenant(tenantId, currentRuntime);
    if (!turn || turn.fallbackInProgress || !config) {
      return false;
    }
    if (turn.context.effect === 'side-effect-observed') {
      if (!turn.recoveryRequiredEmitted) {
        turn.recoveryRequiredEmitted = true;
        emitter.emit('session-end', tenantId, {
          type: 'runtime-recovery-required',
          correlationId: turn.context.correlationId,
          runtime: currentRuntime,
          effect: turn.context.effect,
          reason: sanitizeRuntimeError(error),
        } satisfies SessionEndReason);
      }
      return true;
    }
    if (turn.sawResponse || !nextRuntime || turn.attemptedFallbacks.has(nextRuntime)) {
      return false;
    }

    turn.fallbackInProgress = true;
    turn.attemptedFallbacks.add(nextRuntime);
    // Collect previous session info for fallback context injection.
    const previousProc = adapter.get(tenantId);
    const fallbackReason = sanitizeRuntimeError(error);
    try {
      try {
        await adapter.kill(tenantId);
      } catch {
        // The adapter may already have exited after emitting the error.
      }
      // Build fallback context so the new session knows why it was created.
      const fallbackContext = previousProc ? {
        previousSessionJsonlPath: `session-id:${previousProc.sessionId}`,
        previousRuntime: currentRuntime,
        reason: fallbackReason,
      } satisfies import('./types.js').FallbackContext : undefined;
      await adapterFor(nextRuntime).spawn({
        ...config,
        ...usageBindingForRuntime(config, nextRuntime),
        runtime: nextRuntime,
        fallbackContext,
      });
      tenantRuntime.set(tenantId, nextRuntime);
      emitter.emit('runtime-fallback', {
        tenantId,
        from: currentRuntime,
        to: nextRuntime,
        reason: sanitizeRuntimeError(error),
        attemptIndex: resolveConversationRuntimePreference(config).indexOf(nextRuntime),
        correlationId: turn.context.correlationId,
        idempotencyKey: turn.context.idempotencyKey,
        effect: turn.context.effect,
      } satisfies ConversationRuntimeFallbackEvent);
      // Existing adapters queue turns in submission order. Killing the failed
      // adapter drops that queue, so replay the accepted head and its still-
      // pending tail with their original identities on the replacement.
      for (const queuedTurn of queuedTurns ?? [turn]) {
        if (queuedTurn.receipt) {
          capture?.finish(queuedTurn.receipt, 'failed');
          rememberAttempt(tenantId, queuedTurn.receipt);
        }
        queuedTurn.receipt = capture?.begin(nextRuntime, queuedTurn.receiptConfig ?? config, queuedTurn.context.correlationId,
          queuedTurn.receipt?.ref);
        queuedTurn.context.hostAttemptRef = queuedTurn.receipt?.ref;
        if (queuedTurn.receipt) { rememberAttempt(tenantId, queuedTurn.receipt); capture?.linkHuman(queuedTurn.receipt, humanMessageProof(queuedTurn.message)); }
        await adapterFor(nextRuntime).send(tenantId, queuedTurn.message, queuedTurn.context);
      }
      return true;
    } catch (fallbackError) {
      emitter.emit('error', tenantId, fallbackError instanceof Error ? fallbackError : new Error(String(fallbackError)));
      return true;
    } finally {
      turn.fallbackInProgress = false;
    }
  }

  function relayEvents(adapter: ConversationRuntimeAdapter): void {
    adapter.on('turn-accepted', (tenantId, meta) => {
      if (!isActiveAdapterEvent(adapter, tenantId)) return;
      // A runtime-notification turn is not a Relay turn; queued turns do not own it.
      if (meta?.origin === RUNTIME_NOTIFICATION_TURN_ORIGIN) {
        if (!notificationAttempts.has(tenantId)) beginNotificationAttempt(tenantId, adapter.id);
        emitter.emit('turn-accepted', tenantId, meta);
        return;
      }
      if (activeTurns.get(tenantId)?.[0]?.context.deliveryVisibility === 'control') return;
      emitter.emit('turn-accepted', tenantId);
    });
    adapter.on('response', (tenantId, msg) => {
      const adapterActive = isActiveAdapterEvent(adapter, tenantId);
      // Output of a runtime-notification turn never settles or feeds a queued Relay turn.
      const runtimeNotification = isRuntimeNotificationTurnMessage(msg);
      const queue = runtimeNotification ? undefined : activeTurns.get(tenantId);
      // A notification frame still carries the previous Relay turn's attempt
      // ref, which does not identify it.
      const ref = !runtimeNotification && typeof msg.host_attempt_ref === 'string' ? msg.host_attempt_ref : undefined;
      const absorbedCount = msg.type === 'result' && ref && queue && adapterActive
        ? queue.findIndex(item => item.receipt?.ref === ref) : -1;
      if (queue && absorbedCount > 0) {
        // Runtimes settle turns in order: a result for a later queued turn means
        // the runtime folded the turns ahead of it into that run. They will
        // never see a result of their own.
        for (const absorbed of queue.splice(0, absorbedCount)) if (absorbed.receipt) {
          capture?.gap(absorbed.receipt, 'unpaired_result');
          capture?.finish(absorbed.receipt, 'cancelled');
          rememberAttempt(tenantId, absorbed.receipt);
        }
      }
      const turn = queue?.[0];
      // Active attempts are not subject to the bounded late-settlement cache:
      // a legitimate long-running turn must still be meterable after 5 minutes.
      const activeAttempt = ref ? queue?.find(item => item.receipt?.ref === ref)?.receipt : undefined;
      const cached = ref ? attemptsByRef.get(ref) : undefined;
      const identified = activeAttempt ?? (cached?.tenantId === tenantId ? cached.attempt : undefined);
      if (!adapterActive) {
        // Late facts from a killed/switched runtime cannot drive the new turn.
        if (identified?.runtime === adapter.id) capture?.response(identified, msg);
        else if (msg.host_usage_only === true || msg.type === 'usage_observation') capture?.gap(undefined, 'unpaired_result');
        return;
      }
      if (identified?.runtime && identified.runtime !== adapter.id) { capture?.gap(undefined, 'unpaired_result'); return; }
      if (msg.type === 'usage_observation' || msg.host_usage_only === true ||
          (ref !== undefined && ref !== turn?.receipt?.ref)) {
        // Explicitly identified late frames are persisted but never shift B's
        // queue, reach B's channel, or take B's assignment. Unknown refs are gaps.
        const owner = identified ?? (runtimeNotification
          ? notificationAttempts.get(tenantId) ?? beginNotificationAttempt(tenantId, adapter.id) : undefined);
        if (owner) capture?.response(owner, msg);
        else capture?.gap(undefined, 'unpaired_result');
        return;
      }
      if (msg.type === 'work_process' && (!turn ||
        (typeof msg.correlation_id === 'string' && msg.correlation_id !== turn.context.correlationId))) return;
      const notification = runtimeNotification
        ? notificationAttempts.get(tenantId) ?? beginNotificationAttempt(tenantId, adapter.id) : undefined;
      if (turn?.receipt) capture?.response(turn.receipt, msg);
      else if (notification) {
        capture?.response(notification, msg);
        if (msg.type === 'result') notificationAttempts.delete(tenantId);
      } else if (msg.type === 'result') capture?.gap(undefined, 'unpaired_result');
      if (turn) {
        if (msg.type === 'assistant') turn.sawResponse = true;
        const inferred = inferTurnEffect(msg);
        if (inferred === 'side-effect-observed' || (inferred === 'read-only-effect' && turn.context.effect === 'no-effect')) {
          turn.context.effect = inferred;
        }
      }
      if (turn && turn.context.deliveryVisibility !== 'control') {
        for (const event of publicWorkEvents(adapter.id, msg)) {
          emitter.emit('response', tenantId, { type: 'work_process', process_event: event });
        }
      }
      const delivered = turn?.context.deliveryVisibility === 'control'
        ? { ...msg, delivery_visibility: 'control' }
        : msg;
      const resultSession = adapter.get(tenantId);
      const correlated = msg.type === 'result' && resultSession && turn?.context.idempotencyKey.startsWith('operation_v1_')
        ? {...delivered, host_turn_idempotency_key:turn.context.idempotencyKey,
          host_session_ref:runtimeAuthorityRef(resultSession.sessionId)}
        : delivered;
      emitter.emit('response', tenantId, correlated);
      if (turn?.context.deliveryVisibility !== 'control') evaluateContextAutoCompact(adapter, tenantId, msg);
      rolloverHints.observe(hintKey(tenantId), msg);
      if (msg.type === 'result' && queue) {
        if (msg.is_error === true) terminalResultErrors.add(tenantId);
        const settled = queue.shift();
        if (settled?.receipt) rememberAttempt(tenantId, settled.receipt);
        if (queue.length === 0) activeTurns.delete(tenantId);
      }
    });
    adapter.on('exit', (tenantId, code, meta) => {
      if (!isActiveAdapterEvent(adapter, tenantId)) return;
      // A per-turn child exit must only settle its own opaque attempt: Codex A
      // can exit after its result synchronously dispatches queued B.
      const exitRef = typeof meta?.hostAttemptRef === 'string' ? meta.hostAttemptRef : undefined;
      const cachedExit = exitRef ? attemptsByRef.get(exitRef) : undefined;
      const exitedAttempt = exitRef ? activeTurns.get(tenantId)?.find(t => t.receipt?.ref === exitRef)?.receipt
        ?? (cachedExit?.tenantId === tenantId ? cachedExit.attempt : undefined) : undefined;
      if (exitedAttempt) {
        capture?.finish(exitedAttempt, code === 0 ? 'completed' : 'failed');
        rememberAttempt(tenantId, exitedAttempt);
        const queue = activeTurns.get(tenantId);
        const index = queue?.findIndex((turn) => turn.receipt?.ref === meta?.hostAttemptRef) ?? -1;
        if (index >= 0 && queue) {
          queue.splice(index, 1);
          if (queue.length === 0) activeTurns.delete(tenantId);
        }
      } else if (!meta?.hostAttemptRef && !(adapter.id === 'codex' && code === 0)) {
        for (const turn of activeTurns.get(tenantId) ?? []) {
          if (turn.receipt) capture?.finish(turn.receipt, 'failed');
        }
      }
      const control=activeTurns.get(tenantId)?.[0]?.context.deliveryVisibility==='control';
      emitter.emit('exit',tenantId,code,control?{...meta,suppressGenericRecoveryToast:true}:meta);
    });
    adapter.on('error', (tenantId, error) => {
      if (!isActiveAdapterEvent(adapter, tenantId)) return;
      if (terminalResultErrors.delete(tenantId) || typeof (error as Error & {hostAttemptRef?: unknown}).hostAttemptRef === 'string') {
        emitter.emit('error', tenantId, error); return;
      }
      void fallbackActiveTurn(adapter, tenantId, error).then((handled) => {
        if (!handled) emitter.emit('error', tenantId, error);
      });
    });
    adapter.on('turn-warning', (tenantId, currentTurn, maxTurns) => {
      if (!isActiveAdapterEvent(adapter, tenantId)) return;
      if(activeTurns.get(tenantId)?.[0]?.context.deliveryVisibility==='control')return;
      emitter.emit('turn-warning', tenantId, currentTurn, maxTurns);
    });
    adapter.on('session-end', (tenantId, reason) => {
      if (!isActiveAdapterEvent(adapter, tenantId)) return;
      if (reason.type === 'idle-reclaimed') {
        // Adapters reclaim only a runtime with no turn in flight, so anything
        // still queued here is bookkeeping that never settled, not an
        // interrupted turn.
        for (const stale of activeTurns.get(tenantId) ?? []) if (stale.receipt) {
          capture?.finish(stale.receipt, 'cancelled');
          rememberAttempt(tenantId, stale.receipt);
        }
        activeTurns.delete(tenantId);
      }
      const turn = activeTurns.get(tenantId)?.[0];
      if (turn && reason.type === 'runtime-recovery-required') {
        turn.recoveryRequiredEmitted = true;
      }
      if (turn?.context.effect === 'side-effect-observed' && reason.type !== 'runtime-recovery-required') {
        if (!turn.recoveryRequiredEmitted) {
          turn.recoveryRequiredEmitted = true;
          emitter.emit('session-end', tenantId, {
            type: 'runtime-recovery-required',
            correlationId: turn.context.correlationId,
            runtime: adapter.id,
            effect: turn.context.effect,
            reason: `runtime ended: ${reason.type}`,
          } satisfies SessionEndReason);
        }
        return;
      }
      if (reason.type === 'rate-limited') {
        const error = new Error(`runtime rate limited after ${reason.retryCount} retries`);
        void fallbackActiveTurn(adapter, tenantId, error).then((handled) => {
          if (!handled) emitter.emit('session-end', tenantId, reason);
        });
        return;
      }
      emitter.emit('session-end', tenantId, reason);
    });
    adapter.on('crash-retry', (tenantId) => {
      if (!isActiveAdapterEvent(adapter, tenantId)) return;
      capture?.gap(activeTurns.get(tenantId)?.[0]?.receipt, 'native_retry');
      emitter.emit('crash-retry', tenantId);
    });
    adapter.on('turn-replay', (tenantId) => {
      if (!isActiveAdapterEvent(adapter, tenantId)) return;
      const turn = activeTurns.get(tenantId)?.[0];
      if (!turn?.receipt) return;
      capture?.finish(turn.receipt, 'failed');
      rememberAttempt(tenantId, turn.receipt);
      turn.receipt = capture?.begin(adapter.id, turn.receiptConfig, turn.context.correlationId, turn.receipt.ref);
      turn.context.hostAttemptRef = turn.receipt?.ref;
      if (turn.receipt) { rememberAttempt(tenantId, turn.receipt); capture?.linkHuman(turn.receipt, humanMessageProof(turn.message)); }
    });
    adapter.on('tool-files', (tenantId, filePaths, workspace) => {
      if (!isActiveAdapterEvent(adapter, tenantId)) return;
      emitter.emit('tool-files', tenantId, filePaths, workspace);
    });
    adapter.on('busy-progress', (tenantId, progress) => {
      if (!isActiveAdapterEvent(adapter, tenantId)) return;
      emitter.emit('busy-progress', tenantId, progress);
    });
    adapter.on('provider-fallback', (tenantId: string, event: import('./types.js').ProviderFallbackEvent) => {
      if (!isActiveAdapterEvent(adapter, tenantId)) return;
      capture?.gap(activeTurns.get(tenantId)?.[0]?.receipt, 'provider_fallback');
      emitter.emit('provider-fallback', tenantId, event);
    });
    adapter.on('session-chain-recovery', (tenantId: string, event: import('./types.js').SessionChainRecoveryEvent) => {
      if (!isActiveAdapterEvent(adapter, tenantId)) return;
      emitter.emit('session-chain-recovery', tenantId, event);
    });
    adapter.on('rate-limit-notice', (tenantId: string, info: { retryCount: number; nextDelayMs: number }) => {
      if (!isActiveAdapterEvent(adapter, tenantId)) return;
      emitter.emit('rate-limit-notice', tenantId, info);
    });
    adapter.on('turn-effect', (tenantId, effect) => {
      if (!isActiveAdapterEvent(adapter, tenantId)) return;
      const turn = activeTurns.get(tenantId)?.[0];
      if (!turn) return;
      if (effect === 'side-effect-observed' || (effect === 'read-only-effect' && turn.context.effect === 'no-effect')) {
        turn.context.effect = effect;
      }
      if (turn.context.deliveryVisibility !== 'control') emitter.emit('turn-effect', tenantId, effect);
    });
    adapter.on('stream-delta', (tenantId, delta, correlationId) => {
      if (!isActiveAdapterEvent(adapter, tenantId)) return;
      if (activeTurns.get(tenantId)?.[0]?.context.deliveryVisibility === 'control') return;
      emitter.emit('stream-delta', tenantId, delta, correlationId);
    });
    adapter.on('native-trace', (tenantId, trace) => {
      if (!isActiveAdapterEvent(adapter, tenantId)) return;
      emitter.emit('native-trace', tenantId, trace);
    });
  }

  async function sendTurn(
    tenantId: string,
    message: StreamJsonMessage,
    durableIdempotencyKey?: string,
    submit?: TurnSubmitOptions,
  ): Promise<void> {
    const hint = carriesRolloverHint(message) ? rolloverHints.take(hintKey(tenantId)) : undefined;
    if (hint) message = withRolloverHint(message, hint);
    const arrival: TurnArrival = { seq: submit?.seq ?? nextTurnSeq(), accepted: () => submit?.onAccepted?.(),
      owned: Boolean(submit?.onAccepted) || Boolean(durableIdempotencyKey), resume: submit?.resume };
    // Queue, never drop: a held barrier delays the turn until it is released.
    // A resumed record of a turn waiting at the barrier goes out from there.
    if (await options.barrier?.admit(tenantId, tenantRuntime.get(tenantId) ?? 'unknown', message, arrival) === 'held_here') return;
    const adapter = adapterForTenant(tenantId);
    const queue = activeTurns.get(tenantId) ?? [];
    if (queue.length > 0 && adapter.id === 'pi') {
      if (
        durableIdempotencyKey
        && queue.some((turn) => turn.context.idempotencyKey === durableIdempotencyKey)
      ) {
        arrival.accepted();
        return;
      }
      if (durableIdempotencyKey) throw new Error('A distinct durable entry cannot be steered into another active turn');
      if (!adapter.steer) throw new Error(`Conversation runtime ${adapter.id} cannot accept input while busy`);
      capture?.gap(queue[0]?.receipt, 'steering');
      if (queue[0]?.receipt) capture?.linkHuman(queue[0].receipt, humanMessageProof(message));
      await adapter.steer(tenantId, message);
      arrival.accepted();
      return;
    }
    if (durableIdempotencyKey && !/^operation_v1_[A-Za-z0-9_.:-]+$/.test(durableIdempotencyKey)) {
      throw new Error('durable conversation idempotency key is invalid');
    }
    // The same durable entry is already active: that turn holds it.
    if (durableIdempotencyKey && queue.some(item => item.context.idempotencyKey === durableIdempotencyKey)) {
      arrival.accepted();
      return;
    }
    const correlationId = durableIdempotencyKey
      ? `turn:entry:${durableIdempotencyKey}`
      : `turn:${randomUUID()}`;
    const config = tenantConfig.get(tenantId);
    if (durableIdempotencyKey) {
      const disposition = capture?.dispatchDisposition?.(adapter.id, config, correlationId);
      // Metering is shadow evidence only.  Preserve recovery uncertainty, but
      // never change the pre-existing sendIdempotent admission/replay policy.
      if (disposition === 'accepted' || disposition === 'recovery_unknown') {
        capture?.markRecoveryUnknown?.(adapter.id, config, correlationId);
      }
    }
    const turn: ActiveTurn = {
      message,
      sawResponse: false,
      fallbackInProgress: false,
      recoveryRequiredEmitted: false,
      attemptedFallbacks: new Set(),
      context: {
        arrival,
        correlationId,
        idempotencyKey: durableIdempotencyKey ?? correlationId,
        effect: 'no-effect',
        deliveryVisibility: message.delivery_visibility === 'control' ? 'control' : 'public',
      },
    };
    const nativeSession = adapter.get(tenantId)?.sessionId;
    // A durable operation is bound to its persisted session tuple. Do not
    // replace that identity with a process-local adapter session on restart.
    turn.receiptConfig = config ? {...config, sessionId:durableIdempotencyKey ? config.sessionId : (nativeSession || config.sessionId),
      computeCostBinding:config.computeCostBinding ? {...config.computeCostBinding} : undefined} : undefined;
    turn.receipt = capture?.begin(adapter.id, turn.receiptConfig, correlationId);
    turn.context.hostAttemptRef = turn.receipt?.ref;
    if (turn.receipt) {
      rememberAttempt(tenantId, turn.receipt); capture?.linkHuman(turn.receipt, humanMessageProof(message));
      dispatchedTurns.set(tenantId, {runtime: adapter.id, config: turn.receiptConfig, ref: turn.receipt.ref});
    }
    queue.push(turn);
    activeTurns.set(tenantId, queue);
    try {
      await adapter.send(tenantId, message, turn.context);
      if (!adapter.admitsTurns) arrival.accepted();
    } catch (error) {
      if (turn.receipt) capture?.finish(turn.receipt, 'failed');
      const index = queue.indexOf(turn);
      if (index >= 0) queue.splice(index, 1);
      if (queue.length === 0) activeTurns.delete(tenantId);
      throw error;
    }
  }

  const manager: ConversationRuntimeManager = {
    id: 'cc',
    register(adapter: ConversationRuntimeAdapter): void {
      if (adapters.has(adapter.id)) {
        throw new Error(`Conversation runtime adapter already registered: ${adapter.id}`);
      }
      adapters.set(adapter.id, adapter);
      relayEvents(adapter);
    },
    resolveRuntime: resolveConversationRuntimeId,
    resolveRuntimePreference: resolveConversationRuntimePreference,
    onRuntimeFallback(handler: (event: ConversationRuntimeFallbackEvent) => void): void {
      emitter.on('runtime-fallback', handler);
    },
    onContextSoftWarning(handler: (event: ContextSoftWarningEvent) => void): void {
      emitter.on('context-soft-warning', handler);
    },
    async spawn(config: ConversationSpawnConfig): Promise<ConversationProcess> {
      // Agent metadata comes from the Host's registered route, including ordinary
      // non-WI dispatches. A telemetry lookup must not alter business admission.
      try {
        const agentId = options.resolveMeteringAgent?.(config.tenantId);
        if (agentId) config = {...config, meteringAgentId: agentId};
      } catch { /* unresolved metadata remains unknown */ }
      const preference = resolveConversationRuntimePreference(config);
      if (
        config.conversationRuntime
        && config.runtimeSessionBinding
        && (
          config.runtimeSessionBinding.runtime !== preference[0]
          || config.runtimeSessionBinding.sessionId !== config.sessionId
        )
      ) {
        throw new Error(
          `Runtime session binding mismatch: persisted=${config.runtimeSessionBinding.runtime}:${config.runtimeSessionBinding.sessionId} selected=${preference[0]}:${config.sessionId}`,
        );
      }
      const failures: Array<{ runtime: ConversationRuntimeId; error: unknown }> = [];
      adapters.get('codex')?.setYoloDefaultMode?.(config.tenantId, config.codexYoloDefault === true);

      for (let index = 0; index < preference.length; index++) {
        const runtime = preference[index];
        const entryReceipt = capture?.begin(runtime, config, 'spawn', undefined, false);
        try {
          // If previous runtimes failed, inject fallback context for the new session.
          const spawnConfig = failures.length > 0
            ? {
                ...config,
                ...usageBindingForRuntime(config, runtime),
                runtime,
                fallbackContext: {
                  previousSessionJsonlPath: 'session-not-yet-created',
                  previousRuntime: failures[failures.length - 1].runtime,
                  reason: sanitizeRuntimeError(failures[failures.length - 1].error),
                } satisfies import('./types.js').FallbackContext,
              }
            : { ...config, runtime };
          const process = await adapterFor(runtime).spawn(spawnConfig);
          if (entryReceipt) capture?.finish(entryReceipt, 'completed');
          tenantRuntime.set(config.tenantId, runtime);
          tenantConfig.set(config.tenantId, config);
          if (failures.length > 0) {
            const previous = failures[failures.length - 1];
            emitter.emit('runtime-fallback', {
              tenantId: config.tenantId,
              from: previous.runtime,
              to: runtime,
              reason: sanitizeRuntimeError(previous.error),
              attemptIndex: index,
            } satisfies ConversationRuntimeFallbackEvent);
          }
          return { ...process, runtime };
        } catch (error) {
          if (entryReceipt) capture?.finish(entryReceipt, 'failed');
          failures.push({ runtime, error });
        }
      }

      if (failures.length === 1) {
        throw failures[0].error;
      }
      const summary = failures.map((failure) => `${failure.runtime}: ${sanitizeRuntimeError(failure.error)}`).join('; ');
      throw new Error(`All conversation runtimes failed: ${summary}`);
    },
    async send(tenantId: string, message: StreamJsonMessage, submit?: TurnSubmitOptions): Promise<void> {
      await sendTurn(tenantId, message, undefined, submit);
    },
    async sendIdempotent(
      tenantId: string,
      message: StreamJsonMessage,
      idempotencyKey: string,
      submit?: TurnSubmitOptions,
    ): Promise<void> {
      await sendTurn(tenantId, message, idempotencyKey, submit);
    },
    async injectInput(tenantId: string, text: string): Promise<void> {
      const adapter = adapterForTenant(tenantId);
      if (!adapter.injectInput) {
        throw new Error(`Conversation runtime ${adapter.id} does not support injectInput`);
      }
      await adapter.injectInput(tenantId, text);
    },
    async abortTurn(tenantId: string): Promise<void> {
      const adapter = adapterForTenant(tenantId);
      if (adapter.abortTurn) {
        await adapter.abortTurn(tenantId);
        const turn = activeTurns.get(tenantId)?.[0];
        if (turn?.receipt) capture?.finish(turn.receipt, 'cancelled');
        return;
      }
      const process = adapter.get(tenantId)?.process;
      if (!process) throw new Error(`Conversation runtime ${adapter.id} does not support turn abort`);
      process.kill('SIGINT');
      const turn = activeTurns.get(tenantId)?.[0];
      if (turn?.receipt) capture?.finish(turn.receipt, 'cancelled');
    },
    async setModel(tenantId: string, model: ConversationModelRoute): Promise<void> {
      const adapter = adapterForTenant(tenantId);
      if (!adapter.setModel) {
        throw new Error(`Conversation runtime ${adapter.id} does not support native model switching`);
      }
      await adapter.setModel(tenantId, model);
    },
    async listModels(tenantId: string): Promise<ConversationRuntimeModelOption[]> {
      const adapter = adapterForTenant(tenantId);
      if (!adapter.listModels) return [];
      return adapter.listModels(tenantId);
    },
    async setThinkingLevel(tenantId, level): Promise<void> {
      const adapter = adapterForTenant(tenantId);
      if (!adapter.setThinkingLevel) throw new Error(`Conversation runtime ${adapter.id} does not support native thinking levels`);
      await adapter.setThinkingLevel(tenantId, level);
    },
    async listThinkingLevels(tenantId) {
      const adapter = adapterForTenant(tenantId);
      return adapter.listThinkingLevels ? adapter.listThinkingLevels(tenantId) : [];
    },
    async compact(tenantId, customInstructions) {
      const adapter = adapterForTenant(tenantId);
      if (!adapter.compact) throw new Error(`Conversation runtime ${adapter.id} does not support native compaction`);
      capture?.gap(activeTurns.get(tenantId)?.[0]?.receipt, 'automatic_compaction');
      return adapter.compact(tenantId, customInstructions);
    },
    async getNativeState(tenantId) {
      const adapter = adapterForTenant(tenantId);
      if (!adapter.getNativeState) throw new Error(`Conversation runtime ${adapter.id} does not expose native state`);
      return adapter.getNativeState(tenantId);
    },
    async getNativeSessionStats(tenantId) {
      const adapter = adapterForTenant(tenantId);
      if (!adapter.getNativeSessionStats) throw new Error(`Conversation runtime ${adapter.id} does not expose native session stats`);
      return adapter.getNativeSessionStats(tenantId);
    },
    async activateRuntimeUsageBaseline(tenantId) {
      const adapter = adapterForTenant(tenantId);
      if (!adapter.activateRuntimeUsageBaseline) throw new Error(`Conversation runtime ${adapter.id} does not expose usage baseline activation`);
      return adapter.activateRuntimeUsageBaseline(tenantId);
    },
    async sealRuntimeUsage(tenantId) {
      const adapter = adapterForTenant(tenantId);
      if (!adapter.sealRuntimeUsage) throw new Error(`Conversation runtime ${adapter.id} does not expose usage sealing`);
      return adapter.sealRuntimeUsage(tenantId);
    },
    async kill(tenantId: string): Promise<void> {
      await adapterForTenant(tenantId).kill(tenantId);
      for (const turn of activeTurns.get(tenantId) ?? []) if (turn.receipt) {
        capture?.finish(turn.receipt, 'cancelled');
        rememberAttempt(tenantId, turn.receipt);
      }
      tenantRuntime.delete(tenantId);
      tenantConfig.delete(tenantId);
      activeTurns.delete(tenantId);
      for (const key of compactInjectedSessions) {
        if (key.startsWith(`${tenantId}:`)) compactInjectedSessions.delete(key);
      }
      rolloverHints.forget(`${tenantId}:`);
    },
    async killAll(): Promise<void> {
      await Promise.all(Array.from(adapters.values(), (adapter) => adapter.killAll()));
      for (const [tenantId, turns] of activeTurns) for (const turn of turns) if (turn.receipt) {
        capture?.finish(turn.receipt, 'cancelled');
        rememberAttempt(tenantId, turn.receipt);
      }
      tenantRuntime.clear();
      tenantConfig.clear();
      activeTurns.clear();
      terminalResultErrors.clear();
      compactInjectedSessions.clear();
    },
    get(tenantId: string): ConversationProcess | undefined {
      const runtime = tenantRuntime.get(tenantId);
      if (runtime) return adapters.get(runtime)?.get(tenantId);

      for (const adapter of adapters.values()) {
        const process = adapter.get(tenantId);
        if (process) return process;
      }
      return undefined;
    },
    getAll(): ConversationProcess[] {
      return Array.from(adapters.values()).flatMap((adapter) => adapter.getAll());
    },
    getStatus(): ConversationStatusInfo[] {
      return Array.from(adapters.values()).flatMap((adapter) => adapter.getStatus());
    },
    setYoloMode(tenantId: string, enabled: boolean): void {
      const adapter = adapters.get('codex');
      if (!adapter?.setYoloMode) {
        if (!enabled) return;
        throw new Error('Codex conversation runtime adapter does not support YOLO mode');
      }
      adapter.setYoloMode(tenantId, enabled);
    },
    clearYoloMode(tenantId: string): void {
      adapters.get('codex')?.clearYoloMode?.(tenantId);
    },
    setYoloDefaultMode(tenantId: string, enabled: boolean): void {
      adapters.get('codex')?.setYoloDefaultMode?.(tenantId, enabled);
    },
    getYoloOverrideMode(tenantId: string): boolean | undefined {
      return adapters.get('codex')?.getYoloOverrideMode?.(tenantId);
    },
    getYoloMode(tenantId: string): boolean {
      return adapters.get('codex')?.getYoloMode?.(tenantId) ?? false;
    },
    on(event: string, handler: (...args: unknown[]) => void): void {
      // eslint-disable-next-line @typescript-eslint/no-explicit-any
      emitter.on(event, handler as (...args: any[]) => void);
    },
    emitShutdown(): Set<string> {
      const notified = new Set<string>();
      for (const adapter of adapters.values()) {
        for (const id of adapter.emitShutdown()) notified.add(id);
      }
      return notified;
    },
    async drainBusySessions(timeoutMs: number): Promise<Set<string>> {
      const remaining = new Set<string>();
      const drains: Promise<void>[] = [];
      for (const adapter of adapters.values()) {
        if (!adapter.drainBusySessions) continue;
        drains.push(
          adapter.drainBusySessions(timeoutMs).then((stillBusy) => {
            for (const id of stillBusy) remaining.add(id);
          }).catch(() => { /* best-effort */ }),
        );
      }
      await Promise.all(drains);
      return remaining;
    },
    dispose(): void {
      for (const adapter of adapters.values()) adapter.dispose();
      adapters.clear();
      tenantRuntime.clear();
      tenantConfig.clear();
      activeTurns.clear();
      clearAttempts();
      terminalResultErrors.clear();
      compactInjectedSessions.clear();
      emitter.removeAllListeners();
    },
  } as ConversationRuntimeManager;

  return manager;
}
