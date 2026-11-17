import { createHash } from 'node:crypto';
import { readFileSync } from 'node:fs';
import { harnessPrincipalForAgent, type RelayConfig, type ScheduledJob } from './types.js';

export const NODE_PLACEMENT_PATH_ENV = 'RELAY_NODE_PLACEMENT_PATH';
export const CURRENT_NODE_ENV = 'RELAY_CURRENT_NODE';
export const NODE_PLACEMENT_RELAY_SOURCE_ENV = 'RELAY_NODE_PLACEMENT_RELAY_SOURCE';
export const NODE_PLACEMENT_CRON_SOURCE_ENV = 'RELAY_NODE_PLACEMENT_CRON_SOURCE';

export type PlacementStatus = 'candidate' | 'active';

export interface PlacementNode {
  id: string;
  description?: string;
}

export interface LogicalAgentPlacement {
  id: string;
  placements: string[];
  /** Scheduler owner IDs that resolve to this logical agent. Always explicit. */
  ownerIds: string[];
  /** Explicit logical owner -> delivery bot mapping. Never inferred from equal IDs. */
  deliveryBotId?: string;
}

export interface BotPlacement {
  id: string;
  agentId: string;
  activeNode: string;
  /** References only. Runtime credential values remain in the secured Relay source. */
  credentialRefs: Record<string, string>;
}

export interface JobPlacement {
  id: string;
  ownerId: string;
  runNode: string;
}

export interface BindingPlacement {
  id: string;
  activeNode: string;
}

export interface UserDefaultPlacement {
  userId: string;
  nodeId: string;
  botId: string;
}

export interface NodePlacementDocument {
  schemaVersion: 1;
  deploymentId: string;
  status: PlacementStatus;
  nodes: PlacementNode[];
  agents: LogicalAgentPlacement[];
  bots: BotPlacement[];
  bindings: BindingPlacement[];
  jobs: JobPlacement[];
  userDefaults?: UserDefaultPlacement[];
}

export interface CronJobsDocument {
  version: number;
  jobs: ScheduledJob[];
  [key: string]: unknown;
}

export class NodePlacementError extends Error {
  constructor(message: string) {
    super(message);
    this.name = 'NodePlacementError';
  }
}

function object(value: unknown, label: string): Record<string, unknown> {
  if (!value || typeof value !== 'object' || Array.isArray(value)) {
    throw new NodePlacementError(`${label} must be an object`);
  }
  return value as Record<string, unknown>;
}

function allowedKeys(value: Record<string, unknown>, allowed: string[], label: string): void {
  const extra = Object.keys(value).filter(key => !allowed.includes(key));
  if (extra.length) throw new NodePlacementError(`${label} contains unknown field(s): ${extra.sort().join(',')}`);
}

function string(value: unknown, label: string): string {
  if (typeof value !== 'string' || !value.trim()) {
    throw new NodePlacementError(`${label} must be a non-empty string`);
  }
  return value.trim();
}

function idString(value: unknown, label: string): string {
  if (typeof value !== 'string' || !/^[A-Za-z0-9][A-Za-z0-9._:-]*$/.test(value)) {
    throw new NodePlacementError(`${label} must match the placement id contract`);
  }
  return value;
}

function idArray(value: unknown, label: string): string[] {
  if (!Array.isArray(value) || value.length === 0) {
    throw new NodePlacementError(`${label} must be a non-empty id array`);
  }
  return value.map((entry, index) => idString(entry, `${label}[${index}]`));
}

function uniqueById<T extends { id: string }>(items: T[], label: string): Map<string, T> {
  const map = new Map<string, T>();
  for (const item of items) {
    if (map.has(item.id)) throw new NodePlacementError(`duplicate ${label} id: ${item.id}`);
    map.set(item.id, item);
  }
  return map;
}

function assertUniqueStrings(values: string[], label: string): void {
  if (new Set(values).size !== values.length) {
    throw new NodePlacementError(`${label} contains duplicate values`);
  }
}

/** Parse + semantically validate schema v1. This is the runtime validator; the
 * checked-in JSON Schema is the editor/CI-facing representation of the same contract. */
export function parseNodePlacement(value: unknown): NodePlacementDocument {
  const root = object(value, 'node placement');
  allowedKeys(root, ['schemaVersion', 'deploymentId', 'status', 'nodes', 'agents', 'bots', 'bindings', 'jobs', 'userDefaults'], 'node placement');
  if (root.schemaVersion !== 1) {
    throw new NodePlacementError(`unsupported node placement schemaVersion: ${String(root.schemaVersion)}`);
  }
  const status = root.status;
  if (status !== 'candidate' && status !== 'active') {
    throw new NodePlacementError(`unsupported placement status: ${status}`);
  }

  if (!Array.isArray(root.nodes) || !Array.isArray(root.agents) || !Array.isArray(root.bots) || !Array.isArray(root.bindings) || !Array.isArray(root.jobs)) {
    throw new NodePlacementError('nodes, agents, bots, bindings and jobs must be arrays');
  }

  const nodes: PlacementNode[] = root.nodes.map((raw, index) => {
    const entry = object(raw, `nodes[${index}]`);
    allowedKeys(entry, ['id', 'description'], `nodes[${index}]`);
    const node: PlacementNode = { id: idString(entry.id, `nodes[${index}].id`) };
    if (entry.description !== undefined) node.description = string(entry.description, `nodes[${index}].description`);
    return node;
  });
  const nodeMap = uniqueById(nodes, 'node');
  if (nodeMap.size === 0) throw new NodePlacementError('at least one node is required');

  const agents: LogicalAgentPlacement[] = root.agents.map((raw, index) => {
    const entry = object(raw, `agents[${index}]`);
    allowedKeys(entry, ['id', 'placements', 'ownerIds', 'deliveryBotId'], `agents[${index}]`);
    const placements = idArray(entry.placements, `agents[${index}].placements`);
    assertUniqueStrings(placements, `agent ${String(entry.id)} placements`);
    const agent: LogicalAgentPlacement = {
      id: idString(entry.id, `agents[${index}].id`),
      placements,
      ownerIds: idArray(entry.ownerIds, `agents[${index}].ownerIds`),
    };
    assertUniqueStrings(agent.ownerIds, `agent ${agent.id} ownerIds`);
    if (entry.deliveryBotId !== undefined) agent.deliveryBotId = idString(entry.deliveryBotId, `agents[${index}].deliveryBotId`);
    return agent;
  });
  const agentMap = uniqueById(agents, 'agent');
  const ownerMap = new Map<string, LogicalAgentPlacement>();
  for (const agent of agents) {
    for (const nodeId of agent.placements) {
      if (!nodeMap.has(nodeId)) throw new NodePlacementError(`agent ${agent.id} references unknown node ${nodeId}`);
    }
    for (const ownerId of agent.ownerIds) {
      const existing = ownerMap.get(ownerId);
      if (existing) throw new NodePlacementError(`scheduler owner ${ownerId} maps to both ${existing.id} and ${agent.id}`);
      ownerMap.set(ownerId, agent);
    }
  }

  const bindings: BindingPlacement[] = root.bindings.map((raw, index) => {
    const entry = object(raw, `bindings[${index}]`);
    allowedKeys(entry, ['id', 'activeNode'], `bindings[${index}]`);
    return {
      id: idString(entry.id, `bindings[${index}].id`),
      activeNode: idString(entry.activeNode, `bindings[${index}].activeNode`),
    };
  });
  uniqueById(bindings, 'binding');
  for (const binding of bindings) {
    if (!nodeMap.has(binding.activeNode)) throw new NodePlacementError(`binding ${binding.id} references unknown active node ${binding.activeNode}`);
  }

  const bots: BotPlacement[] = root.bots.map((raw, index) => {
    const entry = object(raw, `bots[${index}]`);
    allowedKeys(entry, ['id', 'agentId', 'activeNode', 'credentialRefs'], `bots[${index}]`);
    const refsRaw = object(entry.credentialRefs, `bots[${index}].credentialRefs`);
    const credentialRefs: Record<string, string> = {};
    for (const [kind, rawRef] of Object.entries(refsRaw)) {
      const ref = typeof rawRef === 'string' ? rawRef : '';
      if (!/^env:[A-Z][A-Z0-9_]*$/.test(ref)) {
        throw new NodePlacementError(`bot ${String(entry.id)} credential ${kind} must be an env: reference`);
      }
      credentialRefs[kind] = ref;
    }
    if (Object.keys(credentialRefs).length === 0) {
      throw new NodePlacementError(`bot ${String(entry.id)} must declare at least one credential reference`);
    }
    if (status === 'active' && Object.values(credentialRefs).some(ref => /^env:(?:TODO|EXAMPLE)_/.test(ref))) {
      throw new NodePlacementError(`active placement bot ${String(entry.id)} contains placeholder credential references`);
    }
    return {
      id: idString(entry.id, `bots[${index}].id`),
      agentId: idString(entry.agentId, `bots[${index}].agentId`),
      activeNode: idString(entry.activeNode, `bots[${index}].activeNode`),
      credentialRefs,
    };
  });
  const botMap = uniqueById(bots, 'bot');
  for (const bot of bots) {
    const agent = agentMap.get(bot.agentId);
    if (!agent) throw new NodePlacementError(`bot ${bot.id} references unknown agent ${bot.agentId}`);
    if (!nodeMap.has(bot.activeNode)) throw new NodePlacementError(`bot ${bot.id} references unknown active node ${bot.activeNode}`);
    if (!agent.placements.includes(bot.activeNode)) {
      throw new NodePlacementError(`bot ${bot.id} active node ${bot.activeNode} is not a placement of agent ${agent.id}`);
    }
  }
  for (const agent of agents) {
    if (!agent.deliveryBotId) continue;
    const bot = botMap.get(agent.deliveryBotId);
    if (!bot) throw new NodePlacementError(`agent ${agent.id} references unknown delivery bot ${agent.deliveryBotId}`);
    if (bot.agentId !== agent.id) {
      throw new NodePlacementError(`agent ${agent.id} delivery bot ${bot.id} belongs to ${bot.agentId}`);
    }
  }

  const jobs: JobPlacement[] = root.jobs.map((raw, index) => {
    const entry = object(raw, `jobs[${index}]`);
    allowedKeys(entry, ['id', 'ownerId', 'runNode'], `jobs[${index}]`);
    return {
      id: idString(entry.id, `jobs[${index}].id`),
      ownerId: idString(entry.ownerId, `jobs[${index}].ownerId`),
      runNode: idString(entry.runNode, `jobs[${index}].runNode`),
    };
  });
  uniqueById(jobs, 'job');
  for (const job of jobs) {
    const agent = ownerMap.get(job.ownerId);
    if (!agent) throw new NodePlacementError(`job ${job.id} references unknown scheduler owner ${job.ownerId}`);
    if (!nodeMap.has(job.runNode)) throw new NodePlacementError(`job ${job.id} references unknown run node ${job.runNode}`);
    if (!agent.placements.includes(job.runNode)) {
      throw new NodePlacementError(`job ${job.id} owner ${agent.id} cannot run on node ${job.runNode}`);
    }
  }

  let userDefaults: UserDefaultPlacement[] | undefined;
  if (root.userDefaults !== undefined) {
    if (!Array.isArray(root.userDefaults)) throw new NodePlacementError('userDefaults must be an array');
    userDefaults = root.userDefaults.map((raw, index) => {
      const entry = object(raw, `userDefaults[${index}]`);
      allowedKeys(entry, ['userId', 'nodeId', 'botId'], `userDefaults[${index}]`);
      return {
        userId: idString(entry.userId, `userDefaults[${index}].userId`),
        nodeId: idString(entry.nodeId, `userDefaults[${index}].nodeId`),
        botId: idString(entry.botId, `userDefaults[${index}].botId`),
      };
    });
    const keys = userDefaults.map(entry => `${entry.userId}\u0000${entry.nodeId}`);
    assertUniqueStrings(keys, 'userDefaults userId/nodeId keys');
    for (const entry of userDefaults) {
      const bot = botMap.get(entry.botId);
      if (!nodeMap.has(entry.nodeId)) throw new NodePlacementError(`user default references unknown node ${entry.nodeId}`);
      if (!bot) throw new NodePlacementError(`user default references unknown bot ${entry.botId}`);
      if (bot.activeNode !== entry.nodeId) {
        throw new NodePlacementError(`user default bot ${entry.botId} is not active on ${entry.nodeId}`);
      }
    }
  }

  const result: NodePlacementDocument = {
    schemaVersion: 1,
    deploymentId: idString(root.deploymentId, 'deploymentId'),
    status: status as PlacementStatus,
    nodes,
    agents,
    bots,
    bindings,
    jobs,
  };
  if (userDefaults) result.userDefaults = userDefaults;
  return result;
}

export function loadNodePlacement(path: string): NodePlacementDocument {
  let parsed: unknown;
  try {
    parsed = JSON.parse(readFileSync(path, 'utf8'));
  } catch (error) {
    throw new NodePlacementError(`cannot read node placement ${path}: ${error instanceof Error ? error.message : String(error)}`);
  }
  return parseNodePlacement(parsed);
}

function sorted<T>(values: Iterable<T>, key: (value: T) => string): T[] {
  return Array.from(values).sort((left, right) => key(left).localeCompare(key(right)));
}

function canonicalize(value: unknown): unknown {
  if (Array.isArray(value)) return value.map(canonicalize);
  if (value && typeof value === 'object') {
    return Object.fromEntries(
      Object.entries(value as Record<string, unknown>)
        .filter(([, entry]) => entry !== undefined)
        .sort(([left], [right]) => left.localeCompare(right))
        .map(([key, entry]) => [key, canonicalize(entry)]),
    );
  }
  return value;
}

export function stableJson(value: unknown): string {
  return `${JSON.stringify(canonicalize(value), null, 2)}\n`;
}

export function sha256(value: string): string {
  return createHash('sha256').update(value).digest('hex');
}

function setDiff(left: Set<string>, right: Set<string>): string[] {
  return sorted(Array.from(left).filter(value => !right.has(value)), value => value);
}

export function validateSourceInventories(
  placement: NodePlacementDocument,
  relaySource: RelayConfig,
  cronSource: CronJobsDocument,
): void {
  const declaredBotMap = new Map(placement.bots.map(bot => [bot.id, bot]));
  const declaredBots = new Set(declaredBotMap.keys());
  assertUniqueStrings(relaySource.users.map(user => user.userId), 'relay source user ids');
  const relayBotList = relaySource.users.flatMap(user => user.agents.map(agent => agent.id));
  assertUniqueStrings(relayBotList, 'relay source bot ids');
  const relayBots = new Set(relayBotList);
  const extraBots = setDiff(relayBots, declaredBots);
  const missingBots = setDiff(declaredBots, relayBots);
  if (extraBots.length || missingBots.length) {
    throw new NodePlacementError(`relay source bot inventory mismatch; extra=[${extraBots.join(',')}], missing=[${missingBots.join(',')}]`);
  }
  for (const agent of relaySource.users.flatMap(user => user.agents)) {
    const declared = declaredBotMap.get(agent.id)!;
    const principal = harnessPrincipalForAgent(agent);
    if (principal !== declared.agentId) {
      throw new NodePlacementError(
        'relay source bot ' + agent.id + ' Harness principal mismatch: source=' + principal
          + ', placement=' + declared.agentId,
      );
    }
  }

  const declaredJobs = new Map(placement.jobs.map(job => [job.id, job]));
  const ownerToAgent = new Map(placement.agents.flatMap(agent => agent.ownerIds.map(ownerId => [ownerId, agent] as const)));
  const sourceJobs = new Map<string, ScheduledJob>();
  for (const job of cronSource.jobs) {
    if (!job?.id) throw new NodePlacementError('cron source contains a job without id');
    if (sourceJobs.has(job.id)) throw new NodePlacementError(`cron source contains duplicate job id ${job.id}`);
    if (job._state !== undefined) {
      throw new NodePlacementError(`cron source job ${job.id} contains dynamic _state; declarative source must not include runtime state`);
    }
    sourceJobs.set(job.id, job);
  }
  const extraJobs = setDiff(new Set(sourceJobs.keys()), new Set(declaredJobs.keys()));
  const missingJobs = setDiff(new Set(declaredJobs.keys()), new Set(sourceJobs.keys()));
  if (extraJobs.length || missingJobs.length) {
    throw new NodePlacementError(`cron source inventory mismatch; extra=[${extraJobs.join(',')}], missing=[${missingJobs.join(',')}]`);
  }
  for (const [jobId, declared] of declaredJobs) {
    const source = sourceJobs.get(jobId)!;
    if (source.owner !== declared.ownerId) {
      throw new NodePlacementError(`job ${jobId} owner mismatch: source=${source.owner}, placement=${declared.ownerId}`);
    }
    if (source.runNode !== undefined && source.runNode !== declared.runNode) {
      throw new NodePlacementError(`job ${jobId} runNode mismatch: source=${source.runNode}, placement=${declared.runNode}`);
    }
    const ownerAgent = ownerToAgent.get(declared.ownerId)!;
    if (['announce', 'telegram_document', 'feishu_document'].includes(source.delivery.mode) && !ownerAgent.deliveryBotId) {
      throw new NodePlacementError(`job ${jobId} delivery mode ${source.delivery.mode} requires explicit deliveryBotId for owner ${declared.ownerId}`);
    }
    if (['announce', 'telegram_document', 'feishu_document'].includes(source.delivery.mode)) {
      const deliveryBot = placement.bots.find(bot => bot.id === ownerAgent.deliveryBotId);
      if (!deliveryBot || deliveryBot.activeNode !== declared.runNode) {
        throw new NodePlacementError(`job ${jobId} delivery bot ${ownerAgent.deliveryBotId} is not active on run node ${declared.runNode}`);
      }
    }
  }

  const declaredBindings = new Map(placement.bindings.map(binding => [binding.id, binding]));
  const sourceBindings = new Map((relaySource.bindings ?? []).map(binding => [binding.id, binding]));
  if (sourceBindings.size !== (relaySource.bindings ?? []).length) {
    throw new NodePlacementError('relay source contains duplicate binding ids');
  }
  const extraBindings = setDiff(new Set(sourceBindings.keys()), new Set(declaredBindings.keys()));
  const missingBindings = setDiff(new Set(declaredBindings.keys()), new Set(sourceBindings.keys()));
  if (extraBindings.length || missingBindings.length) {
    throw new NodePlacementError(`relay source binding inventory mismatch; extra=[${extraBindings.join(',')}], missing=[${missingBindings.join(',')}]`);
  }

  for (const binding of relaySource.bindings ?? []) {
    const principal = relaySource.users.find(user => user.userId === binding.principalId);
    if (!principal) {
      throw new NodePlacementError(`binding ${binding.id} references missing principal ${binding.principalId}`);
    }
    if (binding.allowedAgentIds.length === 0) {
      throw new NodePlacementError(`binding ${binding.id} allowedAgentIds must not be empty`);
    }
    if (!binding.allowedAgentIds.includes(binding.defaultAgentId)) {
      throw new NodePlacementError(`binding ${binding.id} default bot ${binding.defaultAgentId} is not allowed`);
    }
    assertUniqueStrings(binding.allowedAgentIds, `binding ${binding.id} allowed bot ids`);
    const principalBots = new Set(principal.agents.map(agent => agent.id));
    const refs = [binding.defaultAgentId, ...binding.allowedAgentIds];
    if (binding.address.botId) refs.push(binding.address.botId);
    for (const ref of refs) {
      if (!declaredBots.has(ref)) throw new NodePlacementError(`binding ${binding.id} references unknown bot ${ref}`);
      if (!principalBots.has(ref)) {
        throw new NodePlacementError(`binding ${binding.id} references bot ${ref} outside principal ${binding.principalId}`);
      }
    }
    const nodeId = declaredBindings.get(binding.id)!.activeNode;
    const activeOnNode = new Set(placement.bots.filter(bot => bot.activeNode === nodeId).map(bot => bot.id));
    if (!activeOnNode.has(binding.defaultAgentId)) {
      throw new NodePlacementError(`binding ${binding.id} default bot ${binding.defaultAgentId} is not active on ${nodeId}`);
    }
    if (binding.address.botId && !activeOnNode.has(binding.address.botId)) {
      throw new NodePlacementError(`binding ${binding.id} address bot ${binding.address.botId} is not active on ${nodeId}`);
    }
    for (const allowed of binding.allowedAgentIds) {
      if (!activeOnNode.has(allowed)) {
        throw new NodePlacementError(`binding ${binding.id} allowed bot ${allowed} is not active on ${nodeId}`);
      }
    }
  }


  const defaultsByKey = new Map((placement.userDefaults ?? []).map(entry => [`${entry.userId}\u0000${entry.nodeId}`, entry]));
  const expectedDefaultKeys = new Set<string>();
  const botNode = new Map(placement.bots.map(bot => [bot.id, bot.activeNode]));
  for (const user of relaySource.users) {
    const userBotIds = new Set(user.agents.map(agent => agent.id));
    const localNodes = new Set(Array.from(userBotIds).map(id => botNode.get(id)).filter((id): id is string => Boolean(id)));
    for (const nodeId of localNodes) {
      expectedDefaultKeys.add(`${user.userId}\u0000${nodeId}`);
      const declaredDefault = defaultsByKey.get(`${user.userId}\u0000${nodeId}`);
      if (!declaredDefault) throw new NodePlacementError(`user ${user.userId} has bots on ${nodeId} but no explicit userDefault`);
      if (!userBotIds.has(declaredDefault.botId)) {
        throw new NodePlacementError(`user ${user.userId} default bot ${declaredDefault.botId} is not in that user's relay source agents`);
      }
    }
  }
  const extraDefaultKeys = setDiff(new Set(defaultsByKey.keys()), expectedDefaultKeys);
  if (extraDefaultKeys.length) throw new NodePlacementError(`userDefaults contain extra user/node entries: ${extraDefaultKeys.join(',')}`);
  for (const entry of placement.userDefaults ?? []) {
    const user = relaySource.users.find(candidate => candidate.userId === entry.userId);
    if (!user) throw new NodePlacementError(`userDefault references missing relay user ${entry.userId}`);
    if (!user.agents.some(agent => agent.id === entry.botId)) {
      throw new NodePlacementError(`userDefault ${entry.userId}/${entry.nodeId} references bot ${entry.botId} outside that user`);
    }
  }
}

function assertKnownNode(placement: NodePlacementDocument, currentNode: string): void {
  if (!placement.nodes.some(node => node.id === currentNode)) {
    throw new NodePlacementError(`unknown current node: ${currentNode}`);
  }
}

export function projectRelayConfig(
  placement: NodePlacementDocument,
  currentNode: string,
  relaySource: RelayConfig,
): RelayConfig {
  assertKnownNode(placement, currentNode);
  const activeBots = new Set(placement.bots.filter(bot => bot.activeNode === currentNode).map(bot => bot.id));
  const localBindingIds = new Set(placement.bindings.filter(binding => binding.activeNode === currentNode).map(binding => binding.id));
  const defaults = new Map(
    (placement.userDefaults ?? [])
      .filter(entry => entry.nodeId === currentNode)
      .map(entry => [entry.userId, entry.botId]),
  );
  const users = sorted(relaySource.users, user => user.userId).flatMap(user => {
    const agents = sorted(user.agents.filter(agent => activeBots.has(agent.id)), agent => agent.id);
    if (agents.length === 0) return [];
    const defaultAgent = defaults.get(user.userId);
    if (!defaultAgent || !agents.some(agent => agent.id === defaultAgent)) {
      throw new NodePlacementError(`user ${user.userId} has local bots on ${currentNode} but no explicit local default bot`);
    }
    return [{ ...user, defaultAgent, agents }];
  });
  const localUsers = new Set(users.map(user => user.userId));
  const bindings = sorted(relaySource.bindings ?? [], binding => binding.id).flatMap(binding => {
    if (!localBindingIds.has(binding.id)) return [];
    if (!localUsers.has(binding.principalId)) {
      throw new NodePlacementError(`local binding ${binding.id} principal ${binding.principalId} has no local user projection`);
    }
    const addressBot = binding.address.botId;
    if (addressBot && !activeBots.has(addressBot)) {
      throw new NodePlacementError(`local binding ${binding.id} address bot ${addressBot} is not active on ${currentNode}`);
    }
    if (!activeBots.has(binding.defaultAgentId)) {
      throw new NodePlacementError(`local binding ${binding.id} default bot ${binding.defaultAgentId} is not active on ${currentNode}`);
    }
    const allowedAgentIds = sorted(binding.allowedAgentIds, id => id);
    if (allowedAgentIds.length === 0 || !allowedAgentIds.includes(binding.defaultAgentId)) {
      throw new NodePlacementError(`local binding ${binding.id} has invalid allowed/default relationship`);
    }
    return [{ ...binding, allowedAgentIds }];
  });
  return bindings.length ? { users, bindings } : { users };
}

export function projectCronJobs(
  placement: NodePlacementDocument,
  currentNode: string,
  cronSource: CronJobsDocument,
): CronJobsDocument {
  assertKnownNode(placement, currentNode);
  const declared = new Map(placement.jobs.map(job => [job.id, job]));
  const jobs = sorted(
    cronSource.jobs
      .filter(job => declared.get(job.id)?.runNode === currentNode)
      .map(job => ({ ...job, runNode: currentNode })),
    job => job.id,
  );
  const { jobs: _ignored, ...topLevel } = cronSource;
  return { ...topLevel, version: Math.max(3, cronSource.version), jobs };
}

export interface ProjectionResult {
  relay: RelayConfig;
  cron: CronJobsDocument;
  relayBytes: string;
  cronBytes: string;
  relaySha256: string;
  cronSha256: string;
}

export function stageNodePlacedJob(
  placement: NodePlacementDocument,
  currentNode: string,
  cronSource: CronJobsDocument,
  rawJob: Omit<ScheduledJob, '_state'>,
): { placement: NodePlacementDocument; cronSource: CronJobsDocument; job: Omit<ScheduledJob, '_state'> } {
  assertKnownNode(placement, currentNode);
  if (!rawJob.id || !rawJob.owner) throw new NodePlacementError('new job requires id and owner');
  if ('_state' in rawJob) throw new NodePlacementError('new declarative job must not include _state');
  if (placement.jobs.some(job => job.id === rawJob.id) || cronSource.jobs.some(job => job.id === rawJob.id)) {
    throw new NodePlacementError(`duplicate job id: ${rawJob.id}`);
  }
  const ownerAgent = placement.agents.find(agent => agent.ownerIds.includes(rawJob.owner));
  if (!ownerAgent) throw new NodePlacementError(`new job references unknown scheduler owner ${rawJob.owner}`);
  if (!ownerAgent.placements.includes(currentNode)) {
    throw new NodePlacementError(`new job owner ${rawJob.owner} cannot run on ${currentNode}`);
  }
  if (rawJob.runNode && rawJob.runNode !== currentNode) {
    throw new NodePlacementError(`new job runNode ${rawJob.runNode} does not match current node ${currentNode}`);
  }
  if (['announce', 'telegram_document', 'feishu_document'].includes(rawJob.delivery.mode) && !ownerAgent.deliveryBotId) {
    throw new NodePlacementError(`new job delivery mode ${rawJob.delivery.mode} requires explicit deliveryBotId for owner ${rawJob.owner}`);
  }
  if (['announce', 'telegram_document', 'feishu_document'].includes(rawJob.delivery.mode)) {
    const deliveryBot = placement.bots.find(bot => bot.id === ownerAgent.deliveryBotId);
    if (!deliveryBot || deliveryBot.activeNode !== currentNode) {
      throw new NodePlacementError(`new job delivery bot ${ownerAgent.deliveryBotId} is not active on ${currentNode}`);
    }
  }
  const job = { ...rawJob, runNode: currentNode };
  const nextPlacement = parseNodePlacement({
    ...placement,
    jobs: sorted([...placement.jobs, { id: job.id, ownerId: job.owner, runNode: currentNode }], entry => entry.id),
  });
  const { jobs: _ignored, ...topLevel } = cronSource;
  const nextCron: CronJobsDocument = {
    ...topLevel,
    version: Math.max(3, cronSource.version),
    jobs: sorted([...cronSource.jobs, job], entry => entry.id),
  };
  return { placement: nextPlacement, cronSource: nextCron, job };
}

export function projectNodeConfig(
  placement: NodePlacementDocument,
  currentNode: string,
  relaySource: RelayConfig,
  cronSource: CronJobsDocument,
): ProjectionResult {
  validateSourceInventories(placement, relaySource, cronSource);
  const relay = projectRelayConfig(placement, currentNode, relaySource);
  const cron = projectCronJobs(placement, currentNode, cronSource);
  const relayBytes = stableJson(relay);
  const cronBytes = stableJson(cron);
  return {
    relay,
    cron,
    relayBytes,
    cronBytes,
    relaySha256: sha256(relayBytes),
    cronSha256: sha256(cronBytes),
  };
}

function readJsonFile<T>(path: string, label: string): T {
  try {
    return JSON.parse(readFileSync(path, 'utf8')) as T;
  } catch (error) {
    throw new NodePlacementError(`cannot read ${label} ${path}: ${error instanceof Error ? error.message : String(error)}`);
  }
}

export interface PlacementRuntimePaths {
  placementPath: string;
  currentNode: string;
  relaySourcePath: string;
  cronSourcePath: string;
}

export function resolvePlacementRuntimePaths(explicit: Partial<PlacementRuntimePaths> = {}): PlacementRuntimePaths | undefined {
  const values = {
    placementPath: explicit.placementPath ?? process.env[NODE_PLACEMENT_PATH_ENV],
    currentNode: explicit.currentNode ?? process.env[CURRENT_NODE_ENV],
    relaySourcePath: explicit.relaySourcePath ?? process.env[NODE_PLACEMENT_RELAY_SOURCE_ENV],
    cronSourcePath: explicit.cronSourcePath ?? process.env[NODE_PLACEMENT_CRON_SOURCE_ENV],
  };
  const present = Object.values(values).filter(Boolean).length;
  if (present === 0) return undefined; // compatibility gate until runbook activation
  if (present !== 4) {
    throw new NodePlacementError(`node placement runtime requires ${NODE_PLACEMENT_PATH_ENV}, ${CURRENT_NODE_ENV}, ${NODE_PLACEMENT_RELAY_SOURCE_ENV} and ${NODE_PLACEMENT_CRON_SOURCE_ENV} together`);
  }
  return values as PlacementRuntimePaths;
}

export interface PlacementRuntimeContext extends PlacementRuntimePaths {
  placement: NodePlacementDocument;
  activeBotIds: Set<string>;
  ownerCanRunOnNode(ownerAgentId: string, nodeId?: string): boolean;
  deliveryBotIdForOwner(ownerAgentId: string): string | undefined;
  preparePreflight(relayLocalPath: string, cronLocalPath: string): { projection: ProjectionResult; commit(): void };
  assertPreflight(relayLocalPath: string, cronLocalPath: string): ProjectionResult;
}

export function loadPlacementRuntimeContext(explicit: Partial<PlacementRuntimePaths> = {}): PlacementRuntimeContext | undefined {
  const paths = resolvePlacementRuntimePaths(explicit);
  if (!paths) return undefined;
  const initialPlacement = loadNodePlacement(paths.placementPath);
  if (initialPlacement.status !== 'active') {
    throw new NodePlacementError(`placement ${initialPlacement.deploymentId} is ${initialPlacement.status}; Relay requires status=active`);
  }
  assertKnownNode(initialPlacement, paths.currentNode);
  let ownerMap = new Map(initialPlacement.agents.flatMap(agent => agent.ownerIds.map(ownerId => [ownerId, agent] as const)));
  const activeBotIds = new Set(initialPlacement.bots.filter(bot => bot.activeNode === paths.currentNode).map(bot => bot.id));
  const context: PlacementRuntimeContext = {
    ...paths,
    placement: initialPlacement,
    activeBotIds,
    ownerCanRunOnNode(ownerAgentId: string, nodeId = paths.currentNode): boolean {
      return ownerMap.get(ownerAgentId)?.placements.includes(nodeId) ?? false;
    },
    deliveryBotIdForOwner(ownerAgentId: string): string | undefined {
      const id = ownerMap.get(ownerAgentId)?.deliveryBotId;
      return id && activeBotIds.has(id) ? id : undefined;
    },
    preparePreflight(relayLocalPath: string, cronLocalPath: string): { projection: ProjectionResult; commit(): void } {
      const nextPlacement = loadNodePlacement(paths.placementPath);
      if (nextPlacement.status !== 'active') {
        throw new NodePlacementError(`placement ${nextPlacement.deploymentId} is ${nextPlacement.status}; Relay requires status=active`);
      }
      const relaySource = readJsonFile<RelayConfig>(paths.relaySourcePath, 'relay source');
      const cronSource = readJsonFile<CronJobsDocument>(paths.cronSourcePath, 'cron source');
      const projected = projectNodeConfig(nextPlacement, paths.currentNode, relaySource, cronSource);
      const relayLocal = stableJson(readJsonFile<RelayConfig>(relayLocalPath, 'local relay config'));
      const cronLocal = stableJson(readJsonFile<CronJobsDocument>(cronLocalPath, 'local cron config'));
      if (relayLocal !== projected.relayBytes) {
        throw new NodePlacementError(`local relay projection mismatch: expected sha256:${projected.relaySha256}, actual sha256:${sha256(relayLocal)}`);
      }
      if (cronLocal !== projected.cronBytes) {
        throw new NodePlacementError(`local cron projection mismatch: expected sha256:${projected.cronSha256}, actual sha256:${sha256(cronLocal)}`);
      }
      return {
        projection: projected,
        commit(): void {
          // Publish only after the caller's runtime fences (notably no running
          // scheduler jobs) pass. Validation itself must not redirect a live
          // in-flight delivery through the next generation.
          context.placement = nextPlacement;
          ownerMap = new Map(nextPlacement.agents.flatMap(agent => agent.ownerIds.map(ownerId => [ownerId, agent] as const)));
          activeBotIds.clear();
          for (const bot of nextPlacement.bots) if (bot.activeNode === paths.currentNode) activeBotIds.add(bot.id);
        },
      };
    },
    assertPreflight(relayLocalPath: string, cronLocalPath: string): ProjectionResult {
      const prepared = context.preparePreflight(relayLocalPath, cronLocalPath);
      prepared.commit();
      return prepared.projection;
    },
  };
  return context;
}

export function readRelaySource(path: string): RelayConfig {
  return readJsonFile<RelayConfig>(path, 'relay source');
}

export function readCronSource(path: string): CronJobsDocument {
  return readJsonFile<CronJobsDocument>(path, 'cron source');
}
