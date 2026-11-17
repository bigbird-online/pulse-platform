import { afterEach, describe, expect, it } from 'vitest';
import { mkdtempSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { main } from '../src/compute-profile-cli.js';

const tempDirs: string[] = [];

afterEach(() => {
  delete process.env['PULSE_COMPUTE_PROFILES_PATH'];
  delete process.env['PULSE_CODEX_MODELS_CACHE_PATH'];
  delete process.env['RELAY_MODEL_CATALOG_PATH'];
  for (const dir of tempDirs.splice(0)) rmSync(dir, { recursive: true, force: true });
});

function useCodexCatalog(fetchedAt: string, models: object[]): void {
  const dir = mkdtempSync(join(tmpdir(), 'relay-codex-models-'));
  tempDirs.push(dir);
  const path = join(dir, 'models_cache.json');
  writeFileSync(path, JSON.stringify({ fetched_at: fetchedAt, models }));
  process.env['PULSE_CODEX_MODELS_CACHE_PATH'] = path;
}

function run(argv: string[]): { code: number; out: string[]; err: string[] } {
  const out: string[] = [];
  const err: string[] = [];
  const code = main(argv, line => out.push(line), line => err.push(line));
  return { code, out, err };
}

const terra = {
  slug: 'gpt-6.1-terra',
  visibility: 'list',
  supported_reasoning_levels: [{ effort: 'medium' }],
};

describe('compute-profile resolve CLI', () => {
  it('resolves media for both /wrap providers as stable JSON', () => {
    useCodexCatalog(new Date().toISOString(), [terra]);

    const cc = run(['resolve', 'media', '--provider', 'cc', '--json']);
    expect(cc.code).toBe(0);
    expect(JSON.parse(cc.out[0])).toMatchObject({
      profile: 'media',
      provider: 'cc',
      model: 'sonnet',
      model_alias: 'claude-sonnet-latest',
      reasoning_effort: 'medium',
    });

    const codex = run(['resolve', 'media', '--provider', 'codex', '--json']);
    expect(codex.code).toBe(0);
    expect(JSON.parse(codex.out[0])).toMatchObject({
      profile: 'media',
      provider: 'codex',
      model: 'gpt-6.1-terra',
      model_alias: 'codex-terra-latest',
      reasoning_effort: 'medium',
    });
    expect(JSON.parse(codex.out[0]).source).toMatch(/Relay\/config\/compute-profiles\.json$/);
  });

  it('resolves the review profile as the kernel reads it', () => {
    useCodexCatalog(new Date().toISOString(), [{
      slug: 'gpt-7.0-sol',
      visibility: 'list',
      supported_reasoning_levels: [{ effort: 'high' }],
    }]);

    const dir = mkdtempSync(join(tmpdir(), 'relay-models-'));
    tempDirs.push(dir);
    process.env['RELAY_MODEL_CATALOG_PATH'] = join(dir, 'models.json');
    writeFileSync(process.env['RELAY_MODEL_CATALOG_PATH'], JSON.stringify({
      models: [{ id: 'claude-opus-5-5' }, { id: 'claude-opus-5' }, { id: 'opus' }],
    }));
    const cc = run(['resolve', 'review', '--provider', 'cc', '--json']);
    expect(cc.code).toBe(0);
    expect(JSON.parse(cc.out[0])).toMatchObject({
      profile: 'review',
      provider: 'cc',
      model: 'claude-opus-5-5',
      model_alias: 'claude-opus-latest',
      reasoning_effort: 'high',
    });
    const codex = run(['resolve', 'review', '--provider', 'codex', '--json']);
    expect(codex.code).toBe(0);
    expect(JSON.parse(codex.out[0])).toMatchObject({
      profile: 'review',
      provider: 'codex',
      model: 'gpt-7.0-sol',
      model_alias: 'codex-sol-latest',
      reasoning_effort: 'high',
    });
  });

  it('resolves Claude Code without depending on the Codex catalog', () => {
    useCodexCatalog('2020-01-01T00:00:00Z', [terra]);

    expect(run(['resolve', 'media', '--provider', 'cc', '--json']).code).toBe(0);
    const codex = run(['resolve', 'media', '--provider', 'codex', '--json']);
    expect(codex.code).toBe(1);
    expect(codex.out).toEqual([]);
    expect(codex.err[0]).toContain('Codex model catalog stale');
  });

  it('fails closed for unknown profiles and malformed arguments', () => {
    useCodexCatalog(new Date().toISOString(), [terra]);

    const unknown = run(['resolve', 'nope', '--provider', 'cc', '--json']);
    expect(unknown.code).toBe(1);
    expect(unknown.out).toEqual([]);
    expect(unknown.err[0]).toContain('unknown compute profile');
    for (const argv of [
      [],
      ['resolve', 'media'],
      ['resolve', 'media', '--provider', 'pi'],
      ['resolve', '--provider', 'cc'],
      ['resolve', 'media', '--provider', 'cc', '--model', 'opus'],
    ]) {
      const result = run(argv);
      expect(result.code).toBe(64);
      expect(result.out).toEqual([]);
    }
  });
});
