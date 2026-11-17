import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { EventEmitter } from 'node:events';
import { PassThrough } from 'node:stream';
import { chmodSync, existsSync, mkdirSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

import { createPool } from '../src/pool.js';
import { createPoolConversationAdapter } from '../src/conversation-runtime.js';
import { ccRuntimeUsageEnvironment, createCcUsageMeasurement, type CcUsageCommandRunner } from '../src/cc-usage-measurement.js';
import {
  buildRuntimeUsageBinding,
  persistedRuntimeUsageBinding,
  runtimeAuthorityRef,
  runtimeProcurementSource,
  usageRuntimeForConversation,
} from '../src/runtime-usage-binding.js';
import { RuntimeUsageSealError } from '../src/runtime-usage-seal.js';

const { spawnMock } = vi.hoisted(() => ({ spawnMock: vi.fn() }));
vi.mock('node:child_process', async (original) => ({
  ...await original<typeof import('node:child_process')>(),
  spawn: spawnMock,
}));

const WI = 'wi_20261001_297';
const SURFACE = 'surface_v1_ownermac_relay';

function ccBinding(sessionId: string, overrides: Partial<Parameters<typeof buildRuntimeUsageBinding>[0]> = {}) {
  return buildRuntimeUsageBinding({
    workItemId: WI, agentId: 'pulse', phase: 'implementation', epoch: 1, runtime: 'cc',
    procurementSource: 'claude_subscription', authorityRef: runtimeAuthorityRef(sessionId),
    executionSurface: SURFACE, ...overrides,
  });
}

describe('CC runtime usage binding contract', () => {
  it('maps conversation runtimes and per-runtime procurement', () => {
    expect(usageRuntimeForConversation('cc-sdk')).toBe('cc_sdk');
    expect(usageRuntimeForConversation('cc')).toBe('cc');
    const env = { PULSE_PROCUREMENT_SOURCE: 'chatgpt_subscription' };
    expect(runtimeProcurementSource(env, 'cc')).toBe('claude_subscription');
    expect(runtimeProcurementSource(env, 'cc_sdk')).toBe('claude_subscription');
    expect(runtimeProcurementSource(env, 'pi')).toBe('chatgpt_subscription');
    expect(runtimeProcurementSource({ ...env, PULSE_PROCUREMENT_SOURCE_CC: 'openrouter_api' }, 'cc')).toBe('openrouter_api');
    expect(runtimeProcurementSource({ PULSE_PROCUREMENT_SOURCE_CC: 'bogus' }, 'cc')).toBeUndefined();
  });

  it('injects the same child environment as Pi and refuses a binding for another session or surface', () => {
    const binding = ccBinding('cc-session');
    const injected = ccRuntimeUsageEnvironment(binding, 'cc-session', SURFACE);
    expect(JSON.parse(injected.env.PULSE_RUNTIME_USAGE_BINDING!).binding_sha256).toBe(binding.binding_sha256);
    expect(injected.env).toMatchObject({
      PULSE_RUNTIME_AUTHORITY_REF: runtimeAuthorityRef('cc-session'),
      PULSE_RUNTIME_EXECUTION_SURFACE: SURFACE,
      HARNESS_SURFACE_ID: SURFACE,
      HARNESS_AGENT_ID: 'pulse',
      HARNESS_WORK_ITEM_ID: WI,
    });
    expect(injected.usageBindingSha256).toBe(binding.binding_sha256);
    expect(ccRuntimeUsageEnvironment(binding, 'repaired-session').rejected).toMatch(/native session/);
    expect(ccRuntimeUsageEnvironment(binding, 'cc-session', 'surface_v1_other').rejected).toMatch(/surface/);
    const pi = buildRuntimeUsageBinding({ workItemId: WI, agentId: 'pulse', phase: 'implementation', epoch: 1,
      runtime: 'pi', procurementSource: 'unknown' });
    // 回退或持久化带来的异族绑定只跳过注入，不在 spawn 时抛出。
    expect(ccRuntimeUsageEnvironment(pi, 'cc-session')).toEqual({ env: {}, rejected: expect.stringMatching(/another runtime/) });
    expect(ccRuntimeUsageEnvironment({ ...binding, binding_sha256: '0'.repeat(64) }, 'cc-session').env).toEqual({});
  });

  it('restores a persisted cc-sdk binding under its conversation runtime name', () => {
    const binding = buildRuntimeUsageBinding({ workItemId: WI, agentId: 'pulse', phase: 'implementation', epoch: 1,
      runtime: 'cc_sdk', procurementSource: 'claude_subscription', authorityRef: runtimeAuthorityRef('sdk-session') });
    const session = { runtime: 'cc-sdk', sessionId: 'sdk-session', activeWorkItemRuntimeBinding: {
      schema: 'pulse_active_work_item_runtime_binding/v1', workItemId: WI, sessionId: 'sdk-session',
      sessionRef: runtimeAuthorityRef('sdk-session'), runtime: 'cc-sdk' as const, usageBinding: binding } };
    expect(persistedRuntimeUsageBinding(session, 'cc-sdk', 'sdk-session')).toEqual(binding);
    expect(() => persistedRuntimeUsageBinding(session, 'cc', 'sdk-session')).toThrow();
  });
});

function child() {
  const value = Object.assign(new EventEmitter(), {
    stdin: new PassThrough(), stdout: new PassThrough(), stderr: new PassThrough(),
    pid: 1234567, killed: false,
    kill: vi.fn((signal?: string) => { value.killed = true; value.emit('exit', 0, signal); return true; }),
  });
  return value;
}

describe('CC pool spawn', () => {
  let home: string;
  let pool: ReturnType<typeof createPool>;
  beforeEach(() => {
    home = mkdtempSync(join(tmpdir(), 'cc-runtime-usage-'));
    vi.stubEnv('HOME', home);
    vi.stubEnv('PULSE_COMPUTECOST_RECEIPT_DIR', '');
    vi.stubEnv('PULSE_SCL_SURFACE_ID', SURFACE);
    vi.stubEnv('PULSE_RUNTIME_USAGE_BINDING', 'stale-parent-binding');
    spawnMock.mockReset();
    spawnMock.mockImplementation(() => child());
    pool = createPool({ cleanupIntervalMs: 999999, ccMode: 'print' });
  });
  afterEach(async () => {
    await pool.killAll();
    pool.dispose();
    vi.unstubAllEnvs();
    vi.restoreAllMocks();
    rmSync(home, { recursive: true, force: true });
  });

  async function spawn(sessionId: string, computeCostBinding?: ReturnType<typeof ccBinding>) {
    const pending = pool.spawn({ tenantId: 'fixture', sessionId, workspace: home, ccMode: 'print', computeCostBinding });
    spawnMock.mock.results.at(-1)!.value.stdout.write('{"type":"system","subtype":"init","content":"ready"}\n');
    const proc = await pending;
    return { proc, env: spawnMock.mock.calls.at(-1)![2].env as NodeJS.ProcessEnv };
  }

  it('injects the WI binding into a bound CC child and exposes its measurement context', async () => {
    const binding = ccBinding('bound-session');
    const { proc, env } = await spawn('bound-session', binding);
    expect(proc.usageBindingSha256).toBe(binding.binding_sha256);
    expect(JSON.parse(env.PULSE_RUNTIME_USAGE_BINDING!).binding_sha256).toBe(binding.binding_sha256);
    expect(env.HARNESS_WORK_ITEM_ID).toBe(WI);
    expect(env.PULSE_RUNTIME_AUTHORITY_REF).toBe(runtimeAuthorityRef('bound-session'));
    const context = pool.usageMeasurementContext!('fixture');
    expect(context?.binding?.binding_sha256).toBe(binding.binding_sha256);
    expect(context?.transcriptPath.endsWith('/bound-session.jsonl')).toBe(true);
  });

  it('never forwards a parent binding to an unbound CC child', async () => {
    const { proc, env } = await spawn('plain-session');
    expect(env.PULSE_RUNTIME_USAGE_BINDING).toBeUndefined();
    expect(proc.usageBindingSha256).toBeUndefined();
    expect(pool.usageMeasurementContext!('fixture')?.binding).toBeUndefined();
  });
});

describe('Host-side CC bounded measurement', () => {
  const transcriptPath = '/projects/-ws/cc-session.jsonl';
  const host = { python: '/py/bin/python', pythonPath: '/cc', sclExecutable: '/bin/scl',
    sclHarnessRoot: '/harness', sclKernelRoot: '/kernel' };

  function fixture(sclStatus: 'open' | 'sealed', error?: { code: string; retryable: boolean }) {
    const binding = ccBinding('cc-session');
    const calls: Array<{ file: string; args: string[]; env: NodeJS.ProcessEnv }> = [];
    const runner: CcUsageCommandRunner = async (file, args, options) => {
      calls.push({ file, args, env: options.env });
      if (file === host.python) {
        const role = args.at(-1) as 'baseline' | 'final';
        return { exitCode: 0, stderr: '', stdout: JSON.stringify({ schema: 'cc_atomic_capture/v1', idempotent_replay: false,
          snapshot: { schema: 'cc_session_usage_snapshot/v2', work_item_id: WI, epoch: 1,
            runtime_binding_sha256: binding.binding_sha256, measurement_role: role,
            snapshot_sha256: (role === 'baseline' ? 'b' : 'f').repeat(64) } }) };
      }
      if (error) return { exitCode: 1, stderr: '', stdout: JSON.stringify({ schema_version: 'scl_response/v1', error }) + '\n' };
      return { exitCode: 0, stderr: '', stdout: JSON.stringify({ schema_version: 'scl_response/v1', status: 'local_committed',
        result: { measurement: { schema_version: 'scl_measurement_epoch/v1', status: sclStatus, work_item_id: WI,
          execution_epoch: 1, agent_id: 'pulse', runtime: 'cc', execution_surface: SURFACE,
          baseline_sha256: 'b'.repeat(64), ...(sclStatus === 'sealed' ? { final_sha256: 'f'.repeat(64), seal_sha256: 'c'.repeat(64) } : {}) } } }) + '\n' };
    };
    return { binding, calls, measurement: createCcUsageMeasurement({ host, runner, env: { PATH: '/usr/bin' } }) };
  }

  it('captures a baseline and records it through the canonical SCL command', async () => {
    const { binding, calls, measurement } = fixture('open');
    const readback = await measurement.activateBaseline({ sessionId: 'cc-session', binding, transcriptPath });
    expect(readback).toMatchObject({ status: 'open', work_item_id: WI, epoch: 1, baseline_digest: 'b'.repeat(64),
      artifact_ref: `${WI}-${binding.binding_sha256}-baseline.json` });
    expect(calls[0]!.args).toEqual(['-m', 'pulse_compute_cost', 'cc-session-probe', '--lifecycle-state', 'open', '--measurement-role', 'baseline']);
    expect(calls[0]!.env).toMatchObject({ CC_SESSION_FILE: transcriptPath, CC_SESSION_ID: 'cc-session', PYTHONPATH: '/cc',
      PULSE_CC_USAGE_CAPTURE_PATH: `/projects/-ws/compute-cost/${WI}-${binding.binding_sha256}-baseline.json` });
    expect(calls[1]!.args).toEqual(['record-native-usage-local', WI, '--harness-root', '/harness', '--kernel-root', '/kernel',
      '--input', `/projects/-ws/compute-cost/${WI}-${binding.binding_sha256}-baseline.json`]);
    expect(calls[1]!.env.SCL_SESSION_REF).toBe(runtimeAuthorityRef('cc-session'));
  });

  it('seals against the immutable baseline and surfaces typed SCL rejection', async () => {
    const sealed = fixture('sealed');
    const readback = await sealed.measurement.seal({ sessionId: 'cc-session', binding: sealed.binding, transcriptPath });
    expect(readback).toMatchObject({ status: 'sealed', sealed_digest: 'f'.repeat(64) });
    expect(sealed.calls[0]!.env.PULSE_CC_USAGE_BASELINE_SNAPSHOT)
      .toBe(`/projects/-ws/compute-cost/${WI}-${sealed.binding.binding_sha256}-baseline.json`);
    const rejected = fixture('open', { code: 'measurement_fact_conflict', retryable: false });
    await expect(rejected.measurement.activateBaseline({ sessionId: 'cc-session', binding: rejected.binding, transcriptPath }))
      .rejects.toMatchObject({ code: 'measurement_fact_conflict' });
    const alreadySealed = fixture('sealed');
    await expect(alreadySealed.measurement.activateBaseline({ sessionId: 'cc-session', binding: alreadySealed.binding, transcriptPath }))
      .rejects.toMatchObject({ code: 'seal_conflict' });
  });

  it('is unavailable (never an authority gate) without Host resources or a v3 binding', async () => {
    const unconfigured = createCcUsageMeasurement({ runner: vi.fn() });
    await expect(unconfigured.activateBaseline({ sessionId: 'cc-session', binding: ccBinding('cc-session'), transcriptPath }))
      .rejects.toMatchObject({ code: 'measurement_authority_unavailable' });
    const { measurement } = fixture('open');
    const v1 = buildRuntimeUsageBinding({ workItemId: WI, agentId: 'pulse', phase: 'implementation', epoch: 1,
      runtime: 'cc', procurementSource: 'claude_subscription' });
    await expect(measurement.activateBaseline({ sessionId: 'cc-session', binding: v1, transcriptPath }))
      .rejects.toMatchObject({ code: 'measurement_authority_unavailable' });
  });

  it('the pool adapter measures only a settled CC process', async () => {
    const binding = ccBinding('cc-session');
    let state = 'busy';
    const measurement = { activateBaseline: vi.fn(async () => ({}) as never), seal: vi.fn(async () => ({}) as never) };
    const adapter = createPoolConversationAdapter({
      get: () => ({ state, sessionId: 'cc-session' }),
      usageMeasurementContext: () => ({ sessionId: 'cc-session', binding, transcriptPath }),
    } as never, { usageMeasurement: measurement });
    await expect(adapter.sealRuntimeUsage!('t')).rejects.toMatchObject({ code: 'not_settled' });
    state = 'idle';
    await adapter.sealRuntimeUsage!('t');
    expect(measurement.seal).toHaveBeenCalledWith({ sessionId: 'cc-session', binding, transcriptPath });
  });
});

const PYTHON = '/Users/owner/.local/share/coevolution/python-env/bin/python';
const COMPUTE_COST = resolve(dirname(fileURLToPath(import.meta.url)), '..', '..', 'ComputeCost');

describe.skipIf(!existsSync(PYTHON) || !existsSync(join(COMPUTE_COST, 'pulse_compute_cost', 'cc_usage.py')))(
  'Relay -> ComputeCost cc-session-probe', () => {
    it('produces an SCL-shaped bounded baseline from a real CC transcript', async () => {
      const root = mkdtempSync(join(tmpdir(), 'cc-probe-'));
      try {
        const project = join(root, 'projects', '-ws');
        mkdirSync(project, { recursive: true });
        const transcriptPath = join(project, 'cc-session.jsonl');
        writeFileSync(transcriptPath, JSON.stringify({ type: 'assistant', uuid: 'a0', sessionId: 'cc-session',
          timestamp: '2026-10-01T00:00:00Z', message: { id: 'm0', model: 'claude-opus-4-1',
            usage: { input_tokens: 1, output_tokens: 1 } } }) + '\n');
        // Task 子代理 transcript 同属该会话，进入逐文件清单。
        mkdirSync(join(project, 'cc-session', 'subagents'), { recursive: true });
        writeFileSync(join(project, 'cc-session', 'subagents', 'agent-a1.jsonl'), JSON.stringify({ type: 'assistant',
          uuid: 's0', sessionId: 'cc-session', isSidechain: true, timestamp: '2026-10-01T00:00:00Z',
          message: { id: 'm1', model: 'claude-opus-4-1', usage: { input_tokens: 1, output_tokens: 1 } } }) + '\n');
        const scl = join(root, 'scl');
        // 只回显 SCL 的成功形状；真实验证由 agent-harness 测试覆盖。
        writeFileSync(scl, [
          `#!${PYTHON}`,
          'import json, sys',
          'snapshot = json.load(open(sys.argv[sys.argv.index("--input") + 1]))',
          'print(json.dumps({"schema_version": "scl_response/v1", "status": "local_committed", "result": {"measurement": {',
          `  "schema_version": "scl_measurement_epoch/v1", "status": "open", "work_item_id": "${WI}", "execution_epoch": 1,`,
          `  "agent_id": "pulse", "runtime": "cc", "execution_surface": "${SURFACE}", "baseline_sha256": snapshot["snapshot_sha256"]}}}))`,
        ].join('\n') + '\n');
        chmodSync(scl, 0o755);
        const measurement = createCcUsageMeasurement({ host: { python: PYTHON, pythonPath: COMPUTE_COST,
          sclExecutable: scl, sclHarnessRoot: root, sclKernelRoot: root }, env: { PATH: process.env['PATH'] } });
        const binding = ccBinding('cc-session');
        const readback = await measurement.activateBaseline({ sessionId: 'cc-session', binding, transcriptPath });
        const artifact = JSON.parse(readFileSync(join(project, 'compute-cost', readback.artifact_ref), 'utf8'));
        expect(artifact).toMatchObject({ schema: 'cc_session_usage_snapshot/v2', runtime: 'cc', measurement_role: 'baseline',
          coverage: 'exact_zero_baseline', snapshot_sha256: readback.baseline_digest });
        expect(artifact.attribution_boundary.excluded_pre_baseline_usage_events).toBe(2);
        expect(artifact.attribution_boundary.baseline.sources).toHaveLength(2);
      } finally {
        rmSync(root, { recursive: true, force: true });
      }
    });
  });

// Pool behavior is isolated from OS process ownership, tested with real processes separately.
vi.mock('../src/owned-process.js', async () => ({
  spawnOwnedProcess: (await import('node:child_process')).spawn,
}));
