#!/usr/bin/env node
import { createHash } from 'node:crypto';
import { readFileSync } from 'node:fs';
import { readFile } from 'node:fs/promises';
import { spawnSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';

const TRUSTED_SOURCE_URLS = new Map([
  ['OpenAI', /^https:\/\/developers\.openai\.com\/api\/docs\/models\/gpt-6-astra\/?$/],
  ['OpenAI Codex', /^https:\/\/api\.github\.com\/repos\/openai\/codex\/releases\/tags\/rust-v0\.153\.4$/],
  ['Anthropic npm', /^https:\/\/registry\.npmjs\.org\/@anthropic-ai%2Fclaude-agent-sdk\/0\.3\.261$/],
  ['Anthropic release', /^https:\/\/api\.github\.com\/repos\/anthropics\/claude-agent-sdk-typescript\/releases\/tags\/v0\.3\.261$/],
]);

const REQUIRED_ALERTS = [
  'source_drift',
  'runtime_too_old',
  'model_missing',
  'conversation_uat_failed',
  'rollback_failed',
];

function stableJson(value) {
  if (Array.isArray(value)) return `[${value.map(stableJson).join(',')}]`;
  if (value && typeof value === 'object') {
    return `{${Object.keys(value).sort().map((key) => `${JSON.stringify(key)}:${stableJson(value[key])}`).join(',')}}`;
  }
  return JSON.stringify(value);
}

export function canonicalizeSource(body, source) {
  if (Array.isArray(source.canonical_json_fields)) {
    const parsed = JSON.parse(body);
    const selected = Object.fromEntries(source.canonical_json_fields.map((field) => [field, parsed[field] ?? null]));
    return stableJson(selected);
  }
  return body
    .replace(/<[^>]+>/g, ' ')
    .replace(/&#x27;|&#39;|&apos;/g, "'")
    .replace(/&quot;/g, '"')
    .replace(/&nbsp;/g, ' ')
    .replace(/&amp;/g, '&')
    .replace(/&lt;/g, '<')
    .replace(/&gt;/g, '>')
    .replace(/\s+/g, ' ')
    .trim();
}

export function sha256(value) {
  return createHash('sha256').update(value).digest('hex');
}

export function compareVersions(left, right) {
  const parse = (value) => {
    const match = String(value).match(/\d+(?:\.\d+)+/);
    if (!match) throw new Error(`version is unreadable: ${value}`);
    return match[0].split('.').map(Number);
  };
  const a = parse(left);
  const b = parse(right);
  for (let i = 0; i < Math.max(a.length, b.length); i += 1) {
    const delta = (a[i] ?? 0) - (b[i] ?? 0);
    if (delta !== 0) return Math.sign(delta);
  }
  const leftPrerelease = /\d+(?:\.\d+)+-/.test(String(left));
  const rightPrerelease = /\d+(?:\.\d+)+-/.test(String(right));
  if (leftPrerelease !== rightPrerelease) return leftPrerelease ? -1 : 1;
  return 0;
}

export function validateManifest(manifest) {
  if (manifest?.schema_version !== 'pulse_model_release/v1') throw new Error('unsupported model release schema');
  if (manifest?.model?.id !== 'gpt-6-astra' || manifest?.model?.provider !== 'openai-codex') {
    throw new Error('release must use the official openai-codex/gpt-6-astra route');
  }
  if (manifest?.runtimes?.codex?.minimum_version !== '0.153.4'
    || manifest.runtimes.codex.package !== '@openai/codex') {
    throw new Error('Codex runtime contract is incomplete');
  }
  if (manifest?.runtimes?.pi?.minimum_version !== '0.84.2'
    || manifest.runtimes.pi.required_model !== 'openai-codex/gpt-6-astra') {
    throw new Error('Pi runtime contract is incomplete');
  }
  if (manifest?.runtimes?.claude_agent_sdk?.version !== '0.3.261'
    || manifest.runtimes.claude_agent_sdk.applicability !== 'CC routes only; GPT-6 Astra must not be routed through CC.') {
    throw new Error('Claude Agent SDK runtime contract is incomplete');
  }
  if (!Array.isArray(manifest.sources) || manifest.sources.length !== TRUSTED_SOURCE_URLS.size) {
    throw new Error('official source set is incomplete');
  }
  const seenAuthorities = new Set();
  for (const source of manifest.sources) {
    const trustKey = source.authority === 'Anthropic'
      ? (source.url.startsWith('https://registry.npmjs.org/') ? 'Anthropic npm' : 'Anthropic release')
      : source.authority;
    const trustedPattern = TRUSTED_SOURCE_URLS.get(trustKey);
    if (!trustedPattern || !trustedPattern.test(source.url)) throw new Error(`untrusted source URL: ${source.url}`);
    if (seenAuthorities.has(trustKey)) throw new Error(`duplicate official source authority: ${trustKey}`);
    seenAuthorities.add(trustKey);
    if (!/^[a-f0-9]{64}$/.test(source.canonical_sha256 ?? '')) throw new Error(`source digest missing: ${source.url}`);
    if (!Array.isArray(source.required_text) || source.required_text.length === 0
      || source.required_text.some((text) => typeof text !== 'string' || text.length === 0)) {
      throw new Error(`source assertions missing: ${source.url}`);
    }
  }
  if (seenAuthorities.size !== TRUSTED_SOURCE_URLS.size) throw new Error('official source set is incomplete');
  if (manifest.discovery?.claude_agent_sdk?.url !== 'https://registry.npmjs.org/@anthropic-ai%2Fclaude-agent-sdk/latest'
    || manifest.discovery.claude_agent_sdk.package !== '@anthropic-ai/claude-agent-sdk'
    || manifest.discovery.claude_agent_sdk.pinned_version !== manifest.runtimes.claude_agent_sdk.version) {
    throw new Error('candidate discovery contract is incomplete');
  }
  if (manifest.rollout?.human_gate_before_activation !== true) throw new Error('activation human gate must be enabled');
  if (!Array.isArray(manifest.rollout?.order) || manifest.rollout.order.join(',') !== 'ownermac,TCloud,Vultr') {
    throw new Error('rollout order must be ownermac, TCloud, Vultr');
  }
  if (typeof manifest.rollout?.rollback !== 'string' || manifest.rollout.rollback.trim().length === 0) {
    throw new Error('rollback contract is required');
  }
  if (!Array.isArray(manifest.rollout?.alert_on)
    || REQUIRED_ALERTS.some((alert) => !manifest.rollout.alert_on.includes(alert))) {
    throw new Error('alert contract is incomplete');
  }
  const alerting = manifest.rollout?.alerting;
  if (alerting?.delivery !== 'github-actions-failed-check'
    || typeof alerting.owner !== 'string' || alerting.owner.trim().length === 0
    || !/^[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})$/.test(alerting.owner_login ?? '')
    || !Number.isInteger(alerting.ack_sla_minutes) || alerting.ack_sla_minutes !== 30
    || alerting.rollback_failed_severity !== 'critical'
    || typeof alerting.escalation !== 'string' || alerting.escalation.trim().length === 0) {
    throw new Error('alert delivery and ownership contract is incomplete');
  }
  return manifest;
}

export async function verifyOfficialSources(manifest, fetchImpl = fetch) {
  const results = [];
  for (const source of manifest.sources) {
    const response = await fetchImpl(source.url, {
      headers: { accept: 'text/html,application/json', 'user-agent': 'PULSE-model-release-gate/1' },
      redirect: 'follow',
    });
    if (!response.ok) throw new Error(`official source unavailable (${response.status}): ${source.url}`);
    const canonical = canonicalizeSource(await response.text(), source);
    const missing = source.required_text.filter((text) => !canonical.includes(text));
    if (missing.length > 0) throw new Error(`official source assertion failed (${source.url}): ${missing.join(' | ')}`);
    const digest = sha256(canonical);
    results.push({ authority: source.authority, url: source.url, digest, source_drift: digest !== source.canonical_sha256 });
  }
  return results;
}

export async function discoverRuntimeCandidates(manifest, fetchImpl = fetch) {
  const source = manifest.discovery.claude_agent_sdk;
  const response = await fetchImpl(source.url, {
    headers: { accept: 'application/json', 'user-agent': 'PULSE-model-release-gate/1' },
    redirect: 'follow',
  });
  if (!response.ok) throw new Error(`official candidate source unavailable (${response.status}): ${source.url}`);
  const value = await response.json();
  if (value.name !== source.package || typeof value.version !== 'string') {
    throw new Error('official candidate response is invalid');
  }
  return [{
    runtime: 'claude_agent_sdk',
    pinned_version: source.pinned_version,
    latest_version: value.version,
    candidate_available: compareVersions(value.version, source.pinned_version) > 0,
  }];
}

export function verifyLocalRuntime(manifest, options = {}) {
  const run = options.spawnSync ?? spawnSync;
  const codexBin = options.codexBin ?? fileURLToPath(new URL('../node_modules/.bin/codex', import.meta.url));
  const codex = run(codexBin, ['--version'], { encoding: 'utf8' });
  if (codex.status !== 0) throw new Error('Codex runtime readback failed');
  const codexVersion = (codex.stdout || codex.stderr).trim();
  if (compareVersions(codexVersion, manifest.runtimes.codex.minimum_version) < 0) {
    throw new Error(`Codex runtime too old: ${codexVersion}; required >= ${manifest.runtimes.codex.minimum_version}`);
  }

  const pi = run('pi', ['--version'], { encoding: 'utf8' });
  if (pi.status !== 0) throw new Error('Pi runtime readback failed');
  const piVersion = (pi.stdout || pi.stderr).trim();
  if (compareVersions(piVersion, manifest.runtimes.pi.minimum_version) < 0) {
    throw new Error(`Pi runtime too old: ${piVersion}; required >= ${manifest.runtimes.pi.minimum_version}`);
  }
  const models = run('pi', ['--list-models', manifest.model.provider], { encoding: 'utf8' });
  if (models.status !== 0) throw new Error('Pi model catalog readback failed');
  const exactModelPresent = models.stdout.split(/\r?\n/).some((line) => {
    const columns = line.trim().split(/\s+/);
    return columns[0] === manifest.model.provider && columns[1] === manifest.model.id;
  });
  if (!exactModelPresent) throw new Error(`model missing from Pi catalog: ${manifest.runtimes.pi.required_model}`);

  const sdkVersion = options.sdkVersion ?? JSON.parse(readFileSync(
    new URL('../node_modules/@anthropic-ai/claude-agent-sdk/package.json', import.meta.url),
    'utf8',
  )).version;
  if (sdkVersion !== manifest.runtimes.claude_agent_sdk.version) {
    throw new Error(`Claude Agent SDK version mismatch: ${sdkVersion}; required ${manifest.runtimes.claude_agent_sdk.version}`);
  }
  return {
    codex_version: codexVersion,
    pi_version: piVersion,
    claude_agent_sdk_version: sdkVersion,
    model: manifest.runtimes.pi.required_model,
  };
}

async function main() {
  const args = process.argv.slice(2);
  const take = (name, fallback) => {
    const index = args.indexOf(name);
    return index >= 0 ? args[index + 1] : fallback;
  };
  const manifestPath = take('--manifest', new URL('../config/model-release.json', import.meta.url));
  const manifest = validateManifest(JSON.parse(await readFile(manifestPath, 'utf8')));
  const output = { schema_version: 'pulse_model_release_gate/v1', release: manifest.release, manifest: 'valid' };
  if (args.includes('--online')) {
    output.sources = await verifyOfficialSources(manifest);
    output.candidates = await discoverRuntimeCandidates(manifest);
  }
  if (args.includes('--runtime-readback')) output.runtime = verifyLocalRuntime(manifest);
  process.stdout.write(`${JSON.stringify(output, null, 2)}\n`);
  if (output.sources?.some((source) => source.source_drift)) {
    process.stderr.write('official source drift detected; human review is required before activation\n');
    process.exitCode = 3;
  }
}

if (process.argv[1] && fileURLToPath(import.meta.url) === process.argv[1]) {
  main().catch((error) => {
    process.stderr.write(`model-release-gate: ${error instanceof Error ? error.message : String(error)}\n`);
    process.exitCode = 1;
  });
}
