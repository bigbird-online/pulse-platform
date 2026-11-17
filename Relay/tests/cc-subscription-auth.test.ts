import { afterEach, describe, expect, it } from 'vitest';
import { mkdtempSync, mkdirSync, readFileSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { readCcSubscriptionToken, assertNoCcTokenCopies, ccSubscriptionSettings } from '../src/cc-subscription-auth.js';

const roots: string[] = [];
function temp(): string { const root = mkdtempSync(join(tmpdir(), 'cc-auth-source-')); roots.push(root); return root; }
afterEach(() => { for (const root of roots.splice(0)) rmSync(root, { recursive: true, force: true }); });

describe('CC subscription credential source', () => {
  it('reads only the declared token and adopts a file replacement without caching it', () => {
    const file = join(temp(), '.env');
    writeFileSync(file, 'OTHER_SECRET=do-not-inherit\nexport CLAUDE_CODE_OAUTH_TOKEN="sk-ant-oat01-first" # setup token\n');
    expect(readCcSubscriptionToken(file)).toBe('sk-ant-oat01-first');
    writeFileSync(file, "CLAUDE_CODE_OAUTH_TOKEN='sk-ant-oat01-second'\n");
    expect(readCcSubscriptionToken(file)).toBe('sk-ant-oat01-second');
  });
  it('fails closed without exposing invalid credential contents', () => {
    const root = temp(), file = join(root, '.env');
    for (const text of ['', '# CLAUDE_CODE_OAUTH_TOKEN=old', 'CLAUDE_CODE_OAUTH_TOKEN=bad secret value']) {
      writeFileSync(file, text);
      expect(() => readCcSubscriptionToken(file)).toThrow('CC subscription credential unavailable');
      try { readCcSubscriptionToken(file); } catch (error) { expect(String(error)).not.toContain('bad secret value'); }
    }
    expect(() => readCcSubscriptionToken(join(root, 'missing'))).toThrow('CC subscription credential unavailable');
    expect(() => readCcSubscriptionToken('relative.env')).toThrow('CC subscription credential unavailable');
  });
  it('rejects a legacy HOME token copy without rewriting preferences or conversation history', () => {
    const root = temp(); mkdirSync(join(root, '.claude'));
    const file = join(root, '.claude', 'settings.json');
    const before = JSON.stringify({ theme: 'dark', env: { CLAUDE_CODE_OAUTH_TOKEN: 'sk-ant-oat01-old' } });
    writeFileSync(file, before);
    expect(() => assertNoCcTokenCopies(root, root)).toThrow('Remove CC OAuth token copies');
    expect(readFileSync(file, 'utf8')).toBe(before);
    writeFileSync(file, JSON.stringify({ theme: 'dark', env: { ANTHROPIC_BASE_URL: 'https://gateway.example.test/anthropic' } }));
    expect(() => assertNoCcTokenCopies(root, root)).not.toThrow();
  });
  it('rejects project token copies as well as user settings', () => {
    const home = temp(), workspace = temp(); mkdirSync(join(workspace, '.claude'));
    writeFileSync(join(workspace, '.claude', 'settings.local.json'), JSON.stringify({ env: { CLAUDE_CODE_OAUTH_TOKEN_FALLBACK: 'sk-ant-oat01-old' } }));
    expect(() => assertNoCcTokenCopies(home, workspace)).toThrow('Remove CC OAuth token copies');
  });
  it('overrides routing without putting an OAuth token into settings or argv', () => {
    const settings = JSON.parse(ccSubscriptionSettings());
    expect(settings.env.ANTHROPIC_BASE_URL).toBe('https://api.anthropic.com');
    expect(settings.env.ANTHROPIC_AUTH_TOKEN).toBe('');
    expect(settings.env.ANTHROPIC_API_KEY).toBe('');
    expect(settings.env).not.toHaveProperty('CLAUDE_CODE_OAUTH_TOKEN');
    expect(settings.env).not.toHaveProperty('CLAUDE_CODE_OAUTH_TOKEN_FALLBACK');
  });
});
