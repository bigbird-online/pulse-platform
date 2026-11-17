import { describe, it, expect, beforeEach, afterEach } from 'vitest';
import { isMockPayAllowed } from '../src/mock-pay-policy.js';

describe('mock-pay-policy [P0-002]', () => {
  const origEnv = process.env;

  beforeEach(() => {
    process.env = { ...origEnv };
    delete process.env['PLATFORM_ALLOW_MOCK_PAY'];
  });

  afterEach(() => {
    process.env = origEnv;
  });

  it('rejects when NODE_ENV is production even if PLATFORM_ALLOW_MOCK_PAY=1', () => {
    process.env['NODE_ENV'] = 'production';
    process.env['PLATFORM_ALLOW_MOCK_PAY'] = '1';
    expect(isMockPayAllowed()).toBe(false);
  });

  it('rejects when flag unset and NODE_ENV is development', () => {
    process.env['NODE_ENV'] = 'development';
    expect(isMockPayAllowed()).toBe(false);
  });

  it('rejects when flag is not exactly "1"', () => {
    process.env['NODE_ENV'] = 'test';
    process.env['PLATFORM_ALLOW_MOCK_PAY'] = 'true';
    expect(isMockPayAllowed()).toBe(false);
  });

  it('allows when NODE_ENV is test and PLATFORM_ALLOW_MOCK_PAY=1', () => {
    process.env['NODE_ENV'] = 'test';
    process.env['PLATFORM_ALLOW_MOCK_PAY'] = '1';
    expect(isMockPayAllowed()).toBe(true);
  });

  it('allows when NODE_ENV is development and PLATFORM_ALLOW_MOCK_PAY=1', () => {
    process.env['NODE_ENV'] = 'development';
    process.env['PLATFORM_ALLOW_MOCK_PAY'] = '1';
    expect(isMockPayAllowed()).toBe(true);
  });

  it('allows when NODE_ENV is unset and PLATFORM_ALLOW_MOCK_PAY=1 (treated as non-production)', () => {
    delete process.env['NODE_ENV'];
    process.env['PLATFORM_ALLOW_MOCK_PAY'] = '1';
    expect(isMockPayAllowed()).toBe(true);
  });
});
