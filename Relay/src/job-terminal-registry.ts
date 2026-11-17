import { randomUUID } from 'node:crypto';
import {
  attachHarnessScanRef,
  buildRelaySchedulerLineage,
  emitRelaySchedulerLineage,
  relayHarnessScanLineageRecord,
  relaySchedulerLineageRecord,
  type RelaySchedulerLineage,
} from './compute-cost-lineage.js';

export type JobTerminalDeliveryStatus = 'open' | 'sending' | 'sent' | 'failed';

export interface JobTerminalDeliveryReceipt {
  channel: 'telegram' | 'feishu';
  status: 'sent' | 'failed';
  externalMessageId?: string;
  attempts?: number;
  error?: string;
}

export interface JobTerminalRunSnapshot {
  runId: string;
  jobId: string;
  owner: string;
  lineage: RelaySchedulerLineage;
  status: JobTerminalDeliveryStatus;
  receipts: JobTerminalDeliveryReceipt[];
  startedAt: number;
  finishedAt?: number;
}

export interface JobTerminalReservation {
  deliver: boolean;
  deduplicated: boolean;
  reason?: 'not_found' | 'owner_mismatch' | 'ambiguous_owner';
  run?: JobTerminalRunSnapshot;
}

const DEFAULT_RETENTION_MS = 2 * 60 * 60 * 1000;

/**
 * Process-local coordinator for one user-visible terminal delivery per cron run.
 *
 * `/internal/push` and the scheduler fallback share this instance. Reserving
 * before I/O closes the concurrent-retry race; a failed delivery may be retried,
 * while a sent or in-flight terminal is deduplicated.
 *
 * ComputeCost lineage is produced at the same seam because startRun owns all
 * explicit scheduler facts needed for safe attribution: owner, stable job id and
 * per-run id. Harness may later enrich that same run with its producer-owned
 * `scan_ref`; raw scan ids never enter this registry or the optional writer.
 */
export class JobTerminalRegistry {
  private readonly runs = new Map<string, JobTerminalRunSnapshot>();

  constructor(private readonly retentionMs = DEFAULT_RETENTION_MS) {}

  startRun(jobId: string, owner: string): JobTerminalRunSnapshot {
    this.prune();
    const runId = randomUUID();
    const startedAt = Date.now();
    const lineage = buildRelaySchedulerLineage({ runId, jobId, owner });
    const run: JobTerminalRunSnapshot = {
      runId,
      jobId,
      owner,
      lineage,
      status: 'open',
      receipts: [],
      startedAt,
    };
    this.runs.set(run.runId, run);
    emitRelaySchedulerLineage(relaySchedulerLineageRecord({ runId, jobId, owner, startedAt }));
    return this.snapshot(run);
  }

  attachHarnessScanRef(
    runId: string,
    owner: string,
    scanRef: string,
    observedAt = Date.now(),
  ): JobTerminalRunSnapshot {
    const run = this.runs.get(runId);
    if (!run) throw new Error('scheduler run not found for Harness scanRef');
    if (run.owner !== owner) throw new Error('scheduler run owner mismatch for Harness scanRef');

    const enriched = attachHarnessScanRef(run.lineage, scanRef);
    if (enriched.scan_ref !== run.lineage.scan_ref) {
      run.lineage = enriched;
      emitRelaySchedulerLineage(relayHarnessScanLineageRecord({ lineage: enriched, observedAt }));
    }
    return this.snapshot(run);
  }

  finishRun(runId: string): void {
    const run = this.runs.get(runId);
    if (run && run.finishedAt === undefined) run.finishedAt = Date.now();
  }

  reserve(runId: string | undefined, owner: string): JobTerminalReservation {
    const resolved = this.resolve(runId, owner);
    if (!resolved.run) {
      return { deliver: false, deduplicated: false, reason: resolved.reason };
    }

    const run = resolved.run;
    if (run.status === 'sent' || run.status === 'sending') {
      return { deliver: false, deduplicated: true, run: this.snapshot(run) };
    }

    run.status = 'sending';
    run.receipts = [];
    return { deliver: true, deduplicated: false, run: this.snapshot(run) };
  }

  complete(runId: string, receipts: readonly JobTerminalDeliveryReceipt[]): JobTerminalRunSnapshot | undefined {
    const run = this.runs.get(runId);
    if (!run) return undefined;
    run.receipts = receipts.map(receipt => ({ ...receipt }));
    run.status = receipts.some(receipt => receipt.status === 'sent') ? 'sent' : 'failed';
    return this.snapshot(run);
  }

  get(runId: string): JobTerminalRunSnapshot | undefined {
    const run = this.runs.get(runId);
    return run ? this.snapshot(run) : undefined;
  }

  private resolve(
    runId: string | undefined,
    owner: string,
  ): { run?: JobTerminalRunSnapshot; reason?: JobTerminalReservation['reason'] } {
    if (runId) {
      const run = this.runs.get(runId);
      if (!run) return { reason: 'not_found' };
      if (run.owner !== owner) return { reason: 'owner_mismatch' };
      return { run };
    }

    const active = Array.from(this.runs.values()).filter(run => run.owner === owner && run.finishedAt === undefined);
    if (active.length === 0) return { reason: 'not_found' };
    if (active.length > 1) return { reason: 'ambiguous_owner' };
    return { run: active[0] };
  }

  private prune(now = Date.now()): void {
    for (const [runId, run] of this.runs) {
      if (run.finishedAt !== undefined && now - run.finishedAt > this.retentionMs) {
        this.runs.delete(runId);
      }
    }
  }

  private snapshot(run: JobTerminalRunSnapshot): JobTerminalRunSnapshot {
    return {
      ...run,
      lineage: { ...run.lineage },
      receipts: run.receipts.map(receipt => ({ ...receipt })),
    };
  }
}
