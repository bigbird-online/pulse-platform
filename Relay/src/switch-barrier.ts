/**
 * Managed switch barrier: the durable record of one switch transaction.
 *
 * Unlike the short deployment hold, this record lives on disk, outlives the
 * Relay process and never opens by itself. A lease that runs out moves the
 * record to `expired_unreleased`, which still blocks; only the holder (renew
 * or release, proving the token it chose) or an explicit operator break ends
 * it. A record that cannot be read is `invalid` and blocks as well.
 *
 * Every change is written (temporary file, fsync, rename, directory fsync)
 * before memory changes; a failed write rereads the disk so memory never
 * claims a state the next process would not find. A write that fails after
 * the record reached the disk (directory fsync) counts as done.
 */
import { createHash } from 'node:crypto';
import { closeSync, chmodSync, fsyncSync, mkdirSync, openSync, readFileSync, renameSync, unlinkSync, writeSync } from 'node:fs';
import { dirname, join } from 'node:path';

export const SWITCH_BARRIER_CAPABILITY = { name: 'relay.switch-barrier', version: 1 } as const;
export const SWITCH_BARRIER_SCHEMA = 'pulse.relay.switch-barrier/v1';
export const SWITCH_LEASE_MIN_MS = 5_000;
export const SWITCH_LEASE_MAX_MS = 60 * 60 * 1000;

export type SwitchState = 'absent' | 'held' | 'expired_unreleased' | 'released' | 'invalid';

export interface SwitchRecord {
  schema: typeof SWITCH_BARRIER_SCHEMA;
  state: 'held' | 'expired_unreleased' | 'released';
  holder: string;
  transaction: string;
  tokenSha256: string;
  acquiredAt: number;
  renewedAt: number;
  leaseMs: number;
  leaseExpiresAt: number;
  expiredAt?: number;
  reason?: string;
  lastExpiry?: { expiredAt: number; recoveredAt: number };
  releasedAt?: number;
  releasedBy?: 'holder' | 'operator';
  operator?: string;
  updatedAt: number;
}

export interface SwitchStatus {
  state: SwitchState;
  blocking: boolean;
  holder?: string;
  transaction?: string | null;
  acquiredAt?: number;
  renewedAt?: number;
  leaseMs?: number;
  leaseExpiresAt?: number;
  expiredAt?: number;
  lastExpiry?: { expiredAt: number; recoveredAt: number };
  releasedAt?: number;
  releasedBy?: 'holder' | 'operator';
  operator?: string;
  reason?: string;
  invalidReason?: string;
  /** What this process found when it started; proves the barrier was read. */
  readAtStartup: { state: SwitchState; at: number; pid: number };
}

export type SwitchError =
  | 'holder_invalid' | 'transaction_invalid' | 'token_invalid' | 'leaseMs_invalid'
  | 'operator_invalid' | 'reason_invalid'
  | 'held_by_another' | 'lease_expired' | 'not_holder' | 'not_held' | 'record_invalid'
  | 'transaction_mismatch' | 'closed' | 'persist_failed'
  /** Held, but a waiting turn held only in memory could not be written (see maintenance-barrier.ts). */
  | 'pending_unsecured';

export type SwitchResult =
  | { ok: true; status: SwitchStatus; recovered?: boolean; alreadyReleased?: boolean }
  | { ok: false; error: SwitchError; status: SwitchStatus };

export interface SwitchClaim { holder: unknown; transaction: unknown; token: unknown }

const ID = /^[A-Za-z0-9._:@/-]{1,128}$/;
const TOKEN = /^[A-Za-z0-9._~-]{32,256}$/;

const sha256 = (value: string): string => createHash('sha256').update(value).digest('hex');

export function switchErrorStatus(error: SwitchError): number {
  if (error === 'persist_failed') return 500;
  if (error.endsWith('_invalid') && error !== 'record_invalid') return 400;
  return 409;
}

export interface SwitchBarrierStore {
  status(): SwitchStatus;
  blocking(): boolean;
  acquire(claim: SwitchClaim & { leaseMs: unknown }): SwitchResult;
  renew(claim: SwitchClaim & { leaseMs: unknown }): SwitchResult;
  release(claim: SwitchClaim): SwitchResult;
  breakBarrier(request: { transaction: unknown; operator: unknown; reason: unknown }): SwitchResult;
  /** Shutdown: no new acquire; renew, release and break still work. */
  close(): void;
  /** Called after any change that may have opened the barrier. */
  onChange(listener: () => void): void;
  dispose(): void;
}

type Loaded = { record?: SwitchRecord; invalid?: string };

function parse(raw: string): Loaded {
  let value: unknown;
  try { value = JSON.parse(raw); } catch { return { invalid: 'unparseable_json' }; }
  const r = value as Partial<SwitchRecord> | null;
  if (!r || typeof r !== 'object' || r.schema !== SWITCH_BARRIER_SCHEMA) return { invalid: 'unknown_schema' };
  if (!['held', 'expired_unreleased', 'released'].includes(String(r.state))
      || typeof r.holder !== 'string' || typeof r.transaction !== 'string'
      || typeof r.tokenSha256 !== 'string' || !/^[0-9a-f]{64}$/.test(r.tokenSha256)
      || ![r.acquiredAt, r.renewedAt, r.leaseMs, r.leaseExpiresAt, r.updatedAt].every(n => Number.isFinite(n))) {
    return { invalid: 'malformed_record' };
  }
  return { record: r as SwitchRecord };
}

function load(path: string): Loaded {
  let raw: string;
  try { raw = readFileSync(path, 'utf8'); } catch (error) {
    if ((error as NodeJS.ErrnoException).code === 'ENOENT') return {};
    return { invalid: `unreadable:${(error as NodeJS.ErrnoException).code ?? 'error'}` };
  }
  return parse(raw);
}

function writeDurably(path: string, record: SwitchRecord): void {
  const dir = dirname(path);
  mkdirSync(dir, { recursive: true, mode: 0o700 });
  chmodSync(dir, 0o700);
  const tmp = `${path}.${process.pid}.tmp`;
  const fd = openSync(tmp, 'w', 0o600);
  try {
    writeSync(fd, `${JSON.stringify(record, null, 2)}\n`);
    fsyncSync(fd);
  } finally { closeSync(fd); }
  try {
    chmodSync(tmp, 0o600);
    renameSync(tmp, path);
  } catch (error) {
    try { unlinkSync(tmp); } catch { /* already gone */ }
    throw error;
  }
  const dirFd = openSync(dir, 'r');
  try { fsyncSync(dirFd); } finally { closeSync(dirFd); }
}

export function createSwitchBarrierStore(path: string, options: {
  now?: () => number;
  /** Test seam: the durable write. */
  write?: (path: string, record: SwitchRecord) => void;
} = {}): SwitchBarrierStore {
  const now = options.now ?? Date.now;
  const write = options.write ?? writeDurably;
  let loaded = load(path);
  let closed = false;
  let timer: ReturnType<typeof setTimeout> | undefined;
  const listeners: Array<() => void> = [];

  function currentState(): SwitchState {
    if (loaded.invalid) return 'invalid';
    return loaded.record?.state ?? 'absent';
  }

  const readAtStartup = { state: 'absent' as SwitchState, at: now(), pid: process.pid };

  function persist(next: SwitchRecord): boolean {
    try {
      write(path, next);
      loaded = { record: next };
      return true;
    } catch (error) {
      loaded = load(path);
      if (loaded.record && JSON.stringify(loaded.record) === JSON.stringify(next)) {
        console.warn({ ts: new Date().toISOString(), level: 'warn', module: 'switch-barrier',
          msg: 'switch barrier write reported an error after the record reached the disk; taken as written', error: String(error) });
        return true;
      }
      console.error({ ts: new Date().toISOString(), level: 'error', module: 'switch-barrier',
        msg: 'switch barrier write failed; state kept as on disk', error: String(error) });
      return false;
    } finally {
      schedule();
    }
  }

  /** A held record past its lease becomes expired_unreleased (still blocking). */
  function expireIfDue(): void {
    const record = loaded.record;
    if (!record || record.state !== 'held' || now() < record.leaseExpiresAt) return;
    const at = now();
    const next: SwitchRecord = { ...record, state: 'expired_unreleased', expiredAt: record.leaseExpiresAt,
      reason: 'lease_expired', updatedAt: at };
    if (!persist(next)) {
      // Memory follows the disk; the reported state is computed from the lease.
      return;
    }
    console.warn({ ts: new Date().toISOString(), level: 'warn', module: 'switch-barrier',
      msg: 'switch barrier lease expired; still blocking until holder or operator acts',
      holder: record.holder, transaction: record.transaction });
  }

  function schedule(): void {
    if (timer) clearTimeout(timer);
    timer = undefined;
    const record = loaded.record;
    if (!record || record.state !== 'held') return;
    timer = setTimeout(expireIfDue, Math.max(0, record.leaseExpiresAt - now()) + 1);
    timer.unref?.();
  }

  function status(): SwitchStatus {
    expireIfDue();
    const state = currentState();
    const r = loaded.record;
    if (state === 'invalid') {
      return { state, blocking: true, transaction: null, invalidReason: loaded.invalid, readAtStartup };
    }
    if (!r) return { state, blocking: false, readAtStartup };
    // A held record whose write of the expiry failed still reports the expiry.
    const effective: SwitchState = r.state === 'held' && now() >= r.leaseExpiresAt ? 'expired_unreleased' : r.state;
    return {
      state: effective,
      blocking: effective !== 'released',
      holder: r.holder,
      transaction: r.transaction,
      acquiredAt: r.acquiredAt,
      renewedAt: r.renewedAt,
      leaseMs: r.leaseMs,
      leaseExpiresAt: r.leaseExpiresAt,
      ...(r.expiredAt !== undefined ? { expiredAt: r.expiredAt } : {}),
      ...(r.lastExpiry ? { lastExpiry: r.lastExpiry } : {}),
      ...(r.releasedAt !== undefined ? { releasedAt: r.releasedAt } : {}),
      ...(r.releasedBy ? { releasedBy: r.releasedBy } : {}),
      ...(r.operator ? { operator: r.operator } : {}),
      ...(effective === 'expired_unreleased' && !r.reason ? { reason: 'lease_expired' } : r.reason ? { reason: r.reason } : {}),
      readAtStartup,
    };
  }

  readAtStartup.state = status().state;
  schedule();

  function fail(error: SwitchError): SwitchResult {
    return { ok: false, error, status: status() };
  }

  function checkClaim(claim: SwitchClaim): SwitchError | { holder: string; transaction: string; tokenSha256: string } {
    if (typeof claim.holder !== 'string' || !ID.test(claim.holder)) return 'holder_invalid';
    if (typeof claim.transaction !== 'string' || !ID.test(claim.transaction)) return 'transaction_invalid';
    if (typeof claim.token !== 'string' || !TOKEN.test(claim.token)) return 'token_invalid';
    return { holder: claim.holder, transaction: claim.transaction, tokenSha256: sha256(claim.token) };
  }

  function checkLease(leaseMs: unknown): number | undefined {
    return typeof leaseMs === 'number' && Number.isInteger(leaseMs)
      && leaseMs >= SWITCH_LEASE_MIN_MS && leaseMs <= SWITCH_LEASE_MAX_MS ? leaseMs : undefined;
  }

  const matches = (r: SwitchRecord, c: { holder: string; transaction: string; tokenSha256: string }): boolean =>
    r.holder === c.holder && r.transaction === c.transaction && r.tokenSha256 === c.tokenSha256;

  function changed(): void {
    for (const listener of listeners) {
      try { listener(); } catch (error) {
        console.error({ ts: new Date().toISOString(), level: 'error', module: 'switch-barrier', msg: 'listener failed', error: String(error) });
      }
    }
  }

  function renewRecord(r: SwitchRecord, leaseMs: number): SwitchResult {
    const at = now();
    const wasExpired = status().state === 'expired_unreleased';
    const next: SwitchRecord = {
      ...r, state: 'held', renewedAt: at, leaseMs, leaseExpiresAt: at + leaseMs, updatedAt: at,
      ...(wasExpired ? { lastExpiry: { expiredAt: r.expiredAt ?? r.leaseExpiresAt, recoveredAt: at } } : {}),
    };
    delete next.expiredAt;
    delete next.reason;
    if (!persist(next)) return fail('persist_failed');
    return { ok: true, status: status(), ...(wasExpired ? { recovered: true } : {}) };
  }

  return {
    status,
    blocking: () => status().blocking,
    acquire(request) {
      const claim = checkClaim(request);
      if (typeof claim === 'string') return fail(claim);
      const leaseMs = checkLease(request.leaseMs);
      if (leaseMs === undefined) return fail('leaseMs_invalid');
      const state = status().state;
      if (state === 'invalid') return fail('record_invalid');
      const r = loaded.record;
      if (r && (state === 'held' || state === 'expired_unreleased')) {
        if (!matches(r, claim)) return fail('held_by_another');
        if (state === 'expired_unreleased') return fail('lease_expired');
        // Same holder, transaction and token: a retried acquire renews.
        return renewRecord(r, leaseMs);
      }
      if (closed) return fail('closed');
      const at = now();
      const next: SwitchRecord = { schema: SWITCH_BARRIER_SCHEMA, state: 'held', ...claim,
        acquiredAt: at, renewedAt: at, leaseMs, leaseExpiresAt: at + leaseMs, updatedAt: at };
      if (!persist(next)) return fail('persist_failed');
      console.log({ ts: new Date().toISOString(), level: 'info', module: 'switch-barrier', msg: 'switch barrier acquired',
        holder: claim.holder, transaction: claim.transaction, leaseMs });
      changed();
      return { ok: true, status: status() };
    },
    renew(request) {
      const claim = checkClaim(request);
      if (typeof claim === 'string') return fail(claim);
      const leaseMs = checkLease(request.leaseMs);
      if (leaseMs === undefined) return fail('leaseMs_invalid');
      const state = status().state;
      if (state === 'invalid') return fail('record_invalid');
      const r = loaded.record;
      if (!r || (state !== 'held' && state !== 'expired_unreleased')) return fail('not_held');
      if (!matches(r, claim)) return fail('not_holder');
      return renewRecord(r, leaseMs);
    },
    release(request) {
      const claim = checkClaim(request);
      if (typeof claim === 'string') return fail(claim);
      const state = status().state;
      if (state === 'invalid') return fail('record_invalid');
      const r = loaded.record;
      if (!r) return fail('not_held');
      if (state === 'released') {
        if (!matches(r, claim) || r.releasedBy !== 'holder') return fail('not_held');
        // A retry may follow a release whose notice never went out: wake waiters again.
        changed();
        return { ok: true, status: status(), alreadyReleased: true };
      }
      if (!matches(r, claim)) return fail('not_holder');
      const at = now();
      const next: SwitchRecord = { ...r, state: 'released', releasedAt: at, releasedBy: 'holder', updatedAt: at };
      if (!persist(next)) return fail('persist_failed');
      console.log({ ts: new Date().toISOString(), level: 'info', module: 'switch-barrier', msg: 'switch barrier released by holder',
        holder: r.holder, transaction: r.transaction });
      changed();
      return { ok: true, status: status() };
    },
    breakBarrier(request) {
      const operator = typeof request.operator === 'string' ? request.operator.trim() : '';
      const reason = typeof request.reason === 'string' ? request.reason.trim() : '';
      if (!operator || operator.length > 200) return fail('operator_invalid');
      if (!reason || reason.length > 200) return fail('reason_invalid');
      const current = status();
      if (!current.blocking) return fail('not_held');
      if (request.transaction !== current.transaction) return fail('transaction_mismatch');
      const at = now();
      const r = loaded.record;
      const next: SwitchRecord = r
        ? { ...r, state: 'released', releasedAt: at, releasedBy: 'operator', operator, reason, updatedAt: at }
        // An invalid record is replaced by a released one naming the operator.
        : { schema: SWITCH_BARRIER_SCHEMA, state: 'released', holder: 'operator', transaction: 'invalid-record',
          tokenSha256: sha256(`invalid-record:${at}`), acquiredAt: at, renewedAt: at, leaseMs: 0, leaseExpiresAt: at,
          releasedAt: at, releasedBy: 'operator', operator, reason, updatedAt: at };
      if (!persist(next)) return fail('persist_failed');
      console.warn({ ts: new Date().toISOString(), level: 'warn', module: 'switch-barrier', msg: 'switch barrier broken by operator',
        operator, reason, transaction: current.transaction });
      changed();
      return { ok: true, status: status() };
    },
    close() { closed = true; },
    onChange(listener) { listeners.push(listener); },
    dispose() { if (timer) clearTimeout(timer); timer = undefined; },
  };
}

/** Where a Relay keeps its switch barrier record. */
export function switchBarrierPath(dataDir: string): string {
  return join(dataDir, 'maintenance', 'switch-barrier.json');
}
