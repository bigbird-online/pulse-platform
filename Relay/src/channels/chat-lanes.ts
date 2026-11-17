/**
 * Per-conversation lanes for channel intake.
 *
 * A lane runs its tasks one after another, in arrival order, so one
 * conversation's messages keep their order; lanes of different keys run
 * independently. `run` returns at once, so the receiving loop (polling,
 * websocket frame handler) never waits for the work it hands over.
 */
export interface ChatLanes {
  /** Queue `task` behind the earlier tasks of `key`; returns at once. */
  run(key: string, task: () => Promise<void>): void;
  /** Tasks of `key` not yet finished, the running one included. */
  pending(key: string): number;
}

export function createChatLanes(onError: (key: string, error: unknown) => void): ChatLanes {
  const tails = new Map<string, Promise<void>>();
  const counts = new Map<string, number>();
  return {
    run(key, task) {
      counts.set(key, (counts.get(key) ?? 0) + 1);
      const next = (tails.get(key) ?? Promise.resolve())
        .then(task)
        .catch(error => onError(key, error))
        .finally(() => {
          const left = (counts.get(key) ?? 1) - 1;
          if (left > 0) counts.set(key, left);
          else counts.delete(key);
          if (tails.get(key) === next) tails.delete(key);
        });
      tails.set(key, next);
    },
    pending: key => counts.get(key) ?? 0,
  };
}
