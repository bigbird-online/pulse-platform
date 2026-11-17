import { afterEach, describe, expect, it, vi } from 'vitest';
import { mkdtempSync, readFileSync, rmSync, writeFileSync } from 'node:fs';
import { createServer, type IncomingHttpHeaders, type Server } from 'node:http';
import type { AddressInfo } from 'node:net';
import { tmpdir } from 'node:os';
import { join } from 'node:path';

const tempDirs: string[] = [];
const servers: Server[] = [];

afterEach(async () => {
  for (const server of servers.splice(0)) await new Promise(resolve => server.close(resolve));
  vi.restoreAllMocks();
  vi.unstubAllEnvs();
  vi.resetModules();
  for (const dir of tempDirs.splice(0)) rmSync(dir, { recursive: true, force: true });
});

describe('relay-jobs writes', () => {
  it('state that the resident Relay has not adopted the change and never signal a PID', async () => {
    const dir = mkdtempSync(join(tmpdir(), 'relay-jobs-cli-'));
    tempDirs.push(dir);
    const cronPath = join(dir, 'cron-jobs.json');
    vi.stubEnv('PULSE_RELAY_PATHS', undefined);
    vi.stubEnv('CRON_JOBS_PATH', cronPath);
    vi.stubEnv('CRON_JOBS_STATE_PATH', join(dir, 'cron-jobs-state.json'));
    vi.stubEnv('HOME', dir);
    const kill = vi.spyOn(process, 'kill');
    const warn = vi.spyOn(console, 'warn').mockImplementation(() => undefined);
    vi.spyOn(console, 'log').mockImplementation(() => undefined);
    const { main, ADOPTION_NOTICE } = await import('../src/relay-jobs.js');

    expect(await main(['add', '--cron', '0 9 * * *', '--prompt', 'fixture', '--owner', 'fixture'])).toBe(0);
    const id = JSON.parse(readFileSync(cronPath, 'utf-8')).jobs[0].id as string;
    expect(await main(['del', id, '--owner', 'fixture'])).toBe(0);

    expect(kill).not.toHaveBeenCalled();
    expect(warn.mock.calls.filter(call => call[0] === ADOPTION_NOTICE)).toHaveLength(2);
    expect(ADOPTION_NOTICE).toContain('pulse-relay reload');
  });
});

interface Captured { url?: string; headers?: IncomingHttpHeaders; body?: unknown }

/** A stand-in resident Relay answering /internal/jobs/run with a fixed reply. */
async function fakeRelay(status: number, reply: unknown): Promise<{ port: number; captured: Captured }> {
  const captured: Captured = {};
  const server = createServer((req, res) => {
    let raw = '';
    req.on('data', chunk => { raw += chunk; });
    req.on('end', () => {
      Object.assign(captured, { url: req.url, headers: req.headers, body: JSON.parse(raw) });
      res.writeHead(status, { 'content-type': 'application/json' });
      res.end(JSON.stringify(reply));
    });
  });
  servers.push(server);
  await new Promise<void>(resolve => server.listen(0, '127.0.0.1', resolve));
  return { port: (server.address() as AddressInfo).port, captured };
}

async function loadCli(env: Record<string, string | undefined>) {
  const dir = mkdtempSync(join(tmpdir(), 'relay-jobs-run-'));
  tempDirs.push(dir);
  const cronPath = join(dir, 'cron-jobs.json');
  writeFileSync(cronPath, JSON.stringify({ version: 2, jobs: [{
    id: 'daily', name: 'Daily', enabled: true, owner: 'system',
    schedule: { expr: '0 6 * * *', tz: 'Asia/Shanghai' },
    agent: { workspace: dir, prompt: 'fixture', timeout: 10 },
    delivery: { mode: 'none' },
  }] }));
  vi.stubEnv('PULSE_RELAY_PATHS', undefined);
  vi.stubEnv('CRON_JOBS_PATH', cronPath);
  vi.stubEnv('CRON_JOBS_STATE_PATH', join(dir, 'cron-jobs-state.json'));
  vi.stubEnv('PULSE_CODEX_MODELS_CACHE_PATH', join(dir, 'absent-models.json'));
  vi.stubEnv('HOME', dir);
  vi.stubEnv('RELAY_HTTP_HOST', undefined);
  vi.stubEnv('RELAY_STATUS_HOST', undefined);
  for (const [key, value] of Object.entries(env)) vi.stubEnv(key, value);
  const log = vi.spyOn(console, 'log').mockImplementation(() => undefined);
  const error = vi.spyOn(console, 'error').mockImplementation(() => undefined);
  vi.spyOn(console, 'warn').mockImplementation(() => undefined);
  const { main } = await import('../src/relay-jobs.js');
  const text = (spy: typeof log) => spy.mock.calls.map(call => String(call[0])).join('\n');
  return { main, out: () => text(log), err: () => text(error) };
}

describe('relay-jobs run', () => {
  it('asks the resident Relay to run the job with its token and returns once accepted', async () => {
    const relay = await fakeRelay(202, { ok: true, jobId: 'daily', name: 'Daily', acceptedAt: '2026-11-03T05:00:00.000Z' });
    const cli = await loadCli({ RELAY_HTTP_PORT: String(relay.port), RELAY_INTERNAL_TOKEN: 'secret' });

    expect(await cli.main(['run', 'daily'])).toBe(0);
    expect(relay.captured.url).toBe('/internal/jobs/run');
    expect(relay.captured.headers?.['x-relay-token']).toBe('secret');
    expect(relay.captured.body).toEqual({ jobId: 'daily' });
    expect(cli.out()).toContain('已交运行中的 Relay 执行');
    expect(cli.out()).toContain('pulse-relay jobs list');
  });

  it('reports a refused run without retrying it locally', async () => {
    const relay = await fakeRelay(409, { ok: false, error: 'job_running', jobId: 'daily' });
    const cli = await loadCli({ RELAY_HTTP_PORT: String(relay.port) });

    expect(await cli.main(['run', 'daily'])).toBe(1);
    expect(cli.err()).toContain('正在运行或排队');
    expect(cli.out()).not.toContain('本地运行');
  });

  it('fails with a --local hint when the resident Relay is unknown or unreachable', async () => {
    const unknown = await loadCli({ RELAY_HTTP_PORT: undefined });
    expect(await unknown.main(['run', 'daily'])).toBe(1);
    expect(unknown.err()).toContain('--local');

    vi.restoreAllMocks();
    vi.resetModules();
    // A port that was just listening and is now closed refuses the connection.
    const { port } = await fakeRelay(202, {});
    await new Promise(resolve => servers.pop()!.close(resolve));
    const unreachable = await loadCli({ RELAY_HTTP_PORT: String(port) });
    expect(await unreachable.main(['run', 'daily'])).toBe(1);
    expect(unreachable.err()).toContain('连不上运行中的 Relay');
    expect(unreachable.err()).toContain('--local');
  });

  it('--local runs in this process, says it does not deliver, and makes no Relay request', async () => {
    const relay = await fakeRelay(202, {});
    const cli = await loadCli({ RELAY_HTTP_PORT: String(relay.port) });

    // The absent model catalog fails the run before any runtime is spawned.
    expect(await cli.main(['run', 'daily', '--local'])).toBe(1);
    expect(cli.out()).toContain('本地运行，不投递');
    expect(relay.captured.url).toBeUndefined();
  });
});
