// Sequential `member-NNNN` allocator (v0.3.0).
//
// Cold-start scans both the Tenants/ root *and* relay.json.bindings[].tenantId so
// we never collide when an op manually deleted a Tenants directory.
//
// In-memory; backed by the union of those two scans + an in-process counter
// for any IDs allocated this run. Persistence is the file system itself
// (Tenants/<id>/ directories).

import { existsSync, readdirSync, readFileSync, statSync } from 'node:fs';
import { join } from 'node:path';

const MEMBER_RE = /^member-(\d{4,})$/;

function parseMemberSeq(name: string): number | undefined {
  const m = MEMBER_RE.exec(name);
  if (!m) return undefined;
  const n = Number.parseInt(m[1]!, 10);
  if (!Number.isInteger(n) || n < 0) return undefined;
  return n;
}

export function scanMaxMemberSeqFromTenantsDir(tenantsRoot: string): number {
  if (!tenantsRoot || !existsSync(tenantsRoot)) return 0;
  let max = 0;
  for (const name of readdirSync(tenantsRoot)) {
    const seq = parseMemberSeq(name);
    if (seq === undefined) continue;
    let isDir = false;
    try {
      isDir = statSync(join(tenantsRoot, name)).isDirectory();
    } catch {
      isDir = false;
    }
    if (isDir && seq > max) max = seq;
  }
  return max;
}

export function scanMaxMemberSeqFromRelayConfig(relayConfigPath: string): number {
  if (!relayConfigPath || !existsSync(relayConfigPath)) return 0;
  let max = 0;
  try {
    const raw = readFileSync(relayConfigPath, 'utf8');
    const doc = JSON.parse(raw) as { bindings?: { tenantId?: string }[] };
    for (const b of doc.bindings ?? []) {
      const seq = b.tenantId ? parseMemberSeq(b.tenantId) : undefined;
      if (seq !== undefined && seq > max) max = seq;
    }
  } catch {
    return max;
  }
  return max;
}

export class TenantIdAllocator {
  private cursor: number;

  constructor(initial: number) {
    this.cursor = initial;
  }

  static async fromEnvironment(opts: {
    tenantsRoot: string;
    relayConfigPath: string;
  }): Promise<TenantIdAllocator> {
    const a = scanMaxMemberSeqFromTenantsDir(opts.tenantsRoot);
    const b = scanMaxMemberSeqFromRelayConfig(opts.relayConfigPath);
    return new TenantIdAllocator(Math.max(a, b));
  }

  /** Allocate next id, e.g. `member-0042`. */
  next(): string {
    this.cursor += 1;
    return `member-${String(this.cursor).padStart(4, '0')}`;
  }

  /** For inspection / tests. */
  peek(): number {
    return this.cursor;
  }
}

/** Fixed naming rules (FR-P0-010). */
export interface DerivedTenantNames {
  tenantId: string;
  principalId: string;
  workspaceId: string;
  agentId: string;
  bindingId: string;
  agentName: string;
  emoji: string;
}

export function deriveTenantNames(opts: {
  tenantId: string;
  displayName: string | undefined;
}): DerivedTenantNames {
  const display = (opts.displayName ?? '').trim() || '我';
  return {
    tenantId: opts.tenantId,
    principalId: opts.tenantId,
    workspaceId: opts.tenantId,
    agentId: `${opts.tenantId}-pulse`,
    bindingId: `bind-${opts.tenantId}`,
    agentName: `${display}的pulse管理员`,
    emoji: '🐦',
  };
}
