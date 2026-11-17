import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { writeFileSync, mkdirSync, rmSync } from 'node:fs';
import { join } from 'node:path';
import { tmpdir } from 'node:os';
import { createRouter } from '../src/router.js';
import { validateConfig } from '../src/types.js';
import type { RelayConfig, AgentConfig } from '../src/types.js';

const testDir = join(tmpdir(), 'relay-feishu-integration-' + Date.now());

// --- Integration: validateConfig + router end-to-end ---

const mixedConfig: RelayConfig = {
  users: [
    {
      userId: 'owner',
      chatId: '111222333',
      defaultAgent: 'duke',
      agents: [{
        id: 'duke', name: 'Duke', emoji: '\u{1F3B2}',
        botToken: 'fake-duke-token', workspace: '/tmp/ws', sessionId: 'uuid-duke',
      }],
    },
    {
      userId: 'eric',
      feishuOpenId: 'ou_eric123',
      defaultAgent: 'pulse',
      agents: [{
        id: 'pulse', name: 'Pulse', emoji: '\u{1F52E}',
        channel: 'feishu',
        workspace: '/tmp/ws2',
        feishu: { appId: 'cli_xxx', appSecret: 'secret', sessionId: 'uuid-pulse' },
        maxTurns: 500,
        allowedTools: ['Read', 'Write', 'Edit', 'Glob', 'Grep', 'Bash'],
      }],
    },
    {
      userId: 'kuoran',
      feishuOpenId: 'ou_kuoran456',
      defaultAgent: 'pulse',
      agents: [{
        id: 'pulse', name: 'Pulse', emoji: '\u{1F52E}',
        channel: 'feishu',
        workspace: '/tmp/ws3',
        feishu: { appId: 'cli_xxx', appSecret: 'secret', sessionId: 'uuid-pulse-kr' },
      }],
    },
  ],
};

function writeConfig(config: object = mixedConfig): string {
  const path = join(testDir, 'relay.json');
  writeFileSync(path, JSON.stringify(config));
  return path;
}

beforeEach(() => {
  mkdirSync(testDir, { recursive: true });
});

afterEach(() => {
  rmSync(testDir, { recursive: true, force: true });
});

describe('feishu integration: validateConfig + router', () => {
  it('valid mixed config passes validation and creates router', () => {
    expect(() => validateConfig(mixedConfig)).not.toThrow();
    const router = createRouter(writeConfig());
    const tgBots = router.getAllAgentBots();
    const feishuBots = router.getAllFeishuBots();
    expect(tgBots.length).toBe(1);
    expect(feishuBots.length).toBe(1);
    expect(feishuBots[0].authorizedOpenIds).toEqual(['ou_eric123', 'ou_kuoran456']);
  });

  it('feishu agent resolved via openId has correct sessionId', () => {
    const router = createRouter(writeConfig());
    const resolved = router.resolveByFeishuAgent('pulse', 'ou_eric123');
    expect(resolved).toBeDefined();
    expect(resolved!.agent.feishu!.sessionId).toBe('uuid-pulse');
  });

  it('spawn config uses feishu sessionId over tg sessionId', () => {
    const router = createRouter(writeConfig());
    const resolved = router.resolveByFeishuAgent('pulse', 'ou_eric123')!;
    const agent = resolved.agent;
    const sessionId = agent.feishu?.sessionId ?? agent.sessionId;
    expect(sessionId).toBe('uuid-pulse');
    expect(sessionId).not.toBe(agent.sessionId); // sessionId is the TG fallback
  });

  it('TG route does not find feishu users', () => {
    const router = createRouter(writeConfig());
    // eric has no chatId
    const ericUser = router.resolveUser('111222333');
    expect(ericUser?.userId).toBe('owner'); // owner owns chatId 111222333
    // feishu openId not found via TG route
    expect(router.resolveByBotAgent('pulse', '111222333')).toBeUndefined();
  });

  it('dual-channel config resolves the same logical agent via Telegram and Feishu', () => {
    const dualConfig: RelayConfig = {
      users: [{
        userId: 'owner',
        chatId: '123456',
        feishuOpenId: 'ou_owner',
        defaultAgent: 'main',
        agents: [{
          id: 'main', name: 'Main', emoji: '\u{1F3B2}',
          botToken: 'tg-token',
          workspace: '/tmp/ws',
          sessionId: 'uuid-tg',
          feishu: { appId: 'cli_xxx', appSecret: 'secret', sessionId: 'uuid-fs' },
          feishuOpenId: 'ou_owner',
        }],
      }],
    };
    expect(() => validateConfig(dualConfig)).not.toThrow();
    const router = createRouter(writeConfig(dualConfig));

    expect(router.getAllAgentBots()).toHaveLength(1);
    expect(router.getAllFeishuBots()).toHaveLength(1);
    expect(router.resolveByBotAgent('main', '123456')?.agent.id).toBe('main');
    expect(router.resolveByFeishuAgent('main', 'ou_owner')?.agent.id).toBe('main');
    expect(router.poolKey('owner', 'main')).toBe('owner:main');
  });

  it('router poolKey is consistent across TG and feishu for same user', () => {
    const router = createRouter(writeConfig());
    const tgPoolKey = router.poolKey('owner', 'duke');
    const fsPoolKey = router.poolKey('eric', 'pulse');
    expect(tgPoolKey).toBe('owner:duke');
    expect(fsPoolKey).toBe('eric:pulse');
  });
});

describe('feishu integration: multi-user shared agent', () => {
  it('both feishu users share the same agent config via getAllFeishuBots', () => {
    const router = createRouter(writeConfig());
    const bots = router.getAllFeishuBots();
    expect(bots.length).toBe(1);
    expect(bots[0].authorizedOpenIds).toContain('ou_eric123');
    expect(bots[0].authorizedOpenIds).toContain('ou_kuoran456');
  });

  it('each user resolves to their own UserConfig', () => {
    const router = createRouter(writeConfig());
    const ericResolved = router.resolveByFeishuAgent('pulse', 'ou_eric123');
    const kuoranResolved = router.resolveByFeishuAgent('pulse', 'ou_kuoran456');
    expect(ericResolved!.user.userId).toBe('eric');
    expect(kuoranResolved!.user.userId).toBe('kuoran');
    // Same agent id, but different user config
    expect(ericResolved!.agent.id).toBe('pulse');
    expect(kuoranResolved!.agent.id).toBe('pulse');
  });
});
