import { describe, expect, it, vi } from 'vitest';
import { createConversationRuntimeManager, type ConversationProcess, type ConversationRuntimeAdapter } from '../src/conversation-runtime.js';
import { createMaintenanceBarrier } from '../src/maintenance-barrier.js';
import { busyAtShutdown, persistQueuedTurns, replayPendingTurns, stampShutdownInterrupts } from '../src/shutdown-interrupts.js';
import type { PersistedSession } from '../src/types.js';

function adapter(id: 'cc' | 'pi'): ConversationRuntimeAdapter {
  const processes = new Map<string, ConversationProcess>();
  return {
    id,
    spawn: vi.fn(async config => {
      const process: ConversationProcess = { tenantId: config.tenantId, runtime: id, sessionId: 's', state: 'ready',
        lastActivity: 1, spawnedAt: 1, restartCount: 0, restartWindowStart: 1 };
      processes.set(config.tenantId, process);
      return process;
    }),
    send: vi.fn(async () => {}),
    kill: vi.fn(async () => {}),
    killAll: vi.fn(async () => {}),
    get: vi.fn((tenantId: string) => processes.get(tenantId)),
    getAll: vi.fn(() => [...processes.values()]),
    getStatus: vi.fn(() => []),
    on: vi.fn(),
    emitShutdown: vi.fn(() => new Set<string>()),
  } as unknown as ConversationRuntimeAdapter;
}

describe('maintenance barrier', () => {
  it('queues a new Pi turn while held and dispatches it on release, never dropping it', async () => {
    const barrier = createMaintenanceBarrier();
    const pi = adapter('pi');
    const manager = createConversationRuntimeManager({ barrier });
    manager.register(pi);
    await manager.spawn({ tenantId: 'owner:aki', runtime: 'pi', workspace: '/tmp/w', sessionId: 's' });

    const held = barrier.hold(60_000);
    if (!held.ok) throw new Error('hold refused');
    const sent = manager.send('owner:aki', { type: 'human', content: 'after the check' });
    await new Promise(resolve => setTimeout(resolve, 10));
    expect(pi.send).not.toHaveBeenCalled();
    expect(barrier.status()).toMatchObject({ held: true, queued: [{ tenantId: 'owner:aki', runtime: 'pi' }] });
    expect(JSON.stringify(barrier.status())).not.toContain('after the check');

    expect(barrier.release('someone-else')).toBe(false);
    expect(pi.send).not.toHaveBeenCalled();
    expect(barrier.release(held.token)).toBe(true);
    await sent;
    expect(pi.send).toHaveBeenCalledOnce();
    expect(barrier.status()).toEqual({ held: false, queued: [] });
  });

  it('gives each hold its own token; another deployment cannot renew or release it', () => {
    const barrier = createMaintenanceBarrier();
    const first = barrier.hold(60_000);
    if (!first.ok) throw new Error('hold refused');
    expect(barrier.hold(60_000)).toMatchObject({ ok: false, error: 'held_by_another' });
    expect(barrier.release('stale-token')).toBe(false);
    expect(barrier.isHeld()).toBe(true);
    expect(barrier.hold(60_000, first.token)).toMatchObject({ ok: true, token: first.token });
    expect(barrier.release(first.token)).toBe(true);
    const second = barrier.hold(60_000);
    expect(second.ok && second.token !== first.token).toBe(true);
    expect(barrier.release(first.token)).toBe(false); // A's late release never opens B's hold
    expect(barrier.isHeld()).toBe(true);
  });

  it('after close, waiting and later turns are recorded at arrival and never dispatched', async () => {
    const barrier = createMaintenanceBarrier();
    const recorded: string[] = [];
    const held = barrier.hold(60_000);
    if (!held.ok) throw new Error('hold refused');
    let dispatched = 0;
    void barrier.admit('owner:pulse', 'cc', { type: 'human', content: 'queued before close' }).then(() => { dispatched++; });
    barrier.close(turn => recorded.push(String(turn.message.content)));
    expect(recorded).toEqual(['queued before close']);
    void barrier.admit('owner:pulse', 'cc', { type: 'human', content: 'arrived after the save' }).then(() => { dispatched++; });
    expect(recorded).toEqual(['queued before close', 'arrived after the save']);
    barrier.release(held.token);
    await new Promise(resolve => setTimeout(resolve, 5));
    expect(dispatched).toBe(0);
    expect(barrier.hold(60_000)).toMatchObject({ ok: false, error: 'closed' });
    expect(barrier.isHeld()).toBe(true); // no scheduled or manual run during shutdown
  });

  it('releases by itself when the deployer goes away', async () => {
    vi.useFakeTimers();
    try {
      const barrier = createMaintenanceBarrier();
      barrier.hold(5_000);
      const admitted = barrier.admit('owner:pulse', 'cc', { type: 'human', content: 'x' });
      await vi.advanceTimersByTimeAsync(5_000);
      await admitted;
      expect(barrier.isHeld()).toBe(false);
    } finally {
      vi.useRealTimers();
    }
  });

  it('keeps queued turns apart from the interrupted turn a runtime callback rewrites', async () => {
    const sessions: Record<string, PersistedSession> = {
      'owner:pulse': { poolKey: 'owner:pulse', agentId: 'pulse', sessionId: 'a', turnCount: 1, maxTurns: 9,
        lastActivity: 1, status: 'active' },
      'owner:old': { poolKey: 'owner:old', agentId: 'old', sessionId: 'b', turnCount: 1, maxTurns: 9,
        lastActivity: 1, status: 'active', wasBusyAtShutdown: true, shutdownInterruptedAt: 5 },
      'owner:busy': { poolKey: 'owner:busy', agentId: 'busy', sessionId: 'c', turnCount: 1, maxTurns: 9,
        lastActivity: 1, status: 'active' },
    };
    const state = {
      flush: vi.fn(),
      getSession: (key: string) => sessions[key],
      getAllSessions: () => sessions,
      updateSession: (key: string, patch: Partial<PersistedSession>) => { sessions[key] = { ...sessions[key]!, ...patch }; },
    };
    const before = busyAtShutdown(state);
    sessions['owner:busy']!.wasBusyAtShutdown = true; // set by this shutdown's emitShutdown
    expect(stampShutdownInterrupts(state, before, 100)).toEqual(['owner:busy']);
    expect(sessions['owner:old']!.shutdownInterruptedAt).toBe(5);

    const image = { type: 'human', content: [{ type: 'image', source: { type: 'base64', media_type: 'image/png', data: 'iVBORw0KGgo=' } }] };
    expect(persistQueuedTurns(state, [
      { tenantId: 'owner:pulse', runtime: 'codex', queuedAt: 90, seq: 900, message: { type: 'human', content: 'B' } },
      { tenantId: 'owner:pulse', runtime: 'codex', queuedAt: 91, seq: 910, message: image },
      { tenantId: 'owner:unknown', runtime: 'cc', queuedAt: 92, seq: 920, message: { type: 'human', content: 'lost' } },
    ], 100)).toEqual(['owner:pulse']);
    // The Codex shutdown callback then records its own in-flight turn A.
    state.updateSession('owner:pulse', { runtime: 'codex', wasBusyAtShutdown: true, interruptedUserMessage: 'A' });
    expect(sessions['owner:pulse']).toMatchObject({ interruptedUserMessage: 'A', shutdownInterruptedAt: 100,
      pendingMaintenanceTurns: [{ queuedAt: 90, seq: 900, message: { type: 'human', content: 'B' } }, { queuedAt: 91, seq: 910, message: image }] });

    // Replay: in order, the complete message under its arrival number, each
    // removed only once the runtime accepted it.
    const sent: unknown[] = [];
    let fail = true;
    await expect(replayPendingTurns(state, 'owner:pulse', async (message, submit) => {
      if (sent.length === 1 && fail) { fail = false; throw new Error('runtime refused'); }
      expect(submit.seq).toBe(sent.length === 0 ? 900 : 910);
      sent.push(message);
      submit.onAccepted?.();
    })).rejects.toThrow('runtime refused');
    expect(sessions['owner:pulse']!.pendingMaintenanceTurns).toEqual([{ queuedAt: 91, seq: 910, message: image }]);
    expect(await replayPendingTurns(state, 'owner:pulse', async (message, submit) => { sent.push(message); submit.onAccepted?.(); })).toBe(1);
    expect(sent).toEqual([{ type: 'human', content: 'B' }, image]);
    expect(sessions['owner:pulse']!.pendingMaintenanceTurns).toEqual([]);
  });

  it('replays records saved before arrival numbers existed, two in one millisecond, each once and in order', async () => {
    const sessions: Record<string, PersistedSession> = {
      'owner:pulse': { poolKey: 'owner:pulse', agentId: 'pulse', sessionId: 'a', turnCount: 1, maxTurns: 9,
        lastActivity: 1, status: 'active', pendingMaintenanceTurns: [
          { queuedAt: 1_800_000_000_000, message: { type: 'human', content: 'first' } },
          { queuedAt: 1_800_000_000_000, message: { type: 'human', content: 'second' } },
        ] },
    };
    const state = {
      flush: vi.fn(),
      getSession: (key: string) => sessions[key],
      getAllSessions: () => sessions,
      updateSession: (key: string, patch: Partial<PersistedSession>) => { sessions[key] = { ...sessions[key]!, ...patch }; },
    };
    const sent: Array<{ content: unknown; seq?: number }> = [];
    let acceptFirst!: () => void;
    const replay = replayPendingTurns(state, 'owner:pulse', async (message, submit) => {
      sent.push({ content: message.content, seq: submit.seq });
      if (sent.length === 1) acceptFirst = () => submit.onAccepted?.(); else submit.onAccepted?.();
    });
    await vi.waitFor(() => expect(sent).toHaveLength(2));
    // Each record got its own number, saved with it.
    expect(sessions['owner:pulse']!.pendingMaintenanceTurns!.map(turn => turn.message.content)).toEqual(['first']);
    acceptFirst();
    await replay;
    expect(sent.map(turn => turn.content)).toEqual(['first', 'second']);
    expect(new Set(sent.map(turn => turn.seq)).size).toBe(2);
    expect(sessions['owner:pulse']!.pendingMaintenanceTurns).toEqual([]);
  });

  it('keeps arrival order when runtime-queued turns are recorded after barrier-held ones', () => {
    const sessions: Record<string, PersistedSession> = {
      'owner:pulse': { poolKey: 'owner:pulse', agentId: 'pulse', sessionId: 'a', turnCount: 1, maxTurns: 9,
        lastActivity: 1, status: 'active' },
    };
    const state = {
      flush: vi.fn(),
      getSession: (key: string) => sessions[key],
      getAllSessions: () => sessions,
      updateSession: (key: string, patch: Partial<PersistedSession>) => { sessions[key] = { ...sessions[key]!, ...patch }; },
    };
    // The barrier closes first and records the turn it held...
    persistQueuedTurns(state, [{ tenantId: 'owner:pulse', runtime: 'cc', queuedAt: 30, seq: 30_000, message: { type: 'human', content: 'held' } }], 100);
    // ...then the runtime hands over the older turns queued behind its busy turn.
    persistQueuedTurns(state, [
      { tenantId: 'owner:pulse', runtime: 'cc', queuedAt: 10, seq: 10_000, message: { type: 'human', content: 'queued 1' } },
      { tenantId: 'owner:pulse', runtime: 'cc', queuedAt: 20, seq: 20_000, message: { type: 'human', content: 'queued 2' } },
    ], 100);
    expect(sessions['owner:pulse']!.pendingMaintenanceTurns!.map(turn => turn.message.content))
      .toEqual(['queued 1', 'queued 2', 'held']);
  });
});
