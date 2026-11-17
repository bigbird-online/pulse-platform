import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { EventEmitter } from 'node:events';
import { PassThrough } from 'node:stream';
import { mkdtempSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { createPool } from '../src/pool.js';
import { relayHostSessionRef } from '../src/session-identity.js';

const { spawnMock } = vi.hoisted(() => ({ spawnMock: vi.fn() }));
vi.mock('node:child_process', async (original) => ({
  ...await original<typeof import('node:child_process')>(),
  spawn: spawnMock,
}));

function child() {
  const value = Object.assign(new EventEmitter(), {
    stdin: new PassThrough(), stdout: new PassThrough(), stderr: new PassThrough(),
    pid: 1234567, killed: false,
    kill: vi.fn((signal?: string) => { value.killed = true; value.emit('exit', 0, signal); return true; }),
  });
  return value;
}

describe('Relay CC Host environment', () => {
  let home: string;
  let pool: ReturnType<typeof createPool>;
  beforeEach(() => {
    home = mkdtempSync(join(tmpdir(), 'cc-host-context-'));
    vi.stubEnv('HOME', home);
    vi.stubEnv('SCL_HUMAN_DECISION_KEY_FILE', join(home, 'host/key'));
    vi.stubEnv('PULSE_COMPUTECOST_RECEIPT_DIR', '');
    vi.stubEnv('PULSE_SCL_SURFACE_ID', 'surface_v1_fixture');
    vi.stubEnv('PULSE_SCL_RUNTIME_CLASS', 'resident_harness');
    vi.stubEnv('SCL_SESSION_REF', 'session_v1_' + 'a'.repeat(64));
    vi.stubEnv('SCL_RELAY_HOST_CONTEXT', 'stale-parent-context');
    vi.stubEnv('HARNESS_SURFACE_ID', 'stale-surface');
    vi.stubEnv('HARNESS_RUNTIME_CLASS', 'stale-runtime');
    spawnMock.mockReset();
    spawnMock.mockImplementation(() => child());
    pool = createPool({ cleanupIntervalMs: 999999, ccMode: 'print' });
  });
  afterEach(async () => {
    await pool.killAll();
    pool.dispose();
    vi.unstubAllEnvs();
    vi.restoreAllMocks();
    rmSync(home, { recursive: true, force: true });
  });

  async function spawn(sessionId: string, ccMode: 'print' | 'interactive' = 'print', tenantId = 'fixture') {
    const pending = pool.spawn({ tenantId, sessionId, workspace: home, ccMode });
    const process = spawnMock.mock.results.at(-1)!.value;
    process.stdout.write('{"type":"system","subtype":"init","content":"ready"}\n');
    await pending;
    return spawnMock.mock.calls.at(-1)![2].env as NodeJS.ProcessEnv;
  }

  it.each(['print', 'interactive'] as const)('provides only the current session ref for %s CC', async (mode) => {
    const env = await spawn('cc-current-session', mode);
    expect(env.SCL_SESSION_REF).toBe(relayHostSessionRef('cc-current-session'));
    expect(env.SCL_RELAY_HOST_CONTEXT).toBeUndefined();
    expect(env.HARNESS_SURFACE_ID).toBeUndefined();
    expect(env.HARNESS_RUNTIME_CLASS).toBeUndefined();
  });

  it('keeps concurrent sessions separate and refreshes a replacement spawn', async () => {
    const first = await spawn('first', 'print', 'one');
    const second = await spawn('second', 'print', 'two');
    expect(first.SCL_SESSION_REF).toBe(relayHostSessionRef('first'));
    expect(second.SCL_SESSION_REF).toBe(relayHostSessionRef('second'));
    await pool.kill('one');
    const replacement = await spawn('replacement', 'print', 'one');
    expect(replacement.SCL_SESSION_REF).toBe(relayHostSessionRef('replacement'));
    expect(process.env.SCL_RELAY_HOST_CONTEXT).toBe('stale-parent-context');
  });

  it('loads no lifecycle MCP server without the installed native core', async () => {
    await spawn('tool-config');
    const args = spawnMock.mock.calls.at(-1)![1] as string[];
    const index = args.indexOf('--mcp-config');
    if (index >= 0) expect(JSON.parse(args[index + 1]).mcpServers.pulse).toBeUndefined();
    expect(args.join(' ')).not.toContain('pulse_wi_continue');
  });
});

// Pool behavior is isolated from OS process ownership, tested with real processes separately.
vi.mock('../src/owned-process.js', async () => ({
  spawnOwnedProcess: (await import('node:child_process')).spawn,
}));
