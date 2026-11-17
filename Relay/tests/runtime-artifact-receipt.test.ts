import { createHash } from 'node:crypto';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { chmodSync, existsSync, mkdtempSync, readFileSync, rmSync, statSync, writeFileSync } from 'node:fs';
import { join } from 'node:path';
import { tmpdir } from 'node:os';
import {
  writeCronJobsConfig,
  type RuntimeArtifactCommandRunner,
} from '../src/runtime-artifact-receipt.js';

const ENROLLMENT = 'runtime_artifact_v1_pulse_vultr_relay_cron_jobs';
const PRODUCER = 'relay_cron_config_writer';

function tempConfig(initial?: unknown): { dir: string; path: string } {
  const dir = mkdtempSync(join(tmpdir(), 'relay-runtime-artifact-'));
  const path = join(dir, 'cron-jobs.json');
  if (initial !== undefined) writeFileSync(path, `${JSON.stringify(initial, null, 2)}\n`);
  return { dir, path };
}

function configuredEnv(): NodeJS.ProcessEnv {
  return {
    RELAY_CRON_ARTIFACT_ATTEST_BIN: '/opt/harness/bin/harness',
    RELAY_CRON_ARTIFACT_ENROLLMENT_ID: ENROLLMENT,
    RELAY_CRON_ARTIFACT_PRODUCER_ID: PRODUCER,
  };
}

function successfulRunner(document: unknown, receiptId = `runtime_receipt_v1_${'a'.repeat(64)}`): RuntimeArtifactCommandRunner {
  const postimage = Buffer.from(`${JSON.stringify(document, null, 2)}\n`);
  return (_bin, args) => ({
    status: 0,
    stdout: JSON.stringify({
      ok: true,
      receipt: {
        receipt_id: receiptId,
        enrollment_id: ENROLLMENT,
        receipt_kind: 'producer',
        producer_id: PRODUCER,
        attested_by: PRODUCER,
        operation_ref: args[args.indexOf('--operation-ref') + 1],
        postimage_size_bytes: postimage.length,
        postimage_sha256: createHash('sha256').update(postimage).digest('hex'),
      },
    }),
    stderr: '',
  });
}

const roots: string[] = [];
afterEach(() => {
  for (const root of roots.splice(0)) rmSync(root, { recursive: true, force: true });
  vi.restoreAllMocks();
});

describe('Relay cron runtime-artifact producer receipts', () => {
  it('preserves compatibility when the attestor is entirely disabled', () => {
    const target = tempConfig();
    roots.push(target.dir);
    const document = { version: 2, jobs: [{ id: 'a', enabled: false }] };

    const result = writeCronJobsConfig(target.path, document, 'test', { env: {} });

    expect(result).toEqual({ changed: true, receiptId: null });
    expect(JSON.parse(readFileSync(target.path, 'utf8'))).toEqual(document);
  });

  it('rejects partial attestor configuration before touching the file', () => {
    const initial = { version: 2, jobs: [{ id: 'old', enabled: false }] };
    const target = tempConfig(initial);
    roots.push(target.dir);
    const before = readFileSync(target.path);

    expect(() => writeCronJobsConfig(
      target.path,
      { version: 2, jobs: [] },
      'test',
      { env: { RELAY_CRON_ARTIFACT_ATTEST_BIN: '/bin/false' } },
    )).toThrow(/partially configured/);
    expect(readFileSync(target.path)).toEqual(before);
  });

  it('writes once and invokes the exact producer attestation without a shell', () => {
    const initial = { version: 2, jobs: [{ id: 'old', enabled: false }] };
    const next = { version: 2, jobs: [{ id: 'new', enabled: false }] };
    const target = tempConfig(initial);
    roots.push(target.dir);
    const calls: Array<{ bin: string; args: string[] }> = [];
    const runCommand: RuntimeArtifactCommandRunner = (bin, args) => {
      calls.push({ bin, args });
      return successfulRunner(next)(bin, args);
    };

    const result = writeCronJobsConfig(target.path, next, 'job-store-add', {
      env: configuredEnv(),
      now: () => 1_700_000_000_000,
      runCommand,
    });

    expect(result.changed).toBe(true);
    expect(result.receiptId).toMatch(/^runtime_receipt_v1_/);
    expect(JSON.parse(readFileSync(target.path, 'utf8'))).toEqual(next);
    expect(calls).toHaveLength(1);
    expect(calls[0].bin).toBe('/opt/harness/bin/harness');
    expect(calls[0].args).toEqual(expect.arrayContaining([
      'runtime-artifact', 'attest', ENROLLMENT,
      '--receipt-kind', 'producer',
      '--producer-id', PRODUCER,
      '--attested-by', PRODUCER,
      '--json',
    ]));
    expect(calls[0].args.join(' ')).not.toContain('human-confirmed');
  });

  it('restores the exact existing preimage when attestation fails', () => {
    const initial = Buffer.from('{"version":2,"jobs":[{"id":"old","enabled":false}]}\n');
    const target = tempConfig();
    roots.push(target.dir);
    writeFileSync(target.path, initial);

    expect(() => writeCronJobsConfig(
      target.path,
      { version: 2, jobs: [{ id: 'new', enabled: false }] },
      'test',
      {
        env: configuredEnv(),
        runCommand: () => ({ status: 23, stdout: '', stderr: 'invalid payload' }),
      },
    )).toThrow(/attestation failed/);
    expect(readFileSync(target.path)).toEqual(initial);
  });

  it('removes a newly created file when its attestation fails', () => {
    const target = tempConfig();
    roots.push(target.dir);

    expect(() => writeCronJobsConfig(
      target.path,
      { version: 2, jobs: [] },
      'test',
      {
        env: configuredEnv(),
        runCommand: () => ({ status: 1, stdout: '', stderr: 'no receipt' }),
      },
    )).toThrow(/attestation failed/);
    expect(() => readFileSync(target.path)).toThrow();
  });

  it('restores the preimage for malformed or mismatched success output', () => {
    const initial = { version: 2, jobs: [{ id: 'old', enabled: false }] };
    const target = tempConfig(initial);
    roots.push(target.dir);
    const before = readFileSync(target.path);

    expect(() => writeCronJobsConfig(
      target.path,
      { version: 2, jobs: [] },
      'test',
      {
        env: configuredEnv(),
        runCommand: () => ({ status: 0, stdout: '{}', stderr: '' }),
      },
    )).toThrow(/mismatched receipt/);
    expect(readFileSync(target.path)).toEqual(before);
  });

  it('rejects a receipt for a different concurrent postimage and restores the preimage', () => {
    const initial = { version: 2, jobs: [{ id: 'old', enabled: false }] };
    const next = { version: 2, jobs: [{ id: 'new', enabled: false }] };
    const target = tempConfig(initial);
    roots.push(target.dir);
    const before = readFileSync(target.path);

    expect(() => writeCronJobsConfig(target.path, next, 'test', {
      env: configuredEnv(),
      runCommand: successfulRunner({ version: 2, jobs: [{ id: 'other' }] }),
    })).toThrow(/mismatched receipt/);
    expect(readFileSync(target.path)).toEqual(before);
  });

  it('rejects a live cross-process writer lock before touching the file', () => {
    const initial = { version: 2, jobs: [{ id: 'old', enabled: false }] };
    const target = tempConfig(initial);
    roots.push(target.dir);
    const before = readFileSync(target.path);
    writeFileSync(
      `${target.path}.runtime-artifact.lock`,
      `${JSON.stringify({ pid: process.pid, token: 'active-writer' })}\n`,
    );

    expect(() => writeCronJobsConfig(
      target.path,
      { version: 2, jobs: [] },
      'test',
      { env: configuredEnv(), runCommand: successfulRunner({ version: 2, jobs: [] }) },
    )).toThrow(/already in progress/);
    expect(readFileSync(target.path)).toEqual(before);
  });

  it('changes only 19 disabled Harness entrypoints in a 37-job preimage', () => {
    const old = '/Users/owner/Coevolution/Bigbird/Lab/BigbirdEnvironment/agent-harness/.venv/bin/harness';
    const canonical = '/home/pulse/.local/share/coevolution/tools/agent-harness/bin/harness';
    const initial = {
      version: 2,
      keep: { unrelated: ['unchanged'] },
      jobs: Array.from({ length: 37 }, (_, index) => ({
        id: `synthetic-${index}`,
        owner: 'unchanged-owner',
        enabled: false,
        agent: { prompt: 'unchanged task', workspace: '/unchanged/workspace' },
        schedule: { expr: '0 6 * * *', tz: 'Asia/Shanghai' },
        ...(index < 19 ? {
          execution: { mode: 'harness_dispatch', harnessDispatch: {
            harnessBin: old, harnessRoot: '/unchanged/ledger', agentId: 'unchanged-agent',
          } },
        } : {}),
      })),
    };
    const target = tempConfig(initial);
    roots.push(target.dir);
    chmodSync(target.path, 0o600);
    const before = readFileSync(target.path);
    const next = structuredClone(initial);
    for (const job of next.jobs) {
      if (job.execution?.harnessDispatch.harnessBin === old) {
        job.execution.harnessDispatch.harnessBin = canonical;
      }
    }
    const result = writeCronJobsConfig(target.path, next, 'wi184-disabled-harness-entrypoints', {
      env: configuredEnv(),
      expectedPreimageSha256: createHash('sha256').update(before).digest('hex'),
      runCommand: successfulRunner(next),
    });
    expect(result.changed).toBe(true);
    expect(statSync(target.path).mode & 0o777).toBe(0o600);
    const after = JSON.parse(readFileSync(target.path, 'utf8')) as typeof next;
    expect(after.jobs).toHaveLength(37);
    expect(after.jobs.every(job => job.enabled === false)).toBe(true);
    expect(after.jobs.filter(job => job.execution?.harnessDispatch.harnessBin === canonical)).toHaveLength(19);
    for (const job of after.jobs) {
      if (job.execution?.harnessDispatch.harnessBin === canonical) job.execution.harnessDispatch.harnessBin = old;
    }
    expect(after).toEqual(initial);
  });

  it('refuses a concurrent preimage change without overwriting or attesting it', () => {
    const target = tempConfig({ version: 2, jobs: [{ id: 'old', enabled: false }] });
    roots.push(target.dir);
    const expected = createHash('sha256').update(readFileSync(target.path)).digest('hex');
    const concurrent = Buffer.from('{"version":2,"jobs":[{"id":"other","enabled":false}],"keep":true}\n');
    writeFileSync(target.path, concurrent);
    const next = { version: 2, jobs: [{ id: 'old', enabled: false, changed: true }] };
    const runCommand = vi.fn(successfulRunner(next));
    expect(() => writeCronJobsConfig(target.path, next, 'test', {
      env: configuredEnv(), expectedPreimageSha256: expected, runCommand,
    })).toThrow(/changed after preflight/);
    expect(readFileSync(target.path)).toEqual(concurrent);
    expect(runCommand).not.toHaveBeenCalled();
    expect(existsSync(`${target.path}.runtime-artifact.lock`)).toBe(false);
  });

  it('refuses disappearance of the bound preimage without recreating the configuration', () => {
    const target = tempConfig({ version: 2, jobs: [] });
    roots.push(target.dir);
    const expected = createHash('sha256').update(readFileSync(target.path)).digest('hex');
    rmSync(target.path);
    const runCommand = vi.fn(successfulRunner({ version: 2, jobs: [] }));
    expect(() => writeCronJobsConfig(target.path, { version: 2, jobs: [] }, 'test', {
      env: configuredEnv(), expectedPreimageSha256: expected, runCommand,
    })).toThrow(/changed after preflight/);
    expect(existsSync(target.path)).toBe(false);
    expect(runCommand).not.toHaveBeenCalled();
  });

  it('rejects malformed preimage digests before creating a lock', () => {
    const target = tempConfig({ version: 2, jobs: [] });
    roots.push(target.dir);
    const before = readFileSync(target.path);
    expect(() => writeCronJobsConfig(target.path, { version: 2, jobs: [] }, 'test', {
      env: {}, expectedPreimageSha256: 'not-a-digest',
    })).toThrow(/SHA-256/);
    expect(readFileSync(target.path)).toEqual(before);
    expect(existsSync(`${target.path}.runtime-artifact.lock`)).toBe(false);
  });

  it('keeps an exact postimage retry a no-op even with the original preflight digest', () => {
    const initial = { version: 2, jobs: [{ id: 'same', enabled: false }] };
    const next = { version: 2, jobs: [{ id: 'same', enabled: false, changed: true }] };
    const target = tempConfig(initial);
    roots.push(target.dir);
    const expected = createHash('sha256').update(readFileSync(target.path)).digest('hex');
    const runCommand = vi.fn(successfulRunner(next));
    const options = { env: configuredEnv(), expectedPreimageSha256: expected, runCommand };
    expect(writeCronJobsConfig(target.path, next, 'test', options).changed).toBe(true);
    const after = readFileSync(target.path);
    expect(writeCronJobsConfig(target.path, next, 'test', options)).toEqual({ changed: false, receiptId: null });
    expect(readFileSync(target.path)).toEqual(after);
    expect(runCommand).toHaveBeenCalledTimes(1);
  });

  it('restores exact preimage bytes and mode after a bound write attestation fails', () => {
    const target = tempConfig();
    roots.push(target.dir);
    const before = Buffer.from('{"version":2,"jobs":[{"id":"same","enabled":false}]}\n');
    writeFileSync(target.path, before);
    chmodSync(target.path, 0o600);
    const expected = createHash('sha256').update(before).digest('hex');
    expect(() => writeCronJobsConfig(target.path, { version: 2, jobs: [] }, 'test', {
      env: configuredEnv(), expectedPreimageSha256: expected,
      runCommand: () => ({ status: 23, stdout: '', stderr: 'synthetic rejection' }),
    })).toThrow(/attestation failed/);
    expect(readFileSync(target.path)).toEqual(before);
    expect(statSync(target.path).mode & 0o777).toBe(0o600);
    expect(existsSync(`${target.path}.runtime-artifact.lock`)).toBe(false);
  });

  it('does not mint a second receipt for an unchanged postimage', () => {
    const document = { version: 2, jobs: [{ id: 'same', enabled: false }] };
    const target = tempConfig(document);
    roots.push(target.dir);
    const runCommand = vi.fn(successfulRunner(document));

    const result = writeCronJobsConfig(target.path, document, 'test', {
      env: configuredEnv(),
      runCommand,
    });

    expect(result).toEqual({ changed: false, receiptId: null });
    expect(runCommand).not.toHaveBeenCalled();
  });
});
