/**
 * A result's `session_id` is the Relay conversation's own session as the
 * runtime reports it (CC's print-mode UUID): follow it into the router and the
 * persisted state whenever it differs. A runtime whose native conversation is
 * not the Relay session (agy) reports that under its own field, never here.
 */
import { parseConversationPoolKey } from './conversation-address.js';
import type { Router } from './router.js';
import type { StateManager, StreamJsonMessage } from './types.js';

export function captureReportedSessionId(poolKey: string, msg: StreamJsonMessage,
  deps: { router: Pick<Router, 'getAgents' | 'overrideSessionId'>; stateManager: StateManager }): void {
  const ccSid = typeof msg.session_id === 'string' ? msg.session_id : undefined;
  if (!ccSid) return;
  const existing = deps.stateManager.getSession(poolKey);
  if (existing?.sessionId === ccSid) return;
  const previousSid = existing?.sessionId;
  const address = parseConversationPoolKey(poolKey);
  if (!address) return;
  const { userId, agentId, channelScope } = address;
  if (!deps.router.getAgents(userId).some(agent => agent.id === agentId)) return;
  // Router owns the channel-specific session field selection for both
  // scoped and legacy unscoped pool keys.
  deps.router.overrideSessionId(userId, agentId, ccSid, channelScope);
  // Also persist to state so it survives relay restart
  deps.stateManager.updateSession(poolKey, {
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
