import { describe, it, expect, beforeEach, afterEach } from 'vitest';
import type { Server } from 'node:http';
import { mkdtemp, readFile, writeFile } from 'node:fs/promises';
import { join } from 'node:path';
import { tmpdir } from 'node:os';
import { createHttpServer } from '../src/http-server.js';
import { PhoneAllowlist } from '../src/phone-allowlist.js';

const baseRelay = `{
  "bindings": [],
  "users": []
}\n`;

describe('onboarding HTTP — three-step flow [P0-009/010/011]', () => {
  let server: Server;
  let baseUrl: string;
  let relayPath: string;
  let tenantsRoot: string;
  const origEnv = process.env;

  beforeEach(async () => {
    process.env = { ...origEnv };
    process.env['NODE_ENV'] = 'test';
    process.env['PLATFORM_ALLOW_MOCK_PAY'] = '1';
    const dir = await mkdtemp(join(tmpdir(), 'poh-'));
    relayPath = join(dir, 'relay.json');
    tenantsRoot = join(dir, 'Tenants');
    await writeFile(relayPath, baseRelay, 'utf8');
    const allow = new PhoneAllowlist(new Set(['+8613800138000']));
    server = createHttpServer({
      allowlist: allow,
      relayConfigPath: relayPath,
      tenantsRoot,
      skipRelayReload: true,
      verifyFeishuCredentials: async () => ({
        ok: true,
        tenantAccessToken: 't_test_abc',
        expire: 7200,
      }),
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

  it('POST /internal/orders 403 when phone not allowlisted', async () => {
    const res = await fetch(`${baseUrl}/internal/orders`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ phone: '13900139000', displayName: 'Eric' }),
    });
    expect(res.status).toBe(403);
  });

  it('Step 1: POST /internal/orders persists displayName + returns pending', async () => {
    const res = await fetch(`${baseUrl}/internal/orders`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ phone: '13800138000', displayName: '聪聪' }),
    });
    expect(res.status).toBe(200);
    const body = (await res.json()) as { orderId: string; status: string; displayName?: string };
    expect(body.orderId).toMatch(/^ord_/);
    expect(body.status).toBe('pending');
    expect(body.displayName).toBe('聪聪');

    const get = await fetch(`${baseUrl}/internal/orders/${body.orderId}`);
    expect(get.status).toBe(200);
    const detail = (await get.json()) as { displayName: string; status: string };
    expect(detail.displayName).toBe('聪聪');
    expect(detail.status).toBe('pending');
  });

  it('Step 2: provision body schema is now {orderId, feishuAppId, feishuAppSecret}', async () => {
    const o = await fetch(`${baseUrl}/internal/orders`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ phone: '13800138000', displayName: '聪聪' }),
    });
    const { orderId } = (await o.json()) as { orderId: string };
    await fetch(`${baseUrl}/internal/mock-pay`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ orderId }),
    });

    const prov = await fetch(`${baseUrl}/internal/provision`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        orderId,
        feishuAppId: 'cli_a',
        feishuAppSecret: 's_test',
      }),
    });
    expect(prov.status).toBe(200);
    const body = (await prov.json()) as {
      ok: true;
      tenantId: string;
      agentId: string;
      principalId: string;
      bindingId: string;
      agentName: string;
    };
    expect(body.tenantId).toMatch(/^member-\d{4}$/);
    expect(body.agentId).toBe(`${body.tenantId}-pulse`);
    expect(body.principalId).toBe(body.tenantId);
    expect(body.bindingId).toBe(`bind-${body.tenantId}`);
    expect(body.agentName).toBe('聪聪的pulse管理员');

    const txt = await readFile(relayPath, 'utf8');
    const parsed = JSON.parse(txt) as {
      bindings: { id: string; status: string; address: { value: string } }[];
      users: { agents: { id: string; name: string; emoji: string }[] }[];
    };
    expect(parsed.bindings).toHaveLength(1);
    expect(parsed.bindings[0].status).toBe('pending');
    expect(parsed.bindings[0].address.value).toBe('');
    const agent = parsed.users[0].agents[0];
    expect(agent.id).toBe(body.agentId);
    expect(agent.name).toBe('聪聪的pulse管理员');
    expect(agent.emoji).toBe('🐦');
  });

  it('Step 2: missing fields → 400 missing_fields', async () => {
    const o = await fetch(`${baseUrl}/internal/orders`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ phone: '13800138000', displayName: 'Eric' }),
    });
    const { orderId } = (await o.json()) as { orderId: string };
    const prov = await fetch(`${baseUrl}/internal/provision`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ orderId, feishuAppId: 'cli_a' }),
    });
    expect(prov.status).toBe(400);
    const body = (await prov.json()) as { error: string };
    expect(body.error).toBe('missing_fields');
  });

  it('Step 2: not_paid → 409', async () => {
    const o = await fetch(`${baseUrl}/internal/orders`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ phone: '13800138000', displayName: 'Eric' }),
    });
    const { orderId } = (await o.json()) as { orderId: string };
    const prov = await fetch(`${baseUrl}/internal/provision`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ orderId, feishuAppId: 'cli_a', feishuAppSecret: 's' }),
    });
    expect(prov.status).toBe(409);
    const body = (await prov.json()) as { error: string };
    expect(body.error).toBe('not_paid');
  });

  it('AC-P0-005: invalid Feishu credentials → 400 with Feishu code/msg', async () => {
    server.close();
    server = createHttpServer({
      allowlist: new PhoneAllowlist(new Set(['+8613800138000'])),
      relayConfigPath: relayPath,
      tenantsRoot,
      skipRelayReload: true,
      verifyFeishuCredentials: async () => ({
        ok: false,
        error: 'invalid_credentials',
        code: 99991663,
        msg: 'app_secret invalid, please check it.',
      }),
    });
    await new Promise<void>((resolve, reject) => {
      server.listen(0, '127.0.0.1', () => resolve());
      server.on('error', reject);
    });
    const addr = server.address();
    if (addr === null || typeof addr === 'string') throw new Error('addr');
    baseUrl = `http://127.0.0.1:${addr.port}`;

    const o = await fetch(`${baseUrl}/internal/orders`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ phone: '13800138000', displayName: '聪聪' }),
    });
    const { orderId } = (await o.json()) as { orderId: string };
    await fetch(`${baseUrl}/internal/mock-pay`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ orderId }),
    });
    const prov = await fetch(`${baseUrl}/internal/provision`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ orderId, feishuAppId: 'cli_a', feishuAppSecret: 'wrong' }),
    });
    expect(prov.status).toBe(400);
    const body = (await prov.json()) as { error: string; feishuCode: number; feishuMsg: string };
    expect(body.error).toBe('invalid_feishu_credentials');
    expect(body.feishuCode).toBe(99991663);
    expect(body.feishuMsg).toContain('app_secret invalid');

    const txt = await readFile(relayPath, 'utf8');
    expect(txt).not.toContain('cli_a');
  });
});

describe('Step 3: /internal/pair', () => {
  let server: Server;
  let baseUrl: string;
  let relayPath: string;
  let tenantsRoot: string;
  let fakeRelayCalls: { path: string; body: unknown }[] = [];
  let fakeRelayResponse: { status: number; body: unknown };
  const origEnv = process.env;

  beforeEach(async () => {
    process.env = { ...origEnv };
    process.env['NODE_ENV'] = 'test';
    process.env['PLATFORM_ALLOW_MOCK_PAY'] = '1';
    const dir = await mkdtemp(join(tmpdir(), 'poh3-'));
    relayPath = join(dir, 'relay.json');
    tenantsRoot = join(dir, 'Tenants');
    await writeFile(relayPath, baseRelay, 'utf8');

    fakeRelayCalls = [];
    fakeRelayResponse = {
      status: 200,
      body: {
        ok: true,
        openId: 'ou_pair_done',
        agentId: 'member-0001-pulse',
        principalId: 'member-0001',
      },
    };
    const fetchImpl: typeof fetch = (async (
      url: RequestInfo | URL,
      init?: RequestInit,
    ): Promise<Response> => {
      const u = String(url);
      const path = u.replace(/^https?:\/\/[^/]+/, '');
      let parsed: unknown;
      try { parsed = JSON.parse(String(init?.body ?? '{}')); } catch { parsed = undefined; }
      fakeRelayCalls.push({ path, body: parsed });
      return new Response(JSON.stringify(fakeRelayResponse.body), {
        status: fakeRelayResponse.status,
        headers: { 'Content-Type': 'application/json' },
      });
    }) as unknown as typeof fetch;

    server = createHttpServer({
      allowlist: new PhoneAllowlist(new Set(['+8613800138000'])),
      relayConfigPath: relayPath,
      tenantsRoot,
      skipRelayReload: true,
      relayHttpUrl: 'http://127.0.0.1:65535',
      verifyFeishuCredentials: async () => ({ ok: true, tenantAccessToken: 't', expire: 100 }),
      fetchImpl,
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

  async function bootstrapToStep3(): Promise<{ orderId: string; agentId: string }> {
    const o = await fetch(`${baseUrl}/internal/orders`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ phone: '13800138000', displayName: '聪聪' }),
    });
    const { orderId } = (await o.json()) as { orderId: string };
    await fetch(`${baseUrl}/internal/mock-pay`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ orderId }),
    });
    const provRes = await fetch(`${baseUrl}/internal/provision`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ orderId, feishuAppId: 'cli_a', feishuAppSecret: 's' }),
    });
    const prov = (await provRes.json()) as { agentId: string };
    return { orderId, agentId: prov.agentId };
  }

  it('happy path: forwards to Relay and returns 200 + openId', async () => {
    const { orderId, agentId } = await bootstrapToStep3();
    const res = await fetch(`${baseUrl}/internal/pair`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ orderId, code: 'AB1234' }),
    });
    expect(res.status).toBe(200);
    const body = (await res.json()) as { ok: boolean; openId: string; agentId: string };
    expect(body.ok).toBe(true);
    expect(body.openId).toBe('ou_pair_done');
    expect(body.agentId).toBe(agentId);

    const forward = fakeRelayCalls.find((c) => c.path === '/internal/pairing/approve');
    expect(forward).toBeDefined();
    expect(forward?.body).toMatchObject({
      code: 'AB1234',
      agentId,
      principalId: agentId.replace(/-pulse$/, ''),
    });
  });

  it('returns 404 when Relay reports code_unknown', async () => {
    const { orderId } = await bootstrapToStep3();
    fakeRelayResponse = { status: 404, body: { ok: false, error: 'code_unknown' } };
    const res = await fetch(`${baseUrl}/internal/pair`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ orderId, code: 'WRONG1' }),
    });
    expect(res.status).toBe(404);
    const body = (await res.json()) as { error: string };
    expect(body.error).toBe('pair_code_unknown');
  });

  it('returns 503 pair_unconfigured when relayHttpUrl is empty', async () => {
    server.close();
    server = createHttpServer({
      allowlist: new PhoneAllowlist(new Set(['+8613800138000'])),
      relayConfigPath: relayPath,
      tenantsRoot,
      skipRelayReload: true,
      // intentionally no relayHttpUrl
      verifyFeishuCredentials: async () => ({ ok: true, tenantAccessToken: 't', expire: 100 }),
    });
    await new Promise<void>((resolve, reject) => {
      server.listen(0, '127.0.0.1', () => resolve());
      server.on('error', reject);
    });
    const addr = server.address();
    if (addr === null || typeof addr === 'string') throw new Error('addr');
    const url = `http://127.0.0.1:${addr.port}`;

    const res = await fetch(`${url}/internal/pair`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ orderId: 'ord_x', code: 'AB1234' }),
    });
    expect(res.status).toBe(503);
  });

  it('returns 409 not_provisioned if Step 2 not yet done', async () => {
    const o = await fetch(`${baseUrl}/internal/orders`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ phone: '13800138000', displayName: 'Eric' }),
    });
    const { orderId } = (await o.json()) as { orderId: string };
    const res = await fetch(`${baseUrl}/internal/pair`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ orderId, code: 'AB1234' }),
    });
    expect(res.status).toBe(409);
    const body = (await res.json()) as { error: string };
    expect(body.error).toBe('not_provisioned');
  });
});

describe('provision HTTP without paths', () => {
  let server: Server;
  let baseUrl: string;
  const origEnv = process.env;

  beforeEach(async () => {
    process.env = { ...origEnv };
    server = createHttpServer({
      allowlist: new PhoneAllowlist(new Set(['+8613800138000'])),
      relayConfigPath: '',
      tenantsRoot: '',
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

  it('POST /internal/provision returns 503 when unconfigured', async () => {
    const res = await fetch(`${baseUrl}/internal/provision`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ orderId: 'ord_x', feishuAppId: 'i', feishuAppSecret: 's' }),
    });
    expect(res.status).toBe(503);
  });
});
