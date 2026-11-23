import { describe, expect, it } from 'vitest';
import { dueQuotaRetry, earliestResetAt, MAX_PENDING_QUOTA_RETRIES, MAX_QUOTA_RETRIES, nextQuotaRetries, pendingQuotaRetry, subscriptionResetAt } from '../src/quota-retry.js';

const SESSION = "You've hit your session limit · resets 5:20pm (Asia/Taipei)";
const SLOT = '2026-11-15T09:00:00.000Z';
const LATER = '2026-11-15T09:10:00.000Z';
const AT = new Date('2026-11-15T09:00:06Z'); // 17:00:06 in Taipei
const DEF = 'a'.repeat(64);
const RESET = '2026-11-15T09:20:00.000Z';

function quotaFailure(scheduledFor = SLOT, definitionHash = DEF, quotaResetAt: string | undefined = RESET) {
  return { scheduledFor, definitionHash, failed: true, quotaResetAt };
}

describe('subscriptionResetAt', () => {
  it('reads the announced local reset time in its zone', () => {
    expect(subscriptionResetAt(SESSION, AT)?.toISOString()).toBe(RESET);
    expect(subscriptionResetAt(`ok\n${"You've hit your session limit · resets 5:50pm (Asia/Taipei)"}`, AT)?.toISOString())
      .toBe('2026-11-15T09:50:00.000Z');
  });

  it('rolls a time already past today to the next day and honours an explicit date', () => {
    expect(subscriptionResetAt("You've hit your weekly limit · resets 2pm (Asia/Taipei)", AT)?.toISOString())
      .toBe('2026-11-16T06:00:00.000Z');
    expect(subscriptionResetAt("You've hit your weekly limit · resets Nov 16, 2pm (Asia/Taipei)", AT)?.toISOString())
      .toBe('2026-11-16T06:00:00.000Z');
  });

  it('accepts a reset that falls in the current minute', () => {
    const inMinute = new Date('2026-11-15T09:20:01Z');
    expect(subscriptionResetAt(SESSION, inMinute)?.toISOString()).toBe(RESET);
    expect(subscriptionResetAt("You've hit your weekly limit · resets Nov 15, 5:20pm (Asia/Taipei)", inMinute)?.toISOString())
      .toBe(RESET);
  });

  it('ignores other failures, unknown zones and resets beyond the wait limit', () => {
    expect(subscriptionResetAt('cc exited with code 1', AT)).toBeUndefined();
    expect(subscriptionResetAt("You've hit your session limit · resets 5:20pm (Mars/Olympus)", AT)).toBeUndefined();
    expect(subscriptionResetAt("You've hit your weekly limit · resets Nov 30, 2pm (Asia/Taipei)", AT)).toBeUndefined();
  });
});

describe('nextQuotaRetries', () => {
  it('schedules the failed slot after the reset and caps retries per slot', () => {
    let retries = nextQuotaRetries(undefined, quotaFailure(), DEF);
    expect(retries).toEqual([{ scheduledFor: SLOT, definitionHash: DEF, notBefore: '2026-11-15T09:22:00.000Z', attempts: 1 }]);
    for (let n = 2; n <= MAX_QUOTA_RETRIES; n += 1) retries = nextQuotaRetries(retries, quotaFailure(), DEF);
    expect(retries?.[0]?.attempts).toBe(MAX_QUOTA_RETRIES);
    expect(nextQuotaRetries(retries, quotaFailure(), DEF)).toBeUndefined();
  });

  it('retries a slot once: its first limit waits, a second fails it', () => {
    expect(MAX_QUOTA_RETRIES).toBe(1);
    const waiting = pendingQuotaRetry(undefined, quotaFailure(), DEF);
    expect(waiting).toEqual({ scheduledFor: SLOT, definitionHash: DEF, notBefore: '2026-11-15T09:22:00.000Z', attempts: 1 });
    expect(pendingQuotaRetry([waiting!], quotaFailure(), DEF)).toBeUndefined();
    expect(pendingQuotaRetry(undefined, { scheduledFor: SLOT, definitionHash: DEF, failed: true }, DEF)).toBeUndefined();
    expect(pendingQuotaRetry(undefined, { definitionHash: DEF, failed: true, quotaResetAt: RESET }, DEF)).toBeUndefined();
  });

  it('keeps distinct slots apart and clears a slot on any other finish', () => {
    const both = nextQuotaRetries(nextQuotaRetries(undefined, quotaFailure(), DEF), quotaFailure(LATER), DEF);
    expect(both?.map(item => item.scheduledFor)).toEqual([SLOT, LATER]);
    expect(nextQuotaRetries(both, { scheduledFor: SLOT, definitionHash: DEF, failed: false }, DEF)?.map(i => i.scheduledFor)).toEqual([LATER]);
    expect(nextQuotaRetries(both, { scheduledFor: SLOT, definitionHash: DEF, failed: true }, DEF)?.map(i => i.scheduledFor)).toEqual([LATER]);
    expect(nextQuotaRetries(both, { definitionHash: DEF, failed: true, quotaResetAt: RESET }, DEF)).toEqual(both);
  });

  it('fails a slot that finds every waiting place taken, keeping the slots already waiting', () => {
    const slots = Array.from({ length: MAX_PENDING_QUOTA_RETRIES + 1 },
      (_, i) => new Date(Date.parse(SLOT) + i * 600_000).toISOString());
    let retries: ReturnType<typeof nextQuotaRetries>;
    for (const slot of slots.slice(0, -1)) retries = nextQuotaRetries(retries, quotaFailure(slot), DEF);
    expect(retries?.map(item => item.scheduledFor)).toEqual(slots.slice(0, -1));
    const ninth = quotaFailure(slots.at(-1)!);
    expect(nextQuotaRetries(retries, ninth, DEF)).toEqual(retries);
    expect(pendingQuotaRetry(retries, ninth, DEF)).toBeUndefined();
  });

  it('drops a retry an older Relay kept beyond the retry limit', () => {
    const legacy = [{ scheduledFor: SLOT, definitionHash: DEF, notBefore: '2026-11-15T09:22:00.000Z', attempts: 2 }];
    expect(dueQuotaRetry(legacy, { definitionHash: DEF }, new Date('2026-11-15T09:30:00Z'))).toBeUndefined();
    expect(nextQuotaRetries(legacy, quotaFailure(LATER), DEF)?.map(item => item.scheduledFor)).toEqual([LATER]);
    expect(pendingQuotaRetry(legacy, quotaFailure(), DEF)?.attempts).toBe(1);
  });

  it('binds retries to the current definition', () => {
    const retries = nextQuotaRetries(undefined, quotaFailure(), DEF);
    // A reload replaced the definition: the old slot's retry is dropped.
    expect(nextQuotaRetries(retries, quotaFailure(LATER, 'b'.repeat(64)), 'c'.repeat(64))).toBeUndefined();
    // A run of the older definition finishing after the reload adds nothing.
    expect(nextQuotaRetries(undefined, quotaFailure(SLOT, DEF), 'c'.repeat(64))).toBeUndefined();
  });
});

describe('dueQuotaRetry', () => {
  const retries = nextQuotaRetries(undefined, quotaFailure(), DEF);

  it('returns the original slot only once the reset has passed', () => {
    const state = { definitionHash: DEF, activatedAt: '2026-11-14T00:00:00.000Z' };
    expect(dueQuotaRetry(retries, state, new Date('2026-11-15T09:21:59Z'))).toBeUndefined();
    expect(dueQuotaRetry(retries, state, new Date('2026-11-15T09:22:00Z'))?.toISOString()).toBe(SLOT);
    expect(dueQuotaRetry(undefined, state, AT)).toBeUndefined();
  });

  it('never runs a slot under another definition or before the definition activated', () => {
    const late = new Date('2026-11-15T09:30:00Z');
    expect(dueQuotaRetry(retries, { definitionHash: 'c'.repeat(64) }, late)).toBeUndefined();
    expect(dueQuotaRetry(retries, { definitionHash: DEF, activatedAt: '2026-11-15T09:23:00.000Z' }, late)).toBeUndefined();
  });
});

describe('earliestResetAt', () => {
  it('takes the earliest reset any failed attempt announced', () => {
    const codex = "You've hit your usage limit · resets 6:00pm (Asia/Taipei)";
    expect(earliestResetAt([{ output: 'The usage limit has been reached', endedAt: AT }, { output: SESSION, endedAt: AT }])?.toISOString())
      .toBe(RESET);
    expect(earliestResetAt([{ output: codex, endedAt: AT }, { output: SESSION, endedAt: AT }])?.toISOString()).toBe(RESET);
    expect(earliestResetAt([{ output: 'cc exited with code 1', endedAt: AT }])).toBeUndefined();
  });

  it('reads each notice against the moment its attempt ended, not when the run finished', () => {
    // CC announced 17:20 at 17:00; the fallback only gave up at 17:30.
    expect(earliestResetAt([{ output: SESSION, endedAt: AT },
      { output: 'stream disconnected before completion', endedAt: new Date('2026-11-15T09:30:00Z') }])?.toISOString())
      .toBe(RESET);
  });
});
