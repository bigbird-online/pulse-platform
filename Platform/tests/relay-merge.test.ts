import { describe, it, expect } from 'vitest';
import { writeFile, mkdtemp } from 'node:fs/promises';
import { join } from 'node:path';
import { tmpdir } from 'node:os';
import {
  applyFeishuTenantMerge,
  mergeFeishuTenantIntoRelayJson,
  readRelayConfigFile,
} from '../src/relay-merge.js';
import type { RelayConfigDoc } from '../src/relay-types.js';

const baseRelay = `{
  "_x": 1,
  "bindings": [],
  "users": []
}\n`;

describe('relay-merge [P0-004]', () => {
  const input = {
    principalId: 'member-042',
    tenantId: 'member-042',
    workspaceAbs: '/tmp/t/member-042',
    agentId: 'main-feishu',
    agentName: 'Bot',
    agentEmoji: '🔮',
    feishuAppId: 'cli_x',
    feishuAppSecret: 'sec',
    feishuOpenId: 'ou_1',
    sessionId: 'sess-uuid',
    bindingId: 'bind-42',
    workspaceId: 'member-042',
    nowIso: '2026-05-01T00:00:00.000Z',
  };

  it('applyFeishuTenantMerge appends user agent and binding', () => {
    const doc = JSON.parse(baseRelay) as RelayConfigDoc;
    applyFeishuTenantMerge(doc, input);
    expect(doc.users).toHaveLength(1);
    expect(doc.users[0]?.agents[0]?.feishu?.appId).toBe('cli_x');
    expect(doc.bindings?.some((b) => b.id === 'bind-42')).toBe(true);
  });

  it('applyFeishuTenantMerge replaces binding with same id', () => {
    const doc = JSON.parse(baseRelay) as RelayConfigDoc;
    applyFeishuTenantMerge(doc, input);
    applyFeishuTenantMerge(doc, {
      ...input,
      feishuOpenId: 'ou_2',
      sessionId: 'sess-2',
    });
    expect(doc.bindings?.filter((b) => b.id === 'bind-42')).toHaveLength(1);
    expect(doc.bindings?.[0]?.address.value).toBe('ou_2');
  });

  it('rejects reusing the same feishu appId across different tenants', () => {
    const doc = JSON.parse(baseRelay) as RelayConfigDoc;
    applyFeishuTenantMerge(doc, input);
    expect(() =>
      applyFeishuTenantMerge(doc, {
        ...input,
        principalId: 'member-043',
        tenantId: 'member-043',
        workspaceAbs: '/tmp/t/member-043',
        agentId: 'other-feishu',
        bindingId: 'bind-43',
      }),
    ).toThrow(/feishu_app_id_in_use:cli_x:member-042:main-feishu/);
  });

  it('mergeFeishuTenantIntoRelayJson writes file under lock', async () => {
    const dir = await mkdtemp(join(tmpdir(), 'rlm-'));
    const path = join(dir, 'relay.json');
    await writeFile(path, baseRelay, 'utf8');
    await mergeFeishuTenantIntoRelayJson(path, input);
    const { doc } = readRelayConfigFile(path);
    expect(doc.users[0]?.userId).toBe('member-042');
  });

  it('mergeFeishuTenantIntoRelayJson fails when file missing', async () => {
    const dir = await mkdtemp(join(tmpdir(), 'rlm-'));
    const path = join(dir, 'missing.json');
    await expect(mergeFeishuTenantIntoRelayJson(path, input)).rejects.toThrow(/relay_missing/);
  });

  // --- PT0-019: linuxUser in relay.json agent config ---

  it('applyFeishuTenantMerge writes linuxUser to agent doc when provided', () => {
    const doc = JSON.parse(baseRelay) as RelayConfigDoc;
    applyFeishuTenantMerge(doc, { ...input, linuxUser: 'tenant-member-042' });
    expect(doc.users[0]?.agents[0]?.linuxUser).toBe('tenant-member-042');
  });

  it('applyFeishuTenantMerge omits linuxUser from agent doc when not provided', () => {
    const doc = JSON.parse(baseRelay) as RelayConfigDoc;
    applyFeishuTenantMerge(doc, input);
    expect(doc.users[0]?.agents[0]?.linuxUser).toBeUndefined();
  });
});
