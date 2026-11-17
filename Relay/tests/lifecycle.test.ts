import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { EventEmitter } from 'node:events';
import { PassThrough } from 'node:stream';
import { createPool } from '../src/pool.js';
import type { SpawnConfig } from '../src/types.js';

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
let spawnCallCount = 0;

vi.mock('node:child_process', () => ({
  spawn: (...args: unknown[]) => mockSpawn(...args),
}));

const baseConfig: SpawnConfig = {
  tenantId: 'member-000',
  workspace: '/opt/PULSE/Tenants/member-000/',
  sessionId: 'test-session-uuid',
};

describe('crash restart', () => {
  let pool: ReturnType<typeof createPool>;

  beforeEach(() => {
    spawnCallCount = 0;
    mockSpawn = vi.fn(() => {
      spawnCallCount++;
      const proc = createMockProcess(1000 + spawnCallCount);
      // Auto-emit ready after short delay
      setTimeout(() => proc.stdout.write('{"type":"system","content":"ready"}\n'), 5);
      return proc;
    });
    pool = createPool({
      cleanupIntervalMs: 999999,
      maxRestartsPerHour: 3,
      ccMode: 'print',
    });
  });

  afterEach(() => {
    pool.dispose();
    vi.restoreAllMocks();
  });

  it('auto-restarts on abnormal exit (non-zero code)', async () => {
    const proc = createMockProcess();
    mockSpawn.mockReturnValueOnce(proc);

    const spawnPromise = pool.spawn(baseConfig);
    proc.stdout.write('{"type":"system","content":"ready"}\n');
    await spawnPromise;

    // Reset mockSpawn for restart
    const restartProc = createMockProcess(5678);
    mockSpawn.mockReturnValueOnce(restartProc);

    // Simulate crash (non-zero exit)
    proc.emit('exit', 137, 'SIGKILL');

    // Wait for restart attempt
    await new Promise((resolve) => setTimeout(resolve, 100));
    restartProc.stdout.write('{"type":"system","content":"ready"}\n');
    await new Promise((resolve) => setTimeout(resolve, 100));

    // Should have spawned again with --session-id (session file doesn't exist on disk
    // in this mock environment, so --resume is avoided to prevent CC crash).
    expect(mockSpawn).toHaveBeenCalledTimes(2);
    const restartArgs = mockSpawn.mock.calls[1][1] as string[];
    expect(restartArgs).toContain('--session-id');
    expect(restartArgs).not.toContain('--resume');
  });

  it('stops restarting after max restarts per hour', async () => {
    // We'll manually trigger crashes
    const procs: MockProcess[] = [];
    mockSpawn.mockImplementation(() => {
      const p = createMockProcess(2000 + procs.length);
      procs.push(p);
      return p;
    });

    // Initial spawn
    const spawnPromise = pool.spawn(baseConfig);
    procs[0].stdout.write('{"type":"system","content":"ready"}\n');
    await spawnPromise;

    // Crash 1
    procs[0].emit('exit', 1, null);
    await new Promise((resolve) => setTimeout(resolve, 50));
    if (procs[1]) procs[1].stdout.write('{"type":"system","content":"ready"}\n');
    await new Promise((resolve) => setTimeout(resolve, 50));

    // Crash 2
    if (procs[1]) procs[1].emit('exit', 1, null);
    await new Promise((resolve) => setTimeout(resolve, 50));
    if (procs[2]) procs[2].stdout.write('{"type":"system","content":"ready"}\n');
    await new Promise((resolve) => setTimeout(resolve, 50));

    // Crash 3
    if (procs[2]) procs[2].emit('exit', 1, null);
    await new Promise((resolve) => setTimeout(resolve, 50));
    if (procs[3]) procs[3].stdout.write('{"type":"system","content":"ready"}\n');
    await new Promise((resolve) => setTimeout(resolve, 50));

    // Crash 4 — should NOT restart (exceeded maxRestartsPerHour=3)
    const spawnCountBefore = mockSpawn.mock.calls.length;
    if (procs[3]) procs[3].emit('exit', 1, null);
    await new Promise((resolve) => setTimeout(resolve, 100));

    // No new spawn after the 4th crash
    // Total spawns: 1 initial + 3 restarts = 4
    expect(mockSpawn.mock.calls.length).toBeLessThanOrEqual(spawnCountBefore);
    expect(pool.get('member-000')).toBeUndefined();
  });

  it('does not restart on normal exit (code 0)', async () => {
    const proc = createMockProcess();
    mockSpawn.mockReturnValueOnce(proc);

    const spawnPromise = pool.spawn(baseConfig);
    proc.stdout.write('{"type":"system","content":"ready"}\n');
    await spawnPromise;

    const callsBefore = mockSpawn.mock.calls.length;
    proc.emit('exit', 0, null);
    await new Promise((resolve) => setTimeout(resolve, 100));

    // No restart on normal exit
    expect(mockSpawn.mock.calls.length).toBe(callsBefore);
  });
});

describe('idle cleanup', () => {
  it('kills idle processes after timeout', async () => {
    const proc = createMockProcess();
    mockSpawn = vi.fn().mockReturnValueOnce(proc);

    vi.mock('node:child_process', () => ({
      spawn: (...args: unknown[]) => mockSpawn(...args),
    }));

    const pool = createPool({
      idleTimeoutMs: 100,       // 100ms for testing
      cleanupIntervalMs: 50,    // check every 50ms
      killTimeoutMs: 50,
      ccMode: 'print',
    });

    const spawnPromise = pool.spawn(baseConfig);
    proc.stdout.write('{"type":"system","content":"ready"}\n');
    await spawnPromise;

    // Make process idle
    await pool.send('member-000', { type: 'human', content: 'test' });
    proc.stdout.write('{"type":"result","total_cost_usd":0.01}\n');
    await new Promise((resolve) => setTimeout(resolve, 50));

    expect(pool.get('member-000')?.state).toBe('idle');

    // Wait for idle timeout + cleanup interval
    await new Promise((resolve) => setTimeout(resolve, 200));

    // Process should be cleaned up
    expect(pool.get('member-000')).toBeUndefined();

    pool.dispose();
  });

  it('does not kill active processes', async () => {
    const proc = createMockProcess();
    mockSpawn = vi.fn().mockReturnValueOnce(proc);

    const pool = createPool({
      idleTimeoutMs: 500,       // 500ms
      cleanupIntervalMs: 50,    // check every 50ms
      ccMode: 'print',
    });

    const spawnPromise = pool.spawn(baseConfig);
    proc.stdout.write('{"type":"system","content":"ready"}\n');
    await spawnPromise;

    // Process is in ready state, not idle
    await new Promise((resolve) => setTimeout(resolve, 150));

    // Should still exist (ready, not idle)
    expect(pool.get('member-000')).toBeDefined();
    expect(pool.get('member-000')?.state).toBe('ready');

    pool.dispose();
  });
});

describe('dispose', () => {
  it('clears cleanup timer and kills all processes', async () => {
    const proc = createMockProcess();
    mockSpawn = vi.fn().mockReturnValueOnce(proc);

    const pool = createPool({ cleanupIntervalMs: 999999, ccMode: 'print' });

    const spawnPromise = pool.spawn(baseConfig);
    proc.stdout.write('{"type":"system","content":"ready"}\n');
    await spawnPromise;

    expect(pool.getAll()).toHaveLength(1);

    pool.dispose();

    // After dispose, processes should be killed
    await new Promise((resolve) => setTimeout(resolve, 100));
    expect(pool.getAll()).toHaveLength(0);
  });
});

// Pool behavior is isolated from OS process ownership, tested with real processes separately.
vi.mock('../src/owned-process.js', async () => ({
  spawnOwnedProcess: (await import('node:child_process')).spawn,
}));
