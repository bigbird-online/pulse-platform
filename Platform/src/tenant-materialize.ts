// Stage A (FR-P0-014) — orchestrator that materialises a tenant workspace
// from the `tenant-pulse-admin` template after an order transitions to
// `paid`.
//
// Idempotent:
//   - if the order has no `tenantId`, allocate one and persist via
//     `OrderStore.setTenantId`;
//   - if the workspace already contains files, never overwrite them;
//   - safe to call again on webhook replays / `markPaid({already:true})`.

import { join } from 'node:path';
import type { OrderStore } from './order-store.js';
import { TenantIdAllocator, deriveTenantNames } from './tenant-id-allocator.js';
import { ensureTenantLinuxUser } from './tenant-linux-user.js';
import {
  defaultTenantPulseAdminTemplateRoot,
  materializeTenantWorkspaceFromTemplate,
  type MaterializeResult,
} from './tenant-template.js';

export interface MaterializeForOrderDeps {
  orderStore: OrderStore;
  tenantIdAllocator: TenantIdAllocator;
  tenantsRoot: string;
  /** Override for tests; defaults to bundled `tenant-pulse-admin/`. */
  templateRoot?: string;
  /** ISO timestamp source; defaults to now (used for `{{provisionedAtIso}}`). */
  nowIso?: string;
}

export type MaterializeForOrderOutcome =
  | {
      ok: true;
      tenantId: string;
      workspaceAbs: string;
      copied: string[];
      skipped: string[];
      allocated: boolean;
    }
  | {
      ok: false;
      error:
        | 'order_not_found'
        | 'not_paid'
        | 'tenants_root_unset'
        | 'template_missing'
        | 'io_error';
      message?: string;
    };

export function materializeTenantSpaceForOrder(
  deps: MaterializeForOrderDeps,
  orderId: string,
): MaterializeForOrderOutcome {
  if (!deps.tenantsRoot) return { ok: false, error: 'tenants_root_unset' };

  const order = deps.orderStore.get(orderId);
  if (!order) return { ok: false, error: 'order_not_found' };
  if (order.status !== 'paid') return { ok: false, error: 'not_paid' };

  let allocated = false;
  let tenantId = order.tenantId;
  if (!tenantId) {
    tenantId = deps.tenantIdAllocator.next();
    deps.orderStore.setTenantId(orderId, tenantId);
    allocated = true;
  }

  const names = deriveTenantNames({ tenantId, displayName: order.displayName });
  const workspaceAbs = join(deps.tenantsRoot, names.tenantId);
  const provisionedAtIso = deps.nowIso ?? new Date().toISOString();
  const templateRoot = deps.templateRoot ?? defaultTenantPulseAdminTemplateRoot();

  let result: MaterializeResult;
  ensureTenantLinuxUser({ tenantId: names.tenantId, workspace: workspaceAbs });
  try {
    result = materializeTenantWorkspaceFromTemplate({
      templateRoot,
      workspaceAbs,
      vars: {
        tenantId: names.tenantId,
        agentId: names.agentId,
        agentName: names.agentName,
        displayName: order.displayName ?? '我',
        provisionedAtIso,
      },
    });
  } catch (e) {
    const msg = e instanceof Error ? e.message : String(e);
    if (msg.startsWith('tenant_template_missing:')) {
      return { ok: false, error: 'template_missing', message: msg };
    }
    return { ok: false, error: 'io_error', message: msg };
  }

  return {
    ok: true,
    tenantId: names.tenantId,
    workspaceAbs,
    copied: result.copied,
    skipped: result.skipped,
    allocated,
  };
}
