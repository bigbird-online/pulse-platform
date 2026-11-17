import { appendFileSync, existsSync, mkdirSync, readFileSync } from 'node:fs';
import { dirname } from 'node:path';
import { withFileLock } from './file-lock.js';
import { normalizeCnPhone, parseAllowlistLines } from './phone-allowlist.js';

export type AllowlistWriteResult =
  | { ok: true; already: boolean; normalized: string }
  | { ok: false; error: 'allowlist_path_unset' | 'invalid_phone' | 'io_error'; message?: string };

function isPlausibleCnMobile(raw: string): boolean {
  const digits = raw.replace(/\D/g, '');
  // Accept +86/86-prefixed or plain 11-digit mainland mobile.
  const core = digits.startsWith('86') && digits.length >= 13 ? digits.slice(2) : digits;
  return core.length === 11 && core.startsWith('1');
}

export async function addPhoneToAllowlistFile(opts: {
  allowlistPath: string;
  phone: string;
  nowIso?: string;
}): Promise<AllowlistWriteResult> {
  const path = opts.allowlistPath.trim();
  if (!path) return { ok: false, error: 'allowlist_path_unset' };
  if (!isPlausibleCnMobile(opts.phone)) return { ok: false, error: 'invalid_phone' };

  const normalized = normalizeCnPhone(opts.phone);
  const nowIso = opts.nowIso ?? new Date().toISOString();
  const lockPath = `${path}.lock`;

  try {
    mkdirSync(dirname(path), { recursive: true });
    return await withFileLock(lockPath, async () => {
      const existing = existsSync(path) ? parseAllowlistLines(readFileSync(path, 'utf8')) : new Set<string>();
      if (existing.has(normalized)) return { ok: true, already: true, normalized };
      const line = `${opts.phone.trim()} # added ${nowIso}\n`;
      appendFileSync(path, line, 'utf8');
      return { ok: true, already: false, normalized };
    });
  } catch (e) {
    const message = e instanceof Error ? e.message : String(e);
    return { ok: false, error: 'io_error', message };
  }
}

