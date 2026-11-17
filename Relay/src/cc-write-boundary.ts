/**
 * Hard write boundary for CC sessions of an Agent with `ccWriteBoundary`.
 *
 * CC runs with --dangerously-skip-permissions, so the prompt alone does not
 * stop a write. Two CC mechanisms apply in that mode as well:
 * - a PreToolUse hook (runtime/cc-write-boundary.mjs) denies Edit, Write,
 *   MultiEdit and NotebookEdit outside the write roots;
 * - CC's Bash sandbox (macOS Seatbelt) lets Bash and its child processes write
 *   only inside the working directory, the write roots and the sandbox's own
 *   temporary directory; unsandboxed fallback is refused, and CC exits at start
 *   when the sandbox is unavailable.
 * Inside the roots, both deny the paths that change execution policy (see
 * ccPolicyPaths), so a session cannot add a hook or switch the boundary off.
 * A workspace the kernel allocated to a WI the session holds adds its
 * worktree and the Git paths a commit there writes as roots, and the paths
 * the kernel keeps closed inside them as further denials (ccWriteDenials).
 * An Agent's configured commit repositories (`commitRepos`) open to Bash, not
 * to the file tools, the Git paths a commit, branch, fetch or push there
 * writes, with the paths that redirect Git or run commands kept closed
 * (ccCommitRepoPaths); its `allowedDomains` are added to the sandbox network
 * allowlist. Both come from relay.json, not from a WI grant.
 * Bash also runs Pi against an agent directory Relay leases to the one
 * session and may write it and the workspace's Pi session directory
 * (ccPiState, leasePiAgentDir); the account's shared Pi agent directory stays
 * closed.
 * Reads stay open: the shared repositories, SSOT and granted read paths are
 * named in the prompt only; none of them becomes a write root.
 */
import { execFileSync } from 'node:child_process';
import { randomUUID } from 'node:crypto';
import {
  chmodSync, chownSync, existsSync, lstatSync, mkdirSync, readdirSync, readFileSync, realpathSync, renameSync, rmSync, statSync, symlinkSync,
  writeFileSync,
} from 'node:fs';
import { basename, dirname, isAbsolute, join, normalize, resolve, sep } from 'node:path';
import { fileURLToPath } from 'node:url';
import { resolveRelayDataDir } from './config-paths.js';
import { relayChildEnv } from './relay-secrets.js';
import { mergeCcSettingsArg } from './session-wi-guard.js';
import { guardAccountStatePath } from './test-account-path-guard.js';

export const WRITE_BOUNDARY_MATCHER = 'Edit|Write|MultiEdit|NotebookEdit';
const BOUNDARY_SCRIPT = join(dirname(fileURLToPath(import.meta.url)), '..', 'runtime', 'cc-write-boundary.mjs');

/** A root as the sandbox sees it: the real path when it exists. */
function realRoot(path: string): string {
  try { return realpathSync(path); } catch { return path; }
}

/** A path as the sandbox sees it once created: its deepest existing ancestor at its real path. */
function realPath(path: string): string {
  const parent = dirname(path);
  if (existsSync(path) || parent === path) return realRoot(path);
  return join(realPath(parent), basename(path));
}

interface AllocatedWrite { write: string[]; deny: string[] }

/** The workspace (with its .relay transit), the outbox, the write-mode grants and the allocated workspaces. */
export function ccWriteRoots(input: {
  workspace: string; outbox?: string; sessionAccess?: { path: string; mode: 'read' | 'write' }[];
  allocatedWorkspaces?: AllocatedWrite[];
}): string[] {
  return [...new Set([input.workspace, ...(input.outbox ? [input.outbox] : []),
    ...(input.sessionAccess ?? []).filter((row) => row.mode === 'write').map((row) => row.path),
    ...(input.allocatedWorkspaces ?? []).flatMap((row) => row.write)].map(realRoot))];
}

/** Paths the kernel keeps closed inside allocated workspaces' roots, as named and as real paths. */
export function ccWriteDenials(allocatedWorkspaces: AllocatedWrite[] | undefined): string[] {
  return [...new Set((allocatedWorkspaces ?? []).flatMap((row) => row.deny).flatMap((path) => [path, realRoot(path)]))];
}

/** The Relay installation that holds the boundary hook; no session writes it. */
export const RELAY_INSTALLATION = dirname(dirname(BOUNDARY_SCRIPT));

/**
 * Paths inside the roots that change execution policy, denied to Bash as the
 * hook denies them to the file tools: CC configuration (settings, hooks,
 * skills, agents), MCP declarations, Git hooks and config, and the Relay
 * installation.
 */
export function ccPolicyPaths(roots: string[], denials: string[] = []): string[] {
  // An entry point that is a link (`.claude -> config`) is denied together with its real target.
  const entries = roots.flatMap((root) => ['.claude', '.mcp.json', join('.git', 'hooks'), join('.git', 'config')]
    .map((name) => join(root, name)));
  return [...new Set([...entries.flatMap((entry) => [entry, realRoot(entry)]), realRoot(RELAY_INSTALLATION), ...denials])];
}

/** An Agent's standing repository access from relay.json; absent or empty opens nothing. */
export interface CcRepoAccess { commitRepos?: string[]; allowedDomains?: string[] }

/** What Bash alone gets beyond the session roots: commit-repository Git paths, their closed paths, network domains, Pi's state (ccPiState). */
export interface CcBashAccess { write: string[]; deny: string[]; allowedDomains: string[]; env: Record<string, string> }

const DOMAIN = /^(\*\.)?([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$/;

/**
 * Why an Agent's `commitRepos` / `allowedDomains` are malformed; [] when they
 * are absent, empty or well formed. A repository is an absolute, normalized
 * path without `..`; a domain is a lowercase host name, optionally `*.`-prefixed.
 * Whether a path is a repository root is decided at session start
 * (ccCommitRepoPaths), where the file system is read.
 */
export function ccRepoAccessProblems(access: { commitRepos?: unknown; allowedDomains?: unknown }): string[] {
  const problems: string[] = [];
  const list = (name: string, value: unknown, check: (item: string) => string | undefined) => {
    if (value === undefined) return;
    if (!Array.isArray(value)) { problems.push(`${name} must be an array of strings`); return; }
    for (const item of value) {
      const problem = typeof item === 'string' ? check(item) : 'is not a string';
      if (problem) problems.push(`${name} entry ${JSON.stringify(item)} ${problem}`);
    }
  };
  list('commitRepos', access.commitRepos, (path) => {
    if (!isAbsolute(path)) return 'is not an absolute path';
    if (path.split(/[\\/]/).includes('..')) return 'contains ..';
    if (path === sep || path !== normalize(path) || path.endsWith(sep)) return 'is not a normalized repository root';
    return undefined;
  });
  list('allowedDomains', access.allowedDomains, (domain) => (DOMAIN.test(domain) ? undefined : 'is not a host name'));
  return problems;
}

type GitLayout =
  | { kind: 'main'; repo: string; common: string }
  | { kind: 'linked'; repo: string; admin: string; common: string };

/** The first line of a Git pointer file after its prefix, else undefined. */
function pointer(file: string, prefix = ''): string | undefined {
  try {
    const line = readFileSync(file, 'utf8').split('\n', 1)[0]!.trim();
    return line.startsWith(prefix) && line.length > prefix.length ? line.slice(prefix.length) : undefined;
  } catch { return undefined; }
}

function isDirectory(path: string): boolean {
  try { return lstatSync(path).isDirectory(); } catch { return false; }
}

/**
 * Where a configured repository root keeps its Git data, proven on the host.
 * The root must be its own real path (no link redirects it) and hold `.git`
 * itself: a directory (main checkout) or, for a linked worktree, a file naming
 * an administrative directory directly under `<common>/worktrees/` that names
 * the worktree back. A path inside a repository but not at its root, or one
 * whose `.git` is a link, is refused.
 */
function gitLayout(repo: string): GitLayout {
  const [problem] = ccRepoAccessProblems({ commitRepos: [repo] });
  if (problem) throw new Error(problem);
  let real: string;
  try { real = realpathSync(repo); } catch { throw new Error(`commitRepos entry ${repo} does not exist`); }
  if (real !== repo) throw new Error(`commitRepos entry ${repo} is reached through a link (real path ${real})`);
  const dotGit = join(repo, '.git');
  let entry;
  try { entry = lstatSync(dotGit); } catch { throw new Error(`commitRepos entry ${repo} is not a repository root (no .git)`); }
  if (entry.isDirectory()) {
    if (!existsSync(join(dotGit, 'HEAD')) || !isDirectory(join(dotGit, 'objects'))) {
      throw new Error(`commitRepos entry ${repo}: ${dotGit} is not a Git directory`);
    }
    if (existsSync(join(dotGit, 'commondir'))) throw new Error(`commitRepos entry ${repo}: ${dotGit} redirects to another common directory`);
    return { kind: 'main', repo, common: dotGit };
  }
  if (!entry.isFile()) throw new Error(`commitRepos entry ${repo}: ${dotGit} is neither a Git directory nor a worktree link`);
  const named = pointer(dotGit, 'gitdir: ');
  let admin: string;
  try { admin = realpathSync(resolve(repo, named ?? '')); } catch { admin = ''; }
  if (!named || !admin || admin !== resolve(repo, named) || !isDirectory(admin) || basename(dirname(admin)) !== 'worktrees') {
    throw new Error(`commitRepos entry ${repo}: ${dotGit} does not name a worktree administrative directory`);
  }
  const common = dirname(dirname(admin));
  const commonNamed = pointer(join(admin, 'commondir'));
  let commonReal = '';
  try { commonReal = commonNamed ? realpathSync(resolve(admin, commonNamed)) : ''; } catch { /* refused below */ }
  if (commonReal !== common || pointer(join(admin, 'gitdir')) !== dotGit) {
    throw new Error(`commitRepos entry ${repo}: ${admin} does not belong to this worktree`);
  }
  return { kind: 'linked', repo, admin, common };
}

/** Shared Git data a commit, branch, fetch or push writes in the common directory. */
export const COMMON_GIT_WRITES = ['objects', 'refs', 'logs', 'packed-refs', 'packed-refs.lock', 'packed-refs.new'];

/** Per-worktree refs; Git updates or deletes each through `<name>.lock`. Each holds an object name, never a command. */
const WORKTREE_REFS = ['HEAD', 'ORIG_HEAD', 'FETCH_HEAD', 'MERGE_HEAD', 'AUTO_MERGE', 'CHERRY_PICK_HEAD', 'REVERT_HEAD',
  'REBASE_HEAD'];

/**
 * Per-worktree Git files a commit, branch, checkout, reset, fetch or push
 * writes in the worktree's own Git directory (the main checkout's Git
 * directory, a linked worktree's administrative directory): the index and
 * the per-worktree refs, each with its lock, and the commit and merge
 * messages and merge mode, which Git reads as text and options, not as
 * instructions.
 */
export const WORKTREE_GIT_WRITES = [
  'index', 'index.lock', ...WORKTREE_REFS.flatMap((name) => [name, `${name}.lock`]),
  'COMMIT_EDITMSG', 'MERGE_MSG', 'MERGE_MSG.lock', 'MERGE_MODE',
];

/**
 * State Git executes from when an operation continues: the todo lists of an
 * interactive rebase or a cherry-pick/revert sequence (`exec <command>`
 * lines) and their stored options (merge strategy, rewritten-commit hooks).
 * Never opened; denied even inside a session root.
 */
export const EXECUTABLE_GIT_STATE = ['rebase-merge', 'rebase-apply', 'sequencer'];

/**
 * The Git paths Bash may write for the configured commit repositories, and
 * the paths it may not write inside them; throws on an entry that is not a
 * repository root (see gitLayout).
 *
 * Only named Git data opens, never the Git directory, an administrative
 * directory or `worktrees/` as a whole, so a worktree another session adds
 * later stays closed. Both kinds open the shared data of the common
 * directory (COMMON_GIT_WRITES: `objects/` with packs, `refs/`, `logs/`,
 * `packed-refs` with its lock and the temporary file Git renames over it)
 * and their own per-worktree files (WORKTREE_GIT_WRITES); a linked worktree
 * also opens its own `logs/` (the reflog of its HEAD). Closed even when a
 * session root contains them: `hooks/` and `config` (and `config.worktree`),
 * which run commands; the rebase and sequencer state (EXECUTABLE_GIT_STATE),
 * which a later `--continue` outside the sandbox would execute;
 * `commondir`, the administrative `gitdir`, the worktree's `.git` link and
 * `objects/info/alternates`, which redirect Git to other data; `info/`
 * (attributes, excludes, sparse patterns); and `modules/`, whose submodule
 * Git directories carry their own hooks and config (CLOSED_COMMON_GIT,
 * CLOSED_WORKTREE_GIT, CLOSED_ADMINISTRATIVE_GIT). Writes outside the list
 * (`gc --auto` state, other worktrees, rebase and multi-commit cherry-pick
 * or revert) are refused; a refused automatic gc does not fail the commit.
 *
 * Admission is fail-closed (verifyGitData): the start is refused when the
 * common Git directory (which holds a linked worktree's administrative
 * directory) contains a symbolic link anywhere, or a file outside `objects/`
 * with more than one hard link, or when any opened or closed entry is not
 * at its own real path or is itself hard-linked. Seatbelt judges a write by the real path it reaches, so a closed
 * entry that is a link (`config -> objects/policy`) or shares an inode with
 * an opened file would be writable through the opened one; and an opened
 * entry that is a link would carry the grant to its target. Git creates
 * neither inside its directory. Each closed entry is denied as named and as
 * its real path, like ccPolicyPaths.
 */
export function ccCommitRepoPaths(repos: string[] | undefined): { write: string[]; deny: string[] } {
  const layouts = [...new Set(repos ?? [])].map(gitLayout);
  const write: string[] = [];
  const closed: string[] = [];
  for (const layout of layouts) {
    const { common } = layout;
    const own = layout.kind === 'main' ? common : layout.admin;
    write.push(...COMMON_GIT_WRITES.map((name) => join(common, name)), ...WORKTREE_GIT_WRITES.map((name) => join(own, name)));
    closed.push(...CLOSED_COMMON_GIT.map((name) => join(common, name)), ...CLOSED_WORKTREE_GIT.map((name) => join(own, name)));
    if (layout.kind === 'linked') {
      write.push(join(own, 'logs'));
      closed.push(join(layout.repo, '.git'), ...CLOSED_ADMINISTRATIVE_GIT.map((name) => join(own, name)));
    }
  }
  verifyGitData([...new Set(layouts.map((layout) => layout.common))], [...write, ...closed]);
  return { write: [...new Set(write)], deny: [...new Set(closed.flatMap((path) => [path, realRoot(path)]))] };
}

/** Common-directory entries that run commands or redirect Git: never opened, denied even inside a session root. */
export const CLOSED_COMMON_GIT = ['hooks', 'config', 'config.worktree', 'commondir', 'modules', 'info',
  join('objects', 'info', 'alternates')];

/** Per-worktree entries Git executes from or reads settings out of. */
export const CLOSED_WORKTREE_GIT = [...EXECUTABLE_GIT_STATE, 'info'];

/** A linked worktree's administrative files that redirect it or carry its configuration. */
export const CLOSED_ADMINISTRATIVE_GIT = ['commondir', 'gitdir', 'config.worktree'];

/** Entries a Git directory may hold before admission gives up on proving it. */
const MAX_GIT_ENTRIES = 2_000_000;

/**
 * Refuse a Git directory whose opened or closed entries could be reached by
 * another name: any symbolic link in it, any file outside `objects/` with
 * more than one hard link, and any listed entry (opened or closed) that
 * exists away from its own real path. Throws with the first offending path.
 * Inside `objects/` Git itself links a finished temporary object or pack to
 * its final name before removing the temporary one, so a second link there
 * is a normal transient state; a listed file there (`alternates`), and any
 * closed file linked into `objects/`, is still caught by its own link count.
 * Entries removed while the scan runs (another Git process finishing) are
 * skipped.
 */
export function verifyGitData(commons: string[], entries: string[]): void {
  let seen = 0;
  for (const common of commons) {
    const objects = join(common, 'objects');
    const pending = [common];
    for (let dir = pending.pop(); dir !== undefined; dir = pending.pop()) {
      let children;
      try { children = readdirSync(dir, { withFileTypes: true }); } catch (error) {
        if ((error as NodeJS.ErrnoException).code === 'ENOENT' && dir !== common) continue;
        throw new Error(`commitRepos: cannot read ${dir} (${(error as NodeJS.ErrnoException).code ?? String(error)})`);
      }
      for (const child of children) {
        if (++seen > MAX_GIT_ENTRIES) throw new Error(`commitRepos: ${common} holds more than ${MAX_GIT_ENTRIES} entries to verify`);
        const path = join(dir, child.name);
        if (child.isSymbolicLink()) throw new Error(`commitRepos: ${path} is a symbolic link; Git does not create one there`);
        if (child.isDirectory()) { pending.push(path); continue; }
        if (dir === objects || dir.startsWith(objects + sep)) continue;
        let links: number;
        try { links = lstatSync(path).nlink; } catch (error) {
          if ((error as NodeJS.ErrnoException).code === 'ENOENT') continue;
          throw error;
        }
        if (links > 1) throw new Error(`commitRepos: ${path} has ${links} hard links`);
      }
    }
  }
  for (const entry of entries) {
    let info;
    try { info = lstatSync(entry); } catch { continue; }
    let real = '';
    try { real = realpathSync(entry); } catch { /* a dangling link: refused below */ }
    if (real !== entry) throw new Error(`commitRepos: ${entry} is not at its own real path${real ? ` (${real})` : ''}`);
    // A listed file inside objects/ (alternates) is counted here, since the scan leaves objects/ to Git's own linking.
    if (!info.isDirectory() && info.nlink > 1) throw new Error(`commitRepos: ${entry} has ${info.nlink} hard links`);
  }
}

/**
 * Pi's default session directory name for a working directory, as Pi's
 * session manager encodes it: the absolute path without its leading
 * separator, every `/`, `\` and `:` replaced by `-`, wrapped in `--`.
 */
export function piSessionDirName(cwd: string): string {
  return `--${resolve(cwd).replace(/^[/\\]/, '').replace(/[/\\:]/g, '-')}--`;
}

/** Characters CC's sandbox reads as a glob in a configured path. */
const SANDBOX_GLOB = /[*?[\]]/;

/** Relay's own directory for the Pi agent directories of bounded sessions (ccPiState). */
export function ccPiStateRoot(): string {
  return guardAccountStatePath(join(resolveRelayDataDir(), 'cc-pi-agent'));
}

/** Where Pi (0.87.1) run from a bounded session's Bash keeps its state. */
export interface CcPiState { source: string; agentDir: string; sessionDir: string }

/**
 * Pi state for one bounded session's sandboxed Bash, outside the account's
 * shared Pi agent directory `<home>/.pi/agent` (`source`). Pi takes
 * proper-lockfile locks (`auth.json.lock`, `settings.json.lock`,
 * `models-store.json.lock`, `trust.json.lock`) beside the files it reads, in
 * its agent directory; a session that could write those shared lock
 * directories could leave a file in one and keep every Pi on the machine from
 * taking that lock again. Bash therefore runs Pi with `PI_CODING_AGENT_DIR`
 * set to `agentDir`, Relay's own directory for this session
 * (`<stateRoot>/<piSessionDirName(workspace)>/<id>`, leased by
 * leasePiAgentDir), and `PI_CODING_AGENT_SESSION_DIR` set to `sessionDir`,
 * the workspace's session directory in the shared one, where Pi started from
 * the workspace writes its logs anyway. Undefined without a home or state
 * root, or when either path would read to CC as a glob.
 */
export function ccPiState(home: string | undefined, workspace: string, stateRoot: string | undefined, id: string): CcPiState | undefined {
  if (!home || !stateRoot) return undefined;
  const name = piSessionDirName(realRoot(workspace));
  const source = realPath(join(home, '.pi', 'agent'));
  const state = { source, agentDir: join(realPath(stateRoot), name, id), sessionDir: join(source, 'sessions', name) };
  return SANDBOX_GLOB.test(state.agentDir) || SANDBOX_GLOB.test(state.sessionDir) ? undefined : state;
}

/** Entries of the shared agent directory a Pi agent directory does not link: the logs, the locks, Pi's scratch directory. */
function unlinkedPiEntry(name: string): boolean {
  return name === 'sessions' || name === 'tmp' || name.endsWith('.lock');
}

/** The account a session runs as when it is not Relay's own (`linuxUser`, run through `sudo -u`). */
export interface PiAgentDirUser { name: string; uid: number; gid: number }

/** What leasePiAgentDir needs from the system; tests replace it. */
export interface PiAgentDirSystem {
  /** Relay's process, the owner of a lease until adopt names the session's process. */
  pid: number;
  /** Whether a process still runs; a process of another account counts as running. */
  alive(pid: number): boolean;
  chown(path: string, uid: number, gid: number): void;
  /** Delete a path and what is under it without following links (`rm -rf`). */
  remove(path: string): void;
}

export const PI_AGENT_DIR_SYSTEM: PiAgentDirSystem = {
  pid: process.pid,
  alive(pid) {
    try { process.kill(pid, 0); return true; } catch (error) { return (error as NodeJS.ErrnoException).code === 'EPERM'; }
  },
  chown: chownSync,
  remove(path) { rmSync(path, { recursive: true, force: true }); },
};

/** The uid and gid of a local account (`id -u`, `id -g`); throws when the account does not exist. */
export function lookupPiAgentDirUser(name: string): PiAgentDirUser {
  const id = (flag: string): number => Number(execFileSync('id', [flag, name], { encoding: 'utf8', stdio: ['ignore', 'pipe', 'ignore'], env: relayChildEnv() }).trim());
  const user = { name, uid: id('-u'), gid: id('-g') };
  if (!Number.isInteger(user.uid) || !Number.isInteger(user.gid)) throw new Error(`no uid/gid for ${name}`);
  return user;
}

/** One session's Pi agent directory: `adopt` names the session's process as its owner, `release` deletes it (discardPiAgentLease; never throws). */
export interface PiAgentLease { state: CcPiState; adopt(pid: number): void; release(): void }

/** A lease, or why the session's Bash gets no Pi state (logged; the start is not refused). */
export type PiAgentDirResult = { lease: PiAgentLease } | { skipped: string };

/** Whether `uid`/`gid` may pass through `dir` (search permission by owner, group or others). */
function traversable(dir: string, user: PiAgentDirUser): boolean {
  const stat = statSync(dir);
  if (stat.uid === user.uid) return (stat.mode & 0o100) !== 0;
  if (stat.gid === user.gid) return (stat.mode & 0o010) !== 0;
  return (stat.mode & 0o001) !== 0;
}

function warnPiAgentDir(msg: string, path: string, error: unknown): void {
  console.warn({ ts: new Date().toISOString(), level: 'warn', module: 'cc-write-boundary', msg, path, error: String(error) });
}

/** Relay's directory, beside the workspaces' (`<stateRoot>/.quarantine`), for Pi agent directories it could not delete. */
function piQuarantine(workspaceDir: string): string {
  return join(dirname(workspaceDir), '.quarantine');
}

/**
 * Give the owner back search, read and write on every directory under `path`
 * (0700), without following links: what a session closed with chmod (`chmod
 * 000`) becomes deletable again. A directory Relay may not chmod stays as it is.
 */
function reopenForRemoval(path: string): void {
  let stat;
  try { stat = lstatSync(path); } catch { return; }
  if (!stat.isDirectory()) return;
  try { chmodSync(path, 0o700); } catch { return; }
  let names: string[] = [];
  try { names = readdirSync(path); } catch { return; }
  for (const name of names) reopenForRemoval(join(path, name));
}

/** Delete `path` (system.remove), reopening its directories (reopenForRemoval) when the first attempt fails; never throws. */
function removePiPath(path: string, system: PiAgentDirSystem): unknown {
  try { system.remove(path); return undefined; } catch { /* reopen and retry */ }
  reopenForRemoval(path);
  try { system.remove(path); return undefined; } catch (error) { return error; }
}

/**
 * Remove a Pi agent directory: delete it (removePiPath), else move it into
 * Relay's quarantine (piQuarantine) under a fresh name and log a warning, else
 * leave it and log; never throws. True when `path` is gone.
 */
function discardPiAgentDir(path: string, system: PiAgentDirSystem): boolean {
  const error = removePiPath(path, system);
  if (error === undefined) return true;
  const quarantine = piQuarantine(dirname(path));
  const target = join(quarantine, `${basename(path)}-${randomUUID()}`);
  try {
    mkdirSync(quarantine, { recursive: true, mode: 0o700 });
    renameSync(path, target);
  } catch (moveError) {
    warnPiAgentDir('Pi agent directory neither deleted nor quarantined; the next start retries', path, moveError);
    return false;
  }
  warnPiAgentDir(`Pi agent directory not deleted; moved to ${target}`, path, error);
  return true;
}

/** Delete a Pi agent directory, then its owner record (only once the directory is gone); never throws. */
function discardPiAgentLease(agentDir: string, system: PiAgentDirSystem): void {
  if (!discardPiAgentDir(agentDir, system)) return;
  const error = removePiPath(`${agentDir}.owner`, system);
  if (error !== undefined) warnPiAgentDir('Pi agent directory owner record not deleted', `${agentDir}.owner`, error);
}

/**
 * Delete the Pi agent directories of a workspace whose owner no longer runs.
 * Each directory `<id>` has an owner record `<id>.owner` beside it, outside
 * every sandbox's writes, holding the owner's process id; a directory without
 * a record, or whose recorded process is gone, is ownerless. Deletion does not
 * follow links, so whatever a session left there goes and the account's files
 * stay. Each entry is handled on its own and nothing throws: a directory the
 * session made undeletable (a subdirectory closed with `chmod 000`) is reopened
 * and deleted, or else moved into the quarantine (discardPiAgentDir), whose
 * entries every sweep tries to delete again.
 */
export function sweepPiAgentDirs(workspaceDir: string, system: PiAgentDirSystem = PI_AGENT_DIR_SYSTEM): void {
  const quarantine = piQuarantine(workspaceDir);
  let held: string[] = [];
  try { held = readdirSync(quarantine); } catch { /* nothing quarantined */ }
  for (const entry of held) removePiPath(join(quarantine, entry), system);
  let entries: string[];
  try { entries = readdirSync(workspaceDir); } catch { return; }
  const owned = new Set<string>();
  for (const entry of entries.filter((name) => name.endsWith('.owner'))) {
    const id = entry.slice(0, -'.owner'.length);
    let pid: number;
    // A record released meanwhile is gone with its directory.
    try { pid = Number(readFileSync(join(workspaceDir, entry), 'utf8').trim()); } catch { continue; }
    let alive = false;
    try { alive = Number.isInteger(pid) && pid > 0 && system.alive(pid); } catch { /* unknown: treated as gone */ }
    if (alive) { owned.add(id); continue; }
    // A directory that stays keeps its record, so the next sweep retries it.
    discardPiAgentLease(join(workspaceDir, id), system);
  }
  // What has no record: a directory a crash left behind, or a record replacement (`<id>.owner.new`) of one.
  for (const entry of entries.filter((name) => !name.endsWith('.owner'))) {
    const id = entry.replace(/\.owner\.new$/, '');
    if (owned.has(id) || existsSync(join(workspaceDir, `${id}.owner`))) continue;
    discardPiAgentDir(join(workspaceDir, entry), system);
  }
}

/**
 * Give one CC session its own Pi agent directory before it starts, so
 * concurrent sessions of a workspace (a conversation beside a scheduled run)
 * never share a lock directory. The owner record is written first, as Relay's
 * process, then the directory: a symbolic link to each entry of the shared
 * agent directory (credential, settings, model catalog, trust record,
 * extensions, packages, prompts) except unlinkedPiEntry, so Pi reads the
 * account's configuration and credential, while its locks (taken with
 * `realpath: false`, so beside the link) land in `agentDir`. Writes through a
 * link reach the shared file and stay denied by the sandbox: Pi inside cannot
 * refresh an OAuth credential, record a trust decision or update its model
 * catalog. Ownerless directories of the workspace are swept first
 * (sweepPiAgentDirs); a live session's directory is never touched, and a
 * failed sweep is logged and does not stop the lease. `release` deletes the
 * directory the same way (discardPiAgentLease) and never throws.
 *
 * For a session run as another account (`user`), Relay hands the directory to
 * that account with chown and opens the two levels it owns above it for
 * search (0711); when chown is not permitted or a directory above it cannot
 * be passed by that account, the session gets no Pi state (`skipped`).
 */
export function leasePiAgentDir(
  home: string | undefined, workspace: string, stateRoot: string | undefined,
  options: { id?: string; user?: PiAgentDirUser; system?: PiAgentDirSystem } = {},
): PiAgentDirResult | undefined {
  const system = options.system ?? PI_AGENT_DIR_SYSTEM;
  const state = ccPiState(home, workspace, stateRoot, options.id ?? randomUUID());
  if (!state) return undefined;
  const workspaceDir = dirname(state.agentDir);
  mkdirSync(workspaceDir, { recursive: true, mode: 0o700 });
  try { sweepPiAgentDirs(workspaceDir, system); } catch (error) { warnPiAgentDir('Pi agent directories not swept', workspaceDir, error); }
  const owner = `${state.agentDir}.owner`;
  writeFileSync(owner, `${system.pid}\n`, { flag: 'wx', mode: 0o600 });
  const release = (): void => discardPiAgentLease(state.agentDir, system);
  try {
    mkdirSync(state.agentDir, { mode: 0o700 });
    let names: string[] = [];
    try { names = readdirSync(state.source); } catch { /* no Pi configuration in this account */ }
    for (const name of names.filter((entry) => !unlinkedPiEntry(entry))) symlinkSync(join(state.source, name), join(state.agentDir, name));
    const user = options.user;
    if (user) {
      for (const dir of [dirname(workspaceDir), workspaceDir]) chmodSync(dir, 0o711);
      const closed = ancestors(workspaceDir).find((dir) => !traversable(dir, user));
      if (closed) {
        release();
        return { skipped: `${user.name} cannot pass through ${closed}` };
      }
      try { system.chown(state.agentDir, user.uid, user.gid); } catch (error) {
        release();
        return { skipped: `cannot hand ${state.agentDir} to ${user.name}: ${(error as Error).message}` };
      }
    }
  } catch (error) {
    release();
    throw error;
  }
  return {
    lease: {
      state,
      adopt(pid) {
        writeFileSync(`${owner}.new`, `${pid}\n`, { mode: 0o600 });
        renameSync(`${owner}.new`, owner);
      },
      release,
    },
  };
}

/** `dir` and every directory above it. */
function ancestors(dir: string): string[] {
  const all = [dir];
  for (let current = dir; dirname(current) !== current; current = dirname(current)) all.push(dirname(current));
  return all;
}

/** What Bash may write for Pi (ccPiState): its own agent directory and the workspace's session directory. */
export function ccPiWrites(state: CcPiState | undefined): string[] {
  return state ? [state.agentDir, state.sessionDir] : [];
}

/** The environment that points Pi at that state. */
export function ccPiEnv(state: CcPiState | undefined): Record<string, string> {
  return state ? { PI_CODING_AGENT_DIR: state.agentDir, PI_CODING_AGENT_SESSION_DIR: state.sessionDir } : {};
}

/**
 * Bash's additions: an Agent's repository access and Pi's writes and
 * environment for the session (ccPiState); throws on malformed or unprovable
 * configuration.
 */
export function ccBashAccess(access: CcRepoAccess | undefined, pi?: CcPiState): CcBashAccess {
  const [problem] = ccRepoAccessProblems(access ?? {});
  if (problem) throw new Error(problem);
  const { write, deny } = ccCommitRepoPaths(access?.commitRepos);
  return { write: [...new Set([...write, ...ccPiWrites(pi)])], deny, allowedDomains: [...new Set(access?.allowedDomains ?? [])],
    env: ccPiEnv(pi) };
}

/**
 * Environment the sandboxed Bash needs for the AI CLIs it runs; CC applies the
 * settings `env` to its own process, so Bash and its children inherit it.
 * Codex inside the macOS sandbox cannot read the system keychain's roots and
 * fails its workspace routing discovery, so on darwin it is pointed at the
 * system bundle `/etc/ssl/cert.pem`; on Linux Codex reads the distribution's
 * bundle under `/etc/ssl/certs` itself, and the macOS path does not exist
 * there. Node (Pi) uses `HTTPS_PROXY`, which the sandbox sets to its proxy,
 * only with `NODE_USE_ENV_PROXY=1`, which CC does not set.
 */
export function ccSandboxEnv(platform: NodeJS.Platform = process.platform): Record<string, string> {
  return { ...(platform === 'darwin' ? { CODEX_CA_CERTIFICATE: '/etc/ssl/cert.pem' } : {}), NODE_USE_ENV_PROXY: '1' };
}

/**
 * The CC settings fragment: the sandbox environment (ccSandboxEnv), the
 * file-tool hook and the Bash sandbox over the same roots. The hook runs in exec form with `onFailure: "block"`, so a hook
 * that cannot start, crashes or times out blocks the call; `disableAllHooks`
 * is pinned off at the flag source. `bash` adds the commit repositories' Git
 * paths and closed paths and Pi's writes to the sandbox only, Pi's environment to `env`, and its domains to the sandbox
 * network allowlist (`sandbox.network.allowedDomains`); the hook's roots stay
 * the session roots, so the file tools still cannot write Git data.
 */
export function ccWriteBoundarySettings(
  roots: string[], node: string = process.execPath, denials: string[] = [], bash?: CcBashAccess,
  platform: NodeJS.Platform = process.platform,
): Record<string, unknown> {
  return {
    env: { ...ccSandboxEnv(platform), ...bash?.env },
    disableAllHooks: false,
    hooks: {
      PreToolUse: [{
        matcher: WRITE_BOUNDARY_MATCHER,
        hooks: [{ type: 'command', command: node, args: [BOUNDARY_SCRIPT, JSON.stringify(roots), JSON.stringify(denials)],
          timeout: 10, onFailure: 'block' }],
      }],
    },
    sandbox: {
      enabled: true,
      failIfUnavailable: true,
      allowUnsandboxedCommands: false,
      autoAllowBashIfSandboxed: true,
      filesystem: {
        allowWrite: [...new Set([...roots, ...(bash?.write ?? [])])],
        denyWrite: [...new Set([...ccPolicyPaths(roots, denials), ...(bash?.deny ?? [])])],
      },
      ...(bash?.allowedDomains.length ? { network: { allowedDomains: bash.allowedDomains } } : {}),
    },
  };
}

/**
 * Add the boundary to CC's single `--settings` value, after refusing settings
 * that would widen CC's writes beyond the roots. CC unites array settings
 * across its sources, so a user, project, local or managed file could add
 * sandbox write paths (`sandbox.filesystem.allowWrite`, path-specific Edit
 * allow rules, `permissions.additionalDirectories`) or relax the sandbox
 * itself; the hook would then judge other roots than Bash. Only the files
 * are judged: the commit repositories and domains this fragment adds from the
 * Agent's configuration (`repoAccess`) are Relay's own `--settings` value.
 * Malformed or unprovable `repoAccess` refuses the start, like a conflict.
 * With `piStateRoot` (ccPiStateRoot) Bash gets Pi's state (ccPiState) in a
 * directory leased for this session once the start is not refused
 * (leasePiAgentDir); the caller adopts the lease for the CC process and
 * releases it when that process exits. `piUser` is the account a `linuxUser`
 * session runs as; a lease that account could not use is skipped with a
 * warning and Bash gets nothing for Pi.
 */
export function addCcWriteBoundarySettings(
  args: string[], roots: string[],
  context: {
    home?: string; workspace: string; denials?: string[]; repoAccess?: CcRepoAccess; piStateRoot?: string;
    piUser?: string; piSystem?: PiAgentDirSystem; piLookupUser?: (name: string) => PiAgentDirUser;
  },
  node: string = process.execPath,
): PiAgentLease | undefined {
  try { ccBashAccess(context.repoAccess); } catch (error) {
    throw new Error(`CC write boundary refused: ${(error as Error).message}`);
  }
  const conflicts = ccSettingsSources(context.home, context.workspace).flatMap((path) => writeRelaxations(path));
  if (conflicts.length > 0) {
    throw new Error(`CC write boundary refused: settings widen writes beyond the session roots or disable its hook (${conflicts.join('; ')})`);
  }
  let result: PiAgentDirResult | undefined;
  try {
    let user: PiAgentDirUser | undefined;
    if (context.piUser) {
      try { user = (context.piLookupUser ?? lookupPiAgentDirUser)(context.piUser); } catch (error) {
        result = { skipped: `no account ${context.piUser}: ${(error as Error).message}` };
      }
    }
    result ??= leasePiAgentDir(context.home, context.workspace, context.piStateRoot, { user, system: context.piSystem });
  } catch (error) { throw new Error(`CC write boundary refused: Pi state: ${(error as Error).message}`); }
  if (result && 'skipped' in result) {
    console.warn({ ts: new Date().toISOString(), level: 'warn', module: 'cc-write-boundary', workspace: context.workspace,
      msg: 'sandboxed Bash gets no Pi state', reason: result.skipped });
  }
  const lease = result && 'lease' in result ? result.lease : undefined;
  try {
    mergeCcSettingsArg(args, ccWriteBoundarySettings(roots, node, context.denials, ccBashAccess(context.repoAccess, lease?.state)));
  } catch (error) {
    lease?.release();
    throw error;
  }
  return lease;
}

const MANAGED_SETTINGS = '/Library/Application Support/ClaudeCode/managed-settings.json';

/** CC settings files that apply to a session in `workspace`: user, project (the workspace and its repository root), local, managed. */
export function ccSettingsSources(home: string | undefined, workspace: string): string[] {
  const projects = [workspace];
  for (let dir = workspace; dirname(dir) !== dir; dir = dirname(dir)) {
    if (existsSync(join(dir, '.git'))) { if (dir !== workspace) projects.push(dir); break; }
  }
  return [...new Set([
    ...(home ? [join(home, '.claude', 'settings.json')] : []),
    ...projects.flatMap((dir) => [join(dir, '.claude', 'settings.json'), join(dir, '.claude', 'settings.local.json')]),
    MANAGED_SETTINGS,
  ])];
}

const FILE_TOOLS = /^(Edit|Write|MultiEdit|NotebookEdit)\(/;

/**
 * Why one settings file would widen writes or keep the hook from running; []
 * when it would not or is absent. CC ignores a user or project file it cannot
 * parse; managed settings it cannot read are refused, since CC then cannot
 * tell which hooks policy allows.
 */
export function writeRelaxations(path: string, managed: boolean = path === MANAGED_SETTINGS): string[] {
  let settings: Record<string, any>;
  try { settings = JSON.parse(readFileSync(path, 'utf8')) as Record<string, any>; } catch (error) {
    const absent = (error as NodeJS.ErrnoException).code === 'ENOENT';
    return managed && !absent ? [`${path}: unreadable managed settings`] : [];
  }
  if (!settings || typeof settings !== 'object') return [];
  const found: string[] = [];
  if (settings['disableAllHooks'] === true) found.push('disableAllHooks');
  if (settings['allowManagedHooksOnly'] === true) found.push('allowManagedHooksOnly');
  const sandbox = settings['sandbox'];
  if (sandbox && typeof sandbox === 'object') {
    if (Array.isArray(sandbox.filesystem?.allowWrite) && sandbox.filesystem.allowWrite.length) found.push('sandbox.filesystem.allowWrite');
    if (sandbox.filesystem?.disabled === true) found.push('sandbox.filesystem.disabled');
    if (Array.isArray(sandbox.excludedCommands) && sandbox.excludedCommands.length) found.push('sandbox.excludedCommands');
    if (sandbox.allowUnsandboxedCommands === true) found.push('sandbox.allowUnsandboxedCommands');
    if (sandbox.enabled === false) found.push('sandbox.enabled=false');
  }
  const permissions = settings['permissions'];
  if (Array.isArray(permissions?.allow) && permissions.allow.some((rule: unknown) => typeof rule === 'string' && FILE_TOOLS.test(rule))) {
    found.push('path-specific file-tool allow rule');
  }
  if (Array.isArray(permissions?.additionalDirectories) && permissions.additionalDirectories.length) {
    found.push('permissions.additionalDirectories');
  }
  return found.map((reason) => `${path}: ${reason}`);
}
