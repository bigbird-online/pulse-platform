import { existsSync, readdirSync, realpathSync, statSync } from 'node:fs';
import { basename, dirname, isAbsolute, join, resolve, sep } from 'node:path';

function safeTenantDirectory(tenantId: string): string {
  return tenantId.replace(/[^a-zA-Z0-9._-]/g, '_');
}

function existingRealPath(path: string): string {
  try {
    return realpathSync(path);
  } catch {
    return resolve(path);
  }
}

function pathWithin(root: string, candidate: string): boolean {
  return candidate === root || candidate.startsWith(`${root}${sep}`);
}

function sharedOutboxRoot(workspace: string): string {
  return join(existingRealPath(workspace), '.relay', 'outbox');
}

/**
 * Resolve the only directory whose contents may be interpreted as outbound
 * tool-generated files. Ordinary workspace writes are never delivery intent.
 *
 * One workspace can host several channel sessions of the same agent (for
 * example Telegram `user:agent` and Feishu `user:agent:feishu`). Each session
 * owns its own subdirectory, so a file is delivered only to the conversation
 * whose runtime was told to write there.
 */
export function resolveToolFileOutboxPath(tenantId: string, workspace: string): string {
  const devRoot = process.env['RELAY_OUTBOX_DIR'];
  if (devRoot) return join(existingRealPath(devRoot), safeTenantDirectory(tenantId));
  return join(sharedOutboxRoot(workspace), safeTenantDirectory(tenantId));
}

export type ToolFileOutboxValidation =
  | { ok: true; path: string; outbox: string }
  | { ok: false; error: string };

/**
 * Validate both lexical ownership and real paths before a channel uploads or
 * deletes a file. This is a consumer-side guard: producers are not trusted to
 * label arbitrary workspace paths as disposable outbox artifacts.
 */
export function validateToolFileOutboxPath(
  tenantId: string,
  workspace: string,
  filePath: string,
): ToolFileOutboxValidation {
  const outbox = resolveToolFileOutboxPath(tenantId, workspace);
  const candidate = isAbsolute(filePath) ? resolve(filePath) : resolve(workspace, filePath);
  if (!existsSync(candidate)) return { ok: false, error: `File not found: ${filePath}` };

  try {
    // Canonicalize parent aliases (for example macOS /tmp → /private/tmp) while
    // retaining the final directory entry so an outbox-external symlink cannot
    // borrow an in-outbox target's ownership.
    const canonicalEntry = join(realpathSync(dirname(candidate)), basename(candidate));
    if (canonicalEntry === outbox || !pathWithin(outbox, canonicalEntry)) {
      return { ok: false, error: `Path is outside the tool-file outbox: ${filePath}` };
    }
    const realOutbox = realpathSync(outbox);
    const realCandidate = realpathSync(candidate);
    if (!process.env['RELAY_OUTBOX_DIR']) {
      const realWorkspace = realpathSync(workspace);
      if (!pathWithin(realWorkspace, realOutbox)) {
        return { ok: false, error: `Tool-file outbox escapes the workspace: ${outbox}` };
      }
    }
    if (!pathWithin(realOutbox, realCandidate)) {
      return { ok: false, error: `Tool-file path escapes the outbox: ${filePath}` };
    }
    if (!statSync(realCandidate).isFile()) {
      return { ok: false, error: `Not a regular outbox file: ${filePath}` };
    }
    return { ok: true, path: candidate, outbox };
  } catch {
    return { ok: false, error: `Cannot validate tool-file outbox path: ${filePath}` };
  }
}

const warnedUnattributedFiles = new Set<string>();

/**
 * Files directly under the shared `.relay/outbox/` root have no session owner
 * (legacy layout). They are never delivered or deleted; warn once per path so
 * an operator can move or remove them.
 */
function warnUnattributedOutboxFiles(tenantId: string, workspace: string): void {
  if (process.env['RELAY_OUTBOX_DIR']) return;
  const root = sharedOutboxRoot(workspace);
  if (!existsSync(root)) return;
  try {
    for (const name of readdirSync(root)) {
      if (name.startsWith('.')) continue;
      const path = join(root, name);
      if (warnedUnattributedFiles.has(path)) continue;
      try { if (!statSync(path).isFile()) continue; } catch { continue; }
      warnedUnattributedFiles.add(path);
      console.warn({
        ts: new Date().toISOString(), level: 'warn', poolKey: tenantId,
        msg: 'Unattributed outbox file left undelivered', path,
      });
    }
  } catch { /* best-effort diagnostics */ }
}

/** Scan the session's own outbox once at terminal turn settlement. */
export function scanToolFileOutbox(tenantId: string, workspace: string): string[] {
  warnUnattributedOutboxFiles(tenantId, workspace);
  const outbox = resolveToolFileOutboxPath(tenantId, workspace);
  if (!existsSync(outbox)) return [];
  try {
    return readdirSync(outbox)
      .filter((name) => !name.startsWith('.'))
      .sort()
      .map((name) => join(outbox, name))
      .filter((path) => validateToolFileOutboxPath(tenantId, workspace, path).ok);
  } catch {
    return [];
  }
}
