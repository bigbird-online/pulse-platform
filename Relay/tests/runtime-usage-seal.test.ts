import { createHash } from 'node:crypto';
import { chmodSync, mkdtempSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { afterEach, describe, expect, it } from 'vitest';

import {
  RuntimeUsageSealError,
  parsePiUsageBaselineNotification,
  verifyPiUsageBaselineArtifact,
  verifyPiUsageSealArtifact,
} from '../src/runtime-usage-seal.js';
import { buildRuntimeUsageBinding, type RuntimeUsageBinding } from '../src/runtime-usage-binding.js';
import type { CanonicalMeasurementBaselineReference } from '../src/canonical-measurement-baseline.js';

const roots: string[] = [];

it.each([
  ['measurement_fact_conflict',false],['measurement_transport_unavailable',true],
  ['private unsafe output',false],
])('preserves only safe baseline failure codes and valid retry policy: %s', (code,retryable)=>{
  try {
    parsePiUsageBaselineNotification({type:'extension_ui_request',method:'notify',
      message:'PULSE_USAGE_BASELINE/v1 '+JSON.stringify({ok:false,error:{code,retryable:true}})});
    expect.fail('expected baseline rejection');
  } catch(error) {
    expect(error).toBeInstanceOf(RuntimeUsageSealError);
    expect(error).toMatchObject({code:code==='private unsafe output'?'seal_failed':code,retryable});
    expect(String(error)).not.toContain('private unsafe');
  }
});
const binding: RuntimeUsageBinding = {
  schema: 'pulse_runtime_usage_binding/v3', work_item_id: 'wi_20260827_282', agent_id: 'pulse',
  phase: 'work_item', epoch: 1, runtime: 'pi', procurement_source: 'chatgpt_subscription',
  issued_at: '2026-08-27T00:00:00Z', authority_ref: `session_v1_${'b'.repeat(64)}`,
  execution_surface: 'surface_v1_local_macos', binding_sha256: 'a'.repeat(64),
};

function canonical(value: unknown): string {
  if (Array.isArray(value)) return `[${value.map(canonical).join(',')}]`;
  if (value && typeof value === 'object') {
    const record = value as Record<string, unknown>;
    return `{${Object.keys(record).sort().map(key => `${JSON.stringify(key)}:${canonical(record[key])}`).join(',')}}`;
  }
  return JSON.stringify(value);
}

function usageSummary(eventCount: number, totals: Record<string, number>, position: Record<string, unknown>) {
  return {
    schema: 'pi_native_usage_summary/v1', event_count: eventCount, totals,
    by_provider_model_effort: {}, request_partitions: eventCount ? [{}] : [],
    pricing_contract: 'tokens_only_openrouter_authority_required',
    last_entry_ref: position.last_entry_ref,
    source_prefix_sha256: position.source_prefix_sha256,
    source_bytes: position.source_bytes,
    source_entry_count: eventCount,
    excluded_usage_event_count: 0,
    deduplicated_entry_count: 0,
    privacy: 'raw_session_entry_response_content_and_path_omitted',
  };
}

function baselineSnapshot(extra: Record<string, unknown> = {}) {
  const position = { source_bytes: 0, source_prefix_sha256: createHash('sha256').update('').digest('hex'), last_entry_ref: null };
  const unsigned = {
    schema: 'pi_session_usage_snapshot/v2', generated_at: '2026-08-27T00:00:01Z', lifecycle_state: 'open',
    measurement_role: 'baseline', work_item_id: binding.work_item_id, agent_id: binding.agent_id,
    execution_surface: binding.execution_surface, phase: binding.phase, epoch: binding.epoch, runtime: 'pi',
    procurement_source: binding.procurement_source, authority_ref: binding.authority_ref,
    runtime_binding_sha256: binding.binding_sha256,
    attribution_boundary: { policy: 'baseline_exclusive_final_inclusive', baseline_snapshot_sha256: null,
      baseline: position, final: position, excluded_pre_baseline_usage_events: 0, deduplicated_entries: 0 },
    summary: usageSummary(0, {
      input: 0, cache_read: 0, cache_write: 0, output: 0, reasoning: 0, total_tokens: 0,
    }, position),
    coverage: 'exact_zero_baseline', ...extra,
  };
  return { unsigned, snapshotSha: createHash('sha256').update(canonical(unsigned)).digest('hex') };
}

function baselineArtifact(extra: Record<string, unknown> = {}) {
  const outputDir = mkdtempSync(join(tmpdir(), 'pulse-baseline-privacy-'));
  roots.push(outputDir);
  const { unsigned, snapshotSha } = baselineSnapshot(extra);
  const artifactRef = `${binding.work_item_id}-${binding.binding_sha256}-baseline.json`;
  const path = join(outputDir, artifactRef);
  writeFileSync(path, JSON.stringify({ ...unsigned, snapshot_sha256: snapshotSha }), { mode: 0o600 });
  chmodSync(path, 0o600);
  return {
    outputDir,
    snapshotSha,
    envelope: { ok: true, receipt: {
      schema: 'pi_host_usage_capture_receipt/v1' as const, work_item_id: binding.work_item_id,
      epoch: binding.epoch, lifecycle_state: 'open' as const, binding_sha256: binding.binding_sha256,
      snapshot_sha256: snapshotSha, artifact_ref: artifactRef,
    } },
  };
}

function artifact(extra: Record<string, unknown>) {
  const outputDir = mkdtempSync(join(tmpdir(), 'pulse-seal-privacy-'));
  roots.push(outputDir);
  const position = { source_bytes: 1, source_prefix_sha256: 'c'.repeat(64), last_entry_ref: `pi_entry_v1_${'d'.repeat(64)}` };
  const baseline = baselineSnapshot();
  const baselineRef = `${binding.work_item_id}-${binding.binding_sha256}-baseline.json`;
  writeFileSync(
    join(outputDir, baselineRef),
    JSON.stringify({ ...baseline.unsigned, snapshot_sha256: baseline.snapshotSha }),
    { mode: 0o600 },
  );
  const unsigned = {
    schema: 'pi_session_usage_snapshot/v2', generated_at: '2026-08-27T00:01:00Z', lifecycle_state: 'sealed',
    measurement_role: 'final', work_item_id: binding.work_item_id, agent_id: binding.agent_id,
    execution_surface: binding.execution_surface, phase: binding.phase, epoch: binding.epoch, runtime: 'pi',
    procurement_source: binding.procurement_source, authority_ref: binding.authority_ref,
    runtime_binding_sha256: binding.binding_sha256,
    attribution_boundary: { policy: 'baseline_exclusive_final_inclusive', baseline_snapshot_sha256: baseline.snapshotSha,
      baseline: baseline.unsigned.attribution_boundary.baseline, final: position,
      excluded_pre_baseline_usage_events: 0, deduplicated_entries: 0 },
    summary: usageSummary(1, {
      input: 1, cache_read: 0, cache_write: 0, output: 0, reasoning: 0, total_tokens: 1,
    }, position),
    coverage: 'exact_bounded_sealed', ...extra,
  };
  const snapshotSha = createHash('sha256').update(canonical(unsigned)).digest('hex');
  const artifactRef = `${binding.work_item_id}-${binding.binding_sha256}-sealed.json`;
  const path = join(outputDir, artifactRef);
  writeFileSync(path, JSON.stringify({ ...unsigned, snapshot_sha256: snapshotSha }), { mode: 0o600 });
  chmodSync(path, 0o600);
  return {
    outputDir,
    envelope: { ok: true, receipt: {
      schema: 'pi_host_usage_capture_receipt/v1' as const, work_item_id: binding.work_item_id,
      epoch: binding.epoch, lifecycle_state: 'sealed' as const, binding_sha256: binding.binding_sha256,
      snapshot_sha256: snapshotSha, artifact_ref: artifactRef,
    } },
  };
}

afterEach(() => { while (roots.length) rmSync(roots.pop()!, { recursive: true, force: true }); });

describe('Pi usage seal independent privacy readback', () => {
  it('accepts one private digest-bound prospective baseline artifact', async () => {
    const input = baselineArtifact();
    await expect(verifyPiUsageBaselineArtifact({ ...input, binding })).resolves.toMatchObject({
      schema: 'pulse_runtime_usage_baseline_readback/v1',
      status: 'open',
      baseline_digest: input.snapshotSha,
    });
  });

  it('applies the same privacy gate to a prospective baseline', async () => {
    const input = baselineArtifact({ summary: { event_count: 0, cwd: '/private/worktree' } });
    await expect(verifyPiUsageBaselineArtifact({ ...input, binding })).rejects.toMatchObject<Partial<RuntimeUsageSealError>>({
      code: 'artifact_privacy_invalid',
    });
  });

  it('accepts a final artifact only with its exact private baseline pair', async () => {
    const input = artifact({});
    await expect(verifyPiUsageSealArtifact({ ...input, binding })).resolves.toMatchObject({
      schema: 'pulse_runtime_usage_seal_readback/v1',
      status: 'sealed',
    });
    rmSync(join(
      input.outputDir,
      `${binding.work_item_id}-${binding.binding_sha256}-baseline.json`,
    ));
    await expect(verifyPiUsageSealArtifact({ ...input, binding })).rejects.toMatchObject<Partial<RuntimeUsageSealError>>({
      code: 'artifact_missing',
    });
  });

  it.each([
    ['forbidden source field', { summary: { event_count: 1, session_file: 'redacted.jsonl' } }],
    ['camel-case raw identity', { summary: { event_count: 1, sessionId: 'raw-private-id' } }],
    ['absolute internal path', { summary: { event_count: 1, source: '/Users/private/pi-session.jsonl' } }],
  ])('rejects a canonically digested artifact containing %s', async (_label, extra) => {
    const input = artifact(extra);
    await expect(verifyPiUsageSealArtifact({ ...input, binding })).rejects.toMatchObject<Partial<RuntimeUsageSealError>>({
      code: 'artifact_privacy_invalid',
    });
  });
});

describe('canonical measurement origin independent artifact readback', () => {
  const current = buildRuntimeUsageBinding({
    workItemId: binding.work_item_id, agentId: binding.agent_id, phase: binding.phase,
    epoch: binding.epoch, runtime: 'pi', procurementSource: binding.procurement_source,
    authorityRef: binding.authority_ref, executionSurface: binding.execution_surface,
    issuedAt: '2026-09-22T12:00:00Z',
  });
  function reference(baselineSha = baselineSnapshot().snapshotSha): CanonicalMeasurementBaselineReference {
    return {
      schema: 'pulse_canonical_measurement_baseline_reference/v1', status: 'open',
      work_item_id: current.work_item_id, agent_id: current.agent_id, epoch: current.epoch,
      phase: current.phase, runtime: 'pi', procurement_source: current.procurement_source,
      authority_ref: current.authority_ref!, execution_surface: current.execution_surface!,
      measurement_epoch_ref: `measurement_epoch_v1_${'e'.repeat(64)}`,
      baseline_binding_sha256: binding.binding_sha256, baseline_sha256: baselineSha,
      active_runtime_binding_sha256: current.binding_sha256,
    };
  }
  function receipt<T extends ReturnType<typeof baselineArtifact> | ReturnType<typeof artifact>>(input: T) {
    return { ...input, binding: current, baselineReference: reference(), envelope: {
      ...input.envelope, receipt: { ...input.envelope.receipt,
        active_runtime_binding_sha256: current.binding_sha256 },
    } };
  }

  it('reads the original private baseline while reporting the distinct active binding', async () => {
    const input = receipt(baselineArtifact());
    await expect(verifyPiUsageBaselineArtifact(input)).resolves.toMatchObject({
      baseline_digest: input.snapshotSha,
      measurement_origin_binding_sha256: binding.binding_sha256,
      active_runtime_binding_sha256: current.binding_sha256,
    });
    expect(current.binding_sha256).not.toBe(binding.binding_sha256);
    expect(current.issued_at).toBe('2026-09-22T12:00:00Z');
  });

  it.each(['binding_sha256', 'active_runtime_binding_sha256'] as const)(
    'rejects a receipt with the wrong %s even when its private artifact is valid', async (field) => {
      const input = receipt(baselineArtifact());
      input.envelope.receipt[field] = 'f'.repeat(64);
      await expect(verifyPiUsageBaselineArtifact(input)).rejects.toMatchObject({ code: 'authority_mismatch' });
    },
  );

  it('rejects a different baseline digest instead of replacing the canonical origin', async () => {
    const input = receipt(baselineArtifact());
    input.baselineReference = reference('f'.repeat(64));
    await expect(verifyPiUsageBaselineArtifact(input)).rejects.toMatchObject({ code: 'authority_mismatch' });
  });

  it('rejects a newly generated active-binding artifact masquerading as the original baseline', async () => {
    const input = receipt(baselineArtifact({ runtime_binding_sha256: current.binding_sha256 }));
    input.baselineReference = reference(input.snapshotSha);
    await expect(verifyPiUsageBaselineArtifact(input)).rejects.toMatchObject({ code: 'authority_mismatch' });
  });

  it('verifies final usage against the original private baseline pair', async () => {
    const input = receipt(artifact({}));
    await expect(verifyPiUsageSealArtifact(input)).resolves.toMatchObject({
      status: 'sealed', artifact_ref: `${binding.work_item_id}-${binding.binding_sha256}-sealed.json`,
    });
    input.baselineReference = reference('f'.repeat(64));
    await expect(verifyPiUsageSealArtifact(input)).rejects.toMatchObject({ code: 'authority_mismatch' });
  });

  it('keeps a verified sealed retirement baseline explicitly sealed rather than reporting reopened work', async () => {
    const input = receipt(baselineArtifact());
    const envelope = parsePiUsageBaselineNotification({ type: 'extension_ui_request', method: 'notify',
      message: 'PULSE_USAGE_BASELINE/v1 ' + JSON.stringify({ ok: true, receipt: {
        ...input.envelope.receipt, idempotent_replay: true, canonical_lifecycle_state: 'sealed',
      } }),
    })!;
    await expect(verifyPiUsageBaselineArtifact({ ...input, envelope })).resolves.toMatchObject({
      status: 'idempotent_replay', sealed: true,
      measurement_origin_binding_sha256: binding.binding_sha256,
      active_runtime_binding_sha256: current.binding_sha256,
    });
  });
});
