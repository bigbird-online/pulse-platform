import { managedRelayPath } from './managed-paths.js';
// Provision a new Agent into a Relay config (~/.relay/relay.json by default).
//
// Pure logic split from CLI (provision-agent.ts) for testability. Does not
// touch process.env directly.

import { copyFileSync, existsSync, mkdirSync, readFileSync, renameSync, writeFileSync } from 'node:fs';
import { dirname, join, resolve } from 'node:path';
import { randomUUID } from 'node:crypto';
import type { AgentConfig, ChannelType } from './types.js';

export interface ProvisionRequest {
  configPath: string;
  userId: string;
  agentId: string;
  name: string;
  emoji: string;
  workspace: string;
  channel?: ChannelType;
  botToken?: string;
  feishuAppId?: string;
  feishuAppSecret?: string;
  feishuOpenId?: string;
  monthlyBudgetUsd?: number;
  maxTurns?: number;
  allowedTools?: string[];
  runtime?: 'cc' | 'codex';
  /** HOME dir for non-linuxUser agents (cross-channel session access). */
  userHome?: string;
  dryRun?: boolean;
  /** Override now() for tests; default Date.now(). */
  nowMs?: number;
  /** Override session UUID generator for tests; default randomUUID(). */
  uuid?: () => string;
}

export interface ProvisionOutcome {
  configPath: string;
  user: string;
  agent: AgentConfig;
  backupPath?: string;
  dryRun: boolean;
}

interface RelayConfigFile {
  users: Array<{
    userId: string;
    chatId?: string;
    feishuOpenId?: string;
    defaultAgent?: string;
    agents: AgentConfig[];
  }>;
  [key: string]: unknown;
}

export class ProvisionError extends Error {
  constructor(public readonly code: string, message: string) {
    super(message);
    this.name = 'ProvisionError';
  }
}

const ID_PATTERN = /^[a-z0-9]([a-z0-9-]*[a-z0-9])?$/;

export function buildAgentConfig(req: ProvisionRequest, sessionId: string): AgentConfig {
  const channel: ChannelType | undefined = req.channel;
  const agent: AgentConfig = {
    id: req.agentId,
    name: req.name,
    emoji: req.emoji,
    workspace: resolve(req.workspace),
  };
  if (channel) agent.channel = channel;
  if (req.maxTurns !== undefined) agent.maxTurns = req.maxTurns;
  if (req.allowedTools && req.allowedTools.length > 0) agent.allowedTools = req.allowedTools;
  if (req.runtime) agent.runtime = req.runtime;
  if (req.userHome) agent.userHome = req.userHome;
  if (req.monthlyBudgetUsd !== undefined && req.monthlyBudgetUsd > 0) {
    agent.monthlyBudgetUsd = req.monthlyBudgetUsd;
  }

  if (channel === 'feishu') {
    if (!req.feishuAppId || !req.feishuAppSecret || !req.feishuOpenId) {
      throw new ProvisionError('FEISHU_INCOMPLETE', 'feishu channel requires feishuAppId, feishuAppSecret, feishuOpenId');
    }
    agent.feishu = { appId: req.feishuAppId, appSecret: req.feishuAppSecret, sessionId };
    agent.feishuOpenId = req.feishuOpenId;
  } else {
    if (!req.botToken) {
      throw new ProvisionError('TG_TOKEN_MISSING', 'telegram channel requires botToken');
    }
    agent.botToken = req.botToken;
    agent.sessionId = sessionId;
  }

  return agent;
}

export function validateRequest(req: ProvisionRequest): void {
  if (!ID_PATTERN.test(req.agentId)) {
    throw new ProvisionError('ID_INVALID', `agentId "${req.agentId}" must be lowercase alphanumeric with hyphens (a-z, 0-9, -)`);
  }
  if (!ID_PATTERN.test(req.userId)) {
    throw new ProvisionError('USER_INVALID', `userId "${req.userId}" must be lowercase alphanumeric with hyphens`);
  }
  if (!req.name || !req.emoji || !req.workspace) {
    throw new ProvisionError('FIELD_MISSING', 'name, emoji, workspace are required');
  }
  if (req.channel && req.channel !== 'telegram' && req.channel !== 'feishu') {
    throw new ProvisionError('CHANNEL_INVALID', `channel "${req.channel}" must be 'telegram' or 'feishu'`);
  }
  if (req.monthlyBudgetUsd !== undefined && req.monthlyBudgetUsd < 0) {
    throw new ProvisionError('BUDGET_INVALID', 'monthlyBudgetUsd must be >= 0');
  }
}

function loadConfig(configPath: string): RelayConfigFile {
  managedRelayPath('relayConfig', configPath);
  if (!existsSync(configPath)) {
    throw new ProvisionError('CONFIG_MISSING', `Relay config not found at ${configPath}`);
  }
  const raw = readFileSync(configPath, 'utf-8');
  try {
    return JSON.parse(raw) as RelayConfigFile;
  } catch (err) {
    throw new ProvisionError('CONFIG_PARSE_FAIL', `Failed to parse ${configPath}: ${String(err)}`);
  }
}

function backupConfig(configPath: string, nowMs: number): string {
  const ts = new Date(nowMs).toISOString().replace(/[:.]/g, '-');
  const backupDir = join(dirname(configPath), 'backups');
  mkdirSync(backupDir, { recursive: true });
  const backupPath = join(backupDir, `relay-${ts}.json`);
  copyFileSync(configPath, backupPath);
  return backupPath;
}

function atomicWrite(configPath: string, data: RelayConfigFile): void {
  const serialized = JSON.stringify(data, null, 2) + '\n';
  const tmpPath = `${configPath}.provision.tmp`;
  writeFileSync(tmpPath, serialized, { mode: 0o600 });
  renameSync(tmpPath, configPath);
}

export function provisionAgent(req: ProvisionRequest): ProvisionOutcome {
  validateRequest(req);

  const config = loadConfig(req.configPath);
  const user = config.users.find((u) => u.userId === req.userId);
  if (!user) {
    throw new ProvisionError('USER_NOT_FOUND', `userId "${req.userId}" not present in ${req.configPath}; create it first or pick an existing user`);
  }
  if (user.agents.some((a) => a.id === req.agentId)) {
    throw new ProvisionError('AGENT_DUPLICATE', `agentId "${req.agentId}" already exists under user "${req.userId}"`);
  }

  const sessionId = (req.uuid ?? randomUUID)();
  const agent = buildAgentConfig(req, sessionId);

  if (!req.dryRun) {
    const workspaceAbs = resolve(req.workspace);
    if (!existsSync(workspaceAbs)) {
      mkdirSync(workspaceAbs, { recursive: true });
    }
  }

  user.agents.push(agent);
  if (!user.defaultAgent) user.defaultAgent = agent.id;

  let backupPath: string | undefined;
  if (!req.dryRun) {
    backupPath = backupConfig(req.configPath, req.nowMs ?? Date.now());
    atomicWrite(req.configPath, config);
  }

  return {
    configPath: req.configPath,
    user: req.userId,
    agent,
    backupPath,
    dryRun: Boolean(req.dryRun),
  };
}
