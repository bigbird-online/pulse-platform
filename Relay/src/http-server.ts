import type { MaintenanceBarrier } from './maintenance-barrier.js';
import { SWITCH_BARRIER_CAPABILITY, switchErrorStatus } from './switch-barrier.js';
import { readRelayPathMap } from './managed-paths.js';
import { resolveRelayDataDir } from './config-paths.js';
// Relay loopback HTTP — for Platform's three-step provisioning flow.
//
// Bound to 127.0.0.1 only; no auth (relies on network isolation).
// Endpoints:
//   POST /internal/pairing/approve  — finalize a pending pairing
//   POST /internal/agents/reload    — spawn newly-added feishu WS clients (idempotent)
//   POST /internal/notice           — one bounded text send, original binding and receipt, no retry
//   POST /internal/push             — event-driven push (telegram/feishu/both; feishu supports attachments via `path`)
//   POST /internal/jobs/run         — manual run of one scheduled job on the resident scheduler (202, runs in background)
//   POST /internal/heartbeat        — meta-monitoring liveness beacon (body {source, ts}; wi_381 S1)
//   GET  /internal/heartbeat        — read per-source lastSeen (dual watcher freshness check)
//   GET  /internal/health           — token-authenticated uptime + bot count

import { randomUUID } from 'node:crypto';
import { NOTICE_CONTRACT, parseNoticeBinding, type NoticeBinding } from './notice-contract.js';
import { existsSync, mkdirSync, readFileSync, renameSync, writeFileSync } from 'node:fs';
import { createServer, type IncomingMessage, type Server, type ServerResponse } from 'node:http';
import { homedir } from 'node:os';
import { dirname, isAbsolute, join } from 'node:path';
import type { Bot } from 'grammy';
import type { FeishuBotInstance } from './channels/feishu.js';
import type { TelegramBotInstance } from './channels/telegram.js';
import { parseDeliveryRequest, type CollabDeliveryRequest, type CollabDeliveryStatus } from './collab-delivery.js';
import { AgentJobError } from './agent-jobs.js';
import { AgentJobAuthError } from './agent-jobs-auth.js';
import {
  approveFeishuPairingViaHttp,
  consumePendingPairingForHttp,
  peekPendingPairingForHttp,
} from './channels/feishu.js';
import {
  checkFileForDelivery,
  deliverFeishuFile,
  deliverFeishuImage,
  deliverFeishuText,
  deliverTelegramText,
  isImagePath,
  type FeishuMessageClient,
} from './delivery.js';
import type { Router } from './router.js';
import type { ConversationRuntimeManager } from './conversation-runtime.js';
import type { DeliveryReceipt, DeliveryRequest, Scheduler } from './types.js';
import type { JobTerminalRegistry } from './job-terminal-registry.js';
import { type TerminalManager, renderTerminalHtml } from './web-terminal.js';
import { WebSocketServer, type WebSocket } from 'ws';

/** Restart-safety projection. Only native session handovers are counted. */
export interface SclIntentMaintenanceStatus {
  available: boolean;
  busy: number;
  restartBlocking: number;
  states: Record<'recognized' | 'attempting' | 'retry_wait' | 'result_ready', number>;
}

const JSON_HDR = { 'Content-Type': 'application/json; charset=utf-8' } as const;
const BODY_LIMIT = 16 * 1024;

export interface RelayHttpServerDeps {
  feishuBots: FeishuBotInstance;
  telegramBots?: TelegramBotInstance;
  router: Router;
  configPath: string;
  startedAtMs?: number;
  /** Web Terminal manager (enables /terminal/:token browser-based CC takeover). */
  terminalManager?: TerminalManager;
  /** Resolve a Telegram bot for /internal/push by agent id. Returns null when unknown. */
  getTelegramBot?: (agentId: string) => Bot | null;
  /** Resolve a Feishu client for /internal/push by agent id. Returns null when unknown. */
  getFeishuClient?: (agentId: string) => FeishuMessageClient | null;
  /** Runtime manager for /internal/cc-status (busy-guard endpoint for safe restart). */
  runtimeManager?: ConversationRuntimeManager;
  /** Scheduler status for the same maintenance guard. */
  scheduler?: Pick<Scheduler, 'getRunningJobIds' | 'stop' | 'reload' | 'listJobs' | 'runJob'>;
  /** Durable SCL intents whose retry or final channel delivery must settle before restart. */
  sclIntentStatus?: () => SclIntentMaintenanceStatus;
  /** Shared scheduler run registry for idempotent terminal pushes. */
  terminalRegistry?: JobTerminalRegistry;
  /** Optional exact-destination restriction for a notice-only experiment. */
  noticeRouteAllowed?: (agentId: string, channel: string, to: string) => boolean;
  /** Re-project/compare placement before any live registry is mutated. */
  preflightPlacement?: () => void | (() => void);
  /** Deployment maintenance barrier (new turns and scheduled runs wait while held). */
  maintenance?: Pick<MaintenanceBarrier, 'hold' | 'release' | 'status' | 'owns' | 'switchBarrier' | 'acquireSwitch'>;
  /** Deliver a main-only collaboration contract into its holder conversation. */
  deliverCollaboration?: (request: CollabDeliveryRequest) => Promise<CollabDeliveryStatus>;
  /** An Agent's own scheduled jobs from a signed request (agent-jobs-auth.ts); absent where no scheduler runs. */
  agentJobs?: (envelope: unknown) => unknown;
}

export interface RelayHttpServerHandle {
  server: Server;
  port: number;
  stop(): Promise<void>;
}

function sendJson(res: ServerResponse, status: number, body: unknown): void {
  res.writeHead(status, JSON_HDR);
  res.end(JSON.stringify(body));
}

function readJsonBody(req: IncomingMessage, limit = BODY_LIMIT): Promise<unknown> {
  return new Promise((resolve, reject) => {
    const chunks: Buffer[] = [];
    let len = 0;
    req.on('data', (chunk: Buffer) => {
      len += chunk.length;
      if (len > limit) {
        reject(new Error('body_too_large'));
        return;
      }
      chunks.push(chunk);
    });
    req.on('end', () => {
      try {
        const raw = Buffer.concat(chunks).toString('utf8').trim();
        resolve(raw ? (JSON.parse(raw) as unknown) : {});
      } catch {
        reject(new Error('invalid_json'));
      }
    });
    req.on('error', reject);
  });
}

function maskCode(code: string): string {
  if (!code) return '';
  return `${code.slice(0, 2)}${'*'.repeat(Math.max(0, code.length - 2))}`;
}

function maskOpenId(openId: string): string {
  return openId.length > 8 ? `${openId.slice(0, 8)}...` : openId;
}

async function handlePairingApprove(
  req: IncomingMessage,
  res: ServerResponse,
  deps: RelayHttpServerDeps,
): Promise<void> {
  let body: unknown;
  try {
    body = await readJsonBody(req);
  } catch (e) {
    if (e instanceof Error && e.message === 'body_too_large') {
      sendJson(res, 413, { ok: false, error: 'payload_too_large' });
      return;
    }
    sendJson(res, 400, { ok: false, error: 'invalid_json' });
    return;
  }
  const obj = (body ?? {}) as Record<string, unknown>;
  const code = typeof obj.code === 'string' ? obj.code.trim() : '';
  const principalId = typeof obj.principalId === 'string' ? obj.principalId.trim() : '';
  const agentId = typeof obj.agentId === 'string' ? obj.agentId.trim() : '';
  if (!code || !principalId || !agentId) {
    sendJson(res, 400, { ok: false, error: 'missing_fields' });
    return;
  }

  const pending = peekPendingPairingForHttp(code);
  if (!pending) {
    console.log({ ts: new Date().toISOString(), level: 'info', msg: 'pairing_approve code_unknown', codeMasked: maskCode(code) });
    sendJson(res, 404, { ok: false, error: 'code_unknown' });
    return;
  }
  if (pending.agentId !== agentId) {
    console.warn({
      ts: new Date().toISOString(),
      level: 'warn',
      msg: 'pairing_approve agent_mismatch',
      codeMasked: maskCode(code),
      requested: agentId,
      expected: pending.agentId,
    });
    sendJson(res, 409, { ok: false, error: 'agent_mismatch', expectedAgentId: pending.agentId });
    return;
  }

  try {
    approveFeishuPairingViaHttp({
      configPath: deps.configPath,
      router: deps.router,
      principalId,
      agentId,
      openId: pending.openId,
    });
  } catch (err) {
    const message = err instanceof Error ? err.message : String(err);
    if (/missing Feishu agent/i.test(message)) {
      sendJson(res, 404, { ok: false, error: 'config_missing_agent', message });
      return;
    }
    console.error({ ts: new Date().toISOString(), level: 'error', msg: 'pairing_approve failed', error: message });
    sendJson(res, 500, { ok: false, error: 'approve_failed', message });
    return;
  }

  consumePendingPairingForHttp(code);
  console.log({
    ts: new Date().toISOString(),
    level: 'info',
    msg: 'pairing_approve ok',
    codeMasked: maskCode(code),
    agentId,
    openIdMasked: maskOpenId(pending.openId),
  });
  sendJson(res, 200, {
    ok: true,
    openId: pending.openId,
    agentId,
    principalId,
  });
}

async function handleAgentsReload(
  _req: IncomingMessage,
  res: ServerResponse,
  deps: RelayHttpServerDeps,
): Promise<void> {
  try {
    // Invalid/missing placement leaves the existing writers untouched. Once a
    // new projection passes, stop cron before reconciling clients; if a client
    // start fails the scheduler remains stopped and reload returns 500.
    const commitPlacement = deps.preflightPlacement?.();
    if (deps.preflightPlacement) {
      const runningJobs = deps.scheduler?.getRunningJobIds() ?? [];
      if (runningJobs.length) throw new Error(`placement reload rejected: scheduler jobs still running: ${runningJobs.join(',')}`);
    }
    commitPlacement?.();
    if (deps.preflightPlacement) deps.scheduler?.stop();
    // Outside placement the scheduler is never stopped. Its reload validates
    // the whole candidate first and throws without replacing anything, so it
    // runs before the channels: an invalid cron-jobs.json changes nothing.
    // Claims already in runningMap finish against their old job objects and
    // still block a duplicate start of the same job.
    else deps.scheduler?.reload();
    // Re-read relay.json so newly provisioned agents are discoverable.
    const routerState = deps.router.reload();
    console.log({
      ts: new Date().toISOString(),
      level: 'info',
      msg: 'Router reloaded from disk',
      ...routerState,
    });
    const telegramResult = await deps.telegramBots?.reload() ?? { added: [], removed: [], total: 0 };
    const result = await deps.feishuBots.reload();
    if (deps.preflightPlacement) deps.scheduler?.reload();
    sendJson(res, 200, { ok: true, ...result, addedIds: result.added, telegram: telegramResult });
  } catch (err) {
    const message = err instanceof Error ? err.message : String(err);
    console.error({ ts: new Date().toISOString(), level: 'error', msg: 'agents_reload failed', error: message });
    sendJson(res, 500, { ok: false, error: 'reload_failed', message });
  }
}

function handleHealth(res: ServerResponse, deps: RelayHttpServerDeps): void {
  const startedAt = deps.startedAtMs ?? Date.now();
  const map = readRelayPathMap();
  const feishu = deps.feishuBots.runningAgentIds();
  const telegram = deps.telegramBots?.runningAgentIds() ?? [];
  const configured = map ? {
    telegram: deps.router.getAllAgentBots().map(item => item.agent.id).sort(),
    feishu: deps.router.getAllFeishuBots().map(item => item.agent.id).sort(),
  } : undefined;
  const running = { telegram, feishu };
  const missing = map ? [
    ...Object.entries(configured!).flatMap(([channel, ids]) => ids
      .filter(id => !running[channel as 'telegram' | 'feishu'].includes(id))
      .map(agentId => ({ channel, agentId }))),
    ...(map.requiredChannels ?? []).filter(item => !running[item.channel].includes(item.agentId)),
  ] : [];
  sendJson(res, 200, {
    status: missing.length ? 'degraded' : 'ok',
    uptime: Math.max(0, Math.floor((Date.now() - startedAt) / 1000)),
    feishuBots: feishu.length,
    ...(map ? { telegramBots: telegram.length, configured, running, missing,
      configuration: { map: process.env['PULSE_RELAY_PATHS'], paths: map.paths } } : {}),
  });
}

/** GET /internal/cc-status — unified maintenance guard for safe restarts.
 *  Returns busy CC sessions and scheduler jobs that have been claimed but have
 *  not finished. Callers must require safeToRestart=true: restarting either a
 *  busy CC subprocess or a scheduler child loses in-flight work. */
function handleCcStatus(res: ServerResponse, deps: RelayHttpServerDeps): void {
  const status = maintenanceStatus(deps);
  if (!status) {
    sendJson(res, 503, { ok: false, error: 'runtime_manager_unavailable' });
    return;
  }
  sendJson(res, 200, status);
}

/** One consistent snapshot for the restart decision; undefined without a runtime manager. */
function maintenanceStatus(deps: RelayHttpServerDeps): Record<string, unknown> | undefined {
  if (!deps.runtimeManager) return undefined;
  const statuses = deps.runtimeManager.getStatus();
  const counts: Record<string, number> = { starting: 0, ready: 0, busy: 0, idle: 0, dead: 0 };
  const busyTenants: string[] = [];
  // The live runtime of each busy session, from this same snapshot.
  const busySessions: Array<{ tenantId: string; runtime: string }> = [];
  for (const s of statuses) {
    counts[s.state] = (counts[s.state] ?? 0) + 1;
    if (s.state === 'busy') {
      busyTenants.push(s.tenantId);
      busySessions.push({ tenantId: s.tenantId, runtime: String((s as { runtime?: unknown }).runtime ?? 'unknown') });
    }
  }

  let schedulerAvailable = false;
  let runningJobs: string[] = [];
  if (deps.scheduler) {
    try {
      runningJobs = deps.scheduler.getRunningJobIds();
      schedulerAvailable = true;
    } catch {
      // Keep the CC status useful to diagnostics while making the restart
      // decision fail closed through safeToRestart=false.
    }
  }
  let sclIntents:SclIntentMaintenanceStatus={
    available:deps.sclIntentStatus===undefined,busy:0,restartBlocking:0,
    states:{recognized:0,attempting:0,retry_wait:0,result_ready:0},
  };
  if(deps.sclIntentStatus){
    try{sclIntents=deps.sclIntentStatus();}catch{sclIntents={...sclIntents,available:false};}
  }
  const maintenance = deps.maintenance?.status();
  return {
    ok: true,
    total: statuses.length,
    counts,
    busy: busyTenants,
    busySessions,
    scheduler: {
      available: schedulerAvailable,
      busy: runningJobs.length,
      running: runningJobs,
    },
    sclIntents,
    ...(maintenance ? { maintenance } : {}),
    // A waiting turn held only in memory would be lost by the restart.
    safeToRestart: schedulerAvailable&&sclIntents.available
      &&busyTenants.length===0&&runningJobs.length===0&&sclIntents.restartBlocking===0
      &&!maintenance?.unsecured,
  };
}

type PushChannel = 'telegram' | 'feishu' | 'both';
type HumanDecisionKind = 'main_only' | 'owner_decision' | 'irreversible_authorization';

const HUMAN_DECISION_KINDS = new Set<HumanDecisionKind>([
  'main_only',
  'owner_decision',
  'irreversible_authorization',
]);
const AUTHORITY_FINGERPRINT_RE = /^[0-9a-f]{64}$/;

interface PushBody {
  channel: PushChannel;
  agentId: string;
  /** Single-channel convenience and fallback for `both` when toTelegram/toFeishu omitted. */
  to?: string;
  /** Explicit per-channel recipient. Takes precedence over `to`. */
  toTelegram?: string;
  toFeishu?: string;
  /** Text body. Required unless `path` is set (feishu attachment-only call). */
  text?: string;
  /** Absolute file path for feishu image/file delivery. Auto-routed via isImagePath. */
  path?: string;
  severity?: 'high' | 'mid' | 'low';
  source?: string;
  /** Scheduler execution id from RELAY_JOB_RUN_ID. */
  runId?: string;
  /** Marks this request as the run's user-visible terminal delivery. */
  terminal?: boolean;
  /** Harness's authoritative owner-attention routing receipt. */
  requiresHuman?: true;
  humanDecisionKind?: HumanDecisionKind;
  authorityFingerprint?: string;
}

interface PushReceipt {
  channel: 'telegram' | 'feishu';
  to: string;
  status: 'sent' | 'failed';
  externalMessageId?: string;
  attempts: number;
  error?: string;
}

function maskToken(token: string | undefined): string {
  if (!token) return '<empty>';
  return token.length <= 4 ? '*'.repeat(token.length) : `${token.slice(0, 2)}${'*'.repeat(token.length - 4)}${token.slice(-2)}`;
}

function buildTelegramRequest(to: string, text: string): DeliveryRequest {
  return {
    id: `push:tg:${randomUUID()}`,
    correlationId: `push:${randomUUID()}`,
    channel: 'telegram',
    address: { channel: 'telegram', kind: 'telegram_chat', value: to },
    payload: { kind: 'text', text },
  };
}

function buildFeishuRequest(to: string, text: string): DeliveryRequest {
  return {
    id: `push:fs:${randomUUID()}`,
    correlationId: `push:${randomUUID()}`,
    channel: 'feishu',
    address: { channel: 'feishu', kind: 'feishu_open_id', value: to },
    payload: { kind: 'text', text },
  };
}

function buildFeishuDocumentRequest(to: string, path: string): DeliveryRequest {
  return {
    id: `push:fs:doc:${randomUUID()}`,
    correlationId: `push:${randomUUID()}`,
    channel: 'feishu',
    address: { channel: 'feishu', kind: 'feishu_open_id', value: to },
    payload: { kind: 'document', path },
  };
}

function toPushReceipt(channel: 'telegram' | 'feishu', to: string, r: DeliveryReceipt): PushReceipt {
  if (r.status === 'sent') {
    return {
      channel,
      to,
      status: 'sent',
      attempts: r.attempts,
      ...(r.externalMessageId ? { externalMessageId: r.externalMessageId } : {}),
    };
  }
  return {
    channel,
    to,
    status: 'failed',
    attempts: r.attempts,
    error: r.error ?? 'unknown_error',
  };
}

async function handlePush(
  req: IncomingMessage,
  res: ServerResponse,
  deps: Pick<RelayHttpServerDeps, 'getTelegramBot' | 'getFeishuClient' | 'terminalRegistry' | 'noticeRouteAllowed'>,
  singleAttempt = false,
): Promise<void> {
  let notice: NoticeBinding | null = null;
  let sendAttempted = false;
  const reply = (status: number, body: Record<string, unknown>): void => {
    sendJson(res, status, singleAttempt
      ? { ...body, notice: notice ?? { contract: NOTICE_CONTRACT }, sendAttempted }
      : body);
  };
  // Auth: when RELAY_INTERNAL_TOKEN is set (non-empty), require exact header match.
  const expectedToken = (process.env['RELAY_INTERNAL_TOKEN'] ?? '').trim();
  if (expectedToken) {
    const headerToken = req.headers['x-relay-token'];
    const provided = Array.isArray(headerToken) ? headerToken[0] : headerToken;
    if (!provided || provided !== expectedToken) {
      reply(401, { ok: false, error: 'unauthorized' });
      return;
    }
  }

  let body: unknown;
  try {
    body = await readJsonBody(req, singleAttempt ? 32 * 1024 : BODY_LIMIT);
  } catch (e) {
    if (e instanceof Error && e.message === 'body_too_large') {
      reply(413, { ok: false, error: 'payload_too_large' });
      return;
    }
    reply(400, { ok: false, error: 'invalid_json' });
    return;
  }

  if (singleAttempt) {
    notice = parseNoticeBinding(body);
    if (!notice) {
      reply(400, { ok: false, error: 'invalid_notice' });
      return;
    }
  }

  const obj = (body ?? {}) as Record<string, unknown>;
  const channel = typeof obj.channel === 'string' ? (obj.channel.trim() as PushChannel | string) : '';
  const agentId = typeof obj.agentId === 'string' ? obj.agentId.trim() : '';
  const to = typeof obj.to === 'string' ? obj.to.trim() : '';
  const toTelegram = typeof obj.toTelegram === 'string' ? obj.toTelegram.trim() : '';
  const toFeishu = typeof obj.toFeishu === 'string' ? obj.toFeishu.trim() : '';
  const text = typeof obj.text === 'string' ? obj.text : '';
  const path = typeof obj.path === 'string' ? obj.path.trim() : '';
  const severity = typeof obj.severity === 'string' ? obj.severity.trim() : undefined;
  const source = typeof obj.source === 'string' ? obj.source.trim() : undefined;
  const runId = typeof obj.runId === 'string' ? obj.runId.trim() : '';
  const terminal = obj.terminal === true;
  const requiresHuman = obj.requiresHuman === true;
  const humanDecisionKind = typeof obj.humanDecisionKind === 'string'
    ? obj.humanDecisionKind.trim()
    : '';
  const authorityFingerprint = typeof obj.authorityFingerprint === 'string'
    ? obj.authorityFingerprint.trim()
    : '';

  if (!channel || !agentId) {
    reply(400, { ok: false, error: 'missing_fields' });
    return;
  }
  if (channel !== 'telegram' && channel !== 'feishu' && channel !== 'both') {
    reply(400, { ok: false, error: 'unknown_channel' });
    return;
  }
  // Harness owner pushes fail closed unless they carry the structured receipt
  // produced by human_attention.requires_human. Labels, severity and prose are
  // deliberately ignored here. Other /internal/push clients keep their own
  // contracts and are unaffected.
  if (source === 'harness-escalation') {
    if (
      !requiresHuman
      || !HUMAN_DECISION_KINDS.has(humanDecisionKind as HumanDecisionKind)
      || !AUTHORITY_FINGERPRINT_RE.test(authorityFingerprint)
    ) {
      reply(409, { ok: false, error: 'human_attention_receipt_required' });
      return;
    }
  }
  // path-only (attachment, no text) is allowed for feishu. Otherwise text is required.
  if (!path && (!text || !text.trim())) {
    reply(400, { ok: false, error: 'empty_text' });
    return;
  }
  if (path) {
    if (channel !== 'feishu') {
      reply(400, { ok: false, error: 'attachment_unsupported_channel', channel });
      return;
    }
    if (!isAbsolute(path)) {
      reply(400, { ok: false, error: 'path_not_absolute' });
      return;
    }
    const check = checkFileForDelivery(path);
    if (!check.ok) {
      reply(400, { ok: false, error: 'invalid_path', message: check.error });
      return;
    }
  }

  // Resolve per-channel recipients. Per-channel fields take precedence; `to`
  // serves as a single-field convenience and a fallback for `both` calls.
  // Format mismatches (TG numeric chat_id vs Feishu open_id) when both legs
  // fall back to the same `to` are intentionally not validated here — the
  // deliver functions will surface that via per-leg failed receipts.
  const needTelegram = channel === 'telegram' || channel === 'both';
  const needFeishu = channel === 'feishu' || channel === 'both';

  const resolvedTgTo = toTelegram || to;
  const resolvedFsTo = toFeishu || to;

  if (needTelegram && !resolvedTgTo) {
    const missing: 'toTelegram' | 'toFeishu' | 'both' =
      needFeishu && !resolvedFsTo ? 'both' : 'toTelegram';
    reply(400, { ok: false, error: 'missing_to', missing });
    return;
  }
  if (needFeishu && !resolvedFsTo) {
    reply(400, { ok: false, error: 'missing_to', missing: 'toFeishu' });
    return;
  }

  const push: PushBody = {
    channel: channel as PushChannel,
    agentId,
    ...(to ? { to } : {}),
    ...(toTelegram ? { toTelegram } : {}),
    ...(toFeishu ? { toFeishu } : {}),
    ...(text ? { text } : {}),
    ...(path ? { path } : {}),
    ...(severity === 'high' || severity === 'mid' || severity === 'low' ? { severity } : {}),
    ...(source ? { source } : {}),
    ...(runId ? { runId } : {}),
    ...(terminal ? { terminal: true } : {}),
    ...(requiresHuman ? { requiresHuman: true } : {}),
    ...(HUMAN_DECISION_KINDS.has(humanDecisionKind as HumanDecisionKind)
      ? { humanDecisionKind: humanDecisionKind as HumanDecisionKind }
      : {}),
    ...(AUTHORITY_FINGERPRINT_RE.test(authorityFingerprint)
      ? { authorityFingerprint }
      : {}),
  };

  if (singleAttempt && deps.noticeRouteAllowed && !deps.noticeRouteAllowed(agentId, channel, to)) {
    reply(403, { ok: false, error: 'notice_route_not_admitted' });
    return;
  }

  // Resolve targets first so we can fail-fast on agent_not_found (before any send).
  const bot = needTelegram ? deps.getTelegramBot?.(agentId) ?? null : null;
  const feishuClient = needFeishu ? deps.getFeishuClient?.(agentId) ?? null : null;

  if (needTelegram && !bot) {
    reply(404, { ok: false, error: 'agent_not_found', missing: 'telegram_bot', agentId });
    return;
  }
  if (needFeishu && !feishuClient) {
    reply(404, { ok: false, error: 'agent_not_found', missing: 'feishu_client', agentId });
    return;
  }

  let terminalRunId: string | undefined;
  if (terminal) {
    if (!deps.terminalRegistry) {
      reply(503, { ok: false, error: 'terminal_registry_unavailable' });
      return;
    }
    const reservation = deps.terminalRegistry.reserve(runId || undefined, agentId);
    if (!reservation.run) {
      reply(409, { ok: false, error: 'terminal_run_unavailable', reason: reservation.reason });
      return;
    }
    terminalRunId = reservation.run.runId;
    if (!reservation.deliver) {
      reply(200, {
        ok: true,
        deduplicated: true,
        runId: terminalRunId,
        terminalStatus: reservation.run.status,
        receipts: reservation.run.receipts,
      });
      return;
    }
  }

  const tasks: Array<Promise<PushReceipt>> = [];
  sendAttempted = true;
  if (needTelegram && bot) {
    tasks.push(
      deliverTelegramText(buildTelegramRequest(resolvedTgTo, text), bot, singleAttempt ? 0 : undefined).then((r) => toPushReceipt('telegram', resolvedTgTo, r)),
    );
  }
  if (needFeishu && feishuClient) {
    if (path) {
      const deliver = isImagePath(path) ? deliverFeishuImage : deliverFeishuFile;
      tasks.push(
        deliver(buildFeishuDocumentRequest(resolvedFsTo, path), feishuClient).then((r) => toPushReceipt('feishu', resolvedFsTo, r)),
      );
      if (text && text.trim()) {
        tasks.push(
          deliverFeishuText(buildFeishuRequest(resolvedFsTo, text), feishuClient).then((r) => toPushReceipt('feishu', resolvedFsTo, r)),
        );
      }
    } else {
      tasks.push(
        deliverFeishuText(buildFeishuRequest(resolvedFsTo, text), feishuClient).then((r) => toPushReceipt('feishu', resolvedFsTo, r)),
      );
    }
  }

  let receipts: PushReceipt[];
  try {
    receipts = await Promise.all(tasks);
  } catch (err) {
    // deliverTelegramText / deliverFeishuText already swallow per-call errors and
    // return failed receipts; reaching here would indicate an unexpected throw.
    if (singleAttempt) {
      reply(500, { ok: false, error: 'notice_result_unknown' });
      return;
    }
    const message = err instanceof Error ? err.message : String(err);
    console.error({
      ts: new Date().toISOString(),
      level: 'error',
      module: 'http-push',
      source: push.source,
      agentId,
      channel: push.channel,
      severity: push.severity,
      error: message,
    });
    if (terminalRunId) {
      deps.terminalRegistry?.complete(terminalRunId, [{
        channel: needFeishu ? 'feishu' : 'telegram',
        status: 'failed',
        error: message,
      }]);
    }
    reply(500, { ok: false, error: 'internal_error', message });
    return;
  }

  if (terminalRunId) {
    deps.terminalRegistry?.complete(terminalRunId, receipts);
  }

  const ok = receipts.every((r) => r.status === 'sent');
  if (singleAttempt) {
    // Do not expose transport errors (which may contain URLs or credentials).
    const safeReceipts = receipts.map(({ error, ...receipt }) => ({
      ...receipt, ...(error ? { error: 'channel_result_unknown' } : {}),
    }));
    reply(200, { ok, receipts: safeReceipts });
    return;
  }
  const logBase = {
    ts: new Date().toISOString(),
    module: 'http-push',
    source: push.source,
    agentId,
    channel: push.channel,
    severity: push.severity,
    runId: terminalRunId,
    terminal: push.terminal,
    requiresHuman: push.requiresHuman,
    humanDecisionKind: push.humanDecisionKind,
    authorityFingerprint: push.authorityFingerprint,
    tokenMasked: maskToken(expectedToken || undefined),
    // TG chat_id kept whole; Feishu open_id is long → mask to first 8 chars + ...
    toTelegramMasked: needTelegram ? resolvedTgTo : undefined,
    toFeishuMasked: needFeishu ? maskOpenId(resolvedFsTo) : undefined,
    attachment: path ? { name: path.split('/').pop() ?? '', kind: isImagePath(path) ? 'image' : 'file' } : undefined,
    receiptIds: receipts.map((r) => r.externalMessageId ?? null),
    attempts: receipts.map((r) => r.attempts),
    statuses: receipts.map((r) => r.status),
  };
  if (ok) {
    console.log({ ...logBase, level: 'info', msg: 'push delivered' });
  } else {
    console.error({
      ...logBase,
      level: 'error',
      msg: 'push partial/failed',
      errors: receipts.filter((r) => r.status === 'failed').map((r) => r.error),
    });
  }

  reply(200, { ok, receipts, ...(terminalRunId ? { runId: terminalRunId, terminal: true } : {}) });
}

// ── 元监控心跳端点（wi_20260703_381 S1，ho_234 设计约定）──────────────────────
// POST /internal/heartbeat  body {source, ts}  → 落 ~/.relay/heartbeats.json
// GET  /internal/heartbeat  → 返回各 source 的 lastSeen（对偶 watcher 巡新鲜度）
//
// 语义：一端周期性 POST 自己的心跳，对端 GET 检查 source 的 receivedAt 是否超时未更新
// → 超时=对端 probe 静默/整机挂 → 告警（S2 watcher 的活）。receivedAt 用服务端接收
// 时间做权威判据（不信任上报方时钟）；ts 存上报值供诊断。鉴权复用 RELAY_INTERNAL_TOKEN。

const HEARTBEAT_FILE = join(resolveRelayDataDir(), 'heartbeats.json');

/** 校验 x-relay-token。RELAY_INTERNAL_TOKEN 非空则强制匹配；空则放行（同 handlePush）。
 *  返回 true=通过。未通过时已写 401 响应。 */
async function handleCollabDeliver(req: IncomingMessage, res: ServerResponse,
  deps: Pick<RelayHttpServerDeps, 'deliverCollaboration'>): Promise<void> {
  if (!checkInternalToken(req, res)) return;
  if (!deps.deliverCollaboration) {
    sendJson(res, 503, { ok: false, error: 'collaboration_delivery_unavailable' });
    return;
  }
  let request: CollabDeliveryRequest;
  try {
    request = parseDeliveryRequest(await readJsonBody(req, 16 * 1024));
  } catch (error) {
    sendJson(res, 400, { ok: false, error: error instanceof Error ? error.message : 'invalid_body' });
    return;
  }
  try {
    sendJson(res, 200, { ok: true, status: await deps.deliverCollaboration(request) });
  } catch (error) {
    sendJson(res, 500, { ok: false, error: 'delivery_failed', message: String(error) });
  }
}

/** POST /internal/agent-jobs — the `pulse_jobs` tool's only entry. No internal
 * token: Agent sessions never hold it. The body is a request signed with the
 * calling conversation's key; an unproven caller gets 401 (agent-jobs-auth.ts). */
async function handleAgentJobs(req: IncomingMessage, res: ServerResponse,
  deps: Pick<RelayHttpServerDeps, 'agentJobs'>): Promise<void> {
  if (!deps.agentJobs) {
    sendJson(res, 503, { ok: false, error: 'scheduler_unavailable' });
    return;
  }
  let body: unknown;
  try {
    body = await readJsonBody(req, 32 * 1024);
    if (!body || typeof body !== 'object' || Array.isArray(body)) throw new Error('invalid_body');
  } catch {
    sendJson(res, 400, { ok: false, error: 'invalid_body' });
    return;
  }
  try {
    sendJson(res, 200, { ok: true, result: deps.agentJobs(body) });
  } catch (error) {
    const [status, code] = error instanceof AgentJobAuthError ? [401, 'unauthenticated']
      : error instanceof AgentJobError ? [400, 'refused'] : [500, 'failed'];
    sendJson(res, status, { ok: false, error: code, message: String((error as Error).message ?? error) });
  }
}

/** POST /internal/jobs/run {jobId} — a manual run on the resident scheduler,
 *  so claim, delivery and run state are those of a scheduled run. A run may
 *  take an hour; the reply is 202 once the run is claimed, and the outcome is
 *  read from the job's state (`relay-jobs list`) and the `job finished` log. */
async function handleJobRun(req: IncomingMessage, res: ServerResponse,
  deps: Pick<RelayHttpServerDeps, 'scheduler'>): Promise<void> {
  if (!checkInternalToken(req, res)) return;
  const scheduler = deps.scheduler;
  if (!scheduler) {
    sendJson(res, 503, { ok: false, error: 'scheduler_unavailable' });
    return;
  }
  let jobId: unknown;
  try {
    jobId = (await readJsonBody(req) as { jobId?: unknown } | null)?.jobId;
  } catch {
    sendJson(res, 400, { ok: false, error: 'invalid_body' });
    return;
  }
  if (typeof jobId !== 'string' || !jobId.trim()) {
    sendJson(res, 400, { ok: false, error: 'jobId_required' });
    return;
  }
  const job = scheduler.listJobs().find(item => item.id === jobId);
  if (!job) {
    sendJson(res, 404, { ok: false, error: 'job_not_found', jobId });
    return;
  }
  if (scheduler.getRunningJobIds().includes(jobId)) {
    sendJson(res, 409, { ok: false, error: 'job_running', jobId });
    return;
  }
  // runJob claims synchronously on call: if the id is not running right after
  // the call, the claim was refused and the promise already holds the reason.
  const run = scheduler.runJob(jobId);
  if (!scheduler.getRunningJobIds().includes(jobId)) {
    let result: Awaited<typeof run>;
    try {
      result = await run;
    } catch (error) {
      sendJson(res, 500, { ok: false, error: 'run_failed', message: String(error) });
      return;
    }
    const maintenance = /maintenance/i.test(result.errorMessage ?? '');
    sendJson(res, maintenance ? 503 : 409, { ok: false, error: maintenance ? 'maintenance_hold' : 'job_running',
      jobId, message: result.errorMessage });
    return;
  }
  const acceptedAt = new Date().toISOString();
  run.then(
    result => console.log({ ts: new Date().toISOString(), level: 'info', module: 'http', job: jobId,
      runId: result.runId, status: result.status, deliveryStatus: result.deliveryStatus, msg: 'manual job run finished' }),
    error => console.error({ ts: new Date().toISOString(), level: 'error', module: 'http', job: jobId,
      msg: 'manual job run threw', error: String(error) }),
  );
  sendJson(res, 202, { ok: true, jobId, name: job.name, acceptedAt });
}

function checkInternalToken(req: IncomingMessage, res: ServerResponse): boolean {
  const expectedToken = (process.env['RELAY_INTERNAL_TOKEN'] ?? '').trim();
  if (!expectedToken) return true;
  const headerToken = req.headers['x-relay-token'];
  const provided = Array.isArray(headerToken) ? headerToken[0] : headerToken;
  if (!provided || provided !== expectedToken) {
    sendJson(res, 401, { ok: false, error: 'unauthorized' });
    return false;
  }
  return true;
}

interface HeartbeatEntry {
  source: string;
  ts: string | null; // 上报方声称的时间（诊断用，不作判据）
  receivedAt: string; // 服务端接收时间（权威新鲜度判据）
  count: number; // 累计上报次数
}

function readHeartbeats(): Record<string, HeartbeatEntry> {
  if (!existsSync(HEARTBEAT_FILE)) return {};
  try {
    const parsed = JSON.parse(readFileSync(HEARTBEAT_FILE, 'utf8'));
    return parsed && typeof parsed === 'object' ? (parsed as Record<string, HeartbeatEntry>) : {};
  } catch {
    return {}; // 文件损坏不阻塞——下次写入覆盖重建
  }
}

/** 原子写：先写 tmp 再 rename，避免并发读到半截 JSON。mode 600（心跳含拓扑信息）。 */
function writeHeartbeats(data: Record<string, HeartbeatEntry>): void {
  const dir = dirname(HEARTBEAT_FILE);
  if (!existsSync(dir)) mkdirSync(dir, { recursive: true, mode: 0o700 });
  const tmp = `${HEARTBEAT_FILE}.tmp-${process.pid}`;
  writeFileSync(tmp, JSON.stringify(data, null, 2), { mode: 0o600 });
  renameSync(tmp, HEARTBEAT_FILE);
}

async function handleHeartbeatPost(req: IncomingMessage, res: ServerResponse): Promise<void> {
  if (!checkInternalToken(req, res)) return;
  let body: unknown;
  try {
    body = await readJsonBody(req);
  } catch (e) {
    if (e instanceof Error && e.message === 'body_too_large') {
      sendJson(res, 413, { ok: false, error: 'payload_too_large' });
      return;
    }
    sendJson(res, 400, { ok: false, error: 'invalid_json' });
    return;
  }
  const obj = (body ?? {}) as Record<string, unknown>;
  const source = typeof obj.source === 'string' ? obj.source.trim() : '';
  const ts = typeof obj.ts === 'string' ? obj.ts.trim() : null;
  if (!source) {
    sendJson(res, 400, { ok: false, error: 'missing_source' });
    return;
  }
  const now = new Date().toISOString();
  const data = readHeartbeats();
  const prev = data[source];
  data[source] = { source, ts, receivedAt: now, count: (prev?.count ?? 0) + 1 };
  try {
    writeHeartbeats(data);
  } catch (err) {
    sendJson(res, 500, { ok: false, error: 'write_failed', message: String(err).slice(0, 200) });
    return;
  }
  sendJson(res, 200, { ok: true, source, receivedAt: now });
}

function handleHeartbeatGet(req: IncomingMessage, res: ServerResponse): void {
  // GET 也鉴权：vultr RELAY_HTTP_HOST=0.0.0.0 含公网口，心跳含机器拓扑（哪些节点在/
  // lastSeen），不能裸给公网。对偶 watcher 本就持 token（要 POST 心跳），GET 带上零成本。
  if (!checkInternalToken(req, res)) return;
  const data = readHeartbeats();
  sendJson(res, 200, { ok: true, heartbeats: data });
}

function routePath(url: string): string {
  return url.split('?')[0] ?? '';
}

async function route(req: IncomingMessage, res: ServerResponse, deps: RelayHttpServerDeps): Promise<void> {
  const url = routePath(req.url ?? '');
  if (req.method === 'GET' && (url === '/internal/health' || url === '/internal/health/')) {
    if (!checkInternalToken(req, res)) return;
    handleHealth(res, deps);
    return;
  }
  if (req.method === 'GET' && (url === '/internal/cc-status' || url === '/internal/cc-status/')) {
    if (!checkInternalToken(req, res)) return;
    handleCcStatus(res, deps);
    return;
  }
  // Deployment maintenance barrier. A hold returns its own token and, in the
  // same response, the restart-decision snapshot taken under the hold.
  // Renewal and release name the token; another holder's token is refused.
  if (req.method === 'POST' && deps.maintenance
      && (url === '/internal/maintenance/hold' || url === '/internal/maintenance/release')) {
    if (!checkInternalToken(req, res)) return;
    let body: { ttlMs?: unknown; token?: unknown } | null;
    try { body = await readJsonBody(req) as typeof body; } catch { sendJson(res, 400, { ok: false, error: 'invalid_body' }); return; }
    const token = typeof body?.token === 'string' ? body.token : undefined;
    if (url.endsWith('/release')) {
      const released = token !== undefined && deps.maintenance.release(token);
      sendJson(res, released ? 200 : 409, { ok: released, ...(released ? {} : { error: 'not_holder' }),
        maintenance: deps.maintenance.status() });
      return;
    }
    const ttlMs = Number(body?.ttlMs);
    if (!Number.isFinite(ttlMs) || ttlMs <= 0) {
      sendJson(res, 400, { ok: false, error: 'ttlMs_required' });
      return;
    }
    const result = deps.maintenance.hold(ttlMs, token);
    if (!result.ok) {
      sendJson(res, 409, { ok: false, error: result.error, maintenance: result.status });
      return;
    }
    sendJson(res, 200, { ok: true, token: result.token, maintenance: result.status,
      status: maintenanceStatus(deps) ?? null });
    return;
  }
  // Durable switch barrier (switch-barrier.ts): survives restarts, opens only
  // by its holder's release or an operator break. 404 where unsupported.
  const switchBarrier = deps.maintenance?.switchBarrier;
  if (switchBarrier && url === '/internal/maintenance/switch' && req.method === 'GET') {
    if (!checkInternalToken(req, res)) return;
    sendJson(res, 200, { ok: true, capability: SWITCH_BARRIER_CAPABILITY, switch: switchBarrier.status(),
      maintenance: deps.maintenance!.status() });
    return;
  }
  if (switchBarrier && req.method === 'POST' && url.startsWith('/internal/maintenance/switch/')) {
    const action = url.slice('/internal/maintenance/switch/'.length);
    if (['acquire', 'renew', 'release', 'break'].includes(action)) {
      if (!checkInternalToken(req, res)) return;
      let body: Record<string, unknown>;
      try {
        const parsed = await readJsonBody(req);
        if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) throw new Error('not an object');
        body = parsed as Record<string, unknown>;
      } catch {
        sendJson(res, 400, { ok: false, error: 'invalid_body', switch: switchBarrier.status() });
        return;
      }
      const claim = { holder: body['holder'], transaction: body['transaction'], token: body['token'] };
      const result = action === 'acquire' ? deps.maintenance!.acquireSwitch({ ...claim, leaseMs: body['leaseMs'] })
        : action === 'renew' ? switchBarrier.renew({ ...claim, leaseMs: body['leaseMs'] })
        : action === 'release' ? switchBarrier.release(claim)
        : switchBarrier.breakBarrier({ transaction: body['transaction'], operator: body['operator'], reason: body['reason'] });
      if (!result.ok) {
        sendJson(res, switchErrorStatus(result.error), { ok: false, error: result.error, switch: result.status,
          // Held but not safe: the snapshot shows the unsecured turns; retry the same acquire.
          ...(result.error === 'pending_unsecured' ? { status: maintenanceStatus(deps) ?? null } : {}) });
        return;
      }
      sendJson(res, 200, { ok: true, switch: result.status,
        ...(result.recovered ? { recovered: true } : {}),
        ...(result.alreadyReleased ? { alreadyReleased: true } : {}),
        // Acquire answers with the restart-decision snapshot taken under the barrier.
        ...(action === 'acquire' ? { status: maintenanceStatus(deps) ?? null } : {}) });
      return;
    }
  }
  if (req.method === 'POST' && (url === '/internal/pairing/approve' || url === '/internal/pairing/approve/')) {
    await handlePairingApprove(req, res, deps);
    return;
  }
  if (req.method === 'POST' && (url === '/internal/agents/reload' || url === '/internal/agents/reload/')) {
    await handleAgentsReload(req, res, deps);
    return;
  }
  if (req.method === 'POST' && (url === '/internal/collab-deliver' || url === '/internal/collab-deliver/')) {
    await handleCollabDeliver(req, res, deps);
    return;
  }
  if (req.method === 'POST' && (url === '/internal/agent-jobs' || url === '/internal/agent-jobs/')) {
    await handleAgentJobs(req, res, deps);
    return;
  }
  if (req.method === 'POST' && (url === '/internal/jobs/run' || url === '/internal/jobs/run/')) {
    await handleJobRun(req, res, deps);
    return;
  }
  if (req.method === 'POST' && (url === '/internal/notice' || url === '/internal/notice/')) {
    await handlePush(req, res, deps, true);
    return;
  }
  if (req.method === 'POST' && (url === '/internal/push' || url === '/internal/push/')) {
    await handlePush(req, res, deps);
    return;
  }
  if (url === '/internal/heartbeat' || url === '/internal/heartbeat/') {
    if (req.method === 'POST') {
      await handleHeartbeatPost(req, res);
      return;
    }
    if (req.method === 'GET') {
      handleHeartbeatGet(req, res);
      return;
    }
  }
  // Web Terminal: serve static HTML page
  if (req.method === 'GET' && deps.terminalManager) {
    const termMatch = url.match(/^\/terminal\/([a-f0-9-]+)$/i);
    if (termMatch) {
      const token = termMatch[1];
      if (!deps.terminalManager.validateToken(token)) {
        sendJson(res, 410, { ok: false, error: 'token_expired_or_invalid' });
        return;
      }
      res.writeHead(200, { 'Content-Type': 'text/html; charset=utf-8' });
      res.end(renderTerminalHtml(token));
      return;
    }
  }
  sendJson(res, 404, { ok: false, error: 'not_found' });
}

/**
 * Resolve target port from env. `RELAY_HTTP_PORT` must be a positive integer to enable HTTP.
 */
export function resolveRelayHttpPort(env: NodeJS.ProcessEnv = process.env): number | undefined {
  const raw = (env['RELAY_HTTP_PORT'] ?? '').trim();
  if (!raw) return undefined;
  const n = Number(raw);
  if (!Number.isInteger(n) || n <= 0 || n > 65535) return undefined;
  return n;
}

/** Resolve bind host. Defaults to 127.0.0.1 (loopback-only); set RELAY_HTTP_HOST=0.0.0.0 to allow remote access (e.g. Web Terminal from external browsers). */
export function resolveRelayHttpHost(env: NodeJS.ProcessEnv = process.env): string {
  const raw = (env['RELAY_HTTP_HOST'] ?? '').trim();
  return raw || '127.0.0.1';
}

/** Start an HTTP listener. Defaults to loopback; set RELAY_HTTP_HOST=0.0.0.0 for remote access. */
export function startRelayHttpServer(deps: RelayHttpServerDeps, port: number, host?: string): Promise<RelayHttpServerHandle> {
  return new Promise((resolve, reject) => {
    const server = createServer((req, res) => {
      void (async () => {
        try {
          await route(req, res, deps);
        } catch {
          sendJson(res, 500, { ok: false, error: 'internal_error' });
        }
      })();
    });

    // WebSocket upgrade for Web Terminal (/terminal/:token/ws)
    const wss = new WebSocketServer({ noServer: true });
    server.on('upgrade', (req, socket, head) => {
      const url = routePath(req.url ?? '');
      const wsMatch = url.match(/^\/terminal\/([a-f0-9-]+)\/ws$/i);
      if (wsMatch && deps.terminalManager) {
        const token = wsMatch[1];
        wss.handleUpgrade(req, socket, head, (ws: WebSocket) => {
          deps.terminalManager!.attachWebSocket(token, ws);
        });
      } else {
        socket.destroy();
      }
    });

    server.once('error', (err) => reject(err));
    const bindHost = host ?? '127.0.0.1';
    server.listen(port, bindHost, () => {
      const addr = server.address();
      const actualPort = typeof addr === 'object' && addr ? addr.port : port;
      console.log({
        ts: new Date().toISOString(),
        level: 'info',
        msg: 'Relay HTTP server up',
        host: bindHost,
        port: actualPort,
      });
      resolve({
        server,
        port: actualPort,
        stop: (): Promise<void> =>
          new Promise<void>((res2) => {
            server.close(() => res2());
          }),
      });
    });
  });
}

/** Convenience: only start if `RELAY_HTTP_PORT` is set; otherwise return undefined. */
export async function startRelayHttpServerIfEnabled(
  deps: RelayHttpServerDeps,
  env: NodeJS.ProcessEnv = process.env,
): Promise<RelayHttpServerHandle | undefined> {
  const port = resolveRelayHttpPort(env);
  if (!port) return undefined;
  const host = resolveRelayHttpHost(env);
  return startRelayHttpServer(deps, port, host);
}

/** Exact existing destinations admitted by an operator for a bounded experiment. */
export interface NoticeDestination {
  agentId: string;
  channel: 'telegram' | 'feishu';
  to: string;
}

/** Same delivery handler, with no polling, WS, pairing or management endpoints. */
export function startNoticeOnlyServer(
  deps: Pick<RelayHttpServerDeps, 'getTelegramBot' | 'getFeishuClient'>,
  destinations: readonly NoticeDestination[],
  port = 0,
): Promise<RelayHttpServerHandle> {
  if (destinations.length < 1 || destinations.length > 128) {
    throw new Error('An explicit bounded notice destination set is required');
  }
  const admitted = new Set(destinations.map(d => JSON.stringify([d.agentId, d.channel, d.to])));
  const bound = {
    ...deps,
    noticeRouteAllowed: (agentId: string, channel: string, to: string) =>
      admitted.has(JSON.stringify([agentId, channel, to])),
  };
  return new Promise((resolve, reject) => {
    const server = createServer((req, res) => {
      if (req.method !== 'POST' || req.url !== '/internal/notice') {
        sendJson(res, 404, { ok: false, error: 'not_found' });
        return;
      }
      void handlePush(req, res, bound, true).catch(() => {
        if (!res.headersSent) {
          sendJson(res, 500, { ok: false, error: 'notice_result_unknown',
            notice: { contract: NOTICE_CONTRACT }, sendAttempted: true });
        } else {
          res.destroy();
        }
      });
    });
    server.on('upgrade', (_req, socket) => socket.destroy());
    server.once('error', reject);
    server.listen(port, '127.0.0.1', () => {
      const address = server.address();
      const actualPort = typeof address === 'object' && address ? address.port : port;
      resolve({
        server, port: actualPort,
        stop: () => new Promise<void>(done => { server.close(() => done()); }),
      });
    });
  });
}
