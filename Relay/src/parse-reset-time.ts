/**
 * Parse a 429 rate-limit quota-reset timestamp from provider error text.
 *
 * Extracted from pool.ts for testability — the error text arrives via PTY
 * stdout and may contain residual ANSI escape sequences that the caller's
 * own stripping misses (e.g. ?-prefixed CSI like \x1B[?2026h, or OSC like
 * \x1B]0;title\x07). This function strips them defensively before matching.
 *
 * The date-time pattern is matched directly (not requiring a "reset at"
 * prefix) because PTY rendering can garble words. Digit-only request IDs
 * (20260617095543...) have no dashes and won't false-match.
 */

/** Strip ANSI escape sequences: CSI (incl. ?-prefixed) + OSC. */
export function stripAnsiEscapes(text: string): string {
  return text.replace(/\x1b\[[0-9;?]*[a-zA-Z]|\x1b\][^\x07]*\x07/g, '');
}

/** Parse epoch-ms from a 429 "reset at YYYY-MM-DD HH:MM:SS" text.
 *  PTY rendering may consume the separator between date and time
 *  (e.g. "2026-06-1801:52:49"), so the separator is optional. */
export function parseResetTime(text: string): number | undefined {
  const cleaned = stripAnsiEscapes(text);
  const match = cleaned.match(/(\d{4})-(\d{2})-(\d{2})[T ]?(\d{2}):(\d{2}):(\d{2})/);
  if (!match) return undefined;
  const parsed = Date.parse(`${match[1]}-${match[2]}-${match[3]}T${match[4]}:${match[5]}:${match[6]}`);
  return Number.isNaN(parsed) ? undefined : parsed;
}
