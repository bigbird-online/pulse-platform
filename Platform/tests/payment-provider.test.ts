import { describe, it, expect } from 'vitest';
import {
  UnconfiguredPaymentProvider,
} from '../src/payment-provider.js';

describe('payment-provider [P1-001]', () => {
  it('UnconfiguredPaymentProvider ignores webhook', async () => {
    const p = new UnconfiguredPaymentProvider();
    const r = await p.verifyNotification({ headers: {}, rawBody: '{}' });
    expect(r.kind).toBe('ignored');
  });
});
