import { describe, it, expect, beforeEach, afterEach } from 'vitest';
import { mkdtempSync, rmSync, existsSync, readFileSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { OutboundTextCache } from '../src/feishu-outbound-cache.js';

describe('OutboundTextCache', () => {
  it('records and looks up by message_id', () => {
    const cache = new OutboundTextCache();
    cache.record('om_1', 'hello world');
    expect(cache.lookup('om_1')?.text).toBe('hello world');
  });

  it('trims keys and values, ignores empty', () => {
    const cache = new OutboundTextCache();
    cache.record('  om_2  ', '  spaced  ');
    expect(cache.lookup('om_2')?.text).toBe('spaced');
    cache.record('om_3', '   ');
    expect(cache.lookup('om_3')).toBeUndefined();
    cache.record(undefined, 'x');
    expect(cache.size).toBe(1);
  });

  it('returns undefined for unknown / empty ids', () => {
    const cache = new OutboundTextCache();
    expect(cache.lookup('nope')).toBeUndefined();
    expect(cache.lookup(undefined)).toBeUndefined();
    expect(cache.lookup('   ')).toBeUndefined();
  });

  it('evicts oldest entries beyond capacity (LRU by insertion)', () => {
    const cache = new OutboundTextCache(2);
    cache.record('a', '1');
    cache.record('b', '2');
    cache.record('c', '3'); // evicts 'a'
    expect(cache.lookup('a')).toBeUndefined();
    expect(cache.lookup('b')?.text).toBe('2');
    expect(cache.lookup('c')?.text).toBe('3');
  });

  it('refreshes recency on re-record so it is not evicted first', () => {
    const cache = new OutboundTextCache(2);
    cache.record('a', '1');
    cache.record('b', '2');
    cache.record('a', '1-updated'); // 'a' moves to tail, value updated
    cache.record('c', '3');         // evicts 'b' (now oldest)
    expect(cache.lookup('b')).toBeUndefined();
    expect(cache.lookup('a')?.text).toBe('1-updated');
    expect(cache.lookup('c')?.text).toBe('3');
  });

  it('records and returns origin (agentId/sessionId/poolKey) when provided', () => {
    const cache = new OutboundTextCache();
    cache.record('om_origin', 'with metadata', {
      agentId: 'pulse',
      sessionId: 'sess-abc',
      poolKey: 'owner:pulse:feishu',
    });
    const got = cache.lookup('om_origin');
    expect(got?.text).toBe('with metadata');
    expect(got?.agentId).toBe('pulse');
    expect(got?.sessionId).toBe('sess-abc');
    expect(got?.poolKey).toBe('owner:pulse:feishu');
  });

  it('legacy entries without origin still load and serve text', () => {
    const cache = new OutboundTextCache();
    cache.record('om_legacy', 'no metadata'); // no origin arg
    const got = cache.lookup('om_legacy');
    expect(got?.text).toBe('no metadata');
    expect(got?.agentId).toBeUndefined();
    expect(got?.sessionId).toBeUndefined();
  });
});

describe('OutboundTextCache persistence', () => {
  let dir: string;
  let file: string;

  beforeEach(() => {
    dir = mkdtempSync(join(tmpdir(), 'fo-cache-'));
    file = join(dir, 'cache.json');
  });
  afterEach(() => {
    rmSync(dir, { recursive: true, force: true });
  });

  it('writes to disk on flush and reloads in a new instance', () => {
    const a = new OutboundTextCache();
    a.enablePersistence({ filePath: file });
    a.record('om_1', '机器人的上一条回复');
    a.flush();
    expect(existsSync(file)).toBe(true);

    const b = new OutboundTextCache();
    b.enablePersistence({ filePath: file });
    expect(b.lookup('om_1')?.text).toBe('机器人的上一条回复');
  });

  it('persists and reloads origin metadata', () => {
    const a = new OutboundTextCache();
    a.enablePersistence({ filePath: file });
    a.record('om_o', '带元数据', {
      agentId: 'pulse',
      sessionId: 'sess-persist',
      poolKey: 'owner:pulse:feishu',
    });
    a.flush();

    const b = new OutboundTextCache();
    b.enablePersistence({ filePath: file });
    const got = b.lookup('om_o');
    expect(got?.text).toBe('带元数据');
    expect(got?.agentId).toBe('pulse');
    expect(got?.sessionId).toBe('sess-persist');
    expect(got?.poolKey).toBe('owner:pulse:feishu');
  });

  it('drops entries older than the TTL on load', () => {
    const stale = {
      version: 1,
      updatedAt: new Date().toISOString(),
      entries: {
        old: { text: 'expired', ts: Date.now() - 10_000 },
        fresh: { text: 'kept', ts: Date.now() },
      },
    };
    writeFileSync(file, JSON.stringify(stale));

    const cache = new OutboundTextCache();
    cache.enablePersistence({ filePath: file, ttlMs: 5_000 });
    expect(cache.lookup('old')).toBeUndefined();
    expect(cache.lookup('fresh')?.text).toBe('kept');
  });

  it('survives a corrupt cache file by starting empty', () => {
    writeFileSync(file, '{ not valid json');
    const cache = new OutboundTextCache();
    cache.enablePersistence({ filePath: file });
    expect(cache.size).toBe(0);
    cache.record('om_2', 'still works');
    expect(cache.lookup('om_2')?.text).toBe('still works');
  });

  it('does not touch disk when persistence is disabled', () => {
    const cache = new OutboundTextCache();
    cache.record('om_3', 'memory only');
    cache.flush();
    expect(existsSync(file)).toBe(false);
    expect(cache.lookup('om_3')?.text).toBe('memory only');
  });
});
