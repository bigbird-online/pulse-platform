import { describe, it, expect, beforeEach, afterEach } from 'vitest';
import { mkdtempSync, readFileSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { addPhoneToAllowlistFile } from '../src/allowlist-admin.js';
import { PhoneAllowlist } from '../src/phone-allowlist.js';

let dir: string;

beforeEach(() => {
  dir = mkdtempSync(join(tmpdir(), 'allowlist-admin-'));
});

afterEach(() => {
  rmSync(dir, { recursive: true, force: true });
});

describe('addPhoneToAllowlistFile', () => {
  it('requires allowlist path', async () => {
    const r = await addPhoneToAllowlistFile({ allowlistPath: '', phone: '13800138000' });
    expect(r.ok).toBe(false);
    if (!r.ok) expect(r.error).toBe('allowlist_path_unset');
  });

  it('rejects invalid phone', async () => {
    const r = await addPhoneToAllowlistFile({ allowlistPath: join(dir, 'a.txt'), phone: 'not-phone' });
    expect(r.ok).toBe(false);
    if (!r.ok) expect(r.error).toBe('invalid_phone');
  });

  it('appends once and is idempotent', async () => {
    const path = join(dir, 'phones.txt');
    const r1 = await addPhoneToAllowlistFile({ allowlistPath: path, phone: '13893454471', nowIso: '2026-05-05T00:00:00Z' });
    expect(r1).toEqual({ ok: true, already: false, normalized: '+8613893454471' });

    const r2 = await addPhoneToAllowlistFile({ allowlistPath: path, phone: '+86 13893454471', nowIso: '2026-05-05T00:00:01Z' });
    expect(r2).toEqual({ ok: true, already: true, normalized: '+8613893454471' });

    const raw = readFileSync(path, 'utf8');
    expect(raw.split('\n').filter(Boolean).length).toBe(1);

    const allow = PhoneAllowlist.loadFromFile(path);
    expect(allow.isAllowed('13893454471')).toBe(true);
  });
});

