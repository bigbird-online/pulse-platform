import { describe, it, expect, beforeEach, afterEach } from 'vitest';
import { writeFileSync, mkdirSync, rmSync } from 'node:fs';
import { join } from 'node:path';
import { tmpdir } from 'node:os';
import { createRouter } from '../src/router.js';

const testDir = join(tmpdir(), 'relay-feishu-router-test-' + Date.now());

const mixedConfig = {
  users: [
    {
      userId: 'owner',
      chatId: '111222333',
      defaultAgent: 'main',
      agents: [
        {
          id: 'main',
          name: 'Duke',
          emoji: '\u{1F3B2}',
          botToken: 'fake-duke-token',
          workspace: '/tmp/ws',
          sessionId: 'uuid-duke',
        },
      ],
    },
    {
      userId: 'eric',
      feishuOpenId: 'ou_eric123',
      defaultAgent: 'pulse',
      agents: [
        {
          id: 'pulse',
          name: 'Pulse',
          emoji: '\u{1F52E}',
          channel: 'feishu',
          workspace: '/tmp/ws2',
          feishu: { appId: 'cli_xxx', appSecret: 'secret', sessionId: 'uuid-pulse' },
        },
      ],
    },
    {
      userId: 'kuoran',
      feishuOpenId: 'ou_kuoran456',
      defaultAgent: 'pulse',
      agents: [
        {
          id: 'pulse',
          name: 'Pulse',
          emoji: '\u{1F52E}',
          channel: 'feishu',
          workspace: '/tmp/ws3',
          feishu: { appId: 'cli_xxx', appSecret: 'secret', sessionId: 'uuid-pulse-kr' },
        },
      ],
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

describe('Router feishu extensions', () => {
  it('resolveByFeishuAgent finds user by openId', () => {
    const router = createRouter(writeConfig());
    const result = router.resolveByFeishuAgent('pulse', 'ou_eric123');
    expect(result).toBeDefined();
    expect(result!.user.userId).toBe('eric');
    expect(result!.agent.id).toBe('pulse');
  });

  it('resolveByFeishuAgent returns undefined for unknown openId', () => {
    const router = createRouter(writeConfig());
    expect(router.resolveByFeishuAgent('pulse', 'ou_unknown')).toBeUndefined();
  });

  it('resolveByFeishuAgent returns undefined for wrong agentId', () => {
    const router = createRouter(writeConfig());
    expect(router.resolveByFeishuAgent('nonexistent', 'ou_eric123')).toBeUndefined();
  });

  it('TG and feishu routing do not interfere', () => {
    const router = createRouter(writeConfig());
    // TG route
    const tgResult = router.resolveByBotAgent('main', '111222333');
    expect(tgResult).toBeDefined();
    expect(tgResult!.user.userId).toBe('owner');
    // Feishu route
    const fsResult = router.resolveByFeishuAgent('pulse', 'ou_eric123');
    expect(fsResult).toBeDefined();
    expect(fsResult!.user.userId).toBe('eric');
    // TG openId not found
    expect(router.resolveByFeishuAgent('main', '111222333')).toBeUndefined();
    // Feishu chatId not found
    expect(router.resolveByBotAgent('pulse', 'ou_eric123')).toBeUndefined();
  });

  it('getAllFeishuBots returns only feishu agents', () => {
    const router = createRouter(writeConfig());
    const bots = router.getAllFeishuBots();
    expect(bots.length).toBe(1);
    expect(bots[0].agent.id).toBe('pulse');
    expect(bots[0].authorizedOpenIds).toContain('ou_eric123');
    expect(bots[0].authorizedOpenIds).toContain('ou_kuoran456');
  });

  it('getAllFeishuBots returns empty for v1-only config', () => {
    const v1OnlyConfig = {
      users: [{
        userId: 'owner',
        chatId: '123456',
        defaultAgent: 'main',
        agents: [{
          id: 'main', name: 'Duke', emoji: '\u{1F3B2}',
          botToken: 'fake-token', workspace: '/tmp', sessionId: 'uuid',
        }],
      }],
    };
    const router = createRouter(writeConfig(v1OnlyConfig));
    expect(router.getAllFeishuBots()).toEqual([]);
  });

  it('getAllAgentBots returns only TG agents (not feishu)', () => {
    const router = createRouter(writeConfig());
    const bots = router.getAllAgentBots();
    expect(bots.length).toBe(1);
    expect(bots[0].agent.id).toBe('main');
    expect(bots[0].authorizedChatIds).toEqual(['111222333']);
  });

  it('getAgentChannel returns correct channel type', () => {
    const router = createRouter(writeConfig());
    const tgAgents = router.getAllAgentBots();
    const fsAgents = router.getAllFeishuBots();
    expect(router.getAgentChannel(tgAgents[0].agent)).toBe('telegram');
    expect(router.getAgentChannel(fsAgents[0].agent)).toBe('feishu');
  });

  it('resetSession updates feishu sessionId', () => {
    const router = createRouter(writeConfig());
    const oldSession = router.resolveByFeishuAgent('pulse', 'ou_eric123')!.agent.feishu!.sessionId;
    const newId = router.resetSession('eric', 'pulse');
    expect(newId).toBeDefined();
    expect(newId).not.toBe(oldSession);
    const updated = router.resolveByFeishuAgent('pulse', 'ou_eric123')!.agent;
    expect(updated.feishu!.sessionId).toBe(newId);
  });

  it('overrideSessionId updates feishu sessionId', () => {
    const router = createRouter(writeConfig());
    router.overrideSessionId('eric', 'pulse', 'custom-uuid');
    const updated = router.resolveByFeishuAgent('pulse', 'ou_eric123')!.agent;
    expect(updated.feishu!.sessionId).toBe('custom-uuid');
  });

  it('dual-channel agent is addressable through Telegram and Feishu with one pool key', () => {
    const config = {
      users: [{
        userId: 'owner',
        chatId: '111222333',
        defaultAgent: 'main',
        agents: [{
          id: 'main',
          name: 'Duke',
          emoji: '\u{1F3B2}',
          botToken: 'fake-duke-token',
          workspace: '/tmp/ws',
          sessionId: 'uuid-duke',
          feishu: { appId: 'cli_duke', appSecret: 'secret', sessionId: 'uuid-duke-fs' },
          feishuOpenId: 'ou_duke_app',
        }],
      }],
    };
    const router = createRouter(writeConfig(config));

    expect(router.getAllAgentBots().map(bot => bot.agent.id)).toEqual(['main']);
    expect(router.getAllFeishuBots().map(bot => bot.agent.id)).toEqual(['main']);
    expect(router.resolveByBotAgent('main', '111222333')?.agent.id).toBe('main');
    expect(router.resolveByFeishuAgent('main', 'ou_duke_app')?.agent.id).toBe('main');
    expect(router.poolKey('owner', 'main')).toBe('owner:main');
  });

  it.each([undefined, 'tg', 'feishu'] as const)('dual-channel reset isolates scope %s', (scope) => {
    const config = {
      users: [{
        userId: 'owner',
        chatId: '111222333',
        defaultAgent: 'main',
        agents: [{
          id: 'main',
          name: 'Duke',
          emoji: '\u{1F3B2}',
          botToken: 'fake-duke-token',
          workspace: '/tmp/ws',
          sessionId: 'uuid-duke',
          feishu: { appId: 'cli_duke', appSecret: 'secret', sessionId: 'uuid-duke-fs' },
          feishuOpenId: 'ou_duke_app',
        }],
      }],
    };
    const router = createRouter(writeConfig(config));
    const newId = router.resetSession('owner', 'main', scope);
    const updated = router.resolveByFeishuAgent('main', 'ou_duke_app')!.agent;

    expect(updated.sessionId).toBe(scope === 'feishu' ? 'uuid-duke' : newId);
    expect(updated.feishu!.sessionId).toBe(scope === 'feishu' ? newId : 'uuid-duke-fs');
  });

  it('resolves Feishu users from explicit binding records without legacy openId fields', () => {
    const config = {
      users: [{
        userId: 'owner',
        defaultAgent: 'main',
        agents: [{
          id: 'main',
          name: 'Duke',
          emoji: '\u{1F3B2}',
          channel: 'feishu',
          workspace: '/tmp/ws',
          feishu: { appId: 'cli_duke', appSecret: 'secret', sessionId: 'uuid-duke-fs' },
        }],
      }],
      bindings: [{
        id: 'binding-feishu-main',
        address: { channel: 'feishu', kind: 'feishu_open_id', value: 'ou_explicit', appId: 'cli_duke', botId: 'main' },
        principalId: 'owner',
        tenantId: 'owner',
        defaultAgentId: 'main',
        allowedAgentIds: ['main'],
        status: 'active',
        createdAt: '2026-04-27T00:00:00Z',
      }],
    };
    const router = createRouter(writeConfig(config));

    expect(router.resolveByFeishuAgent('main', 'ou_explicit')?.user.userId).toBe('owner');
    expect(router.getAllFeishuBots()[0].authorizedOpenIds).toEqual(['ou_explicit']);
    expect(router.getGatewayBindings?.()).toHaveLength(1);
  });

  it('resolves Telegram users from explicit binding records without legacy chatId', () => {
    const config = {
      users: [{
        userId: 'owner',
        defaultAgent: 'main',
        agents: [{
          id: 'main',
          name: 'Duke',
          emoji: '\u{1F3B2}',
          botToken: 'fake-duke-token',
          workspace: '/tmp/ws',
          sessionId: 'uuid-duke',
        }],
      }],
      bindings: [{
        id: 'binding-tg-main',
        address: { channel: 'telegram', kind: 'telegram_chat', value: '111222333', botId: 'main' },
        principalId: 'owner',
        tenantId: 'owner',
        defaultAgentId: 'main',
        allowedAgentIds: ['main'],
        status: 'active',
        createdAt: '2026-04-27T00:00:00Z',
      }],
    };
    const router = createRouter(writeConfig(config));

    expect(router.resolveByBotAgent('main', '111222333')?.user.userId).toBe('owner');
    expect(router.getAllAgentBots()[0].authorizedChatIds).toEqual(['111222333']);
  });
});

describe('Router feishu per-agent openId', () => {
  // One user with two feishu agents, each with different openId (per-app)
  const perAgentOpenIdConfig = {
    users: [
      {
        userId: 'eric',
        defaultAgent: 'artificer',
        agents: [
          {
            id: 'artificer',
            name: 'Artificer',
            emoji: '\u{1F527}',
            channel: 'feishu',
            workspace: '/tmp/ws-artificer',
            feishu: { appId: 'cli_a1', appSecret: 's1', sessionId: 'uuid-artificer' },
            feishuOpenId: 'ou_artificer_app',
          },
          {
            id: 'gardener',
            name: 'Gardener',
            emoji: '\u{1F331}',
            channel: 'feishu',
            workspace: '/tmp/ws-gardener',
            feishu: { appId: 'cli_a2', appSecret: 's2', sessionId: 'uuid-gardener' },
            feishuOpenId: 'ou_gardener_app',
          },
        ],
      },
    ],
  };

  it('resolveByFeishuAgent finds user by agent-level openId', () => {
    const router = createRouter(writeConfig(perAgentOpenIdConfig));
    const artificer = router.resolveByFeishuAgent('artificer', 'ou_artificer_app');
    expect(artificer).toBeDefined();
    expect(artificer!.user.userId).toBe('eric');
    expect(artificer!.agent.id).toBe('artificer');

    const gardener = router.resolveByFeishuAgent('gardener', 'ou_gardener_app');
    expect(gardener).toBeDefined();
    expect(gardener!.user.userId).toBe('eric');
    expect(gardener!.agent.id).toBe('gardener');
  });

  it('resolveByFeishuAgent resolves correctly when user has multiple agents sharing different openIds', () => {
    const router = createRouter(writeConfig(perAgentOpenIdConfig));
    // Both openIds map to same user; agentId disambiguates
    const viaArtificerOpenId = router.resolveByFeishuAgent('gardener', 'ou_artificer_app');
    expect(viaArtificerOpenId).toBeDefined();
    expect(viaArtificerOpenId!.user.userId).toBe('eric');
    expect(viaArtificerOpenId!.agent.id).toBe('gardener');
  });

  it('getAllFeishuBots collects per-agent openIds', () => {
    const router = createRouter(writeConfig(perAgentOpenIdConfig));
    const bots = router.getAllFeishuBots();
    expect(bots.length).toBe(2);
    const artificerBot = bots.find(b => b.agent.id === 'artificer');
    const gardenerBot = bots.find(b => b.agent.id === 'gardener');
    expect(artificerBot!.authorizedOpenIds).toEqual(['ou_artificer_app']);
    expect(gardenerBot!.authorizedOpenIds).toEqual(['ou_gardener_app']);
  });

  it('resolveByFeishuAgent returns undefined for unknown openId', () => {
    const router = createRouter(writeConfig(perAgentOpenIdConfig));
    expect(router.resolveByFeishuAgent('artificer', 'ou_unknown')).toBeUndefined();
  });
});
