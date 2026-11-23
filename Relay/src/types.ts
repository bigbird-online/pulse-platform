import { resolve } from 'node:path';
import type { ChildProcess } from 'node:child_process';
import {channelConversationAgent, type ChannelConversationConfig} from './channel-conversation.js';
import type { RuntimeUsageBinding } from './runtime-usage-binding.js';
import type { CanonicalMeasurementBaselineReference } from './canonical-measurement-baseline.js';
import { ccRepoAccessProblems, type CcRepoAccess, type PiAgentLease } from './cc-write-boundary.js';

// --- Relay Config (v2: multi-agent per user) ---

export type ChannelType = 'telegram' | 'feishu';

export interface FeishuAgentCredentials extends ChannelConversationConfig {
  appId: string;
  appSecret: string;
  sessionId: string;
}

/** Auxiliary vision model config for text-only primary models. */
export interface VisionModelConfig {
  model: string;                // Vision model ID served by an Anthropic-compatible endpoint
  baseUrl?: string;             // Independent API endpoint (falls back to RELAY_VISION_BASE_URL, then primary provider)
  apiKey?: string;              // Independent credential (falls back to RELAY_VISION_API_KEY, then primary provider)
  systemPrompt?: string;        // Override the default image description prompt
}

export interface AgentConfig {
  id: string;                  // OA id, e.g. "main", "ken"
  /** Operator-provisioned Harness principal when the channel id is an alias.
   * Does not rename the channel, pool key, or physical Session identity. */
  harnessAgentId?: string;
  name: string;                // Display name, e.g. "Duke", "Ken"
  emoji: string;               // Identifier, e.g. "🎲", "🔧"
  botToken?: string;           // TG bot token (required for TG channel)
  workspace: string;           // CC working directory
  sessionId?: string;          // CC session UUID (TG agents; feishu uses feishu.sessionId)
  maxTurns?: number;           // CC --max-turns (default 1000)
  allowedTools?: string[];     // CC --allowed-tools
  /** Per-agent CC `--model` (overrides RELAY_CLAUDE_MODEL / CLAUDE_CODE_MODEL when set). */
  ccModel?: string;
  /** Emergency per-agent CC process mode override. Default is provider-aware auto mode. */
  ccMode?: CCMode;
  /** CC prompt trims that cost something (`cc-session-settings.ts`); absent keeps CC's default prompt. Legacy `cc` runtime only (not `cc-sdk`); a change reaches new sessions only. */
  ccPrompt?: import('./cc-session-settings.js').CcPromptOptions;
  runtime?: ConversationRuntimeId; // conversation runtime; default 'codex' (codex-defaults.ts)
  conversationRuntimePreference?: ConversationRuntimeId[]; // TG conversation runtime fallback order
  /** Structured conversation route. New runtimes must use this namespace
   * instead of adding runtime-specific fields to AgentConfig/SpawnConfig. */
  conversationRuntime?: ConversationRuntimeRoute;
  /** Auth/endpoint mode for the `cc-sdk` runtime. Default 'subscription' (no
   *  base_url injected). Ignored by the legacy `cc` / `codex` runtimes. */
  ccSdkProviderMode?: CcSdkProviderMode;
  /** Default Codex YOLO permission mode for new sessions. Keep true only for infra agents. */
  codexYoloDefault?: boolean;
  /** Remind each undeclared request to call declare_session_wi; CC also denies listed
   *  write operations until the request is declared. Default off. */
  sessionWiGuard?: boolean;
  /** CC sessions and scheduled CC runs may write only inside their write roots
   *  (hook for file tools, CC sandbox for Bash). Default off. */
  ccWriteBoundary?: boolean;
  /** With `ccWriteBoundary`: repository roots (absolute real paths) whose Git
   *  data Bash may write, so a session can commit and push there; hooks and
   *  config stay closed. Absent or empty opens none. */
  commitRepos?: string[];
  /** With `ccWriteBoundary`: host names added to the CC Bash sandbox network
   *  allowlist (`sandbox.network.allowedDomains`). Absent or empty adds none. */
  allowedDomains?: string[];
  // --- v2 channel binding ---
  channel?: ChannelType;       // absent = telegram (v1 compat)
  feishu?: FeishuAgentCredentials;  // feishu credentials for the same logical agent
  feishuOpenId?: string;       // feishu open_id for this specific app (per-app, per-user)
  // --- B2 soft quota ---
  monthlyBudgetUsd?: number;   // soft monthly cost cap; warning fires once per month at 80%
  // --- Auxiliary vision model ---
  visionModel?: VisionModelConfig;
  /** PT0-018: Linux user for per-tenant file isolation (e.g. "tenant-member-0001"). */
  linuxUser?: string;
  /** HOME dir to inject when spawning CC (for non-linuxUser agents, so session files
   *  land in the real user's ~/.claude/projects/). */
  userHome?: string;
}

/** Never infer authority by stripping channel prefixes or reading a WI owner. */
export function harnessPrincipalForAgent(agent: Pick<AgentConfig, 'id' | 'harnessAgentId'>): string {
  if (agent.harnessAgentId === undefined) return agent.id;
  const principal = agent.harnessAgentId;
  if (typeof principal !== 'string' || !/^[a-zA-Z0-9][a-zA-Z0-9_-]{0,127}$/.test(principal)) {
    throw new ConfigValidationError(`Agent "${agent.id}" has an invalid Harness principal`);
  }
  return principal;
}

export interface UserConfig {
  userId: string;              // User ID, e.g. "owner"
  /** Local runtime validation account with no external channel identity. */
  localOnly?: true;
  /** Explicit local file links may be read only under these node-local, per-user directories. */
  telegramLocalFileRoots?: string[];
  chatId?: string;             // TG chat ID (required for TG agents)
  feishuOpenId?: string;       // Feishu open_id (required for feishu agents)
  defaultAgent: string;        // Default agent id
  agents: AgentConfig[];       // Available agents
}

export interface RelayConfig {
  users: UserConfig[];
  bindings?: ChannelBindingRecord[];
  /** Node-provisioned runtime routes. These make a runtime selectable for every
   * agent on the node without changing any agent's default runtime. */
  conversationRuntimeDefaults?: Partial<Record<ConversationRuntimeId, ConversationRuntimeDefault>>;
  /** The PULSE platform bot: owner confirmations only, never an Agent conversation. */
  platformBot?: { botToken: string; ownerChatId: string };
}

// --- Config Validation ---

export class ConfigValidationError extends Error {
  constructor(message: string) {
    super(message);
    this.name = 'ConfigValidationError';
  }
}

/**
 * Validate relay.json config.
 * - Each agent must have at least one channel credential (botToken or feishu).
 * - A logical agent may expose multiple channel credentials.
 * - If channel is explicitly set, it must match a credential block present.
 * - Feishu openId is optional at startup; missing bindings use pairing flow.
 * - v1 config (no channel field, botToken present) is valid.
 */
export function validateConfig(config: RelayConfig): void {
  const piDefault = config.conversationRuntimeDefaults?.pi;
  if (piDefault && (!piDefault.model?.provider || !piDefault.model.model || !piDefault.model.reasoning || !piDefault.isolationProfile)) {
    throw new ConfigValidationError(
      'Node Pi runtime default requires provider, model, reasoning, and isolationProfile',
    );
  }

  if (config.platformBot !== undefined && (typeof config.platformBot?.botToken !== 'string' || !config.platformBot.botToken
    || typeof config.platformBot.ownerChatId !== 'string' || !/^[0-9]{1,20}$/.test(config.platformBot.ownerChatId))) {
    throw new ConfigValidationError('Platform bot requires its bot token and the owner chat');
  }
  const platformToken = config.platformBot?.botToken;
  for (const user of config.users) {
    if (user.localOnly !== undefined && user.localOnly !== true) {
      throw new ConfigValidationError(`User "${user.userId}" localOnly must be true when provided`);
    }
    if (user.localOnly && (user.chatId || user.feishuOpenId
      || config.bindings?.some(binding => binding.tenantId === user.userId))) {
      throw new ConfigValidationError(`Local user "${user.userId}" cannot have channel addresses or bindings`);
    }
    if (user.telegramLocalFileRoots !== undefined && (!Array.isArray(user.telegramLocalFileRoots)
      || user.telegramLocalFileRoots.some(root => typeof root !== 'string' || !root.startsWith('/') || resolve(root) === '/'))) {
      throw new ConfigValidationError(`User "${user.userId}" telegramLocalFileRoots requires absolute, non-root directories`);
    }
    for (const agent of user.agents) {
      if (platformToken && agent.botToken === platformToken) {
        throw new ConfigValidationError(`Agent "${agent.id}" cannot share the platform bot`);
      }
      harnessPrincipalForAgent(agent);
      validateAgentRepoAccess(agent);
      const hasTG = !!agent.botToken;
      const hasFeishu = !!agent.feishu;
      const channel = agent.channel ?? 'telegram';

      if (user.localOnly && (hasTG || hasFeishu || agent.channel || agent.feishuOpenId)) {
        throw new ConfigValidationError(`Local agent "${agent.id}" cannot have channel credentials or addresses`);
      }
      // channel field mismatch
      if (!user.localOnly && channel === 'feishu' && !hasFeishu) {
        throw new ConfigValidationError(
          `Agent "${agent.id}" (user: ${user.userId}) channel is "feishu" but no feishu credentials provided`,
        );
      }
      if (!user.localOnly && channel === 'telegram' && !hasTG) {
        throw new ConfigValidationError(
          `Agent "${agent.id}" (user: ${user.userId}) channel is "telegram" but no botToken provided`,
        );
      }

      if (!user.localOnly && !hasTG && !hasFeishu) {
        throw new ConfigValidationError(
          `Agent "${agent.id}" (user: ${user.userId}) has no channel credentials`,
        );
      }

      for (const channelAgent of [agent, ...(hasFeishu ? [channelConversationAgent(agent, 'feishu')] : [])]) {
        if (channelAgent.conversationRuntime?.runtime === 'pi') {
          const route = channelAgent.conversationRuntime;
          if (!route.model?.provider || !route.model.model || !route.model.reasoning || !route.isolationProfile) {
            throw new ConfigValidationError(
              `Agent "${agent.id}" (user: ${user.userId}) Pi runtime requires provider, model, reasoning, and isolationProfile`,
            );
          }
          if (channelAgent.conversationRuntimePreference?.some((runtime) => runtime !== 'pi')) {
            throw new ConfigValidationError(
              `Agent "${agent.id}" (user: ${user.userId}) Pi runtime cannot be configured as automatic fallback`,
            );
          }
        }
      }

      // Feishu agent: openId is optional at startup (pending pairing).
      // If missing, the agent will collect openId via the pairing flow on first message.
      // Only warn; do not block startup.
    }
  }
}

/** An Agent's `commitRepos` / `allowedDomains` must be well formed; checked at start and on every reload. */
export function validateAgentRepoAccess(agent: Pick<AgentConfig, 'id' | 'commitRepos' | 'allowedDomains'>): void {
  const problems = ccRepoAccessProblems(agent);
  if (problems.length > 0) throw new ConfigValidationError(`Agent "${agent.id}" ${problems.join('; ')}`);
}

/** The repository access an Agent's CC sessions carry; undefined when it configures none. */
export function agentRepoAccess(agent: Pick<AgentConfig, 'commitRepos' | 'allowedDomains'>): CcRepoAccess | undefined {
  if (!agent.commitRepos?.length && !agent.allowedDomains?.length) return undefined;
  return { commitRepos: agent.commitRepos ?? [], allowedDomains: agent.allowedDomains ?? [] };
}

// --- Runtime Selection ---

export type RuntimeId = 'cc' | 'cc-interactive' | 'codex' | 'agy';
export type ConversationRuntimeId = 'cc' | 'codex' | 'cc-sdk' | 'pi' | 'agy';

export type ConversationReasoningLevel = 'off' | 'minimal' | 'low' | 'medium' | 'high' | 'xhigh' | 'max';

export interface ConversationModelRoute {
  provider: string;
  model: string;
  reasoning: ConversationReasoningLevel;
}

/** Runtime, model, reasoning, isolation and runtime-native options are separate
 * axes. `runtimeOptions` is namespaced so a fourth runtime does not leak fields
 * into the shared spawn/state contract. */
export interface ConversationRuntimeRoute {
  runtime: ConversationRuntimeId;
  model?: ConversationModelRoute;
  isolationProfile?: string;
  runtimeOptions?: Partial<Record<ConversationRuntimeId, Record<string, unknown>>>;
}

/** Reviewed node capability used to materialize an agent route on explicit
 * `/runtime` selection. It is capability supply, not an automatic fallback. */
export type ConversationRuntimeDefault = Omit<ConversationRuntimeRoute, 'runtime'>;

export interface ConversationRuntimeSessionBinding {
  runtime: ConversationRuntimeId;
  sessionId: string;
}

export type ConversationTurnEffect = 'no-effect' | 'read-only-effect' | 'side-effect-observed';

/**
 * One turn handed to the conversation runtime: its place in the arrival order
 * and its admission receipt.
 */
export interface TurnArrival {
  /** Process-wide monotonic arrival number (see nextTurnSeq). Orders turns
   *  recorded at shutdown across the barrier and the runtime queues, and
   *  identifies a recorded turn when it is replayed. */
  seq: number;
  /** Called (possibly more than once) when the turn sits in a record that a
   *  shutdown hands over for resume: the barrier, a runtime queue, a runtime's
   *  dispatch preparation or its in-flight turn. */
  accepted(): void;
  /** A durable record upstream (channel intake journal, pending list or
   *  durable entry) holds the turn until `accepted`; the switch barrier then
   *  withholds the receipt instead of recording the turn itself. */
  owned?: boolean;
  /** Sent by the channel resume pass (its resume prompt or a pending-list
   *  record): passes while the maintenance barrier holds other turns back
   *  for that pass. */
  resume?: boolean;
}

/** What a channel may attach to a turn it submits. */
export interface TurnSubmitOptions {
  /** Arrival number of a recorded turn being replayed; a new one otherwise. */
  seq?: number;
  /** Admission receipt; see TurnArrival.accepted. */
  onAccepted?: () => void;
  /** Set by the channel resume pass; see TurnArrival.resume. */
  resume?: boolean;
}

export interface ConversationTurnContext {
  /** Arrival and admission receipt; set by the runtime manager. */
  arrival?: TurnArrival;
  correlationId: string;
  /** Opaque Host-owned attempt identity, propagated by native adapters only. */
  hostAttemptRef?: string;
  idempotencyKey: string;
  effect: ConversationTurnEffect;
  /** Internal control turns are observable to Relay consumers but not channels. */
  deliveryVisibility?: 'public' | 'control';
  /** The Host's last edit of a turn's input (the context rollover hint),
   *  applied by an adapter with `preparesInput` each time it writes the turn to
   *  a runtime process, a replay into a replacement process included, with the
   *  session and mode of the process that receives it. */
  prepareInput?: (message: StreamJsonMessage, receiver: TurnInputReceiver) => StreamJsonMessage;
}

/** The runtime process a turn's input is written to: the native session it
 *  was started with (exact in CC print mode before its init arrives) and its
 *  CC mode. */
export interface TurnInputReceiver {
  sessionId: string;
  mode?: 'print' | 'interactive';
}

/** Per-agent auth/endpoint selection for the cc-sdk runtime. Pure config; the
 *  adapter injects the matching session env at spawn. 'apikey' is reserved. */
export type CcSdkProviderMode = 'subscription' | 'apikey';
export type CCMode = 'print' | 'interactive';

// --- Logical Gateway Contracts (v1.1 internal view) ---

export type GatewayChannel = 'telegram' | 'feishu' | 'harness' | 'system';
export type PrincipalType = 'human' | 'agent' | 'system';
export type ChannelAddressKind =
  | 'telegram_chat'
  | 'feishu_open_id'
  | 'feishu_chat_id'
  | 'harness_target'
  | 'system';

export interface Principal {
  id: string;
  type: PrincipalType;
  tenantId: string;
  displayName?: string;
}

export interface ChannelAddress {
  channel: GatewayChannel;
  kind: ChannelAddressKind;
  value: string;
  botId?: string;
  appId?: string;
}

export interface ChannelBinding {
  id: string;
  address: ChannelAddress;
  principalId: string;
  tenantId: string;
  workspaceId: string;
  defaultAgentId: string;
  allowedAgentIds: string[];
  defaultRuntimePreference?: ConversationRuntimeId[];
  auditTags?: string[];
}

export type ChannelBindingStatus = 'active' | 'pending' | 'revoked';

export interface ChannelBindingRecord {
  id: string;
  address: ChannelAddress;
  principalId: string;
  tenantId: string;
  defaultAgentId: string;
  allowedAgentIds: string[];
  status: ChannelBindingStatus;
  createdAt: string;
  approvedAt?: string;
  approvedBy?: string;
  defaultRuntimePreference?: ConversationRuntimeId[];
  auditTags?: string[];
}

export interface GatewayEvent {
  id: string;
  createdAt: string;
  channel: GatewayChannel;
  address: ChannelAddress;
  text?: string;
  command?: string;
  attachments?: unknown[];
  raw?: unknown;
}

export interface RoutePolicyResult {
  principal: Principal;
  binding: ChannelBinding;
  agentId: string;
  workspace: string;
  sessionId: string;
  runtimePreference: ConversationRuntimeId[];
  conversationKey: string;
  audit: {
    correlationId: string;
    source: 's1-inbound' | 's2-job' | 's2-escalation';
  };
}

export type DeliveryStatus = 'sent' | 'failed' | 'skipped';

export interface DeliveryRequest {
  id: string;
  correlationId: string;
  channel: GatewayChannel;
  address: ChannelAddress;
  payload:
    | { kind: 'text'; text: string }
    | { kind: 'document'; path: string; caption?: string }
    | { kind: 'card'; markdown: string; title?: string; card?: Record<string, unknown> }
    | { kind: 'escalation'; title: string; body: string };
}

export interface DeliveryReceipt {
  id: string;
  requestId: string;
  correlationId: string;
  status: DeliveryStatus;
  channel: GatewayChannel;
  address: ChannelAddress;
  externalMessageId?: string;
  attempts: number;
  error?: string;
  deliveredAt?: string;
}

export interface ContextSoftWarningEvent {
  tenantId: string;
  sessionId: string;
  runtime: ConversationRuntimeId;
  currentSize: number;
  limit: number;
  ratio: number;
  /** Model id used to choose `limit` (`result.model` / `usage.model`, else CC model env). */
  modelId?: string;
}

export interface SoftQuotaWarningEvent {
  tenantId: string;
  agentId: string;
  monthKey: string;
  currentCostUsd: number;
  budgetUsd: number;
  ratio: number;
}

export interface ConversationRuntimeFallbackEvent {
  tenantId: string;
  from: ConversationRuntimeId;
  to: ConversationRuntimeId;
  reason: string;
  attemptIndex: number;
  correlationId?: string;
  idempotencyKey?: string;
  effect?: ConversationTurnEffect;
}

/** Emitted when the pool switches to the next available catalog model by provider priority. */
export interface ProviderFallbackEvent {
  tenantId: string;
  fromModel: string;
  toModel: string;
  reason: string;
}

/** Emitted when resume is abandoned due to a broken session jsonl parentUuid chain. */
export interface SessionChainRecoveryEvent {
  tenantId: string;
  previousSessionId: string;
  newSessionId: string;
  previousSessionJsonlPath: string;
  missingParentUuid?: string;
}

// --- Process State Machine ---
// starting → ready → busy → idle → dead
// See design.md §4 Data Model / State Machine

export type ProcessState = 'starting' | 'ready' | 'busy' | 'idle' | 'dead';

// --- CC Process Instance ---

export interface CCProcess {
  /** Native workstation authentication used by this child. */
  ccAuthMode?: import('./cc-subscription-auth.js').CcAuthMode;
  tenantId: string;
  process: ChildProcess;
  /** Per-spawn credential helper file; removed when the child exits or is killed. */
  apiKeyFilePath?: string;
  /** Per-spawn Pi agent directory of a bounded session (leasePiAgentDir); released when the child exits or is killed. */
  piAgentLease?: PiAgentLease;
  sessionId: string;
  state: ProcessState;
  lastActivity: number;       // Date.now()
  spawnedAt: number;
  restartCount: number;
  restartWindowStart: number;
  /** Digest of the WI runtime usage binding injected into this child, if any. */
  usageBindingSha256?: string;
}

// --- Fallback Context ---

export interface FallbackContext {
  /** Path to the previous session's JSONL file (agent can read it for history). */
  previousSessionJsonlPath: string;
  /** Previous runtime id (e.g. 'cc', 'codex'). */
  previousRuntime?: string;
  /** Previous model id (e.g. 'claude-opus-4'). */
  previousModel?: string;
  /** Human-readable reason for the fallback. */
  reason: string;
}

// --- Spawn Configuration ---

export interface SpawnConfig {
  /** Original Host handover transport; never a business execution grant. */
  nativeHandoverRef?: string;
  tenantId: string;
  workspace: string;
  sessionId: string;
  maxTurns?: number;           // default 1000
  allowedTools?: string[];
  /** CC `--model` for this spawn (pool builds argv after session args). */
  ccModel?: string;
  /** The conversation's /think level, mapped by each runtime (see reasoning-control.ts). */
  reasoningLevel?: ConversationReasoningLevel;
  /** CC process mode for this spawn. Overrides provider-aware auto mode when no global override is set. */
  ccMode?: CCMode;
  /** Agent's `ccPrompt`. */
  ccPrompt?: import('./cc-session-settings.js').CcPromptOptions;
  /** Auth/endpoint mode for the `cc-sdk` runtime spawn (subscription/apikey).
   *  Ignored by the legacy `cc` / `codex` runtimes. */
  ccSdkProviderMode?: CcSdkProviderMode;
  /** Preferred structured route for all new runtimes. Legacy fields above stay
   * readable for cc/codex/cc-sdk compatibility. */
  conversationRuntime?: ConversationRuntimeRoute;
  /** Durable source runtime for a resumed session. A mismatch with the selected
   * structured route is rejected rather than rewritten as a fake migration. */
  runtimeSessionBinding?: ConversationRuntimeSessionBinding;
  /** Host-owned WI/phase/epoch/procurement attribution. It contains no raw
   * provider session/thread/message identity and must match the selected runtime. */
  computeCostBinding?: RuntimeUsageBinding;
  /** Paths the kernel opened to this session: its grants and its Agent's standing read grants. */
  sessionAccess?: { path: string; mode: 'read' | 'write'; standing?: true }[];
  /** Ready workspaces the kernel allocated to the WIs this session holds; written like a write grant. */
  allocatedWorkspaces?: { workItemId: string; resourceId: string; path: string; write: string[]; deny: string[] }[];
  /** The Agent's Harness principal, whose standing grants every session start applies. */
  harnessAgentId?: string;
  measurementBaselineReference?: CanonicalMeasurementBaselineReference;
  /** Resolved by the authenticated Host route (not prompt content) when no WI
   * binding exists, so metering still identifies the configured executor. */
  meteringAgentId?: string;
  /** Host-known purpose of a paid invitation/probe before execution is granted.
   * Metering only: never activates a SCL binding, baseline, workspace or permission. */
  meteringWorkItemId?: string;
  /** Codex YOLO default for this tenant; session commands may override it in memory. */
  codexYoloDefault?: boolean;
  /** Agent's `sessionWiGuard`: WI declaration reminder (CC, Codex and Pi) and CC write-operation guard. */
  sessionWiGuard?: boolean;
  /** Agent's `ccWriteBoundary`: writes outside the session's write roots are rejected. */
  ccWriteBoundary?: boolean;
  /** Agent's `commitRepos` and `allowedDomains`, applied inside its `ccWriteBoundary`. */
  repoAccess?: CcRepoAccess;
  /** Auxiliary vision model config (per-agent, overrides env-level config). */
  visionModel?: VisionModelConfig;
  resume?: boolean;            // default false
  /** v0.3.0: Linux user for per-tenant file isolation (e.g. "tenant-member-0001").
   *  When set, spawn prefixes with `sudo -u {linuxUser}`. */
  linuxUser?: string;
  /** HOME dir to inject when spawning CC for non-linuxUser agents, so session files
   *  land in the real user's ~/.claude/projects/ directory. */
  userHome?: string;
  /** When set, the new session was created by a model/runtime fallback. Injected into --append-system-prompt. */
  fallbackContext?: FallbackContext;
  /** Wall-clock (Date.now()) of the session's last recorded activity, as known
   *  to state.json for this pool key. Used as a sanity reference before --resume:
   *  if the on-disk jsonl's last record is far OLDER than this, the sessionId
   *  has drifted to a stale/wrong jsonl and resuming it would load a chronologically
   *  impossible session — so we start fresh instead (memo 2026-06-26). */
  expectedLastActivity?: number;
}

// --- Pool Options ---

export interface PoolOptions {
  idleTimeoutMs?: number;       // default 30 * 60 * 1000
  /** Max residency while background work is pending; default 4 h (RELAY_RUNTIME_BACKGROUND_HARD_CAP_MS). */
  backgroundWorkHardCapMs?: number;
  cleanupIntervalMs?: number;   // default 60 * 1000
  maxRestartsPerHour?: number;  // default 3
  maxRateLimitRetriesPerMessage?: number; // default 0
  maxInteractiveAcceptRetriesPerMessage?: number; // default 1
  rateLimitRestartDelayMs?: number; // default 30_000
  rateLimitAbortTimeoutMs?: number; // default 90_000
  /** default 90_000; override with `RELAY_INTERACTIVE_ACCEPT_TIMEOUT_MS` (ms, 5_000–600_000) */
  interactiveAcceptTimeoutMs?: number;
  /** default 60_000; how long after last PTY stdout before the re-armed accept timer fires */
  interactiveAcceptStdoutIdleMs?: number;
  interactiveSubmitDelayMs?: number; // default 300
  interactiveWarmupMs?: number; // default 3_000
  interactiveSlashCommandSettleMs?: number; // default 30_000
  busyTimeoutMs?: number;       // default 30 * 60 * 1000 — busy check interval (periodic notification, no auto-kill)
  maxBusyChecks?: number;       // default 10 — progress notifications before asking user to /stop
  ccMode?: CCMode;              // default auto (interactive when the catalog provider sets force_interactive, else print)
  killTimeoutMs?: number;       // default 5000
  primaryToken?: string;        // CLAUDE_CODE_OAUTH_TOKEN (env fallback)
  fallbackToken?: string;       // CLAUDE_CODE_OAUTH_TOKEN_FALLBACK (env fallback)
  /** Sole subscription source when set; reread on every CC process spawn. */
  oauthEnvFile?: string;
  /** Called during emitShutdown / busy-SIGTERM for each busy session.
   *  Fires for every busy session regardless of whether lastInFlight has text:
   *  busy = "user is waiting for a reply", and that's what the resume path needs
   *  to know. messageText may be empty when the turn was already mid-flight with
   *  CC (e.g. long thinking, 429 internal retry) and no fresh Relay→CC send. */
  onShutdownInterrupt?: (tenantId: string, payload: { messageText: string }) => void;
  /** Called with each session's turns not yet sent when the shutdown begins
   *  (queued behind a busy turn or still preparing their dispatch), and with
   *  every turn that arrives afterwards. They are never sent to the stopping
   *  process. */
  onShutdownQueued?: (tenantId: string, turns: Array<{ queuedAt: number; seq: number; message: StreamJsonMessage }>) => void;
  /** Model catalog for provider model routing (P0-004) */
  modelCatalog?: import('./model-catalog.js').ModelCatalog;
  /** Return true if the tenant has a pending interactive question (AskUserQuestion)
   *  and should not be idle-reclaimed while waiting for user response. */
  isPendingInteractive?: (tenantId: string) => boolean;
  /** ComputeCost session WI MCP registered for every CC session when its host resources are available. */
  sessionWiMcp?: import('./cc-session-wi-mcp.js').CcSessionWiMcp;
  /** Directory of each CC session's starting `ccPrompt` (cc-prompt-record.ts); default `<Relay dataDir>/cc-prompt-sessions`. */
  ccPromptRecordDir?: string;
}

export type TokenTier = 'primary' | 'fallback';

/** Optional metadata on CC pool `exit` events (Feishu/TG recovery hints). */
export interface PoolExitMeta {
  suppressGenericRecoveryToast?: boolean;
  /** Opaque Host attempt identity for a per-turn native child exit. */
  hostAttemptRef?: string;
}

// --- Stream JSON Protocol ---

export interface StreamJsonMessage {
  type: string;                 // 'human' | 'assistant' | 'result' | ...
  content?: string | unknown[];  // string for text, array for multimodal content blocks
  [key: string]: unknown;
}

// --- Pool Interface ---

export interface Pool {
  spawn(config: SpawnConfig): Promise<CCProcess>;
  /** Bound session + transcript used by Host-side CC usage measurement. */
  usageMeasurementContext?(tenantId: string): import('./cc-usage-measurement.js').CcUsageMeasurementContext | undefined;
  send(tenantId: string, message: StreamJsonMessage, turn?: ConversationTurnContext): Promise<void>;
  /** Calls `turn.arrival.accepted()` once the turn is queued, preparing its
   *  dispatch or in flight (see ConversationRuntimeAdapter.admitsTurns). */
  readonly admitsTurns?: boolean;
  /** Applies `turn.prepareInput` as it writes a new turn to CC. */
  readonly preparesInput?: boolean;
  /** Checks an internal delivery's target session as it writes the turn (internal-turn-fence.ts). */
  readonly fencesInternalTurns?: boolean;
  /** Write raw keystrokes to an interactive CC's PTY stdin (e.g. AskUserQuestion
   *  answers). Optional so lightweight mocks stay valid; createPool always provides it. */
  injectInput?(tenantId: string, text: string): Promise<void>;
  kill(tenantId: string): Promise<void>;
  killAll(): Promise<void>;
  get(tenantId: string): CCProcess | undefined;
  getAll(): CCProcess[];
  getStatus(): ProcessStatusInfo[];
  on(event: 'turn-accepted', handler: (tenantId: string, meta?: import('./runtime-background-work.js').TurnAcceptedMeta) => void): void;
  on(event: 'response', handler: (tenantId: string, msg: StreamJsonMessage) => void): void;
  on(event: 'exit', handler: (tenantId: string, code: number | null, meta?: PoolExitMeta) => void): void;
  on(event: 'error', handler: (tenantId: string, error: Error) => void): void;
  on(event: 'turn-warning', handler: (tenantId: string, currentTurn: number, maxTurns: number) => void): void;
  on(event: 'session-end', handler: (tenantId: string, reason: SessionEndReason) => void): void;
  on(event: 'crash-retry', handler: (tenantId: string) => void): void;
  /** Native adapter restarted and replayed the active turn; a new provider attempt. */
  on(event: 'turn-replay', handler: (tenantId: string) => void): void;
  on(event: 'tool-files', handler: (tenantId: string, filePaths: string[], workspace: string) => void): void;
  on(event: 'busy-progress', handler: (tenantId: string, progress: BusyProgressEvent) => void): void;
  on(event: 'provider-fallback', handler: (tenantId: string, event: ProviderFallbackEvent) => void): void;
  on(event: 'session-chain-recovery', handler: (tenantId: string, event: SessionChainRecoveryEvent) => void): void;
  /** 429 退避循环到达通知阈值时触发一次，channels 仅发用户提示，不停 typing、不清状态。 */
  on(event: 'rate-limit-notice', handler: (tenantId: string, info: { retryCount: number; nextDelayMs: number }) => void): void;
  on(event: 'turn-effect', handler: (tenantId: string, effect: ConversationTurnEffect) => void): void;
  on(event: 'stream-delta', handler: (tenantId: string, delta: string, correlationId?: string) => void): void;
  on(event: 'native-trace', handler: (tenantId: string, trace: Record<string, unknown>) => void): void;
  /** Emit relay-shutdown session-end for all active processes. Returns notified tenantIds. */
  emitShutdown(): Set<string>;
  /** Wait up to timeoutMs for all busy sessions to transition out of busy
   *  (i.e., until CC SDK has flushed the current turn to jsonl). Returns the
   *  set of tenantIds still busy at timeout. Use during graceful shutdown to
   *  protect in-flight jsonl writes. */
  drainBusySessions(timeoutMs: number): Promise<Set<string>>;
  dispose(): void;
}

export interface ProcessStatusInfo {
  tenantId: string;
  state: ProcessState;
  pid: number | undefined;
  lastActivity: number;
  spawnedAt: number;
  restartCount: number;
}

// --- Session Resilience (F1) ---

export type SessionEndReason =
  | { type: 'idle-reclaimed' }
  | { type: 'turns-exhausted'; turnCount: number; maxTurns: number }
  | { type: 'crash'; exitCode: number }
  | { type: 'crash-unrecoverable'; exitCode: number }
  /** Claude reports this session id is held by another process; not a crash loop. */
  | { type: 'session-in-use' }
  | { type: 'rate-limited'; retryCount: number }
  | { type: 'timeout'; timeoutMs: number }
  | { type: 'runtime-failed'; runtime: ConversationRuntimeId; correlationId?: string }
  | {
      type: 'runtime-recovery-required';
      correlationId: string;
      runtime: ConversationRuntimeId;
      effect: ConversationTurnEffect;
      reason: string;
    }
  /** wasBusy=true if the process had an in-flight user turn at shutdown time.
   *  Channels SHOULD only surface a "Relay restarting" notice to users when
   *  wasBusy is true — idle agents have nobody waiting and the message is noise. */
  | { type: 'relay-shutdown'; wasBusy: boolean };

export interface BusyProgressEvent {
  elapsedMs: number;
  checkCount: number;
  maxChecks: number;
  isFinalCheck: boolean;
}

export type SessionRecoveryReason =
  | 'context-window-limit'
  | 'compact-failed'
  | 'wrap-cannot-run';

export type WorkItemEntryStage =
  | 'planned'
  | 'harness_prepare'
  | 'usage_binding'
  | 'manifest_preflight'
  | 'source_seal'
  | 'source_stop'
  | 'source_restore'
  | 'target_spawn'
  | 'target_readback'
  | 'target_baseline'
  | 'prompt_dispatch'
  | 'first_result'
  | 'harness_apply'
  | 'harness_recovery'
  | 'state_commit';

export interface WorkItemEntryFailure {
  stage: WorkItemEntryStage;
  code: string;
  message: string;
  at: string;
}

/** Evidence from an actual runtime result event; never an authority grant. */
export interface WorkItemBusinessResultReceipt {
  schema: 'pulse_work_item_business_result/v1';
  epoch: number;
  operationRef: string;
  sessionRef: string;
  resultSha256: string;
  settledAt: string;
}

export interface PendingWorkItemEntry {
  schema: 'pulse_pending_work_item_entry/v1' | 'pulse_pending_work_item_entry/v2';
  workItemId: string;
  sessionId: string;
  sessionRef: string;
  runtime: ConversationRuntimeId;
  state:
    | 'planned'
    | 'prepared'
    | 'bound'
    | 'activating'
    | 'dispatching'
    | 'settled'
    | 'applying'
    | 'recovery_required';
  operationRef?: string;
  entryProtocol?: 'local_scl' | 'legacy';
  /** Exact canonical prepare/apply epoch; a recovery projection, not a grant or measurement. */
  executionEpoch?: number;
  executionPhase?: string;
  /** Original execution principal; never inferred from measurement. Not an authority grant. */
  executionAgentId?: string;
  businessDispatchState?: 'not_started' | 'sending' | 'accepted' | 'unknown';
  businessResultReceipt?: WorkItemBusinessResultReceipt;
  launchWorkspace?: string;
  launchRuntimeClass?: string;
  launchSurfaceId?: string;
  launchAuthorityState?: 'prepared' | 'recovery_required' | 'active';
  workspaceManifestSha256?: string;
  resumePrompt?: string;
  attemptCount?: number;
  lastError?: WorkItemEntryFailure;
  usageBinding?: RuntimeUsageBinding;
}

export interface ActiveWorkItemRuntimeBinding {
  businessResultReceipt?: WorkItemBusinessResultReceipt;
  schema: 'pulse_active_work_item_runtime_binding/v1';
  workItemId: string;
  sessionId: string;
  sessionRef: string;
  runtime: ConversationRuntimeId;
  /** Historical measurement observation, never a prerequisite for execution identity. */
  usageBinding?: RuntimeUsageBinding;
  measurementBaselineReference?: CanonicalMeasurementBaselineReference;
  workspace?: string;
}

/** One native session's whole identity unit, saved and restored together by `/wi switch`. */
export type SessionIdentityUnit = Pick<PersistedSession, 'sessionId'|'runtime'|'codexThreadId'|'codexThreadUsage'
  |'agyConversation'|'agyWorkItemBinding'|'reasoningLevel'|'activeWorkItemRuntimeBinding'|'pendingWorkItemEntry'|'terminalWorkItemRuntimeBinding'>;
export interface WorkItemSession {unit:SessionIdentityUnit;at:string}

export interface PersistedSession {
  pendingNativeRollover?: import("./native-session-rollover.js").NativeRolloverPlan;
  nativeSessionHistory?: Array<{sessionId:string;runtime:ConversationRuntimeId;rolloverRef:string;activeWorkItemRuntimeBinding?:ActiveWorkItemRuntimeBinding}>;
  /** `/wi switch`: the native session each work item last ran in, for returning to it. */
  workItemSessions?: Record<string, WorkItemSession>;
  /** A `/wi switch` from replacing the session until its enter settles (`settleInterruptedSwitch`). */
  pendingWiSwitch?: import("./native-wi-switch.js").PendingWiSwitch;
  runtimeUsageBaseline?: {status:'unavailable';observedAt:string};
  /** User scope fence survives intent delivery and runtime restart recovery. */
  sclBusinessDispatchAllowed?: boolean;
  poolKey: string;
  agentId: string;
  runtime?: ConversationRuntimeId; // default 'cc' for old state files
  sessionId: string;
  codexThreadId?: string;
  /** Codex thread-cumulative usage after its last Relay turn (receipt baseline). */
  codexThreadUsage?: {threadId: string; usage: Record<string, number>};
  /** agy conversation resumed with `--conversation`, bound to the Relay session that started it;
   *  `usage` is agy's conversation-cumulative total after the last Relay turn. */
  agyConversation?: {sessionId: string; conversationId: string; usage?: Record<string, number>};
  /** The WI a /wi command handed this agy conversation: agy has no tools to declare one, so
   *  Relay attributes the conversation's turns to it with this binding. `conversationId` is the
   *  native conversation the core recognized; absent only for a rollover successor before its
   *  first conversation, which then adopts the first one agy starts. */
  agyWorkItemBinding?: {sessionId: string; conversationId?: string; usageBinding: RuntimeUsageBinding};
  /** /think level of this conversation; each runtime maps it (reasoning-control.ts). Cleared with the session. */
  reasoningLevel?: ConversationReasoningLevel;
  wrapRequestedAt?: number;
  recoveryRequiredAt?: number;
  recoveryReason?: SessionRecoveryReason;
  turnCount: number;
  maxTurns: number;
  lastActivity: number;
  status: 'active' | 'idle-reclaimed' | 'turns-exhausted' | 'crashed' | 'recovery-required';
  /** User message that was in-flight when Relay shut down; injected as context on next resume.
   *  May be absent even when wasBusyAtShutdown=true: the turn was already inside CC
   *  (long thinking / 429 internal retry) and Relay had no in-flight send to record. */
  interruptedUserMessage?: string;
  /** Process was busy (state==='busy') at shutdown — user was waiting for a reply.
   *  Set by pool.ts onShutdownInterrupt; cleared by channels.notifyResumed after the
   *  resume turn is dispatched. This is the canonical "user is waiting" signal —
   *  unlike interruptedUserMessage it does not depend on Relay holding a fresh
   *  doSend at the SIGTERM instant. */
  wasBusyAtShutdown?: boolean;
  /** When the shutdown that set wasBusyAtShutdown interrupted this session.
   *  Resume windows measure from here: a long turn may have had no recorded
   *  activity for longer than the window before it was interrupted. */
  shutdownInterruptedAt?: number;
  /** Turns accepted while Relay was shutting down (maintenance barrier) and
   *  never dispatched, complete messages in arrival order. Kept apart from the
   *  interrupted turn above, which a runtime's shutdown callback may rewrite.
   *  Replayed after that turn's resume; each is removed once the runtime
   *  accepted it. `seq` is the turn's arrival number (absent in records from
   *  before it existed). */
  pendingMaintenanceTurns?: Array<{ queuedAt: number; seq?: number; message: StreamJsonMessage }>;
  /** Crash-safe private host plan. Raw runtime identity remains local Relay state and
   * is never projected into Harness or Git. */
  pendingWorkItemEntry?: PendingWorkItemEntry;
  /** A newer canonical reinit proved the reserved target obsolete. Keep its
   * exact local request for audit; this record grants no target authority. */
  supersededWorkItemEntries?: Array<{entry:PendingWorkItemEntry;transactionRef:string;supersededAt:string}>;
  stoppedWorkItemEntries?: Array<{entry:PendingWorkItemEntry;sourceSessionId:string;stoppedAt:string}>;
  /** Correlated usage authority retained after first-prompt dispatch so an exact
   * process restart cannot silently resume the Session without its binding. */
  activeWorkItemRuntimeBinding?: ActiveWorkItemRuntimeBinding;
  /** Historical bindings replaced only under an exact canonical reinit receipt. */
  runtimeBindingRecoveries?:Array<{operationId:string;epoch:number;previous:ActiveWorkItemRuntimeBinding;adoptedAt:string}>;
  /** Former local conversations; ongoing WI and successor authority are unchanged. */
  releasedWorkItemRuntimeBindings?: Array<{
    binding: ActiveWorkItemRuntimeBinding;
    reason: 'transferred' | 'rollover_prepared';
    localRevision: number;
    stateDigest: string;
    observedAt: string;
  }>;
  /** /reset retires only the local conversation. This is not SCL transfer proof. */
  forcedSessionResets?: Array<{
    sessionId: string;
    resetAt: string;
    binding?: ActiveWorkItemRuntimeBinding;
  }>;
  /** A signed Host stop revoked local execution before canonical settlement.
   * Historical attribution only; never grants workspace or execution authority. */
  stoppedWorkItemRuntimeBindings?: Array<{
    binding: ActiveWorkItemRuntimeBinding;
    reason: 'human_cancelled';
    observedAt: string;
  }>;
  /** Historical attribution only; never grants workspace or execution authority. */
  terminalWorkItemRuntimeBinding?: {
    binding: ActiveWorkItemRuntimeBinding;
    lifecycleStatus: string;
    localRevision: number;
    stateDigest: string;
    observedAt: string;
  };
}

/** A channel update acknowledged to its platform but not yet handed to a runtime. */
export interface ChannelIntakeRecord {
  /** Lane the update runs in (the chat); one lane keeps arrival order. */
  lane: string;
  /** Platform id of the update, unique per channel journal. */
  id: string;
  receivedAt: number;
  /** The raw update, replayed through the channel's handlers after restart. */
  payload: unknown;
}

export interface PersistedState {
  version: 1;
  updatedAt: string;
  sessions: Record<string, PersistedSession>;
  /** Channel intake journals by channel and bot (`telegram:<agentId>`), in arrival order. */
  channelIntake?: Record<string, ChannelIntakeRecord[]>;
}

export interface StateManager {
  readonly statePath?:string;
  load(): PersistedState;
  getSession(poolKey: string): PersistedSession | undefined;
  updateSession(poolKey: string, update: Partial<PersistedSession>): void;
  removeSession(poolKey: string): void;
  getAllSessions(): Record<string, PersistedSession>;
  /** Intake journal of one channel bot; empty when none was recorded. */
  getChannelIntake?(key: string): ChannelIntakeRecord[];
  /** Replace one intake journal; written with the next save. */
  setChannelIntake?(key: string, records: ChannelIntakeRecord[]): void;
  flush(): void;
  /** Synchronous write that propagates an unrecoverable persistence failure. */
  flushStrict(): void;
  dispose(): void;
}

// --- Command Feedback (F2) ---

export interface MenuCommand {
  command: string;
  description: string;
}

// ─────────────────────────────────────────────────────────────────────────────
// Agent Scheduler Types (agent-scheduler feature)
// ─────────────────────────────────────────────────────────────────────────────

/** A single scheduled job definition, with run state merged from cron-jobs-state.json */
/** Privacy-safe observation attached to the existing native cron state, not a WI ledger. */
export interface JobRunObservation {
  runId?: string;
  definitionHash?: string;
  startedAt: string;
  finishedAt?: string;
  scheduledFor?: string;
  phase?: 'queued' | 'running';
  schedulerPid: number;
  schedulerStartedAt: number;
  status?: 'ok' | 'error' | 'timeout';
  executionStatus?: 'ok' | 'error' | 'timeout';
  deliveryStatus?: DeliveryStatus;
  artifactVerified?: boolean;
  /** This run's own `agent.verify` outcome; absent when no verify ran. */
  verifyStatus?: 'passed' | 'failed';
  /** First 500 characters of a skipping gate's output. */
  skipSummary?: string;
  skipped?: boolean;
  deferred?: boolean;
}

export interface ScheduledJob {
  id: string;                         // Unique ID, kebab-case
  name: string;                       // Human-readable name
  enabled: boolean;
  owner: string;
  /** WorkItem its usage belongs to; absent, ComputeCost names the usage by this job. */
  wi?: string;
  /** Exactly one execution node when node-placement is enabled. */
  runNode?: string;
  /**
   * A wake job starts no batch session: its gate (`collab-inbox-gate.py wake`)
   * hands `agent.prompt` to the one Relay conversation that holds this WI, queued
   * while that conversation is busy. When no conversation here holds it, the
   * scheduler reminds the owner's human through `delivery` instead.
   */
  wake?: { workItemId: string };
  /** Created through the Agent's own `pulse_jobs` tool (agent-jobs.ts); only such jobs it edits. */
  managedBy?: 'agent';
  schedule: {
    expr: string;                     // 5-field cron expression
    tz: string;                       // IANA timezone, e.g. "Asia/Shanghai"
  };
  agent: {
    workspace: string;                // CC process cwd (determines CLAUDE.md persona)
    prompt: string;                   // Task instruction injected into Agent
    timeout?: number;                 // Seconds, default 300
    maxTurns?: number;                // CC --max-turns for batch jobs, default 1000
    thinkingMode?: 'low' | 'high';   // Reserved for CC --model param (P1)
    runtimePreference?: RuntimeId[];  // Preferred batch runtimes, ordered
    codexYolo?: boolean;              // Batch Codex fallback uses --dangerously-bypass-approvals-and-sandbox when true
    computeProfile?: string;          // Central profile name; absent uses platform_default. Model literals are forbidden here.
    gate?: {
      command: string;                // Deterministic pre-LLM executable (no shell)
      args?: string[];
      timeout?: number;               // Seconds, default 30
      skipExitCode?: number;          // Default 3: successful no-op, no LLM is started
      skipOutput?: string;            // Optional exact stdout marker required for a successful skip
    };
    inject_file?: string;             // File to inject into prompt at fire time (relative to workspace or absolute)
    /**
     * Post-run artifact check. When set, a zero exit code is not enough for
     * `executionStatus: 'ok'` — the named file must also exist and be a
     * non-empty regular file, or the run is recorded as an error (and the
     * opt-in Feishu failure alert fires).
     *
     * Exists because a batch agent can exit 0 having produced nothing: the
     * 2026-08-09 S1 W32 weekly review reported `lastStatus=ok` while no report
     * was written and no alert was pushed. Jobs delivering via
     * `feishu_document` / `telegram_document` already get this check for free
     * (the delivery path verifies `documentPath`); this field brings the same
     * guarantee to `delivery.mode: 'none'` jobs.
     *
     * Unset = no check, so existing jobs are unaffected.
     */
    expectArtifact?: {
      path: string;                   // Absolute/workspace-relative; supports ${DATE}, ${MONTH} (YYYY-MM) and ${ISO_WEEK}
      minBytes?: number;              // Default 1 — file must be non-empty
      maxAgeSeconds?: number;         // If set, mtime must be within this many seconds of run end
      contains?: string[];            // If set, every literal fragment must occur in the UTF-8 file
    };
    /**
     * Post-run verification command, symmetric to `gate`. Runs only after the
     * agent exited 0 and `expectArtifact` passed; a non-zero exit, timeout or
     * spawn failure records the run as an error (and the opt-in Feishu failure
     * alert fires). The command receives RELAY_JOB_ID, RELAY_JOB_RUN_ID,
     * RELAY_JOB_STARTED_AT (ISO-8601 UTC), RELAY_JOB_DATE (run start date in
     * schedule.tz), RELAY_JOB_WORKSPACE and RELAY_JOB_RUNTIME.
     */
    verify?: {
      command: string;                // Deterministic post-run executable (no shell)
      args?: string[];
      timeout?: number;               // Seconds, default 60
    };
  };
  delivery: {
    mode: 'announce' | 'none' | 'telegram_document' | 'feishu_document';
    channel?: 'telegram' | 'feishu';
    to?: string;                      // TG chat_id (required when mode=announce)
    botAgentId?: string;              // relay agent id whose TG bot delivers; overrides owner mapping, never falls back
    feishuOpenId?: string;            // Feishu open_id target (required when mode=feishu_document)
    documentPath?: string;            // file path template (telegram_document | feishu_document); relative = inside agent.workspace
    captionPath?: string;             // workspace-relative UTF-8 text file template, sent as a text message after the document; any failure fails the delivery
    caption?: string;                 // caption template (telegram_document; sent as a separate Feishu text for feishu_document)
    sendOutput?: boolean;             // telegram_document: after the document (and captionPath text), send the run's final reply as text
    skipIfOutputIncludes?: string[];  // whole-line output markers that skip delivery (announce | telegram_document | feishu_document)
    /**
     * Opt-in background progress notice for collaboration-inbox jobs. When true
     * and the pre-LLM gate prints a non-empty `pulse.collab_inbox_gate/v1`
     * batch, the scheduler posts "📨 后台正在处理协作合同。" with expandable
     * contract details to `feishuOpenId` (card) and/or `to` (Telegram) before
     * the run, then rewrites it with the outcome. Independent of `mode`; a
     * failed notice never changes the job result. Unset = no notice.
     */
    progressNotice?: boolean;
  };
  deleteAfterRun?: boolean;           // true = one-shot, remove from JSON after run [P1]
  maxConsecutiveErrors?: number;      // Circuit-breaker threshold, default 5 [P1]
  /**
   * A state-reconciling check (inbox scan, checkout convergence): each successful
   * run re-reads the whole current state, so it settles the job's earlier failed
   * slots. Unset = every failed slot stays owed until that slot itself succeeds.
   */
  convergent?: boolean;
  /**
   * Runs of this job that may be active at once, an integer 1-8; unset = 1.
   * Above 1 the job declares its runs independent of each other and of the
   * workspace's other jobs (each gate run claims its own work), so they skip
   * the workspace queue. Used by `<agent>-pr-review-*`.
   */
  maxConcurrent?: number;
  /**
   * true = the job only reads its workspace and writes elsewhere, so it does not
   * hold the workspace queue and the workspace's other jobs never wait behind it.
   * Its own runs stay capped by `maxConcurrent`. Used by long nightly jobs such as `hypnos`.
   */
  sharedWorkspace?: boolean;
  /**
   * true = run only the declared `agent.runtimePreference`, never failing over
   * to another runtime (heterogeneous review pairs). Unset = fail over.
   */
  runtimeStrict?: boolean;
  // Runtime state (written to cron-jobs-state.json by scheduler)
  _state?: {
    consecutiveErrors: number;
    lastRunAt?: string;               // ISO8601
    lastStatus?: 'ok' | 'error' | 'timeout';
    definitionHash?: string;          // sha256 of config definition (excludes _state)
    activatedAt?: string;             // earliest slot valid for definitionHash
    currentRun?: JobRunObservation;
    lastResult?: JobRunObservation;
    /** Different scheduled slots do not cancel each other's missing deliveries. */
    unresolvedResults?: JobRunObservation[];
    resultHistoryIncomplete?: boolean;
    /** First failure of the current failing streak; cleared by a success. */
    failingSince?: string;
    /** This failing streak has already alerted a human; cleared by a success. */
    failureAlerted?: boolean;
    /** Scheduled slots that hit the subscription limit, each run again after it resets. */
    quotaRetries?: QuotaRetry[];
  };
}

/** One scheduled slot waiting for a subscription limit to reset. */
export interface QuotaRetry {
  scheduledFor: string;               // ISO8601 slot that failed
  definitionHash?: string;            // definition the slot is retried under
  notBefore: string;                  // ISO8601, reset plus margin
  attempts: number;                   // retries scheduled for this slot so far
}

/** Internal result of a single job execution (not persisted to JSON) */
export interface JobResult {
  jobId: string;
  /** Unique scheduler execution id used to correlate cross-layer terminal receipts. */
  runId?: string;
  status: 'ok' | 'error' | 'timeout';
  executionStatus?: 'ok' | 'error' | 'timeout';
  deliveryStatus?: DeliveryStatus;
  deliveryReceipt?: DeliveryReceipt;
  output: string;                     // CC stdout (text mode)
  finalReply?: string;                // the run's last reply alone, when the runtime separates it
  durationMs: number;
  runtime?: RuntimeId;
  attemptedRuntimes?: RuntimeId[];
  errorMessage?: string;
  skipped?: boolean;
  reasonCode?: string;
  /** Work was intentionally not attempted yet (for example, agent lock held). */
  deferred?: boolean;
  /** Structured reason retained for watcher retry and audit logs. */
  deferredReason?: string;
  /** Outcome of `agent.verify` when it ran for this run; absent when it did not run. */
  verifyStatus?: 'passed' | 'failed';
  /** ISO reset instant of a subscription limit read from stdout or stderr. */
  quotaResetAt?: string;
}

/** Scheduler interface for cron job management */
export interface Scheduler {
  start(): void;
  stop(): void;
  reload(): void;
  /** IDs of jobs that have been claimed and have not finished yet. */
  getRunningJobIds(): string[];
  listJobs(owner?: string): ScheduledJob[];
  addJob(job: Omit<ScheduledJob, '_state'>): ScheduledJob;
  removeJob(id: string, owner: string): boolean;
  runJob(id: string): Promise<JobResult>;
  /** Start an unattended run now (see scheduler.ts `triggerJob`); never waits. */
  triggerJob(id: string): JobTriggerStatus;
}

/** How a run was started: on schedule, as a missed-slot catch-up, by an event, or by hand. */
export type JobTriggerSource = 'cron' | 'reconcile' | 'event' | 'manual';
/** `running`: a run of the job is claimed or queued; `held`: the maintenance barrier is held. */
export type JobTriggerStatus = 'started' | 'running' | 'held' | 'disabled' | 'not_found';

