import { afterEach, describe, expect, it } from 'vitest';
import { spawn, execFileSync, type ChildProcess } from 'node:child_process';
import { once } from 'node:events';
import { mkdtempSync, readFileSync, writeFileSync, rmSync, existsSync, realpathSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { spawnOwnedProcess } from '../src/owned-process.js';

const roots: string[] = [];
const children: ChildProcess[] = [];
function root(): string {
  const path = mkdtempSync(join(tmpdir(), 'relay-owned-process-'));
  roots.push(path);
  return path;
}
async function until(predicate: () => boolean): Promise<void> {
  const deadline = Date.now() + 7000;
  while (!predicate() && Date.now() < deadline) await new Promise(r => setTimeout(r, 20));
  expect(predicate()).toBe(true);
}
function running(pid: number): boolean {
  try {
    const state = execFileSync('ps', ['-p', String(pid), '-o', 'stat='], { encoding: 'utf8' }).trim();
    return state.length > 0 && !state.startsWith('Z');
  } catch { return false; }
}
afterEach(() => {
  for (const child of children.splice(0)) child.kill('SIGKILL');
  for (const path of roots.splice(0)) rmSync(path, { recursive: true, force: true });
});

describe('owned pipe-mode runtime processes', () => {
  it('preserves stdin EOF, separate output, cwd, env, PID, and exit status', async () => {
    const cwd = root();
    const child = spawnOwnedProcess(process.execPath, ['-e', `
      let text = ''; process.stdin.on('data', b => text += b);
      process.stdin.on('end', () => setTimeout(() => {
        console.log(JSON.stringify({text, cwd: process.cwd(), pid: process.pid, value: process.env.PROBE}));
        console.error('separate stderr'); process.exitCode = 7;
      }, 150));
    `], { cwd, env: { ...process.env, PROBE: 'owned value' }, stdio: ['pipe', 'pipe', 'pipe'] });
    children.push(child);
    let stdout = ''; let stderr = '';
    child.stdout!.on('data', b => stdout += b);
    child.stderr!.on('data', b => stderr += b);
    const closed = once(child, 'close');
    child.stdin!.end('input survives EOF');
    expect(await closed).toEqual([7, null]);
    expect(JSON.parse(stdout)).toEqual({ text: 'input survives EOF', cwd: realpathSync(cwd), pid: child.pid, value: 'owned value' });
    expect(stderr).toBe('separate stderr\n');
  });

  it('reports an actual exec failure as a spawn error', async () => {
    const child = spawnOwnedProcess('/no-such-pulse-runtime', [], { stdio: ['pipe', 'pipe', 'pipe'] });
    children.push(child);
    const error = await new Promise<NodeJS.ErrnoException>(resolve => child.once('error', resolve));
    expect(error.code).toBe('ENOENT');
    expect(error.message).toContain('/no-such-pulse-runtime');
  });

  it.each(['owner', 'leader'] as const)('reaps TERM-ignoring descendants after %s SIGKILL', async (victim) => {
    const cwd = root();
    const pids = join(cwd, 'pids.json');
    const target = join(cwd, 'target.cjs');
    writeFileSync(target, `
      const { spawn } = require('node:child_process');
      const { writeFileSync } = require('node:fs');
      process.on('SIGTERM', () => {});
      if (process.argv[2] === 'descendant') {
        process.send({pid:process.pid}); setInterval(() => {}, 1000);
      } else {
        const child = spawn(process.execPath, [__filename, 'descendant'], {stdio:['ignore','ignore','ignore','ipc']});
        child.on('message', ({pid}) => writeFileSync(${JSON.stringify(pids)}, JSON.stringify({leader:process.pid, descendant:pid})));
        setInterval(() => {}, 1000);
      }
    `);
    // Test the built consumer, as used by the installed Relay.
    const owner = spawn(process.execPath, ['--input-type=module', '-e', `
      import { spawnOwnedProcess } from ${JSON.stringify(new URL('../dist/owned-process.js', import.meta.url).href)};
      spawnOwnedProcess(process.execPath, [${JSON.stringify(target)}], {stdio:['pipe','pipe','pipe']});
      setInterval(() => {}, 1000);
    `], { stdio: ['ignore', 'pipe', 'pipe'] });
    children.push(owner);
    let startupErrors = '';
    owner.stderr!.on('data', b => startupErrors += b);
    await until(() => existsSync(pids) || owner.exitCode !== null);
    expect(startupErrors).toBe('');
    const ids = JSON.parse(readFileSync(pids, 'utf8')) as {leader:number; descendant:number};
    const unrelated = spawn(process.execPath, ['-e', 'setInterval(() => {}, 1000)'], { stdio: 'ignore' });
    children.push(unrelated);
    await once(unrelated, 'spawn');
    try {
      if (victim === 'owner') owner.kill('SIGKILL');
      else process.kill(ids.leader, 'SIGKILL');
      await until(() => !running(ids.leader) && !running(ids.descendant));
      expect(running(unrelated.pid!)).toBe(true);
    } finally {
      // The fixture owns this private group; no process-name discovery.
      if (running(ids.leader) || running(ids.descendant)) process.kill(-ids.leader, 'SIGKILL');
    }
  }, 15_000);
});
