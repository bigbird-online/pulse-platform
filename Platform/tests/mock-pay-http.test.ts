import { describe, it, expect, beforeEach, afterEach } from 'vitest';
import type { Server } from 'node:http';
import { createHttpServer } from '../src/http-server.js';
import type { OrderStore } from '../src/order-store.js';

async function readJson(res: Response): Promise<unknown> {
  return res.json() as Promise<unknown>;
}

describe('POST /internal/mock-pay [P0-002]', () => {
  const origEnv = process.env;
  let server: Server;
  let baseUrl: string;
  let store: OrderStore;

  beforeEach(async () => {
    process.env = { ...origEnv };
    const { OrderStore } = await import('../src/order-store.js');
    store = new OrderStore();
    process.env['NODE_ENV'] = 'test';
    process.env['PLATFORM_ALLOW_MOCK_PAY'] = '1';
    server = createHttpServer({ orderStore: store });
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

  it('403 when mock pay disabled (flag off)', async () => {
    delete process.env['PLATFORM_ALLOW_MOCK_PAY'];
    const id = store.createPending({ phone: '13800138000' });
    const res = await fetch(`${baseUrl}/internal/mock-pay`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ orderId: id }),
    });
    expect(res.status).toBe(403);
    const body = (await readJson(res)) as { error: string };
    expect(body.error).toBe('mock_pay_disabled');
  });

  it('403 when NODE_ENV production', async () => {
    process.env['NODE_ENV'] = 'production';
    process.env['PLATFORM_ALLOW_MOCK_PAY'] = '1';
    const id = store.createPending({ phone: '13800138000' });
    const res = await fetch(`${baseUrl}/internal/mock-pay`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ orderId: id }),
    });
    expect(res.status).toBe(403);
  });

  it('404 when order unknown', async () => {
    const res = await fetch(`${baseUrl}/internal/mock-pay`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ orderId: 'ord_none' }),
    });
    expect(res.status).toBe(404);
  });

  it('400 when body invalid JSON', async () => {
    const res = await fetch(`${baseUrl}/internal/mock-pay`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: 'not-json',
    });
    expect(res.status).toBe(400);
  });

  it('200 marks pending order paid', async () => {
    const id = store.createPending({ phone: '13800138000' });
    const res = await fetch(`${baseUrl}/internal/mock-pay`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ orderId: id }),
    });
    expect(res.status).toBe(200);
    const body = (await readJson(res)) as { orderId: string; status: string; already?: boolean };
    expect(body.orderId).toBe(id);
    expect(body.status).toBe('paid');
    expect(body.already).toBe(false);
    expect(store.get(id)?.status).toBe('paid');
  });

  it('200 idempotent when already paid', async () => {
    const id = store.createPending({ phone: '13800138000' });
    await fetch(`${baseUrl}/internal/mock-pay`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ orderId: id }),
    });
    const res = await fetch(`${baseUrl}/internal/mock-pay`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ orderId: id }),
    });
    expect(res.status).toBe(200);
    const body = (await readJson(res)) as { already: boolean };
    expect(body.already).toBe(true);
  });

  it('409 when order failed', async () => {
    const id = store.createPending({ phone: '13800138000' });
    store.markFailed(id);
    const res = await fetch(`${baseUrl}/internal/mock-pay`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ orderId: id }),
    });
    expect(res.status).toBe(409);
  });
});
