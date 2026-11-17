import { readFileSync } from 'node:fs';
import { join } from 'node:path';
import type { StreamJsonMessage } from './types.js';

/** When no model id or no rule matches. */
const DEFAULT_CONTEXT_LIMIT = 128_000;

interface BuiltinRule {
  test: (normalizedId: string) => boolean;
  limit: number;
}

/**
 * Prefix-based built-ins (verified against CC v2.1.142 binary, 2026-05-16).
 * Operators SHOULD set `RELAY_MODEL_CONTEXT_LIMITS` for exact ids when a model
 * deviates from its family default.
 *
 * claude-opus-4-7: 1M tokens (native, no beta header needed)
 * All other claude-* / short aliases: 200k
 *   (1M requires `context-1m-2025-08-07` beta header, not sent by default)
 */

// CC often surfaces short aliases instead of full model IDs.
// "opus" alias maps to opus-4-7 (1M); "sonnet"/"haiku" map to their 200k models.
const CLAUDE_SHORT_ALIASES_200K = new Set(['sonnet', 'haiku']);

const BUILTIN_CONTEXT_RULES: BuiltinRule[] = [
  // opus-4-7: native 1M context
  { test: (id) => id === 'claude-opus-4-7', limit: 1_000_000 },
  // "opus" short alias → opus-4-7 → 1M
  { test: (id) => id === 'opus', limit: 1_000_000 },
  // All other claude-* and remaining short aliases: 200k
  { test: (id) => id.startsWith('claude-') || CLAUDE_SHORT_ALIASES_200K.has(id), limit: 200_000 },
  { test: (id) => id.startsWith('gpt-'), limit: 128_000 },
];

function parseEnvModelLimits(): Map<string, number> {
  const raw = process.env['RELAY_MODEL_CONTEXT_LIMITS'];
  if (!raw?.trim()) return new Map();
  try {
    const obj = JSON.parse(raw) as unknown;
    if (typeof obj !== 'object' || obj === null || Array.isArray(obj)) return new Map();
    const m = new Map<string, number>();
    for (const [k, v] of Object.entries(obj)) {
      const limit = Number(v);
      if (k.trim() && Number.isFinite(limit) && limit > 0) {
        m.set(k.trim().toLowerCase(), Math.floor(limit));
      }
    }
    return m;
  } catch {
    return new Map();
  }
}

/** CC `result` may surface model on top-level or under `usage` depending on CLI/SDK version. */
export function extractResultModelId(msg: StreamJsonMessage): string | undefined {
  if (msg.type !== 'result') return undefined;
  const m = msg as Record<string, unknown>;
  if (typeof m['model'] === 'string') {
    const s = m['model'].trim();
    if (s) return s;
  }
  const usage = m['usage'];
  if (usage && typeof usage === 'object' && usage !== null) {
    const u = usage as Record<string, unknown>;
    if (typeof u['model'] === 'string') {
      const s = u['model'].trim();
      if (s) return s;
    }
  }
  return undefined;
}

/**
 * Resolve the configured CC model id for soft-warning limit lookup.
 * Precedence: `RELAY_CLAUDE_MODEL` env → `CLAUDE_CODE_MODEL` env →
 * `~/.claude/settings.json` `model` field.
 */
export function resolveConfiguredCcModelEnv(): string | undefined {
  const v = process.env['RELAY_CLAUDE_MODEL'] ?? process.env['CLAUDE_CODE_MODEL'];
  if (typeof v !== 'string') {
    // Fallback: read model from CC settings.json
    try {
      const settingsPath = join(process.env['HOME'] ?? '', '.claude', 'settings.json');
      const settings = JSON.parse(readFileSync(settingsPath, 'utf-8'));
      const model = settings?.['model'];
      if (typeof model === 'string') {
        const s = model.trim();
        if (s) return s;
      }
    } catch { /* not readable or invalid */ }
    return undefined;
  }
  const s = v.trim();
  return s || undefined;
}

/**
 * Fallback denominator when model is unknown or unmatched.
 * `RELAY_CONTEXT_LIMIT_DEFAULT` or 128k.
 */
export function readDefaultContextLimit(): number {
  const raw = Number(process.env['RELAY_CONTEXT_LIMIT_DEFAULT']);
  return Number.isFinite(raw) && raw > 0 ? raw : DEFAULT_CONTEXT_LIMIT;
}

/**
 * Context **input** budget for soft-warning ratio (not output tokens).
 * Precedence: `RELAY_MODEL_CONTEXT_LIMITS` exact key (lowercase) → built-in prefix rules →
 * `readDefaultContextLimit()`.
 */
export function resolveContextWindowLimit(modelId: string | undefined): number {
  const norm = modelId?.trim().toLowerCase();
  if (norm) {
    const fromEnvMap = parseEnvModelLimits().get(norm);
    if (fromEnvMap !== undefined) return fromEnvMap;
    for (const rule of BUILTIN_CONTEXT_RULES) {
      if (rule.test(norm)) return rule.limit;
    }
  }
  return readDefaultContextLimit();
}
