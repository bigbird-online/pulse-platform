import { describe, it, expect, beforeEach, afterEach } from 'vitest';
import type { Server } from 'node:http';
import { OrderStore } from '../src/order-store.js';
import { PhoneAllowlist } from '../src/phone-allowlist.js';
import {
  createOrderNativePayment,
  isNativePaymentEnabled,
  isNativePayMockAllowed,
  parseQuery,
} from '../src/payment-create-order.js';
import { createHttpServer } from '../src/http-server.js';

describe('payment-create-order (官网契约)', () => {
  const origEnv = process.env;

  beforeEach(() => {
    process.env = { ...origEnv };
    process.env['NODE_ENV'] = 'test';
    process.env['PLATFORM_NATIVE_PAY_MOCK'] = '1';
  });

  afterEach(() => {
    process.env = origEnv;
  });

  it('parseQuery reads internalOrderId', () => {
    const u = parseQuery('/api/payment/create-order?type=test&phone=13800138000&internalOrderId=ord_1');
    expect(u.get('type')).toBe('test');
    expect(u.get('phone')).toBe('13800138000');
    expect(u.get('internalOrderId')).toBe('ord_1');
  });

  it('createOrderNativePayment returns qrcode_base64 when mock allowed', async () => {
    const store = new OrderStore();
    const id = store.createPending({ phone: '13800138000' });
    const allow = new PhoneAllowlist(new Set(['+8613800138000']));
    const r = await createOrderNativePayment({
      rawUrl: `/api/payment/create-order?type=test&phone=13800138000&internalOrderId=${id}`,
      orderStore: store,
      allowlist: allow,
    });
    expect(r.success).toBe(true);
    if (r.success) {
      expect(r.order_no).toMatch(/^PULSE-/);
      expect(r.qrcode_base64.length).toBeGreaterThan(32);
      expect(r.generated_at).toMatch(/^\d{4}-/);
    }
  });

  it('rejects when native mock disabled in production', async () => {
    process.env['NODE_ENV'] = 'production';
    process.env['PLATFORM_NATIVE_PAY_MOCK'] = '1';
    expect(isNativePayMockAllowed()).toBe(false);
  });

  it('isNativePaymentEnabled is true when App 平台凭据已配置', () => {
    delete process.env['PLATFORM_PAYMENT_PROXY_URL'];
    process.env['PLATFORM_APP_ID'] = 'app_x';
    process.env['PLATFORM_APP_API_KEY'] = 'key_y';
    process.env['PLATFORM_NATIVE_PAY_MOCK'] = '';
    expect(isNativePaymentEnabled()).toBe(true);
  });

  it('returns error when not pending', async () => {
    const store = new OrderStore();
    const id = store.createPending({ phone: '13800138000' });
    store.markPaid(id);
    const allow = new PhoneAllowlist(new Set(['+8613800138000']));
    const r = await createOrderNativePayment({
      rawUrl: `/api/payment/create-order?type=test&phone=13800138000&internalOrderId=${id}`,
      orderStore: store,
      allowlist: allow,
    });
    expect(r).toEqual({ success: false, error: 'order_not_pending' });
  });
});

describe('GET /api/payment/create-order HTTP', () => {
  const origEnv = process.env;
  let server: Server;
  let baseUrl: string;
  let orderId: string;

  beforeEach(async () => {
    process.env = { ...origEnv };
    process.env['NODE_ENV'] = 'test';
    process.env['PLATFORM_NATIVE_PAY_MOCK'] = '1';
    const allow = new PhoneAllowlist(new Set(['+8613800138000']));
    const store = new OrderStore();
    server = createHttpServer({ allowlist: allow, orderStore: store, webRoot: false });
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
      body: JSON.stringify({ phone: '13800138000' }),
    });
    const body = (await o.json()) as { orderId: string };
    orderId = body.orderId;
  });

  afterEach(async () => {
    process.env = origEnv;
    await new Promise<void>((resolve, reject) => {
      server.close((err) => (err ? reject(err) : resolve()));
    });
  });

  it('returns JSON body matching 官网 shape', async () => {
    const res = await fetch(
      `${baseUrl}/api/payment/create-order?type=test&phone=13800138000&internalOrderId=${orderId}`,
    );
    expect(res.status).toBe(200);
    const data = (await res.json()) as { success: boolean; qrcode_base64?: string; order_no?: string };
    expect(data.success).toBe(true);
    expect(data.qrcode_base64).toBeDefined();
    expect(data.order_no).toMatch(/^PULSE-/);
  });
});
