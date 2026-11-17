import { describe, it, expect, beforeEach, afterEach } from 'vitest';
import { mkdirSync, mkdtempSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import {
  TenantIdAllocator,
  scanMaxMemberSeqFromTenantsDir,
  scanMaxMemberSeqFromRelayConfig,
  deriveTenantNames,
} from '../src/tenant-id-allocator.js';

let dir: string;
let tenantsRoot: string;
let relayPath: string;

beforeEach(() => {
  dir = mkdtempSync(join(tmpdir(), 'tenant-alloc-'));
  tenantsRoot = join(dir, 'Tenants');
  relayPath = join(dir, 'relay.json');
  mkdirSync(tenantsRoot, { recursive: true });
});

afterEach(() => {
  rmSync(dir, { recursive: true, force: true });
});

describe('scanMaxMemberSeqFromTenantsDir', () => {
  it('returns 0 for empty / missing root', () => {
    expect(scanMaxMemberSeqFromTenantsDir(tenantsRoot)).toBe(0);
    expect(scanMaxMemberSeqFromTenantsDir(join(dir, 'absent'))).toBe(0);
  });

  it('finds the largest member-NNNN directory', () => {
    mkdirSync(join(tenantsRoot, 'member-0001'));
    mkdirSync(join(tenantsRoot, 'member-0042'));
    mkdirSync(join(tenantsRoot, 'member-0007'));
    mkdirSync(join(tenantsRoot, 'admin'));
    expect(scanMaxMemberSeqFromTenantsDir(tenantsRoot)).toBe(42);
  });

  it('ignores files (only directories count)', () => {
    writeFileSync(join(tenantsRoot, 'member-0099'), 'not-a-dir');
    mkdirSync(join(tenantsRoot, 'member-0001'));
    expect(scanMaxMemberSeqFromTenantsDir(tenantsRoot)).toBe(1);
  });
});

describe('scanMaxMemberSeqFromRelayConfig', () => {
  it('returns 0 for missing or unparsable file', () => {
    expect(scanMaxMemberSeqFromRelayConfig(relayPath)).toBe(0);
    writeFileSync(relayPath, 'not json');
    expect(scanMaxMemberSeqFromRelayConfig(relayPath)).toBe(0);
  });

  it('finds largest tenantId in bindings[]', () => {
    writeFileSync(relayPath, JSON.stringify({
      users: [],
      bindings: [
        { tenantId: 'member-0010' },
        { tenantId: 'member-0099' },
        { tenantId: 'something-else' },
      ],
    }));
    expect(scanMaxMemberSeqFromRelayConfig(relayPath)).toBe(99);
  });
});

describe('TenantIdAllocator', () => {
  it('takes the union of Tenants/ and relay.json bindings', async () => {
    mkdirSync(join(tenantsRoot, 'member-0005'));
    writeFileSync(relayPath, JSON.stringify({
      users: [],
      bindings: [{ tenantId: 'member-0042' }],
    }));
    const a = await TenantIdAllocator.fromEnvironment({ tenantsRoot, relayConfigPath: relayPath });
    expect(a.peek()).toBe(42);
    expect(a.next()).toBe('member-0043');
    expect(a.next()).toBe('member-0044');
  });

  it('starts from 0 in clean environment', async () => {
    const a = await TenantIdAllocator.fromEnvironment({ tenantsRoot, relayConfigPath: relayPath });
    expect(a.next()).toBe('member-0001');
  });

  it('zero-pads to at least 4 digits', async () => {
    const a = new TenantIdAllocator(9);
    expect(a.next()).toBe('member-0010');
  });
});

describe('deriveTenantNames (FR-P0-010)', () => {
  it('builds the canonical naming bundle', () => {
    const names = deriveTenantNames({ tenantId: 'member-0042', displayName: '聪聪' });
    expect(names).toEqual({
      tenantId: 'member-0042',
      principalId: 'member-0042',
      workspaceId: 'member-0042',
      agentId: 'member-0042-pulse',
      bindingId: 'bind-member-0042',
      agentName: '聪聪的pulse管理员',
      emoji: '🐦',
    });
  });

  it('falls back to "我" when displayName missing', () => {
    const names = deriveTenantNames({ tenantId: 'member-0001', displayName: undefined });
    expect(names.agentName).toBe('我的pulse管理员');
  });
});
