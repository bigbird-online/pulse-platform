import { afterEach, describe, expect, it } from 'vitest';
import { mkdtempSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { resolveComputeProfile } from '../src/compute-profiles.js';

const tempDirs: string[] = [];

afterEach(() => {
  delete process.env['PULSE_COMPUTE_PROFILES_PATH'];
  delete process.env['PULSE_CODEX_MODELS_CACHE_PATH'];
  delete process.env['RELAY_MODEL_CATALOG_PATH'];
  for (const dir of tempDirs.splice(0)) rmSync(dir, { recursive: true, force: true });
});

function writeTempJson(prefix: string, value: unknown): string {
  const dir = mkdtempSync(join(tmpdir(), prefix));
  tempDirs.push(dir);
  const path = join(dir, 'config.json');
  writeFileSync(path, JSON.stringify(value));
  return path;
}

function useRelayModels(ids: string[]): void {
  process.env['RELAY_MODEL_CATALOG_PATH'] = writeTempJson(
    'relay-models-',
    { providers: {}, models: ids.map(id => ({ id })) },
  );
}

function useCodexCatalog(models: object[]): void {
  process.env['PULSE_CODEX_MODELS_CACHE_PATH'] = writeTempJson(
    'relay-codex-models-',
    { fetched_at: new Date().toISOString(), models },
  );
}

function codexModel(slug: string, efforts = ['medium', 'xhigh'], visibility = 'list'): object {
  return {
    slug,
    visibility,
    supported_reasoning_levels: efforts.map(effort => ({ effort })),
  };
}

describe('central cron compute profiles', () => {
  it('resolves platform and frontier profiles to the visible official Astra model', () => {
    useCodexCatalog([
      codexModel('gpt-5.6-sol'),
      codexModel('gpt-6-astra', ['high', 'xhigh']),
    ]);

    expect(resolveComputeProfile()).toEqual({
      cc: { model: 'claude-fable-5' },
      codex: {
        model: 'gpt-5.6-sol',
        modelAlias: 'codex-sol-latest',
        reasoningEffort: 'xhigh',
      },
    });
    expect(resolveComputeProfile('frontier').codex).toEqual({
      model: 'gpt-6-astra',
      modelAlias: 'codex-astra-latest',
      reasoningEffort: 'high',
    });
  });

  it('resolves economy to Claude Haiku low and the visible Luna low model', () => {
    useCodexCatalog([
      codexModel('gpt-5.6-luna', ['low']),
      codexModel('gpt-6.0-luna', ['medium']),
    ]);

    expect(resolveComputeProfile('economy')).toEqual({
      cc: {
        model: 'haiku',
        modelAlias: 'claude-haiku-latest',
        reasoningEffort: 'low',
      },
      codex: {
        model: 'gpt-5.6-luna',
        modelAlias: 'codex-luna-latest',
        reasoningEffort: 'low',
      },
    });
  });

  it('resolves latest aliases from runtime-owned model catalogs', () => {
    useCodexCatalog([
      codexModel('gpt-5.6-terra'),
      codexModel('gpt-6.1-terra'),
      codexModel('gpt-8.0-terra', ['low']),
      codexModel('gpt-9.0-terra', ['medium'], 'hide'),
      codexModel('gpt-10.0-terra', ['medium'], 'none'),
      {
        slug: 'gpt-11.0-terra',
        supported_reasoning_levels: [{ effort: 'medium' }],
      },
    ]);

    expect(resolveComputeProfile('media')).toEqual({
      cc: {
        model: 'sonnet',
        modelAlias: 'claude-sonnet-latest',
        reasoningEffort: 'medium',
      },
      codex: {
        model: 'gpt-6.1-terra',
        modelAlias: 'codex-terra-latest',
        reasoningEffort: 'medium',
      },
    });
  });

  it('resolves automation to the latest Sol high with Claude Sonnet high', () => {
    useCodexCatalog([
      codexModel('gpt-6.1-sol', ['high', 'xhigh']),
      codexModel('gpt-7.0-sol', ['xhigh']),
    ]);

    expect(resolveComputeProfile('automation')).toEqual({
      cc: {
        model: 'sonnet',
        modelAlias: 'claude-sonnet-latest',
        reasoningEffort: 'high',
      },
      codex: {
        model: 'gpt-6.1-sol',
        modelAlias: 'codex-sol-latest',
        reasoningEffort: 'high',
      },
    });
  });

  it('resolves review to the current Opus and Sol flagships at high effort', () => {
    useRelayModels(['claude-opus-4-1', 'opus', 'claude-opus-5-5', 'claude-opus-5', 'claude-sonnet-6', 'gpt-6.1-sol']);
    useCodexCatalog([
      codexModel('gpt-6.1-sol', ['high', 'xhigh']),
      codexModel('gpt-7.0-sol', ['high']),
      codexModel('gpt-8.0-astra', ['high']),
    ]);

    expect(resolveComputeProfile('review')).toEqual({
      cc: {
        model: 'claude-opus-5-5',
        modelAlias: 'claude-opus-latest',
        reasoningEffort: 'high',
      },
      codex: {
        model: 'gpt-7.0-sol',
        modelAlias: 'codex-sol-latest',
        reasoningEffort: 'high',
      },
    });
  });

  it('fails closed when the Relay model catalog has no Opus or is missing', () => {
    useCodexCatalog([codexModel('gpt-7.0-sol', ['high'])]);
    expect(() => resolveComputeProfile('review')).toThrow('Relay model catalog is not configured');
    process.env['RELAY_MODEL_CATALOG_PATH'] = join(tmpdir(), 'relay-absent-models', 'models.json');
    expect(() => resolveComputeProfile('review')).toThrow('Relay model catalog unreadable');
    useRelayModels(['opus', 'claude-sonnet-6']);
    expect(() => resolveComputeProfile('review')).toThrow('has no claude-opus-* model');
    // Other profiles keep Claude Code's short alias and need no catalog.
    delete process.env['RELAY_MODEL_CATALOG_PATH'];
    useCodexCatalog([codexModel('gpt-6.1-sol', ['high'])]);
    expect(resolveComputeProfile('automation').cc.model).toBe('sonnet');
  });

  it('review takes the newest Sol and a stronger supported effort, never an older Sol', () => {
    useRelayModels(['claude-opus-5-5']);
    useCodexCatalog([
      codexModel('gpt-6.1-sol', ['high', 'xhigh']),
      codexModel('gpt-7.0-sol', ['xhigh']),
    ]);
    expect(resolveComputeProfile('review').codex).toEqual({
      model: 'gpt-7.0-sol',
      modelAlias: 'codex-sol-latest',
      reasoningEffort: 'xhigh',
    });
    useCodexCatalog([
      codexModel('gpt-6.1-sol', ['high']),
      codexModel('gpt-7.0-sol', ['low', 'medium']),
    ]);
    expect(() => resolveComputeProfile('review')).toThrow('gpt-7.0-sol supports no effort >= high');
  });

  it('refuses review aliases outside the Opus and Sol families', () => {
    useRelayModels(['claude-sonnet-6', 'claude-opus-5-5']);
    useCodexCatalog([codexModel('gpt-7.0-terra', ['high']), codexModel('gpt-7.0-sol', ['high'])]);
    for (const [cc, codex] of [
      ['claude-sonnet-latest', 'codex-sol-latest'],
      ['claude-opus-latest', 'codex-terra-latest'],
    ]) {
      process.env['PULSE_COMPUTE_PROFILES_PATH'] = writeTempJson('relay-compute-profile-', {
        schema_version: 2,
        profiles: {
          review: {
            cc: { model_alias: cc, reasoning_effort: 'high' },
            codex: { model_alias: codex, reasoning_effort: 'high' },
          },
        },
      });
      expect(() => resolveComputeProfile('review')).toThrow('must use claude-opus-latest and codex-sol-latest');
    }
  });

  it('refuses a review profile with a pinned model or effort below high', () => {
    useCodexCatalog([codexModel('gpt-7.0-sol', ['medium', 'high'])]);
    const review = (cc: object, codexEffort = 'high') => {
      process.env['PULSE_COMPUTE_PROFILES_PATH'] = writeTempJson('relay-compute-profile-', {
        schema_version: 2,
        profiles: {
          review: {
            cc,
            codex: { model_alias: 'codex-sol-latest', reasoning_effort: codexEffort },
          },
        },
      });
      return () => resolveComputeProfile('review');
    };

    expect(review({ model: 'claude-opus-5-5', reasoning_effort: 'high' })).toThrow(
      'review.cc must use a latest model_alias',
    );
    expect(review({ model_alias: 'claude-opus-latest' })).toThrow('requires reasoning_effort high');
    expect(review({ model_alias: 'claude-opus-latest', reasoning_effort: 'medium' })).toThrow(
      'requires reasoning_effort high',
    );
    expect(review({ model_alias: 'claude-opus-latest', reasoning_effort: 'high' }, 'medium')).toThrow(
      'requires reasoning_effort high',
    );
  });

  it('fails closed for an unknown profile', () => {
    expect(() => resolveComputeProfile('does-not-exist')).toThrow('unknown compute profile');
  });

  it('forbids Codex model literals in schema v2', () => {
    process.env['PULSE_COMPUTE_PROFILES_PATH'] = writeTempJson('relay-compute-profile-', {
      schema_version: 2,
      profiles: {
        broken: {
          cc: { model_alias: 'claude-sonnet-latest' },
          codex: {
            model: 'gpt-5.6-terra',
            model_alias: 'codex-terra-latest',
            reasoning_effort: 'medium',
          },
        },
      },
    });
    useCodexCatalog([codexModel('gpt-5.6-terra')]);

    expect(() => resolveComputeProfile('broken')).toThrow('model literals are forbidden');
  });

  it('fails closed when a latest alias has no matching visible model', () => {
    useCodexCatalog([codexModel('gpt-5.6-sol')]);

    expect(() => resolveComputeProfile('media')).toThrow(
      'codex-terra-latest has no visible model supporting effort medium',
    );
  });

  it('fails closed when the Codex model catalog is stale', () => {
    process.env['PULSE_CODEX_MODELS_CACHE_PATH'] = writeTempJson(
      'relay-codex-models-',
      {
        fetched_at: '2020-01-01T00:00:00Z',
        models: [codexModel('gpt-5.6-terra')],
      },
    );

    expect(() => resolveComputeProfile('media')).toThrow(
      'Codex model catalog stale or missing fetched_at',
    );
  });

  it('fails closed when a required route field is absent', () => {
    process.env['PULSE_COMPUTE_PROFILES_PATH'] = writeTempJson('relay-compute-profile-', {
      schema_version: 2,
      profiles: {
        broken: {
          cc: { model_alias: 'claude-sonnet-latest' },
          codex: { model_alias: 'codex-terra-latest' },
        },
      },
    });

    expect(() => resolveComputeProfile('broken')).toThrow('broken.codex.reasoning_effort');
  });
});
