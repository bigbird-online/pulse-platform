import {forgetNativeProofOnExit,observeNativeSession} from './native-core-runtime.js';
import { publicCommentaryEvent, publicPiAssistantParts } from './public-assistant-progress.js';
import { shouldReclaimIdleRuntime } from './runtime-background-work.js';
import { publicWorkEvents, toolTurnEffect } from './work-process-events.js';
import { piNativeModelUsage, piNativeTurnUsage } from './native-model-usage.js';
import { publicRuntimeErrorDetail } from './public-runtime-failure.js';
import {retireLegacyPiUsage} from './pi-legacy-usage-retirement.js';
import { randomUUID } from 'node:crypto';
import { spawn as nodeSpawn, type ChildProcessWithoutNullStreams, type SpawnOptionsWithoutStdio } from 'node:child_process';
import { EventEmitter } from 'node:events';
import { join } from 'node:path';
import { StringDecoder } from 'node:string_decoder';
import type {
  ConversationProcess,
  ConversationRuntimeAdapter,
  ConversationSpawnConfig,
  ConversationStatusInfo,
} from './conversation-runtime.js';
import {
  buildPiCommand,
  buildPiDialogueCommand,
  existingRealPath,
  pathWithin,
  validateManifest,
  validateProfile,
  type PiConversationRuntimeAdapter,
  type PiConversationRuntimeOptions,
  type PiIsolationProfile,
  type PiSession,
} from './pi-runtime-config.js';
import type {
  ConversationTurnContext,
  StreamJsonMessage,
} from './types.js';
import { RUNTIME_USAGE_BINDING_SCHEMA, RUNTIME_USAGE_BINDING_SCHEMA_V2 } from './runtime-usage-binding.js';
import { scanToolFileOutbox } from './tool-file-outbox.js';
import { withSessionWiReminder } from './session-wi-guard.js';
import { standingAccessFor, withStandingAccess } from './standing-access.js';
import {
  parsePiUsageBaselineNotification,
  parsePiUsageSealNotification,
  RuntimeUsageSealError,
  verifyPiUsageBaselineArtifact,
  verifyPiUsageSealArtifact,
  type RuntimeUsageBaselineReadback,
  type RuntimeUsageSealReadback,
} from './runtime-usage-seal.js';
export {
  buildPiCommand,
  loadPiRuntimeManifest,
  PI_RUNTIME_MANIFEST_SCHEMA,
} from './pi-runtime-config.js';
export type {
  PiCommandSpec,
  PiConversationRuntimeAdapter,
  PiConversationRuntimeOptions,
  PiIsolationProfile,
  PiRuntimeManifest,
} from './pi-runtime-config.js';
// Classify provider failures and preserve their message through the shared
// redaction boundary; credentials and private URLs must not reach channels.
function nativeTurnFailure(message: Record<string, unknown>): PiSession['nativeTurnFailure'] {
  if (message.stopReason === 'aborted') {
    return { code: 'runtime_turn_aborted', message: '本次回复已中止。会话历史保留，可继续发送消息。' };
  }
  if (message.stopReason !== 'error') return undefined;
  const raw = String(message.errorMessage ?? '').toLowerCase();
  const detail = publicRuntimeErrorDetail(message.errorMessage);
  if (/blocked by our safety systems/.test(raw)) {
    return { code: 'provider_safety_rejected', message: '模型服务的安全检查拒绝了本次请求，回复未完成；服务方未提供具体触发原因。会话历史保留。', detail };
  }
  if (/\b402\b|insufficient credits|insufficient_quota/.test(raw)) {
    return { code: 'provider_quota_exceeded', message: '模型服务拒绝了本次请求：余额或可用额度不足。请检查账户余额、密钥额度及请求大小；处理后可在原会话继续，无需 /new 或 /reset。', detail };
  }
  if (/\b429\b|rate.?limit/.test(raw)) {
    return { code: 'provider_rate_limited', message: '模型服务限流，本次回复未完成。请稍后在原会话重试。', detail };
  }
  if (/\b40[13]\b|unauthorized|authentication/.test(raw)) {
    return { code: 'provider_auth_failed', message: '模型服务认证失败，本次回复未完成。请检查凭据或访问权限；会话历史保留。', detail };
  }
  // 11-14 Oracle Pi: "Your input exceeds the context window of this model".
  if (/context.{0,20}(limit|length|exceed)|maximum context|exceeds? the context window/.test(raw)) {
    return { code: 'provider_context_exceeded', message: '本次请求超出模型上下文限制，回复未完成。需缩减请求或压缩上下文；会话历史保留。', detail };
  }
  return { code: 'provider_request_failed', message: '模型服务请求失败，本次回复未完成。会话历史保留，可在原会话重试。', detail };
}

function assistantHasToolCall(message: unknown): boolean {
  const typed = message && typeof message === 'object' ? message as Record<string, unknown> : {};
  return typed.role === 'assistant' && Array.isArray(typed.content)
    && typed.content.some((block) => !!block && typeof block === 'object'
      && ['toolCall', 'tool_use'].includes(String((block as { type?: unknown }).type)));
}

function piPromptPayload(content: StreamJsonMessage['content']): {
  message: string;
  images?: Array<{ type: 'image'; data: string; mimeType: string }>;
} {
  if (typeof content === 'string' || content === undefined) return { message: content ?? '' };
  if (!Array.isArray(content)) throw new Error('Pi prompt content must be text or content blocks');
  const text: string[] = [];
  const images: Array<{ type: 'image'; data: string; mimeType: string }> = [];
  for (const block of content) {
    if (!block || typeof block !== 'object') throw new Error('Pi prompt contains an invalid content block');
    const typed = block as Record<string, unknown>;
    if (typed.type === 'text' && typeof typed.text === 'string') {
      text.push(typed.text);
      continue;
    }
    const source = typed.source as Record<string, unknown> | undefined;
    if (
      typed.type === 'image'
      && source?.type === 'base64'
      && typeof source.data === 'string'
      && typeof source.media_type === 'string'
    ) {
      images.push({ type: 'image', data: source.data, mimeType: source.media_type });
      continue;
    }
    throw new Error('Pi prompt contains an unsupported content block');
  }
  return { message: text.join(''), ...(images.length ? { images } : {}) };
}

function sanitizeTrace(record: Record<string, unknown>, turn?: ConversationTurnContext): Record<string, unknown> {
  const trace: Record<string, unknown> = { type: record.type };
  if (typeof record.command === 'string') trace.command = record.command;
  if (typeof record.success === 'boolean') trace.success = record.success;
  if (typeof record.toolName === 'string') trace.toolName = record.toolName;
  if (turn) trace.correlationId = turn.correlationId;
  return trace;
}

class PiRpcRequestTimeoutError extends Error {
  constructor(readonly commandType: string) {
    super(`Pi RPC ${commandType} request timed out`);
    this.name = 'PiRpcRequestTimeoutError';
  }
}

export function createPiConversationRuntime(options: PiConversationRuntimeOptions = {}): PiConversationRuntimeAdapter {
  const emitter = new EventEmitter();
  emitter.setMaxListeners(0);
  const sessions = new Map<string, PiSession>();
  const launcher = options.launcher ?? ((command, args, spawnOptions) => nodeSpawn(command, args, spawnOptions));
  const now = options.now ?? Date.now;
  const idleTimeoutMs = options.idleTimeoutMs ?? 30 * 60_000;
  const cleanupIntervalMs = options.cleanupIntervalMs ?? 60_000;
  const requestTimeoutMs = options.requestTimeoutMs ?? 15_000;
  const turnTimeoutMs = options.turnTimeoutMs ?? options.requestTimeoutMs ?? 30 * 60_000;
  const killGraceMs = options.killGraceMs ?? 5_000;
  let disposed = false;

  function getSession(tenantId: string): PiSession {
    const session = sessions.get(tenantId);
    if (!session || session.state === 'dead') throw new Error(`No active Pi conversation process for tenant ${tenantId}`);
    return session;
  }

  function sessionIsDead(session: PiSession): boolean {
    return session.state === 'dead';
  }

  function writeRecord(session: PiSession, record: Record<string, unknown>): void {
    if (!session.child.stdin.writable) throw new Error('Pi RPC stdin is not writable');
    session.child.stdin.write(`${JSON.stringify(record)}\n`);
  }

  function request(
    session: PiSession,
    command: Record<string, unknown>,
    timeoutMs = requestTimeoutMs,
    onSuccess?: () => void,
  ): Promise<Record<string, unknown>> {
    const id = typeof command.id === 'string' ? command.id : `rpc:${randomUUID()}`;
    if (session.pending.has(id)) return Promise.reject(new Error('Duplicate Pi RPC request id'));
    return new Promise((resolveRequest, rejectRequest) => {
      const timer = setTimeout(() => {
        session.pending.delete(id);
        rejectRequest(new PiRpcRequestTimeoutError(String(command.type)));
      }, timeoutMs);
      timer.unref?.();
      session.pending.set(id, { resolve: resolveRequest, reject: rejectRequest, timer, onSuccess });
      try {
        writeRecord(session, { ...command, id });
      } catch (error) {
        clearTimeout(timer);
        session.pending.delete(id);
        rejectRequest(error instanceof Error ? error : new Error(String(error)));
      }
    });
  }

  function failPending(session: PiSession, reason: string): void {
    for (const pending of session.pending.values()) {
      clearTimeout(pending.timer);
      pending.reject(new Error(reason));
    }
    session.pending.clear();
  }

  function protocolFailure(session: PiSession, reason: string): void {
    if (sessions.get(session.tenantId) !== session || session.state === 'dead') return;
    session.state = 'dead';
    failPending(session, reason);
    emitter.emit('error', session.tenantId, new Error(reason));
    emitter.emit('session-end', session.tenantId, {
      type: 'runtime-failed',
      runtime: 'pi',
      correlationId: session.activeTurn?.correlationId,
    });
    try { session.child.kill('SIGKILL'); } catch { /* already gone */ }
  }

  async function finishSettled(session: PiSession): Promise<void> {
    const turn = session.activeTurn;
    if (!turn || session.settling) return;
    session.settling = true;
    const outboxFiles = scanToolFileOutbox(session.tenantId, session.workspace);
    const failure = session.nativeTurnFailure;
    if (failure) console.warn({
      ts: new Date(now()).toISOString(), level: 'warn', msg: 'Pi turn failed',
      poolKey: session.tenantId, correlationId: turn.correlationId,
      provider: session.provider, model: session.model, errorCode: failure.code, detail: failure.detail,
    });
    const text = failure
      ? `${failure.message}${failure.detail ? `\n诊断：${failure.detail}。` : ''}\n[${failure.code}] 请求编号：${turn.correlationId}`
      : session.pendingAssistantText;
    if (text) {
      emitter.emit('response', session.tenantId, {
        type: 'assistant',
        message: { content: [{ type: 'text', text }] },
        content: text,
        runtime: 'pi',
        correlation_id: turn.correlationId,
        delivery_phase: 'final',
        event_id: `${turn.correlationId}:final`,
      } satisfies StreamJsonMessage);
    }
    if (sessions.get(session.tenantId) !== session || session.intentionalExit) return;
    // Business completion never waits for optional session statistics. These
    // already-delivered message_end observations belong only to this dispatch;
    // late cumulative replies cannot leak into the next turn or a resumed one.
    const observedUsage = piNativeTurnUsage(session.modelUsageMessages ?? []);
    const nativeModelUsage = observedUsage && piNativeModelUsage(session.modelUsageMessages ?? [], observedUsage);
    session.modelUsageMessages = undefined;
    session.state = 'idle';
    session.lastActivity = now();
    session.activeTurn = undefined;
    session.pendingAssistantText = '';
    session.settling = false;
    emitter.emit('response', session.tenantId, {
      type: 'result',
      subtype: failure ? 'error_during_execution' : 'success',
      ...(failure ? { is_error: true, error_code: failure.code, errors: [failure.message], public_failure_reported: true } : {}),
      runtime: 'pi',
      session_id: session.sessionId,
      provider: session.provider,
      model: session.model,
      ...(nativeModelUsage ? {native_model_usage: nativeModelUsage} : {}),
      correlation_id: turn.correlationId,
      usage_status: observedUsage ? 'observed_aggregate' : 'unavailable',
      usage: observedUsage,
    } satisfies StreamJsonMessage);
    if (outboxFiles.length > 0) emitter.emit('tool-files', session.tenantId, outboxFiles, session.workspace);
  }

  // A later public message or tool proves the pending text is progress.
  function publishPendingCommentary(session: PiSession, source?: import('./work-process-events.js').WorkProcessEvent['source']): void {
    if (!session.activeTurn || session.settling || !session.pendingAssistantText) return;
    emitter.emit('response', session.tenantId, {
      type: 'work_process',
      process_event: publicCommentaryEvent(session.pendingAssistantEventId!, session.pendingAssistantText, source),
      correlation_id: session.activeTurn.correlationId,
      host_attempt_ref: session.activeTurn.hostAttemptRef,
    });
    session.pendingAssistantText = '';
    session.pendingAssistantEventId = undefined;
  }

  function handleRecord(session: PiSession, record: Record<string, unknown>): void {
    session.lastActivity = now();
    emitter.emit('native-trace', session.tenantId, sanitizeTrace(record, session.activeTurn));
    try {
      const baselineReadback = parsePiUsageBaselineNotification(record);
      if (baselineReadback) session.usageBaselineReadbacks.push(baselineReadback);
    } catch (error) {
      session.usageBaselineReadbackError = error instanceof Error ? error : new Error(String(error));
    }
    try {
      const sealReadback = parsePiUsageSealNotification(record);
      if (sealReadback) session.usageSealReadbacks.push(sealReadback);
    } catch (error) {
      session.usageSealReadbackError = error instanceof Error ? error : new Error(String(error));
    }
    if (record.type === 'response' && typeof record.id === 'string') {
      const pending = session.pending.get(record.id);
      if (pending) {
        clearTimeout(pending.timer);
        session.pending.delete(record.id);
        if (record.success === false) pending.reject(new Error(`Pi RPC ${String(record.command)} failed`));
        else {
          // Publish acceptance before parsing subsequent records in this stdout batch.
          pending.onSuccess?.();
          pending.resolve(record);
        }
      }
      return;
    }
    if (record.type === 'agent_start' && !session.activeTurn) {
      // Relay's Pi configuration has no self-triggered turns; surface the
      // contract break instead of dropping it silently.
      console.warn({ ts: new Date(now()).toISOString(), level: 'warn', msg: 'Pi started a turn without a Relay prompt; output is not routed',
        poolKey: session.tenantId });
    }
    if (record.type === 'tool_execution_start') publishPendingCommentary(session);
    if (session.activeTurn && !session.settling) {
      for (const event of publicWorkEvents('pi', record)) {
        emitter.emit('response', session.tenantId, { type: 'work_process', process_event: event,
          correlation_id: session.activeTurn.correlationId,
          host_attempt_ref: session.activeTurn.hostAttemptRef });
      }
    }
    if (record.type === 'message_update') {
      // Unclassified partial frames remain private. Completed public text
      // is classified below, and settlement alone selects the final answer.
      return;
    }
    if (record.type === 'message_end') {
      const message = record.message as Record<string, unknown> | undefined;
      if (session.activeTurn && !session.settling && message?.role !== 'assistant'
          && message?.usage !== undefined && (session.modelUsageMessages?.length ?? 10001) <= 10000) {
        // Tool subcalls are preserved in native history, not silently omitted
        // from a supposedly complete assistant-only partition.
        session.modelUsageMessages?.push(null);
      }
      if (session.activeTurn && !session.settling && message?.role === 'assistant') {
        publishPendingCommentary(session);
        const observations = session.modelUsageMessages;
        if (observations && observations.length <= 10000) observations.push({
          timestamp: message.timestamp, model: message.model, provider: message.provider, usage: message.usage});
        // A successful native retry supersedes its earlier failed attempt.
        session.nativeTurnFailure = nativeTurnFailure(message);
        if (session.nativeTurnFailure) return;
      }
      if (session.activeTurn && !session.settling) {
        session.publicMessageSequence = (session.publicMessageSequence ?? 0) + 1;
        const parts = publicPiAssistantParts(record.message);
        for (const [index, part] of parts.entries()) {
          const id = part.id ? `pi:${part.id}` : `pi:message:${session.publicMessageSequence}:block:${index}`;
          if (part.kind === 'commentary') {
            publishPendingCommentary(session);
            session.pendingAssistantEventId = id;
            session.pendingAssistantText = part.text;
            publishPendingCommentary(session, part.source);
          } else {
            session.pendingAssistantEventId ??= id;
            session.pendingAssistantText += part.text;
          }
        }
        if (assistantHasToolCall(record.message)) publishPendingCommentary(session);
      }
      return;
    }
    if (record.type === 'tool_execution_start' && typeof record.toolName === 'string') {
      session.pendingAssistantText = '';
      const effect = toolTurnEffect(record.toolName);
      emitter.emit('turn-effect', session.tenantId, effect);
      return;
    }
    if (record.type === 'tool_execution_end' && record.isError !== true && typeof record.toolCallId === 'string') return;
    const appended = record.entry as Record<string, unknown> | undefined;
    if (session.activeTurn && (session.modelUsageMessages?.length ?? 10001) <= 10000
      && (record.type === 'compaction_start' || record.type === 'auto_compaction_start'
        || (record.type === 'entry_appended' && ['usage','compaction','branch_summary'].includes(String(appended?.type))))) {
      session.modelUsageMessages?.push(null);
    }
    if (record.type === 'agent_settled') {
      void finishSettled(session).catch((error) => {
        emitter.emit('error', session.tenantId, error instanceof Error ? error : new Error(String(error)));
      });
    }
  }

  function consumeStdout(session: PiSession, chunk: Buffer): void {
    if (sessions.get(session.tenantId) !== session) return;
    session.stdoutBuffer += session.decoder.write(chunk);
    while (true) {
      const newline = session.stdoutBuffer.indexOf('\n');
      if (newline < 0) break;
      let line = session.stdoutBuffer.slice(0, newline);
      session.stdoutBuffer = session.stdoutBuffer.slice(newline + 1);
      if (line.endsWith('\r')) line = line.slice(0, -1);
      if (!line) continue;
      try {
        const parsed = JSON.parse(line) as unknown;
        if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) throw new Error('not an object');
        handleRecord(session, parsed as Record<string, unknown>);
      } catch {
        protocolFailure(session, 'Pi RPC emitted malformed LF-delimited JSON');
        return;
      }
    }
  }

  async function gracefulKill(session: PiSession): Promise<void> {
    if (session.state === 'dead') return;
    session.intentionalExit = true;
    if (session.activeTurn) {
      try { await request(session, { type: 'abort' }); } catch { /* continue shutdown */ }
    }
    try { session.child.stdin.end(); } catch { /* already closed */ }
    await new Promise<void>((resolveExit) => {
      let settled = false;
      const done = (): void => {
        if (settled) return;
        settled = true;
        resolveExit();
      };
      session.child.once('exit', done);
      try { session.child.kill('SIGTERM'); } catch { done(); return; }
      const timer = setTimeout(() => {
        try { session.child.kill('SIGKILL'); } catch { /* already gone */ }
        done();
      }, killGraceMs);
      timer.unref?.();
    });
    if (sessions.get(session.tenantId) === session) sessions.delete(session.tenantId);
  }

  const cleanupTimer = setInterval(() => {
    if (disposed) return;
    for (const session of sessions.values()) {
      // Every Pi record refreshes lastActivity; Pi has no background tasks.
      if (!session.activeTurn && shouldReclaimIdleRuntime({ now: now(), lastActivity: session.lastActivity, idleTimeoutMs, hardCapMs: Infinity })) {
        emitter.emit('session-end', session.tenantId, { type: 'idle-reclaimed' });
        void gracefulKill(session);
      }
    }
  }, cleanupIntervalMs);
  cleanupTimer.unref?.();

  const adapter: PiConversationRuntimeAdapter = {
    id: 'pi',
    async spawn(config): Promise<ConversationProcess> {
      if (sessions.has(config.tenantId)) throw new Error(`Pi process already exists for tenant ${config.tenantId}`);
      const manifest = validateManifest(options.manifest);
      // The Agent's standing grants open to every session it starts, as SSOT does.
      config = { ...config, sessionAccess: withStandingAccess(config.sessionAccess,
        await standingAccessFor(config.harnessAgentId)) };
      let spec: ReturnType<typeof buildPiCommand>;
      try { spec = buildPiCommand(config, manifest); }
      catch (originalError) {
        try { spec = buildPiDialogueCommand(config, manifest); }
        catch { throw originalError; }
      }
      const profile = validateProfile(manifest, spec.profileId);
      // Persisted legacy attribution is not a live binding for the native core.
      const usageBinding = profile.workItemCoreHost ? undefined : config.computeCostBinding;
      const child = launcher(spec.command, spec.args, { cwd: spec.cwd, env: spec.env, stdio: ['pipe', 'pipe', 'pipe'] });
      forgetNativeProofOnExit(child);
      const session: PiSession = {
        tenantId: config.tenantId,
        runtime: 'pi',
        state: 'starting',
        sessionId: config.sessionId,
        usageBindingSha256: usageBinding?.binding_sha256,
        lastActivity: now(),
        spawnedAt: now(),
        restartCount: 0,
        restartWindowStart: now(),
        process: child,
        child,
        profile,
        workspace: spec.cwd,
        provider: config.conversationRuntime?.model?.provider ?? 'unknown',
        model: config.conversationRuntime?.model?.model ?? 'unknown',
        reasoning: config.conversationRuntime?.model?.reasoning ?? 'off',
        sessionDir: spec.sessionDir,
        sessionWiGuard: config.sessionWiGuard === true && spec.conversationOnly !== true,
        decoder: new StringDecoder('utf8'),
        stdoutBuffer: '',
        stderrTail: '',
        pending: new Map(),
        pendingAssistantText: '',
        intentionalExit: false,
        settling: false,
        usageBinding,
        measurementBaselineReference:profile.workItemCoreHost ? undefined : config.measurementBaselineReference,
        conversationOnly:spec.conversationOnly,
        usageBaselineActivated: !usageBinding || usageBinding.schema !== RUNTIME_USAGE_BINDING_SCHEMA,
        usageBaselineReadbacks: [],
        usageSealReadbacks: [],
      };
      sessions.set(config.tenantId, session);
      child.stdout.on('data', (chunk: Buffer) => consumeStdout(session, chunk));
      child.stdout.on('end', () => {
        session.stdoutBuffer += session.decoder.end();
        if (session.stdoutBuffer.length > 0 && session.state !== 'dead') {
          protocolFailure(session, 'Pi RPC stdout ended with an unterminated JSONL record');
        }
      });
      child.stderr.on('data', (chunk: Buffer) => {
        session.stderrTail = `${session.stderrTail}${chunk.toString('utf8')}`.slice(-4096);
      });
      child.on('error', () => protocolFailure(session, 'Pi isolation launcher failed'));
      child.on('exit', (code) => {
        if (sessions.get(session.tenantId) !== session) return;
        const wasActive = session.state !== 'dead';
        session.state = 'dead';
        failPending(session, 'Pi RPC process exited');
        sessions.delete(session.tenantId);
        emitter.emit('exit', session.tenantId, code);
        if (wasActive && !session.intentionalExit) {
          emitter.emit('session-end', session.tenantId, {
            type: 'runtime-failed',
            runtime: 'pi',
            correlationId: session.activeTurn?.correlationId,
          });
        }
      });
      try {
        const state = await request(session, { type: 'get_state' });
        const native = state.data as Record<string, unknown> | undefined;
        if (!native || native.sessionId !== config.sessionId) throw new Error('Pi native session binding mismatch');
        if(child.pid)observeNativeSession(child.pid,config.sessionId,config.tenantId,'pi');
        const nativeModel = native.model as Record<string, unknown> | undefined;
        if (
          !nativeModel
          || nativeModel.provider !== session.provider
          || nativeModel.id !== session.model
        ) {
          throw new Error('Pi native provider/model/reasoning binding mismatch');
        }
        if (native.thinkingLevel !== session.reasoning) {
          const available = await request(session, { type: 'get_available_thinking_levels' });
          const levels = (available.data as {levels?: unknown} | undefined)?.levels;
          // Pi clamps unsupported requested levels to this model's capabilities.
          // Provider/model identity remains exact; a supported request must match.
          if (!Array.isArray(levels) || levels.includes(session.reasoning)
            || typeof native.thinkingLevel !== 'string' || !levels.includes(native.thinkingLevel)) {
            throw new Error('Pi native thinking level binding mismatch');
          }
          session.reasoning = native.thinkingLevel as PiSession['reasoning'];
        }
        session.conversationGuarded = spec.conversationOnly === true;
        session.state = 'ready';
        if (!session.conversationOnly&&usageBinding?.schema === RUNTIME_USAGE_BINDING_SCHEMA
          && config.runtimeSessionBinding?.runtime === 'pi'
          && config.runtimeSessionBinding.sessionId === config.sessionId) {
          // Optional legacy-source readback. It never gates startup or dispatch;
          // native-core profiles do not load or call this retired producer.
          void adapter.activateRuntimeUsageBaseline!(config.tenantId).catch((error) => {
            if (sessions.get(config.tenantId) !== session) return;
            session.usageBaselineActivated = false;
            emitter.emit('native-trace', config.tenantId, { type: 'usage-baseline-unavailable',
              code: error instanceof RuntimeUsageSealError ? error.code : 'measurement_transport_unavailable' });
          });
        }
        return session;
      } catch (error) {
        await gracefulKill(session);
        throw error;
      }
    },
    async send(tenantId, message, turn): Promise<void> {
      const session = getSession(tenantId);
      // Measurement capture runs independently; it cannot delay dispatch or
      // revoke the Host-admitted tool set when pending, unavailable or sealed.
      if (session.activeTurn || session.state === 'busy') throw new Error(`Pi turn already active for tenant ${tenantId}`);
      const prompt = piPromptPayload((session.sessionWiGuard ? withSessionWiReminder(message) : message).content);
      const generatedTurnId = `turn:${randomUUID()}`;
      const context = turn ?? {
        correlationId: generatedTurnId,
        idempotencyKey: generatedTurnId,
        effect: 'no-effect' as const,
      };
      session.activeTurn = context;
      session.modelUsageMessages = [];
      session.nativeTurnFailure = undefined;
      session.state = 'busy';
      session.pendingAssistantText = '';
      session.pendingAssistantEventId = undefined;
      session.publicMessageSequence = 0;
      session.settling = false;
      try {
        await request(session, { id: context.idempotencyKey, type: 'prompt', ...prompt }, turnTimeoutMs, () => {
          if (session.activeTurn === context && !sessionIsDead(session)) emitter.emit('turn-accepted', tenantId);
        });
      } catch (error) {
        if (error instanceof PiRpcRequestTimeoutError) {
          protocolFailure(session, `${error.message}; Pi runtime terminated to prevent detached execution`);
        } else if (!sessionIsDead(session)) {
          session.activeTurn = undefined;
          session.state = 'ready';
        }
        throw error;
      }
    },
    async steer(tenantId, message): Promise<void> {
      const session = getSession(tenantId);
      if (!session.activeTurn || session.state !== 'busy') {
        throw new Error(`No active Pi turn to steer for tenant ${tenantId}`);
      }
      const prompt = piPromptPayload(message.content);
      await request(session, { type: 'steer', ...prompt });
    },
    async getNativeState(tenantId): Promise<Record<string, unknown>> {
      const response = await request(getSession(tenantId), { type: 'get_state' });
      return (response.data as Record<string, unknown>) ?? {};
    },
    async abortTurn(tenantId): Promise<void> {
      await request(getSession(tenantId), { type: 'abort' });
    },
    async setModel(tenantId, model): Promise<void> {
      const session = getSession(tenantId);
      if (session.activeTurn || session.state === 'busy') {
        throw new Error(`Pi model cannot switch while a turn is active for tenant ${tenantId}`);
      }
      const previous = {
        provider: session.provider,
        model: session.model,
        reasoning: session.reasoning,
      };
      try {
        await request(session, { type: 'set_model', provider: model.provider, modelId: model.model });
        // set_model may change Pi's effective thinking level based on the new
        // model. Re-assert the reviewed route even when the requested level is
        // the same as the prior model's level.
        await request(session, { type: 'set_thinking_level', level: model.reasoning });
        const response = await request(session, { type: 'get_state' });
        const state = response.data as Record<string, unknown> | undefined;
        const nativeModel = state?.model as Record<string, unknown> | undefined;
        if (
          !nativeModel
          || nativeModel.provider !== model.provider
          || nativeModel.id !== model.model
          || state?.thinkingLevel !== model.reasoning
        ) {
          throw new Error('Pi native model switch verification failed');
        }
      } catch (error) {
        try {
          await request(session, { type: 'set_model', provider: previous.provider, modelId: previous.model });
          await request(session, { type: 'set_thinking_level', level: previous.reasoning });
        } catch (rollbackError) {
          throw new Error(
            `Pi native model switch failed and rollback failed: ${error instanceof Error ? error.message : String(error)}; rollback=${rollbackError instanceof Error ? rollbackError.message : String(rollbackError)}`,
          );
        }
        throw error;
      }
      session.provider = model.provider;
      session.model = model.model;
      session.reasoning = model.reasoning;
    },
    async listModels(tenantId) {
      const response = await request(getSession(tenantId), { type: 'get_available_models' });
      const data = response.data as Record<string, unknown> | undefined;
      const models = Array.isArray(data?.models) ? data.models : [];
      return models.flatMap((candidate) => {
        if (!candidate || typeof candidate !== 'object') return [];
        const model = candidate as Record<string, unknown>;
        if (typeof model.provider !== 'string' || typeof model.id !== 'string') return [];
        return [{
          provider: model.provider,
          model: model.id,
          displayName: typeof model.name === 'string' ? model.name : model.id,
        }];
      });
    },
    async setThinkingLevel(tenantId, level): Promise<void> {
      const session = getSession(tenantId);
      if (session.activeTurn || session.state === 'busy') {
        throw new Error(`Pi thinking level cannot switch while a turn is active for tenant ${tenantId}`);
      }
      const previous = session.reasoning;
      try {
        await request(session, { type: 'set_thinking_level', level });
        const response = await request(session, { type: 'get_state' });
        const state = response.data as Record<string, unknown> | undefined;
        if (state?.thinkingLevel !== level) throw new Error('Pi native thinking level verification failed');
      } catch (error) {
        try {
          await request(session, { type: 'set_thinking_level', level: previous });
        } catch (rollbackError) {
          throw new Error(
            `Pi native thinking switch failed and rollback failed: ${error instanceof Error ? error.message : String(error)}; rollback=${rollbackError instanceof Error ? rollbackError.message : String(rollbackError)}`,
          );
        }
        throw error;
      }
      session.reasoning = level;
    },
    async listThinkingLevels(tenantId) {
      const response = await request(getSession(tenantId), { type: 'get_available_thinking_levels' });
      const data = response.data as Record<string, unknown> | undefined;
      const allowed = new Set(['off', 'minimal', 'low', 'medium', 'high', 'xhigh', 'max']);
      return Array.isArray(data?.levels)
        ? data.levels.filter((level): level is import('./types.js').ConversationReasoningLevel => typeof level === 'string' && allowed.has(level))
        : [];
    },
    async compact(tenantId, customInstructions) {
      const session = getSession(tenantId);
      if (session.activeTurn || session.state === 'busy') {
        throw new Error(`Pi context cannot compact while a turn is active for tenant ${tenantId}`);
      }
      const response = await request(session, {
        type: 'compact',
        ...(customInstructions?.trim() ? { customInstructions: customInstructions.trim() } : {}),
      });
      return (response.data as Record<string, unknown> | undefined) ?? {};
    },
    async getNativeSessionStats(tenantId) {
      const response = await request(getSession(tenantId), { type: 'get_session_stats' });
      return (response.data as Record<string, unknown> | undefined) ?? {};
    },
    async activateRuntimeUsageBaseline(tenantId): Promise<RuntimeUsageBaselineReadback> {
      const session = getSession(tenantId);
      if (session.conversationOnly) throw new RuntimeUsageSealError('measurement_authority_unavailable',
        'Original conversation retained without current workspace admission');
      if (session.usageBaselineActivation) return session.usageBaselineActivation;
      const activation = (async (): Promise<RuntimeUsageBaselineReadback> => {
      if (session.activeTurn || session.state === 'busy' || session.state === 'starting') {
        throw new RuntimeUsageSealError('not_settled', 'Pi usage baseline cannot activate during a runtime turn');
      }
      if (!session.usageBinding || session.usageBinding.schema !== RUNTIME_USAGE_BINDING_SCHEMA) {
        throw new RuntimeUsageSealError('authority_mismatch', 'Pi bounded usage is unavailable for an unbound or legacy session');
      }
      const available = await request(session, { type: 'get_commands' });
      const commands = (available.data as { commands?: unknown } | undefined)?.commands;
      if (
        !Array.isArray(commands)
        || !commands.some((item) => !!item && typeof item === 'object'
          && (item as { name?: unknown }).name === 'runtime-usage-baseline'
          && (item as { source?: unknown }).source === 'extension')
      ) {
        throw new RuntimeUsageSealError('command_transport_failed', 'Pi usage baseline extension command is unavailable');
      }
      session.usageBaselineReadbacks = [];
      session.usageBaselineReadbackError = undefined;
      try {
        await request(session, { type: 'prompt', message: '/runtime-usage-baseline' });
      } catch (error) {
        if (error instanceof PiRpcRequestTimeoutError) {
          // Preserve the unknown measurement result for readback. This affects
          // measured coverage only, never dispatch or execution authority.
          session.usageBaselineActivated = false;
          throw new RuntimeUsageSealError('measurement_transport_unavailable',
            'Pi usage baseline acknowledgement timed out; read back the original result', true);
        }
        throw new RuntimeUsageSealError('command_transport_failed', 'Pi usage baseline command was rejected');
      }
      session.lastActivity = now();
      if (session.usageBaselineReadbackError) throw session.usageBaselineReadbackError;
      if (session.usageBaselineReadbacks.length === 0) {
        throw new RuntimeUsageSealError('receipt_missing', 'Pi usage baseline returned no artifact receipt');
      }
      if (session.usageBaselineReadbacks.length !== 1) {
        throw new RuntimeUsageSealError('receipt_ambiguous', 'Pi usage baseline returned multiple artifact receipts');
      }
      const readback = await verifyPiUsageBaselineArtifact({
        outputDir: options.usageSnapshotDir ?? join(session.sessionDir, 'compute-cost'),
        binding: session.usageBinding,
        baselineReference:session.measurementBaselineReference,
        envelope: session.usageBaselineReadbacks[0]!,
      });
      session.usageBaselineActivated = true;
      session.usageSealed = session.usageSealed || readback.sealed === true;
      return readback;
      })();
      session.usageBaselineActivation = activation;
      try { return await activation; }
      finally { if (session.usageBaselineActivation === activation) session.usageBaselineActivation = undefined; }
    },
    async sealRuntimeUsage(tenantId): Promise<RuntimeUsageSealReadback> {
      const session = getSession(tenantId);
      if (session.activeTurn || session.state === 'busy' || session.state === 'starting') {
        throw new RuntimeUsageSealError('not_settled', 'Pi usage cannot seal before the runtime settles',true);
      }
      if(session.usageBinding?.schema===RUNTIME_USAGE_BINDING_SCHEMA_V2){
        const native=await request(session,{type:'get_state'});
        const receipt=await retireLegacyPiUsage({binding:session.usageBinding,sessionId:session.sessionId,
          sessionDir:session.sessionDir,nativeState:(native.data??{}) as Record<string,unknown>,
          outputDir:options.usageSnapshotDir??join(session.sessionDir,'compute-cost')});
        session.usageBaselineActivated=false;
        return receipt;
      }
      if (!session.usageBinding || session.usageBinding.schema !== RUNTIME_USAGE_BINDING_SCHEMA) {
        throw new RuntimeUsageSealError('authority_mismatch', 'Pi bounded usage is unavailable for an unbound or legacy session');
      }
      if (!session.usageBaselineActivated) {
        throw new RuntimeUsageSealError('authority_mismatch', 'Pi usage baseline was not activated');
      }
      const available = await request(session, { type: 'get_commands' });
      const commands = (available.data as { commands?: unknown } | undefined)?.commands;
      if (
        !Array.isArray(commands)
        || !commands.some((item) => !!item && typeof item === 'object'
          && (item as { name?: unknown }).name === 'runtime-usage-seal'
          && (item as { source?: unknown }).source === 'extension')
      ) {
        throw new RuntimeUsageSealError('command_transport_failed', 'Pi usage seal extension command is unavailable');
      }
      session.usageSealReadbacks = [];
      session.usageSealReadbackError = undefined;
      try {
        await request(session, { type: 'prompt', message: '/runtime-usage-seal' });
      } catch (error) {
        if (error instanceof PiRpcRequestTimeoutError) {
          // Unknown sealing is a measurement gap, not an execution fence.
          session.usageBaselineActivated = false;
          throw new RuntimeUsageSealError('measurement_transport_unavailable',
            'Pi usage seal acknowledgement timed out; read back the original result', true);
        }
        throw new RuntimeUsageSealError('command_transport_failed', 'Pi usage seal command was rejected');
      }
      session.lastActivity = now();
      if (session.usageSealReadbackError) throw session.usageSealReadbackError;
      if (session.usageSealReadbacks.length === 0) {
        throw new RuntimeUsageSealError('receipt_missing', 'Pi usage seal returned no artifact receipt');
      }
      if (session.usageSealReadbacks.length !== 1) {
        throw new RuntimeUsageSealError('receipt_ambiguous', 'Pi usage seal returned multiple artifact receipts');
      }
      return verifyPiUsageSealArtifact({
        outputDir: options.usageSnapshotDir ?? join(session.sessionDir, 'compute-cost'),
        binding: session.usageBinding,
        baselineReference:session.measurementBaselineReference,
        envelope: session.usageSealReadbacks[0]!,
      });
    },
    async switchSession(tenantId, sessionPath): Promise<void> {
      const session = getSession(tenantId);
      const candidate = existingRealPath(sessionPath, 'session path');
      if (!pathWithin(session.sessionDir, candidate)) throw new Error('Pi session path escaped the tenant session directory');
      const response = await request(session, { type: 'switch_session', sessionPath: candidate });
      const data = response.data as { cancelled?: unknown } | undefined;
      if (data?.cancelled === true) throw new Error('Pi session switch was cancelled');
    },
    async kill(tenantId): Promise<void> {
      await gracefulKill(getSession(tenantId));
    },
    async killAll(): Promise<void> {
      await Promise.all(Array.from(sessions.values(), (session) => gracefulKill(session)));
    },
    get(tenantId): ConversationProcess | undefined {
      return sessions.get(tenantId);
    },
    getAll(): ConversationProcess[] {
      return Array.from(sessions.values());
    },
    getStatus(): ConversationStatusInfo[] {
      return Array.from(sessions.values(), (session) => ({
        tenantId: session.tenantId,
        runtime: 'pi',
        state: session.state,
        pid: session.child.pid,
        lastActivity: session.lastActivity,
        spawnedAt: session.spawnedAt,
        restartCount: session.restartCount,
      }));
    },
    on: ((event: string, handler: (...args: unknown[]) => void): void => {
      // EventEmitter owns runtime validation; overloads live on the adapter contract.
      // eslint-disable-next-line @typescript-eslint/no-explicit-any
      emitter.on(event, handler as (...args: any[]) => void);
    }) as ConversationRuntimeAdapter['on'],
    emitShutdown(): Set<string> {
      const notified = new Set<string>();
      for (const session of sessions.values()) {
        notified.add(session.tenantId);
        emitter.emit('session-end', session.tenantId, { type: 'relay-shutdown', wasBusy: !!session.activeTurn });
        session.intentionalExit = true;
        if (session.activeTurn) writeRecord(session, { type: 'abort', id: `shutdown:${randomUUID()}` });
        try { session.child.kill('SIGTERM'); } catch { /* already stopped */ }
      }
      return notified;
    },
    async drainBusySessions(timeoutMs): Promise<Set<string>> {
      const deadline = now() + timeoutMs;
      while (now() < deadline && Array.from(sessions.values()).some((session) => !!session.activeTurn)) {
        await new Promise((resolveWait) => setTimeout(resolveWait, Math.min(25, Math.max(1, deadline - now()))));
      }
      return new Set(Array.from(sessions.values()).filter((session) => !!session.activeTurn).map((session) => session.tenantId));
    },
    dispose(): void {
      if (disposed) return;
      disposed = true;
      clearInterval(cleanupTimer);
      for (const session of sessions.values()) {
        session.intentionalExit = true;
        try { session.child.kill('SIGTERM'); } catch { /* best effort */ }
      }
      sessions.clear();
      emitter.removeAllListeners();
    },
  };

  return adapter;
}
