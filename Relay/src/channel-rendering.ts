/**
 * Channel-neutral rendering contract for one conversation turn.
 *
 * The identity is an opaque tenant/session/turn/channel scope. Body text is
 * deliberately never used as a durable idempotency key: adapters may retain it
 * in memory while rendering, but must not persist full-text hashes.
 */
export type ChannelRenderFrame = {
  identity: string;
  version: number;
  text: string;
  final: boolean;
};

/** One accepted-turn acknowledgement shared by every conversational channel. */
export const TURN_ACCEPTED_STATUS_TEXT = '▶️ 已收到，开始处理…';
/** Opening status of a turn the runtime started itself (background task finished). */
export const RUNTIME_NOTIFICATION_STATUS_TEXT = '🔔 后台任务有新结果，正在整理回复…';

/** First line of the reply of a runtime-notification turn. */
export const RUNTIME_NOTIFICATION_REPLY_LEAD = '🔔 后台任务有新结果';

/** Status line for a newly accepted turn, by origin. */
export function turnAcceptedStatusText(meta?: { origin?: string }): string {
  return meta?.origin === 'runtime-notification' ? RUNTIME_NOTIFICATION_STATUS_TEXT : TURN_ACCEPTED_STATUS_TEXT;
}

/** Line that leads the delivered reply of a turn, by origin. */
export function turnReplyLead(meta?: { origin?: string }): string | undefined {
  return meta?.origin === 'runtime-notification' ? RUNTIME_NOTIFICATION_REPLY_LEAD : undefined;
}
/** Confirm only that Relay delivered this turn's authoritative final response. */
export const TURN_GENERATED_STATUS_TEXT = '✅ 本轮回复已生成';

/**
 * Keep tool-only activity visible without turning every tool call into a
 * transport update: transports coalesce the latest actual count by time.
 * This is channel-neutral so Telegram and Feishu cannot
 * drift into different progress heuristics.
 */
export function activityMilestone(step: number): string | null {
  if (!Number.isInteger(step) || step < 1) return null;
  return `⏳ 仍在工作，已推进 ${step} 个执行步骤…`;
}

export type TurnActivityProgress = Readonly<{
  step: number;
  text: string | null;
}>;

/**
 * Count runtime effects from the accepted-turn boundary. Both channel adapters
 * use this state machine so reset, increment, and milestone semantics cannot
 * drift. Effects outside an active turn are ignored rather than attributed to
 * the next turn.
 */
export class TurnActivityCounter {
  private readonly steps = new Map<string, number>();

  begin(turnKey: string): void {
    this.steps.set(turnKey, 0);
  }

  advance(turnKey: string): TurnActivityProgress | null {
    const current = this.steps.get(turnKey);
    if (current === undefined) return null;
    const step = current + 1;
    this.steps.set(turnKey, step);
    return { step, text: activityMilestone(step) };
  }

  end(turnKey: string): void {
    this.steps.delete(turnKey);
  }
}

function containsEither(left: string, right: string): string | null {
  if (left === right) return left;
  if (right.startsWith(left) || right.includes(left)) return right;
  if (left.startsWith(right) || left.includes(right)) return left;
  return null;
}

export class ChannelTurnRenderingState {
  readonly identity: string;
  /** Semantic turn body only. Transient status/activity is never stored here. */
  private text = '';
  /** Latest actual tool-activity count stays visible until replaced or finalized. */
  private activityText = '';
  private renderedText = '';
  private version = 0;
  private finalized = false;
  private readonly seenEventIds = new Set<string>();
  /** Line placed above the final reply only (never in the turn body). */
  private readonly replyLead?: string;

  constructor(identity: string, options: { replyLead?: string } = {}) {
    this.identity = identity;
    this.replyLead = options.replyLead;
  }

  currentText(): string {
    return this.text;
  }

  isFinalized(): boolean {
    return this.finalized;
  }

  /** Release active-turn body and replay ids after the adapter terminalizes. */
  dispose(): void {
    this.text = '';
    this.activityText = '';
    this.renderedText = '';
    this.seenEventIds.clear();
  }

  private acceptEvent(eventId?: string): boolean {
    if (!eventId) return true;
    if (this.seenEventIds.has(eventId)) return false;
    this.seenEventIds.add(eventId);
    // Event ids are opaque bounded sequence ids. Avoid an unbounded replay set.
    if (this.seenEventIds.size > 512) {
      const oldest = this.seenEventIds.values().next().value as string | undefined;
      if (oldest) this.seenEventIds.delete(oldest);
    }
    return true;
  }

  private frame(next: string, final: boolean): ChannelRenderFrame | null {
    if (!next) return null;
    if (next === this.renderedText && final === this.finalized) return null;
    this.renderedText = next;
    this.finalized = final;
    this.version += 1;
    return { identity: this.identity, version: this.version, text: next, final };
  }

  private activeText(): string {
    return this.activityText
      ? (this.text ? `${this.text}\n\n${this.activityText}` : this.activityText)
      : this.text;
  }

  /** A visible, non-semantic placeholder replaced by the first real turn body. */
  status(status: string, eventId?: string): ChannelRenderFrame | null {
    if (this.finalized || !status || !this.acceptEvent(eventId)) return null;
    return this.frame(status, false);
  }

  /** Visible activity overlay which never becomes part of the turn body. */
  activity(status: string, eventId?: string): ChannelRenderFrame | null {
    if (this.finalized || !status || !this.acceptEvent(eventId)) return null;
    this.activityText = status;
    return this.frame(this.activeText(), false);
  }

  /** A true protocol delta. Equal text from distinct events is not a duplicate. */
  delta(delta: string, eventId?: string): ChannelRenderFrame | null {
    if (this.finalized || !delta || !this.acceptEvent(eventId)) return null;
    this.text = `${this.text}${delta}`;
    return this.frame(this.activeText(), false);
  }

  /** A cumulative runtime snapshot (for example Pi short prefix -> long prefix). */
  snapshot(snapshot: string, eventId?: string): ChannelRenderFrame | null {
    if (this.finalized || !snapshot || !this.acceptEvent(eventId)) return null;
    const contained = containsEither(this.text, snapshot);
    this.text = contained ?? snapshot;
    return this.frame(this.activeText(), false);
  }

  /** A discrete commentary record. Independent records remain readable paragraphs. */
  commentary(commentary: string, eventId?: string): ChannelRenderFrame | null {
    if (this.finalized || !commentary || !this.acceptEvent(eventId)) return null;
    const contained = containsEither(this.text, commentary);
    this.text = contained ?? (this.text ? `${this.text}\n\n${commentary}` : commentary);
    return this.frame(this.activeText(), false);
  }

  /** The sole authoritative terminal body for this turn. */
  final(finalText?: string, eventId?: string, exact = false): ChannelRenderFrame | null {
    if (this.finalized) return null;
    if (!this.acceptEvent(eventId)) return null;
    const candidate = finalText || this.text;
    if (!candidate) return null;
    const contained = containsEither(this.text, candidate);
    this.text = exact && finalText ? finalText : contained ?? candidate;
    this.activityText = '';
    return this.frame(this.replyLead ? `${this.replyLead}\n\n${this.text}` : this.text, true);
  }
}
