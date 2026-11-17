import { spawn, type ChildProcess, type SpawnOptions } from 'node:child_process';
import { userInfo } from 'node:os';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

const launcher = join(dirname(fileURLToPath(import.meta.url)), '..', '..', 'Scripts', 'relay-owned-process.py');

/** Pipe-mode runtimes own a private process group and a separate lifetime socket.
 * The launcher execs the real command: PID, streams, signals and exit status
 * remain native ChildProcess behavior, including a direct SIGKILL.
 */
export function spawnOwnedProcess(
  command: string, args: string[],
  options: Pick<SpawnOptions, 'cwd' | 'env'> & { stdio: ['pipe' | 'ignore', 'pipe', 'pipe'] },
): ChildProcess {
  const python = join(userInfo().homedir, '.local/share/coevolution/python-env/bin/python');
  const child = spawn(python, ['-I', '-S', launcher, command, ...args], {
    ...options, detached: true, stdio: [...options.stdio, 'pipe', 'pipe'],
  });
  const lifetime = child.stdio[3];
  const startup = child.stdio[4];
  let errorText = '';
  startup?.on('data', (chunk: Buffer) => { errorText += chunk.toString(); });
  startup?.on('end', () => {
    if (!errorText) return;
    const failure = JSON.parse(errorText) as { code: string; message: string };
    child.emit('error', Object.assign(new Error(`spawn ${command}: ${failure.message}`), {
      code: failure.code, path: command, spawnargs: args,
    }));
  });
  // A normal command exit also ends ownership; don't keep ChildProcess.close
  // waiting for the guardian's bounded cleanup of any remaining descendants.
  child.once('exit', () => lifetime?.destroy());
  child.once('error', () => lifetime?.destroy());
  return child;
}
