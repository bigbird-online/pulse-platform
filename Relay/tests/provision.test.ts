import { describe, it, expect, beforeEach, afterEach } from 'vitest';
import { existsSync, mkdirSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from 'node:fs';
import { join } from 'node:path';
import { tmpdir } from 'node:os';
import { provisionAgent, ProvisionError, type ProvisionRequest } from '../src/provision.js';

let testDir: string;
let configPath: string;

beforeEach(() => {
  testDir = mkdtempSync(join(tmpdir(), 'relay-provision-test-'));
  configPath = join(testDir, 'relay.json');
  writeFileSync(configPath, JSON.stringify({
    users: [
      {
        userId: 'owner',
        chatId: '111',
        defaultAgent: 'main',
        agents: [
          { id: 'main', name: 'Duke', emoji: '🎲', botToken: 'fake', workspace: '/tmp/duke', sessionId: 'uuid-duke' },
        ],
      },
      { userId: 'eric', defaultAgent: 'art', agents: [{ id: 'art', name: 'Artificer', emoji: '🔧', workspace: '/tmp/art' }] },
    ],
  }, null, 2));
});

afterEach(() => {
  rmSync(testDir, { recursive: true, force: true });
});

function baseReq(overrides: Partial<ProvisionRequest> = {}): ProvisionRequest {
  return {
    configPath,
    userId: 'owner',
    agentId: 'newbot',
    name: 'NewBot',
    emoji: '🧪',
    workspace: join(testDir, 'newbot-ws'),
    channel: 'telegram',
    botToken: 'token-x',
    uuid: () => 'fixed-uuid-1',
    nowMs: Date.parse('2026-05-01T15:00:00Z'),
    ...overrides,
  };
}

describe('provisionAgent', () => {
  it('rejects unknown user', () => {
    expect(() => provisionAgent(baseReq({ userId: 'ghost' }))).toThrow(ProvisionError);
    expect(() => provisionAgent(baseReq({ userId: 'ghost' }))).toThrow(/USER_NOT_FOUND|not present/);
  });

  it('rejects duplicate agent id under same user', () => {
    const err = (() => {
      try { provisionAgent(baseReq({ agentId: 'main' })); return null; }
      catch (e) { return e as ProvisionError; }
    })();
    expect(err).toBeInstanceOf(ProvisionError);
    expect(err?.code).toBe('AGENT_DUPLICATE');
  });

  function expectCode(req: ProvisionRequest, code: string): void {
    let captured: ProvisionError | null = null;
    try { provisionAgent(req); } catch (e) { captured = e as ProvisionError; }
    expect(captured).toBeInstanceOf(ProvisionError);
    expect(captured?.code).toBe(code);
  }

  it('rejects invalid agentId pattern', () => {
    expectCode(baseReq({ agentId: 'Bad_Id!' }), 'ID_INVALID');
  });

  it('telegram channel without botToken fails fast', () => {
    expectCode(baseReq({ botToken: undefined }), 'TG_TOKEN_MISSING');
  });

  it('feishu channel without credentials fails fast', () => {
    expectCode(baseReq({
      channel: 'feishu',
      botToken: undefined,
      feishuAppId: 'cli_x',
    }), 'FEISHU_INCOMPLETE');
  });

  it('writes new TG agent, creates workspace, leaves backup', () => {
    const outcome = provisionAgent(baseReq());

    expect(outcome.dryRun).toBe(false);
    expect(outcome.agent.id).toBe('newbot');
    expect(outcome.agent.sessionId).toBe('fixed-uuid-1');
    expect(outcome.agent.botToken).toBe('token-x');
    expect(outcome.agent.feishu).toBeUndefined();
    expect(existsSync(join(testDir, 'newbot-ws'))).toBe(true);
    expect(outcome.backupPath).toBeDefined();
    expect(existsSync(outcome.backupPath!)).toBe(true);

    const config = JSON.parse(readFileSync(configPath, 'utf-8'));
    const owner = config.users.find((u: any) => u.userId === 'owner');
    expect(owner.agents).toHaveLength(2);
    expect(owner.agents[1]).toMatchObject({ id: 'newbot', sessionId: 'fixed-uuid-1' });
  });

  it('feishu provisioning binds appId/secret + open_id', () => {
    const outcome = provisionAgent(baseReq({
      agentId: 'feishu-bot',
      channel: 'feishu',
      botToken: undefined,
      feishuAppId: 'cli_a',
      feishuAppSecret: 'secret_b',
      feishuOpenId: 'ou_c',
    }));

    expect(outcome.agent.feishu).toEqual({ appId: 'cli_a', appSecret: 'secret_b', sessionId: 'fixed-uuid-1' });
    expect(outcome.agent.feishuOpenId).toBe('ou_c');
    expect(outcome.agent.botToken).toBeUndefined();
  });

  it('dry-run does not write config or backup', () => {
    const before = readFileSync(configPath, 'utf-8');
    const outcome = provisionAgent(baseReq({ dryRun: true }));

    expect(outcome.dryRun).toBe(true);
    expect(outcome.backupPath).toBeUndefined();
    expect(readFileSync(configPath, 'utf-8')).toBe(before);
    expect(existsSync(join(testDir, 'newbot-ws'))).toBe(false);
  });

  it('records monthlyBudgetUsd when provided and > 0', () => {
    const outcome = provisionAgent(baseReq({ monthlyBudgetUsd: 25 }));
    expect(outcome.agent.monthlyBudgetUsd).toBe(25);
  });

  it('omits monthlyBudgetUsd when 0 or unset', () => {
    expect(provisionAgent(baseReq()).agent.monthlyBudgetUsd).toBeUndefined();
    expect(provisionAgent(baseReq({ agentId: 'zerobot', monthlyBudgetUsd: 0 })).agent.monthlyBudgetUsd).toBeUndefined();
  });

  it('inherits maxTurns / allowedTools / runtime when provided', () => {
    const outcome = provisionAgent(baseReq({ maxTurns: 50, allowedTools: ['Read', 'Bash'], runtime: 'codex' }));
    expect(outcome.agent.maxTurns).toBe(50);
    expect(outcome.agent.allowedTools).toEqual(['Read', 'Bash']);
    expect(outcome.agent.runtime).toBe('codex');
  });

  it('sets defaultAgent on user when previously absent', () => {
    writeFileSync(configPath, JSON.stringify({
      users: [{ userId: 'newbie', agents: [] }],
    }, null, 2));

    const outcome = provisionAgent(baseReq({ userId: 'newbie' }));

    const config = JSON.parse(readFileSync(configPath, 'utf-8'));
    const newbie = config.users.find((u: any) => u.userId === 'newbie');
    expect(newbie.defaultAgent).toBe('newbot');
    expect(newbie.agents).toHaveLength(1);
    expect(outcome.agent.id).toBe('newbot');
  });

  it('keeps existing defaultAgent when present', () => {
    const outcome = provisionAgent(baseReq());
    const config = JSON.parse(readFileSync(configPath, 'utf-8'));
    const owner = config.users.find((u: any) => u.userId === 'owner');
    expect(owner.defaultAgent).toBe('main');
    expect(outcome.agent.id).toBe('newbot');
  });

  it('fails when config path does not exist', () => {
    const missing = join(testDir, 'absent', 'relay.json');
    expectCode(baseReq({ configPath: missing }), 'CONFIG_MISSING');
  });
});
