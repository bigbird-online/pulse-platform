import { managedRelayPath } from './managed-paths.js';
import { existsSync, readFileSync } from 'node:fs';
import { join } from 'node:path';
import { resolvePulseRoot } from './config-paths.js';

export const DEFAULT_PULSE_CODEX_MODEL = 'gpt-6-astra';
export const DEFAULT_PULSE_CODEX_REASONING_EFFORT = 'xhigh';
export const DEFAULT_PULSE_RUNTIME_PRIORITY = ['codex'] as const;

/** Prefer the reviewed Codex binary shipped inside the immutable Relay release. */
export function resolvePulseCodexBin(explicit?: string): string {
  const configured = firstNonEmpty(explicit, process.env['CODEX_BIN']);
  if (configured) return configured;
  const bundled = join(resolvePulseRoot(), 'Relay', 'node_modules', '.bin', 'codex');
  return existsSync(bundled) ? bundled : 'codex';
}

interface RuntimePriorityProfile {
  schema_version: 1;
  runtime_priority: Array<'codex' | 'cc'>;
  codex: {
    model: string;
    reasoning_effort: string;
    yolo: boolean;
  };
  claude: { model: string };
}

function readRuntimePriorityProfile(): RuntimePriorityProfile | undefined {
  const path = managedRelayPath('runtimePriority', process.env['PULSE_RUNTIME_PRIORITY_PATH']?.trim())
    || join(resolvePulseRoot(), 'data', 'runtime-priority.json');
  try {
    const parsed = JSON.parse(readFileSync(path, 'utf8')) as unknown;
    if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) return undefined;
    const profile = parsed as Record<string, unknown>;
    const priority = profile['runtime_priority'];
    const codex = profile['codex'];
    const claude = profile['claude'];
    if (
      profile['schema_version'] !== 1
      || !Array.isArray(priority)
      || priority.length === 0
      || priority.some((item) => item !== 'codex' && item !== 'cc')
      || new Set(priority).size !== priority.length
      || !codex || typeof codex !== 'object' || Array.isArray(codex)
      || !claude || typeof claude !== 'object' || Array.isArray(claude)
    ) return undefined;
    const codexFields = codex as Record<string, unknown>;
    const claudeFields = claude as Record<string, unknown>;
    if (
      typeof codexFields['model'] !== 'string' || !codexFields['model'].trim()
      || typeof codexFields['reasoning_effort'] !== 'string'
      || !codexFields['reasoning_effort'].trim()
      || typeof codexFields['yolo'] !== 'boolean'
      || typeof claudeFields['model'] !== 'string' || !claudeFields['model'].trim()
    ) return undefined;
    return parsed as RuntimePriorityProfile;
  } catch {
    return undefined;
  }
}

function firstNonEmpty(...values: Array<string | undefined>): string | undefined {
  for (const value of values) {
    const trimmed = value?.trim();
    if (trimmed) return trimmed;
  }
  return undefined;
}

export function resolvePulseCodexModel(explicit?: string): string {
  const profile = readRuntimePriorityProfile();
  return firstNonEmpty(
    explicit,
    process.env['PULSE_CODEX_MODEL'],
    process.env['RELAY_CODEX_MODEL'],
    profile?.codex.model,
  ) ?? DEFAULT_PULSE_CODEX_MODEL;
}

export function resolvePulseCodexReasoningEffort(explicit?: string): string {
  const profile = readRuntimePriorityProfile();
  return firstNonEmpty(
    explicit,
    process.env['PULSE_CODEX_REASONING_EFFORT'],
    process.env['RELAY_CODEX_REASONING_EFFORT'],
    profile?.codex.reasoning_effort,
  ) ?? DEFAULT_PULSE_CODEX_REASONING_EFFORT;
}

export function resolvePulseRuntimePriority(): Array<'codex' | 'cc'> {
  return readRuntimePriorityProfile()?.runtime_priority.slice()
    ?? [...DEFAULT_PULSE_RUNTIME_PRIORITY];
}

export function buildPulseCodexModelArgs(input: {
  model?: string;
  reasoningEffort?: string;
} = {}): string[] {
  return [
    '-m', resolvePulseCodexModel(input.model),
    '-c', `model_reasoning_effort="${resolvePulseCodexReasoningEffort(input.reasoningEffort)}"`,
  ];
}
