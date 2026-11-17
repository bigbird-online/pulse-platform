import { spawn } from 'node:child_process';

/**
 * 若设置 `PLATFORM_RELAY_RELOAD_CMD`，用 shell 非阻塞触发（如 `systemctl reload relay`）。
 * 不重载也不视为 provision 失败。
 */
export function runRelayReloadHook(env: NodeJS.ProcessEnv = process.env): Promise<{ ran: boolean; detail?: string }> {
  const cmd = env['PLATFORM_RELAY_RELOAD_CMD']?.trim();
  if (!cmd) return Promise.resolve({ ran: false });
  return new Promise((resolve) => {
    const child = spawn(cmd, { shell: true, stdio: 'ignore', detached: true });
    child.unref();
    child.on('error', (err) => resolve({ ran: true, detail: String(err) }));
    child.on('close', (code) => resolve({ ran: true, detail: `exit:${code}` }));
  });
}
