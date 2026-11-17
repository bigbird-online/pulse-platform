import { createHmac, timingSafeEqual } from 'node:crypto';
import QRCode from 'qrcode';
import type { OrderStore } from './order-store.js';
import { normalizeCnPhone, type PhoneAllowlist } from './phone-allowlist.js';
import type { PaymentCreateOrderBody } from './payment-create-order.js';
import { parseQuery } from './payment-create-order.js';

/** 默认与集成指南一致：`docs/integration-guide/README.md` */
export const DEFAULT_APP_PLATFORM_API_BASE = 'https://www.bigbird.online/api/v1';

export function isAppPlatformPaymentConfigured(env: NodeJS.ProcessEnv = process.env): boolean {
  return Boolean(env['PLATFORM_APP_ID']?.trim() && env['PLATFORM_APP_API_KEY']?.trim());
}

export function appPlatformApiBase(env: NodeJS.ProcessEnv = process.env): string {
  return (env['PLATFORM_APP_API_BASE']?.trim() || DEFAULT_APP_PLATFORM_API_BASE).replace(/\/$/, '');
}

/** 与官网 `payment.json` 场景对齐：test ¥0.01；member 从订单 topUpAmount 读取；兜底 ¥2,980。金额单位：分 */
export function scenarioAmountFen(type: string, topUpAmount?: number): number {
  if (type === 'test') return 1;
  if (topUpAmount && topUpAmount > 0) return Math.round(topUpAmount * 100);
  return 298_000;
}

export function scenarioDescription(type: string): string {
  return type === 'member'
    ? '行业垂类Agent开发辅导社群-会员服务费'
    : '测试支付';
}

type AppPlatformPaymentResponse = {
  order_id?: string;
  payment_id?: string;
  payment_info?: { code_url?: string };
  detail?: string;
  error?: string;
  message?: string;
};

export function scopedProviderOrderId(platformOrderId: string, type: string): string {
  const t = (type || 'test').trim() || 'test';
  // WeChat out_trade_no allows only: ^[0-9a-zA-Z_\\-\\|\\*]+$ (no ':')
  return `${platformOrderId}|${t}`;
}

export function unscopedPlatformOrderId(providerOrderId: string): string {
  const i = providerOrderId.indexOf('|');
  if (i <= 0) return providerOrderId;
  return providerOrderId.slice(0, i);
}

/**
 * App 平台在「重复下单 / 二维码刷新」且微信侧已收款时返回的业务错误文案。
 * 此时本地 OrderStore 可能因 webhook 延迟仍为 pending，需让前端轮询 `/internal/orders/:id`。
 */
export function isProviderReportedPaidError(message: string): boolean {
  const m = message.trim();
  if (!m) return false;
  if (m.includes('该订单已支付')) return true;
  if (/already\s+paid/i.test(m) && /wechat|wxpay|order/i.test(m)) return true;
  return false;
}

/**
 * 调用 App 管理平台 `POST /payments/`（见 `payment-service.md` · wechat_native），
 * 将返回的 `code_url` 转为 PNG Base64，以兼容现有前端 `payment/index.html` 契约。
 */
export async function createAppPlatformNativePayment(opts: {
  rawUrl: string;
  orderStore: OrderStore;
  allowlist: PhoneAllowlist;
  env?: NodeJS.ProcessEnv;
}): Promise<PaymentCreateOrderBody> {
  const env = opts.env ?? process.env;
  const appId = env['PLATFORM_APP_ID']?.trim();
  const apiKey = env['PLATFORM_APP_API_KEY']?.trim();
  if (!appId || !apiKey) {
    return { success: false, error: 'app_platform_not_configured' };
  }

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
  if (order.status === 'paid') return { success: false, error: 'order_already_paid' };
  if (order.status !== 'pending') return { success: false, error: 'order_not_pending' };

  const publicBase = (env['PLATFORM_PUBLIC_BASE_URL'] ?? 'https://bigbird.online/platform').trim();
  const callbackUrl = publicBase
    ? `${publicBase.replace(/\/$/, '')}/internal/webhooks/app-platform-payment`
    : undefined;

  const payload = {
    app_id: appId,
    // App 平台/微信侧对同 order_id 的“重入”要求参数与首次一致；
    // 将平台内部订单号按场景隔离，避免用户切换 test/member 触发重入校验失败。
    order_id: scopedProviderOrderId(internalOrderId, type),
    amount: scenarioAmountFen(type, order.topUpAmount),
    currency: 'CNY',
    payment_method: 'wechat_native',
    description: scenarioDescription(type),
    ...(callbackUrl ? { callback_url: callbackUrl } : {}),
    extra_data: {
      pulse_platform: true,
      phone_e164: normalizeCnPhone(phone),
    },
  };

  const url = `${appPlatformApiBase(env)}/payments/`;
  let r: Response;
  try {
    r = await fetch(url, {
      method: 'POST',
      headers: {
        Authorization: `Bearer ${apiKey}`,
        'Content-Type': 'application/json',
        Accept: 'application/json',
      },
      body: JSON.stringify(payload),
      signal: AbortSignal.timeout(30_000),
    });
  } catch {
    return { success: false, error: 'app_platform_network_error' };
  }

  const text = await r.text();
  let data: AppPlatformPaymentResponse;
  try {
    data = JSON.parse(text) as AppPlatformPaymentResponse;
  } catch {
    return { success: false, error: 'app_platform_bad_response' };
  }

  if (!r.ok) {
    const msg =
      data.detail ?? data.error ?? data.message ?? `http_${r.status}`;
    const text = String(msg);
    if (isProviderReportedPaidError(text)) {
      // Webhook 可能因 callback_url 未配置 / 网络问题而延迟或缺失；
      // 但 provider 已明确返回「已支付」，此处做幂等落库以便前端轮询推进 Step 2。
      opts.orderStore.markPaid(internalOrderId);
      return { success: false, error: 'provider_order_already_paid' };
    }
    return { success: false, error: text };
  }

  // 集成指南约定 payment_info.code_url；实际响应也可能在 extra_data.api_response.code_url
  const extra = typeof (data as Record<string, unknown>)?.extra_data === 'object'
    ? (data as Record<string, unknown>).extra_data as Record<string, unknown> : undefined;
  const apiResp = typeof extra?.api_response === 'object'
    ? extra.api_response as Record<string, unknown> : undefined;
  const codeUrl: string | undefined =
    data.payment_info?.code_url ?? apiResp?.code_url as string | undefined;
  if (!codeUrl || typeof codeUrl !== 'string') {
    return { success: false, error: 'missing_code_url' };
  }

  const png = await QRCode.toBuffer(codeUrl, { type: 'png', width: 240, margin: 2 });
  const generated_at = new Date().toISOString();
  return {
    success: true,
    order_no: data.order_id ?? internalOrderId,
    qrcode_base64: png.toString('base64'),
    generated_at,
  };
}

/**
 * 与集成指南一致：`X-Webhook-Signature: sha256=<HMAC-SHA256(hex)>`，payload 为 **原始 body 字节**。
 */
export function verifyAppPlatformWebhookSignature(
  rawBody: Buffer,
  signatureHeader: string | string[] | undefined,
  secret: string,
): boolean {
  if (!secret) return true;
  const sigIn = Array.isArray(signatureHeader) ? signatureHeader[0] : signatureHeader;
  if (!sigIn) return false;
  const expected = `sha256=${createHmac('sha256', secret).update(rawBody).digest('hex')}`;
  try {
    const a = Buffer.from(expected, 'utf8');
    const b = Buffer.from(sigIn, 'utf8');
    if (a.length !== b.length) return false;
    return timingSafeEqual(a, b);
  } catch {
    return false;
  }
}

export type AppPlatformWebhookApplyResult =
  | { ok: true; ignored?: boolean }
  | { ok: false; error: string };

/** 解析 Webhook JSON（`payment-service.md`），`payment_status === paid` 时 `markPaid(order_id)`。 */
export function applyAppPlatformWebhookToOrderStore(
  body: unknown,
  orderStore: OrderStore,
): AppPlatformWebhookApplyResult {
  if (typeof body !== 'object' || body === null) {
    return { ok: false, error: 'invalid_body' };
  }
  const o = body as Record<string, unknown>;
  const paymentStatus = o['payment_status'];
  const event = o['event'];
  const orderIdRaw = typeof o['order_id'] === 'string' ? o['order_id'].trim() : '';
  const orderId = orderIdRaw ? unscopedPlatformOrderId(orderIdRaw) : '';

  if (!orderId) return { ok: false, error: 'missing_order_id' };

  const paid =
    paymentStatus === 'paid' ||
    event === 'payment.success' ||
    o['status'] === 'paid';

  if (!paid) return { ok: true, ignored: true };

  const r = orderStore.markPaid(orderId);
  if (!r.success && r.error === 'not_found') {
    return { ok: false, error: 'order_not_found' };
  }
  return { ok: true };
}

/**
 * 主动向 App Platform 查询支付状态，如果已支付则 markPaid。
 * 用于弥补 webhook 回调不稳定的场景。
 */
export async function syncPaymentStatusFromProvider(opts: {
  orderId: string;
  orderStore: OrderStore;
  env?: NodeJS.ProcessEnv;
  fetchImpl?: typeof fetch;
}): Promise<{ synced: boolean; paid: boolean }> {
  const env = opts.env ?? process.env;
  const fetchImpl = opts.fetchImpl ?? fetch;
  const apiKey = env['PLATFORM_APP_API_KEY']?.trim();
  if (!apiKey) return { synced: false, paid: false };

  const order = opts.orderStore.get(opts.orderId);
  if (!order || order.status === 'paid') return { synced: false, paid: order?.status === 'paid' };

  const providerOrderId = scopedProviderOrderId(opts.orderId, 'member');
  const base = appPlatformApiBase(env);

  try {
    const url = `${base}/payments/?order_id=${encodeURIComponent(providerOrderId)}`;
    const r = await fetchImpl(url, {
      headers: {
        Authorization: `Bearer ${apiKey}`,
        Accept: 'application/json',
      },
      signal: AbortSignal.timeout(10_000),
    });
    if (!r.ok) return { synced: false, paid: false };

    const data = await r.json() as Array<{ order_id: string; status: string }>;
    // Find the latest payment for this order that is paid
    const paidEntry = data.find((p) =>
      p.order_id === providerOrderId && p.status === 'paid',
    );
    if (paidEntry) {
      const result = opts.orderStore.markPaid(opts.orderId);
      return { synced: true, paid: result.success };
    }
    return { synced: true, paid: false };
  } catch {
    return { synced: false, paid: false };
  }
}
