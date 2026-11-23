/** `relay-jobs run --local` resolves owner isolation as the resident Relay does: agy runs only for a host-account owner. */
import { afterAll, describe, expect, it, vi } from 'vitest';
vi.mock('../src/owned-process.js', async () => ({ spawnOwnedProcess: vi.fn() }));
vi.mock('../src/standing-access.js', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../src/standing-access.js')>()), standingAccessFor: vi.fn(async () => []),
}));
vi.mock('../src/automation-control.js', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../src/automation-control.js')>()), requireAutomatic: vi.fn(),
}));
import { mkdtempSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';

const dir = mkdtempSync(join(tmpdir(), 'relay-jobs-agy-'));
const agyBin = join(dir, 'agy');
writeFileSync(agyBin, '#!/bin/sh\n', { mode: 0o755 });
const cronPath = join(dir, 'cron-jobs.json');
writeFileSync(cronPath, JSON.stringify({ version: 1, jobs: [{
  id: 'agy-iso', name: 'agy isolated', enabled: true, owner: 'iso',
  schedule: { expr: '0 6 * * *', tz: 'Asia/Shanghai' },
  agent: { workspace: dir, prompt: 'hello', timeout: 10, runtimePreference: ['agy'] },
  delivery: { mode: 'none' },
}] }));
// The scheduler resolves the compute profile (Codex catalog) before any runtime starts.
const modelsPath = join(dir, 'models_cache.json');
writeFileSync(modelsPath, JSON.stringify({ fetched_at: new Date().toISOString(), models: [
  { slug: 'gpt-6-astra', visibility: 'list', supported_reasoning_levels: [{ effort: 'high' }, { effort: 'xhigh' }] },
  { slug: 'gpt-5.6-sol', visibility: 'list', supported_reasoning_levels: [{ effort: 'xhigh' }] },
  { slug: 'gpt-5.6-terra', visibility: 'list', supported_reasoning_levels: [{ effort: 'medium' }] },
] }));
const configPath = join(dir, 'relay.json');
writeFileSync(configPath, JSON.stringify({ users: [{ userId: 'owner', chatId: '1', defaultAgent: 'iso',
  agents: [{ id: 'iso', name: 'Iso', emoji: 'I', workspace: dir, linuxUser: 'agent-iso' }] }] }));
const saved = Object.fromEntries(['CRON_JOBS_PATH', 'CRON_JOBS_STATE_PATH', 'RELAY_CONFIG_PATH', 'PULSE_AGY_BIN',
  'PULSE_CODEX_MODELS_CACHE_PATH'].map((key) => [key, process.env[key]]));
Object.assign(process.env, { CRON_JOBS_PATH: cronPath, CRON_JOBS_STATE_PATH: join(dir, 'cron-jobs-state.json'),
  RELAY_CONFIG_PATH: configPath, PULSE_AGY_BIN: agyBin, PULSE_CODEX_MODELS_CACHE_PATH: modelsPath });
const { main } = await import('../src/relay-jobs.js');
const { spawnOwnedProcess } = await import('../src/owned-process.js');

describe('relay-jobs run --local with agy', () => {
  afterAll(() => {
    for (const [key, value] of Object.entries(saved)) {
      if (value === undefined) delete process.env[key];
      else process.env[key] = value;
    }
    rmSync(dir, { recursive: true, force: true });
  });

  it('refuses a job of an Agent isolated under its own Linux user', async () => {
    const errors = vi.spyOn(console, 'error').mockImplementation(() => undefined);
    const logs = vi.spyOn(console, 'log').mockImplementation(() => undefined);
    try {
      expect(await main(['run', 'agy-iso', '--local'])).toBe(1);
      expect(errors.mock.calls.map((call) => String(call[0])).join('\n')).toContain('独立 Linux 用户隔离');
      expect(spawnOwnedProcess).not.toHaveBeenCalled();
    } finally {
      errors.mockRestore();
      logs.mockRestore();
    }
  });

  it('refuses agy when the Relay configuration cannot be read to verify the owner', async () => {
    process.env['RELAY_CONFIG_PATH'] = join(dir, 'missing-relay.json');
    const errors = vi.spyOn(console, 'error').mockImplementation(() => undefined);
    const logs = vi.spyOn(console, 'log').mockImplementation(() => undefined);
    try {
      expect(await main(['run', 'agy-iso', '--local'])).toBe(1);
      expect(errors.mock.calls.map((call) => String(call[0])).join('\n')).toContain('无法核实');
      expect(spawnOwnedProcess).not.toHaveBeenCalled();
    } finally {
      errors.mockRestore();
      logs.mockRestore();
    }
  });
});
