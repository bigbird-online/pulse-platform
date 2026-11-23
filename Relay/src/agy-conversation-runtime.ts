/**
 * agy（Antigravity CLI）会话运行时：每轮拉起一次 `agy -p … --output-format stream-json`，
 * 续会话用 `--conversation <id>`。接口依据见 wi_20261120_666 N01（agy 1.5.13 实测）。
 *
 * - 账户 HOME：agy 的登录在账户钥匙串里，启动脚本也在账户 HOME 下；Agent HOME 读不到，
 *   所以子进程的 HOME 固定为系统账户 HOME（userInfo），不跟随 Relay 的 HOME。
 * - 出口：代理由启动脚本设置，这里不写代理地址。代理挂起时 agy 的 `--print-timeout`
 *   不生效，故运行时自设外层超时，并从本轮 `--log-file` 识别出口故障，明确报错，不重试。
 * - 用量：续会话时 result.usage 是整会话累计值；本轮用量取本轮各步 usage 之和，
 *   拿不到才用前后两次累计值相减，都不行就不报数（未知）。output_tokens 已含思考 token。
 * - 权限：无头模式被拒时退出码仍为 0，须读 denied_actions 并上报。
 * - MCP：无头模式不能按会话注入 MCP（只能改全局配置），本运行时不注入。
 * - 会话号：消息里的 session_id 在 Relay 里指 Relay 会话（启动入口据此回写顶层 sessionId），
 *   agy 的对话号另用 agy_conversation_id 承载，不写 session_id。
 * - 退役：kill 后（/new、/reset、切运行时）旧子进程迟到的输出与收尾不落盘、不发事件。
 * - 停止：/stop、kill 与超时都先 SIGTERM，宽限期后 SIGKILL；再过一个宽限期仍未报告退出，
 *   本轮按已停止收尾，不让会话一直 busy。
 * - 失败：一轮失败只发错误结果与 error 事件（渠道据此发一条明确通知），不发 session-end：
 *   agy 会话与对话都还在，没有任何东西被"自动恢复"。
 * - 会话来源证明：init 给出 conversation_id 后，为本轮 agy 进程写 runtime=agy 的证明
 *   （启动脚本 exec 真二进制，PID 不变），进程退出即删，内核据此认定会话所属对话；
 *   另为该对话写一份保留记录（不随进程删除），空闲时 /wi 命令靠它认出会话。
 * - WI 用量：/wi 把 WI 交给本会话后，Relay 发下 runtime usage binding（bindRuntimeUsage），
 *   之后每轮的 Host 记录都带这个 WI。
 */
import { createHash } from 'node:crypto';
import { spawn as cpSpawn, type ChildProcess } from 'node:child_process';
import { EventEmitter } from 'node:events';
import { existsSync, mkdirSync, readFileSync, realpathSync, rmSync } from 'node:fs';
import { join } from 'node:path';
import { userInfo } from 'node:os';
import { createInterface } from 'node:readline';
import { stripRelaySecrets } from './relay-secrets.js';
import { forgetNativeProofOnExit, observeNativeSession, retainNativeConversation } from './native-core-runtime.js';
import { publicCommentaryEvent } from './public-assistant-progress.js';
import { publicWorkEvents } from './work-process-events.js';
import { redactRuntimeDiagnostic } from './public-runtime-failure.js';
import { recordQueuedAtShutdown } from './shutdown-interrupts.js';
import { nextTurnSeq } from './maintenance-barrier.js';
import { runtimeUsageBindingEnvironment, type RuntimeUsageBinding } from './runtime-usage-binding.js';
import { resolveRelayDataDir, sharedAccessPromptLines, standingAccessPromptLine } from './config-paths.js';
import { ccBashAccess, ccPolicyPaths, ccWriteDenials, ccWriteRoots } from './cc-write-boundary.js';
import { standingAccessFor } from './standing-access.js';
import { nodeWorkItemCore, readRetainedSessionAccess } from './native-work-item-command.js';
import { AGY_MODELS, AGY_MODEL_PROVIDER, DEFAULT_AGY_MODEL, isAgyModel } from './agy-models.js';
import type { Counts, NativeModelUsage } from './native-model-usage.js';
import { refuseMisroutedTurn, refusedTurnResult, settleInternalTurn, type InternalTurnOutcome } from './internal-turn-fence.js';
import type { ConversationModelRoute, ConversationReasoningLevel, ConversationTurnContext, PersistedSession, SessionEndReason, StateManager, StreamJsonMessage } from './types.js';
import type {
  ConversationProcess,
  ConversationRuntimeAdapter,
  ConversationRuntimeModelOption,
  ConversationSpawnConfig,
  ConversationStatusInfo,
} from './conversation-runtime.js';

const DEFAULT_TIMEOUT_MS = 10 * 60 * 1000;
/** 代理挂起时 agy 卡在回合开始前（N01 e04：约 60 秒后日志出现 deadline exceeded，进程仍不退出）。 */
const DEFAULT_START_TIMEOUT_MS = 90 * 1000;
const DEFAULT_KILL_GRACE_MS = 5_000;
/** After exit, how long stdout may still deliver the final result before the turn settles without it. */
const DEFAULT_EXIT_DRAIN_MS = 2_000;

// The catalog has no runtime imports, so the launch selection can read it (agy-models.ts).
export { AGY_MODELS, AGY_MODEL_PROVIDER, DEFAULT_AGY_MODEL, isAgyModel } from './agy-models.js';

const AGY_LEVEL_ORDER: ConversationReasoningLevel[] = ['low', 'medium', 'high', 'xhigh'];
const AGY_LEVEL_SUFFIX = /^(.*)-(low|medium|high|xhigh)$/;

export function agyModelOptions(): ConversationRuntimeModelOption[] {
  return AGY_MODELS.map((entry) => ({ provider: AGY_MODEL_PROVIDER, model: entry.model, displayName: entry.displayName }));
}

/** 模型族与档位：`gemini-4-neon-low` → gemini-4-neon / low；没有档位后缀的模型自成一族。 */
export function agyModelFamily(model: string): { family: string; level?: ConversationReasoningLevel } {
  const match = AGY_LEVEL_SUFFIX.exec(model);
  return match ? { family: match[1]!, level: match[2] as ConversationReasoningLevel } : { family: model };
}

function agyFamilyMembers(family: string): Array<{ model: string; displayName: string; level?: ConversationReasoningLevel }> {
  return AGY_MODELS.flatMap((entry) => {
    const parsed = agyModelFamily(entry.model);
    return parsed.family === family ? [{ ...entry, level: parsed.level }] : [];
  });
}

/** 当前模型族实测提供的档位，从低到高；未知模型按默认模型算。 */
export function agyModelLevels(model: string | undefined): ConversationReasoningLevel[] {
  const { family } = agyModelFamily(isAgyModel(model) ? model : DEFAULT_AGY_MODEL);
  const levels = new Set(agyFamilyMembers(family).flatMap((entry) => entry.level ? [entry.level] : []));
  return AGY_LEVEL_ORDER.filter((level) => levels.has(level));
}

/** 同一模型族在该档位的模型 ID；该族没有这个档位时为 undefined。 */
export function agyModelForLevel(model: string, level: ConversationReasoningLevel): string | undefined {
  const { family } = agyModelFamily(model);
  return agyFamilyMembers(family).find((entry) => entry.level === level)?.model;
}

/** 本轮实际用的模型：配置的模型（未知则默认），再叠加会话档位（该族有此变体时）。 */
export function agyEffectiveModel(model: string | undefined, level?: ConversationReasoningLevel): string {
  const base = isAgyModel(model) ? model : DEFAULT_AGY_MODEL;
  return (level && agyModelForLevel(base, level)) || base;
}

/** /model 卡片的模型族：每族一个按钮，显示名去掉档位括注。 */
export function agyModelFamilies(): Array<{ family: string; displayName: string; models: string[] }> {
  const families = new Map<string, { family: string; displayName: string; models: string[] }>();
  for (const entry of AGY_MODELS) {
    const { family } = agyModelFamily(entry.model);
    const row = families.get(family) ?? { family, displayName: entry.displayName.replace(/\s*\((?:Low|Medium|High|XHigh)\)$/, ''), models: [] };
    row.models.push(entry.model);
    families.set(family, row);
  }
  return [...families.values()];
}

/** 系统账户 HOME：不受 Relay/Agent 进程 HOME 影响。 */
export function agyAccountHome(): string {
  return userInfo().homedir;
}

/** 启动脚本：显式参数 → $PULSE_AGY_BIN → <账户 HOME>/.local/bin/agy。 */
export function resolveAgyBin(explicit?: string): string {
  return explicit?.trim() || process.env['PULSE_AGY_BIN']?.trim() || join(agyAccountHome(), '.local', 'bin', 'agy');
}

/**
 * agy 只以 Relay 主机账号和它的 HOME 运行（登录在该账号钥匙串里）。要求独立 Linux 用户
 * 隔离的 Agent 用 agy 会失去原有的 UID 与路径隔离，所以一律拒绝，返回给人的原因。
 */
export function agyIsolationRefusal(linuxUser: string | undefined): string | undefined {
  return linuxUser?.trim()
    ? '本 Agent 配置了独立 Linux 用户隔离；agy 只能以 Relay 主机账号运行，会失去这层隔离，因此不能使用 agy。'
    : undefined;
}

/** 本节点是否装有 agy 启动脚本；没有就不在 /runtime 里提供 agy。 */
export function isAgyInstalled(explicit?: string): boolean {
  try { return existsSync(resolveAgyBin(explicit)); } catch { return false; }
}

export interface AgyCommandSpec { command: string; args: string[]; cwd: string }

/**
 * agy 会话的权限与同一 Agent 的 CC 会话实际生效的权限相同（大鸟 2026-11-23）。CC 会话一律带
 * `--dangerously-skip-permissions`；只有 Agent 配置了 `ccWriteBoundary`，Relay 才再给它沙箱与写边界
 * （cc-write-boundary.ts `ccWriteBoundarySettings`）。所以：
 * - 没有 `ccWriteBoundary`：CC 不受任何限制，agy 同样用 `--dangerously-skip-permissions`。
 * - 有 `ccWriteBoundary`：agy 进受限模式，规则从 CC 的同一份数据生成（`agyAccess`）。
 */
export function agyRestricted(config: { ccWriteBoundary?: boolean }): boolean {
  return config.ccWriteBoundary === true;
}

/**
 * 受限模式下允许出终端沙箱的命令，只有内核入口 `harness-native`：它对应 CC 在沙箱外调用的内核工具
 * （`pulse_work_item`，CC 的 MCP 工具不走 Bash 沙箱），agy 没有 MCP，只能经它办内核操作。其余命令与 CC
 * 一样留在沙箱里（CC 设 `allowUnsandboxedCommands: false`）。`command(<名字>)` 按首个词匹配（agy 1.5.14 实测）。
 */
export const AGY_UNSANDBOXED_COMMANDS = ['harness-native'] as const;

/** CC 受限会话的读取不受限（沙箱只设 allowWrite/denyWrite），agy 同样放行整棵树。 */
export const AGY_READ_ALL = 'read_file(/)';

/**
 * 受限 agy 会话的写与联网边界，与 CC 受限会话同一份数据：
 * - 写：`ccWriteRoots`（工作区、写授权、所持 WI 的受管工作区）加 `ccBashAccess` 的提交仓库 Git 路径；
 *   拒写为 `ccWriteDenials`、提交仓库的关闭路径与 `ccPolicyPaths`（各写根里改执行策略的入口、Relay 安装）。
 * - 联网：`ccBashAccess` 的 `allowedDomains`（CC 沙箱的 network.allowedDomains），没有配置就不联网。
 * 读取与 CC 一样不受限（AGY_READ_ALL）。
 */
export interface AgyAccess { writeRoots: string[]; deniedWrites: string[]; domains: string[] }

export function agyAccess(config: Pick<ConversationSpawnConfig, 'workspace' | 'sessionAccess' | 'allocatedWorkspaces'
  | 'repoAccess'>): AgyAccess {
  const sessionRoots = ccWriteRoots({ workspace: config.workspace, sessionAccess: config.sessionAccess,
    allocatedWorkspaces: config.allocatedWorkspaces });
  const bash = ccBashAccess(config.repoAccess);
  // The workspace is written by default (and named as the first write rule), as named or at its real path.
  const workspace = new Set([config.workspace, (() => { try { return realpathSync(config.workspace); } catch { return config.workspace; } })()]);
  return {
    writeRoots: [...new Set([...sessionRoots, ...bash.write])].filter((root) => !workspace.has(root)),
    deniedWrites: [...new Set([...ccWriteDenials(config.allocatedWorkspaces), ...bash.deny,
      ...ccPolicyPaths(sessionRoots)])],
    domains: bash.allowedDomains,
  };
}

/**
 * agy 在这一行之后才执行工具：受限运行固定开终端沙箱（`--sandbox` 与 settings 的
 * enableTerminalSandbox），沙箱可用时 CLI 日志写下这一行；沙箱不可用时 agy 静默忽略这两个开关
 * （官方文档 Terminal sandbox「If the sandbox isn't available」），所以没有这一行就拒绝这一轮。
 */
export const AGY_SANDBOX_ENABLED = 'Print mode: enabling terminal sandbox for this session';

/** 本轮 CLI 日志证明终端沙箱已开启。 */
export function agySandboxEnabled(logFile: string): boolean {
  try { return readFileSync(logFile, 'utf8').includes(AGY_SANDBOX_ENABLED); } catch { return false; }
}

export const AGY_SANDBOX_REFUSAL = 'agy terminal sandbox unavailable: the restricted run was stopped, nothing was relaxed';

/**
 * 受限运行固定的执行模式，不随账户 settings.json：默认模式（agentMode 为空，不用 auto 的出沙箱自动审核），
 * 终端沙箱开启，沙箱内命令免审，出沙箱一律需批准规则（无头时即拒绝）。`--mode` 没有 default 取值（实测报
 * unrecognized），所以只用 settings 固定。
 */
export const AGY_RESTRICTED_SETTINGS = { agentMode: '', enableTerminalSandbox: true, toolPermission: 'proceed-in-sandbox' } as const;

/**
 * 无头权限（agy 1.5.x 实测）：
 * - write_file 只放行工作区与额外可写根：`write_file(<dir>)`，含其子目录，不含同名前缀的兄弟目录。
 *   不写 `<dir>/**`：agy 由写规则推出终端沙箱的可写目录，glob 让沙箱报 `globs not supported`，
 *   沙箱内的命令一律起不来。沙箱内的命令无需批准，只能写这些目录。`deniedWrites` 列为拒写。
 * - read_file 不限目录（AGY_READ_ALL）；`read_url(<域名>)` 让沙箱内的命令只连这些域名（agyAccess）。
 * - 出沙箱（BypassSandbox）的命令无头时需要放行规则，否则被拒；只放行 AGY_UNSANDBOXED_COMMANDS。
 * yolo 与 Codex 的 bypass 同义，改用 --dangerously-skip-permissions（没有任何目录边界）。
 */
export function buildAgyCommand(input: {
  agyBin?: string;
  workspace: string;
  prompt: string;
  model: string;
  logFile: string;
  conversationId?: string;
  additionalWritableRoots?: string[];
  deniedWrites?: string[];
  domains?: string[];
  yoloMode?: boolean;
}): AgyCommandSpec {
  const extraRoots = [...new Set(input.additionalWritableRoots ?? [])]
    .filter((path) => path.trim().length > 0 && path !== input.workspace);
  const writeRoots = [...new Set([input.workspace, ...extraRoots].map((root) => root.replace(/(.)\/+$/, '$1')))];
  const permissionArgs = input.yoloMode
    ? ['--dangerously-skip-permissions']
    : ['--sandbox', '--settings', JSON.stringify({ ...AGY_RESTRICTED_SETTINGS, permissions: (() => {
      const deny = (input.deniedWrites ?? []).map((path) => `write_file(${path})`);
      return { allow: [
        ...AGY_UNSANDBOXED_COMMANDS.map((name) => `command(${name})`),
        ...writeRoots.map((root) => `write_file(${root})`),
        AGY_READ_ALL,
        ...[...new Set(input.domains ?? [])].map((domain) => `read_url(${domain})`)],
      ...(deny.length ? { deny } : {}) };
    })() })];
  return {
    command: resolveAgyBin(input.agyBin),
    args: [
      '-p', input.prompt,
      '--output-format', 'stream-json',
      '--model', input.model,
      '--log-file', input.logFile,
      '--disable-slash-commands',
      ...(input.conversationId ? ['--conversation', input.conversationId] : []),
      ...extraRoots.flatMap((root) => ['--add-dir', root]),
      ...permissionArgs,
    ],
    cwd: input.workspace,
  };
}

export function buildAgyEnv(binding?: RuntimeUsageBinding): NodeJS.ProcessEnv {
  const env = stripRelaySecrets({ ...process.env });
  env['HOME'] = agyAccountHome();
  try {
    Object.assign(env, runtimeUsageBindingEnvironment(binding, 'agy'));
  } catch (error) {
    console.warn(JSON.stringify({ level: 'warn', msg: 'agy runtime usage binding not injected',
      reason: error instanceof Error ? error.message : 'invalid binding' }));
  }
  return env;
}

export function agyMessageToPrompt(message: StreamJsonMessage): string {
  if (Array.isArray(message.attachments) && message.attachments.length > 0) {
    throw new Error('agy conversation runtime only supports text messages');
  }
  if (typeof message.content === 'string') return message.content;
  if (Array.isArray(message.content)) {
    return message.content.map((block) => {
      if (block && typeof block === 'object' && (block as { type?: unknown }).type === 'text'
          && typeof (block as { text?: unknown }).text === 'string') return (block as { text: string }).text;
      throw new Error('agy conversation runtime only supports text messages');
    }).join('\n\n');
  }
  throw new Error('agy conversation runtime only supports text messages');
}

/** A plain JSON object, or undefined: agy output lines and their fields are untrusted shapes. */
export function agyObject(value: unknown): Record<string, unknown> | undefined {
  return value !== null && typeof value === 'object' && !Array.isArray(value) ? value as Record<string, unknown> : undefined;
}

/** One stream-json line as an event object; valid JSON that is not an object (null, a number, an array) is no event. */
export function parseAgyEvent(line: string): Record<string, unknown> | undefined {
  try { return agyObject(JSON.parse(line)); } catch { return undefined; }
}

/** agy 的 usage 字段 → Relay 统一口径；任一字段不可读即视为未知。 */
export function agyUsageCounts(value: unknown): Counts | undefined {
  const raw = value !== null && typeof value === 'object' ? value as Record<string, unknown> : {};
  const pick = (key: string): number | undefined => {
    const n = raw[key];
    return typeof n === 'number' && Number.isSafeInteger(n) && n >= 0 ? n : undefined;
  };
  const input = pick('input_tokens'), cacheRead = pick('cache_read_tokens'), output = pick('output_tokens');
  if (input === undefined || cacheRead === undefined || output === undefined) return undefined;
  const thinking = pick('thinking_tokens');
  return {
    input_tokens: input,
    cache_read_input_tokens: cacheRead,
    output_tokens: output,
    ...(thinking !== undefined ? { thinking_tokens: thinking } : {}),
  };
}

const COUNT_KEYS = ['input_tokens', 'cache_read_input_tokens', 'output_tokens', 'thinking_tokens'] as const;

export function addCounts(a: Counts | undefined, b: Counts): Counts {
  const out: Counts = { ...(a ?? {}) };
  for (const key of COUNT_KEYS) {
    if (b[key] !== undefined) out[key] = (out[key] ?? 0) + b[key]!;
  }
  return out;
}

/** 本轮用量 = 本轮累计 − 上一轮之后的累计；基线缺失或倒退时不可算。 */
export function agyTurnUsageFromTotals(total: Counts | undefined, baseline: Counts | undefined): Counts | undefined {
  if (!total || !baseline) return undefined;
  const out: Counts = {};
  for (const key of COUNT_KEYS) {
    if (total[key] === undefined) continue;
    const base = baseline[key] ?? 0;
    if (total[key]! < base) return undefined;
    out[key] = total[key]! - base;
  }
  return out;
}

const EGRESS_LOG_PATTERN = /proxyconnect|context deadline exceeded|connection refused|i\/o timeout|no such host|TLS handshake timeout|network is unreachable/i;

/** 从本轮日志找出口故障：连接 Google 服务的请求失败在代理或网络层。 */
export function agyEgressFailure(log: string): string | undefined {
  for (const line of log.split('\n')) {
    if (!EGRESS_LOG_PATTERN.test(line)) continue;
    if (!/proxyconnect/i.test(line) && !/googleapis\.com|google\.com/i.test(line)) continue;
    const reason = line.match(EGRESS_LOG_PATTERN)?.[0] ?? 'network failure';
    const host = line.match(/https?:\/\/([a-z0-9.-]+)/i)?.[1];
    return `${reason}${host ? ` (${host})` : ''}`;
  }
  return undefined;
}

interface QueueItem {
  message: StreamJsonMessage;
  queuedAt: number;
  seq: number;
  prompt: string;
  turn?: ConversationTurnContext;
  interrupted?: boolean;
}

interface AgyProcess extends ConversationProcess {
  runtime: 'agy';
  workspace: string;
  ccModel?: string;
  /** /think 设的会话档位：本轮在同一模型族里换用该档位的变体。 */
  reasoningLevel?: ConversationReasoningLevel;
  agyConversationId?: string;
  /** Conversation-cumulative usage after the last settled turn of agyConversationId. */
  conversationUsage?: Counts;
  computeCostBinding?: RuntimeUsageBinding;
  currentChild?: ChildProcess;
  /** Stops the running turn's child: SIGTERM, SIGKILL after the grace period, then a bounded settle. */
  stopTurn?: () => boolean;
  currentItem?: QueueItem;
  queue: QueueItem[];
  standingAccess?: string[];
  /** What the kernel opened to this session, read again before every turn (`refreshAccess`). */
  sessionAccess?: ConversationSpawnConfig['sessionAccess'];
  allocatedWorkspaces?: ConversationSpawnConfig['allocatedWorkspaces'];
  /** The Agent's commit repositories and network domains (relay.json), as CC reads them. */
  repoAccess?: ConversationSpawnConfig['repoAccess'];
  /** The Agent's CC sessions run inside the write boundary (`agyRestricted`). */
  restricted: boolean;
  /** This session's write and network boundary (`agyAccess`), recomputed before each turn. */
  access: AgyAccess;
  /** Why the latest readback could not be turned into rules; a restricted turn is refused meanwhile. */
  accessError?: string;
}

export interface AgyConversationRuntimeOptions {
  agyBin?: string;
  timeoutMs?: number;
  startTimeoutMs?: number;
  killGraceMs?: number;
  exitDrainMs?: number;
  stateManager?: StateManager;
  /** Per-session agy CLI logs; failed turns keep theirs for diagnosis. */
  logDirectory?: string;
}

function persistedConversation(session: PersistedSession | undefined, sessionId: string):
  { conversationId: string; usage?: Counts } | undefined {
  const saved = session?.agyConversation;
  return saved && saved.sessionId === sessionId && typeof saved.conversationId === 'string' && saved.conversationId
    ? { conversationId: saved.conversationId, usage: saved.usage } : undefined;
}

export function createAgyConversationRuntime(options: AgyConversationRuntimeOptions = {}): ConversationRuntimeAdapter {
  const emitter = new EventEmitter();
  const processes = new Map<string, AgyProcess>();
  const yoloDefaultByTenant = new Map<string, boolean>();
  const yoloOverrideByTenant = new Map<string, boolean>();
  const timeoutMs = options.timeoutMs ?? DEFAULT_TIMEOUT_MS;
  const startTimeoutMs = options.startTimeoutMs ?? DEFAULT_START_TIMEOUT_MS;
  const killGraceMs = options.killGraceMs ?? DEFAULT_KILL_GRACE_MS;
  const exitDrainMs = options.exitDrainMs ?? DEFAULT_EXIT_DRAIN_MS;
  let shuttingDown = false;

  function logDirectory(): string {
    return options.logDirectory ?? join(resolveRelayDataDir(), 'agy-logs');
  }

  function effectiveYoloMode(tenantId: string): boolean {
    return yoloOverrideByTenant.get(tenantId) ?? yoloDefaultByTenant.get(tenantId) ?? false;
  }

  /** A killed or replaced process is retired: nothing it does afterwards reaches the tenant. */
  function isCurrent(tenantId: string, proc: AgyProcess): boolean {
    return processes.get(tenantId) === proc && proc.state !== 'dead';
  }

  function persistConversation(tenantId: string, proc: AgyProcess): void {
    if (!isCurrent(tenantId, proc) || !proc.agyConversationId) return;
    // The record must still be this Relay session's: /new or /reset rewrites it before a late turn ends.
    const state = options.stateManager;
    const session = state?.getSession(tenantId);
    if (!state || !session || session.sessionId !== proc.sessionId || (session.runtime && session.runtime !== 'agy')) return;
    state.updateSession(tenantId, {
      agyConversation: { sessionId: proc.sessionId, conversationId: proc.agyConversationId,
        ...(proc.conversationUsage ? { usage: proc.conversationUsage } : {}) },
      lastActivity: Date.now(),
    });
    try { state.flushStrict(); } catch { /* the debounced writer retries */ }
  }

  /**
   * agy answered a resumed turn in another conversation than the one requested. The WI binding
   * was issued for the requested conversation, which the core recognizes as the holder; the new
   * one has no confirmed holder, so its later turns carry no binding (usage stays unattributed)
   * until a /wi command binds it again. Withdrawn from the process and the saved session; the
   * runtime manager reads `runtimeUsageBinding` before each turn. The turn already running keeps
   * the binding it started with.
   */
  function withdrawUsageBinding(tenantId: string, proc: AgyProcess, replaced: string): void {
    const withdrawn = proc.computeCostBinding;
    const state = options.stateManager;
    const session = state?.getSession(tenantId);
    const saved = session?.agyWorkItemBinding?.sessionId === proc.sessionId ? session.agyWorkItemBinding : undefined;
    if (!withdrawn && !saved) return;
    proc.computeCostBinding = undefined;
    proc.usageBindingSha256 = undefined;
    if (saved) state!.updateSession(tenantId, { agyWorkItemBinding: undefined });
    console.warn(JSON.stringify({ level: 'warn', msg: 'agy replaced its conversation; WI usage binding withdrawn',
      tenantId, sessionId: proc.sessionId, replaced, conversation: proc.agyConversationId,
      workItemId: withdrawn?.work_item_id ?? saved?.usageBinding.work_item_id }));
  }

  /** A binding issued before the session had a conversation (a rollover successor) names the first one. */
  function adoptFirstConversation(tenantId: string, proc: AgyProcess): void {
    const state = options.stateManager;
    const saved = state?.getSession(tenantId)?.agyWorkItemBinding;
    if (!state || !saved || saved.sessionId !== proc.sessionId || saved.conversationId || !proc.agyConversationId) return;
    state.updateSession(tenantId, { agyWorkItemBinding: { ...saved, conversationId: proc.agyConversationId } });
  }

  function emitTo(tenantId: string, item: QueueItem, message: StreamJsonMessage): void {
    emitter.emit('response', tenantId, { ...message, correlation_id: item.turn?.correlationId, host_attempt_ref: item.turn?.hostAttemptRef });
  }

  function processNext(tenantId: string): void {
    const proc = processes.get(tenantId);
    if (shuttingDown || !proc || proc.state === 'dead' || proc.state === 'busy') return;
    const item = proc.queue.shift();
    if (!item) return;
    if (!proc.restricted || !proc.agyConversationId || !nodeWorkItemCore()) {
      runTurn(tenantId, proc, item);
      return;
    }
    // Each turn launches with the permissions the kernel holds for this conversation now.
    proc.state = 'busy';
    void refreshAccess(tenantId, proc).then(() => {
      if (isCurrent(tenantId, proc) && !shuttingDown) runTurn(tenantId, proc, item);
    });
  }

  /**
   * The kernel's write grants and allocated workspaces for the conversation this turn resumes,
   * read as CC reads them before a human turn (pool.ts `applySessionAccess`), by the retained
   * record (agy runs no process between turns). An unavailable readback keeps the last boundary.
   */
  async function refreshAccess(tenantId: string, proc: AgyProcess): Promise<void> {
    const conversation = proc.agyConversationId;
    if (!conversation) return;
    let readback: Awaited<ReturnType<typeof readRetainedSessionAccess>>;
    try {
      readback = await readRetainedSessionAccess(conversation, tenantId);
    } catch (error) {
      console.warn(JSON.stringify({ level: 'warn', tenant: tenantId,
        msg: 'agy session access readback unavailable; keeping the current boundary',
        error: error instanceof Error ? error.message : String(error) }));
      return;
    }
    proc.sessionAccess = readback.access;
    proc.allocatedWorkspaces = readback.workspaces;
    if (!proc.restricted) return;
    // A readback that cannot be turned into rules refuses the turn: the last rules may hold what it revoked.
    try {
      proc.access = agyAccess(proc);
      proc.accessError = undefined;
    } catch (error) {
      proc.accessError = error instanceof Error ? error.message : String(error);
    }
  }

  function runTurn(tenantId: string, proc: AgyProcess, item: QueueItem): void {
    // An internal delivery runs only in the conversation it was routed to; agy may have named
    // another one on a turn that ran after the delivery was queued.
    if (refuseMisroutedTurn(item.message, proc, tenantId)) {
      proc.state = 'idle';
      emitTo(tenantId, item, refusedTurnResult(item.turn?.hostAttemptRef));
      processNext(tenantId);
      return;
    }
    proc.state = 'busy';
    proc.lastActivity = Date.now();
    proc.currentItem = item;
    item.turn?.arrival?.accepted();
    // An internal delivery counts as received once agy has started the turn (its init passed the
    // sandbox check); any failure before that leaves it to be offered again.
    const delivery = (outcome: InternalTurnOutcome) => settleInternalTurn(item.message, outcome);

    const model = agyEffectiveModel(proc.ccModel, proc.reasoningLevel);
    const launchedConversation = proc.agyConversationId;
    const baseline = launchedConversation ? proc.conversationUsage : undefined;
    const logDir = join(logDirectory(), createHash('sha256').update(tenantId).digest('hex').slice(0, 16), proc.sessionId);
    const logFile = join(logDir, `turn-${Date.now()}-${item.seq}.log`);
    const access = launchedConversation ? [] : [
      ...sharedAccessPromptLines(proc.workspace, proc.standingAccess), ...(proc.standingAccess ?? []).map(standingAccessPromptLine)];
    const prompt = access.length ? `${access.join('\n')}\n\n${item.prompt}` : item.prompt;

    // The same permissions as the Agent's CC session (agyRestricted), never widened by /yolo.
    const yolo = !proc.restricted;
    let sandboxRefused = false;
    let child: ChildProcess;
    try {
      if (proc.restricted && proc.accessError) {
        throw new Error(`the session's permissions could not be applied (${proc.accessError}); the turn was not run`);
      }
      mkdirSync(logDir, { recursive: true, mode: 0o700 });
      const command = buildAgyCommand({
        agyBin: options.agyBin, workspace: proc.workspace, prompt, model, logFile,
        conversationId: launchedConversation, additionalWritableRoots: proc.access.writeRoots,
        deniedWrites: proc.access.deniedWrites, domains: proc.access.domains, yoloMode: yolo,
      });
      child = cpSpawn(command.command, command.args, { cwd: command.cwd, stdio: ['ignore', 'pipe', 'pipe'],
        env: buildAgyEnv(proc.computeCostBinding) });
    } catch (error) {
      settle({ exitCode: 1, errorMessage: `agy failed to start: ${error instanceof Error ? error.message : String(error)}` });
      return;
    }
    forgetNativeProofOnExit(child);
    proc.currentChild = child;

    let finished = false;
    let started = false;
    let stderr = '';
    let observedModel: string | undefined;
    let observedConversation: string | undefined;
    let result: Record<string, unknown> | undefined;
    let stepUsage: Counts | undefined;
    let stepUsageInvalid = false;
    /** A stream line Relay could not process: the turn's outcome is not proven. */
    let streamFault = false;
    // A finished step without usage: the steps' sum no longer covers the turn.
    let stepUsageIncomplete = false;
    let timeoutKind: 'start' | 'idle' | undefined;
    const stepText = new Map<number, string>();
    const published = new Set<number>();
    let idleTimer: NodeJS.Timeout | undefined;
    let killTimer: NodeJS.Timeout | undefined;
    let forceTimer: NodeJS.Timeout | undefined;
    let drainTimer: NodeJS.Timeout | undefined;

    /** Returns whether SIGTERM was delivered; escalation is armed once and survives a refused SIGTERM. */
    const stop = (): boolean => {
      if (finished) return true;
      let sent = false;
      try { sent = child.kill('SIGTERM'); } catch { /* ignore */ }
      if (killTimer) return sent;
      killTimer = setTimeout(() => {
        if (finished) return;
        try { child.kill('SIGKILL'); } catch { /* ignore */ }
        forceTimer = setTimeout(() => {
          if (finished) return;
          console.warn(JSON.stringify({ level: 'warn', msg: 'agy child reported no exit after SIGKILL; turn settled as stopped',
            pid: child.pid }));
          finish(exitCode ?? 137);
        }, killGraceMs);
      }, killGraceMs);
      return sent;
    };
    proc.stopTurn = stop;
    const terminate = (kind: 'start' | 'idle'): void => {
      if (finished) return;
      timeoutKind = kind;
      stop();
    };
    const armTimer = (): void => {
      if (idleTimer) clearTimeout(idleTimer);
      idleTimer = setTimeout(() => terminate(started ? 'idle' : 'start'), started ? timeoutMs : startTimeoutMs);
    };
    armTimer();

    /** Agent text of a finished step that was followed by more work is commentary, not the reply. */
    const publishStepText = (beforeStep: number): void => {
      for (const [index, text] of stepText) {
        if (index >= beforeStep || published.has(index) || !text.trim()) continue;
        published.add(index);
        emitTo(tenantId, item, { type: 'work_process',
          process_event: publicCommentaryEvent(`agy:${observedConversation ?? 'conversation'}:${index}`, text.trim()) });
      }
    };

    const rl = child.stdout ? createInterface({ input: child.stdout }) : null;
    // Any fault in reading one line fails this turn only; it never escapes into the Relay process.
    rl?.on('line', (line: string) => {
      try { handleLine(line); } catch (error) {
        streamFault = true;
        stderr += `agy stream line not processed: ${error instanceof Error ? error.message : String(error)}\n`;
      }
    });
    function handleLine(line: string): void {
      const trimmed = line.trim();
      if (!trimmed || finished || !isCurrent(tenantId, proc)) return;
      const event = parseAgyEvent(trimmed);
      if (!event) {
        stderr += `Invalid agy stream-json: ${trimmed.slice(0, 200)}\n`;
        return;
      }
      proc.lastActivity = Date.now();
      if (event.event === 'init') {
        started = true;
        if (!yolo && !agySandboxEnabled(logFile)) {
          // No tool has run yet: stop before the model can act without the sandbox.
          sandboxRefused = true;
          delivery('refused');
          stop();
          return;
        }
        delivery('consumed');
        const init = agyObject(event.init) ?? {};
        if (typeof init.model === 'string') observedModel = init.model;
        if (typeof event.conversation_id === 'string' && event.conversation_id) {
          observedConversation = event.conversation_id;
          if (child.pid) {
            try { observeNativeSession(child.pid, observedConversation, tenantId, 'agy', proc.sessionId); } catch (error) {
              console.warn(JSON.stringify({ level: 'warn', msg: 'agy native session proof not written',
                reason: error instanceof Error ? error.message : String(error) }));
            }
          }
          // Outlives this turn's process: an idle conversation stays recognizable to /wi.
          try { retainNativeConversation(observedConversation, tenantId, proc.sessionId); } catch (error) {
            console.warn(JSON.stringify({ level: 'warn', msg: 'agy retained conversation record not written',
              reason: error instanceof Error ? error.message : String(error) }));
          }
          if (proc.agyConversationId !== observedConversation) {
            // A new conversation (or agy replaced the requested one): earlier totals do not apply.
            const replaced = proc.agyConversationId;
            proc.agyConversationId = observedConversation;
            proc.conversationUsage = undefined;
            if (replaced) withdrawUsageBinding(tenantId, proc, replaced);
            else adoptFirstConversation(tenantId, proc);
            persistConversation(tenantId, proc);
          }
        }
        emitter.emit('turn-accepted', tenantId);
        armTimer();
        return;
      }
      if (event.event === 'step_update') {
        started = true;
        armTimer();
        const step = agyObject(event.step_update) ?? {};
        const index = typeof step.step_index === 'number' ? step.step_index : -1;
        if (step.step_type === 'agent_response') {
          if (typeof step.text_delta === 'string') stepText.set(index, (stepText.get(index) ?? '') + step.text_delta);
          if (step.state === 'DONE' && step.usage === undefined) stepUsageIncomplete = true;
          else if (step.state === 'DONE') {
            const counts = agyUsageCounts(step.usage);
            if (counts) stepUsage = addCounts(stepUsage, counts);
            else stepUsageInvalid = true;
          }
          return;
        }
        if (step.step_type === 'tool') {
          publishStepText(index);
          for (const processEvent of publicWorkEvents('agy', event)) {
            emitTo(tenantId, item, { type: 'work_process', process_event: processEvent });
          }
        }
        return;
      }
      const settled = event.event === 'result' ? agyObject(event.result) : undefined;
      if (settled) {
        result = settled;
        if (!observedConversation && typeof settled.conversation_id === 'string' && settled.conversation_id) {
          observedConversation = settled.conversation_id;
        }
      }
    }
    child.stderr?.on('data', (chunk: Buffer) => { stderr += chunk.toString(); });
    child.on('error', (error: Error) => finish(1, `agy failed to start: ${error.message}`));
    // Node may report exit before stdout is read to its end, and the final result
    // is the last line: settle once stdout closes, or after a bounded drain.
    let exitCode: number | undefined;
    let stdoutClosed = !rl;
    rl?.on('close', () => { stdoutClosed = true; if (exitCode !== undefined) finish(exitCode); });
    child.on('exit', (code, signal) => {
      exitCode = code ?? (signal ? 1 : 0);
      if (stdoutClosed || timeoutKind !== undefined) finish(exitCode);
      else drainTimer = setTimeout(() => finish(exitCode!), exitDrainMs);
    });

    function finish(exitCode: number, spawnError?: string): void {
      if (finished) return;
      finished = true;
      delivery('refused');  // No effect once the turn started.
      if (idleTimer) clearTimeout(idleTimer);
      if (killTimer) clearTimeout(killTimer);
      if (forceTimer) clearTimeout(forceTimer);
      if (drainTimer) clearTimeout(drainTimer);
      if (proc.stopTurn === stop) proc.stopTurn = undefined;
      rl?.close();
      if (!isCurrent(tenantId, proc)) {
        // Killed or replaced while this turn ran: its outcome belongs to no current session.
        if (proc.currentChild === child) proc.currentChild = undefined;
        return;
      }

      let log = '';
      try { log = readFileSync(logFile, 'utf8'); } catch { /* agy may not have written it */ }
      const status = typeof result?.status === 'string' ? result.status : undefined;
      const failed = !!spawnError || exitCode !== 0 || status !== 'SUCCESS' || timeoutKind !== undefined || streamFault
        || sandboxRefused;
      const egress = failed && !item.interrupted ? agyEgressFailure(log) : undefined;

      let errorMessage: string | undefined;
      if (item.interrupted) errorMessage = undefined;
      else if (sandboxRefused) errorMessage = AGY_SANDBOX_REFUSAL;
      else if (egress) {
        errorMessage = `agy egress unavailable: ${egress}; the request was not retried through another egress`;
      } else if (timeoutKind === 'start') {
        errorMessage = `agy conversation runtime timed out after ${startTimeoutMs}ms`;
      } else if (timeoutKind === 'idle') {
        errorMessage = `agy conversation runtime timed out after ${timeoutMs}ms`;
      } else if (spawnError) {
        errorMessage = spawnError;
      } else if (failed) {
        const native = typeof result?.error === 'string' && result.error ? result.error
          : stderr.trim() || `agy exited with code ${exitCode}`;
        errorMessage = native;
      }
      if (!failed) {
        try { rmSync(logFile, { force: true }); } catch { /* best effort */ }
      }

      // Usage: this turn's steps; else the cumulative delta; a fresh conversation's total is the turn.
      const total = agyUsageCounts(result?.usage);
      const sameConversation = observedConversation !== undefined && observedConversation === proc.agyConversationId;
      let turnUsage: Counts | undefined;
      if (stepUsage && !stepUsageInvalid && !stepUsageIncomplete) turnUsage = stepUsage;
      else if (!stepUsageInvalid && launchedConversation && launchedConversation === observedConversation) {
        turnUsage = agyTurnUsageFromTotals(total, baseline);
      } else if (!stepUsageInvalid && !launchedConversation && observedConversation) turnUsage = total;
      if (sameConversation) {
        proc.conversationUsage = total;
        persistConversation(tenantId, proc);
      }

      const denied = Array.isArray(result?.denied_actions)
        ? (result!.denied_actions as unknown[]).flatMap((entry) => {
          const row = entry && typeof entry === 'object' ? entry as Record<string, unknown> : {};
          return typeof row.action === 'string' ? [{ action: row.action,
            display_name: typeof row.display_name === 'string' ? row.display_name : row.action }] : [];
        })
        : [];
      const response = typeof result?.response === 'string' ? result.response.trim() : '';
      const finalStep = Math.max(-1, ...stepText.keys());
      publishStepText(response ? finalStep : finalStep + 1);
      const deniedNotice = denied.length
        ? `agy 无头模式拒绝了以下工具权限：${denied.map((d) => `${d.action}（${d.display_name}）`).join('、')}。`
          + '本运行时只放行工作区内写文件；其余需批准的操作（如执行命令、写工作区外）不会执行。'
        : '';
      if (!errorMessage && !response && denied.length) {
        errorMessage = `agy permission denied: ${denied.map((d) => d.action).join(', ')}`;
      }

      const usedModel = observedModel ?? model;
      const nativeModelUsage: NativeModelUsage | undefined = turnUsage
        ? { schema: 'pulse_native_model_usage/v1', runtime: 'agy',
          models: [{ model: usedModel, provider: null, usage: turnUsage }] }
        : undefined;
      const resultMetadata = {
        runtime: 'agy' as const,
        model: usedModel,
        ...(nativeModelUsage ? { native_model_usage: nativeModelUsage } : {}),
        ...(proc.agyConversationId ? { agy_conversation_id: proc.agyConversationId } : {}),
        ...(denied.length ? { denied_actions: denied } : {}),
        ...(item.turn?.hostAttemptRef ? { host_attempt_ref: item.turn.hostAttemptRef } : {}),
      };

      proc.currentChild = undefined;
      proc.currentItem = undefined;
      proc.state = 'idle';
      proc.lastActivity = Date.now();
      const exitMetadata = item.turn?.hostAttemptRef ? { hostAttemptRef: item.turn.hostAttemptRef } : undefined;

      if (item.interrupted) {
        emitTo(tenantId, item, { type: 'result', subtype: 'interrupted', interrupted: true, is_error: true,
          ...(turnUsage ? { usage: turnUsage } : {}), ...resultMetadata });
        emitter.emit('exit', tenantId, 0, exitMetadata);
      } else if (errorMessage) {
        emitTo(tenantId, item, { type: 'result', is_error: true, ...(turnUsage ? { usage: turnUsage } : {}), ...resultMetadata });
        const error = new Error(redactRuntimeDiagnostic(errorMessage) || errorMessage);
        if (item.turn?.hostAttemptRef) Object.assign(error, { hostAttemptRef: item.turn.hostAttemptRef });
        emitter.emit('error', tenantId, error);
        emitter.emit('exit', tenantId, exitCode === 0 ? 1 : exitCode, exitMetadata);
      } else {
        const text = [response, deniedNotice].filter(Boolean).join('\n\n');
        emitTo(tenantId, item, { type: 'assistant', message: { content: [{ type: 'text', text }] }, content: text,
          delivery_phase: 'final', event_id: `agy:${proc.agyConversationId ?? 'conversation'}:${finalStep}`,
          ...(proc.agyConversationId ? { agy_conversation_id: proc.agyConversationId } : {}) });
        emitTo(tenantId, item, { type: 'result', ...(turnUsage ? { usage: turnUsage } : {}), ...resultMetadata });
        emitter.emit('exit', tenantId, 0, exitMetadata);
      }
      processNext(tenantId);
    }

    function settle(input: { exitCode: number; errorMessage: string }): void {
      delivery('refused');
      proc.currentItem = undefined;
      proc.state = 'idle';
      emitTo(tenantId, item, { type: 'result', is_error: true, runtime: 'agy' });
      const error = new Error(input.errorMessage);
      if (item.turn?.hostAttemptRef) Object.assign(error, { hostAttemptRef: item.turn.hostAttemptRef });
      emitter.emit('error', tenantId, error);
      emitter.emit('exit', tenantId, input.exitCode, item.turn?.hostAttemptRef ? { hostAttemptRef: item.turn.hostAttemptRef } : undefined);
      processNext(tenantId);
    }
  }

  const adapter: ConversationRuntimeAdapter = {
    id: 'agy',
    admitsTurns: true,
    fencesInternalTurns: true,
    async spawn(config: ConversationSpawnConfig): Promise<ConversationProcess> {
      const existing = processes.get(config.tenantId);
      if (existing && existing.state !== 'dead') throw new Error(`Process already exists for tenant ${config.tenantId}`);
      const refused = agyIsolationRefusal(config.linuxUser);
      if (refused) throw new Error(refused);
      if (!isAgyInstalled(options.agyBin)) throw new Error(`agy launcher not found: ${resolveAgyBin(options.agyBin)}`);
      const standing = await standingAccessFor(config.harnessAgentId);
      if (config.codexYoloDefault === true) yoloDefaultByTenant.set(config.tenantId, true);
      else yoloDefaultByTenant.delete(config.tenantId);
      const saved = persistedConversation(options.stateManager?.getSession(config.tenantId), config.sessionId);
      const proc: AgyProcess = {
        tenantId: config.tenantId,
        runtime: 'agy',
        state: 'idle',
        sessionId: config.sessionId,
        usageBindingSha256: config.computeCostBinding?.binding_sha256,
        computeCostBinding: config.computeCostBinding,
        ccModel: config.ccModel,
        reasoningLevel: config.reasoningLevel,
        agyConversationId: saved?.conversationId,
        conversationUsage: saved?.usage,
        lastActivity: Date.now(),
        spawnedAt: Date.now(),
        restartCount: existing ? existing.restartCount + 1 : 0,
        restartWindowStart: existing?.restartWindowStart ?? Date.now(),
        queue: [],
        workspace: config.workspace,
        standingAccess: standing.map((row) => row.path),
        sessionAccess: config.sessionAccess,
        allocatedWorkspaces: config.allocatedWorkspaces,
        repoAccess: config.repoAccess,
        restricted: agyRestricted(config),
        // Malformed repository access refuses the start, as it refuses a restricted CC session.
        access: agyRestricted(config) ? agyAccess(config) : { writeRoots: [], deniedWrites: [], domains: [] },
      };
      processes.set(config.tenantId, proc);
      // A conversation resumed from state (a Relay restart or upgrade, /kill, /wi switch) is retained
      // again now, not only at its next turn's init: /wi recognizes the idle conversation at once.
      if (saved?.conversationId) {
        try { retainNativeConversation(saved.conversationId, config.tenantId, config.sessionId); } catch (error) {
          console.warn(JSON.stringify({ level: 'warn', msg: 'agy retained conversation record not written',
            reason: error instanceof Error ? error.message : String(error) }));
        }
      }
      return proc;
    },
    async send(tenantId: string, message: StreamJsonMessage, turn?: ConversationTurnContext): Promise<void> {
      const proc = processes.get(tenantId);
      if (!proc || proc.state === 'dead') throw new Error(`No active agy conversation process for tenant ${tenantId}`);
      if (shuttingDown) throw new Error('Relay is shutting down');
      const item: QueueItem = { message, queuedAt: Date.now(), seq: turn?.arrival?.seq ?? nextTurnSeq(),
        prompt: agyMessageToPrompt(message), turn };
      proc.queue.push(item);
      turn?.arrival?.accepted();
      if (proc.state !== 'busy') processNext(tenantId);
    },
    async abortTurn(tenantId: string): Promise<void> {
      const proc = processes.get(tenantId);
      const child = proc?.currentChild;
      const item = proc?.currentItem;
      if (!proc || proc.state !== 'busy' || !child || !item) throw new Error(`No active agy turn for tenant ${tenantId}`);
      item.interrupted = true;
      // The turn stays busy until the child exits; SIGKILL and a bounded settle follow an ignored SIGTERM.
      if (!(proc.stopTurn ?? (() => child.kill('SIGTERM')))()) {
        item.interrupted = false;
        throw new Error(`Failed to interrupt agy turn for tenant ${tenantId}`);
      }
    },
    async setModel(tenantId: string, route: ConversationModelRoute): Promise<void> {
      const proc = processes.get(tenantId);
      if (!proc) throw new Error(`No active agy conversation process for tenant ${tenantId}`);
      if (!isAgyModel(route.model)) throw new Error(`agy does not offer model ${route.model}`);
      // agy switches models inside one conversation; the next turn launches with it.
      // An explicitly chosen model carries its own level, so the session level no longer applies.
      proc.ccModel = route.model;
      proc.reasoningLevel = undefined;
    },
    async listModels(): Promise<ConversationRuntimeModelOption[]> {
      return agyModelOptions();
    },
    runtimeUsageBinding(tenantId: string): RuntimeUsageBinding | undefined {
      return processes.get(tenantId)?.computeCostBinding;
    },
    bindRuntimeUsage(tenantId: string, binding: RuntimeUsageBinding): void {
      const proc = processes.get(tenantId);
      if (!proc) throw new Error(`No active agy conversation process for tenant ${tenantId}`);
      // The next turn's process and Host receipt carry it; a running turn keeps its own.
      proc.computeCostBinding = binding;
      proc.usageBindingSha256 = binding.binding_sha256;
    },
    async setThinkingLevel(tenantId: string, level: ConversationReasoningLevel): Promise<void> {
      const proc = processes.get(tenantId);
      if (!proc) throw new Error(`No active agy conversation process for tenant ${tenantId}`);
      const base = agyEffectiveModel(proc.ccModel);
      if (!agyModelForLevel(base, level)) throw new Error(`agy model ${base} has no ${level} variant`);
      proc.reasoningLevel = level;
    },
    async listThinkingLevels(tenantId: string): Promise<ConversationReasoningLevel[]> {
      return agyModelLevels(processes.get(tenantId)?.ccModel);
    },
    async kill(tenantId: string): Promise<void> {
      const proc = processes.get(tenantId);
      if (!proc) return;
      proc.state = 'dead';
      proc.queue = [];
      // Retired first, so the child's late output and exit reach nobody; it is still stopped for certain.
      processes.delete(tenantId);
      proc.stopTurn?.();
    },
    async killAll(): Promise<void> {
      await Promise.all(Array.from(processes.keys(), (tenantId) => adapter.kill(tenantId)));
    },
    get(tenantId: string): ConversationProcess | undefined {
      return processes.get(tenantId);
    },
    getAll(): ConversationProcess[] {
      return Array.from(processes.values());
    },
    getStatus(): ConversationStatusInfo[] {
      return Array.from(processes.values()).map((proc) => ({
        tenantId: proc.tenantId, runtime: 'agy', state: proc.state, pid: proc.currentChild?.pid,
        lastActivity: proc.lastActivity, spawnedAt: proc.spawnedAt, restartCount: proc.restartCount,
      }));
    },
    setYoloMode(tenantId: string, enabled: boolean): void { yoloOverrideByTenant.set(tenantId, enabled); },
    clearYoloMode(tenantId: string): void { yoloOverrideByTenant.delete(tenantId); },
    setYoloDefaultMode(tenantId: string, enabled: boolean): void {
      if (enabled) yoloDefaultByTenant.set(tenantId, true);
      else yoloDefaultByTenant.delete(tenantId);
    },
    getYoloOverrideMode(tenantId: string): boolean | undefined { return yoloOverrideByTenant.get(tenantId); },
    getYoloMode(tenantId: string): boolean { return effectiveYoloMode(tenantId); },
    on(event: string, handler: (...args: unknown[]) => void): void {
      // eslint-disable-next-line @typescript-eslint/no-explicit-any
      emitter.on(event, handler as (...args: any[]) => void);
    },
    emitShutdown(): Set<string> {
      shuttingDown = true;
      const notified = new Set<string>();
      let persisted = false;
      for (const [tenantId, proc] of processes) {
        const wasBusy = proc.state === 'busy';
        if (wasBusy) {
          const prompt = proc.currentItem?.prompt.trim();
          options.stateManager?.updateSession(tenantId, { runtime: 'agy', wasBusyAtShutdown: true,
            lastActivity: Date.now(), status: 'active', ...(prompt ? { interruptedUserMessage: prompt } : {}) });
          persisted = true;
        }
        if (proc.queue.length > 0) {
          const queued = proc.queue;
          proc.queue = [];
          recordQueuedAtShutdown(options.stateManager, tenantId, 'agy', queued);
          persisted = true;
        }
        emitter.emit('session-end', tenantId, { type: 'relay-shutdown', wasBusy } satisfies SessionEndReason);
        notified.add(tenantId);
      }
      if (persisted) options.stateManager?.flush();
      return notified;
    },
    async drainBusySessions(drainTimeoutMs: number): Promise<Set<string>> {
      const deadline = Date.now() + drainTimeoutMs;
      while (Date.now() < deadline) {
        if (![...processes.values()].some((proc) => proc.state === 'busy')) return new Set();
        await new Promise((resolve) => setTimeout(resolve, 200));
      }
      return new Set([...processes].filter(([, proc]) => proc.state === 'busy').map(([tenantId]) => tenantId));
    },
    dispose(): void {
      void adapter.killAll();
      yoloDefaultByTenant.clear();
      yoloOverrideByTenant.clear();
      emitter.removeAllListeners();
    },
  } as ConversationRuntimeAdapter;

  return adapter;
}
