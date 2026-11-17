/** Scheduler-started runtimes carry a native session proof for the process the scheduler started. */
import { describe, it, expect, vi, afterAll } from 'vitest';
import { chmodSync, existsSync, mkdtempSync, readdirSync, readFileSync, realpathSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';

const data = realpathSync(mkdtempSync(join(tmpdir(), 'relay-scheduler-proof-')));
vi.mock('../src/managed-paths.js', () => ({ managedRelayPath: () => data }));
vi.mock('../src/native-work-item-command.js', () => ({
  nodeWorkItemCore: () => ({ python: '/usr/bin/python3', entrypoint: '/x/harness.pyz', extension: '/x/runtime/pi.mjs' }),
}));
const { forgetNativeSession, observeScheduledSession } = await import('../src/native-core-runtime.js');
const { createCcBatchRuntimeAdapter, createCcInteractiveBatchRuntimeAdapter, createCodexBatchRuntimeAdapter } = await import('../src/runtime.js');
const { collaborationRef } = await import('../src/collaboration-capture.js');

const proofs = join(data, 'native-session-proofs');
const run = { jobId: 'hestia-dispatch', owner: 'hestia', runId: 'run-1' };

// The fake runtime reports the proof keyed by the process the scheduler started
// (itself, or its parent when a PTY bridge launched it) while it is running.
const reader = `
const fs = require('node:fs');
const path = require('node:path');
function proofFor(pid, waitMs) {
  const file = path.join(process.env.RELAY_TEST_PROOFS, pid + '.json');
  const deadline = Date.now() + waitMs;
  while (Date.now() < deadline) {
    if (fs.existsSync(file)) return { pid, proof: JSON.parse(fs.readFileSync(file, 'utf8')) };
    Atomics.wait(new Int32Array(new SharedArrayBuffer(4)), 0, 0, 25);
  }
  return { pid, proof: null };
}
const waitMs = Number(process.env.RELAY_TEST_PROOF_WAIT_MS || '5000');
`;

function fake(dir: string, name: string, body: string): string {
  const file = join(dir, name);
  writeFileSync(file, `#!/usr/bin/env node\n${reader}\n${body}`, 'utf-8');
  chmodSync(file, 0o755);
  return file;
}

async function withEnv<T>(values: Record<string, string>, action: () => Promise<T>): Promise<T> {
  const saved = new Map(Object.keys(values).map((key) => [key, process.env[key]]));
  Object.assign(process.env, values);
  try {
    return await action();
  } finally {
    for (const [key, value] of saved) {
      if (value === undefined) delete process.env[key];
      else process.env[key] = value;
    }
  }
}

describe('scheduler native session proof', () => {
  afterAll(() => rmSync(data, { recursive: true, force: true }));

  it('records the Codex thread from its stderr header and removes the proof at exit', async () => {
    const dir = mkdtempSync(join(tmpdir(), 'relay-scheduler-codex-'));
    const codex = fake(dir, 'codex-fake.cjs', `
const args = process.argv.slice(2);
process.stderr.write('OpenAI Codex v0\\n--------\\nworkdir: x\\nsession i');
setTimeout(() => {
  process.stderr.write('d: 01a154e4-266b-72d0-9bec-1801d94a91a3\\n--------\\n');
  const seen = proofFor(process.pid, waitMs);
  fs.writeFileSync(args[args.indexOf('--output-last-message') + 1], JSON.stringify(seen));
  process.exit(0);
}, 50);
`);
    try {
      const result = await withEnv({ CODEX_BIN: codex, RELAY_TEST_PROOFS: proofs }, () =>
        createCodexBatchRuntimeAdapter().run({ workspace: dir, prompt: 'p', timeoutMs: 20_000, nativeSession: run }));
      const seen = JSON.parse(result.stdout) as { pid: number; proof: Record<string, unknown> };
      expect(result.exitCode).toBe(0);
      expect(seen.proof).toMatchObject({
        schema: 'pulse.native-session-source/v1', runtime: 'codex', relay_pid: process.pid,
        session_id: '01a154e4-266b-72d0-9bec-1801d94a91a3', allocation_id: 'run-1',
        tenant: 'scheduler:hestia-dispatch', entry: 'scheduler', owner: 'hestia',
      });
      expect((seen.proof['process'] as { pid: number }).pid).toBe(seen.pid);
      expect(existsSync(join(proofs, `${seen.pid}.json`))).toBe(false);
    } finally {
      rmSync(dir, { recursive: true, force: true });
    }
  }, 30_000);

  it('records the job of a Codex run for ComputeCost without its session ID', async () => {
    const dir = mkdtempSync(join(tmpdir(), 'relay-scheduler-receipt-'));
    const receipts = join(dir, 'host-receipts');
    const thread = '01a154e4-266b-72d0-9bec-1801d94a91a4';
    const codex = fake(dir, 'codex-fake.cjs', `
const args = process.argv.slice(2);
process.stderr.write('session id: ${thread}\\n');
fs.writeFileSync(args[args.indexOf('--output-last-message') + 1], 'ok');
process.exit(0);
`);
    try {
      const scheduled = { ...run, jobName: 'Oikos\n30 分钟异常分流', startedAt: '2026-10-21T00:00:00.000Z', wi: 'wi_20261015_418' };
      await withEnv({ CODEX_BIN: codex, RELAY_TEST_PROOFS: proofs, RELAY_TEST_PROOF_WAIT_MS: '0',
        PULSE_COMPUTECOST_RECEIPT_DIR: receipts }, () =>
        createCodexBatchRuntimeAdapter().run({ workspace: dir, prompt: 'p', timeoutMs: 20_000, nativeSession: scheduled }));
      const [file] = readdirSync(receipts);
      const text = readFileSync(join(receipts, file!), 'utf8');
      const record = JSON.parse(text.trim()) as Record<string, unknown>;
      expect(record).toMatchObject({ schema: 'pulse_collaboration_host_event/v1', kind: 'scheduled_session',
        native_scope_ref: collaborationRef('native_session', ['codex', thread]),
        run_ref: collaborationRef('scheduler_run', ['hestia-dispatch', 'run-1']),
        started_at: '2026-10-21T00:00:00.000Z', job_id: 'hestia-dispatch', job_name: 'Oikos 30 分钟异常分流',
        wi: 'wi_20261015_418', sequence: 1, lost_events: 0 });
      expect(text).not.toContain(thread);
      expect(text).not.toContain('"p"');
    } finally {
      rmSync(dir, { recursive: true, force: true });
    }
  }, 30_000);

  it('records the CC launch session ID and removes the proof at exit', async () => {
    const dir = mkdtempSync(join(tmpdir(), 'relay-scheduler-cc-'));
    const claude = fake(dir, 'claude-fake.cjs', `
const args = process.argv.slice(2);
const seen = { ...proofFor(process.pid, waitMs), argv: args[args.indexOf('--session-id') + 1] };
process.stdout.write(JSON.stringify({ type: 'result', result: JSON.stringify(seen) }) + '\\n');
process.stdin.resume();
process.stdin.on('end', () => process.exit(0));
`);
    try {
      const result = await withEnv({ CLAUDE_BIN: claude, RELAY_TEST_PROOFS: proofs }, () =>
        createCcBatchRuntimeAdapter().run({ workspace: dir, prompt: 'p', timeoutMs: 20_000, nativeSession: run }));
      const seen = JSON.parse(result.stdout) as { pid: number; argv: string; proof: Record<string, unknown> };
      expect(result.exitCode).toBe(0);
      expect(seen.proof).toMatchObject({ runtime: 'cc', session_id: seen.argv, entry: 'scheduler', owner: 'hestia' });
      expect(existsSync(join(proofs, `${seen.pid}.json`))).toBe(false);
    } finally {
      rmSync(dir, { recursive: true, force: true });
    }
  }, 30_000);

  it('keys a cc-interactive proof by the PTY bridge that launched CC', async () => {
    const dir = realpathSync(mkdtempSync(join(tmpdir(), 'relay-scheduler-interactive-')));
    const claude = fake(dir, 'claude-fake.cjs', `
const args = process.argv.slice(2);
const sessionId = args[args.indexOf('--session-id') + 1];
const seen = { ...proofFor(process.ppid, waitMs), argv: sessionId };
const project = path.join(process.env.HOME, '.claude', 'projects', process.cwd().replace(/[^A-Za-z0-9_-]/g, '-'));
fs.mkdirSync(project, { recursive: true });
fs.writeFileSync(path.join(project, sessionId + '.jsonl'), JSON.stringify({ type: 'assistant',
  message: { stop_reason: 'end_turn', content: [{ type: 'text', text: JSON.stringify(seen) }] } }) + '\\n');
process.exit(0);
`);
    try {
      const result = await withEnv({ CLAUDE_BIN: claude, HOME: dir, RELAY_TEST_PROOFS: proofs }, () =>
        createCcInteractiveBatchRuntimeAdapter().run({ workspace: dir, prompt: 'p', timeoutMs: 20_000, nativeSession: run }));
      const seen = JSON.parse(result.stdout) as { pid: number; argv: string; proof: Record<string, unknown> };
      expect(seen.proof).toMatchObject({ runtime: 'cc', session_id: seen.argv, entry: 'scheduler' });
      expect((seen.proof['process'] as { pid: number }).pid).toBe(seen.pid);
      expect(existsSync(join(proofs, `${seen.pid}.json`))).toBe(false);
    } finally {
      rmSync(dir, { recursive: true, force: true });
    }
  }, 30_000);

  it('writes no proof for a batch run without a scheduler job', async () => {
    const dir = mkdtempSync(join(tmpdir(), 'relay-scheduler-none-'));
    const codex = fake(dir, 'codex-fake.cjs', `
const args = process.argv.slice(2);
process.stderr.write('session id: 01a154e4-266b-72d0-9bec-1801d94a91a3\\n');
fs.writeFileSync(args[args.indexOf('--output-last-message') + 1], JSON.stringify(proofFor(process.pid, waitMs)));
process.exit(0);
`);
    try {
      const result = await withEnv({ CODEX_BIN: codex, RELAY_TEST_PROOFS: proofs, RELAY_TEST_PROOF_WAIT_MS: '300' }, () =>
        createCodexBatchRuntimeAdapter().run({ workspace: dir, prompt: 'p', timeoutMs: 20_000 }));
      expect(JSON.parse(result.stdout)).toMatchObject({ proof: null });
    } finally {
      rmSync(dir, { recursive: true, force: true });
    }
  }, 30_000);

  it('cleanup keeps a newer proof for a reused PID', () => {
    const path = observeScheduledSession(process.pid, 'session-a', 'cc', run)!;
    forgetNativeSession(path, 'session-b');
    expect(JSON.parse(readFileSync(path, 'utf8'))).toMatchObject({ session_id: 'session-a' });
    forgetNativeSession(path, 'session-a');
    expect(existsSync(path)).toBe(false);
    forgetNativeSession(path, 'session-a');
  });
});
