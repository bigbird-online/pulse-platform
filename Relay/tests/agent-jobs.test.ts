import { mkdtempSync, readFileSync, realpathSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { describe, expect, it, vi } from 'vitest';
import { applyAgentJobs, MAX_AGENT_JOBS, type AgentJobRequest } from '../src/agent-jobs.js';
import { createAgentJobsVerifier, issueAgentJobsKey } from '../src/agent-jobs-auth.js';
import { startRelayHttpServer } from '../src/http-server.js';
import type { Router } from '../src/router.js';
import { runJobsTool, signedRequest } from '../runtime/pulse-jobs-tool.mjs';

const GATE = '/env/bin/python';
function inbox(owner: string) {
  return {
    id: `${owner}-collab-inbox`, name: 'inbox', enabled: true, owner, schedule: { expr: '15,45 * * * *', tz: 'Asia/Shanghai' },
    agent: { workspace: `/ws/${owner}`, prompt: 'p', gate: { command: GATE, skipExitCode: 3,
      args: ['-I', '-B', '/tools/collab-inbox-gate.py', 'check', '--actor', owner, '--state', '/s', '--harness', '/bin/harness-native'] } },
    delivery: { mode: 'none', progressNotice: true, to: '1000000001' },
  };
}

function rig(extra: object[] = []) {
  const dir = mkdtempSync(join(tmpdir(), 'agent-jobs-'));
  const cronJobsPath = join(dir, 'cron-jobs.json');
  const controlPath = join(dir, 'automation-control.json');
  writeFileSync(cronJobsPath, JSON.stringify({ version: 2, jobs: [inbox('ken'), inbox('aki'),
    { id: 'ken-heartbeat', name: 'hb', enabled: true, owner: 'ken', schedule: { expr: '30 9 * * 1', tz: 'Asia/Shanghai' },
      agent: { workspace: '/ws/ken', prompt: 'hb' }, delivery: { mode: 'none' } }, ...extra] }));
  writeFileSync(controlPath, JSON.stringify({ schema: 'pulse_automation_control/v1', automatic_enabled: true, revision: 3,
    reason: 'r', decision_ref: 'd', updated_at: '2026-11-16T00:00:00Z', entries: {} }), { mode: 0o600 });
  const reload = vi.fn();
  let refusal: string | undefined;
  const run = (request: Partial<AgentJobRequest>) => applyAgentJobs(
    { actor: 'ken', session: 'session.ken', surface: 'ownermac', action: 'list', ...request } as AgentJobRequest, {
    cronJobsPath, controlPath, workspaceFor: owner => `/ws/${owner}`, reload, refusal: () => refusal,
    hosted: (actor, session, surface) => session === `session.${actor}` && surface === 'ownermac',
    now: () => new Date('2026-11-16T08:00:00Z'),
  });
  return {
    run, reload, controlPath, cronJobsPath, refuse: (why: string) => { refusal = why; },
    jobs: () => JSON.parse(readFileSync(cronJobsPath, 'utf8')).jobs as Array<Record<string, any>>,
    control: () => JSON.parse(readFileSync(controlPath, 'utf8')),
  };
}

const WAKE = { job_id: 'ken-wake-morning', name: '早班', schedule: { expr: '0 10 * * *' }, prompt: '按 HEARTBEAT.md 做早班',
  wake: { workItemId: 'wi_20261022_664' } };

describe('agent-managed jobs', () => {
  it('adds a wake job with the inbox gate and reminder target, registers it and reloads', () => {
    const r = rig();
    expect(r.run({ action: 'add', ...WAKE })).toMatchObject({ job: { id: 'ken-wake-morning', kind: 'wake', managed: true } });
    const job = r.jobs().find(j => j.id === 'ken-wake-morning')!;
    expect(job).toMatchObject({ owner: 'ken', managedBy: 'agent', wake: { workItemId: 'wi_20261022_664' },
      delivery: { mode: 'none', to: '1000000001' }, schedule: { expr: '0 10 * * *', tz: 'Asia/Shanghai' } });
    expect(job.agent.gate.args).toEqual(['-I', '-B', '/tools/collab-inbox-gate.py', 'wake', '--actor', 'ken',
      '--wi', 'wi_20261022_664', '--job', 'ken-wake-morning', '--harness', '/bin/harness-native', '--inbox-timeout', '120']);
    expect(r.control()).toMatchObject({ revision: 4, entries: { 'job:ken-wake-morning': { owner: 'ken', enabled: true } } });
    expect(r.reload).toHaveBeenCalledTimes(1);
    expect((r.run({}) as { jobs: Array<{ id: string }> }).jobs.map(j => j.id))
      .toEqual(['ken-collab-inbox', 'ken-heartbeat', 'ken-wake-morning']);
  });

  it('edits, disables, enables and deletes only its own managed jobs', () => {
    const r = rig();
    r.run({ action: 'add', ...WAKE });
    r.run({ action: 'edit', job_id: 'ken-wake-morning', schedule: { expr: '45 11 * * *' } });
    expect(r.jobs().find(j => j.id === 'ken-wake-morning')!.schedule.expr).toBe('45 11 * * *');
    r.run({ action: 'disable', job_id: 'ken-wake-morning' });
    expect(r.control().entries['job:ken-wake-morning'].enabled).toBe(false);
    r.run({ action: 'enable', job_id: 'ken-wake-morning' });
    r.run({ action: 'delete', job_id: 'ken-wake-morning' });
    expect(r.jobs().some(j => j.id === 'ken-wake-morning')).toBe(false);
    expect(r.control().entries['job:ken-wake-morning']).toBeUndefined();
  });

  it('may stop an old own job but never re-enable, edit or delete it', () => {
    const r = rig();
    r.run({ action: 'disable', job_id: 'ken-heartbeat' });
    expect(r.jobs().find(j => j.id === 'ken-heartbeat')!.enabled).toBe(false);
    expect(() => r.run({ action: 'enable', job_id: 'ken-heartbeat' })).toThrow('platform pause stays');
    expect(() => r.run({ action: 'edit', job_id: 'ken-heartbeat', prompt: 'x' })).toThrow('only agent-managed');
    expect(() => r.run({ action: 'delete', job_id: 'ken-heartbeat' })).toThrow('only agent-managed');
  });

  it('refuses other owners, platform jobs, frequent schedules, long runs and too many jobs', () => {
    const r = rig();
    expect(() => r.run({ action: 'add', ...WAKE, job_id: 'aki-wake' })).toThrow('must start with "ken-"');
    expect(() => r.run({ action: 'disable', job_id: 'ken-collab-inbox' })).toThrow('platform jobs');
    expect(() => r.run({ action: 'add', ...WAKE, schedule: { expr: '*/5 * * * *' } })).toThrow('at most hourly');
    expect(() => r.run({ action: 'add', job_id: 'ken-batch', schedule: { expr: '0 3 * * *' }, prompt: 'p', timeout: 7200 }))
      .toThrow('timeout must be');
    for (let n = 0; n < MAX_AGENT_JOBS; n++) r.run({ action: 'add', ...WAKE, job_id: `ken-wake-${n}` });
    expect(() => r.run({ action: 'add', ...WAKE, job_id: 'ken-wake-extra' })).toThrow(`at most ${MAX_AGENT_JOBS}`);
    const before = r.jobs();
    r.refuse('a maintenance switch is in progress; retry later');
    expect(() => r.run({ action: 'delete', job_id: 'ken-wake-0' })).toThrow('maintenance');
    expect(r.jobs()).toEqual(before);
  });

  it('answers only a conversation this Relay hosts for the caller', () => {
    const r = rig();
    expect(() => r.run({ session: 'session.aki' })).toThrow('not a conversation this Relay hosts');
    expect(() => r.run({ action: 'add', ...WAKE, surface: 'tcloud' })).toThrow('not a conversation this Relay hosts');
    expect(r.jobs().some(j => j.id === 'ken-wake-morning')).toBe(false);
  });

  it('writes nothing when the control file is broken and restores both files when reload fails', () => {
    const r = rig();
    const before = readFileSync(r.cronJobsPath, 'utf8');
    writeFileSync(r.controlPath, '{not json');
    expect(() => r.run({ action: 'add', ...WAKE })).toThrow('automation-control.json is not valid JSON');
    expect(readFileSync(r.cronJobsPath, 'utf8')).toBe(before);
    const fresh = rig();
    const control = readFileSync(fresh.controlPath, 'utf8');
    fresh.reload.mockImplementation(() => { throw new Error('reload rejected'); });
    expect(() => fresh.run({ action: 'add', ...WAKE })).toThrow('configuration restored');
    expect(fresh.jobs().some(j => j.id === 'ken-wake-morning')).toBe(false);
    expect(readFileSync(fresh.controlPath, 'utf8')).toBe(control);
  });

  it('adds a bounded batch job in the owner workspace', () => {
    const r = rig();
    r.run({ action: 'add', job_id: 'ken-daily-batch', schedule: { expr: '0 10 * * *' }, prompt: '按 HEARTBEAT.md 执行', inject_file: 'HEARTBEAT.md' });
    expect(r.jobs().find(j => j.id === 'ken-daily-batch')).toMatchObject({
      agent: { workspace: '/ws/ken', timeout: 1800, inject_file: 'HEARTBEAT.md' }, delivery: { mode: 'none' } });
    expect(() => r.run({ action: 'add', job_id: 'ken-escape', schedule: { expr: '0 3 * * *' }, prompt: 'p', inject_file: '../x' }))
      .toThrow('inside the workspace');
  });
});

describe('agent-managed inject file', () => {
  it('never follows a link out of the workspace', async () => {
    const { readInjectFile } = await import('../src/scheduler.js');
    const { mkdirSync, symlinkSync } = await import('node:fs');
    const dir = mkdtempSync(join(tmpdir(), 'inject-'));
    mkdirSync(join(dir, 'ws'));
    writeFileSync(join(dir, 'outside.md'), 'SECRET');
    writeFileSync(join(dir, 'ws', 'HEARTBEAT.md'), 'ok');
    symlinkSync(join(dir, 'outside.md'), join(dir, 'ws', 'link.md'));
    const job = { managedBy: 'agent', agent: { workspace: join(dir, 'ws') } } as never;
    expect(readInjectFile(job, join(dir, 'ws', 'HEARTBEAT.md'))).toBe('ok');
    expect(() => readInjectFile(job, join(dir, 'ws', 'link.md'))).toThrow('outside the workspace');
    expect(readInjectFile({ agent: { workspace: join(dir, 'ws') } } as never, join(dir, 'ws', 'link.md'))).toBe('SECRET');
  });
});

const KEN = 'u1:ken';
const AKI = 'u1:aki';
const IDENTITY = async () => ({ schema: 'harness.native_identity/v1', principal: { actor: 'ken', session: 'session.ken', surface: 'ownermac' } });

/** Relay's own wiring (index.ts): the verified key names the conversation and its owner. */
function signedRig(now = () => Date.parse('2026-11-16T08:00:00Z')) {
  const r = rig();
  const data = realpathSync(mkdtempSync(join(tmpdir(), 'agent-jobs-keys-')));
  const verify = createAgentJobsVerifier(data, now);
  const owners: Record<string, string> = { [KEN]: 'ken', [AKI]: 'aki' };
  const agentJobs = (envelope: unknown) => {
    const { poolKey, body } = verify(envelope, Object.keys(owners));
    const owner = owners[poolKey]!;
    return applyAgentJobs({ ...(body as AgentJobRequest), actor: owner }, {
      cronJobsPath: r.cronJobsPath, controlPath: r.controlPath, workspaceFor: o => `/ws/${o}`, reload: r.reload,
      refusal: () => undefined, now: () => new Date(now()),
      hosted: (actor, session, surface) => actor === owner && session === `session.${owner}` && surface === 'ownermac',
    });
  };
  return { ...r, data, agentJobs, key: (poolKey: string) => issueAgentJobsKey(data, poolKey) };
}

describe('agent-jobs caller proof', () => {
  it('accepts only a request signed with the calling conversation key, once and in time', () => {
    const r = signedRig();
    const at = Date.parse('2026-11-16T08:00:00Z');
    const kenKey = r.key(KEN);
    const disable = { action: 'disable', job_id: 'ken-heartbeat', actor: 'ken', session: 'session.ken', surface: 'ownermac' };
    // Bypassing the tool: the public session reference alone proves nothing.
    expect(() => r.agentJobs(disable)).toThrow('unauthenticated: not a signed request');
    // Another conversation's key is that conversation: aki cannot act as ken.
    expect(() => r.agentJobs(signedRequest(disable, r.key(AKI), at))).toThrow('not a conversation this Relay hosts for that Agent');
    // A key Relay did not issue to a hosted conversation, a changed body, an old proof.
    expect(() => r.agentJobs(signedRequest(disable, issueAgentJobsKey(r.data, 'u1:gone'), at))).toThrow('not a conversation this Relay hosts');
    const forged = { ...signedRequest(disable, kenKey, at), body: JSON.stringify({ ...disable, job_id: 'ken-collab-inbox' }) };
    expect(() => r.agentJobs(forged)).toThrow('unauthenticated: bad signature');
    expect(() => r.agentJobs(signedRequest(disable, kenKey, at - 61_000))).toThrow('unauthenticated: proof expired');
    expect(r.jobs().find(j => j.id === 'ken-heartbeat')!.enabled).toBe(true);
    const proof = signedRequest(disable, kenKey, at);
    expect(r.agentJobs(proof)).toMatchObject({ job: { id: 'ken-heartbeat', enabled: false } });
    expect(() => r.agentJobs(proof)).toThrow('unauthenticated: proof already used');
  });

  it('refuses an unsigned HTTP request that copies another session reference', async () => {
    const r = signedRig(Date.now);
    const dir = mkdtempSync(join(tmpdir(), 'agent-jobs-http-'));
    writeFileSync(join(dir, 'relay.json'), JSON.stringify({ users: [], bindings: [] }));
    const handle = await startRelayHttpServer({
      feishuBots: { start: async () => {}, stop: () => {}, reload: async () => ({ added: [], removed: [], total: 0 }), runningAgentIds: () => [] } as never,
      router: {} as Router, configPath: join(dir, 'relay.json'), agentJobs: r.agentJobs,
    }, 0, '127.0.0.1');
    try {
      const env = { RELAY_HTTP_PORT: String(handle.port), PULSE_AGENT_JOBS_KEY_FILE: r.key(KEN) };
      const raw = await fetch(`http://127.0.0.1:${handle.port}/internal/agent-jobs`, { method: 'POST',
        headers: { 'content-type': 'application/json' },
        body: JSON.stringify({ action: 'disable', job_id: 'ken-heartbeat', actor: 'ken', session: 'session.ken', surface: 'ownermac' }) });
      expect(raw.status).toBe(401);
      expect(await raw.json()).toMatchObject({ ok: false, error: 'unauthenticated' });
      expect(r.jobs().find(j => j.id === 'ken-heartbeat')!.enabled).toBe(true);
      const out = await runJobsTool({ action: 'disable', job_id: 'ken-heartbeat' }, IDENTITY, fetch, env);
      expect(out.isError).toBe(false);
      expect(r.jobs().find(j => j.id === 'ken-heartbeat')!.enabled).toBe(false);
    } finally {
      await handle.stop();
    }
  });
});

describe('pulse_jobs tool', () => {
  it('signs the session identity with the conversation key, never sending the key nor a Relay credential', async () => {
    const post = vi.fn(async () => ({ json: async () => ({ ok: true, result: { jobs: [] } }) }));
    const keyFile = issueAgentJobsKey(realpathSync(mkdtempSync(join(tmpdir(), 'agent-jobs-keys-'))), KEN);
    const out = await runJobsTool({ action: 'list', actor: 'aki' }, IDENTITY, post as never,
      { RELAY_HTTP_PORT: '9999', RELAY_INTERNAL_TOKEN: 'secret', PULSE_AGENT_JOBS_KEY_FILE: keyFile });
    expect(out).toEqual({ isError: false, text: '{"jobs":[]}' });
    const [url, init] = post.mock.calls[0] as unknown as [string, { body: string; headers: Record<string, string> }];
    expect(url).toBe('http://127.0.0.1:9999/internal/agent-jobs');
    const envelope = JSON.parse(init.body);
    expect(JSON.parse(envelope.body)).toMatchObject({ actor: 'ken', session: 'session.ken', surface: 'ownermac' });
    expect(init.body).not.toContain(readFileSync(keyFile, 'utf8'));
    expect(init.headers['x-relay-token']).toBeUndefined();
    expect((await runJobsTool({ action: 'list' }, async () => ({}), post as never, { PULSE_AGENT_JOBS_KEY_FILE: keyFile })).isError).toBe(true);
    expect(JSON.parse((await runJobsTool({ action: 'list' }, IDENTITY, post as never, {})).text).error).toContain('only available');
  });
});
