import { agentRepoAccess, harnessPrincipalForAgent, type AgentConfig, type ConversationRuntimeId, type PersistedSession } from './types.js';
import type { ConversationSpawnConfig } from './conversation-runtime.js';
import { persistedRuntimeWorkspace, persistedWorkItemSession } from './runtime-usage-binding.js';
import { isAgyModel } from './agy-models.js';

type LaunchIntent = 'inbound' | 'recovery' | 'restart' | 'pi-command';

/**
 * The WI binding a /wi command gave the agy conversation of this Relay session, while the session
 * still resumes that native conversation: the core recognizes the holder by its conversation id,
 * so a binding never follows agy into another conversation. A binding that names no conversation
 * (a rollover successor) applies only before the session has one.
 */
export function agyConversationBinding(session: PersistedSession | undefined, sessionId: string) {
  const bound = session?.agyWorkItemBinding;
  if (!bound || bound.sessionId !== sessionId) return undefined;
  const saved = session?.agyConversation?.sessionId === sessionId ? session.agyConversation.conversationId : undefined;
  return bound.conversationId === saved ? bound.usageBinding : undefined;
}

/**
 * The runtime of a session a /wi switch saved for a WI (or a switch in flight names as its origin or
 * target), when the conversation stands on it: that native session resumes only in the runtime that
 * made it, whatever the channel now prefers. A session no switch saved follows the channel as before.
 */
export function switchedSessionRuntime(session: PersistedSession | undefined): ConversationRuntimeId | undefined {
  if (!session?.sessionId) return undefined;
  const pending = session.pendingWiSwitch;
  const units = [...Object.values(session.workItemSessions ?? {}).map((saved) => saved.unit),
    ...(pending ? [pending.dest, pending.origin] : [])];
  const unit = units.find((candidate) => candidate?.sessionId === session.sessionId);
  return unit ? unit.runtime ?? session.runtime ?? 'cc' : undefined;
}

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
  const switched = intent === 'pi-command' ? undefined : switchedSessionRuntime(session);
  const pinned = bound?.runtime ?? switched;
  const runtime = pinned ?? (
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
    conversationRuntimePreference: pinned ? [pinned]
      : intent === 'pi-command' ? ['pi'] : input.runtimePreference ?? agent.conversationRuntimePreference,
    // A structured route of another runtime would override the switched session's own runtime.
    conversationRuntime: !bound && switched && agent.conversationRuntime?.runtime !== switched
      ? undefined : agent.conversationRuntime,
    runtimeSessionBinding: session ? {
      runtime: session.runtime ?? (intent === 'recovery' && channel === 'feishu' ? runtime : intent === 'pi-command' ? 'pi' : 'cc'),
      sessionId: session.sessionId,
    } : undefined,
    workspace: persistedRuntimeWorkspace(session, runtime, sessionId, agent.workspace),
    sessionId,
    codexThreadId: session?.codexThreadId,
    maxTurns: agent.maxTurns,
    allowedTools: input.allowedTools ?? agent.allowedTools,
    // The channel's model is the Agent's; an agy model means nothing to a session /wi switch resumed in another runtime.
    ccModel: runtime !== 'agy' && isAgyModel(agent.ccModel) ? undefined : agent.ccModel,
    reasoningLevel: session?.reasoningLevel,
    ccMode: agent.ccMode,
    ccPrompt: agent.ccPrompt,
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
    // Only an agy conversation carries a WI binding from its launch; no other launch names the key.
    ...(runtime === 'agy' && agyConversationBinding(session, sessionId)
      ? { computeCostBinding: agyConversationBinding(session, sessionId) } : {}),
  };
}
