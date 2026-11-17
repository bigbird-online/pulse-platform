import { afterEach, describe, expect, it, vi } from 'vitest';
vi.mock('../src/owned-process.js', async () => ({
  spawnOwnedProcess: (await import('node:child_process')).spawn,
}));
vi.mock('node:child_process', async (importOriginal) => {
  const actual = await importOriginal<typeof import('node:child_process')>();
  return { ...actual, spawn: vi.fn() };
});
import { EventEmitter } from 'node:events';
import { PassThrough } from 'node:stream';
import { mkdtempSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import type { Bot } from 'grammy';
import { startRelayHttpServer, type RelayHttpServerDeps, type RelayHttpServerHandle } from '../src/http-server.js';
import { createScheduler } from '../src/scheduler.js';
import type { Router } from '../src/router.js';
import type { JobResult, Scheduler } from '../src/types.js';

const BOT = { api: { sendMessage: async () => undefined } } as unknown as Bot;
const JOB = {
  id: 'daily', name: 'Daily', enabled: true, owner: 'system',
  schedule: { expr: '0 6 * * *', tz: 'Asia/Shanghai' },
  agent: { workspace: '/tmp', prompt: 'fixture', timeout: 10 },
  delivery: { mode: 'none' },
};

let handle: RelayHttpServerHandle | undefined;
let scheduler: Scheduler | undefined;
const dirs: string[] = [];

function tempDir(): string {
  const dir = mkdtempSync(join(tmpdir(), 'relay-jobs-run-'));
  dirs.push(dir);
  return dir;
}

async function start(extra: Partial<RelayHttpServerDeps>): Promise<string> {
  const configPath = join(tempDir(), 'relay.json');
  writeFileSync(configPath, JSON.stringify({ users: [], bindings: [] }));
  handle = await startRelayHttpServer({
    feishuBots: { start: async () => {}, stop: () => {}, reload: async () => ({ added: [], removed: [], total: 0 }), runningAgentIds: () => [] } as never,
    router: {} as Router, configPath, ...extra,
  }, 0);
  return `http://127.0.0.1:${handle.port}/internal/jobs/run`;
}

function residentScheduler(admit = () => true): Scheduler {
  const dir = tempDir();
  const cronPath = join(dir, 'cron-jobs.json');
  writeFileSync(cronPath, JSON.stringify({ version: 2, jobs: [JOB] }));
  process.env['CRON_JOBS_STATE_PATH'] = join(dir, 'cron-jobs-state.json');
  // The batch runtime reads a Codex model catalog before it spawns.
  process.env['PULSE_CODEX_MODELS_CACHE_PATH'] = join(dir, 'models_cache.json');
  writeFileSync(process.env['PULSE_CODEX_MODELS_CACHE_PATH'], JSON.stringify({
    fetched_at: new Date().toISOString(),
    models: ['gpt-6-astra', 'gpt-5.6-sol', 'gpt-5.6-terra'].map(slug => ({
      slug, visibility: 'list', supported_reasoning_levels: [{ effort: 'medium' }, { effort: 'high' }, { effort: 'xhigh' }],
    })),
  }));
  scheduler = createScheduler(BOT, cronPath, {
    registerCronTasks: false, reconcileMissedRuns: false, admitScheduledRun: admit,
  });
  scheduler.start();
  return scheduler;
}

const post = (url: string, payload: unknown, token?: string) => fetch(url, {
  method: 'POST', headers: { 'content-type': 'application/json', ...(token ? { 'x-relay-token': token } : {}) },
  body: JSON.stringify(payload),
});

afterEach(async () => {
  await handle?.stop();
  handle = undefined;
  scheduler?.stop();
  scheduler = undefined;
  for (const dir of dirs.splice(0)) rmSync(dir, { recursive: true, force: true });
  delete process.env['RELAY_INTERNAL_TOKEN'];
  delete process.env['CRON_JOBS_STATE_PATH'];
  delete process.env['PULSE_CODEX_MODELS_CACHE_PATH'];
});

describe('Relay HTTP — /internal/jobs/run', () => {
  it('requires the token, and reports a Relay without scheduler', async () => {
    process.env['RELAY_INTERNAL_TOKEN'] = 'secret';
    const url = await start({});
    expect((await post(url, { jobId: 'daily' })).status).toBe(401);
    const none = await post(url, { jobId: 'daily' }, 'secret');
    expect([none.status, (await none.json()).error]).toEqual([503, 'scheduler_unavailable']);
  });

  it('claims the run on the resident scheduler, answers 202 at once, and refuses a second run', async () => {
    let finish: (result: JobResult) => void = () => undefined;
    const running = new Set<string>();
    const fake = {
      listJobs: () => [JOB],
      getRunningJobIds: () => [...running],
      runJob: vi.fn((id: string) => {
        running.add(id);
        return new Promise<JobResult>(resolve => { finish = resolve; });
      }),
      stop: () => undefined, reload: () => undefined,
    } as unknown as Scheduler;
    const url = await start({ scheduler: fake });

    const missing = await post(url, {});
    expect([missing.status, (await missing.json()).error]).toEqual([400, 'jobId_required']);
    const accepted = await post(url, { jobId: 'daily' });
    const body = await accepted.json();
    expect(accepted.status).toBe(202);
    expect(body).toMatchObject({ ok: true, jobId: 'daily', name: 'Daily' });
    expect(fake.runJob).toHaveBeenCalledWith('daily');

    const again = await post(url, { jobId: 'daily' });
    expect([again.status, (await again.json()).error]).toEqual([409, 'job_running']);
    expect(fake.runJob).toHaveBeenCalledTimes(1);

    const unknown = await post(url, { jobId: 'nope' });
    expect([unknown.status, (await unknown.json()).error]).toEqual([404, 'job_not_found']);
    finish({ jobId: 'daily', status: 'ok', output: '', durationMs: 1 });
  });

  it('on the real scheduler, a maintenance barrier is refused with 503 and nothing starts', async () => {
    const { spawn } = await import('node:child_process');
    vi.mocked(spawn).mockReset();
    const url = await start({ scheduler: residentScheduler(() => false) });
    const held = await post(url, { jobId: 'daily' });
    expect([held.status, (await held.json()).error]).toEqual([503, 'maintenance_hold']);
    expect(scheduler!.getRunningJobIds()).toEqual([]);
    expect(vi.mocked(spawn)).not.toHaveBeenCalled();
  });

  it('on the real scheduler, the claim is visible as soon as runJob returns', async () => {
    const real = residentScheduler();
    const url = await start({ scheduler: real });
    // A run already claimed (e.g. by cron) is refused, not duplicated.
    const { spawn } = await import('node:child_process');
    const procs: EventEmitter[] = [];
    vi.mocked(spawn).mockImplementation(() => {
      const proc = Object.assign(new EventEmitter(), {
        stdout: new PassThrough(), stderr: new PassThrough(), stdin: new PassThrough(),
        pid: 4242, exitCode: null, kill: () => true,
      });
      procs.push(proc);
      return proc as never;
    });
    const first = real.runJob('daily');
    expect(real.getRunningJobIds()).toEqual(['daily']);
    const refused = await post(url, { jobId: 'daily' });
    expect([refused.status, (await refused.json()).error]).toEqual([409, 'job_running']);
    await vi.waitFor(() => expect(procs.length).toBeGreaterThan(0));
    for (const proc of procs) proc.emit('exit', 1, null);
    await first;
    expect(real.getRunningJobIds()).toEqual([]);
  });
});
