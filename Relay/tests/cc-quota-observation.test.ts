import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { existsSync, mkdirSync, mkdtempSync, readdirSync, readFileSync, rmSync, statSync, symlinkSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import {
  isRateLimitFailureMessage,
  observeCcRateLimitEvent,
  parseCcQuotaObservation,
  resolveCcQuotaPath,
  writeCcQuotaDocument,
} from '../src/cc-quota-observation.js';

const observedAt = new Date('2026-10-01T03:30:00.123Z');
const event = (info: Record<string, unknown>) => ({ type: 'rate_limit_event', uuid: 'u-1', session_id: 'secret-session', rate_limit_info: info });
// 2026-10-01 实采样本（新形态，带 unifiedWindows）。
const sample = event({ status: 'allowed', resetsAt: 1790826600, rateLimitType: 'five_hour', overageStatus: 'rejected',
  overageDisabledReason: 'org_level_disabled', isUsingOverage: false,
  unifiedWindows: { five_hour: { utilization: 0.46, resetsAt: 1790826600 }, seven_day: { utilization: 0.04, resetsAt: 1790906400 } } });

describe('parseCcQuotaObservation', () => {
  it('maps the unified-window sample to the shared Stats contract exactly', () => {
    expect(parseCcQuotaObservation(sample, observedAt)).toEqual({
      schema: 'pulse.stats.cc_quota/v1',
      status: 'available',
      source: 'relay_cc_rate_limit_event',
      observed_at: '2026-10-01T03:30:00Z',
      weekly: { used_percent: 4, window_minutes: 10080, resets_at: 1790906400 },
      five_hour: { used_percent: 46, window_minutes: 300, resets_at: 1790826600 },
      rate_limit_status: 'allowed',
    });
  });

  it('accepts the old single-window shape and rounds to one decimal', () => {
    const document = parseCcQuotaObservation(event({ status: 'allowed_warning', rateLimitType: 'seven_day',
      utilization: 0.96789, resetsAt: 1790906400.9, surpassedThreshold: 0.75 }), observedAt);
    expect(document?.weekly).toEqual({ used_percent: 96.8, window_minutes: 10080, resets_at: 1790906400 });
    expect(document?.five_hour).toBeNull();
    expect(document?.rate_limit_status).toBe('allowed_warning');
  });

  it('rejects events without a usable seven_day window', () => {
    // 旧 fixture 无 resetsAt：周窗口不完整，不写半份文档。
    expect(parseCcQuotaObservation(event({ status: 'allowed_warning', rateLimitType: 'seven_day', utilization: 0.96 }))).toBeUndefined();
    expect(parseCcQuotaObservation(event({ status: 'allowed', rateLimitType: 'five_hour', utilization: 0.5, resetsAt: 1790826600 }))).toBeUndefined();
    expect(parseCcQuotaObservation(event({ status: 'allowed', unifiedWindows: { five_hour: { utilization: 0.5, resetsAt: 1790826600 } } }))).toBeUndefined();
    expect(parseCcQuotaObservation(event({ unifiedWindows: { seven_day: { utilization: 0.5, resetsAt: 1790906400 } } }))).toBeUndefined();
    expect(parseCcQuotaObservation({ type: 'rate_limit_event' })).toBeUndefined();
    expect(parseCcQuotaObservation({ type: 'assistant' })).toBeUndefined();
  });

  it('rejects out-of-range utilization and reset times', () => {
    for (const seven of [{ utilization: 1.2, resetsAt: 1790906400 }, { utilization: -0.1, resetsAt: 1790906400 },
      { utilization: Number.NaN, resetsAt: 1790906400 }, { utilization: '0.5', resetsAt: 1790906400 },
      { utilization: 0.5, resetsAt: 1790906400000 }, { utilization: 0.5, resetsAt: 0 }]) {
      expect(parseCcQuotaObservation(event({ status: 'allowed', unifiedWindows: { seven_day: seven } }))).toBeUndefined();
    }
    const document = parseCcQuotaObservation(event({ status: 'allowed', unifiedWindows: {
      seven_day: { utilization: 1, resetsAt: 1790906400 }, five_hour: { utilization: 3, resetsAt: 1790826600 } } }));
    expect(document?.weekly.used_percent).toBe(100);
    expect(document?.five_hour).toBeNull();
  });
});

describe('CC quota document persistence', () => {
  let dir: string;
  beforeEach(() => { dir = mkdtempSync(join(tmpdir(), 'cc-quota-')); });
  afterEach(() => { vi.restoreAllMocks(); rmSync(dir, { recursive: true, force: true }); });

  it('resolves the explicit path first, then RELAY_DATA_DIR, otherwise stays disabled', () => {
    expect(resolveCcQuotaPath({ PULSE_CC_QUOTA_PATH: '/x/q.json', RELAY_DATA_DIR: '/data' })).toBe('/x/q.json');
    expect(resolveCcQuotaPath({ RELAY_DATA_DIR: '/data' })).toBe('/data/quota/cc-quota-v1.json');
    expect(resolveCcQuotaPath({})).toBeUndefined();
    expect(observeCcRateLimitEvent(sample, {})).toBe(false);
  });

  it('atomically replaces the document with mode 0600 and no secrets or temp leftovers', () => {
    const path = join(dir, 'quota', 'cc-quota-v1.json');
    expect(observeCcRateLimitEvent(sample, { RELAY_DATA_DIR: dir }, observedAt)).toBe(true);
    expect(observeCcRateLimitEvent(event({ status: 'allowed', unifiedWindows: { seven_day: { utilization: 0.05, resetsAt: 1790906400 } } }),
      { RELAY_DATA_DIR: dir }, observedAt)).toBe(true);
    const raw = readFileSync(path, 'utf8');
    expect(JSON.parse(raw).weekly.used_percent).toBe(5);
    expect(statSync(path).mode & 0o777).toBe(0o600);
    expect(raw).not.toMatch(/secret-session|u-1|org_level_disabled/);
    expect(readdirSync(join(dir, 'quota'))).toEqual(['cc-quota-v1.json']);
  });

  it('does not write when the event lacks a weekly window', () => {
    expect(observeCcRateLimitEvent(event({ status: 'allowed' }), { RELAY_DATA_DIR: dir })).toBe(false);
    expect(existsSync(join(dir, 'quota'))).toBe(false);
  });

  it('refuses a symlinked target and only warns on write failure', () => {
    const target = join(dir, 'elsewhere.json');
    writeFileSync(target, 'keep');
    mkdirSync(join(dir, 'quota'));
    symlinkSync(target, join(dir, 'quota', 'cc-quota-v1.json'));
    const warn = vi.spyOn(console, 'warn').mockImplementation(() => {});
    expect(observeCcRateLimitEvent(sample, { RELAY_DATA_DIR: dir })).toBe(false);
    expect(readFileSync(target, 'utf8')).toBe('keep');
    expect(warn).toHaveBeenCalledWith(expect.objectContaining({ level: 'warn', msg: 'CC quota observation write failed' }));
    expect(readdirSync(join(dir, 'quota'))).toEqual(['cc-quota-v1.json']);
    const document = parseCcQuotaObservation(sample)!;
    expect(() => writeCcQuotaDocument(join(dir, 'quota', 'cc-quota-v1.json'), document)).toThrow(/symlink/);
  });
});

describe('isRateLimitFailureMessage', () => {
  it('treats every allowed* status as a pass and anything else as a rate-limit failure', () => {
    expect(isRateLimitFailureMessage(sample)).toBe(false);
    expect(isRateLimitFailureMessage(event({ status: 'allowed_warning' }))).toBe(false);
    expect(isRateLimitFailureMessage(event({ status: 'rejected' }))).toBe(true);
    expect(isRateLimitFailureMessage({ type: 'rate_limit_event' })).toBe(true);
    expect(isRateLimitFailureMessage({ type: 'system', subtype: 'api_retry', error_status: 429 })).toBe(true);
    expect(isRateLimitFailureMessage({ type: 'assistant' })).toBe(false);
  });
});
