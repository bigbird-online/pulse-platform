import { describe, it, expect, beforeEach, afterEach } from 'vitest';
import { mkdtempSync, rmSync, statSync, readFileSync, writeFileSync, mkdirSync, existsSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import {
  ensureClaudeOnboardingComplete,
  ensureClaudeProjectRegistered,
  ensureClaudeBareRootTrust,
  ensureClaudeJsonWritable,
  mirrorAnthropicAuthTokenToApiKey,
  recordCustomApiKeyAccepted,
  customApiKeySuffixVariants,
  CLAUDE_BARE_REGISTRY_CWD,
  CLAUDE_ONBOARDING_DEFAULTS,
  CLAUDE_PROJECT_DEFAULTS,
} from '../src/claude-onboarding.js';

describe('ensureClaudeOnboardingComplete', () => {
  let tmpHome: string;
  let claudeJsonPath: string;

  beforeEach(() => {
    tmpHome = mkdtempSync(join(tmpdir(), 'relay-onboard-'));
    claudeJsonPath = join(tmpHome, '.claude.json');
  });

  afterEach(() => {
    rmSync(tmpHome, { recursive: true, force: true });
  });

  it('creates ~/.claude.json with onboarding defaults when file is missing', () => {
    expect(existsSync(claudeJsonPath)).toBe(false);

    const result = ensureClaudeOnboardingComplete(tmpHome);

    expect(result.changed).toBe(true);
    expect(result.path).toBe(claudeJsonPath);
    const written = JSON.parse(readFileSync(claudeJsonPath, 'utf-8'));
    for (const [key, expected] of Object.entries(CLAUDE_ONBOARDING_DEFAULTS)) {
      expect(written[key]).toEqual(expected);
    }
    const mode = statSync(claudeJsonPath).mode & 0o777;
    expect(mode).toBe(0o600);
  });

  it('fills only null/undefined fields; preserves existing user values', () => {
    writeFileSync(
      claudeJsonPath,
      JSON.stringify({
        theme: 'light',
        hasCompletedOnboarding: null,
        userID: 'preserve-me',
        cachedGrowthBookFeatures: { foo: 1 },
      }),
      { mode: 0o600 },
    );

    const result = ensureClaudeOnboardingComplete(tmpHome);

    expect(result.changed).toBe(true);
    const written = JSON.parse(readFileSync(claudeJsonPath, 'utf-8'));
    expect(written.theme).toBe('light');
    expect(written.hasCompletedOnboarding).toBe(true);
    expect(written.userID).toBe('preserve-me');
    expect(written.cachedGrowthBookFeatures).toEqual({ foo: 1 });
    expect(written.tipsHistory).toEqual({});
  });

  it('is a no-op when all onboarding fields already valid', () => {
    const initial = {
      ...CLAUDE_ONBOARDING_DEFAULTS,
      theme: 'dark',
      userID: 'abc',
    };
    writeFileSync(claudeJsonPath, JSON.stringify(initial), { mode: 0o600 });
    const before = statSync(claudeJsonPath).mtimeMs;

    const result = ensureClaudeOnboardingComplete(tmpHome);

    expect(result.changed).toBe(false);
    const after = statSync(claudeJsonPath).mtimeMs;
    expect(after).toBe(before);
  });

  it('does not throw when ~/.claude.json is corrupted JSON; leaves file untouched', () => {
    writeFileSync(claudeJsonPath, '{ this is not json', { mode: 0o600 });
    const beforeBytes = readFileSync(claudeJsonPath, 'utf-8');

    const result = ensureClaudeOnboardingComplete(tmpHome);

    expect(result.changed).toBe(false);
    expect(result.error).toBeDefined();
    expect(readFileSync(claudeJsonPath, 'utf-8')).toBe(beforeBytes);
  });

  it('returns gracefully when home is empty/undefined', () => {
    expect(() => ensureClaudeOnboardingComplete('')).not.toThrow();
    expect(ensureClaudeOnboardingComplete('').changed).toBe(false);
  });

  it('creates parent dir when only ~/.claude/ exists but ~/.claude.json missing', () => {
    mkdirSync(join(tmpHome, '.claude'), { recursive: true });
    const result = ensureClaudeOnboardingComplete(tmpHome);
    expect(result.changed).toBe(true);
    expect(existsSync(claudeJsonPath)).toBe(true);
  });
});

describe('mirrorAnthropicAuthTokenToApiKey', () => {
  it('strips ANTHROPIC_API_KEY so API providers use AUTH_TOKEN only (no CC custom-key TUI)', () => {
    const env: NodeJS.ProcessEnv = {
      ANTHROPIC_AUTH_TOKEN: 'tok-123',
      ANTHROPIC_API_KEY: 'tok-should-strip',
    };
    mirrorAnthropicAuthTokenToApiKey(env);
    expect(env['ANTHROPIC_AUTH_TOKEN']).toBe('tok-123');
    expect(env['ANTHROPIC_API_KEY']).toBeUndefined();
  });

  it('leaves env unchanged when API_KEY already absent', () => {
    const env: NodeJS.ProcessEnv = { ANTHROPIC_AUTH_TOKEN: 'tok-123' };
    mirrorAnthropicAuthTokenToApiKey(env);
    expect(env['ANTHROPIC_AUTH_TOKEN']).toBe('tok-123');
    expect(env['ANTHROPIC_API_KEY']).toBeUndefined();
  });
});

describe('customApiKeySuffixVariants', () => {
  it('returns full key, last 20 chars, and segment after last dot', () => {
    const key = '0123456789abcdef0123456789abcdef.FIXTUREfixture00';
    expect(customApiKeySuffixVariants(key)).toEqual([
      key,
      'def.FIXTUREfixture00',
      'FIXTUREfixture00',
    ]);
  });
});

describe('recordCustomApiKeyAccepted', () => {
  let tmpHome: string;
  let claudeJsonPath: string;
  const apiKey = '0123456789abcdef0123456789abcdef.FIXTUREfixture00';

  beforeEach(() => {
    tmpHome = mkdtempSync(join(tmpdir(), 'relay-apikey-'));
    claudeJsonPath = join(tmpHome, '.claude.json');
  });

  afterEach(() => {
    rmSync(tmpHome, { recursive: true, force: true });
  });

  it('records all suffix variants and removes matching rejected entries', () => {
    writeFileSync(
      claudeJsonPath,
      JSON.stringify({
        customApiKeyResponses: {
          rejected: ['def.FIXTUREfixture00', 'other'],
          accepted: [],
        },
      }),
      { mode: 0o600 },
    );

    recordCustomApiKeyAccepted(tmpHome, apiKey);

    const written = JSON.parse(readFileSync(claudeJsonPath, 'utf-8'));
    expect(written.customApiKeyResponses.rejected).toEqual(['other']);
    expect(written.customApiKeyResponses.accepted).toEqual(
      expect.arrayContaining(customApiKeySuffixVariants(apiKey)),
    );
  });

  it('is a no-op when all variants already accepted and not rejected', () => {
    writeFileSync(
      claudeJsonPath,
      JSON.stringify({
        customApiKeyResponses: {
          rejected: [],
          accepted: customApiKeySuffixVariants(apiKey),
        },
      }),
      { mode: 0o600 },
    );
    const before = readFileSync(claudeJsonPath, 'utf-8');

    recordCustomApiKeyAccepted(tmpHome, apiKey);

    expect(readFileSync(claudeJsonPath, 'utf-8')).toBe(before);
  });
});

// bug-20260510-001: CC --bare mode never opens the trust dialog. Without an
// entry in ~/.claude.json#projects[<cwd>] the CC client refuses to load that
// directory's CLAUDE.md, and tenant Agent identity is silently lost. Relay
// must idempotently register the spawn workspace as a trusted project before
// every spawn. Behavior must mirror ensureClaudeOnboardingComplete: only fill
// missing fields, never overwrite user-set values, never throw.
describe('ensureClaudeProjectRegistered', () => {
  let tmpHome: string;
  let claudeJsonPath: string;
  const cwd = '/opt/PULSE/Tenants/member-0002';

  beforeEach(() => {
    tmpHome = mkdtempSync(join(tmpdir(), 'relay-project-trust-'));
    claudeJsonPath = join(tmpHome, '.claude.json');
  });

  afterEach(() => {
    rmSync(tmpHome, { recursive: true, force: true });
  });

  it('creates ~/.claude.json with projects[cwd] entry when file is missing', () => {
    expect(existsSync(claudeJsonPath)).toBe(false);

    const result = ensureClaudeProjectRegistered(tmpHome, cwd);

    expect(result.changed).toBe(true);
    expect(result.path).toBe(claudeJsonPath);
    const written = JSON.parse(readFileSync(claudeJsonPath, 'utf-8'));
    expect(written.projects).toBeDefined();
    expect(written.projects[cwd]).toBeDefined();
    for (const [key, expected] of Object.entries(CLAUDE_PROJECT_DEFAULTS)) {
      expect(written.projects[cwd][key]).toEqual(expected);
    }
    expect(written.projects[cwd].hasTrustDialogAccepted).toBe(true);
  });

  it('adds projects[cwd] without touching existing projects entries', () => {
    writeFileSync(
      claudeJsonPath,
      JSON.stringify({
        userID: 'preserve-me',
        projects: {
          '/opt/pulse': {
            hasTrustDialogAccepted: true,
            allowedTools: ['Read'],
            lastCost: 0.0123,
          },
        },
      }),
      { mode: 0o600 },
    );

    const result = ensureClaudeProjectRegistered(tmpHome, cwd);

    expect(result.changed).toBe(true);
    const written = JSON.parse(readFileSync(claudeJsonPath, 'utf-8'));
    expect(written.userID).toBe('preserve-me');
    expect(written.projects['/opt/pulse'].hasTrustDialogAccepted).toBe(true);
    expect(written.projects['/opt/pulse'].allowedTools).toEqual(['Read']);
    expect(written.projects['/opt/pulse'].lastCost).toBe(0.0123);
    expect(written.projects[cwd].hasTrustDialogAccepted).toBe(true);
    expect(written.projects[cwd].hasCompletedProjectOnboarding).toBe(true);
  });

  it('fills missing fields when entry exists but is partial', () => {
    writeFileSync(
      claudeJsonPath,
      JSON.stringify({
        projects: {
          [cwd]: {
            allowedTools: ['Bash'],
            lastSessionId: 'existing-uuid',
          },
        },
      }),
      { mode: 0o600 },
    );

    const result = ensureClaudeProjectRegistered(tmpHome, cwd);

    expect(result.changed).toBe(true);
    const written = JSON.parse(readFileSync(claudeJsonPath, 'utf-8'));
    expect(written.projects[cwd].allowedTools).toEqual(['Bash']);
    expect(written.projects[cwd].lastSessionId).toBe('existing-uuid');
    expect(written.projects[cwd].hasTrustDialogAccepted).toBe(true);
    expect(written.projects[cwd].hasCompletedProjectOnboarding).toBe(true);
    expect(written.projects[cwd].mcpServers).toEqual({});
  });

  it('does NOT overwrite hasTrustDialogAccepted=false (user-explicit reject)', () => {
    writeFileSync(
      claudeJsonPath,
      JSON.stringify({
        projects: {
          [cwd]: { hasTrustDialogAccepted: false },
        },
      }),
      { mode: 0o600 },
    );

    ensureClaudeProjectRegistered(tmpHome, cwd);

    const written = JSON.parse(readFileSync(claudeJsonPath, 'utf-8'));
    expect(written.projects[cwd].hasTrustDialogAccepted).toBe(false);
  });

  it('is a no-op when projects[cwd] entry is fully populated', () => {
    const fullEntry = { ...CLAUDE_PROJECT_DEFAULTS };
    writeFileSync(
      claudeJsonPath,
      JSON.stringify({ projects: { [cwd]: fullEntry } }),
      { mode: 0o600 },
    );
    const before = statSync(claudeJsonPath).mtimeMs;

    const result = ensureClaudeProjectRegistered(tmpHome, cwd);

    expect(result.changed).toBe(false);
    expect(statSync(claudeJsonPath).mtimeMs).toBe(before);
  });

  it('does not throw when ~/.claude.json is corrupted JSON', () => {
    writeFileSync(claudeJsonPath, '{ corrupted', { mode: 0o600 });
    const beforeBytes = readFileSync(claudeJsonPath, 'utf-8');

    const result = ensureClaudeProjectRegistered(tmpHome, cwd);

    expect(result.changed).toBe(false);
    expect(result.error).toBeDefined();
    expect(readFileSync(claudeJsonPath, 'utf-8')).toBe(beforeBytes);
  });

  it('returns gracefully when home or cwd is empty/undefined', () => {
    expect(() => ensureClaudeProjectRegistered('', cwd)).not.toThrow();
    expect(ensureClaudeProjectRegistered('', cwd).changed).toBe(false);
    expect(() => ensureClaudeProjectRegistered(tmpHome, '')).not.toThrow();
    expect(ensureClaudeProjectRegistered(tmpHome, '').changed).toBe(false);
    expect(() => ensureClaudeProjectRegistered(undefined, undefined)).not.toThrow();
  });
});

describe('ensureClaudeBareRootTrust', () => {
  let tmpHome: string;
  let claudeJsonPath: string;

  beforeEach(() => {
    tmpHome = mkdtempSync(join(tmpdir(), 'relay-bare-root-trust-'));
    claudeJsonPath = join(tmpHome, '.claude.json');
  });

  afterEach(() => {
    rmSync(tmpHome, { recursive: true, force: true });
  });

  it('registers projects[/root] for CC --bare interactive trust gate', () => {
    const result = ensureClaudeBareRootTrust(tmpHome);

    expect(result.changed).toBe(true);
    const written = JSON.parse(readFileSync(claudeJsonPath, 'utf-8'));
    expect(written.projects[CLAUDE_BARE_REGISTRY_CWD].hasTrustDialogAccepted).toBe(true);
  });
});

describe('ensureClaudeJsonWritable', () => {
  it('does not throw when file is missing or home is unset', () => {
    expect(() => ensureClaudeJsonWritable(undefined)).not.toThrow();
    expect(() => ensureClaudeJsonWritable('/nonexistent/home')).not.toThrow();
  });
});
