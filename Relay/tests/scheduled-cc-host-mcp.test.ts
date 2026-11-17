import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { EventEmitter } from 'node:events';
import { PassThrough } from 'node:stream';
import { mkdtempSync, realpathSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { basename, join } from 'node:path';
import { CC_SESSION_WI_SERVER, CC_SESSION_WI_TOOL, type CcSessionWiMcp } from '../src/cc-session-wi-mcp.js';
import { createCcBatchRuntimeAdapter, createCcInteractiveBatchRuntimeAdapter } from '../src/runtime.js';
import { ccPiState, ccPiWrites } from '../src/cc-write-boundary.js';
import { createPool } from '../src/pool.js';
import { issueAgentJobsKey } from '../src/agent-jobs-auth.js';

const fixture = vi.hoisted(() => ({
  data: undefined as string | undefined,
  core: { extension: '/core/runtime/pi.mjs', python: '/core/python', entrypoint: '/core/harness.pyz' } as
    { extension: string; python: string; entrypoint: string } | undefined,
}));
const { spawnMock } = vi.hoisted(() => ({ spawnMock: vi.fn() }));

vi.mock('../src/managed-paths.js', async (original) => ({
  ...await original<typeof import('../src/managed-paths.js')>(),
  managedRelayPath: (key: string, selected?: string) => (key === 'dataDir' ? fixture.data : selected),
}));
vi.mock('../src/native-work-item-command.js', async (original) => ({
  ...await original<typeof import('../src/native-work-item-command.js')>(),
  nodeWorkItemCore: () => fixture.core,
  readSessionAccess: () => [],
}));
// The scheduled session proof and its capture are covered elsewhere.
vi.mock('../src/native-core-runtime.js', async (original) => ({
  ...await original<typeof import('../src/native-core-runtime.js')>(),
  observeScheduledSession: () => undefined,
  observeNativeSession: () => undefined,
}));
vi.mock('../src/collaboration-capture.js', () => ({ captureScheduledSession: () => undefined }));
vi.mock('node:child_process', async (original) => ({
  ...await original<typeof import('node:child_process')>(),
  spawn: spawnMock,
}));
vi.mock('../src/owned-process.js', async () => ({
  spawnOwnedProcess: (await import('node:child_process')).spawn,
}));

const sessionWiMcp: CcSessionWiMcp = {
  server: { type: 'stdio', command: '/reviewed/python', args: ['/release/ComputeCost/runtime/codex-mcp-request-purpose.py', '--host', 'cc'] },
  instructions: 'declare_session_wi rule',
};

/** A child that exits once its input ends (print batch) or at once (interactive bridge). */
function child(exitAtOnce: boolean) {
  const value = Object.assign(new EventEmitter(), {
    stdin: new PassThrough(), stdout: new PassThrough(), stderr: new PassThrough(),
    pid: 1234567, killed: false,
    kill: vi.fn((signal?: string) => { value.killed = true; value.emit('exit', 0, signal); return true; }),
  });
  const exit = () => setImmediate(() => { value.stdout.end(); value.emit('exit', 0, null); value.emit('close', 0, null); });
  if (exitAtOnce) exit(); else value.stdin.on('finish', exit);
  return value;
}

function mcpConfig(args: string[]): { mcpServers: Record<string, any> } {
  expect(args.filter((arg) => arg === '--mcp-config')).toHaveLength(1);
  return JSON.parse(args[args.indexOf('--mcp-config') + 1]);
}

describe('scheduled CC runs get the conversation Host MCP servers', () => {
  let home: string;
  beforeEach(() => {
    home = realpathSync(mkdtempSync(join(tmpdir(), 'scheduled-cc-mcp-')));
    vi.stubEnv('HOME', home);
    vi.stubEnv('CLAUDE_BIN', '/fixture/claude');
    vi.stubEnv('RELAY_CC_OAUTH_ENV_FILE', undefined as unknown as string);
    fixture.data = join(home, 'data');
    fixture.core = { extension: '/core/runtime/pi.mjs', python: '/core/python', entrypoint: '/core/harness.pyz' };
    spawnMock.mockReset();
  });
  afterEach(() => {
    vi.unstubAllEnvs();
    rmSync(home, { recursive: true, force: true });
  });

  async function batchArgs(input: { ccWriteBoundary?: boolean; ccRepoAccess?: { allowedDomains?: string[] } } = {}, mcp: CcSessionWiMcp | null = sessionWiMcp) {
    spawnMock.mockImplementation(() => child(false));
    await createCcBatchRuntimeAdapter({ sessionWiMcp: mcp ?? undefined }).run({ workspace: home, prompt: 'review', timeoutMs: 5000, ...input });
    return spawnMock.mock.calls.at(-1)![1] as string[];
  }

  it('the CC batch registers declare_session_wi and pulse_work_item, the core bridge carrying its own environment', async () => {
    const config = mcpConfig(await batchArgs());
    expect(Object.keys(config.mcpServers).sort()).toEqual([CC_SESSION_WI_SERVER, 'pulse'].sort());
    expect(config.mcpServers[CC_SESSION_WI_SERVER]).toEqual(sessionWiMcp.server);
    const core = config.mcpServers['pulse'];
    expect(core.args[0].endsWith(join('runtime', 'cc-work-item-core.mjs'))).toBe(true);
    // A scheduled run claims no tenant: the core reads the run from its scheduler proof.
    expect(core.env).toEqual({
      PULSE_WORK_ITEM_CORE_HOST: JSON.stringify(fixture.core),
      PULSE_CORE_REQUEST_STATE_ROOT: join(fixture.data!, 'native-requests'),
    });
  });

  it('the interactive batch passes the same servers past --bare', async () => {
    spawnMock.mockImplementation(() => child(true));
    await createCcInteractiveBatchRuntimeAdapter({ sessionWiMcp }).run({ workspace: home, prompt: 'review', timeoutMs: 1000 });
    const args = spawnMock.mock.calls.at(-1)![1] as string[];
    expect(args).toContain('--bare');
    expect(mcpConfig(args)).toEqual(mcpConfig(await batchArgs()));
  });

  it('matches the conversation servers apart from the core environment the conversation process carries', async () => {
    spawnMock.mockImplementation(() => child(false));
    const pool = createPool({ cleanupIntervalMs: 999999, ccMode: 'print', sessionWiMcp });
    try {
      const pending = pool.spawn({ tenantId: 'fixture', sessionId: 'cc-session', workspace: home, ccMode: 'print' });
      spawnMock.mock.results.at(-1)!.value.stdout.write('{"type":"system","subtype":"init","content":"ready"}\n');
      await pending;
      const chatArgs = spawnMock.mock.calls.at(-1)![1] as string[];
      const chat = mcpConfig(chatArgs);
      expect(chatArgs[chatArgs.indexOf('--allowed-tools') + 1].split(',')).toEqual(expect.arrayContaining(
        ['mcp__pulse__pulse_work_item', 'mcp__pulse__pulse_stats', CC_SESSION_WI_TOOL]));
      const scheduled = mcpConfig(await batchArgs());
      const { env, ...scheduledCore } = scheduled.mcpServers['pulse'];
      expect(env).toBeDefined();
      expect({ ...scheduled.mcpServers, pulse: scheduledCore }).toEqual(chat.mcpServers);
    } finally {
      await pool.killAll();
      pool.dispose();
    }
  });

  it('gives a pool conversation its jobs key and pulse_jobs, and a scheduled run neither', async () => {
    spawnMock.mockImplementation(() => child(false));
    const pool = createPool({ cleanupIntervalMs: 999999, ccMode: 'print', sessionWiMcp });
    try {
      const pending = pool.spawn({ tenantId: 'fixture', sessionId: 'cc-session', workspace: home, ccMode: 'print' });
      spawnMock.mock.results.at(-1)!.value.stdout.write('{"type":"system","subtype":"init","content":"ready"}\n');
      await pending;
      const [, chatArgs, chatOptions] = spawnMock.mock.calls.at(-1)! as [string, string[], { env: Record<string, string> }];
      expect(chatArgs[chatArgs.indexOf('--allowed-tools') + 1].split(',')).toContain('mcp__pulse__pulse_jobs');
      expect(chatOptions.env['PULSE_AGENT_JOBS_KEY_FILE']).toBe(issueAgentJobsKey(fixture.data!, 'fixture'));
      const scheduled = await batchArgs();
      expect(scheduled.join(' ')).not.toContain('mcp__pulse__pulse_jobs');
      expect(JSON.stringify(mcpConfig(scheduled))).not.toContain('PULSE_AGENT_JOBS_KEY_FILE');
    } finally {
      await pool.killAll();
      pool.dispose();
    }
  });

  it('keeps the write boundary in the single --settings value beside the servers', async () => {
    const args = await batchArgs({ ccWriteBoundary: true });
    expect(args.filter((arg) => arg === '--settings')).toHaveLength(1);
    const settings = JSON.parse(args[args.indexOf('--settings') + 1]);
    const pi = ccPiState(home, home, join(fixture.data, 'cc-pi-agent'), basename(settings.env.PI_CODING_AGENT_DIR));
    expect(settings.sandbox).toMatchObject({ enabled: true, filesystem: { allowWrite: [home, ...ccPiWrites(pi)] } });
    expect(Object.keys(mcpConfig(args).mcpServers)).toContain(CC_SESSION_WI_SERVER);
    expect(settings.sandbox.network).toBeUndefined();
    const access = await batchArgs({ ccWriteBoundary: true, ccRepoAccess: { allowedDomains: ['github.com'] } });
    expect(JSON.parse(access[access.indexOf('--settings') + 1]).sandbox.network).toEqual({ allowedDomains: ['github.com'] });
  });

  it('registers only what resolved', async () => {
    fixture.data = undefined;
    expect(Object.keys(mcpConfig(await batchArgs()).mcpServers)).toEqual([CC_SESSION_WI_SERVER]);
    fixture.data = join(home, 'data');
    expect(Object.keys(mcpConfig(await batchArgs({}, null)).mcpServers)).toEqual(['pulse']);
    fixture.core = undefined;
    expect(await batchArgs({}, null)).not.toContain('--mcp-config');
  });
});
