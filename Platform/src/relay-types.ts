/** 与 Relay `relay.json` 对齐的最小子集（Platform merge 用）。 */

export type RelayFeishuCreds = {
  appId: string;
  appSecret: string;
  sessionId: string;
};

export type RelayAgentDoc = {
  id: string;
  name: string;
  emoji: string;
  workspace: string;
  channel?: 'feishu' | 'telegram';
  feishu?: RelayFeishuCreds;
  feishuOpenId?: string;
  maxTurns?: number;
  allowedTools?: string[];
  runtime?: 'cc' | 'codex';
  botToken?: string;
  sessionId?: string;
  /** PT0-019: Linux user for per-tenant file isolation (e.g. "tenant-member-0001"). */
  linuxUser?: string;
};

export type RelayUserDoc = {
  userId: string;
  chatId?: string;
  feishuOpenId?: string;
  defaultAgent?: string;
  agents: RelayAgentDoc[];
};

export type RelayBindingAddressDoc = {
  channel: 'feishu' | 'telegram' | 'harness' | 'system';
  kind: string;
  value: string;
  botId?: string;
  appId?: string;
};

export type RelayBindingRecordDoc = {
  id: string;
  address: RelayBindingAddressDoc;
  principalId: string;
  tenantId: string;
  defaultAgentId: string;
  allowedAgentIds: string[];
  status: 'active' | 'pending' | 'revoked';
  createdAt: string;
  approvedAt?: string;
  approvedBy?: string;
};

export type RelayConfigDoc = {
  users: RelayUserDoc[];
  bindings?: RelayBindingRecordDoc[];
  [key: string]: unknown;
};
