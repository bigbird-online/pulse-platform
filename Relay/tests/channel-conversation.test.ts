import {isolateHumanDecisions} from './helpers/scl-human-decision.js';
isolateHumanDecisions();
import {afterEach, beforeEach, describe, expect, it, vi} from 'vitest';
import {mkdtempSync, writeFileSync, rmSync} from 'node:fs';
import {tmpdir} from 'node:os';
import {join} from 'node:path';
import {createRouter, conversationChannelRouter} from '../src/router.js';
import {handleModel, handleThink, resolveInboundRoutingAgent, type CommandDeps} from '../src/commands.js';
import {validateConfig} from '../src/types.js';
import {createGatewayBindingView} from '../src/gateway.js';
import {readFileSync} from 'node:fs';
import {dispatchMenuAction, type MessageHandlerContext} from '../src/channels/feishu.js';

describe('dual-channel conversation configuration', () => {
  let dir: string;
  let configPath: string;
  beforeEach(() => {
    dir = mkdtempSync(join(tmpdir(), 'relay-channel-conversation-'));
    vi.stubEnv('RELAY_HOME', dir);
    configPath = join(dir, 'relay.json');
    writeFileSync(configPath, JSON.stringify({
      conversationRuntimeDefaults: {pi: {model: {provider: 'openai-codex', model: 'gpt-6.1-sol', reasoning: 'xhigh'}, isolationProfile: 'resident-host-native'}},
      users: [{userId: 'human', chatId: '123', feishuOpenId: 'ou_human', defaultAgent: 'pulse', agents: [{
        id: 'pulse', name: 'Pulse', emoji: '⚡', botToken: 'fake', workspace: dir,
        runtime: 'cc', conversationRuntimePreference: ['cc'], sessionId: 'original-tg',
        ccModel: 'claude-opus-5-5', feishu: {appId: 'app', appSecret: 'fake', sessionId: 'original-feishu'},
      }]}],
    }));
  });
  afterEach(() => {vi.unstubAllEnvs(); rmSync(dir, {recursive: true, force: true});});

  it('a real Feishu runtime card leaves the original TG runtime and history binding intact', async () => {
    const router = createRouter(configPath);
    const removeSession = vi.fn();
    const ctx = {
      router, agent: router.getAgents('human')[0], pool: {get: vi.fn(), kill: vi.fn(), clearYoloMode: vi.fn()},
      stateManager: {getSession: vi.fn(), removeSession, flush: vi.fn()},
      sendTextMessage: vi.fn(), sendCardMessage: vi.fn(),
    } as unknown as MessageHandlerContext;
    await dispatchMenuAction(ctx, 'ou_human', 'runtime', 'pi');
    const tg = router.resolveByBotAgent('pulse', '123')!.agent;
    expect(tg.runtime).toBe('cc');
    expect(tg.sessionId).toBe('original-tg');
    expect(tg.conversationRuntimePreference).toEqual(['cc']);
    expect(removeSession).toHaveBeenCalledWith('human:pulse:feishu');
    expect(removeSession).not.toHaveBeenCalledWith('human:pulse');
    const feishu = router.resolveByFeishuAgent('pulse', 'ou_human')!.agent;
    expect(feishu.runtime).toBe('pi');
    expect(feishu.feishu!.sessionId).toBe('original-feishu');
    router.reload();
    const restarted = createRouter(configPath);
    expect(restarted.resolveByBotAgent('pulse', '123')!.agent.runtime).toBe('cc');
    expect(restarted.resolveByFeishuAgent('pulse', 'ou_human')!.agent.runtime).toBe('pi');
  });

  it('TG runtime and model changes retain the legacy inherited Feishu route across restart', () => {
    const router = createRouter(configPath);
    router.setAgentRuntime('human', 'pulse', 'pi');
    router.setAgentModel('human', 'pulse', 'opus');
    const restarted = createRouter(configPath);
    const tg = restarted.resolveByBotAgent('pulse', '123')!.agent;
    const fs = restarted.resolveByFeishuAgent('pulse', 'ou_human')!.agent;
    expect(tg.runtime).toBe('pi');
    expect(fs.runtime).toBe('cc');
    expect(fs.conversationRuntime).toBeUndefined();
    expect(fs.ccModel).toBe('claude-opus-5-5');
    expect(fs.feishu!.sessionId).toBe('original-feishu');
  });

  it('a Feishu cross-runtime model command changes only the Feishu route', async () => {
    const router = createRouter(configPath);
    const deps = {
      router: conversationChannelRouter(router, 'feishu'), inboundChannel: 'feishu',
      runtime: {get: vi.fn(), kill: vi.fn(), clearYoloMode: vi.fn()},
      modelCatalog: {validateModelId: () => ({valid: true, resolvedId: 'gpt-6.1-sol'}), runtimeForModel: () => 'codex'},
    } as unknown as CommandDeps;
    await handleModel('human', 'pulse', 'gpt-6.1-sol', deps);
    const restarted = createRouter(configPath);
    expect(restarted.resolveByBotAgent('pulse', '123')!.agent.runtime).toBe('cc');
    expect(restarted.getAgentModel('human', 'pulse')).toBe('claude-opus-5-5');
    expect(restarted.resolveByFeishuAgent('pulse', 'ou_human')!.agent.runtime).toBe('codex');
    expect(restarted.getAgentModel('human', 'pulse', 'feishu')).toBe('gpt-6.1-sol');
  });

  it('Feishu model default and TG model overrides remain independent after hot reload', () => {
    const router = createRouter(configPath);
    router.setAgentModel('human', 'pulse', undefined, 'feishu');
    router.setAgentModel('human', 'pulse', 'opus');
    router.reload();
    expect(router.getAgentModel('human', 'pulse', 'feishu')).toBeUndefined();
    expect(router.resolveByFeishuAgent('pulse', 'ou_human')!.agent.ccModel).toBeUndefined();
    expect(router.getAgentModel('human', 'pulse')).toBe('opus');
    expect(createRouter(configPath).getAgentModel('human', 'pulse', 'feishu')).toBeUndefined();
  });

  it('Pi model and thinking changes stay in the selected channel', async () => {
    const router = createRouter(configPath);
    router.setAgentRuntime('human', 'pulse', 'pi');
    router.setAgentRuntime('human', 'pulse', 'pi', 'feishu');
    const previous = structuredClone(router.getAgents('human')[0].conversationRuntime);
    router.setAgentConversationModel('human', 'pulse', {provider: 'openai-codex', model: 'gpt-6-sol', reasoning: 'high'}, 'feishu');
    const deps = {
      router: conversationChannelRouter(router, 'feishu'), inboundChannel: 'feishu',
      runtime: {get: () => ({runtime: 'pi', state: 'idle'}), setThinkingLevel: vi.fn()},
    } as unknown as CommandDeps;
    const result = await handleThink('human', 'pulse', 'low', deps);
    expect(result.text).toContain('thinking → low');
    expect(router.getAgents('human')[0].conversationRuntime).toEqual(previous);
    const fs = createRouter(configPath).resolveByFeishuAgent('pulse', 'ou_human')!.agent;
    expect(fs.conversationRuntime!.model).toEqual({provider: 'openai-codex', model: 'gpt-6-sol', reasoning: 'low'});
  });

  it('keeps channel routes and tenant overrides across reload for IDs containing separators', () => {
    const config = JSON.parse(readFileSync(configPath, 'utf8'));
    const userId = 'human:team';
    const agentId = 'pulse:feishu';
    config.users[0].userId = userId;
    config.users[0].defaultAgent = agentId;
    config.users[0].agents[0].id = agentId;
    writeFileSync(configPath, JSON.stringify(config));
    const router = createRouter(configPath);
    router.setAgentModel(userId, agentId, 'opus');
    router.setAgentRuntime(userId, agentId, 'pi', 'feishu');
    router.setAgentConversationModel(userId, agentId, {provider: 'openai-codex', model: 'gpt-6-sol', reasoning: 'high'}, 'feishu');
    router.setAgentModel(userId, agentId, undefined, 'feishu');
    router.reload();
    for (const current of [router, createRouter(configPath)]) {
      const tg = current.resolveByBotAgent(agentId, '123')!.agent;
      const fs = current.resolveByFeishuAgent(agentId, 'ou_human')!.agent;
      expect(tg.runtime).toBe('cc');
      expect(tg.ccModel).toBe('opus');
      expect(tg.sessionId).toBe('original-tg');
      expect(current.getAgentModel(userId, agentId)).toBe('opus');
      expect(current.getAgentModel(userId, agentId, 'feishu')).toBeUndefined();
      expect(fs.runtime).toBe('pi');
      expect(fs.conversationRuntime!.model).toEqual({provider: 'openai-codex', model: 'gpt-6-sol', reasoning: 'high'});
      expect(fs.ccModel).toBeUndefined();
      expect(fs.feishu!.sessionId).toBe('original-feishu');
    }
  });

  it('gateway and cached channel handlers read the new Feishu route after reload', () => {
    const router = createRouter(configPath);
    const cached = router.getAgents('human')[0];
    router.setAgentRuntime('human', 'pulse', 'pi', 'feishu');
    router.reload();
    const fs = resolveInboundRoutingAgent(router, 'human', cached, {inboundChannel: 'feishu'});
    expect(fs.runtime).toBe('pi');
    expect(fs.conversationRuntime!.model!.model).toBe('gpt-6.1-sol');
    const view = createGatewayBindingView({users: router.getAllUsers()});
    expect(view.bindings.find(b => b.address.channel === 'telegram')!.defaultRuntimePreference).toEqual(['cc']);
    expect(view.bindings.find(b => b.address.channel === 'feishu')!.defaultRuntimePreference).toEqual(['pi']);
    expect(router.getAllFeishuBots()[0].agent.runtime).toBe('pi');
  });

  it('validates a nested Pi route even when TG uses CC', () => {
    const router = createRouter(configPath);
    router.setAgentRuntime('human', 'pulse', 'pi', 'feishu');
    const config = JSON.parse(readFileSync(configPath, 'utf8'));
    expect(() => validateConfig(config)).not.toThrow();
    delete config.users[0].agents[0].feishu.conversationRuntime.model.reasoning;
    expect(() => validateConfig(config)).toThrow('Pi runtime requires');
  });
});
