#!/usr/bin/env node
import { createHash } from 'node:crypto';
import { readFile, writeFile } from 'node:fs/promises';
import { fileURLToPath } from 'node:url';
import { compareVersions, validateManifest } from './model-release-gate.mjs';

const ORDER = ['ownermac', 'TCloud'];
const ALERT_CODES = new Set([
  'source_drift', 'runtime_too_old', 'model_missing', 'conversation_uat_failed', 'rollback_failed',
]);

export function incidentKey(candidateSha, code, site = 'global') {
  return createHash('sha256').update(`${candidateSha}:${code}:${site}`).digest('hex').slice(0, 20);
}

function failure(code, site, reason, severity = code === 'rollback_failed' ? 'critical' : 'error') {
  if (!ALERT_CODES.has(code)) throw new Error(`unsupported model-release alert: ${code}`);
  return { code, site, reason, severity };
}

export function assessRelease(manifestInput, bundle, now = new Date()) {
  const manifest = validateManifest(structuredClone(manifestInput));
  if (bundle?.schema !== 'pulse_model_release_monitor_bundle/v1') throw new Error('unsupported monitor bundle');
  const candidateSha = bundle.candidate_sha;
  if (!/^[0-9a-f]{40}$/.test(candidateSha ?? '')) throw new Error('candidate SHA is invalid');
  const failures = [];

  if (bundle.sources?.status !== 'pass' || !Array.isArray(bundle.sources?.results)) {
    failures.push(failure('source_drift', 'global', bundle.sources?.reason ?? 'official discovery did not complete'));
  } else if (bundle.sources.results.some((item) => item.source_drift)) {
    failures.push(failure('source_drift', 'global', 'official pinned source digest changed'));
  }

  const sites = Array.isArray(bundle.sites) ? bundle.sites : [];
  let nextSite = ORDER[0];
  let incomplete = false;
  for (let index = 0; index < ORDER.length; index += 1) {
    const expectedSite = ORDER[index];
    const receipt = sites[index];
    if (!receipt) {
      incomplete = true;
      nextSite = expectedSite;
      break;
    }
    if (receipt.site !== expectedSite) {
      failures.push(failure('model_missing', expectedSite, 'receipt order is invalid'));
      break;
    }
    for (const item of receipt.failures ?? []) {
      failures.push(failure(item.code, expectedSite, item.reason ?? 'site probe failed'));
    }
    if (receipt.schema !== 'pulse_model_release_site_receipt/v1'
      || receipt.status !== 'pass' || receipt.activation_status !== 'activated') {
      if (!(receipt.failures ?? []).length) failures.push(failure('runtime_too_old', expectedSite, 'activation receipt is invalid'));
      break;
    }
    if (receipt.source_sha !== candidateSha) {
      failures.push(failure('model_missing', expectedSite, 'site is not activated on the candidate SHA'));
      break;
    }
    let runtimeValid = false;
    try {
      runtimeValid = compareVersions(receipt.runtime?.codex_version ?? '', manifest.runtimes.codex.minimum_version) >= 0
        && compareVersions(receipt.runtime?.pi_version ?? '', manifest.runtimes.pi.minimum_version) >= 0
        && receipt.runtime?.claude_agent_sdk_version === manifest.runtimes.claude_agent_sdk.version;
    } catch {
      runtimeValid = false;
    }
    if (!runtimeValid) {
      failures.push(failure('runtime_too_old', expectedSite, 'runtime readback does not meet the pinned contract'));
      break;
    }
    if (receipt.catalog_model !== manifest.runtimes.pi.required_model) {
      failures.push(failure('model_missing', expectedSite, 'exact model catalog readback is absent'));
      break;
    }
    if (receipt.uat?.provider !== manifest.model.provider || receipt.uat?.model !== manifest.model.id
      || receipt.uat?.response_matched !== true) {
      failures.push(failure('conversation_uat_failed', expectedSite, 'real no-tool conversation UAT failed'));
      break;
    }
    const activatedAt = Date.parse(receipt.activated_at);
    if (!Number.isFinite(activatedAt)) {
      failures.push(failure('model_missing', expectedSite, 'activation receipt has no valid timestamp'));
      break;
    }
    // Array position is the controller's progression ledger. A later repair of
    // an earlier site may legitimately have a newer timestamp than downstream.
    nextSite = ORDER[index + 1] ?? null;
  }

  for (const rollback of bundle.rollbacks ?? []) {
    if (rollback.status !== 'rolled_back') {
      failures.push(failure('rollback_failed', rollback.site ?? 'global', 'rollback receipt is absent or failed'));
    }
  }

  const deduped = [...new Map(failures.map((item) => [`${item.code}:${item.site}`, item])).values()]
    .map((item) => ({ ...item, incident_key: incidentKey(candidateSha, item.code, item.site) }));
  return {
    schema: 'pulse_model_release_assessment/v1',
    candidate_sha: candidateSha,
    evaluated_at: now.toISOString(),
    rollout_order: ORDER,
    next_site: deduped.length === 0 ? nextSite : null,
    status: deduped.length > 0 ? 'blocked' : (incomplete ? 'ready' : 'pass'),
    blocking: deduped.length > 0,
    failures: deduped,
  };
}

async function githubRequest(path, options = {}) {
  const token = process.env.GITHUB_TOKEN;
  const repository = process.env.GITHUB_REPOSITORY;
  if (!token || !repository) throw new Error('GitHub incident delivery environment is incomplete');
  const response = await fetch(`https://api.github.com/repos/${repository}${path}`, {
    ...options,
    headers: {
      accept: 'application/vnd.github+json',
      authorization: `Bearer ${token}`,
      'x-github-api-version': '2022-11-28',
      'content-type': 'application/json',
      ...(options.headers ?? {}),
    },
  });
  if (!response.ok) throw new Error(`GitHub incident delivery failed (${response.status})`);
  return response.status === 204 ? null : response.json();
}

export async function reconcileIncidents(assessment, alerting, now = new Date(), api = githubRequest) {
  const issues = await api('/issues?state=all&per_page=100');
  const owner = alerting.owner_login;
  const currentIssues = [];
  for (const item of assessment.failures) {
    const marker = `<!-- pulse-model-release-incident:${item.incident_key} -->`;
    let issue = issues.find((candidate) => !candidate.pull_request && candidate.body?.includes(marker));
    if (!issue) {
      issue = await api('/issues', { method: 'POST', body: JSON.stringify({
        title: `[model-release] ${item.code} at ${item.site}`,
        body: `${marker}\nOwner: @${owner}\nSeverity: ${item.severity}\nCandidate: \`${assessment.candidate_sha}\`\nFailure: ${item.reason}\n\nAcknowledge with \`/ack model-release\` within ${alerting.ack_sla_minutes} minutes.`,
        assignees: [owner],
      }) });
      issues.push(issue);
    } else if (issue.state !== 'open') {
      issue = await api(`/issues/${issue.number}`, { method: 'PATCH', body: JSON.stringify({ state: 'open' }) });
    }
    currentIssues.push(issue);
  }

  const tracked = [...new Map(issues
    .filter((issue) => !issue.pull_request && issue.state === 'open'
      && issue.body?.includes('<!-- pulse-model-release-incident:'))
    .concat(currentIssues)
    .map((issue) => [issue.number, issue])).values()];
  const receipts = [];
  for (const issue of tracked) {
    const key = issue.body.match(/pulse-model-release-incident:([a-f0-9]{20})/)?.[1];
    if (!key) continue;
    const comments = await api(`/issues/${issue.number}/comments?per_page=100`);
    const acknowledged = comments.some((comment) => comment.user?.login === owner
      && comment.body?.trim() === '/ack model-release');
    const ageMinutes = (now.getTime() - Date.parse(issue.created_at)) / 60000;
    const escalationMarker = `<!-- pulse-model-release-escalated:${key} -->`;
    const escalated = !acknowledged && ageMinutes >= alerting.ack_sla_minutes;
    if (escalated && !comments.some((comment) => comment.body?.includes(escalationMarker))) {
      await api(`/issues/${issue.number}/comments`, { method: 'POST', body: JSON.stringify({
        body: `${escalationMarker}\nACK SLA breached. @${owner} must acknowledge; rollout remains blocked.`,
      }) });
    }
    receipts.push({ incident_key: key, issue_number: issue.number, acknowledged, escalated });
  }
  return { schema: 'pulse_model_release_incident_receipts/v1', open_blockers: tracked.length, receipts };
}

async function main() {
  const args = process.argv.slice(2);
  const take = (name) => { const index = args.indexOf(name); return index >= 0 ? args[index + 1] : undefined; };
  const bundlePath = take('--bundle');
  const outputPath = take('--output');
  if (!bundlePath) throw new Error('--bundle is required');
  const manifestPath = take('--manifest') ?? new URL('../config/model-release.json', import.meta.url);
  const [manifest, bundle] = await Promise.all([
    readFile(manifestPath, 'utf8').then(JSON.parse), readFile(bundlePath, 'utf8').then(JSON.parse),
  ]);
  const assessment = assessRelease(manifest, bundle);
  let incidents = null;
  if (!args.includes('--no-incident-api')) {
    incidents = await reconcileIncidents(assessment, manifest.rollout.alerting);
  }
  const result = { assessment, incidents };
  if (outputPath) await writeFile(outputPath, `${JSON.stringify(result, null, 2)}\n`, { mode: 0o600 });
  process.stdout.write(`${JSON.stringify(result, null, 2)}\n`);
  if (assessment.status !== 'pass' || (incidents?.open_blockers ?? 0) > 0) process.exitCode = 4;
}

if (process.argv[1] && fileURLToPath(import.meta.url) === process.argv[1]) {
  main().catch((error) => {
    process.stderr.write(`model-release-control: ${error instanceof Error ? error.message : String(error)}\n`);
    process.exitCode = 1;
  });
}
