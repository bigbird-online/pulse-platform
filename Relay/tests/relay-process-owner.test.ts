import { afterEach, describe, expect, it, vi } from 'vitest';
import { mkdtempSync, readFileSync, writeFileSync, rmSync, existsSync, utimesSync, symlinkSync } from 'node:fs';
import { spawn } from 'node:child_process';
import { once } from 'node:events';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { assertRelayStopped, removeOwnedRelayPid } from '../src/relay-process-owner.js';

describe('Relay process ownership', () => {
  const roots: string[] = [];
  function pidFile(): string {
    const root = mkdtempSync(join(tmpdir(), 'relay-owner-'));
    roots.push(root);
    return join(root, 'relay.pid');
  }
  afterEach(() => { vi.unstubAllEnvs(); for (const root of roots.splice(0)) rmSync(root, { recursive: true }); });

  it('refuses a live PID without terminating it or overwriting its record', async () => {
    const file = pidFile();
    const child = spawn(process.execPath, ['-e', 'setInterval(() => {}, 1000)'], { stdio: 'ignore' });
    await once(child, 'spawn');
    try {
      writeFileSync(file, String(child.pid));
      expect(() => assertRelayStopped(file)).toThrow('service manager');
      expect(readFileSync(file, 'utf8')).toBe(String(child.pid));
      expect(() => process.kill(child.pid!, 0)).not.toThrow();
    } finally {
      const closed = once(child, 'close');
      child.kill();
      await closed;
    }
    expect(() => assertRelayStopped(file)).not.toThrow();
  });

  it('does not discover or kill unrelated stream-json processes', async () => {
    const child = spawn(process.execPath, ['-e', 'setInterval(() => {}, 1000)', '--',
      'stream-json', '--dangerously-skip-permissions'], { stdio: 'ignore' });
    await once(child, 'spawn');
    try {
      assertRelayStopped(pidFile());
      expect(() => process.kill(child.pid!, 0)).not.toThrow();
    } finally {
      const closed = once(child, 'close');
      child.kill();
      await closed;
    }
  });

  it('allows recovery when a retained PID record predates the live process using that PID', async () => {
    const file=pidFile();
    const child=spawn(process.execPath,['-e','setInterval(() => {}, 1000)'],{stdio:'ignore'});
    await once(child,'spawn');
    try{
      writeFileSync(file,String(child.pid));
      const old=new Date(Date.now()-60_000);utimesSync(file,old,old);
      expect(()=>assertRelayStopped(file)).not.toThrow();
      expect(readFileSync(file,'utf8')).toBe(String(child.pid));
      expect(()=>process.kill(child.pid!,0)).not.toThrow();
    }finally{
      const closed=once(child,'close');child.kill();await closed;
    }
  });

  it('keeps a possible owner when the native start observation is unavailable',async()=>{
    const file=pidFile();
    const child=spawn(process.execPath,['-e','setInterval(() => {}, 1000)'],{stdio:'ignore'});
    await once(child,'spawn');
    try{
      writeFileSync(file,String(child.pid));
      const old=new Date(Date.now()-60_000);utimesSync(file,old,old);
      vi.stubEnv('PATH','/unavailable-native-tools');
      expect(()=>assertRelayStopped(file)).toThrow();
      expect(readFileSync(file,'utf8')).toBe(String(child.pid));
      expect(()=>process.kill(child.pid!,0)).not.toThrow();
    }finally{
      const closed=once(child,'close');child.kill();await closed;
    }
  });

  it.runIf(process.platform === 'linux')('uses kernel birth time despite the reproduced procps elapsed-time underflow', async () => {
    const file = pidFile();
    const bin = mkdtempSync(join(tmpdir(), 'relay-owner-tools-')); roots.push(bin);
    writeFileSync(join(bin, 'ps'), '#!/bin/sh\nprintf "441077234-00:18:40\\n"\n', { mode: 0o700 });
    symlinkSync('/usr/bin/getconf', join(bin, 'getconf'));
    vi.stubEnv('PATH', bin);
    const child = spawn(process.execPath, ['-e', 'setInterval(() => {}, 1000)'], { stdio: 'ignore' });
    await once(child, 'spawn');
    try {
      writeFileSync(file, String(child.pid));
      expect(() => assertRelayStopped(file)).toThrow('service manager');
      const old = new Date(Date.now() - 60_000); utimesSync(file, old, old);
      expect(() => assertRelayStopped(file)).not.toThrow();
      expect(readFileSync(file, 'utf8')).toBe(String(child.pid));
      expect(() => process.kill(child.pid!, 0)).not.toThrow();
    } finally {
      const closed = once(child, 'close'); child.kill(); await closed;
    }
  });

  it('does not remove a successor PID during late shutdown', () => {
    const file = pidFile();
    writeFileSync(file, '222');
    removeOwnedRelayPid(file, 111);
    expect(readFileSync(file, 'utf8')).toBe('222');
    removeOwnedRelayPid(file, 222);
    expect(existsSync(file)).toBe(false);
  });

  it.each(['', '0', '-1', '123garbage', '999999999999999999999'])('never signals malformed PID %j', (value) => {
    const file = pidFile();
    writeFileSync(file, value);
    expect(() => assertRelayStopped(file)).not.toThrow();
  });
});
