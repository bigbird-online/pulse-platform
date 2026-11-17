import { readFileSync, writeFileSync, renameSync, existsSync } from 'node:fs';
import { mkdirSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { withFileLock } from './file-lock.js';
import type { RelayAgentDoc, RelayBindingRecordDoc, RelayConfigDoc } from './relay-types.js';


function checkManagedRelayTarget(configPath: string): void {
  const mapFile = process.env['PULSE_RELAY_PATHS'];
  if (!mapFile) return;
  const map = JSON.parse(readFileSync(mapFile, 'utf8')) as { schema?: string; paths?: { relayConfig?: string } };
  if (map.schema !== 'pulse_relay_paths/v1' || map.paths?.relayConfig !== configPath) {
    throw new Error('Platform relay target differs from the managed Relay instance');
  }
}

function parseRelayDoc(raw: string): RelayConfigDoc {
  const doc = JSON.parse(raw) as RelayConfigDoc;
  if (!Array.isArray(doc.users)) {
    throw new Error('relay_invalid: users must be an array');
  }
  if (!doc.bindings) doc.bindings = [];
  return doc;
}

function atomicWriteJson(path: string, doc: RelayConfigDoc): void {
  const serialized = `${JSON.stringify(doc, null, 2)}\n`;
  const tmpPath = `${path}.platform-tmp`;
  writeFileSync(tmpPath, serialized, { encoding: 'utf8', mode: 0o600 });
  renameSync(tmpPath, path);
}

export type MergeFeishuTenantInput = {
  principalId: string;
  tenantId: string;
  workspaceAbs: string;
  agentId: string;
  agentName: string;
  agentEmoji: string;
  feishuAppId: string;
  feishuAppSecret: string;
  /**
   * 用户的飞书 open_id；v0.3.0 三步流的 Step 2 写盘时此处传 `''`，binding `status` 设为 `'pending'`，
   * Step 3（HTTP `pairing/approve`）落实 openId 后改为 `'active'`。
   */
  feishuOpenId: string;
  sessionId: string;
  bindingId: string;
  workspaceId: string;
  nowIso: string;
  /** v0.3.0：默认 `'active'`；三步流 Step 2 传 `'pending'` 占位。 */
  bindingStatus?: 'active' | 'pending';
  /** PT0-019: Linux user for per-tenant file isolation (e.g. "tenant-member-0001"). */
  linuxUser?: string;
};

/**
 * 在文件锁保护下读改写 relay.json：幂等 upsert user / feishu agent / binding。
 */
export async function mergeFeishuTenantIntoRelayJson(
  configPath: string,
  input: MergeFeishuTenantInput,
): Promise<void> {
  checkManagedRelayTarget(configPath);
  const lockPath = `${configPath}.platform-lock`;
  await withFileLock(lockPath, async () => {
    if (!existsSync(configPath)) {
      throw new Error(`relay_missing:${configPath}`);
    }
    const text = readFileSync(configPath, 'utf8');
    const doc = parseRelayDoc(text);
    applyFeishuTenantMerge(doc, input);
    atomicWriteJson(configPath, doc);
  });
}

export function applyFeishuTenantMerge(doc: RelayConfigDoc, input: MergeFeishuTenantInput): void {
  const bindings = doc.bindings ?? [];
  doc.bindings = bindings;

  // One-tenant-one-Feishu-app invariant: a Feishu `appId` must not be reused
  // across different tenants/agents in the same relay.json. If reused, inbound
  // messages become ambiguous and pairing codes can be issued by the wrong bot.
  for (const u of doc.users) {
    const agents = u.agents ?? [];
    for (const a of agents) {
      if (a.channel !== 'feishu') continue;
      const existingAppId = a.feishu?.appId;
      if (!existingAppId) continue;
      if (existingAppId !== input.feishuAppId) continue;
      if (a.id === input.agentId) continue; // same agent, allowed (update)
      const existingTenantId = (u.userId ?? '').trim();
      if (existingTenantId && existingTenantId !== input.tenantId) {
        throw new Error(`feishu_app_id_in_use:${input.feishuAppId}:${existingTenantId}:${a.id}`);
      }
    }
  }

  const agent: RelayAgentDoc = {
    id: input.agentId,
    name: input.agentName,
    emoji: input.agentEmoji,
    channel: 'feishu',
    workspace: input.workspaceAbs,
    feishu: {
      appId: input.feishuAppId,
      appSecret: input.feishuAppSecret,
      sessionId: input.sessionId,
    },
    feishuOpenId: input.feishuOpenId,
    maxTurns: 1000,
    allowedTools: ['Read', 'Write', 'Edit', 'Glob', 'Grep', 'Bash', 'Agent', 'Task'],
    ...(input.linuxUser ? { linuxUser: input.linuxUser } : {}),
  };

  let user = doc.users.find((u) => u.userId === input.principalId);
  if (!user) {
    user = {
      userId: input.principalId,
      feishuOpenId: input.feishuOpenId,
      defaultAgent: input.agentId,
      agents: [agent],
    };
    doc.users.push(user);
  } else {
    if (!user.feishuOpenId) user.feishuOpenId = input.feishuOpenId;
    const idx = user.agents.findIndex((a) => a.id === input.agentId);
    if (idx >= 0) user.agents[idx] = agent;
    else user.agents.push(agent);
    if (!user.defaultAgent) user.defaultAgent = input.agentId;
  }

  const binding: RelayBindingRecordDoc = {
    id: input.bindingId,
    address: {
      channel: 'feishu',
      kind: 'feishu_open_id',
      value: input.feishuOpenId,
      appId: input.feishuAppId,
    },
    principalId: input.principalId,
    tenantId: input.tenantId,
    defaultAgentId: input.agentId,
    allowedAgentIds: [input.agentId],
    status: input.bindingStatus ?? 'active',
    createdAt: input.nowIso,
  };

  const bi = bindings.findIndex((b) => b.id === input.bindingId);
  if (bi >= 0) bindings[bi] = binding;
  else bindings.push(binding);
}

/** 确保租户工作区目录存在（幂等）。 */
export function ensureTenantWorkspaceDir(workspaceAbs: string, opts?: {
  tenantId?: string;
  agentId?: string;
  agentName?: string;
}): void {
  mkdirSync(workspaceAbs, { recursive: true });

  // Tenant isolation relies on the workspace prompt. If the directory is empty,
  // agent runtimes may inherit `/opt/PULSE/CLAUDE.md` (platform admin identity).
  // Create a tenant-local `CLAUDE.md` as the nearest SSOT for identity/boundaries.
  const claudePath = join(workspaceAbs, 'CLAUDE.md');
  if (existsSync(claudePath)) return;

  const tenantId = opts?.tenantId?.trim() || 'member-????';
  const agentId = opts?.agentId?.trim() || `${tenantId}-pulse`;
  const agentName = opts?.agentName?.trim() || '租户的pulse管理员';

  const doc = `---
name: "Tenant|CLAUDE.md"
version: 1.0.0
description: "租户工作区身份卡（${tenantId}）"
---

# ${agentName}（${agentId}）

你是 **租户 ${tenantId} 的 pulse 管理员**，只负责该租户的日常使用支持与租户级事务处理。

## 行为边界（必须遵守）

- 你 **不是** PULSE 平台管家（平台管理员 Agent）。
- 你 **不能** 做平台级运维操作：例如修改/重启平台服务、编辑其它租户配置、调整全局路由与角色定义。
- 你只应在本租户工作区内工作：\`${tenantId}\`。

## 当用户提到平台问题

请明确告知：这属于平台级问题，应联系 **PULSE 平台管家 Agent（Pulse）** 或平台运维人员处理。
`;

  writeFileSync(claudePath, `${doc.trim()}\n`, 'utf8');
}

export function backupRelayConfig(configPath: string, nowMs: number): string {
  const ts = new Date(nowMs).toISOString().replace(/[:.]/g, '-');
  const backupDir = join(dirname(configPath), 'backups');
  mkdirSync(backupDir, { recursive: true });
  const backupPath = join(backupDir, `relay-${ts}.json`);
  const raw = readFileSync(configPath, 'utf8');
  writeFileSync(backupPath, raw, 'utf8');
  return backupPath;
}

export type ReadRelayConfigResult = { doc: RelayConfigDoc; raw: string };

export function readRelayConfigFile(configPath: string): ReadRelayConfigResult {
  const raw = readFileSync(configPath, 'utf8');
  return { doc: parseRelayDoc(raw), raw };
}
