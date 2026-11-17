/**
 * Shared residency and unsolicited-turn primitives for conversation runtimes.
 *
 * A runtime may keep working after its Relay-initiated turn has ended: CC runs
 * background Bash commands and subagents, then wakes on their completion and
 * emits a new assistant/result turn that no human message started. Every
 * runtime adapter uses these primitives so that
 *   - idle reclaim never kills a process with pending background work, while a
 *     hard cap still bounds residency;
 *   - output of such a turn is tagged with its origin, so the manager does not
 *     settle a queued human turn with it and channels can render it.
 */
import type { StreamJsonMessage } from './types.js';

/** Turn opened by the runtime itself, not by a Relay send. */
export const RUNTIME_NOTIFICATION_TURN_ORIGIN = 'runtime-notification';
export type TurnOrigin = 'user' | typeof RUNTIME_NOTIFICATION_TURN_ORIGIN;
export interface TurnAcceptedMeta {
  origin: TurnOrigin;
}
export const RUNTIME_NOTIFICATION_TURN: TurnAcceptedMeta = Object.freeze({ origin: RUNTIME_NOTIFICATION_TURN_ORIGIN });

export const DEFAULT_BACKGROUND_WORK_HARD_CAP_MS = 4 * 60 * 60 * 1000;

/** `RELAY_RUNTIME_BACKGROUND_HARD_CAP_MS` overrides the 4 h residency cap. */
export function resolveBackgroundWorkHardCapMs(env: NodeJS.ProcessEnv = process.env): number {
  const raw = Number(env['RELAY_RUNTIME_BACKGROUND_HARD_CAP_MS']);
  return Number.isFinite(raw) && raw > 0 ? raw : DEFAULT_BACKGROUND_WORK_HARD_CAP_MS;
}

/**
 * Idle reclaim decision shared by every runtime. Output refreshes
 * `lastActivity`; pending background work defers reclaim until the oldest
 * pending item exceeds `hardCapMs`.
 */
export function shouldReclaimIdleRuntime(input: {
  now: number;
  lastActivity: number;
  idleTimeoutMs: number;
  backgroundWorkSince?: number;
  hardCapMs: number;
}): boolean {
  if (input.now - input.lastActivity < input.idleTimeoutMs) return false;
  if (input.backgroundWorkSince === undefined) return true;
  return input.now - input.backgroundWorkSince >= input.hardCapMs;
}

/** Records that carry turn content (as opposed to system bookkeeping). */
export function isTurnContentRecord(msg: { type?: unknown }): boolean {
  return msg.type === 'assistant' || msg.type === 'user' || msg.type === 'stream_event';
}

export function tagRuntimeNotificationTurn(msg: StreamJsonMessage): StreamJsonMessage {
  return { ...msg, turn_origin: RUNTIME_NOTIFICATION_TURN_ORIGIN };
}

export function isRuntimeNotificationTurnMessage(msg: StreamJsonMessage): boolean {
  return msg.turn_origin === RUNTIME_NOTIFICATION_TURN_ORIGIN;
}

const TASK_ID_PATTERN = /<task-id>([^<]+)<\/task-id>/g;

function userText(record: Record<string, unknown>): string {
  const message = record['message'] as { content?: unknown } | undefined;
  const content = message?.content ?? record['content'];
  if (typeof content === 'string') return content;
  if (!Array.isArray(content)) return '';
  return content.map((block) => {
    const b = block as { type?: string; text?: unknown };
    return b?.type === 'text' && typeof b.text === 'string' ? b.text : '';
  }).join('');
}

/**
 * Tracks CC background tasks (run_in_background Bash, async subagents,
 * backgrounded MCP tasks) from either CC stream-json/SDK messages or CC
 * transcript records:
 *   - `system/task_started` (`is_backgrounded !== false`) opens a task;
 *   - `system/task_notification` settles it;
 *   - `system/background_tasks_changed` replaces the whole set (level signal);
 *   - transcript fallback: `toolUseResult.backgroundTaskId` / async agent
 *     launch opens, a `<task-notification>` user record settles.
 * Ambient tasks are not activity and are ignored.
 */
export class BackgroundTaskTracker {
  private readonly tasks = new Map<string, number>();

  constructor(private readonly now: () => number = Date.now) {}

  observe(record: Record<string, unknown>): void {
    if (record['type'] === 'system') {
      const subtype = record['subtype'];
      const taskId = typeof record['task_id'] === 'string' ? record['task_id'] : undefined;
      if (subtype === 'task_started' && taskId) {
        if (record['ambient'] === true || record['skip_transcript'] === true) return;
        if (record['is_backgrounded'] === false) return;
        if (!this.tasks.has(taskId)) this.tasks.set(taskId, this.now());
      } else if (subtype === 'task_updated' && taskId) {
        const patch = record['patch'] as { is_backgrounded?: unknown; status?: unknown } | undefined;
        if (patch?.status === 'completed' || patch?.status === 'failed' || patch?.status === 'killed') {
          this.tasks.delete(taskId);
        } else if (patch?.is_backgrounded === true && !this.tasks.has(taskId)) {
          this.tasks.set(taskId, this.now());
        }
      } else if (subtype === 'task_notification' && taskId) {
        this.tasks.delete(taskId);
      } else if (subtype === 'background_tasks_changed' && Array.isArray(record['tasks'])) {
        const live = new Set<string>();
        for (const task of record['tasks'] as Array<{ task_id?: unknown; ambient?: unknown }>) {
          if (typeof task?.task_id === 'string' && task.ambient !== true) live.add(task.task_id);
        }
        for (const id of [...this.tasks.keys()]) if (!live.has(id)) this.tasks.delete(id);
        for (const id of live) if (!this.tasks.has(id)) this.tasks.set(id, this.now());
      }
      return;
    }
    if (record['type'] !== 'user') return;
    const result = (record['toolUseResult'] ?? record['tool_use_result']) as Record<string, unknown> | undefined;
    if (result && typeof result === 'object') {
      const id = typeof result['backgroundTaskId'] === 'string'
        ? result['backgroundTaskId']
        : result['status'] === 'async_launched' && typeof result['agentId'] === 'string' ? result['agentId'] : undefined;
      if (id && !this.tasks.has(id)) this.tasks.set(id, this.now());
    }
    const text = userText(record);
    if (text.includes('<task-notification>')) {
      for (const match of text.matchAll(TASK_ID_PATTERN)) this.tasks.delete(match[1]!.trim());
    }
  }

  /** Start time of the oldest pending task, or undefined when none. */
  pendingSince(): number | undefined {
    let oldest: number | undefined;
    for (const since of this.tasks.values()) if (oldest === undefined || since < oldest) oldest = since;
    return oldest;
  }

  get size(): number {
    return this.tasks.size;
  }

  clear(): void {
    this.tasks.clear();
  }
}
