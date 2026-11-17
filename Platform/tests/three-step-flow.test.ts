import { describe, it, expect } from 'vitest';
import { mkdtemp, writeFile, readFile } from 'node:fs/promises';
import { join } from 'node:path';
import { tmpdir } from 'node:os';
import { OrderStore } from '../src/order-store.js';
import { PhoneAllowlist } from '../src/phone-allowlist.js';
import { TenantIdAllocator } from '../src/tenant-id-allocator.js';
import { pairFeishuStep3, provisionFeishuStep2 } from '../src/provision.js';

const baseRelay = `{
  "bindings": [],
  "users": []
}\n`;

async function fixture(): Promise<{
  relayConfigPath: string;
  tenantsRoot: string;
  orderStore: OrderStore;
  allowlist: PhoneAllowlist;
  tenantIdAllocator: TenantIdAllocator;
}> {
  const dir = await mkdtemp(join(tmpdir(), 'prv3-'));
  const relayConfigPath = join(dir, 'relay.json');
  await writeFile(relayConfigPath, baseRelay, 'utf8');
  const tenantsRoot = join(dir, 'Tenants');
  const orderStore = new OrderStore();
  const allowlist = new PhoneAllowlist(new Set(['+8613800138000']));
  const tenantIdAllocator = new TenantIdAllocator(0);
  return { relayConfigPath, tenantsRoot, orderStore, allowlist, tenantIdAllocator };
}

async function paidOrder(
  s: OrderStore,
  opts?: { displayName?: string },
): Promise<string> {
  const id = s.createPending({ phone: '13800138000', displayName: opts?.displayName });
  s.markPaid(id);
  return id;
}

describe('Step 2 — provisionFeishuStep2', () => {
  it('FR-P0-010: derives all internal names from displayName + tenant seq', async () => {
    const f = await fixture();
    const orderId = await paidOrder(f.orderStore, { displayName: '聪聪' });

    const r = await provisionFeishuStep2(
      {
        ...f,
        verifyFeishuCredentials: async () => ({ ok: true, tenantAccessToken: 't', expire: 7200 }),
        skipReload: true,
      },
      { orderId, feishuAppId: 'cli_a', feishuAppSecret: 's' },
    );

    expect(r.ok).toBe(true);
    if (!r.ok) return;
    expect(r.tenantId).toBe('member-0001');
    expect(r.principalId).toBe('member-0001');
    expect(r.agentId).toBe('member-0001-pulse');
    expect(r.bindingId).toBe('bind-member-0001');
    expect(r.agentName).toBe('聪聪的pulse管理员');
    expect(f.orderStore.get(orderId)?.tenantId).toBe('member-0001');
  });

  it('Step 2 writes binding with status=pending and empty open_id', async () => {
    const f = await fixture();
    const orderId = await paidOrder(f.orderStore, { displayName: 'Eric' });

    await provisionFeishuStep2(
      {
        ...f,
        verifyFeishuCredentials: async () => ({ ok: true, tenantAccessToken: 't', expire: 100 }),
        skipReload: true,
      },
      { orderId, feishuAppId: 'cli_a', feishuAppSecret: 's' },
    );

    const doc = JSON.parse(await readFile(f.relayConfigPath, 'utf8')) as {
      bindings: { status: string; address: { value: string; appId: string } }[];
      users: { agents: { id: string; name: string; emoji: string; feishu: { appId: string } }[] }[];
    };
    expect(doc.bindings).toHaveLength(1);
    expect(doc.bindings[0].status).toBe('pending');
    expect(doc.bindings[0].address.value).toBe('');
    expect(doc.bindings[0].address.appId).toBe('cli_a');
    expect(doc.users[0].agents[0]).toMatchObject({
      id: 'member-0001-pulse',
      name: 'Eric的pulse管理员',
      emoji: '🐦',
      feishu: { appId: 'cli_a' },
    });
  });

  it('AC-P0-005: invalid feishu credentials surface code/msg verbatim', async () => {
    const f = await fixture();
    const orderId = await paidOrder(f.orderStore, { displayName: '聪聪' });
    const r = await provisionFeishuStep2(
      {
        ...f,
        verifyFeishuCredentials: async () => ({
          ok: false,
          error: 'invalid_credentials',
          code: 99991663,
          msg: 'app_secret invalid, please check it.',
        }),
        skipReload: true,
      },
      { orderId, feishuAppId: 'cli_a', feishuAppSecret: 'wrong' },
    );
    expect(r).toEqual({
      ok: false,
      error: 'invalid_feishu_credentials',
      feishuCode: 99991663,
      feishuMsg: 'app_secret invalid, please check it.',
    });
    const txt = await readFile(f.relayConfigPath, 'utf8');
    expect(txt).not.toContain('cli_a');
  });

  it('rejects unpaid orders', async () => {
    const f = await fixture();
    const id = f.orderStore.createPending({ phone: '13800138000', displayName: 'X' });
    const r = await provisionFeishuStep2(
      { ...f, skipReload: true, verifyFeishuCredentials: async () => ({ ok: true, tenantAccessToken: 't', expire: 0 }) },
      { orderId: id, feishuAppId: 'cli_a', feishuAppSecret: 's' },
    );
    expect(r).toEqual({ ok: false, error: 'not_paid' });
  });

  it('is idempotent — second call returns already=true with same names', async () => {
    const f = await fixture();
    const orderId = await paidOrder(f.orderStore, { displayName: '聪聪' });
    const opts = {
      ...f,
      verifyFeishuCredentials: async () => ({ ok: true as const, tenantAccessToken: 't', expire: 100 }),
      skipReload: true,
    };
    const a = await provisionFeishuStep2(opts, { orderId, feishuAppId: 'cli_a', feishuAppSecret: 's' });
    const b = await provisionFeishuStep2(opts, { orderId, feishuAppId: 'cli_a', feishuAppSecret: 's' });
    expect(a.ok).toBe(true);
    expect(b.ok).toBe(true);
    if (a.ok && b.ok) {
      expect(b.already).toBe(true);
      expect(b.tenantId).toBe(a.tenantId);
    }
  });

  it('AC-P0-006: triggers Relay HTTP reload when relayHttpUrl is set', async () => {
    const f = await fixture();
    const orderId = await paidOrder(f.orderStore, { displayName: 'Eric' });
    let reloadCalled = false;
    const fetchImpl: typeof fetch = (async () => {
      reloadCalled = true;
      return new Response(JSON.stringify({ ok: true, added: ['member-0001-pulse'], total: 1 }), {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      });
    }) as unknown as typeof fetch;
    await provisionFeishuStep2(
      {
        ...f,
        verifyFeishuCredentials: async () => ({ ok: true, tenantAccessToken: 't', expire: 100 }),
        relayHttpUrl: 'http://127.0.0.1:65535',
        fetchImpl,
      },
      { orderId, feishuAppId: 'cli_a', feishuAppSecret: 's' },
    );
    expect(reloadCalled).toBe(true);
  });
});

describe('Step 3 — pairFeishuStep3', () => {
  it('forwards to Relay using derived agentId/principalId from order.tenantId', async () => {
    const f = await fixture();
    const orderId = await paidOrder(f.orderStore, { displayName: '聪聪' });
    f.orderStore.setTenantId(orderId, 'member-0042');
    f.orderStore.markProvisioned(orderId);

    let captured: { code: string; principalId: string; agentId: string } | null = null;
    const r = await pairFeishuStep3(
      {
        orderStore: f.orderStore,
        relayHttpUrl: 'http://127.0.0.1:0',
        approveFn: async (_opts, body) => {
          captured = body;
          return { ok: true, openId: 'ou_xx', agentId: body.agentId, principalId: body.principalId };
        },
      },
      { orderId, code: 'AB1234' },
    );
    expect(r.ok).toBe(true);
    if (r.ok) {
      expect(r.tenantId).toBe('member-0042');
      expect(r.agentId).toBe('member-0042-pulse');
      expect(r.openId).toBe('ou_xx');
    }
    expect(captured).toEqual({ code: 'AB1234', principalId: 'member-0042', agentId: 'member-0042-pulse' });
  });

  it('returns not_provisioned for orders that have not completed Step 2', async () => {
    const f = await fixture();
    const orderId = await paidOrder(f.orderStore, { displayName: 'X' });
    const r = await pairFeishuStep3(
      { orderStore: f.orderStore, relayHttpUrl: 'http://127.0.0.1:0', approveFn: async () => ({ ok: false, error: 'code_unknown' }) },
      { orderId, code: 'AB1234' },
    );
    expect(r).toEqual({ ok: false, error: 'not_provisioned' });
  });

  it('classifies Relay error responses', async () => {
    const f = await fixture();
    const orderId = await paidOrder(f.orderStore, { displayName: 'X' });
    f.orderStore.setTenantId(orderId, 'member-0001');
    f.orderStore.markProvisioned(orderId);

    const cases: Array<[
      Awaited<ReturnType<NonNullable<Parameters<typeof pairFeishuStep3>[0]['approveFn']>>>,
      string,
    ]> = [
      [{ ok: false, error: 'code_unknown' }, 'pair_code_unknown'],
      [{ ok: false, error: 'agent_mismatch', expectedAgentId: 'other' }, 'pair_agent_mismatch'],
      [{ ok: false, error: 'config_missing_agent' }, 'pair_config_missing_agent'],
      [{ ok: false, error: 'relay_unreachable', detail: 'ECONNREFUSED' }, 'pair_relay_unreachable'],
      [{ ok: false, error: 'unexpected', status: 418 }, 'pair_unexpected'],
    ];

    for (const [resp, expectedError] of cases) {
      const r = await pairFeishuStep3(
        { orderStore: f.orderStore, relayHttpUrl: 'http://x', approveFn: async () => resp },
        { orderId, code: 'AB1234' },
      );
      expect(r.ok).toBe(false);
      if (!r.ok) expect(r.error).toBe(expectedError);
    }
  });
});
