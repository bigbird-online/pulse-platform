import { describe, it, expect, beforeEach, afterEach } from 'vitest';
import { mkdtempSync, writeFileSync, readFileSync, existsSync, mkdirSync, rmSync } from 'node:fs';
import { spawn } from 'node:child_process';
import { once } from 'node:events';
import { join } from 'node:path';
import { tmpdir } from 'node:os';
import { detectRapidRestart, writeRestartMarker } from '../src/restart-detect.js';
import { formatFatalErrorLog } from '../src/fatal-log.js';

describe('non-serving CLI invocations', () => {
  it.each([
    { args: ['--help'], expectedCode: 0 },
    { args: ['--version'], expectedCode: 0 },
    { args: ['--definitely-invalid'], expectedCode: 2 },
  ])('does not touch the live Relay lifecycle for $args', async ({ args, expectedCode }) => {
    const home = mkdtempSync(join(tmpdir(), 'relay-cli-lifecycle-'));
    const relayDir = join(home, '.relay');
    const pidFile = join(relayDir, 'relay.pid');
    const auditFile = join(relayDir, 'restart-audit.log');
    const sentinel = spawn(process.execPath, ['-e', 'setInterval(() => {}, 1000)'], {
      stdio: 'ignore',
    });
    await once(sentinel, 'spawn');

    try {
      mkdirSync(relayDir, { recursive: true });
      writeFileSync(pidFile, String(sentinel.pid));
      const cli = spawn(process.execPath, [join(process.cwd(), 'dist', 'cli.js'), ...args], {
        env: { ...process.env, HOME: home },
        stdio: 'ignore',
      });
      const timer = setTimeout(() => cli.kill('SIGKILL'), 8_000);
      const [code] = await once(cli, 'close') as [number | null];
      clearTimeout(timer);

      await new Promise(resolve => setTimeout(resolve, 50));
      expect(code).toBe(expectedCode);
      expect(sentinel.exitCode).toBeNull();
      expect(sentinel.signalCode).toBeNull();
      expect(readFileSync(pidFile, 'utf-8')).toBe(String(sentinel.pid));
      expect(existsSync(auditFile)).toBe(false);
    } finally {
      if (sentinel.exitCode === null && sentinel.signalCode === null) {
        const closed = once(sentinel, 'close');
        sentinel.kill('SIGKILL');
        await closed;
      }
      rmSync(home, { recursive: true, force: true });
    }
  });
});

describe('restart detection', () => {
  let tempDir: string;
  let markerPath: string;

  beforeEach(() => {
    tempDir = mkdtempSync(join(tmpdir(), 'relay-cli-'));
    markerPath = join(tempDir, '.restart-marker');
  });

  afterEach(() => {
    rmSync(tempDir, { recursive: true, force: true });
  });

  describe('writeRestartMarker()', () => {
    it('writes timestamp to marker file', () => {
      writeRestartMarker(markerPath);
      expect(existsSync(markerPath)).toBe(true);
      const data = JSON.parse(readFileSync(markerPath, 'utf-8'));
      expect(data.ts).toBeTypeOf('number');
      expect(Date.now() - data.ts).toBeLessThan(1000);
    });
  });

  describe('detectRapidRestart()', () => {
    it('returns false when marker does not exist', () => {
      expect(detectRapidRestart(markerPath)).toBe(false);
    });

    it('returns true when marker was written < 10s ago', () => {
      writeFileSync(markerPath, JSON.stringify({ ts: Date.now() }));
      expect(detectRapidRestart(markerPath)).toBe(true);
    });

    it('returns false when marker was written > 10s ago', () => {
      writeFileSync(markerPath, JSON.stringify({ ts: Date.now() - 15000 }));
      expect(detectRapidRestart(markerPath)).toBe(false);
    });

    it('returns false when marker contains invalid JSON', () => {
      writeFileSync(markerPath, 'not json');
      expect(detectRapidRestart(markerPath)).toBe(false);
    });
  });
});

describe('formatFatalErrorLog', () => {
  it('includes the stack trace for an Error, not just the message', () => {
    const err = new TypeError("Cannot read properties of undefined (reading 'toLowerCase')");
    const log = formatFatalErrorLog(err);

    expect(log.level).toBe('fatal');
    expect(log.msg).toBe('Uncaught exception');
    // message body preserved
    expect(log.error).toContain('toLowerCase');
    // the type name is part of both toString() and stack, so assert a frame too
    expect(log.error).toMatch(/\n\s*at\b/);
  });

  it('regression: does NOT collapse to String(err) which drops the stack', () => {
    // cli.ts:130 used `String(err)` — that yields "Error: boom" with no stack,
    // making recurring crashes unlocatable. Our formatter must keep the stack.
    const err = new Error('boom');

    expect(String(err)).not.toMatch(/\n\s*at\b/); // sanity: String() really does drop it
    expect(formatFatalErrorLog(err).error).toMatch(/\n\s*at\b/); // ours keeps it
  });

  it('falls back to String() for non-Error throwables', () => {
    expect(formatFatalErrorLog('plain string').error).toBe('plain string');
    expect(formatFatalErrorLog(42).error).toBe('42');
    expect(formatFatalErrorLog(undefined).error).toBe('undefined');
    expect(formatFatalErrorLog(null).error).toBe('null');
    expect(formatFatalErrorLog({ x: 1 }).error).toBe('[object Object]');
  });

  it('falls back to message when an Error has no stack property', () => {
    const err = new Error('no stack here');
    delete err.stack;
    expect(formatFatalErrorLog(err).error).toContain('no stack here');
  });

  it('produces a parseable ISO timestamp', () => {
    const log = formatFatalErrorLog(new Error('x'));
    expect(new Date(log.ts).toISOString()).toBe(log.ts);
  });
});
