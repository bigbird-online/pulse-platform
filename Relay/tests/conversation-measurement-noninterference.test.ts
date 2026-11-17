import { describe, expect, it, vi } from 'vitest';
import { conversationLaunch } from '../src/conversation-launch.js';
import { acquireConversation, conversationConsumerProjection } from '../src/conversation-acquisition.js';
import { assertAdoptedConversationRuntime, buildRuntimeUsageBinding, persistedRuntimeUsageBinding,
  persistedWorkItemSession, persistedRuntimeWorkspace, runtimeAuthorityRef } from '../src/runtime-usage-binding.js';
import type { AgentConfig, PersistedSession, StateManager } from '../src/types.js';
import type { CommandRuntime } from '../src/commands.js';

const agent: AgentConfig = { id: 'pulse', name: 'Pulse', emoji: 'P', runtime: 'pi', workspace: '/default' };
const binding = buildRuntimeUsageBinding({ workItemId: 'wi_20990101_271', agentId: 'pulse', phase: 'work_item',
  epoch: 1, runtime: 'pi', procurementSource: 'unknown', authorityRef: runtimeAuthorityRef('original') });
function saved(): PersistedSession {
  return { poolKey: 'human:pulse', agentId: 'pulse', runtime: 'pi', sessionId: 'original',
    status: 'active', lastActivity: 1, turnCount: 2, maxTurns: 100,
    activeWorkItemRuntimeBinding: { schema: 'pulse_active_work_item_runtime_binding/v1',
      workItemId: binding.work_item_id, sessionId: 'original', sessionRef: runtimeAuthorityRef('original'),
      runtime: 'pi', workspace: '/registered/work', usageBinding: binding } };
}
const measurements = [undefined, null, {}, { ...binding, binding_sha256: 'bad' },
  { ...binding, procurement_source: 'bad' }, { ...binding, epoch: 0 },
  buildRuntimeUsageBinding({ workItemId: 'wi_20990101_999', agentId: 'pulse', phase: 'work_item', epoch: 1,
    runtime: 'pi', procurementSource: 'unknown' }), binding];

describe('ordinary conversation measurement noninterference', () => {
  it.each(measurements.map((value, index) => ({ value, index })))('does not admit, deny or reroute from measurement $index', ({ value }) => {
    const session = saved();
    session.activeWorkItemRuntimeBinding!.usageBinding = value as never;
    session.activeWorkItemRuntimeBinding!.measurementBaselineReference = { invalid: 'old observation' } as never;
    const before = JSON.stringify(session);
    expect(persistedWorkItemSession(session)).toEqual({ runtime: 'pi', sessionId: 'original' });
    expect(persistedRuntimeWorkspace(session, 'pi', 'original', '/default')).toBe('/registered/work');
    expect(() => assertAdoptedConversationRuntime(session, {
      runtime: 'pi', sessionId: 'original', usageBindingSha256: 'unrelated-observation', workspace: '/registered/work',
    })).not.toThrow();
    for (const channel of ['telegram', 'feishu'] as const) {
      for (const intent of ['inbound', 'recovery', 'restart'] as const) {
        const launch = conversationLaunch({ poolKey: session.poolKey, agent, session, channel, intent });
        expect(launch).toMatchObject({ runtime: 'pi', sessionId: 'original', workspace: '/registered/work', resume: true });
        expect(launch.computeCostBinding).toBeUndefined();
        expect(launch.measurementBaselineReference).toBeUndefined();
      }
    }
    expect(JSON.stringify(session)).toBe(before);
  });

  it.each(['binding', 'baseline'] as const)('measurement-only %s updates cannot cancel a cold start', async kind => {
    const session = saved(), before = conversationConsumerProjection(session);
    type Process = NonNullable<ReturnType<CommandRuntime['get']>>;
    const proc = { runtime: 'pi', sessionId: 'original', workspace: '/registered/work', state: 'ready' } as Process;
    const runtime = { get: vi.fn(() => undefined), kill: vi.fn(), spawn: vi.fn(async () => {
      if (kind === 'binding') session.activeWorkItemRuntimeBinding!.usageBinding = {} as never;
      else session.activeWorkItemRuntimeBinding!.measurementBaselineReference = {} as never;
      return proc;
    }) };
    await expect(acquireConversation({ poolKey: session.poolKey, agent, runtime,
      stateManager: { getSession: () => session } as unknown as StateManager,
      channel: 'feishu', intent: 'recovery' })).resolves.toBe(proc);
    expect(conversationConsumerProjection(session)).toBe(before);
    expect(runtime.spawn).toHaveBeenCalledTimes(1);
    expect(runtime.kill).not.toHaveBeenCalled();
  });

  it.each(['session', 'runtime', 'reference', 'schema'] as const)('ready metering cannot override wrong %s identity', field => {
    const session = saved(), active = session.activeWorkItemRuntimeBinding!;
    if (field === 'session') active.sessionId = 'other';
    if (field === 'runtime') active.runtime = 'cc';
    if (field === 'reference') active.sessionRef = runtimeAuthorityRef('other');
    if (field === 'schema') active.schema = 'invalid' as never;
    expect(() => persistedWorkItemSession(session)).toThrow();
    expect(() => persistedRuntimeWorkspace(session, 'pi', 'original', '/default')).toThrow();
    expect(() => assertAdoptedConversationRuntime(session, { runtime: 'pi', sessionId: 'original' })).toThrow();
  });

  it('retains actual pending admission, live runtime and strict measurement-only validation', () => {
    const session = saved();
    expect(() => assertAdoptedConversationRuntime(session, { runtime: 'cc', sessionId: 'original' })).toThrow();
    expect(() => assertAdoptedConversationRuntime({ ...session, activeWorkItemRuntimeBinding: undefined,
      pendingWorkItemEntry: { sessionId: 'original', launchAuthorityState: 'prepared' } as never },
      { runtime: 'pi', sessionId: 'original' })).toThrow('not adopted');
    session.activeWorkItemRuntimeBinding!.usageBinding = {} as never;
    expect(() => persistedRuntimeUsageBinding(session, 'pi', 'original')).toThrow();
  });
});
