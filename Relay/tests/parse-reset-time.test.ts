import { describe, it, expect } from 'vitest';
import { parseResetTime } from '../src/parse-reset-time.js';

describe('parseResetTime', () => {
  it('parses clean "reset at" text', () => {
    const text = 'Your limit will reset at 2026-06-17 15:40:55';
    expect(parseResetTime(text)).toBe(Date.parse('2026-06-17T15:40:55'));
  });

  it('parses ISO-style date with T separator', () => {
    const text = 'reset at 2026-06-03T23:50:02';
    expect(parseResetTime(text)).toBe(Date.parse('2026-06-03T23:50:02'));
  });

  // Regression: PTY rendering leaves ?-prefixed CSI sequences (\x1B[?2026h)
  // that the old stripping regex \x1b\[[0-9;]*[a-zA-Z] did not match.
  it('parses text with ?-prefixed ANSI CSI escapes', () => {
    const text = '\x1B[?2026h ⎿ APIError:429 [1310]Your limit will reset at 2026-06-17 15:40:55';
    expect(parseResetTime(text)).toBe(Date.parse('2026-06-17T15:40:55'));
  });

  it('parses text with standard ANSI CSI escapes', () => {
    const text = '\x1B[31mError\x1B[0m reset at 2026-06-17 15:40:55';
    expect(parseResetTime(text)).toBe(Date.parse('2026-06-17T15:40:55'));
  });

  it('parses text with OSC escape sequences', () => {
    const text = '\x1B]0;Claude Code\x07 reset at 2026-06-17 15:40:55';
    expect(parseResetTime(text)).toBe(Date.parse('2026-06-17T15:40:55'));
  });

  // Regression: PTY rendering may garble "reset at" → "rsetat" (missing chars).
  // parseResetTime must not depend on the "reset at" prefix — match the date
  // pattern directly.
  it('parses garbled text without "reset at" prefix', () => {
    const text = 'MonthlyLimitExhausted.Yourlimitwill rsetat 2026-06-17 15:40:55';
    expect(parseResetTime(text)).toBe(Date.parse('2026-06-17T15:40:55'));
  });

  it('does NOT match digit-only request IDs (no dashes)', () => {
    // Request IDs look like 2026061709554369782b5c8 — no dashes, won't match
    const text = '[2026061709554369782b5c8] rate limit';
    expect(parseResetTime(text)).toBeUndefined();
  });

  it('returns undefined for text without a date', () => {
    expect(parseResetTime('rate limit reached, no time given')).toBeUndefined();
  });

  it('returns undefined for empty string', () => {
    expect(parseResetTime('')).toBeUndefined();
  });

  // Regression: PTY rendering may consume the separator between date and time,
  // producing "2026-06-1801:52:49" (no space/T). The separator must be optional.
  it('parses date-time without separator (PTY-consumed space)', () => {
    const text = '[1308][Usagelimitreachedfor5hour.Yourlimitwill resetat2026-06-1801:52:49][20260617221151530f8ac15cd644e9]';
    expect(parseResetTime(text)).toBe(Date.parse('2026-06-18T01:52:49'));
  });
});
