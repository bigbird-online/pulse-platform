import { replayPendingTurns } from '../shutdown-interrupts.js';
import { createChatLanes, type ChatLanes } from './chat-lanes.js';
import { createChannelIntakeJournal, intakeAwareRuntime, replayIntakeJournal, runJournaled, type ChannelIntakeJournal } from './intake-journal.js';
import { dispatchNativeWorkItemCommand, NATIVE_WI_UNAVAILABLE } from '../native-work-item-command.js';
import { WorkProcessView, workProcessEnabled } from '../work-process-view.js';
import { feishuProcessWriter } from '../work-process-transports.js';
import type { WorkProcessEvent } from '../work-process-events.js';
import {annotatedHumanMessage, invalidateHumanInput, readHumanInput, latestHumanInput, recordHumanInput, restoreHumanInputBoundary} from '../scl-human-decision.js';
import {runtimeEntryFailureMessage} from '../runtime-entry-error.js';
import lark from '@larksuiteoapi/node-sdk';
import type { StreamJsonMessage, AgentConfig, SessionEndReason, StateManager, UserConfig, RelayConfig, ConversationRuntimeId, RoutePolicyResult, ChannelBindingRecord, PoolExitMeta, Scheduler, BusyProgressEvent, PersistedSession, TurnSubmitOptions } from '../types.js';
import {conversationChannelRouter, type Router} from '../router.js';
import { readFileSync, readdirSync, renameSync, writeFileSync, mkdirSync, mkdtempSync, rmSync, statSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { createHash, randomUUID } from 'node:crypto';
import type { TurnAcceptedMeta } from '../runtime-background-work.js';
import { ChannelTurnRenderingState, TURN_ACCEPTED_STATUS_TEXT, TURN_GENERATED_STATUS_TEXT, TurnActivityCounter, turnAcceptedStatusText, turnReplyLead, type ChannelRenderFrame } from '../channel-rendering.js';
import {
  compileFeishuPayloads,
  isRichDocRolloutEnabled,
  persistChannelRenderingObservation,
  resolveRichDocChannelProfiles,
  type ChannelRenderingObserver,
  type ChannelRichDocProfile,
  type FeishuPayloadChunk,
  type FeishuPayloadCompilation,
} from '../channel-rich-rendering.js';
import { assertAdoptedConversationRuntime } from '../runtime-usage-binding.js';
import { conversationLaunch } from '../conversation-launch.js';
import { acquireConversation, conversationConsumerProjection } from '../conversation-acquisition.js';
import { collectRichDocCodeBlocks, compileRichDoc } from '../rich-doc.js';
import { formatConversationPoolKey, parseConversationPoolKey } from '../conversation-address.js';
import { FeishuInboundRecovery, FeishuRecoveryError, feishuRecoveryBoundaryToken, feishuRecoveryControlCommand, latestDurableFeishuInput, platformProvesStartupRejection, readFeishuRecoveryHistory, recordFeishuRecoveryBoundary, type FeishuInboundRecoveryRequest } from '../feishu-inbound-recovery.js';
import { existingPiSessionFile, loadPiRuntimeManifest } from '../pi-runtime-config.js';
import { managedRelayPath, RELAY_PATHS_ENV } from '../managed-paths.js';
import { publicRuntimeExceptionNotice, publicRuntimeResultNotice } from '../public-runtime-failure.js';

/**
 * Directory for saved Feishu images (created lazily).
 *
 * Persistent runners may execute Relay as several service accounts. A shared
 * `/tmp/relay-images` becomes unwritable as soon as another account creates it,
 * and its never-reclaimed files can grow without a bound. Keep the namespace
 * per runtime identity and prune it before every write.
 */
const RELAY_RUNTIME_ID = typeof process.getuid === 'function' ? String(process.getuid()) : String(process.pid);
const RELAY_IMAGE_DIR = join(tmpdir(), `relay-images-${RELAY_RUNTIME_ID}`);
const RELAY_IMAGE_MAX_BYTES = 512 * 1024 * 1024;
const RELAY_IMAGE_MAX_AGE_MS = 24 * 60 * 60 * 1000;

function prepareRelayImageDir(incomingBytes: number): void {
  // Keep the historical cross-user read contract for isolated tenant workers;
  // the per-UID namespace prevents other service accounts from writing here.
  mkdirSync(RELAY_IMAGE_DIR, { recursive: true, mode: 0o755 });
  const now = Date.now();
  const retained: Array<{ path: string; size: number; mtimeMs: number }> = [];
  for (const name of readdirSync(RELAY_IMAGE_DIR)) {
    const path = join(RELAY_IMAGE_DIR, name);
    try {
      const stat = statSync(path);
      if (!stat.isFile()) continue;
      if (now - stat.mtimeMs > RELAY_IMAGE_MAX_AGE_MS) {
        rmSync(path, { force: true });
        continue;
      }
      retained.push({ path, size: stat.size, mtimeMs: stat.mtimeMs });
    } catch {
      // A concurrent cleanup may have removed the entry.
    }
  }

  let retainedBytes = retained.reduce((total, file) => total + file.size, 0);
  for (const file of retained.sort((a, b) => a.mtimeMs - b.mtimeMs)) {
    if (retainedBytes + incomingBytes <= RELAY_IMAGE_MAX_BYTES) break;
    try {
      rmSync(file.path, { force: true });
      retainedBytes -= file.size;
    } catch {
      // If it vanished concurrently, the hard bound is checked below.
    }
  }
  if (retainedBytes + incomingBytes > RELAY_IMAGE_MAX_BYTES) {
    throw new Error(`Relay image cache limit exceeded (${RELAY_IMAGE_MAX_BYTES} bytes)`);
  }
}
/** Window for "session is still warm" — anything more idle than this is left alone on restart.
 *  24h is intentional (aligned with original 24757b4): if a user typed yesterday, they may
 *  still be waiting for the agent to come back. */
const FEISHU_RESUME_ACTIVE_WINDOW_MS = 24 * 60 * 60 * 1000;
/** Per-openId in-memory suppression window. A single restart wave triggers one wake-up
 *  per openId; subsequent restarts inside this window are silenced. Cleared when the
 *  process exits, so it does NOT survive a real (non-storm) restart later. */
const FEISHU_RESUME_SAME_STORM_MS = 3 * 60 * 1000;
const FEISHU_RESUME_SUPPRESS_STATE: Map<string, number> = new Map();

/** Test/runtime hook: clear in-memory same-storm suppression state.
 *  Real process clears this implicitly on restart (state is in-memory). */
export function _resetFeishuResumeSuppressState(): void {
  FEISHU_RESUME_SUPPRESS_STATE.clear();
}

/** Pure logic: should we emit a Feishu resume notice for this openId now?
 *  Returns true exactly once per FEISHU_RESUME_SAME_STORM_MS window per openId,
 *  and records the timestamp as a side effect when returning true. */
export function _shouldEmitFeishuResume(openId: string, now: number = Date.now()): boolean {
  const lastNotifiedAt = FEISHU_RESUME_SUPPRESS_STATE.get(openId);
  if (lastNotifiedAt !== undefined && now - lastNotifiedAt < FEISHU_RESUME_SAME_STORM_MS) {
    return false;
  }
  FEISHU_RESUME_SUPPRESS_STATE.set(openId, now);
  return true;
}

/** Build the same policy-bearing spawn request used for ordinary Feishu turns.
 * Restart recovery must not silently discard an agent's runtime preference or
 * Codex permission default merely because the process is being reconstructed. */
export function buildFeishuResumeSpawnConfig(
  agent: AgentConfig,
  poolKey: string,
  session: PersistedSession,
): Parameters<CommandRuntime['spawn']>[0] {
  return conversationLaunch({ poolKey, agent, session, channel: 'feishu', intent: 'recovery' });
}

import {
  handleStart,
  handleStatus,
  handleHelp,
  handleKill,
  handleRestart,
  handleNew,
  handleStop,
  handleSession,
  handleUsage,
  handleUsageSeal,
  handleCc,
  handleList,
  handleSwitch,
  handleReset,
  handleRuntime,
  handleYolo,
  handleModel,
  handleModelScope,
  handleModels,
  handleThink,
  handleCompact,
  handleContext,
  resolveInboundRoutingAgent,
  resolveAgentFromPoolKey,
  withAgentPickerUi,
  buildMenuCard,
  buildRuntimeOptionCard,
  buildYoloOptionCard,
  type CommandResult,
  type CommandRuntime,
} from '../commands.js';
import { handleListJobs, handleAddJob, handleDelJob, handleRunJob } from '../job-commands.js';
import { channelAddressKey, normalizeFeishuOpenIdAddress, resolveInboundRoute } from '../gateway.js';
import { deliverFeishuCard, deliverFeishuText, deliverFeishuImage, deliverFeishuFile, checkFileForDelivery, isImagePath } from '../delivery.js';
import type { FeishuMessageClient } from '../delivery.js';
import { buildReplyContextPrompt } from '../reply-context.js';
import { stripOutboundToolcallLeak, scrubDegenerateLines } from '../outbound-leak-guard.js';
import { emitDegenObserved } from '../relay-observability.js';
import { validateToolFileOutboxPath } from '../tool-file-outbox.js';
import { traceInboundRequest, traceDeliveryReceipt } from '../relay-request-trace.js';
import { recordFeishuOutboundText, lookupFeishuOutboundText } from '../feishu-outbound-cache.js';
import { transcribeAudio } from '../stt.js';
import { internalDeliveryPrompt } from '../collaboration-capture.js';
import { namesConversation, type NativeIdSource } from '../session-identity.js';
import { fencedInternalTurn } from '../internal-turn-fence.js';
import { collabTurnHeadline, feishuTurnNoticeCard, type CollabTurnNotice } from '../collab-progress-notice.js';
import {
  FeishuStreamingCardSession,
  isRetryableFeishuCardUpdateError,
  isFeishuStreamingCardEnvEnabled,
  mergeStreamingText,
} from '../feishu-streaming-card.js';
const FEISHU_MAX_CARD_LENGTH = 4000;
const FEISHU_COMMAND_CARD_ELEMENT_BUDGET = 80;

/** Inbound messages are tied to the Feishu app that received them; do not follow TG-only active agent. */
const FEISHU_INBOUND_ROUTING = { inboundChannel: 'feishu' as const };
const DEFAULT_FEISHU_FILE_MAX_BYTES = 25 * 1024 * 1024;
const UNREGISTERED_MSG = '\u26A0\uFE0F \u60A8\u5C1A\u672A\u6CE8\u518C\uFF0C\u8BF7\u8054\u7CFB\u7BA1\u7406\u5458\u5F00\u901A\u670D\u52A1\u3002';

// --- Pairing State ---

interface PendingPairing {
  code: string;
  agentId: string;
  openId: string;
  createdAt: number;
}

const pendingPairings = new Map<string, PendingPairing>();

// Feishu events are at-least-once delivered. Two gates, both applied when the
// event arrives, keep one message from being processed twice:
//
// 1. Stale gate on `create_time` (2026-08-05). A message written more than
//    FEISHU_MESSAGE_MAX_AGE_MS ago is dropped. Live case: a rating sent at
//    02:19:30 was redelivered at 03:24:55 (65 minutes later) and landed in the
//    session opened by a `/new` at 02:32 (message_id
//    om_x100b681d95e11cb0de39e7f124). `create_time` says when the message was
//    written, so the gate needs no memory and survives restarts. Five minutes
//    leaves room for a slow first delivery after a network stall.
// 2. message_id table for every younger message. Its window must outlive the
//    stale gate: an id forgotten before its message turns stale lets a
//    redelivery in between pass both gates. Feishu redelivers an event that is
//    not acknowledged within 3 seconds (15 s, 5 min, 1 h and 6 h later), and
//    again after a reconnect, so a redelivery can land anywhere inside the
//    stale window. The margin covers clock skew between Feishu and this host.
//
// Events are acknowledged at arrival (see createFeishuIntake), so a long Agent
// turn no longer causes redeliveries in the first place.
const recentInboundMessageIds = new Map<string, number>();
const FEISHU_MESSAGE_MAX_AGE_MS = 5 * 60 * 1000;
const FEISHU_CLOCK_SKEW_MARGIN_MS = 5 * 60 * 1000;
export const FEISHU_MESSAGE_DEDUPE_WINDOW_MS = FEISHU_MESSAGE_MAX_AGE_MS + FEISHU_CLOCK_SKEW_MARGIN_MS;

// Test seam: allow vitest to reset module-level dedupe state.
export function __resetFeishuInboundDedupeForTests(): void {
  recentInboundMessageIds.clear();
}

/** 飞书 `create_time` 是毫秒时间戳字符串；解析不出时返回 null（判不了就不拦）。 */
function parseFeishuCreateTime(value: unknown): number | null {
  if (typeof value !== 'string' && typeof value !== 'number') return null;
  const ms = Number(value);
  return Number.isFinite(ms) && ms > 0 ? ms : null;
}

setInterval(() => {
  const now = Date.now();
  for (const [k, ts] of recentInboundMessageIds) {
    if (now - ts > FEISHU_MESSAGE_DEDUPE_WINDOW_MS) recentInboundMessageIds.delete(k);
  }
}, 60_000).unref?.();

function feishuPoolKey(router: Router, userId: string, agent: AgentConfig): string {
  const base = router.poolKey(userId, agent.id);
  return agent.botToken
    ? formatConversationPoolKey({ userId, agentId: agent.id, channelScope: 'feishu' })
    : base;
}

/**
 * Feishu wires one pool listener per app. Only deliver CC events whose tenant poolKey
 * belongs to this agent — otherwise a stale mapping (or shared pool) can fan out artificer
 * replies to every feishu-only bot.
 */
function isFeishuDeliveryPoolKey(agent: AgentConfig, poolKey: string): boolean {
  const address = parseConversationPoolKey(poolKey);
  if (!address || address.agentId !== agent.id) return false;

  const dualChannelFeishu = !!agent.botToken && !!agent.feishu;
  return dualChannelFeishu ? address.channelScope === 'feishu' : !address.channelScope;
}

function feishuRecoveryUserId(agent: AgentConfig, poolKey: string): string {
  const address = parseConversationPoolKey(poolKey);
  if (!address || !isFeishuDeliveryPoolKey(agent, poolKey))
    throw new FeishuRecoveryError('recovery_identity_mismatch');
  return address.userId;
}

function targetsPiRuntime(ctx: MessageHandlerContext, userId: string): boolean {
  const routing = resolveInboundRoutingAgent(ctx.router, userId, ctx.agent, FEISHU_INBOUND_ROUTING);
  const poolKey = feishuPoolKey(ctx.router, userId, routing);
  const proc = ctx.pool.get(poolKey);
  return proc && 'runtime' in proc
    ? proc.runtime === 'pi'
    : routing.conversationRuntime?.runtime === 'pi';
}

function scopedFeishuCommandRouter(router: Router, agent: AgentConfig): Router {
  return {
    ...conversationChannelRouter(router, 'feishu'),
    poolKey(userId: string, agentId: string): string {
      if (agentId === agent.id) return feishuPoolKey(router, userId, agent);
      return router.poolKey(userId, agentId);
    },
  };
}

function generatePairingCode(): string {
  const chars = 'ABCDEFGHJKLMNPQRSTUVWXYZ23456789';
  let code = '';
  for (let i = 0; i < 6; i++) code += chars[Math.floor(Math.random() * chars.length)];
  return code;
}

function createPendingPairing(agentId: string, openId: string): string {
  for (const [code, p] of pendingPairings) {
    if (p.agentId === agentId && p.openId === openId) pendingPairings.delete(code);
  }
  const code = generatePairingCode();
  pendingPairings.set(code, { code, agentId, openId, createdAt: Date.now() });
  return code;
}

/**
 * 未绑定用户收到的配对提示。受管实例（PULSE_RELAY_PATHS）沿用 Platform 开通页 Step 3 的原文；
 * 独立部署没有开通页，改为中性文字：把配对码交给管理员。
 */
export function feishuPairingMessage(code: string, env: NodeJS.ProcessEnv = process.env): string {
  if (env[RELAY_PATHS_ENV] !== undefined) {
    return `\u{1F527} \u60A8\u5C1A\u672A\u7ED1\u5B9A\u6B64 Agent\u3002\n` +
      `\u914D\u5BF9\u7801\uFF1A${code}\n` +
      `\u8BF7\u56DE\u5230 PULSE \u5F00\u901A\u9875\u9762 Step 3 \u7C98\u8D34\u6B64\u7801\u5B8C\u6210\u7ED1\u5B9A\u3002` +
      `\uFF08\u8FD0\u8425\u53EF\u9009\uFF1A\u5728\u5DF2\u7ED1\u5B9A\u7684 Agent \u4E2D\u53D1\u9001 /approve ${code}\uFF09`;
  }
  return `\u{1F527} 您尚未绑定此助手。\n配对码：${code}\n请把配对码发给管理员完成绑定。`;
}

function claimPendingPairing(code: string): PendingPairing | undefined {
  const p = pendingPairings.get(code);
  if (!p) return undefined;
  pendingPairings.delete(code);
  return p;
}

/**
 * Public API used by Relay HTTP layer (`feishu-pairing-http`):
 * looks up a pairing without consuming it (peek) so callers can validate
 * `agentId` ownership before committing the binding write.
 */
export function peekPendingPairingForHttp(code: string): { agentId: string; openId: string } | undefined {
  const p = pendingPairings.get(code);
  if (!p) return undefined;
  return { agentId: p.agentId, openId: p.openId };
}

/** Public API for Relay HTTP layer: drop a pairing record after success. */
export function consumePendingPairingForHttp(code: string): void {
  pendingPairings.delete(code);
}

/** Public API for Relay HTTP layer: commit binding to relay.json and refresh router. */
export function approveFeishuPairingViaHttp(opts: {
  configPath: string;
  router: Router;
  principalId: string;
  agentId: string;
  openId: string;
  approvedBy?: string;
}): void {
  writeOpenIdToConfig(
    opts.configPath,
    opts.principalId,
    opts.agentId,
    opts.openId,
    opts.approvedBy ?? 'platform-http',
    opts.router,
  );
}

// Expire pending pairings older than 5 minutes
setInterval(() => {
  const now = Date.now();
  for (const [code, p] of pendingPairings) {
    if (now - p.createdAt > 5 * 60 * 1000) pendingPairings.delete(code);
  }
}, 60_000).unref?.();

function writeOpenIdToConfig(
  configPath: string,
  principalId: string,
  agentId: string,
  openId: string,
  approvedBy: string,
  router: Router,
): void {
  const raw = readFileSync(configPath, 'utf-8');
  const config = JSON.parse(raw) as RelayConfig;
  const user = config.users.find(candidate => candidate.userId === principalId);
  const agent = user?.agents.find(candidate => candidate.id === agentId && candidate.feishu);
  if (!user || !agent?.feishu) {
    throw new Error(`Cannot approve binding for missing Feishu agent "${agentId}"`);
  }
  const now = new Date().toISOString();
  const address = normalizeFeishuOpenIdAddress(openId, agent.feishu.appId, agent.id);
  const binding: ChannelBindingRecord = {
    id: `binding:${channelAddressKey(address)}:${user.userId}`,
    address,
    principalId: user.userId,
    tenantId: user.userId,
    defaultAgentId: agent.id,
    allowedAgentIds: user.agents.map(candidate => candidate.id),
    status: 'active',
    createdAt: now,
    approvedAt: now,
    approvedBy,
    auditTags: ['relay-json-bindings', 'feishu-pairing'],
  };
  config.bindings = (config.bindings ?? []).filter(existing => existing.id !== binding.id);
  config.bindings.push(binding);
  delete agent.feishuOpenId;
  const tmpPath = `${configPath}.tmp`;
  writeFileSync(tmpPath, JSON.stringify(config, null, 2) + '\n', 'utf-8');
  renameSync(tmpPath, configPath);
  router.upsertGatewayBinding?.(binding);
}

// --- Interactive Card Builder ---

export interface CardOptions {
  header?: { title: string; template?: string };
  note?: string;
  /** Appended after markdown / note blocks (e.g. interactive buttons). */
  trailingElements?: Record<string, unknown>[];
}

export function buildMarkdownCard(text: string, options?: CardOptions): Record<string, unknown> {
  const elements: Record<string, unknown>[] = [];
  if (text.trim()) {
    elements.push({ tag: 'markdown', content: text });
  }
  if (options?.note) {
    elements.push({ tag: 'hr' });
    elements.push({ tag: 'markdown', content: `<font color='grey'>${options.note}</font>` });
  }
  if (options?.trailingElements?.length) {
    elements.push(...options.trailingElements);
  }
  const card: Record<string, unknown> = {
    schema: '2.0',
    config: { width_mode: 'fill' },
    body: { elements },
  };
  if (options?.header) {
    card.header = {
      title: { tag: 'plain_text', content: options.header.title },
      template: options.header.template ?? 'blue',
    };
  }
  return card;
}

// --- Text Splitting ---

export function splitCardText(text: string, maxLength: number = FEISHU_MAX_CARD_LENGTH): string[] {
  if (text.length <= maxLength) return [text];

  const result: string[] = [];
  let remaining = text;

  while (remaining.length > 0) {
    if (remaining.length <= maxLength) { result.push(remaining); break; }

    let splitIdx = remaining.lastIndexOf('\n\n', maxLength);
    if (splitIdx > 0) { result.push(remaining.slice(0, splitIdx)); remaining = remaining.slice(splitIdx + 2); continue; }

    splitIdx = remaining.lastIndexOf('\n', maxLength);
    if (splitIdx > 0) { result.push(remaining.slice(0, splitIdx)); remaining = remaining.slice(splitIdx + 1); continue; }

    result.push(remaining.slice(0, maxLength));
    remaining = remaining.slice(maxLength);
  }

  return result;
}

// --- CC Noise Filter ---

const CC_NOISE_PATTERNS = [
  /OAuth\s*轮询.*超时/,
  /Key\s*已经在用了/,
  /second OAuth poll.*timed out/i,
  /apiKeySource/,
  // CC emits "[Request interrupted]" / "[Request interrupted by user]" as a
  // standalone marker line when a tool turn is aborted (e.g. AskUserQuestion
  // with no interactive frontend). Anchored to the whole line so normal prose
  // that merely contains "interrupted" is not stripped.
  /^\[Request interrupted(?: by user)?\]$/i,
  // CC AskUserQuestion self-answer transition text (A' approach: suppressed)
  /^I[''']ve asked (you )?(the question|about)/i,
  /waiting for (your|a) (answer|response|selection)/i,
];

// --- AskUserQuestion: interactive card REMOVED (2026-06-26) ---
// The Feishu AskUserQuestion card (registerPendingAsk / buildAskUserQuestionCard /
// applyAskAnswer / handleSubmitAsk / tryRouteTextAsAskAnswer / relay_ask_* callbacks /
// the pendingAsk persistence schema) was deleted after being dormant since the
// 2026-06-19 product decision (commits 2397a1ed/679e14fc) that disabled it: all
// Feishu agents run print-mode (interactive=null), where CC self-answers within the
// same turn and the runtime exits, so a card callback would hit a dead PTY
// ("No conversation runtime process for tenant"). AskUserQuestion tool_use blocks
// are now suppressed in handleFeishuPoolResponse; CC's self-answer narration is
// filtered by CC_NOISE_PATTERNS above. To re-add an interactive picker, restore from
// git history (commit c71e395c) or the spec under specs/features/interactive-question-card/.

export function stripCCNoise(text: string): string {
  const lines = text.split('\n');
  const cleaned = lines.filter(line => {
    const trimmed = line.trim();
    if (!trimmed) return true;
    return !CC_NOISE_PATTERNS.some(p => p.test(trimmed));
  });
  return cleaned.join('\n').trim();
}

function feishuInboundFileMaxBytes(): number {
  const raw = Number(process.env['RELAY_FEISHU_FILE_MAX_BYTES']);
  if (Number.isFinite(raw) && raw > 0) return Math.floor(raw);
  return DEFAULT_FEISHU_FILE_MAX_BYTES;
}

function isAsyncIterable(value: unknown): value is AsyncIterable<unknown> {
  return value != null && Symbol.asyncIterator in (value as object);
}

function extractFeishuDownloadContentType(resp: unknown): string | undefined {
  const r = resp as Record<string, unknown> & {
    headers?: Record<string, unknown>;
    header?: Record<string, unknown>;
    contentType?: string;
    mime_type?: string;
  };
  const headers = r.headers ?? r.header;
  if (headers && typeof headers === 'object') {
    for (const [k, v] of Object.entries(headers)) {
      if (k.toLowerCase() !== 'content-type') continue;
      if (typeof v === 'string' && v.trim()) return v.split(';')[0]?.trim();
      if (Array.isArray(v)) {
        const s = v.find((x) => typeof x === 'string');
        if (typeof s === 'string' && s.trim()) return s.split(';')[0]?.trim();
      }
    }
  }
  if (typeof r.contentType === 'string' && r.contentType.trim()) return r.contentType.split(';')[0]?.trim();
  if (typeof r.mime_type === 'string' && r.mime_type.trim()) return r.mime_type.split(';')[0]?.trim();
  return undefined;
}

/**
 * Normalize Feishu message_resource keys (trim outer whitespace).
 */
export function normalizeFeishuResourceKey(key: string): string {
  return key.trim();
}

/**
 * 把资源类消息（file/image/audio/media）的 content 渲染成人能读的一行。
 *
 * 引用回复场景专用：Agent 只拿得到被引用消息的渲染结果，拿不到原始 content。
 * 渲染成 `[Feishu file message]` 这种占位符等于什么都没说——2026-07-27 大鸟
 * 伴读时 Aki 连猜三轮关联文章全错，就是因为引用的文件名没透出来。
 *
 * 文件名是唯一能让 Agent 认出「引用的是哪个附件」的信息，必须优先给；
 * 没有文件名时退到 key（截断，只为可区分不同附件，不是给人读的）。
 */
export function describeFeishuResourceContent(
  msgType: string,
  content: unknown,
): string | undefined {
  let parsed: { file_key?: unknown; image_key?: unknown; file_name?: unknown };
  try {
    parsed = typeof content === 'string' ? JSON.parse(content) : (content as typeof parsed);
  } catch {
    return undefined;
  }
  if (!parsed || typeof parsed !== 'object') return undefined;

  const fileName = typeof parsed.file_name === 'string' ? parsed.file_name.trim() : '';
  if (fileName) return `[Feishu ${msgType}: ${fileName}]`;

  // image 用 image_key，其余资源类用 file_key（与直发路径同口径）。
  const rawKey = typeof parsed.image_key === 'string'
    ? parsed.image_key
    : (typeof parsed.file_key === 'string' ? parsed.file_key : '');
  const key = rawKey ? normalizeFeishuResourceKey(rawKey) : '';
  // key 只用于区分「是不是同一个附件」，全文对 Agent 无意义且很长，截断。
  if (key) return `[Feishu ${msgType}: ${key.slice(0, 16)}…]`;
  return undefined;
}

export async function readFeishuDownloadBuffer(resp: unknown): Promise<Buffer> {
  if (Buffer.isBuffer(resp)) return resp;
  if (resp instanceof Uint8Array) return Buffer.from(resp);
  if (resp instanceof ArrayBuffer) return Buffer.from(resp);

  const r = resp as Record<string, unknown> & {
    code?: number;
    msg?: string;
    data?: unknown;
    getReadableStream?: () => NodeJS.ReadableStream;
    writeFile?: (path: string) => Promise<void>;
  };

  if (typeof r.code === 'number' && r.code !== 0) {
    throw new Error(`Feishu resource download failed: ${typeof r.msg === 'string' ? r.msg : `code ${r.code}`}`);
  }

  if (r.data != null) {
    if (Buffer.isBuffer(r.data)) return r.data;
    if (r.data instanceof ArrayBuffer) return Buffer.from(r.data);
  }

  if (typeof r.getReadableStream === 'function') {
    const stream = r.getReadableStream();
    const chunks: Buffer[] = [];
    for await (const chunk of stream as unknown as AsyncIterable<Uint8Array | Buffer | string>) {
      chunks.push(Buffer.isBuffer(chunk) ? chunk : Buffer.from(chunk));
    }
    return Buffer.concat(chunks);
  }

  if (typeof r.writeFile === 'function') {
    const tmpDir = mkdtempSync(join(tmpdir(), 'relay-feishu-dl-'));
    try {
      const tmpPath = join(tmpDir, 'blob');
      await r.writeFile(tmpPath);
      return readFileSync(tmpPath);
    } finally {
      rmSync(tmpDir, { recursive: true, force: true });
    }
  }

  if (isAsyncIterable(resp)) {
    const chunks: Buffer[] = [];
    for await (const chunk of resp as AsyncIterable<Uint8Array | Buffer | string>) {
      chunks.push(Buffer.isBuffer(chunk) ? chunk : Buffer.from(chunk));
    }
    return Buffer.concat(chunks);
  }

  throw new Error(`Unexpected Feishu download response: ${typeof resp}`);
}

export async function downloadFeishuMessageResource(
  client: lark.Client,
  messageId: string,
  fileKey: string,
  resourceType: 'image' | 'file',
): Promise<{ buffer: Buffer; contentType?: string }> {
  const normalized = normalizeFeishuResourceKey(fileKey);
  if (!normalized) {
    throw new Error('Feishu message resource download failed: empty file_key');
  }
  const resp = await client.im.messageResource.get({
    path: { message_id: messageId, file_key: normalized },
    params: { type: resourceType },
  });
  const contentType = extractFeishuDownloadContentType(resp);
  const buffer = await readFeishuDownloadBuffer(resp);
  return { buffer, contentType };
}

// --- Image Download ---

export type FeishuFileClassification =
  | { kind: 'image'; mediaType: string }
  | { kind: 'document'; mediaType: string }
  | { kind: 'text_file'; mediaType: string }
  | { kind: 'unsupported'; mimeLabel: string };

function extLower(name: string | undefined): string {
  if (!name || typeof name !== 'string') return '';
  const i = name.lastIndexOf('.');
  return i >= 0 ? name.slice(i).toLowerCase() : '';
}

const FEISHU_TEXT_FILE_EXT = new Set([
  '.txt', '.md', '.markdown', '.csv', '.json', '.log', '.xml', '.yaml', '.yml', '.ini', '.cfg',
  '.ts', '.tsx', '.js', '.mjs', '.cjs', '.py', '.rs', '.go', '.html', '.css', '.sh',
]);

const FEISHU_IMAGE_EXT = new Set(['.png', '.jpg', '.jpeg', '.gif', '.webp', '.bmp', '.tif', '.tiff']);

function guessImageMimeFromExt(ext: string): string {
  switch (ext) {
    case '.png': return 'image/png';
    case '.gif': return 'image/gif';
    case '.webp': return 'image/webp';
    case '.bmp': return 'image/bmp';
    case '.tif':
    case '.tiff':
      return 'image/tiff';
    default:
      return 'image/jpeg';
  }
}

export function classifyFeishuInboundFile(params: {
  contentType?: string;
  fileName?: string;
}): FeishuFileClassification {
  const rawMime = params.contentType?.split(';')[0]?.trim().toLowerCase() ?? '';
  const ext = extLower(params.fileName);

  if (rawMime.startsWith('image/') || FEISHU_IMAGE_EXT.has(ext)) {
    const mediaType = rawMime.startsWith('image/') ? rawMime : guessImageMimeFromExt(ext);
    return { kind: 'image', mediaType };
  }
  if (rawMime === 'application/pdf' || ext === '.pdf') {
    return { kind: 'document', mediaType: 'application/pdf' };
  }
  if (rawMime.startsWith('text/') || FEISHU_TEXT_FILE_EXT.has(ext)) {
    const mediaType = rawMime.startsWith('text/') ? rawMime : 'text/plain';
    return { kind: 'text_file', mediaType };
  }

  return { kind: 'unsupported', mimeLabel: rawMime || ext || 'unknown' };
}

export function humanContentBlocksFromFeishuFile(
  buffer: Buffer,
  classification: { kind: 'image'; mediaType: string } | { kind: 'document'; mediaType: string } | { kind: 'text_file'; mediaType: string },
  fileName?: string,
): unknown[] {
  const label = fileName ? `[文件: ${fileName}]\n` : '';
  if (classification.kind === 'text_file') {
    const text = buffer.toString('utf-8');
    return [{ type: 'text', text: label + text }];
  }
  const base64 = buffer.toString('base64');
  const blockType = classification.kind === 'image' ? 'image' : 'document';
  return [{ type: blockType, source: { type: 'base64', media_type: classification.mediaType, data: base64 } }];
}

export async function downloadFeishuImage(
  client: lark.Client,
  messageId: string,
  imageKey: string,
): Promise<{ base64: string; mediaType: string }> {
  const { buffer, contentType } = await downloadFeishuMessageResource(client, messageId, imageKey, 'image');
  const mediaType =
    contentType && contentType.toLowerCase().startsWith('image/') ? contentType : 'image/jpeg';
  return { base64: buffer.toString('base64'), mediaType };
}

// --- Feishu Bot Types ---

export interface FeishuBotDeps {
  pool: CommandRuntime;
  router: Router;
  stateManager?: StateManager;
  scheduler?: Scheduler;
  configPath: string;
  meteringDir?: string;
  metering?: {
    onSoftQuotaWarning(handler: (event: import('../types.js').SoftQuotaWarningEvent) => void): void;
  };
  modelCatalog?: import('../model-catalog.js').ModelCatalog;
  /** Web Terminal manager for /cc browser takeover. */
  terminalManager?: import('../web-terminal.js').TerminalManager;
  /** Platform HTTP base URL for billing commands (e.g. http://127.0.0.1:8787). */
  platformHttpUrl?: string;
}

export interface FeishuBotInstance {
  start(): Promise<void>;
  stop(): void;
  /**
   * Idempotent: spawn WS clients for any feishu agent in `relay.json` not yet running.
   * Existing bots are untouched (no SIGTERM, no reconnect). Returns ids actually added.
   */
  reload(): Promise<{ added: string[]; removed: string[]; total: number }>;
  /** For diagnostics. */
  runningAgentIds(): string[];
  getMessageClient(agentId: string): FeishuMessageClient | null;
  sendText(agentId: string, openId: string, text: string): Promise<void>;
  /** Broadcast restart notice to Feishu users not already notified via pool session-end. */
  notifyShutdown(excludePoolKeys?: Set<string>): Promise<void>;
  /** After Relay has fully started, wake up recently active Feishu CC sessions to notify users. */
  notifyResumed(): Promise<void>;
  /** Start one internal turn in an existing conversation this bot owns
   * (collaboration delivery). False when not owned or its native session changed. */
  deliverInternalTurn(poolKey: string, prompt: string, expectNativeIds: string[], notice?: CollabTurnNotice, deliveryRef?: string): Promise<boolean>;
}

export interface ReconcileClient {
  start(): Promise<void>;
  stop(): void;
}

/** Keeps construction side-effect free so reconcile can create a prospective
 * client without opening a second writer before the stop phase has succeeded. */
export function createDeferredFeishuWsLifecycle<TDispatcher>(
  wsClient: {
    start(options: { eventDispatcher: TDispatcher }): unknown;
    close(options: { force: boolean }): unknown;
  },
  eventDispatcher: TDispatcher,
): ReconcileClient {
  return {
    async start(): Promise<void> {
      await Promise.resolve(wsClient.start({ eventDispatcher }));
    },
    stop(): void {
      wsClient.close({ force: true });
    },
  };
}

/** Reconcile a live client registry to an exact desired set. Removals are
 * stopped before additions start, so an active-node move cannot retain the old
 * writer while a new one is created. Any stop/start failure rejects reload. */
export async function reconcileFeishuClientRegistry<TSpec, TClient extends ReconcileClient>(
  running: Map<string, TClient>,
  desired: Map<string, TSpec>,
  create: (id: string, spec: TSpec) => TClient,
): Promise<{ added: string[]; removed: string[]; total: number }> {
  const added: string[] = [];
  const removed: string[] = [];
  const failures: string[] = [];
  for (const id of Array.from(running.keys()).sort()) {
    if (desired.has(id)) continue;
    const client = running.get(id)!;
    try {
      client.stop();
      running.delete(id);
      removed.push(id);
    } catch (error) {
      failures.push(`stop ${id}: ${String(error)}`);
    }
  }
  if (failures.length) throw new Error(`Feishu client reconcile failed before additions: ${failures.join('; ')}`);
  for (const id of Array.from(desired.keys()).sort()) {
    if (running.has(id)) continue;
    const client = create(id, desired.get(id)!);
    try {
      await client.start();
      running.set(id, client);
      added.push(id);
    } catch (error) {
      try { client.stop(); } catch { /* retain original start failure */ }
      failures.push(`start ${id}: ${String(error)}`);
      break;
    }
  }
  if (failures.length) {
    for (const id of added) {
      try { running.get(id)?.stop(); } catch { /* registry deletion remains mandatory */ }
      running.delete(id);
    }
    throw new Error(`Feishu client reconcile failed: ${failures.join('; ')}`);
  }
  return { added, removed, total: running.size };
}

// --- Message handler context ---

export interface MessageHandlerContext {
  agent: AgentConfig;
  pool: CommandRuntime;
  router: Router;
  recordStartupFailure?:(input:FeishuInboundRecoveryRequest)=>boolean;
  stateManager?: StateManager;
  scheduler?: Scheduler;
  poolKeyToOpenId: Map<string, string>;
  configPath: string;
  meteringDir?: string;
  sendTextMessage: (openId: string, text: string) => Promise<void>;
  sendCardMessage: (openId: string, card: Record<string, unknown>) => boolean | void | Promise<boolean | void>;
  downloadImage: (messageId: string, imageKey: string) => Promise<{ base64: string; mediaType: string }>;
  /** Inbound msg_type=file — defaults to Lark `messageResource` with type=file. */
  downloadFileResource: (messageId: string, fileKey: string) => Promise<{ buffer: Buffer; contentType?: string }>;
  /** Inbound msg_type=audio — same STT path as Telegram (`stt.transcribeAudio`, `CLAUDE_CODE_OAUTH_TOKEN`). */
  transcribeInboundAudio: (buffer: Buffer) => Promise<string>;
  fetchReplyText?: (messageId: string) => Promise<string | undefined>;
  startTyping?: (messageId: string) => void;
  stopTyping?: (messageId?:string) => void;
  /** Lark client for calling Feishu OpenAPI directly (e.g. minutes transcript). */
  larkClient: lark.Client;
  /** Platform HTTP base URL for billing commands. */
  platformHttpUrl?: string;
  modelCatalog?: import('../model-catalog.js').ModelCatalog;
  terminalManager?: import('../web-terminal.js').TerminalManager;
}

function resolveGatewayRoute(ctx: MessageHandlerContext, openId: string, expectedUserId: string, text?: string): RoutePolicyResult | undefined {
  if (!ctx.agent.feishu) return undefined;
  const route = resolveInboundRoute({
    users: ctx.router.getAllUsers(),
    bindings: ctx.router.getGatewayBindings?.(),
    event: {
      id: `feishu:${ctx.agent.id}:${openId}:${Date.now()}`,
      createdAt: new Date().toISOString(),
      channel: 'feishu',
      address: normalizeFeishuOpenIdAddress(openId, ctx.agent.feishu.appId, ctx.agent.id),
      text,
    },
    targetAgentId: ctx.agent.id,
  });
  if (route.status !== 'ok') {
    console.warn({
      ts: new Date().toISOString(),
      level: 'warn',
      msg: 'Gateway route facade did not resolve authorized Feishu route',
      agent: ctx.agent.id,
      openId: `${openId.slice(0, 8)}...`,
      status: route.status,
      reason: route.reason,
    });
    return undefined;
  }
  if (route.route.agentId !== ctx.agent.id || route.route.principal.id !== expectedUserId) {
    console.warn({
      ts: new Date().toISOString(),
      level: 'warn',
      msg: 'Gateway route facade drift',
      agent: ctx.agent.id,
      routeAgent: route.route.agentId,
      expectedUserId,
      routePrincipal: route.route.principal.id,
      openId: `${openId.slice(0, 8)}...`,
    });
    return undefined;
  }
  return route.route;
}

// --- Post message text extraction ---

interface PostContentBlock {
  tag: string;
  text?: string;
  href?: string;
  user_id?: string;
  user_name?: string;
}

/**
 * Extract plain text from Feishu post message content.
 * Post messages wrap text in a nested structure: { zh_cn: { title, content: [[block, block], ...] } }
 *
 * Handles multiple fallback shapes for copy-paste and edge-case content:
 * - Direct string content
 * - Flat { text: '...' } object
 * - One-dimensional content arrays (each element is a block, not a line)
 * - emotion tags (extract emoji key as text)
 */
export function extractPostText(contentStr: string): string | null {
  try {
    const content = JSON.parse(contentStr);

    // 1. If content is a plain string, return it directly.
    if (typeof content === 'string') {
      return content || null;
    }

    // 2. If content has a direct text field (flat structure).
    if (typeof content.text === 'string') {
      return content.text || null;
    }

    // 3. Resolve locale block (zh_cn / en_us / ja_jp / direct post / first key).
    let locale = content.zh_cn ?? content.en_us ?? content.ja_jp;
    if (!locale) {
      // Some clients send post content without locale wrapper: { title, content }
      if (Array.isArray(content.content)) {
        locale = content;
      } else {
        locale = Object.values(content).find(v => v && typeof v === 'object') ?? null;
      }
    }
    if (!locale) return null;

    // 4. Handle content array (2D lines or 1D blocks fallback).
    const rawContent = locale.content;
    if (!Array.isArray(rawContent)) return null;

    const lines: string[] = [];
    if (locale.title) {
      lines.push(locale.title);
    }
    for (const lineOrBlock of rawContent) {
      if (Array.isArray(lineOrBlock)) {
        // Standard 2D: lineOrBlock is a line (array of blocks).
        let lineText = '';
        for (const block of lineOrBlock) {
          lineText += extractBlockText(block);
        }
        if (lineText) lines.push(lineText);
      } else {
        // 1D fallback: each element is a single block.
        const blockText = extractBlockText(lineOrBlock);
        if (blockText) lines.push(blockText);
      }
    }

    return lines.join('\n') || null;
  } catch {
    return null;
  }
}

function extractBlockText(block: unknown): string {
  if (!block || typeof block !== 'object') return '';
  const b = block as Record<string, unknown>;
  if (b.tag === 'text' && typeof b.text === 'string') {
    return b.text;
  }
  if (b.tag === 'a' && typeof b.href === 'string') {
    return b.href;
  }
  if (b.tag === 'at' && typeof b.user_name === 'string') {
    return `@${b.user_name}`;
  }
  if (b.tag === 'emotion' && typeof b.emoji_type === 'string') {
    return `:${b.emoji_type}:`;
  }
  return '';
}

/**
 * 图文混排（post）里嵌的图片块，按出现顺序取出 image_key。
 *
 * 飞书把「发图片时附一句话」编码成 post，图片是 `tag: 'img'` 块。
 * {@link extractBlockText} 对它返回空串——文字过得去、图片凭空消失，
 * Agent 只看见文字，还以为大鸟没发图（2026-07-27 大鸟指出）。
 */
export function collectPostImageKeys(contentStr: string): string[] {
  const keys: string[] = [];
  const visit = (node: unknown): void => {
    if (Array.isArray(node)) {
      for (const child of node) visit(child);
      return;
    }
    if (!node || typeof node !== 'object') return;
    const b = node as Record<string, unknown>;
    if (b.tag === 'img') {
      const raw = typeof b.image_key === 'string' ? b.image_key : '';
      const key = raw ? normalizeFeishuResourceKey(raw) : '';
      if (key && !keys.includes(key)) keys.push(key);
      return;
    }
    // 不认识的 tag 也往下走：locale 包装层、content 二维数组都靠这一步穿透。
    for (const v of Object.values(b)) visit(v);
  };
  try {
    visit(JSON.parse(contentStr));
  } catch {
    return [];
  }
  return keys;
}

/**
 * Pull human-readable text out of an interactive-card content JSON (the shape
 * produced by {@link buildMarkdownCard}). Walks `elements` recursively so nested
 * columns are covered, collecting `markdown`/`text`/`plain_text` block content.
 * Returns undefined when nothing textual is found.
 */
export function extractCardText(content: unknown): string | undefined {
  let card: unknown = content;
  if (typeof content === 'string') {
    try {
      card = JSON.parse(content);
    } catch {
      return undefined;
    }
  }
  if (!card || typeof card !== 'object') return undefined;

  const parts: string[] = [];
  const visit = (node: unknown): void => {
    if (!node) return;
    if (Array.isArray(node)) {
      node.forEach(visit);
      return;
    }
    if (typeof node !== 'object') return;
    const n = node as Record<string, unknown>;
    const tag = typeof n.tag === 'string' ? n.tag : '';
    if ((tag === 'markdown' || tag === 'plain_text' || tag === 'lark_md') && typeof n.content === 'string') {
      parts.push(n.content);
    } else if (tag === 'div' && n.text && typeof (n.text as Record<string, unknown>).content === 'string') {
      parts.push((n.text as Record<string, unknown>).content as string);
    }
    // Recurse into common nesting fields.
    visit(n.elements);
    visit(n.columns);
    if (n.tag === 'column_set') visit(n.columns);
  };

  const c = card as Record<string, unknown>;
  // schema 2.0 nests under body.elements; 1.0 uses top-level elements.
  visit((c.body as Record<string, unknown> | undefined)?.elements ?? c.elements);
  const header = c.header as Record<string, unknown> | undefined;
  if (header) {
    const rawTitle = header.title;
    const title = typeof rawTitle === 'object' && rawTitle
      ? (rawTitle as Record<string, unknown>).content
      : rawTitle;
    if (typeof title === 'string' && title.trim()) parts.unshift(title.trim());
  }

  const text = parts
    .join('\n')
    // Strip the grey note wrapper buildMarkdownCard adds, keep inner text.
    .replace(/<font color='grey'>([\s\S]*?)<\/font>/g, '$1')
    .trim();
  return text || undefined;
}

function feishuReplyMessageId(message: any): string | undefined {
  const parentId = typeof message?.parent_id === 'string' ? message.parent_id.trim() : '';
  if (parentId) return parentId;
  const rootId = typeof message?.root_id === 'string' ? message.root_id.trim() : '';
  const messageId = typeof message?.message_id === 'string' ? message.message_id : '';
  if (rootId && rootId !== messageId) return rootId;
  return undefined;
}

interface ResolvedReply {
  text: string;
  /** Origin from outbound cache (if it was a cache hit, not an API fallback). */
  cachedAgentId?: string;
  cachedSessionId?: string;
  cachedTs?: number;
}

async function resolveFeishuReplyText(
  ctx: MessageHandlerContext,
  message: any,
): Promise<ResolvedReply | undefined> {
  const replyId = feishuReplyMessageId(message);
  if (!replyId) return undefined;
  // Prefer our own outbound cache: Feishu's im.message.get returns a placeholder
  // for the bot's interactive cards, so the API can't recover what we sent.
  const cached = lookupFeishuOutboundText(replyId);
  if (cached) {
    return {
      text: cached.text,
      cachedAgentId: cached.agentId,
      cachedSessionId: cached.sessionId,
      cachedTs: cached.ts,
    };
  }
  try {
    const fetched = await ctx.fetchReplyText?.(replyId);
    return { text: fetched?.trim() || `[Feishu reply message_id: ${replyId}]` };
  } catch (err) {
    console.warn({
      ts: new Date().toISOString(),
      level: 'warn',
      agent: ctx.agent.id,
      msg: 'Failed to fetch Feishu reply context',
      replyId,
      error: String(err),
    });
    return { text: `[Feishu reply message_id: ${replyId}]` };
  }
}

/**
 * Resolve the currently-active sessionId for a poolKey by checking the live
 * CC process first, then falling back to persisted state. Used for the
 * cross-session reply-context guard: we need to know which session is about
 * to receive the quoted text so we can compare against the cached origin.
 */
function resolveCurrentSessionId(ctx: MessageHandlerContext, poolKey: string): string | undefined {
  return ctx.pool.get(poolKey)?.sessionId
    ?? ctx.stateManager?.getSession(poolKey)?.sessionId
    ?? undefined;
}

/**
 * Resolve the sessionId to PERSIST for a poolKey. Priority order matters:
 *
 *   1. live CC process  — the session CC is actually running right now
 *   2. persisted state  — what we last recorded for this poolKey
 *   3. router config    — last resort only (first spawn, before CC exists)
 *
 * 2026-07-25 记忆错乱事故：这里原本直接写 `routing.feishu?.sessionId ?? routing.sessionId`
 * ——即 router 配置值。CC 在 print-mode 下会自己 randomUUID 生成 session（pool 走
 * `--session-id` 新建那条路），这个真 session 只存在于 pool 的内存里。配置值一旦与
 * 真值不同，state 就被写成"配置说的那个"，进程懒驻留驱逐后按 state resume，直接接到
 * 另一条会话上（实测接到了昨天的 jsonl，用户表现为"记忆错乱"）。
 *
 * 把 live 进程放在第一优先级，state 才会始终记住 CC 真在跑的那条。配置值退化为
 * 冷启动兜底——那时 pool 里本来就没有进程，配置值是唯一已知的起点。
 */
function resolveSessionIdToPersist(
  ctx: MessageHandlerContext,
  poolKey: string,
  routing: { sessionId?: string; feishu?: { sessionId?: string } },
): string | undefined {
  return ctx.pool.get(poolKey)?.sessionId
    ?? ctx.stateManager?.getSession(poolKey)?.sessionId
    ?? routing.feishu?.sessionId
    ?? routing.sessionId;
}

async function buildFeishuRuntimeText(
  ctx: MessageHandlerContext,
  message: any,
  text: string,
  currentSessionId?: string,
): Promise<string> {
  const resolved = await resolveFeishuReplyText(ctx, message);
  if (!resolved) return buildReplyContextPrompt(text, undefined);

  // Build origin metadata for the prompt builder. We only flag cross-session
  // when we have BOTH a cached sessionId AND a current sessionId — pre-2026-06-22
  // legacy cache entries have no sessionId, so we can't distinguish and fall
  // back to the legacy header (no false positives during the 7-day TTL drain).
  let origin: import('../reply-context.js').ReplyContextOrigin | undefined;
  if (resolved.cachedSessionId && currentSessionId) {
    origin = {
      sameSession: resolved.cachedSessionId === currentSessionId,
      quotedAtMs: resolved.cachedTs,
      quotedSessionId: resolved.cachedSessionId,
    };
  }
  return buildReplyContextPrompt(text, resolved.text, origin);
}

function countCardElements(value: unknown): number {
  if (Array.isArray(value)) return value.reduce<number>((sum, item) => sum + countCardElements(item), 0);
  if (!value || typeof value !== 'object') return 0;
  const record = value as Record<string, unknown>;
  return (typeof record.tag === 'string' ? 1 : 0)
    + Object.values(record).reduce<number>((sum, item) => sum + countCardElements(item), 0);
}

async function sendFeishuCommandResult(
  ctx: MessageHandlerContext,
  openId: string,
  result: CommandResult,
  header?: { title: string },
): Promise<void> {
  if (result.feishuCardElements?.length) {
    const card = buildMarkdownCard(result.text, { header, trailingElements: result.feishuCardElements });
    const elementCount = countCardElements((card.body as Record<string, unknown> | undefined)?.elements);
    if (elementCount > FEISHU_COMMAND_CARD_ELEMENT_BUDGET) {
      console.error({
        ts: new Date().toISOString(),
        level: 'error',
        msg: 'Refusing oversized Feishu command card',
        agent: ctx.agent.id,
        elementCount,
        budget: FEISHU_COMMAND_CARD_ELEMENT_BUDGET,
      });
      await ctx.sendTextMessage(openId, `${result.text}\n\n⚠️ 交互选项过多，已改用文字反馈；请用带关键词的 /models 检索。`);
      return;
    }
    try {
      await ctx.sendCardMessage(openId, card);
    } catch (error) {
      console.error({
        ts: new Date().toISOString(),
        level: 'error',
        msg: 'Failed to send Feishu command card; falling back to text',
        agent: ctx.agent.id,
        error: error instanceof Error ? error.message : String(error),
      });
      await ctx.sendTextMessage(openId, `${result.text}\n\n⚠️ 交互卡片发送失败，已自动回退为文字；可重新发送该命令重试。`);
    }
  } else {
    await ctx.sendTextMessage(openId, result.text);
  }
}

/**
 * Dispatch a command-menu button action (feishu-command-menu feature).
 * - First-level action/query buttons → execute the corresponding handleXxx.
 * - First-level config buttons (no arg) → send a second-level option card.
 * - Second-level option buttons (with arg) → execute the parameterized handleXxx.
 * CommandDeps MUST include modelCatalog (the pre-existing card-action commandDeps did not).
 * Exported for unit testing.
 */
export async function dispatchMenuAction(
  ctx: MessageHandlerContext,
  openId: string,
  action: string,
  arg: string,
): Promise<void> {
  const resolved = ctx.router.resolveByFeishuAgent(ctx.agent.id, openId);
  if (!resolved) {
    await ctx.sendTextMessage(openId, '\u26A0\uFE0F \u65E0\u6CD5\u89E3\u6790\u7528\u6237\uFF0C\u8BF7\u786E\u8BA4\u5DF2\u7ED1\u5B9A\u3002');
    return;
  }
  const { agent, user } = resolved;
  const userId = user.userId;
  const agentId = agent.id;

  const commandDeps = {
    router: scopedFeishuCommandRouter(ctx.router, ctx.agent),
    runtime: ctx.pool,
    stateManager: ctx.stateManager,
    meteringDir: ctx.meteringDir,
    modelCatalog: ctx.modelCatalog,
    terminalManager: ctx.terminalManager,
    inboundChannel: 'feishu' as const,
  };

  const consumerControl=feishuRecoveryControlCommand('/'+action);
  recordFeishuRecoveryBoundary(commandDeps.router.poolKey(userId,agentId),`card-menu:${action}`,process.env,
    {kind:consumerControl?'control':'input',control:consumerControl});

  // Second-level option (arg present) → execute parameterized switch.
  if (arg) {
    let result: CommandResult;
    if (action === 'model') result = await handleModel(userId, agentId, arg, commandDeps);
    else if (action === 'model_scope') result = await handleModelScope(userId, agentId, arg, commandDeps);
    else if (action === 'think') result = await handleThink(userId, agentId, arg, commandDeps);
    else if (action === 'runtime') result = await handleRuntime(userId, agentId, arg, commandDeps);
    else if (action === 'yolo') result = await handleYolo(userId, agentId, arg, commandDeps);
    else result = { text: `\u672A\u77E5\u64CD\u4F5C: ${action}` };
    await sendFeishuCommandResult(ctx, openId, result);
    return;
  }

  // First-level: action/query execute directly; config sends a second-level card.
  if (action === 'model' || action === 'runtime' || action === 'yolo' || action === 'think') {
    const card = action === 'model'
      ? await handleModel(userId, agentId, undefined, commandDeps)
      : action === 'runtime' ? buildRuntimeOptionCard(agent, commandDeps)
      : action === 'think' ? await handleThink(userId, agentId, undefined, commandDeps)
      : buildYoloOptionCard(agent);
    await sendFeishuCommandResult(ctx, openId, card);
    return;
  }

  let result: CommandResult;
  switch (action) {
    case 'new': result = await handleNew(userId, agentId, commandDeps); break;
    case 'reset': result = await handleReset(userId, agentId, commandDeps); break;
    case 'stop':
      result = await handleStop(userId, agentId, commandDeps); break;
    case 'kill':
      result = await handleKill(userId, agentId, commandDeps); break;
    case 'restart': result = await handleRestart(userId, agentId, commandDeps); break;
    case 'status': result = handleStatus(userId, agentId, commandDeps); break;
    case 'session': result = handleSession(userId, agentId, commandDeps); break;
    case 'usage': result = handleUsage(userId, agentId, commandDeps); break;
    default: result = { text: `\u672A\u77E5\u64CD\u4F5C: ${action}` };
  }
  await sendFeishuCommandResult(ctx, openId, result);
}

/** Feishu 卡片 callback（需订阅 `card.action.trigger`）；收不到时可用 /switch 兜底。 */
export async function handleFeishuCardAction(ctx: MessageHandlerContext, raw: unknown): Promise<void> {
  const obj = raw as Record<string, unknown>;
  const event = (obj?.event ?? obj) as Record<string, unknown>;
  const operator = event?.operator as Record<string, unknown> | undefined;
  const openId = typeof operator?.open_id === 'string'
    ? operator.open_id
    : typeof event?.open_id === 'string'
      ? event.open_id
      : undefined;
  const action = event?.action as Record<string, unknown> | undefined;
  const value = action?.value as Record<string, unknown> | undefined;

  // Feishu command-menu button (feishu-command-menu feature).
  const relayMenu = typeof value?.relay_menu === 'string' ? value.relay_menu : '';
  const relayMenuArg = typeof value?.relay_menu_arg === 'string' ? value.relay_menu_arg : '';
  if (relayMenu && openId) {
    await dispatchMenuAction(ctx, openId, relayMenu, relayMenuArg);
    return;
  }

  // Compatibility for already-delivered cards: legacy code-copy callbacks
  // contain only a bounded index. New answer cards no longer emit this control.
  const codeCopyIndex = typeof value?.relay_copy_code === 'number'
    && Number.isSafeInteger(value.relay_copy_code)
    && value.relay_copy_code >= 0
    ? value.relay_copy_code
    : undefined;
  if (codeCopyIndex !== undefined && openId) {
    const context = event?.context as Record<string, unknown> | undefined;
    const messageId = typeof event?.open_message_id === 'string'
      ? event.open_message_id
      : typeof (context as Record<string, unknown> | undefined)?.open_message_id === 'string'
        ? String(context!.open_message_id)
        : undefined;
    const cached = messageId ? lookupFeishuOutboundText(messageId) : undefined;
    const codeBlocks = cached
      ? collectRichDocCodeBlocks(compileRichDoc(cached.text))
      : [];
    const selected = codeBlocks[codeCopyIndex];
    if (selected?.type === 'code_block') {
      await ctx.sendTextMessage(openId, selected.value);
    } else if (cached) {
      await ctx.sendTextMessage(openId, '⚠️ 未找到对应的代码块。');
    } else {
      await ctx.sendTextMessage(openId, '⚠️ 内容正在生成中，请稍后再试。');
    }
    return;
  }

  // Compatibility for the whole-answer control on already-delivered cards.
  if (value?.relay_copy && openId) {
    const context = event?.context as Record<string, unknown> | undefined;
    const messageId = typeof event?.open_message_id === 'string'
      ? event.open_message_id
      : typeof (context as Record<string, unknown> | undefined)?.open_message_id === 'string'
        ? String(context!.open_message_id)
        : undefined;
    const cached = messageId ? lookupFeishuOutboundText(messageId) : undefined;
    if (cached) {
      await ctx.sendTextMessage(openId, cached.text);
    } else {
      await ctx.sendTextMessage(openId, '\u26A0\uFE0F \u5185\u5BB9\u6B63\u5728\u751F\u6210\u4E2D\uFF0C\u8BF7\u7A0D\u540E\u518D\u8BD5\u3002');
    }
    return;
  }

  const relaySwitch = typeof value?.relay_switch === 'string' ? value.relay_switch.trim() : '';

  // AskUserQuestion 卡片功能已删除（2026-06-26，见文件上方说明）。老卡片可能仍
  // 残留在用户飞书历史里，点击会回调 relay_ask_answer / relay_ask_submit —— 这里
  // 兜底给个友好提示后丢弃，避免触发死路径让用户看到 raw error。
  const askAnswer = typeof value?.relay_ask_answer === 'string' ? value.relay_ask_answer : '';
  const askSubmit = typeof value?.relay_ask_submit === 'string' ? value.relay_ask_submit : '';
  if ((askAnswer || askSubmit) && openId) {
    await ctx.sendTextMessage(openId, '⚠️ 图形选项面板已停用，请直接用文字回复。');
    return;
  }

  if (!openId || !relaySwitch) return;

  const resolved = ctx.router.resolveByFeishuAgent(ctx.agent.id, openId);
  if (!resolved) return;

  const commandDeps = {
    router: scopedFeishuCommandRouter(ctx.router, ctx.agent),
    runtime: ctx.pool,
    stateManager: ctx.stateManager,
    meteringDir: ctx.meteringDir,
  };
  const switched = handleSwitch(resolved.user.userId, relaySwitch, commandDeps);
  const result = withAgentPickerUi(switched, resolved.user.userId, ctx.router, ctx.agent);
  await sendFeishuCommandResult(ctx, openId, result);
}

const FEISHU_MINUTES_URL_RE = /https?:\/\/(?:[\w-]+\.)?feishu\.cn\/minutes\/(\w{20,})/i;

async function handleMinutesTranscript(
  ctx: MessageHandlerContext,
  openId: string,
  minuteToken: string,
  resolved: { user: UserConfig },
  message: any,
): Promise<void> {
  const msgId = message?.message_id;
  const routing=resolveInboundRoutingAgent(ctx.router,resolved.user.userId,ctx.agent,FEISHU_INBOUND_ROUTING);
  const dispatchBoundary=feishuRecoveryBoundaryToken(feishuPoolKey(ctx.router,resolved.user.userId,routing));
  ctx.startTyping?.(msgId);
  try {
    const resp = (await ctx.larkClient.request({
      method: 'GET',
      url: `/open-apis/minutes/v1/minutes/${minuteToken}/transcript`,
      params: {
        file_format: 'txt',
        need_speaker: true,
        need_timestamp: true,
      },
    })) as { code?: number; msg?: string; data?: string | { content?: string } };

    let transcriptText: string;
    if (resp?.code === 0 && typeof resp?.data === 'string') {
      transcriptText = resp.data;
    } else if (resp?.code === 0 && typeof resp?.data === 'object' && typeof resp.data?.content === 'string') {
      transcriptText = resp.data.content;
    } else if (typeof resp === 'string') {
      transcriptText = resp;
    } else {
      throw new Error(`Unexpected transcript response: ${JSON.stringify(resp).slice(0, 200)}`);
    }

    if (!transcriptText.trim()) {
      await ctx.sendTextMessage(openId, '\u26A0\uFE0F \u8BE5\u5999\u8BB0\u6682\u65E0\u8F6C\u5199\u5185\u5BB9\u3002');
      return;
    }

    const route = resolveGatewayRoute(ctx, openId, resolved.user.userId);
    let startupError:unknown;
    const poolKey = await ensureProcess(ctx, resolved, route,error=>{startupError=error;});
    if (!poolKey) {
      if((startupError as {code?:string}|undefined)?.code==='canonical_attachment_interrupted')return;
      await ctx.sendTextMessage(openId, runtimeEntryFailureMessage(startupError));
      return;
    }

    const sendCurrent=captureFeishuBusinessDispatch(ctx,poolKey,dispatchBoundary,msgId);
    const header = `\u{1F4C4} \u5999\u8BB0\u8F6C\u5199 (${minuteToken.slice(0, 8)}...)\n---\n`;
    const content = await buildFeishuRuntimeText(ctx, message, header + transcriptText, resolveCurrentSessionId(ctx, poolKey));
    submitFeishuTurn(sendCurrent({ type: 'human', content }), () => persistFeishuSession(ctx, poolKey, routing),
      feishuDispatchFailed(ctx, openId, poolKey, msgId));
  } catch (err) {
    console.error({
      ts: new Date().toISOString(),
      level: 'error',
      agent: ctx.agent.id,
      msg: 'Failed to fetch minutes transcript',
      minuteToken: minuteToken.slice(0, 8) + '...',
      error: String(err),
    });
    const errorMsg = String(err);
    if (errorMsg.includes('permission') || errorMsg.includes('2091005')) {
      await ctx.sendTextMessage(openId, '\u26A0\uFE0F \u65E0\u6743\u9650\u8BBF\u95EE\u8BE5\u5999\u8BB0\u8F6C\u5199\uFF0C\u8BF7\u786E\u8BA4 Bot \u5DF2\u5F00\u901A minutes \u6743\u9650\u4E14\u5999\u8BB0\u5BF9 Bot \u53EF\u89C1\u3002');
    } else if (errorMsg.includes('not ready') || errorMsg.includes('2091003')) {
      await ctx.sendTextMessage(openId, '\u26A0\uFE0F \u5999\u8BB0\u8F6C\u5199\u5C1A\u672A\u751F\u6210\uFF0C\u8BF7\u7A0D\u540E\u518D\u8BD5\u3002');
    } else {
      await ctx.sendTextMessage(openId, '\u26A0\uFE0F \u83B7\u53D6\u5999\u8BB0\u8F6C\u5199\u5931\u8D25\uFF0C\u8BF7\u7A0D\u540E\u91CD\u8BD5\u3002');
    }
  } finally {
    ctx.stopTyping?.(msgId);
  }
}

// --- Message handler (exported for testability) ---

/**
 * Stale and duplicate gates for one inbound message (see the dedupe constants).
 * Runs when the event arrives, before any queueing, so neither gate measures
 * time spent waiting behind earlier messages.
 */
export function admitFeishuInboundMessage(agentId: string, data: { message: any; sender: any }, now = Date.now()): boolean {
  const message = data.message;
  const openId = data.sender?.sender_id?.open_id;
  const msgId = message?.message_id;
  if (!openId) return false;

  // The stale gate runs first: a redelivered old message must not refresh its
  // id in the table. Without a readable create_time the message passes —
  // a missing field never swallows a real message.
  const createdAt = parseFeishuCreateTime(message?.create_time);
  if (createdAt !== null) {
    const age = now - createdAt;
    if (age > FEISHU_MESSAGE_MAX_AGE_MS) {
      console.log({
        ts: new Date().toISOString(),
        level: 'warn',
        agent: agentId,
        msg: 'Stale Feishu message ignored (redelivery)',
        msgId: typeof msgId === 'string' ? msgId : undefined,
        createTime: new Date(createdAt).toISOString(),
        ageMs: age,
        openId: openId.slice(0, 8) + '...',
      });
      return false;
    }
  }

  if (typeof msgId === 'string' && msgId.trim()) {
    const key = `${agentId}:${msgId.trim()}`;
    const prev = recentInboundMessageIds.get(key);
    if (prev && now - prev < FEISHU_MESSAGE_DEDUPE_WINDOW_MS) {
      console.log({
        ts: new Date().toISOString(),
        level: 'debug',
        agent: agentId,
        msg: 'Duplicate Feishu message ignored',
        msgId: msgId.trim(),
        openId: openId.slice(0, 8) + '...',
      });
      return false;
    }
    recentInboundMessageIds.set(key, now);
  }
  return true;
}

export async function handleFeishuMessage(
  ctx: MessageHandlerContext,
  data: { message: any; sender: any },
): Promise<void> {
  if (!admitFeishuInboundMessage(ctx.agent.id, data)) return;
  await processFeishuMessage(ctx, data);
}

/** Commands answered at once, never queued behind the sender's earlier messages. */
const FEISHU_IMMEDIATE_COMMANDS: ReadonlySet<string> = new Set(['stop', 'kill', 'status', 'help']);

function feishuImmediateCommand(message: any): boolean {
  if (message?.message_type !== 'text') return false;
  let text: unknown;
  try { text = (JSON.parse(message.content ?? '') as { text?: unknown }).text; } catch { return false; }
  const match = typeof text === 'string' ? /^\/([A-Za-z0-9_]+)(?:\s|$)/.exec(text.trim()) : null;
  return !!match && FEISHU_IMMEDIATE_COMMANDS.has(match[1]!.toLowerCase());
}

/**
 * Feishu intake that acknowledges each event when it arrives.
 *
 * The SDK acknowledges a websocket event only after its handler returns, and
 * Feishu redelivers an event not acknowledged within 3 seconds. A handler that
 * waited for an Agent turn therefore drew redeliveries. Here the gates run on
 * arrival, the message goes to its sender's lane, and the handler returns:
 * one sender's messages keep their order, senders run independently, and
 * /stop /kill /status /help bypass the lane so they act during a long turn.
 *
 * Once acknowledged, Feishu never sends the event again, so a queued message
 * is written to the bot's intake journal before the handler returns and stays
 * there until its handling hands a turn to the runtime (or ends without one).
 * A failed write throws: the SDK answers with an error and Feishu redelivers.
 * A restarted bot replays what is left, in order (see replayIntakeJournal).
 */
export function createFeishuIntake(ctx: MessageHandlerContext, lanes: ChatLanes, journal: ChannelIntakeJournal): (data: { message: any; sender: any }) => void {
  return data => {
    const msgId = typeof data.message?.message_id === 'string' && data.message.message_id.trim()
      ? data.message.message_id.trim() as string : undefined;
    // Redelivered after a restart while the journal still holds it.
    if (msgId && journal.has(msgId)) return;
    if (!admitFeishuInboundMessage(ctx.agent.id, data)) return;
    const openId = data.sender?.sender_id?.open_id as string;
    if (feishuImmediateCommand(data.message)) {
      void processFeishuMessage(ctx, data).catch(error => feishuIntakeFailed(ctx.agent.id, openId, error));
      return;
    }
    const event = { message: data.message, sender: data.sender };
    const record = journal.accept(openId, msgId ?? randomUUID(), event);
    try {
      journal.persist();
    } catch (error) {
      // Not acknowledged: the redelivery must pass the duplicate gate.
      journal.release(record);
      if (msgId) recentInboundMessageIds.delete(`${ctx.agent.id}:${msgId}`);
      feishuIntakeFailed(ctx.agent.id, openId, error);
      throw error;
    }
    lanes.run(openId, () => runJournaled(journal, record, () => processFeishuMessage(ctx, event)));
  };
}

/**
 * Queue the messages an earlier process left in the journal, each in its
 * sender's lane behind `ready` (see replayIntakeJournal). They passed the
 * arrival gates before; time spent waiting for the restart is no staleness.
 */
export function replayFeishuIntake(ctx: MessageHandlerContext, lanes: ChatLanes, journal: ChannelIntakeJournal, ready: Promise<void>): number {
  return replayIntakeJournal(lanes, journal, ready,
    record => processFeishuMessage(ctx, record.payload as { message: any; sender: any }));
}

function feishuIntakeFailed(agentId: string, openId: string, err: unknown): void {
  console.error({
    ts: new Date().toISOString(),
    level: 'error',
    agent: agentId,
    msg: 'handleFeishuMessage threw (message dropped silently before this log)',
    openId: openId.slice(0, 8) + '...',
    error: err instanceof Error ? err.message : String(err),
    stack: err instanceof Error ? err.stack : undefined,
  });
}

/**
 * Hand a turn to the runtime without waiting for its dispatch, so the sender's
 * lane moves on. The call fixes the turn's place in its session: the runtime
 * queues keep call order and a busy session holds the turn until the earlier
 * ones finish. `onSent` runs once the turn is dispatched; `onFailure` when the
 * dispatch fails.
 */
function submitFeishuTurn(
  dispatch: Promise<boolean>,
  onSent: () => void,
  onFailure: (error: unknown) => Promise<unknown>,
): void {
  dispatch.then(sent => { if (sent) onSent(); }, async (error: unknown) => {
    try { await onFailure(error); } catch { /* the failure notice is best effort */ }
  });
}

/** Failure of a submitted turn: log, clear the typing mark, tell the sender. */
function feishuDispatchFailed(ctx: MessageHandlerContext, openId: string, poolKey: string, msgId: string | undefined,
  notice = '\u26A0\uFE0F \u6D88\u606F\u53D1\u9001\u5931\u8D25\uFF0C\u8BF7\u7A0D\u540E\u91CD\u8BD5\u3002'): (err: unknown) => Promise<void> {
  return async err => {
    console.error({
      ts: new Date().toISOString(),
      level: 'error',
      agent: ctx.agent.id,
      poolKey,
      msg: 'Feishu pool.send failed',
      error: err instanceof Error ? err.message : String(err),
      stack: err instanceof Error ? err.stack : undefined,
    });
    ctx.stopTyping?.(msgId);
    await ctx.sendTextMessage(openId, notice);
  };
}

/** Record the session as active once its turn is dispatched. */
function persistFeishuSession(ctx: MessageHandlerContext, poolKey: string, routing: AgentConfig): void {
  ctx.stateManager?.updateSession(poolKey, {
    poolKey,
    agentId: routing.id,
    sessionId: resolveSessionIdToPersist(ctx, poolKey, routing),
    lastActivity: Date.now(),
    status: 'active',
  });
}

async function processFeishuMessage(
  ctx: MessageHandlerContext,
  data: { message: any; sender: any },
): Promise<void> {
  const { agent, router, stateManager } = ctx;
  const message = data.message;
  const sender = data.sender;
  const openId = sender?.sender_id?.open_id;
  const msgType = message?.message_type;
  const msgId = message?.message_id;
  const contentStr = message?.content ?? '';
  const createdAt = parseFeishuCreateTime(message?.create_time);

  if (!openId) return;

  console.log({
    ts: new Date().toISOString(),
    level: 'debug',
    agent: agent.id,
    msg: 'Received Feishu message',
    msgType,
    // msgId / createTime 是这次查证的关键——先前只打 msgType/内容/openId，
    // 出事时无从判断「这条是刚发的还是重投的」，只能去飞书 API 回捞。
    msgId: typeof msgId === 'string' ? msgId : undefined,
    createTime: createdAt !== null ? new Date(createdAt).toISOString() : undefined,
    contentPreview: typeof contentStr === 'string' ? contentStr.slice(0, 300) : String(contentStr),
    openId: openId.slice(0, 8) + '...',
  });

  const resolved = router.resolveByFeishuAgent(agent.id, openId);
  if (!resolved) {
    // Pairing flow: generate a code for the unregistered user.
    //
    // Two acceptance paths:
    //   - User pastes the code into the Platform onboarding UI (Step 3),
    //     which calls Relay HTTP /internal/pairing/approve.
    //   - Ops pastes `/approve <code>` from an already-bound agent (legacy).
    const code = createPendingPairing(agent.id, openId);
    await ctx.sendTextMessage(openId, feishuPairingMessage(code));
    console.log({ ts: new Date().toISOString(), level: 'info', agent: agent.id, msg: 'Pairing code issued', code, openId });
    return;
  }

  const routing = resolveInboundRoutingAgent(router, resolved.user.userId, agent, FEISHU_INBOUND_ROUTING);

  if(typeof msgId==='string'&&msgId)recordFeishuRecoveryBoundary(
    feishuPoolKey(router,resolved.user.userId,routing),msgId,process.env,{kind:'input'});
  const dispatchBoundary=feishuRecoveryBoundaryToken(feishuPoolKey(router,resolved.user.userId,routing));

  // 切片1 入站埋点：一条来自已绑定用户的飞书请求抵达。旁路观测,dark-deploy 开关控制,
  // 全隔离永不抛（traceInboundRequest 内部已 try/catch）。openId 只取前缀,不落全量 PII。
  traceInboundRequest({
    channel: 'feishu',
    agent: routing.id,
    // 切片A scope 归一：落带 scope 的 feishuPoolKey（dual-channel 带 :feishu 后缀）,与 CC turn 实际
    // spawn 的池键（同 feishuPoolKey(router, userId, routing)）+ turn.end/delivery 埋点逐字符对齐。
    // 旧代码用 router.poolKey() 落 bare（漏 :feishu）,配不上 turn.end/delivery——是三事件唯一异类。
    session: feishuPoolKey(router, resolved.user.userId, routing),
    who: openId.slice(0, 8),
    detail: { msgType },
  });

  if (msgType === 'text') {
    const parsed = JSON.parse(contentStr);
    const text = parsed.text ?? '';

    // Auto-detect Feishu Minutes URL and fetch transcript
    const minutesMatch = text.match(FEISHU_MINUTES_URL_RE);
    if (minutesMatch) {
      await handleMinutesTranscript(ctx, openId, minutesMatch[1], resolved, message);
      return;
    }

    const route = resolveGatewayRoute(ctx, openId, resolved.user.userId, text);

    if (text.startsWith('/')) {
      const cmd = text.slice(1).split(/\s+/)[0];
      // Keep legacy runtimes on their existing passthrough path. Pi RPC needs
      // explicit native commands; sending these strings as prompts is not equivalent.
      if ((cmd === 'compact' || cmd === 'context') && !targetsPiRuntime(ctx, resolved.user.userId)) {
        // fall through to normal CC message flow below
      } else {
        await handleCommand(ctx, text, openId, resolved,typeof msgId==='string'?msgId:undefined);
        return;
      }
    }

    // Save before startup; enrichment can await I/O without changing this
    // input's authenticated proof or taking the next input's WI assignment.
    const source = typeof msgId === 'string' && msgId ? {channel:'feishu' as const,eventId:msgId,humanRef:resolved.user.userId,message:text} : undefined;
    const outgoing = annotatedHumanMessage('',source,feishuPoolKey(router,resolved.user.userId,routing));
    const provenanceSuffix = outgoing.content ?? '';
    const originalInputRef=latestHumanInput(feishuPoolKey(router,resolved.user.userId,routing));

    let startupError:unknown;
    const poolKey = await ensureProcess(ctx, resolved, route,error=>{startupError=error;});
    if (!poolKey) {
      if((startupError as {code?:string}|undefined)?.code==='canonical_attachment_interrupted')return;
      const failedPool=feishuPoolKey(router,resolved.user.userId,routing);
      const retained=stateManager?.getSession(failedPool);
      const queued=originalInputRef&&retained?.sessionId&&ctx.recordStartupFailure?.({
        poolKey:failedPool,sessionId:retained.sessionId,humanInputRef:originalInputRef});
      const failure=runtimeEntryFailureMessage(startupError);
      await ctx.sendTextMessage(openId, queued
        ?failure.replace('尚未确认后台重试。','原消息已保存，Host 将自动恢复并继续；无需重发。'):failure);
      return;
    }
    const sendCurrent=captureFeishuBusinessDispatch(ctx,poolKey,dispatchBoundary,msgId);
    const failed = feishuDispatchFailed(ctx, openId, poolKey, msgId);

    try {
      ctx.startTyping?.(msgId);
      outgoing.content = await buildFeishuRuntimeText(ctx,message,text,resolveCurrentSessionId(ctx,poolKey)) + provenanceSuffix;
      submitFeishuTurn(sendCurrent(outgoing), () => persistFeishuSession(ctx, poolKey, routing), failed);
    } catch (err) {
      await failed(err);
    }
    return;
  }

  if (msgType === 'post') {
    const text = extractPostText(contentStr);
    const postImageKeys = collectPostImageKeys(contentStr);
    // 纯图片 post（发图不带字）文字为空，但不该按「解析失败」打回——图片才是正文。
    if (!text && postImageKeys.length === 0) {
      console.warn({
        ts: new Date().toISOString(),
        level: 'warn',
        agent: agent.id,
        msg: 'Failed to extract text from post message',
        contentPreview: contentStr.slice(0, 200),
      });
      await ctx.sendTextMessage(openId, '\u26A0\uFE0F \u65E0\u6CD5\u89E3\u6790\u5BCC\u6587\u672C\u6D88\u606F\uFF0C\u8BF7\u53D1\u9001\u6587\u5B57\u6D88\u606F\u3002');
      return;
    }
    const postText = text ?? '';
    const route = resolveGatewayRoute(ctx, openId, resolved.user.userId, postText);

    if (postText.startsWith('/')) {
      const cmd = postText.slice(1).split(/\s+/)[0];
      if ((cmd === 'compact' || cmd === 'context') && !targetsPiRuntime(ctx, resolved.user.userId)) {
        // fall through
      } else {
        await handleCommand(ctx, postText, openId, resolved,typeof msgId==='string'?msgId:undefined);
        return;
      }
    }

    // Rich-text is the same authenticated human ingress as a text message.
    // Keep its original extracted body and proof before startup/enrichment.
    const source = postText.trim() && typeof msgId === 'string' && msgId
      ? {channel:'feishu' as const,eventId:msgId,humanRef:resolved.user.userId,message:postText}
      : undefined;
    const outgoing = annotatedHumanMessage('',source,feishuPoolKey(router,resolved.user.userId,routing));
    const provenanceSuffix = outgoing.content ?? '';

    let startupError:unknown;
    const poolKey = await ensureProcess(ctx, resolved, route,error=>{startupError=error;});
    if (!poolKey) {
      if((startupError as {code?:string}|undefined)?.code==='canonical_attachment_interrupted')return;
      await ctx.sendTextMessage(openId, runtimeEntryFailureMessage(startupError));
      return;
    }
    const sendCurrent=captureFeishuBusinessDispatch(ctx,poolKey,dispatchBoundary,msgId);
    const failed = feishuDispatchFailed(ctx, openId, poolKey, msgId);

    try {
      ctx.startTyping?.(msgId);
      const runtimeText = await buildFeishuRuntimeText(
        ctx,
        message,
        postText,
        resolveCurrentSessionId(ctx, poolKey),
      ) + provenanceSuffix;
      if (postImageKeys.length === 0) {
        outgoing.content = runtimeText;
        submitFeishuTurn(sendCurrent(outgoing), () => persistFeishuSession(ctx, poolKey, routing), failed);
      } else {
        // \u56FE\u6587\u6DF7\u6392\uFF1A\u628A\u56FE\u7247\u6309 msgType='image' \u7684\u540C\u4E00\u5957\u8DEF\u5B50\u4E0B\u8F7D\u6210 image block\uFF0C
        // \u4E0E\u6587\u5B57\u5E76\u6392\u9001\u8FDB\u53BB\u3002\u4E0B\u8F7D\u5931\u8D25\u7684\u5355\u5F20\u964D\u7EA7\u6210\u5360\u4F4D\u8BF4\u660E\uFF0C\u4E0D\u56E0\u4E00\u5F20\u56FE\u4E22\u6389\u6574\u6761\u6D88\u606F\u3002
        const contentBlocks: unknown[] = [];
        for (const imageKey of postImageKeys) {
          try {
            const { base64, mediaType } = await ctx.downloadImage(msgId, imageKey);
            const ext = mediaType.split('/')[1]?.replace('jpeg', 'jpg') ?? 'png';
            const imageBuffer = Buffer.from(base64, 'base64');
            prepareRelayImageDir(imageBuffer.length);
            const imgName = `${Date.now()}-${randomUUID().slice(0, 8)}.${ext}`;
            const imgPath = join(RELAY_IMAGE_DIR, imgName);
            writeFileSync(imgPath, imageBuffer, { mode: 0o644 });
            contentBlocks.push({
              type: 'image',
              source: { type: 'base64', media_type: mediaType, data: base64 },
              filePath: imgPath,
            });
          } catch (err) {
            console.warn({
              ts: new Date().toISOString(),
              level: 'warn',
              agent: agent.id,
              msg: 'Failed to download inline post image',
              imageKey,
              error: err instanceof Error ? err.message : String(err),
            });
            contentBlocks.push({ type: 'text', text: `[Feishu image: ${imageKey} \u4E0B\u8F7D\u5931\u8D25]` });
          }
        }
        contentBlocks.push({ type: 'text', text: runtimeText });
        outgoing.content = contentBlocks;
        submitFeishuTurn(sendCurrent(outgoing), () => persistFeishuSession(ctx, poolKey, routing), failed);
      }
    } catch (err) {
      await failed(err);
    }
    return;
  }

  if (msgType === 'image') {
    const route = resolveGatewayRoute(ctx, openId, resolved.user.userId);
    let parsed: { image_key?: string };
    try {
      parsed = JSON.parse(contentStr) as { image_key?: string };
    } catch {
      await ctx.sendTextMessage(openId, '\u26A0\uFE0F \u65E0\u6CD5\u89E3\u6790\u56FE\u7247\u6D88\u606F\u5185\u5BB9\u3002');
      return;
    }
    const imageKey = typeof parsed.image_key === 'string' ? parsed.image_key : '';

    let startupError:unknown;
    const poolKey = await ensureProcess(ctx, resolved, route,error=>{startupError=error;});
    if (!poolKey) {
      if((startupError as {code?:string}|undefined)?.code==='canonical_attachment_interrupted')return;
      await ctx.sendTextMessage(openId, runtimeEntryFailureMessage(startupError));
      return;
    }
    const sendCurrent=captureFeishuBusinessDispatch(ctx,poolKey,dispatchBoundary,msgId);
    const failed = feishuDispatchFailed(ctx, openId, poolKey, msgId,
      '\u26A0\uFE0F \u56FE\u7247\u5904\u7406\u5931\u8D25\uFF0C\u8BF7\u7A0D\u540E\u91CD\u8BD5\u3002');

    try {
      ctx.startTyping?.(msgId);
      const { base64, mediaType } = await ctx.downloadImage(msgId, imageKey);
      // Save image to temp file so CC can read it via its native Read tool.
      const ext = mediaType.split('/')[1]?.replace('jpeg', 'jpg') ?? 'png';
      const imageBuffer = Buffer.from(base64, 'base64');
      prepareRelayImageDir(imageBuffer.length);
      const fileName = `${Date.now()}-${randomUUID().slice(0, 8)}.${ext}`;
      const filePath = join(RELAY_IMAGE_DIR, fileName);
      writeFileSync(filePath, imageBuffer, { mode: 0o644 });
      const contentBlocks: unknown[] = [
        { type: 'image', source: { type: 'base64', media_type: mediaType, data: base64 }, filePath },
      ];
      submitFeishuTurn(sendCurrent({ type: 'human', content: contentBlocks }), () => {}, failed);
    } catch (err) {
      await failed(err);
    }
    return;
  }

  if (msgType === 'file') {
    const route = resolveGatewayRoute(ctx, openId, resolved.user.userId);
    let parsed: { file_key?: string; file_name?: string };
    try {
      parsed = JSON.parse(contentStr) as { file_key?: string; file_name?: string };
    } catch {
      await ctx.sendTextMessage(openId, '\u26A0\uFE0F \u65E0\u6CD5\u89E3\u6790\u6587\u4EF6\u6D88\u606F\u5185\u5BB9\u3002');
      return;
    }
    const fileKey = typeof parsed.file_key === 'string' ? normalizeFeishuResourceKey(parsed.file_key) : '';
    const fileName = typeof parsed.file_name === 'string' ? parsed.file_name : undefined;
    if (!fileKey) {
      await ctx.sendTextMessage(openId, '\u26A0\uFE0F \u6587\u4EF6\u6D88\u606F\u7F3A\u5C11 file_key\u3002');
      return;
    }

    let startupError:unknown;
    const poolKey = await ensureProcess(ctx, resolved, route,error=>{startupError=error;});
    if (!poolKey) {
      if((startupError as {code?:string}|undefined)?.code==='canonical_attachment_interrupted')return;
      await ctx.sendTextMessage(openId, runtimeEntryFailureMessage(startupError));
      return;
    }
    const sendCurrent=captureFeishuBusinessDispatch(ctx,poolKey,dispatchBoundary,msgId);

    const maxBytes = feishuInboundFileMaxBytes();
    try {
      ctx.startTyping?.(msgId);
      const { buffer, contentType } = await ctx.downloadFileResource(msgId, fileKey);
      if (buffer.length > maxBytes) {
        ctx.stopTyping?.(msgId);
        const mb = (maxBytes / (1024 * 1024)).toFixed(0);
        await ctx.sendTextMessage(openId, `\u26A0\uFE0F \u6587\u4EF6\u8FC7\u5927\uFF08\u8D85\u8FC7 ${mb} MB\uFF09\uFF0C\u8BF7\u6362\u5C0F\u6587\u4EF6\u6216\u538B\u7F29\u540E\u91CD\u8BD5\u3002`);
        return;
      }
      const classification = classifyFeishuInboundFile({ contentType, fileName });
      if (classification.kind === 'unsupported') {
        ctx.stopTyping?.(msgId);
        await ctx.sendTextMessage(
          openId,
          `\u6682\u4E0D\u652F\u6301\u8BE5\u6587\u4EF6\u7C7B\u578B (${classification.mimeLabel})\uFF0C\u652F\u6301\u56FE\u7247\u3001PDF\u3001\u6587\u672C\u6587\u4EF6\u3002`,
        );
        return;
      }
      const contentBlocks = humanContentBlocksFromFeishuFile(buffer, classification, fileName);
      submitFeishuTurn(sendCurrent({ type: 'human', content: contentBlocks }), () => persistFeishuSession(ctx, poolKey, routing),
        feishuDispatchFailed(ctx, openId, poolKey, msgId,
          '\u26A0\uFE0F \u6587\u4EF6\u5904\u7406\u5931\u8D25\uFF0C\u8BF7\u7A0D\u540E\u91CD\u8BD5\u3002'));
    } catch (err) {
      ctx.stopTyping?.(msgId);
      console.error({
        ts: new Date().toISOString(),
        level: 'error',
        agent: agent.id,
        msg: 'Failed to download Feishu file',
        error: String(err),
      });
      await ctx.sendTextMessage(openId, '\u26A0\uFE0F \u6587\u4EF6\u5904\u7406\u5931\u8D25\uFF0C\u8BF7\u7A0D\u540E\u91CD\u8BD5\u3002');
      return;
    }
    return;
  }

  if (msgType === 'audio') {
    const route = resolveGatewayRoute(ctx, openId, resolved.user.userId);
    let parsed: { file_key?: string };
    try {
      parsed = JSON.parse(contentStr) as { file_key?: string };
    } catch {
      await ctx.sendTextMessage(openId, '\u26A0\uFE0F \u65E0\u6CD5\u89E3\u6790\u97F3\u9891\u6D88\u606F\u5185\u5BB9\u3002');
      return;
    }
    const fileKey = typeof parsed.file_key === 'string' ? normalizeFeishuResourceKey(parsed.file_key) : '';
    if (!fileKey) {
      await ctx.sendTextMessage(openId, '\u26A0\uFE0F \u97F3\u9891\u6D88\u606F\u7F3A\u5C11 file_key\u3002');
      return;
    }

    let startupError:unknown;
    const poolKey = await ensureProcess(ctx, resolved, route,error=>{startupError=error;});
    if (!poolKey) {
      if((startupError as {code?:string}|undefined)?.code==='canonical_attachment_interrupted')return;
      await ctx.sendTextMessage(openId, runtimeEntryFailureMessage(startupError));
      return;
    }
    const sendCurrent=captureFeishuBusinessDispatch(ctx,poolKey,dispatchBoundary,msgId);

    const maxBytes = feishuInboundFileMaxBytes();
    try {
      ctx.startTyping?.(msgId);
      const { buffer } = await ctx.downloadFileResource(msgId, fileKey);
      if (buffer.length > maxBytes) {
        ctx.stopTyping?.(msgId);
        const mb = (maxBytes / (1024 * 1024)).toFixed(0);
        await ctx.sendTextMessage(openId, `\u26A0\uFE0F \u97F3\u9891\u6587\u4EF6\u8FC7\u5927\uFF08\u8D85\u8FC7 ${mb} MB\uFF09\uFF0C\u8BF7\u91CD\u8BD5\u3002`);
        return;
      }
      const transcript = await ctx.transcribeInboundAudio(buffer);
      if (!transcript.trim()) {
        ctx.stopTyping?.(msgId);
        await ctx.sendTextMessage(openId, '\u26A0\uFE0F \u672A\u80FD\u8BC6\u522B\u8BED\u97F3\u5185\u5BB9\uFF0C\u8BF7\u91CD\u8BD5\u3002');
        return;
      }
      const content = await buildFeishuRuntimeText(ctx, message, transcript, resolveCurrentSessionId(ctx, poolKey));
      submitFeishuTurn(sendCurrent({ type: 'human', content }), () => persistFeishuSession(ctx, poolKey, routing),
        feishuDispatchFailed(ctx, openId, poolKey, msgId,
          '\u26A0\uFE0F \u97F3\u9891\u5904\u7406\u5931\u8D25\uFF0C\u8BF7\u7A0D\u540E\u91CD\u8BD5\u3002'));
    } catch (err) {
      ctx.stopTyping?.(msgId);
      console.error({
        ts: new Date().toISOString(),
        level: 'error',
        agent: agent.id,
        msg: 'Failed to process Feishu audio',
        error: String(err),
      });
      await ctx.sendTextMessage(openId, '\u26A0\uFE0F \u97F3\u9891\u5904\u7406\u5931\u8D25\uFF0C\u8BF7\u7A0D\u540E\u91CD\u8BD5\u3002');
      return;
    }
    return;
  }

  await ctx.sendTextMessage(openId, `\u6682\u4E0D\u652F\u6301\u8BE5\u6D88\u606F\u7C7B\u578B (${msgType ?? '未知'})\uFF0C\u8BF7\u53D1\u9001\u6587\u5B57\u6D88\u606F\u3002`);
}

// --- Command handler (exported for testability) ---

export async function handleCommand(
  ctx: MessageHandlerContext,
  text: string,
  openId: string,
  resolved: { user: UserConfig },
  inboundEventId?: string,
): Promise<void> {
  const { agent, pool, router, stateManager, meteringDir } = ctx;
  const cmd = text.slice(1).split(' ')[0];
  const userId = resolved.user.userId;
  const agentId = agent.id;
  const commandDeps = {
    router: scopedFeishuCommandRouter(router, agent),
    runtime: pool,
    stateManager,
    meteringDir,
    modelCatalog: ctx.modelCatalog,
    terminalManager: ctx.terminalManager,
    inboundChannel: 'feishu' as const,
  };
  const routeAgent = resolveInboundRoutingAgent(router, userId, agent, FEISHU_INBOUND_ROUTING);
  const routeAgentId = routeAgent.id;
  const consumerControl=feishuRecoveryControlCommand(text);
  if(inboundEventId&&consumerControl)recordFeishuRecoveryBoundary(
    commandDeps.router.poolKey(userId,routeAgentId),inboundEventId,process.env,
    {kind:'control',control:consumerControl});
  invalidateHumanInput(commandDeps.router.poolKey(userId,routeAgentId));
  // Slash commands (especially /stop) are also durable human boundaries;
  // restart recovery must not revive an older signed business decision.
  if(inboundEventId)recordHumanInput({channel:'feishu',eventId:inboundEventId,
    humanRef:userId,message:text},commandDeps.router.poolKey(userId,routeAgentId));
  if (cmd === 'wi') {
    try {
      await dispatchNativeWorkItemCommand({
        agent:routeAgent,poolKey:commandDeps.router.poolKey(userId,routeAgentId),runtime:pool,
        source:inboundEventId?{channel:'feishu',eventId:inboundEventId,humanRef:userId,message:text}:undefined,
        stateManager:commandDeps.stateManager,reply:message=>ctx.sendTextMessage(openId,message),
        acquire:async()=>{
          const key=await ensureProcess(ctx,resolved);
          if(key)ctx.poolKeyToOpenId.set(key,openId);
          return key;
        },
      });
    } catch { await ctx.sendTextMessage(openId,NATIVE_WI_UNAVAILABLE); }
    return;
  }

  let result: CommandResult;
  switch (cmd) {
    case 'start': result = handleStart(userId, agentId, commandDeps); break;
    case 'list':
    case 'switch':
      await ctx.sendTextMessage(openId, '当前 bot 已绑定固定 Agent，无需切换。');
      return;
    case 'status': result = handleStatus(userId, routeAgentId, commandDeps); break;
    case 'new': result = await handleNew(userId, routeAgentId, commandDeps); break;
    case 'stop': result = await handleStop(userId, routeAgentId, commandDeps); break;
    case 'kill': result = await handleKill(userId, routeAgentId, commandDeps); break;
    case 'restart': result = await handleRestart(userId, routeAgentId, commandDeps); break;
    case 'menu': {
      const menuCard = buildMenuCard(agent);
      await sendFeishuCommandResult(ctx, openId, menuCard, { title: `${agent.emoji} ${agent.name} 菜单` });
      return;
    }
    case 'help': result = handleHelp(); break;
    case 'session': result = handleSession(userId, routeAgentId, commandDeps); break;
    case 'cc': result = await handleCc(userId, routeAgentId, commandDeps); break;
    case 'usage': result = handleUsage(userId, routeAgentId, commandDeps); break;
    case 'usage_seal': result = await handleUsageSeal(userId, routeAgentId, commandDeps); break;
    case 'balance': {
      try {
        const balResult = await handleFeishuBalance(ctx, userId, routeAgentId);
        await ctx.sendTextMessage(openId, balResult);
      } catch (err) {
        const msg = err instanceof Error ? err.message : String(err);
        await ctx.sendTextMessage(openId, `⚠️ 余额查询失败: ${msg}`);
      }
      return;
    }
    case 'reset': result = await handleReset(userId, routeAgentId, commandDeps); break;
    case 'runtime': {
      const rtArg = text.slice(1).split(/\s+/).slice(1).join(' ').trim();
      result = await handleRuntime(userId, routeAgentId, rtArg || undefined, commandDeps);
      break;
    }
    case 'yolo': {
      const yoloArg = text.slice(1).split(/\s+/).slice(1).join(' ').trim();
      result = await handleYolo(userId, routeAgentId, yoloArg || undefined, commandDeps);
      break;
    }
    case 'model': {
      const modelArg = text.slice(1).split(/\s+/).slice(1).join(' ').trim();
      result = await handleModel(userId, routeAgentId, modelArg || undefined, commandDeps);
      break;
    }
    case 'models': {
      const modelsArg = text.slice(1).split(/\s+/).slice(1).join(' ').trim();
      result = await handleModels(userId, routeAgentId, commandDeps, modelsArg || undefined);
      break;
    }
    case 'think': {
      const thinkArg = text.slice(1).split(/\s+/).slice(1).join(' ').trim();
      result = await handleThink(userId, routeAgentId, thinkArg || undefined, commandDeps);
      break;
    }
    case 'compact': {
      const compactArg = text.slice(1).split(/\s+/).slice(1).join(' ').trim();
      result = await handleCompact(userId, routeAgentId, compactArg || undefined, commandDeps);
      break;
    }
    case 'context': result = await handleContext(userId, routeAgentId, commandDeps); break;
    case 'jobs': {
      if (!ctx.scheduler) {
        await ctx.sendTextMessage(openId, '调度器未就绪。');
        return;
      }
      const jobResult = handleListJobs(agent.id, ctx.scheduler);
      await ctx.sendTextMessage(openId, jobResult);
      return;
    }
    case 'job_add': {
      if (!ctx.scheduler) {
        await ctx.sendTextMessage(openId, '调度器未就绪。');
        return;
      }
      const jobArgs = text.slice(1).split(/\s+/).slice(1).join(' ').trim();
      const jobResult = handleAddJob(agent.id, agent.workspace, userId, jobArgs, ctx.scheduler);
      await ctx.sendTextMessage(openId, jobResult);
      return;
    }
    case 'job_del': {
      if (!ctx.scheduler) {
        await ctx.sendTextMessage(openId, '调度器未就绪。');
        return;
      }
      const jobId = text.slice(1).split(/\s+/)[1]?.trim() ?? '';
      const jobResult = handleDelJob(agent.id, jobId, ctx.scheduler);
      await ctx.sendTextMessage(openId, jobResult);
      return;
    }
    case 'job_run': {
      if (!ctx.scheduler) {
        await ctx.sendTextMessage(openId, '调度器未就绪。');
        return;
      }
      const jobId = text.slice(1).split(/\s+/)[1]?.trim() ?? '';
      const jobResult = await handleRunJob(agent.id, jobId, ctx.scheduler);
      await ctx.sendTextMessage(openId, jobResult);
      return;
    }
    case 'approve': {
      const approveCode = text.slice(1).split(' ')[1];
      if (!approveCode) {
        await ctx.sendTextMessage(openId, '\u26A0\uFE0F \u7528\u6CD5\uFF1A/approve <\u914D\u5BF9\u7801>');
        return;
      }
      const pending = claimPendingPairing(approveCode);
      if (!pending) {
        await ctx.sendTextMessage(openId, '\u26A0\uFE0F \u914D\u5BF9\u7801\u65E0\u6548\u6216\u5DF2\u8FC7\u671F\u3002');
        return;
      }
      try {
        writeOpenIdToConfig(ctx.configPath, userId, pending.agentId, pending.openId, openId, router);
        await ctx.sendTextMessage(openId, `\u2705 \u5DF2\u6210\u529F\u7ED1\u5B9A Agent "${pending.agentId}" (openId: ${pending.openId.slice(0, 8)}...)`);
        console.log({ ts: new Date().toISOString(), level: 'info', msg: 'Agent paired via /approve', agentId: pending.agentId, openId: pending.openId.slice(0, 8) });
      } catch (err) {
        await ctx.sendTextMessage(openId, `\u26A0\uFE0F \u7ED1\u5B9A\u5931\u8D25\uFF1A${String(err)}`);
      }
      return;
    }
    default:
      await ctx.sendTextMessage(openId, `/${cmd} 暂不支持，发 /help 查看可用命令。`);
      return;
  }
  await sendFeishuCommandResult(ctx, openId, result);
}

// --- /balance command: generate dashboard link via Platform token ---

interface PlatformTokenResponse {
  token: string;
  dashboardUrl: string;
  barePath: string;
  expiresInMs: number;
}

async function handleFeishuBalance(
  ctx: MessageHandlerContext,
  userId: string,
  agentId: string,
): Promise<string> {
  const platformUrl = ctx.platformHttpUrl;
  if (!platformUrl) {
    return '⚠️ 平台账单服务未配置';
  }

  // Platform tenantId = Relay userId (e.g. "member-0001"),
  // NOT the full poolKey "userId:agentId"
  const tenantId = userId;

  try {
    const res = await fetchJson<PlatformTokenResponse>(`${platformUrl}/api/auth/dashboard-token`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ tenantId }),
    });

    if (!res.dashboardUrl) {
      return '⚠️ Platform 未配置公网地址，无法生成链接';
    }

    return `💰 账户中心\n点击链接查看余额、消费明细和充值入口（24小时有效）：\n${res.dashboardUrl}`;
  } catch (err) {
    const msg = err instanceof Error ? err.message : '未知错误';
    return `⚠️ 生成链接失败: ${msg}`;
  }
}

async function fetchJson<T>(url: string, init?: RequestInit): Promise<T> {
  const res = await fetch(url, { ...init, signal: AbortSignal.timeout(5000) });
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
  return (await res.json()) as T;
}

function buildDashboardLinkCard(dashboardUrl: string): Record<string, unknown> {
  return {
    schema: '2.0',
    config: { streaming_mode: false },
    header: {
      title: { tag: 'plain_text', content: '\u{1F4B0} \u8D26\u6237\u4E2D\u5FC3' },
      template: 'blue',
    },
    body: {
      elements: [
        {
          tag: 'markdown',
          content: '\u70B9\u51FB\u4E0B\u65B9\u6309\u94AE\u67E5\u770B\u4F59\u989D\u3001\u6D88\u8D39\u660E\u7EC6\u548C\u5145\u503C\u5165\u53E3\u3002\n\u94FE\u63A5 24 \u5C0F\u65F6\u5185\u6709\u6548\u3002',
        },
        {
          tag: 'button',
          type: 'primary',
          text: { tag: 'plain_text', content: '\u{1F4CA} \u6253\u5F00\u8D26\u6237\u4E2D\u5FC3' },
          behaviors: [{ type: 'open_url', default_url: dashboardUrl }],
        },
      ],
    },
  };
}

function buildDashboardLinkErrorCard(errorMsg: string): Record<string, unknown> {
  return {
    schema: '2.0',
    config: { streaming_mode: false },
    header: {
      title: { tag: 'plain_text', content: '💰 账户中心' },
      template: 'red',
    },
    body: {
      elements: [
        { tag: 'markdown', content: `⚠️ ${errorMsg}` },
      ],
    },
  };
}

/** Same live native process. Adapters may return a fresh wrapper per lookup
 * (CC spreads its pool entry), so compare the wrapped child process and session. */
function sameLiveConversationProcess(current:ReturnType<MessageHandlerContext['pool']['get']>,captured:typeof current):boolean{
  if(!current||!captured)return false;
  if(current===captured)return true;
  const field=(value:object,key:'process'|'runtime')=>key in value?(value as Record<string,unknown>)[key]:undefined;
  const live=field(current,'process');
  return live!==undefined&&live===field(captured,'process')&&field(current,'runtime')===field(captured,'runtime')
    &&current.sessionId===captured.sessionId;
}

/** Capture after consumer admission, then fence the actual send after all
 * enrichment/download awaits. A later input owns its own independently captured fence. */
export function captureFeishuBusinessDispatch(ctx:Pick<MessageHandlerContext,'pool'|'stateManager'|'stopTyping'>,poolKey:string,
  boundaryToken:string|undefined,messageId?:string): (message:Parameters<CommandRuntime['send']>[1],submit?:TurnSubmitOptions)=>Promise<boolean>{
  const process=ctx.pool.get(poolKey),projection=conversationConsumerProjection(ctx.stateManager?.getSession(poolKey));
  const stopOwnedTyping=()=>{if(messageId)ctx.stopTyping?.(messageId);};
  const current=()=>feishuRecoveryBoundaryToken(poolKey)===boundaryToken&&sameLiveConversationProcess(ctx.pool.get(poolKey),process)
    &&conversationConsumerProjection(ctx.stateManager?.getSession(poolKey))===projection;
  const dropped=()=>{stopOwnedTyping();
    console.warn({ts:new Date().toISOString(),level:'warn',msg:'Feishu business dispatch fenced; message not sent',poolKey});return false;};
  return async (message,submit)=>{
    if(!current())return dropped();
    // Metering is not a dispatch dependency. The real Stop/session/projection
    // fence above still applies immediately before the authorized send.
    try{assertAdoptedConversationRuntime(ctx.stateManager?.getSession(poolKey),process!);}
    catch(error){stopOwnedTyping();throw error;}
    await (submit?ctx.pool.send(poolKey,message,submit):ctx.pool.send(poolKey,message));return true;
  };
}

// --- Ensure process alive (exported for testability) ---

export async function ensureProcess(
  ctx: MessageHandlerContext,
  resolved: { user: UserConfig },
  route?: RoutePolicyResult,
  onFailure?:(error:unknown)=>void,
): Promise<string | null> {
  const { agent, pool, router, poolKeyToOpenId, stateManager } = ctx;
  const routing = resolveInboundRoutingAgent(router, resolved.user.userId, agent, FEISHU_INBOUND_ROUTING);
  const poolKey = feishuPoolKey(router, resolved.user.userId, routing);
  const boundaryToken=feishuRecoveryBoundaryToken(poolKey);
  const openId = route?.binding.address.channel === 'feishu'
    ? route.binding.address.value
    : agent.feishuOpenId ?? resolved.user.feishuOpenId;
  if (openId) {
    poolKeyToOpenId.set(poolKey, openId);
  }

  let proc: Awaited<ReturnType<typeof acquireConversation>>;
  try {
    proc = await acquireConversation({
      poolKey, agent: routing, runtime: pool, stateManager,
      channel: 'feishu', intent: 'inbound', runtimePreference: route?.runtimePreference,
      recovery: {
        boundaryToken,
        isCurrent: () => feishuRecoveryBoundaryToken(poolKey) === boundaryToken,
      },
    });
  } catch (error) {
    console.error({
      ts: new Date().toISOString(), level: 'error', agent: routing.id, poolKey,
      msg: 'Feishu ensureProcess acquisition failed',
      runtime: route?.runtimePreference[0] ?? routing.runtime ?? 'cc',
      error: error instanceof Error ? error.message : String(error),
      stack: error instanceof Error ? error.stack : undefined,
    });
    onFailure?.(error);
    return null;
  }
  if (stateManager) {
    const runtimeId: ConversationRuntimeId = proc && 'runtime' in proc
      ? proc.runtime
      : routing.runtime ?? 'cc';
    const codexThreadId = proc && 'codexThreadId' in proc ? proc.codexThreadId : undefined;
    const existing = stateManager.getSession(poolKey);
    const interrupted = existing?.interruptedUserMessage;
    stateManager.updateSession(poolKey, {
      poolKey,
      agentId: routing.id,
      runtime: runtimeId,
      sessionId: resolveSessionIdToPersist(ctx, poolKey, routing) ?? existing?.sessionId ?? '',
      codexThreadId: runtimeId === 'codex' ? codexThreadId ?? existing?.codexThreadId : undefined,
      lastActivity: Date.now(),
      status: 'active',
      interruptedUserMessage: undefined,
    });
    stateManager.flush();
    // If Relay restarted mid-turn, inject context so the agent knows what was interrupted
    if (interrupted) {
      captureFeishuBusinessDispatch(ctx,poolKey,boundaryToken)({
        type: 'human',
        content: `[系统提示：Relay 刚刚重启，上条消息被中断，未能送达。中断前的消息内容为：${interrupted}]`,
      }).catch(() => {});
    }
  }
  return poolKey;
}

// --- Pool response handler (exported for testability) ---

export type FeishuStreamingCardSink = {
  onProcess?(poolKey: string, openId: string, event: WorkProcessEvent): Promise<void>;
  onAction?(raw: unknown): Promise<boolean>;
  fail?(poolKey: string): Promise<void>;
  enabled: true;
  beginTurn(poolKey: string, meta?: TurnAcceptedMeta): void;
  onAssistant(poolKey: string, openId: string, text: string, phase?: 'status' | 'activity' | 'snapshot' | 'commentary' | 'final', eventId?: string): Promise<void>;
  onPulse(poolKey: string, line: string | null): void;
  onResult(poolKey: string): Promise<void>;
};

// Format a tool_use block into a short single-line activity hint shown under the card.
function formatToolPulse(name: string | undefined, input: unknown): string | null {
  if (!name) return null;
  let hint = '';
  if (input && typeof input === 'object') {
    const obj = input as Record<string, unknown>;
    // Pick the most informative single field; keep it short.
    const candidates = ['command', 'description', 'file_path', 'pattern', 'url', 'path', 'query', 'prompt'];
    for (const key of candidates) {
      const v = obj[key];
      if (typeof v === 'string' && v.trim()) { hint = v.trim(); break; }
    }
  }
  // Collapse whitespace and clamp; the card has limited width.
  hint = hint.replace(/\s+/g, ' ').slice(0, 80);
  return hint ? `🔧 ${name}(${hint})` : `🔧 ${name}`;
}

export async function handleFeishuPoolResponse(
  agent: AgentConfig,
  poolKey: string,
  msg: StreamJsonMessage,
  poolKeyToOpenId: Map<string, string>,
  sendCardMessage: (openId: string, card: Record<string, unknown>) => boolean | void | Promise<boolean | void>,
  streaming?: FeishuStreamingCardSink,
): Promise<void> {
  if (!poolKeyToOpenId.has(poolKey)) return;
  if (!isFeishuDeliveryPoolKey(agent, poolKey)) return;

  if (msg.type === 'result') {
    if (streaming?.enabled) await streaming.onResult(poolKey);
    return;
  }

  if (msg.type !== 'assistant') return;

  const msgObj = msg.message as { stop_reason?: string | null; content?: Array<Record<string, unknown>> } | undefined;

  // AskUserQuestion: 所有飞书 agent 当前均为 print-mode（interactive=null），
  // print-mode CC 在同一 turn 内就会 self-answer 并结束工具调用。卡片功能已删除
  // （2026-06-26，见文件上方说明），这里直接吞掉 AskUserQuestion tool_use 块——
  // 既不渲染卡片也不下沉为 tool pulse 活动行；CC self-answer 伴生的 marker 文本由
  // CC_NOISE_PATTERNS 过滤。
  if (Array.isArray(msgObj?.content)) {
    const hasAskBlock = msgObj.content.some(
      (b) => (b as { type?: string; name?: string }).type === 'tool_use'
        && (b as { type?: string; name?: string }).name === 'AskUserQuestion',
    );
    if (hasAskBlock) {
      return; // tool_use 块不外发；后续 self-answer 文本走正常路径
    }
  }

  // Only forward records that contain visible text in message.content.
  // Intermediate tool-call records have only tool_use blocks here; the top-level
  // msg.content fallback is intentionally removed to avoid leaking reasoning narration.
  const textContent = Array.isArray(msgObj?.content)
    ? msgObj.content.filter(c => c.type === 'text' && c.text).map(c => c.text).join('')
    : '';

  // Tool-use-only record: surface as a transient pulse line so the streaming card
  // keeps showing activity during long tool runs (Bash/ssh/Agent) when the LLM
  // emits no text for minutes. Without this, Feishu server closes the card after
  // ~3min idle and falls back to a static "Generating..." card.
  if (!textContent) {
    if (streaming?.enabled && Array.isArray(msgObj?.content)) {
      const tool = msgObj.content.find(
        (c): c is Record<string, unknown> & { type: string; name?: string; input?: unknown } =>
          c.type === 'tool_use',
      );
      if (tool) {
        const line = formatToolPulse(tool.name, tool.input);
        if (line) streaming.onPulse(poolKey, line);
      }
    }
    return;
  }

  const noiseStripped = stripCCNoise(textContent);
  if (!noiseStripped) return;

  // Outbound tool-call-leak guard (2026-06-28): never let a `<invoke name=...>`
  // leak reach Feishu. If it lands as a card it sediments in the shared chat and
  // gets carried back into another session's next message (the cross-session
  // re-injection chain). Strip the leak tail before send; loudly log when we do
  // (silent failure on a critical path is unacceptable per domain red line).
  const leak = stripOutboundToolcallLeak(noiseStripped);
  if (leak.leaked) {
    const leakTs = new Date().toISOString();
    console.warn({
      ts: leakTs,
      level: 'warn',
      agent: agent.id,
      poolKey,
      msg: 'Outbound tool-call leak detected — stripped before sending to Feishu',
      removedChars: leak.removedChars,
      keptChars: leak.text.length,
    });
    // L1 出站埋点：退化冲过了内部所有闸、抵达"发给用户"这最后一道才被截。对探针格外重要——
    // 命中这里说明离用户只差一步（内部防线本应更早拦下）。session 无法直接取，poolKey 足够定位。
    emitDegenObserved({
      ts: leakTs,
      agent: agent.id,
      session: poolKey,
      kind: 'outbound-invoke-leak',
      disposition: 'stripped-outbound',
      detail: { removedChars: leak.removedChars, keptChars: leak.text.length },
    });
  }
  // 散落型降解 scrub（2026-07-04）：出站闸的第二道——stripOutboundToolcallLeak 只切 <invoke>
  // 泄漏尾巴，挡不住 court/课题/exotic 这类**每段开头都漏一个**的整行降解（大鸟看到满屏
  // courtan_confirm = court 粘住下段正文）。就地删这些独占整行的降解 token，保留正文。
  const degen = scrubDegenerateLines(leak.text);
  if (degen.removed > 0) {
    const degenTs = new Date().toISOString();
    console.warn({
      ts: degenTs,
      level: 'warn',
      agent: agent.id,
      poolKey,
      msg: 'Outbound degenerate lines scrubbed before sending to Feishu (Opus decode degeneration)',
      removedLines: degen.removed,
      keptChars: degen.text.length,
    });
    // L1 出站埋点：散落型解码降解（court/课题/exotic 整行）冲到出站才被 scrub。
    emitDegenObserved({
      ts: degenTs,
      agent: agent.id,
      session: poolKey,
      kind: 'outbound-degen-scrub',
      disposition: 'stripped-outbound',
      detail: { removedLines: degen.removed, keptChars: degen.text.length },
    });
  }
  const cleanedText = degen.text;
  if (!cleanedText) return;

  const openId = poolKeyToOpenId.get(poolKey);
  if (!openId) return;

  if (streaming?.enabled) {
    // Real text means the LLM is talking again — clear any stale tool pulse.
    streaming.onPulse(poolKey, null);
    const explicitPhase = msg.delivery_phase;
    const phase = explicitPhase === 'snapshot' || explicitPhase === 'commentary' || explicitPhase === 'final'
      ? explicitPhase
      : msgObj?.stop_reason === 'end_turn'
        ? 'final'
        : 'commentary';
    await streaming.onAssistant(
      poolKey,
      openId,
      cleanedText,
      phase,
      typeof msg.event_id === 'string' ? msg.event_id : undefined,
    );
    return;
  }

  await Promise.resolve(sendCardMessage(openId, buildMarkdownCard(cleanedText)));
}

/** Sync facade for callers/tests that do not await (legacy). Prefer `handleFeishuPoolResponse`. */
export function handlePoolResponse(
  agent: AgentConfig,
  poolKey: string,
  msg: StreamJsonMessage,
  poolKeyToOpenId: Map<string, string>,
  sendCardMessage: (openId: string, card: Record<string, unknown>) => void,
  streaming?: FeishuStreamingCardSink,
): void {
  void handleFeishuPoolResponse(agent, poolKey, msg, poolKeyToOpenId, sendCardMessage, streaming).catch((err) => {
    console.error({
      ts: new Date().toISOString(),
      level: 'error',
      agent: agent.id,
      msg: 'handleFeishuPoolResponse failed',
      error: String(err),
    });
  });
}

export type FeishuStreamingSinkOptions = {
  workProcess?: boolean;
  processThrottleMs?: number;
  richDocProfile?: ChannelRichDocProfile;
  observe?: ChannelRenderingObserver;
};

function isDefiniteFeishuFormatError(error: unknown): boolean {
  return /\b400\b|invalid|format|schema|markdown|card content/i.test(String(error));
}

export function createFeishuStreamingSink(
  agent: AgentConfig,
  client: InstanceType<typeof lark.Client>,
  sendCardMessage: (openId: string, card: Record<string, unknown>) => boolean | void | Promise<boolean | void>,
  /** Resolves the active sessionId for a poolKey, used to tag outbox cache
   *  entries so a reply to this card can later detect cross-session quoting. */
  sessionIdResolver?: (poolKey: string) => string | undefined,
  options: FeishuStreamingSinkOptions = {},
): FeishuStreamingCardSink | undefined {
  if (!agent.feishu || !isFeishuStreamingCardEnvEnabled()) return undefined;

  const creds = { appId: agent.feishu.appId, appSecret: agent.feishu.appSecret };
  const richDocProfile = options.richDocProfile ?? resolveRichDocChannelProfiles().feishu;
  const processes = (options.workProcess ?? workProcessEnabled())
    ? new WorkProcessView(`feishu:${agent.id}`, feishuProcessWriter(client), options.processThrottleMs)
    : undefined;
  type FallbackState = { kind: 'fallback'; openId: string; rendering: ChannelTurnRenderingState; sent: boolean };
  const sessions = new Map<string, FeishuStreamingCardSession | FallbackState>();
  const renderings = new Map<string, ChannelTurnRenderingState>();
  // Reply lead of the turn begun by beginTurn; consumed by its rendering state.
  const replyLeads = new Map<string, string>();
  const chains = new Map<string, Promise<void>>();
  const openIdMap = new Map<string, string>();
  const turnSequence = new Map<string, number>();
  type RichTurnState = {
    openId: string;
    cards: FeishuStreamingCardSession[];
    latest?: FeishuPayloadCompilation;
    fallbackRequired: boolean;
  };
  const richTurns = new Map<string, RichTurnState>();
  const richEnabledByPool = new Map<string, boolean>();
  const richTerminalPools = new Set<string>();

  function markTerminalPool(poolKey: string): void {
    richTerminalPools.add(poolKey);
    if (richTerminalPools.size > 2048) {
      const oldest = richTerminalPools.values().next().value as string | undefined;
      if (oldest) richTerminalPools.delete(oldest);
    }
  }

  function observe(event: Parameters<NonNullable<FeishuStreamingSinkOptions['observe']>>[0]): void {
    persistChannelRenderingObservation(event);
    try {
      options.observe?.(event);
    } catch {
      // Observability must never alter delivery behavior.
    }
  }

  function turnScope(poolKey: string): string {
    return `${agent.id}/${poolKey}/turn:${turnSequence.get(poolKey) ?? 1}/feishu`;
  }

  function richEnabled(poolKey: string): boolean {
    let enabled = richEnabledByPool.get(poolKey);
    if (enabled === undefined) {
      enabled = isRichDocRolloutEnabled(richDocProfile, turnScope(poolKey));
      richEnabledByPool.set(poolKey, enabled);
    }
    return enabled;
  }

  function terminalDisplayText(answer: string): string {
    return answer.endsWith(TURN_GENERATED_STATUS_TEXT)
      ? answer
      : `${answer}\n\n${TURN_GENERATED_STATUS_TEXT}`;
  }

  function compileRichFrame(text: string, terminal = false): FeishuPayloadCompilation {
    const compilation = compileFeishuPayloads(text, {
      maxLength: Math.min(richDocProfile.rolloverThreshold, richDocProfile.maxPayloadLength),
      lengthUnit: richDocProfile.lengthUnit,
      maxElements: richDocProfile.maxElements,
    });
    observe({
      channel: 'feishu',
      layer: 'compiler',
      action: 'compile',
      result: 'valid',
      chunkCount: compilation.chunks.length,
      rolloverCount: Math.max(0, compilation.chunks.length - 1),
      terminal,
    });
    if (compilation.chunks.length > 1) {
      observe({
        channel: 'feishu',
        layer: 'renderer',
        action: 'rollover',
        mode: 'cardkit_markdown',
        result: 'valid',
        rolloverCount: compilation.chunks.length - 1,
      });
    }
    return compilation;
  }

  async function reconcileRichTurn(
    poolKey: string,
    openId: string,
    frame: ChannelRenderFrame,
    rendering: ChannelTurnRenderingState,
  ): Promise<void> {
    let state = richTurns.get(poolKey);
    if (!state) {
      state = { openId, cards: [], fallbackRequired: false };
      richTurns.set(poolKey, state);
    }
    state.openId = openId;
    // This reconciliation may await network I/O while beginTurn synchronously
    // replaces the maps.  Only the still-current rendering owns writes/cleanup.
    const ownsTurn = (): boolean => richTurns.get(poolKey) === state && renderings.get(poolKey) === rendering;
    let compilation: FeishuPayloadCompilation;
    try {
      compilation = compileRichFrame(frame.text, frame.final);
      state.latest = compilation;
    } catch {
      state.fallbackRequired = true;
      observe({
        channel: 'feishu',
        layer: 'compiler',
        action: 'validation',
        mode: 'plain_text',
        reason: 'validation',
        result: 'invalid',
      });
      return;
    }
    if (state.fallbackRequired) return;
    if (richDocProfile.maxElements < 2) {
      state.fallbackRequired = true;
      observe({
        channel: 'feishu',
        layer: 'renderer',
        action: 'validation',
        mode: 'cardkit_markdown',
        reason: 'validation',
        result: 'invalid',
      });
      return;
    }
    if (state.cards.length > compilation.chunks.length) {
      // Already-visible rollover cards cannot be retracted after a non-prefix
      // snapshot replacement. Preserve the new authoritative snapshot for one
      // terminal fallback instead of manufacturing a duplicate complete set.
      state.fallbackRequired = true;
      return;
    }

    try {
      for (let index = 0; index < compilation.chunks.length; index += 1) {
        if (!ownsTurn()) return;
        const chunk = compilation.chunks[index];
        let card = state.cards[index];
        if (!card) {
          card = new FeishuStreamingCardSession(creds, client, {
            updateThrottleMs: richDocProfile.updateThrottleMs,
          });
          await card.start(openId, 'open_id', chunk.cardKitMarkdown);
          if (!ownsTurn()) { card.dispose(); return; }
          state.cards.push(card);
        }
        if (!ownsTurn()) return;
        if (card.isClosed()) throw new Error('Card streaming mode is closed');
        try {
          await card.update(chunk.cardKitMarkdown);
          if (!ownsTurn()) return;
        } catch (error) {
          if (!ownsTurn()) return;
          if (!isRetryableFeishuCardUpdateError(error)) throw error;
          // The same card retains its latest frame and bounded retry timer.
          observe({ channel: 'feishu', layer: 'transport', action: 'chunk', mode: 'cardkit_markdown', result: 'coalesced' });
          continue;
        }
        observe({ channel: 'feishu', layer: 'renderer', action: 'chunk', mode: 'cardkit_markdown', result: 'delivered' });
      }
    } catch (error) {
      if (!ownsTurn()) return;
      state.fallbackRequired = true;
      for (const card of state.cards) card.dispose();
      observe({
        channel: 'feishu',
        layer: 'transport',
        action: 'validation',
        mode: 'cardkit_markdown',
        reason: /closed|timeout/i.test(String(error)) ? 'server_closed' : 'unknown',
        result: 'coalesced',
      });
      console.warn({
        ts: new Date().toISOString(),
        level: 'warn',
        agent: agent.id,
        msg: 'Feishu RichDoc streaming update failed; coalescing terminal fallback',
        error: String(error),
      });
    }
    // Keep the authoritative state owner explicit: compilation never mutates or
    // replaces ChannelTurnRenderingState.
    void rendering;
  }

  async function sendPostMarkdown(openId: string, chunk: FeishuPayloadChunk): Promise<boolean> {
    const response = await client.im.message.create({
      params: { receive_id_type: 'open_id' },
      data: {
        receive_id: openId,
        msg_type: 'post',
        content: JSON.stringify({
          zh_cn: {
            title: '',
            content: [[{ tag: 'md', text: chunk.postMarkdown }]],
          },
        }),
      },
    });
    return response.code === 0;
  }

  async function sendPlainText(openId: string, chunk: FeishuPayloadChunk): Promise<boolean> {
    const response = await client.im.message.create({
      params: { receive_id_type: 'open_id' },
      data: {
        receive_id: openId,
        msg_type: 'text',
        content: JSON.stringify({ text: chunk.plainText }),
      },
    });
    return response.code === 0;
  }

  async function sendRichStaticFallback(
    openId: string,
    chunks: FeishuPayloadChunk[],
  ): Promise<'cardkit_markdown' | 'post_markdown' | 'plain_text'> {
    let terminalMode: 'cardkit_markdown' | 'post_markdown' | 'plain_text' = 'cardkit_markdown';
    for (let index = 0; index < chunks.length; index += 1) {
      const chunk = chunks[index];
      if (!chunk) continue;
      let cardDelivered = false;
      try {
        cardDelivered = (await Promise.resolve(sendCardMessage(
          openId,
          buildMarkdownCard(chunk.cardKitMarkdown),
        ))) !== false;
      } catch (error) {
        if (!isDefiniteFeishuFormatError(error)) throw error;
      }
      if (cardDelivered) {
        observe({ channel: 'feishu', layer: 'renderer', action: 'chunk', mode: 'cardkit_markdown', result: 'delivered' });
        continue;
      }

      observe({
        channel: 'feishu',
        layer: 'transport',
        action: 'fallback',
        fromMode: 'cardkit_markdown',
        toMode: 'post_markdown',
        reason: 'format',
        result: 'coalesced',
      });
      let postDelivered = false;
      try {
        postDelivered = await sendPostMarkdown(openId, chunk);
      } catch (error) {
        if (!isDefiniteFeishuFormatError(error)) throw error;
      }
      if (postDelivered) {
        terminalMode = 'post_markdown';
        observe({ channel: 'feishu', layer: 'renderer', action: 'chunk', mode: terminalMode, result: 'delivered' });
        continue;
      }

      observe({
        channel: 'feishu',
        layer: 'transport',
        action: 'fallback',
        fromMode: 'post_markdown',
        toMode: 'plain_text',
        reason: 'format',
        result: 'coalesced',
      });
      if (!(await sendPlainText(openId, chunk))) throw new Error('Feishu plain text fallback failed');
      terminalMode = 'plain_text';
      observe({ channel: 'feishu', layer: 'renderer', action: 'chunk', mode: terminalMode, result: 'delivered' });
    }
    return terminalMode;
  }

  /** RichDoc alone retains a retryable terminal content failure for one retry. */
  async function closeRichCard(
    card: FeishuStreamingCardSession,
    finalText: string | undefined,
    ownsTurn: () => boolean,
  ): Promise<void> {
    if (!ownsTurn()) return;
    try {
      await card.close(finalText, { retainOnRetryableFailure: true, ownsTurn });
      if (!ownsTurn()) return;
    } catch (error) {
      if (!ownsTurn()) return;
      if (!isRetryableFeishuCardUpdateError(error)) throw error;
      try {
        await card.close(finalText, { retainOnRetryableFailure: true, ownsTurn });
        if (!ownsTurn()) return;
      } catch (retryError) {
        if (!ownsTurn()) return;
        card.dispose();
        throw retryError;
      }
    }
  }

  function cleanupRichTurn(
    poolKey: string,
    rendering: ChannelTurnRenderingState | undefined,
    expectedState?: RichTurnState,
  ): void {
    // An old queued finalizer must never delete or terminal-mark a successor.
    if ((rendering && renderings.get(poolKey) !== rendering)
      || (expectedState && richTurns.get(poolKey) !== expectedState)) return;
    rendering?.dispose();
    renderings.delete(poolKey);
    sessions.delete(poolKey);
    const state = richTurns.get(poolKey);
    if (state) {
      state.latest = undefined;
      for (const card of state.cards) card.dispose();
      state.cards = [];
    }
    richTurns.delete(poolKey);
    richEnabledByPool.delete(poolKey);
    markTerminalPool(poolKey);
  }

  async function sendConfirmedProcessFinal(poolKey: string, openId: string, answer: string, owns: () => boolean): Promise<void> {
    const sessionId = sessionIdResolver?.(poolKey);
    for (const chunk of compileRichFrame(answer, true).chunks) {
      const payloads = [
        { msg_type: 'interactive', content: JSON.stringify(buildMarkdownCard(chunk.cardKitMarkdown)) },
        { msg_type: 'post', content: JSON.stringify({ zh_cn: { title: '', content: [[{ tag: 'md', text: chunk.postMarkdown }]] } }) },
        { msg_type: 'text', content: JSON.stringify({ text: chunk.plainText }) },
      ];
      let delivered = false;
      for (const payload of payloads) {
        if (!owns()) return;
        let response;
        try {
          response = await client.im.message.create({ params: { receive_id_type: 'open_id' }, data: { receive_id: openId, ...payload } });
        } catch (error) {
          if (payload.msg_type !== 'text' && isDefiniteFeishuFormatError(error)) continue;
          throw error; // An ambiguous send is never replayed through another format.
        }
        if (!owns()) return;
        if (response.code !== 0) {
          if (payload.msg_type !== 'text' && isDefiniteFeishuFormatError(response.msg)) continue;
          throw new Error('Feishu final was rejected');
        }
        const messageId = response.data?.message_id;
        if (!messageId) throw new Error('Feishu final delivery lacks confirmation');
        recordFeishuOutboundText(messageId, answer, { agentId: agent.id, sessionId, poolKey });
        try { traceDeliveryReceipt({ agent: agent.id, session: poolKey, status: 'sent', attempts: 1, channel: 'feishu' }); }
        catch { /* observation cannot change delivery */ }
        delivered = true;
        break;
      }
      if (!delivered) throw new Error('Feishu final delivery failed');
    }
  }

  async function finalizeProcessTurn(poolKey: string, expectedId?: string): Promise<void> {
    const rendering = renderings.get(poolKey);
    if (!processes || !rendering || processes.idFor(poolKey) !== expectedId) return;
    const owns = () => renderings.get(poolKey) === rendering && processes.idFor(poolKey) === expectedId;
    const answer = rendering.final()?.text ?? rendering.currentText();
    const openId = openIdMap.get(poolKey);
    await processes.drain(poolKey, expectedId);
    if (!owns()) return;
    try {
      if (!answer || !openId) {
        await processes.terminal(poolKey, false, '', expectedId);
        return;
      }
      await sendConfirmedProcessFinal(poolKey, openId, answer, owns);
      if (owns()) await processes.terminal(poolKey, true, answer, expectedId);
    } catch {
      if (owns()) await processes.terminal(poolKey, false, '', expectedId);
      try { traceDeliveryReceipt({ agent: agent.id, session: poolKey, status: 'failed', attempts: 1, channel: 'feishu' }); } catch { /* observation only */ }
      console.warn({ msg: 'Feishu folded-process final delivery failed', agent: agent.id });
    } finally {
      if (owns()) cleanupRichTurn(poolKey, rendering);
    }
  }

  async function finalizeRichTurn(poolKey: string): Promise<void> {
    if (richTerminalPools.has(poolKey)) return;
    const rendering = renderings.get(poolKey);
    const ownsTurn = (): boolean => !!rendering && renderings.get(poolKey) === rendering;
    const finalFrame = rendering?.final();
    const finalText = finalFrame?.text ?? rendering?.currentText() ?? '';
    const terminalText = finalText ? terminalDisplayText(finalText) : '';
    let state = richTurns.get(poolKey);
    if (terminalText) {
      const openId = state?.openId ?? openIdMap.get(poolKey);
      if (openId && rendering) {
        await reconcileRichTurn(
          poolKey,
          openId,
          { identity: rendering.identity, version: finalFrame?.version ?? 0, text: terminalText, final: true },
          rendering,
        );
        if (!ownsTurn()) return;
        state = richTurns.get(poolKey);
      }
    }
    if (!ownsTurn() || !state || !state.latest || !finalText) {
      cleanupRichTurn(poolKey, rendering);
      return;
    }

    let terminalMode: 'cardkit_markdown' | 'post_markdown' | 'plain_text' = 'cardkit_markdown';
    let terminalResult: 'delivered' | 'failed' = 'delivered';
    try {
      if (state.fallbackRequired) {
        terminalMode = await sendRichStaticFallback(state.openId, state.latest.chunks);
        if (!ownsTurn() || richTurns.get(poolKey) !== state) return;
        if (terminalMode !== 'cardkit_markdown') {
          try {
            traceDeliveryReceipt({
              agent: agent.id,
              session: poolKey,
              status: 'sent',
              attempts: terminalMode === 'post_markdown' ? 2 : 3,
              channel: 'feishu',
            });
          } catch { /* observation never changes delivery */ }
        }
      } else {
        for (let index = 0; index < state.cards.length; index += 1) {
          const card = state.cards[index];
          const chunk = state.latest.chunks[index];
          const messageId = card.getMessageId();
          await closeRichCard(card, chunk?.cardKitMarkdown, () => ownsTurn() && richTurns.get(poolKey) === state);
          if (!ownsTurn() || richTurns.get(poolKey) !== state) return;
          if (messageId && chunk) {
            // Preserve reply context: every rollover card resolves to the
            // complete authoritative answer, not only the visible chunk. This
            // cache is not rendering observability.
            recordFeishuOutboundText(messageId, finalText, {
              agentId: agent.id,
              sessionId: sessionIdResolver?.(poolKey),
              poolKey,
            });
          }
        }
        if (!ownsTurn() || richTurns.get(poolKey) !== state) return;
        try {
          traceDeliveryReceipt({ agent: agent.id, session: poolKey, status: 'sent', attempts: 1, channel: 'feishu' });
        } catch { /* observation never changes delivery */ }
      }
    } catch (error) {
      if (!ownsTurn() || richTurns.get(poolKey) !== state) return;
      terminalResult = 'failed';
      try {
        traceDeliveryReceipt({ agent: agent.id, session: poolKey, status: 'failed', attempts: 1, channel: 'feishu' });
      } catch { /* observation never changes delivery */ }
      console.warn({
        ts: new Date().toISOString(),
        level: 'warn',
        agent: agent.id,
        msg: 'Feishu RichDoc terminal delivery failed',
        error: String(error),
      });
    } finally {
      if (!ownsTurn() || richTurns.get(poolKey) !== state) return;
      observe({
        channel: 'feishu',
        layer: 'renderer',
        action: 'terminal',
        mode: terminalMode,
        reason: terminalResult === 'failed' ? 'network' : undefined,
        terminal: true,
        result: terminalResult,
        chunkCount: state.latest.chunks.length,
      });
      observe({
        channel: 'feishu',
        layer: 'transport',
        action: 'terminal',
        mode: terminalMode,
        reason: terminalResult === 'failed' ? 'network' : undefined,
        terminal: true,
        result: terminalResult,
        chunkCount: state.latest.chunks.length,
      });
      // Terminal failure is terminal for this turn too.  Retaining failed cards
      // lets duplicate result events create duplicate answers and leaves timers/state.
      cleanupRichTurn(poolKey, rendering, state);
    }
  }

  function enqueue(poolKey: string, fn: () => Promise<void>): Promise<void> {
    const prev = chains.get(poolKey) ?? Promise.resolve();
    const next = prev.then(fn).catch((err) => {
      const rendering = renderings.get(poolKey);
      if (rendering?.isFinalized()) {
        rendering.dispose();
        renderings.delete(poolKey);
        const transport = sessions.get(poolKey);
        if (transport instanceof FeishuStreamingCardSession) transport.dispose();
        sessions.delete(poolKey);
        const rich = richTurns.get(poolKey);
        if (rich) for (const card of rich.cards) card.dispose();
        richTurns.delete(poolKey);
        richEnabledByPool.delete(poolKey);
        markTerminalPool(poolKey);
      }
      console.error({
        ts: new Date().toISOString(),
        level: 'error',
        agent: agent.id,
        msg: 'Feishu streaming queue task failed',
        poolKey,
        error: String(err),
      });
    });
    chains.set(poolKey, next);
    return next;
  }

  return {
    enabled: true,
    onProcess: (poolKey, openId, event) => processes?.event(poolKey, openId, event) ?? Promise.resolve(),
    fail: poolKey => processes?.terminal(poolKey, false) ?? Promise.resolve(),
    async onAction(raw: unknown): Promise<boolean> {
      const envelope = raw as Record<string, any>;
      const event = envelope?.event ?? envelope;
      const value = event?.action?.value?.relay_work_process;
      const openId = event?.operator?.open_id ?? event?.open_id;
      const messageId = event?.open_message_id ?? event?.context?.open_message_id;
      if (typeof value !== 'string' || typeof openId !== 'string' || typeof messageId !== 'string') return false;
      return processes?.callback(value, openId, messageId, false) ?? false;
    },
    beginTurn(poolKey: string, meta?: TurnAcceptedMeta): void {
      processes?.begin(poolKey);
      const replyLead = turnReplyLead(meta);
      if (replyLead) replyLeads.set(poolKey, replyLead); else replyLeads.delete(poolKey);
      const previous = sessions.get(poolKey);
      if (previous instanceof FeishuStreamingCardSession) previous.dispose();
      sessions.delete(poolKey);
      renderings.get(poolKey)?.dispose();
      renderings.delete(poolKey);
      const previousRich = richTurns.get(poolKey);
      if (previousRich) for (const card of previousRich.cards) card.dispose();
      richTurns.delete(poolKey);
      richTerminalPools.delete(poolKey);
      turnSequence.set(poolKey, (turnSequence.get(poolKey) ?? 0) + 1);
      richEnabledByPool.set(poolKey, isRichDocRolloutEnabled(richDocProfile, turnScope(poolKey)));
    },
    onAssistant(poolKey: string, openId: string, text: string, phase = 'snapshot', eventId?: string): Promise<void> {
      const processId = processes?.idFor(poolKey) ?? processes?.begin(poolKey);
      openIdMap.set(poolKey, openId);
      return enqueue(poolKey, async () => {
        if (processes && processes.idFor(poolKey) !== processId) return;
        if (richTerminalPools.has(poolKey)) return;
        let rendering = renderings.get(poolKey);
        if (!rendering) {
          rendering = new ChannelTurnRenderingState(`${agent.id}/${poolKey}/turn:${turnSequence.get(poolKey) ?? 1}/feishu`,
            { replyLead: replyLeads.get(poolKey) });
          replyLeads.delete(poolKey);
          renderings.set(poolKey, rendering);
        }
        const frame = phase === 'status'
          ? rendering.status(text, eventId)
          : phase === 'activity'
            ? rendering.activity(text, eventId)
            : phase === 'commentary'
              ? (processes ? rendering.snapshot(text, eventId) : rendering.commentary(text, eventId))
              : phase === 'final'
                ? rendering.final(text, eventId, Boolean(processes))
                : rendering.snapshot(text, eventId);
        if (processes) {
          // Equal text from distinct public messages is still distinct work.
          // Semantic answer-frame deduplication must not discard the timeline.
          await processes.frame(poolKey, openId, phase, text, eventId);
          return;
        }
        if (!frame) return;
        if (richDocProfile.rollout === 'shadow') {
          try {
            compileRichFrame(frame.text, frame.final);
          } catch {
            observe({
              channel: 'feishu',
              layer: 'compiler',
              action: 'validation',
              mode: 'legacy',
              reason: 'validation',
              result: 'invalid',
            });
          }
        }
        if (richEnabled(poolKey)) {
          await reconcileRichTurn(poolKey, openId, frame, rendering);
          return;
        }
        const st = sessions.get(poolKey);
        if (st && !(st instanceof FeishuStreamingCardSession)) {
          st.openId = openId;
          return;
        }
        if (st instanceof FeishuStreamingCardSession && st.isClosed()) {
          // A server-closed card is already visible with a partial prefix. Do
          // not open another evolving card for each later snapshot. Hold the
          // latest body and emit one complete static fallback at turn end.
          st.dispose();
          sessions.set(poolKey, { kind: 'fallback', openId, rendering, sent: false });
          return;
        } else if (st instanceof FeishuStreamingCardSession) {
          try {
            await st.update(frame.text);
            return;
          } catch (err) {
            if (isRetryableFeishuCardUpdateError(err)) return;
            // Permanent card update failure — fallback to static card
            st.dispose();
            console.warn({
              ts: new Date().toISOString(),
              level: 'warn',
              agent: agent.id,
              msg: 'Feishu streaming card update failed, falling back to static card',
              error: String(err),
            });
            sessions.set(poolKey, { kind: 'fallback', openId, rendering, sent: false });
            return;
          }
        }
        const sess = new FeishuStreamingCardSession(creds, client);
        try {
          await sess.start(openId, 'open_id', frame.text);
          await sess.update(frame.text);
          sessions.set(poolKey, sess);
        } catch (err) {
          console.warn({
            ts: new Date().toISOString(),
            level: 'warn',
            agent: agent.id,
            msg: 'Feishu streaming card start failed, fallback to static card',
            error: String(err),
          });
          sessions.set(poolKey, { kind: 'fallback', openId, rendering, sent: false });
        }
      });
    },
    onPulse(poolKey: string, line: string | null): void {
      const rich = richTurns.get(poolKey);
      if (richEnabled(poolKey) && rich) {
        for (const card of rich.cards) {
          if (!card.isClosed()) card.setPulseLine(line);
        }
        return;
      }
      // Only meaningful when a streaming card already exists; otherwise tool
      // activity prior to the first assistant text is silently dropped (the
      // card will be created on the next text frame and heartbeat will kick in).
      const st = sessions.get(poolKey);
      if (st instanceof FeishuStreamingCardSession && !st.isClosed()) {
        st.setPulseLine(line);
      }
    },
    onResult(poolKey: string): Promise<void> {
      const processId = processes?.idFor(poolKey);
      return enqueue(poolKey, async () => {
        if (richTerminalPools.has(poolKey)) return;
        if (processes) {
          await finalizeProcessTurn(poolKey, processId);
          return;
        }
        if (richEnabled(poolKey)) {
          await finalizeRichTurn(poolKey);
          return;
        }
        const st = sessions.get(poolKey);
        if (st instanceof FeishuStreamingCardSession) {
          const rendering = renderings.get(poolKey);
          const finalFrame = rendering?.final();
          const answerText = finalFrame?.text || rendering?.currentText() || '';
          const accumulated = answerText || st.getCurrentText();
          const terminalText = answerText ? terminalDisplayText(answerText) : accumulated;
          const streamMsgId = st.getMessageId();
          try {
            // Activity is a visible overlay, never part of the authoritative
            // body. The generated marker is presentation-only and is excluded
            // from reply-context caching below.
            await st.close(terminalText);
            // Cache the final text so a user replying to this card gets real
            // content (im.message.get returns a placeholder for bot cards).
            // Tag with origin (agent + session) so cross-session reply guard works.
            recordFeishuOutboundText(streamMsgId ?? undefined, accumulated, {
              agentId: agent.id,
              sessionId: sessionIdResolver?.(poolKey),
              poolKey,
            });
            // 缺陷 A（2026-07-15 canary）：交互回复走这条流式卡片路径,绕过 delivery.ts,
            // 故此前交互对话的投递零 delivery.receipt 埋点。这里补收口——close() 成功 =
            // 流式卡片已定稿投递到用户,记 status=sent。session 用 poolKey,与本轮 req.inbound
            // 的 session 键一致（req.inbound 用 router.poolKey,即同一 feishu poolKey）→ 探针可配对。
            // 旁路观测,dark-deploy 开关控制,全隔离永不抛（traceDeliveryReceipt 内部已 try/catch,
            // 此处再包一层 try 兜住构造参数意外）。仅在流式卡片路径打点——静态卡 fallback 走
            // sendCardMessage→delivery.ts,已由 receipt() 工厂各自埋点,不在此重复计数。
            try {
              traceDeliveryReceipt({ agent: agent.id, session: poolKey, status: 'sent', attempts: 1, channel: 'feishu' });
            } catch { /* 埋点绝不影响主链路 */ }
          } catch (err) {
            console.warn({
              ts: new Date().toISOString(),
              level: 'warn',
              agent: agent.id,
              msg: 'Feishu streaming card close failed, sending fallback message',
              error: String(err),
            });
            // 缺陷 A：流式卡片 close 失败 = 交互回复经流式路径未送达（飞书投递无重试）。
            // 记 status=failed。下方 fallback 静态卡若发出,是另一次经 delivery.ts 的独立投递,
            // 由 receipt() 工厂自行埋点,不与此 failed 冲抵——探针能同时看到"流式失败 + 静态补发"。
            try {
              traceDeliveryReceipt({ agent: agent.id, session: poolKey, status: 'failed', attempts: 1, channel: 'feishu' });
            } catch { /* 埋点绝不影响主链路 */ }
            // Close failed — send accumulated text as static card fallback
            if (accumulated) {
              const openId = openIdMap.get(poolKey);
              if (openId) {
                await Promise.resolve(sendCardMessage(openId, buildMarkdownCard(terminalText)));
              }
            }
          }
        } else if (st && !st.sent) {
          const finalFrame = st.rendering.final();
          const finalText = finalFrame?.text ?? st.rendering.currentText();
          if (finalText) {
            st.sent = true;
            await Promise.resolve(sendCardMessage(st.openId, buildMarkdownCard(terminalDisplayText(finalText))));
          }
        }
        sessions.delete(poolKey);
        renderings.get(poolKey)?.dispose();
        renderings.delete(poolKey);
        richEnabledByPool.delete(poolKey);
        markTerminalPool(poolKey);
      });
    },
  };
}

// --- Session-end handler (exported for testability) ---

export function handleSessionEnd(
  agent: AgentConfig,
  poolKey: string,
  reason: SessionEndReason,
  poolKeyToOpenId: Map<string, string>,
  sendTextMessage: (openId: string, text: string) => void,
  router: Router,
): void {
  if (!poolKeyToOpenId.has(poolKey)) return;
  if (!isFeishuDeliveryPoolKey(agent, poolKey)) return;
  const openId = poolKeyToOpenId.get(poolKey);
  if (!openId) return;

  const userId = parseConversationPoolKey(poolKey)?.userId ?? '';
  const labelAgent = resolveAgentFromPoolKey(router, userId, poolKey) ?? agent;

  let message: string | null = null;
  switch (reason.type) {
    case 'turns-exhausted':
      message = `\u26A0\uFE0F ${labelAgent.name} \u5DF2\u8FBE\u5230\u8F6E\u6B21\u4E0A\u9650 (${reason.turnCount}/${reason.maxTurns})\uFF0C\u53D1\u65B0\u6D88\u606F\u81EA\u52A8\u6062\u590D\u3002`;
      break;
    case 'crash':
      message = `\u26A0\uFE0F ${labelAgent.name} \u9047\u5230\u95EE\u9898\uFF0C\u5DF2\u81EA\u52A8\u6062\u590D\u3002`;
      break;
    case 'crash-unrecoverable':
      message = `\u26A0\uFE0F ${labelAgent.name} \u591A\u6B21\u5D29\u6E83\u5DF2\u6682\u505C\uFF0C\u53D1\u6D88\u606F\u91CD\u8BD5\u3002`;
      break;
    case 'session-in-use':
      message = [
        `\u26A0\uFE0F ${labelAgent.name} \u7684 Claude \u4F1A\u8BDD ID \u6B63\u88AB\u5176\u4ED6\u8FDB\u7A0B\u5360\u7528\uFF08\u6216\u672A\u6B63\u5E38\u91CA\u653E\uFF09\u3002`,
        '\u8BF7\u5173\u95ED\u5176\u4ED6\u7EC8\u7AEF\u91CC\u7684 claude / \u590D\u5236\u6216\u53D1 /new \u6362\u65B0 session \u540E\u518D\u8BD5\u3002',
      ].join('\n');
      break;
    case 'rate-limited':
      message = [
        `\u26A0\uFE0F ${labelAgent.name} \u5F53\u524D\u63A8\u7406\u670D\u52A1\u88AB\u9650\u6D41\uFF0C\u5DF2\u505C\u6B62\u81EA\u52A8\u91CD\u8BD5\u3002`,
        '\u76F4\u63A5\u518D\u53D1\u4E00\u6761\u6D88\u606F\u53EF\u7EE7\u7EED\uFF1B\u5982\u53CD\u590D\u89E6\u53D1\uFF0C\u5EFA\u8BAE /new \u5207\u6362 session \u6216\u7A0D\u5019\u51E0\u5206\u949F\u3002',
      ].join('\n');
      break;
    case 'relay-shutdown':
      // Only tell the user about a restart if they're mid-turn waiting for us.
      if (reason.wasBusy) message = `\u26A9\uFF0F Relay \u6B63\u5728\u91CD\u542F\uFF0C\u51E0\u79D2\u540E\u6062\u590D\u3002`;
      break;
    case 'timeout':
      message = `\u26A0\uFE0F ${labelAgent.name} \u5F53\u524D\u8BF7\u6C42\u8D85\u65F6\uFF08>${Math.round(reason.timeoutMs / 1000)}s\uFF09\uFF0C\u5DF2\u4E2D\u6B62\u3002\u76F4\u63A5\u518D\u53D1\u4E00\u6761\u6D88\u606F\u7EE7\u7EED\u3002`;
      break;
    case 'runtime-failed':
      message = `⚠️ ${labelAgent.name} 的 ${reason.runtime} runtime 已停止，当前请求没有自动切换或重放。请新开会话后重试。`;
      break;
    case 'runtime-recovery-required':
      message = [
        `⚠️ ${labelAgent.name} 已执行可能产生外部副作用的操作，随后 runtime 失败。`,
        '为避免重复写入或重复网络操作，系统没有自动换 runtime 重放；请先核对结果，再新开会话或重试。',
      ].join('\n');
      break;
    case 'idle-reclaimed':
      break;
  }

  if (message) {
    sendTextMessage(openId, message);
  }
}
// --- Tool-output file delivery ---

async function deliverToolFiles(
  agent: AgentConfig,
  poolKey: string,
  workspace: string,
  openId: string,
  filePaths: string[],
  client: FeishuMessageClient,
  sendTextMessage: (openId: string, text: string) => void,
): Promise<void> {
  for (const filePath of filePaths) {
    const ownership = validateToolFileOutboxPath(poolKey, workspace, filePath);
    if (!ownership.ok) {
      console.warn({ ts: new Date().toISOString(), level: 'warn', agent: agent.id, msg: 'Tool file delivery rejected', path: filePath, reason: ownership.error });
      continue;
    }
    const check = checkFileForDelivery(ownership.path);
    if (!check.ok) {
      console.warn({ ts: new Date().toISOString(), level: 'warn', agent: agent.id, msg: 'Tool file delivery skipped', path: ownership.path, reason: check.error });
      continue;
    }
    const address = normalizeFeishuOpenIdAddress(openId, agent.feishu!.appId, agent.id);
    const baseRequest = {
      id: `delivery:feishu:${agent.id}:toolfile:${Date.now()}:${Buffer.from(ownership.path).toString('base64').slice(0, 16)}`,
      correlationId: `feishu:${agent.id}:${openId}:${Date.now()}`,
      channel: 'feishu' as const,
      address,
      payload: { kind: 'document' as const, path: ownership.path },
    };
    const fileName = ownership.path.split('/').pop() ?? ownership.path;
    try {
      const receipt = isImagePath(ownership.path)
        ? await deliverFeishuImage(baseRequest, client)
        : await deliverFeishuFile(baseRequest, client);
      if (receipt.status === 'sent') {
        console.log({ ts: new Date().toISOString(), level: 'info', agent: agent.id, msg: 'Tool file delivered', path: ownership.path, size: check.size });
        // Only validated outbox-owned files are disposable after delivery.
        try { rmSync(ownership.path, { force: true }); } catch { /* best-effort */ }
      } else {
        console.warn({ ts: new Date().toISOString(), level: 'warn', agent: agent.id, msg: 'Tool file delivery failed', path: ownership.path, error: receipt.error });
        sendTextMessage(openId, `\u26A0\uFE0F \u6587\u4EF6\u53D1\u9001\u5931\u8D25: ${fileName}`);
      }
    } catch (err) {
      console.error({ ts: new Date().toISOString(), level: 'error', agent: agent.id, msg: 'Tool file delivery error', path: ownership.path, error: String(err) });
      sendTextMessage(openId, `\u26A0\uFE0F \u6587\u4EF6\u53D1\u9001\u5931\u8D25: ${fileName}`);
    }
  }
}

// --- Pool event wiring (exported for testability) ---

export function wirePoolEvents(
  agent: AgentConfig,
  pool: CommandRuntime,
  poolKeyToOpenId: Map<string, string>,
  router: Router,
  sendTextMessage: (openId: string, text: string) => void,
  sendCardMessage: (openId: string, card: Record<string, unknown>) => boolean | void | Promise<boolean | void>,
  stopTyping?: () => void,
  streaming?: FeishuStreamingCardSink,
  client?: FeishuMessageClient,
  stateManager?: StateManager,
  isActive: () => boolean = () => true,
): void {
  const nativeStreamText = new Map<string, string>();
  const nativeStreamSequence = new Map<string, number>();
  const activity = new TurnActivityCounter();
  const reportedRuntimeErrors = new Set<string>();
  // Relay sends and runtime-notification turns (CC woke on a finished
  // background task) both open a fresh streaming turn here.
  pool.on('turn-accepted', (poolKey: string, meta?: TurnAcceptedMeta) => {
    reportedRuntimeErrors.delete(poolKey);
    if (!isActive() || !streaming?.enabled) return;
    if (!poolKeyToOpenId.has(poolKey) || !isFeishuDeliveryPoolKey(agent, poolKey)) return;
    nativeStreamText.delete(poolKey);
    nativeStreamSequence.delete(poolKey);
    activity.begin(poolKey);
    streaming.beginTurn(poolKey, meta);
    const openId = poolKeyToOpenId.get(poolKey);
    if (openId) void streaming.onAssistant(poolKey, openId, turnAcceptedStatusText(meta), 'status', `${poolKey}:accepted`);
  });
  pool.on('turn-effect', (poolKey: string) => {
    if (!isActive() || !streaming?.enabled) return;
    if (!poolKeyToOpenId.has(poolKey) || !isFeishuDeliveryPoolKey(agent, poolKey)) return;
    const openId = poolKeyToOpenId.get(poolKey);
    if (!openId) return;
    const progress = activity.advance(poolKey);
    if (progress?.text) {
      void streaming.onAssistant(poolKey, openId, progress.text, 'activity', `${poolKey}:activity:${progress.step}`);
    }
  });
  pool.on('stream-delta', (poolKey: string, delta: string, correlationId?: string) => {
    if (!isActive() || !streaming?.enabled) return;
    if (!poolKeyToOpenId.has(poolKey) || !isFeishuDeliveryPoolKey(agent, poolKey)) return;
    const openId = poolKeyToOpenId.get(poolKey);
    if (!openId) return;
    const accumulated = `${nativeStreamText.get(poolKey) ?? ''}${delta}`;
    nativeStreamText.set(poolKey, accumulated);
    const sequence = (nativeStreamSequence.get(poolKey) ?? 0) + 1;
    nativeStreamSequence.set(poolKey, sequence);
    void streaming.onAssistant(poolKey, openId, accumulated, 'snapshot', `${correlationId ?? poolKey}:delta:${sequence}`);
  });
  pool.on('response', (poolKey: string, msg: StreamJsonMessage) => {
    if (!isActive()) return;
    if (msg.delivery_visibility === 'control') return;
    if (msg.type === 'work_process') {
      const openId = poolKeyToOpenId.get(poolKey);
      if (openId && isFeishuDeliveryPoolKey(agent, poolKey)) void streaming?.onProcess?.(poolKey, openId, msg.process_event as WorkProcessEvent);
      return;
    }
    if (msg.type === 'result' && msg.is_error === true) void streaming?.fail?.(poolKey);
    const process = pool.get?.(poolKey);
    const runtimeId = process ? ('runtime' in process ? process.runtime : 'cc') : agent.conversationRuntime?.runtime ?? agent.runtime;
    const failureNotice = publicRuntimeResultNotice(msg, runtimeId);
    if (failureNotice) {
      console.error({ ts: new Date().toISOString(), level: 'error', agent: agent.id,
        poolKey, msg: 'Conversation runtime failed result', error: failureNotice });
      reportedRuntimeErrors.add(poolKey);
    }
    void handleFeishuPoolResponse(agent, poolKey, msg, poolKeyToOpenId, sendCardMessage, streaming).catch((err) => {
      console.error({
        ts: new Date().toISOString(),
        level: 'error',
        agent: agent.id,
        msg: 'handleFeishuPoolResponse failed',
        error: String(err),
      });
    }).then(() => {
      if (!failureNotice) return;
      const openId = poolKeyToOpenId.get(poolKey);
      if (openId) return sendTextMessage(openId, failureNotice);
    }).catch((err) => {
      console.error({ ts: new Date().toISOString(), level: 'error', agent: agent.id,
        msg: 'Feishu runtime failure notice failed', error: String(err) });
    });
    // Stop typing on result (CC finished this turn)
    if (msg.type === 'result') {
      if (msg.is_error !== true) reportedRuntimeErrors.delete(poolKey);
      nativeStreamText.delete(poolKey);
      nativeStreamSequence.delete(poolKey);
      activity.end(poolKey);
      stopTyping?.();
    }
  });


  pool.on('error', (poolKey: string, error: Error) => {
    if (!isActive()) return;
    if (!poolKeyToOpenId.has(poolKey) || !isFeishuDeliveryPoolKey(agent, poolKey)) return;
    nativeStreamText.delete(poolKey);
    nativeStreamSequence.delete(poolKey);
    activity.end(poolKey);
    void streaming?.fail?.(poolKey);
    const openId = poolKeyToOpenId.get(poolKey);
    const process = pool.get?.(poolKey);
    const runtimeId = process ? ('runtime' in process ? process.runtime : 'cc') : agent.conversationRuntime?.runtime ?? agent.runtime;
    const notice = publicRuntimeExceptionNotice(runtimeId, error);
    console.error({ ts: new Date().toISOString(), level: 'error', agent: agent.id,
      poolKey, msg: 'Pool error', error: notice });
    if (reportedRuntimeErrors.has(poolKey)) return;
    reportedRuntimeErrors.add(poolKey);
    if (openId) void Promise.resolve().then(() => sendTextMessage(openId, notice)).catch(() => {});
  });

  pool.on('turn-warning', (poolKey: string, currentTurn: number, maxTurns: number) => {
    if (!isActive()) return;
    if (!poolKeyToOpenId.has(poolKey) || !isFeishuDeliveryPoolKey(agent, poolKey)) return;
    const openId = poolKeyToOpenId.get(poolKey);
    if (!openId) return;
    const userId = parseConversationPoolKey(poolKey)?.userId ?? '';
    const label = resolveAgentFromPoolKey(router, userId, poolKey) ?? agent;
    sendTextMessage(openId, `\u26A0\uFE0F ${label.name} \u5DF2\u7528 ${currentTurn}/${maxTurns} \u8F6E\u6B21\uFF0880%\uFF09\uFF0C\u5373\u5C06\u8FBE\u5230\u4E0A\u9650\u3002`);
  });

  pool.on('exit', (poolKey: string, code: number | null, meta?: PoolExitMeta) => {
    if (!isActive()) return;
    if (!poolKeyToOpenId.has(poolKey) || !isFeishuDeliveryPoolKey(agent, poolKey)) return;
    activity.end(poolKey);
    if (code === 0 || code === null || code === 143) return;
    void streaming?.fail?.(poolKey);
    if (reportedRuntimeErrors.delete(poolKey)) return;
    if (meta?.suppressGenericRecoveryToast) return;
    const openId = poolKeyToOpenId.get(poolKey);
    if (openId) sendTextMessage(openId, '\u26A0\uFE0F \u7CFB\u7EDF\u5F00\u5C0F\u5DEE\u4E86\uFF0C\u6B63\u5728\u6062\u590D\u4E2D...');
  });

  pool.on('session-end', (poolKey: string, reason: SessionEndReason) => {
    if (!isActive()) return;
    if (!poolKeyToOpenId.has(poolKey) || !isFeishuDeliveryPoolKey(agent, poolKey)) return;
    // Mirror Telegram: clear typing on any session-end — busy timeout and other
    // abort paths do not emit `result`, so wirePoolEvents would otherwise leave
    // the Feishu Typing reaction stuck.
    nativeStreamText.delete(poolKey);
    nativeStreamSequence.delete(poolKey);
    activity.end(poolKey);
    if (['crash', 'crash-unrecoverable', 'session-in-use', 'timeout', 'runtime-failed', 'runtime-recovery-required'].includes(reason.type)
      || (reason.type === 'relay-shutdown' && reason.wasBusy)) void streaming?.fail?.(poolKey);
    stopTyping?.();
    // bug-20260512-002: flush streaming card accumulated content before sending
    // session-end notification. Without this, timeout/crash kills CC before the
    // normal onResult path fires, silently dropping any partial response.
    if (streaming?.enabled) {
      void streaming.onResult(poolKey).catch(() => {});
    }
    handleSessionEnd(agent, poolKey, reason, poolKeyToOpenId, sendTextMessage, router);
  });

  // 429 退避到达阈值时的一次性提示——不停 typing、不清状态，仅告知用户后台仍在等待。
  pool.on('rate-limit-notice', (poolKey: string, info: { retryCount: number; nextDelayMs: number }) => {
    if (!isActive()) return;
    if (!poolKeyToOpenId.has(poolKey) || !isFeishuDeliveryPoolKey(agent, poolKey)) return;
    const openId = poolKeyToOpenId.get(poolKey);
    if (!openId) return;
    const userId = parseConversationPoolKey(poolKey)?.userId ?? '';
    const label = resolveAgentFromPoolKey(router, userId, poolKey) ?? agent;
    const nextMin = Math.round(info.nextDelayMs / 60_000);
    const text = `⏳ ${label.name} 当前推理服务被限流（rate limit），后台仍在按指数退避自动重试（下次约 ${nextMin} 分钟后），无需操作。`;
    sendTextMessage(openId, text);
  });

  // --- Busy progress notifications (replaces auto-kill) ---
  pool.on('busy-progress', (poolKey: string, progress: BusyProgressEvent) => {
    if (!isActive()) return;
    if (!poolKeyToOpenId.has(poolKey) || !isFeishuDeliveryPoolKey(agent, poolKey)) return;
    const openId = poolKeyToOpenId.get(poolKey);
    if (!openId) return;

    const userId = parseConversationPoolKey(poolKey)?.userId ?? '';
    const label = resolveAgentFromPoolKey(router, userId, poolKey) ?? agent;

    const elapsedMin = Math.round(progress.elapsedMs / 60_000);
    let msg: string;
    if (progress.isFinalCheck) {
      msg = `⏳ ${label.name} 已持续运行 ${elapsedMin} 分钟。这是最后一次自动通知，如需终止请发送 /stop。`;
    } else {
      msg = `⏳ ${label.name} 正在执行任务（已运行 ${elapsedMin} 分钟），请耐心等待。如需终止请发送 /stop。`;
    }
    sendTextMessage(openId, msg);
  });

  // Tool-output file delivery: send files created by CC tools to the user
  pool.on('tool-files', (poolKey: string, filePaths: string[], workspace: string) => {
    if (!isActive()) return;
    if (!client || !poolKeyToOpenId.has(poolKey) || !isFeishuDeliveryPoolKey(agent, poolKey)) return;
    const openId = poolKeyToOpenId.get(poolKey);
    if (!openId) return;
    void deliverToolFiles(agent, poolKey, workspace, openId, filePaths, client, sendTextMessage).catch((err) => {
      console.error({ ts: new Date().toISOString(), level: 'error', agent: agent.id, msg: 'deliverToolFiles failed', error: String(err) });
    });
  });

  pool.on('provider-fallback', (poolKey: string, event: import('../types.js').ProviderFallbackEvent) => {
    if (!isActive()) return;
    if (!poolKeyToOpenId.has(poolKey) || !isFeishuDeliveryPoolKey(agent, poolKey)) return;
    const openId = poolKeyToOpenId.get(poolKey);
    if (!openId) return;
    const userId = parseConversationPoolKey(poolKey)?.userId ?? '';
    const label = resolveAgentFromPoolKey(router, userId, poolKey) ?? agent;
    const reason = event.reason ? `，原因：${truncateReason(event.reason)}` : '';
    const text = [
      `⚠️ ${label.name} 模型已从 ${event.fromModel} 切换到 ${event.toModel}${reason}。`,
      '已自动开启新对话继续处理，无需重发消息。',
    ].join('\n');
    sendTextMessage(openId, text);
  });

  pool.on('session-chain-recovery', (poolKey: string, event: import('../types.js').SessionChainRecoveryEvent) => {
    if (!isActive()) return;
    if (!isFeishuDeliveryPoolKey(agent, poolKey)) return;
    const address = parseConversationPoolKey(poolKey)!;
    const { userId, agentId } = address;
    router.overrideSessionId(userId, agentId, event.newSessionId, address.channelScope);
    stateManager?.updateSession(poolKey, {
      sessionId: event.newSessionId,
      lastActivity: Date.now(),
    });
    stateManager?.flush();
    const openId = poolKeyToOpenId.get(poolKey);
    if (!openId) return;
    const label = resolveAgentFromPoolKey(router, userId, poolKey) ?? agent;
    sendTextMessage(
      openId,
      `⚠️ ${label.name} 检测到对话历史链断裂，已自动开启新对话。旧记录仍保存在服务器，如需查阅可联系管理员。`,
    );
  });

  pool.onRuntimeFallback?.((event) => {
    if (!isActive()) return;
    if (!poolKeyToOpenId.has(event.tenantId) || !isFeishuDeliveryPoolKey(agent, event.tenantId)) return;
    const openId = poolKeyToOpenId.get(event.tenantId);
    if (!openId) return;
    const userId = parseConversationPoolKey(event.tenantId)?.userId ?? '';
    const label = resolveAgentFromPoolKey(router, userId, event.tenantId) ?? agent;
    console.warn({
      ts: new Date().toISOString(),
      level: 'warn',
      agent: agent.id,
      msg: 'Conversation runtime fallback',
      poolKey: event.tenantId,
      from: event.from,
      to: event.to,
      attemptIndex: event.attemptIndex,
      reason: event.reason,
    });
    const reason = event.reason ? `, reason: ${truncateReason(event.reason)}` : '';
    const text = [
      `\u26A0\uFE0F ${label.name} \u4E3B\u63A8\u7406\u901A\u9053\u6682\u65F6\u53D7\u9650\uFF0C\u5DF2\u81EA\u52A8\u5207\u5230\u5907\u7528\u901A\u9053\u7EE7\u7EED\u672C\u8F6E\u3002`,
      '\u65E0\u9700\u91CD\u53D1\u6D88\u606F\uFF0C\u56DE\u590D\u53EF\u80FD\u7A0D\u6162\uFF1B\u672C\u4F1A\u8BDD\u5269\u4F59\u6D88\u606F\u9ED8\u8BA4\u8D70\u5907\u7528\u901A\u9053\u3002',
      `\uFF08${event.from} \u2192 ${event.to}${reason}\uFF09`,
    ].join('\n');
    sendTextMessage(openId, text);
  });
}

function truncateReason(reason: string, maxLen = 120): string {
  const collapsed = reason.replace(/\s+/g, ' ').trim();
  if (collapsed.length <= maxLen) return collapsed;
  return `${collapsed.slice(0, maxLen - 1)}\u2026`;
}

// --- Create All Feishu Bots ---

export function createFeishuBots(deps: FeishuBotDeps): FeishuBotInstance {
  const { router } = deps;
  // Live registry; reload() spawns missing only.
  const running = new Map<string, SingleBotInstance>();
  let reloadChain: Promise<unknown> = Promise.resolve();
  let resumePassed = false;

  function snapshotFeishuAgents(): { agent: AgentConfig; authorizedOpenIds: string[] }[] {
    return router.getAllFeishuBots();
  }

  async function reconcileFromConfig(): Promise<{ added: string[]; removed: string[]; total: number }> {
    const desired = new Map(snapshotFeishuAgents().map(spec => [spec.agent.id, spec]));
    return reconcileFeishuClientRegistry(running, desired, (id, spec) => {
      if (!spec.agent.feishu) throw new Error(`Agent "${id}" has no feishu credentials`);
      return createSingleFeishuBot(spec.agent, new Set(spec.authorizedOpenIds), deps, resumePassed);
    });
  }

  return {
    async start(): Promise<void> {
      const { added } = await reconcileFromConfig();
      console.log({ ts: new Date().toISOString(), level: 'info', msg: 'Feishu bots up', total: running.size, started: added.length });
    },
    stop(): void {
      for (const inst of running.values()) inst.stop();
      running.clear();
    },
    async notifyShutdown(excludePoolKeys?: Set<string>): Promise<void> {
      await Promise.all(Array.from(running.values()).map(inst => inst.notifyShutdown(excludePoolKeys)));
    },
    async notifyResumed(): Promise<void> {
      resumePassed = true;
      await Promise.all(Array.from(running.values()).map(inst => inst.notifyResumed()));
    },
    async deliverInternalTurn(poolKey: string, prompt: string, expectNativeIds: string[], notice?: CollabTurnNotice, deliveryRef?: string): Promise<boolean> {
      for (const inst of running.values()) {
        if (await inst.deliverInternalTurn(poolKey, prompt, expectNativeIds, notice, deliveryRef)) return true;
      }
      return false;
    },
    async reload(): Promise<{ added: string[]; removed: string[]; total: number }> {
      const job = reloadChain.then(async () => {
        const result = await reconcileFromConfig();
        console.log({ ts: new Date().toISOString(), level: 'info', msg: 'Feishu bots reloaded', ...result });
        return result;
      });
      reloadChain = job.catch(() => undefined);
      return job;
    },
    runningAgentIds(): string[] { return Array.from(running.keys()); },
    getMessageClient(agentId: string): FeishuMessageClient | null { return running.get(agentId)?.messageClient() ?? null; },
    async sendText(agentId: string, openId: string, text: string): Promise<void> {
      const instance=running.get(agentId);
      if(!instance)throw new Error(`Feishu bot ${agentId} is not active`);
      await instance.sendText(openId,text);
    },
  };
}

// --- Internal: Single Feishu Bot (Lark SDK wiring) ---

interface SingleBotInstance { start(): Promise<void>; stop(): void; messageClient(): FeishuMessageClient; sendText(openId:string,text:string):Promise<void>; notifyShutdown(excludePoolKeys?: Set<string>): Promise<void>; notifyResumed(): Promise<void>; deliverInternalTurn(poolKey: string, prompt: string, expectNativeIds: string[], notice?: CollabTurnNotice, deliveryRef?: string): Promise<boolean>; }

/** Announce a delivered collaboration contract: a card with folded details,
 * its headline as text when the card fails, a generic line without a notice. */
export async function sendCollabTurnNotice(
  send: { card(card: Record<string, unknown>): Promise<boolean>; text(text: string): Promise<void> },
  notice?: CollabTurnNotice,
): Promise<void> {
  const carded = notice ? await send.card(feishuTurnNoticeCard(notice)).catch(() => false) : false;
  if (carded) return;
  await send.text(notice ? collabTurnHeadline(notice) : '📨 收到一份协作合同，开始处理。').catch(() => {});
}

function createSingleFeishuBot(agent: AgentConfig, _authorizedOpenIds: Set<string>, deps: FeishuBotDeps,
  /** Created after the Relay's resume pass (a reload): nothing to wait for. */
  afterResume = false): SingleBotInstance {
  if (!agent.feishu) throw new Error(`Agent "${agent.id}" has no feishu credentials`);

  const client = new lark.Client({ appId: agent.feishu.appId, appSecret: agent.feishu.appSecret });
  const wsClient = new lark.WSClient({ appId: agent.feishu.appId, appSecret: agent.feishu.appSecret, loggerLevel: lark.LoggerLevel.warn });
  let active = false;

  const poolKeyToOpenId = new Map<string, string>();
  for (const user of deps.router.getAllUsers()) {
    for (const ag of user.agents) {
      if (ag.feishu?.appId !== agent.feishu.appId) continue;
      const openId = ag.feishuOpenId ?? user.feishuOpenId;
      if (openId) {
        poolKeyToOpenId.set(feishuPoolKey(deps.router, user.userId, ag), openId);
      }
    }
  }
  for (const binding of deps.router.getGatewayBindings?.() ?? []) {
    if (
      binding.status === 'active'
      && binding.address.channel === 'feishu'
      && binding.address.kind === 'feishu_open_id'
      && binding.address.botId === agent.id
    ) {
      poolKeyToOpenId.set(feishuPoolKey(deps.router, binding.principalId, agent), binding.address.value);
    }
  }

  const sendTextMessage = async (openId: string, text: string) => {
    if (!active) return;
    const receipt = await deliverFeishuText({
      id: `delivery:feishu:${agent.id}:text:${Date.now()}`,
      correlationId: `feishu:${agent.id}:${openId}:${Date.now()}`,
      channel: 'feishu',
      address: normalizeFeishuOpenIdAddress(openId, agent.feishu!.appId, agent.id),
      payload: { kind: 'text', text },
    }, client);
    if (receipt.status !== 'sent') {
      console.warn({ ts: new Date().toISOString(), level: 'warn', agent: agent.id, msg: 'Failed to send Feishu text', receiptId: receipt.id, error: receipt.error });
    }
  };

  const sendCardMessage = async (openId: string, card: Record<string, unknown>): Promise<boolean> => {
    if (!active) return false;
    const receipt = await deliverFeishuCard({
      id: `delivery:feishu:${agent.id}:card:${Date.now()}`,
      correlationId: `feishu:${agent.id}:${openId}:${Date.now()}`,
      channel: 'feishu',
      address: normalizeFeishuOpenIdAddress(openId, agent.feishu!.appId, agent.id),
      payload: { kind: 'card', markdown: '', title: agent.name, card },
    }, client);
    if (receipt.status !== 'sent') {
      console.warn({ ts: new Date().toISOString(), level: 'warn', agent: agent.id, msg: 'Failed to send Feishu card', receiptId: receipt.id, error: receipt.error });
      return false;
    }
    // Cache card text by message_id so replies to this card resolve to real
    // content (im.message.get returns a placeholder for bot interactive cards).
    // Tag with origin (agent + session) so cross-session reply guard works —
    // reverse-lookup poolKey from openId via the binding map, then ask pool/state
    // for the current sessionId.
    let poolKey: string | undefined;
    for (const [pk, oid] of poolKeyToOpenId.entries()) {
      if (oid === openId) { poolKey = pk; break; }
    }
    const sessionId = poolKey
      ? (deps.pool.get(poolKey)?.sessionId ?? deps.stateManager?.getSession(poolKey)?.sessionId ?? undefined)
      : undefined;
    recordFeishuOutboundText(receipt.externalMessageId, extractCardText(card), {
      agentId: agent.id,
      sessionId,
      poolKey,
    });
    return true;
  };

  // Keep each asynchronous reaction owned by the input that created it.
  // An older input's cleanup must not clear the next input's indicator.
  let typing:{messageId:string;reactionId?:string}|undefined;
  const removeTyping=(messageId:string,reactionId:string)=>{
    client.im.messageReaction.delete({path:{message_id:messageId,reaction_id:reactionId}}).catch((err:unknown)=>{
      console.warn({ts:new Date().toISOString(),level:'debug',agent:agent.id,msg:'Typing indicator remove failed',error:String(err)});
    });
  };
  const stopTyping=(messageId?:string)=>{
    if(!typing||(messageId&&typing.messageId!==messageId))return;
    const previous=typing;typing=undefined;
    if(previous.reactionId)removeTyping(previous.messageId,previous.reactionId);
  };
  const startTyping=(messageId:string)=>{
    stopTyping();
    const current={messageId,reactionId:undefined as string|undefined};typing=current;
    client.im.messageReaction.create({path:{message_id:messageId},data:{reaction_type:{emoji_type:'Typing'}}})
      .then((resp:any)=>{
        const reactionId=resp?.data?.reaction_id;
        if(!reactionId)return;
        if(typing!==current){removeTyping(messageId,reactionId);return;}
        current.reactionId=reactionId;
      }).catch((err:unknown)=>{
        console.warn({ts:new Date().toISOString(),level:'debug',agent:agent.id,msg:'Typing indicator add failed',error:String(err)});
      });
  };

  const fetchReplyText = async (messageId: string): Promise<string | undefined> => {
    const resp = await client.im.message.get({ path: { message_id: messageId } });
    const data = (resp as any)?.data;
    const item = data?.items?.[0] ?? data?.message ?? data;
    const msgType = item?.msg_type ?? item?.message_type;
    const content = item?.body?.content ?? item?.content;
    if (msgType === 'text') {
      try {
        const parsed = typeof content === 'string' ? JSON.parse(content) : content;
        return typeof parsed?.text === 'string' ? parsed.text : undefined;
      } catch {
        return typeof content === 'string' ? content : undefined;
      }
    }
    if (msgType === 'post') {
      return typeof content === 'string' ? extractPostText(content) ?? undefined : undefined;
    }
    if (msgType === 'interactive') {
      // Bot replies are usually cards (buildMarkdownCard). Pull the markdown/text
      // blocks out of the card JSON so a user replying to a bot card gets real text.
      const cardText = extractCardText(content);
      if (cardText) return cardText;
    }
    // 资源类：content 就在手边（上面已取出），直发路径也一直在解——没有理由
    // 在引用回复里退化成占位符。透出文件名，Agent 才认得出引用的是哪个附件。
    if (msgType === 'file' || msgType === 'image' || msgType === 'audio' || msgType === 'media') {
      const described = describeFeishuResourceContent(msgType, content);
      if (described) return described;
    }
    return typeof msgType === 'string' ? `[Feishu ${msgType} message]` : undefined;
  };

  const handlerCtx: MessageHandlerContext = {
    // Every send from the message handlers hands its event over (intake journal).
    agent, pool: intakeAwareRuntime(deps.pool), router: deps.router,
    stateManager: deps.stateManager,
    get scheduler() { return deps.scheduler; },
    poolKeyToOpenId,
    configPath: deps.configPath,
    meteringDir: deps.meteringDir,
    sendTextMessage,
    sendCardMessage,
    downloadImage: (msgId, imageKey) => downloadFeishuImage(client, msgId, imageKey),
    downloadFileResource: (msgId, fileKey) => downloadFeishuMessageResource(client, msgId, fileKey, 'file'),
    transcribeInboundAudio: (buf) => transcribeAudio(buf, process.env['CLAUDE_CODE_OAUTH_TOKEN'] ?? ''),
    fetchReplyText,
    startTyping, stopTyping,
    larkClient: client,
    platformHttpUrl: deps.platformHttpUrl,
    modelCatalog: deps.modelCatalog,
    terminalManager: deps.terminalManager,
  };

  const nativeHistory=(poolKey:string,sessionId:string,inputRef:string,marker?:string)=>{
    const session=deps.stateManager?.getSession(poolKey);
    if(session?.sessionId!==sessionId||session.runtime!=='pi')throw new FeishuRecoveryError('recovery_session_changed');
    const manifestPath=managedRelayPath('piManifest',process.env.RELAY_PI_RUNTIME_MANIFEST_PATH?.trim());
    const route=agent.conversationRuntime??deps.router.getConversationRuntimeDefault?.('pi');
    if(!manifestPath||!route?.isolationProfile)throw new FeishuRecoveryError('native_dispatch_readback_unavailable');
    const path=existingPiSessionFile(loadPiRuntimeManifest(manifestPath),route.isolationProfile,poolKey,sessionId);
    if(!path)throw new FeishuRecoveryError('native_dispatch_readback_unavailable');
    return readFeishuRecoveryHistory(path,sessionId,inputRef,marker);
  };
  const inboundRecovery=new FeishuInboundRecovery({
    agentId:agent.id,session:poolKey=>deps.stateManager?.getSession(poolKey),
    runtimeRevision:()=>{
      const path=managedRelayPath('piManifest',process.env.RELAY_PI_RUNTIME_MANIFEST_PATH?.trim());
      return path?createHash('sha256').update(readFileSync(path)).digest('hex'):'manifest_unavailable';
    },
    owns:poolKey=>active&&poolKeyToOpenId.has(poolKey)&&isFeishuDeliveryPoolKey(agent,poolKey),
    busy:poolKey=>['starting','busy'].includes(deps.pool.get(poolKey)?.state??''),
    ensureRuntime:async(poolKey,sessionId,boundaryToken)=>{
      const userId=feishuRecoveryUserId(agent,poolKey);
      const user=deps.router.getAllUsers().find(value=>value.userId===userId);
      if(!user||deps.stateManager?.getSession(poolKey)?.sessionId!==sessionId
        ||feishuRecoveryBoundaryToken(poolKey)!==boundaryToken)throw new FeishuRecoveryError('recovery_session_changed');
      let failure:unknown;
      const ready=await ensureProcess(handlerCtx,{user},undefined,error=>{failure=error;});
      if(ready!==poolKey)throw failure??new FeishuRecoveryError('runtime_start_failed');
    },
    dispatch:async(poolKey,content,idempotencyKey)=>{
      const input=latestDurableFeishuInput(poolKey);
      if(!input||!deps.pool.sendIdempotent)throw new FeishuRecoveryError('durable_dispatch_unavailable');
      restoreHumanInputBoundary(input.ref,poolKey,'feishu',feishuRecoveryUserId(agent,poolKey));
      await deps.pool.sendIdempotent(poolKey,{type:'human',content},idempotencyKey);
    },
    readDispatchEvidence:async(poolKey,sessionId,inputRef,marker)=>{
      const history=nativeHistory(poolKey,sessionId,inputRef,marker);
      if(history.accepted||history.marked)return 'accepted';
      const source=readHumanInput(inputRef,poolKey,'feishu',feishuRecoveryUserId(agent,poolKey));
      const openId=poolKeyToOpenId.get(poolKey);
      return openId&&await platformProvesStartupRejection(client.im.message,source,openId,agent.feishu!.appId)
        ?'proven_unsent':'unknown';
    },
  });
  handlerCtx.recordStartupFailure=input=>{
    try{return inboundRecovery.recordStartupFailure(input).state==='pending';}
    catch(error){console.error({msg:'Feishu startup failure could not be queued',agent:agent.id,
      errorType:error instanceof Error?error.name:typeof error});return false;}
  };
  let recoveryTimer:ReturnType<typeof setInterval>|undefined;
  let recoveryRunning=false;
  const resumeRecoveries=()=>{if(recoveryRunning)return;recoveryRunning=true;
    void (async()=>{
      for(const resume of [()=>inboundRecovery.resumePending()]){
        try{await resume();}catch(error){console.error({msg:'Feishu Host recovery retry failed',agent:agent.id,
          errorType:error instanceof Error?error.name:typeof error,code:(error as {code?:string}).code});}
      }
    })().finally(()=>{recoveryRunning=false;});};

  // The lark EventDispatcher swallows rejected promises silently. Any throw
  // while processing a message (JSON.parse, gateway route, spawn, send, …)
  // would otherwise vanish with zero log — the exact "静默失败不可接受" red
  // line. Lanes and the card handler log every failure.
  const lanes = createChatLanes((openId, err) => feishuIntakeFailed(agent.id, openId, err));
  const intake = createChannelIntakeJournal(deps.stateManager, `feishu:${agent.id}`);
  let resumeDone!: () => void;
  const resumed = new Promise<void>(resolve => { resumeDone = resolve; });
  if (afterResume) resumeDone();
  const receiveMessage = createFeishuIntake(handlerCtx, lanes, intake);
  const eventDispatcher = new lark.EventDispatcher({}).register({
      // Returns at once, the message recorded: the SDK acknowledges the event
      // when this handler returns.
      'im.message.receive_v1': (data: any) => { receiveMessage(data); },
      'card.action.trigger': async (data: any) => {
        try {
          if (await streamingSink?.onAction?.(data)) return;
          await handleFeishuCardAction(handlerCtx, data);
        } catch (err) {
          console.error({
            ts: new Date().toISOString(),
            level: 'error',
            agent: agent.id,
            msg: 'handleFeishuCardAction threw',
            error: err instanceof Error ? err.message : String(err),
            stack: err instanceof Error ? err.stack : undefined,
          });
        }
      },
    });
  const wsLifecycle = createDeferredFeishuWsLifecycle(wsClient, eventDispatcher);

  const streamingSink = createFeishuStreamingSink(agent, client, sendCardMessage, (poolKey: string) =>
    deps.pool.get(poolKey)?.sessionId ?? deps.stateManager?.getSession(poolKey)?.sessionId ?? undefined,
  );
  wirePoolEvents(
    agent,
    deps.pool,
    poolKeyToOpenId,
    deps.router,
    sendTextMessage,
    sendCardMessage,
    stopTyping,
    streamingSink,
    client,
    deps.stateManager,
    () => active,
  );

  deps.metering?.onSoftQuotaWarning((event) => {
    if (!active) return;
    const openId = poolKeyToOpenId.get(event.tenantId);
    if (!openId) return;
    const userId = parseConversationPoolKey(event.tenantId)?.userId ?? '';
    const label = resolveAgentFromPoolKey(deps.router, userId, event.tenantId) ?? agent;
    const percent = Math.min(99, Math.round(event.ratio * 100));
    console.warn({
      ts: new Date().toISOString(),
      level: 'warn',
      agent: agent.id,
      msg: 'Soft quota warning emitted',
      poolKey: event.tenantId,
      monthKey: event.monthKey,
      currentCostUsd: event.currentCostUsd,
      budgetUsd: event.budgetUsd,
      percent,
    });
    const text = [
      `\u26A0\uFE0F ${label.name} \u672C\u6708\uFF08${event.monthKey} UTC\uFF09\u7D2F\u8BA1\u6D88\u8017 $${event.currentCostUsd.toFixed(4)} / $${event.budgetUsd.toFixed(2)} \u8F6F\u914D\u989D\uFF08${percent}%\uFF09\u3002`,
      '\u8F6F\u914D\u989D\u4EC5\u505A\u63D0\u9192\uFF0C\u4E0D\u5F3A\u5236\u963B\u65AD\u3002\u5982\u9700\u660E\u7EC6\u8BF7\u53D1 /usage\u3002',
    ].join('\n');
    sendTextMessage(openId, text);
  });

  const instance: SingleBotInstance = {
    messageClient(): FeishuMessageClient { return client as unknown as FeishuMessageClient; },
    async sendText(openId:string,text:string):Promise<void>{await sendTextMessage(openId,text);},
    async start(): Promise<void> {
      // Messages the last process acknowledged but did not hand over run
      // first, each behind its sender's resumed turns.
      const replayed = replayFeishuIntake(handlerCtx, lanes, intake, resumed);
      if (replayed > 0) console.log({ ts: new Date().toISOString(), level: 'info', agent: agent.id, msg: 'Replaying Feishu messages recorded before restart', count: replayed });
      await wsLifecycle.start();
      active = true;
      recoveryTimer=setInterval(resumeRecoveries,5_000);recoveryTimer.unref?.();resumeRecoveries();
      console.log({ ts: new Date().toISOString(), level: 'info', msg: `${agent.emoji} ${agent.name} feishu WSClient started` });
    },
    stop(): void {
      active = false;
      // Queued messages stay in the journal for the next process.
      intake.close();
      resumeDone();
      if(recoveryTimer)clearInterval(recoveryTimer);
      wsLifecycle.stop();
      console.log({ ts: new Date().toISOString(), level: 'info', msg: `${agent.emoji} ${agent.name} feishu WSClient stopped` });
    },
    async notifyShutdown(excludePoolKeys?: Set<string>): Promise<void> {
      // Only notify sessions that are actually mid-turn (proc.state === 'busy').
      // Idle agents don't need a "I'm restarting" message — there's nobody waiting.
      // (notifyResumed pairs naturally: same predicate via interruptedUserMessage,
      // which pool.ts writes for exactly these busy sessions during SIGTERM.)
      const seen = new Set<string>();
      for (const [poolKey, openId] of poolKeyToOpenId) {
        if (excludePoolKeys?.has(poolKey)) continue;
        const proc = deps.pool.get(poolKey);
        if (!proc || proc.state !== 'busy') continue;
        if (seen.has(openId)) continue;
        seen.add(openId);
        await sendTextMessage(openId, `⚩／ Relay 正在重启，几秒后恢复。`).catch(() => {});
      }
    },
    async deliverInternalTurn(poolKey: string, prompt: string, expectNativeIds: string[], notice?: CollabTurnNotice, deliveryRef?: string): Promise<boolean> {
      const openId = poolKeyToOpenId.get(poolKey);
      const session = deps.stateManager?.getSession(poolKey);
      if (!openId || !session || !isFeishuDeliveryPoolKey(agent, poolKey)) return false;
      const boundary = feishuRecoveryBoundaryToken(poolKey);
      let proc = deps.pool.get(poolKey);
      if (!proc || proc.state === 'dead') {
        proc = await acquireConversation({agent, runtime: deps.pool, stateManager: deps.stateManager,
          poolKey, channel: 'feishu', intent: 'recovery',
          recovery: {boundaryToken: boundary, isCurrent: () => feishuRecoveryBoundaryToken(poolKey) === boundary}});
        if (!proc || !['ready', 'idle'].includes(proc.state)) return false;
        assertAdoptedConversationRuntime(deps.stateManager?.getSession(poolKey), proc);
      }
      const codexThreadId = 'codexThreadId' in proc ? (proc as { codexThreadId?: string }).codexThreadId : undefined;
      if (!namesConversation(proc as NativeIdSource, expectNativeIds)) return false;
      await sendCollabTurnNotice({
        card: card => sendCardMessage(openId, card),
        text: text => sendTextMessage(openId, text),
      }, notice);
      const content = internalDeliveryPrompt(prompt, codexThreadId ? 'codex' : 'cc', codexThreadId ?? proc.sessionId, process.env);
      // Fenced: the runtime runs it only in the conversation it was routed to, checked where the
      // turn starts (internal-turn-fence.ts); the check above only spares a notice.
      return captureFeishuBusinessDispatch(deps, poolKey, boundary)(
        fencedInternalTurn({ type: 'human', content }, expectNativeIds, deliveryRef));
    },
    async notifyResumed(): Promise<void> {
      // Active wake-up ONLY for sessions that were busy when Relay shut down,
      // i.e. the user was waiting for an agent reply. Idle agents stay quiet.
      //
      // Signal: pool.onShutdownInterrupt sets session.wasBusyAtShutdown=true
      // for every CC process whose state==='busy' at SIGTERM, regardless of
      // whether Relay still held a fresh in-flight send (long-thinking / 429
      // paths clear lastInFlight before SIGTERM). interruptedUserMessage is
      // best-effort context — when present we inject it on resume; when
      // absent we still wake the session and let the agent continue from
      // its own context.
      //
      // Cost guard: in-memory per-openId same-storm suppression (3min) + maxTurns
      // inherits agent default (so the resume turn can naturally finish the
      // interrupted work — see 2e92836).
      const now = Date.now();
      const sessions = deps.stateManager?.getAllSessions() ?? {};
      const seenOpenIds = new Set<string>();
      for (const [poolKey, openId] of poolKeyToOpenId) {
        if (!isFeishuDeliveryPoolKey(agent, poolKey)) continue;
        const session = sessions[poolKey];
        if (!session) continue;
        // Turns accepted during the shutdown and never dispatched; replayed
        // after the interrupted turn's resume, in arrival order.
        const pending = session.pendingMaintenanceTurns?.length ?? 0;
        // Skip sessions where the user was not waiting at shutdown.
        if (!session.wasBusyAtShutdown && !pending) continue;
        // Belt-and-suspenders: very old state is stale even with the flag.
        const since = session.shutdownInterruptedAt ?? session.lastActivity;
        if (now - since > FEISHU_RESUME_ACTIVE_WINDOW_MS) continue;

        // Same-restart-wave suppression (per openId) applies to the resume
        // prompt only. Each restart wave fires notifyResumed once across all
        // agents; within FEISHU_RESUME_SAME_STORM_MS we deliver to a given
        // openId at most once. User turns waiting in pending are never dropped.
        let resume = session.wasBusyAtShutdown === true && !seenOpenIds.has(openId);
        if (resume) {
          seenOpenIds.add(openId);
          if (!_shouldEmitFeishuResume(openId, now)) {
            console.log({
              ts: new Date().toISOString(),
              level: 'info',
              agent: agent.id,
              msg: 'Feishu resume notice suppressed (same-storm window)',
              poolKey,
            });
            resume = false;
          }
        }
        if (!resume && !pending) continue;

        const interrupted = session.interruptedUserMessage?.trim();
        // Note: by the time we get here, the CC SDK has been re-spawned with
        // --resume and replayed the prior jsonl. If shutdown was graceful
        // (pool.writeUnfinishedTurnMarker fired), the jsonl tail carries a
        // `summary` record explaining the gap. We still inject this active
        // prompt to (a) wake the agent so it sends a reply, and (b) call out
        // the channel-vs-jsonl divergence explicitly so the agent doesn't
        // pretend it never said anything if the user references "the message
        // you just sent" (memo 2026-06-24).
        const resumePrompt = interrupted
          ? `[系统提示：Relay 刚重启恢复。重启前用户最后这条消息可能未完整处理：${interrupted}\n注意：重启时你可能正在生成回复——已发出的部分文本可能已落到飞书卡片但未写入你的 jsonl，因此你看不到自己说过什么。如果用户引用"你刚才说的话"和你的记忆对不上，请承认并请用户复述。请优先继续这条消息；若上下文不足，请简短说明需要用户重发。]`
          : '[系统提示：Relay 刚重启恢复。重启时你正在处理一个用户回合（具体内容未被 Relay 记录）。已发出的部分文本可能落到飞书卡片但未写入 jsonl，因此你可能看不到自己说过什么——如果用户引用"你刚才说的话"和你的记忆对不上，请承认并请用户复述。请用一句话告诉用户已回来，然后基于会话上下文继续未完成的工作。]';

        // Spawn CC (resume) for this session and inject the resume prompt.
        const resumeBoundary=feishuRecoveryBoundaryToken(poolKey);
        try {
          let proc = deps.pool.get(poolKey);
          if (!proc || proc.state === 'dead') {
            const token=feishuRecoveryBoundaryToken(poolKey);
            proc=await acquireConversation({agent,runtime:deps.pool,stateManager:deps.stateManager,
              poolKey,channel:'feishu',intent:'recovery',
              recovery:{boundaryToken:token,isCurrent:()=>feishuRecoveryBoundaryToken(poolKey)===token}});
            if(!proc||!['ready','idle'].includes(proc.state)||proc.sessionId!==session.sessionId)
              throw new FeishuRecoveryError('recovery_session_changed');
            assertAdoptedConversationRuntime(deps.stateManager?.getSession(poolKey),proc);
          }
          const dispatch=captureFeishuBusinessDispatch(deps,poolKey,resumeBoundary);
          if (resume) {
            if(!await dispatch({
              type: 'human',
              content: resumePrompt,
            },{resume:true}))continue;
            // Clear wake signals atomically — both flag and best-effort text.
            deps.stateManager?.updateSession(poolKey, {
              wasBusyAtShutdown: false,
              interruptedUserMessage: undefined,
              lastActivity: now,
            });
          }
          if (deps.stateManager) {
            await replayPendingTurns(deps.stateManager, poolKey, async (message, submit) => {
              if (!await dispatch(message, submit)) throw new FeishuRecoveryError('pending_turn_not_accepted');
            });
          }
          deps.stateManager?.updateSession(poolKey, { shutdownInterruptedAt: undefined, lastActivity: now });
        } catch (err) {
          console.error({
            ts: new Date().toISOString(),
            level: 'error',
            agent: agent.id,
            msg: 'Feishu notifyResumed failed; falling back to static text',
            poolKey,
            error: String(err),
          });
          await sendTextMessage(openId, '⚡ Relay 已重启恢复，可继续对话。').catch(() => {});
        }
      }
    },
  };
  // Replayed messages wait for this bot's resume pass, however it ends.
  const notifyResumed = instance.notifyResumed.bind(instance);
  instance.notifyResumed = async () => { try { await notifyResumed(); } finally { resumeDone(); } };
  return instance;
}
