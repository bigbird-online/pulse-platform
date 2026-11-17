import { describe, expect, it } from 'vitest';
import { conversationLaunch } from '../src/conversation-launch.js';
import { buildRuntimeUsageBinding, runtimeAuthorityRef, usageRuntimeForConversation, type BoundConversationRuntime } from '../src/runtime-usage-binding.js';
import type { AgentConfig, PersistedSession } from '../src/types.js';

const agent: AgentConfig = {
  id: 'pulse', name: 'Pulse', emoji: 'P', workspace: '/default',
  runtime: 'cc', sessionId: 'stale-configured-session',
  linuxUser: 'tenant-pulse', userHome: '/home/tenant-pulse', ccSdkProviderMode: 'subscription',
};
const session: PersistedSession = {
  poolKey: 'person:pulse', agentId: 'pulse', runtime: 'pi', sessionId: 'native-session',
  status: 'active', lastActivity: 123, turnCount: 2, maxTurns: 1000,
};
function boundSession(runtime: BoundConversationRuntime = 'pi'): PersistedSession {
  const usageBinding = buildRuntimeUsageBinding({
    workItemId: 'wi_20990101_271', agentId: 'pulse', phase: 'work_item', epoch: 1,
    runtime: usageRuntimeForConversation(runtime), procurementSource: runtime === 'cc' || runtime === 'cc-sdk' ? 'claude_subscription' : 'chatgpt_subscription',
    authorityRef: runtimeAuthorityRef(session.sessionId), issuedAt: '2026-09-01T00:00:00Z',
  });
  return { ...session, runtime, activeWorkItemRuntimeBinding: {
    schema: 'pulse_active_work_item_runtime_binding/v1', workItemId: usageBinding.work_item_id,
    sessionId: session.sessionId, sessionRef: runtimeAuthorityRef(session.sessionId),
    runtime, workspace: '/registered/wi-worktree', usageBinding,
  } };
}

describe('conversation launch identity', () => {
  for (const runtime of ['pi', 'codex', 'cc', 'cc-sdk'] as const) {
    for (const channel of ['telegram', 'feishu'] as const) {
      for (const intent of ['inbound', 'recovery', 'restart'] as const) {
        it(`${runtime} ${channel} ${intent} retains one WI identity despite stale route defaults`, () => {
          const persisted = boundSession(runtime);
          const launch = conversationLaunch({ poolKey: session.poolKey, agent, session: persisted,
            channel, intent, runtimePreference: ['cc', 'codex'] });
          expect(launch).toMatchObject({
            runtime, conversationRuntimePreference: [runtime], sessionId: 'native-session',
            workspace: '/registered/wi-worktree',
            resume: true, expectedLastActivity: 123,
          });
          expect(launch.computeCostBinding).toBeUndefined();
          expect(launch.measurementBaselineReference).toBeUndefined();
        });
      }
    }
  }

  it('repairs a retained transcript even when the in-memory process disappeared', () => {
    expect(conversationLaunch({ poolKey: session.poolKey, agent: { ...agent, sessionId: undefined },
      session: { ...session, runtime: 'cc' }, channel: 'telegram', intent: 'inbound' }))
      .toMatchObject({ sessionId: session.sessionId, resume: true,
        linuxUser: agent.linuxUser, userHome: agent.userHome, ccSdkProviderMode: 'subscription' });
  });

  it('does not mark an unbound new conversation as a resume', () => {
    expect(conversationLaunch({ poolKey: session.poolKey, agent: { ...agent, sessionId: undefined },
      channel: 'telegram', intent: 'inbound' })).toMatchObject({ sessionId: '', resume: false });
  });

  it('rejects a split WI identity instead of falling back to the configured workspace', () => {
    const persisted = boundSession();
    persisted.sessionId = 'another-session';
    expect(() => conversationLaunch({ poolKey: session.poolKey, agent, session: persisted,
      channel: 'telegram', intent: 'restart' })).toThrow();
  });

  for (const channel of ['telegram', 'feishu'] as const) {
    for (const intent of ['inbound', 'recovery', 'restart'] as const) {
      it(`${channel} ${intent} retains the reset Session over stale configuration`, () => {
        const dualAgent = { ...agent, botToken: 'test-only-token', feishu: { sessionId: 'feishu-configured' } as never };
        expect(conversationLaunch({ poolKey: session.poolKey, agent: dualAgent, session,
          channel, intent }).sessionId).toBe(session.sessionId);
      });
    }
  }

  it('does not borrow a Feishu identity for an unconfigured Telegram conversation', () => {
    const dualAgent = { ...agent, sessionId: undefined, feishu: { sessionId: 'feishu-configured' } as never };
    expect(conversationLaunch({ poolKey: session.poolKey, agent: dualAgent,
      channel: 'telegram', intent: 'restart' })).toMatchObject({ sessionId: '', resume: false });
    expect(conversationLaunch({ poolKey: session.poolKey, agent: dualAgent,
      channel: 'feishu', intent: 'restart' }).sessionId).toBe('feishu-configured');
  });

  it('native Pi commands require a Pi route and an existing session', () => {
    expect(() => conversationLaunch({ poolKey: session.poolKey, agent, session,
      channel: 'telegram', intent: 'pi-command' })).toThrow('current Pi conversation');
  });

  it('retains the configured runtime for legacy Feishu recovery without a stored runtime', () => {
    expect(conversationLaunch({ poolKey: session.poolKey, agent: { ...agent, runtime: 'pi' },
      session: { ...session, runtime: undefined }, channel: 'feishu', intent: 'recovery' }))
      .toMatchObject({ runtime: 'pi', runtimeSessionBinding: { runtime: 'pi', sessionId: session.sessionId } });
  });
});

describe('conversation launch repository access', () => {
  it('carries the Agent commit repositories and domains, and nothing when it configures none', () => {
    const launch = (extra: Partial<AgentConfig>) => conversationLaunch({ poolKey: 'person:pulse',
      agent: { ...agent, ccWriteBoundary: true, ...extra }, channel: 'telegram', intent: 'inbound' });
    expect(launch({ commitRepos: ['/repo'], allowedDomains: ['github.com'] }).repoAccess)
      .toEqual({ commitRepos: ['/repo'], allowedDomains: ['github.com'] });
    expect(launch({ allowedDomains: ['github.com'] }).repoAccess).toEqual({ commitRepos: [], allowedDomains: ['github.com'] });
    expect(launch({}).repoAccess).toBeUndefined();
    expect(launch({ commitRepos: [], allowedDomains: [] }).repoAccess).toBeUndefined();
  });
});
