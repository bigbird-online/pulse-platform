import { closeSync, constants, fstatSync, lstatSync, openSync, readFileSync } from 'node:fs';
import { dirname, isAbsolute } from 'node:path';

/**
 * Relay service credentials live in one private env file instead of the
 * service definition (launchd plist / systemd unit). The service definition
 * names the file through this variable; Relay loads it before any other
 * configuration and never falls back to plaintext copies.
 */
export const RELAY_SECRETS_ENV_FILE = 'RELAY_SECRETS_ENV_FILE';

const NAME = /^[A-Z][A-Z0-9_]*$/;
/** Only credential-shaped names are accepted; the file cannot smuggle PATH, NODE_OPTIONS and the like. */
const SECRET_NAME = /^(?:RELAY_BOT_TOKEN_[A-Z0-9_]+|[A-Z][A-Z0-9_]*_(?:TOKEN|API_KEY|SECRET|PASSWORD))$/;
/** Relay-internal credentials no Agent session or helper needs. */
const RELAY_INTERNAL_TOKEN = 'RELAY_INTERNAL_TOKEN';
const BOT_TOKEN_NAME = /^(?:TELEGRAM_BOT_TOKEN|BOT_TOKEN_[A-Z0-9_]*|RELAY_BOT_TOKEN_[A-Z0-9_]*)$/;
/**
 * Provider key pools Relay routes with (model catalog api_key_pool). Recognised
 * by name together with SECRET_NAME, so hosts that inject them through their
 * own service EnvironmentFile (TCloud systemd) are covered as well; callers
 * inject the selected route explicitly.
 */
const PROVIDER_POOL_PREFIX = /^OPENROUTER_/;

const loadedKeys = new Set<string>();

export class RelaySecretsError extends Error {
  constructor(message: string) {
    super(message);
    this.name = 'RelaySecretsError';
  }
}

export function isRelaySecretName(name: string): boolean {
  return SECRET_NAME.test(name);
}

/** Parse `KEY=value`, `KEY='value'`, `KEY="value"`, optional `export`, blank and `#` lines. Messages carry line numbers, never values. */
export function parseRelaySecretsEnv(text: string): Map<string, string> {
  const values = new Map<string, string>();
  text.split(/\r?\n/).forEach((raw, index) => {
    const line = raw.trim();
    if (!line || line.startsWith('#')) return;
    const assignment = line.match(/^(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$/);
    if (!assignment) throw new RelaySecretsError(`line ${index + 1} is not KEY=value`);
    const [, key, rest] = assignment as unknown as [string, string, string];
    if (!NAME.test(key) || !isRelaySecretName(key)) {
      throw new RelaySecretsError(`line ${index + 1} names ${key}, which is not a credential name`);
    }
    let value: string;
    if (rest.startsWith('"') || rest.startsWith("'")) {
      // A quoted value must close, and only blanks or a `#` comment may follow.
      const quoted = rest.match(/^(?:"([^"]*)"|'([^']*)')(?:\s+#.*|\s*)$/);
      if (!quoted) throw new RelaySecretsError(`line ${index + 1} has an unterminated quote or text after the closing quote for ${key}`);
      value = quoted[1] ?? quoted[2] ?? '';
    } else {
      value = rest.split('#', 1)[0]!.trim();
    }
    if (!value || /[\0\r\n]/.test(value)) throw new RelaySecretsError(`line ${index + 1} has an empty or invalid value for ${key}`);
    if (values.has(key)) throw new RelaySecretsError(`${key} is declared more than once`);
    values.set(key, value);
  });
  return values;
}

function assertPrivate(kind: string, path: string, info: { uid: number; mode: number }): void {
  const uid = process.getuid?.();
  if (uid !== undefined && info.uid !== uid) throw new RelaySecretsError(`${kind} ${path} belongs to another account`);
  if (info.mode & 0o077) {
    throw new RelaySecretsError(`${kind} ${path} is accessible to other users (mode ${(info.mode & 0o777).toString(8)}); expected ${kind === 'directory' ? '0700' : '0600'}`);
  }
}

/** Read and validate the declared file without touching `env`. */
export function readRelaySecretsFile(path: string): Map<string, string> {
  if (!path || !isAbsolute(path) || /[\0\r\n]/.test(path)) {
    throw new RelaySecretsError(`${RELAY_SECRETS_ENV_FILE} must be an absolute path`);
  }
  const directory = dirname(path);
  let dirInfo;
  try { dirInfo = lstatSync(directory); }
  catch { throw new RelaySecretsError(`secrets directory ${directory} is missing`); }
  if (!dirInfo.isDirectory()) throw new RelaySecretsError(`secrets directory ${directory} is not a real directory`);
  assertPrivate('directory', directory, dirInfo);
  let fd: number;
  try { fd = openSync(path, constants.O_RDONLY | constants.O_NOFOLLOW); }
  catch (error) {
    const code = (error as NodeJS.ErrnoException).code;
    if (code === 'ENOENT') throw new RelaySecretsError(`secrets env file ${path} is missing`);
    if (code === 'ELOOP') throw new RelaySecretsError(`secrets env file ${path} is a symbolic link`);
    throw new RelaySecretsError(`secrets env file ${path} cannot be opened (${code ?? 'error'})`);
  }
  try {
    const info = fstatSync(fd);
    if (!info.isFile()) throw new RelaySecretsError(`secrets env file ${path} is not a regular file`);
    assertPrivate('file', path, info);
    return parseRelaySecretsEnv(readFileSync(fd, 'utf8'));
  } finally {
    closeSync(fd);
  }
}

/**
 * Load the declared secrets file into `env`. Returns the loaded key names, or
 * undefined when the service declares no file (hosts that inject credentials
 * through their own private EnvironmentFile). A declared file that is missing,
 * too permissive or malformed, or a credential that also appears in plaintext
 * in the service environment, refuses startup.
 */
export function loadRelaySecrets(env: NodeJS.ProcessEnv = process.env): string[] | undefined {
  const path = env[RELAY_SECRETS_ENV_FILE];
  if (path === undefined) return undefined;
  const values = readRelaySecretsFile(path);
  for (const key of values.keys()) {
    if (env[key] !== undefined && !loadedKeys.has(key)) {
      throw new RelaySecretsError(`${key} is also set in the service environment; remove the plaintext copy`);
    }
  }
  for (const [key, value] of values) {
    env[key] = value;
    loadedKeys.add(key);
  }
  return [...values.keys()].sort();
}

/**
 * Remove Relay's own credentials from a child environment, however the
 * service received them (secrets file, systemd EnvironmentFile, plist): every
 * key loaded from the secrets file, bot tokens, provider key pools
 * (credential-shaped OPENROUTER_* names), the file locator and (unless kept for a Relay-owned helper
 * that calls the loopback API) RELAY_INTERNAL_TOKEN. Provider credentials a
 * runtime needs are injected explicitly by its caller.
 */
export function stripRelaySecrets<T extends Record<string, string | undefined>>(
  env: T,
  options: { keepInternalToken?: boolean } = {},
): T {
  delete env[RELAY_SECRETS_ENV_FILE];
  for (const key of Object.keys(env)) {
    if (options.keepInternalToken && key === RELAY_INTERNAL_TOKEN) continue;
    if (loadedKeys.has(key) || BOT_TOKEN_NAME.test(key) || (PROVIDER_POOL_PREFIX.test(key) && SECRET_NAME.test(key)) || key === RELAY_INTERNAL_TOKEN) {
      delete env[key];
    }
  }
  return env;
}

/** A copy of the current process environment without Relay's credentials. */
export function relayChildEnv(options: { keepInternalToken?: boolean } = {}): NodeJS.ProcessEnv {
  return stripRelaySecrets({ ...process.env }, options);
}

/** Names Relay withholds from a child even when a configuration allowlists them. */
export function isRelayInternalSecret(name: string): boolean {
  return name === RELAY_INTERNAL_TOKEN || name === RELAY_SECRETS_ENV_FILE || BOT_TOKEN_NAME.test(name);
}

/** Test seam: forget which keys came from the file. */
export function resetRelaySecretsForTest(): void {
  loadedKeys.clear();
}
