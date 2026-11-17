import { afterEach, describe, expect, it, vi } from 'vitest';
import { EventEmitter } from 'node:events';
import { readFileSync, readdirSync, statSync } from 'node:fs';
import { join } from 'node:path';

const spawned = vi.hoisted(() => [] as { command: string; env: NodeJS.ProcessEnv | undefined }[]);

vi.mock('node:child_process', async (importOriginal) => {
  const actual = await importOriginal<typeof import('node:child_process')>();
  return {
    ...actual,
    spawn: (command: string, _args: string[], options: { env?: NodeJS.ProcessEnv }) => {
      spawned.push({ command, env: options?.env });
      const child = Object.assign(new EventEmitter(), {
        stdout: new EventEmitter(),
        stdin: { write: () => true, end: () => undefined },
      });
      setImmediate(() => child.emit('close', 1));
      return child;
    },
  };
});

const { transcribeAudio } = await import('../src/stt.js');

const CREDENTIAL = /^(RELAY_BOT_TOKEN_|RELAY_INTERNAL_TOKEN$|RELAY_SECRETS_ENV_FILE$|TELEGRAM_BOT_TOKEN$|OPENROUTER_.*(API_KEY|TOKEN))/;

describe('voice transcription helper', () => {
  const saved = { ...process.env };
  afterEach(() => {
    for (const key of Object.keys(process.env)) if (!(key in saved)) delete process.env[key];
    Object.assign(process.env, saved);
    spawned.length = 0;
  });

  it('starts ffmpeg without Relay credentials', async () => {
    Object.assign(process.env, {
      RELAY_BOT_TOKEN_AKI: '1:fixture-bot',
      RELAY_INTERNAL_TOKEN: 'fixture-internal',
      OPENROUTER_API_KEY: 'fixture-openrouter',
      KEEP_ME: 'k',
    });
    await expect(transcribeAudio(Buffer.from('audio'), 'fixture-oauth')).rejects.toThrow('Audio conversion failed');
    expect(spawned.map(call => call.command)).toEqual(['ffmpeg']);
    const env = spawned[0]!.env!;
    expect(Object.keys(env).filter(key => CREDENTIAL.test(key))).toEqual([]);
    expect(env['KEEP_ME']).toBe('k');
  });
});

/** Every child_process / node-pty call site in Relay/src names its environment, so
 * none silently inherits Relay's process.env (which holds Relay's credentials). */
describe('child process call sites', () => {
  function sources(directory: string): string[] {
    return readdirSync(directory).flatMap((name) => {
      const path = join(directory, name);
      return statSync(path).isDirectory() ? sources(path) : name.endsWith('.ts') ? [path] : [];
    });
  }

  function callText(text: string, open: number): string {
    let depth = 0;
    for (let index = open; index < text.length; index += 1) {
      if (text[index] === '(') depth += 1;
      if (text[index] === ')' && --depth === 0) return text.slice(open, index + 1);
    }
    return text.slice(open);
  }

  it('pass an explicit env (or a caller-supplied options object)', () => {
    const missing: string[] = [];
    let calls = 0;
    for (const path of sources(join(process.cwd(), 'src'))) {
      const text = readFileSync(path, 'utf8');
      const names = new Set<string>();
      for (const match of text.matchAll(/import\s*\{([^}]*)\}\s*from\s*'(node:child_process|@homebridge\/node-pty-prebuilt-multiarch)'/g)) {
        for (const part of match[1]!.split(',')) {
          const item = part.trim();
          if (!item || item.startsWith('type ')) continue;
          const [imported, local] = item.split(/\s+as\s+/);
          if (/^(spawn|spawnSync|exec|execSync|execFile|execFileSync|fork)$/.test(imported!)) names.add((local ?? imported)!);
        }
      }
      for (const name of names) {
        for (const match of text.matchAll(new RegExp(`(?<![.\\w])${name}\\s*\\(`, 'g'))) {
          const before = text.slice(Math.max(0, match.index! - 20), match.index!);
          if (/(function|async)\s*$/.test(before)) continue;
          calls += 1;
          const call = callText(text, match.index! + match[0].length - 1);
          if (!/\benv\b|[oO]ptions\b/.test(call)) {
            missing.push(`${path.slice(process.cwd().length + 1)}:${text.slice(0, match.index!).split('\n').length}`);
          }
        }
      }
    }
    expect(calls).toBeGreaterThan(20);
    expect(missing).toEqual([]);
  });
});
