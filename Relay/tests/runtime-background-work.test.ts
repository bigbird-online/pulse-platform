import { describe, it, expect, vi, afterEach } from 'vitest';
import { EventEmitter } from 'node:events';
import { PassThrough } from 'node:stream';
import { createPool } from '../src/pool.js';
import { createCcSdkConversationRuntime, type SdkMessage, type SdkQueryOptions, type SdkUserMessage } from '../src/cc-sdk-conversation-runtime.js';
import { createConversationRuntimeManager, type ConversationProcess, type ConversationRuntimeAdapter } from '../src/conversation-runtime.js';
import {
  BackgroundTaskTracker,
  RUNTIME_NOTIFICATION_TURN,
  shouldReclaimIdleRuntime,
  tagRuntimeNotificationTurn,
  type TurnAcceptedMeta,
} from '../src/runtime-background-work.js';
import type { SpawnConfig, StreamJsonMessage } from '../src/types.js';

interface MockProcess extends EventEmitter {
  stdin: PassThrough; stdout: PassThrough; stderr: PassThrough; pid: number;
  kill: ReturnType<typeof vi.fn>; killed: boolean;
}

function createMockProcess(): MockProcess {
  const proc = new EventEmitter() as MockProcess;
  proc.stdin = new PassThrough();
  proc.stdout = new PassThrough();
  proc.stderr = new PassThrough();
  proc.pid = 4321;
  proc.killed = false;
  proc.kill = vi.fn((signal?: string) => {
    proc.killed = true;
    proc.emit('exit', signal === 'SIGKILL' ? 137 : 0, signal ?? 'SIGTERM');
    return true;
  });
  return proc;
}

let mockSpawn: ReturnType<typeof vi.fn>;
vi.mock('node:child_process', () => ({ spawn: (...args: unknown[]) => mockSpawn(...args) }));

const sleep = (ms: number) => new Promise((resolve) => setTimeout(resolve, ms));
async function waitFor(predicate: () => boolean, timeoutMs = 2000): Promise<boolean> {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    if (predicate()) return true;
    await sleep(5);
  }
  return predicate();
}

const tenant = 'member-bg';
const baseConfig: SpawnConfig = { tenantId: tenant, workspace: '/opt/PULSE/Tenants/member-bg/', sessionId: 'bg-session' };
const line = (record: unknown) => `${JSON.stringify(record)}\n`;
const assistant = (text: string) => ({ type: 'assistant', message: { stop_reason: 'end_turn', content: [{ type: 'text', text }] } });

describe('shared idle reclaim policy', () => {
  it('reclaims only after idle timeout, defers for pending work, and enforces the hard cap', () => {
    const base = { idleTimeoutMs: 100, hardCapMs: 1000 };
    expect(shouldReclaimIdleRuntime({ ...base, now: 150, lastActivity: 100 })).toBe(false);
    expect(shouldReclaimIdleRuntime({ ...base, now: 250, lastActivity: 100 })).toBe(true);
    expect(shouldReclaimIdleRuntime({ ...base, now: 250, lastActivity: 100, backgroundWorkSince: 50 })).toBe(false);
    expect(shouldReclaimIdleRuntime({ ...base, now: 1050, lastActivity: 100, backgroundWorkSince: 50 })).toBe(true);
  });

  it('tracks CC background tasks from stream-json and transcript records', () => {
    let now = 10;
    const tracker = new BackgroundTaskTracker(() => now);
    tracker.observe({ type: 'system', subtype: 'task_started', task_id: 'fg', is_backgrounded: false });
    tracker.observe({ type: 'system', subtype: 'task_started', task_id: 'ambient', ambient: true });
    expect(tracker.size).toBe(0);
    tracker.observe({ type: 'system', subtype: 'task_started', task_id: 'b1', is_backgrounded: true });
    now = 20;
    tracker.observe({ type: 'user', toolUseResult: { backgroundTaskId: 'b2' }, message: { content: [] } });
    expect(tracker.pendingSince()).toBe(10);
    tracker.observe({ type: 'system', subtype: 'task_notification', task_id: 'b1', status: 'completed' });
    expect(tracker.pendingSince()).toBe(20);
    tracker.observe({ type: 'user', message: { content: '<task-notification>\n<task-id>b2</task-id>\n<status>completed</status>' } });
    expect(tracker.size).toBe(0);
    tracker.observe({ type: 'system', subtype: 'background_tasks_changed', tasks: [{ task_id: 'b3' }, { task_id: 'w', ambient: true }] });
    expect(tracker.size).toBe(1);
    tracker.observe({ type: 'system', subtype: 'background_tasks_changed', tasks: [] });
    expect(tracker.size).toBe(0);
  });
});

describe('CC print pool: background work and runtime-notification turns', () => {
  let pool: ReturnType<typeof createPool> | undefined;
  afterEach(() => { pool?.dispose(); pool = undefined; vi.restoreAllMocks(); });

  async function spawnPrint(options: Parameters<typeof createPool>[0]) {
    mockSpawn = vi.fn();
    const proc = createMockProcess();
    mockSpawn.mockReturnValue(proc);
    pool = createPool({ ccMode: 'print', ...options });
    const spawned = pool.spawn(baseConfig);
    proc.stdout.write(line({ type: 'system', content: 'ready' }));
    await spawned;
    let stdin = '';
    proc.stdin.on('data', (chunk: Buffer) => { stdin += chunk.toString(); });
    return { proc, stdin: () => stdin };
  }

  it('keeps an idle process with pending background tasks until the hard cap', async () => {
    const { proc } = await spawnPrint({ idleTimeoutMs: 40, cleanupIntervalMs: 10, backgroundWorkHardCapMs: 400 });
    await pool!.send(tenant, { type: 'human', content: 'start build' });
    const startedAt = Date.now();
    proc.stdout.write(line({ type: 'system', subtype: 'task_started', task_id: 'build', is_backgrounded: true }));
    proc.stdout.write(line(assistant('started')));
    proc.stdout.write(line({ type: 'result', result: '' }));
    await sleep(200);
    expect(proc.killed).toBe(false);
    expect(await waitFor(() => proc.killed, 2000)).toBe(true);
    expect(Date.now() - startedAt).toBeGreaterThanOrEqual(380);
  });

  it('counts runtime output as activity and reclaims once both output and pending work stop', async () => {
    const { proc } = await spawnPrint({ idleTimeoutMs: 60, cleanupIntervalMs: 10, backgroundWorkHardCapMs: 60_000 });
    await pool!.send(tenant, { type: 'human', content: 'hi' });
    proc.stdout.write(line({ type: 'system', subtype: 'task_started', task_id: 'b1', is_backgrounded: true }));
    proc.stdout.write(line({ type: 'result', result: '' }));
    proc.stdout.write(line({ type: 'system', subtype: 'task_notification', task_id: 'b1', status: 'completed' }));
    for (let i = 0; i < 8; i += 1) {
      await sleep(25);
      proc.stdout.write(line({ type: 'system', subtype: 'task_progress', task_id: 'x' }));
    }
    expect(proc.killed).toBe(false);
    expect(await waitFor(() => proc.killed, 1000)).toBe(true);
  });

  it('opens a runtime-notification turn, queues a human message behind it, and does not mismatch results', async () => {
    const { proc, stdin } = await spawnPrint({ cleanupIntervalMs: 999_999 });
    const events: Array<{ kind: string; meta?: TurnAcceptedMeta; msg?: StreamJsonMessage }> = [];
    pool!.on('turn-accepted', (_t, meta) => events.push({ kind: 'accepted', meta }));
    pool!.on('response', (_t, msg) => { if (msg.type === 'assistant' || msg.type === 'result') events.push({ kind: msg.type, msg }); });

    await pool!.send(tenant, { type: 'human', content: 'A' });
    proc.stdout.write(line(assistant('A answer')));
    proc.stdout.write(line({ type: 'result', result: '' }));
    await waitFor(() => pool!.get(tenant)?.state === 'idle');
    events.length = 0;

    // CC wakes on the completed background task without a Relay send.
    proc.stdout.write(line({ type: 'system', subtype: 'task_notification', task_id: 'b1', status: 'completed' }));
    proc.stdout.write(line(assistant('Background task finished.')));
    await waitFor(() => events.some((e) => e.kind === 'assistant'));
    expect(pool!.get(tenant)?.state).toBe('busy');
    expect(events[0]).toEqual({ kind: 'accepted', meta: RUNTIME_NOTIFICATION_TURN });
    expect(events[1]?.msg?.turn_origin).toBe('runtime-notification');

    const queued = pool!.send(tenant, { type: 'human', content: 'B-during-notification' });
    await sleep(20);
    expect(stdin()).not.toContain('B-during-notification');

    proc.stdout.write(line({ type: 'result', result: '' }));
    await queued;
    expect(await waitFor(() => stdin().includes('B-during-notification'))).toBe(true);
    const kinds = events.map((e) => `${e.kind}:${e.meta?.origin ?? e.msg?.turn_origin ?? 'user'}`);
    // The notification result reaches listeners before B begins.
    expect(kinds).toEqual([
      'accepted:runtime-notification', 'assistant:runtime-notification',
      'result:runtime-notification', 'accepted:user',
    ]);
    proc.stdout.write(line(assistant('B answer')));
    proc.stdout.write(line({ type: 'result', result: '' }));
    await waitFor(() => events.filter((e) => e.kind === 'result').length === 2);
    expect(events.filter((e) => e.kind === 'result').map((e) => e.msg?.turn_origin)).toEqual(['runtime-notification', undefined]);
  });
});

describe('conversation manager: runtime-notification output does not settle a queued Relay turn', () => {
  it('keeps the queued turn identity for its own result', async () => {
    const events = new EventEmitter();
    const proc: ConversationProcess = { tenantId: tenant, runtime: 'cc', state: 'idle', sessionId: 's',
      lastActivity: Date.now(), spawnedAt: Date.now(), restartCount: 0, restartWindowStart: Date.now() };
    const adapter = {
      id: 'cc', spawn: vi.fn(async () => proc), send: vi.fn(async () => {}), kill: vi.fn(async () => {}),
      killAll: vi.fn(async () => {}), get: vi.fn(() => proc), getAll: vi.fn(() => [proc]), getStatus: vi.fn(() => []),
      on: (event: string, handler: (...args: any[]) => void) => { events.on(event, handler); },
      emitShutdown: vi.fn(), drainBusySessions: vi.fn(async () => {}), dispose: vi.fn(),
    } as unknown as ConversationRuntimeAdapter;
    const manager = createConversationRuntimeManager({ env: {} });
    manager.register(adapter);
    await manager.spawn({ ...baseConfig, runtime: 'cc' });
    const results: StreamJsonMessage[] = [];
    const accepted: Array<TurnAcceptedMeta | undefined> = [];
    manager.on('response', (_t, msg) => { if (msg.type === 'result') results.push(msg); });
    manager.on('turn-accepted', (_t, meta) => accepted.push(meta));

    await manager.sendIdempotent(tenant, { type: 'human', content: 'B' }, 'operation_v1_B');
    events.emit('turn-accepted', tenant, RUNTIME_NOTIFICATION_TURN);
    events.emit('response', tenant, tagRuntimeNotificationTurn({ type: 'result', result: '' }));
    events.emit('response', tenant, { type: 'result', result: '' });
    expect(accepted).toEqual([RUNTIME_NOTIFICATION_TURN]);
    expect(results[0]?.host_turn_idempotency_key).toBeUndefined();
    expect(results[1]?.host_turn_idempotency_key).toBe('operation_v1_B');
    manager.dispose();
  });
});

describe('cc-sdk: background work and runtime-notification turns', () => {
  function controlledSdk() {
    const turns: string[] = [];
    const out: SdkMessage[] = [];
    let wake: (() => void) | undefined;
    let ended = false;
    const push = (...messages: unknown[]) => { out.push(...(messages as SdkMessage[])); wake?.(); };
    let onTurn = (text: string) => push(assistant(`echo:${text}`), { type: 'result', subtype: 'success', is_error: false });
    const query = (args: { prompt: AsyncIterable<SdkUserMessage>; options: SdkQueryOptions }): AsyncIterable<SdkMessage> => ({
      async *[Symbol.asyncIterator](): AsyncGenerator<SdkMessage> {
        void (async () => {
          for await (const user of args.prompt) {
            const text = typeof user.message.content === 'string' ? user.message.content : JSON.stringify(user.message.content);
            turns.push(text);
            onTurn(text);
          }
          ended = true;
          wake?.();
        })();
        yield { type: 'system', subtype: 'init', session_id: 'sdk-bg' } as SdkMessage;
        while (true) {
          while (out.length > 0) yield out.shift()!;
          if (ended) return;
          await new Promise<void>((resolve) => { wake = resolve; });
          wake = undefined;
        }
      },
    });
    return { sdk: { query }, turns, push, setOnTurn: (fn: (text: string) => void) => { onTurn = fn; } };
  }

  it('defers idle eviction while background tasks are pending, up to the hard cap', async () => {
    const control = controlledSdk();
    control.setOnTurn(() => control.push(
      { type: 'system', subtype: 'task_started', task_id: 'b1', is_backgrounded: true },
      assistant('started'), { type: 'result', subtype: 'success', is_error: false }));
    const adapter = createCcSdkConversationRuntime({ sdk: control.sdk, idleResidenceMs: 30, backgroundWorkHardCapMs: 300 });
    const ends: string[] = [];
    adapter.on('session-end', (_t, reason) => ends.push(reason.type));
    await adapter.spawn({ tenantId: tenant, workspace: '/tmp', sessionId: 's1' });
    await adapter.send(tenant, { type: 'human', content: 'go' });
    const sentAt = Date.now();
    await sleep(150);
    expect(ends).toEqual([]);
    expect(await waitFor(() => ends.includes('idle-reclaimed'), 2000)).toBe(true);
    expect(Date.now() - sentAt).toBeGreaterThanOrEqual(280);
    adapter.dispose();
  });

  it('delivers a runtime-notification turn and holds a human send until its result', async () => {
    const control = controlledSdk();
    const adapter = createCcSdkConversationRuntime({ sdk: control.sdk, idleResidenceMs: 60_000 });
    const events: string[] = [];
    adapter.on('turn-accepted', (_t, meta) => events.push(`accepted:${meta?.origin ?? 'user'}`));
    adapter.on('response', (_t, msg) => {
      if (msg.type === 'assistant' || msg.type === 'result') events.push(`${msg.type}:${String(msg.turn_origin ?? 'user')}`);
    });
    await adapter.spawn({ tenantId: tenant, workspace: '/tmp', sessionId: 's2' });
    await adapter.send(tenant, { type: 'human', content: 'A' });
    await waitFor(() => events.includes('result:user'));
    events.length = 0;

    control.push({ type: 'system', subtype: 'task_notification', task_id: 'b1', status: 'completed' }, assistant('Background done.'));
    await waitFor(() => events.length >= 2);
    expect(adapter.get(tenant)?.state).toBe('busy');
    const held = adapter.send(tenant, { type: 'human', content: 'B' });
    await sleep(20);
    expect(control.turns).toEqual(['A']);
    control.push({ type: 'result', subtype: 'success', is_error: false });
    await held;
    expect(await waitFor(() => events.filter((e) => e === 'result:user').length === 1)).toBe(true);
    expect(control.turns).toEqual(['A', 'B']);
    expect(events).toEqual([
      'accepted:runtime-notification', 'assistant:runtime-notification', 'result:runtime-notification',
      'accepted:user', 'assistant:user', 'result:user',
    ]);
    adapter.dispose();
  });
});

// Pool behavior is isolated from OS process ownership, tested with real processes separately.
vi.mock('../src/owned-process.js', async () => ({
  spawnOwnedProcess: (await import('node:child_process')).spawn,
}));
