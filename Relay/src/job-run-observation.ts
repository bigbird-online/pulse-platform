import type { JobResult, JobRunObservation, ScheduledJob } from './types.js';

const MAX_UNRESOLVED = 32;
const PROCESS_STARTED_AT = Date.now() - process.uptime() * 1000;

export function startObservation(job: ScheduledJob, scheduledFor?: Date, now = new Date()): JobRunObservation {
  return {
    definitionHash: job._state?.definitionHash, phase: 'queued',
    startedAt: now.toISOString(), scheduledFor: scheduledFor?.toISOString(),
    schedulerPid: process.pid, schedulerStartedAt: PROCESS_STARTED_AT,
  };
}

/** Reuses the scheduler's existing state write; never changes admission or task outcome. */
export function finishObservation(job: ScheduledJob, result: JobResult,
  run: JobRunObservation, now = new Date()): NonNullable<ScheduledJob['_state']> {
  const state = job._state ?? { consecutiveErrors: 0 };
  // A pre-LLM gate skip and a declared output skip are both intended no-work runs.
  const skipped = result.skipped === true
    || (result.deliveryStatus === 'skipped' && !!job.delivery.skipIfOutputIncludes?.length);
  const receipt: JobRunObservation = {
    ...run, runId: result.runId, finishedAt: now.toISOString(), status: result.status,
    executionStatus: result.executionStatus, deliveryStatus: result.deliveryStatus,
    // An explicitly configured post-run check is part of executionStatus in executeJob.
    artifactVerified: job.agent.expectArtifact ? result.executionStatus === 'ok' : undefined,
    // The verify command's own receipt, never inferred from the exit status.
    verifyStatus: job.agent.verify ? result.verifyStatus : undefined,
    skipped, deferred: result.deferred === true,
    // Why a gate skipped (its first output), so readers can tell "nothing to do" from "already reported".
    ...(skipped && result.reasonCode === 'gate_skip' && result.output
      ? { skipSummary: result.output.slice(0, 500) } : {}),
  };
  const prior = state.unresolvedResults ?? [];
  // A run is identified by its runId. An earlier receipt is settled only by a
  // run that started after it finished (runs of one job may overlap, and event
  // runs of one minute share a slot): a later run of the same slot and
  // definition replaces it, and a convergent success, having re-read the whole
  // state, settles every such miss. A concurrent run never settles another.
  const readBefore = (item: JobRunObservation): boolean =>
    !item.finishedAt || Date.parse(item.finishedAt) <= Date.parse(run.startedAt);
  const sameSlot = (item: JobRunObservation): boolean => !!run.scheduledFor &&
    item.scheduledFor === run.scheduledFor && item.definitionHash === run.definitionHash;
  const settled = result.status === 'ok' && !result.deferred;
  const unresolved = prior.filter(item => !(
    (!!item.runId && item.runId === receipt.runId)
    || (readBefore(item) && (sameSlot(item) || (settled && job.convergent === true)))));
  if (!settled) unresolved.push(receipt);
  return {
    ...state, currentRun: undefined, lastResult: receipt,
    unresolvedResults: unresolved.slice(-MAX_UNRESOLVED),
    resultHistoryIncomplete: state.resultHistoryIncomplete === true || unresolved.length > MAX_UNRESOLVED,
  };
}
