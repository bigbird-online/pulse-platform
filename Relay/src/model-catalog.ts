import { readFileSync } from 'node:fs';

// --- Provider Types (new) ---

/** Provider definition — a supplier of model API access. */
export interface ProviderDef {
  /** Protocol: 'anthropic' (CC runtime) or 'openai' (Codex runtime) */
  protocol: 'anthropic' | 'openai';
  /** Environment variable name holding the provider's base URL */
  base_url_env: string;
  /** API key env var names; order = fallback priority; empty = unavailable */
  api_key_pool: string[];
  /** If true, CC must use --bare interactive mode for this provider */
  force_interactive?: boolean;
}

// --- Model Catalog Types (updated) ---

/** A single model entry in the catalog. */
export interface ModelCatalogEntry {
  /** CC --model argument value, also accepted by /model command */
  id: string;
  /** Display name for /model list output */
  display_name: string;
  /** Provider identifier referencing a key in providers (e.g. "openrouter") */
  provider?: string;
  /** Claude short alias (sonnet/opus/haiku) — resolved by CC internally */
  claude_alias?: boolean;
  /** Pricing currency */
  currency?: 'CNY' | 'USD';
  /** Input price per million tokens */
  input_price_per_million?: number;
  /** Output price per million tokens */
  output_price_per_million?: number;
  /** Priority for auto-fallback when current model's provider is rate-limited.
   *  Lower number = higher priority. Omit or 0 = lowest priority.
   *  Models are compared flat across providers — not scoped by provider. */
  priority?: number;
  // --- Legacy fields (backward compat with old models.json format) ---
  /** @deprecated Use provider + providers.{name}.base_url_env instead */
  endpoint_env?: string;
  /** @deprecated Use provider + providers.{name}.api_key_pool instead */
  api_key_env?: string;
}

/** Top-level model catalog config (models.json format). */
export interface ModelCatalogConfig {
  /** Provider definitions (new format) */
  providers?: Record<string, ProviderDef>;
  /** Model entries */
  models: ModelCatalogEntry[];
}

export interface ModelValidationResult {
  valid: boolean;
  resolvedId: string;
  entry?: ModelCatalogEntry;
}

export interface ModelListEntry {
  id: string;
  display_name: string;
  /** Runtime label for display: 'CC' for anthropic protocol, 'Codex' for openai protocol */
  runtimeLabel?: string;
  /** Whether the provider has at least one available key */
  available?: boolean;
}

export interface ModelCatalog {
  readonly entries: ReadonlyArray<ModelCatalogEntry>;
  readonly providers: Readonly<Record<string, ProviderDef>> | undefined;
  findById(id: string): ModelCatalogEntry | undefined;
  findProvider(providerName: string): ProviderDef | undefined;
  getProviderForModel(modelId: string): ProviderDef | undefined;
  isProviderAvailable(providerName: string): boolean;
  validateModelId(input: string): ModelValidationResult;
  listIds(): ModelListEntry[];
  /** Determine which runtime a model requires based on its provider's protocol */
  runtimeForModel(modelId: string): 'cc' | 'codex' | undefined;
  /** Find the first available model for a given protocol (used as default when no ccModel is set) */
  getDefaultModelForProtocol(protocol: 'anthropic' | 'openai'): ModelCatalogEntry | undefined;
  /** Find the next available model by priority, skipping the current model's
   *  provider and any whose keys are all in 429 cooldown. Models are ranked
   *  flat across providers by their priority field (lower = higher priority). */
  findNextAvailableModel(
    currentModelId: string,
    isProviderKeyCoolingDown: (providerName: string) => boolean,
  ): { providerName: string; provider: ProviderDef; model: ModelCatalogEntry } | undefined;
  reload(): void;
}

// --- Built-in Claude alias resolution (always available) ---

const CLAUDE_ALIASES = new Set(['sonnet', 'opus', 'haiku']);

// --- Implementation ---

export function createModelCatalog(configPath?: string): ModelCatalog {
  let config: ModelCatalogConfig = { models: [] };

  function reload(): void {
    if (!configPath) return;
    try {
      const raw = readFileSync(configPath, 'utf-8');
      const parsed = JSON.parse(raw) as ModelCatalogConfig;
      if (parsed && Array.isArray(parsed.models)) {
        config = parsed;
        console.log({
          ts: new Date().toISOString(),
          level: 'info',
          msg: 'Model catalog loaded',
          models: config.models.length,
          providers: config.providers ? Object.keys(config.providers).length : 0,
          path: configPath,
        });
      }
    } catch (err) {
      if ((err as NodeJS.ErrnoException).code !== 'ENOENT') {
        console.error({
          ts: new Date().toISOString(),
          level: 'warn',
          msg: 'Failed to load model catalog',
          path: configPath,
          error: String(err),
        });
      }
    }
  }

  function findById(id: string): ModelCatalogEntry | undefined {
    return config.models.find(m => m.id === id);
  }

  function findProvider(providerName: string): ProviderDef | undefined {
    return config.providers?.[providerName];
  }

  function getProviderForModel(modelId: string): ProviderDef | undefined {
    const entry = findById(modelId);
    if (!entry?.provider) return undefined;
    return findProvider(entry.provider);
  }

  /**
   * Check if a provider has at least one API key with a non-empty value in process.env.
   * A provider with 0 keys or all keys empty is considered unavailable.
   */
  function isProviderAvailable(providerName: string): boolean {
    const provider = findProvider(providerName);
    if (!provider) return false;
    if (provider.api_key_pool.length === 0) return false;
    return provider.api_key_pool.some(envVar => !!process.env[envVar]);
  }

  /**
   * Determine which runtime a model requires.
   * Returns 'cc' for anthropic protocol providers, 'codex' for openai protocol providers.
   * Returns undefined for models without a provider or unknown providers.
   */
  function runtimeForModel(modelId: string): 'cc' | 'codex' | undefined {
    const provider = getProviderForModel(modelId);
    if (!provider) return undefined;
    return provider.protocol === 'openai' ? 'codex' : 'cc';
  }

  /**
   * Validate a model ID from /model command input.
   * Resolution order:
   * 1. Exact match in model catalog
   * 2. Claude short aliases (sonnet/opus/haiku)
   * 3. claude-* prefix (full Claude model IDs)
   */
  function validateModelId(input: string): ModelValidationResult {
    const trimmed = input.trim();
    if (!trimmed) return { valid: false, resolvedId: trimmed };

    // 1. Exact match in catalog
    const direct = config.models.find(m => m.id === trimmed);
    if (direct) return { valid: true, resolvedId: trimmed, entry: direct };

    // When providers are configured, only accept models in the catalog.
    // Short aliases (sonnet/opus/haiku) and claude-* prefix are only accepted
    // in legacy mode (no model catalog or no providers section).
    if (config.providers && Object.keys(config.providers).length > 0) {
      return { valid: false, resolvedId: trimmed };
    }

    // 2. Claude short aliases (legacy, no providers configured)
    if (CLAUDE_ALIASES.has(trimmed.toLowerCase())) {
      return { valid: true, resolvedId: trimmed.toLowerCase() };
    }

    // 3. Full claude-* IDs (legacy)
    if (trimmed.startsWith('claude-')) {
      return { valid: true, resolvedId: trimmed };
    }

    return { valid: false, resolvedId: trimmed };
  }

  function listIds(): ModelListEntry[] {
    return config.models.map(m => {
      const provider = m.provider ? findProvider(m.provider) : undefined;
      const runtimeLabel = provider?.protocol === 'openai' ? 'Codex' : 'CC';
      // No provider = Claude native = always available.
      // Legacy config (no providers section) has no key-pool info — treat as available.
      const available = m.provider && config.providers ? isProviderAvailable(m.provider) : true;
      return { id: m.id, display_name: m.display_name, runtimeLabel, available };
    });
  }

  /** Find the first available model for a given protocol (used as default when no ccModel is set). */
  function getDefaultModelForProtocol(protocol: 'anthropic' | 'openai'): ModelCatalogEntry | undefined {
    if (!config.providers) return undefined;
    for (const model of config.models) {
      if (!model.provider) continue;
      const prov = config.providers[model.provider];
      if (prov?.protocol === protocol && isProviderAvailable(model.provider)) {
        return model;
      }
    }
    return undefined;
  }

  /** Find the best available model by priority, skipping any whose provider
   *  keys are all in 429 cooldown. Scans from the top (priority=1) every time —
   *  if the previously rate-limited provider has recovered, it will be picked
   *  again. This avoids cascading to low-priority models when the primary
   *  provider's quota resets mid-fallback. */
  function findNextAvailableModel(
    currentModelId: string,
    isProviderKeyCoolingDown: (providerName: string) => boolean,
  ): { providerName: string; provider: ProviderDef; model: ModelCatalogEntry } | undefined {
    if (!config.providers) return undefined;

    // Sort all non-alias models by priority (flat across providers), scan from top
    const candidates = config.models
      .filter(m => !m.claude_alias && m.provider)
      .filter(m => isProviderAvailable(m.provider!))        // has keys in env
      .filter(m => !isProviderKeyCoolingDown(m.provider!))  // not all 429'd
      .sort((a, b) => (a.priority ?? Infinity) - (b.priority ?? Infinity));

    for (const model of candidates) {
      const provider = config.providers![model.provider!];
      if (provider) return { providerName: model.provider!, provider, model };
    }
    return undefined;
  }

  reload();

  return {
    get entries() { return config.models; },
    get providers() { return config.providers; },
    findById,
    findProvider,
    getProviderForModel,
    isProviderAvailable,
    runtimeForModel,
    getDefaultModelForProtocol,
    findNextAvailableModel,
    validateModelId,
    listIds,
    reload,
  };
}