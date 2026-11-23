// scheduler.ts — Agent Scheduler for Relay

import { stripRelaySecrets } from './relay-secrets.js';
import { spawn } from 'node:child_process';
import { createHash } from 'node:crypto';
import { readFileSync, writeFileSync, existsSync, statSync, realpathSync } from 'node:fs';
import { resolve, isAbsolute, relative, join as pathJoin } from 'node:path';
import { schedule as cronSchedule, validate as cronValidate } from 'node-cron';
import type { ScheduledTask } from 'node-cron';
import type { Bot } from 'grammy';
import type { DeliveryReceipt, ScheduledJob, JobResult, JobRunObservation, JobTriggerSource, JobTriggerStatus, Scheduler, RuntimeId } from './types.js';
import { createJobStore, mergeCronJobsState, resolveCronJobsStatePath, type JobStore } from './job-store.js';
import { writeCronJobsConfig } from './runtime-artifact-receipt.js';
import {
  deliverTelegramDocument as deliverTelegramDocumentRequest,
  deliverTelegramText,
  deliverFeishuFile as deliverFeishuFileRequest,
  deliverFeishuText as deliverFeishuTextRequest,
  type FeishuMessageClient,
} from './delivery.js';
import {
  createBatchRuntimeOrchestrator,
  createCcBatchRuntimeAdapter,
  createCcInteractiveBatchRuntimeAdapter,
  createCodexBatchRuntimeAdapter,
  failoverRuntimes,
  runCcBatch,
} from './runtime.js';
import { resolvePulseRuntimePriority } from './codex-defaults.js';
import { createAgyBatchRuntimeAdapter } from './agy-batch-runtime.js';
import { resolveComputeProfile } from './compute-profiles.js';
import { sharedAccessPromptLines, standingAccessPromptLine } from './config-paths.js';
import { standingAccessFor } from './standing-access.js';
import { JobTerminalRegistry } from './job-terminal-registry.js';
import { dueQuotaRetry, earliestResetAt, nextQuotaRetries } from './quota-retry.js';
import { startObservation, finishObservation } from './job-run-observation.js';
import { parseCollabBatch, startProgressNotice, type AgentDisplayName, type ProgressNotice } from './collab-progress-notice.js';

// --- Constants ---

const DEFAULT_TIMEOUT_MS = 3_600_000; // 60 minutes (cron job 缺省超时统一 60min)
const DEFAULT_RUNTIME_COOLDOWN_MS = 5 * 60 * 1000;
const DEFAULT_MISSED_RUN_RECONCILE_INTERVAL_MS = 60_000;
const DEFAULT_MISSED_RUN_LOOKBACK_MS = 26 * 60 * 60 * 1000;
const MIN_MISSED_RUN_RECONCILE_INTERVAL_MS = 1_000;
const SCHEDULER_MAX_CONCURRENT_JOBS_ENV = 'RELAY_SCHEDULER_MAX_CONCURRENT_JOBS';
const CRON_MODEL_LITERAL_FIELDS = ['ccModel', 'ccReasoningEffort', 'codexModel', 'codexReasoningEffort'] as const;

const MONTH_ALIASES: Record<string, number> = {
  jan: 1,
  feb: 2,
  mar: 3,
  apr: 4,
  may: 5,
  jun: 6,
  jul: 7,
  aug: 8,
  sep: 9,
  oct: 10,
  nov: 11,
  dec: 12,
};

const WEEKDAY_ALIASES: Record<string, number> = {
  sun: 0,
  mon: 1,
  tue: 2,
  wed: 3,
  thu: 4,
  fri: 5,
  sat: 6,
};

function deliveryStatusForMode(mode: ScheduledJob['delivery']['mode']): DeliveryReceipt['status'] {
  return mode === 'none' ? 'skipped' : 'skipped';
}

function parseDurationMs(value: string | undefined, fallback: number, min = 0): number {
  if (!value) return fallback;
  const parsed = Number.parseInt(value, 10);
  if (!Number.isFinite(parsed) || parsed < min) return fallback;
  return parsed;
}

function parseSchedulerConcurrencyLimit(value: string | undefined): number | undefined {
  const normalized = value?.trim();
  if (!normalized) return undefined;
  if (!/^\d+$/.test(normalized)) {
    throw new Error(`${SCHEDULER_MAX_CONCURRENT_JOBS_ENV} must be a positive integer`);
  }
  const parsed = Number.parseInt(normalized, 10);
  if (!Number.isSafeInteger(parsed) || parsed < 1) {
    throw new Error(`${SCHEDULER_MAX_CONCURRENT_JOBS_ENV} must be a positive safe integer`);
  }
  return parsed;
}

function floorToMinute(date: Date): Date {
  const result = new Date(date);
  result.setSeconds(0, 0);
  return result;
}

function parseCronValue(raw: string, aliases?: Record<string, number>): number | undefined {
  const normalized = raw.trim().toLowerCase();
  const alias = aliases?.[normalized.slice(0, 3)];
  if (alias !== undefined) return alias;
  if (!/^\d+$/.test(normalized)) return undefined;
  return Number.parseInt(normalized, 10);
}

function normalizeWeekday(value: number): number {
  return value === 7 ? 0 : value;
}

function cronFieldMatches(
  field: string,
  value: number,
  min: number,
  max: number,
  aliases?: Record<string, number>,
  normalize: (value: number) => number = v => v,
): boolean {
  for (const part of field.split(',')) {
    const trimmed = part.trim();
    if (!trimmed) continue;

    const [rangePart, stepRaw] = trimmed.split('/');
    const step = stepRaw === undefined ? 1 : Number.parseInt(stepRaw, 10);
    if (!Number.isFinite(step) || step <= 0) continue;

    let start: number | undefined;
    let end: number | undefined;
    if (rangePart === '*') {
      start = min;
      end = max;
    } else if (rangePart.includes('-')) {
      const [startRaw, endRaw] = rangePart.split('-', 2);
      start = parseCronValue(startRaw, aliases);
      end = parseCronValue(endRaw, aliases);
    } else {
      start = parseCronValue(rangePart, aliases);
      end = start;
    }

    if (start === undefined || end === undefined) continue;
    if (start < min || end > max || start > end) continue;

    for (let candidate = start; candidate <= end; candidate += step) {
      if (normalize(candidate) === normalize(value)) return true;
    }
  }

  return false;
}

function zonedCronParts(date: Date, timeZone: string): {
  second: number;
  minute: number;
  hour: number;
  day: number;
  month: number;
  weekday: number;
} {
  const parts = new Intl.DateTimeFormat('en-US', {
    timeZone,
    hourCycle: 'h23',
    weekday: 'short',
    month: 'numeric',
    day: 'numeric',
    hour: 'numeric',
    minute: 'numeric',
    second: 'numeric',
  }).formatToParts(date);
  const partValue = (type: Intl.DateTimeFormatPartTypes): string => parts.find(part => part.type === type)?.value ?? '';
  const weekday = WEEKDAY_ALIASES[partValue('weekday').toLowerCase().slice(0, 3)] ?? 0;
  return {
    second: Number.parseInt(partValue('second'), 10),
    minute: Number.parseInt(partValue('minute'), 10),
    hour: Number.parseInt(partValue('hour'), 10),
    day: Number.parseInt(partValue('day'), 10),
    month: Number.parseInt(partValue('month'), 10),
    weekday,
  };
}

function cronExpressionMatches(expr: string, timeZone: string, date: Date): boolean {
  const rawFields = expr.trim().split(/\s+/);
  if (rawFields.length !== 5 && rawFields.length !== 6) return false;
  const fields = rawFields.length === 5 ? ['0', ...rawFields] : rawFields;
  const parts = zonedCronParts(date, timeZone);
  return cronFieldMatches(fields[0], parts.second, 0, 59)
    && cronFieldMatches(fields[1], parts.minute, 0, 59)
    && cronFieldMatches(fields[2], parts.hour, 0, 23)
    && cronFieldMatches(fields[3], parts.day, 1, 31)
    && cronFieldMatches(fields[4], parts.month, 1, 12, MONTH_ALIASES)
    && cronFieldMatches(fields[5], parts.weekday, 0, 7, WEEKDAY_ALIASES, normalizeWeekday);
}

/** Upper bound of a job's `maxConcurrent`. */
export const MAX_JOB_CONCURRENCY = 8;

export function findLatestDueRun(
  job: ScheduledJob,
  now: Date,
  registeredAt: Date,
  lookbackMs = DEFAULT_MISSED_RUN_LOOKBACK_MS,
): Date | undefined {
  const lastRunAtMs = job._state?.lastRunAt ? Date.parse(job._state.lastRunAt) : NaN;
  // ``registeredAt`` is the activation floor for the *current job definition*.
  // A persisted lastRunAt belongs to an older definition after config reload;
  // it must never move the floor backwards and replay a slot that predates the
  // new definition (2026-07-19 ho_333 incident).
  const baselineMs = Number.isFinite(lastRunAtMs)
    ? Math.max(lastRunAtMs, registeredAt.getTime())
    : registeredAt.getTime();
  const searchStartMs = Math.max(baselineMs, now.getTime() - lookbackMs);
  const end = floorToMinute(now);
  let latest: Date | undefined;

  for (let ts = floorToMinute(new Date(searchStartMs)).getTime(); ts <= end.getTime(); ts += 60_000) {
    if (ts <= baselineMs) continue;
    const candidate = new Date(ts);
    if (cronExpressionMatches(job.schedule.expr, job.schedule.tz, candidate)) {
      latest = candidate;
    }
  }

  return latest;
}

function validTimeZone(tz: unknown): boolean {
  if (typeof tz !== 'string' || !tz.trim()) return false;
  try {
    new Intl.DateTimeFormat('en-US', { timeZone: tz });
    return true;
  } catch {
    return false;
  }
}

// --- loadJobs ---
/**
 * Startup tolerates a missing/unreadable file and skips invalid entries.
 * `strict` (hot reload) instead throws before anything is replaced: a missing,
 * unreadable or malformed file, or any invalid entry, keeps the running jobs.
 * Only a readable file with an empty `jobs` array means "no jobs".
 */
export function loadJobs(
  configPath: string,
  statePath = resolveCronJobsStatePath(configPath),
  options: { strict?: boolean } = {},
): ScheduledJob[] {
  if (!existsSync(configPath)) {
    if (options.strict) throw new Error(`cron-jobs.json not found: ${configPath}`);
    console.error({ ts: new Date().toISOString(), level: 'error', module: 'scheduler', msg: 'cron-jobs.json not found', path: configPath });
    return [];
  }

  let raw: unknown;
  try {
    const text = readFileSync(configPath, 'utf-8');
    raw = JSON.parse(text);
  } catch (err) {
    if (options.strict) throw new Error(`Failed to parse cron-jobs.json: ${String(err)}`);
    console.error({ ts: new Date().toISOString(), level: 'error', module: 'scheduler', msg: 'Failed to parse cron-jobs.json', error: String(err) });
    return [];
  }
  if (options.strict && (typeof raw !== 'object' || raw === null || Array.isArray(raw))) {
    throw new Error('cron-jobs.json is not an object');
  }

  const data = raw as { version?: number; jobs?: unknown[] };
  if ((data.version ?? 1) < 2) {
    for (const job of (data.jobs ?? [])) {
      const j = job as Record<string, unknown>;
      if (!j.owner) j.owner = 'system';
    }
    data.version = 2;
    writeCronJobsConfig(configPath, data, 'scheduler-v1-migration');
    console.log({ ts: new Date().toISOString(), level: 'info', module: 'scheduler', msg: 'Migrated cron-jobs.json v1 → v2 (added owner field)' });
  }

  if (!Array.isArray(data.jobs)) {
    if (options.strict) throw new Error('cron-jobs.json missing "jobs" array');
    console.error({ ts: new Date().toISOString(), level: 'error', module: 'scheduler', msg: 'cron-jobs.json missing "jobs" array' });
    return [];
  }

  const valid: ScheduledJob[] = [];
  const seen = new Set<string>();
  for (const entry of data.jobs) {
    const job = entry as Partial<ScheduledJob>;
    if (typeof job.id !== 'string' || !job.id.trim()) {
      console.warn({ ts: new Date().toISOString(), level: 'warn', module: 'scheduler', msg: 'Job missing a string "id", skipping', entry });
      continue;
    }
    // Two definitions with one id would register two cron tasks under one
    // key; the first could then never be stopped. Reload rejects the file.
    if (seen.has(job.id)) {
      if (options.strict) throw new Error(`Duplicate job id "${job.id}" in cron-jobs.json`);
      console.warn({ ts: new Date().toISOString(), level: 'warn', module: 'scheduler', msg: 'Duplicate job id, skipping', id: job.id });
      continue;
    }
    seen.add(job.id);
    if (job.schedule?.tz !== undefined && !validTimeZone(job.schedule.tz)) {
      console.warn({ ts: new Date().toISOString(), level: 'warn', module: 'scheduler', msg: 'Job has an invalid "schedule.tz", skipping', id: job.id });
      continue;
    }
    if (!job.schedule?.expr) {
      console.warn({ ts: new Date().toISOString(), level: 'warn', module: 'scheduler', msg: 'Job missing "schedule.expr", skipping', id: job.id });
      continue;
    }
    if (Object.prototype.hasOwnProperty.call(job, 'prompt')) {
      console.warn({ ts: new Date().toISOString(), level: 'warn', module: 'scheduler', msg: 'Job has top-level "prompt"; move it to "agent.prompt", skipping', id: job.id });
      continue;
    }
    if (!job.agent?.workspace) {
      console.warn({ ts: new Date().toISOString(), level: 'warn', module: 'scheduler', msg: 'Job missing "agent.workspace", skipping', id: job.id });
      continue;
    }
    if (!job.agent?.prompt) {
      console.warn({ ts: new Date().toISOString(), level: 'warn', module: 'scheduler', msg: 'Job missing "agent.prompt", skipping', id: job.id });
      continue;
    }
    if (!job.delivery?.mode) {
      console.warn({ ts: new Date().toISOString(), level: 'warn', module: 'scheduler', msg: 'Job missing "delivery.mode", skipping', id: job.id });
      continue;
    }
    const hardcodedModelFields = CRON_MODEL_LITERAL_FIELDS.filter(field =>
      Object.prototype.hasOwnProperty.call(job.agent, field));
    if (hardcodedModelFields.length > 0) {
      console.warn({
        ts: new Date().toISOString(),
        level: 'warn',
        module: 'scheduler',
        msg: 'Job hardcodes model settings; use agent.computeProfile, skipping',
        id: job.id,
        fields: hardcodedModelFields,
      });
      continue;
    }
    if (job.agent.gate) {
      const gate = job.agent.gate;
      const invalidGate = typeof gate.command !== 'string' || !gate.command.trim()
        || (gate.args !== undefined && (!Array.isArray(gate.args) || gate.args.some(arg => typeof arg !== 'string')))
        || (gate.timeout !== undefined && (!Number.isFinite(gate.timeout) || gate.timeout <= 0))
        || (gate.skipExitCode !== undefined && (!Number.isInteger(gate.skipExitCode) || gate.skipExitCode <= 0))
        || (gate.skipOutput !== undefined && (typeof gate.skipOutput !== 'string' || !gate.skipOutput.trim()));
      if (invalidGate) {
        console.warn({ ts: new Date().toISOString(), level: 'warn', module: 'scheduler', msg: 'Job has invalid agent.gate, skipping', id: job.id });
        continue;
      }
    }
    if (job.wake !== undefined) {
      const wake = job.wake as { workItemId?: unknown } | null;
      const remindable = Boolean(job.delivery.to || job.delivery.feishuOpenId);
      if (!wake || typeof wake.workItemId !== 'string' || !/^wi_\d{8}_\d{3}$/.test(wake.workItemId)
        || !job.agent.gate || !remindable) {
        console.warn({ ts: new Date().toISOString(), level: 'warn', module: 'scheduler',
          msg: 'Job has invalid "wake" (needs wake.workItemId, agent.gate and delivery.to or feishuOpenId), skipping', id: job.id });
        continue;
      }
    }
    if (job.agent.verify !== undefined) {
      const verify = job.agent.verify;
      const invalidVerify = typeof verify !== 'object' || verify === null
        || typeof verify.command !== 'string' || !verify.command.trim()
        || (verify.args !== undefined && (!Array.isArray(verify.args) || verify.args.some(arg => typeof arg !== 'string')))
        || (verify.timeout !== undefined && (!Number.isFinite(verify.timeout) || verify.timeout <= 0));
      if (invalidVerify) {
        console.warn({ ts: new Date().toISOString(), level: 'warn', module: 'scheduler', msg: 'Job has invalid agent.verify, skipping', id: job.id });
        continue;
      }
    }
    if (job.maxConcurrent !== undefined && !(Number.isInteger(job.maxConcurrent)
      && job.maxConcurrent >= 1 && job.maxConcurrent <= MAX_JOB_CONCURRENCY)) {
      console.warn({ ts: new Date().toISOString(), level: 'warn', module: 'scheduler', msg: 'Job has invalid "maxConcurrent", skipping', id: job.id });
      continue;
    }
    if (job.runtimeStrict !== undefined && typeof job.runtimeStrict !== 'boolean') {
      console.warn({ ts: new Date().toISOString(), level: 'warn', module: 'scheduler', msg: 'Job has invalid "runtimeStrict", skipping', id: job.id });
      continue;
    }
    if (job.sharedWorkspace !== undefined && typeof job.sharedWorkspace !== 'boolean') {
      console.warn({ ts: new Date().toISOString(), level: 'warn', module: 'scheduler', msg: 'Job has invalid "sharedWorkspace", skipping', id: job.id });
      continue;
    }
    if (job.wi !== undefined && (typeof job.wi !== 'string' || !/^wi_\d{8}_\d{3}$/.test(job.wi))) {
      console.warn({ ts: new Date().toISOString(), level: 'warn', module: 'scheduler', msg: 'Job has invalid "wi", skipping', id: job.id });
      continue;
    }
    const deliveryPathError = invalidDeliveryPaths(job.delivery);
    if (deliveryPathError) {
      console.warn({ ts: new Date().toISOString(), level: 'warn', module: 'scheduler', msg: `Job has invalid ${deliveryPathError}, skipping`, id: job.id });
      continue;
    }
    const retired = job as { execution?: { mode?: unknown }; delivery?: { mode?: unknown } };
    if (retired.execution?.mode === 'harness_dispatch' || retired.delivery?.mode === 'harness_escalation') {
      console.warn({ ts: new Date().toISOString(), level: 'warn', module: 'scheduler', msg: 'Job uses a retired Harness CLI mode, skipping', id: job.id });
      continue;
    }
    if (options.strict && !cronValidate(job.schedule.expr)) {
      throw new Error(`Job "${job.id}" has an invalid cron expression`);
    }
    valid.push(job as ScheduledJob);
  }
  if (options.strict && valid.length !== data.jobs.length) {
    throw new Error(`${data.jobs.length - valid.length} invalid job definition(s) in cron-jobs.json; see scheduler warnings`);
  }

  return mergeCronJobsState(valid, statePath);
}

// --- spawnCC ---
export async function spawnCC(
  workspace: string,
  prompt: string,
  timeoutMs: number,
): Promise<{ stdout: string; exitCode: number }> {
  const result = await runCcBatch({ workspace, prompt, timeoutMs });
  if (result.exitCode !== 0) {
    console.error({
      ts: new Date().toISOString(),
      level: 'error',
      module: 'scheduler',
      msg: 'CC non-zero exit',
      exitCode: result.exitCode,
      stderr: clipStderr(result.stderr),
      stdout: result.stdout.slice(0, 500),
    });
  }
  return { stdout: result.stdout, exitCode: result.exitCode };
}

// --- deliverResult ---
export async function deliverResult(
  job: ScheduledJob,
  result: JobResult,
  bot: Bot,
): Promise<DeliveryReceipt | undefined> {
  if (job.delivery.mode !== 'announce') return undefined;

  const to = job.delivery.to;
  if (!to) {
    console.warn({ ts: new Date().toISOString(), level: 'warn', module: 'scheduler', msg: 'announce mode but no delivery.to', job: job.id });
    return undefined;
  }

  const text = result.output.trim() || `[${job.name}] 执行完成（无输出）`;
  const receipt = await deliverTelegramText({
    id: `delivery:${job.id}:announce:${Date.now()}`,
    correlationId: `job:${job.id}`,
    channel: 'telegram',
    address: { channel: 'telegram', kind: 'telegram_chat', value: to },
    payload: { kind: 'text', text },
  }, bot);

  if (receipt.status === 'sent') {
    console.log({ ts: new Date().toISOString(), level: 'info', module: 'scheduler', job: job.id, msg: 'Delivered to Telegram', chatId: Number(to), receiptId: receipt.id, attempts: receipt.attempts });
  } else {
    console.error({ ts: new Date().toISOString(), level: 'error', module: 'scheduler', job: job.id, msg: 'TG delivery failed', receiptId: receipt.id, error: receipt.error });
  }
  return receipt;
}

function detectsRequiredNotificationFailure(prompt: string, stdout: string, stderr: string): string | undefined {
  const text = `${stdout}\n${stderr}`;
  const mentionsFailure = /PermissionError|Operation not permitted|permission denied|denied|failed|失败|未完成|不能提权|cannot.*retry|Could not resolve host|返回非 JSON|没有拿到\s*`?ok:true/i.test(text);

  const requiresTelegramDelivery = /sendDocument|api\.telegram\.org|ok:true|Telegram/.test(prompt);
  const mentionsTelegram = /Telegram|sendDocument|api\.telegram\.org|curl:\s*\(\d+\)|ok:true/i.test(text);
  if (requiresTelegramDelivery && mentionsTelegram && mentionsFailure) {
    return 'required Telegram delivery failed';
  }

  return undefined;
}

function formatDateForTimeZone(timeZone: string, at = new Date()): string {
  const parts = new Intl.DateTimeFormat('en-CA', {
    timeZone,
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
  }).formatToParts(at);
  const value = (type: string): string => parts.find(part => part.type === type)?.value ?? '';
  return `${value('year')}-${value('month')}-${value('day')}`;
}

function formatIsoWeekForTimeZone(timeZone: string): string {
  const parts = new Intl.DateTimeFormat('en-CA', {
    timeZone,
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
  }).formatToParts(new Date());
  const value = (type: string): number => Number(parts.find(part => part.type === type)?.value ?? '0');
  const localDate = new Date(Date.UTC(value('year'), value('month') - 1, value('day')));
  const isoDay = localDate.getUTCDay() || 7;
  localDate.setUTCDate(localDate.getUTCDate() + 4 - isoDay);
  const isoYear = localDate.getUTCFullYear();
  const yearStart = new Date(Date.UTC(isoYear, 0, 1));
  const week = Math.ceil((((localDate.getTime() - yearStart.getTime()) / 86_400_000) + 1) / 7);
  return `${isoYear}-W${String(week).padStart(2, '0')}`;
}

function expandDeliveryTemplate(template: string, job: ScheduledJob): string {
  const date = formatDateForTimeZone(job.schedule.tz);
  const isoWeek = formatIsoWeekForTimeZone(job.schedule.tz);
  return template
    .replaceAll('${DATE}', date)
    .replaceAll('${MONTH}', date.slice(0, 7))
    .replaceAll('${ISO_WEEK}', isoWeek);
}

/** Telegram's text-message limit; also bounds the Feishu text sent from `captionPath`. */
export const CAPTION_FILE_MAX_CHARS = 4096;
/** Byte ceiling checked before reading, so an oversized file is never loaded. */
const CAPTION_FILE_MAX_BYTES = CAPTION_FILE_MAX_CHARS * 4;

function invalidDeliveryPaths(delivery: ScheduledJob['delivery']): string | undefined {
  if (delivery.documentPath !== undefined
    && (typeof delivery.documentPath !== 'string' || !delivery.documentPath.trim())) {
    return 'delivery.documentPath';
  }
  if (delivery.sendOutput !== undefined
    && (typeof delivery.sendOutput !== 'boolean' || delivery.mode !== 'telegram_document')) {
    return 'delivery.sendOutput';
  }
  if (delivery.captionPath === undefined) return undefined;
  if (typeof delivery.captionPath !== 'string' || !delivery.captionPath.trim()
    || isAbsolute(delivery.captionPath)
    || (delivery.mode !== 'telegram_document' && delivery.mode !== 'feishu_document')) {
    return 'delivery.captionPath';
  }
  return undefined;
}

function insideDirectory(root: string, candidate: string): boolean {
  const rel = relative(root, candidate);
  return rel === '' || (!rel.startsWith('..') && !isAbsolute(rel));
}

type DeliveryPathResult = { ok: true; path: string } | { ok: false; errorMessage: string };

/**
 * Resolve a delivery file template. Absolute paths are used as written; a
 * relative path resolves against `agent.workspace` (as `expectArtifact.path`
 * does) and must stay inside it, both lexically and after following symlinks.
 * A path that does not exist yet passes here; the caller reports it missing.
 */
function resolveDeliveryPath(template: string, job: ScheduledJob, label: string): DeliveryPathResult {
  const expanded = expandDeliveryTemplate(template, job);
  if (isAbsolute(expanded)) return { ok: true, path: expanded };
  const workspace = resolve(job.agent.workspace);
  const candidate = resolve(workspace, expanded);
  if (!insideDirectory(workspace, candidate)) {
    return { ok: false, errorMessage: `${label} escapes agent.workspace: ${expanded}` };
  }
  if (existsSync(candidate) && !insideDirectory(realpathSync(workspace), realpathSync(candidate))) {
    return { ok: false, errorMessage: `${label} resolves outside agent.workspace: ${candidate}` };
  }
  return { ok: true, path: candidate };
}

/**
 * Read the `delivery.captionPath` text before anything is sent, so an unusable
 * caption fails the delivery without a half-sent document. `undefined` text
 * means no captionPath is configured.
 */
function readDeliveryCaptionFile(job: ScheduledJob, mode: string): { ok: true; text?: string } | { ok: false; errorMessage: string } {
  const template = job.delivery.captionPath;
  if (template === undefined) return { ok: true };
  const label = `${mode} captionPath`;
  if (isAbsolute(template)) return { ok: false, errorMessage: `${label} must be relative to agent.workspace: ${template}` };
  const resolved = resolveDeliveryPath(template, job, label);
  if (!resolved.ok) return resolved;
  const captionPath = resolved.path;
  if (!existsSync(captionPath)) return { ok: false, errorMessage: `${label} missing file: ${captionPath}` };
  const stat = statSync(captionPath);
  if (!stat.isFile()) return { ok: false, errorMessage: `${label} is not a regular file: ${captionPath}` };
  if (stat.size > CAPTION_FILE_MAX_BYTES) {
    return { ok: false, errorMessage: `${label} too large (${stat.size} > ${CAPTION_FILE_MAX_BYTES} bytes): ${captionPath}` };
  }
  let text: string;
  try {
    text = new TextDecoder('utf-8', { fatal: true }).decode(readFileSync(captionPath)).trim();
  } catch {
    return { ok: false, errorMessage: `${label} is not valid UTF-8: ${captionPath}` };
  }
  if (!text) return { ok: false, errorMessage: `${label} is empty: ${captionPath}` };
  if ([...text].length > CAPTION_FILE_MAX_CHARS) {
    return { ok: false, errorMessage: `${label} exceeds ${CAPTION_FILE_MAX_CHARS} characters: ${captionPath}` };
  }
  return { ok: true, text };
}

/**
 * Verify a job's declared output artifact after a zero-exit run.
 *
 * A batch agent can exit 0 having written nothing — `lastStatus=ok` then
 * reports success for a run that produced no work. `agent.expectArtifact`
 * closes that gap for jobs whose delivery mode does not already touch the
 * file (`feishu_document` / `telegram_document` verify `documentPath` on the
 * delivery path, so they get this for free).
 *
 * Returns an error message when the artifact is missing/empty/stale, or
 * `undefined` when the artifact is present or no check is configured.
 */
function detectsMissingExpectedArtifact(job: ScheduledJob): string | undefined {
  const expect = job.agent.expectArtifact;
  if (!expect?.path) return undefined;

  const expanded = expandDeliveryTemplate(expect.path, job);
  const resolved = isAbsolute(expanded) ? expanded : pathJoin(job.agent.workspace, expanded);

  if (!existsSync(resolved)) {
    return `expected artifact missing: ${resolved}`;
  }

  const stat = statSync(resolved);
  if (!stat.isFile()) {
    return `expected artifact is not a regular file: ${resolved}`;
  }

  const minBytes = expect.minBytes ?? 1;
  if (stat.size < minBytes) {
    return `expected artifact too small (${stat.size} < ${minBytes} bytes): ${resolved}`;
  }

  // Guards against a stale artifact from a previous week satisfying the check
  // — the exact failure mode where "the file exists" hides "this run wrote
  // nothing".
  if (expect.maxAgeSeconds !== undefined) {
    const ageSeconds = (Date.now() - stat.mtimeMs) / 1000;
    if (ageSeconds > expect.maxAgeSeconds) {
      return `expected artifact is stale (${Math.round(ageSeconds)}s old > ${expect.maxAgeSeconds}s): ${resolved}`;
    }
  }

  if (expect.contains !== undefined) {
    if (!Array.isArray(expect.contains)
      || expect.contains.some(fragment => typeof fragment !== 'string' || !fragment)) {
      return `expected artifact contains contract is invalid: ${resolved}`;
    }
    const content = readFileSync(resolved, 'utf-8');
    const missing = expect.contains.filter(fragment => !content.includes(fragment));
    if (missing.length > 0) {
      return `expected artifact missing required content ${JSON.stringify(missing)}: ${resolved}`;
    }
  }

  return undefined;
}

// POSIX signal numbers for the signals we may see a child die from. Anything
// not listed falls back to SIGTERM's 15, which still yields a 128+n code that
// cannot collide with harness CLI's 0 ("clean") / 1 ("hits found") semantics.
const SIGNAL_NUMBERS: Record<string, number> = {
  SIGHUP: 1, SIGINT: 2, SIGQUIT: 3, SIGABRT: 6, SIGKILL: 9,
  SIGSEGV: 11, SIGTERM: 15,
};

function runCommand(
  bin: string,
  args: string[],
  timeoutMs: number,
  env?: NodeJS.ProcessEnv,
): Promise<{ exitCode: number; stdout: string; stderr: string; timedOut: boolean; signal: NodeJS.Signals | null }> {
  return new Promise(resolveRun => {
    let stdout = '';
    let stderr = '';
    let settled = false;
    let timedOut = false;

    const finish = (exitCode: number, signal: NodeJS.Signals | null = null): void => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      resolveRun({ exitCode, stdout, stderr, timedOut, signal });
    };

    // Gate and verify commands are Relay-owned helpers: they keep the loopback
    // API token (collab-inbox-gate delivers through it), never bot tokens or
    // provider key pools.
    const child = spawn(bin, args, {
      stdio: ['ignore', 'pipe', 'pipe'],
      env: stripRelaySecrets({ ...(env ?? process.env) }, { keepInternalToken: true }),
    });
    const timer = setTimeout(() => {
      timedOut = true;
      child.kill('SIGTERM');
    }, timeoutMs);

    child.stdout?.on('data', (chunk: Buffer) => { stdout += chunk.toString(); });
    child.stderr?.on('data', (chunk: Buffer) => { stderr += chunk.toString(); });
    child.on('error', (err: NodeJS.ErrnoException) => {
      const code = err.code ? ` (${err.code})` : '';
      stderr += `${err.message}${code}`;
      finish(127);
    });
    // A signal-killed child reports code === null (the signal carries the
    // cause). The old `code ?? 1` collapsed that onto exit 1 — which harness
    // CLI uses for "scan found hits" — so a SIGTERM'd scan impersonated a
    // successful scan and was later misreported as "returned hits but no
    // scan_id JSON". Encode signals as the shell-conventional 128+signo so the
    // value can never be mistaken for a real harness exit code.
    child.on('exit', (code, signal) => {
      if (code !== null && code !== undefined) {
        finish(code, signal ?? null);
        return;
      }
      const signo = signal ? SIGNAL_NUMBERS[signal] ?? 15 : 15;
      finish(128 + signo, signal ?? null);
    });
  });
}

const VERIFY_SUMMARY_LIMIT = 500;

/** Keep the start and the end of long stderr: Python tracebacks put the exception last. */
export function clipStderr(text: string, limit = 500): string {
  if (text.length <= limit) return text;
  const head = Math.floor(limit * 0.4);
  const tail = limit - head;
  return `${text.slice(0, head)} … ${text.slice(-tail)}`;
}

/**
 * Run a job's `agent.verify` command after a zero-exit run whose
 * `expectArtifact` passed. The command checks facts the agent claims to have
 * produced (for example a ledger record named in its artifact); Relay only
 * interprets the exit status. Returns an error message, or `undefined` when
 * the command exited 0.
 */
async function detectsFailedPostRunVerification(
  job: ScheduledJob,
  run: { runId: string; startedAt: number; runtime?: string },
): Promise<string | undefined> {
  const verify = job.agent.verify;
  if (!verify) return undefined;
  const timeoutMs = (verify.timeout ?? 60) * 1000;
  const started = new Date(run.startedAt);
  const result = await runCommand(verify.command, verify.args ?? [], timeoutMs, {
    ...process.env,
    RELAY_JOB_ID: job.id,
    RELAY_JOB_RUN_ID: run.runId,
    RELAY_JOB_STARTED_AT: started.toISOString(),
    RELAY_JOB_DATE: formatDateForTimeZone(job.schedule.tz, started),
    RELAY_JOB_WORKSPACE: job.agent.workspace,
    RELAY_JOB_RUNTIME: run.runtime ?? '',
  });
  if (result.timedOut) return `post-run verify timed out after ${timeoutMs}ms`;
  if (result.exitCode === 0) return undefined;
  const summary = (result.stderr.trim() || result.stdout.trim()).slice(-VERIFY_SUMMARY_LIMIT);
  return `post-run verify exited with code ${result.exitCode}${summary ? `: ${summary}` : ''}`;
}

type DeliveryCommandResult = { ok: boolean; skipped?: boolean; errorMessage?: string };

function findDeliverySkipMarker(job: ScheduledJob, output: string): string | undefined {
  // Whole-line match only: a skip marker fires only when it occupies a line by
  // itself (after trim), never as a substring buried in narration. This stops
  // codex/CC runs from self-skipping just by *mentioning* a marker word while
  // explaining their logic. The job must deliberately emit the marker on its
  // own line to declare a no-op.
  const lines = output.split(/\r?\n/).map(line => line.trim());
  return job.delivery.skipIfOutputIncludes?.find(marker => {
    const trimmedMarker = marker.trim();
    if (!trimmedMarker) return false;
    return lines.some(line => line === trimmedMarker);
  });
}

async function deliverTelegramDocument(
  job: ScheduledJob,
  result: JobResult,
  bot: Bot,
): Promise<DeliveryCommandResult> {
  if (job.delivery.mode !== 'telegram_document') return { ok: true };

  const skipMarker = findDeliverySkipMarker(job, result.output);
  if (skipMarker) {
    console.log({ ts: new Date().toISOString(), level: 'info', module: 'scheduler', job: job.id, msg: 'telegram document delivery skipped by output marker', marker: skipMarker });
    return { ok: true, skipped: true };
  }

  if (!job.delivery.to) {
    return { ok: false, errorMessage: 'telegram_document delivery missing delivery.to' };
  }
  if (!job.delivery.documentPath) {
    return { ok: false, errorMessage: 'telegram_document delivery missing documentPath' };
  }

  const resolvedDocument = resolveDeliveryPath(job.delivery.documentPath, job, 'telegram_document documentPath');
  if (!resolvedDocument.ok) return resolvedDocument;
  const documentPath = resolvedDocument.path;
  if (!existsSync(documentPath)) {
    return { ok: false, errorMessage: `telegram_document missing file: ${documentPath}` };
  }
  const captionFile = readDeliveryCaptionFile(job, 'telegram_document');
  if (!captionFile.ok) return captionFile;
  // The reply is checked before anything is sent, so a run without one sends no document either.
  // Only the run's last reply is sent; `output` also joins the text of earlier turns.
  const reply = job.delivery.sendOutput === true ? (result.finalReply ?? '').trim() : undefined;
  if (reply === '') {
    return { ok: false, errorMessage: result.finalReply === undefined
      ? 'telegram_document sendOutput: the runtime did not separate the last reply'
      : 'telegram_document sendOutput: the run left no reply to send' };
  }

  const caption = job.delivery.caption
    ? expandDeliveryTemplate(job.delivery.caption, job)
    : undefined;
  const receipt = await deliverTelegramDocumentRequest({
    id: `delivery:${job.id}:telegram-document:${Date.now()}`,
    correlationId: `job:${job.id}`,
    channel: 'telegram',
    address: { channel: 'telegram', kind: 'telegram_chat', value: job.delivery.to },
    payload: { kind: 'document', path: documentPath, ...(caption ? { caption } : {}) },
  }, bot);

  if (receipt.status !== 'sent') {
    const errorMessage = `telegram_document delivery failed: ${receipt.error ?? 'unknown error'}`;
    console.error({ ts: new Date().toISOString(), level: 'error', module: 'scheduler', job: job.id, msg: 'Telegram document delivery failed', receiptId: receipt.id, error: receipt.error, documentPath });
    return { ok: false, errorMessage };
  }
  console.log({ ts: new Date().toISOString(), level: 'info', module: 'scheduler', job: job.id, msg: 'Delivered document to Telegram', chatId: Number(job.delivery.to), documentPath, receiptId: receipt.id, attempts: receipt.attempts });

  if (captionFile.text !== undefined) {
    const textReceipt = await deliverTelegramText({
      id: `delivery:${job.id}:telegram-document-caption-file:${Date.now()}`,
      correlationId: `job:${job.id}`,
      channel: 'telegram',
      address: { channel: 'telegram', kind: 'telegram_chat', value: job.delivery.to },
      payload: { kind: 'text', text: captionFile.text },
    }, bot);
    if (textReceipt.status !== 'sent') {
      const errorMessage = `telegram_document captionPath delivery failed (document already sent): ${textReceipt.error ?? 'unknown error'}`;
      console.error({ ts: new Date().toISOString(), level: 'error', module: 'scheduler', job: job.id, msg: 'Telegram captionPath delivery failed', receiptId: textReceipt.id, error: textReceipt.error });
      return { ok: false, errorMessage };
    }
  }
  if (reply !== undefined) {
    // Plain text, cut at Telegram's limit like every scheduled text.
    const replyReceipt = await deliverTelegramText({
      id: `delivery:${job.id}:telegram-document-reply:${Date.now()}`,
      correlationId: `job:${job.id}`,
      channel: 'telegram',
      address: { channel: 'telegram', kind: 'telegram_chat', value: job.delivery.to },
      payload: { kind: 'text', text: reply },
    }, bot);
    if (replyReceipt.status !== 'sent') {
      const errorMessage = `telegram_document reply delivery failed (document already sent): ${replyReceipt.error ?? 'unknown error'}`;
      console.error({ ts: new Date().toISOString(), level: 'error', module: 'scheduler', job: job.id, msg: 'Telegram document reply delivery failed', receiptId: replyReceipt.id, error: replyReceipt.error });
      return { ok: false, errorMessage };
    }
    console.log({ ts: new Date().toISOString(), level: 'info', module: 'scheduler', job: job.id, msg: 'Delivered reply to Telegram after document', chatId: Number(job.delivery.to), receiptId: replyReceipt.id, attempts: replyReceipt.attempts });
  }
  return { ok: true };
}

async function deliverFeishuDocument(
  job: ScheduledJob,
  result: JobResult,
  client: FeishuMessageClient,
): Promise<DeliveryCommandResult> {
  if (job.delivery.mode !== 'feishu_document') return { ok: true };

  const skipMarker = findDeliverySkipMarker(job, result.output);
  if (skipMarker) {
    console.log({ ts: new Date().toISOString(), level: 'info', module: 'scheduler', job: job.id, msg: 'feishu document delivery skipped by output marker', marker: skipMarker });
    return { ok: true, skipped: true };
  }

  if (!job.delivery.feishuOpenId) {
    return { ok: false, errorMessage: 'feishu_document delivery missing delivery.feishuOpenId' };
  }
  if (!job.delivery.documentPath) {
    return { ok: false, errorMessage: 'feishu_document delivery missing documentPath' };
  }

  const resolvedDocument = resolveDeliveryPath(job.delivery.documentPath, job, 'feishu_document documentPath');
  if (!resolvedDocument.ok) return resolvedDocument;
  const documentPath = resolvedDocument.path;
  if (!existsSync(documentPath)) {
    return { ok: false, errorMessage: `feishu_document missing file: ${documentPath}` };
  }
  const stat = statSync(documentPath);
  if (!stat.isFile() || stat.size === 0) {
    return { ok: false, errorMessage: `feishu_document empty or non-regular file: ${documentPath}` };
  }
  const captionFile = readDeliveryCaptionFile(job, 'feishu_document');
  if (!captionFile.ok) return captionFile;

  const openId = job.delivery.feishuOpenId;
  const receipt = await deliverFeishuFileRequest({
    id: `delivery:${job.id}:feishu-document:${Date.now()}`,
    correlationId: `job:${job.id}`,
    channel: 'feishu',
    address: { channel: 'feishu', kind: 'feishu_open_id', value: openId },
    payload: { kind: 'document', path: documentPath },
  }, client);

  if (receipt.status !== 'sent') {
    const errorMessage = `feishu_document delivery failed: ${receipt.error ?? 'unknown error'}`;
    console.error({ ts: new Date().toISOString(), level: 'error', module: 'scheduler', job: job.id, msg: 'Feishu document delivery failed', receiptId: receipt.id, error: receipt.error, documentPath });
    return { ok: false, errorMessage };
  }

  // Optional caption — sent as a separate Feishu text message (file msg carries no caption).
  if (job.delivery.caption) {
    const caption = expandDeliveryTemplate(job.delivery.caption, job);
    const captionReceipt = await deliverFeishuTextRequest({
      id: `delivery:${job.id}:feishu-document-caption:${Date.now()}`,
      correlationId: `job:${job.id}`,
      channel: 'feishu',
      address: { channel: 'feishu', kind: 'feishu_open_id', value: openId },
      payload: { kind: 'text', text: caption },
    }, client);
    if (captionReceipt.status !== 'sent') {
      // Caption is best-effort; the file already landed, so don't fail the whole delivery.
      console.warn({ ts: new Date().toISOString(), level: 'warn', module: 'scheduler', job: job.id, msg: 'Feishu document caption delivery failed (file already sent)', error: captionReceipt.error });
    }
  }

  if (captionFile.text !== undefined) {
    const textReceipt = await deliverFeishuTextRequest({
      id: `delivery:${job.id}:feishu-document-caption-file:${Date.now()}`,
      correlationId: `job:${job.id}`,
      channel: 'feishu',
      address: { channel: 'feishu', kind: 'feishu_open_id', value: openId },
      payload: { kind: 'text', text: captionFile.text },
    }, client);
    if (textReceipt.status !== 'sent') {
      const errorMessage = `feishu_document captionPath delivery failed (document already sent): ${textReceipt.error ?? 'unknown error'}`;
      console.error({ ts: new Date().toISOString(), level: 'error', module: 'scheduler', job: job.id, msg: 'Feishu captionPath delivery failed', receiptId: textReceipt.id, error: textReceipt.error, documentPath });
      return { ok: false, errorMessage };
    }
  }

  console.log({ ts: new Date().toISOString(), level: 'info', module: 'scheduler', job: job.id, msg: 'Delivered document to Feishu', openId, documentPath, receiptId: receipt.id });
  return { ok: true };
}

/** A convergent job alerts only after this many consecutive failed runs... */
export const PERSISTENT_FAILURE_RUNS = 3;
/** ...spanning at least this long since the streak's first failure. */
export const PERSISTENT_FAILURE_MS = 30 * 60 * 1000;

/** Kept for callers/tests compiled against the old cooldown implementation. */
export function resetJobFailureAlertStreaks(): void {
  // Run-level terminal reservations supersede process-local per-job streaks.
}

async function maybeAlertJobFailureFeishu(
  job: ScheduledJob,
  result: JobResult,
  feishuClient: FeishuMessageClient | null,
  terminalRegistry: JobTerminalRegistry,
  runId: string,
  holder: () => ScheduledJob = () => job,
): Promise<void> {
  const openId = job.delivery.feishuOpenId;
  if (!openId) return;
  if (result.status === 'ok') return;
  // A slot waiting for its subscription limit to reset is not failed yet; its retry alerts if it fails.
  if (result.deferred) return;
  if (job.convergent === true) {
    // A convergent job's next success settles this failure, so a failure that
    // recovers by itself must not take a human's attention. Alert once, only
    // when the streak has neither recovered nor stopped repeating.
    // The state already counts this run: it is recorded before the alert.
    const state = holder()._state ?? { consecutiveErrors: 0 };
    const failures = state.consecutiveErrors;
    const since = Date.parse(state.failingSince ?? '');
    const lastingMs = Number.isFinite(since) ? Date.now() - since : 0;
    if (state.failureAlerted || failures < PERSISTENT_FAILURE_RUNS || lastingMs < PERSISTENT_FAILURE_MS) {
      console.log({
        ts: new Date().toISOString(),
        level: 'info',
        module: 'scheduler',
        job: job.id,
        runId,
        msg: 'job failure alert withheld: convergent job may still recover',
        failures,
        lastingMs,
        alreadyAlerted: state.failureAlerted === true,
      });
      return;
    }
  }

  const reservation = terminalRegistry.reserve(runId, job.owner);
  if (!reservation.deliver) {
    if (reservation.deduplicated) {
      console.log({
        ts: new Date().toISOString(),
        level: 'info',
        module: 'scheduler',
        job: job.id,
        runId,
        msg: 'job failure alert suppressed: run terminal already sent or in flight',
        terminalStatus: reservation.run?.status,
        terminalReceiptIds: reservation.run?.receipts.map(receipt => receipt.externalMessageId ?? null),
      });
    }
    return;
  }

  if (!feishuClient) {
    terminalRegistry.complete(runId, [{
      channel: 'feishu',
      status: 'failed',
      error: `no Feishu client for owner ${job.owner}`,
    }]);
    console.error({
      ts: new Date().toISOString(),
      level: 'error',
      module: 'scheduler',
      job: job.id,
      runId,
      msg: 'job failure alert unconfigured: feishuOpenId set but no Feishu client for owner',
      owner: job.owner,
    });
    return;
  }

  const now = Date.now();
  const text = [
    '⚠️ cron job 失败',
    `job: ${job.name} (${job.id})`,
    `run: ${runId}`,
    `错误: ${result.errorMessage ?? 'unknown'}`,
    '（Relay scheduler run-level fallback）',
  ].join('\n');
  const receipt = await deliverFeishuTextRequest({
    id: `delivery:${job.id}:${runId}:job-failure-alert:${now}`,
    correlationId: `job:${job.id}:run:${runId}`,
    channel: 'feishu',
    address: { channel: 'feishu', kind: 'feishu_open_id', value: openId },
    payload: { kind: 'text', text },
  }, feishuClient);
  terminalRegistry.complete(runId, [{
    channel: 'feishu',
    status: receipt.status === 'sent' ? 'sent' : 'failed',
    attempts: receipt.attempts,
    ...(receipt.externalMessageId ? { externalMessageId: receipt.externalMessageId } : {}),
    ...(receipt.error ? { error: receipt.error } : {}),
  }]);
  if (receipt.status === 'sent') {
    // Only a delivered alert ends this streak's alerting; a failed send retries next run.
    if (job.convergent === true) {
      const current = holder();
      current._state = { ...(current._state ?? { consecutiveErrors: 0 }), failureAlerted: true };
    }
    console.log({
      ts: new Date().toISOString(),
      level: 'info',
      module: 'scheduler',
      job: job.id,
      runId,
      msg: 'job failure alert sent',
      openId,
      receiptId: receipt.externalMessageId,
    });
  } else {
    console.error({
      ts: new Date().toISOString(),
      level: 'error',
      module: 'scheduler',
      job: job.id,
      runId,
      msg: 'job failure alert delivery failed',
      error: receipt.error,
    });
  }
}

import { requireAutomatic, AutomaticExecutionPaused } from './automation-control.js';

/**
 * Start the opt-in collab-inbox progress notice for a positive gate batch.
 * Feishu goes to `delivery.feishuOpenId`, Telegram to `delivery.to`; no
 * target, no client, or a gate output that is not a non-empty collab batch
 * sends nothing.
 */
function startCollabProgressNotice(
  job: ScheduledJob,
  gateOutput: string,
  bot: Bot | null,
  feishuClient: FeishuMessageClient | null,
  displayName?: AgentDisplayName,
): ProgressNotice | undefined {
  const contracts = parseCollabBatch(gateOutput);
  if (!contracts) {
    console.warn({ ts: new Date().toISOString(), level: 'warn', module: 'scheduler', job: job.id,
      msg: 'progressNotice set but gate output is not a non-empty collab inbox batch; no notice sent' });
    return undefined;
  }
  const openId = job.delivery.feishuOpenId;
  const chatId = job.delivery.channel === 'feishu' ? undefined : job.delivery.to;
  return startProgressNotice(job, contracts, {
    ...(openId && feishuClient ? { feishu: { client: feishuClient, openId } } : {}),
    ...(chatId && bot ? { telegram: { bot, chatId } } : {}),
  }, undefined, displayName);
}

/** An Agent-managed job reads only a file really inside its workspace, whatever
 * a link inside the workspace points to; other jobs read the configured path. */
export function readInjectFile(job: ScheduledJob, injectPath: string): string {
  if (job.managedBy !== 'agent') return readFileSync(injectPath, 'utf-8');
  const real = realpathSync(injectPath);
  const inside = relative(realpathSync(job.agent.workspace), real);
  if (!inside || inside.startsWith('..') || isAbsolute(inside)) throw new Error('inject_file resolves outside the workspace');
  return readFileSync(real, 'utf-8');
}

/** Gate markers of a wake job (`collab-inbox-gate.py wake`). */
export const WAKE_DELIVERED = 'WAKE_DELIVERED';
export const WAKE_NOT_DELIVERED = 'WAKE_NOT_DELIVERED';

/**
 * The end of a wake run: the gate either handed the prompt to the holding
 * conversation, or found none here, in which case the owner's human gets one
 * reminder through the job's delivery target. Neither starts a batch session.
 */
async function finishWake(job: ScheduledJob, runId: string, output: string, durationMs: number,
  bot: Bot | null, feishuClient: FeishuMessageClient | null): Promise<JobResult> {
  const base = { jobId: job.id, runId, status: 'ok' as const, executionStatus: 'ok' as const, output, durationMs, skipped: true };
  if (output.startsWith(WAKE_DELIVERED)) return { ...base, deliveryStatus: 'skipped', reasonCode: 'wake_delivered' };
  if (!output.startsWith(WAKE_NOT_DELIVERED)) throw new Error(`wake gate marker missing: ${output.slice(0, 200)}`);
  const reason = output.slice(WAKE_NOT_DELIVERED.length).trim() || 'not_hosted';
  const text = `⏰ 定时唤醒未送达：${job.name}（${job.id}）要唤醒 WI ${job.wake!.workItemId} 的会话，但这台 Relay 上没有会话握着它（${reason}）。请在对话里继续这件 WI，或用 /wi continue ${job.wake!.workItemId} 接回。`;
  const at = Date.now();
  let sent = false;
  if (job.delivery.feishuOpenId && feishuClient) {
    sent = (await deliverFeishuTextRequest({ id: `delivery:${job.id}:wake:${at}`, correlationId: `job:${job.id}`, channel: 'feishu',
      address: { channel: 'feishu', kind: 'feishu_open_id', value: job.delivery.feishuOpenId },
      payload: { kind: 'text', text } }, feishuClient)).status === 'sent' || sent;
  }
  if (job.delivery.to && job.delivery.channel !== 'feishu' && bot) {
    sent = (await deliverTelegramText({ id: `delivery:${job.id}:wake:${at}`, correlationId: `job:${job.id}`, channel: 'telegram',
      address: { channel: 'telegram', kind: 'telegram_chat', value: job.delivery.to },
      payload: { kind: 'text', text } }, bot)).status === 'sent' || sent;
  }
  console.log({ ts: new Date().toISOString(), level: 'warn', module: 'scheduler', job: job.id, msg: 'wake not delivered', reason, reminded: sent });
  return { ...base, deliveryStatus: sent ? 'sent' : 'failed', reasonCode: 'wake_not_delivered' };
}

// --- executeJob ---
/** One run of a job whose claim the caller holds and releases. */
async function executeJob(
  job: ScheduledJob,
  bot: Bot | null,
  runWithRuntime: ReturnType<typeof createBatchRuntimeOrchestrator>['run'],
  deliveryEnabled = true,
  feishuClient: FeishuMessageClient | null = null,
  triggerSource: JobTriggerSource = 'manual',
  terminalRegistry: JobTerminalRegistry = new JobTerminalRegistry(),
  automaticGuard?: () => void,
  displayName?: AgentDisplayName,
  stateHolder: () => ScheduledJob = () => job,
  settle: (result: JobResult) => JobResult = (result) => result,
): Promise<JobResult> {
  const terminalRun = terminalRegistry.startRun(job.id, job.owner);
  const runId = terminalRun.runId;
  const startedAt = Date.now();
  let progressNotice: ProgressNotice | undefined;
  console.log({ ts: new Date().toISOString(), level: 'info', module: 'scheduler', job: job.id, runId, msg: 'job started' });

  try {
    automaticGuard?.();
    const timeoutMs = (job.agent.timeout ?? 3600) * 1000;

    // inject_file: dynamically read file content and prepend to prompt
    let prompt = job.agent.prompt;
    if (job.agent.inject_file) {
      const injectPath = isAbsolute(job.agent.inject_file)
        ? job.agent.inject_file
        : resolve(job.agent.workspace, job.agent.inject_file);
      try {
        const injected = readInjectFile(job, injectPath);
        prompt = `${injected}\n\n${prompt}`;
      } catch (err) {
        console.warn({
          ts: new Date().toISOString(),
          level: 'warn',
          module: 'scheduler',
          job: job.id,
          msg: 'inject_file not readable, degrading to pure prompt',
          path: injectPath,
          error: String(err),
        });
      }
    }

    // Trigger-source marker: prepend `trigger=cron` at the very front of the
    // prompt so cron/reconcile-launched skills (e.g. amanda autonomous-review
    // step 0) can detect unattended auto-trigger. reconcile is a missed-window
    // catch-up run — still unattended — so it is marked as cron too, as is an
    // event run (triggerJob). manual/CLI runs add no marker, preserving the
    // skill's fallback-to-manual semantics.
    if (triggerSource !== 'manual') {
      prompt = `trigger=cron\n\n${prompt}`;
    }

    if (job.agent.gate) {
      const gate = job.agent.gate;
      const gateTimeoutMs = (gate.timeout ?? 30) * 1000;
      const skipExitCode = gate.skipExitCode ?? 3;
      const gateResult = await runCommand(gate.command, gate.args ?? [], gateTimeoutMs);
      if (gateResult.timedOut) {
        throw new Error(`pre-LLM gate timed out after ${gateTimeoutMs}ms`);
      }
      if (gateResult.exitCode === skipExitCode) {
        const durationMs = Date.now() - startedAt;
        const output = gateResult.stdout.trim();
        if (!output) {
          throw new Error('pre-LLM gate returned skip code without evidence marker');
        }
        if (gate.skipOutput !== undefined && output !== gate.skipOutput.trim()) {
          throw new Error(`pre-LLM gate skip marker mismatch: ${output.slice(0, 200)}`);
        }
        console.log({
          ts: new Date().toISOString(),
          level: 'info',
          module: 'scheduler',
          job: job.id,
          msg: 'job finished',
          status: 'ok',
          executionStatus: 'ok',
          deliveryStatus: 'skipped',
          gateStatus: 'skip',
          durationMs,
          runtime: 'none',
        });
        if (job.wake) return await finishWake(job, runId, output, durationMs, bot, feishuClient);
        return {
          jobId: job.id,
          runId,
          status: 'ok',
          executionStatus: 'ok',
          deliveryStatus: 'skipped',
          output,
          durationMs,
          skipped: true,
          reasonCode: 'gate_skip',
        };
      }
      if (gateResult.exitCode !== 0) {
        const detail = `exited with code ${gateResult.exitCode}`;
        // Some gates report their rejection on stdout; never alert with an empty reason.
        const reason = gateResult.stderr.trim() || gateResult.stdout.trim();
        throw new Error(`pre-LLM gate ${detail}: ${clipStderr(reason)}`);
      }
      if (job.wake) {
        // A wake gate delivers or reminds and always skips; a model run would be a batch session.
        throw new Error('wake gate exited 0; a wake job never starts a batch session');
      }
      const gateOutput = gateResult.stdout.trim();
      if (!gateOutput) {
        throw new Error('pre-LLM gate exited 0 without evidence payload');
      }
      prompt = `【机械 gate 输出；只处理这些新增事件】\n${gateOutput}\n\n${prompt}`;
      if (deliveryEnabled && job.delivery.progressNotice === true) {
        progressNotice = startCollabProgressNotice(job, gateOutput, bot, feishuClient, displayName);
      }
    }

    // The owner Agent's standing grants open to its scheduled runs as to its
    // conversations; every batch runtime reads the same prompt lines.
    const standing = await standingAccessFor(job.owner);
    if (standing.length > 0) {
      prompt = `【本 Agent 的常驻只读授权】\n${standing.map((row) => standingAccessPromptLine(row.path)).join('\n')}\n\n${prompt}`;
    }
    // Scheduled runs read the shared repositories as conversations do.
    const shared = sharedAccessPromptLines(job.agent.workspace, standing.map((row) => row.path));
    if (shared.length > 0) prompt = `【共享仓库与规则正本】\n${shared.join('\n')}\n\n${prompt}`;

    const computeProfile = resolveComputeProfile(job.agent.computeProfile);
    console.log({
      ts: new Date().toISOString(),
      level: 'info',
      msg: 'Compute profile aliases resolved',
      jobId: job.id,
      computeProfile: job.agent.computeProfile ?? 'platform_default',
      ccModelAlias: computeProfile.cc.modelAlias,
      ccModel: computeProfile.cc.model,
      ccReasoningEffort: computeProfile.cc.reasoningEffort,
      codexModelAlias: computeProfile.codex.modelAlias,
      codexModel: computeProfile.codex.model,
      codexReasoningEffort: computeProfile.codex.reasoningEffort,
    });

    const runtimeResult = await runWithRuntime(
      {
        workspace: job.agent.workspace,
        prompt,
        timeoutMs,
        beforeAttempt: automaticGuard,
        env: {
          RELAY_JOB_RUN_ID: runId,
          RELAY_JOB_TERMINAL_CONTRACT: 'v1',
          // Propagate the centrally resolved named profile for nested Duke
          // adjudications; the nested entrypoint resolves it again from the
          // catalog and fails closed if it cannot do so.
          PULSE_PARENT_COMPUTE_PROFILE: job.agent.computeProfile ?? 'platform_default',
        },
        nativeSession: { jobId: job.id, owner: job.owner, runId, jobName: job.name,
          startedAt: new Date(startedAt).toISOString(), ...(job.wi ? { wi: job.wi } : {}) },
        maxTurns: job.agent.maxTurns,
        codexYolo: job.agent.codexYolo === true,
        codexModel: computeProfile.codex.model,
        codexReasoningEffort: computeProfile.codex.reasoningEffort,
        ccModel: computeProfile.cc.model,
        ccReasoningEffort: computeProfile.cc.reasoningEffort,
      },
      failoverRuntimes(job.agent.runtimePreference, job.runtimeStrict === true),
    );
    const { stdout, exitCode, runtime, attemptedRuntimes, timedOut, stderr } = runtimeResult;

    const durationMs = Date.now() - startedAt;
    let status: JobResult['status'];
    let executionStatus: JobResult['executionStatus'];
    let errorMessage: string | undefined;

    // Artifact check runs for every delivery mode: a job can declare an output
    // file it must produce regardless of how (or whether) the result is
    // delivered. The notification-failure sniff stays scoped to mode 'none',
    // where no delivery path would otherwise catch it.
    let postconditionError = exitCode === 0
      ? (detectsMissingExpectedArtifact(job)
        ?? (job.delivery.mode === 'none'
          ? detectsRequiredNotificationFailure(prompt, stdout, stderr)
          : undefined))
      : undefined;
    // The verify command runs last: it may rely on the declared artifact.
    // Its own outcome is kept so readers need not infer it from executionStatus.
    let verifyStatus: JobResult['verifyStatus'];
    if (exitCode === 0 && !postconditionError && job.agent.verify) {
      postconditionError = await detectsFailedPostRunVerification(job, { runId, startedAt, runtime });
      verifyStatus = postconditionError ? 'failed' : 'passed';
    }

    if (postconditionError) {
      status = 'error';
      executionStatus = 'error';
      errorMessage = postconditionError;
    } else if (exitCode === 0) {
      status = 'ok';
      executionStatus = 'ok';
    } else if (timedOut || (exitCode === 1 && stdout === '' && durationMs >= timeoutMs - 1000)) {
      status = 'timeout';
      executionStatus = 'timeout';
      errorMessage = `${runtime ?? 'runtime'} timeout after ${durationMs}ms${runtimeResult.failureReason ? `: ${runtimeResult.failureReason}` : ''}`;
    } else {
      status = 'error';
      executionStatus = 'error';
      errorMessage = runtimeResult.spawnErrorCode
        ? `${runtime ?? 'runtime'} unavailable (${runtimeResult.spawnErrorCode})`
        : `${runtime ?? 'runtime'} exited with code ${exitCode}${runtimeResult.failureReason ? `: ${runtimeResult.failureReason}` : ''}`;
    }

    if ((attemptedRuntimes?.length ?? 0) > 1) {
      console.warn({
        ts: new Date().toISOString(),
        level: 'warn',
        module: 'scheduler',
        job: job.id,
        msg: 'runtime fallback triggered',
        attemptedRuntimes,
        selectedRuntime: runtime,
      });
    } else if (exitCode !== 0 && stderr) {
      console.error({
        ts: new Date().toISOString(),
        level: 'error',
        module: 'scheduler',
        job: job.id,
        msg: 'batch runtime non-zero exit',
        runtime,
        exitCode,
        stderr: clipStderr(stderr),
        stdout: stdout.slice(0, 500),
      });
    }

    let deliveryStatus: DeliveryReceipt['status'] = executionStatus === 'ok'
      ? deliveryStatusForMode(job.delivery.mode)
      : 'skipped';
    let deliveryReceipt: DeliveryReceipt | undefined;

    const quotaResetAt = status === 'ok' ? undefined
      : earliestResetAt([
        ...(runtimeResult.failedAttempts ?? []).map((attempt) => ({ output: attempt.output, endedAt: new Date(attempt.endedAt) })),
        { output: `${stdout}\n${stderr ?? ''}`, endedAt: new Date(startedAt + durationMs) },
      ])?.toISOString();
    let result: JobResult = {
      jobId: job.id,
      runId,
      status,
      executionStatus,
      deliveryStatus,
      output: stdout,
      ...(runtimeResult.finalText !== undefined ? { finalReply: runtimeResult.finalText } : {}),
      durationMs,
      runtime,
      attemptedRuntimes,
      errorMessage,
      ...(verifyStatus ? { verifyStatus } : {}),
      ...(quotaResetAt ? { quotaResetAt } : {}),
    };

    if (executionStatus === 'ok' && job.delivery.mode === 'telegram_document') {
      if (deliveryEnabled && bot) {
        const delivery = await deliverTelegramDocument(job, result, bot);
        if (!delivery.ok) {
          deliveryStatus = 'failed';
          status = 'error';
          errorMessage = delivery.errorMessage;
          result = { ...result, status, executionStatus, deliveryStatus, errorMessage };
        } else if (delivery.skipped) {
          deliveryStatus = 'skipped';
          result = { ...result, deliveryStatus };
        } else {
          deliveryStatus = 'sent';
          result = { ...result, deliveryStatus };
        }
      } else if (deliveryEnabled && !bot) {
        deliveryStatus = 'failed';
        status = 'error';
        errorMessage = `telegram_document delivery failed: no Telegram client mapped for owner ${job.owner}`;
        result = { ...result, status, executionStatus, deliveryStatus, errorMessage };
        console.error({
          ts: new Date().toISOString(),
          level: 'error',
          module: 'scheduler',
          job: job.id,
          msg: errorMessage,
        });
      } else {
        deliveryStatus = 'skipped';
        result = { ...result, deliveryStatus };
        console.log({
          ts: new Date().toISOString(),
          level: 'info',
          module: 'scheduler',
          job: job.id,
          msg: 'telegram document delivery skipped by caller',
        });
      }
    }

    if (executionStatus === 'ok' && job.delivery.mode === 'feishu_document') {
      if (deliveryEnabled && feishuClient) {
        const delivery = await deliverFeishuDocument(job, result, feishuClient);
        if (!delivery.ok) {
          deliveryStatus = 'failed';
          status = 'error';
          errorMessage = delivery.errorMessage;
          result = { ...result, status, executionStatus, deliveryStatus, errorMessage };
        } else if (delivery.skipped) {
          deliveryStatus = 'skipped';
          result = { ...result, deliveryStatus };
        } else {
          deliveryStatus = 'sent';
          result = { ...result, deliveryStatus };
        }
      } else if (deliveryEnabled && !feishuClient) {
        deliveryStatus = 'failed';
        status = 'error';
        errorMessage = `feishu_document delivery failed: no Feishu client for owner ${job.owner}`;
        result = { ...result, status, executionStatus, deliveryStatus, errorMessage };
        console.error({
          ts: new Date().toISOString(),
          level: 'error',
          module: 'scheduler',
          job: job.id,
          owner: job.owner,
          msg: 'feishu_document delivery has no Feishu client for owner',
        });
      } else {
        deliveryStatus = 'skipped';
        result = { ...result, deliveryStatus };
        console.log({
          ts: new Date().toISOString(),
          level: 'info',
          module: 'scheduler',
          job: job.id,
          msg: 'feishu document delivery skipped by caller',
        });
      }
    }

    const announceSkipMarker = executionStatus === 'ok' && job.delivery.mode === 'announce'
      ? findDeliverySkipMarker(job, result.output)
      : undefined;
    if (announceSkipMarker) {
      deliveryStatus = 'skipped';
      result = { ...result, deliveryStatus };
      console.log({ ts: new Date().toISOString(), level: 'info', module: 'scheduler', job: job.id, msg: 'announce delivery skipped by output marker', marker: announceSkipMarker });
    } else if (executionStatus === 'ok' && job.delivery.mode === 'announce') {
      if (deliveryEnabled && bot) {
        deliveryReceipt = await deliverResult(job, result, bot);
        if (deliveryReceipt?.status === 'sent') {
          deliveryStatus = 'sent';
          result = { ...result, deliveryStatus, deliveryReceipt };
        } else {
          deliveryStatus = 'failed';
          status = 'error';
          errorMessage = deliveryReceipt?.error ?? 'announce delivery failed';
          result = { ...result, status, executionStatus, deliveryStatus, deliveryReceipt, errorMessage };
        }
      } else if (deliveryEnabled && !bot) {
        deliveryStatus = 'failed';
        status = 'error';
        errorMessage = `announce delivery failed: no explicit delivery bot for owner ${job.owner}`;
        result = { ...result, status, executionStatus, deliveryStatus, errorMessage };
        console.error({
          ts: new Date().toISOString(),
          level: 'error',
          module: 'scheduler',
          job: job.id,
          owner: job.owner,
          msg: 'announce delivery has no explicit owner bot',
        });
      } else {
        deliveryStatus = 'skipped';
        result = { ...result, deliveryStatus };
        if (!deliveryEnabled) {
          console.log({
            ts: new Date().toISOString(),
            level: 'info',
            module: 'scheduler',
            job: job.id,
            msg: 'delivery skipped by caller',
          });
        }
      }
    }

    const logEntry: Record<string, unknown> = {
      ts: new Date().toISOString(),
      level: 'info',
      module: 'scheduler',
      job: job.id,
      runId,
      msg: 'job finished',
      status,
      executionStatus,
      deliveryStatus,
      durationMs,
      runtime: runtime ?? 'unknown',
      attemptedRuntimes,
    };
    if (status !== 'ok') {
      logEntry.exitCode = runtimeResult.exitCode;
      if (errorMessage) logEntry.errorMessage = errorMessage;
      const stderrHead = clipStderr(runtimeResult.stderr ?? '');
      if (stderrHead) logEntry.stderrHead = stderrHead;
    }
    // The run's state, its retry and whether it counts as failed are decided in
    // one step, before anything is awaited: what the notice and the alert say
    // is what the durable state records.
    result = settle(result);
    if (result.deferred) {
      // Limited, not failed: the slot runs again after the reset, and that run settles it.
      logEntry.deferred = true;
      logEntry.deferredReason = result.deferredReason;
    }
    console.log(logEntry);

    await progressNotice?.finish(result.deferred ? 'deferred' : result.status, result.durationMs);
    await maybeAlertJobFailureFeishu(job, result, feishuClient, terminalRegistry, runId, stateHolder);

    return result;
  } catch (err) {
    if (err instanceof AutomaticExecutionPaused) {
      await progressNotice?.finish('paused', Date.now() - startedAt);
      return {jobId:job.id, runId, status:'ok', executionStatus:'ok', deliveryStatus:'skipped',
        skipped:true, reasonCode:'automatic_paused', output:'Automatic AI execution paused',
        durationMs:Date.now()-startedAt, attemptedRuntimes:[]};
    }
    console.error({ ts: new Date().toISOString(), level: 'error', module: 'scheduler', job: job.id, runId, msg: 'executeJob unexpected error', error: String(err) });
    const durationMs = Date.now() - startedAt;
    const crashResult = settle({ jobId: job.id, runId, status: 'error', executionStatus: 'error', deliveryStatus: 'skipped', output: '', durationMs, errorMessage: String(err) });
    await progressNotice?.finish('error', durationMs);
    // crash-type failures (spawn error / unexpected throw) bypass the normal
    // finish path above, so alert here too — this is exactly the "skill never
    // started" case the opt-in Feishu alert is meant to catch. Guarded so an
    // alert failure can't mask the original error.
    try {
      await maybeAlertJobFailureFeishu(job, crashResult, feishuClient, terminalRegistry, runId, stateHolder);
    } catch (alertErr) {
      console.error({ ts: new Date().toISOString(), level: 'error', module: 'scheduler', job: job.id, runId, msg: 'job failure alert threw during crash handling', error: String(alertErr) });
    }
    return crashResult;
  } finally {
    terminalRegistry.finishRun(runId);
  }
}

// wi_20260421_634 fix: per-job bot resolution so owner=amanda pushes via
// @example_amanda_bot instead of whichever agent bot happened to be first in
// agentBotList. The resolver returns null when owner has no dedicated bot
// (e.g. ``system``-owned system jobs like blood-pressure reminders) — in
// that case we fall back to the original single-bot behaviour.
export interface SchedulerOptions {
  getBotByAgentId?: (agentId: string) => Bot | null;
  /** Resolve a Feishu send client for a job owner (feishu_document delivery). */
  getFeishuClientByAgentId?: (agentId: string) => FeishuMessageClient | null;
  /** Enables node-local filtering and makes runNode/owner placement mandatory. */
  currentNode?: string;
  /** Logical owner validation; checked against each job's declared runNode. */
  ownerCanRunOnNode?: (ownerAgentId: string, nodeId: string) => boolean;
  /** Explicit logical owner -> bot identity mapping. Equal names are never assumed. */
  deliveryBotIdForOwner?: (ownerAgentId: string) => string | undefined;
  /** false for one-shot/manual runners that must not register cron callbacks */
  registerCronTasks?: boolean;
  /** false for CLI/manual runs that should reuse execution semantics without TG delivery */
  deliveryEnabled?: boolean;
  /** false for controlled tests/manual runners that must not auto-catch missed scheduled runs */
  reconcileMissedRuns?: boolean;
  /** How often Relay checks for node-cron-silent missed runs. */
  missedRunReconcileIntervalMs?: number;
  /** How far back Relay looks for a single latest missed scheduled run per job. */
  missedRunLookbackMs?: number;
  /** Shared with /internal/push so business and scheduler terminal paths deduplicate per run. */
  terminalRegistry?: JobTerminalRegistry;
  /** False while a deployment maintenance barrier holds new scheduled runs. */
  admitScheduledRun?: () => boolean;
  /** Display name of a Harness actor for collaboration progress notices. */
  agentDisplayName?: AgentDisplayName;
  /** Whether the owner Agent has `ccWriteBoundary`: its CC runs write only inside the workspace. */
  ccWriteBoundaryFor?: (owner: string) => boolean;
  /** The owner Agent's `commitRepos` and `allowedDomains` for its bounded CC runs. */
  ccRepoAccessFor?: (owner: string) => import('./cc-write-boundary.js').CcRepoAccess | undefined;
  /** Whether the owner Agent runs as the Relay host account (agent-isolation.ts); absent is unknown. */
  ownerIsolationFor?: (owner: string) => import('./agent-isolation.js').OwnerIsolation;
  /** The session WI MCP Relay's CC conversations get; every scheduled CC run gets it too. */
  ccSessionWiMcp?: import('./cc-session-wi-mcp.js').CcSessionWiMcp;
  /**
   * Scheduler-instance-wide FIFO concurrency ceiling. When omitted, resolves
   * from RELAY_SCHEDULER_MAX_CONCURRENT_JOBS; an unset env keeps legacy
   * unbounded cross-workspace behavior for an explicit production rollout.
   */
  maxConcurrentJobs?: number;
  runtimePolicy?: {
    primary?: RuntimeId;
    fallbacks?: RuntimeId[];
    cooldownMs?: number;
  };
}

export function prepareNodePlacedJob(
  input: Omit<ScheduledJob, '_state'>,
  currentNode: string,
  ownerCanRunOnNode: (ownerAgentId: string, nodeId: string) => boolean,
): Omit<ScheduledJob, '_state'> {
  const job = { ...input, runNode: input.runNode ?? currentNode };
  if (job.runNode !== currentNode) throw new Error(`New job must run on current node "${currentNode}"`);
  if (!ownerCanRunOnNode(job.owner, currentNode)) {
    throw new Error(`Job owner "${job.owner}" cannot run on current node "${currentNode}"`);
  }
  return job;
}

/**
 * Resolve the delivery bot for a job owner. Exported for unit testing:
 *  - If ``getByAgentId(owner)`` returns a Bot, use it.
 *  - Otherwise fall back to ``fallback`` and warn (owner = ``unknown`` if
 *    no job is supplied; used mainly via {@link createScheduler}).
 */
export function resolveBotForOwner(
  owner: string,
  getByAgentId: (agentId: string) => Bot | null,
  fallback: Bot,
): { bot: Bot; source: 'resolved' | 'fallback' } {
  const resolved = getByAgentId(owner);
  if (resolved) return { bot: resolved, source: 'resolved' };
  return { bot: fallback, source: 'fallback' };
}

// --- createScheduler ---
export function createScheduler(
  fallbackBot: Bot,
  configPath: string,
  options: SchedulerOptions = {},
): Scheduler {
  const taskMap = new Map<string, ScheduledTask>();
  // Active claims per job, at most the job's `maxConcurrent` (default 1).
  const runningMap = new Map<string, number>();
  // Observations of the job's active runs in start order; `currentRun` is the latest.
  const liveRuns = new Map<string, JobRunObservation[]>();
  const registeredAtMap = new Map<string, Date>();
  // Workspace-level serialization: chains jobs targeting the same workspace via
  // a Promise queue so they never run concurrently (CC session lock contention).
  const workspaceQueue = new Map<string, Promise<void>>();
  const maxConcurrentJobs = options.maxConcurrentJobs === undefined
    ? parseSchedulerConcurrencyLimit(process.env[SCHEDULER_MAX_CONCURRENT_JOBS_ENV])
    : parseSchedulerConcurrencyLimit(String(options.maxConcurrentJobs));
  let activeJobCount = 0;
  const globalWaitQueue: Array<{ jobId: string; grant: () => void }> = [];
  let jobStore: JobStore | null = null;
  let reconcileTimer: NodeJS.Timeout | undefined;
  const resolveBot = options.getBotByAgentId ?? ((): Bot | null => null);
  const resolveFeishuClient = options.getFeishuClientByAgentId ?? ((): FeishuMessageClient | null => null);
  const currentNode = options.currentNode;
  const registerCronTasksEnabled = options.registerCronTasks ?? true;
  const deliveryEnabled = options.deliveryEnabled ?? true;
  const reconcileMissedRunsEnabled = options.reconcileMissedRuns ?? true;
  const terminalRegistry = options.terminalRegistry ?? new JobTerminalRegistry();
  const missedRunReconcileIntervalMs = options.missedRunReconcileIntervalMs
    ?? parseDurationMs(
      process.env['RELAY_SCHEDULER_RECONCILE_INTERVAL_MS'],
      DEFAULT_MISSED_RUN_RECONCILE_INTERVAL_MS,
      MIN_MISSED_RUN_RECONCILE_INTERVAL_MS,
    );
  const missedRunLookbackMs = options.missedRunLookbackMs
    ?? parseDurationMs(
      process.env['RELAY_SCHEDULER_MISSED_RUN_LOOKBACK_MS'],
      DEFAULT_MISSED_RUN_LOOKBACK_MS,
      60_000,
    );
  const runtimePriority = resolvePulseRuntimePriority();
  const runtimeOrchestrator = createBatchRuntimeOrchestrator({
    primary: options.runtimePolicy?.primary ?? runtimePriority[0],
    fallbacks: options.runtimePolicy?.fallbacks ?? runtimePriority.slice(1),
    cooldownMs: options.runtimePolicy?.cooldownMs ?? DEFAULT_RUNTIME_COOLDOWN_MS,
    adapters: [
      createCcBatchRuntimeAdapter({ sessionWiMcp: options.ccSessionWiMcp }),
      createCcInteractiveBatchRuntimeAdapter({ sessionWiMcp: options.ccSessionWiMcp }),
      createCodexBatchRuntimeAdapter(),
      createAgyBatchRuntimeAdapter(),
    ],
  });

  function botForJob(job: ScheduledJob): Bot | null {
    // An explicit relay agent id names the bot exactly; absent means no fallback.
    if (job.delivery.botAgentId) return resolveBot(job.delivery.botAgentId);
    const deliveryBotId = options.deliveryBotIdForOwner?.(job.owner);
    const direct = currentNode
      ? (deliveryBotId ? resolveBot(deliveryBotId) : null)
      : resolveBot(deliveryBotId ?? job.owner);
    if (direct) return direct;
    // Placement mode is strict: an absent explicit mapping is not permission to
    // deliver through whichever bot happened to be first in relay.json.
    if (currentNode) return null;
    const { bot, source } = resolveBotForOwner(job.owner, resolveBot, fallbackBot);
    if (source === 'fallback') {
      console.warn({
        ts: new Date().toISOString(),
        level: 'warn',
        module: 'scheduler',
        job: job.id,
        owner: job.owner,
        msg: 'no bot mapped for owner — using fallback bot',
      });
    }
    return bot;
  }

  function feishuClientForJob(job: ScheduledJob): FeishuMessageClient | null {
    const deliveryBotId = options.deliveryBotIdForOwner?.(job.owner);
    const client = currentNode
      ? (deliveryBotId ? resolveFeishuClient(deliveryBotId) : null)
      : resolveFeishuClient(deliveryBotId ?? job.owner);
    if (!client && job.delivery.mode === 'feishu_document') {
      console.warn({
        ts: new Date().toISOString(),
        level: 'warn',
        module: 'scheduler',
        job: job.id,
        owner: job.owner,
        msg: 'no Feishu client mapped for owner — feishu_document delivery will fail',
      });
    }
    return client;
  }

    function localJobs(jobs: ScheduledJob[]): ScheduledJob[] {
    if (!currentNode) return jobs;
    for (const job of jobs) {
      if (!job.runNode) throw new Error(`Job "${job.id}" is missing runNode`);
      if (!options.ownerCanRunOnNode?.(job.owner, job.runNode)) {
        throw new Error(`Job "${job.id}" owner "${job.owner}" cannot run on node "${job.runNode}"`);
      }
    }
    return jobs.filter(job => job.runNode === currentNode);
  }

  /**
   * Folds a finished run into the job's state and returns the result as
   * recorded: marked deferred exactly when the state now keeps its slot
   * waiting for a subscription limit to reset.
   */
  function recordJobRunState(job: ScheduledJob, finished: JobResult,
    observation = startObservation(job)): JobResult {
    const live = (liveRuns.get(job.id) ?? []).filter(run => run !== observation);
    if (live.length) liveRuns.set(job.id, live); else liveRuns.delete(job.id);
    // reload() replaces jobStore and its job objects while already-claimed work
    // continues against the old object, and other runs of the job may finish
    // on the new one meanwhile. The current object's state is authoritative:
    // fold this outcome into it (keeping its definitionHash/activatedAt, so the
    // durable cursor cannot trigger a duplicate catch-up); the run's own
    // definition decides how its outcome reads.
    const target = jobStore?.getJob(job.id) ?? job;
    const prior = target._state;
    const failed = finished.status !== 'ok';
    const now = new Date().toISOString();
    const quotaRetries = nextQuotaRetries(prior?.quotaRetries, {
      scheduledFor: observation.scheduledFor, definitionHash: observation.definitionHash,
      failed, quotaResetAt: finished.quotaResetAt,
    }, finishObservation({ ...job, _state: prior }, finished, observation).definitionHash);
    const waiting = failed ? quotaRetries?.find(item => item.scheduledFor === observation.scheduledFor) : undefined;
    const { deferred: _deferred, deferredReason: _reason, ...plain } = finished;
    void _deferred; void _reason;
    const result: JobResult = waiting ? { ...plain, deferred: true, deferredReason: 'quota_limit' } : plain;
    const observed = { ...finishObservation({ ...job, _state: prior }, result, observation),
      currentRun: live.at(-1) };
    // A slot waiting for its limit to reset leaves the failure streak as it was.
    const counted = failed && !waiting;
    target._state = {
      ...observed,
      consecutiveErrors: counted ? (prior?.consecutiveErrors ?? 0) + 1 : failed ? (prior?.consecutiveErrors ?? 0) : 0,
      lastRunAt: now,
      lastStatus: result.status,
      failingSince: counted ? (prior?.failingSince ?? now) : failed ? prior?.failingSince : undefined,
      failureAlerted: failed && prior?.failureAlerted === true ? true : undefined,
      quotaRetries,
    };
    if (waiting) {
      console.warn({ ts: now, level: 'warn', module: 'scheduler', job: job.id,
        msg: 'subscription limit reached; slot will run again after reset', ...waiting });
    }
    jobStore?.persistRunState();
    return result;
  }

  function acquireWorkspace(workspace: string, jobId: string): Promise<() => void> {
    const prev = workspaceQueue.get(workspace);
    let resolveCurrent!: () => void;
    const current = new Promise<void>(resolve => { resolveCurrent = resolve; });
    const release = (): void => {
      resolveCurrent();
      if (workspaceQueue.get(workspace) === current) {
        workspaceQueue.delete(workspace);
      }
    };
    // Set synchronously so subsequent callers chain behind us
    workspaceQueue.set(workspace, current);
    if (prev) {
      console.log({
        ts: new Date().toISOString(),
        level: 'info',
        module: 'scheduler',
        job: jobId,
        msg: 'workspace busy, queuing behind running job',
        workspace,
      });
      return prev.then(() => release);
    }
    return Promise.resolve(release);
  }

  function acquireGlobalCapacity(jobId: string): Promise<() => void> {
    if (maxConcurrentJobs === undefined) return Promise.resolve(() => {});

    return new Promise(resolvePermit => {
      const grant = (): void => {
        activeJobCount += 1;
        let released = false;
        resolvePermit(() => {
          if (released) return;
          released = true;
          activeJobCount -= 1;
          const next = globalWaitQueue.shift();
          next?.grant();
        });
      };

      if (activeJobCount < maxConcurrentJobs) {
        grant();
        return;
      }

      globalWaitQueue.push({ jobId, grant });
      console.log({
        ts: new Date().toISOString(),
        level: 'info',
        module: 'scheduler',
        job: jobId,
        msg: 'global concurrency limit reached, queuing job',
        active: activeJobCount,
        limit: maxConcurrentJobs,
        queueDepth: globalWaitQueue.length,
      });
    });
  }

  function maintenanceHeld(): boolean {
    return Boolean(options.admitScheduledRun && !options.admitScheduledRun());
  }

  function atCapacity(job: ScheduledJob): boolean {
    return (runningMap.get(job.id) ?? 0) >= (job.maxConcurrent ?? 1);
  }

  /**
   * The one claim every run passes: cron, missed-run catch-up and manual.
   * Returns the claim's release (idempotent), or null when none is granted.
   */
  function claimJob(job: ScheduledJob): (() => void) | null {
    // A held maintenance barrier admits no new run; a skipped scheduled slot
    // is picked up by missed-run reconciliation (or the next start) later.
    if (maintenanceHeld()) {
      console.warn({ ts: new Date().toISOString(), level: 'warn', module: 'scheduler', job: job.id, msg: 'run deferred by maintenance barrier' });
      return null;
    }
    if (atCapacity(job)) {
      console.warn({ ts: new Date().toISOString(), level: 'warn', module: 'scheduler', job: job.id, msg: 'previous run still active, skipping', active: runningMap.get(job.id), limit: job.maxConcurrent ?? 1 });
      return null;
    }
    runningMap.set(job.id, (runningMap.get(job.id) ?? 0) + 1);
    let released = false;
    return () => {
      if (released) return;
      released = true;
      runningMap.set(job.id, Math.max(0, (runningMap.get(job.id) ?? 0) - 1));
    };
  }

  async function executeClaimedJob(
    job: ScheduledJob,
    releaseClaim: () => void,
    source: JobTriggerSource,
    scheduledFor?: Date,
  ): Promise<JobResult> {
    let releaseCapacity: (() => void) | undefined;
    let releaseWorkspace: (() => void) | undefined;
    const observation = startObservation(job, scheduledFor);
    // The run as executeJob recorded it, once it has.
    let settled: JobResult | undefined;
    liveRuns.set(job.id, [...(liveRuns.get(job.id) ?? []), observation]);
    job._state = { ...(job._state ?? { consecutiveErrors: 0 }), currentRun: observation };
    try {
      jobStore?.persistRunState();
      // Capacity is acquired before the workspace lock so admission order is a
      // single FIFO across cron, reconciliation, and manual triggers. A queued
      // claim remains in runningMap and therefore keeps safeToRestart=false.
      releaseCapacity = await acquireGlobalCapacity(job.id);
      // Runs of a job allowed to overlap itself are independent of the
      // workspace, or the queue would serialize them again; a job that only
      // reads its workspace never makes the workspace's other jobs wait.
      if ((job.maxConcurrent ?? 1) === 1 && job.sharedWorkspace !== true) {
        releaseWorkspace = await acquireWorkspace(job.agent.workspace, job.id);
      }
      observation.phase = 'running';
      jobStore?.persistRunState();
      const deliveryBot = deliveryEnabled ? botForJob(job) : null;
      const deliveryFeishuClient = deliveryEnabled ? feishuClientForJob(job) : null;
      const result = await executeJob(
        job,
        deliveryBot,
        (input, preference) => runtimeOrchestrator.run({ ...input,
          ccWriteBoundary: options.ccWriteBoundaryFor?.(job.owner) === true,
          ccRepoAccess: options.ccRepoAccessFor?.(job.owner),
          ownerIsolation: options.ownerIsolationFor?.(job.owner) ?? 'unknown' }, preference),
        deliveryEnabled,
        deliveryFeishuClient,
        source,
        terminalRegistry,
        source === 'manual' ? undefined : () => {
          const current = jobStore?.getJob(job.id);
          if (!current?.enabled) throw new AutomaticExecutionPaused('job disabled while queued');
          requireAutomatic('job:' + job.id, job.owner);
        },
        options.agentDisplayName,
        // The failure streak lives on the current definition after a reload.
        () => jobStore?.getJob(job.id) ?? job,
        // A run is recorded once; a crash after that keeps the first record.
        (finished) => (settled ??= recordJobRunState(job, finished, observation)),
      );
      if (!settled) return recordJobRunState(job, result, observation);
      // The alert may have marked the streak alerted after the record.
      jobStore?.persistRunState();
      return result;
    } catch (error) {
      if (!settled) {
        recordJobRunState(job, {jobId: job.id, status: 'error', executionStatus: 'error',
          deliveryStatus: 'skipped', output: '', durationMs: Math.max(0, Date.now()-Date.parse(observation.startedAt))}, observation);
      }
      throw error;
    } finally {
      // The one ownership boundary of the claim, so resolver/workspace
      // failures cannot leak it.
      releaseClaim();
      releaseWorkspace?.();
      releaseCapacity?.();
    }
  }

  function triggerScheduledJob(job: ScheduledJob, source: 'cron' | 'reconcile', scheduledFor?: Date): void {
    const release = claimJob(job);
    if (!release) return;

    if (source === 'reconcile') {
      console.warn({
        ts: new Date().toISOString(),
        level: 'warn',
        module: 'scheduler',
        job: job.id,
        msg: 'missed scheduled run detected; triggering catch-up',
        scheduledFor: scheduledFor?.toISOString(),
        lastRunAt: job._state?.lastRunAt,
      });
    }

    (async () => {
      await executeClaimedJob(job, release, source, scheduledFor ?? floorToMinute(new Date()));
    })().catch(err => {
      release();
      console.error({ ts: new Date().toISOString(), level: 'error', module: 'scheduler', job: job.id, msg: 'scheduled job callback error', source, error: String(err) });
    });
  }

  async function reconcileDueJobs(source: 'startup' | 'timer' | 'reload'): Promise<void> {
    if (!jobStore || !reconcileMissedRunsEnabled) return;
    const now = new Date();
    const dueJobs: Array<{ job: ScheduledJob; dueRun: Date }> = [];
    for (const job of jobStore.listJobs().filter(j => j.enabled)) {
      if (runningMap.get(job.id)) continue;
      const registeredAt = registeredAtMap.get(job.id) ?? now;
      // A missed regular slot goes first: running it advances lastRunAt, which
      // would hide it if a quota retry ran before. The retry is keyed by its own
      // slot, not the cursor, so it simply runs on a later pass.
      const dueRun = findLatestDueRun(job, now, registeredAt, missedRunLookbackMs)
        ?? dueQuotaRetry(job._state?.quotaRetries, job._state ?? {}, now);
      if (!dueRun) continue;
      dueJobs.push({ job, dueRun });
    }

    dueJobs.sort((a, b) => a.dueRun.getTime() - b.dueRun.getTime());
    for (const { job, dueRun } of dueJobs) {
      if (runningMap.get(job.id)) continue;
      console.warn({
        ts: new Date().toISOString(),
        level: 'warn',
        module: 'scheduler',
        job: job.id,
        msg: 'scheduler reconciliation found missed run',
        source,
        scheduledFor: dueRun.toISOString(),
      });
      triggerScheduledJob(job, 'reconcile', dueRun);
    }
  }

  function clearReconcileTimer(): void {
    if (!reconcileTimer) return;
    clearTimeout(reconcileTimer);
    reconcileTimer = undefined;
  }

  function scheduleReconcileTimer(): void {
    clearReconcileTimer();
    if (!registerCronTasksEnabled || !reconcileMissedRunsEnabled) return;
    reconcileTimer = setTimeout(() => {
      reconcileDueJobs('timer')
        .catch(err => {
          console.error({ ts: new Date().toISOString(), level: 'error', module: 'scheduler', msg: 'scheduler reconciliation failed', error: String(err) });
        })
        .finally(scheduleReconcileTimer);
    }, missedRunReconcileIntervalMs);
    reconcileTimer.unref?.();
  }

  function canonicalizeJson(value: unknown): unknown {
    if (Array.isArray(value)) return value.map(canonicalizeJson);
    if (value !== null && typeof value === 'object') {
      return Object.fromEntries(
        Object.entries(value)
          .filter(([, entry]) => entry !== undefined)
          .sort(([left], [right]) => left.localeCompare(right))
          .map(([key, entry]) => [key, canonicalizeJson(entry)]),
      );
    }
    return value;
  }

  function jobDefinitionHash(job: ScheduledJob): string {
    const { _state: _ignoredState, ...definition } = job;
    return createHash('sha256')
      .update(JSON.stringify(canonicalizeJson(definition)))
      .digest('hex');
  }

  function activationFloorForDefinition(job: ScheduledJob, now = new Date()): { floor: Date; stateChanged: boolean } {
    const definitionHash = jobDefinitionHash(job);
    const activatedAtMs = job._state?.activatedAt ? Date.parse(job._state.activatedAt) : NaN;
    if (job._state?.definitionHash === definitionHash && Number.isFinite(activatedAtMs)) {
      return { floor: new Date(activatedAtMs), stateChanged: false };
    }

    // A legacy state record cannot prove which definition produced lastRunAt.
    // Fail safe once during migration: activate at ``now`` and intentionally
    // forgo historical catch-up. After the hash is durable, unchanged jobs
    // retain their activation floor and normal outage catch-up resumes.
    const floor = now;
    job._state = {
      ...(job._state ?? { consecutiveErrors: 0 }),
      definitionHash,
      activatedAt: floor.toISOString(),
    };
    return { floor, stateChanged: true };
  }

  function registerCronTask(job: ScheduledJob, activationFloor: Date): void {
    // reload must not erase a running or globally queued claim. Otherwise the
    // restart guard goes falsely green and a duplicate trigger can start.
    if (!runningMap.has(job.id)) runningMap.set(job.id, 0);
    registeredAtMap.set(job.id, activationFloor);
    const task = cronSchedule(
      job.schedule.expr,
      () => {
        triggerScheduledJob(job, 'cron');
      },
      { timezone: job.schedule.tz },
    );
    taskMap.set(job.id, task);
    console.log({ ts: new Date().toISOString(), level: 'info', module: 'scheduler', job: job.id, msg: 'registered cron', expr: job.schedule.expr, tz: job.schedule.tz });
  }

  /**
   * Build every cron registration of a candidate before any live one changes.
   * A registration that throws (an expression or timezone the scheduler
   * rejects) stops the ones already built and leaves the live set untouched.
   */
  function buildRegistrations(jobs: ScheduledJob[], activatedAt: Date):
    { tasks: Map<string, ScheduledTask>; floors: Map<string, Date> } {
    const tasks = new Map<string, ScheduledTask>();
    const floors = new Map<string, Date>();
    try {
      for (const job of jobs) {
        // Disabled definitions must also record their activation hash. Otherwise
        // re-enabling the old expression can replay slots from the disabled gap.
        const activation = activationFloorForDefinition(job, activatedAt);
        if (!job.enabled) continue;
        floors.set(job.id, activation.floor);
        tasks.set(job.id, cronSchedule(job.schedule.expr, () => {
          triggerScheduledJob(job, 'cron');
        }, { timezone: job.schedule.tz }));
      }
    } catch (err) {
      for (const task of tasks.values()) {
        try { task.stop(); } catch { /* ignore */ }
      }
      throw err;
    }
    return { tasks, floors };
  }

  function commitRegistrations(jobs: ScheduledJob[], built: ReturnType<typeof buildRegistrations>): void {
    for (const job of jobs) {
      const task = built.tasks.get(job.id);
      if (!task) continue;
      // reload must not erase a running or globally queued claim.
      if (!runningMap.has(job.id)) runningMap.set(job.id, 0);
      registeredAtMap.set(job.id, built.floors.get(job.id)!);
      taskMap.set(job.id, task);
      console.log({ ts: new Date().toISOString(), level: 'info', module: 'scheduler', job: job.id, msg: 'registered cron', expr: job.schedule.expr, tz: job.schedule.tz });
    }
  }

  function start(): void {
    const statePath = resolveCronJobsStatePath(configPath);
    const jobs = localJobs(loadJobs(configPath, statePath));
    jobStore = createJobStore(jobs, configPath, statePath);
    const enabled = jobs.filter(j => j.enabled);

    console.log({ ts: new Date().toISOString(), level: 'info', module: 'scheduler', msg: `Scheduler loaded ${enabled.length} jobs`, total: jobs.length });

    if (registerCronTasksEnabled) {
      commitRegistrations(jobs, buildRegistrations(jobs, new Date()));
      // Definition activation is a correctness boundary, not telemetry. It
      // must survive an immediate crash after reload/start, so bypass the
      // ordinary 200ms run-state debounce before reconciliation begins. The
      // write also prunes state for definitions removed from the config.
      const activationDurable = jobStore.persistRunStateNow();
      if (activationDurable) {
        reconcileDueJobs('startup').catch(err => {
          console.error({ ts: new Date().toISOString(), level: 'error', module: 'scheduler', msg: 'startup scheduler reconciliation failed', error: String(err) });
        });
      } else {
        console.error({ ts: new Date().toISOString(), level: 'error', module: 'scheduler', msg: 'startup missed-run reconciliation suppressed: activation state was not durable' });
      }
      scheduleReconcileTimer();
    }

  }

  function stop(): void {
    clearReconcileTimer();
    for (const task of taskMap.values()) {
      try { task.stop(); } catch { /* ignore */ }
    }
    taskMap.clear();
    registeredAtMap.clear();
    console.log({ ts: new Date().toISOString(), level: 'info', module: 'scheduler', msg: 'Scheduler stopped' });
  }

  function reload(): void {
    const statePath = resolveCronJobsStatePath(configPath);
    // 1. Make the current run state durable first: a run that just finished
    //    may still sit behind the 200ms debounce, and the candidate below
    //    reads run state from disk. A stale lastRunAt would replay that run.
    if (jobStore && !jobStore.persistRunStateNow()) {
      throw new Error('scheduler reload rejected: current run state is not durable');
    }
    // 2. Read and validate the whole candidate before replacing anything; on
    //    failure the registered jobs and the current store stay untouched.
    const jobs = localJobs(loadJobs(configPath, statePath, { strict: true }));
    // 3. Build the complete candidate registration set; a failure throws here.
    const built = registerCronTasksEnabled ? buildRegistrations(jobs, new Date()) : undefined;

    // 4. Commit synchronously (no timer can fire in between): retire the old
    //    store's pending writes and cron tasks, then adopt the candidate.
    jobStore?.dispose();
    clearReconcileTimer();
    for (const [, entry] of taskMap) {
      try { entry.stop(); } catch { /* ignore */ }
    }
    taskMap.clear();
    registeredAtMap.clear();
    jobStore = createJobStore(jobs, configPath, statePath);

    const enabled = jobs.filter(j => j.enabled);
    if (built) {
      commitRegistrations(jobs, built);
      const activationDurable = jobStore.persistRunStateNow();
      if (activationDurable) {
        reconcileDueJobs('reload').catch(err => {
          console.error({ ts: new Date().toISOString(), level: 'error', module: 'scheduler', msg: 'reload scheduler reconciliation failed', error: String(err) });
        });
      } else {
        console.error({ ts: new Date().toISOString(), level: 'error', module: 'scheduler', msg: 'reload missed-run reconciliation suppressed: activation state was not durable' });
      }
      scheduleReconcileTimer();
    }

    console.log({ ts: new Date().toISOString(), level: 'info', module: 'scheduler', msg: `Scheduler reloaded ${enabled.length} jobs` });
  }

  function listJobs(owner?: string): ScheduledJob[] {
    if (!jobStore) return [];
    return jobStore.listJobs(owner);
  }

  function getRunningJobIds(): string[] {
    return Array.from(runningMap.entries())
      .filter(([, running]) => running)
      .map(([jobId]) => jobId)
      .sort();
  }

  function addJob(input: Omit<ScheduledJob, '_state'>): ScheduledJob {
    if (!jobStore) throw new Error('Scheduler not started');
    if (currentNode) {
      prepareNodePlacedJob(input, currentNode, options.ownerCanRunOnNode ?? (() => false));
      throw new Error('Direct scheduler CRUD is disabled in node-placement mode; stage SSOT and cron source together, then project and preflight');
    }
    const job = jobStore.addJob(input);
    if (job.enabled && registerCronTasksEnabled) {
      const activation = activationFloorForDefinition(job);
      registerCronTask(job, activation.floor);
      jobStore.persistRunStateNow();
    }
    return job;
  }

  function removeJob(id: string, owner: string): boolean {
    if (!jobStore) return false;
    if (currentNode) {
      throw new Error('Direct scheduler CRUD is disabled in node-placement mode; stage SSOT and cron source together, then project and preflight');
    }
    const job = jobStore.getJob(id);
    if (!job || job.owner !== owner) return false;
    if (runningMap.get(id)) {
      console.warn({
        ts: new Date().toISOString(),
        level: 'warn',
        module: 'scheduler',
        job: id,
        msg: 'refusing to remove claimed job',
      });
      return false;
    }
    const task = taskMap.get(id);
    if (task) {
      try { task.stop(); } catch { /* ignore */ }
      taskMap.delete(id);
    }
    registeredAtMap.delete(id);
    runningMap.delete(id);
    return jobStore.removeJob(id, owner);
  }

  /**
   * Start one unattended run now, outside the job's schedule (an event such as
   * a new collaboration contract). It passes the same claim, automation control
   * and queues as a cron run and is marked `trigger=cron`; its observation slot
   * is the current minute. Never waits and never logs a skip: the caller
   * retries a `running` or `held` answer itself.
   */
  function triggerJob(id: string): JobTriggerStatus {
    const job = jobStore?.getJob(id);
    if (!job) return 'not_found';
    if (!job.enabled) return 'disabled';
    if (maintenanceHeld()) return 'held';
    if (atCapacity(job)) return 'running';
    const release = claimJob(job);
    if (!release) return 'running';
    console.log({ ts: new Date().toISOString(), level: 'info', module: 'scheduler', job: job.id, msg: 'event run triggered' });
    executeClaimedJob(job, release, 'event', floorToMinute(new Date())).catch(err => {
      release();
      console.error({ ts: new Date().toISOString(), level: 'error', module: 'scheduler', job: job.id, msg: 'event job callback error', error: String(err) });
    });
    return 'started';
  }

  async function runJob(id: string): Promise<JobResult> {
    if (!jobStore) throw new Error('Scheduler not started');
    const job = jobStore.getJob(id);
    if (!job) throw new Error(`Job "${id}" not found`);
    const release = claimJob(job);
    if (!release) {
      const errorMessage = maintenanceHeld()
        ? 'Relay maintenance in progress; run not started, retry after it'
        : 'previous run still active';
      return { jobId: job.id, status: 'error', output: '', durationMs: 0, errorMessage };
    }
    return executeClaimedJob(job, release, 'manual');
  }

  return { start, stop, reload, getRunningJobIds, listJobs, addJob, removeJob, runJob, triggerJob };
}
