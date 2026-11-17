import { describe, it, expect, beforeEach, afterEach } from 'vitest';
import { mkdirSync, rmSync, writeFileSync } from 'node:fs';
import { join } from 'node:path';
import { tmpdir } from 'node:os';
import { loadPricingConfig, calculateCost } from '../src/pricing.js';
import type { PricingConfig } from '../src/pricing.js';

const testDir = join(tmpdir(), 'relay-pricing-test-' + Date.now());

beforeEach(() => mkdirSync(testDir, { recursive: true }));
afterEach(() => rmSync(testDir, { recursive: true, force: true }));

function writeConfig(config: object, filename = 'pricing.json'): string {
  const path = join(testDir, filename);
  writeFileSync(path, JSON.stringify(config, null, 2));
  return path;
}

const SAMPLE_CONFIG: PricingConfig = {
  exchange_rate: { usd_to_cny: 7.25, updated_at: '2026-05-27T10:00:00+08:00', source: 'manual' },
  models: [
    { match: 'cny-priced', display_name: 'CNY Priced', currency: 'CNY', pricing_unit: 'per_million', input_price: 2.0, output_price: 8.0, is_default_domestic: true },
    { match: 'cny-alt', display_name: 'CNY Alt', currency: 'CNY', pricing_unit: 'per_million', input_price: 5.0, output_price: 10.0 },
    { match: 'claude-sonnet', display_name: 'Claude Sonnet', currency: 'USD', pricing_unit: 'per_million', input_price: 3.0, output_price: 15.0, is_default_foreign: true },
    { match: 'claude-opus', display_name: 'Claude Opus', currency: 'USD', pricing_unit: 'per_million', input_price: 15.0, output_price: 75.0 },
    { match: '*', display_name: 'Unknown', currency: 'CNY', pricing_unit: 'per_million', input_price: 5.0, output_price: 15.0, is_default: true },
  ],
};

describe('loadPricingConfig', () => {
  it('loads valid JSON config', () => {
    const path = writeConfig(SAMPLE_CONFIG);
    const config = loadPricingConfig(path);
    expect(config.models).toHaveLength(5);
    expect(config.exchange_rate.usd_to_cny).toBe(7.25);
  });

  it('returns empty config when file does not exist', () => {
    const config = loadPricingConfig(join(testDir, 'nonexistent.json'));
    expect(config.models).toHaveLength(0);
    expect(config.exchange_rate.usd_to_cny).toBe(0);
  });

  it('returns empty config for malformed JSON', () => {
    const path = join(testDir, 'bad.json');
    writeFileSync(path, 'not json at all {{{');
    const config = loadPricingConfig(path);
    expect(config.models).toHaveLength(0);
  });
});

describe('calculateCost', () => {
  const config = SAMPLE_CONFIG;

  it('matches a CNY-priced model by prefix', () => {
    const result = calculateCost(config, 'cny-priced-chat', 5000, 2000);
    expect(result.matched_entry).toBe('CNY Priced');
    expect(result.input_tokens).toBe(5000);
    expect(result.output_tokens).toBe(2000);
    // (5000 * 2 + 2000 * 8) / 1_000_000 = (10000 + 16000) / 1_000_000 = 0.026
    expect(result.cost_cny).toBeCloseTo(0.026, 5);
    expect(result.cost_usd).toBeUndefined();
  });

  it('matches foreign model by prefix (Claude Sonnet)', () => {
    const result = calculateCost(config, 'claude-sonnet-4-20250514', 10000, 3000);
    expect(result.matched_entry).toBe('Claude Sonnet');
    // (10000 * 3 + 3000 * 15) / 1_000_000 = (30000 + 45000) / 1_000_000 = 0.075 USD
    expect(result.cost_usd).toBeCloseTo(0.075, 5);
    expect(result.cost_cny).toBeCloseTo(0.075 * 7.25, 5);
    expect(result.exchange_rate).toBe(7.25);
  });

  it('matches claude-opus prefix', () => {
    const result = calculateCost(config, 'claude-opus-4-20250514', 1000, 500);
    expect(result.matched_entry).toBe('Claude Opus');
    // (1000 * 15 + 500 * 75) / 1_000_000 = (15000 + 37500) / 1_000_000 = 0.0525
    expect(result.cost_usd).toBeCloseTo(0.0525, 5);
  });

  it('matches case-insensitively', () => {
    const result = calculateCost(config, 'CNY-Priced-V3', 1000, 1000);
    expect(result.matched_entry).toBe('CNY Priced');
  });

  it('falls back to wildcard for unknown models', () => {
    const result = calculateCost(config, 'some-unknown-model-v1', 1000, 1000);
    expect(result.matched_entry).toBe('Unknown');
    // (1000 * 5 + 1000 * 15) / 1_000_000 = 20000 / 1_000_000 = 0.02
    expect(result.cost_cny).toBeCloseTo(0.02, 5);
  });

  // Regression (2026-06-12): a model-catalog row with no price fields slips
  // through models.json format detection and reaches calculateCost as a
  // PricingEntry with `match: undefined`. The old code did `entry.match.toLowerCase()`
  // → uncaught TypeError → process.exit(1) → whole Relay crash-loop → every
  // Feishu card stuck on "Generating". calculateCost must tolerate such entries.
  it('skips entries with undefined match instead of crashing', () => {
    const malformed = {
      exchange_rate: { usd_to_cny: 7.25, updated_at: '', source: 'manual' },
      models: [
        // No `match` (catalog row that lost its price fields) — must be skipped, not throw.
        { display_name: 'Claude Opus（最新版）', currency: 'CNY', pricing_unit: 'per_million', input_price: 0, output_price: 0 } as unknown,
        { match: 'opus', display_name: 'Opus priced', currency: 'USD', pricing_unit: 'per_million', input_price: 15, output_price: 75 },
      ],
    } as unknown as PricingConfig;
    expect(() => calculateCost(malformed, 'opus', 1000, 500)).not.toThrow();
    const result = calculateCost(malformed, 'opus', 1000, 500);
    // Falls through the match-less entry and matches the well-formed 'opus' entry.
    expect(result.matched_entry).toBe('Opus priced');
  });

  it('does not crash when model itself is undefined', () => {
    expect(() => calculateCost(config, undefined as unknown as string, 1000, 500)).not.toThrow();
  });

  it('returns zero cost when no match and no wildcard', () => {
    const noWildcard: PricingConfig = {
      exchange_rate: { usd_to_cny: 7.25, updated_at: '', source: 'manual' },
      models: [
        { match: 'cny-priced', display_name: 'CNY Priced', currency: 'CNY', pricing_unit: 'per_million', input_price: 2.0, output_price: 8.0 },
      ],
    };
    const result = calculateCost(noWildcard, 'unknown-model', 1000, 1000);
    expect(result.cost_cny).toBe(0);
    expect(result.matched_entry).toBe('');
  });

  it('handles zero tokens', () => {
    const result = calculateCost(config, 'cny-priced-chat', 0, 0);
    expect(result.cost_cny).toBe(0);
  });

  it('uses CNY Alt pricing for cny-alt-1', () => {
    const result = calculateCost(config, 'cny-alt-1', 10000, 5000);
    expect(result.matched_entry).toBe('CNY Alt');
    // (10000 * 5 + 5000 * 10) / 1_000_000 = (50000 + 50000) / 1_000_000 = 0.1
    expect(result.cost_cny).toBeCloseTo(0.1, 5);
  });
});

// P0-005: Model catalog (models.json) format support
describe('loadPricingConfig with models.json format [P0-005]', () => {
  it('loads models.json format and converts to PricingConfig', () => {
    const path = writeConfig({
      models: [
        { id: 'cny-alt-1', display_name: 'CNY Alt 1', provider: 'gateway', currency: 'CNY', input_price_per_million: 5.0, output_price_per_million: 10.0 },
        { id: 'cny-priced-chat', display_name: 'CNY Priced Chat', provider: 'gateway', currency: 'CNY', input_price_per_million: 2.0, output_price_per_million: 8.0 },
      ],
    }, 'models.json');
    const config = loadPricingConfig(path);
    expect(config.models).toHaveLength(2);
    expect(config.models[0]).toMatchObject({
      match: 'cny-alt-1',
      display_name: 'CNY Alt 1',
      currency: 'CNY',
      pricing_unit: 'per_million',
      input_price: 5.0,
      output_price: 10.0,
    });
    expect(config.exchange_rate.usd_to_cny).toBe(7.25);
  });

  it('calculates cost using exact model ID match from models.json', () => {
    const path = writeConfig({
      models: [
        { id: 'cny-alt-1', display_name: 'CNY Alt 1', currency: 'CNY', input_price_per_million: 5.0, output_price_per_million: 10.0 },
      ],
    }, 'models.json');
    const config = loadPricingConfig(path);
    const result = calculateCost(config, 'cny-alt-1', 1000, 500);
    expect(result.matched_entry).toBe('CNY Alt 1');
    // (1000 * 5 + 500 * 10) / 1_000_000 = 10000 / 1_000_000 = 0.01
    expect(result.cost_cny).toBeCloseTo(0.01, 5);
  });

  it('filters out models without pricing info', () => {
    const path = writeConfig({
      models: [
        { id: 'cny-alt-1', display_name: 'CNY Alt 1', currency: 'CNY', input_price_per_million: 5.0, output_price_per_million: 10.0 },
        { id: 'no-pricing', display_name: 'No Pricing' }, // No pricing fields
      ],
    }, 'models.json');
    const config = loadPricingConfig(path);
    expect(config.models).toHaveLength(1);
    expect(config.models[0].match).toBe('cny-alt-1');
  });

  it('falls back to CNY when currency is not specified', () => {
    const path = writeConfig({
      models: [
        { id: 'test-model', display_name: 'Test', input_price_per_million: 1.0, output_price_per_million: 2.0 },
      ],
    }, 'models.json');
    const config = loadPricingConfig(path);
    expect(config.models[0].currency).toBe('CNY');
  });
});

describe('calculateCost with models.json format [P0-005]', () => {
  it('matches exact model ID (case-insensitive)', () => {
    const config: PricingConfig = {
      exchange_rate: { usd_to_cny: 7.25, updated_at: '', source: 'manual' },
      models: [
        { match: 'cny-alt-1', display_name: 'CNY Alt 1', currency: 'CNY', pricing_unit: 'per_million', input_price: 5.0, output_price: 10.0 },
      ],
    };
    const result = calculateCost(config, 'CNY-ALT-1', 1000, 500);
    expect(result.matched_entry).toBe('CNY Alt 1');
  });
});

// Regression: process-crashing toLowerCase bug (2026-06-16).
// Root cause: models.json can list Claude aliases (id + display_name, no
// pricing) as the FIRST entry. isModelsJsonFormat only inspected models[0]
// and gated on price, so a price-less alias at [0] made it mis-detect the
// whole file as pricing.json format. The raw entries (which have `id`, not
// `match`) then flowed into calculateCost, where entry.match.toLowerCase()
// crashed on undefined → uncaughtException → process restart → message
// queue lost (see fatal-log.ts / cli.ts).
describe('regression: priceless alias + undefined match [bug-20260616]', () => {
  it('recognizes models.json format when first entry is a price-less alias', () => {
    const path = writeConfig({
      models: [
        { id: 'claude-fable-5', display_name: 'Claude Fable 5', claude_alias: true },
        { id: 'cny-alt-1', display_name: 'CNY Alt 1', currency: 'CNY', input_price_per_million: 8, output_price_per_million: 8 },
      ],
    }, 'models.json');
    const config = loadPricingConfig(path);
    // alias filtered out (no price), cny-alt-1 kept with match = id
    expect(config.models).toHaveLength(1);
    expect(config.models[0].match).toBe('cny-alt-1');
  });

  it('calculateCost skips entries with non-string match instead of crashing', () => {
    const config: PricingConfig = {
      exchange_rate: { usd_to_cny: 7.25, updated_at: '', source: 'manual' },
      models: [
        // Simulate a mis-converted entry: match is undefined (the real bug
        // produced this shape when models.json was read as pricing.json).
        { match: undefined as unknown as string, display_name: 'Bad', currency: 'CNY', pricing_unit: 'per_million', input_price: 1, output_price: 1 },
        { match: 'cny-alt', display_name: 'CNY Alt', currency: 'CNY', pricing_unit: 'per_million', input_price: 5, output_price: 10 },
      ],
    };
    const result = calculateCost(config, 'cny-alt-1', 1000, 500);
    expect(result.matched_entry).toBe('CNY Alt');
  });

  it('calculateCost returns zero cost for undefined model instead of crashing', () => {
    const config: PricingConfig = {
      exchange_rate: { usd_to_cny: 7.25, updated_at: '', source: 'manual' },
      models: [
        { match: 'cny-alt', display_name: 'CNY Alt', currency: 'CNY', pricing_unit: 'per_million', input_price: 5, output_price: 10 },
      ],
    };
    const result = calculateCost(config, undefined as unknown as string, 1000, 500);
    expect(result.cost_cny).toBe(0);
  });
});
