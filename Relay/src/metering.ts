import { appendFileSync, mkdirSync, readdirSync, readFileSync } from 'node:fs';
import { EventEmitter } from 'node:events';
import { join } from 'node:path';
import type { SoftQuotaWarningEvent, StreamJsonMessage } from './types.js';
import { loadPricingConfig, calculateCost } from './pricing.js';
import type { PricingConfig } from './pricing.js';
import type { RelayInteractiveLineage } from './relay-interactive-lineage.js';

export interface BudgetInfo {
  agentId: string;
  budgetUsd: number;
}

export interface MeteringOptions {
  meteringDir: string;
  /** Path to pricing.json. If absent, cost calculation is skipped (cost=0). */
  pricingConfigPath?: string;
  /** Balance deduction callback. costCents is in CNY fen (分). Returns true=success. */
  deductBalance?: (tenantId: string, costCents: number, metadata?: { modelId?: string; tokensIn?: number; tokensOut?: number; sessionId?: string }) => Promise<boolean>;
  /** Balance query callback. Returns balance in CNY fen (分), or null on failure. */
  getBalance?: (tenantId: string) => Promise<number | null>;
  /** Map a tenant key (poolKey) to agent's monthly soft budget. */
  resolveBudget?: (tenantId: string) => BudgetInfo | undefined;
  /** Default 0.8. */
  softWarnRatio?: number;
}

export interface LowBalanceEvent {
  tenantId: string;
  balanceCents: number;   // remaining balance in fen
  costCents: number;      // last deduction in fen
  threshold: number;      // 1000 = ¥10
}

export interface Metering {
  record(tenantId: string, sessionId: string, msg: StreamJsonMessage, options?: { fallbackModel?: string; lineage?: RelayInteractiveLineage }): void;
  /** Balance pre-check. Returns true if balance is sufficient for CC spawn. */
  checkBalance(tenantId: string): Promise<boolean>;
  onSoftQuotaWarning(handler: (event: SoftQuotaWarningEvent) => void): void;
  onLowBalance(handler: (event: LowBalanceEvent) => void): void;
  dispose(): void;
}

interface UsageData {
  input_tokens?: number;
  output_tokens?: number;
  cache_creation_input_tokens?: number;
  cache_read_input_tokens?: number;
  cached_input_tokens?: number;
  cache_write_input_tokens?: number;
}

export interface UsageSummary {
  tenant: string;
  monthStart: string;
  monthEnd: string;
  recordCount: number;
  totalCostUsd: number;
  totalCostCny: number;
  totalInputTokens: number;
  totalOutputTokens: number;
  totalCacheCreationInputTokens: number;
  totalCacheReadInputTokens: number;
  daysCovered: number;
  malformedEntries: number;
  unknownUsageEntries: number;
  unknownCostEntries: number;
}

function monthKey(date: Date): string {
  return `${date.getUTCFullYear()}-${String(date.getUTCMonth() + 1).padStart(2, '0')}`;
}

const LOW_BALANCE_THRESHOLD_CENTS = 1000; // ¥10

/**
 * CC/SDK result 的 `modelUsage` 以真实模型 id 为键。混用模型时取 costUSD
 * （缺失时取 token 总量）最大者作为本回合计价模型；无可用键返回 undefined。
 */
function dominantModelUsage(value: unknown): string | undefined {
  if (value === null || typeof value !== 'object' || Array.isArray(value)) return undefined;
  const amount = (raw: unknown, key: string): number => {
    const n = (raw as Record<string, unknown> | null)?.[key];
    return typeof n === 'number' && Number.isFinite(n) && n > 0 ? n : 0;
  };
  let best: { model: string; cost: number; tokens: number } | undefined;
  for (const [model, raw] of Object.entries(value)) {
    if (!model.trim() || raw === null || typeof raw !== 'object') continue;
    const cost = amount(raw, 'costUSD');
    const tokens = ['inputTokens', 'outputTokens', 'cacheReadInputTokens', 'cacheCreationInputTokens']
      .reduce((sum, key) => sum + amount(raw, key), 0);
    if (!best || cost > best.cost || (cost === best.cost && tokens > best.tokens)) best = { model, cost, tokens };
  }
  return best?.model;
}

export function createMetering(options: MeteringOptions): Metering {
  const { meteringDir, resolveBudget, softWarnRatio = 0.8 } = options;
  let dirEnsured = false;

  const pricingConfig: PricingConfig = options.pricingConfigPath
    ? loadPricingConfig(options.pricingConfigPath)
    : { exchange_rate: { usd_to_cny: 0, updated_at: '', source: 'manual' }, models: [] };

  // In-memory monthly cost accumulator; key = `${tenantId}:${YYYY-MM}`. Bootstrap
  // from disk lazily on first record() per (tenant, month) to absorb the cost
  // already spent before this Relay process started.
  const monthlyCostUsd = new Map<string, number>();
  const bootstrappedKeys = new Set<string>();
  const warnedKeys = new Set<string>();
  const lowBalanceWarnedKeys = new Set<string>();
  const emitter = new EventEmitter();
  emitter.setMaxListeners(0);

  function ensureDir(): void {
    if (!dirEnsured) {
      mkdirSync(meteringDir, { recursive: true });
      dirEnsured = true;
    }
  }

  function bootstrapTenantMonth(tenantId: string, mKey: string): number {
    const summary = summarizeMonthlyUsage(meteringDir, tenantId, new Date(`${mKey}-01T00:00:00Z`));
    return summary.totalCostUsd;
  }

  function evaluateSoftQuota(tenantId: string, costThisRecord: number): void {
    if (!resolveBudget) return;
    const budget = resolveBudget(tenantId);
    if (!budget || !(budget.budgetUsd > 0)) return;

    const mKey = monthKey(new Date());
    const accumKey = `${tenantId}:${mKey}`;
    if (!bootstrappedKeys.has(accumKey)) {
      bootstrappedKeys.add(accumKey);
      const seed = bootstrapTenantMonth(tenantId, mKey);
      // Avoid double-counting the just-written record: it is already on disk.
      monthlyCostUsd.set(accumKey, seed);
    } else {
      monthlyCostUsd.set(accumKey, (monthlyCostUsd.get(accumKey) ?? 0) + costThisRecord);
    }

    const total = monthlyCostUsd.get(accumKey) ?? 0;
    if (warnedKeys.has(accumKey)) return;
    if (total < budget.budgetUsd * softWarnRatio) return;

    warnedKeys.add(accumKey);
    emitter.emit('soft-quota-warning', {
      tenantId,
      agentId: budget.agentId,
      monthKey: mKey,
      currentCostUsd: total,
      budgetUsd: budget.budgetUsd,
      ratio: total / budget.budgetUsd,
    } satisfies SoftQuotaWarningEvent);
  }

  function record(tenantId: string, sessionId: string, msg: StreamJsonMessage, recordOpts?: { fallbackModel?: string; lineage?: RelayInteractiveLineage }): void {
    if (msg.type !== 'result') return;

    const usage = (msg.usage ?? {}) as UsageData;
    const resultModel = typeof msg.model === 'string' && msg.model ? msg.model : undefined;
    const usageModel = resultModel ? undefined : dominantModelUsage(msg.modelUsage);
    const model = resultModel ?? usageModel ?? recordOpts?.fallbackModel;
    const count = (value: unknown): number | null => typeof value === 'number' && Number.isSafeInteger(value) && value >= 0 ? value : null;
    const inputTokens = count(usage.input_tokens);
    const outputTokens = count(usage.output_tokens);
    const isCodex = msg.runtime === 'codex';
    const cacheRead = count(isCodex ? usage.cached_input_tokens : usage.cache_read_input_tokens);
    const cacheWrite = isCodex && usage.cache_write_input_tokens === undefined ? 0
      : count(isCodex ? usage.cache_write_input_tokens : usage.cache_creation_input_tokens);
    const usageComplete = [inputTokens, outputTokens, cacheRead, cacheWrite].every(v=>v!==null);

    // Pricing calculation
    let costCny = 0;
    let costUsd: number | undefined;
    let exchangeRate: number | undefined;
    if (model && pricingConfig.models.length > 0 && inputTokens !== null && outputTokens !== null) {
      const costResult = calculateCost(pricingConfig, model, inputTokens, outputTokens);
      costCny = costResult.cost_cny;
      costUsd = costResult.cost_usd;
      exchangeRate = costResult.exchange_rate;
    }
    // Legacy: fallback to total_cost_usd if no pricing config
    const legacyCostUsd = (typeof msg.total_cost_usd === 'number' ? msg.total_cost_usd : 0) || 0;

    const entry: Record<string, unknown> = {
      schema: 'relay_usage_observation/v2',
      ts: new Date().toISOString(),
      tenant: tenantId,
      session_id: sessionId,
      runtime: typeof msg.runtime === 'string' ? msg.runtime : 'unknown',
      usage_status: usageComplete ? 'observed' : 'partial',
      input_semantics: isCodex ? 'inclusive' : 'uncached',
      model_basis: resultModel ? 'result_metadata' : usageModel ? 'result_model_usage' : 'configured_fallback',
      input_tokens: inputTokens,
      output_tokens: outputTokens,
      cache_creation_input_tokens: cacheWrite,
      cache_read_input_tokens: cacheRead,
    };

    if (recordOpts?.lineage) entry.lineage = recordOpts.lineage;
    if (model) entry.model = model;
    if (costCny > 0) entry.cost_cny = costCny;
    if (costUsd !== undefined && costUsd > 0) entry.cost_usd = costUsd;
    else if (legacyCostUsd > 0) entry.cost_usd = legacyCostUsd;
    if (exchangeRate !== undefined) entry.exchange_rate = exchangeRate;

    const today = new Date().toISOString().slice(0, 10);
    const filePath = join(meteringDir, `${today}.jsonl`);

    try {
      ensureDir();
      appendFileSync(filePath, JSON.stringify(entry) + '\n', {mode:0o600});
    } catch (err) {
      console.error({
        ts: new Date().toISOString(),
        level: 'error',
        tenant: tenantId,
        msg: 'Failed to write metering log',
        error: String(err),
      });
    }

    // Async balance deduction (fire-and-forget)
    if (options.deductBalance && costCny > 0) {
      const costCents = Math.round(costCny * 100);
      // Mark as pending synchronously to prevent concurrent low-balance checks
      const shouldCheckLowBalance = !lowBalanceWarnedKeys.has(tenantId) && !!options.getBalance;
      if (shouldCheckLowBalance) lowBalanceWarnedKeys.add(tenantId);
      options.deductBalance(tenantId, costCents, {
        modelId: model,
        tokensIn: inputTokens ?? undefined,
        tokensOut: outputTokens ?? undefined,
        sessionId,
      })
        .then(async (ok) => {
          if (!ok || !shouldCheckLowBalance) return;
          const balance = await options.getBalance!(tenantId);
          if (balance !== null && balance < LOW_BALANCE_THRESHOLD_CENTS) {
            emitter.emit('low-balance', {
              tenantId,
              balanceCents: balance,
              costCents,
              threshold: LOW_BALANCE_THRESHOLD_CENTS,
            } satisfies LowBalanceEvent);
          }
        })
        .catch(err => {
          console.error({
            ts: new Date().toISOString(),
            level: 'error',
            tenant: tenantId,
            msg: 'Balance deduction failed',
            error: String(err),
          });
        });
    }

    try {
      evaluateSoftQuota(tenantId, costUsd ?? legacyCostUsd);
    } catch (err) {
      console.error({
        ts: new Date().toISOString(),
        level: 'error',
        tenant: tenantId,
        msg: 'Soft quota evaluation failed',
        error: String(err),
      });
    }
  }

  async function checkBalance(tenantId: string): Promise<boolean> {
    if (!options.getBalance) return true; // no balance check configured → allow
    try {
      const balance = await options.getBalance(tenantId);
      if (balance === null) return true; // query failed → allow (fail-open)
      return balance > 0;
    } catch {
      return true; // fail-open
    }
  }

  return {
    record,
    checkBalance,
    onSoftQuotaWarning(handler: (event: SoftQuotaWarningEvent) => void): void {
      emitter.on('soft-quota-warning', handler);
    },
    onLowBalance(handler: (event: LowBalanceEvent) => void): void {
      emitter.on('low-balance', handler);
    },
    dispose(): void {
      emitter.removeAllListeners();
      monthlyCostUsd.clear();
      bootstrappedKeys.clear();
      warnedKeys.clear();
      lowBalanceWarnedKeys.clear();
    },
  };
}

const DAILY_FILE_PATTERN = /^(\d{4}-\d{2}-\d{2})\.jsonl$/;

function lastDayOfUtcMonth(year: number, monthIndex: number): number {
  return new Date(Date.UTC(year, monthIndex + 1, 0)).getUTCDate();
}

/**
 * Summarize a tenant's metering records for the calendar month containing `now`.
 *
 * Reads `{meteringDir}/{YYYY-MM-DD}.jsonl` for every file whose date prefix is
 * within the same UTC year+month as `now`. Lines that fail JSON.parse are
 * counted as `malformedEntries` and skipped. Totals are known subtotals only;
 * unknownUsageEntries/unknownCostEntries expose missing observations, not zeros.
 *
 * Read-only: never creates or mutates metering files.
 */
export function summarizeMonthlyUsage(meteringDir: string, tenant: string, now?: Date): UsageSummary {
  const refDate = now ?? new Date();
  const yyyy = refDate.getUTCFullYear();
  const monthIdx = refDate.getUTCMonth();
  const mm = String(monthIdx + 1).padStart(2, '0');
  const monthStart = `${yyyy}-${mm}-01`;
  const refDay = refDate.getUTCDate();
  const lastDay = lastDayOfUtcMonth(yyyy, monthIdx);
  const monthEnd = `${yyyy}-${mm}-${String(Math.min(refDay, lastDay)).padStart(2, '0')}`;

  const summary: UsageSummary = {
    tenant,
    monthStart,
    monthEnd,
    recordCount: 0,
    totalCostUsd: 0,
    totalCostCny: 0,
    totalInputTokens: 0,
    totalOutputTokens: 0,
    totalCacheCreationInputTokens: 0,
    totalCacheReadInputTokens: 0,
    daysCovered: 0,
    malformedEntries: 0,
    unknownUsageEntries: 0,
    unknownCostEntries: 0,
  };

  let entries: string[];
  try {
    entries = readdirSync(meteringDir);
  } catch {
    return summary;
  }

  const monthPrefix = `${yyyy}-${mm}-`;
  const monthFiles = entries
    .filter(name => name.startsWith(monthPrefix) && DAILY_FILE_PATTERN.test(name))
    .sort();

  const daysWithRecords = new Set<string>();
  const numericOrZero = (value: unknown): number => {
    const num = typeof value === 'number' ? value : Number(value);
    return Number.isFinite(num) ? num : 0;
  };

  for (const file of monthFiles) {
    let content: string;
    try {
      content = readFileSync(join(meteringDir, file), 'utf-8');
    } catch {
      continue;
    }
    const day = file.slice(0, 10);
    for (const rawLine of content.split('\n')) {
      const line = rawLine.trim();
      if (!line) continue;
      let entry: Record<string, unknown>;
      try {
        entry = JSON.parse(line) as Record<string, unknown>;
      } catch {
        summary.malformedEntries++;
        continue;
      }
      if (entry['tenant'] !== tenant) continue;
      summary.recordCount++;
      if (entry['usage_status'] === 'partial' || ['input_tokens','output_tokens','cache_read_input_tokens','cache_creation_input_tokens']
        .some(key=>typeof entry[key] !== 'number' || !Number.isFinite(entry[key]) || Number(entry[key]) < 0)) summary.unknownUsageEntries++;
      if (typeof entry['cost_usd'] !== 'number') summary.unknownCostEntries++;
      summary.totalCostUsd += numericOrZero(entry['cost_usd']);
      summary.totalCostCny += numericOrZero(entry['cost_cny']);
      summary.totalInputTokens += numericOrZero(entry['input_tokens']);
      summary.totalOutputTokens += numericOrZero(entry['output_tokens']);
      summary.totalCacheCreationInputTokens += numericOrZero(entry['cache_creation_input_tokens']);
      summary.totalCacheReadInputTokens += numericOrZero(entry['cache_read_input_tokens']);
      daysWithRecords.add(day);
    }
  }

  summary.daysCovered = daysWithRecords.size;
  return summary;
}
