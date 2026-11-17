import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { mkdirSync, rmSync, readFileSync, writeFileSync, existsSync } from 'node:fs';
import { join } from 'node:path';
import { tmpdir } from 'node:os';
import { createMetering, summarizeMonthlyUsage } from '../src/metering.js';
import type { PricingConfig } from '../src/pricing.js';

const testDir = join(tmpdir(), 'relay-metering-test-' + Date.now());

beforeEach(() => mkdirSync(testDir, { recursive: true }));
afterEach(() => rmSync(testDir, { recursive: true, force: true }));

function writePricingConfig(): string {
  const config: PricingConfig = {
    exchange_rate: { usd_to_cny: 7.25, updated_at: '2026-05-27T10:00:00+08:00', source: 'manual' },
    models: [
      { match: 'cny-priced', display_name: 'CNY Priced', currency: 'CNY', pricing_unit: 'per_million', input_price: 2.0, output_price: 8.0 },
      { match: 'claude-sonnet', display_name: 'Claude Sonnet', currency: 'USD', pricing_unit: 'per_million', input_price: 3.0, output_price: 15.0 },
      { match: '*', display_name: 'Fallback', currency: 'CNY', pricing_unit: 'per_million', input_price: 5.0, output_price: 15.0, is_default: true },
    ],
  };
  const path = join(testDir, 'pricing.json');
  writeFileSync(path, JSON.stringify(config));
  return path;
}

describe('createMetering', () => {
  it('records result message to daily JSONL file', () => {
    const metering = createMetering({ meteringDir: testDir });

    metering.record('member-000', 'session-uuid', {
      type: 'result',
      total_cost_usd: 0.0625,
      usage: {
        input_tokens: 100,
        output_tokens: 50,
        cache_creation_input_tokens: 9000,
        cache_read_input_tokens: 11000,
      },
    });

    const today = new Date().toISOString().slice(0, 10);
    const filePath = join(testDir, `${today}.jsonl`);
    expect(existsSync(filePath)).toBe(true);

    const content = readFileSync(filePath, 'utf-8').trim();
    const record = JSON.parse(content);
    expect(record.tenant).toBe('member-000');
    expect(record.session_id).toBe('session-uuid');
    expect(record.cost_usd).toBe(0.0625);
    expect(record.input_tokens).toBe(100);
    expect(record.output_tokens).toBe(50);
    expect(record.ts).toBeDefined();
  });

  it('each line is valid JSON', () => {
    const metering = createMetering({ meteringDir: testDir });

    metering.record('member-000', 's1', {
      type: 'result',
      total_cost_usd: 0.01,
      usage: { input_tokens: 10, output_tokens: 5 },
    });
    metering.record('member-001', 's2', {
      type: 'result',
      total_cost_usd: 0.02,
      usage: { input_tokens: 20, output_tokens: 10 },
    });

    const today = new Date().toISOString().slice(0, 10);
    const lines = readFileSync(join(testDir, `${today}.jsonl`), 'utf-8').trim().split('\n');
    expect(lines).toHaveLength(2);
    for (const line of lines) {
      expect(() => JSON.parse(line)).not.toThrow();
    }
  });

  it('auto-creates metering directory if missing', () => {
    const nestedDir = join(testDir, 'nested', 'dir');
    expect(existsSync(nestedDir)).toBe(false);

    const metering = createMetering({ meteringDir: nestedDir });
    metering.record('member-000', 's1', {
      type: 'result',
      total_cost_usd: 0.01,
      usage: { input_tokens: 1, output_tokens: 1 },
    });

    expect(existsSync(nestedDir)).toBe(true);
  });

  it('ignores non-result messages', () => {
    const metering = createMetering({ meteringDir: testDir });
    metering.record('member-000', 's1', { type: 'assistant', content: 'hello' });

    const today = new Date().toISOString().slice(0, 10);
    expect(existsSync(join(testDir, `${today}.jsonl`))).toBe(false);
  });

  it('does not throw on write failure', () => {
    const spy = vi.spyOn(console, 'error').mockImplementation(() => {});
    const metering = createMetering({ meteringDir: '/dev/null/impossible' });
    expect(() => {
      metering.record('member-000', 's1', {
        type: 'result',
        total_cost_usd: 0.01,
        usage: { input_tokens: 1, output_tokens: 1 },
      });
    }).not.toThrow();
    spy.mockRestore();
  });
});

describe('pricing integration (assistant usage in synthetic result)', () => {
  it('calculates cost_cny for a CNY-priced model from synthetic result', () => {
    const pricingPath = writePricingConfig();
    const metering = createMetering({ meteringDir: testDir, pricingConfigPath: pricingPath });

    metering.record('member-000', 's1', {
      type: 'result',
      result: '',
      model: 'cny-priced-model',
      usage: { input_tokens: 5000, output_tokens: 2000 },
    });

    const today = new Date().toISOString().slice(0, 10);
    const record = JSON.parse(readFileSync(join(testDir, `${today}.jsonl`), 'utf-8').trim());
    expect(record.model).toBe('cny-priced-model');
    expect(record.input_tokens).toBe(5000);
    expect(record.output_tokens).toBe(2000);
    expect(record.cost_cny).toBeCloseTo(0.026, 5);
    expect(record.cost_usd).toBeUndefined();
  });

  it('calculates cost_cny with exchange rate for foreign model', () => {
    const pricingPath = writePricingConfig();
    const metering = createMetering({ meteringDir: testDir, pricingConfigPath: pricingPath });

    metering.record('member-000', 's1', {
      type: 'result',
      result: '',
      model: 'claude-sonnet-4-20250514',
      usage: { input_tokens: 10000, output_tokens: 3000 },
    });

    const today = new Date().toISOString().slice(0, 10);
    const record = JSON.parse(readFileSync(join(testDir, `${today}.jsonl`), 'utf-8').trim());
    expect(record.model).toBe('claude-sonnet-4-20250514');
    expect(record.cost_usd).toBeCloseTo(0.075, 5);
    expect(record.cost_cny).toBeCloseTo(0.075 * 7.25, 5);
    expect(record.exchange_rate).toBe(7.25);
  });

  it('writes cost=0 when no model in result (backward compat)', () => {
    const pricingPath = writePricingConfig();
    const metering = createMetering({ meteringDir: testDir, pricingConfigPath: pricingPath });

    metering.record('member-000', 's1', {
      type: 'result',
      result: '',
      usage: { input_tokens: 100, output_tokens: 50 },
    });

    const today = new Date().toISOString().slice(0, 10);
    const record = JSON.parse(readFileSync(join(testDir, `${today}.jsonl`), 'utf-8').trim());
    expect(record.model).toBeUndefined();
    expect(record.cost_cny).toBeUndefined();
    expect(record.input_tokens).toBe(100);
  });

  it('calls deductBalance callback with cost in cents', async () => {
    const pricingPath = writePricingConfig();
    const deductions: Array<{ tenant: string; cents: number }> = [];
    const metering = createMetering({
      meteringDir: testDir,
      pricingConfigPath: pricingPath,
      deductBalance: async (tenant, cents) => {
        deductions.push({ tenant, cents });
        return true;
      },
    });

    metering.record('member-000', 's1', {
      type: 'result',
      result: '',
      model: 'cny-priced-model',
      usage: { input_tokens: 5000, output_tokens: 2000 },
    });

    await new Promise(r => setTimeout(r, 50));
    expect(deductions).toHaveLength(1);
    expect(deductions[0].tenant).toBe('member-000');
    expect(deductions[0].cents).toBe(Math.round(0.026 * 100));
  });
});

describe('balance pre-check', () => {
  it('returns true when no getBalance configured', async () => {
    const metering = createMetering({ meteringDir: testDir });
    expect(await metering.checkBalance('tenant-1')).toBe(true);
  });

  it('returns true when balance > 0', async () => {
    const metering = createMetering({ meteringDir: testDir, getBalance: async () => 5000 });
    expect(await metering.checkBalance('tenant-1')).toBe(true);
  });

  it('returns false when balance <= 0', async () => {
    const metering = createMetering({ meteringDir: testDir, getBalance: async () => 0 });
    expect(await metering.checkBalance('tenant-1')).toBe(false);
  });

  it('returns true (fail-open) when getBalance throws', async () => {
    const metering = createMetering({
      meteringDir: testDir,
      getBalance: async () => { throw new Error('API down'); },
    });
    expect(await metering.checkBalance('tenant-1')).toBe(true);
  });

  it('returns true (fail-open) when getBalance returns null', async () => {
    const metering = createMetering({ meteringDir: testDir, getBalance: async () => null });
    expect(await metering.checkBalance('tenant-1')).toBe(true);
  });
});

describe('low balance event', () => {
  it('emits low-balance when balance drops below threshold after deduction', async () => {
    const pricingPath = writePricingConfig();
    const events: Array<import('../src/metering.js').LowBalanceEvent> = [];
    const metering = createMetering({
      meteringDir: testDir,
      pricingConfigPath: pricingPath,
      deductBalance: async () => true,
      getBalance: async () => 800, // ¥8 < ¥10
    });
    metering.onLowBalance((e) => events.push(e));

    metering.record('member-000', 's1', {
      type: 'result', result: '', model: 'cny-priced-model',
      usage: { input_tokens: 5000, output_tokens: 2000 },
    });

    await new Promise(r => setTimeout(r, 100));
    expect(events).toHaveLength(1);
    expect(events[0].tenantId).toBe('member-000');
    expect(events[0].balanceCents).toBe(800);
    expect(events[0].threshold).toBe(1000);
  });

  it('does not fire low-balance twice for same tenant', async () => {
    const pricingPath = writePricingConfig();
    const events: Array<import('../src/metering.js').LowBalanceEvent> = [];
    const metering = createMetering({
      meteringDir: testDir,
      pricingConfigPath: pricingPath,
      deductBalance: async () => true,
      getBalance: async () => 500,
    });
    metering.onLowBalance((e) => events.push(e));

    metering.record('member-000', 's1', {
      type: 'result', result: '', model: 'cny-priced-model',
      usage: { input_tokens: 5000, output_tokens: 2000 },
    });
    metering.record('member-000', 's2', {
      type: 'result', result: '', model: 'cny-priced-model',
      usage: { input_tokens: 5000, output_tokens: 2000 },
    });

    await new Promise(r => setTimeout(r, 100));
    expect(events).toHaveLength(1);
  });
});

describe('summarizeMonthlyUsage', () => {
  function writeJsonl(file: string, entries: unknown[]): void {
    writeFileSync(file, entries.map(e => JSON.stringify(e)).join('\n') + '\n');
  }

  it('returns zero summary when metering dir is missing', () => {
    const ref = new Date(Date.UTC(2026, 4, 15));
    const summary = summarizeMonthlyUsage(join(testDir, 'absent'), 'owner:main', ref);
    expect(summary.recordCount).toBe(0);
    expect(summary.totalCostUsd).toBe(0);
    expect(summary.daysCovered).toBe(0);
    expect(summary.monthStart).toBe('2026-05-01');
    expect(summary.monthEnd).toBe('2026-05-15');
  });

  it('aggregates one tenant across multiple days within month', () => {
    writeJsonl(join(testDir, '2026-05-02.jsonl'), [
      { ts: 't', tenant: 'owner:main', session_id: 's', cost_usd: 0.10, input_tokens: 100, output_tokens: 50, cache_creation_input_tokens: 1000, cache_read_input_tokens: 2000 },
      { ts: 't', tenant: 'owner:main', session_id: 's', cost_usd: 0.20, input_tokens: 200, output_tokens: 80, cache_creation_input_tokens: 0, cache_read_input_tokens: 500 },
    ]);
    writeJsonl(join(testDir, '2026-05-03.jsonl'), [
      { ts: 't', tenant: 'owner:main', session_id: 's', cost_usd: 0.05, input_tokens: 50, output_tokens: 25, cache_creation_input_tokens: 0, cache_read_input_tokens: 0 },
    ]);

    const ref = new Date(Date.UTC(2026, 4, 15));
    const summary = summarizeMonthlyUsage(testDir, 'owner:main', ref);
    expect(summary.recordCount).toBe(3);
    expect(summary.totalCostUsd).toBeCloseTo(0.35, 5);
    expect(summary.totalInputTokens).toBe(350);
    expect(summary.totalOutputTokens).toBe(155);
    expect(summary.daysCovered).toBe(2);
    expect(summary.malformedEntries).toBe(0);
  });

  it('filters out other tenants and other months', () => {
    writeJsonl(join(testDir, '2026-05-10.jsonl'), [
      { ts: 't', tenant: 'owner:main', cost_usd: 1, input_tokens: 1, output_tokens: 1 },
      { ts: 't', tenant: 'other:agent', cost_usd: 99, input_tokens: 999, output_tokens: 999 },
    ]);
    writeJsonl(join(testDir, '2026-04-30.jsonl'), [
      { ts: 't', tenant: 'owner:main', cost_usd: 50, input_tokens: 500, output_tokens: 500 },
    ]);

    const ref = new Date(Date.UTC(2026, 4, 15));
    const summary = summarizeMonthlyUsage(testDir, 'owner:main', ref);
    expect(summary.recordCount).toBe(1);
    expect(summary.totalCostUsd).toBe(1);
    expect(summary.daysCovered).toBe(1);
  });

  it('counts malformed lines but does not throw', () => {
    writeFileSync(join(testDir, '2026-05-05.jsonl'),
      JSON.stringify({ tenant: 'owner:main', cost_usd: 0.5, input_tokens: 10, output_tokens: 5 }) + '\n' +
      'this-is-not-json\n' +
      JSON.stringify({ tenant: 'owner:main', cost_usd: 0.5, input_tokens: 10, output_tokens: 5 }) + '\n',
    );

    const ref = new Date(Date.UTC(2026, 4, 15));
    const summary = summarizeMonthlyUsage(testDir, 'owner:main', ref);
    expect(summary.recordCount).toBe(2);
    expect(summary.totalCostUsd).toBeCloseTo(1.0, 5);
    expect(summary.malformedEntries).toBe(1);
  });

  it('treats missing numeric fields as zero', () => {
    writeJsonl(join(testDir, '2026-05-06.jsonl'), [
      { tenant: 'owner:main' },
      { tenant: 'owner:main', cost_usd: '0.1', input_tokens: '20' },
    ]);

    const ref = new Date(Date.UTC(2026, 4, 15));
    const summary = summarizeMonthlyUsage(testDir, 'owner:main', ref);
    expect(summary.recordCount).toBe(2);
    expect(summary.totalCostUsd).toBeCloseTo(0.1, 5);
    expect(summary.totalInputTokens).toBe(20);
  });

  it('uses today as monthEnd when within current month', () => {
    const ref = new Date(Date.UTC(2026, 4, 7));
    const summary = summarizeMonthlyUsage(testDir, 'absent-tenant', ref);
    expect(summary.monthStart).toBe('2026-05-01');
    expect(summary.monthEnd).toBe('2026-05-07');
  });

  it('aggregates cost_cny field', () => {
    writeJsonl(join(testDir, '2026-05-10.jsonl'), [
      { ts: 't', tenant: 'owner:main', cost_cny: 1.5, input_tokens: 100, output_tokens: 50 },
      { ts: 't', tenant: 'owner:main', cost_cny: 2.3, input_tokens: 200, output_tokens: 80 },
    ]);

    const ref = new Date(Date.UTC(2026, 4, 15));
    const summary = summarizeMonthlyUsage(testDir, 'owner:main', ref);
    expect(summary.totalCostCny).toBeCloseTo(3.8, 5);
  });
});

describe('soft quota warning (B2)', () => {
  function resultMsg(cost: number) {
    return {
      type: 'result',
      total_cost_usd: cost,
      usage: { input_tokens: 10, output_tokens: 5 },
    } as unknown as import('../src/types.js').StreamJsonMessage;
  }

  it('does not emit when no budget is configured', () => {
    const metering = createMetering({ meteringDir: testDir });
    const events: unknown[] = [];
    metering.onSoftQuotaWarning((e) => events.push(e));
    metering.record('owner:main', 's', resultMsg(50));
    metering.record('owner:main', 's', resultMsg(100));
    expect(events).toHaveLength(0);
  });

  it('emits once when monthly cost crosses 80% of budget', () => {
    const metering = createMetering({
      meteringDir: testDir,
      resolveBudget: (tenant) => tenant === 'owner:main' ? { agentId: 'main', budgetUsd: 10 } : undefined,
    });
    const events: import('../src/types.js').SoftQuotaWarningEvent[] = [];
    metering.onSoftQuotaWarning((e) => events.push(e));

    metering.record('owner:main', 's', resultMsg(2));
    expect(events).toHaveLength(0);

    metering.record('owner:main', 's', resultMsg(7));
    expect(events).toHaveLength(1);
    expect(events[0]).toMatchObject({ tenantId: 'owner:main', agentId: 'main', budgetUsd: 10 });
    expect(events[0].ratio).toBeGreaterThanOrEqual(0.8);
  });

  it('does not double-fire within the same month', () => {
    const metering = createMetering({
      meteringDir: testDir,
      resolveBudget: () => ({ agentId: 'main', budgetUsd: 10 }),
    });
    const events: unknown[] = [];
    metering.onSoftQuotaWarning((e) => events.push(e));
    metering.record('owner:main', 's', resultMsg(9));
    metering.record('owner:main', 's', resultMsg(2));
    metering.record('owner:main', 's', resultMsg(3));
    expect(events).toHaveLength(1);
  });

  it('respects custom softWarnRatio', () => {
    const metering = createMetering({
      meteringDir: testDir,
      resolveBudget: () => ({ agentId: 'main', budgetUsd: 10 }),
      softWarnRatio: 0.5,
    });
    const events: unknown[] = [];
    metering.onSoftQuotaWarning((e) => events.push(e));
    metering.record('owner:main', 's', resultMsg(3));
    expect(events).toHaveLength(0);
    metering.record('owner:main', 's', resultMsg(3));
    expect(events).toHaveLength(1);
    expect(events[0].ratio).toBeGreaterThanOrEqual(0.5);
  });

  it('seeds from disk on first record so cross-restart cost survives', () => {
    const today = new Date().toISOString().slice(0, 10);
    writeFileSync(join(testDir, `${today}.jsonl`),
      JSON.stringify({ ts: 't', tenant: 'owner:main', cost_usd: 8.5, input_tokens: 0, output_tokens: 0 }) + '\n',
    );

    const metering = createMetering({
      meteringDir: testDir,
      resolveBudget: () => ({ agentId: 'main', budgetUsd: 10 }),
    });
    const events: unknown[] = [];
    metering.onSoftQuotaWarning((e) => events.push(e));
    metering.record('owner:main', 's', resultMsg(0.5));
    expect(events).toHaveLength(1);
  });
});

describe('versioned operational usage observations',()=>{
  it('preserves missing counters and exposes monthly unknown counts',()=>{
    const meter=createMetering({meteringDir:testDir});meter.record('test','s',{type:'result'});
    const record=JSON.parse(readFileSync(join(testDir,new Date().toISOString().slice(0,10)+'.jsonl'),'utf8').trim());
    expect(record.schema).toBe('relay_usage_observation/v2');expect(record.input_tokens).toBeNull();
    expect(record.cache_read_input_tokens).toBeNull();expect(record.usage_status).toBe('partial');
  });
  it('records the CC runtime tag and prefers the dominant modelUsage id over the configured fallback',()=>{
    const meter=createMetering({meteringDir:testDir});
    meter.record('test','s',{type:'result',runtime:'cc',usage:{input_tokens:1,output_tokens:2},modelUsage:{
      'claude-haiku-4-5':{inputTokens:900,outputTokens:10,costUSD:0.001},
      'claude-opus-4-5':{inputTokens:5,outputTokens:200,costUSD:0.02}}},{fallbackModel:'opus'});
    meter.record('test','s',{type:'result',runtime:'cc',modelUsage:{}},{fallbackModel:'opus'});
    const [observed,fallback]=readFileSync(join(testDir,new Date().toISOString().slice(0,10)+'.jsonl'),'utf8').trim().split('\n').map(line=>JSON.parse(line));
    expect(observed).toMatchObject({runtime:'cc',model:'claude-opus-4-5',model_basis:'result_model_usage'});
    expect(fallback).toMatchObject({runtime:'cc',model:'opus',model_basis:'configured_fallback'});
  });
  it('maps Codex inclusive input/cache fields without treating cache as new input',()=>{
    const meter=createMetering({meteringDir:testDir});meter.record('test','s',{type:'result',runtime:'codex',
      usage:{input_tokens:100,cached_input_tokens:80,output_tokens:2}});
    const record=JSON.parse(readFileSync(join(testDir,new Date().toISOString().slice(0,10)+'.jsonl'),'utf8').trim());
    expect(record.input_tokens).toBe(100);expect(record.cache_read_input_tokens).toBe(80);
    expect(record.cache_creation_input_tokens).toBe(0);expect(record.input_semantics).toBe('inclusive');
  });
});
