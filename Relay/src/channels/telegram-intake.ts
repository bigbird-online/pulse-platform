/**
 * Telegram intake that never holds up polling.
 *
 * grammY's long polling handles one batch of updates at a time and fetches
 * the next batch only after every handler of the current batch has returned.
 * A handler that waited for an Agent turn would therefore stop its whole bot
 * from fetching. Here each update goes to its chat's lane and the poller moves
 * on at once. A lane runs its updates one after another, in arrival order, so
 * one chat's messages keep their order; lanes of different chats run
 * independently. Interrupt and status commands bypass the lanes so they are
 * answered even while that chat's lane is still working.
 *
 * The next getUpdates acknowledges every update of the batch, so Telegram
 * never sends a queued update again. Each one is recorded in the bot's intake
 * journal before that acknowledgement and stays there until its handling hands
 * a turn to the runtime (or ends without one); a restarted bot replays what
 * is left, in order, behind the turns the shutdown recorded for resume.
 */
import type { Context, MiddlewareFn } from 'grammy';
import type { Update } from 'grammy/types';
import type { ChatLanes } from './chat-lanes.js';
import { replayIntakeJournal, runJournaled, type ChannelIntakeJournal } from './intake-journal.js';

/** Commands answered at once, never queued behind the chat's earlier updates. */
export const TELEGRAM_IMMEDIATE_COMMANDS: ReadonlySet<string> = new Set(['stop', 'kill', 'status', 'help']);

/** Name of the bot command that opens `text` (`/stop@PulseBot now` → `stop`). */
export function telegramCommandName(text: string | undefined): string | undefined {
  const match = /^\/([A-Za-z0-9_]+)(?:@\w+)?(?:\s|$)/.exec(text ?? '');
  return match?.[1]?.toLowerCase();
}

/** Updates replayed after restart; they run inside their lane task already. */
const replaying = new WeakSet<object>();

/** First middleware of a bot: record each chat update and hand it to its lane. */
export function telegramIntakeMiddleware(lanes: ChatLanes, journal: ChannelIntakeJournal): MiddlewareFn<Context> {
  return (ctx, next) => {
    if (replaying.has(ctx.update)) return next();
    const chatId = ctx.chat?.id;
    if (chatId === undefined || TELEGRAM_IMMEDIATE_COMMANDS.has(telegramCommandName(ctx.message?.text) ?? '')) {
      return next();
    }
    const id = String(ctx.update.update_id);
    // Telegram sends an update again when its acknowledgement was lost; the
    // journal holds it already.
    if (journal.has(id)) return Promise.resolve();
    const record = journal.accept(String(chatId), id, ctx.update);
    lanes.run(record.lane, () => runJournaled(journal, record, next));
    return Promise.resolve();
  };
}

/**
 * Queue the updates an earlier process left in the journal (see
 * replayIntakeJournal); each one passes the intake middleware straight through.
 */
export function replayTelegramIntake(
  lanes: ChatLanes, journal: ChannelIntakeJournal, ready: Promise<void>, handle: (update: Update) => Promise<void>,
): number {
  return replayIntakeJournal(lanes, journal, ready, record => {
    const update = record.payload as Update;
    replaying.add(update);
    return handle(update);
  });
}
