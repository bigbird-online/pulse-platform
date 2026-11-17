import { describe, it, expect } from 'vitest';
import { mkdtemp, writeFile } from 'node:fs/promises';
import { join } from 'node:path';
import { tmpdir } from 'node:os';
import { withFileLock } from '../src/file-lock.js';

describe('withFileLock [P0-004]', () => {
  it('serializes two critical sections', async () => {
    const dir = await mkdtemp(join(tmpdir(), 'plf-'));
    const lockPath = join(dir, 't.lock');
    const seq: string[] = [];
    await withFileLock(lockPath, async () => {
      seq.push('a-start');
      await new Promise((r) => setTimeout(r, 20));
      seq.push('a-end');
    });
    await withFileLock(lockPath, async () => {
      seq.push('b');
    });
    expect(seq).toEqual(['a-start', 'a-end', 'b']);
  });

  it('second waiter runs after first releases', async () => {
    const dir = await mkdtemp(join(tmpdir(), 'plf-'));
    const lockPath = join(dir, 't2.lock');
    let overlap = false;
    let inner = 0;
    const p1 = withFileLock(lockPath, async () => {
      inner++;
      await new Promise((r) => setTimeout(r, 40));
      inner--;
      return 1;
    });
    await new Promise((r) => setTimeout(r, 5));
    const p2 = withFileLock(lockPath, async () => {
      if (inner > 0) overlap = true;
      return 2;
    });
    const [a, b] = await Promise.all([p1, p2]);
    expect(a).toBe(1);
    expect(b).toBe(2);
    expect(overlap).toBe(false);
  });
});
