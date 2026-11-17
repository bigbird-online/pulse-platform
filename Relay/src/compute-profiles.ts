import { readFileSync } from 'node:fs';
import { homedir } from 'node:os';
import { join } from 'node:path';
import { resolvePulseRoot } from './config-paths.js';
import { managedRelayPath } from './managed-paths.js';

export const DEFAULT_COMPUTE_PROFILE = 'platform_default';
/**
 * PR review by the other model family. The kernel compares a reviewer's actual
 * model with this profile's resolution, so the profile must follow the current
 * flagship (a latest alias, never a pinned version) at reasoning effort >= high.
 */
export const REVIEW_COMPUTE_PROFILE = 'review';
const REASONING_EFFORTS = new Set(['low', 'medium', 'high', 'xhigh', 'max']);
const REVIEW_REASONING_EFFORTS = new Set(['high', 'xhigh', 'max']);
// Review is fixed to the flagship families; only the alias resolution moves.
const REVIEW_CC_ALIAS = 'claude-opus-latest';
const REVIEW_CODEX_ALIAS = 'codex-sol-latest';
const MAX_CODEX_MODEL_CATALOG_AGE_MS = 7 * 24 * 60 * 60 * 1000;
const MAX_CODEX_MODEL_CATALOG_CLOCK_SKEW_MS = 5 * 60 * 1000;

export interface ComputeProfile {
  cc: {
    model: string;
    modelAlias?: string;
    reasoningEffort?: string;
  };
  codex: {
    model: string;
    modelAlias: string;
    reasoningEffort: string;
  };
}

interface ComputeProfilesFile {
  schema_version: number;
  profiles: Record<string, {
    cc?: { model?: unknown; model_alias?: unknown; reasoning_effort?: unknown };
    codex?: { model?: unknown; model_alias?: unknown; reasoning_effort?: unknown };
  }>;
}

interface CodexModelsCache {
  fetched_at?: unknown;
  models?: Array<{
    slug?: unknown;
    visibility?: unknown;
    supported_reasoning_levels?: Array<{ effort?: unknown }>;
  }>;
}

export function resolveComputeProfilesPath(): string {
  return process.env['PULSE_COMPUTE_PROFILES_PATH']
    ?? join(resolvePulseRoot(), 'Relay', 'config', 'compute-profiles.json');
}

export function resolveCodexModelsCachePath(): string {
  if (process.env['PULSE_CODEX_MODELS_CACHE_PATH']) {
    return process.env['PULSE_CODEX_MODELS_CACHE_PATH'];
  }
  const codexHome = process.env['CODEX_HOME'] ?? join(homedir(), '.codex');
  return join(codexHome, 'models_cache.json');
}

function requiredString(value: unknown, field: string): string {
  if (typeof value !== 'string' || !value.trim()) {
    throw new Error(`compute profile field ${field} must be a non-empty string`);
  }
  return value.trim();
}

function optionalString(value: unknown, field: string): string | undefined {
  if (value === undefined) return undefined;
  return requiredString(value, field);
}

function reasoningEffort(value: unknown, field: string, optional = false): string | undefined {
  const effort = optional ? optionalString(value, field) : requiredString(value, field);
  if (effort !== undefined && !REASONING_EFFORTS.has(effort)) {
    throw new Error(`compute profile field ${field} has unsupported effort: ${effort}`);
  }
  return effort;
}

function resolveClaudeLatestAlias(alias: string, field: string): string {
  const match = /^claude-(sonnet|opus|haiku)-latest$/.exec(alias);
  if (!match) {
    throw new Error(`compute profile field ${field} has unsupported Claude alias: ${alias}`);
  }
  // Claude Code owns these short aliases and resolves them to its current model.
  return match[1];
}

function compareNumericVersions(left: number[], right: number[]): number {
  const width = Math.max(left.length, right.length);
  for (let index = 0; index < width; index++) {
    const difference = (left[index] ?? 0) - (right[index] ?? 0);
    if (difference !== 0) return difference;
  }
  return 0;
}

function codexFamily(alias: string, field: string): string {
  const aliasMatch = /^codex-([a-z0-9-]+)-latest$/.exec(alias);
  if (!aliasMatch) {
    throw new Error(`compute profile field ${field} has unsupported Codex alias: ${alias}`);
  }
  return aliasMatch[1];
}

function readCodexCatalog(): { cache: CodexModelsCache & { models: NonNullable<CodexModelsCache['models']> }; cachePath: string } {
  const cachePath = resolveCodexModelsCachePath();
  let cache: CodexModelsCache;
  try {
    cache = JSON.parse(readFileSync(cachePath, 'utf-8')) as CodexModelsCache;
  } catch (err) {
    throw new Error(`Codex model catalog unreadable at ${cachePath}: ${String(err)}`);
  }
  if (!Array.isArray(cache.models)) {
    throw new Error(`Codex model catalog invalid schema at ${cachePath}`);
  }
  const fetchedAt = typeof cache.fetched_at === 'string' ? Date.parse(cache.fetched_at) : Number.NaN;
  const catalogAge = Date.now() - fetchedAt;
  if (!Number.isFinite(fetchedAt)
    || catalogAge > MAX_CODEX_MODEL_CATALOG_AGE_MS
    || catalogAge < -MAX_CODEX_MODEL_CATALOG_CLOCK_SKEW_MS) {
    throw new Error(`Codex model catalog stale or missing fetched_at at ${cachePath}`);
  }
  return { cache: cache as CodexModelsCache & { models: NonNullable<CodexModelsCache['models']> }, cachePath };
}

function resolveCodexLatestAlias(alias: string, effort: string, field: string): string {
  const family = codexFamily(alias, field);
  const { cache, cachePath } = readCodexCatalog();
  const candidates = cache.models.flatMap(model => {
    if (typeof model.slug !== 'string' || model.visibility !== 'list') return [];
    const match = new RegExp(`^gpt-(\\d+(?:\\.\\d+)*)-${family}$`).exec(model.slug);
    if (!match) return [];
    const supportsEffort = Array.isArray(model.supported_reasoning_levels)
      && model.supported_reasoning_levels.some(level => level?.effort === effort);
    if (!supportsEffort) return [];
    return [{ slug: model.slug, version: match[1].split('.').map(Number) }];
  });
  candidates.sort((left, right) => compareNumericVersions(right.version, left.version));
  const selected = candidates[0];
  if (!selected) {
    throw new Error(`Codex alias ${alias} has no visible model supporting effort ${effort} in ${cachePath}`);
  }
  return selected.slug;
}

/**
 * Review never downgrades the model: take the newest visible model of the family
 * first, then the configured effort or the next stronger one it supports.
 */
function resolveCodexReview(alias: string, effort: string, field: string): { model: string; effort: string } {
  const family = codexFamily(alias, field);
  const { cache, cachePath } = readCodexCatalog();
  const candidates = cache.models.flatMap(model => {
    if (typeof model.slug !== 'string' || model.visibility !== 'list') return [];
    const match = new RegExp(`^gpt-(\\d+(?:\\.\\d+)*)-${family}$`).exec(model.slug);
    return match ? [{ model, version: match[1].split('.').map(Number) }] : [];
  });
  candidates.sort((left, right) => compareNumericVersions(right.version, left.version));
  const newest = candidates[0];
  if (!newest) throw new Error(`Codex alias ${alias} has no visible model in ${cachePath}`);
  const supported = new Set((newest.model.supported_reasoning_levels ?? []).map(level => level?.effort));
  const order = ['high', 'xhigh', 'max'];
  const selected = order.slice(order.indexOf(effort)).find(value => supported.has(value));
  if (!selected) {
    throw new Error(`Codex alias ${alias} newest model ${String(newest.model.slug)} supports no effort >= ${effort}`);
  }
  return { model: newest.model.slug as string, effort: selected };
}

interface CheckedProfile {
  name: string;
  ccModel?: string;
  ccModelAlias?: string;
  ccEffort?: string;
  codexModelAlias: string;
  codexEffort: string;
}

/** Read and validate one profile for both providers before resolving any alias. */
function readProfile(name: string): CheckedProfile {
  const profileName = requiredString(name, 'name');
  const path = resolveComputeProfilesPath();
  let parsed: ComputeProfilesFile;
  try {
    parsed = JSON.parse(readFileSync(path, 'utf-8')) as ComputeProfilesFile;
  } catch (err) {
    throw new Error(`compute profiles unreadable at ${path}: ${String(err)}`);
  }
  if (parsed.schema_version !== 2 || !parsed.profiles || typeof parsed.profiles !== 'object') {
    throw new Error(`compute profiles invalid schema at ${path}`);
  }
  const raw = parsed.profiles[profileName];
  if (!raw || typeof raw !== 'object') {
    throw new Error(`unknown compute profile: ${profileName}`);
  }
  const ccModel = optionalString(raw.cc?.model, `${profileName}.cc.model`);
  const ccModelAlias = optionalString(raw.cc?.model_alias, `${profileName}.cc.model_alias`);
  if ((ccModel ? 1 : 0) + (ccModelAlias ? 1 : 0) !== 1) {
    throw new Error(`compute profile ${profileName}.cc must set exactly one of model or model_alias`);
  }
  const codexModelAlias = requiredString(raw.codex?.model_alias, `${profileName}.codex.model_alias`);
  if (raw.codex?.model !== undefined) {
    throw new Error(`compute profile ${profileName}.codex.model literals are forbidden; use model_alias`);
  }
  const codexEffort = reasoningEffort(
    raw.codex?.reasoning_effort,
    `${profileName}.codex.reasoning_effort`,
  )!;
  const ccEffort = reasoningEffort(raw.cc?.reasoning_effort, `${profileName}.cc.reasoning_effort`, true);
  if (profileName === REVIEW_COMPUTE_PROFILE) {
    if (ccModelAlias !== REVIEW_CC_ALIAS || codexModelAlias !== REVIEW_CODEX_ALIAS) {
      if (ccModel) {
        throw new Error(`compute profile ${profileName}.cc must use a latest model_alias, not a pinned model`);
      }
      throw new Error(`compute profile ${profileName} must use ${REVIEW_CC_ALIAS} and ${REVIEW_CODEX_ALIAS}`);
    }
    if (!ccModelAlias) {
      throw new Error(`compute profile ${profileName}.cc must use a latest model_alias, not a pinned model`);
    }
    if (!ccEffort || !REVIEW_REASONING_EFFORTS.has(ccEffort) || !REVIEW_REASONING_EFFORTS.has(codexEffort)) {
      throw new Error(`compute profile ${profileName} requires reasoning_effort high, xhigh or max for both providers`);
    }
  }
  return {
    name: profileName,
    ccModel,
    ccModelAlias,
    ccEffort,
    codexModelAlias,
    codexEffort,
  };
}

/** The resident Relay model catalog (managed path map, then RELAY_MODEL_CATALOG_PATH). */
export function resolveRelayModelCatalogPath(): string | undefined {
  return managedRelayPath('models', process.env['RELAY_MODEL_CATALOG_PATH']);
}

/**
 * The newest exact Claude model of one family in the Relay model catalog, e.g.
 * claude-opus-latest -> claude-opus-5-5. Review needs the exact current
 * flagship; Claude Code's short alias would also admit an older version.
 */
function resolveClaudeCatalogModel(alias: string, field: string): string {
  const family = resolveClaudeLatestAlias(alias, field);
  const path = resolveRelayModelCatalogPath();
  if (!path) throw new Error(`Relay model catalog is not configured for ${field}`);
  let catalog: { models?: Array<{ id?: unknown }> };
  try {
    catalog = JSON.parse(readFileSync(path, 'utf-8')) as typeof catalog;
  } catch (err) {
    throw new Error(`Relay model catalog unreadable at ${path}: ${String(err)}`);
  }
  if (!Array.isArray(catalog.models)) throw new Error(`Relay model catalog invalid schema at ${path}`);
  const candidates = catalog.models.flatMap(model => {
    if (typeof model?.id !== 'string') return [];
    const match = new RegExp(`^claude-${family}-(\\d+(?:-\\d+)*)$`).exec(model.id);
    return match ? [{ id: model.id, version: match[1].split('-').map(Number) }] : [];
  });
  candidates.sort((left, right) => compareNumericVersions(right.version, left.version));
  if (!candidates[0]) throw new Error(`Claude alias ${alias} has no claude-${family}-* model in ${path}`);
  return candidates[0].id;
}

function resolveCc(profile: CheckedProfile): ComputeProfile['cc'] {
  return {
    model: profile.ccModel
      ?? (profile.name === REVIEW_COMPUTE_PROFILE
        ? resolveClaudeCatalogModel(profile.ccModelAlias!, `${profile.name}.cc.model_alias`)
        : resolveClaudeLatestAlias(profile.ccModelAlias!, `${profile.name}.cc.model_alias`)),
    ...(profile.ccModelAlias ? { modelAlias: profile.ccModelAlias } : {}),
    reasoningEffort: profile.ccEffort,
  };
}

function resolveCodex(profile: CheckedProfile): ComputeProfile['codex'] {
  if (profile.name === REVIEW_COMPUTE_PROFILE) {
    const review = resolveCodexReview(
      profile.codexModelAlias,
      profile.codexEffort,
      `${profile.name}.codex.model_alias`,
    );
    return { model: review.model, modelAlias: profile.codexModelAlias, reasoningEffort: review.effort };
  }
  return {
    model: resolveCodexLatestAlias(
      profile.codexModelAlias,
      profile.codexEffort,
      `${profile.name}.codex.model_alias`,
    ),
    modelAlias: profile.codexModelAlias,
    reasoningEffort: profile.codexEffort,
  };
}

/** Resolve a cron compute profile from the central tracked catalog. */
export function resolveComputeProfile(name = DEFAULT_COMPUTE_PROFILE): ComputeProfile {
  const profile = readProfile(name);
  return { cc: resolveCc(profile), codex: resolveCodex(profile) };
}

export type ComputeProvider = 'cc' | 'codex';

export interface ProviderComputeProfile {
  profile: string;
  provider: ComputeProvider;
  model: string;
  model_alias: string | null;
  reasoning_effort: string | null;
  source: string;
}

/**
 * Resolve one provider only. A Claude Code spawn must not depend on the Codex
 * catalog, but both providers' static fields are still validated.
 */
export function resolveProviderComputeProfile(name: string, provider: ComputeProvider): ProviderComputeProfile {
  const profile = readProfile(name);
  const resolved = provider === 'cc' ? resolveCc(profile) : resolveCodex(profile);
  return {
    profile: profile.name,
    provider,
    model: resolved.model,
    model_alias: resolved.modelAlias ?? null,
    reasoning_effort: resolved.reasoningEffort ?? null,
    source: resolveComputeProfilesPath(),
  };
}
