import { chmodSync, mkdtempSync, realpathSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { describe, expect, it, vi } from 'vitest';
// @ts-expect-error Pi loads this reviewed ESM extension directly.
import install, { installSharedCodexOAuth } from '../runtime/pi-codex-oauth.mjs';

const endpoint = 'https://chatgpt.com/backend-api';

interface AccountFixture {
  start: () => {
    check: () => Promise<{ type: string; source: string } | undefined>;
    resolve: (options: { signal?: AbortSignal }) => Promise<{ auth: { apiKey: string; baseUrl: string } }>;
  };
  write: (seconds: number, mode?: number) => string;
}

function accountFixture(run: (fixture: AccountFixture) => Promise<void>) {
  const root = mkdtempSync(join(realpathSync(tmpdir()), 'pi-codex-account-'));
  const credentialFile = join(root, 'auth.json');
  const entrypoint = join(root, 'harness.pyz');
  writeFileSync(entrypoint, '');
  writeFileSync(join(root, 'host-policy.json'), JSON.stringify({
    python: '/usr/bin/false',
    producer_node: process.execPath,
    pi_codex_oauth: { credential_file: credentialFile, codex: '/unused' },
  }), { mode: 0o600 });
  const on = vi.fn();
  const registerProvider = vi.fn();
  installSharedCodexOAuth({ on, registerProvider }, { entrypoint });
  const original = { id: 'openai-codex', baseUrl: endpoint, models: [{ id: 'original' }] };
  const fixture = {
    start() {
      expect(on.mock.calls[0][0]).toBe('session_start');
      on.mock.calls[0][1](null, { modelRegistry: { getProvider: () => original } });
      const registered = registerProvider.mock.calls[0][0];
      expect(registered.models).toBe(original.models);
      return registered.auth.apiKey;
    },
    write(seconds: number, mode = 0o600) {
      const exp = Math.floor(Date.now() / 1000) + seconds;
      const token = 'header.' + Buffer.from(JSON.stringify({ exp })).toString('base64url') + '.signature';
      writeFileSync(credentialFile, JSON.stringify({ auth_mode: 'chatgpt', tokens: { access_token: token } }));
      chmodSync(credentialFile, mode);
      return token;
    },
  };
  return Promise.resolve().then(() => run(fixture)).finally(() => rmSync(root, { recursive: true, force: true }));
}

describe('Oracle shared Codex OAuth', () => {
  it('requires the reviewed launcher binding', () => {
    vi.stubEnv('PULSE_PI_CODEX_OAUTH_HOST', undefined);
    try {
      const on = vi.fn();
      expect(() => install({ on })).toThrow('Installed Codex account host is unavailable');
      expect(on).not.toHaveBeenCalled();
    } finally { vi.unstubAllEnvs(); }
  });

  it('reads the installed credential anew and rejects public credentials', () => accountFixture(async ({ start, write }) => {
    const auth = start();
    const first = write(1000);
    await expect(auth.check()).resolves.toMatchObject({ type: 'oauth' });
    await expect(auth.resolve({})).resolves.toMatchObject({ auth: { apiKey: first, baseUrl: endpoint } });
    const next = write(2000);
    await expect(auth.resolve({})).resolves.toMatchObject({ auth: { apiKey: next, baseUrl: endpoint } });
    write(2000, 0o644);
    await expect(auth.check()).resolves.toBeUndefined();
    await expect(auth.resolve({})).rejects.toThrow('Installed Codex login unavailable');
  }));

  it('requires a managed refresh inside the expiry window and maps its failure', () => accountFixture(async ({ start, write }) => {
    const auth = start();
    write(30);
    await expect(auth.resolve({})).rejects.toThrow('Installed Codex login refresh unavailable');
  }));
});
