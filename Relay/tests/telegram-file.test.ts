import { afterEach, describe, expect, it, vi } from 'vitest';
import { GrammyError, HttpError } from 'grammy';
import { getTelegramFile, TelegramFileLookupError, telegramPhotoFailureMessage } from '../src/channels/telegram-file.js';

afterEach(() => vi.useRealTimers());
const apiError = (code: number) => new GrammyError('SECRET', { ok: false, error_code: code, description: 'SECRET' }, 'getFile', { file_id: 'SECRET' });

describe('Telegram getFile transient failure recovery', () => {
  it.each([500, 502, 503, 504])('recovers from %s before downloading or delivering a turn', async code => {
    vi.useFakeTimers();
    const file = { file_path: 'photos/picture.jpg' };
    const api = { getFile: vi.fn().mockRejectedValueOnce(apiError(code)).mockResolvedValue(file) };
    const pending = getTelegramFile(api, 'file-id');
    await vi.advanceTimersByTimeAsync(999);
    expect(api.getFile).toHaveBeenCalledTimes(1);
    await vi.advanceTimersByTimeAsync(1);
    expect(await pending).toBe(file);
    expect(api.getFile).toHaveBeenCalledTimes(2);
    expect(api.getFile.mock.calls[1][0]).toBe('file-id');
    expect(api.getFile.mock.calls[1][1]).toBeInstanceOf(AbortSignal);
    expect(vi.getTimerCount()).toBe(0);
  });
  it('retries actual grammy transport errors without exposing credentials', async () => {
    vi.useFakeTimers();
    const error = new HttpError('https://api.telegram.org/botSECRET/getFile', Object.assign(new Error('SECRET'), { code: 'ECONNRESET' }));
    const api = { getFile: vi.fn().mockRejectedValue(error) };
    const result = getTelegramFile(api, 'SECRET').catch(error => error);
    await vi.runAllTimersAsync();
    const failed = await result;
    expect(failed).toBeInstanceOf(TelegramFileLookupError);
    expect(failed.attempts).toBe(3);
    expect(api.getFile).toHaveBeenCalledTimes(3);
    expect(String(failed)).not.toContain('SECRET');
    expect(failed.cause).toBeUndefined();
    expect(telegramPhotoFailureMessage(failed)).toContain('图片尚未送达模型');
  });
  it.each([400, 401, 403, 404, 429])('does not retry permanent or rate-limited %s responses', async code => {
    const api = { getFile: vi.fn().mockRejectedValue(apiError(code)) };
    await expect(getTelegramFile(api, 'file-id')).rejects.toMatchObject({ temporary: false, attempts: 1, status: code });
    expect(api.getFile).toHaveBeenCalledTimes(1);
  });
  it('caps repeated 504 responses at three attempts and 3 seconds of backoff', async () => {
    vi.useFakeTimers();
    const api = { getFile: vi.fn().mockRejectedValue(apiError(504)) };
    const result = getTelegramFile(api, 'file-id').catch(error => error);
    await vi.advanceTimersByTimeAsync(3_000);
    expect(await result).toMatchObject({ temporary: true, attempts: 3, status: 504 });
    expect(api.getFile).toHaveBeenCalledTimes(3);
    expect(vi.getTimerCount()).toBe(0);
  });
  it('aborts stalled requests and bounds the entire lookup to 63 seconds even if abort is ignored', async () => {
    vi.useFakeTimers();
    const api = { getFile: vi.fn(() => new Promise<never>(() => {})) };
    const result = getTelegramFile(api, 'file-id').catch(error => error);
    await vi.advanceTimersByTimeAsync(63_000);
    expect(await result).toMatchObject({ temporary: true, attempts: 3 });
    expect(api.getFile).toHaveBeenCalledTimes(3);
    for (const call of vi.mocked(api.getFile).mock.calls as unknown as [string, AbortSignal][]) expect(call[1].aborted).toBe(true);
    expect(vi.getTimerCount()).toBe(0);
  });
  it('does not mislabel runtime failures as an undelivered image', () => {
    expect(telegramPhotoFailureMessage(new Error('runtime failed'))).not.toContain('尚未送达模型');
  });
});
