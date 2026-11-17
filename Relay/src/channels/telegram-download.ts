import { Agent, get } from 'node:https';

// Bot API requests and file downloads must use the same IPv4 transport.
// DNS ipv4first alone still lets Node abandon IPv4 for an unreachable IPv6.
export const telegramApiAgent = new Agent({ keepAlive: true, family: 4 });
export const TELEGRAM_DOWNLOAD_MAX_BYTES = 20 * 1024 * 1024;

export function downloadTelegramFile(url: string): Promise<Buffer> {
  return new Promise((resolve, reject) => {
    const fail = (error: unknown) => {
      // The URL contains the bot credential. Never include it in an error.
      const code = (error as NodeJS.ErrnoException)?.code;
      reject(new Error(`Telegram file download failed (${code ?? 'connection error'})`));
    };
    const request = get(url, { agent: telegramApiAgent, signal: AbortSignal.timeout(30_000) }, response => {
      if (response.statusCode !== 200) {
        response.resume();
        reject(new Error(`Telegram file download returned HTTP ${response.statusCode}`));
        return;
      }
      const chunks: Buffer[] = [];
      let size = 0;
      response.on('data', (chunk: Buffer) => {
        size += chunk.length;
        if (size > TELEGRAM_DOWNLOAD_MAX_BYTES) {
          reject(new Error('Telegram file exceeds the 20 MiB download limit'));
          response.destroy();
          request.destroy();
          return;
        }
        chunks.push(chunk);
      });
      response.on('end', () => resolve(Buffer.concat(chunks)));
      response.on('error', fail);
      response.on('aborted', () => fail({ code: 'ECONNRESET' }));
    });
    request.on('error', fail);
  });
}
