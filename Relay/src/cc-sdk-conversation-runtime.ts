import { stripRelaySecrets } from './relay-secrets.js';
import { resolveClaudeRuntimeModel } from './model-context.js';
import {spawn as spawnNative,type ChildProcess} from 'node:child_process';
import {forgetNativeProofOnExit,nativeCoreRuntime,observeNativeSession} from './native-core-runtime.js';
import { reasoningControl } from './reasoning-control.js';
import { parseConversationPoolKey } from './conversation-address.js';
import { ccToolUses, publicWorkEvents, toolTurnEffect } from './work-process-events.js';
import { observeCcRateLimitEvent } from './cc-quota-observation.js';
import { BackgroundTaskTracker, RUNTIME_NOTIFICATION_TURN, isTurnContentRecord, resolveBackgroundWorkHardCapMs, shouldReclaimIdleRuntime, tagRuntimeNotificationTurn } from './runtime-background-work.js';
import { CC_RUNTIME_USAGE_ENV_KEYS, ccRuntimeUsageEnvironment } from './cc-usage-measurement.js';
/** Shape of the SDK's spawnClaudeCodeProcess argument; local so the optional SDK is not needed to compile. */
interface SdkSpawnOptions {
  command: string;
  args: string[];
  cwd?: string;
  env: { [envVar: string]: string | undefined };
  signal: AbortSignal;
}

export const CLAUDE_AGENT_SDK_PACKAGE = '@anthropic-ai/claude-agent-sdk';

/**
 * Load the optional Claude Agent SDK. It is an optionalDependency that `npm ci` installs (every
 * production node has it); an install may leave it out. Only the cc-sdk runtime needs it: when it is
 * missing the cc-sdk turn fails with this message and the other runtimes are unaffected.
 */
export async function loadClaudeAgentSdkQuery(
  importer: (specifier: string) => Promise<{ query?: unknown }> = (specifier) => import(specifier),
): Promise<SdkQuery> {
  let mod: { query?: unknown };
  try {
    mod = await importer(CLAUDE_AGENT_SDK_PACKAGE);
  } catch (error) {
    const code = (error as NodeJS.ErrnoException | undefined)?.code;
    if (code === 'ERR_MODULE_NOT_FOUND' || code === 'MODULE_NOT_FOUND') {
      throw new Error(`cc-sdk runtime needs the optional package ${CLAUDE_AGENT_SDK_PACKAGE}, which is not installed. `
        + `Run \`npm ci\` in Relay/ to install it (it is distributed under Anthropic's own terms), `
        + 'or choose the codex runtime.');
    }
    throw error;
  }
  if (typeof mod.query !== 'function') throw new Error(`${CLAUDE_AGENT_SDK_PACKAGE} does not export query()`);
  return mod.query as SdkQuery;
}

/**
 * cc-sdk conversation runtime adapter — SDK-driven, streaming-input resident
 * session with lazy residence + official `resume`.
 *
 * DESIGN CONTRACT (MVS wi_20260702_029, four laws):
 *  - This is the NEW path, parallel to the legacy `cc` (claude -p) runtime. The
 *    legacy runtime and all its jsonl defenses stay untouched.
 *  - Memory belongs to the ENGINE. This adapter NEVER reads/writes/inspects
 *    anything under ~/.claude/projects/. No leak scrub, no stale-jsonl check, no
 *    unfinished-turn marker — those are the legacy path's bandages and are NOT
 *    ported here (that is slice 2's concern for the message layer, if at all).
 *    Session continuity is delivered purely by the SDK `resume` option and the
 *    engine's own jsonl handling in its separate process.
 *  - Lazy residence is a HARD requirement (1.6G VPS, ~400M free): an idle engine
 *    is gracefully ended after `idleResidenceMs` (default 15min). Its sessionId
 *    survives in state.json; the next message resumes it on demand.
 *  - Auth/endpoint is pure config: `subscription` (default, inject no base_url) /
 *    `apikey` (reserved). Switching paths never touches code — driven by
 *    AgentConfig.ccSdkProviderMode.
 *
 * Env poison guard (per PULSE MEMORY `relay-bare-anthropic-base-url-poisons-claude`):
 * inherited ANTHROPIC_* routing never reaches the engine. `buildSdkEngineEnv()`
 * strips it from process.env; only an explicit `engineRoute` option sets it.
 */
import { EventEmitter } from 'node:events';
import { recordQueuedAtShutdown } from './shutdown-interrupts.js';
import { nextTurnSeq } from './maintenance-barrier.js';
import type {
  ConversationProcess,
  ConversationRuntimeAdapter,
  ConversationSpawnConfig,
  ConversationStatusInfo,
} from './conversation-runtime.js';
import { stripInheritedProviderRoutingEnv } from './provider-routing-env.js';
import { refuseMisroutedTurn, refusedTurnResult, settleInternalTurn } from './internal-turn-fence.js';
import type {
  CcSdkProviderMode,
  ProcessState,
  SessionEndReason,
  StateManager,
  StreamJsonMessage,
} from './types.js';

// Loaded lazily so unit tests can inject a mock SDK without pulling in the real
// native engine binary. See CcSdkRuntimeOptions.sdk.
type SdkQuery = (args: { prompt: AsyncIterable<SdkUserMessage>; options: SdkQueryOptions }) => AsyncIterable<SdkMessage>;

export interface SdkUserMessage {
  type: 'user';
  message: { role: 'user'; content: string | unknown[] };
  parent_tool_use_id: null;
  session_id?: string;
}

export interface SdkQueryOptions {
  model?: string;
  pathToClaudeCodeExecutable?: string;
  allowedTools?: string[];
  permissionMode?: string;
  cwd?: string;
  env?: Record<string, string | undefined>;
  resume?: string;
  maxTurns?: number;
  [key: string]: unknown;
}

export interface SdkMessage {
  type: string;
  subtype?: string;
  session_id?: string;
  message?: unknown;
  [key: string]: unknown;
}

export type { CcSdkProviderMode } from './types.js';

const DEFAULT_IDLE_RESIDENCE_MS = 15 * 60 * 1000; // lazy residence: evict idle engine after 15min
const DEFAULT_MAX_TURNS = 1000;
// If the SDK query() yields NOTHING (not even the `system/init` frame) within
// this window after a turn is pushed, the engine has silently stalled — a rare
// SDK cold-start race where query() hangs producing zero output (reproduced in
// situ ~1/13 fresh spawns). A healthy cold start emits `system/init` within
// ~6-8s, so this is the FIRST-message deadline, NOT a whole-turn budget: 60s is
// generous headroom yet fast enough to self-heal before a user gives up.
const DEFAULT_FIRST_CHUNK_TIMEOUT_MS = 60 * 1000;
// On a silent first-chunk stall we transparently restart the engine and replay
// the in-flight turn this many times before surfacing an error. One retry turns
// a ~1/13 stall into ~1/169 — invisible to the user — while still failing loud
// ("静默失败不可接受") if the engine is genuinely wedged.
const DEFAULT_STALL_RETRIES = 1;
const DEFAULT_PERMISSION_MODE = 'bypassPermissions';

export interface CcSdkRuntimeOptions {
  stateManager?: StateManager;
  /** Idle residence window before an engine is gracefully evicted. Default 15min. */
  idleResidenceMs?: number;
  /** Max wait for the FIRST SDK message after a turn is pushed before treating
   *  the engine as silently stalled. Default 60s. */
  firstChunkTimeoutMs?: number;
  /** How many times a silent first-chunk stall transparently restarts the engine
   *  and replays the in-flight turn before surfacing an error. Default 1. */
  stallRetries?: number;
  /** Override the SDK `query` fn (tests inject a mock). Defaults to the real SDK. */
  sdk?: { query: SdkQuery };
  /** Max residency while background tasks are pending. Default 4 h. */
  backgroundWorkHardCapMs?: number;
  /** Explicit engine route (for example an isolated fixture endpoint). When
   *  absent the engine uses subscription OAuth and no ANTHROPIC_* routing. */
  engineRoute?: { baseUrl: string; authToken: string };
  /** Resolve per-agent provider mode. Defaults to reading AgentConfig-shaped fields
   *  off the spawn config; tests can override. */
  resolveProviderMode?: (config: ConversationSpawnConfig) => CcSdkProviderMode;
}

interface CcSdkSession {
  nativeHandoverRef?:string;
  nativeProcess?:ChildProcess;
  tenantId: string;
  sessionId: string;
  /** WI runtime usage binding env for the engine (same contract as legacy cc / Pi). */
  usageEnv?: Record<string, string>;
  usageBindingSha256?: string;
  workspace: string;
  providerMode: CcSdkProviderMode;
  model?: string;
  /** /think level, passed as the SDK `effort` when the engine starts. */
  effort?: import('./types.js').ConversationReasoningLevel;
  allowedTools?: string[];
  maxTurns: number;
  state: ProcessState;
  lastActivity: number;
  spawnedAt: number;
  restartCount: number;
  restartWindowStart: number;
  /** true while the SDK query loop holds a live engine subprocess. */
  resident: boolean;
  /** Pending user turns fed to the streaming-input iterator. */
  queue: PendingTurn[];
  /** The turn currently being served (last one dequeued/pushed). Retained so a
   *  silent-stall restart can replay it into the fresh engine — the original may
   *  already have been consumed off the queue by the stalled iterator. */
  inFlightTurn?: PendingTurn;
  /** Opaque Host context for the turn currently yielded to this engine. */
  activeTurnContext?: import('./types.js').ConversationTurnContext;
  /** tool_use ids already counted as execution steps in the current turn. */
  turnToolUseIds?: Set<string>;
  /** Monotonic fence: buffered output from a superseded iterator is ignored. */
  generation: number;
  /** Silent first-chunk stall restarts already spent for the in-flight turn. */
  stallRetryCount: number;
  /** Set while an engine start is in flight. Concurrent sends on a cold session
   *  await THIS promise instead of each starting their own engine (the async
   *  race where `resident` is only set after `await getQuery()` let a second
   *  send slip past the `!resident` guard and spawn a duplicate engine). */
  starting?: Promise<void>;
  /** Resolver waiting on the next turn (streaming-input backpressure). */
  waiting?: (turn: IteratorResult<SdkUserMessage>) => void;
  /** Set when a turn is in-flight so idle eviction skips busy sessions. */
  busy: boolean;
  /** Idle-eviction timer handle. */
  idleTimer?: ReturnType<typeof setTimeout>;
  /** Signals the streaming-input iterator to end (graceful eviction). */
  closing: boolean;
  /** Abort handle for the live query loop. */
  abort?: AbortController;
  /** True once the engine has run ≥1 turn (a jsonl exists to resume). Drives the
   *  resume decision: false on a brand-new session's first turn, true after that
   *  (including across idle eviction). Also true when spawn() adopts a sessionId
   *  restored from state.json (prior process already produced the jsonl). */
  hasRunOnce: boolean;
  /** CC background tasks; pending work defers idle eviction up to the hard cap. */
  backgroundTasks: BackgroundTaskTracker;
  /** Turn the engine started itself (background task finished) with no Relay send. */
  notificationTurn?: { announced: boolean };
  /** Human sends parked behind the turn in flight (a Relay turn or a
   *  notification turn), in arrival order; dispatched one by one as turns end,
   *  recorded for replay at shutdown. */
  heldSends?: Array<{ message: StreamJsonMessage; turn?: import('./types.js').ConversationTurnContext; queuedAt: number; seq: number; resolve: () => void; reject: (error: Error) => void }>;
}

interface PendingTurn {
  message: SdkUserMessage;
  turn?: import('./types.js').ConversationTurnContext;
}

/**
 * Build the env passed to the SDK-spawned engine.
 *
 * Poison guard: start from process.env, strip inherited ANTHROPIC_* routing and
 * retired provider credentials, then apply the explicit override, if any.
 */
export function buildSdkEngineEnv(override?: Record<string, string>): Record<string, string> {
  const base: Record<string, string> = {};
  for (const [k, v] of Object.entries(process.env)) {
    if (v === undefined) continue;
    base[k] = v;
  }
  stripInheritedProviderRoutingEnv(base);
  stripRelaySecrets(base);
  return { ...base, ...(override ?? {}) };
}

/**
 * Resolve the SDK session env + model for a given provider mode.
 *  - subscription: inject NOTHING (rely on ~/.claude/.credentials.json OAuth).
 *  - apikey: reserved structure; falls back to subscription env until implemented.
 * An explicit route overrides the endpoint for either mode.
 */
export function resolveProviderEnv(
  mode: CcSdkProviderMode,
  configModel?: string,
  route?: { baseUrl: string; authToken: string },
): { env: Record<string, string>; model?: string } {
  // `apikey` is reserved: an API-key path would inject ANTHROPIC_API_KEY here
  // from a configured secret. Until then it uses the subscription env.
  void mode;
  const override = route ? { ANTHROPIC_BASE_URL: route.baseUrl, ANTHROPIC_AUTH_TOKEN: route.authToken } : undefined;
  return { env: buildSdkEngineEnv(override), model: configModel && resolveClaudeRuntimeModel(configModel) };
}

function assistantText(m: SdkMessage): string {
  if (m.type !== 'assistant') return '';
  const content = (m.message as { content?: unknown } | undefined)?.content;
  if (typeof content === 'string') return content;
  if (!Array.isArray(content)) return '';
  return content
    .filter((b: unknown): b is { type: string; text: string } =>
      typeof b === 'object' && b !== null && (b as { type?: unknown }).type === 'text')
    .map((b) => b.text)
    .join('\n');
}

function messageContent(message: StreamJsonMessage): string | unknown[] {
  const content = message.content;
  if (typeof content === 'string') return content;
  if (Array.isArray(content)) return content;
  return '';
}

export function createCcSdkConversationRuntime(
  options: CcSdkRuntimeOptions = {},
): ConversationRuntimeAdapter {
  const emitter = new EventEmitter();
  emitter.setMaxListeners(0);
  const sessions = new Map<string, CcSdkSession>();
  const idleResidenceMs = options.idleResidenceMs ?? DEFAULT_IDLE_RESIDENCE_MS;
  const backgroundWorkHardCapMs = options.backgroundWorkHardCapMs ?? resolveBackgroundWorkHardCapMs();
  const firstChunkTimeoutMs = options.firstChunkTimeoutMs ?? DEFAULT_FIRST_CHUNK_TIMEOUT_MS;
  const stallRetries = options.stallRetries ?? DEFAULT_STALL_RETRIES;
  const engineRoute = options.engineRoute;

  const resolveProviderMode = options.resolveProviderMode ?? ((config: ConversationSpawnConfig): CcSdkProviderMode => {
    const mode = config.ccSdkProviderMode;
    if (mode === 'apikey' || mode === 'subscription') return mode;
    return 'subscription';
  });

  // Structured logging — the adapter shipped with zero observability, which made
  // engine-start / eviction / resume failures invisible ("静默失败不可接受").
  function log(level: 'debug' | 'info' | 'warn' | 'error', tenantId: string, msg: string, extra: Record<string, unknown> = {}): void {
    const line = { ts: new Date().toISOString(), level, runtime: 'cc-sdk', tenantId, msg, ...extra };
    if (level === 'error') console.error(line);
    else if (level === 'warn') console.warn(line);
    else console.log(line);
  }

  /** Whether spawn() is adopting a sessionId that a prior process already ran
   *  (i.e. a jsonl exists to resume). Signalled by SpawnConfig.resume — the same
   *  flag the legacy cc path uses to distinguish resume vs fresh. */
  function isResumeSpawn(config: ConversationSpawnConfig): boolean {
    return config.resume === true;
  }

  // Lazy-load the real SDK only when no mock is supplied. Kept as a thunk so the
  // native engine dependency is never imported during unit tests.
  let realQuery: SdkQuery | undefined;
  async function getQuery(): Promise<SdkQuery> {
    if (options.sdk) return options.sdk.query;
    if (!realQuery) realQuery = await loadClaudeAgentSdkQuery();
    return realQuery;
  }

  function persistSession(session: CcSdkSession, patch: Record<string, unknown> = {}): void {
    if (!options.stateManager) return;
    options.stateManager.updateSession(session.tenantId, {
      runtime: 'cc-sdk',
      sessionId: session.sessionId,
      lastActivity: session.lastActivity,
      ...patch,
    });
  }

  function clearIdleTimer(session: CcSdkSession): void {
    if (session.idleTimer) {
      clearTimeout(session.idleTimer);
      session.idleTimer = undefined;
    }
  }

  function armIdleTimer(session: CcSdkSession): void {
    clearIdleTimer(session);
    if (!session.resident) return;
    session.idleTimer = setTimeout(() => {
      // Skip eviction if a turn is in-flight, output arrived since arming, or
      // background tasks are still pending below the hard cap.
      if (session.busy || !shouldReclaimIdleRuntime({
        now: Date.now(), lastActivity: session.lastActivity, idleTimeoutMs: idleResidenceMs,
        backgroundWorkSince: session.backgroundTasks.pendingSince(), hardCapMs: backgroundWorkHardCapMs,
      })) {
        armIdleTimer(session);
        return;
      }
      evictSession(session, 'idle-reclaimed');
    }, idleResidenceMs);
    // Do not keep the process alive solely for the idle timer.
    session.idleTimer?.unref?.();
  }

  /** Send parked human turns once the turn in flight has settled. The first
   *  takes the session; the rest park again behind it, still in order. */
  function dispatchHeldSends(session: CcSdkSession): void {
    const held = session.heldSends ?? [];
    session.heldSends = undefined;
    for (const item of held) {
      adapter.send(session.tenantId, item.message, item.turn).then(item.resolve, (error: unknown) =>
        item.reject(error instanceof Error ? error : new Error(String(error))));
    }
  }

  /** Gracefully end the resident engine. sessionId survives for later resume. */
  function evictSession(session: CcSdkSession, reason: 'idle-reclaimed' | 'shutdown'): void {
    if (!session.resident) return;
    clearIdleTimer(session);
    // The engine's background tasks end with it.
    session.backgroundTasks.clear();
    session.closing = true;
    session.resident = false;
    session.state = 'idle';
    // Wake a blocked iterator so the streaming-input loop can observe `closing`.
    const waiting = session.waiting;
    session.waiting = undefined;
    if (waiting) waiting({ value: undefined as never, done: true });
    try {
      session.abort?.abort();
    } catch {
      /* best-effort */
    }
    session.abort = undefined;
    // A shutdown keeps the interrupted-turn flag emitShutdown just wrote.
    persistSession(session, reason === 'idle-reclaimed'
      ? { status: 'idle-reclaimed', wasBusyAtShutdown: false } : { status: 'idle-reclaimed' });
    log('info', session.tenantId, 'cc-sdk engine evicted (sessionId survives for resume)', {
      reason,
      sessionId: session.sessionId ? session.sessionId.slice(0, 8) : '(new)',
    });
    if (reason === 'idle-reclaimed') {
      emitter.emit('session-end', session.tenantId, { type: 'idle-reclaimed' } satisfies SessionEndReason);
    }
  }

  // Streaming-input iterator: yields queued user turns; ends when `closing`.
  function makeTurnIterable(session: CcSdkSession): AsyncIterable<SdkUserMessage> {
    return {
      [Symbol.asyncIterator](): AsyncIterator<SdkUserMessage> {
        return {
          next: (): Promise<IteratorResult<SdkUserMessage>> => {
            const pending = session.queue.shift();
            if (pending) {
              session.activeTurnContext = pending.turn;
              return Promise.resolve({ value: pending.message, done: false });
            }
            if (session.closing) return Promise.resolve({ value: undefined as never, done: true });
            return new Promise((resolve) => {
              session.waiting = resolve;
            });
          },
        };
      },
    };
  }

  function pushTurn(session: CcSdkSession, turn: PendingTurn): void {
    // Retain the context object: manager updates its opaque ref before replay.
    session.inFlightTurn = turn;
    const waiting = session.waiting;
    if (waiting) {
      session.waiting = undefined;
      session.activeTurnContext = turn.turn;
      waiting({ value: turn.message, done: false });
    } else {
      session.queue.push(turn);
    }
  }

  function toSdkUserMessage(message: StreamJsonMessage): SdkUserMessage {
    return {
      type: 'user',
      message: { role: 'user', content: messageContent(message) },
      parent_tool_use_id: null,
    };
  }

  /**
   * Serialize engine starts so concurrent cold sends share ONE engine.
   *
   * Fixes the async race in send(): `resident` is only set inside
   * startEngineLoop AFTER `await getQuery()`, so two near-simultaneous sends
   * both saw `!resident` and each spawned an engine (duplicate query() loops
   * trampling shared session.abort/waiting/queue → silence). Now the FIRST
   * cold send installs `session.starting`; any concurrent send awaits that same
   * promise instead of starting a second engine. The latch is cleared once the
   * engine loop is live (resident=true) or the start failed.
   */
  async function ensureEngineStarted(session: CcSdkSession, resume: boolean): Promise<void> {
    if (session.resident) return;
    if (session.starting) {
      // Another send is already starting the engine — ride its start.
      await session.starting;
      return;
    }
    const startPromise = startEngineLoop(session, resume).finally(() => {
      if (session.starting === startPromise) session.starting = undefined;
    });
    session.starting = startPromise;
    await startPromise;
  }

  /** Start (or resume) the resident engine loop for a session. */
  async function startEngineLoop(session: CcSdkSession, resume: boolean): Promise<void> {
    let query: SdkQuery;
    let env: Record<string, string>;
    let model: string | undefined;
    try {
      query = await getQuery();
      ({ env, model } = resolveProviderEnv(session.providerMode, session.model, engineRoute));
      for (const name of CC_RUNTIME_USAGE_ENV_KEYS) delete env[name];
      if (session.usageEnv) env = { ...env, ...session.usageEnv };
    } catch (error) {
      // SDK import or provider-env resolution failed BEFORE the loop even
      // started — surface it instead of leaving the turn silently stuck.
      session.busy = false;
      session.state = 'dead';
      session.resident = false;
      dispatchHeldSends(session);
      log('error', session.tenantId, 'cc-sdk engine start failed (import/env)', {
        providerMode: session.providerMode,
        error: error instanceof Error ? error.message : String(error),
        stack: error instanceof Error ? error.stack : undefined,
      });
      emitter.emit('error', session.tenantId, error instanceof Error ? error : new Error(String(error)));
      throw error instanceof Error ? error : new Error(String(error));
    }
    const abort = new AbortController();
    const generation = ++session.generation;
    let retiredAttemptRef: string | undefined;
    let retiredSessionId: string | undefined;
    abort.signal.addEventListener('abort', () => {
      // Snapshot scalars BEFORE replay mutates the shared turn context.
      retiredAttemptRef = session.activeTurnContext?.hostAttemptRef;
      retiredSessionId = session.sessionId;
    }, {once:true});
    session.abort = abort;
    session.closing = false;
    session.resident = true;
    session.state = 'ready';

    const options: SdkQueryOptions = {
      allowedTools: session.allowedTools,
      permissionMode: DEFAULT_PERMISSION_MODE,
      cwd: session.workspace,
      env,
      maxTurns: session.maxTurns,
      abortController: abort,
    };
    const core=nativeCoreRuntime(session.tenantId,session.nativeHandoverRef);
    if(core) {
      options.env={...env,...core.env};
      options.mcpServers={pulse:core.server};
      options.allowedTools=Array.from(new Set([...(options.allowedTools ?? []),'mcp__pulse__pulse_work_item','mcp__pulse__pulse_stats','mcp__pulse__pulse_jobs']));
      // Native --session-id is fixed before the first tool, and reused on resume.
      if(!resume) options.sessionId=session.sessionId;
      options.spawnClaudeCodeProcess=(spawn:SdkSpawnOptions)=>{
        const child=spawnNative(spawn.command,spawn.args,{cwd:spawn.cwd,env:spawn.env,
          signal:spawn.signal,stdio:['pipe','pipe','pipe']});
        session.nativeProcess=child;
        forgetNativeProofOnExit(child);
        return child;
      };
    }
    if (model) options.model = model;
    if (session.effort) options.effort = session.effort;
    // Use the same managed Claude Code as the CC pool when resolving Fable.
    const fableExecutable = model === 'fable' ? process.env['CLAUDE_BIN']?.trim() : undefined;
    if (fableExecutable) options.pathToClaudeCodeExecutable = fableExecutable;
    if (resume) options.resume = session.sessionId;

    const iterable = makeTurnIterable(session);
    log('info', session.tenantId, resume ? 'cc-sdk engine resuming' : 'cc-sdk engine starting fresh', {
      providerMode: session.providerMode,
      model,
      workspace: session.workspace,
      sessionId: session.sessionId ? session.sessionId.slice(0, 8) : '(new)',
    });

    // Run the consume loop in the background; it lives until eviction/close.
    // `abort` is captured per-loop: a later resume creates a NEW controller, so we
    // must judge "was this loop deliberately aborted" by THIS loop's signal, not
    // by session.closing (which the next loop resets to false — a race that would
    // otherwise leak the abort rejection as a spurious `error` event).
    // First-chunk watchdog: if the SDK produces no message (not even
    // `system/init`) within the window, the engine has stalled silently — the
    // rare cold-start race reproduced in situ. Cleared on the first yielded
    // message. On stall we first try to SELF-HEAL: abort the wedged query,
    // restart the engine, and replay the in-flight turn (up to `stallRetries`).
    // Only after retries are exhausted do we surface an error ("静默失败不可接受").
    let sawAnyChunk = false;
    const stallTimer = setTimeout(() => {
      if (sawAnyChunk || abort.signal.aborted) return;
      // Tear down THIS wedged loop before doing anything else.
      try { abort.abort(); } catch { /* best-effort */ }

      if (session.stallRetryCount < stallRetries && session.inFlightTurn && !session.closing) {
        session.stallRetryCount++;
        log('warn', session.tenantId, 'cc-sdk engine stalled: no SDK output — restarting engine + replaying turn', {
          firstChunkTimeoutMs,
          attempt: session.stallRetryCount,
          maxAttempts: stallRetries,
          providerMode: session.providerMode,
          model,
          sessionId: session.sessionId ? session.sessionId.slice(0, 8) : '(new)',
        });
        // The stalled cold start never produced a session_id, so the engine is
        // still fresh; restart with the same resume decision, then re-inject the
        // pending turn. `busy` stays true — the turn is still in flight.
        const replay = session.inFlightTurn;
        // The replay is a distinct native/provider attempt. Tell the Host
        // correlation layer before the replacement loop can emit any usage.
        emitter.emit('turn-replay', session.tenantId);
        session.resident = false;
        // Drop any half-consumed queue state from the dead loop.
        session.queue = [];
        session.waiting = undefined;
        // Route through the same latch so a send() arriving mid-restart rides
        // this restart instead of spawning yet another engine.
        ensureEngineStarted(session, resume)
          .then(() => {
            // The turn is still in flight (busy stayed true), so re-assert the
            // 'busy' state that the fresh startEngineLoop reset to 'ready' — keep
            // the restart-safe guard chain from seeing a spurious 'ready' window
            // while a reply is genuinely pending. Invariant: busy → not 'ready'.
            session.state = 'busy';
            if (replay) pushTurn(session, replay);
          })
          .catch((error) => {
            // Restart itself failed (import/env) — surface, don't hang.
            session.busy = false;
            session.state = 'dead';
            session.resident = false;
            dispatchHeldSends(session);
            emitter.emit('error', session.tenantId, error instanceof Error ? error : new Error(String(error)));
            emitter.emit('exit', session.tenantId, 1);
          });
        return;
      }

      // Retries exhausted (or nothing to replay): fail loud.
      log('error', session.tenantId, 'cc-sdk engine stalled: no SDK output within timeout (retries exhausted)', {
        firstChunkTimeoutMs,
        attempts: session.stallRetryCount,
        providerMode: session.providerMode,
        model,
        sessionId: session.sessionId ? session.sessionId.slice(0, 8) : '(new)',
      });
      session.busy = false;
      session.state = 'dead';
      session.resident = false;
      dispatchHeldSends(session);
      const stallError = new Error(`cc-sdk engine produced no output within ${firstChunkTimeoutMs}ms after ${session.stallRetryCount} restart(s) (silent stall)`);
      emitter.emit('error', session.tenantId, stallError);
      emitter.emit('exit', session.tenantId, 1);
    }, firstChunkTimeoutMs);
    stallTimer.unref?.();

    void (async () => {
      try {
        const q = query({ prompt: iterable, options });
        for await (const msg of q) {
          // SDK iterators can yield buffered frames after abort. A replacement
          // generation owns the session; stale output must never enter replay.
          if (generation !== session.generation || abort.signal.aborted) {
            // A stale generation cannot drive the new turn's lifecycle/UI, but
            // already-observed provider usage is still a fact. Route it only to
            // the old immutable Host attempt (or an explicit unpaired gap).
            if (msg.type === 'assistant' || msg.type === 'result') {
              emitter.emit('response', session.tenantId, {...msg,
                session_id:msg.session_id ?? retiredSessionId, host_attempt_ref:retiredAttemptRef,
                host_usage_only:true} as StreamJsonMessage);
            }
            continue;
          }
          if (!sawAnyChunk) { sawAnyChunk = true; clearTimeout(stallTimer); }
          session.lastActivity = Date.now();
          session.backgroundTasks.observe(msg as unknown as Record<string, unknown>);
          if (!session.busy && isTurnContentRecord(msg)) {
            // The engine woke on a finished background task: hold the session
            // busy so human sends wait for this turn's result.
            session.busy = true;
            session.state = 'busy';
            session.notificationTurn = { announced: false };
            session.activeTurnContext = undefined;
            session.turnToolUseIds = undefined;
            clearIdleTimer(session);
            log('info', session.tenantId, 'cc-sdk runtime-notification turn started', { pendingBackgroundTasks: session.backgroundTasks.size });
          }
          const notificationTurn = session.notificationTurn;
          if (msg.type === 'system' && msg.subtype === 'init' && msg.session_id) {
            // Capture (or confirm) session id. In streaming mode init fires every
            // turn; only the first assignment matters for a fresh session.
            session.sessionId = msg.session_id;
            if(session.nativeProcess?.pid)observeNativeSession(session.nativeProcess.pid,session.sessionId,session.tenantId,'cc');
            persistSession(session, { status: 'active' });
          }
          observeCcRateLimitEvent(msg);
          if (msg.type === 'user' && session.activeTurnContext) {
            for (const event of publicWorkEvents('cc-sdk', msg)) {
              emitter.emit('response', session.tenantId, { type: 'work_process', process_event: event,
                correlation_id: session.activeTurnContext.correlationId,
                host_attempt_ref: session.activeTurnContext.hostAttemptRef });
            }
          }
          if (msg.type === 'assistant' && session.activeTurnContext) {
            // One execution step per tool call, as pi counts tool starts.
            const seen = session.turnToolUseIds ??= new Set<string>();
            for (const tool of ccToolUses(msg)) {
              if (seen.has(tool.id)) continue;
              seen.add(tool.id);
              emitter.emit('turn-effect', session.tenantId, toolTurnEffect(tool.name));
            }
          }
          if (msg.type === 'assistant') {
            const relayMsg: StreamJsonMessage = {
              type: 'assistant',
              session_id: session.sessionId,
              message: msg.message,
              content: assistantText(msg),
              delivery_phase: (msg.message as { stop_reason?: string } | undefined)?.stop_reason === 'end_turn' ? 'final' : 'commentary',
              ...(session.activeTurnContext?.hostAttemptRef ? {host_attempt_ref:session.activeTurnContext.hostAttemptRef} : {}),
            };
            if (notificationTurn && !notificationTurn.announced && relayMsg.content) {
              notificationTurn.announced = true;
              emitter.emit('turn-accepted', session.tenantId, RUNTIME_NOTIFICATION_TURN);
            }
            emitter.emit('response', session.tenantId, notificationTurn ? tagRuntimeNotificationTurn(relayMsg) : relayMsg);
          }
          if (msg.type === 'result') {
            session.busy = false;
            session.state = 'ready';
            // The turn completed: clear replay state so a future stall on the
            // NEXT turn gets its own fresh retry budget.
            session.inFlightTurn = undefined;
            session.turnToolUseIds = undefined;
            session.stallRetryCount = 0;
            // A completed turn means a jsonl now exists; future starts must resume.
            session.hasRunOnce = true;
            persistSession(session, { status: 'active' });
            session.notificationTurn = undefined;
            const resultMsg = {
              ...msg,
              runtime: 'cc-sdk',
              ...(session.activeTurnContext?.hostAttemptRef ? {host_attempt_ref:session.activeTurnContext.hostAttemptRef} : {}),
            } as StreamJsonMessage;
            emitter.emit('response', session.tenantId, notificationTurn ? tagRuntimeNotificationTurn(resultMsg) : resultMsg);
            armIdleTimer(session);
            dispatchHeldSends(session);
          }
        }
      } catch (error) {
        // A deliberate eviction/kill aborts THIS loop's controller — not an error.
        if (abort.signal.aborted) return;
        session.busy = false;
        session.state = 'dead';
        session.resident = false;
        dispatchHeldSends(session);
        log('error', session.tenantId, 'cc-sdk engine loop crashed', {
          error: error instanceof Error ? error.message : String(error),
          stack: error instanceof Error ? error.stack : undefined,
        });
        emitter.emit('error', session.tenantId, error instanceof Error ? error : new Error(String(error)));
        emitter.emit('exit', session.tenantId, 1);
      } finally {
        clearTimeout(stallTimer);
        // Only surrender residency/state if this loop still owns the session's
        // live controller; a resume may have already installed a newer one.
        if (session.abort === abort) {
          session.resident = false;
          if (session.state !== 'dead') session.state = 'idle';
          session.backgroundTasks.clear();
          if (session.notificationTurn) {
            // The engine ended mid notification turn; parked sends restart it.
            session.notificationTurn = undefined;
            session.busy = false;
          }
          if (!session.busy) dispatchHeldSends(session);
        }
      }
    })();
  }

  let shuttingDown = false;
  const adapter: ConversationRuntimeAdapter = {
    id: 'cc-sdk',
    admitsTurns: true,
    fencesInternalTurns: true,
    async spawn(config: ConversationSpawnConfig): Promise<ConversationProcess> {
      const existing = sessions.get(config.tenantId);
      if (existing && existing.state !== 'dead') {
        throw new Error(`cc-sdk process already exists for tenant ${config.tenantId}`);
      }
      const now = Date.now();
      const usage = ccRuntimeUsageEnvironment(config.computeCostBinding, config.sessionId,
        process.env['PULSE_SCL_SURFACE_ID']?.trim(), 'cc_sdk');
      if (usage.rejected) log('warn', config.tenantId, 'cc-sdk runtime usage binding not injected', { reason: usage.rejected });
      const session: CcSdkSession = {
        tenantId: config.tenantId,
        sessionId: config.sessionId,
        nativeHandoverRef:config.nativeHandoverRef,
        usageEnv: Object.fromEntries(Object.entries(usage.env).filter((entry): entry is [string, string] => typeof entry[1] === 'string')),
        usageBindingSha256: usage.usageBindingSha256,
        workspace: config.workspace,
        providerMode: resolveProviderMode(config),
        model: config.ccModel,
        effort: config.reasoningLevel && (reasoningControl('cc-sdk').levels() as string[]).includes(config.reasoningLevel)
          ? config.reasoningLevel : undefined,
        allowedTools: config.allowedTools,
        maxTurns: config.maxTurns ?? DEFAULT_MAX_TURNS,
        state: 'starting',
        lastActivity: now,
        spawnedAt: now,
        restartCount: existing ? existing.restartCount + 1 : 0,
        restartWindowStart: existing?.restartWindowStart ?? now,
        resident: false,
        queue: [],
        stallRetryCount: 0,
        backgroundTasks: new BackgroundTaskTracker(),
        busy: false,
        closing: false,
        // A resume-spawn adopts a sessionId whose jsonl a prior process wrote, so
        // the first engine start must resume it, not create a fresh session.
        hasRunOnce: isResumeSpawn(config),
        generation: 0,
      };
      sessions.set(config.tenantId, session);
      // Persist the sessionId immediately (same mechanism as legacy cc path).
      persistSession(session, { status: 'active', poolKey: config.tenantId,
        agentId: parseConversationPoolKey(config.tenantId)?.agentId ?? config.tenantId });
      // The engine loop starts lazily on the first send() so a freshly spawned
      // but never-messaged session holds no resident process.
      session.state = 'ready';
      log('info', session.tenantId, 'cc-sdk spawn (lazy — engine starts on first send)', {
        providerMode: session.providerMode,
        model: session.model,
        resume: session.hasRunOnce,
        sessionId: session.sessionId ? session.sessionId.slice(0, 8) : '(new)',
      });
      return {
        tenantId: session.tenantId,
        runtime: 'cc-sdk',
        process:session.nativeProcess?.exitCode===null && !session.nativeProcess.killed?session.nativeProcess:undefined,
        state: session.state,
        sessionId: session.sessionId,
        lastActivity: session.lastActivity,
        spawnedAt: session.spawnedAt,
        restartCount: session.restartCount,
        restartWindowStart: session.restartWindowStart,
        ...(session.usageBindingSha256 ? { usageBindingSha256: session.usageBindingSha256 } : {}),
      };
    },
    async send(tenantId: string, message: StreamJsonMessage, turn?: import('./types.js').ConversationTurnContext): Promise<void> {
      const session = sessions.get(tenantId);
      if (!session || session.state === 'dead') {
        throw new Error(`No active cc-sdk conversation process for tenant ${tenantId}`);
      }
      if (shuttingDown) throw new Error('Relay is shutting down');
      if (session.notificationTurn || session.busy) {
        // One turn at a time, as in the cc pool and codex: a send waits in
        // Relay for the turn in flight to end instead of reaching the engine
        // mid-turn, so a shutdown knows exactly which turns never started.
        return new Promise<void>((resolve, reject) => {
          (session.heldSends ??= []).push({ message, turn, queuedAt: Date.now(), seq: turn?.arrival?.seq ?? nextTurnSeq(), resolve, reject });
          // Held: the shutdown records it for resume.
          turn?.arrival?.accepted();
          log('debug', tenantId, 'cc-sdk send held behind the turn in flight', { held: session.heldSends.length, notificationTurn: !!session.notificationTurn });
        });
      }
      // An internal delivery starts only in the session it was routed to: a turn in flight while it
      // was held may have moved this conversation to another session (its init updates sessionId).
      if (refuseMisroutedTurn(message, session, tenantId)) {
        emitter.emit('response', tenantId, refusedTurnResult(turn?.hostAttemptRef, turn?.correlationId));
        return;
      }
      session.busy = true;
      session.lastActivity = Date.now();
      // A genuinely new user turn gets a fresh self-heal budget.
      session.stallRetryCount = 0;
      clearIdleTimer(session);
      // Lazy residence: if the engine is not resident (fresh, or evicted after
      // idle), start it now. Resume whenever the engine has already produced a
      // jsonl for this session (hasRunOnce) — false only on a brand-new session's
      // very first turn, when there is no jsonl to resume yet.
      //
      // ensureEngineStarted serializes concurrent cold sends onto ONE start: a
      // second near-simultaneous message awaits the same in-flight start rather
      // than spawning a duplicate engine (the send()/startEngineLoop race).
      // It rethrows import/env failures so the caller (and the feishu/TG send
      // catch) can surface them instead of hanging the turn.
      if (!session.resident) {
        log('debug', tenantId, 'cc-sdk send: engine not resident, starting', { resume: session.hasRunOnce });
        try {
          await ensureEngineStarted(session, session.hasRunOnce);
        } catch (error) {
          settleInternalTurn(message, 'refused');
          throw error;
        }
      }
      // Mark the session as replying so the external restart-safe guard chain
      // (getStatus → /internal/cc-status → pulse-relay-restart-safe.sh) counts
      // this tenant as busy and refuses to SIGKILL it mid-turn. This MUST happen
      // AFTER ensureEngineStarted: on a cold start, startEngineLoop sets
      // session.state = 'ready' when the loop goes live, which would clobber a
      // pre-start 'busy'. Invariant: while session.busy === true, state is never
      // 'ready'/'idle' — it is 'busy' (or a terminal 'dead'). Mirrors the legacy
      // cc/codex path, where "replying" is expressed as state === 'busy'.
      //
      // Guard against a start-time crash race: the engine loop runs on a
      // microtask, so it can fail between ensureEngineStarted resolving and here.
      // Every terminal start-failure path (loop crash, watchdog stall-fail,
      // import/env error) clears session.busy AND sets state='dead' together. We
      // set busy=true at the top of send() and nothing on the happy path clears
      // it before the turn is served, so `!session.busy` here means the turn was
      // already terminated during start — leave the session as the failure path
      // left it (dead + already emitted error/exit) instead of resurrecting it to
      // 'busy'. A later send() will re-spawn it.
      // (Guarding on the boolean, not state === 'dead': TS narrows session.state
      // to non-'dead' after the throw above and cannot model the async
      // engine-loop mutation, flagging a direct comparison as a dead branch.)
      if (!session.busy) {
        settleInternalTurn(message, 'refused');
        return;
      }
      session.state = 'busy';
      emitter.emit('turn-accepted', tenantId);
      // In the engine: the shutdown records the session as interrupted.
      turn?.arrival?.accepted();
      pushTurn(session, {message: toSdkUserMessage(message), turn});
      settleInternalTurn(message, 'consumed');
    },
    async kill(tenantId: string): Promise<void> {
      const session = sessions.get(tenantId);
      if (!session) return;
      clearIdleTimer(session);
      session.closing = true;
      session.state = 'dead';
      const held = session.heldSends ?? [];
      session.heldSends = undefined;
      for (const item of held) item.reject(new Error(`cc-sdk conversation for tenant ${tenantId} was killed`));
      const waiting = session.waiting;
      session.waiting = undefined;
      if (waiting) waiting({ value: undefined as never, done: true });
      try {
        session.abort?.abort();
      } catch {
        /* ignore */
      }
      session.abort = undefined;
      session.resident = false;
      sessions.delete(tenantId);
    },
    async killAll(): Promise<void> {
      await Promise.all(Array.from(sessions.keys(), (tenantId) => adapter.kill(tenantId)));
    },
    get(tenantId: string): ConversationProcess | undefined {
      const session = sessions.get(tenantId);
      if (!session) return undefined;
      return {
        tenantId: session.tenantId,
        runtime: 'cc-sdk',
        process:session.nativeProcess?.exitCode===null && !session.nativeProcess.killed?session.nativeProcess:undefined,
        state: session.state,
        sessionId: session.sessionId,
        lastActivity: session.lastActivity,
        spawnedAt: session.spawnedAt,
        restartCount: session.restartCount,
        restartWindowStart: session.restartWindowStart,
      };
    },
    getAll(): ConversationProcess[] {
      return Array.from(sessions.values()).map((session) => ({
        tenantId: session.tenantId,
        runtime: 'cc-sdk' as const,
        state: session.state,
        sessionId: session.sessionId,
        lastActivity: session.lastActivity,
        spawnedAt: session.spawnedAt,
        restartCount: session.restartCount,
        restartWindowStart: session.restartWindowStart,
      }));
    },
    getStatus(): ConversationStatusInfo[] {
      return Array.from(sessions.values()).map((session) => ({
        tenantId: session.tenantId,
        runtime: 'cc-sdk',
        process:session.nativeProcess?.exitCode===null && !session.nativeProcess.killed?session.nativeProcess:undefined,
        state: session.state,
        pid: undefined,
        lastActivity: session.lastActivity,
        spawnedAt: session.spawnedAt,
        restartCount: session.restartCount,
      }));
    },
    on(event: string, handler: (...args: unknown[]) => void): void {
      // eslint-disable-next-line @typescript-eslint/no-explicit-any
      emitter.on(event, handler as (...args: any[]) => void);
    },
    emitShutdown(): Set<string> {
      shuttingDown = true;
      const notified = new Set<string>();
      let persistedInterrupt = false;
      for (const [tenantId, session] of sessions) {
        const wasBusy = session.busy;
        if (wasBusy) {
          persistSession(session, { wasBusyAtShutdown: true, status: 'active' });
          persistedInterrupt = true;
        }
        // Sends parked behind the turn in flight were accepted but never
        // reached the engine: recorded in arrival order, they resume after
        // restart; for their callers they count as delivered.
        const held = session.heldSends ?? [];
        session.heldSends = undefined;
        if (held.length > 0) {
          recordQueuedAtShutdown(options.stateManager, tenantId, 'cc-sdk', held);
          persistedInterrupt = true;
          for (const item of held) item.resolve();
        }
        // Gracefully end the resident engine; sessionId survives for resume.
        // wasBusy is already captured above for the shutdown notice; once the
        // engine is torn down no turn is in flight, so clear busy to keep the
        // invariant (busy → not 'idle') and stop drainBusySessions from hanging
        // on a session whose engine no longer exists.
        if (session.resident) {
          evictSession(session, 'shutdown');
          session.busy = false;
        }
        emitter.emit('session-end', tenantId, { type: 'relay-shutdown', wasBusy } satisfies SessionEndReason);
        notified.add(tenantId);
      }
      if (persistedInterrupt) options.stateManager?.flush();
      return notified;
    },
    async drainBusySessions(timeoutMs: number): Promise<Set<string>> {
      const deadline = Date.now() + timeoutMs;
      const pollIntervalMs = 200;
      while (Date.now() < deadline) {
        let stillBusy = 0;
        for (const session of sessions.values()) {
          if (session.busy) stillBusy++;
        }
        if (stillBusy === 0) return new Set();
        await new Promise((resolve) => setTimeout(resolve, pollIntervalMs));
      }
      const remaining = new Set<string>();
      for (const [tenantId, session] of sessions) {
        if (session.busy) remaining.add(tenantId);
      }
      return remaining;
    },
    dispose(): void {
      void adapter.killAll();
      emitter.removeAllListeners();
    },
  } as ConversationRuntimeAdapter;

  return adapter;
}
