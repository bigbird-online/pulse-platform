import type { Api } from 'grammy';

const ATTEMPTS = 3;
const ATTEMPT_TIMEOUT_MS = 20_000;
const NETWORK_CODES = new Set(['ECONNRESET', 'ETIMEDOUT', 'EAI_AGAIN', 'ENETUNREACH', 'EHOSTUNREACH', 'ECONNREFUSED', 'ABORT_ERR']);

export class TelegramFileLookupError extends Error {
  constructor(readonly temporary: boolean, readonly attempts: number, readonly status?: number) {
    // Never retain the original exception: transport errors may contain the bot URL.
    super(`Telegram getFile failed (${status ?? 'connection error'}; attempts=${attempts})`);
    this.name = 'TelegramFileLookupError';
  }
}

function failure(error: unknown, timedOut: boolean): { temporary: boolean; status?: number } {
  const e = error as { error_code?: number; code?: string; error?: { code?: string } } | null;
  const status = typeof e?.error_code === 'number' ? e.error_code : undefined;
  return { status, temporary: timedOut || (status !== undefined
    ? status >= 500 && status <= 599
    : NETWORK_CODES.has(e?.code ?? e?.error?.code ?? '')) };
}

/** Only retry this read-only API call, never the incoming turn or runtime.send. */
export async function getTelegramFile(
  api: Pick<Api, 'getFile'>,
  fileId: string,
): ReturnType<Api['getFile']> {
  for (let attempt = 1; ; attempt++) {
    const controller = new AbortController();
    let timer: ReturnType<typeof setTimeout> | undefined;
    try {
      // Promise.race also bounds a transport that fails to honour cancellation.
      const timeout = new Promise<never>((_, reject) => {
        timer = setTimeout(() => {
          controller.abort();
          reject(new Error('getFile timeout'));
        }, ATTEMPT_TIMEOUT_MS);
      });
      // grammy's declarations use the older abort-controller shim; at runtime
      // it accepts a native signal's addEventListener/removeEventListener API.
      const signal = controller.signal as unknown as Parameters<Api['getFile']>[1];
      return await Promise.race([api.getFile(fileId, signal), timeout]);
    } catch (error) {
      const result = failure(error, controller.signal.aborted);
      if (!result.temporary || attempt === ATTEMPTS) {
        throw new TelegramFileLookupError(result.temporary, attempt, result.status);
      }
    } finally {
      clearTimeout(timer);
    }
    await new Promise(resolve => setTimeout(resolve, attempt * 1_000));
  }
}

export function telegramPhotoFailureMessage(error: unknown): string {
  if (error instanceof TelegramFileLookupError) {
    return error.temporary
      ? '⚠️ Telegram 获取图片暂时失败，自动重试后共尝试 3 次仍未成功；图片尚未送达模型，请稍后重新发送。'
      : '⚠️ Telegram 无法获取这张图片，图片尚未送达模型；请重新发送，也可尝试以文件方式发送。';
  }
  return '⚠️ 图片处理失败，请稍后重试。';
}
