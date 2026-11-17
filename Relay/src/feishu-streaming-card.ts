import { TURN_ACCEPTED_STATUS_TEXT } from './channel-rendering.js';

/** Feishu Card Kit streaming, with same-card recovery after streaming expiry. */
const STREAMING_UPDATE_THROTTLE_MS = 1500;
const STREAMING_SIGNIFICANT_DELTA_CHARS = 18;
// Keepalives reduce idle expiry; they cannot extend the server streaming lifetime.
const STREAMING_HEARTBEAT_TICK_MS = 30_000;
const STREAMING_HEARTBEAT_IDLE_MS = 60_000;
const UPDATE_RETRY_DELAYS_MS = [1500, 3000, 6000] as const;
const tokenCache = new Map<string, { token: string; expiresAt: number }>();

class FeishuCardUpdateError extends Error {
  constructor(message: string, readonly retryable: boolean) { super(message); }
}

/** Only idempotent card updates, not initial message creation, use this recovery. */
export function isRetryableFeishuCardUpdateError(error: unknown): boolean {
  return error instanceof FeishuCardUpdateError && error.retryable;
}

export function mergeStreamingText(previousText: string | undefined, nextText: string | undefined): string {
  const previous = typeof previousText === 'string' ? previousText : '';
  const next = typeof nextText === 'string' ? nextText : '';
  if (!next) return previous;
  if (!previous || next === previous) return next;
  if (next.startsWith(previous)) return next;
  if (previous.startsWith(next)) return previous;
  if (next.includes(previous)) return next;
  if (previous.includes(next)) return previous;
  const maxOverlap = Math.min(previous.length, next.length);
  for (let overlap = maxOverlap; overlap > 0; overlap -= 1) {
    if (previous.slice(-overlap) === next.slice(0, overlap)) return `${previous}${next.slice(overlap)}`;
  }
  return `${previous}${next}`;
}

function hasNaturalStreamingBoundary(text: string): boolean {
  return /[\n。！？!?；;：:]$/.test(text);
}

function shouldPushStreamingUpdate(previousText: string, nextText: string): boolean {
  if (!previousText) return true;
  if (hasNaturalStreamingBoundary(nextText)) return true;
  return nextText.length - previousText.length >= STREAMING_SIGNIFICANT_DELTA_CHARS;
}

function truncateSummary(text: string, max = 50): string {
  if (!text) return '';
  const clean = text.replace(/\n/g, ' ').trim();
  return clean.length <= max ? clean : `${clean.slice(0, max - 3)}...`;
}

export interface FeishuStreamingCredentials {
  appId: string;
  appSecret: string;
  /** Default https://open.feishu.cn/open-apis */
  apiBase?: string;
}

export interface FeishuMessageCreateClient {
  im: {
    message: {
      create(input: {
        params: { receive_id_type: 'open_id' };
        data: { receive_id: string; msg_type: string; content: string };
      }): Promise<{ code?: number; msg?: string; data?: { message_id?: string } }>;
    };
  };
}

function defaultApiBase(): string {
  const raw = process.env['FEISHU_OPEN_API_BASE']?.trim();
  if (raw) return raw.replace(/\/+$/, '');
  return 'https://open.feishu.cn/open-apis';
}

async function getTenantAccessToken(creds: FeishuStreamingCredentials): Promise<string> {
  const apiBase = creds.apiBase ?? defaultApiBase();
  const key = `${apiBase}|${creds.appId}`;
  const cached = tokenCache.get(key);
  if (cached && cached.expiresAt > Date.now() + 60_000) return cached.token;
  const res = await fetch(`${apiBase}/auth/v3/tenant_access_token/internal`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', 'User-Agent': 'Pulse-Relay/feishu-streaming' },
    body: JSON.stringify({ app_id: creds.appId, app_secret: creds.appSecret }),
  });
  if (!res.ok) throw new Error(`Feishu token HTTP ${res.status}`);
  const data = (await res.json()) as { code?: number; msg?: string; tenant_access_token?: string; expire?: number };
  if (data.code !== 0 || !data.tenant_access_token) throw new Error(`Feishu token error: ${data.msg ?? 'unknown'}`);
  tokenCache.set(key, { token: data.tenant_access_token, expiresAt: Date.now() + (data.expire ?? 7200) * 1000 });
  return data.tenant_access_token;
}

type CardState = { cardId: string; messageId: string; sequence: number; currentText: string };

/** One evolving Card Kit entity per turn/chunk, even after streaming expires. */
export class FeishuStreamingCardSession {
  private readonly creds: FeishuStreamingCredentials;
  private readonly client: FeishuMessageCreateClient;
  private state: CardState | null = null;
  private closed = false;
  private streaming = true;
  private queue: Promise<void> = Promise.resolve();
  private lastUpdateTime = 0;
  private pendingText: string | null = null;
  private flushTimer: ReturnType<typeof setTimeout> | null = null;
  private readonly updateThrottleMs: number;
  private heartbeatTimer: ReturnType<typeof setInterval> | null = null;
  private pulseSignal: string | null = null;
  private hasContentUpdate = false;
  private heartbeatQueued = false;
  private retryAttempt = 0;
  private retryNotBefore = 0;

  constructor(creds: FeishuStreamingCredentials, client: FeishuMessageCreateClient,
    options: { updateThrottleMs?: number } = {}) {
    this.creds = { ...creds, apiBase: creds.apiBase ?? defaultApiBase() };
    this.client = client;
    this.updateThrottleMs = options.updateThrottleMs ?? STREAMING_UPDATE_THROTTLE_MS;
  }

  private apiBase(): string { return this.creds.apiBase ?? defaultApiBase(); }
  getCurrentText(): string { return this.pendingText ?? this.state?.currentText ?? ''; }
  getMessageId(): string | null { return this.state?.messageId ?? null; }
  /** Streaming expiry does not terminalize the conversation turn. */
  isClosed(): boolean { return this.closed; }

  /** Cancel retries/heartbeats and prevent late HTTP completions reviving a turn. */
  dispose(): void {
    this.closed = true;
    this.clearFlushTimer();
    this.clearHeartbeatTimer();
    this.state = null;
    this.pendingText = null;
    this.pulseSignal = null;
    this.hasContentUpdate = false;
    this.retryAttempt = 0;
    this.retryNotBefore = 0;
  }

  private clearFlushTimer(): void {
    if (this.flushTimer) { clearTimeout(this.flushTimer); this.flushTimer = null; }
  }

  private schedulePendingFlush(): void {
    if (this.flushTimer || !this.pendingText || this.closed) return;
    const delayMs = Math.max(0, this.retryNotBefore - Date.now(),
      this.updateThrottleMs - (Date.now() - this.lastUpdateTime));
    this.flushTimer = setTimeout(() => {
      this.flushTimer = null;
      void this.flushPending().catch((error) => {
        console.warn({ ts: new Date().toISOString(), level: 'warn',
          msg: 'Feishu streaming card pending flush failed', error: String(error) });
      });
    }, delayMs);
  }

  private scheduleRetry(error: unknown): void {
    if (this.closed || !this.state || !isRetryableFeishuCardUpdateError(error)) return;
    const delay = UPDATE_RETRY_DELAYS_MS[this.retryAttempt];
    if (delay === undefined) return; // Bounded burst, no zero-delay retry loop.
    this.retryAttempt += 1;
    this.retryNotBefore = Date.now() + delay;
    this.clearFlushTimer();
    this.schedulePendingFlush();
  }

  private commitText(state: CardState, text: string): void {
    if (this.state !== state) return;
    state.currentText = text;
    if (this.pendingText === text) this.pendingText = null;
    this.lastUpdateTime = Date.now();
    this.retryAttempt = 0;
    this.retryNotBefore = 0;
    if (this.pendingText) this.schedulePendingFlush();
  }

  private flushPending(): Promise<void> {
    if (!this.state || this.closed || !this.pendingText) return Promise.resolve();
    const operation = this.queue.catch(() => undefined).then(async () => {
      const state = this.state;
      if (!state || this.closed) return;
      if (Date.now() < this.retryNotBefore) { this.schedulePendingFlush(); return; }
      const text = this.pendingText;
      if (!text || (text === state.currentText && this.retryAttempt === 0)) { this.pendingText = null; return; }
      try {
        await this.updateCardContent(text);
        this.commitText(state, text);
      } catch (error) {
        if (this.state === state) this.scheduleRetry(error);
        throw error;
      }
    });
    this.queue = operation;
    return operation;
  }

  async start(receiveId: string, receiveIdType: 'open_id' = 'open_id',
    initialText = TURN_ACCEPTED_STATUS_TEXT): Promise<void> {
    if (this.state || this.closed) return;
    const visibleInitialText = initialText.trim() || TURN_ACCEPTED_STATUS_TEXT;
    const token = await getTenantAccessToken(this.creds);
    if (this.closed) return;
    const cardJson = {
      schema: '2.0',
      config: {
        streaming_mode: true, summary: { content: truncateSummary(visibleInitialText) },
        streaming_config: { print_frequency_ms: { default: 50 }, print_step: { default: 1 } },
      },
      body: { elements: [{ tag: 'markdown', content: visibleInitialText, element_id: 'content' }] },
    };
    const createRes = await fetch(`${this.apiBase()}/cardkit/v1/cards`, {
      method: 'POST',
      headers: { Authorization: `Bearer ${token}`, 'Content-Type': 'application/json', 'User-Agent': 'Pulse-Relay/feishu-streaming' },
      body: JSON.stringify({ type: 'card_json', data: JSON.stringify(cardJson) }),
    });
    if (!createRes.ok) throw new Error(`Create card HTTP ${createRes.status}`);
    const createData = (await createRes.json()) as { code?: number; msg?: string; data?: { card_id: string } };
    if (createData.code !== 0 || !createData.data?.card_id) throw new Error(`Create card failed: ${createData.msg ?? 'unknown'}`);
    if (this.closed) return;
    const cardId = createData.data.card_id;
    const sendRes = await this.client.im.message.create({
      params: { receive_id_type: receiveIdType },
      data: { receive_id: receiveId, msg_type: 'interactive', content: JSON.stringify({ type: 'card', data: { card_id: cardId } }) },
    });
    if (sendRes.code !== 0 || !sendRes.data?.message_id) throw new Error(`Send streaming card failed: ${sendRes.msg ?? 'unknown'}`);
    if (this.closed) return;
    this.state = { cardId, messageId: sendRes.data.message_id, sequence: 1, currentText: visibleInitialText };
    this.lastUpdateTime = Date.now();
    this.startHeartbeat();
  }

  /** Tool details remain private; only the shared visible frame is replayed. */
  setPulseLine(line: string | null): void {
    if (this.closed) return;
    const signal = line && line.trim() ? line.trim().slice(0, 200) : null;
    if (signal === this.pulseSignal) return;
    this.pulseSignal = signal;
    if (!this.state || this.closed || !this.streaming) return;
    void this.pushHeartbeat().catch(() => {});
  }

  private startHeartbeat(): void {
    if (this.heartbeatTimer) return;
    this.heartbeatTimer = setInterval(() => {
      if (!this.state || this.closed || !this.streaming) return;
      if (Date.now() - this.lastUpdateTime < STREAMING_HEARTBEAT_IDLE_MS) return;
      void this.pushHeartbeat().catch((error) => {
        console.warn({ ts: new Date().toISOString(), level: 'warn',
          msg: 'Feishu streaming card heartbeat failed', error: String(error) });
      });
    }, STREAMING_HEARTBEAT_TICK_MS);
  }

  private clearHeartbeatTimer(): void {
    if (this.heartbeatTimer) { clearInterval(this.heartbeatTimer); this.heartbeatTimer = null; }
  }

  /** Heartbeat, progress and final writes share one sequence and queue. */
  private pushHeartbeat(): Promise<void> {
    if (!this.state || this.closed || !this.streaming || this.heartbeatQueued) return Promise.resolve();
    this.heartbeatQueued = true;
    const operation = this.queue.catch(() => undefined).then(async () => {
      const state = this.state;
      if (!state || this.closed || !this.streaming || this.retryAttempt > 0) return;
      // Read AFTER preceding writes settle, not before awaiting the queue/token.
      const text = this.pendingText ?? state.currentText;
      if (!text) return;
      try {
        await this.updateCardContent(text, 'h');
        this.commitText(state, text);
      } catch (error) {
        if (this.state === state && !this.closed) {
          this.lastUpdateTime = Date.now();
          this.pendingText ??= text;
          this.scheduleRetry(error);
        }
        throw error;
      }
    }).finally(() => { this.heartbeatQueued = false; });
    this.queue = operation;
    return operation;
  }

  private async updateCardContent(text: string, prefix = 's'): Promise<void> {
    const state = this.state;
    if (!state) return;
    let token: string;
    try { token = await getTenantAccessToken(this.creds); }
    catch (error) {
      throw new FeishuCardUpdateError('Card update token unavailable',
        error instanceof TypeError || /HTTP (408|429|5\d\d)/.test(String(error)));
    }
    if (this.state !== state) return;
    const streaming = this.streaming;
    const sequence = ++state.sequence;
    this.lastUpdateTime = Date.now(); // Failed attempts are throttled too.
    const card = {
      schema: '2.0',
      config: { streaming_mode: false, summary: { content: truncateSummary(text) } },
      body: { elements: [{ tag: 'markdown', element_id: 'content', content: text }] },
    };
    let res: Response;
    try {
      res = await fetch(`${this.apiBase()}/cardkit/v1/cards/${state.cardId}${streaming ? '/elements/content/content' : ''}`, {
        method: 'PUT',
        headers: { Authorization: `Bearer ${token}`, 'Content-Type': 'application/json', 'User-Agent': 'Pulse-Relay/feishu-streaming' },
        body: JSON.stringify(streaming
          ? { content: text, sequence, uuid: `${prefix}_${state.cardId}_${sequence}` }
          : { card: { type: 'card_json', data: JSON.stringify(card) }, sequence, uuid: `r_${state.cardId}_${sequence}` }),
      });
    } catch { throw new FeishuCardUpdateError('Card update network failure', true); }
    if (this.state !== state) return;
    if (!res.ok) throw new FeishuCardUpdateError(`Card update HTTP ${res.status}`,
      res.status === 408 || res.status === 429 || res.status >= 500);
    let data: { code?: number; msg?: string };
    try { data = await res.json(); }
    catch { throw new FeishuCardUpdateError('Card update invalid response', true); }
    if (this.state !== state || data.code === 0) return;
    if (streaming && /streaming mode is closed|card streaming timeout/i.test(data.msg ?? '')) {
      // The Card Kit entity is still editable: retain card_id and message_id.
      this.streaming = false;
      this.clearHeartbeatTimer();
      await this.updateCardContent(text);
      return;
    }
    throw new FeishuCardUpdateError(`Card update failed: ${data.msg ?? 'unknown'}`, data.code === 99991400);
  }

  /** Replace the pending snapshot; transport coalescing never loses the latest count. */
  async update(text: string): Promise<void> {
    if (!this.state || this.closed || !text) return;
    if (text === (this.pendingText ?? this.state.currentText)) return;
    this.pendingText = text;
    this.clearFlushTimer();
    const shouldForce = !this.hasContentUpdate || shouldPushStreamingUpdate(this.state.currentText, text);
    this.hasContentUpdate = true;
    const now = Date.now();
    if (now < this.retryNotBefore || (!shouldForce && now - this.lastUpdateTime < this.updateThrottleMs)) {
      this.schedulePendingFlush();
      return;
    }
    await this.flushPending();
  }

  async close(finalText?: string, options: {
    retainOnRetryableFailure?: boolean;
    ownsTurn?: () => boolean;
  } = {}): Promise<void> {
    const state = this.state;
    const retainOnRetryableFailure = options.retainOnRetryableFailure ?? false;
    const ownsTurn = (): boolean => !options.ownsTurn || options.ownsTurn();
    if (!state || this.closed) return;
    if (!ownsTurn()) { this.dispose(); return; }
    this.closed = true;
    this.clearFlushTimer();
    this.clearHeartbeatTimer();
    let retain = false;
    try {
      await this.queue.catch(() => undefined);
      if (this.state !== state || !ownsTurn()) return;
      const text = finalText || this.pendingText || state.currentText;
      if (text && text !== state.currentText) {
        await this.updateCardContent(text);
        if (this.state !== state || !ownsTurn()) return;
        state.currentText = text;
      }
      if (!this.streaming) return;
      const token = await getTenantAccessToken(this.creds);
      if (this.state !== state || !ownsTurn()) return;
      const sequence = ++state.sequence;
      const res = await fetch(`${this.apiBase()}/cardkit/v1/cards/${state.cardId}/settings`, {
        method: 'PATCH',
        headers: { Authorization: `Bearer ${token}`, 'Content-Type': 'application/json; charset=utf-8', 'User-Agent': 'Pulse-Relay/feishu-streaming' },
        body: JSON.stringify({
          settings: JSON.stringify({ config: { streaming_mode: false, summary: { content: truncateSummary(text) } } }),
          sequence, uuid: `c_${state.cardId}_${sequence}`,
        }),
      });
      if (this.state !== state || !ownsTurn()) return;
      const data = res.ok ? await res.json().catch(() => ({})) as { code?: number } : undefined;
      if (this.state !== state || !ownsTurn()) return;
      if (!res.ok || data?.code !== 0) console.warn({ ts: new Date().toISOString(), level: 'warn',
        msg: 'Feishu streaming card close settings failed', status: res.status });
    } catch (error) {
      retain = retainOnRetryableFailure
        && isRetryableFeishuCardUpdateError(error)
        && this.state === state
        && ownsTurn();
      if (retain) this.closed = false;
      throw error;
    } finally {
      if (!retain && this.state === state) this.dispose();
    }
  }
}

export function isFeishuStreamingCardEnvEnabled(): boolean {
  const v = process.env['RELAY_FEISHU_STREAMING_CARD']?.trim().toLowerCase();
  if (!v) return true;
  return !['0', 'false', 'off', 'no'].includes(v);
}
