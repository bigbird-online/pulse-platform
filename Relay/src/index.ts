import {nodeWorkItemCore} from './native-work-item-command.js';
import {sweepNativeSessionProofs} from './native-core-runtime.js';
import {warmStandingAccess} from './standing-access.js';
import {rolloverNativeSession,nativeRolloverActiveCount} from './native-session-rollover.js';
import {adoptAgentRolloverRequests,agentRolloverRequestDir} from './agent-rollover-request.js';
import {adoptAgentSwitchRequests,agentSwitchRequestDir} from './agent-switch-request.js';
import {runAgentSwitch,settleWiSwitch} from './native-work-item-command.js';
import {switchInProgress} from './native-wi-switch.js';
import {acquireConversation} from './conversation-acquisition.js';
import {channelConversationAgent} from './channel-conversation.js';
import { managedRelayPath } from './managed-paths.js';
// Relay Gateway — entry point

import type { RelayConfig, Scheduler, StreamJsonMessage, StateManager } from './types.js';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { agentRepoAccess, harnessPrincipalForAgent, validateConfig } from './types.js';
import { agentDisplayName, createCollabDelivery, nativeIds, sessionRef } from './collab-delivery.js';
import { AgentJobError, applyAgentJobs, type AgentJobRequest } from './agent-jobs.js';
import { createAgentJobsVerifier } from './agent-jobs-auth.js';
import { createCollabLedgerWatch, ledgerBinding, type CollabLedgerWatch } from './collab-ledger-watch.js';
import { resolveConversationRuntimeId as resolveAgentRuntimeId } from './conversation-runtime.js';
import { isPoolKeyPendingInteractive } from './pending-interactive.js';
import { traceTurnEnd, shouldTraceTurnEnd } from './relay-request-trace.js';
import { resolveConfigPath, resolveCronJobsPath, resolveMeteringDir, resolveRelayDataDir } from './config-paths.js';
import { resolvePulseRuntimePriority } from './codex-defaults.js';
import { buildRelayInteractiveLineage } from './relay-interactive-lineage.js';
import { loadPlacementRuntimeContext, type PlacementRuntimePaths } from './node-placement.js';
import { parseConversationPoolKey } from './conversation-address.js';
import { platformSettings, startPlatformBot } from './platform-bot.js';
import { createTelegramApiBot } from './channels/telegram.js';
import { persistQueuedTurns, recordQueuedAtShutdown, unrecordPendingTurn } from './shutdown-interrupts.js';
import { switchBarrierPath } from './switch-barrier.js';

export { createPool } from './pool.js';
export { createConversationRuntimeManager, createPoolConversationAdapter, resolveConversationRuntimeId, resolveConversationRuntimePreference } from './conversation-runtime.js';
export { createCodexConversationRuntime, buildCodexCommand, codexMessageToPrompt } from './codex-conversation-runtime.js';
export { createCcSdkConversationRuntime, buildSdkEngineEnv, resolveProviderEnv } from './cc-sdk-conversation-runtime.js';
export { createPiConversationRuntime, buildPiCommand, loadPiRuntimeManifest } from './pi-conversation-runtime.js';
export { createRouter } from './router.js';
export { collectRichDocCodeBlocks, compileRichDoc, renderRichDocMarkdown, richDocToPlainText, sanitizeLinkTarget, splitRichDoc, validateRichDoc } from './rich-doc.js';
export { compileFeishuPayloads, compileTelegramPayloads, isRichDocRolloutEnabled, resolveRichDocChannelProfiles } from './channel-rich-rendering.js';
export type { RichBlock, RichDoc, RichDocSplitOptions, RichInline, RichListItem } from './rich-doc.js';
export type { ChannelRichDocProfile, ChannelRenderingObservation, FeishuPayloadCompilation, RichDocChannelProfiles, RichDocRolloutMode, TelegramPayloadCompilation } from './channel-rich-rendering.js';
export { createTelegramBots, createTelegramBot, splitMessage, prefixMessage } from './channels/telegram.js';
export { createFeishuBots, buildMarkdownCard, splitCardText, peekPendingPairingForHttp, consumePendingPairingForHttp, approveFeishuPairingViaHttp } from './channels/feishu.js';
export { startRelayHttpServer, startRelayHttpServerIfEnabled, resolveRelayHttpPort, resolveRelayHttpHost } from './http-server.js';
export { createMetering } from './metering.js';
export { loadPricingConfig, calculateCost } from './pricing.js';
export { createStateManager } from './state.js';
export { resolveConfigPath, resolveCronJobsPath, resolveMeteringDir, resolvePulseRoot } from './config-paths.js';
export { createScheduler } from './scheduler.js';
export { JobTerminalRegistry } from './job-terminal-registry.js';
export { getAllMenuCommands, registerCommandMenu } from './command-menu.js';
export type { Pool, SpawnConfig, PoolOptions, CCProcess, ProcessState, StreamJsonMessage, AgentConfig, UserConfig, RelayConfig, StateManager, SessionEndReason, PersistedState, PersistedSession, MenuCommand, ScheduledJob, JobResult, ConversationRuntimeId, ConversationRuntimeFallbackEvent, ChannelType, FeishuAgentCredentials } from './types.js';
export type { ConversationRuntimeAdapter, ConversationRuntimeManager, ConversationProcess, ConversationProcessState, ConversationMessage, ConversationAttachment, ConversationSpawnConfig, ConversationStatusInfo } from './conversation-runtime.js';
export type { CodexConversationRuntimeOptions, CodexCommandSpec } from './codex-conversation-runtime.js';
export type { CcSdkRuntimeOptions, CcSdkProviderMode } from './cc-sdk-conversation-runtime.js';
export type { PiConversationRuntimeAdapter, PiConversationRuntimeOptions, PiRuntimeManifest, PiIsolationProfile, PiCommandSpec } from './pi-conversation-runtime.js';
export { RUNTIME_USAGE_BINDING_ENV, RUNTIME_USAGE_BINDING_SCHEMA, runtimeUsageBindingEnvironment, validateRuntimeUsageBinding } from './runtime-usage-binding.js';
export type { RuntimeUsageBinding, RuntimeUsageRuntime, RuntimeProcurementSource } from './runtime-usage-binding.js';
export type { Router } from './router.js';
export type { TelegramBotInstance, TelegramBotDeps } from './channels/telegram.js';
export type { FeishuBotInstance, FeishuBotDeps } from './channels/feishu.js';
export type { Metering, MeteringOptions, LowBalanceEvent } from './metering.js';
export type { PricingConfig, PricingEntry, CostResult } from './pricing.js';
export type { Scheduler } from './types.js';
export type { ModelCatalog, ModelCatalogEntry, ModelCatalogConfig, ModelValidationResult, ModelListEntry, ProviderDef } from './model-catalog.js';

export function resolveMeteringPrincipal(
  router: Pick<import('./router.js').Router, 'getAgents'>,
  poolKey: string,
): string | undefined {
  const address = parseConversationPoolKey(poolKey);
  if (!address) return undefined;
  const { userId, agentId } = address;
  const agent = router.getAgents(userId).find(candidate => candidate.id === agentId);
  return agent ? harnessPrincipalForAgent(agent) : undefined;
}

/** Feishu and local-only hosts also need an observable scheduler. */
export function shouldStartScheduler(
  telegramBotCount: number,
  feishuBotCount: number,
  placementEnabled: boolean,
  localOnlyAgentConfigured = false,
): boolean {
  return telegramBotCount > 0 || feishuBotCount > 0 || placementEnabled || localOnlyAgentConfigured;
}

/**
 * Start Relay gateway.
 * Reads relay.json config, validates config, launches TG bots and Feishu WS clients.
 */
export async function startRelay(options?: {
  configPath?: string;
  meteringDir?: string;
  stateManager?: StateManager;
  cronJobsPath?: string;
  modelCatalogPath?: string;
  nodePlacement?: Partial<PlacementRuntimePaths>;
}): Promise<{ stop: () => void; notifyShutdown?: () => Promise<void>; drainBusySessions?: (timeoutMs: number) => Promise<Set<string>>; reloadScheduler?: () => void }> {
  // 路径推导唯一真源在 config-paths.ts——三入口（本文件 / provision-agent / relay-jobs）
  // 曾各写一份默认值，裸跑发散。改默认值只改那一处。
  const configPath = resolveConfigPath(options?.configPath);

  const meteringDir = resolveMeteringDir(options?.meteringDir);

  const cronJobsPath = resolveCronJobsPath(options?.cronJobsPath);

  // Compatibility is explicit: when none of the four placement settings are
  // present the legacy single-node runtime remains unchanged. Once any setting
  // is installed all four become mandatory and startup is fail-closed.
  const placementContext = loadPlacementRuntimeContext(options?.nodePlacement);
  placementContext?.assertPreflight(configPath, cronJobsPath);

  const modelCatalogPath = managedRelayPath('models', options?.modelCatalogPath
    ?? process.env['RELAY_MODEL_CATALOG_PATH'])
    ?? undefined;

  const { createPool } = await import('./pool.js');
  const { createConversationRuntimeManager, createPoolConversationAdapter } = await import('./conversation-runtime.js');
  const { createCodexConversationRuntime } = await import('./codex-conversation-runtime.js');
  const { createCcSdkConversationRuntime } = await import('./cc-sdk-conversation-runtime.js');
  const { createPiConversationRuntime, loadPiRuntimeManifest } = await import('./pi-conversation-runtime.js');
  const { validateConfiguredPiProfiles } = await import('./pi-runtime-config.js');
  const { createRouter } = await import('./router.js');
  const { createTelegramBots } = await import('./channels/telegram.js');
  const { createFeishuBots } = await import('./channels/feishu.js');
  const { createMetering } = await import('./metering.js');
  const { createScheduler } = await import('./scheduler.js');
  const { createModelCatalog } = await import('./model-catalog.js');
  const { readFileSync } = await import('node:fs');

  // P0-006: Load model catalog for provider model routing
  const modelCatalog = modelCatalogPath ? createModelCatalog(modelCatalogPath) : undefined;

  // Web Terminal manager for /cc browser-based CC takeover
  const { TerminalManager } = await import('./web-terminal.js');
  const terminalManager = new TerminalManager();

  const router = createRouter(configPath);
  const piManifestPath = managedRelayPath('piManifest', process.env['RELAY_PI_RUNTIME_MANIFEST_PATH']?.trim());
  const piManifest = piManifestPath ? loadPiRuntimeManifest(piManifestPath) : undefined;
  const configuredPiAgents = [...router.getAllUsers().flatMap(user =>
    router.getAgents(user.userId).filter(agent => resolveAgentRuntimeId(agent) === 'pi'),
  ), ...router.getAllFeishuBots().map(bot => bot.agent).filter(agent => resolveAgentRuntimeId(agent) === 'pi')];
  const configuredPiProfiles = configuredPiAgents.map(agent => {
    const runtimeDefault = router.getConversationRuntimeDefault?.('pi');
    const route = agent.conversationRuntime ?? (runtimeDefault ? { ...runtimeDefault, runtime: 'pi' as const } : undefined);
    if (
      route?.runtime !== 'pi'
      || !route.model?.provider
      || !route.model.model
      || !route.model.reasoning
      || !route.isolationProfile
    ) throw new Error(`Configured Pi agent ${agent.id} has an incomplete runtime route`);
    return route.isolationProfile;
  });
  if (configuredPiProfiles.length > 0) {
    validateConfiguredPiProfiles(piManifest, configuredPiProfiles, {
      requireComputeCostHost: false,
    });
  }

  // CC 用量测量与 session WI MCP 复用 WI 绑定 Pi profile 已审定的 ComputeCost/SCL Host 资源；不存在唯一配置则二者均不可用。
  const ccUsageHost = (() => {
    if (!piManifest) return undefined;
    const preferred = configuredPiProfiles.map(name => piManifest.profiles[name]).filter(profile => !!profile);
    const candidates = (preferred.length ? preferred : Object.values(piManifest.profiles))
      .map(profile => profile.computeCostHost).filter(host => !!host);
    const distinct = new Map(candidates.map(host => [JSON.stringify([
      host!.python, host!.pythonPath, host!.sclExecutable, host!.sclHarnessRoot, host!.sclKernelRoot]), host!]));
    if (distinct.size !== 1) return undefined;
    const host = [...distinct.values()][0]!;
    return { python: host.python, pythonPath: host.pythonPath, sclExecutable: host.sclExecutable,
      sclHarnessRoot: host.sclHarnessRoot, sclKernelRoot: host.sclKernelRoot };
  })();
  const { resolveCcSessionWiMcp } = await import('./cc-session-wi-mcp.js');
  // The session WI declaration needs only the canonical Python and this release's
  // ComputeCost; under the native core it no longer depends on the retired
  // ComputeCost/SCL host profile that gated CC usage measurement.
  const nativeCore = nodeWorkItemCore();
  // Proofs of processes that ended while no Relay watched them would name a
  // later process under the same PID; remove them before any session starts.
  sweepNativeSessionProofs();
  const sessionWiHost = ccUsageHost ?? (nativeCore ? {
    python: nativeCore.python,
    pythonPath: join(dirname(dirname(dirname(fileURLToPath(import.meta.url)))), 'ComputeCost'),
  } : undefined);
  // Conversations and scheduled runs register the same resolved server.
  const ccSessionWiMcp = resolveCcSessionWiMcp(sessionWiHost);
  const pool = createPool({
    sessionWiMcp: ccSessionWiMcp,
    onShutdownInterrupt: (tenantId, payload) => {
      // wasBusyAtShutdown is the canonical "user is waiting" signal
      // (notifyResumed reads it). interruptedUserMessage is best-effort context
      // for the resume prompt — only set when Relay held a fresh doSend at
      // SIGTERM (long-thinking / 429 paths clear it before SIGTERM).
      const patch: Partial<import('./types.js').PersistedSession> = { wasBusyAtShutdown: true };
      if (payload.messageText) patch.interruptedUserMessage = payload.messageText;
      options?.stateManager?.updateSession(tenantId, patch);
    },
    onShutdownQueued: (tenantId, turns) => recordQueuedAtShutdown(options?.stateManager, tenantId, 'cc', turns),
    modelCatalog,
    isPendingInteractive: isPoolKeyPendingInteractive,
  });
  const { createMaintenanceBarrier } = await import('./maintenance-barrier.js');
  const { createSwitchBarrierStore } = await import('./switch-barrier.js');
  // Read before any channel, scheduler, resume or HTTP intake starts: a switch
  // barrier left by the previous process keeps this one from taking new work.
  const maintenanceBarrier = createMaintenanceBarrier(Date.now, {
    switchBarrier: createSwitchBarrierStore(switchBarrierPath(resolveRelayDataDir())),
    recordTurn: turn => {
      const state = options?.stateManager;
      if (!state || persistQueuedTurns(state, [turn], Date.now()).length === 0) return false;
      try { state.flushStrict(); return true; } catch { return false; }
    },
    unrecordTurn: turn => { if (options?.stateManager) unrecordPendingTurn(options.stateManager, turn.tenantId, turn.seq); },
  });
  // Before any intake starts: until the channel resume pass below settles,
  // only its turns pass, even if the switch barrier is released first.
  const startStartupResume = maintenanceBarrier.startupResume();
  {
    const found = maintenanceBarrier.status().switch;
    console.log({ ts: new Date().toISOString(), level: found?.blocking ? 'warn' : 'info', module: 'switch-barrier',
      msg: found?.blocking ? 'switch barrier in force at startup; no new work until it is released' : 'switch barrier read at startup',
      state: found?.state, holder: found?.holder, transaction: found?.transaction });
  }
  const runtimeManager = createConversationRuntimeManager({
    barrier: maintenanceBarrier,
    resolveMeteringAgent(poolKey) {
      // Validate against the actual Host router, never a prompt or workspace path.
      // Channel aliases stay unchanged; metering uses the operator-provisioned
      // canonical Harness principal when one exists.
      return resolveMeteringPrincipal(router, poolKey);
    },
  });
  const { createCcUsageMeasurement } = await import('./cc-usage-measurement.js');
  runtimeManager.register(createPoolConversationAdapter(pool, {
    usageMeasurement: createCcUsageMeasurement({ host: ccUsageHost }),
  }));
  runtimeManager.register(createCodexConversationRuntime({ stateManager: options?.stateManager, modelCatalog }));
  // cc-sdk: new SDK-driven runtime, parallel to legacy cc. No agent selects it
  // by default (opt-in via relay.json `runtime: "cc-sdk"`), so registering it is
  // inert for the current 11 bots — pure new path.
  runtimeManager.register(createCcSdkConversationRuntime({ stateManager: options?.stateManager }));
  runtimeManager.register(createPiConversationRuntime({
    manifest: piManifest,
  }));

  // Ensure CC-runtime agents have a ccModel: if modelCatalog has providers,
  // assign the first available anthropic-protocol model as default IN-MEMORY ONLY.
  // Do NOT call router.setAgentModel (which persists to agent-models.json) — that
  // would overwrite user-selected models on every restart when agent-models.json
  // hasn't been read yet during router init. The router init already merges
  // agent-models.json overrides into agent.ccModel, so agents with explicit user
  // selections are skipped by the `if (agent.ccModel) continue` guard.
  // Codex-runtime agents are SKIPPED: ccModel is a CC concept.
  if (modelCatalog?.providers) {
    for (const user of router.getAllUsers()) {
      for (const agent of router.getAgents(user.userId)) {
        if (agent.ccModel) continue;
        if (!['cc', 'cc-sdk'].includes(resolveAgentRuntimeId(agent))) continue;
        const defaultEntry = modelCatalog.getDefaultModelForProtocol('anthropic');
        if (defaultEntry) {
          agent.ccModel = defaultEntry.id;
        }
      }
    }
  }

  function resolveBudget(tenantId: string): { agentId: string; budgetUsd: number } | undefined {
    const address = parseConversationPoolKey(tenantId);
    if (!address) return undefined;
    const { userId, agentId } = address;
    const agent = router.getAgents(userId).find((a) => a.id === agentId);
    if (!agent || !agent.monthlyBudgetUsd || agent.monthlyBudgetUsd <= 0) return undefined;
    return { agentId, budgetUsd: agent.monthlyBudgetUsd };
  }

  // Platform HTTP URL: co-located Platform defaults to localhost:8787
  const platformHttpUrl = process.env['PLATFORM_HTTP_URL']?.trim() || 'http://localhost:8787';

  /**
   * Extract the Platform tenantId from a Relay poolKey.
   * PoolKey format: `userId:agentId[:scope]`
   *
   * For tenant users with a linuxUser field (e.g. "tenant-member-0001"),
   * strip the "tenant-" prefix to get the canonical tenantId ("member-0001").
   * For admin users without linuxUser, use the userId as-is — the Platform
   * BalanceStore will auto-create a shadow account to track consumption
   * without requiring balance.
   */
  function tenantIdFromPoolKey(poolKey: string): string {
    const address = parseConversationPoolKey(poolKey);
    if (!address) return poolKey;
    const { userId, agentId } = address;
    const agent = router.getAgents(userId).find((a) => a.id === agentId);
    if (agent?.linuxUser?.startsWith('tenant-')) {
      return agent.linuxUser.slice('tenant-'.length);
    }
    return userId;
  }

  const pricingConfigPath = managedRelayPath('pricing', process.env['PRICING_CONFIG_PATH']?.trim())
    || modelCatalogPath
    || undefined;

  const metering = createMetering({
    meteringDir,
    resolveBudget,
    pricingConfigPath,
    // Balance deduction: call Platform /internal/balance/deduct
    deductBalance: async (poolKey: string, costCents: number, metadata?: { modelId?: string; tokensIn?: number; tokensOut?: number; sessionId?: string }): Promise<boolean> => {
      const tenantId = tenantIdFromPoolKey(poolKey);
      try {
        const res = await fetch(`${platformHttpUrl}/internal/balance/deduct`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            tenantId,
            costFen: costCents,
            description: '模型调用消费',
            ...(metadata ? { metadata } : {}),
          }),
          signal: AbortSignal.timeout(5000),
        });
        if (!res.ok) {
          console.error({ ts: new Date().toISOString(), level: 'error', msg: 'Balance deduct request failed', tenantId, status: res.status });
          return false;
        }
        const data = await res.json() as { ok?: boolean; error?: string; newBalance?: number; balance?: number; tracked?: boolean };
        if (!data.ok) {
          // Admin shadow accounts (balance=0) return insufficient_balance with tracked=true.
          // This is expected — consumption is recorded but no actual deduction occurs.
          // Log a warning but return true so metering doesn't retry.
          if (data.error === 'insufficient_balance' && data.tracked) {
            console.log({ ts: new Date().toISOString(), level: 'info', msg: 'Balance deduction skipped (admin/trial user)', tenantId, costCents, tracked: true });
            return true;
          }
          console.warn({ ts: new Date().toISOString(), level: 'warn', msg: 'Balance deduct rejected', tenantId, error: data.error });
          return false;
        }
        console.log({ ts: new Date().toISOString(), level: 'info', msg: 'Balance deducted', tenantId, costCents, newBalance: data.newBalance });
        return true;
      } catch (err) {
        console.error({ ts: new Date().toISOString(), level: 'error', msg: 'Balance deduct error', tenantId, error: String(err) });
        return false;
      }
    },
    // Balance query: call Platform /internal/balance/{tenantId}
    getBalance: async (poolKey: string): Promise<number | null> => {
      const tenantId = tenantIdFromPoolKey(poolKey);
      try {
        const res = await fetch(`${platformHttpUrl}/internal/balance/${encodeURIComponent(tenantId)}`, {
          signal: AbortSignal.timeout(5000),
        });
        if (!res.ok) return null;
        const data = await res.json() as { balance?: number; exists?: boolean };
        return typeof data.balance === 'number' ? data.balance : null;
      } catch {
        return null;
      }
    },
  });

  // Validate config (single-channel constraint)
  const rawConfig = JSON.parse(readFileSync(configPath, 'utf-8'));
  validateConfig(rawConfig);

  // Restore sessionIds from state.json (survives /new across Relay restarts)
  if (options?.stateManager) {
    const sessions = options.stateManager.getAllSessions();
    for (const [poolKey, session] of Object.entries(sessions)) {
      if (session.sessionId) {
        const address = parseConversationPoolKey(poolKey);
        if (!address) {
          console.warn({ ts: new Date().toISOString(), level: 'warn', msg: 'Skipped malformed persisted pool key', poolKey });
          continue;
        }
        const { userId, agentId, channelScope } = address;
        const agentExists = router.getAgents(userId).some(agent => agent.id === agentId);
        if (!agentExists) {
          options.stateManager.removeSession(poolKey);
          console.warn({ ts: new Date().toISOString(), level: 'warn', msg: 'Removed stale session for missing agent', poolKey });
          continue;
        }
        if (channelScope) {
          // Scope the restore to that channel so e.g. a feishu pool key never
          // writes the TG session field (and vice versa). Without this, a
          // dual-channel agent's stale TG session could clobber its fresh
          // feishu session depending on iteration order (memo 2026-06-26).
          router.overrideSessionId(userId, agentId, session.sessionId, channelScope);
          console.log({ ts: new Date().toISOString(), level: 'info', msg: 'Restored scoped session from state', poolKey, scope: channelScope, sessionId: session.sessionId.slice(0, 8) });
          continue;
        }
        // Unscoped pool key = TG (or single-channel) session. Legacy field
        // selection (TG vs feishu-only) is preserved by passing no scope; the
        // router no longer mirrors a TG restore onto a dual-channel agent's
        // feishu field (memo 2026-06-26).
        router.overrideSessionId(userId, agentId, session.sessionId);
        console.log({ ts: new Date().toISOString(), level: 'info', msg: 'Restored sessionId from state', poolKey, sessionId: session.sessionId.slice(0, 8) });
      }
    }
  }

  // Scheduler is created after bots — use a shared deps object so we can assign scheduler later
  const botDeps: import('./channels/telegram.js').TelegramBotDeps = { runtime: runtimeManager, router, stateManager: options?.stateManager, meteringDir, metering, modelCatalog };

  const bots = createTelegramBots(botDeps);

  // Wire metering
  runtimeManager.on('response', (poolKey: string, msg: StreamJsonMessage) => {
    if (msg.type === 'result') {
      const proc = runtimeManager.get(poolKey);

      // 切片1 回合结束埋点：CC 的 result 消息即一个 turn 的收尾,带 stopReason/耗时/token。
      // 旁路观测,dark-deploy 开关控制,全隔离永不抛（traceTurnEnd 内部已 try/catch）。
      // 主链路(metering/session 捕获)一字节不变——这行只读 msg,不改任何业务对象。
      //
      // 缺陷 B（2026-07-15 canary）：一个 print-mode turn 会 emit 两条 result——
      //   pool.ts finishTurn 先 emit 合成的 {type:'result', result:''}（无 subtype/usage/is_error,
      //   即"空"事件）,随后 stdout 又 emit CC 原始 result（带 subtype/duration_ms/usage/is_error,
      //   即"完整"事件）。旧代码对两条都打点 → turn.end 计数虚高 + 一条空事件污染对账。
      // 修法=实质数据判据：只在 result 带实质回合信号（subtype 或 usage）时打点。
      //   - print-mode 空合成 result（无 subtype 无 usage）→ 跳过。
      //   - print-mode 真 result（有 subtype+usage）→ 打点。
      //   - interactive-runtime 合成 result（无 subtype 但带 usage,见 pool.ts finishTurn 传 usageRecord）
      //     → 打点（有 usage 即算实质,不误伤 interactive 的唯一 turn.end）。
      {
        const usage = (msg.usage && typeof msg.usage === 'object') ? msg.usage as Record<string, unknown> : undefined;
        const num = (v: unknown): number | undefined => (typeof v === 'number' ? v : undefined);
        if (shouldTraceTurnEnd(msg)) {
          traceTurnEnd({
            agent: parseConversationPoolKey(poolKey)?.agentId,
            // 切片A 配对主键：turn.end 同时带 poolKey（带 scope 的完整池键）+ sessionId,成为
            // req.inbound(poolKey) ↔ delivery(poolKey) 与 CC sessionId 的桥。poolKey 就是 on('response')
            // 回传的池键,与 req.inbound(feishuPoolKey)/delivery(poolKey) 逐字符同口径。
            poolKey: poolKey,
            session: proc?.sessionId ?? (typeof msg.session_id === 'string' ? msg.session_id : undefined),
            stopReason: typeof msg.subtype === 'string' ? msg.subtype : undefined,
            durationMs: num(msg.duration_ms),
            inputTokens: usage ? num(usage.input_tokens) : undefined,
            outputTokens: usage ? num(usage.output_tokens) : undefined,
            // 缓存两项必须单独带出：裸 input_tokens 在长上下文会话里只占真实输入的万分之一
            // （实测单 session 裸 input 1802 / cache_read 2.78 亿）,只记裸 input 会让
            // Stats 的用量与成本判断整体失真。
            cacheReadTokens: usage ? num(usage.cache_read_input_tokens) : undefined,
            cacheCreationTokens: usage ? num(usage.cache_creation_input_tokens) : undefined,
            isError: typeof msg.is_error === 'boolean' ? msg.is_error : undefined,
          });
        }
      }

      // Fallback model: when CC's result doesn't include model field,
      // resolve from router's agent config (covers Claude native models
      // and auto-resolved catalog models).
      const fallbackModel = typeof msg.model !== 'string' || !msg.model
        ? (() => {
            const address = parseConversationPoolKey(poolKey);
            return address ? router.getAgentModel(address.userId, address.agentId) : undefined;
          })()
        : undefined;
      const reportedSessionId = typeof msg.session_id === 'string' ? msg.session_id.trim() : '';
      const meteringSessionId = reportedSessionId || proc?.sessionId || 'unknown';
      metering.record(poolKey, meteringSessionId, msg, {
        fallbackModel,
        lineage: buildRelayInteractiveLineage({
          tenantId: poolKey,
          sessionId: meteringSessionId,
          runtime: proc?.runtime,
        }),
      });

      // Capture session_id emitted by CC in its result message.
      // CC generates its own UUID whenever it starts a fresh session (print-mode
      // spawns with `--session-id <new uuid>`), so this is the only place the
      //真 session 会主动报给 Relay。
      //
      // 2026-07-25 记忆错乱事故：这里原本的 guard 是 `if (!existing?.sessionId)`
      // ——只在"这个 poolKey 从来没有过 session"时才回写。一旦 state 里已经躺着
      // 任何一个值（哪怕是昨天的、早已不是 CC 在跑的那条），本块整个跳过，CC 自产的
      // 真 session 永远进不了 state。事故当天全天 journal 里 `Captured CC session_id`
      // 零条，就是这条 guard 挡的。
      //
      // 改成"值不一致就回写"：CC 报什么就记什么，state 始终跟住真在跑的那条。
      const ccSid = typeof msg.session_id === 'string' ? msg.session_id : undefined;
      if (ccSid && options?.stateManager) {
        const existing = options.stateManager.getSession(poolKey);
        if (existing?.sessionId !== ccSid) {
          const previousSid = existing?.sessionId;
          const address = parseConversationPoolKey(poolKey);
          if (!address) return;
          const { userId, agentId, channelScope } = address;
          if (!router.getAgents(userId).some(agent => agent.id === agentId)) return;
          // Router owns the channel-specific session field selection for both
          // scoped and legacy unscoped pool keys.
          router.overrideSessionId(userId, agentId, ccSid, channelScope);
          // Also persist to state so it survives relay restart
          options.stateManager.updateSession(poolKey, {
            poolKey,
            agentId,
            sessionId: ccSid,
            runtime: existing?.runtime ?? 'cc',
            lastActivity: Date.now(),
            status: 'active',
          });
          console.log({
            ts: new Date().toISOString(), level: 'info', msg: 'Captured CC session_id',
            poolKey, sessionId: ccSid.slice(0, 8),
            // 带上被替换的旧值：state 与 CC 真值分叉时这行就是取证锚点。
            previousSessionId: previousSid ? previousSid.slice(0, 8) : undefined,
            corrected: Boolean(previousSid),
          });
        }
      }
    }
  });

  const users = router.getAllUsers();
  const agentBots = router.getAllAgentBots();
  const feishuBotsList = router.getAllFeishuBots();

  console.log({
    ts: new Date().toISOString(),
    level: 'info',
    msg: 'Relay starting',
    users: users.length,
    tgBots: agentBots.length,
    feishuBots: feishuBotsList.length,
  });

  await bots.start();

  // The PULSE platform bot asks only the owner's confirmations (AH-083). It runs
  // where relay.json holds its token and the installed core has external entries.
  let platformBot: { stop(): void } | undefined;
  const platformToken = (rawConfig as RelayConfig).platformBot?.botToken;
  if (platformToken) {
    const core = nodeWorkItemCore();
    const settings = core ? platformSettings(join(dirname(core.entrypoint), 'host-policy.json')) : undefined;
    if (settings) platformBot = startPlatformBot(createTelegramApiBot(platformToken), settings);
    else console.warn({ ts: new Date().toISOString(), level: 'warn', module: 'platform-bot',
      msg: 'Platform bot token is configured but the installed core has no external entries' });
  }

  console.log({
    ts: new Date().toISOString(),
    level: 'info',
    msg: `All ${agentBots.length} TG bots started (long polling)`,
  });

  // Start Feishu bots — even with 0 currently configured, we keep the manager
  // around so the HTTP `/internal/agents/reload` path can spawn newcomers without
  // a Relay restart (Platform three-step provisioning).
  const feishuDeps: import('./channels/feishu.js').FeishuBotDeps = { pool: runtimeManager, router, stateManager: options?.stateManager, configPath, meteringDir, metering, modelCatalog, platformHttpUrl, terminalManager };
  const feishuBots = createFeishuBots(feishuDeps);
  if (feishuBotsList.length > 0) {
    await feishuBots.start();
    console.log({
      ts: new Date().toISOString(),
      level: 'info',
      msg: `All ${feishuBotsList.length} Feishu bots started (WebSocket)`,
    });
  }
  let nativeSessionRecoveryTimer:ReturnType<typeof setInterval>|undefined;
  if(nodeWorkItemCore() && options?.stateManager){
    let recovering=false;
    const recover=async()=>{
      if(recovering)return;
      // A switch barrier defers session handovers until it is released.
      if(maintenanceBarrier.switchBlocking())return;
      recovering=true;
      const conversation=(poolKey:string)=>{
        const address=parseConversationPoolKey(poolKey);
        const configured=address&&router.getAgents(address.userId).find(a=>a.id===address.agentId);
        if(!address||!configured)return undefined;
        const channel:'feishu'|'telegram'=address.channelScope==='feishu'||(!address.channelScope&&!configured.botToken)?'feishu':'telegram';
        return {channel,agent:channelConversationAgent(configured,channel==='feishu'?'feishu':'tg'),
          user:router.getAllUsers().find(u=>u.userId===address.userId),configured};
      };
      /** A Host notice in the conversation's own chat. */
      const notify=async(poolKey:string,text:string)=>{
        const target=conversation(poolKey);
        if(!target?.user)throw new Error('agent_switch_chat_unavailable');
        if(target.channel==='telegram'){
          if(!target.user.chatId)throw new Error('agent_switch_chat_unavailable');
          await bots.sendText(target.configured.id,target.user.chatId,text);
        }else{
          const openId=target.configured.feishuOpenId??target.user.feishuOpenId;
          if(!openId)throw new Error('agent_switch_chat_unavailable');
          await feishuBots.sendText(target.configured.id,openId,text);
        }
      };
      try {
        // An Agent's own committed request starts like /wi rollover, once its turn has ended.
        await adoptAgentRolloverRequests({dir:agentRolloverRequestDir(),stateManager:options.stateManager!,
          runtime:runtimeManager,start:async(request)=>{
            const target=conversation(request.tenant);
            if(!target)throw new Error('agent_rollover_conversation_unavailable');
            return rolloverNativeSession({poolKey:request.tenant,...target,runtime:runtimeManager,
              stateManager:options.stateManager!,ref:request.ref,origin:'agent',
              sourceNativeId:request.sourceNativeId});
          }}).catch(error=>console.error({msg:'Agent rollover requests unreadable',
            code:error instanceof Error?error.message:'agent_rollover_unknown'}));
        const acquireFor=(poolKey:string,target:NonNullable<ReturnType<typeof conversation>>)=>async()=>{
          await acquireConversation({poolKey,agent:target.agent,runtime:runtimeManager,
            stateManager:options.stateManager,channel:target.channel,intent:'inbound'});
          return poolKey;
        };
        // A /wi switch a restart or an unknown enter left unsettled is read back before anything else.
        for(const [poolKey,session] of Object.entries(options.stateManager!.getAllSessions())){
          if(!session.pendingWiSwitch)continue;
          const target=conversation(poolKey);
          if(!target)continue;
          await settleWiSwitch({poolKey,runtime:runtimeManager,stateManager:options.stateManager!,
            acquire:acquireFor(poolKey,target),reply:text=>notify(poolKey,text)})
            .catch(error=>console.error({msg:'Interrupted WI switch retained',poolKey,
              code:error instanceof Error?error.message:'wi_switch_unknown'}));
        }
        // An Agent's own committed request_switch runs like /wi switch, once its turn has ended.
        await adoptAgentSwitchRequests({dir:agentSwitchRequestDir(),stateManager:options.stateManager!,
          runtime:runtimeManager,start:async(request)=>{
            const target=conversation(request.tenant);
            if(!target)throw new Error('agent_switch_conversation_unavailable');
            const poolKey=request.tenant;
            return runAgentSwitch({ref:request.ref,target:request.target,poolKey,runtime:runtimeManager,
              stateManager:options.stateManager!,reply:text=>notify(poolKey,text),acquire:acquireFor(poolKey,target)});
          }}).catch(error=>console.error({msg:'Agent switch requests unreadable',
            code:error instanceof Error?error.message:'agent_switch_unknown'}));
        for(const [poolKey,session] of Object.entries(options.stateManager!.getAllSessions())){
          if(!session.pendingNativeRollover||session.pendingWiSwitch||switchInProgress(poolKey))continue;
          const live=runtimeManager.get(poolKey);
          if(live && ['busy','starting'].includes(live.state))continue;
          const target=conversation(poolKey);
          if(!target)continue;
          try {
            await rolloverNativeSession({poolKey,...target,runtime:runtimeManager,stateManager:options.stateManager!});
          } catch(error) {
            console.error({msg:'Native session handover retained for original-request recovery',poolKey,
              code:error instanceof Error?error.message:'native_rollover_unknown'});
          }
        }
      } finally {recovering=false;}
    };
    nativeSessionRecoveryTimer=setInterval(()=>{void recover();},30_000);
    // A turn that ended may have committed a request_switch or request_rollover: look soon after.
    runtimeManager.on('response',(_tenantId,msg)=>{
      if(msg.type==='result')setTimeout(()=>{void recover();},2_000).unref?.();
    });
    nativeSessionRecoveryTimer.unref?.();
    setTimeout(()=>{void recover();},1000).unref?.();
  }

  // Read standing grants once in the background so the first sessions need not wait.
  warmStandingAccess();

  // Initialize scheduler with per-owner bot resolution (wi_20260421_634 fix).
  // cron-jobs with ``owner=<agentId>`` push to the matching agent bot; jobs
  // with no matching bot (e.g. ``owner=system``) fall back to the first
  // agent bot.
  const agentBotList = router.getAllAgentBots();

  let scheduler: Scheduler | undefined;
  const { JobTerminalRegistry } = await import('./job-terminal-registry.js');
  const jobTerminalRegistry = new JobTerminalRegistry();

  // Collaboration notices name Agents as configured (Amanda), not by Harness actor.
  const displayAgentName = (actor: string) =>
    agentDisplayName(router.getAllUsers().flatMap(user => router.getAgents(user.userId)), actor);

  const localOnlyAgentConfigured = (rawConfig as RelayConfig).users.some(user => user.localOnly === true);
  if (shouldStartScheduler(agentBotList.length, feishuBotsList.length, Boolean(placementContext), localOnlyAgentConfigured)) {
    // Feishu, placement and local-only hosts can run without a Telegram client.
    const fallbackBot = agentBotList[0]
      ? bots.getApiBot(agentBotList[0].agent.id)!
      : ({ api: {} } as unknown as import('grammy').Bot);
    const runtimePriority = resolvePulseRuntimePriority();
    scheduler = createScheduler(fallbackBot, cronJobsPath, {
      getBotByAgentId: (agentId: string) => bots.getApiBot(agentId),
      getFeishuClientByAgentId: (agentId: string) => feishuBots.getMessageClient(agentId),
      currentNode: placementContext?.currentNode,
      ownerCanRunOnNode: placementContext?.ownerCanRunOnNode,
      deliveryBotIdForOwner: placementContext?.deliveryBotIdForOwner,
      runtimePolicy: {
        primary: runtimePriority[0],
        fallbacks: runtimePriority.slice(1),
      },
      terminalRegistry: jobTerminalRegistry,
      admitScheduledRun: () => !maintenanceBarrier.isHeld(),
      agentDisplayName: displayAgentName,
      ccWriteBoundaryFor: (owner: string) => router.getAllUsers().flatMap(user => router.getAgents(user.userId))
        .some(agent => agent.ccWriteBoundary === true && harnessPrincipalForAgent(agent) === owner),
      // Channel aliases of one principal must agree; differing lists open none.
      ccRepoAccessFor: (owner: string) => {
        const lists = [...new Set(router.getAllUsers().flatMap(user => router.getAgents(user.userId))
          .filter(agent => agent.ccWriteBoundary === true && harnessPrincipalForAgent(agent) === owner)
          .map(agent => JSON.stringify(agentRepoAccess(agent) ?? null)))];
        return lists.length === 1 ? JSON.parse(lists[0]!) ?? undefined : undefined;
      },
      ccSessionWiMcp,
    });
    scheduler.start();
    botDeps.scheduler = scheduler;
    feishuDeps.scheduler = scheduler;
    console.log({
      ts: new Date().toISOString(),
      level: 'info',
      msg: `Scheduler initialized with ${bots.runningAgentIds().length} agent bot(s) for per-owner delivery routing`,
      agentIds: bots.runningAgentIds(),
      feishuClientAgentIds: feishuBots.runningAgentIds(),
    });
  } else {
    console.warn({ ts: new Date().toISOString(), level: 'warn', msg: 'No configured scheduler owner, scheduler not started' });
  }

  // A conversation's channel is not always in its pool key (Feishu-only agents);
  // each channel checks that it owns the conversation before sending.
  const agentJobsData = managedRelayPath('dataDir');
  const verifyAgentJobs = agentJobsData ? createAgentJobsVerifier(agentJobsData) : undefined;
  /** The installed core's namespace: with the surface it names this Relay's native sessions. */
  const coreNamespace = (): string | undefined => {
    const core = nodeWorkItemCore();
    if (!core) return undefined;
    try {
      const policy = JSON.parse(readFileSync(join(dirname(core.entrypoint), 'host-policy.json'), 'utf8')) as { namespace?: unknown };
      return typeof policy.namespace === 'string' ? policy.namespace : undefined;
    } catch { return undefined; }
  };
  const collabDelivery = createCollabDelivery({
    sessions: () => options?.stateManager?.getAllSessions() ?? {},
    live: poolKey => runtimeManager.get(poolKey),
    principalFor: poolKey => resolveMeteringPrincipal(router, poolKey),
    channelFor: () => ({
      deliverInternalTurn: async (poolKey, prompt, ids, notice) =>
        await bots.deliverInternalTurn(poolKey, prompt, ids, notice)
        || (feishuBots ? await feishuBots.deliverInternalTurn(poolKey, prompt, ids, notice) : false),
    }),
    displayName: displayAgentName,
    ledgerPath: join(resolveRelayDataDir(), 'collab-delivery-ledger.json'),
    // Declared below; a delivery cannot be deferred before the HTTP server starts.
    rerun: actors => collabLedgerWatch?.request(actors),
  });
  runtimeManager.on('response', (_tenantId, msg) => { if (msg.type === 'result') void collabDelivery.drain(); });

  // A contract entering the production ledger runs the recipient's (or, once
  // answered, the requester's) collaboration-inbox job at once, and so does a
  // pull request result for the Agent that registered it; the job's schedule
  // stays the fallback. Only local Git reads: see collab-ledger-watch.ts.
  let collabLedgerWatch: CollabLedgerWatch | undefined;
  if (scheduler) {
    const jobs = scheduler;
    try {
      const core = nodeWorkItemCore();
      const binding = core ? ledgerBinding(join(dirname(core.entrypoint), 'host-policy.json')) : undefined;
      if (binding) {
        collabLedgerWatch = createCollabLedgerWatch({
          ...binding, jobs: () => jobs.listJobs(), trigger: id => jobs.triggerJob(id),
        });
        collabLedgerWatch.start();
      }
    } catch (error) {
      console.warn({ ts: new Date().toISOString(), level: 'warn', module: 'collab-ledger-watch',
        msg: 'collaboration ledger watch not started; inbox jobs run on schedule only', error: String(error) });
    }
  }

  // Start loopback HTTP after pools are built so /internal/push can resolve
  // bots/clients via injected resolvers. When pools are absent, the resolvers
  // return null and push responds with 404 agent_not_found.
  const { startRelayHttpServerIfEnabled } = await import('./http-server.js');
  const httpHandle = await startRelayHttpServerIfEnabled({
    feishuBots,
    telegramBots: bots,
    router,
    configPath,
    startedAtMs: Date.now(),
    terminalManager,
    getTelegramBot: (agentId: string) => bots.getApiBot(agentId),
    getFeishuClient: (agentId: string) => feishuBots.getMessageClient(agentId),
    runtimeManager,
    scheduler,
    sclIntentStatus:()=>{
      // Native session handovers in flight block a safe restart.
      const active=nativeRolloverActiveCount(runtimeManager);
      return {available:true,busy:active,restartBlocking:active,
        states:{recognized:0,attempting:active,retry_wait:0,result_ready:0}};
    },
    terminalRegistry: jobTerminalRegistry,
    maintenance: maintenanceBarrier,
    agentJobs: scheduler && verifyAgentJobs ? envelope => {
      // The verified key names the conversation; its owner is Relay's, never the body's.
      const sessions = options?.stateManager?.getAllSessions() ?? {};
      const { poolKey, body } = verifyAgentJobs(envelope, Object.keys(sessions));
      const owner = resolveMeteringPrincipal(router, poolKey);
      if (!owner) throw new AgentJobError('this conversation has no Agent principal');
      return applyAgentJobs({ ...(body as AgentJobRequest), actor: owner }, {
      cronJobsPath, controlPath: join(dirname(cronJobsPath), 'automation-control.json'),
      workspaceFor: owner => router.getAllUsers().flatMap(user => router.getAgents(user.userId))
        .find(agent => { try { return harnessPrincipalForAgent(agent) === owner; } catch { return false; } })?.workspace,
      reload: () => scheduler!.reload(),
      // The core identity the tool sent must be this conversation's native session.
      hosted: (actor, session, surface) => {
        const namespace = coreNamespace();
        return namespace !== undefined && actor === owner
          && nativeIds(sessions[poolKey], runtimeManager.get(poolKey)).some(id => sessionRef(namespace, actor, id, surface) === session);
      },
      refusal: () => placementContext ? 'node placement mode: change the placement source instead'
        : maintenanceBarrier.isHeld() ? 'a maintenance switch is in progress; retry later' : undefined,
      });
    } : undefined,
    deliverCollaboration: request => {
      if (request.role !== 'wake') return collabDelivery.deliver(request);
      // A wake carries its own job's prompt, and only for the WI and owner that job names.
      const job = scheduler?.listJobs().find(candidate => candidate.id === request.job_id);
      if (!job?.enabled || job.owner !== request.actor || job.wake?.workItemId !== request.target_work_item_id) {
        return Promise.resolve('not_hosted');
      }
      return collabDelivery.deliver({ ...request, prompt: job.agent.prompt });
    },
    preflightPlacement: placementContext
      ? () => placementContext.preparePreflight(configPath, cronJobsPath).commit
      : undefined,
  });

  // After full startup, wake recently active human sessions so agents can notify users.
  // Small delay ensures the pool and WS clients are fully ready before injecting messages.
  // Under a switch barrier the resume (and the intake replays queued behind
  // it) waits until the barrier is released; turns that waited here go out
  // after it, so the earlier process's pending turns keep their place.
  setTimeout(() => {
    const when = startStartupResume(() => Promise.allSettled([bots.notifyResumed(), feishuBots?.notifyResumed()]));
    if (when === 'deferred') console.warn({ ts: new Date().toISOString(), level: 'warn', module: 'switch-barrier',
      msg: 'channel resume deferred until the maintenance barrier is released' });
  }, 3000);

  return {
    stop() {
      botDeps.scheduler?.stop();
      if(nativeSessionRecoveryTimer)clearInterval(nativeSessionRecoveryTimer);
      feishuBots?.stop();
      bots.stop();
      platformBot?.stop();
      metering.dispose();
      collabDelivery.stop();
      collabLedgerWatch?.stop();
      maintenanceBarrier.switchBarrier?.dispose();
      runtimeManager.dispose();
      void httpHandle?.stop();
    },
    async notifyShutdown(): Promise<void> {
      const { busyAtShutdown, stampShutdownInterrupts } = await import('./shutdown-interrupts.js');
      const state = options?.stateManager;
      // Close intake first: turns waiting at the barrier, and every turn that
      // still arrives until the process exits, are written to state as the
      // interrupted message of their session at arrival; none is dispatched.
      maintenanceBarrier.close(turn => {
        if (!state || persistQueuedTurns(state, [turn], Date.now()).length === 0) {
          console.warn({ ts: new Date().toISOString(), level: 'warn', msg: 'turn arrived during shutdown for an unknown session; not recorded', tenantId: turn.tenantId });
          return;
        }
        state.flush();
      });
      const earlier = state ? busyAtShutdown(state) : new Set<string>();
      const notifiedByPool = runtimeManager.emitShutdown();
      // Resume windows measure from this interruption, not from lastActivity.
      if (state) stampShutdownInterrupts(state, earlier, Date.now());
      // Broadcast to Feishu bindings not already notified via pool session-end
      await feishuBots?.notifyShutdown(notifiedByPool);
      // Give API calls a moment to dispatch
      await new Promise(resolve => setTimeout(resolve, 1000));
    },
    async drainBusySessions(timeoutMs: number): Promise<Set<string>> {
      if (!runtimeManager.drainBusySessions) return new Set();
      return runtimeManager.drainBusySessions(timeoutMs);
    },
    reloadScheduler() {
      if (placementContext) {
        throw new Error('SIGHUP scheduler-only reload is disabled in node-placement mode; use the fenced agents reload path');
      }
      botDeps.scheduler?.reload();
    },
  };
}

// --- Auto-start when run directly (node dist/index.js) ---
const isMainModule = process.argv[1]?.endsWith('dist/index.js')
  || process.argv[1]?.endsWith('dist/index.ts');

if (isMainModule) {
  startRelay().then((relay) => {
    console.log({ ts: new Date().toISOString(), level: 'info', msg: 'Relay ready' });

    const shutdown = async (signal: string) => {
      console.log({ ts: new Date().toISOString(), level: 'info', msg: `Received ${signal}, shutting down...` });
      await relay.notifyShutdown?.();
      relay.stop();
      process.exit(0);
    };

    process.on('SIGINT', () => shutdown('SIGINT'));
    process.on('SIGTERM', () => shutdown('SIGTERM'));
  }).catch((err) => {
    console.error({ ts: new Date().toISOString(), level: 'error', msg: 'Relay failed to start', error: String(err) });
    process.exit(1);
  });
}
