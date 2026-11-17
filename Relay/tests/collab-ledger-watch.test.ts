import { describe, it, expect, beforeEach, afterEach } from 'vitest';
import { execFileSync } from 'node:child_process';
import { createHash } from 'node:crypto';
import * as fs from 'node:fs';
import * as os from 'node:os';
import * as path from 'node:path';
import {
  collabInboxActor, collaborationActors, createCollabLedgerWatch, ledgerBinding, localGit,
  MIN_INTERVAL_MS, PENDING_LIMIT_MS,
} from '../src/collab-ledger-watch.js';
import type { JobTriggerStatus, ScheduledJob } from '../src/types.js';

const TRACKING = 'refs/remotes/origin/main';
let repo: string;

function git(...args: string[]): string {
  return execFileSync('git', ['-C', repo, '-c', 'user.name=t', '-c', 'user.email=t@example.com', ...args],
    { encoding: 'utf8' }).trim();
}

function entryPath(wi: string, revision: number): string {
  return `.harness/core/journals/${wi}/${String(revision).padStart(12, '0')}.json`;
}

function write(file: string, content: string | Buffer): void {
  fs.mkdirSync(path.dirname(path.join(repo, file)), { recursive: true });
  fs.writeFileSync(path.join(repo, file), content);
}

/** Commit journal entries (inline records) and move the tracking ref there. */
function publish(entries: Record<string, unknown[]>): string {
  for (const [file, events] of Object.entries(entries)) {
    write(file, JSON.stringify({ originals: {}, record: { at: '2026-11-03T00:00:00Z', events } }));
  }
  git('add', '-A');
  git('commit', '-q', '-m', 'ledger');
  const sha = git('rev-parse', 'HEAD');
  git('update-ref', TRACKING, sha);
  return sha;
}

const offered = (contract: string, by: string, ...recipients: string[]) => ({
  kind: 'CollaborationOffered',
  payload: { contract_id: contract, offered_by: { actor: by, session: 'session.x', surface: 's' }, main_only: true,
    recipients: recipients.map(actor => ({ principal: { actor } })) },
});
const responded = (contract: string, actor: string, decision: string) => ({
  kind: 'CollaborationResponded', payload: { contract_id: contract, actor, decision },
});

const submitted = (candidate: string, actor: string) => ({
  kind: 'PullRequestSubmitted', payload: { candidate_id: candidate, number: 7, author: { actor, session: 'session.x', surface: 's' } },
});
const reviewed = (candidate: string, mergeEffect: string | null = null) => ({
  kind: 'PullRequestReviewed', payload: { candidate_id: candidate, merge_effect: mergeEffect, review: {} },
});
const mergeRequested = (effect: string, candidate: string) => ({
  kind: 'EffectRequested', payload: { effect_id: effect, effect_kind: 'merge_pull_request', candidate_id: candidate },
});
const observedEffect = (effect: string) => ({ kind: 'EffectObserved', payload: { effect_id: effect, observation: { outcome: 'success' } } });

function inboxJob(id: string, actor: string, enabled = true): ScheduledJob {
  return {
    id, name: id, enabled, owner: actor, schedule: { expr: '3,33 * * * *', tz: 'Asia/Shanghai' },
    agent: { workspace: '/tmp', prompt: 'p', gate: { command: '/usr/bin/python3',
      args: ['-I', '-B', '/x/tools/collab-inbox/collab-inbox-gate.py', 'check', '--actor', actor, '--state', '/s.json'] } },
    delivery: { mode: 'none' },
  };
}

beforeEach(() => {
  repo = fs.mkdtempSync(path.join(os.tmpdir(), 'collab-ledger-watch-'));
  execFileSync('git', ['init', '-q', repo]);
  publish({ [entryPath('wi_1', 1)]: [{ kind: 'WorkItemCreated', payload: {} }] });
});
afterEach(() => { fs.rmSync(repo, { recursive: true, force: true }); });

describe('collabInboxActor', () => {
  it('names the actor of a collab-inbox gate job only', () => {
    expect(collabInboxActor(inboxJob('amanda-collab-inbox', 'amanda'))).toBe('amanda');
    const other = inboxJob('x', 'amanda');
    other.agent.gate!.args = ['/x/other-gate.py', 'check', '--actor', 'amanda'];
    expect(collabInboxActor(other)).toBeUndefined();
    const noGate = inboxJob('y', 'amanda');
    delete noGate.agent.gate;
    expect(collabInboxActor(noGate)).toBeUndefined();
  });
});

describe('ledgerBinding', () => {
  it('derives the remote-tracking ref of the installed production ledger', () => {
    const policy = path.join(repo, 'host-policy.json');
    fs.writeFileSync(policy, JSON.stringify({ production_ledger: {
      identity: 'github.com/o/r', ref: 'refs/heads/main', remote: 'origin', repository: '/srv/ledger' } }));
    expect(ledgerBinding(policy)).toEqual({ repository: '/srv/ledger', trackingRef: TRACKING });
    fs.writeFileSync(policy, JSON.stringify({ production_ledger: { ref: 'main', remote: 'origin', repository: '/srv' } }));
    expect(() => ledgerBinding(policy)).toThrow('invalid');
    fs.writeFileSync(policy, JSON.stringify({}));
    expect(ledgerBinding(policy)).toBeUndefined();
  });
});

describe('collaborationActors', () => {
  it('names recipients of an offer and the requester of an answer, never an acceptance', async () => {
    const base = git('rev-parse', 'HEAD');
    const offer = publish({ [entryPath('wi_1', 2)]: [offered('op_a', 'amanda', 'pulse', 'hestia')] });
    expect([...await collaborationActors(localGit(repo), base, offer)].sort()).toEqual(['hestia', 'pulse']);

    const accepted = publish({ [entryPath('wi_1', 3)]: [responded('op_a', 'pulse', 'accept')] });
    expect([...await collaborationActors(localGit(repo), offer, accepted)]).toEqual([]);

    // The offer is in an earlier entry: the requester is read from the WI's journal.
    const completed = publish({ [entryPath('wi_1', 4)]: [responded('op_a', 'pulse', 'complete')] });
    expect([...await collaborationActors(localGit(repo), accepted, completed)]).toEqual(['amanda']);

    const unrelated = publish({ [entryPath('wi_2', 1)]: [{ kind: 'WorkItemCreated', payload: {} }] });
    expect([...await collaborationActors(localGit(repo), completed, unrelated)]).toEqual([]);
  });

  it('names the Agent that registered a round when it is reviewed or its merge is read back', async () => {
    const base = git('rev-parse', 'HEAD');
    const registered = publish({ [entryPath('wi_5', 1)]: [submitted('op_r1', 'pulse')] });
    // Registration alone concerns nobody: the Agent did it itself.
    expect([...await collaborationActors(localGit(repo), base, registered)]).toEqual([]);
    const review = publish({ [entryPath('wi_5', 2)]: [reviewed('op_r1')] });
    expect([...await collaborationActors(localGit(repo), registered, review)]).toEqual(['pulse']);
    const merge = publish({ [entryPath('wi_5', 3)]: [reviewed('op_r1', 'op_m.merge'), mergeRequested('op_m.merge', 'op_r1')] });
    const merged = publish({ [entryPath('wi_5', 4)]: [observedEffect('op_m.merge')] });
    // The merge request and the round's registration are found in earlier entries.
    expect([...await collaborationActors(localGit(repo), merge, merged)]).toEqual(['pulse']);
    const other = publish({ [entryPath('wi_5', 5)]: [observedEffect('op_r1.review')] });
    expect([...await collaborationActors(localGit(repo), merged, other)]).toEqual([]);
  });

  it('reads a packed entry from its ledger objects', async () => {
    const base = git('rev-parse', 'HEAD');
    const content = Buffer.from(JSON.stringify({ events: [offered('op_b', 'duke', 'aki')] }));
    const sha = createHash('sha256').update(content).digest('hex');
    write(`.harness/core/objects/${sha.slice(0, 2)}/${sha}`, content);
    write(entryPath('wi_3', 1), JSON.stringify({ originals: {}, entry: { sha256: sha, size: content.length, chunks: [sha] } }));
    git('add', '-A');
    git('commit', '-q', '-m', 'packed');
    expect([...await collaborationActors(localGit(repo), base, git('rev-parse', 'HEAD'))]).toEqual(['aki']);
  });

  it('finds the requester of a packed offer when a later entry answers it', async () => {
    const content = Buffer.from(JSON.stringify({ events: [offered('op_c', 'amanda', 'pulse')] }));
    const sha = createHash('sha256').update(content).digest('hex');
    write(`.harness/core/objects/${sha.slice(0, 2)}/${sha}`, content);
    write(entryPath('wi_4', 1), JSON.stringify({ originals: {}, entry: { sha256: sha, size: content.length, chunks: [sha] } }));
    git('add', '-A');
    git('commit', '-q', '-m', 'packed offer');
    const offeredAt = git('rev-parse', 'HEAD');
    const completed = publish({ [entryPath('wi_4', 2)]: [responded('op_c', 'pulse', 'complete')] });
    expect([...await collaborationActors(localGit(repo), offeredAt, completed)]).toEqual(['amanda']);
  });
});

describe('createCollabLedgerWatch', () => {
  it('triggers the inbox job of each concerned Agent once the tracking ref moves', async () => {
    let clock = 1_000_000;
    const answers: Record<string, JobTriggerStatus> = {};
    const triggered: string[] = [];
    const watch = createCollabLedgerWatch({
      repository: repo, trackingRef: TRACKING, now: () => clock, log: () => {},
      jobs: () => [inboxJob('amanda-collab-inbox', 'amanda'), inboxJob('pulse-collab-inbox', 'pulse'),
        inboxJob('hestia-collab-inbox', 'hestia', false)],
      trigger: id => { triggered.push(id); return answers[id] ?? 'started'; },
    });
    await watch.tick(); // baseline only
    expect(triggered).toEqual([]);

    answers['pulse-collab-inbox'] = 'running';
    publish({ [entryPath('wi_1', 2)]: [offered('op_a', 'amanda', 'pulse', 'hestia')] });
    await watch.tick();
    // hestia's job is disabled; pulse's is running and stays owed.
    expect(triggered).toEqual(['pulse-collab-inbox']);

    await watch.tick();
    expect(triggered).toEqual(['pulse-collab-inbox', 'pulse-collab-inbox']);
    answers['pulse-collab-inbox'] = 'started';
    await watch.tick();
    await watch.tick();
    expect(triggered).toEqual(['pulse-collab-inbox', 'pulse-collab-inbox', 'pulse-collab-inbox']);

    // A second event within MIN_INTERVAL_MS waits for the interval.
    publish({ [entryPath('wi_1', 3)]: [offered('op_c', 'amanda', 'pulse')] });
    await watch.tick();
    expect(triggered).toHaveLength(3);
    clock += MIN_INTERVAL_MS;
    await watch.tick();
    expect(triggered).toHaveLength(4);

    // The requester's job runs once its contract is answered.
    clock += MIN_INTERVAL_MS;
    publish({ [entryPath('wi_1', 4)]: [responded('op_a', 'pulse', 'return')] });
    await watch.tick();
    expect(triggered.at(-1)).toBe('amanda-collab-inbox');
  });

  it('runs the inbox job of an Agent on request, as for a ledger event', async () => {
    let clock = 0;
    const answers: Record<string, JobTriggerStatus> = { 'pulse-collab-inbox': 'running' };
    const triggered: string[] = [];
    const watch = createCollabLedgerWatch({
      repository: repo, trackingRef: TRACKING, now: () => clock, log: () => {},
      jobs: () => [inboxJob('pulse-collab-inbox', 'pulse'), inboxJob('amanda-collab-inbox', 'amanda')],
      trigger: id => { triggered.push(id); return answers[id] ?? 'started'; },
    });
    watch.request(['pulse']);
    expect(triggered).toEqual(['pulse-collab-inbox']);
    // Still running: retried on the next tick until it starts.
    answers['pulse-collab-inbox'] = 'started';
    await watch.tick();
    expect(triggered).toEqual(['pulse-collab-inbox', 'pulse-collab-inbox']);
    await watch.tick();
    expect(triggered).toHaveLength(2);
  });

  it('gives up on a job held past the pending limit and leaves it to the schedule', async () => {
    let clock = 0;
    const triggered: string[] = [];
    const watch = createCollabLedgerWatch({
      repository: repo, trackingRef: TRACKING, now: () => clock, log: () => {},
      jobs: () => [inboxJob('pulse-collab-inbox', 'pulse')],
      trigger: id => { triggered.push(id); return 'held'; },
    });
    await watch.tick();
    publish({ [entryPath('wi_1', 2)]: [offered('op_a', 'amanda', 'pulse')] });
    await watch.tick();
    clock += PENDING_LIMIT_MS + 1;
    await watch.tick();
    await watch.tick();
    expect(triggered).toHaveLength(2);
  });

  it('does not trigger anything when the tracking ref cannot be read', async () => {
    const triggered: string[] = [];
    const logs: Array<Record<string, unknown>> = [];
    const watch = createCollabLedgerWatch({
      repository: repo, trackingRef: 'refs/remotes/origin/absent', log: e => logs.push(e),
      jobs: () => [inboxJob('pulse-collab-inbox', 'pulse')],
      trigger: id => { triggered.push(id); return 'started'; },
    });
    await watch.tick();
    expect(triggered).toEqual([]);
    expect(logs.some(e => String(e['msg']).includes('read failed'))).toBe(true);
  });
});
