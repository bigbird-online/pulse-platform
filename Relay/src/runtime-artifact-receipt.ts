import { relayChildEnv } from './relay-secrets.js';
import { createHash, randomUUID } from 'node:crypto';
import {
  closeSync,
  existsSync,
  openSync,
  readFileSync,
  renameSync,
  statSync,
  unlinkSync,
  writeFileSync,
} from 'node:fs';
import { dirname, isAbsolute, join } from 'node:path';
import { spawnSync } from 'node:child_process';

const ATTEST_BIN = 'RELAY_CRON_ARTIFACT_ATTEST_BIN';
const ENROLLMENT_ID = 'RELAY_CRON_ARTIFACT_ENROLLMENT_ID';
const PRODUCER_ID = 'RELAY_CRON_ARTIFACT_PRODUCER_ID';
const ENROLLMENT_RE = /^runtime_artifact_v1_[A-Za-z0-9_.-]{1,160}$/;
const TOKEN_RE = /^[a-z0-9][a-z0-9_.-]{0,159}$/;

interface AttestationConfig {
  bin: string;
  enrollmentId: string;
  producerId: string;
}

interface WriteLock {
  path: string;
  token: string;
}

export interface RuntimeArtifactCommandResult {
  status: number | null;
  stdout: string;
  stderr: string;
  signal?: NodeJS.Signals | null;
  error?: Error;
}

export type RuntimeArtifactCommandRunner = (
  bin: string,
  args: string[],
) => RuntimeArtifactCommandResult;

export interface RuntimeArtifactWriteOptions {
  /** Reject a changed preimage under the writer lock; an exact postimage retry remains a no-op. */
  expectedPreimageSha256?: string;
  env?: NodeJS.ProcessEnv;
  now?: () => number;
  runCommand?: RuntimeArtifactCommandRunner;
}

export interface RuntimeArtifactWriteResult {
  changed: boolean;
  receiptId: string | null;
}

function sha256(value: Buffer | string): string {
  return createHash('sha256').update(value).digest('hex');
}

function resolveAttestationConfig(env: NodeJS.ProcessEnv): AttestationConfig | null {
  const bin = env[ATTEST_BIN]?.trim() ?? '';
  const enrollmentId = env[ENROLLMENT_ID]?.trim() ?? '';
  const producerId = env[PRODUCER_ID]?.trim() ?? '';
  if (!bin && !enrollmentId && !producerId) return null;
  if (!bin || !enrollmentId || !producerId) {
    throw new Error('Relay cron runtime-artifact attestor is only partially configured');
  }
  if (!isAbsolute(bin)) {
    throw new Error('Relay cron runtime-artifact attestor binary must be an absolute path');
  }
  if (!ENROLLMENT_RE.test(enrollmentId) || !TOKEN_RE.test(producerId)) {
    throw new Error('Relay cron runtime-artifact identity is invalid');
  }
  return { bin, enrollmentId, producerId };
}

function defaultRunCommand(bin: string, args: string[]): RuntimeArtifactCommandResult {
  const result = spawnSync(bin, args, {
    encoding: 'utf8',
    env: relayChildEnv(),
    shell: false,
    timeout: 120_000,
  });
  return {
    status: result.status,
    stdout: result.stdout ?? '',
    stderr: result.stderr ?? '',
    signal: result.signal,
    error: result.error,
  };
}

function atomicWrite(path: string, payload: Buffer, mode: number): void {
  const temporary = join(dirname(path), `.${randomUUID()}.runtime-artifact.tmp`);
  try {
    writeFileSync(temporary, payload, { flag: 'wx', mode });
    renameSync(temporary, path);
  } finally {
    if (existsSync(temporary)) unlinkSync(temporary);
  }
}

function processIsAlive(pid: number): boolean {
  try {
    process.kill(pid, 0);
    return true;
  } catch (error) {
    return (error as NodeJS.ErrnoException).code !== 'ESRCH';
  }
}

function removeStaleLock(lockPath: string): boolean {
  try {
    const owner = JSON.parse(readFileSync(lockPath, 'utf8')) as { pid?: unknown; token?: unknown };
    if (!Number.isSafeInteger(owner.pid) || (owner.pid as number) <= 0 || typeof owner.token !== 'string') {
      return false;
    }
    if (processIsAlive(owner.pid as number)) return false;

    const current = JSON.parse(readFileSync(lockPath, 'utf8')) as { pid?: unknown; token?: unknown };
    if (current.pid !== owner.pid || current.token !== owner.token) return false;
    unlinkSync(lockPath);
    return true;
  } catch {
    return false;
  }
}

function acquireWriteLock(path: string): WriteLock {
  const lockPath = `${path}.runtime-artifact.lock`;
  for (let attempt = 0; attempt < 2; attempt += 1) {
    const token = randomUUID();
    let descriptor: number | null = null;
    try {
      descriptor = openSync(lockPath, 'wx', 0o600);
      writeFileSync(descriptor, `${JSON.stringify({ pid: process.pid, token })}\n`);
      closeSync(descriptor);
      descriptor = null;
      return { path: lockPath, token };
    } catch (error) {
      if (descriptor !== null) {
        try {
          closeSync(descriptor);
        } catch {
          // Preserve the authoritative acquisition failure below.
        }
        try {
          unlinkSync(lockPath);
        } catch {
          // Preserve the authoritative acquisition failure below.
        }
      }
      const code = (error as NodeJS.ErrnoException).code;
      if (code === 'EEXIST' && attempt === 0 && removeStaleLock(lockPath)) continue;
      if (code === 'EEXIST') {
        throw new Error('Relay cron runtime-artifact write is already in progress');
      }
      throw error;
    }
  }
  throw new Error('Relay cron runtime-artifact lock could not be acquired');
}

function releaseWriteLock(lock: WriteLock): void {
  const owner = JSON.parse(readFileSync(lock.path, 'utf8')) as { token?: unknown };
  if (owner.token !== lock.token) {
    throw new Error('Relay cron runtime-artifact lock ownership changed unexpectedly');
  }
  unlinkSync(lock.path);
}

function restorePreimage(path: string, existed: boolean, payload: Buffer, mode: number): void {
  if (existed) {
    atomicWrite(path, payload, mode);
  } else if (existsSync(path)) {
    unlinkSync(path);
  }
}

function parseReceipt(stdout: string, config: AttestationConfig, postimage: Buffer): string {
  let payload: unknown;
  try {
    payload = JSON.parse(stdout);
  } catch {
    throw new Error('Runtime-artifact attestor returned malformed JSON');
  }
  const value = payload as {
    ok?: unknown;
    receipt?: {
      receipt_id?: unknown;
      enrollment_id?: unknown;
      receipt_kind?: unknown;
      producer_id?: unknown;
      attested_by?: unknown;
      postimage_size_bytes?: unknown;
      postimage_sha256?: unknown;
    };
  };
  const receipt = value.receipt;
  if (
    value.ok !== true
    || typeof receipt?.receipt_id !== 'string'
    || receipt.enrollment_id !== config.enrollmentId
    || receipt.receipt_kind !== 'producer'
    || receipt.producer_id !== config.producerId
    || receipt.attested_by !== config.producerId
    || receipt.postimage_size_bytes !== postimage.length
    || receipt.postimage_sha256 !== sha256(postimage)
  ) {
    throw new Error('Runtime-artifact attestor returned a mismatched receipt');
  }
  return receipt.receipt_id;
}

function attest(
  config: AttestationConfig,
  purpose: string,
  postimage: Buffer,
  now: () => number,
  runCommand: RuntimeArtifactCommandRunner,
): string {
  const safePurpose = purpose.toLowerCase().replace(/[^a-z0-9.-]+/g, '-').slice(0, 40) || 'write';
  const operationRef = [
    'operation_v1_relay-cron',
    safePurpose,
    now().toString(36),
    process.pid.toString(36),
    sha256(postimage).slice(0, 16),
  ].join('.');
  const args = [
    'runtime-artifact', 'attest', config.enrollmentId,
    '--receipt-kind', 'producer',
    '--producer-id', config.producerId,
    '--operation-ref', operationRef,
    '--reason', `Relay cron configuration write: ${safePurpose}`,
    '--attested-by', config.producerId,
    '--json',
  ];
  const result = runCommand(config.bin, args);
  if (result.error || result.status !== 0 || result.signal) {
    const detail = result.error?.message ?? (result.stderr.trim().slice(0, 500) || 'unknown error');
    throw new Error(`Runtime-artifact attestation failed: ${detail}`);
  }
  return parseReceipt(result.stdout, config, postimage);
}

/**
 * Persist one Relay cron configuration postimage and, when explicitly enabled,
 * bind it to the exact external Harness producer receipt. Attestation failure
 * restores the exact preimage so the runtime cannot silently outrun its proof.
 */
export function writeCronJobsConfig(
  path: string,
  document: unknown,
  purpose: string,
  options: RuntimeArtifactWriteOptions = {},
): RuntimeArtifactWriteResult {
  const env = options.env ?? process.env;
  const config = resolveAttestationConfig(env); // validate before touching the file
  const expected = options.expectedPreimageSha256;
  if (expected !== undefined && !/^[a-f0-9]{64}$/.test(expected)) {
    throw new Error('Relay cron expected preimage must be a SHA-256 digest');
  }
  const postimage = Buffer.from(`${JSON.stringify(document, null, 2)}\n`, 'utf8');
  const lock = acquireWriteLock(path);
  try {
    const existed = existsSync(path);
    const preimage = existed ? readFileSync(path) : Buffer.alloc(0);
    if (existed && preimage.equals(postimage)) return { changed: false, receiptId: null };
    if (expected !== undefined && (!existed || sha256(preimage) !== expected)) {
      throw new Error('Relay cron configuration changed after preflight');
    }
    const mode = existed ? statSync(path).mode & 0o777 : 0o600;

    atomicWrite(path, postimage, mode);
    if (!config) return { changed: true, receiptId: null };

    try {
      const receiptId = attest(
        config,
        purpose,
        postimage,
        options.now ?? Date.now,
        options.runCommand ?? defaultRunCommand,
      );
      return { changed: true, receiptId };
    } catch (error) {
      try {
        restorePreimage(path, existed, preimage, mode);
      } catch (rollbackError) {
        throw new AggregateError(
          [error, rollbackError],
          'Runtime-artifact attestation and exact preimage restoration both failed',
        );
      }
      throw error;
    }
  } finally {
    releaseWriteLock(lock);
  }
}
