/**
 * CC PreToolUse write boundary: Edit, Write, MultiEdit and NotebookEdit may
 * change only files under the session's write roots, given as one JSON argv,
 * and never under the denied paths, given as a second (the paths the kernel
 * keeps closed inside an allocated workspace's roots).
 *
 * The real path the write would reach decides: symbolic links are followed,
 * dangling ones included, so a link inside a root cannot lead outside it. A
 * path that cannot be resolved safely, or a request this hook cannot read, is
 * denied. Bash writes are bounded by CC's sandbox instead
 * (Relay/src/cc-write-boundary.ts); every other tool passes untouched.
 */
import { lstatSync, readlinkSync, realpathSync } from 'node:fs';
import { basename, dirname, isAbsolute, join, resolve, sep } from 'node:path';
import { fileURLToPath } from 'node:url';

export const BOUNDARY_TOOLS = new Set(['Edit', 'Write', 'MultiEdit', 'NotebookEdit']);
const MAX_LINKS = 40;

/**
 * The real path a write would reach, or undefined when it cannot be resolved
 * safely (unreadable entry, link loop). Missing components are kept as named;
 * a link, even one whose target is missing, is followed to its target.
 */
export function realTarget(path, links = 0) {
  if (links > MAX_LINKS) return undefined;
  const rest = [];
  let current = path;
  for (;;) {
    let entry;
    try {
      entry = lstatSync(current);
    } catch (error) {
      if (error?.code !== 'ENOENT') return undefined;
      const parent = dirname(current);
      if (parent === current) return undefined;
      rest.unshift(basename(current));
      current = parent;
      continue;
    }
    if (entry.isSymbolicLink()) {
      let link;
      try { link = readlinkSync(current); } catch { return undefined; }
      return realTarget(join(resolve(dirname(current), link), ...rest), links + 1);
    }
    try { return join(realpathSync(current), ...rest); } catch { return undefined; }
  }
}

function within(root, target) {
  return target === root || target.startsWith(root.endsWith(sep) ? root : root + sep);
}

/** undefined when the call may proceed, else the reason it is denied. */
export function boundaryDecision(input, roots, denials = []) {
  if (!input || typeof input !== 'object') return 'unreadable tool request';
  if (!BOUNDARY_TOOLS.has(input.tool_name)) return undefined;
  const path = input.tool_input?.file_path ?? input.tool_input?.notebook_path;
  if (typeof path !== 'string' || !path) return 'write request names no file';
  const lexical = resolve(isAbsolute(path) ? path : resolve(typeof input.cwd === 'string' ? input.cwd : process.cwd(), path));
  const target = realTarget(lexical);
  if (target === undefined) return `${path} cannot be resolved safely; write inside your workspace`;
  const realRoots = roots.map((root) => realTarget(root)).filter((root) => root !== undefined);
  const policy = policyPath(lexical, target, [...new Set([...roots.map((root) => resolve(root)), ...realRoots])]);
  if (policy) return `${target} ${policy}; the session cannot change its own execution policy`;
  const denied = denials.find((path) => within(path, lexical) || within(path, target));
  if (denied) return `${target} is under ${denied}, which the kernel keeps closed in an allocated workspace`;
  if (realRoots.some((root) => within(root, target))) return undefined;
  return `${target} is outside this session's write roots (${roots.join(', ')}); `
    + 'write inside your workspace, or ask for a write grant (grant_access)';
}

/** The Relay installation holding this hook; a session never writes it. */
export const RELAY_ROOT = resolve(dirname(fileURLToPath(import.meta.url)), '..');

/** Entry points that change execution policy: CC configuration, MCP servers, Git hooks and config. */
export const POLICY_ENTRIES = ['.claude', '.mcp.json', '.git', join('.git', 'hooks'), join('.git', 'config')];

function policyName(parts) {
  if (parts.includes('.claude')) return 'is CC configuration (.claude/)';
  if (parts.includes('.git')) return 'is Git metadata (.git/), whose hooks and config run commands';
  if (parts.includes('.mcp.json')) return 'is an MCP server declaration (.mcp.json)';
  return undefined;
}

/**
 * Why a write still changes execution policy, else undefined. Both the
 * requested (lexical) path and the real path it reaches are judged: by the
 * names of their components under a root, and against every policy entry
 * point (at each root and each directory on either path) together with the
 * real target that entry resolves to. A link such as `.claude -> config`
 * therefore protects `config/` too. The Relay installation is never writable.
 */
export function policyPath(lexical, target, roots) {
  const relay = [RELAY_ROOT, realTarget(RELAY_ROOT)].filter(Boolean);
  if (relay.some((root) => within(root, lexical) || within(root, target))) return 'is part of the Relay installation';
  const directories = new Set(roots);
  for (const root of roots) {
    for (const path of [lexical, target]) {
      if (!within(root, path) || path === root) continue;
      const named = policyName(path.slice(root.length + 1).split(sep));
      if (named) return named;
      for (let dir = dirname(path); within(root, dir); dir = dirname(dir)) {
        directories.add(dir);
        if (dir === root) break;
      }
    }
  }
  for (const dir of directories) {
    for (const name of POLICY_ENTRIES) {
      const entry = join(dir, name);
      const resolved = realTarget(entry);
      for (const entryPath of resolved && resolved !== entry ? [entry, resolved] : [entry]) {
        if (within(entryPath, lexical) || within(entryPath, target)) return `${policyName(name.split(sep))} (through ${entry})`;
      }
    }
  }
  return undefined;
}

function deny(reason) {
  process.stdout.write(JSON.stringify({
    hookSpecificOutput: { hookEventName: 'PreToolUse', permissionDecision: 'deny', permissionDecisionReason: reason },
  }));
}

async function main() {
  let roots;
  let denials;
  const paths = (value) => Array.isArray(value) && value.every((path) => typeof path === 'string' && isAbsolute(path));
  try {
    roots = JSON.parse(process.argv[2] ?? '');
    denials = process.argv.length > 3 ? JSON.parse(process.argv[3]) : [];
    if (!paths(roots) || !paths(denials)) throw new Error();
  } catch {
    deny('write boundary roots are unavailable');
    return;
  }
  let raw = '';
  for await (const chunk of process.stdin) raw += chunk;
  let input;
  try { input = JSON.parse(raw); } catch { input = undefined; }
  const reason = boundaryDecision(input, roots, denials);
  if (reason) deny(reason);
}

if (process.argv[1] && resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  // Any failure of this hook denies the call; CC also blocks when the hook cannot start or times out (onFailure).
  try { await main(); } catch (error) { deny(`write boundary hook failed: ${String(error)}`); }
}
