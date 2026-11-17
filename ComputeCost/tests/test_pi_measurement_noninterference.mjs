import test from 'node:test';
import assert from 'node:assert/strict';
import {installPiUsageLifecycle} from '../runtime/pi-usage-binding.mjs';

// These are configured by the real Host, not granted by the meter.
const configured = ['read', 'bash', 'write', 'edit', 'pulse_work_item'];
const tick = () => new Promise(resolve => setImmediate(resolve));
function fixture(captureSnapshot, options = {}) {
  const handlers = new Map(), commands = new Map();
  let toolChanges = 0, active = [...configured];
  installPiUsageLifecycle({on: (name, fn) => handlers.set(name, fn),
    registerCommand: (name, value) => commands.set(name, value), appendEntry: () => {},
    getAllTools: () => [...configured, 'unconfigured_tool'].map(name => ({name})),
    setActiveTools: names => { toolChanges++; active = names; }}, {captureSnapshot, ...options});
  const ctx = {hasUI: false, waitForIdle: async () => {},
    sessionManager: {getSessionFile: () => '/synthetic/never-read.jsonl'}};
  return {handlers, commands, ctx, check: async () => {
    assert.equal(toolChanges, 0, 'measurement may neither grant nor revoke tools');
    assert.deepEqual(active, configured);
    for (const name of ['tool_call', 'before_agent_start', 'session_before_switch', 'session_before_fork', 'session_before_tree']) {
      assert.equal(handlers.has(name), false, `measurement cannot own ${name}`);
    }
    // Absence of a meter hook does not bypass a separate real Host restriction.
    const hostAllows = name => active.includes(name);
    assert.equal(hostAllows('unconfigured_tool'), false);
    assert.equal(commands.has('wi'), false, 'meter cannot be a lifecycle command writer');
  }};
}

for (const mode of ['pending', 'failure', 'ready', 'sealed', 'restart_failure', 'control_pending']) {
  test(`measurement cannot control execution: ${mode}`, async () => {
    let count = 0;
    const f = fixture(() => {
      count++;
      if (mode === 'pending' || mode === 'control_pending' && count > 1) return new Promise(() => {});
      if (mode === 'failure' || mode === 'restart_failure' && count > 1) return Promise.reject(new Error('synthetic failure'));
      return Promise.resolve({schema: 'pi_host_usage_capture_receipt/v1', lifecycle_state: 'open',
        ...(mode === 'sealed' ? {idempotent_replay: true, canonical_lifecycle_state: 'sealed'} : {})});
    });
    await f.handlers.get('session_start')({}, f.ctx); await tick();
    if (mode === 'restart_failure') { await f.handlers.get('session_start')({}, f.ctx); await tick(); }
    if (mode === 'control_pending') { void f.commands.get('runtime-usage-baseline').handler('', f.ctx); await tick(); }
    await f.check();
  });
}

test('legacy standalone configuration does not restore a second WI controller', async () => {
  const f = fixture(() => assert.fail('unsupported legacy source must not be inferred'),
    {standaloneConfig: {schema: 'old configuration is evidence, not a grant'}});
  await f.handlers.get('session_start')({}, f.ctx);
  await f.check();
});
