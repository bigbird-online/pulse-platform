import { describe, it, expect } from 'vitest';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';
import {
  normalizeCnPhone,
  parseAllowlistLines,
  PhoneAllowlist,
} from '../src/phone-allowlist.js';

const here = dirname(fileURLToPath(import.meta.url));

describe('phone-allowlist [P0-003]', () => {
  it('normalizeCnPhone adds +86 for 11-digit China mobile', () => {
    expect(normalizeCnPhone('138 0013 8000')).toBe('+8613800138000');
    expect(normalizeCnPhone('13800138000')).toBe('+8613800138000');
  });

  it('normalizeCnPhone keeps explicit E.164', () => {
    expect(normalizeCnPhone('+86 138 0013 8000')).toBe('+8613800138000');
  });

  it('parseAllowlistLines ignores blanks and comments', () => {
    const raw = readFileSync(join(here, 'fixtures/phone-allowlist-sample.txt'), 'utf8');
    const set = parseAllowlistLines(raw);
    expect(set.has('+8613800138000')).toBe(true);
    expect(set.has('+8613800138001')).toBe(true);
    expect(set.size).toBe(2);
  });

  it('PhoneAllowlist.isAllowed uses normalized form', () => {
    const a = new PhoneAllowlist(new Set(['+8613800138000']));
    expect(a.isAllowed('13800138000')).toBe(true);
    expect(a.isAllowed('13900139000')).toBe(false);
  });

  it('PhoneAllowlist.loadFromFile reads fixture', () => {
    const a = PhoneAllowlist.loadFromFile(join(here, 'fixtures/phone-allowlist-sample.txt'));
    expect(a.isAllowed('13800138000')).toBe(true);
    expect(a.isAllowed('13800138001')).toBe(true);
  });
});
