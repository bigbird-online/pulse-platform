import { describe, it, expect } from 'vitest';
import { validateConfig, ConfigValidationError } from '../src/types.js';
import type { RelayConfig } from '../src/types.js';

// --- v1 config (TG only, no channel field) ---
const v1Config: RelayConfig = {
  users: [{
    userId: 'owner',
    chatId: '123456',
    defaultAgent: 'main',
    agents: [{
      id: 'main', name: 'Duke', emoji: '\u{1F3B2}',
      botToken: 'fake-token', workspace: '/tmp/ws', sessionId: 'uuid-1',
    }],
  }],
};

// --- v2 feishu config ---
const feishuConfig: RelayConfig = {
  users: [{
    userId: 'eric',
    feishuOpenId: 'ou_abc123',
    defaultAgent: 'pulse',
    agents: [{
      id: 'pulse', name: 'Pulse', emoji: '\u{1F52E}',
      channel: 'feishu',
      workspace: '/tmp/ws2',
      feishu: { appId: 'cli_xxx', appSecret: 'secret', sessionId: 'uuid-2' },
    }],
  }],
};

// --- dual channel config ---
const dualChannelConfig: RelayConfig = {
  users: [{
    userId: 'owner',
    chatId: '123456',
    defaultAgent: 'main',
    agents: [{
        id: 'main', name: 'Duke', emoji: '\u{1F3B2}',
        botToken: 'fake-token',
        workspace: '/tmp/ws',
        sessionId: 'uuid-tg',
        feishu: { appId: 'cli_xxx', appSecret: 'secret', sessionId: 'uuid-3' },
        feishuOpenId: 'ou_duke',
      }],
  }],
};

// --- feishu agent without feishuOpenId ---
const missingOpenIdConfig: RelayConfig = {
  users: [{
    userId: 'eric',
    defaultAgent: 'pulse',
    agents: [{
      id: 'pulse', name: 'Pulse', emoji: '\u{1F52E}',
      channel: 'feishu',
      workspace: '/tmp/ws2',
      feishu: { appId: 'cli_xxx', appSecret: 'secret', sessionId: 'uuid-4' },
    }],
  }],
};

describe('validateConfig', () => {
  it('allows a local runtime user without opening a channel', () => {
    const config: RelayConfig = { users: [{
      userId: 'oracle-local', localOnly: true, defaultAgent: 'pulse',
      agents: [{ id: 'pulse', name: 'Pulse', emoji: '⚡', workspace: '/tmp/ws' }],
    }] };
    expect(() => validateConfig(config)).not.toThrow();
    config.users[0]!.agents[0]!.botToken = 'unexpected';
    expect(() => validateConfig(config)).toThrow(/cannot have channel credentials/);
  });

  it('v1 config (no channel field) passes', () => {
    expect(() => validateConfig(v1Config)).not.toThrow();
  });

  it('accepts only a separate platform bot with its owner chat', () => {
    const config: RelayConfig = structuredClone(v1Config);
    config.platformBot = { botToken: 'platform-token', ownerChatId: '1000000001' };
    expect(() => validateConfig(config)).not.toThrow();
    config.platformBot = { botToken: 'fake-token', ownerChatId: '1000000001' };
    expect(() => validateConfig(config)).toThrow(ConfigValidationError);
    config.platformBot = { botToken: 'platform-token', ownerChatId: 'owner' };
    expect(() => validateConfig(config)).toThrow(ConfigValidationError);
  });

  it('feishu config passes', () => {
    expect(() => validateConfig(feishuConfig)).not.toThrow();
  });

  it('allows one logical agent to expose both Telegram and Feishu credentials', () => {
    expect(() => validateConfig(dualChannelConfig)).not.toThrow();
  });

  it('allows feishu agent without feishuOpenId for pending pairing', () => {
    expect(() => validateConfig(missingOpenIdConfig)).not.toThrow();
  });

  it('accepts an explicit complete Pi conversation route', () => {
    const config: RelayConfig = structuredClone(v1Config);
    config.users[0]!.agents[0]!.conversationRuntime = {
      runtime: 'pi',
      model: { provider: 'anthropic', model: 'claude-sonnet-4-5', reasoning: 'high' },
      isolationProfile: 'internal-canary',
      runtimeOptions: { pi: { idleEviction: true } },
    };
    expect(() => validateConfig(config)).not.toThrow();
  });

  it('accepts a complete node Pi capability without requiring per-agent routes', () => {
    const config: RelayConfig = structuredClone(v1Config);
    config.conversationRuntimeDefaults = {
      pi: {
        model: { provider: 'openai-codex', model: 'gpt-5.6-sol', reasoning: 'medium' },
        isolationProfile: 'resident-host-native',
      },
    };
    expect(() => validateConfig(config)).not.toThrow();
  });

  it('rejects an incomplete node Pi capability', () => {
    const config: RelayConfig = structuredClone(v1Config);
    config.conversationRuntimeDefaults = { pi: { isolationProfile: 'resident-host-native' } };
    expect(() => validateConfig(config)).toThrow(/Node Pi runtime default requires provider, model, reasoning, and isolationProfile/);
  });

  it('rejects incomplete or mixed-fallback Pi routes', () => {
    const incomplete: RelayConfig = structuredClone(v1Config);
    incomplete.users[0]!.agents[0]!.conversationRuntime = { runtime: 'pi' };
    expect(() => validateConfig(incomplete)).toThrow(/requires provider, model, reasoning, and isolationProfile/);

    const mixed: RelayConfig = structuredClone(v1Config);
    mixed.users[0]!.agents[0]!.conversationRuntime = {
      runtime: 'pi',
      model: { provider: 'anthropic', model: 'claude-sonnet-4-5', reasoning: 'high' },
      isolationProfile: 'internal-canary',
    };
    mixed.users[0]!.agents[0]!.conversationRuntimePreference = ['pi', 'codex'];
    expect(() => validateConfig(mixed)).toThrow(/cannot be configured as automatic fallback/);
  });

  it('throws on channel=feishu but no feishu block', () => {
    const badConfig: RelayConfig = {
      users: [{
        userId: 'eric',
        feishuOpenId: 'ou_abc',
        defaultAgent: 'pulse',
        agents: [{
          id: 'pulse', name: 'Pulse', emoji: '\u{1F52E}',
          channel: 'feishu',
          botToken: 'some-token',
          workspace: '/tmp/ws',
          sessionId: 'uuid-5',
        }],
      }],
    };
    expect(() => validateConfig(badConfig)).toThrow(/no feishu credentials/i);
  });

  it('throws on channel=telegram but no botToken', () => {
    const badConfig: RelayConfig = {
      users: [{
        userId: 'owner',
        chatId: '123456',
        defaultAgent: 'main',
        agents: [{
          id: 'main', name: 'Duke', emoji: '\u{1F3B2}',
          channel: 'telegram',
          workspace: '/tmp/ws',
          sessionId: 'uuid-6',
        }],
      }],
    };
    expect(() => validateConfig(badConfig)).toThrow(/no botToken/i);
  });

  it('feishu agent with agent-level feishuOpenId passes (no user-level)', () => {
    const config: RelayConfig = {
      users: [{
        userId: 'eric',
        defaultAgent: 'artificer',
        agents: [
          {
            id: 'artificer', name: 'Artificer', emoji: '\u{1F527}',
            channel: 'feishu',
            workspace: '/tmp/ws',
            feishu: { appId: 'cli_a1', appSecret: 's1', sessionId: 'uuid-7' },
            feishuOpenId: 'ou_per_agent_1',
          },
          {
            id: 'gardener', name: 'Gardener', emoji: '\u{1F331}',
            channel: 'feishu',
            workspace: '/tmp/ws2',
            feishu: { appId: 'cli_a2', appSecret: 's2', sessionId: 'uuid-8' },
            feishuOpenId: 'ou_per_agent_2',
          },
        ],
      }],
    };
    expect(() => validateConfig(config)).not.toThrow();
  });

  it('allows feishu agent with neither agent-level nor user-level openId for pending pairing', () => {
    const config: RelayConfig = {
      users: [{
        userId: 'eric',
        defaultAgent: 'pulse',
        agents: [{
          id: 'pulse', name: 'Pulse', emoji: '\u{1F52E}',
          channel: 'feishu',
          workspace: '/tmp/ws',
          feishu: { appId: 'cli_xxx', appSecret: 'secret', sessionId: 'uuid-9' },
          // no feishuOpenId at agent or user level
        }],
      }],
    };
    expect(() => validateConfig(config)).not.toThrow();
  });
});
