import { readFileSync } from 'node:fs';

// --- Pricing Types ---

export interface PricingEntry {
  match: string;          // model name prefix ('*' = fallback)
  display_name: string;
  currency: 'CNY' | 'USD';
  pricing_unit: 'per_million' | 'per_1k';
  input_price: number;
  output_price: number;
  is_default?: boolean;
  is_default_domestic?: boolean;
  is_default_foreign?: boolean;
}

export interface PricingConfig {
  exchange_rate: { usd_to_cny: number; updated_at: string; source: string };
  models: PricingEntry[];
}

export interface CostResult {
  model: string;
  cost_cny: number;       // CNY cost (yuan)
  cost_usd?: number;      // USD cost (foreign models only)
  exchange_rate?: number;
  matched_entry: string;  // display_name of matched entry
  input_tokens: number;
  output_tokens: number;
}

// --- Config Loading ---

const EMPTY_CONFIG: PricingConfig = {
  exchange_rate: { usd_to_cny: 0, updated_at: '', source: 'manual' },
  models: [],
};

// --- Model Catalog Compatibility (P0-005) ---

/** Models.json entry format (from model catalog) */
interface ModelCatalogEntry {
  id: string;
  display_name: string;
  provider?: string;
  endpoint_env?: string;
  api_key_env?: string;
  currency?: 'CNY' | 'USD';
  input_price_per_million?: number;
  output_price_per_million?: number;
}

/** Detect if config is in models.json format (entries keyed by `id`, not `match`).
 *  Scans ALL entries — models.json legitimately lists price-less aliases
 *  (e.g. claude-fable-5) that must not defeat detection. See bug-20260616. */
function isModelsJsonFormat(parsed: unknown): parsed is { models: ModelCatalogEntry[] } {
  if (!parsed || typeof parsed !== 'object') return false;
  const models = (parsed as Record<string, unknown>).models;
  if (!Array.isArray(models) || models.length === 0) return false;
  return models.some(
    (m) => m && typeof m === 'object' && 'id' in m && 'display_name' in m,
  );
}

/** Convert models.json format to PricingConfig format */
function normalizeModelsJson(raw: unknown): PricingConfig {
  const parsed = raw as { models: ModelCatalogEntry[] };
  const models: PricingEntry[] = parsed.models
    .filter((m) => m.input_price_per_million !== undefined || m.output_price_per_million !== undefined)
    .map((m) => ({
      match: m.id,
      display_name: m.display_name,
      currency: m.currency ?? 'CNY',
      pricing_unit: 'per_million' as const,
      input_price: m.input_price_per_million ?? 0,
      output_price: m.output_price_per_million ?? 0,
    }));
  return {
    exchange_rate: { usd_to_cny: 7.25, updated_at: new Date().toISOString(), source: 'manual' },
    models,
  };
}

export function loadPricingConfig(configPath: string): PricingConfig {
  try {
    const raw = readFileSync(configPath, 'utf-8');
    const parsed = JSON.parse(raw) as unknown;

    // P0-005: Support models.json format (model catalog)
    if (isModelsJsonFormat(parsed)) {
      return normalizeModelsJson(parsed);
    }

    // Standard pricing.json format
    const pricingParsed = parsed as PricingConfig;
    if (pricingParsed && Array.isArray(pricingParsed.models)) {
      return pricingParsed;
    }
    return EMPTY_CONFIG;
  } catch (err) {
    if ((err as NodeJS.ErrnoException).code !== 'ENOENT') {
      console.error({
        ts: new Date().toISOString(),
        level: 'warn',
        msg: 'Failed to load pricing config, using empty config',
        path: configPath,
        error: String(err),
      });
    }
    return EMPTY_CONFIG;
  }
}

// --- Cost Calculation ---

const DIVISORS: Record<string, number> = {
  per_million: 1_000_000,
  per_1k: 1_000,
};

export function calculateCost(
  config: PricingConfig,
  model: string,
  inputTokens: number,
  outputTokens: number,
): CostResult {
  const result: CostResult = {
    model,
    cost_cny: 0,
    matched_entry: '',
    input_tokens: inputTokens,
    output_tokens: outputTokens,
  };

  // Untrusted-config guard: a malformed entry or model must not crash the
  // process. See bug-20260616 (entry.match undefined → toLowerCase crash).
  if (typeof model !== 'string') return result;

  // Find matching entry
  // P0-005: Support both prefix matching (pricing.json) and exact matching (models.json)
  let matched: PricingEntry | undefined;
  let fallback: PricingEntry | undefined;

  const modelLower = typeof model === 'string' ? model.toLowerCase() : '';
  for (const entry of config.models) {
    // A malformed pricing entry (no `match`, e.g. a model-catalog row that has
    // no price fields and slipped through format detection) must never crash the
    // whole Relay via `entry.match.toLowerCase()` on undefined — skip it. This
    // is the sole turn-completion path for metering, so an unguarded throw here
    // takes the process down mid-turn and strands every in-flight Feishu card.
    if (typeof entry.match !== 'string' || !entry.match) continue;
    if (entry.match === '*') {
      fallback = entry;
      continue;
    }
    const matchLower = entry.match.toLowerCase();
    // Exact match (models.json format uses id as match)
    if (matchLower === modelLower) {
      matched = entry;
      break;
    }
    // Prefix match (pricing.json format)
    if (modelLower.startsWith(matchLower)) {
      matched = entry;
      break;
    }
  }

  const entry = matched ?? fallback;
  if (!entry) {
    console.error({
      ts: new Date().toISOString(),
      level: 'warn',
      msg: 'No pricing match for model',
      model,
    });
    return result;
  }

  result.matched_entry = entry.display_name;
  const divisor = DIVISORS[entry.pricing_unit] ?? 1_000_000;
  const raw = (inputTokens * entry.input_price + outputTokens * entry.output_price) / divisor;

  if (entry.currency === 'USD') {
    result.cost_usd = raw;
    const rate = config.exchange_rate.usd_to_cny || 7.25;
    result.cost_cny = raw * rate;
    result.exchange_rate = rate;
  } else {
    result.cost_cny = raw;
  }

  return result;
}
