import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { EventEmitter } from 'node:events';
import { PassThrough } from 'node:stream';
import { createPool } from '../src/pool.js';
import type { SpawnConfig, StreamJsonMessage } from '../src/types.js';

interface MockProcess extends EventEmitter {
  stdin: PassThrough;
  stdout: PassThrough;
  stderr: PassThrough;
  pid: number;
  kill: ReturnType<typeof vi.fn>;
  killed: boolean;
}

function createMockProcess(pid = 4321): MockProcess {
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

const aki: SpawnConfig = {
  tenantId: 'owner:aki',
  workspace: '/opt/PULSE/PARA/aki-space/',
  sessionId: 'aki-repro-session',
  ccModel: 'opus',
};

async function flush(): Promise<void> {
  await new Promise((r) => setTimeout(r, 20));
}

describe('429 mid-turn does not crash the relay (Feishu Generating-stuck repro)', () => {
  let pool: ReturnType<typeof createPool>;

  beforeEach(() => {
    mockSpawn = vi.fn();
    pool = createPool({ cleanupIntervalMs: 999999, ccMode: 'print' });
  });

  afterEach(() => {
    pool.dispose();
    vi.restoreAllMocks();
  });

  // Drive aki to busy, then push the exact 429 signals CC prints to stdout while
  // mid-turn. The production crash was an uncaught `.toLowerCase()` of undefined
  // fired synchronously off these lines (journald: "429 retry detected inside CC"
  // immediately followed by "fatal: Uncaught exception ... toLowerCase").
  for (const rateLine of [
    '{"type":"system","subtype":"api_retry","error_status":429}',
    '{"type":"rate_limit_event","rate_limit_info":{"status":"throttled"}}',
    // Same events but with model/usage fields stripped (what fallback resolution sees).
    '{"type":"system","subtype":"api_retry","error_status":429,"usage":{}}',
  ]) {
    it(`survives 429 stdout line: ${rateLine.slice(0, 48)}`, async () => {
      const proc = createMockProcess();
      mockSpawn.mockReturnValue(proc);

      // Wire a production-shaped 'response' listener (mirrors index.ts / channel handlers).
      const responses: StreamJsonMessage[] = [];
      pool.on('response', (_poolKey: string, msg: StreamJsonMessage) => {
        responses.push(msg);
      });
      pool.on('error', () => { /* channels swallow pool errors */ });

      const spawnPromise = pool.spawn(aki);
      proc.stdout.write('{"type":"system","content":"ready"}\n');
      await spawnPromise;

      // Send a user message → tenant goes busy.
      await pool.send('owner:aki', { type: 'human', content: '你好' });
      expect(pool.get('owner:aki')?.state).toBe('busy');

      // Now the 429 arrives mid-turn. This is the line that crashed production.
      proc.stdout.write(rateLine + '\n');
      await flush();

      // If we got here without an uncaught throw, the bug is fixed. The process
      // must still be alive (not killed) and serving.
      expect(pool.get('owner:aki')).toBeDefined();
    });
  }
});

// Pool behavior is isolated from OS process ownership, tested with real processes separately.
vi.mock('../src/owned-process.js', async () => ({
  spawnOwnedProcess: (await import('node:child_process')).spawn,
}));
