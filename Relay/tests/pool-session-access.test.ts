/**
 * Human-approved session access: Relay reads the kernel's grants before a human
 * turn and resumes the same CC session with exactly those paths opened.
 */
import { describe, it, expect, vi, afterEach } from 'vitest';
import { EventEmitter } from 'node:events';
import { PassThrough } from 'node:stream';
import { existsSync, mkdirSync, mkdtempSync, readFileSync, realpathSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { basename, join } from 'node:path';
import { createPool } from '../src/pool.js';
import { ccPiEnv, ccPiState, ccPiWrites, ccSandboxEnv } from '../src/cc-write-boundary.js';
import { createConversationRuntimeManager, createPoolConversationAdapter } from '../src/conversation-runtime.js';
import { createMaintenanceBarrier } from '../src/maintenance-barrier.js';
import { persistQueuedTurns } from '../src/shutdown-interrupts.js';
import type { PersistedSession, StreamJsonMessage } from '../src/types.js';
import { resetStandingAccessCache, standingAccessFor } from '../src/standing-access.js';

/** The sandbox write list without Pi's writes (ccPiWrites): the session roots the hook judges. */
function sessionRoots(allowWrite: string[]): string[] {
  return allowWrite.filter((path) => !path.includes(join('.pi', 'agent')) && !path.includes('cc-pi-agent'));
}

const access = vi.fn();
const standing = vi.fn(async () => new Map());
const fixture = vi.hoisted(() => {
  const { mkdtempSync: make } = require('node:fs') as typeof import('node:fs');
  const { tmpdir: temp } = require('node:os') as typeof import('node:os');
  return { data: make(require('node:path').join(temp(), 'session-access-data-')) as string };
});
vi.mock('../src/managed-paths.js', async (original) => ({
  ...(await original<typeof import('../src/managed-paths.js')>()),
  managedRelayPath: (key: string, selected?: string) => (key === 'dataDir' ? fixture.data : selected),
}));
vi.mock('../src/native-work-item-command.js', async (original) => ({
  ...(await original<typeof import('../src/native-work-item-command.js')>()),
  nodeWorkItemCore: () => ({ extension: '/fixture/ext.mjs', python: '/fixture/python', entrypoint: '/fixture/harness.pyz' }),
  // An array is the merged view whose standing rows are the readback; an object is the readback.
  readSessionAccess: async (...args: unknown[]) => { const value = await access(...args);
    return Array.isArray(value)
      ? { access: value, standing: value.filter((row: { standing?: true }) => row.standing), workspaces: [] } : value; },
  readStandingAccess: () => standing(),
}));
vi.mock('../src/native-core-runtime.js', async (original) => ({
  ...(await original<typeof import('../src/native-core-runtime.js')>()),
  observeNativeSession: () => {},
}));
let mockSpawn: ReturnType<typeof vi.fn>;
vi.mock('node:child_process', async (original) => ({
  ...(await original<typeof import('node:child_process')>()),
  spawn: (...args: unknown[]) => mockSpawn(...args),
}));
vi.mock('../src/owned-process.js', async () => ({
  spawnOwnedProcess: (await import('node:child_process')).spawn,
}));

function child(pid: number) {
  const proc = Object.assign(new EventEmitter(), {
    stdin: new PassThrough(), stdout: new PassThrough(), stderr: new PassThrough(), pid, killed: false,
  }) as EventEmitter & { stdin: PassThrough; stdout: PassThrough; stderr: PassThrough;
    pid: number; killed: boolean; kill: ReturnType<typeof vi.fn> };
  proc.kill = vi.fn(() => { proc.killed = true; proc.emit('exit', 0, 'SIGTERM'); return true; });
  return proc;
}

describe('pool — human-approved session access', () => {
  let pool: ReturnType<typeof createPool>;
  const dir = mkdtempSync(join(tmpdir(), 'session-access-'));
  afterEach(() => { pool?.dispose(); access.mockReset(); resetStandingAccessCache(); vi.restoreAllMocks(); });

  it('resumes the same session only when the approved paths change', async () => {
    mockSpawn = vi.fn().mockReturnValueOnce(child(9201)).mockReturnValueOnce(child(9202));
    pool = createPool({ cleanupIntervalMs: 999999, ccMode: 'print' });
    await pool.spawn({ tenantId: 'owner:hestia', workspace: dir, sessionId: 'hestia-access', ccModel: 'opus' });
    access.mockResolvedValueOnce([]);
    await pool.send('owner:hestia', { type: 'human', content: 'first' }).catch(() => {});
    expect(mockSpawn).toHaveBeenCalledTimes(1);

    const granted = [{ path: dir, mode: 'write' as const }];
    access.mockResolvedValue(granted);
    pool.get('owner:hestia')!.state = 'ready';
    await pool.send('owner:hestia', { type: 'human', content: 'after approval' }).catch(() => {});
    expect(access).toHaveBeenLastCalledWith(9201);
    expect(mockSpawn).toHaveBeenCalledTimes(2);
    const args = mockSpawn.mock.calls[1][1] as string[];
    expect(args.slice(args.indexOf('--add-dir'), args.indexOf('--add-dir') + 2)).toEqual(['--add-dir', dir]);
    expect(args[args.indexOf('--append-system-prompt') + 1]).toContain(`会话授权：${dir}/（可读写`);
    expect(pool.get('owner:hestia')!.process.pid).toBe(9202);

    pool.get('owner:hestia')!.state = 'ready';
    await pool.send('owner:hestia', { type: 'human', content: 'same access' }).catch(() => {});
    expect(mockSpawn).toHaveBeenCalledTimes(2);

    access.mockRejectedValue(new Error('session_access_unavailable'));
    pool.get('owner:hestia')!.state = 'ready';
    await pool.send('owner:hestia', { type: 'human', content: 'readback down' }).catch(() => {});
    expect(mockSpawn).toHaveBeenCalledTimes(2);
    rmSync(dir, { recursive: true, force: true });
  });

  it('lists the shared repositories and SSOT read-only for every session without opening them for writing', async () => {
    const root = realpathSync(mkdtempSync(join(tmpdir(), 'coevo-')));
    const workspace = join(root, 'PARA', 'aki-space');
    const repos = [root, join(root, 'Bigbird', 'PULSE'), join(root, 'Bigbird', 'Oikos')];
    mkdirSync(join(root, 'ssot'));
    for (const repo of repos.slice(1)) mkdirSync(repo, { recursive: true });
    mkdirSync(workspace, { recursive: true });
    const sclHost = join(root, 'Bigbird', 'PULSE', 'data', 'agent-harness', 'scl-host');
    mkdirSync(sclHost, { recursive: true });
    mockSpawn = vi.fn().mockReturnValueOnce(child(9301));
    pool = createPool({ cleanupIntervalMs: 999999, ccMode: 'print' });
    await pool.spawn({ tenantId: 'owner:aki', workspace, sessionId: 'aki-ssot', ccModel: 'opus', ccWriteBoundary: true });
    const args = mockSpawn.mock.calls[0][1] as string[];
    const prompt = args[args.indexOf('--append-system-prompt') + 1];
    const ssot = join(root, 'ssot');
    expect(prompt).toContain(`- 共享仓库（只读）：${repos.map((repo) => `${repo}/`).join('、')}（本机共享检出；`);
    expect(prompt).toContain('不得在其中建 worktree');
    expect(prompt).toContain('- 共享仓库内不得读取（本会话工作区与授权路径不在此限）：凭据与令牌文件（');
    expect(prompt).toContain(`${sclHost}/`);
    expect(prompt).not.toContain(`、${workspace}/`);
    expect(prompt).toContain(`- 规则正本：${ssot}/（共享 SSOT，修改走自己分支的 PR）\n禁止读取或写入上述目录以外`);
    const addDirs = args.filter((arg, index) => args[index - 1] === '--add-dir');
    const settings = JSON.parse(args[args.lastIndexOf('--settings') + 1]);
    const roots = sessionRoots(settings.sandbox.filesystem.allowWrite as string[]);
    expect(settings.hooks.PreToolUse[0].hooks[0].args[1]).toBe(JSON.stringify(roots));
    for (const path of [...repos, ssot]) {
      expect(addDirs).not.toContain(path);
      expect(roots).not.toContain(path);
    }
    expect(roots[0]).toBe(workspace);
    rmSync(root, { recursive: true, force: true });
  });

  it('lists only the shared repositories present on the node', async () => {
    const root = realpathSync(mkdtempSync(join(tmpdir(), 'coevo-')));
    const workspace = join(root, 'PARA', 'aki-space');
    mkdirSync(join(root, 'ssot'));
    mkdirSync(join(root, 'Bigbird', 'PULSE'), { recursive: true });
    mkdirSync(workspace, { recursive: true });
    mockSpawn = vi.fn().mockReturnValueOnce(child(9302));
    pool = createPool({ cleanupIntervalMs: 999999, ccMode: 'print' });
    await pool.spawn({ tenantId: 'owner:aki', workspace, sessionId: 'aki-no-oikos', ccModel: 'opus' });
    const args = mockSpawn.mock.calls[0][1] as string[];
    const prompt = args[args.indexOf('--append-system-prompt') + 1];
    expect(prompt).toContain(`- 共享仓库（只读）：${root}/、${join(root, 'Bigbird', 'PULSE')}/（`);
    expect(prompt).not.toContain('Oikos');
    rmSync(root, { recursive: true, force: true });
  });

  it('opens the Agent standing read grants at spawn and keeps them when the readback agrees', async () => {
    const counterpart = mkdtempSync(join(tmpdir(), 'vigil-space-'));
    const row = { path: counterpart, mode: 'read' as const, standing: true as const };
    standing.mockResolvedValue(new Map([['vigil-33', [row]]]));
    mockSpawn = vi.fn().mockReturnValueOnce(child(9401));
    pool = createPool({ cleanupIntervalMs: 999999, ccMode: 'print' });
    await pool.spawn({ tenantId: 'xb:vigil-33', workspace: dir, sessionId: 'vigil-33-standing', ccModel: 'opus',
      harnessAgentId: 'vigil-33' });
    const args = mockSpawn.mock.calls[0][1] as string[];
    expect(args[args.indexOf('--append-system-prompt') + 1]).toContain(`常驻授权：${counterpart}/（只读`);
    expect(args.filter((arg, index) => args[index - 1] === '--add-dir')).toContain(counterpart);
    access.mockResolvedValue([row]);
    await pool.send('xb:vigil-33', { type: 'human', content: 'read my base' }).catch(() => {});
    expect(mockSpawn).toHaveBeenCalledTimes(1);
    rmSync(counterpart, { recursive: true, force: true });
  });

  it('keeps the standing grant when a session grant opens the same path', async () => {
    const counterpart = mkdtempSync(join(tmpdir(), 'vigil-space-'));
    const row = { path: counterpart, mode: 'read' as const, standing: true as const };
    standing.mockResolvedValue(new Map([['vigil-33', [row]]]));
    mockSpawn = vi.fn().mockReturnValueOnce(child(9411)).mockReturnValueOnce(child(9412));
    pool = createPool({ cleanupIntervalMs: 999999, ccMode: 'print' });
    await pool.spawn({ tenantId: 'xb:vigil-33', workspace: dir, sessionId: 'vigil-33-overlap', ccModel: 'opus',
      harnessAgentId: 'vigil-33' });
    // The merged view shows one write row; the independent standing readback still holds the grant.
    access.mockResolvedValue({ access: [{ path: counterpart, mode: 'write' }], standing: [row], workspaces: [] });
    await pool.send('xb:vigil-33', { type: 'human', content: 'write there too' }).catch(() => {});
    expect(mockSpawn).toHaveBeenCalledTimes(2);
    expect(await standingAccessFor('vigil-33')).toEqual([row]);
    rmSync(counterpart, { recursive: true, force: true });
  });

  it('enforces the write boundary for an Agent with ccWriteBoundary and keeps read grants read-only', async () => {
    const readable = mkdtempSync(join(tmpdir(), 'boundary-read-'));
    const writable = mkdtempSync(join(tmpdir(), 'boundary-write-'));
    const workspace = mkdtempSync(join(tmpdir(), 'vigil-xb-space-'));
    const cc = child(9501);
    mockSpawn = vi.fn().mockReturnValueOnce(cc);
    pool = createPool({ cleanupIntervalMs: 999999, ccMode: 'print' });
    await pool.spawn({ tenantId: 'xb:vigil-xb', workspace, sessionId: 'vigil-xb-boundary', ccModel: 'opus',
      ccWriteBoundary: true,
      sessionAccess: [{ path: readable, mode: 'read' }, { path: writable, mode: 'write' }] });
    const args = mockSpawn.mock.calls[0][1] as string[];
    const settings = JSON.parse(args[args.lastIndexOf('--settings') + 1]);
    const roots = sessionRoots(settings.sandbox.filesystem.allowWrite as string[]);
    expect(settings.sandbox).toMatchObject({ enabled: true, allowUnsandboxedCommands: false, failIfUnavailable: true });
    expect(settings.env).toMatchObject(ccSandboxEnv());
    expect(roots[0]).toBe(realpathSync(workspace));
    expect(roots[1]).toContain(join('.relay', 'outbox'));
    expect(roots.slice(2)).toEqual([realpathSync(writable)]);
    // Bash alone also gets Pi's agent directory in Relay's state and this workspace's Pi session directory.
    const pi = ccPiState(process.env['HOME'], workspace, join(fixture.data, 'cc-pi-agent'), basename(settings.env.PI_CODING_AGENT_DIR))!;
    expect(settings.sandbox.filesystem.allowWrite).toEqual([...roots, ...ccPiWrites(pi)]);
    expect(settings.env).toMatchObject(ccPiEnv(pi));
    // The directory is this session's: its CC process owns it, and its exit removes it.
    expect(readFileSync(`${pi.agentDir}.owner`, 'utf8')).toBe('9501\n');
    cc.emit('exit', 0, null);
    expect(existsSync(pi.agentDir)).toBe(false);
    expect(existsSync(`${pi.agentDir}.owner`)).toBe(false);
    expect(settings.hooks.PreToolUse[0].hooks[0]).toMatchObject({ args: [expect.any(String), JSON.stringify(roots), '[]'], onFailure: 'block' });
    expect(settings.sandbox.filesystem.denyWrite).toContain(join(realpathSync(workspace), '.claude'));
    expect(args.filter((arg, index) => args[index - 1] === '--add-dir')).toEqual([writable]);
    expect(args[args.indexOf('--append-system-prompt') + 1]).toContain('写入边界由系统强制');
    rmSync(readable, { recursive: true, force: true });
    rmSync(writable, { recursive: true, force: true });
    rmSync(workspace, { recursive: true, force: true });
  });

  it('opens the Agent commit repository Git data and domains to Bash and names them in the prompt', async () => {
    const workspace = realpathSync(mkdtempSync(join(tmpdir(), 'vigil-space-')));
    const repo = realpathSync(mkdtempSync(join(tmpdir(), 'commit-repo-')));
    mkdirSync(join(repo, '.git', 'objects'), { recursive: true });
    writeFileSync(join(repo, '.git', 'HEAD'), 'ref: refs/heads/main\n');
    mockSpawn = vi.fn().mockReturnValueOnce(child(9505));
    pool = createPool({ cleanupIntervalMs: 999999, ccMode: 'print' });
    await pool.spawn({ tenantId: 'owner:vigil', workspace, sessionId: 'vigil-commit', ccModel: 'opus', ccWriteBoundary: true,
      repoAccess: { commitRepos: [repo], allowedDomains: ['github.com', 'api.github.com'] } });
    const args = mockSpawn.mock.calls[0][1] as string[];
    const settings = JSON.parse(args[args.lastIndexOf('--settings') + 1]);
    expect(settings.sandbox.filesystem.allowWrite).toEqual(expect.arrayContaining([join(repo, '.git', 'index'), join(repo, '.git', 'objects')]));
    expect(settings.sandbox.filesystem.allowWrite).not.toContain(join(repo, '.git'));
    expect(settings.sandbox.filesystem.denyWrite).toEqual(expect.arrayContaining(
      [join(repo, '.git', 'hooks'), join(repo, '.git', 'config')]));
    expect(settings.sandbox.network).toEqual({ allowedDomains: ['github.com', 'api.github.com'] });
    expect(settings.hooks.PreToolUse[0].hooks[0].args[1]).not.toContain(join(repo, '.git'));
    const prompt = args[args.indexOf('--append-system-prompt') + 1];
    expect(prompt).toContain(`可提交的仓库：${repo}`);
    expect(prompt).toContain('放行出网的域名：github.com、api.github.com');
    // An entry that is not a repository root refuses the start.
    mockSpawn = vi.fn().mockReturnValue(child(9506));
    await expect(pool.spawn({ tenantId: 'owner:vigil-2', workspace, sessionId: 'vigil-commit-2', ccModel: 'opus',
      ccWriteBoundary: true, repoAccess: { commitRepos: [workspace] } })).rejects.toThrow('not a repository root');
    expect(mockSpawn).not.toHaveBeenCalled();
    rmSync(workspace, { recursive: true, force: true });
    rmSync(repo, { recursive: true, force: true });
  });

  it('keeps standing read grants out of the write roots under the boundary', async () => {
    const counterpart = mkdtempSync(join(tmpdir(), 'vigil-space-'));
    standing.mockResolvedValue(new Map([['vigil-33', [{ path: counterpart, mode: 'read' as const, standing: true as const }]]]));
    mockSpawn = vi.fn().mockReturnValueOnce(child(9504));
    pool = createPool({ cleanupIntervalMs: 999999, ccMode: 'print' });
    await pool.spawn({ tenantId: 'xb:vigil-33', workspace: dir, sessionId: 'vigil-33-bounded', ccModel: 'opus',
      harnessAgentId: 'vigil-33', ccWriteBoundary: true });
    const args = mockSpawn.mock.calls[0][1] as string[];
    expect(args[args.indexOf('--append-system-prompt') + 1]).toContain(`常驻授权：${counterpart}/（只读`);
    expect(args.filter((arg, index) => args[index - 1] === '--add-dir')).not.toContain(counterpart);
    const settings = JSON.parse(args[args.lastIndexOf('--settings') + 1]);
    expect(settings.sandbox.filesystem.allowWrite).not.toContain(realpathSync(counterpart));
    rmSync(counterpart, { recursive: true, force: true });
  });

  it('refuses to start a bounded session whose settings widen writes', async () => {
    const workspace = mkdtempSync(join(tmpdir(), 'vigil-xb-space-'));
    mkdirSync(join(workspace, '.claude'));
    writeFileSync(join(workspace, '.claude', 'settings.local.json'),
      JSON.stringify({ permissions: { additionalDirectories: ['/srv/shared'] } }));
    mockSpawn = vi.fn().mockReturnValue(child(9503));
    pool = createPool({ cleanupIntervalMs: 999999, ccMode: 'print' });
    await expect(pool.spawn({ tenantId: 'xb:vigil-xb', workspace, sessionId: 'vigil-xb-refused', ccModel: 'opus',
      ccWriteBoundary: true })).rejects.toThrow('permissions.additionalDirectories');
    expect(mockSpawn).not.toHaveBeenCalled();
    rmSync(workspace, { recursive: true, force: true });
  });

  it('leaves Agents without ccWriteBoundary unchanged', async () => {
    mockSpawn = vi.fn().mockReturnValueOnce(child(9502));
    pool = createPool({ cleanupIntervalMs: 999999, ccMode: 'print' });
    await pool.spawn({ tenantId: 'owner:pulse', workspace: dir, sessionId: 'pulse-no-boundary', ccModel: 'opus' });
    const args = mockSpawn.mock.calls[0][1] as string[];
    expect(JSON.stringify(args)).not.toContain('"sandbox"');
    expect(JSON.stringify(args)).not.toContain('cc-write-boundary.mjs');
    expect(JSON.stringify(args)).not.toContain('NODE_USE_ENV_PROXY');
    expect(JSON.stringify(args)).not.toContain('CODEX_CA_CERTIFICATE');
  });

  it('opens the workspaces the kernel allocated to the held WIs and closes them when the readback drops them', async () => {
    const workspace = mkdtempSync(join(tmpdir(), 'vigil-xb-space-'));
    const managed = realpathSync(mkdtempSync(join(tmpdir(), 'managed-')));
    const path = join(managed, 'wi_1', 'workspace.op_1.0');
    const admin = join(managed, 'repo.git', 'worktrees', 'workspace.op_1.0');
    mkdirSync(path, { recursive: true });
    mkdirSync(admin, { recursive: true });
    const allocated = { workItemId: 'wi_1', resourceId: 'workspace.op_1.0', path,
      write: [path, admin], deny: [join(path, '.git'), join(admin, 'config.worktree')] };
    mockSpawn = vi.fn().mockReturnValueOnce(child(9601)).mockReturnValueOnce(child(9602))
      .mockReturnValueOnce(child(9603));
    pool = createPool({ cleanupIntervalMs: 999999, ccMode: 'print' });
    await pool.spawn({ tenantId: 'xb:vigil-xb', workspace, sessionId: 'vigil-xb-allocated', ccModel: 'opus',
      ccWriteBoundary: true });
    const settingsOf = (call: number) => {
      const args = mockSpawn.mock.calls[call][1] as string[];
      return { args, settings: JSON.parse(args[args.lastIndexOf('--settings') + 1]) };
    };
    expect(settingsOf(0).settings.sandbox.filesystem.allowWrite).not.toContain(path);

    // The session now holds a WI whose workspace is ready: the same session resumes with it open.
    access.mockResolvedValue({ access: [], standing: [], workspaces: [allocated] });
    await pool.send('xb:vigil-xb', { type: 'human', content: 'edit in the workspace' }).catch(() => {});
    expect(mockSpawn).toHaveBeenCalledTimes(2);
    const { args, settings } = settingsOf(1);
    const roots = sessionRoots(settings.sandbox.filesystem.allowWrite as string[]);
    expect(roots).toEqual(expect.arrayContaining([path, admin]));
    // The kernel's denials join the policy paths, which still hold inside every new root.
    expect(settings.sandbox.filesystem.denyWrite).toEqual(expect.arrayContaining([
      ...allocated.deny, join(path, '.claude'), join(path, '.git', 'hooks'), join(path, '.git', 'config')]));
    expect(settings.hooks.PreToolUse[0].hooks[0].args.slice(1)).toEqual(
      [JSON.stringify(roots), JSON.stringify(allocated.deny)]);
    expect(args.filter((arg, index) => args[index - 1] === '--add-dir')).toEqual([path]);
    expect(args[args.indexOf('--append-system-prompt') + 1]).toContain(`内核分配的工作区（可读写）：${path}/（wi_1 的 workspace.op_1.0；`);

    // An unavailable readback keeps the current boundary; it never opens anything new.
    access.mockRejectedValue(new Error('session_access_unavailable'));
    pool.get('xb:vigil-xb')!.state = 'ready';
    await pool.send('xb:vigil-xb', { type: 'human', content: 'readback down' }).catch(() => {});
    expect(mockSpawn).toHaveBeenCalledTimes(2);

    // Retired, or no longer held: the kernel stops naming it and the session resumes without it.
    access.mockResolvedValue({ access: [], standing: [], workspaces: [] });
    pool.get('xb:vigil-xb')!.state = 'ready';
    await pool.send('xb:vigil-xb', { type: 'human', content: 'after retirement' }).catch(() => {});
    expect(mockSpawn).toHaveBeenCalledTimes(3);
    expect(settingsOf(2).settings.sandbox.filesystem.allowWrite).not.toContain(path);
    expect(settingsOf(2).args[settingsOf(2).args.indexOf('--append-system-prompt') + 1]).not.toContain('内核分配的工作区（可读写）');
    rmSync(workspace, { recursive: true, force: true });
    rmSync(managed, { recursive: true, force: true });
  });

  it('opens no allocated workspace when a new session cannot read its access back', async () => {
    const workspace = mkdtempSync(join(tmpdir(), 'vigil-xb-space-'));
    mockSpawn = vi.fn().mockReturnValueOnce(child(9611));
    pool = createPool({ cleanupIntervalMs: 999999, ccMode: 'print' });
    await pool.spawn({ tenantId: 'xb:vigil-xb', workspace, sessionId: 'vigil-xb-unread', ccModel: 'opus',
      ccWriteBoundary: true });
    access.mockRejectedValue(new Error('session_access_unavailable'));
    await pool.send('xb:vigil-xb', { type: 'human', content: 'first' }).catch(() => {});
    expect(mockSpawn).toHaveBeenCalledTimes(1);
    const args = mockSpawn.mock.calls[0][1] as string[];
    const roots = sessionRoots(JSON.parse(args[args.lastIndexOf('--settings') + 1]).sandbox.filesystem.allowWrite as string[]);
    expect(roots).toHaveLength(2);
    rmSync(workspace, { recursive: true, force: true });
  });

  it('a shutdown while a turn waits for its access readback records it for resume; the receipt came first, nothing reaches the process', async () => {
    const proc = child(9401);
    mockSpawn = vi.fn().mockReturnValueOnce(proc);
    const sessions: Record<string, PersistedSession> = {};
    const state = {
      flush: vi.fn(),
      getSession: (key: string) => sessions[key],
      getAllSessions: () => sessions,
      updateSession: (key: string, patch: Partial<PersistedSession>) => { sessions[key] = { ...sessions[key]!, ...patch }; },
    };
    const interrupted = vi.fn();
    pool = createPool({ cleanupIntervalMs: 999999, ccMode: 'print', onShutdownInterrupt: interrupted,
      onShutdownQueued: (tenantId, turns) => { persistQueuedTurns(state, turns.map(turn => ({ tenantId, runtime: 'cc', ...turn })), Date.now()); } });
    const barrier = createMaintenanceBarrier();
    const runtime = createConversationRuntimeManager({ barrier });
    runtime.register(createPoolConversationAdapter(pool));
    await runtime.spawn({ tenantId: 'owner:hestia', workspace: dir, sessionId: 'hestia-window', ccModel: 'opus', runtime: 'cc' } as never);
    sessions['owner:hestia'] = { poolKey: 'owner:hestia', agentId: 'hestia', sessionId: 'hestia-window', turnCount: 0, maxTurns: 9,
      lastActivity: Date.now(), status: 'active' };
    const written: string[] = [];
    proc.stdin.on('data', (chunk: Buffer) => written.push(chunk.toString()));
    let releaseAccess!: () => void;
    access.mockImplementation(() => new Promise(resolve => { releaseAccess = () => resolve([]); }));

    const receipts: string[] = [];
    let settled = false;
    const sent = runtime.send('owner:hestia', { type: 'human', content: 'during readback' },
      { onAccepted: () => receipts.push('accepted') }).then(() => { settled = true; });
    await vi.waitFor(() => expect(access).toHaveBeenCalled());
    // Still preparing its dispatch: already accepted, so its channel may let go.
    expect(receipts).toEqual(['accepted']);
    expect(pool.get('owner:hestia')!.state).toBe('ready');

    // Shutdown order of index.ts: barrier closed, then the runtimes.
    barrier.close(turn => { persistQueuedTurns(state, [turn], Date.now()); });
    runtime.emitShutdown();
    expect(interrupted).not.toHaveBeenCalled();
    expect(sessions['owner:hestia']!.pendingMaintenanceTurns?.map((turn: { message: StreamJsonMessage }) => turn.message.content))
      .toEqual(['during readback']);

    releaseAccess();
    await sent;
    expect(settled).toBe(true);
    expect(written.join('')).not.toContain('during readback');
    expect(sessions['owner:hestia']!.pendingMaintenanceTurns).toHaveLength(1);
    runtime.dispose();
    rmSync(dir, { recursive: true, force: true });
  });
});
