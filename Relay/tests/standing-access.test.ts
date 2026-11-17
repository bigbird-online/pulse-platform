/**
 * Standing read grants: one kernel read for every Agent, kept and refreshed in
 * the background, merged into each session's access with the standing mark.
 */
import { afterEach, describe, expect, it, vi } from 'vitest';
import { accessRows } from '../src/native-work-item-command.js';
import {
  STANDING_ACCESS_MAX_AGE_MS, STANDING_ACCESS_TTL_MS, noteStandingAccess, resetStandingAccessCache, standingAccessFor, withStandingAccess,
} from '../src/standing-access.js';

const base = { path: '/c/PARA/vigil-space', mode: 'read' as const, standing: true as const };
const core = () => ({});

describe('standing access cache', () => {
  afterEach(() => { resetStandingAccessCache(); vi.restoreAllMocks(); });

  it('waits for the first read, then serves it and refreshes in the background once stale', async () => {
    let now = 1_000;
    const read = vi.fn().mockResolvedValueOnce(new Map([['vigil-33', [base]]])).mockResolvedValueOnce(new Map());
    expect(await standingAccessFor('vigil-33', { read, now: () => now, core })).toEqual([base]);
    expect(await standingAccessFor('vigil', { read, now: () => now, core })).toEqual([]);
    expect(read).toHaveBeenCalledTimes(1);
    now += STANDING_ACCESS_TTL_MS + 1;
    // A stale read still serves this start; the refresh (revocation here) applies next.
    expect(await standingAccessFor('vigil-33', { read, now: () => now, core })).toEqual([base]);
    await Promise.resolve(); await Promise.resolve();
    expect(read).toHaveBeenCalledTimes(2);
    expect(await standingAccessFor('vigil-33', { read, now: () => now, core })).toEqual([]);
  });

  it('applies nothing without an installed core or when the read is unavailable', async () => {
    vi.spyOn(console, 'warn').mockImplementation(() => {});
    const read = vi.fn().mockRejectedValue(new Error('standing_access_unavailable'));
    expect(await standingAccessFor('vigil-33', { read, core: () => undefined })).toEqual([]);
    expect(read).not.toHaveBeenCalled();
    expect(await standingAccessFor('vigil-33', { read, core })).toEqual([]);
  });

  it('stops applying an expired read once a refresh after its expiry fails', async () => {
    vi.spyOn(console, 'warn').mockImplementation(() => {});
    let now = 1_000;
    const read = vi.fn().mockResolvedValueOnce(new Map([['vigil-33', [base]]]))
      .mockRejectedValue(new Error('ledger_unavailable'));
    expect(await standingAccessFor('vigil-33', { read, now: () => now, core })).toEqual([base]);
    now += STANDING_ACCESS_TTL_MS + 1;
    await standingAccessFor('vigil-33', { read, now: () => now, core });
    await Promise.resolve(); await Promise.resolve(); await Promise.resolve();
    expect(read).toHaveBeenCalledTimes(2);
    // The kernel could not confirm the grant after it expired: it no longer applies.
    expect(await standingAccessFor('vigil-33', { read, now: () => now, core })).toEqual([]);
  });

  it('never applies a read older than the maximum age, even while a refresh hangs', async () => {
    let now = 1_000;
    const read = vi.fn().mockResolvedValueOnce(new Map([['vigil-33', [base]]])).mockReturnValue(new Promise(() => {}));
    await standingAccessFor('vigil-33', { read, now: () => now, core });
    now += STANDING_ACCESS_MAX_AGE_MS + 1;
    expect(await standingAccessFor('vigil-33', { read, now: () => now, core })).toEqual([]);
  });

  it('keeps an independent standing readback of one Agent for the next start', async () => {
    let now = 1_000;
    const read = vi.fn().mockResolvedValue(new Map([['vigil-33', [base]]]));
    await standingAccessFor('vigil-33', { read, now: () => now, core });
    now += 1;
    noteStandingAccess('vigil-33', [], () => now);
    expect(await standingAccessFor('vigil-33', { read, now: () => now, core })).toEqual([]);
    expect(await standingAccessFor('vigil', { read, now: () => now, core })).toEqual([]);
  });
});

describe('merging session and standing access', () => {
  it('replaces earlier standing rows and lets a session grant of the same path win', () => {
    const session = [{ path: '/c/shared', mode: 'write' as const }, { path: '/old', mode: 'read' as const, standing: true as const }];
    expect(withStandingAccess(session, [base, { path: '/c/shared', mode: 'read', standing: true }])).toEqual([
      { path: '/c/PARA/vigil-space', mode: 'read', standing: true },
      { path: '/c/shared', mode: 'write' },
    ]);
    expect(accessRows([{ paths: ['/a'], mode: 'read', standing: true }, { paths: ['/a'], mode: 'read' }]))
      .toEqual([{ path: '/a', mode: 'read' }]);
  });
});
