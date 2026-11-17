// Stage A (FR-P0-014 / FR-P0-015) — recursive copy of the
// `tenant-pulse-admin` template into a tenant workspace with `{{var}}`
// placeholder substitution.
//
// Contract:
//   - Idempotent: never overwrites existing destination files (so user-edited
//     files are preserved across retries / webhook replays).
//   - Skips designated meta files (default: `README.md`) — these document the
//     template source itself, not the tenant.
//   - Substitutes `{{var}}` placeholders only in known text extensions.
//   - Unknown placeholders are left as-is (surfaces drift instead of silently
//     producing an empty value).

import {
  copyFileSync,
  existsSync,
  mkdirSync,
  readFileSync,
  readdirSync,
  writeFileSync,
} from 'node:fs';
import { dirname, join, relative } from 'node:path';
import { fileURLToPath } from 'node:url';

export interface MaterializeOptions {
  templateRoot: string;
  workspaceAbs: string;
  vars: Record<string, string>;
  /** Files (relative to templateRoot) to skip. Default: ['README.md']. */
  skipFiles?: string[];
  /** When true (default), do not overwrite existing destination files. */
  preserveExisting?: boolean;
}

export interface MaterializeResult {
  copied: string[];
  skipped: string[];
}

const PLACEHOLDER_RE = /\{\{\s*([a-zA-Z_][a-zA-Z0-9_]*)\s*\}\}/g;
const TEXT_EXT = new Set(['.md', '.txt', '.yaml', '.yml', '.json', '.toml']);

export function substitutePlaceholders(
  text: string,
  vars: Record<string, string>,
): string {
  return text.replace(PLACEHOLDER_RE, (_full, name: string) => {
    if (Object.prototype.hasOwnProperty.call(vars, name)) return vars[name]!;
    return `{{${name}}}`;
  });
}

function isTextFile(p: string): boolean {
  const i = p.lastIndexOf('.');
  if (i < 0) return false;
  return TEXT_EXT.has(p.slice(i).toLowerCase());
}

export function materializeTenantWorkspaceFromTemplate(
  opts: MaterializeOptions,
): MaterializeResult {
  if (!existsSync(opts.templateRoot)) {
    throw new Error(`tenant_template_missing:${opts.templateRoot}`);
  }
  const skipSet = new Set(opts.skipFiles ?? ['README.md']);
  const preserveExisting = opts.preserveExisting !== false;
  const copied: string[] = [];
  const skipped: string[] = [];

  mkdirSync(opts.workspaceAbs, { recursive: true });

  const walk = (srcDir: string, dstDir: string): void => {
    for (const entry of readdirSync(srcDir, { withFileTypes: true })) {
      const srcPath = join(srcDir, entry.name);
      const rel = relative(opts.templateRoot, srcPath);
      if (skipSet.has(rel)) {
        skipped.push(rel);
        continue;
      }
      const dstPath = join(dstDir, entry.name);
      if (entry.isDirectory()) {
        mkdirSync(dstPath, { recursive: true });
        walk(srcPath, dstPath);
        continue;
      }
      if (!entry.isFile()) continue;

      if (preserveExisting && existsSync(dstPath)) {
        skipped.push(rel);
        continue;
      }
      if (isTextFile(srcPath)) {
        const raw = readFileSync(srcPath, 'utf8');
        writeFileSync(dstPath, substitutePlaceholders(raw, opts.vars), 'utf8');
      } else {
        copyFileSync(srcPath, dstPath);
      }
      copied.push(rel);
    }
  };
  walk(opts.templateRoot, opts.workspaceAbs);

  return { copied, skipped };
}

/**
 * Resolve the bundled `tenant-pulse-admin` template directory.
 *
 * Source layout:        Platform/src/tenant-template.ts
 *                       Platform/templates/tenant-pulse-admin/
 * Compiled (dist):      Platform/dist/tenant-template.js
 * Both should resolve via walking up to the package root.
 */
export function defaultTenantPulseAdminTemplateRoot(): string {
  const here = dirname(fileURLToPath(import.meta.url));
  let dir = here;
  for (let i = 0; i < 6; i += 1) {
    const candidate = join(dir, 'templates', 'tenant-pulse-admin');
    if (existsSync(candidate)) return candidate;
    const parent = dirname(dir);
    if (parent === dir) break;
    dir = parent;
  }
  return join(here, '..', 'templates', 'tenant-pulse-admin');
}
