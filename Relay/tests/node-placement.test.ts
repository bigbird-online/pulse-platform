import { afterEach, describe, expect, it, vi } from 'vitest';
import { mkdtempSync, readFileSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import {
  NodePlacementError,
  loadPlacementRuntimeContext,
  parseNodePlacement,
  projectNodeConfig,
  stageNodePlacedJob,
  stableJson,
  validateSourceInventories,
  type CronJobsDocument,
  type NodePlacementDocument,
} from '../src/node-placement.js';
import { createDeferredFeishuWsLifecycle, reconcileFeishuClientRegistry, wirePoolEvents } from '../src/channels/feishu.js';
import { activeTelegramListener, reconcileTelegramClientRegistry } from '../src/channels/telegram.js';
import { createScheduler, prepareNodePlacedJob } from '../src/scheduler.js';
import type { Bot } from 'grammy';
import type { RelayConfig, ScheduledJob } from '../src/types.js';
import { main as nodePlacementCliMain } from '../src/node-placement-cli.js';

function job(id: string, owner: string, delivery: ScheduledJob['delivery'] = { mode: 'none' }): ScheduledJob {
  return {
    id,
    name: id,
    enabled: true,
    owner,
    schedule: { expr: '0 6 * * *', tz: 'UTC' },
    agent: { workspace: '/tmp', prompt: id },
    delivery,
  };
}

function placement(status: 'candidate' | 'active' = 'candidate'): NodePlacementDocument {
  return parseNodePlacement({
    schemaVersion: 1,
    deploymentId: 'test-placement',
    status,
    nodes: [{ id: 'node-a' }, { id: 'node-b' }],
    agents: [
      { id: 'logical-a', placements: ['node-a', 'node-b'], ownerIds: ['owner-a', 'owner-a-legacy'], deliveryBotId: 'bot-a' },
      { id: 'logical-b', placements: ['node-b'], ownerIds: ['owner-b'], deliveryBotId: 'bot-b' },
    ],
    bots: [
      { id: 'bot-a', agentId: 'logical-a', activeNode: 'node-a', credentialRefs: { telegramToken: 'env:TEST_BOT_A' } },
      { id: 'bot-b', agentId: 'logical-b', activeNode: 'node-b', credentialRefs: { telegramToken: 'env:TEST_BOT_B' } },
    ],
    bindings: [
      { id: 'binding-a', activeNode: 'node-a' },
      { id: 'binding-b', activeNode: 'node-b' },
    ],
    jobs: [
      { id: 'job-a', ownerId: 'owner-a-legacy', runNode: 'node-a' },
      { id: 'job-b', ownerId: 'owner-b', runNode: 'node-b' },
    ],
    userDefaults: [
      { userId: 'user', nodeId: 'node-a', botId: 'bot-a' },
      { userId: 'user', nodeId: 'node-b', botId: 'bot-b' },
    ],
  });
}

function relaySource(): RelayConfig {
  return {
    users: [{
      userId: 'user',
      chatId: 'not-a-secret-fixture',
      defaultAgent: 'bot-a',
      agents: [
        { id: 'bot-b', harnessAgentId: 'logical-b', name: 'B', emoji: 'B', botToken: 'fixture-b', workspace: '/tmp/b' },
        { id: 'bot-a', harnessAgentId: 'logical-a', name: 'A', emoji: 'A', botToken: 'fixture-a', workspace: '/tmp/a' },
      ],
    }],
    bindings: [
      {
        id: 'binding-b', principalId: 'user', tenantId: 'user', defaultAgentId: 'bot-b', allowedAgentIds: ['bot-b'],
        address: { channel: 'telegram', kind: 'telegram_chat', value: 'b', botId: 'bot-b' },
        status: 'active', createdAt: '2026-01-01T00:00:00Z',
      },
      {
        id: 'binding-a', principalId: 'user', tenantId: 'user', defaultAgentId: 'bot-a', allowedAgentIds: ['bot-a'],
        address: { channel: 'telegram', kind: 'telegram_chat', value: 'a', botId: 'bot-a' },
        status: 'active', createdAt: '2026-01-01T00:00:00Z',
      },
    ],
  };
}

function cronSource(): CronJobsDocument {
  return { version: 2, jobs: [job('job-b', 'owner-b'), job('job-a', 'owner-a-legacy')] };
}

describe('node placement schema and projection', () => {
  it('requires Relay aliases to preserve the declared Harness principal', () => {
    const relay = relaySource();
    delete relay.users[0]!.agents[0]!.harnessAgentId;
    expect(() => validateSourceInventories(placement(), relay, cronSource()))
      .toThrow(/bot-b Harness principal mismatch.*source=bot-b.*placement=logical-b/);

    relay.users[0]!.agents[0]!.harnessAgentId = 'logical-b';
    expect(() => validateSourceInventories(placement(), relay, cronSource())).not.toThrow();
  });

  it('rejects duplicate bot identity (the scalar activeNode cannot be multi-active)', () => {
    const raw = JSON.parse(stableJson(placement()));
    raw.bots.push({ ...raw.bots[0], activeNode: 'node-b' });
    expect(() => parseNodePlacement(raw)).toThrow(/duplicate bot id/);
  });

  it('rejects unknown nodes, overlapping owner mappings, missing owners and unsupported versions', () => {
    const raw = JSON.parse(stableJson(placement()));
    raw.agents[1].ownerIds = ['owner-a'];
    expect(() => parseNodePlacement(raw)).toThrow(/maps to both/);

    const missingOwner = JSON.parse(stableJson(placement()));
    missingOwner.jobs[0].ownerId = 'ghost';
    expect(() => parseNodePlacement(missingOwner)).toThrow(/unknown scheduler owner/);

    const unknownNode = JSON.parse(stableJson(placement()));
    unknownNode.jobs[0].runNode = 'ghost';
    expect(() => parseNodePlacement(unknownNode)).toThrow(/unknown run node/);

    const unsupported = JSON.parse(stableJson(placement()));
    unsupported.schemaVersion = 2;
    expect(() => parseNodePlacement(unsupported)).toThrow(/unsupported.*schemaVersion/);
  });

  it('rejects unknown fields and requires explicit owner ids exactly like the checked schema', () => {
    const rootTypo = JSON.parse(stableJson(placement()));
    rootTypo.activeNdoe = 'node-a';
    expect(() => parseNodePlacement(rootTypo)).toThrow(/unknown field.*activeNdoe/);

    const entryTypo = JSON.parse(stableJson(placement()));
    entryTypo.bots[0].activeNdoe = 'node-a';
    expect(() => parseNodePlacement(entryTypo)).toThrow(/bots\[0\].*unknown field/);

    const implicitOwner = JSON.parse(stableJson(placement()));
    delete implicitOwner.agents[0].ownerIds;
    expect(() => parseNodePlacement(implicitOwner)).toThrow(/ownerIds must be a non-empty id array/);

    const invalidId = JSON.parse(stableJson(placement()));
    invalidId.nodes[0].id = 'node a';
    expect(() => parseNodePlacement(invalidId)).toThrow(/placement id contract/);
  });

  it('candidate may carry obvious credential placeholders but active placement rejects them', () => {
    const raw = JSON.parse(stableJson(placement()));
    raw.bots[0].credentialRefs.telegramToken = 'env:TODO_REAL_KEY';
    expect(() => parseNodePlacement(raw)).not.toThrow();
    raw.status = 'active';
    expect(() => parseNodePlacement(raw)).toThrow(/placeholder credential/);
  });

  it('projects only current-node users, bots, bindings and jobs with stable bytes', () => {
    const first = projectNodeConfig(placement(), 'node-a', relaySource(), cronSource());
    const reversedRelay = relaySource();
    reversedRelay.users[0].agents.reverse();
    reversedRelay.bindings!.reverse();
    const reversedCron = cronSource();
    reversedCron.jobs.reverse();
    const second = projectNodeConfig(placement(), 'node-a', reversedRelay, reversedCron);

    expect(first.relayBytes).toBe(second.relayBytes);
    expect(first.cronBytes).toBe(second.cronBytes);
    expect(first.relay.users[0].agents.map(agent => agent.id)).toEqual(['bot-a']);
    expect(first.relay.bindings?.map(binding => binding.id)).toEqual(['binding-a']);
    expect(first.cron.jobs.map(entry => [entry.id, entry.runNode])).toEqual([['job-a', 'node-a']]);
    expect(first.cron.version).toBe(3);
  });

  it('fails closed on extra/missing binding, bot, job, dynamic state and cross-node allowed bot', () => {
    const relay = relaySource();
    relay.bindings!.push({ ...relay.bindings![0], id: 'extra-binding' });
    expect(() => validateSourceInventories(placement(), relay, cronSource())).toThrow(/binding inventory mismatch/);

    const crossNode = relaySource();
    crossNode.bindings![0].allowedAgentIds.push('bot-a');
    expect(() => validateSourceInventories(placement(), crossNode, cronSource())).toThrow(/allowed bot bot-a is not active/);

    const cron = cronSource();
    cron.jobs[0]._state = { consecutiveErrors: 0 };
    expect(() => validateSourceInventories(placement(), relaySource(), cron)).toThrow(/dynamic _state/);

    const missingJob = cronSource();
    missingJob.jobs.pop();
    expect(() => validateSourceInventories(placement(), relaySource(), missingJob)).toThrow(/cron source inventory mismatch/);

    const wrongRunNode = cronSource();
    wrongRunNode.jobs[0].runNode = 'node-a';
    expect(() => validateSourceInventories(placement(), relaySource(), wrongRunNode)).toThrow(/runNode mismatch/);
  });

  it('requires exact user/node defaults and never infers source default across nodes', () => {
    const raw = JSON.parse(stableJson(placement()));
    raw.userDefaults = raw.userDefaults.filter((entry: { nodeId: string }) => entry.nodeId !== 'node-b');
    const parsed = parseNodePlacement(raw);
    expect(() => validateSourceInventories(parsed, relaySource(), cronSource())).toThrow(/no explicit userDefault/);
  });

  it('rejects duplicate relay identities, extra defaults and cross-principal bindings before projection', () => {
    const duplicateUser = relaySource();
    duplicateUser.users.push({ ...duplicateUser.users[0], agents: [] });
    expect(() => validateSourceInventories(placement(), duplicateUser, cronSource())).toThrow(/duplicate values/);

    const duplicateBot = relaySource();
    duplicateBot.users.push({ userId: 'other', defaultAgent: 'bot-a', agents: [duplicateBot.users[0].agents[0]] });
    expect(() => validateSourceInventories(placement(), duplicateBot, cronSource())).toThrow(/bot ids contains duplicate/);

    const raw = JSON.parse(stableJson(placement()));
    raw.userDefaults.push({ userId: 'ghost', nodeId: 'node-a', botId: 'bot-a' });
    expect(() => validateSourceInventories(parseNodePlacement(raw), relaySource(), cronSource())).toThrow(/extra user\/node entries/);

    const wrongPrincipal = relaySource();
    wrongPrincipal.users.push({ userId: 'other', defaultAgent: 'bot-a', agents: [] });
    wrongPrincipal.bindings![0].principalId = 'other';
    expect(() => validateSourceInventories(placement(), wrongPrincipal, cronSource())).toThrow(/outside principal other/);

    const defaultNotAllowed = relaySource();
    defaultNotAllowed.bindings![0].allowedAgentIds = ['bot-a'];
    expect(() => validateSourceInventories(placement(), defaultNotAllowed, cronSource())).toThrow(/default bot bot-b is not allowed/);
  });

  it('a job and one-to-one bot belong to exactly one node projection during migration/rollback', () => {
    const a = projectNodeConfig(placement(), 'node-a', relaySource(), cronSource());
    const b = projectNodeConfig(placement(), 'node-b', relaySource(), cronSource());
    const botIntersection = a.relay.users[0].agents.map(x => x.id).filter(id => b.relay.users[0].agents.some(x => x.id === id));
    const jobIntersection = a.cron.jobs.map(x => x.id).filter(id => b.cron.jobs.some(x => x.id === id));
    expect(botIntersection).toEqual([]);
    expect(jobIntersection).toEqual([]);
    expect(new Set([...a.cron.jobs, ...b.cron.jobs].map(x => x.id))).toEqual(new Set(['job-a', 'job-b']));
  });

  it('stages a new job into SSOT and global cron source with current runNode, never runtime state', () => {
    const staged = stageNodePlacedJob(placement(), 'node-a', cronSource(), job('job-new', 'owner-a'));
    expect(staged.job.runNode).toBe('node-a');
    expect(staged.placement.jobs.find(entry => entry.id === 'job-new')).toEqual({ id: 'job-new', ownerId: 'owner-a', runNode: 'node-a' });
    expect(staged.cronSource.version).toBe(3);
    expect(staged.cronSource.jobs.find(entry => entry.id === 'job-new')?._state).toBeUndefined();
  });
});

describe('runtime preflight', () => {
  const tempDirs: string[] = [];
  afterEach(() => {
    for (const path of tempDirs.splice(0)) rmSync(path, { recursive: true, force: true });
  });

  function temp(): string {
    const path = mkdtempSync(join(tmpdir(), 'node-placement-test-'));
    tempDirs.push(path);
    return path;
  }

  it('rejects missing/malformed/candidate SSOT before a writer starts', () => {
    expect(() => loadPlacementRuntimeContext({ placementPath: '/missing', currentNode: 'node-a', relaySourcePath: '/missing-relay', cronSourcePath: '/missing-cron' })).toThrow(/cannot read node placement/);
    const dir = temp();
    const bad = join(dir, 'bad.json');
    writeFileSync(bad, '{');
    expect(() => loadPlacementRuntimeContext({ placementPath: bad, currentNode: 'node-a', relaySourcePath: bad, cronSourcePath: bad })).toThrow(/cannot read node placement/);
    const candidate = join(dir, 'candidate.json');
    writeFileSync(candidate, stableJson(placement()));
    expect(() => loadPlacementRuntimeContext({ placementPath: candidate, currentNode: 'node-a', relaySourcePath: bad, cronSourcePath: bad })).toThrow(/requires status=active/);
  });

  it('preflight rejects a local extra bot/digest mismatch', () => {
    const dir = temp();
    const p = placement('active');
    const projected = projectNodeConfig(p, 'node-a', relaySource(), cronSource());
    const paths = {
      placementPath: join(dir, 'placement.json'), currentNode: 'node-a',
      relaySourcePath: join(dir, 'relay-source.json'), cronSourcePath: join(dir, 'cron-source.json'),
    };
    writeFileSync(paths.placementPath, stableJson(p));
    writeFileSync(paths.relaySourcePath, stableJson(relaySource()));
    writeFileSync(paths.cronSourcePath, stableJson(cronSource()));
    const relayLocal = join(dir, 'relay-local.json');
    const cronLocal = join(dir, 'cron-local.json');
    writeFileSync(relayLocal, projected.relayBytes);
    writeFileSync(cronLocal, projected.cronBytes);
    const context = loadPlacementRuntimeContext(paths)!;
    expect(() => context.assertPreflight(relayLocal, cronLocal)).not.toThrow();
    const next = JSON.parse(stableJson(p));
    next.deploymentId = 'next-generation';
    writeFileSync(paths.placementPath, stableJson(next));
    const prepared = context.preparePreflight(relayLocal, cronLocal);
    expect(context.placement.deploymentId).toBe('test-placement');
    prepared.commit();
    expect(context.placement.deploymentId).toBe('next-generation');
    const extra = JSON.parse(readFileSync(relayLocal, 'utf8'));
    extra.users[0].agents.push(relaySource().users[0].agents[1]);
    writeFileSync(relayLocal, stableJson(extra));
    expect(() => context.assertPreflight(relayLocal, cronLocal)).toThrow(/local relay projection mismatch/);

    const extraBinding = JSON.parse(projected.relayBytes);
    extraBinding.bindings.push({ ...extraBinding.bindings[0], id: 'binding-extra-local' });
    writeFileSync(relayLocal, stableJson(extraBinding));
    expect(() => context.assertPreflight(relayLocal, cronLocal)).toThrow(/local relay projection mismatch/);

    writeFileSync(relayLocal, projected.relayBytes);
    const missingLocalJob = JSON.parse(projected.cronBytes);
    missingLocalJob.jobs.pop();
    writeFileSync(cronLocal, stableJson(missingLocalJob));
    expect(() => context.assertPreflight(relayLocal, cronLocal)).toThrow(/local cron projection mismatch/);
  });

  it('CLI project is staging-only and generated files pass CLI preflight', async () => {
    const dir = temp();
    const paths = {
      ssot: join(dir, 'placement.json'), relay: join(dir, 'relay-source.json'), cron: join(dir, 'cron-source.json'),
      relayOut: join(dir, 'relay-stage.json'), cronOut: join(dir, 'cron-stage.json'),
    };
    writeFileSync(paths.ssot, stableJson(placement('active')));
    writeFileSync(paths.relay, stableJson(relaySource()));
    writeFileSync(paths.cron, stableJson(cronSource()));
    const log = vi.spyOn(console, 'log').mockImplementation(() => {});
    const error = vi.spyOn(console, 'error').mockImplementation(() => {});
    try {
      expect(await nodePlacementCliMain([
        'project', '--ssot', paths.ssot, '--current-node', 'node-a', '--relay-source', paths.relay,
        '--cron-source', paths.cron, '--relay-out', paths.relayOut, '--cron-out', paths.cronOut,
      ])).toBe(0);
      expect(await nodePlacementCliMain([
        'preflight', '--ssot', paths.ssot, '--current-node', 'node-a', '--relay-source', paths.relay,
        '--cron-source', paths.cron, '--relay-local', paths.relayOut, '--cron-local', paths.cronOut,
      ])).toBe(0);
      expect(await nodePlacementCliMain([
        'project', '--ssot', paths.ssot, '--current-node', 'node-a', '--relay-source', paths.relay,
        '--cron-source', paths.cron, '--relay-out', paths.relayOut, '--cron-out', paths.cronOut,
      ])).toBe(1);
      expect(error).toHaveBeenCalledWith(expect.stringMatching(/outputs must both be new staging paths/));
    } finally {
      log.mockRestore();
      error.mockRestore();
    }
  });
});

describe('Feishu and scheduler node lifecycle', () => {
  it('constructs Feishu WS lifecycle without starting and awaits the real start failure', async () => {
    const ws = {
      start: vi.fn(async () => { throw new Error('connect failed'); }),
      close: vi.fn(),
    };
    const lifecycle = createDeferredFeishuWsLifecycle(ws, { dispatcher: true });
    expect(ws.start).not.toHaveBeenCalled();
    await expect(lifecycle.start()).rejects.toThrow(/connect failed/);
    expect(ws.start).toHaveBeenCalledOnce();
  });

  it('stops and deletes removed Feishu client before adding the new registry', async () => {
    const events: string[] = [];
    const running = new Map<string, { start(): Promise<void>; stop(): void }>([
      ['old', { start: async () => {}, stop: () => { events.push('stop:old'); } }],
    ]);
    const desired = new Map([['new', {}]]);
    const result = await reconcileFeishuClientRegistry(running, desired, id => ({
      start: async () => { events.push(`start:${id}`); },
      stop: () => { events.push(`stop:${id}`); },
    }));
    expect(events).toEqual(['stop:old', 'start:new']);
    expect(result).toEqual({ added: ['new'], removed: ['old'], total: 1 });
    expect(Array.from(running.keys())).toEqual(['new']);
  });

  it('does not report reconcile success when a new Feishu client fails to start', async () => {
    const running = new Map<string, { start(): Promise<void>; stop(): void }>();
    await expect(reconcileFeishuClientRegistry(running, new Map([['bad', {}]]), () => ({
      start: async () => { throw new Error('offline'); },
      stop: vi.fn(),
    }))).rejects.toThrow(/start bad/);
    expect(running.size).toBe(0);
  });

  it('does not start additions when any Feishu removal fails', async () => {
    const startNew = vi.fn(async () => {});
    const old = { start: vi.fn(async () => {}), stop: vi.fn(() => { throw new Error('stuck'); }) };
    const running = new Map([['old', old]]);
    await expect(reconcileFeishuClientRegistry(running, new Map([['new', {}]]), () => ({ start: startNew, stop: vi.fn() })))
      .rejects.toThrow(/before additions/);
    expect(startNew).not.toHaveBeenCalled();
    expect(running.get('old')).toBe(old);
  });

  it('rolls back every Feishu addition when a later start fails', async () => {
    const stopped: string[] = [];
    const running = new Map<string, { start(): Promise<void>; stop(): void }>();
    await expect(reconcileFeishuClientRegistry(running, new Map([['a', {}], ['b', {}]]), id => ({
      start: async () => { if (id === 'b') throw new Error('offline'); },
      stop: () => { stopped.push(id); },
    }))).rejects.toThrow(/start b/);
    expect(running.size).toBe(0);
    expect(stopped).toEqual(expect.arrayContaining(['a', 'b']));
  });

  it('Telegram exact registry stops removals first and rolls back partial additions', async () => {
    const events: string[] = [];
    const running = new Map<string, { start(): Promise<void>; stop(): void }>([
      ['old', { start: async () => {}, stop: () => { events.push('stop:old'); } }],
    ]);
    await expect(reconcileTelegramClientRegistry(running, new Map([['a', {}], ['b', {}]]), id => ({
      start: async () => { events.push(`start:${id}`); if (id === 'b') throw new Error('offline'); },
      stop: () => { events.push(`stop:${id}`); },
    }))).rejects.toThrow(/start b/);
    expect(events.slice(0, 2)).toEqual(['stop:old', 'start:a']);
    expect(running.size).toBe(0);
    expect(events).toEqual(expect.arrayContaining(['stop:a', 'stop:b']));
  });

  it('inactive Telegram shared-runtime listeners cannot emit through a removed writer', () => {
    let active = true;
    const apiSend = vi.fn();
    const oldListener = activeTelegramListener(() => active, () => apiSend());
    oldListener();
    expect(apiSend).toHaveBeenCalledOnce();
    active = false;
    oldListener();
    oldListener();
    expect(apiSend).toHaveBeenCalledOnce();
  });

  it('inactive Feishu instance listeners cannot emit through a removed writer', () => {
    const listeners = new Map<string, (...args: any[]) => void>();
    const pool = { on: (name: string, handler: (...args: any[]) => void) => { listeners.set(name, handler); } };
    const sends = vi.fn();
    let active = true;
    const agent = { id: 'bot-a', name: 'A', emoji: 'A', workspace: '/tmp', feishu: { appId: 'fixture', appSecret: 'fixture' } };
    const router = { poolKey: () => 'user:bot-a', getUser: () => undefined, getAgents: () => [agent] };
    wirePoolEvents(
      agent as any,
      pool as any,
      new Map([['user:bot-a', 'ou_fixture']]),
      router as any,
      sends,
      vi.fn(),
      undefined,
      undefined,
      undefined,
      undefined,
      () => active,
    );
    listeners.get('turn-warning')?.('user:bot-a', 8, 10);
    expect(sends).toHaveBeenCalledOnce();
    active = false;
    listeners.get('turn-warning')?.('user:bot-a', 9, 10);
    expect(sends).toHaveBeenCalledOnce();
  });

  it('scheduler filters non-local jobs, rejects missing owner and disables direct projection CRUD', () => {
    const dir = mkdtempSync(join(tmpdir(), 'scheduler-placement-'));
    try {
      const path = join(dir, 'cron.json');
      writeFileSync(path, stableJson({ version: 3, jobs: [
        { ...job('local', 'owner-a'), runNode: 'node-a' },
        { ...job('remote', 'owner-b'), runNode: 'node-b' },
      ] }));
      process.env['CRON_JOBS_STATE_PATH'] = join(dir, 'state.json');
      const scheduler = createScheduler({ api: {} } as unknown as Bot, path, {
        registerCronTasks: false,
        currentNode: 'node-a',
        ownerCanRunOnNode: (owner, node) => (owner === 'owner-a' && node === 'node-a') || (owner === 'owner-b' && node === 'node-b'),
      });
      scheduler.start();
      expect(scheduler.listJobs().map(entry => entry.id)).toEqual(['local']);
      expect(() => scheduler.addJob(job('new', 'owner-a'))).toThrow(/Direct scheduler CRUD is disabled/);
      expect(() => scheduler.removeJob('local', 'owner-a')).toThrow(/Direct scheduler CRUD is disabled/);
      scheduler.stop();

      const badPath = join(dir, 'bad.json');
      writeFileSync(badPath, stableJson({ version: 3, jobs: [{ ...job('bad', 'ghost'), runNode: 'node-a' }] }));
      const bad = createScheduler({ api: {} } as unknown as Bot, badPath, {
        registerCronTasks: false,
        currentNode: 'node-a',
        ownerCanRunOnNode: () => false,
      });
      expect(() => bad.start()).toThrow(/owner "ghost" cannot run/);
    } finally {
      delete process.env['CRON_JOBS_STATE_PATH'];
      rmSync(dir, { recursive: true, force: true });
    }
  });

  it('new-job preparation records current node and validates owner without bot-name fallback', () => {
    const prepared = prepareNodePlacedJob(job('new', 'owner-a'), 'node-a', (owner, node) => owner === 'owner-a' && node === 'node-a');
    expect(prepared.runNode).toBe('node-a');
    expect(() => prepareNodePlacedJob(job('bad', 'ghost'), 'node-a', () => false)).toThrow(/cannot run/);
  });
});
