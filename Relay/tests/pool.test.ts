import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { EventEmitter } from 'node:events';
import { PassThrough } from 'node:stream';
import { appendFileSync, existsSync, mkdirSync, mkdtempSync, readFileSync, realpathSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { dirname, join, resolve } from 'node:path';
import { createPool, validateSessionChain } from '../src/pool.js';
import { createConversationRuntimeManager, createPoolConversationAdapter } from '../src/conversation-runtime.js';
import { createMaintenanceBarrier } from '../src/maintenance-barrier.js';
import { persistQueuedTurns, replayPendingTurns } from '../src/shutdown-interrupts.js';
import type { PersistedSession } from '../src/types.js';
import { resolveToolFileOutboxPath } from '../src/tool-file-outbox.js';
import type { SpawnConfig, StreamJsonMessage } from '../src/types.js';

// --- Mock child_process ---

interface MockProcess extends EventEmitter {
  stdin: PassThrough;
  stdout: PassThrough;
  stderr: PassThrough;
  pid: number;
  kill: ReturnType<typeof vi.fn>;
  killed: boolean;
}

function createMockProcess(pid = 1234): MockProcess {
  const proc = new EventEmitter() as MockProcess;
  proc.stdin = new PassThrough();
  proc.stdout = new PassThrough();
  proc.stderr = new PassThrough();
  proc.pid = pid;
  proc.killed = false;
  proc.kill = vi.fn((signal?: string) => {
    proc.killed = true;
    proc.emit('exit', signal === 'SIGKILL' ? 137 : 0, signal ?? 'SIGTERM');
    return true;
  });
  return proc;
}

function getSessionFilePath(workspace: string, sessionId: string): string {
  const cwdEncoded = realpathSync(workspace).replace(/[^A-Za-z0-9_-]/g, '-');
  return join(process.env['HOME'] ?? '', '.claude', 'projects', cwdEncoded, `${sessionId}.jsonl`);
}

/** CC `--bare` always writes under ~/.claude/projects/-root/ (see pool.ts getBareSessionFilePath). */
function getBareSessionFilePath(sessionId: string, homeDir?: string): string {
  const home = homeDir ?? process.env['HOME'] ?? '';
  return join(home, '.claude', 'projects', '-root', `${sessionId}.jsonl`);
}

let mockSpawn: ReturnType<typeof vi.fn>;

vi.mock('node:child_process', () => ({
  spawn: (...args: unknown[]) => mockSpawn(...args),
}));

const baseConfig: SpawnConfig = {
  tenantId: 'member-000',
  workspace: '/opt/PULSE/Tenants/member-000/',
  sessionId: 'test-session-uuid',
};

/** Poll a predicate until it returns true or the deadline elapses. Avoids fixed-sleep timing races. */
async function waitFor(predicate: () => boolean, timeoutMs = 2000, intervalMs = 5): Promise<boolean> {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    if (predicate()) return true;
    await new Promise((resolve) => setTimeout(resolve, intervalMs));
  }
  return predicate();
}

/** Accumulate everything written to a mock process stdin without consuming the buffer destructively. */
function captureStdin(proc: MockProcess): { text: () => string } {
  let buf = '';
  proc.stdin.on('data', (chunk: Buffer | string) => { buf += chunk.toString(); });
  return { text: () => buf };
}

/** Build the expected meta-prompt that Relay sends through PTY for temp-file mode.
 *  If workspace exists on disk, temp file goes to {workspace}/.relay/prompts/prompt-{tenant}.txt;
 *  otherwise falls back to /tmp/relay-prompts-{runtime}/prompt-{tenant}.txt. */
function metaPromptFor(tenantId: string, workspace?: string): string {
  const runtimeId = typeof process.getuid === 'function' ? String(process.getuid()) : String(process.pid);
  const tempPath = workspace && existsSync(workspace)
    ? `${realpathSync(workspace)}/.relay/prompts/prompt-${tenantId}.txt`
    : join(tmpdir(), `relay-prompts-${runtimeId}`, `prompt-${tenantId}.txt`);
  return `[Relay] 用户的消息因终端输入长度限制保存在文件中，请用 Read 工具读取 ${tempPath}，其中的内容就是用户发送给你的消息。读取后用 Bash 执行 rm ${tempPath} 清理临时文件。`;
}

describe('pool declared subscription source', () => {
  let root: string;
  let pool: ReturnType<typeof createPool>;
  beforeEach(() => {
    root = mkdtempSync(join(tmpdir(), 'pool-subscription-source-'));
    mkdirSync(join(root, '.claude'));
    writeFileSync(join(root, '.claude', 'settings.json'), JSON.stringify({ theme: 'dark', env: { ANTHROPIC_BASE_URL: 'https://gateway.example.test/anthropic' } }));
    mockSpawn = vi.fn();
    vi.stubEnv('ANTHROPIC_BASE_URL', 'https://gateway.example.test/anthropic');
    vi.stubEnv('ANTHROPIC_AUTH_TOKEN', 'gateway-secret');
    vi.stubEnv('RELAY_BOT_TOKEN_AKI', '123:relay-bot');
    vi.stubEnv('RELAY_INTERNAL_TOKEN', 'relay-internal');
  });
  afterEach(() => { pool?.dispose(); vi.unstubAllEnvs(); rmSync(root, { recursive: true, force: true }); });

  it('reloads the declared token on a same-session CC process rebuild and keeps it out of argv', async () => {
    const file = join(root, '.env');
    writeFileSync(file, 'CLAUDE_CODE_OAUTH_TOKEN=sk-ant-oat01-first\nOTHER_SECRET=do-not-inherit\n');
    pool = createPool({ cleanupIntervalMs: 999999, ccMode: 'print', oauthEnvFile: file, primaryToken: 'stale-parent-token' });
    const config = { ...baseConfig, workspace: root, userHome: root, ccModel: 'claude-opus-5-5' };
    const first = createMockProcess();
    mockSpawn.mockReturnValueOnce(first);
    const started = pool.spawn(config);
    first.stdout.write('{"type":"system","content":"ready"}\n');
    await started;
    const [, firstArgs, firstOptions] = mockSpawn.mock.calls[0] as [string, string[], { env: NodeJS.ProcessEnv }];
    expect(firstOptions.env.CLAUDE_CODE_OAUTH_TOKEN).toBe('sk-ant-oat01-first');
    expect(firstOptions.env.ANTHROPIC_BASE_URL).toBe('https://api.anthropic.com');
    expect(firstOptions.env.ANTHROPIC_AUTH_TOKEN).toBe('');
    expect(firstOptions.env.ANTHROPIC_API_KEY).toBeUndefined();
    expect(firstOptions.env.OTHER_SECRET).toBeUndefined();
    expect(firstOptions.env.RELAY_BOT_TOKEN_AKI).toBeUndefined();
    expect(firstOptions.env.RELAY_INTERNAL_TOKEN).toBeUndefined();
    expect(firstArgs.join(' ')).not.toContain('sk-ant-oat01-first');
    expect(JSON.parse(firstArgs[firstArgs.lastIndexOf('--settings') + 1]).env).not.toHaveProperty('CLAUDE_CODE_OAUTH_TOKEN');
    await pool.kill(config.tenantId);
    const historyDir = join(root, '.claude', 'projects', realpathSync(root).replace(/[^A-Za-z0-9_-]/g, '-'));
    mkdirSync(historyDir, { recursive: true });
    const history = join(historyDir, config.sessionId + '.jsonl');
    const original = JSON.stringify({ type: 'user', uuid: 'original-message', parentUuid: null, timestamp: new Date().toISOString(), message: { role: 'user', content: 'original conversation' } }) + '\n';
    writeFileSync(history, original);
    writeFileSync(file, 'CLAUDE_CODE_OAUTH_TOKEN=sk-ant-oat01-second\n');
    const second = createMockProcess(1235);
    mockSpawn.mockReturnValueOnce(second);
    const resumed = pool.spawn({ ...config, resume: true });
    second.stdout.write('{"type":"system","content":"ready"}\n');
    const process = await resumed;
    const [, secondArgs, secondOptions] = mockSpawn.mock.calls[1] as [string, string[], { env: NodeJS.ProcessEnv }];
    expect(secondOptions.env.CLAUDE_CODE_OAUTH_TOKEN).toBe('sk-ant-oat01-second');
    expect(process.sessionId).toBe(config.sessionId);
    expect(secondArgs).toContain('--resume');
    expect(secondArgs).toContain(config.sessionId);
    expect(readFileSync(history, 'utf8')).toBe(original);
  });

  it('refuses to launch through a stale settings token copy', async () => {
    const file = join(root, '.env');
    writeFileSync(file, 'CLAUDE_CODE_OAUTH_TOKEN=sk-ant-oat01-current\n');
    writeFileSync(join(root, '.claude', 'settings.json'), JSON.stringify({ env: { CLAUDE_CODE_OAUTH_TOKEN: 'sk-ant-oat01-old' } }));
    pool = createPool({ cleanupIntervalMs: 999999, ccMode: 'print', oauthEnvFile: file });
    await expect(pool.spawn({ ...baseConfig, workspace: root, userHome: root })).rejects.toThrow('Remove CC OAuth token copies');
    expect(mockSpawn).not.toHaveBeenCalled();
  });

  it('keeps an explicit API route independent of the subscription file', async () => {
    const provider = { protocol: 'anthropic', base_url_env: 'OPENROUTER_BASE_URL', api_key_pool: ['OPENROUTER_ANTHROPIC_AUTH_TOKEN'] };
    vi.stubEnv('OPENROUTER_BASE_URL', 'https://openrouter.ai/api/v1');
    vi.stubEnv('OPENROUTER_ANTHROPIC_AUTH_TOKEN', 'api-route-secret');
    vi.stubEnv('CLAUDE_CODE_OAUTH_TOKEN', 'parent-subscription-secret');
    vi.stubEnv('CLAUDE_CODE_OAUTH_TOKEN_FALLBACK', 'parent-fallback-secret');
    const model = 'deepseek/deepseek-v4-flash-0731';
    const catalog = {
      providers: { openrouter: provider },
      findById: (id: string) => id === model ? { id, provider: 'openrouter' } : undefined,
      findProvider: (name: string) => name === 'openrouter' ? provider : undefined,
    } as never;
    pool = createPool({ cleanupIntervalMs: 999999, ccMode: 'print', oauthEnvFile: join(root, 'missing.env'), modelCatalog: catalog });
    const proc = createMockProcess();
    mockSpawn.mockReturnValue(proc);
    const promise = pool.spawn({ ...baseConfig, workspace: root, userHome: root, ccModel: model });
    proc.stdout.write('{"type":"system","content":"ready"}\n');
    await promise;
    const [, args, options] = mockSpawn.mock.calls[0] as [string, string[], { env: NodeJS.ProcessEnv }];
    expect(options.env.ANTHROPIC_AUTH_TOKEN).toBe('api-route-secret');
    expect(options.env.ANTHROPIC_BASE_URL).toBe('https://openrouter.ai/api/v1');
    expect(options.env.CLAUDE_CODE_OAUTH_TOKEN).toBeUndefined();
    expect(options.env.CLAUDE_CODE_OAUTH_TOKEN_FALLBACK).toBeUndefined();
    expect(args.join(' ')).not.toContain('parent-subscription-secret');
    expect(args.join(' ')).not.toContain('parent-fallback-secret');
  });
});

describe('createPool', () => {
  beforeEach(() => {
    mockSpawn = vi.fn();
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  it('returns a Pool interface', () => {
    const pool = createPool();
    expect(pool).toHaveProperty('spawn');
    expect(pool).toHaveProperty('send');
    expect(pool).toHaveProperty('kill');
    expect(pool).toHaveProperty('killAll');
    expect(pool).toHaveProperty('get');
    expect(pool).toHaveProperty('getAll');
    expect(pool).toHaveProperty('on');
    expect(pool).toHaveProperty('dispose');
    pool.dispose();
  });
});

describe('pool.spawn', () => {
  let pool: ReturnType<typeof createPool>;

  beforeEach(() => {
    mockSpawn = vi.fn();
    pool = createPool({ cleanupIntervalMs: 999999, ccMode: 'print' }); // disable cleanup timer for tests
  });

  afterEach(() => {
    pool.dispose();
    delete process.env['RELAY_CC_MODE'];
    delete process.env['ANTHROPIC_BASE_URL'];
    vi.restoreAllMocks();
  });

  it('gives the CC child the system sbin directories', async () => {
    const original = process.env['PATH'];
    process.env['PATH'] = '/opt/node/bin:/usr/bin:/bin';
    try {
      const proc = createMockProcess();
      mockSpawn.mockReturnValue(proc);
      const promise = pool.spawn(baseConfig);
      proc.stdout.write('{"type":"system","content":"ready"}\n');
      await promise;
      const opts = mockSpawn.mock.calls[0][2] as { env: NodeJS.ProcessEnv };
      expect(opts.env['PATH']).toBe('/opt/node/bin:/usr/bin:/bin:/usr/sbin:/sbin');
    } finally {
      process.env['PATH'] = original;
    }
  });

  it('builds correct CC spawn arguments', async () => {
    const proc = createMockProcess();
    mockSpawn.mockReturnValue(proc);

    const promise = pool.spawn(baseConfig);
    // Simulate CC ready: emit a line on stdout
    proc.stdout.write('{"type":"system","content":"ready"}\n');
    await promise;

    expect(mockSpawn).toHaveBeenCalledOnce();
    const [cmd, args, opts] = mockSpawn.mock.calls[0];
    expect(cmd).toBe(process.env['CLAUDE_BIN'] ?? 'claude');
    expect(args).toContain('--input-format');
    expect(args).toContain('stream-json');
    expect(args).toContain('--output-format');
    expect(args).toContain('--session-id');
    expect(args).toContain('test-session-uuid');
    expect(args).toContain('--dangerously-skip-permissions');
    expect(args).toContain('--allowed-tools');
    const allowedTools = args[args.indexOf('--allowed-tools') + 1];
    expect(allowedTools).toContain('Agent');
    expect(allowedTools).toContain('Task');
    // AskUserQuestion is no longer globally disallowed — intercepted by Feishu channel.
    expect(args).not.toContain('--disallowed-tools');
    expect(opts.cwd).toBe('/opt/PULSE/Tenants/member-000/');
  });

  it('uses a per-session workspace .relay/outbox subdirectory for file delivery [PT0-025 / FR-P0-019]', async () => {
    const workspace = mkdtempSync(join(tmpdir(), 'relay-pool-outbox-ws-'));
    try {
      const proc = createMockProcess();
      mockSpawn.mockReturnValue(proc);
      const config = { ...baseConfig, tenantId: 'member-outbox', workspace: `${workspace}/` };
      const promise = pool.spawn(config);
      proc.stdout.write('{"type":"system","content":"ready"}\n');
      await promise;

      const args = mockSpawn.mock.calls[0][1] as string[];
      const promptIdx = args.indexOf('--append-system-prompt');
      expect(promptIdx).toBeGreaterThanOrEqual(0);
      const prompt = args[promptIdx + 1];
      const expectedOutbox = join(realpathSync(workspace), '.relay', 'outbox', 'member-outbox');
      expect(prompt).toContain(`${expectedOutbox}\n`);
      expect(prompt).not.toContain('/tmp/relay-outbox');
      expect(existsSync(expectedOutbox)).toBe(true);
    } finally {
      rmSync(workspace, { recursive: true, force: true });
    }
  });

  it('omits --session-id and --resume when sessionId is empty or whitespace [bug-20260504-001]', async () => {
    const proc = createMockProcess();
    mockSpawn.mockReturnValue(proc);

    for (const sessionId of ['', '   ', '\t']) {
      mockSpawn.mockClear();
      const p = pool.spawn({ ...baseConfig, tenantId: `t-${sessionId.length || 'empty'}`, sessionId });
      proc.stdout.write('{"type":"system","content":"ready"}\n');
      await p;
      const args = mockSpawn.mock.calls[0][1] as string[];
      expect(args, `sessionId=${JSON.stringify(sessionId)}`).not.toContain('--resume');
      expect(args, `sessionId=${JSON.stringify(sessionId)}`).not.toContain('--session-id');
      await pool.kill(`t-${sessionId.length || 'empty'}`);
    }
  });

  it('passes --model to CC when ccModel is set [P2-006]', async () => {
    const proc = createMockProcess();
    mockSpawn.mockReturnValue(proc);

    const promise = pool.spawn({ ...baseConfig, ccModel: '  sonnet-xyz  ' });
    proc.stdout.write('{"type":"system","content":"ready"}\n');
    await promise;

    expect(mockSpawn).toHaveBeenCalledOnce();
    const [, args] = mockSpawn.mock.calls[0];
    const modelIdx = args.indexOf('--model');
    expect(modelIdx).toBeGreaterThan(-1);
    expect(args[modelIdx + 1]).toBe('sonnet-xyz');
  });

  it('drops --model when ccModel contains TUI state like "opus[1m]"', async () => {
    const proc = createMockProcess();
    mockSpawn.mockReturnValue(proc);

    const promise = pool.spawn({ ...baseConfig, ccModel: 'opus[1m]' });
    proc.stdout.write('{"type":"system","content":"ready"}\n');
    await promise;

    const args = mockSpawn.mock.calls[0][1] as string[];
    expect(args).not.toContain('--model');
  });

  it('keeps --model for provider-slug ids like "deepseek/deepseek-v4-flash-0731"', async () => {
    const proc = createMockProcess();
    mockSpawn.mockReturnValue(proc);

    const promise = pool.spawn({ ...baseConfig, ccModel: 'deepseek/deepseek-v4-flash-0731' });
    proc.stdout.write('{"type":"system","content":"ready"}\n');
    await promise;

    const args = mockSpawn.mock.calls[0][1] as string[];
    const modelIdx = args.indexOf('--model');
    expect(modelIdx).toBeGreaterThan(-1);
    expect(args[modelIdx + 1]).toBe('deepseek/deepseek-v4-flash-0731');
  });

  it('does not expose unselected provider key-pool credentials to nested tools', async () => {
    const proc = createMockProcess();
    mockSpawn.mockReturnValue(proc);
    const previousKey = process.env['OPENROUTER_API_KEY'];
    const previousAnthropicToken = process.env['OPENROUTER_ANTHROPIC_AUTH_TOKEN'];
    process.env['OPENROUTER_API_KEY'] = 'sk-or-v1-unselected-legacy-secret';
    process.env['OPENROUTER_ANTHROPIC_AUTH_TOKEN'] = 'sk-or-v1-unselected-catalog-secret';
    const modelCatalog = {
      providers: {
        openrouter: {
          protocol: 'anthropic',
          base_url_env: 'OPENROUTER_BASE_URL',
          api_key_pool: ['OPENROUTER_ANTHROPIC_AUTH_TOKEN'],
        },
      },
      findById: () => undefined,
      findProvider: () => undefined,
    } as never;
    const isolatedPool = createPool({
      cleanupIntervalMs: 999999,
      ccMode: 'print',
      modelCatalog,
    });

    try {
      const promise = isolatedPool.spawn(baseConfig);
      proc.stdout.write('{"type":"system","content":"ready"}\n');
      await promise;

      const spawnOptions = mockSpawn.mock.calls[0][2] as { env: NodeJS.ProcessEnv };
      expect(spawnOptions.env['OPENROUTER_API_KEY']).toBeUndefined();
      expect(spawnOptions.env['OPENROUTER_ANTHROPIC_AUTH_TOKEN']).toBeUndefined();
    } finally {
      isolatedPool.dispose();
      if (previousKey === undefined) delete process.env['OPENROUTER_API_KEY'];
      else process.env['OPENROUTER_API_KEY'] = previousKey;
      if (previousAnthropicToken === undefined) delete process.env['OPENROUTER_ANTHROPIC_AUTH_TOKEN'];
      else process.env['OPENROUTER_ANTHROPIC_AUTH_TOKEN'] = previousAnthropicToken;
    }
  });

  it('scopes an explicitly selected third-party provider and redacts its spawn log', async () => {
    const proc = createMockProcess();
    mockSpawn.mockReturnValue(proc);
    const secret = 'sk-or-v1-explicit-provider-secret';
    const legacySecret = 'sk-or-v1-explicit-legacy-secret';
    const subscriptionHome = mkdtempSync(join(tmpdir(), 'relay-subscription-home-'));
    const subscriptionToken = 'test-subscription-oauth-secret';
    mkdirSync(join(subscriptionHome, '.claude'));
    writeFileSync(join(subscriptionHome, '.claude', 'settings.json'), JSON.stringify({
      env: { CLAUDE_CODE_OAUTH_TOKEN: subscriptionToken, CLAUDE_CODE_OAUTH_TOKEN_FALLBACK: subscriptionToken },
    }));
    const model = 'deepseek/deepseek-v4-flash-0731';
    const previousKey = process.env['OPENROUTER_API_KEY'];
    const previousAnthropicToken = process.env['OPENROUTER_ANTHROPIC_AUTH_TOKEN'];
    const previousBaseUrl = process.env['OPENROUTER_BASE_URL'];
    process.env['OPENROUTER_API_KEY'] = legacySecret;
    process.env['OPENROUTER_ANTHROPIC_AUTH_TOKEN'] = secret;
    process.env['OPENROUTER_BASE_URL'] = 'https://openrouter.ai/api/v1';
    const logSpy = vi.spyOn(console, 'log').mockImplementation(() => {});
    const provider = {
      protocol: 'anthropic' as const,
      base_url_env: 'OPENROUTER_BASE_URL',
      api_key_pool: ['OPENROUTER_ANTHROPIC_AUTH_TOKEN'],
    };
    const entry = { id: model, display_name: 'DeepSeek V4 Flash', provider: 'openrouter' };
    const modelCatalog = {
      providers: { openrouter: provider },
      findById: (id: string) => id === model ? entry : undefined,
      findProvider: (name: string) => name === 'openrouter' ? provider : undefined,
    } as never;
    const scopedPool = createPool({
      cleanupIntervalMs: 999999,
      ccMode: 'print',
      modelCatalog,
    });

    try {
      const promise = scopedPool.spawn({ ...baseConfig, ccModel: model, userHome: subscriptionHome });
      proc.stdout.write('{"type":"system","content":"ready"}\n');
      await promise;

      const [, args, spawnOptions] = mockSpawn.mock.calls[0] as [string, string[], { env: NodeJS.ProcessEnv }];
      expect(spawnOptions.env['OPENROUTER_API_KEY']).toBeUndefined();
      expect(spawnOptions.env['OPENROUTER_ANTHROPIC_AUTH_TOKEN']).toBeUndefined();
      expect(spawnOptions.env['ANTHROPIC_AUTH_TOKEN']).toBe(secret);

      const settingsIndex = args.lastIndexOf('--settings');
      expect(settingsIndex).toBeGreaterThanOrEqual(0);
      const settings = JSON.parse(args[settingsIndex + 1]) as {
        env: Record<string, string>;
        modelOverrides: Record<string, string>;
      };
      expect(settings.env).toMatchObject({
        CLAUDE_CODE_OAUTH_TOKEN: '',
        CLAUDE_CODE_OAUTH_TOKEN_FALLBACK: '',
        ANTHROPIC_API_KEY: '',
        ANTHROPIC_DEFAULT_HAIKU_MODEL: model,
        ANTHROPIC_DEFAULT_SONNET_MODEL: model,
        ANTHROPIC_DEFAULT_OPUS_MODEL: model,
        ANTHROPIC_CUSTOM_MODEL_OPTION: model,
      });
      expect(settings.modelOverrides).toMatchObject({
        'claude-haiku-4-5': model,
      });
      expect(args.join(' ')).not.toContain(subscriptionToken);

      const spawnLog = logSpy.mock.calls
        .map(([value]) => value as Record<string, unknown>)
        .find((value) => value?.msg === 'Spawning CC');
      expect(spawnLog).toBeDefined();
      expect(JSON.stringify(spawnLog)).not.toContain(secret);
      expect(JSON.stringify(spawnLog)).not.toContain(legacySecret);
      expect(String(spawnLog?.args)).toContain('--settings [REDACTED]');
    } finally {
      scopedPool.dispose();
      rmSync(subscriptionHome, { recursive: true, force: true });
      if (previousKey === undefined) delete process.env['OPENROUTER_API_KEY'];
      else process.env['OPENROUTER_API_KEY'] = previousKey;
      if (previousAnthropicToken === undefined) delete process.env['OPENROUTER_ANTHROPIC_AUTH_TOKEN'];
      else process.env['OPENROUTER_ANTHROPIC_AUTH_TOKEN'] = previousAnthropicToken;
      if (previousBaseUrl === undefined) delete process.env['OPENROUTER_BASE_URL'];
      else process.env['OPENROUTER_BASE_URL'] = previousBaseUrl;
    }
  });

  it('drops --model when env RELAY_CLAUDE_MODEL is malformed', async () => {
    const prev = process.env['RELAY_CLAUDE_MODEL'];
    process.env['RELAY_CLAUDE_MODEL'] = 'opus[1m]';
    try {
      const proc = createMockProcess();
      mockSpawn.mockReturnValue(proc);

      const promise = pool.spawn({ ...baseConfig });
      proc.stdout.write('{"type":"system","content":"ready"}\n');
      await promise;

      const args = mockSpawn.mock.calls[0][1] as string[];
      expect(args).not.toContain('--model');
    } finally {
      if (prev === undefined) delete process.env['RELAY_CLAUDE_MODEL'];
      else process.env['RELAY_CLAUDE_MODEL'] = prev;
    }
  });

  it('adds subagent tools when an agent provides a custom allowedTools list', async () => {
    const proc = createMockProcess();
    mockSpawn.mockReturnValue(proc);

    const promise = pool.spawn({ ...baseConfig, allowedTools: ['Read', 'Bash'] });
    proc.stdout.write('{"type":"system","content":"ready"}\n');
    await promise;

    const args = mockSpawn.mock.calls[0][1] as string[];
    expect(args[args.indexOf('--allowed-tools') + 1]).toBe('Read,Bash,Agent,Task');
  });

  it('preserves existing subagent tools without duplicates', async () => {
    const proc = createMockProcess();
    mockSpawn.mockReturnValue(proc);

    const promise = pool.spawn({ ...baseConfig, allowedTools: ['Read', 'Agent'] });
    proc.stdout.write('{"type":"system","content":"ready"}\n');
    await promise;

    const args = mockSpawn.mock.calls[0][1] as string[];
    expect(args[args.indexOf('--allowed-tools') + 1]).toBe('Read,Agent,Task');
  });

  it('always uses --session-id even when resume is true', async () => {
    const proc = createMockProcess();
    mockSpawn.mockReturnValue(proc);

    const promise = pool.spawn({ ...baseConfig, resume: true });
    proc.stdout.write('{"type":"system","content":"ready"}\n');
    await promise;

    const args = mockSpawn.mock.calls[0][1] as string[];
    expect(args).toContain('--session-id');
    expect(args).not.toContain('--resume');
    expect(args).toContain('test-session-uuid');
  });

  it('preempts (kills) the existing process when spawning duplicate tenantId', async () => {
    // 2026-06-16: spawnProcess used to `throw new Error('Process already exists')`,
    // but the rate-limit backoff path could delete the in-memory process record
    // BEFORE calling spawnProcess, hiding a still-running child from this guard
    // and producing orphan CC siblings. The new contract: spawn always wins by
    // killing whatever was there. See pool-rate-limit-orphan-spawn.test.ts (Case C).
    const proc1 = createMockProcess(7001);
    const proc2 = createMockProcess(7002);
    mockSpawn.mockReturnValueOnce(proc1).mockReturnValueOnce(proc2);

    const promise = pool.spawn(baseConfig);
    proc1.stdout.write('{"type":"system","content":"ready"}\n');
    await promise;
    expect(pool.get('member-000')?.process.pid).toBe(7001);

    await expect(pool.spawn(baseConfig)).resolves.toBeDefined();

    expect(proc1.kill).toHaveBeenCalled();
    expect(pool.get('member-000')?.process.pid).toBe(7002);
  });

  it('transitions state: starting → ready after first stdout', async () => {
    const proc = createMockProcess();
    mockSpawn.mockReturnValue(proc);

    const promise = pool.spawn(baseConfig);

    // Before stdout — state is starting
    const starting = pool.get('member-000');
    expect(starting?.state).toBe('starting');

    proc.stdout.write('{"type":"system","content":"ready"}\n');
    const result = await promise;
    expect(result.state).toBe('ready');
  });

  it('uses PTY bridge in interactive mode', async () => {
    const proc = createMockProcess();
    mockSpawn.mockReturnValue(proc);
    const interactivePool = createPool({ cleanupIntervalMs: 999999, ccMode: 'interactive' });

    await interactivePool.spawn(baseConfig);

    expect(mockSpawn).toHaveBeenCalledOnce();
    const [cmd, args, opts] = mockSpawn.mock.calls[0];
    expect(cmd).toBe('python3');
    expect(args[0]).toBe('-u');
    expect(String(args[1])).toContain('claude_pty_bridge.py');
    expect(args).toContain(process.env['CLAUDE_BIN'] ?? 'claude');
    expect(args).toContain('--permission-mode');
    expect(args).toContain('dontAsk');
    expect(args).toContain('--allowed-tools');
    const allowedTools = args[args.indexOf('--allowed-tools') + 1];
    expect(allowedTools).toContain('Agent');
    expect(allowedTools).toContain('Task');
    // AskUserQuestion is no longer globally disallowed — intercepted by Feishu channel.
    expect(args).not.toContain('--disallowed-tools');
    expect(opts.cwd).toBe('/opt/PULSE/Tenants/member-000/');
    interactivePool.dispose();
  });

  it('uses PTY bridge when the spawn config requests interactive mode', async () => {
    const proc = createMockProcess();
    mockSpawn.mockReturnValue(proc);
    const defaultPool = createPool({ cleanupIntervalMs: 999999 });

    await defaultPool.spawn({ ...baseConfig, ccMode: 'interactive' });

    expect(mockSpawn).toHaveBeenCalledOnce();
    const [cmd, args] = mockSpawn.mock.calls[0];
    expect(cmd).toBe('python3');
    expect(args[0]).toBe('-u');
    expect(String(args[1])).toContain('claude_pty_bridge.py');
    // --bare removed permanently: it disabled CC interactive tools (AskUserQuestion).
    expect(args).not.toContain('--bare');
    defaultPool.dispose();
  });

  it('uses print mode for a third-party ANTHROPIC_BASE_URL without a catalog opt-in', async () => {
    const proc = createMockProcess();
    mockSpawn.mockReturnValue(proc);
    vi.stubEnv('ANTHROPIC_BASE_URL', 'https://gateway.example.test/anthropic');
    const autoPool = createPool({ cleanupIntervalMs: 999999 });

    await autoPool.spawn(baseConfig);

    expect(mockSpawn).toHaveBeenCalledOnce();
    const [cmd, args] = mockSpawn.mock.calls[0];
    expect(cmd).toBe(process.env['CLAUDE_BIN'] ?? 'claude');
    expect(args).toContain('-p');
    autoPool.dispose();
    vi.unstubAllEnvs();
  });

  it('uses print mode for Claude models when ccModel is set', async () => {
    const proc = createMockProcess();
    mockSpawn.mockReturnValue(proc);
    const autoPool = createPool({ cleanupIntervalMs: 999999 });

    await autoPool.spawn({ ...baseConfig, ccModel: 'claude-sonnet-4-0' });

    expect(mockSpawn).toHaveBeenCalledOnce();
    const [cmd, args] = mockSpawn.mock.calls[0];
    expect(cmd).toBe(process.env['CLAUDE_BIN'] ?? 'claude');
    expect(args).toContain('-p');
    autoPool.dispose();
  });

  function forceInteractiveCatalog(model: string): never {
    const provider = {
      protocol: 'anthropic',
      base_url_env: 'OPENROUTER_ANTHROPIC_BASE_URL',
      api_key_pool: ['OPENROUTER_ANTHROPIC_AUTH_TOKEN'],
      force_interactive: true,
    };
    return {
      providers: { openrouter: provider },
      entries: [{ id: model, provider: 'openrouter' }],
      findById: (id: string) => id === model ? { id, provider: 'openrouter' } : undefined,
      findProvider: (name: string) => name === 'openrouter' ? provider : undefined,
      getProviderForModel: (id: string) => id === model ? provider : undefined,
      isProviderAvailable: () => true,
    } as never;
  }

  it('uses interactive mode when the catalog provider sets force_interactive', async () => {
    const proc = createMockProcess();
    mockSpawn.mockReturnValue(proc);
    const model = 'deepseek/deepseek-v4-flash-0731';
    vi.stubEnv('OPENROUTER_ANTHROPIC_BASE_URL', 'https://openrouter.ai/api');
    vi.stubEnv('OPENROUTER_ANTHROPIC_AUTH_TOKEN', 'openrouter-token');
    const autoPool = createPool({ cleanupIntervalMs: 999999, modelCatalog: forceInteractiveCatalog(model) });

    await autoPool.spawn({ ...baseConfig, ccModel: model });

    expect(mockSpawn).toHaveBeenCalledOnce();
    const [cmd, args] = mockSpawn.mock.calls[0];
    expect(cmd).toBe('python3');
    expect(args).not.toContain('--bare');
    autoPool.dispose();
    vi.unstubAllEnvs();
  });

  it('agent ccMode print overrides provider force_interactive', async () => {
    const proc = createMockProcess();
    mockSpawn.mockReturnValue(proc);
    const model = 'deepseek/deepseek-v4-flash-0731';
    vi.stubEnv('OPENROUTER_ANTHROPIC_BASE_URL', 'https://openrouter.ai/api');
    vi.stubEnv('OPENROUTER_ANTHROPIC_AUTH_TOKEN', 'openrouter-token');
    const autoPool = createPool({ cleanupIntervalMs: 999999, modelCatalog: forceInteractiveCatalog(model) });

    await autoPool.spawn({ ...baseConfig, ccModel: model, ccMode: 'print' });

    expect(mockSpawn).toHaveBeenCalledOnce();
    const [cmd, args] = mockSpawn.mock.calls[0];
    expect(cmd).toBe(process.env['CLAUDE_BIN'] ?? 'claude');
    expect(args).toContain('-p');
    autoPool.dispose();
    vi.unstubAllEnvs();
  });

  it('passes --add-dir <workspace> in interactive bare mode (CLAUDE.md re-enable)', async () => {
    // bug-20260510-001 follow-up: --bare disables CLAUDE.md auto-discovery,
    // and CC's official re-entry is `--add-dir (CLAUDE.md dirs)`. Without
    // this flag the tenant Agent identity is silently lost.
    const proc = createMockProcess();
    mockSpawn.mockReturnValue(proc);
    const interactivePool = createPool({ cleanupIntervalMs: 999999, ccMode: 'interactive' });
    const workspace = mkdtempSync(join(tmpdir(), 'relay-pool-add-dir-'));

    try {
      await interactivePool.spawn({ ...baseConfig, workspace });

      const args = mockSpawn.mock.calls[0][1] as string[];
      expect(args).toContain('--add-dir');
      const idx = args.indexOf('--add-dir');
      expect(realpathSync(args[idx + 1])).toBe(realpathSync(workspace));
    } finally {
      interactivePool.dispose();
      rmSync(workspace, { recursive: true, force: true });
    }
  });

  it('passes Claude settings source to interactive bare mode when available', async () => {
    const proc = createMockProcess();
    mockSpawn.mockReturnValue(proc);
    const originalHome = process.env['HOME'];
    const homeDir = mkdtempSync(join(tmpdir(), 'relay-pool-home-'));
    const settingsDir = join(homeDir, '.claude');
    const settingsPath = join(settingsDir, 'settings.json');
    mkdirSync(settingsDir, { recursive: true });
    writeFileSync(settingsPath, JSON.stringify({ env: { ANTHROPIC_BASE_URL: 'https://example.invalid' } }));
    process.env['HOME'] = homeDir;
    const interactivePool = createPool({ cleanupIntervalMs: 999999, ccMode: 'interactive' });

    try {
      await interactivePool.spawn(baseConfig);

      const args = mockSpawn.mock.calls[0][1] as string[];
      expect(args).toEqual(expect.arrayContaining([
        '--setting-sources',
        'user,project,local',
        '--settings',
        settingsPath,
      ]));
      expect(args).not.toContain('--bare');
      expect(args).not.toContain(JSON.stringify({ env: { ANTHROPIC_BASE_URL: 'https://example.invalid' } }));
    } finally {
      interactivePool.dispose();
      if (originalHome === undefined) delete process.env['HOME'];
      else process.env['HOME'] = originalHome;
      rmSync(homeDir, { recursive: true, force: true });
    }
  });
  // --- PT0-018: sudo -u tenant isolation spawn (Red phase) ---

  it('prefixes spawn with sudo -u when linuxUser is set (print mode)', async () => {
    const proc = createMockProcess();
    mockSpawn.mockReturnValue(proc);
    const prevToken = process.env['ANTHROPIC_AUTH_TOKEN'];
    process.env['ANTHROPIC_AUTH_TOKEN'] = 'test-token-pool-sudo';

    try {
      await pool.spawn({ ...baseConfig, linuxUser: 'tenant-member-0001' });

      expect(mockSpawn).toHaveBeenCalledOnce();
      const [cmd, args, opts] = mockSpawn.mock.calls[0];
      expect(cmd).toBe('sudo');
      // args: ['-u', 'tenant-member-0001', '--preserve-env=ANTHROPIC_AUTH_TOKEN,...', 'claude', ...printArgs]
      expect(args[0]).toBe('-u');
      expect(args[1]).toBe('tenant-member-0001');
      expect(args[2]).toMatch(/^--preserve-env=/);
      // preserve-env list should include HOME; ANTHROPIC_AUTH_TOKEN is excluded
      // because it's delivered via apiKeyHelper when linuxUser is set
      const preserveArg = args[2] as string;
      expect(preserveArg).toContain('HOME');
      expect(preserveArg).not.toContain('ANTHROPIC_AUTH_TOKEN');
      // claude binary follows preserve-env arg
      const claudeBin = process.env['CLAUDE_BIN'] ?? 'claude';
      const claudeIdx = args.indexOf(claudeBin);
      expect(claudeIdx).toBeGreaterThan(2);
      // Original print-mode flags still present
      expect(args).toContain('--input-format');
      expect(args).toContain('stream-json');
      expect(args).toContain('--dangerously-skip-permissions');
      expect(opts.cwd).toBe('/opt/PULSE/Tenants/member-000/');

      const settingsIndex = args.indexOf('--settings');
      expect(settingsIndex).toBeGreaterThan(-1);
      const settings = JSON.parse(args[settingsIndex + 1]) as { apiKeyHelper: string };
      const apiKeyFilePath = settings.apiKeyHelper.replace(/^cat /, '');
      expect(apiKeyFilePath).toMatch(/\.relay-key-[^-]+-[0-9a-f-]{36}$/);
      expect(existsSync(apiKeyFilePath)).toBe(true);
      await pool.kill('member-000');
      expect(existsSync(apiKeyFilePath)).toBe(false);
    } finally {
      if (prevToken === undefined) delete process.env['ANTHROPIC_AUTH_TOKEN'];
      else process.env['ANTHROPIC_AUTH_TOKEN'] = prevToken;
    }
  });

  it('prefixes spawn with sudo -u when linuxUser is set (interactive mode)', async () => {
    const proc = createMockProcess();
    mockSpawn.mockReturnValue(proc);
    const interactivePool = createPool({ cleanupIntervalMs: 999999, ccMode: 'interactive' });

    try {
      await interactivePool.spawn({ ...baseConfig, linuxUser: 'tenant-member-0002' });

      expect(mockSpawn).toHaveBeenCalledOnce();
      const [cmd, args, opts] = mockSpawn.mock.calls[0];
      expect(cmd).toBe('sudo');
      // args: ['-u', 'tenant-member-0002', '--preserve-env=ANTHROPIC_AUTH_TOKEN,...', 'python3', '-u', 'bridge.py', 'claude', ...interactiveArgs]
      expect(args[0]).toBe('-u');
      expect(args[1]).toBe('tenant-member-0002');
      expect(args[2]).toMatch(/^--preserve-env=/);
      const pythonIdx = args.indexOf('python3');
      expect(pythonIdx).toBeGreaterThan(2);
      // Original interactive-mode flags still present (--bare removed permanently)
      expect(args).not.toContain('--bare');
      expect(args).toContain('--dangerously-skip-permissions');
      expect(args).toContain('--add-dir');
      expect(opts.cwd).toBe('/opt/PULSE/Tenants/member-000/');
    } finally {
      interactivePool.dispose();
    }
  });

  it('uses tenant HOME for --settings when linuxUser is set (interactive)', async () => {
    const proc = createMockProcess();
    mockSpawn.mockReturnValue(proc);
    const relayHome = mkdtempSync(join(tmpdir(), 'relay-pulse-home-'));
    const tenantWorkspace = mkdtempSync(join(tmpdir(), 'relay-tenant-ws-'));
    const tenantSettingsDir = join(tenantWorkspace, '.claude');
    const tenantSettingsPath = join(tenantSettingsDir, 'settings.json');
    mkdirSync(tenantSettingsDir, { recursive: true });
    writeFileSync(tenantSettingsPath, JSON.stringify({ env: { FOO: 'tenant' } }));
    mkdirSync(join(relayHome, '.claude'), { recursive: true });
    writeFileSync(join(relayHome, '.claude', 'settings.json'), JSON.stringify({ env: { FOO: 'pulse' } }));
    const originalHome = process.env['HOME'];
    process.env['HOME'] = relayHome;
    const interactivePool = createPool({ cleanupIntervalMs: 999999, ccMode: 'interactive' });

    try {
      await interactivePool.spawn({
        ...baseConfig,
        workspace: `${tenantWorkspace}/`,
        linuxUser: 'tenant-member-0002',
      });

      const args = mockSpawn.mock.calls[0][1] as string[];
      expect(args).toContain('--settings');
      const idx = args.indexOf('--settings');
      expect(args[idx + 1]).toBe(realpathSync(tenantSettingsPath));
      expect(args[idx + 1]).not.toContain(relayHome);
    } finally {
      interactivePool.dispose();
      if (originalHome === undefined) delete process.env['HOME'];
      else process.env['HOME'] = originalHome;
      rmSync(relayHome, { recursive: true, force: true });
      rmSync(tenantWorkspace, { recursive: true, force: true });
    }
  });

  it('does not prefix with sudo when linuxUser is absent (backward compat)', async () => {
    const proc = createMockProcess();
    mockSpawn.mockReturnValue(proc);

    await pool.spawn(baseConfig);

    expect(mockSpawn).toHaveBeenCalledOnce();
    const [cmd, args] = mockSpawn.mock.calls[0];
    // No sudo prefix — original behavior unchanged
    expect(cmd).not.toBe('sudo');
    expect(cmd).toBe(process.env['CLAUDE_BIN'] ?? 'claude');
    expect(args).not.toContain('-u');
    expect(args).not.toContain('tenant-');
  });
});

describe('pool.send', () => {
  let pool: ReturnType<typeof createPool>;

  beforeEach(() => {
    mockSpawn = vi.fn();
    pool = createPool({ cleanupIntervalMs: 999999, ccMode: 'print' });
  });

  afterEach(() => {
    pool.dispose();
    delete process.env['RELAY_CC_MODE'];
    vi.restoreAllMocks();
  });

  it('writes message to stdin and updates state to busy', async () => {
    const proc = createMockProcess();
    mockSpawn.mockReturnValue(proc);

    const spawnPromise = pool.spawn(baseConfig);
    proc.stdout.write('{"type":"system","content":"ready"}\n');
    await spawnPromise;

    const stdinChunks: Buffer[] = [];
    proc.stdin.on('data', (chunk: Buffer) => stdinChunks.push(chunk));

    await pool.send('member-000', { type: 'human', content: 'hello' });

    const written = Buffer.concat(stdinChunks).toString();
    // CC stream-json input wraps in user envelope
    expect(written).toContain('"type":"user"');
    expect(written).toContain('"content":"hello"');

    const process = pool.get('member-000');
    expect(process?.state).toBe('busy');
  });

  it('sends single-line interactive prompt directly via PTY', async () => {
    const proc = createMockProcess();
    const writeSpy = vi.spyOn(proc.stdin, 'write');
    mockSpawn.mockReturnValue(proc);
    const interactivePool = createPool({
      cleanupIntervalMs: 999999,
      ccMode: 'interactive',
      interactiveWarmupMs: 0,
      interactiveSubmitDelayMs: 0,
    });

    await interactivePool.spawn(baseConfig);
    await interactivePool.send('member-000', { type: 'human', content: 'hello' });
    await new Promise((resolve) => setTimeout(resolve, 20));

    // Single-line prompts (no \n) go directly through PTY — no temp file.
    const writes = writeSpy.mock.calls.map((call) => call[0]?.toString());
    const allWritten = writes.join('');
    expect(allWritten).toContain('hello');
    expect(allWritten).toContain('\r');
    expect(allWritten).not.toContain('.relay/');
    expect(allWritten).not.toContain('[Relay]');

    interactivePool.dispose();
  });

  it('auto-accepts Bypass Permissions dialog with down+enter before prompt submit', async () => {
    const proc = createMockProcess();
    const writeSpy = vi.spyOn(proc.stdin, 'write');
    mockSpawn.mockReturnValue(proc);
    const interactivePool = createPool({
      cleanupIntervalMs: 999999,
      ccMode: 'interactive',
      interactiveWarmupMs: 0,
      interactiveSubmitDelayMs: 50,
    });

    await interactivePool.spawn(baseConfig);
    void interactivePool.send('member-000', { type: 'human', content: 'hello' });

    proc.stdout.write(
      'By proceeding, you accept all responsibility for actions taken while running in Bypass Permissions mode.\n'
      + '  1. No, exit\n  2. Yes, I accept\n',
    );

    await new Promise((resolve) => setTimeout(resolve, 120));

    const writes = writeSpy.mock.calls.map((call) => call[0]?.toString());
    const allWritten = writes.join('');
    expect(allWritten).toContain('\x1b[B\r');
    expect(allWritten).toContain('hello');

    interactivePool.dispose();
  });

  it('auto-accepts Bypass Permissions dialog when PTY uses cursor-forward spacing', async () => {
    const proc = createMockProcess();
    const writeSpy = vi.spyOn(proc.stdin, 'write');
    mockSpawn.mockReturnValue(proc);
    const interactivePool = createPool({
      cleanupIntervalMs: 999999,
      ccMode: 'interactive',
      interactiveWarmupMs: 0,
      interactiveSubmitDelayMs: 50,
    });

    await interactivePool.spawn(baseConfig);
    void interactivePool.send('member-000', { type: 'human', content: 'hello' });

    // CC 2.x real PTY: words separated by ESC [ n C cursor-forward, not ASCII spaces.
    proc.stdout.write(
      '\x1b[?2026h WARNING:\x1b[1CClaude\x1b[1CCode\x1b[1Crunning\x1b[1Cin\x1b[1CBypass\x1b[1CPermissions\x1b[1Cmode',
    );
    proc.stdout.write(
      ' By\x1b[1Cproceeding,\x1b[1Cyou\x1b[1Caccept\x1b[1Call\x1b[1Cresponsibility\x1b[1Cfor\x1b[1Cactions\x1b[1Ctaken\x1b[1Cwhile\x1b[1Crunning',
    );
    proc.stdout.write(
      ' \x1b[2C1.\x1b[1CNo,\x1b[1Cexit\n \x1b[4C2.\x1b[1CYes,\x1b[1CI\x1b[1Caccept\n \x1b[2CEnter\x1b[1Cto\x1b[1Cconfirm',
    );

    await new Promise((resolve) => setTimeout(resolve, 200));

    const writes = writeSpy.mock.calls.map((call) => call[0]?.toString());
    expect(writes.join('')).toContain('\x1b[B\r');

    interactivePool.dispose();
  });

  it('auto-accepts /root trust dialog with enter before prompt submit', async () => {
    const proc = createMockProcess();
    const writeSpy = vi.spyOn(proc.stdin, 'write');
    mockSpawn.mockReturnValue(proc);
    const interactivePool = createPool({
      cleanupIntervalMs: 999999,
      ccMode: 'interactive',
      interactiveWarmupMs: 0,
      interactiveSubmitDelayMs: 50,
    });

    await interactivePool.spawn(baseConfig);
    void interactivePool.send('member-000', { type: 'human', content: 'hello' });

    proc.stdout.write(
      'Accessing workspace: /root\n'
      + 'Quick safety check: Is this a project you created or one you trust?\n',
    );

    await new Promise((resolve) => setTimeout(resolve, 120));

    const writes = writeSpy.mock.calls.map((call) => call[0]?.toString());
    const allWritten = writes.join('');
    expect(allWritten).toContain('\r');
    expect(allWritten).toContain('hello');

    interactivePool.dispose();
  });

  it('auto-accepts /root trust dialog when PTY uses cursor-forward spacing', async () => {
    const proc = createMockProcess();
    const writeSpy = vi.spyOn(proc.stdin, 'write');
    mockSpawn.mockReturnValue(proc);
    const interactivePool = createPool({
      cleanupIntervalMs: 999999,
      ccMode: 'interactive',
      interactiveWarmupMs: 0,
      interactiveSubmitDelayMs: 50,
    });

    await interactivePool.spawn(baseConfig);
    void interactivePool.send('member-000', { type: 'human', content: 'hello' });

    proc.stdout.write(
      '\x1b[?2026h\x1b[38;5;220m\x1b[1mAccessing\x1b[1Cworkspace:\x1b[22m\x1b[39m\x1b[1m/root\x1b[22m',
    );
    proc.stdout.write(
      '\x1b[1CQuick\x1b[1Csafety\x1b[1Ccheck:\x1b[1CIs\x1b[1Cthis\x1b[1Ca\x1b[1Cproject\x1b[1Cyou\x1Ccreated\x1C or\x1b[1Cone\x1b[1Cyou\x1Ctrust?',
    );

    await new Promise((resolve) => setTimeout(resolve, 120));

    const writes = writeSpy.mock.calls.map((call) => call[0]?.toString());
    expect(writes.join('')).toContain('\r');

    interactivePool.dispose();
  });

  it('writes multi-line prompt to temp file and sends meta-prompt', async () => {
    const proc = createMockProcess();
    const writeSpy = vi.spyOn(proc.stdin, 'write');
    mockSpawn.mockReturnValue(proc);
    const interactivePool = createPool({
      cleanupIntervalMs: 999999,
      ccMode: 'interactive',
      interactiveWarmupMs: 0,
      interactiveSubmitDelayMs: 0,
    });
    const workspace = mkdtempSync(join(tmpdir(), 'relay-pool-multiline-'));
    // Contains \n → must use temp file (PTY interprets \n as Enter)
    const multiLinePrompt = 'line one\nline two\nline three';

    try {
      await interactivePool.spawn({ ...baseConfig, workspace });
      await interactivePool.send('member-000', { type: 'human', content: multiLinePrompt });
      await new Promise((resolve) => setTimeout(resolve, 20));

      // Multi-line prompts go through temp file + meta-prompt.
      const writes = writeSpy.mock.calls.map((call) => call[0]?.toString());
      const allWritten = writes.join('');
      expect(allWritten).toContain('[Relay]');
      expect(allWritten).toContain('prompt-member-000');
      expect(allWritten).toContain('Read ');
      expect(allWritten).toContain('\r');
    } finally {
      interactivePool.dispose();
      rmSync(workspace, { recursive: true, force: true });
    }
  });

  it('settles interactive /compact without jsonl echo and drains queued messages', async () => {
    const proc = createMockProcess();
    const writeSpy = vi.spyOn(proc.stdin, 'write');
    mockSpawn.mockReturnValue(proc);
    const workspace = mkdtempSync(join(tmpdir(), 'relay-pool-interactive-compact-'));
    const sessionFile = getBareSessionFilePath(baseConfig.sessionId);
    mkdirSync(dirname(sessionFile), { recursive: true });
    const interactivePool = createPool({
      cleanupIntervalMs: 999999,
      ccMode: 'interactive',
      interactiveWarmupMs: 0,
      interactiveSubmitDelayMs: 0,
      interactiveAcceptTimeoutMs: 1000,
      interactiveSlashCommandSettleMs: 10,
      killTimeoutMs: 1,
    });
    const onAccepted = vi.fn();
    interactivePool.on('turn-accepted', onAccepted);

    await interactivePool.spawn({ ...baseConfig, workspace });
    await interactivePool.send('member-000', { type: 'human', content: '/compact' });
    const queued = interactivePool.send('member-000', { type: 'human', content: 'after compact' });
    await new Promise((resolve) => setTimeout(resolve, 50));
    await queued;
    appendFileSync(sessionFile, `${JSON.stringify({
      type: 'user',
      timestamp: new Date().toISOString(),
      message: { role: 'user', content: 'after compact' },
    })}\n`);
    await new Promise((resolve) => setTimeout(resolve, 650));

    const writes = writeSpy.mock.calls.map((call) => call[0]?.toString());
    // /compact is a slash command → sent directly through PTY
    // "after compact" is short (13 bytes) → also sent directly through PTY
    const allWritten = writes.join('');
    expect(allWritten).toContain('/compact');
    expect(allWritten).toContain('after compact');
    expect(allWritten).toContain('\r');
    expect(proc.kill).not.toHaveBeenCalled();
    expect(onAccepted).toHaveBeenCalledWith('member-000');
    expect(onAccepted).toHaveBeenCalledTimes(2);
    interactivePool.dispose();
    rmSync(workspace, { recursive: true, force: true });
    rmSync(dirname(sessionFile), { recursive: true, force: true });
  });

  it('accepts interactive /wrap without jsonl echo and waits for assistant completion', async () => {
    const proc = createMockProcess();
    const writeSpy = vi.spyOn(proc.stdin, 'write');
    mockSpawn.mockReturnValue(proc);
    const workspace = mkdtempSync(join(tmpdir(), 'relay-pool-interactive-wrap-'));
    const sessionFile = getBareSessionFilePath(baseConfig.sessionId);
    mkdirSync(dirname(sessionFile), { recursive: true });
    const interactivePool = createPool({
      cleanupIntervalMs: 999999,
      ccMode: 'interactive',
      interactiveWarmupMs: 0,
      interactiveSubmitDelayMs: 0,
      interactiveAcceptTimeoutMs: 10,
      interactiveSlashCommandSettleMs: 10,
      killTimeoutMs: 1,
    });
    const onAccepted = vi.fn();
    const onResponse = vi.fn();
    interactivePool.on('turn-accepted', onAccepted);
    interactivePool.on('response', onResponse);

    await interactivePool.spawn({ ...baseConfig, workspace });
    await interactivePool.send('member-000', { type: 'human', content: '/wrap' });
    await new Promise((resolve) => setTimeout(resolve, 50));

    expect(proc.kill).not.toHaveBeenCalled();
    expect(onAccepted).toHaveBeenCalledWith('member-000');
    expect(interactivePool.get('member-000')?.state).toBe('busy');

    appendFileSync(sessionFile, `${JSON.stringify({
      type: 'assistant',
      timestamp: new Date().toISOString(),
      message: {
        role: 'assistant',
        stop_reason: 'end_turn',
        content: [{ type: 'text', text: 'wrap complete' }],
      },
    })}\n`);
    await new Promise((resolve) => setTimeout(resolve, 350));

    const writes = writeSpy.mock.calls.map((call) => call[0]?.toString());
    // /wrap is a slash command → sent directly through PTY
    const allWritten = writes.join('');
    expect(allWritten).toContain('/wrap');
    expect(allWritten).toContain('\r');
    expect(onResponse).toHaveBeenCalledWith('member-000', expect.objectContaining({
      type: 'assistant',
      content: 'wrap complete',
    }));
    expect(onResponse).toHaveBeenCalledWith('member-000', expect.objectContaining({ type: 'result' }));
    expect(interactivePool.get('member-000')?.state).toBe('idle');
    interactivePool.dispose();
    rmSync(workspace, { recursive: true, force: true });
    rmSync(dirname(sessionFile), { recursive: true, force: true });
  });

  it('emits turn-accepted after interactive user turn is written to session jsonl', async () => {
    const proc = createMockProcess();
    mockSpawn.mockReturnValue(proc);
    const workspace = mkdtempSync(join(tmpdir(), 'relay-pool-interactive-'));
    const sessionFile = getBareSessionFilePath(baseConfig.sessionId);
    mkdirSync(dirname(sessionFile), { recursive: true });
    const interactivePool = createPool({
      cleanupIntervalMs: 999999,
      ccMode: 'interactive',
      interactiveWarmupMs: 0,
      interactiveSubmitDelayMs: 0,
      interactiveAcceptTimeoutMs: 1000,
    });
    const onAccepted = vi.fn();
    interactivePool.on('turn-accepted', onAccepted);

    await interactivePool.spawn({ ...baseConfig, workspace });
    await interactivePool.send('member-000', { type: 'human', content: 'hello' });
    await new Promise((resolve) => setTimeout(resolve, 20));
    appendFileSync(sessionFile, `${JSON.stringify({
      type: 'user',
      timestamp: new Date().toISOString(),
      message: { role: 'user', content: 'hello' },
    })}\n`);
    await new Promise((resolve) => setTimeout(resolve, 650));

    expect(onAccepted).toHaveBeenCalledWith('member-000');
    interactivePool.dispose();
    rmSync(workspace, { recursive: true, force: true });
    rmSync(dirname(sessionFile), { recursive: true, force: true });
  });

  it('interactive watcher prefers workspace-encoded jsonl path; relocates to -root legacy', async () => {
    const proc = createMockProcess();
    mockSpawn.mockReturnValue(proc);
    const root = mkdtempSync(join(tmpdir(), 'relay-pool-interactive-unicode-'));
    const workspace = join(root, '精进领域技能', '写作', 'marsh-space');
    mkdirSync(workspace, { recursive: true });
    const sessionFile = getBareSessionFilePath(baseConfig.sessionId);
    const workspaceSessionFile = getSessionFilePath(workspace, baseConfig.sessionId);
    expect(workspaceSessionFile).toContain('marsh-space');
    expect(sessionFile.endsWith('-root/test-session-uuid.jsonl')).toBe(true);
    mkdirSync(dirname(sessionFile), { recursive: true });
    const interactivePool = createPool({
      cleanupIntervalMs: 999999,
      ccMode: 'interactive',
      interactiveWarmupMs: 0,
      interactiveSubmitDelayMs: 0,
      interactiveAcceptTimeoutMs: 1000,
    });
    const onAccepted = vi.fn();
    interactivePool.on('turn-accepted', onAccepted);

    await interactivePool.spawn({ ...baseConfig, workspace });
    await interactivePool.send('member-000', { type: 'human', content: 'hello' });
    await new Promise((resolve) => setTimeout(resolve, 20));
    appendFileSync(sessionFile, `${JSON.stringify({
      type: 'user',
      timestamp: new Date().toISOString(),
      message: { role: 'user', content: 'hello' },
    })}\n`);
    await new Promise((resolve) => setTimeout(resolve, 650));

    expect(onAccepted).toHaveBeenCalledWith('member-000');
    interactivePool.dispose();
    rmSync(root, { recursive: true, force: true });
    rmSync(dirname(sessionFile), { recursive: true, force: true });
  });

  it('rescues interactive accept when jsonl evidence lands before the watchdog poll', async () => {
    const proc = createMockProcess();
    mockSpawn.mockReturnValue(proc);
    const workspace = mkdtempSync(join(tmpdir(), 'relay-pool-interactive-accept-rescue-'));
    const sessionFile = getBareSessionFilePath(baseConfig.sessionId);
    mkdirSync(dirname(sessionFile), { recursive: true });
    const interactivePool = createPool({
      cleanupIntervalMs: 999999,
      ccMode: 'interactive',
      interactiveWarmupMs: 0,
      interactiveSubmitDelayMs: 0,
      interactiveAcceptTimeoutMs: 10,
      killTimeoutMs: 1,
    });
    const onAccepted = vi.fn();
    interactivePool.on('turn-accepted', onAccepted);

    await interactivePool.spawn({ ...baseConfig, workspace });
    await interactivePool.send('member-000', { type: 'human', content: 'hello' });
    appendFileSync(sessionFile, `${JSON.stringify({
      type: 'user',
      timestamp: new Date().toISOString(),
      message: { role: 'user', content: 'hello' },
    })}\n`);
    await new Promise((resolve) => setTimeout(resolve, 80));

    expect(onAccepted).toHaveBeenCalledWith('member-000');
    expect(proc.kill).not.toHaveBeenCalled();
    interactivePool.dispose();
    rmSync(workspace, { recursive: true, force: true });
    rmSync(dirname(sessionFile), { recursive: true, force: true });
  });

  it('replays jsonl evidence after watchdog rescue so completed turns are emitted', async () => {
    const proc = createMockProcess();
    mockSpawn.mockReturnValue(proc);
    const workspace = mkdtempSync(join(tmpdir(), 'relay-pool-interactive-accept-replay-'));
    const sessionFile = getBareSessionFilePath(baseConfig.sessionId);
    mkdirSync(dirname(sessionFile), { recursive: true });
    const interactivePool = createPool({
      cleanupIntervalMs: 999999,
      ccMode: 'interactive',
      interactiveWarmupMs: 0,
      interactiveSubmitDelayMs: 0,
      interactiveAcceptTimeoutMs: 10,
      killTimeoutMs: 1,
    });
    const onAccepted = vi.fn();
    const onResponse = vi.fn();
    interactivePool.on('turn-accepted', onAccepted);
    interactivePool.on('response', onResponse);

    await interactivePool.spawn({ ...baseConfig, workspace });
    await interactivePool.send('member-000', { type: 'human', content: 'hello' });
    appendFileSync(sessionFile, `${JSON.stringify({
      type: 'user',
      timestamp: new Date().toISOString(),
      message: { role: 'user', content: 'hello' },
    })}\n`);
    appendFileSync(sessionFile, `${JSON.stringify({
      type: 'assistant',
      timestamp: new Date().toISOString(),
      message: {
        role: 'assistant',
        stop_reason: 'end_turn',
        content: [{ type: 'text', text: 'hi back' }],
      },
    })}\n`);
    await new Promise((resolve) => setTimeout(resolve, 80));

    expect(onAccepted).toHaveBeenCalledWith('member-000');
    expect(onResponse).toHaveBeenCalledWith('member-000', expect.objectContaining({ content: 'hi back' }));
    expect(interactivePool.get('member-000')?.state).toBe('idle');
    expect(proc.kill).not.toHaveBeenCalled();
    interactivePool.dispose();
    rmSync(workspace, { recursive: true, force: true });
    rmSync(dirname(sessionFile), { recursive: true, force: true });
  });

  it('finishes interactive turn on end_turn even when there is no assistant text (tool-only)', async () => {
    const proc = createMockProcess();
    mockSpawn.mockReturnValue(proc);
    const workspace = mkdtempSync(join(tmpdir(), 'relay-pool-interactive-tool-only-'));
    const sessionFile = getBareSessionFilePath(baseConfig.sessionId);
    mkdirSync(dirname(sessionFile), { recursive: true });
    const interactivePool = createPool({
      cleanupIntervalMs: 999999,
      ccMode: 'interactive',
      interactiveWarmupMs: 0,
      interactiveSubmitDelayMs: 0,
      interactiveAcceptTimeoutMs: 10,
      killTimeoutMs: 1,
    });
    const onResponse = vi.fn();
    interactivePool.on('response', onResponse);
    const previousReceiptDir = process.env['PULSE_COMPUTECOST_RECEIPT_DIR'];
    process.env['PULSE_COMPUTECOST_RECEIPT_DIR'] = '/tmp/relay-test-receipts';

    await interactivePool.spawn({ ...baseConfig, workspace });
    await interactivePool.send('member-000', { type: 'human', content: 'hello' }, {correlationId:'A',idempotencyKey:'A',effect:'no-effect',hostAttemptRef:'attempt-A'});
    // finishTurn(A) drains B synchronously; it must retain B's context.
    const queuedB = interactivePool.send('member-000', { type: 'human', content: 'next' }, {correlationId:'B',idempotencyKey:'B',effect:'no-effect',hostAttemptRef:'attempt-B'});
    appendFileSync(sessionFile, `${JSON.stringify({
      type: 'user',
      timestamp: new Date().toISOString(),
      message: { role: 'user', content: 'hello' },
    })}\n`);
    appendFileSync(sessionFile, `${JSON.stringify({
      type: 'assistant',
      timestamp: new Date().toISOString(),
      message: {
        role: 'assistant',
        stop_reason: 'end_turn',
        content: [{ type: 'tool_use', id: 'toolu_test', name: 'Bash', input: { command: 'true' } }],
      },
    })}\n`);
    await new Promise((resolve) => setTimeout(resolve, 80));
    await queuedB;
    appendFileSync(sessionFile, `${JSON.stringify({type:'assistant',timestamp:new Date().toISOString(),message:{role:'assistant',stop_reason:'end_turn',id:'b-tool-only',model:'test',usage:{input_tokens:1},content:[{type:'tool_use',id:'toolu_b',name:'Bash',input:{command:'true'}}]}})}\n`);
    await new Promise((resolve) => setTimeout(resolve, 80));

    expect(onResponse).toHaveBeenCalledWith('member-000', expect.objectContaining({ type: 'result' }));
    expect(onResponse).toHaveBeenCalledWith('member-000', expect.objectContaining({type:'usage_observation',host_attempt_ref:'attempt-B'}));
    expect(interactivePool.get('member-000')?.state).toBe('idle');
    expect(proc.kill).not.toHaveBeenCalled();
    interactivePool.dispose();
    if (previousReceiptDir === undefined) delete process.env['PULSE_COMPUTECOST_RECEIPT_DIR']; else process.env['PULSE_COMPUTECOST_RECEIPT_DIR'] = previousReceiptDir;
    rmSync(workspace, { recursive: true, force: true });
    rmSync(dirname(sessionFile), { recursive: true, force: true });
  });

  // bug-20260509-002: some providers split thinking & text into two independent
  // assistant records, each with stop_reason 'end_turn'. Pool must NOT close
  // the turn on the first (thinking-only) record — otherwise the follow-up
  // text record arrives after watcher tracking is cleared and is dropped.
  it('defers interactive finishTurn on a thinking-only end_turn record (split response)', async () => {
    const proc = createMockProcess();
    mockSpawn.mockReturnValue(proc);
    const workspace = mkdtempSync(join(tmpdir(), 'relay-pool-interactive-thinking-only-'));
    const sessionFile = getBareSessionFilePath(baseConfig.sessionId);
    mkdirSync(dirname(sessionFile), { recursive: true });
    const interactivePool = createPool({
      cleanupIntervalMs: 999999,
      ccMode: 'interactive',
      interactiveWarmupMs: 0,
      interactiveSubmitDelayMs: 0,
      interactiveAcceptTimeoutMs: 1000,
      killTimeoutMs: 1,
    });
    const onResponse = vi.fn();
    interactivePool.on('response', onResponse);

    await interactivePool.spawn({ ...baseConfig, workspace });
    await interactivePool.send('member-000', { type: 'human', content: 'who are you' });

    appendFileSync(sessionFile, `${JSON.stringify({
      type: 'user',
      timestamp: new Date().toISOString(),
      message: { role: 'user', content: 'who are you' },
    })}\n`);
    appendFileSync(sessionFile, `${JSON.stringify({
      type: 'assistant',
      timestamp: new Date().toISOString(),
      message: {
        role: 'assistant',
        stop_reason: 'end_turn',
        content: [{ type: 'thinking', thinking: 'The user is asking who I am ...' }],
      },
    })}\n`);
    await new Promise((resolve) => setTimeout(resolve, 400));

    expect(onResponse).not.toHaveBeenCalled();
    expect(interactivePool.get('member-000')?.state).toBe('busy');

    appendFileSync(sessionFile, `${JSON.stringify({
      type: 'assistant',
      timestamp: new Date().toISOString(),
      message: {
        role: 'assistant',
        stop_reason: 'end_turn',
        content: [{ type: 'text', text: 'I am Claude Code.' }],
      },
    })}\n`);
    await new Promise((resolve) => setTimeout(resolve, 400));

    expect(onResponse).toHaveBeenCalledWith(
      'member-000',
      expect.objectContaining({ content: 'I am Claude Code.' }),
    );
    expect(interactivePool.get('member-000')?.state).toBe('idle');
    expect(proc.kill).not.toHaveBeenCalled();
    interactivePool.dispose();
    rmSync(workspace, { recursive: true, force: true });
    rmSync(dirname(sessionFile), { recursive: true, force: true });
  });

  // Regression guard: Anthropic-native records carry thinking + text together
  // in a single record's content array. That must still finish the turn in one
  // shot (the new defer only triggers when blocks are *all* thinking).
  it('finishes interactive turn when thinking and text share a single end_turn record', async () => {
    const proc = createMockProcess();
    mockSpawn.mockReturnValue(proc);
    const workspace = mkdtempSync(join(tmpdir(), 'relay-pool-interactive-combined-'));
    const sessionFile = getBareSessionFilePath(baseConfig.sessionId);
    mkdirSync(dirname(sessionFile), { recursive: true });
    const interactivePool = createPool({
      cleanupIntervalMs: 999999,
      ccMode: 'interactive',
      interactiveWarmupMs: 0,
      interactiveSubmitDelayMs: 0,
      interactiveAcceptTimeoutMs: 10,
      killTimeoutMs: 1,
    });
    const onResponse = vi.fn();
    interactivePool.on('response', onResponse);

    await interactivePool.spawn({ ...baseConfig, workspace });
    await interactivePool.send('member-000', { type: 'human', content: 'hello' });
    appendFileSync(sessionFile, `${JSON.stringify({
      type: 'user',
      timestamp: new Date().toISOString(),
      message: { role: 'user', content: 'hello' },
    })}\n`);
    appendFileSync(sessionFile, `${JSON.stringify({
      type: 'assistant',
      timestamp: new Date().toISOString(),
      message: {
        role: 'assistant',
        stop_reason: 'end_turn',
        content: [
          { type: 'thinking', thinking: 'short reflection' },
          { type: 'text', text: 'hi back' },
        ],
      },
    })}\n`);
    await new Promise((resolve) => setTimeout(resolve, 400));

    expect(onResponse).toHaveBeenCalledWith(
      'member-000',
      expect.objectContaining({ content: 'hi back' }),
    );
    expect(interactivePool.get('member-000')?.state).toBe('idle');
    interactivePool.dispose();
    rmSync(workspace, { recursive: true, force: true });
    rmSync(dirname(sessionFile), { recursive: true, force: true });
  });

  // Regression guard: empty content end_turn still finishes the turn so the
  // pool never hangs in busy state waiting for a record that will never come.
  it('finishes interactive turn on end_turn even when content array is empty', async () => {
    const proc = createMockProcess();
    mockSpawn.mockReturnValue(proc);
    const workspace = mkdtempSync(join(tmpdir(), 'relay-pool-interactive-empty-'));
    const sessionFile = getBareSessionFilePath(baseConfig.sessionId);
    mkdirSync(dirname(sessionFile), { recursive: true });
    const interactivePool = createPool({
      cleanupIntervalMs: 999999,
      ccMode: 'interactive',
      interactiveWarmupMs: 0,
      interactiveSubmitDelayMs: 0,
      interactiveAcceptTimeoutMs: 1000,
      killTimeoutMs: 1,
    });
    const onResponse = vi.fn();
    interactivePool.on('response', onResponse);

    await interactivePool.spawn({ ...baseConfig, workspace });
    await interactivePool.send('member-000', { type: 'human', content: 'hello' });
    appendFileSync(sessionFile, `${JSON.stringify({
      type: 'user',
      timestamp: new Date().toISOString(),
      message: { role: 'user', content: 'hello' },
    })}\n`);
    appendFileSync(sessionFile, `${JSON.stringify({
      type: 'assistant',
      timestamp: new Date().toISOString(),
      message: {
        role: 'assistant',
        stop_reason: 'end_turn',
        content: [
          { type: 'thinking', thinking: 'short reflection' },
          { type: 'text', text: 'hi back' },
        ],
      },
    })}\n`);
    await new Promise((resolve) => setTimeout(resolve, 400));

    expect(onResponse).toHaveBeenCalledWith(
      'member-000',
      expect.objectContaining({ content: 'hi back' }),
    );
    expect(interactivePool.get('member-000')?.state).toBe('idle');
    interactivePool.dispose();
    rmSync(workspace, { recursive: true, force: true });
    rmSync(dirname(sessionFile), { recursive: true, force: true });
  });

  // Regression guard: empty content end_turn still finishes the turn so the
  // pool never hangs in busy state waiting for a record that will never come.
  it('finishes interactive turn on end_turn even when content array is empty', async () => {
    const proc = createMockProcess();
    mockSpawn.mockReturnValue(proc);
    const workspace = mkdtempSync(join(tmpdir(), 'relay-pool-interactive-empty-'));
    const sessionFile = getBareSessionFilePath(baseConfig.sessionId);
    mkdirSync(dirname(sessionFile), { recursive: true });
    const interactivePool = createPool({
      cleanupIntervalMs: 999999,
      ccMode: 'interactive',
      interactiveWarmupMs: 0,
      interactiveSubmitDelayMs: 0,
      interactiveAcceptTimeoutMs: 1000,
      killTimeoutMs: 1,
    });
    const onResponse = vi.fn();
    interactivePool.on('response', onResponse);

    await interactivePool.spawn({ ...baseConfig, workspace });
    await interactivePool.send('member-000', { type: 'human', content: 'hello' });
    appendFileSync(sessionFile, `${JSON.stringify({
      type: 'user',
      timestamp: new Date().toISOString(),
      message: { role: 'user', content: 'hello' },
    })}\n`);
    appendFileSync(sessionFile, `${JSON.stringify({
      type: 'assistant',
      timestamp: new Date().toISOString(),
      message: { role: 'assistant', stop_reason: 'end_turn', content: [] },
    })}\n`);
    await new Promise((resolve) => setTimeout(resolve, 400));

    expect(onResponse).toHaveBeenCalledWith('member-000', expect.objectContaining({ type: 'result' }));
    expect(interactivePool.get('member-000')?.state).toBe('idle');
    interactivePool.dispose();
    rmSync(workspace, { recursive: true, force: true });
    rmSync(dirname(sessionFile), { recursive: true, force: true });
  });

  it('emits busy-progress instead of killing when interactive prompt is not accepted', async () => {
    const proc1 = createMockProcess(3001);
    mockSpawn.mockReturnValue(proc1);
    const workspace = mkdtempSync(join(tmpdir(), 'relay-pool-interactive-replay-'));
    const sessionFile = getBareSessionFilePath(baseConfig.sessionId);
    mkdirSync(dirname(sessionFile), { recursive: true });
    const interactivePool = createPool({
      cleanupIntervalMs: 999999,
      ccMode: 'interactive',
      interactiveWarmupMs: 0,
      interactiveSubmitDelayMs: 0,
      interactiveAcceptTimeoutMs: 10,
      maxInteractiveAcceptRetriesPerMessage: 1,
      maxBusyChecks: 10,
      killTimeoutMs: 1,
    });

    const onBusyProgress = vi.fn();
    const onSessionEnd = vi.fn();
    interactivePool.on('busy-progress', onBusyProgress);
    interactivePool.on('session-end', onSessionEnd);

    await interactivePool.spawn({ ...baseConfig, workspace });
    await interactivePool.send('member-000', { type: 'human', content: 'hello' });

    // Wait for the accept timer to fire and emit busy-progress
    await new Promise((resolve) => setTimeout(resolve, 50));

    // Should emit busy-progress, NOT kill the process or emit session-end
    expect(onBusyProgress).toHaveBeenCalledWith('member-000', expect.objectContaining({
      checkCount: 1,
      isFinalCheck: false,
    }));
    expect(proc1.kill).not.toHaveBeenCalledWith('SIGTERM');
    expect(onSessionEnd).not.toHaveBeenCalled();
    interactivePool.dispose();
    rmSync(workspace, { recursive: true, force: true });
    rmSync(dirname(sessionFile), { recursive: true, force: true });
  });

  it('ignores synthetic assistant noise before interactive user turn is accepted', async () => {
    const proc = createMockProcess();
    mockSpawn.mockReturnValue(proc);
    const workspace = mkdtempSync(join(tmpdir(), 'relay-pool-interactive-noise-'));
    const sessionFile = getBareSessionFilePath(baseConfig.sessionId);
    mkdirSync(dirname(sessionFile), { recursive: true });
    const interactivePool = createPool({
      cleanupIntervalMs: 999999,
      ccMode: 'interactive',
      interactiveWarmupMs: 0,
      interactiveSubmitDelayMs: 0,
      interactiveAcceptTimeoutMs: 1000,
    });
    const onAccepted = vi.fn();
    const onResponse = vi.fn();
    interactivePool.on('turn-accepted', onAccepted);
    interactivePool.on('response', onResponse);

    await interactivePool.spawn({ ...baseConfig, workspace });
    await interactivePool.send('member-000', { type: 'human', content: 'hello' });
    await new Promise((resolve) => setTimeout(resolve, 20));
    appendFileSync(sessionFile, `${JSON.stringify({
      type: 'assistant',
      timestamp: new Date().toISOString(),
      message: {
        role: 'assistant',
        stop_reason: 'stop_sequence',
        content: [{ type: 'text', text: 'No response requested.' }],
      },
    })}\n`);
    await new Promise((resolve) => setTimeout(resolve, 350));

    expect(onAccepted).not.toHaveBeenCalled();
    expect(onResponse).not.toHaveBeenCalled();
    interactivePool.dispose();
    rmSync(workspace, { recursive: true, force: true });
    rmSync(dirname(sessionFile), { recursive: true, force: true });
  });

  it('accepts interactive turn when assistant progress appears before user echo', async () => {
    const proc = createMockProcess();
    mockSpawn.mockReturnValue(proc);
    const workspace = mkdtempSync(join(tmpdir(), 'relay-pool-interactive-assistant-progress-'));
    const sessionFile = getBareSessionFilePath(baseConfig.sessionId);
    mkdirSync(dirname(sessionFile), { recursive: true });
    const interactivePool = createPool({
      cleanupIntervalMs: 999999,
      ccMode: 'interactive',
      interactiveWarmupMs: 0,
      interactiveSubmitDelayMs: 0,
      interactiveAcceptTimeoutMs: 1000,
    });
    const onAccepted = vi.fn();
    const onResponse = vi.fn();
    interactivePool.on('turn-accepted', onAccepted);
    interactivePool.on('response', onResponse);

    await interactivePool.spawn({ ...baseConfig, workspace });
    await interactivePool.send('member-000', { type: 'human', content: 'hello' });
    await new Promise((resolve) => setTimeout(resolve, 20));
    appendFileSync(sessionFile, `${JSON.stringify({
      type: 'assistant',
      timestamp: new Date().toISOString(),
      message: {
        role: 'assistant',
        stop_reason: null,
        content: [{ type: 'text', text: 'working on it' }],
      },
    })}\n`);
    await new Promise((resolve) => setTimeout(resolve, 350));

    expect(onAccepted).toHaveBeenCalledWith('member-000');
    // Intermediate assistant records (stop_reason=null) are not emitted to channels —
    // only end_turn/stop_sequence records are forwarded to avoid surfacing reasoning monologues.
    expect(onResponse).not.toHaveBeenCalled();
    expect(proc.kill).not.toHaveBeenCalled();
    interactivePool.dispose();
    rmSync(workspace, { recursive: true, force: true });
    rmSync(dirname(sessionFile), { recursive: true, force: true });
  });

  it('updates lastActivity timestamp', async () => {
    const proc = createMockProcess();
    mockSpawn.mockReturnValue(proc);

    const spawnPromise = pool.spawn(baseConfig);
    proc.stdout.write('{"type":"system","content":"ready"}\n');
    await spawnPromise;

    const before = pool.get('member-000')!.lastActivity;

    // Small delay to ensure timestamp differs
    await new Promise((resolve) => setTimeout(resolve, 10));
    await pool.send('member-000', { type: 'human', content: 'test' });

    const after = pool.get('member-000')!.lastActivity;
    expect(after).toBeGreaterThanOrEqual(before);
  });

  it('throws when process is not ready or idle', async () => {
    await expect(pool.send('nonexistent', { type: 'human', content: 'hi' }))
      .rejects.toThrow();
  });

  it('transitions state busy → idle when result message received', async () => {
    const proc = createMockProcess();
    mockSpawn.mockReturnValue(proc);

    const spawnPromise = pool.spawn(baseConfig);
    proc.stdout.write('{"type":"system","content":"ready"}\n');
    await spawnPromise;

    await pool.send('member-000', { type: 'human', content: 'hello' });
    expect(pool.get('member-000')?.state).toBe('busy');

    // CC responds with result
    proc.stdout.write('{"type":"result","total_cost_usd":0.01}\n');
    await new Promise((resolve) => setTimeout(resolve, 50));

    expect(pool.get('member-000')?.state).toBe('idle');
  });

  it('aborts busy process on system/api_retry 429 so Relay can own replay', async () => {
    const proc = createMockProcess();
    mockSpawn.mockReturnValue(proc);
    const poolWithFastAbort = createPool({ cleanupIntervalMs: 999999, rateLimitAbortTimeoutMs: 1, ccMode: 'print' });

    const spawnPromise = poolWithFastAbort.spawn(baseConfig);
    proc.stdout.write('{"type":"system","content":"ready"}\n');
    await spawnPromise;

    await poolWithFastAbort.send('member-000', { type: 'human', content: 'hello' });
    expect(poolWithFastAbort.get('member-000')?.state).toBe('busy');

    proc.stdout.write('{"type":"system","subtype":"api_retry","error_status":429,"error":"rate_limit"}\n');
    await new Promise((resolve) => setTimeout(resolve, 80));

    expect(proc.kill).toHaveBeenCalledWith('SIGKILL');
    poolWithFastAbort.dispose();
  });

  it('aborts busy process on legacy rate_limit_event message', async () => {
    const proc = createMockProcess();
    mockSpawn.mockReturnValue(proc);
    const poolWithFastAbort = createPool({ cleanupIntervalMs: 999999, rateLimitAbortTimeoutMs: 1, ccMode: 'print' });

    const spawnPromise = poolWithFastAbort.spawn(baseConfig);
    proc.stdout.write('{"type":"system","content":"ready"}\n');
    await spawnPromise;

    await poolWithFastAbort.send('member-000', { type: 'human', content: 'hello' });
    proc.stdout.write('{"type":"rate_limit_event"}\n');
    await new Promise((resolve) => setTimeout(resolve, 80));

    expect(proc.kill).toHaveBeenCalledWith('SIGKILL');
    poolWithFastAbort.dispose();
  });

  it('does not abort busy process on allowed rate_limit_event message', async () => {
    const proc = createMockProcess();
    mockSpawn.mockReturnValue(proc);
    const poolWithFastAbort = createPool({ cleanupIntervalMs: 999999, rateLimitAbortTimeoutMs: 1, ccMode: 'print' });

    const spawnPromise = poolWithFastAbort.spawn(baseConfig);
    proc.stdout.write('{"type":"system","content":"ready"}\n');
    await spawnPromise;

    await poolWithFastAbort.send('member-000', { type: 'human', content: 'hello' });
    proc.stdout.write('{"type":"rate_limit_event","rate_limit_info":{"status":"allowed","rateLimitType":"five_hour"}}\n');
    await new Promise((resolve) => setTimeout(resolve, 80));

    expect(proc.kill).not.toHaveBeenCalledWith('SIGKILL');
    expect(poolWithFastAbort.get('member-000')?.state).toBe('busy');

    proc.stdout.write('{"type":"result","total_cost_usd":0.01}\n');
    await new Promise((resolve) => setTimeout(resolve, 50));
    expect(poolWithFastAbort.get('member-000')?.state).toBe('idle');
    poolWithFastAbort.dispose();
  });

  it('does not abort busy process on allowed_warning rate_limit_event (usage heads-up, not 429)', async () => {
    // 2026-06-25 regression: Anthropic sends status="allowed_warning" when 7-day
    // utilization crosses 0.75 — the request STILL passes. The old `!== 'allowed'`
    // check mis-classified this as a 429, looping amanda/aki/duke into SIGKILL→respawn.
    const proc = createMockProcess();
    mockSpawn.mockReturnValue(proc);
    const poolWithFastAbort = createPool({ cleanupIntervalMs: 999999, rateLimitAbortTimeoutMs: 1, ccMode: 'print' });

    const spawnPromise = poolWithFastAbort.spawn(baseConfig);
    proc.stdout.write('{"type":"system","content":"ready"}\n');
    await spawnPromise;

    await poolWithFastAbort.send('member-000', { type: 'human', content: 'hello' });
    proc.stdout.write('{"type":"rate_limit_event","rate_limit_info":{"status":"allowed_warning","rateLimitType":"seven_day","utilization":0.96,"surpassedThreshold":0.75}}\n');
    await new Promise((resolve) => setTimeout(resolve, 80));

    expect(proc.kill).not.toHaveBeenCalledWith('SIGKILL');
    expect(poolWithFastAbort.get('member-000')?.state).toBe('busy');

    proc.stdout.write('{"type":"result","total_cost_usd":0.01}\n');
    await new Promise((resolve) => setTimeout(resolve, 50));
    expect(poolWithFastAbort.get('member-000')?.state).toBe('idle');
    poolWithFastAbort.dispose();
  });

  // 2026-06-25: amanda/aki/duke stuck in endless SIGKILL→respawn loop because
  // Anthropic emits status="allowed_warning" (request PASSED, just a 7-day usage
  // heads-up at utilization>0.75) and the old `status !== 'allowed'` check
  // mis-classified it as a 429. Any "allowed*" status must be treated as a pass.
  it('does not abort busy process on allowed_warning rate_limit_event message', async () => {
    const proc = createMockProcess();
    mockSpawn.mockReturnValue(proc);
    const poolWithFastAbort = createPool({ cleanupIntervalMs: 999999, rateLimitAbortTimeoutMs: 1, ccMode: 'print' });

    const spawnPromise = poolWithFastAbort.spawn(baseConfig);
    proc.stdout.write('{"type":"system","content":"ready"}\n');
    await spawnPromise;

    await poolWithFastAbort.send('member-000', { type: 'human', content: 'hello' });
    proc.stdout.write('{"type":"rate_limit_event","rate_limit_info":{"status":"allowed_warning","rateLimitType":"seven_day","utilization":0.96,"surpassedThreshold":0.75}}\n');
    await new Promise((resolve) => setTimeout(resolve, 80));

    expect(proc.kill).not.toHaveBeenCalledWith('SIGKILL');
    expect(poolWithFastAbort.get('member-000')?.state).toBe('busy');

    proc.stdout.write('{"type":"result","total_cost_usd":0.01}\n');
    await new Promise((resolve) => setTimeout(resolve, 50));
    expect(poolWithFastAbort.get('member-000')?.state).toBe('idle');
    poolWithFastAbort.dispose();
  });

  it('emits busy-progress instead of auto-killing after busy timeout', async () => {
    const proc = createMockProcess();
    mockSpawn.mockReturnValue(proc);
    const timeoutPool = createPool({ cleanupIntervalMs: 999999, busyTimeoutMs: 5, killTimeoutMs: 1, ccMode: 'print' });
    const onBusyProgress = vi.fn();
    const onSessionEnd = vi.fn();
    timeoutPool.on('busy-progress', onBusyProgress);
    timeoutPool.on('session-end', onSessionEnd);

    const spawnPromise = timeoutPool.spawn(baseConfig);
    proc.stdout.write('{"type":"system","content":"ready"}\n');
    await spawnPromise;

    await timeoutPool.send('member-000', { type: 'human', content: 'hello' });
    await new Promise((resolve) => setTimeout(resolve, 30));

    // Should emit busy-progress, NOT kill the process or emit session-end(timeout)
    expect(onBusyProgress).toHaveBeenCalledWith('member-000', expect.objectContaining({
      checkCount: 1,
      maxChecks: 10,
      isFinalCheck: false,
    }));
    expect(proc.kill).not.toHaveBeenCalledWith('SIGTERM');
    expect(onSessionEnd).not.toHaveBeenCalled();
    timeoutPool.dispose();
  });

  it('re-arms interactive accept timer while CC stdout is active', async () => {
    const proc = createMockProcess();
    mockSpawn.mockReturnValue(proc);
    const workspace = mkdtempSync(join(tmpdir(), 'relay-pool-accept-stdout-keepalive-'));
    const interactivePool = createPool({
      cleanupIntervalMs: 999999,
      ccMode: 'interactive',
      interactiveWarmupMs: 0,
      interactiveSubmitDelayMs: 0,
      interactiveAcceptTimeoutMs: 50,   // short initial timeout
      killTimeoutMs: 1,
    });
    const onSessionEnd = vi.fn();
    interactivePool.on('session-end', onSessionEnd);

    await interactivePool.spawn({ ...baseConfig, workspace });
    await interactivePool.send('member-000', { type: 'human', content: 'long task' });

    // Simulate CC producing PTY stdout every 20ms — accept timer should keep re-arming.
    const stdoutInterval = setInterval(() => {
      proc.stdout.write(Buffer.from('working...\n'));
    }, 20);

    // After 200ms (well past the 50ms timeout), CC should NOT have been killed.
    await new Promise((resolve) => setTimeout(resolve, 200));
    clearInterval(stdoutInterval);

    expect(proc.kill).not.toHaveBeenCalled();
    expect(onSessionEnd).not.toHaveBeenCalled();
    interactivePool.dispose();
    rmSync(workspace, { recursive: true, force: true });
  });

  it('emits busy-progress when interactive accept times out after CC stdout goes idle', async () => {
    const proc = createMockProcess();
    mockSpawn.mockReturnValue(proc);
    const workspace = mkdtempSync(join(tmpdir(), 'relay-pool-accept-stdout-timeout-'));
    const interactivePool = createPool({
      cleanupIntervalMs: 999999,
      ccMode: 'interactive',
      interactiveWarmupMs: 0,
      interactiveSubmitDelayMs: 0,
      interactiveAcceptTimeoutMs: 50,
      interactiveAcceptStdoutIdleMs: 50,
      maxInteractiveAcceptRetriesPerMessage: 0,
      maxBusyChecks: 10,
      killTimeoutMs: 1,
    });
    const onBusyProgress = vi.fn();
    const onSessionEnd = vi.fn();
    interactivePool.on('busy-progress', onBusyProgress);
    interactivePool.on('session-end', onSessionEnd);

    await interactivePool.spawn({ ...baseConfig, workspace });
    await interactivePool.send('member-000', { type: 'human', content: 'task that stalls' });

    // Emit stdout for a while to keep the timer re-arming...
    const stdoutInterval = setInterval(() => {
      proc.stdout.write(Buffer.from('working...\n'));
    }, 20);

    await new Promise((resolve) => setTimeout(resolve, 120));

    // ...then stop emitting. The re-armed timer should eventually fire.
    clearInterval(stdoutInterval);

    // Wait for the re-armed idle window (50ms) + busy-progress emission.
    await new Promise((resolve) => setTimeout(resolve, 150));

    // Should emit busy-progress, NOT kill the process or emit session-end
    expect(onBusyProgress).toHaveBeenCalled();
    expect(onSessionEnd).not.toHaveBeenCalled();
    interactivePool.dispose();
    rmSync(workspace, { recursive: true, force: true });
  });
});

// bug-20260615-001: CC can emit end_turn after dispatching a background
// subagent, flipping Relay's state to 'idle' while the subagent is still
// running. The watcher keeps reading new JSONL records, but handleInteractiveRecord
// ignores them (prompt-acceptance cleared by finishTurn). Without refreshing
// lastActivity on JSONL activity, cleanupIdleProcesses reclaims the process
// idleTimeoutMs after the premature end_turn — killing the in-flight subagent.
describe('pool idle reclamation vs JSONL activity', () => {
  beforeEach(() => {
    mockSpawn = vi.fn();
  });
  afterEach(() => {
    vi.restoreAllMocks();
  });

  it('does NOT reclaim idle process while JSONL records keep arriving', async () => {
    const proc = createMockProcess();
    mockSpawn.mockReturnValue(proc);
    const workspace = mkdtempSync(join(tmpdir(), 'relay-pool-idle-jsonl-alive-'));
    const sessionFile = getBareSessionFilePath(baseConfig.sessionId);
    mkdirSync(dirname(sessionFile), { recursive: true });
    const interactivePool = createPool({
      idleTimeoutMs: 600,
      cleanupIntervalMs: 50,
      ccMode: 'interactive',
      interactiveWarmupMs: 0,
      interactiveSubmitDelayMs: 0,
      interactiveAcceptTimeoutMs: 10,
      killTimeoutMs: 1,
    });
    const onSessionEnd = vi.fn();
    interactivePool.on('session-end', onSessionEnd);

    await interactivePool.spawn({ ...baseConfig, workspace });
    await interactivePool.send('member-000', { type: 'human', content: 'hello' });

    // user echo + end_turn → finishTurn → idle (premature: subagent still running)
    appendFileSync(sessionFile, `${JSON.stringify({
      type: 'user', timestamp: new Date().toISOString(),
      message: { role: 'user', content: 'hello' },
    })}\n`);
    appendFileSync(sessionFile, `${JSON.stringify({
      type: 'assistant', timestamp: new Date().toISOString(),
      message: { role: 'assistant', stop_reason: 'end_turn',
        content: [{ type: 'text', text: 'kicked off background task' }] },
    })}\n`);

    expect(await waitFor(() => interactivePool.get('member-000')?.state === 'idle')).toBe(true);

    // Simulate CC continuing to write records (subagent + validation) well past
    // the 600ms idleTimeoutMs. Each batch is picked up by the 250ms watcher poll.
    for (let i = 0; i < 12; i++) {
      await new Promise((r) => setTimeout(r, 100));
      appendFileSync(sessionFile, `${JSON.stringify({
        type: 'system', timestamp: new Date().toISOString(),
      })}\n`);
    }
    // ~1200ms since finishTurn — double the idle threshold.

    expect(proc.kill).not.toHaveBeenCalledWith('SIGTERM');
    expect(onSessionEnd).not.toHaveBeenCalled();
    expect(interactivePool.get('member-000')?.state).toBe('idle');
    interactivePool.dispose();
    rmSync(workspace, { recursive: true, force: true });
    rmSync(dirname(sessionFile), { recursive: true, force: true });
  });

  it('reclaims idle process when JSONL goes silent past idleTimeoutMs (control)', async () => {
    const proc = createMockProcess();
    mockSpawn.mockReturnValue(proc);
    const workspace = mkdtempSync(join(tmpdir(), 'relay-pool-idle-jsonl-silent-'));
    const sessionFile = getBareSessionFilePath(baseConfig.sessionId);
    mkdirSync(dirname(sessionFile), { recursive: true });
    const interactivePool = createPool({
      idleTimeoutMs: 300,
      cleanupIntervalMs: 50,
      ccMode: 'interactive',
      interactiveWarmupMs: 0,
      interactiveSubmitDelayMs: 0,
      interactiveAcceptTimeoutMs: 10,
      killTimeoutMs: 1,
    });
    const onSessionEnd = vi.fn();
    interactivePool.on('session-end', onSessionEnd);

    await interactivePool.spawn({ ...baseConfig, workspace });
    await interactivePool.send('member-000', { type: 'human', content: 'hello' });

    appendFileSync(sessionFile, `${JSON.stringify({
      type: 'user', timestamp: new Date().toISOString(),
      message: { role: 'user', content: 'hello' },
    })}\n`);
    appendFileSync(sessionFile, `${JSON.stringify({
      type: 'assistant', timestamp: new Date().toISOString(),
      message: { role: 'assistant', stop_reason: 'end_turn',
        content: [{ type: 'text', text: 'done' }] },
    })}\n`);

    expect(await waitFor(() => interactivePool.get('member-000')?.state === 'idle')).toBe(true);

    // No further records → should be reclaimed after idleTimeoutMs.
    const reclaimed = await waitFor(() => onSessionEnd.mock.calls.length > 0, 3000);
    expect(reclaimed).toBe(true);
    expect(onSessionEnd).toHaveBeenCalledWith('member-000', expect.objectContaining({
      type: 'idle-reclaimed',
    }));
    interactivePool.dispose();
    rmSync(workspace, { recursive: true, force: true });
    rmSync(dirname(sessionFile), { recursive: true, force: true });
  });
});

describe('pool.kill', () => {
  let pool: ReturnType<typeof createPool>;

  beforeEach(() => {
    mockSpawn = vi.fn();
    pool = createPool({ cleanupIntervalMs: 999999, killTimeoutMs: 100, ccMode: 'print' });
  });

  afterEach(() => {
    pool.dispose();
    vi.restoreAllMocks();
  });

  it('sends SIGTERM and removes from registry', async () => {
    const proc = createMockProcess();
    mockSpawn.mockReturnValue(proc);

    const spawnPromise = pool.spawn(baseConfig);
    proc.stdout.write('{"type":"system","content":"ready"}\n');
    await spawnPromise;

    await pool.kill('member-000');

    expect(proc.kill).toHaveBeenCalled();
    expect(pool.get('member-000')).toBeUndefined();
  });

  it('preserves undelivered outbox files when the runtime is killed', async () => {
    const workspace = mkdtempSync(join(tmpdir(), 'relay-pool-kill-outbox-'));
    const previousOutboxRoot = process.env['RELAY_OUTBOX_DIR'];
    delete process.env['RELAY_OUTBOX_DIR'];
    try {
      const proc = createMockProcess();
      mockSpawn.mockReturnValue(proc);
      const config = { ...baseConfig, tenantId: 'member-kill-outbox', workspace };
      const spawnPromise = pool.spawn(config);
      proc.stdout.write('{"type":"system","content":"ready"}\n');
      await spawnPromise;

      const outbox = resolveToolFileOutboxPath(config.tenantId, workspace);
      const pending = join(outbox, 'not-delivered.txt');
      writeFileSync(pending, 'preserve until a channel confirms delivery');

      await pool.kill(config.tenantId);

      expect(existsSync(pending)).toBe(true);
    } finally {
      if (previousOutboxRoot === undefined) delete process.env['RELAY_OUTBOX_DIR'];
      else process.env['RELAY_OUTBOX_DIR'] = previousOutboxRoot;
      rmSync(workspace, { recursive: true, force: true });
    }
  });

  it('resolves even for non-existent tenant', async () => {
    await expect(pool.kill('nonexistent')).resolves.toBeUndefined();
  });
});

describe('pool events', () => {
  let pool: ReturnType<typeof createPool>;

  beforeEach(() => {
    mockSpawn = vi.fn();
    pool = createPool({ cleanupIntervalMs: 999999, ccMode: 'print' });
  });

  afterEach(() => {
    pool.dispose();
    vi.restoreAllMocks();
  });

  it('emits response events for stdout messages', async () => {
    const proc = createMockProcess();
    mockSpawn.mockReturnValue(proc);

    const responses: Array<{ tenantId: string; msg: StreamJsonMessage }> = [];
    pool.on('response', (tenantId, msg) => responses.push({ tenantId, msg }));

    const spawnPromise = pool.spawn(baseConfig);
    proc.stdout.write('{"type":"system","content":"ready"}\n');
    await spawnPromise;

    // print mode: assistant is buffered and emitted only when result arrives
    proc.stdout.write('{"type":"assistant","message":{"content":[{"type":"text","text":"hi there"}]},"content":"hi there"}\n');
    proc.stdout.write('{"type":"result","result":""}\n');
    await new Promise((resolve) => setTimeout(resolve, 50));

    // system message (ready) + assistant message (flushed on result)
    expect(responses.length).toBeGreaterThanOrEqual(1);
    const assistantMsg = responses.find(r => r.msg.type === 'assistant');
    expect(assistantMsg).toBeDefined();
    expect(assistantMsg?.msg.content).toBe('hi there');
  });

  it('delivers outbox files only to the channel session that owns them [wi_20261004_159]', async () => {
    const workspace = mkdtempSync(join(tmpdir(), 'relay-pool-shared-outbox-'));
    const previousOutboxRoot = process.env['RELAY_OUTBOX_DIR'];
    delete process.env['RELAY_OUTBOX_DIR'];
    const warn = vi.spyOn(console, 'warn').mockImplementation(() => undefined);
    try {
      const telegramProc = createMockProcess();
      const feishuProc = createMockProcess();
      mockSpawn.mockReturnValueOnce(telegramProc).mockReturnValueOnce(feishuProc);
      const telegram = { ...baseConfig, tenantId: 'owner:pulse', workspace };
      const feishu = { ...baseConfig, tenantId: 'owner:pulse:feishu', workspace, sessionId: 'feishu-session' };
      const telegramSpawn = pool.spawn(telegram);
      telegramProc.stdout.write('{"type":"system","content":"ready"}\n');
      await telegramSpawn;
      const feishuSpawn = pool.spawn(feishu);
      feishuProc.stdout.write('{"type":"system","content":"ready"}\n');
      await feishuSpawn;

      const delivered: Array<{ tenantId: string; paths: string[] }> = [];
      pool.on('tool-files', (tenantId, paths) => delivered.push({ tenantId, paths }));

      const telegramOutbox = resolveToolFileOutboxPath(telegram.tenantId, workspace);
      const feishuOutbox = resolveToolFileOutboxPath(feishu.tenantId, workspace);
      expect(telegramOutbox).not.toBe(feishuOutbox);
      const telegramFile = join(telegramOutbox, 'wi297-cc-followups.md');
      writeFileSync(telegramFile, 'written by the Telegram turn');
      const legacy = join(realpathSync(workspace), '.relay', 'outbox', 'legacy-unowned.md');
      writeFileSync(legacy, 'left by the old shared layout');

      // The Feishu session's turn ends first: it must not sweep Telegram's file.
      await pool.send(feishu.tenantId, { type: 'human', content: 'sleep 120' });
      feishuProc.stdout.write('{"type":"result","result":""}\n');
      await waitFor(() => pool.get(feishu.tenantId)?.state === 'idle');
      expect(delivered).toEqual([]);

      await pool.send(telegram.tenantId, { type: 'human', content: 'write the notes' });
      telegramProc.stdout.write('{"type":"result","result":""}\n');
      await waitFor(() => delivered.length > 0);
      expect(delivered).toEqual([{ tenantId: telegram.tenantId, paths: [telegramFile] }]);
      expect(existsSync(legacy)).toBe(true);
      expect(warn).toHaveBeenCalledWith(expect.objectContaining({ msg: 'Unattributed outbox file left undelivered', path: legacy }));
    } finally {
      if (previousOutboxRoot === undefined) delete process.env['RELAY_OUTBOX_DIR'];
      else process.env['RELAY_OUTBOX_DIR'] = previousOutboxRoot;
      rmSync(workspace, { recursive: true, force: true });
    }
  });

  it('retains queued B attempt context when print A result synchronously drains the queue', async () => {
    const previous=process.env['PULSE_COMPUTECOST_RECEIPT_DIR'];
    process.env['PULSE_COMPUTECOST_RECEIPT_DIR']='/synthetic/receipt-spool';
    try {
      const proc=createMockProcess();mockSpawn.mockReturnValue(proc);
      const messages:StreamJsonMessage[]=[];pool.on('response',(_tenant,msg)=>messages.push(msg));
      const spawned=pool.spawn({...baseConfig,ccMode:'print'});
      proc.stdout.write('{"type":"system","content":"ready"}\n');await spawned;
      await pool.send('member-000',{type:'human',content:'A'},
        {correlationId:'A',idempotencyKey:'A',effect:'no-effect',hostAttemptRef:'attempt-A'});
      const queued=pool.send('member-000',{type:'human',content:'B'},
        {correlationId:'B',idempotencyKey:'B',effect:'no-effect',hostAttemptRef:'attempt-B'});
      proc.stdout.write('{"type":"result","result":"A done"}\n');await queued;
      proc.stdout.write(JSON.stringify({type:'assistant',message:{id:'B-tool',model:'test',
        content:[{type:'tool_use',id:'tool',name:'Read',input:{}}],stop_reason:'tool_use',
        usage:{input_tokens:7,output_tokens:3}}})+'\n');
      await new Promise(r=>setTimeout(r,20));
      const telemetry=messages.find(m=>m.type==='usage_observation'&&(m.message as any)?.id==='B-tool');
      expect(telemetry?.host_attempt_ref).toBe('attempt-B');
      expect(messages.find(m=>m.type==='result')?.host_attempt_ref).toBe('attempt-A');
    } finally { if(previous===undefined)delete process.env['PULSE_COMPUTECOST_RECEIPT_DIR'];else process.env['PULSE_COMPUTECOST_RECEIPT_DIR']=previous; }
  });

  it('tags usage of a runtime-notification turn instead of the settled Relay ref', async () => {
    const previous=process.env['PULSE_COMPUTECOST_RECEIPT_DIR'];
    process.env['PULSE_COMPUTECOST_RECEIPT_DIR']='/synthetic/receipt-spool';
    try {
      const proc=createMockProcess();mockSpawn.mockReturnValue(proc);
      const messages:StreamJsonMessage[]=[];pool.on('response',(_tenant,msg)=>messages.push(msg));
      const spawned=pool.spawn({...baseConfig,ccMode:'print'});
      proc.stdout.write('{"type":"system","content":"ready"}\n');await spawned;
      await pool.send('member-000',{type:'human',content:'A'},
        {correlationId:'A',idempotencyKey:'A',effect:'no-effect',hostAttemptRef:'attempt-A'});
      proc.stdout.write('{"type":"result","result":"A done"}\n');
      await new Promise(r=>setTimeout(r,20));
      // Background work completes: CC opens a turn on its own.
      proc.stdout.write(JSON.stringify({type:'assistant',message:{id:'N-tool',model:'test',
        content:[{type:'tool_use',id:'tool',name:'Read',input:{}}],stop_reason:'tool_use',
        usage:{input_tokens:7,output_tokens:3}}})+'\n');
      await new Promise(r=>setTimeout(r,20));
      const telemetry=messages.find(m=>m.type==='usage_observation'&&(m.message as any)?.id==='N-tool');
      expect(telemetry?.turn_origin).toBe('runtime-notification');
      expect(telemetry?.host_attempt_ref).toBeUndefined();
    } finally { if(previous===undefined)delete process.env['PULSE_COMPUTECOST_RECEIPT_DIR'];else process.env['PULSE_COMPUTECOST_RECEIPT_DIR']=previous; }
  });

  it('forwards AskUserQuestion tool_use in print mode even without text', async () => {
    const proc = createMockProcess();
    mockSpawn.mockReturnValue(proc);

    const responses: Array<{ tenantId: string; msg: StreamJsonMessage }> = [];
    pool.on('response', (tenantId, msg) => responses.push({ tenantId, msg }));

    const spawnPromise = pool.spawn(baseConfig);
    proc.stdout.write('{"type":"system","content":"ready"}\n');
    await spawnPromise;

    proc.stdout.write(JSON.stringify({
      type: 'assistant',
      message: {
        stop_reason: 'tool_use',
        content: [{
          type: 'tool_use',
          id: 'call_ask_1',
          name: 'AskUserQuestion',
          input: { questions: [{ question: 'Q?', options: [{ label: 'A' }] }] },
        }],
      },
    }) + '\n');
    proc.stdout.write('{"type":"result","result":""}\n');
    await new Promise((resolve) => setTimeout(resolve, 50));

    const askMsg = responses.find((r) =>
      r.msg.type === 'assistant'
      && Array.isArray((r.msg.message as { content?: unknown[] })?.content)
      && (r.msg.message as { content: Array<{ name?: string }> }).content.some((b) => b.name === 'AskUserQuestion'),
    );
    expect(askMsg).toBeDefined();

    const bashOnly = responses.filter((r) =>
      r.msg.type === 'assistant'
      && Array.isArray((r.msg.message as { content?: unknown[] })?.content)
      && (r.msg.message as { content: Array<{ type?: string; name?: string }> }).content.every(
        (b) => b.type !== 'text' && b.name !== 'AskUserQuestion',
      ),
    );
    expect(bashOnly.length).toBe(0);
  });

  // Print-mode tool-call leak 断根修法 A+B（2026-06-29，升级自 2026-06-27 仅清盘）。
  // 旧契约（泄漏 record 仍 emit + 仅异步清盘）已被推翻：A 现在拦截 + nudge 重发，不让泄漏
  // 扩散；B 在 A 超限后清盘 + 优雅重启常驻进程清内存种子。

  // 复用 leak 文本：end_turn + 结构完整 <invoke>（含 parameter/闭合），无真 tool_use = 泄漏。
  const LEAK_TEXT = '先给你结论。\n\ncall\n<invoke name="Bash">\n<parameter name="command">ls</parameter>\n</invoke>';
  function leakLine(): string {
    return JSON.stringify({
      type: 'assistant',
      message: { stop_reason: 'end_turn', content: [{ type: 'text', text: LEAK_TEXT }] },
    }) + '\n';
  }

  // 修法 A：第一次泄漏 → 不 emit 泄漏正文 + nudge CC 重发真工具调用（写进 stdin）。
  it('print-mode A: a leaked tool-call record is NOT emitted and a nudge is sent (intercept)', async () => {
    const proc = createMockProcess();
    mockSpawn.mockReturnValue(proc);
    const stdin = captureStdin(proc);
    const responses: Array<{ tenantId: string; msg: StreamJsonMessage }> = [];
    pool.on('response', (tenantId, msg) => responses.push({ tenantId, msg }));

    const spawnPromise = pool.spawn(baseConfig);
    proc.stdout.write('{"type":"system","content":"ready"}\n');
    await spawnPromise;

    // 起一个 turn（busy），再吐泄漏 record。
    pool.send(baseConfig.tenantId, { type: 'user', content: [{ type: 'text', text: 'hi' }] }).catch(() => {});
    await new Promise((resolve) => setTimeout(resolve, 20));
    proc.stdout.write(leakLine());
    await new Promise((resolve) => setTimeout(resolve, 80));

    // 契约 1：泄漏 record 没 emit 给 channel（A 拦住，不让用户看到 <invoke> 文本）。
    const leakedToChannel = responses.find(
      (r) => r.msg.type === 'assistant'
        && JSON.stringify(r.msg).includes('<invoke name='),
    );
    expect(leakedToChannel).toBeUndefined();
    // 契约 2：nudge user 消息已写进 CC stdin（要求重发真工具调用）。
    expect(stdin.text()).toContain('把工具调用写成了文本');
    // 契约 3：进程仍活着（A 不杀进程，只是 nudge 重发）。
    expect(proc.killed).toBe(false);
  });

  // 修法 B：泄漏经 A 超限（maxToolcallLeakRetries=2）后 → 不 emit + 优雅 SIGTERM 重启清内存 + escalation。
  it('print-mode B: after nudge limit, CC is gracefully restarted and escalation fires', async () => {
    const proc = createMockProcess();
    mockSpawn.mockReturnValue(proc);
    const errors: Array<{ tenantId: string; err: Error }> = [];
    const responses: Array<{ tenantId: string; msg: StreamJsonMessage }> = [];
    pool.on('error', (tenantId: string, err: Error) => errors.push({ tenantId, err }));
    pool.on('response', (tenantId, msg) => responses.push({ tenantId, msg }));

    const spawnPromise = pool.spawn(baseConfig);
    proc.stdout.write('{"type":"system","content":"ready"}\n');
    await spawnPromise;

    // 连吐 3 条泄漏：前 2 条走 A（nudge），第 3 条超限走 B（重启）。
    for (let i = 0; i < 3; i++) {
      pool.send(baseConfig.tenantId, { type: 'user', content: [{ type: 'text', text: 'go' }] }).catch(() => {});
      await new Promise((resolve) => setTimeout(resolve, 20));
      proc.stdout.write(leakLine());
      await new Promise((resolve) => setTimeout(resolve, 80));
    }

    // 契约 1：第 3 条触发 B → SIGTERM 该进程（mock kill 记 killed=true）。
    expect(proc.kill).toHaveBeenCalledWith('SIGTERM');
    // 契约 2：escalation 不静默——emit 了 error（带"tool-call leak unresolved"）。
    const esc = errors.find((e) => /tool-call leak unresolved/.test(e.err.message));
    expect(esc).toBeDefined();
    // 契约 3：泄漏正文自始至终没 emit 给 channel。
    const leakedToChannel = responses.find(
      (r) => r.msg.type === 'assistant'
        && JSON.stringify(r.msg).includes('<invoke name='),
    );
    expect(leakedToChannel).toBeUndefined();
  });

  // flag 关：回退到旧行为——异步清盘 + 照常 emit 泄漏 record（可逆，安全阀）。
  it('print-mode: RELAY_ENABLE_PRINT_LEAK_GUARD=false reverts to old behavior (emit + async de-poison only)', async () => {
    process.env['RELAY_ENABLE_PRINT_LEAK_GUARD'] = 'false';
    const proc = createMockProcess();
    mockSpawn.mockReturnValue(proc);
    const responses: Array<{ tenantId: string; msg: StreamJsonMessage }> = [];
    pool.on('response', (tenantId, msg) => responses.push({ tenantId, msg }));

    const spawnPromise = pool.spawn(baseConfig);
    proc.stdout.write('{"type":"system","content":"ready"}\n');
    await spawnPromise;

    proc.stdout.write(leakLine());
    proc.stdout.write('{"type":"result","result":""}\n');
    await new Promise((resolve) => setTimeout(resolve, 50));

    // flag 关：旧契约——泄漏 record 仍 emit（不拦不重启），进程不被杀。
    expect(responses.find((r) => r.msg.type === 'assistant')).toBeDefined();
    expect(proc.killed).toBe(false);
    delete process.env['RELAY_ENABLE_PRINT_LEAK_GUARD'];
  });
});

describe('pool state machine', () => {
  let pool: ReturnType<typeof createPool>;

  beforeEach(() => {
    mockSpawn = vi.fn();
    pool = createPool({ cleanupIntervalMs: 999999, ccMode: 'print' });
  });

  afterEach(() => {
    pool.dispose();
    vi.restoreAllMocks();
  });

  it('follows starting → ready → busy → idle cycle', async () => {
    const proc = createMockProcess();
    mockSpawn.mockReturnValue(proc);

    const spawnPromise = pool.spawn(baseConfig);
    expect(pool.get('member-000')?.state).toBe('starting');

    proc.stdout.write('{"type":"system","content":"ready"}\n');
    await spawnPromise;
    expect(pool.get('member-000')?.state).toBe('ready');

    await pool.send('member-000', { type: 'human', content: 'test' });
    expect(pool.get('member-000')?.state).toBe('busy');

    proc.stdout.write('{"type":"result","total_cost_usd":0.01}\n');
    await new Promise((resolve) => setTimeout(resolve, 50));
    expect(pool.get('member-000')?.state).toBe('idle');
  });
});

describe('pool session-end events', () => {
  let pool: ReturnType<typeof createPool>;

  beforeEach(() => {
    mockSpawn = vi.fn();
    pool = createPool({ cleanupIntervalMs: 999999, ccMode: 'print' });
  });

  afterEach(() => {
    pool.dispose();
    vi.restoreAllMocks();
  });

  it('emits session-end with turns-exhausted when exit code=0 and turns full', async () => {
    const proc = createMockProcess();
    mockSpawn.mockReturnValue(proc);

    await pool.spawn({ ...baseConfig, maxTurns: 3 });

    // Send message to make it busy
    await pool.send('member-000', { type: 'human', content: 'test' });

    // Simulate 3 assistant turns
    proc.stdout.write('{"type":"assistant","message":{"content":[{"type":"text","text":"r1"}]}}\n');
    proc.stdout.write('{"type":"assistant","message":{"content":[{"type":"text","text":"r2"}]}}\n');
    proc.stdout.write('{"type":"assistant","message":{"content":[{"type":"text","text":"r3"}]}}\n');
    proc.stdout.write('{"type":"result","total_cost_usd":0.01}\n');
    await new Promise(r => setTimeout(r, 50));

    const events: Array<{ tenantId: string; reason: unknown }> = [];
    pool.on('session-end', (tenantId, reason) => {
      events.push({ tenantId, reason });
    });

    // Now CC exits with code 0 (turns exhausted)
    proc.emit('exit', 0, null);
    await new Promise(r => setTimeout(r, 50));

    expect(events).toHaveLength(1);
    expect(events[0].tenantId).toBe('member-000');
    expect((events[0].reason as { type: string }).type).toBe('turns-exhausted');
  });

  it('does NOT emit turns-exhausted when exit code=0 but turns not full', async () => {
    const proc = createMockProcess();
    mockSpawn.mockReturnValue(proc);

    await pool.spawn({ ...baseConfig, maxTurns: 100 });
    await pool.send('member-000', { type: 'human', content: 'test' });

    // Only 1 assistant turn
    proc.stdout.write('{"type":"assistant","message":{"content":[{"type":"text","text":"r1"}]}}\n');
    proc.stdout.write('{"type":"result","total_cost_usd":0.01}\n');
    await new Promise(r => setTimeout(r, 50));

    const events: unknown[] = [];
    pool.on('session-end', (_t, reason) => events.push(reason));

    proc.emit('exit', 0, null);
    await new Promise(r => setTimeout(r, 50));

    // No turns-exhausted event (normal exit with low turns)
    const turnsEvents = events.filter((e: unknown) => (e as { type: string }).type === 'turns-exhausted');
    expect(turnsEvents).toHaveLength(0);
  });

  it('emits session-end with idle-reclaimed on idle cleanup', async () => {
    const poolWithCleanup = createPool({
      idleTimeoutMs: 100,
      cleanupIntervalMs: 50,
      ccMode: 'print',
    });

    const proc = createMockProcess();
    // Don't actually kill the process in mock - just emit exit
    proc.kill = vi.fn(() => {
      proc.emit('exit', 143, 'SIGTERM');
      return true;
    });
    mockSpawn.mockReturnValue(proc);

    await poolWithCleanup.spawn(baseConfig);
    await poolWithCleanup.send('member-000', { type: 'human', content: 'test' });
    proc.stdout.write('{"type":"result","total_cost_usd":0.01}\n');
    await new Promise(r => setTimeout(r, 50));

    const events: Array<{ reason: { type: string } }> = [];
    poolWithCleanup.on('session-end', (_t, reason) => events.push({ reason }));

    // Wait for idle cleanup to trigger
    await new Promise(r => setTimeout(r, 250));

    poolWithCleanup.dispose();

    const idleEvents = events.filter(e => e.reason.type === 'idle-reclaimed');
    expect(idleEvents.length).toBeGreaterThanOrEqual(1);
  });

  it('emits session-end relay-shutdown and rejects pending when SIGTERM interrupts busy CC', async () => {
    const proc = createMockProcess();
    mockSpawn.mockReturnValue(proc);

    await pool.spawn(baseConfig);

    // First send: goes through immediately (CC idle → busy), no result yet so CC stays busy
    // Suppress unhandled rejection — this in-flight send has no result before CC is killed
    pool.send('member-000', { type: 'human', content: 'first' }).catch(() => {});
    await new Promise(r => setTimeout(r, 10));

    // Second send: CC is now busy, so this one queues in pendingMessages
    let pendingRejected = false;
    const pendingSend = pool.send('member-000', { type: 'human', content: 'queued' });
    pendingSend.catch(() => { pendingRejected = true; });

    const sessionEvents: Array<{ type: string }> = [];
    pool.on('session-end', (_t, reason) => sessionEvents.push(reason as { type: string }));

    // CC exits with SIGTERM (143) while busy — simulates relay restart mid-turn
    proc.emit('exit', 143, null);
    await new Promise(r => setTimeout(r, 50));

    // queued pending send should be rejected
    expect(pendingRejected).toBe(true);
    await expect(pendingSend).rejects.toThrow('CC interrupted by SIGTERM while busy');
    // session-end should fire with relay-shutdown
    expect(sessionEvents).toHaveLength(1);
    expect(sessionEvents[0].type).toBe('relay-shutdown');
  });

  it('does NOT emit relay-shutdown session-end when idle CC exits with SIGTERM (wasState=idle)', async () => {
    const proc = createMockProcess();
    mockSpawn.mockReturnValue(proc);

    await pool.spawn(baseConfig);
    // No send — CC is idle

    const sessionEvents: Array<{ type: string }> = [];
    pool.on('session-end', (_t, reason) => sessionEvents.push(reason as { type: string }));

    proc.emit('exit', 143, null);
    await new Promise(r => setTimeout(r, 50));

    const shutdownEvents = sessionEvents.filter(e => e.type === 'relay-shutdown');
    expect(shutdownEvents).toHaveLength(0);
  });

  it('on 429 with in-flight message: backs off (no rate-limited session-end), respawns, replays', async () => {
    const poolWithRateLimit = createPool({
      cleanupIntervalMs: 999999,
      // 测试覆盖：把退避序列压到 1ms。
      rateLimitRestartDelayMs: 1,
      rateLimitAbortTimeoutMs: 1,
      ccMode: 'print',
    });

    const proc1 = createMockProcess(2001);
    const proc2 = createMockProcess(2002);
    mockSpawn
      .mockReturnValueOnce(proc1)
      .mockReturnValueOnce(proc2);

    await poolWithRateLimit.spawn(baseConfig);
    void poolWithRateLimit.send('member-000', { type: 'human', content: 'hello' }).catch(() => {});

    const sessionEvents: Array<{ type: string; retryCount?: number }> = [];
    const noticeEvents: Array<{ retryCount: number; nextDelayMs: number }> = [];
    poolWithRateLimit.on('session-end', (_t, reason) => {
      sessionEvents.push(reason as { type: string; retryCount?: number });
    });
    poolWithRateLimit.on('rate-limit-notice', (_t, info) => {
      noticeEvents.push(info);
    });

    proc1.stdout.write('{"type":"system","subtype":"api_retry","error_status":429,"error":"rate_limit"}\n');
    await new Promise(r => setTimeout(r, 120));

    // 关键断言：有 in-flight 消息时不再立即 emit rate-limited，而是退避后 respawn。
    expect(sessionEvents.filter(e => e.type === 'rate-limited')).toEqual([]);
    expect(proc1.kill).toHaveBeenCalledWith('SIGKILL');
    expect(mockSpawn).toHaveBeenCalledTimes(2); // 原 spawn + respawn after backoff
    // 首次 retry 不发用户提示（NOTIFY_AFTER_RETRY_INDEX=2 → retryCount=3 才发）。
    expect(noticeEvents).toEqual([]);

    poolWithRateLimit.dispose();
  });

  it('emits rate-limit-notice once on third backoff retry', async () => {
    const poolWithRateLimit = createPool({
      cleanupIntervalMs: 999999,
      rateLimitRestartDelayMs: 1,
      rateLimitAbortTimeoutMs: 1,
      ccMode: 'print',
    });

    const procs = [
      createMockProcess(3001),
      createMockProcess(3002),
      createMockProcess(3003),
      createMockProcess(3004),
    ];
    mockSpawn
      .mockReturnValueOnce(procs[0])
      .mockReturnValueOnce(procs[1])
      .mockReturnValueOnce(procs[2])
      .mockReturnValueOnce(procs[3]);

    await poolWithRateLimit.spawn(baseConfig);
    void poolWithRateLimit.send('member-000', { type: 'human', content: 'hello' }).catch(() => {});

    const noticeEvents: Array<{ retryCount: number; nextDelayMs: number }> = [];
    poolWithRateLimit.on('rate-limit-notice', (_t, info) => {
      noticeEvents.push(info);
    });

    // 触发 3 次连续 429。每次 spawn 出来的 proc 都立即再 429。
    // 必须等当前 in-pool 的 proc 变 busy 后再写 429，否则 stream reader
    // 的 `proc.state === 'busy'` 守卫会丢弃这条 line（baseline flake 根因）。
    for (let i = 0; i < 3; i++) {
      await waitFor(() => {
        const p = poolWithRateLimit.get('member-000');
        return p?.process === procs[i] && p.state === 'busy';
      }, 1000);
      procs[i]!.stdout.write('{"type":"system","subtype":"api_retry","error_status":429,"error":"rate_limit"}\n');
      await new Promise(r => setTimeout(r, 30));
    }
    // 让最后一轮 backoff 退场，避免 dispose 抓到 in-flight handler。
    await new Promise(r => setTimeout(r, 30));

    // 第 3 次 retry（retryCount=3）触发一次 notice，后续不再重复。
    expect(noticeEvents.length).toBe(1);
    expect(noticeEvents[0]!.retryCount).toBe(3);

    poolWithRateLimit.dispose();
  });
});

describe('session-management verification', () => {
  let pool: ReturnType<typeof createPool>;

  beforeEach(() => {
    mockSpawn = vi.fn();
    pool = createPool({ cleanupIntervalMs: 999999, ccMode: 'print' });
  });

  afterEach(() => {
    pool.dispose();
    vi.restoreAllMocks();
  });

  it('spawn → kill → spawn(resume:true) rebuilds with same sessionId [Unit Test-001]', async () => {
    const proc1 = createMockProcess(3001);
    const proc2 = createMockProcess(3002);
    mockSpawn
      .mockReturnValueOnce(proc1)
      .mockReturnValueOnce(proc2);

    // First spawn
    const spawn1 = pool.spawn(baseConfig);
    proc1.stdout.write('{"type":"system","content":"ready"}\n');
    await spawn1;
    expect(pool.get('member-000')).toBeDefined();

    // Kill the process
    await pool.kill('member-000');
    expect(pool.get('member-000')).toBeUndefined();

    // Re-spawn with resume:true — simulates telegram.ts dead→resume path
    const spawn2 = pool.spawn({ ...baseConfig, resume: true });
    proc2.stdout.write('{"type":"system","content":"ready"}\n');
    await spawn2;

    // Second spawn should include --session-id with the same sessionId
    expect(mockSpawn).toHaveBeenCalledTimes(2);
    const secondArgs = mockSpawn.mock.calls[1][1] as string[];
    expect(secondArgs).toContain('--session-id');
    expect(secondArgs).toContain('test-session-uuid');
    // In print mode, --resume is never passed (it's a stream-json protocol flag)
    expect(secondArgs).not.toContain('--resume');
    expect(pool.get('member-000')).toBeDefined();
    expect(pool.get('member-000')?.sessionId).toBe('test-session-uuid');
  });

  it('getStatus() returns correct fields for active process [Unit Test-002]', async () => {
    const proc = createMockProcess(4001);
    mockSpawn.mockReturnValue(proc);

    const spawnPromise = pool.spawn(baseConfig);
    proc.stdout.write('{"type":"system","content":"ready"}\n');
    await spawnPromise;

    const statuses = pool.getStatus();
    expect(statuses).toHaveLength(1);
    const status = statuses[0];
    expect(status.tenantId).toBe('member-000');
    expect(status.state).toBe('ready');
    expect(status.pid).toBe(4001);
    expect(status.lastActivity).toBeGreaterThan(0);
    expect(status.spawnedAt).toBeGreaterThan(0);
    expect(status.restartCount).toBe(0);
  });
});

describe('validateSessionChain', () => {
  it('returns valid for a complete parentUuid chain', () => {
    const dir = mkdtempSync(join(tmpdir(), 'relay-chain-valid-'));
    const file = join(dir, 'session.jsonl');
    writeFileSync(file, [
      JSON.stringify({ type: 'user', uuid: 'u1', message: { role: 'user', content: 'hi' } }),
      JSON.stringify({ type: 'assistant', uuid: 'a1', parentUuid: 'u1', message: { role: 'assistant', content: [] } }),
    ].join('\n') + '\n');
    expect(validateSessionChain(file)).toEqual({ valid: true });
    rmSync(dir, { recursive: true, force: true });
  });

  it('detects missing parentUuid references', () => {
    const dir = mkdtempSync(join(tmpdir(), 'relay-chain-broken-'));
    const file = join(dir, 'session.jsonl');
    writeFileSync(file, [
      JSON.stringify({ type: 'user', uuid: 'u1', message: { role: 'user', content: 'hi' } }),
      JSON.stringify({ type: 'assistant', uuid: 'a1', parentUuid: 'missing', message: { role: 'assistant', content: [] } }),
    ].join('\n') + '\n');
    const result = validateSessionChain(file);
    expect(result.valid).toBe(false);
    expect(result.missingParentUuid).toBe('missing');
    expect(result.brokenLine).toBe(1);
    rmSync(dir, { recursive: true, force: true });
  });
});

describe('bug-20260610-001 session chain recovery', () => {
  let pool: ReturnType<typeof createPool>;

  beforeEach(() => {
    mockSpawn = vi.fn();
  });

  afterEach(() => {
    pool?.dispose();
    vi.restoreAllMocks();
  });

  it('starts a fresh session when resume jsonl chain is truly unrepairable (cycle)', async () => {
    // 2026-07-05 C（兜底降级）改动后：判 chain 断裂会先跑 repairSessionJsonl 的 orphan-rewire
    // 补链再重校验，只有**修复后仍 invalid** 才丢弃。CC 自产孤儿(parentUuid 悬空)现在都能被
    // 补链救回、resume 保住 → 不再触发 recovery（见下面的姊妹测试）。
    // 因此这条「丢弃 + 起新 session」路径的测试 fixture 必须是**真·不可修复**的断裂——
    // 用一个 parentUuid 环：环里每条的 parent 都存在（Pass 2 的孤儿补链够不着它），
    // validateSessionChain 回溯遇环判 invalid，repair 无能为力 → 走丢弃降级。
    const proc = createMockProcess();
    mockSpawn.mockReturnValue(proc);
    const workspace = mkdtempSync(join(tmpdir(), 'relay-chain-recovery-'));
    const sessionFile = getBareSessionFilePath(baseConfig.sessionId);
    mkdirSync(dirname(sessionFile), { recursive: true });
    writeFileSync(sessionFile, [
      // a1.parent=a2, a2.parent=a1 —— 环。leaf=a2 回溯 a2→a1→a2 撞环。
      JSON.stringify({ type: 'assistant', uuid: 'a1', parentUuid: 'a2', message: { role: 'assistant', content: [] } }),
      JSON.stringify({ type: 'assistant', uuid: 'a2', parentUuid: 'a1', message: { role: 'assistant', content: [] } }),
    ].join('\n') + '\n');

    pool = createPool({
      cleanupIntervalMs: 999999,
      ccMode: 'interactive',
      interactiveWarmupMs: 0,
    });
    const onRecovery = vi.fn();
    pool.on('session-chain-recovery', onRecovery);

    await pool.spawn({ ...baseConfig, workspace, resume: true });

    expect(onRecovery).toHaveBeenCalledTimes(1);
    const event = onRecovery.mock.calls[0]![1] as { newSessionId: string; previousSessionId: string };
    expect(event.previousSessionId).toBe(baseConfig.sessionId);
    expect(event.newSessionId).not.toBe(baseConfig.sessionId);

    const args = mockSpawn.mock.calls[0]![1] as string[];
    expect(args).toContain('--session-id');
    expect(args).toContain(event.newSessionId);
    expect(args).not.toContain('--resume');
    expect(args.join(' ')).toContain('链断裂');

    const procInfo = pool.get('member-000');
    expect(procInfo?.sessionId).toBe(event.newSessionId);

    rmSync(workspace, { recursive: true, force: true });
    rmSync(dirname(sessionFile), { recursive: true, force: true });
  });

  it('CC 自产孤儿 parentUuid：补链救回、保留 sessionId resume，不触发 recovery', async () => {
    // 2026-07-05 断链失忆根因治本：CC SDK 高频 tool_use 下写 jsonl 丢行 → 某条记录 parentUuid
    // 悬空。旧行为 = 判整个 session 断链 → 丢弃 + 起新 session + 推「对话历史链断裂」→ 失忆。
    // 新行为 = repairSessionJsonl orphan-rewire 补链 → chain valid → 保留 sessionId 正常 resume。
    const proc = createMockProcess();
    mockSpawn.mockReturnValue(proc);
    const workspace = mkdtempSync(join(tmpdir(), 'relay-orphan-heal-'));
    const sessionFile = getBareSessionFilePath(baseConfig.sessionId);
    mkdirSync(dirname(sessionFile), { recursive: true });
    // u1(root) → a1(parent=u1) → a2(parent='ghost' 悬空孤儿，物理前一条是 a1)
    writeFileSync(sessionFile, [
      JSON.stringify({ type: 'user', uuid: 'u1', message: { role: 'user', content: 'hi' } }),
      JSON.stringify({ type: 'assistant', uuid: 'a1', parentUuid: 'u1', message: { role: 'assistant', content: [] } }),
      JSON.stringify({ type: 'assistant', uuid: 'a2', parentUuid: 'ghost', message: { role: 'assistant', content: [] } }),
    ].join('\n') + '\n');

    pool = createPool({
      cleanupIntervalMs: 999999,
      ccMode: 'interactive',
      interactiveWarmupMs: 0,
    });
    const onRecovery = vi.fn();
    pool.on('session-chain-recovery', onRecovery);

    await pool.spawn({ ...baseConfig, workspace, resume: true });

    // 关键：不丢弃、不起新 session、不推断链通知。
    expect(onRecovery).not.toHaveBeenCalled();
    const args = mockSpawn.mock.calls[0]![1] as string[];
    expect(args).toContain('--resume');
    expect(args).toContain(baseConfig.sessionId);
    expect(args.join(' ')).not.toContain('链断裂');

    const procInfo = pool.get('member-000');
    expect(procInfo?.sessionId).toBe(baseConfig.sessionId);

    // 文件被补链：a2.parentUuid 从 'ghost' rewire 到物理前一条 a1，chain 现已 valid。
    const healed = readFileSync(sessionFile, 'utf-8').split('\n').filter((l) => l.trim())
      .map((l) => JSON.parse(l) as { uuid?: string; parentUuid?: string });
    const a2 = healed.find((r) => r.uuid === 'a2')!;
    expect(a2.parentUuid).toBe('a1');

    rmSync(workspace, { recursive: true, force: true });
    rmSync(dirname(sessionFile), { recursive: true, force: true });
  });

  it('waits for interactive resume readiness before returning from spawn', async () => {
    const proc = createMockProcess();
    mockSpawn.mockReturnValue(proc);
    const workspace = mkdtempSync(join(tmpdir(), 'relay-resume-ready-'));
    const sessionFile = getBareSessionFilePath(baseConfig.sessionId);
    mkdirSync(dirname(sessionFile), { recursive: true });
    writeFileSync(sessionFile, `${JSON.stringify({
      type: 'user',
      uuid: 'u1',
      message: { role: 'user', content: 'ok' },
    })}\n`);

    pool = createPool({
      cleanupIntervalMs: 999999,
      ccMode: 'interactive',
      interactiveWarmupMs: 60,
    });

    const start = Date.now();
    await pool.spawn({ ...baseConfig, workspace, resume: true });
    expect(Date.now() - start).toBeGreaterThanOrEqual(60);

    rmSync(workspace, { recursive: true, force: true });
    rmSync(dirname(sessionFile), { recursive: true, force: true });
  });
});

// Pool behavior is isolated from OS process ownership, tested with real processes separately.
vi.mock('../src/owned-process.js', async () => ({
  spawnOwnedProcess: (await import('node:child_process')).spawn,
}));

describe('pool shutdown with turns queued behind a busy turn', () => {
  let pool: ReturnType<typeof createPool>;
  afterEach(() => { pool.dispose(); vi.restoreAllMocks(); });

  it('records the busy turn as interrupted and hands the queued turns over in order', async () => {
    mockSpawn = vi.fn();
    const interrupted = vi.fn();
    const queued = vi.fn();
    pool = createPool({ cleanupIntervalMs: 999999, ccMode: 'print', onShutdownInterrupt: interrupted, onShutdownQueued: queued });
    const proc = createMockProcess();
    mockSpawn.mockReturnValue(proc);
    const spawned = pool.spawn(baseConfig);
    proc.stdout.write('{"type":"system","content":"ready"}\n');
    await spawned;

    await pool.send('member-000', { type: 'human', content: 'A' });
    let settledB = false, settledC = false;
    const b = pool.send('member-000', { type: 'human', content: 'B' }).then(() => { settledB = true; });
    const c = pool.send('member-000', { type: 'human', content: 'C' }).then(() => { settledC = true; });
    await Promise.resolve();
    expect(settledB || settledC).toBe(false);

    pool.emitShutdown();
    expect(interrupted).toHaveBeenCalledWith('member-000', { messageText: 'A' });
    expect(queued).toHaveBeenCalledOnce();
    const [tenantId, turns] = queued.mock.calls[0] as [string, Array<{ queuedAt: number; message: StreamJsonMessage }>];
    expect(tenantId).toBe('member-000');
    expect(turns.map(turn => turn.message.content)).toEqual(['B', 'C']);
    expect(turns.every(turn => typeof turn.queuedAt === 'number')).toBe(true);
    // Recorded turns count as delivered; the later SIGTERM exit rejects nothing.
    await Promise.all([b, c]);
    proc.emit('exit', 143, null);
  });
});

describe('turns recorded at shutdown across the barrier and the CC pool', () => {
  let pool: ReturnType<typeof createPool>;
  afterEach(() => { pool.dispose(); vi.restoreAllMocks(); });

  /** Real barrier, runtime manager and pool over a mocked CC child, with state in memory. */
  async function relayParts() {
    mockSpawn = vi.fn();
    const sessions: Record<string, PersistedSession> = {
      'member-000': { poolKey: 'member-000', agentId: 'pulse', sessionId: 'test-session-uuid', turnCount: 1, maxTurns: 9,
        lastActivity: Date.now(), status: 'active' },
    };
    const state = {
      flush: vi.fn(),
      getSession: (key: string) => sessions[key],
      getAllSessions: () => sessions,
      updateSession: (key: string, patch: Partial<PersistedSession>) => { sessions[key] = { ...sessions[key]!, ...patch }; },
    };
    pool = createPool({ cleanupIntervalMs: 999999, ccMode: 'print', onShutdownInterrupt: vi.fn(),
      onShutdownQueued: (tenantId, turns) => { persistQueuedTurns(state, turns.map(turn => ({ tenantId, runtime: 'cc', ...turn })), Date.now()); } });
    const barrier = createMaintenanceBarrier();
    const runtime = createConversationRuntimeManager({ barrier });
    runtime.register(createPoolConversationAdapter(pool));
    const proc = createMockProcess();
    mockSpawn.mockReturnValue(proc);
    const spawned = runtime.spawn({ ...baseConfig, runtime: 'cc' } as never);
    proc.stdout.write('{"type":"system","content":"ready"}\n');
    await spawned;
    const written: string[] = [];
    proc.stdin.on('data', (chunk: Buffer) => written.push(chunk.toString()));
    return {
      runtime, state, written,
      // index.ts notifyShutdown: the barrier closes first, the runtimes follow.
      closeBarrier: () => barrier.close(turn => { persistQueuedTurns(state, [turn], Date.now()); }),
      pending: () => (sessions['member-000']!.pendingMaintenanceTurns ?? []).map(turn => turn.message.content),
    };
  }

  const ticks = async () => { for (let i = 0; i < 5; i++) await Promise.resolve(); };

  it('keeps arrival order within one millisecond: the pool hands over its older turn after the barrier recorded a newer one', async () => {
    const parts = await relayParts();
    vi.spyOn(Date, 'now').mockReturnValue(1_800_000_000_000);
    await parts.runtime.send('member-000', { type: 'human', content: 'A' });
    void parts.runtime.send('member-000', { type: 'human', content: 'B' });  // queued behind A
    await ticks();
    parts.closeBarrier();
    void parts.runtime.send('member-000', { type: 'human', content: 'C' });  // recorded by the barrier
    expect(parts.pending()).toEqual(['C']);
    parts.runtime.emitShutdown();  // the pool hands over B, same millisecond
    expect(parts.pending()).toEqual(['B', 'C']);
  });

  it('a second shutdown during the replay of recorded turns loses none and records none twice', async () => {
    const parts = await relayParts();
    parts.state.updateSession('member-000', { pendingMaintenanceTurns: [
      { queuedAt: 1, seq: 1_000, message: { type: 'human', content: 'B' } },
      { queuedAt: 2, seq: 2_000, message: { type: 'human', content: 'C' } },
    ] });
    // The resume turn A is in flight; the replay queues B behind it.
    await parts.runtime.send('member-000', { type: 'human', content: 'A' });
    const replay = replayPendingTurns(parts.state, 'member-000', (message, submit) => parts.runtime.send('member-000', message, submit));
    // Each record leaves the list once the pool accepted its turn.
    await replay;
    expect(parts.pending()).toEqual([]);
    parts.closeBarrier();
    parts.runtime.emitShutdown();  // B and C handed back under their own numbers
    expect(parts.pending()).toEqual(['B', 'C']);
    expect(parts.state.getSession('member-000')!.pendingMaintenanceTurns!.map(turn => turn.seq)).toEqual([1_000, 2_000]);
    // A replay that meets the closed barrier records the turn once, in place.
    const again = replayPendingTurns(parts.state, 'member-000', (message, submit) => parts.runtime.send('member-000', message, submit));
    await again;
    expect(parts.pending()).toEqual(['B', 'C']);
    expect(parts.written.join('')).toContain('"A"');
    expect(parts.written.join('')).not.toMatch(/"(B|C)"/);
  });
});
