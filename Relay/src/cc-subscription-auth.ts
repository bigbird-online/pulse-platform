import { closeSync, constants, fstatSync, openSync, readFileSync } from 'node:fs';
import { basename, dirname, isAbsolute, join } from 'node:path';

export const CC_OAUTH_ENV_FILE = 'RELAY_CC_OAUTH_ENV_FILE';
export type CcAuthMode = 'subscription' | 'api';
const OAUTH_KEYS = ['CLAUDE_CODE_OAUTH_TOKEN', 'CLAUDE_CODE_OAUTH_TOKEN_FALLBACK'] as const;
const ROUTING_ENV = {
  ANTHROPIC_BASE_URL: 'https://api.anthropic.com',
  ANTHROPIC_AUTH_TOKEN: '',
  ANTHROPIC_API_KEY: '',
  ANTHROPIC_DEFAULT_FABLE_MODEL: '',
  ANTHROPIC_DEFAULT_HAIKU_MODEL: '',
  ANTHROPIC_DEFAULT_SONNET_MODEL: '',
  ANTHROPIC_DEFAULT_OPUS_MODEL: '',
  ANTHROPIC_CUSTOM_MODEL_OPTION: '',
};

/** Read one literal setup token on each spawn; never load the whole file into Relay's environment. */
export function readCcSubscriptionToken(file: string): string {
  try {
    if (!isAbsolute(file) || /[\0\r\n]/.test(file)) throw new Error();
    let token: string | undefined;
    for (const line of readFileSync(file, 'utf8').split(/\r?\n/)) {
      const assignment = line.match(/^\s*(?:export\s+)?CLAUDE_CODE_OAUTH_TOKEN\s*=\s*(.*)$/);
      if (!assignment) continue;
      const value = assignment[1].trim();
      const quoted = value.match(/^(?:"([^"]*)"|'([^']*)')\s*(?:#.*)?$/);
      token = quoted ? (quoted[1] ?? quoted[2]) : value.split('#', 1)[0].trim();
    }
    if (!token || !/^[A-Za-z0-9_-]+$/.test(token)) throw new Error();
    return token;
  } catch {
    // Neither parser failures nor filesystem exceptions may include credential contents.
    throw new Error('CC subscription credential unavailable in declared environment file');
  }
}

/** A persisted settings token would override the child's current environment. Migration is explicit. */
export function assertNoCcTokenCopies(home: string | undefined, workspace: string): void {
  const paths = new Set([
    ...(home ? [join(home, '.claude', 'settings.json')] : []),
    join(workspace, '.claude', 'settings.json'),
    join(workspace, '.claude', 'settings.local.json'),
  ]);
  for (const path of paths) {
    let settings: { env?: Record<string, unknown> };
    try { settings = JSON.parse(readFileSync(path, 'utf8')); }
    catch (error) {
      if ((error as NodeJS.ErrnoException).code === 'ENOENT') continue;
      throw new Error('CC subscription settings could not be validated');
    }
    if (OAUTH_KEYS.some(key => settings.env?.[key])) {
      throw new Error('Remove CC OAuth token copies from settings before using the declared credential source');
    }
  }
}

export function applyCcSubscriptionAuth(env: NodeJS.ProcessEnv, token: string): void {
  Object.assign(env, ROUTING_ENV, { CLAUDE_CODE_OAUTH_TOKEN: token });
  delete env.CLAUDE_CODE_OAUTH_TOKEN_FALLBACK;
}

/** Command-line settings contain only routing controls; the token stays exclusively in child env. */
export function ccSubscriptionSettings(): string {
  return JSON.stringify({ apiKeyHelper: '', env: ROUTING_ENV });
}

function readPrivateFile(path: string): string {
  const fd = openSync(path, constants.O_RDONLY | constants.O_NOFOLLOW | constants.O_NONBLOCK);
  try {
    const info = fstatSync(fd);
    if (!info.isFile() || info.uid !== process.getuid?.() || (info.mode & 0o077) !== 0 || info.size > 1024) {
      throw new Error('Invalid private CC authentication file');
    }
    return readFileSync(fd, 'utf8');
  } finally {
    closeSync(fd);
  }
}

/** Optional mode beside a workstation's registered subscription file. */
export function readCcAuthMode(oauthFile: string): CcAuthMode {
  if (!isAbsolute(oauthFile) || /[\0\r\n]/.test(oauthFile) || basename(oauthFile) !== 'relay.env') {
    return 'subscription';
  }
  try {
    const raw = readPrivateFile(join(dirname(oauthFile), 'relay-auth'));
    const match = /^claude_auth=(subscription|api)\n?$/.exec(raw);
    if (match) return match[1] === 'api' ? 'api' : 'subscription';
  } catch (error) {
    if ((error as NodeJS.ErrnoException).code === 'ENOENT') return 'subscription';
  }
  throw new Error('Invalid CC authentication selection');
}

export function ccConsoleSettings(oauthFile: string, env: NodeJS.ProcessEnv): string {
  const keyPath = join(dirname(oauthFile), 'anthropic-api.key');
  try {
    const key = readPrivateFile(keyPath).trim();
    if (!/^sk-ant-[A-Za-z0-9_-]+$/.test(key) || key.startsWith('sk-ant-oat')) throw new Error();
  } catch {
    throw new Error('CC Console API key unavailable');
  }
  const routing = { ...ROUTING_ENV, CLAUDE_CODE_OAUTH_TOKEN: '', CLAUDE_CODE_OAUTH_TOKEN_FALLBACK: '' };
  Object.assign(env, routing);
  const quotedKeyPath = "'" + keyPath.replaceAll("'", "'\"'\"'") + "'";
  return JSON.stringify({ apiKeyHelper: `/bin/cat ${quotedKeyPath}`, env: routing });
}
