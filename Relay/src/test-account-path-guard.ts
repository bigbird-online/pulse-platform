import { realpathSync } from 'node:fs';
import { tmpdir, userInfo } from 'node:os';
import { isAbsolute, relative, resolve } from 'node:path';

/**
 * Account-level Host state (human inputs, recovery projections) falls back to
 * the system account's home, which `userInfo()` reads regardless of HOME. A
 * test that forgets its own directory would therefore write real records.
 * Under Vitest such a path is refused and recorded, so the test fails even
 * where the caller swallows the error. Outside Vitest the path is unchanged.
 */
const store = globalThis as { [key: symbol]: string[] | undefined };
const KEY = Symbol.for('relay.test.accountStatePathViolations');
const violations: string[] = store[KEY] ??= [];

function inside(child: string, parent: string): boolean {
  const path = relative(parent, child);
  return path === '' || (!path.startsWith('..') && !isAbsolute(path));
}

function real(path: string): string {
  try { return realpathSync(path); } catch { return path; }
}

export function guardAccountStatePath(path: string): string {
  if (!process.env['VITEST']) return path;
  const target = resolve(path);
  const temp = tmpdir();
  if (!inside(target, userInfo().homedir) || inside(target, temp) || inside(target, real(temp))) return path;
  violations.push(target);
  throw new Error('Test refused account-level Host state path: ' + target);
}

/** Return and clear the refused paths; the Vitest setup fails any test with one. */
export function takeAccountStatePathViolations(): string[] {
  return violations.splice(0);
}
