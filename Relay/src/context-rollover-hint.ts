/**
 * Tell an Agent that it should roll its WI over to a fresh session of itself:
 * the context is nearly full or has been compacted repeatedly. Compactions are
 * counted per native CC session in print mode, the mode whose stream carries
 * `compact_boundary`. Two private files hold them: the rollover state, which
 * restores a resumed session's count after a Relay restart or a process kill
 * and only drops a tenant's long-ended sessions, and the Stats record
 * `pulse.stats.context_compactions/v1`, a bounded publication window. Each
 * compaction counts toward the WI the session had then, so a later declaration
 * relabels the session without moving earlier compactions; after a restart the
 * newer of the two copies restores the count. From the
 * configured compaction count on, every ordinary turn of that session carries
 * the hint, human or Host delivery alike, until the session is replaced. The
 * hint opens the message so that trailing provenance references stay last; the
 * Agent decides, and the core still checks the request.
 */
import { lstatSync, readFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import type {StreamJsonMessage} from './types.js';
import {bindHumanMessageProof,collaborationRef,humanMessageProof} from './collaboration-capture.js';
import {writePrivateJson} from './cc-quota-observation.js';

const DEFAULT_RATIO = 0.6;
const DEFAULT_COMPACTIONS = 3;
export const CONTEXT_COMPACTIONS_SCHEMA = 'pulse.stats.context_compactions/v1';
export const CONTEXT_COMPACTIONS_PATH_ENV = 'PULSE_CONTEXT_COMPACTIONS_PATH';
export const CONTEXT_ROLLOVER_STATE_SCHEMA = 'pulse.relay.context_rollover_sessions/v1';
export const CONTEXT_ROLLOVER_STATE_PATH_ENV = 'PULSE_CONTEXT_ROLLOVER_STATE_PATH';
/** Stats window: sessions not compacted for this long, or beyond the newest MAX_SESSIONS, leave the record. */
const KEEP_MS = 30 * 24 * 3600 * 1000;
const MAX_SESSIONS = 2000;
/** Rollover state: a tenant runs one session at a time, so all but its newest are ended; keep this many. */
const SESSIONS_PER_TENANT = 16;
const WI = /^wi_\d{8}_\d{3}$/;
const REF = /^ccf1_[a-f0-9]{64}$/;

/** Compactions of a session while it had this WI; `wi: null` while it had none. */
export type WiCompactions = {wi: string | null; compactions: number};
export type CompactedSession = {
  session_ref: string;
  runtime: 'cc';
  /** The session's WI now: its latest successful declaration, else the Host metering binding. */
  wi: string | null;
  compactions: number;
  /** The compactions split by the WI each happened under, in order of first compaction; they sum to `compactions`. */
  by_wi: WiCompactions[];
  context_ratio: number | null;
  first_compaction_at: string;
  last_compaction_at: string;
};
export type ContextCompactionsDocument = {
  schema: typeof CONTEXT_COMPACTIONS_SCHEMA;
  source: 'relay_cc_compact_boundary';
  updated_at: string;
  /** When this record began; sessions compacted only before it are not in it. */
  recording_started_at: string;
  /** The latest last compaction of a session the window dropped; null while none was. */
  dropped_through: string | null;
  sessions: CompactedSession[];
};
export type RolloverStateSession = Omit<CompactedSession, 'runtime'> & {tenant_ref: string; updated_at: string};
export type ContextRolloverStateDocument = {
  schema: typeof CONTEXT_ROLLOVER_STATE_SCHEMA;
  updated_at: string;
  sessions: RolloverStateSession[];
};

function positive(raw: string | undefined, fallback: number): number {
  const value = Number(raw);
  return raw !== undefined && Number.isFinite(value) && value > 0 ? value : fallback;
}

/** Explicit PULSE_CONTEXT_COMPACTIONS_PATH first; otherwise only under an existing RELAY_DATA_DIR. */
export function resolveContextCompactionsPath(env: NodeJS.ProcessEnv = process.env): string | undefined {
  const explicit = env[CONTEXT_COMPACTIONS_PATH_ENV]?.trim();
  if (explicit) return explicit;
  const dataDir = env['RELAY_DATA_DIR']?.trim();
  return dataDir ? join(dataDir, 'context', 'context-compactions-v1.json') : undefined;
}

/** Explicit PULSE_CONTEXT_ROLLOVER_STATE_PATH first; otherwise beside the Stats record. */
export function resolveContextRolloverStatePath(env: NodeJS.ProcessEnv = process.env): string | undefined {
  const explicit = env[CONTEXT_ROLLOVER_STATE_PATH_ENV]?.trim();
  if (explicit) return explicit;
  const record = resolveContextCompactionsPath(env);
  return record ? join(dirname(record), 'context-rollover-sessions-v1.json') : undefined;
}

/** The hashed native session scope ComputeCost and Stats use; the native ID never leaves Relay. */
export function compactionSessionRef(sessionId: string): string {
  return collaborationRef('native_session', ['cc', sessionId]);
}

function instant(value: unknown): value is string {
  return typeof value === 'string' && Number.isFinite(Date.parse(value));
}

function wiOrNull(value: unknown): value is string | null {
  return value === null || (typeof value === 'string' && WI.test(value));
}

function count(value: unknown): value is number {
  return Number.isSafeInteger(value) && (value as number) >= 1;
}

/** A valid split: distinct WIs, each counted at least once, summing to the session's count. */
function split(value: unknown, total: number): WiCompactions[] | undefined {
  if (!Array.isArray(value)) return undefined;
  const result: WiCompactions[] = [];
  for (const entry of value) {
    const e = (entry ?? {}) as Record<string, unknown>;
    if (!wiOrNull(e.wi) || !count(e.compactions) || result.some(r => r.wi === e.wi)) return undefined;
    result.push({wi: e.wi, compactions: e.compactions});
  }
  return result.reduce((sum, r) => sum + r.compactions, 0) === total ? result : undefined;
}

function stored(row: unknown): CompactedSession | undefined {
  const r = (row ?? {}) as Record<string, unknown>;
  if (typeof r.session_ref !== 'string' || !REF.test(r.session_ref) || !count(r.compactions) || !wiOrNull(r.wi)
    || !instant(r.first_compaction_at) || !instant(r.last_compaction_at)) return undefined;
  const byWi = split(r.by_wi, r.compactions);
  if (!byWi) return undefined;
  const ratio = typeof r.context_ratio === 'number' && Number.isFinite(r.context_ratio) && r.context_ratio >= 0
    ? r.context_ratio : null;
  return {session_ref: r.session_ref, runtime: 'cc', wi: r.wi, compactions: r.compactions, by_wi: byWi,
    context_ratio: ratio, first_compaction_at: r.first_compaction_at, last_compaction_at: r.last_compaction_at};
}

/** Of two copies of one session, the later: more compactions, then the later last compaction; the first on a tie. */
function newer<T extends CompactedSession | RolloverStateSession>(a: T | undefined, b: T | undefined): T | undefined {
  if (!a || !b) return a ?? b;
  if (a.compactions !== b.compactions) return a.compactions > b.compactions ? a : b;
  return Date.parse(b.last_compaction_at) > Date.parse(a.last_compaction_at) ? b : a;
}

function readDocument(path: string): Record<string, unknown> | undefined {
  try {
    if (lstatSync(path).isSymbolicLink()) return undefined;
    const document = JSON.parse(readFileSync(path, 'utf8')) as unknown;
    return document && typeof document === 'object' ? document as Record<string, unknown> : undefined;
  } catch { return undefined; /* absent or damaged: nothing recorded */ }
}

type CompactionsRecord = {sessions: Map<string, CompactedSession>; recordingStartedAt?: string; droppedThrough?: string};

function readRecord(path: string): CompactionsRecord {
  const sessions = new Map<string, CompactedSession>();
  const document = readDocument(path);
  if (document?.schema !== CONTEXT_COMPACTIONS_SCHEMA || !Array.isArray(document.sessions)) return {sessions};
  for (const row of document.sessions) {
    const session = stored(row);
    if (session) sessions.set(session.session_ref, session);
  }
  return {sessions, recordingStartedAt: instant(document.recording_started_at) ? document.recording_started_at : undefined,
    droppedThrough: instant(document.dropped_through) ? document.dropped_through : undefined};
}

/** Every valid session row of the Stats record; an absent or damaged file reads as none. */
export function readContextCompactions(path: string): Map<string, CompactedSession> {
  return readRecord(path).sessions;
}

/** Every valid session of the rollover state, by session_ref; an absent or damaged file reads as none. */
export function readContextRolloverState(path: string): Map<string, RolloverStateSession> {
  const result = new Map<string, RolloverStateSession>();
  const document = readDocument(path);
  if (document?.schema !== CONTEXT_ROLLOVER_STATE_SCHEMA || !Array.isArray(document.sessions)) return result;
  for (const row of document.sessions) {
    const session = stored(row);
    const r = row as Record<string, unknown>;
    if (!session || typeof r.tenant_ref !== 'string' || !REF.test(r.tenant_ref) || !instant(r.updated_at)) continue;
    const {runtime: _runtime, ...kept} = session;
    result.set(session.session_ref, {...kept, tenant_ref: r.tenant_ref, updated_at: r.updated_at});
  }
  return result;
}

type Seen = {tenant: string; ratio?: number; window?: number; tokens?: number; compactions: number; hinted: boolean;
  ref?: string; declared?: string; bound?: string; first?: string; last?: string; byWi: Map<string | null, number>};
export type HintContext = {sessionId?: string; wi?: string};

export function rolloverHintText(ratio: number | undefined, compactions: number): string {
  const used = ratio === undefined ? undefined : `${Math.round(ratio * 100)}%`;
  const state = compactions
    ? `本会话已压缩 ${compactions} 次${used ? `（上下文约 ${used}）` : ''}`
    : `本会话上下文已用约 ${used ?? '未知'}`;
  return `[Host 上下文提醒，不是人类决定：${state}。若你持有仍有工作的 WI，现在先记 checkpoint（已完成、待办、下一步），`
    + '再对该 WI 执行 request_rollover 并结束本轮；Host 会在新会话里接续。]';
}

/** The acknowledgements `declare_session_wi` returns on success (ComputeCost mcp_request_purpose). */
const DECLARATION_ACKS = new Set(['cc_session_wi/v1', 'cc_request_wi/v1']);

function resultText(content: unknown): string | undefined {
  if (typeof content === 'string') return content;
  return Array.isArray(content) && content.length === 1 && content[0]?.type === 'text' && typeof content[0].text === 'string'
    ? content[0].text : undefined;
}

/**
 * The WI a successful declare_session_wi call declared, read from its
 * tool_result: CC print mode forwards tool results, not tool-only calls, and a
 * failed or rejected call returns no acknowledgement. A `none` declaration
 * names no WI and leaves the session's WI as it was.
 */
function declaredWi(message: Record<string, any>): string | undefined {
  const content = message.message?.content;
  if (!Array.isArray(content)) return undefined;
  let found: string | undefined;
  for (const block of content) {
    if (block?.type !== 'tool_result' || block.is_error === true) continue;
    const text = resultText(block.content);
    if (!text) continue;
    try {
      const ack = JSON.parse(text) as Record<string, unknown>;
      if (DECLARATION_ACKS.has(ack.schema as string) && ack.origin === 'agent_declaration'
        && typeof ack.wi === 'string' && WI.test(ack.wi)) found = ack.wi;
    } catch { /* another tool's output */ }
  }
  return found;
}

/** Context facts per (tenant, native session); the hint each turn of that session calls for. */
export class ContextRolloverHints {
  private readonly seen = new Map<string, Seen>();
  private readonly path: string | undefined;
  private readonly statePath: string | undefined;
  constructor(private readonly env: NodeJS.ProcessEnv = process.env, private readonly now: () => Date = () => new Date()) {
    this.path = resolveContextCompactionsPath(env);
    this.statePath = resolveContextRolloverStatePath(env);
  }

  /**
   * The session's row, its compactions restored the first time it is seen from
   * whichever file holds the later copy: either write may have failed alone.
   */
  private row(tenantId: string, sessionId: string | undefined): Seen {
    const key = JSON.stringify([tenantId, sessionId ?? null]);
    let row = this.seen.get(key);
    if (row) return row;
    row = {tenant: tenantId, compactions: 0, hinted: false, byWi: new Map()};
    if (sessionId) {
      row.ref = compactionSessionRef(sessionId);
      const saved = newer<CompactedSession | RolloverStateSession>(
        this.statePath ? readContextRolloverState(this.statePath).get(row.ref) : undefined,
        this.path ? readContextCompactions(this.path).get(row.ref) : undefined);
      if (saved) Object.assign(row, {compactions: saved.compactions, declared: saved.wi ?? undefined,
        first: saved.first_compaction_at, last: saved.last_compaction_at,
        byWi: new Map(saved.by_wi.map(entry => [entry.wi, entry.compactions]))});
    }
    this.seen.set(key, row);
    return row;
  }

  /** Merge this session into both files as it is now; a failed write is logged and costs durability, never the turn. */
  private persist(row: Seen): void {
    if (!row.ref || !row.compactions || !row.first || !row.last) return;
    const now = this.now().toISOString();
    const session: CompactedSession = {session_ref: row.ref, runtime: 'cc', wi: row.declared ?? row.bound ?? null,
      compactions: row.compactions, by_wi: [...row.byWi].map(([wi, compactions]) => ({wi, compactions})),
      context_ratio: row.ratio ?? null,
      first_compaction_at: row.first, last_compaction_at: row.last};
    this.write('rollover state', () => this.statePath && this.writeState(this.statePath, row.tenant, session, now));
    this.write('Stats record', () => this.path && this.writeRecord(this.path, session, now));
  }

  private write(what: string, act: () => unknown): void {
    try { act(); } catch (error) {
      console.warn({ts: new Date().toISOString(), level: 'warn', msg: `Context compaction ${what} write failed`,
        error: error instanceof Error ? error.message : String(error)});
    }
  }

  /** Per tenant, only sessions beyond its newest SESSIONS_PER_TENANT leave: those ended long ago. */
  private writeState(path: string, tenantId: string, session: CompactedSession, now: string): void {
    const sessions = readContextRolloverState(path);
    const {runtime: _runtime, ...kept} = session;
    const tenantRef = collaborationRef('relay_tenant', [tenantId]);
    sessions.set(session.session_ref, {...kept, tenant_ref: tenantRef, updated_at: now});
    const byTenant = new Map<string, RolloverStateSession[]>();
    for (const s of sessions.values()) byTenant.set(s.tenant_ref, [...(byTenant.get(s.tenant_ref) ?? []), s]);
    const rows = [...byTenant.values()].flatMap(list => list
      .sort((a, b) => b.updated_at.localeCompare(a.updated_at) || a.session_ref.localeCompare(b.session_ref))
      .slice(0, SESSIONS_PER_TENANT))
      .sort((a, b) => a.tenant_ref.localeCompare(b.tenant_ref) || b.updated_at.localeCompare(a.updated_at)
        || a.session_ref.localeCompare(b.session_ref));
    const document: ContextRolloverStateDocument = {schema: CONTEXT_ROLLOVER_STATE_SCHEMA, updated_at: now, sessions: rows};
    writePrivateJson(path, document);
  }

  /** The Stats window, which states its own start and the latest compaction it dropped. */
  private writeRecord(path: string, session: CompactedSession, now: string): void {
    const record = readRecord(path);
    record.sessions.set(session.session_ref, session);
    const horizon = Date.parse(now) - KEEP_MS;
    const ordered = [...record.sessions.values()]
      .sort((a, b) => b.last_compaction_at.localeCompare(a.last_compaction_at) || a.session_ref.localeCompare(b.session_ref));
    const kept = ordered.filter(s => Date.parse(s.last_compaction_at) >= horizon).slice(0, MAX_SESSIONS);
    let dropped = record.droppedThrough;
    for (const s of ordered) {
      if (!kept.includes(s) && (!dropped || Date.parse(s.last_compaction_at) > Date.parse(dropped))) dropped = s.last_compaction_at;
    }
    const document: ContextCompactionsDocument = {schema: CONTEXT_COMPACTIONS_SCHEMA,
      source: 'relay_cc_compact_boundary', updated_at: now, recording_started_at: record.recordingStartedAt ?? now,
      dropped_through: dropped ?? null, sessions: kept};
    writePrivateJson(path, document);
  }

  /** Context facts as the runtimes report them: CC result occupancy, assistant usage, compaction boundaries, successful WI declarations. */
  observe(tenantId: string, msg: StreamJsonMessage, context: HintContext = {}): void {
    const row = this.row(tenantId, context.sessionId);
    if (context.wi && WI.test(context.wi)) row.bound = context.wi;
    const m = msg as Record<string, any>;
    if (msg.type === 'system' && m.subtype === 'compact_boundary') {
      row.compactions++;
      const wi = row.declared ?? row.bound ?? null;
      row.byWi.set(wi, (row.byWi.get(wi) ?? 0) + 1);
      row.tokens = undefined;
      row.last = this.now().toISOString();
      row.first ??= row.last;
      this.persist(row);
      return;
    }
    if (msg.type === 'user') {
      const wi = declaredWi(m);
      if (wi && wi !== row.declared) {
        row.declared = wi;
        this.persist(row);
      }
      return;
    }
    if (msg.type === 'assistant') {
      const usage = m.message?.usage;
      const tokens = ['input_tokens', 'cache_creation_input_tokens', 'cache_read_input_tokens']
        .reduce((sum, name) => sum + (Number.isFinite(Number(usage?.[name])) ? Number(usage[name]) : 0), 0);
      if (usage && tokens > 0) row.tokens = tokens;
      if (row.tokens && row.window) row.ratio = row.tokens / row.window;
      return;
    }
    if (msg.type !== 'result') return;
    const pct = Number(m.context_window?.used_percentage);
    if (Number.isFinite(pct) && pct >= 0) { row.ratio = pct / 100; return; }
    const windows = Object.values(m.modelUsage ?? {}).map((u: any) => Number(u?.contextWindow)).filter(n => n > 0);
    if (windows.length) row.window = Math.max(...windows);
    if (row.tokens && row.window) row.ratio = row.tokens / row.window;
  }

  /**
   * The hint for this turn of the session: every turn once it reached the
   * compaction threshold, otherwise once when the context passed the occupancy
   * threshold.
   */
  take(tenantId: string, sessionId?: string): string | undefined {
    const row = this.row(tenantId, sessionId);
    const ratio = positive(this.env.RELAY_ROLLOVER_HINT_RATIO, DEFAULT_RATIO);
    const compactions = positive(this.env.RELAY_ROLLOVER_HINT_COMPACTIONS, DEFAULT_COMPACTIONS);
    if (row.compactions >= compactions) return rolloverHintText(row.ratio, row.compactions);
    if (row.hinted || (row.ratio ?? 0) < ratio) return undefined;
    row.hinted = true;
    return rolloverHintText(row.ratio, row.compactions);
  }

  /** Drop the tenant's in-memory facts; recorded compactions stay and return if the same session resumes. */
  forget(tenantId: string): void {
    for (const [key, row] of this.seen) if (row.tenant === tenantId) this.seen.delete(key);
  }
}

function messageText(message: StreamJsonMessage): string {
  return typeof message.content === 'string' ? message.content
    : Array.isArray(message.content) ? message.content.map((b: any) => b?.type === 'text' ? b.text : '').join('') : '';
}

/**
 * A turn that can carry the hint: a human message or a Host delivery (scheduled
 * wake-up, contract or PR result), never a slash command, Host control, or a
 * machine envelope that readers recognise by its opening text.
 */
export function carriesRolloverHint(message: StreamJsonMessage): boolean {
  if (message.type !== 'human' || message.delivery_visibility === 'control') return false;
  return !/^\s*(?:\/|<|SCL_HOST_)/.test(messageText(message));
}

const HINT_OPENING = /^\[Host 上下文提醒，不是人类决定：[^\]]*\]\n\n/;

function withProof(message: StreamJsonMessage, next: StreamJsonMessage): StreamJsonMessage {
  const proof = humanMessageProof(message);
  if (proof && next !== message) bindHumanMessageProof(next, proof);
  return next;
}

/**
 * The same message, with its provenance, without the hint an earlier dispatch
 * of the same turn opened it with (a replay or a resumed record); unchanged
 * when it opens with none.
 */
export function withoutRolloverHint(message: StreamJsonMessage): StreamJsonMessage {
  const blocks = Array.isArray(message.content) ? message.content : undefined;
  if (blocks) {
    const opening = blocks[0] as {type?: unknown; text?: unknown} | undefined;
    if (opening?.type !== 'text' || typeof opening.text !== 'string' || !HINT_OPENING.test(opening.text)) return message;
    const rest = opening.text.replace(HINT_OPENING, '');
    return withProof(message, {...message, content: rest ? [{...opening, text: rest}, ...blocks.slice(1)] : blocks.slice(1)});
  }
  const text = String(message.content ?? '');
  return HINT_OPENING.test(text) ? withProof(message, {...message, content: text.replace(HINT_OPENING, '')}) : message;
}

/** The same message, with its provenance, and the one current hint opening its text. */
export function withRolloverHint(message: StreamJsonMessage, hint: string): StreamJsonMessage {
  const base = withoutRolloverHint(message);
  return withProof(message, Array.isArray(base.content)
    ? {...base, content: [{type: 'text', text: hint + '\n\n'}, ...base.content]}
    : {...base, content: hint + '\n\n' + String(base.content ?? '')});
}
