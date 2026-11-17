import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { mkdtempSync, mkdirSync, readFileSync, rmSync, statSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { createMaintenanceBarrier } from '../src/maintenance-barrier.js';
import { createSwitchBarrierStore, switchBarrierPath, SWITCH_BARRIER_CAPABILITY } from '../src/switch-barrier.js';
import { createConversationRuntimeManager, type ConversationProcess, type ConversationRuntimeAdapter } from '../src/conversation-runtime.js';
import { persistQueuedTurns, replayPendingTurns, unrecordPendingTurn } from '../src/shutdown-interrupts.js';
import { startRelayHttpServer, type RelayHttpServerHandle } from '../src/http-server.js';
import type { ConversationRuntimeManager } from '../src/conversation-runtime.js';
import type { PersistedSession, Scheduler } from '../src/types.js';
import type { Router } from '../src/router.js';

const ME = { holder: 'oikos@ownermac', transaction: 'switch-20261110-ab12', token: 'a'.repeat(40) };
const OTHER = { holder: 'oikos@tcloud', transaction: 'switch-20261110-cd34', token: 'b'.repeat(40) };

let dataDir: string;
let path: string;
let clock: number;
const now = () => clock;

beforeEach(() => {
  dataDir = mkdtempSync(join(tmpdir(), 'relay-switch-barrier-'));
  path = switchBarrierPath(dataDir);
  clock = 1_800_000_000_000;
});
afterEach(() => rmSync(dataDir, { recursive: true, force: true }));

function adapter(): ConversationRuntimeAdapter {
  const processes = new Map<string, ConversationProcess>();
  return {
    id: 'pi',
    spawn: vi.fn(async config => {
      const process: ConversationProcess = { tenantId: config.tenantId, runtime: 'pi', sessionId: 's', state: 'ready',
        lastActivity: 1, spawnedAt: 1, restartCount: 0, restartWindowStart: 1 };
      processes.set(config.tenantId, process);
      return process;
    }),
    send: vi.fn(async () => {}),
    // A busy Pi session takes the next turn as steering input.
    steer: vi.fn(async () => {}),
    kill: vi.fn(async () => {}),
    killAll: vi.fn(async () => {}),
    get: vi.fn((tenantId: string) => processes.get(tenantId)),
    getAll: vi.fn(() => [...processes.values()]),
    getStatus: vi.fn(() => []),
    on: vi.fn(),
    emitShutdown: vi.fn(() => new Set<string>()),
  } as unknown as ConversationRuntimeAdapter;
}

/** A state file on disk, read anew by each simulated process. */
function diskState(file: string, failStrict: () => boolean = () => false) {
  const read = (): Record<string, PersistedSession> => JSON.parse(readFileSync(file, 'utf8'));
  let sessions = read();
  return {
    flush: () => writeFileSync(file, JSON.stringify(sessions)),
    flushStrict: () => {
      if (failStrict()) throw new Error('ENOSPC');
      writeFileSync(file, JSON.stringify(sessions));
    },
    getSession: (key: string) => sessions[key],
    getAllSessions: () => sessions,
    updateSession: (key: string, patch: Partial<PersistedSession>) => { sessions[key] = { ...sessions[key]!, ...patch }; },
    reload: () => { sessions = read(); },
  };
}

/** One Relay process: barrier read from the data dir, a runtime manager behind it.
 *  `startup`: the startup resume gate taken at once, as index.ts does. */
async function relayProcess(stateFile: string, opts: { startup?: boolean; failStrict?: () => boolean } = {}) {
  const state = diskState(stateFile, opts.failStrict);
  const store = createSwitchBarrierStore(path, { now });
  const barrier = createMaintenanceBarrier(now, {
    switchBarrier: store,
    // As index.ts: a failed strict flush reports the turn unwritten.
    recordTurn: turn => {
      if (persistQueuedTurns(state, [turn], now()).length === 0) return false;
      try { state.flushStrict(); return true; } catch { return false; }
    },
    unrecordTurn: turn => unrecordPendingTurn(state, turn.tenantId, turn.seq),
  });
  const startResume = opts.startup ? barrier.startupResume() : undefined;
  const runtime = adapter();
  const manager = createConversationRuntimeManager({ barrier });
  manager.register(runtime);
  await manager.spawn({ tenantId: 'owner:aki', runtime: 'pi', workspace: '/tmp/w', sessionId: 's' });
  return { state, store, barrier, runtime, manager, startResume };
}

function recordOrder(runtime: ConversationRuntimeAdapter): string[] {
  const order: string[] = [];
  for (const fn of [runtime.send, runtime.steer!]) {
    (fn as ReturnType<typeof vi.fn>).mockImplementation(async (_tenant: string, message: { content: string }) => { order.push(message.content); });
  }
  return order;
}

function seedState(): string {
  const file = join(dataDir, 'state.json');
  writeFileSync(file, JSON.stringify({ 'owner:aki': { poolKey: 'owner:aki', agentId: 'aki', sessionId: 's', turnCount: 1,
    maxTurns: 9, lastActivity: 1, status: 'active' } }));
  return file;
}

const tick = () => new Promise(resolve => setTimeout(resolve, 5));

describe('switch barrier store', () => {
  it('persists the hold with private permissions and a new process reads it before taking work', () => {
    const first = createSwitchBarrierStore(path, { now });
    expect(first.status()).toMatchObject({ state: 'absent', blocking: false, readAtStartup: { state: 'absent' } });
    expect(first.acquire({ ...ME, leaseMs: 600_000 })).toMatchObject({ ok: true, status: { state: 'held', holder: ME.holder } });
    expect(statSync(path).mode & 0o777).toBe(0o600);
    expect(statSync(join(dataDir, 'maintenance')).mode & 0o777).toBe(0o700);
    const raw = readFileSync(path, 'utf8');
    expect(raw).not.toContain(ME.token);
    first.dispose();

    // Restart: a second process on the same data dir.
    const second = createSwitchBarrierStore(path, { now });
    const barrier = createMaintenanceBarrier(now, { switchBarrier: second });
    expect(second.status()).toMatchObject({ state: 'held', blocking: true, transaction: ME.transaction,
      readAtStartup: { state: 'held', pid: process.pid } });
    expect(barrier.isHeld()).toBe(true);
    expect(barrier.switchBlocking()).toBe(true);
    expect(barrier.status()).toMatchObject({ held: false, blocking: true, capabilities: ['maintenance-hold/1', 'switch-barrier/1'] });
    second.dispose();
  });

  it('binds holder and transaction to the token; only the original holder releases', () => {
    const store = createSwitchBarrierStore(path, { now });
    store.acquire({ ...ME, leaseMs: 60_000 });
    expect(store.acquire({ ...OTHER, leaseMs: 60_000 })).toMatchObject({ ok: false, error: 'held_by_another',
      status: { holder: ME.holder } });
    expect(store.acquire({ ...ME, leaseMs: 60_000 })).toMatchObject({ ok: true }); // retried acquire is idempotent
    expect(store.release({ ...ME, token: 'c'.repeat(40) })).toMatchObject({ ok: false, error: 'not_holder' });
    expect(store.release({ ...OTHER })).toMatchObject({ ok: false, error: 'not_holder' });
    expect(store.renew({ ...ME, transaction: 'switch-other', leaseMs: 60_000 })).toMatchObject({ ok: false, error: 'not_holder' });
    expect(store.release({ ...ME, token: 'short' })).toMatchObject({ ok: false, error: 'token_invalid' });
    expect(store.blocking()).toBe(true);
    const released = store.release(ME);
    expect(released).toMatchObject({ ok: true, status: { state: 'released', releasedBy: 'holder', blocking: false } });
    expect(store.release(ME)).toMatchObject({ ok: true, alreadyReleased: true });
    expect(store.renew({ ...ME, leaseMs: 60_000 })).toMatchObject({ ok: false, error: 'not_held' });
    // A new transaction may now acquire.
    expect(store.acquire({ ...OTHER, leaseMs: 60_000 })).toMatchObject({ ok: true, status: { holder: OTHER.holder } });
    store.dispose();
  });

  it('a lapsed lease never opens: it stays blocking across a restart until the holder recovers or releases', () => {
    const store = createSwitchBarrierStore(path, { now });
    store.acquire({ ...ME, leaseMs: 10_000 });
    clock += 10_001;
    expect(store.status()).toMatchObject({ state: 'expired_unreleased', blocking: true, reason: 'lease_expired',
      expiredAt: 1_800_000_010_000 });
    expect(JSON.parse(readFileSync(path, 'utf8'))).toMatchObject({ state: 'expired_unreleased', reason: 'lease_expired' });
    store.dispose();

    const restarted = createSwitchBarrierStore(path, { now });
    expect(restarted.status()).toMatchObject({ state: 'expired_unreleased', blocking: true,
      readAtStartup: { state: 'expired_unreleased' } });
    expect(restarted.acquire({ ...ME, leaseMs: 10_000 })).toMatchObject({ ok: false, error: 'lease_expired' });
    expect(restarted.acquire({ ...OTHER, leaseMs: 10_000 })).toMatchObject({ ok: false, error: 'held_by_another' });
    const recovered = restarted.renew({ ...ME, leaseMs: 30_000 });
    expect(recovered).toMatchObject({ ok: true, recovered: true, status: { state: 'held',
      lastExpiry: { expiredAt: 1_800_000_010_000, recoveredAt: clock } } });
    restarted.dispose();
  });

  it('expires by its own timer, persisting the reason', async () => {
    vi.useFakeTimers();
    try {
      const store = createSwitchBarrierStore(path);
      store.acquire({ ...ME, leaseMs: 5_000 });
      await vi.advanceTimersByTimeAsync(5_010);
      expect(JSON.parse(readFileSync(path, 'utf8'))).toMatchObject({ state: 'expired_unreleased' });
      store.dispose();
    } finally { vi.useRealTimers(); }
  });

  it('an unreadable record blocks and only an explicit operator break clears it', () => {
    mkdirSync(join(dataDir, 'maintenance'), { recursive: true });
    writeFileSync(path, '{ not json');
    const store = createSwitchBarrierStore(path, { now });
    expect(store.status()).toMatchObject({ state: 'invalid', blocking: true, transaction: null, invalidReason: 'unparseable_json' });
    expect(store.acquire({ ...ME, leaseMs: 10_000 })).toMatchObject({ ok: false, error: 'record_invalid' });
    expect(store.breakBarrier({ transaction: 'x', operator: 'owner', reason: 'checked' })).toMatchObject({ ok: false, error: 'transaction_mismatch' });
    expect(store.breakBarrier({ transaction: null, operator: '', reason: 'checked' })).toMatchObject({ ok: false, error: 'operator_invalid' });
    expect(store.breakBarrier({ transaction: null, operator: 'owner', reason: 'checked' })).toMatchObject({ ok: true,
      status: { state: 'released', releasedBy: 'operator', operator: 'owner', reason: 'checked' } });
    expect(store.acquire({ ...ME, leaseMs: 10_000 })).toMatchObject({ ok: true });
    expect(store.breakBarrier({ transaction: ME.transaction, operator: 'owner', reason: 'holder lost its token' }))
      .toMatchObject({ ok: true, status: { state: 'released', releasedBy: 'operator' } });
    // The holder's late release is not mistaken for its own.
    expect(store.release(ME)).toMatchObject({ ok: false, error: 'not_held' });
    store.dispose();
  });

  it('a failed release keeps the barrier and can be retried', () => {
    let failing = false;
    const real = createSwitchBarrierStore(path, { now });
    real.acquire({ ...ME, leaseMs: 60_000 });
    real.dispose();
    const store = createSwitchBarrierStore(path, { now, write: (target, record) => {
      if (failing) throw new Error('EIO');
      writeFileSync(target, JSON.stringify(record));
    } });
    failing = true;
    expect(store.release(ME)).toMatchObject({ ok: false, error: 'persist_failed', status: { state: 'held', blocking: true } });
    expect(JSON.parse(readFileSync(path, 'utf8'))).toMatchObject({ state: 'held' });
    failing = false;
    expect(store.release(ME)).toMatchObject({ ok: true, status: { state: 'released' } });
    store.dispose();
  });

  it('a release that reached the disk before the write reported an error opens the gate', async () => {
    const real = createSwitchBarrierStore(path, { now });
    real.acquire({ ...ME, leaseMs: 60_000 });
    real.dispose();
    let failing = false;
    const store = createSwitchBarrierStore(path, { now, write: (target, record) => {
      writeFileSync(target, JSON.stringify(record));
      if (failing) throw new Error('EIO: directory fsync');
    } });
    const barrier = createMaintenanceBarrier(now, { switchBarrier: store });
    let dispatched = false;
    void barrier.admit('owner:aki', 'pi', { type: 'human', content: 'x' }).then(() => { dispatched = true; });
    failing = true;
    expect(store.release(ME)).toMatchObject({ ok: true, status: { state: 'released', blocking: false } });
    await tick();
    expect(dispatched).toBe(true);
    // A retried release wakes this process's waiters again.
    const changed = vi.fn();
    store.onChange(changed);
    expect(store.release(ME)).toMatchObject({ ok: true, alreadyReleased: true });
    expect(changed).toHaveBeenCalledOnce();
    store.dispose();
  });
});

describe('switch barrier with queued turns across a restart', () => {
  it('withholds the receipt of journaled turns, records the rest at arrival, and dispatches each once on release', async () => {
    const stateFile = seedState();
    const a = await relayProcess(stateFile);
    expect(a.store.acquire({ ...ME, leaseMs: 600_000 }).ok).toBe(true);

    // A channel turn whose intake journal holds it, and an internal turn without one.
    const journalReceipt = vi.fn();
    void a.manager.send('owner:aki', { type: 'human', content: 'journaled' }, { onAccepted: journalReceipt });
    void a.manager.send('owner:aki', { type: 'human', content: 'internal' });
    await tick();
    expect(a.runtime.send).not.toHaveBeenCalled();
    expect(journalReceipt).not.toHaveBeenCalled(); // the journal keeps it on disk
    a.state.reload();
    expect(a.state.getSession('owner:aki')!.pendingMaintenanceTurns!.map(turn => turn.message.content)).toEqual(['internal']);

    // Graceful shutdown: neither turn is recorded a second time.
    const sink = vi.fn();
    a.barrier.close(sink);
    expect(sink).not.toHaveBeenCalled();
    a.store.dispose();

    // The next process reads the barrier first and takes no work.
    const b = await relayProcess(stateFile);
    expect(b.barrier.isHeld()).toBe(true);
    expect(b.store.status().readAtStartup.state).toBe('held');
    expect(b.state.getSession('owner:aki')!.pendingMaintenanceTurns).toHaveLength(1);
    // The journal replays its turn; it waits again without a receipt.
    const replayReceipt = vi.fn();
    const replayed = b.manager.send('owner:aki', { type: 'human', content: 'journaled' }, { onAccepted: replayReceipt });
    // A turn arriving now, with no upstream record, is written at once.
    const late = b.manager.send('owner:aki', { type: 'human', content: 'late internal' });
    await tick();
    expect(b.runtime.send).not.toHaveBeenCalled();
    expect(replayReceipt).not.toHaveBeenCalled();
    expect(b.state.getSession('owner:aki')!.pendingMaintenanceTurns!.map(turn => turn.message.content))
      .toEqual(['internal', 'late internal']);

    // Only the holder opens it, on the new process.
    expect(b.store.release(OTHER)).toMatchObject({ ok: false, error: 'not_holder' });
    expect(b.barrier.isHeld()).toBe(true);
    const opened = vi.fn();
    b.barrier.onOpen(opened);
    expect(b.store.release(ME).ok).toBe(true);
    await Promise.all([replayed, late]);
    expect(opened).toHaveBeenCalledOnce();
    const delivered = [b.runtime.send, b.runtime.steer!].flatMap(fn => (fn as ReturnType<typeof vi.fn>).mock.calls)
      .map(call => call[1].content);
    expect(delivered).toEqual(['journaled', 'late internal']);
    expect(replayReceipt).toHaveBeenCalled(); // the runtime's receipt releases the journal record
    // The turn dispatched here left the pending list; the earlier process's
    // record stays for the channel resume pass that runs on open.
    expect(b.state.getSession('owner:aki')!.pendingMaintenanceTurns!.map(turn => turn.message.content)).toEqual(['internal']);
    b.store.dispose();
  });

  it('a crash while held loses nothing: unowned turns are already on disk', async () => {
    const stateFile = seedState();
    const a = await relayProcess(stateFile);
    a.store.acquire({ ...ME, leaseMs: 600_000 });
    void a.manager.send('owner:aki', { type: 'human', content: 'before crash' });
    await tick();
    a.store.dispose(); // no close(): the process died
    const b = await relayProcess(stateFile);
    expect(b.state.getSession('owner:aki')!.pendingMaintenanceTurns!.map(turn => turn.message.content)).toEqual(['before crash']);
    expect(b.barrier.isHeld()).toBe(true);
    b.store.dispose();
  });

  it('keeps a recorded turn when its dispatch fails before the runtime receipt, for a later replay', async () => {
    const stateFile = seedState();
    const a = await relayProcess(stateFile);
    a.store.acquire({ ...ME, leaseMs: 600_000 });
    const sent = a.manager.send('owner:aki', { type: 'human', content: 'internal' });
    await tick();
    a.state.reload();
    const recorded = a.state.getSession('owner:aki')!.pendingMaintenanceTurns!;
    expect(recorded).toHaveLength(1);
    (a.runtime.send as ReturnType<typeof vi.fn>).mockRejectedValueOnce(new Error('runtime refused before admission'));
    expect(a.store.release(ME).ok).toBe(true);
    await expect(sent).rejects.toThrow('runtime refused before admission');
    a.state.reload();
    expect(a.state.getSession('owner:aki')!.pendingMaintenanceTurns).toEqual(recorded);
    // Replayed under the same arrival number; the receipt then takes the record.
    expect(await replayPendingTurns(a.state, 'owner:aki', (message, submit) => a.manager.send('owner:aki', message, submit))).toBe(1);
    a.state.reload();
    expect(a.state.getSession('owner:aki')!.pendingMaintenanceTurns).toEqual([]);
    expect((a.runtime.send as ReturnType<typeof vi.fn>).mock.calls.map(call => call[1].content)).toEqual(['internal', 'internal']);
    a.store.dispose();
  });

  it('on release, turns recorded by the earlier process go out before turns that waited in this one', async () => {
    const stateFile = seedState();
    const a = await relayProcess(stateFile);
    a.store.acquire({ ...ME, leaseMs: 600_000 });
    void a.manager.send('owner:aki', { type: 'human', content: 'A' });
    await tick();
    a.store.dispose(); // crash

    const b = await relayProcess(stateFile, { startup: true });
    const order = recordOrder(b.runtime);
    const late = b.manager.send('owner:aki', { type: 'human', content: 'B' });
    await tick();
    expect(b.state.getSession('owner:aki')!.pendingMaintenanceTurns!.map(turn => turn.message.content)).toEqual(['A', 'B']);
    // The channel resume pass, as index.ts runs it.
    expect(b.startResume!(() => replayPendingTurns(b.state, 'owner:aki',
      (message, submit) => b.manager.send('owner:aki', message, submit)))).toBe('deferred');
    expect(b.store.release(ME).ok).toBe(true);
    await late;
    await tick();
    expect(order).toEqual(['A', 'B']);
    b.state.reload();
    expect(b.state.getSession('owner:aki')!.pendingMaintenanceTurns).toEqual([]);
    b.store.dispose();
  });

  it('a release before the startup resume pass has started still runs turns of the earlier process first', async () => {
    const stateFile = seedState();
    const a = await relayProcess(stateFile);
    a.store.acquire({ ...ME, leaseMs: 600_000 });
    void a.manager.send('owner:aki', { type: 'human', content: 'A' });
    await tick();
    a.store.dispose(); // crash

    // The gate is taken before intake opens; the resume pass starts seconds later.
    const b = await relayProcess(stateFile, { startup: true });
    const order = recordOrder(b.runtime);
    const late = b.manager.send('owner:aki', { type: 'human', content: 'B' });
    await tick();
    // Released inside that window: B still waits for the resume pass.
    expect(b.store.release(ME).ok).toBe(true);
    expect(b.barrier.isHeld()).toBe(false);
    await tick();
    expect(order).toEqual([]);
    expect(b.startResume!(() => replayPendingTurns(b.state, 'owner:aki',
      (message, submit) => b.manager.send('owner:aki', message, submit)))).toBe('now');
    await late;
    await tick();
    expect(order).toEqual(['A', 'B']);
    b.state.reload();
    expect(b.state.getSession('owner:aki')!.pendingMaintenanceTurns).toEqual([]);
    b.store.dispose();
  });

  it('a startup without a barrier also runs pending turns before turns that arrived first in the new process', async () => {
    const stateFile = seedState();
    // A, recorded by the earlier process's shutdown.
    const earlier = diskState(stateFile);
    persistQueuedTurns(earlier, [{ tenantId: 'owner:aki', runtime: 'pi', queuedAt: 1, seq: 1, message: { type: 'human', content: 'A' } }], 1);
    earlier.flush();

    const b = await relayProcess(stateFile, { startup: true });
    expect(b.barrier.isHeld()).toBe(false);
    const order = recordOrder(b.runtime);
    const early = b.manager.send('owner:aki', { type: 'human', content: 'B' });
    await tick();
    expect(order).toEqual([]);
    expect(b.startResume!(() => replayPendingTurns(b.state, 'owner:aki',
      (message, submit) => b.manager.send('owner:aki', message, submit)))).toBe('now');
    await early;
    await tick();
    expect(order).toEqual(['A', 'B']);
    b.store.dispose();
  });

  it('a waiting turn whose only copy cannot be written refuses the switch acquire and keeps restart unsafe until a retry writes it', async () => {
    const stateFile = seedState();
    let failing = false;
    const a = await relayProcess(stateFile, { failStrict: () => failing });
    const hold = a.barrier.hold(60_000);
    if (!hold.ok) throw new Error('hold refused');
    // A channel turn under the short hold: its receipt deletes the journal copy at once.
    const receipt = vi.fn();
    void a.manager.send('owner:aki', { type: 'human', content: 'only in memory' }, { onAccepted: receipt });
    await tick();
    expect(receipt).toHaveBeenCalledOnce();

    process.env['RELAY_INTERNAL_TOKEN'] = 'test-secret';
    const http = await startRelayHttpServer({
      router: {} as unknown as Router, configPath: join(dataDir, 'relay.json'), startedAtMs: Date.now(),
      runtimeManager: { getStatus: () => [] } as unknown as ConversationRuntimeManager,
      scheduler: { getRunningJobIds: () => [], stop: vi.fn(), reload: vi.fn() } as unknown as Scheduler,
      maintenance: a.barrier,
    }, 0);
    const call = async (route: string, body?: unknown) => {
      const response = await fetch(`http://127.0.0.1:${http.port}/internal${route}`, {
        method: body === undefined ? 'GET' : 'POST',
        headers: { 'x-relay-token': 'test-secret', 'content-type': 'application/json' },
        ...(body === undefined ? {} : { body: JSON.stringify(body) }) });
      return { code: response.status, body: await response.json() as Record<string, any> };
    };
    try {
      failing = true;
      const refused = await call('/maintenance/switch/acquire', { ...ME, leaseMs: 600_000 });
      expect(refused).toMatchObject({ code: 409, body: { ok: false, error: 'pending_unsecured', switch: { state: 'held' },
        status: { safeToRestart: false, maintenance: { unsecured: 1, blocking: true } } } });
      expect((await call('/cc-status')).body).toMatchObject({ safeToRestart: false, maintenance: { unsecured: 1 } });
      a.state.reload();
      expect(a.state.getSession('owner:aki')!.pendingMaintenanceTurns ?? []).toEqual([]);

      // The disk recovers: the same acquire writes the turn and is granted.
      failing = false;
      const granted = await call('/maintenance/switch/acquire', { ...ME, leaseMs: 600_000 });
      expect(granted).toMatchObject({ code: 200, body: { ok: true, status: { safeToRestart: true } } });
      expect(granted.body['status'].maintenance.unsecured).toBeUndefined();
      a.state.reload();
      expect(a.state.getSession('owner:aki')!.pendingMaintenanceTurns!.map(turn => turn.message.content)).toEqual(['only in memory']);

      // A turn arriving under the barrier whose write fails is counted the same way.
      failing = true;
      void a.manager.send('owner:aki', { type: 'human', content: 'late, unwritten' });
      await tick();
      expect((await call('/cc-status')).body).toMatchObject({ safeToRestart: false, maintenance: { unsecured: 1 } });
      // Released without a retry: its record, left in memory by the failed write, leaves with the dispatch.
      failing = false;
      expect(a.barrier.release(hold.token)).toBe(true);
      expect((await call('/maintenance/switch/release', ME)).code).toBe(200);
      await tick();
      expect((a.runtime.send as ReturnType<typeof vi.fn>).mock.calls.length
        + (a.runtime.steer as ReturnType<typeof vi.fn>).mock.calls.length).toBe(2);
      expect(a.state.getSession('owner:aki')!.pendingMaintenanceTurns).toEqual([]);
      expect((await call('/cc-status')).body).toMatchObject({ safeToRestart: true });
    } finally {
      await http.stop();
      delete process.env['RELAY_INTERNAL_TOKEN'];
      a.store.dispose();
    }
  });

  it('records turns already waiting under a short hold when the switch barrier is taken', async () => {
    const stateFile = seedState();
    const a = await relayProcess(stateFile);
    const hold = a.barrier.hold(60_000);
    if (!hold.ok) throw new Error('hold refused');
    const receipt = vi.fn();
    void a.manager.send('owner:aki', { type: 'human', content: 'under short hold' }, { onAccepted: receipt });
    await tick();
    expect(receipt).toHaveBeenCalledOnce(); // short hold: receipt at once (unchanged)
    a.store.acquire({ ...ME, leaseMs: 600_000 });
    a.state.reload();
    expect(a.state.getSession('owner:aki')!.pendingMaintenanceTurns!.map(turn => turn.message.content)).toEqual(['under short hold']);
    a.barrier.release(hold.token);
    a.store.release(ME);
    await tick();
    expect(a.runtime.send).toHaveBeenCalledOnce();
    expect(a.state.getSession('owner:aki')!.pendingMaintenanceTurns).toEqual([]);
    expect(a.state.getSession('owner:aki')!.shutdownInterruptedAt).toBeUndefined();
    a.store.dispose();
  });
});

describe('short TTL hold beside the switch barrier', () => {
  it('keeps its own token and expiry; neither barrier opens the gate while the other is in force', async () => {
    vi.useFakeTimers();
    try {
      const store = createSwitchBarrierStore(path);
      const barrier = createMaintenanceBarrier(Date.now, { switchBarrier: store });
      const hold = barrier.hold(5_000);
      expect(hold).toMatchObject({ ok: true, status: { held: true, blocking: true, switch: { state: 'absent' } } });
      store.acquire({ ...ME, leaseMs: 600_000 });
      let dispatched = false;
      void barrier.admit('owner:aki', 'pi', { type: 'human', content: 'x' }).then(() => { dispatched = true; });
      await vi.advanceTimersByTimeAsync(5_000); // the short hold lapses as before
      expect(barrier.status().held).toBe(false);
      expect(dispatched).toBe(false);
      expect(barrier.isHeld()).toBe(true);
      const second = barrier.hold(60_000);
      if (!second.ok) throw new Error('hold refused');
      store.release(ME);
      await vi.advanceTimersByTimeAsync(1);
      expect(dispatched).toBe(false);
      expect(barrier.release(second.token)).toBe(true);
      await vi.advanceTimersByTimeAsync(1);
      expect(dispatched).toBe(true);
      store.dispose();
    } finally { vi.useRealTimers(); }
  });
});

describe('switch barrier HTTP', () => {
  let handle: RelayHttpServerHandle;
  let store: ReturnType<typeof createSwitchBarrierStore>;
  beforeEach(async () => {
    process.env['RELAY_INTERNAL_TOKEN'] = 'test-secret';
    store = createSwitchBarrierStore(path);
    handle = await startRelayHttpServer({
      router: {} as unknown as Router,
      configPath: join(dataDir, 'relay.json'),
      startedAtMs: Date.now(),
      runtimeManager: { getStatus: () => [] } as unknown as ConversationRuntimeManager,
      scheduler: { getRunningJobIds: () => [], stop: vi.fn(), reload: vi.fn() } as unknown as Scheduler,
      maintenance: createMaintenanceBarrier(Date.now, { switchBarrier: store }),
    }, 0);
  });
  afterEach(async () => {
    await handle.stop();
    store.dispose();
    delete process.env['RELAY_INTERNAL_TOKEN'];
  });
  const call = async (route: string, body?: unknown) => {
    const response = await fetch(`http://127.0.0.1:${handle.port}/internal/maintenance${route}`, {
      method: body === undefined ? 'GET' : 'POST',
      headers: { 'x-relay-token': 'test-secret', 'content-type': 'application/json' },
      ...(body === undefined ? {} : { body: JSON.stringify(body) }) });
    return { code: response.status, body: await response.json() as Record<string, any> };
  };

  it('serves capability, acquire, renew and release with readback and error codes', async () => {
    expect((await fetch(`http://127.0.0.1:${handle.port}/internal/maintenance/switch`)).status).toBe(401);
    const probe = await call('/switch');
    expect(probe).toMatchObject({ code: 200, body: { capability: SWITCH_BARRIER_CAPABILITY, switch: { state: 'absent' } } });
    expect((await call('/switch/acquire', { ...ME, leaseMs: 1 })).body).toMatchObject({ error: 'leaseMs_invalid' });
    expect((await call('/switch/acquire', { ...ME, leaseMs: 1 })).code).toBe(400);
    const acquired = await call('/switch/acquire', { ...ME, leaseMs: 600_000 });
    expect(acquired).toMatchObject({ code: 200, body: { ok: true, switch: { state: 'held' },
      status: { safeToRestart: true, maintenance: { blocking: true, held: false, switch: { state: 'held' } } } } });
    expect(JSON.stringify(acquired.body)).not.toContain(ME.token);
    expect(await call('/switch/acquire', { ...OTHER, leaseMs: 600_000 })).toMatchObject({ code: 409, body: { error: 'held_by_another' } });
    expect(await call('/switch/renew', { ...ME, leaseMs: 600_000 })).toMatchObject({ code: 200, body: { switch: { state: 'held' } } });
    expect(await call('/switch/release', OTHER)).toMatchObject({ code: 409, body: { error: 'not_holder' } });
    expect(await call('/switch/release', ME)).toMatchObject({ code: 200, body: { switch: { state: 'released' } } });
    expect(await call('/switch/release', ME)).toMatchObject({ code: 200, body: { alreadyReleased: true } });
    expect(await call('/switch/break', { transaction: ME.transaction, operator: 'owner', reason: 'x' }))
      .toMatchObject({ code: 409, body: { error: 'not_held' } });
    expect((await call('/switch/acquire', [])).body).toMatchObject({ error: 'invalid_body' });
    // The short hold keeps its existing contract.
    const hold = await call('/hold', { ttlMs: 60_000 });
    expect(hold).toMatchObject({ code: 200, body: { ok: true, token: expect.any(String), maintenance: { held: true } } });
    expect(await call('/release', { token: hold.body['token'] })).toMatchObject({ code: 200, body: { ok: true } });
  });

  it('is 404 on a Relay without a switch barrier store', async () => {
    const plain = await startRelayHttpServer({
      router: {} as unknown as Router, configPath: join(dataDir, 'relay.json'), startedAtMs: Date.now(),
      maintenance: createMaintenanceBarrier(),
    }, 0);
    try {
      const response = await fetch(`http://127.0.0.1:${plain.port}/internal/maintenance/switch`, { headers: { 'x-relay-token': 'test-secret' } });
      expect(response.status).toBe(404);
    } finally { await plain.stop(); }
  });
});
