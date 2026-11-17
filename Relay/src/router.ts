import { managedRelayPath } from './managed-paths.js';
import { formatConversationPoolKey } from './conversation-address.js';
import {channelConversationAgent, retainFeishuConversation, type ConversationChannel} from './channel-conversation.js';

import { readFileSync, renameSync, writeFileSync, existsSync, mkdirSync } from 'node:fs';
import { join, dirname } from 'node:path';
import type { AgentConfig, ChannelBindingRecord, UserConfig, RelayConfig, ConversationModelRoute, ConversationRuntimeDefault, ConversationRuntimeId } from './types.js';
import { validateAgentRepoAccess } from './types.js';

/** Resolve Relay home directory from env (for agent-models.json path). */
function resolveRelayHomeDir(): string {
  return process.env['RELAY_HOME'] ?? process.env['HOME'] ?? '/home/pulse';
}

/** Path to agent-models.json for a specific user.
 *  Stored under Relay's own .relay directory (always writable by pulse user),
 *  not under the tenant's home directory (which pulse cannot write to). */
function agentModelOverridePath(userId: string): string {
  return join(managedRelayPath('modelOverridesDir') ?? join(resolveRelayHomeDir(), '.relay', 'agent-models'), `${userId}.json`);
}

/** Override record: three states.
 *  - undefined (no entry): no override — agent uses relay.json ccModel as-is
 *  - { model: null }: explicit default — clear relay.json ccModel (CC自主选择, no --model arg)
 *  - { model: string }: explicit override — use this model
 */
type ModelOverrideRecord = { model: string | null };
type OverrideLoadResult = string | null | undefined; // string=set, null=explicit default, undefined=no entry

/** Load tenant model override for a specific agent. Returns three states (see ModelOverrideRecord). */
function loadTenantModelOverride(userId: string, agentId: string): OverrideLoadResult {
  const path = agentModelOverridePath(userId);
  try {
    const raw = readFileSync(path, 'utf-8');
    const parsed = JSON.parse(raw) as { agents?: Record<string, ModelOverrideRecord> };
    const rec = parsed.agents?.[agentId];
    if (!rec || !('model' in rec)) return undefined;
    return rec.model; // string | null
  } catch {
    return undefined;
  }
}

/** Write tenant model override for a specific agent.
 *  model === undefined: remove the entry (forget override → relay.json ccModel wins)
 *  model === null/'default': record explicit default (override clears relay.json ccModel)
 *  model === string: record explicit override
 */
function writeTenantModelOverride(userId: string, agentId: string, model: string | null | undefined): void {
  const path = agentModelOverridePath(userId);
  let data: { agents: Record<string, ModelOverrideRecord> } = { agents: {} };

  // Load existing data
  try {
    const raw = readFileSync(path, 'utf-8');
    const parsed = JSON.parse(raw) as { agents?: Record<string, ModelOverrideRecord> };
    if (parsed.agents) data = { agents: parsed.agents };
  } catch {
    // File doesn't exist yet — will create
  }

  // Update or delete the agent's model
  if (model === undefined) {
    delete data.agents[agentId];
  } else {
    data.agents[agentId] = { model };
  }

  // Ensure directory exists
  const dir = dirname(path);
  if (!existsSync(dir)) {
    mkdirSync(dir, { recursive: true });
  }

  // Write atomically
  const tmpPath = `${path}.tmp`;
  writeFileSync(tmpPath, `${JSON.stringify(data, null, 2)}\n`, { mode: 0o600 });
  renameSync(tmpPath, path);
}

function supportsTelegram(agent: AgentConfig): boolean {
  return !!agent.botToken;
}

function supportsFeishu(agent: AgentConfig): boolean {
  return !!agent.feishu;
}

function preferredSessionField(agent: AgentConfig): 'sessionId' | 'feishuSessionId' {
  return agent.botToken || !agent.feishu ? 'sessionId' : 'feishuSessionId';
}

export interface Router {
  resolveUser(chatId: string): UserConfig | undefined;
  getActiveAgent(userId: string): AgentConfig | undefined;
  switchAgent(userId: string, agentId: string): AgentConfig | undefined;
  poolKey(userId: string, agentId: string): string;
  getAgents(userId: string): AgentConfig[];
  getAllUsers(): UserConfig[];
  /** Find user authorized for a specific TG bot (by agentId + chatId). */
  resolveByBotAgent(agentId: string, chatId: string): { user: UserConfig; agent: AgentConfig } | undefined;
  /** Find user authorized for a specific Feishu bot (by agentId + openId). */
  resolveByFeishuAgent(agentId: string, openId: string): { user: UserConfig; agent: AgentConfig } | undefined;
  /** Reset only the selected channel session; unscoped selects the agent's primary channel. */
  resetSession(userId: string, agentId: string, channelScope?: 'feishu' | 'tg'): string | undefined;
  /**
   * Override sessionId for a specific agent (e.g., restore from state.json).
   *
   * `channelScope` scopes the write to one channel's session field so a
   * dual-channel agent (botToken + feishu) does not cross-pollinate sessions:
   *   - 'feishu'      → only agent.feishu.sessionId
   *   - 'tg'          → only agent.sessionId
   *   - undefined     → only the agent's primary channel session field
   * Passing the right scope is what stops a stale TG session from clobbering a
   * fresh feishu session on restart (memo 2026-06-26 wrong-jsonl bug).
   */
  overrideSessionId(userId: string, agentId: string, sessionId: string, channelScope?: 'feishu' | 'tg'): void;
  /** Persistently set TG conversation runtime for a specific agent. */
  setAgentRuntime(userId: string, agentId: string, runtime: ConversationRuntimeId, channelScope?: ConversationChannel): AgentConfig | undefined;
  /** Get a reviewed node-level runtime route available to every agent. */
  getConversationRuntimeDefault?(runtime: ConversationRuntimeId): ConversationRuntimeDefault | undefined;
  /** Persistently set the model route for a structured conversation runtime. */
  setAgentConversationModel(userId: string, agentId: string, model: ConversationModelRoute, channelScope?: ConversationChannel): AgentConfig | undefined;
  /** Persistently set CC model for a specific agent (undefined clears ccModel). */
  setAgentModel(userId: string, agentId: string, model: string | undefined, channelScope?: ConversationChannel): AgentConfig | undefined;
  /** Get effective model for an agent (tenant override > relay.json ccModel). */
  getAgentModel(userId: string, agentId: string, channelScope?: ConversationChannel): string | undefined;
  /** Get effective model for an agent (tenant override > relay.json ccModel). */
  /** Get SSH alias from relay.json top-level config (for /cc takeover on servers). */
  getSshAlias?(): string | undefined;
  /** Get all unique TG agent definitions across all users (for multi-bot startup). */
  getAllAgentBots(): Array<{ agent: AgentConfig; authorizedChatIds: string[] }>;
  /** Get all feishu-type agents across all users. */
  getAllFeishuBots(): Array<{ agent: AgentConfig; authorizedOpenIds: string[] }>;
  /** Explicit Gateway binding records from relay.json, if configured. */
  getGatewayBindings?(): ChannelBindingRecord[];
  /** Add/update an explicit Gateway binding in memory after approval. */
  upsertGatewayBinding?(binding: ChannelBindingRecord): boolean;
  /** Get the effective channel type for an agent. */
  getAgentChannel(agent: AgentConfig): 'telegram' | 'feishu';
  /** Dynamically index a feishu openId for an agent (used by pairing flow). */
  indexFeishuOpenId(agentId: string, openId: string): boolean;
  /** Re-read configuration and rebuild indexes, retaining current channel sessions. */
  reload(): { users: number; bindings: number };
}

/** All conversation reads and writes in a channel command use one scope. */
export function conversationChannelRouter(router: Router, channel: ConversationChannel): Router {
  const project = (agent: AgentConfig | undefined) => agent ? channelConversationAgent(agent, channel) : undefined;
  return {
    ...router,
    getAgents: userId => router.getAgents(userId).map(agent => channelConversationAgent(agent, channel)),
    getActiveAgent: userId => project(router.getActiveAgent(userId)),
    switchAgent: (userId, agentId) => project(router.switchAgent(userId, agentId)),
    setAgentRuntime: (userId, agentId, runtime) => router.setAgentRuntime(userId, agentId, runtime, channel),
    setAgentConversationModel: (userId, agentId, model) => router.setAgentConversationModel(userId, agentId, model, channel),
    setAgentModel: (userId, agentId, model) => router.setAgentModel(userId, agentId, model, channel),
    getAgentModel: (userId, agentId) => router.getAgentModel(userId, agentId, channel),
    resetSession: (userId, agentId) => router.resetSession(userId, agentId, channel),
    overrideSessionId: (userId, agentId, sessionId) => router.overrideSessionId(userId, agentId, sessionId, channel),
  };
}

export function createRouter(configPath: string): Router {
  configPath = managedRelayPath('relayConfig', configPath) ?? configPath;
  let raw = readFileSync(configPath, 'utf-8');
  let data = JSON.parse(raw) as RelayConfig;

  // chatId → UserConfig (TG)
  const chatMap = new Map<string, UserConfig>();
  // openId → UserConfig (Feishu)
  const openIdMap = new Map<string, UserConfig>();
  // userId → UserConfig
  const userMap = new Map<string, UserConfig>();
  // userId → activeAgentId
  const activeAgentMap = new Map<string, string>();
  // `userId:agentId` → AgentConfig
  const agentLookup = new Map<string, AgentConfig>();

  function rebuildIndexes(): void {
    chatMap.clear();
    openIdMap.clear();
    userMap.clear();
    activeAgentMap.clear();
    agentLookup.clear();

    for (const user of data.users) {
      userMap.set(user.userId, user);
      activeAgentMap.set(user.userId, user.defaultAgent);

      if (user.chatId) {
        chatMap.set(user.chatId, user);
      }

      if (user.feishuOpenId) {
        openIdMap.set(user.feishuOpenId, user);
      }

      for (const agent of user.agents) {
        agentLookup.set(formatConversationPoolKey({ userId: user.userId, agentId: agent.id }), agent);
        if (agent.feishuOpenId) {
          openIdMap.set(agent.feishuOpenId, user);
        }
      }
    }

    const bindingRecords = data.bindings ?? [];
    for (const binding of bindingRecords) {
      indexBinding(binding);
    }
  }

  function indexBinding(binding: ChannelBindingRecord): void {
    if (binding.status !== 'active') return;
    const user = userMap.get(binding.principalId);
    if (!user) return;
    if (binding.address.channel === 'telegram' && binding.address.kind === 'telegram_chat') {
      chatMap.set(binding.address.value, user);
    }
    if (binding.address.channel === 'feishu' && binding.address.kind === 'feishu_open_id') {
      openIdMap.set(binding.address.value, user);
    }
  }

  // Initial index build
  rebuildIndexes();

  function mergeModelOverrides(): void {
    // Merge tenant model overrides from agent-models.json into in-memory ccModel
    // so that routing.ccModel reflects the effective model for each agent.
    // Three states: undefined → keep relay.json; null → explicit default (clear ccModel);
    // string → set ccModel. The null branch is what makes `/model default` survive restart.
    for (const user of userMap.values()) {
      for (const agent of user.agents) {
        const override = loadTenantModelOverride(user.userId, agent.id);
        if (override === undefined) continue;
        if (override === null) delete agent.ccModel;
        else agent.ccModel = override;
      }
    }
  }
  mergeModelOverrides();

  return {
    resolveUser(chatId: string): UserConfig | undefined {
      return chatMap.get(chatId);
    },

    getActiveAgent(userId: string): AgentConfig | undefined {
      const activeId = activeAgentMap.get(userId);
      if (!activeId) return undefined;
      return agentLookup.get(formatConversationPoolKey({ userId, agentId: activeId }));
    },

    switchAgent(userId: string, agentId: string): AgentConfig | undefined {
      const user = userMap.get(userId);
      if (!user) return undefined;

      const agent = agentLookup.get(formatConversationPoolKey({ userId, agentId }));
      if (!agent) return undefined;

      activeAgentMap.set(userId, agentId);
      return agent;
    },

    poolKey(userId: string, agentId: string): string {
      return formatConversationPoolKey({ userId, agentId });
    },

    getAgents(userId: string): AgentConfig[] {
      const user = userMap.get(userId);
      if (!user) return [];
      return user.agents;
    },

    getAllUsers(): UserConfig[] {
      return Array.from(userMap.values());
    },

    resetSession(userId: string, agentId: string, channelScope?: 'feishu' | 'tg'): string | undefined {
      const agent = agentLookup.get(formatConversationPoolKey({ userId, agentId }));
      if (!agent) return undefined;
      if (channelScope === 'feishu' && !agent.feishu) return undefined;
      const newId = crypto.randomUUID();

      if ((channelScope === 'feishu' || (!channelScope && preferredSessionField(agent) === 'feishuSessionId')) && agent.feishu) {
        agent.feishu.sessionId = newId;
      } else {
        agent.sessionId = newId;
      }
      return newId;
    },

    overrideSessionId(userId: string, agentId: string, sessionId: string, channelScope?: 'feishu' | 'tg'): void {
      const agent = agentLookup.get(formatConversationPoolKey({ userId, agentId }));
      if (!agent) return;

      // Scoped writes never cross channels: a feishu-scoped restore only ever
      // touches the feishu session field, and a tg-scoped restore only ever
      // touches the TG field. This is the guard that stops a stale TG session
      // from overwriting a fresh feishu session (memo 2026-06-26).
      if (channelScope === 'feishu') {
        if (agent.feishu) agent.feishu.sessionId = sessionId;
        return;
      }
      if (channelScope === 'tg') {
        agent.sessionId = sessionId;
        return;
      }

      // Unscoped: pick the agent's single preferred field. A feishu-only agent
      // (no botToken) writes its feishu session; everyone else writes the TG
      // session. Crucially, a dual-channel agent (botToken + feishu) does NOT
      // mirror onto agent.feishu.sessionId here — TG and feishu are independent
      // conversations with independent jsonls, and mirroring is exactly how a
      // stale TG session clobbered a fresh feishu session (memo 2026-06-26).
      if (preferredSessionField(agent) === 'feishuSessionId' && agent.feishu) {
        agent.feishu.sessionId = sessionId;
      } else {
        agent.sessionId = sessionId;
      }
    },

    setAgentRuntime(userId: string, agentId: string, runtime: ConversationRuntimeId, channelScope?: ConversationChannel): AgentConfig | undefined {
      const agent = agentLookup.get(formatConversationPoolKey({ userId, agentId }));

      if (!agent) return undefined;
      retainFeishuConversation(agent);
      const target = channelScope === 'feishu' ? agent.feishu : agent;
      if (!target) return undefined;
      target.runtime = runtime;
      target.conversationRuntimePreference = [runtime];
      const runtimeDefault = data.conversationRuntimeDefaults?.[runtime];
      const existing = target.conversationRuntime;
      if (existing || runtimeDefault) {
        const runtimeOptions = {
          ...(runtimeDefault?.runtimeOptions ? structuredClone(runtimeDefault.runtimeOptions) : {}),
          ...(existing?.runtimeOptions ?? {}),
        };
        target.conversationRuntime = {
          ...(runtimeDefault ? structuredClone(runtimeDefault) : {}),
          ...(existing ?? {}),
          model: existing?.model ?? (runtimeDefault?.model ? structuredClone(runtimeDefault.model) : undefined),
          isolationProfile: existing?.isolationProfile ?? runtimeDefault?.isolationProfile,
          ...(Object.keys(runtimeOptions).length > 0 ? { runtimeOptions } : {}),
          runtime,
        };
      }
      const tmpPath = `${configPath}.tmp`;
      writeFileSync(tmpPath, `${JSON.stringify(data, null, 2)}\n`, { mode: 0o600 });
      renameSync(tmpPath, configPath);
      return channelConversationAgent(agent, channelScope ?? 'tg');
    },

    getConversationRuntimeDefault(runtime: ConversationRuntimeId): ConversationRuntimeDefault | undefined {
      const runtimeDefault = data.conversationRuntimeDefaults?.[runtime];
      return runtimeDefault ? structuredClone(runtimeDefault) : undefined;
    },

    setAgentConversationModel(userId: string, agentId: string, model: ConversationModelRoute, channelScope?: ConversationChannel): AgentConfig | undefined {
      const agent = agentLookup.get(formatConversationPoolKey({ userId, agentId }));
      if (!agent || !channelConversationAgent(agent, channelScope ?? 'tg').conversationRuntime) return undefined;
      retainFeishuConversation(agent);
      const target = channelScope === 'feishu' ? agent.feishu : agent;
      if (!target?.conversationRuntime) return undefined;
      target.conversationRuntime.model = structuredClone(model);

      const tmpPath = `${configPath}.tmp`;
      writeFileSync(tmpPath, `${JSON.stringify(data, null, 2)}\n`, { mode: 0o600 });
      renameSync(tmpPath, configPath);
      return channelConversationAgent(agent, channelScope ?? 'tg');
    },

    setAgentModel(userId: string, agentId: string, model: string | undefined, channelScope?: ConversationChannel): AgentConfig | undefined {
      const agent = agentLookup.get(formatConversationPoolKey({ userId, agentId }));

      if (!agent) return undefined;
      retainFeishuConversation(agent);
      if (channelScope === 'feishu') {
        if (!agent.feishu) return undefined;
        agent.feishu.ccModel = model ?? null;
      } else {
        // Persist as explicit default (null), so subsequent restarts know to clear
        // relay.json's ccModel rather than silently revert. undefined here is the
        // user-facing "default" intent — we encode it as null in the override file.
        writeTenantModelOverride(userId, agentId, model === undefined ? null : model);
        // Also update in-memory ccModel for backward compat (relay.json not written)
        if (model === undefined) delete agent.ccModel;
        else agent.ccModel = model;
      }
      if (agent.feishu) {
        const tmpPath = `${configPath}.tmp`;
        writeFileSync(tmpPath, `${JSON.stringify(data, null, 2)}\n`, {mode: 0o600});
        renameSync(tmpPath, configPath);
      }
      return channelConversationAgent(agent, channelScope ?? 'tg');
    },

    getAgentModel(userId: string, agentId: string, channelScope?: ConversationChannel): string | undefined {
      const scoped = agentLookup.get(formatConversationPoolKey({ userId, agentId }));
      if (channelScope === 'feishu' && scoped?.feishu && Object.hasOwn(scoped.feishu, 'ccModel')) return scoped.feishu.ccModel ?? undefined;
      // Priority: tenant override > relay.json ccModel.
      // override === null means "explicit default" → no model (undefined).
      const tenantOverride = loadTenantModelOverride(userId, agentId);
      if (tenantOverride === null) return undefined;
      if (typeof tenantOverride === 'string' && tenantOverride) return tenantOverride;
      const agent = agentLookup.get(formatConversationPoolKey({ userId, agentId }));
      return agent?.ccModel;
    },

    resolveByBotAgent(agentId: string, chatId: string): { user: UserConfig; agent: AgentConfig } | undefined {
      // Find user with this chatId that has a TG agent with this id
      const user = chatMap.get(chatId);
      const agent = user?.agents.find(a => a.id === agentId && supportsTelegram(a));
      if (user && agent) return { user, agent };
      return undefined;
    },

    resolveByFeishuAgent(agentId: string, openId: string): { user: UserConfig; agent: AgentConfig } | undefined {
      const user = openIdMap.get(openId);
      if (!user) return undefined;
      const agent = user.agents.find(a => a.id === agentId && supportsFeishu(a));
      if (!agent) return undefined;
      return { user, agent: channelConversationAgent(agent, 'feishu') };
    },

    getAllAgentBots(): Array<{ agent: AgentConfig; authorizedChatIds: string[] }> {
      // Only TG agents with botToken
      const botMap = new Map<string, { agent: AgentConfig; chatIds: string[] }>();
      const push = (agent: AgentConfig, chatId?: string): void => {
        const existing = botMap.get(agent.id);
        if (existing) {
          if (chatId) existing.chatIds.push(chatId);
        } else {
          botMap.set(agent.id, { agent, chatIds: chatId ? [chatId] : [] });
        }
      };
      for (const user of userMap.values()) {
        for (const agent of user.agents) {
          if (!supportsTelegram(agent)) continue;
          push(agent, user.chatId);
        }
      }
      for (const binding of (data.bindings ?? [])) {
        if (binding.status !== 'active' || binding.address.channel !== 'telegram' || binding.address.kind !== 'telegram_chat') continue;
        const user = userMap.get(binding.principalId);
        const agent = user?.agents.find(candidate => candidate.id === binding.address.botId && supportsTelegram(candidate));
        if (agent) push(agent, binding.address.value);
      }
      return Array.from(botMap.values()).map(e => ({
        agent: e.agent,
        authorizedChatIds: [...new Set(e.chatIds)],
      }));
    },

    getAllFeishuBots(): Array<{ agent: AgentConfig; authorizedOpenIds: string[] }> {
      const botMap = new Map<string, { agent: AgentConfig; openIds: string[] }>();
      const push = (agent: AgentConfig, openId?: string): void => {
        const existing = botMap.get(agent.id);
        if (existing) {
          if (openId) existing.openIds.push(openId);
        } else {
          botMap.set(agent.id, { agent, openIds: openId ? [openId] : [] });
        }
      };
      for (const user of userMap.values()) {
        for (const agent of user.agents) {
          if (!supportsFeishu(agent)) continue;
          // openId: agent-level (per-app) preferred, user-level fallback
          const openId = agent.feishuOpenId ?? user.feishuOpenId;
          push(agent, openId);
        }
      }
      for (const binding of (data.bindings ?? [])) {
        if (binding.status !== 'active' || binding.address.channel !== 'feishu' || binding.address.kind !== 'feishu_open_id') continue;
        const user = userMap.get(binding.principalId);
        const agent = user?.agents.find(candidate => candidate.id === binding.address.botId && supportsFeishu(candidate));
        if (agent) push(agent, binding.address.value);
      }
      return Array.from(botMap.values()).map(e => ({
        agent: channelConversationAgent(e.agent, 'feishu'),
        authorizedOpenIds: [...new Set(e.openIds)],
      }));
    },

    getGatewayBindings(): ChannelBindingRecord[] {
      return data.bindings ?? [];
    },

    upsertGatewayBinding(binding: ChannelBindingRecord): boolean {
      if (!userMap.has(binding.principalId)) return false;
      const records = data.bindings ?? [];
      const existingIndex = records.findIndex(candidate => candidate.id === binding.id);
      if (existingIndex >= 0) records[existingIndex] = binding;
      else records.push(binding);
      data.bindings = records;
      indexBinding(binding);
      return true;
    },

    getAgentChannel(agent: AgentConfig): 'telegram' | 'feishu' {
      if (agent.channel) return agent.channel;
      return agent.botToken ? 'telegram' : 'feishu';
    },

    indexFeishuOpenId(agentId: string, openId: string): boolean {
      // Find the agent and update both the config object and the openIdMap
      for (const user of userMap.values()) {
        const agent = user.agents.find(a => a.id === agentId);
        if (agent) {
          agent.feishuOpenId = openId;
          openIdMap.set(openId, user);
          return true;
        }
      }
      return false;
    },

    reload(): { users: number; bindings: number } {
      const nextRaw = readFileSync(configPath, 'utf-8');
      const next = JSON.parse(nextRaw) as RelayConfig;
      // A malformed commit-repository or domain list fails the reload and keeps the running configuration.
      for (const user of next.users) for (const agent of user.agents) validateAgentRepoAccess(agent);
      // relay.json supplies configuration, while running/reset/restored session
      // identities belong to each channel. Reload must not undo those identities.
      for (const user of next.users) {
        for (const agent of user.agents) {
          const current = agentLookup.get(`${user.userId}:${agent.id}`);
          if (agent.botToken && current?.botToken && current.sessionId) agent.sessionId = current.sessionId;
          if (agent.feishu && current?.feishu?.sessionId) agent.feishu.sessionId = current.feishu.sessionId;
        }
      }
      raw = nextRaw;
      data = next;
      rebuildIndexes();
      mergeModelOverrides();
      return { users: userMap.size, bindings: (data.bindings ?? []).length };
    },

    getSshAlias(): string | undefined {
      return (data as unknown as Record<string, unknown>).sshAlias as string | undefined;
    },
  };
}
