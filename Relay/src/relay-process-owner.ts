import { existsSync, readFileSync, statSync, unlinkSync } from 'node:fs';
import { execFileSync } from 'node:child_process';
import { relayChildEnv } from './relay-secrets.js';

/** Linux procps can underflow etime for a process born within the current clock
 * tick (observed as 441077234 days). Compare the kernel's fractional boot clock
 * and start ticks directly instead of trusting that formatted observation. */
function elapsedSeconds(pid: number): number | undefined {
  if (process.platform === 'linux') {
    const hz = Number(execFileSync('getconf', ['CLK_TCK'], { encoding: 'utf8', timeout: 5_000, env: relayChildEnv() }).trim());
    const record = readFileSync(`/proc/${pid}/stat`, 'utf8');
    const start = Number(record.slice(record.lastIndexOf(')') + 1).trim().split(/\s+/)[19]);
    const uptime = Number(readFileSync('/proc/uptime', 'utf8').trim().split(/\s+/)[0]);
    if (!Number.isSafeInteger(hz) || hz <= 0 || !Number.isSafeInteger(start) || start < 0
        || !Number.isFinite(uptime) || uptime < start / hz) return undefined;
    return uptime - start / hz;
  }
  const elapsed = execFileSync('ps', ['-p', String(pid), '-o', 'etime='], {
    encoding: 'utf8', timeout: 5_000, env: { ...relayChildEnv(), LC_ALL: 'C' },
  }).trim();
  const match = /^(?:(\d+)-)?(?:(\d+):)?(\d+):(\d+)$/.exec(elapsed);
  if (!match) return undefined;
  const seconds = Number(match[1] ?? 0) * 86400 + Number(match[2] ?? 0) * 3600
    + Number(match[3]) * 60 + Number(match[4]);
  return Number.isSafeInteger(seconds) ? seconds : undefined;
}

/** A surviving PID record cannot own a process born after it was written.
 * Native elapsed time avoids timezone/DST parsing of a display start timestamp.
 * Rounded seconds and command latency are bounded conservatively. Unknown
 * observations never permit replacement, and no signal other than probe 0 is sent. */
function recordPredatesLiveProcess(pidFile:string,pid:number,text:string):boolean {
  const record=statSync(pidFile),before=Date.now();
  const seconds=elapsedSeconds(pid);
  if(seconds===undefined||Date.now()<before||record.mtimeMs>=before-(seconds+2)*1000)return false;
  // An owner may have replaced its record while ps was running. Fail closed
  // instead of applying an observation to that new owner.
  const current=statSync(pidFile);
  return current.dev===record.dev&&current.ino===record.ino&&current.mtimeMs===record.mtimeMs
    &&readFileSync(pidFile,'utf8').trim()===text;
}

/** Startup must never terminate a process identified only by a reused PID or
 * command-line pattern. This boundary recognizes provably stale records,
 * refuses a possible live owner and removes only our own PID. Child-process
 * cleanup is a separate runtime/service contract, not implied by this check. */
export function assertRelayStopped(pidFile: string): void {
  if (!existsSync(pidFile)) return;
  const text = readFileSync(pidFile, 'utf8').trim();
  if (!/^[1-9]\d*$/.test(text)) return;
  const pid = Number(text);
  if (!Number.isSafeInteger(pid)) return;
  try {
    process.kill(pid, 0);
  } catch (error) {
    if ((error as NodeJS.ErrnoException).code === 'ESRCH') return;
    throw error;
  }
  if(recordPredatesLiveProcess(pidFile,pid,text))return;
  throw new Error(`Relay PID file refers to live process ${pid}; stop it through its service manager before starting Relay`);
}

export function removeOwnedRelayPid(pidFile: string, pid = process.pid): void {
  try {
    if (readFileSync(pidFile, 'utf8').trim() === String(pid)) unlinkSync(pidFile);
  } catch (error) {
    if ((error as NodeJS.ErrnoException).code !== 'ENOENT') throw error;
  }
}
