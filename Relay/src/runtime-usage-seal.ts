import { createHash } from 'node:crypto';
import { lstat, readFile } from 'node:fs/promises';
import { isAbsolute, join } from 'node:path';

import type { RuntimeUsageBinding } from './runtime-usage-binding.js';
import { validateMeasurementBaselineReference, type CanonicalMeasurementBaselineReference } from './canonical-measurement-baseline.js';

const BASELINE_PREFIX = 'PULSE_USAGE_BASELINE/v1 ';
const SEAL_PREFIX = 'PULSE_USAGE_SEAL/v1 ';
const SHA_RE = /^[0-9a-f]{64}$/;

export type RuntimeUsageSealErrorCode =
  | 'command_transport_failed'
  | 'receipt_missing'
  | 'receipt_ambiguous'
  | 'receipt_invalid'
  | 'artifact_missing'
  | 'artifact_mode_invalid'
  | 'artifact_digest_invalid'
  | 'artifact_privacy_invalid'
  | 'authority_mismatch'
  | 'not_settled'
  | 'single_winner_timeout'
  | 'seal_conflict'
  | 'measurement_fact_conflict'
  | 'measurement_epoch_conflict'
  | 'measurement_authority_conflict'
  | 'measurement_authority_unavailable'
  | 'measurement_epoch_required'
  | 'source_authority_required'
  | 'local_persistence_unavailable'
  | 'schema_incompatible'
  | 'invalid_measurement'
  | 'measurement_transport_unavailable'
  | 'seal_failed';

export class RuntimeUsageSealError extends Error {
  constructor(readonly code: RuntimeUsageSealErrorCode, message: string, readonly retryable = false) {
    super(message);
    this.name = 'RuntimeUsageSealError';
  }
}

interface PiUsageCaptureReceipt {
  schema: 'pi_host_usage_capture_receipt/v1';
  work_item_id: string;
  epoch: number;
  lifecycle_state: 'open' | 'sealed';
  binding_sha256: string;
  active_runtime_binding_sha256?:string;
  snapshot_sha256: string;
  artifact_ref: string;
  idempotent_replay?: true;
  canonical_lifecycle_state?: 'sealed';
}

export interface PiUsageCaptureEnvelope {
  ok: boolean;
  receipt?: PiUsageCaptureReceipt;
  error?: { code: RuntimeUsageSealErrorCode };
}

export type PiUsageSealEnvelope = PiUsageCaptureEnvelope;

export interface RuntimeUsageBaselineReadback {
  schema: 'pulse_runtime_usage_baseline_readback/v1';
  status: 'open' | 'idempotent_replay';
  work_item_id: string;
  epoch: number;
  artifact_ref: string;
  baseline_digest: string;
  sealed?: true;
  measurement_origin_binding_sha256?:string;
  active_runtime_binding_sha256?:string;
}

export interface RuntimeUsageSealReadback {
  schema: 'pulse_runtime_usage_seal_readback/v1';
  status: 'sealed' | 'idempotent_replay' | 'legacy_unmeasured';
  work_item_id: string;
  epoch: number;
  artifact_ref: string;
  sealed_digest: string;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return !!value && typeof value === 'object' && !Array.isArray(value);
}

function canonical(value: unknown): string {
  if (Array.isArray(value)) return `[${value.map(canonical).join(',')}]`;
  if (isRecord(value)) {
    return `{${Object.keys(value).sort().map(key => `${JSON.stringify(key)}:${canonical(value[key])}`).join(',')}}`;
  }
  return JSON.stringify(value);
}

const FORBIDDEN_ARTIFACT_KEYS = new Set([
  'session_id', 'sessionid', 'session_file', 'sessionfile',
  'thread_id', 'threadid', 'turn_id', 'turnid',
  'message_id', 'messageid', 'response_id', 'responseid',
  'prompt', 'response', 'credential', 'api_key', 'cwd', 'path', 'content',
]);
const SNAPSHOT_FIELDS = [
  'schema', 'generated_at', 'lifecycle_state', 'measurement_role',
  'work_item_id', 'agent_id', 'execution_surface', 'phase', 'epoch',
  'runtime', 'procurement_source', 'authority_ref', 'runtime_binding_sha256',
  'attribution_boundary', 'summary', 'coverage', 'snapshot_sha256',
];
const SUMMARY_FIELDS = [
  'schema', 'event_count', 'totals', 'by_provider_model_effort',
  'request_partitions', 'pricing_contract', 'last_entry_ref',
  'source_prefix_sha256', 'source_bytes', 'source_entry_count',
  'excluded_usage_event_count', 'deduplicated_entry_count', 'privacy',
];
const TOKEN_FIELDS = ['input', 'output', 'cache_read', 'cache_write', 'reasoning', 'total_tokens'];
const BOUNDARY_FIELDS = [
  'policy', 'baseline_snapshot_sha256', 'baseline', 'final',
  'excluded_pre_baseline_usage_events', 'deduplicated_entries',
];
const POSITION_FIELDS = ['source_bytes', 'source_prefix_sha256', 'last_entry_ref'];

function exactFields(value: Record<string, unknown>, fields: string[]): boolean {
  return Object.keys(value).sort().join('\0') === [...fields].sort().join('\0');
}

function isCount(value: unknown): value is number {
  return typeof value === 'number' && Number.isSafeInteger(value) && value >= 0;
}

function isEntryRef(value: unknown): boolean {
  return value === null || (typeof value === 'string' && /^pi_entry_v1_[0-9a-f]{64}$/.test(value));
}

function assertArtifactPrivacy(value: unknown): void {
  if (typeof value === 'string') {
    if (isAbsolute(value)) {
      throw new RuntimeUsageSealError('artifact_privacy_invalid', 'Pi usage artifact exposed an internal path');
    }
    return;
  }
  if (Array.isArray(value)) {
    value.forEach(assertArtifactPrivacy);
    return;
  }
  if (!isRecord(value)) return;
  for (const [key, nested] of Object.entries(value)) {
    if (FORBIDDEN_ARTIFACT_KEYS.has(key.toLowerCase())) {
      throw new RuntimeUsageSealError('artifact_privacy_invalid', `Pi usage artifact exposed forbidden field: ${key}`);
    }
    assertArtifactPrivacy(nested);
  }
}

async function readPrivatePiArtifact(path: string): Promise<Record<string, unknown>> {
  let metadata;
  let text;
  try {
    metadata = await lstat(path);
    text = await readFile(path, 'utf8');
  } catch {
    throw new RuntimeUsageSealError('artifact_missing', 'Pi usage artifact is unavailable');
  }
  if (!metadata.isFile() || metadata.isSymbolicLink() || (metadata.mode & 0o777) !== 0o600) {
    throw new RuntimeUsageSealError('artifact_mode_invalid', 'Pi usage artifact must be a mode-0600 regular file');
  }
  let snapshot: unknown;
  try { snapshot = JSON.parse(text); } catch {
    throw new RuntimeUsageSealError('artifact_digest_invalid', 'Pi usage artifact is malformed');
  }
  if (!isRecord(snapshot)) {
    throw new RuntimeUsageSealError('artifact_digest_invalid', 'Pi usage artifact is invalid');
  }
  assertArtifactPrivacy(snapshot);
  return snapshot;
}

function validatePiUsageSnapshot(
  snapshot: Record<string, unknown>,
  binding: RuntimeUsageBinding,
  lifecycleState: 'open' | 'sealed',
  measurementRole: 'baseline' | 'final',
  expectedDigest: string,
  measurementBindingDigest=binding.binding_sha256,
): { boundary: Record<string, unknown>; summary: Record<string, unknown> } {
  if (
    !exactFields(snapshot, SNAPSHOT_FIELDS)
    || snapshot.schema !== 'pi_session_usage_snapshot/v2'
    || snapshot.lifecycle_state !== lifecycleState
    || snapshot.measurement_role !== measurementRole
    || snapshot.work_item_id !== binding.work_item_id
    || snapshot.agent_id !== binding.agent_id
    || snapshot.execution_surface !== binding.execution_surface
    || snapshot.phase !== binding.phase
    || snapshot.epoch !== binding.epoch
    || snapshot.runtime !== 'pi'
    || snapshot.procurement_source !== binding.procurement_source
    || snapshot.authority_ref !== binding.authority_ref
    || snapshot.runtime_binding_sha256 !== measurementBindingDigest
    || snapshot.snapshot_sha256 !== expectedDigest
  ) {
    throw new RuntimeUsageSealError('authority_mismatch', 'Pi usage artifact does not match runtime authority');
  }
  const unsigned = Object.fromEntries(Object.entries(snapshot).filter(([key]) => key !== 'snapshot_sha256'));
  const digest = createHash('sha256').update(canonical(unsigned)).digest('hex');
  if (digest !== expectedDigest) {
    throw new RuntimeUsageSealError('artifact_digest_invalid', 'Pi usage artifact digest does not match');
  }
  const summary = isRecord(snapshot.summary) ? snapshot.summary : undefined;
  const totals = summary && isRecord(summary.totals) ? summary.totals : undefined;
  const boundary = isRecord(snapshot.attribution_boundary) ? snapshot.attribution_boundary : undefined;
  const baseline = boundary && isRecord(boundary.baseline) ? boundary.baseline : undefined;
  const final = boundary && isRecord(boundary.final) ? boundary.final : undefined;
  if (
    !summary || !totals || !boundary || !baseline || !final
    || !exactFields(summary, SUMMARY_FIELDS)
    || !exactFields(totals, TOKEN_FIELDS)
    || !exactFields(boundary, BOUNDARY_FIELDS)
    || !exactFields(baseline, POSITION_FIELDS)
    || !exactFields(final, POSITION_FIELDS)
    || boundary.policy !== 'baseline_exclusive_final_inclusive'
    || summary.schema !== 'pi_native_usage_summary/v1'
    || summary.pricing_contract !== 'tokens_only_openrouter_authority_required'
    || summary.privacy !== 'raw_session_entry_response_content_and_path_omitted'
    || !isCount(summary.event_count)
    || !Array.isArray(summary.request_partitions)
    || summary.request_partitions.length !== summary.event_count
    || !isRecord(summary.by_provider_model_effort)
    || TOKEN_FIELDS.some(field => !isCount(totals[field]))
    || totals.total_tokens !== Number(totals.input) + Number(totals.cache_read)
      + Number(totals.cache_write) + Number(totals.output)
    || Number(totals.reasoning) > Number(totals.output)
    || !isCount(summary.source_bytes)
    || !isCount(summary.source_entry_count)
    || !isCount(summary.excluded_usage_event_count)
    || !isCount(summary.deduplicated_entry_count)
    || !isEntryRef(summary.last_entry_ref)
    || !SHA_RE.test(String(summary.source_prefix_sha256 ?? ''))
    || !isCount(baseline.source_bytes)
    || !isCount(final.source_bytes) || final.source_bytes < baseline.source_bytes
    || !isEntryRef(baseline.last_entry_ref) || !isEntryRef(final.last_entry_ref)
    || !SHA_RE.test(String(baseline.source_prefix_sha256 ?? ''))
    || !SHA_RE.test(String(final.source_prefix_sha256 ?? ''))
    || summary.source_bytes !== final.source_bytes
    || summary.source_prefix_sha256 !== final.source_prefix_sha256
    || summary.last_entry_ref !== final.last_entry_ref
    || !isCount(boundary.excluded_pre_baseline_usage_events)
    || !isCount(boundary.deduplicated_entries)
    || summary.excluded_usage_event_count !== boundary.excluded_pre_baseline_usage_events
    || summary.deduplicated_entry_count !== boundary.deduplicated_entries
  ) {
    throw new RuntimeUsageSealError('artifact_digest_invalid', 'Pi usage artifact boundary is invalid');
  }
  if (measurementRole === 'baseline' && (
    summary.event_count !== 0
    || Object.values(totals).some(value => value !== 0)
    || boundary.baseline_snapshot_sha256 !== null
    || canonical(baseline) !== canonical(final)
    || snapshot.coverage !== 'exact_zero_baseline'
  )) {
    throw new RuntimeUsageSealError('artifact_digest_invalid', 'Pi usage baseline is not an exact zero boundary');
  }
  if (measurementRole === 'final' && (
    !SHA_RE.test(String(boundary.baseline_snapshot_sha256 ?? ''))
    || snapshot.coverage !== 'exact_bounded_sealed'
  )) {
    throw new RuntimeUsageSealError('artifact_digest_invalid', 'Pi usage final artifact is not baseline bounded');
  }
  return { boundary, summary };
}

function parsePiUsageNotification(
  record: Record<string, unknown>,
  prefix: string,
  lifecycleState: 'open' | 'sealed',
): PiUsageCaptureEnvelope | undefined {
  if (record.type !== 'extension_ui_request' || record.method !== 'notify' || typeof record.message !== 'string') return undefined;
  if (!record.message.startsWith(prefix)) return undefined;
  let value: unknown;
  try { value = JSON.parse(record.message.slice(prefix.length)); } catch {
    throw new RuntimeUsageSealError('receipt_invalid', 'Pi usage readback is malformed');
  }
  if (!isRecord(value) || typeof value.ok !== 'boolean') {
    throw new RuntimeUsageSealError('receipt_invalid', 'Pi usage readback envelope is invalid');
  }
  if (!value.ok) {
    const error = isRecord(value.error) ? value.error : {};
    const allowed = new Set(['command_transport_failed','receipt_missing','receipt_ambiguous','receipt_invalid',
      'artifact_missing','artifact_mode_invalid','artifact_digest_invalid','artifact_privacy_invalid','authority_mismatch',
      'not_settled','single_winner_timeout','seal_conflict','seal_failed','measurement_fact_conflict',
      'measurement_epoch_conflict','measurement_authority_conflict','measurement_authority_unavailable',
      'measurement_epoch_required','source_authority_required','local_persistence_unavailable','schema_incompatible',
      'invalid_measurement','measurement_transport_unavailable']);
    const code = typeof error.code === 'string' && allowed.has(error.code) ? error.code as RuntimeUsageSealErrorCode : 'seal_failed';
    const retryable = ['measurement_transport_unavailable','local_persistence_unavailable'].includes(code)
      && error.retryable === true;
    throw new RuntimeUsageSealError(code, `Pi usage capture failed [${code}]`, retryable);
  }
  if (!isRecord(value.receipt)) throw new RuntimeUsageSealError('receipt_invalid', 'Pi usage receipt is missing');
  const receipt = value.receipt;
  if (
    receipt.schema !== 'pi_host_usage_capture_receipt/v1'
    || receipt.lifecycle_state !== lifecycleState
    || typeof receipt.work_item_id !== 'string'
    || !Number.isSafeInteger(receipt.epoch)
    || typeof receipt.binding_sha256 !== 'string'
    || !SHA_RE.test(String(receipt.snapshot_sha256 ?? ''))
    || typeof receipt.artifact_ref !== 'string'
    || !/^[A-Za-z0-9._-]+\.json$/.test(receipt.artifact_ref)
    || (receipt.canonical_lifecycle_state !== undefined && (
      receipt.canonical_lifecycle_state !== 'sealed' || lifecycleState !== 'open' || receipt.idempotent_replay !== true
    ))
    || (receipt.idempotent_replay !== undefined && receipt.idempotent_replay !== true)
  ) {
    throw new RuntimeUsageSealError('receipt_invalid', 'Pi usage receipt fields are invalid');
  }
  return { ok: true, receipt: receipt as unknown as PiUsageCaptureReceipt };
}

export function parsePiUsageBaselineNotification(record: Record<string, unknown>): PiUsageCaptureEnvelope | undefined {
  return parsePiUsageNotification(record, BASELINE_PREFIX, 'open');
}

export function parsePiUsageSealNotification(record: Record<string, unknown>): PiUsageSealEnvelope | undefined {
  return parsePiUsageNotification(record, SEAL_PREFIX, 'sealed');
}

async function verifyPiUsageArtifact(input: {
  outputDir: string;
  binding: RuntimeUsageBinding;
  baselineReference?:CanonicalMeasurementBaselineReference;
  envelope: PiUsageCaptureEnvelope;
  lifecycleState: 'open' | 'sealed';
  artifactState: 'baseline' | 'sealed';
}): Promise<PiUsageCaptureReceipt> {
  if (!isAbsolute(input.outputDir)) throw new RuntimeUsageSealError('artifact_missing', 'Pi usage artifact directory is unavailable');
  const receipt = input.envelope.receipt;
  if (!input.envelope.ok || !receipt) throw new RuntimeUsageSealError('receipt_missing', 'Pi usage receipt was not returned');
  const origin=input.baselineReference?validateMeasurementBaselineReference(input.baselineReference,input.binding):undefined;
  const measurementDigest=origin?.baseline_binding_sha256??input.binding.binding_sha256;
  const expectedRef = `${input.binding.work_item_id}-${measurementDigest}-${input.artifactState}.json`;
  if (
    receipt.work_item_id !== input.binding.work_item_id
    || receipt.epoch !== input.binding.epoch
    || receipt.lifecycle_state !== input.lifecycleState
    || receipt.binding_sha256 !== measurementDigest
    || (origin&&receipt.active_runtime_binding_sha256!==input.binding.binding_sha256)
    || receipt.artifact_ref !== expectedRef
  ) {
    throw new RuntimeUsageSealError('authority_mismatch', 'Pi usage receipt does not match runtime authority');
  }
  const path = join(input.outputDir, expectedRef);
  const snapshot = await readPrivatePiArtifact(path);
  const expectedRole = input.artifactState === 'baseline' ? 'baseline' : 'final';
  const validated = validatePiUsageSnapshot(
    snapshot,
    input.binding,
    input.lifecycleState,
    expectedRole,
    receipt.snapshot_sha256,
    measurementDigest,
  );
  if(origin&&input.artifactState==='baseline'&&snapshot.snapshot_sha256!==origin.baseline_sha256)
    throw new RuntimeUsageSealError('authority_mismatch','Canonical measurement origin changed');
  if (input.artifactState === 'sealed') {
    const baselineRef = `${input.binding.work_item_id}-${measurementDigest}-baseline.json`;
    const baseline = await readPrivatePiArtifact(join(input.outputDir, baselineRef));
    validatePiUsageSnapshot(
      baseline,
      input.binding,
      'open',
      'baseline',
      String(validated.boundary.baseline_snapshot_sha256 ?? ''),
      measurementDigest,
    );
    if(origin&&baseline.snapshot_sha256!==origin.baseline_sha256)
      throw new RuntimeUsageSealError('authority_mismatch','Final usage does not retain its canonical origin');
    const baselineBoundary = baseline.attribution_boundary;
    if (
      !isRecord(baselineBoundary)
      || canonical(validated.boundary.baseline) !== canonical(baselineBoundary.baseline)
    ) {
      throw new RuntimeUsageSealError('seal_conflict', 'Pi usage final does not descend from its baseline');
    }
  }
  return receipt;
}

export async function verifyPiUsageBaselineArtifact(input: {
  outputDir: string;
  binding: RuntimeUsageBinding;
  baselineReference?:CanonicalMeasurementBaselineReference;
  envelope: PiUsageCaptureEnvelope;
}): Promise<RuntimeUsageBaselineReadback> {
  const receipt = await verifyPiUsageArtifact({ ...input, lifecycleState: 'open', artifactState: 'baseline' });
  return {
    schema: 'pulse_runtime_usage_baseline_readback/v1',
    status: receipt.idempotent_replay ? 'idempotent_replay' : 'open',
    work_item_id: receipt.work_item_id,
    epoch: receipt.epoch,
    artifact_ref: receipt.artifact_ref,
    baseline_digest: receipt.snapshot_sha256,
    ...(input.baselineReference?{measurement_origin_binding_sha256:receipt.binding_sha256,
      active_runtime_binding_sha256:input.binding.binding_sha256}:{}),
    ...(receipt.canonical_lifecycle_state === 'sealed' ? { sealed: true as const } : {}),
  };
}

export async function verifyPiUsageSealArtifact(input: {
  outputDir: string;
  binding: RuntimeUsageBinding;
  baselineReference?:CanonicalMeasurementBaselineReference;
  envelope: PiUsageSealEnvelope;
}): Promise<RuntimeUsageSealReadback> {
  const receipt = await verifyPiUsageArtifact({ ...input, lifecycleState: 'sealed', artifactState: 'sealed' });
  return {
    schema: 'pulse_runtime_usage_seal_readback/v1',
    status: receipt.idempotent_replay ? 'idempotent_replay' : 'sealed',
    work_item_id: receipt.work_item_id,
    epoch: receipt.epoch,
    artifact_ref: receipt.artifact_ref,
    sealed_digest: receipt.snapshot_sha256,
  };
}
