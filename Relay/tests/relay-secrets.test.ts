import { afterEach, beforeEach, describe, expect, it } from 'vitest';
import { chmodSync, existsSync, mkdirSync, mkdtempSync, rmSync, symlinkSync, writeFileSync } from 'node:fs';
import { spawn } from 'node:child_process';
import { once } from 'node:events';
import { join } from 'node:path';
import { tmpdir } from 'node:os';
import {
  RELAY_SECRETS_ENV_FILE,
  isRelayInternalSecret,
  loadRelaySecrets,
  parseRelaySecretsEnv,
  relayChildEnv,
  resetRelaySecretsForTest,
  stripRelaySecrets,
} from '../src/relay-secrets.js';
import { buildCodexEnv } from '../src/codex-conversation-runtime.js';
import { buildSdkEngineEnv } from '../src/cc-sdk-conversation-runtime.js';

const FIXTURE = [
  '# Relay service credentials',
  "RELAY_INTERNAL_TOKEN='fixture-internal'",
  "RELAY_BOT_TOKEN_AKI='123:fixture-bot'",
  "OPENROUTER_API_KEY='fixture-openrouter'",
  '',
].join('\n');

let root: string;
let directory: string;
let file: string;

function writeSecrets(text = FIXTURE, mode = 0o600): void {
  writeFileSync(file, text, { mode });
  chmodSync(file, mode);
}

beforeEach(() => {
  resetRelaySecretsForTest();
  root = mkdtempSync(join(tmpdir(), 'relay-secrets-'));
  directory = join(root, 'secrets');
  mkdirSync(directory, { mode: 0o700 });
  chmodSync(directory, 0o700);
  file = join(directory, 'relay-service.env');
});

afterEach(() => {
  resetRelaySecretsForTest();
  rmSync(root, { recursive: true, force: true });
});

describe('parseRelaySecretsEnv', () => {
  it('accepts quoted, unquoted and exported credential assignments', () => {
    const values = parseRelaySecretsEnv('export A_TOKEN="x"\nB_API_KEY=y # note\nRELAY_BOT_TOKEN_X=\'1:z\'\n');
    expect(Object.fromEntries(values)).toEqual({ A_TOKEN: 'x', B_API_KEY: 'y', RELAY_BOT_TOKEN_X: '1:z' });
  });

  it.each([
    ['PATH=/tmp', 'not a credential name'],
    ['NODE_OPTIONS=--require=x', 'not a credential name'],
    ['A_TOKEN=', 'empty or invalid value'],
    ['A_TOKEN=x\nA_TOKEN=y', 'more than once'],
    ['garbage line', 'not KEY=value'],
    ["RELAY_INTERNAL_TOKEN='unterminated", 'unterminated quote'],
    ['OPENROUTER_API_KEY="valid" garbage', 'text after the closing quote'],
    ['A_TOKEN="x"y', 'text after the closing quote'],
    ['A_TOKEN="x\'', 'unterminated quote'],
  ])('rejects %j without echoing values', (text, reason) => {
    expect(() => parseRelaySecretsEnv(text)).toThrow(reason);
    try { parseRelaySecretsEnv(text); } catch (error) {
      expect((error as Error).message).not.toMatch(/unterminated'?$|valid"|garbage|"x/);
    }
  });

  it('allows blanks and a comment after a closed quote', () => {
    expect(Object.fromEntries(parseRelaySecretsEnv("A_TOKEN='x'   \nB_TOKEN=\"y\"  # note\n"))).toEqual({ A_TOKEN: 'x', B_TOKEN: 'y' });
  });
});

describe('loadRelaySecrets', () => {
  it('does nothing when the service declares no file', () => {
    const env: NodeJS.ProcessEnv = {};
    expect(loadRelaySecrets(env)).toBeUndefined();
    expect(env).toEqual({});
  });

  it('loads a private file into the environment', () => {
    writeSecrets();
    const env: NodeJS.ProcessEnv = { [RELAY_SECRETS_ENV_FILE]: file };
    expect(loadRelaySecrets(env)).toEqual(['OPENROUTER_API_KEY', 'RELAY_BOT_TOKEN_AKI', 'RELAY_INTERNAL_TOKEN']);
    expect(env['RELAY_INTERNAL_TOKEN']).toBe('fixture-internal');
    expect(env['OPENROUTER_API_KEY']).toBe('fixture-openrouter');
  });

  it('refuses a missing file instead of falling back to plaintext', () => {
    const env: NodeJS.ProcessEnv = { [RELAY_SECRETS_ENV_FILE]: file };
    expect(() => loadRelaySecrets(env)).toThrow('is missing');
  });

  it('refuses a file readable by others', () => {
    writeSecrets(FIXTURE, 0o644);
    expect(() => loadRelaySecrets({ [RELAY_SECRETS_ENV_FILE]: file })).toThrow('accessible to other users (mode 644); expected 0600');
  });

  it('refuses a directory open to others', () => {
    writeSecrets();
    chmodSync(directory, 0o755);
    expect(() => loadRelaySecrets({ [RELAY_SECRETS_ENV_FILE]: file })).toThrow('expected 0700');
  });

  it('refuses a symbolic link and a relative path', () => {
    writeSecrets();
    const link = join(directory, 'link.env');
    symlinkSync(file, link);
    expect(() => loadRelaySecrets({ [RELAY_SECRETS_ENV_FILE]: link })).toThrow('symbolic link');
    expect(() => loadRelaySecrets({ [RELAY_SECRETS_ENV_FILE]: 'relay-service.env' })).toThrow('absolute path');
  });

  it('refuses a credential that is also present in plaintext', () => {
    writeSecrets();
    const env: NodeJS.ProcessEnv = { [RELAY_SECRETS_ENV_FILE]: file, RELAY_BOT_TOKEN_AKI: 'plaintext' };
    expect(() => loadRelaySecrets(env)).toThrow('RELAY_BOT_TOKEN_AKI is also set in the service environment');
    expect(env['RELAY_INTERNAL_TOKEN']).toBeUndefined();
  });
});

describe('child environments', () => {
  const saved = { ...process.env };
  afterEach(() => {
    for (const key of Object.keys(process.env)) if (!(key in saved)) delete process.env[key];
    Object.assign(process.env, saved);
  });

  it('strips file keys, bot tokens and the internal token; helpers may keep the token', () => {
    writeSecrets();
    const env: NodeJS.ProcessEnv = { [RELAY_SECRETS_ENV_FILE]: file, TELEGRAM_BOT_TOKEN: 't', BOT_TOKEN_X: 'b', KEEP: 'k' };
    loadRelaySecrets(env);
    expect(stripRelaySecrets({ ...env })).toEqual({ KEEP: 'k' });
    expect(stripRelaySecrets({ ...env }, { keepInternalToken: true })).toEqual({ KEEP: 'k', RELAY_INTERNAL_TOKEN: 'fixture-internal' });
  });

  it('strips legacy plaintext bot tokens even without a secrets file', () => {
    expect(stripRelaySecrets({ RELAY_BOT_TOKEN_PULSE: 'x', RELAY_INTERNAL_TOKEN: 'y', HOME: '/h' })).toEqual({ HOME: '/h' });
  });

  it('keeps Relay credentials out of CC SDK, Codex and helper children', () => {
    writeSecrets();
    // A test shell started by Relay may itself carry legacy plaintext copies.
    for (const key of Object.keys(process.env)) {
      if (/^(RELAY_BOT_TOKEN_|RELAY_INTERNAL_TOKEN$|OPENROUTER_API_KEY$)/.test(key)) delete process.env[key];
    }
    process.env[RELAY_SECRETS_ENV_FILE] = file;
    loadRelaySecrets(process.env);
    for (const env of [buildSdkEngineEnv(), buildCodexEnv(), relayChildEnv()]) {
      expect(Object.keys(env).filter(key => /RELAY_BOT_TOKEN_|RELAY_INTERNAL_TOKEN|OPENROUTER_API_KEY|RELAY_SECRETS_ENV_FILE/.test(key))).toEqual([]);
    }
    expect(buildCodexEnv({ baseUrl: 'https://route', apiKey: 'explicit' })['OPENAI_API_KEY']).toBe('explicit');
    // Relay itself still resolves its routes from its own environment.
    expect(process.env['OPENROUTER_API_KEY']).toBe('fixture-openrouter');
  });

  it('strips provider key pools and service credentials without a secrets file (systemd EnvironmentFile)', () => {
    // TCloud: systemd injects the credentials directly; RELAY_SECRETS_ENV_FILE is never declared.
    delete process.env[RELAY_SECRETS_ENV_FILE];
    Object.assign(process.env, {
      OPENROUTER_API_KEY: 'fixture-openrouter',
      OPENROUTER_ANTHROPIC_AUTH_TOKEN: 'fixture-anthropic-route',
      OPENROUTER_ANTHROPIC_BASE_URL: 'https://openrouter.example/api',
      RELAY_INTERNAL_TOKEN: 'fixture-internal',
      RELAY_BOT_TOKEN_PULSE: '1:fixture-bot',
    });
    expect(loadRelaySecrets(process.env)).toBeUndefined();
    const leaked = (env: Record<string, string | undefined>) =>
      Object.keys(env).filter(key => /^(OPENROUTER_.*(API_KEY|TOKEN)|RELAY_BOT_TOKEN_.*|RELAY_INTERNAL_TOKEN)$/.test(key));
    for (const env of [buildSdkEngineEnv(), buildCodexEnv(), relayChildEnv()]) {
      expect(leaked(env)).toEqual([]);
      expect(env['OPENROUTER_ANTHROPIC_BASE_URL']).toBe('https://openrouter.example/api');
    }
    // Relay-owned helpers (scheduler gate/verify, native core) keep only the loopback token.
    expect(leaked(relayChildEnv({ keepInternalToken: true }))).toEqual(['RELAY_INTERNAL_TOKEN']);
    // The selected route is injected explicitly by the caller.
    expect(buildCodexEnv({ baseUrl: 'https://route', apiKey: 'explicit' })['OPENAI_API_KEY']).toBe('explicit');
    expect(process.env['OPENROUTER_ANTHROPIC_AUTH_TOKEN']).toBe('fixture-anthropic-route');
  });

  it('marks Relay-internal names that no allowlist may forward', () => {
    expect(['RELAY_INTERNAL_TOKEN', 'RELAY_BOT_TOKEN_AKI', 'TELEGRAM_BOT_TOKEN', RELAY_SECRETS_ENV_FILE].every(isRelayInternalSecret)).toBe(true);
    expect(isRelayInternalSecret('OPENROUTER_API_KEY')).toBe(false);
  });
});

describe('Relay CLI startup', () => {
  async function start(env: NodeJS.ProcessEnv): Promise<{ code: number | null; stderr: string; home: string }> {
    const home = join(root, 'home');
    mkdirSync(home, { recursive: true });
    const cli = spawn(process.execPath, [join(process.cwd(), 'dist', 'cli.js')], {
      env: { PATH: process.env['PATH'], HOME: home, ...env },
      stdio: ['ignore', 'ignore', 'pipe'],
    });
    let stderr = '';
    cli.stderr!.on('data', (chunk: Buffer) => { stderr += chunk.toString(); });
    const timer = setTimeout(() => cli.kill('SIGKILL'), 8_000);
    const [code] = await once(cli, 'close') as [number | null];
    clearTimeout(timer);
    return { code, stderr, home };
  }

  it.each([
    ['missing file', () => undefined, 'is missing'],
    ['world-readable file', () => writeSecrets(FIXTURE, 0o644), 'accessible to other users'],
    ['unterminated quote', () => writeSecrets("RELAY_INTERNAL_TOKEN='fixture-unterminated\n"), 'unterminated quote'],
    ['text after a closing quote', () => writeSecrets('OPENROUTER_API_KEY="fixture-valid" garbage\n'), 'text after the closing quote'],
  ])('refuses to start with a %s before touching lifecycle state', async (_name, prepare, reason) => {
    prepare();
    const { code, stderr, home } = await start({ [RELAY_SECRETS_ENV_FILE]: file });
    expect(code).toBe(78);
    expect(stderr).toContain('Relay refused to start');
    expect(stderr).toContain(reason);
    expect(stderr).not.toContain('fixture-');
    expect(existsSync(join(home, '.relay', 'relay.pid'))).toBe(false);
    expect(existsSync(join(home, '.relay', 'restart-audit.log'))).toBe(false);
  });
});
