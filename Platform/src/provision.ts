import { randomUUID } from 'node:crypto';
import { join } from 'node:path';
import { appendAuditLog } from './audit-log.js';
import {
  verifyFeishuAppCredentials,
  type FeishuVerifyResult,
} from './feishu-credentials.js';
import type { OrderStore } from './order-store.js';
import type { PhoneAllowlist } from './phone-allowlist.js';
import {
  approvePairing,
  reloadRelayAgents,
  type RelayApproveResult,
} from './relay-pairing-client.js';
import {
  backupRelayConfig,
  ensureTenantWorkspaceDir,
  mergeFeishuTenantIntoRelayJson,
} from './relay-merge.js';
import { runRelayReloadHook } from './relay-reload.js';
import { ensureTenantLinuxUser } from './tenant-linux-user.js';
import {
  deriveTenantNames,
  TenantIdAllocator,
} from './tenant-id-allocator.js';

/** v0.3.0 三步流 Step 2 输入。其余字段均由服务端按规则生成。 */
export interface FeishuStep2Input {
  orderId: string;
  feishuAppId: string;
  feishuAppSecret: string;
}

export type Step2Outcome =
  | {
      ok: true;
      already: boolean;
      tenantId: string;
      agentId: string;
      principalId: string;
      bindingId: string;
      agentName: string;
    }
  | {
      ok: false;
      error:
        | 'order_not_found'
        | 'not_paid'
        | 'not_allowlisted'
        | 'invalid_feishu_credentials'
        | 'feishu_unreachable'
        | 'feishu_unexpected_response'
        | 'relay_error';
      message?: string;
      feishuCode?: number;
      feishuMsg?: string;
    };

export interface Step2Deps {
  orderStore: OrderStore;
  allowlist: PhoneAllowlist;
  relayConfigPath: string;
  tenantsRoot: string;
  /** Allocator must outlive the request; usually one per process. */
  tenantIdAllocator: TenantIdAllocator;
  /** Mock seam for verifyFeishuAppCredentials tests. */
  verifyFeishuCredentials?: (input: { appId: string; appSecret: string }) => Promise<FeishuVerifyResult>;
  /** Optional Relay loopback URL; when set, hot-reload via HTTP after merge. */
  relayHttpUrl?: string;
  /** Override fetch for HTTP reload (tests). */
  fetchImpl?: typeof fetch;
  /** Tests can pin uuid/now. */
  uuid?: () => string;
  nowMs?: number;
  /** Optional backup before write (defaults off in HTTP). */
  withBackup?: boolean;
  auditEnv?: NodeJS.ProcessEnv;
  env?: NodeJS.ProcessEnv;
  /** When true, skip reload entirely (tests). */
  skipReload?: boolean;
}

/**
 * Step 2: validate Feishu credentials, allocate tenantId, write `relay.json`
 * with binding `status='pending'` and empty open_id, then ask Relay to spawn the
 * new feishu WS client. Step 3 (`/internal/pair`) finalises the open_id.
 */
export async function provisionFeishuStep2(deps: Step2Deps, input: FeishuStep2Input): Promise<Step2Outcome> {
  const env = deps.env ?? process.env;
  const auditEnv = deps.auditEnv ?? env;

  const order = deps.orderStore.get(input.orderId);
  if (!order) return { ok: false, error: 'order_not_found' };
  if (order.status !== 'paid') return { ok: false, error: 'not_paid' };
  if (!deps.allowlist.isAllowed(order.phone)) return { ok: false, error: 'not_allowlisted' };

  const verify = deps.verifyFeishuCredentials
    ? await deps.verifyFeishuCredentials({ appId: input.feishuAppId, appSecret: input.feishuAppSecret })
    : await verifyFeishuAppCredentials({ appId: input.feishuAppId, appSecret: input.feishuAppSecret });
  if (!verify.ok) {
    appendAuditLog(
      {
        ts: new Date(deps.nowMs ?? Date.now()).toISOString(),
        action: 'step2_feishu_verify_failed',
        orderId: input.orderId,
        detail: {
          error: verify.error,
          feishuCode: verify.error === 'invalid_credentials' ? verify.code : undefined,
          feishuMsg: verify.error === 'invalid_credentials' ? verify.msg : undefined,
        },
      },
      auditEnv,
    );
    if (verify.error === 'invalid_credentials') {
      return {
        ok: false,
        error: 'invalid_feishu_credentials',
        feishuCode: verify.code,
        feishuMsg: verify.msg,
      };
    }
    return {
      ok: false,
      error: verify.error,
      message: 'detail' in verify ? verify.detail : undefined,
    };
  }

  // If this order has already been provisioned to Step 2, short-circuit using the
  // tenant id we previously stored.
  if (deps.orderStore.isProvisioned(input.orderId) && order.tenantId) {
    const names = deriveTenantNames({ tenantId: order.tenantId, displayName: order.displayName });
    const workspaceAbs = join(deps.tenantsRoot, names.tenantId);
    // Still scaffold tenant CLAUDE.md on repeat visits (older provisions had empty dirs → inherited /opt/PULSE/CLAUDE.md).
    ensureTenantWorkspaceDir(workspaceAbs, {
      tenantId: names.tenantId,
      agentId: names.agentId,
      agentName: names.agentName,
    });
    ensureTenantLinuxUser({ tenantId: names.tenantId, workspace: workspaceAbs });
    return {
      ok: true,
      already: true,
      tenantId: names.tenantId,
      agentId: names.agentId,
      principalId: names.principalId,
      bindingId: names.bindingId,
      agentName: names.agentName,
    };
  }

  const tenantId = order.tenantId ?? deps.tenantIdAllocator.next();
  const names = deriveTenantNames({ tenantId, displayName: order.displayName });
  const workspaceAbs = join(deps.tenantsRoot, names.tenantId);
  ensureTenantWorkspaceDir(workspaceAbs, {
    tenantId: names.tenantId,
    agentId: names.agentId,
    agentName: names.agentName,
  });
  const linuxUserResult = ensureTenantLinuxUser({ tenantId: names.tenantId, workspace: workspaceAbs });

  const sessionId = (deps.uuid ?? randomUUID)();
  const nowMs = deps.nowMs ?? Date.now();
  const nowIso = new Date(nowMs).toISOString();

  try {
    if (deps.withBackup) backupRelayConfig(deps.relayConfigPath, nowMs);
    await mergeFeishuTenantIntoRelayJson(deps.relayConfigPath, {
      principalId: names.principalId,
      tenantId: names.tenantId,
      workspaceAbs,
      agentId: names.agentId,
      agentName: names.agentName,
      agentEmoji: names.emoji,
      feishuAppId: input.feishuAppId,
      feishuAppSecret: input.feishuAppSecret,
      feishuOpenId: '',
      sessionId,
      bindingId: names.bindingId,
      workspaceId: names.workspaceId,
      nowIso,
      bindingStatus: 'pending',
      linuxUser: linuxUserResult.linuxUser,
    });
  } catch (e) {
    const message = e instanceof Error ? e.message : String(e);
    console.error(JSON.stringify({ ts: new Date().toISOString(), level: 'error', msg: 'step2_relay_merge_failed', orderId: input.orderId, tenantId, error: message, stack: e instanceof Error ? e.stack : undefined }));
    appendAuditLog(
      { ts: nowIso, action: 'step2_relay_merge_failed', orderId: input.orderId, tenantId, detail: { message } },
      auditEnv,
    );
    return { ok: false, error: 'relay_error', message };
  }

  deps.orderStore.setTenantId(input.orderId, tenantId);
  deps.orderStore.markProvisioned(input.orderId);
  appendAuditLog({ ts: nowIso, action: 'step2_provisioned', orderId: input.orderId, tenantId }, auditEnv);

  if (!deps.skipReload) {
    if (deps.relayHttpUrl) {
      const r = await reloadRelayAgents({ baseUrl: deps.relayHttpUrl, fetchImpl: deps.fetchImpl });
      appendAuditLog(
        { ts: nowIso, action: r.ok ? 'step2_relay_http_reload_ok' : 'step2_relay_http_reload_failed', orderId: input.orderId, tenantId, detail: { ...r } },
        auditEnv,
      );
    } else {
      await runRelayReloadHook(env);
    }
  }

  return {
    ok: true,
    already: false,
    tenantId: names.tenantId,
    agentId: names.agentId,
    principalId: names.principalId,
    bindingId: names.bindingId,
    agentName: names.agentName,
  };
}

export interface Step3Input {
  orderId: string;
  /** 6-letter pairing code copied from the Feishu bot. */
  code: string;
}

export type Step3Outcome =
  | { ok: true; tenantId: string; agentId: string; principalId: string; openId: string }
  | { ok: false; error: 'order_not_found' | 'not_provisioned' | 'pair_relay_unreachable' | 'pair_code_unknown' | 'pair_agent_mismatch' | 'pair_config_missing_agent' | 'pair_unexpected'; detail?: string; expectedAgentId?: string };

export interface Step3Deps {
  orderStore: OrderStore;
  /** Required: Relay loopback URL (no fallback for pairing). */
  relayHttpUrl: string;
  fetchImpl?: typeof fetch;
  /** Optional override (tests). */
  approveFn?: (
    opts: { baseUrl: string; fetchImpl?: typeof fetch },
    body: { code: string; principalId: string; agentId: string },
  ) => Promise<RelayApproveResult>;
  auditEnv?: NodeJS.ProcessEnv;
  nowMs?: number;
}

/** Step 3: forward a pairing code to Relay (`/internal/pairing/approve`). */
export async function pairFeishuStep3(deps: Step3Deps, input: Step3Input): Promise<Step3Outcome> {
  const auditEnv = deps.auditEnv ?? process.env;
  const nowIso = new Date(deps.nowMs ?? Date.now()).toISOString();
  const order = deps.orderStore.get(input.orderId);
  if (!order) return { ok: false, error: 'order_not_found' };
  if (!order.tenantId || !deps.orderStore.isProvisioned(input.orderId)) return { ok: false, error: 'not_provisioned' };
  const names = deriveTenantNames({ tenantId: order.tenantId, displayName: order.displayName });

  const approve = deps.approveFn ?? approvePairing;
  const r = await approve(
    { baseUrl: deps.relayHttpUrl, fetchImpl: deps.fetchImpl },
    { code: input.code, principalId: names.principalId, agentId: names.agentId },
  );

  if (r.ok) {
    appendAuditLog(
      { ts: nowIso, action: 'step3_pair_ok', orderId: input.orderId, tenantId: order.tenantId, detail: { agentId: names.agentId } },
      auditEnv,
    );
    return { ok: true, tenantId: order.tenantId, agentId: names.agentId, principalId: names.principalId, openId: r.openId };
  }

  appendAuditLog(
    { ts: nowIso, action: 'step3_pair_failed', orderId: input.orderId, tenantId: order.tenantId, detail: { error: r.error, ...('detail' in r ? { detail: r.detail } : {}) } },
    auditEnv,
  );

  switch (r.error) {
    case 'code_unknown': return { ok: false, error: 'pair_code_unknown' };
    case 'agent_mismatch': return { ok: false, error: 'pair_agent_mismatch', expectedAgentId: r.expectedAgentId };
    case 'config_missing_agent': return { ok: false, error: 'pair_config_missing_agent' };
    case 'relay_unreachable': return { ok: false, error: 'pair_relay_unreachable', detail: r.detail };
    default: return { ok: false, error: 'pair_unexpected', detail: 'detail' in r ? r.detail : undefined };
  }
}

// --- Backward compat (legacy single-shot endpoint) ---
//
// Old callers (tests, ad-hoc scripts) used `provisionFeishuTenantAfterPaid` with
// 11 fields. We keep the export so they continue to work; new HTTP routes go
// through Step 2/3.

export type FeishuProvisionInput = {
  orderId: string;
  tenantId: string;
  principalId: string;
  agentId: string;
  bindingId: string;
  name: string;
  emoji: string;
  feishuAppId: string;
  feishuAppSecret: string;
  feishuOpenId: string;
  workspaceId: string;
};

export type ProvisionOutcome =
  | { ok: true; already: boolean }
  | { ok: false; error: 'order_not_found' | 'not_paid' | 'not_allowlisted' | 'relay_error'; message?: string };

export async function provisionFeishuTenantAfterPaid(opts: {
  orderStore: OrderStore;
  allowlist: PhoneAllowlist;
  relayConfigPath: string;
  tenantsRoot: string;
  input: FeishuProvisionInput;
  nowMs?: number;
  uuid?: () => string;
  withBackup?: boolean;
  auditEnv?: NodeJS.ProcessEnv;
  runReload?: boolean;
  env?: NodeJS.ProcessEnv;
}): Promise<ProvisionOutcome> {
  const env = opts.env ?? process.env;
  const auditEnv = opts.auditEnv ?? env;
  const input = opts.input;
  const order = opts.orderStore.get(input.orderId);
  if (!order) return { ok: false, error: 'order_not_found' };
  if (order.status !== 'paid') return { ok: false, error: 'not_paid' };
  if (!opts.allowlist.isAllowed(order.phone)) return { ok: false, error: 'not_allowlisted' };
  if (opts.orderStore.isProvisioned(input.orderId)) return { ok: true, already: true };

  const workspaceAbs = join(opts.tenantsRoot, input.tenantId);
  ensureTenantWorkspaceDir(workspaceAbs, {
    tenantId: input.tenantId,
    agentId: input.agentId,
    agentName: input.name,
  });
  const linuxUserResult = ensureTenantLinuxUser({ tenantId: input.tenantId, workspace: workspaceAbs });
  const sessionId = (opts.uuid ?? randomUUID)();
  const nowMs = opts.nowMs ?? Date.now();
  const nowIso = new Date(nowMs).toISOString();

  try {
    if (opts.withBackup) backupRelayConfig(opts.relayConfigPath, nowMs);
    await mergeFeishuTenantIntoRelayJson(opts.relayConfigPath, {
      principalId: input.principalId,
      tenantId: input.tenantId,
      workspaceAbs,
      agentId: input.agentId,
      agentName: input.name,
      agentEmoji: input.emoji,
      feishuAppId: input.feishuAppId,
      feishuAppSecret: input.feishuAppSecret,
      feishuOpenId: input.feishuOpenId,
      sessionId,
      bindingId: input.bindingId,
      workspaceId: input.workspaceId,
      nowIso,
      linuxUser: linuxUserResult.linuxUser,
    });
  } catch (e) {
    const message = e instanceof Error ? e.message : String(e);
    appendAuditLog(
      { ts: nowIso, action: 'provision_failed', orderId: input.orderId, tenantId: input.tenantId, detail: { message } },
      auditEnv,
    );
    return { ok: false, error: 'relay_error', message };
  }

  opts.orderStore.markProvisioned(input.orderId);
  opts.orderStore.setTenantId(input.orderId, input.tenantId);
  appendAuditLog({ ts: nowIso, action: 'provision_ok', orderId: input.orderId, tenantId: input.tenantId }, auditEnv);

  if (opts.runReload !== false) await runRelayReloadHook(env);

  return { ok: true, already: false };
}
