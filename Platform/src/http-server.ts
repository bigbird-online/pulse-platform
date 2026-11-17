import { createServer, type IncomingMessage, type Server, type ServerResponse } from 'node:http';
import { isMockPayAllowed } from './mock-pay-policy.js';
import { OrderStore } from './order-store.js';
import { PhoneAllowlist } from './phone-allowlist.js';
import { addPhoneToAllowlistFile } from './allowlist-admin.js';
import {
  applyAppPlatformWebhookToOrderStore,
  createAppPlatformNativePayment,
  isAppPlatformPaymentConfigured,
  syncPaymentStatusFromProvider,
  verifyAppPlatformWebhookSignature,
} from './app-platform-payment.js';
import {
  createOrderNativePayment,
  isNativePaymentEnabled,
  proxyPaymentCreateOrder,
} from './payment-create-order.js';
import {
  pairFeishuStep3,
  provisionFeishuStep2,
  type Step2Deps,
  type Step2Outcome,
  type Step3Outcome,
} from './provision.js';
import { reloadRelayAgents } from './relay-pairing-client.js';
import { defaultWebRoot, tryServeWebRoot } from './serve-web.js';
import {
  TenantIdAllocator,
  scanMaxMemberSeqFromRelayConfig,
  scanMaxMemberSeqFromTenantsDir,
} from './tenant-id-allocator.js';
import { materializeTenantSpaceForOrder } from './tenant-materialize.js';
import { BalanceStore } from './balance-store.js';
import { ConsumptionStore } from './consumption-store.js';
import { randomUUID } from 'node:crypto';
// v0.6.0: subscription deprecated but kept for backward compat
import { createSubscription as _createSubscription } from './subscription-client.js';
const createSubscription = _createSubscription;

const JSON_HDR = { 'Content-Type': 'application/json; charset=utf-8' } as const;
const BODY_LIMIT = 65536;

export function healthPayload(): { status: string; service: string } {
  return { status: 'ok', service: '@pulse/platform' };
}

export function pingPayload(): { pong: boolean } {
  return { pong: true };
}

export type HttpServerOptions = {
  orderStore?: OrderStore;
  allowlist?: PhoneAllowlist;
  /** relay.json 绝对路径；未设则 POST /internal/provision 返回 503 */
  relayConfigPath?: string;
  /** 租户根目录；与 relay agent workspace 下路径一致 */
  tenantsRoot?: string;
  /** 为测试关闭 provision 后的 reload */
  skipRelayReload?: boolean;
  /** Web 静态资源目录（`npm run build:web` → `dist/web`）；`false` 关闭 */
  webRoot?: string | false;
  /** v0.3.0：tenantId 分配器；未提供则按 env 懒加载 */
  tenantIdAllocator?: TenantIdAllocator;
  /** v0.3.0：Relay loopback URL（用于热加载与配对转发）；未设则 reload 走 PLATFORM_RELAY_RELOAD_CMD，pair 返回 503 */
  relayHttpUrl?: string;
  /** v0.3.0 测试钩子：Feishu 凭证校验注入。生产环境留空，走真实飞书 API。 */
  verifyFeishuCredentials?: Step2Deps['verifyFeishuCredentials'];
  /** v0.3.0 测试钩子：Step 3 转发到 Relay 的 fetch（默认走全局 fetch）。 */
  fetchImpl?: typeof fetch;
  /** v0.4.1：覆盖租户模板根目录（测试）；缺省走 `tenant-template.defaultTenantPulseAdminTemplateRoot()`。 */
  tenantTemplateRoot?: string;
  /**
   * v0.4.1：FR-P0-014 Stage A 自动物化开关。默认 `true`。
   * 设为 `false` 时不在 `markPaid` 后自动拷贝模板（用于隔离单测）。
   */
  enableStageAMaterialize?: boolean;
};

type ResolvedHttpDeps = {
  orderStore: OrderStore;
  allowlist: PhoneAllowlist;
  allowlistInjected: boolean;
  relayConfigPath: string;
  tenantsRoot: string;
  skipRelayReload: boolean;
  webRoot: string;
  tenantIdAllocator?: TenantIdAllocator;
  relayHttpUrl: string;
  verifyFeishuCredentials?: Step2Deps['verifyFeishuCredentials'];
  fetchImpl?: typeof fetch;
  appApiKey: string;
  tenantTemplateRoot?: string;
  balanceStore: BalanceStore;
  consumptionStore: ConsumptionStore;
};

function resolveDeps(options?: HttpServerOptions): ResolvedHttpDeps {
  // Data persistence directory — all stores write JSON files here when set.
  const dataDir = (process.env['PLATFORM_DATA_DIR'] ?? '').trim();
  const storeOpts = dataDir ? {
    orderStore: options?.orderStore ?? new OrderStore({ filePath: `${dataDir}/orders.json` }),
    balanceStore: new BalanceStore({ filePath: `${dataDir}/balances.json` }),
    consumptionStore: new ConsumptionStore({ filePath: `${dataDir}/consumptions.json` }),
  } : {
    orderStore: options?.orderStore ?? new OrderStore(),
    balanceStore: new BalanceStore(),
    consumptionStore: new ConsumptionStore(),
  };

  const deps: ResolvedHttpDeps = {
    orderStore: storeOpts.orderStore,
    allowlist: options?.allowlist ?? PhoneAllowlist.fromEnvWithInline(),
    allowlistInjected: Boolean(options?.allowlist),
    relayConfigPath: (options?.relayConfigPath ?? process.env['PLATFORM_RELAY_CONFIG_PATH'] ?? '').trim(),
    tenantsRoot: (options?.tenantsRoot ?? process.env['PLATFORM_TENANTS_ROOT'] ?? '').trim(),
    skipRelayReload: Boolean(options?.skipRelayReload),
    webRoot:
      options?.webRoot === false ? '' : (options?.webRoot ?? defaultWebRoot()),
    tenantIdAllocator: options?.tenantIdAllocator,
    relayHttpUrl: (options?.relayHttpUrl ?? process.env['PLATFORM_RELAY_HTTP_URL'] ?? '').trim(),
    verifyFeishuCredentials: options?.verifyFeishuCredentials,
    fetchImpl: options?.fetchImpl,
    appApiKey: (process.env['PLATFORM_APP_API_KEY'] ?? '').trim(),
    ...(options?.tenantTemplateRoot ? { tenantTemplateRoot: options.tenantTemplateRoot } : {}),
    balanceStore: storeOpts.balanceStore,
    consumptionStore: storeOpts.consumptionStore,
  };

  // FR-P0-014 Stage A: when tenants root is configured, eagerly initialise
  // the TenantIdAllocator (sync filesystem scan) and register an `onPaid`
  // listener that copies the tenant-pulse-admin template into
  // `Tenants/<tenantId>/`. The listener fires on every successful markPaid
  // (mock-pay, payment webhook, provider-already-paid recovery), is fully
  // idempotent and synchronous.
  const stageAEnabled = options?.enableStageAMaterialize !== false;
  if (stageAEnabled && deps.tenantsRoot) {
    if (!deps.tenantIdAllocator) {
      const seqA = scanMaxMemberSeqFromTenantsDir(deps.tenantsRoot);
      const seqB = scanMaxMemberSeqFromRelayConfig(deps.relayConfigPath);
      deps.tenantIdAllocator = new TenantIdAllocator(Math.max(seqA, seqB));
    }
    const allocator = deps.tenantIdAllocator;
    deps.orderStore.onPaid((orderId) => {
      // FR-P0-014: materialize tenant workspace
      const r = materializeTenantSpaceForOrder(
        {
          orderStore: deps.orderStore,
          tenantIdAllocator: allocator,
          tenantsRoot: deps.tenantsRoot,
          ...(deps.tenantTemplateRoot ? { templateRoot: deps.tenantTemplateRoot } : {}),
        },
        orderId,
      );
      // FR-P0-020: top-up balance if order has topUpAmount
      const order = deps.orderStore.get(orderId);
      if (order?.tenantId && order.topUpAmount && order.topUpAmount > 0) {
        const isRecharge = order.source === 'recharge';
        // Grayscale test only applies to signup orders (¥0.01 → ¥10 base credit)
        const grayscale = !isRecharge && (process.env['PLATFORM_GRAYSCALE_TEST'] ?? '').trim() === '1';
        const baseCreditYuan = (grayscale && order.topUpAmount === 0.01)
          ? 10
          : order.topUpAmount;
        // Credit multiplier applies to all orders (business model)
        const multiplier = parseFloat(process.env['PLATFORM_CREDIT_MULTIPLIER'] ?? '5') || 5;
        const actualFen = Math.round(order.topUpAmount * 100);
        const creditFen = Math.round(baseCreditYuan * 100 * multiplier);
        deps.balanceStore.createOrTopUp(order.tenantId, actualFen, creditFen);
        console.log(JSON.stringify({
          ts: new Date().toISOString(),
          level: 'info',
          msg: 'balance_top_up',
          orderId,
          source: order.source ?? 'signup',
          paidYuan: order.topUpAmount,
          creditYuan: creditFen / 100,
        }));
      }
      const ts = new Date().toISOString();
      if (r.ok) {
        console.log(
          JSON.stringify({
            ts,
            level: 'info',
            msg: 'tenant_workspace_materialised',
            orderId,
            tenantId: r.tenantId,
            allocated: r.allocated,
            copied: r.copied.length,
            skipped: r.skipped.length,
          }),
        );
      } else {
        console.error(
          JSON.stringify({
            ts,
            level: 'error',
            msg: 'tenant_workspace_materialise_failed',
            orderId,
            error: r.error,
            ...(r.message ? { message: r.message } : {}),
          }),
        );
      }
    });
  }

  return deps;
}

function sendJson(res: ServerResponse, status: number, body: unknown): void {
  res.writeHead(status, JSON_HDR);
  res.end(JSON.stringify(body));
}

function readJsonBody(req: IncomingMessage): Promise<unknown> {
  return new Promise((resolve, reject) => {
    const chunks: Buffer[] = [];
    let len = 0;
    req.on('data', (chunk: Buffer) => {
      len += chunk.length;
      if (len > BODY_LIMIT) {
        reject(new Error('body_too_large'));
        return;
      }
      chunks.push(chunk);
    });
    req.on('end', () => {
      try {
        const raw = Buffer.concat(chunks).toString('utf8').trim();
        if (!raw) {
          resolve({});
          return;
        }
        resolve(JSON.parse(raw) as unknown);
      } catch {
        reject(new Error('invalid_json'));
      }
    });
    req.on('error', reject);
  });
}

async function handleMockPay(
  req: IncomingMessage,
  res: ServerResponse,
  orderStore: OrderStore,
): Promise<void> {
  if (!isMockPayAllowed()) {
    sendJson(res, 403, { error: 'mock_pay_disabled' });
    return;
  }
  let body: unknown;
  try {
    body = await readJsonBody(req);
  } catch (e) {
    if (e instanceof Error && e.message === 'body_too_large') {
      sendJson(res, 413, { error: 'payload_too_large' });
      return;
    }
    sendJson(res, 400, { error: 'invalid_json' });
    return;
  }
  const orderId =
    typeof body === 'object' && body !== null && 'orderId' in body
      ? String((body as { orderId: unknown }).orderId).trim()
      : '';
  if (!orderId) {
    sendJson(res, 400, { error: 'missing_orderId' });
    return;
  }

  const r = orderStore.markPaid(orderId);
  if (r.success) {
    sendJson(res, 200, {
      orderId,
      status: 'paid',
      already: r.already,
    });
    return;
  }
  if (r.error === 'not_found') {
    sendJson(res, 404, { error: 'order_not_found' });
    return;
  }
  sendJson(res, 409, { error: 'invalid_state', currentStatus: r.currentStatus });
}

async function handleCreateOrder(
  req: IncomingMessage,
  res: ServerResponse,
  deps: ResolvedHttpDeps,
): Promise<void> {
  let body: unknown;
  try {
    body = await readJsonBody(req);
  } catch (e) {
    if (e instanceof Error && e.message === 'body_too_large') {
      sendJson(res, 413, { error: 'payload_too_large' });
      return;
    }
    sendJson(res, 400, { error: 'invalid_json' });
    return;
  }
  const obj = (typeof body === 'object' && body !== null) ? (body as Record<string, unknown>) : {};
  const phone = typeof obj.phone === 'string' ? obj.phone.trim() : '';
  const displayName = typeof obj.displayName === 'string' ? obj.displayName.trim() : '';
  const planId = typeof obj.planId === 'string' ? obj.planId.trim() : '';
  const topUpAmount = typeof obj.topUpAmount === 'number' ? obj.topUpAmount : undefined;
  const modelGroup = typeof obj.modelGroup === 'string' ? obj.modelGroup.trim() : undefined;
  if (!phone) {
    sendJson(res, 400, { error: 'missing_phone' });
    return;
  }
  // Reload allowlist from env/file each call unless injected (tests).
  const allowlist = deps.allowlistInjected ? deps.allowlist : PhoneAllowlist.fromEnvWithInline();
  if (!allowlist.isAllowed(phone)) {
    sendJson(res, 403, { error: 'not_allowlisted' });
    return;
  }
  const orderId = deps.orderStore.createPending({
    phone,
    ...(displayName ? { displayName } : {}),
    ...(planId ? { planId } : {}),
    ...(topUpAmount && topUpAmount > 0 ? { topUpAmount } : {}),
    ...(modelGroup ? { modelGroup } : {}),
  });
  sendJson(res, 200, { orderId, status: 'pending', displayName: displayName || undefined, ...(topUpAmount ? { topUpAmount } : {}), ...(modelGroup ? { modelGroup } : {}) });
}

function requireAppApiKey(req: IncomingMessage, deps: ResolvedHttpDeps): boolean {
  if (!deps.appApiKey) return false;
  const header = req.headers['x-platform-api-key'];
  const got = Array.isArray(header) ? header[0] : header;
  return typeof got === 'string' && got.trim() === deps.appApiKey;
}

async function handleOrderRestore(req: IncomingMessage, res: ServerResponse, deps: ResolvedHttpDeps): Promise<void> {
  if (!requireAppApiKey(req, deps)) {
    sendJson(res, 401, { error: 'unauthorized' });
    return;
  }
  let body: unknown;
  try {
    body = await readJsonBody(req);
  } catch (e) {
    if (e instanceof Error && e.message === 'body_too_large') {
      sendJson(res, 413, { error: 'payload_too_large' });
      return;
    }
    sendJson(res, 400, { error: 'invalid_json' });
    return;
  }
  const o = (typeof body === 'object' && body !== null) ? (body as Record<string, unknown>) : {};
  const orderId = typeof o.orderId === 'string' ? o.orderId.trim() : '';
  const phone = typeof o.phone === 'string' ? o.phone.trim() : '';
  const displayName = typeof o.displayName === 'string' ? o.displayName.trim() : '';
  const status = typeof o.status === 'string' ? o.status.trim() : 'paid';
  const tenantId = typeof o.tenantId === 'string' ? o.tenantId.trim() : '';
  const provisioned = typeof o.provisioned === 'boolean' ? o.provisioned : false;
  if (!orderId || !phone) {
    sendJson(res, 400, { error: 'missing_fields' });
    return;
  }
  if (!['pending', 'paid', 'failed', 'refunded'].includes(status)) {
    sendJson(res, 400, { error: 'invalid_status' });
    return;
  }
  deps.orderStore.restore({
    id: orderId,
    phone,
    ...(displayName ? { displayName } : {}),
    status: status as 'pending' | 'paid' | 'failed' | 'refunded',
    ...(tenantId ? { tenantId } : {}),
    paidAt: status === 'paid' ? Date.now() : undefined,
    provisionedAt: provisioned ? Date.now() : undefined,
  });
  const restored = deps.orderStore.get(orderId);
  sendJson(res, 200, { ok: true, orderId, restored });
}

async function handleAllowlistAdd(req: IncomingMessage, res: ServerResponse, deps: ResolvedHttpDeps): Promise<void> {
  if (!requireAppApiKey(req, deps)) {
    sendJson(res, 401, { error: 'unauthorized' });
    return;
  }
  let body: unknown;
  try {
    body = await readJsonBody(req);
  } catch (e) {
    if (e instanceof Error && e.message === 'body_too_large') {
      sendJson(res, 413, { error: 'payload_too_large' });
      return;
    }
    sendJson(res, 400, { error: 'invalid_json' });
    return;
  }
  const o = (typeof body === 'object' && body !== null) ? (body as Record<string, unknown>) : {};
  const phone = typeof o.phone === 'string' ? o.phone.trim() : '';
  if (!phone) {
    sendJson(res, 400, { error: 'missing_phone' });
    return;
  }
  const path = (process.env['PLATFORM_PHONE_ALLOWLIST_PATH'] ?? '').trim();
  const outcome = await addPhoneToAllowlistFile({ allowlistPath: path, phone });
  if (!outcome.ok) {
    const status =
      outcome.error === 'allowlist_path_unset' ? 503
        : outcome.error === 'invalid_phone' ? 400
          : 502;
    sendJson(res, status, { ok: false, error: outcome.error, message: outcome.message });
    return;
  }
  // On success, reload allowlist for subsequent requests.
  if (!deps.allowlistInjected) deps.allowlist = PhoneAllowlist.fromEnvWithInline();
  sendJson(res, 200, { ok: true, already: outcome.already, normalized: outcome.normalized });
}

async function ensureTenantIdAllocator(deps: ResolvedHttpDeps): Promise<TenantIdAllocator> {
  if (deps.tenantIdAllocator) return deps.tenantIdAllocator;
  const a = await TenantIdAllocator.fromEnvironment({
    tenantsRoot: deps.tenantsRoot,
    relayConfigPath: deps.relayConfigPath,
  });
  deps.tenantIdAllocator = a;
  return a;
}

function step2StatusCode(outcome: Step2Outcome): number {
  if (outcome.ok) return 200;
  switch (outcome.error) {
    case 'order_not_found': return 404;
    case 'not_paid': return 409;
    case 'not_allowlisted': return 403;
    case 'invalid_feishu_credentials': return 400;
    case 'feishu_unreachable':
    case 'feishu_unexpected_response': return 502;
    case 'relay_error':
      return (outcome.message?.startsWith('feishu_app_id_in_use')) ? 409 : 502;
    default: return 500;
  }
}

async function handleStep2Provision(
  req: IncomingMessage,
  res: ServerResponse,
  deps: ResolvedHttpDeps,
): Promise<void> {
  if (!deps.relayConfigPath || !deps.tenantsRoot) {
    sendJson(res, 503, { error: 'provision_unconfigured' });
    return;
  }
  let body: unknown;
  try {
    body = await readJsonBody(req);
  } catch (e) {
    if (e instanceof Error && e.message === 'body_too_large') {
      sendJson(res, 413, { error: 'payload_too_large' });
      return;
    }
    sendJson(res, 400, { error: 'invalid_json' });
    return;
  }
  const o = (typeof body === 'object' && body !== null) ? (body as Record<string, unknown>) : {};
  const orderId = typeof o.orderId === 'string' ? o.orderId.trim() : '';
  const feishuAppId = typeof o.feishuAppId === 'string' ? o.feishuAppId.trim() : '';
  const feishuAppSecret = typeof o.feishuAppSecret === 'string' ? o.feishuAppSecret.trim() : '';
  if (!orderId || !feishuAppId || !feishuAppSecret) {
    sendJson(res, 400, { error: 'missing_fields' });
    return;
  }

  const allocator = await ensureTenantIdAllocator(deps);
  const outcome = await provisionFeishuStep2(
    {
      orderStore: deps.orderStore,
      allowlist: deps.allowlist,
      relayConfigPath: deps.relayConfigPath,
      tenantsRoot: deps.tenantsRoot,
      tenantIdAllocator: allocator,
      relayHttpUrl: deps.relayHttpUrl || undefined,
      skipReload: deps.skipRelayReload || undefined,
      verifyFeishuCredentials: deps.verifyFeishuCredentials,
      fetchImpl: deps.fetchImpl,
    },
    { orderId, feishuAppId, feishuAppSecret },
  );

  const status = step2StatusCode(outcome);
  if (outcome.ok) {
    sendJson(res, status, {
      ok: true,
      already: outcome.already,
      tenantId: outcome.tenantId,
      agentId: outcome.agentId,
      principalId: outcome.principalId,
      bindingId: outcome.bindingId,
      agentName: outcome.agentName,
    });
    return;
  }
  sendJson(res, status, {
    ok: false,
    error: outcome.error,
    ...(outcome.feishuCode !== undefined ? { feishuCode: outcome.feishuCode } : {}),
    ...(outcome.feishuMsg !== undefined ? { feishuMsg: outcome.feishuMsg } : {}),
    ...(outcome.message !== undefined ? { message: outcome.message } : {}),
  });
}

function step3StatusCode(outcome: Step3Outcome): number {
  if (outcome.ok) return 200;
  switch (outcome.error) {
    case 'order_not_found': return 404;
    case 'not_provisioned': return 409;
    case 'pair_code_unknown': return 404;
    case 'pair_agent_mismatch': return 409;
    case 'pair_config_missing_agent': return 404;
    case 'pair_relay_unreachable': return 502;
    default: return 500;
  }
}

async function handleStep3Pair(
  req: IncomingMessage,
  res: ServerResponse,
  deps: ResolvedHttpDeps,
): Promise<void> {
  if (!deps.relayHttpUrl) {
    sendJson(res, 503, { error: 'pair_unconfigured' });
    return;
  }
  let body: unknown;
  try {
    body = await readJsonBody(req);
  } catch (e) {
    if (e instanceof Error && e.message === 'body_too_large') {
      sendJson(res, 413, { error: 'payload_too_large' });
      return;
    }
    sendJson(res, 400, { error: 'invalid_json' });
    return;
  }
  const o = (typeof body === 'object' && body !== null) ? (body as Record<string, unknown>) : {};
  const orderId = typeof o.orderId === 'string' ? o.orderId.trim() : '';
  const code = typeof o.code === 'string' ? o.code.trim() : '';
  if (!orderId || !code) {
    sendJson(res, 400, { error: 'missing_fields' });
    return;
  }

  const outcome = await pairFeishuStep3(
    { orderStore: deps.orderStore, relayHttpUrl: deps.relayHttpUrl, fetchImpl: deps.fetchImpl },
    { orderId, code },
  );
  const status = step3StatusCode(outcome);
  if (outcome.ok) {
    sendJson(res, status, {
      ok: true,
      tenantId: outcome.tenantId,
      agentId: outcome.agentId,
      principalId: outcome.principalId,
      openId: outcome.openId,
    });
    return;
  }
  sendJson(res, status, {
    ok: false,
    error: outcome.error,
    ...('expectedAgentId' in outcome && outcome.expectedAgentId ? { expectedAgentId: outcome.expectedAgentId } : {}),
    ...('detail' in outcome && outcome.detail ? { detail: outcome.detail } : {}),
  });
}

async function handleRelayReload(req: IncomingMessage, res: ServerResponse, deps: ResolvedHttpDeps): Promise<void> {
  if (!deps.relayHttpUrl) {
    sendJson(res, 503, { ok: false, error: 'relay_unconfigured' });
    return;
  }
  const r = await reloadRelayAgents({ baseUrl: deps.relayHttpUrl, fetchImpl: deps.fetchImpl });
  if (r.ok) {
    sendJson(res, 200, { ok: true, added: r.added, total: r.total });
    return;
  }
  if (r.error === 'relay_unreachable') {
    sendJson(res, 502, { ok: false, error: 'relay_unreachable', detail: r.detail });
    return;
  }
  if (r.error === 'reload_failed') {
    sendJson(res, 502, { ok: false, error: 'reload_failed', message: r.message });
    return;
  }
  sendJson(res, 502, { ok: false, error: 'unexpected', status: 'status' in r ? r.status : undefined });
}

async function handlePaymentCreateOrder(
  req: IncomingMessage,
  res: ServerResponse,
  deps: ResolvedHttpDeps,
): Promise<void> {
  const raw = req.url ?? '';
  if (process.env['PLATFORM_PAYMENT_PROXY_URL']?.trim()) {
    try {
      const { status, contentType, body } = await proxyPaymentCreateOrder(raw);
      res.writeHead(status, { 'Content-Type': contentType });
      res.end(body);
    } catch {
      sendJson(res, 200, { success: false, error: 'payment_proxy_failed' });
    }
    return;
  }
  if (isAppPlatformPaymentConfigured()) {
    try {
      const result = await createAppPlatformNativePayment({
        rawUrl: raw,
        orderStore: deps.orderStore,
        allowlist: deps.allowlist,
      });
      sendJson(res, 200, result);
    } catch (e) {
      const message = e instanceof Error ? e.message : String(e);
      sendJson(res, 200, { success: false, error: 'payment_internal_error', message });
    }
    return;
  }
  try {
    const result = await createOrderNativePayment({
      rawUrl: raw,
      orderStore: deps.orderStore,
      allowlist: deps.allowlist,
    });
    sendJson(res, 200, result);
  } catch (e) {
    const message = e instanceof Error ? e.message : String(e);
    sendJson(res, 200, { success: false, error: 'payment_internal_error', message });
  }
}

function readRawBodyBuffer(req: IncomingMessage): Promise<Buffer> {
  return new Promise((resolve, reject) => {
    const chunks: Buffer[] = [];
    let len = 0;
    req.on('data', (chunk: Buffer) => {
      len += chunk.length;
      if (len > BODY_LIMIT) {
        reject(new Error('body_too_large'));
        return;
      }
      chunks.push(chunk);
    });
    req.on('end', () => {
      resolve(Buffer.concat(chunks));
    });
    req.on('error', reject);
  });
}

async function handleAppPlatformWebhook(
  req: IncomingMessage,
  res: ServerResponse,
  deps: ResolvedHttpDeps,
): Promise<void> {
  let raw: Buffer;
  try {
    raw = await readRawBodyBuffer(req);
  } catch {
    sendJson(res, 413, { error: 'payload_too_large' });
    return;
  }
  const secret = (process.env['PLATFORM_APP_WEBHOOK_SECRET'] ?? '').trim();
  const sig = req.headers['x-webhook-signature'];
  if (secret && !verifyAppPlatformWebhookSignature(raw, sig, secret)) {
    sendJson(res, 401, { error: 'invalid_webhook_signature' });
    return;
  }
  let body: unknown;
  try {
    const t = raw.toString('utf8').trim();
    body = t ? (JSON.parse(t) as unknown) : {};
  } catch {
    sendJson(res, 400, { error: 'invalid_json' });
    return;
  }
  const result = applyAppPlatformWebhookToOrderStore(body, deps.orderStore);
  if (!result.ok) {
    sendJson(res, 404, { error: result.error });
    return;
  }
  sendJson(res, 200, { success: true, received: true });
}

/** v0.5.0: POST /internal/subscription/create — create subscription via App Platform. */
async function handleSubscriptionCreate(
  req: IncomingMessage,
  res: ServerResponse,
  deps: ResolvedHttpDeps,
): Promise<void> {
  let body: unknown;
  try {
    body = await readJsonBody(req);
  } catch (e) {
    if (e instanceof Error && e.message === 'body_too_large') {
      sendJson(res, 413, { error: 'payload_too_large' });
      return;
    }
    sendJson(res, 400, { error: 'invalid_json' });
    return;
  }
  const o = (typeof body === 'object' && body !== null) ? (body as Record<string, unknown>) : {};
  const phone = typeof o.phone === 'string' ? o.phone.trim() : '';
  const displayName = typeof o.displayName === 'string' ? o.displayName.trim() : '';
  const planId = typeof o.planId === 'string' ? o.planId.trim() : 'monthly-auto';
  if (!phone) {
    sendJson(res, 400, { error: 'missing_phone' });
    return;
  }
  const allowlist = deps.allowlistInjected ? deps.allowlist : PhoneAllowlist.fromEnvWithInline();
  if (!allowlist.isAllowed(phone)) {
    sendJson(res, 403, { error: 'not_allowlisted' });
    return;
  }

  // Create pending order with planId
  const orderId = deps.orderStore.createPending({
    phone,
    ...(displayName ? { displayName } : {}),
    planId,
  });

  // Build notify URL
  const publicBase = (process.env['PLATFORM_PUBLIC_BASE_URL'] ?? '').trim();
  const notifyUrl = publicBase
    ? `${publicBase.replace(/\/$/, '')}/internal/webhooks/subscription`
    : undefined;

  try {
    const result = await createSubscription(
      { env: process.env },
      { phone, displayName: displayName || undefined, planId, outOrderId: orderId, notifyUrl },
    );
    deps.orderStore.setSubscriptionId(orderId, result.subscriptionId);
    sendJson(res, 200, {
      orderId,
      subscriptionId: result.subscriptionId,
      ...(result.signUrl ? { signUrl: result.signUrl } : {}),
      ...(result.signQrcodeBase64 ? { signQrcodeBase64: result.signQrcodeBase64 } : {}),
    });
  } catch (e) {
    const message = e instanceof Error ? e.message : String(e);
    console.error(JSON.stringify({ ts: new Date().toISOString(), level: 'error', msg: 'subscription_create_failed', orderId, error: message }));
    sendJson(res, 502, { error: 'subscription_create_failed', message });
  }
}

/** v0.5.0: POST /internal/webhooks/subscription — receive subscription events from App Platform. */
async function handleSubscriptionWebhook(
  req: IncomingMessage,
  res: ServerResponse,
  deps: ResolvedHttpDeps,
): Promise<void> {
  let raw: Buffer;
  try {
    raw = await readRawBodyBuffer(req);
  } catch {
    sendJson(res, 413, { error: 'payload_too_large' });
    return;
  }
  const secret = (process.env['PLATFORM_APP_WEBHOOK_SECRET'] ?? '').trim();
  const sig = req.headers['x-webhook-signature'];
  if (secret && !verifyAppPlatformWebhookSignature(raw, sig, secret)) {
    sendJson(res, 401, { error: 'invalid_webhook_signature' });
    return;
  }
  let body: unknown;
  try {
    const t = raw.toString('utf8').trim();
    body = t ? (JSON.parse(t) as unknown) : {};
  } catch {
    sendJson(res, 400, { error: 'invalid_json' });
    return;
  }

  const b = (typeof body === 'object' && body !== null) ? (body as Record<string, unknown>) : {};
  const event = typeof b['event'] === 'string' ? b['event'] as string : '';
  const orderId = typeof b['out_order_id'] === 'string' ? (b['out_order_id'] as string).trim() : '';

  console.log(JSON.stringify({ ts: new Date().toISOString(), level: 'info', msg: 'subscription_webhook', event, orderId }));

  // subscription.activated → mark order as paid (triggers Stage A materialization)
  if ((event === 'subscription.activated' || event === 'subscription.renewed') && orderId) {
    const r = deps.orderStore.markPaid(orderId);
    if (!r.success && r.error === 'not_found') {
      sendJson(res, 404, { error: 'order_not_found' });
      return;
    }
    sendJson(res, 200, { success: true, received: true, event });
    return;
  }

  // Other events: acknowledge but don't change order state yet
  sendJson(res, 200, { success: true, received: true, event });
}

// --- Dashboard token store (in-memory, 24h TTL) ---
const DASHBOARD_TOKEN_TTL_MS = 24 * 60 * 60 * 1000;
interface DashboardTokenEntry { tenantId: string; createdAt: number; expiresAt: number; }
const dashboardTokens = new Map<string, DashboardTokenEntry>();

function generateDashboardToken(tenantId: string): string {
  // Prune expired tokens
  const now = Date.now();
  for (const [tok, entry] of dashboardTokens) {
    if (entry.expiresAt <= now) dashboardTokens.delete(tok);
  }
  const token = randomUUID();
  dashboardTokens.set(token, { tenantId, createdAt: now, expiresAt: now + DASHBOARD_TOKEN_TTL_MS });
  return token;
}

function verifyDashboardToken(token: string): DashboardTokenEntry | null {
  const entry = dashboardTokens.get(token);
  if (!entry) return null;
  if (entry.expiresAt <= Date.now()) {
    dashboardTokens.delete(token);
    return null;
  }
  return entry;
}

/** POST /api/auth/dashboard-token — generate a 24h dashboard token for a tenant. */
async function handleDashboardTokenCreate(
  req: IncomingMessage,
  res: ServerResponse,
  deps: ResolvedHttpDeps,
): Promise<void> {
  let body: unknown;
  try {
    body = await readJsonBody(req);
  } catch (e) {
    if (e instanceof Error && e.message === 'body_too_large') {
      sendJson(res, 413, { error: 'payload_too_large' });
      return;
    }
    sendJson(res, 400, { error: 'invalid_json' });
    return;
  }
  const o = (typeof body === 'object' && body !== null) ? (body as Record<string, unknown>) : {};
  const tenantId = typeof o.tenantId === 'string' ? o.tenantId.trim() : '';
  if (!tenantId) {
    sendJson(res, 400, { error: 'missing_tenantId' });
    return;
  }
  // Token generation is trust-on-first-use: Relay has already authenticated
  // the user. Platform may have no billing records yet (fresh restart / new
  // tenant) — that's fine; the dashboard will simply show "no data".
  const token = generateDashboardToken(tenantId);
  const publicBaseUrl = (process.env['PLATFORM_PUBLIC_BASE_URL'] ?? 'https://bigbird.online/platform').trim();
  const dashboardUrl = publicBaseUrl
    ? `${publicBaseUrl.replace(/\/$/, '')}/#billing?token=${encodeURIComponent(token)}`
    : '';
  sendJson(res, 200, {
    token,
    dashboardUrl,
    // Always include the bare token so Relay can build a URL if it has its own base
    barePath: `#billing?token=${encodeURIComponent(token)}`,
    expiresInMs: DASHBOARD_TOKEN_TTL_MS,
  });
}

/** GET /api/auth/verify-token — verify a dashboard token and return tenantId. */
function handleDashboardTokenVerify(
  req: IncomingMessage,
  res: ServerResponse,
): void {
  const url = new URL(req.url ?? '', `http://${req.headers.host ?? 'localhost'}`);
  const token = url.searchParams.get('token')?.trim() ?? '';
  if (!token) {
    sendJson(res, 400, { error: 'missing_token' });
    return;
  }
  const entry = verifyDashboardToken(token);
  if (!entry) {
    sendJson(res, 401, { error: 'token_invalid_or_expired' });
    return;
  }
  sendJson(res, 200, { tenantId: entry.tenantId, expiresInMs: entry.expiresAt - Date.now() });
}

/** v0.6.0: POST /internal/balance/deduct — deduct tenant balance (called by Relay metering). */
async function handleBalanceDeduct(
  req: IncomingMessage,
  res: ServerResponse,
  deps: ResolvedHttpDeps,
): Promise<void> {
  let body: unknown;
  try {
    body = await readJsonBody(req);
  } catch (e) {
    if (e instanceof Error && e.message === 'body_too_large') {
      sendJson(res, 413, { error: 'payload_too_large' });
      return;
    }
    sendJson(res, 400, { error: 'invalid_json' });
    return;
  }
  const b = (typeof body === 'object' && body !== null) ? (body as Record<string, unknown>) : {};
  const tenantId = typeof b['tenantId'] === 'string' ? (b['tenantId'] as string).trim() : '';
  const costFen = typeof b['costFen'] === 'number' ? b['costFen'] as number : 0;
  if (!tenantId) { sendJson(res, 400, { error: 'missing_tenantId' }); return; }
  if (costFen <= 0) { sendJson(res, 200, { ok: true, newBalance: deps.balanceStore.getBalance(tenantId) ?? 0 }); return; }

  const result = deps.balanceStore.deduct(tenantId, costFen);
  if (!result.ok) {
    // Shadow accounts (admin/non-billed, totalRecharged=0) still track totalConsumed even
    // with insufficient balance. Include `tracked: true` so Relay knows consumption was
    // recorded and doesn't treat this as a real billing failure.
    const tracked = result.error === 'insufficient_balance' && result.shadow === true;
    sendJson(res, 200, { ok: false, error: result.error, ...(result.error === 'insufficient_balance' ? { balance: result.balance, ...(tracked ? { tracked: true } : {}) } : {}) });
    return;
  }

  // Record consumption detail for billing transparency
  const description = typeof b['description'] === 'string' ? b['description'] : '模型调用消费';
  const metadata = typeof b['metadata'] === 'object' && b['metadata'] !== null ? b['metadata'] as Record<string, unknown> : undefined;
  deps.consumptionStore.record({
    tenantId,
    amountFen: costFen,
    description,
    ...(metadata ? {
      metadata: {
        modelId: typeof metadata['modelId'] === 'string' ? metadata['modelId'] : undefined,
        tokensIn: typeof metadata['tokensIn'] === 'number' ? metadata['tokensIn'] : undefined,
        tokensOut: typeof metadata['tokensOut'] === 'number' ? metadata['tokensOut'] : undefined,
        sessionId: typeof metadata['sessionId'] === 'string' ? metadata['sessionId'] : undefined,
      },
    } : {}),
  });

  sendJson(res, 200, { ok: true, newBalance: result.newBalance });
}

/** v0.7.0: GET /api/billing/balance — Tenant billing balance with summary. */
async function handleBillingBalance(
  req: IncomingMessage,
  res: ServerResponse,
  deps: ResolvedHttpDeps,
): Promise<void> {
  const url = new URL(req.url ?? '', `http://${req.headers.host ?? 'localhost'}`);
  const tenantId = url.searchParams.get('tenantId')?.trim() ?? '';
  if (!tenantId) {
    sendJson(res, 400, { error: 'missing_tenantId' });
    return;
  }

  const account = deps.balanceStore.getAccount(tenantId);
  const summary = deps.consumptionStore.getSummary(tenantId);

  // Look up phone from tenant's most recent paid order (needed for recharge flow)
  const allOrders = deps.orderStore.listAll?.() ?? { items: [] };
  const tenantOrders = allOrders.items
    .filter((o: any) => o.tenantId === tenantId && o.status === 'paid')
    .sort((a: any, b: any) => (b.paidAt ?? 0) - (a.paidAt ?? 0));
  const phone = tenantOrders.length > 0 ? tenantOrders[0].phone : undefined;

  sendJson(res, 200, {
    tenantId,
    balance: account?.balance ?? 0,
    totalRecharged: account?.totalRecharged ?? 0,
    totalConsumed: account?.totalConsumed ?? 0,
    consumptionCount: summary.count,
    lastConsumptionAt: summary.lastConsumptionAt,
    phone,
    exists: account !== undefined,
  });
}

/** v0.7.0: GET /api/billing/recharges — Tenant recharge history. */
async function handleBillingRecharges(
  req: IncomingMessage,
  res: ServerResponse,
  deps: ResolvedHttpDeps,
): Promise<void> {
  const url = new URL(req.url ?? '', `http://${req.headers.host ?? 'localhost'}`);
  const tenantId = url.searchParams.get('tenantId')?.trim() ?? '';
  const limit = parseInt(url.searchParams.get('limit') ?? '20', 10);
  const offset = parseInt(url.searchParams.get('offset') ?? '0', 10);

  if (!tenantId) {
    sendJson(res, 400, { error: 'missing_tenantId' });
    return;
  }

  // Get all orders for tenant and filter paid ones with topUpAmount
  const allOrders = deps.orderStore.listAll?.() ?? { items: [] };
  const recharges = allOrders.items
    .filter((o: any) => o.tenantId === tenantId && o.status === 'paid' && (o.topUpAmount ?? 0) > 0)
    .map((o: any) => ({
      orderId: o.id,
      amountYuan: o.topUpAmount,
      paidAt: o.paidAt,
      status: o.status,
    }))
    .sort((a: any, b: any) => (b.paidAt ?? 0) - (a.paidAt ?? 0));

  const total = recharges.length;
  const items = recharges.slice(offset, offset + limit);

  sendJson(res, 200, { items, total, limit, offset });
}

/** v0.7.0: GET /api/billing/consumptions — Tenant consumption history. */
async function handleBillingConsumptions(
  req: IncomingMessage,
  res: ServerResponse,
  deps: ResolvedHttpDeps,
): Promise<void> {
  const url = new URL(req.url ?? '', `http://${req.headers.host ?? 'localhost'}`);
  const tenantId = url.searchParams.get('tenantId')?.trim() ?? '';
  const limit = parseInt(url.searchParams.get('limit') ?? '20', 10);
  const offset = parseInt(url.searchParams.get('offset') ?? '0', 10);

  if (!tenantId) {
    sendJson(res, 400, { error: 'missing_tenantId' });
    return;
  }

  const result = deps.consumptionStore.list({ tenantId, limit, offset });
  sendJson(res, 200, result);
}

/** v0.8.0: POST /api/billing/recharge — create recharge order for existing tenant (no phone required). */
async function handleBillingRecharge(
  req: IncomingMessage,
  res: ServerResponse,
  deps: ResolvedHttpDeps,
): Promise<void> {
  let body: unknown;
  try {
    body = await readJsonBody(req);
  } catch {
    sendJson(res, 400, { error: 'invalid_json' });
    return;
  }
  const obj = (typeof body === 'object' && body !== null) ? (body as Record<string, unknown>) : {};
  const tenantId = typeof obj.tenantId === 'string' ? obj.tenantId.trim() : '';
  const topUpAmount = typeof obj.topUpAmount === 'number' ? obj.topUpAmount : undefined;

  if (!tenantId) {
    sendJson(res, 400, { error: 'missing_tenantId' });
    return;
  }
  if (!topUpAmount || topUpAmount <= 0) {
    sendJson(res, 400, { error: 'missing_topUpAmount' });
    return;
  }

  // Look up phone from tenant's most recent paid order
  const allOrders = deps.orderStore.listAll?.() ?? { items: [] };
  const tenantOrders = allOrders.items
    .filter((o: any) => o.tenantId === tenantId && o.status === 'paid')
    .sort((a: any, b: any) => (b.paidAt ?? 0) - (a.paidAt ?? 0));
  if (tenantOrders.length === 0) {
    sendJson(res, 404, { error: 'no_previous_order' });
    return;
  }
  const phone = tenantOrders[0].phone;

  const orderId = deps.orderStore.createPending({
    phone,
    topUpAmount,
    modelGroup: 'domestic',
    source: 'recharge',
  });
  // Set tenantId immediately so onPaid can credit the right account
  deps.orderStore.setTenantId(orderId, tenantId);

  sendJson(res, 200, { orderId, status: 'pending', phone, topUpAmount });
}

/** v0.7.0: GET /api/admin/billing/summary — Admin billing dashboard. */
async function handleAdminBillingSummary(
  _req: IncomingMessage,
  res: ServerResponse,
  deps: ResolvedHttpDeps,
): Promise<void> {
  const globalSummary = deps.consumptionStore.getGlobalSummary();
  const accounts = deps.balanceStore.listAccounts();

  // Calculate today's stats
  const now = Date.now();
  const dayStart = now - 24 * 60 * 60 * 1000;
  const weekStart = now - 7 * 24 * 60 * 60 * 1000;
  const monthStart = now - 30 * 24 * 60 * 60 * 1000;

  // Aggregate recharges by time period
  const allOrders = deps.orderStore.listAll?.() ?? { items: [] };
  const paidOrders = allOrders.items.filter((o: any) => o.status === 'paid' && (o.topUpAmount ?? 0) > 0);

  const todayRecharge = paidOrders
    .filter((o: any) => (o.paidAt ?? 0) >= dayStart)
    .reduce((sum: number, o: any) => sum + (o.topUpAmount ?? 0), 0);

  const weekRecharge = paidOrders
    .filter((o: any) => (o.paidAt ?? 0) >= weekStart)
    .reduce((sum: number, o: any) => sum + (o.topUpAmount ?? 0), 0);

  const monthRecharge = paidOrders
    .filter((o: any) => (o.paidAt ?? 0) >= monthStart)
    .reduce((sum: number, o: any) => sum + (o.topUpAmount ?? 0), 0);

  sendJson(res, 200, {
    overview: {
      totalBalanceFen: accounts.reduce((sum, a) => sum + a.balance, 0),
      totalRechargedFen: accounts.reduce((sum, a) => sum + a.totalRecharged, 0),
      totalConsumedFen: globalSummary.totalConsumedFen,
      tenantCount: accounts.length,
      activeTenantCount: globalSummary.tenantCount,
      transactionCount: globalSummary.totalTransactions,
    },
    recharges: {
      todayYuan: todayRecharge,
      weekYuan: weekRecharge,
      monthYuan: monthRecharge,
      totalYuan: paidOrders.reduce((sum: number, o: any) => sum + (o.topUpAmount ?? 0), 0),
    },
    tenants: accounts.map(a => ({
      tenantId: a.tenantId,
      balanceFen: a.balance,
      totalRechargedFen: a.totalRecharged,
      totalConsumedFen: a.totalConsumed,
    })),
  });
}

function routePath(url: string): string {
  const raw = url.split('?')[0] ?? '';
  // Support nginx subpath deployment: /platform/* → same handlers as /*.
  if (raw === '/platform') return '/';
  if (raw.startsWith('/platform/')) return raw.slice('/platform'.length) || '/';
  return raw;
}

async function route(req: IncomingMessage, res: ServerResponse, deps: ResolvedHttpDeps): Promise<void> {
  const url = routePath(req.url ?? '');
  if (req.method === 'GET' && (url === '/health' || url === '/health/')) {
    sendJson(res, 200, healthPayload());
    return;
  }
  if (req.method === 'GET' && (url === '/ping' || url === '/ping/')) {
    sendJson(res, 200, pingPayload());
    return;
  }
  if (req.method === 'GET' && (url === '/internal/capabilities' || url === '/internal/capabilities/')) {
    const grayscaleEnabled = (process.env['PLATFORM_GRAYSCALE_TEST'] ?? '').trim() === '1';
    const multiplier = parseFloat(process.env['PLATFORM_CREDIT_MULTIPLIER'] ?? '5') || 5;
    const monthlyMinimumYuan = parseFloat(process.env['PLATFORM_MONTHLY_MINIMUM'] ?? '100') || 100;
    const rawPresets: number[] = grayscaleEnabled ? [0.01, 50, 100, 200, 500] : [50, 100, 200, 500];
    const topUpPresets = rawPresets.map(amount => {
      const credit = grayscaleEnabled && amount === 0.01
        ? Math.round(10 * 100 * multiplier) / 100   // ¥0.01 test → ¥10 base × multiplier
        : Math.round(amount * 100 * multiplier) / 100; // normal presets
      return { amount, credit, bonus: Math.round((credit - amount) * 100) / 100 };
    });
    sendJson(res, 200, {
      mockPay: isMockPayAllowed(),
      provisionConfigured: Boolean(deps.relayConfigPath && deps.tenantsRoot),
      pairingConfigured: Boolean(deps.relayHttpUrl),
      nativePayment: isNativePaymentEnabled(),
      appPlatformPayment: isAppPlatformPaymentConfigured(),
      paymentInstructions: '请使用微信扫码支付（预付费充值）',
      paymentRefreshInterval: 3,
      billingModel: 'prepaid' as const,
      topUpPresets,
      minTopUpYuan: grayscaleEnabled ? 0.01 : 50,
      creditMultiplier: multiplier,
      monthlyMinimumYuan,
      billingNotice: `充值按 ${multiplier} 倍到账，每月最低充值 ¥${monthlyMinimumYuan}，不足部分月末按充值额补扣。当前倍率 ×${multiplier}，充值即锁定当期倍率。`,
      modelGroups: [
        { id: 'domestic', display_name: '国内模型', description: 'DeepSeek（经 OpenRouter）', pricing_tier: '经济型' },
        { id: 'foreign', display_name: '国外模型', description: 'Claude Sonnet / Opus / Haiku', pricing_tier: '高性能型' },
      ],
      paymentScenarios: {
        member: {
          title: '余额充值',
          description: '预付费按量扣减',
        },
        ...(grayscaleEnabled ? {
          test: {
            title: '支付测试',
            amount: '¥0.01',
            description: '测试支付',
          },
        } : {}),
      },
      subscriptionPlans: [],
    });
    return;
  }
  if (req.method === 'GET' && (url === '/api/payment/create-order' || url === '/api/payment/create-order/')) {
    await handlePaymentCreateOrder(req, res, deps);
    return;
  }
  if (
    req.method === 'POST' &&
    (url === '/internal/mock-pay' || url === '/internal/mock-pay/')
  ) {
    await handleMockPay(req, res, deps.orderStore);
    return;
  }
  if (req.method === 'POST' && (url === '/internal/orders' || url === '/internal/orders/')) {
    await handleCreateOrder(req, res, deps);
    return;
  }
  if (req.method === 'POST' && (url === '/internal/orders/restore' || url === '/internal/orders/restore/')) {
    await handleOrderRestore(req, res, deps);
    return;
  }
  if (req.method === 'POST' && (url === '/internal/allowlist/add' || url === '/internal/allowlist/add/')) {
    await handleAllowlistAdd(req, res, deps);
    return;
  }
  if (req.method === 'GET' && url.startsWith('/internal/orders/')) {
    const id = decodeURIComponent(url.slice('/internal/orders/'.length).replace(/\/$/, ''));
    if (!id) { sendJson(res, 400, { error: 'missing_orderId' }); return; }
    let order = deps.orderStore.get(id);
    if (!order) { sendJson(res, 404, { error: 'order_not_found' }); return; }
    // If still pending, actively sync payment status from App Platform
    // to compensate for unreliable webhook callbacks.
    if (order.status === 'pending' && isAppPlatformPaymentConfigured()) {
      const sync = await syncPaymentStatusFromProvider({
        orderId: id,
        orderStore: deps.orderStore,
        fetchImpl: deps.fetchImpl,
      });
      if (sync.paid) {
        order = deps.orderStore.get(id)!;
      }
    }
    sendJson(res, 200, {
      orderId: order.id,
      status: order.status,
      displayName: order.displayName,
      tenantId: order.tenantId,
      provisioned: order.provisionedAt != null,
      subscriptionId: order.subscriptionId,
      planId: order.planId,
      topUpAmount: order.topUpAmount,
      modelGroup: order.modelGroup,
    });
    return;
  }
  if (req.method === 'POST' && (url === '/internal/provision' || url === '/internal/provision/')) {
    await handleStep2Provision(req, res, deps);
    return;
  }
  if (req.method === 'POST' && (url === '/internal/pair' || url === '/internal/pair/')) {
    await handleStep3Pair(req, res, deps);
    return;
  }
  if (req.method === 'POST' && (url === '/internal/relay/reload' || url === '/internal/relay/reload/')) {
    await handleRelayReload(req, res, deps);
    return;
  }
  if (
    req.method === 'POST' &&
    (url === '/internal/webhooks/app-platform-payment' || url === '/internal/webhooks/app-platform-payment/')
  ) {
    await handleAppPlatformWebhook(req, res, deps);
    return;
  }
  if (
    req.method === 'POST' &&
    (url === '/internal/subscription/create' || url === '/internal/subscription/create/')
  ) {
    // v0.6.0: subscription deprecated, kept for backward compat (no-op)
    sendJson(res, 410, { error: 'subscription_deprecated', message: 'Use top-up balance flow instead' });
    return;
  }
  if (
    req.method === 'POST' &&
    (url === '/internal/webhooks/subscription' || url === '/internal/webhooks/subscription/')
  ) {
    // v0.6.0: subscription webhook deprecated, acknowledge only
    sendJson(res, 200, { success: true, received: true, deprecated: true });
    return;
  }
  // v0.7.0: Dashboard token auth
  if (req.method === 'POST' && (url === '/api/auth/dashboard-token' || url === '/api/auth/dashboard-token/')) {
    await handleDashboardTokenCreate(req, res, deps);
    return;
  }
  if (req.method === 'GET' && (url === '/api/auth/verify-token' || url === '/api/auth/verify-token/')) {
    handleDashboardTokenVerify(req, res);
    return;
  }
  // v0.7.0: Billing Dashboard API (tenant-billing-dashboard)
  if (req.method === 'GET' && (url === '/api/billing/balance' || url === '/api/billing/balance/')) {
    await handleBillingBalance(req, res, deps);
    return;
  }
  if (req.method === 'GET' && (url === '/api/billing/recharges' || url === '/api/billing/recharges/')) {
    await handleBillingRecharges(req, res, deps);
    return;
  }
  if (req.method === 'GET' && (url === '/api/billing/consumptions' || url === '/api/billing/consumptions/')) {
    await handleBillingConsumptions(req, res, deps);
    return;
  }
  // v0.8.0: POST /api/billing/recharge — create recharge order for existing tenant
  if (req.method === 'POST' && (url === '/api/billing/recharge' || url === '/api/billing/recharge/')) {
    await handleBillingRecharge(req, res, deps);
    return;
  }
  if (req.method === 'GET' && (url === '/api/admin/billing/summary' || url === '/api/admin/billing/summary/')) {
    await handleAdminBillingSummary(req, res, deps);
    return;
  }

  // v0.6.0: Balance API (FR-P0-020)
  if (req.method === 'GET' && url.startsWith('/internal/balance/')) {
    const tenantId = decodeURIComponent(url.slice('/internal/balance/'.length).replace(/\/$/, ''));
    if (!tenantId) { sendJson(res, 400, { error: 'missing_tenantId' }); return; }
    const balance = deps.balanceStore.getBalance(tenantId);
    if (balance === null) { sendJson(res, 200, { tenantId, balance: 0, exists: false }); return; }
    sendJson(res, 200, { tenantId, balance, exists: true });
    return;
  }
  if (req.method === 'POST' && (url === '/internal/balance/deduct' || url === '/internal/balance/deduct/')) {
    await handleBalanceDeduct(req, res, deps);
    return;
  }
  if (req.method === 'GET' && tryServeWebRoot(deps.webRoot, url, res)) {
    return;
  }
  sendJson(res, 404, { error: 'not_found' });
}

/** HTTP 探针 + 内部 onboarding / mock 支付（mock 支付受 `mock-pay-policy` 约束）。 */
export function createHttpServer(options?: HttpServerOptions): Server {
  const deps = resolveDeps(options);
  return createServer((req, res) => {
    void (async () => {
      try {
        await route(req, res, deps);
      } catch {
        sendJson(res, 500, { error: 'internal_error' });
      }
    })();
  });
}
