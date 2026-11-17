import { describe, expect, it } from 'vitest';
import {
  channelAddressKey,
  createGatewayBindingView,
  normalizeFeishuOpenIdAddress,
  normalizeTelegramChatAddress,
  resolveInboundRoute,
} from '../src/gateway.js';
import type { RelayConfig } from '../src/types.js';

describe('gateway address normalization', () => {
  it('normalizes Telegram chat addresses with bot scope', () => {
    const address = normalizeTelegramChatAddress(111222333, 'duke');

    expect(address).toEqual({
      channel: 'telegram',
      kind: 'telegram_chat',
      value: '111222333',
      botId: 'duke',
    });
    expect(channelAddressKey(address)).toBe('telegram:telegram_chat::duke:111222333');
  });

  it('normalizes Feishu open_id addresses with app scope', () => {
    const dukeAddress = normalizeFeishuOpenIdAddress('ou_shared', 'cli_duke_app', 'duke');
    const pulseAddress = normalizeFeishuOpenIdAddress('ou_shared', 'cli_pulse_app', 'pulse');

    expect(dukeAddress).toEqual({
      channel: 'feishu',
      kind: 'feishu_open_id',
      value: 'ou_shared',
      botId: 'duke',
      appId: 'cli_duke_app',
    });
    expect(channelAddressKey(dukeAddress)).toBe('feishu:feishu_open_id:cli_duke_app:duke:ou_shared');
    expect(channelAddressKey(dukeAddress)).not.toBe(channelAddressKey(pulseAddress));
  });
});

describe('createGatewayBindingView', () => {
  it('loads active explicit bindings before legacy-derived bindings', () => {
    const config: RelayConfig = {
      users: [{
        userId: 'duke-owner',
        chatId: '111222333',
        defaultAgent: 'duke',
        agents: [{
          id: 'duke',
          name: 'Duke',
          emoji: 'D',
          botToken: 'fake-duke-token',
          workspace: '/work/duke',
          sessionId: 'uuid-duke',
          runtime: 'codex',
        }],
      }],
      bindings: [{
        id: 'binding-explicit-tg-duke',
        address: normalizeTelegramChatAddress('111222333', 'duke'),
        principalId: 'duke-owner',
        tenantId: 'duke-owner',
        defaultAgentId: 'duke',
        allowedAgentIds: ['duke'],
        status: 'active',
        createdAt: '2026-04-27T00:00:00Z',
        defaultRuntimePreference: ['cc'],
        auditTags: ['approved'],
      }],
    };

    const view = createGatewayBindingView(config);

    expect(view.bindings).toHaveLength(1);
    expect(view.bindings[0]).toMatchObject({
      id: 'binding-explicit-tg-duke',
      principalId: 'duke-owner',
      workspaceId: '/work/duke',
      defaultRuntimePreference: ['cc'],
    });
    expect(view.bindings[0].auditTags).toEqual(['relay-json-bindings', 'approved']);
  });

  it('lets non-active explicit bindings suppress legacy fallback for the same address', () => {
    const config: RelayConfig = {
      users: [{
        userId: 'duke-owner',
        chatId: '111222333',
        defaultAgent: 'duke',
        agents: [{
          id: 'duke',
          name: 'Duke',
          emoji: 'D',
          botToken: 'fake-duke-token',
          workspace: '/work/duke',
          sessionId: 'uuid-duke',
        }],
      }],
      bindings: [{
        id: 'binding-revoked-tg-duke',
        address: normalizeTelegramChatAddress('111222333', 'duke'),
        principalId: 'duke-owner',
        tenantId: 'duke-owner',
        defaultAgentId: 'duke',
        allowedAgentIds: ['duke'],
        status: 'revoked',
        createdAt: '2026-04-27T00:00:00Z',
      }],
    };

    expect(createGatewayBindingView(config).bindings).toHaveLength(0);
  });

  it('derives distinct bindings for multiple Telegram chat ids', () => {
    const config: RelayConfig = {
      users: [
        {
          userId: 'owner',
          chatId: '111222333',
          defaultAgent: 'duke',
          agents: [{
            id: 'duke',
            name: 'Duke',
            emoji: 'D',
            botToken: 'fake-duke-token',
            workspace: '/work/owner/duke',
            sessionId: 'uuid-duke-owner',
            conversationRuntimePreference: ['codex', 'cc'],
          }],
        },
        {
          userId: 'eric',
          chatId: '444555666',
          defaultAgent: 'pulse',
          agents: [{
            id: 'pulse',
            name: 'Pulse',
            emoji: 'P',
            botToken: 'fake-pulse-token',
            workspace: '/work/eric/pulse',
            sessionId: 'uuid-pulse-eric',
            runtime: 'cc',
          }],
        },
      ],
    };

    const view = createGatewayBindingView(config);

    expect(view.bindings).toHaveLength(2);
    expect(view.bindings.map(binding => binding.address.value)).toEqual(['111222333', '444555666']);
    expect(view.bindings.map(binding => binding.principalId)).toEqual(['owner', 'eric']);
    expect(view.bindings[0].address).toMatchObject({
      channel: 'telegram',
      kind: 'telegram_chat',
      botId: 'duke',
    });
    expect(view.bindings[0].defaultRuntimePreference).toEqual(['codex', 'cc']);
  });

  it('uses agent-level Feishu open_id before user-level open_id and includes appId', () => {
    const config: RelayConfig = {
      users: [{
        userId: 'duke-owner',
        feishuOpenId: 'ou_user_level',
        defaultAgent: 'duke',
        agents: [{
          id: 'duke',
          name: 'Duke',
          emoji: 'D',
          channel: 'feishu',
          workspace: '/work/duke',
          feishu: { appId: 'cli_duke_app', appSecret: 'secret', sessionId: 'uuid-duke-feishu' },
          feishuOpenId: 'ou_agent_level',
          conversationRuntimePreference: ['codex', 'cc'],
        }],
      }],
    };

    const binding = createGatewayBindingView(config).bindings[0];

    expect(binding.address).toEqual({
      channel: 'feishu',
      kind: 'feishu_open_id',
      value: 'ou_agent_level',
      botId: 'duke',
      appId: 'cli_duke_app',
    });
    expect(binding.principalId).toBe('duke-owner');
    expect(binding.defaultRuntimePreference).toEqual(['codex', 'cc']);
  });

  it('allows the same principal to own Telegram and Feishu addresses', () => {
    const config: RelayConfig = {
      users: [{
        userId: 'duke-owner',
        chatId: '111222333',
        feishuOpenId: 'ou_duke_owner',
        defaultAgent: 'duke',
        agents: [
          {
            id: 'duke',
            name: 'Duke',
            emoji: 'D',
            botToken: 'fake-duke-token',
            workspace: '/work/duke/tg',
            sessionId: 'uuid-duke-tg',
          },
          {
            id: 'duke',
            name: 'Duke',
            emoji: 'D',
            channel: 'feishu',
            workspace: '/work/duke/feishu',
            feishu: { appId: 'cli_duke_app', appSecret: 'secret', sessionId: 'uuid-duke-feishu' },
          },
        ],
      }],
    };

    const view = createGatewayBindingView(config);

    expect(view.principals).toEqual([{ id: 'duke-owner', type: 'human', tenantId: 'duke-owner' }]);
    expect(view.bindings).toHaveLength(2);
    expect(view.bindings.map(binding => binding.principalId)).toEqual(['duke-owner', 'duke-owner']);
    expect(view.bindings.map(binding => binding.address.channel).sort()).toEqual(['feishu', 'telegram']);
    expect(view.bindings.every(binding => binding.allowedAgentIds.includes('duke'))).toBe(true);
  });

  it('derives TG and Feishu bindings from one dual-channel logical agent', () => {
    const config: RelayConfig = {
      users: [{
        userId: 'duke-owner',
        chatId: '111222333',
        defaultAgent: 'duke',
        agents: [{
          id: 'duke',
          name: 'Duke',
          emoji: 'D',
          botToken: 'fake-duke-token',
          workspace: '/work/duke',
          sessionId: 'uuid-duke',
          runtime: 'codex',
          conversationRuntimePreference: ['codex', 'cc'],
          feishu: { appId: 'cli_duke_app', appSecret: 'secret', sessionId: 'uuid-duke-feishu' },
          feishuOpenId: 'ou_duke_app',
        }],
      }],
    };

    const view = createGatewayBindingView(config);
    expect(view.bindings).toHaveLength(2);
    expect(view.bindings.map(binding => binding.address.channel).sort()).toEqual(['feishu', 'telegram']);
    expect(view.bindings.map(binding => binding.defaultAgentId)).toEqual(['duke', 'duke']);

    const feishu = resolveInboundRoute({
      users: config.users,
      event: {
        id: 'evt-feishu-dual',
        createdAt: '2026-04-27T00:00:00Z',
        channel: 'feishu',
        address: normalizeFeishuOpenIdAddress('ou_duke_app', 'cli_duke_app', 'duke'),
      },
      targetAgentId: 'duke',
    });
    expect(feishu.status).toBe('ok');
    if (feishu.status !== 'ok') return;
    expect(feishu.route).toMatchObject({
      agentId: 'duke',
      workspace: '/work/duke',
      sessionId: 'uuid-duke',
      runtimePreference: ['codex', 'cc'],
      conversationKey: 'duke-owner:duke',
    });
  });

  it('falls back to user-level Feishu open_id when agent-level binding is absent', () => {
    const config: RelayConfig = {
      users: [{
        userId: 'pulse-owner',
        feishuOpenId: 'ou_user_level',
        defaultAgent: 'pulse',
        agents: [{
          id: 'pulse',
          name: 'Pulse',
          emoji: 'P',
          channel: 'feishu',
          workspace: '/work/pulse',
          feishu: { appId: 'cli_pulse_app', appSecret: 'secret', sessionId: 'uuid-pulse-feishu' },
        }],
      }],
    };

    const binding = createGatewayBindingView(config).bindings[0];

    expect(binding.address).toEqual({
      channel: 'feishu',
      kind: 'feishu_open_id',
      value: 'ou_user_level',
      botId: 'pulse',
      appId: 'cli_pulse_app',
    });
    expect(binding.id).toContain('cli_pulse_app:pulse:ou_user_level');
  });

  it('keeps same Feishu open_id separate across different apps', () => {
    const config: RelayConfig = {
      users: [{
        userId: 'duke-owner',
        feishuOpenId: 'ou_shared',
        defaultAgent: 'duke',
        agents: [
          {
            id: 'duke',
            name: 'Duke',
            emoji: 'D',
            channel: 'feishu',
            workspace: '/work/duke',
            feishu: { appId: 'cli_duke_app', appSecret: 'secret', sessionId: 'uuid-duke' },
          },
          {
            id: 'pulse',
            name: 'Pulse',
            emoji: 'P',
            channel: 'feishu',
            workspace: '/work/pulse',
            feishu: { appId: 'cli_pulse_app', appSecret: 'secret', sessionId: 'uuid-pulse' },
          },
        ],
      }],
    };

    const view = createGatewayBindingView(config);
    const addressKeys = view.bindings.map(binding => channelAddressKey(binding.address));

    expect(view.bindings).toHaveLength(2);
    expect(addressKeys).toEqual([
      'feishu:feishu_open_id:cli_duke_app:duke:ou_shared',
      'feishu:feishu_open_id:cli_pulse_app:pulse:ou_shared',
    ]);
    expect(new Set(addressKeys).size).toBe(2);
  });
});

describe('resolveInboundRoute', () => {
  const config: RelayConfig = {
    users: [{
      userId: 'duke-owner',
      chatId: '111222333',
      feishuOpenId: 'ou_duke_owner',
      defaultAgent: 'duke',
      agents: [
        {
          id: 'duke',
          name: 'Duke',
          emoji: 'D',
          botToken: 'fake-duke-token',
          workspace: '/work/duke/tg',
          sessionId: 'uuid-duke-tg',
          conversationRuntimePreference: ['codex', 'cc'],
        },
        {
          id: 'pulse',
          name: 'Pulse',
          emoji: 'P',
          channel: 'feishu',
          workspace: '/work/pulse/feishu',
          feishu: { appId: 'cli_pulse_app', appSecret: 'secret', sessionId: 'uuid-pulse-feishu' },
          runtime: 'cc',
        },
      ],
    }],
  };

  it('resolves a Telegram inbound event into a RoutePolicyResult', () => {
    const result = resolveInboundRoute({
      users: config.users,
      event: {
        id: 'evt-tg-1',
        createdAt: '2026-04-27T12:47:17+0800',
        channel: 'telegram',
        address: normalizeTelegramChatAddress('111222333', 'duke'),
        text: 'hello',
      },
      targetAgentId: 'duke',
    });

    expect(result.status).toBe('ok');
    if (result.status !== 'ok') throw new Error('expected ok route');
    expect(result.route).toMatchObject({
      agentId: 'duke',
      workspace: '/work/duke/tg',
      sessionId: 'uuid-duke-tg',
      runtimePreference: ['codex', 'cc'],
      conversationKey: 'duke-owner:duke',
      audit: { correlationId: 'evt-tg-1', source: 's1-inbound' },
    });
    expect(result.route.principal).toEqual({ id: 'duke-owner', type: 'human', tenantId: 'duke-owner' });
  });

  it('resolves a Feishu inbound event with app-scoped open_id', () => {
    const result = resolveInboundRoute({
      users: config.users,
      event: {
        id: 'evt-fs-1',
        createdAt: '2026-04-27T12:47:17+0800',
        channel: 'feishu',
        address: normalizeFeishuOpenIdAddress('ou_duke_owner', 'cli_pulse_app', 'pulse'),
        text: 'hello',
      },
      targetAgentId: 'pulse',
    });

    expect(result.status).toBe('ok');
    if (result.status !== 'ok') throw new Error('expected ok route');
    expect(result.route).toMatchObject({
      agentId: 'pulse',
      workspace: '/work/pulse/feishu',
      sessionId: 'uuid-pulse-feishu',
      runtimePreference: ['cc'],
      conversationKey: 'duke-owner:pulse',
      audit: { correlationId: 'evt-fs-1', source: 's1-inbound' },
    });
    expect(result.route.binding.address.appId).toBe('cli_pulse_app');
  });

  it('carries runtime: cc-sdk through the Feishu route preference instead of collapsing to cc', () => {
    // Regression: cc-sdk agents were silently downgraded to cc because the
    // gateway preference filter only kept cc/codex, which sent Feishu spawns
    // back onto the legacy pool path.
    const sdkConfig: RelayConfig = {
      users: [{
        userId: 'duke-owner',
        feishuOpenId: 'ou_duke_owner',
        defaultAgent: 'sdktest',
        agents: [
          {
            id: 'sdktest',
            name: 'SdkTest',
            emoji: 'S',
            channel: 'feishu',
            workspace: '/work/sdktest/feishu',
            feishu: { appId: 'cli_sdk_app', appSecret: 'secret', sessionId: 'uuid-sdk-feishu' },
            runtime: 'cc-sdk',
          },
        ],
      }],
    };
    const result = resolveInboundRoute({
      users: sdkConfig.users,
      event: {
        id: 'evt-fs-sdk',
        createdAt: '2026-04-27T12:47:17+0800',
        channel: 'feishu',
        address: normalizeFeishuOpenIdAddress('ou_duke_owner', 'cli_sdk_app', 'sdktest'),
        text: 'hello',
      },
      targetAgentId: 'sdktest',
    });

    expect(result.status).toBe('ok');
    if (result.status !== 'ok') throw new Error('expected ok route');
    expect(result.route.runtimePreference).toEqual(['cc-sdk']);
  });

  it('carries runtime: pi through the gateway route instead of collapsing to cc', () => {
    const piConfig: RelayConfig = {
      users: [{
        userId: 'owner',
        chatId: '111222333',
        defaultAgent: 'tcloud-pulse',
        agents: [{
          id: 'tcloud-pulse',
          name: 'TCloud Pulse',
          emoji: 'P',
          botToken: 'fake-pi-token',
          workspace: '/work/tcloud-pulse',
          sessionId: 'uuid-pi',
          runtime: 'pi',
          conversationRuntimePreference: ['pi'],
        }],
      }],
    };
    const result = resolveInboundRoute({
      users: piConfig.users,
      event: {
        id: 'evt-tg-pi',
        createdAt: '2026-09-05T08:33:06Z',
        channel: 'telegram',
        address: normalizeTelegramChatAddress('111222333', 'tcloud-pulse'),
        text: 'resume native Pi',
      },
      targetAgentId: 'tcloud-pulse',
    });

    expect(result.status).toBe('ok');
    if (result.status !== 'ok') throw new Error('expected ok route');
    expect(result.route.runtimePreference).toEqual(['pi']);
  });

  it('returns pairing_required for an unbound channel address', () => {
    const result = resolveInboundRoute({
      users: config.users,
      event: {
        id: 'evt-tg-unknown',
        createdAt: '2026-04-27T12:47:17+0800',
        channel: 'telegram',
        address: normalizeTelegramChatAddress('999999999', 'duke'),
      },
      targetAgentId: 'duke',
    });

    expect(result).toEqual({
      status: 'pairing_required',
      address: normalizeTelegramChatAddress('999999999', 'duke'),
      reason: 'unbound-address',
    });
  });

  it('returns access_denied when the target agent is outside the binding allowlist', () => {
    const result = resolveInboundRoute({
      users: config.users,
      event: {
        id: 'evt-denied',
        createdAt: '2026-04-27T12:47:17+0800',
        channel: 'telegram',
        address: normalizeTelegramChatAddress('111222333', 'duke'),
      },
      targetAgentId: 'amanda',
    });

    expect(result).toEqual({
      status: 'access_denied',
      address: normalizeTelegramChatAddress('111222333', 'duke'),
      reason: 'agent-not-allowed',
    });
  });

  it('resolves active explicit bindings when no legacy open_id is present', () => {
    const explicitConfig: RelayConfig = {
      users: [{
        userId: 'duke-owner',
        defaultAgent: 'duke',
        agents: [{
          id: 'duke',
          name: 'Duke',
          emoji: 'D',
          channel: 'feishu',
          workspace: '/work/duke/feishu',
          feishu: { appId: 'cli_duke_app', appSecret: 'secret', sessionId: 'uuid-duke-feishu' },
          conversationRuntimePreference: ['codex', 'cc'],
        }],
      }],
      bindings: [{
        id: 'binding-explicit-feishu-duke',
        address: normalizeFeishuOpenIdAddress('ou_explicit', 'cli_duke_app', 'duke'),
        principalId: 'duke-owner',
        tenantId: 'duke-owner',
        defaultAgentId: 'duke',
        allowedAgentIds: ['duke'],
        status: 'active',
        createdAt: '2026-04-27T00:00:00Z',
        approvedAt: '2026-04-27T00:01:00Z',
        approvedBy: 'admin',
      }],
    };

    const result = resolveInboundRoute({
      users: explicitConfig.users,
      bindings: explicitConfig.bindings,
      event: {
        id: 'evt-explicit-feishu',
        createdAt: '2026-04-27T12:47:17+0800',
        channel: 'feishu',
        address: normalizeFeishuOpenIdAddress('ou_explicit', 'cli_duke_app', 'duke'),
      },
      targetAgentId: 'duke',
    });

    expect(result.status).toBe('ok');
    if (result.status !== 'ok') throw new Error('expected ok route');
    expect(result.route).toMatchObject({
      agentId: 'duke',
      conversationKey: 'duke-owner:duke',
      runtimePreference: ['codex', 'cc'],
    });
    expect(result.route.binding.id).toBe('binding-explicit-feishu-duke');
  });
});
