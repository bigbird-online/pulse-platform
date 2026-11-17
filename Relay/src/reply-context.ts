const REPLY_CONTEXT_MAX_CHARS = 3000;

export function truncateReplyContextText(
  text: string | undefined,
  maxChars: number = REPLY_CONTEXT_MAX_CHARS,
): string | undefined {
  const normalized = text?.replace(/\r\n/g, '\n').trim();
  if (!normalized) return undefined;
  if (normalized.length <= maxChars) return normalized;
  return `${normalized.slice(0, maxChars)}\n[reply context truncated]`;
}

/** Line between quoted reply and the user's new utterance (see `buildReplyContextPrompt`). */
export const REPLY_CONTEXT_USER_MARKER_LINE = '--- user message ---';

/**
 * When {@link buildReplyContextPrompt} wraps a reply chain, CC session jsonl often
 * records only the final user utterance (`text`), not the full bridged prompt.
 * This returns the substring after {@link REPLY_CONTEXT_USER_MARKER_LINE} when
 * present; otherwise the whole prompt (trimmed).
 */
export function extractReplyContextUserTail(promptText: string): string {
  const normalized = promptText.replace(/\r\n/g, '\n');
  const needle = `\n${REPLY_CONTEXT_USER_MARKER_LINE}\n`;
  const idx = normalized.lastIndexOf(needle);
  if (idx === -1) return normalized.trim();
  return normalized.slice(idx + needle.length).trim();
}

/**
 * Optional metadata about the quoted message's origin. When the quoted card
 * came from a *different* session than the one now receiving the reply,
 * {@link buildReplyContextPrompt} rewrites the header to make that crossing
 * explicit — otherwise the receiving CC reads the quoted text as its own
 * prior utterance, which is the 2026-06-22 cross-session stranding bug.
 */
export interface ReplyContextOrigin {
  /** True iff the quoted message demonstrably came from the current session. */
  sameSession: boolean;
  /** Roughly when the quoted message was sent (epoch ms), if known. */
  quotedAtMs?: number;
  /** Quoted message's session id, if known (truncated to first 8 chars for display). */
  quotedSessionId?: string;
}

function formatAge(quotedAtMs: number): string {
  const deltaMs = Date.now() - quotedAtMs;
  if (deltaMs < 60_000) return '刚刚';
  if (deltaMs < 3600_000) return `${Math.round(deltaMs / 60_000)} 分钟前`;
  if (deltaMs < 86_400_000) return `${Math.round(deltaMs / 3_600_000)} 小时前`;
  return `${Math.round(deltaMs / 86_400_000)} 天前`;
}

export function buildReplyContextPrompt(
  text: string,
  replyText?: string,
  origin?: ReplyContextOrigin,
): string {
  const quoted = truncateReplyContextText(replyText);
  if (!quoted) return text;

  // Default (no origin info) = legacy header. Pre-2026-06-22 entries lacked
  // sessionId so we can't distinguish — keep old wording to avoid alarming
  // users on every reply during the legacy-cache drain window.
  let header = '用户正在回复上一条消息。被回复消息如下：';
  if (origin && !origin.sameSession) {
    const parts: string[] = ['（注意：以下被回复消息来自其他 session'];
    if (origin.quotedSessionId) parts.push(`，session ${origin.quotedSessionId.slice(0, 8)}`);
    if (origin.quotedAtMs) parts.push(`，发送于${formatAge(origin.quotedAtMs)}`);
    parts.push('，不是你的上一条话）');
    header = `用户引用了一条历史消息：${parts.join('')}`;
  }

  return [
    header,
    '--- replied message ---',
    quoted,
    REPLY_CONTEXT_USER_MARKER_LINE,
    text,
  ].join('\n');
}
