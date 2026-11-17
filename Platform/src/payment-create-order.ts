import { randomBytes } from 'node:crypto';
import QRCode from 'qrcode';
import type { OrderStore } from './order-store.js';
import { normalizeCnPhone, type PhoneAllowlist } from './phone-allowlist.js';

/** 与「大鸟聪聪官网」`payment/index.html` 一致的 JSON 成功体字段名。 */
export type PaymentCreateOrderOk = {
  success: true;
  order_no: string;
  qrcode_base64: string;
  generated_at: string;
};

export type PaymentCreateOrderErr = {
  success: false;
  error: string;
};

export type PaymentCreateOrderBody = PaymentCreateOrderOk | PaymentCreateOrderErr;

/** 非生产 + `PLATFORM_NATIVE_PAY_MOCK=1` 时允许本地生成占位收款码（JSON 载荷，非真微信 URL）。 */
export function isNativePayMockAllowed(env: NodeJS.ProcessEnv = process.env): boolean {
  if (env['NODE_ENV'] === 'production') return false;
  return env['PLATFORM_NATIVE_PAY_MOCK'] === '1';
}

export function isNativePaymentEnabled(env: NodeJS.ProcessEnv = process.env): boolean {
  if (env['PLATFORM_PAYMENT_PROXY_URL']?.trim()) return true;
  if (env['PLATFORM_APP_ID']?.trim() && env['PLATFORM_APP_API_KEY']?.trim()) return true;
  return isNativePayMockAllowed(env);
}

/** 将查询串解析为 `URLSearchParams`（不含 `?`）。 */
export function parseQuery(rawUrl: string): URLSearchParams {
  const q = rawUrl.indexOf('?');
  return new URLSearchParams(q >= 0 ? rawUrl.slice(q + 1) : '');
}

/**
 * 内聚模式：校验 `internalOrderId` + 手机号后生成二维码 PNG（Base64）。
 * 与官网「真实微信码」不同，Mock 仅用于联调 UI；生产请配置 `PLATFORM_PAYMENT_PROXY_URL` 走网关。
 */
export async function createOrderNativePayment(opts: {
  rawUrl: string;
  orderStore: OrderStore;
  allowlist: PhoneAllowlist;
  env?: NodeJS.ProcessEnv;
}): Promise<PaymentCreateOrderBody> {
  const env = opts.env ?? process.env;
  const u = parseQuery(opts.rawUrl);
  const type = u.get('type') ?? 'test';
  const phone = u.get('phone')?.trim() ?? '';
  const internalOrderId = u.get('internalOrderId')?.trim() ?? '';

  if (!internalOrderId || !phone) {
    return { success: false, error: 'missing_phone_or_internalOrderId' };
  }

  if (!opts.allowlist.isAllowed(phone)) {
    return { success: false, error: 'not_allowlisted' };
  }

  const order = opts.orderStore.get(internalOrderId);
  if (!order) return { success: false, error: 'order_not_found' };
  if (normalizeCnPhone(order.phone) !== normalizeCnPhone(phone)) {
    return { success: false, error: 'phone_mismatch' };
  }
  if (order.status !== 'pending') {
    return { success: false, error: 'order_not_pending' };
  }

  if (!isNativePayMockAllowed(env)) {
    return { success: false, error: 'payment_service_unconfigured' };
  }

  const order_no = `PULSE-${Date.now()}-${randomBytes(3).toString('hex')}`;
  const payload = JSON.stringify({ v: 1, internalOrderId, order_no, type });
  const png = await QRCode.toBuffer(payload, { type: 'png', width: 240, margin: 2 });
  const qrcode_base64 = png.toString('base64');
  const generated_at = new Date().toISOString();
  return { success: true, order_no, qrcode_base64, generated_at };
}

/**
 * 透传模式：将完整查询参数合并到 `PLATFORM_PAYMENT_PROXY_URL` 并原样返回响应体（用于对接官网或统一收银台）。
 */
export async function proxyPaymentCreateOrder(
  rawUrl: string,
  env: NodeJS.ProcessEnv = process.env,
): Promise<{ status: number; contentType: string; body: string }> {
  const proxyBase = env['PLATFORM_PAYMENT_PROXY_URL']?.trim();
  if (!proxyBase) {
    return {
      status: 502,
      contentType: 'application/json; charset=utf-8',
      body: JSON.stringify({ success: false, error: 'payment_proxy_missing' }),
    };
  }

  const incoming = parseQuery(rawUrl);
  let targetUrl: string;
  try {
    const base = new URL(proxyBase);
    incoming.forEach((value, key) => {
      base.searchParams.set(key, value);
    });
    targetUrl = base.toString();
  } catch {
    const joiner = proxyBase.includes('?') ? '&' : '?';
    const qs = incoming.toString();
    targetUrl = qs ? `${proxyBase}${joiner}${qs}` : proxyBase;
  }

  const r = await fetch(targetUrl, {
    headers: { Accept: 'application/json' },
    signal: AbortSignal.timeout(25_000),
  });
  const contentType = r.headers.get('content-type') ?? 'application/json; charset=utf-8';
  const body = await r.text();
  return { status: r.status, contentType, body };
}
