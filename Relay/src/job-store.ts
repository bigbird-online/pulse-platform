import { resolveRelayDataDir } from './config-paths.js';
import { managedRelayPath } from './managed-paths.js';
import { existsSync, mkdirSync, readFileSync, writeFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { validate as cronValidate } from 'node-cron';
import type { ScheduledJob } from './types.js';
import { writeCronJobsConfig } from './runtime-artifact-receipt.js';

type JobRunState = NonNullable<ScheduledJob['_state']>;

export interface JobStore {
  listJobs(owner?: string): ScheduledJob[];
  addJob(job: Omit<ScheduledJob, '_state'>): ScheduledJob;
  removeJob(id: string, owner: string): boolean;
  getJob(id: string): ScheduledJob | undefined;
  getAllJobs(): ScheduledJob[];
  persistConfig(): void;
  persistRunState(): void;
  persistRunStateNow(): boolean;
  /** Cancel every pending write; a replaced store must never write again. */
  dispose(): void;
}

function stripRunState(job: ScheduledJob): Omit<ScheduledJob, '_state'> {
  const { _state, ...configJob } = job;
  void _state;
  return configJob;
}

export function resolveCronJobsStatePath(configPath: string): string {
  const managed = managedRelayPath('cronState', process.env['CRON_JOBS_STATE_PATH']);
  if (managed) return managed;
  const home = process.env['HOME'];
  if (home || process.env['PULSE_RELAY_PATHS']) return join(resolveRelayDataDir(), 'cron-jobs-state.json');
  return `${configPath}.state.json`;
}

export function readCronJobsState(statePath: string): Record<string, JobRunState> {
  if (!existsSync(statePath)) return {};
  try {
    const raw = JSON.parse(readFileSync(statePath, 'utf-8')) as { jobs?: Record<string, JobRunState> };
    return raw.jobs ?? {};
  } catch (err) {
    console.error({ ts: new Date().toISOString(), level: 'error', module: 'scheduler', msg: 'Failed to parse cron job state', path: statePath, error: String(err) });
    return {};
  }
}

export function mergeCronJobsState(jobs: ScheduledJob[], statePath: string): ScheduledJob[] {
  const state = readCronJobsState(statePath);
  return jobs.map(job => ({
    ...job,
    _state: state[job.id] ?? job._state ?? { consecutiveErrors: 0 },
  }));
}

export function createJobStore(initialJobs: ScheduledJob[], configPath: string, statePath = resolveCronJobsStatePath(configPath)): JobStore {
  const jobs: ScheduledJob[] = [...initialJobs];
  let persistTimer: ReturnType<typeof setTimeout> | null = null;
  let statePersistTimer: ReturnType<typeof setTimeout> | null = null;

  function persistConfig(): void {
    if (persistTimer) clearTimeout(persistTimer);
    persistTimer = setTimeout(() => {
      try {
        const data = { version: 2, jobs: jobs.map(stripRunState) };
        writeCronJobsConfig(configPath, data, 'job-store-persist');
      } catch (err) {
        console.error({ ts: new Date().toISOString(), level: 'error', module: 'scheduler', msg: 'persistConfig failed', error: String(err) });
      }
    }, 200);
  }

  function persistRunState(): void {
    if (statePersistTimer) clearTimeout(statePersistTimer);
    statePersistTimer = setTimeout(() => {
      statePersistTimer = null;
      persistRunStateNow();
    }, 200);
  }

  function persistRunStateNow(): boolean {
    if (statePersistTimer) {
      clearTimeout(statePersistTimer);
      statePersistTimer = null;
    }
    try {
      mkdirSync(dirname(statePath), { recursive: true });
      const state = Object.fromEntries(
        jobs.map(job => [job.id, job._state ?? { consecutiveErrors: 0 }]),
      );
      writeFileSync(statePath, JSON.stringify({ version: 1, jobs: state }, null, 2) + '\n');
      return true;
    } catch (err) {
      console.error({ ts: new Date().toISOString(), level: 'error', module: 'scheduler', msg: 'persistRunState failed', error: String(err) });
      return false;
    }
  }

  function listJobs(owner?: string): ScheduledJob[] {
    if (!owner) return [...jobs];
    return jobs.filter(j => j.owner === owner || j.owner === 'system');
  }

  function addJob(input: Omit<ScheduledJob, '_state'>): ScheduledJob {
    if (jobs.some(j => j.id === input.id)) {
      throw new Error(`Job id "${input.id}" already exists`);
    }
    if (!cronValidate(input.schedule.expr)) {
      throw new Error(`Invalid cron expression: "${input.schedule.expr}"`);
    }
    const job: ScheduledJob = { ...input, _state: { consecutiveErrors: 0 } };
    writeCronJobsConfig(
      configPath,
      { version: 2, jobs: [...jobs, job].map(stripRunState) },
      'job-store-add',
    );
    jobs.push(job);
    persistRunState();
    return job;
  }

  function removeJob(id: string, owner: string): boolean {
    const idx = jobs.findIndex(j => j.id === id);
    if (idx === -1) return false;
    if (jobs[idx].owner !== owner) return false;
    const remaining = jobs.filter((_, jobIndex) => jobIndex !== idx);
    writeCronJobsConfig(
      configPath,
      { version: 2, jobs: remaining.map(stripRunState) },
      'job-store-remove',
    );
    jobs.splice(idx, 1);
    persistRunState();
    return true;
  }

  function getJob(id: string): ScheduledJob | undefined {
    return jobs.find(j => j.id === id);
  }

  function getAllJobs(): ScheduledJob[] {
    return [...jobs];
  }

  function dispose(): void {
    if (persistTimer) clearTimeout(persistTimer);
    if (statePersistTimer) clearTimeout(statePersistTimer);
    persistTimer = null;
    statePersistTimer = null;
  }

  return { listJobs, addJob, removeJob, getJob, getAllJobs, persistConfig, persistRunState, persistRunStateNow, dispose };
}
