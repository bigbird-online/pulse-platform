import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { EventEmitter } from 'node:events';
import { PassThrough } from 'node:stream';
import { chmodSync, existsSync, mkdirSync, mkdtempSync, readdirSync, realpathSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { dirname, join, resolve } from 'node:path';
import {
  CC_SESSION_WI_SERVER, CC_SESSION_WI_TOOL, resolveCcSessionWiMcp, type CcSessionWiMcp,
} from '../src/cc-session-wi-mcp.js';
import { createPool } from '../src/pool.js';
import { CC_BASE_PROMPT, CC_GIT_RULES, type CcPromptOptions } from '../src/cc-session-settings.js';
import { canonicalPython } from './helpers/canonical-python.js';

const { spawnMock } = vi.hoisted(() => ({ spawnMock: vi.fn() }));
vi.mock('node:child_process', async (original) => ({
  ...await original<typeof import('node:child_process')>(),
  spawn: spawnMock,
}));

const PYTHON = canonicalPython;

describe('session WI MCP resolution', () => {
  it('checks the rule and tool of the actual CC-mode server', () => {
    const resolved = resolveCcSessionWiMcp({ python: PYTHON, pythonPath: resolve('../ComputeCost') });
    expect(resolved?.server).toEqual({ type: 'stdio', command: PYTHON,
      args: [resolve('../ComputeCost/runtime/codex-mcp-request-purpose.py'), '--host', 'cc'] });
  });

  it('skips with a warning when host resources are absent or the contract differs', () => {
    const warn = vi.spyOn(console, 'warn').mockImplementation(() => {});
    expect(resolveCcSessionWiMcp(undefined)).toBeUndefined();
    expect(resolveCcSessionWiMcp({ python: PYTHON, pythonPath: '/absent' })).toBeUndefined();
    const wrong = () => ({ status: 0, stdout: '{"id":1,"result":{"instructions":"x"}}\n{"id":2,"result":{"tools":[]}}\n' });
    expect(resolveCcSessionWiMcp({ python: PYTHON, pythonPath: resolve('../ComputeCost') }, wrong)).toBeUndefined();
    expect(warn).toHaveBeenCalledTimes(2);
    warn.mockRestore();
  });
});

function child() {
  const value = Object.assign(new EventEmitter(), {
    stdin: new PassThrough(), stdout: new PassThrough(), stderr: new PassThrough(),
    pid: 1234567, killed: false,
    kill: vi.fn((signal?: string) => { value.killed = true; value.emit('exit', 0, signal); return true; }),
  });
  return value;
}

describe('CC spawn registers the session WI MCP', () => {
  const sessionWiMcp: CcSessionWiMcp = {
    server: { type: 'stdio', command: '/reviewed/python', args: ['/release/ComputeCost/runtime/codex-mcp-request-purpose.py', '--host', 'cc'] },
  };
  let home: string;
  let pool: ReturnType<typeof createPool> | undefined;
  beforeEach(() => {
    home = mkdtempSync(join(tmpdir(), 'cc-session-wi-'));
    vi.stubEnv('HOME', home);
    vi.stubEnv('PULSE_COMPUTECOST_RECEIPT_DIR', '');
    spawnMock.mockReset();
    spawnMock.mockImplementation(() => child());
  });
  afterEach(async () => {
    await pool?.killAll();
    pool?.dispose();
    vi.unstubAllEnvs();
    vi.restoreAllMocks();
    try { chmodSync(join(home, 'cc-prompt-sessions'), 0o700); } catch { /* absent or a file */ }
    rmSync(home, { recursive: true, force: true });
  });

  async function spawnArgs(mode: 'print' | 'interactive', mcp?: CcSessionWiMcp, ccPrompt?: CcPromptOptions,
    sessionId = 'cc-session'): Promise<string[]> {
    await pool?.killAll();
    pool?.dispose();
    pool = createPool({ cleanupIntervalMs: 999999, ccMode: mode, sessionWiMcp: mcp, ccPromptRecordDir: join(home, 'cc-prompt-sessions') });
    const pending = pool.spawn({ tenantId: 'fixture', sessionId, workspace: home, ccMode: mode, ccPrompt });
    spawnMock.mock.results.at(-1)!.value.stdout.write('{"type":"system","subtype":"init","content":"ready"}\n');
    await pending;
    return spawnMock.mock.calls.at(-1)![1] as string[];
  }

  it.each(['print', 'interactive'] as const)('adds server and allowed tool for %s CC, the rule only as MCP instructions', async (mode) => {
    const args = await spawnArgs(mode, sessionWiMcp);
    const config = JSON.parse(args[args.indexOf('--mcp-config') + 1]);
    expect(Object.keys(config.mcpServers)).toEqual([CC_SESSION_WI_SERVER]);
    expect(config.mcpServers[CC_SESSION_WI_SERVER]).toEqual(sessionWiMcp.server);
    expect(args[args.indexOf('--allowed-tools') + 1].split(',')).toContain(CC_SESSION_WI_TOOL);
    // CC shows the server's instructions itself; Relay's resident prompt does not repeat them.
    expect(args[args.indexOf('--append-system-prompt') + 1]).not.toContain('declare_session_wi');
  });

  it.each(['print', 'interactive'] as const)('trims CC sections a Relay conversation cannot use for %s CC', async (mode) => {
    const args = await spawnArgs(mode, sessionWiMcp);
    expect(args).toContain('--exclude-dynamic-system-prompt-sections');
    const settings = JSON.parse(args[args.lastIndexOf('--settings') + 1]);
    for (const name of ['dataviz', 'claude-api', 'keybindings-help', 'loop', 'update-config']) {
      expect(settings.skillOverrides[name]).toBe('off');
    }
    expect(settings.skillOverrides['code-review']).toBeUndefined();
    expect(settings.enabledPlugins).toEqual({ 'telegram@claude-plugins-official': false });
  });

  it.each(['print', 'interactive'] as const)('keeps CC\'s default prompt and git instructions unless the Agent opts out, %s CC', async (mode) => {
    const args = await spawnArgs(mode, sessionWiMcp);
    expect(args).toContain('--append-system-prompt');
    expect(args).not.toContain('--system-prompt');
    expect(args[args.indexOf('--append-system-prompt') + 1]).not.toContain(CC_GIT_RULES);
    expect(JSON.parse(args[args.lastIndexOf('--settings') + 1])).not.toHaveProperty('includeGitInstructions');
  });

  it.each(['print', 'interactive'] as const)('replaces CC\'s default prompt with Relay\'s base prompt for %s CC', async (mode) => {
    const args = await spawnArgs(mode, sessionWiMcp, { systemPrompt: 'replace' });
    expect(args).not.toContain('--append-system-prompt');
    // CC ignores the dynamic-section flag under --system-prompt, so Relay leaves it out.
    expect(args).not.toContain('--exclude-dynamic-system-prompt-sections');
    const prompt = args[args.indexOf('--system-prompt') + 1];
    expect(prompt.startsWith(CC_BASE_PROMPT)).toBe(true);
    expect(prompt).toContain('## File Delivery');
    expect(prompt).toContain('user\'s auto-memory');
    expect(JSON.parse(args[args.lastIndexOf('--settings') + 1]).skillOverrides.dataviz).toBe('off');
  });

  it.each(['print', 'interactive'] as const)('drops the git snapshot and keeps Relay\'s Git rules for %s CC', async (mode) => {
    const args = await spawnArgs(mode, sessionWiMcp, { gitStatus: false });
    expect(JSON.parse(args[args.lastIndexOf('--settings') + 1]).includeGitInstructions).toBe(false);
    expect(args[args.indexOf('--append-system-prompt') + 1].endsWith(CC_GIT_RULES)).toBe(true);
    expect(args).toContain('--exclude-dynamic-system-prompt-sections');
  });

  /** A transcript on disk: the next spawn of this session resumes it. */
  function transcript(sessionId: string): void {
    const file = join(home, '.claude', 'projects', realpathSync(home).replace(/[^A-Za-z0-9_-]/g, '-'), `${sessionId}.jsonl`);
    mkdirSync(dirname(file), { recursive: true });
    writeFileSync(file, JSON.stringify({ type: 'user', uuid: 'u1', parentUuid: null, sessionId,
      message: { role: 'user', content: 'x' }, timestamp: new Date().toISOString() }) + '\n');
  }

  function promptMode(args: string[]) {
    const settings = JSON.parse(args[args.lastIndexOf('--settings') + 1]);
    const prompt = args[args.indexOf(args.includes('--system-prompt') ? '--system-prompt' : '--append-system-prompt') + 1];
    return { replace: args.includes('--system-prompt'), gitInstructions: settings.includeGitInstructions !== false,
      gitRules: prompt.includes(CC_GIT_RULES) };
  }

  it.each(['print', 'interactive'] as const)('keeps a session begun on the default there when the Agent first opts in, %s CC', async (mode) => {
    transcript('old-session');
    const args = await spawnArgs(mode, sessionWiMcp, { systemPrompt: 'replace', gitStatus: false }, 'old-session');
    expect(args).toContain('--resume');
    expect(promptMode(args)).toEqual({ replace: false, gitInstructions: true, gitRules: false });
  });

  it.each(['print', 'interactive'] as const)('resumes a session with the ccPrompt it began with, %s CC', async (mode) => {
    await spawnArgs(mode, sessionWiMcp, { gitStatus: false }, 'trimmed-session');
    transcript('trimmed-session');
    const kept = await spawnArgs(mode, sessionWiMcp, { gitStatus: false }, 'trimmed-session');
    expect(kept).toContain('--resume');
    expect(promptMode(kept)).toEqual({ replace: false, gitInstructions: false, gitRules: true });
    // The Agent turned it off meanwhile: the resumed session still keeps Bash's Git section and Relay's rules apart.
    const reverted = await spawnArgs(mode, sessionWiMcp, undefined, 'trimmed-session');
    expect(promptMode(reverted)).toEqual({ replace: false, gitInstructions: false, gitRules: true });
  });

  it.each(['print', 'interactive'] as const)('starts a new session with the Agent\'s current ccPrompt, %s CC', async (mode) => {
    await spawnArgs(mode, sessionWiMcp, { systemPrompt: 'replace', gitStatus: false }, 'first-session');
    transcript('first-session');
    const args = await spawnArgs(mode, sessionWiMcp, undefined, 'next-session');
    expect(args).toContain('--session-id');
    expect(promptMode(args)).toEqual({ replace: false, gitInstructions: true, gitRules: false });
    const opted = await spawnArgs(mode, sessionWiMcp, { systemPrompt: 'replace' }, 'third-session');
    expect(promptMode(opted)).toEqual({ replace: true, gitInstructions: true, gitRules: false });
  });

  /** ccPrompt warnings the pool logged. */
  function ccPromptWarnings(warn: ReturnType<typeof vi.spyOn>): string[] {
    return warn.mock.calls.map(([entry]) => String((entry as { msg?: unknown })?.msg ?? ''))
      .filter((msg) => msg.startsWith('ccPrompt'));
  }
  const DEFAULT = { replace: false, gitInstructions: true, gitRules: false };
  const records = () => join(home, 'cc-prompt-sessions');

  it.each(['print', 'interactive'] as const)('writes no record for a session on the default, %s CC', async (mode) => {
    const warn = vi.spyOn(console, 'warn');
    const args = await spawnArgs(mode, sessionWiMcp, undefined, 'plain-session');
    expect(promptMode(args)).toEqual(DEFAULT);
    expect(existsSync(records())).toBe(false);
    await spawnArgs(mode, sessionWiMcp, { systemPrompt: 'append', gitStatus: true }, 'explicit-default');
    expect(existsSync(records())).toBe(false);
    expect(ccPromptWarnings(warn)).toEqual([]);
  });

  it.each(['print', 'interactive'] as const)('starts on the default when a file occupies the record directory, %s CC', async (mode) => {
    writeFileSync(records(), 'not a directory');
    const warn = vi.spyOn(console, 'warn');
    const args = await spawnArgs(mode, sessionWiMcp, { systemPrompt: 'replace', gitStatus: false }, 'blocked-session');
    expect(promptMode(args)).toEqual(DEFAULT);
    expect(ccPromptWarnings(warn)).toEqual(['ccPrompt record not written; new CC session starts on the default prompt']);
  });

  it.each(['print', 'interactive'] as const)('starts on the default when the record directory is not writable, %s CC', async (mode) => {
    mkdirSync(records());
    chmodSync(records(), 0o500);
    const warn = vi.spyOn(console, 'warn');
    const args = await spawnArgs(mode, sessionWiMcp, { gitStatus: false }, 'readonly-session');
    expect(promptMode(args)).toEqual(DEFAULT);
    expect(ccPromptWarnings(warn)).toEqual(['ccPrompt record not written; new CC session starts on the default prompt']);
    expect(readdirSync(records())).toEqual([]);
  });

  it.each(['print', 'interactive'] as const)('resumes on the default with a warning when the record is corrupt, %s CC', async (mode) => {
    transcript('corrupt-session');
    mkdirSync(records());
    writeFileSync(join(records(), 'corrupt-session.json'), '{"ccPrompt": {"gitStatus": "no"}}');
    writeFileSync(join(records(), 'garbled-session.json'), '{not json');
    const warn = vi.spyOn(console, 'warn');
    const args = await spawnArgs(mode, sessionWiMcp, { gitStatus: false }, 'corrupt-session');
    expect(args).toContain('--resume');
    expect(promptMode(args)).toEqual(DEFAULT);
    transcript('garbled-session');
    expect(promptMode(await spawnArgs(mode, sessionWiMcp, { gitStatus: false }, 'garbled-session'))).toEqual(DEFAULT);
    writeFileSync(join(records(), 'locked-session.json'), '{"ccPrompt":{"gitStatus":false}}');
    chmodSync(join(records(), 'locked-session.json'), 0o000);
    transcript('locked-session');
    expect(promptMode(await spawnArgs(mode, sessionWiMcp, { gitStatus: false }, 'locked-session'))).toEqual(DEFAULT);
    expect(ccPromptWarnings(warn)).toEqual([
      'ccPrompt record invalid; resumed CC session keeps CC\'s default Git section and settings',
      'ccPrompt record invalid; resumed CC session keeps CC\'s default Git section and settings',
      'ccPrompt record unreadable; resumed CC session keeps CC\'s default Git section and settings',
    ]);
  });

  it.each(['print', 'interactive'] as const)('resumes on the default without a warning when the record is missing, %s CC', async (mode) => {
    transcript('unrecorded-session');
    const warn = vi.spyOn(console, 'warn');
    const args = await spawnArgs(mode, sessionWiMcp, { systemPrompt: 'replace', gitStatus: false }, 'unrecorded-session');
    expect(args).toContain('--resume');
    expect(promptMode(args)).toEqual(DEFAULT);
    expect(ccPromptWarnings(warn)).toEqual([]);
  });

  it('leaves spawn unchanged when the MCP is unavailable', async () => {
    const args = await spawnArgs('print');
    expect(args).not.toContain('--mcp-config');
    expect(args[args.indexOf('--allowed-tools') + 1]).not.toContain(CC_SESSION_WI_TOOL);
    expect(args[args.indexOf('--append-system-prompt') + 1]).not.toContain('declare_session_wi');
  });
});

// Pool behavior is isolated from OS process ownership, tested with real processes separately.
vi.mock('../src/owned-process.js', async () => ({
  spawnOwnedProcess: (await import('node:child_process')).spawn,
}));
