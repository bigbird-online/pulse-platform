import { stripRelaySecrets } from './relay-secrets.js';
import { spawn as cpSpawn } from 'node:child_process';
import { randomUUID } from 'node:crypto';
import { mkdtempSync, readFileSync, realpathSync, rmSync, statSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { dirname, join } from 'node:path';
import { createInterface } from 'node:readline';
import { fileURLToPath } from 'node:url';
import { buildPulseCodexModelArgs, resolvePulseCodexBin, resolvePulseCodexModel, resolvePulseRuntimePriority } from './codex-defaults.js';
import type { RuntimeId } from './types.js';
import { isRateLimitFailureMessage, observeCcRateLimitEvent } from './cc-quota-observation.js';
import { spawnOwnedProcess } from './owned-process.js';
import { forgetNativeSession, observeScheduledSession, scheduledCodexCoreArguments, type ScheduledNativeSession } from './native-core-runtime.js';
import { captureScheduledSession } from './collaboration-capture.js';
import { stripInheritedProviderRoutingEnv } from './provider-routing-env.js';
import { CC_OAUTH_ENV_FILE, readCcSubscriptionToken, assertNoCcTokenCopies, applyCcSubscriptionAuth, ccSubscriptionSettings } from './cc-subscription-auth.js';
import { addCcWriteBoundarySettings, ccPiStateRoot, ccWriteRoots, type CcRepoAccess, type PiAgentLease } from './cc-write-boundary.js';
import { ccHostMcp, type CcSessionWiMcp } from './cc-session-wi-mcp.js';

const DEFAULT_RUNTIME_COOLDOWN_MS = 5 * 60 * 1000;
const SIGKILL_DELAY_MS = 5_000;

export interface BatchRunInput {
  /** Recheck execution permission before each provider/fallback attempt. */
  beforeAttempt?: () => void;
  workspace: string;
  prompt: string;
  timeoutMs: number;
  /** Additional non-secret run context exposed to the batch child process. */
  env?: NodeJS.ProcessEnv;
  /** The scheduler run recorded in the native session proof of each attempt. */
  nativeSession?: ScheduledNativeSession;
  maxTurns?: number;
  /** The owner Agent's `ccWriteBoundary`: a CC run may write only inside its workspace. */
  ccWriteBoundary?: boolean;
  /** The owner Agent's `commitRepos` and `allowedDomains`, applied inside its `ccWriteBoundary`. */
  ccRepoAccess?: CcRepoAccess;
  codexYolo?: boolean;
  ccModel?: string;
  ccReasoningEffort?: string;
  codexModel?: string;
  codexReasoningEffort?: string;
}

export interface BatchRunResult {
  runtime: RuntimeId;
  stdout: string;
  stderr: string;
  exitCode: number;
  durationMs: number;
  timedOut: boolean;
  spawnErrorCode?: string;
  spawnErrorMessage?: string;
  attemptedRuntimes?: RuntimeId[];
  /** Every attempt that failed before this result: its stdout and stderr, and when it ended. */
  failedAttempts?: Array<{ output: string; endedAt: string }>;
}

export interface RuntimeFailure {
  reason: 'binary-missing' | 'startup-failure' | 'auth' | 'quota' | 'rate-limit' | 'timeout' | 'context-window' | 'provider-rejection';
  message: string;
  cooldownMs?: number;
}

/** What a CC batch adapter adds to every run: the session WI MCP a conversation also gets. */
export interface CcBatchOptions {
  sessionWiMcp?: CcSessionWiMcp;
}

export interface BatchRuntimeAdapter {
  id: RuntimeId;
  run(input: BatchRunInput): Promise<BatchRunResult>;
  classifyFailure(result: BatchRunResult): RuntimeFailure | null;
}

export interface BatchRuntimeOrchestratorOptions {
  primary?: RuntimeId;
  fallbacks?: RuntimeId[];
  cooldownMs?: number;
  adapters: BatchRuntimeAdapter[];
}

export interface BatchRuntimeOrchestrator {
  run(input: BatchRunInput, preferredRuntimes?: RuntimeId[]): Promise<BatchRunResult>;
  markHealthy(runtime: RuntimeId): void;
  markUnavailable(runtime: RuntimeId, cooldownMs?: number): void;
}

interface CommandSpec {
  runtime: RuntimeId;
  command: string;
  args: string[];
  cwd: string;
  env: NodeJS.ProcessEnv;
  stdinText?: string;
  streamJson?: boolean;
  outputPath?: string;
  cleanupPath?: string;
  /** A bounded CC run's Pi agent directory (leasePiAgentDir): owned by the child, released when it ends. */
  piAgentLease?: PiAgentLease;
  /** CC names its session at launch; Codex prints it in its stderr header. */
  sessionId?: string;
  nativeSession?: ScheduledNativeSession;
}

function buildCleanEnv(): NodeJS.ProcessEnv {
  const env = stripRelaySecrets({ ...process.env });
  // BatchRunInput.env is merged after this, so an explicit caller route still applies.
  stripInheritedProviderRoutingEnv(env);
  return env;
}

/**
 * A declared Relay subscription credential applies to scheduled CC runs the
 * same way it applies to conversation sessions; an explicit caller route wins.
 * The run gets the conversation's Host MCP servers as well, so a scheduled
 * session can declare its WI and use `pulse_work_item`.
 */
function ccBatchLaunch(input: BatchRunInput, options: CcBatchOptions): {
  env: NodeJS.ProcessEnv; settingsArgs: string[]; mcpArgs: string[]; piAgentLease?: PiAgentLease;
} {
  const env = { ...buildCleanEnv(), ...input.env };
  const oauthEnvFile = process.env[CC_OAUTH_ENV_FILE];
  const settingsArgs: string[] = [];
  if (oauthEnvFile !== undefined && input.env?.['ANTHROPIC_BASE_URL'] === undefined) {
    const token = readCcSubscriptionToken(oauthEnvFile);
    assertNoCcTokenCopies(env['HOME'], input.workspace);
    applyCcSubscriptionAuth(env, token);
    settingsArgs.push('--settings', ccSubscriptionSettings());
  }
  let piAgentLease: PiAgentLease | undefined;
  if (input.ccWriteBoundary) {
    piAgentLease = addCcWriteBoundarySettings(settingsArgs, ccWriteRoots({ workspace: input.workspace }),
      { home: env['HOME'], workspace: input.workspace, repoAccess: input.ccRepoAccess, piStateRoot: ccPiStateRoot() });
  }
  try {
    return { env, settingsArgs, mcpArgs: ccHostMcp(options.sessionWiMcp, { scheduled: true }).args, piAgentLease };
  } catch (error) {
    releasePiAgentLease(piAgentLease);
    throw error;
  }
}

/** Delete a run's Pi agent directory; a failure leaves it to the next start's sweep (sweepPiAgentDirs). */
function releasePiAgentLease(lease: PiAgentLease | undefined): void {
  try { lease?.release(); } catch (error) {
    console.warn({ ts: new Date().toISOString(), level: 'warn', module: 'runtime', msg: 'Pi agent directory not released', error: String(error) });
  }
}

/** Codex exec's human-mode header names the thread before the first model turn. */
const CODEX_SESSION_HEADER = /^session id: ([A-Za-z0-9-]{1,128})\r?$/m;
const CODEX_HEADER_LIMIT = 64 * 1024;

/**
 * A scheduled runtime gets the same kind of native session proof as a
 * conversation, keyed by the process the scheduler started, so the core does
 * not depend on reading a shell's environment. Failure to write it is logged
 * and leaves the run unchanged; the proof is removed when the process exits.
 */
function scheduledSessionProof(run: ScheduledNativeSession | undefined, runtime: 'cc' | 'codex', pid: number | undefined) {
  let written: { path: string; sessionId: string } | undefined;
  let attempted = false;
  const observe = (sessionId: string): void => {
    if (!run || pid === undefined || attempted) return;
    attempted = true;
    captureScheduledSession(runtime, sessionId, run, process.env);
    try {
      const path = observeScheduledSession(pid, sessionId, runtime, run);
      if (path) written = { path, sessionId };
    } catch (error) {
      console.warn({ ts: new Date().toISOString(), level: 'warn', module: 'runtime', job: run.jobId, runtime,
        msg: 'scheduler native session proof unavailable', error: String(error) });
    }
  };
  return {
    observe,
    observeCodexHeader(stderr: string): void {
      if (attempted || stderr.length > CODEX_HEADER_LIMIT) return;
      const match = CODEX_SESSION_HEADER.exec(stderr);
      if (match) observe(match[1]!);
    },
    forget(): void {
      if (!written) return;
      try { forgetNativeSession(written.path, written.sessionId); } catch (error) {
        console.warn({ ts: new Date().toISOString(), level: 'warn', module: 'runtime', job: run?.jobId, runtime,
          msg: 'scheduler native session proof cleanup failed', error: String(error) });
      }
      written = undefined;
    },
  };
}

function isContextWindowFailureText(text: string): boolean {
  return (
    text.includes('context window limit') ||
    text.includes('exceeds the context window') ||
    text.includes('context-window-limit') ||
    text.includes('model_context_window_exceeded') ||
    text.includes('context_length_exceeded') ||
    text.includes('maximum context length') ||
    text.includes('max_output_tokens')
  );
}

function isProviderRejectionFailureText(text: string): boolean {
  return (
    text.includes('system detected potentially unsafe or sensitive content') ||
    text.includes('potentially unsafe or sensitive content') ||
    (text.includes('api error: 400') && text.includes('sensitive content'))
  );
}

// Auth/quota exhaustion arrives as a *synthetic* assistant turn: CC emits one
// short text message with stop_reason `stop_sequence` and exits 0. That shape is
// indistinguishable from a real one-line answer by exit code alone, so the only
// available signal is the text itself. Observed verbatim in production on
// 2026-08-05, when the daily patrol recorded lastStatus=ok for a run that did no
// work at all (both strings below came from that incident's session jsonl).
function isAuthOrQuotaFailureText(text: string): boolean {
  return (
    text.includes('login expired') ||
    text.includes('please run /login') ||
    text.includes('hit your weekly limit') ||
    text.includes('hit your session limit') ||
    text.includes('hit your usage limit') ||
    text.includes('claude usage limit reached') ||
    text.includes('credit balance is too low') ||
    text.includes('oauth token has expired') ||
    text.includes('authentication_error')
  );
}

function isImmediateRuntimeFailureText(text: string): boolean {
  return isContextWindowFailureText(text)
    || isProviderRejectionFailureText(text)
    || isAuthOrQuotaFailureText(text);
}

async function runCommand(spec: CommandSpec, timeoutMs: number, beforeAttempt?: () => void): Promise<BatchRunResult> {
  let child: ReturnType<typeof spawnOwnedProcess>;
  try {
    beforeAttempt?.();
    child = spawnOwnedProcess(spec.command, spec.args, {
      cwd: spec.cwd,
      stdio: [spec.stdinText ? 'pipe' : 'ignore', 'pipe', 'pipe'],
      env: spec.env,
    });
  } catch (error) {
    releasePiAgentLease(spec.piAgentLease);
    throw error;
  }
  const startedAt = Date.now();
  // The run owns its Pi agent directory while it lives; one that did not start gives it back now.
  if (child.pid === undefined) releasePiAgentLease(spec.piAgentLease);
  else {
    try { spec.piAgentLease?.adopt(child.pid); } catch (error) {
      console.warn({ ts: new Date().toISOString(), level: 'warn', module: 'runtime', msg: 'Pi agent directory owner not recorded', error: String(error) });
    }
  }
  const proof = scheduledSessionProof(spec.nativeSession, spec.runtime === 'codex' ? 'codex' : 'cc', child.pid);
  if (spec.sessionId) proof.observe(spec.sessionId);

  return new Promise((resolve) => {
    let stdout = '';
    let stderr = '';
    let timedOut = false;
    let finished = false;
    let spawnErrorCode: string | undefined;
    let spawnErrorMessage: string | undefined;
    let failureAbort = false;
    const codexRun = spec.runtime === 'codex';
    let sigkillTimer: ReturnType<typeof setTimeout> | undefined;

    const requestChildTermination = (): void => {
      try { child.kill('SIGTERM'); } catch { /* ignore */ }
      if (!sigkillTimer) {
        sigkillTimer = setTimeout(() => {
          try { child.kill('SIGKILL'); } catch { /* ignore */ }
        }, SIGKILL_DELAY_MS);
      }
    };

    const abortForImmediateFailure = (text: string): void => {
      if (failureAbort || !isImmediateRuntimeFailureText(text.toLowerCase())) return;
      failureAbort = true;
      requestChildTermination();
    };

    const finalize = (exitCode: number): void => {
      if (finished) return;
      finished = true;
      clearTimeout(killTimer);
      if (sigkillTimer) clearTimeout(sigkillTimer);

      let finalStdout = stdout;
      if (spec.outputPath) {
        try {
          finalStdout = readFileSync(spec.outputPath, 'utf-8');
        } catch {
          finalStdout = stdout;
        }
      }

      if (spec.cleanupPath) {
        rmSync(spec.cleanupPath, { recursive: true, force: true });
      }
      releasePiAgentLease(spec.piAgentLease);
      proof.forget();

      let effectiveExitCode = exitCode;
      // Codex exits non-zero on its own provider failures and writes tool output
      // to stderr; only CC reports them as a zero-exit text turn.
      if (effectiveExitCode === 0 && !codexRun && isImmediateRuntimeFailureText(`${stderr}\n${finalStdout}`.toLowerCase())) {
        effectiveExitCode = 1;
      }

      resolve({
        runtime: spec.runtime,
        stdout: finalStdout,
        stderr,
        exitCode: effectiveExitCode,
        durationMs: Date.now() - startedAt,
        timedOut,
        spawnErrorCode,
        spawnErrorMessage,
      });
    };

    const killTimer = setTimeout(() => {
      if (child.exitCode === null && !finished) {
        timedOut = true;
        requestChildTermination();
      }
    }, timeoutMs);

    if (spec.streamJson && child.stdout) {
      const rl = createInterface({ input: child.stdout });
      rl.on('line', (line: string) => {
        const trimmed = line.trim();
        if (!trimmed) return;
        try {
          const msg = JSON.parse(trimmed) as {
            type?: string;
            subtype?: string;
            error_status?: number;
            error?: string;
            rate_limit_info?: { status?: string };
            content?: Array<{ type?: string; text?: string }>;
            message?: { content?: Array<{ type?: string; text?: string }> };
            result?: string;
          };

          if (spec.runtime === 'cc') observeCcRateLimitEvent(msg);
          const isRateLimitRetry = isRateLimitFailureMessage(msg);

          if (isRateLimitRetry) {
            // Log but do NOT kill — let CC handle 429 retries with its own exponential backoff.
            stderr += `API retry (${msg.error_status ?? '429'}), CC handling internally\n`;
            return;
          }

          if (msg.type === 'assistant') {
            const blocks = msg.message?.content ?? msg.content ?? [];
            const text = Array.isArray(blocks)
              ? blocks
                .filter((block) => block?.type === 'text' && typeof block.text === 'string')
                .map((block) => block.text)
                .join('')
              : '';
            if (text) stdout += text;
            if (text) abortForImmediateFailure(text);
            return;
          }

          if (msg.type === 'result' && typeof msg.result === 'string' && !stdout) {
            stdout = msg.result;
            abortForImmediateFailure(msg.result);
            return;
          }
        } catch {
          stdout += `${trimmed}\n`;
          abortForImmediateFailure(trimmed);
        }
      });
    } else {
      child.stdout?.on('data', (chunk: Buffer) => {
        stdout += chunk.toString();
      });
    }

    child.stderr?.on('data', (chunk: Buffer) => {
      const text = chunk.toString();
      stderr += text;
      if (spec.runtime === 'codex') proof.observeCodexHeader(stderr);
      if (!codexRun) abortForImmediateFailure(text);
    });

    if (spec.stdinText && child.stdin) {
      child.stdin.write(spec.stdinText);
      child.stdin.end();
    }

    child.on('exit', (code, signal) => {
      const exitCode = code ?? (signal ? 1 : 0);
      finalize(exitCode);
    });

    child.on('error', (err: NodeJS.ErrnoException) => {
      spawnErrorCode = err.code;
      spawnErrorMessage = err.message;
      finalize(1);
    });
  });
}

function createCcCommand(input: BatchRunInput, options: CcBatchOptions): CommandSpec {
  const launch = ccBatchLaunch(input, options);
  const sessionId = randomUUID();
  const ccModel = typeof input.ccModel === 'string' ? input.ccModel.trim() : '';
  const ccReasoningEffort = typeof input.ccReasoningEffort === 'string' ? input.ccReasoningEffort.trim() : '';
  return {
    runtime: 'cc',
    command: process.env['CLAUDE_BIN'] ?? 'claude',
    args: [
      '-p',
      '--input-format', 'stream-json',
      '--output-format', 'stream-json',
      '--session-id', sessionId,
      '--max-turns', String(input.maxTurns ?? 1000),
      '--dangerously-skip-permissions',
      '--verbose',
      ...(ccModel ? ['--model', ccModel] : []),
      ...(ccReasoningEffort ? ['--effort', ccReasoningEffort] : []),
      ...launch.settingsArgs,
      ...launch.mcpArgs,
    ],
    cwd: input.workspace,
    env: launch.env,
    ...(launch.piAgentLease ? { piAgentLease: launch.piAgentLease } : {}),
    sessionId,
    nativeSession: input.nativeSession,
    streamJson: true,
    stdinText: `${JSON.stringify({
      type: 'user',
      message: { role: 'user', content: input.prompt },
      parent_tool_use_id: null,
      session_id: null,
    })}\n`,
  };
}

function createCodexCommand(input: BatchRunInput): CommandSpec {
  const tempDir = mkdtempSync(join(tmpdir(), 'relay-codex-'));
  const outputPath = join(tempDir, 'last-message.txt');
  const permissionArgs = input.codexYolo
    ? ['--dangerously-bypass-approvals-and-sandbox']
    : ['-a', 'never', '-s', 'workspace-write'];
  return {
    runtime: 'codex',
    command: resolvePulseCodexBin(),
    args: [
      ...permissionArgs,
      ...buildPulseCodexModelArgs({
        model: input.codexModel ?? resolvePulseCodexModel(),
        reasoningEffort: input.codexReasoningEffort,
      }),
      // The conversation's Host MCP servers: declare_session_wi and pulse_work_item.
      ...scheduledCodexCoreArguments(),
      '-C', input.workspace,
      'exec',
      '--skip-git-repo-check',
      '--color', 'never',
      '--output-last-message', outputPath,
      '-',
    ],
    cwd: input.workspace,
    env: { ...buildCleanEnv(), ...input.env },
    nativeSession: input.nativeSession,
    stdinText: input.prompt,
    outputPath,
    cleanupPath: tempDir,
  };
}

function classifyGenericFailure(runtime: RuntimeId, result: BatchRunResult): RuntimeFailure | null {
  if (result.timedOut) {
    return { reason: 'timeout', message: `${runtime} timed out`, cooldownMs: DEFAULT_RUNTIME_COOLDOWN_MS };
  }

  if (result.spawnErrorCode) {
    if (result.spawnErrorCode === 'ENOENT' || result.spawnErrorCode === 'EACCES') {
      return { reason: 'binary-missing', message: `${runtime} binary unavailable`, cooldownMs: DEFAULT_RUNTIME_COOLDOWN_MS };
    }
    return { reason: 'startup-failure', message: `${runtime} spawn failed`, cooldownMs: DEFAULT_RUNTIME_COOLDOWN_MS };
  }

  const text = `${result.stderr}\n${result.stdout}`.toLowerCase();
  if (isContextWindowFailureText(text)) {
    return { reason: 'context-window', message: `${runtime} context window exhausted`, cooldownMs: DEFAULT_RUNTIME_COOLDOWN_MS };
  }

  if (isProviderRejectionFailureText(text)) {
    return { reason: 'provider-rejection', message: `${runtime} provider rejected content`, cooldownMs: DEFAULT_RUNTIME_COOLDOWN_MS };
  }

  if (
    text.includes('rate limit') ||
    text.includes('rate_limit') ||
    text.includes('429') ||
    text.includes('overloaded') ||
    // Codex: "Selected model is at capacity. Please try a different model."
    text.includes('at capacity')
  ) {
    return { reason: 'rate-limit', message: `${runtime} rate limited`, cooldownMs: DEFAULT_RUNTIME_COOLDOWN_MS };
  }

  if (
    text.includes('insufficient_quota') ||
    text.includes('quota exceeded') ||
    text.includes('limit exhausted') ||
    text.includes('weekly/monthly') ||
    text.includes('monthly limit') ||
    text.includes('weekly limit') ||
    // Claude subscription windows and Codex plan limits.
    text.includes('session limit') ||
    text.includes('usage limit') ||
    text.includes('"code":"1310"') ||
    text.includes('code=1310')
  ) {
    return { reason: 'quota', message: `${runtime} quota exhausted`, cooldownMs: DEFAULT_RUNTIME_COOLDOWN_MS };
  }

  if (
    text.includes('401') ||
    text.includes('unauthorized') ||
    text.includes('not logged in') ||
    text.includes('login expired') ||
    text.includes('please run /login') ||
    text.includes('authentication') ||
    text.includes('oauth')
  ) {
    return { reason: 'auth', message: `${runtime} auth unavailable`, cooldownMs: DEFAULT_RUNTIME_COOLDOWN_MS };
  }

  if (
    text.includes('unknown option') ||
    text.includes('unrecognized option') ||
    text.includes('unexpected argument') ||
    text.includes('invalid value')
  ) {
    return { reason: 'startup-failure', message: `${runtime} invocation incompatible`, cooldownMs: DEFAULT_RUNTIME_COOLDOWN_MS };
  }

  if (text.includes('incomplete turn')) {
    return { reason: 'startup-failure', message: `${runtime} ended before terminal assistant turn`, cooldownMs: DEFAULT_RUNTIME_COOLDOWN_MS };
  }

  return null;
}

/**
 * A run its owner's write boundary refuses. It is not a runtime failure: no
 * cooldown, no fallback to an unbounded runtime; the job reports the reason.
 */
function boundaryRefusal(runtime: RuntimeId, message: string): BatchRunResult {
  return { runtime, stdout: '', stderr: message, exitCode: 1, durationMs: 0, timedOut: false };
}

export function createCcBatchRuntimeAdapter(options: CcBatchOptions = {}): BatchRuntimeAdapter {
  return {
    id: 'cc',
    run(input) {
      let command: CommandSpec;
      try {
        command = createCcCommand(input, options);
      } catch (error) {
        if (input.ccWriteBoundary) return Promise.resolve(boundaryRefusal('cc', String((error as Error).message)));
        throw error;
      }
      return runCommand(command, input.timeoutMs, input.beforeAttempt);
    },
    classifyFailure(result) {
      return classifyGenericFailure('cc', result);
    },
  };
}

export function createCodexBatchRuntimeAdapter(): BatchRuntimeAdapter {
  return {
    id: 'codex',
    run(input) {
      return runCommand(createCodexCommand(input), input.timeoutMs, input.beforeAttempt);
    },
    classifyFailure(result) {
      return classifyGenericFailure('codex', result);
    },
  };
}

export function createBatchRuntimeOrchestrator(
  options: BatchRuntimeOrchestratorOptions,
): BatchRuntimeOrchestrator {
  const runtimePriority = resolvePulseRuntimePriority();
  const primary = options.primary ?? runtimePriority[0];
  const fallbacks = options.fallbacks ?? runtimePriority.slice(1);
  const cooldownMs = options.cooldownMs ?? DEFAULT_RUNTIME_COOLDOWN_MS;
  const adapterMap = new Map(options.adapters.map((adapter) => [adapter.id, adapter]));
  const unavailableUntil = new Map<RuntimeId, number>();

  function buildOrder(preferredRuntimes?: RuntimeId[]): RuntimeId[] {
    const raw = preferredRuntimes && preferredRuntimes.length > 0
      ? preferredRuntimes
      : [primary, ...fallbacks];
    const order = [...new Set(raw)].filter((runtimeId) => adapterMap.has(runtimeId));
    const now = Date.now();
    const available = order.filter((runtimeId) => (unavailableUntil.get(runtimeId) ?? 0) <= now);
    const coolingDown = order.filter((runtimeId) => (unavailableUntil.get(runtimeId) ?? 0) > now);
    return available.length > 0 ? [...available, ...coolingDown] : order;
  }

  return {
    async run(input, preferredRuntimes) {
      const order = buildOrder(preferredRuntimes);
      if (order.length === 0) {
        throw new Error('No batch runtimes configured');
      }

      let lastResult: BatchRunResult | null = null;
      const attempted: RuntimeId[] = [];
      const failedAttempts: Array<{ output: string; endedAt: string }> = [];
      // A non-successful result always carries the attempts that failed before it,
      // so facts they announced (a quota reset) outlive a later fallback failure.
      const finish = (result: BatchRunResult, earlier: Array<{ output: string; endedAt: string }>): BatchRunResult => ({
        ...result, attemptedRuntimes: [...attempted],
        ...(result.exitCode !== 0 && earlier.length ? { failedAttempts: earlier } : {}) });
      const recordFailure = (result: BatchRunResult): void => {
        failedAttempts.push({ output: `${result.stdout}\n${result.stderr}`, endedAt: new Date().toISOString() });
      };

      for (const runtimeId of order) {
        const adapter = adapterMap.get(runtimeId);
        if (!adapter) continue;

        input.beforeAttempt?.();
        let result = await adapter.run(input);
        attempted.push(runtimeId);

        if (result.exitCode === 0) {
          unavailableUntil.delete(runtimeId);
          return { ...result, attemptedRuntimes: attempted };
        }

        const failure = adapter.classifyFailure(result);
        if (!failure) {
          return finish(result, [...failedAttempts]);
        }

        if (failure.reason === 'rate-limit') {
          unavailableUntil.set(runtimeId, Date.now() + (failure.cooldownMs ?? cooldownMs));
          recordFailure(result);
          lastResult = result;
          continue;
        }

        // Non-rate-limit failure: mark unavailable and try next runtime
        unavailableUntil.set(runtimeId, Date.now() + (failure.cooldownMs ?? cooldownMs));
        recordFailure(result);
        lastResult = result;
      }

      if (!lastResult) {
        throw new Error('No runtime attempt was executed');
      }
      // The last failure is the result itself; the attempts before it ride along.
      return finish(lastResult, failedAttempts.slice(0, -1));
    },

    markHealthy(runtime) {
      unavailableUntil.delete(runtime);
    },

    markUnavailable(runtime, nextCooldownMs = cooldownMs) {
      unavailableUntil.set(runtime, Date.now() + nextCooldownMs);
    },
  };
}

export async function runCcBatch(input: BatchRunInput): Promise<BatchRunResult> {
  return createCcBatchRuntimeAdapter().run(input);
}

// ---------------------------------------------------------------------------
// CC Interactive (PTY bridge) batch adapter
// ---------------------------------------------------------------------------

const INTERACTIVE_SUBMIT_DELAY_MS = 500;
const INTERACTIVE_WARMUP_MS = 5_000;
const INTERACTIVE_POLL_INTERVAL_MS = 500;
const INTERACTIVE_IDLE_TIMEOUT_MS = 5 * 60 * 1000;
const INTERACTIVE_IDLE_TIMEOUT_ENV = 'RELAY_CC_INTERACTIVE_IDLE_TIMEOUT_MS';

const bridgeScriptPath = join(
  dirname(fileURLToPath(import.meta.url)),
  '..',
  'scripts',
  'claude_pty_bridge.py',
);

function getSessionFilePath(workspace: string, sessionId: string): string {
  let resolvedWorkspace = workspace;
  try {
    resolvedWorkspace = realpathSync(workspace);
  } catch { /* use original path */ }
  const cwdEncoded = resolvedWorkspace.replace(/[^A-Za-z0-9_-]/g, '-');
  return join(process.env['HOME'] ?? '', '.claude', 'projects', cwdEncoded, `${sessionId}.jsonl`);
}

function sleep(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

function getCcInteractiveIdleTimeoutMs(totalTimeoutMs: number): number {
  const configured = Number(process.env[INTERACTIVE_IDLE_TIMEOUT_ENV]);
  const idleTimeoutMs = Number.isFinite(configured) && configured > 0
    ? configured
    : INTERACTIVE_IDLE_TIMEOUT_MS;
  return Math.min(idleTimeoutMs, totalTimeoutMs);
}

function getFileProgressSignature(path: string): string {
  try {
    const stats = statSync(path);
    return `${stats.size}:${stats.mtimeMs}`;
  } catch {
    return '';
  }
}

export interface InteractiveRunOutcome {
  stdout: string;
  terminal: boolean;
  sawAssistant: boolean;
  lastStopReason?: string | null;
  pendingToolUseIds: string[];
}

export function extractInteractiveRunOutcome(jsonlPath: string, afterTimestamp: number): InteractiveRunOutcome {
  let content: string;
  try {
    content = readFileSync(jsonlPath, 'utf-8');
  } catch {
    return {
      stdout: '',
      terminal: false,
      sawAssistant: false,
      pendingToolUseIds: [],
    };
  }
  const parts: string[] = [];
  const toolUseIds = new Set<string>();
  const toolResultIds = new Set<string>();
  let terminal = false;
  let sawAssistant = false;
  let lastStopReason: string | null | undefined;

  for (const line of content.split('\n')) {
    const trimmed = line.trim();
    if (!trimmed) continue;
    try {
      const entry = JSON.parse(trimmed);
      // Only consider entries after our prompt was sent
      if (entry.timestamp && new Date(entry.timestamp).getTime() < afterTimestamp) continue;
      if (entry.type === 'assistant' && Array.isArray(entry.message?.content)) {
        sawAssistant = true;
        lastStopReason = entry.message?.stop_reason;
        let hasText = false;
        for (const block of entry.message.content) {
          if (block.type === 'text' && typeof block.text === 'string') {
            parts.push(block.text);
            hasText = hasText || block.text.length > 0;
          } else if (block.type === 'tool_use' && typeof block.id === 'string') {
            toolUseIds.add(block.id);
          }
        }
        if ((lastStopReason === 'end_turn' || lastStopReason === 'stop_sequence') && hasText) {
          terminal = true;
        }
      } else if (entry.type === 'user' && Array.isArray(entry.message?.content)) {
        for (const block of entry.message.content) {
          if (block.type === 'tool_result' && typeof block.tool_use_id === 'string') {
            toolResultIds.add(block.tool_use_id);
          }
        }
      } else if (entry.type === 'result' && typeof entry.result === 'string') {
        parts.push(entry.result);
        terminal = true;
      }
    } catch {
      // skip malformed lines
    }
  }

  const pendingToolUseIds = [...toolUseIds].filter((id) => !toolResultIds.has(id));
  return {
    stdout: parts.join('\n'),
    terminal,
    sawAssistant,
    lastStopReason,
    pendingToolUseIds,
  };
}

export function createCcInteractiveBatchRuntimeAdapter(options: CcBatchOptions = {}): BatchRuntimeAdapter {
  return {
    id: 'cc-interactive',
    async run(input: BatchRunInput): Promise<BatchRunResult> {
      const startedAt = Date.now();
      const sessionId = randomUUID();
      const jsonlPath = getSessionFilePath(input.workspace, sessionId);
      const claudeBin = process.env['CLAUDE_BIN'] ?? 'claude';
      // --bare skips every settings hook, so the file-tool half of the boundary would not run.
      if (input.ccWriteBoundary) {
        return boundaryRefusal('cc-interactive', 'CC write boundary refused: the interactive batch runs with --bare, which skips hooks');
      }
      const { env, settingsArgs, mcpArgs } = ccBatchLaunch(input, options);

      input.beforeAttempt?.();
      const child = cpSpawn('python3', [
        bridgeScriptPath,
        claudeBin,
        '--session-id', sessionId,
        '--bare',
        '--permission-mode', 'bypassPermissions',
        ...settingsArgs,
        // --bare skips MCP discovery; servers named here still start.
        ...mcpArgs,
      ], {
        cwd: input.workspace,
        stdio: ['pipe', 'pipe', 'pipe'],
        env,
      });
      // The PTY bridge is the launcher; CC is its direct child.
      const proof = scheduledSessionProof(input.nativeSession, 'cc', child.pid);
      proof.observe(sessionId);

      let stderr = '';
      let finished = false;
      let timedOut = false;
      let spawnErrorCode: string | undefined;
      let spawnErrorMessage: string | undefined;
      let submitTimer: ReturnType<typeof setTimeout> | undefined;
      let pollTimer: ReturnType<typeof setInterval> | undefined;
      let sigkillTimer: ReturnType<typeof setTimeout> | undefined;
      let terminalStopRequested = false;
      let terminationRequested = false;
      let lastProgressSignature = '';
      let lastProgressAt = Date.now();

      const requestChildTermination = (): void => {
        if (terminationRequested) return;
        terminationRequested = true;
        try { child.kill('SIGTERM'); } catch { /* ignore */ }
        if (!sigkillTimer) {
          sigkillTimer = setTimeout(() => {
            try { child.kill('SIGKILL'); } catch { /* ignore */ }
          }, SIGKILL_DELAY_MS);
        }
      };

      const finalize = (exitCode: number): BatchRunResult => {
        if (finished) return result;
        finished = true;
        clearTimeout(killTimer);
        if (submitTimer) clearTimeout(submitTimer);
        if (pollTimer) clearInterval(pollTimer);
        if (sigkillTimer) clearTimeout(sigkillTimer);
        proof.forget();
        const outcome = extractInteractiveRunOutcome(jsonlPath, promptSentAt);
        const stdout = outcome.stdout;
        // SIGTERM (143) is expected — we kill the process after getting a response.
        // Only terminal assistant turns count as success. A process can exit 0
        // after a partial tool_use turn; treating that as success drops work.
        let effectiveExitCode = ((exitCode === 143 || terminalStopRequested) && stdout && outcome.terminal && !timedOut) ? 0 : exitCode;
        if (!timedOut && effectiveExitCode === 0 && !outcome.terminal) {
          effectiveExitCode = 1;
          const detail = outcome.pendingToolUseIds.length > 0
            ? `pending tool_use ids: ${outcome.pendingToolUseIds.join(',')}`
            : `last stop_reason: ${outcome.lastStopReason ?? 'none'}`;
          stderr += `cc-interactive incomplete turn: ${detail}\n`;
        }
        if (!timedOut && effectiveExitCode === 0 && isImmediateRuntimeFailureText(`${stderr}\n${stdout}`.toLowerCase())) {
          effectiveExitCode = 1;
        }
        result = {
          runtime: 'cc-interactive',
          stdout,
          stderr,
          exitCode: effectiveExitCode,
          durationMs: Date.now() - startedAt,
          timedOut,
          spawnErrorCode,
          spawnErrorMessage,
        };
        return result;
      };

      let result: BatchRunResult = {
        runtime: 'cc-interactive',
        stdout: '',
        stderr,
        exitCode: 1,
        durationMs: 0,
        timedOut: false,
      };

      const killTimer = setTimeout(() => {
        if (!finished) {
          timedOut = true;
          requestChildTermination();
        }
      }, input.timeoutMs);

      child.stderr?.on('data', (chunk: Buffer) => {
        stderr += chunk.toString();
      });

      child.on('error', (err: NodeJS.ErrnoException) => {
        spawnErrorCode = err.code;
        spawnErrorMessage = err.message;
        finalize(1);
      });

      child.on('exit', (code, signal) => {
        const exitCode = code ?? (signal === 'SIGTERM' ? 143 : signal === 'SIGKILL' ? 137 : 1);
        finalize(exitCode);
      });

      // Wait for CC to warm up, then send prompt
      let promptSentAt = Date.now();

      // Warmup — let CC initialize the session
      await sleep(INTERACTIVE_WARMUP_MS);
      if (finished) return result;

      // Write prompt in chunks — PTY master drops large single writes
      const PTY_CHUNK_SIZE = 512;
      if (child.stdin && !child.killed) {
        promptSentAt = Date.now();
        const buf = Buffer.from(input.prompt, 'utf-8');
        for (let offset = 0; offset < buf.length; offset += PTY_CHUNK_SIZE) {
          const chunk = buf.subarray(offset, Math.min(offset + PTY_CHUNK_SIZE, buf.length));
          child.stdin.write(chunk);
          // Tiny pause between chunks to let PTY drain
          if (offset + PTY_CHUNK_SIZE < buf.length) {
            await sleep(10);
          }
        }

        // Delayed Enter (submit semantics)
        submitTimer = setTimeout(() => {
          if (!finished && child.stdin && !child.killed) {
            child.stdin.write('\r');
          }
        }, INTERACTIVE_SUBMIT_DELAY_MS);
      }

      lastProgressSignature = getFileProgressSignature(jsonlPath);
      lastProgressAt = Date.now();
      const idleTimeoutMs = getCcInteractiveIdleTimeoutMs(input.timeoutMs);

      pollTimer = setInterval(() => {
        if (finished) return;
        const progressSignature = getFileProgressSignature(jsonlPath);
        if (progressSignature && progressSignature !== lastProgressSignature) {
          lastProgressSignature = progressSignature;
          lastProgressAt = Date.now();
        }
        const outcome = extractInteractiveRunOutcome(jsonlPath, promptSentAt);
        if (outcome.terminal) {
          terminalStopRequested = true;
          requestChildTermination();
          return;
        }
        if (outcome.stdout && isImmediateRuntimeFailureText(outcome.stdout.toLowerCase())) {
          requestChildTermination();
          return;
        }
        if (Date.now() - lastProgressAt >= idleTimeoutMs) {
          timedOut = true;
          stderr += `cc-interactive idle timeout: no jsonl progress for ${idleTimeoutMs}ms\n`;
          requestChildTermination();
        }
      }, INTERACTIVE_POLL_INTERVAL_MS);

      // Wait for process to exit (CC --bare usually exits on its own; polling
      // only intervenes after terminal output, known provider failures, or idle stalls).
      return new Promise((resolve) => {
        child.on('exit', () => {
          resolve(result);
        });
      });
    },
    classifyFailure(result) {
      return classifyGenericFailure('cc-interactive', result);
    },
  };
}

/**
 * The runtimes a scheduled job tries in order. A job may fail over to every
 * other batch runtime after its own preference, so a model quota or outage on
 * one does not lose the run; `strict` keeps exactly the declared runtimes
 * (heterogeneous review pairs depend on that). A job without a preference
 * starts from the platform priority and still fails over.
 */
export function failoverRuntimes(preference: RuntimeId[] | undefined, strict: boolean): RuntimeId[] | undefined {
  if (strict) return preference;
  const batch: RuntimeId[] = [...new Set<RuntimeId>([...resolvePulseRuntimePriority(), 'codex', 'cc'])];
  const declared = preference ?? [];
  return [...declared, ...batch.filter((runtime) => !declared.includes(runtime))];
}
