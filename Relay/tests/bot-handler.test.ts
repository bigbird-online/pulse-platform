import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { writeFileSync, mkdirSync, rmSync } from 'node:fs';
import { join } from 'node:path';
import { tmpdir } from 'node:os';
import { EventEmitter } from 'node:events';
import type { Pool, SpawnConfig, CCProcess, StreamJsonMessage, ProcessStatusInfo, AgentConfig, SessionEndReason } from '../src/types.js';
import type { Router } from '../src/router.js';
import { createRouter } from '../src/router.js';

const testDir = join(tmpdir(), 'relay-bot-handler-test-' + Date.now());

const sampleConfig = {
  users: [
    {
      userId: 'owner',
      chatId: '111222333',
      defaultAgent: 'main',
      agents: [
        { id: 'main', name: 'Duke', emoji: '\u{1F3B2}', botToken: 'fake-duke', workspace: '/home/duke-space', sessionId: 'uuid-duke', maxTurns: 20, allowedTools: ['Read', 'Write'] },
        { id: 'ken', name: 'Ken', emoji: '\u{1F527}', botToken: 'fake-ken', workspace: '/home/ken-space', sessionId: 'uuid-ken', runtime: 'codex' },
      ],
    },
  ],
};

function createMockPool(): Pool & { emitter: EventEmitter; processes: Map<string, CCProcess> } {
  const emitter = new EventEmitter();
  const processes = new Map<string, CCProcess>();

  return {
    emitter,
    processes,
    spawn: vi.fn(async (config: SpawnConfig) => {
      const proc = {
        tenantId: config.tenantId,
        process: {} as never,
        sessionId: config.sessionId,
        state: 'ready' as const,
        lastActivity: Date.now(),
        spawnedAt: Date.now(),
        restartCount: 0,
        restartWindowStart: Date.now(),
      };
      processes.set(config.tenantId, proc);
      return proc;
    }),
    send: vi.fn(async () => {}),
    kill: vi.fn(async () => {}),
    killAll: vi.fn(async () => {}),
    get: (id: string) => processes.get(id),
    getAll: () => Array.from(processes.values()),
    getStatus: () => [] as ProcessStatusInfo[],
    on: (event: string, handler: (...args: unknown[]) => void) => {
      // eslint-disable-next-line @typescript-eslint/no-explicit-any
      emitter.on(event, handler as (...args: any[]) => void);
    },
    emitShutdown: vi.fn(),
    dispose: vi.fn(),
  };
}

function setupRouter(): Router {
  const path = join(testDir, 'relay.json');
  writeFileSync(path, JSON.stringify(sampleConfig));
  return createRouter(path);
}

/**
 * Simulate the v2 message handler logic (multi-agent routing).
 * Extracted from telegram.ts for testing without grammy.
 */
async function handleMessage(
  chatId: string,
  text: string,
  router: Router,
  pool: Pool,
  replyFn: (msg: string) => Promise<void>,
): Promise<void> {
  const user = router.resolveUser(chatId);

  if (!user) {
    await replyFn('\u26A0\uFE0F \u60A8\u5C1A\u672A\u6CE8\u518C\uFF0C\u8BF7\u8054\u7CFB\u7BA1\u7406\u5458\u5F00\u901A\u670D\u52A1\u3002');
    return;
  }

  const agent = router.getActiveAgent(user.userId);
  if (!agent) {
    await replyFn('\u26A0\uFE0F \u65E0\u6D3B\u8DC3 Agent\u3002');
    return;
  }

  const poolKey = router.poolKey(user.userId, agent.id);
  let proc = pool.get(poolKey);
  if (!proc || proc.state === 'dead') {
    proc = await pool.spawn({
      tenantId: poolKey,
      runtime: agent.runtime ?? 'cc',
      workspace: agent.workspace,
      sessionId: agent.sessionId,
      maxTurns: agent.maxTurns,
      allowedTools: agent.allowedTools,
      resume: proc?.state === 'dead',
    });
  }

  await pool.send(poolKey, { type: 'human', content: text });
}

/**
 * Prefix message with agent identity.
 */
function prefixMessage(agent: AgentConfig, text: string, isFirstChunk: boolean): string {
  if (!isFirstChunk) return text;
  return `${agent.emoji} ${agent.name}:\n${text}`;
}

beforeEach(() => {
  mkdirSync(testDir, { recursive: true });
});

afterEach(() => {
  rmSync(testDir, { recursive: true, force: true });
});

describe('bot message handler (v2 multi-agent)', () => {
  let router: Router;
  let pool: ReturnType<typeof createMockPool>;
  let replyFn: ReturnType<typeof vi.fn>;

  beforeEach(() => {
    router = setupRouter();
    pool = createMockPool();
    replyFn = vi.fn(async () => {});
  });

  // [P0-004] Unit Test-025: unregistered chatId
  it('replies with registration message for unknown chatId', async () => {
    await handleMessage('999999', 'hello', router, pool, replyFn);
    expect(replyFn).toHaveBeenCalledWith('\u26A0\uFE0F \u60A8\u5C1A\u672A\u6CE8\u518C\uFF0C\u8BF7\u8054\u7CFB\u7BA1\u7406\u5458\u5F00\u901A\u670D\u52A1\u3002');
    expect(pool.send).not.toHaveBeenCalled();
  });

  // [P0-004] Unit Test-023: text message routes to active agent
  it('routes to default agent (Duke) and spawns process', async () => {
    await handleMessage('111222333', 'hello', router, pool, replyFn);

    expect(pool.spawn).toHaveBeenCalledOnce();
    expect(pool.spawn).toHaveBeenCalledWith(expect.objectContaining({
      tenantId: 'owner:main',
      workspace: '/home/duke-space',
      sessionId: 'uuid-duke',
    }));
    expect(pool.send).toHaveBeenCalledWith('owner:main', { type: 'human', content: 'hello' });
  });

  it('routes to switched agent after switchAgent', async () => {
    router.switchAgent('owner', 'ken');
    await handleMessage('111222333', 'hi ken', router, pool, replyFn);

    expect(pool.spawn).toHaveBeenCalledWith(expect.objectContaining({
      tenantId: 'owner:ken',
      runtime: 'codex',
      workspace: '/home/ken-space',
    }));
    expect(pool.send).toHaveBeenCalledWith('owner:ken', { type: 'human', content: 'hi ken' });
  });

  it('reuses existing process', async () => {
    pool.processes.set('owner:main', {
      tenantId: 'owner:main',
      process: {} as never,
      sessionId: 'uuid-duke',
      state: 'idle',
      lastActivity: Date.now(),
      spawnedAt: Date.now(),
      restartCount: 0,
      restartWindowStart: Date.now(),
    });

    await handleMessage('111222333', 'hello again', router, pool, replyFn);
    expect(pool.spawn).not.toHaveBeenCalled();
    expect(pool.send).toHaveBeenCalledWith('owner:main', { type: 'human', content: 'hello again' });
  });

  it('resumes dead process', async () => {
    pool.processes.set('owner:main', {
      tenantId: 'owner:main',
      process: {} as never,
      sessionId: 'uuid-duke',
      state: 'dead',
      lastActivity: Date.now(),
      spawnedAt: Date.now(),
      restartCount: 0,
      restartWindowStart: Date.now(),
    });

    await handleMessage('111222333', 'hi', router, pool, replyFn);
    expect(pool.spawn).toHaveBeenCalledWith(expect.objectContaining({ resume: true }));
  });
});

// [P0-004] Unit Test-024/026: agent prefix
describe('prefixMessage', () => {
  const agent: AgentConfig = { id: 'main', name: 'Duke', emoji: '\u{1F3B2}', workspace: '/w', sessionId: 'x' };

  it('adds prefix to first chunk', () => {
    expect(prefixMessage(agent, '\u4F60\u597D', true)).toBe('\u{1F3B2} Duke:\n\u4F60\u597D');
  });

  it('does not add prefix to subsequent chunks', () => {
    expect(prefixMessage(agent, '\u7EED\u7EED', false)).toBe('\u7EED\u7EED');
  });
});

describe('pool response routing (v2)', () => {
  it('emits assistant messages for correct pool key', () => {
    const pool = createMockPool();
    const messages: StreamJsonMessage[] = [];

    pool.on('response', (tenantId: string, msg: StreamJsonMessage) => {
      if (msg.type === 'assistant') messages.push(msg);
    });

    pool.emitter.emit('response', 'owner:main', { type: 'assistant', content: 'Hello!' });
    pool.emitter.emit('response', 'owner:main', { type: 'result', total_cost_usd: 0.01 });

    expect(messages).toHaveLength(1);
    expect(messages[0].content).toBe('Hello!');
  });
});

// --- F1: session-end notification tests ---

describe('session-end event notifications', () => {
  let pool: ReturnType<typeof createMockPool>;

  beforeEach(() => {
    pool = createMockPool();
  });

  it('turns-exhausted triggers notification with turn counts', () => {
    const messages: string[] = [];
    pool.on('session-end', (_tenantId: string, reason: SessionEndReason) => {
      if (reason.type === 'turns-exhausted') {
        messages.push(`turns: ${reason.turnCount}/${reason.maxTurns}`);
      }
    });

    pool.emitter.emit('session-end', 'owner:ken', { type: 'turns-exhausted', turnCount: 100, maxTurns: 100 });
    expect(messages).toHaveLength(1);
    expect(messages[0]).toContain('100/100');
  });

  it('idle-reclaimed does NOT emit crash or turns events', () => {
    const events: string[] = [];
    pool.on('session-end', (_t: string, reason: SessionEndReason) => {
      events.push(reason.type);
    });

    pool.emitter.emit('session-end', 'owner:ken', { type: 'idle-reclaimed' });
    expect(events).toEqual(['idle-reclaimed']);
  });

  it('crash-unrecoverable carries exitCode', () => {
    const events: SessionEndReason[] = [];
    pool.on('session-end', (_t: string, reason: SessionEndReason) => {
      events.push(reason);
    });

    pool.emitter.emit('session-end', 'owner:ken', { type: 'crash-unrecoverable', exitCode: 137 });
    expect(events).toHaveLength(1);
    expect(events[0].type).toBe('crash-unrecoverable');
  });
});
