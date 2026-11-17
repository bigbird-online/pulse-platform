import { resolvePulseRuntimePriority } from './codex-defaults.js';
import {channelConversationAgent} from './channel-conversation.js';
import type {
  AgentConfig,
  ChannelAddress,
  ChannelBinding,
  ChannelBindingRecord,
  ConversationRuntimeId,
  GatewayEvent,
  Principal,
  RelayConfig,
  RoutePolicyResult,
  UserConfig,
} from './types.js';

export interface GatewayBindingView {
  principals: Principal[];
  bindings: ChannelBinding[];
}

export type InboundRouteResolution =
  | { status: 'ok'; route: RoutePolicyResult }
  | { status: 'pairing_required'; address: ChannelAddress; reason: 'unbound-address' }
  | { status: 'access_denied'; address: ChannelAddress; reason: 'agent-not-allowed' | 'agent-not-found' };

export interface InboundRouteInput {
  users: UserConfig[];
  bindings?: ChannelBindingRecord[];
  event: GatewayEvent;
  targetAgentId: string;
}

export function normalizeTelegramChatAddress(chatId: string | number, botId?: string): ChannelAddress {
  return {
    channel: 'telegram',
    kind: 'telegram_chat',
    value: String(chatId),
    botId,
  };
}

export function normalizeFeishuOpenIdAddress(openId: string, appId: string, botId?: string): ChannelAddress {
  return {
    channel: 'feishu',
    kind: 'feishu_open_id',
    value: openId,
    botId,
    appId,
  };
}

export function channelAddressKey(address: ChannelAddress): string {
  return [
    address.channel,
    address.kind,
    address.appId ?? '',
    address.botId ?? '',
    address.value,
  ].join(':');
}

function unique<T>(values: T[]): T[] {
  return [...new Set(values)];
}

function runtimePreferenceFor(agent: AgentConfig): ConversationRuntimeId[] {
  const requested = agent.conversationRuntimePreference?.length
    ? agent.conversationRuntimePreference
    : [agent.runtime ?? resolvePulseRuntimePriority()[0]];
  // Preserve every supported conversation runtime so the gateway route consumed
  // by TG and Feishu does not silently collapse native runtimes to legacy cc.
  const valid = requested.filter(
    (runtime): runtime is ConversationRuntimeId => runtime === 'cc'
      || runtime === 'codex'
      || runtime === 'cc-sdk'
      || runtime === 'pi',
  );
  return unique(valid.length ? valid : ['cc']);
}

function allowedAgentsFor(user: UserConfig): string[] {
  return unique(user.agents.map(agent => agent.id));
}

function supportsTelegram(agent: AgentConfig): boolean {
  return !!agent.botToken;
}

function supportsFeishu(agent: AgentConfig): boolean {
  return !!agent.feishu;
}

function principalFor(user: UserConfig): Principal {
  return {
    id: user.userId,
    type: 'human',
    tenantId: user.userId,
  };
}

function bindingId(parts: Array<string | undefined>): string {
  return parts.filter((part): part is string => !!part).join(':');
}

export function createGatewayBindingView(config: RelayConfig): GatewayBindingView {
  return createGatewayBindingViewFromUsers(config.users, config.bindings);
}

function bindingRecordToViewBinding(record: ChannelBindingRecord, users: UserConfig[]): ChannelBinding {
  const user = users.find(candidate => candidate.userId === record.principalId);
  const defaultAgent = user?.agents.find(agent => agent.id === record.defaultAgentId)
    ?? user?.agents.find(agent => record.allowedAgentIds.includes(agent.id));
  return {
    id: record.id,
    address: record.address,
    principalId: record.principalId,
    tenantId: record.tenantId,
    workspaceId: defaultAgent?.workspace ?? '',
    defaultAgentId: record.defaultAgentId,
    allowedAgentIds: record.allowedAgentIds,
    defaultRuntimePreference: record.defaultRuntimePreference,
    auditTags: unique(['relay-json-bindings', ...(record.auditTags ?? [])]),
  };
}

export function createGatewayBindingViewFromUsers(
  users: UserConfig[],
  explicitBindings: ChannelBindingRecord[] = [],
): GatewayBindingView {
  const principals = new Map<string, Principal>();
  const bindings: ChannelBinding[] = [];
  const explicitAddressKeys = new Set(explicitBindings.map(binding => channelAddressKey(binding.address)));

  for (const user of users) {
    const principal = principalFor(user);
    principals.set(principal.id, principal);
  }

  for (const record of explicitBindings) {
    if (record.status !== 'active') continue;
    const user = users.find(candidate => candidate.userId === record.principalId);
    if (user) principals.set(user.userId, principalFor(user));
    bindings.push(bindingRecordToViewBinding(record, users));
  }

  for (const user of users) {
    const principal = principalFor(user);
    const allowedAgentIds = allowedAgentsFor(user);

    for (const agent of user.agents) {
      if (supportsTelegram(agent) && user.chatId) {
        const address = normalizeTelegramChatAddress(user.chatId, agent.id);
        if (explicitAddressKeys.has(channelAddressKey(address))) continue;
        bindings.push({
          id: bindingId(['binding', channelAddressKey(address), user.userId]),
          address,
          principalId: principal.id,
          tenantId: principal.tenantId,
          workspaceId: agent.workspace,
          defaultAgentId: user.defaultAgent,
          allowedAgentIds,
          defaultRuntimePreference: runtimePreferenceFor(agent),
          auditTags: ['legacy-relay-json', 's1-inbound'],
        });
      }

      if (supportsFeishu(agent) && agent.feishu) {
        const openId = agent.feishuOpenId ?? user.feishuOpenId;
        if (!openId) continue;
        const address = normalizeFeishuOpenIdAddress(openId, agent.feishu.appId, agent.id);
        if (explicitAddressKeys.has(channelAddressKey(address))) continue;

        bindings.push({
          id: bindingId(['binding', channelAddressKey(address), user.userId]),
          address,
          principalId: principal.id,
          tenantId: principal.tenantId,
          workspaceId: agent.workspace,
          defaultAgentId: user.defaultAgent,
          allowedAgentIds,
          defaultRuntimePreference: runtimePreferenceFor(channelConversationAgent(agent, 'feishu')),
          auditTags: ['legacy-relay-json', 's1-inbound'],
        });
      }
    }
  }

  return {
    principals: Array.from(principals.values()),
    bindings,
  };
}

function sessionIdFor(agent: AgentConfig): string {
  return agent.botToken ? agent.sessionId ?? agent.feishu?.sessionId ?? '' : agent.feishu?.sessionId ?? agent.sessionId ?? '';
}

function conversationKeyFor(binding: ChannelBinding, agentId: string): string {
  return `${binding.principalId}:${agentId}`;
}

export function resolveInboundRoute(input: InboundRouteInput): InboundRouteResolution {
  const view = createGatewayBindingViewFromUsers(input.users, input.bindings);
  const addressKey = channelAddressKey(input.event.address);
  const binding = view.bindings.find(candidate => channelAddressKey(candidate.address) === addressKey);
  if (!binding) {
    return { status: 'pairing_required', address: input.event.address, reason: 'unbound-address' };
  }

  if (!binding.allowedAgentIds.includes(input.targetAgentId)) {
    return { status: 'access_denied', address: input.event.address, reason: 'agent-not-allowed' };
  }

  const user = input.users.find(candidate => candidate.userId === binding.principalId);
  const configuredAgent = user?.agents.find(candidate => candidate.id === input.targetAgentId);
  const agent = configuredAgent && channelConversationAgent(configuredAgent, input.event.channel === 'feishu' ? 'feishu' : 'tg');
  if (!user || !agent) {
    return { status: 'access_denied', address: input.event.address, reason: 'agent-not-found' };
  }

  const principal: Principal = {
    id: user.userId,
    type: 'human',
    tenantId: user.userId,
  };
  const conversationKey = conversationKeyFor(binding, agent.id);

  return {
    status: 'ok',
    route: {
      principal,
      binding,
      agentId: agent.id,
      workspace: agent.workspace,
      sessionId: sessionIdFor(agent),
      runtimePreference: binding.defaultRuntimePreference ?? runtimePreferenceFor(agent),
      conversationKey,
      audit: {
        correlationId: input.event.id,
        source: 's1-inbound',
      },
    },
  };
}
