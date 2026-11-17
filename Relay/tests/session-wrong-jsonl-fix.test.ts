import { describe, it, expect, beforeEach, afterEach } from 'vitest';
import { writeFileSync, mkdirSync, rmSync } from 'node:fs';
import { join } from 'node:path';
import { tmpdir } from 'node:os';
import { createRouter } from '../src/router.js';
import { readSessionLastTimestamp, isResumeJsonlStale } from '../src/pool.js';

// Regression coverage for the 2026-06-26 wrong-jsonl bug
// (memo: pulse-session-bound-to-wrong-jsonl-after-depoison).
//
// Root cause: a dual-channel agent (botToken + feishu) shares one AgentConfig.
// overrideSessionId's legacy unscoped branch mirrored a TG session onto
// agent.feishu.sessionId, so restoring a stale TG sid from state.json clobbered
// the fresh feishu sid → feishu resumed a days-old jsonl.

const testDir = join(tmpdir(), 'relay-wrong-jsonl-fix-' + Date.now());

// Dual-channel agent: has BOTH botToken (TG) and feishu.
const dualChannelConfig = {
  users: [
    {
      userId: 'owner',
      chatId: '111222333',
      feishuOpenId: 'ou_owner',
      defaultAgent: 'pulse',
      agents: [
        {
          id: 'pulse',
          name: 'Pulse',
          emoji: '\u{1F52E}',
          botToken: 'fake-pulse-tg-token',
          workspace: '/tmp/ws',
          sessionId: 'tg-session-fresh',
          feishu: { appId: 'cli_xxx', appSecret: 'secret', sessionId: 'feishu-session-fresh' },
        },
      ],
    },
  ],
};

function writeConfig(config: object = dualChannelConfig): string {
  const path = join(testDir, 'relay.json');
  writeFileSync(path, JSON.stringify(config));
  return path;
}

beforeEach(() => { mkdirSync(testDir, { recursive: true }); });
afterEach(() => { rmSync(testDir, { recursive: true, force: true }); });

describe('overrideSessionId channel scoping (待修点2: no cross-pollination)', () => {
  it('feishu-scoped override touches ONLY feishu.sessionId, not TG sessionId', () => {
    const router = createRouter(writeConfig());
    router.overrideSessionId('owner', 'pulse', 'feishu-restored', 'feishu');
    const agent = router.getAgents('owner').find(a => a.id === 'pulse')!;
    expect(agent.feishu!.sessionId).toBe('feishu-restored');
    expect(agent.sessionId).toBe('tg-session-fresh'); // untouched
  });

  it('tg-scoped override touches ONLY TG sessionId, not feishu.sessionId', () => {
    const router = createRouter(writeConfig());
    router.overrideSessionId('owner', 'pulse', 'tg-restored', 'tg');
    const agent = router.getAgents('owner').find(a => a.id === 'pulse')!;
    expect(agent.sessionId).toBe('tg-restored');
    expect(agent.feishu!.sessionId).toBe('feishu-session-fresh'); // untouched — the bug
  });

  it('unscoped override on a dual-channel agent no longer mirrors onto feishu', () => {
    // This is the exact crash path: restoring the stale TG sid unscoped must NOT
    // overwrite the fresh feishu sid.
    const router = createRouter(writeConfig());
    router.overrideSessionId('owner', 'pulse', 'stale-tg-from-state');
    const agent = router.getAgents('owner').find(a => a.id === 'pulse')!;
    expect(agent.sessionId).toBe('stale-tg-from-state');
    expect(agent.feishu!.sessionId).toBe('feishu-session-fresh'); // NOT clobbered
  });

  it('feishu-only agent (no botToken) still updates its feishu session when unscoped', () => {
    const feishuOnly = {
      users: [{
        userId: 'eric', feishuOpenId: 'ou_eric', defaultAgent: 'pulse',
        agents: [{
          id: 'pulse', name: 'Pulse', emoji: '\u{1F52E}', channel: 'feishu',
          workspace: '/tmp/ws', feishu: { appId: 'c', appSecret: 's', sessionId: 'old' },
        }],
      }],
    };
    const router = createRouter(writeConfig(feishuOnly));
    router.overrideSessionId('eric', 'pulse', 'new-feishu');
    const agent = router.getAgents('eric').find(a => a.id === 'pulse')!;
    expect(agent.feishu!.sessionId).toBe('new-feishu');
  });

  it('configuration reload retains both current channel sessions while adopting new configuration', () => {
    const path = writeConfig();
    const router = createRouter(path);
    router.overrideSessionId('owner', 'pulse', 'current-tg', 'tg');
    router.overrideSessionId('owner', 'pulse', 'current-feishu', 'feishu');
    const changed = structuredClone(dualChannelConfig);
    changed.users[0].agents[0].name = 'Updated Pulse';
    writeFileSync(path, JSON.stringify(changed));
    router.reload();
    const agent = router.getAgents('owner')[0];
    expect(agent.name).toBe('Updated Pulse');
    expect(agent.sessionId).toBe('current-tg');
    expect(agent.feishu!.sessionId).toBe('current-feishu');
    // A new process restores the same identities from each persisted pool key.
    const restarted = createRouter(path);
    restarted.overrideSessionId('owner', 'pulse', agent.sessionId!, 'tg');
    restarted.overrideSessionId('owner', 'pulse', agent.feishu!.sessionId, 'feishu');
    expect(restarted.getAgents('owner')[0]).toMatchObject({
      sessionId: 'current-tg', feishu: { sessionId: 'current-feishu' },
    });
  });
});

describe('resume staleness sanity check (待修点3)', () => {
  it('readSessionLastTimestamp returns the newest record timestamp (epoch ms)', () => {
    const f = join(testDir, 'sess.jsonl');
    writeFileSync(f, [
      JSON.stringify({ type: 'user', uuid: 'a', timestamp: '2026-06-20T02:55:12.036Z' }),
      JSON.stringify({ type: 'assistant', uuid: 'b', parentUuid: 'a', timestamp: '2026-06-20T03:01:00.000Z' }),
      JSON.stringify({ type: 'summary', leafUuid: 'b' }), // no timestamp — skipped
      '',
    ].join('\n'));
    expect(readSessionLastTimestamp(f)).toBe(Date.parse('2026-06-20T03:01:00.000Z'));
  });

  it('readSessionLastTimestamp returns null for a missing file', () => {
    expect(readSessionLastTimestamp(join(testDir, 'nope.jsonl'))).toBeNull();
  });

  it('flags stale when jsonl is far older than expected lastActivity', () => {
    const jsonlLast = Date.parse('2026-06-20T03:00:00Z');
    const expected = Date.parse('2026-06-26T13:27:00Z'); // 6 days newer
    expect(isResumeJsonlStale(jsonlLast, expected, 12 * 60 * 60 * 1000)).toBe(true);
  });

  it('does NOT flag a normal overnight gap (under threshold)', () => {
    const jsonlLast = Date.parse('2026-06-26T01:00:00Z');
    const expected = Date.parse('2026-06-26T09:00:00Z'); // 8h gap
    expect(isResumeJsonlStale(jsonlLast, expected, 12 * 60 * 60 * 1000)).toBe(false);
  });

  it('is conservative: never flags when a timestamp is missing or jsonl is newer', () => {
    expect(isResumeJsonlStale(null, Date.now(), 1000)).toBe(false);
    expect(isResumeJsonlStale(Date.now(), undefined, 1000)).toBe(false);
    // jsonl newer than expected (expected - last is negative) → not stale
    expect(isResumeJsonlStale(Date.parse('2026-06-26T10:00:00Z'), Date.parse('2026-06-26T09:00:00Z'), 1000)).toBe(false);
  });
});
