import { execFile as nodeExecFile } from 'node:child_process';
import { createHash, randomUUID } from 'node:crypto';
import { chmod, lstat, mkdir, open, readFile, rename, rm, stat } from 'node:fs/promises';
import { basename, dirname, isAbsolute, join } from 'node:path';
import { promisify } from 'node:util';

const execFile = promisify(nodeExecFile);
const BINDING_SCHEMA = 'pulse_runtime_usage_binding/v3';
const SNAPSHOT_SCHEMA = 'pi_session_usage_snapshot/v2';
const SHA_RE = /^[0-9a-f]{64}$/;
const WI_RE = /^wi_\d{8}_\d{3}$/;
const TOKEN_RE = /^[a-z][a-z0-9_.-]{0,127}$/;
const UTC_TIMESTAMP_RE = /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})(?:\.\d{1,6})?Z$/;
const BINDING_FIELDS = [
  'schema', 'work_item_id', 'agent_id', 'phase', 'epoch', 'runtime',
  'procurement_source', 'issued_at', 'authority_ref', 'execution_surface',
  'binding_sha256',
];
const PROCUREMENT = new Set([
  'chatgpt_subscription', 'openrouter_api', 'claude_subscription',
  'cursor_subscription', 'unknown',
]);
const PI_MEASUREMENT_RUNTIME_CLASSES = new Set(['pi', 'resident_harness']);
const MEASUREMENT_FAILURE_CODES = new Set([
  'measurement_fact_conflict', 'measurement_epoch_conflict',
  'measurement_authority_conflict', 'measurement_authority_unavailable',
  'measurement_epoch_required', 'source_authority_required',
  'local_persistence_unavailable', 'schema_incompatible', 'invalid_measurement',
]);

export class PiUsageCaptureError extends Error {
  constructor(code, retryable = false) {
    super(`Pi canonical measurement rejected [${code}]`);
    this.code = code;
    this.retryable = retryable;
  }
}

function isCanonicalUtcTimestamp(value) {
  if (typeof value !== 'string') return false;
  const match = UTC_TIMESTAMP_RE.exec(value);
  if (!match) return false;
  const parsed = new Date(value);
  if (!Number.isFinite(parsed.getTime())) return false;
  const [, year, month, day, hour, minute, second] = match;
  return parsed.getUTCFullYear() === Number(year)
    && parsed.getUTCMonth() + 1 === Number(month)
    && parsed.getUTCDate() === Number(day)
    && parsed.getUTCHours() === Number(hour)
    && parsed.getUTCMinutes() === Number(minute)
    && parsed.getUTCSeconds() === Number(second);
}

function hostConfig(env) {
  const raw = env.PULSE_RUNTIME_USAGE_BINDING;
  if (!raw || env.PULSE_RUNTIME_USAGE_BINDING_FILE) {
    throw new Error('Pi host requires exactly one inline runtime usage binding');
  }
  let binding;
  try {
    binding = JSON.parse(raw);
  } catch {
    throw new Error('Pi host runtime usage binding is malformed');
  }
  if (
    !binding || typeof binding !== 'object' || Array.isArray(binding)
    || Object.keys(binding).sort().join('\0') !== [...BINDING_FIELDS].sort().join('\0')
    || binding.schema !== BINDING_SCHEMA || binding.runtime !== 'pi'
    || !WI_RE.test(String(binding.work_item_id ?? ''))
    || !TOKEN_RE.test(String(binding.agent_id ?? ''))
    || !TOKEN_RE.test(String(binding.phase ?? ''))
    || !Number.isSafeInteger(binding.epoch) || binding.epoch < 1
    || !PROCUREMENT.has(binding.procurement_source)
    || !isCanonicalUtcTimestamp(binding.issued_at)
    || !/^session_v1_[0-9a-f]{64}$/.test(String(binding.authority_ref ?? ''))
    || !/^surface_v1_[a-z0-9_]{1,96}$/.test(String(binding.execution_surface ?? ''))
    || env.PULSE_RUNTIME_AUTHORITY_REF !== binding.authority_ref
    || env.PULSE_RUNTIME_EXECUTION_SURFACE !== binding.execution_surface
    || !SHA_RE.test(String(binding.binding_sha256 ?? ''))
  ) {
    throw new Error('Pi host runtime usage binding is invalid');
  }
  const unsigned = Object.fromEntries(
    Object.entries(binding).filter(([key]) => key !== 'binding_sha256'),
  );
  const expectedBindingDigest = createHash('sha256').update(canonical(unsigned)).digest('hex');
  if (expectedBindingDigest !== binding.binding_sha256) {
    throw new Error('Pi host runtime usage binding digest does not match');
  }
  const measurementBaselineReference=readMeasurementBaselineReference(env.PULSE_CANONICAL_MEASUREMENT_BASELINE_REFERENCE,binding);
  const python = env.PULSE_COMPUTE_COST_PYTHON;
  const pythonPath = env.PULSE_COMPUTE_COST_PYTHONPATH;
  const outputDir = env.PULSE_COMPUTE_COST_PI_SNAPSHOT_DIR;
  const sclBin = env.PULSE_COMPUTE_COST_SCL_BIN;
  const sclHarnessRoot = env.PULSE_COMPUTE_COST_SCL_HARNESS_ROOT;
  const sclKernelRoot = env.PULSE_COMPUTE_COST_SCL_KERNEL_ROOT;
  const sclSessionRef = env.SCL_SESSION_REF;
  if (!python || !isAbsolute(python) || !pythonPath || !isAbsolute(pythonPath)) {
    throw new Error('Pi host ComputeCost executable binding is unavailable');
  }
  if (!outputDir || !isAbsolute(outputDir)) {
    throw new Error('Pi host snapshot directory binding is unavailable');
  }
  if (
    !sclBin || !isAbsolute(sclBin)
    || !sclHarnessRoot || !isAbsolute(sclHarnessRoot)
    || !sclKernelRoot || !isAbsolute(sclKernelRoot)
    || !/^session_v1_[0-9a-f]{64}$/.test(String(sclSessionRef ?? ''))
  ) {
    throw new Error('Pi host canonical SCL measurement binding is unavailable');
  }
  return {
    binding, measurementBaselineReference, python, pythonPath, outputDir,
    sclBin, sclHarnessRoot, sclKernelRoot, sclSessionRef,
  };
}

function readMeasurementBaselineReference(raw,binding){
  if(raw===undefined)return;
  let reference;try{reference=JSON.parse(raw);}catch{throw new Error('Canonical measurement origin is malformed');}
  const fields=['schema','status','work_item_id','agent_id','epoch','phase','runtime','procurement_source','authority_ref',
    'execution_surface','measurement_epoch_ref','baseline_binding_sha256','baseline_sha256','active_runtime_binding_sha256'];
  if(!reference||typeof reference!=='object'||Array.isArray(reference)
    ||Object.keys(reference).sort().join('\0')!==fields.sort().join('\0')
    ||reference.schema!=='pulse_canonical_measurement_baseline_reference/v1'||reference.status!=='open'
    ||!['work_item_id','agent_id','epoch','phase','runtime','procurement_source','authority_ref','execution_surface'].every(k=>reference[k]===binding[k])
    ||reference.active_runtime_binding_sha256!==binding.binding_sha256
    ||!/^measurement_epoch_v1_[a-f0-9]{64}$/.test(reference.measurement_epoch_ref)
    ||![reference.baseline_binding_sha256,reference.baseline_sha256].every(v=>SHA_RE.test(v)))
    throw new Error('Canonical measurement origin does not identify this runtime');
  return reference;
}
function measurementBindingDigest(config){return config.measurementBaselineReference?.baseline_binding_sha256??config.binding.binding_sha256;}

async function readCanonicalBaseline(config,env,runner){
  const origin=config.measurementBaselineReference;
  let result;
  try{result=await runner(config.sclBin,['describe-measurement-local',config.binding.work_item_id,
    '--agent',config.binding.agent_id,'--session-ref',config.binding.authority_ref,
    '--epoch',String(config.binding.epoch),'--phase',config.binding.phase,'--surface-id',config.binding.execution_surface,
    '--harness-root',config.sclHarnessRoot,'--kernel-root',config.sclKernelRoot],{
    env:{PATH:env.PATH??'',SCL_SESSION_REF:config.sclSessionRef,
      ...(typeof env.LD_LIBRARY_PATH==='string'?{LD_LIBRARY_PATH:env.LD_LIBRARY_PATH}:{})},
    encoding:'utf8',maxBuffer:2*1024*1024,timeout:30_000});}
  catch{throw new PiUsageCaptureError('measurement_authority_unavailable');}
  let response;try{response=JSON.parse(result.stdout);if(result.stderr)throw new Error();}
  catch{throw new PiUsageCaptureError('measurement_authority_unavailable');}
  const m=response?.result?.measurement;
  if(response?.schema_version!=='scl_response/v1'||response.status!=='read_only'
    ||m?.schema_version!=='scl_measurement_epoch/v1'||m.status!=='open'
    ||m.work_item_id!==origin.work_item_id||m.agent_id!==origin.agent_id||m.execution_epoch!==origin.epoch
    ||m.phase!==origin.phase||m.runtime!=='pi'||m.procurement_source!==origin.procurement_source
    ||m.authority_ref!==origin.authority_ref||m.host_session_ref!==origin.authority_ref
    ||m.execution_surface!==origin.execution_surface||m.measurement_epoch_ref!==origin.measurement_epoch_ref
    ||m.runtime_binding_sha256!==origin.baseline_binding_sha256||m.baseline_sha256!==origin.baseline_sha256
    ||m.final_sha256||m.seal_ref||m.seal_sha256)
    throw new PiUsageCaptureError('measurement_authority_conflict');
}

const FORBIDDEN_SNAPSHOT_KEYS = new Set([
  'session_id', 'sessionid', 'session_file', 'sessionfile',
  'thread_id', 'threadid', 'turn_id', 'turnid',
  'message_id', 'messageid', 'response_id', 'responseid',
  'prompt', 'response', 'credential', 'api_key', 'cwd', 'path', 'content',
]);

function assertPrivacy(value, rawSessionFile) {
  const pending = [value];
  while (pending.length) {
    const current = pending.pop();
    if (typeof current === 'string') {
      if (current === rawSessionFile || isAbsolute(current)) {
        throw new Error('Pi snapshot exposed its source path');
      }
    } else if (Array.isArray(current)) {
      pending.push(...current);
    } else if (current && typeof current === 'object') {
      for (const [key, nested] of Object.entries(current)) {
        if (FORBIDDEN_SNAPSHOT_KEYS.has(key.toLowerCase())) {
          throw new Error(`Pi snapshot exposed forbidden field: ${key}`);
        }
        pending.push(nested);
      }
    }
  }
}

async function atomicWrite(path, text) {
  await mkdir(dirname(path), { recursive: true, mode: 0o700 });
  const temporary = `${path}.${process.pid}.${randomUUID()}.tmp`;
  const handle = await open(temporary, 'wx', 0o600);
  try {
    await handle.writeFile(text, 'utf8');
    await handle.sync();
  } finally {
    await handle.close();
  }
  try {
    await rename(temporary, path);
    await chmod(path, 0o600);
  } finally {
    await rm(temporary, { force: true });
  }
}

function canonical(value) {
  if (Array.isArray(value)) return `[${value.map(canonical).join(',')}]`;
  if (value && typeof value === 'object') {
    return `{${Object.keys(value).sort().map((key) => `${JSON.stringify(key)}:${canonical(value[key])}`).join(',')}}`;
  }
  return JSON.stringify(value);
}

function validateSnapshot(snapshot, config, lifecycleState, measurementRole, rawSessionFile) {
  if (
    snapshot?.schema !== SNAPSHOT_SCHEMA
    || snapshot.work_item_id !== config.binding.work_item_id
    || snapshot.agent_id !== config.binding.agent_id
    || snapshot.execution_surface !== config.binding.execution_surface
    || snapshot.phase !== config.binding.phase
    || snapshot.epoch !== config.binding.epoch
    || snapshot.procurement_source !== config.binding.procurement_source
    || snapshot.authority_ref !== config.binding.authority_ref
    || snapshot.runtime_binding_sha256 !== measurementBindingDigest(config)
    || snapshot.lifecycle_state !== lifecycleState
    || snapshot.measurement_role !== measurementRole
    || !SHA_RE.test(String(snapshot.snapshot_sha256 ?? ''))
  ) {
    throw new Error('Pi host usage snapshot does not match runtime authority');
  }
  const unsigned = Object.fromEntries(Object.entries(snapshot).filter(([key]) => key !== 'snapshot_sha256'));
  const digest = createHash('sha256').update(canonical(unsigned)).digest('hex');
  if (digest !== snapshot.snapshot_sha256) {
    throw new Error('Pi host usage snapshot digest does not match');
  }
  const summary = snapshot.summary;
  const boundary = snapshot.attribution_boundary;
  if (!summary || typeof summary !== 'object' || !boundary || typeof boundary !== 'object'
      || boundary.policy !== 'baseline_exclusive_final_inclusive') {
    throw new Error('Pi host bounded usage snapshot is invalid');
  }
  if (measurementRole === 'baseline' && (
    summary.event_count !== 0
    || !summary.totals || Object.values(summary.totals).some(value => value !== 0)
    || boundary.baseline_snapshot_sha256 !== null
    || canonical(boundary.baseline) !== canonical(boundary.final)
    || snapshot.coverage !== 'exact_zero_baseline'
  )) throw new Error('Pi host baseline is not an exact zero boundary');
  if (measurementRole === 'final' && (
    !SHA_RE.test(String(boundary.baseline_snapshot_sha256 ?? ''))
    || snapshot.coverage !== 'exact_bounded_sealed'
  )) throw new Error('Pi host final is not bounded by an immutable baseline');
  assertPrivacy(snapshot, rawSessionFile);
  return snapshot;
}

function receiptFor(snapshot, binding, artifactRef, idempotentReplay = false, origin) {
  return {
    schema: 'pi_host_usage_capture_receipt/v1',
    work_item_id: binding.work_item_id,
    epoch: binding.epoch,
    lifecycle_state: snapshot.lifecycle_state,
    binding_sha256: origin?.baseline_binding_sha256??binding.binding_sha256,
    ...(origin?{active_runtime_binding_sha256:binding.binding_sha256}:{}),
    snapshot_sha256: snapshot.snapshot_sha256,
    artifact_ref: artifactRef,
    ...(idempotentReplay ? { idempotent_replay: true } : {}),
  };
}

async function readExistingSnapshot(target, config, lifecycleState, measurementRole, rawSessionFile) {
  let text;
  try {
    const metadata = await lstat(target);
    if (!metadata.isFile() || metadata.isSymbolicLink() || (metadata.mode & 0o777) !== 0o600) {
      throw new Error('Pi host usage snapshot mode is invalid');
    }
    text = await readFile(target, 'utf8');
  } catch (error) {
    if (error?.code === 'ENOENT') return undefined;
    if (error instanceof Error && error.message === 'Pi host usage snapshot mode is invalid') throw error;
    throw new Error('Pi host usage snapshot readback failed');
  }
  let snapshot;
  try {
    snapshot = JSON.parse(text);
  } catch {
    throw new Error('Pi host usage snapshot readback is malformed');
  }
  return validateSnapshot(snapshot, config, lifecycleState, measurementRole, rawSessionFile);
}

async function sourceBytes(sessionFile, initialSessionState) {
  try {
    return await readFile(sessionFile);
  } catch (error) {
    if (error?.code !== 'ENOENT' || initialSessionState === undefined) throw error;
    return Buffer.from(serializeInitialSessionSource(initialSessionState), 'utf8');
  }
}

async function verifySourcePrefix(snapshot, sessionFile, position, initialSessionState) {
  const sourcePosition = snapshot?.attribution_boundary?.[position];
  if (
    !sourcePosition || !Number.isSafeInteger(sourcePosition.source_bytes)
    || sourcePosition.source_bytes < 0
    || !SHA_RE.test(String(sourcePosition.source_prefix_sha256 ?? ''))
  ) throw new Error('Pi host bounded source position is invalid');
  let raw;
  try {
    raw = await sourceBytes(sessionFile, initialSessionState);
  } catch {
    throw new Error('Pi host bounded source readback failed');
  }
  if (
    raw.length < sourcePosition.source_bytes
    || createHash('sha256').update(raw.subarray(0, sourcePosition.source_bytes)).digest('hex')
      !== sourcePosition.source_prefix_sha256
  ) throw new Error(`Pi host usage changed before the ${position} position`);
  return { sourceBytes: sourcePosition.source_bytes, excludedSuffixBytes: raw.length - sourcePosition.source_bytes };
}

function assertFinalDescendsFromBaseline(finalSnapshot, baselineSnapshot) {
  const finalBoundary = finalSnapshot?.attribution_boundary;
  const baselineBoundary = baselineSnapshot?.attribution_boundary;
  if (
    finalBoundary?.baseline_snapshot_sha256 !== baselineSnapshot?.snapshot_sha256
    || canonical(finalBoundary?.baseline) !== canonical(baselineBoundary?.baseline)
    || !Number.isSafeInteger(finalBoundary?.final?.source_bytes)
    || finalBoundary.final.source_bytes < baselineBoundary?.baseline?.source_bytes
  ) {
    throw new Error('Pi host final does not descend from the immutable baseline');
  }
}

async function readImmutableBaseline(target, config, sessionFile, initialSessionState) {
  const baseline = await readExistingSnapshot(
    target, config, 'open', 'baseline', sessionFile,
  );
  if (!baseline) throw new Error('Pi host immutable usage baseline is unavailable');
  await verifySourcePrefix(baseline, sessionFile, 'baseline', initialSessionState);
  return baseline;
}

async function recordSclNativeUsage(
  config,
  env,
  snapshotPath,
  snapshot,
  baselineSnapshot,
  lifecycleState,
  runner,
  sealedBaselineSnapshot,
) {
  let result;
  try {
    result = await runner(
      config.sclBin,
      [
        'record-native-usage-local', config.binding.work_item_id,
        '--harness-root', config.sclHarnessRoot,
        '--kernel-root', config.sclKernelRoot,
        '--input', snapshotPath,
      ],
      {
        env: {
          PATH: env.PATH ?? '',
          SCL_SESSION_REF: config.sclSessionRef,
          ...(typeof env.LD_LIBRARY_PATH==='string'?{LD_LIBRARY_PATH:env.LD_LIBRARY_PATH}:{}),
          ...(env.PULSE_HARNESS_HOME ? {PULSE_HARNESS_HOME: env.PULSE_HARNESS_HOME} : {}),
          ...(env.HARNESS_WORKTREE_STATE_ROOT ? {HARNESS_WORKTREE_STATE_ROOT: env.HARNESS_WORKTREE_STATE_ROOT} : {}),
        },
        encoding: 'utf8',
        maxBuffer: 2 * 1024 * 1024,
        timeout: 30_000,
      },
    );
  } catch (error) {
    // execFile rejects nonzero exits, including valid sealed SCL rejections.
    // Retain only typed machine evidence; never forward stderr/provider text.
    if (typeof error?.stdout !== 'string' || !error.stdout.trim()) {
      const transient = error?.code === 'EAGAIN' || error?.code === 'ETIMEDOUT'
        || (error?.killed === true && error?.signal === 'SIGTERM');
      throw new PiUsageCaptureError('measurement_transport_unavailable', transient);
    }
    result = { stdout: error.stdout, stderr: '' };
  }
  let response;
  try {
    const lines = result.stdout.split(/\r?\n/).map(line => line.trim()).filter(Boolean);
    if (lines.length !== 1 || result.stderr) throw new Error('unclean response');
    response = JSON.parse(lines[0]);
  } catch {
    throw new Error('Pi host canonical SCL measurement record was malformed');
  }
  const measurement = response?.result?.measurement;
  const sealedBaselineReplay = lifecycleState === 'open' && snapshot.measurement_role === 'baseline'
    && measurement?.status === 'sealed' && sealedBaselineSnapshot !== undefined;
  if (response?.schema_version === 'scl_response/v1'
      && MEASUREMENT_FAILURE_CODES.has(response?.error?.code)
      && typeof response.error.retryable === 'boolean') {
    throw new PiUsageCaptureError(response.error.code, response.error.retryable);
  }
  if (
    response?.schema_version !== 'scl_response/v1'
    || response.status !== 'local_committed'
    || response?.work_item?.work_item_id !== config.binding.work_item_id
    || measurement?.schema_version !== 'scl_measurement_epoch/v1'
    || measurement.execution_epoch !== config.binding.epoch
    || measurement.phase !== config.binding.phase
    || measurement.execution_surface !== config.binding.execution_surface
    || !PI_MEASUREMENT_RUNTIME_CLASSES.has(measurement.runtime_class)
    || measurement.agent_id !== config.binding.agent_id
    || (sealedBaselineSnapshot !== undefined && !sealedBaselineReplay)
    || measurement.status !== (lifecycleState === 'sealed' || sealedBaselineReplay ? 'sealed' : 'open')
    || !/^measurement_epoch_v1_[0-9a-f]{64}$/.test(String(measurement.measurement_epoch_ref ?? ''))
    || !SHA_RE.test(String(measurement.attribution_sha256 ?? ''))
    || measurement.baseline_sha256 !== baselineSnapshot.snapshot_sha256
    || (config.measurementBaselineReference && (
      measurement.runtime!=='pi'||measurement.procurement_source!==config.binding.procurement_source
      ||measurement.authority_ref!==config.binding.authority_ref
      ||measurement.runtime_binding_sha256!==measurementBindingDigest(config)
      ||measurement.measurement_epoch_ref!==config.measurementBaselineReference.measurement_epoch_ref
    ))
    || (sealedBaselineReplay && (
      measurement.authority_ref !== config.binding.authority_ref
      || measurement.runtime_binding_sha256 !== measurementBindingDigest(config)
      || measurement.final_sha256 !== sealedBaselineSnapshot.snapshot_sha256
      || !SHA_RE.test(String(measurement.seal_sha256 ?? ''))
    ))
    || (lifecycleState === 'sealed' && (
      measurement.final_sha256 !== snapshot.snapshot_sha256
      || !SHA_RE.test(String(measurement.seal_sha256 ?? ''))
    ))
  ) {
    throw new Error('Pi host canonical SCL measurement record did not match runtime authority');
  }
  return measurement.status;
}

async function runProbe(config, env, sessionFile, lifecycleState, measurementRole, baselinePath, runner, unpersistedBaseline = false, capturePath) {
  let result;
  try {
    result = await runner(
      config.python,
      [
        '-m', 'pulse_compute_cost', 'pi-session-probe',
        '--lifecycle-state', lifecycleState, '--measurement-role', measurementRole,
      ],
      {
        env: {
          PATH: env.PATH ?? '',
          PYTHONPATH: config.pythonPath,
          PI_SESSION_FILE: sessionFile,
          PULSE_PI_USAGE_CAPTURE_PATH: capturePath,
          ...(typeof env.LD_LIBRARY_PATH === 'string'
            ? { LD_LIBRARY_PATH: env.LD_LIBRARY_PATH } : {}),
          ...(unpersistedBaseline ? { PULSE_PI_USAGE_UNPERSISTED_BASELINE: '1' } : {}),
          PULSE_RUNTIME_USAGE_BINDING: env.PULSE_RUNTIME_USAGE_BINDING,
          ...(config.measurementBaselineReference?{PULSE_CANONICAL_MEASUREMENT_BASELINE_REFERENCE:JSON.stringify(config.measurementBaselineReference)}:{}),
          ...(baselinePath ? { PULSE_PI_USAGE_BASELINE_SNAPSHOT: baselinePath } : {}),
        },
        encoding: 'utf8',
        maxBuffer: 2 * 1024 * 1024,
        timeout: 30_000,
      },
    );
  } catch {
    throw new Error('Pi host usage probe failed');
  }
  let captured;
  try {
    captured = JSON.parse(result.stdout);
  } catch {
    throw new Error('Pi host usage probe returned malformed JSON');
  }
  if (captured?.schema !== 'pi_atomic_capture/v1'
      || typeof captured.idempotent_replay !== 'boolean') {
    throw new Error('Pi host native capture response is invalid');
  }
  return {
    snapshot: validateSnapshot(captured.snapshot, config, lifecycleState, measurementRole, sessionFile),
    idempotentReplay: captured.idempotent_replay,
  };
}

function serializeInitialSessionSource(initialSessionState) {
  if (
    !initialSessionState || typeof initialSessionState !== 'object'
    || typeof initialSessionState.session_id !== 'string'
    || !initialSessionState.session_id
    || !Array.isArray(initialSessionState.entries)
    || !initialSessionState.header || typeof initialSessionState.header !== 'object'
    || Array.isArray(initialSessionState.header)
    || initialSessionState.header.type !== 'session'
    || initialSessionState.header.id !== initialSessionState.session_id
    || !Number.isSafeInteger(initialSessionState.header.version)
    || initialSessionState.header.version < 1
    || !isCanonicalUtcTimestamp(initialSessionState.header.timestamp)
    || typeof initialSessionState.header.cwd !== 'string'
    || !initialSessionState.header.cwd
  ) {
    throw new Error('Pi host unpersisted baseline state is invalid');
  }
  const allowed = new Set(['model_change', 'thinking_level_change']);
  const hostMetadata = (entry) => entry.type === 'custom' && (
    (entry.customType === 'pulse-computecost-usage'
      && entry.data?.schema === 'pi_host_usage_capture_receipt/v1')
    || (entry.customType === 'pulse-standalone-pi-binding'
      && entry.data?.schema === 'pulse_standalone_pi_binding/v1')
  );
  if (initialSessionState.entries.some((entry) => (
    !entry || typeof entry !== 'object' || Array.isArray(entry)
    || (!allowed.has(entry.type) && !hostMetadata(entry))
    || typeof entry.id !== 'string' || !entry.id
    || !isCanonicalUtcTimestamp(entry.timestamp)
    || (entry.type === 'model_change' && (
      typeof entry.provider !== 'string' || !entry.provider
      || typeof entry.modelId !== 'string' || !entry.modelId
    ))
    || (entry.type === 'thinking_level_change' && (
      typeof entry.thinkingLevel !== 'string' || !entry.thinkingLevel
    ))
  ))) {
    throw new Error('Pi host unpersisted baseline contains non-configuration entries');
  }
  return `${[initialSessionState.header, ...initialSessionState.entries]
    .map((entry) => JSON.stringify(entry)).join('\n')}\n`;
}

async function runBaselineProbe(
  config,
  env,
  sessionFile,
  lifecycleState,
  measurementRole,
  initialSessionState,
  runner,
  capturePath,
) {
  if (
    lifecycleState !== 'open'
    || measurementRole !== 'baseline'
    || initialSessionState === undefined
  ) {
    return runProbe(config, env, sessionFile, lifecycleState, measurementRole, undefined, runner, false, capturePath);
  }
  try {
    await stat(sessionFile);
    return await runProbe(config, env, sessionFile, lifecycleState, measurementRole, undefined, runner, false, capturePath);
  } catch (error) {
    if (error?.code !== 'ENOENT') throw error;
  }
  // Pi 0.84 defers a new session's first JSONL write until the first assistant
  // message. Materialize only the host-owned in-memory header long enough for
  // the native probe to validate the source and establish a byte-zero origin.
  const source = serializeInitialSessionSource(initialSessionState);
  await mkdir(config.outputDir, { recursive: true, mode: 0o700 });
  const probeFile = join(
    config.outputDir,
    `.pi-unpersisted-baseline-${process.pid}-${randomUUID()}.jsonl`,
  );
  const handle = await open(probeFile, 'wx', 0o600);
  try {
    await handle.writeFile(source, 'utf8');
    await handle.sync();
  } finally {
    await handle.close();
  }
  try {
    const captured = await runProbe(config, env, probeFile, lifecycleState, measurementRole, undefined, runner, true, capturePath);
    assertPrivacy(captured.snapshot, sessionFile);
    return captured;
  } finally {
    await rm(probeFile, { force: true });
  }
}

async function recoverUncommittedVolatileBaseline(
  { config, env, target, existing, sessionFile, initialSessionState, runner, sclRunner },
) {
  // Never repair a persisted transcript mismatch or replace a canonical fact.
  // SCL's immutable-fact compare-and-set is the authority for this migration.
  try { await stat(sessionFile); throw new Error('Pi persisted baseline conflict'); }
  catch (error) { if (error?.code !== 'ENOENT') throw error; }
  serializeInitialSessionSource(initialSessionState);
  if (existing.summary.event_count !== 0
      || existing.attribution_boundary.excluded_pre_baseline_usage_events !== 0
      || existing.attribution_boundary.baseline.source_bytes === 0) {
    throw new Error('Pi baseline is not an uncommitted volatile origin');
  }
  {
    const original = await readFile(target, 'utf8');
    if (JSON.parse(original).snapshot_sha256 !== existing.snapshot_sha256) {
      throw new Error('Pi baseline changed during origin recovery');
    }
    const archive = `${target}.superseded-${existing.snapshot_sha256}.json`;
    try {
      const handle = await open(archive, 'wx', 0o600);
      try { await handle.writeFile(original); await handle.sync(); } finally { await handle.close(); }
    } catch (error) {
      if (error?.code !== 'EEXIST' || await readFile(archive, 'utf8') !== original) throw error;
    }
    const candidatePath = `${target}.origin-candidate.json`;
    let candidate = await readExistingSnapshot(candidatePath, config, 'open', 'baseline', sessionFile);
    if (!candidate) {
      const captured = await runBaselineProbe(config, env, sessionFile, 'open', 'baseline', initialSessionState, runner, candidatePath);
      candidate = captured.snapshot;
    }
    if (candidate.attribution_boundary.baseline.source_bytes !== 0
        || candidate.attribution_boundary.excluded_pre_baseline_usage_events !== 0) {
      throw new Error('Pi origin recovery did not produce an empty boundary');
    }
    await verifySourcePrefix(candidate, sessionFile, 'baseline', initialSessionState);
    // An already accepted different baseline rejects here. Keep both local
    // artifacts and do not overwrite the authoritative baseline alias.
    await recordSclNativeUsage(config, env, candidatePath, candidate, candidate, 'open', sclRunner);
    const current = await readFile(target, 'utf8');
    if (current !== original && JSON.parse(current).snapshot_sha256 !== candidate.snapshot_sha256) {
      throw new Error('Pi baseline changed during origin recovery');
    }
    if (current === original) await atomicWrite(target, await readFile(candidatePath, 'utf8'));
    return receiptFor(candidate, config.binding, basename(target), current !== original);
  }
}

export async function capturePiUsageSnapshot({
  env = process.env,
  sessionFile,
  lifecycleState,
  measurementRole = lifecycleState === 'sealed' ? 'final' : 'baseline',
  initialSessionState,
  runner = execFile,
  sclRunner = execFile,
}) {
  if (!sessionFile || !isAbsolute(sessionFile)) {
    throw new Error('Pi host session file is unavailable');
  }
  if (!['open', 'sealed'].includes(lifecycleState)) {
    throw new Error('Pi host lifecycle state is invalid');
  }
  if (
    !['baseline', 'progress', 'final'].includes(measurementRole)
    || (lifecycleState === 'sealed') !== (measurementRole === 'final')
  ) {
    throw new Error('Pi host measurement role is invalid');
  }
  const config = hostConfig(env);
  const artifactState = measurementRole === 'baseline' ? 'baseline' : lifecycleState;
  const target = join(
    config.outputDir,
    `${config.binding.work_item_id}-${measurementBindingDigest(config)}-${artifactState}.json`,
  );
  const baselineTarget = join(
    config.outputDir,
    `${config.binding.work_item_id}-${measurementBindingDigest(config)}-baseline.json`,
  );
  const artifactRef = basename(target);
  const immutableBaseline = measurementRole === 'baseline'
    ? undefined
    : await readImmutableBaseline(baselineTarget, config, sessionFile, initialSessionState);
  if(config.measurementBaselineReference&&immutableBaseline?.snapshot_sha256!==undefined
    &&immutableBaseline.snapshot_sha256!==config.measurementBaselineReference.baseline_sha256)
    throw new PiUsageCaptureError('measurement_authority_conflict');
  if (measurementRole === 'baseline') {
    const existing = await readExistingSnapshot(target, config, lifecycleState, measurementRole, sessionFile);
    if(config.measurementBaselineReference){
      if(!existing||existing.snapshot_sha256!==config.measurementBaselineReference.baseline_sha256)
        throw new PiUsageCaptureError('measurement_authority_conflict');
      // Read the original native prefix and current canonical fact; never
      // regenerate an origin or reopen a sealed measurement to adopt a process.
      await verifySourcePrefix(existing,sessionFile,'baseline',initialSessionState);
      const sealed=await readExistingSnapshot(join(config.outputDir,
        `${config.binding.work_item_id}-${measurementBindingDigest(config)}-sealed.json`),config,'sealed','final',sessionFile);
      if(sealed){
        await verifySourcePrefix(sealed,sessionFile,'final');
        assertFinalDescendsFromBaseline(sealed,existing);
        await recordSclNativeUsage(config,env,target,existing,existing,'open',sclRunner,sealed);
        return {...receiptFor(existing,config.binding,artifactRef,true,config.measurementBaselineReference),
          canonical_lifecycle_state:'sealed'};
      }
      await readCanonicalBaseline(config,env,sclRunner);
      return receiptFor(existing,config.binding,artifactRef,true,config.measurementBaselineReference);
    }
    if (existing) {
      try {
        await verifySourcePrefix(existing, sessionFile, 'baseline', initialSessionState);
      } catch (error) {
        if (error.message !== 'Pi host usage changed before the baseline position') throw error;
        return recoverUncommittedVolatileBaseline({
          config, env, target, existing, sessionFile, initialSessionState, runner, sclRunner,
        });
      }
      // A resumed rollover source may already be sealed. Verify the existing
      // final and canonical seal before acknowledging its baseline for retirement.
      const sealedSnapshot = await readExistingSnapshot(
        join(config.outputDir, `${config.binding.work_item_id}-${measurementBindingDigest(config)}-sealed.json`),
        config, 'sealed', 'final', sessionFile,
      );
      if (sealedSnapshot) {
        await verifySourcePrefix(sealedSnapshot, sessionFile, 'final');
        assertFinalDescendsFromBaseline(sealedSnapshot, existing);
      }
      const canonicalState = await recordSclNativeUsage(
        config, env, target, existing, existing, lifecycleState, sclRunner, sealedSnapshot,
      );
      return { ...receiptFor(existing, config.binding, artifactRef, true, config.measurementBaselineReference),
        ...(canonicalState === 'sealed' ? { canonical_lifecycle_state: 'sealed' } : {}),
      };
    }
  }
  if (lifecycleState === 'sealed') {
    const existing = await readExistingSnapshot(target, config, lifecycleState, measurementRole, sessionFile);
    if (existing) {
      await verifySourcePrefix(existing, sessionFile, 'final');
      assertFinalDescendsFromBaseline(existing, immutableBaseline);
      await recordSclNativeUsage(
        config, env, target, existing, immutableBaseline, lifecycleState, sclRunner,
      );
      return receiptFor(existing, config.binding, artifactRef, true, config.measurementBaselineReference);
    }
  }

  // The actual native sampler/writer holds an OS lock through publication.
  // There is no PID/mtime lease and no lock directory for a crash to strand.
  const captured = measurementRole === 'baseline'
    ? await runBaselineProbe(
      config, env, sessionFile, lifecycleState, measurementRole,
      initialSessionState, runner, target,
    )
    : await runProbe(
      config, env, sessionFile, lifecycleState, measurementRole,
      baselineTarget, runner, false, target,
    );
  const readback = await readExistingSnapshot(target, config, lifecycleState, measurementRole, sessionFile);
  if (!readback || readback.snapshot_sha256 !== captured.snapshot.snapshot_sha256) {
    throw new Error('Pi host usage snapshot readback is unavailable or changed');
  }
  await verifySourcePrefix(
    readback, sessionFile, measurementRole === 'baseline' ? 'baseline' : 'final', initialSessionState,
  );
  if (measurementRole !== 'baseline') assertFinalDescendsFromBaseline(readback, immutableBaseline);
  if (measurementRole !== 'progress') {
    await recordSclNativeUsage(
      config, env, target, readback, immutableBaseline ?? readback, lifecycleState, sclRunner,
    );
  }
  return receiptFor(readback, config.binding, artifactRef, captured.idempotentReplay, config.measurementBaselineReference);
}

// A measurement extension is an observer, never a WI or tool controller.
// Legacy standalone /wi admission was removed; its saved entries remain history.
export function installPiUsageLifecycle(pi, options = {}) {
  const managed = options.standaloneConfig === undefined;
  const captureSnapshot = options.captureSnapshot ?? capturePiUsageSnapshot;
  let settled = true;
  let sealed = false;
  let baselineReady = false;
  const captureEnvironment = process.env;
  const conversationOnly = process.env.PULSE_PI_CONVERSATION_ONLY === '1';
  let captureTail = Promise.resolve();

  const captureNow = async (ctx, lifecycleState, measurementRole) => {
    if (conversationOnly) throw new PiUsageCaptureError('measurement_authority_unavailable', false);
    const sessionFile = ctx.sessionManager.getSessionFile();
    if (!sessionFile) throw new Error('Pi host session is not persisted');
    const receipt = await captureSnapshot({
      env: captureEnvironment,
      sessionFile,
      lifecycleState,
      measurementRole,
      initialSessionState: {
        header: ctx.sessionManager.getHeader?.(),
        session_id: ctx.sessionManager.getSessionId?.(),
        entries: ctx.sessionManager.getEntries?.(),
      },
    });
    if (!receipt.idempotent_replay) pi.appendEntry('pulse-computecost-usage', receipt);
    if (lifecycleState === 'sealed' || receipt.canonical_lifecycle_state === 'sealed') sealed = true;
    return receipt;
  };
  const capture = (ctx, lifecycleState, measurementRole) => {
    const pending = captureTail.then(() => captureNow(ctx, lifecycleState, measurementRole));
    captureTail = pending.catch(() => undefined);
    return pending;
  };

  pi.on('session_start', (_event, ctx) => {
    settled = true;
    sealed = false;
    baselineReady = false;
    if (!managed) {
      if (ctx.hasUI) ctx.ui.notify('Legacy standalone measurement admission is retired; use the installed native WI entry. Historical facts are retained; measurement is unavailable.', 'warning');
      return;
    }
    // Observation must not hold startup or the RPC reader. A missing baseline
    // limits this interval's measured coverage, not any execution capability.
    void capture(ctx, 'open', 'baseline').then(() => {
      baselineReady = true;
    }).catch(() => {
      if (ctx.hasUI) ctx.ui.notify('ComputeCost baseline unavailable; authorized work and native recovery are unaffected', 'warning');
    });
  });
  pi.on('agent_start', () => { settled = false; });
  pi.on('agent_settled', (_event, ctx) => {
    settled = true;
    if (!managed || !baselineReady || sealed) return;
    // Preserve ordering among captures without delaying the next user turn.
    void capture(ctx, 'open', 'progress').catch(() => undefined);
  });
  pi.on('session_shutdown', (event, ctx) => {
    if (!managed || event.reason === 'reload' || !settled || sealed || !baselineReady) return;
    // Do not delay shutdown for telemetry; the durable native source remains
    // available for bounded backfill. This is not a successful final seal.
    void capture(ctx, 'open', 'progress').catch(() => undefined);
  });

  pi.registerCommand('runtime-usage-baseline', {
    description: 'Persist and read back a usage baseline; never authorizes or blocks work',
    handler: async (_args, ctx) => {
      let readback;
      try {
        await ctx.waitForIdle();
        if (!managed) throw new PiUsageCaptureError('measurement_authority_unavailable', false);
        const receipt = await capture(ctx, 'open', 'baseline');
        baselineReady = true;
        readback = { ok: true, receipt };
      } catch (error) {
        baselineReady = false;
        const message = error instanceof Error ? error.message : '';
        const code = message.includes('mode is invalid') ? 'artifact_mode_invalid'
          : message.includes('digest does not match') ? 'artifact_digest_invalid'
            : message.includes('does not match runtime authority') ? 'authority_mismatch'
              : 'seal_failed';
        readback = { ok: false, error: error instanceof PiUsageCaptureError
          ? { code: error.code, retryable: error.retryable } : { code } };
      }
      if (ctx.hasUI) {
        ctx.ui.notify(`PULSE_USAGE_BASELINE/v1 ${JSON.stringify(readback)}`, readback.ok ? 'info' : 'error');
      }
    },
  });

  pi.registerCommand('runtime-usage-seal', {
    description: 'Seal measured usage after settling; does not retire or restrict execution',
    handler: async (_args, ctx) => {
      let readback;
      try {
        await ctx.waitForIdle();
        if (!managed || !settled) throw new Error('Pi agent is not settled');
        if (!baselineReady) throw new Error('Pi immutable baseline is not active');
        const receipt = await capture(ctx, 'sealed', 'final');
        readback = { ok: true, receipt };
      } catch (error) {
        baselineReady = false;
        const message = error instanceof Error ? error.message : '';
        const code = message.includes('not settled') ? 'not_settled'
          : message.includes('mode is invalid') ? 'artifact_mode_invalid'
            : message.includes('digest does not match') ? 'artifact_digest_invalid'
              : message.includes('does not match runtime authority') ? 'authority_mismatch'
                : message.includes('lock did not converge') ? 'single_winner_timeout'
                  : message.includes('usage changed before the final position')
                    || message.includes('does not descend from the immutable baseline') ? 'seal_conflict'
                    : 'seal_failed';
        readback = { ok: false, error: error instanceof PiUsageCaptureError
          ? { code: error.code, retryable: error.retryable } : { code } };
      }
      if (ctx.hasUI) {
        ctx.ui.notify(`PULSE_USAGE_SEAL/v1 ${JSON.stringify(readback)}`, readback.ok ? 'info' : 'error');
      }
    },
  });
}

export default function install(pi) {
  installPiUsageLifecycle(pi);
}
