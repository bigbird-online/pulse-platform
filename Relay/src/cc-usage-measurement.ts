/**
 * Claude Code 的 WI 运行时用量：绑定注入 + Host 侧有界测量。
 *
 * Pi 在进程内扩展里做 baseline/seal；CC 没有扩展 API，但 Relay 掌握 CC 的
 * transcript 路径、原生 session 与 settled 边界。因此由 Host 在 settled 时调用
 * ComputeCost `cc-session-probe` 产出与 Pi 同形状的有界快照，再经
 * `scl record-native-usage-local` 交给 SCL 验证落账。测量失败只记为不可用，
 * 不改变业务派发（与 Pi baseline 不可用时的语义一致）。
 */
import { execFile } from 'node:child_process';
import { dirname, isAbsolute, join } from 'node:path';

import {
  RUNTIME_AUTHORITY_REF_ENV,
  RUNTIME_USAGE_BINDING_SCHEMA,
  runtimeAuthorityRef,
  runtimeUsageBindingEnvironment,
  type RuntimeUsageBinding,
} from './runtime-usage-binding.js';
import {
  RuntimeUsageSealError,
  type RuntimeUsageBaselineReadback,
  type RuntimeUsageSealErrorCode,
  type RuntimeUsageSealReadback,
} from './runtime-usage-seal.js';

/** Binding-specific variables a CC child may only receive from its own spawn config. */
export const CC_RUNTIME_USAGE_ENV_KEYS = [
  'PULSE_RUNTIME_USAGE_BINDING',
  'PULSE_RUNTIME_USAGE_BINDING_FILE',
  RUNTIME_AUTHORITY_REF_ENV,
  'PULSE_RUNTIME_EXECUTION_SURFACE',
] as const;

/**
 * Same child environment Pi receives for a WI-bound session. A binding that no
 * longer identifies the selected native session (for example after Relay's
 * jsonl chain repair started a new session) is not injected.
 */
export function ccRuntimeUsageEnvironment(
  binding: RuntimeUsageBinding | undefined,
  sessionId: string,
  hostSurface?: string,
  runtime: 'cc' | 'cc_sdk' = 'cc',
): { env: NodeJS.ProcessEnv; usageBindingSha256?: string; rejected?: string } {
  if (!binding) return { env: {} };
  let usage: NodeJS.ProcessEnv;
  try {
    // 持久化或回退带来的异族/损坏绑定只跳过注入，不阻断 spawn。
    usage = runtimeUsageBindingEnvironment(binding, runtime);
  } catch (error) {
    return { env: {}, rejected: error instanceof Error ? error.message : 'CC usage binding is invalid' };
  }
  if (binding.authority_ref && binding.authority_ref !== runtimeAuthorityRef(sessionId)) {
    return { env: {}, rejected: 'CC usage binding does not identify the selected native session' };
  }
  if (binding.execution_surface && hostSurface && hostSurface !== binding.execution_surface) {
    return { env: {}, rejected: 'CC usage binding does not match the current Host surface' };
  }
  const env: NodeJS.ProcessEnv = {
    ...usage,
    HARNESS_AGENT_ID: binding.agent_id,
    HARNESS_WORK_ITEM_ID: binding.work_item_id,
  };
  if (binding.authority_ref) env[RUNTIME_AUTHORITY_REF_ENV] = binding.authority_ref;
  if (binding.execution_surface) {
    env['PULSE_RUNTIME_EXECUTION_SURFACE'] = binding.execution_surface;
    env['HARNESS_SURFACE_ID'] = binding.execution_surface;
  }
  return { env, usageBindingSha256: binding.binding_sha256 };
}

/** Reviewed Host resources; the same ones a WI-bound Pi profile uses. */
export interface CcUsageHostConfig {
  python: string;
  pythonPath: string;
  sclExecutable: string;
  sclHarnessRoot: string;
  sclKernelRoot: string;
}

export interface CcUsageMeasurementContext {
  sessionId: string;
  binding?: RuntimeUsageBinding;
  /** Actual CC transcript (`<projects>/<cwd>/<session>.jsonl`), possibly not yet created. */
  transcriptPath: string;
}

export type CcUsageCommandRunner = (
  file: string,
  args: string[],
  options: { env: NodeJS.ProcessEnv; timeoutMs: number },
) => Promise<{ stdout: string; stderr: string; exitCode: number | null }>;

const defaultRunner: CcUsageCommandRunner = (file, args, options) => new Promise((resolve) => {
  execFile(file, args, { env: options.env, encoding: 'utf8', timeout: options.timeoutMs, maxBuffer: 4 * 1024 * 1024 },
    (error, stdout, stderr) => {
      const code = error && typeof (error as { code?: unknown }).code === 'number'
        ? (error as { code: number }).code : error ? null : 0;
      resolve({ stdout: String(stdout ?? ''), stderr: String(stderr ?? ''), exitCode: code });
    });
});

const SHA_RE = /^[0-9a-f]{64}$/;
const SCL_FAILURE_CODES = new Set<RuntimeUsageSealErrorCode>([
  'measurement_fact_conflict', 'measurement_epoch_conflict', 'measurement_authority_conflict',
  'measurement_authority_unavailable', 'measurement_epoch_required', 'source_authority_required',
  'local_persistence_unavailable', 'schema_incompatible', 'invalid_measurement',
]);

interface CcSnapshot {
  schema: 'cc_session_usage_snapshot/v2';
  work_item_id: string;
  epoch: number;
  runtime_binding_sha256: string;
  measurement_role: 'baseline' | 'final';
  snapshot_sha256: string;
}

export interface CcUsageMeasurement {
  activateBaseline(context: CcUsageMeasurementContext): Promise<RuntimeUsageBaselineReadback>;
  seal(context: CcUsageMeasurementContext): Promise<RuntimeUsageSealReadback>;
}

export function createCcUsageMeasurement(options: {
  host?: CcUsageHostConfig;
  runner?: CcUsageCommandRunner;
  env?: NodeJS.ProcessEnv;
  timeoutMs?: number;
} = {}): CcUsageMeasurement {
  const runner = options.runner ?? defaultRunner;
  const sourceEnv = options.env ?? process.env;
  // 每次 settled/续问都在派发路径上；probe 与 SCL 落账各自应在数秒内完成。
  const timeoutMs = options.timeoutMs ?? 10_000;

  function authority(context: CcUsageMeasurementContext): { host: CcUsageHostConfig; binding: RuntimeUsageBinding } {
    const host = options.host;
    if (!host || Object.values(host).some(path => typeof path !== 'string' || !isAbsolute(path))) {
      throw new RuntimeUsageSealError('measurement_authority_unavailable', 'CC usage measurement Host resources are not configured');
    }
    const binding = context.binding;
    if (!binding || binding.schema !== RUNTIME_USAGE_BINDING_SCHEMA || binding.runtime !== 'cc'
      || binding.authority_ref !== runtimeAuthorityRef(context.sessionId) || !isAbsolute(context.transcriptPath)) {
      // 不用 authority_mismatch：CC 测量不可用不应升级为业务门禁失败。
      throw new RuntimeUsageSealError('measurement_authority_unavailable', 'CC bounded usage requires a current v3 binding');
    }
    return { host, binding };
  }

  function artifactName(binding: RuntimeUsageBinding, state: 'baseline' | 'sealed'): string {
    return `${binding.work_item_id}-${binding.binding_sha256}-${state}.json`;
  }

  async function probe(context: CcUsageMeasurementContext, role: 'baseline' | 'final'): Promise<{ snapshot: CcSnapshot; replay: boolean; path: string }> {
    const { host, binding } = authority(context);
    const directory = join(dirname(context.transcriptPath), 'compute-cost');
    const path = join(directory, artifactName(binding, role === 'baseline' ? 'baseline' : 'sealed'));
    const result = await runner(host.python, [
      '-m', 'pulse_compute_cost', 'cc-session-probe',
      '--lifecycle-state', role === 'baseline' ? 'open' : 'sealed', '--measurement-role', role,
    ], {
      timeoutMs,
      env: {
        PATH: sourceEnv['PATH'] ?? '',
        PYTHONPATH: host.pythonPath,
        CC_SESSION_FILE: context.transcriptPath,
        CC_SESSION_ID: context.sessionId,
        PULSE_CC_USAGE_CAPTURE_PATH: path,
        ...runtimeUsageBindingEnvironment(binding, 'cc'),
        ...(role === 'final' ? { PULSE_CC_USAGE_BASELINE_SNAPSHOT: join(directory, artifactName(binding, 'baseline')) } : {}),
      },
    });
    let captured: { schema?: unknown; snapshot?: Partial<CcSnapshot>; idempotent_replay?: unknown };
    try {
      if (result.exitCode !== 0) throw new Error('probe failed');
      captured = JSON.parse(result.stdout);
    } catch {
      throw new RuntimeUsageSealError('seal_failed', `CC usage ${role} probe failed`, true);
    }
    const snapshot = captured.snapshot;
    if (captured.schema !== 'cc_atomic_capture/v1' || typeof captured.idempotent_replay !== 'boolean'
      || snapshot?.schema !== 'cc_session_usage_snapshot/v2' || snapshot.work_item_id !== binding.work_item_id
      || snapshot.epoch !== binding.epoch || snapshot.runtime_binding_sha256 !== binding.binding_sha256
      || snapshot.measurement_role !== role || !SHA_RE.test(String(snapshot.snapshot_sha256 ?? ''))) {
      throw new RuntimeUsageSealError('receipt_invalid', `CC usage ${role} snapshot does not match runtime authority`);
    }
    return { snapshot: snapshot as CcSnapshot, replay: captured.idempotent_replay, path };
  }

  async function record(context: CcUsageMeasurementContext, input: string): Promise<Record<string, unknown>> {
    const { host, binding } = authority(context);
    const result = await runner(host.sclExecutable, [
      'record-native-usage-local', binding.work_item_id,
      '--harness-root', host.sclHarnessRoot, '--kernel-root', host.sclKernelRoot, '--input', input,
    ], {
      timeoutMs,
      env: {
        PATH: sourceEnv['PATH'] ?? '',
        SCL_SESSION_REF: binding.authority_ref!,
        ...(sourceEnv['PULSE_HARNESS_HOME'] ? { PULSE_HARNESS_HOME: sourceEnv['PULSE_HARNESS_HOME'] } : {}),
        ...(sourceEnv['HARNESS_WORKTREE_STATE_ROOT'] ? { HARNESS_WORKTREE_STATE_ROOT: sourceEnv['HARNESS_WORKTREE_STATE_ROOT'] } : {}),
      },
    });
    let response: Record<string, unknown>;
    try {
      const lines = result.stdout.split(/\r?\n/).map(line => line.trim()).filter(Boolean);
      if (lines.length !== 1) throw new Error('unclean response');
      response = JSON.parse(lines[0]!);
    } catch {
      throw new RuntimeUsageSealError('measurement_transport_unavailable', 'CC canonical SCL measurement record was malformed', true);
    }
    const error = response['error'] as { code?: unknown; retryable?: unknown } | undefined;
    if (response['schema_version'] === 'scl_response/v1' && error && SCL_FAILURE_CODES.has(error.code as RuntimeUsageSealErrorCode)) {
      throw new RuntimeUsageSealError(error.code as RuntimeUsageSealErrorCode, 'CC canonical SCL measurement was rejected', error.retryable === true);
    }
    const measurement = (response['result'] as { measurement?: Record<string, unknown> } | undefined)?.measurement;
    if (response['schema_version'] !== 'scl_response/v1' || response['status'] !== 'local_committed'
      || measurement?.['schema_version'] !== 'scl_measurement_epoch/v1'
      || measurement['work_item_id'] !== binding.work_item_id || measurement['execution_epoch'] !== binding.epoch
      || measurement['agent_id'] !== binding.agent_id || measurement['runtime'] !== 'cc'
      || measurement['execution_surface'] !== binding.execution_surface) {
      throw new RuntimeUsageSealError('measurement_authority_conflict', 'CC canonical SCL measurement did not match runtime authority');
    }
    return measurement;
  }

  return {
    async activateBaseline(context) {
      const { binding } = authority(context);
      const captured = await probe(context, 'baseline');
      const measurement = await record(context, captured.path);
      if (measurement['baseline_sha256'] !== captured.snapshot.snapshot_sha256) {
        throw new RuntimeUsageSealError('measurement_fact_conflict', 'SCL retains a different CC baseline');
      }
      if (measurement['status'] === 'sealed') {
        // 该测量区间已经封存；不能在已封存区间上继续计量。
        throw new RuntimeUsageSealError('seal_conflict', 'CC measurement interval is already sealed');
      }
      return {
        schema: 'pulse_runtime_usage_baseline_readback/v1',
        status: captured.replay ? 'idempotent_replay' : 'open',
        work_item_id: binding.work_item_id,
        epoch: binding.epoch,
        artifact_ref: artifactName(binding, 'baseline'),
        baseline_digest: captured.snapshot.snapshot_sha256,
      };
    },
    async seal(context) {
      const { binding } = authority(context);
      const captured = await probe(context, 'final');
      const measurement = await record(context, captured.path);
      if (measurement['status'] !== 'sealed' || measurement['final_sha256'] !== captured.snapshot.snapshot_sha256
        || !SHA_RE.test(String(measurement['seal_sha256'] ?? ''))) {
        throw new RuntimeUsageSealError('seal_conflict', 'SCL did not seal this CC final snapshot');
      }
      return {
        schema: 'pulse_runtime_usage_seal_readback/v1',
        status: captured.replay ? 'idempotent_replay' : 'sealed',
        work_item_id: binding.work_item_id,
        epoch: binding.epoch,
        artifact_ref: artifactName(binding, 'sealed'),
        sealed_digest: captured.snapshot.snapshot_sha256,
      };
    },
  };
}
