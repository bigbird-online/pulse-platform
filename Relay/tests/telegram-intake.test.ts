/**
 * Real grammY long polling over a fake Bot API: a turn that never finishes
 * must not stop its bot from fetching updates.
 */
import { describe, it, expect, vi, afterEach } from 'vitest';
import { EventEmitter } from 'node:events';
import { mkdtempSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { telegramCommandName } from '../src/channels/telegram-intake.js';
import { createChatLanes } from '../src/channels/chat-lanes.js';
import { createChannelIntakeJournal, replayIntakeJournal, runJournaled } from '../src/channels/intake-journal.js';
import type { ConversationProcess, ConversationRuntimeAdapter } from '../src/conversation-runtime.js';
import type { ConversationTurnContext, PersistedSession, StateManager, StreamJsonMessage } from '../src/types.js';

interface ApiCall { method: string; payload: Record<string, unknown> }
const downloadMock = vi.hoisted(() => vi.fn());
vi.mock('../src/channels/telegram-download.js', async original => ({
  ...await original<typeof import('../src/channels/telegram-download.js')>(), downloadTelegramFile: downloadMock,
}));

const telegram = vi.hoisted(() => {
  const state = {
    calls: [] as ApiCall[],
    updates: [] as Record<string, unknown>[],
    nextUpdateId: 1,
  };
  async function fakeFetch(url: string, init: { body?: string; signal?: AbortSignal }): Promise<{ json(): Promise<unknown> }> {
    const method = String(url).split('/').pop()!;
    const payload = init.body ? JSON.parse(init.body) as Record<string, unknown> : {};
    state.calls.push({ method, payload });
    let result: unknown = true;
    if (method === 'getUpdates') {
      const offset = typeof payload.offset === 'number' ? payload.offset : 0;
      state.updates = state.updates.filter(update => (update.update_id as number) >= offset);
      if (state.updates.length === 0) {
        // A short long-poll: answer empty after a moment, or when stopped.
        await new Promise<void>(resolve => {
          const timer = setTimeout(resolve, 10);
          init.signal?.addEventListener('abort', () => { clearTimeout(timer); resolve(); }, { once: true });
        });
      }
      result = [...state.updates];
    } else if (method === 'getFile') {
      result = { file_id: payload.file_id, file_unique_id: 'unique', file_path: 'photos/picture.jpg' };
    } else if (method === 'sendMessage') {
      result = { message_id: state.calls.length, chat: { id: payload.chat_id }, date: 0, text: payload.text };
    }
    return { json: async () => ({ ok: true, result }) };
  }
  return { state, fakeFetch };
});

vi.mock('grammy', async (importOriginal) => {
  const real = await importOriginal<typeof import('grammy')>();
  class Bot extends real.Bot {
    constructor(token: string, options?: ConstructorParameters<typeof real.Bot>[1]) {
      super(token, {
        ...options,
        botInfo: { id: 42, is_bot: true, first_name: 'Pulse', username: 'pulse_test_bot', can_join_groups: false,
          can_read_all_group_messages: false, supports_inline_queries: false, can_connect_to_business: false,
          has_main_web_app: false } as never,
        client: { ...options?.client, fetch: telegram.fakeFetch as never },
      });
    }
  }
  return { ...real, Bot };
});

const { createConversationRuntimeManager } = await import('../src/conversation-runtime.js');
const { createRouter } = await import('../src/router.js');
const { createTelegramBots } = await import('../src/channels/telegram.js');
const { createStateManager } = await import('../src/state.js');
const { createMaintenanceBarrier } = await import('../src/maintenance-barrier.js');
const { persistQueuedTurns } = await import('../src/shutdown-interrupts.js');

const CHAT_A = 111222333;
const CHAT_B = 444555666;

function textUpdate(chatId: number, text: string): void {
  const entities = text.startsWith('/') ? [{ type: 'bot_command', offset: 0, length: text.split(' ')[0]!.length }] : undefined;
  telegram.state.updates.push({
    update_id: telegram.state.nextUpdateId++,
    message: { message_id: telegram.state.nextUpdateId, date: 0, chat: { id: chatId, type: 'private' },
      from: { id: chatId, is_bot: false, first_name: 'owner' }, text, ...(entities ? { entities } : {}) },
  });
}

function photoUpdate(chatId: number, caption: string): void {
  telegram.state.updates.push({
    update_id: telegram.state.nextUpdateId++,
    message: { message_id: telegram.state.nextUpdateId, date: 0, chat: { id: chatId, type: 'private' },
      from: { id: chatId, is_bot: false, first_name: 'owner' }, caption,
      photo: [{ file_id: 'photo-file', file_unique_id: 'unique', width: 1, height: 1 }] },
  });
}

function lastUpdateId(): number {
  return telegram.state.nextUpdateId - 1;
}

function acknowledgedThrough(updateId: number): boolean {
  return telegram.state.calls.some(call => call.method === 'getUpdates' && (call.payload.offset as number) > updateId);
}

function pollCount(): number {
  return telegram.state.calls.filter(call => call.method === 'getUpdates').length;
}

function textOf(message: StreamJsonMessage): string {
  return typeof message.content === 'string' ? message.content : JSON.stringify(message.content);
}

function memoryState(): StateManager {
  const sessions: Record<string, PersistedSession> = {};
  return {
    load: () => ({ version: 1, updatedAt: new Date().toISOString(), sessions }),
    getSession: (key: string) => sessions[key],
    updateSession: (key: string, update: Partial<PersistedSession>) => { sessions[key] = { ...sessions[key], ...update } as PersistedSession; },
    removeSession: (key: string) => { delete sessions[key]; },
    getAllSessions: () => sessions,
    getPendingSclIntents: () => ({}),
    upsertPendingSclIntent: vi.fn(),
    removePendingSclIntent: vi.fn(),
    flush: vi.fn(),
    flushStrict: vi.fn(),
    dispose: vi.fn(),
  };
}

/**
 * A CC adapter whose turns named "forever" never finish being sent. With
 * `receipts`, it gives admission receipts itself, and a turn named "unadmitted"
 * stays in an unrecorded preparation: never accepted, never sent.
 */
function ccAdapter(options: { spawnDelayMs?: (tenantId: string) => number; receipts?: boolean } = {}) {
  const events = new EventEmitter();
  const processes = new Map<string, ConversationProcess>();
  const sent: Array<{ tenantId: string; text: string }> = [];
  const adapter: ConversationRuntimeAdapter = {
    id: 'cc',
    spawn: vi.fn(async (config) => {
      const delay = options.spawnDelayMs?.(config.tenantId) ?? 0;
      if (delay) await new Promise(resolve => setTimeout(resolve, delay));
      const proc: ConversationProcess = { tenantId: config.tenantId, runtime: 'cc', sessionId: config.sessionId || 'cc-session',
        state: 'ready', lastActivity: Date.now(), spawnedAt: Date.now(), restartCount: 0, restartWindowStart: Date.now() };
      processes.set(config.tenantId, proc);
      return proc;
    }),
    admitsTurns: options.receipts === true,
    send: vi.fn(async (tenantId: string, message: StreamJsonMessage, turn?: ConversationTurnContext) => {
      const text = textOf(message);
      if (options.receipts && text.includes('unadmitted')) { await new Promise<void>(() => {}); return; }
      if (options.receipts) turn?.arrival?.accepted();
      sent.push({ tenantId, text });
      if (text.includes('forever')) await new Promise<void>(() => {});
    }),
    kill: vi.fn(async (tenantId: string) => { processes.delete(tenantId); }),
    killAll: vi.fn(async () => { processes.clear(); }),
    get: vi.fn((tenantId: string) => processes.get(tenantId)),
    getAll: vi.fn(() => [...processes.values()]),
    getStatus: vi.fn(() => []),
    on: vi.fn((event: string, handler: (...args: unknown[]) => void) => { events.on(event, handler); }) as ConversationRuntimeAdapter['on'],
    emitShutdown: vi.fn(() => new Set<string>()),
    dispose: vi.fn(),
  };
  return { adapter, sent };
}

async function until(predicate: () => boolean, label: string): Promise<void> {
  await vi.waitFor(() => { if (!predicate()) throw new Error(`waiting for ${label}`); }, { timeout: 3000, interval: 5 });
}

function writeConfig(dir: string, withBot = true): string {
  const agent = (sessionId: string) => ({ id: 'pulse', name: 'Pulse', emoji: 'P', ...(withBot ? { botToken: '123456:intake' } : {}),
    workspace: dir, sessionId, runtime: 'cc', conversationRuntimePreference: ['cc'], conversationRuntime: { runtime: 'cc' } });
  const configPath = join(dir, 'relay.json');
  writeFileSync(configPath, JSON.stringify({ users: [
    { userId: 'owner', chatId: String(CHAT_A), defaultAgent: 'pulse', agents: [agent('session-a')] },
    { userId: 'guest', chatId: String(CHAT_B), defaultAgent: 'pulse', agents: [agent('session-b')] },
  ] }));
  return configPath;
}

function fixture(adapterOptions?: Parameters<typeof ccAdapter>[0]) {
  const dir = mkdtempSync(join(tmpdir(), 'relay-tg-intake-'));
  const configPath = writeConfig(dir);
  const { adapter, sent } = ccAdapter(adapterOptions);
  const runtime = createConversationRuntimeManager();
  runtime.register(adapter);
  const bots = createTelegramBots({ runtime, router: createRouter(configPath), stateManager: memoryState() });
  return {
    bots, sent,
    async cleanup() { bots.stop(); runtime.dispose(); rmSync(dir, { recursive: true, force: true }); },
  };
}

afterEach(() => {
  telegram.state.calls.length = 0;
  telegram.state.updates.length = 0;
});

describe('Telegram intake while a turn never finishes', () => {
  it('keeps polling and serves other chats and /stop; health stays ok', async () => {
    const f = fixture();
    try {
      await f.bots.start();
      textUpdate(CHAT_A, 'forever turn');
      await until(() => f.sent.some(s => s.text.includes('forever turn')), 'the endless turn');
      const pollsAtStall = pollCount();

      await until(() => pollCount() >= pollsAtStall + 3, 'polling to continue');
      textUpdate(CHAT_B, 'hello from B');
      await until(() => f.sent.some(s => s.tenantId === 'guest:pulse' && s.text.includes('hello from B')), 'chat B');

      textUpdate(CHAT_A, '/stop');
      await until(() => telegram.state.calls.some(c => c.method === 'sendMessage' && c.payload.chat_id === CHAT_A), '/stop reply');
      expect(f.bots.runningAgentIds()).toEqual(['pulse']);
    } finally { await f.cleanup(); }
  });

  it('keeps a second message of the same chat after the first', async () => {
    // The first message waits for a slow spawn; the second arrives meanwhile.
    const f = fixture({ spawnDelayMs: tenantId => tenantId === 'owner:pulse' ? 100 : 0 });
    try {
      await f.bots.start();
      textUpdate(CHAT_A, 'first forever');
      textUpdate(CHAT_A, 'second');
      await until(() => f.sent.filter(s => s.tenantId === 'owner:pulse').length === 2, 'both turns of chat A');
      expect(f.sent.filter(s => s.tenantId === 'owner:pulse').map(s => s.text.includes('first') ? 'first' : 'second'))
        .toEqual(['first', 'second']);
    } finally { await f.cleanup(); }
  });
});

/**
 * One Relay process as far as Telegram intake is concerned: state on disk,
 * the maintenance barrier, and the shutdown order of cli.ts (close the
 * barrier, record what reaches it, stop the bots, flush).
 */
function relayProcess(dir: string, adapterOptions?: Parameters<typeof ccAdapter>[0]) {
  const state = createStateManager(join(dir, 'state.json'));
  state.load();
  const barrier = createMaintenanceBarrier();
  const { adapter, sent } = ccAdapter(adapterOptions);
  const runtime = createConversationRuntimeManager({ barrier });
  runtime.register(adapter);
  const router = createRouter(join(dir, 'relay.json'));
  const bots = createTelegramBots({ runtime, router, stateManager: state });
  return {
    bots, sent, state, router,
    shutdown(): void {
      barrier.close(turn => { persistQueuedTurns(state, [turn], Date.now()); state.flush(); });
      runtime.emitShutdown();
      bots.stop();
      state.flush();
    },
    dispose(): void { state.dispose(); runtime.dispose(); },
  };
}

function intakeIds(state: StateManager): string[] {
  return (state.getChannelIntake?.('telegram:pulse') ?? []).map(record => record.id);
}

function chatATexts(sent: Array<{ tenantId: string; text: string }>): string[] {
  return sent.filter(s => s.tenantId === 'owner:pulse').map(s => /photo-one|second|third|first|unadmitted/.exec(s.text)?.[0] ?? s.text);
}

describe('Telegram intake across a graceful restart', () => {
  afterEach(() => { vi.unstubAllGlobals(); });

  it('a slow photo finishes during shutdown: it resumes from state, the later texts from the journal, once each and in order', async () => {
    const dir = mkdtempSync(join(tmpdir(), 'relay-tg-restart-'));
    writeConfig(dir);
    let releaseDownload!: () => void;
    const download = new Promise<void>(resolve => { releaseDownload = resolve; });
    downloadMock.mockImplementation(async () => { await download; return Buffer.from('jpeg'); });
    const before = relayProcess(dir);
    let after: ReturnType<typeof relayProcess> | undefined;
    try {
      await before.bots.start();
      photoUpdate(CHAT_A, 'photo-one');
      textUpdate(CHAT_A, 'second');
      textUpdate(CHAT_A, 'third');
      const third = lastUpdateId();
      await until(() => acknowledgedThrough(third), 'Telegram to acknowledge all three updates');
      expect(before.sent).toEqual([]);

      before.shutdown();
      // The photo reaches the runtime only now, at the closed barrier.
      releaseDownload();
      await until(() => (before.state.getSession('owner:pulse')?.pendingMaintenanceTurns?.length ?? 0) === 1, 'the photo turn recorded for resume');
      expect(intakeIds(before.state)).toEqual([String(third - 1), String(third)]);
      expect(before.sent).toEqual([]);
      before.dispose();

      after = relayProcess(dir);
      await after.bots.start();
      await after.bots.notifyResumed();
      await until(() => chatATexts(after!.sent).length === 3, 'all three turns after restart');
      expect(chatATexts(after.sent)).toEqual(['photo-one', 'second', 'third']);
      await until(() => intakeIds(after!.state).length === 0, 'the journal to empty');
      await new Promise(resolve => setTimeout(resolve, 50));
      expect(chatATexts(after.sent)).toHaveLength(3);
    } finally {
      before.bots.stop();
      after?.bots.stop();
      after?.dispose();
      rmSync(dir, { recursive: true, force: true });
    }
  });

  it('a slow start that never reaches the runtime: the restarted bot replays all, ignores a redelivery, keeps order', async () => {
    const dir = mkdtempSync(join(tmpdir(), 'relay-tg-restart-'));
    writeConfig(dir);
    // The stopping process never finishes starting the first conversation.
    const before = relayProcess(dir, { spawnDelayMs: () => 1_000_000 });
    let after: ReturnType<typeof relayProcess> | undefined;
    try {
      await before.bots.start();
      textUpdate(CHAT_A, 'first');
      const first = lastUpdateId();
      textUpdate(CHAT_A, 'second');
      textUpdate(CHAT_A, 'third');
      const third = lastUpdateId();
      await until(() => acknowledgedThrough(third), 'Telegram to acknowledge all three updates');
      before.shutdown();
      expect(intakeIds(before.state)).toEqual([String(first), String(first + 1), String(third)]);
      before.dispose();

      // Telegram sends the second one again, as after a lost acknowledgement.
      telegram.state.updates.push({
        update_id: first + 1,
        message: { message_id: 900, date: 0, chat: { id: CHAT_A, type: 'private' },
          from: { id: CHAT_A, is_bot: false, first_name: 'owner' }, text: 'second' },
      });
      after = relayProcess(dir);
      await after.bots.start();
      await until(() => acknowledgedThrough(first + 1) && telegram.state.updates.length === 0, 'the redelivery to be polled');
      expect(after.sent).toEqual([]);
      await after.bots.notifyResumed();
      await until(() => chatATexts(after!.sent).length === 3, 'all three turns after restart');
      await new Promise(resolve => setTimeout(resolve, 50));
      expect(chatATexts(after.sent)).toEqual(['first', 'second', 'third']);
      expect(before.sent).toEqual([]);
      expect(intakeIds(after.state)).toEqual([]);
    } finally {
      before.bots.stop();
      after?.bots.stop();
      after?.dispose();
      rmSync(dir, { recursive: true, force: true });
    }
  });
});

describe('Telegram intake across a reload that replaces the bot', () => {
  afterEach(() => { vi.unstubAllGlobals(); });

  it('an update still downloading when the bot is removed and added again is dispatched once, by the bot that started it', async () => {
    const dir = mkdtempSync(join(tmpdir(), 'relay-tg-reload-'));
    writeConfig(dir);
    let releaseDownload!: () => void;
    const download = new Promise<void>(resolve => { releaseDownload = resolve; });
    let downloads = 0;
    downloadMock.mockImplementation(async () => { downloads++; await download; return Buffer.from('jpeg'); });
    const relay = relayProcess(dir);
    try {
      await relay.bots.start();
      await relay.bots.notifyResumed();  // as index.ts does after startup
      photoUpdate(CHAT_A, 'photo-one');
      const photo = lastUpdateId();
      await until(() => acknowledgedThrough(photo) && downloads === 1, 'the photo download to start');

      // Configuration drops the bot, then brings it back: a new bot generation.
      writeConfig(dir, false);
      relay.router.reload();
      expect((await relay.bots.reload()).removed).toEqual(['pulse']);
      writeConfig(dir);
      relay.router.reload();
      expect((await relay.bots.reload()).added).toEqual(['pulse']);
      await new Promise(resolve => setTimeout(resolve, 30));
      expect(downloads).toBe(1);  // the new bot does not take over an update in hand

      releaseDownload();
      await until(() => chatATexts(relay.sent).length === 1, 'the photo turn');
      await new Promise(resolve => setTimeout(resolve, 50));
      expect(chatATexts(relay.sent)).toEqual(['photo-one']);
      expect(intakeIds(relay.state)).toEqual([]);
    } finally {
      relay.bots.stop();
      relay.dispose();
      rmSync(dir, { recursive: true, force: true });
    }
  });
});

describe('Telegram intake keeps an update until the runtime admits its turn', () => {
  it('a turn the runtime had not admitted at shutdown stays in the journal and runs once after restart', async () => {
    const dir = mkdtempSync(join(tmpdir(), 'relay-tg-receipt-'));
    writeConfig(dir);
    const before = relayProcess(dir, { receipts: true });
    let after: ReturnType<typeof relayProcess> | undefined;
    try {
      await before.bots.start();
      textUpdate(CHAT_A, 'unadmitted');
      const first = lastUpdateId();
      await until(() => acknowledgedThrough(first), 'Telegram to acknowledge the update');
      // The send was made; the runtime has not admitted the turn.
      await new Promise(resolve => setTimeout(resolve, 30));
      expect(intakeIds(before.state)).toEqual([String(first)]);
      before.shutdown();
      expect(intakeIds(before.state)).toEqual([String(first)]);
      before.dispose();

      after = relayProcess(dir);
      await after.bots.start();
      await after.bots.notifyResumed();
      await until(() => chatATexts(after!.sent).length === 1, 'the turn after restart');
      await new Promise(resolve => setTimeout(resolve, 30));
      expect(chatATexts(after.sent)).toEqual(['unadmitted']);
      expect(chatATexts(before.sent)).toEqual([]);
      expect(intakeIds(after.state)).toEqual([]);
    } finally {
      before.bots.stop();
      after?.bots.stop();
      after?.dispose();
      rmSync(dir, { recursive: true, force: true });
    }
  });
});

describe('channel intake journal generations', () => {
  it('a record cut short in a replaced bot goes to the newest running bot, which alone replays it', async () => {
    const dir = mkdtempSync(join(tmpdir(), 'relay-intake-generations-'));
    try {
      const state = createStateManager(join(dir, 'state.json'));
      state.load();
      const older = createChannelIntakeJournal(state, 'telegram:pulse');
      const lanesOlder = createChatLanes(() => {});
      let cutShort!: (error: Error) => void;
      const record = older.accept('1', '7', { update_id: 7 });
      lanesOlder.run('1', () => runJournaled(older, record, () => new Promise((_resolve, reject) => { cutShort = reject; })));
      await until(() => cutShort !== undefined, 'the older bot to start handling');
      older.close();

      const newer = createChannelIntakeJournal(state, 'telegram:pulse');
      const handled: string[] = [];
      expect(replayIntakeJournal(createChatLanes(() => {}), newer, Promise.resolve(), async r => { handled.push(r.id); })).toBe(0);
      cutShort(new Error('download aborted'));
      await until(() => handled.length === 1, 'the newer bot to adopt the record');
      expect(handled).toEqual(['7']);
      await until(() => (state.getChannelIntake?.('telegram:pulse') ?? []).length === 0, 'the record to be released');
      state.dispose();
    } finally {
      rmSync(dir, { recursive: true, force: true });
    }
  });
});

describe('telegram intake lanes', () => {
  it('runs one key in order and different keys independently', async () => {
    const lanes = createChatLanes(() => {});
    const order: string[] = [];
    let releaseA!: () => void;
    lanes.run('a', () => new Promise<void>(resolve => { releaseA = () => { order.push('a1'); resolve(); }; }));
    lanes.run('a', async () => { order.push('a2'); });
    lanes.run('b', async () => { order.push('b1'); });
    await until(() => order.includes('b1'), 'lane b');
    expect(order).toEqual(['b1']);
    expect(lanes.pending('a')).toBe(2);
    releaseA();
    await until(() => order.length === 3, 'lane a');
    expect(order).toEqual(['b1', 'a1', 'a2']);
    expect(lanes.pending('a')).toBe(0);
  });

  it('reports a failed task and keeps the lane going', async () => {
    const errors: unknown[] = [];
    const lanes = createChatLanes((_key, error) => errors.push(error));
    const order: string[] = [];
    lanes.run('a', async () => { throw new Error('boom'); });
    lanes.run('a', async () => { order.push('after'); });
    await until(() => order.length === 1, 'next task');
    expect(String(errors[0])).toContain('boom');
  });

  it('names commands with or without the bot suffix', () => {
    expect(telegramCommandName('/stop')).toBe('stop');
    expect(telegramCommandName('/Stop@pulse_bot now')).toBe('stop');
    expect(telegramCommandName('stop')).toBeUndefined();
    expect(telegramCommandName('/stopping')).toBe('stopping');
  });
});

const { createSwitchBarrierStore, switchBarrierPath } = await import('../src/switch-barrier.js');

describe('Telegram intake under a durable switch barrier', () => {  const ME = { holder: 'oikos@ownermac', transaction: 'switch-test-1', token: 't'.repeat(40) };

  /** relayProcess with the barrier read from the data dir, the resume run as index.ts does. */
  function switchProcess(dir: string) {
    const state = createStateManager(join(dir, 'state.json'));
    state.load();
    const store = createSwitchBarrierStore(switchBarrierPath(dir));
    const barrier = createMaintenanceBarrier(Date.now, {
      switchBarrier: store,
      recordTurn: turn => persistQueuedTurns(state, [turn], Date.now()).length > 0 && (state.flushStrict(), true),
    });
    const startResume = barrier.startupResume();
    const { adapter, sent } = ccAdapter();
    const runtime = createConversationRuntimeManager({ barrier });
    runtime.register(adapter);
    const bots = createTelegramBots({ runtime, router: createRouter(join(dir, 'relay.json')), stateManager: state });
    return {
      bots, sent, state, store, barrier,
      resume: () => startResume(() => bots.notifyResumed()),
      shutdown(): void {
        barrier.close(turn => { persistQueuedTurns(state, [turn], Date.now()); state.flush(); });
        runtime.emitShutdown();
        bots.stop();
        state.flush();
      },
      crash(): void { bots.stop(); },
      dispose(): void { store.dispose(); state.dispose(); runtime.dispose(); },
    };
  }

  for (const ending of ['shutdown', 'crash'] as const) {
    it(`turns that arrive while held survive a ${ending}, wait in the new process, and run once in order after release`, async () => {
      const dir = mkdtempSync(join(tmpdir(), 'relay-tg-switch-'));
      writeConfig(dir);
      const before = switchProcess(dir);
      let after: ReturnType<typeof switchProcess> | undefined;
      try {
        await before.bots.start();
        before.resume();
        expect(before.store.acquire({ ...ME, leaseMs: 600_000 }).ok).toBe(true);
        textUpdate(CHAT_A, 'first');
        textUpdate(CHAT_A, 'second');
        const second = lastUpdateId();
        await until(() => acknowledgedThrough(second), 'Telegram to acknowledge both updates');
        await new Promise(resolve => setTimeout(resolve, 30));
        expect(before.sent).toEqual([]);
        // No receipt under the switch barrier: the journal still holds both.
        expect(intakeIds(before.state)).toEqual([String(second - 1), String(second)]);
        before[ending]();
        before.dispose();

        after = switchProcess(dir);
        expect(after.barrier.isHeld()).toBe(true);
        await after.bots.start();
        expect(after.resume()).toBe('deferred');
        textUpdate(CHAT_A, 'third');
        const third = lastUpdateId();
        await until(() => acknowledgedThrough(third), 'the new process to poll');
        await new Promise(resolve => setTimeout(resolve, 30));
        expect(after.sent).toEqual([]);
        expect(intakeIds(after.state)).toEqual([String(second - 1), String(second), String(third)]);

        expect(after.store.release(ME).ok).toBe(true);
        await until(() => chatATexts(after!.sent).length === 3, 'all three turns after release');
        await new Promise(resolve => setTimeout(resolve, 50));
        expect(chatATexts(after.sent)).toEqual(['first', 'second', 'third']);
        expect(intakeIds(after.state)).toEqual([]);
      } finally {
        before.bots.stop();
        after?.bots.stop();
        after?.dispose();
        rmSync(dir, { recursive: true, force: true });
      }
    });
  }
});
