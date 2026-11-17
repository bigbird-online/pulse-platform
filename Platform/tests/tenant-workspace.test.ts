import { describe, it, expect } from 'vitest';
import { mkdtemp, readFile, stat } from 'node:fs/promises';
import { join } from 'node:path';
import { tmpdir } from 'node:os';
import { ensureTenantWorkspaceDir } from '../src/relay-merge.js';

describe('tenant workspace scaffold', () => {
  it('writes CLAUDE.md with tenant identity when missing', async () => {
    const dir = await mkdtemp(join(tmpdir(), 'tenant-ws-'));
    ensureTenantWorkspaceDir(dir, {
      tenantId: 'member-0004',
      agentId: 'member-0004-pulse',
      agentName: '长征的pulse管理员',
    });
    const p = join(dir, 'CLAUDE.md');
    const s = await stat(p);
    expect(s.isFile()).toBe(true);
    const txt = await readFile(p, 'utf8');
    expect(txt).toContain('租户 member-0004 的 pulse 管理员');
    expect(txt).toContain('你 **不是** PULSE 平台管家');
    expect(txt).toContain('member-0004-pulse');
  });
});

