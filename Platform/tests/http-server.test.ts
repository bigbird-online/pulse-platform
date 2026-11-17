import { describe, it, expect, beforeAll, afterAll } from 'vitest';
import { createHttpServer } from '../src/http-server.js';
import { OrderStore } from '../src/order-store.js';
import type { Server } from 'node:http';

describe('http-server [P0-001]', () => {
  let server: Server;
  let baseUrl: string;

  beforeAll(async () => {
    server = createHttpServer({ webRoot: false });
    await new Promise<void>((resolve, reject) => {
      server.listen(0, '127.0.0.1', () => resolve());
      server.on('error', reject);
    });
    const addr = server.address();
    if (addr === null || typeof addr === 'string') {
      throw new Error('expected TCP address');
    }
    baseUrl = `http://127.0.0.1:${addr.port}`;
  });

  afterAll(async () => {
    await new Promise<void>((resolve, reject) => {
      server.close((err) => (err ? reject(err) : resolve()));
    });
  });

  it('GET /health returns ok', async () => {
    const res = await fetch(`${baseUrl}/health`);
    expect(res.status).toBe(200);
    const body = (await res.json()) as { status: string; service: string };
    expect(body.status).toBe('ok');
    expect(body.service).toBe('@pulse/platform');
  });

  it('GET /ping returns pong', async () => {
    const res = await fetch(`${baseUrl}/ping`);
    expect(res.status).toBe(200);
    const body = (await res.json()) as { pong: boolean };
    expect(body.pong).toBe(true);
  });

  it('unknown path returns 404', async () => {
    const res = await fetch(`${baseUrl}/nope`);
    expect(res.status).toBe(404);
  });

  it('GET /internal/capabilities returns flags', async () => {
    const res = await fetch(`${baseUrl}/internal/capabilities`);
    expect(res.status).toBe(200);
    const body = (await res.json()) as {
      mockPay: boolean;
      provisionConfigured: boolean;
      nativePayment: boolean;
      appPlatformPayment?: boolean;
      paymentScenarios: Record<string, unknown>;
    };
    expect(typeof body.mockPay).toBe('boolean');
    expect(typeof body.provisionConfigured).toBe('boolean');
    expect(typeof body.nativePayment).toBe('boolean');
    expect(typeof body.appPlatformPayment).toBe('boolean');
    expect(body.paymentScenarios['member']).toBeDefined();
  });
});

describe('dashboard token API [v0.7.0]', () => {
  let server: Server;
  let baseUrl: string;
  let orderStore: OrderStore;

  beforeAll(async () => {
    orderStore = new OrderStore();
    server = createHttpServer({ webRoot: false, orderStore });
    await new Promise<void>((resolve, reject) => {
      server.listen(0, '127.0.0.1', () => resolve());
      server.on('error', reject);
    });
    const addr = server.address();
    if (addr === null || typeof addr === 'string') {
      throw new Error('expected TCP address');
    }
    baseUrl = `http://127.0.0.1:${addr.port}`;
  });

  afterAll(async () => {
    await new Promise<void>((resolve, reject) => {
      server.close((err) => (err ? reject(err) : resolve()));
    });
  });

  it('POST /api/auth/dashboard-token returns 400 without tenantId', async () => {
    const res = await fetch(`${baseUrl}/api/auth/dashboard-token`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({}),
    });
    expect(res.status).toBe(400);
    const body = (await res.json()) as { error: string };
    expect(body.error).toBe('missing_tenantId');
  });

  it('POST /api/auth/dashboard-token works for any tenantId (trust-on-first-use)', async () => {
    const res = await fetch(`${baseUrl}/api/auth/dashboard-token`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ tenantId: 'new-tenant:agent' }),
    });
    expect(res.status).toBe(200);
    const body = (await res.json()) as { token: string; dashboardUrl: string; expiresInMs: number };
    expect(body.token).toBeTruthy();
    expect(body.expiresInMs).toBe(24 * 60 * 60 * 1000);
  });

  it('full token lifecycle: create → verify', async () => {
    const tenantId = 'test-user:test-agent';

    // Create a paid order with this tenantId so the tenant "exists"
    const orderId = orderStore.createPending({ phone: '13800000001', topUpAmount: 100 });
    orderStore.markPaid(orderId);
    // Manually set tenantId on the order (simulating provision flow)
    const order = orderStore.get(orderId);
    expect(order).toBeTruthy();
    // Use restore to set tenantId on the order
    orderStore.restore({
      id: orderId,
      phone: '13800000001',
      status: 'paid',
      tenantId,
    });

    // Now generate dashboard token
    const tokenRes = await fetch(`${baseUrl}/api/auth/dashboard-token`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ tenantId }),
    });
    expect(tokenRes.status).toBe(200);

    const tokenData = (await tokenRes.json()) as { token: string; dashboardUrl: string; barePath: string; expiresInMs: number };
    expect(tokenData.token).toBeTruthy();
    expect(tokenData.expiresInMs).toBe(24 * 60 * 60 * 1000);
    // dashboardUrl is empty without PLATFORM_PUBLIC_BASE_URL; barePath is always present
    expect(tokenData.barePath).toContain('token=');

    // Verify the token
    const verifyRes = await fetch(`${baseUrl}/api/auth/verify-token?token=${encodeURIComponent(tokenData.token)}`);
    expect(verifyRes.status).toBe(200);
    const verifyBody = (await verifyRes.json()) as { tenantId: string; expiresInMs: number };
    expect(verifyBody.tenantId).toBe(tenantId);
    expect(verifyBody.expiresInMs).toBeGreaterThan(0);

    // Second generation should return a different token (both valid)
    const tokenRes2 = await fetch(`${baseUrl}/api/auth/dashboard-token`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ tenantId }),
    });
    const tokenData2 = (await tokenRes2.json()) as { token: string };
    expect(tokenData2.token).not.toBe(tokenData.token);

    // First token should still be valid
    const verifyRes1 = await fetch(`${baseUrl}/api/auth/verify-token?token=${encodeURIComponent(tokenData.token)}`);
    expect(verifyRes1.status).toBe(200);
  });

  it('GET /api/auth/verify-token returns 400 without token', async () => {
    const res = await fetch(`${baseUrl}/api/auth/verify-token`);
    expect(res.status).toBe(400);
    const body = (await res.json()) as { error: string };
    expect(body.error).toBe('missing_token');
  });

  it('GET /api/auth/verify-token returns 401 for expired token', async () => {
    const res = await fetch(`${baseUrl}/api/auth/verify-token?token=definitely-invalid-token`);
    expect(res.status).toBe(401);
    const body = (await res.json()) as { error: string };
    expect(body.error).toBe('token_invalid_or_expired');
  });
});
