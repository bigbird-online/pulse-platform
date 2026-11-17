import assert from 'node:assert/strict';
import { createHash } from 'node:crypto';
import { mkdtemp, readFile, stat, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import test from 'node:test';
import { execFile } from 'node:child_process';
import { promisify } from 'node:util';
import { fileURLToPath } from 'node:url';

import {
  capturePiUsageSnapshot as captureAtomicPiUsageSnapshot,
  installPiUsageLifecycle,
  PiUsageCaptureError,
} from '../runtime/pi-usage-binding.mjs';

const unsignedBinding = {
  schema: 'pulse_runtime_usage_binding/v3',
  work_item_id: 'wi_20260822_730',
  agent_id: 'pulse',
  phase: 'work_item',
  epoch: 12,
  runtime: 'pi',
  procurement_source: 'chatgpt_subscription',
  issued_at: '2026-08-25T01:00:00Z',
  authority_ref: `session_v1_${'a'.repeat(64)}`,
  execution_surface: 'surface_v1_local_macos',
};

test('nonzero canonical rejection retains a safe deterministic code instead of seal_failed', async () => {
  const root = await mkdtemp(join(tmpdir(), 'pi-usage-rejection-'));
  const sessionFile = join(root, 'target.jsonl');
  await writeFile(sessionFile, '');
  await assert.rejects(captureNativePiUsageSnapshot({
    env: environment(root), sessionFile, lifecycleState: 'open',
    runner: async () => ({ stdout: JSON.stringify(snapshot('open')), stderr: '' }),
    sclRunner: async () => { throw Object.assign(new Error('private transport details'), {
      stdout: JSON.stringify({schema_version:'scl_response/v1',status:'rejected',
        error:{code:'measurement_fact_conflict',reason:'private native bytes',retryable:false}}),
      stderr:'private stderr',
    }); },
  }), error => error instanceof PiUsageCaptureError && error.code === 'measurement_fact_conflict'
    && error.retryable === false && !error.message.includes('private'));
});
for (const [details, retryable] of [
  [{code:'ENOENT'}, false], [{code:'EACCES'}, false], [{code:1}, false],
  [{}, false], [{code:'EAGAIN'}, true], [{code:'ETIMEDOUT'}, true],
  [{killed:true,signal:'SIGTERM'}, true],
]) {
  test(`missing SCL response retries only a proven transient transport (${JSON.stringify(details)})`, async () => {
    const root=await mkdtemp(join(tmpdir(),'pi-usage-transport-'));
    const sessionFile=join(root,'target.jsonl');await writeFile(sessionFile,'');
    await assert.rejects(captureNativePiUsageSnapshot({
      env:environment(root),sessionFile,lifecycleState:'open',
      runner:async()=>({stdout:JSON.stringify(snapshot('open')),stderr:''}),
      sclRunner:async()=>{throw Object.assign(new Error('private startup details'),details,{stdout:'',stderr:'private startup details'});},
    }), error=>error instanceof PiUsageCaptureError && error.code==='measurement_transport_unavailable'
      && error.retryable===retryable && !error.message.includes('private'));
  });
}

const binding = {
  ...unsignedBinding,
  binding_sha256: createHash('sha256').update(canonical(unsignedBinding)).digest('hex'),
};

function environment(outputDir) {
  return {
    PATH: '/usr/bin:/bin',
    PULSE_RUNTIME_USAGE_BINDING: JSON.stringify(binding),
    PULSE_RUNTIME_AUTHORITY_REF: binding.authority_ref,
    PULSE_RUNTIME_EXECUTION_SURFACE: binding.execution_surface,
    PULSE_COMPUTE_COST_PYTHON: '/usr/bin/python3',
    PULSE_COMPUTE_COST_PYTHONPATH: '/private/pulse/ComputeCost',
    PULSE_COMPUTE_COST_PI_SNAPSHOT_DIR: outputDir,
    PULSE_COMPUTE_COST_SCL_BIN: '/private/scl/bin/harness-scl',
    PULSE_COMPUTE_COST_SCL_HARNESS_ROOT: '/private/scl/harness-authority/.harness',
    PULSE_COMPUTE_COST_SCL_KERNEL_ROOT: '/private/scl/kernel',
    SCL_SESSION_REF: `session_v1_${'f'.repeat(64)}`,
  };
}

function canonical(value) {
  if (Array.isArray(value)) return `[${value.map(canonical).join(',')}]`;
  if (value && typeof value === 'object') {
    return `{${Object.keys(value).sort().map((key) => `${JSON.stringify(key)}:${canonical(value[key])}`).join(',')}}`;
  }
  return JSON.stringify(value);
}

const standaloneBindingEntry = 'pulse-standalone-pi-binding';

function sessionSource(id = 'private-session') {
  return `${JSON.stringify({
    type: 'session', version: 3, id,
    timestamp: '2026-08-25T01:00:00.000Z', cwd: '/private/project',
  })}\n`;
}

async function sclRunner(command, args, options, runtimeClass = 'pi') {
  const inputPath = args.at(-1);
  const lifecycleState = inputPath.includes('-sealed.json') ? 'sealed' : 'open';
  const inputSnapshot = JSON.parse(await readFile(inputPath, 'utf8'));
  const baselineSnapshot = lifecycleState === 'sealed'
    ? JSON.parse(await readFile(
      inputPath.replace(/-sealed\.json$/, '-baseline.json'), 'utf8',
    ))
    : inputSnapshot;
  assert.equal(command, '/private/scl/bin/harness-scl');
  assert.deepEqual(args.slice(0, 2), ['record-native-usage-local', binding.work_item_id]);
  assert.equal(options.env.SCL_SESSION_REF, `session_v1_${'f'.repeat(64)}`);
  return {
    stdout: JSON.stringify({
      schema_version: 'scl_response/v1',
      status: 'local_committed',
      work_item: { work_item_id: binding.work_item_id },
      result: {
        measurement: {
          schema_version: 'scl_measurement_epoch/v1',
          status: lifecycleState,
          measurement_epoch_ref: `measurement_epoch_v1_${'e'.repeat(64)}`,
          execution_epoch: binding.epoch,
          phase: binding.phase,
          execution_surface: binding.execution_surface,
          runtime_class: runtimeClass,
          agent_id: binding.agent_id,
          attribution_sha256: 'a'.repeat(64),
          baseline_sha256: baselineSnapshot.snapshot_sha256,
          ...(lifecycleState === 'sealed' ? {
            final_sha256: inputSnapshot.snapshot_sha256,
            seal_sha256: 'd'.repeat(64),
          } : {}),
        },
      },
    }),
    stderr: '',
  };
}

test('native capture preserves reviewed storage locators but no other Host secrets', async () => {
  const root = await mkdtemp(join(tmpdir(), 'pi-usage-storage-'));
  const sessionFile = join(root, 'target.jsonl'); await writeFile(sessionFile, '');
  const env = {...environment(root), PULSE_HARNESS_HOME: '/reviewed/pulse',
    HARNESS_WORKTREE_STATE_ROOT: '/reviewed/registry', UNRELATED_SECRET: 'never-forward'};
  await captureNativePiUsageSnapshot({env, sessionFile, lifecycleState: 'open',
    runner: async () => ({stdout: JSON.stringify(snapshot('open')), stderr: ''}),
    sclRunner: async (command, args, options) => {
      assert.equal(options.env.PULSE_HARNESS_HOME, env.PULSE_HARNESS_HOME);
      assert.equal(options.env.HARNESS_WORKTREE_STATE_ROOT, env.HARNESS_WORKTREE_STATE_ROOT);
      assert.equal(options.env.UNRELATED_SECRET, undefined);
      return sclRunner(command, args, options);
    },
  });
});

const mockCaptureTails = new Map();
function captureNativePiUsageSnapshot(input) {
  if (!input.runner) return captureAtomicPiUsageSnapshot(input);
  const original = input.runner;
  const runner = async (command, args, options) => {
    const target = options.env.PULSE_PI_USAGE_CAPTURE_PATH;
    const previous = mockCaptureTails.get(target) ?? Promise.resolve();
    const result = previous.then(async () => {
      let existing;
      try { existing = JSON.parse(await readFile(target, 'utf8')); }
      catch (error) { if (error.code !== 'ENOENT') throw error; }
      const replay = existing && args.at(-1) !== 'progress';
      const response = replay ? undefined : await original(command, args, options);
      const value = replay ? existing : JSON.parse(response.stdout);
      if (!replay) await writeFile(target, JSON.stringify(value), { mode: 0o600 });
      return { stdout: JSON.stringify({schema: 'pi_atomic_capture/v1', snapshot: value,
        idempotent_replay: Boolean(replay)}), stderr: response?.stderr ?? '' };
    });
    mockCaptureTails.set(target, result.catch(() => {}));
    return result;
  };
  return captureAtomicPiUsageSnapshot({...input, runner});
}

function capturePiUsageSnapshot(input) {
  return captureNativePiUsageSnapshot({ ...input, sclRunner });
}

function snapshot(lifecycleState, summary = {}) {
  const role = lifecycleState === 'sealed' ? 'final' : 'baseline';
  const baselinePosition = {
    source_bytes: 0,
    source_prefix_sha256: createHash('sha256').update('').digest('hex'),
    last_entry_ref: null,
  };
  const finalPosition = {
    source_bytes: Number(summary.source_bytes ?? 0),
    source_prefix_sha256: String(summary.source_prefix_sha256 ?? createHash('sha256').update('').digest('hex')),
    last_entry_ref: null,
  };
  const normalizedSummary = {
    event_count: role === 'baseline' ? 0 : 1,
    totals: {
      input: role === 'baseline' ? 0 : 1, cache_read: 0, cache_write: 0,
      output: 0, reasoning: 0, total_tokens: role === 'baseline' ? 0 : 1,
    },
    ...summary,
  };
  const value = {
    schema: 'pi_session_usage_snapshot/v2',
    generated_at: '2026-08-25T01:01:00Z',
    lifecycle_state: lifecycleState,
    measurement_role: role,
    work_item_id: binding.work_item_id,
    agent_id: binding.agent_id,
    execution_surface: binding.execution_surface,
    phase: binding.phase,
    epoch: binding.epoch,
    runtime: 'pi',
    procurement_source: binding.procurement_source,
    authority_ref: binding.authority_ref,
    runtime_binding_sha256: binding.binding_sha256,
    attribution_boundary: {
      policy: 'baseline_exclusive_final_inclusive',
      baseline_snapshot_sha256: role === 'baseline' ? null : snapshot('open').snapshot_sha256,
      baseline: baselinePosition,
      final: role === 'baseline' ? baselinePosition : finalPosition,
      excluded_pre_baseline_usage_events: 0,
      deduplicated_entries: 0,
    },
    summary: normalizedSummary,
    coverage: role === 'baseline' ? 'exact_zero_baseline' : 'exact_bounded_sealed',
  };
  return {
    ...value,
    snapshot_sha256: createHash('sha256').update(canonical(value)).digest('hex'),
  };
}

async function persistBaseline(root) {
  const baseline = snapshot('open');
  const path = join(
    root, `${binding.work_item_id}-${binding.binding_sha256}-baseline.json`,
  );
  await writeFile(path, JSON.stringify(baseline), { mode: 0o600 });
  return baseline;
}

test('final capture requires the exact private immutable baseline', async () => {
  const root = await mkdtemp(join(tmpdir(), 'pi-usage-extension-'));
  const sessionFile = join(root, 'target.jsonl');
  const source = sessionSource('final-session');
  await writeFile(sessionFile, source);
  await assert.rejects(
    capturePiUsageSnapshot({
      env: environment(root), sessionFile, lifecycleState: 'sealed',
      runner: async () => { throw new Error('probe must not run'); },
    }),
    /immutable usage baseline is unavailable/,
  );

  await persistBaseline(root);
  const mismatched = snapshot('sealed', {
    source_bytes: Buffer.byteLength(source),
    source_prefix_sha256: createHash('sha256').update(source).digest('hex'),
  });
  mismatched.attribution_boundary.baseline_snapshot_sha256 = 'f'.repeat(64);
  mismatched.snapshot_sha256 = createHash('sha256').update(canonical(
    Object.fromEntries(Object.entries(mismatched).filter(([key]) => key !== 'snapshot_sha256')),
  )).digest('hex');
  await assert.rejects(
    capturePiUsageSnapshot({
      env: environment(root), sessionFile, lifecycleState: 'sealed',
      runner: async () => ({ stdout: JSON.stringify(mismatched), stderr: '' }),
    }),
    /does not descend from the immutable baseline/,
  );
});

test('post-final host capture passes raw session path only through child env', async () => {
  const root = await mkdtemp(join(tmpdir(), 'pi-usage-extension-'));
  const rawSessionFile = join(root, 'raw-session-secret.jsonl');
  const source = sessionSource('sealed-source');
  await writeFile(rawSessionFile, source);
  await persistBaseline(root);
  let invocation;
  const receipt = await capturePiUsageSnapshot({
    env: environment(root),
    sessionFile: rawSessionFile,
    lifecycleState: 'sealed',
    runner: async (command, args, options) => {
      invocation = { command, args, options };
      return { stdout: JSON.stringify(snapshot('sealed', {
        source_bytes: Buffer.byteLength(source),
        source_prefix_sha256: createHash('sha256').update(source).digest('hex'),
      })), stderr: '' };
    },
  });

  assert.equal(invocation.command, '/usr/bin/python3');
  assert.deepEqual(invocation.args, [
    '-m', 'pulse_compute_cost', 'pi-session-probe',
    '--lifecycle-state', 'sealed', '--measurement-role', 'final',
  ]);
  assert.equal(invocation.options.env.PI_SESSION_FILE, rawSessionFile);
  assert.equal(JSON.stringify(invocation.args).includes(rawSessionFile), false);
  assert.deepEqual(receipt, {
    schema: 'pi_host_usage_capture_receipt/v1',
    work_item_id: binding.work_item_id,
    epoch: 12,
    lifecycle_state: 'sealed',
    binding_sha256: binding.binding_sha256,
    snapshot_sha256: snapshot('sealed', {
      source_bytes: Buffer.byteLength(source),
      source_prefix_sha256: createHash('sha256').update(source).digest('hex'),
    }).snapshot_sha256,
    artifact_ref: `${binding.work_item_id}-${binding.binding_sha256}-sealed.json`,
  });
  const path = join(root, `${binding.work_item_id}-${binding.binding_sha256}-sealed.json`);
  const persisted = await readFile(path, 'utf8');
  assert.equal(persisted.includes(rawSessionFile), false);
  assert.equal(JSON.parse(persisted).lifecycle_state, 'sealed');
  assert.equal((await stat(path)).mode & 0o777, 0o600);
});

test('native probe permits only explicit LD_LIBRARY_PATH from the host environment', async () => {
  for (const [label, ldLibraryPath] of [
    ['explicit', '/private/tool-python/lib'],
    ['absent', undefined],
  ]) {
    const root = await mkdtemp(join(tmpdir(), `pi-probe-env-${label}-`));
    const sessionFile = join(root, 'target.jsonl');
    await writeFile(sessionFile, '');
    const env = {
      ...environment(root),
      ...(ldLibraryPath === undefined ? {} : { LD_LIBRARY_PATH: ldLibraryPath }),
      LD_PRELOAD: '/private/unsafe-preload.so',
      UNRELATED_SECRET: 'must-not-reach-probe',
    };
    let probeEnv;
    await captureNativePiUsageSnapshot({
      env, sessionFile, lifecycleState: 'open',
      runner: async (_command, _args, options) => {
        probeEnv = options.env;
        return { stdout: JSON.stringify(snapshot('open')), stderr: '' };
      },
      sclRunner,
    });
    assert.deepEqual(Object.keys(probeEnv).sort(), [
      'PATH', 'PI_SESSION_FILE', 'PULSE_PI_USAGE_CAPTURE_PATH', 'PULSE_RUNTIME_USAGE_BINDING',
      'PYTHONPATH',
      ...(ldLibraryPath === undefined ? [] : ['LD_LIBRARY_PATH']),
    ].sort());
    assert.equal(probeEnv.LD_LIBRARY_PATH, ldLibraryPath);
    assert.equal(probeEnv.LD_PRELOAD, undefined);
    assert.equal(probeEnv.UNRELATED_SECRET, undefined);
  }
});

test('native probe failure remains sanitized when child stderr is sensitive', async () => {
  const root = await mkdtemp(join(tmpdir(), 'pi-probe-sanitized-'));
  const sessionFile = join(root, 'target.jsonl');
  await writeFile(sessionFile, '');
  await assert.rejects(
    captureNativePiUsageSnapshot({
      env: environment(root), sessionFile, lifecycleState: 'open',
      runner: async () => { throw new Error('private native stderr and credential'); },
      sclRunner,
    }),
    error => error.message === 'Pi host usage probe failed'
      && !error.message.includes('private') && !error.message.includes('credential'),
  );
});

test('canonical SCL readback is mandatory before an open baseline succeeds', async () => {
  const root = await mkdtemp(join(tmpdir(), 'pi-usage-extension-'));
  const sessionFile = join(root, 'target.jsonl');
  await writeFile(sessionFile, '');
  await assert.rejects(
    captureNativePiUsageSnapshot({
      env: environment(root),
      sessionFile,
      lifecycleState: 'open',
      runner: async () => ({ stdout: JSON.stringify(snapshot('open')), stderr: '' }),
      sclRunner: async () => ({
        stdout: JSON.stringify({ schema_version: 'scl_response/v1', status: 'rejected' }),
        stderr: '',
      }),
    }),
    /did not match runtime authority/,
  );

  const incomplete = environment(root);
  delete incomplete.PULSE_COMPUTE_COST_SCL_KERNEL_ROOT;
  await assert.rejects(
    captureNativePiUsageSnapshot({
      env: incomplete,
      sessionFile: '/private/pi/target.jsonl',
      lifecycleState: 'open',
      runner: async () => ({ stdout: JSON.stringify(snapshot('open')), stderr: '' }),
      sclRunner,
    }),
    /canonical SCL measurement binding is unavailable/,
  );
});

test('canonical SCL readback accepts resident Harness execution with a native Pi producer', async () => {
  const root = await mkdtemp(join(tmpdir(), 'pi-usage-extension-'));
  const sessionFile = join(root, 'target.jsonl');
  await writeFile(sessionFile, '');
  const receipt = await captureNativePiUsageSnapshot({
    env: environment(root),
    sessionFile,
    lifecycleState: 'open',
    runner: async () => ({ stdout: JSON.stringify(snapshot('open')), stderr: '' }),
    sclRunner: (command, args, options) => sclRunner(
      command, args, options, 'resident_harness',
    ),
  });

  assert.equal(receipt.lifecycle_state, 'open');
});

test('canonical SCL readback rejects an unmapped execution runtime class', async () => {
  const root = await mkdtemp(join(tmpdir(), 'pi-usage-extension-'));
  const sessionFile = join(root, 'target.jsonl');
  await writeFile(sessionFile, '');
  await assert.rejects(
    captureNativePiUsageSnapshot({
      env: environment(root),
      sessionFile,
      lifecycleState: 'open',
      runner: async () => ({ stdout: JSON.stringify(snapshot('open')), stderr: '' }),
      sclRunner: (command, args, options) => sclRunner(
        command, args, options, 'remote_app_server',
      ),
    }),
    /did not match runtime authority/,
  );
});

test('new Pi session uses its empty in-memory header as the prospective native baseline', async () => {
  const root = await mkdtemp(join(tmpdir(), 'pi-usage-extension-'));
  const absentSessionFile = join(root, 'not-yet-persisted.jsonl');
  const header = {
    type: 'session',
    version: 3,
    id: 'host-owned-session-id',
    timestamp: '2026-08-25T01:00:00.000Z',
    cwd: '/private/project',
  };
  let probeFile;
  const receipt = await capturePiUsageSnapshot({
    env: environment(root),
    sessionFile: absentSessionFile,
    lifecycleState: 'open',
    measurementRole: 'baseline',
    initialSessionState: { header, session_id: header.id, entries: [] },
    runner: async (_command, _args, options) => {
      probeFile = options.env.PI_SESSION_FILE;
      assert.notEqual(probeFile, absentSessionFile);
      assert.equal(await readFile(probeFile, 'utf8'), `${JSON.stringify(header)}\n`);
      return { stdout: JSON.stringify(snapshot('open', {
        schema: 'pi_native_usage_summary/v1',
        event_count: 0,
        source_prefix_sha256: createHash('sha256').update(`${JSON.stringify(header)}\n`).digest('hex'),
        source_bytes: Buffer.byteLength(`${JSON.stringify(header)}\n`),
      })), stderr: '' };
    },
  });

  assert.equal(receipt.lifecycle_state, 'open');
  await assert.rejects(readFile(probeFile, 'utf8'), /ENOENT/);
});

test('new Pi session baseline includes only its pre-turn model and thinking configuration', async () => {
  const root = await mkdtemp(join(tmpdir(), 'pi-usage-extension-'));
  const header = {
    type: 'session', version: 3, id: 'target',
    timestamp: '2026-08-25T01:00:00.000Z', cwd: '/private/project',
  };
  const entries = [
    {
      type: 'model_change', id: 'model-1', parentId: null,
      timestamp: '2026-08-25T01:00:00.001Z',
      provider: 'openai-codex', modelId: 'gpt-5.6-sol',
    },
    {
      type: 'thinking_level_change', id: 'thinking-1', parentId: 'model-1',
      timestamp: '2026-08-25T01:00:00.002Z', thinkingLevel: 'xhigh',
    },
  ];
  const source = `${[header, ...entries].map((entry) => JSON.stringify(entry)).join('\n')}\n`;
  await capturePiUsageSnapshot({
    env: environment(root),
    sessionFile: join(root, 'missing.jsonl'),
    lifecycleState: 'open',
    measurementRole: 'baseline',
    initialSessionState: { header, session_id: header.id, entries },
    runner: async (_command, _args, options) => {
      assert.equal(await readFile(options.env.PI_SESSION_FILE, 'utf8'), source);
      return { stdout: JSON.stringify(snapshot('open', {
        schema: 'pi_native_usage_summary/v1', event_count: 0,
        source_prefix_sha256: createHash('sha256').update(source).digest('hex'), source_bytes: Buffer.byteLength(source),
      })), stderr: '' };
    },
  });
});

test('missing Pi session source rejects any in-memory business or usage entry', async () => {
  const root = await mkdtemp(join(tmpdir(), 'pi-usage-extension-'));
  const input = {
    env: environment(root),
    sessionFile: join(root, 'missing.jsonl'),
    lifecycleState: 'open',
    measurementRole: 'baseline',
    runner: async () => {
      throw new Error('probe must not run');
    },
  };
  await assert.rejects(
    capturePiUsageSnapshot({
      ...input,
      initialSessionState: {
        header: {
          type: 'session', version: 3, id: 'target',
          timestamp: '2026-08-25T01:00:00.000Z', cwd: '/private/project',
        },
        session_id: 'target',
        entries: [{
          type: 'message', id: 'user-1', parentId: null,
          timestamp: '2026-08-25T01:00:00.001Z',
          message: { role: 'user', content: [{ type: 'text', text: 'private' }] },
        }],
      },
    }),
    /contains non-configuration entries/,
  );
});

test('Pi lifecycle captures target baseline before turns and seals once after final persistence', async () => {
  const handlers = new Map();
  let command;
  const entries = [];
  const captures = [];
  const pi = {
    on: (name, handler) => handlers.set(name, handler),
    appendEntry: (name, receipt) => entries.push({ name, receipt }),
    registerCommand: (_name, spec) => { command = spec; },
  };
  const ctx = {
    sessionManager: {
      getSessionFile: () => '/private/pi/target.jsonl',
      getHeader: () => ({
        type: 'session', version: 3, id: 'target',
        timestamp: '2026-08-25T01:00:00.000Z', cwd: '/private/project',
      }),
      getSessionId: () => 'target',
      getEntries: () => [],
    },
    waitForIdle: async () => {},
    hasUI: false,
  };
  installPiUsageLifecycle(pi, {
    captureSnapshot: async ({ sessionFile, lifecycleState, measurementRole, initialSessionState }) => {
      const priorSeals = captures.filter((item) => item.lifecycleState === 'sealed').length;
      captures.push({ sessionFile, lifecycleState, measurementRole, initialSessionState });
      return {
        schema: 'pi_host_usage_capture_receipt/v1',
        work_item_id: binding.work_item_id,
        epoch: binding.epoch,
        lifecycle_state: lifecycleState,
        binding_sha256: binding.binding_sha256,
        snapshot_sha256: lifecycleState === 'sealed' ? 'd'.repeat(64) : 'c'.repeat(64),
        ...(lifecycleState === 'sealed' && priorSeals > 0 ? { idempotent_replay: true } : {}),
      };
    },
  });

  await handlers.get('session_start')({}, ctx);
  await new Promise(resolve => setImmediate(resolve));
  assertNoExecutionHooks(handlers);
  handlers.get('agent_start')();
  await handlers.get('agent_settled')({}, ctx);
  await new Promise(resolve => setImmediate(resolve));
  await handlers.get('session_shutdown')({ reason: 'exit' }, ctx);
  await new Promise(resolve => setImmediate(resolve));
  await handlers.get('session_shutdown')({ reason: 'exit' }, ctx);
  await new Promise(resolve => setImmediate(resolve));
  await command.handler('', ctx);
  await command.handler('', ctx);

  assert.deepEqual(captures.map(item => item.lifecycleState), ['open', 'open', 'open', 'open', 'sealed', 'sealed']);
  assert.deepEqual(captures.map(item => item.measurementRole), ['baseline', 'progress', 'progress', 'progress', 'final', 'final']);
  assert.equal(captures[0].sessionFile, '/private/pi/target.jsonl');
  assert.equal(captures[0].initialSessionState.entries.length, 0);
  assert.equal(entries.length, 5);
  assert.equal(typeof command.handler, 'function');
});

test('measurement observes startup but does not own session replacement', async () => {
  const handlers = new Map();
  let captures = 0;
  let shutdowns = 0;
  const pi = {
    on: (name, handler) => handlers.set(name, handler),
    appendEntry: () => {},
    registerCommand: () => {},
  };
  installPiUsageLifecycle(pi, {
    captureSnapshot: async () => {
      captures += 1;
      return snapshot('open');
    },
  });
  const ctx = {
    sessionManager: {
      getSessionFile: () => '/private/pi/target.jsonl',
      getSessionId: () => 'target',
      getHeader: () => ({
        type: 'session', version: 3, id: 'target',
        timestamp: '2026-08-25T01:00:00.000Z', cwd: '/private/project',
      }),
      getBranch: () => [],
    },
    hasUI: false,
    shutdown: () => { shutdowns += 1; },
  };

  assertNoExecutionHooks(handlers);

  await handlers.get('session_start')({ reason: 'new' }, ctx);
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(captures, 1);
  assert.equal(shutdowns, 0);
  assertNoExecutionHooks(handlers);
});

test('baseline failure and retry change measurement only, never business capabilities', async () => {
  const handlers = new Map();
  const commands = new Map();
  const entries = [];
  let captures = 0;
  let shutdowns = 0;
  let aborts = 0;
  const pi = {
    on: (name, handler) => handlers.set(name, handler),
    appendEntry: (name, receipt) => entries.push({ name, receipt }),
    registerCommand: (name, spec) => commands.set(name, spec),
  };
  installPiUsageLifecycle(pi, {
    captureSnapshot: async () => {
      captures += 1;
      if (captures === 1) throw new Error('target authority not adopted yet');
      return {
        schema: 'pi_host_usage_capture_receipt/v1',
        work_item_id: binding.work_item_id,
        epoch: binding.epoch,
        lifecycle_state: 'open',
        binding_sha256: binding.binding_sha256,
        snapshot_sha256: 'c'.repeat(64),
      };
    },
  });
  const ctx = {
    sessionManager: {
      getSessionFile: () => '/private/pi/target.jsonl',
      getSessionId: () => 'target',
      getHeader: () => ({
        type: 'session', version: 3, id: 'target',
        timestamp: '2026-08-25T01:00:00.000Z', cwd: '/private/project',
      }),
      getBranch: () => [],
    },
    waitForIdle: async () => {},
    hasUI: false,
    shutdown: () => { shutdowns += 1; },
    abort: () => { aborts += 1; },
  };

  await handlers.get('session_start')({}, ctx);
  await new Promise(resolve => setImmediate(resolve));
  assertNoExecutionHooks(handlers);
  assert.equal(shutdowns, 0);
  assert.equal(aborts, 0);
  assert.equal(entries.length, 0);

  await commands.get('runtime-usage-baseline').handler('', ctx);
  assertNoExecutionHooks(handlers);
  assert.equal(captures, 2);
  assert.equal(entries.length, 1);
});

async function standaloneHostConfig(root) {
  const managedExtension = join(
    process.cwd(), 'ComputeCost/runtime/pi-usage-binding.mjs',
  );
  return {
    schema: 'pulse_standalone_pi_host_config/v1',
    pi_version: '0.84.2',
    agent_id: 'pulse',
    runtime_class: 'resident_harness',
    execution_surface: 'surface_v1_local_macos',
    procurement_source: 'unknown',
    scl_executable: '/private/harness/bin/harness-scl',
    scl_harness_root: '/private/harness-authority/.harness',
    scl_kernel_root: '/private/scl-kernel',
    python: '/usr/bin/python3',
    python_path: '/private/pulse/ComputeCost',
    snapshot_dir: join(root, 'usage'),
    toolchain_path: '/usr/bin:/bin',
    managed_extension: managedExtension,
    managed_extension_sha256: createHash('sha256').update(
      await readFile(managedExtension),
    ).digest('hex'),
  };
}

function standaloneAuthority(rawSessionId) {
  return `session_v1_${createHash('sha256').update(
    ['pulse-session-v1', rawSessionId].join('\0'),
  ).digest('hex')}`;
}

function standaloneBinding(authorityRef) {
  const unsigned = {
    schema: 'pulse_runtime_usage_binding/v3',
    work_item_id: 'wi_20260822_730',
    agent_id: 'pulse',
    phase: 'work_item',
    epoch: 12,
    runtime: 'pi',
    procurement_source: 'unknown',
    issued_at: '2026-09-08T16:00:00Z',
    authority_ref: authorityRef,
    execution_surface: 'surface_v1_local_macos',
  };
  return {
    ...unsigned,
    binding_sha256: createHash('sha256').update(canonical(unsigned)).digest('hex'),
  };
}

function assertNoExecutionHooks(handlers) {
  for (const name of ['input', 'before_agent_start', 'tool_call',
    'session_before_switch', 'session_before_fork', 'session_before_tree']) {
    assert.equal(handlers.has(name), false, `meter must not own ${name}`);
  }
}

function interactiveFixture(entries = []) {
  const handlers = new Map();
  const commands = new Map();
  const notifications = [];
  let aborts = 0;
  const pi = {
    on: (name, handler) => handlers.set(name, handler),
    registerCommand: (name, spec) => commands.set(name, spec),
    appendEntry: (customType, data) => {
      const prior = entries.at(-1)?.id ?? null;
      entries.push({
        type: 'custom', id: `custom-${entries.length + 1}`, parentId: prior,
        timestamp: '2026-09-08T16:00:01.000Z', customType, data,
      });
    },
  };
  const ctx = {
    cwd: '/private/registered-worktree',
    hasUI: true,
    ui: { notify: (message, level) => notifications.push({ message, level }) },
    sessionManager: {
      getSessionFile: () => '/private/raw-private-session.jsonl',
      getSessionId: () => 'raw-private-session-id',
      getHeader: () => ({
        type: 'session', version: 3, id: 'raw-private-session-id',
        timestamp: '2026-09-08T16:00:00.000Z', cwd: '/private/registered-worktree',
      }),
      getEntries: () => [...entries],
      getBranch: () => [...entries],
    },
    waitForIdle: async () => {},
    abort: () => { aborts += 1; },
  };
  return { pi, ctx, handlers, commands, notifications, entries, aborts: () => aborts };
}

for (const mode of ['reviewed', 'unreviewed', 'restart', 'missing-source']) {
  test(`retired standalone meter never enters a WI or changes saved history: ${mode}`, async () => {
    const root = await mkdtemp(join(tmpdir(), 'pi-standalone-extension-'));
    const config = await standaloneHostConfig(root);
    if (mode === 'unreviewed') config.pi_version = 'latest';
    if (mode === 'missing-source') config.scl_executable = '/missing';
    const entries = mode === 'restart' ? [{
      type: 'custom', id: 'binding-1', parentId: null,
      timestamp: '2026-09-08T16:00:01.000Z', customType: standaloneBindingEntry,
      data: {schema: 'pulse_standalone_pi_binding/v1',
        binding: standaloneBinding(standaloneAuthority('raw-private-session-id'))},
    }] : [];
    const before = JSON.stringify(entries);
    const fixture = interactiveFixture(entries);
    installPiUsageLifecycle(fixture.pi, {
      standaloneConfig: config,
      standaloneRunner: () => assert.fail('meter must not invoke a lifecycle controller'),
      captureSnapshot: () => assert.fail('retired admission is not a measurement source'),
    });
    await fixture.handlers.get('session_start')({}, fixture.ctx);
    assertNoExecutionHooks(fixture.handlers);
    assert.equal(fixture.commands.has('wi'), false);
    assert.match(fixture.notifications.at(-1).message, /retired/);
    await fixture.commands.get('runtime-usage-baseline').handler('', fixture.ctx);
    assert.match(fixture.notifications.at(-1).message, /measurement_authority_unavailable/);
    assert.equal(JSON.stringify(entries), before);
    assert.equal(JSON.stringify(fixture.notifications).includes('raw-private'), false);
    assert.equal(fixture.aborts(), 0);
  });
}

test('sealed capture is durable, restart-idempotent, and concurrent single-winner', async () => {
  const root = await mkdtemp(join(tmpdir(), 'pi-usage-extension-'));
  const sessionFile = join(root, 'target.jsonl');
  const source = sessionSource('sealed-prefix');
  await writeFile(sessionFile, source);
  await persistBaseline(root);
  const sealedSnapshot = snapshot('sealed', {
    event_count: 1,
    source_bytes: Buffer.byteLength(source),
    source_prefix_sha256: createHash('sha256').update(source).digest('hex'),
  });
  let runnerCalls = 0;
  const runner = async () => {
    runnerCalls += 1;
    await new Promise((resolve) => setTimeout(resolve, 25));
    return { stdout: JSON.stringify(sealedSnapshot), stderr: '' };
  };
  const input = {
    env: environment(root),
    sessionFile,
    lifecycleState: 'sealed',
    runner,
  };

  const [winner, replay] = await Promise.all([
    capturePiUsageSnapshot(input),
    capturePiUsageSnapshot(input),
  ]);
  const afterRestart = await capturePiUsageSnapshot(input);

  assert.equal(runnerCalls, 1);
  assert.equal([winner, replay].filter((item) => item.idempotent_replay === true).length, 1);
  assert.equal(afterRestart.idempotent_replay, true);
  assert.equal(afterRestart.snapshot_sha256, winner.snapshot_sha256);
  const path = join(root, `${binding.work_item_id}-${binding.binding_sha256}-sealed.json`);
  assert.deepEqual(JSON.parse(await readFile(path, 'utf8')), sealedSnapshot);
});

test('sealed replay excludes an appended suffix and fails closed on a changed sealed prefix', async () => {
  const root = await mkdtemp(join(tmpdir(), 'pi-usage-extension-'));
  const sessionFile = join(root, 'target.jsonl');
  const source = sessionSource('sealed-replay');
  await writeFile(sessionFile, source);
  await persistBaseline(root);
  const current = snapshot('sealed', {
    event_count: 1,
    source_bytes: Buffer.byteLength(source),
    source_prefix_sha256: createHash('sha256').update(source).digest('hex'),
  });
  const input = {
    env: environment(root),
    sessionFile,
    lifecycleState: 'sealed',
    runner: async () => ({ stdout: JSON.stringify(current), stderr: '' }),
  };
  await capturePiUsageSnapshot(input);
  await writeFile(sessionFile, `${source}post-seal\n`);
  assert.equal((await capturePiUsageSnapshot(input)).idempotent_replay, true);
  await writeFile(sessionFile, `${sessionSource('changed-prefix')}post-seal\n`);

  await assert.rejects(capturePiUsageSnapshot(input), /usage changed before the final position/);
});

test('host capture rejects stale target authority, tampered binding/replay, and ambiguous binding source', async () => {
  const root = await mkdtemp(join(tmpdir(), 'pi-usage-extension-'));
  await assert.rejects(
    capturePiUsageSnapshot({
      env: {
        ...environment(root),
        PULSE_RUNTIME_USAGE_BINDING: JSON.stringify({
          ...binding,
          issued_at: '2026-02-30T01:00:00Z',
        }),
      },
      sessionFile: '/private/pi/session.jsonl',
      lifecycleState: 'open',
      runner: async () => ({ stdout: JSON.stringify(snapshot('open')), stderr: '' }),
    }),
    /binding is invalid/,
  );
  await assert.rejects(
    capturePiUsageSnapshot({
      env: {
        ...environment(root),
        PULSE_RUNTIME_AUTHORITY_REF: `session_v1_${'b'.repeat(64)}`,
      },
      sessionFile: '/private/pi/session.jsonl',
      lifecycleState: 'open',
      runner: async () => ({ stdout: JSON.stringify(snapshot('open')), stderr: '' }),
    }),
    /binding is invalid/,
  );
  await assert.rejects(
    capturePiUsageSnapshot({
      env: {
        ...environment(root),
        PULSE_RUNTIME_USAGE_BINDING: JSON.stringify({ ...binding, epoch: 13 }),
      },
      sessionFile: '/private/pi/session.jsonl',
      lifecycleState: 'open',
      runner: async () => ({ stdout: JSON.stringify(snapshot('open')), stderr: '' }),
    }),
    /binding digest does not match/,
  );
  await assert.rejects(
    capturePiUsageSnapshot({
      env: environment(root),
      sessionFile: '/private/pi/session.jsonl',
      lifecycleState: 'open',
      runner: async () => ({
        stdout: JSON.stringify({ ...snapshot('open'), epoch: 13 }),
        stderr: '',
      }),
    }),
    /does not match runtime authority/,
  );
  const sealedPath = join(root, `${binding.work_item_id}-${binding.binding_sha256}-sealed.json`);
  const sealedSessionFile = join(root, 'sealed-session.jsonl');
  await writeFile(sealedSessionFile, '');
  await persistBaseline(root);
  const valid = snapshot('sealed');
  await import('node:fs/promises').then(async ({ chmod, writeFile }) => {
    await writeFile(sealedPath, JSON.stringify({ ...valid, summary: { event_count: 999 } }));
    await chmod(sealedPath, 0o600);
  });
  await assert.rejects(
    capturePiUsageSnapshot({
      env: environment(root),
      sessionFile: sealedSessionFile,
      lifecycleState: 'sealed',
      runner: async () => ({ stdout: JSON.stringify(valid), stderr: '' }),
    }),
    /digest does not match/,
  );
  await assert.rejects(
    capturePiUsageSnapshot({
      env: {
        ...environment(root),
        PULSE_RUNTIME_USAGE_BINDING_FILE: '/private/also-present.json',
      },
      sessionFile: '/private/pi/session.jsonl',
      lifecycleState: 'open',
      runner: async () => ({ stdout: '{}', stderr: '' }),
    }),
    /exactly one/,
  );
});

for (const canonicalAccepted of [false, true]) {
  test(`volatile origin migration preserves artifacts and canonical CAS (${canonicalAccepted})`, async () => {
    const root = await mkdtemp(join(tmpdir(), 'pi-origin-recovery-'));
    const env = environment(root);
    const target = join(root, `${binding.work_item_id}-${binding.binding_sha256}-baseline.json`);
    const old = snapshot('open');
    const oldPosition = {source_bytes: 495, source_prefix_sha256: '9'.repeat(64), last_entry_ref: null};
    old.attribution_boundary.baseline = old.attribution_boundary.final = oldPosition;
    delete old.snapshot_sha256;
    old.snapshot_sha256 = createHash('sha256').update(canonical(old)).digest('hex');
    const original = JSON.stringify(old);
    await writeFile(target, original, {mode:0o600});
    const header = {type:'session', version:3, id:'same-session', timestamp:'2026-09-10T05:49:45.705Z', cwd:'/private/work'};
    let records = 0;
    const input = {env, lifecycleState:'open', measurementRole:'baseline', sessionFile:join(root,'missing.jsonl'), initialSessionState:{header,session_id:header.id,entries:[]},
      runner:async (_cmd,_args,options)=>{
        assert.equal(options.env.PULSE_PI_USAGE_UNPERSISTED_BASELINE, '1');
        return {stdout:JSON.stringify(snapshot('open')),stderr:''};
      }, sclRunner:async (...args)=>{records++;if(canonicalAccepted)throw new Error('measurement_fact_conflict');return sclRunner(...args);}};
    if(canonicalAccepted){
      await assert.rejects(captureNativePiUsageSnapshot(input), /measurement_transport_unavailable/);
      assert.equal(await readFile(target,'utf8'), original);
    }else{
      const first = await captureNativePiUsageSnapshot(input);
      assert.equal(first.snapshot_sha256, snapshot('open').snapshot_sha256);
      input.initialSessionState.header.timestamp = '2026-09-10T06:00:00.000Z';
      input.initialSessionState.entries = [{type:'custom',id:'receipt',timestamp:header.timestamp,customType:'pulse-computecost-usage',data:first}];
      const replay = await captureNativePiUsageSnapshot(input);
      assert.equal(replay.idempotent_replay,true);
      assert.equal(replay.snapshot_sha256, first.snapshot_sha256);
      assert.equal(records,2);
    }
    assert.equal(await readFile(`${target}.superseded-${old.snapshot_sha256}.json`,'utf8'),original);
    await assert.rejects(stat(input.sessionFile), /ENOENT/);
  });
}


test('real native probe publishes one private final through host transport', async () => {
  const root = await mkdtemp(join(tmpdir(), 'pi-native-capture-'));
  const sessionFile = join(root, 'session.jsonl');
  const source = sessionSource('native-transport');
  await writeFile(sessionFile, source, {mode: 0o600});
  // The probe intentionally strips loader environment overrides. CI can bind
  // an actual standalone interpreter rather than a relocated toolcache build.
  const pythonCommand = process.env.PULSE_COMPUTE_COST_PYTHON ?? 'python3';
  const python = (await promisify(execFile)(pythonCommand, ['-c', 'import sys; print(sys.executable)'])).stdout.trim();
  const env = {...environment(root), PATH: process.env.PATH,
    LD_LIBRARY_PATH: process.env.LD_LIBRARY_PATH,
    PULSE_COMPUTE_COST_PYTHON: python,
    PULSE_COMPUTE_COST_PYTHONPATH: fileURLToPath(new URL('../', import.meta.url))};
  const input = {env, sessionFile, sclRunner};
  await captureAtomicPiUsageSnapshot({...input, lifecycleState: 'open', measurementRole: 'baseline'});
  await writeFile(sessionFile, source + JSON.stringify({type: 'message', id: 'native-final',
    timestamp: '2026-08-25T01:00:01.000Z', message: {role: 'assistant',
      provider: 'openai-codex', model: 'gpt-fixture', usage: {
        input: 100, cacheRead: 50, cacheWrite: 0, output: 5, totalTokens: 155,
      }}}) + '\n');
  const results = await Promise.all([0, 1].map(() => captureAtomicPiUsageSnapshot({
    ...input, lifecycleState: 'sealed', measurementRole: 'final'})));
  assert.equal(results[0].snapshot_sha256, results[1].snapshot_sha256);
  assert.equal(results.filter(item => item.idempotent_replay).length, 1);
  const replay = await captureAtomicPiUsageSnapshot({...input, lifecycleState: 'sealed', measurementRole: 'final'});
  assert.equal(replay.idempotent_replay, true);
  const artifact = join(root, replay.artifact_ref);
  assert.equal((await stat(artifact)).mode & 0o777, 0o600);
  assert.equal(JSON.parse(await readFile(artifact, 'utf8')).summary.totals.total_tokens, 155);
});

test('sealed measurement retains all artifacts without restricting execution', async () => {
  const root = await mkdtemp(join(tmpdir(), 'pi-sealed-source-'));
  const sessionFile = join(root, 'source.jsonl'); await writeFile(sessionFile, '');
  await persistBaseline(root);
  const final = snapshot('sealed');
  const finalPath = join(root, `${binding.work_item_id}-${binding.binding_sha256}-sealed.json`);
  await writeFile(finalPath, JSON.stringify(final), {mode:0o600});
  const baselinePath = join(root, `${binding.work_item_id}-${binding.binding_sha256}-baseline.json`);
  const before = await readFile(baselinePath,'utf8');
  const sealedRunner = async (...args) => {
    const response=await sclRunner(...args); const parsed=JSON.parse(response.stdout);
    Object.assign(parsed.result.measurement,{status:'sealed', authority_ref:binding.authority_ref,
      runtime_binding_sha256:binding.binding_sha256,final_sha256:final.snapshot_sha256,seal_sha256:'d'.repeat(64)});
    return {...response,stdout:JSON.stringify(parsed)};
  };
  const capture = input => captureAtomicPiUsageSnapshot({...input,env:environment(root),sclRunner:sealedRunner});
  const receipt = await capture({sessionFile,lifecycleState:'open'});
  assert.equal(receipt.canonical_lifecycle_state,'sealed');assert.equal(receipt.idempotent_replay,true);
  assert.equal(await readFile(baselinePath,'utf8'),before);
  const handlers=new Map(),commands=new Map();const notices=[];
  installPiUsageLifecycle({on:(name,fn)=>handlers.set(name,fn),registerCommand:(name,c)=>commands.set(name,c),
    appendEntry:()=>assert.fail('retirement replay must not append usage')},{captureSnapshot:capture});
  const ctx={sessionManager:{getSessionFile:()=>sessionFile},waitForIdle:async()=>{},hasUI:true,
    ui:{notify:message=>notices.push(message)},abort:()=>{}};
  await handlers.get('session_start')({},ctx);
  await new Promise(resolve => setImmediate(resolve));
  await commands.get('runtime-usage-baseline').handler('',ctx);
  assertNoExecutionHooks(handlers);
  await commands.get('runtime-usage-seal').handler('',ctx);
  assert.match(notices.at(-1),/PULSE_USAGE_SEAL\/v1.*"ok":true/);
  assert.equal(await readFile(finalPath,'utf8'),JSON.stringify(final));
  for(const patch of [{status:'open'},{authority_ref:'wrong'},{runtime_binding_sha256:'0'.repeat(64)},
    {final_sha256:'0'.repeat(64)},{seal_sha256:null}]) {
    await assert.rejects(captureAtomicPiUsageSnapshot({env:environment(root),sessionFile,lifecycleState:'open',
      sclRunner:async(...args)=>{const r=await sealedRunner(...args);const p=JSON.parse(r.stdout);
        Object.assign(p.result.measurement,patch);return {...r,stdout:JSON.stringify(p)};}}),/did not match runtime authority/);
  }
  const {unlink}=await import('node:fs/promises');await unlink(finalPath);
  await assert.rejects(capture({sessionFile,lifecycleState:'open'}),/did not match runtime authority/);
});


test('a hung capture cannot hold startup, input, tools or model reply', async () => {
  const handlers = new Map();
  const commands = new Map();
  let captures = 0;
  installPiUsageLifecycle({on:(key,fn)=>handlers.set(key,fn),
    appendEntry:()=>assert.fail('no receipt was returned'), registerCommand:(key,fn)=>commands.set(key,fn)},
    {captureSnapshot:()=>{captures++;return new Promise(()=>{});}});
  const ctx={hasUI:false,waitForIdle:async()=>{},abort:()=>assert.fail('conversation must not abort'),
    sessionManager:{getSessionFile:()=>'/private/original.jsonl'}};
  await handlers.get('session_start')({},ctx);
  await new Promise(resolve=>setImmediate(resolve));
  assert.equal(captures,1);
  void commands.get('runtime-usage-baseline').handler('',ctx);
  assertNoExecutionHooks(handlers);
  await handlers.get('agent_settled')({},ctx);
  assert.equal(captures,1,'no duplicate capture while the original command is unresolved');
});


test('the meter cannot project tools, even after successful capture', async () => {
  const previous=process.env.PULSE_PI_MEASUREMENT_TOOLS;
  process.env.PULSE_PI_MEASUREMENT_TOOLS=JSON.stringify(['read','bash','pulse_wi_continue']);
  const handlers=new Map(),commands=new Map();let active=['read','bash','pulse_work_item'];let resolveCapture;
  try{
    installPiUsageLifecycle({on:(name,fn)=>handlers.set(name,fn),registerCommand:(name,fn)=>commands.set(name,fn),
      appendEntry:()=>{},getAllTools:()=>['read','bash','external_upload','pulse_wi_continue'].map(name=>({name})),
      setActiveTools:names=>{active=names;}},{captureSnapshot:()=>new Promise(resolve=>{resolveCapture=resolve;})});
    const ctx={sessionManager:{getSessionFile:()=>'/private/original.jsonl'},hasUI:false,waitForIdle:async()=>{}};
    await handlers.get('session_start')({},ctx);
    await new Promise(resolve=>setImmediate(resolve));
    assert.deepEqual(active,['read','bash','pulse_work_item']);
    resolveCapture({schema:'pi_host_usage_capture_receipt/v1',lifecycle_state:'open'});
    await new Promise(resolve=>setImmediate(resolve));
    assert.deepEqual(active,['read','bash','pulse_work_item']);
    assert.equal(active.includes('external_upload'),false);
  }finally{if(previous===undefined)delete process.env.PULSE_PI_MEASUREMENT_TOOLS;else process.env.PULSE_PI_MEASUREMENT_TOOLS=previous;}
});

for (const mode of ['pending', 'failed', 'sealed']) {
  test(`metering ${mode} leaves Host tools unchanged for legacy and native sessions`, async () => {
    const handlers = new Map(), commands = new Map(), entries = [];
    const tools = ['read', 'bash', 'edit', 'write', 'pulse_work_item', 'pulse_wi_continue'];
    const active = [...tools];
    installPiUsageLifecycle({
      on: (name, handler) => handlers.set(name, handler),
      registerCommand: (name, command) => commands.set(name, command),
      appendEntry: (_name, receipt) => entries.push(receipt),
      getAllTools: () => [...tools, 'external_upload'].map(name => ({name})),
      setActiveTools: () => assert.fail('measurement must not change Host tool selection'),
    }, {captureSnapshot: async ({lifecycleState}) => {
      if (mode === 'pending') return new Promise(() => {});
      if (mode === 'failed') throw new Error('SCL measurement unavailable');
      return {schema:'pi_host_usage_capture_receipt/v1', lifecycle_state:lifecycleState};
    }});
    const ctx = {hasUI:false, waitForIdle:async()=>{},
      sessionManager:{getSessionFile:()=>'/private/original.jsonl'}};
    await handlers.get('session_start')({}, ctx);
    await new Promise(resolve => setImmediate(resolve));
    if (mode === 'sealed') await commands.get('runtime-usage-seal').handler('', ctx);
    else void commands.get('runtime-usage-baseline').handler('', ctx);
    for (const toolName of tools) {
      assert.equal(await handlers.get('tool_call')?.({toolName}, ctx), undefined);
    }
    assertNoExecutionHooks(handlers);
    assert.deepEqual(active, tools);
    assert.equal(active.includes('external_upload'), false);
    if (mode !== 'sealed') assert.deepEqual(entries, []);
  });
}

test('Host conversation-only admission still forbids measurement capture', async () => {
  const previous=process.env.PULSE_PI_CONVERSATION_ONLY;
  process.env.PULSE_PI_CONVERSATION_ONLY='1';
  const handlers=new Map(),commands=new Map();
  try {
    installPiUsageLifecycle({on:(name,fn)=>handlers.set(name,fn),registerCommand:(name,fn)=>commands.set(name,fn),
      appendEntry:()=>assert.fail('no authority to record'),
      setActiveTools:()=>assert.fail('Host owns its no-tools admission')},
      {captureSnapshot:()=>assert.fail('unverified process cannot record a baseline')});
    const ctx={sessionManager:{getSessionFile:()=>'/private/original.jsonl'},hasUI:false,waitForIdle:async()=>{}};
    await handlers.get('session_start')({},ctx);
    await commands.get('runtime-usage-baseline').handler('',ctx);
  } finally {
    if(previous===undefined)delete process.env.PULSE_PI_CONVERSATION_ONLY;
    else process.env.PULSE_PI_CONVERSATION_ONLY=previous;
  }
});

test('reattaches a replaced v3 binding to its accepted origin using read-only canonical proof and real native boundaries',async()=>{
 const root=await mkdtemp(join(tmpdir(),'pi-canonical-origin-'));const sessionFile=join(root,'original.jsonl');
 const source=sessionSource('original-native-session');await writeFile(sessionFile,source,{mode:0o600});
 const pythonCommand=process.env.PULSE_COMPUTE_COST_PYTHON??'python3';
 const python=(await promisify(execFile)(pythonCommand,['-c','import sys; print(sys.executable)'])).stdout.trim();
 const env={...environment(root),PATH:process.env.PATH,LD_LIBRARY_PATH:process.env.LD_LIBRARY_PATH,
   PULSE_COMPUTE_COST_PYTHON:python,PULSE_COMPUTE_COST_PYTHONPATH:fileURLToPath(new URL('../',import.meta.url))};
 const original=await captureAtomicPiUsageSnapshot({env,sessionFile,lifecycleState:'open',sclRunner});
 const baselinePath=join(root,original.artifact_ref);const before=await readFile(baselinePath,'utf8');
 const freshUnsigned={...unsignedBinding,issued_at:'2026-09-22T12:21:06.265Z'};
 const active={...freshUnsigned,binding_sha256:createHash('sha256').update(canonical(freshUnsigned)).digest('hex')};
 const origin={schema:'pulse_canonical_measurement_baseline_reference/v1',status:'open',work_item_id:binding.work_item_id,
   agent_id:binding.agent_id,epoch:binding.epoch,phase:binding.phase,runtime:'pi',procurement_source:binding.procurement_source,
   authority_ref:binding.authority_ref,execution_surface:binding.execution_surface,
   measurement_epoch_ref:'measurement_epoch_v1_'+'c'.repeat(64),baseline_binding_sha256:binding.binding_sha256,
   baseline_sha256:original.snapshot_sha256,active_runtime_binding_sha256:active.binding_sha256};
 const activeEnv={...env,PULSE_RUNTIME_USAGE_BINDING:JSON.stringify(active),PULSE_CANONICAL_MEASUREMENT_BASELINE_REFERENCE:JSON.stringify(origin)};
 let canonicalStatus='open';const commands=[];
 const canonicalRunner=async(command,args,options)=>{
   commands.push(args[0]);
   if(args[0]==='describe-measurement-local')return{stdout:JSON.stringify({schema_version:'scl_response/v1',status:'read_only',result:{measurement:{
     schema_version:'scl_measurement_epoch/v1',status:canonicalStatus,work_item_id:binding.work_item_id,agent_id:binding.agent_id,
     execution_epoch:binding.epoch,phase:binding.phase,runtime:'pi',procurement_source:binding.procurement_source,
     authority_ref:binding.authority_ref,host_session_ref:binding.authority_ref,execution_surface:binding.execution_surface,
     measurement_epoch_ref:origin.measurement_epoch_ref,runtime_binding_sha256:binding.binding_sha256,baseline_sha256:origin.baseline_sha256,
     final_sha256:null,seal_ref:null,seal_sha256:null}}}),stderr:''};
   const r=await sclRunner(command,args,options);const p=JSON.parse(r.stdout);Object.assign(p.result.measurement,{
     runtime:'pi',procurement_source:binding.procurement_source,authority_ref:binding.authority_ref,
     runtime_binding_sha256:binding.binding_sha256,measurement_epoch_ref:origin.measurement_epoch_ref});
   if(canonicalStatus==='sealed'){
     const finalSnapshot=JSON.parse(await readFile(join(root,`${binding.work_item_id}-${binding.binding_sha256}-sealed.json`),'utf8'));
     Object.assign(p.result.measurement,{status:'sealed',final_sha256:finalSnapshot.snapshot_sha256,seal_sha256:'d'.repeat(64)});
   }
   return{...r,stdout:JSON.stringify(p)};
 };
 const msg=(id,count)=>JSON.stringify({type:'message',id,timestamp:'2026-09-22T12:26:00.000Z',message:{role:'assistant',
   provider:'openai-codex',model:'gpt-fixture',usage:{input:count,output:0,cacheRead:0,cacheWrite:0,totalTokens:count}}})+'\n';
 await writeFile(sessionFile,source+msg('isolated',17));
 const reopened=await captureAtomicPiUsageSnapshot({env:activeEnv,sessionFile,lifecycleState:'open',sclRunner:canonicalRunner,
   runner:async()=>assert.fail('reattachment must not regenerate baseline')});
 assert.equal(reopened.binding_sha256,binding.binding_sha256);assert.equal(reopened.active_runtime_binding_sha256,active.binding_sha256);
 assert.equal(reopened.snapshot_sha256,original.snapshot_sha256);assert.deepEqual(commands,['describe-measurement-local']);
 canonicalStatus='sealed';await assert.rejects(captureAtomicPiUsageSnapshot({env:activeEnv,sessionFile,lifecycleState:'open',sclRunner:canonicalRunner}),/measurement_authority_conflict/);
 canonicalStatus='open';await writeFile(sessionFile,sessionSource('foreign-native-session')+msg('isolated',17));
 await assert.rejects(captureAtomicPiUsageSnapshot({env:activeEnv,sessionFile,lifecycleState:'open',sclRunner:canonicalRunner}),/changed before the baseline/);
 await writeFile(sessionFile,source+msg('isolated',17)+msg('canonical',23));
 const final=await captureAtomicPiUsageSnapshot({env:activeEnv,sessionFile,lifecycleState:'sealed',sclRunner:canonicalRunner});
 assert.equal(final.binding_sha256,binding.binding_sha256);assert.equal(final.active_runtime_binding_sha256,active.binding_sha256);
 const finalSnapshot=JSON.parse(await readFile(join(root,final.artifact_ref),'utf8'));
 assert.equal(finalSnapshot.summary.totals.total_tokens,40);assert.equal(finalSnapshot.attribution_boundary.baseline_snapshot_sha256,original.snapshot_sha256);
 assert.equal(await readFile(baselinePath,'utf8'),before);assert.equal(activeEnv.PULSE_RUNTIME_USAGE_BINDING,JSON.stringify(active));
 canonicalStatus='sealed';
 const retirement=await captureAtomicPiUsageSnapshot({env:activeEnv,sessionFile,lifecycleState:'open',sclRunner:canonicalRunner});
 assert.equal(retirement.canonical_lifecycle_state,'sealed');assert.equal(retirement.binding_sha256,binding.binding_sha256);
 assert.equal(retirement.active_runtime_binding_sha256,active.binding_sha256);assert.equal(await readFile(baselinePath,'utf8'),before);
});
