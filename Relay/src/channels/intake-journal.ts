/**
 * Intake journal of one channel bot.
 *
 * A channel that acknowledges an update before handling it (Telegram confirms
 * every update of a batch with the next getUpdates; the Feishu SDK confirms an
 * event when its handler returns) is the only holder of that update until a
 * runtime has it. The journal keeps each such update, in
 * state, until a runtime has taken over its turn:
 *
 * - `accept` records the update in memory; `persist` writes it durably and
 *   runs before the platform is told the update was received.
 * - The update is released when the runtime's admission receipt for its turn
 *   arrives (TurnArrival.accepted: the turn sits in the barrier, a runtime
 *   queue, a dispatch preparation or in flight, all of which a shutdown hands
 *   over), when a send for it fails while the bot is running (the sender is
 *   told), or when its handling ends without a turn.
 * - After `close` (shutdown, or a reload that replaces the bot) no recorded
 *   update starts in this bot, and only a receipt releases one.
 *
 * One process keeps one book of records per journal key, shared by the
 * successive bots of a key (a reload stops a bot and creates another): an
 * update has one record, and the bot that started handling it holds it
 * until its handling settles. A newer bot replays only the records nobody
 * holds; a record whose handling was cut short by `close` goes to the newest
 * running bot, or stays for the next process.
 */
import { AsyncLocalStorage } from 'node:async_hooks';
import type { ChatLanes } from './chat-lanes.js';
import { WRAPPED_RUNTIME } from '../runtime-identity.js';
import type { ChannelIntakeRecord, StateManager, TurnSubmitOptions } from '../types.js';

export interface ChannelIntakeJournal {
  /** Recorded updates no bot is handling (left by an earlier process or a replaced bot), in arrival order. */
  recovered(): ChannelIntakeRecord[];
  /** True while an update with this id is recorded and not released. */
  has(id: string): boolean;
  accept(lane: string, id: string, payload: unknown): ChannelIntakeRecord;
  /** Write accepted updates durably; throws when they could not be written. */
  persist(): void;
  release(record: ChannelIntakeRecord): void;
  /** This bot starts handling the update; no other bot replays it meanwhile. */
  hold(record: ChannelIntakeRecord): void;
  /** Handling cut short by `close`: the record goes to the newest running bot, if any. */
  letGo(record: ChannelIntakeRecord): void;
  /** How this bot takes over a record let go by a replaced one. */
  onAdopt(adopt: (record: ChannelIntakeRecord) => void): void;
  close(): void;
  readonly closed: boolean;
}

interface Book {
  records: ChannelIntakeRecord[];
  held: Set<ChannelIntakeRecord>;
  dirty: boolean;
  /** Running bots of this key, oldest first. */
  bots: Array<{ closed: boolean; adopt?: (record: ChannelIntakeRecord) => void }>;
}

const books = new WeakMap<StateManager, Map<string, Book>>();

function bookFor(state: StateManager | undefined, key: string, durable: boolean): Book {
  const fresh = (): Book => ({ records: durable ? state!.getChannelIntake!(key) : [], held: new Set(), dirty: false, bots: [] });
  if (!state) return fresh();
  let byKey = books.get(state);
  if (!byKey) books.set(state, byKey = new Map());
  let book = byKey.get(key);
  if (!book) byKey.set(key, book = fresh());
  return book;
}

export function createChannelIntakeJournal(state: StateManager | undefined, key: string): ChannelIntakeJournal {
  const durable = Boolean(state?.getChannelIntake && state.setChannelIntake);
  const book = bookFor(state, key, durable);
  const bot: Book['bots'][number] = { closed: false };
  book.bots.push(bot);

  function store(): void {
    if (durable) state!.setChannelIntake!(key, book.records);
  }

  return {
    recovered: () => book.records.filter(record => !book.held.has(record)),
    has: id => book.records.some(record => record.id === id),
    accept(lane, id, payload) {
      const record: ChannelIntakeRecord = { lane, id, receivedAt: Date.now(), payload };
      book.records.push(record);
      store();
      book.dirty = true;
      return record;
    },
    persist() {
      if (!book.dirty) return;
      if (durable) state!.flushStrict();
      book.dirty = false;
    },
    release(record) {
      book.held.delete(record);
      const index = book.records.indexOf(record);
      if (index < 0) return;
      book.records.splice(index, 1);
      store();
      if (durable) state!.flush();
    },
    hold(record) { book.held.add(record); },
    letGo(record) {
      book.held.delete(record);
      if (!book.records.includes(record)) return;
      const heir = [...book.bots].reverse().find(candidate => !candidate.closed && candidate.adopt);
      heir?.adopt!(record);
    },
    onAdopt(adopt) { bot.adopt = adopt; },
    close() {
      bot.closed = true;
      book.bots.splice(book.bots.indexOf(bot), 1);
    },
    get closed() { return bot.closed; },
  };
}

/** Handling of one recorded update and the turns it has submitted. */
interface Handling {
  journal: ChannelIntakeJournal;
  record: ChannelIntakeRecord;
  /** Turns submitted and neither admitted nor failed. */
  open: number;
  /** A turn of this update was admitted by a runtime. */
  admitted: boolean;
  finished: boolean;
  settled: boolean;
}

/** The recorded update whose handling the current async context belongs to. */
const handling = new AsyncLocalStorage<Handling>();

/**
 * Settle a handling once no submitted turn is pending: released when a
 * runtime admitted its turn (the handler's remaining work is not replayed) or
 * when it finished while the bot runs; let go when it finished after `close`
 * without an admitted turn (it may have been cut short).
 */
function settle(current: Handling): void {
  if (current.settled || current.open > 0) return;
  if (current.admitted || (current.finished && !current.journal.closed)) {
    current.settled = true;
    current.journal.release(current.record);
  } else if (current.finished) {
    current.settled = true;
    current.journal.letGo(current.record);
  }
}

/** Lane task of a recorded update: handle it; the journal keeps it until settled. */
export async function runJournaled(journal: ChannelIntakeJournal, record: ChannelIntakeRecord, work: () => Promise<unknown>): Promise<void> {
  // Closed: never started here; a newer bot or the next process has it.
  if (journal.closed) return;
  journal.hold(record);
  const current: Handling = { journal, record, open: 0, admitted: false, finished: false, settled: false };
  try {
    await handling.run(current, work);
  } finally {
    current.finished = true;
    settle(current);
  }
}

/**
 * Queue the updates nobody is handling (left by an earlier process or a
 * replaced bot), each in its lane,
 * behind `ready` (the resume of the turns that shutdown recorded, which
 * arrived before them). Live updates of the same lane queue behind.
 */
export function replayIntakeJournal(
  lanes: ChatLanes, journal: ChannelIntakeJournal, ready: Promise<void>,
  handle: (record: ChannelIntakeRecord) => Promise<unknown>,
): number {
  const run = (record: ChannelIntakeRecord): void =>
    lanes.run(record.lane, () => runJournaled(journal, record, () => handle(record)));
  // Records a replaced bot lets go later join their lanes here.
  journal.onAdopt(run);
  const recovered = journal.recovered();
  const gated = new Set<string>();
  for (const record of recovered) {
    if (!gated.has(record.lane)) {
      gated.add(record.lane);
      lanes.run(record.lane, () => ready);
    }
    run(record);
  }
  return recovered.length;
}

/** Submit one turn of the update being handled, releasing it on the runtime's receipt. */
function submitJournaled(
  send: (options: TurnSubmitOptions) => unknown, options: TurnSubmitOptions | undefined,
): unknown {
  const current = handling.getStore();
  if (!current || current.settled) return send(options ?? {});
  current.open++;
  let open = true;
  const close = (): void => { if (open) { open = false; current.open--; } };
  const sent = send({
    ...options,
    onAccepted: () => {
      options?.onAccepted?.();
      current.admitted = true;
      close();
      settle(current);
    },
  });
  // A send that returns has not by itself put the turn anywhere a shutdown
  // hands over (a runtime may still hold it in an unrecorded queue): only the
  // receipt releases. A failed send settles with the handling: done while the
  // bot runs (the sender is told), let go after `close` (it may have been the
  // shutdown itself).
  Promise.resolve(sent).catch(() => {
    close();
    settle(current);
  });
  return sent;
}

/**
 * The runtime as a bot's handlers see it: each send carries an admission
 * receipt that releases the update being handled once a runtime has taken
 * over its turn.
 */
export function intakeAwareRuntime<T extends object>(runtime: T): T {
  return new Proxy(runtime, {
    get(target, property) {
      // Same runtime for state kept per runtime (see underlyingRuntime).
      if (property === WRAPPED_RUNTIME) return target;
      const value = Reflect.get(target, property, target) as unknown;
      if (typeof value !== 'function') return value;
      const fn = value as (...args: unknown[]) => unknown;
      if (property === 'send') {
        return (tenantId: unknown, message: unknown, options?: TurnSubmitOptions) =>
          submitJournaled(submit => fn.call(target, tenantId, message, submit), options);
      }
      if (property === 'sendIdempotent') {
        return (tenantId: unknown, message: unknown, key: unknown, options?: TurnSubmitOptions) =>
          submitJournaled(submit => fn.call(target, tenantId, message, key, submit), options);
      }
      return fn.bind(target);
    },
  });
}
