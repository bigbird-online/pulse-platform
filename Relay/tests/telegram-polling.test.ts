import { afterEach, describe, expect, it, vi } from 'vitest';
import { registerCommandMenu } from '../src/command-menu.js';
import {
  POLLING_RESTART_DELAYS_MS,
  superviseTelegramPolling,
  telegramRetryAfterMs,
  withTelegramRateLimit,
  type PollingBot,
} from '../src/channels/telegram-polling.js';

/** The shape grammY throws for "429: Too Many Requests: retry after N". */
function tooMany(seconds: number) {
  return Object.assign(new Error(`429: Too Many Requests: retry after ${seconds}`), {
    error_code: 429,
    parameters: { retry_after: seconds },
  });
}

describe('Telegram 429 retry', () => {
  it('reads retry_after only from a 429', () => {
    expect(telegramRetryAfterMs(tooMany(5))).toBe(5000);
    expect(telegramRetryAfterMs({ error_code: 400 })).toBeNull();
    expect(telegramRetryAfterMs(new Error('network'))).toBeNull();
  });

  it('waits retry_after, then succeeds', async () => {
    const waits: number[] = [];
    const call = vi.fn().mockRejectedValueOnce(tooMany(5)).mockResolvedValueOnce('ok');
    await expect(withTelegramRateLimit(call, { sleep: async ms => { waits.push(ms); } })).resolves.toBe('ok');
    expect(waits).toEqual([5000]);
  });

  it('gives up after the bound and never retries other errors', async () => {
    const sleep = vi.fn(async () => {});
    const limited = vi.fn().mockRejectedValue(tooMany(5));
    await expect(withTelegramRateLimit(limited, { attempts: 3, sleep })).rejects.toThrow('429');
    expect(limited).toHaveBeenCalledTimes(3);
    const broken = vi.fn().mockRejectedValue(new Error('400 Bad Request'));
    await expect(withTelegramRateLimit(broken, { sleep })).rejects.toThrow('400');
    expect(broken).toHaveBeenCalledTimes(1);
  });

  it('registers the command menu after a 429 (2026-11-08 Steward)', async () => {
    const setMyCommands = vi.fn().mockRejectedValueOnce(tooMany(5)).mockResolvedValueOnce(true);
    await registerCommandMenu({ api: { setMyCommands } }, { sleep: async () => {} });
    expect(setMyCommands).toHaveBeenCalledTimes(2);
  });
});

class FakeBot implements PollingBot {
  botInfo: unknown = undefined;
  starts = 0;
  stops = 0;
  private finish?: () => void;
  constructor(
    public api: { getMe(): Promise<unknown> },
    private behaviour: (attempt: number) => 'fail' | 'poll' | 'hang',
  ) {}
  isInited(): boolean { return this.botInfo !== undefined; }
  start(options: { onStart: () => void }): Promise<void> {
    this.starts += 1;
    const kind = this.behaviour(this.starts);
    if (kind === 'fail') return Promise.reject(new Error('start failed'));
    return new Promise(resolve => {
      this.finish = resolve;
      if (kind === 'poll') options.onStart();
    });
  }
  stop(): Promise<void> {
    this.stops += 1;
    this.finish?.();
    return Promise.resolve();
  }
}

describe('superviseTelegramPolling', () => {
  afterEach(() => { vi.useRealTimers(); });

  it('starts polling after getMe answered 429, without waiting for anything else', async () => {
    vi.useFakeTimers();
    const getMe = vi.fn().mockRejectedValueOnce(tooMany(5)).mockResolvedValue({ id: 1 });
    const bot = new FakeBot({ getMe }, () => 'poll');
    const onStart = vi.fn();
    superviseTelegramPolling(bot, { onStart, onEnded: vi.fn() }, { sleep: async () => {} });
    await vi.advanceTimersByTimeAsync(0);
    expect(getMe).toHaveBeenCalledTimes(2);
    expect(onStart).toHaveBeenCalledOnce();
  });

  it('restarts a failed start in the background with capped delays until it polls', async () => {
    vi.useFakeTimers();
    const bot = new FakeBot({ getMe: async () => ({ id: 1 }) }, attempt => (attempt < 6 ? 'fail' : 'poll'));
    const onStart = vi.fn();
    const delays: number[] = [];
    superviseTelegramPolling(bot, { onStart, onEnded: (_e, ms) => delays.push(ms) });
    await vi.advanceTimersByTimeAsync(10 * 60_000);
    expect(bot.starts).toBe(6);
    expect(onStart).toHaveBeenCalledOnce();
    expect(delays).toEqual([...POLLING_RESTART_DELAYS_MS, POLLING_RESTART_DELAYS_MS.at(-1)]);
  });

  it('stops and restarts a start that never begins polling', async () => {
    vi.useFakeTimers();
    const bot = new FakeBot({ getMe: async () => ({ id: 1 }) }, attempt => (attempt === 1 ? 'hang' : 'poll'));
    const onStart = vi.fn();
    const onEnded = vi.fn();
    superviseTelegramPolling(bot, { onStart, onEnded }, { startTimeoutMs: 1000 });
    await vi.advanceTimersByTimeAsync(1000 + POLLING_RESTART_DELAYS_MS[0]);
    expect(bot.stops).toBe(1);
    expect(String(onEnded.mock.calls[0][0])).toContain('did not start in time');
    expect(onStart).toHaveBeenCalledOnce();
  });

  it('a stop while getMe is pending never starts the bot, even when getMe answers late', async () => {
    vi.useFakeTimers();
    let answer!: (value: unknown) => void;
    const bot = new FakeBot({ getMe: () => new Promise(resolve => { answer = resolve; }) }, () => 'poll');
    bot.isInited = () => false;
    const onStart = vi.fn();
    const onEnded = vi.fn();
    const supervisor = superviseTelegramPolling(bot, { onStart, onEnded });
    await vi.advanceTimersByTimeAsync(0);
    supervisor.stop();
    answer({ id: 1 });
    await vi.advanceTimersByTimeAsync(10 * 60_000);
    expect(bot.starts).toBe(0);
    expect(bot.botInfo).toBeUndefined();
    expect(onStart).not.toHaveBeenCalled();
    expect(onEnded).not.toHaveBeenCalled();
  });

  it('a stop during a 429 retry_after wait never starts the bot', async () => {
    vi.useFakeTimers();
    const getMe = vi.fn().mockRejectedValueOnce(tooMany(60)).mockResolvedValue({ id: 1 });
    const bot = new FakeBot({ getMe }, () => 'poll');
    bot.isInited = () => bot.botInfo !== undefined;
    const onStart = vi.fn();
    const supervisor = superviseTelegramPolling(bot, { onStart, onEnded: vi.fn() });
    await vi.advanceTimersByTimeAsync(1000);
    supervisor.stop();
    await vi.advanceTimersByTimeAsync(10 * 60_000);
    expect(getMe).toHaveBeenCalledOnce();
    expect(bot.starts).toBe(0);
    expect(onStart).not.toHaveBeenCalled();
  });

  it('bounds a getMe that never answers and starts a fresh attempt; the late answer is ignored', async () => {
    vi.useFakeTimers();
    const late: Array<(value: unknown) => void> = [];
    let calls = 0;
    const getMe = vi.fn(() => {
      calls += 1;
      return calls === 1 ? new Promise(resolve => { late.push(resolve); }) : Promise.resolve({ id: 2 });
    });
    const bot = new FakeBot({ getMe }, () => 'poll');
    const onStart = vi.fn();
    const onEnded = vi.fn();
    superviseTelegramPolling(bot, { onStart, onEnded }, { startTimeoutMs: 1000 });
    await vi.advanceTimersByTimeAsync(1000);
    expect(String(onEnded.mock.calls[0]?.[0])).toContain('did not start in time');
    await vi.advanceTimersByTimeAsync(POLLING_RESTART_DELAYS_MS[0]);
    expect(onStart).toHaveBeenCalledOnce();
    expect(bot.botInfo).toEqual({ id: 2 });
    late[0]({ id: 1 });
    await vi.advanceTimersByTimeAsync(10 * 60_000);
    expect(bot.starts).toBe(1);
    expect(bot.botInfo).toEqual({ id: 2 });
    expect(onEnded).toHaveBeenCalledOnce();
  });

  it('never restarts after stop', async () => {
    vi.useFakeTimers();
    const bot = new FakeBot({ getMe: async () => ({ id: 1 }) }, () => 'fail');
    const supervisor = superviseTelegramPolling(bot, { onStart: vi.fn(), onEnded: vi.fn() });
    await vi.advanceTimersByTimeAsync(0);
    supervisor.stop();
    await vi.advanceTimersByTimeAsync(10 * 60_000);
    expect(bot.starts).toBe(1);
  });
});
