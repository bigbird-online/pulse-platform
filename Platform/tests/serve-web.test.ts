import { describe, it, expect } from 'vitest';
import { mkdtempSync, writeFileSync, existsSync } from 'node:fs';
import { join } from 'node:path';
import { tmpdir } from 'node:os';
import type { ServerResponse } from 'node:http';
import { resolveSafeWebPath, tryServeWebRoot } from '../src/serve-web.js';

function makeRes(): ServerResponse & { _body: Buffer[]; _code?: number } {
  const r = {
    _body: [] as Buffer[],
    _code: 200,
    writeHead(code: number, _h?: Record<string, string>) {
      r._code = code;
    },
    end(buf?: Buffer | string) {
      if (buf !== undefined) r._body.push(Buffer.isBuffer(buf) ? buf : Buffer.from(buf));
    },
  };
  return r as ServerResponse & { _body: Buffer[]; _code?: number };
}

describe('serve-web', () => {
  it('resolveSafeWebPath rejects parent segments', () => {
    const root = mkdtempSync(join(tmpdir(), 'sw-'));
    expect(resolveSafeWebPath(root, '/../etc/passwd')).toBeUndefined();
    expect(resolveSafeWebPath(root, '/foo/../../../etc/passwd')).toBeUndefined();
  });

  it('tryServeWebRoot serves / and assets', () => {
    const root = mkdtempSync(join(tmpdir(), 'sw-'));
    writeFileSync(join(root, 'index.html'), '<!doctype html><title>x</title>', 'utf8');
    writeFileSync(join(root, 'a.js'), 'console.log(1)', 'utf8');

    const m1 = makeRes();
    expect(tryServeWebRoot(root, '/', m1)).toBe(true);
    expect(Buffer.concat(m1._body).toString()).toContain('title');

    const m2 = makeRes();
    expect(tryServeWebRoot(root, '/a.js', m2)).toBe(true);
    expect(Buffer.concat(m2._body).toString()).toContain('console');
  });

  it('tryServeWebRoot returns false when webRoot missing', () => {
    const ghost = join(tmpdir(), 'nonexistent-web-root-xyz');
    expect(existsSync(ghost)).toBe(false);
    const m = makeRes();
    expect(tryServeWebRoot(ghost, '/', m)).toBe(false);
  });
});
