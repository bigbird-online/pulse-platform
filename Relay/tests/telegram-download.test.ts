import { afterEach, describe, expect, it, vi } from 'vitest';
import { EventEmitter } from 'node:events';
import { PassThrough } from 'node:stream';

const network = vi.hoisted(() => ({ get: vi.fn() }));
vi.mock('node:https', async original => ({ ...await original<typeof import('node:https')>(), get: network.get }));
const { downloadTelegramFile, telegramApiAgent, TELEGRAM_DOWNLOAD_MAX_BYTES } = await import('../src/channels/telegram-download.js');
afterEach(() => vi.restoreAllMocks());

function respond(status: number, chunks: Buffer[], error?: Error) {
  network.get.mockImplementation((_url, options, callback) => {
    const request = new EventEmitter() as EventEmitter & { destroy: ReturnType<typeof vi.fn> };
    request.destroy = vi.fn();
    queueMicrotask(() => {
      if (error) { request.emit('error', error); return; }
      const response = Object.assign(new PassThrough(), { statusCode: status });
      callback(response);
      for (const chunk of chunks) response.write(chunk);
      response.end();
    });
    return request;
  });
}

describe('Telegram attachment transport', () => {
  it('downloads bytes using the same IPv4 agent as Bot API, even when global fetch fails', async () => {
    vi.spyOn(globalThis, 'fetch').mockRejectedValue(new TypeError('fetch failed'));
    respond(200, [Buffer.from('jpeg')]);
    expect(await downloadTelegramFile('https://api.telegram.org/file/botSECRET/photos/a.jpg')).toEqual(Buffer.from('jpeg'));
    expect(network.get.mock.lastCall?.[1].agent).toBe(telegramApiAgent);
    expect(telegramApiAgent.options.family).toBe(4);
    expect(network.get.mock.lastCall?.[1].signal).toBeInstanceOf(AbortSignal);
    expect(fetch).not.toHaveBeenCalled();
  });
  it('fails on an HTTP error and never reports a credential-bearing URL', async () => {
    respond(404, []);
    await expect(downloadTelegramFile('https://api.telegram.org/file/botSECRET/a')).rejects.toThrow('HTTP 404');
    respond(200, [], Object.assign(new Error('SECRET URL'), { code: 'ETIMEDOUT' }));
    await expect(downloadTelegramFile('https://api.telegram.org/file/botSECRET/a')).rejects.toThrow('ETIMEDOUT');
    try { await downloadTelegramFile('https://api.telegram.org/file/botSECRET/a'); } catch (e) { expect(String(e)).not.toContain('SECRET'); }
  });
  it('bounds the actual downloaded bytes', async () => {
    respond(200, [Buffer.alloc(TELEGRAM_DOWNLOAD_MAX_BYTES + 1)]);
    await expect(downloadTelegramFile('https://api.telegram.org/file/botSECRET/a')).rejects.toThrow('20 MiB');
  });
});
