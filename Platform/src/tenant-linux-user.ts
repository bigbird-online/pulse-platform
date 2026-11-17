import { execSync } from 'node:child_process';

/**
 * PT0-019: Ensure a dedicated Linux system user exists for the tenant
 * and the workspace directory has correct ownership/permissions.
 *
 * This function is idempotent: if the user already exists it skips useradd
 * but still repairs directory permissions and ACL.
 *
 * Only runs on Linux (detected by `process.platform`). On other platforms
 * (macOS dev), it is a no-op and returns the expected linuxUser name anyway
 * so relay.json can be populated consistently.
 */
export function ensureTenantLinuxUser(opts: {
  tenantId: string;
  workspace: string;
  /** Override execSync for tests. */
  execImpl?: (cmd: string, opts?: { encoding?: string }) => Buffer | string;
  /** Force Linux behavior for tests on non-Linux hosts. */
  forceLinux?: boolean;
}): { linuxUser: string } {
  const { tenantId, workspace } = opts;
  const exec = opts.execImpl ?? ((cmd: string, o?: { encoding?: string }) => execSync(cmd, { ...o, encoding: 'utf8' }));

  const linuxUser = `tenant-${tenantId}`;
  const isLinux = opts.forceLinux === true || process.platform === 'linux';

  if (!isLinux) {
    // macOS / dev: no-op, just return the name for relay.json
    return { linuxUser };
  }

  // Check if user already exists
  const userExists = (() => {
    try {
      exec(`id -u ${linuxUser}`, { encoding: 'utf8' });
      return true;
    } catch {
      return false;
    }
  })();

  if (!userExists) {
    exec(`useradd -r -m -d ${workspace} -s /bin/bash ${linuxUser}`, { encoding: 'utf8' });
  }

  // Always repair ownership and permissions (idempotent)
  exec(`chown -R ${linuxUser}:${linuxUser} ${workspace}`, { encoding: 'utf8' });
  exec(`chmod 700 ${workspace}`, { encoding: 'utf8' });
  // Grant pulse user rwx via ACL so Relay can chdir and manage files.
  // Must set mask explicitly: chmod 700 sets mask to ---, which disables
  // all named ACL entries. setfacl -m alone does NOT always recalculate
  // the mask on all distros, so we force it.
  exec(`setfacl -m u:pulse:rwx ${workspace}`, { encoding: 'utf8' });
  exec(`setfacl -m m::rwx ${workspace}`, { encoding: 'utf8' });

  return { linuxUser };
}
