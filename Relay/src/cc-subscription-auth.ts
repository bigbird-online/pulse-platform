import { readFileSync } from 'node:fs';
import { isAbsolute, join } from 'node:path';

export const CC_OAUTH_ENV_FILE = 'RELAY_CC_OAUTH_ENV_FILE';
const OAUTH_KEYS = ['CLAUDE_CODE_OAUTH_TOKEN', 'CLAUDE_CODE_OAUTH_TOKEN_FALLBACK'] as const;
const ROUTING_ENV = {
  ANTHROPIC_BASE_URL: 'https://api.anthropic.com',
  ANTHROPIC_AUTH_TOKEN: '',
  ANTHROPIC_API_KEY: '',
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
