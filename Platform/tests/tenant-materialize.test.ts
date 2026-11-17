import { describe, expect, it } from 'vitest';
import { mkdtempSync, mkdirSync, readFileSync, writeFileSync, existsSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { OrderStore } from '../src/order-store.js';
import { TenantIdAllocator } from '../src/tenant-id-allocator.js';
import { materializeTenantSpaceForOrder } from '../src/tenant-materialize.js';

function mkTmp(prefix: string): string {
  return mkdtempSync(join(tmpdir(), prefix));
}

function makeFakeTemplate(root: string): void {
  mkdirSync(root, { recursive: true });
  writeFileSync(
    join(root, 'CLAUDE.md'),
    '# {{agentName}}\n你是租户 {{tenantId}} 的 pulse 管理员（{{agentId}}）。\nDisplay: {{displayName}}\nAt: {{provisionedAtIso}}\n',
    'utf8',
  );
  writeFileSync(join(root, 'README.md'), '# template README — DO NOT copy\n', 'utf8');
}

describe('materializeTenantSpaceForOrder (Stage A · FR-P0-014)', () => {
  it('allocates tenantId, materialises template, and is idempotent on retry', () => {
    const tplRoot = mkTmp('tpl-');
    const tenantsRoot = mkTmp('tenants-');
    makeFakeTemplate(tplRoot);

    const store = new OrderStore();
    const orderId = store.createPending({ phone: '13800138000', displayName: 'Eric' });
    store.markPaid(orderId);
    const allocator = new TenantIdAllocator(0);

    const r1 = materializeTenantSpaceForOrder(
      { orderStore: store, tenantIdAllocator: allocator, tenantsRoot, templateRoot: tplRoot, nowIso: '2026-05-08T10:00:00+0800' },
      orderId,
    );
    expect(r1.ok).toBe(true);
    if (!r1.ok) return;
    expect(r1.tenantId).toBe('member-0001');
    expect(r1.allocated).toBe(true);
    expect(store.get(orderId)?.tenantId).toBe('member-0001');

    const tenantDir = join(tenantsRoot, 'member-0001');
    const claudePath = join(tenantDir, 'CLAUDE.md');
    expect(existsSync(claudePath)).toBe(true);
    expect(existsSync(join(tenantDir, 'README.md'))).toBe(false);
    const claude = readFileSync(claudePath, 'utf8');
    expect(claude).toContain('Eric的pulse管理员');
    expect(claude).toContain('租户 member-0001 的 pulse 管理员');
    expect(claude).toContain('member-0001-pulse');
    expect(claude).toContain('Display: Eric');
    expect(claude).toContain('At: 2026-05-08T10:00:00+0800');

    // Second call (e.g. webhook replay) should be a no-op: same tenantId, no allocation, no overwrite.
    const r2 = materializeTenantSpaceForOrder(
      { orderStore: store, tenantIdAllocator: allocator, tenantsRoot, templateRoot: tplRoot },
      orderId,
    );
    expect(r2.ok).toBe(true);
    if (!r2.ok) return;
    expect(r2.tenantId).toBe('member-0001');
    expect(r2.allocated).toBe(false);
    expect(r2.copied).toEqual([]);
    expect(r2.skipped).toContain('CLAUDE.md');
  });

  it('preserves user-edited files on retry', () => {
    const tplRoot = mkTmp('tpl-');
    const tenantsRoot = mkTmp('tenants-');
    makeFakeTemplate(tplRoot);

    const store = new OrderStore();
    const orderId = store.createPending({ phone: '13800138000', displayName: 'Maxwell' });
    store.markPaid(orderId);
    const allocator = new TenantIdAllocator(0);

    materializeTenantSpaceForOrder(
      { orderStore: store, tenantIdAllocator: allocator, tenantsRoot, templateRoot: tplRoot },
      orderId,
    );

    const tenantDir = join(tenantsRoot, 'member-0001');
    const claudePath = join(tenantDir, 'CLAUDE.md');
    writeFileSync(claudePath, 'USER EDIT — keep me\n', 'utf8');

    materializeTenantSpaceForOrder(
      { orderStore: store, tenantIdAllocator: allocator, tenantsRoot, templateRoot: tplRoot },
      orderId,
    );

    expect(readFileSync(claudePath, 'utf8')).toBe('USER EDIT — keep me\n');
  });

  it('rejects when order not paid yet', () => {
    const tplRoot = mkTmp('tpl-');
    const tenantsRoot = mkTmp('tenants-');
    makeFakeTemplate(tplRoot);

    const store = new OrderStore();
    const orderId = store.createPending({ phone: '13800138000' });
    const allocator = new TenantIdAllocator(0);

    const r = materializeTenantSpaceForOrder(
      { orderStore: store, tenantIdAllocator: allocator, tenantsRoot, templateRoot: tplRoot },
      orderId,
    );
    expect(r.ok).toBe(false);
    if (r.ok) return;
    expect(r.error).toBe('not_paid');
  });

  it('returns order_not_found for unknown id', () => {
    const tplRoot = mkTmp('tpl-');
    const tenantsRoot = mkTmp('tenants-');
    makeFakeTemplate(tplRoot);

    const store = new OrderStore();
    const allocator = new TenantIdAllocator(0);
    const r = materializeTenantSpaceForOrder(
      { orderStore: store, tenantIdAllocator: allocator, tenantsRoot, templateRoot: tplRoot },
      'ord_999999',
    );
    expect(r.ok).toBe(false);
    if (r.ok) return;
    expect(r.error).toBe('order_not_found');
  });

  it('reports tenants_root_unset when not configured', () => {
    const tplRoot = mkTmp('tpl-');
    makeFakeTemplate(tplRoot);

    const store = new OrderStore();
    const orderId = store.createPending({ phone: '13800138000' });
    store.markPaid(orderId);
    const allocator = new TenantIdAllocator(0);

    const r = materializeTenantSpaceForOrder(
      { orderStore: store, tenantIdAllocator: allocator, tenantsRoot: '', templateRoot: tplRoot },
      orderId,
    );
    expect(r.ok).toBe(false);
    if (r.ok) return;
    expect(r.error).toBe('tenants_root_unset');
  });

  it('reports template_missing when template root does not exist', () => {
    const tenantsRoot = mkTmp('tenants-');

    const store = new OrderStore();
    const orderId = store.createPending({ phone: '13800138000' });
    store.markPaid(orderId);
    const allocator = new TenantIdAllocator(0);

    const r = materializeTenantSpaceForOrder(
      { orderStore: store, tenantIdAllocator: allocator, tenantsRoot, templateRoot: join(tmpdir(), 'no-such-tpl-' + Date.now()) },
      orderId,
    );
    expect(r.ok).toBe(false);
    if (r.ok) return;
    expect(r.error).toBe('template_missing');
  });

  it('uses default agentName "我的pulse管理员" when displayName is missing', () => {
    const tplRoot = mkTmp('tpl-');
    const tenantsRoot = mkTmp('tenants-');
    makeFakeTemplate(tplRoot);

    const store = new OrderStore();
    const orderId = store.createPending({ phone: '13800138000' }); // no displayName
    store.markPaid(orderId);
    const allocator = new TenantIdAllocator(0);

    const r = materializeTenantSpaceForOrder(
      { orderStore: store, tenantIdAllocator: allocator, tenantsRoot, templateRoot: tplRoot },
      orderId,
    );
    expect(r.ok).toBe(true);
    if (!r.ok) return;
    const claude = readFileSync(join(r.workspaceAbs, 'CLAUDE.md'), 'utf8');
    expect(claude).toContain('我的pulse管理员');
    expect(claude).toContain('Display: 我');
  });
});

describe('OrderStore.onPaid hook', () => {
  it('fires once on fresh markPaid (already=false)', () => {
    const store = new OrderStore();
    const events: Array<{ orderId: string; already: boolean }> = [];
    store.onPaid((orderId, info) => events.push({ orderId, ...info }));
    const id = store.createPending({ phone: '13800138000' });
    store.markPaid(id);
    expect(events).toEqual([{ orderId: id, already: false }]);
  });

  it('fires again on idempotent retry (already=true)', () => {
    const store = new OrderStore();
    const events: Array<{ orderId: string; already: boolean }> = [];
    store.onPaid((orderId, info) => events.push({ orderId, ...info }));
    const id = store.createPending({ phone: '13800138000' });
    store.markPaid(id);
    store.markPaid(id);
    expect(events).toEqual([
      { orderId: id, already: false },
      { orderId: id, already: true },
    ]);
  });

  it('does not fire for invalid_state (refunded → markPaid)', () => {
    const store = new OrderStore();
    const events: string[] = [];
    store.onPaid((orderId) => events.push(orderId));
    const id = store.createPending({ phone: '13800138000' });
    store.markRefunded(id);
    store.markPaid(id);
    expect(events).toEqual([]);
  });

  it('listener errors are swallowed (markPaid still succeeds)', () => {
    const store = new OrderStore();
    store.onPaid(() => {
      throw new Error('boom');
    });
    const id = store.createPending({ phone: '13800138000' });
    expect(() => store.markPaid(id)).not.toThrow();
    expect(store.get(id)?.status).toBe('paid');
  });

  it('unsubscribe stops further notifications', () => {
    const store = new OrderStore();
    const events: string[] = [];
    const off = store.onPaid((orderId) => events.push(orderId));
    const id = store.createPending({ phone: '13800138000' });
    store.markPaid(id);
    off();
    store.markPaid(id);
    expect(events).toEqual([id]);
  });
});
