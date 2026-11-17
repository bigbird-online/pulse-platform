import { describe, it, expect, beforeEach, vi, afterEach } from 'vitest';
import { ConsumptionStore } from '../src/consumption-store.js';

describe('ConsumptionStore', () => {
  let store: ConsumptionStore;

  beforeEach(() => {
    store = new ConsumptionStore();
    vi.useFakeTimers();
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it('records a consumption event', () => {
    const rec = store.record({
      tenantId: 'tenant-1',
      amountFen: 150,
      description: '模型调用: claude-sonnet-4-7',
    });
    expect(rec.id).toMatch(/^cons_/);
    expect(rec.tenantId).toBe('tenant-1');
    expect(rec.amountFen).toBe(150);
    expect(rec.description).toContain('claude-sonnet');
    expect(rec.createdAt).toBeGreaterThan(0);
  });

  it('rejects zero or negative amounts', () => {
    expect(() => store.record({ tenantId: 't', amountFen: 0, description: '' })).toThrow();
    expect(() => store.record({ tenantId: 't', amountFen: -1, description: '' })).toThrow();
  });

  it('lists records for a tenant, newest first', () => {
    store.record({ tenantId: 't1', amountFen: 100, description: 'first' });
    vi.advanceTimersByTime(10);
    store.record({ tenantId: 't2', amountFen: 200, description: 'other tenant' });
    vi.advanceTimersByTime(10);
    store.record({ tenantId: 't1', amountFen: 300, description: 'second' });

    const result = store.list({ tenantId: 't1' });
    expect(result.total).toBe(2);
    expect(result.items[0].amountFen).toBe(300); // newest first
    expect(result.items[1].amountFen).toBe(100);
  });

  it('paginates correctly', () => {
    for (let i = 0; i < 5; i++) {
      store.record({ tenantId: 't1', amountFen: (i + 1) * 10, description: `item-${i}` });
      vi.advanceTimersByTime(1);
    }
    const page1 = store.list({ tenantId: 't1', limit: 2, offset: 0 });
    expect(page1.items.length).toBe(2);
    expect(page1.total).toBe(5);

    const page2 = store.list({ tenantId: 't1', limit: 2, offset: 2 });
    expect(page2.items.length).toBe(2);

    const page3 = store.list({ tenantId: 't1', limit: 2, offset: 4 });
    expect(page3.items.length).toBe(1);
  });

  it('filters by time range', () => {
    const r1 = store.record({ tenantId: 't1', amountFen: 10, description: 'old' });
    vi.advanceTimersByTime(1000);
    const r2 = store.record({ tenantId: 't1', amountFen: 20, description: 'new' });

    const result = store.list({
      tenantId: 't1',
      startTime: r1.createdAt + 1,
      endTime: r2.createdAt + 1,
    });
    expect(result.total).toBe(1);
    expect(result.items[0].amountFen).toBe(20);
  });

  it('computes tenant summary', () => {
    store.record({ tenantId: 't1', amountFen: 100, description: 'a' });
    store.record({ tenantId: 't1', amountFen: 200, description: 'b' });
    store.record({ tenantId: 't2', amountFen: 999, description: 'other' });

    const summary = store.getSummary('t1');
    expect(summary.totalConsumedFen).toBe(300);
    expect(summary.count).toBe(2);
    expect(summary.avgAmountFen).toBe(150);
  });

  it('returns empty for unknown tenant', () => {
    const result = store.list({ tenantId: 'unknown' });
    expect(result.total).toBe(0);
    expect(result.items).toEqual([]);
  });

  it('clamps limit and offset', () => {
    store.record({ tenantId: 't1', amountFen: 10, description: 'a' });
    const result = store.list({ tenantId: 't1', limit: -5, offset: -1 });
    expect(result.items.length).toBe(1);
    expect(result.limit).toBe(1); // clamped to min 1
    expect(result.offset).toBe(0);
  });
});
