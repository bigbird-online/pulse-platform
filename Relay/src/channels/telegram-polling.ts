/**
 * Telegram start-up that survives rate limits.
 *
 * Fifteen bots start one after another, so Telegram may answer an early call
 * with 429 "retry after N". Such a call is repeated after N seconds, a bounded
 * number of times. A bot whose polling fails to start, or stops on its own, is
 * started again in the background after a capped delay until it polls or the
 * bot is stopped; health reports it missing only meanwhile. Command menus are
 * registered on one background queue, so no bot's polling waits for a menu.
 */

const DEFAULT_RETRY_AFTER_MS = 5_000;
export const RATE_LIMIT_ATTEMPTS = 4;
export const RATE_LIMIT_MAX_WAIT_MS = 60_000;
/** Delays before restarting a bot whose polling did not start or stopped by itself. */
export const POLLING_RESTART_DELAYS_MS = [5_000, 15_000, 30_000, 60_000] as const;
/** A start that has not begun polling by then is stopped and started again. */
export const POLLING_START_TIMEOUT_MS = 90_000;

type Sleep = (ms: number) => Promise<void>;
const realSleep: Sleep = ms => new Promise(resolve => setTimeout(resolve, ms));

/** Milliseconds Telegram asked to wait for a 429 error; null for any other error. */
export function telegramRetryAfterMs(error: unknown): number | null {
  const value = error as { error_code?: unknown; parameters?: { retry_after?: unknown } } | null;
  if (!value || value.error_code !== 429) return null;
  const seconds = value.parameters?.retry_after;
  return typeof seconds === 'number' && Number.isFinite(seconds) && seconds >= 0
    ? seconds * 1000
    : DEFAULT_RETRY_AFTER_MS;
}

/** Run `call`, waiting out Telegram 429 answers; any other error, or the last 429, is thrown. */
export async function withTelegramRateLimit<T>(
  call: () => Promise<T>,
  options: { attempts?: number; maxWaitMs?: number; sleep?: Sleep } = {},
): Promise<T> {
  const attempts = options.attempts ?? RATE_LIMIT_ATTEMPTS;
  const sleep = options.sleep ?? realSleep;
  for (let attempt = 1; ; attempt++) {
    try {
      return await call();
    } catch (error) {
      const waitMs = telegramRetryAfterMs(error);
      if (waitMs === null || attempt >= attempts) throw error;
      await sleep(Math.min(waitMs, options.maxWaitMs ?? RATE_LIMIT_MAX_WAIT_MS));
    }
  }
}

export interface PollingBot {
  isInited(): boolean;
  botInfo: unknown;
  api: { getMe(): Promise<unknown> };
  start(options: { onStart: () => void }): Promise<void>;
  stop(): Promise<void>;
}

/** grammY's stop() returns a promise that may reject once polling is gone. */
export function stopQuietly(bot: { stop(): Promise<void> | void }): void {
  try {
    void Promise.resolve(bot.stop()).catch(() => {});
  } catch {
    // already stopped
  }
}

export interface PollingSupervisor {
  stop(): void;
}

/** Rejects with the signal's reason once it aborts; the underlying work is abandoned. */
function abortable<T>(work: Promise<T>, signal: AbortSignal): Promise<T> {
  return new Promise<T>((resolve, reject) => {
    if (signal.aborted) {
      reject(signal.reason);
      return;
    }
    const onAbort = (): void => reject(signal.reason);
    signal.addEventListener('abort', onAbort, { once: true });
    work.then(
      value => { signal.removeEventListener('abort', onAbort); resolve(value); },
      error => { signal.removeEventListener('abort', onAbort); reject(error); },
    );
  });
}

/**
 * Keep one bot polling. `onStart` runs each time polling begins; `onEnded`
 * each time an attempt fails or polling ends while the bot should still run.
 *
 * One attempt is getMe (with its 429 waits) plus grammY's start, bounded as a
 * whole by `startTimeoutMs`. A timed-out or stopped attempt is abandoned: a
 * late getMe answer never starts it and a late onStart never reports it.
 */
export function superviseTelegramPolling(
  bot: PollingBot,
  handlers: { onStart: () => void; onEnded: (error: unknown, restartInMs: number) => void },
  options: { sleep?: Sleep; startTimeoutMs?: number; delaysMs?: readonly number[] } = {},
): PollingSupervisor {
  const delays = options.delaysMs ?? POLLING_RESTART_DELAYS_MS;
  const startTimeoutMs = options.startTimeoutMs ?? POLLING_START_TIMEOUT_MS;
  const sleep = options.sleep ?? realSleep;
  let stopped = false;
  let failures = 0;
  let restartTimer: ReturnType<typeof setTimeout> | undefined;
  let attempt: AbortController | undefined;

  async function runOnce(controller: AbortController): Promise<unknown> {
    const { signal } = controller;
    let started = false;
    const watchdog = setTimeout(
      () => controller.abort(new Error('Telegram polling did not start in time')), startTimeoutMs);
    // An abandoned attempt that already reached grammY's start is stopped there.
    signal.addEventListener('abort', () => { if (!started) stopQuietly(bot); }, { once: true });
    try {
      if (!bot.isInited()) {
        const me = await abortable(withTelegramRateLimit(() => bot.api.getMe(),
          { sleep: ms => abortable(sleep(ms), signal) }), signal);
        bot.botInfo = me;
      }
      if (signal.aborted) return signal.reason;
      await bot.start({
        onStart: () => {
          if (signal.aborted) return;
          started = true;
          clearTimeout(watchdog);
          failures = 0;
          handlers.onStart();
        },
      });
      if (started) return new Error('Telegram polling ended');
      return signal.aborted ? signal.reason : new Error('Telegram polling did not start');
    } finally {
      clearTimeout(watchdog);
    }
  }

  function launch(): void {
    restartTimer = undefined;
    if (stopped) return;
    const controller = new AbortController();
    attempt = controller;
    void runOnce(controller).catch(error => error).then(error => {
      if (stopped || attempt !== controller) return;
      const delay = delays[Math.min(failures, delays.length - 1)];
      failures += 1;
      handlers.onEnded(error, delay);
      restartTimer = setTimeout(launch, delay);
    });
  }

  launch();
  return {
    stop(): void {
      if (stopped) return;
      stopped = true;
      if (restartTimer !== undefined) clearTimeout(restartTimer);
      restartTimer = undefined;
      attempt?.abort(new Error('Telegram bot stopped'));
      stopQuietly(bot);
    },
  };
}
