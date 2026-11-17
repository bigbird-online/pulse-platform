import { agentRepoAccess, harnessPrincipalForAgent, type AgentConfig, type ConversationRuntimeId, type PersistedSession } from './types.js';
import type { ConversationSpawnConfig } from './conversation-runtime.js';
import { persistedRuntimeWorkspace, persistedWorkItemSession } from './runtime-usage-binding.js';

type LaunchIntent = 'inbound' | 'recovery' | 'restart' | 'pi-command';

/** Resolve the whole launch identity together. Channels must not independently
 * choose a Session, workspace or runtime from different sources. Historical
 * measurement bindings stay saved; they are not startup admission inputs.
 * This selects existing evidence; native spawn still performs admission. */
export function conversationLaunch(input: {
  poolKey: string;
  agent: AgentConfig;
  session?: PersistedSession;
  channel: 'telegram' | 'feishu';
  intent: LaunchIntent;
  runtimePreference?: ConversationRuntimeId[];
  allowedTools?: string[];
}): ConversationSpawnConfig {
  const { agent, session, intent, channel } = input;
  const bound = persistedWorkItemSession(session);
  const configuredRuntime = input.runtimePreference?.[0] ?? agent.runtime;
  const runtime = bound?.runtime ?? (
    intent === 'pi-command' ? 'pi'
      : intent === 'recovery' ? session?.runtime ?? configuredRuntime ?? 'cc'
        : intent === 'restart' ? agent.conversationRuntime?.runtime ?? agent.runtime ?? session?.runtime ?? 'cc'
          : configuredRuntime ?? session?.runtime ?? 'cc'
  );

  // The persisted channel owns its current Session after reset/recovery.
  // Configuration only supplies a missing identity; Telegram never borrows the
  // Feishu Session. An accepted WI identity still wins as a unit.
  const channelSession = channel === 'feishu'
    ? agent.feishu?.sessionId ?? agent.sessionId : agent.sessionId;
  const sessionId = bound?.sessionId ?? (
    intent === 'inbound' || intent === 'recovery'
      ? session?.sessionId || channelSession || ''
      : session?.sessionId ?? channelSession ?? ''
  );
  if (intent === 'pi-command') {
    if (agent.conversationRuntime?.runtime !== 'pi' || runtime !== 'pi') {
      throw new Error('native Pi command requires the current Pi conversation');
    }
    if (!sessionId) throw new Error('Pi session ID missing');
  }

  return {
    tenantId: input.poolKey,
    runtime,
    conversationRuntimePreference: bound ? [bound.runtime]
      : intent === 'pi-command' ? ['pi'] : input.runtimePreference ?? agent.conversationRuntimePreference,
    conversationRuntime: agent.conversationRuntime,
    runtimeSessionBinding: session ? {
      runtime: session.runtime ?? (intent === 'recovery' && channel === 'feishu' ? runtime : intent === 'pi-command' ? 'pi' : 'cc'),
      sessionId: session.sessionId,
    } : undefined,
    workspace: persistedRuntimeWorkspace(session, runtime, sessionId, agent.workspace),
    sessionId,
    codexThreadId: session?.codexThreadId,
    maxTurns: agent.maxTurns,
    allowedTools: input.allowedTools ?? agent.allowedTools,
    ccModel: agent.ccModel,
    ccMode: agent.ccMode,
    ccSdkProviderMode: agent.ccSdkProviderMode,
    codexYoloDefault: agent.codexYoloDefault === true,
    sessionWiGuard: agent.sessionWiGuard === true,
    harnessAgentId: harnessPrincipalForAgent(agent),
    ccWriteBoundary: agent.ccWriteBoundary === true,
    repoAccess: agentRepoAccess(agent),
    visionModel: agent.visionModel,
    // The adapter chooses resume vs a fresh native file. Having lost the
    // in-memory process is not a reason to skip persisted transcript repair.
    resume: sessionId.length > 0,
    linuxUser: agent.linuxUser,
    userHome: agent.userHome,
    expectedLastActivity: session?.lastActivity,
  };
}
