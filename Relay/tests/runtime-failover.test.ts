import { describe, expect, it, vi } from 'vitest';
import { chmodSync, mkdtempSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import {
  createBatchRuntimeOrchestrator,
  createCcBatchRuntimeAdapter,
  createCodexBatchRuntimeAdapter,
  failoverRuntimes,
  type BatchRunResult,
} from '../src/runtime.js';
import type { RuntimeId } from '../src/types.js';

function result(runtime: RuntimeId, overrides: Partial<BatchRunResult> = {}): BatchRunResult {
  return { runtime, stdout: '', stderr: '', exitCode: 0, durationMs: 10, timedOut: false, ...overrides };
}

describe('scheduled job runtime failover', () => {
  it('appends every other runtime after a declared preference unless strict', () => {
    expect(failoverRuntimes(['codex'], false)).toEqual(['codex', 'cc']);
    expect(failoverRuntimes(['cc'], false)).toEqual(['cc', 'codex']);
    expect(failoverRuntimes(['codex', 'cc'], false)).toEqual(['codex', 'cc']);
    expect(failoverRuntimes(['cc'], true)).toEqual(['cc']);
    expect(failoverRuntimes(undefined, false)).toEqual(['codex', 'cc']);
    expect(failoverRuntimes([], false)).toEqual(['codex', 'cc']);
    expect(failoverRuntimes(undefined, true)).toBeUndefined();
  });

  it('a job without a preference still fails over when codex quota is exhausted', async () => {
    const adapters = [createCodexBatchRuntimeAdapter(), createCcBatchRuntimeAdapter()].map((adapter) => ({
      ...adapter,
      run: vi.fn().mockResolvedValue(adapter.id === 'codex'
        ? result('codex', { exitCode: 1, stdout: 'The usage limit has been reached' })
        : result('cc', { stdout: 'done' })),
    }));
    const orchestrator = createBatchRuntimeOrchestrator({ primary: 'codex', fallbacks: [], adapters });
    const run = await orchestrator.run({ workspace: '/tmp', prompt: 'report', timeoutMs: 1000 },
      failoverRuntimes(undefined, false));
    expect(run.attemptedRuntimes).toEqual(['codex', 'cc']);
    expect(run.runtime).toBe('cc');
  });

  it('a zero-exit cc session-limit turn from the real adapter moves the run to codex', async () => {
    const tempDir = mkdtempSync(join(tmpdir(), 'relay-cc-failover-'));
    const fakeClaude = join(tempDir, 'claude-fake.cjs');
    const oldClaudeBin = process.env['CLAUDE_BIN'];
    writeFileSync(fakeClaude, `#!/usr/bin/env node
process.stdout.write(${JSON.stringify("You've hit your session limit · resets 5:20pm (Asia/Taipei)")});
process.exit(0);
`);
    chmodSync(fakeClaude, 0o755);
    try {
      process.env['CLAUDE_BIN'] = fakeClaude;
      const codexRun = vi.fn().mockResolvedValue(result('codex', { stdout: 'done' }));
      const orchestrator = createBatchRuntimeOrchestrator({ primary: 'cc', fallbacks: ['codex'],
        adapters: [createCcBatchRuntimeAdapter(), { ...createCodexBatchRuntimeAdapter(), run: codexRun }] });
      const run = await orchestrator.run({ workspace: tempDir, prompt: 'weekly', timeoutMs: 10_000 },
        failoverRuntimes(['cc'], false));
      expect(run.attemptedRuntimes).toEqual(['cc', 'codex']);
      expect(run.runtime).toBe('codex');
      expect(codexRun).toHaveBeenCalledOnce();
    } finally {
      if (oldClaudeBin === undefined) delete process.env['CLAUDE_BIN'];
      else process.env['CLAUDE_BIN'] = oldClaudeBin;
      rmSync(tempDir, { recursive: true, force: true });
    }
  });

  it.each([
    ['codex', 'The usage limit has been reached'],
    ['cc', "You've hit your session limit · resets 5:20pm (Asia/Taipei)"],
  ] as const)('a %s quota failure moves the run to the other runtime', async (first, message) => {
    const other: RuntimeId = first === 'codex' ? 'cc' : 'codex';
    const adapters = [createCodexBatchRuntimeAdapter(), createCcBatchRuntimeAdapter()].map((adapter) => ({
      ...adapter,
      run: vi.fn().mockResolvedValue(adapter.id === first
        ? result(first, { exitCode: 1, stdout: message })
        : result(other, { stdout: 'done' })),
    }));
    const orchestrator = createBatchRuntimeOrchestrator({ primary: first, fallbacks: [other], adapters });
    const run = await orchestrator.run({ workspace: '/tmp', prompt: 'report', timeoutMs: 1000 },
      failoverRuntimes([first], false));
    expect(run.runtime).toBe(other);
    expect(run.attemptedRuntimes).toEqual([first, other]);
  });

  it('an unclassified fallback failure still carries the earlier quota notice', async () => {
    const adapters = [createCcBatchRuntimeAdapter(), createCodexBatchRuntimeAdapter()].map((adapter) => ({
      ...adapter,
      run: vi.fn().mockResolvedValue(adapter.id === 'cc'
        ? result('cc', { exitCode: 1, stderr: "You've hit your session limit · resets 5:20pm (Asia/Taipei)" })
        : result('codex', { exitCode: 1, stderr: 'stream disconnected before completion' })),
    }));
    const orchestrator = createBatchRuntimeOrchestrator({ primary: 'cc', fallbacks: ['codex'], adapters });
    const run = await orchestrator.run({ workspace: '/tmp', prompt: 'weekly', timeoutMs: 1000 }, failoverRuntimes(['cc'], false));
    expect(run.attemptedRuntimes).toEqual(['cc', 'codex']);
    expect(run.failedAttempts).toHaveLength(1);
    expect(run.failedAttempts?.[0]?.output).toContain('resets 5:20pm');
    expect(Number.isFinite(Date.parse(run.failedAttempts?.[0]?.endedAt ?? ''))).toBe(true);
  });

  it('a strict job stays on its runtime when its quota is exhausted', async () => {
    const adapters = [createCodexBatchRuntimeAdapter(), createCcBatchRuntimeAdapter()].map((adapter) => ({
      ...adapter,
      run: vi.fn().mockResolvedValue(result(adapter.id, { exitCode: 1, stdout: 'The usage limit has been reached' })),
    }));
    const orchestrator = createBatchRuntimeOrchestrator({ primary: 'codex', fallbacks: ['cc'], adapters });
    const run = await orchestrator.run({ workspace: '/tmp', prompt: 'review', timeoutMs: 1000 },
      failoverRuntimes(['codex'], true));
    expect(run.attemptedRuntimes).toEqual(['codex']);
    expect(adapters[1]!.run).not.toHaveBeenCalled();
  });
});
