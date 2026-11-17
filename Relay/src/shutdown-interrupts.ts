/**
 * Shutdown bookkeeping for the channel resume path.
 *
 * A turn interrupted by this shutdown is recorded with its own interruption
 * time, so resume windows measure from the interruption instead of from the
 * last recorded activity (a long turn may have none for many minutes).
 */
import type { PersistedSession, StreamJsonMessage, TurnSubmitOptions } from './types.js';
import type { QueuedTurn } from './maintenance-barrier.js';

interface SessionState {
  getSession(poolKey: string): PersistedSession | undefined;
  getAllSessions(): Record<string, PersistedSession>;
  updateSession(poolKey: string, patch: Partial<PersistedSession>): void;
  flush(): void;
}

export function busyAtShutdown(state: SessionState): Set<string> {
  return new Set(Object.entries(state.getAllSessions())
    .filter(([, session]) => session.wasBusyAtShutdown).map(([key]) => key));
}

/** Stamp sessions this shutdown marked busy; earlier unresolved flags keep their time. */
export function stampShutdownInterrupts(state: SessionState, before: Set<string>, at: number): string[] {
  const stamped = [...busyAtShutdown(state)].filter(key => !before.has(key));
  for (const key of stamped) state.updateSession(key, { shutdownInterruptedAt: at });
  return stamped;
}

type PendingTurn = NonNullable<PersistedSession['pendingMaintenanceTurns']>[number];

/**
 * A session's pending turns, each with its own arrival number. Records from
 * before `seq` existed get one here, saved with them: in list order, at least
 * their time in microsecond units (the scale of nextTurnSeq) and above the
 * record before, so two records of one millisecond stay two turns.
 */
function numberedPendingTurns(state: SessionState, poolKey: string): Array<PendingTurn & { seq: number }> {
  const pending = state.getSession(poolKey)?.pendingMaintenanceTurns ?? [];
  if (pending.every(turn => turn.seq !== undefined)) return pending as Array<PendingTurn & { seq: number }>;
  let last = -Infinity;
  const numbered = pending.map(turn => {
    const seq = turn.seq ?? Math.max(turn.queuedAt * 1000, last + 1);
    last = seq;
    return { ...turn, seq };
  });
  state.updateSession(poolKey, { pendingMaintenanceTurns: numbered });
  state.flush();
  return numbered;
}

/**
 * Turns still waiting at the maintenance barrier or in a runtime queue were
 * accepted from the user but never dispatched. Each goes, complete, into its
 * session's own pending list, kept in arrival order (the global arrival
 * number, so turns recorded by the barrier and by a runtime interleave
 * correctly); a turn already recorded under its arrival number is not
 * recorded twice. The interrupted-turn fields belong to the runtime callbacks.
 */
export function persistQueuedTurns(state: SessionState, turns: QueuedTurn[], at: number): string[] {
  const recorded = new Set<string>();
  for (const turn of turns) {
    const session = state.getSession(turn.tenantId);
    if (!session) continue;
    const pending = numberedPendingTurns(state, turn.tenantId);
    recorded.add(turn.tenantId);
    if (pending.some(existing => existing.seq === turn.seq)) continue;
    state.updateSession(turn.tenantId, {
      shutdownInterruptedAt: session.shutdownInterruptedAt ?? at,
      pendingMaintenanceTurns: [...pending, { queuedAt: turn.queuedAt, seq: turn.seq, message: turn.message }]
        .sort((a, b) => a.seq - b.seq),
    });
  }
  return [...recorded];
}

/**
 * Record one runtime's turns still queued at shutdown for its session. A
 * session unknown to state cannot be resumed; that is logged, not hidden.
 */
export function recordQueuedAtShutdown(
  state: SessionState | undefined, tenantId: string, runtime: string,
  turns: Array<{ queuedAt: number; seq: number; message: StreamJsonMessage }>,
): void {
  if (turns.length === 0) return;
  const recorded = state
    ? persistQueuedTurns(state, turns.map(turn => ({ tenantId, runtime, queuedAt: turn.queuedAt, seq: turn.seq, message: turn.message })), Date.now())
    : [];
  if (recorded.length === 0) {
    console.warn({ ts: new Date().toISOString(), level: 'warn', msg: 'queued turns at shutdown for an unknown session; not recorded',
      tenantId, runtime, count: turns.length });
  }
}

/**
 * Dispatch a session's pending turns in arrival order, each under its own
 * arrival number. A record leaves the list when the runtime accepts the turn
 * (see TurnArrival), not from a copy read before the send: a shutdown during
 * the replay hands a turn it had accepted back under the same number, so no
 * turn is lost or recorded twice. The next record goes out once the runtime
 * accepted (or finished) the previous one; the runtime queues keep the order,
 * and a turn recorded by a closed barrier does not hold up the rest. A send
 * failing before its receipt keeps its record for the next attempt.
 */
export async function replayPendingTurns(
  state: SessionState, poolKey: string,
  send: (message: StreamJsonMessage, options: TurnSubmitOptions) => Promise<void>,
): Promise<number> {
  const attempted = new Set<number>();
  let replayed = 0;
  for (;;) {
    const record = numberedPendingTurns(state, poolKey).find(turn => !attempted.has(turn.seq));
    if (!record) return replayed;
    const seq = record.seq;
    attempted.add(seq);
    await new Promise<void>((resolve, reject) => {
      let taken = false;
      send(record.message, { seq, resume: true, onAccepted: () => {
        removePendingTurn(state, poolKey, seq);
        if (!taken) { taken = true; resolve(); }
      } }).then(() => { if (!taken) { taken = true; resolve(); } }, (error: unknown) => {
        if (!taken) { taken = true; reject(error); return; }
        console.warn({ ts: new Date().toISOString(), level: 'warn', msg: 'replayed turn failed after the runtime accepted it',
          poolKey, error: String(error) });
      });
    });
    replayed++;
  }
}

function removePendingTurn(state: SessionState, poolKey: string, seq: number): void {
  const pending = numberedPendingTurns(state, poolKey);
  if (!pending.some(turn => turn.seq === seq)) return;
  state.updateSession(poolKey, { pendingMaintenanceTurns: pending.filter(turn => turn.seq !== seq) });
  state.flush();
}

/**
 * Take back a turn the switch barrier recorded at arrival, once the runtime
 * accepted it in this process. With nothing else left to resume,
 * the interruption time it set goes too, so a later shutdown measures its
 * resume window from its own interruption.
 */
export function unrecordPendingTurn(state: SessionState, poolKey: string, seq: number): void {
  removePendingTurn(state, poolKey, seq);
  const session = state.getSession(poolKey);
  if (session && !session.wasBusyAtShutdown && !(session.pendingMaintenanceTurns?.length)) {
    state.updateSession(poolKey, { shutdownInterruptedAt: undefined });
    state.flush();
  }
}
