/**
 * agy（Antigravity CLI）定时任务运行时：每次运行拉起一次 `agy -p … --output-format stream-json`，
 * 总是新对话（定时任务不续会话）。与会话运行时同一套命令、账户 HOME、出口识别与用量口径。
 *
 * - 隔离：agy 只以 Relay 主机账号运行；只有核实所属 Agent 以主机账号运行（ownerIsolation 为 host）才放行，
 *   带 linuxUser 或无法核实的一律拒绝，原因写进 failureReason。
 * - 权限：denied_actions 非空即按失败记，回复文字保留在输出里，拒绝的动作随结果带出；
 *   会话里有回复时照常送达并附说明，定时任务无人在场，拒绝即意味着所需操作没做完。
 * - 不切换：agy 的任何失败（出口、超时、权限、启动）都不归类为可切换失败，作业不改走其他运行时。
 * - 证明与归属：init 给出 conversation_id 后写 runtime=agy 的调度会话证明并登记 scheduled_session；
 *   作业配了 WI 时，按它签发 PULSE_RUNTIME_USAGE_BINDING 注入子进程，并以同一绑定把本次运行
 *   记成一轮 Host 执行与 agy_result.v1 用量（字段与 scope 与会话运行时一致）。
 * - 停止：超时先 SIGTERM，宽限期后 SIGKILL；再过一个宽限期仍未退出，按超时收尾。
 */
import { readFileSync, mkdtempSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { createInterface } from 'node:readline';
import type { ChildProcess } from 'node:child_process';
import {
  DEFAULT_AGY_MODEL, addCounts, agyAccountHome, agyEgressFailure, agyIsolationRefusal, agyObject, agyUsageCounts,
  AGY_SANDBOX_REFUSAL, agyAccess, agyRestricted, agySandboxEnabled, buildAgyCommand, buildAgyEnv, isAgyInstalled, parseAgyEvent, resolveAgyBin,
} from './agy-conversation-runtime.js';
import { scheduledTurnCapture } from './collaboration-capture.js';
import type { Counts } from './native-model-usage.js';
import { spawnOwnedProcess } from './owned-process.js';
import { buildRuntimeUsageBinding, runtimeProcurementSource, type RuntimeUsageBinding } from './runtime-usage-binding.js';
import { scheduledSessionProof, type BatchRunInput, type BatchRunResult, type BatchRuntimeAdapter } from './runtime.js';
import type { SpawnConfig, StreamJsonMessage } from './types.js';

const DEFAULT_KILL_GRACE_MS = 5_000;
/** Node may report exit before stdout is read to its end; the result is the last line. */
const EXIT_DRAIN_MS = 2_000;

export interface AgyBatchOptions {
  agyBin?: string;
  killGraceMs?: number;
  spawn?: typeof spawnOwnedProcess;
}

/** The job's WI binding for this run; a job without a WI, or an unusable owner name, has none. */
export function scheduledAgyBinding(input: BatchRunInput): RuntimeUsageBinding | undefined {
  const run = input.nativeSession;
  if (!run?.wi) return undefined;
  try {
    return buildRuntimeUsageBinding({ workItemId: run.wi, agentId: run.owner, phase: 'scheduled', epoch: 1,
      runtime: 'agy', procurementSource: runtimeProcurementSource(process.env, 'agy') ?? 'unknown' });
  } catch (error) {
    console.warn({ ts: new Date().toISOString(), level: 'warn', module: 'agy-batch', job: run.jobId,
      msg: 'agy job WI binding not issued', error: String(error) });
    return undefined;
  }
}

function refused(reason: string): BatchRunResult {
  return { runtime: 'agy', stdout: '', stderr: reason, failureReason: reason, exitCode: 1, durationMs: 0, timedOut: false };
}

export function createAgyBatchRuntimeAdapter(options: AgyBatchOptions = {}): BatchRuntimeAdapter {
  const killGraceMs = options.killGraceMs ?? DEFAULT_KILL_GRACE_MS;
  const spawn = options.spawn ?? spawnOwnedProcess;
  return {
    id: 'agy',
    run(input) {
      const refusal = input.ownerIsolation === 'host' ? undefined
        : input.ownerIsolation === 'isolated' ? agyIsolationRefusal('isolated')
          : '无法核实作业所属 Agent 是否以 Relay 主机账号运行；agy 只能以主机账号运行，因此拒绝。';
      if (refusal) return Promise.resolve(refused(refusal));
      if (!isAgyInstalled(options.agyBin)) {
        return Promise.resolve({ ...refused(`agy launcher not found: ${resolveAgyBin(options.agyBin)}`), failureReason: '本节点未安装 agy 启动脚本' });
      }
      return runAgy(input, { agyBin: options.agyBin, killGraceMs, spawn });
    },
    // Every agy failure is reported as it is; none hands the job to another runtime.
    classifyFailure() { return null; },
  };
}

async function runAgy(input: BatchRunInput, options: { agyBin?: string; killGraceMs: number; spawn: typeof spawnOwnedProcess }):
  Promise<BatchRunResult> {
  const run = input.nativeSession;
  const binding = scheduledAgyBinding(input);
  // The same permissions as the owner Agent's CC run (`agyRestricted`): its write boundary, or none.
  // Computed before the log directory: a malformed repository access refuses the run and leaves nothing behind.
  const restricted = agyRestricted(input);
  const access = restricted ? agyAccess({ workspace: input.workspace, repoAccess: input.ccRepoAccess }) : undefined;
  const logDir = mkdtempSync(join(tmpdir(), 'relay-agy-'));
  const logFile = join(logDir, 'turn.log');
  const command = buildAgyCommand({ agyBin: options.agyBin, workspace: input.workspace, prompt: input.prompt,
    model: DEFAULT_AGY_MODEL, logFile, yoloMode: !restricted, additionalWritableRoots: access?.writeRoots,
    deniedWrites: access?.deniedWrites, domains: access?.domains });
  // The account HOME wins over anything the job passes: agy's login and launcher live there.
  const env = { ...buildAgyEnv(binding), ...input.env, HOME: agyAccountHome() };
  const startedAt = Date.now();
  const capture = scheduledTurnCapture(process.env);
  const config = { computeCostBinding: binding, meteringAgentId: run?.owner, meteringWorkItemId: run?.wi } as SpawnConfig;
  const attempt = capture?.begin('agy', config, `scheduled:${run?.jobId ?? 'manual'}:${run?.runId ?? startedAt}`);

  let child: ChildProcess;
  try {
    input.beforeAttempt?.();
    child = options.spawn(command.command, command.args, { cwd: command.cwd, stdio: ['ignore', 'pipe', 'pipe'], env });
  } catch (error) {
    if (attempt) capture?.finish(attempt, 'failed');
    try { rmSync(logDir, { recursive: true, force: true }); } catch { /* cleanup never hides the start failure */ }
    throw error;
  }
  const proof = scheduledSessionProof(run, 'agy', child.pid);

  return new Promise((resolve) => {
    let finished = false;
    let exitCode: number | undefined;
    let stdoutClosed = false;
    let timedOut = false;
    let spawnError: NodeJS.ErrnoException | undefined;
    let stderr = '';
    let conversation: string | undefined;
    let observedModel: string | undefined;
    let result: Record<string, unknown> | undefined;
    let stepUsage: Counts | undefined;
    /** A finished step without usage: the steps' sum no longer covers the run; its result total still does. */
    let stepUsageIncomplete = false;
    /** A step usage that is not counts: the run's usage is unknown. */
    let stepUsageInvalid = false;
    /** A stream line Relay could not process: the run's outcome is not proven. */
    let streamFault = false;
    /** A restricted run whose terminal sandbox agy did not enable: stopped before any tool ran. */
    let sandboxRefused = false;
    let killTimer: NodeJS.Timeout | undefined;
    let forceTimer: NodeJS.Timeout | undefined;
    let drainTimer: NodeJS.Timeout | undefined;

    const timeoutTimer = setTimeout(() => {
      if (finished) return;
      timedOut = true;
      try { child.kill('SIGTERM'); } catch { /* ignore */ }
      killTimer = setTimeout(() => {
        if (finished) return;
        try { child.kill('SIGKILL'); } catch { /* ignore */ }
        forceTimer = setTimeout(() => {
          console.warn({ ts: new Date().toISOString(), level: 'warn', module: 'agy-batch', job: run?.jobId,
            msg: 'agy child reported no exit after SIGKILL; run settled as timed out', pid: child.pid });
          finish(exitCode ?? 137);
        }, options.killGraceMs);
      }, options.killGraceMs);
    }, input.timeoutMs);

    const rl = child.stdout ? createInterface({ input: child.stdout }) : undefined;
    if (!rl) stdoutClosed = true;
    // Any fault in reading one line fails this run only; it never escapes into the Relay process.
    rl?.on('line', (line) => {
      try { handleLine(line); } catch (error) {
        streamFault = true;
        stderr += `agy stream line not processed: ${error instanceof Error ? error.message : String(error)}\n`;
      }
    });
    function handleLine(line: string): void {
      const trimmed = line.trim();
      if (!trimmed || finished) return;
      const event = parseAgyEvent(trimmed);
      if (!event) {
        stderr += `Invalid agy stream-json: ${trimmed.slice(0, 200)}\n`;
        return;
      }
      if (event.event === 'init') {
        if (restricted && !agySandboxEnabled(logFile)) {
          sandboxRefused = true;
          try { child.kill('SIGTERM'); } catch { /* ignore */ }
          return;
        }
        const init = agyObject(event.init) ?? {};
        if (typeof init.model === 'string') observedModel = init.model;
        if (typeof event.conversation_id === 'string' && event.conversation_id && !conversation) {
          conversation = event.conversation_id;
          proof.observe(conversation);
        }
      } else if (event.event === 'step_update') {
        const step = agyObject(event.step_update) ?? {};
        if (step.step_type !== 'agent_response' || step.state !== 'DONE') return;
        if (step.usage === undefined) { stepUsageIncomplete = true; return; }
        const counts = agyUsageCounts(step.usage);
        if (counts) stepUsage = addCounts(stepUsage, counts);
        else stepUsageInvalid = true;
      } else if (event.event === 'result') {
        result = agyObject(event.result) ?? result;
      }
    }
    rl?.on('close', () => { stdoutClosed = true; if (exitCode !== undefined) finish(exitCode); });
    child.stderr?.on('data', (chunk: Buffer) => { stderr += chunk.toString(); });
    child.on('error', (error: NodeJS.ErrnoException) => { spawnError = error; finish(1); });
    child.on('exit', (code, signal) => {
      exitCode = code ?? (signal ? 1 : 0);
      if (stdoutClosed || timedOut) finish(exitCode);
      else drainTimer = setTimeout(() => finish(exitCode!), EXIT_DRAIN_MS);
    });

    /** Settles the run once; a fault while settling fails this run instead of escaping into the Relay process. */
    function finish(code: number): void {
      if (finished) return;
      try { settle(code); } catch (error) {
        // Every accepted attempt still gets its terminal receipt.
        if (attempt) {
          try { capture!.finish(attempt, 'failed'); }
          catch { /* the run result below still reports the failure */ }
        }
        resolve({ runtime: 'agy', stdout: '', exitCode: 1, durationMs: Date.now() - startedAt, timedOut,
          stderr: `agy run not settled: ${error instanceof Error ? error.message : String(error)}\n${stderr}`.trim() });
      }
    }

    function settle(code: number): void {
      finished = true;
      for (const timer of [timeoutTimer, killTimer, forceTimer, drainTimer]) if (timer) clearTimeout(timer);
      rl?.close();
      proof.forget();
      let log = '';
      try { log = readFileSync(logFile, 'utf8'); } catch { /* agy may not have written it */ }
      // Temporary log cleanup never decides the run or its receipt.
      try { rmSync(logDir, { recursive: true, force: true }); } catch (error) {
        stderr += `\nagy turn log not removed: ${error instanceof Error ? error.message : String(error)}`;
      }

      const status = typeof result?.status === 'string' ? result.status : undefined;
      const response = typeof result?.response === 'string' ? result.response.trim() : '';
      // Any denied action fails the run; names are only for display.
      const deniedRows = Array.isArray(result?.denied_actions) ? result!.denied_actions as unknown[] : [];
      const denied = deniedRows.map((row) => row && typeof row === 'object'
        && typeof (row as { action?: unknown }).action === 'string' ? (row as { action: string }).action : '(unnamed)');
      const failed = !!spawnError || code !== 0 || status !== 'SUCCESS' || timedOut || denied.length > 0 || streamFault
        || sandboxRefused;
      const egress = failed ? agyEgressFailure(log) : undefined;
      const failureReason = sandboxRefused ? 'agy 终端沙箱不可用，受限运行已停止，没有放宽权限'
        : egress ? 'agy 出口不可用（代理连接失败或无应答），本次未改走其他出口或运行时'
        : denied.length ? `agy 无头模式拒绝了工具权限：${denied.join(', ')}；所需操作可能未完成，作业按失败记`
          : undefined;
      const reason = sandboxRefused ? AGY_SANDBOX_REFUSAL
        : egress ? `agy egress unavailable: ${egress}; the request was not retried through another egress`
        : timedOut ? `agy batch runtime timed out after ${input.timeoutMs}ms`
          : spawnError ? `agy failed to start: ${spawnError.message}`
            : denied.length ? `agy permission denied: ${denied.join(', ')}`
              : failed ? (typeof result?.error === 'string' && result.error) || stderr.trim() || `agy exited with code ${code}`
                : undefined;

      // A fresh conversation: this run's steps; a step without usage falls back to the run's total;
      // a step usage that is not counts leaves the run's usage unknown.
      const total = agyUsageCounts(result?.usage);
      const usage = stepUsageInvalid ? undefined
        : stepUsage && !stepUsageIncomplete ? stepUsage : conversation ? total : undefined;
      const model = observedModel ?? DEFAULT_AGY_MODEL;
      if (attempt) {
        capture!.response(attempt, { type: 'result', runtime: 'agy', model, ...(failed ? { is_error: true } : {}),
          ...(usage ? { usage, native_model_usage: { schema: 'pulse_native_model_usage/v1', runtime: 'agy',
            models: [{ model, provider: null, usage }] } } : {}),
          ...(conversation ? { agy_conversation_id: conversation } : {}) } as StreamJsonMessage);
      }
      resolve({
        runtime: 'agy',
        stdout: response,
        stderr: reason ? `${reason}\n${stderr}`.trim() : stderr,
        exitCode: failed ? (code === 0 ? 1 : code) : 0,
        durationMs: Date.now() - startedAt,
        timedOut,
        ...(failureReason ? { failureReason } : {}),
        ...(denied.length ? { deniedActions: denied } : {}),
        ...(spawnError ? { spawnErrorCode: spawnError.code, spawnErrorMessage: spawnError.message } : {}),
      });
    }
  });
}
