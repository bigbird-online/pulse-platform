import { execFile } from 'node:child_process';
import { constants, closeSync, fstatSync, openSync, readFileSync, realpathSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { promisify } from 'node:util';

const run = promisify(execFile);
const refreshHelper = fileURLToPath(new URL('./pi-codex-oauth.py', import.meta.url));
const provider = 'openai-codex';
const endpoint = 'https://chatgpt.com/backend-api';
const source = 'Codex CLI OAuth';
const tokenPattern = /^[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+$/;

function privateJson(path, maxSize, excludedMode) {
  const fd = openSync(path, constants.O_RDONLY | constants.O_NOFOLLOW | constants.O_NONBLOCK);
  try {
    const info = fstatSync(fd);
    if (!info.isFile() || info.uid !== process.getuid() || info.mode & excludedMode ||
        info.nlink !== 1 || info.size < 1 || info.size > maxSize) {
      throw new Error('Invalid protected account file');
    }
    return JSON.parse(readFileSync(fd, 'utf8'));
  } finally {
    closeSync(fd);
  }
}

function installedBinding(entrypoint) {
  const path = join(dirname(realpathSync(entrypoint)), 'host-policy.json');
  return privateJson(path, 1024 * 1024, 0o022);
}

function readAccess(binding) {
  try {
    if (realpathSync(dirname(binding.credential_file)) !== dirname(binding.credential_file)) {
      throw new Error('Codex account directory redirects');
    }
    const data = privateJson(binding.credential_file, 128 * 1024, 0o077);
    const token = data.tokens?.access_token;
    if (data.auth_mode !== 'chatgpt' || typeof token !== 'string' ||
        token.length > 16384 || !tokenPattern.test(token)) {
      throw new Error('Invalid Codex access credential');
    }
    const expires = JSON.parse(Buffer.from(token.split('.')[1], 'base64url').toString()).exp;
    if (!Number.isSafeInteger(expires)) throw new Error('Codex access expiry is missing');
    return { token, expires };
  } catch {
    throw new Error('Installed Codex login unavailable');
  }
}

async function currentAccess(python, node, binding, signal) {
  let access = readAccess(binding);
  if (access.expires <= Date.now() / 1000 + 120) {
    try {
      await run(python, ['-I', '-B', refreshHelper, binding.credential_file, binding.codex, node], {
        timeout: 24000, maxBuffer: 64 * 1024, signal,
      });
    } catch {
      throw new Error('Installed Codex login refresh unavailable');
    }
    access = readAccess(binding);
    if (access.expires <= Date.now() / 1000 + 120) {
      throw new Error('Installed Codex login refresh unavailable');
    }
  }
  return access.token;
}

export function installSharedCodexOAuth(pi, host) {
  const policy = installedBinding(host.entrypoint);
  const binding = policy.pi_codex_oauth;
  if (!binding) throw new Error('Installed Codex account binding is unavailable');

  pi.on('session_start', (_, ctx) => {
    const original = ctx.modelRegistry.getProvider(provider);
    if (!original || original.baseUrl !== endpoint) {
      throw new Error('The built-in OpenAI Codex provider is unavailable');
    }
    pi.registerProvider({
      ...original,
      auth: {
        apiKey: {
          name: source,
          async check() {
            try {
              readAccess(binding);
              return { type: 'oauth', source };
            } catch {
              return undefined;
            }
          },
          async resolve({ signal }) {
            const apiKey = await currentAccess(policy.python, policy.producer_node, binding, signal);
            return { auth: { apiKey, baseUrl: endpoint }, source };
          },
        },
      },
    });
  });
}

export default function install(pi) {
  const host = JSON.parse(process.env.PULSE_PI_CODEX_OAUTH_HOST ?? '{}');
  if (typeof host.entrypoint !== 'string') throw new Error('Installed Codex account host is unavailable');
  installSharedCodexOAuth(pi, host);
}
