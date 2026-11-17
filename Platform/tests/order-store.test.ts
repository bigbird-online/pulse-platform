import { describe, it, expect } from 'vitest';
import { OrderStore } from '../src/order-store.js';

describe('OrderStore [P0-002]', () => {
  it('createPending assigns pending status', () => {
    const s = new OrderStore();
    const id = s.createPending({ phone: '13800138000' });
    expect(id).toMatch(/^ord_[0-9a-z]+_[0-9a-f]{6}$/);
    expect(id.length).toBeGreaterThanOrEqual(6);
    const o = s.get(id);
    expect(o?.status).toBe('pending');
    expect(o?.phone).toBe('13800138000');
  });

  it('markPaid transitions pending to paid', () => {
    const s = new OrderStore();
    const id = s.createPending({ phone: '13800138000' });
    const r = s.markPaid(id);
    expect(r).toEqual({ success: true, already: false });
    expect(s.get(id)?.status).toBe('paid');
    expect(s.get(id)?.paidAt).toBeTypeOf('number');
  });

  it('markPaid is idempotent for already paid', () => {
    const s = new OrderStore();
    const id = s.createPending({ phone: '13800138000' });
    s.markPaid(id);
    const r = s.markPaid(id);
    expect(r).toEqual({ success: true, already: true });
  });

  it('markPaid returns not_found for unknown id', () => {
    const s = new OrderStore();
    expect(s.markPaid('ord_missing')).toEqual({ success: false, error: 'not_found' });
  });

  it('markPaid returns invalid_state for failed order', () => {
    const s = new OrderStore();
    const id = s.createPending({ phone: '13800138000' });
    s.markFailed(id);
    expect(s.markPaid(id)).toEqual({
      success: false,
      error: 'invalid_state',
      currentStatus: 'failed',
    });
  });

  it('markRefunded blocks markPaid', () => {
    const s = new OrderStore();
    const id = s.createPending({ phone: '13800138000' });
    s.markRefunded(id);
    expect(s.markPaid(id)).toEqual({
      success: false,
      error: 'invalid_state',
      currentStatus: 'refunded',
    });
  });

  it('markProvisioned records timestamp', () => {
    const s = new OrderStore();
    const id = s.createPending({ phone: '13800138000' });
    expect(s.isProvisioned(id)).toBe(false);
    s.markProvisioned(id);
    expect(s.isProvisioned(id)).toBe(true);
  });

  it('captures displayName when provided (v0.3.0)', () => {
    const s = new OrderStore();
    const id = s.createPending({ phone: '13800138000', displayName: '聪聪' });
    expect(s.get(id)?.displayName).toBe('聪聪');
  });

  it('trims/ignores empty displayName', () => {
    const s = new OrderStore();
    const id = s.createPending({ phone: '13800138000', displayName: '   ' });
    expect(s.get(id)?.displayName).toBeUndefined();
  });

  it('setTenantId stores tenant binding for later pair (v0.3.0)', () => {
    const s = new OrderStore();
    const id = s.createPending({ phone: '13800138000', displayName: 'Eric' });
    s.setTenantId(id, 'member-0042');
    expect(s.get(id)?.tenantId).toBe('member-0042');
  });
});
