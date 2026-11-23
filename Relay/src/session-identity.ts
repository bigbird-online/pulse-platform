import { createHash } from 'node:crypto';

/**
 * One opaque Host identity shared by Relay lifecycle control and every tool
 * process inside the resident runtime. Raw provider/Pi identities never cross
 * this boundary.
 */
export function relayHostSessionRef(rawSessionId: string): string {
  const hash = createHash('sha256');
  hash.update(['pulse-session-v1', rawSessionId].join('\0'), 'utf8');
  return `session_v1_${hash.digest('hex')}`;
}

/** What a live conversation exposes for naming its native source to the core. */
export interface NativeSourceCandidate {
  sessionId: string;
  runtime?: string;
  process?: { pid?: number };
  codexThreadId?: string;
  agyConversationId?: string;
}

/**
 * The native source a core command names: a live process by PID; else the conversation the
 * runtime itself named (Codex thread, agy conversation — neither runs between turns), which
 * the core finds by Relay's retained record. An agy session before its first turn has none.
 */
export function nativeCommandSource(live: NativeSourceCandidate, tenant: string):
  { pid: number } | { native_id: string; tenant: string } | undefined {
  if (live.runtime === 'agy') return live.agyConversationId ? { native_id: live.agyConversationId, tenant } : undefined;
  if (live.process?.pid) return { pid: live.process.pid };
  return { native_id: live.codexThreadId ?? live.sessionId, tenant };
}

/** A conversation as Relay holds it, persisted or live, for naming the IDs the core knows it by. */
export interface NativeIdSource {
  sessionId?: string;
  codexThreadId?: string;
  /** Live agy process: the conversation agy named on its latest turn. */
  agyConversationId?: string;
  /** Persisted agy conversation: it names the session only while saved for that same session. */
  agyConversation?: { sessionId: string; conversationId: string };
}

function agyConversationOf(s: NativeIdSource): string | undefined {
  if (s.agyConversationId) return s.agyConversationId;
  const saved = s.agyConversation;
  return saved && saved.sessionId === s.sessionId && saved.conversationId ? saved.conversationId : undefined;
}

/** Every ID the core may name these conversations by: Relay's session ID (CC), the Codex thread,
 *  and the agy conversation. */
export function conversationNativeIds(...sources: Array<NativeIdSource | undefined>): string[] {
  const ids = new Set<string>();
  for (const s of sources) {
    if (!s) continue;
    for (const id of [s.sessionId, s.codexThreadId, agyConversationOf(s)]) if (id) ids.add(id);
  }
  return [...ids];
}

/** Whether a live conversation is still one the delivery was routed to: alive, and known by one of `expect`. */
export function namesConversation(live: (NativeIdSource & { state?: string }) | undefined, expect: string[]): boolean {
  return !!live && live.state !== 'dead' && conversationNativeIds(live).some(id => expect.includes(id));
}

/** The one ID the core names a conversation by: its Codex thread or agy conversation, else Relay's session ID. */
export function coreNativeId(s: NativeIdSource): string | undefined {
  return s.codexThreadId || agyConversationOf(s) || s.sessionId || undefined;
}
