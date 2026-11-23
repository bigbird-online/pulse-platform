import type { QuotaRetry } from './types.js';

/** A scheduled slot is retried once after a subscription limit; a second limit fails it. */
export const MAX_QUOTA_RETRIES = 1;
/** Retry this long after the announced reset, so the new window has opened. */
export const QUOTA_RETRY_MARGIN_MS = 2 * 60_000;
/** A reset further away than this is not waited for; the slot stays failed. */
export const MAX_QUOTA_WAIT_MS = 48 * 3_600_000;
/** Distinct slots of one job kept waiting at once; a slot beyond them is not kept waiting and fails. */
export const MAX_PENDING_QUOTA_RETRIES = 8;

const LIMIT = /hit your (?:session|weekly|usage) limit[^\n]*?\bresets\s+(?:([A-Za-z]{3,9})\s+(\d{1,2}),?\s+(?:at\s+)?)?(\d{1,2})(?::(\d{2}))?\s*(am|pm)\s*\(([^)]+)\)/i;
const MONTHS = ['jan', 'feb', 'mar', 'apr', 'may', 'jun', 'jul', 'aug', 'sep', 'oct', 'nov', 'dec'];

function localParts(at: Date, zone: string): { month: number; day: number; hour: number; minute: number } {
  const parts = Object.fromEntries(new Intl.DateTimeFormat('en-US', {
    timeZone: zone, month: 'numeric', day: 'numeric', hour: 'numeric', minute: 'numeric', hourCycle: 'h23',
  }).formatToParts(at).map(part => [part.type, part.value]));
  return { month: Number(parts.month), day: Number(parts.day), hour: Number(parts.hour), minute: Number(parts.minute) };
}

/**
 * The instant a Claude subscription limit announced in `text` resets, e.g.
 * "You've hit your session limit · resets 5:20pm (Asia/Taipei)". Undefined when
 * the text carries no such limit, the zone is unknown, or the reset is not
 * within MAX_QUOTA_WAIT_MS. Callers pass every output stream the runtime wrote.
 */
export function subscriptionResetAt(text: string, now: Date): Date | undefined {
  const match = LIMIT.exec(text);
  if (!match) return undefined;
  const [, monthName, dayText, hourText, minuteText, meridiem, zone] = match;
  const hour = (Number(hourText) % 12) + (meridiem!.toLowerCase() === 'pm' ? 12 : 0);
  const minute = Number(minuteText ?? 0);
  const month = monthName ? MONTHS.indexOf(monthName.slice(0, 3).toLowerCase()) + 1 : undefined;
  if (hour > 23 || minute > 59 || month === 0) return undefined;
  try {
    localParts(now, zone!);
  } catch {
    return undefined;
  }
  // The notice is exact to the minute: a reset in the current minute is today's.
  const start = Math.floor(now.getTime() / 60_000) * 60_000;
  for (let at = start; at <= now.getTime() + MAX_QUOTA_WAIT_MS; at += 60_000) {
    const local = localParts(new Date(at), zone!);
    if (local.hour === hour && local.minute === minute
        && (month === undefined || (local.month === month && local.day === Number(dayText)))) {
      return new Date(at);
    }
  }
  return undefined;
}

/** The finished run a retry decision is about. */
export interface FinishedSlot {
  /** ISO slot of the run; manual runs have none and never schedule a retry. */
  scheduledFor?: string;
  /** Definition the run executed. */
  definitionHash?: string;
  failed: boolean;
  /** ISO reset instant recognised from the run's output streams. */
  quotaResetAt?: string;
}

/**
 * The retries a finished run leaves behind, one per slot of the current
 * definition. A slot that failed only because the subscription limit was
 * reached is retried once the limit resets; any other finish of that slot
 * clears it, other slots keep their own, and a run of an older definition
 * never schedules one for the current definition.
 */
export function nextQuotaRetries(prior: QuotaRetry[] | undefined, run: FinishedSlot,
  currentDefinition: string | undefined): QuotaRetry[] | undefined {
  // A retry beyond MAX_QUOTA_RETRIES (kept by an older Relay that allowed more) is dropped, never run.
  const current = (prior ?? []).filter(item => item.definitionHash === currentDefinition && item.attempts <= MAX_QUOTA_RETRIES);
  const kept = current.filter(item => item.scheduledFor !== run.scheduledFor);
  const previous = current.find(item => item.scheduledFor === run.scheduledFor);
  if (run.scheduledFor && run.failed && run.quotaResetAt && run.definitionHash === currentDefinition) {
    const attempts = (previous?.attempts ?? 0) + 1;
    // A slot that finds every waiting place taken is not kept waiting: it fails
    // now, so no slot already waiting is dropped without being settled.
    if (attempts <= MAX_QUOTA_RETRIES && kept.length < MAX_PENDING_QUOTA_RETRIES) {
      kept.push({
        scheduledFor: run.scheduledFor, definitionHash: run.definitionHash,
        notBefore: new Date(Date.parse(run.quotaResetAt) + QUOTA_RETRY_MARGIN_MS).toISOString(), attempts,
      });
    }
  }
  kept.sort((a, b) => Date.parse(a.scheduledFor) - Date.parse(b.scheduledFor));
  return kept.length ? kept : undefined;
}

/** The retry a finished run leaves waiting for its own slot, if any. */
export function pendingQuotaRetry(prior: QuotaRetry[] | undefined, run: FinishedSlot,
  currentDefinition: string | undefined): QuotaRetry | undefined {
  if (!run.scheduledFor || !run.failed) return undefined;
  return nextQuotaRetries(prior, run, currentDefinition)?.find(item => item.scheduledFor === run.scheduledFor);
}

/**
 * The earliest slot to run again now: its limit has reset, it belongs to the
 * current definition, and it is not older than that definition's activation.
 */
export function dueQuotaRetry(retries: QuotaRetry[] | undefined,
  state: { definitionHash?: string; activatedAt?: string }, now: Date): Date | undefined {
  const floor = state.activatedAt ? Date.parse(state.activatedAt) : -Infinity;
  const due = (retries ?? []).find(item => item.definitionHash === state.definitionHash
    && item.attempts <= MAX_QUOTA_RETRIES && Date.parse(item.scheduledFor) >= floor && Date.parse(item.notBefore) <= now.getTime());
  return due ? new Date(due.scheduledFor) : undefined;
}

/**
 * The earliest subscription reset announced by any of a run's failed attempts,
 * each read against the moment that attempt ended (the notice names a local
 * time of day, which a later reading would push to the next day).
 */
export function earliestResetAt(attempts: Array<{ output: string; endedAt: Date }>): Date | undefined {
  const resets = attempts.map(({ output, endedAt }) => subscriptionResetAt(output, endedAt))
    .filter((at): at is Date => at !== undefined);
  return resets.length ? new Date(Math.min(...resets.map((at) => at.getTime()))) : undefined;
}
