import { readFileSync, writeFileSync, renameSync, existsSync } from 'node:fs';

/**
 * Outbound message text cache for Feishu reply-context resolution.
 *
 * Why this exists: when a user taps "reply" on a bot message in Feishu, the
 * inbound event only carries `parent_id` (a message_id), not the quoted text.
 * Fetching that text via `im.message.get` works for plain text/post messages,
 * but for the bot's own interactive (Card Kit) messages Feishu returns a
 * placeholder card ("请升级至最新版本客户端，以查看内容") instead of the real
 * content — so the API path can never recover what the bot actually said.
 *
 * Since bot replies are almost always cards, we cache the text we sent keyed by
 * the returned message_id. {@link resolveFeishuReplyText} checks this cache
 * before falling back to the API.
 *
 * Persistence: the cache survives Relay restarts by writing to disk (atomic
 * tmp+rename, debounced). Without this, a user replying to a card sent before
 * the last restart would silently fall back to the API placeholder — a
 * confusing "the bot forgot what it just said" experience.
 */

const DEFAULT_MAX_ENTRIES = 2000;
const DEFAULT_TTL_MS = 7 * 24 * 60 * 60 * 1000; // 7 days
const SAVE_DEBOUNCE_MS = 2000;
const PERSIST_VERSION = 1;

/**
 * Origin metadata for an outbound message. All fields optional so legacy
 * entries (pre-2026-06-22, no origin) still load and serve {@link lookup}.
 * When present, {@link lookup} returns these alongside the text so reply
 * handlers can decide whether the quoted card came from the *current* session
 * or a stale cross-session card (root cause of the 2026-06-22 quoted-text
 * stranding: a 12h-old card got replied to and Relay silently fed its text
 * into a fresh CC as if it were the bot's own prior utterance).
 */
export interface OutboundOrigin {
  agentId?: string;
  sessionId?: string;
  poolKey?: string;
}

export interface OutboundLookupResult extends OutboundOrigin {
  text: string;
  ts: number;
}

type Entry = OutboundLookupResult;

interface PersistOptions {
  filePath: string;
  ttlMs?: number;
}

interface PersistedCache {
  version: number;
  updatedAt: string;
  entries: Record<string, Entry>;
}

/** Insertion-ordered LRU with optional disk persistence and TTL. */
class OutboundTextCache {
  private readonly map = new Map<string, Entry>();
  private persist: PersistOptions | null = null;
  private ttlMs = DEFAULT_TTL_MS;
  private saveTimer: ReturnType<typeof setTimeout> | null = null;

  constructor(private readonly maxEntries = DEFAULT_MAX_ENTRIES) {}

  /**
   * Enable disk persistence. Loads any existing file (dropping expired entries)
   * and arranges for subsequent writes to be flushed to disk. Idempotent-ish:
   * calling again re-points the file and reloads.
   */
  enablePersistence(options: PersistOptions): void {
    this.persist = options;
    if (typeof options.ttlMs === 'number') this.ttlMs = options.ttlMs;
    this.loadFromDisk();
  }

  private loadFromDisk(): void {
    const fp = this.persist?.filePath;
    if (!fp || !existsSync(fp)) return;
    try {
      const parsed = JSON.parse(readFileSync(fp, 'utf-8')) as PersistedCache;
      const now = Date.now();
      const entries = Object.entries(parsed.entries ?? {})
        // Keep only unexpired, then sort oldest→newest so insertion order
        // (and thus LRU eviction order) matches recency.
        .filter(([, e]) => e && typeof e.text === 'string' && now - e.ts < this.ttlMs)
        .sort((a, b) => a[1].ts - b[1].ts);
      for (const [id, e] of entries) {
        const restored: Entry = { text: e.text, ts: e.ts };
        // Origin fields are optional and were added 2026-06-22; legacy entries lack them.
        if (e.agentId) restored.agentId = e.agentId;
        if (e.sessionId) restored.sessionId = e.sessionId;
        if (e.poolKey) restored.poolKey = e.poolKey;
        this.map.set(id, restored);
      }
      this.evict();
    } catch {
      // Corrupt file — start empty, will be overwritten on next save.
      console.warn({
        ts: new Date().toISOString(),
        level: 'warn',
        msg: 'Failed to parse feishu-outbound-cache, starting empty',
        path: fp,
      });
    }
  }

  private writeToDisk(): void {
    const fp = this.persist?.filePath;
    if (!fp) return;
    const payload: PersistedCache = {
      version: PERSIST_VERSION,
      updatedAt: new Date().toISOString(),
      entries: Object.fromEntries(this.map),
    };
    const tmp = fp + '.tmp';
    try {
      writeFileSync(tmp, JSON.stringify(payload));
      renameSync(tmp, fp);
    } catch {
      try { writeFileSync(fp, JSON.stringify(payload)); } catch { /* ignore */ }
    }
  }

  private scheduleSave(): void {
    if (!this.persist) return;
    if (this.saveTimer) clearTimeout(this.saveTimer);
    this.saveTimer = setTimeout(() => {
      this.saveTimer = null;
      this.writeToDisk();
    }, SAVE_DEBOUNCE_MS);
  }

  private evict(): void {
    while (this.map.size > this.maxEntries) {
      const oldest = this.map.keys().next().value;
      if (oldest === undefined) break;
      this.map.delete(oldest);
    }
  }

  record(messageId: string | undefined, text: string | undefined, origin?: OutboundOrigin): void {
    const id = messageId?.trim();
    const value = text?.trim();
    if (!id || !value) return;
    // Refresh recency: delete then re-insert so it moves to the tail.
    if (this.map.has(id)) this.map.delete(id);
    const entry: Entry = { text: value, ts: Date.now() };
    if (origin?.agentId) entry.agentId = origin.agentId;
    if (origin?.sessionId) entry.sessionId = origin.sessionId;
    if (origin?.poolKey) entry.poolKey = origin.poolKey;
    this.map.set(id, entry);
    this.evict();
    this.scheduleSave();
  }

  lookup(messageId: string | undefined): OutboundLookupResult | undefined {
    const id = messageId?.trim();
    if (!id) return undefined;
    const entry = this.map.get(id);
    if (!entry) return undefined;
    if (Date.now() - entry.ts >= this.ttlMs) {
      this.map.delete(id);
      this.scheduleSave();
      return undefined;
    }
    // Return a copy so callers can't mutate the cache.
    const result: OutboundLookupResult = { text: entry.text, ts: entry.ts };
    if (entry.agentId) result.agentId = entry.agentId;
    if (entry.sessionId) result.sessionId = entry.sessionId;
    if (entry.poolKey) result.poolKey = entry.poolKey;
    return result;
  }

  /** Flush pending writes synchronously (call on graceful shutdown). */
  flush(): void {
    if (this.saveTimer) {
      clearTimeout(this.saveTimer);
      this.saveTimer = null;
    }
    this.writeToDisk();
  }

  clear(): void {
    this.map.clear();
    if (this.saveTimer) {
      clearTimeout(this.saveTimer);
      this.saveTimer = null;
    }
    this.persist = null;
    this.ttlMs = DEFAULT_TTL_MS;
  }

  get size(): number {
    return this.map.size;
  }
}

const sharedCache = new OutboundTextCache();

/**
 * Enable disk persistence for the shared cache. Call once at startup. Loads any
 * existing on-disk entries so replies to cards sent before a restart still
 * resolve to real text.
 */
export function enableFeishuOutboundPersistence(filePath: string, ttlMs?: number): void {
  sharedCache.enablePersistence({ filePath, ttlMs });
}

export function recordFeishuOutboundText(
  messageId: string | undefined,
  text: string | undefined,
  origin?: OutboundOrigin,
): void {
  sharedCache.record(messageId, text, origin);
}

export function lookupFeishuOutboundText(messageId: string | undefined): OutboundLookupResult | undefined {
  return sharedCache.lookup(messageId);
}

/** Flush the shared cache to disk synchronously (graceful shutdown). */
export function flushFeishuOutboundCache(): void {
  sharedCache.flush();
}

/** Test hook — reset shared state between tests. */
export function __resetFeishuOutboundCacheForTests(): void {
  sharedCache.clear();
}

export { OutboundTextCache };
