import { describe, it, expect, beforeEach } from 'vitest';
import { ConsumptionStore } from '../src/consumption-store.js';

describe('ConsumptionStore', () => {
  let store: ConsumptionStore;

  beforeEach(() => {
    store = new ConsumptionStore();
  });

  describe('record', () => {
    it('should record a consumption with generated id and timestamp', () => {
      const record = store.record({
        tenantId: 'tenant-001',
        amountFen: 100,
        description: '模型调用',
      });

      expect(record.id).toMatch(/^cons_[a-z0-9]+_[a-f0-9]{4}$/);
      expect(record.tenantId).toBe('tenant-001');
      expect(record.amountFen).toBe(100);
      expect(record.description).toBe('模型调用');
      expect(record.createdAt).toBeGreaterThan(0);
    });

    it('should include metadata when provided', () => {
      const record = store.record({
        tenantId: 'tenant-001',
        amountFen: 200,
        description: '模型调用',
        metadata: { modelId: 'claude-opus', tokensIn: 1000, tokensOut: 500 },
      });

      expect(record.metadata).toEqual({ modelId: 'claude-opus', tokensIn: 1000, tokensOut: 500 });
    });

    it('should throw for non-positive amount', () => {
      expect(() => store.record({ tenantId: 't1', amountFen: 0, description: 'test' }))
        .toThrow('amountFen must be positive');
      expect(() => store.record({ tenantId: 't1', amountFen: -10, description: 'test' }))
        .toThrow('amountFen must be positive');
    });
  });

  describe('list', () => {
    it('should return empty list for new tenant', () => {
      const result = store.list({ tenantId: 'tenant-001' });
      expect(result.items).toHaveLength(0);
      expect(result.total).toBe(0);
    });

    it('should list records in descending order by time', async () => {
      store.record({ tenantId: 'tenant-001', amountFen: 100, description: 'first' });
      await new Promise(r => setTimeout(r, 5)); // Ensure different timestamps
      store.record({ tenantId: 'tenant-001', amountFen: 200, description: 'second' });
      await new Promise(r => setTimeout(r, 5));
      store.record({ tenantId: 'tenant-001', amountFen: 300, description: 'third' });

      const result = store.list({ tenantId: 'tenant-001' });
      expect(result.items).toHaveLength(3);
      // Newest first (300 was recorded last)
      expect(result.items[0].amountFen).toBe(300);
      expect(result.items[1].amountFen).toBe(200);
      expect(result.items[2].amountFen).toBe(100);
    });

    it('should support pagination', () => {
      for (let i = 1; i <= 5; i++) {
        store.record({ tenantId: 'tenant-001', amountFen: i * 100, description: `item-${i}` });
      }

      const page1 = store.list({ tenantId: 'tenant-001', limit: 2, offset: 0 });
      expect(page1.items).toHaveLength(2);
      expect(page1.total).toBe(5);

      const page2 = store.list({ tenantId: 'tenant-001', limit: 2, offset: 2 });
      expect(page2.items).toHaveLength(2);

      const page3 = store.list({ tenantId: 'tenant-001', limit: 2, offset: 4 });
      expect(page3.items).toHaveLength(1);
    });

    it('should isolate tenants', () => {
      store.record({ tenantId: 'tenant-a', amountFen: 100, description: 'a' });
      store.record({ tenantId: 'tenant-b', amountFen: 200, description: 'b' });

      const resultA = store.list({ tenantId: 'tenant-a' });
      const resultB = store.list({ tenantId: 'tenant-b' });

      expect(resultA.items).toHaveLength(1);
      expect(resultA.items[0].amountFen).toBe(100);
      expect(resultB.items).toHaveLength(1);
      expect(resultB.items[0].amountFen).toBe(200);
    });

    it('should filter by time range', async () => {
      store.record({ tenantId: 'tenant-001', amountFen: 100, description: 'old' });

      // Wait to ensure different timestamps
      await new Promise(r => setTimeout(r, 10));
      const mid = Date.now();
      await new Promise(r => setTimeout(r, 10));

      store.record({ tenantId: 'tenant-001', amountFen: 200, description: 'mid' });
      store.record({ tenantId: 'tenant-001', amountFen: 300, description: 'new' });

      const filtered = store.list({ tenantId: 'tenant-001', startTime: mid });
      expect(filtered.items.length).toBeGreaterThanOrEqual(1);
      // Should only include records created after mid
      expect(filtered.items.every(r => r.createdAt >= mid)).toBe(true);
    });
  });

  describe('getSummary', () => {
    it('should return zero summary for unknown tenant', () => {
      const summary = store.getSummary('tenant-999');
      expect(summary.totalConsumedFen).toBe(0);
      expect(summary.count).toBe(0);
      expect(summary.avgAmountFen).toBe(0);
    });

    it('should calculate correct summary', () => {
      store.record({ tenantId: 'tenant-001', amountFen: 100, description: 'a' });
      store.record({ tenantId: 'tenant-001', amountFen: 200, description: 'b' });
      store.record({ tenantId: 'tenant-001', amountFen: 300, description: 'c' });

      const summary = store.getSummary('tenant-001');
      expect(summary.tenantId).toBe('tenant-001');
      expect(summary.totalConsumedFen).toBe(600);
      expect(summary.count).toBe(3);
      expect(summary.avgAmountFen).toBe(200);
      expect(summary.lastConsumptionAt).toBeGreaterThan(0);
    });
  });

  describe('getGlobalSummary', () => {
    it('should return zeros for empty store', () => {
      const global = store.getGlobalSummary();
      expect(global.totalConsumedFen).toBe(0);
      expect(global.totalTransactions).toBe(0);
      expect(global.tenantCount).toBe(0);
    });

    it('should aggregate across all tenants', () => {
      store.record({ tenantId: 'tenant-a', amountFen: 100, description: 'a1' });
      store.record({ tenantId: 'tenant-a', amountFen: 200, description: 'a2' });
      store.record({ tenantId: 'tenant-b', amountFen: 300, description: 'b1' });

      const global = store.getGlobalSummary();
      expect(global.totalConsumedFen).toBe(600);
      expect(global.totalTransactions).toBe(3);
      expect(global.tenantCount).toBe(2);
    });
  });

  describe('listAll', () => {
    it('should list all records for admin', () => {
      store.record({ tenantId: 'tenant-a', amountFen: 100, description: 'a' });
      store.record({ tenantId: 'tenant-b', amountFen: 200, description: 'b' });

      const all = store.listAll();
      expect(all.items).toHaveLength(2);
      expect(all.total).toBe(2);
    });
  });
});
