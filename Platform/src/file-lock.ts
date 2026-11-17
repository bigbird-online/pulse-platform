import { open, unlink } from 'node:fs/promises';
import { constants } from 'node:fs';

/** 与 relay.json 同目录的互斥锁文件（O_CREAT | O_EXCL）。 */
export async function withFileLock<T>(lockPath: string, fn: () => Promise<T>): Promise<T> {
  const deadline = Date.now() + 30_000;
  for (;;) {
    try {
      const fh = await open(lockPath, constants.O_CREAT | constants.O_EXCL | constants.O_WRONLY);
      try {
        await fh.writeFile(String(process.pid));
        return await fn();
      } finally {
        await fh.close();
        await unlink(lockPath).catch(() => undefined);
      }
    } catch (e: unknown) {
      const code = e && typeof e === 'object' && 'code' in e ? String((e as { code: unknown }).code) : '';
      if (code !== 'EEXIST') throw e;
      if (Date.now() >= deadline) {
        throw new Error(`lock_timeout:${lockPath}`);
      }
      await new Promise((r) => setTimeout(r, 15));
    }
  }
}
