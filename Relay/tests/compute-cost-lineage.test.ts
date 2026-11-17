import { describe, expect, it, vi } from 'vitest';
import { mkdtempSync, readFileSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import {
  COMPUTE_COST_LINEAGE_DIR_ENV,
  attachHarnessScanRef,
  buildRelaySchedulerLineage,
  emitRelaySchedulerLineage,
  relayHarnessScanLineageRecord,
  relaySchedulerLineageRecord,
} from '../src/compute-cost-lineage.js';

const SCAN_REF_A = `scan_v1_${'a'.repeat(64)}`;
const SCAN_REF_B = `scan_v1_${'b'.repeat(64)}`;

describe('ComputeCost Relay lineage producer', () => {
  it('hashes scheduler run and cron job keys into separate private namespaces', () => {
    const lineage = buildRelaySchedulerLineage({
      runId: 'same-raw-key',
      jobId: 'same-raw-key',
      owner: 'pulse',
    });

    expect(lineage).toMatchObject({
      root_agent: 'pulse',
      execution_mode: 'automatic',
      session_role: 'main',
      parent_run_ref: 'unknown',
      scan_ref: 'unknown',
    });
    expect(lineage.root_run_ref).toMatch(/^lineage_v1_[0-9a-f]{64}$/);
    expect(lineage.cron_job_ref).toMatch(/^cron_v1_[0-9a-f]{64}$/);
    expect(lineage.root_run_ref.slice('lineage_v1_'.length)).not.toBe(
      lineage.cron_job_ref.slice('cron_v1_'.length),
    );
  });

  it('is deterministic for the same explicit producer facts and changes across runs', () => {
    const first = buildRelaySchedulerLineage({ runId: 'run-a', jobId: 'weekly-review', owner: 'duke' });
    const replay = buildRelaySchedulerLineage({ runId: 'run-a', jobId: 'weekly-review', owner: 'duke' });
    const nextRun = buildRelaySchedulerLineage({ runId: 'run-b', jobId: 'weekly-review', owner: 'duke' });

    expect(replay).toEqual(first);
    expect(nextRun.root_run_ref).not.toBe(first.root_run_ref);
    expect(nextRun.cron_job_ref).toBe(first.cron_job_ref);
  });

  it('accepts only explicit Harness producer references and rejects conflicts', () => {
    const base = buildRelaySchedulerLineage({ runId: 'run-a', jobId: 'dispatcher', owner: 'pulse' });
    const enriched = attachHarnessScanRef(base, SCAN_REF_A);

    expect(base.scan_ref).toBe('unknown');
    expect(enriched.scan_ref).toBe(SCAN_REF_A);
    expect(attachHarnessScanRef(enriched, SCAN_REF_A)).toEqual(enriched);
    expect(() => attachHarnessScanRef(base, 'sc_20260818_raw')).toThrow(/Harness producer reference/);
    expect(() => attachHarnessScanRef(enriched, SCAN_REF_B)).toThrow(/conflicting/);
  });

  it('writes only privacy-safe scheduler lineage when an absolute candidate evidence dir is enabled', () => {
    const dir = mkdtempSync(join(tmpdir(), 'relay-lineage-'));
    try {
      const rawRunId = 'raw-run-should-never-be-written';
      const rawJobId = 'raw-job-should-never-be-written';
      const record = relaySchedulerLineageRecord({
        runId: rawRunId,
        jobId: rawJobId,
        owner: 'amanda',
        startedAt: Date.parse('2026-08-18T01:02:03Z'),
      });

      expect(emitRelaySchedulerLineage(record, { [COMPUTE_COST_LINEAGE_DIR_ENV]: dir })).toBe(true);
      const raw = readFileSync(join(dir, '2026-08-18.jsonl'), 'utf8');
      const parsed = JSON.parse(raw.trim()) as Record<string, unknown>;

      expect(raw).not.toContain(rawRunId);
      expect(raw).not.toContain(rawJobId);
      expect(raw).not.toContain('/Users/');
      expect(parsed).toMatchObject({
        schema: 'relay_lineage_evidence.v1',
        occurred_at: '2026-08-18T01:02:03.000Z',
        producer: 'relay_scheduler',
        root_agent: 'amanda',
        execution_mode: 'automatic',
        session_role: 'main',
        parent_run_ref: 'unknown',
        scan_ref: 'unknown',
      });
      expect(parsed['root_run_ref']).toMatch(/^lineage_v1_[0-9a-f]{64}$/);
      expect(parsed['cron_job_ref']).toMatch(/^cron_v1_[0-9a-f]{64}$/);
    } finally {
      rmSync(dir, { recursive: true, force: true });
    }
  });

  it('writes the full scheduler lineage again when Harness supplies scan_ref', () => {
    const dir = mkdtempSync(join(tmpdir(), 'relay-scan-lineage-'));
    try {
      const lineage = attachHarnessScanRef(
        buildRelaySchedulerLineage({ runId: 'run-a', jobId: 'dispatcher', owner: 'pulse' }),
        SCAN_REF_A,
      );
      const record = relayHarnessScanLineageRecord({
        lineage,
        observedAt: Date.parse('2026-08-18T02:03:04Z'),
      });

      expect(emitRelaySchedulerLineage(record, { [COMPUTE_COST_LINEAGE_DIR_ENV]: dir })).toBe(true);
      const raw = readFileSync(join(dir, '2026-08-18.jsonl'), 'utf8');
      const parsed = JSON.parse(raw.trim()) as Record<string, unknown>;

      expect(parsed).toMatchObject({
        schema: 'relay_lineage_evidence.v1',
        occurred_at: '2026-08-18T02:03:04.000Z',
        producer: 'relay_harness_scan',
        root_agent: 'pulse',
        scan_ref: SCAN_REF_A,
      });
      expect(raw).not.toContain('sc_20260818');
    } finally {
      rmSync(dir, { recursive: true, force: true });
    }
  });

  it('is default-off and rejects relative destinations without affecting the scheduler', () => {
    const record = relaySchedulerLineageRecord({
      runId: 'run-a',
      jobId: 'job-a',
      owner: 'pulse',
      startedAt: 0,
    });
    expect(emitRelaySchedulerLineage(record, {})).toBe(false);

    const warn = vi.spyOn(console, 'warn').mockImplementation(() => {});
    expect(emitRelaySchedulerLineage(record, { [COMPUTE_COST_LINEAGE_DIR_ENV]: 'relative/path' })).toBe(false);
    expect(warn).toHaveBeenCalledOnce();
    warn.mockRestore();
  });

  it('fails closed on missing stable producer keys', () => {
    expect(() => buildRelaySchedulerLineage({ runId: '', jobId: 'job', owner: 'pulse' })).toThrow(/runId/);
    expect(() => buildRelaySchedulerLineage({ runId: 'run', jobId: '', owner: 'pulse' })).toThrow(/jobId/);
    expect(() => buildRelaySchedulerLineage({ runId: 'run', jobId: 'job', owner: ' ' })).toThrow(/owner/);
  });
});
