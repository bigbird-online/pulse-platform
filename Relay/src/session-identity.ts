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
