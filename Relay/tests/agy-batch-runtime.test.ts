// Stream and log shapes follow the agy 1.5.13 fixtures recorded for wi_20261120_666 N01
// (a02 new conversation, e03 refused proxy), as in agy-conversation-runtime.test.ts.
import { EventEmitter } from 'node:events';
import { chmodSync, mkdirSync, mkdtempSync, readdirSync, readFileSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir, userInfo } from 'node:os';
import { join } from 'node:path';
import { PassThrough } from 'node:stream';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

const observed = vi.hoisted(() => ({ scheduled: [] as unknown[][] }));
vi.mock('../src/native-core-runtime.js', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../src/native-core-runtime.js')>()),
  observeScheduledSession: (...args: unknown[]) => { observed.scheduled.push(args); return undefined; },
}));

const { createAgyBatchRuntimeAdapter } = await import('../src/agy-batch-runtime.js');
const { createBatchRuntimeOrchestrator, failoverRuntimes } = await import('../src/runtime.js');
const { collaborationRef } = await import('../src/collaboration-capture.js');
import type { BatchRunInput, BatchRuntimeAdapter } from '../src/runtime.js';
import { AGY_SANDBOX_ENABLED, AGY_SANDBOX_REFUSAL } from '../src/agy-conversation-runtime.js';

interface MockChild extends EventEmitter {
  stdout: PassThrough; stderr: PassThrough; pid: number; kill: ReturnType<typeof vi.fn>;
}
const CONV = '8f0067ec-b62d-4ecf-bd95-04cae57649ac';
const WI = 'wi_20261120_666';
const USAGE = { input_tokens: 9772, output_tokens: 3, thinking_tokens: 0, cache_read_tokens: 0, total_tokens: 9775 };
const E04_LOG = 'W1120 18:42:09.131754 267 cache.go:157] Cache(loadCodeAssistResponse): Singleflight refresh failed: Post "https://daily-cloudcode-pa.googleapis.com/v1internal:loadCodeAssist": context deadline exceeded';
const E03_LOG = 'E1120 18:39:02.460259 73 errorreport.go:285] Post "https://daily-cloudcode-pa.googleapis.com/v1internal:loadCodeAssist": proxyconnect tcp: dial tcp 127.0.0.1:9: connect: connection refused';

const lines = (child: MockChild, events: unknown[]) => { for (const e of events) child.stdout.write(`${JSON.stringify(e)}\n`); };
const successStream = [
  { event: 'init', conversation_id: CONV, init: { model: 'gemini-4-neon-high' } },
  { event: 'step_update', step_update: { step_index: 1, state: 'DONE', step_type: 'agent_response', text_delta: 'Hello there!', usage: USAGE } },
  { event: 'result', result: { conversation_id: CONV, status: 'SUCCESS', response: 'Hello there!\n', usage: USAGE } },
];
const tick = () => new Promise((resolve) => setImmediate(resolve));
const wait = (ms: number) => new Promise((resolve) => setTimeout(resolve, ms));

let dir: string;
let agyBin: string;
let spawned: Array<{ command: string; args: string[]; env: NodeJS.ProcessEnv; child: MockChild }>;
let behave: (child: MockChild, args: string[]) => void;
const savedReceipts = process.env['PULSE_COMPUTECOST_RECEIPT_DIR'];

function spawn(command: string, args: string[], options: { env?: NodeJS.ProcessEnv }) {
  const child = new EventEmitter() as MockChild;
  child.stdout = new PassThrough();
  child.stderr = new PassThrough();
  child.pid = 7300 + spawned.length;
  child.kill = vi.fn(() => true);
  spawned.push({ command, args, env: options.env ?? {}, child });
  // agy writes this line at start when the restricted run's terminal sandbox is on.
  if (args.includes('--sandbox')) writeFileSync(args[args.indexOf('--log-file') + 1]!, `${AGY_SANDBOX_ENABLED}\n`);
  setImmediate(() => behave(child, args));
  return child as never;
}
const end = (child: MockChild, code: number | null, signal: string | null = null) => {
  child.stdout.end();
  setImmediate(() => child.emit('exit', code, signal));
};

function input(extra: Partial<BatchRunInput> = {}): BatchRunInput {
  return { workspace: '/ws', prompt: 'say hello', timeoutMs: 60_000, env: { RELAY_JOB_RUN_ID: 'run-1' }, ownerIsolation: 'host',
    nativeSession: { jobId: 'agy-smoke', owner: 'pulse', runId: 'run-1', jobName: 'agy smoke',
      startedAt: '2026-11-21T01:00:00.000Z', wi: WI }, ...extra };
}
function receipts(): Array<Record<string, any>> {
  const root = process.env['PULSE_COMPUTECOST_RECEIPT_DIR']!;
  return readdirSync(root).filter((name) => name.endsWith('.jsonl'))
    .flatMap((name) => readFileSync(join(root, name), 'utf8').trim().split('\n').map((row) => JSON.parse(row)));
}

beforeEach(() => {
  dir = mkdtempSync(join(tmpdir(), 'agy-batch-'));
  agyBin = join(dir, 'agy');
  writeFileSync(agyBin, '#!/bin/sh\n', { mode: 0o755 });
  // One spool per test: the Host capture is cached per directory.
  process.env['PULSE_COMPUTECOST_RECEIPT_DIR'] = join(dir, 'receipts');
  spawned = [];
  observed.scheduled.length = 0;
  behave = (child) => { lines(child, successStream); end(child, 0); };
});
afterEach(() => {
  if (savedReceipts === undefined) delete process.env['PULSE_COMPUTECOST_RECEIPT_DIR'];
  else process.env['PULSE_COMPUTECOST_RECEIPT_DIR'] = savedReceipts;
  rmSync(dir, { recursive: true, force: true });
});

describe('agy batch runtime', () => {
  it('runs a fresh conversation under the account HOME with the job WI binding, proof and agy_result.v1 usage', async () => {
    const adapter = createAgyBatchRuntimeAdapter({ agyBin, spawn });
    // The owner Agent's CC runs inside its write boundary, so this agy run is restricted too.
    const result = await adapter.run(input({ ccWriteBoundary: true }));
    expect(result).toMatchObject({ runtime: 'agy', exitCode: 0, stdout: 'Hello there!', timedOut: false });

    const call = spawned[0]!;
    expect(call.command).toBe(agyBin);
    expect(call.args).toContain('say hello');
    expect(call.args).not.toContain('--conversation');
    expect(call.args.join(' ')).not.toMatch(/proxy|https?:\/\//i);
    // A scheduled run gets the same permissions: only the kernel entry leaves the sandbox.
    const allow: string[] = JSON.parse(call.args[call.args.indexOf('--settings') + 1]!).permissions.allow;
    expect(allow).toContain('command(harness-native)');
    // The same boundary as a conversation with no grants: only the kernel entry leaves the sandbox.
    expect(allow.filter((rule) => rule.startsWith('command('))).toEqual(['command(harness-native)']);
    expect(allow).toContain('read_file(/)');  // Reads are open in a restricted CC run too.
    expect(allow).not.toContain('command(*)');
    expect(allow.filter((rule) => rule.includes('*'))).toEqual([]);
    expect(call.env['HOME']).toBe(userInfo().homedir);
    expect(call.env['RELAY_JOB_RUN_ID']).toBe('run-1');
    const binding = JSON.parse(call.env['PULSE_RUNTIME_USAGE_BINDING']!);
    expect(binding).toMatchObject({ work_item_id: WI, agent_id: 'pulse', runtime: 'agy', phase: 'scheduled' });

    expect(observed.scheduled).toEqual([[call.child.pid, CONV, 'agy', input().nativeSession]]);

    const scope = collaborationRef('native_session', ['agy', CONV]);
    const rows = receipts();
    expect(rows.find((r) => r.kind === 'scheduled_session')).toMatchObject({ native_scope_ref: scope, wi: WI, job_id: 'agy-smoke' });
    const assignment = collaborationRef('assignment', binding.binding_sha256);
    const executions = rows.filter((r) => r.kind === 'execution');
    expect(executions.map((r) => r.state)).toEqual(['accepted', 'completed']);
    expect(executions[0]).toMatchObject({ runtime: 'agy', wi: WI, executor: 'pulse', assignment_ref: assignment });
    const usage = rows.filter((r) => r.kind === 'usage');
    expect(usage).toHaveLength(1);
    expect(usage[0]).toMatchObject({ runtime: 'agy', mapping: 'agy_result.v1', granularity: 'turn_aggregate', final: true,
      covers: [], wi: WI, executor: 'pulse', native_scope_ref: scope, attempt_ref: executions[0]!.attempt_ref,
      model: 'gemini-4-neon-high', usage: { input_tokens: 9772, cache_read_tokens: 0, output_tokens: 3 } });
    expect(typeof usage[0]!.started_at).toBe('string');
  });

  it.each(['null', '[]', '1', '"text"', '{not json'])('skips the stream line %s and still settles the run', async (raw) => {
    behave = (child) => {
      child.stdout.write(`${raw}\n`);
      lines(child, [successStream[0], { event: 'init', init: 5 }, { event: 'step_update', step_update: null },
        { event: 'result', result: [] }, ...successStream.slice(1)]);
      end(child, 0);
    };
    const result = await createAgyBatchRuntimeAdapter({ agyBin, spawn }).run(input());
    expect(result).toMatchObject({ exitCode: 0, stdout: 'Hello there!' });
    expect(result.stderr).toContain('Invalid agy stream-json');
  });

  it('falls back to the run total when a finished step lacks usage, and leaves invalid usage unknown', async () => {
    const steps = (second: Record<string, unknown>) => [successStream[0],
      { event: 'step_update', step_update: { step_index: 1, state: 'DONE', step_type: 'agent_response', usage: { ...USAGE, input_tokens: 1 } } },
      { event: 'step_update', step_update: { step_index: 2, state: 'DONE', step_type: 'agent_response', ...second } },
      successStream[2]];
    behave = (child) => { lines(child, steps({})); end(child, 0); };
    await createAgyBatchRuntimeAdapter({ agyBin, spawn }).run(input());
    expect(receipts().find((r) => r.kind === 'usage')!.usage).toEqual({ input_tokens: 9772, cache_read_tokens: 0, output_tokens: 3 });

    process.env['PULSE_COMPUTECOST_RECEIPT_DIR'] = join(dir, 'receipts-invalid');
    behave = (child) => { lines(child, steps({ usage: { input_tokens: 'n/a' } })); end(child, 0); };
    await createAgyBatchRuntimeAdapter({ agyBin, spawn }).run(input());
    expect(receipts().find((r) => r.kind === 'usage')!.usage).toEqual({});
  });

  it('runs without a binding or WI when the job names no WI', async () => {
    const adapter = createAgyBatchRuntimeAdapter({ agyBin, spawn });
    const { wi: _wi, ...run } = input().nativeSession!;
    await adapter.run(input({ nativeSession: run }));
    expect(spawned[0]!.env['PULSE_RUNTIME_USAGE_BINDING']).toBeUndefined();
    const usage = receipts().find((r) => r.kind === 'usage')!;
    expect(usage).toMatchObject({ wi: null, executor: 'pulse', mapping: 'agy_result.v1' });
  });

  it('reports a refused proxy as an egress failure and never fails over to another runtime', async () => {
    behave = (child, args) => {
      writeFileSync(args[args.indexOf('--log-file') + 1]!, E03_LOG);
      lines(child, [{ event: 'result', result: { conversation_id: '', status: 'ERROR', response: '', error: 'invalid model selection' } }]);
      end(child, 1);
    };
    const codex: BatchRuntimeAdapter = { id: 'codex', run: vi.fn(), classifyFailure: () => null };
    const orchestrator = createBatchRuntimeOrchestrator({ adapters: [createAgyBatchRuntimeAdapter({ agyBin, spawn }), codex] });
    const result = await orchestrator.run(input(), failoverRuntimes(['agy'], false));
    expect(result.exitCode).toBe(1);
    expect(result.stderr).toMatch(/^agy egress unavailable: proxyconnect \(daily-cloudcode-pa\.googleapis\.com\); the request was not retried/);
    expect(result.failureReason).toContain('agy 出口不可用');
    expect(result.attemptedRuntimes).toEqual(['agy']);
    expect(codex.run).not.toHaveBeenCalled();
    expect(receipts().filter((r) => r.kind === 'execution').map((r) => r.state)).toEqual(['accepted', 'failed']);
  });

  it('refuses a job whose owner is isolated, or whose isolation cannot be verified', async () => {
    const codex: BatchRuntimeAdapter = { id: 'codex', run: vi.fn(), classifyFailure: () => null };
    const orchestrator = createBatchRuntimeOrchestrator({ adapters: [createAgyBatchRuntimeAdapter({ agyBin, spawn }), codex] });
    const isolated = await orchestrator.run(input({ ownerIsolation: 'isolated' }), failoverRuntimes(['agy'], false));
    expect(isolated).toMatchObject({ exitCode: 1, attemptedRuntimes: ['agy'] });
    expect(isolated.failureReason).toContain('独立 Linux 用户隔离');
    for (const ownerIsolation of ['unknown', undefined] as const) {
      const unverified = await orchestrator.run(input({ ownerIsolation }), failoverRuntimes(['agy'], false));
      expect(unverified.exitCode).toBe(1);
      expect(unverified.failureReason).toContain('无法核实');
    }
    expect(spawned).toEqual([]);
    expect(codex.run).not.toHaveBeenCalled();
  });

  it('records a run with denied tools as failed, keeping the reply and the denied actions', async () => {
    behave = (child) => {
      lines(child, [successStream[0], { event: 'result', result: { conversation_id: CONV, status: 'SUCCESS',
        response: 'I could not write the report.', usage: USAGE,
        denied_actions: [{ action: 'write_file', display_name: 'Edit' }, { action: 'run_command', display_name: 'Bash' }] } }]);
      end(child, 0);
    };
    const result = await createAgyBatchRuntimeAdapter({ agyBin, spawn }).run(input());
    expect(result).toMatchObject({ exitCode: 1, stdout: 'I could not write the report.', deniedActions: ['write_file', 'run_command'] });
    expect(result.failureReason).toContain('拒绝了工具权限：write_file, run_command');
    expect(result.failureReason).toContain('作业按失败记');
    expect(receipts().filter((r) => r.kind === 'execution').map((r) => r.state)).toEqual(['accepted', 'failed']);
  });

  it('fails a run whose denied actions carry no action name', async () => {
    behave = (child) => {
      lines(child, [successStream[0], { event: 'result', result: { conversation_id: CONV, status: 'SUCCESS',
        response: 'done?', usage: USAGE, denied_actions: [{ display_name: 'Edit' }, null] } }]);
      end(child, 0);
    };
    const result = await createAgyBatchRuntimeAdapter({ agyBin, spawn }).run(input());
    expect(result).toMatchObject({ exitCode: 1, deniedActions: ['(unnamed)', '(unnamed)'] });
    expect(receipts().filter((r) => r.kind === 'execution').map((r) => r.state)).toEqual(['accepted', 'failed']);
  });

  it('still settles the receipt when the turn log cannot be removed', async () => {
    const root = join(dir, 'tmp');
    mkdirSync(root);
    const savedTmp = process.env['TMPDIR'];
    process.env['TMPDIR'] = root;
    try {
      behave = (child) => {
        // The run's log directory now exists under root; make it undeletable (EACCES).
        chmodSync(root, 0o555);
        lines(child, successStream);
        end(child, 0);
      };
      const result = await createAgyBatchRuntimeAdapter({ agyBin, spawn }).run(input());
      expect(result.exitCode).toBe(0);
      expect(result.stderr).toContain('agy turn log not removed');
      expect(receipts().filter((r) => r.kind === 'execution').map((r) => r.state)).toEqual(['accepted', 'completed']);
    } finally {
      chmodSync(root, 0o755);
      if (savedTmp === undefined) delete process.env['TMPDIR']; else process.env['TMPDIR'] = savedTmp;
    }
  });

  it('keeps the egress reason when a hung egress ends in the outer timeout', async () => {
    behave = (child, args) => {
      lines(child, [successStream[0]]);
      child.kill.mockImplementation(() => {
        writeFileSync(args[args.indexOf('--log-file') + 1]!, E04_LOG);
        end(child, null, 'SIGTERM');
        return true;
      });
    };
    const result = await createAgyBatchRuntimeAdapter({ agyBin, spawn, killGraceMs: 15 }).run(input({ timeoutMs: 20 }));
    expect(result.timedOut).toBe(true);
    expect(result.failureReason).toContain('agy 出口不可用');
    expect(result.stderr).toMatch(/^agy egress unavailable: context deadline exceeded/);
  });

  it('stops a restricted run whose terminal sandbox agy did not enable', async () => {
    const original = spawn;
    const adapter = createAgyBatchRuntimeAdapter({ agyBin, spawn: ((command: string, args: string[], options: never) => {
      const child = original(command, args.filter((arg) => arg !== '--sandbox'), options) as unknown as MockChild;
      return child as never;
    }) as never });
    behave = (child) => { child.stdout.write(`${JSON.stringify({ event: 'init', conversation_id: CONV, init: {} })}\n`); };
    const run = adapter.run(input({ ccWriteBoundary: true }));
    await new Promise((resolve) => setTimeout(resolve, 20));
    const child = spawned[0]!.child;
    expect(child.kill).toHaveBeenCalledWith('SIGTERM');
    end(child, 1);
    const result = await run;
    expect(result.exitCode).toBe(1);
    expect(result.stderr).toContain(AGY_SANDBOX_REFUSAL);
    expect(result.failureReason).toContain('终端沙箱不可用');
  });

  it('runs without restriction when the owner Agent CC runs have no write boundary', async () => {
    const result = await createAgyBatchRuntimeAdapter({ agyBin, spawn: ((command: string, args: string[], options: never) => {
      const child = spawn(command, args, options) as unknown as MockChild;
      return child as never;
    }) as never }).run(input());
    const call = spawned[0]!;
    expect(call.args).toContain('--dangerously-skip-permissions');
    expect(call.args).not.toContain('--settings');
    expect(result.exitCode).toBe(0);
  });

  it('reports a missing launcher without starting anything', async () => {
    const result = await createAgyBatchRuntimeAdapter({ agyBin: join(dir, 'missing'), spawn }).run(input());
    expect(result).toMatchObject({ exitCode: 1, runtime: 'agy' });
    expect(result.stderr).toContain('agy launcher not found');
    expect(spawned).toEqual([]);
  });

  it('times out with SIGTERM, then SIGKILL, and settles once the child exits', async () => {
    behave = (child) => {
      lines(child, [successStream[0]]);
      child.kill.mockImplementation((signal: string) => { if (signal === 'SIGKILL') end(child, null, 'SIGKILL'); return true; });
    };
    const result = await createAgyBatchRuntimeAdapter({ agyBin, spawn, killGraceMs: 15 }).run(input({ timeoutMs: 20 }));
    expect(spawned[0]!.child.kill.mock.calls.map((c) => c[0])).toEqual(['SIGTERM', 'SIGKILL']);
    expect(result).toMatchObject({ timedOut: true, exitCode: 1 });
    expect(result.stderr).toContain('agy batch runtime timed out after 20ms');
  });

  it('settles a bounded time after SIGKILL when the child never reports its exit', async () => {
    behave = () => { /* ignores every signal */ };
    const warn = vi.spyOn(console, 'warn').mockImplementation(() => undefined);
    try {
      const started = Date.now();
      const result = await createAgyBatchRuntimeAdapter({ agyBin, spawn, killGraceMs: 15 }).run(input({ timeoutMs: 20 }));
      expect(Date.now() - started).toBeLessThan(1_000);
      expect(spawned[0]!.child.kill.mock.calls.map((c) => c[0])).toEqual(['SIGTERM', 'SIGKILL']);
      expect(result).toMatchObject({ timedOut: true, exitCode: 137 });
      spawned[0]!.child.emit('exit', null, 'SIGKILL');
      await tick();
      await wait(5);
    } finally {
      warn.mockRestore();
    }
  });
});
