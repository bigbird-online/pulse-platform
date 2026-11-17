/**
 * App Platform subscription API client.
 *
 * PULSE calls these endpoints on App Platform (bigbird.online) to create
 * subscriptions (WeChat PAP signing), query status, and cancel.
 * App Platform handles the actual WeChat Pay integration.
 */

import { appPlatformApiBase } from './app-platform-payment.js';

export interface SubscriptionClientDeps {
  env?: Record<string, string | undefined>;
}

export interface CreateSubscriptionOpts {
  phone: string;
  displayName?: string;
  planId: string;
  outOrderId: string;
  notifyUrl?: string;
}

export interface CreateSubscriptionResult {
  subscriptionId: string;
  signUrl?: string;
  signQrcodeBase64?: string;
}

export interface QuerySubscriptionResult {
  subscriptionId: string;
  status: 'pending_signing' | 'active' | 'past_due' | 'cancelled' | 'expired';
  planId: string;
  currentPeriodEnd?: string;
  autoRenew: boolean;
}

function apiKey(env: Record<string, string | undefined>): string {
  return (env['PLATFORM_APP_API_KEY'] ?? '').trim();
}

/**
 * Create a subscription on App Platform.
 * For auto-renew plans: returns signUrl (WeChat redirect) + signQrcodeBase64.
 */
export async function createSubscription(
  deps: SubscriptionClientDeps,
  opts: CreateSubscriptionOpts,
): Promise<CreateSubscriptionResult> {
  const env = deps.env ?? process.env;
  const key = apiKey(env);
  if (!key) throw new Error('PLATFORM_APP_API_KEY not configured');

  const base = appPlatformApiBase(env);
  const url = `${base}/subscriptions`;

  const payload = {
    user_phone: opts.phone,
    ...(opts.displayName ? { user_display_name: opts.displayName } : {}),
    plan_id: opts.planId,
    out_order_id: opts.outOrderId,
    ...(opts.notifyUrl ? { notify_url: opts.notifyUrl } : {}),
  };

  const r = await fetch(url, {
    method: 'POST',
    headers: {
      Authorization: `Bearer ${key}`,
      'Content-Type': 'application/json',
      Accept: 'application/json',
    },
    body: JSON.stringify(payload),
    signal: AbortSignal.timeout(30_000),
  });

  if (!r.ok) {
    const text = await r.text().catch(() => '');
    throw new Error(`App Platform subscription API returned ${r.status}: ${text.slice(0, 200)}`);
  }

  const data = await r.json() as Record<string, unknown>;
  return {
    subscriptionId: String(data['subscription_id'] ?? ''),
    ...(typeof data['sign_url'] === 'string' ? { signUrl: data['sign_url'] } : {}),
    ...(typeof data['sign_qrcode_base64'] === 'string' ? { signQrcodeBase64: data['sign_qrcode_base64'] } : {}),
  };
}

/** Query subscription status from App Platform. */
export async function querySubscription(
  deps: SubscriptionClientDeps,
  subscriptionId: string,
): Promise<QuerySubscriptionResult> {
  const env = deps.env ?? process.env;
  const key = apiKey(env);
  if (!key) throw new Error('PLATFORM_APP_API_KEY not configured');

  const base = appPlatformApiBase(env);
  const url = `${base}/subscriptions/${encodeURIComponent(subscriptionId)}`;

  const r = await fetch(url, {
    method: 'GET',
    headers: {
      Authorization: `Bearer ${key}`,
      Accept: 'application/json',
    },
    signal: AbortSignal.timeout(15_000),
  });

  if (!r.ok) {
    throw new Error(`App Platform subscription query returned ${r.status}`);
  }

  const data = await r.json() as Record<string, unknown>;
  return {
    subscriptionId: String(data['subscription_id'] ?? subscriptionId),
    status: (data['status'] as QuerySubscriptionResult['status']) ?? 'pending_signing',
    planId: String(data['plan_id'] ?? ''),
    ...(typeof data['current_period_end'] === 'string' ? { currentPeriodEnd: data['current_period_end'] } : {}),
    autoRenew: Boolean(data['auto_renew']),
  };
}

/** Cancel a subscription on App Platform. */
export async function cancelSubscription(
  deps: SubscriptionClientDeps,
  subscriptionId: string,
): Promise<{ ok: boolean }> {
  const env = deps.env ?? process.env;
  const key = apiKey(env);
  if (!key) throw new Error('PLATFORM_APP_API_KEY not configured');

  const base = appPlatformApiBase(env);
  const url = `${base}/subscriptions/${encodeURIComponent(subscriptionId)}/cancel`;

  const r = await fetch(url, {
    method: 'POST',
    headers: {
      Authorization: `Bearer ${key}`,
      'Content-Type': 'application/json',
      Accept: 'application/json',
    },
    body: JSON.stringify({}),
    signal: AbortSignal.timeout(15_000),
  });

  if (!r.ok) {
    throw new Error(`App Platform subscription cancel returned ${r.status}`);
  }

  return { ok: true };
}
