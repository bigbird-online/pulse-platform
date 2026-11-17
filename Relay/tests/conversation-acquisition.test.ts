import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { acquireConversation } from '../src/conversation-acquisition.js';
import { intakeAwareRuntime } from '../src/channels/intake-journal.js';
import { buildRuntimeUsageBinding, runtimeAuthorityRef } from '../src/runtime-usage-binding.js';
import type { AgentConfig, PersistedSession, StateManager } from '../src/types.js';
import type { CommandRuntime } from '../src/commands.js';

function deferred() {
  let resolve!: () => void;
  const promise = new Promise<void>(done => { resolve = done; });
  return { promise, resolve };
}

function fixture() {
  const poolKey = 'human:pulse';
  const session: PersistedSession = { poolKey, agentId: 'pulse', runtime: 'pi', sessionId: 'original',
    status: 'active', lastActivity: 1, turnCount: 2, maxTurns: 100 };
  const agent: AgentConfig = { id: 'pulse', name: 'Pulse', emoji: 'P', workspace: '/workspace', runtime: 'pi' };
  type Process = NonNullable<ReturnType<CommandRuntime['get']>>;
  const processes = new Map<string, Process>();
  const makeProcess = (sessionId = session.sessionId) => ({ runtime: 'pi', sessionId, state: 'ready',
    usageBindingSha256: session.activeWorkItemRuntimeBinding?.usageBinding.binding_sha256 }) as Process;
  const runtime = {
    get: vi.fn((key: string) => processes.get(key)),
    spawn: vi.fn(async (config: Parameters<CommandRuntime['spawn']>[0]) => {
      const proc = makeProcess(config.sessionId);
      processes.set(config.tenantId, proc);
      return { ...proc }; // the real runtime manager returns a copy too
    }),
    kill: vi.fn(async (key: string) => { processes.delete(key); }),
  };
  const stateManager = { getSession: () => session } as unknown as StateManager;
  const input = { poolKey, agent, runtime, stateManager, channel: 'telegram' as const, intent: 'inbound' as const };
  const bind = () => {
    const usageBinding = buildRuntimeUsageBinding({ workItemId: 'wi_20990101_271', agentId: 'pulse',
      phase: 'work_item', epoch: 1, runtime: 'pi', procurementSource: 'unknown',
      authorityRef: runtimeAuthorityRef(session.sessionId) });
    session.activeWorkItemRuntimeBinding = { schema: 'pulse_active_work_item_runtime_binding/v1',
      workItemId: usageBinding.work_item_id, sessionId: session.sessionId, sessionRef: usageBinding.authority_ref,
      runtime: 'pi', workspace: '/registered/wi-worktree', usageBinding };
  };
  return { input, session, runtime, processes, makeProcess, bind };
}

describe('conversation acquisition ownership', () => {
  beforeEach(() => { vi.stubEnv('PULSE_SCL_BIN', ''); });
  afterEach(() => { vi.unstubAllEnvs(); });

  it('one address queue for the runtime and its channel wrapper: a cold start is not repeated through the wrapper', async () => {
    const f = fixture(), gate = deferred(), entered = deferred();
    const spawn = f.runtime.spawn.getMockImplementation()!;
    f.runtime.spawn.mockImplementationOnce(async config => { entered.resolve(); await gate.promise; return spawn(config); });
    // Resume or internal delivery acquires through the runtime itself...
    const resume = acquireConversation(f.input);
    await entered.promise;
    // ...while a user's message acquires through the intake wrapper of the same runtime.
    const inbound = acquireConversation({ ...f.input, runtime: intakeAwareRuntime(f.runtime) });
    gate.resolve();
    const results = await Promise.all([resume, inbound]);
    expect(results.map(proc => proc.sessionId)).toEqual(['original', 'original']);
    expect(f.runtime.spawn).toHaveBeenCalledTimes(1);
  });

  it('serializes simultaneous cold requests and reuses the one admitted native process', async () => {
    const f = fixture(), gate = deferred(), entered = deferred();
    const spawn = f.runtime.spawn.getMockImplementation()!;
    f.runtime.spawn.mockImplementationOnce(async config => { entered.resolve(); await gate.promise; return spawn(config); });
    const first = acquireConversation(f.input);
    await entered.promise;
    const second = acquireConversation(f.input);
    gate.resolve();
    const results = await Promise.all([first, second]);
    expect(results.map(proc => proc.sessionId)).toEqual(['original', 'original']);
    expect(f.runtime.spawn).toHaveBeenCalledTimes(1);
    expect(f.runtime.kill).not.toHaveBeenCalled();
  });

  it('does not block another address behind a slow start', async () => {
    const f = fixture(), gate = deferred(), entered = deferred();
    const spawn = f.runtime.spawn.getMockImplementation()!;
    f.runtime.spawn.mockImplementationOnce(async config => { entered.resolve(); await gate.promise; return spawn(config); });
    const first = acquireConversation(f.input);
    await entered.promise;
    await expect(acquireConversation({ ...f.input, poolKey: 'other:pulse', stateManager: undefined }))
      .resolves.toMatchObject({ state: 'ready' });
    gate.resolve();
    await first;
  });

  it('allows newer input to adopt a published child before an older start returns', async () => {
    const f = fixture(), gate = deferred(), entered = deferred();
    let current = true;
    f.runtime.spawn.mockImplementationOnce(async () => {
      const proc = f.makeProcess(); f.processes.set(f.input.poolKey, proc);
      entered.resolve(); await gate.promise; return proc;
    });
    const first = acquireConversation({ ...f.input, recovery: { isCurrent: () => current } });
    const rejected = expect(first).rejects.toMatchObject({ code: 'canonical_attachment_interrupted' });
    await entered.promise;
    current = false;
    await expect(acquireConversation(f.input)).resolves.toMatchObject({ sessionId: 'original' });
    gate.resolve(); await rejected;
    expect(f.runtime.spawn).toHaveBeenCalledTimes(1);
  });

  it('does not reuse a process that an explicit restart is retiring', async () => {
    const f = fixture(), gate = deferred(), entered = deferred();
    const old = f.makeProcess(); f.processes.set(f.input.poolKey, old);
    f.runtime.kill.mockImplementationOnce(async () => {
      entered.resolve(); await gate.promise; f.processes.delete(f.input.poolKey);
    });
    const restarted = acquireConversation({ ...f.input, intent: 'restart' });
    await entered.promise;
    let reused = false;
    const incoming = acquireConversation(f.input).then(proc => { reused = true; return proc; });
    await Promise.resolve(); expect(reused).toBe(false);
    gate.resolve();
    await restarted;
    expect(await incoming).not.toBe(old);
    expect(f.runtime.spawn).toHaveBeenCalledTimes(1);
  });

  it('rejects an authority change hidden inside adapter initialization', async () => {
    const f = fixture();
    const spawn = f.runtime.spawn.getMockImplementation()!;
    f.runtime.spawn.mockImplementationOnce(async config => {
      const proc = await spawn(config);
      f.bind();
      return proc;
    });
    await expect(acquireConversation(f.input)).rejects.toMatchObject({ code: 'recovery_session_changed' });
  });

  it('allows the next legitimate request after a failed acquisition', async () => {
    const f = fixture();
    f.runtime.spawn.mockRejectedValueOnce(new Error('provider unavailable'));
    await expect(acquireConversation(f.input)).rejects.toThrow('provider unavailable');
    await expect(acquireConversation(f.input)).resolves.toMatchObject({ sessionId: 'original' });
    expect(f.runtime.spawn).toHaveBeenCalledTimes(2);
  });

  it('reuses the same live identity despite a different measurement digest', async () => {
    const f = fixture(); f.bind();
    f.processes.set(f.input.poolKey, { ...f.makeProcess(), usageBindingSha256: 'a'.repeat(64) });
    await expect(acquireConversation(f.input)).resolves.toMatchObject({ sessionId: 'original' });
    expect(f.runtime.spawn).not.toHaveBeenCalled();
    expect(f.runtime.kill).not.toHaveBeenCalled();
  });

  it('validates a restart before killing a process and preserves the original on rejection', async () => {
    const f = fixture(); f.bind();
    f.processes.set(f.input.poolKey, f.makeProcess());
    f.session.sessionId = 'different';
    await expect(acquireConversation({ ...f.input, intent: 'restart' })).rejects.toThrow();
    expect(f.runtime.kill).not.toHaveBeenCalled();
    expect(f.runtime.spawn).not.toHaveBeenCalled();
  });

  it('does not spawn a stale restart after identity changes during retirement', async () => {
    const f = fixture();
    f.processes.set(f.input.poolKey, f.makeProcess());
    f.runtime.kill.mockImplementationOnce(async () => { f.session.sessionId = 'new-authority'; });
    await expect(acquireConversation({ ...f.input, intent: 'restart' }))
      .rejects.toMatchObject({ code: 'recovery_session_changed' });
    expect(f.runtime.spawn).not.toHaveBeenCalled();
  });

  it('keeps the WI workspace and Session without reactivating retired measurement admission', async () => {
    const f = fixture(); f.bind();
    const before = JSON.stringify(f.session);
    f.processes.set(f.input.poolKey, f.makeProcess());
    await acquireConversation({ ...f.input, intent: 'restart' });
    expect(f.runtime.spawn).toHaveBeenCalledExactlyOnceWith(expect.objectContaining({
      workspace: '/registered/wi-worktree', sessionId: 'original', runtime: 'pi',
    }));
    expect(f.runtime.spawn.mock.calls[0][0].computeCostBinding).toBeUndefined();
    expect(f.runtime.spawn.mock.calls[0][0].measurementBaselineReference).toBeUndefined();
    expect(JSON.stringify(f.session)).toBe(before);
  });

  it('does not adopt an old cold start after the persisted Session changes', async () => {
    const f = fixture();
    f.runtime.spawn.mockImplementationOnce(async () => {
      const proc = f.makeProcess(); f.session.sessionId = 'later-session'; return proc;
    });
    await expect(acquireConversation(f.input)).rejects.toMatchObject({ code: 'recovery_session_changed' });
    expect(f.runtime.kill).not.toHaveBeenCalled();
  });

  it('allows activity bookkeeping during a cold start', async () => {
    const f = fixture();
    const spawn = f.runtime.spawn.getMockImplementation()!;
    f.runtime.spawn.mockImplementationOnce(async config => { f.session.lastActivity++; return spawn(config); });
    await expect(acquireConversation(f.input)).resolves.toMatchObject({ sessionId: 'original' });
  });

});
