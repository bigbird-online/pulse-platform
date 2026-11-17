import {readFileSync} from 'node:fs';
import {createRouter} from './router.js';
import {parseConversationPoolKey} from './conversation-address.js';
import {channelConversationAgent} from './channel-conversation.js';
import {conversationLaunch} from './conversation-launch.js';
import {buildPiCommand, type PiRuntimeManifest} from './pi-runtime-config.js';
import type {PersistedState} from './types.js';

/** Build the same retained launch as channel intake before stopping the old
 * Relay. No child, model request, message, state update or lifecycle operation.
 * Profile roots come from reviewed Host deployment, never from session state. */
export function verifyRetainedPiConversations(configPath: string, statePath: string,
  manifest: PiRuntimeManifest, environment: NodeJS.ProcessEnv): {verified: number} {
  const router = createRouter(configPath);
  const state = JSON.parse(readFileSync(statePath, 'utf8')) as PersistedState;
  let verified = 0;
  for (const [poolKey, session] of Object.entries(state.sessions)) {
    if (session.runtime !== 'pi' || !session.sessionId) continue;
    const address = parseConversationPoolKey(poolKey);
    if (!address) throw new Error('Retained Pi conversation address is invalid');
    const configured = router.getAgents(address.userId).find(agent => agent.id === address.agentId);
    if (!configured) continue; // Retired bot configuration has no current consumer.
    const agent = channelConversationAgent(configured, address.channelScope ?? 'tg');
    const profileId = agent.conversationRuntime?.isolationProfile;
    if (!profileId || !manifest.profiles[profileId]?.workItemCoreHost) continue;
    const config = conversationLaunch({poolKey, agent, session,
      channel: address.channelScope === 'feishu' ? 'feishu' : 'telegram', intent: 'inbound'});
    const spec = buildPiCommand(config, manifest, environment);
    const hasHistory = session.turnCount > 0 || !!session.activeWorkItemRuntimeBinding
      || !!session.terminalWorkItemRuntimeBinding;
    if (hasHistory && !spec.args.includes('--session')) {
      throw new Error('Retained Pi conversation transcript is unavailable; deployment cannot replace it');
    }
    verified++;
  }
  return {verified};
}
