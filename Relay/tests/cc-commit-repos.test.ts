/**
 * Agent commit repositories and network domains inside the CC write boundary:
 * the sandbox opens exactly the Git paths a commit writes, keeps hooks and
 * config closed, adds the domains, and refuses malformed configuration.
 */
import { describe, expect, it, vi } from 'vitest';
import { execFileSync, spawnSync } from 'node:child_process';
import { existsSync, linkSync, mkdirSync, mkdtempSync, readFileSync, realpathSync, renameSync, rmSync, symlinkSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { basename, join, resolve } from 'node:path';
import {
  CLOSED_COMMON_GIT, CLOSED_WORKTREE_GIT, COMMON_GIT_WRITES, WORKTREE_GIT_WRITES, addCcWriteBoundarySettings, ccBashAccess, ccPiState, ccPiWrites, ccCommitRepoPaths, ccPolicyPaths, ccRepoAccessProblems, ccWriteBoundarySettings,
} from '../src/cc-write-boundary.js';
import { ConfigValidationError, validateConfig, type RelayConfig } from '../src/types.js';
import { createRouter } from '../src/router.js';

// The router reads the relay.json this test names, whatever path map the host declares.
vi.mock('../src/managed-paths.js', async (original) => ({
  ...(await original<typeof import('../src/managed-paths.js')>()),
  managedRelayPath: (_key: string, selected?: string) => selected,
}));

const GIT = ['-c', 'user.name=Commit Test', '-c', 'user.email=commit@example.invalid', '-c', 'gc.auto=0',
  '-c', 'maintenance.auto=false', '-c', 'core.hooksPath=', '-c', 'init.defaultBranch=main'];
const git = (cwd: string, ...args: string[]) => execFileSync('git', ['-C', cwd, ...GIT, ...args], { encoding: 'utf8' }).trim();

function repository(): { root: string; repo: string } {
  const root = realpathSync(mkdtempSync(join(tmpdir(), 'commit-repos-')));
  const repo = join(root, 'Coevolution');
  mkdirSync(join(repo, 'agent-space'), { recursive: true });
  git(repo, 'init', '-q');
  writeFileSync(join(repo, 'agent-space', 'a.txt'), 'a\n');
  git(repo, 'add', '.');
  git(repo, 'commit', '-qm', 'init');
  return { root, repo };
}

const SANDBOX = process.platform === 'darwin' && existsSync('/usr/bin/sandbox-exec') ? '/usr/bin/sandbox-exec' : undefined;

/** Run a command as CC's Bash sandbox would: no writes under `root` except the allowed paths, never the denied ones. */
function sandboxed(root: string, allow: string[], deny: string[], command: string[]) {
  const quoted = (paths: string[]) => paths.map((path) => `(subpath "${path}")`).join(' ');
  const profile = `(version 1)(allow default)(deny file-write* (subpath "${root}"))`
    + `(allow file-write* ${quoted(allow)})(deny file-write* ${quoted(deny)})`;
  return spawnSync(SANDBOX!, ['-p', profile, ...command], { encoding: 'utf8' });
}

const sandboxedGit = (root: string, allow: string[], deny: string[], cwd: string, ...args: string[]) =>
  sandboxed(root, allow, deny, ['git', '-C', cwd, ...GIT, ...args]);

describe('commit repositories in the CC write boundary', () => {
  it('opens only named Git data of a main checkout to Bash, with hooks, config, rebase state and redirects closed', () => {
    const { root, repo } = repository();
    const workspace = join(repo, 'agent-space');
    const gitDir = join(repo, '.git');
    const bash = ccBashAccess({ commitRepos: [repo], allowedDomains: ['github.com', 'api.github.com'] });
    const named = [...COMMON_GIT_WRITES, ...WORKTREE_GIT_WRITES].map((name) => join(gitDir, name));
    expect(bash.write).toEqual(named);
    for (const name of ['index', 'index.lock', 'HEAD', 'objects', 'refs', 'logs', 'packed-refs']) expect(bash.write).toContain(join(gitDir, name));
    for (const name of ['', 'rebase-merge', 'rebase-apply', 'sequencer']) expect(bash.write).not.toContain(join(gitDir, name));
    for (const name of ['hooks', 'config', 'config.worktree', 'commondir', 'modules', join('objects', 'info', 'alternates'),
      'rebase-merge', 'rebase-apply', 'sequencer']) {
      expect(bash.deny).toContain(join(gitDir, name));
    }
    const settings = ccWriteBoundarySettings([workspace], '/node', [], bash) as Record<string, any>;
    expect(settings.sandbox.filesystem.allowWrite).toEqual([workspace, ...named]);
    expect(settings.sandbox.filesystem.denyWrite).toEqual([...ccPolicyPaths([workspace]), ...bash.deny]);
    expect(settings.sandbox.network).toEqual({ allowedDomains: ['github.com', 'api.github.com'] });
    // The file-tool hook keeps the session roots: Write and Edit still cannot reach .git.
    expect(settings.hooks.PreToolUse[0].hooks[0].args.slice(1)).toEqual([JSON.stringify([workspace]), '[]']);
    rmSync(root, { recursive: true, force: true });
  });

  it('opens only named files of a linked worktree administrative directory and the shared object, ref and log stores', () => {
    const { root, repo } = repository();
    const tree = join(root, 'wt');
    git(repo, 'worktree', 'add', '-q', '-b', 'agent/wt', tree);
    const common = join(repo, '.git');
    const admin = git(tree, 'rev-parse', '--absolute-git-dir');
    expect(admin).toBe(join(common, 'worktrees', 'wt'));
    const { write, deny } = ccCommitRepoPaths([tree]);
    expect(write).toEqual([...COMMON_GIT_WRITES.map((name) => join(common, name)),
      ...WORKTREE_GIT_WRITES.map((name) => join(admin, name)), join(admin, 'logs')]);
    for (const path of [common, admin, join(common, 'worktrees')]) expect(write).not.toContain(path);
    for (const path of [join(tree, '.git'), join(admin, 'commondir'), join(admin, 'gitdir'), join(admin, 'config.worktree'),
      join(admin, 'rebase-merge'), join(admin, 'rebase-apply'), join(admin, 'sequencer'),
      join(common, 'hooks'), join(common, 'config')]) expect(deny).toContain(path);
    rmSync(root, { recursive: true, force: true });
  });

  /**
   * Under the Seatbelt profile CC's Bash sandbox applies: everyday Git runs
   * without even a non-fatal refusal, and the paths that run commands or
   * hold executable rebase/sequencer state cannot be written.
   */
  function exercise(root: string, allow: string[], deny: string[], cwd: string, own: string) {
    writeFileSync(join(cwd, 'a.txt'), `changed in ${cwd}\n`);
    const start = git(cwd, 'rev-parse', '--abbrev-ref', 'HEAD');
    const remote = join(root, `remote-${start.replace(/\W/g, '-')}.git`);
    git(root, 'init', '-q', '--bare', remote);
    git(remote, 'fetch', '-q', git(cwd, 'rev-parse', '--show-toplevel'), `+${start}:refs/heads/upstream`);
    for (const args of [['add', '-A'], ['commit', '-qm', 'agent commit'], ['branch', 'agent/topic'],
      ['update-ref', 'refs/remotes/origin/topic', 'HEAD'], ['checkout', '-q', 'agent/topic'],
      ['commit', '-q', '--allow-empty', '-m', 'second'], ['reset', '-q', '--soft', 'HEAD~1'], ['reset', '-q', '--hard', 'HEAD'],
      ['commit', '-q', '--allow-empty', '-m', 'topic commit'], ['checkout', '-q', start], ['cherry-pick', '--allow-empty', 'agent/topic'], ['branch', '-D', 'agent/topic'],
      ['fetch', '-q', remote, '+refs/heads/*:refs/remotes/origin/*'], ['pack-refs', '--all']]) {
      const run = sandboxedGit(root, allow, deny, cwd, ...args);
      expect(run.status, `${args.join(' ')}: ${run.stderr}`).toBe(0);
      // Not even a non-fatal refusal (Git warns and goes on when it cannot lock AUTO_MERGE). Deleting a branch
      // also drops its section from the closed config: that one warning is expected and the branch is gone.
      const refusals = run.stderr.split('\n').filter((line) => /Operation not permitted/.test(line));
      if (args[1] === '-D') expect(refusals.every((line) => line.includes('.git/config')), run.stderr).toBe(true);
      else expect(refusals, args.join(' ')).toEqual([]);
    }
    expect(git(cwd, 'branch', '--list', 'agent/topic')).toBe('');
    expect(git(cwd, 'rev-parse', '--verify', '-q', 'refs/remotes/origin/upstream')).not.toBe('');
    expect(git(cwd, 'log', '-2', '--format=%s').split('\n')).toEqual(['topic commit', 'agent commit']);
    // A todo with `exec` would run outside the sandbox on the next `--continue`; none can be written.
    for (const file of [join('rebase-merge', 'git-rebase-todo'), join('rebase-apply', 'next'), join('sequencer', 'todo'),
      join('sequencer', 'opts'), 'hooks/pre-commit', 'config']) {
      const common = realpathSync(resolve(cwd, git(cwd, 'rev-parse', '--git-common-dir')));
      const target = join(file === 'hooks/pre-commit' || file === 'config' ? common : own, file);
      const run = sandboxed(root, allow, deny,
        ['sh', '-c', `mkdir -p "$(dirname '${target}')" && printf 'exec touch /tmp/pwned\\n' > '${target}'`]);
      expect(run.status, target).not.toBe(0);
      if (file !== 'config') expect(existsSync(target), target).toBe(false);
    }
    expect(sandboxedGit(root, allow, deny, cwd, 'config', 'core.hooksPath', '/tmp/h').status).not.toBe(0);
  }

  it('lets everyday Git run in a main checkout under that sandbox, and not hooks, config or rebase state', () => {
    // Seatbelt is the sandbox CC uses on macOS; elsewhere only the path set is checked (above).
    if (!SANDBOX) return;
    const { root, repo } = repository();
    const workspace = join(repo, 'agent-space');
    const { write, deny } = ccCommitRepoPaths([repo]);
    const denied = [...ccPolicyPaths([workspace]), ...deny];
    // Without the repository, the commit cannot take the index lock.
    writeFileSync(join(workspace, 'a.txt'), 'changed\n');
    expect(sandboxedGit(root, [workspace], denied, workspace, 'add', 'a.txt').status).not.toBe(0);
    // The session root is the workspace inside the checkout, not the repository root: only the list opens .git.
    exercise(root, [workspace, ...write], denied, workspace, join(repo, '.git'));
    rmSync(root, { recursive: true, force: true });
  });

  it('lets everyday Git run in a linked worktree under that sandbox, and not hooks, config, rebase state or another worktree', () => {
    if (!SANDBOX) return;
    const { root, repo } = repository();
    const tree = join(root, 'wt');
    git(repo, 'worktree', 'add', '-q', '-b', 'agent/wt', tree);
    git(repo, 'worktree', 'add', '-q', '-b', 'agent/other', join(root, 'other'));
    const admin = git(tree, 'rev-parse', '--absolute-git-dir');
    const { write, deny } = ccCommitRepoPaths([tree]);
    exercise(root, [join(tree, 'agent-space'), ...write], [...ccPolicyPaths([join(tree, 'agent-space')]), ...deny],
      join(tree, 'agent-space'), admin);
    for (const name of ['commondir', 'gitdir', 'config.worktree']) {
      expect(sandboxed(root, [tree, ...write], deny, ['sh', '-c', `printf x > '${join(admin, name)}'`]).status, name).not.toBe(0);
    }
    writeFileSync(join(root, 'other', 'agent-space', 'a.txt'), 'foreign\n');
    expect(sandboxedGit(root, [tree, ...write], deny, join(root, 'other'), 'add', '-A').status).not.toBe(0);
    rmSync(root, { recursive: true, force: true });
  });

  it('keeps a worktree added after the session started closed, with the main checkout and a linked worktree configured', () => {
    const { root, repo } = repository();
    const tree = join(root, 'own');
    git(repo, 'worktree', 'add', '-q', '-b', 'agent/own', tree);
    const common = join(repo, '.git');
    const access = ccCommitRepoPaths([repo, tree]);
    // Another session adds its worktree after this one started.
    git(repo, 'worktree', 'add', '-q', '-b', 'agent/later', join(root, 'later'));
    const later = join(common, 'worktrees', 'later');
    if (SANDBOX) {
      const allow = [join(repo, 'agent-space'), ...access.write];
      for (const name of ['HEAD', 'index', 'gitdir', 'config.worktree']) {
        const run = sandboxed(root, allow, access.deny, ['sh', '-c', `printf x > '${join(later, name)}'`]);
        expect(run.status, name).not.toBe(0);
      }
      expect(git(join(root, 'later'), 'rev-parse', '--abbrev-ref', 'HEAD')).toBe('agent/later');
      // Its own worktree and the main checkout still commit.
      writeFileSync(join(tree, 'agent-space', 'a.txt'), 'own\n');
      for (const args of [['add', '-A'], ['commit', '-qm', 'own commit']]) {
        expect(sandboxedGit(root, [tree, ...allow], access.deny, tree, ...args).status).toBe(0);
      }
      writeFileSync(join(repo, 'agent-space', 'a.txt'), 'main\n');
      for (const args of [['add', '-A'], ['commit', '-qm', 'main commit']]) {
        expect(sandboxedGit(root, allow, access.deny, repo, ...args).status).toBe(0);
      }
    }
    expect(access.write).not.toContain(common);
    expect(access.write).not.toContain(join(common, 'worktrees'));
    expect(access.write).toContain(join(common, 'worktrees', 'own', 'HEAD'));
    expect(access.write).not.toContain(join(common, 'worktrees', 'own'));
    expect(access.write.some((path) => later === path || later.startsWith(`${path}/`))).toBe(false);
    rmSync(root, { recursive: true, force: true });
  });

  /** The core.hooksPath the repository's own config file holds, as an unsandboxed Git would read it. */
  const hooksPath = (repo: string) => spawnSync('git', ['config', '--file', join(repo, '.git', 'config'), '--get', 'core.hooksPath'],
    { encoding: 'utf8' }).stdout.trim();

  it('refuses a repository whose config is a link into opened Git data, so Bash cannot rewrite it through objects', () => {
    const { root, repo } = repository();
    const gitDir = join(repo, '.git');
    // An unsandboxed actor left config as a link to a file under objects/, which the session may write.
    renameSync(join(gitDir, 'config'), join(gitDir, 'objects', 'policy'));
    symlinkSync(join(gitDir, 'objects', 'policy'), join(gitDir, 'config'));
    let access: ReturnType<typeof ccCommitRepoPaths> | undefined;
    let refusal = '';
    try { access = ccCommitRepoPaths([repo]); } catch (error) { refusal = (error as Error).message; }
    if (SANDBOX && access) {
      const workspace = join(repo, 'agent-space');
      sandboxed(root, [workspace, ...access.write], [...ccPolicyPaths([workspace]), ...access.deny],
        ['sh', '-c', `printf '[core]\\n\\thooksPath = /tmp/injected\\n' >> '${join(gitDir, 'objects', 'policy')}'`]);
    }
    expect(hooksPath(repo)).toBe('');
    expect(refusal).toBe(`commitRepos: ${join(gitDir, 'config')} is a symbolic link; Git does not create one there`);
    expect(() => addCcWriteBoundarySettings([], [join(repo, 'agent-space')],
      { workspace: join(repo, 'agent-space'), repoAccess: { commitRepos: [repo] } }, '/node')).toThrow('CC write boundary refused');
    rmSync(root, { recursive: true, force: true });
  });

  it('refuses links and shared inodes anywhere in the Git data, in both layouts, before opening anything', () => {
    const cases: Array<[string, (gitDir: string, root: string) => void, string]> = [
      ['hooks directory', (g) => { mkdirSync(join(g, 'objects', 'h')); rmSync(join(g, 'hooks'), { recursive: true, force: true });
        symlinkSync(join(g, 'objects', 'h'), join(g, 'hooks')); }, 'hooks is a symbolic link'],
      ['hook file', (g) => { mkdirSync(join(g, 'hooks'), { recursive: true }); writeFileSync(join(g, 'objects', 'x'), '');
        symlinkSync(join(g, 'objects', 'x'), join(g, 'hooks', 'pre-commit')); }, 'pre-commit is a symbolic link'],
      ['alternates', (g, r) => { mkdirSync(join(g, 'objects', 'info'), { recursive: true });
        symlinkSync(join(r, 'elsewhere'), join(g, 'objects', 'info', 'alternates')); }, 'alternates is a symbolic link'],
      ['config hard link', (g) => linkSync(join(g, 'config'), join(g, 'objects', 'shared')), 'has 2 hard links'],
      ['alternates hard link', (g) => { mkdirSync(join(g, 'objects', 'info'), { recursive: true });
        writeFileSync(join(g, 'objects', 'info', 'alternates'), ''); linkSync(join(g, 'objects', 'info', 'alternates'),
        join(g, 'objects', 'shared')); }, 'alternates has 2 hard links'],
      ['rebase state', (g, r) => { mkdirSync(join(r, 'todo')); symlinkSync(join(r, 'todo'), join(g, 'rebase-merge')); },
        'rebase-merge is a symbolic link'],
      ['info', (g, r) => { rmSync(join(g, 'info'), { recursive: true, force: true }); mkdirSync(join(r, 'info'));
        symlinkSync(join(r, 'info'), join(g, 'info')); }, 'info is a symbolic link'],
      // Opened data that leads elsewhere: the grant would follow it.
      ['objects', (g, r) => { renameSync(join(g, 'objects'), join(r, 'objects')); symlinkSync(join(r, 'objects'), join(g, 'objects')); },
        'is not a Git directory'],
      ['ref', (g, r) => symlinkSync(join(r, 'outside'), join(g, 'refs', 'heads', 'planted')), 'planted is a symbolic link'],
      ['reflog', (g, r) => { rmSync(join(g, 'logs', 'HEAD')); symlinkSync(join(r, '.zshrc'), join(g, 'logs', 'HEAD')); },
        'HEAD is a symbolic link'],
      ['commit message', (g, r) => { rmSync(join(g, 'COMMIT_EDITMSG'), { force: true }); symlinkSync(join(r, '.zshrc'), join(g, 'COMMIT_EDITMSG')); },
        'COMMIT_EDITMSG is a symbolic link'],
    ];
    for (const [name, plant, reason] of cases) {
      const { root, repo } = repository();
      plant(join(repo, '.git'), root);
      expect(() => ccCommitRepoPaths([repo]), name).toThrow(reason);
      rmSync(root, { recursive: true, force: true });
    }
    // A linked worktree: its administrative files and the common directory it shares.
    const linked: Array<[string, (admin: string, common: string, root: string) => void, string]> = [
      ['config.worktree', (a, c) => { writeFileSync(join(c, 'objects', 'wt-config'), ''); symlinkSync(join(c, 'objects', 'wt-config'),
        join(a, 'config.worktree')); }, 'config.worktree is a symbolic link'],
      ['sequencer', (a, _c, r) => { mkdirSync(join(r, 'seq')); symlinkSync(join(r, 'seq'), join(a, 'sequencer')); }, 'sequencer is a symbolic link'],
      ['worktree reflog', (a, _c, r) => { rmSync(join(a, 'logs', 'HEAD'), { force: true }); mkdirSync(join(a, 'logs'), { recursive: true });
        symlinkSync(join(r, '.zshrc'), join(a, 'logs', 'HEAD')); }, 'HEAD is a symbolic link'],
      ['common config', (_a, c) => linkSync(join(c, 'config'), join(c, 'objects', 'shared')), 'has 2 hard links'],
    ];
    for (const [name, plant, reason] of linked) {
      const { root, repo } = repository();
      const tree = join(root, 'wt');
      git(repo, 'worktree', 'add', '-q', '-b', 'agent/wt', tree);
      plant(git(tree, 'rev-parse', '--absolute-git-dir'), join(repo, '.git'), root);
      expect(() => ccCommitRepoPaths([tree]), name).toThrow(reason);
      rmSync(root, { recursive: true, force: true });
    }
  });

  it('denies every closed entry by name and real path, and lets no session turn one into a link or a hard link', () => {
    const { root, repo } = repository();
    const gitDir = join(repo, '.git');
    const workspace = join(repo, 'agent-space');
    const { write, deny } = ccCommitRepoPaths([repo]);
    for (const name of [...CLOSED_COMMON_GIT, ...CLOSED_WORKTREE_GIT]) {
      expect(deny).toContain(join(gitDir, name));
      expect(deny).toContain(realpathSync(gitDir) === gitDir ? join(gitDir, name) : join(realpathSync(gitDir), name));
    }
    if (SANDBOX) {
      const config = readFileSync(join(gitDir, 'config'), 'utf8');
      const allow = [workspace, ...write];
      const denied = [...ccPolicyPaths([workspace]), ...deny];
      for (const command of [
        `rm -f '${join(gitDir, 'config')}'; ln -s '${join(gitDir, 'objects', 'policy')}' '${join(gitDir, 'config')}'`,
        `ln '${join(gitDir, 'config')}' '${join(gitDir, 'objects', 'shared')}'`,
        `mv '${join(gitDir, 'hooks')}' '${join(gitDir, 'objects', 'hooks')}'`,
        `mkdir -p '${join(gitDir, 'objects', 'info')}' && ln -s /tmp '${join(gitDir, 'objects', 'info', 'alternates')}'`,
        // A link the session may create in opened data still cannot carry a write past the boundary.
        `ln -s '${join(gitDir, 'config')}' '${join(gitDir, 'objects', 'to-config')}' && printf x >> '${join(gitDir, 'objects', 'to-config')}'`,
        `ln -s '${join(root, 'outside')}' '${join(gitDir, 'refs', 'to-outside')}' && printf x > '${join(gitDir, 'refs', 'to-outside')}'`,
      ]) expect(sandboxed(root, allow, denied, ['sh', '-c', command]).status, command).not.toBe(0);
      expect(existsSync(join(root, 'outside'))).toBe(false);
      expect(readFileSync(join(gitDir, 'config'), 'utf8')).toBe(config);
      expect(existsSync(join(gitDir, 'objects', 'shared'))).toBe(false);
      // The links left behind refuse the next start.
      expect(() => ccCommitRepoPaths([repo])).toThrow('is a symbolic link');
    }
    rmSync(root, { recursive: true, force: true });
  });

  it('refuses entries that are relative, carry .., are not normalized, or are not a repository root', () => {
    const { root, repo } = repository();
    for (const [entry, reason] of [
      ['Coevolution', 'not an absolute path'], [`${repo}/../Coevolution`, 'contains ..'], [`${repo}/`, 'normalized'],
      [`${root}//Coevolution`, 'normalized'], ['/', 'normalized'],
    ] as const) {
      expect(ccRepoAccessProblems({ commitRepos: [entry] })[0]).toContain(reason);
      expect(() => addCcWriteBoundarySettings([], [repo], { workspace: repo, repoAccess: { commitRepos: [entry] } }, '/node'))
        .toThrow(`CC write boundary refused: commitRepos entry ${JSON.stringify(entry)}`);
    }
    const refused = (entry: string) => () => ccCommitRepoPaths([entry]);
    expect(refused(join(repo, 'agent-space'))).toThrow('not a repository root');
    expect(refused(join(root, 'missing'))).toThrow('does not exist');
    symlinkSync(repo, join(root, 'link'));
    expect(refused(join(root, 'link'))).toThrow('through a link');
    // A .git that is a link, or a worktree link to an administrative directory that does not name it back.
    const linked = join(root, 'linked');
    mkdirSync(linked);
    symlinkSync(join(repo, '.git'), join(linked, '.git'));
    expect(refused(linked)).toThrow('neither a Git directory nor a worktree link');
    const forged = join(root, 'forged');
    mkdirSync(forged);
    git(repo, 'worktree', 'add', '-q', '-b', 'agent/real', join(root, 'real'));
    writeFileSync(join(forged, '.git'), `gitdir: ${join(repo, '.git', 'worktrees', 'real')}\n`);
    expect(refused(forged)).toThrow('does not belong to this worktree');
    writeFileSync(join(forged, '.git'), `gitdir: ${join(repo, '.git')}\n`);
    expect(refused(forged)).toThrow('does not name a worktree administrative directory');
    expect(ccRepoAccessProblems({ commitRepos: 'x' })).toEqual(['commitRepos must be an array of strings']);
    rmSync(root, { recursive: true, force: true });
  });

  it('refuses malformed domains', () => {
    for (const domain of ['https://github.com', '*', 'GitHub.com', 'github.com:443', 'github.com/x', 'localhost', '127.0.0.1']) {
      expect(ccRepoAccessProblems({ allowedDomains: [domain] }), domain).toHaveLength(1);
    }
    expect(ccRepoAccessProblems({ allowedDomains: ['github.com', 'api.github.com', '*.githubusercontent.com'] })).toEqual([]);
  });

  it('leaves an Agent without commit repositories or domains unchanged', () => {
    expect(ccBashAccess(undefined)).toEqual({ write: [], deny: [], allowedDomains: [], env: {} });
    expect(ccBashAccess({ commitRepos: [], allowedDomains: [] })).toEqual({ write: [], deny: [], allowedDomains: [], env: {} });
    const plain = ccWriteBoundarySettings(['/w'], '/node');
    expect(ccWriteBoundarySettings(['/w'], '/node', [], ccBashAccess(undefined))).toEqual(plain);
    expect((plain['sandbox'] as Record<string, unknown>)['network']).toBeUndefined();
    const args: string[] = [];
    addCcWriteBoundarySettings(args, ['/w'], { workspace: '/w', repoAccess: undefined }, '/node');
    expect(JSON.parse(args[1]!)).toEqual(plain);
  });

  it('accepts its own --settings grant and still refuses a user-level one', () => {
    const { root, repo } = repository();
    const home = join(root, 'home');
    mkdirSync(join(home, '.claude'), { recursive: true });
    const workspace = join(repo, 'agent-space');
    const repoAccess = { commitRepos: [repo], allowedDomains: ['github.com'] };
    const args = ['--settings', JSON.stringify({ sandbox: { filesystem: { allowWrite: ['/elsewhere'] } } })];
    const piStateRoot = join(root, 'pi-state');
    addCcWriteBoundarySettings(args, [workspace], { home, workspace, repoAccess, piStateRoot }, '/node')!.release();
    const settings = JSON.parse(args[1]!);
    const pi = ccPiState(home, workspace, piStateRoot, basename(settings.env.PI_CODING_AGENT_DIR));
    expect(settings.sandbox.filesystem.allowWrite)
      .toEqual([workspace, ...ccCommitRepoPaths([repo]).write, ...ccPiWrites(pi)]);
    expect(settings.sandbox.network.allowedDomains).toEqual(['github.com']);
    writeFileSync(join(home, '.claude', 'settings.json'), JSON.stringify({
      sandbox: { filesystem: { allowWrite: [join(repo, '.git')] }, network: { allowedDomains: ['github.com'] } } }));
    expect(() => addCcWriteBoundarySettings([], [workspace], { home, workspace, repoAccess }, '/node'))
      .toThrow('sandbox.filesystem.allowWrite');
    rmSync(root, { recursive: true, force: true });
  });
});

describe('commit repository configuration', () => {
  const config = (agent: Record<string, unknown>): RelayConfig => ({ users: [{ userId: 'owner', chatId: '1', defaultAgent: 'vigil',
    agents: [{ id: 'vigil', name: 'Vigil', emoji: 'v', botToken: 't', workspace: '/w', ccWriteBoundary: true, ...agent } as never] }] });

  it('validates the fields at start and on reload, keeping the running configuration on a bad reload', () => {
    expect(() => validateConfig(config({ commitRepos: ['/Users/owner/Coevolution'], allowedDomains: ['github.com'] }))).not.toThrow();
    expect(() => validateConfig(config({}))).not.toThrow();
    expect(() => validateConfig(config({ commitRepos: ['relative'] }))).toThrow(ConfigValidationError);
    expect(() => validateConfig(config({ allowedDomains: ['https://github.com'] }))).toThrow('allowedDomains');
    const dir = realpathSync(mkdtempSync(join(tmpdir(), 'commit-repos-config-')));
    const file = join(dir, 'relay.json');
    writeFileSync(file, JSON.stringify(config({ commitRepos: ['/r'] })));
    const router = createRouter(file);
    writeFileSync(file, JSON.stringify(config({ commitRepos: ['/r/../etc'] })));
    expect(() => router.reload()).toThrow('contains ..');
    expect(router.getAgents('owner')[0]!.commitRepos).toEqual(['/r']);
    writeFileSync(file, JSON.stringify(config({ commitRepos: ['/s'], allowedDomains: ['github.com'] })));
    router.reload();
    expect(router.getAgents('owner')[0]).toMatchObject({ commitRepos: ['/s'], allowedDomains: ['github.com'] });
    rmSync(dir, { recursive: true, force: true });
  });
});
