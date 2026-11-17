/**
 * Bug repro: same-tenant orphan CC processes from races between
 * rate-limit backoff (handleRateLimitRestart) and new-message spawn paths.
 *
 * Production incident (2026-06-16 12:35–12:40 UTC, Vultr Ken bot):
 *
 *   t=0       429 → handleRateLimitRestart#A entered setTimeout(240_000)
 *   t+101     new feishu message arrives → ensureProcess → spawnProcess
 *             doSend resets rateLimitRetries; new CC started, then also 429
 *   t+119     that proc exited → handleRateLimitRestart#B entered
 *             setTimeout(60_000)
 *   t+241     #A's setTimeout woke up: it deleted the in-memory record
 *             (held #B's child) and spawned a brand-new CC → orphan #1
 *   t+301     #B's setTimeout woke up: spawned again → orphan #2
 *
 * After this fix:
 *   - Any new spawnProcess / non-replay doSend abort all pending backoffs.
 *   - A superseding handleRateLimitRestart aborts the previous controller.
 *   - spawnProcess preempts (kills) any in-memory CCProcess instead of
 *     throwing or silently overwriting it.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { EventEmitter } from 'node:events';
import { PassThrough } from 'node:stream';
import { createPool } from '../src/pool.js';
import type { SpawnConfig } from '../src/types.js';

interface MockProcess extends EventEmitter {
  stdin: PassThrough;
  stdout: PassThrough;
  stderr: PassThrough;
  pid: number;
  kill: ReturnType<typeof vi.fn>;
  killed: boolean;
}

function createMockProcess(pid: number): MockProcess {
  const proc = new EventEmitter() as MockProcess;
  proc.stdin = new PassThrough();
  proc.stdout = new PassThrough();
  proc.stderr = new PassThrough();
  proc.pid = pid;
  proc.killed = false;
  proc.kill = vi.fn((signal?: string) => {
    proc.killed = true;
    // emit exit synchronously so killProcess's `child.on('exit', done)` fires
    proc.emit('exit', signal === 'SIGKILL' ? 137 : 0, signal ?? 'SIGTERM');
    return true;
  });
  return proc;
}

let mockSpawn: ReturnType<typeof vi.fn>;
vi.mock('node:child_process', () => ({
  spawn: (...args: unknown[]) => mockSpawn(...args),
}));

const ken: SpawnConfig = {
  tenantId: 'owner:ken',
  workspace: '/opt/PULSE/PARA/ken-space/',
  sessionId: 'ken-orphan-repro',
  ccModel: 'opus',
};

async function flush(ms = 40): Promise<void> {
  await new Promise((r) => setTimeout(r, ms));
}

describe('pool — rate-limit backoff cannot spawn orphan children', () => {
  let pool: ReturnType<typeof createPool>;

  beforeEach(() => {
    mockSpawn = vi.fn();
  });

  afterEach(() => {
    pool?.dispose();
    vi.restoreAllMocks();
  });

  /**
   * Case A — production repro:
   *
   * 1. Spawn → user sends → 429 → handleRateLimitRestart begins backoff.
   * 2. While the backoff is still sleeping, a NEW user message lands and
   *    sendMessage → doSend fires (non-replay path).
   * 3. doSend must cancel the backoff so when its timer eventually fires
   *    it returns early instead of spawning a second CC.
   *
   * We use a deliberately long override so we can observe the cancellation
   * window: 200ms backoff, second message at +30ms, then wait 400ms and
   * assert no extra spawn happened.
   */
  it('A: new user message in doSend aborts pending rate-limit backoff', async () => {
    pool = createPool({
      cleanupIntervalMs: 999999,
      rateLimitRestartDelayMs: 200, // long enough to observe race
      rateLimitAbortTimeoutMs: 1,
      ccMode: 'print',
    });

    const proc1 = createMockProcess(9001);
    mockSpawn.mockReturnValueOnce(proc1);

    await pool.spawn(ken);
    expect(pool.get('owner:ken')?.state).toBe('ready');

    // Send a turn → busy. lastInFlight is now set, so 429 will trigger backoff.
    void pool.send('owner:ken', { type: 'human', content: 'first' }).catch(() => {});
    await flush(5);
    expect(pool.get('owner:ken')?.state).toBe('busy');

    // 429 mid-turn → rateLimitAbortTimeoutMs=1ms → SIGKILL → exit → handleRateLimitRestart.
    proc1.stdout.write('{"type":"system","subtype":"api_retry","error_status":429,"error":"rate_limit"}\n');
    await flush(30); // give the abort + exit + handleRateLimitRestart entry time to run
    expect(mockSpawn).toHaveBeenCalledTimes(1); // backoff still sleeping; no respawn yet

    // While the 200ms backoff is asleep, a new user message arrives.
    // After 429 abort, killProcess wiped the process map; ensureProcess in
    // production would call spawnProcess. We simulate that here directly.
    const proc2 = createMockProcess(9002);
    mockSpawn.mockReturnValueOnce(proc2);
    await pool.spawn({ ...ken, resume: true });
    expect(pool.get('owner:ken')?.process.pid).toBe(9002);

    // New non-replay turn → doSend cancels the backoff.
    await pool.send('owner:ken', { type: 'human', content: 'second' });

    // Wait long enough for the original 200ms backoff to elapse + safety margin.
    await flush(400);

    // CRITICAL: only the two intentional spawns. No orphan from the backoff
    // timer waking up.
    expect(mockSpawn).toHaveBeenCalledTimes(2);
    expect(pool.get('owner:ken')?.process.pid).toBe(9002);
    expect(proc2.killed).toBe(false);
  });

  /**
   * Case B — two backoff cycles racing on the same tenant only produce one
   * net respawn. The first backoff is superseded by an external spawn (the
   * production race), and when it eventually wakes up it must NOT add a
   * third CC child to the pool.
   *
   * Production timeline this guards:
   *   t+0   handler#A: setTimeout(240s)
   *   t+102 spawnProcess (new user msg) → cancels #A
   *   t+241 #A timer fires → must observe aborted signal, return early
   *
   * Asserts:
   *   - Total spawn count after the original backoff would have fired = 2
   *     (initial + replacement), NOT 3.
   *   - The active in-memory process is the replacement (not orphan).
   */
  it('B: a superseded backoff does not spawn when its timer eventually fires', async () => {
    pool = createPool({
      cleanupIntervalMs: 999999,
      rateLimitRestartDelayMs: 250, // long backoff so we can race a spawn in
      rateLimitAbortTimeoutMs: 1,
      ccMode: 'print',
    });

    const procs = [
      createMockProcess(9101), // initial
      createMockProcess(9102), // replacement (production new-msg path)
      createMockProcess(9103), // would-be orphan from stale backoff (must NOT spawn)
    ];
    mockSpawn
      .mockReturnValueOnce(procs[0]!)
      .mockReturnValueOnce(procs[1]!)
      .mockReturnValueOnce(procs[2]!);

    await pool.spawn(ken);
    void pool.send('owner:ken', { type: 'human', content: 'go' }).catch(() => {});
    await flush(5);

    // 429 mid-turn → kills proc1, enters handleRateLimitRestart's 250ms backoff
    procs[0]!.stdout.write('{"type":"system","subtype":"api_retry","error_status":429,"error":"rate_limit"}\n');
    await flush(30);
    expect(mockSpawn).toHaveBeenCalledTimes(1); // backoff still asleep

    // While the backoff sleeps, simulate the new-message path: spawn a fresh
    // CC for the tenant. spawnProcess MUST cancel the pending backoff.
    await pool.spawn({ ...ken, resume: true });
    expect(pool.get('owner:ken')?.process.pid).toBe(9102);

    // Wait well past the original 250ms backoff window.
    await flush(400);

    // The stale backoff timer fired, but must have observed signal.aborted
    // and returned without spawning. Only two spawns total.
    expect(mockSpawn).toHaveBeenCalledTimes(2);
    expect(pool.get('owner:ken')?.process.pid).toBe(9102);
    // procs[2] was never consumed
    expect(procs[2]!.killed).toBe(false);
  });

  /**
   * Case C — spawnProcess preempts an existing live in-memory process
   * instead of throwing. Reproduces the guard hole at pool.ts:2589 where
   * the backoff path deletes `processes[tenantId]` *before* calling
   * spawnProcess, so the original guard never fires even when a real
   * child is still alive.
   */
  it('C: spawnProcess kills any existing non-dead process and continues', async () => {
    pool = createPool({
      cleanupIntervalMs: 999999,
      ccMode: 'print',
    });

    const proc1 = createMockProcess(9201);
    const proc2 = createMockProcess(9202);
    mockSpawn
      .mockReturnValueOnce(proc1)
      .mockReturnValueOnce(proc2);

    await pool.spawn(ken);
    expect(pool.get('owner:ken')?.process.pid).toBe(9201);
    expect(proc1.killed).toBe(false);

    // Old code: throws 'Process already exists'. New code: kills proc1
    // (logging 'spawnProcess preempting existing process') and spawns proc2.
    await expect(pool.spawn(ken)).resolves.toBeDefined();

    expect(proc1.kill).toHaveBeenCalled();
    expect(proc1.killed).toBe(true);
    expect(pool.get('owner:ken')?.process.pid).toBe(9202);
  });
});

// Pool behavior is isolated from OS process ownership, tested with real processes separately.
vi.mock('../src/owned-process.js', async () => ({
  spawnOwnedProcess: (await import('node:child_process')).spawn,
}));
