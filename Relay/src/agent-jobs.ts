/**
 * Agent-managed scheduled jobs (`pulse_jobs` tool, POST /internal/agent-jobs).
 *
 * An Agent lists its own jobs and adds, edits, enables, disables or deletes
 * the ones it may manage, without PULSE writing cron-jobs.json, registering
 * the execution permission or reloading by hand. The caller is the Harness
 * owner of the conversation whose signed request Relay verified
 * (agent-jobs-auth.ts), and the session the core identity names must be that
 * conversation's native session; every rule below is enforced here, whatever
 * the caller sends:
 *
 * - only jobs whose `owner` is the caller; ids start with `<owner>-`;
 * - the platform families (`-collab-inbox`, `-collab-deliver`, `-pr-review-*`)
 *   are never touched;
 * - add, edit, enable and delete only jobs this module created (`managedBy: 'agent'`);
 *   any other own job may only be disabled (e.g. retire a heartbeat), never
 *   re-enabled, so a pause PULSE or the human decided stays in force;
 * - at most MAX_AGENT_JOBS managed jobs per owner, each at most hourly
 *   (a single minute in the cron expression), batch runs at most an hour;
 * - a wake job (`wake.workItemId`) reuses the owner's collaboration-inbox gate
 *   and reminder target; a batch job runs in the owner's own workspace.
 *
 * Every change writes cron-jobs.json and its automation-control entry, then
 * reloads the scheduler. Node-placement mode and a held maintenance barrier
 * refuse every change.
 */
import { lstatSync, readFileSync, renameSync, unlinkSync, writeFileSync } from 'node:fs';
import { dirname, isAbsolute, join, normalize } from 'node:path';
import { validate as cronValidate } from 'node-cron';
import { writeCronJobsConfig } from './runtime-artifact-receipt.js';
import { loadJobs } from './scheduler.js';
import { resolveCronJobsStatePath } from './job-store.js';
import type { ScheduledJob } from './types.js';

export const AGENT_JOBS_SCHEMA = 'pulse.agent_jobs/v1';
export const MAX_AGENT_JOBS = 8;
export const MAX_BATCH_TIMEOUT = 3600;
const PROTECTED = /-(collab-inbox|collab-deliver|pr-review-[a-z]+)$/;
const ID = /^[a-z0-9][a-z0-9-]{0,62}$/;
const WI = /^wi_\d{8}_\d{3}$/;
const ACTIONS = new Set(['list', 'add', 'edit', 'enable', 'disable', 'delete']);

export interface AgentJobRequest {
  actor: string;
  /** The core identity's session reference and surface of the calling conversation. */
  session?: string;
  surface?: string;
  action: string;
  job_id?: string;
  /** add / edit */
  name?: string;
  schedule?: { expr?: unknown; tz?: unknown };
  prompt?: unknown;
  wake?: { workItemId?: unknown };
  inject_file?: unknown;
  timeout?: unknown;
}

export interface AgentJobsDeps {
  cronJobsPath: string;
  controlPath: string;
  /** The owner's own workspace, from the agent configuration. */
  workspaceFor(owner: string): string | undefined;
  reload(): void;
  /** A conversation this Relay hosts for `actor` with that session reference. */
  hosted(actor: string, session: string, surface: string): boolean;
  /** A reason to refuse every change now (maintenance barrier, placement mode). */
  refusal(): string | undefined;
  now?: () => Date;
}

export class AgentJobError extends Error {}

interface CronDocument { version?: number; jobs: Array<ScheduledJob & Record<string, unknown>> }

function fail(message: string): never { throw new AgentJobError(message); }

function readDocument(path: string): CronDocument {
  const value = JSON.parse(readFileSync(path, 'utf8')) as CronDocument;
  if (!value || !Array.isArray(value.jobs)) fail('cron-jobs.json has no jobs list');
  return value;
}

function summary(job: ScheduledJob & Record<string, unknown>) {
  return {
    id: job.id, name: job.name, enabled: job.enabled, schedule: job.schedule,
    kind: job.wake ? 'wake' : 'batch', ...(job.wake ? { workItemId: job.wake.workItemId } : {}),
    managed: job.managedBy === 'agent', protected: PROTECTED.test(job.id),
  };
}

/** At most hourly: one fixed minute, a valid expression and time zone. */
function schedule(value: AgentJobRequest['schedule']): { expr: string; tz: string } {
  const expr = typeof value?.expr === 'string' ? value.expr.trim() : '';
  const tz = typeof value?.tz === 'string' ? value.tz : 'Asia/Shanghai';
  const fields = expr.split(/\s+/);
  if (fields.length !== 5 || !cronValidate(expr)) fail('schedule.expr must be a 5-field cron expression');
  if (!/^\d{1,2}$/.test(fields[0]!) || Number(fields[0]) > 59) fail('schedule runs at most hourly: give one fixed minute');
  try { new Intl.DateTimeFormat('en-US', { timeZone: tz }); } catch { fail('schedule.tz is not a time zone'); }
  return { expr, tz };
}

function prompt(value: unknown): string {
  if (typeof value !== 'string' || !value.trim() || value.length > 4000) fail('prompt must be 1-4000 characters');
  return value;
}

/** The owner's inbox job carries its gate program and its human's chat. */
function inboxOf(jobs: CronDocument['jobs'], owner: string) {
  const inbox = jobs.find(job => job.id === `${owner}-collab-inbox` && job.agent?.gate);
  if (!inbox?.agent.gate) fail(`${owner} has no collaboration-inbox job to take the wake gate and reminder target from`);
  const args = inbox.agent.gate.args ?? [];
  const script = args.find(arg => arg.endsWith('collab-inbox-gate.py'));
  const harness = args[args.indexOf('--harness') + 1];
  if (!script || args.indexOf('--harness') < 0 || !harness) fail(`${owner}-collab-inbox gate is not the inbox gate`);
  return { python: inbox.agent.gate.command, script, harness, delivery: inbox.delivery };
}

function build(request: AgentJobRequest, jobs: CronDocument['jobs'], workspace: string,
  previous?: CronDocument['jobs'][number]): CronDocument['jobs'][number] {
  const owner = request.actor;
  const id = request.job_id!;
  const name = typeof request.name === 'string' && request.name.trim() ? request.name.trim().slice(0, 80)
    : previous?.name ?? id;
  const wi = request.wake?.workItemId ?? previous?.wake?.workItemId;
  const base = {
    id, name, enabled: previous?.enabled ?? true, owner, managedBy: 'agent' as const,
    schedule: request.schedule !== undefined ? schedule(request.schedule) : previous!.schedule,
  };
  const text = request.prompt !== undefined ? prompt(request.prompt) : previous!.agent.prompt;
  if (wi !== undefined) {
    if (typeof wi !== 'string' || !WI.test(wi)) fail('wake.workItemId must be a WI id');
    const inbox = inboxOf(jobs, owner);
    if (!inbox.delivery?.to && !inbox.delivery?.feishuOpenId) fail(`${owner}-collab-inbox names no chat to remind`);
    return {
      ...base, wake: { workItemId: wi }, convergent: false,
      agent: { workspace, prompt: text, gate: {
        command: inbox.python, timeout: 150, skipExitCode: 3,
        args: ['-I', '-B', inbox.script, 'wake', '--actor', owner, '--wi', wi, '--job', id,
          '--harness', inbox.harness, '--inbox-timeout', '120'],
      } },
      delivery: { mode: 'none',
        ...(inbox.delivery?.to ? { to: inbox.delivery.to } : {}),
        ...(inbox.delivery?.channel ? { channel: inbox.delivery.channel } : {}),
        ...(inbox.delivery?.feishuOpenId ? { feishuOpenId: inbox.delivery.feishuOpenId } : {}) },
    } as CronDocument['jobs'][number];
  }
  const timeout = request.timeout ?? previous?.agent.timeout ?? 1800;
  if (typeof timeout !== 'number' || !Number.isInteger(timeout) || timeout < 60 || timeout > MAX_BATCH_TIMEOUT) {
    fail(`timeout must be 60-${MAX_BATCH_TIMEOUT} seconds`);
  }
  const inject = request.inject_file ?? previous?.agent.inject_file;
  if (inject !== undefined && (typeof inject !== 'string' || isAbsolute(inject)
    || normalize(inject).startsWith('..'))) fail('inject_file must be a path inside the workspace');
  return {
    ...base,
    agent: { workspace, prompt: text, timeout, ...(inject ? { inject_file: inject } : {}) },
    delivery: { mode: 'none' },
  } as CronDocument['jobs'][number];
}

function readControl(path: string) {
  const info = lstatSync(path);
  if (!info.isFile() || info.nlink !== 1) fail('automation-control.json is not a plain file');
  let control: { revision: number; updated_at: string; entries: Record<string, Record<string, unknown>> };
  try { control = JSON.parse(readFileSync(path, 'utf8')); } catch { fail('automation-control.json is not valid JSON'); }
  if (!Number.isInteger(control.revision) || !control.entries || typeof control.entries !== 'object') {
    fail('automation-control.json has no revision or entries');
  }
  return { control, info };
}

function writeControl(path: string, entry: string, owner: string, enabled: boolean | undefined, now: Date): void {
  const { control, info } = readControl(path);
  if (enabled === undefined) delete control.entries[entry];
  else control.entries[entry] = { owner, enabled, decision_ref: `agent:${owner}` };
  control.revision += 1;
  control.updated_at = now.toISOString();
  const temporary = join(dirname(path), `.automation-control.${process.pid}.tmp`);
  writeFileSync(temporary, `${JSON.stringify(control, null, 2)}\n`, { mode: info.mode & 0o777 });
  renameSync(temporary, path);
}

/** The scheduler's own strict load of the candidate, before anything is written. */
function validateCandidate(path: string, document: CronDocument): void {
  const candidate = join(dirname(path), `.agent-jobs-candidate.${process.pid}.json`);
  writeFileSync(candidate, JSON.stringify(document), { mode: 0o600 });
  try {
    loadJobs(candidate, resolveCronJobsStatePath(path), { strict: true });
  } catch (error) {
    fail(`candidate rejected by the scheduler: ${error instanceof Error ? error.message : String(error)}`);
  } finally {
    try { unlinkSync(candidate); } catch { /* already gone */ }
  }
}

export function applyAgentJobs(request: AgentJobRequest, deps: AgentJobsDeps) {
  const owner = request.actor;
  if (typeof owner !== 'string' || !ID.test(owner)) fail('actor is required');
  if (!ACTIONS.has(request.action)) fail(`action must be one of ${[...ACTIONS].join(', ')}`);
  if (typeof request.session !== 'string' || typeof request.surface !== 'string'
    || !deps.hosted(owner, request.session, request.surface)) fail('not a conversation this Relay hosts for that Agent');
  const document = readDocument(deps.cronJobsPath);
  const own = document.jobs.filter(job => job.owner === owner);
  if (request.action === 'list') return { schema: AGENT_JOBS_SCHEMA, jobs: own.map(summary) };

  const refusal = deps.refusal();
  if (refusal) fail(refusal);
  const id = request.job_id;
  if (typeof id !== 'string' || !ID.test(id) || !id.startsWith(`${owner}-`)) fail(`job_id must start with "${owner}-"`);
  if (PROTECTED.test(id)) fail('platform jobs (collab-inbox, collab-deliver, pr-review) are not agent-managed');
  const index = document.jobs.findIndex(job => job.id === id);
  const current = index >= 0 ? document.jobs[index] : undefined;
  if (current && current.owner !== owner) fail('job belongs to another owner');
  const managed = current?.managedBy === 'agent';
  const now = deps.now?.() ?? new Date();
  let enabled: boolean | undefined;

  if (request.action === 'add') {
    if (current) fail('job already exists; use edit');
    if (own.filter(job => job.managedBy === 'agent').length >= MAX_AGENT_JOBS) fail(`at most ${MAX_AGENT_JOBS} agent-managed jobs`);
    const workspace = deps.workspaceFor(owner) ?? fail(`${owner} has no configured workspace`);
    document.jobs.push(build(request, document.jobs, workspace));
    enabled = true;
  } else if (!current) {
    fail('job not found');
  } else if (request.action === 'edit') {
    if (!managed) fail('only agent-managed jobs are edited here');
    document.jobs[index] = build(request, document.jobs, current.agent.workspace, current);
    enabled = document.jobs[index]!.enabled;
  } else if (request.action === 'delete') {
    if (!managed) fail('only agent-managed jobs are deleted; disable this one');
    document.jobs.splice(index, 1);
    enabled = undefined;
  } else {
    enabled = request.action === 'enable';
    if (enabled && !managed) fail('only agent-managed jobs are enabled here; a platform pause stays');
    document.jobs[index] = { ...current, enabled };
  }
  validateCandidate(deps.cronJobsPath, document);
  readControl(deps.controlPath);  // A broken control file refuses before anything is written.
  const original = { cron: readDocument(deps.cronJobsPath), control: readFileSync(deps.controlPath) };
  try {
    writeCronJobsConfig(deps.cronJobsPath, document, `agent-jobs:${owner}`);
    writeControl(deps.controlPath, `job:${id}`, owner, enabled, now);
    deps.reload();
  } catch (error) {
    // Put both files back; the scheduler's reload replaces nothing when it throws.
    writeCronJobsConfig(deps.cronJobsPath, original.cron, `agent-jobs:${owner}:rollback`);
    writeFileSync(deps.controlPath, original.control);
    fail(`change not applied, configuration restored: ${error instanceof Error ? error.message : String(error)}`);
  }
  const after = document.jobs.find(job => job.id === id);
  return { schema: AGENT_JOBS_SCHEMA, action: request.action, job: after ? summary(after) : { id, deleted: true } };
}
