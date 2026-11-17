import type {AgentConfig, ConversationRuntimeRoute, ConversationRuntimeId} from './types.js';
import {resolvePulseRuntimePriority} from './codex-defaults.js';

export type ConversationChannel = 'tg' | 'feishu';

/** The top-level route remains the Telegram/legacy route. Feishu inherits it
 * until a channel changes configuration, when its effective route is retained. */
export interface ChannelConversationConfig {
  runtime?: ConversationRuntimeId;
  conversationRuntimePreference?: ConversationRuntimeId[];
  conversationRuntime?: ConversationRuntimeRoute | null;
  ccModel?: string | null;
}

export function channelConversationAgent(agent: AgentConfig, channel: ConversationChannel): AgentConfig {
  if (channel !== 'feishu' || !agent.feishu) return agent;
  const config = agent.feishu;
  if (!['runtime', 'conversationRuntimePreference', 'conversationRuntime', 'ccModel'].some(key => Object.hasOwn(config, key))) return agent;
  return {
    ...agent,
    runtime: config.runtime ?? agent.runtime,
    conversationRuntimePreference: config.conversationRuntimePreference ?? agent.conversationRuntimePreference,
    conversationRuntime: Object.hasOwn(config, 'conversationRuntime')
      ? config.conversationRuntime ?? undefined : agent.conversationRuntime,
    ccModel: Object.hasOwn(config, 'ccModel') ? config.ccModel ?? undefined : agent.ccModel,
  };
}

/** Freeze the inherited sibling route before either channel writes; null
 * records an explicit absence, so a later TG route cannot become a fallback. */
export function retainFeishuConversation(agent: AgentConfig): void {
  if (!agent.feishu) return;
  const effective = channelConversationAgent(agent, 'feishu');
  agent.feishu.runtime ??= effective.conversationRuntime?.runtime ?? effective.conversationRuntimePreference?.[0]
    ?? effective.runtime ?? resolvePulseRuntimePriority()[0];
  agent.feishu.conversationRuntimePreference ??= [...(effective.conversationRuntimePreference ?? [agent.feishu.runtime])];
  if (!Object.hasOwn(agent.feishu, 'conversationRuntime')) {
    agent.feishu.conversationRuntime = effective.conversationRuntime ? structuredClone(effective.conversationRuntime) : null;
  }
  if (!Object.hasOwn(agent.feishu, 'ccModel')) agent.feishu.ccModel = effective.ccModel ?? null;
}
