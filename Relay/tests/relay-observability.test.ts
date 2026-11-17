import { afterEach, beforeEach, describe, expect, it } from 'vitest';
import { mkdtempSync, readFileSync, readdirSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import {
  agentFromSessionPath,
  sessionFromPath,
  emitObsEvent,
  emitDegenObserved,
  resolveObsDir,
  isObsEnabled,
} from '../src/relay-observability.js';

describe('agentFromSessionPath', () => {
  it('extracts the post-Coevolution segment as a stable agent key', () => {
    const p = '/home/pulse/.claude/projects/-Users-owner-Coevolution-Bigbird-PULSE-vultr-pulse-space/4d50429b-64bd.jsonl';
    expect(agentFromSessionPath(p)).toBe('Bigbird-PULSE-vultr-pulse-space');
  });

  it('handles other agents (emi / duke / hestia) — same for the same agent every time', () => {
    expect(agentFromSessionPath('/x/-Users-y-Coevolution-Bigbird-LifeLab-emi-space/a.jsonl')).toBe('Bigbird-LifeLab-emi-space');
    expect(agentFromSessionPath('/x/-Users-y-Coevolution-duke-space/b.jsonl')).toBe('duke-space');
    expect(agentFromSessionPath('/x/-Users-y-Coevolution-Bigbird-homeassistant-hestia-space/c.jsonl')).toBe('Bigbird-homeassistant-hestia-space');
  });

  it('is stable — the same path always maps to the same key', () => {
    const p = '/x/-Users-y-Coevolution-duke-space/b.jsonl';
    expect(agentFromSessionPath(p)).toBe(agentFromSessionPath(p));
  });

  it('falls back to the directory name when no -Coevolution- anchor', () => {
    expect(agentFromSessionPath('/x/-Users-weird-dir/c.jsonl')).toBe('Users-weird-dir');
  });

  it('returns undefined for empty/invalid input', () => {
    expect(agentFromSessionPath(undefined)).toBeUndefined();
    expect(agentFromSessionPath('')).toBeUndefined();
  });
});

describe('sessionFromPath', () => {
  it('strips dir and .jsonl extension', () => {
    expect(sessionFromPath('/a/b/4d50429b-64bd.jsonl')).toBe('4d50429b-64bd');
  });
  it('returns undefined for empty', () => {
    expect(sessionFromPath(undefined)).toBeUndefined();
  });
});

describe('isObsEnabled', () => {
  it('defaults to enabled', () => {
    expect(isObsEnabled({})).toBe(true);
  });
  it('respects RELAY_OBS_ENABLED=false', () => {
    expect(isObsEnabled({ RELAY_OBS_ENABLED: 'false' })).toBe(false);
    expect(isObsEnabled({ RELAY_OBS_ENABLED: 'FALSE' })).toBe(false);
    expect(isObsEnabled({ RELAY_OBS_ENABLED: 'true' })).toBe(true);
  });
});

describe('resolveObsDir', () => {
  it('honors RELAY_OBS_DIR override', () => {
    expect(resolveObsDir({ RELAY_OBS_DIR: '/custom/obs' })).toBe('/custom/obs');
  });
  it('defaults under ~/.relay/obs', () => {
    expect(resolveObsDir({})).toMatch(/\.relay\/obs$/);
  });
});

describe('emitObsEvent (落盘)', () => {
  let dir: string;
  let env: NodeJS.ProcessEnv;

  beforeEach(() => {
    dir = mkdtempSync(join(tmpdir(), 'obs-test-'));
    env = { RELAY_OBS_DIR: dir };
  });
  afterEach(() => {
    rmSync(dir, { recursive: true, force: true });
  });

  it('appends a single-line JSON event to degen-{date}.jsonl bucketed by ts', () => {
    emitObsEvent({ ts: '2026-07-07T05:58:54.000Z', evt: 'degen.observed', agent: 'vultr-pulse-space', kind: 'x' }, env);
    const files = readdirSync(dir);
    expect(files).toContain('degen-2026-07-07.jsonl');
    const lines = readFileSync(join(dir, 'degen-2026-07-07.jsonl'), 'utf8').trim().split('\n');
    expect(lines).toHaveLength(1);
    const parsed = JSON.parse(lines[0]!);
    expect(parsed.evt).toBe('degen.observed');
    expect(parsed.agent).toBe('vultr-pulse-space');
  });

  it('appends multiple events to the same daily file', () => {
    emitObsEvent({ ts: '2026-07-07T01:00:00.000Z', evt: 'a' }, env);
    emitObsEvent({ ts: '2026-07-07T02:00:00.000Z', evt: 'b' }, env);
    const lines = readFileSync(join(dir, 'degen-2026-07-07.jsonl'), 'utf8').trim().split('\n');
    expect(lines).toHaveLength(2);
  });

  it('is a no-op when disabled', () => {
    emitObsEvent({ ts: '2026-07-07T05:00:00.000Z', evt: 'x' }, { ...env, RELAY_OBS_ENABLED: 'false' });
    expect(readdirSync(dir)).toHaveLength(0);
  });

  it('never throws even when the dir is unwritable (fail-safe)', () => {
    // 指向一个不可能创建的路径（父是文件）——emit 必须吞掉错误，绝不上抛。
    const badEnv = { RELAY_OBS_DIR: '/dev/null/cannot/mkdir' };
    expect(() => emitObsEvent({ ts: '2026-07-07T05:00:00.000Z', evt: 'x' }, badEnv)).not.toThrow();
  });
});

describe('emitDegenObserved (L1 便捷入口)', () => {
  let dir: string;
  let env: NodeJS.ProcessEnv;
  beforeEach(() => {
    dir = mkdtempSync(join(tmpdir(), 'obs-test-'));
    env = { RELAY_OBS_DIR: dir };
  });
  afterEach(() => rmSync(dir, { recursive: true, force: true }));

  it('infers critical severity for restarted disposition', () => {
    emitDegenObserved({ ts: '2026-07-07T05:00:00.000Z', agent: 'a', disposition: 'restarted', kind: 'k' }, env);
    const parsed = JSON.parse(readFileSync(join(dir, 'degen-2026-07-07.jsonl'), 'utf8').trim());
    expect(parsed.severity).toBe('critical');
    expect(parsed.evt).toBe('degen.observed');
  });

  it('defaults to warn severity for cleansed disposition', () => {
    emitDegenObserved({ ts: '2026-07-07T05:00:00.000Z', agent: 'a', disposition: 'cleansed' }, env);
    const parsed = JSON.parse(readFileSync(join(dir, 'degen-2026-07-07.jsonl'), 'utf8').trim());
    expect(parsed.severity).toBe('warn');
  });

  it('flattens detail into the event', () => {
    emitDegenObserved({ ts: '2026-07-07T05:00:00.000Z', agent: 'a', disposition: 'cleansed', detail: { leakTrimmed: 3 } }, env);
    const parsed = JSON.parse(readFileSync(join(dir, 'degen-2026-07-07.jsonl'), 'utf8').trim());
    expect(parsed.leakTrimmed).toBe(3);
  });
});
