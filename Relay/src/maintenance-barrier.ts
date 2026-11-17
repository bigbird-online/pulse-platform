/**
 * Maintenance barrier for a deployment's restart decision.
 *
 * While held, new conversation turns wait in arrival order instead of being
 * dispatched, and no scheduled or manual job run is claimed. Each hold has its
 * own token: only that token renews or releases it, so one deployment can never
 * release another's hold. A release (explicit, or the hold's own expiry when the
 * deployer goes away) dispatches the waiting turns.
 *
 * At shutdown the barrier closes: waiting turns, and every turn that still
 * arrives afterwards, go to a sink that records them for the channel resume
 * path at the moment they arrive; none is dispatched to the stopping runtime.
 *
 * The durable switch barrier (switch-barrier.ts) closes the same gate and
 * never opens by itself. While it blocks, a waiting turn gets no admission
 * receipt when a durable upstream record (channel intake journal, pending
 * list, durable entry) still holds it; any other waiting turn is written to
 * its session's pending list at arrival. A crash therefore loses no turn; on
 * shutdown the first kind stays with its record, the second is already saved.
 * A recorded turn leaves the pending list on the runtime's admission receipt,
 * not on release: a dispatch failing before the receipt keeps it for replay.
 * A turn whose only copy is in memory and whose write failed is `unsecured`:
 * the switch barrier is not granted (acquire answers `pending_unsecured`, a
 * retry writes again) and the restart decision counts it.
 *
 * When intake opens, an open listener may return the resume pass (a promise).
 * Until it settles only the turns it sends pass; every other turn waits,
 * kept durable as under the switch barrier. Then the waiting turns go out in
 * arrival order, so turns recorded by an earlier process run before later ones.
 * The startup resume gate (startupResume) holds that state from before intake
 * opens until the startup resume pass settles, however early a release comes.
 */
import { randomUUID } from 'node:crypto';
import type { StreamJsonMessage, TurnArrival } from './types.js';
import type { SwitchBarrierStore, SwitchClaim, SwitchResult, SwitchStatus } from './switch-barrier.js';

export const MAINTENANCE_HOLD_MAX_MS = 10 * 60 * 1000;

// Starts from the clock (in microsecond units) so a later process numbers
// above the turns an earlier one recorded.
let lastTurnSeq = Date.now() * 1000;

/** Next arrival number of a turn: strictly increasing within the process. */
export function nextTurnSeq(): number {
  lastTurnSeq = Math.max(lastTurnSeq + 1, Date.now() * 1000);
  return lastTurnSeq;
}

export interface QueuedTurn {
  tenantId: string;
  runtime: string;
  queuedAt: number;
  /** Arrival number; orders recorded turns, identifies a replayed one. */
  seq: number;
  /** The complete message, attachments included; never reported outward. */
  message: StreamJsonMessage;
}

export interface MaintenanceStatus {
  /** The short deployment hold only (unchanged meaning). */
  held: boolean;
  expiresAt?: number;
  /** Tenants and runtimes only; message text never leaves the process. */
  queued: Array<{ tenantId: string; runtime: string }>;
  /** Waiting turns held only in memory whose durable write failed (absent when none). */
  unsecured?: number;
  /** Present only with a switch barrier store. */
  capabilities?: string[];
  /** Either barrier closes intake (present only with a switch barrier store). */
  blocking?: boolean;
  switch?: SwitchStatus;
}

/** `held_here`: the resume pass sent the record of a turn already waiting here; not dispatched again. */
export type Admission = 'dispatch' | 'held_here';

export type HoldResult =
  | { ok: true; token: string; status: MaintenanceStatus }
  | { ok: false; error: 'held_by_another' | 'closed'; status: MaintenanceStatus };

export interface MaintenanceBarrier {
  /** Without a token: a new hold, refused while another one is held. With
   *  the token of the current hold: a renewal. */
  hold(ttlMs: number, token?: string): HoldResult;
  /** Releases only the hold that `token` names. */
  release(token: string): boolean;
  /** True while any barrier (short hold, switch barrier, shutdown) admits no new work. */
  isHeld(): boolean;
  /** True while the durable switch barrier blocks (held, expired_unreleased or invalid). */
  switchBlocking(): boolean;
  /** True when `token` names the hold in force. */
  owns(token: string): boolean;
  /** Resolves at once unless held or resuming; then when released. A turn
   *  held or recorded here is accepted (see TurnArrival). */
  admit(tenantId: string, runtime: string, message: StreamJsonMessage, arrival?: TurnArrival): Promise<Admission>;
  /** Shutdown: hand waiting and later turns to `sink`; nothing is dispatched. */
  close(sink: (turn: QueuedTurn) => void): void;
  status(): MaintenanceStatus;
  /** The durable switch barrier; undefined without a store. */
  readonly switchBarrier?: SwitchBarrierStore;
  /** Takes the switch barrier, then writes every waiting turn that must be
   *  durable. `pending_unsecured` when one held only in memory could not be
   *  written: the barrier stays held, the same acquire retries the write. */
  acquireSwitch(request: SwitchClaim & { leaseMs: unknown }): SwitchResult;
  /** Startup: from now until the pass handed to the returned function settles,
   *  only resume turns pass (as while resuming). The pass runs at once, or
   *  when intake opens if a barrier is in force; call before intake starts. */
  startupResume(): (pass: () => void | Promise<unknown>) => 'now' | 'deferred';
  /** Called each time intake opens again (every barrier gone). A returned
   *  promise is a resume pass: other turns wait until it settles. */
  onOpen(listener: () => void | Promise<unknown>): void;
}

export interface MaintenanceBarrierOptions {
  /** Durable switch barrier (see switch-barrier.ts). */
  switchBarrier?: SwitchBarrierStore;
  /** Writes a turn with no durable upstream record into its session's
   *  pending list; true when it was written durably. */
  recordTurn?: (turn: QueuedTurn) => boolean;
  /** Removes that record again once the runtime accepted the turn. */
  unrecordTurn?: (turn: QueuedTurn) => void;
}

interface WaitingTurn extends QueuedTurn {
  resolve: (admission: Admission) => void;
  arrival?: TurnArrival;
  /** The admission receipt was given (the upstream record is released). */
  accepted: boolean;
  /** Written to the session's pending list by recordTurn. */
  recorded: boolean;
  /** recordTurn ran: memory may hold the record even when the write failed. */
  recordTried?: boolean;
  /** Must be durable, has no upstream record, and its write failed. */
  unsecured?: boolean;
}

export function createMaintenanceBarrier(now: () => number = Date.now, options: MaintenanceBarrierOptions = {}): MaintenanceBarrier {
  let token: string | undefined;
  let expiresAt: number | undefined;
  let timer: ReturnType<typeof setTimeout> | undefined;
  let sink: ((turn: QueuedTurn) => void) | undefined;
  const waiting: WaitingTurn[] = [];
  const store = options.switchBarrier;
  const openListeners: Array<() => void | Promise<unknown>> = [];
  /** Resume passes started on open and not yet settled. */
  let resuming = 0;

  const switchBlocking = (): boolean => store?.blocking() ?? false;
  const blocking = (): boolean => token !== undefined || sink !== undefined || switchBlocking();
  const plain = ({ tenantId, runtime, queuedAt, seq, message }: WaitingTurn): QueuedTurn =>
    ({ tenantId, runtime, queuedAt, seq, message });

  function status(): MaintenanceStatus {
    const base: MaintenanceStatus = {
      held: token !== undefined,
      ...(token !== undefined ? { expiresAt } : {}),
      queued: waiting.map(({ tenantId, runtime }) => ({ tenantId, runtime })),
    };
    const unsecured = waiting.filter(turn => turn.unsecured).length;
    if (unsecured > 0) base.unsecured = unsecured;
    if (!store) return base;
    return { ...base, capabilities: ['maintenance-hold/1', 'switch-barrier/1'], blocking: blocking(), switch: store.status() };
  }

  /**
   * Under the switch barrier a waiting turn must survive a crash. A turn whose
   * channel journal or pending list still holds it keeps that record (no
   * receipt yet); any other turn is written to its session's pending list.
   * False when that write failed: the turn is then `unsecured`.
   */
  function secure(turn: WaitingTurn): boolean {
    if (turn.recorded) return true;
    if (!turn.accepted && turn.arrival?.owned) return true;
    if (options.recordTurn) {
      turn.recordTried = true;
      try { turn.recorded = options.recordTurn(plain(turn)); } catch { turn.recorded = false; }
    }
    turn.unsecured = !turn.recorded;
    if (turn.unsecured) {
      console.error({ ts: new Date().toISOString(), level: 'error', module: 'maintenance-barrier',
        msg: 'turn waiting at the barrier could not be recorded durably; only memory holds it, restart unsafe', tenantId: turn.tenantId });
    }
    if (!turn.accepted) { turn.accepted = true; turn.arrival?.accepted(); }
    return turn.recorded;
  }

  /** Writes every waiting turn that must be durable; the number still unsecured. */
  function secureWaiting(): number {
    let left = 0;
    for (const turn of waiting) if (!secure(turn)) left++;
    return left;
  }

  function dispatch(turn: WaitingTurn): void {
    // A failed write may still have left the record in memory, to be saved by
    // a later write; it goes the same way as a written one.
    if (turn.recorded || turn.recordTried) {
      if (turn.arrival) {
        // The runtime's receipt takes the record back; until then it stays.
        let taken = false;
        turn.arrival.accepted = () => {
          if (taken) return;
          taken = true;
          options.unrecordTurn?.(plain(turn));
        };
      } else {
        options.unrecordTurn?.(plain(turn));
      }
    }
    turn.resolve('dispatch');
  }

  /** Waiting turns out in arrival order; while resuming, the resume pass's only. */
  function drain(): void {
    if (blocking()) return;
    waiting.sort((a, b) => a.seq - b.seq);
    const out = resuming > 0 ? waiting.filter(turn => turn.arrival?.resume) : waiting.slice();
    for (const turn of out) waiting.splice(waiting.indexOf(turn), 1);
    for (const turn of out) dispatch(turn);
  }

  function settle(): void {
    resuming--;
    drain();
  }

  function maybeOpen(): void {
    if (blocking()) return;
    for (const listener of openListeners) {
      try {
        const pass = listener();
        if (pass && typeof pass.then === 'function') {
          resuming++;
          pass.then(settle, (error: unknown) => {
            console.error({ ts: new Date().toISOString(), level: 'error', module: 'maintenance-barrier', msg: 'resume pass failed', error: String(error) });
            settle();
          });
        }
      } catch (error) {
        console.error({ ts: new Date().toISOString(), level: 'error', module: 'maintenance-barrier', msg: 'open listener failed', error: String(error) });
      }
    }
    drain();
  }

  function openHold(): void {
    if (timer) clearTimeout(timer);
    timer = undefined;
    token = undefined;
    expiresAt = undefined;
    maybeOpen();
  }

  store?.onChange(() => {
    if (switchBlocking()) secureWaiting();
    else maybeOpen();
  });

  return {
    hold(ttlMs, requested) {
      if (sink) return { ok: false, error: 'closed', status: status() };
      if (token !== undefined && requested !== token) return { ok: false, error: 'held_by_another', status: status() };
      const ttl = Math.min(Math.max(1000, Number.isFinite(ttlMs) ? ttlMs : 0), MAINTENANCE_HOLD_MAX_MS);
      token = token ?? randomUUID();
      expiresAt = now() + ttl;
      if (timer) clearTimeout(timer);
      timer = setTimeout(openHold, ttl);
      timer.unref?.();
      return { ok: true, token, status: status() };
    },
    release(requested) {
      if (token === undefined || requested !== token) return false;
      openHold();
      return true;
    },
    isHeld: blocking,
    switchBlocking,
    owns: requested => token !== undefined && requested === token,
    admit(tenantId, runtime, message, arrival) {
      // The resume pass sends a record this process holds waiting: the
      // waiting turn dispatches it in its place.
      if (arrival?.resume && waiting.some(turn => turn.tenantId === tenantId && turn.seq === arrival.seq)) {
        return Promise.resolve('held_here');
      }
      if (!blocking() && (resuming === 0 || arrival?.resume)) return Promise.resolve('dispatch');
      const turn = { tenantId, runtime, queuedAt: now(), seq: arrival?.seq ?? nextTurnSeq(), message };
      if (sink) {
        // The receipt goes first: a replayed record is removed on receipt and
        // the sink records it again, under the same arrival number.
        arrival?.accepted();
        sink(turn);  // recorded now; never dispatched
        return new Promise<Admission>(() => {});
      }
      return new Promise<Admission>(resolve => {
        const entry: WaitingTurn = { ...turn, resolve, arrival, accepted: false, recorded: false };
        if (switchBlocking() || resuming > 0) secure(entry);
        else { entry.accepted = true; arrival?.accepted(); }
        waiting.push(entry);
      });
    },
    close(target) {
      sink = target;
      store?.close();
      for (const turn of waiting.splice(0)) {
        // Its journal or pending list still holds it; the next process replays it.
        if (!turn.accepted) continue;
        // Already in the session's pending list under its arrival number.
        if (turn.recorded) continue;
        target(plain(turn));
      }
    },
    status,
    switchBarrier: store,
    acquireSwitch(request) {
      if (!store) throw new Error('no switch barrier store');
      const result = store.acquire(request);
      if (!result.ok) return result;
      // A retried acquire renews without a change notice: write here as well.
      const left = secureWaiting();
      if (left === 0) return result;
      console.error({ ts: new Date().toISOString(), level: 'error', module: 'maintenance-barrier',
        msg: 'switch barrier held but waiting turns are not durable; acquire refused until a retry writes them', unsecured: left });
      return { ok: false, error: 'pending_unsecured', status: store.status() };
    },
    startupResume() {
      // Counted as a resume pass from now: no turn but the pass's own passes,
      // even when a release opens the gate before the pass has started.
      resuming++;
      let started = false;
      return pass => {
        if (started) throw new Error('startup resume already started');
        started = true;
        const run = (): void => {
          let result: void | Promise<unknown>;
          try { result = pass(); } catch (error) { result = Promise.reject(error); }
          Promise.resolve(result).then(settle, (error: unknown) => {
            console.error({ ts: new Date().toISOString(), level: 'error', module: 'maintenance-barrier', msg: 'startup resume pass failed', error: String(error) });
            settle();
          });
        };
        if (!blocking()) { run(); return 'now'; }
        let done = false;
        openListeners.push(() => { if (done) return; done = true; run(); });
        return 'deferred';
      };
    },
    onOpen(listener) { openListeners.push(listener); },
  };
}
