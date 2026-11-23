import { abandonWiSwitch, switchInProgress } from './native-wi-switch.js';
import {channelConversationAgent} from './channel-conversation.js';
import type {
  AgentConfig,
  BusyProgressEvent,
  CCProcess,
  ContextSoftWarningEvent,
  ConversationRuntimeFallbackEvent,
  ConversationRuntimeId,
  PersistedSession,
  PoolExitMeta,
  SessionEndReason,
  SpawnConfig,
  StateManager,
  StreamJsonMessage,
} from './types.js';
import type { ConversationProcess } from './conversation-runtime.js';
import type { Router } from './router.js';
import { summarizeMonthlyUsage, type UsageSummary } from './metering.js';
import { existsSync } from 'node:fs';
import { CC_LINUX_USER_PRESERVED_ENV_KEYS, ccConfigDir, ccLaunchHome, getSessionJsonlPath, getBareSessionJsonlPath } from './pool.js';
import { buildSdkEngineEnv } from './cc-sdk-conversation-runtime.js';
import { conversationLaunch } from './conversation-launch.js';
import { persistedRuntimeUsageBinding } from './runtime-usage-binding.js';
import { acquireConversation } from './conversation-acquisition.js';
import { formatConversationPoolKey } from './conversation-address.js';
import { RuntimeUsageSealError, type RuntimeUsageBaselineReadback, type RuntimeUsageSealReadback } from './runtime-usage-seal.js';
import { resolveClaudeRuntimeModel } from './model-context.js';
import { INHERITED_ROUTING_ENV_KEYS } from './provider-routing-env.js';
import { AGY_MODEL_PROVIDER, DEFAULT_AGY_MODEL, agyEffectiveModel, agyIsolationRefusal, agyModelFamilies, agyModelFamily, agyModelForLevel, agyModelOptions, isAgyInstalled, isAgyModel } from './agy-conversation-runtime.js';
import { REASONING_LEVELS, describeClaudeDefaultReasoning, describeCodexDefaultReasoning, lastClaudeTurnEffort, parseReasoningLevel, reasoningControl } from './reasoning-control.js';

type RuntimeProcess = CCProcess | ConversationProcess;

export type InboundChannelHint = 'telegram' | 'feishu';

export interface CommandRuntime {
  spawn(config: SpawnConfig & { runtime?: ConversationRuntimeId; conversationRuntimePreference?: ConversationRuntimeId[]; codexThreadId?: string; codexYoloDefault?: boolean }): Promise<RuntimeProcess>;
  send(tenantId: string, message: StreamJsonMessage, options?: import('./types.js').TurnSubmitOptions): Promise<void>;
  /** Submit a crash-retryable control-plane turn with a durable idempotency key. */
  sendIdempotent?(tenantId: string, message: StreamJsonMessage, idempotencyKey: string, options?: import('./types.js').TurnSubmitOptions): Promise<void>;
  /** Inject raw keystrokes into an interactive CC PTY (AskUserQuestion answers). */
  injectInput?(tenantId: string, text: string): Promise<void>;
  /** Cancel only the active turn while preserving the runtime session. */
  abortTurn?(tenantId: string): Promise<void>;
  /** Switch the active runtime's native model without discarding its session. */
  setModel?(tenantId: string, model: import('./types.js').ConversationModelRoute): Promise<void>;
  /** List models from the active native runtime instead of a static Relay catalog. */
  listModels?(tenantId: string): Promise<import('./conversation-runtime.js').ConversationRuntimeModelOption[]>;
  setThinkingLevel?(tenantId: string, level: import('./types.js').ConversationReasoningLevel): Promise<void>;
  listThinkingLevels?(tenantId: string): Promise<import('./types.js').ConversationReasoningLevel[]>;
  bindRuntimeUsage?(tenantId: string, binding: import('./runtime-usage-binding.js').RuntimeUsageBinding): void;
  compact?(tenantId: string, customInstructions?: string): Promise<Record<string, unknown>>;
  getNativeState?(tenantId: string): Promise<Record<string, unknown>>;
  getNativeSessionStats?(tenantId: string): Promise<Record<string, unknown>>;
  activateRuntimeUsageBaseline?(tenantId: string): Promise<RuntimeUsageBaselineReadback>;
  sealRuntimeUsage?(tenantId: string): Promise<RuntimeUsageSealReadback>;
  kill(tenantId: string): Promise<void>;
  get(tenantId: string): RuntimeProcess | undefined;
  on(event: 'turn-accepted', handler: (tenantId: string, meta?: import('./runtime-background-work.js').TurnAcceptedMeta) => void): void;
  on(event: 'response', handler: (tenantId: string, msg: StreamJsonMessage) => void): void;
  on(event: 'exit', handler: (tenantId: string, code: number | null, meta?: PoolExitMeta) => void): void;
  on(event: 'error', handler: (tenantId: string, error: Error) => void): void;
  on(event: 'turn-warning', handler: (tenantId: string, currentTurn: number, maxTurns: number) => void): void;
  on(event: 'session-end', handler: (tenantId: string, reason: SessionEndReason) => void): void;
  on(event: 'crash-retry', handler: (tenantId: string) => void): void;
  on(event: 'tool-files', handler: (tenantId: string, filePaths: string[], workspace: string) => void): void;
  on(event: 'busy-progress', handler: (tenantId: string, progress: BusyProgressEvent) => void): void;
  on(event: 'provider-fallback', handler: (tenantId: string, event: import('./types.js').ProviderFallbackEvent) => void): void;
  on(event: 'session-chain-recovery', handler: (tenantId: string, event: import('./types.js').SessionChainRecoveryEvent) => void): void;
  on(event: 'rate-limit-notice', handler: (tenantId: string, info: { retryCount: number; nextDelayMs: number }) => void): void;
  on(event: 'turn-effect', handler: (tenantId: string, effect: import('./types.js').ConversationTurnEffect) => void): void;
  on(event: 'stream-delta', handler: (tenantId: string, delta: string, correlationId?: string) => void): void;
  on(event: 'native-trace', handler: (tenantId: string, trace: Record<string, unknown>) => void): void;
  setYoloMode?(tenantId: string, enabled: boolean): void;
  clearYoloMode?(tenantId: string): void;
  setYoloDefaultMode?(tenantId: string, enabled: boolean): void;
  getYoloOverrideMode?(tenantId: string): boolean | undefined;
  getYoloMode?(tenantId: string): boolean;
  onRuntimeFallback?(handler: (event: ConversationRuntimeFallbackEvent) => void): void;
  onContextSoftWarning?(handler: (event: ContextSoftWarningEvent) => void): void;
}

export interface CommandDeps {
  router: Router;
  runtime: CommandRuntime;
  stateManager?: StateManager;
  meteringDir?: string;
  /** When set, command replies target the physical channel bot instead of the cross-agent active route. */
  inboundChannel?: InboundChannelHint;
  /** Test seam: override metering aggregation. Production stays on `summarizeMonthlyUsage`. */
  summarizeUsage?: (meteringDir: string, tenant: string, now?: Date) => UsageSummary;
  /** Model catalog for provider model routing (P0-003) */
  modelCatalog?: import('./model-catalog.js').ModelCatalog;
  /** Web Terminal manager — when set, /cc returns a browser terminal URL instead of SSH command */
  terminalManager?: import('./web-terminal.js').TerminalManager;
}

export interface CommandResult {
  text: string;
  /** Telegram inline keyboard (one row per inner array). */
  inlineKeyboard?: { text: string; callbackData: string }[][];
  /** Appended to Feishu interactive card `body.elements` after main markdown. */
  feishuCardElements?: Record<string, unknown>[];
}

function configuredChannelSession(agent: AgentConfig, channel?: InboundChannelHint): string | undefined {
  return channel === "feishu" || (!channel && !agent.botToken)
    ? agent.feishu?.sessionId ?? agent.sessionId : agent.sessionId;
}

function completePiRoute(route: Pick<import('./types.js').ConversationRuntimeRoute, 'model' | 'isolationProfile'> | undefined): boolean {
  return Boolean(route?.model?.provider && route.model.model && route.model.reasoning && route.isolationProfile);
}

function nodeCanUsePi(deps: Pick<CommandDeps, 'router'>, agent: AgentConfig): boolean {
  return completePiRoute(agent.conversationRuntime) || completePiRoute(deps.router.getConversationRuntimeDefault?.('pi'));
}

export type ModelAccessMode = 'subscription' | 'openrouter';

const MODEL_SCOPE_CARD_LIMIT = 8;
const MODEL_TEXT_LIST_LIMIT = 30;

// Apply the Relay menu policy to both catalog IDs and provider-qualified native
// IDs, including OpenRouter's nested model names.
function isRetiredModelOption(model: string): boolean {
  // The current native Haiku alias still resolves to Claude 4.5.
  return /^haiku$/i.test(model)
    || /(?:^|\/)(?:gpt-5(?=[.:-]|$)|claude-(?:[a-z]+-)?4(?=[.:-]|$))/i.test(model);
}

const RUNTIME_LABELS: Record<ConversationRuntimeId, string> = {
  cc: '🟦 CC',
  codex: '🟩 Codex',
  'cc-sdk': '🟨 CC SDK',
  pi: '🟪 Pi',
  agy: '🟫 agy',
};

const SUBSCRIPTION_PROVIDER_IDS = new Set([
  'anthropic',
  'claude',
  'openai-chatgpt',
  'openai-codex',
  'github-copilot',
]);

function modelAccessLabel(access: ModelAccessMode): string {
  return access === 'subscription' ? '订阅' : 'OpenRouter API';
}

function effectiveCommandRuntime(
  agent: AgentConfig,
  proc: RuntimeProcess | undefined,
  persistedRuntime?: ConversationRuntimeId,
): ConversationRuntimeId {
  const configured = agent.conversationRuntime?.runtime
    ?? agent.conversationRuntimePreference?.[0]
    ?? agent.runtime
    ?? 'cc';
  return proc ? processRuntime(proc, configured) : persistedRuntime ?? configured;
}

function catalogModelAccess(
  catalog: NonNullable<CommandDeps['modelCatalog']>,
  providerName: string | undefined,
): ModelAccessMode | undefined {
  if (!providerName) return 'subscription';
  if (providerName.toLowerCase() === 'openrouter') return 'openrouter';
  const provider = typeof catalog.findProvider === 'function' ? catalog.findProvider(providerName) : undefined;
  if (provider?.api_key_pool.length === 0 || SUBSCRIPTION_PROVIDER_IDS.has(providerName.toLowerCase())) {
    return 'subscription';
  }
  return undefined;
}

function catalogModelsForScope(
  catalog: NonNullable<CommandDeps['modelCatalog']>,
  runtime: ConversationRuntimeId,
  access: ModelAccessMode,
): Array<{ provider?: string; model: string; displayName: string }> {
  if (runtime === 'pi') return [];
  if (runtime === 'agy') return access === 'subscription' ? agyModelOptions() : [];
  if (runtime === 'cc-sdk' && access === 'openrouter') return [];
  const entries = Array.isArray(catalog.entries)
    ? catalog.entries
    : catalog.listIds().map((entry) => ({ id: entry.id, display_name: entry.display_name, provider: undefined }));
  return entries.flatMap((entry) => {
    if (isRetiredModelOption(entry.id)) return [];
    const provider = entry.provider && typeof catalog.findProvider === 'function'
      ? catalog.findProvider(entry.provider)
      : undefined;
    const entryAccess = catalogModelAccess(catalog, entry.provider);
    if (entryAccess !== access) return [];
    const supported = runtime === 'cc-sdk'
      ? !entry.provider
      : runtime === 'codex'
        ? provider?.protocol === 'openai'
        : !entry.provider || provider?.protocol === 'anthropic';
    if (!supported) return [];
    return [{ provider: entry.provider, model: entry.id, displayName: entry.display_name }];
  });
}

function nativeModelsForScope(
  models: import('./conversation-runtime.js').ConversationRuntimeModelOption[],
  access: ModelAccessMode,
  catalog?: CommandDeps['modelCatalog'],
): import('./conversation-runtime.js').ConversationRuntimeModelOption[] {
  const filtered = models.filter((model) => {
    if (isRetiredModelOption(model.model)) return false;
    const provider = model.provider.toLowerCase();
    return access === 'openrouter'
      ? provider === 'openrouter'
      : SUBSCRIPTION_PROVIDER_IDS.has(provider);
  });
  if (access === 'subscription') {
    // Promote the newest discovered Sol, including minor releases, before the
    // eight-button limit. Availability and switching remain native.
    let sol: (typeof filtered)[number] | undefined;
    let newest: [number, number] = [-1, -1];
    for (const model of filtered) {
      if (model.provider.toLowerCase() !== 'openai-codex') continue;
      const version = /^gpt-(\d+)(?:\.(\d+))?-sol$/.exec(model.model);
      if (!version) continue;
      const major = Number(version[1]);
      const minor = Number(version[2] ?? 0);
      if (major > newest[0] || (major === newest[0] && minor > newest[1])) {
        sol = model;
        newest = [major, minor];
      }
    }
    return sol ? [sol, ...filtered.filter((model) => model !== sol)] : filtered;
  }
  if (!catalog) return filtered;

  // Pi may expose hundreds of OpenRouter models. Prefer Relay's reviewed catalog
  // rather than presenting an arbitrary prefix of the provider's full catalog.
  const reviewed = new Set(
    (Array.isArray(catalog.entries) ? catalog.entries : [])
      .filter((entry) => entry.provider?.toLowerCase() === 'openrouter')
      .map((entry) => entry.id),
  );
  const curated = filtered.filter((model) => reviewed.has(model.model));
  return curated.length > 0 ? curated : filtered;
}

/** Callback data prefix for Telegram agent switch buttons (keep total ≤ 64 bytes). */
export const RELAY_SWITCH_CB_PREFIX = 'rs:';

export function resolveInboundRoutingAgent(
  router: Router,
  userId: string,
  physicalAgent: AgentConfig,
  options?: { inboundChannel?: InboundChannelHint },
): AgentConfig {
  const agents = router.getAgents(userId);
  const registered = agents.find(agent => agent.id === physicalAgent.id);
  const hasChannelRoute = registered?.feishu && ['runtime', 'conversationRuntimePreference', 'conversationRuntime', 'ccModel']
    .some(key => Object.hasOwn(registered.feishu!, key));
  const currentAgent = options?.inboundChannel === 'feishu' && hasChannelRoute ? registered! : physicalAgent;
  const channelAgent = options?.inboundChannel === 'feishu'
    ? channelConversationAgent(currentAgent, 'feishu') : currentAgent;
  if (agents.length <= 1) return channelAgent;
  // Dedicated channel bots already identify the target agent; do not remap them to the active/default agent.
  if (options?.inboundChannel === 'feishu' || options?.inboundChannel === 'telegram') return channelAgent;
  const active = router.getActiveAgent(userId);
  if (active && agents.some(a => a.id === active.id)) return active;
  return physicalAgent;
}

export function resolveAgentFromPoolKey(router: Router, userId: string, poolKey: string): AgentConfig | undefined {
  // Known configuration supplies identity; legacy key bytes cannot distinguish
  // an Agent separator from a channel suffix on their own.
  const matches = router.getAgents(userId).filter(agent =>
    poolKey === formatConversationPoolKey({ userId, agentId: agent.id })
    || poolKey === formatConversationPoolKey({ userId, agentId: agent.id, channelScope: 'feishu' }));
  return matches.length === 1 ? matches[0] : undefined;
}

export function withAgentPickerUi(
  base: CommandResult,
  userId: string,
  router: Router,
  physicalAgent: AgentConfig,
): CommandResult {
  const agents = router.getAgents(userId);
  if (agents.length <= 1) return base;

  const active = router.getActiveAgent(userId);
  const inlineKeyboard: { text: string; callbackData: string }[][] = [];
  const feishuCardElements: Record<string, unknown>[] = [
    { tag: 'hr' },
    { tag: 'markdown', content: '<text_tag color="blue">切换 Agent</text_tag>（亦可发送 /switch `<id>`）' },
  ];

  for (const a of agents) {
    const mark = active?.id === a.id ? ' ✓' : '';
    const label = `${a.emoji} ${a.name}${mark}`;
    const cb = `${RELAY_SWITCH_CB_PREFIX}${a.id}`;
    if (cb.length <= 64) {
      inlineKeyboard.push([{ text: label.slice(0, 60), callbackData: cb }]);
    }
    feishuCardElements.push({
      tag: 'button',
      text: { tag: 'plain_text', content: label.slice(0, 60) },
      type: active?.id === a.id ? 'primary' : 'default',
      width: 'default',
      behaviors: [{ type: 'callback', value: { relay_switch: a.id } }],
    });
  }

  return {
    ...base,
    inlineKeyboard,
    feishuCardElements,
  };
}

// --- Feishu command menu card (feishu-command-menu feature) ---

/** Build a menu button element. `arg` undefined → first-level; set → second-level option. */
function menuButton(
  label: string,
  action: string,
  type: 'default' | 'primary' | 'danger',
  arg?: string,
): Record<string, unknown> {
  const value: Record<string, unknown> = { relay_menu: action };
  if (arg !== undefined) value.relay_menu_arg = arg;
  return {
    tag: 'button',
    text: { tag: 'plain_text', content: label },
    type,
    width: 'default',
    behaviors: [{ type: 'callback', value }],
  };
}

/** Lay out buttons horizontally using a Schema 2.0 column_set.
 *  Uses weight-based sizing so buttons share space proportionally
 *  instead of auto-sizing (which truncates to "..." on narrow screens). */
function buttonRow(buttons: Record<string, unknown>[]): Record<string, unknown> {
  const weight = Math.floor(1 / buttons.length * 100);
  return {
    tag: 'column_set',
    columns: buttons.map(b => ({ tag: 'column', weight, elements: [b] })),
  };
}

/** Build a titled section: hr + markdown heading + button row. */
function menuSection(title: string, buttons: Record<string, unknown>[]): Record<string, unknown>[] {
  return [
    { tag: 'hr' },
    { tag: 'markdown', content: title },
    buttonRow(buttons),
  ];
}

/**
 * Build the Feishu `/menu` main menu card body (no header — caller wraps via buildMarkdownCard).
 * Four groups: 会话 / 进程 / 查看 / 配置. Buttons are first-level (no relay_menu_arg).
 * `reset` uses danger type to distinguish from `new`.
 */
export function buildMenuCard(agent: AgentConfig): CommandResult {
  const elements: Record<string, unknown>[] = [];
  elements.push(
    ...menuSection('🗂 会话', [
      menuButton('✨ 新对话', 'new', 'default'),
      menuButton('⚠️ 强制重置', 'reset', 'danger'),
    ]),
  );
  elements.push(
    ...menuSection('⚡ 进程', [
      menuButton('⏹ 中断', 'stop', 'default'),
      menuButton('⛔ 终止', 'kill', 'default'),
      menuButton('🔄 重启', 'restart', 'default'),
    ]),
  );
  elements.push(
    ...menuSection('🔍 查看', [
      menuButton('📊 状态', 'status', 'default'),
      menuButton('💬 Session', 'session', 'default'),
      menuButton('💰 用量', 'usage', 'default'),
    ]),
  );
  elements.push(
    ...menuSection('⚙️ 配置', [
      menuButton('🖥 Runtime', 'runtime', 'default'),
      menuButton('🤖 模型', 'model', 'default'),
      menuButton('🧠 推理', 'think', 'default'),
      menuButton('⚡ YOLO', 'yolo', 'default'),
    ]),
  );
  return {
    text: `点击下方按钮操作，或直接发送 /命令。当前 Agent：${agent.emoji} ${agent.name}`,
    feishuCardElements: elements,
  };
}

/**
 * agy's model buttons, one per model family: a family is picked at the current level when it has
 * that variant (so /model keeps the /think level), else at its own default. agy has one access
 * scope, so its models sit on the /model card itself.
 */
function agyModelButtons(current: string): Record<string, unknown>[] {
  const { family: currentFamily, level } = agyModelFamily(current);
  const buttons = agyModelFamilies().map((family) => {
    const first = family.models[0]!;
    const target = (level && agyModelForLevel(first, level)) || agyModelForLevel(first, 'high') || first;
    const selected = family.family === currentFamily;
    return menuButton(selected ? `✓ ${family.displayName}` : family.displayName, 'model', selected ? 'primary' : 'default', target);
  });
  const rows: Record<string, unknown>[] = [];
  for (let i = 0; i < buttons.length; i += 2) rows.push(buttonRow(buttons.slice(i, i + 2)));
  return rows;
}

/** Build the Relay-wide model control surface.
 *
 * Runtime selection and model access are intentionally separate steps. A model
 * click must never silently discard a session by crossing runtimes. The active
 * runtime exposes its subscription/OpenRouter scopes; other runtimes expose an
 * explicit runtime-switch button first.
 */
export function buildModelOptionCard(
  agent: AgentConfig,
  _userId: string,
  deps: CommandDeps,
  currentModel?: string,
  currentRuntime?: ConversationRuntimeId,
): CommandResult {
  const catalog = deps.modelCatalog;
  const activeRuntime = currentRuntime
    ?? agent.conversationRuntime?.runtime
    ?? agent.conversationRuntimePreference?.[0]
    ?? agent.runtime
    ?? 'cc';
  const structuredModel = activeRuntime === 'pi' ? agent.conversationRuntime?.model : undefined;
  const currentLabel = structuredModel
    ? `${structuredModel.provider}/${structuredModel.model} · thinking=${structuredModel.reasoning}`
    : activeRuntime === 'agy'
      ? isAgyModel(currentModel) ? currentModel : `${DEFAULT_AGY_MODEL}（默认）`
    : currentModel === 'auto'
      ? 'auto（按可用性自动选择）'
      : currentModel ?? 'default（运行时自主选择）';
  const elements: Record<string, unknown>[] = [];

  const canUseAgy = !agyIsolationRefusal(agent.linuxUser) && (activeRuntime === 'agy' || isAgyInstalled());
  const runtimes: ConversationRuntimeId[] = ['cc', 'codex', 'cc-sdk', 'pi', ...(canUseAgy ? ['agy' as const] : [])];
  for (const runtime of runtimes) {
    const isCurrent = runtime === activeRuntime;
    const canUsePi = nodeCanUsePi(deps, agent);
    const buttons: Record<string, unknown>[] = [];
    if (!isCurrent) {
      if (runtime !== 'pi' || canUsePi) {
        buttons.push(menuButton(`切换到 ${runtime}`, 'runtime', 'default', runtime));
      }
    } else {
      const subscriptionAvailable = runtime === 'pi'
        ? canUsePi
        : runtime === 'agy'
          ? false
          : runtime === 'cc-sdk'
            ? true
          : !catalog || catalogModelsForScope(catalog, runtime, 'subscription').length > 0;
      const openRouterAvailable = runtime === 'pi'
        ? canUsePi
        : runtime === 'agy'
          ? false
          : Boolean(catalog && catalogModelsForScope(catalog, runtime, 'openrouter').length > 0);
      if (subscriptionAvailable) {
        buttons.push(menuButton('订阅模型', 'model_scope', 'primary', `${runtime}|subscription`));
      }
      if (openRouterAvailable) {
        buttons.push(menuButton('OpenRouter API', 'model_scope', 'default', `${runtime}|openrouter`));
      }
    }
    const accessSummary = runtime === 'cc-sdk'
      ? '订阅 ✓ · OpenRouter API 尚未接入'
      : runtime === 'agy'
        ? isCurrent ? '下面按模型族选择；思考档位用 /think 调。' : '订阅 ✓（agy 账号额度）· OpenRouter API 不适用'
      : runtime === 'pi'
        ? canUsePi ? '订阅 / OpenRouter API（进入后由 Pi 原生探测）' : '节点未配置 Pi runtime'
        : catalog
          ? [
              catalogModelsForScope(catalog, runtime, 'subscription').length > 0 ? '订阅 ✓' : '订阅未配置',
              catalogModelsForScope(catalog, runtime, 'openrouter').length > 0 ? 'OpenRouter API ✓' : 'OpenRouter API 未配置',
            ].join(' · ')
          : '模型目录未就绪';
    elements.push(
      { tag: 'hr' },
      { tag: 'markdown', content: `${RUNTIME_LABELS[runtime]}${isCurrent ? ' <text_tag color="green">当前</text_tag>' : ''}\n<font color='grey'>${accessSummary}</font>` },
    );
    if (buttons.length > 0) elements.push(buttonRow(buttons));
    if (isCurrent && runtime === 'agy') elements.push(...agyModelButtons(isAgyModel(currentModel) ? currentModel : DEFAULT_AGY_MODEL));
  }
  return {
    text: [
      `${agent.emoji} ${agent.name} 模型与运行时`,
      `当前 runtime: ${activeRuntime}`,
      `当前模型: ${currentLabel}`,
      '模型选择按 runtime → 接入方式 → model 分层；切换 runtime 会清理当前 active session。',
    ].join('\n'),
    feishuCardElements: elements,
  };
}

/** Build runtime second-level option card from the agent's allowed runtime capabilities. */
export function buildRuntimeOptionCard(agent: AgentConfig, deps?: Pick<CommandDeps, 'router'>): CommandResult {
  const current = agent.conversationRuntime?.runtime
    ?? agent.conversationRuntimePreference?.[0]
    ?? agent.runtime
    ?? 'cc';
  const buttons = [
    menuButton('🟦 CC', 'runtime', current === 'cc' ? 'primary' : 'default', 'cc'),
    menuButton('🟩 Codex', 'runtime', current === 'codex' ? 'primary' : 'default', 'codex'),
    menuButton('🟨 CC SDK', 'runtime', current === 'cc-sdk' ? 'primary' : 'default', 'cc-sdk'),
  ];
  if (completePiRoute(agent.conversationRuntime) || (deps && nodeCanUsePi(deps, agent))) {
    buttons.push(menuButton('🟪 Pi', 'runtime', current === 'pi' ? 'primary' : 'default', 'pi'));
  }
  if (!agyIsolationRefusal(agent.linuxUser) && (current === 'agy' || isAgyInstalled())) {
    buttons.push(menuButton('🟫 agy', 'runtime', current === 'agy' ? 'primary' : 'default', 'agy'));
  }
  const elements: Record<string, unknown>[] = [];
  for (let i = 0; i < buttons.length; i += 2) elements.push(buttonRow(buttons.slice(i, i + 2)));
  return {
    text: `${agent.emoji} ${agent.name} Runtime 选择\n当前: ${current}`,
    feishuCardElements: elements,
  };
}

function buildScopedModelOptionCard(
  agent: AgentConfig,
  runtime: ConversationRuntimeId,
  access: ModelAccessMode,
  currentModel: string,
  models: import('./conversation-runtime.js').ConversationRuntimeModelOption[],
): CommandResult {
  const visible = models.slice(0, MODEL_SCOPE_CARD_LIMIT);
  const buttons = visible.map((model) => {
    const qualified = `${model.provider}/${model.model}`;
    const selected = model.model === currentModel || qualified === currentModel;
    return menuButton(
      selected ? `✓ ${model.displayName}` : model.displayName,
      'model',
      selected ? 'primary' : 'default',
      runtime === 'pi' ? qualified : model.model,
    );
  });
  const elements: Record<string, unknown>[] = [];
  for (let i = 0; i < buttons.length; i += 2) elements.push(buttonRow(buttons.slice(i, i + 2)));
  if (runtime !== 'pi' && access === 'subscription') {
    elements.push(buttonRow([
      menuButton('auto（自动选择）', 'model', 'default', 'auto'),
      menuButton('default（自主选择）', 'model', 'default', 'default'),
    ]));
  }
  const availability = models.length > 0
    ? `可用模型: ${models.length}${models.length > visible.length ? `；卡片仅展示前 ${visible.length} 个，请用 /models <关键词> 检索` : ''}`
    : '当前 scope 没有可用模型。';
  return {
    text: [
      `${agent.emoji} ${agent.name} ${RUNTIME_LABELS[runtime]} · ${modelAccessLabel(access)}`,
      `当前: ${currentModel}`,
      availability,
    ].join('\n'),
    feishuCardElements: elements.length > 0 ? elements : undefined,
  };
}

export async function handleModelScope(
  userId: string,
  agentId: string,
  requestedScope: string,
  deps: CommandDeps,
): Promise<CommandResult> {
  const agent = deps.router.getAgents(userId).find((candidate) => candidate.id === agentId)
    ?? deps.router.getActiveAgent(userId);
  if (!agent) return { text: '无活跃 Agent。' };

  const [runtimeRaw, accessRaw] = requestedScope.split('|');
  const runtime = parseConversationRuntime(runtimeRaw?.trim().toLowerCase() ?? '');
  const access = accessRaw === 'subscription' || accessRaw === 'openrouter' ? accessRaw : undefined;
  if (!runtime || !access) return { text: '无效的模型分组，请重新发送 /model。' };

  const poolKey = deps.router.poolKey(userId, agent.id);
  let proc = deps.runtime.get(poolKey);
  const currentRuntime = effectiveCommandRuntime(agent, proc, deps.stateManager?.getSession(poolKey)?.runtime);
  if (runtime !== currentRuntime) {
    return { text: `卡片已过期：当前 runtime=${currentRuntime}。请重新发送 /model，或先执行 /runtime ${runtime}。` };
  }

  let models: import('./conversation-runtime.js').ConversationRuntimeModelOption[] = [];
  let currentModel = deps.router.getAgentModel(userId, agent.id) ?? 'default';
  if (runtime === 'pi') {
    if (!agent.conversationRuntime?.model) return { text: '当前 Pi runtime 缺少 structured model route。' };
    if (!proc || proc.state === 'dead') {
      try {
        proc = await ensurePiCommandProcess(userId, agent, deps);
      } catch (error) {
        return { text: `Pi native runtime 启动失败：${error instanceof Error ? error.message : String(error)}` };
      }
    }
    try {
      const native = deps.runtime.listModels ? await deps.runtime.listModels(poolKey) : [];
      models = nativeModelsForScope(native, access, deps.modelCatalog);
    } catch (error) {
      return { text: `读取 Pi 原生模型失败：${error instanceof Error ? error.message : String(error)}` };
    }
    currentModel = `${agent.conversationRuntime.model.provider}/${agent.conversationRuntime.model.model}`;
  } else if (runtime === 'agy') {
    // An older card's scope button: agy models now sit on the /model card itself.
    return buildModelOptionCard(agent, userId, deps,
      agyEffectiveModel(currentModel, deps.stateManager?.getSession(poolKey)?.reasoningLevel), 'agy');
  } else if (deps.modelCatalog) {
    models = catalogModelsForScope(deps.modelCatalog, runtime, access).map((model) => ({
      provider: model.provider ?? 'subscription',
      model: model.model,
      displayName: model.displayName,
    }));
  }

  return buildScopedModelOptionCard(agent, runtime, access, currentModel, models);
}

function buildThinkingOptionCard(
  agent: AgentConfig,
  heading: string,
  current: string,
  levels: import('./types.js').ConversationReasoningLevel[],
  note: string,
): CommandResult {
  const buttons = levels.map((level) => menuButton(
    level === current ? `✓ ${level}` : level,
    'think',
    level === current ? 'primary' : 'default',
    level,
  ));
  const elements: Record<string, unknown>[] = [];
  for (let i = 0; i < buttons.length; i += 2) elements.push(buttonRow(buttons.slice(i, i + 2)));
  return {
    text: [`${agent.emoji} ${agent.name} ${heading}`, `当前: ${current}`, note].join('\n'),
    feishuCardElements: elements.length > 0 ? elements : undefined,
  };
}

/** Build yolo second-level option card: on / off / status. */
export function buildYoloOptionCard(agent: AgentConfig): CommandResult {
  return {
    text: `${agent.emoji} ${agent.name} YOLO 模式`,
    feishuCardElements: [
      buttonRow([
        menuButton('✅ 开启', 'yolo', 'primary', 'on'),
        menuButton('⛔ 关闭', 'yolo', 'default', 'off'),
        menuButton('📊 状态', 'yolo', 'default', 'status'),
      ]),
      { tag: 'hr' },
      { tag: 'markdown', content: "<font color='grey'>YOLO 仅对 Codex runtime 生效；非 Codex 时 on/off 会被拒绝。</font>" },
    ],
  };
}

function processRuntime(proc: RuntimeProcess | undefined, fallback: ConversationRuntimeId): ConversationRuntimeId {
  return proc && 'runtime' in proc ? proc.runtime : fallback;
}

async function ensurePiCommandProcess(
  userId: string,
  agent: AgentConfig,
  deps: CommandDeps,
): Promise<RuntimeProcess> {
  const poolKey = deps.router.poolKey(userId, agent.id);
  const existing = deps.runtime.get(poolKey);
  const spawned = await acquireConversation({
    poolKey, agent, runtime: deps.runtime, stateManager: deps.stateManager,
    channel: deps.inboundChannel ?? 'telegram', intent: 'pi-command',
  });
  if (spawned === existing) return spawned;

  deps.stateManager?.updateSession(poolKey, {
    poolKey,
    agentId: agent.id,
    runtime: 'pi',
    sessionId: spawned.sessionId,
    lastActivity: Date.now(),
    status: 'active',
  });
  deps.stateManager?.flush();
  return spawned;
}

function processCodexThreadId(proc: RuntimeProcess | undefined): string | undefined {
  return proc && 'codexThreadId' in proc ? proc.codexThreadId : undefined;
}

function formatLastActivity(ts: number | undefined): string {
  if (!ts) return 'N/A';
  return new Date(ts).toISOString();
}

function parseConversationRuntime(input: string): ConversationRuntimeId | undefined {
  return input === 'cc' || input === 'codex' || input === 'cc-sdk' || input === 'pi' || input === 'agy' ? input : undefined;
}

// CC-native short aliases — passed as-is so CC resolves them to "latest of that family".
// Full claude-* IDs (with or without date suffix) are also accepted and passed through.
const CC_NATIVE_ALIASES = new Set(['sonnet', 'opus', 'haiku']);

function parseModelInput(input: string): string | null {
  const lower = input.trim().toLowerCase();
  if (CC_NATIVE_ALIASES.has(lower)) return lower;
  if (input.startsWith('claude-')) return input.trim();
  return null;
}

export function handleStart(userId: string, agentId: string, deps: CommandDeps): CommandResult {
  const { router } = deps;
  const physical = agentId
    ? router.getAgents(userId).find(a => a.id === agentId)
    : router.getActiveAgent(userId);

  if (!physical) {
    return { text: '无可用 Agent。' };
  }

  const routingOpts = deps.inboundChannel ? { inboundChannel: deps.inboundChannel } : undefined;
  const agent = resolveInboundRoutingAgent(router, userId, physical, routingOpts);
  const lines = [
    `${agent.emoji} ${agent.name} 已就绪。直接发消息开始对话。`,
    '',
  ];
  if (router.getAgents(userId).length > 1) {
    lines.push('/list — 查看可用 Agent', '/switch `<id>` — 切换对话 Agent', '');
  }
  lines.push(
    '/status — 查看进程状态',
    '/session — 查看 session 信息',
    '/runtime — 查看/切换 conversation runtime',
    '/model — 查看/切换模型 (sonnet/opus/haiku)',
    '/yolo — 查看/切换 Codex YOLO 模式',
    '/new — 开启新对话',
    '/reset — 无条件释放本地 session 并开启新对话（WI 权限不变）',
    '/stop — 中断当前工作',
    '/kill — 终止进程',
    '/restart — 重启进程',
    '/usage — 查看费用统计',
    '/help — 命令帮助',
  );

  const base = { text: lines.join('\n') };
  return withAgentPickerUi(base, userId, router, physical);
}

export function handleList(userId: string, deps: CommandDeps): CommandResult {
  const { router, runtime } = deps;
  const agents = router.getAgents(userId);
  const active = router.getActiveAgent(userId);

  const lines = ['可用 Agent:\n'];
  for (const agent of agents) {
    const isActive = active && agent.id === active.id;
    const marker = isActive ? '▶ ' : '  ';
    const poolKey = router.poolKey(userId, agent.id);
    const proc = runtime.get(poolKey);
    const state = proc ? `[${proc.state}]` : '[未启动]';
    lines.push(`${marker}${agent.emoji} ${agent.name} ${state}`);
  }
  lines.push('');
  lines.push('▶ = 当前活跃');

  return { text: lines.join('\n') };
}

export function handleSwitch(userId: string, agentId: string, deps: CommandDeps): CommandResult {
  const { router } = deps;

  if (!agentId) {
    return { text: '用法: /switch <agent_id>。发送 /list 查看可用 ID。' };
  }

  const agent = router.switchAgent(userId, agentId);
  if (!agent) {
    return { text: `未知 Agent: ${agentId}。发送 /list 查看可用列表。` };
  }

  return { text: `已切换到 ${agent.emoji} ${agent.name}` };
}

export function handleStatus(userId: string, agentId: string, deps: CommandDeps): CommandResult {
  const { router, runtime } = deps;
  const agent = agentId
    ? router.getAgents(userId).find(a => a.id === agentId)
    : router.getActiveAgent(userId);
  if (!agent) {
    return { text: '无活跃 Agent。' };
  }

  const poolKey = router.poolKey(userId, agent.id);
  const proc = runtime.get(poolKey);
  const persisted = deps.stateManager?.getSession(poolKey);

  if (!proc) {
    const status = persisted ? '待恢复' : '未启动';
    const hint = persisted ? '会话仍保留，发送消息即可恢复。' : '发送消息即可启动。';
    return { text: `当前 Agent: ${agent.emoji} ${agent.name}\n状态: ${status}\n${hint}` };
  }

  const elapsed = Math.round((Date.now() - proc.lastActivity) / 1000);
  const timeAgo = elapsed < 60
    ? `${elapsed} 秒前`
    : `${Math.round(elapsed / 60)} 分钟前`;

  const lines = [
    `当前 Agent: ${agent.emoji} ${agent.name}`,
    `状态: ${proc.state}`,
    `PID: ${proc.process?.pid ?? 'N/A'}`,
    `Session: ${proc.sessionId}`,
    `上次活动: ${timeAgo}`,
    `重启次数: ${proc.restartCount}`,
  ];

  return { text: lines.join('\n') };
}

export function handleHelp(): CommandResult {
  const lines = [
    '可用命令:\n',
    '/start — 欢迎信息',
    '/list — 列出 Agent（多 Agent 时）',
    '/switch `<id>` — 切换活跃 Agent（多 Agent 时）',
    '/status — 查看 Agent 状态',
    '/session — 查看 session 信息',
    '/runtime [cc|codex|cc-sdk|pi|agy] — 查看或切换 conversation runtime（Pi 仅限已配置 canary）',
    '/model [<id>] — 按 runtime / 接入方式查看或切换模型',
    '/models [关键词] — 检索当前 runtime 可用模型（结果有界）',
    '/think [level] — 查看/切换推理档位（cc/cc-sdk effort、codex reasoning effort、agy 模型变体、Pi 原生）',
    '/compact [instructions] — 压缩 context（CC 系透传、Pi 原生）',
    '/context — 查看 context（CC 系透传、Pi 原生）',
    '/yolo [on|off|status] — 查看或切换 Codex YOLO 模式',
    '/new — 开启新对话（活动 WI 必须先 rollover 或 recovery）',
    '/wi rollover <wi_id> — 为同一 WI 执行受控 session rollover',
    '/wi switch <wi_id> — 切到该 WI 的原会话（没有就新开），当前 WI 自动记检查点',
    '/reset — 无条件释放本地 session；保留旧 WI 审计，Harness 权限不变',
    '/stop — 中断当前 runtime 的 active turn（保留 session）',
    '/kill — 终止进程',
    '/restart — 按当前 runtime 重启进程（保留 session binding）',
    '/usage — 查看费用统计',
    '/usage_seal — 幂等封存当前 WI 绑定的 Pi usage',
    '/menu — 打开命令菜单（按钮版）',
    '/help — 显示此帮助',
  ];
  return { text: lines.join('\n') };
}

function blockBoundWorkItemSessionReplacement(
  poolKey: string,
  deps: CommandDeps,
  action: string,
): CommandResult | undefined {
  const persisted = typeof deps.stateManager?.getSession === 'function'
    ? deps.stateManager.getSession(poolKey)
    : undefined;
  const workItemId = persisted?.activeWorkItemRuntimeBinding?.workItemId;
  if (!workItemId) return undefined;
  return {
    text: [
      `拒绝${action}：当前 session 承载活动 WI ${workItemId}。`,
      `正常切换请使用 /wi rollover ${workItemId}；源 session 已失效时必须走 Harness recovery。`,
    ].join('\n'),
  };
}

export async function handleRuntime(
  userId: string,
  agentId: string,
  requestedRuntime: string | undefined,
  deps: CommandDeps,
): Promise<CommandResult> {
  const { router, runtime } = deps;
  const agent = agentId
    ? router.getAgents(userId).find(a => a.id === agentId)
    : router.getActiveAgent(userId);

  if (!agent) {
    return { text: '无活跃 Agent。' };
  }

  const poolKey = router.poolKey(userId, agent.id);
  const proc = runtime.get(poolKey);
  const persisted = deps.stateManager?.getSession(poolKey);
  const configuredRuntime = agent.conversationRuntime?.runtime
    ?? agent.conversationRuntimePreference?.[0]
    ?? agent.runtime
    ?? 'cc';
  const activeRuntime = proc ? processRuntime(proc, configuredRuntime) : persisted?.runtime;
  const activeState = proc?.state ?? persisted?.status;

  if (!requestedRuntime?.trim()) {
    return buildRuntimeOptionCard(agent, deps);
  }

  const nextRuntime = parseConversationRuntime(requestedRuntime.trim().toLowerCase());
  if (!nextRuntime) {
    return { text: '不支持的 runtime。可用值: cc, codex, cc-sdk, pi, agy。' };
  }
  const agyRefused = nextRuntime === 'agy' ? agyIsolationRefusal(agent.linuxUser) : undefined;
  if (agyRefused) return { text: agyRefused };
  if (nextRuntime === 'agy' && !isAgyInstalled()) {
    return { text: '当前节点未安装 agy 启动脚本，不能切到 agy。' };
  }
  if (
    nextRuntime === 'pi'
    && !nodeCanUsePi(deps, agent)
  ) {
    return { text: '当前 TCloud 节点尚未配置完整的 Pi provider/model/reasoning/isolation capability。' };
  }
  if (configuredRuntime === nextRuntime && (!activeRuntime || activeRuntime === nextRuntime)) {
    return { text: `${agent.emoji} ${agent.name} 当前已经使用 ${nextRuntime}（状态: ${activeState ?? '未启动'}）。` };
  }
  const replacementBlocked = blockBoundWorkItemSessionReplacement(poolKey, deps, '切换 runtime');
  if (replacementBlocked) return replacementBlocked;

  const updatedAgent = router.setAgentRuntime(userId, agent.id, nextRuntime);
  if (!updatedAgent) {
    return { text: `未知 Agent: ${agent.id}` };
  }
  // An agy model id means nothing to the other runtimes; leaving agy drops it.
  const droppedAgyModel = nextRuntime !== 'agy' && isAgyModel(router.getAgentModel(userId, agent.id));
  if (droppedAgyModel) router.setAgentModel(userId, agent.id, undefined);

  if (proc) {
    await runtime.kill(poolKey);
  }

  if (runtime.clearYoloMode) runtime.clearYoloMode(poolKey);
  else runtime.setYoloMode?.(poolKey, false);
  deps.stateManager?.removeSession(poolKey);
  deps.stateManager?.flush();

  return {
    text: [
      `${updatedAgent.emoji} ${updatedAgent.name} runtime 已切换为 ${nextRuntime}。`,
      `conversationRuntimePreference 已同步为 [${nextRuntime}]，避免旧 fallback 顺序覆盖本次切换。`,
      '已终止/清理当前 active conversation session。',
      ...(droppedAgyModel ? ['agy 专用模型已清除，新 runtime 使用默认模型。'] : []),
      '下一条消息会使用新的 runtime。',
    ].join('\n'),
  };
}

export async function handleModel(
  userId: string,
  agentId: string,
  requestedModel: string | undefined,
  deps: CommandDeps,
): Promise<CommandResult> {
  const { router, runtime, modelCatalog } = deps;
  const agent = agentId
    ? router.getAgents(userId).find(a => a.id === agentId)
    : router.getActiveAgent(userId);

  if (!agent) {
    return { text: '无活跃 Agent。' };
  }

  if (requestedModel && isRetiredModelOption(requestedModel.trim())) {
    return { text: `该模型已从 Relay 模型选项移除：${requestedModel.trim()}。请重新发送 /model 选择当前版本。` };
  }

  const poolKey = router.poolKey(userId, agent.id);
  const effectiveModel = router.getAgentModel(userId, agent.id);
  let proc = runtime.get(poolKey);
  const persisted = deps.stateManager?.getSession(poolKey);
  const effectiveRuntime = proc
    ? effectiveCommandRuntime(agent, proc, persisted?.runtime)
    : effectiveCommandRuntime(agent, undefined, persisted?.runtime);

  if (effectiveRuntime === 'pi') {
    const route = agent.conversationRuntime;
    if (!route?.model) {
      return { text: '当前 Pi runtime 缺少 structured provider/model/reasoning 配置。' };
    }
    if (!requestedModel?.trim()) {
      return buildModelOptionCard(agent, userId, deps, undefined, effectiveRuntime);
    }
    if (!proc || proc.state === 'dead') {
      try {
        proc = await ensurePiCommandProcess(userId, agent, deps);
      } catch (error) {
        return { text: `Pi native runtime 启动失败：${error instanceof Error ? error.message : String(error)}` };
      }
    }
    const previousModel = { ...route.model };
    const loadNativeModels = async () => {
      if (!proc || !runtime.listModels) return [];
      try {
        return await runtime.listModels(poolKey);
      } catch {
        return [];
      }
    };
    const requested = requestedModel.trim();
    if (requested === 'default' || requested === 'auto') {
      return { text: 'Pi canary 要求显式 provider/model，不能使用 default 或 auto。' };
    }
    if (proc?.state === 'busy') {
      return { text: `${agent.emoji} ${agent.name} 正在工作；请先 /stop，待 turn settled 后再切换 Pi model。` };
    }
    const nativeModels = await loadNativeModels();
    const requestedNative = nativeModels.find((model) => (
      `${model.provider}/${model.model}` === requested
      || (model.model === requested && nativeModels.filter((candidate) => candidate.model === requested).length === 1)
    ));
    if (proc && nativeModels.length > 0 && !requestedNative) {
      return { text: `Pi 原生 runtime 当前不可用该模型：${requested}。请从 /model 卡片选择。` };
    }
    const entry = modelCatalog?.findById(requested);
    const explicitRoute = !requestedNative && !entry && requested.includes('/')
      ? { provider: requested.slice(0, requested.indexOf('/')), model: requested.slice(requested.indexOf('/') + 1) }
      : undefined;
    if (explicitRoute && (!explicitRoute.provider || !explicitRoute.model)) {
      return { text: 'Pi model 用法: /model provider/model-id（model-id 可继续包含 /）。' };
    }
    const nextModel = {
      provider: requestedNative?.provider ?? entry?.provider ?? explicitRoute?.provider ?? route.model.provider,
      model: requestedNative?.model ?? entry?.id ?? explicitRoute?.model ?? requested,
      reasoning: route.model.reasoning,
    };
    try {
      if (proc) {
        if (!runtime.setModel) return { text: '当前 runtime manager 不支持 Pi native model switching。' };
        await runtime.setModel(poolKey, nextModel);
      }
    } catch (error) {
      return { text: `Pi model 切换失败，原配置保持不变：${error instanceof Error ? error.message : String(error)}` };
    }
    let updated: AgentConfig | undefined;
    try {
      updated = router.setAgentConversationModel(userId, agent.id, nextModel);
      if (!updated) throw new Error('structured route missing');
    } catch (error) {
      route.model = previousModel;
      if (proc && runtime.setModel) {
        try {
          await runtime.setModel(poolKey, previousModel);
        } catch (rollbackError) {
          return {
            text: `Pi model 持久化失败，且 native rollback 失败；请停止继续操作并核对 runtime state：persist=${error instanceof Error ? error.message : String(error)}; rollback=${rollbackError instanceof Error ? rollbackError.message : String(rollbackError)}`,
          };
        }
      }
      return { text: `Pi model 持久化失败，native 配置已回滚：${error instanceof Error ? error.message : String(error)}` };
    }
    return {
      text: `${updated.emoji} ${updated.name} Pi model → ${nextModel.provider}/${nextModel.model}（reasoning=${nextModel.reasoning}）。${proc ? '当前 native session 已热切换。' : '下一次 spawn 生效。'}`,
    };
  }

  if (effectiveRuntime === 'agy') {
    const agyRefused = agyIsolationRefusal(agent.linuxUser);
    if (agyRefused) return { text: agyRefused };
    if (!requestedModel?.trim()) {
      return buildModelOptionCard(agent, userId, deps, agyEffectiveModel(effectiveModel, persisted?.reasoningLevel), effectiveRuntime);
    }
    const requested = requestedModel.trim();
    const bare = requested.startsWith(`${AGY_MODEL_PROVIDER}/`) ? requested.slice(AGY_MODEL_PROVIDER.length + 1) : requested;
    const reset = bare === 'default' || bare === 'auto';
    if (!reset && !isAgyModel(bare)) {
      return { text: [`agy 不提供该模型：${requested}`, '可用模型:',
        ...agyModelOptions().map((model) => `• ${model.model} (${model.displayName})`)].join('\n') };
    }
    const updatedAgent = router.setAgentModel(userId, agent.id, reset ? undefined : bare);
    if (!updatedAgent) return { text: `未知 Agent: ${agent.id}` };
    const nextModel = reset ? DEFAULT_AGY_MODEL : bare;
    if (proc && proc.state !== 'dead' && runtime.setModel) {
      await runtime.setModel(poolKey, { provider: AGY_MODEL_PROVIDER, model: nextModel, reasoning: 'medium' });
    }
    // The chosen model names its own level; a /think level set before no longer applies.
    if (persisted?.reasoningLevel) {
      deps.stateManager?.updateSession(poolKey, { reasoningLevel: undefined });
      deps.stateManager?.flush();
    }
    return { text: `${updatedAgent.emoji} ${updatedAgent.name} agy model → ${nextModel}${reset ? '（默认）' : ''}。下一轮生效，会话保留、接着同一 conversation。` };
  }

  if (!requestedModel?.trim()) {
    // The Relay-wide model control surface always starts with runtime and access
    // scope. It never flattens an unbounded provider catalog into one card.
    return buildModelOptionCard(agent, userId, deps, effectiveModel, effectiveRuntime);
  }

  // /model default — clear model preference, let CC decide
  if (requestedModel.trim().toLowerCase() === 'default') {
    const updatedAgent = router.setAgentModel(userId, agent.id, undefined);
    if (!updatedAgent) {
      return { text: `未知 Agent: ${agent.id}` };
    }

    const proc = runtime.get(poolKey);
    if (proc) {
      if (proc.state === 'busy') {
        return { text: `${updatedAgent.emoji} ${updatedAgent.name} model 将在当前回合结束后切换为 default。当前进程正在工作中，等它空闲后自动生效。` };
      }
      await runtime.kill(poolKey);
    }

    return { text: `${updatedAgent.emoji} ${updatedAgent.name} model → default（CC 自主选择）。下次 spawn 不传 --model，CC switch 生效。` };
  }

  // /model auto — auto-select model based on provider availability
  if (requestedModel.trim().toLowerCase() === 'auto') {
    const updatedAgent = router.setAgentModel(userId, agent.id, 'auto');
    if (!updatedAgent) {
      return { text: `未知 Agent: ${agent.id}` };
    }

    const proc = runtime.get(poolKey);
    if (proc) {
      if (proc.state === 'busy') {
        return { text: `${updatedAgent.emoji} ${updatedAgent.name} model 将在当前回合结束后切换为 auto（按可用性自动选择）。当前进程正在工作中，等它空闲后自动生效。` };
      }
      await runtime.kill(poolKey);
    }

    return { text: `${updatedAgent.emoji} ${updatedAgent.name} model → auto（按可用性自动选择）。下次 spawn 时自动选择可用模型。` };
  }

  // Validate model ID
  const validation = modelCatalog
    ? modelCatalog.validateModelId(requestedModel.trim())
    : { valid: !!parseModelInput(requestedModel.trim()), resolvedId: requestedModel.trim() };

  if (!validation.valid) {
    if (modelCatalog) {
      const models = modelCatalog.listIds().filter((model) => !isRetiredModelOption(model.id));
      const lines = [
        `不支持的模型: ${requestedModel.trim()}`,
        '',
        '可用模型:',
        ...models.map(m => {
          const runtimeTag = m.runtimeLabel ? ` [${m.runtimeLabel}]` : '';
          const availTag = m.available === false ? ' [不可用]' : '';
          return `• ${m.id} (${m.display_name})${runtimeTag}${availTag}`;
        }),
      ];
      return { text: lines.join('\n') };
    }
    return { text: `不支持的模型: ${requestedModel.trim()}。支持短别名 sonnet/opus 或完整 claude-* ID（如 claude-opus-5-5）。` };
  }

  const modelId = validation.resolvedId;

  // Check if the model's provider requires a different runtime
  let targetRuntime: 'cc' | 'codex' | undefined;
  let runtimeSwitchNeeded = false;
  if (modelCatalog) {
    targetRuntime = modelCatalog.runtimeForModel(modelId);
  }

  const currentRuntime = proc
    ? processRuntime(proc, agent.runtime ?? 'cc')
    : persisted?.runtime ?? agent.runtime ?? 'cc';

  // Determine if runtime switch is needed
  if (targetRuntime && targetRuntime !== currentRuntime) {
    runtimeSwitchNeeded = true;
  }

  if (runtimeSwitchNeeded) {
    const replacementBlocked = blockBoundWorkItemSessionReplacement(poolKey, deps, '跨 runtime 切换 model');
    if (replacementBlocked) return replacementBlocked;
  }

  // Update model (writes to agent-models.json)
  const updatedAgent = router.setAgentModel(userId, agent.id, modelId);
  if (!updatedAgent) {
    return { text: `未知 Agent: ${agent.id}` };
  }

  // Auto-switch runtime if needed
  if (runtimeSwitchNeeded) {
    router.setAgentRuntime(userId, agent.id, targetRuntime!);
    if (proc) {
      await runtime.kill(poolKey);
    }
    if (runtime.clearYoloMode) runtime.clearYoloMode(poolKey);
    else runtime.setYoloMode?.(poolKey, false);
    deps.stateManager?.removeSession(poolKey);
    deps.stateManager?.flush();

    const lines = [
      `${updatedAgent.emoji} ${updatedAgent.name} model → ${modelId}。`,
      `已同时切换到 ${targetRuntime?.toUpperCase()} 运行时（${currentRuntime} → ${targetRuntime}）。`,
      `conversationRuntimePreference 已同步为 [${targetRuntime}]，避免旧 fallback 顺序把 runtime 拽回。`,
    ];
    return { text: lines.join('\n') };
  }

  // Same runtime — hot-switch (kill + --resume)
  const sessionId = proc?.sessionId || persisted?.sessionId || configuredChannelSession(updatedAgent, deps.inboundChannel) || '';
  const canHotSwitch = sessionId.length > 0;

  if (proc) {
    if (proc.state === 'busy') {
      const lines = [
        `${updatedAgent.emoji} ${updatedAgent.name} model 将在当前回合结束后切换为 ${modelId}。`,
        '当前进程正在工作中，等它空闲后自动生效。',
      ];
      return { text: lines.join('\n') };
    }
    await runtime.kill(poolKey);
  }

  const lines = [
    `${updatedAgent.emoji} ${updatedAgent.name} model → ${modelId}。`,
  ];

  if (canHotSwitch) {
    lines.push(proc
      ? '热切换：已重启进程，下次发消息时以新模型 + 原有 session 历史继续。'
      : '下次发消息时以新模型 + 原有 session 历史继续。');
  } else {
    lines.push('⚠️ 此 Agent 无持久 session ID，切换后将从新 session 开始。');
  }

  return { text: lines.join('\n') };
}

export async function handleModels(
  userId: string,
  agentId: string,
  deps: CommandDeps,
  query?: string,
): Promise<CommandResult> {
  const agent = deps.router.getAgents(userId).find((candidate) => candidate.id === agentId)
    ?? deps.router.getActiveAgent(userId);
  if (!agent) return { text: '无活跃 Agent。' };
  const poolKey = deps.router.poolKey(userId, agent.id);
  let proc = deps.runtime.get(poolKey);
  const runtime = effectiveCommandRuntime(agent, proc, deps.stateManager?.getSession(poolKey)?.runtime);
  let models: import('./conversation-runtime.js').ConversationRuntimeModelOption[] = [];

  if (runtime === 'pi') {
    if (!proc || proc.state === 'dead') {
      try {
        proc = await ensurePiCommandProcess(userId, agent, deps);
      } catch (error) {
        return { text: `Pi native runtime 启动失败：${error instanceof Error ? error.message : String(error)}` };
      }
    }
    try {
      models = deps.runtime.listModels ? await deps.runtime.listModels(poolKey) : [];
    } catch (error) {
      return { text: `读取 Pi 原生模型失败：${error instanceof Error ? error.message : String(error)}` };
    }
  } else if (runtime === 'agy') {
    models = agyModelOptions();
  } else if (deps.modelCatalog) {
    const scopes: ModelAccessMode[] = ['subscription', 'openrouter'];
    models = scopes.flatMap((access) => catalogModelsForScope(deps.modelCatalog!, runtime, access).map((model) => ({
      provider: model.provider ?? 'subscription',
      model: model.model,
      displayName: model.displayName,
    })));
  } else {
    return handleModel(userId, agentId, undefined, deps);
  }

  models = models.filter((model) => !isRetiredModelOption(model.model));
  const needle = query?.trim().toLowerCase();
  const filtered = needle
    ? models.filter((model) => `${model.provider}/${model.model} ${model.displayName}`.toLowerCase().includes(needle))
    : models;
  const visible = filtered.slice(0, MODEL_TEXT_LIST_LIMIT);
  const lines = [
    `${agent.emoji} ${agent.name} ${runtime} 可用模型`,
    `匹配: ${filtered.length}${needle ? `（关键词: ${query!.trim()}）` : ''}`,
    ...visible.map((model) => `• ${model.provider}/${model.model} — ${model.displayName}`),
  ];
  if (filtered.length > visible.length) {
    lines.push(`仅显示前 ${visible.length} 个；请使用 /models <关键词> 缩小范围。`);
  }
  if (visible.length === 0) lines.push('没有匹配模型。');
  return { text: lines.join('\n') };
}

export async function handleThink(
  userId: string,
  agentId: string,
  requestedLevel: string | undefined,
  deps: CommandDeps,
): Promise<CommandResult> {
  const { router, runtime } = deps;
  const agent = router.getAgents(userId).find((candidate) => candidate.id === agentId) ?? router.getActiveAgent(userId);
  if (!agent) return { text: '无活跃 Agent。' };
  const poolKey = router.poolKey(userId, agent.id);
  const persisted = deps.stateManager?.getSession(poolKey);
  const effectiveRuntime = effectiveCommandRuntime(agent, runtime.get(poolKey), persisted?.runtime);
  if (effectiveRuntime === 'pi') return handlePiThink(userId, agent, requestedLevel, deps);

  const control = reasoningControl(effectiveRuntime);
  const proc = runtime.get(poolKey);
  const label = `${RUNTIME_LABELS[effectiveRuntime]} ${control.label}`;
  const agyModel = effectiveRuntime === 'agy'
    ? agyEffectiveModel(router.getAgentModel(userId, agent.id), persisted?.reasoningLevel) : undefined;
  const levels = control.levels(agyModel);
  const current = agyModel ? agyModelFamily(agyModel).level ?? '无档位'
    : persisted?.reasoningLevel ?? describeDefaultReasoning(effectiveRuntime as 'cc' | 'cc-sdk' | 'codex', agent, poolKey, persisted, deps);
  const unavailable = agyModel && levels.length === 0
    ? `当前 agy 模型 ${agyModel} 没有可选的思考档位；换档位请先用 /model 换模型。` : undefined;
  if (!requestedLevel?.trim()) {
    return buildThinkingOptionCard(agent, label, current, levels, unavailable ?? (agyModel
      ? `当前模型 ${agyModel}；可选档位即同一模型族的变体，下一轮生效。`
      : `可选: ${levels.join(', ')}；下一轮生效，/new 后回到默认。`));
  }
  if (unavailable) return { text: unavailable };
  const level = parseReasoningLevel(requestedLevel);
  if (!level || !levels.includes(level)) {
    return { text: agyModel
      ? `agy 模型 ${agyModel} 没有 ${requestedLevel.trim()} 档位；可用: ${levels.join(', ')}。`
      : `${effectiveRuntime} 不支持 thinking=${requestedLevel.trim()}；可用: ${levels.join(', ')}。` };
  }
  if (!persisted?.sessionId) return { text: `${agent.emoji} ${agent.name} 还没有会话；先发一条消息，再用 /think 设档位。` };
  const live = proc && proc.state !== 'dead' ? proc : undefined;
  if (live && control.restartToApply && live.state === 'busy') {
    return { text: `${agent.emoji} ${agent.name} 正在工作；请先 /stop，待本轮结束后再切换 ${control.label}。` };
  }
  if (live && !control.restartToApply) {
    if (!runtime.setThinkingLevel) return { text: `当前 runtime manager 不支持切换 ${control.label}。` };
    try {
      await runtime.setThinkingLevel(poolKey, level);
    } catch (error) {
      return { text: `${control.label} 切换失败，原档位保留：${error instanceof Error ? error.message : String(error)}` };
    }
  }
  deps.stateManager?.updateSession(poolKey, { reasoningLevel: level });
  deps.stateManager?.flush();
  // CC reads its effort at start: the idle process restarts and the next turn resumes the same session.
  if (live && control.restartToApply) await runtime.kill(poolKey);
  return { text: `${agent.emoji} ${agent.name} ${label} → ${level}${agyModel ? `（${agyEffectiveModel(agyModel, level)}）` : ''}。`
    + '下一轮生效，会话保留；/new 后回到默认。' };
}

/**
 * The "当前" line when the session has no /think level. Codex gets the effort Relay passes it.
 * cc and cc-sdk choose their own: the line reports the effort the session's transcript recorded
 * for its last turn, found under the config dir the CC process sees; nothing is predicted.
 */
function describeDefaultReasoning(
  runtime: 'cc' | 'cc-sdk' | 'codex',
  agent: AgentConfig,
  poolKey: string,
  persisted: PersistedSession | undefined,
  deps: CommandDeps,
): string {
  if (runtime === 'codex') return describeCodexDefaultReasoning();
  return describeClaudeDefaultReasoning(lastClaudeTurnEffort(claudeTranscriptPaths(runtime, agent, poolKey, persisted, deps)));
}

/** Where the CC process of this session writes its transcript, by the launch's workspace, HOME and env. */
function claudeTranscriptPaths(
  runtime: 'cc' | 'cc-sdk',
  agent: AgentConfig,
  poolKey: string,
  persisted: PersistedSession | undefined,
  deps: CommandDeps,
): string[] {
  let launch: ReturnType<typeof conversationLaunch>;
  try {
    launch = conversationLaunch({ poolKey, agent, session: persisted, channel: deps.inboundChannel ?? 'telegram', intent: 'inbound' });
  } catch {
    return [];
  }
  if (!launch.sessionId) return [];
  let env: NodeJS.ProcessEnv;
  if (runtime === 'cc-sdk') {
    // The SDK engine's CC inherits this env, HOME included.
    env = buildSdkEngineEnv();
  } else {
    const home = ccLaunchHome(launch);
    // A linuxUser tenant runs under sudo, which keeps only the preserved keys.
    env = launch.linuxUser
      ? { ...Object.fromEntries(CC_LINUX_USER_PRESERVED_ENV_KEYS.map((key) => [key, process.env[key]])), HOME: home }
      : { ...process.env, HOME: home };
  }
  const configDir = ccConfigDir(env);
  if (!configDir) return [];
  return [
    getSessionJsonlPath(launch.workspace, launch.sessionId, env['HOME'], configDir),
    getBareSessionJsonlPath(launch.sessionId, env['HOME'], configDir),
  ];
}

async function handlePiThink(
  userId: string,
  agent: AgentConfig,
  requestedLevel: string | undefined,
  deps: CommandDeps,
): Promise<CommandResult> {
  const { router, runtime } = deps;
  const route = agent.conversationRuntime;
  if (!route?.model) return { text: '当前 Pi runtime 缺少 structured provider/model/reasoning 配置。' };
  const poolKey = router.poolKey(userId, agent.id);
  let proc = runtime.get(poolKey);
  if (!proc || proc.state === 'dead') {
    try {
      proc = await ensurePiCommandProcess(userId, agent, deps);
    } catch (error) {
      return { text: `Pi native runtime 启动失败：${error instanceof Error ? error.message : String(error)}` };
    }
  }
  let levels: import('./types.js').ConversationReasoningLevel[] = [];
  if (proc && runtime.listThinkingLevels) {
    try {
      levels = await runtime.listThinkingLevels(poolKey);
    } catch {
      levels = [];
    }
  }
  if (!requestedLevel?.trim()) {
    return buildThinkingOptionCard(agent, 'Pi Thinking', route.model.reasoning, levels,
      levels.length > 0 ? `当前模型原生支持: ${levels.join(', ')}` : 'Pi 尚未启动，原生 thinking level 列表暂不可读。');
  }
  if (proc?.state === 'busy') return { text: `${agent.emoji} ${agent.name} 正在工作；请先 /stop，待 turn settled 后再切换 thinking level。` };

  const normalized = parseReasoningLevel(requestedLevel);
  if (!normalized) return { text: `不支持的 thinking level。可用值: ${REASONING_LEVELS.join(', ')}。` };
  if (proc && levels.length > 0 && !levels.includes(normalized)) {
    return { text: `当前 Pi model 不支持 thinking=${normalized}；原生可用值: ${levels.join(', ')}。` };
  }
  const previousModel = { ...route.model };
  const nextModel = { ...route.model, reasoning: normalized };
  try {
    if (proc) {
      if (!runtime.setThinkingLevel) throw new Error('runtime manager does not support native thinking levels');
      await runtime.setThinkingLevel(poolKey, normalized);
    }
    const updated = router.setAgentConversationModel(userId, agent.id, nextModel);
    if (!updated) throw new Error('structured route missing');
  } catch (error) {
    route.model = previousModel;
    if (proc && runtime.setThinkingLevel) {
      try {
        await runtime.setThinkingLevel(poolKey, previousModel.reasoning);
      } catch (rollbackError) {
        return { text: `Pi thinking 持久化失败且 native rollback 失败：${error instanceof Error ? error.message : String(error)}; rollback=${rollbackError instanceof Error ? rollbackError.message : String(rollbackError)}` };
      }
    }
    return { text: `Pi thinking 切换失败，原配置已保留：${error instanceof Error ? error.message : String(error)}` };
  }
  return { text: `${agent.emoji} ${agent.name} Pi thinking → ${normalized}。${proc ? '当前 native session 已热切换。' : '下一次 spawn 生效。'}` };
}

export async function handleCompact(
  userId: string,
  agentId: string,
  customInstructions: string | undefined,
  deps: CommandDeps,
): Promise<CommandResult> {
  const agent = deps.router.getAgents(userId).find((candidate) => candidate.id === agentId) ?? deps.router.getActiveAgent(userId);
  if (!agent) return { text: '无活跃 Agent。' };
  const poolKey = deps.router.poolKey(userId, agent.id);
  let proc = deps.runtime.get(poolKey);
  const configuredRuntime = effectiveCommandRuntime(agent, proc, deps.stateManager?.getSession(poolKey)?.runtime);
  if ((!proc || proc.state === 'dead') && configuredRuntime === 'pi') {
    try {
      proc = await ensurePiCommandProcess(userId, agent, deps);
    } catch (error) {
      return { text: `Pi native runtime 启动失败：${error instanceof Error ? error.message : String(error)}` };
    }
  }
  if (!proc) return { text: `${agent.emoji} ${agent.name} 当前没有 live runtime context。` };
  if (processRuntime(proc, agent.conversationRuntime?.runtime ?? agent.runtime ?? 'cc') !== 'pi') {
    return { text: '该命令应交给当前非 Pi runtime 的原生命令路径。' };
  }
  if (proc.state === 'busy') return { text: `${agent.emoji} ${agent.name} 正在工作；请先 /stop，待 turn settled 后再 /compact。` };
  if (!deps.runtime.compact) return { text: '当前 runtime manager 不支持 Pi native compact。' };
  try {
    const result = await deps.runtime.compact(poolKey, customInstructions);
    const before = typeof result.tokensBefore === 'number' ? result.tokensBefore : undefined;
    const after = typeof result.estimatedTokensAfter === 'number' ? result.estimatedTokensAfter : undefined;
    return {
      text: [
        `✅ ${agent.emoji} ${agent.name} Pi context 已压缩。`,
        before === undefined ? undefined : `tokensBefore=${before}`,
        after === undefined ? undefined : `estimatedTokensAfter=${after}`,
      ].filter(Boolean).join('\n'),
    };
  } catch (error) {
    return { text: `Pi context 压缩失败：${error instanceof Error ? error.message : String(error)}` };
  }
}

export async function handleContext(userId: string, agentId: string, deps: CommandDeps): Promise<CommandResult> {
  const agent = deps.router.getAgents(userId).find((candidate) => candidate.id === agentId) ?? deps.router.getActiveAgent(userId);
  if (!agent) return { text: '无活跃 Agent。' };
  const poolKey = deps.router.poolKey(userId, agent.id);
  let proc = deps.runtime.get(poolKey);
  const configuredRuntime = effectiveCommandRuntime(agent, proc, deps.stateManager?.getSession(poolKey)?.runtime);
  if ((!proc || proc.state === 'dead') && configuredRuntime === 'pi') {
    try {
      proc = await ensurePiCommandProcess(userId, agent, deps);
    } catch (error) {
      return { text: `Pi native runtime 启动失败：${error instanceof Error ? error.message : String(error)}` };
    }
  }
  if (!proc) return { text: `${agent.emoji} ${agent.name} 当前没有 live runtime context。` };
  if (processRuntime(proc, agent.conversationRuntime?.runtime ?? agent.runtime ?? 'cc') !== 'pi') {
    return { text: '该命令应交给当前非 Pi runtime 的原生命令路径。' };
  }
  if (!deps.runtime.getNativeState || !deps.runtime.getNativeSessionStats) {
    return { text: '当前 runtime manager 不支持 Pi native context 状态。' };
  }
  try {
    const [state, stats] = await Promise.all([
      deps.runtime.getNativeState(poolKey),
      deps.runtime.getNativeSessionStats(poolKey),
    ]);
    const context = stats.contextUsage as Record<string, unknown> | undefined;
    const tokens = stats.tokens as Record<string, unknown> | undefined;
    return {
      text: [
        `${agent.emoji} ${agent.name} Pi Context`,
        `sessionId=${String(state.sessionId ?? proc.sessionId)}`,
        `messages=${String(state.messageCount ?? stats.totalMessages ?? 'unknown')}`,
        `contextTokens=${String(context?.tokens ?? tokens?.total ?? 'unknown')}`,
        `contextWindow=${String(context?.contextWindow ?? 'unknown')}`,
        `percent=${String(context?.percent ?? 'unknown')}`,
        `autoCompaction=${String(state.autoCompactionEnabled ?? 'unknown')}`,
      ].join('\n'),
    };
  } catch (error) {
    return { text: `读取 Pi context 失败：${error instanceof Error ? error.message : String(error)}` };
  }
}

export async function handleYolo(
  userId: string,
  agentId: string,
  requestedMode: string | undefined,
  deps: CommandDeps,
): Promise<CommandResult> {
  const { router, runtime } = deps;
  const agent = agentId
    ? router.getAgents(userId).find(a => a.id === agentId)
    : router.getActiveAgent(userId);

  if (!agent) {
    return { text: '无活跃 Agent。' };
  }

  if (!runtime.setYoloMode || !runtime.getYoloMode) {
    return { text: '当前 runtime manager 不支持 Codex YOLO 模式。' };
  }

  const poolKey = router.poolKey(userId, agent.id);
  const proc = runtime.get(poolKey);
  const persisted = deps.stateManager?.getSession(poolKey);
  const effectiveRuntime = effectiveCommandRuntime(agent, proc, persisted?.runtime);

  if (effectiveRuntime !== 'codex') {
    return { text: `当前 Agent runtime=${effectiveRuntime}，YOLO 只对 Codex 生效。请先执行 /runtime codex。` };
  }

  if (!requestedMode?.trim()) {
    return buildYoloOptionCard(agent);
  }

  const normalized = requestedMode.trim().toLowerCase();
  const defaultEnabled = agent.codexYoloDefault === true;
  runtime.setYoloDefaultMode?.(poolKey, defaultEnabled);

  if (normalized === 'status') {
    const enabled = runtime.getYoloMode(poolKey);
    const override = runtime.getYoloOverrideMode?.(poolKey);
    return {
      text: [
        `${agent.emoji} ${agent.name} Codex YOLO: ${enabled ? 'on' : 'off'}`,
        `default=${defaultEnabled ? 'on' : 'off'}`,
        `sessionOverride=${override === undefined ? 'none' : override ? 'on' : 'off'}`,
        '作用范围: default 来自 relay.json；sessionOverride 只在当前 session 内存中生效。',
        '用法: /yolo on 或 /yolo off',
      ].join('\n'),
    };
  }

  if (normalized === 'on') {
    runtime.setYoloMode(poolKey, true);
    return {
      text: [
        `⚠️ ${agent.emoji} ${agent.name} Codex YOLO 已开启。`,
        '后续 Codex turn 将使用 --dangerously-bypass-approvals-and-sandbox。',
        `default=${defaultEnabled ? 'on' : 'off'}；sessionOverride=on。`,
        '不会清理或新建 thread；不会写入 relay.json。',
        '关闭: /yolo off',
      ].join('\n'),
    };
  }

  if (normalized === 'off') {
    runtime.setYoloMode(poolKey, false);
    return {
      text: [
        `${agent.emoji} ${agent.name} Codex YOLO 已关闭。`,
        `default=${defaultEnabled ? 'on' : 'off'}；sessionOverride=off。`,
        `后续 Codex turn 恢复 workspace-write + approval=never；当前 thread 保持不变；/new 或 Relay 重启后恢复默认 ${defaultEnabled ? 'on' : 'off'}。`,
      ].join('\n'),
    };
  }

  return { text: '用法: /yolo on、/yolo off 或 /yolo status' };
}

export async function handleNew(userId: string, agentId: string, deps: CommandDeps): Promise<CommandResult> {
  const { router, runtime } = deps;
  const agent = agentId
    ? router.getAgents(userId).find(a => a.id === agentId)
    : router.getActiveAgent(userId);

  if (!agent) {
    return { text: agentId ? `未知 Agent: ${agentId}` : '无活跃 Agent。' };
  }

  const poolKey = router.poolKey(userId, agent.id);
  const replacementBlocked = blockBoundWorkItemSessionReplacement(poolKey, deps, '创建新 session');
  if (replacementBlocked) return replacementBlocked;
  if (switchInProgress(poolKey)) return { text: '一次 /wi switch 正在替换会话，完成后再 /new；未做任何改动。' };
  const persistedBefore = typeof deps.stateManager?.getSession === 'function'
    ? deps.stateManager.getSession(poolKey) : undefined;

  // An unfinished /wi switch never applies to the session this command makes (native-wi-switch.ts).
  if (persistedBefore?.pendingWiSwitch && deps.stateManager) abandonWiSwitch(deps.stateManager, poolKey, '/new');
  // Kill existing process
  const proc = runtime.get(poolKey);
  if (proc) {
    await runtime.kill(poolKey);
  }
  if (runtime.clearYoloMode) runtime.clearYoloMode(poolKey);
  else runtime.setYoloMode?.(poolKey, false);

  // Reset session ID
  const newSessionId = router.resetSession(userId, agent.id, deps.inboundChannel === 'feishu' ? 'feishu' : undefined);
  if (!newSessionId) {
    return { text: '重置 session 失败。' };
  }

  // Persist new sessionId so it survives Relay restart
  if (deps.stateManager) {
    const configuredRuntime = agent.conversationRuntime?.runtime
      ?? agent.conversationRuntimePreference?.[0]
      ?? agent.runtime
      ?? 'cc';
    deps.stateManager.updateSession(poolKey, {
      poolKey,
      agentId: agent.id,
      runtime: configuredRuntime,
      sessionId: newSessionId,
      codexThreadId: undefined,
      // /think is a per-session setting: a new session starts at the runtime default (reasoning-control.ts).
      reasoningLevel: undefined,
      pendingWiSwitch: undefined,
      wrapRequestedAt: undefined,
      recoveryRequiredAt: undefined,
      recoveryReason: undefined,
      sclBusinessDispatchAllowed: undefined,
      terminalWorkItemRuntimeBinding: undefined,
      pendingWorkItemEntry: undefined,
      // A retired legacy entry has no remaining writer; keep it as audit history.
      ...(persistedBefore?.pendingWorkItemEntry ? { stoppedWorkItemEntries: [...(persistedBefore.stoppedWorkItemEntries ?? []),
        { entry: persistedBefore.pendingWorkItemEntry, sourceSessionId: persistedBefore.sessionId, stoppedAt: new Date().toISOString() }] } : {}),
      activeWorkItemRuntimeBinding: undefined,
      turnCount: 0,
      maxTurns: agent.maxTurns ?? 100,
      lastActivity: Date.now(),
      status: 'active',
    });
    deps.stateManager.flush();
  }

  return { text: `${agent.emoji} ${agent.name} 已开启新对话。\nSession: ${newSessionId.slice(0, 8)}...` };
}

export async function handleReset(userId: string, agentId: string, deps: CommandDeps): Promise<CommandResult> {
  const { router, runtime, stateManager } = deps;
  const agent = agentId
    ? router.getAgents(userId).find(a => a.id === agentId)
    : router.getActiveAgent(userId);
  if (!agent) return { text: agentId ? `未知 Agent: ${agentId}` : '无活跃 Agent。' };

  const poolKey = router.poolKey(userId, agent.id);
  // A switch in flight owns the conversation's session until it settles; its record then yields to /reset.
  if (switchInProgress(poolKey)) return { text: '一次 /wi switch 正在替换会话，完成后再 /reset；未做任何改动。' };
  const before = stateManager?.getSession(poolKey);
  const resetAt = new Date().toISOString();
  // Stop old automatic WI continuation before terminating its native process.
  // /reset deliberately does not ask SCL to release or transfer WI authority.
  try {
    if (before && stateManager) {
      stateManager.updateSession(poolKey, { sclBusinessDispatchAllowed: false });
      // An unfinished /wi switch never applies to the session this reset makes (native-wi-switch.ts).
      if (before.pendingWiSwitch) abandonWiSwitch(stateManager, poolKey, '/reset');
      stateManager.flushStrict();
    }
    if (runtime.get(poolKey)) await runtime.kill(poolKey);
  } catch (error) {
    return { text: `重置 session 失败；旧 session 未释放。诊断：${error instanceof Error ? error.message : String(error)}` };
  }

  const oldAgentSessionId = agent.sessionId;
  const oldFeishuSessionId = agent.feishu?.sessionId;
  const newSessionId = router.resetSession(userId, agent.id, deps.inboundChannel === 'feishu' ? 'feishu' : undefined);
  if (!newSessionId) return { text: '重置 session 失败；旧 session 未释放。' };

  try {
    if (stateManager) {
      const configuredRuntime = agent.conversationRuntime?.runtime
        ?? agent.conversationRuntimePreference?.[0]
        ?? agent.runtime
        ?? 'cc';
      stateManager.updateSession(poolKey, {
        poolKey,
        agentId: agent.id,
        runtime: configuredRuntime,
        sessionId: newSessionId,
        codexThreadId: undefined,
        reasoningLevel: undefined,
        wrapRequestedAt: undefined,
        recoveryRequiredAt: undefined,
        recoveryReason: undefined,
        sclBusinessDispatchAllowed: undefined,
        terminalWorkItemRuntimeBinding: undefined,
        pendingWorkItemEntry: undefined,
        pendingWiSwitch: undefined,
        // A pending rollover keeps its original request and binds the session this reset made as its stand-in.
        ...(before?.pendingNativeRollover ? { pendingNativeRollover: { ...before.pendingNativeRollover, standInSessionId: newSessionId } } : {}),
        activeWorkItemRuntimeBinding: undefined,
        stoppedWorkItemEntries: before?.pendingWorkItemEntry ? [
          ...(before.stoppedWorkItemEntries ?? []),
          { entry: before.pendingWorkItemEntry, sourceSessionId: before.sessionId, stoppedAt: resetAt },
        ] : before?.stoppedWorkItemEntries,
        forcedSessionResets: before ? [
          ...(before.forcedSessionResets ?? []),
          { sessionId: before.sessionId, resetAt, ...(before.activeWorkItemRuntimeBinding ? { binding: before.activeWorkItemRuntimeBinding } : {}) },
        ] : undefined,
        interruptedUserMessage: undefined,
        wasBusyAtShutdown: undefined,
        turnCount: 0,
        maxTurns: agent.maxTurns ?? 100,
        lastActivity: Date.now(),
        status: 'active',
      });
      stateManager.flushStrict();
    }
  } catch (error) {
    agent.sessionId = oldAgentSessionId;
    if (agent.feishu && oldFeishuSessionId !== undefined) agent.feishu.sessionId = oldFeishuSessionId;
    if (stateManager) {
      if (before) stateManager.updateSession(poolKey, before);
      else stateManager.removeSession(poolKey);
    }
    return { text: `重置 session 失败；旧 session 未释放。诊断：${error instanceof Error ? error.message : String(error)}` };
  }

  if (runtime.clearYoloMode) runtime.clearYoloMode(poolKey);
  else runtime.setYoloMode?.(poolKey, false);
  return { text: [
    '已强制重置 session；历史上下文已丢弃。',
    `${agent.emoji} ${agent.name} 已开启新对话。\nSession: ${newSessionId.slice(0, 8)}...`,
    before?.activeWorkItemRuntimeBinding || before?.pendingWorkItemEntry
      ? '旧 WI 关联已保留审计；此操作不改变 Harness 中的 WI 权限或状态。'
      : '',
  ].filter(Boolean).join('\n') };
}

export async function handleStop(userId: string, agentId: string, deps: CommandDeps): Promise<CommandResult> {
  const { router, runtime } = deps;
  const agent = agentId
    ? router.getAgents(userId).find(a => a.id === agentId)
    : router.getActiveAgent(userId);
  if (!agent) {
    return { text: '无活跃 Agent。' };
  }

  const poolKey = router.poolKey(userId, agent.id);
  const proc = runtime.get(poolKey);
  if (!proc || proc.state === 'dead') {
    return { text: `${agent.emoji} ${agent.name} 未在运行。` };
  }

  if (proc.state !== 'busy') {
    return { text: `${agent.emoji} ${agent.name} 当前空闲，无需终止。` };
  }

  try {
    if (runtime.abortTurn) {
      await runtime.abortTurn(poolKey);
    } else if (proc.process) {
      // Legacy runtime compatibility: interrupt the current turn in-process.
      proc.process.kill('SIGINT');
    } else {
      return { text: `${agent.emoji} ${agent.name} 当前运行时不支持 /stop。` };
    }
    return { text: `已中断 ${agent.emoji} ${agent.name} 当前工作。` };
  } catch {
    return { text: `中断失败，进程可能已结束。` };
  }
}

export async function handleKill(userId: string, agentId: string, deps: CommandDeps): Promise<CommandResult> {
  const { router, runtime } = deps;
  const agent = agentId
    ? router.getAgents(userId).find(a => a.id === agentId)
    : router.getActiveAgent(userId);
  if (!agent) {
    return { text: '无活跃 Agent。' };
  }

  const poolKey = router.poolKey(userId, agent.id);
  const proc = runtime.get(poolKey);
  if (!proc) {
    return { text: `${agent.emoji} ${agent.name} 未启动，无需终止。` };
  }

  await runtime.kill(poolKey);
  return { text: `已终止 ${agent.emoji} ${agent.name} 进程。下次发消息时自动重启。` };
}

export async function handleRestart(userId: string, agentId: string, deps: CommandDeps): Promise<CommandResult> {
  const { router, runtime } = deps;
  const agent = agentId
    ? router.getAgents(userId).find(a => a.id === agentId)
    : router.getActiveAgent(userId);
  if (!agent) {
    return { text: '无活跃 Agent。' };
  }

  const poolKey = router.poolKey(userId, agent.id);

  await acquireConversation({
    poolKey, agent, runtime, stateManager: deps.stateManager,
    channel: deps.inboundChannel ?? 'telegram', intent: 'restart',
  });

  return { text: `已重启 ${agent.emoji} ${agent.name}。` };
}

export function handleSession(userId: string, agentId: string, deps: CommandDeps): CommandResult {
  const { router, runtime } = deps;
  const agent = agentId
    ? router.getAgents(userId).find(a => a.id === agentId)
    : router.getActiveAgent(userId);

  if (!agent) {
    return { text: '无活跃 Agent。' };
  }

  const poolKey = router.poolKey(userId, agent.id);
  const proc = runtime.get(poolKey);
  const persisted = deps.stateManager?.getSession(poolKey);
  const runtimeId = processRuntime(proc, persisted?.runtime ?? agent.conversationRuntime?.runtime ?? agent.runtime ?? 'cc');
  const state = proc ? proc.state : (persisted ? '待恢复' : '未启动');
  const lastActivity = proc?.lastActivity ?? persisted?.lastActivity;
  const codexThreadId = processCodexThreadId(proc) ?? persisted?.codexThreadId;

  // The selected channel owns its fallback; live/persisted identity takes precedence.
  const configured = configuredChannelSession(agent, deps.inboundChannel);
  const sessionId = proc?.sessionId || persisted?.sessionId || configured || "";
  const sessionIdSource = proc?.sessionId ? "live-process"
    : persisted?.sessionId ? "state.json"
    : configured ? (deps.inboundChannel === "feishu" || !agent.botToken ? "relay.json:feishu" : "relay.json:top")
    : "none";

  const lines = [
    `${agent.emoji} ${agent.name} Session 信息`,
    `Session ID: ${sessionId || 'N/A'} (来源: ${sessionIdSource})`,
    `状态: ${state}`,
    `runtime=${runtimeId}`,
    `sessionId=${sessionId || 'N/A'}`,
    `state=${state}`,
    `lastActivity=${formatLastActivity(lastActivity)}`,
    `工作区: ${agent.workspace}`,
  ];

  // Resolve the actual jsonl path. CC writes to ~/.claude/projects/<cwd-encoded>/<sid>.jsonl,
  // but --bare mode writes to .claude/projects/-root/<sid>.jsonl. Probe both.
  if (sessionId && runtimeId === 'cc') {
    const home = agent.linuxUser ? `/home/${agent.linuxUser}` : (process.env['HOME'] ?? '');
    const primary = getSessionJsonlPath(agent.workspace, sessionId, home);
    const bare = getBareSessionJsonlPath(sessionId, home);
    let actual = primary;
    let exists = false;
    try { exists = existsSync(primary); } catch { /* ignore */ }
    if (!exists) {
      try {
        if (existsSync(bare)) { actual = bare; exists = true; }
      } catch { /* ignore */ }
    }
    lines.push(`JSONL: ${actual}${exists ? '' : ' (未落盘)'}`);
  }

  if (runtimeId === 'codex') {
    lines.push(`threadId=${codexThreadId ? `${codexThreadId.slice(0, 8)}...` : 'N/A'}`);
  }
  if (runtimeId === 'agy') {
    const live = proc && 'agyConversationId' in proc ? (proc as { agyConversationId?: string }).agyConversationId : undefined;
    const saved = persisted?.agyConversation?.sessionId === sessionId ? persisted.agyConversation.conversationId : undefined;
    lines.push(`conversationId=${live ?? saved ?? 'N/A'}`);
  }

  if (proc) {
    const uptime = Math.round((Date.now() - proc.spawnedAt) / 1000);
    const uptimeStr = uptime < 60
      ? `${uptime} 秒`
      : `${Math.round(uptime / 60)} 分钟`;
    lines.push(`运行时长: ${uptimeStr}`);
    lines.push(`PID: ${proc.process?.pid ?? 'N/A'}`);
  } else if (persisted) {
    lines.push('说明: 当前进程未运行，但 session 仍保留，发送消息即可自动恢复。');
  }

  return { text: lines.join('\n') };
}

/**
 * `/cc` — Kill Relay's CC process and return a command for manual session takeover.
 * macOS: returns a local `claude --resume` command.
 * Linux: returns an SSH command (requires `sshAlias` in relay.json).
 */
export async function handleCc(userId: string, agentId: string, deps: CommandDeps): Promise<CommandResult> {
  const { router, runtime } = deps;
  const agent = agentId
    ? router.getAgents(userId).find(a => a.id === agentId)
    : router.getActiveAgent(userId);

  if (!agent) {
    return { text: '无活跃 Agent。' };
  }

  const poolKey = router.poolKey(userId, agent.id);
  const proc = runtime.get(poolKey);
  const sessionId = proc?.sessionId || deps.stateManager?.getSession(poolKey)?.sessionId
    || configuredChannelSession(agent, deps.inboundChannel);
  if (!sessionId) {
    return { text: `${agent.emoji} ${agent.name} 无 session ID，无法接管。` };
  }

  // Kill Relay's CC process so the manual CC can resume without conflict
  if (proc) {
    await runtime.kill(poolKey);
  }

  // Web Terminal path: if terminalManager is available, generate a browser URL
  if (deps.terminalManager) {
    // Build agent-specific CC args mirroring pool.ts spawn parameters,
    // so the terminal CC has the same provider/model/config as Relay-managed CC.
    const ccArgs: string[] = [
      '--setting-sources', 'user,project,local',  // Read ~/.claude/settings.json for env/model config
    ];
    // Add workspace access
    if (agent.workspace) {
      ccArgs.push('--add-dir', agent.workspace);
    }
    const ccEnv: Record<string, string> = {};
    const ccModel = router.getAgentModel(userId, agent.id);
    const ccRuntimeModel = resolveClaudeRuntimeModel(ccModel ?? 'auto');
    const entry = ccModel ? deps.modelCatalog?.findById(ccModel) : undefined;
    if (ccModel && ccModel !== 'auto' && deps.modelCatalog) {
      if (entry?.provider) {
        const provider = deps.modelCatalog.findProvider(entry.provider);
        if (provider) {
          // Resolve API key from pool (first available)
          const keyVar = provider.api_key_pool.find(v => process.env[v]);
          if (keyVar) {
            ccEnv['ANTHROPIC_AUTH_TOKEN'] = process.env[keyVar]!;
            const baseUrl = provider.base_url_env
              ? (process.env[provider.base_url_env] ?? '')
              : '';
            if (baseUrl) ccEnv['ANTHROPIC_BASE_URL'] = baseUrl;
          }
        }
      }
      ccArgs.push('--model', ccRuntimeModel);
    }
    // Model mappings from global env (systemd service config)
    for (const key of INHERITED_ROUTING_ENV_KEYS) {
      if (!key.startsWith('ANTHROPIC_DEFAULT_')) continue;
      if (key === 'ANTHROPIC_DEFAULT_FABLE_MODEL' && ccRuntimeModel === 'fable' && !entry?.provider) {
        ccEnv[key] = '';
      } else if (process.env[key]) {
        ccEnv[key] = process.env[key]!;
      }
    }
    // --settings with env block (provider credentials + model mappings)
    ccArgs.push('--settings', JSON.stringify({ env: ccEnv }));

    const token = deps.terminalManager.createToken({
      tenantId: poolKey,
      workspace: agent.workspace,
      sessionId,
      linuxUser: agent.linuxUser,
      userHome: agent.userHome,
      ccArgs,
      ccEnv,
    });
    const isLocal = process.platform === 'darwin';
    const httpPort = process.env['RELAY_HTTP_PORT'];
    const publicHost = process.env['RELAY_PUBLIC_HOST'];
    const url = publicHost
      ? `https://${publicHost}/terminal/${token}`
      : `http://${isLocal ? 'localhost' : (router.getSshAlias?.() ?? 'localhost')}:${httpPort}/terminal/${token}`;
    return {
      text: `${agent.emoji} ${agent.name} 已释放，点击下方链接在浏览器中接管：`,
      feishuCardElements: [
        { tag: 'markdown', content: `[🔗 打开终端](${url})` },
        { tag: 'hr' },
        { tag: 'markdown', content: "<font color='grey'>⏱️ 终端 10 分钟无活动自动关闭。下次飞书对话建议先发 /new。</font>" },
      ],
    };
  }

  // Fallback: SSH command path (no terminal manager configured)
  const isLocal = process.platform === 'darwin';
  const sshAlias = router.getSshAlias?.();
  let command: string;
  if (isLocal || !sshAlias) {
    command = `cd '${agent.workspace}' && claude --resume ${sessionId}`;
  } else {
    const sudoUser = agent.linuxUser ?? 'pulse';
    command = `ssh ${sshAlias} -t 'sudo -u ${sudoUser} env HOME=${agent.workspace} PATH=/usr/local/bin:/usr/bin:/bin claude --resume ${sessionId}'`;
  }

  return {
    text: `${agent.emoji} ${agent.name} 已释放，可手动接管。复制以下命令在终端中执行：`,
    feishuCardElements: [
      { tag: 'markdown', content: `\`${command}\`` },
      { tag: 'hr' },
      { tag: 'markdown', content: "<font color='grey'>⚠️ 下次通过飞书对话时建议先发 /new 开新 session。</font>" },
    ],
  };
}

function formatTokens(value: number): string {
  return value.toLocaleString('en-US');
}

export async function handleUsageSeal(userId: string, agentId: string, deps: CommandDeps): Promise<CommandResult> {
  const { router, runtime } = deps;
  const agent = agentId
    ? router.getAgents(userId).find(a => a.id === agentId)
    : router.getActiveAgent(userId);
  if (!agent) return { text: '无活跃 Agent，尚未发送封存请求。平台核对当前入口的 Agent 配置，不推断或新建 WI。' };

  const poolKey = router.poolKey(userId, agent.id);
  const proc = runtime.get(poolKey);
  if (!proc || proc.state === 'dead') return { text: '当前 Pi runtime 未运行，尚未发送封存请求。平台核对原会话状态与已有用量制品；恢复原会话后再处理，不重建历史。' };
  const sealRuntime = processRuntime(proc, agent.runtime ?? 'cc');
  // CC 由 Relay Host 读取 transcript 封存，与 Pi 扩展封存产出同一 SCL 有界测量。
  if (sealRuntime !== 'pi' && sealRuntime !== 'cc') return { text: 'usage seal 仅支持当前为 Pi 或 CC 的 runtime，本次未封存。当前 Agent 核对实际运行类型，不为测试切换运行时。' };
  if (proc.state === 'busy' || proc.state === 'starting') return { text: '当前 Pi 尚未 settled，尚未发送封存请求。当前 Agent 等本轮业务结束后核对原请求；系统尚未安排后台封存。' };

  const persisted = deps.stateManager?.getSession(poolKey);
  try {
    const binding = persistedRuntimeUsageBinding(persisted, sealRuntime, proc.sessionId);
    if (!binding) return { text: '当前 Pi 没有可验证的 WI usage binding，拒绝封存。平台核对原 WI 的用量绑定与执行代际，不创建替代基线或重复授权。' };
  } catch {
    return { text: '当前 Pi 的 WI usage authority 冲突，拒绝封存。平台比对原绑定和已有封存凭据，只修复冲突；保留用量历史。' };
  }

  if (!runtime.sealRuntimeUsage) return { text: '当前 Relay 不支持安全的 Pi extension command，拒绝封存。平台核对已安装的 Pi 扩展能力，修复后沿原请求处理，不要求重置会话。' };
  try {
    const readback = await runtime.sealRuntimeUsage(poolKey);
    return {
      text: [
        `usage seal: ${readback.status}`,
        `WI=${readback.work_item_id} epoch=${readback.epoch}`,
        `artifact=${readback.artifact_ref}`,
        `sealed_digest=${readback.sealed_digest}`,
      ].join('\n'),
    };
  } catch (error) {
    const code = error instanceof RuntimeUsageSealError ? error.code : 'command_transport_failed';
    return { text: `usage seal 失败 [${code}]；尚未取得可验证的 sealed artifact readback，不能据此认定没有写入。平台先读回原封存制品与摘要，只恢复未完成部分，不重复未知封存；尚未确认后台重试。` };
  }
}

export function handleUsage(userId: string, agentId: string, deps: CommandDeps): CommandResult {
  const { router, meteringDir } = deps;
  const agent = agentId
    ? router.getAgents(userId).find(a => a.id === agentId)
    : router.getActiveAgent(userId);

  if (!agent) {
    return { text: '无活跃 Agent。' };
  }

  if (!meteringDir) {
    return {
      text: [
        `${agent.emoji} ${agent.name} 当前 Relay 未启用 metering 目录，无法统计费用。`,
        '请在启动参数或 METERING_DIR 环境变量中提供 metering 目录后再试。',
      ].join('\n'),
    };
  }

  const tenant = router.poolKey(userId, agent.id);
  const summarize = deps.summarizeUsage ?? summarizeMonthlyUsage;
  const summary = summarize(meteringDir, tenant);

  if (summary.recordCount === 0) {
    return {
      text: [
        `${agent.emoji} ${agent.name} 本月用量`,
        `周期: ${summary.monthStart} ~ ${summary.monthEnd}（UTC）`,
        '当前周期暂无 metering 记录。',
        summary.malformedEntries > 0
          ? `⚠️ 跳过 ${summary.malformedEntries} 条损坏记录。`
          : '',
      ].filter(Boolean).join('\n'),
    };
  }

  const lines = [
    `${agent.emoji} ${agent.name} 本月用量`,
    `周期: ${summary.monthStart} ~ ${summary.monthEnd}（UTC，${summary.daysCovered} 天有数据）`,
    `调用次数: ${summary.recordCount}`,
    `Cost: $${summary.totalCostUsd.toFixed(4)} USD`,
    `Input tokens:  ${formatTokens(summary.totalInputTokens)}`,
    `Output tokens: ${formatTokens(summary.totalOutputTokens)}`,
    `Cache read:    ${formatTokens(summary.totalCacheReadInputTokens)}`,
    `Cache create:  ${formatTokens(summary.totalCacheCreationInputTokens)}`,
  ];

  if (summary.malformedEntries > 0) {
    lines.push(`⚠️ 跳过 ${summary.malformedEntries} 条损坏记录（统计仅含可读字段）。`);
  }

  return { text: lines.join('\n') };
}
