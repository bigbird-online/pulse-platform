// Stage A (FR-P0-014) HTTP-level regression: hitting `/internal/mock-pay`
// must trigger tenant workspace materialisation through the OrderStore.onPaid
// listener wired in `createHttpServer`. Covers AC-P0-008 surface.

import { describe, it, expect, beforeEach, afterEach } from 'vitest';
import type { Server } from 'node:http';
import { mkdtempSync, mkdirSync, readFileSync, writeFileSync, existsSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { createHttpServer } from '../src/http-server.js';
import { OrderStore } from '../src/order-store.js';
import { PhoneAllowlist, normalizeCnPhone } from '../src/phone-allowlist.js';

function mkTmp(prefix: string): string {
  return mkdtempSync(join(tmpdir(), prefix));
}

function makeFakeTemplate(root: string): void {
  mkdirSync(root, { recursive: true });
  writeFileSync(
    join(root, 'CLAUDE.md'),
    '# {{agentName}}\n你是租户 {{tenantId}} 的 pulse 管理员（{{agentId}}）。\n',
    'utf8',
  );
  writeFileSync(join(root, 'README.md'), '# template README — DO NOT copy\n', 'utf8');
}

describe('Stage A materialisation via HTTP /internal/mock-pay [AC-P0-008]', () => {
  const origEnv = process.env;
  let server: Server;
  let baseUrl: string;
  let store: OrderStore;
  let tenantsRoot: string;
  let templateRoot: string;

  beforeEach(async () => {
    process.env = { ...origEnv };
    process.env['NODE_ENV'] = 'test';
    process.env['PLATFORM_ALLOW_MOCK_PAY'] = '1';
    store = new OrderStore();
    tenantsRoot = mkTmp('tenants-');
    templateRoot = mkTmp('tpl-');
    makeFakeTemplate(templateRoot);
    server = createHttpServer({
      orderStore: store,
      // Inject a tightly scoped allowlist so we don't depend on env files.
      allowlist: new PhoneAllowlist(new Set([normalizeCnPhone('13800138000')])),
      tenantsRoot,
      tenantTemplateRoot: templateRoot,
      // Avoid relay reload side-effects in this isolated test.
      skipRelayReload: true,
    });
    await new Promise<void>((resolve, reject) => {
      server.listen(0, '127.0.0.1', () => resolve());
      server.on('error', reject);
    });
    const addr = server.address();
    if (addr === null || typeof addr === 'string') throw new Error('addr');
    baseUrl = `http://127.0.0.1:${addr.port}`;
  });

  afterEach(async () => {
    process.env = origEnv;
    await new Promise<void>((resolve, reject) => {
      server.close((err) => (err ? reject(err) : resolve()));
    });
  });

  it('mock-pay → tenantId allocated + template materialised + idempotent', async () => {
    const id = store.createPending({ phone: '13800138000', displayName: 'Eric' });

    const res = await fetch(`${baseUrl}/internal/mock-pay`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ orderId: id }),
    });
    expect(res.status).toBe(200);

    // The onPaid listener runs synchronously inside markPaid → response time.
    const order = store.get(id);
    expect(order?.status).toBe('paid');
    expect(order?.tenantId).toBe('member-0001');

    const claudePath = join(tenantsRoot, 'member-0001', 'CLAUDE.md');
    expect(existsSync(claudePath)).toBe(true);
    const claude = readFileSync(claudePath, 'utf8');
    expect(claude).toContain('Eric的pulse管理员');
    expect(claude).toContain('租户 member-0001 的 pulse 管理员');
    expect(claude).toContain('member-0001-pulse');
    expect(claude).not.toMatch(/\{\{[a-zA-Z]+\}\}/);

    expect(existsSync(join(tenantsRoot, 'member-0001', 'README.md'))).toBe(false);

    // User-edited file survives a webhook-style replay.
    writeFileSync(claudePath, 'USER EDIT — keep me\n', 'utf8');
    const res2 = await fetch(`${baseUrl}/internal/mock-pay`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ orderId: id }),
    });
    expect(res2.status).toBe(200);
    const body2 = (await res2.json()) as { already: boolean };
    expect(body2.already).toBe(true);
    expect(readFileSync(claudePath, 'utf8')).toBe('USER EDIT — keep me\n');
  });

  it('handles two paid orders with sequential tenantIds', async () => {
    const a = store.createPending({ phone: '13800138000', displayName: 'Alice' });
    const b = store.createPending({ phone: '13800138000', displayName: 'Bob' });
    for (const id of [a, b]) {
      const r = await fetch(`${baseUrl}/internal/mock-pay`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ orderId: id }),
      });
      expect(r.status).toBe(200);
    }
    expect(store.get(a)?.tenantId).toBe('member-0001');
    expect(store.get(b)?.tenantId).toBe('member-0002');
    expect(existsSync(join(tenantsRoot, 'member-0001', 'CLAUDE.md'))).toBe(true);
    expect(existsSync(join(tenantsRoot, 'member-0002', 'CLAUDE.md'))).toBe(true);

    const claudeA = readFileSync(join(tenantsRoot, 'member-0001', 'CLAUDE.md'), 'utf8');
    const claudeB = readFileSync(join(tenantsRoot, 'member-0002', 'CLAUDE.md'), 'utf8');
    expect(claudeA).toContain('Alice的pulse管理员');
    expect(claudeB).toContain('Bob的pulse管理员');
  });

  it('does NOT materialise when enableStageAMaterialize=false (test isolation)', async () => {
    // Re-create server with Stage A disabled.
    await new Promise<void>((resolve, reject) => {
      server.close((err) => (err ? reject(err) : resolve()));
    });
    const isolatedStore = new OrderStore();
    server = createHttpServer({
      orderStore: isolatedStore,
      allowlist: new PhoneAllowlist(new Set([normalizeCnPhone('13800138000')])),
      tenantsRoot,
      tenantTemplateRoot: templateRoot,
      skipRelayReload: true,
      enableStageAMaterialize: false,
    });
    await new Promise<void>((resolve, reject) => {
      server.listen(0, '127.0.0.1', () => resolve());
      server.on('error', reject);
    });
    const addr = server.address();
    if (addr === null || typeof addr === 'string') throw new Error('addr');
    baseUrl = `http://127.0.0.1:${addr.port}`;

    const id = isolatedStore.createPending({ phone: '13800138000', displayName: 'Carol' });
    const res = await fetch(`${baseUrl}/internal/mock-pay`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ orderId: id }),
    });
    expect(res.status).toBe(200);
    expect(isolatedStore.get(id)?.tenantId).toBeUndefined();
    // Workspace dir should not exist (no materialisation triggered).
    expect(existsSync(join(tenantsRoot, 'member-0001'))).toBe(false);
  });
});
