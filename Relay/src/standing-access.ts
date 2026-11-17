/**
 * Standing read grants of each Agent, applied whenever Relay starts one of its
 * sessions — conversation or scheduled run — as the shared SSOT root is.
 *
 * The kernel is the authority; one read returns every Agent's grants but
 * replays every WI journal, so Relay keeps the last read and refreshes it in
 * the background once it is older than STANDING_ACCESS_TTL_MS. Only the first
 * session start after Relay starts waits for a read. An unavailable read
 * applies no standing grant (read access fails closed) and never blocks a run.
 * An expired read stops applying as soon as a refresh after its expiry fails,
 * and in any case once it is older than STANDING_ACCESS_MAX_AGE_MS, so a
 * revocation is never held off by an unreadable kernel.
 */
import { accessRows, nodeWorkItemCore, readStandingAccess, type SessionAccess } from './native-work-item-command.js';

export const STANDING_ACCESS_TTL_MS = 10 * 60 * 1000;
export const STANDING_ACCESS_MAX_AGE_MS = 2 * STANDING_ACCESS_TTL_MS;

type Reader = () => Promise<Map<string, SessionAccess[]>>;
interface Read { at: number; rows: SessionAccess[] }

/** The last full read, every Agent at once. */
let full: { at: number; byActor: Map<string, SessionAccess[]> } | undefined;
/** Fresher standing readbacks of single Agents (relay-access). */
const noted = new Map<string, Read>();
/** When the last refresh failed. */
let failedAt: number | undefined;
let inflight: Promise<void> | undefined;

function refresh(read: Reader, now: () => number): Promise<void> {
  inflight ??= read()
    .then((byActor) => { full = { at: now(), byActor }; failedAt = undefined; })
    .catch((error) => {
      failedAt = now();
      console.warn({ ts: new Date().toISOString(), level: 'warn', module: 'standing-access',
        msg: 'Standing access read unavailable; expired standing grants stop applying', error: String(error) });
    })
    .finally(() => { inflight = undefined; });
  return inflight;
}

function latest(actor: string): Read | undefined {
  const fromFull = full ? { at: full.at, rows: full.byActor.get(actor) ?? [] } : undefined;
  const fromNote = noted.get(actor);
  if (!fromFull) return fromNote;
  return fromNote && fromNote.at > fromFull.at ? fromNote : fromFull;
}

/** The Agent's standing read paths now; [] without an installed native core. */
export async function standingAccessFor(
  actor: string | undefined,
  options: { read?: Reader; now?: () => number; core?: () => unknown } = {},
): Promise<SessionAccess[]> {
  const { read = readStandingAccess, now = Date.now, core = nodeWorkItemCore } = options;
  if (!actor || !core()) return [];
  if (!full) await refresh(read, now);
  const current = latest(actor);
  if (!current) return [];
  const age = now() - current.at;
  if (age <= STANDING_ACCESS_TTL_MS) return current.rows;
  void refresh(read, now);
  // An expired read applies only until a refresh after its expiry fails.
  const failedSinceExpiry = failedAt !== undefined && failedAt > current.at + STANDING_ACCESS_TTL_MS;
  return age <= STANDING_ACCESS_MAX_AGE_MS && !failedSinceExpiry ? current.rows : [];
}

/** Keep one Agent's standing grants exactly as an independent kernel readback returned them. */
export function noteStandingAccess(actor: string, standing: SessionAccess[], now: () => number = Date.now): void {
  noted.set(actor, { at: now(), rows: standing });
}

/** This session's own grants with the Agent's current standing ones. */
export function withStandingAccess(session: SessionAccess[] | undefined, standing: SessionAccess[]): SessionAccess[] {
  return accessRows([...(session ?? []).filter((row) => row.standing !== true), ...standing]
    .map((row) => ({ paths: [row.path], mode: row.mode, standing: row.standing === true })));
}

/** Start the first read at Relay start, without waiting for it. */
export function warmStandingAccess(read: Reader = readStandingAccess, core: () => unknown = nodeWorkItemCore): void {
  if (core() && !full) void refresh(read, Date.now);
}

export function resetStandingAccessCache(): void {
  full = undefined;
  failedAt = undefined;
  inflight = undefined;
  noted.clear();
}
