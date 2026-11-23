/** A conversation proof lives only as long as its process; ownerless proofs are swept at start. */
import { describe, it, expect, vi, afterAll } from 'vitest';
import { spawn } from 'node:child_process';
import { existsSync, mkdirSync, mkdtempSync, readFileSync, realpathSync, rmSync, statSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';

const data = realpathSync(mkdtempSync(join(tmpdir(), 'relay-proof-cleanup-')));
vi.mock('../src/managed-paths.js', () => ({ managedRelayPath: () => data }));
vi.mock('../src/native-work-item-command.js', () => ({
  nodeWorkItemCore: () => ({ python: '/usr/bin/python3', entrypoint: '/x/harness.pyz', extension: '/x/runtime/pi.mjs' }),
}));
const { forgetNativeProofOnExit, nativeProcessStamp, observeNativeSession, retainNativeConversation, sweepNativeSessionProofs } =
  await import('../src/native-core-runtime.js');

const proofs = join(data, 'native-session-proofs');
const path = (pid: number) => join(proofs, `${pid}.json`);

function sleeper() {
  const child = spawn(process.execPath, ['-e', 'setTimeout(() => {}, 30000)'], { stdio: 'ignore' });
  const exited = new Promise<void>(resolve => child.once('exit', () => resolve()));
  return { child, exited };
}

function record(pid: number, change: Record<string, unknown> = {}) {
  mkdirSync(proofs, { recursive: true, mode: 0o700 });
  writeFileSync(path(pid), JSON.stringify({ schema: 'pulse.native-session-source/v1', runtime: 'cc',
    process: nativeProcessStamp(pid), relay_pid: process.pid, session_id: 'session-a',
    tenant: 'owner:pulse', entry: 'telegram', ...change }) + '\n', { mode: 0o600 });
}

describe('native session proof cleanup', () => {
  afterAll(() => rmSync(data, { recursive: true, force: true }));

  it('removes a conversation proof when its process exits', async () => {
    const { child, exited } = sleeper();
    forgetNativeProofOnExit(child);
    observeNativeSession(child.pid!, 'session-a', 'owner:pulse', 'cc');
    // A later observation of the same process replaces the one to remove.
    observeNativeSession(child.pid!, 'session-b', 'owner:pulse', 'cc');
    expect(JSON.parse(readFileSync(path(child.pid!), 'utf8')).session_id).toBe('session-b');
    child.kill();
    await exited;
    expect(existsSync(path(child.pid!))).toBe(false);
  });

  it('keeps a proof another writer replaced before the process exited', async () => {
    const { child, exited } = sleeper();
    forgetNativeProofOnExit(child);
    observeNativeSession(child.pid!, 'session-a', 'owner:pulse', 'cc');
    record(child.pid!, { relay_pid: process.pid + 1 });
    child.kill();
    await exited;
    expect(existsSync(path(child.pid!))).toBe(true);
    rmSync(path(child.pid!));
  });

  it('sweeps only proofs with no live process and Relay', async () => {
    const { child, exited } = sleeper();
    const ended = child.pid!;
    child.kill();
    await exited;
    record(process.pid);
    record(process.ppid, { relay_pid: process.ppid });
    // The process has exited.
    writeFileSync(path(ended), JSON.stringify({ process: { pid: ended }, relay_pid: process.pid }), { mode: 0o600 });
    const other = join(proofs, 'notes.json');
    writeFileSync(other, '{}', { mode: 0o600 });
    expect(sweepNativeSessionProofs()).toBe(1);
    expect(existsSync(path(ended))).toBe(false);
    // Live process and this Relay; live process and another live Relay; not a proof name.
    expect([existsSync(path(process.pid)), existsSync(path(process.ppid)), existsSync(other)])
      .toEqual([true, true, true]);
    // Another process under a reused PID.
    record(process.pid, { process: { pid: process.pid, started: 'earlier' } });
    expect(sweepNativeSessionProofs()).toBe(1);
    // A live process whose Relay has exited.
    record(process.pid, { relay_pid: ended });
    expect(sweepNativeSessionProofs()).toBe(1);
    expect(existsSync(path(process.pid))).toBe(false);
  });

  it('keeps one retained record per agy conversation through process exit and the startup sweep', () => {
    const first = retainNativeConversation('agy-conv-1', 'owner:asobi', 'relay-session')!;
    expect(retainNativeConversation('agy-conv-1', 'owner:asobi', 'relay-session')).toBe(first);
    expect(first.startsWith(join(proofs, 'retained-'))).toBe(true);
    expect(JSON.parse(readFileSync(first, 'utf8'))).toEqual({ schema: 'pulse.native-session-retained/v1', runtime: 'agy',
      session_id: 'agy-conv-1', allocation_id: 'relay-session', tenant: 'owner:asobi', entry: 'telegram', relay_pid: process.pid });
    expect(statSync(first).mode & 0o777).toBe(0o600);
    const second = retainNativeConversation('agy-conv-2', 'owner:asobi:feishu', 'other-session')!;
    expect(second).not.toBe(first);
    expect(sweepNativeSessionProofs()).toBe(0);
    expect([existsSync(first), existsSync(second)]).toEqual([true, true]);
    expect(() => retainNativeConversation('../escape', 'owner:asobi', 'relay-session')).toThrow('Invalid native session source');
    rmSync(first); rmSync(second);
  });

  it('logs an unreadable proof and keeps starting', () => {
    const log = vi.spyOn(console, 'log').mockImplementation(() => {});
    mkdirSync(proofs, { recursive: true, mode: 0o700 });
    writeFileSync(path(process.pid), 'not json', { mode: 0o600 });
    expect(sweepNativeSessionProofs()).toBe(0);
    expect(log).toHaveBeenCalledWith(expect.objectContaining({ msg: 'Native session proof not swept' }));
    log.mockRestore();
    rmSync(path(process.pid));
  });
});
