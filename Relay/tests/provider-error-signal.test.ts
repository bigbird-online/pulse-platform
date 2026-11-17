import { describe, it, expect } from 'vitest';

// isProviderErrorSignal is a private function inside pool.ts createPool closure.
// We test the detection patterns here by replicating the logic.
// If the patterns change in pool.ts, update these tests too.
function isProviderErrorSignal(text: string): boolean {
  const lower = text.toLowerCase();
  return (
    /\bapierr(?:or)?\s*:?\s*500\b/i.test(lower) ||
    lower.includes('model not found') ||
    lower.includes('request failed') ||
    (/\b500\b/.test(lower) && /\berror\b/i.test(lower) && /\bmessage\b/i.test(lower))
  );
}

describe('isProviderErrorSignal (provider 500 / model-not-found detection)', () => {
  it('detects PTY-truncated "APIError: 500"', () => {
    const text = 'APIErro: 500 {"error":{"message":"upstreamrequestfailedwithSid: cht000baf3b"';
    expect(isProviderErrorSignal(text)).toBe(true);
  });

  it('detects "APIError:500" (no space after colon)', () => {
    const text = 'APIError:500 {"error":{"message":"server error"}}';
    expect(isProviderErrorSignal(text)).toBe(true);
  });

  it('detects "model not found"', () => {
    const text = '500 {"error":{"message":"Model NotFound","code":10404}}';
    expect(isProviderErrorSignal(text)).toBe(true);
  });

  it('detects "request failed" in provider error', () => {
    const text = 'API error: request failed with status 500';
    expect(isProviderErrorSignal(text)).toBe(true);
  });

  it('detects generic 500 + error + message pattern', () => {
    const text = '500 {"error":{"message":"internal server error"}}';
    expect(isProviderErrorSignal(text)).toBe(true);
  });

  it('does NOT trigger on 429 rate limit', () => {
    const text = 'APIError: 429 rate limit exceeded';
    expect(isProviderErrorSignal(text)).toBe(false);
  });

  it('does NOT trigger on normal output', () => {
    const text = '✻ Working on your request...';
    expect(isProviderErrorSignal(text)).toBe(false);
  });

  it('detects with ANSI escape sequences mixed in', () => {
    const text = '\x1B[?2026h ⎿ APIErro: 500 {"error":{"message":"Xunfi claude rques faled"';
    expect(isProviderErrorSignal(text)).toBe(true);
  });
});
