import { describe, it, expect, beforeEach, afterEach } from 'vitest';
import { writeFileSync, mkdirSync, rmSync } from 'node:fs';
import { join } from 'node:path';
import { createModelCatalog, type ModelCatalog } from '../src/model-catalog.js';

const TMP_DIR = join(import.meta.dirname, '__tmp_model_catalog_test__');

function writeTmpConfig(name: string, data: unknown): string {
  mkdirSync(TMP_DIR, { recursive: true });
  const p = join(TMP_DIR, name);
  writeFileSync(p, JSON.stringify(data));
  return p;
}

describe('createModelCatalog', () => {
  afterEach(() => {
    try { rmSync(TMP_DIR, { recursive: true }); } catch { /* ok */ }
  });

  it('returns empty entries when no config path', () => {
    const catalog = createModelCatalog();
    expect(catalog.entries).toEqual([]);
    expect(catalog.listIds()).toEqual([]);
  });

  it('returns empty entries when config file does not exist', () => {
    const catalog = createModelCatalog(join(TMP_DIR, 'nonexistent.json'));
    expect(catalog.entries).toEqual([]);
  });

  it('returns empty entries when config is malformed', () => {
    const path = writeTmpConfig('bad.json', { foo: 'bar' });
    const catalog = createModelCatalog(path);
    expect(catalog.entries).toEqual([]);
  });

  it('loads models from valid config', () => {
    process.env['_TEST_OPENROUTER_KEY'] = 'test-key';
    process.env['_TEST_GATEWAY_KEY'] = 'test-key';
    const path = writeTmpConfig('valid.json', {
      providers: {
        openrouter: { protocol: 'anthropic', base_url_env: 'OPENROUTER_BASE_URL', api_key_pool: ['_TEST_OPENROUTER_KEY'] },
        gateway: { protocol: 'anthropic', base_url_env: 'GATEWAY_BASE_URL', api_key_pool: ['_TEST_GATEWAY_KEY'] },
      },
      models: [
        { id: 'deepseek/deepseek-v4-flash-0731', display_name: 'DeepSeek V4 Flash', provider: 'openrouter' },
        { id: 'gateway-model', display_name: 'Gateway Model', provider: 'gateway' },
      ],
    });
    const catalog = createModelCatalog(path);
    expect(catalog.entries).toHaveLength(2);
    expect(catalog.listIds()).toEqual([
      { id: 'deepseek/deepseek-v4-flash-0731', display_name: 'DeepSeek V4 Flash', runtimeLabel: 'CC', available: true },
      { id: 'gateway-model', display_name: 'Gateway Model', runtimeLabel: 'CC', available: true },
    ]);
    delete process.env['_TEST_OPENROUTER_KEY'];
    delete process.env['_TEST_GATEWAY_KEY'];
  });
});

describe('findById', () => {
  let catalog: ModelCatalog;

  beforeEach(() => {
    const path = writeTmpConfig('test.json', {
      models: [
        { id: 'deepseek/deepseek-v4-flash-0731', display_name: 'DeepSeek V4 Flash', provider: 'openrouter', endpoint_env: 'OPENROUTER_ANTHROPIC_BASE_URL' },
        { id: 'gateway-model', display_name: 'Gateway Model', provider: 'gateway' },
      ],
    });
    catalog = createModelCatalog(path);
  });

  it('finds model by exact ID', () => {
    const entry = catalog.findById('deepseek/deepseek-v4-flash-0731');
    expect(entry).toBeDefined();
    expect(entry!.id).toBe('deepseek/deepseek-v4-flash-0731');
    expect(entry!.provider).toBe('openrouter');
    expect(entry!.endpoint_env).toBe('OPENROUTER_ANTHROPIC_BASE_URL');
  });

  it('returns undefined for unknown ID', () => {
    expect(catalog.findById('nonexistent')).toBeUndefined();
  });

  it('does not fuzzy match', () => {
    expect(catalog.findById('deepseek')).toBeUndefined();
    expect(catalog.findById('DEEPSEEK/DEEPSEEK-V4-FLASH-0731')).toBeUndefined(); // case-sensitive
  });
});

describe('validateModelId', () => {
  let catalog: ModelCatalog;

  beforeEach(() => {
    const path = writeTmpConfig('test.json', {
      models: [
        { id: 'deepseek/deepseek-v4-flash-0731', display_name: 'DeepSeek V4 Flash', provider: 'openrouter' },
        { id: 'gateway-model', display_name: 'Gateway Model', provider: 'gateway' },
        { id: 'gateway-model-2', display_name: 'Gateway Model 2', provider: 'gateway' },
      ],
    });
    catalog = createModelCatalog(path);
  });

  it('accepts catalog model ID', () => {
    const result = catalog.validateModelId('deepseek/deepseek-v4-flash-0731');
    expect(result.valid).toBe(true);
    expect(result.resolvedId).toBe('deepseek/deepseek-v4-flash-0731');
    expect(result.entry).toBeDefined();
    expect(result.entry!.id).toBe('deepseek/deepseek-v4-flash-0731');
  });

  it('accepts all catalog model IDs', () => {
    expect(catalog.validateModelId('gateway-model').valid).toBe(true);
    expect(catalog.validateModelId('gateway-model-2').valid).toBe(true);
  });

  it('accepts Claude short alias: sonnet', () => {
    const result = catalog.validateModelId('sonnet');
    expect(result.valid).toBe(true);
    expect(result.resolvedId).toBe('sonnet');
  });

  it('accepts Claude short alias: opus', () => {
    expect(catalog.validateModelId('opus').valid).toBe(true);
  });

  it('accepts Claude short alias: haiku', () => {
    expect(catalog.validateModelId('haiku').valid).toBe(true);
  });

  it('accepts Claude short alias case-insensitively', () => {
    expect(catalog.validateModelId('Sonnet').valid).toBe(true);
    expect(catalog.validateModelId('OPUS').valid).toBe(true);
  });

  it('accepts full claude-* model ID', () => {
    const result = catalog.validateModelId('claude-opus-4-7-20250514');
    expect(result.valid).toBe(true);
    expect(result.resolvedId).toBe('claude-opus-4-7-20250514');
  });

  it('rejects unknown model ID', () => {
    const result = catalog.validateModelId('gpt-4o');
    expect(result.valid).toBe(false);
    expect(result.resolvedId).toBe('gpt-4o');
  });

  it('rejects empty input', () => {
    expect(catalog.validateModelId('').valid).toBe(false);
  });

  it('works without config (Claude aliases still valid)', () => {
    const emptyCatalog = createModelCatalog();
    expect(emptyCatalog.validateModelId('sonnet').valid).toBe(true);
    expect(emptyCatalog.validateModelId('claude-opus-4-7-20250514').valid).toBe(true);
    expect(emptyCatalog.validateModelId('deepseek/deepseek-v4-flash-0731').valid).toBe(false);
  });
});

describe('listIds', () => {
  it('returns all entries with id and display_name', () => {
    const path = writeTmpConfig('test.json', {
      models: [
        { id: 'deepseek/deepseek-v4-flash-0731', display_name: 'DeepSeek V4 Flash' },
        { id: 'gateway-model', display_name: 'Gateway Model' },
      ],
    });
    const catalog = createModelCatalog(path);
    const ids = catalog.listIds();
    expect(ids).toEqual([
      { id: 'deepseek/deepseek-v4-flash-0731', display_name: 'DeepSeek V4 Flash', runtimeLabel: 'CC', available: true },
      { id: 'gateway-model', display_name: 'Gateway Model', runtimeLabel: 'CC', available: true },
    ]);
  });
});

describe('reload', () => {
  it('reloads config from disk', () => {
    const path = writeTmpConfig('reload.json', {
      models: [{ id: 'deepseek/deepseek-v4-flash-0731', display_name: 'DeepSeek V4 Flash' }],
    });
    const catalog = createModelCatalog(path);
    expect(catalog.entries).toHaveLength(1);

    // Overwrite file
    writeFileSync(path, JSON.stringify({
      models: [
        { id: 'deepseek/deepseek-v4-flash-0731', display_name: 'DeepSeek V4 Flash' },
        { id: 'gateway-model', display_name: 'Gateway Model' },
      ],
    }));

    catalog.reload();
    expect(catalog.entries).toHaveLength(2);
    expect(catalog.findById('gateway-model')).toBeDefined();
  });
});
