import { describe, it, expect } from 'vitest';
import { shouldStartScheduler, startRelay } from '../src/index.js';

describe('startRelay', () => {
  it('starts scheduler observability for Feishu and local-only deployments', () => {
    expect(shouldStartScheduler(0, 1, false)).toBe(true);
    expect(shouldStartScheduler(1, 0, false)).toBe(true);
    expect(shouldStartScheduler(0, 0, true)).toBe(true);
    expect(shouldStartScheduler(0, 0, false, true)).toBe(true);
    expect(shouldStartScheduler(0, 0, false)).toBe(false);
  });

  // [P0-005] Unit Test-028
  it('throws when relay.json path is invalid', async () => {
    await expect(startRelay({ configPath: '/nonexistent/path.json' }))
      .rejects.toThrow();
  });
});
