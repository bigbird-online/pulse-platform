import { describe, it, expect } from 'vitest';
import { mkdtemp, writeFile } from 'node:fs/promises';
import { join } from 'node:path';
import { tmpdir } from 'node:os';
import { OrderStore } from '../src/order-store.js';
import { PhoneAllowlist } from '../src/phone-allowlist.js';
import { provisionFeishuTenantAfterPaid } from '../src/provision.js';
import { readRelayConfigFile } from '../src/relay-merge.js';

const baseRelay = `{
  "bindings": [],
  "users": []
}\n`;

describe('provisionFeishuTenantAfterPaid [P0-005]', () => {
  const provisionInput = {
    orderId: 'ord_x',
    tenantId: 'member-t1',
    principalId: 'member-t1',
    agentId: 'main-feishu',
    bindingId: 'bind-t1',
    name: 'Bot',
    emoji: '🔮',
    feishuAppId: 'cli_x',
    feishuAppSecret: 'sec',
    feishuOpenId: 'ou_1',
    workspaceId: 'member-t1',
  };

  async function setup(): Promise<{ dir: string; relay: string; tenants: string; store: OrderStore }> {
    const dir = await mkdtemp(join(tmpdir(), 'prv-'));
    const relay = join(dir, 'relay.json');
    await writeFile(relay, baseRelay, 'utf8');
    const tenants = join(dir, 'Tenants');
    const store = new OrderStore();
    return { dir, relay, tenants, store };
  }

  it('rejects unpaid order', async () => {
    const { relay, tenants, store } = await setup();
    const id = store.createPending({ phone: '13800138000' });
    const allow = new PhoneAllowlist(new Set(['+8613800138000']));
    const r = await provisionFeishuTenantAfterPaid({
      orderStore: store,
      allowlist: allow,
      relayConfigPath: relay,
      tenantsRoot: tenants,
      input: { ...provisionInput, orderId: id },
      runReload: false,
    });
    expect(r).toEqual({ ok: false, error: 'not_paid' });
  });

  it('rejects allowlist mismatch', async () => {
    const { relay, tenants, store } = await setup();
    const id = store.createPending({ phone: '13800138000' });
    store.markPaid(id);
    const allow = new PhoneAllowlist(new Set(['+8613900139000']));
    const r = await provisionFeishuTenantAfterPaid({
      orderStore: store,
      allowlist: allow,
      relayConfigPath: relay,
      tenantsRoot: tenants,
      input: { ...provisionInput, orderId: id },
      runReload: false,
    });
    expect(r).toEqual({ ok: false, error: 'not_allowlisted' });
  });

  it('writes relay + tenant dir when paid + allowlisted', async () => {
    const { relay, tenants, store } = await setup();
    const id = store.createPending({ phone: '13800138000' });
    store.markPaid(id);
    const allow = new PhoneAllowlist(new Set(['+8613800138000']));
    const r = await provisionFeishuTenantAfterPaid({
      orderStore: store,
      allowlist: allow,
      relayConfigPath: relay,
      tenantsRoot: tenants,
      input: { ...provisionInput, orderId: id },
      runReload: false,
    });
    expect(r).toEqual({ ok: true, already: false });
    const { doc } = readRelayConfigFile(relay);
    expect(doc.bindings?.some((b) => b.id === 'bind-t1')).toBe(true);
    expect(store.isProvisioned(id)).toBe(true);
  });

  it('is idempotent per order', async () => {
    const { relay, tenants, store } = await setup();
    const id = store.createPending({ phone: '13800138000' });
    store.markPaid(id);
    const allow = new PhoneAllowlist(new Set(['+8613800138000']));
    await provisionFeishuTenantAfterPaid({
      orderStore: store,
      allowlist: allow,
      relayConfigPath: relay,
      tenantsRoot: tenants,
      input: { ...provisionInput, orderId: id },
      runReload: false,
    });
    const r2 = await provisionFeishuTenantAfterPaid({
      orderStore: store,
      allowlist: allow,
      relayConfigPath: relay,
      tenantsRoot: tenants,
      input: { ...provisionInput, orderId: id },
      runReload: false,
    });
    expect(r2).toEqual({ ok: true, already: true });
  });
});
