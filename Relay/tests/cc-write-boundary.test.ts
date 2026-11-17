/**
 * CC write boundary: the PreToolUse hook denies file-tool writes outside the
 * session's write roots, and the settings fragment confines Bash writes with
 * CC's sandbox; reads are never judged.
 */
import { describe, expect, it, vi } from 'vitest';
import { execFileSync, spawnSync } from 'node:child_process';
import {
  chmodSync, existsSync, lstatSync, mkdirSync, mkdtempSync, readdirSync, readFileSync, readlinkSync, realpathSync, rmdirSync, rmSync, statSync, symlinkSync,
  writeFileSync,
} from 'node:fs';
import { tmpdir } from 'node:os';
import { basename, dirname, join } from 'node:path';
// @ts-expect-error -- plain ESM hook script without type declarations
import { boundaryDecision } from '../runtime/cc-write-boundary.mjs';
import {
  RELAY_INSTALLATION, addCcWriteBoundarySettings, ccPiEnv, ccPiState, ccPiWrites, ccPolicyPaths, ccSandboxEnv,
  ccWriteBoundarySettings, ccWriteDenials, ccWriteRoots, leasePiAgentDir, piSessionDirName, writeRelaxations,
  type PiAgentDirResult, type PiAgentDirSystem, type PiAgentLease,
} from '../src/cc-write-boundary.js';
import { addSessionWiGuardSettings } from '../src/session-wi-guard.js';

const HOOK = join(__dirname, '..', 'runtime', 'cc-write-boundary.mjs');

describe('CC write boundary hook', () => {
  const root = realpathSync(mkdtempSync(join(tmpdir(), 'write-boundary-')));
  const workspace = join(root, 'tenants', '33', 'vigil-33-space');
  const base = join(root, 'vigil-space');
  mkdirSync(workspace, { recursive: true });
  mkdirSync(base, { recursive: true });

  it('rejects a file-tool write outside the write roots and allows one inside', () => {
    const write = (file_path: string) => ({ tool_name: 'Write', tool_input: { file_path, content: 'x' }, cwd: workspace });
    expect(boundaryDecision(write(join(workspace, 'memory', 'note.md')), [workspace])).toBeUndefined();
    expect(boundaryDecision(write('relative/note.md'), [workspace])).toBeUndefined();
    expect(boundaryDecision(write(join(base, 'note.md')), [workspace])).toContain('outside');
    expect(boundaryDecision(write(join(workspace, '..', 'escape.md')), [workspace])).toContain('outside');
    expect(boundaryDecision({ tool_name: 'Edit', tool_input: { file_path: join(base, 'a') } }, [workspace])).toContain('outside');
    expect(boundaryDecision({ tool_name: 'NotebookEdit', tool_input: { notebook_path: join(base, 'a.ipynb') } }, [workspace]))
      .toContain('outside');
  });

  it('keeps reads and other tools untouched', () => {
    expect(boundaryDecision({ tool_name: 'Read', tool_input: { file_path: join(base, 'note.md') } }, [workspace])).toBeUndefined();
    expect(boundaryDecision({ tool_name: 'Grep', tool_input: { path: base } }, [workspace])).toBeUndefined();
  });

  it('follows a symbolic link out of a root and denies it', () => {
    symlinkSync(base, join(workspace, 'base-link'));
    expect(boundaryDecision({ tool_name: 'Write', tool_input: { file_path: join(workspace, 'base-link', 'x.md') } },
      [workspace])).toContain('outside');
    rmSync(join(workspace, 'base-link'));
  });

  it('follows a dangling link to its missing target outside the root and denies it', () => {
    const outside = join(root, 'outside');
    mkdirSync(outside);
    symlinkSync(join(outside, 'new.txt'), join(workspace, 'dangling'));
    symlinkSync(join(outside, 'missing-dir'), join(workspace, 'dangling-dir'));
    const write = (file_path: string) => boundaryDecision({ tool_name: 'Write', tool_input: { file_path } }, [workspace]);
    expect(write(join(workspace, 'dangling'))).toContain('outside');
    expect(write(join(workspace, 'dangling-dir', 'x.txt'))).toContain('outside');
    // A link loop cannot be resolved safely.
    symlinkSync(join(workspace, 'loop-b'), join(workspace, 'loop-a'));
    symlinkSync(join(workspace, 'loop-a'), join(workspace, 'loop-b'));
    expect(write(join(workspace, 'loop-a'))).toContain('cannot be resolved safely');
    // A dangling link that stays inside the root is an ordinary new file.
    symlinkSync(join(workspace, 'later.md'), join(workspace, 'inside-link'));
    expect(write(join(workspace, 'inside-link'))).toBeUndefined();
    for (const name of ['dangling', 'dangling-dir', 'loop-a', 'loop-b', 'inside-link']) rmSync(join(workspace, name));
  });

  it('runs as a CC hook command and fails closed on an unreadable request', () => {
    const run = (input: string, roots = JSON.stringify([workspace])) =>
      execFileSync(process.execPath, [HOOK, roots], { input, encoding: 'utf8' });
    const outside = JSON.parse(run(JSON.stringify({ tool_name: 'Write', tool_input: { file_path: join(base, 'x') } })));
    expect(outside.hookSpecificOutput).toMatchObject({ hookEventName: 'PreToolUse', permissionDecision: 'deny' });
    expect(run(JSON.stringify({ tool_name: 'Write', tool_input: { file_path: join(workspace, 'x') } }))).toBe('');
    expect(JSON.parse(run('not json')).hookSpecificOutput.permissionDecision).toBe('deny');
    expect(JSON.parse(run('{}', 'not roots')).hookSpecificOutput.permissionDecision).toBe('deny');
  });

  it('denies writes under the paths the kernel keeps closed in an allocated workspace, as a command too', () => {
    const allocated = join(root, 'managed', 'workspace.op_1.0');
    const admin = join(root, 'repo', '.git', 'worktrees', 'workspace.op_1.0');
    mkdirSync(allocated, { recursive: true });
    mkdirSync(admin, { recursive: true });
    const roots = [workspace, allocated, admin];
    const denials = [join(allocated, '.git'), join(admin, 'config.worktree')];
    const write = (file_path: string) => ({ tool_name: 'Write', tool_input: { file_path, content: 'x' } });
    expect(boundaryDecision(write(join(allocated, 'src', 'a.ts')), roots, denials)).toBeUndefined();
    expect(boundaryDecision(write(join(admin, 'index')), roots, denials)).toBeUndefined();
    expect(boundaryDecision(write(join(admin, 'config.worktree')), roots, denials)).toContain('keeps closed');
    expect(boundaryDecision(write(join(allocated, '.git')), roots, denials)).toBeDefined();
    const run = (file_path: string, extra: string[]) => execFileSync(process.execPath,
      [HOOK, JSON.stringify(roots), ...extra], { input: JSON.stringify(write(file_path)), encoding: 'utf8' });
    expect(JSON.parse(run(join(admin, 'config.worktree'), [JSON.stringify(denials)])).hookSpecificOutput.permissionDecision)
      .toBe('deny');
    expect(run(join(admin, 'HEAD'), [JSON.stringify(denials)])).toBe('');
    // Unreadable denials fail closed.
    expect(JSON.parse(run(join(admin, 'HEAD'), ['not denials'])).hookSpecificOutput.permissionDecision).toBe('deny');
  });

  it('denies writes that would change the session execution policy, even inside a root', () => {
    const write = (file_path: string, roots = [workspace]) =>
      boundaryDecision({ tool_name: 'Write', tool_input: { file_path, content: '{}' } }, roots);
    for (const file of [
      join(workspace, '.claude'),
      join(workspace, '.claude', 'settings.json'),
      join(workspace, '.claude', 'settings.local.json'),
      join(workspace, '.claude', 'hooks', 'pre.sh'),
      join(workspace, '.claude', 'skills', 'x', 'SKILL.md'),
      join(workspace, '.mcp.json'),
      join(workspace, 'nested', '.mcp.json'),
      join(workspace, 'nested', '.claude', 'settings.json'),
      join(workspace, '.git', 'hooks', 'pre-commit'),
      join(workspace, '.git', 'config'),
    ]) expect(write(file)).toContain('execution policy');
    // The Relay installation holding the hook is never writable, even when a root contains it.
    expect(write(HOOK, [join(__dirname, '..', '..')])).toContain('Relay installation');
    expect(boundaryDecision({ tool_name: 'Edit', tool_input: { file_path: join(workspace, '.claude', 'settings.local.json') } },
      [workspace])).toContain('execution policy');
    expect(write(join(workspace, 'notes', 'claude.md'))).toBeUndefined();
  });

  it('protects a policy entry point that is a link to an ordinary in-root target, through both names', () => {
    const ws = realpathSync(mkdtempSync(join(tmpdir(), 'boundary-links-')));
    const write = (file_path: string) => boundaryDecision({ tool_name: 'Write', tool_input: { file_path } }, [ws]);
    mkdirSync(join(ws, 'config', 'hooks'), { recursive: true });
    symlinkSync(join(ws, 'config'), join(ws, '.claude'));
    writeFileSync(join(ws, 'servers.json'), '{}');
    symlinkSync(join(ws, 'servers.json'), join(ws, '.mcp.json'));
    mkdirSync(join(ws, 'gitmeta', 'hooks'), { recursive: true });
    mkdirSync(join(ws, '.git'));
    symlinkSync(join(ws, 'gitmeta', 'hooks'), join(ws, '.git', 'hooks'));
    mkdirSync(join(ws, 'nested', 'cfg'), { recursive: true });
    symlinkSync(join(ws, 'nested', 'cfg'), join(ws, 'nested', '.claude'));
    for (const file of [
      join(ws, '.claude', 'settings.json'), join(ws, '.claude', 'hooks', 'pre.sh'),
      join(ws, 'config', 'settings.local.json'), join(ws, 'config', 'hooks', 'pre.sh'),
      join(ws, '.mcp.json'), join(ws, 'servers.json'),
      join(ws, '.git', 'hooks', 'pre-commit'), join(ws, 'gitmeta', 'hooks', 'pre-commit'),
      join(ws, 'nested', '.claude', 'settings.json'), join(ws, 'nested', 'cfg', 'settings.json'),
    ]) expect(write(file), file).toContain('execution policy');
    expect(write(join(ws, 'notes.md'))).toBeUndefined();
    expect(write(join(ws, 'gitmeta', 'README.md'))).toBeUndefined();
    expect(write(join(ws, 'nested', 'notes.md'))).toBeUndefined();
    const denied = ccPolicyPaths([ws]);
    for (const path of [join(ws, '.claude'), join(ws, 'config'), join(ws, '.mcp.json'), join(ws, 'servers.json'),
      join(ws, '.git', 'hooks'), join(ws, 'gitmeta', 'hooks')]) expect(denied).toContain(path);
    rmSync(ws, { recursive: true, force: true });
  });
});

describe('CC write boundary settings', () => {
  it('bounds Bash with the sandbox over the same roots, without an unsandboxed fallback', () => {
    const roots = ccWriteRoots({ workspace: '/w', outbox: '/w/.relay/outbox/t',
      sessionAccess: [{ path: '/granted', mode: 'write' }, { path: '/read-only', mode: 'read' }] });
    expect(roots).toEqual(['/w', '/w/.relay/outbox/t', '/granted']);
    const settings = ccWriteBoundarySettings(roots, '/node') as { sandbox: Record<string, unknown>; hooks: Record<string, any[]> };
    expect(settings.sandbox).toEqual({ enabled: true, failIfUnavailable: true, allowUnsandboxedCommands: false,
      autoAllowBashIfSandboxed: true, filesystem: { allowWrite: roots, denyWrite: ccPolicyPaths(roots) } });
    expect(settings.hooks['PreToolUse']![0].matcher).toBe('Edit|Write|MultiEdit|NotebookEdit');
  });

  it('denies Bash the same policy paths inside every root', () => {
    const denied = ccPolicyPaths(['/w', '/granted']);
    for (const path of ['/w/.claude', '/w/.mcp.json', '/w/.git/hooks', '/w/.git/config', '/granted/.claude', '/granted/.mcp.json']) {
      expect(denied).toContain(path);
    }
    expect(denied).toContain(realpathSync(RELAY_INSTALLATION));
  });

  it('opens an allocated workspace and its commit paths, and denies the kernel denials with the policy paths', () => {
    const allocated = [{ write: ['/m/ws', '/r/.git/worktrees/ws', '/r/.git/objects'],
      deny: ['/m/ws/.git', '/r/.git/worktrees/ws/config.worktree'] }];
    const roots = ccWriteRoots({ workspace: '/w', allocatedWorkspaces: allocated });
    expect(roots).toEqual(['/w', '/m/ws', '/r/.git/worktrees/ws', '/r/.git/objects']);
    const denials = ccWriteDenials(allocated);
    const settings = ccWriteBoundarySettings(roots, '/node', denials) as Record<string, any>;
    const denied = settings.sandbox.filesystem.denyWrite as string[];
    for (const path of [...allocated[0]!.deny, '/m/ws/.claude', '/m/ws/.git/hooks', '/w/.claude']) expect(denied).toContain(path);
    expect(settings.hooks.PreToolUse[0].hooks[0].args.slice(1)).toEqual([JSON.stringify(roots), JSON.stringify(denials)]);
    expect(ccWriteDenials(undefined)).toEqual([]);
  });

  it('runs the hook fail-closed: exec form, onFailure block, hooks pinned on', () => {
    const settings = ccWriteBoundarySettings(['/w'], '/node') as Record<string, any>;
    // A hook that cannot start (missing script), exits non-zero or times out blocks the call.
    expect(settings.hooks.PreToolUse[0].hooks).toEqual([{ type: 'command', command: '/node',
      args: [HOOK, JSON.stringify(['/w']), '[]'], timeout: 10, onFailure: 'block' }]);
    expect(settings.disableAllHooks).toBe(false);
  });

  it('gives sandboxed Bash the AI CLI environment, with the CA bundle only on macOS', () => {
    expect(ccSandboxEnv('darwin')).toEqual({ CODEX_CA_CERTIFICATE: '/etc/ssl/cert.pem', NODE_USE_ENV_PROXY: '1' });
    expect(ccSandboxEnv('linux')).toEqual({ NODE_USE_ENV_PROXY: '1' });
    expect(ccWriteBoundarySettings(['/w'], '/node', [], undefined, 'darwin')['env']).toEqual(ccSandboxEnv('darwin'));
    expect(ccWriteBoundarySettings(['/w'], '/node', [], undefined, 'linux')['env']).toEqual(ccSandboxEnv('linux'));
    expect(ccWriteBoundarySettings(['/w'], '/node')['env']).toEqual(ccSandboxEnv());
  });

  it('merges into the single --settings value beside the WI guard and subscription routing', () => {
    const args = ['-p', '--settings', JSON.stringify({ env: { ANTHROPIC_BASE_URL: 'https://api.anthropic.com' } })];
    addSessionWiGuardSettings(args, '/node');
    addCcWriteBoundarySettings(args, ['/w'], { workspace: '/w' }, '/node');
    expect(args.filter((arg) => arg === '--settings')).toHaveLength(1);
    const merged = JSON.parse(args[args.indexOf('--settings') + 1]);
    expect(merged.env).toEqual({ ANTHROPIC_BASE_URL: 'https://api.anthropic.com', ...ccSandboxEnv() });
    expect(merged.hooks.PreToolUse.map((group: { matcher: string }) => group.matcher))
      .toEqual(['Bash|mcp__pulse__pulse_work_item', 'Edit|Write|MultiEdit|NotebookEdit']);
    expect(merged.sandbox.filesystem.allowWrite).toEqual(['/w']);
  });

  it('replaces any sandbox already in the --settings value instead of uniting its relaxations', () => {
    const args = ['--settings', JSON.stringify({ sandbox: { excludedCommands: ['git'], filesystem: { allowWrite: ['/etc'], disabled: true } } })];
    addCcWriteBoundarySettings(args, ['/w'], { workspace: '/w' }, '/node');
    expect(JSON.parse(args[1]).sandbox).toEqual(ccWriteBoundarySettings(['/w'], '/node')['sandbox']);
  });

  it('refuses settings files that would widen writes beyond the session roots', () => {
    const home = mkdtempSync(join(tmpdir(), 'boundary-home-'));
    const repo = mkdtempSync(join(tmpdir(), 'boundary-repo-'));
    const workspace = join(repo, 'agent-space');
    mkdirSync(join(repo, '.git'));
    mkdirSync(join(home, '.claude'), { recursive: true });
    mkdirSync(join(repo, '.claude'), { recursive: true });
    mkdirSync(join(workspace, '.claude'), { recursive: true });
    const add = () => addCcWriteBoundarySettings(['-p'], [workspace], { home, workspace }, '/node');
    writeFileSync(join(home, '.claude', 'settings.json'), JSON.stringify({ model: 'opus', permissions: { allow: ['Edit', 'Bash(git status)'] } }));
    expect(add).not.toThrow();
    const cases: Array<[string, unknown, string]> = [
      [join(home, '.claude', 'settings.json'), { sandbox: { filesystem: { allowWrite: ['/tmp/x'] } } }, 'sandbox.filesystem.allowWrite'],
      [join(workspace, '.claude', 'settings.local.json'), { sandbox: { excludedCommands: ['docker'] } }, 'sandbox.excludedCommands'],
      [join(workspace, '.claude', 'settings.json'), { sandbox: { filesystem: { disabled: true } } }, 'sandbox.filesystem.disabled'],
      [join(repo, '.claude', 'settings.local.json'), { permissions: { allow: ['Edit(//Volumes/other/**)'] } }, 'file-tool allow rule'],
      [join(repo, '.claude', 'settings.json'), { permissions: { additionalDirectories: ['/srv'] } }, 'additionalDirectories'],
      // Hooks switched off anywhere would leave the file tools unbounded.
      [join(workspace, '.claude', 'settings.local.json'), { disableAllHooks: true }, 'disableAllHooks'],
      [join(home, '.claude', 'settings.json'), { disableAllHooks: true }, 'disableAllHooks'],
      [join(repo, '.claude', 'settings.local.json'), { allowManagedHooksOnly: true }, 'allowManagedHooksOnly'],
    ];
    for (const [file, settings, reason] of cases) {
      writeFileSync(file, JSON.stringify(settings));
      expect(add).toThrow(reason);
      rmSync(file);
    }
    expect(writeRelaxations(join(home, 'absent.json'))).toEqual([]);
    // Managed settings CC cannot read leave hook policy unknown: refused. Other unreadable files CC ignores.
    writeFileSync(join(home, 'broken.json'), '{not json');
    expect(writeRelaxations(join(home, 'broken.json'), true)).toEqual([`${join(home, 'broken.json')}: unreadable managed settings`]);
    expect(writeRelaxations(join(home, 'broken.json'))).toEqual([]);
    writeFileSync(join(home, 'managed.json'), JSON.stringify({ allowManagedHooksOnly: true }));
    expect(writeRelaxations(join(home, 'managed.json'), true)[0]).toContain('allowManagedHooksOnly');
    rmSync(home, { recursive: true, force: true });
    rmSync(repo, { recursive: true, force: true });
  });
});

describe('Pi state under the CC write boundary', () => {
  /** An account Pi agent directory with the entries Pi 0.87.1 keeps there. */
  function account() {
    const home = realpathSync(mkdtempSync(join(tmpdir(), 'boundary-pi-home-')));
    const source = join(home, '.pi', 'agent');
    mkdirSync(join(source, 'sessions'), { recursive: true });
    mkdirSync(join(source, 'extensions'), { recursive: true });
    mkdirSync(join(source, 'tmp'), { recursive: true });
    for (const name of ['auth.json', 'settings.json', 'models-store.json', 'trust.json']) writeFileSync(join(source, name), '{}');
    return { home, source };
  }

  /** Processes that run, as leasePiAgentDir sees them; Relay itself is 100. Deleting a path in `stuck` fails as an immutable one would. */
  function system(running: Set<number> = new Set([100]), stuck: Set<string> = new Set()) {
    const chowned: Array<[string, number, number]> = [];
    const sys: PiAgentDirSystem = {
      pid: 100, alive: (pid) => running.has(pid), chown: (path, uid, gid) => { chowned.push([path, uid, gid]); },
      remove: (path) => {
        if (stuck.has(path)) throw Object.assign(new Error(`EPERM: operation not permitted, rmdir '${path}'`), { code: 'EPERM' });
        rmSync(path, { recursive: true, force: true });
      },
    };
    return { sys, running, chowned, stuck };
  }

  /** What a sandboxed session can leave in its own directory to block `rm -rf`: a closed directory holding a file, and a closed directory itself. */
  function closeUp(agentDir: string): void {
    mkdirSync(join(agentDir, 'unreadable', 'deeper'), { recursive: true });
    writeFileSync(join(agentDir, 'unreadable', 'deeper', 'left'), '');
    writeFileSync(join(agentDir, 'unreadable', 'left'), '');
    chmodSync(join(agentDir, 'unreadable', 'deeper'), 0o000);
    chmodSync(join(agentDir, 'unreadable'), 0o000);
    chmodSync(agentDir, 0o500);
  }

  function lease(result: PiAgentDirResult | undefined): PiAgentLease {
    if (!result || !('lease' in result)) throw new Error(`no lease: ${JSON.stringify(result)}`);
    return result.lease;
  }

  const links = ['auth.json', 'extensions', 'models-store.json', 'settings.json', 'trust.json'];

  it('names the session directory the way Pi does', () => {
    expect(piSessionDirName('/Users/owner/Coevolution/PARA/Areas/happiness-cornerstone/live-well/vigil-space'))
      .toBe('--Users-owner-Coevolution-PARA-Areas-happiness-cornerstone-live-well-vigil-space--');
    expect(piSessionDirName('/Users/owner/.local/state/agent-harness/workspaces/x'))
      .toBe('--Users-owner-.local-state-agent-harness-workspaces-x--');
    expect(piSessionDirName('/srv/a:b/c/')).toBe('--srv-a-b-c--');
    expect(piSessionDirName('/')).toBe('----');
  });

  it("keeps Pi's agent directory in Relay's state and opens only it and the workspace's session directory", () => {
    const { home, source } = account();
    const state = realpathSync(mkdtempSync(join(tmpdir(), 'boundary-pi-state-')));
    const real = realpathSync(mkdtempSync(join(tmpdir(), 'boundary-pi-ws-')));
    const linked = join(home, 'linked-space');
    symlinkSync(real, linked);
    const pi = ccPiState(home, linked, state, 'run-1')!;
    // Pi names the session directory from its physical working directory; Relay's directory for the workspace takes the same name.
    expect(pi).toEqual({ source, agentDir: join(state, piSessionDirName(real), 'run-1'), sessionDir: join(source, 'sessions', piSessionDirName(real)) });
    expect(ccPiWrites(pi)).toEqual([pi.agentDir, pi.sessionDir]);
    expect(ccPiEnv(pi)).toEqual({ PI_CODING_AGENT_DIR: pi.agentDir, PI_CODING_AGENT_SESSION_DIR: pi.sessionDir });
    for (const closed of [source, join(source, 'sessions'), ...['auth', 'settings', 'models-store', 'trust']
      .flatMap((name) => [join(source, `${name}.json`), join(source, `${name}.json.lock`)])]) {
      expect(ccPiWrites(pi)).not.toContain(closed);
    }
    expect(ccPiState(undefined, real, state, 'run-1')).toBeUndefined();
    expect(ccPiState(home, real, undefined, 'run-1')).toBeUndefined();
    expect(ccPiWrites(undefined)).toEqual([]);
    expect(ccPiEnv(undefined)).toEqual({});
    // A path CC would read as a glob gives Pi nothing.
    expect(ccPiState(home, '/w/a[1]', state, 'run-1')).toBeUndefined();
    for (const dir of [home, state, real]) rmSync(dir, { recursive: true, force: true });
  });

  it("links the account's Pi configuration and sweeps only the directories whose session no longer runs", () => {
    const { home, source } = account();
    mkdirSync(join(source, 'trust.json.lock'));
    writeFileSync(join(source, 'trust.json.lock', 'owner'), '');
    const state = realpathSync(mkdtempSync(join(tmpdir(), 'boundary-pi-state-')));
    const workspace = realpathSync(tmpdir());
    const workspaceDir = join(state, piSessionDirName(workspace));
    const { sys, running } = system();
    const first = lease(leasePiAgentDir(home, workspace, state, { id: 'a', system: sys }));
    expect(lstatSync(first.state.agentDir).isDirectory()).toBe(true);
    expect(readdirSync(first.state.agentDir).sort()).toEqual(links);
    for (const name of links) expect(readlinkSync(join(first.state.agentDir, name))).toBe(join(source, name));
    expect(readFileSync(join(workspaceDir, 'a.owner'), 'utf8')).toBe('100\n');
    first.adopt(201);
    running.add(201);
    expect(readFileSync(join(workspaceDir, 'a.owner'), 'utf8')).toBe('201\n');
    // The session poisons its own directory and replaces it with a link to the account's.
    mkdirSync(join(first.state.agentDir, 'trust.json.lock'));
    writeFileSync(join(first.state.agentDir, 'trust.json.lock', 'poison'), '');
    // A crash left a directory without an owner record.
    mkdirSync(join(workspaceDir, 'crashed'));
    const second = lease(leasePiAgentDir(home, workspace, state, { id: 'b', system: sys }));
    expect(readdirSync(workspaceDir).sort()).toEqual(['a', 'a.owner', 'b', 'b.owner']);
    expect(existsSync(join(first.state.agentDir, 'trust.json.lock', 'poison'))).toBe(true);
    // Once its process is gone the directory is ownerless: the next start deletes it without following links.
    running.delete(201);
    rmSync(first.state.agentDir, { recursive: true });
    symlinkSync(source, first.state.agentDir);
    const third = lease(leasePiAgentDir(home, workspace, state, { id: 'c', system: sys }));
    expect(readdirSync(workspaceDir).sort()).toEqual(['b', 'b.owner', 'c', 'c.owner']);
    expect(readdirSync(source).sort()).toEqual(
      ['auth.json', 'extensions', 'models-store.json', 'sessions', 'settings.json', 'tmp', 'trust.json', 'trust.json.lock']);
    expect(existsSync(join(source, 'trust.json.lock', 'owner'))).toBe(true);
    // A session that ends removes its own directory and record.
    second.release();
    third.release();
    expect(readdirSync(workspaceDir)).toEqual([]);
    for (const dir of [home, state]) rmSync(dir, { recursive: true, force: true });
  });

  it("keeps a running session's Pi lock when another session of the workspace starts", () => {
    const { home } = account();
    const state = realpathSync(mkdtempSync(join(tmpdir(), 'boundary-pi-state-')));
    const workspace = realpathSync(mkdtempSync(join(tmpdir(), 'boundary-pi-ws-')));
    const { sys, running } = system();
    // A conversation and a scheduled run of the same workspace, both bounded, run at once.
    const conversation: string[] = [];
    const conversationLease = addCcWriteBoundarySettings(conversation, [workspace], { home, workspace, piStateRoot: state, piSystem: sys }, '/node')!;
    conversationLease.adopt(301);
    running.add(301);
    const conversationDir = JSON.parse(conversation[1]!).env.PI_CODING_AGENT_DIR as string;
    // Its Pi holds a lock the way proper-lockfile takes one (`realpath: false`: a directory beside the link).
    const lock = join(conversationDir, 'auth.json.lock');
    mkdirSync(lock);
    const held = lstatSync(lock).ino;
    const scheduled: string[] = [];
    const scheduledLease = addCcWriteBoundarySettings(scheduled, [workspace], { home, workspace, piStateRoot: state, piSystem: sys }, '/node')!;
    scheduledLease.adopt(302);
    running.add(302);
    const scheduledDir = JSON.parse(scheduled[1]!).env.PI_CODING_AGENT_DIR as string;
    expect(scheduledDir).not.toBe(conversationDir);
    expect(dirname(scheduledDir)).toBe(dirname(conversationDir));
    expect(JSON.parse(scheduled[1]!).sandbox.filesystem.allowWrite).not.toContain(conversationDir);
    // The second start leaves the first Pi's lock as it was, and that Pi still releases it.
    expect(lstatSync(lock).ino).toBe(held);
    mkdirSync(join(scheduledDir, 'auth.json.lock'));
    rmdirSync(lock);
    rmdirSync(join(scheduledDir, 'auth.json.lock'));
    // Each session's end removes only its own directory.
    scheduledLease.release();
    expect(existsSync(scheduledDir)).toBe(false);
    expect(readlinkSync(join(conversationDir, 'auth.json'))).toBe(join(home, '.pi', 'agent', 'auth.json'));
    conversationLease.release();
    expect(readdirSync(dirname(conversationDir))).toEqual([]);
    for (const dir of [home, state, workspace]) rmSync(dir, { recursive: true, force: true });
  });

  it("deletes or quarantines a directory its session made undeletable, and never refuses the next session for it", () => {
    const { home, source } = account();
    const state = realpathSync(mkdtempSync(join(tmpdir(), 'boundary-pi-state-')));
    const workspace = realpathSync(mkdtempSync(join(tmpdir(), 'boundary-pi-ws-')));
    const workspaceDir = join(state, piSessionDirName(workspace));
    const quarantine = join(state, '.quarantine');
    const { sys, running, stuck } = system();
    const warn = vi.spyOn(console, 'warn').mockImplementation(() => {});
    const warned = () => warn.mock.calls.map((call) => String((call[0] as { msg?: string }).msg));
    try {
      // The session's end: the closed directories are reopened and everything goes.
      const ended = lease(leasePiAgentDir(home, workspace, state, { id: 'ended', system: sys }));
      closeUp(ended.state.agentDir);
      expect(() => ended.release()).not.toThrow();
      expect(readdirSync(workspaceDir)).toEqual([]);
      // A session that crashed (its process gone) left the same; the next start deletes it and leases as usual.
      const crashed = lease(leasePiAgentDir(home, workspace, state, { id: 'crashed', system: sys }));
      crashed.adopt(201);
      closeUp(crashed.state.agentDir);
      const next = lease(leasePiAgentDir(home, workspace, state, { id: 'next', system: sys }));
      expect(readdirSync(workspaceDir).sort()).toEqual(['next', 'next.owner']);
      next.release();
      // What cannot be deleted even reopened is moved into Relay's quarantine, outside every workspace's directory.
      const immutable = lease(leasePiAgentDir(home, workspace, state, { id: 'immutable', system: sys }));
      closeUp(immutable.state.agentDir);
      stuck.add(immutable.state.agentDir);
      expect(() => immutable.release()).not.toThrow();
      expect(readdirSync(workspaceDir)).toEqual([]);
      const [held] = readdirSync(quarantine);
      expect(held).toMatch(/^immutable-/);
      expect(statSync(quarantine).mode & 0o777).toBe(0o700);
      expect(warned()).toContainEqual(expect.stringContaining(`moved to ${join(quarantine, held!)}`));
      // Each later sweep tries to delete it again; once it can, it goes.
      stuck.clear();
      const after = lease(leasePiAgentDir(home, workspace, state, { id: 'after', system: sys }));
      expect(readdirSync(quarantine)).toEqual([]);
      after.release();
      // With the quarantine unusable too, the directory and its record stay, are logged, and the next lease still comes.
      writeFileSync(join(state, '.quarantine-blocker'), '');
      rmSync(quarantine, { recursive: true });
      symlinkSync(join(state, '.quarantine-blocker'), quarantine);
      const kept = lease(leasePiAgentDir(home, workspace, state, { id: 'kept', system: sys }));
      kept.adopt(202);
      running.add(202);
      closeUp(kept.state.agentDir);
      stuck.add(kept.state.agentDir);
      running.delete(202);
      const later = lease(leasePiAgentDir(home, workspace, state, { id: 'later', system: sys }));
      expect(readdirSync(workspaceDir).sort()).toEqual(['kept', 'kept.owner', 'later', 'later.owner']);
      expect(warned()).toContainEqual(expect.stringContaining('neither deleted nor quarantined'));
      later.release();
      // The account's Pi directory is untouched throughout.
      expect(readdirSync(source).sort()).toEqual(['auth.json', 'extensions', 'models-store.json', 'sessions', 'settings.json', 'tmp', 'trust.json']);
    } finally {
      warn.mockRestore();
      execFileSync('chmod', ['-R', 'u+rwx', state]);
      for (const dir of [home, state, workspace]) rmSync(dir, { recursive: true, force: true });
    }
  });

  it('hands the directory to the account a linuxUser session runs as, or gives Pi nothing', () => {
    const { home } = account();
    // Relay's state root lies under a directory only its owner may pass through (0700), as an account's home does.
    const closed = realpathSync(mkdtempSync(join(tmpdir(), 'boundary-pi-closed-')));
    chmodSync(closed, 0o700);
    const state = join(closed, 'state');
    mkdirSync(state);
    const workspace = realpathSync(mkdtempSync(join(tmpdir(), 'boundary-pi-ws-')));
    const workspaceDir = join(state, piSessionDirName(workspace));
    const me = { name: 'tenant-member-0001', uid: process.getuid!(), gid: process.getgid!() };
    const { sys, chowned } = system();
    const args: string[] = [];
    const handed = addCcWriteBoundarySettings(args, [workspace],
      { home, workspace, piStateRoot: state, piUser: me.name, piLookupUser: () => me, piSystem: sys }, '/node')!;
    expect(chowned).toEqual([[handed.state.agentDir, me.uid, me.gid]]);
    expect(JSON.parse(args[1]!).env.PI_CODING_AGENT_DIR).toBe(handed.state.agentDir);
    // The two levels Relay owns above it open for search only.
    for (const dir of [state, workspaceDir]) expect(statSync(dir).mode & 0o777).toBe(0o711);
    handed.release();
    const nothing = (context: Partial<Parameters<typeof addCcWriteBoundarySettings>[2]>) => {
      const plain: string[] = [];
      const warn = vi.spyOn(console, 'warn').mockImplementation(() => {});
      expect(addCcWriteBoundarySettings(plain, [workspace], { home, workspace, piStateRoot: state, piUser: me.name, piSystem: sys, ...context }, '/node'))
        .toBeUndefined();
      const reason = String((warn.mock.calls[0]?.[0] as { reason?: string } | undefined)?.reason);
      warn.mockRestore();
      const settings = JSON.parse(plain[1]!);
      expect(settings.env).toEqual(ccSandboxEnv());
      expect(settings.sandbox.filesystem.allowWrite).toEqual([workspace]);
      expect(readdirSync(workspaceDir)).toEqual([]);
      return reason;
    };
    // Relay may not chown (it is not root): no Pi state rather than a directory the account cannot use.
    const denied = Object.assign(new Error('EPERM: operation not permitted, chown'), { code: 'EPERM' });
    expect(nothing({ piLookupUser: () => me, piSystem: { ...sys, chown: () => { throw denied; } } })).toContain('EPERM');
    // Another account, neither owner nor group of that directory, cannot pass through it.
    expect(statSync(closed).mode & 0o777).toBe(0o700);
    expect(statSync(closed).uid).not.toBe(2147483000);
    expect(statSync(closed).gid).not.toBe(2147483000);
    expect(nothing({ piLookupUser: () => ({ name: 'tenant-member-0002', uid: 2147483000, gid: 2147483000 }) }))
      .toBe(`tenant-member-0002 cannot pass through ${closed}`);
    // An account that does not exist.
    expect(nothing({ piLookupUser: () => { throw new Error('no such user'); } })).toContain('no such user');
    for (const dir of [home, closed, workspace]) rmSync(dir, { recursive: true, force: true });
  });

  it('gives Pi its state through Bash only, not through the file-tool hook', () => {
    const { home } = account();
    const state = realpathSync(mkdtempSync(join(tmpdir(), 'boundary-pi-state-')));
    const workspace = realpathSync(mkdtempSync(join(tmpdir(), 'boundary-pi-ws-')));
    const args: string[] = [];
    const held = addCcWriteBoundarySettings(args, [workspace], { home, workspace, piStateRoot: state }, '/node')!;
    const settings = JSON.parse(args[args.indexOf('--settings') + 1]);
    const pi = ccPiState(home, workspace, state, basename(settings.env.PI_CODING_AGENT_DIR))!;
    expect(held.state).toEqual(pi);
    expect(settings.sandbox.filesystem.allowWrite).toEqual([workspace, pi.agentDir, pi.sessionDir]);
    expect(settings.env).toEqual({ ...ccSandboxEnv(), ...ccPiEnv(pi) });
    expect(settings.hooks.PreToolUse[0].hooks[0].args[1]).toBe(JSON.stringify([workspace]));
    expect(settings.sandbox.filesystem.denyWrite).toEqual(ccPolicyPaths([workspace]));
    expect(readlinkSync(join(pi.agentDir, 'auth.json'))).toBe(join(pi.source, 'auth.json'));
    // Relay's own process owns it until the caller adopts the CC process.
    expect(readFileSync(`${pi.agentDir}.owner`, 'utf8')).toBe(`${process.pid}\n`);
    held.release();
    // Without Relay's state root Bash gets nothing for Pi.
    const plain: string[] = [];
    expect(addCcWriteBoundarySettings(plain, [workspace], { home, workspace }, '/node')).toBeUndefined();
    expect(JSON.parse(plain[1]!).sandbox.filesystem.allowWrite).toEqual([workspace]);
    expect(JSON.parse(plain[1]!).env).toEqual(ccSandboxEnv());
    for (const dir of [home, state, workspace]) rmSync(dir, { recursive: true, force: true });
  });

  // Needs @anthropic-ai/sandbox-runtime, which Relay does not install: SRT_CLI names its `srt` (`dist/cli.js`).
  it.skipIf(!process.env['SRT_CLI'] || process.platform !== 'darwin')(
    "leaves the account's Pi locks to every other Pi when a sandboxed session poisons its own", () => {
      const { home, source } = account();
      const state = realpathSync(mkdtempSync(join(tmpdir(), 'boundary-pi-state-')));
      const workspace = realpathSync(mkdtempSync(join(tmpdir(), 'boundary-pi-ws-')));
      const args: string[] = [];
      const held = addCcWriteBoundarySettings(args, [workspace], { home, workspace, piStateRoot: state }, '/node')!;
      const settings = JSON.parse(args[args.indexOf('--settings') + 1]);
      const pi = held.state;
      const srtSettings = join(state, 'srt.json');
      writeFileSync(srtSettings, JSON.stringify({ filesystem: { denyRead: [], ...settings.sandbox.filesystem },
        network: { allowedDomains: [], deniedDomains: [] } }));
      // proper-lockfile takes a lock with mkdir and releases it with rmdir; a file inside makes rmdir fail with ENOTEMPTY.
      const poison = (dir: string) => `mkdir ${dir}/trust.json.lock && : > ${dir}/trust.json.lock/poison && echo poisoned ${dir}`;
      const run = spawnSync('node', [process.env['SRT_CLI']!, '-s', srtSettings, '-c',
        `${poison(source)}; ${poison('"$PI_CODING_AGENT_DIR"')}; : > ${pi.sessionDir}.x; : > ${pi.agentDir}.owner; mkdir -p ${pi.sessionDir} && : > ${pi.sessionDir}/s.jsonl`],
      { cwd: workspace, env: { ...process.env, ...settings.env }, encoding: 'utf8' });
      expect(run.stdout).toContain(`poisoned ${pi.agentDir}`);
      expect(run.stdout).not.toContain(`poisoned ${source}`);
      expect(existsSync(join(pi.agentDir, 'trust.json.lock', 'poison'))).toBe(true);
      expect(existsSync(`${pi.sessionDir}.x`)).toBe(false);
      // The owner record lies outside the sandbox's writes.
      expect(readFileSync(`${pi.agentDir}.owner`, 'utf8')).toBe(`${process.pid}\n`);
      expect(existsSync(join(pi.sessionDir, 's.jsonl'))).toBe(true);
      // A second Pi takes and releases the account's lock as before.
      mkdirSync(join(source, 'trust.json.lock'));
      rmdirSync(join(source, 'trust.json.lock'));
      // The session's end drops its poisoned directory.
      held.release();
      expect(existsSync(pi.agentDir)).toBe(false);
      for (const dir of [home, state, workspace]) rmSync(dir, { recursive: true, force: true });
    });
});
