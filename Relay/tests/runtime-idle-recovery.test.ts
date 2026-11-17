/**
 * Idle reclaim must not report a runtime failure for turns that already ended.
 * Production (2026-10-26, owner:pulse): two collaboration deliveries both passed
 * the pool's idle check while the session-access readback was pending, so both
 * were written to CC. CC folded the second into the running turn and emitted one
 * result carrying the second ref; the first Relay turn (with side effects) stayed
 * queued, and the later idle reclaim raised runtime-recovery-required.
 */
import { describe, it, expect, vi, afterEach } from 'vitest';
import { EventEmitter } from 'node:events';
import { PassThrough } from 'node:stream';
import { mkdtempSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { createPool } from '../src/pool.js';
import { createConversationRuntimeManager, createPoolConversationAdapter } from '../src/conversation-runtime.js';
import type { ConversationTurnContext, SessionEndReason, StreamJsonMessage } from '../src/types.js';

const access = vi.fn();
const fixture = vi.hoisted(() => {
  const { mkdtempSync: make } = require('node:fs') as typeof import('node:fs');
  const { tmpdir: temp } = require('node:os') as typeof import('node:os');
  return { data: make(require('node:path').join(temp(), 'idle-recovery-data-')) as string };
});
vi.mock('../src/managed-paths.js', async (original) => ({
  ...(await original<typeof import('../src/managed-paths.js')>()),
  managedRelayPath: (key: string, selected?: string) => (key === 'dataDir' ? fixture.data : selected),
}));
vi.mock('../src/native-work-item-command.js', async (original) => ({
  ...(await original<typeof import('../src/native-work-item-command.js')>()),
  nodeWorkItemCore: () => ({ extension: '/fixture/ext.mjs', python: '/fixture/python', entrypoint: '/fixture/harness.pyz' }),
  // An array is the merged view whose standing rows are the readback; an object is the readback.
  readSessionAccess: async (...args: unknown[]) => { const value = await access(...args);
    return Array.isArray(value)
      ? { access: value, standing: value.filter((row: { standing?: true }) => row.standing), workspaces: [] } : value; },
}));
vi.mock('../src/native-core-runtime.js', async (original) => ({
  ...(await original<typeof import('../src/native-core-runtime.js')>()),
  observeNativeSession: () => {},
}));
let mockSpawn: ReturnType<typeof vi.fn>;
vi.mock('node:child_process', async (original) => ({
  ...(await original<typeof import('node:child_process')>()),
  spawn: (...args: unknown[]) => mockSpawn(...args),
}));
vi.mock('../src/owned-process.js', async () => ({
  spawnOwnedProcess: (await import('node:child_process')).spawn,
}));

/** CC print mode: stdin user envelopes that arrive before a result are folded
 *  into the running turn, which ends with a single result. */
function fakeCc(pid: number) {
  const proc = Object.assign(new EventEmitter(), {
    stdin: new PassThrough(), stdout: new PassThrough(), stderr: new PassThrough(), pid, killed: false,
  }) as EventEmitter & { stdin: PassThrough; stdout: PassThrough; stderr: PassThrough;
    pid: number; killed: boolean; kill: ReturnType<typeof vi.fn> };
  proc.kill = vi.fn(() => { proc.killed = true; proc.emit('exit', 143, null); return true; });
  const inputs: string[] = [];
  let buffered = '';
  proc.stdin.on('data', (chunk: Buffer) => {
    buffered += chunk.toString();
    const lines = buffered.split('\n');
    buffered = lines.pop() ?? '';
    for (const line of lines) {
      const record = JSON.parse(line) as { type?: string; message?: { content?: unknown } };
      if (record.type === 'user') inputs.push(String(record.message?.content));
    }
  });
  let tool = 0;
  const emit = (record: Record<string, unknown>) => proc.stdout.write(`${JSON.stringify(record)}\n`);
  return {
    proc,
    inputs,
    emit,
    /** Run every input written so far as one native turn: a write tool, then one result. */
    runTurn(): void {
      inputs.length = 0;
      emit({ type: 'assistant', message: { content: [{ type: 'tool_use', id: `tool-${++tool}`, name: 'Bash', input: {} }] } });
      emit({ type: 'result', subtype: 'success', result: 'done' });
    },
  };
}

function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((r) => { resolve = r; });
  return { promise, resolve };
}

const flush = () => new Promise((resolve) => setTimeout(resolve, 10));

describe('idle reclaim after interleaved Relay turns', () => {
  const dir = mkdtempSync(join(tmpdir(), 'idle-recovery-'));
  let dispose: (() => void) | undefined;
  afterEach(() => { dispose?.(); dispose = undefined; access.mockReset(); });

  it('keeps one Relay turn per native turn, settles both, and reclaims silently', async () => {
    const cc = fakeCc(9301);
    mockSpawn = vi.fn().mockReturnValue(cc.proc);
    const pool = createPool({ cleanupIntervalMs: 20, idleTimeoutMs: 60_000, ccMode: 'print' });
    let ref = 0;
    const capture = { begin: vi.fn(() => ({ ref: `attempt-${++ref}`, finished: false }) as any),
      finish: vi.fn(), response: vi.fn(), gap: vi.fn(), human: vi.fn(), linkHuman: vi.fn() };
    const manager = createConversationRuntimeManager({ collaborationCapture: capture });
    manager.register(createPoolConversationAdapter(pool));
    dispose = () => { manager.dispose(); rmSync(dir, { recursive: true, force: true }); };
    const sessionEnds: SessionEndReason[] = [];
    const results: StreamJsonMessage[] = [];
    manager.on('session-end', (_tenantId, reason) => sessionEnds.push(reason));
    manager.on('response', (_tenantId, msg) => { if (msg.type === 'result') results.push(msg); });
    await manager.spawn({ tenantId: 'owner:pulse', runtime: 'cc', workspace: dir, sessionId: 'pulse-session' });

    // Both deliveries see an idle process while the access readback is pending.
    const readback = deferred<unknown[]>();
    access.mockReturnValue(readback.promise);
    const sendB = manager.send('owner:pulse', { type: 'human', content: 'contract B' });
    const sendC = manager.send('owner:pulse', { type: 'human', content: 'contract C' });
    readback.resolve([]);
    await flush();
    const turnInputs: string[][] = [];
    while (cc.inputs.length > 0) {
      turnInputs.push([...cc.inputs]);
      cc.runTurn();
      await flush();
    }
    await Promise.all([sendB, sendC]);
    expect(turnInputs).toEqual([['contract B'], ['contract C']]);
    expect(results.map((msg) => msg.host_attempt_ref)).toEqual(['attempt-2', 'attempt-3']);

    pool.get('owner:pulse')!.lastActivity = 0;
    await new Promise((resolve) => setTimeout(resolve, 60));
    expect(sessionEnds).toEqual([{ type: 'idle-reclaimed' }]);
  });

  it('queues a Relay turn behind a runtime-notification turn that starts during the readback', async () => {
    const cc = fakeCc(9302);
    mockSpawn = vi.fn().mockReturnValue(cc.proc);
    const pool = createPool({ cleanupIntervalMs: 999_999, ccMode: 'print' });
    dispose = () => { pool.dispose(); rmSync(dir, { recursive: true, force: true }); };
    const results: StreamJsonMessage[] = [];
    pool.on('response', (_tenantId, msg) => { if (msg.type === 'result') results.push(msg); });
    await pool.spawn({ tenantId: 'owner:pulse', workspace: dir, sessionId: 'pulse-session', ccModel: 'opus' });

    const readback = deferred<unknown[]>();
    access.mockReturnValue(readback.promise);
    const turn: ConversationTurnContext = { correlationId: 'turn:r', idempotencyKey: 'turn:r', effect: 'no-effect',
      deliveryVisibility: 'public', hostAttemptRef: 'attempt-r' };
    const send = pool.send('owner:pulse', { type: 'human', content: 'human R' }, turn);
    cc.emit({ type: 'user', message: { content: [{ type: 'text', text: '<task-notification>done</task-notification>' }] } });
    await flush();
    readback.resolve([]);
    await flush();
    expect(cc.inputs).toHaveLength(0);

    cc.runTurn();
    await send;
    await flush();
    expect(cc.inputs).toEqual(['human R']);
    cc.runTurn();
    await flush();
    expect(results[0]).toMatchObject({ turn_origin: 'runtime-notification' });
    expect(results[0].host_attempt_ref).toBeUndefined();
    expect(results[1]).toMatchObject({ host_attempt_ref: 'attempt-r' });
    expect(results[1].turn_origin).toBeUndefined();
  });
});

describe('conversation runtime settlement of folded and stale turns', () => {
  function adapterWithEvents() {
    const events = new EventEmitter();
    const process = { tenantId: 't', runtime: 'cc' as const, sessionId: 's', state: 'ready' as const,
      lastActivity: 0, spawnedAt: 0, restartCount: 0, restartWindowStart: 0 };
    return {
      events,
      adapter: {
        id: 'cc' as const,
        spawn: vi.fn(async () => process), send: vi.fn(async () => {}), kill: vi.fn(async () => {}),
        killAll: vi.fn(async () => {}), get: vi.fn(() => process), getAll: vi.fn(() => [process]),
        getStatus: vi.fn(() => []), on: vi.fn((event: string, handler: (...args: unknown[]) => void) => { events.on(event, handler); }) as any,
        emitShutdown: vi.fn(), dispose: vi.fn(),
      },
    };
  }

  async function setup() {
    const { events, adapter } = adapterWithEvents();
    let ref = 0;
    const capture = { begin: vi.fn(() => ({ ref: `attempt-${++ref}`, finished: false }) as any),
      finish: vi.fn(), response: vi.fn(), gap: vi.fn(), human: vi.fn(), linkHuman: vi.fn() };
    const manager = createConversationRuntimeManager({ collaborationCapture: capture });
    manager.register(adapter);
    const sessionEnds: SessionEndReason[] = [];
    const results: StreamJsonMessage[] = [];
    manager.on('session-end', (_tenantId, reason) => sessionEnds.push(reason));
    manager.on('response', (_tenantId, msg) => { if (msg.type === 'result') results.push(msg); });
    await manager.spawn({ tenantId: 't', runtime: 'cc', workspace: '/synthetic', sessionId: 's' });
    return { events, capture, manager, sessionEnds, results };
  }

  it('settles turns folded into a later queued turn when its result arrives', async () => {
    const { events, capture, sessionEnds, results, manager } = await setup();
    await manager.send('t', { type: 'human', content: 'B' });
    await manager.send('t', { type: 'human', content: 'C' });
    events.emit('turn-effect', 't', 'side-effect-observed');
    events.emit('response', 't', { type: 'result', host_attempt_ref: 'attempt-3' });
    expect(results).toEqual([expect.objectContaining({ host_attempt_ref: 'attempt-3' })]);
    expect(capture.finish).toHaveBeenCalledWith(expect.objectContaining({ ref: 'attempt-2' }), 'cancelled');

    events.emit('session-end', 't', { type: 'idle-reclaimed' } satisfies SessionEndReason);
    expect(sessionEnds).toEqual([{ type: 'idle-reclaimed' }]);
  });

  it('does not raise recovery-required when an idle runtime is reclaimed over a stale side-effect turn', async () => {
    const { events, sessionEnds, manager } = await setup();
    await manager.send('t', { type: 'human', content: 'B' });
    events.emit('turn-effect', 't', 'side-effect-observed');
    events.emit('session-end', 't', { type: 'idle-reclaimed' } satisfies SessionEndReason);
    expect(sessionEnds).toEqual([{ type: 'idle-reclaimed' }]);
  });

  it('still raises recovery-required when a running side-effect turn loses its runtime', async () => {
    const { events, sessionEnds, manager } = await setup();
    await manager.send('t', { type: 'human', content: 'write' });
    events.emit('turn-effect', 't', 'side-effect-observed');
    events.emit('session-end', 't', { type: 'runtime-failed', runtime: 'cc' } satisfies SessionEndReason);
    expect(sessionEnds).toEqual([expect.objectContaining({
      type: 'runtime-recovery-required', effect: 'side-effect-observed', reason: 'runtime ended: runtime-failed' })]);
  });
});
