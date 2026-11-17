import { describe, expect, it } from 'vitest';
import { mkdtempSync, mkdirSync, readFileSync, writeFileSync, existsSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import {
  defaultTenantPulseAdminTemplateRoot,
  materializeTenantWorkspaceFromTemplate,
  substitutePlaceholders,
} from '../src/tenant-template.js';

function mkTmp(prefix: string): string {
  return mkdtempSync(join(tmpdir(), prefix));
}

function makeFakeTemplate(root: string): void {
  mkdirSync(root, { recursive: true });
  writeFileSync(
    join(root, 'CLAUDE.md'),
    '# {{agentName}}\n\n租户 {{tenantId}} 的 pulse 管理员，agent={{agentId}}。\n',
    'utf8',
  );
  writeFileSync(
    join(root, 'IDENTITY.md'),
    'Mission: serve {{displayName}} only. provisioned at {{provisionedAtIso}}\n',
    'utf8',
  );
  writeFileSync(
    join(root, 'README.md'),
    '# Template README — should NOT be copied to tenants\n',
    'utf8',
  );
  // Nested file to verify recursive copy
  mkdirSync(join(root, 'nested'), { recursive: true });
  writeFileSync(join(root, 'nested', 'note.md'), 'tenant={{tenantId}}\n', 'utf8');
}

describe('substitutePlaceholders', () => {
  it('replaces known {{var}} placeholders', () => {
    expect(substitutePlaceholders('hi {{a}} and {{b}}', { a: '1', b: '2' })).toBe('hi 1 and 2');
  });

  it('leaves unknown placeholders untouched (drift surface)', () => {
    expect(substitutePlaceholders('x {{unknown}} y', {})).toBe('x {{unknown}} y');
  });

  it('handles whitespace inside braces', () => {
    expect(substitutePlaceholders('hi {{  name  }}', { name: 'Eric' })).toBe('hi Eric');
  });
});

describe('materializeTenantWorkspaceFromTemplate', () => {
  const vars = {
    tenantId: 'member-0042',
    agentId: 'member-0042-pulse',
    agentName: 'Eric的pulse管理员',
    displayName: 'Eric',
    provisionedAtIso: '2026-05-08T10:00:00+0800',
  };

  it('copies non-skip files and substitutes placeholders', () => {
    const tplRoot = mkTmp('tpl-');
    const dst = mkTmp('dst-');
    makeFakeTemplate(tplRoot);

    const r = materializeTenantWorkspaceFromTemplate({
      templateRoot: tplRoot,
      workspaceAbs: dst,
      vars,
    });

    expect(r.copied.sort()).toEqual(['CLAUDE.md', 'IDENTITY.md', join('nested', 'note.md')].sort());
    expect(r.skipped).toContain('README.md');

    const claude = readFileSync(join(dst, 'CLAUDE.md'), 'utf8');
    expect(claude).toContain('# Eric的pulse管理员');
    expect(claude).toContain('租户 member-0042 的 pulse 管理员');
    expect(claude).toContain('agent=member-0042-pulse');
    expect(claude).not.toMatch(/\{\{[a-zA-Z]+\}\}/);

    const identity = readFileSync(join(dst, 'IDENTITY.md'), 'utf8');
    expect(identity).toContain('serve Eric only');
    expect(identity).toContain('2026-05-08T10:00:00+0800');

    const nested = readFileSync(join(dst, 'nested', 'note.md'), 'utf8');
    expect(nested).toBe('tenant=member-0042\n');

    expect(existsSync(join(dst, 'README.md'))).toBe(false);
  });

  it('does not overwrite existing destination files (preserveExisting=true default)', () => {
    const tplRoot = mkTmp('tpl-');
    const dst = mkTmp('dst-');
    makeFakeTemplate(tplRoot);

    writeFileSync(join(dst, 'CLAUDE.md'), 'USER EDIT — keep me\n', 'utf8');

    const r = materializeTenantWorkspaceFromTemplate({
      templateRoot: tplRoot,
      workspaceAbs: dst,
      vars,
    });

    expect(r.skipped).toContain('CLAUDE.md');
    expect(r.copied).not.toContain('CLAUDE.md');

    const claude = readFileSync(join(dst, 'CLAUDE.md'), 'utf8');
    expect(claude).toBe('USER EDIT — keep me\n');

    expect(readFileSync(join(dst, 'IDENTITY.md'), 'utf8')).toContain('serve Eric only');
  });

  it('is idempotent on second call (no errors, all skipped)', () => {
    const tplRoot = mkTmp('tpl-');
    const dst = mkTmp('dst-');
    makeFakeTemplate(tplRoot);

    materializeTenantWorkspaceFromTemplate({ templateRoot: tplRoot, workspaceAbs: dst, vars });
    const r2 = materializeTenantWorkspaceFromTemplate({ templateRoot: tplRoot, workspaceAbs: dst, vars });

    expect(r2.copied).toEqual([]);
    expect(r2.skipped.sort()).toContain('CLAUDE.md');
    expect(r2.skipped).toContain('IDENTITY.md');
    expect(r2.skipped).toContain('README.md');
  });

  it('throws when template root does not exist', () => {
    const dst = mkTmp('dst-');
    expect(() =>
      materializeTenantWorkspaceFromTemplate({
        templateRoot: join(tmpdir(), 'tpl-does-not-exist-xyz-' + Date.now()),
        workspaceAbs: dst,
        vars,
      }),
    ).toThrow(/tenant_template_missing:/);
  });

  it('honours custom skipFiles list', () => {
    const tplRoot = mkTmp('tpl-');
    const dst = mkTmp('dst-');
    makeFakeTemplate(tplRoot);

    const r = materializeTenantWorkspaceFromTemplate({
      templateRoot: tplRoot,
      workspaceAbs: dst,
      vars,
      skipFiles: ['IDENTITY.md'],
    });

    expect(r.skipped).toContain('IDENTITY.md');
    expect(r.copied).toContain('CLAUDE.md');
    // README.md is no longer in default skip set, so it's now copied through
    expect(r.copied).toContain('README.md');
  });
});

describe('defaultTenantPulseAdminTemplateRoot', () => {
  it('resolves to the bundled tenant-pulse-admin/ directory', () => {
    const p = defaultTenantPulseAdminTemplateRoot();
    expect(p.endsWith(join('templates', 'tenant-pulse-admin'))).toBe(true);
    expect(existsSync(p)).toBe(true);
    expect(existsSync(join(p, 'CLAUDE.md'))).toBe(true);
    expect(existsSync(join(p, 'IDENTITY.md'))).toBe(true);
    expect(existsSync(join(p, 'SOUL.md'))).toBe(true);
    expect(existsSync(join(p, 'TOOLS.md'))).toBe(true);
    expect(existsSync(join(p, 'MEMORY.md'))).toBe(true);
    expect(existsSync(join(p, 'README.md'))).toBe(true);
  });

  it('bundled template files contain {{var}} placeholders ready for substitution', () => {
    const p = defaultTenantPulseAdminTemplateRoot();
    const claude = readFileSync(join(p, 'CLAUDE.md'), 'utf8');
    expect(claude).toContain('{{tenantId}}');
    expect(claude).toContain('{{agentId}}');
    expect(claude).toContain('{{agentName}}');
  });
});
