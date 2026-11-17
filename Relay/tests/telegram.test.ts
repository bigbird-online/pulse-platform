import {nativeCoreFixture} from './helpers/native-work-item-core.js';
import { createStateManager } from '../src/state.js';
import { createMaintenanceBarrier } from '../src/maintenance-barrier.js';
import { persistQueuedTurns } from '../src/shutdown-interrupts.js';
import {latestHumanInput} from '../src/scl-human-decision.js';
import { isolateHumanDecisions } from './helpers/scl-human-decision.js';
isolateHumanDecisions();
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { EventEmitter } from 'node:events';
import { existsSync, mkdirSync, mkdtempSync, readFileSync, realpathSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { dirname, join } from 'node:path';
import { pathToFileURL } from 'node:url';
import { resolveToolFileOutboxPath } from '../src/tool-file-outbox.js';

interface MockBotInstance {
  token: string;
  options?: unknown;
  api: {
    config: { use: ReturnType<typeof vi.fn> };
    raw: {
      sendRichMessage: ReturnType<typeof vi.fn>;
      editMessageText: ReturnType<typeof vi.fn>;
    };
    sendMessage: ReturnType<typeof vi.fn>;
    sendDocument: ReturnType<typeof vi.fn>;
    editMessageText: ReturnType<typeof vi.fn>;
    deleteMessage: ReturnType<typeof vi.fn>;
    sendChatAction: ReturnType<typeof vi.fn>;
    setMyCommands: ReturnType<typeof vi.fn>;
    getFile: ReturnType<typeof vi.fn>;
  };
  handlers: Map<string, (ctx: Record<string, unknown>) => Promise<void>>;
  commandHandlers: Map<string, (ctx: Record<string, unknown>) => Promise<void>>;
  on: ReturnType<typeof vi.fn>;
  command: ReturnType<typeof vi.fn>;
  catch: ReturnType<typeof vi.fn>;
  start: ReturnType<typeof vi.fn>;
  stop: ReturnType<typeof vi.fn>;
}

const grammyMock = vi.hoisted(() => ({
  instances: [] as MockBotInstance[],
  invokeOnStart: true,
  menuRetryAfter: 0,
}));
const mediaDownload = vi.hoisted(() => vi.fn(async () => Buffer.from('image-fixture')));
vi.mock('../src/channels/telegram-download.js', async original => ({
  ...await original<typeof import('../src/channels/telegram-download.js')>(), downloadTelegramFile: mediaDownload,
}));

vi.mock('grammy', () => ({
  InputFile: class {
    constructor(public file: string, public filename?: string) {}
  },
  InlineKeyboard: class {
    rows: { text: string; callbackData: string }[][] = [[]];

    text(text: string, callbackData: string) {
      this.rows.at(-1)!.push({ text, callbackData });
      return this;
    }

    row() {
      this.rows.push([]);
      return this;
    }
  },
  Bot: class {
    token: string;
    options?: unknown;
    api = {
      getFile: vi.fn(async () => ({ file_path: 'photos/test.png' })),
      config: { use: vi.fn() },
      raw: {
        sendRichMessage: vi.fn(async () => ({ message_id: 901 })),
        editMessageText: vi.fn(async () => ({})),
      },
      sendMessage: vi.fn(async () => ({ message_id: 101 })),
      sendDocument: vi.fn(async () => ({ message_id: 202 })),
      editMessageText: vi.fn(async () => ({})),
      deleteMessage: vi.fn(async () => true),
      sendChatAction: vi.fn(async () => {}),
      setMyCommands: vi.fn(async () => {
        if (grammyMock.menuRetryAfter) {
          throw Object.assign(new Error('429: Too Many Requests'), {
            error_code: 429, parameters: { retry_after: grammyMock.menuRetryAfter } });
        }
      }),
    };
    handlers = new Map<string, (ctx: Record<string, unknown>) => Promise<void>>();
    commandHandlers = new Map<string, (ctx: Record<string, unknown>) => Promise<void>>();
    use = vi.fn();
    on = vi.fn((event: string, handler: (ctx: Record<string, unknown>) => Promise<void>) => {
      this.handlers.set(event, handler);
    });
    command = vi.fn((command: string, handler: (ctx: Record<string, unknown>) => Promise<void>) => {
      this.commandHandlers.set(command, handler);
    });
    catch = vi.fn();
    isInited = vi.fn(() => true);
    start = vi.fn((options?: { onStart?: () => void }) => {
      if (grammyMock.invokeOnStart) options?.onStart?.();
      return new Promise<void>(() => {});
    });
    stop = vi.fn();

    constructor(token: string, options?: unknown) {
      this.token = token;
      this.options = options;
      grammyMock.instances.push(this as unknown as MockBotInstance);
    }
  },
}));

import { RUNTIME_NOTIFICATION_TURN, tagRuntimeNotificationTurn } from '../src/runtime-background-work.js';
import { RUNTIME_NOTIFICATION_REPLY_LEAD } from '../src/channel-rendering.js';
import { createConversationRuntimeManager, type ConversationProcess, type ConversationRuntimeAdapter } from '../src/conversation-runtime.js';
import { createRouter } from '../src/router.js';
import { handleCommand as handleFeishuCommand, ensureProcess as ensureFeishuProcess, wirePoolEvents, type MessageHandlerContext } from '../src/channels/feishu.js';
import type { Router } from '../src/router.js';
import { appendTelegramLocalFiles, telegramImageMessage, buildRuntimePhotoMessage, createTelegramApiBot, createTelegramBots, createTelegramRenderingSink, expandWrapCommandPrompt, extractTelegramMenuRows, resolveTelegramAllowedTools, resolveWrapSkillPath, splitMessage, stripCCNoise, TELEGRAM_TURN_STATUS_TEXT } from '../src/channels/telegram.js';
import { resolveRichDocChannelProfiles, resolveTelegramRichMessageProfile } from '../src/channel-rich-rendering.js';
import { buildRuntimeUsageBinding, runtimeAuthorityRef } from '../src/runtime-usage-binding.js';
import type { AgentConfig, PersistedSession, StateManager, StreamJsonMessage, UserConfig } from '../src/types.js';

beforeEach(() => {
  grammyMock.instances.length = 0;
  grammyMock.invokeOnStart = true;
  grammyMock.menuRetryAfter = 0;
});

function anyTurnContext(): unknown {
  return expect.objectContaining({
    correlationId: expect.stringMatching(/^turn:/),
    idempotencyKey: expect.stringMatching(/^turn:/),
    effect: 'no-effect',
  });
}

describe('createTelegramApiBot', () => {
  it('configures Telegram API calls with the shared IPv4 HTTPS agent', () => {
    createTelegramApiBot('token');
    const instance = grammyMock.instances[0] as MockBotInstance & { options?: any };
    expect(instance.options?.client?.baseFetchConfig?.agent?.options?.family).toBe(4);
  });
});

describe('Telegram ingress readiness', () => {
  it('does not count a bot still retrying startup as a working Telegram channel', async () => {
    const dir = mkdtempSync(join(tmpdir(), 'relay-tg-startup-readiness-'));
    const runtime = createConversationRuntimeManager();
    try {
      const configPath = join(dir, 'relay.json');
      writeFileSync(configPath, JSON.stringify({ users: [{
        userId: 'owner', chatId: '111222333', defaultAgent: 'pulse',
        agents: [{ id: 'pulse', name: 'Pulse', emoji: 'P', botToken: '123456:test-token',
          workspace: dir, sessionId: 'session-pulse', runtime: 'pi' }],
      }] }));
      grammyMock.invokeOnStart = false;
      const bots = createTelegramBots({ runtime, router: createRouter(configPath), stateManager: createMemoryStateManager() });
      await bots.start();
      expect(grammyMock.instances[0]?.start).toHaveBeenCalledOnce();
      expect(bots.runningAgentIds()).toEqual([]);
      bots.stop();
    } finally {
      runtime.dispose();
      rmSync(dir, { recursive: true, force: true });
    }
  });

  it('starts every bot polling at once while menus wait out 429 in the background (2026-11-08)', async () => {
    vi.useFakeTimers();
    const dir = mkdtempSync(join(tmpdir(), 'relay-tg-menu-429-'));
    const runtime = createConversationRuntimeManager();
    try {
      const configPath = join(dir, 'relay.json');
      const ids = ['aki', 'pulse', 'steward', 'vigil'];
      writeFileSync(configPath, JSON.stringify({ users: [{
        userId: 'owner', chatId: '111222333', defaultAgent: 'pulse',
        agents: ids.map((id, index) => ({ id, name: id, emoji: 'P', botToken: `12345${index}:test-token`,
          workspace: dir, sessionId: `session-${id}`, runtime: 'pi' })),
      }] }));
      grammyMock.menuRetryAfter = 60;
      const bots = createTelegramBots({ runtime, router: createRouter(configPath), stateManager: createMemoryStateManager() });
      await bots.start();
      expect(grammyMock.instances.map(bot => bot.start.mock.calls.length)).toEqual([1, 1, 1, 1]);
      expect(bots.runningAgentIds()).toEqual(ids);
      // Menus are serialized: only the first bot has tried before its 60 s wait ends.
      expect(grammyMock.instances.map(bot => bot.api.setMyCommands.mock.calls.length)).toEqual([1, 0, 0, 0]);
      grammyMock.menuRetryAfter = 0;
      await vi.advanceTimersByTimeAsync(60_000);
      expect(grammyMock.instances.map(bot => bot.api.setMyCommands.mock.calls.length)).toEqual([2, 1, 1, 1]);
      bots.stop();
    } finally {
      vi.useRealTimers();
      runtime.dispose();
      rmSync(dir, { recursive: true, force: true });
    }
  });

  it('degrades when polling has not succeeded recently and recovers on a real getUpdates response', async () => {
    const dir = mkdtempSync(join(tmpdir(), 'relay-tg-readiness-'));
    let now = 1_000_000;
    const clock = vi.spyOn(Date, 'now').mockImplementation(() => now);
    const runtime = createConversationRuntimeManager();
    try {
      const configPath = join(dir, 'relay.json');
      writeFileSync(configPath, JSON.stringify({ users: [{
        userId: 'owner', chatId: '111222333', defaultAgent: 'pulse',
        agents: [{ id: 'pulse', name: 'Pulse', emoji: 'P', botToken: '123456:test-token',
          workspace: dir, sessionId: 'session-pulse', runtime: 'pi' }],
      }] }));
      const bots = createTelegramBots({ runtime, router: createRouter(configPath), stateManager: createMemoryStateManager() });
      await bots.start();
      const bot = grammyMock.instances[0];
      expect(bots.runningAgentIds()).toEqual(['pulse']);
      now += 91_000;
      expect(bots.runningAgentIds()).toEqual([]);
      const transformer = bot.api.config.use.mock.calls[0]?.[0] as
        (prev: (...args: unknown[]) => Promise<unknown>, method: string, payload: object) => Promise<unknown>;
      await transformer(async () => ({ ok: true, result: [] }), 'getUpdates', {});
      expect(bots.runningAgentIds()).toEqual(['pulse']);
      bots.stop();
      expect(bots.runningAgentIds()).toEqual([]);
    } finally {
      clock.mockRestore();
      runtime.dispose();
      rmSync(dir, { recursive: true, force: true });
    }
  });
});

describe('resolveTelegramAllowedTools', () => {
  it('uses the channel default tool list without AskUserQuestion', () => {
    const tools = resolveTelegramAllowedTools();
    expect(tools).toContain('Read');
    expect(tools).toContain('Agent');
    expect(tools).toContain('Task');
    expect(tools).not.toContain('AskUserQuestion');
  });

  it('preserves custom allowed tools while removing AskUserQuestion', () => {
    expect(resolveTelegramAllowedTools(['Read', 'AskUserQuestion', 'Bash'])).toEqual(['Read', 'Bash']);
  });
});

describe('extractTelegramMenuRows', () => {
  it('converts Pi thinking buttons while ignoring non-interactive card body elements', () => {
    const rows = extractTelegramMenuRows([
      { tag: 'markdown', content: 'Pi Thinking' },
      {
        tag: 'column_set',
        columns: [
          {
            tag: 'column',
            elements: [
              {
                tag: 'button',
                text: { tag: 'plain_text', content: '✓ high' },
                behaviors: [{ type: 'callback', value: { relay_menu: 'think', relay_menu_arg: 'high' } }],
              },
            ],
          },
          {
            tag: 'column',
            elements: [
              {
                tag: 'button',
                text: { tag: 'plain_text', content: 'low' },
                behaviors: [{ type: 'callback', value: { relay_menu: 'think', relay_menu_arg: 'low' } }],
              },
            ],
          },
        ],
      },
    ]);

    expect(rows).toEqual([[
      { text: '✓ high', action: 'think', arg: 'high' },
      { text: 'low', action: 'think', arg: 'low' },
    ]]);
  });

  it('does not expose unsupported Feishu menu actions on Telegram', () => {
    expect(extractTelegramMenuRows([
      {
        tag: 'column_set',
        columns: [{
          tag: 'column',
          elements: [{
            tag: 'button',
            text: { tag: 'plain_text', content: 'Delete' },
            behaviors: [{ type: 'callback', value: { relay_menu: 'delete', relay_menu_arg: 'all' } }],
          }],
        }],
      },
    ])).toEqual([]);
  });
});

describe('splitMessage', () => {
  it('returns single element for short text', () => {
    const result = splitMessage('Hello world');
    expect(result).toEqual(['Hello world']);
  });

  it('returns single element for exactly max length', () => {
    const text = 'a'.repeat(4096);
    const result = splitMessage(text);
    expect(result).toEqual([text]);
  });

  it('splits on paragraph breaks for long text', () => {
    const para1 = 'a'.repeat(2000);
    const para2 = 'b'.repeat(2000);
    const para3 = 'c'.repeat(2000);
    const text = `${para1}\n\n${para2}\n\n${para3}`;

    const result = splitMessage(text);
    expect(result.length).toBeGreaterThan(1);
    result.forEach((part) => {
      expect(part.length).toBeLessThanOrEqual(4096);
    });
    // Content preserved (join back and compare stripped)
    expect(result.join('').replace(/\n/g, '')).toBe(text.replace(/\n/g, ''));
  });

  it('splits on newlines when no paragraph breaks fit', () => {
    const lines = Array.from({ length: 100 }, (_, i) => `Line ${i}: ${'x'.repeat(80)}`);
    const text = lines.join('\n');

    const result = splitMessage(text);
    expect(result.length).toBeGreaterThan(1);
    result.forEach((part) => {
      expect(part.length).toBeLessThanOrEqual(4096);
    });
  });

  it('hard-cuts when single line exceeds max', () => {
    const text = 'x'.repeat(10000);
    const result = splitMessage(text);

    expect(result.length).toBe(3); // 4096 + 4096 + 1808
    expect(result[0].length).toBe(4096);
    expect(result[1].length).toBe(4096);
    expect(result[2].length).toBe(1808);
  });

  it('handles empty string', () => {
    expect(splitMessage('')).toEqual(['']);
  });

  it('respects custom maxLength', () => {
    const text = 'abcdefghij'; // 10 chars
    const result = splitMessage(text, 3);
    expect(result).toEqual(['abc', 'def', 'ghi', 'j']);
  });
});

describe('Telegram rendering contract', () => {
  it('uses stable, distinct state markers without inferring a business outcome', () => {
    expect(TELEGRAM_TURN_STATUS_TEXT).toEqual({
      accepted: '▶️ 已收到，开始处理…',
      generated: '✅ 本轮回复已生成',
      retrying: '🔄 遇到暂时问题，正在自动重试…',
      deliveryFailed: '⚠️ 最终回复投递失败，请重试。',
    });
  });

  it('shows an immediate busy frame and delivers one fresh authoritative final', async () => {
    const bot = createTelegramApiBot('token');
    const sink = createTelegramRenderingSink(bot);
    sink.begin('tenant:pi', 'tenant/session/turn');
    await sink.status('tenant:pi', 42, TELEGRAM_TURN_STATUS_TEXT.accepted, 'accepted');
    await sink.assistant('tenant:pi', 42, '先检查现场。', 'commentary', 'commentary-1');
    await sink.assistant('tenant:pi', 42, '最终结论。', 'final', 'final-1');

    const instance = grammyMock.instances.at(-1)!;
    expect(instance.api.sendMessage).toHaveBeenCalledTimes(3);
    expect(instance.api.sendMessage).toHaveBeenNthCalledWith(2, 42, '最终结论。');
    expect(instance.api.sendMessage).toHaveBeenLastCalledWith(42, TELEGRAM_TURN_STATUS_TEXT.generated);
    expect(instance.api.deleteMessage).toHaveBeenCalledWith(42, 101);
  });

  it('places the generated marker below the final answer and retires the earlier progress message', async () => {
    const bot = createTelegramApiBot('token');
    const sink = createTelegramRenderingSink(bot);
    sink.begin('tenant:pi', 'tenant/session/turn');
    await sink.status('tenant:pi', 42, TELEGRAM_TURN_STATUS_TEXT.accepted, 'accepted');
    await sink.assistant('tenant:pi', 42, '最终结论。', 'final', 'final-1');

    const instance = grammyMock.instances.at(-1)!;
    expect(instance.api.sendMessage.mock.calls.map((call) => call[1])).toEqual([
      TELEGRAM_TURN_STATUS_TEXT.accepted,
      '最终结论。',
      TELEGRAM_TURN_STATUS_TEXT.generated,
    ]);
    expect(instance.api.deleteMessage).toHaveBeenCalledWith(42, 101);
    expect(instance.api.sendMessage.mock.invocationCallOrder[1]).toBeLessThan(
      instance.api.deleteMessage.mock.invocationCallOrder[0]!,
    );
    expect(instance.api.deleteMessage.mock.invocationCallOrder[0]).toBeLessThan(
      instance.api.sendMessage.mock.invocationCallOrder[2]!,
    );
  });

  it('keeps tool-only work visible while leaving the final body clean', async () => {
    const bot = createTelegramApiBot('token');
    const sink = createTelegramRenderingSink(bot);
    sink.begin('tenant:pi', 'tenant/session/turn');
    await sink.assistant('tenant:pi', 42, '先检查现场。', 'commentary', 'commentary-1');
    await sink.activity('tenant:pi', 42, '⏳ 仍在工作，已推进 5 个执行步骤…', 'activity-5');
    await sink.assistant('tenant:pi', 42, '最终结论。', 'final', 'final-1');

    const instance = grammyMock.instances.at(-1)!;
    expect(instance.api.sendMessage).toHaveBeenCalledTimes(3);
    expect(instance.api.editMessageText).toHaveBeenNthCalledWith(
      1,
      42,
      101,
      '先检查现场。\n\n⏳ 仍在工作，已推进 5 个执行步骤…',
    );
    expect(instance.api.sendMessage).toHaveBeenNthCalledWith(2, 42, '最终结论。');
    expect(instance.api.sendMessage).toHaveBeenLastCalledWith(42, TELEGRAM_TURN_STATUS_TEXT.generated);
    expect(instance.api.deleteMessage).toHaveBeenCalledWith(42, 101);
  });

  it('edits one evolving message for Pi deltas and does not send streaming final twice', async () => {
    const bot = createTelegramApiBot('token');
    const sink = createTelegramRenderingSink(bot);
    sink.begin('tenant:pi', 'tenant/session/turn');
    await sink.delta('tenant:pi', 42, '已开工。', 'delta-1');
    await sink.delta('tenant:pi', 42, '第一竖切。', 'delta-2');
    await sink.assistant('tenant:pi', 42, '已开工。第一竖切。', 'final', 'final-1');
    await sink.result('tenant:pi', 42);

    const instance = grammyMock.instances.at(-1)!;
    expect(instance.api.sendMessage).toHaveBeenCalledTimes(3);
    expect(instance.api.sendMessage).toHaveBeenNthCalledWith(2, 42, '已开工。第一竖切。');
    expect(instance.api.sendMessage).toHaveBeenLastCalledWith(42, TELEGRAM_TURN_STATUS_TEXT.generated);
    expect(instance.api.deleteMessage).toHaveBeenCalledWith(42, 101);
  });

  it('sends a final-only turn exactly once without manufacturing a progress message', async () => {
    const bot = createTelegramApiBot('token');
    const sink = createTelegramRenderingSink(bot);
    sink.begin('tenant:pi', 'tenant/session/turn');
    await sink.assistant('tenant:pi', 42, '直接结论。', 'final', 'final-1');
    await sink.result('tenant:pi', 42);

    const instance = grammyMock.instances.at(-1)!;
    expect(instance.api.sendMessage).toHaveBeenCalledTimes(1);
    expect(instance.api.sendMessage).toHaveBeenCalledWith(42, '直接结论。');
    expect(instance.api.editMessageText).not.toHaveBeenCalled();
  });

  it('marks a legacy final as failed instead of claiming generation before delivery', async () => {
    const bot = createTelegramApiBot('token');
    const instance = grammyMock.instances.at(-1)!;
    instance.api.sendMessage
      .mockResolvedValueOnce({ message_id: 303 })
      .mockRejectedValueOnce(new Error('ECONNRESET after write'));
    const sink = createTelegramRenderingSink(bot);
    sink.begin('tenant:pi');

    await sink.status('tenant:pi', 42, TELEGRAM_TURN_STATUS_TEXT.accepted, 'accepted');
    await sink.assistant('tenant:pi', 42, '最终结论。', 'final', 'final-1');

    expect(instance.api.sendMessage).toHaveBeenCalledTimes(2);
    expect(instance.api.editMessageText).toHaveBeenLastCalledWith(
      42,
      303,
      TELEGRAM_TURN_STATUS_TEXT.deliveryFailed,
    );
  });

  it('coalesces failed edits and sends one readable authoritative final fallback', async () => {
    const bot = createTelegramApiBot('token');
    const instance = grammyMock.instances.at(-1)!;
    instance.api.editMessageText.mockRejectedValue(new Error('429 flood'));
    const sink = createTelegramRenderingSink(bot);
    sink.begin('tenant:pi');
    await sink.delta('tenant:pi', 42, '短前缀', 'delta-1');
    await sink.delta('tenant:pi', 42, '继续', 'delta-2');
    await sink.assistant('tenant:pi', 42, '短前缀继续完整终态', 'final', 'final-1');
    await sink.result('tenant:pi', 42);

    expect(instance.api.sendMessage).toHaveBeenCalledTimes(3);
    expect(instance.api.sendMessage).toHaveBeenNthCalledWith(2, 42, '短前缀继续完整终态');
    expect(instance.api.sendMessage).toHaveBeenLastCalledWith(42, TELEGRAM_TURN_STATUS_TEXT.generated);
    expect(instance.api.deleteMessage).toHaveBeenCalledWith(42, 101);
  });

  it('bounds queued updates while the first Telegram send is rate limited', async () => {
    const bot = createTelegramApiBot('token');
    const instance = grammyMock.instances.at(-1)!;
    let release!: () => void;
    instance.api.sendMessage.mockImplementationOnce(() => new Promise((resolve) => {
      release = () => resolve({ message_id: 202 });
    }));
    const sink = createTelegramRenderingSink(bot);
    sink.begin('tenant:pi');
    const first = sink.delta('tenant:pi', 42, 'a', 'delta-1');
    await vi.waitFor(() => expect(release).toEqual(expect.any(Function)));
    void sink.delta('tenant:pi', 42, 'b', 'delta-2');
    void sink.delta('tenant:pi', 42, 'c', 'delta-3');
    const final = sink.assistant('tenant:pi', 42, 'abc-final', 'final', 'final-1');
    release();
    await Promise.all([first, final]);

    expect(instance.api.sendMessage).toHaveBeenCalledTimes(3);
    expect(instance.api.deleteMessage).toHaveBeenCalledWith(42, 202);
    expect(instance.api.sendMessage).toHaveBeenNthCalledWith(2, 42, 'abc-final');
    expect(instance.api.sendMessage).toHaveBeenLastCalledWith(42, TELEGRAM_TURN_STATUS_TEXT.generated);
  });

  it('uses Rich HTML for complete snapshots and sends one fresh semantic final before retiring progress', async () => {
    const bot = createTelegramApiBot('token');
    const profile = resolveRichDocChannelProfiles({ RELAY_TELEGRAM_RICHDOC_MODE: 'on' }).telegram;
    const observations: Record<string, unknown>[] = [];
    const sink = createTelegramRenderingSink(bot, { richDocProfile: profile, observe: (event) => observations.push(event) });
    sink.begin('tenant:pi', 'opaque-turn-scope');
    await sink.status('tenant:pi', 42, TELEGRAM_TURN_STATUS_TEXT.accepted, 'accepted');
    await sink.assistant('tenant:pi', 42, '**完整答案** with [link](https://example.com)', 'final', 'final-1');
    await sink.result('tenant:pi', 42);
    await sink.assistant('tenant:pi', 42, '迟到的重复终态', 'final', 'late-final');

    const instance = grammyMock.instances.at(-1)!;
    expect(instance.api.sendMessage).toHaveBeenCalledTimes(3);
    expect(instance.api.sendMessage).toHaveBeenNthCalledWith(1, 42, TELEGRAM_TURN_STATUS_TEXT.accepted, { parse_mode: 'HTML' });
    expect(instance.api.sendMessage).toHaveBeenNthCalledWith(
      2,
      42,
      '<b>完整答案</b> with <a href="https://example.com/">link</a>',
      { parse_mode: 'HTML' },
    );
    expect(instance.api.deleteMessage).toHaveBeenCalledWith(42, 101);
    expect(instance.api.sendMessage).toHaveBeenNthCalledWith(3, 42, TELEGRAM_TURN_STATUS_TEXT.generated);
    expect(instance.api.sendMessage.mock.invocationCallOrder[1]).toBeLessThan(instance.api.deleteMessage.mock.invocationCallOrder[0]!);
    expect(instance.api.deleteMessage.mock.invocationCallOrder[0]).toBeLessThan(instance.api.sendMessage.mock.invocationCallOrder[2]!);
    expect(observations).toEqual(expect.arrayContaining([
      expect.objectContaining({ channel: 'telegram', layer: 'compiler', result: 'valid' }),
      expect.objectContaining({ channel: 'telegram', layer: 'transport', terminal: true, result: 'delivered' }),
    ]));
    expect(JSON.stringify(observations)).not.toMatch(/完整答案|example\.com|opaque-turn-scope/);
  });

  it('uses Rich Message HTML for native tables in streaming edits and the fresh final', async () => {
    const bot = createTelegramApiBot('token');
    const profile = resolveRichDocChannelProfiles({
      RELAY_TELEGRAM_RICHDOC_MODE: 'on',
      RELAY_TELEGRAM_RICHDOC_UPDATE_THROTTLE_MS: '50',
    }).telegram;
    const richMessageProfile = resolveTelegramRichMessageProfile({ RELAY_TELEGRAM_RICH_MESSAGE_MODE: 'on' });
    const sink = createTelegramRenderingSink(bot, { richDocProfile: profile, richMessageProfile });
    const table = '| 名称 | 数量 |\n| --- | ---: |\n| 苹果 | 2 |';

    sink.begin('tenant:pi', 'opaque-turn-scope');
    await sink.status('tenant:pi', 42, TELEGRAM_TURN_STATUS_TEXT.accepted, 'accepted');
    await sink.assistant('tenant:pi', 42, table, 'snapshot', 'snapshot-1');
    await sink.assistant('tenant:pi', 42, table, 'final', 'final-1');

    const instance = grammyMock.instances.at(-1)!;
    expect(instance.api.raw.sendRichMessage).toHaveBeenCalledTimes(2);
    expect(instance.api.raw.sendRichMessage).toHaveBeenNthCalledWith(1, {
      chat_id: 42,
      rich_message: { html: `<p>${TELEGRAM_TURN_STATUS_TEXT.accepted}</p>`, skip_entity_detection: true },
    });
    expect(instance.api.raw.editMessageText).toHaveBeenCalledWith({
      chat_id: 42,
      message_id: 901,
      rich_message: {
        html: expect.stringContaining('<table bordered striped compact>'),
        skip_entity_detection: true,
      },
    });
    expect(instance.api.raw.sendRichMessage).toHaveBeenNthCalledWith(2, {
      chat_id: 42,
      rich_message: {
        html: expect.stringContaining('<tr><th>名称</th><th align="right">数量</th></tr>'),
        skip_entity_detection: true,
      },
    });
    expect(instance.api.sendMessage).toHaveBeenCalledOnce();
    expect(instance.api.sendMessage).toHaveBeenCalledWith(42, TELEGRAM_TURN_STATUS_TEXT.generated);
    expect(instance.api.deleteMessage).toHaveBeenCalledWith(42, 901);
  });

  it('falls back native Rich Message HTML to legacy Rich HTML on a definite capability rejection', async () => {
    const bot = createTelegramApiBot('token');
    const instance = grammyMock.instances.at(-1)!;
    instance.api.raw.sendRichMessage.mockRejectedValueOnce(new Error('Bad Request: unsupported start tag table'));
    const profile = resolveRichDocChannelProfiles({ RELAY_TELEGRAM_RICHDOC_MODE: 'on' }).telegram;
    const richMessageProfile = resolveTelegramRichMessageProfile({ RELAY_TELEGRAM_RICH_MESSAGE_MODE: 'on' });
    const observations: Record<string, unknown>[] = [];
    const sink = createTelegramRenderingSink(bot, {
      richDocProfile: profile,
      richMessageProfile,
      observe: (event) => observations.push(event),
    });

    sink.begin('tenant:pi', 'opaque-turn-scope');
    await sink.assistant('tenant:pi', 42, '| A | B |\n| --- | --- |\n| 1 | 2 |', 'final', 'final-1');

    expect(instance.api.raw.sendRichMessage).toHaveBeenCalledTimes(1);
    expect(instance.api.sendMessage).toHaveBeenCalledWith(
      42,
      expect.stringContaining('<pre>| A | B |'),
      { parse_mode: 'HTML' },
    );
    expect(observations).toContainEqual(expect.objectContaining({
      action: 'fallback',
      fromMode: 'rich_message_html',
      toMode: 'rich_html',
      reason: 'format',
    }));
  });

  it('downgrades a rejected native streaming edit once and keeps the final on legacy HTML', async () => {
    const bot = createTelegramApiBot('token');
    const instance = grammyMock.instances.at(-1)!;
    instance.api.raw.editMessageText.mockRejectedValueOnce(new Error("Bad Request: can't parse rich message"));
    const profile = resolveRichDocChannelProfiles({
      RELAY_TELEGRAM_RICHDOC_MODE: 'on',
      RELAY_TELEGRAM_RICHDOC_UPDATE_THROTTLE_MS: '50',
    }).telegram;
    const richMessageProfile = resolveTelegramRichMessageProfile({ RELAY_TELEGRAM_RICH_MESSAGE_MODE: 'on' });
    const observations: Record<string, unknown>[] = [];
    const sink = createTelegramRenderingSink(bot, {
      richDocProfile: profile,
      richMessageProfile,
      observe: (event) => observations.push(event),
    });
    const table = '| A | B |\n| --- | --- |\n| 1 | 2 |';

    sink.begin('tenant:pi', 'opaque-turn-scope');
    await sink.status('tenant:pi', 42, TELEGRAM_TURN_STATUS_TEXT.accepted, 'accepted');
    await sink.assistant('tenant:pi', 42, table, 'snapshot', 'snapshot-1');
    await sink.assistant('tenant:pi', 42, table, 'final', 'final-1');

    expect(instance.api.raw.sendRichMessage).toHaveBeenCalledTimes(1);
    expect(instance.api.raw.editMessageText).toHaveBeenCalledTimes(1);
    expect(instance.api.editMessageText).toHaveBeenCalledWith(
      42,
      901,
      expect.stringContaining('<pre>| A | B |'),
      { parse_mode: 'HTML' },
    );
    expect(instance.api.sendMessage).toHaveBeenCalledTimes(2);
    expect(instance.api.sendMessage).toHaveBeenNthCalledWith(
      1,
      42,
      expect.stringContaining('<pre>| A | B |'),
      { parse_mode: 'HTML' },
    );
    expect(instance.api.sendMessage).toHaveBeenLastCalledWith(42, TELEGRAM_TURN_STATUS_TEXT.generated);
    expect(observations).toContainEqual(expect.objectContaining({
      action: 'fallback',
      fromMode: 'rich_message_html',
      toMode: 'rich_html',
      reason: 'format',
    }));
  });

  it('retries a definite native Rich Message rate limit without duplicating the final', async () => {
    const bot = createTelegramApiBot('token');
    const instance = grammyMock.instances.at(-1)!;
    instance.api.raw.sendRichMessage
      .mockRejectedValueOnce(new Error('429 flood retry_after 0'))
      .mockResolvedValueOnce({ message_id: 902 });
    const profile = resolveRichDocChannelProfiles({ RELAY_TELEGRAM_RICHDOC_MODE: 'on' }).telegram;
    const richMessageProfile = resolveTelegramRichMessageProfile({ RELAY_TELEGRAM_RICH_MESSAGE_MODE: 'on' });
    const sink = createTelegramRenderingSink(bot, { richDocProfile: profile, richMessageProfile });

    sink.begin('tenant:pi', 'opaque-turn-scope');
    await sink.assistant('tenant:pi', 42, '**one final**', 'final', 'final-1');
    await sink.result('tenant:pi', 42);

    expect(instance.api.raw.sendRichMessage).toHaveBeenCalledTimes(2);
    expect(instance.api.raw.sendRichMessage.mock.calls[0]).toEqual(instance.api.raw.sendRichMessage.mock.calls[1]);
    expect(instance.api.sendMessage).not.toHaveBeenCalled();
  });

  it('keeps native Rich Message shadow mode on the established Rich HTML transport', async () => {
    const bot = createTelegramApiBot('token');
    const profile = resolveRichDocChannelProfiles({ RELAY_TELEGRAM_RICHDOC_MODE: 'on' }).telegram;
    const richMessageProfile = resolveTelegramRichMessageProfile({ RELAY_TELEGRAM_RICH_MESSAGE_MODE: 'shadow' });
    const sink = createTelegramRenderingSink(bot, { richDocProfile: profile, richMessageProfile });

    sink.begin('tenant:pi', 'opaque-turn-scope');
    await sink.assistant('tenant:pi', 42, '**answer**', 'final', 'final-1');

    const instance = grammyMock.instances.at(-1)!;
    expect(instance.api.raw.sendRichMessage).not.toHaveBeenCalled();
    expect(instance.api.sendMessage).toHaveBeenCalledWith(42, '<b>answer</b>', { parse_mode: 'HTML' });
  });

  it('does not fall back an ambiguously failed native Rich Message final', async () => {
    const bot = createTelegramApiBot('token');
    const instance = grammyMock.instances.at(-1)!;
    instance.api.raw.sendRichMessage.mockRejectedValueOnce(new Error('ECONNRESET after write'));
    const profile = resolveRichDocChannelProfiles({ RELAY_TELEGRAM_RICHDOC_MODE: 'on' }).telegram;
    const richMessageProfile = resolveTelegramRichMessageProfile({ RELAY_TELEGRAM_RICH_MESSAGE_MODE: 'on' });
    const sink = createTelegramRenderingSink(bot, { richDocProfile: profile, richMessageProfile });

    sink.begin('tenant:pi', 'opaque-turn-scope');
    await sink.assistant('tenant:pi', 42, '| A | B |\n| --- | --- |\n| 1 | 2 |', 'final', 'final-1');

    expect(instance.api.raw.sendRichMessage).toHaveBeenCalledTimes(1);
    expect(instance.api.sendMessage).not.toHaveBeenCalled();
  });

  it('falls back Rich HTML to safe HTML and then plain text only on definite format rejection', async () => {
    const bot = createTelegramApiBot('token');
    const instance = grammyMock.instances.at(-1)!;
    instance.api.sendMessage
      .mockRejectedValueOnce(new Error("Bad Request: can't parse entities"))
      .mockRejectedValueOnce(new Error('Bad Request: unsupported start tag'))
      .mockResolvedValueOnce({ message_id: 303 });
    const profile = resolveRichDocChannelProfiles({ RELAY_TELEGRAM_RICHDOC_MODE: 'on' }).telegram;
    const observations: Record<string, unknown>[] = [];
    const sink = createTelegramRenderingSink(bot, { richDocProfile: profile, observe: (event) => observations.push(event) });
    sink.begin('tenant:pi', 'opaque-turn-scope');
    await sink.assistant('tenant:pi', 42, '**answer** [link](https://example.com)', 'final', 'final-1');

    expect(instance.api.sendMessage).toHaveBeenCalledTimes(3);
    expect(instance.api.sendMessage.mock.calls[0][2]).toEqual({ parse_mode: 'HTML' });
    expect(instance.api.sendMessage.mock.calls[1][2]).toEqual({ parse_mode: 'HTML' });
    expect(instance.api.sendMessage.mock.calls[2]).toEqual([42, 'answer link (https://example.com/)']);
    expect(observations.filter((event) => event['action'] === 'fallback')).toEqual([
      expect.objectContaining({ fromMode: 'rich_html', toMode: 'safe_html', reason: 'format' }),
      expect.objectContaining({ fromMode: 'safe_html', toMode: 'plain_text', reason: 'format' }),
    ]);
  });

  it('does not retry an ambiguous final send and terminalizes the progress message as failed', async () => {
    const bot = createTelegramApiBot('token');
    const instance = grammyMock.instances.at(-1)!;
    instance.api.sendMessage
      .mockResolvedValueOnce({ message_id: 404 })
      .mockRejectedValueOnce(new Error('ECONNRESET after write'));
    const profile = resolveRichDocChannelProfiles({ RELAY_TELEGRAM_RICHDOC_MODE: 'on' }).telegram;
    const sink = createTelegramRenderingSink(bot, { richDocProfile: profile });
    sink.begin('tenant:pi', 'opaque-turn-scope');
    await sink.status('tenant:pi', 42, TELEGRAM_TURN_STATUS_TEXT.accepted, 'accepted');
    await sink.assistant('tenant:pi', 42, '**answer**', 'final', 'final-1');
    await sink.result('tenant:pi', 42);

    expect(instance.api.sendMessage).toHaveBeenCalledTimes(2);
    expect(instance.api.editMessageText).toHaveBeenLastCalledWith(42, 404, '⚠️ 最终回复投递失败，请重试。');
  });

  it('still sends the generated marker below final when deleting progress fails', async () => {
    const bot = createTelegramApiBot('token');
    const instance = grammyMock.instances.at(-1)!;
    instance.api.deleteMessage.mockRejectedValueOnce(new Error('temporary ECONNRESET'));
    const profile = resolveRichDocChannelProfiles({ RELAY_TELEGRAM_RICHDOC_MODE: 'on' }).telegram;
    const sink = createTelegramRenderingSink(bot, { richDocProfile: profile });
    sink.begin('tenant:pi', 'opaque-turn-scope');
    await sink.status('tenant:pi', 42, TELEGRAM_TURN_STATUS_TEXT.accepted, 'accepted');
    await sink.assistant('tenant:pi', 42, 'answer', 'final', 'final-1');

    expect(instance.api.sendMessage).toHaveBeenCalledTimes(3);
    expect(instance.api.sendMessage).toHaveBeenNthCalledWith(2, 42, 'answer', { parse_mode: 'HTML' });
    expect(instance.api.sendMessage).toHaveBeenLastCalledWith(42, TELEGRAM_TURN_STATUS_TEXT.generated);
  });

  it('retries a definite rate-limit rejection without creating a second delivered final', async () => {
    const bot = createTelegramApiBot('token');
    const instance = grammyMock.instances.at(-1)!;
    instance.api.sendMessage
      .mockRejectedValueOnce(new Error('429 flood retry_after 0'))
      .mockResolvedValueOnce({ message_id: 505 });
    const profile = resolveRichDocChannelProfiles({ RELAY_TELEGRAM_RICHDOC_MODE: 'on' }).telegram;
    const sink = createTelegramRenderingSink(bot, { richDocProfile: profile });
    sink.begin('tenant:pi', 'opaque-turn-scope');
    await sink.assistant('tenant:pi', 42, '**one final**', 'final', 'final-1');
    await sink.result('tenant:pi', 42);

    expect(instance.api.sendMessage).toHaveBeenCalledTimes(2);
    expect(instance.api.sendMessage.mock.calls[0]).toEqual(instance.api.sendMessage.mock.calls[1]);
  });

  it('coalesces rapid RichDoc snapshots behind the configured update throttle', async () => {
    const bot = createTelegramApiBot('token');
    const instance = grammyMock.instances.at(-1)!;
    const profile = resolveRichDocChannelProfiles({
      RELAY_TELEGRAM_RICHDOC_MODE: 'on',
      RELAY_TELEGRAM_RICHDOC_UPDATE_THROTTLE_MS: '50',
    }).telegram;
    const sink = createTelegramRenderingSink(bot, { richDocProfile: profile });
    sink.begin('tenant:pi', 'opaque-turn-scope');
    await sink.delta('tenant:pi', 42, 'a', 'delta-1');
    const second = sink.delta('tenant:pi', 42, 'b', 'delta-2');
    const third = sink.delta('tenant:pi', 42, 'c', 'delta-3');
    await Promise.all([second, third]);

    expect(instance.api.sendMessage).toHaveBeenCalledTimes(1);
    expect(instance.api.editMessageText).toHaveBeenCalledTimes(1);
    expect(instance.api.editMessageText).toHaveBeenCalledWith(42, 101, 'abc', { parse_mode: 'HTML' });
  });

  it('shadow-compiles RichDoc while preserving the legacy transport payload', async () => {
    const bot = createTelegramApiBot('token');
    const profile = resolveRichDocChannelProfiles({ RELAY_TELEGRAM_RICHDOC_MODE: 'shadow' }).telegram;
    const observations: Record<string, unknown>[] = [];
    const sink = createTelegramRenderingSink(bot, { richDocProfile: profile, observe: (event) => observations.push(event) });
    sink.begin('tenant:pi', 'opaque-turn-scope');
    await sink.assistant('tenant:pi', 42, '**legacy visible**', 'final', 'final-1');

    const instance = grammyMock.instances.at(-1)!;
    expect(instance.api.sendMessage).toHaveBeenCalledWith(42, '**legacy visible**');
    expect(observations).toContainEqual(expect.objectContaining({ layer: 'compiler', result: 'valid' }));
  });
});

describe('stripCCNoise', () => {
  it('strips OAuth poll timeout lines', () => {
    const text = '你好\n第二次 OAuth 轮询也超时了，不影响——Key 已经在用了。\n继续讨论';
    expect(stripCCNoise(text)).toBe('你好\n继续讨论');
  });

  it('strips English OAuth pattern', () => {
    const text = 'Hello\nsecond OAuth poll timed out\nLet me help';
    expect(stripCCNoise(text)).toBe('Hello\nLet me help');
  });

  it('returns empty string when entire text is noise', () => {
    expect(stripCCNoise('OAuth 轮询超时了')).toBe('');
  });

  it('passes through clean text unchanged', () => {
    const text = '这是一段正常的回复内容。';
    expect(stripCCNoise(text)).toBe(text);
  });

  it('preserves blank lines between content', () => {
    const text = '段落一\n\n段落二';
    expect(stripCCNoise(text)).toBe('段落一\n\n段落二');
  });

  it('removes standalone [Request interrupted] marker lines', () => {
    expect(stripCCNoise('前文\n[Request interrupted]\n后文')).toBe('前文\n后文');
    expect(stripCCNoise('前文\n[Request interrupted by user]\n后文')).toBe('前文\n后文');
  });

  it('does not strip prose that merely contains the word interrupted', () => {
    const input = 'The connection was interrupted briefly.';
    expect(stripCCNoise(input)).toBe(input);
  });
});

describe('buildRuntimePhotoMessage', () => {
  it('keeps the existing CC image block shape', () => {
    const message = buildRuntimePhotoMessage({
      runtime: 'cc',
      mediaType: 'image/jpeg',
      base64: 'abc123',
      caption: 'look at this',
    });

    expect(message).toEqual({
      type: 'human',
      content: [
        { type: 'image', source: { type: 'base64', media_type: 'image/jpeg', data: 'abc123' } },
        { type: 'text', text: 'look at this' },
      ],
    });
  });

  it('turns TG photo plus caption into a Codex image attachment and stdin prompt', () => {
    const message = buildRuntimePhotoMessage({
      runtime: 'codex',
      mediaType: 'image/png',
      filePath: '/tmp/relay-tg-photo/image.png',
      caption: 'describe this image',
    });

    expect(message).toEqual({
      type: 'human',
      content: 'describe this image',
      attachments: [
        {
          kind: 'image',
          path: '/tmp/relay-tg-photo/image.png',
          mediaType: 'image/png',
          cleanup: true,
        },
      ],
    });
  });
});

function processFor(tenantId: string, runtime: 'cc' | 'codex' | 'pi'): ConversationProcess {
  return {
    tenantId,
    runtime,
    sessionId: `${runtime}-session`,
    state: 'ready',
    lastActivity: Date.now(),
    spawnedAt: Date.now(),
    restartCount: 0,
    restartWindowStart: Date.now(),
  };
}

function createMockAdapter(id: 'cc' | 'codex' | 'pi'): ConversationRuntimeAdapter & { events: EventEmitter } {
  const events = new EventEmitter();
  const processes = new Map<string, ConversationProcess>();
  const yoloModes = new Map<string, boolean>();
  return {
    events,
    id,
    spawn: vi.fn(async (config) => {
      const proc = processFor(config.tenantId, id);
      proc.sessionId = config.sessionId || proc.sessionId;
      processes.set(config.tenantId, proc);
      return proc;
    }),
    send: vi.fn(async () => {}),
    kill: vi.fn(async (tenantId: string) => {
      processes.delete(tenantId);
    }),
    killAll: vi.fn(async () => {
      processes.clear();
    }),
    get: vi.fn((tenantId: string) => processes.get(tenantId)),
    getAll: vi.fn(() => Array.from(processes.values())),
    getStatus: vi.fn(() => Array.from(processes.values()).map((proc) => ({
      tenantId: proc.tenantId,
      runtime: proc.runtime,
      state: proc.state,
      pid: undefined,
      lastActivity: proc.lastActivity,
      spawnedAt: proc.spawnedAt,
      restartCount: proc.restartCount,
    }))),
    ...(id === 'codex' ? {
      setYoloMode: vi.fn((tenantId: string, enabled: boolean) => {
        if (enabled) yoloModes.set(tenantId, true);
        else yoloModes.delete(tenantId);
      }),
      getYoloMode: vi.fn((tenantId: string) => yoloModes.get(tenantId) ?? false),
    } : {}),
    on: vi.fn((event: string, handler: (...args: unknown[]) => void) => {
      events.on(event, handler);
    }) as ConversationRuntimeAdapter['on'],
    emitShutdown: vi.fn(() => new Set<string>()),
    dispose: vi.fn(),
  };
}

function createMemoryStateManager(): StateManager & { updates: PersistedSession[] } {
  const sessions: Record<string, PersistedSession> = {};
  const updates: PersistedSession[] = [];
  return {
    updates,
    load: () => ({ version: 1, updatedAt: new Date().toISOString(), sessions }),
    getSession: (poolKey: string) => sessions[poolKey],
    updateSession: (poolKey: string, update: Partial<PersistedSession>) => {
      const next = { ...sessions[poolKey], ...update } as PersistedSession;
      sessions[poolKey] = next;
      updates.push(next);
    },
    removeSession: (poolKey: string) => {
      delete sessions[poolKey];
    },
    getAllSessions: () => sessions,
    getPendingSclIntents: () => ({}),
    upsertPendingSclIntent: vi.fn(),
    removePendingSclIntent: vi.fn(),
    flush: vi.fn(),
    flushStrict: vi.fn(),
    dispose: vi.fn(),
  };
}

describe('Telegram media and links reach every runtime', () => {
  async function run(runtimeId: 'cc' | 'codex' | 'pi', event: string, message: Record<string, unknown>,
    setup?: (dir: string, message: Record<string, unknown>) => void,
    context: Record<string, unknown> = {}, expectedWarning?: string, failLookupOnce = false) {
    const dir = realpathSync(mkdtempSync(join(tmpdir(), 'relay-tg-media-')));
    setup?.(dir, message);
    const config = join(dir, 'relay.json');
    writeFileSync(config, JSON.stringify({ users: [{ userId: 'owner', chatId: '111222333', defaultAgent: 'pulse',
      telegramLocalFileRoots: [dir], agents: [{ id: 'pulse', name: 'Pulse', emoji: 'P', botToken: '123456:fixture',
        workspace: dir, runtime: runtimeId, conversationRuntimePreference: [runtimeId], conversationRuntime: { runtime: runtimeId } }] }] }));
    const adapter = createMockAdapter(runtimeId), runtime = createConversationRuntimeManager();
    runtime.register(adapter);
    const bots = createTelegramBots({ runtime, router: createRouter(config), stateManager: createMemoryStateManager() });
    const reply = vi.fn(async () => {});
    try {
      await bots.start();
      if (failLookupOnce) grammyMock.instances.at(-1)!.api.getFile.mockRejectedValueOnce(
        Object.assign(new Error('Gateway Timeout'), { error_code: 504 }));
      await grammyMock.instances.at(-1)!.handlers.get(event)!({ chat: { id: 111222333, type: 'private' }, from: { id: 111222333 }, message, reply, ...context });
      if (failLookupOnce) expect(grammyMock.instances.at(-1)!.api.getFile).toHaveBeenCalledTimes(2);
      await vi.waitFor(() => expect(adapter.send).toHaveBeenCalledOnce());
      if (expectedWarning) expect(reply).toHaveBeenCalledWith(expect.stringContaining(expectedWarning));
      else expect(reply).not.toHaveBeenCalled();
      const sent = vi.mocked(adapter.send).mock.calls[0][1];
      const attachments = Array.isArray(sent.attachments) ? sent.attachments : [];
      const imageBytes = attachments.map(a => readFileSync(a.path).toString());
      for (const a of attachments) if (a.cleanupRoot) rmSync(a.cleanupRoot, { recursive: true, force: true });
      return { sent, imageBytes };
    } finally { bots.stop(); runtime.dispose(); rmSync(dir, { recursive: true, force: true }); }
  }
  it.each(['cc', 'codex', 'pi'] as const)('%s receives both screenshots and image documents, including caption link targets', async runtime => {
    for (const [event, media] of [
      ['message:photo', { photo: [{ file_id: 'fixture' }] }],
      ['message:document', { document: { file_id: 'fixture', file_name: 'screen.png', mime_type: 'image/png' } }],
    ] as const) {
      const { sent, imageBytes } = await run(runtime, event, { ...media, caption: '看原文',
        caption_entities: [{ type: 'text_link', offset: 1, length: 2, url: 'https://example.org/source' }] }, undefined, {}, undefined, true);
      expect(JSON.stringify(sent.content)).toContain('https://example.org/source');
      if (runtime === 'codex') {
        expect(imageBytes).toEqual(['image-fixture']);
        expect(JSON.stringify(sent.content)).not.toContain('"type":"image"');
      } else expect(sent.content).toEqual(expect.arrayContaining([expect.objectContaining({ type: 'image',
        source: expect.objectContaining({ type: 'base64', data: Buffer.from('image-fixture').toString('base64') }) })]));
    }
  });
  it.each(['cc', 'codex', 'pi'] as const)('%s receives hidden/quoted links and the exact local HTML bytes', async runtime => {
    const { sent } = await run(runtime, 'message:text', { text: '看图解 https://example.org/plain',
      reply_to_message: { text: '原文', entities: [{ type: 'text_link', offset: 0, length: 2, url: 'https://example.org/original' }] } },
    (dir, message) => {
      const file = join(dir, 'diagram with spaces.html'); writeFileSync(file, '<p>LOCAL-LINK-587</p>');
      message.entities = [{ type: 'text_link', offset: 1, length: 2, url: pathToFileURL(file).href + '#1' }];
    });
    const text = JSON.stringify(sent.content);
    expect(text).toContain('https://example.org/plain'); expect(text).toContain('https://example.org/original');
    expect(text).toContain('LOCAL-LINK-587');
  });
  it.each(['cc', 'codex', 'pi'] as const)('%s ignores file links in old quoted messages', async runtime => {
    const { sent } = await run(runtime, 'message:text', { text: '继续', reply_to_message: { text: 'file:///missing-old-page.html' } });
    expect(JSON.stringify(sent.content)).toContain('继续');
    expect(JSON.stringify(sent.content)).not.toContain('链接读取失败');
  });
  it.each(['cc', 'codex', 'pi'] as const)('%s delivers a valid picture even when its current caption has a broken file link', async runtime => {
    for (const [event, media] of [
      ['message:photo', { photo: [{ file_id: 'fixture' }] }],
      ['message:document', { document: { file_id: 'fixture', file_name: 'screen.png', mime_type: 'image/png' } }],
    ] as const) {
      const { sent, imageBytes } = await run(runtime, event, { ...media, caption: '看图 file:///missing-current-page.html' },
        undefined, {}, '链接读取失败');
      expect(JSON.stringify(sent.content)).toContain('本地文件未附加');
      if (runtime === 'codex') expect(imageBytes).toEqual(['image-fixture']);
      else expect(JSON.stringify(sent.content)).toContain('"type":"image"');
    }
  });
  it.each(['cc', 'codex', 'pi'] as const)('%s still receives the text request when a file link is unavailable', async runtime => {
    const { sent } = await run(runtime, 'message:text', { text: '解释这个问题 file:///missing-page.html' }, undefined, {}, '链接读取失败');
    expect(JSON.stringify(sent.content)).toContain('解释这个问题');
  });
  it('does not attach local files for a group or a different sender', async () => {
    for (const context of [{ chat: { id: 111222333, type: 'group' }, from: { id: 111222333 } },
      { chat: { id: 111222333, type: 'private' }, from: { id: 999 } }]) {
      const { sent } = await run('cc', 'message:text', { text: '' }, (dir, message) => {
        const file = join(dir, 'page.html'); writeFileSync(file, 'DO-NOT-EXPOSE'); message.text = pathToFileURL(file).href;
      }, context, '已绑定用户的私聊');
      expect(JSON.stringify(sent.content)).not.toContain('DO-NOT-EXPOSE');
    }
  });

});

describe('dual-channel session isolation', () => {
  function fixture() {
    const dir = mkdtempSync(join(tmpdir(), 'relay-dual-session-'));
    const path = join(dir, 'relay.json');
    writeFileSync(path, JSON.stringify({ users: [{ userId: 'owner', chatId: '111222333',
      feishuOpenId: 'ou_fixture', defaultAgent: 'pulse', agents: [{ id: 'pulse', name: 'Pulse', emoji: 'P',
        botToken: '123456:fixture', workspace: dir, sessionId: 'tg-original', runtime: 'cc',
        conversationRuntimePreference: ['cc'], conversationRuntime: { runtime: 'cc' },
        feishu: { appId: 'fixture', appSecret: 'fixture', sessionId: 'feishu-original' } }],
    }] }));
    const router = createRouter(path), state = createMemoryStateManager();
    const cc = createMockAdapter('cc'), runtime = createConversationRuntimeManager();
    runtime.register(cc);
    for (const [key, sessionId] of [['owner:pulse', 'tg-original'], ['owner:pulse:feishu', 'feishu-original']]) {
      state.updateSession(key, { poolKey: key, agentId: 'pulse', runtime: 'cc', sessionId,
        status: 'active', lastActivity: Date.now() });
    }
    const user = router.getAllUsers()[0], agent = user.agents[0];
    const feishu = { agent, router, pool: runtime, stateManager: state, poolKeyToOpenId: new Map(),
      sendTextMessage: vi.fn(async () => {}), sendCardMessage: vi.fn(async () => {}),
    } as unknown as MessageHandlerContext;
    return { dir, router, state, runtime, cc, user, agent, feishu };
  }

  it.each(['telegram', 'feishu'] as const)('%s reset preserves the other channel formal WI and usage authority', async channel => {
    const f = fixture();
    const bots = createTelegramBots({ runtime: f.runtime, router: f.router, stateManager: f.state });
    try {
      const peer = channel === 'telegram' ? 'owner:pulse:feishu' : 'owner:pulse';
      const existing = f.state.getSession(peer)!;
      // The opaque authority is a fixture: the other channel must never consume or alter it.
      f.state.updateSession(peer, { turnCount: 37, maxTurns: 1000,
        activeWorkItemRuntimeBinding: { schema: 'pulse_active_work_item_runtime_binding/v1',
          workItemId: 'wi_20990101_003', sessionId: existing.sessionId, sessionRef: 'session_v1_peer',
          runtime: 'cc', workspace: '/registered/peer', usageBinding: { marker: 'peer-usage' },
        } as unknown as NonNullable<PersistedSession['activeWorkItemRuntimeBinding']> });
      const original = JSON.stringify(f.state.getSession(peer));
      await bots.start();
      if (channel === 'feishu') await handleFeishuCommand(f.feishu, '/reset', 'ou_fixture', { user: f.user });
      else await grammyMock.instances[0].commandHandlers.get('reset')!({ chat: { id: 111222333 }, reply: vi.fn(async () => {}) });
      expect(JSON.stringify(f.state.getSession(peer))).toBe(original);
      expect(f.cc.kill).not.toHaveBeenCalledWith(peer);
    } finally { bots.stop(); f.runtime.dispose(); rmSync(f.dir, { recursive: true, force: true }); }
  });

  it.each(['new', 'reset'])('Feishu /%s preserves TG state and cold-starts the original TG session', async command => {
    const f = fixture();
    const bots = createTelegramBots({ runtime: f.runtime, router: f.router, stateManager: f.state });
    try {
      await bots.start();
      const original = JSON.stringify(f.state.getSession('owner:pulse'));
      await handleFeishuCommand(f.feishu, '/' + command, 'ou_fixture', { user: f.user });
      expect(JSON.stringify(f.state.getSession('owner:pulse'))).toBe(original);
      expect(f.agent.sessionId).toBe('tg-original');
      expect(f.agent.feishu!.sessionId).not.toBe('feishu-original');
      const reply = vi.fn(async () => {});
      await grammyMock.instances[0].handlers.get('message:text')!({ chat: { id: 111222333 },
        message: { text: 'continue TG' }, reply });
      expect(f.cc.spawn).toHaveBeenCalledWith(expect.objectContaining({
        tenantId: 'owner:pulse', sessionId: 'tg-original',
        runtimeSessionBinding: { runtime: 'cc', sessionId: 'tg-original' },
      }));
      expect(f.cc.send).toHaveBeenCalled();
      expect(reply).not.toHaveBeenCalled();
    } finally { bots.stop(); f.runtime.dispose(); rmSync(f.dir, { recursive: true, force: true }); }
  });

  it.each(['new', 'reset'])('TG /%s preserves Feishu state and cold-starts its original session', async command => {
    const f = fixture();
    const bots = createTelegramBots({ runtime: f.runtime, router: f.router, stateManager: f.state });
    try {
      await bots.start();
      const original = JSON.stringify(f.state.getSession('owner:pulse:feishu'));
      if (command === 'new') {
        await grammyMock.instances[0].handlers.get('message:text')!({ chat: { id: 111222333 },
          message: { text: '/wrap' }, reply: vi.fn(async () => {}) });
      }
      await grammyMock.instances[0].commandHandlers.get(command)!({ chat: { id: 111222333 },
        message: { text: '/' + command }, reply: vi.fn(async () => {}) });
      expect(JSON.stringify(f.state.getSession('owner:pulse:feishu'))).toBe(original);
      expect(f.agent.sessionId).not.toBe('tg-original');
      expect(f.agent.feishu!.sessionId).toBe('feishu-original');
      expect(await ensureFeishuProcess(f.feishu, { user: f.user })).toBe('owner:pulse:feishu');
      expect(f.cc.spawn).toHaveBeenCalledWith(expect.objectContaining({
        tenantId: 'owner:pulse:feishu', sessionId: 'feishu-original',
      }));
    } finally { bots.stop(); f.runtime.dispose(); rmSync(f.dir, { recursive: true, force: true }); }
  });

  it.each(['message:text', 'message:photo'])('%s cold-starts the persisted TG identity when route metadata is stale', async event => {
    const f = fixture();
    const bots = createTelegramBots({ runtime: f.runtime, router: f.router, stateManager: f.state });
    try {
      f.router.overrideSessionId('owner', 'pulse', 'stale-route', 'tg');
      await bots.start();
      const reply = vi.fn(async () => {});
      // Photo download is deliberately stopped after startup; this assertion
      // verifies the shared media startup path without making external calls.
      await grammyMock.instances[0].handlers.get(event)!({ chat: { id: 111222333 },
        message: { text: 'resume', photo: [{ file_id: 'fixture' }] }, reply });
      expect(f.cc.spawn).toHaveBeenCalledWith(expect.objectContaining({
        tenantId: 'owner:pulse', sessionId: 'tg-original',
        runtimeSessionBinding: { runtime: 'cc', sessionId: 'tg-original' },
      }));
      expect(reply.mock.calls.flat().join(' ')).not.toContain('runtime_start_failed');
    } finally { bots.stop(); f.runtime.dispose(); rmSync(f.dir, { recursive: true, force: true }); }
  });
  it.each(['telegram', 'feishu'] as const)('%s /restart uses its own configured fallback when no state exists', async channel => {
    const f = fixture();
    const bots = createTelegramBots({ runtime: f.runtime, router: f.router, stateManager: f.state });
    try {
      f.state.removeSession('owner:pulse'); f.state.removeSession('owner:pulse:feishu');
      await bots.start();
      if (channel === 'feishu') await handleFeishuCommand(f.feishu, '/restart', 'ou_fixture', { user: f.user });
      else await grammyMock.instances[0].commandHandlers.get('restart')!({ chat: { id: 111222333 }, reply: vi.fn(async () => {}) });
      expect(f.cc.spawn).toHaveBeenCalledWith(expect.objectContaining({
        tenantId: channel === 'feishu' ? 'owner:pulse:feishu' : 'owner:pulse',
        sessionId: channel === 'feishu' ? 'feishu-original' : 'tg-original',
      }));
    } finally { bots.stop(); f.runtime.dispose(); rmSync(f.dir, { recursive: true, force: true }); }
  });

  it('Feishu session-chain recovery updates only the scoped route and state', () => {
    const f = fixture();
    try {
      const original = JSON.stringify(f.state.getSession('owner:pulse'));
      wirePoolEvents(f.agent, f.runtime, new Map(), f.router, vi.fn(), vi.fn(), undefined, undefined, undefined, f.state);
      f.cc.events.emit('session-chain-recovery', 'owner:pulse:feishu', { newSessionId: 'feishu-recovered' });
      expect(f.agent.sessionId).toBe('tg-original');
      expect(f.agent.feishu!.sessionId).toBe('feishu-recovered');
      expect(f.state.getSession('owner:pulse:feishu')?.sessionId).toBe('feishu-recovered');
      expect(JSON.stringify(f.state.getSession('owner:pulse'))).toBe(original);
    } finally { f.runtime.dispose(); rmSync(f.dir, { recursive: true, force: true }); }
  });

  it('a Feishu-only cold start uses persisted identity ahead of stale configuration', async () => {
    const f = fixture();
    try {
      delete f.agent.botToken;
      f.agent.feishu!.sessionId = 'stale-feishu-route';
      f.state.updateSession('owner:pulse', { sessionId: 'feishu-original' });
      expect(await ensureFeishuProcess(f.feishu, { user: f.user })).toBe('owner:pulse');
      expect(f.cc.spawn).toHaveBeenCalledWith(expect.objectContaining({
        tenantId: 'owner:pulse', sessionId: 'feishu-original',
      }));
    } finally { f.runtime.dispose(); rmSync(f.dir, { recursive: true, force: true }); }
  });
});

describe('TG conversation runtime fallback', () => {
  it.each(['cold', 'dead'] as const)('loads the agent subscription home on a %s text spawn', async (state) => {
    const dir = mkdtempSync(join(tmpdir(), 'relay-tg-subscription-home-'));
    const runtime = createConversationRuntimeManager();
    const adapter = createMockAdapter('cc');
    const userHome = join(dir, 'subscription-home');
    const spawn = adapter.spawn;
    adapter.spawn = vi.fn(async (config) => {
      // Authentication is configured in the agent home, not the service home.
      if (config.userHome !== userHome || config.linuxUser !== 'tenant-cc') {
        throw new Error('Subscription configuration was not loaded');
      }
      return spawn(config);
    });
    runtime.register(adapter);
    try {
      const configPath = join(dir, 'relay.json');
      writeFileSync(configPath, JSON.stringify({ users: [{
        userId: 'owner', chatId: '111222333', defaultAgent: 'cc',
        agents: [{ id: 'cc', name: 'CC', emoji: 'C', botToken: '123456:test-token',
          workspace: dir, runtime: 'cc', sessionId: 'subscription-session',
          userHome, linuxUser: 'tenant-cc' }],
      }] }));
      if (state === 'dead') {
        const proc = await runtime.spawn({ tenantId: 'owner:cc', runtime: 'cc',
          workspace: dir, sessionId: 'subscription-session', userHome, linuxUser: 'tenant-cc' });
        proc.state = 'dead';
      }
      await createTelegramBots({ runtime, router: createRouter(configPath), stateManager: createMemoryStateManager() }).start();
      const bot = grammyMock.instances[0];
      const reply = vi.fn(async () => {});
      await bot.handlers.get('message:text')!({ chat: { id: 111222333 }, message: { text: 'hello' }, reply });
      expect(adapter.spawn).toHaveBeenLastCalledWith(expect.objectContaining({ userHome, linuxUser: 'tenant-cc' }));
      expect(adapter.send).toHaveBeenCalledWith('owner:cc', expect.objectContaining({ type: 'human', content: 'hello' }), anyTurnContext());
      expect(reply).not.toHaveBeenCalled();
    } finally {
      runtime.dispose();
      rmSync(dir, { recursive: true, force: true });
    }
  });

  it('delivers this session\'s outbox files to the Telegram chat and rejects other sessions\' files [wi_20261004_159]', async () => {
    const dir = mkdtempSync(join(tmpdir(), 'relay-tg-tool-files-'));
    const previousOutboxRoot = process.env['RELAY_OUTBOX_DIR'];
    delete process.env['RELAY_OUTBOX_DIR'];
    const runtime = createConversationRuntimeManager();
    const adapter = createMockAdapter('cc');
    runtime.register(adapter);
    const log = vi.spyOn(console, 'log').mockImplementation(() => undefined);
    const warn = vi.spyOn(console, 'warn').mockImplementation(() => undefined);
    try {
      const configPath = join(dir, 'relay.json');
      writeFileSync(configPath, JSON.stringify({ users: [{
        userId: 'owner', chatId: '111222333', defaultAgent: 'pulse',
        agents: [{ id: 'pulse', name: 'Pulse', emoji: 'P', botToken: '123456:test-token',
          workspace: dir, sessionId: 'native-cc', runtime: 'cc' }],
      }] }));
      await createTelegramBots({ runtime, router: createRouter(configPath), stateManager: createMemoryStateManager() }).start();
      const bot = grammyMock.instances[0];
      await bot.handlers.get('message:text')?.({ chat: { id: 111222333 }, message: { text: 'write notes' }, reply: vi.fn(async () => {}) });

      const own = join(resolveToolFileOutboxPath('owner:pulse', dir), 'wi297-cc-followups.md');
      const feishuSession = join(resolveToolFileOutboxPath('owner:pulse:feishu', dir), 'feishu-only.md');
      mkdirSync(dirname(own), { recursive: true });
      mkdirSync(dirname(feishuSession), { recursive: true });
      writeFileSync(own, 'written in the Telegram turn');
      writeFileSync(feishuSession, 'belongs to the Feishu conversation');

      adapter.events.emit('tool-files', 'owner:pulse', [feishuSession, own], dir);
      await vi.waitFor(() => expect(existsSync(own)).toBe(false));

      expect(bot.api.sendDocument).toHaveBeenCalledTimes(1);
      expect(bot.api.sendDocument).toHaveBeenCalledWith(111222333, expect.objectContaining({ filename: 'wi297-cc-followups.md' }), undefined);
      expect(existsSync(feishuSession)).toBe(true);
      expect(log).toHaveBeenCalledWith(expect.objectContaining({ msg: 'Tool file delivered', channel: 'telegram', path: own }));
      expect(warn).toHaveBeenCalledWith(expect.objectContaining({ msg: 'Tool file delivery rejected', path: feishuSession }));
    } finally {
      if (previousOutboxRoot === undefined) delete process.env['RELAY_OUTBOX_DIR'];
      else process.env['RELAY_OUTBOX_DIR'] = previousOutboxRoot;
      runtime.dispose();
      rmSync(dir, { recursive: true, force: true });
    }
  });

  it('delivers a safe native runtime failure to TG once after a failed result', async () => {
    const dir = mkdtempSync(join(tmpdir(), 'relay-tg-runtime-failure-'));
    const runtime = createConversationRuntimeManager();
    const adapter = createMockAdapter('cc');
    runtime.register(adapter);
    let observedErrors = 0;
    runtime.on('error', () => { observedErrors++; });
    try {
      const configPath = join(dir, 'relay.json');
      writeFileSync(configPath, JSON.stringify({ users: [{
        userId: 'owner', chatId: '111222333', defaultAgent: 'pulse',
        agents: [{ id: 'pulse', name: 'Pulse', emoji: 'P', botToken: '123456:test-token',
          workspace: dir, sessionId: 'native-cc', runtime: 'cc' }],
      }] }));
      await createTelegramBots({ runtime, router: createRouter(configPath), stateManager: createMemoryStateManager() }).start();
      const bot = grammyMock.instances[0];
      await bot.handlers.get('message:text')?.({ chat: { id: 111222333 }, message: { text: 'run task' }, reply: vi.fn(async () => {}) });
      adapter.events.emit('response', 'owner:pulse', {
        type: 'result', is_error: true, errors: ['upstream ECONNRESET token=secret-key'],
      });
      await vi.waitFor(() => expect(bot.api.sendMessage.mock.calls.some((call) => String(call[1]).includes('ECONNRESET'))).toBe(true));
      adapter.events.emit('error', 'owner:pulse', new Error('upstream ECONNRESET token=secret-key'));
      await vi.waitFor(() => expect(observedErrors).toBe(1));
      const notices = bot.api.sendMessage.mock.calls.map((call) => String(call[1])).filter((text) => text.includes('ECONNRESET'));
      expect(notices).toHaveLength(1);
      expect(notices[0]).not.toContain('secret-key');
      adapter.events.emit('turn-accepted', 'owner:pulse');
      adapter.events.emit('error', 'owner:pulse', new TypeError('fetch failed', { cause: new Error('ENOTFOUND token=secret-key') }));
      await vi.waitFor(() => expect(bot.api.sendMessage.mock.calls.some((call) => String(call[1]).includes('ENOTFOUND'))).toBe(true));
      expect(bot.api.sendMessage.mock.calls.map((call) => String(call[1])).join('\n')).not.toContain('secret-key');
      for (const native of ['Native parser rejected response schema: missing output_items token=secret-key',
        "You've hit your session limit · resets 11:50am (Asia/Shanghai)"]) {
        adapter.events.emit('turn-accepted', 'owner:pulse');
        adapter.events.emit('response', 'owner:pulse', {
          type: 'result', is_error: true, errors: [native], subtype: 'error_during_execution',
        });
        const expected = native.startsWith('Native') ? 'missing output_items' : '11:50am (Asia/Shanghai)';
        await vi.waitFor(() => expect(bot.api.sendMessage.mock.calls.some((call) => String(call[1]).includes(expected))).toBe(true));
        const errorsBefore = observedErrors;
        adapter.events.emit('error', 'owner:pulse', new Error(native));
        await vi.waitFor(() => expect(observedErrors).toBe(errorsBefore + 1));
        const delivered = bot.api.sendMessage.mock.calls.map((call) => String(call[1])).filter((text) => text.includes(expected));
        expect(delivered).toHaveLength(1);
        expect(delivered[0]).toContain('error_during_execution');
        expect(delivered[0]).not.toContain('secret-key');
      }
    } finally {
      runtime.dispose();
      rmSync(dir, { recursive: true, force: true });
    }
  });

  it('delivers a runtime-notification turn that arrives after the previous turn completed', async () => {
    const dir = mkdtempSync(join(tmpdir(), 'relay-tg-runtime-notification-'));
    const runtime = createConversationRuntimeManager();
    const adapter = createMockAdapter('cc');
    runtime.register(adapter);
    try {
      const configPath = join(dir, 'relay.json');
      writeFileSync(configPath, JSON.stringify({ users: [{
        userId: 'owner', chatId: '111222333', defaultAgent: 'pulse',
        agents: [{ id: 'pulse', name: 'Pulse', emoji: 'P', botToken: '123456:test-token',
          workspace: dir, sessionId: 'native-cc', runtime: 'cc' }],
      }] }));
      await createTelegramBots({ runtime, router: createRouter(configPath), stateManager: createMemoryStateManager() }).start();
      const bot = grammyMock.instances[0];
      await bot.handlers.get('message:text')?.({ chat: { id: 111222333 }, message: { text: 'start a background build' }, reply: vi.fn(async () => {}) });
      const delivered = (): string => [...bot.api.sendMessage.mock.calls, ...bot.api.editMessageText.mock.calls]
        .map((call) => String(call[call.length - 1] ?? '') + String(call[1] ?? '') + String(call[2] ?? '')).join('\n');
      adapter.events.emit('turn-accepted', 'owner:pulse');
      adapter.events.emit('response', 'owner:pulse', { type: 'assistant', message: { stop_reason: 'end_turn',
        content: [{ type: 'text', text: 'Build started in the background.' }] } });
      adapter.events.emit('response', 'owner:pulse', { type: 'result', result: '' });
      await vi.waitFor(() => expect(delivered()).toContain('Build started in the background.'));

      // CC wakes on the finished background task with no Relay send.
      adapter.events.emit('turn-accepted', 'owner:pulse', RUNTIME_NOTIFICATION_TURN);
      adapter.events.emit('response', 'owner:pulse', tagRuntimeNotificationTurn({ type: 'assistant', message: { stop_reason: 'end_turn',
        content: [{ type: 'text', text: 'Background build finished: 42 tests passed.' }] } }));
      adapter.events.emit('response', 'owner:pulse', tagRuntimeNotificationTurn({ type: 'result', result: '' }));
      await vi.waitFor(() => expect(delivered()).toContain('Background build finished: 42 tests passed.'));
    } finally {
      runtime.dispose();
      rmSync(dir, { recursive: true, force: true });
    }
  });

  it('renders a runtime-notification turn like a normal turn: folded card, reply once, notice leads the reply', async () => {
    const dir = mkdtempSync(join(tmpdir(), 'relay-tg-runtime-notification-card-'));
    vi.stubEnv('RELAY_WORK_PROCESS_DETAILS', 'on');
    const runtime = createConversationRuntimeManager();
    const adapter = createMockAdapter('cc');
    runtime.register(adapter);
    try {
      const configPath = join(dir, 'relay.json');
      writeFileSync(configPath, JSON.stringify({ users: [{
        userId: 'owner', chatId: '111222333', defaultAgent: 'pulse',
        agents: [{ id: 'pulse', name: 'Pulse', emoji: 'P', botToken: '123456:test-token',
          workspace: dir, sessionId: 'native-cc', runtime: 'cc' }],
      }] }));
      await createTelegramBots({ runtime, router: createRouter(configPath), stateManager: createMemoryStateManager() }).start();
      const bot = grammyMock.instances[0];
      const reply = 'Background build finished: 42 tests passed.';
      // CC stream-json: no stop_reason and no delivery_phase on the answer.
      adapter.events.emit('turn-accepted', 'owner:pulse', RUNTIME_NOTIFICATION_TURN);
      adapter.events.emit('response', 'owner:pulse', tagRuntimeNotificationTurn({ type: 'assistant', message: { stop_reason: null,
        content: [{ type: 'text', text: reply }] } }));
      adapter.events.emit('response', 'owner:pulse', tagRuntimeNotificationTurn({ type: 'result', result: '' }));
      const cards = (): string[] => [...bot.api.raw.sendRichMessage.mock.calls, ...bot.api.raw.editMessageText.mock.calls]
        .map((call) => JSON.stringify(call[0])).filter((text) => text.includes('wp:'));
      await vi.waitFor(() => expect(cards().at(-1)).toContain('本轮回复已生成'), { timeout: 5000 });
      const replies = bot.api.sendMessage.mock.calls.map((call) => String(call[1])).filter((text) => text.includes(reply));
      expect(replies).toEqual([`${RUNTIME_NOTIFICATION_REPLY_LEAD}\n\n${reply}`]);
      expect(cards().at(-1)).toContain('⌄ 展开过程');
      for (const card of cards()) expect(card).not.toContain(reply);

      // Expanding the finished card shows no copy of the delivered reply.
      const open = /wp:[a-f0-9]+:open/.exec(cards().at(-1)!)![0];
      await bot.handlers.get('callback_query:data')?.({ chat: { id: 111222333 }, callbackQuery: { data: open,
        message: { message_id: 901, chat: { id: 111222333 } } }, answerCallbackQuery: vi.fn(async () => {}) });
      await vi.waitFor(() => expect(cards().at(-1)).toContain('⌃ 收起过程'), { timeout: 5000 });
      expect(cards().at(-1)).not.toContain(reply);
    } finally {
      runtime.dispose();
      rmSync(dir, { recursive: true, force: true });
    }
  });

  it('uses scoped one-shot Telegram buttons to select a model without exposing the model id in callback data', async () => {
    const dir = mkdtempSync(join(tmpdir(), 'relay-tg-model-menu-'));
    try {
      const configPath = join(dir, 'relay.json');
      const catalogPath = join(dir, 'models.json');
      writeFileSync(configPath, JSON.stringify({
        users: [
          {
            userId: 'owner',
            chatId: '111222333',
            defaultAgent: 'pulse',
            agents: [
              {
                id: 'pulse',
                name: 'Pulse',
                emoji: 'P',
                botToken: '123456:fake-token-not-a-real-secret',
                workspace: '/tmp/pulse',
                sessionId: 'session-pulse',
                runtime: 'cc',
              },
            ],
          },
        ],
      }));
      writeFileSync(catalogPath, JSON.stringify({
        providers: {
          anthropic: {
            protocol: 'anthropic',
            base_url_env: 'ANTHROPIC_BASE_URL',
            api_key_pool: [],
          },
        },
        models: [
          {
            id: 'claude-opus-5-5-with-a-deliberately-long-reviewed-model-id',
            display_name: 'Claude Opus 5.5',
            provider: 'anthropic',
          },
        ],
      }));

      const { createModelCatalog } = await import('../src/model-catalog.js');
      const runtime = createConversationRuntimeManager();
      runtime.register(createMockAdapter('cc'));
      const router = createRouter(configPath);
      await createTelegramBots({
        runtime,
        router,
        stateManager: createMemoryStateManager(),
        modelCatalog: createModelCatalog(catalogPath),
      }).start();

      const bot = grammyMock.instances[0];
      const modelHandler = bot.commandHandlers.get('model');
      const callbackHandler = bot.handlers.get('callback_query:data');
      const reply = vi.fn(async () => {});
      const answerCallbackQuery = vi.fn(async () => {});

      await modelHandler?.({ chat: { id: 111222333 }, match: '', reply });
      const rootKeyboard = (reply.mock.calls.at(-1)?.[1] as { reply_markup?: { rows?: { text: string; callbackData: string }[][] } })?.reply_markup;
      const subscription = rootKeyboard?.rows?.flat().find(button => button.text === '订阅模型');
      expect(subscription?.callbackData).toMatch(/^rm:/);
      expect(subscription!.callbackData.length).toBeLessThanOrEqual(64);
      expect(subscription!.callbackData).not.toContain('subscription');

      await callbackHandler?.({
        chat: { id: 111222333 },
        callbackQuery: { data: subscription!.callbackData },
        answerCallbackQuery,
        reply,
      });
      const scopedKeyboard = (reply.mock.calls.at(-1)?.[1] as { reply_markup?: { rows?: { text: string; callbackData: string }[][] } })?.reply_markup;
      const model = scopedKeyboard?.rows?.flat().find(button => button.text.endsWith('Claude Opus 5.5'));
      expect(model?.text).toContain('Claude Opus 5.5');
      expect(model?.callbackData).toMatch(/^rm:/);
      expect(model!.callbackData.length).toBeLessThanOrEqual(64);
      expect(model!.callbackData).not.toContain('claude-opus');
      router.setAgentModel('owner', 'pulse', undefined);

      await callbackHandler?.({
        chat: { id: 999888777 },
        callbackQuery: { data: model!.callbackData },
        answerCallbackQuery,
        reply,
      });
      expect(router.getAgentModel('owner', 'pulse')).toBeUndefined();

      await callbackHandler?.({
        chat: { id: 111222333 },
        callbackQuery: { data: model!.callbackData },
        answerCallbackQuery,
        reply,
      });
      expect(router.getAgentModel('owner', 'pulse')).toBe('claude-opus-5-5-with-a-deliberately-long-reviewed-model-id');

      const callsAfterSelection = reply.mock.calls.length;
      await callbackHandler?.({
        chat: { id: 111222333 },
        callbackQuery: { data: model!.callbackData },
        answerCallbackQuery,
        reply,
      });
      expect(reply).toHaveBeenCalledTimes(callsAfterSelection);
      expect(answerCallbackQuery).toHaveBeenLastCalledWith(expect.objectContaining({ text: expect.stringContaining('过期') }));

      await modelHandler?.({ chat: { id: 111222333 }, match: '', reply });
      const expiringKeyboard = (reply.mock.calls.at(-1)?.[1] as { reply_markup?: { rows?: { text: string; callbackData: string }[][] } })?.reply_markup;
      const expiringToken = expiringKeyboard?.rows?.flat()[0]?.callbackData;
      const realNow = Date.now();
      const nowSpy = vi.spyOn(Date, 'now').mockReturnValue(realNow + 11 * 60 * 1000);
      await callbackHandler?.({
        chat: { id: 111222333 },
        callbackQuery: { data: expiringToken },
        answerCallbackQuery,
        reply,
      });
      expect(answerCallbackQuery).toHaveBeenLastCalledWith(expect.objectContaining({ text: expect.stringContaining('过期') }));
      nowSpy.mockRestore();

      await modelHandler?.({ chat: { id: 111222333 }, match: '', reply });
      const boundedKeyboard = (reply.mock.calls.at(-1)?.[1] as { reply_markup?: { rows?: { text: string; callbackData: string }[][] } })?.reply_markup;
      const evictedToken = boundedKeyboard?.rows?.flat()[0]?.callbackData;
      for (let i = 0; i < 129; i++) {
        await modelHandler?.({ chat: { id: 111222333 }, match: '', reply });
      }
      await callbackHandler?.({
        chat: { id: 111222333 },
        callbackQuery: { data: evictedToken },
        answerCallbackQuery,
        reply,
      });
      expect(answerCallbackQuery).toHaveBeenLastCalledWith(expect.objectContaining({ text: expect.stringContaining('过期') }));
    } finally {
      vi.restoreAllMocks();
      rmSync(dir, { recursive: true, force: true });
    }
  });

  it('/stop uses the runtime-native abort path for a busy Codex turn', async () => {
    const dir = mkdtempSync(join(tmpdir(), 'relay-tg-codex-stop-'));
    try {
      const configPath = join(dir, 'relay.json');
      writeFileSync(configPath, JSON.stringify({
        users: [
          {
            userId: 'owner',
            chatId: '111222333',
            defaultAgent: 'aki',
            agents: [{
              id: 'aki',
              name: 'Aki',
              emoji: 'A',
              botToken: '123456:fake-token-not-a-real-secret',
              workspace: '/tmp/aki',
              sessionId: 'session-aki',
              runtime: 'codex',
            }],
          },
        ],
      }));

      const codex = createMockAdapter('codex');
      const abortTurn = vi.fn(async () => undefined);
      codex.abortTurn = abortTurn;
      const runtime = createConversationRuntimeManager();
      runtime.register(codex);
      await runtime.spawn({
        tenantId: 'owner:aki',
        runtime: 'codex',
        workspace: '/tmp/aki',
        sessionId: 'session-aki',
      });
      const active = codex.get('owner:aki');
      if (!active) throw new Error('expected mock Codex process');
      active.state = 'busy';

      const router = createRouter(configPath);
      await createTelegramBots({ runtime, router, stateManager: createMemoryStateManager() }).start();
      const stopHandler = grammyMock.instances[0].commandHandlers.get('stop');
      const reply = vi.fn(async () => {});

      await stopHandler?.({ chat: { id: 111222333 }, reply });

      expect(abortTurn).toHaveBeenCalledWith('owner:aki');
      expect(reply).toHaveBeenCalledWith(expect.stringContaining('已中断'));
    } finally {
      rmSync(dir, { recursive: true, force: true });
    }
  });

  it('routes text to the physical Telegram bot even when the default agent is main', async () => {
    const dir = mkdtempSync(join(tmpdir(), 'relay-tg-physical-bot-'));
    try {
      const configPath = join(dir, 'relay.json');
      writeFileSync(configPath, JSON.stringify({
        users: [
          {
            userId: 'owner',
            chatId: '111222333',
            defaultAgent: 'main',
            agents: [
              {
                id: 'main',
                name: 'Duke',
                emoji: 'D',
                botToken: '123456:main-token-not-real',
                workspace: '/tmp/duke',
                sessionId: 'session-main',
                runtime: 'codex',
              },
              {
                id: 'pulse',
                name: 'Pulse',
                emoji: 'P',
                botToken: '123456:pulse-token-not-real',
                workspace: '/tmp/pulse',
                sessionId: 'session-pulse',
                runtime: 'codex',
                conversationRuntimePreference: ['codex', 'cc'],
                codexYoloDefault: true,
              },
            ],
          },
        ],
      }));

      const codex = createMockAdapter('codex');
      const cc = createMockAdapter('cc');
      const runtime = createConversationRuntimeManager();
      runtime.register(codex);
      runtime.register(cc);
      const stateManager = createMemoryStateManager();
      const router = createRouter(configPath);
      await createTelegramBots({ runtime, router, stateManager }).start();
      const pulseBot = grammyMock.instances.find(bot => bot.token === '123456:pulse-token-not-real');
      expect(pulseBot).toBeDefined();
      const textHandler = pulseBot?.handlers.get('message:text');

      await textHandler?.({
        chat: { id: 111222333 },
        message: { text: 'Pulse should handle this' },
        reply: vi.fn(async () => {}),
      });

      expect(codex.spawn).toHaveBeenCalledWith(expect.objectContaining({
        tenantId: 'owner:pulse',
        workspace: '/tmp/pulse',
        sessionId: 'session-pulse',
        runtime: 'codex',
        conversationRuntimePreference: ['codex', 'cc'],
        codexYoloDefault: true,
      }));
      expect(codex.spawn).not.toHaveBeenCalledWith(expect.objectContaining({
        tenantId: 'owner:main',
      }));
      expect(codex.send).toHaveBeenCalledWith('owner:pulse', { type: 'human', content: 'Pulse should handle this' } satisfies StreamJsonMessage, anyTurnContext());
      expect(stateManager.getSession('owner:pulse')?.agentId).toBe('pulse');
      expect(stateManager.getSession('owner:main')).toBeUndefined();
    } finally {
      rmSync(dir, { recursive: true, force: true });
    }
  });

  it('uses RoutePolicyResult runtimePreference when handling a text message', async () => {
    const handlerAgent: AgentConfig = {
      id: 'aki',
      name: 'Aki',
      emoji: 'A',
      botToken: '123456:fake-token-not-a-real-secret',
      workspace: '/tmp/aki',
      sessionId: 'session-aki',
      runtime: 'cc',
    };
    const routeAgent: AgentConfig = {
      ...handlerAgent,
      runtime: 'codex',
      conversationRuntimePreference: ['codex', 'cc'],
    };
    const routeUser: UserConfig = {
      userId: 'owner',
      chatId: '111222333',
      defaultAgent: 'aki',
      agents: [routeAgent],
    };
    const router = {
      resolveUser: vi.fn(),
      getActiveAgent: vi.fn(),
      switchAgent: vi.fn(),
      poolKey: (userId: string, agentId: string) => `${userId}:${agentId}`,
      getAgents: vi.fn().mockReturnValue([routeAgent]),
      getAllUsers: vi.fn().mockReturnValue([routeUser]),
      resolveByBotAgent: vi.fn().mockReturnValue({ user: routeUser, agent: routeAgent }),
      resolveByFeishuAgent: vi.fn(),
      resetSession: vi.fn(),
      overrideSessionId: vi.fn(),
      setAgentRuntime: vi.fn(),
      getAllAgentBots: vi.fn().mockReturnValue([{ agent: handlerAgent, authorizedChatIds: ['111222333'] }]),
      getAllFeishuBots: vi.fn().mockReturnValue([]),
      getAgentChannel: vi.fn().mockReturnValue('telegram' as const),
      indexFeishuOpenId: vi.fn(),
    } as unknown as Router;

    const codex = createMockAdapter('codex');
    const cc = createMockAdapter('cc');
    const runtime = createConversationRuntimeManager();
    runtime.register(codex);
    runtime.register(cc);
    const stateManager = createMemoryStateManager();
    await createTelegramBots({ runtime, router, stateManager }).start();
    const bot = grammyMock.instances[0];
    const textHandler = bot.handlers.get('message:text');

    await textHandler?.({
      chat: { id: 111222333 },
      message: { text: 'route me' },
      reply: vi.fn(async () => {}),
    });

    expect(codex.spawn).toHaveBeenCalledWith(expect.objectContaining({
      tenantId: 'owner:aki',
      runtime: 'codex',
      conversationRuntimePreference: ['codex', 'cc'],
    }));
    expect(codex.send).toHaveBeenCalledWith('owner:aki', { type: 'human', content: 'route me' } satisfies StreamJsonMessage, anyTurnContext());
  });

  it('cold-starts a persisted native Pi WorkItem session through the gateway route', async () => {
    const dir = mkdtempSync(join(tmpdir(), 'relay-tg-pi-cold-start-'));
    try {
      const configPath = join(dir, 'relay.json');
      writeFileSync(configPath, JSON.stringify({
        users: [{
          userId: 'owner',
          chatId: '111222333',
          defaultAgent: 'tcloud-pulse',
          agents: [{
            id: 'tcloud-pulse',
            name: 'TCloud Pulse',
            emoji: 'P',
            botToken: '123456:pi-token-not-real',
            workspace: '/tmp/tcloud-pulse',
            sessionId: 'session-pi',
            runtime: 'pi',
            conversationRuntimePreference: ['pi'],
          }],
        }],
      }));

      const pi = createMockAdapter('pi');
      const runtime = createConversationRuntimeManager();
      runtime.register(pi);
      const stateManager = createMemoryStateManager();
      const sessionRef = runtimeAuthorityRef('session-pi');
      const usageBinding = buildRuntimeUsageBinding({
        workItemId: 'wi_20990101_003',
        agentId: 'pulse',
        phase: 'work_item',
        epoch: 1,
        runtime: 'pi',
        procurementSource: 'chatgpt_subscription',
        authorityRef: sessionRef,
        issuedAt: '2026-09-05T08:00:00Z',
      });
      stateManager.updateSession('owner:tcloud-pulse', {
        poolKey: 'owner:tcloud-pulse',
        agentId: 'tcloud-pulse',
        runtime: 'pi',
        sessionId: 'session-pi',
        turnCount: 1,
        maxTurns: 1000,
        lastActivity: Date.now(),
        status: 'active',
        activeWorkItemRuntimeBinding: {
          schema: 'pulse_active_work_item_runtime_binding/v1',
          workItemId: 'wi_20990101_003',
          sessionId: 'session-pi',
          sessionRef,
          runtime: 'pi',
          workspace: '/registered/tcloud-pulse',
          usageBinding,
        },
      });
      const router = createRouter(configPath);
      await createTelegramBots({ runtime, router, stateManager }).start();
      const bot = grammyMock.instances[0];
      const textHandler = bot.handlers.get('message:text');
      const reply = vi.fn(async () => {});

      await textHandler?.({
        chat: { id: 111222333 },
        message: { text: 'resume Pi after Relay restart' },
        reply,
      });

      expect(pi.spawn).toHaveBeenCalledWith(expect.objectContaining({
        tenantId: 'owner:tcloud-pulse',
        runtime: 'pi',
        conversationRuntimePreference: ['pi'],
        workspace: '/registered/tcloud-pulse',
        sessionId: 'session-pi',
        runtimeSessionBinding: { runtime: 'pi', sessionId: 'session-pi' },
      }));
      expect(pi.spawn.mock.calls[0]![0]).not.toHaveProperty('computeCostBinding');
      expect(stateManager.getSession('owner:tcloud-pulse')?.activeWorkItemRuntimeBinding?.usageBinding).toEqual(usageBinding);
      expect(pi.send).toHaveBeenCalledWith(
        'owner:tcloud-pulse',
        { type: 'human', content: 'resume Pi after Relay restart' } satisfies StreamJsonMessage,
        anyTurnContext(),
      );
      expect(reply).not.toHaveBeenCalledWith('⚠️ 服务暂时不可用，请稍后重试。');
    } finally {
      rmSync(dir, { recursive: true, force: true });
    }
  });

  it('includes replied Telegram text in the runtime prompt', async () => {
    const dir = mkdtempSync(join(tmpdir(), 'relay-tg-reply-'));
    try {
      const configPath = join(dir, 'relay.json');
      writeFileSync(configPath, JSON.stringify({
        users: [
          {
            userId: 'owner',
            chatId: '111222333',
            defaultAgent: 'aki',
            agents: [
              {
                id: 'aki',
                name: 'Aki',
                emoji: 'A',
                botToken: '123456:fake-token-not-a-real-secret',
                workspace: '/tmp/aki',
                sessionId: 'session-aki',
                runtime: 'codex',
              },
            ],
          },
        ],
      }));

      const codex = createMockAdapter('codex');
      const runtime = createConversationRuntimeManager();
      runtime.register(codex);
      const stateManager = createMemoryStateManager();
      const router = createRouter(configPath);
      await createTelegramBots({ runtime, router, stateManager }).start();
      const bot = grammyMock.instances[0];
      const textHandler = bot.handlers.get('message:text');

      await textHandler?.({
        chat: { id: 111222333 },
        message: {
          text: '看一下这是什么消息',
          reply_to_message: { text: 'Amanda 发来的 WI 摘要' },
        },
        reply: vi.fn(async () => {}),
      });

      expect(codex.send).toHaveBeenCalledWith('owner:aki', {
        type: 'human',
        content: expect.stringContaining('Amanda 发来的 WI 摘要'),
      }, anyTurnContext());
      expect(codex.send).toHaveBeenCalledWith('owner:aki', {
        type: 'human',
        content: expect.stringContaining('看一下这是什么消息'),
      }, anyTurnContext());
    } finally {
      rmSync(dir, { recursive: true, force: true });
    }
  });

  it('sends one fallback notice and persists the final runtime after spawn fallback', async () => {
    const dir = mkdtempSync(join(tmpdir(), 'relay-tg-fallback-'));
    try {
      const configPath = join(dir, 'relay.json');
      writeFileSync(configPath, JSON.stringify({
        users: [
          {
            userId: 'owner',
            chatId: '111222333',
            defaultAgent: 'aki',
            agents: [
              {
                id: 'aki',
                name: 'Aki',
                emoji: 'A',
                botToken: '123456:fake-token-not-a-real-secret',
                workspace: '/tmp/aki',
                sessionId: 'session-aki',
                runtime: 'codex',
                conversationRuntimePreference: ['codex', 'cc'],
              },
            ],
          },
        ],
      }));

      const codex = createMockAdapter('codex');
      const cc = createMockAdapter('cc');
      vi.mocked(codex.spawn).mockRejectedValueOnce(new Error('spawn failed CODEX_AUTH_TOKEN=secret-value'));
      const runtime = createConversationRuntimeManager();
      runtime.register(codex);
      runtime.register(cc);
      const stateManager = createMemoryStateManager();
      const router = createRouter(configPath);
      await createTelegramBots({ runtime, router, stateManager }).start();
      const bot = grammyMock.instances[0];
      const textHandler = bot.handlers.get('message:text');

      await textHandler?.({
        chat: { id: 111222333 },
        message: { text: 'hello' },
        reply: vi.fn(async () => {}),
      });

      const fallbackNotices = bot.api.sendMessage.mock.calls
        .map((call) => String(call[1]))
        .filter((text) => text.includes('主推理通道暂时受限'));
      expect(fallbackNotices).toHaveLength(1);
      expect(fallbackNotices[0]).toContain('codex → cc');
      expect(fallbackNotices[0]).toContain('无需重发');
      expect(fallbackNotices[0]).not.toContain('secret-value');
      expect(fallbackNotices[0]).not.toMatch(/token|auth|key|secret/i);
      expect(cc.send).toHaveBeenCalledWith('owner:aki', { type: 'human', content: 'hello' } satisfies StreamJsonMessage, anyTurnContext());
      expect(stateManager.getSession('owner:aki')?.runtime).toBe('cc');
      expect(stateManager.getSession('owner:aki')?.codexThreadId).toBeUndefined();
    } finally {
      rmSync(dir, { recursive: true, force: true });
    }
  });
});

describe('TG runtime lifecycle notifications', () => {
  it('does NOT notify TG sessions on Relay resume when wasBusyAtShutdown is unset (idle agents stay quiet)', async () => {
    const dir = mkdtempSync(join(tmpdir(), 'relay-tg-resume-notice-'));
    try {
      const configPath = join(dir, 'relay.json');
      writeFileSync(configPath, JSON.stringify({
        users: [
          {
            userId: 'owner',
            chatId: '111222333',
            defaultAgent: 'aki',
            agents: [
              {
                id: 'aki',
                name: 'Aki',
                emoji: 'A',
                botToken: '123456:fake-token-not-a-real-secret',
                workspace: '/tmp/aki',
                sessionId: 'session-aki',
                runtime: 'codex',
              },
            ],
          },
        ],
      }));

      const codex = createMockAdapter('codex');
      const runtime = createConversationRuntimeManager();
      runtime.register(codex);
      const stateManager = createMemoryStateManager();
      stateManager.updateSession('owner:aki', {
        poolKey: 'owner:aki',
        agentId: 'aki',
        runtime: 'codex',
        sessionId: 'session-aki',
        turnCount: 1,
        maxTurns: 100,
        lastActivity: Date.now(),
        status: 'active',
      });
      const router = createRouter(configPath);
      const bots = createTelegramBots({ runtime, router, stateManager });
      await bots.start();
      const bot = grammyMock.instances[0];

      await bots.notifyResumed();

      // wasBusyAtShutdown unset → no notification, no spawn (idle agents stay quiet on restart).
      expect(bot.api.sendMessage).not.toHaveBeenCalled();
      expect(codex.spawn).not.toHaveBeenCalled();
      expect(codex.send).not.toHaveBeenCalled();
    } finally {
      rmSync(dir, { recursive: true, force: true });
    }
  });

  it('injects an interrupted TG message into the runtime after Relay resumes', async () => {
    const dir = mkdtempSync(join(tmpdir(), 'relay-tg-resume-interrupted-'));
    try {
      const configPath = join(dir, 'relay.json');
      writeFileSync(configPath, JSON.stringify({
        users: [
          {
            userId: 'owner',
            chatId: '111222333',
            defaultAgent: 'aki',
            agents: [
              {
                id: 'aki',
                name: 'Aki',
                emoji: 'A',
                botToken: '123456:fake-token-not-a-real-secret',
                workspace: '/tmp/aki',
                sessionId: 'session-aki',
                runtime: 'codex',
                conversationRuntimePreference: ['codex', 'cc'],
              },
            ],
          },
        ],
      }));

      const codex = createMockAdapter('codex');
      const cc = createMockAdapter('cc');
      const runtime = createConversationRuntimeManager();
      runtime.register(codex);
      runtime.register(cc);
      const stateManager = createMemoryStateManager();
      stateManager.updateSession('owner:aki', {
        poolKey: 'owner:aki',
        agentId: 'aki',
        runtime: 'codex',
        sessionId: 'session-aki',
        codexThreadId: 'thread-aki',
        turnCount: 1,
        maxTurns: 100,
        lastActivity: Date.now(),
        status: 'active',
        wasBusyAtShutdown: true,
        interruptedUserMessage: 'finish the previous work',
      });
      const router = createRouter(configPath);
      const bots = createTelegramBots({ runtime, router, stateManager });
      await bots.start();
      const bot = grammyMock.instances[0];

      await bots.notifyResumed();

      expect(bot.api.sendMessage).toHaveBeenCalledWith(111222333, '⚡ Relay 已重启恢复，正在恢复刚才中断的请求。');
      expect(codex.spawn).toHaveBeenCalledWith(expect.objectContaining({
        tenantId: 'owner:aki',
        runtime: 'codex',
        conversationRuntimePreference: ['codex', 'cc'],
        codexThreadId: 'thread-aki',
        resume: true,
      }));
      expect(codex.send).toHaveBeenCalledWith('owner:aki', {
        type: 'human',
        content: expect.stringContaining('finish the previous work'),
      }, anyTurnContext());
      expect(stateManager.getSession('owner:aki')?.interruptedUserMessage).toBeUndefined();
      expect(stateManager.getSession('owner:aki')?.wasBusyAtShutdown).toBe(false);
    } finally {
      rmSync(dir, { recursive: true, force: true });
    }
  });

  it('wakes busy sessions even when interruptedUserMessage was not captured', async () => {
    // Regression: CC long-thinking / 429 internal retry clears lastInFlight
    // before SIGTERM. wasBusyAtShutdown alone must be enough to wake the user.
    const dir = mkdtempSync(join(tmpdir(), 'relay-tg-resume-busy-only-'));
    try {
      const configPath = join(dir, 'relay.json');
      writeFileSync(configPath, JSON.stringify({
        users: [
          {
            userId: 'owner',
            chatId: '111222333',
            defaultAgent: 'aki',
            agents: [
              {
                id: 'aki',
                name: 'Aki',
                emoji: 'A',
                botToken: '123456:fake-token-not-a-real-secret',
                workspace: '/tmp/aki',
                sessionId: 'session-aki',
                runtime: 'codex',
                conversationRuntimePreference: ['codex', 'cc'],
              },
            ],
          },
        ],
      }));

      const codex = createMockAdapter('codex');
      const cc = createMockAdapter('cc');
      const runtime = createConversationRuntimeManager();
      runtime.register(codex);
      runtime.register(cc);
      const stateManager = createMemoryStateManager();
      stateManager.updateSession('owner:aki', {
        poolKey: 'owner:aki',
        agentId: 'aki',
        runtime: 'codex',
        sessionId: 'session-aki',
        codexThreadId: 'thread-aki',
        turnCount: 1,
        maxTurns: 100,
        lastActivity: Date.now(),
        status: 'active',
        wasBusyAtShutdown: true,
        // No interruptedUserMessage — CC was mid-thinking when SIGTERM hit.
      });
      const router = createRouter(configPath);
      const bots = createTelegramBots({ runtime, router, stateManager });
      await bots.start();
      const bot = grammyMock.instances[0];

      await bots.notifyResumed();

      expect(bot.api.sendMessage).toHaveBeenCalledWith(111222333, expect.stringContaining('Relay 已重启恢复'));
      expect(codex.spawn).toHaveBeenCalledWith(expect.objectContaining({
        tenantId: 'owner:aki',
        runtime: 'codex',
        codexThreadId: 'thread-aki',
        resume: true,
      }));
      // Resume prompt is the generic busy variant — no interrupted text to inject.
      expect(codex.send).toHaveBeenCalledWith('owner:aki', expect.objectContaining({
        type: 'human',
      }), anyTurnContext());
      expect(stateManager.getSession('owner:aki')?.wasBusyAtShutdown).toBe(false);
    } finally {
      rmSync(dir, { recursive: true, force: true });
    }
  });

  it.each([
    ['resumes a CC turn interrupted after more than 10 minutes without activity', Date.now(), true],
    ['still skips an old busy flag without an interruption time', undefined, false],
  ])('%s', async (_name, interruptedAt, resumed) => {
    const dir = mkdtempSync(join(tmpdir(), 'relay-tg-resume-long-turn-'));
    try {
      const configPath = join(dir, 'relay.json');
      writeFileSync(configPath, JSON.stringify({ users: [{
        userId: 'owner', chatId: '111222333', defaultAgent: 'aki',
        agents: [{ id: 'aki', name: 'Aki', emoji: 'A', botToken: '123456:fake-token-not-a-real-secret',
          workspace: '/tmp/aki', sessionId: 'session-aki', runtime: 'cc' }],
      }] }));
      const cc = createMockAdapter('cc');
      const runtime = createConversationRuntimeManager();
      runtime.register(cc);
      const stateManager = createMemoryStateManager();
      stateManager.updateSession('owner:aki', {
        poolKey: 'owner:aki', agentId: 'aki', runtime: 'cc', sessionId: 'session-aki',
        turnCount: 1, maxTurns: 100, status: 'active',
        // The turn started 25 minutes ago and recorded no activity since.
        lastActivity: Date.now() - 25 * 60 * 1000,
        wasBusyAtShutdown: true,
        ...(interruptedAt ? { shutdownInterruptedAt: interruptedAt } : {}),
      });
      const bots = createTelegramBots({ runtime, router: createRouter(configPath), stateManager });
      await bots.start();
      await bots.notifyResumed();
      if (resumed) {
        expect(cc.send).toHaveBeenCalledWith('owner:aki', expect.objectContaining({ type: 'human' }), anyTurnContext());
        expect(stateManager.getSession('owner:aki')).toMatchObject({ wasBusyAtShutdown: false });
        expect(stateManager.getSession('owner:aki')?.shutdownInterruptedAt).toBeUndefined();
      } else {
        expect(cc.send).not.toHaveBeenCalled();
      }
    } finally {
      rmSync(dir, { recursive: true, force: true });
    }
  });

  it.each([
    ['a text message', 'B: also check the logs'],
    ['an image-only message', [{ type: 'image', source: { type: 'base64', media_type: 'image/png', data: 'iVBORw0KGgo=' } }]],
  ])('busy Codex turn A plus %s B queued during maintenance: after restart A resumes, then B', async (_name, content) => {
    const dir = mkdtempSync(join(tmpdir(), 'relay-tg-resume-queued-'));
    try {
      const configPath = join(dir, 'relay.json');
      writeFileSync(configPath, JSON.stringify({ users: [{
        userId: 'owner', chatId: '111222333', defaultAgent: 'aki',
        agents: [{ id: 'aki', name: 'Aki', emoji: 'A', botToken: '123456:fake-token-not-a-real-secret',
          workspace: '/tmp/aki', sessionId: 'session-aki', runtime: 'codex' }],
      }] }));
      const stateManager = createMemoryStateManager();
      stateManager.updateSession('owner:aki', {
        poolKey: 'owner:aki', agentId: 'aki', runtime: 'codex', sessionId: 'session-aki',
        codexThreadId: 'thread-aki', turnCount: 1, maxTurns: 100, lastActivity: Date.now(), status: 'active',
      });
      // Shutdown of the old Relay: B waits at the held barrier, intake closes,
      // then the Codex shutdown callback records its in-flight turn A.
      const barrier = createMaintenanceBarrier();
      barrier.hold(60_000);
      const message = { type: 'human', content };
      void barrier.admit('owner:aki', 'codex', message);
      barrier.close(turn => { persistQueuedTurns(stateManager, [turn], Date.now()); stateManager.flush(); });
      stateManager.updateSession('owner:aki', { runtime: 'codex', codexThreadId: 'thread-aki',
        wasBusyAtShutdown: true, interruptedUserMessage: 'A: fix the deploy', lastActivity: Date.now(), status: 'active' });

      // The new Relay resumes.
      const codex = createMockAdapter('codex');
      const runtime = createConversationRuntimeManager();
      runtime.register(codex);
      const bots = createTelegramBots({ runtime, router: createRouter(configPath), stateManager });
      await bots.start();
      await bots.notifyResumed();

      const sent = vi.mocked(codex.send).mock.calls.map(call => call[1]);
      expect(sent).toHaveLength(2);
      expect(String((sent[0] as { content: unknown }).content)).toContain('A: fix the deploy');
      expect(sent[1]).toEqual(message);
      expect(stateManager.getSession('owner:aki')).toMatchObject({ wasBusyAtShutdown: false, pendingMaintenanceTurns: [] });
    } finally {
      rmSync(dir, { recursive: true, force: true });
    }
  });

  it('does not send generic crash notice after a timeout session-end', async () => {
    const dir = mkdtempSync(join(tmpdir(), 'relay-tg-timeout-'));
    try {
      const configPath = join(dir, 'relay.json');
      writeFileSync(configPath, JSON.stringify({
        users: [
          {
            userId: 'owner',
            chatId: '111222333',
            defaultAgent: 'aki',
            agents: [
              {
                id: 'aki',
                name: 'Aki',
                emoji: 'A',
                botToken: '123456:fake-token-not-a-real-secret',
                workspace: '/tmp/aki',
                sessionId: 'session-aki',
                runtime: 'codex',
              },
            ],
          },
        ],
      }));

      const codex = createMockAdapter('codex');
      const runtime = createConversationRuntimeManager();
      runtime.register(codex);
      const stateManager = createMemoryStateManager();
      const router = createRouter(configPath);
      await createTelegramBots({ runtime, router, stateManager }).start();
      const bot = grammyMock.instances[0];
      const textHandler = bot.handlers.get('message:text');

      await textHandler?.({
        chat: { id: 111222333 },
        message: { text: 'hello' },
        reply: vi.fn(async () => {}),
      });
      codex.events.emit('session-end', 'owner:aki', { type: 'timeout', timeoutMs: 600_000 });
      codex.events.emit('exit', 'owner:aki', 1);

      const messages = bot.api.sendMessage.mock.calls.map((call) => String(call[1]));
      expect(messages.filter((text) => text.includes('当前请求超时'))).toHaveLength(1);
      expect(messages).not.toContain('⚠️ 系统开小差了，正在恢复中...请稍后重试。');
    } finally {
      rmSync(dir, { recursive: true, force: true });
    }
  });
});

describe('TG /runtime command', () => {
  // 2026-06-18: commit 669e64b 改 /runtime 无参为交互选择卡（不再显示 configured/active）。
  it('returns the runtime selector card when called without args', async () => {
    const dir = mkdtempSync(join(tmpdir(), 'relay-tg-runtime-show-'));
    try {
      const configPath = join(dir, 'relay.json');
      writeFileSync(configPath, JSON.stringify({
        users: [
          {
            userId: 'owner',
            chatId: '111222333',
            defaultAgent: 'aki',
            agents: [
              {
                id: 'aki',
                name: 'Aki',
                emoji: 'A',
                botToken: '123456:fake-token-not-a-real-secret',
                workspace: '/tmp/aki',
                sessionId: 'session-aki',
                runtime: 'codex',
              },
            ],
          },
        ],
      }));

      const codex = createMockAdapter('codex');
      const runtime = createConversationRuntimeManager();
      runtime.register(codex);
      const stateManager = createMemoryStateManager();
      stateManager.updateSession('owner:aki', {
        poolKey: 'owner:aki',
        agentId: 'aki',
        runtime: 'codex',
        sessionId: 'session-aki',
        codexThreadId: 'thread-123',
        turnCount: 1,
        maxTurns: 100,
        lastActivity: Date.now(),
        status: 'active',
      });
      const router = createRouter(configPath);
      await createTelegramBots({ runtime, router, stateManager }).start();
      const bot = grammyMock.instances[0];
      const runtimeHandler = bot.commandHandlers.get('runtime');
      const reply = vi.fn(async () => {});

      await runtimeHandler?.({
        chat: { id: 111222333 },
        match: '',
        reply,
      });

      expect(reply).toHaveBeenCalledWith(expect.stringContaining('Runtime 选择'));
    } finally {
      rmSync(dir, { recursive: true, force: true });
    }
  });

  it('persists a valid runtime and clears the old active session', async () => {
    const dir = mkdtempSync(join(tmpdir(), 'relay-tg-runtime-set-'));
    try {
      const configPath = join(dir, 'relay.json');
      writeFileSync(configPath, JSON.stringify({
        users: [
          {
            userId: 'owner',
            chatId: '111222333',
            defaultAgent: 'aki',
            agents: [
              {
                id: 'aki',
                name: 'Aki',
                emoji: 'A',
                botToken: '123456:fake-token-not-a-real-secret',
                workspace: '/tmp/aki',
                sessionId: 'session-aki',
                runtime: 'codex',
              },
            ],
          },
        ],
      }));

      const codex = createMockAdapter('codex');
      const cc = createMockAdapter('cc');
      const runtime = createConversationRuntimeManager();
      runtime.register(codex);
      runtime.register(cc);
      const stateManager = createMemoryStateManager();
      const router = createRouter(configPath);
      await createTelegramBots({ runtime, router, stateManager }).start();
      const bot = grammyMock.instances[0];
      const textHandler = bot.handlers.get('message:text');
      const runtimeHandler = bot.commandHandlers.get('runtime');
      const reply = vi.fn(async () => {});

      await textHandler?.({
        chat: { id: 111222333 },
        message: { text: 'hello' },
        reply,
      });
      expect(stateManager.getSession('owner:aki')?.runtime).toBe('codex');

      await runtimeHandler?.({
        chat: { id: 111222333 },
        match: 'cc',
        reply,
      });

      expect(codex.kill).toHaveBeenCalledWith('owner:aki');
      expect(stateManager.getSession('owner:aki')).toBeUndefined();
      expect(JSON.parse(readFileSync(configPath, 'utf-8')).users[0].agents[0].runtime).toBe('cc');
      expect(JSON.parse(readFileSync(configPath, 'utf-8')).users[0].agents[0].conversationRuntimePreference).toEqual(['cc']);
      expect(reply).toHaveBeenCalledWith(expect.stringContaining('下一条消息会使用新的 runtime'));

      await textHandler?.({
        chat: { id: 111222333 },
        message: { text: 'after switch' },
        reply,
      });

      expect(cc.send).toHaveBeenCalledWith('owner:aki', { type: 'human', content: 'after switch' } satisfies StreamJsonMessage, anyTurnContext());
      expect(stateManager.getSession('owner:aki')?.runtime).toBe('cc');
      expect(stateManager.getSession('owner:aki')?.codexThreadId).toBeUndefined();
    } finally {
      rmSync(dir, { recursive: true, force: true });
    }
  });

  it('rejects invalid runtime values', async () => {
    const dir = mkdtempSync(join(tmpdir(), 'relay-tg-runtime-invalid-'));
    try {
      const configPath = join(dir, 'relay.json');
      writeFileSync(configPath, JSON.stringify({
        users: [
          {
            userId: 'owner',
            chatId: '111222333',
            defaultAgent: 'aki',
            agents: [
              {
                id: 'aki',
                name: 'Aki',
                emoji: 'A',
                botToken: '123456:fake-token-not-a-real-secret',
                workspace: '/tmp/aki',
                sessionId: 'session-aki',
                runtime: 'codex',
              },
            ],
          },
        ],
      }));

      const runtime = createConversationRuntimeManager();
      runtime.register(createMockAdapter('codex'));
      const stateManager = createMemoryStateManager();
      const router = createRouter(configPath);
      await createTelegramBots({ runtime, router, stateManager }).start();
      const bot = grammyMock.instances[0];
      const runtimeHandler = bot.commandHandlers.get('runtime');
      const reply = vi.fn(async () => {});

      await runtimeHandler?.({
        chat: { id: 111222333 },
        match: 'python',
        reply,
      });

      expect(reply).toHaveBeenCalledWith(expect.stringContaining('不支持的 runtime'));
      expect(JSON.parse(readFileSync(configPath, 'utf-8')).users[0].agents[0].runtime).toBe('codex');
    } finally {
      rmSync(dir, { recursive: true, force: true });
    }
  });
});

describe('TG /yolo command', () => {
  it('toggles Codex YOLO mode for the current bot session', async () => {
    const dir = mkdtempSync(join(tmpdir(), 'relay-tg-yolo-'));
    try {
      const configPath = join(dir, 'relay.json');
      writeFileSync(configPath, JSON.stringify({
        users: [
          {
            userId: 'owner',
            chatId: '111222333',
            defaultAgent: 'aki',
            agents: [
              {
                id: 'aki',
                name: 'Aki',
                emoji: 'A',
                botToken: '123456:fake-token-not-a-real-secret',
                workspace: '/tmp/aki',
                sessionId: 'session-aki',
                runtime: 'codex',
              },
            ],
          },
        ],
      }));

      const runtime = createConversationRuntimeManager();
      runtime.register(createMockAdapter('codex'));
      const stateManager = createMemoryStateManager();
      const router = createRouter(configPath);
      await createTelegramBots({ runtime, router, stateManager }).start();
      const bot = grammyMock.instances[0];
      const yoloHandler = bot.commandHandlers.get('yolo');
      const reply = vi.fn(async () => {});

      await yoloHandler?.({
        chat: { id: 111222333 },
        match: 'on',
        reply,
      });

      expect(reply).toHaveBeenCalledWith(expect.stringContaining('YOLO 已开启'));
      expect(runtime.getYoloMode?.('owner:aki')).toBe(true);

      await yoloHandler?.({
        chat: { id: 111222333 },
        match: 'off',
        reply,
      });

      expect(reply).toHaveBeenCalledWith(expect.stringContaining('YOLO 已关闭'));
      expect(runtime.getYoloMode?.('owner:aki')).toBe(false);
    } finally {
      rmSync(dir, { recursive: true, force: true });
    }
  });
});

describe('TG CC slash command passthrough', () => {
  it('keeps /compact on the CC passthrough path while reserving native RPC handling for Pi', async () => {
    const dir = mkdtempSync(join(tmpdir(), 'relay-tg-compact-'));
    try {
      const configPath = join(dir, 'relay.json');
      writeFileSync(configPath, JSON.stringify({
        users: [
          {
            userId: 'owner',
            chatId: '111222333',
            defaultAgent: 'emi',
            agents: [
              {
                id: 'emi',
                name: 'Emi',
                emoji: 'E',
                botToken: '123456:fake-token-not-a-real-secret',
                workspace: '/tmp/emi',
                sessionId: 'session-emi',
                runtime: 'cc',
              },
            ],
          },
        ],
      }));

      const cc = createMockAdapter('cc');
      const runtime = createConversationRuntimeManager();
      runtime.register(cc);
      const stateManager = createMemoryStateManager();
      const router = createRouter(configPath);
      await createTelegramBots({ runtime, router, stateManager }).start();
      const bot = grammyMock.instances[0];
      const compactHandler = bot.commandHandlers.get('compact');

      expect(compactHandler).toBeDefined();

      await compactHandler?.({
        chat: { id: 111222333 },
        match: '',
        reply: vi.fn(async () => {}),
      });

      expect(cc.send).toHaveBeenCalledWith('owner:emi', { type: 'human', content: '/compact' } satisfies StreamJsonMessage, anyTurnContext());
      const messages = bot.api.sendMessage.mock.calls.map((call) => String(call[1]));
      expect(messages).not.toContain('/compact 暂不支持，将在后续版本实现。');
    } finally {
      rmSync(dir, { recursive: true, force: true });
    }
  });
});

describe('TG session replacement and legacy wrap compatibility', () => {
  async function setupCodexHumanSession(): Promise<{
    codex: ConversationRuntimeAdapter & { events: EventEmitter };
    stateManager: StateManager & { updates: PersistedSession[] };
    textHandler: ((ctx: Record<string, unknown>) => Promise<void>) | undefined;
    newHandler: ((ctx: Record<string, unknown>) => Promise<void>) | undefined;
    resetHandler: ((ctx: Record<string, unknown>) => Promise<void>) | undefined;
  }> {
    const dir = mkdtempSync(join(tmpdir(), 'relay-tg-wrap-guard-'));
    const configPath = join(dir, 'relay.json');
    writeFileSync(configPath, JSON.stringify({
      users: [
        {
          userId: 'owner',
          chatId: '111222333',
          defaultAgent: 'aki',
          agents: [
            {
              id: 'aki',
              name: 'Aki',
              emoji: 'A',
              botToken: '123456:fake-token-not-a-real-secret',
              workspace: '/tmp/aki',
              sessionId: 'session-aki',
              runtime: 'codex',
            },
          ],
        },
      ],
    }));

    const codex = createMockAdapter('codex');
    const runtime = createConversationRuntimeManager();
    runtime.register(codex);
    const stateManager = createMemoryStateManager();
    const router = createRouter(configPath);
    await createTelegramBots({ runtime, router, stateManager }).start();
    rmSync(dir, { recursive: true, force: true });
    const bot = grammyMock.instances[0];
    return {
      codex,
      stateManager,
      textHandler: bot.handlers.get('message:text'),
      newHandler: bot.commandHandlers.get('new'),
      resetHandler: bot.commandHandlers.get('reset'),
    };
  }

  function existingSession(update: Partial<PersistedSession> = {}): Partial<PersistedSession> {
    return {
      poolKey: 'owner:aki',
      agentId: 'aki',
      runtime: 'codex',
      sessionId: 'session-aki',
      turnCount: 1,
      maxTurns: 100,
      lastActivity: Date.now(),
      status: 'active',
      ...update,
    };
  }

  it.each([
    '不用了。这篇文章7分。已经三天连续推机构文，常平仓没有英雄文吗？',
    '“不用了。这篇文章7分。。已经三天连续推机构文，常平仓没有英雄文吗？”',
    '"不用了。这篇文章7分。。已经三天连续推机构文，常平仓没有英雄文吗？"',
    '结束会话',
  ])('forwards close-like text as ordinary conversation: %s', async (text) => {
    const { codex, stateManager, textHandler } = await setupCodexHumanSession();
    stateManager.updateSession('owner:aki', existingSession());
    const reply = vi.fn(async () => {});

    await textHandler?.({
      chat: { id: 111222333 },
      message: { text },
      reply,
    });

    expect(codex.spawn).toHaveBeenCalledOnce();
    expect(codex.send).toHaveBeenCalledWith('owner:aki', { type: 'human', content: text } satisfies StreamJsonMessage, anyTurnContext());
  });

  it('records /wrap evidence and allows a later close-like Codex human message', async () => {
    const root = mkdtempSync(join(tmpdir(), 'relay-tg-wrap-skill-'));
    const skillPath = join(root, 'CollaborateWithAI', 'ssot', 'skills', 'ops', 'wrap', 'SKILL.md');
    mkdirSync(join(root, 'CollaborateWithAI', 'ssot', 'skills', 'ops', 'wrap'), { recursive: true });
    writeFileSync(skillPath, '# Test wrap skill\n');
    vi.stubEnv('PULSE_COEVOLUTION_ROOT', root);
    try {
      const { codex, stateManager, textHandler } = await setupCodexHumanSession();
      const reply = vi.fn(async () => {});

      await textHandler?.({
        chat: { id: 111222333 },
        message: { text: '/wrap' },
        reply,
      });
      await textHandler?.({
        chat: { id: 111222333 },
        message: { text: '结束会话' },
        reply,
      });

      expect(stateManager.getSession('owner:aki')?.wrapRequestedAt).toEqual(expect.any(Number));
      const expandedWrap = expandWrapCommandPrompt('/wrap');
      expect(expandedWrap).toContain('用户已授权 spawn 一个 reasoning_effort=medium 的收口员 sub-agent');
      expect(expandedWrap).toContain(realpathSync(skillPath));
      expect(expandedWrap).toContain('收口员可以草拟或编辑收口所需文件，但不得 git add 或 git commit');
      expect(expandedWrap).toContain('主 Agent 必须复核收口包和实际 diff，只提交本轮有意变更');
      expect(codex.send).toHaveBeenCalledWith('owner:aki', { type: 'human', content: expandedWrap } satisfies StreamJsonMessage, anyTurnContext());
      expect(codex.send).toHaveBeenCalledWith('owner:aki', { type: 'human', content: '结束会话' } satisfies StreamJsonMessage, anyTurnContext());
    } finally {
      vi.unstubAllEnvs();
      rmSync(root, { recursive: true, force: true });
    }
  });

  it('lets an ordinary session use /new after a legacy wrap blocker', async () => {
    const root = mkdtempSync(join(tmpdir(), 'relay-tg-wrap-blocked-'));
    const skillPath = join(root, 'ssot', 'skills', 'ops', 'wrap', 'SKILL.md');
    mkdirSync(join(root, 'ssot', 'skills', 'ops', 'wrap'), { recursive: true });
    writeFileSync(skillPath, '# Test wrap skill\n');
    vi.stubEnv('PULSE_COEVOLUTION_ROOT', root);
    try {
      const { codex, stateManager, textHandler, newHandler } = await setupCodexHumanSession();
      const reply = vi.fn(async () => {});
      await textHandler?.({ chat: { id: 111222333 }, message: { text: '/wrap' }, reply });
      expect(stateManager.getSession('owner:aki')?.wrapRequestedAt).toEqual(expect.any(Number));

      codex.events.emit('response', 'owner:aki', {
        type: 'assistant',
        delivery_phase: 'final',
        message: {
          stop_reason: 'end_turn',
          content: [{ type: 'text', text: 'BLOCKED_WRAP_ARCHIVE_AUTHORITY_UNAVAILABLE: archive root is not a Git worktree' }],
        },
      } satisfies StreamJsonMessage);
      expect(stateManager.getSession('owner:aki')?.wrapRequestedAt).toBeUndefined();

      await newHandler?.({ chat: { id: 111222333 }, reply });
      expect(reply).toHaveBeenCalledWith(expect.stringContaining('已开启新对话'));
      expect(stateManager.getSession('owner:aki')?.wrapRequestedAt).toBeUndefined();
    } finally {
      vi.unstubAllEnvs();
      rmSync(root, { recursive: true, force: true });
    }
  });

  it('clears a blocked /wrap even when the final response arrives before send acknowledges', async () => {
    const root = mkdtempSync(join(tmpdir(), 'relay-tg-wrap-early-final-'));
    const skillPath = join(root, 'ssot', 'skills', 'ops', 'wrap', 'SKILL.md');
    mkdirSync(join(root, 'ssot', 'skills', 'ops', 'wrap'), { recursive: true });
    writeFileSync(skillPath, '# Test wrap skill\n');
    vi.stubEnv('PULSE_COEVOLUTION_ROOT', root);
    try {
      const { codex, stateManager, textHandler, newHandler } = await setupCodexHumanSession();
      const reply = vi.fn(async () => {});
      vi.mocked(codex.send).mockImplementation(async () => {
        codex.events.emit('response', 'owner:aki', {
          type: 'assistant',
          delivery_phase: 'final',
          message: {
            stop_reason: 'end_turn',
            content: [{ type: 'text', text: 'BLOCKED_WRAP_ARCHIVE_AUTHORITY_UNAVAILABLE' }],
          },
        } satisfies StreamJsonMessage);
      });

      await textHandler?.({ chat: { id: 111222333 }, message: { text: '/wrap' }, reply });
      expect(stateManager.getSession('owner:aki')?.wrapRequestedAt).toBeUndefined();
      await newHandler?.({ chat: { id: 111222333 }, reply });
      expect(reply).toHaveBeenCalledWith(expect.stringContaining('已开启新对话'));
    } finally {
      vi.unstubAllEnvs();
      rmSync(root, { recursive: true, force: true });
    }
  });

  it('rolls back /wrap evidence when the runtime rejects the send', async () => {
    const root = mkdtempSync(join(tmpdir(), 'relay-tg-wrap-send-failure-'));
    const skillPath = join(root, 'ssot', 'skills', 'ops', 'wrap', 'SKILL.md');
    mkdirSync(join(root, 'ssot', 'skills', 'ops', 'wrap'), { recursive: true });
    writeFileSync(skillPath, '# Test wrap skill\n');
    vi.stubEnv('PULSE_COEVOLUTION_ROOT', root);
    try {
      const { codex, stateManager, textHandler } = await setupCodexHumanSession();
      vi.mocked(codex.send).mockRejectedValueOnce(new Error('send rejected'));
      const reply = vi.fn(async () => {});

      await textHandler?.({ chat: { id: 111222333 }, message: { text: '/wrap' }, reply });
      // The handler returns once the turn is submitted; the rejection follows.
      await vi.waitFor(() => expect(reply).toHaveBeenCalledWith(expect.stringContaining('消息发送失败')));
      expect(stateManager.getSession('owner:aki')?.wrapRequestedAt).toBeUndefined();
    } finally {
      vi.unstubAllEnvs();
      rmSync(root, { recursive: true, force: true });
    }
  });

  it('resolves a canonical Coevolution wrap skill and refuses a missing installation', async () => {
    const root = mkdtempSync(join(tmpdir(), 'relay-tg-wrap-root-'));
    vi.stubEnv('PULSE_COEVOLUTION_ROOT', root);
    try {
      const { codex, stateManager, textHandler } = await setupCodexHumanSession();
      const reply = vi.fn(async () => {});
      await textHandler?.({ chat: { id: 111222333 }, message: { text: '/wrap' }, reply });
      expect(reply).toHaveBeenCalledWith(expect.stringContaining('找不到已安装的 /wrap Skill'));
      expect(stateManager.getSession('owner:aki')?.wrapRequestedAt).toBeUndefined();
      expect(codex.send).not.toHaveBeenCalled();

      const skillPath = join(root, 'ssot', 'skills', 'ops', 'wrap', 'SKILL.md');
      mkdirSync(join(root, 'ssot', 'skills', 'ops', 'wrap'), { recursive: true });
      writeFileSync(skillPath, '# Test wrap skill\n');
      expect(resolveWrapSkillPath()).toBe(realpathSync(skillPath));
    } finally {
      vi.unstubAllEnvs();
      rmSync(root, { recursive: true, force: true });
    }
  });

  it('records hard-wrap evidence without sending /wrap to a context-limited old session', async () => {
    const { codex, stateManager, textHandler } = await setupCodexHumanSession();
    const reply = vi.fn(async () => {});

    await textHandler?.({
      chat: { id: 111222333 },
      message: { text: 'hello' },
      reply,
    });
    codex.events.emit('response', 'owner:aki', {
      type: 'assistant',
      message: {
        stop_reason: 'end_turn',
        content: [
          { type: 'text', text: 'API Error: The model has reached its context window limit.' },
        ],
      },
    } satisfies StreamJsonMessage);
    vi.mocked(codex.send).mockClear();

    await textHandler?.({
      chat: { id: 111222333 },
      message: { text: '/wrap' },
      reply,
    });

    const session = stateManager.getSession('owner:aki');
    expect(session?.recoveryRequiredAt).toEqual(expect.any(Number));
    expect(session?.recoveryReason).toBe('context-window-limit');
    expect(session?.wrapRequestedAt).toEqual(expect.any(Number));
    expect(session?.status).toBe('recovery-required');
    expect(codex.send).not.toHaveBeenCalled();
    expect(reply).toHaveBeenCalledWith(expect.stringContaining('hard-wrap'));
    expect(reply).toHaveBeenCalledWith(expect.stringContaining('无法在旧会话内执行 /wrap'));
  });

  it('recovers context-limit state from old session jsonl tail before /wrap', async () => {
    const originalHome = process.env['HOME'];
    const homeDir = mkdtempSync(join(tmpdir(), 'relay-tg-hard-wrap-home-'));
    process.env['HOME'] = homeDir;
    try {
      const { codex, stateManager, textHandler } = await setupCodexHumanSession();
      stateManager.updateSession('owner:aki', existingSession());
      const sessionDir = join(homeDir, '.claude', 'projects', '-tmp-aki');
      mkdirSync(sessionDir, { recursive: true });
      writeFileSync(
        join(sessionDir, 'session-aki.jsonl'),
        '{"type":"assistant","message":{"content":[{"type":"text","text":"API Error: The model has reached its context window limit."}]}}\n',
      );
      const reply = vi.fn(async () => {});

      await textHandler?.({
        chat: { id: 111222333 },
        message: { text: '/wrap' },
        reply,
      });

      expect(codex.send).not.toHaveBeenCalledWith('owner:aki', { type: 'human', content: '/wrap' } satisfies StreamJsonMessage, anyTurnContext());
      expect(reply).toHaveBeenCalledWith(expect.stringContaining('hard-wrap'));
      expect(stateManager.getSession('owner:aki')?.recoveryRequiredAt).toEqual(expect.any(Number));
      expect(stateManager.getSession('owner:aki')?.recoveryReason).toBe('context-window-limit');
      expect(stateManager.getSession('owner:aki')?.wrapRequestedAt).toEqual(expect.any(Number));
    } finally {
      if (originalHome === undefined) delete process.env['HOME'];
      else process.env['HOME'] = originalHome;
      rmSync(homeDir, { recursive: true, force: true });
    }
  });

  it('allows /new after hard-wrap evidence has been recorded', async () => {
    const { codex, stateManager, textHandler, newHandler } = await setupCodexHumanSession();
    stateManager.updateSession('owner:aki', existingSession({
      recoveryRequiredAt: Date.now(),
      recoveryReason: 'compact-failed',
      status: 'recovery-required',
    }));
    const reply = vi.fn(async () => {});

    await textHandler?.({
      chat: { id: 111222333 },
      message: { text: '/wrap' },
      reply,
    });
    await newHandler?.({
      chat: { id: 111222333 },
      reply,
    });

    expect(codex.send).not.toHaveBeenCalledWith('owner:aki', { type: 'human', content: '/wrap' } satisfies StreamJsonMessage, anyTurnContext());
    expect(reply).toHaveBeenCalledWith(expect.stringContaining('已开启新对话'));
    expect(stateManager.getSession('owner:aki')?.recoveryRequiredAt).toBeUndefined();
    expect(stateManager.getSession('owner:aki')?.recoveryReason).toBeUndefined();
  });

  it('allows /new after exact terminal closeout without requiring a second wrap', async () => {
    const {stateManager,newHandler}=await setupCodexHumanSession();
    const session=existingSession();
    stateManager.updateSession('owner:aki',{...session,terminalWorkItemRuntimeBinding:{
      binding:{schema:'pulse_active_work_item_runtime_binding/v1',workItemId:'wi_20260828_309',sessionId:session.sessionId,
        sessionRef:'session_v1_closed',runtime:session.runtime},
      lifecycleStatus:'completed',localRevision:15,stateDigest:'f'.repeat(64),observedAt:new Date().toISOString(),
    }} as never);
    const reply=vi.fn(async()=>{});
    await newHandler?.({chat:{id:111222333},reply});
    expect(reply).toHaveBeenCalledWith(expect.stringContaining('已开启新对话'));
    expect(stateManager.getSession('owner:aki')?.sessionId).not.toBe(session.sessionId);
  });

  it('allows /new for an ordinary session without a legacy wrap marker', async () => {
    const { stateManager, newHandler } = await setupCodexHumanSession();
    stateManager.updateSession('owner:aki', existingSession());
    const reply = vi.fn(async () => {});

    await newHandler?.({
      chat: { id: 111222333 },
      reply,
    });

    expect(reply).toHaveBeenCalledWith(expect.stringContaining('已开启新对话'));
    expect(stateManager.getSession('owner:aki')?.sessionId).not.toBe('session-aki');
  });

  it('still blocks /new for an active WI even with a legacy wrap marker', async () => {
    const { codex, stateManager, newHandler } = await setupCodexHumanSession();
    stateManager.updateSession('owner:aki', existingSession({
      wrapRequestedAt: Date.now(),
      activeWorkItemRuntimeBinding: {
        schema: 'pulse_active_work_item_runtime_binding/v1',
        workItemId: 'wi_20260922_018',
        sessionId: 'session-aki',
        sessionRef: runtimeAuthorityRef('session-aki'),
        runtime: 'codex',
      },
    }));
    const reply = vi.fn(async () => {});

    await newHandler?.({ chat: { id: 111222333 }, reply });

    expect(reply).toHaveBeenCalledWith(expect.stringContaining('/wi rollover wi_20260922_018'));
    expect(stateManager.getSession('owner:aki')?.sessionId).toBe('session-aki');
    expect(codex.kill).not.toHaveBeenCalled();
  });

  it('allows /reset to discard an existing unwrapped human session', async () => {
    const { codex, stateManager, resetHandler } = await setupCodexHumanSession();
    stateManager.updateSession('owner:aki', existingSession());
    const reply = vi.fn(async () => {});

    await resetHandler?.({
      chat: { id: 111222333 },
      reply,
    });

    expect(reply.mock.calls[0]?.[0]).toContain('已强制重置 session');
    expect(reply.mock.calls[0]?.[0]).toContain('历史上下文已丢弃');
    expect(codex.kill).not.toHaveBeenCalled();
    const session = stateManager.getSession('owner:aki');
    expect(session?.sessionId).not.toBe('session-aki');
    expect(session?.wrapRequestedAt).toBeUndefined();
    expect(session?.recoveryRequiredAt).toBeUndefined();
  });

  it('hard-wraps out of band when /wrap is requested after context limit', async () => {
    const { codex, stateManager, textHandler } = await setupCodexHumanSession();
    stateManager.updateSession('owner:aki', existingSession());
    const bot = grammyMock.instances[0];
    const reply = vi.fn(async () => {});

    codex.events.emit('response', 'owner:aki', {
      type: 'assistant',
      message: { stop_reason: 'end_turn', content: [{ type: 'text', text: 'API Error: The model has reached its context window limit.' }] },
      content: 'API Error: The model has reached its context window limit.',
    } satisfies StreamJsonMessage);

    await textHandler?.({
      chat: { id: 111222333 },
      message: { text: '/wrap' },
      reply,
    });

    expect(codex.send).not.toHaveBeenCalledWith('owner:aki', { type: 'human', content: '/wrap' } satisfies StreamJsonMessage, anyTurnContext());
    expect(reply.mock.calls[0]?.[0]).toContain('hard-wrap');
    expect(stateManager.getSession('owner:aki')?.wrapRequestedAt).toEqual(expect.any(Number));
    expect(stateManager.getSession('owner:aki')?.recoveryRequiredAt).toEqual(expect.any(Number));
    expect(bot.api.sendMessage).toHaveBeenCalledWith(
      111222333,
      'API Error: The model has reached its context window limit.',
    );
  });

  it('allows /new after context-limit hard recovery while preserving the normal guard', async () => {
    const { codex, stateManager, newHandler } = await setupCodexHumanSession();
    stateManager.updateSession('owner:aki', existingSession({
      recoveryRequiredAt: Date.now(),
      recoveryReason: 'context-window-limit',
    }));
    const reply = vi.fn(async () => {});

    await newHandler?.({
      chat: { id: 111222333 },
      reply,
    });

    expect(reply.mock.calls[0]?.[0]).toContain('旧会话上下文已满');
    expect(reply.mock.calls[0]?.[0]).toContain('已开启新对话');
    expect(codex.kill).not.toHaveBeenCalled();
    expect(stateManager.getSession('owner:aki')?.wrapRequestedAt).toBeUndefined();
    expect(stateManager.getSession('owner:aki')?.recoveryRequiredAt).toBeUndefined();
  });
});


describe('TG /wi command', () => {
  it('routes native /wi to the same Pi session, bypassing legacy controller and retry',async()=>{
    const dir=mkdtempSync(join(tmpdir(),'native-tg-command-'));
    const previous=process.env.RELAY_PI_RUNTIME_MANIFEST_PATH;
    const runtime=createConversationRuntimeManager(), adapter=createMockAdapter('pi');
    runtime.register(adapter);
    try {
      process.env.RELAY_PI_RUNTIME_MANIFEST_PATH=nativeCoreFixture(dir);
      const configPath=join(dir,'relay.json');
      writeFileSync(configPath,JSON.stringify({users:[{userId:'owner',chatId:'111222333',defaultAgent:'pulse',
        agents:[{id:'pulse',name:'Pulse',emoji:'P',botToken:'123456:native',workspace:dir,
          sessionId:'original-pi',runtime:'pi',conversationRuntime:{runtime:'pi',isolationProfile:'daily',
          model:{provider:'openai',model:'fixture',reasoning:'low'}}}]}]}));
      await createTelegramBots({runtime,router:createRouter(configPath)}).start();
      const reply=vi.fn(),text='/wi continue wi_native_fixture';
      await grammyMock.instances[0].commandHandlers.get('wi')!({chat:{id:111222333},
        update:{update_id:818},message:{message_id:818,text},match:'continue wi_native_fixture',reply});
      expect(adapter.spawn).toHaveBeenCalledWith(expect.objectContaining({sessionId:'original-pi'}));
      // The core executes the command; the accepted work is then handed to the Agent once.
      expect(adapter.send).toHaveBeenCalledTimes(1);
      expect(adapter.send.mock.calls[0][1].content).toMatch(/^接手 wi_native_fixture：/);
      expect(JSON.parse(readFileSync(join(dir,'request.json'),'utf8')).ref).toBe(latestHumanInput('owner:pulse'));
      expect(reply).toHaveBeenCalledWith(expect.stringContaining('local_committed'));
    } finally {
      if(previous===undefined)delete process.env.RELAY_PI_RUNTIME_MANIFEST_PATH;
      else process.env.RELAY_PI_RUNTIME_MANIFEST_PATH=previous;
      runtime.dispose();rmSync(dir,{recursive:true,force:true});
    }
  });

});

// Preserve explicit rollback-path coverage; default folded UI is exercised in folded-process-channels.test.ts.
beforeEach(() => { vi.stubEnv('RELAY_WORK_PROCESS_DETAILS', 'off'); });



describe('TG collaboration delivery', () => {
  async function setup() {
    const dir = mkdtempSync(join(tmpdir(), 'relay-tg-collab-'));
    const configPath = join(dir, 'relay.json');
    writeFileSync(configPath, JSON.stringify({ users: [{ userId: 'owner', chatId: '111222333', defaultAgent: 'aki',
      agents: [{ id: 'aki', name: 'Aki', emoji: 'A', botToken: '123456:fake-token-not-a-real-secret',
        workspace: '/tmp/aki', sessionId: 'session-aki', runtime: 'cc' }] }] }));
    const cc = createMockAdapter('cc');
    const runtime = createConversationRuntimeManager();
    runtime.register(cc);
    const stateManager = createMemoryStateManager();
    stateManager.updateSession('owner:aki', { poolKey: 'owner:aki', agentId: 'aki', runtime: 'cc',
      sessionId: 'session-aki', turnCount: 1, maxTurns: 100, lastActivity: Date.now(), status: 'active' });
    const bots = createTelegramBots({ runtime, router: createRouter(configPath), stateManager });
    await bots.start();
    return { dir, cc, bots, bot: grammyMock.instances.at(-1)! };
  }

  it('starts one turn in the owned conversation whose native session is the holder', async () => {
    const { dir, cc, bots, bot } = await setup();
    try {
      expect(await bots.deliverInternalTurn('owner:aki', 'contract prompt', ['session-aki'])).toBe(true);
      expect(bot.api.sendMessage).toHaveBeenCalledWith(111222333, '📨 收到一份协作合同，开始处理。');
      expect(cc.send).toHaveBeenCalledWith('owner:aki', { type: 'human', content: 'contract prompt' }, anyTurnContext());
    } finally { rmSync(dir, { recursive: true, force: true }); }
  });

  it('names the sender and folds the contract details when a notice is given', async () => {
    const { dir, cc, bots, bot } = await setup();
    try {
      const notice = { role: 'recipient' as const, contractId: 'op_643afde3b37f', workItemId: 'wi_20261023_675',
        kind: 'request', status: 'pending', targetWorkItemId: 'wi_20261024_002', mainOnly: true, offeredBy: 'Amanda' };
      expect(await bots.deliverInternalTurn('owner:aki', 'contract prompt', ['session-aki'], notice)).toBe(true);
      const [chatId, html, options] = bot.api.sendMessage.mock.calls.at(-1)!;
      expect(chatId).toBe(111222333);
      expect(html).toMatch(/^📨 收到 Amanda wi_20261023_675 协作合同，开始处理。\n<blockquote expandable>/);
      expect(html).toContain('op_643afde3b');
      expect(html).toContain('处理 WI：wi_20261024_002');
      expect(options).toEqual({ parse_mode: 'HTML' });
      expect(cc.send).toHaveBeenCalledWith('owner:aki', { type: 'human', content: 'contract prompt' }, anyTurnContext());
    } finally { rmSync(dir, { recursive: true, force: true }); }
  });

  it('sends nothing to another conversation or a changed native session', async () => {
    const { dir, cc, bots } = await setup();
    try {
      expect(await bots.deliverInternalTurn('owner:other', 'contract prompt', ['session-aki'])).toBe(false);
      expect(await bots.deliverInternalTurn('owner:aki', 'contract prompt', ['session-old'])).toBe(false);
      expect(cc.send).not.toHaveBeenCalled();
    } finally { rmSync(dir, { recursive: true, force: true }); }
  });
});


describe('Telegram staged attachment cleanup', () => {
  it('preserves the original Codex image if a local link cannot be attached', () => {
    const message = telegramImageMessage(Buffer.from('image'), 'image/png', 'codex');
    const path = (message.attachments as Array<{ path: string }>)[0].path;
    expect(existsSync(path)).toBe(true);
    expect(() => appendTelegramLocalFiles(message, ['file:///not-present-wi587.html'], [], 'codex')).toThrow();
    expect(existsSync(path)).toBe(true);
    rmSync(dirname(path), { recursive: true, force: true });
  });
});
