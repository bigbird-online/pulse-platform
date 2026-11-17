import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { EventEmitter } from 'node:events';
import { PassThrough } from 'node:stream';
import { createPool } from '../src/pool.js';
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

let mockSpawn: ReturnType<typeof vi.fn>;

vi.mock('node:child_process', () => ({
  spawn: (...args: unknown[]) => mockSpawn(...args),
}));

const baseConfig: SpawnConfig = {
  tenantId: 'member-000',
  workspace: '/opt/PULSE/Tenants/member-000/',
  sessionId: 'test-session-uuid',
};

const PRIMARY_TOKEN = 'sk-ant-oat01-PRIMARY-aaaa';
const FALLBACK_TOKEN = 'sk-ant-oat01-FALLBACK-bbbb';

afterEach(() => {
  vi.unstubAllEnvs();
});

describe('detectTokenSignal (via stderr)', () => {
  let pool: ReturnType<typeof createPool>;

  beforeEach(() => {
    mockSpawn = vi.fn();
    pool = createPool({
      cleanupIntervalMs: 999999,
      primaryToken: PRIMARY_TOKEN,
      fallbackToken: FALLBACK_TOKEN,
      ccMode: 'print',
    });
  });

  afterEach(() => {
    pool.dispose();
    vi.restoreAllMocks();
  });

  it('triggers fallback on "rate limit" in stderr', async () => {
    const proc1 = createMockProcess(1001);
    const proc2 = createMockProcess(1002);
    mockSpawn.mockReturnValueOnce(proc1).mockReturnValueOnce(proc2);

    await pool.spawn(baseConfig);

    // Emit rate limit on stderr
    proc1.stderr.write('Error: rate limit exceeded\n');
    await new Promise(r => setTimeout(r, 100));

    // A second spawn should have happened (fallback)
    expect(mockSpawn).toHaveBeenCalledTimes(2);
    const secondEnv = mockSpawn.mock.calls[1][2].env;
    expect(secondEnv['CLAUDE_CODE_OAUTH_TOKEN']).toBe(FALLBACK_TOKEN);
  });

  it('triggers fallback on "overloaded" in stderr', async () => {
    const proc1 = createMockProcess(1001);
    const proc2 = createMockProcess(1002);
    mockSpawn.mockReturnValueOnce(proc1).mockReturnValueOnce(proc2);

    await pool.spawn(baseConfig);
    proc1.stderr.write('Service overloaded, try again later\n');
    await new Promise(r => setTimeout(r, 100));

    expect(mockSpawn).toHaveBeenCalledTimes(2);
    const secondEnv = mockSpawn.mock.calls[1][2].env;
    expect(secondEnv['CLAUDE_CODE_OAUTH_TOKEN']).toBe(FALLBACK_TOKEN);
  });

  it('triggers fallback on "401" in stderr', async () => {
    const proc1 = createMockProcess(1001);
    const proc2 = createMockProcess(1002);
    mockSpawn.mockReturnValueOnce(proc1).mockReturnValueOnce(proc2);

    await pool.spawn(baseConfig);
    proc1.stderr.write('HTTP 401 Unauthorized\n');
    await new Promise(r => setTimeout(r, 100));

    expect(mockSpawn).toHaveBeenCalledTimes(2);
    const secondEnv = mockSpawn.mock.calls[1][2].env;
    expect(secondEnv['CLAUDE_CODE_OAUTH_TOKEN']).toBe(FALLBACK_TOKEN);
  });

  it('triggers fallback on "insufficient_quota" in stderr', async () => {
    const proc1 = createMockProcess(1001);
    const proc2 = createMockProcess(1002);
    mockSpawn.mockReturnValueOnce(proc1).mockReturnValueOnce(proc2);

    await pool.spawn(baseConfig);
    proc1.stderr.write('Error: insufficient_quota for this account\n');
    await new Promise(r => setTimeout(r, 100));

    expect(mockSpawn).toHaveBeenCalledTimes(2);
    const secondEnv = mockSpawn.mock.calls[1][2].env;
    expect(secondEnv['CLAUDE_CODE_OAUTH_TOKEN']).toBe(FALLBACK_TOKEN);
  });

  it('does NOT trigger fallback on normal stderr output', async () => {
    const proc1 = createMockProcess(1001);
    mockSpawn.mockReturnValueOnce(proc1);

    await pool.spawn(baseConfig);
    proc1.stderr.write('Warning: some harmless debug info\n');
    await new Promise(r => setTimeout(r, 100));

    // Only the initial spawn
    expect(mockSpawn).toHaveBeenCalledTimes(1);
  });
});

describe('token fallback does not consume restart quota', () => {
  let pool: ReturnType<typeof createPool>;

  beforeEach(() => {
    mockSpawn = vi.fn();
    pool = createPool({
      cleanupIntervalMs: 999999,
      maxRestartsPerHour: 3,
      primaryToken: PRIMARY_TOKEN,
      fallbackToken: FALLBACK_TOKEN,
      ccMode: 'print',
    });
  });

  afterEach(() => {
    pool.dispose();
    vi.restoreAllMocks();
  });

  it('restartCount stays at 0 after token fallback', async () => {
    const proc1 = createMockProcess(1001);
    const proc2 = createMockProcess(1002);
    mockSpawn.mockReturnValueOnce(proc1).mockReturnValueOnce(proc2);

    await pool.spawn(baseConfig);

    proc1.stderr.write('rate limit hit\n');
    await new Promise(r => setTimeout(r, 100));

    const fallbackProc = pool.get('member-000');
    expect(fallbackProc).toBeDefined();
    expect(fallbackProc!.restartCount).toBe(0);
  });
});

describe('single token mode (no fallback configured)', () => {
  let pool: ReturnType<typeof createPool>;

  beforeEach(() => {
    mockSpawn = vi.fn();
    pool = createPool({
      cleanupIntervalMs: 999999,
      primaryToken: PRIMARY_TOKEN,
      // No fallbackToken
      ccMode: 'print',
    });
  });

  afterEach(() => {
    pool.dispose();
    vi.restoreAllMocks();
  });

  it('does NOT attempt fallback when no fallbackToken configured', async () => {
    const proc1 = createMockProcess(1001);
    mockSpawn.mockReturnValueOnce(proc1);

    await pool.spawn(baseConfig);
    proc1.stderr.write('rate limit exceeded\n');
    await new Promise(r => setTimeout(r, 100));

    // Only the initial spawn — no fallback attempt
    expect(mockSpawn).toHaveBeenCalledTimes(1);
  });
});

describe('fallback also fails — normal crash flow', () => {
  let pool: ReturnType<typeof createPool>;

  beforeEach(() => {
    mockSpawn = vi.fn();
    pool = createPool({
      cleanupIntervalMs: 999999,
      maxRestartsPerHour: 3,
      primaryToken: PRIMARY_TOKEN,
      fallbackToken: FALLBACK_TOKEN,
      ccMode: 'print',
    });
  });

  afterEach(() => {
    pool.dispose();
    vi.restoreAllMocks();
  });

  it('does not double-switch when already on fallback tier', async () => {
    const proc1 = createMockProcess(1001);
    const proc2 = createMockProcess(1002);
    mockSpawn.mockReturnValueOnce(proc1).mockReturnValueOnce(proc2);

    await pool.spawn(baseConfig);

    // First: trigger fallback (primary → fallback)
    proc1.stderr.write('rate limit\n');
    await new Promise(r => setTimeout(r, 100));

    expect(mockSpawn).toHaveBeenCalledTimes(2);

    // Second: rate limit again on fallback — should NOT spawn a third time
    proc2.stderr.write('rate limit\n');
    await new Promise(r => setTimeout(r, 100));

    // Still only 2 spawns (no circular fallback)
    expect(mockSpawn).toHaveBeenCalledTimes(2);
  });
});

describe('primary token recovery on new spawn', () => {
  let pool: ReturnType<typeof createPool>;

  beforeEach(() => {
    mockSpawn = vi.fn();
    pool = createPool({
      cleanupIntervalMs: 999999,
      primaryToken: PRIMARY_TOKEN,
      fallbackToken: FALLBACK_TOKEN,
      ccMode: 'print',
    });
  });

  afterEach(() => {
    pool.dispose();
    vi.restoreAllMocks();
  });

  it('resets to primary token on non-resume spawn', async () => {
    const proc1 = createMockProcess(1001);
    const proc2 = createMockProcess(1002);
    const proc3 = createMockProcess(1003);
    mockSpawn
      .mockReturnValueOnce(proc1)
      .mockReturnValueOnce(proc2)
      .mockReturnValueOnce(proc3);

    await pool.spawn(baseConfig);

    // Trigger fallback
    proc1.stderr.write('rate limit\n');
    await new Promise(r => setTimeout(r, 100));

    // Now kill the fallback process
    await pool.kill('member-000');

    // Spawn again (non-resume) — should use primary
    await pool.spawn({ ...baseConfig, resume: false });

    const thirdEnv = mockSpawn.mock.calls[2][2].env;
    expect(thirdEnv['CLAUDE_CODE_OAUTH_TOKEN']).toBe(PRIMARY_TOKEN);
  });
});

describe('spawn injects primary token by default', () => {
  it('sets CLAUDE_CODE_OAUTH_TOKEN to primaryToken in child env', async () => {
    mockSpawn = vi.fn();
    const pool = createPool({
      cleanupIntervalMs: 999999,
      primaryToken: PRIMARY_TOKEN,
      fallbackToken: FALLBACK_TOKEN,
      ccMode: 'print',
    });

    const proc = createMockProcess();
    mockSpawn.mockReturnValue(proc);

    await pool.spawn(baseConfig);

    const env = mockSpawn.mock.calls[0][2].env;
    expect(env['CLAUDE_CODE_OAUTH_TOKEN']).toBe(PRIMARY_TOKEN);

    pool.dispose();
  });
});

describe('allowed rate_limit_event', () => {
  let pool: ReturnType<typeof createPool>;

  beforeEach(() => {
    mockSpawn = vi.fn();
    vi.stubEnv('CLAUDE_BIN', 'claude');
    pool = createPool({
      cleanupIntervalMs: 999999,
      primaryToken: PRIMARY_TOKEN,
    });
  });

  afterEach(() => {
    pool.dispose();
    vi.restoreAllMocks();
  });

  it('keeps the busy turn on the current process', async () => {
    const proc1 = createMockProcess(1001);
    mockSpawn.mockReturnValueOnce(proc1);

    await pool.spawn(baseConfig);
    await pool.send(baseConfig.tenantId, { type: 'user', content: 'hello' } as StreamJsonMessage);
    proc1.stdout.write(JSON.stringify({
      type: 'rate_limit_event',
      rate_limit_info: { status: 'allowed', rateLimitType: 'five_hour' },
    }) + '\n');
    await new Promise(r => setTimeout(r, 100));

    expect(mockSpawn).toHaveBeenCalledTimes(1);
    expect(pool.get(baseConfig.tenantId)?.state).toBe('busy');
  });
});

// Pool behavior is isolated from OS process ownership, tested with real processes separately.
vi.mock('../src/owned-process.js', async () => ({
  spawnOwnedProcess: (await import('node:child_process')).spawn,
}));
