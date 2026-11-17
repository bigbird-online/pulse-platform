import { describe, expect, it } from 'vitest';
import { JobTerminalRegistry } from '../src/job-terminal-registry.js';

const SCAN_REF_A = `scan_v1_${'a'.repeat(64)}`;
const SCAN_REF_B = `scan_v1_${'b'.repeat(64)}`;

describe('JobTerminalRegistry', () => {
  it('deduplicates concurrent and retried terminal delivery for the same run', () => {
    const registry = new JobTerminalRegistry();
    const run = registry.startRun('amanda-review', 'amanda');

    expect(registry.reserve(run.runId, 'amanda').deliver).toBe(true);
    const concurrent = registry.reserve(run.runId, 'amanda');
    expect(concurrent).toMatchObject({ deliver: false, deduplicated: true });
    expect(concurrent.run?.status).toBe('sending');

    registry.complete(run.runId, [{ channel: 'feishu', status: 'sent', externalMessageId: 'om_one' }]);
    const retry = registry.reserve(run.runId, 'amanda');
    expect(retry).toMatchObject({ deliver: false, deduplicated: true });
    expect(retry.run?.receipts[0]?.externalMessageId).toBe('om_one');
  });

  it('produces privacy-safe scheduler lineage from explicit run facts', () => {
    const registry = new JobTerminalRegistry();
    const run = registry.startRun('amanda-review', 'amanda');

    expect(run.lineage).toMatchObject({
      root_agent: 'amanda',
      execution_mode: 'automatic',
      session_role: 'main',
      parent_run_ref: 'unknown',
      scan_ref: 'unknown',
    });
    expect(run.lineage.root_run_ref).toMatch(/^lineage_v1_[0-9a-f]{64}$/);
    expect(run.lineage.cron_job_ref).toMatch(/^cron_v1_[0-9a-f]{64}$/);
    expect(JSON.stringify(run.lineage)).not.toContain(run.runId);
    expect(JSON.stringify(run.lineage)).not.toContain(run.jobId);
  });

  it('attaches an explicit Harness producer scan_ref idempotently', () => {
    const registry = new JobTerminalRegistry();
    const run = registry.startRun('pulse-dispatcher', 'pulse');

    const enriched = registry.attachHarnessScanRef(run.runId, 'pulse', SCAN_REF_A, 1234);
    const replay = registry.attachHarnessScanRef(run.runId, 'pulse', SCAN_REF_A, 5678);

    expect(enriched.lineage.scan_ref).toBe(SCAN_REF_A);
    expect(replay.lineage.scan_ref).toBe(SCAN_REF_A);
    expect(registry.get(run.runId)?.lineage.scan_ref).toBe(SCAN_REF_A);
  });

  it('fails closed on invalid, conflicting, or cross-owner scan lineage', () => {
    const registry = new JobTerminalRegistry();
    const run = registry.startRun('pulse-dispatcher', 'pulse');

    expect(() => registry.attachHarnessScanRef(run.runId, 'pulse', 'sc_20260818_raw')).toThrow(
      /Harness producer reference/,
    );
    expect(() => registry.attachHarnessScanRef(run.runId, 'amanda', SCAN_REF_A)).toThrow(
      /owner mismatch/,
    );

    registry.attachHarnessScanRef(run.runId, 'pulse', SCAN_REF_A);
    expect(() => registry.attachHarnessScanRef(run.runId, 'pulse', SCAN_REF_B)).toThrow(/conflicting/);
    expect(registry.get(run.runId)?.lineage.scan_ref).toBe(SCAN_REF_A);
  });

  it('allows scheduler fallback after an internal terminal delivery failed', () => {
    const registry = new JobTerminalRegistry();
    const run = registry.startRun('amanda-review', 'amanda');

    expect(registry.reserve(run.runId, 'amanda').deliver).toBe(true);
    registry.complete(run.runId, [{ channel: 'feishu', status: 'failed', error: 'network' }]);

    expect(registry.reserve(run.runId, 'amanda')).toMatchObject({
      deliver: true,
      deduplicated: false,
      run: { status: 'sending' },
    });
  });

  it('keeps distinct and concurrent runs isolated by run id', () => {
    const registry = new JobTerminalRegistry();
    const first = registry.startRun('job-a', 'amanda');
    const second = registry.startRun('job-b', 'amanda');

    expect(registry.reserve(undefined, 'amanda')).toMatchObject({
      deliver: false,
      deduplicated: false,
      reason: 'ambiguous_owner',
    });
    expect(registry.reserve(first.runId, 'amanda').deliver).toBe(true);
    expect(registry.reserve(second.runId, 'amanda').deliver).toBe(true);
    expect(registry.reserve(first.runId, 'pulse').reason).toBe('owner_mismatch');
  });

  it('retains sent terminal state after the run finishes so late retries stay deduplicated', () => {
    const registry = new JobTerminalRegistry();
    const run = registry.startRun('amanda-review', 'amanda');
    registry.reserve(run.runId, 'amanda');
    registry.complete(run.runId, [{ channel: 'feishu', status: 'sent' }]);
    registry.finishRun(run.runId);

    expect(registry.reserve(run.runId, 'amanda')).toMatchObject({
      deliver: false,
      deduplicated: true,
      run: { status: 'sent' },
    });
  });
});
