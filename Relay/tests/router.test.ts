import { describe, it, expect, beforeEach, afterEach } from 'vitest';
import { writeFileSync, mkdirSync, rmSync } from 'node:fs';
import { join } from 'node:path';
import { tmpdir } from 'node:os';
import { createRouter } from '../src/router.js';

const testDir = join(tmpdir(), 'relay-router-test-' + Date.now());

// --- v2 config format: users + agents ---
const sampleConfig = {
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
          workspace: '/home/owner/duke-space',
          sessionId: 'uuid-duke',
          maxTurns: 20,
        },
        {
          id: 'ken',
          name: 'Ken',
          emoji: '\u{1F527}',
          botToken: 'fake-ken-token',
          workspace: '/home/owner/ken-space',
          sessionId: 'uuid-ken',
        },
        {
          id: 'aki',
          name: 'Aki',
          emoji: '\u{1F342}',
          botToken: 'fake-aki-token',
          workspace: '/home/owner/aki-space',
          sessionId: 'uuid-aki',
        },
      ],
    },
    {
      userId: 'eric',
      chatId: '444555666',
      defaultAgent: 'main',
      agents: [
        {
          id: 'main',
          name: 'Duke',
          emoji: '\u{1F3B2}',
          botToken: 'fake-duke-token',
          workspace: '/home/member/duke-space',
          sessionId: 'uuid-eric-duke',
        },
      ],
    },
  ],
};

function writeConfig(filename = 'relay.json'): string {
  const path = join(testDir, filename);
  writeFileSync(path, JSON.stringify(sampleConfig));
  return path;
}

beforeEach(() => {
  mkdirSync(testDir, { recursive: true });
});

afterEach(() => {
  rmSync(testDir, { recursive: true, force: true });
});

describe('createRouter (v2 multi-agent)', () => {
  it('preserves configured agent ids containing the legacy key separator', () => {
    const config = structuredClone(sampleConfig);
    config.users[0].agents[1].id = 'a:b';
    const file = join(testDir, 'legacy-id.json');
    writeFileSync(file, JSON.stringify(config));
    const router = createRouter(file);
    expect(router.switchAgent('owner', 'a:b')?.id).toBe('a:b');
    expect(router.poolKey('owner', 'a:b')).toBe('owner:a:b');
  });
  // [P0-001] Unit Test-001/002/003: relay.json type validation
  it('loads relay.json with users and agents', () => {
    const router = createRouter(writeConfig());
    const users = router.getAllUsers();
    expect(users).toHaveLength(2);
    expect(users[0].userId).toBe('owner');
    expect(users[0].agents).toHaveLength(3);
  });

  // [P0-002] Unit Test-004: resolveUser by chatId
  it('resolveUser returns correct UserConfig by chatId', () => {
    const router = createRouter(writeConfig());
    const user = router.resolveUser('111222333');
    expect(user).toBeDefined();
    expect(user!.userId).toBe('owner');
    expect(user!.defaultAgent).toBe('main');
  });

  // [P0-002] Unit Test-006: resolveUser unknown chatId
  it('resolveUser returns undefined for unknown chatId', () => {
    const router = createRouter(writeConfig());
    expect(router.resolveUser('999999999')).toBeUndefined();
  });

  // [P0-002] Unit Test-007: getActiveAgent returns defaultAgent initially
  it('getActiveAgent returns defaultAgent initially', () => {
    const router = createRouter(writeConfig());
    const agent = router.getActiveAgent('owner');
    expect(agent).toBeDefined();
    expect(agent!.id).toBe('main');
    expect(agent!.name).toBe('Duke');
    expect(agent!.emoji).toBe('\u{1F3B2}');
  });

  // [P0-002] Unit Test-008: switchAgent changes active agent
  it('switchAgent changes active agent', () => {
    const router = createRouter(writeConfig());
    const result = router.switchAgent('owner', 'ken');
    expect(result).toBeDefined();
    expect(result!.id).toBe('ken');
    expect(result!.name).toBe('Ken');

    // getActiveAgent should now return ken
    const active = router.getActiveAgent('owner');
    expect(active!.id).toBe('ken');
  });

  // [P0-002] Unit Test-009: switchAgent invalid agentId returns undefined
  it('switchAgent returns undefined for invalid agentId', () => {
    const router = createRouter(writeConfig());
    const result = router.switchAgent('owner', 'nobody');
    expect(result).toBeUndefined();

    // active agent unchanged
    const active = router.getActiveAgent('owner');
    expect(active!.id).toBe('main');
  });

  // [P0-002] Unit Test-010: poolKey format
  it('poolKey returns userId:agentId format', () => {
    const router = createRouter(writeConfig());
    expect(router.poolKey('owner', 'ken')).toBe('owner:ken');
  });

  // [P0-002] Unit Test-011: getAgents returns all agents for user
  it('getAgents returns all agents for a user', () => {
    const router = createRouter(writeConfig());
    const agents = router.getAgents('owner');
    expect(agents).toHaveLength(3);
    expect(agents.map(a => a.id)).toEqual(['main', 'ken', 'aki']);
  });

  it('getAgents returns empty array for unknown user', () => {
    const router = createRouter(writeConfig());
    expect(router.getAgents('unknown')).toEqual([]);
  });

  // [P0-002] Unit Test-012: config file missing throws
  it('throws when config file does not exist', () => {
    expect(() => createRouter('/nonexistent/relay.json')).toThrow();
  });

  it('throws when JSON is invalid', () => {
    const path = join(testDir, 'bad.json');
    writeFileSync(path, 'not json');
    expect(() => createRouter(path)).toThrow();
  });

  // [P0-002] Unit Test-005: resolveUser second user
  it('resolveUser resolves second user by chatId', () => {
    const router = createRouter(writeConfig());
    const user = router.resolveUser('444555666');
    expect(user).toBeDefined();
    expect(user!.userId).toBe('eric');
  });

  // getActiveAgent for unknown user
  it('getActiveAgent returns undefined for unknown userId', () => {
    const router = createRouter(writeConfig());
    expect(router.getActiveAgent('unknown')).toBeUndefined();
  });

  // switchAgent for unknown user
  it('switchAgent returns undefined for unknown userId', () => {
    const router = createRouter(writeConfig());
    expect(router.switchAgent('unknown', 'main')).toBeUndefined();
  });

  // resolveByBotAgent
  it('resolveByBotAgent finds user+agent by agentId and chatId', () => {
    const router = createRouter(writeConfig());
    const result = router.resolveByBotAgent('ken', '111222333');
    expect(result).toBeDefined();
    expect(result!.user.userId).toBe('owner');
    expect(result!.agent.id).toBe('ken');
  });

  it('resolveByBotAgent returns undefined for wrong chatId', () => {
    const router = createRouter(writeConfig());
    expect(router.resolveByBotAgent('ken', '999999')).toBeUndefined();
  });

  it('resolveByBotAgent returns undefined for wrong agentId', () => {
    const router = createRouter(writeConfig());
    expect(router.resolveByBotAgent('nobody', '111222333')).toBeUndefined();
  });

  // resetSession
  it('resetSession assigns new UUID and returns it', () => {
    const router = createRouter(writeConfig());
    const oldSession = router.getActiveAgent('owner')!.sessionId;
    const newId = router.resetSession('owner', 'main');
    expect(newId).toBeDefined();
    expect(newId).not.toBe(oldSession);
    expect(router.getActiveAgent('owner')!.sessionId).toBe(newId);
  });

  it('resetSession returns undefined for unknown agent', () => {
    const router = createRouter(writeConfig());
    expect(router.resetSession('owner', 'nobody')).toBeUndefined();
  });

  // --- /model default three-state override (router.ts override file) ---
  describe('agent-models.json override (three-state)', () => {
    let savedRelayHome: string | undefined;
    let savedHome: string | undefined;
    beforeEach(() => {
      savedRelayHome = process.env['RELAY_HOME'];
      savedHome = process.env['HOME'];
      process.env['RELAY_HOME'] = testDir;
    });
    afterEach(() => {
      if (savedRelayHome === undefined) delete process.env['RELAY_HOME'];
      else process.env['RELAY_HOME'] = savedRelayHome;
      if (savedHome === undefined) delete process.env['HOME'];
      else process.env['HOME'] = savedHome;
    });

    function writeConfigWithCcModel(ccModel: string): string {
      const cfg = JSON.parse(JSON.stringify(sampleConfig));
      cfg.users[0].agents[0].ccModel = ccModel; // owner:main
      const path = join(testDir, 'relay-cc.json');
      writeFileSync(path, JSON.stringify(cfg));
      return path;
    }

    it('setAgentModel(undefined) persists null sentinel so default survives restart', () => {
      // Simulate /model default on an agent that has ccModel=fable-5 in relay.json.
      const r1 = createRouter(writeConfigWithCcModel('claude-fable-5'));
      expect(r1.getAgentModel('owner', 'main')).toBe('claude-fable-5');
      r1.setAgentModel('owner', 'main', undefined);
      expect(r1.getAgentModel('owner', 'main')).toBeUndefined();

      // Reload (simulates Relay restart): new router instance reads the same files.
      const r2 = createRouter(writeConfigWithCcModel('claude-fable-5'));
      expect(r2.getAgentModel('owner', 'main')).toBeUndefined();
      // And the in-memory agent.ccModel was cleared during merge, so any spawn
      // built off this agent will not pass --model.
      const bot = r2.getAllAgentBots().find(b => b.agent.id === 'main' && b.authorizedChatIds.includes('111222333'));
      expect(bot?.agent.ccModel).toBeUndefined();
    });

    it('setAgentModel(string) overrides relay.json ccModel across restart', () => {
      const r1 = createRouter(writeConfigWithCcModel('claude-fable-5'));
      r1.setAgentModel('owner', 'main', 'opus');
      expect(r1.getAgentModel('owner', 'main')).toBe('opus');

      const r2 = createRouter(writeConfigWithCcModel('claude-fable-5'));
      expect(r2.getAgentModel('owner', 'main')).toBe('opus');
    });

    it('retains model overrides for configured ids containing a colon', () => {
      const config = structuredClone(sampleConfig);
      config.users[0].agents[1].id = 'a:b';
      const file = join(testDir, 'legacy-model-id.json');
      writeFileSync(file, JSON.stringify(config));
      createRouter(file).setAgentModel('owner', 'a:b', 'opus');
      expect(createRouter(file).switchAgent('owner', 'a:b')?.ccModel).toBe('opus');
    });

    it('no override leaves relay.json ccModel intact', () => {
      const r = createRouter(writeConfigWithCcModel('claude-fable-5'));
      expect(r.getAgentModel('owner', 'main')).toBe('claude-fable-5');
    });
  });

  // getAllAgentBots
  it('getAllAgentBots returns unique agents with authorized chatIds', () => {
    const router = createRouter(writeConfig());
    const bots = router.getAllAgentBots();
    // main appears for both owner and eric
    const mainBot = bots.find(b => b.agent.id === 'main');
    expect(mainBot).toBeDefined();
    expect(mainBot!.authorizedChatIds).toContain('111222333');
    expect(mainBot!.authorizedChatIds).toContain('444555666');
    // ken only for owner
    const kenBot = bots.find(b => b.agent.id === 'ken');
    expect(kenBot).toBeDefined();
    expect(kenBot!.authorizedChatIds).toEqual(['111222333']);
  });
});
