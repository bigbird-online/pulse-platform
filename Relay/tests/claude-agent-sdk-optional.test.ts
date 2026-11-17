// @anthropic-ai/claude-agent-sdk is an optionalDependency: only cc-sdk loads it, and a missing
// package must fail that runtime with a clear message instead of breaking Relay or codex.
import { readFileSync } from 'node:fs';
import { describe, expect, it } from 'vitest';
import { CLAUDE_AGENT_SDK_PACKAGE, loadClaudeAgentSdkQuery } from '../src/cc-sdk-conversation-runtime.js';

const pkg = JSON.parse(readFileSync(new URL('../package.json', import.meta.url), 'utf8')) as {
  dependencies: Record<string, string>; optionalDependencies?: Record<string, string>;
};

describe('optional Claude Agent SDK', () => {
  it('is declared only as an optional dependency at the pinned version', () => {
    expect(pkg.dependencies[CLAUDE_AGENT_SDK_PACKAGE]).toBeUndefined();
    expect(pkg.optionalDependencies?.[CLAUDE_AGENT_SDK_PACKAGE]).toBe('0.3.261');
  });

  it('loads query() from the installed package (production nodes keep it)', async () => {
    expect(typeof await loadClaudeAgentSdkQuery()).toBe('function');
  });

  it('reports a missing package with the install hint and the codex alternative', async () => {
    const missing = Object.assign(new Error("Cannot find package '@anthropic-ai/claude-agent-sdk'"), { code: 'ERR_MODULE_NOT_FOUND' });
    await expect(loadClaudeAgentSdkQuery(async () => { throw missing; }))
      .rejects.toThrow(/cc-sdk runtime needs the optional package @anthropic-ai\/claude-agent-sdk.*npm ci.*codex runtime/);
  });

  it('keeps other load failures as they are', async () => {
    const broken = new Error('native engine crashed');
    await expect(loadClaudeAgentSdkQuery(async () => { throw broken; })).rejects.toBe(broken);
  });

  it('no Relay source imports the SDK statically', () => {
    for (const file of ['cc-sdk-conversation-runtime.ts', 'index.ts', 'conversation-runtime.ts']) {
      const text = readFileSync(new URL(`../src/${file}`, import.meta.url), 'utf8');
      expect(text).not.toMatch(/^import[^;]*from '@anthropic-ai\/claude-agent-sdk'/m);
    }
  });
});
