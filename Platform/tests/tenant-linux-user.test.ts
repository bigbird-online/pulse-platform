import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';

// Mock child_process.execSync before importing the module under test
const mockExecSync = vi.fn<(cmd: string, opts?: unknown) => Buffer>();
vi.mock('node:child_process', () => ({
  execSync: (...args: unknown[]) => mockExecSync(...args as [string, unknown?]),
}));

import { ensureTenantLinuxUser } from '../src/tenant-linux-user.js';

describe('ensureTenantLinuxUser [PT0-019]', () => {
  const tenantId = 'member-0099';
  const workspace = '/opt/PULSE/Tenants/member-0099';
  const expectedUser = 'tenant-member-0099';

  beforeEach(() => {
    mockExecSync.mockReset();
    // execSync should return a Buffer (default)
    mockExecSync.mockReturnValue(Buffer.from(''));
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  it('creates Linux user and sets directory permissions', () => {
    // Simulate: `id <user>` fails (user does not exist) → triggers useradd
    mockExecSync.mockImplementation((cmd: string) => {
      if (cmd.startsWith('id ')) throw new Error('no such user');
      return Buffer.from('');
    });

    ensureTenantLinuxUser({ tenantId, workspace, forceLinux: true });

    // Should call useradd
    expect(mockExecSync).toHaveBeenCalledWith(
      expect.stringContaining(`useradd -r -m -d ${workspace} -s /bin/bash ${expectedUser}`),
      expect.any(Object),
    );
    // Should call chown
    expect(mockExecSync).toHaveBeenCalledWith(
      expect.stringContaining(`chown -R ${expectedUser}:${expectedUser} ${workspace}`),
      expect.any(Object),
    );
    // Should call chmod 700
    expect(mockExecSync).toHaveBeenCalledWith(
      expect.stringContaining(`chmod 700 ${workspace}`),
      expect.any(Object),
    );
    // Should call setfacl for pulse user
    expect(mockExecSync).toHaveBeenCalledWith(
      expect.stringContaining(`setfacl -m u:pulse:rwx ${workspace}`),
      expect.any(Object),
    );
  });

  it('skips useradd when user already exists (idempotent)', () => {
    // Simulate: `id <user>` succeeds (user exists)
    mockExecSync.mockImplementation((cmd: string) => {
      if (cmd.startsWith('id ')) return Buffer.from(`${expectedUser}`);
      if (cmd.includes('useradd')) throw new Error('user already exists');
      return Buffer.from('');
    });

    ensureTenantLinuxUser({ tenantId, workspace, forceLinux: true });

    // useradd should NOT be called
    const useraddCalls = mockExecSync.mock.calls.filter(
      ([cmd]) => typeof cmd === 'string' && cmd.includes('useradd'),
    );
    expect(useraddCalls).toHaveLength(0);

    // But chown/chmod/setfacl should still be called (repair permissions)
    const chownCalls = mockExecSync.mock.calls.filter(
      ([cmd]) => typeof cmd === 'string' && cmd.includes('chown'),
    );
    expect(chownCalls.length).toBeGreaterThanOrEqual(1);
  });

  it('returns the linuxUser name for relay.json', () => {
    const result = ensureTenantLinuxUser({ tenantId, workspace });
    expect(result.linuxUser).toBe(expectedUser);
  });
});
