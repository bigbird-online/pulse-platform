import { describe, expect, it, afterEach } from 'vitest';
import type { StreamJsonMessage } from '../src/types.js';
import {
  extractResultModelId,
  readDefaultContextLimit,
  resolveConfiguredCcModelEnv,
  resolveContextWindowLimit,
} from '../src/model-context.js';

describe('model-context', () => {
  afterEach(() => {
    delete process.env['RELAY_MODEL_CONTEXT_LIMITS'];
    delete process.env['RELAY_CONTEXT_LIMIT_DEFAULT'];
    delete process.env['RELAY_CLAUDE_MODEL'];
    delete process.env['CLAUDE_CODE_MODEL'];
  });

  it('extractResultModelId reads top-level model', () => {
    const msg = { type: 'result', model: 'claude-opus-4-7', usage: {} } as StreamJsonMessage;
    expect(extractResultModelId(msg)).toBe('claude-opus-4-7');
  });

  it('extractResultModelId reads usage.model', () => {
    const msg = { type: 'result', usage: { model: 'claude-sonnet-4-20250514' } } as unknown as StreamJsonMessage;
    expect(extractResultModelId(msg)).toBe('claude-sonnet-4-20250514');
  });

  it('resolveContextWindowLimit returns RELAY_MODEL_CONTEXT_LIMITS exact match', () => {
    process.env['RELAY_MODEL_CONTEXT_LIMITS'] = JSON.stringify({ 'my-custom': 99999 });
    expect(resolveContextWindowLimit('my-custom')).toBe(99_999);
  });

  it('resolveContextWindowLimit uses built-in prefix when not in JSON map', () => {
    process.env['RELAY_MODEL_CONTEXT_LIMITS'] = JSON.stringify({ 'other': 50_000 });
    // claude 4.5 and earlier: 200k
    expect(resolveContextWindowLimit('claude-opus-4-20250514')).toBe(200_000);
    expect(resolveContextWindowLimit('gpt-5')).toBe(128_000);
  });

  it('resolveContextWindowLimit returns 1M only for claude-opus-4-7', () => {
    // opus-4-7 has native 1M; sonnet-4-6 needs beta header (not default) → stays 200k
    expect(resolveContextWindowLimit('claude-opus-4-7')).toBe(1_000_000);
    expect(resolveContextWindowLimit('claude-sonnet-4-6')).toBe(200_000);
    expect(resolveContextWindowLimit('claude-opus-4-6')).toBe(200_000);
  });

  it('resolveContextWindowLimit uses the default for unlisted gateway ids', () => {
    expect(resolveContextWindowLimit('deepseek/deepseek-v4-flash-0731')).toBe(128_000);
  });

  it('readDefaultContextLimit honors RELAY_CONTEXT_LIMIT_DEFAULT', () => {
    process.env['RELAY_CONTEXT_LIMIT_DEFAULT'] = '64000';
    expect(readDefaultContextLimit()).toBe(64_000);
  });

  it('readDefaultContextLimit is 128000 when unset', () => {
    expect(readDefaultContextLimit()).toBe(128_000);
  });

  it('resolveContextWindowLimit returns correct limits for Claude short aliases', () => {
    expect(resolveContextWindowLimit('opus')).toBe(1_000_000);   // opus → opus-4-7
    expect(resolveContextWindowLimit('sonnet')).toBe(200_000);
    expect(resolveContextWindowLimit('haiku')).toBe(200_000);
  });

  it('resolveConfiguredCcModelEnv matches pool env keys', () => {
    process.env['RELAY_CLAUDE_MODEL'] = ' from-env ';
    expect(resolveConfiguredCcModelEnv()).toBe('from-env');
  });
});
