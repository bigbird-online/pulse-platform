/**
 * Relay slash input (/wi) reaches the native core before CC's first init
 * event. The spawned session id is exact, so the native proof exists at spawn.
 */
import { describe, it, expect, vi, afterEach } from 'vitest';
import { EventEmitter } from 'node:events';
import { PassThrough } from 'node:stream';
import { createPool } from '../src/pool.js';

const observed: unknown[][] = [];
vi.mock('../src/native-core-runtime.js', async (original) => ({
  ...(await original<typeof import('../src/native-core-runtime.js')>()),
  observeNativeSession: (...args: unknown[]) => { observed.push(args); },
}));

let mockSpawn: ReturnType<typeof vi.fn>;
vi.mock('node:child_process', () => ({
  spawn: (...args: unknown[]) => mockSpawn(...args),
}));
vi.mock('../src/owned-process.js', async () => ({
  spawnOwnedProcess: (await import('node:child_process')).spawn,
}));

function child(pid: number) {
  const proc = Object.assign(new EventEmitter(), {
    stdin: new PassThrough(), stdout: new PassThrough(), stderr: new PassThrough(),
    pid, killed: false,
  }) as EventEmitter & { stdin: PassThrough; stdout: PassThrough; stderr: PassThrough;
    pid: number; killed: boolean; kill: ReturnType<typeof vi.fn> };
  proc.kill = vi.fn(() => { proc.killed = true; proc.emit('exit', 0, 'SIGTERM'); return true; });
  return proc;
}

describe('pool — CC native session proof', () => {
  let pool: ReturnType<typeof createPool>;
  afterEach(() => { pool?.dispose(); observed.length = 0; vi.restoreAllMocks(); });

  it('proves the spawned session before its first init event', async () => {
    mockSpawn = vi.fn().mockReturnValueOnce(child(9101));
    pool = createPool({ cleanupIntervalMs: 999999, ccMode: 'print' });
    await pool.spawn({ tenantId: 'owner:emi', workspace: '/tmp/emi-space/', sessionId: 'emi-first-input',
      ccModel: 'opus' });
    expect(observed).toEqual([[9101, 'emi-first-input', 'owner:emi', 'cc']]);
  });
});
