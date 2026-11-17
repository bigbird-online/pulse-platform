import { stripRelaySecrets } from './relay-secrets.js';
import {codexCoreArguments,forgetNativeProofOnExit,nativeCoreRuntime,observeNativeSession} from './native-core-runtime.js';
import { publicCommentaryEvent } from './public-assistant-progress.js';
import { publicWorkEvents } from './work-process-events.js';
import { captureCodexSourceBoundary, codexNativeModelUsage } from './codex-native-model-usage.js';
import { codexSessionDeclared, withSessionWiReminder } from './session-wi-guard.js';
import { codexTurnUsage } from './collaboration-capture.js';
import { nativeCounts, usageKeys, type Counts, type NativeModelUsage } from './native-model-usage.js';
import { homedir } from 'node:os';
import { spawn as cpSpawn } from 'node:child_process';
import { EventEmitter } from 'node:events';
import { mkdtempSync, readFileSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { createInterface } from 'node:readline';
import { recordQueuedAtShutdown } from './shutdown-interrupts.js';
import { nextTurnSeq } from './maintenance-barrier.js';
import type { ChildProcess } from 'node:child_process';
import type { StateManager, StreamJsonMessage, SessionEndReason, ConversationTurnContext } from './types.js';
import {
  runtimeUsageBindingEnvironment,
  type RuntimeUsageBinding,
} from './runtime-usage-binding.js';
import { buildPulseCodexModelArgs, resolvePulseCodexBin, resolvePulseCodexModel } from './codex-defaults.js';
import { resolveCodexAdditionalWritableRoots, sharedAccessPromptLines, standingAccessPromptLine } from './config-paths.js';
import { standingAccessFor } from './standing-access.js';
import type {
  ConversationAttachment,
  ConversationProcess,
  ConversationRuntimeAdapter,
  ConversationSpawnConfig,
  ConversationStatusInfo,
} from './conversation-runtime.js';

const DEFAULT_TIMEOUT_MS = 10 * 60 * 1000;
const DEFAULT_KILL_GRACE_MS = 5_000;
// Codex 沙箱的额外可写根（--add-dir），取值规则见 config-paths.ts 的
// resolveCodexAdditionalWritableRoots：受管实例与 canonical 布局不变，独立部署为空。
const DEFAULT_ADDITIONAL_WRITABLE_ROOTS = resolveCodexAdditionalWritableRoots();

interface QueueItem {
  /** The submitted message, kept whole so a shutdown can record it for replay. */
  message: StreamJsonMessage;
  queuedAt: number;
  /** Arrival number (see TurnArrival). */
  seq: number;
  prompt: string;
  attachments: ConversationAttachment[];
  /** Opaque Host context, retained until this child settles. */
  turn?: ConversationTurnContext;
  /** Set before SIGINT so the child exit is finalized as a user abort, not a runtime crash. */
  interrupted?: boolean;
}

interface CodexProcess extends ConversationProcess {
  nativeHandoverRef?:string;
  runtime: 'codex';
  workspace: string;
  ccModel?: string;
  computeCostBinding?: RuntimeUsageBinding;
  currentChild?: ChildProcess;
  currentItem?: QueueItem;
  queue: QueueItem[];
  timedOut?: boolean;
  /** Codex exec reports thread-cumulative usage; this is the last such total. */
  threadUsage?: CodexThreadUsage;
  sessionWiGuard?: boolean;
  /** The Agent's standing read paths, named once in a new thread's first prompt. */
  standingAccess?: string[];
}
interface CodexThreadUsage { threadId: string; usage: Counts }
function threadUsage(value: unknown): CodexThreadUsage | undefined {
  const row = value !== null && typeof value === 'object' ? value as Record<string, unknown> : {};
  const usage = nativeCounts(row['usage'], 'codex');
  return typeof row['threadId'] === 'string' && row['threadId'] && usage ? {threadId: row['threadId'], usage} : undefined;
}

export interface CodexConversationRuntimeOptions {
  /** Explicit Host-local native source, not a message-supplied path. */
  nativeSessionsDirectory?: string;
  codexBin?: string;
  timeoutMs?: number;
  killGraceMs?: number;
  stateManager?: StateManager;
  additionalWritableRoots?: string[];
  /** Model catalog for provider-driven env injection */
  modelCatalog?: import('./model-catalog.js').ModelCatalog;
}

export interface CodexCommandSpec {
  command: string;
  args: string[];
  cwd: string;
  outputPath: string;
}

export function buildCodexCommand(input: {
  codexBin?: string;
  workspace: string;
  outputPath: string;
  threadId?: string;
  imagePaths?: string[];
  additionalWritableRoots?: string[];
  yoloMode?: boolean;
  model?: string;
  reasoningEffort?: string;
  coreArguments?: string[];
}): CodexCommandSpec {
  const addDirArgs = Array.from(new Set(input.additionalWritableRoots ?? []))
    .filter((path) => path.trim().length > 0)
    .flatMap((path) => ['--add-dir', path]);
  const permissionArgs = input.yoloMode
    ? ['--dangerously-bypass-approvals-and-sandbox']
    : ['-a', 'never', '-s', 'workspace-write'];
  const baseArgs = [
    ...permissionArgs,
    ...(input.coreArguments ?? []),
    ...buildPulseCodexModelArgs({ model: input.model, reasoningEffort: input.reasoningEffort }),
    '-C', input.workspace,
    ...addDirArgs,
    'exec',
  ];
  const resumeArgs = input.threadId ? ['resume', input.threadId] : [];
  const imageArgs = (input.imagePaths ?? []).flatMap((path) => ['-i', path]);
  const stdinArgs = imageArgs.length > 0 ? [...imageArgs, '--', '-'] : ['-'];
  return {
    command: resolvePulseCodexBin(input.codexBin),
    args: [
      ...baseArgs,
      ...resumeArgs,
      '--skip-git-repo-check',
      '--json',
      '--output-last-message', input.outputPath,
      ...stdinArgs,
    ],
    cwd: input.workspace,
    outputPath: input.outputPath,
  };
}

function isConversationAttachment(value: unknown): value is ConversationAttachment {
  return Boolean(
    value
    && typeof value === 'object'
    && 'kind' in value
    && value.kind === 'image'
    && 'path' in value
    && typeof value.path === 'string',
  );
}

function getConversationAttachments(message: StreamJsonMessage): ConversationAttachment[] {
  const raw = message.attachments;
  if (!Array.isArray(raw)) return [];
  return raw.filter(isConversationAttachment);
}

export function codexMessageToPrompt(message: StreamJsonMessage): string {
  if (typeof message.content === 'string') return message.content;

  if (Array.isArray(message.content)) {
    const parts: string[] = [];
    for (const block of message.content) {
      if (
        block
        && typeof block === 'object'
        && 'type' in block
        && block.type === 'text'
        && 'text' in block
        && typeof block.text === 'string'
      ) {
        parts.push(block.text);
      } else {
        throw new Error('Codex conversation runtime only supports text messages in P0');
      }
    }
    return parts.join('\n\n');
  }

  throw new Error('Codex conversation runtime only supports text messages in P0');
}

function codexMessageToQueueItem(message: StreamJsonMessage, turn?: ConversationTurnContext): QueueItem {
  return {
    message,
    queuedAt: Date.now(),
    seq: turn?.arrival?.seq ?? nextTurnSeq(),
    prompt: codexMessageToPrompt(message),
    attachments: getConversationAttachments(message),
  };
}

export function buildCodexEnv(
  provider?: { baseUrl: string; apiKey: string },
  binding?: RuntimeUsageBinding,
): NodeJS.ProcessEnv {
  const env = stripRelaySecrets({ ...process.env });
  // When a provider is specified, inject OPENAI_BASE_URL + OPENAI_API_KEY
  // for Codex's OpenAI-compatible protocol.
  if (provider) {
    if (provider.baseUrl) env['OPENAI_BASE_URL'] = provider.baseUrl;
    if (provider.apiKey) env['OPENAI_API_KEY'] = provider.apiKey;
  }
  try {
    Object.assign(env, runtimeUsageBindingEnvironment(binding, 'codex'));
  } catch (error) {
    // 异族/损坏的持久绑定只跳过注入并告警，不阻断 spawn。
    console.warn(JSON.stringify({ level: 'warn', msg: 'Codex runtime usage binding not injected',
      reason: error instanceof Error ? error.message : 'invalid binding' }));
  }
  return env;
}

/** Sticky key map for Codex runtime (separate from CC pool's sticky map). */
const codexStickyKeyMap = new Map<string, string>();

/** Resolve an API key from a provider's key pool (with stickiness for Codex). */
function resolveApiKeyForCodex(poolKey: string, provider: import('./model-catalog.js').ProviderDef): { keyEnvVar: string; keyValue: string } | null {
  // 1. Check sticky key
  const sticky = codexStickyKeyMap.get(poolKey);
  if (sticky) {
    const value = process.env[sticky];
    if (value) return { keyEnvVar: sticky, keyValue: value };
    codexStickyKeyMap.delete(poolKey);
  }

  // 2. Find first available key in pool
  for (const envVar of provider.api_key_pool) {
    const value = process.env[envVar];
    if (value) {
      codexStickyKeyMap.set(poolKey, envVar);
      return { keyEnvVar: envVar, keyValue: value };
    }
  }

  return null;
}

/** Clear sticky key for a Codex process (called on 429 / key exhaustion). */
export function clearCodexStickyKey(poolKey: string): void {
  codexStickyKeyMap.delete(poolKey);
}

function extractCodexError(event: Record<string, unknown>): string {
  if (typeof event.message === 'string') return event.message;
  if (event.error && typeof event.error === 'object' && 'message' in event.error) {
    const message = (event.error as { message?: unknown }).message;
    if (typeof message === 'string') return message;
  }
  return JSON.stringify(event);
}

function assistantMessage(text: string, deliveryPhase: 'commentary' | 'final' = 'commentary', eventId?: string, nativeSession?: string): StreamJsonMessage {
  return {
    type: 'assistant',
    message: { content: [{ type: 'text', text }] },
    content: text,
    delivery_phase: deliveryPhase,
    ...(eventId ? { event_id: eventId } : {}),
    ...(nativeSession ? { session_id: nativeSession } : {}),
  };
}

function cleanupAttachments(attachments: ConversationAttachment[]): void {
  for (const attachment of attachments) {
    if (!attachment.cleanup) continue;
    try {
      if (attachment.cleanupRoot && attachment.path.startsWith(attachment.cleanupRoot)) {
        rmSync(attachment.cleanupRoot, { recursive: true, force: true });
      } else {
        rmSync(attachment.path, { force: true });
      }
    } catch {
      // best-effort cleanup; temp file cleanup must not mask runtime result
    }
  }
}

export function createCodexConversationRuntime(
  options: CodexConversationRuntimeOptions = {},
): ConversationRuntimeAdapter {
  const emitter = new EventEmitter();
  const processes = new Map<string, CodexProcess>();
  const yoloDefaultByTenant = new Map<string, boolean>();
  const yoloOverrideByTenant = new Map<string, boolean>();
  const timeoutMs = options.timeoutMs ?? DEFAULT_TIMEOUT_MS;
  const killGraceMs = options.killGraceMs ?? DEFAULT_KILL_GRACE_MS;
  const additionalWritableRoots = options.additionalWritableRoots ?? DEFAULT_ADDITIONAL_WRITABLE_ROOTS;

  function effectiveYoloMode(tenantId: string): boolean {
    if (yoloOverrideByTenant.has(tenantId)) {
      return yoloOverrideByTenant.get(tenantId) ?? false;
    }
    return yoloDefaultByTenant.get(tenantId) ?? false;
  }

  function persistThreadId(tenantId: string, threadId: string): void {
    const existing = options.stateManager?.getSession(tenantId);
    if (!existing) return;
    options.stateManager?.updateSession(tenantId, {
      codexThreadId: threadId,
      runtime: 'codex',
      lastActivity: Date.now(),
    });
    options.stateManager?.flush();
  }

  /** Durable before the result is published: a lost write may drop one turn's
   * delta, but a stale total never makes the next turn absorb this one. */
  function persistThreadUsage(tenantId: string, proc: CodexProcess, next: CodexThreadUsage | undefined): void {
    proc.threadUsage = next;
    if (!options.stateManager?.getSession(tenantId)) return;
    options.stateManager.updateSession(tenantId, {codexThreadUsage: next});
    try { options.stateManager.flushStrict(); } catch { /* the debounced writer retries */ }
  }

  function persistInterruptedPrompt(tenantId: string, proc: CodexProcess): void {
    const prompt = proc.currentItem?.prompt.trim();
    // Always record wasBusyAtShutdown when busy — that's the "user is waiting"
    // signal notifyResumed needs. Only attach interruptedUserMessage when we
    // actually have a prompt to inject on resume; empty prompt → flag-only.
    const patch: Record<string, unknown> = {
      runtime: 'codex',
      codexThreadId: proc.codexThreadId,
      wasBusyAtShutdown: true,
      lastActivity: Date.now(),
      status: 'active',
    };
    if (prompt) patch['interruptedUserMessage'] = prompt;
    options.stateManager?.updateSession(tenantId, patch as Partial<import('./types.js').PersistedSession>);
  }

  function processNext(tenantId: string): void {
    const proc = processes.get(tenantId);
    // After the shutdown began no queued turn starts in the stopping process.
    if (shuttingDown || !proc || proc.state === 'dead' || proc.state === 'busy') return;
    const item = proc.queue.shift();
    if (!item) return;
    void runTurn(tenantId, proc, item);
  }

  function finalizeTurn(
    tenantId: string,
    proc: CodexProcess,
    params: {
      tempDir: string;
      outputPath: string;
      attachments: ConversationAttachment[];
      exitCode: number;
      stderr: string;
      timedOut: boolean;
      sawAssistant: boolean;
      pendingAssistant?: { id: string; text: string };
      turnCompleted: boolean;
      interrupted: boolean;
      resultUsage?: unknown;
      nativeModelUsage?: NativeModelUsage;
      /** The model this exec was launched with (`-m`). */
      requestedModel?: string;
      /** Thread total before this turn; absent means it is not known. */
      threadUsageBaseline?: Counts;
      errorMessage?: string;
    },
  ): void {
    const turn = proc.currentItem?.turn;
    proc.currentChild = undefined;
    proc.currentItem = undefined;
    proc.timedOut = false;
    proc.lastActivity = Date.now();

    let fallbackText = '';
    try {
      fallbackText = readFileSync(params.outputPath, 'utf-8').trim();
    } catch {
      fallbackText = '';
    }

    cleanupAttachments(params.attachments);
    rmSync(params.tempDir, { recursive: true, force: true });

    if (fallbackText && params.pendingAssistant && fallbackText !== params.pendingAssistant.text) {
      emitter.emit('response', tenantId, { type: 'work_process',
        process_event: publicCommentaryEvent(params.pendingAssistant.id, params.pendingAssistant.text),
        correlation_id: turn?.correlationId, host_attempt_ref: turn?.hostAttemptRef });
      params.pendingAssistant = undefined;
    }
    const finalText = fallbackText || params.pendingAssistant?.text;
    if (!params.errorMessage && params.exitCode === 0 && !params.timedOut && !params.interrupted && finalText) {
      emitter.emit('response', tenantId, {
        ...assistantMessage(finalText, 'final', params.pendingAssistant?.id, proc.codexThreadId),
        correlation_id: turn?.correlationId, host_attempt_ref: turn?.hostAttemptRef,
      });
      params.sawAssistant = true;
    } else if (params.pendingAssistant) {
      emitter.emit('response', tenantId, { type: 'work_process',
        process_event: publicCommentaryEvent(params.pendingAssistant.id, params.pendingAssistant.text),
        correlation_id: turn?.correlationId, host_attempt_ref: turn?.hostAttemptRef });
    }

    const resultMetadata = {
      runtime: 'codex' as const,
      ...(params.nativeModelUsage ? {native_model_usage: params.nativeModelUsage} : {}),
      ...(params.requestedModel ? {requested_model: params.requestedModel} : {}),
      ...(params.threadUsageBaseline ? {thread_usage_baseline: params.threadUsageBaseline} : {}),
      ...(proc.codexThreadId ? {session_id:proc.codexThreadId} : {}),
      ...(turn?.hostAttemptRef ? {host_attempt_ref:turn.hostAttemptRef} : {}),
    };
    const exitMetadata = turn?.hostAttemptRef ? {hostAttemptRef: turn.hostAttemptRef} : undefined;
    // Every terminal path publishes the factual usage observed so far with the
    // originating opaque context. Cancellation/timeout are not completions.
    if (params.interrupted) {
      proc.state = 'idle';
      emitter.emit('response', tenantId, {type:'result', subtype:'interrupted', interrupted:true,
        ...(params.resultUsage !== undefined ? {usage:params.resultUsage} : {}), is_error:true, ...resultMetadata});
      emitter.emit('exit', tenantId, 0, exitMetadata);
    } else if (params.timedOut) {
      const error = new Error(`codex conversation runtime timed out after ${timeoutMs}ms`);
      proc.state = 'idle';
      if (params.resultUsage !== undefined) emitter.emit('response', tenantId, {type:'result', usage:params.resultUsage, is_error:true, ...resultMetadata});
      if (turn?.hostAttemptRef) Object.assign(error, {hostAttemptRef: turn.hostAttemptRef});
      emitter.emit('error', tenantId, error);
      emitter.emit('session-end', tenantId, { type: 'timeout', timeoutMs } satisfies SessionEndReason);
      emitter.emit('exit', tenantId, 1, exitMetadata);
    } else if (params.errorMessage || params.exitCode !== 0) {
      const detail = params.errorMessage ?? params.stderr.trim() ?? `codex exited with code ${params.exitCode}`;
      const effectiveExitCode = params.exitCode === 0 ? 1 : params.exitCode;
      proc.state = 'idle';
      if (params.resultUsage !== undefined) emitter.emit('response', tenantId, {type:'result', usage:params.resultUsage, is_error:true, ...resultMetadata});
      const error = new Error(detail);
      if (turn?.hostAttemptRef) Object.assign(error, {hostAttemptRef: turn.hostAttemptRef});
      emitter.emit('error', tenantId, error);
      emitter.emit('session-end', tenantId, { type: 'crash', exitCode: effectiveExitCode } satisfies SessionEndReason);
      emitter.emit('exit', tenantId, effectiveExitCode, exitMetadata);
    } else {
      proc.state = 'idle';
      emitter.emit('response', tenantId, {type:'result', usage:params.resultUsage, ...resultMetadata});
      emitter.emit('exit', tenantId, 0, exitMetadata);
    }

    processNext(tenantId);
  }

  async function runTurn(tenantId: string, proc: CodexProcess, item: QueueItem): Promise<void> {
    proc.state = 'busy';
    proc.lastActivity = Date.now();
    proc.currentItem = item;
    // In flight: the shutdown records it as the interrupted turn.
    item.turn?.arrival?.accepted();

    const tempDir = mkdtempSync(join(tmpdir(), 'relay-codex-conversation-'));
    const outputPath = join(tempDir, 'last-message.txt');

    // Guard: reject CC-only models on Codex runtime instead of silently
    // falling back to Codex's default model. A model whose provider exists in
    // the catalog but is NOT openai-protocol can only run under CC runtime.
    // Unknown models (no provider in catalog, e.g. opus/sonnet aliases) are
    // passed through to Codex unchanged to avoid false positives.
    if (options.modelCatalog && proc.ccModel) {
      const guardProvider = options.modelCatalog.getProviderForModel(proc.ccModel);
      if (guardProvider && guardProvider.protocol !== 'openai') {
        const providerLabel = options.modelCatalog.findById(proc.ccModel)?.provider ?? 'anthropic';
        const message =
          `模型 ${proc.ccModel} 需要 CC 运行时（${providerLabel} 是 anthropic 协议），`
          + 'Codex 运行时无法运行该模型。请用 /model 切换到 Codex 支持的模型（如 gpt-5.5），'
          + '或用 /runtime cc 切到 CC 运行时。';
        finalizeTurn(tenantId, proc, {
          tempDir,
          outputPath,
          attachments: item.attachments,
          exitCode: 1,
          stderr: '',
          timedOut: false,
          sawAssistant: false,
          turnCompleted: false,
          interrupted: false,
          errorMessage: message,
        });
        return;
      }
    }

    let commandModel: string | undefined;
    if (options.modelCatalog && proc.ccModel) {
      const providerDef = options.modelCatalog.getProviderForModel(proc.ccModel);
      if (providerDef?.protocol === 'openai') commandModel = proc.ccModel;
    }

    const requestedModel = resolvePulseCodexModel(commandModel);
    const launchedThread = proc.codexThreadId;
    const command = buildCodexCommand({
      codexBin: options.codexBin,
      workspace: proc.workspace,
      outputPath,
      imagePaths: item.attachments.map((attachment) => attachment.path),
      threadId: proc.codexThreadId,
      coreArguments: codexCoreArguments(tenantId,proc.nativeHandoverRef),
      additionalWritableRoots,
      yoloMode: effectiveYoloMode(tenantId),
      model: requestedModel,
    });
    // Resolve provider env for Codex's OpenAI-compatible protocol
    let codexProvider: { baseUrl: string; apiKey: string } | undefined;
    if (options.modelCatalog && proc.ccModel) {
      const providerDef = options.modelCatalog.getProviderForModel(proc.ccModel);
      if (providerDef?.protocol === 'openai') {
        const baseUrl = process.env[providerDef.base_url_env] ?? '';
        // Resolve API key from pool (with stickiness)
        const keyResult = resolveApiKeyForCodex(tenantId, providerDef);
        const apiKey = keyResult?.keyValue ?? '';
        if (baseUrl && apiKey) {
          codexProvider = { baseUrl, apiKey };
        }
      }
    }

    const sessionsRoot = options.nativeSessionsDirectory
      ?? join(process.env.CODEX_HOME ?? join(homedir(), '.codex'), 'sessions');
    const sourceBoundary = (process.env.PULSE_COMPUTECOST_RECEIPT_DIR || options.nativeSessionsDirectory)
      ? captureCodexSourceBoundary(sessionsRoot, proc.codexThreadId)
      : undefined;
    // Codex declares once per session; the thread's own rollout is the record.
    const guarded = proc.sessionWiGuard && !(proc.codexThreadId && codexSessionDeclared(
      (sourceBoundary ?? captureCodexSourceBoundary(sessionsRoot, proc.codexThreadId))?.prefix?.bytes))
      ? withSessionWiReminder({ type: 'user', content: item.prompt }).content as string
      : item.prompt;
    // Codex reads outside its write roots anyway; a new thread learns the shared
    // repositories and its standing grants once. Its write roots stay as they are.
    const access = proc.codexThreadId ? [] : [
      ...sharedAccessPromptLines(proc.workspace, proc.standingAccess), ...(proc.standingAccess ?? []).map(standingAccessPromptLine)];
    const prompt = access.length ? `${access.join('\n')}\n\n${guarded}` : guarded;
    const child = cpSpawn(command.command, command.args, {
      cwd: command.cwd,
      stdio: ['pipe', 'pipe', 'pipe'],
      env: {...buildCodexEnv(codexProvider, proc.computeCostBinding),...nativeCoreRuntime(tenantId,proc.nativeHandoverRef)?.env},
    });

    forgetNativeProofOnExit(child);
    proc.currentChild = child;
    let stderr = '';
    let finished = false;
    let sawAssistant = false;
    let pendingAssistant: { id: string; text: string } | undefined;
    let publicMessageSequence = 0;
    const publishPending = (): void => {
      if (!pendingAssistant) return;
      emitter.emit('response', tenantId, { type: 'work_process',
        process_event: publicCommentaryEvent(pendingAssistant.id, pendingAssistant.text),
        correlation_id: item.turn?.correlationId, host_attempt_ref: item.turn?.hostAttemptRef });
      pendingAssistant = undefined;
    };
    let turnCompleted = false;
    let resultUsage: unknown;
    let observedThreadId: string | undefined;
    let errorMessage: string | undefined;
    let idleTimer: NodeJS.Timeout | undefined;
    let killTimer: NodeJS.Timeout | undefined;

    const scheduleIdleTimeout = (): void => {
      if (idleTimer) clearTimeout(idleTimer);
      idleTimer = setTimeout(() => {
        if (finished) return;
        proc.timedOut = true;
        try { child.kill('SIGTERM'); } catch { /* ignore */ }
        killTimer = setTimeout(() => {
          if (!finished) {
            try { child.kill('SIGKILL'); } catch { /* ignore */ }
          }
        }, killGraceMs);
      }, timeoutMs);
    };

    const recordActivity = (): void => {
      if (finished) return;
      proc.lastActivity = Date.now();
      scheduleIdleTimeout();
    };

    scheduleIdleTimeout();

    const finish = (exitCode: number): void => {
      if (finished) return;
      finished = true;
      if (idleTimer) clearTimeout(idleTimer);
      if (killTimer) clearTimeout(killTimer);
      // Baseline: zero for a thread this exec started; for a resume, only the
      // total recorded after the immediately preceding turn of the same thread.
      const sameThread = observedThreadId !== undefined
        && (launchedThread === undefined || observedThreadId === launchedThread);
      const threadUsageBaseline = !sameThread ? undefined
        : launchedThread === undefined ? Object.fromEntries(usageKeys('codex').map(k => [k, 0]))
        : proc.threadUsage?.threadId === launchedThread ? proc.threadUsage.usage : undefined;
      const total = nativeCounts(resultUsage, 'codex');
      // A thread that ran without a readable total has an unknown new total.
      if (observedThreadId !== undefined) {
        persistThreadUsage(tenantId, proc, sameThread && total ? {threadId: observedThreadId, usage: total} : undefined);
      }
      finalizeTurn(tenantId, proc, {
        tempDir,
        outputPath,
        attachments: item.attachments,
        exitCode,
        stderr,
        timedOut: proc.timedOut ?? false,
        sawAssistant,
        pendingAssistant,
        turnCompleted,
        interrupted: item.interrupted === true,
        resultUsage,
        requestedModel,
        threadUsageBaseline,
        nativeModelUsage: turnCompleted && observedThreadId === proc.codexThreadId
          ? codexNativeModelUsage(sourceBoundary, observedThreadId, codexTurnUsage(resultUsage, threadUsageBaseline)) : undefined,
        errorMessage,
      });
    };

    const rl = child.stdout ? createInterface({ input: child.stdout }) : null;
    rl?.on('line', (line: string) => {
      const trimmed = line.trim();
      if (!trimmed) return;
      recordActivity();
      let event: Record<string, unknown>;
      try {
        event = JSON.parse(trimmed) as Record<string, unknown>;
      } catch {
        stderr += `Invalid codex JSONL: ${trimmed}\n`;
        return;
      }

      if (event.type === 'thread.started' && typeof event.thread_id === 'string') {
        observedThreadId = event.thread_id;
        if(child.pid) observeNativeSession(child.pid,event.thread_id,tenantId,'codex',proc.sessionId);
        if (!proc.codexThreadId) {
          proc.codexThreadId = event.thread_id;
          persistThreadId(tenantId, event.thread_id);
        }
        return;
      }

      if (event.type === 'turn.started') {
        emitter.emit('turn-accepted', tenantId);
        return;
      }

      // Codex CLI ReasoningItem.text is explicitly a public reasoning summary
      // (sdk/typescript/src/items.ts at rust-v0.153.4), not a raw protocol frame.
      const summaryItem = event.item as { id?: unknown; type?: unknown; text?: unknown } | undefined;
      if (event.type === 'item.completed' && summaryItem?.type === 'reasoning'
          && typeof summaryItem.id === 'string' && typeof summaryItem.text === 'string') {
        publishPending();
        emitter.emit('response', tenantId, { type: 'work_process',
          process_event: publicCommentaryEvent(`codex:summary:${summaryItem.id}`, summaryItem.text, 'reasoning_summary'),
          correlation_id: item.turn?.correlationId, host_attempt_ref: item.turn?.hostAttemptRef });
        return;
      }
      const processEvents = publicWorkEvents('codex', event);
      if (processEvents.length) publishPending();
      for (const processEvent of processEvents) {
        emitter.emit('response', tenantId, { type: 'work_process', process_event: processEvent,
          correlation_id: item.turn?.correlationId, host_attempt_ref: item.turn?.hostAttemptRef });
      }
      if (event.type === 'item.completed') {
        const itemEvent = event.item as { id?: unknown; type?: unknown; text?: unknown; phase?: unknown } | undefined;
        if (itemEvent?.type === 'agent_message' && typeof itemEvent.text === 'string') {
          publishPending();
          sawAssistant = true;
          pendingAssistant = {
            id: typeof itemEvent.id === 'string' ? `codex:${itemEvent.id}` : `codex:message:${++publicMessageSequence}`,
            text: itemEvent.text,
          };
          if (itemEvent.phase === 'commentary') publishPending();
        }
        return;
      }

      if (event.type === 'turn.completed') {
        turnCompleted = true;
        resultUsage = event.usage;
        return;
      }

      if (event.type === 'error' || event.type === 'turn.failed') {
        errorMessage = extractCodexError(event);
      }
    });

    child.stderr?.on('data', (chunk: Buffer) => {
      recordActivity();
      stderr += chunk.toString();
    });

    child.on('error', (err: NodeJS.ErrnoException) => {
      errorMessage = err.message;
      finish(1);
    });

    child.on('exit', (code, signal) => {
      const exitCode = code ?? (signal ? 1 : 0);
      finish(exitCode);
    });

    child.stdin?.write(prompt);
    child.stdin?.end();
  }

  let shuttingDown = false;
  const adapter: ConversationRuntimeAdapter = {
    id: 'codex',
    admitsTurns: true,
    async spawn(config: ConversationSpawnConfig): Promise<ConversationProcess> {
      const standing = await standingAccessFor(config.harnessAgentId);
      const existing = processes.get(config.tenantId);
      if (existing && existing.state !== 'dead') {
        throw new Error(`Process already exists for tenant ${config.tenantId}`);
      }
      if (config.codexYoloDefault === true) {
        yoloDefaultByTenant.set(config.tenantId, true);
      } else {
        yoloDefaultByTenant.delete(config.tenantId);
      }
      const proc: CodexProcess = {
        tenantId: config.tenantId,
        runtime: 'codex',
        state: 'idle',
        sessionId: config.sessionId,
        nativeHandoverRef:config.nativeHandoverRef,
        usageBindingSha256: config.computeCostBinding?.binding_sha256,
        codexThreadId: config.codexThreadId,
        ccModel: config.ccModel,
        computeCostBinding: config.computeCostBinding,
        threadUsage: threadUsage(options.stateManager?.getSession(config.tenantId)?.codexThreadUsage),
        lastActivity: Date.now(),
        spawnedAt: Date.now(),
        restartCount: existing ? existing.restartCount + 1 : 0,
        restartWindowStart: existing?.restartWindowStart ?? Date.now(),
        queue: [],
        workspace: config.workspace,
        sessionWiGuard: config.sessionWiGuard === true,
        standingAccess: standing.map((row) => row.path),
      };
      processes.set(config.tenantId, proc);
      return proc;
    },
    async send(tenantId: string, message: StreamJsonMessage, turn?: ConversationTurnContext): Promise<void> {
      const proc = processes.get(tenantId);
      if (!proc || proc.state === 'dead') {
        throw new Error(`No active codex conversation process for tenant ${tenantId}`);
      }
      if (shuttingDown) throw new Error('Relay is shutting down');
      let item: QueueItem;
      try {
        item = {...codexMessageToQueueItem(message, turn), turn};
      } catch (err) {
        cleanupAttachments(getConversationAttachments(message));
        throw err;
      }
      proc.queue.push(item);
      // Queued: the shutdown records it for resume.
      turn?.arrival?.accepted();
      if (proc.state !== 'busy') processNext(tenantId);
    },
    async abortTurn(tenantId: string): Promise<void> {
      const proc = processes.get(tenantId);
      const child = proc?.currentChild;
      const item = proc?.currentItem;
      if (!proc || proc.state !== 'busy' || !child || !item) {
        throw new Error(`No active codex turn for tenant ${tenantId}`);
      }

      // Mark the exact in-flight queue item before signalling. The child can
      // emit exit synchronously (including in tests), and finalizeTurn must see
      // this as an intentional /stop rather than triggering runtime fallback.
      item.interrupted = true;
      try {
        if (!child.kill('SIGINT')) {
          item.interrupted = false;
          throw new Error(`Failed to interrupt codex turn for tenant ${tenantId}`);
        }
      } catch (error) {
        item.interrupted = false;
        throw error;
      }
    },
    async kill(tenantId: string): Promise<void> {
      const proc = processes.get(tenantId);
      if (!proc) return;
      proc.state = 'dead';
      if (proc.currentItem) cleanupAttachments(proc.currentItem.attachments);
      for (const item of proc.queue) cleanupAttachments(item.attachments);
      proc.queue = [];
      if (proc.currentChild && !proc.currentChild.killed) {
        try { proc.currentChild.kill('SIGTERM'); } catch { /* ignore */ }
      }
      processes.delete(tenantId);
    },
    async killAll(): Promise<void> {
      await Promise.all(Array.from(processes.keys(), (tenantId) => adapter.kill(tenantId)));
    },
    get(tenantId: string): ConversationProcess | undefined {
      return processes.get(tenantId);
    },
    getAll(): ConversationProcess[] {
      return Array.from(processes.values());
    },
    getStatus(): ConversationStatusInfo[] {
      return Array.from(processes.values()).map((proc) => ({
        tenantId: proc.tenantId,
        runtime: 'codex',
        state: proc.state,
        pid: proc.currentChild?.pid,
        lastActivity: proc.lastActivity,
        spawnedAt: proc.spawnedAt,
        restartCount: proc.restartCount,
      }));
    },
    setYoloMode(tenantId: string, enabled: boolean): void {
      yoloOverrideByTenant.set(tenantId, enabled);
    },
    clearYoloMode(tenantId: string): void {
      yoloOverrideByTenant.delete(tenantId);
    },
    setYoloDefaultMode(tenantId: string, enabled: boolean): void {
      if (enabled) {
        yoloDefaultByTenant.set(tenantId, true);
      } else {
        yoloDefaultByTenant.delete(tenantId);
      }
    },
    getYoloOverrideMode(tenantId: string): boolean | undefined {
      return yoloOverrideByTenant.get(tenantId);
    },
    getYoloMode(tenantId: string): boolean {
      return effectiveYoloMode(tenantId);
    },
    on(event: string, handler: (...args: unknown[]) => void): void {
      // eslint-disable-next-line @typescript-eslint/no-explicit-any
      emitter.on(event, handler as (...args: any[]) => void);
    },
    emitShutdown(): Set<string> {
      shuttingDown = true;
      const notified = new Set<string>();
      let persistedInterrupt = false;
      for (const [tenantId, proc] of processes) {
        const wasBusy = proc.state === 'busy';
        if (wasBusy) {
          persistInterruptedPrompt(tenantId, proc);
          persistedInterrupt = true;
        }
        // Turns queued behind the busy one were accepted but never started:
        // recorded in arrival order, they resume after restart.
        if (proc.queue.length > 0) {
          const queued = proc.queue;
          proc.queue = [];
          recordQueuedAtShutdown(options.stateManager, tenantId, 'codex', queued);
          persistedInterrupt = true;
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
        for (const proc of processes.values()) {
          if (proc.state === 'busy') stillBusy++;
        }
        if (stillBusy === 0) return new Set();
        await new Promise(resolve => setTimeout(resolve, pollIntervalMs));
      }
      const remaining = new Set<string>();
      for (const [tenantId, proc] of processes) {
        if (proc.state === 'busy') remaining.add(tenantId);
      }
      return remaining;
    },
    dispose(): void {
      void adapter.killAll();
      yoloDefaultByTenant.clear();
      yoloOverrideByTenant.clear();
      emitter.removeAllListeners();
    },
  } as ConversationRuntimeAdapter;

  return adapter;
}
