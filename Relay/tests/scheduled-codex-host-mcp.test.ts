import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { EventEmitter } from 'node:events';
import { PassThrough } from 'node:stream';
import { mkdtempSync, realpathSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { createCodexBatchRuntimeAdapter } from '../src/runtime.js';
import { codexCoreArguments, REQUEST_PURPOSE_SERVER } from '../src/native-core-runtime.js';

const fixture = vi.hoisted(() => ({
  data: undefined as string | undefined,
  core: undefined as { extension: string; python: string; entrypoint: string } | undefined,
}));
const { spawnMock } = vi.hoisted(() => ({ spawnMock: vi.fn() }));

vi.mock('../src/managed-paths.js', async (original) => ({
  ...await original<typeof import('../src/managed-paths.js')>(),
  managedRelayPath: (key: string, selected?: string) => (key === 'dataDir' ? fixture.data : selected),
}));
vi.mock('../src/native-work-item-command.js', async (original) => ({
  ...await original<typeof import('../src/native-work-item-command.js')>(),
  nodeWorkItemCore: () => fixture.core,
}));
// The scheduled session proof and its capture are covered elsewhere.
vi.mock('../src/native-core-runtime.js', async (original) => ({
  ...await original<typeof import('../src/native-core-runtime.js')>(),
  observeScheduledSession: () => undefined,
}));
vi.mock('../src/collaboration-capture.js', () => ({ captureScheduledSession: () => undefined }));
vi.mock('node:child_process', async (original) => ({
  ...await original<typeof import('node:child_process')>(),
  spawn: spawnMock,
}));
vi.mock('../src/owned-process.js', async () => ({
  spawnOwnedProcess: (await import('node:child_process')).spawn,
}));

function child() {
  const value = Object.assign(new EventEmitter(), {
    stdin: new PassThrough(), stdout: new PassThrough(), stderr: new PassThrough(),
    pid: 1234567, killed: false,
    kill: vi.fn((signal?: string) => { value.killed = true; value.emit('exit', 0, signal); return true; }),
  });
  value.stdin.on('finish', () => setImmediate(() => {
    value.stdout.end(); value.emit('exit', 0, null); value.emit('close', 0, null);
  }));
  return value;
}

/** Codex `-c key=value` overrides per MCP server, in order. */
function servers(args: string[]): Record<string, Record<string, unknown>> {
  const result: Record<string, Record<string, unknown>> = {};
  args.forEach((arg, index) => {
    if (arg !== '-c') return;
    const match = /^mcp_servers\.([^.]+)\.([^=]+)=(.*)$/.exec(args[index + 1]);
    if (match) (result[match[1]] ??= {})[match[2]] = JSON.parse(match[3]);
  });
  return result;
}

describe('scheduled Codex runs get the conversation Host MCP servers', () => {
  let home: string;
  beforeEach(() => {
    home = realpathSync(mkdtempSync(join(tmpdir(), 'scheduled-codex-mcp-')));
    vi.stubEnv('HOME', home);
    vi.stubEnv('CODEX_BIN', '/fixture/codex');
    fixture.data = join(home, 'data');
    fixture.core = { extension: '/core/runtime/pi.mjs', python: '/core/python', entrypoint: '/core/harness.pyz' };
    spawnMock.mockReset();
    spawnMock.mockImplementation(() => child());
  });
  afterEach(() => {
    vi.unstubAllEnvs();
    rmSync(home, { recursive: true, force: true });
  });

  async function runArgs(): Promise<string[]> {
    await createCodexBatchRuntimeAdapter().run({ workspace: home, prompt: 'review', timeoutMs: 5000 });
    return spawnMock.mock.calls.at(-1)![1] as string[];
  }

  it('registers declare_session_wi and pulse_work_item without a user config.toml, before exec', async () => {
    const args = await runArgs();
    const registered = servers(args);
    expect(Object.keys(registered).sort()).toEqual(['pulse', 'pulse-request-purpose']);
    expect(registered['pulse-request-purpose']).toMatchObject({
      command: '/core/python', args: [REQUEST_PURPOSE_SERVER], enabled_tools: ['declare_session_wi'],
    });
    expect(registered['pulse']).toMatchObject({
      command: process.execPath, args: [join('/core/runtime', 'cc-work-item-core.mjs')],
      enabled_tools: ['pulse_work_item', 'pulse_stats', 'pulse_jobs'],
      // A scheduled run claims no tenant: the core reads the run from its scheduler proof.
      'env.PULSE_WORK_ITEM_CORE_HOST': JSON.stringify(fixture.core),
      'env.PULSE_CORE_REQUEST_STATE_ROOT': join(fixture.data!, 'native-requests'),
    });
    expect(Object.keys(registered['pulse']).filter((key) => key.startsWith('env.')).sort())
      .toEqual(['env.PULSE_CORE_REQUEST_STATE_ROOT', 'env.PULSE_WORK_ITEM_CORE_HOST']);
    expect(args.lastIndexOf('-c')).toBeLessThan(args.indexOf('exec'));
  });

  it('matches the conversation servers apart from the tenant and jobs key the conversation names', async () => {
    const scheduled = servers(await runArgs());
    const conversation = servers(codexCoreArguments('fixture-tenant'));
    expect(conversation['pulse']['env.PULSE_PI_TENANT_ID']).toBe('fixture-tenant');
    expect(conversation['pulse']['env.PULSE_AGENT_JOBS_KEY_FILE']).toMatch(/agent-jobs-keys\/[a-f0-9]{64}\.key$/);
    delete conversation['pulse']['env.PULSE_PI_TENANT_ID'];
    delete conversation['pulse']['env.PULSE_AGENT_JOBS_KEY_FILE'];
    expect(scheduled).toEqual(conversation);
  });

  it('registers nothing without the node core or the Relay data directory', async () => {
    fixture.data = undefined;
    expect(servers(await runArgs())).toEqual({});
    fixture.data = join(home, 'data');
    fixture.core = undefined;
    expect(servers(await runArgs())).toEqual({});
  });
});
