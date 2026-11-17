import { createHash } from 'node:crypto';
import { appendFileSync, mkdirSync } from 'node:fs';
import { isAbsolute, join } from 'node:path';

export const COMPUTE_COST_LINEAGE_DIR_ENV = 'PULSE_COMPUTE_COST_RELAY_LINEAGE_DIR';
export const COMPUTE_COST_LINEAGE_SCHEMA = 'relay_lineage_evidence.v1';

export type HarnessScanRef = `scan_v1_${string}`;

const HARNESS_SCAN_REF_PATTERN = /^scan_v1_[0-9a-f]{64}$/;

export interface RelaySchedulerLineage {
  root_agent: string;
  execution_mode: 'automatic';
  session_role: 'main';
  root_run_ref: string;
  parent_run_ref: 'unknown';
  cron_job_ref: string;
  scan_ref: 'unknown' | HarnessScanRef;
}

export interface RelaySchedulerLineageRecord extends RelaySchedulerLineage {
  schema: typeof COMPUTE_COST_LINEAGE_SCHEMA;
  occurred_at: string;
  producer: 'relay_scheduler';
}

export interface RelayHarnessScanLineageRecord extends RelaySchedulerLineage {
  schema: typeof COMPUTE_COST_LINEAGE_SCHEMA;
  occurred_at: string;
  producer: 'relay_harness_scan';
}

export type RelayLineageEvidenceRecord =
  | RelaySchedulerLineageRecord
  | RelayHarnessScanLineageRecord;

function nonEmpty(value: string, field: string): string {
  const normalized = value.trim();
  if (!normalized) throw new Error(`${field} must be non-empty`);
  return normalized;
}

function privateRef(prefix: 'lineage_v1_' | 'cron_v1_', domain: string, parts: string[]): string {
  const digest = createHash('sha256')
    .update(['pulse.compute_cost.lineage.v1', domain, ...parts].join('\0'), 'utf8')
    .digest('hex');
  return `${prefix}${digest}`;
}

function normalizeHarnessScanRef(value: string): HarnessScanRef {
  const normalized = nonEmpty(value, 'scanRef');
  if (!HARNESS_SCAN_REF_PATTERN.test(normalized)) {
    throw new Error('scanRef must be a Harness producer reference');
  }
  return normalized as HarnessScanRef;
}

/**
 * Build privacy-safe lineage for one Relay scheduler run.
 *
 * Raw scheduler run/job ids are inputs only. They are never returned or written.
 * The domains intentionally differ so equal producer keys cannot collide across
 * root-run and cron-job reference namespaces.
 */
export function buildRelaySchedulerLineage(input: {
  runId: string;
  jobId: string;
  owner: string;
}): RelaySchedulerLineage {
  const runId = nonEmpty(input.runId, 'runId');
  const jobId = nonEmpty(input.jobId, 'jobId');
  const owner = nonEmpty(input.owner, 'owner');
  return {
    root_agent: owner,
    execution_mode: 'automatic',
    session_role: 'main',
    root_run_ref: privateRef('lineage_v1_', 'relay_scheduler_run', [runId]),
    parent_run_ref: 'unknown',
    cron_job_ref: privateRef('cron_v1_', 'relay_scheduler_job', [owner, jobId]),
    scan_ref: 'unknown',
  };
}

/**
 * Enrich scheduler lineage only from an explicit reference produced by Harness.
 * Relay never hashes or infers a scan from scan_id, cwd, prompt, paths, or timing.
 */
export function attachHarnessScanRef(
  lineage: RelaySchedulerLineage,
  scanRef: string,
): RelaySchedulerLineage {
  const normalized = normalizeHarnessScanRef(scanRef);
  if (lineage.scan_ref !== 'unknown' && lineage.scan_ref !== normalized) {
    throw new Error('conflicting Harness scanRef for Relay scheduler run');
  }
  return { ...lineage, scan_ref: normalized };
}

export function relaySchedulerLineageRecord(input: {
  runId: string;
  jobId: string;
  owner: string;
  startedAt: number;
}): RelaySchedulerLineageRecord {
  if (!Number.isFinite(input.startedAt)) throw new Error('startedAt must be finite');
  return {
    schema: COMPUTE_COST_LINEAGE_SCHEMA,
    occurred_at: new Date(input.startedAt).toISOString(),
    producer: 'relay_scheduler',
    ...buildRelaySchedulerLineage(input),
  };
}

export function relayHarnessScanLineageRecord(input: {
  lineage: RelaySchedulerLineage;
  observedAt: number;
}): RelayHarnessScanLineageRecord {
  if (!Number.isFinite(input.observedAt)) throw new Error('observedAt must be finite');
  if (input.lineage.scan_ref === 'unknown') {
    throw new Error('Harness scan lineage record requires an explicit scanRef');
  }
  return {
    schema: COMPUTE_COST_LINEAGE_SCHEMA,
    occurred_at: new Date(input.observedAt).toISOString(),
    producer: 'relay_harness_scan',
    ...input.lineage,
  };
}

/**
 * Candidate evidence writer. It is deliberately default-off: setting an
 * absolute PULSE_COMPUTE_COST_RELAY_LINEAGE_DIR opts a Relay deployment into
 * the producer without changing scheduling/runtime behavior. Write failures
 * are evidence loss only and must never fail a scheduled job.
 */
export function emitRelaySchedulerLineage(
  record: RelayLineageEvidenceRecord,
  env: NodeJS.ProcessEnv = process.env,
): boolean {
  const rawDir = env[COMPUTE_COST_LINEAGE_DIR_ENV]?.trim();
  if (!rawDir) return false;
  if (!isAbsolute(rawDir)) {
    console.warn({
      ts: new Date().toISOString(),
      level: 'warn',
      module: 'compute-cost-lineage',
      msg: `${COMPUTE_COST_LINEAGE_DIR_ENV} must be absolute; lineage evidence not written`,
    });
    return false;
  }

  try {
    mkdirSync(rawDir, { recursive: true });
    const day = record.occurred_at.slice(0, 10);
    appendFileSync(join(rawDir, `${day}.jsonl`), `${JSON.stringify(record)}\n`, { encoding: 'utf8' });
    return true;
  } catch (error) {
    console.warn({
      ts: new Date().toISOString(),
      level: 'warn',
      module: 'compute-cost-lineage',
      msg: 'failed to write ComputeCost Relay lineage evidence; scheduled job continues',
      error: error instanceof Error ? error.message : String(error),
    });
    return false;
  }
}
