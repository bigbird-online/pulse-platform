/**
 * Bug fix tests for the rate-limit infinite respawn loop documented in
 * vultr-pulse-space/memo/2026-06-17-ken-rate-limit-infinite-respawn-loop.md.
 *
 * Production incident (2026-06-17, Vultr Ken & Reeve bots):
 *
 *   - Anthropic opus quota stayed tight; agents kept hitting 429.
 *   - The full fleet was pinned to opus.
 *   - handleRateLimitRestart had no maxRetry — backoff just kept escalating
 *     to the 600s cap and respawning forever as long as lastInFlight was set.
 *   - CC SDK responded under sustained 429 with synthetic stub messages:
 *       message.model === '<synthetic>'
 *       content[0].text === 'No response requested.'
 *       stop_reason === 'stop_sequence'
 *     Relay's stream handler did not recognize the stub, so the assistant
 *     was streamed to Feishu (cards showed "No response requested.") and
 *     the turn never produced a result event → finishTurn never cleared
 *     lastInFlight → backoff timer woke up to the same message → loop.
 *
 * Fixes covered:
 *   A. Synthetic stub assistant messages are dropped before counting toward
 *      turn progress, before resetting the busy timer, and before any
 *      emit('response') so channels never display the stub text.
 *   B. handleRateLimitRestart caps retries at RATE_LIMIT_MAX_RETRY=8; once
 *      exhausted it rejects pending messages, clears in-flight state, and
 *      emits session-end{type:'rate-limited'} instead of respawning again.
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
  sessionId: 'ken-loop-fix',
  ccModel: 'opus',
};

async function flush(ms = 40): Promise<void> {
  await new Promise((r) => setTimeout(r, ms));
}

// Wait until `cond()` holds, polling instead of sleeping a fixed span.
// The retry-cap loop below is driven by real timers, so on a busy box (this
// host has 1 core; a full `vitest run` saturates it) a fixed wall-clock wait
// expires before the loop finishes and the test fails for lack of CPU rather
// than for a real defect. Poll to a generous ceiling instead.
async function waitFor(cond: () => boolean, timeoutMs = 30000): Promise<void> {
  const deadline = Date.now() + timeoutMs;
  while (!cond() && Date.now() < deadline) {
    await flush(10);
  }
}

describe('pool — synthetic stub & rate-limit retry cap', () => {
  let pool: ReturnType<typeof createPool>;

  beforeEach(() => {
    mockSpawn = vi.fn();
  });

  afterEach(() => {
    pool?.dispose();
    vi.restoreAllMocks();
  });

  it('A: synthetic assistant stub is dropped (no response emitted, no turn counted)', async () => {
    pool = createPool({
      cleanupIntervalMs: 999999,
      ccMode: 'print',
    });

    const proc1 = createMockProcess(9301);
    mockSpawn.mockReturnValueOnce(proc1);

    await pool.spawn(ken);
    void pool.send('owner:ken', { type: 'human', content: 'hi' }).catch(() => {});
    await flush(5);

    const responses: unknown[] = [];
    pool.on('response', (_t: string, msg: unknown) => { responses.push(msg); });

    // CC SDK emits a synthetic stub (rate-limited internal state).
    proc1.stdout.write(JSON.stringify({
      type: 'assistant',
      message: {
        model: '<synthetic>',
        role: 'assistant',
        stop_reason: 'stop_sequence',
        content: [{ type: 'text', text: 'No response requested.' }],
      },
    }) + '\n');
    await flush(20);

    // Stub must NOT be emitted as a response to the channel.
    expect(responses).toHaveLength(0);
    // Turn stays busy; lastInFlight remains so backoff/replay can recover.
    expect(pool.get('owner:ken')?.state).toBe('busy');
  });

  it('B: real assistant message after a synthetic stub still flows through', async () => {
    pool = createPool({
      cleanupIntervalMs: 999999,
      ccMode: 'print',
    });

    const proc1 = createMockProcess(9302);
    mockSpawn.mockReturnValueOnce(proc1);

    await pool.spawn(ken);
    void pool.send('owner:ken', { type: 'human', content: 'hi' }).catch(() => {});
    await flush(5);

    const responses: Array<{ model?: string; content?: unknown }> = [];
    pool.on('response', (_t: string, msg: unknown) => {
      const m = (msg as { message?: { model?: string; content?: unknown } }).message;
      if (m) responses.push(m);
    });

    proc1.stdout.write(JSON.stringify({
      type: 'assistant',
      message: {
        model: '<synthetic>',
        role: 'assistant',
        stop_reason: 'stop_sequence',
        content: [{ type: 'text', text: 'No response requested.' }],
      },
    }) + '\n');
    proc1.stdout.write(JSON.stringify({
      type: 'assistant',
      message: {
        model: 'claude-opus-4-7',
        role: 'assistant',
        stop_reason: 'end_turn',
        content: [{ type: 'text', text: 'real reply' }],
      },
    }) + '\n');
    await flush(20);

    expect(responses).toHaveLength(1);
    expect(responses[0]?.model).toBe('claude-opus-4-7');
  });

  it('D: real-model "No response requested." stub is also dropped (session-poisoning fix)', async () => {
    // Production case (2026-06-17, Reeve & Ken on Vultr):
    //   On --resume with a jsonl tail that has an unfinished tool_use turn,
    //   CC SDK auto-injects user: "Continue from where you left off." and the
    //   real model (claude-opus-4-7) replies with text="No response requested."
    //   stop_reason="end_turn". If this gets streamed to the channel + recorded
    //   to jsonl, opus learns "this session means don't respond" and every
    //   subsequent user message returns the same noop → session poisoning.
    //   Fix: drop any assistant message whose sole text block is exactly
    //   "No response requested.", regardless of model or stop_reason.
    pool = createPool({
      cleanupIntervalMs: 999999,
      ccMode: 'print',
    });

    const proc1 = createMockProcess(9303);
    mockSpawn.mockReturnValueOnce(proc1);

    await pool.spawn(ken);
    void pool.send('owner:ken', { type: 'human', content: 'hi' }).catch(() => {});
    await flush(5);

    const responses: unknown[] = [];
    pool.on('response', (_t: string, msg: unknown) => { responses.push(msg); });

    // Real model replies "No response requested." to CC SDK's auto-injected
    // resume message — must be dropped just like the synthetic stub.
    proc1.stdout.write(JSON.stringify({
      type: 'assistant',
      message: {
        model: 'claude-opus-4-7',
        role: 'assistant',
        stop_reason: 'end_turn',
        content: [{ type: 'text', text: 'No response requested.' }],
      },
    }) + '\n');
    await flush(20);

    expect(responses).toHaveLength(0);
    // The next real assistant message (different text) must still flow through.
    proc1.stdout.write(JSON.stringify({
      type: 'assistant',
      message: {
        model: 'claude-opus-4-7',
        role: 'assistant',
        stop_reason: 'end_turn',
        content: [{ type: 'text', text: 'actual answer to user' }],
      },
    }) + '\n');
    await flush(20);

    expect(responses).toHaveLength(1);
  });

  it('C: rate-limit backoff stops after RATE_LIMIT_MAX_RETRY and emits session-end', async () => {
    pool = createPool({
      cleanupIntervalMs: 999999,
      rateLimitRestartDelayMs: 1, // collapse backoff to ms-scale
      rateLimitAbortTimeoutMs: 1,
      ccMode: 'print',
    });

    const sessionEnds: Array<{ type: string; retryCount?: number }> = [];
    pool.on('session-end', (_t: string, reason: { type: string; retryCount?: number }) => {
      sessionEnds.push(reason);
    });

    // Plenty of mock processes — each one immediately self-429s once Relay
    // sends a message through stdin (initial send or replay). This forces
    // handleRateLimitRestart to loop until the cap kicks in.
    const procs: MockProcess[] = [];
    mockSpawn.mockImplementation(() => {
      const proc = createMockProcess(9400 + procs.length);
      procs.push(proc);
      // Whenever Relay writes to stdin (send / replay), kick off a 429 a tick later.
      proc.stdin.on('data', () => {
        setImmediate(() => {
          if (!proc.killed) {
            proc.stdout.write('{"type":"system","subtype":"api_retry","error_status":429,"error":"rate_limit"}\n');
          }
        });
      });
      return proc;
    });

    await pool.spawn(ken);
    void pool.send('owner:ken', { type: 'human', content: 'go' }).catch(() => {});

    // Wait for the loop to reach the cap. Each iteration: stdin write →
    // microtask → 429 stdout → 1ms abort → kill → exit → handleRateLimitRestart
    // → 1ms backoff → spawn → doSend(replay) → stdin write. Poll on the
    // terminal condition rather than sleeping a fixed span — see waitFor.
    await waitFor(() => sessionEnds.some((e) => e.type === 'rate-limited'));

    // After the cap: session-end emitted with type 'rate-limited'.
    expect(sessionEnds.some((e) => e.type === 'rate-limited')).toBe(true);
    // Total spawns = 1 initial + RATE_LIMIT_MAX_RETRY respawns = 9. After that
    // the cap path returns without calling spawnProcess.
    expect(procs.length).toBe(9);
  });
});

// Pool behavior is isolated from OS process ownership, tested with real processes separately.
vi.mock('../src/owned-process.js', async () => ({
  spawnOwnedProcess: (await import('node:child_process')).spawn,
}));
