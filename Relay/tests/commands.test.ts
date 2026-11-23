import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { settleWiSwitch } from '../src/native-work-item-command.js';
import { adoptAgentSwitchRequests } from '../src/agent-switch-request.js';
import { readFileSync, writeFileSync, mkdirSync, rmSync, realpathSync } from 'node:fs';
import { join } from 'node:path';
import { tmpdir } from 'node:os';
import { createRouter } from '../src/router.js';
import { createStateManager as createRealStateManager } from '../src/state.js';
import { conversationLaunch } from '../src/conversation-launch.js';
import { createModelCatalog } from '../src/model-catalog.js';
import { buildRuntimeUsageBinding, runtimeAuthorityRef } from '../src/runtime-usage-binding.js';
import {
  handleStart,
  handleList,
  handleSwitch,
  handleStatus,
  handleHelp,
  handleRuntime,
  handleYolo,
  handleCc,
  handleModel,
  handleModelScope,
  handleModels,
  handleThink,
  handleCompact,
  handleContext,
  handleKill,
  handleRestart,
  handleNew,
  handleReset,
  handleStop,
  handleSession,
  buildRuntimeOptionCard,
  handleUsage,
  handleUsageSeal,
  resolveInboundRoutingAgent,
  resolveAgentFromPoolKey,
  withAgentPickerUi,
  RELAY_SWITCH_CB_PREFIX,
} from '../src/commands.js';
import type { PersistedSession, Pool, ProcessStatusInfo } from '../src/types.js';
import type { TerminalManager } from '../src/web-terminal.js';

const testDir = join(tmpdir(), 'relay-commands-test-' + Date.now());

function writeTmpConfig(name: string, data: unknown): string {
  mkdirSync(testDir, { recursive: true });
  const p = join(testDir, name);
  writeFileSync(p, JSON.stringify(data));
  return p;
}

const sampleConfig = {
  users: [
    {
      userId: 'owner',
      chatId: '111222333',
      defaultAgent: 'main',
      agents: [
        { id: 'main', name: 'Duke', emoji: '\u{1F3B2}', botToken: 'fake-duke', workspace: '/home/duke-space', sessionId: 'uuid-duke', maxTurns: 20 },
        { id: 'ken', name: 'Ken', emoji: '\u{1F527}', botToken: 'fake-ken', workspace: '/home/ken-space', sessionId: 'uuid-ken' },
        { id: 'aki', name: 'Aki', emoji: '\u{1F342}', botToken: 'fake-aki', workspace: '/home/aki-space', sessionId: 'uuid-aki', runtime: 'codex' },
      ],
    },
  ],
};

// Mock pool for testing
function createMockPool(statusMap: Record<string, ProcessStatusInfo> = {}): Pool {
  return {
    spawn: async () => { throw new Error('not implemented'); },
    send: async () => {},
    kill: async () => {},
    killAll: async () => {},
    get: (tenantId: string) => {
      const status = statusMap[tenantId];
      if (!status) return undefined;
      return {
        tenantId,
        process: { pid: status.pid } as never,
        sessionId: 'mock-session',
        state: status.state,
        lastActivity: status.lastActivity,
        spawnedAt: status.spawnedAt,
        restartCount: status.restartCount,
        restartWindowStart: Date.now(),
      };
    },
    getAll: () => [],
    getStatus: () => Object.values(statusMap),
    on: () => {},
    emitShutdown: () => {},
    dispose: () => {},
  };
}

function setupRouter(): ReturnType<typeof createRouter> {
  const path = join(testDir, 'relay.json');
  writeFileSync(path, JSON.stringify(sampleConfig));
  return createRouter(path);
}

function readConfig(): typeof sampleConfig {
  return JSON.parse(readFileSync(join(testDir, 'relay.json'), 'utf-8'));
}

function createStateManager(initial: Record<string, PersistedSession> = {}) {
  const sessions = { ...initial };
  let flushed = false;
  return {
    get flushed() { return flushed; },
    load: () => ({ version: 1 as const, updatedAt: new Date().toISOString(), sessions }),
    getSession: (poolKey: string) => sessions[poolKey],
    updateSession: (poolKey: string, update: Partial<PersistedSession>) => {
      sessions[poolKey] = { ...sessions[poolKey], ...update } as PersistedSession;
    },
    removeSession: (poolKey: string) => {
      delete sessions[poolKey];
    },
    getAllSessions: () => sessions,
    flush: () => { flushed = true; },
    flushStrict: () => { flushed = true; },
    dispose: () => {},
  };
}

const originalRelayHome = process.env['RELAY_HOME'];
beforeEach(() => {
  mkdirSync(testDir, { recursive: true });
  process.env['RELAY_HOME'] = testDir;
});

afterEach(() => {
  rmSync(testDir, { recursive: true, force: true });
  if (originalRelayHome === undefined) delete process.env['RELAY_HOME'];
  else process.env['RELAY_HOME'] = originalRelayHome;
});

describe('resolveInboundRoutingAgent / resolveAgentFromPoolKey / withAgentPickerUi [P2-005]', () => {
  it('resolveInboundRoutingAgent returns physical agent when user has only one agent', () => {
    const soloDir = join(tmpdir(), 'relay-solo-routing-' + Date.now());
    mkdirSync(soloDir, { recursive: true });
    const path = join(soloDir, 'relay.json');
    writeFileSync(
      path,
      JSON.stringify({
        users: [
          {
            userId: 'solo',
            chatId: '1',
            defaultAgent: 'only',
            agents: [{ id: 'only', name: 'Only', emoji: '⭐', botToken: 'tok', workspace: '/w', sessionId: 'sess' }],
          },
        ],
      }),
    );
    const router = createRouter(path);
    const only = router.getAgents('solo')[0]!;
    expect(resolveInboundRoutingAgent(router, 'solo', only)).toBe(only);
    rmSync(soloDir, { recursive: true, force: true });
  });

  it('resolveInboundRoutingAgent follows active agent when it differs from physical bot', () => {
    const router = setupRouter();
    router.switchAgent('owner', 'ken');
    const physicalMain = router.getAgents('owner').find(a => a.id === 'main')!;
    const routed = resolveInboundRoutingAgent(router, 'owner', physicalMain);
    expect(routed.id).toBe('ken');
  });

  it('resolveInboundRoutingAgent keeps physical Telegram bot when inboundChannel is telegram', () => {
    const router = setupRouter();
    const physicalAki = router.getAgents('owner').find(a => a.id === 'aki')!;
    const active = router.getActiveAgent('owner')!;
    expect(active.id).toBe('main');
    expect(resolveInboundRoutingAgent(router, 'owner', physicalAki, { inboundChannel: 'telegram' })).toBe(physicalAki);
  });

  it('resolveInboundRoutingAgent keeps physical Feishu bot when inboundChannel is feishu (multi-agent)', () => {
    const dir = join(tmpdir(), 'relay-feishu-routing-' + Date.now());
    mkdirSync(dir, { recursive: true });
    const path = join(dir, 'relay.json');
    writeFileSync(
      path,
      JSON.stringify({
        users: [
          {
            userId: 'eric',
            defaultAgent: 'artificer',
            agents: [
              {
                id: 'BestYearly',
                name: 'Samantha',
                emoji: '💬',
                feishu: { appId: 'app1', appSecret: 'x', sessionId: 'f1' },
                feishuOpenId: 'ou_s',
                workspace: '/w/s',
              },
              {
                id: 'artificer',
                name: 'Artificer',
                emoji: '🔧',
                botToken: 'tok',
                feishu: { appId: 'app2', appSecret: 'x', sessionId: 'f2' },
                feishuOpenId: 'ou_a',
                workspace: '/w/a',
                sessionId: 'tg',
              },
            ],
          },
        ],
      }),
    );
    const router = createRouter(path);
    router.switchAgent('eric', 'artificer');
    const sam = router.getAgents('eric').find(a => a.id === 'BestYearly')!;
    const active = router.getActiveAgent('eric')!;
    expect(active.id).toBe('artificer');
    expect(resolveInboundRoutingAgent(router, 'eric', sam).id).toBe('artificer');
    expect(resolveInboundRoutingAgent(router, 'eric', sam, { inboundChannel: 'feishu' })).toBe(sam);
    rmSync(dir, { recursive: true, force: true });
  });

  it('resolveAgentFromPoolKey maps poolKey to AgentConfig', () => {
    const router = setupRouter();
    const ken = resolveAgentFromPoolKey(router, 'owner', 'owner:ken');
    expect(ken?.id).toBe('ken');
    expect(resolveAgentFromPoolKey(router, 'owner', 'other:owner:ken')).toBeUndefined();
    expect(resolveAgentFromPoolKey(router, 'owner', 'owner:not-an-agent')).toBeUndefined();
  });

  it('resolves configured separator IDs without truncating or guessing a channel suffix', () => {
    const config = structuredClone(sampleConfig);
    config.users[0]!.agents.push({ ...config.users[0]!.agents[0]!, id: 'a:b' });
    const router = createRouter(writeTmpConfig('separator-agent.json', config));
    expect(resolveAgentFromPoolKey(router, 'owner', 'owner:a:b')?.id).toBe('a:b');
    expect(resolveAgentFromPoolKey(router, 'owner', 'owner:a:b:feishu')?.id).toBe('a:b');
    expect(resolveAgentFromPoolKey(router, 'owner', 'owner:ken:unknown')).toBeUndefined();
    config.users[0]!.agents.push({ ...config.users[0]!.agents[0]!, id: 'ken:feishu' });
    const ambiguous = createRouter(writeTmpConfig('ambiguous-agent.json', config));
    expect(resolveAgentFromPoolKey(ambiguous, 'owner', 'owner:ken:feishu')).toBeUndefined();
  });

  it('withAgentPickerUi adds switch keyboard and Feishu buttons when multiple agents', () => {
    const router = setupRouter();
    const main = router.getAgents('owner').find(a => a.id === 'main')!;
    const base = { text: 'Hello' };
    const merged = withAgentPickerUi(base, 'owner', router, main);
    expect(merged.inlineKeyboard?.length).toBe(3);
    expect(merged.inlineKeyboard?.some(row => row[0]?.callbackData === `${RELAY_SWITCH_CB_PREFIX}ken`)).toBe(true);
    const btn = merged.feishuCardElements?.find(
      e => e['tag'] === 'button' && (e['behaviors'] as { value?: { relay_switch?: string } }[])?.[0]?.value?.relay_switch === 'ken',
    );
    expect(btn).toBeTruthy();
  });

  it('withAgentPickerUi leaves single-agent payloads unchanged', () => {
    const soloDir = join(tmpdir(), 'relay-solo-picker-' + Date.now());
    mkdirSync(soloDir, { recursive: true });
    const path = join(soloDir, 'relay.json');
    writeFileSync(
      path,
      JSON.stringify({
        users: [
          {
            userId: 'solo',
            chatId: '1',
            defaultAgent: 'only',
            agents: [{ id: 'only', name: 'Only', emoji: '⭐', botToken: 'tok', workspace: '/w', sessionId: 'sess' }],
          },
        ],
      }),
    );
    const router = createRouter(path);
    const only = router.getAgents('solo')[0]!;
    const base = { text: 'Hi' };
    expect(withAgentPickerUi(base, 'solo', router, only)).toEqual(base);
    rmSync(soloDir, { recursive: true, force: true });
  });
});

describe('handleStart (multi-bot mode)', () => {
  it('returns welcome with agent name, multi-agent hints, and switch keyboard when several agents', () => {
    const router = setupRouter();
    const pool = createMockPool();
    const result = handleStart('owner', 'main', { router, runtime: pool });

    expect(result.text).toContain('\u{1F3B2} Duke');
    expect(result.text).toContain('已就绪');
    expect(result.text).toContain('/status');
    expect(result.text).toContain('/runtime');
    expect(result.text).toContain('/reset');
    expect(result.text).toContain('/help');
    expect(result.text).toContain('/switch');
    expect(result.text).toContain('/list');
    expect(result.inlineKeyboard?.length).toBeGreaterThan(0);
  });

  it('single-agent user gets no switch keyboard', () => {
    const soloDir = join(tmpdir(), 'relay-solo-start-' + Date.now());
    mkdirSync(soloDir, { recursive: true });
    const path = join(soloDir, 'relay.json');
    writeFileSync(
      path,
      JSON.stringify({
        users: [
          {
            userId: 'solo',
            chatId: '1',
            defaultAgent: 'only',
            agents: [
              { id: 'only', name: 'Only', emoji: '⭐', botToken: 'tok', workspace: '/w', sessionId: 'sess' },
            ],
          },
        ],
      }),
    );
    const router = createRouter(path);
    const pool = createMockPool();
    const result = handleStart('solo', 'only', { router, runtime: pool });
    expect(result.inlineKeyboard).toBeUndefined();
    expect(result.text).not.toContain('/switch');
    rmSync(soloDir, { recursive: true, force: true });
  });

  it('Feishu /start welcome uses physical agent when inboundChannel is feishu', () => {
    const dir = join(tmpdir(), 'relay-feishu-start-' + Date.now());
    mkdirSync(dir, { recursive: true });
    const path = join(dir, 'relay.json');
    writeFileSync(
      path,
      JSON.stringify({
        users: [
          {
            userId: 'eric',
            defaultAgent: 'artificer',
            agents: [
              {
                id: 'BestYearly',
                name: 'Samantha',
                emoji: '💬',
                feishu: { appId: 'app1', appSecret: 'x', sessionId: 'f1' },
                feishuOpenId: 'ou_s',
                workspace: '/w/s',
              },
              {
                id: 'artificer',
                name: 'Artificer',
                emoji: '🔧',
                botToken: 'tok',
                feishu: { appId: 'app2', appSecret: 'x', sessionId: 'f2' },
                feishuOpenId: 'ou_a',
                workspace: '/w/a',
                sessionId: 'tg',
              },
            ],
          },
        ],
      }),
    );
    const router = createRouter(path);
    router.switchAgent('eric', 'artificer');
    const pool = createMockPool();
    const feishuStart = handleStart('eric', 'BestYearly', { router, runtime: pool, inboundChannel: 'feishu' });
    expect(feishuStart.text).toContain('Samantha');
    const tgStyle = handleStart('eric', 'BestYearly', { router, runtime: pool });
    expect(tgStyle.text).toContain('Artificer');
    rmSync(dir, { recursive: true, force: true });
  });

  it('returns error for unknown agent', () => {
    const router = setupRouter();
    const pool = createMockPool();
    const result = handleStart('owner', 'nonexistent', { router, runtime: pool });

    expect(result.text).toContain('无可用');
  });
});

describe('handleList [P0-003 Unit Test-014]', () => {
  it('shows agent list with process states and active marker', () => {
    const router = setupRouter();
    const pool = createMockPool({
      'owner:main': { tenantId: 'owner:main', state: 'idle', pid: 12345, lastActivity: Date.now(), spawnedAt: Date.now(), restartCount: 0 },
    });
    const result = handleList('owner', { router, runtime: pool });

    // Active agent should have ▶ marker
    expect(result.text).toContain('\u25B6');
    expect(result.text).toContain('\u{1F3B2} Duke');
    expect(result.text).toContain('idle');
    // Non-running agents show 未启动
    expect(result.text).toContain('\u672A\u542F\u52A8'); // "未启动"
  });
});

describe('handleSwitch [P0-003 Unit Test-015/016/017]', () => {
  it('switches to valid agent and returns confirmation', () => {
    const router = setupRouter();
    const pool = createMockPool();
    const result = handleSwitch('owner', 'ken', { router, runtime: pool });

    expect(result.text).toContain('\u{1F527} Ken');
    expect(result.text).toContain('\u5207\u6362'); // "切换"
  });

  it.each(['nobody', 'a:b'])('returns error for unknown agent %s', agentId => {
    const router = setupRouter();
    const pool = createMockPool();
    const result = handleSwitch('owner', agentId, { router, runtime: pool });

    expect(result.text).toContain(agentId);
    expect(result.text).toContain('/list');
  });

  it('returns usage hint when agentId is empty', () => {
    const router = setupRouter();
    const pool = createMockPool();
    const result = handleSwitch('owner', '', { router, runtime: pool });

    expect(result.text).toContain('/switch');
    expect(result.text).toContain('/list');
  });
});

describe('handleStatus [P0-003 Unit Test-018/019]', () => {
  it('shows active agent details when process is running', () => {
    const router = setupRouter();
    const now = Date.now();
    const pool = createMockPool({
      'owner:main': { tenantId: 'owner:main', state: 'idle', pid: 12345, lastActivity: now - 120000, spawnedAt: now - 300000, restartCount: 0 },
    });
    const result = handleStatus('owner', 'main', { router, runtime: pool });

    expect(result.text).toContain('\u{1F3B2} Duke');
    expect(result.text).toContain('idle');
    expect(result.text).toContain('12345');
  });

  it('shows "未启动" when no process exists', () => {
    const router = setupRouter();
    const pool = createMockPool();
    const result = handleStatus('owner', 'main', { router, runtime: pool });

    expect(result.text).toContain('\u{1F3B2} Duke');
    expect(result.text).toContain('\u672A\u542F\u52A8'); // "未启动"
  });

  it('shows "待恢复" when process is absent but session is persisted', () => {
    const router = setupRouter();
    const pool = createMockPool();
    const stateManager = {
      getSession: (poolKey: string) => poolKey === 'owner:main'
        ? {
          poolKey,
          agentId: 'main',
          sessionId: 'uuid-duke',
          turnCount: 0,
          maxTurns: 20,
          lastActivity: Date.now(),
          status: 'active' as const,
        }
        : undefined,
    };
    const result = handleStatus('owner', 'main', { router, runtime: pool, stateManager: stateManager as never });

    expect(result.text).toContain('待恢复');
    expect(result.text).toContain('会话仍保留');
  });
});

describe('handleHelp', () => {
  it('returns help text with available commands', () => {
    const result = handleHelp();

    expect(result.text).toContain('/start');
    expect(result.text).toContain('/status');
    expect(result.text).toContain('/session');
    expect(result.text).toContain('/runtime');
    expect(result.text).toContain('/new');
    expect(result.text).toContain('/reset');
    expect(result.text).toContain('/stop');
    expect(result.text).toContain('/kill');
    expect(result.text).toContain('/restart');
    expect(result.text).toContain('/usage');
    expect(result.text).toContain('/help');
    expect(result.text).toContain('/list');
    expect(result.text).toContain('/switch');
  });
});

describe('handleRuntime', () => {
  it('returns option card with CC/Codex buttons when no arg given', async () => {
    const router = setupRouter();
    const pool = createMockPool();

    const result = await handleRuntime('owner', 'main', undefined, { router, runtime: pool });

    expect(result.feishuCardElements).toBeDefined();
    expect(result.feishuCardElements!.length).toBeGreaterThan(0);
    expect(result.text).toContain('Runtime 选择');
  });

  it('rejects invalid runtime values without changing relay.json', async () => {
    const router = setupRouter();
    const pool = createMockPool();

    const result = await handleRuntime('owner', 'main', 'python', { router, runtime: pool });

    expect(result.text).toContain('不支持的 runtime');
    expect(readConfig().users[0].agents[0].runtime).toBeUndefined();
  });

  it('persists a valid runtime and clears the active session', async () => {
    const router = setupRouter();
    const pool = createMockPool({
      'owner:main': { tenantId: 'owner:main', state: 'idle', pid: 12345, lastActivity: Date.now(), spawnedAt: Date.now(), restartCount: 0 },
    });
    let killedKey = '';
    pool.kill = async (tenantId: string) => { killedKey = tenantId; };
    const stateManager = createStateManager({
      'owner:main': {
        poolKey: 'owner:main',
        agentId: 'main',
        runtime: 'cc',
        sessionId: 'uuid-duke',
        codexThreadId: 'old-thread',
        turnCount: 1,
        maxTurns: 20,
        lastActivity: Date.now(),
        status: 'active',
      },
    });

    const result = await handleRuntime('owner', 'main', 'codex', { router, runtime: pool, stateManager });

    expect(killedKey).toBe('owner:main');
    expect(router.getAgents('owner').find((agent) => agent.id === 'main')?.runtime).toBe('codex');
    expect(readConfig().users[0].agents[0].runtime).toBe('codex');
    expect(readConfig().users[0].agents[0].conversationRuntimePreference).toEqual(['codex']);
    expect(stateManager.getSession('owner:main')).toBeUndefined();
    expect(stateManager.flushed).toBe(true);
    expect(result.text).toContain('runtime 已切换为 codex');
    expect(result.text).toContain('下一条消息会使用新的 runtime');
  });

  it('blocks runtime replacement while the session carries active WI authority', async () => {
    const router = setupRouter();
    const pool = createMockPool({
      'owner:main': { tenantId: 'owner:main', state: 'idle', pid: 12345, lastActivity: Date.now(), spawnedAt: Date.now(), restartCount: 0 },
    });
    const kill = vi.fn(async () => undefined);
    pool.kill = kill;
    const stateManager = createStateManager({
      'owner:main': {
        poolKey: 'owner:main', agentId: 'main', runtime: 'pi', sessionId: 'bound-native',
        turnCount: 1, maxTurns: 20, lastActivity: Date.now(), status: 'active',
        activeWorkItemRuntimeBinding: {
          schema: 'pulse_active_work_item_runtime_binding/v1', workItemId: 'wi_20260820_568',
          sessionId: 'bound-native', sessionRef: `session_v1_${'a'.repeat(64)}`, runtime: 'pi',
          usageBinding: {
            schema: 'pulse_runtime_usage_binding/v1', work_item_id: 'wi_20260820_568', agent_id: 'pulse',
            phase: 'work_item', epoch: 3, runtime: 'pi', procurement_source: 'unknown',
            issued_at: '2026-09-01T00:00:00.000Z', binding_sha256: 'b'.repeat(64),
          },
        },
      },
    });

    const result = await handleRuntime('owner', 'main', 'codex', { router, runtime: pool, stateManager });

    expect(result.text).toContain('拒绝切换 runtime');
    expect(result.text).toContain('/wi rollover wi_20260820_568');
    expect(kill).not.toHaveBeenCalled();
    expect(stateManager.getSession('owner:main')?.activeWorkItemRuntimeBinding).toBeDefined();
  });

  it('overrides stale conversationRuntimePreference so the selected runtime wins', async () => {
    const router = setupRouter();
    const pool = createMockPool();
    const cfg = readConfig();
    cfg.users[0].agents[2].conversationRuntimePreference = ['codex', 'cc'];
    writeFileSync(join(testDir, 'relay.json'), JSON.stringify(cfg));
    const refreshedRouter = createRouter(join(testDir, 'relay.json'));

    await handleRuntime('owner', 'aki', 'cc', { router: refreshedRouter, runtime: pool });

    expect(readConfig().users[0].agents[2].runtime).toBe('cc');
    expect(readConfig().users[0].agents[2].conversationRuntimePreference).toEqual(['cc']);
  });

  it('switches an approved Pi canary through the structured route without losing its gate fields', async () => {
    const cfg = JSON.parse(JSON.stringify(sampleConfig));
    cfg.users[0].agents[0].runtime = 'pi';
    cfg.users[0].agents[0].conversationRuntimePreference = ['pi'];
    cfg.users[0].agents[0].conversationRuntime = {
      runtime: 'pi',
      model: { provider: 'openrouter', model: 'anthropic/claude-sonnet-4.5', reasoning: 'high' },
      isolationProfile: 'internal-canary',
    };
    writeFileSync(join(testDir, 'relay.json'), JSON.stringify(cfg));
    const router = createRouter(join(testDir, 'relay.json'));

    await handleRuntime('owner', 'main', 'codex', { router, runtime: createMockPool() });
    expect(readConfig().users[0].agents[0].conversationRuntime).toEqual({
      runtime: 'codex',
      model: { provider: 'openrouter', model: 'anthropic/claude-sonnet-4.5', reasoning: 'high' },
      isolationProfile: 'internal-canary',
    });

    await handleRuntime('owner', 'main', 'pi', { router, runtime: createMockPool() });
    expect(readConfig().users[0].agents[0].runtime).toBe('pi');
    expect(readConfig().users[0].agents[0].conversationRuntime.runtime).toBe('pi');
    expect(readConfig().users[0].agents[0].conversationRuntime.isolationProfile).toBe('internal-canary');
  });

  it('materializes the reviewed node Pi capability for an agent without a per-agent gate', async () => {
    const cfg = JSON.parse(JSON.stringify(sampleConfig));
    cfg.conversationRuntimeDefaults = {
      pi: {
        model: { provider: 'openai-codex', model: 'gpt-5.6-sol', reasoning: 'medium' },
        isolationProfile: 'resident-host-native',
      },
    };
    writeFileSync(join(testDir, 'relay.json'), JSON.stringify(cfg));
    const router = createRouter(join(testDir, 'relay.json'));
    const result = await handleRuntime('owner', 'main', 'pi', { router, runtime: createMockPool() });

    expect(result.text).toContain('runtime 已切换为 pi');
    expect(readConfig().users[0].agents[0]).toMatchObject({
      runtime: 'pi',
      conversationRuntimePreference: ['pi'],
      conversationRuntime: {
        runtime: 'pi',
        model: { provider: 'openai-codex', model: 'gpt-5.6-sol', reasoning: 'medium' },
        isolationProfile: 'resident-host-native',
      },
    });
  });

  it('fails closed when the node itself has no complete Pi capability', async () => {
    const router = setupRouter();
    const result = await handleRuntime('owner', 'main', 'pi', { router, runtime: createMockPool() });

    expect(result.text).toContain('节点尚未配置完整');
    expect(readConfig().users[0].agents[0].runtime).toBeUndefined();
  });
});

describe('handleYolo', () => {
  it('shows and toggles Codex YOLO mode without changing relay.json', async () => {
    const router = setupRouter();
    const yoloModes = new Map<string, boolean>();
    const pool = Object.assign(createMockPool(), {
      setYoloMode: (tenantId: string, enabled: boolean) => {
        yoloModes.set(tenantId, enabled);
      },
      getYoloMode: (tenantId: string) => yoloModes.get(tenantId) ?? false,
      getYoloOverrideMode: (tenantId: string) => yoloModes.get(tenantId),
    });

    const on = await handleYolo('owner', 'aki', 'on', { router, runtime: pool });
    expect(on.text).toContain('YOLO 已开启');
    expect(on.text).toContain('--dangerously-bypass-approvals-and-sandbox');
    expect(yoloModes.get('owner:aki')).toBe(true);
    expect(readConfig().users[0].agents[2].runtime).toBe('codex');

    const status = await handleYolo('owner', 'aki', 'status', { router, runtime: pool });
    expect(status.text).toContain('Codex YOLO: on');
    expect(status.text).toContain('default=off');
    expect(status.text).toContain('sessionOverride=on');

    const off = await handleYolo('owner', 'aki', 'off', { router, runtime: pool });
    expect(off.text).toContain('YOLO 已关闭');
    expect(yoloModes.get('owner:aki')).toBe(false);
  });

  it('returns option card with on/off/status buttons when no arg given', async () => {
    const router = setupRouter();
    const pool = Object.assign(createMockPool(), {
      setYoloMode: (_t: string, _e: boolean) => {},
      getYoloMode: (_t: string) => false,
    });

    const result = await handleYolo('owner', 'aki', undefined, { router, runtime: pool });

    expect(result.feishuCardElements).toBeDefined();
    expect(result.feishuCardElements!.length).toBeGreaterThan(0);
    expect(result.text).toContain('YOLO');
  });

  it('shows configured YOLO default separately from the session override', async () => {
    const cfg = JSON.parse(JSON.stringify(sampleConfig));
    cfg.users[0].agents[2].codexYoloDefault = true;
    writeFileSync(join(testDir, 'relay.json'), JSON.stringify(cfg));
    const router = createRouter(join(testDir, 'relay.json'));
    const defaults = new Map<string, boolean>();
    const overrides = new Map<string, boolean>();
    const pool = Object.assign(createMockPool(), {
      setYoloDefaultMode: (tenantId: string, enabled: boolean) => {
        if (enabled) defaults.set(tenantId, true);
        else defaults.delete(tenantId);
      },
      setYoloMode: (tenantId: string, enabled: boolean) => {
        overrides.set(tenantId, enabled);
      },
      getYoloOverrideMode: (tenantId: string) => overrides.get(tenantId),
      getYoloMode: (tenantId: string) => overrides.has(tenantId)
        ? overrides.get(tenantId) ?? false
        : defaults.get(tenantId) ?? false,
    });

    const status = await handleYolo('owner', 'aki', 'status', { router, runtime: pool });
    expect(status.text).toContain('Codex YOLO: on');
    expect(status.text).toContain('default=on');
    expect(status.text).toContain('sessionOverride=none');

    const off = await handleYolo('owner', 'aki', 'off', { router, runtime: pool });
    expect(off.text).toContain('default=on；sessionOverride=off');
    expect(off.text).toContain('恢复默认 on');
    expect(pool.getYoloMode('owner:aki')).toBe(false);
  });

  it('keeps the existing Codex thread when toggling YOLO permissions', async () => {
    const router = setupRouter();
    const killed: string[] = [];
    const yoloModes = new Map<string, boolean>();
    const pool = Object.assign(createMockPool({
      'owner:aki': {
        tenantId: 'owner:aki',
        state: 'idle',
        pid: 123,
        lastActivity: Date.now(),
        spawnedAt: Date.now(),
        restartCount: 0,
      },
    }), {
      kill: async (tenantId: string) => { killed.push(tenantId); },
      setYoloMode: (tenantId: string, enabled: boolean) => {
        if (enabled) yoloModes.set(tenantId, true);
        else yoloModes.delete(tenantId);
      },
      getYoloMode: (tenantId: string) => yoloModes.get(tenantId) ?? false,
    });
    const stateManager = createStateManager({
      'owner:aki': {
        poolKey: 'owner:aki',
        agentId: 'aki',
        runtime: 'codex',
        sessionId: 'session-aki',
        codexThreadId: 'old-thread',
        lastActivity: Date.now(),
        status: 'active',
      },
    });

    const result = await handleYolo('owner', 'aki', 'on', { router, runtime: pool, stateManager });

    expect(result.text).toContain('不会清理或新建 thread');
    expect(yoloModes.get('owner:aki')).toBe(true);
    expect(killed).toEqual([]);
    expect(stateManager.getSession('owner:aki')?.codexThreadId).toBe('old-thread');
    expect(stateManager.flushed).toBe(false);
  });

  it('rejects YOLO mode when the current agent is not using Codex', async () => {
    const router = setupRouter();
    const pool = Object.assign(createMockPool(), {
      setYoloMode: () => {},
      getYoloMode: () => false,
    });

    const result = await handleYolo('owner', 'main', 'on', { router, runtime: pool });

    expect(result.text).toContain('runtime=cc');
    expect(result.text).toContain('/runtime codex');
  });

  it('clears the session YOLO override when starting a new session', async () => {
    const router = setupRouter();
    let yoloOverride: boolean | undefined = false;
    const pool = Object.assign(createMockPool(), {
      setYoloMode: (_tenantId: string, enabled: boolean) => {
        yoloOverride = enabled;
      },
      clearYoloMode: () => {
        yoloOverride = undefined;
      },
      getYoloMode: () => yoloOverride ?? true,
    });

    await handleNew('owner', 'aki', { router, runtime: pool });

    expect(yoloOverride).toBeUndefined();
    expect(pool.getYoloMode('owner:aki')).toBe(true);
  });
});

describe('handleNew', () => {
  it('kills process and resets session ID', async () => {
    const router = setupRouter();
    let killedKey = '';
    const pool = createMockPool({
      'owner:main': { tenantId: 'owner:main', state: 'idle', pid: 12345, lastActivity: Date.now(), spawnedAt: Date.now(), restartCount: 0 },
    });
    pool.kill = async (tenantId: string) => { killedKey = tenantId; };

    const oldSession = router.getActiveAgent('owner')!.sessionId;
    const result = await handleNew('owner', 'main', { router, runtime: pool });

    expect(killedKey).toBe('owner:main');
    expect(result.text).toContain('\u{1F3B2} Duke');
    expect(result.text).toContain('\u65B0\u5BF9\u8BDD'); // "新对话"
    // Session ID should have changed
    const newSession = router.getActiveAgent('owner')!.sessionId;
    expect(newSession).not.toBe(oldSession);
  });

  it('blocks /new but /reset releases a bound session and preserves the old binding for audit', async () => {
    const router = setupRouter();
    const oldSession = router.getActiveAgent('owner')!.sessionId;
    const pool = createMockPool({
      'owner:main': { tenantId: 'owner:main', state: 'idle', pid: 12345, lastActivity: Date.now(), spawnedAt: Date.now(), restartCount: 0 },
    });
    const kill = vi.fn(async () => undefined);
    pool.kill = kill;
    const stateManager = createStateManager({
      'owner:main': {
        poolKey: 'owner:main', agentId: 'main', runtime: 'pi', sessionId: 'bound-native',
        turnCount: 1, maxTurns: 20, lastActivity: Date.now(), status: 'active',
        activeWorkItemRuntimeBinding: {
          schema: 'pulse_active_work_item_runtime_binding/v1', workItemId: 'wi_20260820_568',
          sessionId: 'bound-native', sessionRef: `session_v1_${'a'.repeat(64)}`, runtime: 'pi',
          usageBinding: {
            schema: 'pulse_runtime_usage_binding/v1', work_item_id: 'wi_20260820_568', agent_id: 'pulse',
            phase: 'work_item', epoch: 3, runtime: 'pi', procurement_source: 'unknown',
            issued_at: '2026-09-01T00:00:00.000Z', binding_sha256: 'b'.repeat(64),
          },
        },
      },
    });

    const fresh = await handleNew('owner', 'main', { router, runtime: pool, stateManager });
    const reset = await handleReset('owner', 'main', { router, runtime: pool, stateManager });

    expect(fresh.text).toContain('/wi rollover wi_20260820_568');
    expect(reset.text).toContain('已强制重置 session');
    expect(reset.text).toContain('不改变 Harness 中的 WI 权限或状态');
    expect(kill).toHaveBeenCalledOnce();
    expect(router.getActiveAgent('owner')!.sessionId).not.toBe(oldSession);
    const saved = stateManager.getSession('owner:main');
    expect(saved?.activeWorkItemRuntimeBinding).toBeUndefined();
    expect(saved?.forcedSessionResets?.at(-1)?.binding?.workItemId).toBe('wi_20260820_568');
    expect(saved?.forcedSessionResets?.at(-1)?.sessionId).toBe('bound-native');
  });

  it('works without active process', async () => {
    const router = setupRouter();
    const pool = createMockPool();
    const result = await handleNew('owner', 'main', { router, runtime: pool });
    expect(result.text).toContain('\u65B0\u5BF9\u8BDD');
  });

  it('persists configured runtime when creating a new session', async () => {
    const router = setupRouter();
    const pool = createMockPool();
    const updates: unknown[] = [];
    const stateManager = {
      updateSession: (_poolKey: string, update: unknown) => { updates.push(update); },
      flush: () => {},
    };

    await handleNew('owner', 'aki', { router, runtime: pool, stateManager: stateManager as never });

    expect(updates[0]).toMatchObject({ agentId: 'aki', runtime: 'codex', codexThreadId: undefined });
  });

  it('persists the structured conversation runtime binding for a new session', async () => {
    const router = setupRouter();
    const agent = router.getAgents('owner').find((candidate) => candidate.id === 'main')!;
    agent.runtime = 'codex';
    agent.conversationRuntime = {
      runtime: 'pi',
      model: { provider: 'openrouter', model: 'deepseek/deepseek-v4-flash-0731', reasoning: 'high' },
      isolationProfile: 'internal-canary',
    };
    const updates: unknown[] = [];
    const stateManager = {
      updateSession: (_poolKey: string, update: unknown) => { updates.push(update); },
      flush: () => {},
    };

    await handleNew('owner', 'main', {
      router,
      runtime: createMockPool(),
      stateManager: stateManager as never,
    });

    expect(updates[0]).toMatchObject({ agentId: 'main', runtime: 'pi' });
  });
});

describe('new conversation scope', () => {
  it.each([handleNew,handleReset])('clears restrictions inherited from the previous conversation', async (command) => {
    const router=setupRouter();
    const stateManager=createStateManager({'owner:main':{
      poolKey:'owner:main',agentId:'main',runtime:'cc',sessionId:'old',
      lastActivity:Date.now(),status:'active',sclBusinessDispatchAllowed:false,
    }});
    await command('owner','main',{router,runtime:createMockPool(),stateManager});
    expect(stateManager.getSession('owner:main')?.sclBusinessDispatchAllowed).not.toBe(false);
  });
});

describe('terminal conversation replacement', () => {
  it.each([handleNew,handleReset])('archives a stopped different-target entry without treating it as the current WI', async command => {
    const router=setupRouter(),runtime=createMockPool();
    const entry={schema:'pulse_pending_work_item_entry/v2',workItemId:'wi_20990101_568',sessionId:'other',sessionRef:'session_v1_'+'a'.repeat(64),runtime:'pi',state:'planned',operationRef:'operation_v1_'+'b'.repeat(64)};
    const stateManager=createStateManager({'owner:main':{
      poolKey:'owner:main',agentId:'main',runtime:'pi',sessionId:'old',lastActivity:Date.now(),status:'active',
      sclBusinessDispatchAllowed:false,pendingWorkItemEntry:entry,
      terminalWorkItemRuntimeBinding:{binding:{workItemId:'wi_20990101_309',sessionId:'old',runtime:'pi'},lifecycleStatus:'completed',localRevision:1,stateDigest:'d'.repeat(64)},
    } as never});
    const result=await command('owner','main',{router,runtime,stateManager});
    expect(result.text).toContain('已开启新对话');
    const saved=stateManager.getSession('owner:main');
    expect(saved?.pendingWorkItemEntry).toBeUndefined();
    expect(saved?.stoppedWorkItemEntries?.[0]?.entry).toEqual(entry);
    expect(saved?.sessionId).not.toBe('old');
    expect(saved?.sclBusinessDispatchAllowed).not.toBe(false);
  });
  it.each(['prepared','bound','activating','dispatching','recovery_required'])('archives a partially adopted entry on forced reset: %s', async state => {
    const router=setupRouter(),runtime=createMockPool();
    const stateManager=createStateManager({'owner:main':{
      poolKey:'owner:main',agentId:'main',runtime:'pi',sessionId:'old',lastActivity:Date.now(),status:'active',
      sclBusinessDispatchAllowed:false,pendingWorkItemEntry:{workItemId:'wi_20990101_568',sessionId:'other',state},
      terminalWorkItemRuntimeBinding:{binding:{workItemId:'wi_20990101_309',sessionId:'old',runtime:'pi'},lifecycleStatus:'completed',localRevision:1,stateDigest:'d'.repeat(64)},
    } as never});
    const result=await handleReset('owner','main',{router,runtime,stateManager});
    expect(result.text).toContain('已强制重置 session');
    expect(stateManager.getSession('owner:main')?.sessionId).not.toBe('old');
    expect(stateManager.getSession('owner:main')?.pendingWorkItemEntry).toBeUndefined();
    expect(stateManager.getSession('owner:main')?.stoppedWorkItemEntries?.at(-1)?.entry.state).toBe(state);
  });
  it('keeps a pending rollover and binds the session that the reset made as its stand-in', async () => {
    const router=setupRouter(),runtime=createMockPool();
    const plan={ref:'f'.repeat(64),sourceSessionId:'old',runtime:'cc' as const};
    const stateManager=createStateManager({'owner:main':{
      poolKey:'owner:main',agentId:'main',runtime:'cc',sessionId:'old',lastActivity:Date.now(),status:'active',
      pendingNativeRollover:plan,
    } as never});
    await handleReset('owner','main',{router,runtime,stateManager});
    const saved=stateManager.getSession('owner:main')!;
    expect(saved.sessionId).not.toBe('old');
    expect(saved.pendingNativeRollover).toEqual({...plan,standInSessionId:saved.sessionId});
  });
});

describe('handleReset', () => {
  it('resets session ID with explicit history discard wording', async () => {
    const router = setupRouter();
    const pool = createMockPool();
    const oldSession = router.getActiveAgent('owner')!.sessionId;

    const result = await handleReset('owner', 'main', { router, runtime: pool });

    expect(result.text).toContain('已强制重置 session');
    expect(result.text).toContain('历史上下文已丢弃');
    expect(router.getActiveAgent('owner')!.sessionId).not.toBe(oldSession);
  });

  it('does not claim release when the session audit cannot be persisted', async () => {
    const router = setupRouter();
    const oldSession = router.getActiveAgent('owner')!.sessionId;
    const stateManager = createStateManager({ 'owner:main': {
      poolKey: 'owner:main', agentId: 'main', runtime: 'pi', sessionId: oldSession,
      turnCount: 1, maxTurns: 20, lastActivity: Date.now(), status: 'active',
    } });
    const manager = { ...stateManager, flushStrict: () => { throw new Error('disk full'); } };

    const result = await handleReset('owner', 'main', { router, runtime: createMockPool(), stateManager: manager as never });

    expect(result.text).toContain('旧 session 未释放');
    expect(router.getActiveAgent('owner')!.sessionId).toBe(oldSession);
  });

  it('restores the old identity when final reset persistence fails', async () => {
    const router = setupRouter();
    const oldSession = router.getActiveAgent('owner')!.sessionId;
    const stateManager = createStateManager({ 'owner:main': {
      poolKey: 'owner:main', agentId: 'main', runtime: 'pi', sessionId: oldSession,
      turnCount: 1, maxTurns: 20, lastActivity: Date.now(), status: 'active',
    } });
    let writes = 0;
    const manager = { ...stateManager, flushStrict: () => {
      if (++writes === 2) throw new Error('disk full');
    } };

    const result = await handleReset('owner', 'main', { router, runtime: createMockPool(), stateManager: manager as never });

    expect(result.text).toContain('旧 session 未释放');
    expect(router.getActiveAgent('owner')!.sessionId).toBe(oldSession);
    expect(manager.getSession('owner:main')?.sessionId).toBe(oldSession);
  });
});

describe('handleStop', () => {
  it('uses the runtime-native abort path without killing the Pi session', async () => {
    const router = setupRouter();
    const pool = createMockPool();
    const abortTurn = vi.fn(async () => undefined);
    pool.abortTurn = abortTurn;
    pool.get = () => ({
      tenantId: 'owner:main',
      runtime: 'pi',
      sessionId: 'pi-session',
      state: 'busy',
      lastActivity: Date.now(),
      spawnedAt: Date.now(),
      restartCount: 0,
      restartWindowStart: Date.now(),
    });

    const result = await handleStop('owner', 'main', { router, runtime: pool });

    expect(abortTurn).toHaveBeenCalledWith('owner:main');
    expect(result.text).toContain('中断');
  });

  it('sends SIGINT when process is busy', async () => {
    const router = setupRouter();
    let signalSent = '';
    const mockProcess = {
      pid: 12345,
      kill: ((sig: string) => { signalSent = sig; return true; }) as never,
    };
    const pool = createMockPool();
    // Override get to return a process with mocked kill
    pool.get = () => ({
      tenantId: 'owner:main',
      process: mockProcess as never,
      sessionId: 'mock-session',
      state: 'busy',
      lastActivity: Date.now(),
      spawnedAt: Date.now(),
      restartCount: 0,
      restartWindowStart: Date.now(),
    });

    const result = await handleStop('owner', 'main', { router, runtime: pool });

    expect(signalSent).toBe('SIGINT');
    expect(result.text).toContain('中断');
  });

  it('returns idle message when process is not busy', async () => {
    const router = setupRouter();
    const pool = createMockPool({
      'owner:main': { tenantId: 'owner:main', state: 'idle', pid: 12345, lastActivity: Date.now(), spawnedAt: Date.now(), restartCount: 0 },
    });

    const result = await handleStop('owner', 'main', { router, runtime: pool });
    expect(result.text).toContain('空闲');
  });

  it('returns not running when no process exists', async () => {
    const router = setupRouter();
    const pool = createMockPool();

    const result = await handleStop('owner', 'main', { router, runtime: pool });
    expect(result.text).toContain('未在运行');
  });
});

describe('handleKill [P1-001 Unit Test-030]', () => {
  it('kills active agent process and returns confirmation', async () => {
    const router = setupRouter();
    let killedKey = '';
    const pool = createMockPool({
      'owner:main': { tenantId: 'owner:main', state: 'idle', pid: 12345, lastActivity: Date.now(), spawnedAt: Date.now(), restartCount: 0 },
    });
    pool.kill = async (tenantId: string) => { killedKey = tenantId; };

    const result = await handleKill('owner', 'main', { router, runtime: pool });

    expect(killedKey).toBe('owner:main');
    expect(result.text).toContain('\u{1F3B2} Duke');
    expect(result.text).toContain('\u7EC8\u6B62'); // "终止"
  });

  it('returns message when no process to kill', async () => {
    const router = setupRouter();
    const pool = createMockPool();

    const result = await handleKill('owner', 'main', { router, runtime: pool });
    expect(result.text).toContain('\u672A\u542F\u52A8'); // "未启动"
  });
});

describe('handleRestart [P1-001 Unit Test-031]', () => {
  it('kills and respawns active agent process', async () => {
    const router = setupRouter();
    let killedKey = '';
    let spawnedConfig: { tenantId: string; resume: boolean } | null = null;
    const pool = createMockPool({
      'owner:main': { tenantId: 'owner:main', state: 'idle', pid: 12345, lastActivity: Date.now(), spawnedAt: Date.now(), restartCount: 0 },
    });
    pool.kill = async (tenantId: string) => { killedKey = tenantId; };
    pool.spawn = async (config) => {
      spawnedConfig = { tenantId: config.tenantId, resume: config.resume ?? false };
      return { tenantId: config.tenantId, process: {} as never, sessionId: config.sessionId, state: 'ready', lastActivity: Date.now(), spawnedAt: Date.now(), restartCount: 0, restartWindowStart: Date.now() };
    };

    const result = await handleRestart('owner', 'main', { router, runtime: pool });

    expect(killedKey).toBe('owner:main');
    expect(spawnedConfig).toBeDefined();
    expect(spawnedConfig!.tenantId).toBe('owner:main');
    expect(spawnedConfig!.resume).toBe(true);
    expect(result.text).toContain('\u91CD\u542F'); // "重启"
  });

  it('passes configured runtime and persisted thread id when restarting', async () => {
    const router = setupRouter();
    let spawnedConfig: { runtime?: string; codexThreadId?: string } | null = null;
    const pool = createMockPool();
    pool.spawn = async (config) => {
      spawnedConfig = { runtime: config.runtime, codexThreadId: config.codexThreadId };
      return { tenantId: config.tenantId, process: {} as never, sessionId: config.sessionId, state: 'ready', lastActivity: Date.now(), spawnedAt: Date.now(), restartCount: 0, restartWindowStart: Date.now() };
    };
    const stateManager = {
      getSession: (poolKey: string) => poolKey === 'owner:aki'
        ? {
          poolKey,
          agentId: 'aki',
          runtime: 'codex' as const,
          sessionId: 'uuid-aki',
          codexThreadId: 'thread-123',
          turnCount: 1,
          maxTurns: 100,
          lastActivity: Date.now(),
          status: 'active' as const,
        }
        : undefined,
    };

    await handleRestart('owner', 'aki', { router, runtime: pool, stateManager: stateManager as never });

    expect(spawnedConfig).toEqual({ runtime: 'codex', codexThreadId: 'thread-123' });
  });

  it('preserves the Pi structured route and exact persisted session binding', async () => {
    const cfg = JSON.parse(JSON.stringify(sampleConfig));
    cfg.users[0].agents[0].runtime = 'pi';
    cfg.users[0].agents[0].conversationRuntime = {
      runtime: 'pi',
      model: { provider: 'openrouter', model: 'anthropic/claude-sonnet-4.5', reasoning: 'high' },
      isolationProfile: 'internal-canary',
    };
    writeFileSync(join(testDir, 'relay.json'), JSON.stringify(cfg));
    const router = createRouter(join(testDir, 'relay.json'));
    let spawnedConfig: Record<string, unknown> | undefined;
    const pool = createMockPool();
    pool.spawn = async (config) => {
      spawnedConfig = config as unknown as Record<string, unknown>;
      return { tenantId: config.tenantId, process: {} as never, sessionId: config.sessionId, state: 'ready', lastActivity: Date.now(), spawnedAt: Date.now(), restartCount: 0, restartWindowStart: Date.now() };
    };
    const usageBinding = {
      schema: 'pulse_runtime_usage_binding/v1' as const,
      work_item_id: 'wi_20260822_730', agent_id: 'pulse', phase: 'work_item', epoch: 12,
      runtime: 'pi' as const, procurement_source: 'chatgpt_subscription' as const,
      issued_at: '2026-08-25T01:00:00Z',
      binding_sha256: '85fcc7240350aaf4929917cd6760bda4e8079a64f2411749766e04e25c0d9f06',
    };
    const stateManager = createStateManager({
      'owner:main': {
        poolKey: 'owner:main', agentId: 'main', runtime: 'pi', sessionId: 'pi-native-session',
        turnCount: 2, maxTurns: 20, lastActivity: Date.now(), status: 'active',
        activeWorkItemRuntimeBinding: {
          schema: 'pulse_active_work_item_runtime_binding/v1',
          workItemId: usageBinding.work_item_id,
          sessionId: 'pi-native-session', sessionRef: runtimeAuthorityRef('pi-native-session'),
          runtime: 'pi', usageBinding,
        },
      },
    });

    await handleRestart('owner', 'main', { router, runtime: pool, stateManager });

    expect(spawnedConfig).toMatchObject({
      runtime: 'pi',
      sessionId: 'pi-native-session',
      runtimeSessionBinding: { runtime: 'pi', sessionId: 'pi-native-session' },
      conversationRuntime: cfg.users[0].agents[0].conversationRuntime,
      resume: true,
    });
    expect(spawnedConfig?.computeCostBinding).toBeUndefined();
  });
});

describe('restart admission before stopping a live conversation', () => {
  it('does not kill a live process to discover that its retained WI identity is invalid', async () => {
    const router = setupRouter();
    const pool = createMockPool();
    let killed = false;
    let spawned = false;
    pool.get = () => ({ sessionId: 'retained-session', state: 'idle' } as never);
    pool.kill = async () => { killed = true; };
    pool.spawn = async () => { spawned = true; return {} as never; };
    const usageBinding = buildRuntimeUsageBinding({ workItemId: 'wi_20990101_271', agentId: 'pulse',
      phase: 'work_item', epoch: 1, runtime: 'pi', procurementSource: 'chatgpt_subscription',
      issuedAt: '2026-09-01T00:00:00Z' });
    const stateManager = createStateManager({
      'owner:main': { sessionId: 'retained-session', runtime: 'pi', activeWorkItemRuntimeBinding: {
        schema: 'pulse_active_work_item_runtime_binding/v1', workItemId: usageBinding.work_item_id,
        sessionId: 'retained-session', sessionRef: runtimeAuthorityRef('wrong-session'), runtime: 'pi', usageBinding,
      } } as never,
    });
    await expect(handleRestart('owner', 'main', { router, runtime: pool, stateManager }))
      .rejects.toThrow('session reference');
    expect(killed).toBe(false);
    expect(spawned).toBe(false);
  });

});

describe('handleUsageSeal [WI730 SCL UAT repair]', () => {
  function piFixture(bound = true) {
    const cfg = JSON.parse(JSON.stringify(sampleConfig));
    cfg.users[0].agents[0].runtime = 'pi';
    writeFileSync(join(testDir, 'relay.json'), JSON.stringify(cfg));
    const router = createRouter(join(testDir, 'relay.json'));
    const pool = createMockPool({
      'owner:main': { tenantId: 'owner:main', state: 'idle', pid: 42, lastActivity: Date.now(), spawnedAt: Date.now(), restartCount: 0 },
    });
    const sealed: string[] = [];
    (pool as typeof pool & { sealRuntimeUsage: (tenantId: string) => Promise<unknown> }).sealRuntimeUsage = async (tenantId) => {
      sealed.push(tenantId);
      return {
        schema: 'pulse_runtime_usage_seal_readback/v1', status: 'sealed',
        work_item_id: 'wi_20260822_730', epoch: 12,
        artifact_ref: 'wi_20260822_730-sealed.json', sealed_digest: 'd'.repeat(64),
      };
    };
    const usageBinding = {
      schema: 'pulse_runtime_usage_binding/v1' as const,
      work_item_id: 'wi_20260822_730', agent_id: 'pulse', phase: 'work_item', epoch: 12,
      runtime: 'pi' as const, procurement_source: 'chatgpt_subscription' as const,
      issued_at: '2026-08-25T01:00:00Z',
      binding_sha256: '85fcc7240350aaf4929917cd6760bda4e8079a64f2411749766e04e25c0d9f06',
    };
    const stateManager = createStateManager({
      'owner:main': {
        poolKey: 'owner:main', agentId: 'main', runtime: 'pi', sessionId: 'mock-session',
        turnCount: 1, maxTurns: 20, lastActivity: Date.now(), status: 'active',
        ...(bound ? { activeWorkItemRuntimeBinding: {
          schema: 'pulse_active_work_item_runtime_binding/v1' as const,
          workItemId: usageBinding.work_item_id,
          sessionId: 'mock-session', sessionRef: runtimeAuthorityRef('mock-session'),
          runtime: 'pi' as const, usageBinding,
        } } : {}),
      },
    });
    return { router, pool, stateManager, sealed };
  }

  it('forwards the Telegram-safe command only to an idle, exactly bound Pi', async () => {
    const f = piFixture();
    const result = await handleUsageSeal('owner', 'main', {
      router: f.router, runtime: f.pool, stateManager: f.stateManager,
    });
    expect(result.text).toContain('usage seal: sealed');
    expect(result.text).toContain(`sealed_digest=${'d'.repeat(64)}`);
    expect(f.sealed).toEqual(['owner:main']);
  });

  it('fails closed when the resumed Pi has no correlated binding', async () => {
    const f = piFixture(false);
    const result = await handleUsageSeal('owner', 'main', {
      router: f.router, runtime: f.pool, stateManager: f.stateManager,
    });
    expect(result.text).toContain('拒绝封存');
    expect(f.sealed).toEqual([]);
  });
});

describe('handleSession', () => {
  it.each(['telegram', 'feishu'] as const)('%s diagnostics and takeover use that channel history', async inboundChannel => {
    const cfg = structuredClone(sampleConfig);
    cfg.users[0].agents[0].feishu = { appId: 'fixture', appSecret: 'fixture', sessionId: 'feishu-history' };
    writeFileSync(join(testDir, 'relay.json'), JSON.stringify(cfg));
    const router = createRouter(join(testDir, "relay.json")), runtime = createMockPool();
    const expected = inboundChannel === 'telegram' ? 'uuid-duke' : 'feishu-history';
    const deps = { router, runtime, inboundChannel };
    expect(handleSession('owner', 'main', deps).text).toContain(`sessionId=${expected}`);
    expect(JSON.stringify(await handleCc('owner', 'main', deps))).toContain(expected);
    const persisted = { getSession: () => ({ sessionId: 'persisted-channel' }) } as never;
    expect(JSON.stringify(await handleCc('owner', 'main', { ...deps, stateManager: persisted }))).toContain('persisted-channel');
  });

  it('prefers live process sessionId over relay.json (the live process is the truth)', () => {
    const router = setupRouter();
    const now = Date.now();
    const pool = createMockPool({
      'owner:main': { tenantId: 'owner:main', state: 'idle', pid: 12345, lastActivity: now, spawnedAt: now - 60000, restartCount: 0 },
    });
    const result = handleSession('owner', 'main', { router, runtime: pool });

    expect(result.text).toContain('\u{1F3B2} Duke');
    expect(result.text).toContain('Session ID');
    // mock pool returns proc.sessionId='mock-session' — that's the live truth, not relay.json's 'uuid-duke'
    expect(result.text).toContain('mock-session');
    expect(result.text).toContain('来源: live-process');
    expect(result.text).toContain('runtime=cc');
    expect(result.text).toContain('sessionId=mock-session');
    expect(result.text).toContain('state=idle');
    expect(result.text).toContain('lastActivity=');
    expect(result.text).toContain('/home/duke-space');
    // JSONL path derived from workspace + live sessionId
    expect(result.text).toMatch(/JSONL: .*\.claude\/projects\/.*-home-duke-space\/mock-session\.jsonl/);
    expect(result.text).toContain('PID');
    expect(result.text).toContain('12345');
  });

  it('falls back to relay.json sessionId when no process and no state.json', () => {
    const router = setupRouter();
    const pool = createMockPool();
    const result = handleSession('owner', 'main', { router, runtime: pool });

    expect(result.text).toContain('uuid-duke');
    expect(result.text).toContain('来源: relay.json:top');
    expect(result.text).toContain('未启动');
    expect(result.text).not.toContain('PID');
  });

  it('prefers state.json over relay.json when process is absent (relay.json is often stale)', () => {
    const router = setupRouter();
    const pool = createMockPool();
    const stateManager = {
      getSession: (poolKey: string) => poolKey === 'owner:main'
        ? {
          poolKey,
          agentId: 'main',
          sessionId: 'persisted-sid-from-state-json',
          turnCount: 0,
          maxTurns: 20,
          lastActivity: Date.now(),
          status: 'active' as const,
        }
        : undefined,
    };
    const result = handleSession('owner', 'main', { router, runtime: pool, stateManager: stateManager as never });

    expect(result.text).toContain('persisted-sid-from-state-json');
    expect(result.text).toContain('来源: state.json');
    expect(result.text).toContain('待恢复');
    expect(result.text).toContain('runtime=cc');
    expect(result.text).toContain('session 仍保留');
    expect(result.text).not.toContain('PID');
  });

  it('shows Codex runtime state with thread prefix and no bot token', () => {
    const router = setupRouter();
    const now = Date.now();
    const runtime = {
      ...createMockPool(),
      get: (tenantId: string) => tenantId === 'owner:aki'
        ? {
          tenantId,
          runtime: 'codex' as const,
          state: 'idle' as const,
          sessionId: 'uuid-aki',
          codexThreadId: '019dc337-1234-5678-9012-abcdefabcdef',
          lastActivity: now,
          spawnedAt: now - 1000,
          restartCount: 0,
          restartWindowStart: now - 1000,
        }
        : undefined,
    };

    const result = handleSession('owner', 'aki', { router, runtime });

    expect(result.text).toContain('runtime=codex');
    expect(result.text).toContain('threadId=019dc337...');
    expect(result.text).toContain('state=idle');
    expect(result.text).toContain('lastActivity=');
    expect(result.text).not.toContain('fake-aki');
    expect(result.text).not.toMatch(/token/i);
  });
});

describe('handleCc', () => {
  it('returns error when agent has no session ID', async () => {
    const cfg = JSON.parse(JSON.stringify(sampleConfig));
    delete cfg.users[0].agents[0].sessionId;
    writeFileSync(join(testDir, 'relay.json'), JSON.stringify(cfg));
    const router = createRouter(join(testDir, 'relay.json'));
    const pool = createMockPool();

    const result = await handleCc('owner', 'main', { router, runtime: pool });

    expect(result.text).toContain('无 session ID');
  });

  it('kills CC process and returns claude --resume command', async () => {
    const router = setupRouter();
    const pool = createMockPool();

    const result = await handleCc('owner', 'main', { router, runtime: pool });

    expect(result.text).toContain('已释放');
    expect(result.feishuCardElements).toBeDefined();
    const cardText = JSON.stringify(result.feishuCardElements);
    expect(cardText).toContain('claude --resume');
  });

  it('resumes Fable latest with the native alias and clears an inherited model override', async () => {
    const router = setupRouter();
    const modelCatalog = createModelCatalog(join(import.meta.dirname, '../config/models.example.json'));
    const createToken = vi.fn<TerminalManager['createToken']>(() => 'terminal-test-token');
    const deps = { router, runtime: createMockPool(), modelCatalog };
    await handleModel('owner', 'main', 'claude-fable-latest', deps);

    vi.stubEnv('ANTHROPIC_DEFAULT_FABLE_MODEL', 'claude-fable-5');
    try {
      await handleCc('owner', 'main', {
        ...deps,
        terminalManager: { createToken } as unknown as TerminalManager,
      });
    } finally {
      vi.unstubAllEnvs();
    }

    const [{ ccArgs = [], ccEnv }] = createToken.mock.calls[0];
    expect(ccArgs[ccArgs.indexOf('--model') + 1]).toBe('fable');
    expect(ccEnv?.['ANTHROPIC_DEFAULT_FABLE_MODEL']).toBe('');
    const settings = JSON.parse(ccArgs[ccArgs.indexOf('--settings') + 1]);
    expect(settings.env.ANTHROPIC_DEFAULT_FABLE_MODEL).toBe('');
  });
});

describe('handleUsage', () => {
  it('falls back to friendly notice when meteringDir is not configured', () => {
    const router = setupRouter();
    const pool = createMockPool();
    const result = handleUsage('owner', 'main', { router, runtime: pool });

    expect(result.text).toContain('\u{1F3B2} Duke');
    expect(result.text).toContain('未启用 metering');
    expect(result.text).toContain('METERING_DIR');
  });

  it('reports zero-record period when metering dir exists but month has no data', () => {
    const router = setupRouter();
    const pool = createMockPool();
    const fakeDir = join(testDir, 'metering-empty');
    const summarize = () => ({
      tenant: 'owner:main',
      monthStart: '2026-05-01',
      monthEnd: '2026-05-15',
      recordCount: 0,
      totalCostUsd: 0,
      totalInputTokens: 0,
      totalOutputTokens: 0,
      totalCacheCreationInputTokens: 0,
      totalCacheReadInputTokens: 0,
      daysCovered: 0,
      malformedEntries: 0,
    });

    const result = handleUsage('owner', 'main', { router, runtime: pool, meteringDir: fakeDir, summarizeUsage: summarize });

    expect(result.text).toContain('\u{1F3B2} Duke');
    expect(result.text).toContain('2026-05-01 ~ 2026-05-15');
    expect(result.text).toContain('暂无 metering 记录');
    expect(result.text).not.toContain('Cost: $');
  });

  it('renders aggregate fields when records exist', () => {
    const router = setupRouter();
    const pool = createMockPool();
    const summarize = (_dir: string, tenant: string) => ({
      tenant,
      monthStart: '2026-05-01',
      monthEnd: '2026-05-15',
      recordCount: 12,
      totalCostUsd: 1.234567,
      totalInputTokens: 12345,
      totalOutputTokens: 6789,
      totalCacheCreationInputTokens: 100000,
      totalCacheReadInputTokens: 250000,
      daysCovered: 7,
      malformedEntries: 0,
    });

    const result = handleUsage('owner', 'main', { router, runtime: pool, meteringDir: '/tmp/metering', summarizeUsage: summarize });

    expect(result.text).toContain('\u{1F3B2} Duke');
    expect(result.text).toContain('调用次数: 12');
    expect(result.text).toContain('Cost: $1.2346 USD');
    expect(result.text).toContain('Input tokens:  12,345');
    expect(result.text).toContain('Output tokens: 6,789');
    expect(result.text).toContain('Cache read:    250,000');
    expect(result.text).toContain('Cache create:  100,000');
    expect(result.text).toContain('7 天有数据');
    expect(result.text).not.toContain('损坏记录');
  });

  it('appends warning when malformed entries were skipped', () => {
    const router = setupRouter();
    const pool = createMockPool();
    const summarize = (_dir: string, tenant: string) => ({
      tenant,
      monthStart: '2026-05-01',
      monthEnd: '2026-05-15',
      recordCount: 3,
      totalCostUsd: 0.5,
      totalInputTokens: 100,
      totalOutputTokens: 50,
      totalCacheCreationInputTokens: 0,
      totalCacheReadInputTokens: 0,
      daysCovered: 1,
      malformedEntries: 2,
    });

    const result = handleUsage('owner', 'main', { router, runtime: pool, meteringDir: '/tmp/metering', summarizeUsage: summarize });

    expect(result.text).toContain('调用次数: 3');
    expect(result.text).toContain('跳过 2 条损坏记录');
  });

  it('uses correct poolKey as tenant filter', () => {
    const router = setupRouter();
    const pool = createMockPool();
    let observedTenant: string | undefined;
    const summarize = (_dir: string, tenant: string) => {
      observedTenant = tenant;
      return {
        tenant,
        monthStart: '2026-05-01',
        monthEnd: '2026-05-15',
        recordCount: 0,
        totalCostUsd: 0,
        totalInputTokens: 0,
        totalOutputTokens: 0,
        totalCacheCreationInputTokens: 0,
        totalCacheReadInputTokens: 0,
        daysCovered: 0,
        malformedEntries: 0,
      };
    };

    handleUsage('owner', 'aki', { router, runtime: pool, meteringDir: '/tmp/metering', summarizeUsage: summarize });

    expect(observedTenant).toBe(router.poolKey('owner', 'aki'));
  });
});

describe('handleModel', () => {
  it('offers the Fable latest alias in the CC model picker and persists the alias', async () => {
    const modelCatalog = createModelCatalog(join(import.meta.dirname, '../config/models.example.json'));
    const router = setupRouter();
    const deps = { router, runtime: createMockPool(), modelCatalog };

    const card = await handleModelScope('owner', 'main', 'cc|subscription', deps);
    expect(JSON.stringify(card.feishuCardElements)).toContain('claude-fable-latest');

    await handleModel('owner', 'main', 'claude-fable-latest', deps);
    expect(router.getAgentModel('owner', 'main')).toBe('claude-fable-latest');
  });

  it('removes previous major models from CC and Codex catalog options and rejects stale selections', async () => {
    const catalogPath = writeTmpConfig('models.json', {
      providers: {
        'openai-chatgpt': { protocol: 'openai', base_url_env: 'UNUSED', api_key_pool: [] },
      },
      models: [
        { id: 'gpt-5.6-sol', display_name: 'GPT-5.6 Sol', provider: 'openai-chatgpt' },
        { id: 'gpt-6.1-sol', display_name: 'GPT-6.1 Sol', provider: 'openai-chatgpt' },
        { id: 'claude-opus-4-7', display_name: 'Claude Opus 4.7', claude_alias: true },
        { id: 'claude-opus-5-5', display_name: 'Claude Opus 5.5', claude_alias: true },
        { id: 'haiku', display_name: 'Haiku latest', claude_alias: true },
      ],
    });
    const modelCatalog = createModelCatalog(catalogPath);
    const router = setupRouter();
    const pool = createMockPool();
    for (const [agentId, runtime, current] of [
      ['main', 'cc', 'claude-opus-5-5'], ['aki', 'codex', 'gpt-6.1-sol'],
    ] as const) {
      const card = await handleModelScope('owner', agentId, `${runtime}|subscription`, { router, runtime: pool, modelCatalog });
      const cardJson = JSON.stringify(card.feishuCardElements);
      expect(cardJson).toContain(current);
      expect(cardJson).not.toContain('gpt-5');
      expect(cardJson).not.toContain('claude-opus-4');
      expect(cardJson).not.toContain('haiku');
      const models = await handleModels('owner', agentId, { router, runtime: pool, modelCatalog });
      expect(models.text).toContain(current);
      expect(models.text).not.toContain('gpt-5');
      expect(models.text).not.toContain('claude-opus-4');
      const rejected = await handleModel('owner', agentId, 'claude-opus-4-7', { router, runtime: pool, modelCatalog });
      expect(rejected.text).toContain('已从 Relay 模型选项移除');
      expect(router.getAgentModel('owner', agentId)).toBeUndefined();
    }
  });

  it('uses Pi native model discovery and set_model while preserving the live session', async () => {
    const cfg = JSON.parse(JSON.stringify(sampleConfig));
    cfg.users[0].agents[0].runtime = 'pi';
    cfg.users[0].agents[0].conversationRuntime = {
      runtime: 'pi',
      model: { provider: 'anthropic', model: 'claude-sonnet-4-5', reasoning: 'high' },
      isolationProfile: 'internal-canary',
    };
    writeFileSync(join(testDir, 'relay.json'), JSON.stringify(cfg));
    const router = createRouter(join(testDir, 'relay.json'));
    const pool = createMockPool({
      'owner:main': { tenantId: 'owner:main', state: 'idle', pid: 938, lastActivity: Date.now(), spawnedAt: Date.now(), restartCount: 0 },
    });
    const setModel = vi.fn(async () => undefined);
    const listModels = vi.fn(async () => [
      { provider: 'anthropic', model: 'claude-opus-5-5', displayName: 'Claude Opus 5.5' },
      { provider: 'openrouter', model: 'anthropic/claude-opus-5.5', displayName: 'Claude Opus 5.5' },
    ]);
    Object.assign(pool, { setModel, listModels });

    const card = await handleModel('owner', 'main', undefined, { router, runtime: pool });
    expect(card.text).toContain('当前 runtime: pi');
    const cardJson = JSON.stringify(card.feishuCardElements);
    expect(cardJson).not.toContain('relay_menu_arg":"auto');
    expect(cardJson).not.toContain('relay_menu_arg":"default');
    expect(listModels).not.toHaveBeenCalled();

    const scoped = await handleModelScope('owner', 'main', 'pi|openrouter', { router, runtime: pool });
    expect(scoped.text).toContain('OpenRouter API');
    expect(scoped.text).toContain('可用模型: 1');
    expect(listModels).toHaveBeenCalledOnce();

    const result = await handleModel('owner', 'main', 'openrouter/anthropic/claude-opus-5.5', { router, runtime: pool });
    expect(setModel).toHaveBeenCalledWith('owner:main', {
      provider: 'openrouter', model: 'anthropic/claude-opus-5.5', reasoning: 'high',
    });
    expect(result.text).toContain('当前 native session 已热切换');
    expect(readConfig().users[0].agents[0].conversationRuntime.model).toEqual({
      provider: 'openrouter', model: 'anthropic/claude-opus-5.5', reasoning: 'high',
    });
  });

  it('rejects model changes while a Pi turn is still busy', async () => {
    const cfg = JSON.parse(JSON.stringify(sampleConfig));
    cfg.users[0].agents[0].runtime = 'pi';
    cfg.users[0].agents[0].conversationRuntime = {
      runtime: 'pi',
      model: { provider: 'anthropic', model: 'claude-sonnet-4-5', reasoning: 'high' },
      isolationProfile: 'internal-canary',
    };
    writeFileSync(join(testDir, 'relay.json'), JSON.stringify(cfg));
    const router = createRouter(join(testDir, 'relay.json'));
    const pool = createMockPool({
      'owner:main': { tenantId: 'owner:main', state: 'busy', pid: 938, lastActivity: Date.now(), spawnedAt: Date.now(), restartCount: 0 },
    });

    const result = await handleModel('owner', 'main', 'openrouter/anthropic/claude-opus-5.5', { router, runtime: pool });
    expect(result.text).toContain('请先 /stop');
    expect(readConfig().users[0].agents[0].conversationRuntime.model.model).toBe('claude-sonnet-4-5');
  });

  it('shows current model when no arg given', async () => {
    const router = setupRouter();
    const pool = createMockPool();

    const result = await handleModel('owner', 'main', undefined, { router, runtime: pool });

    expect(result.text).toContain('Duke');
    expect(result.text).toContain('当前 runtime: cc');
    expect(result.text).toContain('当前模型: default（运行时自主选择）');
  });

  it('shows ccModel when agent has one configured', async () => {
    const cfg = JSON.parse(JSON.stringify(sampleConfig));
    cfg.users[0].agents[0].ccModel = 'claude-sonnet-4-6';
    writeFileSync(join(testDir, 'relay.json'), JSON.stringify(cfg));
    const router = createRouter(join(testDir, 'relay.json'));
    const pool = createMockPool();

    const result = await handleModel('owner', 'main', undefined, { router, runtime: pool });

    expect(result.text).toContain('当前模型: claude-sonnet-4-6');
  });

  it('passes sonnet alias through to CC without expanding to full ID', async () => {
    const router = setupRouter();
    const pool = createMockPool();

    const result = await handleModel('owner', 'main', 'sonnet', { router, runtime: pool });

    expect(result.text).toContain('sonnet');
    // setAgentModel writes to agent-models.json, not relay.json — verify via router
    expect(router.getAgentModel('owner', 'main')).toBe('sonnet');
  });

  it('passes opus alias through to CC without expanding to full ID', async () => {
    const router = setupRouter();
    const pool = createMockPool();

    const result = await handleModel('owner', 'main', 'opus', { router, runtime: pool });

    expect(result.text).toContain('opus');
    expect(router.getAgentModel('owner', 'main')).toBe('opus');
  });

  it('rejects the Haiku alias while its native latest version is Claude 4.x', async () => {
    const router = setupRouter();
    const pool = createMockPool();

    const result = await handleModel('owner', 'main', 'haiku', { router, runtime: pool });

    expect(result.text).toContain('已从 Relay 模型选项移除');
    expect(router.getAgentModel('owner', 'main')).toBeUndefined();
  });

  it('accepts full claude-* model ID directly', async () => {
    const router = setupRouter();
    const pool = createMockPool();

    const result = await handleModel('owner', 'main', 'claude-opus-5-5', { router, runtime: pool });

    expect(result.text).toContain('claude-opus-5-5');
    expect(router.getAgentModel('owner', 'main')).toBe('claude-opus-5-5');
  });

  it('rejects unsupported model names without changing relay.json', async () => {
    const router = setupRouter();
    const pool = createMockPool();

    const result = await handleModel('owner', 'main', 'gpt-4o', { router, runtime: pool });

    expect(result.text).toContain('不支持的模型');
    expect(router.getAgentModel('owner', 'main')).toBeUndefined();
  });

  it('kills running process on model switch', async () => {
    const router = setupRouter();
    const pool = createMockPool({
      'owner:main': { tenantId: 'owner:main', state: 'idle', pid: 12345, lastActivity: Date.now(), spawnedAt: Date.now(), restartCount: 0 },
    });
    let killedKey = '';
    pool.kill = async (tenantId: string) => { killedKey = tenantId; };

    const result = await handleModel('owner', 'main', 'opus', { router, runtime: pool });

    expect(killedKey).toBe('owner:main');
    expect(result.text).toContain('热切换');
  });

  it('does not kill when no process is running', async () => {
    const router = setupRouter();
    const pool = createMockPool();
    let killCalled = false;
    pool.kill = async () => { killCalled = true; };

    const result = await handleModel('owner', 'main', 'sonnet', { router, runtime: pool });

    expect(killCalled).toBe(false);
    expect(result.text).toContain('下次发消息时以新模型');
  });

  it('warns when switching model while runtime is codex', async () => {
    const router = setupRouter();
    // aki agent has runtime=codex in sampleConfig
    const pool = createMockPool({
      'owner:aki': { tenantId: 'owner:aki', state: 'idle', pid: 99, lastActivity: Date.now(), spawnedAt: Date.now(), restartCount: 0 },
    });

    const result = await handleModel('owner', 'aki', 'opus', { router, runtime: pool });

    expect(result.text).toContain('opus');
    // Without modelCatalog, /model does a hot-switch without runtime check
    expect(result.text).toContain('model →');
  });

  it('shows the unified runtime/access control surface on a codex agent', async () => {
    const router = setupRouter();
    const pool = createMockPool({
      'owner:aki': { tenantId: 'owner:aki', state: 'idle', pid: 99, lastActivity: Date.now(), spawnedAt: Date.now(), restartCount: 0 },
    });

    const result = await handleModel('owner', 'aki', undefined, { router, runtime: pool });

    expect(result.text).toContain('当前 runtime: codex');
    expect(result.text).toContain('runtime → 接入方式 → model');
  });
});

describe('Pi native internal commands', () => {
  function setupPiCommandRuntime(state: 'idle' | 'busy' = 'idle') {
    const cfg = JSON.parse(JSON.stringify(sampleConfig));
    cfg.users[0].agents[0].runtime = 'pi';
    cfg.users[0].agents[0].conversationRuntime = {
      runtime: 'pi',
      model: { provider: 'anthropic', model: 'claude-sonnet-4-5', reasoning: 'high' },
      isolationProfile: 'internal-canary',
    };
    writeFileSync(join(testDir, 'relay.json'), JSON.stringify(cfg));
    const router = createRouter(join(testDir, 'relay.json'));
    const pool = createMockPool({
      'owner:main': { tenantId: 'owner:main', state, pid: 938, lastActivity: Date.now(), spawnedAt: Date.now(), restartCount: 0 },
    });
    return { router, pool };
  }

  it('implements /models and /think with Pi native discovery and durable reasoning', async () => {
    const { router, pool } = setupPiCommandRuntime();
    const listModels = vi.fn(async () => [
      { provider: 'anthropic', model: 'claude-opus-5-5', displayName: 'Claude Opus 5.5' },
    ]);
    const listThinkingLevels = vi.fn(async () => ['off', 'low', 'high'] as const);
    const setThinkingLevel = vi.fn(async () => undefined);
    Object.assign(pool, { listModels, listThinkingLevels, setThinkingLevel });

    const models = await handleModels('owner', 'main', { router, runtime: pool });
    expect(models.text).toContain('pi 可用模型');
    expect(models.text).toContain('匹配: 1');
    const card = await handleThink('owner', 'main', undefined, { router, runtime: pool });
    expect(card.text).toContain('当前模型原生支持: off, low, high');
    expect(card.feishuCardElements).toBeDefined();

    const switched = await handleThink('owner', 'main', 'low', { router, runtime: pool });
    expect(setThinkingLevel).toHaveBeenCalledWith('owner:main', 'low');
    expect(switched.text).toContain('当前 native session 已热切换');
    expect((readConfig() as any).users[0].agents[0].conversationRuntime.model.reasoning).toBe('low');
  });

  it('keeps the latest native Sol visible and switchable and removes previous major models', async () => {
    const { router, pool } = setupPiCommandRuntime();
    const modelIds = [
      'gpt-5:batch', 'gpt-5.3-codex-spark', 'gpt-5.4', 'gpt-5.4-mini', 'gpt-5.5',
      'gpt-5.6-luna', 'gpt-5.6-sol', 'gpt-5.6-terra',
      'gpt-6-astra', 'gpt-6-luna', 'gpt-6-sol',
      'gpt-6.1-astra', 'gpt-6.1-luna', 'gpt-6.1-sol',
    ];
    const listModels = vi.fn(async () => [
      { provider: 'anthropic', model: 'claude-fable-5', displayName: 'Claude Fable 5' },
      { provider: 'anthropic', model: 'claude-opus-5-5', displayName: 'Claude Opus 5.5' },
    ].concat(modelIds.map((model) => ({
      provider: 'openai-codex', model, displayName: model,
    }))));
    const setModel = vi.fn(async () => undefined);
    Object.assign(pool, { listModels, setModel });

    const card = await handleModelScope('owner', 'main', 'pi|subscription', { router, runtime: pool });
    expect(card.text).toContain('可用模型: 8');
    const cardJson = JSON.stringify(card.feishuCardElements);
    expect(cardJson).toContain('openai-codex/gpt-6.1-sol');
    expect(cardJson.indexOf('openai-codex/gpt-6.1-sol')).toBeLessThan(cardJson.indexOf('openai-codex/gpt-6-sol'));
    expect(cardJson).not.toContain('gpt-5');
    const models = await handleModels('owner', 'main', { router, runtime: pool });
    expect(models.text).toContain('gpt-6.1-sol');
    expect(models.text).not.toContain('gpt-5');

    const selected = await handleModel('owner', 'main', 'openai-codex/gpt-6.1-sol', { router, runtime: pool });
    expect(selected.text).toContain('openai-codex/gpt-6.1-sol');
    expect(setModel).toHaveBeenCalledWith('owner:main', {
      provider: 'openai-codex', model: 'gpt-6.1-sol', reasoning: 'high',
    });
  });

  it('removes previous major models from native subscription and OpenRouter menus and search', async () => {
    const { router, pool } = setupPiCommandRuntime();
    const listModels = vi.fn(async () => [
      { provider: 'anthropic', model: 'claude-opus-4-7', displayName: 'Claude Opus 4.7' },
      { provider: 'anthropic', model: 'claude-opus-5-5', displayName: 'Claude Opus 5.5' },
      { provider: 'openrouter', model: 'anthropic/claude-sonnet-4.6', displayName: 'Claude Sonnet 4.6' },
      { provider: 'openrouter', model: 'openai/gpt-5.6-sol', displayName: 'GPT-5.6 Sol' },
      { provider: 'openrouter', model: 'anthropic/claude-opus-5.5', displayName: 'Claude Opus 5.5' },
      { provider: 'openrouter', model: 'deepseek/deepseek-v4-flash-0731', displayName: 'DeepSeek V4 Flash' },
    ]);
    const setModel = vi.fn(async () => undefined);
    Object.assign(pool, { listModels, setModel });
    for (const access of ['subscription', 'openrouter']) {
      const card = await handleModelScope('owner', 'main', `pi|${access}`, { router, runtime: pool });
      const cardJson = JSON.stringify(card.feishuCardElements);
      expect(cardJson).not.toContain('claude-opus-4');
      expect(cardJson).not.toContain('claude-sonnet-4');
      expect(cardJson).not.toContain('gpt-5');
      expect(cardJson).toContain('claude-opus-5');
    }
    const models = await handleModels('owner', 'main', { router, runtime: pool });
    expect(models.text).not.toContain('claude-opus-4');
    expect(models.text).not.toContain('claude-sonnet-4');
    expect(models.text).not.toContain('gpt-5');
    expect(models.text).toContain('deepseek-v4');
    for (const retired of ['openai-codex/gpt-5.6-sol', 'openrouter/anthropic/claude-sonnet-4.6']) {
      const result = await handleModel('owner', 'main', retired, { router, runtime: pool });
      expect(result.text).toContain('已从 Relay 模型选项移除');
    }
    expect(setModel).not.toHaveBeenCalled();
  });

  it('cold-starts the exact persisted Pi session for native command discovery after /new', async () => {
    const cfg = JSON.parse(JSON.stringify(sampleConfig));
    cfg.users[0].agents[0].runtime = 'pi';
    cfg.users[0].agents[0].conversationRuntimePreference = ['pi'];
    cfg.users[0].agents[0].conversationRuntime = {
      runtime: 'pi',
      model: { provider: 'openrouter', model: 'openai/gpt-6.1-sol', reasoning: 'medium' },
      isolationProfile: 'internal-canary',
    };
    writeFileSync(join(testDir, 'relay.json'), JSON.stringify(cfg));
    const router = createRouter(join(testDir, 'relay.json'));
    const stateManager = createStateManager({
      'owner:main': {
        poolKey: 'owner:main',
        agentId: 'main',
        runtime: 'pi',
        sessionId: 'new-session-from-command',
        turnCount: 0,
        maxTurns: 20,
        lastActivity: Date.now(),
        status: 'active',
      },
    });
    let live: (NonNullable<ReturnType<Pool['get']>> & { runtime: 'pi' }) | undefined;
    const spawn = vi.fn(async (config: Parameters<Pool['spawn']>[0]) => {
      live = {
        tenantId: config.tenantId,
        process: {} as never,
        sessionId: config.sessionId,
        state: 'ready',
        lastActivity: Date.now(),
        spawnedAt: Date.now(),
        restartCount: 0,
        restartWindowStart: Date.now(),
        runtime: 'pi',
      };
      return live!;
    });
    const pool = Object.assign(createMockPool(), {
      spawn,
      get: () => live,
      listModels: vi.fn(async () => [
        { provider: 'openrouter', model: 'openai/gpt-6.1-sol', displayName: 'GPT-6.1 Sol' },
      ]),
      listThinkingLevels: vi.fn(async () => ['off', 'low', 'medium', 'high'] as const),
      getNativeState: vi.fn(async () => ({ sessionId: 'new-session-from-command', messageCount: 0 })),
      getNativeSessionStats: vi.fn(async () => ({ totalMessages: 0, contextUsage: { tokens: 0 } })),
    });

    const model = await handleModelScope('owner', 'main', 'pi|openrouter', {
      router, runtime: pool, stateManager, inboundChannel: 'feishu',
    });
    const thinking = await handleThink('owner', 'main', undefined, {
      router, runtime: pool, stateManager, inboundChannel: 'feishu',
    });
    const context = await handleContext('owner', 'main', {
      router, runtime: pool, stateManager, inboundChannel: 'feishu',
    });

    expect(spawn).toHaveBeenCalledTimes(1);
    expect(spawn).toHaveBeenCalledWith(expect.objectContaining({
      tenantId: 'owner:main',
      runtime: 'pi',
      conversationRuntimePreference: ['pi'],
      conversationRuntime: cfg.users[0].agents[0].conversationRuntime,
      sessionId: 'new-session-from-command',
      runtimeSessionBinding: { runtime: 'pi', sessionId: 'new-session-from-command' },
      resume: true,
    }));
    expect(model.text).toContain('可用模型: 1');
    expect(model.feishuCardElements).toBeDefined();
    expect(thinking.text).toContain('当前模型原生支持: off, low, medium, high');
    expect(context.text).toContain('sessionId=new-session-from-command');
    expect(stateManager.flushed).toBe(true);
  });

  it('implements Pi native /compact and /context without sending slash text as a prompt', async () => {
    const { router, pool } = setupPiCommandRuntime();
    const compact = vi.fn(async () => ({ tokensBefore: 200, estimatedTokensAfter: 40 }));
    const getNativeState = vi.fn(async () => ({
      sessionId: 'pi-session', messageCount: 8, autoCompactionEnabled: true,
    }));
    const getNativeSessionStats = vi.fn(async () => ({
      totalMessages: 8,
      tokens: { total: 42 },
      contextUsage: { tokens: 21, contextWindow: 200, percent: 10.5 },
    }));
    Object.assign(pool, { compact, getNativeState, getNativeSessionStats });

    const compacted = await handleCompact('owner', 'main', 'keep decisions', { router, runtime: pool });
    expect(compact).toHaveBeenCalledWith('owner:main', 'keep decisions');
    expect(compacted.text).toContain('tokensBefore=200');
    expect(compacted.text).toContain('estimatedTokensAfter=40');

    const context = await handleContext('owner', 'main', { router, runtime: pool });
    expect(context.text).toContain('sessionId=pi-session');
    expect(context.text).toContain('contextTokens=21');
    expect(context.text).toContain('percent=10.5');
  });

  it('bounds a 352-model Pi provider card and requires /models filtering for the full catalog', async () => {
    const { router, pool } = setupPiCommandRuntime();
    const nativeModels = Array.from({ length: 352 }, (_, index) => ({
      provider: 'openrouter',
      model: `vendor/model-${index.toString().padStart(3, '0')}`,
      displayName: `Model ${index.toString().padStart(3, '0')}`,
    }));
    Object.assign(pool, { listModels: vi.fn(async () => nativeModels) });

    const card = await handleModelScope('owner', 'main', 'pi|openrouter', { router, runtime: pool });
    const serialized = JSON.stringify(card.feishuCardElements);
    expect(serialized.match(/\"relay_menu\":\"model\"/g)).toHaveLength(8);
    expect(card.text).toContain('可用模型: 352');
    expect(card.text).toContain('/models <关键词>');

    const list = await handleModels('owner', 'main', { router, runtime: pool }, 'model-');
    expect(list.text.match(/^• /gm)).toHaveLength(30);
    expect(list.text).toContain('仅显示前 30 个');
  });

  it('prefers reviewed Relay catalog entries inside the Pi OpenRouter scope', async () => {
    const { router, pool } = setupPiCommandRuntime();
    const modelCatalog = createModelCatalog(writeTmpConfig('pi-reviewed-models.json', {
      providers: {
        openrouter: { protocol: 'anthropic', base_url_env: 'OPENROUTER_BASE_URL', api_key_pool: ['OPENROUTER_API_KEY'] },
      },
      models: [
        { id: 'deepseek/deepseek-v4-flash-0731', display_name: 'DeepSeek V4 Flash', provider: 'openrouter' },
      ],
    }));
    Object.assign(pool, {
      listModels: vi.fn(async () => [
        ...Array.from({ length: 20 }, (_, index) => ({
          provider: 'openrouter', model: `vendor/unreviewed-${index}`, displayName: `Unreviewed ${index}`,
        })),
        { provider: 'openrouter', model: 'deepseek/deepseek-v4-flash-0731', displayName: 'DeepSeek V4 Flash' },
      ]),
    });

    const card = await handleModelScope('owner', 'main', 'pi|openrouter', { router, runtime: pool, modelCatalog });
    expect(card.text).toContain('可用模型: 1');
    expect(JSON.stringify(card.feishuCardElements)).toContain('DeepSeek V4 Flash');
    expect(JSON.stringify(card.feishuCardElements)).not.toContain('Unreviewed');
  });
});

// P0-003: handleModel with model catalog (domestic model routing)
describe('handleModel with ModelCatalog [P0-003]', () => {
  function setupModelCatalog(configPath?: string) {
    return createModelCatalog(configPath);
  }

  it('accepts a provider model ID from catalog and writes to relay.json', async () => {
    const configPath = writeTmpConfig('models.json', {
      models: [
        { id: 'deepseek/deepseek-v4-flash-0731', display_name: 'DeepSeek V4 Flash', provider: 'openrouter' },
        { id: 'x-ai/grok-code-fast-1', display_name: 'Grok Code Fast', provider: 'openrouter' },
      ],
    });
    const modelCatalog = setupModelCatalog(configPath);
    const router = setupRouter();
    const pool = createMockPool();

    const result = await handleModel('owner', 'main', 'deepseek/deepseek-v4-flash-0731', { router, runtime: pool, modelCatalog });

    expect(result.text).toContain('deepseek/deepseek-v4-flash-0731');
    expect(result.text).toContain('model →');
    expect(router.getAgentModel('owner', 'main')).toBe('deepseek/deepseek-v4-flash-0731');
  });

  it('rejects nonexistent model ID and shows available models list', async () => {
    const configPath = writeTmpConfig('models.json', {
      models: [
        { id: 'deepseek/deepseek-v4-flash-0731', display_name: 'DeepSeek V4 Flash' },
        { id: 'x-ai/grok-code-fast-1', display_name: 'Grok Code Fast' },
      ],
    });
    const modelCatalog = setupModelCatalog(configPath);
    const router = setupRouter();
    const pool = createMockPool();

    const result = await handleModel('owner', 'main', 'nonexistent-model', { router, runtime: pool, modelCatalog });

    expect(result.text).toContain('不支持的模型');
    expect(result.text).toContain('deepseek/deepseek-v4-flash-0731');
    expect(result.text).toContain('DeepSeek V4 Flash');
    expect(result.text).toContain('x-ai/grok-code-fast-1');
    expect(result.text).toContain('Grok Code Fast');
    expect(router.getAgentModel('owner', 'main')).toBeUndefined();
  });

  it('/model without arg shows current and available models with [当前] marker', async () => {
    const configPath = writeTmpConfig('models.json', {
      models: [
        { id: 'deepseek/deepseek-v4-flash-0731', display_name: 'DeepSeek V4 Flash' },
        { id: 'x-ai/grok-code-fast-1', display_name: 'Grok Code Fast' },
        { id: 'sonnet', display_name: 'Claude Sonnet' },
      ],
    });
    const modelCatalog = setupModelCatalog(configPath);

    // Set current model to the OpenRouter DeepSeek model
    const cfg = JSON.parse(JSON.stringify(sampleConfig));
    cfg.users[0].agents[0].ccModel = 'deepseek/deepseek-v4-flash-0731';
    writeFileSync(join(testDir, 'relay.json'), JSON.stringify(cfg));
    const router = createRouter(join(testDir, 'relay.json'));
    const pool = createMockPool();

    const result = await handleModel('owner', 'main', undefined, { router, runtime: pool, modelCatalog });

    // No arg with catalog → interactive button card (not text list)
    expect(result.feishuCardElements).toBeDefined();
    expect(result.feishuCardElements!.length).toBeGreaterThan(0);
    expect(result.text).toContain('模型选择');
  });

  it('existing Claude alias behavior unchanged when modelCatalog is provided', async () => {
    const configPath = writeTmpConfig('models.json', {
      models: [{ id: 'deepseek/deepseek-v4-flash-0731', display_name: 'DeepSeek V4 Flash' }],
    });
    const modelCatalog = setupModelCatalog(configPath);
    const router = setupRouter();
    const pool = createMockPool();

    const result = await handleModel('owner', 'main', 'sonnet', { router, runtime: pool, modelCatalog });

    expect(result.text).toContain('sonnet');
    expect(router.getAgentModel('owner', 'main')).toBe('sonnet');
  });

  it('existing full claude-* ID behavior unchanged when modelCatalog is provided', async () => {
    const configPath = writeTmpConfig('models.json', {
      models: [{ id: 'deepseek/deepseek-v4-flash-0731', display_name: 'DeepSeek V4 Flash' }],
    });
    const modelCatalog = setupModelCatalog(configPath);
    const router = setupRouter();
    const pool = createMockPool();

    const result = await handleModel('owner', 'main', 'claude-opus-5-5', { router, runtime: pool, modelCatalog });

    expect(result.text).toContain('claude-opus-5-5');
    expect(router.getAgentModel('owner', 'main')).toBe('claude-opus-5-5');
  });

  it('works without modelCatalog (backward compatibility)', async () => {
    const router = setupRouter();
    const pool = createMockPool();

    const result = await handleModel('owner', 'main', 'sonnet', { router, runtime: pool });

    expect(result.text).toContain('sonnet');
    expect(router.getAgentModel('owner', 'main')).toBe('sonnet');
  });
});

describe('an unfinished /wi switch and an explicit session replacement', () => {
  const ref='e'.repeat(64);
  const record=(phase?:'entered')=>({ref,target:'wi_b',left:'wi_a',agent:true as const,resumed:false,at:'t',
    origin:{sessionId:'sess-a',runtime:'cc' as const},dest:{sessionId:'sess-new-1',runtime:'cc' as const},
    ...(phase?{phase}:{})});
  /** The conversation after the switch: on dest once entered, else back on origin. */
  function conversation(phase?:'entered'){
    const sessionId=phase?'sess-new-1':'sess-a';
    return createStateManager({'owner:main':{poolKey:'owner:main',agentId:'main',runtime:'cc',sessionId,
      lastActivity:Date.now(),status:'active',turnCount:1,maxTurns:20,pendingWiSwitch:record(phase)} as never});
  }
  /** The live runtime after /reset: one fresh process on whatever session is persisted now. */
  function live(stateManager:ReturnType<typeof createStateManager>){
    let pid=500;
    let proc:any={sessionId:stateManager.getSession('owner:main')!.sessionId,state:'ready',process:{pid}};
    return {get:vi.fn(()=>proc&&{...proc}),kill:vi.fn(async()=>{proc=undefined;}),
      spawn:vi.fn(async(l:any)=>(proc={sessionId:l.sessionId,state:'ready',process:{pid:++pid}})),
      sendIdempotent:vi.fn(async(_k:string,_m:unknown,_key:string,o?:any)=>{o?.onAccepted?.();}),
      send:vi.fn(async(_k:string,_m:unknown,o?:any)=>{o?.onAccepted?.();})} as any;
  }
  const control=vi.fn(async(request:any)=>request.phase==='readback'
    ? {schema:'pulse.wi_switch/v1',phase:'readback',wi:'wi_b',committed:true}
    : {status:'local_committed',work_item:{id:'wi_b'}});

  it.each([['entered','entered' as const],['not yet entered',undefined]])(
    '/reset drops the record (%s) with an audit line; the pass then neither hands over nor replaces the new session',
    async (_name,phase) => {
    const router=setupRouter();
    const stateManager=conversation(phase);
    const log=vi.spyOn(console,'error').mockImplementation(()=>{});
    try {
      await handleReset('owner','main',{router,runtime:createMockPool(),stateManager});
      const reset=stateManager.getSession('owner:main')!;
      expect(reset.pendingWiSwitch).toBeUndefined();
      expect(reset.sessionId).not.toMatch(/^sess-/);
      expect(log).toHaveBeenCalledWith(expect.objectContaining({msg:'WI switch record abandoned',reason:'/reset',ref,
        target:'wi_b',phase:phase??'entering'}));
      const runtime=live(stateManager);
      control.mockClear();
      expect(await settleWiSwitch({poolKey:'owner:main',runtime,stateManager:stateManager as any,
        acquire:async()=>'owner:main',reply:vi.fn(async()=>{}),control})).toBe(false);
      expect(runtime.sendIdempotent).not.toHaveBeenCalled();
      expect(runtime.kill).not.toHaveBeenCalled();
      expect(control).not.toHaveBeenCalled();
      expect(stateManager.getSession('owner:main')!.sessionId).toBe(reset.sessionId);
    } finally {log.mockRestore();}
  });

  it('/new drops the record too', async () => {
    const stateManager=conversation('entered');
    const log=vi.spyOn(console,'error').mockImplementation(()=>{});
    try {
      await handleNew('owner','main',{router:setupRouter(),runtime:createMockPool(),stateManager});
      expect(stateManager.getSession('owner:main')!.pendingWiSwitch).toBeUndefined();
      expect(log).toHaveBeenCalledWith(expect.objectContaining({msg:'WI switch record abandoned',reason:'/new'}));
    } finally {log.mockRestore();}
  });

  it.each([['entered','entered' as const],['not yet entered',undefined]])(
    'a record left on a session it does not name (%s) is abandoned, the current session untouched and the notice retired',
    async (_name,phase) => {
    // Any other replacement (a rollover, an older state) leaves the record naming other sessions.
    const stateManager=conversation(phase);
    stateManager.updateSession('owner:main',{sessionId:'sess-other'});
    const runtime=live(stateManager);
    const reply=vi.fn(async()=>{});
    const log=vi.spyOn(console,'error').mockImplementation(()=>{});
    const dir=join(testDir,'agent-switches-'+(phase??'entering'));
    mkdirSync(dir,{recursive:true});
    const notice=join(dir,ref+'.json');
    writeFileSync(notice,JSON.stringify({schema:'harness.agent-switch-request/v1',ref,wi:'wi_a',target:'wi_b',
      request_id:'ask',tenant:'owner:main',source:{},source_native_id:'sess-a'}),{mode:0o600});
    try {
      control.mockClear();
      expect(await settleWiSwitch({poolKey:'owner:main',runtime,stateManager:stateManager as any,
        acquire:async()=>'owner:main',reply,control})).toBe(true);
      expect(reply).toHaveBeenCalledWith(expect.stringContaining('未完成记录已作废'));
      expect(runtime.sendIdempotent).not.toHaveBeenCalled();
      expect(runtime.kill).not.toHaveBeenCalled();
      expect(control).not.toHaveBeenCalled();
      expect(stateManager.getSession('owner:main')).toMatchObject({sessionId:'sess-other',pendingWiSwitch:undefined});
      expect(log).toHaveBeenCalledWith(expect.objectContaining({msg:'WI switch record abandoned',reason:'session_replaced',
        currentSessionId:'sess-other'}));
      const start=vi.fn();
      await adoptAgentSwitchRequests({dir,stateManager:stateManager as any,runtime,start});
      expect(start).not.toHaveBeenCalled();
      expect(()=>readFileSync(notice)).toThrow();
    } finally {log.mockRestore();}
  });

  it('/reset and /new wait while a switch is replacing the session', async () => {
    const {switchWorkItem}=await import('../src/native-wi-switch.js');
    const stateManager=createStateManager({'owner:main':{poolKey:'owner:main',agentId:'main',runtime:'cc',
      sessionId:'sess-a',lastActivity:Date.now(),status:'active',turnCount:1,maxTurns:20} as never});
    const runtime=live(stateManager);
    let release:()=>void=()=>{};
    const leaving=new Promise<void>(resolve=>{release=resolve;});
    const switching=switchWorkItem({poolKey:'owner:main',target:'wi_b',ref,runtime,stateManager:stateManager as any,
      acquire:async()=>'owner:main',control:async()=>{await leaving;return {status:'rejected',error:{code:'domain_conflict'}};}});
    const router=setupRouter();
    expect((await handleReset('owner','main',{router,runtime:createMockPool(),stateManager})).text).toContain('完成后再 /reset');
    expect((await handleNew('owner','main',{router,runtime:createMockPool(),stateManager})).text).toContain('完成后再 /new');
    expect(stateManager.getSession('owner:main')!.sessionId).toBe('sess-a');
    release();
    await switching;
    expect((await handleReset('owner','main',{router,runtime:createMockPool(),stateManager})).text).toContain('已强制重置');
  });
});

describe('/think on every conversation runtime', () => {
  const savedAgyBin = process.env.PULSE_AGY_BIN;
  beforeEach(() => {
    mkdirSync(testDir, { recursive: true });
    const bin = join(testDir, 'agy');
    writeFileSync(bin, '#!/bin/sh\n', { mode: 0o755 });
    process.env.PULSE_AGY_BIN = bin;
  });
  afterEach(() => {
    if (savedAgyBin === undefined) delete process.env.PULSE_AGY_BIN;
    else process.env.PULSE_AGY_BIN = savedAgyBin;
  });

  function session(runtime: PersistedSession['runtime'], extra: Partial<PersistedSession> = {}): PersistedSession {
    return { poolKey: 'owner:main', agentId: 'main', runtime, sessionId: 'relay-session',
      turnCount: 1, maxTurns: 20, lastActivity: Date.now(), status: 'active', ...extra };
  }
  function live(state: 'idle' | 'busy') {
    return createMockPool({ 'owner:main': { tenantId: 'owner:main', state, pid: 1, lastActivity: Date.now(), spawnedAt: Date.now(), restartCount: 0 } });
  }
  function thinkButtons(card: Awaited<ReturnType<typeof handleThink>>): string[] {
    return (card.feishuCardElements ?? []).flatMap((element) => Array.isArray(element.columns)
      ? (element.columns as Array<{ elements: Array<{ behaviors: Array<{ value: Record<string, string> }> }> }>)
        .map((column) => column.elements[0]!.behaviors[0]!.value)
        .filter((value) => value.relay_menu === 'think').map((value) => value.relay_menu_arg!)
      : []);
  }

  it.each([['cc'], ['cc-sdk']] as const)('%s maps the level to effort and restarts the idle process, keeping the session', async (runtimeId) => {
    const router = setupRouter();
    router.setAgentRuntime('owner', 'main', runtimeId);
    const pool = live('idle');
    const kill = vi.fn(async () => undefined);
    const setThinkingLevel = vi.fn(async () => undefined);
    Object.assign(pool, { kill, setThinkingLevel });
    const stateManager = createStateManager({ 'owner:main': session(runtimeId) });
    const deps = { router, runtime: pool, stateManager };

    const card = await handleThink('owner', 'main', undefined, deps);
    expect(card.text).toContain('effort');
    expect(card.text).toContain('当前: 默认');
    expect(thinkButtons(card)).toEqual(['low', 'medium', 'high', 'xhigh', 'max']);

    const set = await handleThink('owner', 'main', 'max', deps);
    expect(set.text).toContain('effort → max');
    expect(set.text).toContain('下一轮生效');
    expect(stateManager.getSession('owner:main')?.reasoningLevel).toBe('max');
    expect(stateManager.getSession('owner:main')?.sessionId).toBe('relay-session');
    expect(kill).toHaveBeenCalledWith('owner:main');
    expect(setThinkingLevel).not.toHaveBeenCalled();
    expect(thinkButtons(await handleThink('owner', 'main', undefined, deps))).toContain('max');

    const busy = live('busy');
    Object.assign(busy, { kill });
    kill.mockClear();
    const refused = await handleThink('owner', 'main', 'low', { router, runtime: busy, stateManager });
    expect(refused.text).toContain('正在工作');
    expect(kill).not.toHaveBeenCalled();
    expect(stateManager.getSession('owner:main')?.reasoningLevel).toBe('max');
    expect((await handleThink('owner', 'main', 'off', deps)).text).toContain('不支持 thinking=off');
  });

  describe('default line from the transcript of the last turn', () => {
    const turn = (effort: string) => JSON.stringify({ type: 'assistant', requestedModel: 'claude-opus-5-5', effort, perTurnEffort: effort,
      message: { role: 'assistant', model: 'claude-opus-5-5', content: [] } });
    /** Write the session transcript where CC puts it: <config dir>/projects/<cwd encoded>/<session>.jsonl. */
    function writeTranscript(configDir: string, workspace: string, lines: string[]): void {
      const dir = join(configDir, 'projects', realpathSync(workspace).replace(/[^A-Za-z0-9_-]/g, '-'));
      mkdirSync(dir, { recursive: true });
      writeFileSync(join(dir, 'relay-session.jsonl'), lines.join('\n') + '\n');
    }
    function setup(agentExtra: Record<string, unknown> = {}, runtime = 'cc') {
      const workspace = join(testDir, `ws-${runtime}`);
      mkdirSync(workspace, { recursive: true });
      const path = join(testDir, `relay-think-${runtime}.json`);
      writeFileSync(path, JSON.stringify({ users: [{ userId: 'owner', chatId: '111222333', defaultAgent: 'main', agents: [
        { id: 'main', name: 'Vigil', emoji: 'V', botToken: 'fake-v', workspace, runtime, ...agentExtra },
      ] }] }));
      const router = createRouter(path);
      const stateManager = createStateManager({ 'owner:main': session(runtime as PersistedSession['runtime']) });
      return { workspace, deps: { router, runtime: createMockPool(), stateManager } };
    }
    let home: string;
    beforeEach(() => {
      home = join(testDir, 'think-home');
      mkdirSync(home, { recursive: true });
      vi.stubEnv('HOME', home);
      vi.stubEnv('CLAUDE_CONFIG_DIR', '');
    });
    afterEach(() => { vi.unstubAllEnvs(); });

    it.each([['cc'], ['cc-sdk']] as const)('%s shows the effort the last turn actually ran at', async (runtime) => {
      const { workspace, deps } = setup({}, runtime);
      writeTranscript(join(home, '.claude'), workspace, [turn('high'), turn('medium'), JSON.stringify({ type: 'user', message: {} })]);
      const card = await handleThink('owner', 'main', undefined, deps);
      expect(card.text).toContain('当前: 默认（上一轮实际 medium，模型 claude-opus-5-5）');
      expect(card.text).toContain('/new 后回到默认。');
    });

    it('names no level before the first reply or when records carry no effort', async () => {
      const { workspace, deps } = setup();
      expect((await handleThink('owner', 'main', undefined, deps)).text)
        .toContain('当前: 默认（由 Claude Code 决定，首轮回复后可见实际档位）');
      writeTranscript(join(home, '.claude'), workspace, [JSON.stringify({ type: 'assistant', message: { model: 'claude-opus-5-5' } })]);
      expect((await handleThink('owner', 'main', undefined, deps)).text)
        .toContain('当前: 默认（由 Claude Code 决定，首轮回复后可见实际档位）');
    });

    it('reads the agent HOME, and CLAUDE_CONFIG_DIR ahead of it', async () => {
      const agentHome = join(testDir, 'think-agent-home');
      const { workspace, deps } = setup({ userHome: agentHome });
      writeTranscript(join(home, '.claude'), workspace, [turn('max')]);
      writeTranscript(join(agentHome, '.claude'), workspace, [turn('low')]);
      expect((await handleThink('owner', 'main', undefined, deps)).text).toContain('上一轮实际 low');
      const configDir = join(testDir, 'think-config-dir');
      writeTranscript(configDir, workspace, [turn('xhigh')]);
      vi.stubEnv('CLAUDE_CONFIG_DIR', configDir);
      expect((await handleThink('owner', 'main', undefined, deps)).text).toContain('上一轮实际 xhigh');
    });

    it('reads a linuxUser tenant under its workspace HOME, where sudo drops CLAUDE_CONFIG_DIR', async () => {
      const { workspace, deps } = setup({ linuxUser: 'tenant' });
      writeTranscript(join(realpathSync(workspace), '.claude'), workspace, [turn('medium')]);
      const configDir = join(testDir, 'think-relay-config-dir');
      writeTranscript(configDir, workspace, [turn('max')]);
      vi.stubEnv('CLAUDE_CONFIG_DIR', configDir);
      expect((await handleThink('owner', 'main', undefined, deps)).text).toContain('当前: 默认（上一轮实际 medium，模型 claude-opus-5-5）');
    });
  });

  it('shows the effort Relay passes Codex, and promises no level for /new', async () => {
    vi.stubEnv('PULSE_CODEX_REASONING_EFFORT', '');
    vi.stubEnv('RELAY_CODEX_REASONING_EFFORT', '');
    vi.stubEnv('PULSE_RUNTIME_PRIORITY_PATH', join(testDir, 'missing-runtime-priority.json'));
    try {
      // The Agent prefers cc (sampleConfig sets no runtime); this session fell back to Codex.
      const router = setupRouter();
      const stateManager = createStateManager({ 'owner:main': session('codex') });
      const card = await handleThink('owner', 'main', undefined, { router, runtime: createMockPool(), stateManager });
      expect(card.text).toContain('Codex reasoning effort');
      expect(card.text).toContain('当前: 默认（xhigh，来自 Relay 内置默认）');
      expect(card.text).toContain('/new 后回到默认。');
    } finally {
      vi.unstubAllEnvs();
    }
  });

  it('codex maps the level to reasoning effort on the live process for the next turn', async () => {
    const router = setupRouter();
    router.setAgentRuntime('owner', 'main', 'codex');
    const pool = live('busy');
    const kill = vi.fn(async () => undefined);
    const setThinkingLevel = vi.fn(async () => undefined);
    Object.assign(pool, { kill, setThinkingLevel });
    const stateManager = createStateManager({ 'owner:main': session('codex') });
    const deps = { router, runtime: pool, stateManager };

    expect(thinkButtons(await handleThink('owner', 'main', undefined, deps))).toEqual(['low', 'medium', 'high', 'xhigh']);
    const set = await handleThink('owner', 'main', 'xhigh', deps);
    expect(set.text).toContain('reasoning effort → xhigh');
    expect(setThinkingLevel).toHaveBeenCalledWith('owner:main', 'xhigh');
    expect(kill).not.toHaveBeenCalled();
    expect(stateManager.getSession('owner:main')?.reasoningLevel).toBe('xhigh');
    expect((await handleThink('owner', 'main', 'max', deps)).text).toContain('codex 不支持 thinking=max');
  });

  it('agy maps the level to the model family variant, and refuses a level the family lacks', async () => {
    const router = setupRouter();
    router.setAgentRuntime('owner', 'main', 'agy');
    const pool = live('idle');
    const setThinkingLevel = vi.fn(async () => undefined);
    const setModel = vi.fn(async () => undefined);
    Object.assign(pool, { setThinkingLevel, setModel });
    const stateManager = createStateManager({ 'owner:main': session('agy') });
    const deps = { router, runtime: pool, stateManager };

    const card = await handleThink('owner', 'main', undefined, deps);
    expect(card.text).toContain('当前: high');
    expect(thinkButtons(card)).toEqual(['low', 'medium', 'high', 'xhigh']);
    const set = await handleThink('owner', 'main', 'low', deps);
    expect(set.text).toContain('模型变体 → low（gemini-4-neon-low）');
    expect(setThinkingLevel).toHaveBeenCalledWith('owner:main', 'low');
    expect(stateManager.getSession('owner:main')?.reasoningLevel).toBe('low');
    // The /model card follows the session level.
    const modelCard = JSON.stringify((await handleModel('owner', 'main', undefined, deps)).feishuCardElements);
    expect(modelCard).toContain('gemini-4-argon-low');

    await handleModel('owner', 'main', 'gemini-3.8-flash-high', deps);
    expect(stateManager.getSession('owner:main')?.reasoningLevel).toBeUndefined();
    const refused = await handleThink('owner', 'main', 'xhigh', deps);
    expect(refused.text).toContain('agy 模型 gemini-3.8-flash-high 没有 xhigh 档位；可用: low, medium, high');

    await handleModel('owner', 'main', 'gemini-pro-orchestrator-neon', deps);
    const none = await handleThink('owner', 'main', 'low', deps);
    expect(none.text).toContain('没有可选的思考档位');
  });

  it('asks for a conversation before storing a level', async () => {
    const router = setupRouter();
    router.setAgentRuntime('owner', 'main', 'codex');
    const stateManager = createStateManager();
    const result = await handleThink('owner', 'main', 'high', { router, runtime: createMockPool(), stateManager });
    expect(result.text).toContain('还没有会话');
    expect(stateManager.getSession('owner:main')).toBeUndefined();
  });

  it.each([['/new', handleNew], ['/reset', handleReset]] as const)('%s starts the next launch at the runtime default level', async (_name, replace) => {
    const router = setupRouter();
    router.setAgentRuntime('owner', 'main', 'cc');
    const pool = live('idle');
    Object.assign(pool, { kill: vi.fn(async () => undefined) });
    const stateManager = createRealStateManager(join(testDir, 'state.json'));
    stateManager.load();
    stateManager.updateSession('owner:main', session('cc'));
    const deps = { router, runtime: pool, stateManager };
    expect((await handleThink('owner', 'main', 'low', deps)).text).toContain('effort → low');
    const agent = router.getActiveAgent('owner')!;
    const launch = () => conversationLaunch({ poolKey: 'owner:main', agent, session: stateManager.getSession('owner:main'),
      channel: 'telegram', intent: 'inbound' });
    expect(launch().reasoningLevel).toBe('low');

    await replace('owner', 'main', deps);
    expect(stateManager.getSession('owner:main')?.sessionId).not.toBe('relay-session');
    expect(launch().reasoningLevel).toBeUndefined();
  });
});

describe('agy runtime commands', () => {
  const savedAgyBin = process.env.PULSE_AGY_BIN;
  beforeEach(() => {
    mkdirSync(testDir, { recursive: true });
    const bin = join(testDir, 'agy');
    writeFileSync(bin, '#!/bin/sh\n', { mode: 0o755 });
    process.env.PULSE_AGY_BIN = bin;
  });
  afterEach(() => {
    if (savedAgyBin === undefined) delete process.env.PULSE_AGY_BIN;
    else process.env.PULSE_AGY_BIN = savedAgyBin;
  });

  function agySession(extra: Partial<PersistedSession> = {}): PersistedSession {
    return { poolKey: 'owner:main', agentId: 'main', runtime: 'agy', sessionId: 'relay-session',
      turnCount: 1, maxTurns: 20, lastActivity: Date.now(), status: 'active', ...extra };
  }

  it('switches to agy only where the launcher exists', async () => {
    const router = setupRouter();
    const pool = createMockPool();
    const stateManager = createStateManager();
    const switched = await handleRuntime('owner', 'main', 'agy', { router, runtime: pool, stateManager });
    expect(switched.text).toContain('runtime 已切换为 agy');
    expect(readConfig().users[0].agents[0].conversationRuntimePreference).toEqual(['agy']);

    process.env.PULSE_AGY_BIN = join(testDir, 'missing-agy');
    const router2 = setupRouter();
    const refused = await handleRuntime('owner', 'main', 'agy', { router: router2, runtime: pool, stateManager });
    expect(refused.text).toContain('未安装 agy');
  });

  it('refuses agy at /runtime and /model for an Agent isolated under its own Linux user', async () => {
    const config = JSON.parse(JSON.stringify(sampleConfig));
    config.users[0].agents[0].linuxUser = 'agent-iso';
    writeFileSync(join(testDir, 'relay.json'), JSON.stringify(config));
    const router = createRouter(join(testDir, 'relay.json'));
    const pool = createMockPool();
    const stateManager = createStateManager();
    const refused = await handleRuntime('owner', 'main', 'agy', { router, runtime: pool, stateManager });
    expect(refused.text).toContain('独立 Linux 用户隔离');
    expect(readConfig().users[0].agents[0].conversationRuntimePreference)
      .toEqual(config.users[0].agents[0].conversationRuntimePreference);
    const agent = router.getAgents('owner').find((a) => a.id === 'main')!;
    expect(JSON.stringify(buildRuntimeOptionCard(agent, { router }))).not.toContain('agy');

    // An Agent already on agy (configured before this guard) cannot pick or keep an agy model either.
    router.setAgentRuntime('owner', 'main', 'agy');
    const model = await handleModel('owner', 'main', 'gemini-4-neon-medium', { router, runtime: pool, stateManager });
    expect(model.text).toContain('独立 Linux 用户隔离');
    expect(router.getAgentModel('owner', 'main')).not.toBe('gemini-4-neon-medium');
  });

  it('drops an agy model when the agent leaves agy', async () => {
    const router = setupRouter();
    router.setAgentRuntime('owner', 'main', 'agy');
    router.setAgentModel('owner', 'main', 'gemini-4-neon-high');
    const result = await handleRuntime('owner', 'main', 'cc', { router, runtime: createMockPool(), stateManager: createStateManager() });
    expect(result.text).toContain('agy 专用模型已清除');
    expect(router.getAgentModel('owner', 'main')).toBeUndefined();
  });

  it('lists every measured agy model family on the /model card itself, the current one marked', async () => {
    const router = setupRouter();
    router.setAgentRuntime('owner', 'main', 'agy');
    const deps = { router, runtime: createMockPool() };
    const card = await handleModel('owner', 'main', undefined, deps);
    const buttons = (card.feishuCardElements ?? []).flatMap((element) => Array.isArray(element.columns)
      ? (element.columns as Array<{ elements: Array<{ text: { content: string }; behaviors: Array<{ value: Record<string, string> }> }> }>)
        .map((column) => ({ label: column.elements[0]!.text.content, ...column.elements[0]!.behaviors[0]!.value }))
      : []);
    const models = buttons.filter((button) => button.relay_menu === 'model');
    expect(models.map((button) => button.label)).toEqual(['✓ Gemini 4 Neon', 'Gemini 4 Argon', 'Gemini 4 Hydrogen',
      'Orchestrator (Neon)', 'Orchestrator (Argon)', 'Gemini 3.8 Flash', 'Claude Opus 5.5', 'Claude Sonnet 5.5', 'GPT-OSS 120B']);
    // A family is picked at the current level (high) when it has it, else at its only variant.
    expect(models.map((button) => button.relay_menu_arg)).toEqual(['gemini-4-neon-high', 'gemini-4-argon-high',
      'gemini-4-hydrogen-high', 'gemini-pro-orchestrator-neon', 'gemini-pro-orchestrator-g1-pro', 'gemini-3.8-flash-high',
      'claude-opus-5-5-high', 'claude-sonnet-5-5-high', 'gpt-oss-120b-medium']);
    expect(buttons.some((button) => button.relay_menu === 'model_scope')).toBe(false);
    // An older card's scope button lands on the same card.
    const scoped = await handleModelScope('owner', 'main', 'agy|subscription', deps);
    expect(JSON.stringify(scoped.feishuCardElements)).toContain('gemini-pro-orchestrator-g1-pro');
    const listed = await handleModels('owner', 'main', deps, 'claude');
    expect(listed.text).toContain('agy/claude-opus-5-5-low');
    expect(listed.text).toContain('匹配: 6');
  });

  it('selects an agy model in place, keeping the conversation', async () => {
    const router = setupRouter();
    router.setAgentRuntime('owner', 'main', 'agy');
    const pool = createMockPool({
      'owner:main': { tenantId: 'owner:main', state: 'idle', pid: 1, lastActivity: Date.now(), spawnedAt: Date.now(), restartCount: 0 },
    });
    const setModel = vi.fn(async () => undefined);
    const kill = vi.fn(async () => undefined);
    Object.assign(pool, { setModel, kill });
    const stateManager = createStateManager({ 'owner:main': agySession() });

    const result = await handleModel('owner', 'main', 'gemini-4-neon-medium', { router, runtime: pool, stateManager });
    expect(result.text).toContain('agy model → gemini-4-neon-medium');
    expect(setModel).toHaveBeenCalledWith('owner:main', { provider: 'agy', model: 'gemini-4-neon-medium', reasoning: 'medium' });
    expect(kill).not.toHaveBeenCalled();
    expect(router.getAgentModel('owner', 'main')).toBe('gemini-4-neon-medium');

    const rejected = await handleModel('owner', 'main', 'claude-opus-5-5', { router, runtime: pool, stateManager });
    expect(rejected.text).toContain('agy 不提供该模型');
    expect(router.getAgentModel('owner', 'main')).toBe('gemini-4-neon-medium');

    const reset = await handleModel('owner', 'main', 'default', { router, runtime: pool, stateManager });
    expect(reset.text).toContain('gemini-4-neon-high（默认）');
  });

  it('shows the agy conversation id bound to this Relay session', () => {
    const router = setupRouter();
    router.setAgentRuntime('owner', 'main', 'agy');
    const stateManager = createStateManager({ 'owner:main': agySession({
      agyConversation: { sessionId: 'relay-session', conversationId: 'conv-123' } }) });
    const result = handleSession('owner', 'main', { router, runtime: createMockPool(), stateManager });
    expect(result.text).toContain('runtime=agy');
    expect(result.text).toContain('conversationId=conv-123');
  });
});
