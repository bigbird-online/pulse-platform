import { createHmac } from 'node:crypto';
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { OrderStore } from '../src/order-store.js';
import { PhoneAllowlist } from '../src/phone-allowlist.js';
import {
  applyAppPlatformWebhookToOrderStore,
  createAppPlatformNativePayment,
  isAppPlatformPaymentConfigured,
  isProviderReportedPaidError,
  scopedProviderOrderId,
  scenarioAmountFen,
  scenarioDescription,
  verifyAppPlatformWebhookSignature,
} from '../src/app-platform-payment.js';

describe('app-platform-payment', () => {
  const origEnv = process.env;
  const origFetch = globalThis.fetch;

  beforeEach(() => {
    process.env = { ...origEnv };
    process.env['NODE_ENV'] = 'test';
    process.env['PLATFORM_APP_ID'] = 'app_test';
    process.env['PLATFORM_APP_API_KEY'] = 'app_key_test';
    vi.restoreAllMocks();
  });

  afterEach(() => {
    process.env = origEnv;
    globalThis.fetch = origFetch;
  });

  it('isAppPlatformPaymentConfigured respects env', () => {
    expect(isAppPlatformPaymentConfigured()).toBe(true);
    delete process.env['PLATFORM_APP_ID'];
    expect(isAppPlatformPaymentConfigured()).toBe(false);
  });

  it('scenarioAmountFen and scenarioDescription', () => {
    expect(scenarioAmountFen('member')).toBe(298_000);
    expect(scenarioAmountFen('test')).toBe(1);
    expect(scenarioDescription('member')).toContain('会员');
    expect(scenarioDescription('test')).toBe('测试支付');
  });

  it('isProviderReportedPaidError detects WeChat duplicate-pay message', () => {
    expect(isProviderReportedPaidError('WeChat API Error: 该订单已支付')).toBe(true);
    expect(isProviderReportedPaidError('该订单已支付')).toBe(true);
    expect(isProviderReportedPaidError('network down')).toBe(false);
  });

  it('createAppPlatformNativePayment maps provider paid error to provider_order_already_paid', async () => {
    const store = new OrderStore();
    const id = store.createPending({ phone: '13800138000' });
    const allow = new PhoneAllowlist(new Set(['+8613800138000']));
    globalThis.fetch = vi.fn(async () =>
      new Response(
        JSON.stringify({ detail: 'WeChat API Error: 该订单已支付' }),
        { status: 400, headers: { 'Content-Type': 'application/json' } },
      ),
    );

    const r = await createAppPlatformNativePayment({
      rawUrl: `/api/payment/create-order?type=test&phone=13800138000&internalOrderId=${id}`,
      orderStore: store,
      allowlist: allow,
    });

    expect(r).toEqual({ success: false, error: 'provider_order_already_paid' });
    expect(store.get(id)?.status).toBe('paid');
  });

  it('createAppPlatformNativePayment posts and returns qrcode_base64', async () => {
    const store = new OrderStore();
    const id = store.createPending({ phone: '13800138000' });
    const allow = new PhoneAllowlist(new Set(['+8613800138000']));
    globalThis.fetch = vi.fn(async () =>
      new Response(
        JSON.stringify({
          order_id: 'ext-1',
          payment_info: { code_url: 'weixin://wxpay/bizpayurl?pr=test' },
        }),
        { status: 200, headers: { 'Content-Type': 'application/json' } },
      ),
    );

    const r = await createAppPlatformNativePayment({
      rawUrl: `/api/payment/create-order?type=test&phone=13800138000&internalOrderId=${id}`,
      orderStore: store,
      allowlist: allow,
    });

    expect(r.success).toBe(true);
    if (r.success) {
      expect(r.order_no).toBe('ext-1');
      expect(r.qrcode_base64.length).toBeGreaterThan(64);
    }
    expect(globalThis.fetch).toHaveBeenCalled();
    const call = (globalThis.fetch as ReturnType<typeof vi.fn>).mock.calls[0];
    expect(call[0]).toContain('/payments/');
    const init = call[1] as RequestInit;
    const body = JSON.parse(init.body as string) as { app_id: string; payment_method: string; order_id: string };
    expect(body.app_id).toBe('app_test');
    expect(body.payment_method).toBe('wechat_native');
    expect(body.order_id).toBe(scopedProviderOrderId(id, 'test'));
  });

  it('verifyAppPlatformWebhookSignature accepts correct HMAC', () => {
    const raw = Buffer.from('{"order_id":"ord_a","payment_status":"paid"}', 'utf8');
    const secret = 'whsec';
    const sig = `sha256=${createHmac('sha256', secret).update(raw).digest('hex')}`;
    expect(verifyAppPlatformWebhookSignature(raw, sig, secret)).toBe(true);
    expect(verifyAppPlatformWebhookSignature(raw, 'sha256=deadbeef', secret)).toBe(false);
  });

  it('verifyAppPlatformWebhookSignature skips check when secret empty', () => {
    const raw = Buffer.from('{}', 'utf8');
    expect(verifyAppPlatformWebhookSignature(raw, undefined, '')).toBe(true);
  });

  it('applyAppPlatformWebhookToOrderStore marks paid', () => {
    const store = new OrderStore();
    const id = store.createPending({ phone: '13800138000' });
    const r = applyAppPlatformWebhookToOrderStore(
      { order_id: id, payment_status: 'paid' },
      store,
    );
    expect(r).toEqual({ ok: true });
    expect(store.get(id)?.status).toBe('paid');
  });

  it('applyAppPlatformWebhookToOrderStore unscopes provider order_id and marks paid', () => {
    const store = new OrderStore();
    const id = store.createPending({ phone: '13800138000' });
    const r = applyAppPlatformWebhookToOrderStore(
      { order_id: `${id}|member`, payment_status: 'paid' },
      store,
    );
    expect(r).toEqual({ ok: true });
    expect(store.get(id)?.status).toBe('paid');
  });

  it('applyAppPlatformWebhookToOrderStore ignores non-paid', () => {
    const store = new OrderStore();
    const id = store.createPending({ phone: '13800138000' });
    expect(
      applyAppPlatformWebhookToOrderStore({ order_id: id, payment_status: 'pending' }, store),
    ).toEqual({ ok: true, ignored: true });
    expect(store.get(id)?.status).toBe('pending');
  });
});
