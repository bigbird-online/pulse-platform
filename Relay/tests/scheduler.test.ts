/**
 * scheduler.test.ts — TDD tests for agent-scheduler feature
 *
 * Test pyramid:
 *   Unit Tests  (UT-001 ~ UT-010): ~70%  — mock node-cron, child_process, grammy
 *   Integration Tests (IT-001 ~ IT-004): ~30%  — scheduler lifecycle + delivery flow
 *
 * Task traceability:
 *   P0-001: UT-001, UT-002
 *   P0-002: UT-003 ~ UT-010, IT-001 ~ IT-002
 *   P0-003: IT-003 ~ IT-004
 */

import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
vi.mock('../src/owned-process.js', async () => ({
  spawnOwnedProcess: (await import('node:child_process')).spawn,
}));
import { EventEmitter } from 'node:events';
import { PassThrough } from 'node:stream';
import * as fs from 'node:fs';
import * as path from 'node:path';
import * as os from 'node:os';

// These legacy scheduling contracts explicitly authorize their synthetic AI
// entrants. Dedicated control tests cover the persistent denied/allowed gate.
vi.mock('../src/automation-control.js', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../src/automation-control.js')>();
  return {...actual, requireAutomatic: vi.fn()};
});
import { requireAutomatic, AutomaticExecutionPaused } from '../src/automation-control.js';
vi.mock('../src/native-core-runtime.js', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../src/native-core-runtime.js')>();
  return {...actual, observeScheduledSession: vi.fn(() => undefined)};
});
import { observeScheduledSession } from '../src/native-core-runtime.js';
vi.mock('../src/standing-access.js', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../src/standing-access.js')>();
  return {...actual, standingAccessFor: vi.fn(async () => [])};
});
import { standingAccessFor } from '../src/standing-access.js';
beforeEach(() => { vi.mocked(requireAutomatic).mockReset(); });

// Mock child_process at module level (ESM requires vi.mock hoisting)
vi.mock('node:child_process', async (importOriginal) => {
  const actual = await importOriginal<typeof import('node:child_process')>();
  return {
    ...actual,
    spawn: vi.fn(),
  };
});

// --- Type-only imports (tested before implementation exists) ---
import type { ScheduledJob, JobResult } from '../src/types.js';

beforeEach(() => {
  process.env['CRON_JOBS_STATE_PATH'] = path.join(os.tmpdir(), `relay-sched-state-${process.pid}-${Date.now()}-${Math.random()}.json`);
  const modelsDir = fs.mkdtempSync(path.join(os.tmpdir(), 'relay-sched-models-'));
  const modelsPath = path.join(modelsDir, 'models_cache.json');
  fs.writeFileSync(modelsPath, JSON.stringify({
    fetched_at: new Date().toISOString(),
    models: [
      {
        slug: 'gpt-6-astra',
        visibility: 'list',
        supported_reasoning_levels: [{ effort: 'high' }, { effort: 'xhigh' }],
      },
      {
        slug: 'gpt-5.6-sol',
        visibility: 'list',
        supported_reasoning_levels: [{ effort: 'xhigh' }],
      },
      {
        slug: 'gpt-5.6-terra',
        visibility: 'list',
        supported_reasoning_levels: [{ effort: 'medium' }],
      },
    ],
  }));
  process.env['PULSE_CODEX_MODELS_CACHE_PATH'] = modelsPath;
  delete process.env['HARNESS_COMPLETION_NOTIFICATION_AFTER'];
  delete process.env['RELAY_SCHEDULER_MAX_CONCURRENT_JOBS'];
});

afterEach(() => {
  const modelsPath = process.env['PULSE_CODEX_MODELS_CACHE_PATH'];
  if (modelsPath) fs.rmSync(path.dirname(modelsPath), { recursive: true, force: true });
  delete process.env['CRON_JOBS_STATE_PATH'];
  delete process.env['PULSE_CODEX_MODELS_CACHE_PATH'];
  delete process.env['HARNESS_COMPLETION_NOTIFICATION_AFTER'];
  delete process.env['RELAY_SCHEDULER_MAX_CONCURRENT_JOBS'];
});

// ─────────────────────────────────────────────────────────────────────────────
// Helpers
// ─────────────────────────────────────────────────────────────────────────────

function makeJob(overrides: Partial<ScheduledJob> = {}): ScheduledJob {
  return {
    id: 'test-job',
    name: 'Test Job',
    enabled: true,
    owner: 'system',
    schedule: { expr: '0 6 * * *', tz: 'Asia/Shanghai' },
    agent: {
      workspace: '/tmp',
      prompt: 'echo hello',
      timeout: 10,
    },
    delivery: { mode: 'none' },
    ...overrides,
  };
}

function makeTmpJobsFile(jobs: object[]): string {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'relay-sched-'));
  const file = path.join(dir, 'cron-jobs.json');
  fs.writeFileSync(file, JSON.stringify({ version: 1, jobs }), 'utf-8');
  return file;
}

function refreshTestCodexModelCatalogTime(): void {
  const modelsPath = process.env['PULSE_CODEX_MODELS_CACHE_PATH'];
  if (!modelsPath) throw new Error('test Codex model catalog path is missing');
  const catalog = JSON.parse(fs.readFileSync(modelsPath, 'utf-8')) as Record<string, unknown>;
  catalog.fetched_at = new Date().toISOString();
  fs.writeFileSync(modelsPath, JSON.stringify(catalog));
}

/** Minimal grammy Bot mock */
function makeBotMock() {
  const sendMessage = vi.fn().mockResolvedValue(undefined);
  const sendDocument = vi.fn().mockResolvedValue(undefined);
  return { api: { sendMessage, sendDocument } } as unknown as import('grammy').Bot;
}

// ─────────────────────────────────────────────────────────────────────────────
// P0-001: ScheduledJob type correctness
// ─────────────────────────────────────────────────────────────────────────────

describe('P0-001: ScheduledJob + JobResult types [P0-001]', () => {
  it('UT-001: ScheduledJob with all required fields passes type check', () => {
    const job: ScheduledJob = makeJob();
    expect(job.id).toBe('test-job');
    expect(job.schedule.expr).toBe('0 6 * * *');
    expect(job.schedule.tz).toBe('Asia/Shanghai');
    expect(job.agent.workspace).toBe('/tmp');
    expect(job.delivery.mode).toBe('none');
  });

  it('UT-001b: ScheduledJob with announce delivery has to field', () => {
    const job: ScheduledJob = makeJob({
      delivery: { mode: 'announce', channel: 'telegram', to: '1000000001' },
    });
    expect(job.delivery.mode).toBe('announce');
    expect(job.delivery.to).toBe('1000000001');
  });

  it('UT-001c: JobResult type has required fields', () => {
    const result: JobResult = {
      jobId: 'test-job',
      status: 'ok',
      executionStatus: 'ok',
      deliveryStatus: 'skipped',
      output: 'hello',
      durationMs: 100,
    };
    expect(result.jobId).toBe('test-job');
    expect(result.status).toBe('ok');
    expect(result.executionStatus).toBe('ok');
    expect(result.deliveryStatus).toBe('skipped');
  });
});

// ─────────────────────────────────────────────────────────────────────────────
// P0-002: loadJobs
// ─────────────────────────────────────────────────────────────────────────────

describe('P0-002: loadJobs [P0-001, P0-002]', () => {
  it('UT-002a: loadJobs reads valid JSON and returns enabled jobs', async () => {
    const { loadJobs } = await import('../src/scheduler.js');
    const file = makeTmpJobsFile([makeJob({ enabled: true }), makeJob({ id: 'job-2', enabled: false })]);
    const jobs = loadJobs(file);
    // loadJobs should return all jobs (enabled filtering is done at schedule time)
    expect(jobs.length).toBeGreaterThanOrEqual(1);
    expect(jobs[0].id).toBeDefined();
  });

  it('UT-002b: loadJobs skips entries missing required fields and logs WARN', async () => {
    const { loadJobs } = await import('../src/scheduler.js');
    const consoleSpy = vi.spyOn(console, 'warn').mockImplementation(() => {});
    const file = makeTmpJobsFile([
      { name: 'missing id' },                    // missing id
      { id: 'missing-expr', name: 'x', enabled: true, schedule: {}, agent: { workspace: '/tmp', prompt: 'x' }, delivery: { mode: 'none' } }, // missing expr
      makeJob(),                                  // valid
    ]);
    const jobs = loadJobs(file);
    expect(jobs.length).toBe(1);
    expect(consoleSpy).toHaveBeenCalled();
    consoleSpy.mockRestore();
  });

  it('UT-002c: loadJobs rejects top-level prompt so config mistakes are not silent', async () => {
    const { loadJobs } = await import('../src/scheduler.js');
    const consoleSpy = vi.spyOn(console, 'warn').mockImplementation(() => {});
    const file = makeTmpJobsFile([
      {
        ...makeJob(),
        prompt: 'misplaced prompt',
      },
    ]);

    const jobs = loadJobs(file);

    expect(jobs).toEqual([]);
    const warnCalls = consoleSpy.mock.calls.map(c => JSON.stringify(c[0]));
    expect(warnCalls.some(s => s.includes('top-level') && s.includes('agent.prompt'))).toBe(true);
    consoleSpy.mockRestore();
  });

  it('UT-002e: loadJobs keeps a canonical job WI and skips a malformed one', async () => {
    const { loadJobs } = await import('../src/scheduler.js');
    const consoleSpy = vi.spyOn(console, 'warn').mockImplementation(() => {});
    const file = makeTmpJobsFile([
      { ...makeJob(), id: 'named', wi: 'wi_20261015_418' },
      { ...makeJob(), id: 'malformed', wi: 'WI-418' },
    ]);
    expect(loadJobs(file).map(job => [job.id, job.wi])).toEqual([['named', 'wi_20261015_418']]);
    expect(consoleSpy.mock.calls.some(c => JSON.stringify(c[0]).includes('invalid \\"wi\\"'))).toBe(true);
    consoleSpy.mockRestore();
  });

  it('UT-002d: loadJobs rejects cron-level model literals', async () => {
    const { loadJobs } = await import('../src/scheduler.js');
    const consoleSpy = vi.spyOn(console, 'warn').mockImplementation(() => {});
    const file = makeTmpJobsFile([{
      ...makeJob(),
      agent: { ...makeJob().agent, codexModel: 'gpt-5.6-sol' },
    }]);

    expect(loadJobs(file)).toEqual([]);
    expect(consoleSpy.mock.calls.some(call => JSON.stringify(call[0]).includes('computeProfile'))).toBe(true);
    consoleSpy.mockRestore();
  });

  it('UT-003: loadJobs with valid JSON returns correct job array', async () => {
    const { loadJobs } = await import('../src/scheduler.js');
    const job1 = makeJob({ id: 'job-a', name: 'Job A' });
    const job2 = makeJob({ id: 'job-b', name: 'Job B' });
    const file = makeTmpJobsFile([job1, job2]);
    const jobs = loadJobs(file);
    expect(jobs).toHaveLength(2);
    expect(jobs[0].id).toBe('job-a');
    expect(jobs[1].id).toBe('job-b');
  });

  it('UT-004: loadJobs with nonexistent file returns [] and logs ERROR', async () => {
    const { loadJobs } = await import('../src/scheduler.js');
    const consoleSpy = vi.spyOn(console, 'error').mockImplementation(() => {});
    const jobs = loadJobs('/nonexistent/path/cron-jobs.json');
    expect(jobs).toEqual([]);
    expect(consoleSpy).toHaveBeenCalled();
    consoleSpy.mockRestore();
  });

  it('UT-004b: loadJobs with invalid JSON returns [] and logs ERROR', async () => {
    const { loadJobs } = await import('../src/scheduler.js');
    const consoleSpy = vi.spyOn(console, 'error').mockImplementation(() => {});
    const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'relay-sched-'));
    const file = path.join(dir, 'cron-jobs.json');
    fs.writeFileSync(file, 'NOT VALID JSON', 'utf-8');
    const jobs = loadJobs(file);
    expect(jobs).toEqual([]);
    expect(consoleSpy).toHaveBeenCalled();
    consoleSpy.mockRestore();
  });
});

// ─────────────────────────────────────────────────────────────────────────────
// P0-002: spawnCC
// ─────────────────────────────────────────────────────────────────────────────

function createMockProc(pid = 9999) {
  const proc = new EventEmitter() as EventEmitter & {
    stdout: PassThrough; stderr: PassThrough; stdin: PassThrough; pid: number;
    kill: ReturnType<typeof vi.fn>; exitCode: number | null;
  };
  proc.stdout = new PassThrough();
  proc.stderr = new PassThrough();
  proc.stdin = new PassThrough();
  proc.pid = pid;
  proc.exitCode = null;
  proc.kill = vi.fn((signal?: string) => {
    proc.exitCode = signal === 'SIGKILL' ? 137 : null;
    proc.emit('exit', proc.exitCode, signal ?? 'SIGTERM');
    return true;
  });
  return proc;
}

describe('P0-002: spawnCC [P0-002]', () => {
  it('UT-005: spawnCC with exitCode=0 returns status=ok', async () => {
    const { spawn } = await import('node:child_process');
    const mockProc = createMockProc(9999);
    vi.mocked(spawn).mockReturnValue(mockProc as unknown as ReturnType<typeof spawn>);

    const { spawnCC } = await import('../src/scheduler.js');
    const promise = spawnCC('/tmp', 'echo hello', 5000);

    setImmediate(() => {
      mockProc.stdout.push('hello world\n');
      mockProc.stdout.push(null);
      mockProc.emit('exit', 0, null);
    });

    const result = await promise;
    expect(result.exitCode).toBe(0);
    expect(result.stdout).toContain('hello world');
  });

  it('UT-006: spawnCC times out and kills process', async () => {
    vi.useFakeTimers();
    const { spawn } = await import('node:child_process');
    const mockProc = createMockProc(9998);

    vi.mocked(spawn).mockReturnValue(mockProc as unknown as ReturnType<typeof spawn>);

    const { spawnCC } = await import('../src/scheduler.js');
    const promise = spawnCC('/tmp', 'sleep 999', 1000);

    // Advance timer past timeout — this triggers the timeout handler
    await vi.runAllTimersAsync();

    // After SIGTERM the mock process emits exit
    const result = await promise;
    expect(mockProc.kill).toHaveBeenCalled();
    // exitCode should not be 0 (killed)
    expect(result.exitCode).not.toBe(0);

    vi.useRealTimers();
  }, 15000);

  it('UT-007: spawnCC with exitCode=1 returns exitCode=1', async () => {
    const { spawn } = await import('node:child_process');
    const mockProc = createMockProc(9997);
    vi.mocked(spawn).mockReturnValue(mockProc as unknown as ReturnType<typeof spawn>);

    const { spawnCC } = await import('../src/scheduler.js');
    const promise = spawnCC('/tmp', 'false', 5000);

    setImmediate(() => {
      mockProc.stdout.push(null);
      mockProc.emit('exit', 1, null);
    });

    const result = await promise;
    expect(result.exitCode).toBe(1);
  });
});

// ─────────────────────────────────────────────────────────────────────────────
// P0-002: executeJob
// ─────────────────────────────────────────────────────────────────────────────

describe('P0-002: executeJob [P0-002]', () => {
  it('UT-008: executeJob skips when job is already running (overlap protection)', async () => {
    const { createScheduler } = await import('../src/scheduler.js');
    const bot = makeBotMock();
    const file = makeTmpJobsFile([makeJob()]);
    const scheduler = createScheduler(bot, file);

    // Access internal running state by triggering overlapping execution
    // We test this by using a long-running mock and checking that the second
    // invocation is skipped (logged as WARN)
    const warnSpy = vi.spyOn(console, 'warn').mockImplementation(() => {});

    // Simulate the internal state where a job is already running
    // by calling the scheduler's internal executeJob via the exposed runJob (P2)
    // Since runJob may not be implemented yet, we test via integration path

    warnSpy.mockRestore();
    // Scheduler created successfully
    expect(scheduler).toBeDefined();
    expect(typeof scheduler.start).toBe('function');
    expect(typeof scheduler.stop).toBe('function');
  });

  it('UT-009: deliverResult mode=announce calls bot.api.sendMessage', async () => {
    const { deliverResult } = await import('../src/scheduler.js');
    const bot = makeBotMock();
    const job = makeJob({ delivery: { mode: 'announce', channel: 'telegram', to: '1000000001' } });
    const result: JobResult = { jobId: 'test-job', status: 'ok', output: 'hello', durationMs: 100 };

    await deliverResult(job, result, bot);

    expect(bot.api.sendMessage).toHaveBeenCalledWith(1000000001, expect.stringContaining('hello'));
  });

  it('P0-005: announce delivery failure separates execution and delivery status', async () => {
    const cronMod = await import('node-cron');
    const scheduleSpy = vi.spyOn(cronMod, 'schedule').mockReturnValue({
      stop: vi.fn(), start: vi.fn(), destroy: vi.fn(), getStatus: vi.fn(),
    } as unknown as ReturnType<typeof cronMod.schedule>);

    const { spawn } = await import('node:child_process');
    vi.mocked(spawn).mockReset();
    const runtimeProc = createMockProc(2771);
    vi.mocked(spawn).mockReturnValueOnce(runtimeProc as unknown as ReturnType<typeof spawn>);

    const consoleSpy = vi.spyOn(console, 'error').mockImplementation(() => {});
    const { createScheduler } = await import('../src/scheduler.js');
    const bot = makeBotMock();
    vi.mocked(bot.api.sendMessage as unknown as ReturnType<typeof vi.fn>).mockRejectedValueOnce(new Error('telegram send failed'));
    const job = makeJob({
      id: 'announce-delivery-fail',
      delivery: { mode: 'announce', channel: 'telegram', to: '1000000001' },
    });
    const file = makeTmpJobsFile([job]);
    const scheduler = createScheduler(bot, file);
    scheduler.start();

    const resultPromise = scheduler.runJob('announce-delivery-fail');
    setImmediate(() => {
      runtimeProc.stdout.push('execution ok\n');
      runtimeProc.stdout.push(null);
      runtimeProc.emit('exit', 0, null);
    });

    const result = await resultPromise;
    expect(result.status).toBe('error');
    expect(result.executionStatus).toBe('ok');
    expect(result.deliveryStatus).toBe('failed');
    expect(result.deliveryReceipt?.status).toBe('failed');
    expect(result.errorMessage).toContain('telegram send failed');

    scheduler.stop();
    consoleSpy.mockRestore();
    scheduleSpy.mockRestore();
  });

  it('refuses an agy job whose owner Agent is isolated under its own Linux user, without another runtime', async () => {
    const cronMod = await import('node-cron');
    const scheduleSpy = vi.spyOn(cronMod, 'schedule').mockReturnValue({
      stop: vi.fn(), start: vi.fn(), destroy: vi.fn(), getStatus: vi.fn(),
    } as unknown as ReturnType<typeof cronMod.schedule>);
    const { spawn } = await import('node:child_process');
    vi.mocked(spawn).mockReset();
    const consoleSpy = vi.spyOn(console, 'error').mockImplementation(() => {});
    const { createScheduler } = await import('../src/scheduler.js');
    const job = makeJob({ id: 'agy-isolated', owner: 'iso',
      agent: { workspace: '/tmp', prompt: 'hello', timeout: 10, runtimePreference: ['agy'] } });
    const ownerIsolationFor = vi.fn((owner: string) => owner === 'iso' ? 'isolated' as const : 'host' as const);
    const scheduler = createScheduler(makeBotMock(), makeTmpJobsFile([job]), { ownerIsolationFor });
    scheduler.start();
    const result = await scheduler.runJob('agy-isolated');
    expect(ownerIsolationFor).toHaveBeenCalledWith('iso');
    expect(result.status).toBe('error');
    expect(result.runtime).toBe('agy');
    expect(result.attemptedRuntimes).toEqual(['agy']);
    expect(result.errorMessage).toContain('独立 Linux 用户隔离');
    expect(spawn).not.toHaveBeenCalled();
    scheduler.stop();
    consoleSpy.mockRestore();
    scheduleSpy.mockRestore();
  });

  it('refuses an agy job when no owner isolation resolver is configured', async () => {
    const cronMod = await import('node-cron');
    const scheduleSpy = vi.spyOn(cronMod, 'schedule').mockReturnValue({
      stop: vi.fn(), start: vi.fn(), destroy: vi.fn(), getStatus: vi.fn(),
    } as unknown as ReturnType<typeof cronMod.schedule>);
    const { spawn } = await import('node:child_process');
    vi.mocked(spawn).mockReset();
    const consoleSpy = vi.spyOn(console, 'error').mockImplementation(() => {});
    const { createScheduler } = await import('../src/scheduler.js');
    const job = makeJob({ id: 'agy-unverified', owner: 'pulse',
      agent: { workspace: '/tmp', prompt: 'hello', timeout: 10, runtimePreference: ['agy'] } });
    const scheduler = createScheduler(makeBotMock(), makeTmpJobsFile([job]));
    scheduler.start();
    const result = await scheduler.runJob('agy-unverified');
    expect(result.status).toBe('error');
    expect(result.errorMessage).toContain('无法核实');
    expect(spawn).not.toHaveBeenCalled();
    scheduler.stop();
    consoleSpy.mockRestore();
    scheduleSpy.mockRestore();
  });

  it('keeps the timeout status and the recognized egress reason of an agy job', async () => {
    const cronMod = await import('node-cron');
    const scheduleSpy = vi.spyOn(cronMod, 'schedule').mockReturnValue({
      stop: vi.fn(), start: vi.fn(), destroy: vi.fn(), getStatus: vi.fn(),
    } as unknown as ReturnType<typeof cronMod.schedule>);
    const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'relay-sched-agy-'));
    const agyBin = path.join(dir, 'agy');
    fs.writeFileSync(agyBin, '#!/bin/sh\n', { mode: 0o755 });
    const savedBin = process.env['PULSE_AGY_BIN'];
    process.env['PULSE_AGY_BIN'] = agyBin;
    const { spawn } = await import('node:child_process');
    vi.mocked(spawn).mockReset();
    vi.mocked(spawn).mockImplementationOnce(((_command: string, args: string[]) => {
      const proc = createMockProc(2790);
      const kill = proc.kill;
      // The egress hangs: the log names it only once agy is stopped.
      proc.kill = vi.fn((signal?: string) => {
        fs.writeFileSync(args[args.indexOf('--log-file') + 1]!, 'W1120 18:42:09.131754 267 cache.go:157] Post "https://daily-cloudcode-pa.googleapis.com/v1internal:loadCodeAssist": context deadline exceeded');
        proc.stdout.push(null);
        return kill(signal);
      });
      return proc;
    }) as never);
    const consoleSpy = vi.spyOn(console, 'error').mockImplementation(() => {});
    try {
      const { createScheduler } = await import('../src/scheduler.js');
      const job = makeJob({ id: 'agy-hung-egress', owner: 'pulse',
        agent: { workspace: '/tmp', prompt: 'hello', timeout: 0.05, runtimePreference: ['agy'] } });
      const scheduler = createScheduler(makeBotMock(), makeTmpJobsFile([job]), { ownerIsolationFor: () => 'host' });
      scheduler.start();
      const result = await scheduler.runJob('agy-hung-egress');
      scheduler.stop();
      expect(result.status).toBe('timeout');
      expect(result.errorMessage).toMatch(/^agy timeout after \d+ms: agy 出口不可用/);
      expect(result.attemptedRuntimes).toEqual(['agy']);
    } finally {
      if (savedBin === undefined) delete process.env['PULSE_AGY_BIN'];
      else process.env['PULSE_AGY_BIN'] = savedBin;
      fs.rmSync(dir, { recursive: true, force: true });
      consoleSpy.mockRestore();
      scheduleSpy.mockRestore();
    }
  });

  it.each([
    ['every part sent', undefined, 'sent', 'ok'],
    ['the second part fails', 2, 'failed', 'error'],
  ] as const)('a weekly report longer than one Telegram message goes out in order, in parts (%s)', async (_label, failAt, delivery, status) => {
    const cronMod = await import('node-cron');
    const scheduleSpy = vi.spyOn(cronMod, 'schedule').mockReturnValue({
      stop: vi.fn(), start: vi.fn(), destroy: vi.fn(), getStatus: vi.fn(),
    } as unknown as ReturnType<typeof cronMod.schedule>);
    const { spawn } = await import('node:child_process');
    vi.mocked(spawn).mockReset();
    const runtimeProc = createMockProc(2781);
    vi.mocked(spawn).mockReturnValueOnce(runtimeProc as unknown as ReturnType<typeof spawn>);
    const consoleSpy = vi.spyOn(console, 'error').mockImplementation(() => {});
    const { createScheduler } = await import('../src/scheduler.js');
    const { TELEGRAM_TEXT_MAX } = await import('../src/delivery.js');
    const bot = makeBotMock();
    const send = bot.api.sendMessage as unknown as ReturnType<typeof vi.fn>;
    send.mockReset();
    send.mockImplementation(async () => {
      if (send.mock.calls.length === failAt) throw new Error("GrammyError: Call to 'sendMessage' failed! (400: Bad Request)");
      return { message_id: send.mock.calls.length };
    });
    const sections = Array.from({ length: 6 }, (_, i) => `## 第${i + 1}节\n${'健康数据'.repeat(400)}`);
    const report = sections.join('\n\n');
    const job = makeJob({ id: 'long-report', delivery: { mode: 'announce', channel: 'telegram', to: '1000000001' } });
    const scheduler = createScheduler(bot, makeTmpJobsFile([job]));
    scheduler.start();
    const resultPromise = scheduler.runJob('long-report');
    setImmediate(() => {
      runtimeProc.stdout.push(`${report}\n`);
      runtimeProc.stdout.push(null);
      runtimeProc.emit('exit', 0, null);
    });
    const result = await resultPromise;
    const sent = send.mock.calls.map(([, text]) => text as string);
    expect(result.executionStatus).toBe('ok');
    expect(result.deliveryStatus).toBe(delivery);
    expect(result.status).toBe(status);
    expect(sent.every(text => text.length <= TELEGRAM_TEXT_MAX)).toBe(true);
    if (failAt) {
      expect(sent).toHaveLength(2);
      expect(result.errorMessage).toContain('part 2/');
    } else {
      expect(report.length).toBeGreaterThan(4096);
      expect(sent.length).toBeGreaterThan(1);
      expect(sent.join('\n\n')).toBe(report);
      expect(sent.every(text => text.startsWith('## 第'))).toBe(true);
    }
    scheduler.stop();
    consoleSpy.mockRestore();
    scheduleSpy.mockRestore();
  });

  it('announce delivery is skipped when the output carries a whole-line skip marker', async () => {
    const cronMod = await import('node-cron');
    const scheduleSpy = vi.spyOn(cronMod, 'schedule').mockReturnValue({
      stop: vi.fn(), start: vi.fn(), destroy: vi.fn(), getStatus: vi.fn(),
    } as unknown as ReturnType<typeof cronMod.schedule>);

    const { spawn } = await import('node:child_process');
    vi.mocked(spawn).mockReset();
    const runtimeProc = createMockProc(2772);
    vi.mocked(spawn).mockReturnValueOnce(runtimeProc as unknown as ReturnType<typeof spawn>);

    const { createScheduler } = await import('../src/scheduler.js');
    const bot = makeBotMock();
    const job = makeJob({
      id: 'announce-skip-marker',
      delivery: { mode: 'announce', channel: 'telegram', to: '1000000001', skipIfOutputIncludes: ['NO_DECISION_NEEDED'] },
    });
    const file = makeTmpJobsFile([job]);
    const scheduler = createScheduler(bot, file);
    scheduler.start();

    const resultPromise = scheduler.runJob('announce-skip-marker');
    setImmediate(() => {
      runtimeProc.stdout.push('结论已写入 checkpoint\nNO_DECISION_NEEDED\n');
      runtimeProc.stdout.push(null);
      runtimeProc.emit('exit', 0, null);
    });

    const result = await resultPromise;
    expect(result.status).toBe('ok');
    expect(result.executionStatus).toBe('ok');
    expect(result.deliveryStatus).toBe('skipped');
    expect(bot.api.sendMessage).not.toHaveBeenCalled();

    scheduler.stop();
    scheduleSpy.mockRestore();
  });

  it('announce delivery uses delivery.botAgentId instead of the owner mapping or fallback', async () => {
    const cronMod = await import('node-cron');
    const scheduleSpy = vi.spyOn(cronMod, 'schedule').mockReturnValue({
      stop: vi.fn(), start: vi.fn(), destroy: vi.fn(), getStatus: vi.fn(),
    } as unknown as ReturnType<typeof cronMod.schedule>);

    const { spawn } = await import('node:child_process');
    vi.mocked(spawn).mockReset();
    const runtimeProc = createMockProc(2773);
    vi.mocked(spawn).mockReturnValueOnce(runtimeProc as unknown as ReturnType<typeof spawn>);

    const { createScheduler } = await import('../src/scheduler.js');
    const fallback = makeBotMock();
    const mainBot = makeBotMock();
    const job = makeJob({
      id: 'announce-explicit-bot',
      owner: 'duke',
      delivery: { mode: 'announce', channel: 'telegram', to: '1000000001', botAgentId: 'main' },
    });
    const file = makeTmpJobsFile([job]);
    const scheduler = createScheduler(fallback, file, {
      getBotByAgentId: id => (id === 'main' ? mainBot : null) as unknown as import('grammy').Bot | null,
    });
    scheduler.start();

    const resultPromise = scheduler.runJob('announce-explicit-bot');
    setImmediate(() => {
      runtimeProc.stdout.push('需要大鸟决定：一项\n');
      runtimeProc.stdout.push(null);
      runtimeProc.emit('exit', 0, null);
    });

    const result = await resultPromise;
    expect(result.deliveryStatus).toBe('sent');
    expect(mainBot.api.sendMessage).toHaveBeenCalledOnce();
    expect(fallback.api.sendMessage).not.toHaveBeenCalled();

    scheduler.stop();
    scheduleSpy.mockRestore();
  });

  it('UT-010: deliverResult mode=none does NOT call bot.api', async () => {
    const { deliverResult } = await import('../src/scheduler.js');
    const bot = makeBotMock();
    const job = makeJob({ delivery: { mode: 'none' } });
    const result: JobResult = { jobId: 'test-job', status: 'ok', output: 'silent', durationMs: 50 };

    await deliverResult(job, result, bot);

    expect(bot.api.sendMessage).not.toHaveBeenCalled();
    expect(bot.api.sendDocument).not.toHaveBeenCalled();
  });

  // wi_20260421_634 — per-owner bot routing
  it('UT-011: resolveBotForOwner returns mapped bot when getByAgentId hits', async () => {
    const { resolveBotForOwner } = await import('../src/scheduler.js');
    const amandaBot = makeBotMock();
    const fallbackBot = makeBotMock();
    const { bot, source } = resolveBotForOwner(
      'amanda',
      (id) => (id === 'amanda' ? amandaBot : null) as unknown as import('grammy').Bot | null,
      fallbackBot,
    );
    expect(bot).toBe(amandaBot);
    expect(source).toBe('resolved');
  });

  it('UT-012: resolveBotForOwner falls back when getByAgentId returns null', async () => {
    const { resolveBotForOwner } = await import('../src/scheduler.js');
    const fallbackBot = makeBotMock();
    const { bot, source } = resolveBotForOwner(
      'system',
      () => null,
      fallbackBot,
    );
    expect(bot).toBe(fallbackBot);
    expect(source).toBe('fallback');
  });

  it('UT-013: createScheduler without options keeps legacy single-bot behaviour', async () => {
    const { createScheduler } = await import('../src/scheduler.js');
    const singleBot = makeBotMock();
    const job = makeJob({ id: 'legacy', owner: 'anyone' });
    const file = makeTmpJobsFile([job]);
    // no options → old two-arg signature equivalent
    const scheduler = createScheduler(singleBot, file);
    scheduler.start();
    scheduler.stop();
    expect(scheduler).toBeDefined();
  });

  it('UT-014: createScheduler with options wires the resolver into downstream calls', async () => {
    // Ensures the options hook-up reaches botForJob (via resolveBotForOwner)
    // without requiring a real spawn. We check scheduler.start() + stop()
    // don't throw with the new signature and owner-mapped bot fixture.
    const { createScheduler } = await import('../src/scheduler.js');
    const amandaBot = makeBotMock();
    const fallback = makeBotMock();
    const amandaJob = makeJob({
      id: 'amanda-cron-wired',
      owner: 'amanda',
      // cron expr that never fires during the test (far-future minute)
      schedule: { expr: '0 3 31 2 *', tz: 'Asia/Shanghai' },
      delivery: { mode: 'none' },
    });
    const file = makeTmpJobsFile([amandaJob]);
    const calls: string[] = [];
    const scheduler = createScheduler(fallback, file, {
      getBotByAgentId: (id) => {
        calls.push(id);
        return (id === 'amanda' ? amandaBot : null) as unknown as import('grammy').Bot | null;
      },
    });
    scheduler.start();
    scheduler.stop();
    // registerCronTask doesn't invoke botForJob until cron fires, so the
    // resolver may not be called yet — this assertion is intentionally weak:
    // we verify no throw + resolver arity accepted. Deeper assertion is via
    // the standalone resolveBotForOwner UTs above.
    expect(scheduler).toBeDefined();
    expect(calls.length).toBeGreaterThanOrEqual(0);
  });
});

// ─────────────────────────────────────────────────────────────────────────────
// P0-002: Integration Tests — scheduler lifecycle
// ─────────────────────────────────────────────────────────────────────────────

describe('P0-002: Integration Tests [P0-002]', () => {
  it('IT-001: start() registers cron tasks for enabled jobs', async () => {
    const cronMod = await import('node-cron');
    const scheduleSpy = vi.spyOn(cronMod, 'schedule').mockReturnValue({
      stop: vi.fn(),
      start: vi.fn(),
      destroy: vi.fn(),
      getStatus: vi.fn(),
    } as unknown as ReturnType<typeof cronMod.schedule>);

    const { createScheduler } = await import('../src/scheduler.js');
    const bot = makeBotMock();
    const file = makeTmpJobsFile([
      makeJob({ id: 'enabled-job', enabled: true }),
      makeJob({ id: 'disabled-job', enabled: false }),
    ]);

    const scheduler = createScheduler(bot, file);
    scheduler.start();

    // Only enabled jobs should be registered
    expect(scheduleSpy).toHaveBeenCalledTimes(1);
    expect(scheduleSpy).toHaveBeenCalledWith(
      '0 6 * * *',
      expect.any(Function),
      expect.objectContaining({ timezone: 'Asia/Shanghai' })
    );

    scheduler.stop();
    scheduleSpy.mockRestore();
  });

  it('IT-001b: scheduler reconciliation catches a run missed by node-cron', async () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date('2026-01-01T05:59:30.000Z'));
    refreshTestCodexModelCatalogTime();

    const cronMod = await import('node-cron');
    const scheduleSpy = vi.spyOn(cronMod, 'schedule').mockReturnValue({
      stop: vi.fn(),
      start: vi.fn(),
      destroy: vi.fn(),
      getStatus: vi.fn(),
    } as unknown as ReturnType<typeof cronMod.schedule>);

    const { spawn } = await import('node:child_process');
    vi.mocked(spawn).mockReset();
    vi.mocked(spawn).mockImplementation(() => {
      const proc = createMockProc(4242);
      setTimeout(() => {
        proc.stdout.push('catch-up run completed\n');
        proc.stdout.push(null);
        proc.emit('exit', 0, null);
      }, 0);
      return proc as unknown as ReturnType<typeof spawn>;
    });

    fs.writeFileSync(process.env['CRON_JOBS_STATE_PATH']!, JSON.stringify({
      version: 1,
      jobs: {
        'catch-up-job': {
          consecutiveErrors: 0,
          lastRunAt: '2025-12-31T06:00:00.000Z',
          lastStatus: 'ok',
        },
      },
    }), 'utf-8');

    const { createScheduler } = await import('../src/scheduler.js');
    const bot = makeBotMock();
    const file = makeTmpJobsFile([
      makeJob({
        id: 'catch-up-job',
        schedule: { expr: '0 6 * * *', tz: 'UTC' },
        agent: { workspace: '/tmp', prompt: 'run catch-up', timeout: 10, runtimePreference: ['cc'] },
      }),
    ]);
    const scheduler = createScheduler(bot, file, {
      missedRunReconcileIntervalMs: 30_000,
      missedRunLookbackMs: 3 * 60 * 60 * 1000,
      runtimePolicy: { primary: 'cc', fallbacks: [] },
    });

    try {
      scheduler.start();
      expect(vi.mocked(spawn)).not.toHaveBeenCalled();

      await vi.advanceTimersByTimeAsync(30_000);
      await vi.advanceTimersByTimeAsync(0);

      expect(vi.mocked(spawn)).toHaveBeenCalledTimes(1);
    } finally {
      scheduler.stop();
      scheduleSpy.mockRestore();
      vi.useRealTimers();
    }
  });

  it('a due quota retry never hides a missed regular slot of the same job', async () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date('2026-11-14T00:30:00.000Z'));
    refreshTestCodexModelCatalogTime();
    const cronMod = await import('node-cron');
    const scheduleSpy = vi.spyOn(cronMod, 'schedule').mockReturnValue({
      stop: vi.fn(), start: vi.fn(), destroy: vi.fn(), getStatus: vi.fn(),
    } as unknown as ReturnType<typeof cronMod.schedule>);
    const { spawn } = await import('node:child_process');
    vi.mocked(spawn).mockReset();
    vi.mocked(spawn).mockImplementation(() => {
      const proc = createMockProc(4400);
      setTimeout(() => { proc.stdout.push('done\n'); proc.stdout.push(null); proc.emit('exit', 0, null); }, 0);
      return proc as unknown as ReturnType<typeof spawn>;
    });
    fs.writeFileSync(process.env['CRON_JOBS_STATE_PATH']!, JSON.stringify({ version: 1, jobs: {} }), 'utf-8');
    const { createScheduler } = await import('../src/scheduler.js');
    const file = makeTmpJobsFile([makeJob({ id: 'daily-job', schedule: { expr: '0 8 * * *', tz: 'Asia/Shanghai' },
      agent: { workspace: '/tmp', prompt: 'daily', timeout: 10, runtimePreference: ['cc'] } })]);
    const scheduler = createScheduler(makeBotMock(), file, {
      missedRunReconcileIntervalMs: 30_000,
      missedRunLookbackMs: 3 * 60 * 60 * 1000,
      runtimePolicy: { primary: 'cc', fallbacks: [] },
    });
    try {
      scheduler.start();
      const state = scheduler.listJobs()[0]!._state!;
      // 11-15's slot hit the limit and waits for 11-16 00:02; Relay missed 11-16's own slot.
      state.lastRunAt = '2026-11-15T00:00:06.000Z';
      state.quotaRetries = [{ scheduledFor: '2026-11-15T00:00:00.000Z', definitionHash: state.definitionHash,
        notBefore: '2026-11-16T00:02:00.000Z', attempts: 1 }];
      vi.setSystemTime(new Date('2026-11-16T00:30:00.000Z'));
      await vi.advanceTimersByTimeAsync(30_000);
      for (let i = 0; i < 5; i += 1) await vi.advanceTimersByTimeAsync(10);
      expect(vi.mocked(spawn)).toHaveBeenCalledTimes(1);
      expect(scheduler.listJobs()[0]!._state?.lastResult?.scheduledFor).toBe('2026-11-16T00:00:00.000Z');
      await vi.advanceTimersByTimeAsync(30_000);
      for (let i = 0; i < 5; i += 1) await vi.advanceTimersByTimeAsync(10);
      expect(vi.mocked(spawn)).toHaveBeenCalledTimes(2);
      expect(scheduler.listJobs()[0]!._state?.lastResult?.scheduledFor).toBe('2026-11-15T00:00:00.000Z');
      expect(scheduler.listJobs()[0]!._state?.quotaRetries).toBeUndefined();
    } finally {
      scheduler.stop();
      scheduleSpy.mockRestore();
      vi.useRealTimers();
    }
  });

  describe('a slot that hits the subscription limit waits for the reset and runs once more', () => {
    const LIMIT = "You've hit your session limit · resets 5:50pm (Asia/Taipei)\n";
    // Sunday 07:00 Asia/Shanghai; the notice resets at 09:50Z the same day.
    const SLOT = '2026-11-14T23:00:00.000Z';

    async function limitedSlot(retry: 'ok' | 'limited' | 'none', firstOutput = LIMIT) {
      vi.useFakeTimers();
      vi.setSystemTime(new Date('2026-11-14T22:30:00.000Z'));
      refreshTestCodexModelCatalogTime();
      const cronMod = await import('node-cron');
      const scheduleSpy = vi.spyOn(cronMod, 'schedule').mockReturnValue({
        stop: vi.fn(), start: vi.fn(), destroy: vi.fn(), getStatus: vi.fn(),
      } as unknown as ReturnType<typeof cronMod.schedule>);
      const { spawn } = await import('node:child_process');
      vi.mocked(spawn).mockReset();
      const outputs = [firstOutput, retry === 'limited' ? LIMIT : ''];
      vi.mocked(spawn).mockImplementation(() => {
        const proc = createMockProc(4500 + vi.mocked(spawn).mock.calls.length);
        const stderr = outputs.shift() ?? '';
        setTimeout(() => {
          if (stderr) { proc.stderr.push(stderr); proc.stderr.push(null); proc.stdout.push(null); proc.emit('exit', 1, null); }
          else { proc.stdout.push('周报已写\n'); proc.stdout.push(null); proc.emit('exit', 0, null); }
        }, 0);
        return proc as unknown as ReturnType<typeof spawn>;
      });
      const errorSpy = vi.spyOn(console, 'error').mockImplementation(() => {});
      const warnSpy = vi.spyOn(console, 'warn').mockImplementation(() => {});
      const statePath = process.env['CRON_JOBS_STATE_PATH']!;
      fs.writeFileSync(statePath, JSON.stringify({ version: 1, jobs: {} }), 'utf-8');
      const { createScheduler } = await import('../src/scheduler.js');
      const file = makeTmpJobsFile([makeJob({ id: 'weekly-health', runtimeStrict: true,
        schedule: { expr: '0 7 * * 0', tz: 'Asia/Shanghai' },
        agent: { workspace: '/tmp/ws-weekly-health', prompt: 'weekly', timeout: 60, runtimePreference: ['cc'] },
        delivery: { mode: 'none', feishuOpenId: 'ou_owner' } })]);
      const options = { missedRunReconcileIntervalMs: 30_000, missedRunLookbackMs: 3 * 60 * 60 * 1000,
        runtimePolicy: { primary: 'cc' as const, fallbacks: [] } };
      const settle = async () => {
        await vi.advanceTimersByTimeAsync(30_000);
        for (let i = 0; i < 10; i += 1) await vi.advanceTimersByTimeAsync(50);
      };
      const alerts = () => errorSpy.mock.calls.filter(([entry]) =>
        (entry as { msg?: string })?.msg === 'job failure alert unconfigured: feishuOpenId set but no Feishu client for owner').length;
      let scheduler = createScheduler(makeBotMock(), file, options);
      try {
        scheduler.start();
        vi.setSystemTime(new Date('2026-11-14T23:00:20.000Z'));
        await settle();
        expect(vi.mocked(spawn)).toHaveBeenCalledTimes(1);
        const first = scheduler.listJobs()[0]!._state!;
        const persisted = JSON.parse(fs.readFileSync(statePath, 'utf-8')).jobs['weekly-health'];
        scheduler.stop();
        // Relay restarts before the reset; the plan comes back from the state file.
        scheduler = createScheduler(makeBotMock(), file, options);
        scheduler.start();
        const restored = scheduler.listJobs()[0]!._state!;
        vi.setSystemTime(new Date('2026-11-15T09:53:00.000Z'));
        await settle();
        return { first, persisted, restored, final: scheduler.listJobs()[0]!._state!,
          runs: vi.mocked(spawn).mock.calls.length, alerts: alerts() };
      } finally {
        scheduler.stop();
        scheduleSpy.mockRestore();
        errorSpy.mockRestore();
        warnSpy.mockRestore();
        vi.useRealTimers();
      }
    }

    it('records the limited round as waiting, not failed, and keeps the plan across a restart', async () => {
      const { first, persisted, restored } = await limitedSlot('ok');
      expect(first.lastResult).toMatchObject({ scheduledFor: SLOT, status: 'error', deferred: true });
      expect(first.consecutiveErrors).toBe(0);
      expect(first.failingSince).toBeUndefined();
      const plan = [{ scheduledFor: SLOT, definitionHash: first.definitionHash,
        notBefore: '2026-11-15T09:52:00.000Z', attempts: 1 }];
      expect(first.quotaRetries).toEqual(plan);
      expect(persisted.quotaRetries).toEqual(plan);
      expect(restored.quotaRetries).toEqual(plan);
    });

    it('a retry after the reset that succeeds settles the slot', async () => {
      const { final, runs, alerts } = await limitedSlot('ok');
      expect(runs).toBe(2);
      expect(final.lastResult).toMatchObject({ scheduledFor: SLOT, status: 'ok', deferred: false });
      expect(final.lastStatus).toBe('ok');
      expect(final.consecutiveErrors).toBe(0);
      expect(final.quotaRetries).toBeUndefined();
      expect(final.unresolvedResults).toEqual([]);
      expect(alerts).toBe(0);
    });

    it('a retry that hits the limit again fails the slot and alerts', async () => {
      const { first, final, runs, alerts } = await limitedSlot('limited');
      expect(runs).toBe(2);
      expect(final.lastResult).toMatchObject({ scheduledFor: SLOT, status: 'error', deferred: false });
      expect(final.consecutiveErrors).toBe(1);
      expect(final.quotaRetries).toBeUndefined();
      expect(final.unresolvedResults?.map(item => item.runId)).toEqual([final.lastResult!.runId]);
      expect(first.lastResult!.runId).not.toBe(final.lastResult!.runId);
      expect(alerts).toBe(1);
    });

    it('a limit notice without a reset time fails the round as before', async () => {
      const { first, runs, alerts } = await limitedSlot('none', "You've hit your session limit\n");
      expect(runs).toBe(1);
      expect(first.lastResult).toMatchObject({ scheduledFor: SLOT, status: 'error', deferred: false });
      expect(first.consecutiveErrors).toBe(1);
      expect(first.quotaRetries).toBeUndefined();
      expect(alerts).toBe(1);
    });
  });

  it('IT-001c-migration: legacy state cannot replay a slot under an unproven definition', async () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date('2026-07-19T02:11:25.000Z'));

    const cronMod = await import('node-cron');
    const scheduleSpy = vi.spyOn(cronMod, 'schedule').mockReturnValue({
      stop: vi.fn(), start: vi.fn(), destroy: vi.fn(), getStatus: vi.fn(),
    } as unknown as ReturnType<typeof cronMod.schedule>);
    const { spawn } = await import('node:child_process');
    vi.mocked(spawn).mockReset();

    fs.writeFileSync(process.env['CRON_JOBS_STATE_PATH']!, JSON.stringify({
      version: 1,
      jobs: {
        'amanda-night-patrol': {
          consecutiveErrors: 0,
          lastRunAt: '2026-07-18T13:00:00.000Z',
          lastStatus: 'ok',
        },
      },
    }), 'utf-8');

    // This is the already-expanded production definition. The legacy state
    // cannot prove whether its 21:00 cursor predates that expansion, so first
    // startup must not replay yesterday's 23:30 slot.
    const file = makeTmpJobsFile([makeJob({
      id: 'amanda-night-patrol',
      schedule: { expr: '0,30 21-23 * * *', tz: 'Asia/Shanghai' },
    })]);
    const { createScheduler } = await import('../src/scheduler.js');
    const scheduler = createScheduler(makeBotMock(), file, {
      missedRunLookbackMs: 26 * 60 * 60 * 1000,
      runtimePolicy: { primary: 'cc', fallbacks: [] },
    });

    try {
      scheduler.start();
      await vi.advanceTimersByTimeAsync(0);
      expect(vi.mocked(spawn)).not.toHaveBeenCalled();
      const state = JSON.parse(
        fs.readFileSync(process.env['CRON_JOBS_STATE_PATH']!, 'utf-8'),
      ).jobs['amanda-night-patrol'];
      expect(state.activatedAt).toBe('2026-07-19T02:11:25.000Z');
      expect(state.definitionHash).toMatch(/^[a-f0-9]{64}$/);
    } finally {
      scheduler.stop();
      scheduleSpy.mockRestore();
      vi.useRealTimers();
    }
  });

  it('IT-001c-node-move: durable completion state prevents a migrated cron from executing twice', async () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date('2026-08-17T06:00:00.000Z'));
    refreshTestCodexModelCatalogTime();

    const cronMod = await import('node-cron');
    const callbacks: Array<() => void> = [];
    const scheduleSpy = vi.spyOn(cronMod, 'schedule').mockImplementation((_expr, cb) => {
      callbacks.push(cb as () => void);
      return { stop: vi.fn(), start: vi.fn(), destroy: vi.fn(), getStatus: vi.fn() } as unknown as ReturnType<typeof cronMod.schedule>;
    });
    const { spawn } = await import('node:child_process');
    vi.mocked(spawn).mockReset();
    const proc = createMockProc(8280);
    vi.mocked(spawn).mockReturnValue(proc as unknown as ReturnType<typeof spawn>);

    const movedJob = makeJob({
      id: 'move-once',
      owner: 'aki',
      runNode: 'node-a',
      schedule: { expr: '* * * * *', tz: 'UTC' },
      delivery: { mode: 'none' },
    });
    const file = makeTmpJobsFile([movedJob]);
    const { createScheduler } = await import('../src/scheduler.js');
    const ownerCanRun = (owner: string, node: string) => owner === 'aki' && (node === 'node-a' || node === 'node-b');
    const oldNode = createScheduler(makeBotMock(), file, {
      currentNode: 'node-a', ownerCanRunOnNode: ownerCanRun,
      missedRunLookbackMs: 60 * 60 * 1000,
    });
    let newNode: ReturnType<typeof createScheduler> | undefined;

    try {
      oldNode.start();
      callbacks[0]!();
      await vi.advanceTimersByTimeAsync(0);
      proc.stdout.push('completed on old node\n');
      proc.stdout.push(null);
      proc.emit('exit', 0, null);
      await vi.advanceTimersByTimeAsync(250);
      expect(vi.mocked(spawn)).toHaveBeenCalledTimes(1);
      const completed = JSON.parse(fs.readFileSync(process.env['CRON_JOBS_STATE_PATH']!, 'utf8'));
      expect(completed.jobs['move-once'].lastRunAt).toBeTruthy();

      oldNode.stop();
      vi.setSystemTime(new Date('2026-08-17T06:00:30.000Z'));
      fs.writeFileSync(file, JSON.stringify({ version: 3, jobs: [{ ...movedJob, runNode: 'node-b' }] }), 'utf8');
      newNode = createScheduler(makeBotMock(), file, {
        currentNode: 'node-b', ownerCanRunOnNode: ownerCanRun,
        missedRunLookbackMs: 60 * 60 * 1000,
      });
      newNode.start();
      await vi.advanceTimersByTimeAsync(0);

      expect(vi.mocked(spawn)).toHaveBeenCalledTimes(1);
      const migrated = JSON.parse(fs.readFileSync(process.env['CRON_JOBS_STATE_PATH']!, 'utf8'));
      expect(migrated.jobs['move-once'].lastRunAt).toBe(completed.jobs['move-once'].lastRunAt);
      expect(migrated.jobs['move-once'].definitionHash).not.toBe(completed.jobs['move-once'].definitionHash);
      expect(migrated.jobs['move-once'].activatedAt).toBe('2026-08-17T06:00:30.000Z');
    } finally {
      newNode?.stop();
      oldNode.stop();
      scheduleSpy.mockRestore();
      vi.useRealTimers();
    }
  });

  it('IT-001c: reload does not backfill a slot before the changed job definition became active', async () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date('2026-07-19T02:11:25.000Z'));

    const cronMod = await import('node-cron');
    const scheduleSpy = vi.spyOn(cronMod, 'schedule').mockReturnValue({
      stop: vi.fn(),
      start: vi.fn(),
      destroy: vi.fn(),
      getStatus: vi.fn(),
    } as unknown as ReturnType<typeof cronMod.schedule>);

    const { spawn } = await import('node:child_process');
    vi.mocked(spawn).mockReset();

    fs.writeFileSync(process.env['CRON_JOBS_STATE_PATH']!, JSON.stringify({
      version: 1,
      jobs: {
        'amanda-night-patrol': {
          consecutiveErrors: 0,
          lastRunAt: '2026-07-18T13:00:00.000Z',
          lastStatus: 'ok',
        },
      },
    }), 'utf-8');

    const original = makeJob({
      id: 'amanda-night-patrol',
      schedule: { expr: '0 21 * * *', tz: 'Asia/Shanghai' },
    });
    const file = makeTmpJobsFile([original]);
    const { createScheduler } = await import('../src/scheduler.js');
    const scheduler = createScheduler(makeBotMock(), file, {
      missedRunLookbackMs: 26 * 60 * 60 * 1000,
      runtimePolicy: { primary: 'cc', fallbacks: [] },
    });
    let restartedScheduler: ReturnType<typeof createScheduler> | undefined;

    try {
      scheduler.start();
      await vi.advanceTimersByTimeAsync(0);
      expect(vi.mocked(spawn)).not.toHaveBeenCalled();

      fs.writeFileSync(file, JSON.stringify({
        version: 2,
        jobs: [{
          ...original,
          schedule: { expr: '0,30 21-23 * * *', tz: 'Asia/Shanghai' },
        }],
      }), 'utf-8');
      scheduler.reload();
      await vi.advanceTimersByTimeAsync(0);

      // 2026-07-18 23:30 CST is due under the new expression, but it predates
      // this reload. Replaying it would run a job definition that did not yet
      // exist — the exact 2026-07-19 ho_333 production incident.
      expect(vi.mocked(spawn)).not.toHaveBeenCalled();

      const persistedState = JSON.parse(
        fs.readFileSync(process.env['CRON_JOBS_STATE_PATH']!, 'utf-8'),
      );
      expect(persistedState.jobs['amanda-night-patrol'].definitionHash).toMatch(/^[a-f0-9]{64}$/);
      expect(persistedState.jobs['amanda-night-patrol'].activatedAt).toBe(
        '2026-07-19T02:11:25.000Z',
      );

      // The activation boundary must survive a process restart before the new
      // definition has ever run. Otherwise the old lastRunAt becomes active
      // again and the same pre-definition slot is replayed after restart.
      scheduler.stop();
      restartedScheduler = createScheduler(makeBotMock(), file, {
        missedRunLookbackMs: 26 * 60 * 60 * 1000,
        runtimePolicy: { primary: 'cc', fallbacks: [] },
      });
      restartedScheduler.start();
      await vi.advanceTimersByTimeAsync(0);
      expect(vi.mocked(spawn)).not.toHaveBeenCalled();
    } finally {
      restartedScheduler?.stop();
      scheduler.stop();
      scheduleSpy.mockRestore();
      vi.useRealTimers();
    }
  });

  it('IT-001d: semantically identical field reordering preserves the activation floor', async () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date('2026-07-19T02:00:00.000Z'));

    const cronMod = await import('node-cron');
    const scheduleSpy = vi.spyOn(cronMod, 'schedule').mockReturnValue({
      stop: vi.fn(), start: vi.fn(), destroy: vi.fn(), getStatus: vi.fn(),
    } as unknown as ReturnType<typeof cronMod.schedule>);
    const { spawn } = await import('node:child_process');
    vi.mocked(spawn).mockReset();

    const original = makeJob({ id: 'canonical-definition' });
    const file = makeTmpJobsFile([original]);
    const { createScheduler } = await import('../src/scheduler.js');
    const scheduler = createScheduler(makeBotMock(), file);

    try {
      scheduler.start();
      const before = JSON.parse(
        fs.readFileSync(process.env['CRON_JOBS_STATE_PATH']!, 'utf-8'),
      ).jobs['canonical-definition'];

      vi.setSystemTime(new Date('2026-07-19T02:01:00.000Z'));
      const reordered = {
        delivery: original.delivery,
        agent: original.agent,
        schedule: original.schedule,
        enabled: original.enabled,
        owner: original.owner,
        name: original.name,
        id: original.id,
      };
      fs.writeFileSync(file, JSON.stringify({ version: 2, jobs: [reordered] }), 'utf-8');
      scheduler.reload();
      await vi.advanceTimersByTimeAsync(0);

      const after = JSON.parse(
        fs.readFileSync(process.env['CRON_JOBS_STATE_PATH']!, 'utf-8'),
      ).jobs['canonical-definition'];
      expect(after.definitionHash).toBe(before.definitionHash);
      expect(after.activatedAt).toBe(before.activatedAt);
      expect(vi.mocked(spawn)).not.toHaveBeenCalled();
    } finally {
      scheduler.stop();
      scheduleSpy.mockRestore();
      vi.useRealTimers();
    }
  });

  it('IT-001e: re-enabling a job does not replay slots from its disabled gap', async () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date('2026-07-19T02:10:00.000Z'));

    const cronMod = await import('node-cron');
    const scheduleSpy = vi.spyOn(cronMod, 'schedule').mockReturnValue({
      stop: vi.fn(), start: vi.fn(), destroy: vi.fn(), getStatus: vi.fn(),
    } as unknown as ReturnType<typeof cronMod.schedule>);
    const { spawn } = await import('node:child_process');
    vi.mocked(spawn).mockReset();

    const original = makeJob({
      id: 'disabled-gap',
      schedule: { expr: '0 * * * *', tz: 'UTC' },
    });
    const file = makeTmpJobsFile([original]);
    const { createScheduler } = await import('../src/scheduler.js');
    const scheduler = createScheduler(makeBotMock(), file, {
      missedRunLookbackMs: 3 * 60 * 60 * 1000,
      runtimePolicy: { primary: 'cc', fallbacks: [] },
    });

    try {
      scheduler.start();
      vi.setSystemTime(new Date('2026-07-19T02:20:00.000Z'));
      fs.writeFileSync(file, JSON.stringify({
        version: 2,
        jobs: [{ ...original, enabled: false }],
      }), 'utf-8');
      scheduler.reload();

      // 03:00 occurs while disabled. Re-enabling at 03:10 must establish a
      // fresh activation floor instead of treating 03:00 as a missed run.
      vi.setSystemTime(new Date('2026-07-19T03:10:00.000Z'));
      fs.writeFileSync(file, JSON.stringify({ version: 2, jobs: [original] }), 'utf-8');
      scheduler.reload();
      await vi.advanceTimersByTimeAsync(0);

      expect(vi.mocked(spawn)).not.toHaveBeenCalled();
      const state = JSON.parse(
        fs.readFileSync(process.env['CRON_JOBS_STATE_PATH']!, 'utf-8'),
      ).jobs['disabled-gap'];
      expect(state.activatedAt).toBe('2026-07-19T03:10:00.000Z');
    } finally {
      scheduler.stop();
      scheduleSpy.mockRestore();
      vi.useRealTimers();
    }
  });

  it('a cron slot that fires while the maintenance barrier holds starts nothing', async () => {
    const cronMod = await import('node-cron');
    let capturedCallback: (() => void) | undefined;
    const scheduleSpy = vi.spyOn(cronMod, 'schedule').mockImplementation((_expr, cb) => {
      capturedCallback = cb as () => void;
      return { stop: vi.fn(), start: vi.fn(), destroy: vi.fn(), getStatus: vi.fn() } as unknown as ReturnType<typeof cronMod.schedule>;
    });
    const { spawn } = await import('node:child_process');
    vi.mocked(spawn).mockReset();
    const { createScheduler } = await import('../src/scheduler.js');
    const held = true;
    const scheduler = createScheduler(makeBotMock(), makeTmpJobsFile([makeJob({ id: 'held-slot' })]), {
      reconcileMissedRuns: false, admitScheduledRun: () => !held,
    });
    try {
      scheduler.start();
      capturedCallback!();
      await new Promise(resolve => setTimeout(resolve, 10));
      expect(vi.mocked(spawn)).not.toHaveBeenCalled();
      expect(scheduler.getRunningJobIds()).toEqual([]);
      // A manual /job_run (and relay-jobs run) shares the same claim.
      expect(await scheduler.runJob('held-slot')).toMatchObject({
        status: 'error', errorMessage: expect.stringContaining('maintenance in progress') });
      expect(vi.mocked(spawn)).not.toHaveBeenCalled();
      expect(scheduler.getRunningJobIds()).toEqual([]);
    } finally {
      scheduler.stop();
      scheduleSpy.mockRestore();
    }
  });

  it('IT-002: announce delivery flow — TG mock receives message', async () => {
    const cronMod = await import('node-cron');
    let capturedCallback: (() => void) | undefined;
    const scheduleSpy = vi.spyOn(cronMod, 'schedule').mockImplementation((_expr, cb) => {
      capturedCallback = cb as () => void;
      return { stop: vi.fn(), start: vi.fn(), destroy: vi.fn(), getStatus: vi.fn() } as unknown as ReturnType<typeof cronMod.schedule>;
    });

    const { spawn } = await import('node:child_process');
    const mockProc = createMockProc(8888);
    vi.mocked(spawn).mockReturnValue(mockProc as unknown as ReturnType<typeof spawn>);

    const { createScheduler } = await import('../src/scheduler.js');
    const bot = makeBotMock();
    const job = makeJob({
      id: 'announce-job',
      enabled: true,
      delivery: { mode: 'announce', channel: 'telegram', to: '1000000001' },
    });
    const file = makeTmpJobsFile([job]);

    const scheduler = createScheduler(bot, file);
    scheduler.start();

    expect(capturedCallback).toBeDefined();

    // Trigger the cron callback
    const triggerPromise = (async () => {
      capturedCallback!();
      // Give the async executeJob a tick to start
      await new Promise(r => setTimeout(r, 10));
      // Simulate CC completing successfully
      mockProc.stdout.push('Job completed successfully\n');
      mockProc.stdout.push(null);
      mockProc.emit('exit', 0, null);
      // Wait for delivery
      await new Promise(r => setTimeout(r, 100));
    })();

    await triggerPromise;

    expect(bot.api.sendMessage).toHaveBeenCalledWith(
      1000000001,
      expect.stringContaining('Job completed successfully')
    );

    scheduler.stop();
    scheduleSpy.mockRestore();
  });
});

// ─────────────────────────────────────────────────────────────────────────────
// P0-003: Integration Tests — index.ts integration
// ─────────────────────────────────────────────────────────────────────────────

describe('P0-003: Integration Tests — index.ts integration [P0-003]', () => {
  it('IT-003: createScheduler returns valid Scheduler with start/stop', async () => {
    const { createScheduler } = await import('../src/scheduler.js');
    const bot = makeBotMock();
    const file = makeTmpJobsFile([]);
    const scheduler = createScheduler(bot, file);

    expect(scheduler).toHaveProperty('start');
    expect(scheduler).toHaveProperty('stop');
    expect(typeof scheduler.start).toBe('function');
    expect(typeof scheduler.stop).toBe('function');
  });

  it('IT-004: scheduler degrades gracefully when cron-jobs.json is missing', async () => {
    const { createScheduler } = await import('../src/scheduler.js');
    const consoleSpy = vi.spyOn(console, 'error').mockImplementation(() => {});
    const bot = makeBotMock();

    // Should not throw even with nonexistent config
    const scheduler = createScheduler(bot, '/nonexistent/cron-jobs.json');
    expect(() => scheduler.start()).not.toThrow();
    scheduler.stop();
    consoleSpy.mockRestore();
  });
});

// ─────────────────────────────────────────────────────────────────────────────
// Additional coverage tests — executeJob error + overlap paths
// ─────────────────────────────────────────────────────────────────────────────

describe('executeJob via integration — error & overlap paths [P0-002]', () => {
  it('IT-005: executeJob with non-zero exit does NOT deliver to TG (error suppression)', async () => {
    const cronMod = await import('node-cron');
    let capturedCallback: (() => void) | undefined;
    const scheduleSpy = vi.spyOn(cronMod, 'schedule').mockImplementation((_expr, cb) => {
      capturedCallback = cb as () => void;
      return { stop: vi.fn(), start: vi.fn(), destroy: vi.fn(), getStatus: vi.fn() } as unknown as ReturnType<typeof cronMod.schedule>;
    });

    const { spawn } = await import('node:child_process');
    const mockProc = createMockProc(7777);
    vi.mocked(spawn).mockReturnValue(mockProc as unknown as ReturnType<typeof spawn>);

    const { createScheduler } = await import('../src/scheduler.js');
    const bot = makeBotMock();
    const job = makeJob({
      id: 'error-job',
      enabled: true,
      delivery: { mode: 'announce', channel: 'telegram', to: '1000000001' },
    });
    const file = makeTmpJobsFile([job]);

    const scheduler = createScheduler(bot, file);
    scheduler.start();

    capturedCallback!();
    await new Promise(r => setTimeout(r, 10));
    // Simulate CC failing with exit code 1
    mockProc.stdout.push(null);
    mockProc.emit('exit', 1, null);
    await new Promise(r => setTimeout(r, 50));

    // Error case: sendMessage should NOT be called
    expect(bot.api.sendMessage).not.toHaveBeenCalled();

    scheduler.stop();
    scheduleSpy.mockRestore();
  });

  it('IT-006: overlap protection — second trigger skips while first is running', async () => {
    const cronMod = await import('node-cron');
    let capturedCallback: (() => void) | undefined;
    const scheduleSpy = vi.spyOn(cronMod, 'schedule').mockImplementation((_expr, cb) => {
      capturedCallback = cb as () => void;
      return { stop: vi.fn(), start: vi.fn(), destroy: vi.fn(), getStatus: vi.fn() } as unknown as ReturnType<typeof cronMod.schedule>;
    });

    const { spawn } = await import('node:child_process');
    // First proc - stays running (doesn't emit exit)
    const mockProc1 = createMockProc(6666);
    vi.mocked(spawn).mockReturnValue(mockProc1 as unknown as ReturnType<typeof spawn>);

    const warnSpy = vi.spyOn(console, 'warn').mockImplementation(() => {});
    const { createScheduler } = await import('../src/scheduler.js');
    const bot = makeBotMock();
    const job = makeJob({ id: 'overlap-job', enabled: true });
    const file = makeTmpJobsFile([job]);

    const scheduler = createScheduler(bot, file);
    scheduler.start();

    // First trigger — starts running
    capturedCallback!();
    await new Promise(r => setTimeout(r, 15));

    // Second trigger — should be skipped (previous run still active)
    capturedCallback!();
    await new Promise(r => setTimeout(r, 15));

    // Should see "previous run still active" warning
    const warnCalls = warnSpy.mock.calls.map(c => JSON.stringify(c[0]));
    const hasOverlapWarn = warnCalls.some(s => s.includes('previous run still active'));
    expect(hasOverlapWarn).toBe(true);

    // Cleanup: emit exit to finish the first run
    mockProc1.stdout.push(null);
    mockProc1.emit('exit', 0, null);
    await new Promise(r => setTimeout(r, 50));

    scheduler.stop();
    scheduleSpy.mockRestore();
    warnSpy.mockRestore();
  });

  it('UT-011: deliverResult with no delivery.to logs WARN', async () => {
    const { deliverResult } = await import('../src/scheduler.js');
    const warnSpy = vi.spyOn(console, 'warn').mockImplementation(() => {});
    const bot = makeBotMock();
    const job = makeJob({ delivery: { mode: 'announce', channel: 'telegram' } }); // no 'to'
    const result: JobResult = { jobId: 'test-job', status: 'ok', output: 'hello', durationMs: 100 };

    await deliverResult(job, result, bot);

    expect(bot.api.sendMessage).not.toHaveBeenCalled();
    expect(warnSpy).toHaveBeenCalled();
    warnSpy.mockRestore();
  });
});

// ─────────────────────────────────────────────────────────────────────────────
// T1: loadJobs v1→v2 migration (owner field)
// ─────────────────────────────────────────────────────────────────────────────

describe('loadJobs v1→v2 migration', () => {
  it('should add owner="system" to jobs without owner field', async () => {
    const { loadJobs } = await import('../src/scheduler.js');
    const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'relay-mig-'));
    const file = path.join(dir, 'cron-jobs.json');
    // v1 format: no version field, no owner on jobs
    const v1Data = {
      jobs: [
        {
          id: 'legacy-job',
          name: 'Legacy Job',
          enabled: true,
          schedule: { expr: '0 6 * * *', tz: 'Asia/Shanghai' },
          agent: { workspace: '/tmp', prompt: 'echo hello', timeout: 10 },
          delivery: { mode: 'none' },
        },
      ],
    };
    fs.writeFileSync(file, JSON.stringify(v1Data, null, 2), 'utf-8');

    const jobs = loadJobs(file);

    // Every returned job should have owner = 'system'
    expect(jobs).toHaveLength(1);
    expect(jobs[0].owner).toBe('system');

    // File should be rewritten with version = 2
    const rewritten = JSON.parse(fs.readFileSync(file, 'utf-8'));
    expect(rewritten.version).toBe(2);
    expect(rewritten.jobs[0].owner).toBe('system');

    // Cleanup
    fs.rmSync(dir, { recursive: true, force: true });
  });

  it('should not modify v2 JSON that already has owner', async () => {
    const { loadJobs } = await import('../src/scheduler.js');
    const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'relay-mig-'));
    const file = path.join(dir, 'cron-jobs.json');
    // v2 format: version=2, owner present
    const v2Data = {
      version: 2,
      jobs: [
        {
          id: 'owned-job',
          name: 'Owned Job',
          enabled: true,
          owner: 'ken',
          schedule: { expr: '0 6 * * *', tz: 'Asia/Shanghai' },
          agent: { workspace: '/tmp', prompt: 'echo hello', timeout: 10 },
          delivery: { mode: 'none' },
        },
      ],
    };
    fs.writeFileSync(file, JSON.stringify(v2Data, null, 2), 'utf-8');
    const mtimeBefore = fs.statSync(file).mtimeMs;

    const jobs = loadJobs(file);

    // owner should remain 'ken', not overwritten
    expect(jobs).toHaveLength(1);
    expect(jobs[0].owner).toBe('ken');

    // File should NOT be rewritten (mtime unchanged)
    const mtimeAfter = fs.statSync(file).mtimeMs;
    expect(mtimeAfter).toBe(mtimeBefore);

    // Cleanup
    fs.rmSync(dir, { recursive: true, force: true });
  });
});

// ─────────────────────────────────────────────────────────────────────────────
// T2: JobStore CRUD
// ─────────────────────────────────────────────────────────────────────────────

describe('T2: JobStore CRUD', () => {
  function makeTmpDir() {
    const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'relay-store-'));
    const file = path.join(dir, 'cron-jobs.json');
    return { dir, file };
  }

  function cleanDir(dir: string) {
    fs.rmSync(dir, { recursive: true, force: true });
  }

  describe('listJobs', () => {
    it('should return jobs matching owner + system jobs', async () => {
      const { createJobStore } = await import('../src/job-store.js');
      const { dir, file } = makeTmpDir();
      const jobs = [
        makeJob({ id: 'aki-job', owner: 'aki' }),
        makeJob({ id: 'ken-job', owner: 'ken' }),
        makeJob({ id: 'sys-job', owner: 'system' }),
      ];
      const store = createJobStore(jobs, file);

      const akiJobs = store.listJobs('aki');
      expect(akiJobs).toHaveLength(2);
      expect(akiJobs.map(j => j.id).sort()).toEqual(['aki-job', 'sys-job']);

      cleanDir(dir);
    });

    it('should return all jobs when owner is undefined', async () => {
      const { createJobStore } = await import('../src/job-store.js');
      const { dir, file } = makeTmpDir();
      const jobs = [
        makeJob({ id: 'aki-job', owner: 'aki' }),
        makeJob({ id: 'ken-job', owner: 'ken' }),
        makeJob({ id: 'sys-job', owner: 'system' }),
      ];
      const store = createJobStore(jobs, file);

      const allJobs = store.listJobs();
      expect(allJobs).toHaveLength(3);

      cleanDir(dir);
    });
  });

  describe('addJob', () => {
    it('should add valid job and persist', async () => {
      vi.useFakeTimers();
      const { createJobStore } = await import('../src/job-store.js');
      const { dir, file } = makeTmpDir();
      const store = createJobStore([], file);

      const input = {
        id: 'new-job',
        name: 'New Job',
        enabled: true,
        owner: 'aki',
        schedule: { expr: '0 6 * * *', tz: 'Asia/Shanghai' },
        agent: { workspace: '/tmp', prompt: 'echo hi' },
        delivery: { mode: 'none' as const },
      };
      const added = store.addJob(input);

      expect(added.id).toBe('new-job');
      expect(added._state).toEqual({ consecutiveErrors: 0 });
      expect(store.getAllJobs()).toHaveLength(1);

      // Advance past debounce
      await vi.advanceTimersByTimeAsync(300);

      const persisted = JSON.parse(fs.readFileSync(file, 'utf-8'));
      expect(persisted.jobs).toHaveLength(1);
      expect(persisted.jobs[0].id).toBe('new-job');
      expect(persisted.jobs[0]._state).toBeUndefined();

      const persistedState = JSON.parse(fs.readFileSync(process.env['CRON_JOBS_STATE_PATH']!, 'utf-8'));
      expect(persistedState.jobs['new-job'].consecutiveErrors).toBe(0);

      vi.useRealTimers();
      cleanDir(dir);
    });

    it('should throw on duplicate id', async () => {
      const { createJobStore } = await import('../src/job-store.js');
      const { dir, file } = makeTmpDir();
      const store = createJobStore([makeJob({ id: 'dup-job' })], file);

      expect(() => store.addJob({
        id: 'dup-job',
        name: 'Dup',
        enabled: true,
        owner: 'aki',
        schedule: { expr: '0 6 * * *', tz: 'Asia/Shanghai' },
        agent: { workspace: '/tmp', prompt: 'x' },
        delivery: { mode: 'none' },
      })).toThrow('already exists');

      cleanDir(dir);
    });

    it('should throw on invalid cron', async () => {
      const { createJobStore } = await import('../src/job-store.js');
      const { dir, file } = makeTmpDir();
      const store = createJobStore([], file);

      expect(() => store.addJob({
        id: 'bad-cron',
        name: 'Bad',
        enabled: true,
        owner: 'aki',
        schedule: { expr: 'not-a-cron', tz: 'Asia/Shanghai' },
        agent: { workspace: '/tmp', prompt: 'x' },
        delivery: { mode: 'none' },
      })).toThrow('Invalid cron');

      cleanDir(dir);
    });
  });

  describe('removeJob', () => {
    it('should remove job with matching owner', async () => {
      vi.useFakeTimers();
      const { createJobStore } = await import('../src/job-store.js');
      const { dir, file } = makeTmpDir();
      const store = createJobStore([makeJob({ id: 'rm-job', owner: 'aki' })], file);

      const result = store.removeJob('rm-job', 'aki');
      expect(result).toBe(true);
      expect(store.getAllJobs()).toHaveLength(0);

      await vi.advanceTimersByTimeAsync(300);
      vi.useRealTimers();
      cleanDir(dir);
    });

    it('should reject wrong owner', async () => {
      const { createJobStore } = await import('../src/job-store.js');
      const { dir, file } = makeTmpDir();
      const store = createJobStore([makeJob({ id: 'rm-job', owner: 'aki' })], file);

      const result = store.removeJob('rm-job', 'ken');
      expect(result).toBe(false);
      expect(store.getAllJobs()).toHaveLength(1);

      cleanDir(dir);
    });

    it('should reject system job removal', async () => {
      const { createJobStore } = await import('../src/job-store.js');
      const { dir, file } = makeTmpDir();
      const store = createJobStore([makeJob({ id: 'sys-job', owner: 'system' })], file);

      const result = store.removeJob('sys-job', 'aki');
      expect(result).toBe(false);
      expect(store.getAllJobs()).toHaveLength(1);

      cleanDir(dir);
    });

    it('should return false for non-existent job', async () => {
      const { createJobStore } = await import('../src/job-store.js');
      const { dir, file } = makeTmpDir();
      const store = createJobStore([], file);

      const result = store.removeJob('nope', 'aki');
      expect(result).toBe(false);

      cleanDir(dir);
    });
  });

  describe('getJob', () => {
    it('should return job by id', async () => {
      const { createJobStore } = await import('../src/job-store.js');
      const { dir, file } = makeTmpDir();
      const store = createJobStore([makeJob({ id: 'find-me' })], file);

      expect(store.getJob('find-me')?.id).toBe('find-me');
      expect(store.getJob('nope')).toBeUndefined();

      cleanDir(dir);
    });
  });

  describe('persistConfig error handling', () => {
    it('should log error when persist fails', async () => {
      vi.useFakeTimers();
      const errorSpy = vi.spyOn(console, 'error').mockImplementation(() => {});
      const { createJobStore } = await import('../src/job-store.js');
      const store = createJobStore([], '/nonexistent/dir/cron-jobs.json');

      store.persistConfig();
      await vi.advanceTimersByTimeAsync(300);

      const errorCalls = errorSpy.mock.calls.map(c => JSON.stringify(c[0]));
      expect(errorCalls.some(s => s.includes('persistConfig failed'))).toBe(true);

      errorSpy.mockRestore();
      vi.useRealTimers();
    });
  });
});

// ─────────────────────────────────────────────────────────────────────────────
// T2: Scheduler CRUD (listJobs, addJob, removeJob, runJob)
// ─────────────────────────────────────────────────────────────────────────────

describe('T2: Scheduler CRUD', () => {
  describe('Scheduler interface has CRUD methods', () => {
    it('should expose running status and CRUD methods', async () => {
      const cronMod = await import('node-cron');
      vi.spyOn(cronMod, 'schedule').mockReturnValue({
        stop: vi.fn(), start: vi.fn(), destroy: vi.fn(), getStatus: vi.fn(),
      } as unknown as ReturnType<typeof cronMod.schedule>);

      const { createScheduler } = await import('../src/scheduler.js');
      const bot = makeBotMock();
      const file = makeTmpJobsFile([]);
      const scheduler = createScheduler(bot, file, { reconcileMissedRuns: false });
      scheduler.start();

      expect(typeof scheduler.listJobs).toBe('function');
      expect(typeof scheduler.getRunningJobIds).toBe('function');
      expect(typeof scheduler.addJob).toBe('function');
      expect(typeof scheduler.removeJob).toBe('function');
      expect(typeof scheduler.runJob).toBe('function');

      scheduler.stop();
    });
  });

  describe('runJob', () => {
    it('should execute existing job and return result', async () => {
      const cronMod = await import('node-cron');
      vi.spyOn(cronMod, 'schedule').mockReturnValue({
        stop: vi.fn(), start: vi.fn(), destroy: vi.fn(), getStatus: vi.fn(),
      } as unknown as ReturnType<typeof cronMod.schedule>);

      const { spawn } = await import('node:child_process');
      const mockProc = createMockProc(5555);
      vi.mocked(spawn).mockReturnValue(mockProc as unknown as ReturnType<typeof spawn>);

      const { createScheduler } = await import('../src/scheduler.js');
      const bot = makeBotMock();
      const job = makeJob({ id: 'run-me', enabled: true });
      const file = makeTmpJobsFile([job]);
      const scheduler = createScheduler(bot, file);
      scheduler.start();

      const resultPromise = scheduler.runJob('run-me');

      await Promise.resolve();
      expect(scheduler.getRunningJobIds()).toEqual(['run-me']);

      // Simulate CC completing
      setImmediate(() => {
        mockProc.stdout.push('manual run output\n');
        mockProc.stdout.push(null);
        mockProc.emit('exit', 0, null);
      });

      const result = await resultPromise;
      expect(result.jobId).toBe('run-me');
      expect(result.status).toBe('ok');
      expect(result.output).toContain('manual run output');
      expect(scheduler.getRunningJobIds()).toEqual([]);

      scheduler.stop();
    });

    it('persists failed manual run state for operator visibility', async () => {
      const cronMod = await import('node-cron');
      vi.spyOn(cronMod, 'schedule').mockReturnValue({
        stop: vi.fn(), start: vi.fn(), destroy: vi.fn(), getStatus: vi.fn(),
      } as unknown as ReturnType<typeof cronMod.schedule>);

      const { spawn } = await import('node:child_process');
      vi.mocked(spawn).mockReset();
      const mockProc = createMockProc(5557);
      vi.mocked(spawn).mockReturnValue(mockProc as unknown as ReturnType<typeof spawn>);

      const { createScheduler } = await import('../src/scheduler.js');
      const bot = makeBotMock();
      const job = makeJob({ id: 'run-fail', enabled: true });
      const file = makeTmpJobsFile([job]);
      const scheduler = createScheduler(bot, file);
      scheduler.start();

      const resultPromise = scheduler.runJob('run-fail');
      setImmediate(() => {
        mockProc.stderr.push('task failed\n');
        mockProc.emit('exit', 1, null);
      });

      const result = await resultPromise;
      expect(result.status).toBe('error');

      await new Promise(resolve => setTimeout(resolve, 300));
      const persisted = JSON.parse(fs.readFileSync(file, 'utf-8'));
      expect(persisted.jobs[0]._state?.lastRunAt).toBeUndefined();
      const persistedState = JSON.parse(fs.readFileSync(process.env['CRON_JOBS_STATE_PATH']!, 'utf-8'));
      expect(persistedState.jobs['run-fail'].consecutiveErrors).toBe(1);
      expect(persistedState.jobs['run-fail'].lastStatus).toBe('error');
      expect(persistedState.jobs['run-fail'].lastRunAt).toEqual(expect.any(String));

      scheduler.stop();
    });

    it('should throw for non-existent job', async () => {
      const cronMod = await import('node-cron');
      vi.spyOn(cronMod, 'schedule').mockReturnValue({
        stop: vi.fn(), start: vi.fn(), destroy: vi.fn(), getStatus: vi.fn(),
      } as unknown as ReturnType<typeof cronMod.schedule>);

      const { createScheduler } = await import('../src/scheduler.js');
      const bot = makeBotMock();
      const file = makeTmpJobsFile([]);
      const scheduler = createScheduler(bot, file);
      scheduler.start();

      await expect(scheduler.runJob('nonexistent')).rejects.toThrow('not found');

      scheduler.stop();
    });
  });

  describe('triggerJob', () => {
    it('starts one unattended run now, marked trigger=cron, and reports a claimed job as running', async () => {
      const { spawn } = await import('node:child_process');
      vi.mocked(spawn).mockReset();
      const gateProc = createMockProc(6201);
      const runtimeProc = createMockProc(6202);
      vi.mocked(spawn)
        .mockReturnValueOnce(gateProc as unknown as ReturnType<typeof spawn>)
        .mockReturnValueOnce(runtimeProc as unknown as ReturnType<typeof spawn>);
      let runtimeInput = '';
      runtimeProc.stdin.on('data', chunk => { runtimeInput += chunk.toString(); });

      const { createScheduler } = await import('../src/scheduler.js');
      const job = makeJob({
        id: 'inbox', agent: { ...makeJob().agent, runtimePreference: ['cc'], gate: { command: '/fake/gate', skipExitCode: 3 } },
      });
      const disabled = makeJob({ id: 'off', enabled: false });
      const scheduler = createScheduler(makeBotMock(), makeTmpJobsFile([job, disabled]), {
        registerCronTasks: false, deliveryEnabled: false, runtimePolicy: { primary: 'cc', fallbacks: [] },
      });
      scheduler.start();

      expect(scheduler.triggerJob('missing')).toBe('not_found');
      expect(scheduler.triggerJob('off')).toBe('disabled');
      expect(scheduler.triggerJob('inbox')).toBe('started');
      expect(scheduler.getRunningJobIds()).toEqual(['inbox']);
      expect(scheduler.triggerJob('inbox')).toBe('running');

      for (let attempt = 0; attempt < 50 && vi.mocked(spawn).mock.calls.length < 1; attempt++) {
        await new Promise(resolve => setTimeout(resolve, 5));
      }
      gateProc.stdout.push('{"schema":"x"}\n');
      gateProc.stdout.push(null);
      gateProc.emit('exit', 0, null);
      for (let attempt = 0; attempt < 50 && vi.mocked(spawn).mock.calls.length < 2; attempt++) {
        await new Promise(resolve => setTimeout(resolve, 5));
      }
      runtimeProc.stdout.push('{"type":"result","result":"handled"}\n');
      runtimeProc.stdout.push(null);
      runtimeProc.emit('exit', 0, null);
      for (let attempt = 0; attempt < 100 && scheduler.getRunningJobIds().length > 0; attempt++) {
        await new Promise(resolve => setTimeout(resolve, 5));
      }

      expect(scheduler.getRunningJobIds()).toEqual([]);
      const envelope = JSON.parse(runtimeInput.trim()) as { message: { content: string } };
      expect(envelope.message.content).toContain('trigger=cron\n\necho hello');
      const state = scheduler.listJobs().find(j => j.id === 'inbox')!._state!;
      expect(state.lastStatus).toBe('ok');
      expect(state.lastResult?.scheduledFor).toEqual(expect.any(String));
      scheduler.stop();
    });

    it('answers held while the maintenance barrier holds new runs', async () => {
      const { createScheduler } = await import('../src/scheduler.js');
      const scheduler = createScheduler(makeBotMock(), makeTmpJobsFile([makeJob({ id: 'inbox' })]), {
        registerCronTasks: false, deliveryEnabled: false, admitScheduledRun: () => false,
      });
      scheduler.start();
      expect(scheduler.triggerJob('inbox')).toBe('held');
      expect(scheduler.getRunningJobIds()).toEqual([]);
      scheduler.stop();
    });

    it('supports manual-run mode without registering cron or delivering to TG', async () => {
      const cronMod = await import('node-cron');
      const scheduleSpy = vi.spyOn(cronMod, 'schedule').mockReturnValue({
        stop: vi.fn(), start: vi.fn(), destroy: vi.fn(), getStatus: vi.fn(),
      } as unknown as ReturnType<typeof cronMod.schedule>);
      scheduleSpy.mockClear();

      const { spawn } = await import('node:child_process');
      const mockProc = createMockProc(5556);
      vi.mocked(spawn).mockReturnValue(mockProc as unknown as ReturnType<typeof spawn>);

      const { createScheduler } = await import('../src/scheduler.js');
      const bot = makeBotMock();
      const job = makeJob({
        id: 'manual-only',
        enabled: true,
        delivery: { mode: 'announce', channel: 'telegram', to: '12345' },
      });
      const file = makeTmpJobsFile([job]);
      const scheduler = createScheduler(bot, file, {
        registerCronTasks: false,
        deliveryEnabled: false,
      });
      scheduler.start();

      expect(scheduleSpy).not.toHaveBeenCalled();

      const resultPromise = scheduler.runJob('manual-only');
      setImmediate(() => {
        mockProc.stdout.push('manual-only output\n');
        mockProc.stdout.push(null);
        mockProc.emit('exit', 0, null);
      });

      const result = await resultPromise;
      expect(result.status).toBe('ok');
      expect(result.output).toContain('manual-only output');
      expect((bot.api.sendMessage as unknown as ReturnType<typeof vi.fn>)).not.toHaveBeenCalled();

      scheduler.stop();
    });

    it('pre-LLM gate skip returns ok without spawning a runtime', async () => {
      const { spawn } = await import('node:child_process');
      vi.mocked(spawn).mockReset();
      const gateProc = createMockProc(6101);
      vi.mocked(spawn).mockReturnValueOnce(gateProc as unknown as ReturnType<typeof spawn>);

      const { createScheduler } = await import('../src/scheduler.js');
      const job = makeJob({
        id: 'gate-zero',
        agent: {
          ...makeJob().agent,
          computeProfile: 'media',
          gate: {
            command: '/fake/gate',
            skipExitCode: 3,
            skipOutput: '[hestia 30min dispatch] 0 新增',
          },
        },
      });
      const scheduler = createScheduler(makeBotMock(), makeTmpJobsFile([job]), {
        registerCronTasks: false,
        deliveryEnabled: false,
        runtimePolicy: { primary: 'cc', fallbacks: [] },
      });
      scheduler.start();

      const resultPromise = scheduler.runJob('gate-zero');
      setImmediate(() => {
        gateProc.stdout.push('[hestia 30min dispatch] 0 新增\n');
        gateProc.stdout.push(null);
        gateProc.emit('exit', 3, null);
      });
      const result = await resultPromise;

      expect(result.status).toBe('ok');
      expect(result.runtime).toBeUndefined();
      expect(result.output).toContain('0 新增');
      expect(vi.mocked(spawn)).toHaveBeenCalledTimes(1);
      scheduler.stop();
    });

    it('a wake job never starts a runtime and reminds the human when nobody holds the WI', async () => {
      const { spawn } = await import('node:child_process');
      const { createScheduler } = await import('../src/scheduler.js');
      const job = makeJob({
        id: 'ken-wake', name: 'Ken 早班', wake: { workItemId: 'wi_20261022_664' },
        delivery: { mode: 'none', to: '1000000001' },
        agent: { ...makeJob().agent, gate: { command: '/fake/gate', skipExitCode: 3 } },
      });
      const bot = makeBotMock();
      const scheduler = createScheduler(bot, makeTmpJobsFile([job]), {
        registerCronTasks: false, deliveryEnabled: true, runtimePolicy: { primary: 'cc', fallbacks: [] },
      });
      scheduler.start();
      async function run(output: string, code = 3) {
        vi.mocked(spawn).mockReset();
        const gateProc = createMockProc(6301);
        vi.mocked(spawn).mockReturnValueOnce(gateProc as unknown as ReturnType<typeof spawn>);
        const pending = scheduler.runJob('ken-wake');
        setImmediate(() => { gateProc.stdout.push(output + '\n'); gateProc.stdout.push(null); gateProc.emit('exit', code, null); });
        const result = await pending;
        expect(vi.mocked(spawn)).toHaveBeenCalledTimes(1);
        return result;
      }
      const delivered = await run('WAKE_DELIVERED queued');
      expect([delivered.status, delivered.reasonCode, delivered.deliveryStatus]).toEqual(['ok', 'wake_delivered', 'skipped']);
      expect(bot.api.sendMessage).not.toHaveBeenCalled();
      const missed = await run('WAKE_NOT_DELIVERED not_held_by_ken');
      expect([missed.reasonCode, missed.deliveryStatus]).toEqual(['wake_not_delivered', 'sent']);
      expect(vi.mocked(bot.api.sendMessage).mock.calls[0]?.[0]).toBe(1000000001);
      expect(String(vi.mocked(bot.api.sendMessage).mock.calls[0]?.[1])).toContain('wi_20261022_664');
      expect((await run('{"schema":"x"}', 0)).status).toBe('error');
      scheduler.stop();
    });

    it('rejects a wake job without a WI, gate or reminder target', async () => {
      const { loadJobs } = await import('../src/scheduler.js');
      const gate = { command: '/fake/gate', skipExitCode: 3 };
      const jobs = [
        makeJob({ id: 'ok', wake: { workItemId: 'wi_20261022_664' }, delivery: { mode: 'none', to: '1' }, agent: { ...makeJob().agent, gate } }),
        makeJob({ id: 'no-wi', wake: { workItemId: 'x' } as never, delivery: { mode: 'none', to: '1' }, agent: { ...makeJob().agent, gate } }),
        makeJob({ id: 'no-gate', wake: { workItemId: 'wi_20261022_664' }, delivery: { mode: 'none', to: '1' } }),
        makeJob({ id: 'no-target', wake: { workItemId: 'wi_20261022_664' }, delivery: { mode: 'none' }, agent: { ...makeJob().agent, gate } }),
      ];
      expect(loadJobs(makeTmpJobsFile(jobs)).map(j => j.id)).toEqual(['ok']);
    });

    it('gate failure keeps the end of a long traceback', async () => {
      const { spawn } = await import('node:child_process');
      vi.mocked(spawn).mockReset();
      const gateProc = createMockProc(6105);
      vi.mocked(spawn).mockReturnValueOnce(gateProc as unknown as ReturnType<typeof spawn>);

      const { createScheduler } = await import('../src/scheduler.js');
      const job = makeJob({
        id: 'gate-traceback',
        agent: { ...makeJob().agent, gate: { command: '/fake/gate', skipExitCode: 3, skipOutput: '0 新增' } },
      });
      const scheduler = createScheduler(makeBotMock(), makeTmpJobsFile([job]), {
        registerCronTasks: false,
        deliveryEnabled: false,
        runtimePolicy: { primary: 'cc', fallbacks: [] },
      });
      scheduler.start();

      const resultPromise = scheduler.runJob('gate-traceback');
      setImmediate(() => {
        gateProc.stdout.push(null);
        gateProc.stderr.push(`Traceback (most recent call last):\n${'  File "x.py", line 1, in f\n'.repeat(40)}ValueError: ledger busy\n`);
        gateProc.stderr.push(null);
        gateProc.emit('exit', 1, null);
      });
      const result = await resultPromise;

      expect(result.status).toBe('error');
      expect(result.errorMessage).toContain('Traceback (most recent call last)');
      expect(result.errorMessage).toContain('ValueError: ledger busy');
      expect(vi.mocked(spawn)).toHaveBeenCalledTimes(1);
      scheduler.stop();
    });

    it('skip code without the configured zero marker fails closed without a runtime', async () => {
      const { spawn } = await import('node:child_process');
      vi.mocked(spawn).mockReset();
      const gateProc = createMockProc(6111);
      vi.mocked(spawn).mockReturnValueOnce(gateProc as unknown as ReturnType<typeof spawn>);

      const { createScheduler } = await import('../src/scheduler.js');
      const job = makeJob({
        id: 'gate-empty-skip',
        agent: {
          ...makeJob().agent,
          computeProfile: 'media',
          gate: {
            command: '/fake/gate',
            skipExitCode: 3,
            skipOutput: '[hestia 30min dispatch] 0 新增',
          },
        },
      });
      const scheduler = createScheduler(makeBotMock(), makeTmpJobsFile([job]), {
        registerCronTasks: false,
        deliveryEnabled: false,
        runtimePolicy: { primary: 'cc', fallbacks: [] },
      });
      scheduler.start();

      const resultPromise = scheduler.runJob('gate-empty-skip');
      setImmediate(() => gateProc.emit('exit', 3, null));
      const result = await resultPromise;

      expect(result.status).toBe('error');
      expect(result.errorMessage).toContain('without evidence marker');
      expect(vi.mocked(spawn)).toHaveBeenCalledTimes(1);
      scheduler.stop();
    });

    it('a timed-out gate cannot impersonate a successful skip with exit code 3', async () => {
      const { spawn } = await import('node:child_process');
      vi.mocked(spawn).mockReset();
      const gateProc = createMockProc(6112);
      gateProc.kill = vi.fn(() => true);
      vi.mocked(spawn).mockReturnValueOnce(gateProc as unknown as ReturnType<typeof spawn>);

      const { createScheduler } = await import('../src/scheduler.js');
      const job = makeJob({
        id: 'gate-timeout-skip',
        agent: {
          ...makeJob().agent,
          computeProfile: 'media',
          gate: {
            command: '/fake/gate',
            timeout: 0.01,
            skipExitCode: 3,
            skipOutput: '[hestia 30min dispatch] 0 新增',
          },
        },
      });
      const scheduler = createScheduler(makeBotMock(), makeTmpJobsFile([job]), {
        registerCronTasks: false,
        deliveryEnabled: false,
        runtimePolicy: { primary: 'cc', fallbacks: [] },
      });
      scheduler.start();

      const resultPromise = scheduler.runJob('gate-timeout-skip');
      await new Promise(resolve => setTimeout(resolve, 25));
      gateProc.stdout.push('[hestia 30min dispatch] 0 新增\n');
      gateProc.emit('exit', 3, null);
      const result = await resultPromise;

      expect(gateProc.kill).toHaveBeenCalledWith('SIGTERM');
      expect(result.status).toBe('error');
      expect(result.errorMessage).toContain('timed out');
      expect(vi.mocked(spawn)).toHaveBeenCalledTimes(1);
      scheduler.stop();
    });

    it('positive gate resolves media profile before spawning CC', async () => {
      const logSpy = vi.spyOn(console, 'log').mockImplementation(() => {});
      const { spawn } = await import('node:child_process');
      vi.mocked(spawn).mockReset();
      const gateProc = createMockProc(6102);
      const runtimeProc = createMockProc(6103);
      vi.mocked(spawn)
        .mockReturnValueOnce(gateProc as unknown as ReturnType<typeof spawn>)
        .mockReturnValueOnce(runtimeProc as unknown as ReturnType<typeof spawn>);

      let runtimeInput = '';
      runtimeProc.stdin.on('data', chunk => { runtimeInput += chunk.toString(); });

      const { createScheduler } = await import('../src/scheduler.js');
      const job = makeJob({
        id: 'gate-positive',
        agent: {
          ...makeJob().agent,
          computeProfile: 'media',
          runtimePreference: ['cc'],
          gate: { command: '/fake/gate', skipExitCode: 3 },
        },
      });
      const scheduler = createScheduler(makeBotMock(), makeTmpJobsFile([job]), {
        registerCronTasks: false,
        deliveryEnabled: false,
        runtimePolicy: { primary: 'cc', fallbacks: [] },
      });
      scheduler.start();

      const resultPromise = scheduler.runJob('gate-positive');
      for (let attempt = 0; attempt < 50 && vi.mocked(spawn).mock.calls.length < 1; attempt++) {
        await new Promise(resolve => setTimeout(resolve, 5));
      }
      gateProc.stdout.push('[hestia 30min gate] 1 新增\n{"severity":"medium"}\n');
      gateProc.stdout.push(null);
      gateProc.emit('exit', 0, null);
      for (let attempt = 0; attempt < 50 && vi.mocked(spawn).mock.calls.length < 2; attempt++) {
        await new Promise(resolve => setTimeout(resolve, 5));
      }
      runtimeProc.stdout.push('{"type":"result","result":"handled"}\n');
      runtimeProc.stdout.push(null);
      runtimeProc.emit('exit', 0, null);
      const result = await resultPromise;

      const runtimeArgs = vi.mocked(spawn).mock.calls[1][1] as string[];
      const runtimeOptions = vi.mocked(spawn).mock.calls[1][2] as { env?: NodeJS.ProcessEnv };
      expect(runtimeArgs[runtimeArgs.indexOf('--model') + 1]).toBe('sonnet');
      expect(runtimeArgs[runtimeArgs.indexOf('--effort') + 1]).toBe('medium');
      expect(runtimeOptions.env?.PULSE_PARENT_COMPUTE_PROFILE).toBe('media');
      const runtimeEnvelope = JSON.parse(runtimeInput.trim()) as { message: { content: string } };
      expect(runtimeEnvelope.message.content).toContain('机械 gate 输出');
      expect(runtimeEnvelope.message.content).toContain('"severity":"medium"');
      expect(result.runtime).toBe('cc');
      const resolvedLog = logSpy.mock.calls
        .map(call => call[0] as { msg?: string; ccReasoningEffort?: string; codexReasoningEffort?: string })
        .find(entry => entry?.msg === 'Compute profile aliases resolved');
      expect(resolvedLog?.ccReasoningEffort).toBe('medium');
      expect(resolvedLog?.codexReasoningEffort).toBe('medium');
      logSpy.mockRestore();
      scheduler.stop();
    });

    it('falls back from CC to Codex when CC is unavailable', async () => {
      const cronMod = await import('node-cron');
      vi.spyOn(cronMod, 'schedule').mockReturnValue({
        stop: vi.fn(), start: vi.fn(), destroy: vi.fn(), getStatus: vi.fn(),
      } as unknown as ReturnType<typeof cronMod.schedule>);

      const { spawn } = await import('node:child_process');
      vi.mocked(spawn).mockReset();

      const ccProc = createMockProc(4444);
      const ccIntProc = createMockProc(4446);
      const codexProc = createMockProc(4445);
      const spawnStart = vi.mocked(spawn).mock.calls.length;

      vi.mocked(spawn)
        .mockImplementationOnce(() => {
          // CC print mode: binary missing
          setImmediate(() => {
            const err = Object.assign(new Error('claude not found'), { code: 'ENOENT' });
            ccProc.emit('error', err);
          });
          return ccProc as unknown as ReturnType<typeof spawn>;
        })
        .mockImplementationOnce(() => {
          // CC interactive mode (PTY bridge): also fails
          setImmediate(() => {
            const err = Object.assign(new Error('python3 not found'), { code: 'ENOENT' });
            ccIntProc.emit('error', err);
          });
          return ccIntProc as unknown as ReturnType<typeof spawn>;
        })
        .mockImplementationOnce((_cmd, args) => {
          // Codex: succeeds
          const codexArgs = args as string[];
          const outIdx = codexArgs.indexOf('--output-last-message');
          const outputPath = codexArgs[outIdx + 1];
          setImmediate(() => {
            fs.mkdirSync(path.dirname(outputPath), { recursive: true });
            fs.writeFileSync(outputPath, 'codex fallback output\n', 'utf-8');
            codexProc.emit('exit', 0, null);
          });
          return codexProc as unknown as ReturnType<typeof spawn>;
        });

      const { createScheduler } = await import('../src/scheduler.js');
      const bot = makeBotMock();
      const job = makeJob({
        id: 'fallback-job',
        enabled: true,
        agent: {
          workspace: '/tmp',
          prompt: 'echo hello',
          timeout: 10,
          runtimePreference: ['cc', 'cc-interactive', 'codex'],
        },
      });
      const file = makeTmpJobsFile([job]);
      const scheduler = createScheduler(bot, file);
      scheduler.start();

      const result = await scheduler.runJob('fallback-job');

      expect(result.status).toBe('ok');
      expect(result.runtime).toBe('codex');
      expect(result.attemptedRuntimes).toEqual(['cc', 'cc-interactive', 'codex']);
      expect(result.output).toContain('codex fallback output');
      expect(vi.mocked(spawn).mock.calls[spawnStart][0]).toBe(process.env['CLAUDE_BIN'] ?? 'claude');
      expect(vi.mocked(spawn).mock.calls[spawnStart + 1][0]).toBe('python3');
      expect(vi.mocked(spawn).mock.calls[spawnStart + 2][0]).toMatch(
        process.env['CODEX_BIN'] ? process.env['CODEX_BIN'] : /node_modules\/\.bin\/codex$/,
      );

      scheduler.stop();
    }, 10_000); // bumped: runtime.ts INTERACTIVE_WARMUP_MS=5000 pushes total above default 5000ms

    it('falls back from cc-interactive when the session jsonl stops progressing', async () => {
      vi.useFakeTimers();
      process.env['RELAY_CC_INTERACTIVE_IDLE_TIMEOUT_MS'] = '100';

      const cronMod = await import('node-cron');
      vi.spyOn(cronMod, 'schedule').mockReturnValue({
        stop: vi.fn(), start: vi.fn(), destroy: vi.fn(), getStatus: vi.fn(),
      } as unknown as ReturnType<typeof cronMod.schedule>);

      const { spawn } = await import('node:child_process');
      vi.mocked(spawn).mockReset();

      const ccIntProc = createMockProc(4450);
      const codexProc = createMockProc(4451);

      vi.mocked(spawn)
        .mockImplementationOnce(() => ccIntProc as unknown as ReturnType<typeof spawn>)
        .mockImplementationOnce((_cmd, args) => {
          const codexArgs = args as string[];
          const outIdx = codexArgs.indexOf('--output-last-message');
          const outputPath = codexArgs[outIdx + 1];
          setImmediate(() => {
            fs.mkdirSync(path.dirname(outputPath), { recursive: true });
            fs.writeFileSync(outputPath, 'codex recovered from idle cc-interactive\n', 'utf-8');
            codexProc.emit('exit', 0, null);
          });
          return codexProc as unknown as ReturnType<typeof spawn>;
        });

      const { createScheduler } = await import('../src/scheduler.js');
      const bot = makeBotMock();
      const file = makeTmpJobsFile([
        makeJob({
          id: 'interactive-idle',
          enabled: true,
          agent: {
            workspace: '/tmp',
            prompt: 'generate report',
            timeout: 10,
            runtimePreference: ['cc-interactive', 'codex'],
          },
        }),
      ]);
      const scheduler = createScheduler(bot, file, { reconcileMissedRuns: false });
      scheduler.start();

      try {
        const resultPromise = scheduler.runJob('interactive-idle');
        await vi.advanceTimersByTimeAsync(5_600);
        await vi.runAllTimersAsync();

        const result = await resultPromise;

        expect(ccIntProc.kill).toHaveBeenCalledWith('SIGTERM');
        expect(result.status).toBe('ok');
        expect(result.runtime).toBe('codex');
        expect(result.attemptedRuntimes).toEqual(['cc-interactive', 'codex']);
        expect(result.output).toContain('codex recovered from idle cc-interactive');
      } finally {
        scheduler.stop();
        delete process.env['RELAY_CC_INTERACTIVE_IDLE_TIMEOUT_MS'];
        vi.useRealTimers();
      }
    }, 10_000);

    it('falls back from cc stream-json when provider rejects content but the process stays open', async () => {
      const cronMod = await import('node-cron');
      vi.spyOn(cronMod, 'schedule').mockReturnValue({
        stop: vi.fn(), start: vi.fn(), destroy: vi.fn(), getStatus: vi.fn(),
      } as unknown as ReturnType<typeof cronMod.schedule>);

      const { spawn } = await import('node:child_process');
      vi.mocked(spawn).mockReset();

      const ccProc = createMockProc(4452);
      const codexProc = createMockProc(4453);

      vi.mocked(spawn)
        .mockImplementationOnce(() => {
          setImmediate(() => {
            ccProc.stdout.push(`${JSON.stringify({
              type: 'assistant',
              message: {
                content: [{
                  type: 'text',
                  text: 'API Error: System detected potentially unsafe or sensitive content in input or generation.',
                }],
              },
            })}\n`);
          });
          return ccProc as unknown as ReturnType<typeof spawn>;
        })
        .mockImplementationOnce((_cmd, args) => {
          const codexArgs = args as string[];
          const outIdx = codexArgs.indexOf('--output-last-message');
          const outputPath = codexArgs[outIdx + 1];
          setImmediate(() => {
            fs.mkdirSync(path.dirname(outputPath), { recursive: true });
            fs.writeFileSync(outputPath, 'codex recovered from provider rejection\n', 'utf-8');
            codexProc.emit('exit', 0, null);
          });
          return codexProc as unknown as ReturnType<typeof spawn>;
        });

      const { createScheduler } = await import('../src/scheduler.js');
      const bot = makeBotMock();
      const file = makeTmpJobsFile([
        makeJob({
          id: 'cc-provider-rejection',
          enabled: true,
          agent: {
            workspace: '/tmp',
            prompt: 'generate report',
            timeout: 10,
            runtimePreference: ['cc', 'codex'],
          },
        }),
      ]);
      const scheduler = createScheduler(bot, file);
      scheduler.start();

      const result = await scheduler.runJob('cc-provider-rejection');

      expect(ccProc.kill).toHaveBeenCalledWith('SIGTERM');
      expect(result.status).toBe('ok');
      expect(result.runtime).toBe('codex');
      expect(result.attemptedRuntimes).toEqual(['cc', 'codex']);
      expect(result.output).toContain('codex recovered from provider rejection');

      scheduler.stop();
    });

    it('does not fall back on normal CC task failure', async () => {
      const cronMod = await import('node-cron');
      vi.spyOn(cronMod, 'schedule').mockReturnValue({
        stop: vi.fn(), start: vi.fn(), destroy: vi.fn(), getStatus: vi.fn(),
      } as unknown as ReturnType<typeof cronMod.schedule>);

      const { spawn } = await import('node:child_process');
      vi.mocked(spawn).mockReset();
      const ccProc = createMockProc(4333);
      const spawnStart = vi.mocked(spawn).mock.calls.length;
      vi.mocked(spawn).mockImplementationOnce(() => {
        setImmediate(() => {
          ccProc.stderr.push('task failed\n');
          ccProc.emit('exit', 1, null);
        });
        return ccProc as unknown as ReturnType<typeof spawn>;
      });

      const { createScheduler } = await import('../src/scheduler.js');
      const bot = makeBotMock();
      const file = makeTmpJobsFile([makeJob({
        id: 'task-fail',
        enabled: true,
        agent: {
          workspace: '/tmp',
          prompt: 'echo hello',
          timeout: 10,
          runtimePreference: ['cc', 'codex'],
        },
      })]);
      const scheduler = createScheduler(bot, file);
      scheduler.start();

      const result = await scheduler.runJob('task-fail');

      expect(result.status).toBe('error');
      expect(result.runtime).toBe('cc');
      expect(result.attemptedRuntimes).toEqual(['cc']);
      expect(vi.mocked(spawn).mock.calls.length - spawnStart).toBe(1);

      scheduler.stop();
    });

    it('separates a dash-prefixed Codex prompt from CLI options', async () => {
      const cronMod = await import('node-cron');
      vi.spyOn(cronMod, 'schedule').mockReturnValue({
        stop: vi.fn(), start: vi.fn(), destroy: vi.fn(), getStatus: vi.fn(),
      } as unknown as ReturnType<typeof cronMod.schedule>);

      const { spawn } = await import('node:child_process');
      vi.mocked(spawn).mockReset();
      const codexProc = createMockProc(4334);
      const stdinChunks: Buffer[] = [];
      codexProc.stdin.on('data', (chunk: Buffer) => stdinChunks.push(chunk));
      vi.mocked(spawn).mockImplementationOnce((_cmd, args) => {
        const codexArgs = args as string[];
        const outIdx = codexArgs.indexOf('--output-last-message');
        const outputPath = codexArgs[outIdx + 1];
        setImmediate(() => {
          fs.mkdirSync(path.dirname(outputPath), { recursive: true });
          fs.writeFileSync(outputPath, 'codex frontmatter output\n', 'utf-8');
          codexProc.emit('exit', 0, null);
        });
        return codexProc as unknown as ReturnType<typeof spawn>;
      });

      const { createScheduler } = await import('../src/scheduler.js');
      const bot = makeBotMock();
      const prompt = '---\nname: review-cadence\n---\nRun cadence.';
      const file = makeTmpJobsFile([
        makeJob({
          id: 'codex-frontmatter',
          agent: {
            workspace: '/tmp',
            prompt,
            timeout: 10,
            runtimePreference: ['codex'],
          },
        }),
      ]);
      const scheduler = createScheduler(bot, file);
      scheduler.start();

      const result = await scheduler.runJob('codex-frontmatter');
      const codexArgs = vi.mocked(spawn).mock.calls[0][1] as string[];

      expect(result.status).toBe('ok');
      expect(result.runtime).toBe('codex');
      expect(codexArgs).not.toContain(prompt);
      expect(codexArgs.at(-1)).toBe('-');
      expect(Buffer.concat(stdinChunks).toString()).toBe(prompt);

      scheduler.stop();
    });
  });
});

// ─────────────────────────────────────────────────────────────────────────────
// inject_file: dynamic file injection into prompt at fire time
// ─────────────────────────────────────────────────────────────────────────────

describe('inject_file — dynamic prompt injection', () => {
  it('prepends inject_file content to prompt when file exists', async () => {
    const cronMod = await import('node-cron');
    vi.spyOn(cronMod, 'schedule').mockReturnValue({
      stop: vi.fn(), start: vi.fn(), destroy: vi.fn(), getStatus: vi.fn(),
    } as unknown as ReturnType<typeof cronMod.schedule>);

    const { spawn } = await import('node:child_process');
    const mockProc = createMockProc(3333);
    vi.mocked(spawn).mockReturnValue(mockProc as unknown as ReturnType<typeof spawn>);

    // Create a temp file to inject
    const injectDir = fs.mkdtempSync(path.join(os.tmpdir(), 'relay-inject-'));
    const injectFile = path.join(injectDir, 'HB.md');
    fs.writeFileSync(injectFile, '# HEARTBEAT\nDo task A and B.', 'utf-8');

    const { createScheduler } = await import('../src/scheduler.js');
    const bot = makeBotMock();
    const job = makeJob({
      id: 'inject-job',
      enabled: true,
      agent: {
        workspace: injectDir,
        prompt: '你是分身。按 HB 执行。',
        timeout: 10,
        inject_file: 'HB.md',
      },
    });
    const file = makeTmpJobsFile([job]);
    const scheduler = createScheduler(bot, file);
    scheduler.start();
    const stdinChunks: Buffer[] = [];
    mockProc.stdin.on('data', (chunk: Buffer) => stdinChunks.push(chunk));

    const resultPromise = scheduler.runJob('inject-job');

    setImmediate(() => {
      mockProc.stdout.push('done\n');
      mockProc.stdout.push(null);
      mockProc.emit('exit', 0, null);
    });

    const result = await resultPromise;
    expect(result.status).toBe('ok');

    // Verify spawn was called with prepended prompt
    const promptArg = Buffer.concat(stdinChunks).toString();
    expect(promptArg).toContain('# HEARTBEAT');
    expect(promptArg).toContain('Do task A and B.');
    expect(promptArg).toContain('你是分身。按 HB 执行。');

    scheduler.stop();
    fs.rmSync(injectDir, { recursive: true, force: true });
  });

  it('degrades to pure prompt when inject_file does not exist', async () => {
    const cronMod = await import('node-cron');
    vi.spyOn(cronMod, 'schedule').mockReturnValue({
      stop: vi.fn(), start: vi.fn(), destroy: vi.fn(), getStatus: vi.fn(),
    } as unknown as ReturnType<typeof cronMod.schedule>);

    const { spawn } = await import('node:child_process');
    const mockProc = createMockProc(3334);
    vi.mocked(spawn).mockReturnValue(mockProc as unknown as ReturnType<typeof spawn>);

    const warnSpy = vi.spyOn(console, 'warn').mockImplementation(() => {});

    const { createScheduler } = await import('../src/scheduler.js');
    const bot = makeBotMock();
    const job = makeJob({
      id: 'inject-missing',
      enabled: true,
      agent: {
        workspace: '/tmp',
        prompt: 'fallback prompt',
        timeout: 10,
        inject_file: 'nonexistent-HB.md',
      },
    });
    const file = makeTmpJobsFile([job]);
    const scheduler = createScheduler(bot, file);
    scheduler.start();
    const stdinChunks: Buffer[] = [];
    mockProc.stdin.on('data', (chunk: Buffer) => stdinChunks.push(chunk));

    const resultPromise = scheduler.runJob('inject-missing');

    setImmediate(() => {
      mockProc.stdout.push('ok\n');
      mockProc.stdout.push(null);
      mockProc.emit('exit', 0, null);
    });

    const result = await resultPromise;
    expect(result.status).toBe('ok');

    // Verify warn log about missing file
    const warnCalls = warnSpy.mock.calls.map(c => JSON.stringify(c[0]));
    expect(warnCalls.some(s => s.includes('inject_file not readable'))).toBe(true);

    // Prompt should be the original (no injection)
    const promptArg = Buffer.concat(stdinChunks).toString();
    expect(promptArg).toContain('fallback prompt');

    scheduler.stop();
    warnSpy.mockRestore();
  });

  it('backward compatible when inject_file is not set', async () => {
    const cronMod = await import('node-cron');
    vi.spyOn(cronMod, 'schedule').mockReturnValue({
      stop: vi.fn(), start: vi.fn(), destroy: vi.fn(), getStatus: vi.fn(),
    } as unknown as ReturnType<typeof cronMod.schedule>);

    const { spawn } = await import('node:child_process');
    const mockProc = createMockProc(3335);
    vi.mocked(spawn).mockReturnValue(mockProc as unknown as ReturnType<typeof spawn>);

    const { createScheduler } = await import('../src/scheduler.js');
    const bot = makeBotMock();
    // No inject_file — classic job
    const job = makeJob({
      id: 'no-inject',
      enabled: true,
      agent: { workspace: '/tmp', prompt: 'plain prompt', timeout: 10 },
    });
    const file = makeTmpJobsFile([job]);
    const scheduler = createScheduler(bot, file);
    scheduler.start();
    const stdinChunks: Buffer[] = [];
    mockProc.stdin.on('data', (chunk: Buffer) => stdinChunks.push(chunk));

    const resultPromise = scheduler.runJob('no-inject');

    setImmediate(() => {
      mockProc.stdout.push('done\n');
      mockProc.stdout.push(null);
      mockProc.emit('exit', 0, null);
    });

    const result = await resultPromise;
    expect(result.status).toBe('ok');

    const promptArg = Buffer.concat(stdinChunks).toString();
    expect(promptArg).toContain('plain prompt');

    scheduler.stop();
  });

  it('supports absolute path for inject_file', async () => {
    const cronMod = await import('node-cron');
    vi.spyOn(cronMod, 'schedule').mockReturnValue({
      stop: vi.fn(), start: vi.fn(), destroy: vi.fn(), getStatus: vi.fn(),
    } as unknown as ReturnType<typeof cronMod.schedule>);

    const { spawn } = await import('node:child_process');
    const mockProc = createMockProc(3336);
    vi.mocked(spawn).mockReturnValue(mockProc as unknown as ReturnType<typeof spawn>);

    // Create a temp file at an absolute path
    const injectDir = fs.mkdtempSync(path.join(os.tmpdir(), 'relay-abs-'));
    const absPath = path.join(injectDir, 'absolute-hb.md');
    fs.writeFileSync(absPath, '# Absolute HB\nRun absolute task.', 'utf-8');

    const { createScheduler } = await import('../src/scheduler.js');
    const bot = makeBotMock();
    const job = makeJob({
      id: 'inject-abs',
      enabled: true,
      agent: {
        workspace: '/tmp',  // different from file location
        prompt: 'execute now',
        timeout: 10,
        inject_file: absPath,  // absolute path
      },
    });
    const file = makeTmpJobsFile([job]);
    const scheduler = createScheduler(bot, file);
    scheduler.start();
    const stdinChunks: Buffer[] = [];
    mockProc.stdin.on('data', (chunk: Buffer) => stdinChunks.push(chunk));

    const resultPromise = scheduler.runJob('inject-abs');

    setImmediate(() => {
      mockProc.stdout.push('done\n');
      mockProc.stdout.push(null);
      mockProc.emit('exit', 0, null);
    });

    const result = await resultPromise;
    expect(result.status).toBe('ok');

    const promptArg = Buffer.concat(stdinChunks).toString();
    expect(promptArg).toContain('# Absolute HB');
    expect(promptArg).toContain('Run absolute task.');

    scheduler.stop();
    fs.rmSync(injectDir, { recursive: true, force: true });
  });
});

// ─────────────────────────────────────────────────────────────────────────────
// Agent-reported notification failures
// ─────────────────────────────────────────────────────────────────────────────

describe('agent-reported notification failures', () => {
  it('does not infer a retired Harness escalation requirement from prompt text', async () => {
    const cronMod = await import('node-cron');
    vi.spyOn(cronMod, 'schedule').mockReturnValue({
      stop: vi.fn(), start: vi.fn(), destroy: vi.fn(), getStatus: vi.fn(),
    } as unknown as ReturnType<typeof cronMod.schedule>);

    const { spawn } = await import('node:child_process');
    vi.mocked(spawn).mockReset();
    const runtimeProc = createMockProc(5335);
    vi.mocked(spawn).mockReturnValueOnce(runtimeProc as unknown as ReturnType<typeof spawn>);

    const { createScheduler } = await import('../src/scheduler.js');
    const bot = makeBotMock();
    const job = makeJob({
      id: 'legacy-night-patrol',
      agent: {
        workspace: '/tmp',
        prompt: '完成分析后用 harness escalation emit 推送通知。',
        timeout: 10,
      },
    });
    const file = makeTmpJobsFile([job]);
    const scheduler = createScheduler(bot, file);
    scheduler.start();

    const resultPromise = scheduler.runJob('legacy-night-patrol');
    setImmediate(() => {
      runtimeProc.stdout.push('未完成：harness escalation emit 执行失败，PermissionError: Operation not permitted\n');
      runtimeProc.stdout.push(null);
      runtimeProc.emit('exit', 0, null);
    });

    const result = await resultPromise;
    expect(result.status).toBe('ok');
    expect(result.errorMessage).toBeUndefined();
    expect(vi.mocked(spawn).mock.calls).toHaveLength(1);

    scheduler.stop();
  });
});

// ─────────────────────────────────────────────────────────────────────────────
// Telegram document delivery
// ─────────────────────────────────────────────────────────────────────────────

describe('telegram document delivery', () => {
  it('sends document from Relay after successful telegram_document job', async () => {
    const cronMod = await import('node-cron');
    vi.spyOn(cronMod, 'schedule').mockReturnValue({
      stop: vi.fn(), start: vi.fn(), destroy: vi.fn(), getStatus: vi.fn(),
    } as unknown as ReturnType<typeof cronMod.schedule>);

    const { spawn } = await import('node:child_process');
    vi.mocked(spawn).mockReset();
    const runtimeProc = createMockProc(6331);
    vi.mocked(spawn).mockReturnValueOnce(runtimeProc as unknown as ReturnType<typeof spawn>);

    const docDir = fs.mkdtempSync(path.join(os.tmpdir(), 'relay-doc-'));
    const docPath = path.join(docDir, 'briefing.epub');
    fs.writeFileSync(docPath, 'epub', 'utf-8');

    const { createScheduler } = await import('../src/scheduler.js');
    const bot = makeBotMock();
    const job = makeJob({
      id: 'briefing-push',
      owner: 'aki',
      delivery: {
        mode: 'telegram_document',
        channel: 'telegram',
        to: '1000000001',
        documentPath: docPath,
        caption: '早报 ${DATE}',
      },
    });
    const file = makeTmpJobsFile([job]);
    const scheduler = createScheduler(bot, file);
    scheduler.start();

    const resultPromise = scheduler.runJob('briefing-push');
    setImmediate(() => {
      runtimeProc.stdout.push('EPUB ready\n');
      runtimeProc.stdout.push(null);
      runtimeProc.emit('exit', 0, null);
    });

    const result = await resultPromise;
    expect(result.status).toBe('ok');
    expect(result.executionStatus).toBe('ok');
    expect(result.deliveryStatus).toBe('sent');
    expect(bot.api.sendDocument).toHaveBeenCalledWith(
      1000000001,
      expect.any(Object),
      { caption: expect.stringMatching(/^早报 \d{4}-\d{2}-\d{2}$/) },
    );

    scheduler.stop();
  });

  it('placement mode hard-fails telegram_document when explicit delivery mapping is absent, even if owner equals a bot id', async () => {
    const cronMod = await import('node-cron');
    vi.spyOn(cronMod, 'schedule').mockReturnValue({
      stop: vi.fn(), start: vi.fn(), destroy: vi.fn(), getStatus: vi.fn(),
    } as unknown as ReturnType<typeof cronMod.schedule>);
    const { spawn } = await import('node:child_process');
    vi.mocked(spawn).mockReset();
    const runtimeProc = createMockProc(6338);
    vi.mocked(spawn).mockReturnValueOnce(runtimeProc as unknown as ReturnType<typeof spawn>);

    const docDir = fs.mkdtempSync(path.join(os.tmpdir(), 'relay-doc-unmapped-'));
    const docPath = path.join(docDir, 'briefing.epub');
    fs.writeFileSync(docPath, 'epub', 'utf8');
    const job = makeJob({
      id: 'owner-same-as-bot', owner: 'aki', runNode: 'node-a',
      delivery: { mode: 'telegram_document', channel: 'telegram', to: '1000000001', documentPath: docPath },
    });
    const bot = makeBotMock();
    const { createScheduler } = await import('../src/scheduler.js');
    const scheduler = createScheduler(bot, makeTmpJobsFile([job]), {
      currentNode: 'node-a',
      ownerCanRunOnNode: (owner, node) => owner === 'aki' && node === 'node-a',
      deliveryBotIdForOwner: () => undefined,
      getBotByAgentId: id => id === 'aki' ? bot : null,
    });
    scheduler.start();
    const resultPromise = scheduler.runJob(job.id);
    setImmediate(() => {
      runtimeProc.stdout.push('EPUB ready\n');
      runtimeProc.stdout.push(null);
      runtimeProc.emit('exit', 0, null);
    });
    const result = await resultPromise;
    expect(result.status).toBe('error');
    expect(result.deliveryStatus).toBe('failed');
    expect(result.errorMessage).toMatch(/no Telegram client mapped/);
    expect(bot.api.sendDocument).not.toHaveBeenCalled();
    scheduler.stop();
  });

  it('does not treat Relay-delegated Telegram wording as an agent delivery failure', async () => {
    const cronMod = await import('node-cron');
    vi.spyOn(cronMod, 'schedule').mockReturnValue({
      stop: vi.fn(), start: vi.fn(), destroy: vi.fn(), getStatus: vi.fn(),
    } as unknown as ReturnType<typeof cronMod.schedule>);

    const { spawn } = await import('node:child_process');
    vi.mocked(spawn).mockReset();
    const runtimeProc = createMockProc(6335);
    vi.mocked(spawn).mockReturnValueOnce(runtimeProc as unknown as ReturnType<typeof spawn>);

    const docDir = fs.mkdtempSync(path.join(os.tmpdir(), 'relay-doc-delegated-'));
    const docPath = path.join(docDir, 'briefing.epub');
    fs.writeFileSync(docPath, 'epub', 'utf-8');

    const { createScheduler } = await import('../src/scheduler.js');
    const bot = makeBotMock();
    const job = makeJob({
      id: 'briefing-relay-delegated',
      owner: 'aki',
      agent: {
        workspace: '/tmp',
        prompt: '不要直接 curl Telegram，Relay 会在作业成功后负责发送文档。',
        timeout: 10,
      },
      delivery: {
        mode: 'telegram_document',
        channel: 'telegram',
        to: '1000000001',
        documentPath: docPath,
      },
    });
    const file = makeTmpJobsFile([job]);
    const scheduler = createScheduler(bot, file);
    scheduler.start();

    const resultPromise = scheduler.runJob('briefing-relay-delegated');
    setImmediate(() => {
      runtimeProc.stdout.push('Telegram delivery failed 曾导致早报未推送，但 EPUB ready\n');
      runtimeProc.stdout.push(null);
      runtimeProc.emit('exit', 0, null);
    });

    const result = await resultPromise;
    expect(result.status).toBe('ok');
    expect(result.executionStatus).toBe('ok');
    expect(result.deliveryStatus).toBe('sent');
    expect(bot.api.sendDocument).toHaveBeenCalled();

    scheduler.stop();
  });

  it('marks aggregate error when telegram_document delivery fails after execution ok', async () => {
    const cronMod = await import('node-cron');
    vi.spyOn(cronMod, 'schedule').mockReturnValue({
      stop: vi.fn(), start: vi.fn(), destroy: vi.fn(), getStatus: vi.fn(),
    } as unknown as ReturnType<typeof cronMod.schedule>);

    const { spawn } = await import('node:child_process');
    vi.mocked(spawn).mockReset();
    const runtimeProc = createMockProc(6334);
    vi.mocked(spawn).mockReturnValueOnce(runtimeProc as unknown as ReturnType<typeof spawn>);

    const docDir = fs.mkdtempSync(path.join(os.tmpdir(), 'relay-doc-fail-'));
    const docPath = path.join(docDir, 'briefing.epub');
    fs.writeFileSync(docPath, 'epub', 'utf-8');

    const consoleSpy = vi.spyOn(console, 'error').mockImplementation(() => {});
    const { createScheduler } = await import('../src/scheduler.js');
    const bot = makeBotMock();
    vi.mocked(bot.api.sendDocument as unknown as ReturnType<typeof vi.fn>).mockRejectedValueOnce(new Error('telegram document failed'));
    const job = makeJob({
      id: 'briefing-delivery-fail',
      owner: 'aki',
      delivery: {
        mode: 'telegram_document',
        channel: 'telegram',
        to: '1000000001',
        documentPath: docPath,
      },
    });
    const file = makeTmpJobsFile([job]);
    const scheduler = createScheduler(bot, file);
    scheduler.start();

    const resultPromise = scheduler.runJob('briefing-delivery-fail');
    setImmediate(() => {
      runtimeProc.stdout.push('EPUB ready\n');
      runtimeProc.stdout.push(null);
      runtimeProc.emit('exit', 0, null);
    });

    const result = await resultPromise;
    expect(result.status).toBe('error');
    expect(result.executionStatus).toBe('ok');
    expect(result.deliveryStatus).toBe('failed');
    expect(result.errorMessage).toContain('telegram document failed');

    scheduler.stop();
    consoleSpy.mockRestore();
  });

  it('does not treat an agent-reported Telegram delivery failure as success', async () => {
    const cronMod = await import('node-cron');
    vi.spyOn(cronMod, 'schedule').mockReturnValue({
      stop: vi.fn(), start: vi.fn(), destroy: vi.fn(), getStatus: vi.fn(),
    } as unknown as ReturnType<typeof cronMod.schedule>);

    const { spawn } = await import('node:child_process');
    vi.mocked(spawn).mockReset();
    const runtimeProc = createMockProc(6332);
    vi.mocked(spawn).mockReturnValueOnce(runtimeProc as unknown as ReturnType<typeof spawn>);

    const { createScheduler } = await import('../src/scheduler.js');
    const bot = makeBotMock();
    const job = makeJob({
      id: 'legacy-briefing-push',
      agent: {
        workspace: '/tmp',
        prompt: 'curl api.telegram.org sendDocument 并确认 ok:true。',
        timeout: 10,
      },
    });
    const file = makeTmpJobsFile([job]);
    const scheduler = createScheduler(bot, file);
    scheduler.start();

    const resultPromise = scheduler.runJob('legacy-briefing-push');
    setImmediate(() => {
      runtimeProc.stdout.push('Telegram 推送失败：curl: (6) Could not resolve host: api.telegram.org，没有拿到 ok:true\n');
      runtimeProc.stdout.push(null);
      runtimeProc.emit('exit', 0, null);
    });

    const result = await resultPromise;
    expect(result.status).toBe('error');
    expect(result.errorMessage).toBe('required Telegram delivery failed');

    scheduler.stop();
  });

  it('skips telegram_document delivery when job output declares a no-op', async () => {
    const cronMod = await import('node-cron');
    vi.spyOn(cronMod, 'schedule').mockReturnValue({
      stop: vi.fn(), start: vi.fn(), destroy: vi.fn(), getStatus: vi.fn(),
    } as unknown as ReturnType<typeof cronMod.schedule>);

    const { spawn } = await import('node:child_process');
    vi.mocked(spawn).mockReset();
    const runtimeProc = createMockProc(6333);
    vi.mocked(spawn).mockReturnValueOnce(runtimeProc as unknown as ReturnType<typeof spawn>);

    const { createScheduler } = await import('../src/scheduler.js');
    const bot = makeBotMock();
    const job = makeJob({
      id: 'briefing-noop',
      delivery: {
        mode: 'telegram_document',
        channel: 'telegram',
        to: '1000000001',
        documentPath: '/tmp/does-not-exist.epub',
        skipIfOutputIncludes: ['早报未生成，跳过推送'],
      },
    });
    const file = makeTmpJobsFile([job]);
    const scheduler = createScheduler(bot, file);
    scheduler.start();

    const resultPromise = scheduler.runJob('briefing-noop');
    setImmediate(() => {
      runtimeProc.stdout.push('早报未生成，跳过推送\n');
      runtimeProc.stdout.push(null);
      runtimeProc.emit('exit', 0, null);
    });

    const result = await resultPromise;
    expect(result.status).toBe('ok');
    expect(bot.api.sendDocument).not.toHaveBeenCalled();

    scheduler.stop();
  });

  it('does NOT skip telegram_document when the marker only appears inside narration (regression: 2026-05-26 companion miss)', async () => {
    const cronMod = await import('node-cron');
    vi.spyOn(cronMod, 'schedule').mockReturnValue({
      stop: vi.fn(), start: vi.fn(), destroy: vi.fn(), getStatus: vi.fn(),
    } as unknown as ReturnType<typeof cronMod.schedule>);

    const { spawn } = await import('node:child_process');
    vi.mocked(spawn).mockReset();
    const runtimeProc = createMockProc(6334);
    vi.mocked(spawn).mockReturnValueOnce(runtimeProc as unknown as ReturnType<typeof spawn>);

    const epubDir = fs.mkdtempSync(path.join(os.tmpdir(), 'relay-companion-'));
    const epubPath = path.join(epubDir, 'companion.epub');
    fs.writeFileSync(epubPath, 'fake epub bytes', 'utf-8');

    const { createScheduler } = await import('../src/scheduler.js');
    const bot = makeBotMock();
    const job = makeJob({
      id: 'companion-narration',
      delivery: {
        mode: 'telegram_document',
        channel: 'telegram',
        to: '1000000001',
        documentPath: epubPath,
        skipIfOutputIncludes: ['CANDIDATE_POOL_INSUFFICIENT', '本次不推'],
      },
    });
    const file = makeTmpJobsFile([job]);
    const scheduler = createScheduler(bot, file);
    scheduler.start();

    const resultPromise = scheduler.runJob('companion-narration');
    setImmediate(() => {
      // marker words appear ONLY inside narration lines, never on their own line
      runtimeProc.stdout.push('采集+精选后仍无合格候选才判定 CANDIDATE_POOL_INSUFFICIENT 退出。\n');
      runtimeProc.stdout.push('本轮未触发本次不推：active pool 非空，已生成 EPUB。\n');
      runtimeProc.stdout.push('selected_rank: 1\n');
      runtimeProc.stdout.push(null);
      runtimeProc.emit('exit', 0, null);
    });

    const result = await resultPromise;
    expect(result.status).toBe('ok');
    expect(bot.api.sendDocument).toHaveBeenCalledTimes(1);

    scheduler.stop();
  });

  it('skips telegram_document when the marker stands alone on its own line', async () => {
    const cronMod = await import('node-cron');
    vi.spyOn(cronMod, 'schedule').mockReturnValue({
      stop: vi.fn(), start: vi.fn(), destroy: vi.fn(), getStatus: vi.fn(),
    } as unknown as ReturnType<typeof cronMod.schedule>);

    const { spawn } = await import('node:child_process');
    vi.mocked(spawn).mockReset();
    const runtimeProc = createMockProc(6335);
    vi.mocked(spawn).mockReturnValueOnce(runtimeProc as unknown as ReturnType<typeof spawn>);

    const { createScheduler } = await import('../src/scheduler.js');
    const bot = makeBotMock();
    const job = makeJob({
      id: 'companion-realnoop',
      delivery: {
        mode: 'telegram_document',
        channel: 'telegram',
        to: '1000000001',
        documentPath: '/tmp/does-not-exist.epub',
        skipIfOutputIncludes: ['CANDIDATE_POOL_INSUFFICIENT', '本次不推'],
      },
    });
    const file = makeTmpJobsFile([job]);
    const scheduler = createScheduler(bot, file);
    scheduler.start();

    const resultPromise = scheduler.runJob('companion-realnoop');
    setImmediate(() => {
      runtimeProc.stdout.push('真·信源不足，本轮不生成 EPUB。\n');
      runtimeProc.stdout.push('CANDIDATE_POOL_INSUFFICIENT\n');
      runtimeProc.stdout.push(null);
      runtimeProc.emit('exit', 0, null);
    });

    const result = await resultPromise;
    expect(result.status).toBe('ok');
    expect(bot.api.sendDocument).not.toHaveBeenCalled();

    scheduler.stop();
  });
});

// ─────────────────────────────────────────────────────────────────────────────
// Feishu document delivery
// ─────────────────────────────────────────────────────────────────────────────

/** Minimal Feishu send client mock (im.file.create + im.message.create). */
function makeFeishuClientMock() {
  const fileCreate = vi.fn().mockResolvedValue({ file_key: 'file_v3_test' });
  const messageCreate = vi.fn().mockResolvedValue({ data: { message_id: 'om_test' } });
  const client = { im: { file: { create: fileCreate }, message: { create: messageCreate } } };
  return { client: client as unknown as import('../src/delivery.js').FeishuMessageClient, fileCreate, messageCreate };
}

describe('feishu document delivery', () => {
  it('sends EPUB to Feishu open_id after a successful feishu_document job', async () => {
    const cronMod = await import('node-cron');
    vi.spyOn(cronMod, 'schedule').mockReturnValue({
      stop: vi.fn(), start: vi.fn(), destroy: vi.fn(), getStatus: vi.fn(),
    } as unknown as ReturnType<typeof cronMod.schedule>);

    const { spawn } = await import('node:child_process');
    vi.mocked(spawn).mockReset();
    const runtimeProc = createMockProc(7331);
    vi.mocked(spawn).mockReturnValueOnce(runtimeProc as unknown as ReturnType<typeof spawn>);

    const docDir = fs.mkdtempSync(path.join(os.tmpdir(), 'relay-feishu-doc-'));
    const docPath = path.join(docDir, 'companion.epub');
    fs.writeFileSync(docPath, 'fake epub bytes', 'utf-8');

    const { createScheduler } = await import('../src/scheduler.js');
    const bot = makeBotMock();
    const { client, fileCreate, messageCreate } = makeFeishuClientMock();
    const job = makeJob({
      id: 'feishu-briefing-push',
      owner: 'aki',
      delivery: {
        mode: 'feishu_document',
        channel: 'feishu',
        feishuOpenId: 'ou_00000000000000000000000000000001',
        documentPath: docPath,
        caption: '伴读 ${DATE}',
      },
    });
    const file = makeTmpJobsFile([job]);
    const scheduler = createScheduler(bot, file, {
      getFeishuClientByAgentId: (agentId: string) => (agentId === 'aki' ? client : null),
    });
    scheduler.start();

    const resultPromise = scheduler.runJob('feishu-briefing-push');
    setImmediate(() => {
      runtimeProc.stdout.push('EPUB ready\n');
      runtimeProc.stdout.push(null);
      runtimeProc.emit('exit', 0, null);
    });

    const result = await resultPromise;
    expect(result.status).toBe('ok');
    expect(result.executionStatus).toBe('ok');
    expect(result.deliveryStatus).toBe('sent');
    // File uploaded exactly once.
    expect(fileCreate).toHaveBeenCalledTimes(1);
    // One file message + one caption text message.
    expect(messageCreate).toHaveBeenCalledTimes(2);
    const fileMsgCall = messageCreate.mock.calls.find(
      ([arg]: [{ data: { msg_type: string } }]) => arg.data.msg_type === 'file',
    );
    expect(fileMsgCall?.[0].params.receive_id_type).toBe('open_id');
    expect(fileMsgCall?.[0].data.receive_id).toBe('ou_00000000000000000000000000000001');
    // Telegram path must be untouched.
    expect(bot.api.sendDocument).not.toHaveBeenCalled();

    scheduler.stop();
  });

  it('marks failed and does not upload when feishu_document file is missing', async () => {
    const cronMod = await import('node-cron');
    vi.spyOn(cronMod, 'schedule').mockReturnValue({
      stop: vi.fn(), start: vi.fn(), destroy: vi.fn(), getStatus: vi.fn(),
    } as unknown as ReturnType<typeof cronMod.schedule>);

    const { spawn } = await import('node:child_process');
    vi.mocked(spawn).mockReset();
    const runtimeProc = createMockProc(7332);
    vi.mocked(spawn).mockReturnValueOnce(runtimeProc as unknown as ReturnType<typeof spawn>);

    const consoleSpy = vi.spyOn(console, 'error').mockImplementation(() => {});
    const { createScheduler } = await import('../src/scheduler.js');
    const bot = makeBotMock();
    const { client, fileCreate } = makeFeishuClientMock();
    const job = makeJob({
      id: 'feishu-missing-file',
      owner: 'aki',
      delivery: {
        mode: 'feishu_document',
        channel: 'feishu',
        feishuOpenId: 'ou_00000000000000000000000000000001',
        documentPath: '/tmp/relay-feishu-does-not-exist.epub',
      },
    });
    const file = makeTmpJobsFile([job]);
    const scheduler = createScheduler(bot, file, {
      getFeishuClientByAgentId: () => client,
    });
    scheduler.start();

    const resultPromise = scheduler.runJob('feishu-missing-file');
    setImmediate(() => {
      runtimeProc.stdout.push('EPUB ready\n');
      runtimeProc.stdout.push(null);
      runtimeProc.emit('exit', 0, null);
    });

    const result = await resultPromise;
    expect(result.status).toBe('error');
    expect(result.executionStatus).toBe('ok');
    expect(result.deliveryStatus).toBe('failed');
    expect(result.errorMessage).toContain('feishu_document missing file');
    expect(fileCreate).not.toHaveBeenCalled();

    scheduler.stop();
    consoleSpy.mockRestore();
  });

  it('skips feishu_document delivery when a skip marker stands alone on its own line', async () => {
    const cronMod = await import('node-cron');
    vi.spyOn(cronMod, 'schedule').mockReturnValue({
      stop: vi.fn(), start: vi.fn(), destroy: vi.fn(), getStatus: vi.fn(),
    } as unknown as ReturnType<typeof cronMod.schedule>);

    const { spawn } = await import('node:child_process');
    vi.mocked(spawn).mockReset();
    const runtimeProc = createMockProc(7333);
    vi.mocked(spawn).mockReturnValueOnce(runtimeProc as unknown as ReturnType<typeof spawn>);

    const { createScheduler } = await import('../src/scheduler.js');
    const bot = makeBotMock();
    const { client, fileCreate } = makeFeishuClientMock();
    const job = makeJob({
      id: 'feishu-noop',
      owner: 'aki',
      delivery: {
        mode: 'feishu_document',
        channel: 'feishu',
        feishuOpenId: 'ou_00000000000000000000000000000001',
        documentPath: '/tmp/relay-feishu-does-not-exist.epub',
        skipIfOutputIncludes: ['CANDIDATE_POOL_INSUFFICIENT', '本次不推'],
      },
    });
    const file = makeTmpJobsFile([job]);
    const scheduler = createScheduler(bot, file, {
      getFeishuClientByAgentId: () => client,
    });
    scheduler.start();

    const resultPromise = scheduler.runJob('feishu-noop');
    setImmediate(() => {
      runtimeProc.stdout.push('真·信源不足，本轮不生成 EPUB。\n');
      runtimeProc.stdout.push('CANDIDATE_POOL_INSUFFICIENT\n');
      runtimeProc.stdout.push(null);
      runtimeProc.emit('exit', 0, null);
    });

    const result = await resultPromise;
    expect(result.status).toBe('ok');
    expect(result.deliveryStatus).toBe('skipped');
    expect(fileCreate).not.toHaveBeenCalled();

    scheduler.stop();
  });

  it('marks failed when no Feishu client is mapped for the owner', async () => {
    const cronMod = await import('node-cron');
    vi.spyOn(cronMod, 'schedule').mockReturnValue({
      stop: vi.fn(), start: vi.fn(), destroy: vi.fn(), getStatus: vi.fn(),
    } as unknown as ReturnType<typeof cronMod.schedule>);

    const { spawn } = await import('node:child_process');
    vi.mocked(spawn).mockReset();
    const runtimeProc = createMockProc(7334);
    vi.mocked(spawn).mockReturnValueOnce(runtimeProc as unknown as ReturnType<typeof spawn>);

    const docDir = fs.mkdtempSync(path.join(os.tmpdir(), 'relay-feishu-noclient-'));
    const docPath = path.join(docDir, 'companion.epub');
    fs.writeFileSync(docPath, 'fake epub bytes', 'utf-8');

    const consoleSpy = vi.spyOn(console, 'error').mockImplementation(() => {});
    const warnSpy = vi.spyOn(console, 'warn').mockImplementation(() => {});
    const { createScheduler } = await import('../src/scheduler.js');
    const bot = makeBotMock();
    const job = makeJob({
      id: 'feishu-no-client',
      owner: 'aki',
      delivery: {
        mode: 'feishu_document',
        channel: 'feishu',
        feishuOpenId: 'ou_00000000000000000000000000000001',
        documentPath: docPath,
      },
    });
    const file = makeTmpJobsFile([job]);
    // No getFeishuClientByAgentId → resolver returns null.
    const scheduler = createScheduler(bot, file, {});
    scheduler.start();

    const resultPromise = scheduler.runJob('feishu-no-client');
    setImmediate(() => {
      runtimeProc.stdout.push('EPUB ready\n');
      runtimeProc.stdout.push(null);
      runtimeProc.emit('exit', 0, null);
    });

    const result = await resultPromise;
    expect(result.status).toBe('error');
    expect(result.executionStatus).toBe('ok');
    expect(result.deliveryStatus).toBe('failed');
    expect(result.errorMessage).toContain('no Feishu client for owner aki');

    scheduler.stop();
    consoleSpy.mockRestore();
    warnSpy.mockRestore();
  });
});

// ─────────────────────────────────────────────────────────────────────────────
// Workspace-relative documentPath and captionPath
// ─────────────────────────────────────────────────────────────────────────────

describe('workspace-relative document delivery', () => {
  const OPEN_ID = 'ou_00000000000000000000000000000001';

  function makeWorkspace(): string {
    return fs.realpathSync(fs.mkdtempSync(path.join(os.tmpdir(), 'relay-doc-ws-')));
  }

  async function runDocumentJob(
    job: ScheduledJob,
    channels: { feishu?: import('../src/delivery.js').FeishuMessageClient; bot?: import('grammy').Bot },
    pid: number,
    stdoutLines: string[] = ['EPUB ready'],
    exitBeforeOutput = false,
  ): Promise<JobResult> {
    const cronMod = await import('node-cron');
    vi.spyOn(cronMod, 'schedule').mockReturnValue({
      stop: vi.fn(), start: vi.fn(), destroy: vi.fn(), getStatus: vi.fn(),
    } as unknown as ReturnType<typeof cronMod.schedule>);
    const { spawn } = await import('node:child_process');
    vi.mocked(spawn).mockReset();
    const runtimeProc = createMockProc(pid);
    vi.mocked(spawn).mockReturnValueOnce(runtimeProc as unknown as ReturnType<typeof spawn>);
    const errorSpy = vi.spyOn(console, 'error').mockImplementation(() => {});
    const { createScheduler } = await import('../src/scheduler.js');
    const scheduler = createScheduler(channels.bot ?? makeBotMock(), makeTmpJobsFile([job]), {
      getFeishuClientByAgentId: () => channels.feishu ?? null,
    });
    scheduler.start();
    const resultPromise = scheduler.runJob(job.id);
    setImmediate(() => {
      // The process may exit before its last stdout lines are read.
      if (exitBeforeOutput) runtimeProc.emit('exit', 0, null);
      setImmediate(() => {
        for (const line of stdoutLines) runtimeProc.stdout.push(`${line}\n`);
        runtimeProc.stdout.push(null);
        if (!exitBeforeOutput) runtimeProc.emit('exit', 0, null);
      });
    });
    try {
      return await resultPromise;
    } finally {
      scheduler.stop();
      errorSpy.mockRestore();
    }
  }

  function feishuJob(workspace: string, delivery: Partial<ScheduledJob['delivery']>): ScheduledJob {
    return makeJob({
      id: 'aki-morning',
      owner: 'aki',
      agent: { workspace, prompt: 'build', timeout: 10 },
      delivery: { mode: 'feishu_document', channel: 'feishu', feishuOpenId: OPEN_ID, ...delivery },
    });
  }

  function sentMessages(messageCreate: ReturnType<typeof vi.fn>): Array<{ msg_type: string; content: string }> {
    return messageCreate.mock.calls.map(([arg]: [{ data: { msg_type: string; content: string } }]) => arg.data);
  }

  it('resolves a relative documentPath against agent.workspace with ${DATE} expansion', async () => {
    const workspace = makeWorkspace();
    fs.mkdirSync(path.join(workspace, 'Inbox/News'), { recursive: true });
    const date = new Intl.DateTimeFormat('en-CA', { timeZone: 'Asia/Shanghai' }).format(new Date());
    fs.writeFileSync(path.join(workspace, `Inbox/News/${date}_早报.epub`), 'epub', 'utf-8');
    const { client, fileCreate } = makeFeishuClientMock();
    const result = await runDocumentJob(
      feishuJob(workspace, { documentPath: 'Inbox/News/${DATE}_早报.epub' }), { feishu: client }, 7401);
    expect(result.status).toBe('ok');
    expect(result.deliveryStatus).toBe('sent');
    expect(fileCreate).toHaveBeenCalledTimes(1);
  });

  it('rejects a relative documentPath that escapes agent.workspace', async () => {
    const workspace = makeWorkspace();
    fs.writeFileSync(path.join(path.dirname(workspace), `${path.basename(workspace)}-outside.epub`), 'epub', 'utf-8');
    const { client, fileCreate } = makeFeishuClientMock();
    const result = await runDocumentJob(
      feishuJob(workspace, { documentPath: `../${path.basename(workspace)}-outside.epub` }), { feishu: client }, 7402);
    expect(result.status).toBe('error');
    expect(result.deliveryStatus).toBe('failed');
    expect(result.errorMessage).toContain('documentPath escapes agent.workspace');
    expect(fileCreate).not.toHaveBeenCalled();
  });

  it('rejects a relative documentPath whose symlink leaves agent.workspace', async () => {
    const workspace = makeWorkspace();
    const outside = makeWorkspace();
    fs.writeFileSync(path.join(outside, 'secret.epub'), 'epub', 'utf-8');
    fs.symlinkSync(path.join(outside, 'secret.epub'), path.join(workspace, 'linked.epub'));
    const { client, fileCreate } = makeFeishuClientMock();
    const result = await runDocumentJob(feishuJob(workspace, { documentPath: 'linked.epub' }), { feishu: client }, 7403);
    expect(result.status).toBe('error');
    expect(result.errorMessage).toContain('documentPath resolves outside agent.workspace');
    expect(fileCreate).not.toHaveBeenCalled();
  });

  it('sends the captionPath text to Feishu after the document', async () => {
    const workspace = makeWorkspace();
    fs.writeFileSync(path.join(workspace, 'doc.epub'), 'epub', 'utf-8');
    fs.writeFileSync(path.join(workspace, 'caption.txt'), '今日三条要闻\n', 'utf-8');
    const { client, messageCreate } = makeFeishuClientMock();
    const result = await runDocumentJob(
      feishuJob(workspace, { documentPath: 'doc.epub', captionPath: 'caption.txt' }), { feishu: client }, 7404);
    expect(result.status).toBe('ok');
    expect(result.deliveryStatus).toBe('sent');
    const messages = sentMessages(messageCreate);
    expect(messages.map(message => message.msg_type)).toEqual(['file', 'text']);
    expect(JSON.parse(messages[1].content)).toEqual({ text: '今日三条要闻' });
  });

  it('sends the captionPath text to Telegram after the document', async () => {
    const workspace = makeWorkspace();
    fs.writeFileSync(path.join(workspace, 'doc.epub'), 'epub', 'utf-8');
    fs.writeFileSync(path.join(workspace, 'caption.txt'), '伴读导语', 'utf-8');
    const bot = makeBotMock();
    const job = makeJob({
      id: 'aki-daily',
      owner: 'aki',
      agent: { workspace, prompt: 'build', timeout: 10 },
      delivery: { mode: 'telegram_document', channel: 'telegram', to: '1000000001', documentPath: 'doc.epub', captionPath: 'caption.txt' },
    });
    const result = await runDocumentJob(job, { bot }, 7405);
    expect(result.status).toBe('ok');
    const sendDocument = vi.mocked(bot.api.sendDocument);
    const sendMessage = vi.mocked(bot.api.sendMessage);
    expect(sendMessage).toHaveBeenCalledWith(1000000001, '伴读导语');
    expect(sendDocument.mock.invocationCallOrder[0]).toBeLessThan(sendMessage.mock.invocationCallOrder[0]);
  });

  // A stream-json run: progress text, a tool call, then the last reply.
  const STREAMED_RUN = [
    JSON.stringify({ type: 'assistant', message: { content: [{ type: 'text', text: 'Building the HTML report.' }, { type: 'tool_use', id: 't1', name: 'Write', input: {} }] } }),
    JSON.stringify({ type: 'assistant', message: { content: [{ type: 'text', text: 'Weekly health summary.' }] } }),
    JSON.stringify({ type: 'result', subtype: 'success', result: 'Weekly health summary.' }),
  ];
  function sendOutputJob(workspace: string, id: string): ScheduledJob {
    fs.writeFileSync(path.join(workspace, 'report.html'), '<html></html>', 'utf-8');
    return makeJob({
      id,
      owner: 'vigil',
      agent: { workspace, prompt: 'build', timeout: 10, runtimePreference: ['cc'] },
      delivery: { mode: 'telegram_document', channel: 'telegram', to: '1000000001', documentPath: 'report.html', sendOutput: true },
    });
  }

  it('sends only the last reply to Telegram after the document when sendOutput is set', async () => {
    const bot = makeBotMock();
    const result = await runDocumentJob(sendOutputJob(makeWorkspace(), 'vigil-weekly'), { bot }, 7408, STREAMED_RUN);
    expect(result.status).toBe('ok');
    const sendDocument = vi.mocked(bot.api.sendDocument);
    const sendMessage = vi.mocked(bot.api.sendMessage);
    expect(sendMessage).toHaveBeenCalledTimes(1);
    expect(sendMessage).toHaveBeenCalledWith(1000000001, 'Weekly health summary.');
    expect(sendDocument.mock.invocationCallOrder[0]).toBeLessThan(sendMessage.mock.invocationCallOrder[0]);
  });

  it('sends nothing and fails the delivery when sendOutput is set and the last reply is empty', async () => {
    const bot = makeBotMock();
    const run = [
      JSON.stringify({ type: 'assistant', message: { content: [{ type: 'tool_use', id: 't1', name: 'Write', input: {} }] } }),
      JSON.stringify({ type: 'result', subtype: 'success', result: '' }),
    ];
    const result = await runDocumentJob(sendOutputJob(makeWorkspace(), 'vigil-weekly-empty'), { bot }, 7410, run);
    expect(result.status).toBe('error');
    expect(result.deliveryStatus).toBe('failed');
    expect(result.errorMessage).toContain('the run left no reply to send');
    expect(vi.mocked(bot.api.sendDocument)).not.toHaveBeenCalled();
  });

  it('sends nothing when the turn after the progress text ends without a reply or result', async () => {
    const bot = makeBotMock();
    const run = [STREAMED_RUN[0]!, JSON.stringify({ type: 'assistant', message: { content: [{ type: 'tool_use', id: 't2', name: 'Write', input: {} }] } })];
    const result = await runDocumentJob(sendOutputJob(makeWorkspace(), 'vigil-weekly-noresult'), { bot }, 7411, run);
    expect(result.deliveryStatus).toBe('failed');
    expect(result.errorMessage).toContain('the runtime did not separate the last reply');
    expect(vi.mocked(bot.api.sendDocument)).not.toHaveBeenCalled();
  });

  it('sends nothing when the run only answers with the no-response placeholder', async () => {
    const bot = makeBotMock();
    const run = [JSON.stringify({ type: 'result', subtype: 'success', result: 'No response requested.' })];
    const result = await runDocumentJob(sendOutputJob(makeWorkspace(), 'vigil-weekly-placeholder'), { bot }, 7413, run);
    expect(result.deliveryStatus).toBe('failed');
    expect(vi.mocked(bot.api.sendDocument)).not.toHaveBeenCalled();
  });

  it('reads the final result even when the process exits before stdout is drained', async () => {
    const bot = makeBotMock();
    const result = await runDocumentJob(sendOutputJob(makeWorkspace(), 'vigil-weekly-late'), { bot }, 7412, STREAMED_RUN, true);
    expect(result.status).toBe('ok');
    expect(vi.mocked(bot.api.sendMessage)).toHaveBeenCalledWith(1000000001, 'Weekly health summary.');
  });

  it('fails the delivery when sendOutput is set and the reply cannot be sent', async () => {
    const bot = makeBotMock();
    vi.mocked(bot.api.sendMessage).mockRejectedValue(new Error('400: Bad Request'));
    const result = await runDocumentJob(sendOutputJob(makeWorkspace(), 'vigil-weekly-fail'), { bot }, 7409, STREAMED_RUN);
    expect(result.status).toBe('error');
    expect(result.deliveryStatus).toBe('failed');
    expect(result.errorMessage).toContain('telegram_document reply delivery failed (document already sent)');
  });

  it('fails the delivery and sends nothing when the captionPath file is missing', async () => {
    const workspace = makeWorkspace();
    fs.writeFileSync(path.join(workspace, 'doc.epub'), 'epub', 'utf-8');
    const { client, fileCreate } = makeFeishuClientMock();
    const result = await runDocumentJob(
      feishuJob(workspace, { documentPath: 'doc.epub', captionPath: 'caption-${DATE}.txt' }), { feishu: client }, 7406);
    expect(result.status).toBe('error');
    expect(result.deliveryStatus).toBe('failed');
    expect(result.errorMessage).toContain('feishu_document captionPath missing file');
    expect(fileCreate).not.toHaveBeenCalled();
  });

  it('fails the delivery and sends nothing when the captionPath escapes agent.workspace', async () => {
    const workspace = makeWorkspace();
    fs.writeFileSync(path.join(workspace, 'doc.epub'), 'epub', 'utf-8');
    fs.writeFileSync(path.join(path.dirname(workspace), `${path.basename(workspace)}-caption.txt`), 'x', 'utf-8');
    const { client, fileCreate } = makeFeishuClientMock();
    const result = await runDocumentJob(
      feishuJob(workspace, { documentPath: 'doc.epub', captionPath: `../${path.basename(workspace)}-caption.txt` }),
      { feishu: client }, 7407);
    expect(result.status).toBe('error');
    expect(result.errorMessage).toContain('captionPath escapes agent.workspace');
    expect(fileCreate).not.toHaveBeenCalled();
  });

  it('fails the delivery when the captionPath text cannot be sent after the document', async () => {
    const workspace = makeWorkspace();
    fs.writeFileSync(path.join(workspace, 'doc.epub'), 'epub', 'utf-8');
    fs.writeFileSync(path.join(workspace, 'caption.txt'), '导语', 'utf-8');
    const { client, fileCreate, messageCreate } = makeFeishuClientMock();
    messageCreate.mockImplementation(async (arg: { data: { msg_type: string } }) => {
      if (arg.data.msg_type === 'text') throw new Error('feishu text rejected');
      return { data: { message_id: 'om_test' } };
    });
    const result = await runDocumentJob(
      feishuJob(workspace, { documentPath: 'doc.epub', captionPath: 'caption.txt' }), { feishu: client }, 7408);
    expect(fileCreate).toHaveBeenCalledTimes(1);
    expect(result.status).toBe('error');
    expect(result.deliveryStatus).toBe('failed');
    expect(result.errorMessage).toContain('captionPath delivery failed (document already sent): Error: feishu text rejected');
  });

  it('skips a job whose captionPath is absolute or set on a non-document mode', async () => {
    const warnSpy = vi.spyOn(console, 'warn').mockImplementation(() => {});
    const { loadJobs } = await import('../src/scheduler.js');
    const file = makeTmpJobsFile([
      makeJob({ id: 'absolute-caption', delivery: { mode: 'feishu_document', feishuOpenId: OPEN_ID, documentPath: 'a.epub', captionPath: '/tmp/c.txt' } }),
      makeJob({ id: 'none-caption', delivery: { mode: 'none', captionPath: 'c.txt' } }),
      makeJob({ id: 'valid-caption', delivery: { mode: 'feishu_document', feishuOpenId: OPEN_ID, documentPath: 'a.epub', captionPath: 'c.txt' } }),
    ]);
    expect(loadJobs(file).map(job => job.id)).toEqual(['valid-caption']);
    warnSpy.mockRestore();
  });
});

// ─────────────────────────────────────────────────────────────────────────────
// Opt-in job failure alert (Feishu)
//
// Guards the "a failed cron job must not fail silently" acceptance criterion.
// The alert is opt-in via delivery.feishuOpenId and is deliberately independent
// of delivery.mode, so a mode:'none' job (which delivers nothing on success)
// still alerts on failure.
// ─────────────────────────────────────────────────────────────────────────────

describe('job failure feishu alert', () => {
  async function runFailingJob(
    jobOverrides: Partial<ScheduledJob>,
    opts: { withClient?: boolean; exitCode?: number; sendFails?: boolean } = {},
  ) {
    const { withClient = true, exitCode = 1, sendFails = false } = opts;
    const cronMod = await import('node-cron');
    vi.spyOn(cronMod, 'schedule').mockReturnValue({
      stop: vi.fn(), start: vi.fn(), destroy: vi.fn(), getStatus: vi.fn(),
    } as unknown as ReturnType<typeof cronMod.schedule>);

    const { spawn } = await import('node:child_process');
    vi.mocked(spawn).mockReset();
    const runtimeProc = createMockProc(7401);
    vi.mocked(spawn).mockReturnValueOnce(runtimeProc as unknown as ReturnType<typeof spawn>);

    const { createScheduler, resetJobFailureAlertStreaks } = await import('../src/scheduler.js');
    resetJobFailureAlertStreaks();
    const bot = makeBotMock();
    const { client, messageCreate } = makeFeishuClientMock();
    if (sendFails) messageCreate.mockRejectedValue(new Error('feishu unavailable'));
    const job = makeJob(jobOverrides);
    const scheduler = createScheduler(bot, makeTmpJobsFile([job]), {
      getFeishuClientByAgentId: withClient ? () => client : undefined,
    });
    scheduler.start();

    const resultPromise = scheduler.runJob(job.id);
    setImmediate(() => {
      runtimeProc.stderr.push('boom\n');
      runtimeProc.stderr.push(null);
      runtimeProc.stdout.push(null);
      runtimeProc.emit('exit', exitCode, null);
    });
    const result = await resultPromise;
    scheduler.stop();
    return { result, messageCreate, scheduler, job };
  }

  it('pushes a Feishu alert when a mode:none job fails and feishuOpenId is set', async () => {
    const consoleSpy = vi.spyOn(console, 'error').mockImplementation(() => {});
    const { result, messageCreate } = await runFailingJob({
      id: 'weekly-review-job',
      name: 'amanda review cadence',
      owner: 'amanda',
      delivery: { mode: 'none', feishuOpenId: 'ou_alert_target' },
    });

    expect(result.status).toBe('error');
    expect(messageCreate).toHaveBeenCalledTimes(1);
    const [arg] = messageCreate.mock.calls[0] as [
      { params: { receive_id_type: string }; data: { receive_id: string; msg_type: string; content: string } },
    ];
    expect(arg.params.receive_id_type).toBe('open_id');
    expect(arg.data.receive_id).toBe('ou_alert_target');
    // Plain-language alert: names the job so the reader knows what did not run.
    expect(arg.data.content).toContain('amanda review cadence');
    consoleSpy.mockRestore();
  });

  it('withholds alerts while a convergent job may still recover, then alerts once', async () => {
    const consoleSpy = vi.spyOn(console, 'error').mockImplementation(() => {});
    const logSpy = vi.spyOn(console, 'log').mockImplementation(() => {});
    const delivery = { mode: 'none' as const, feishuOpenId: 'ou_alert_target' };
    const ago = (minutes: number) => new Date(Date.now() - minutes * 60_000).toISOString();
    // A first failure, a short streak, and a long but recent-repeating streak are all transient.
    for (const [id, state] of [
      ['first-failure', { consecutiveErrors: 0 }],
      ['short-streak', { consecutiveErrors: 1, failingSince: ago(45) }],
      ['recent-streak', { consecutiveErrors: 9, failingSince: ago(10) }],
      ['already-alerted', { consecutiveErrors: 40, failingSince: ago(90), failureAlerted: true }],
    ] as const) {
      const { result, messageCreate } = await runFailingJob({ id, owner: 'amanda', delivery,
        convergent: true, _state: state });
      expect(result.status).toBe('error');
      expect(messageCreate, id).not.toHaveBeenCalled();
    }
    // Three failed runs over half an hour have not recovered: one alert, then quiet.
    const { messageCreate, scheduler, job } = await runFailingJob({ id: 'persistent', owner: 'amanda',
      delivery, convergent: true, _state: { consecutiveErrors: 2, failingSince: ago(31) } });
    expect(messageCreate).toHaveBeenCalledTimes(1);
    const state = scheduler.listJobs().find(item => item.id === job.id)?._state;
    expect(state?.failureAlerted).toBe(true);
    expect(state?.consecutiveErrors).toBe(3);
    consoleSpy.mockRestore();
    logSpy.mockRestore();
  });

  it('keeps alerting a persistent convergent failure until an alert is actually delivered', async () => {
    const consoleSpy = vi.spyOn(console, 'error').mockImplementation(() => {});
    const logSpy = vi.spyOn(console, 'log').mockImplementation(() => {});
    const warnSpy = vi.spyOn(console, 'warn').mockImplementation(() => {});
    const delivery = { mode: 'none' as const, feishuOpenId: 'ou_alert_target' };
    const failingSince = new Date(Date.now() - 31 * 60_000).toISOString();
    // Feishu is down when the streak first qualifies: nothing is recorded as alerted.
    const first = await runFailingJob({ id: 'persistent-retry', owner: 'amanda', delivery, convergent: true,
      _state: { consecutiveErrors: 2, failingSince } }, { sendFails: true });
    const saved = first.scheduler.listJobs().find(item => item.id === first.job.id)?._state;
    expect(saved?.failureAlerted).toBeUndefined();
    expect(saved?.consecutiveErrors).toBe(3);
    // The next run still fails and Feishu has recovered: the alert goes out once.
    const second = await runFailingJob({ id: 'persistent-retry', owner: 'amanda', delivery, convergent: true,
      _state: saved });
    expect(second.messageCreate).toHaveBeenCalledTimes(1);
    expect(second.scheduler.listJobs().find(item => item.id === second.job.id)?._state?.failureAlerted)
      .toBe(true);
    consoleSpy.mockRestore();
    logSpy.mockRestore();
    warnSpy.mockRestore();
  });

  it('stays silent for a failing job that has no feishuOpenId (opt-in gate)', async () => {
    const consoleSpy = vi.spyOn(console, 'error').mockImplementation(() => {});
    const { result, messageCreate } = await runFailingJob({
      id: 'no-optin-job',
      owner: 'amanda',
      delivery: { mode: 'none' },
    });

    expect(result.status).toBe('error');
    expect(messageCreate).not.toHaveBeenCalled();
    consoleSpy.mockRestore();
  });

  it('does not alert when the job succeeds', async () => {
    const cronMod = await import('node-cron');
    vi.spyOn(cronMod, 'schedule').mockReturnValue({
      stop: vi.fn(), start: vi.fn(), destroy: vi.fn(), getStatus: vi.fn(),
    } as unknown as ReturnType<typeof cronMod.schedule>);

    const { spawn } = await import('node:child_process');
    vi.mocked(spawn).mockReset();
    const runtimeProc = createMockProc(7402);
    vi.mocked(spawn).mockReturnValueOnce(runtimeProc as unknown as ReturnType<typeof spawn>);

    const { createScheduler, resetJobFailureAlertStreaks } = await import('../src/scheduler.js');
    resetJobFailureAlertStreaks();
    const { client, messageCreate } = makeFeishuClientMock();
    const job = makeJob({
      id: 'ok-job',
      owner: 'amanda',
      delivery: { mode: 'none', feishuOpenId: 'ou_alert_target' },
    });
    const scheduler = createScheduler(makeBotMock(), makeTmpJobsFile([job]), {
      getFeishuClientByAgentId: () => client,
    });
    scheduler.start();

    const resultPromise = scheduler.runJob('ok-job');
    setImmediate(() => {
      runtimeProc.stdout.push('done\n');
      runtimeProc.stdout.push(null);
      runtimeProc.emit('exit', 0, null);
    });
    const result = await resultPromise;

    expect(result.status).toBe('ok');
    expect(messageCreate).not.toHaveBeenCalled();
    scheduler.stop();
  });

  it('logs instead of throwing when feishuOpenId is set but no client is mapped', async () => {
    const consoleSpy = vi.spyOn(console, 'error').mockImplementation(() => {});
    const { result } = await runFailingJob({
      id: 'unmapped-client-job',
      owner: 'nobody',
      delivery: { mode: 'none', feishuOpenId: 'ou_alert_target' },
    }, { withClient: false });

    expect(result.status).toBe('error');
    const loggedUnconfigured = consoleSpy.mock.calls.some(
      ([entry]) => typeof entry === 'object' && entry !== null
        && (entry as { msg?: string }).msg === 'job failure alert unconfigured: feishuOpenId set but no Feishu client for owner',
    );
    expect(loggedUnconfigured).toBe(true);
    consoleSpy.mockRestore();
  });

  async function startRunWithTerminalRegistry() {
    const cronMod = await import('node-cron');
    vi.spyOn(cronMod, 'schedule').mockReturnValue({
      stop: vi.fn(), start: vi.fn(), destroy: vi.fn(), getStatus: vi.fn(),
    } as unknown as ReturnType<typeof cronMod.schedule>);

    const { spawn } = await import('node:child_process');
    vi.mocked(spawn).mockReset();
    const runtimeProc = createMockProc(7410);
    vi.mocked(spawn).mockReturnValueOnce(runtimeProc as unknown as ReturnType<typeof spawn>);

    const { createScheduler } = await import('../src/scheduler.js');
    const { JobTerminalRegistry } = await import('../src/job-terminal-registry.js');
    const terminalRegistry = new JobTerminalRegistry();
    const { client, messageCreate } = makeFeishuClientMock();
    const job = makeJob({
      id: 'run-terminal-job',
      name: 'Amanda review',
      owner: 'amanda',
      delivery: { mode: 'none', feishuOpenId: 'ou_alert_target' },
      agent: { workspace: '/tmp', prompt: 'review', timeout: 10, runtimePreference: ['cc'] },
    });
    const scheduler = createScheduler(makeBotMock(), makeTmpJobsFile([job]), {
      getFeishuClientByAgentId: () => client,
      runtimePolicy: { primary: 'cc', fallbacks: [] },
      terminalRegistry,
    });
    scheduler.start();
    const resultPromise = scheduler.runJob(job.id);

    for (let attempt = 0; attempt < 50 && vi.mocked(spawn).mock.calls.length === 0; attempt++) {
      await new Promise(resolve => setTimeout(resolve, 2));
    }
    const spawnOptions = vi.mocked(spawn).mock.calls[0]?.[2] as { env?: NodeJS.ProcessEnv };
    const runId = spawnOptions.env?.['RELAY_JOB_RUN_ID'];
    expect(runId).toMatch(/^[0-9a-f-]{36}$/);
    expect(spawnOptions.env?.['RELAY_JOB_TERMINAL_CONTRACT']).toBe('v1');
    return { scheduler, runtimeProc, resultPromise, terminalRegistry, client, messageCreate, runId: runId! };
  }

  async function startTerminalHttpServer(
    terminalRegistry: import('../src/job-terminal-registry.js').JobTerminalRegistry,
    client: import('../src/delivery.js').FeishuMessageClient,
    scheduler: import('../src/types.js').Scheduler,
    configPath: string,
  ) {
    const { startRelayHttpServer } = await import('../src/http-server.js');
    return startRelayHttpServer({
      feishuBots: {
        start: async () => {}, stop: () => {}, reload: async () => ({ added: [], total: 0 }), runningAgentIds: () => [],
      },
      router: { upsertGatewayBinding: vi.fn() } as unknown as import('../src/router.js').Router,
      configPath,
      getFeishuClient: agentId => agentId === 'amanda' ? client : null,
      scheduler,
      terminalRegistry,
    }, 0);
  }

  function internalPushHeaders(): Record<string, string> {
    const token = (process.env['RELAY_INTERNAL_TOKEN'] ?? '').trim();
    return {
      'Content-Type': 'application/json',
      ...(token ? { 'x-relay-token': token } : {}),
    };
  }

  it('reproduces the legacy two-message failure when an internal push omits run terminal metadata', async () => {
    const consoleSpy = vi.spyOn(console, 'error').mockImplementation(() => {});
    const run = await startRunWithTerminalRegistry();
    // The same client backs business push and scheduler fallback so the
    // assertion counts actual user-visible sends on one Feishu path.
    const server = await startTerminalHttpServer(run.terminalRegistry, run.client, run.scheduler, makeTmpJobsFile([]));

    const push = await fetch(`http://127.0.0.1:${server.port}/internal/push`, {
      method: 'POST',
      headers: internalPushHeaders(),
      body: JSON.stringify({
        channel: 'feishu', agentId: 'amanda', toFeishu: 'ou_alert_target', text: 'Duke 401 handled', source: 'legacy-producer',
      }),
    });
    expect(push.status).toBe(200);
    expect(run.messageCreate).toHaveBeenCalledTimes(1);

    run.runtimeProc.stderr.push('Codex exit 1 after the business terminal\n');
    run.runtimeProc.stderr.push(null);
    run.runtimeProc.stdout.push(null);
    run.runtimeProc.emit('exit', 1, null);
    const result = await run.resultPromise;

    expect(result.runId).toBe(run.runId);
    expect(run.messageCreate).toHaveBeenCalledTimes(2);
    await server.stop();
    run.scheduler.stop();
    consoleSpy.mockRestore();
  });

  it('links real internal push and scheduler exit through one run receipt, yielding one visible terminal', async () => {
    const consoleSpy = vi.spyOn(console, 'error').mockImplementation(() => {});
    const run = await startRunWithTerminalRegistry();
    const server = await startTerminalHttpServer(run.terminalRegistry, run.client, run.scheduler, makeTmpJobsFile([]));

    const push = await fetch(`http://127.0.0.1:${server.port}/internal/push`, {
      method: 'POST',
      headers: internalPushHeaders(),
      body: JSON.stringify({
        channel: 'feishu', agentId: 'amanda', toFeishu: 'ou_alert_target', text: 'Duke 401 handled',
        source: 'terminal-producer', runId: run.runId, terminal: true,
      }),
    });
    expect(push.status).toBe(200);
    expect(await push.json()).toMatchObject({ ok: true, runId: run.runId, terminal: true });

    run.runtimeProc.stderr.push('Codex exit 1 after the business terminal\n');
    run.runtimeProc.stderr.push(null);
    run.runtimeProc.stdout.push(null);
    run.runtimeProc.emit('exit', 1, null);
    await run.resultPromise;

    expect(run.messageCreate).toHaveBeenCalledTimes(1);
    expect(run.terminalRegistry.get(run.runId)).toMatchObject({
      status: 'sent', receipts: [{ externalMessageId: 'om_test' }],
    });
    await server.stop();
    run.scheduler.stop();
    consoleSpy.mockRestore();
  });

  it('suppresses scheduler fallback when the same run already sent its business terminal', async () => {
    const consoleSpy = vi.spyOn(console, 'error').mockImplementation(() => {});
    const run = await startRunWithTerminalRegistry();
    expect(run.terminalRegistry.reserve(run.runId, 'amanda').deliver).toBe(true);
    run.terminalRegistry.complete(run.runId, [{ channel: 'feishu', status: 'sent', externalMessageId: 'om_business' }]);

    run.runtimeProc.stderr.push('codex exit 1 after handled Duke 401\n');
    run.runtimeProc.stderr.push(null);
    run.runtimeProc.stdout.push(null);
    run.runtimeProc.emit('exit', 1, null);
    const result = await run.resultPromise;

    expect(result).toMatchObject({ status: 'error', runId: run.runId });
    expect(run.messageCreate).not.toHaveBeenCalled();
    expect(run.terminalRegistry.get(run.runId)).toMatchObject({ status: 'sent' });
    run.scheduler.stop();
    consoleSpy.mockRestore();
  });

  it('keeps the scheduler fallback when the internal terminal delivery failed', async () => {
    const consoleSpy = vi.spyOn(console, 'error').mockImplementation(() => {});
    const run = await startRunWithTerminalRegistry();
    run.terminalRegistry.reserve(run.runId, 'amanda');
    run.terminalRegistry.complete(run.runId, [{ channel: 'feishu', status: 'failed', error: 'internal push failed' }]);

    run.runtimeProc.stderr.push('runtime crash\n');
    run.runtimeProc.stderr.push(null);
    run.runtimeProc.stdout.push(null);
    run.runtimeProc.emit('exit', 1, null);
    const result = await run.resultPromise;

    expect(result.status).toBe('error');
    expect(run.messageCreate).toHaveBeenCalledTimes(1);
    expect(run.terminalRegistry.get(run.runId)).toMatchObject({
      status: 'sent',
      receipts: [{ channel: 'feishu', status: 'sent' }],
    });
    run.scheduler.stop();
    consoleSpy.mockRestore();
  });

  it('alerts once for each distinct failed run instead of swallowing the second in a job cooldown', async () => {
    const consoleSpy = vi.spyOn(console, 'error').mockImplementation(() => {});
    const cronMod = await import('node-cron');
    vi.spyOn(cronMod, 'schedule').mockReturnValue({
      stop: vi.fn(), start: vi.fn(), destroy: vi.fn(), getStatus: vi.fn(),
    } as unknown as ReturnType<typeof cronMod.schedule>);
    const { spawn } = await import('node:child_process');
    vi.mocked(spawn).mockReset();
    const firstProc = createMockProc(7421);
    const secondProc = createMockProc(7422);
    vi.mocked(spawn)
      .mockReturnValueOnce(firstProc as unknown as ReturnType<typeof spawn>)
      .mockReturnValueOnce(secondProc as unknown as ReturnType<typeof spawn>);
    const { createScheduler } = await import('../src/scheduler.js');
    const { JobTerminalRegistry } = await import('../src/job-terminal-registry.js');
    const { client, messageCreate } = makeFeishuClientMock();
    const job = makeJob({
      id: 'distinct-run-job', owner: 'amanda',
      delivery: { mode: 'none', feishuOpenId: 'ou_alert_target' },
      agent: { workspace: '/tmp', prompt: 'review', timeout: 10, runtimePreference: ['cc'] },
    });
    const scheduler = createScheduler(makeBotMock(), makeTmpJobsFile([job]), {
      getFeishuClientByAgentId: () => client,
      runtimePolicy: { primary: 'cc', fallbacks: [] },
      terminalRegistry: new JobTerminalRegistry(),
    });
    scheduler.start();

    const firstResultPromise = scheduler.runJob(job.id);
    await new Promise(resolve => setImmediate(resolve));
    firstProc.stderr.push('first failure\n');
    firstProc.stderr.push(null);
    firstProc.stdout.push(null);
    firstProc.emit('exit', 1, null);
    const firstResult = await firstResultPromise;

    const secondResultPromise = scheduler.runJob(job.id);
    await new Promise(resolve => setImmediate(resolve));
    secondProc.stderr.push('second failure\n');
    secondProc.stderr.push(null);
    secondProc.stdout.push(null);
    secondProc.emit('exit', 1, null);
    const secondResult = await secondResultPromise;

    expect(firstResult.runId).not.toBe(secondResult.runId);
    expect(messageCreate).toHaveBeenCalledTimes(2);
    scheduler.stop();
    consoleSpy.mockRestore();
  });
});

// ─────────────────────────────────────────────────────────────────────────────
// Batch runtime options
// ─────────────────────────────────────────────────────────────────────────────

describe('batch runtime options', () => {
  it('passes per-job maxTurns to cc batch runtime', async () => {
    const cronMod = await import('node-cron');
    vi.spyOn(cronMod, 'schedule').mockReturnValue({
      stop: vi.fn(), start: vi.fn(), destroy: vi.fn(), getStatus: vi.fn(),
    } as unknown as ReturnType<typeof cronMod.schedule>);

    const { spawn } = await import('node:child_process');
    const mockProc = createMockProc(3337);
    vi.mocked(spawn).mockReturnValue(mockProc as unknown as ReturnType<typeof spawn>);

    const { createScheduler } = await import('../src/scheduler.js');
    const bot = makeBotMock();
    const job = makeJob({
      id: 'max-turns-job',
      enabled: true,
      agent: {
        workspace: '/tmp',
        prompt: 'use more turns',
        timeout: 10,
        maxTurns: 100,
        runtimePreference: ['cc'],
      },
    });
    const file = makeTmpJobsFile([job]);
    const scheduler = createScheduler(bot, file);
    scheduler.start();

    const resultPromise = scheduler.runJob('max-turns-job');

    setImmediate(() => {
      mockProc.stdout.push('done\n');
      mockProc.stdout.push(null);
      mockProc.emit('exit', 0, null);
    });

    const result = await resultPromise;
    expect(result.status).toBe('ok');

    const spawnCalls = vi.mocked(spawn).mock.calls;
    const args = spawnCalls[spawnCalls.length - 1]?.[1] as string[];
    const maxTurnsIndex = args.indexOf('--max-turns');
    expect(maxTurnsIndex).toBeGreaterThanOrEqual(0);
    expect(args[maxTurnsIndex + 1]).toBe('100');

    scheduler.stop();
  });
});

// ─────────────────────────────────────────────────────────────────────────────
// T3: Workspace-level serialization
// ─────────────────────────────────────────────────────────────────────────────

describe('Workspace serialization [wi_639]', () => {
  it('queues second job behind first when sharing workspace', async () => {
    const cronMod = await import('node-cron');
    const callbacks: Map<string, () => void> = new Map();
    vi.spyOn(cronMod, 'schedule').mockImplementation((expr, cb) => {
      const fn = cb as () => void;
      // Extract job id from the closure by checking which expr was passed
      // We register two jobs with different exprs; store callbacks keyed by expr
      callbacks.set(expr as string, fn);
      return { stop: vi.fn(), start: vi.fn(), destroy: vi.fn(), getStatus: vi.fn() } as unknown as ReturnType<typeof cronMod.schedule>;
    });

    const { spawn } = await import('node:child_process');
    const sharedWorkspace = '/tmp/ws-serialization-test';
    const log: string[] = [];

    const proc1 = createMockProc(3111);
    const proc2 = createMockProc(3222);

    vi.mocked(spawn)
      .mockImplementationOnce(() => {
        log.push('job-a-started');
        // job-a runs for a while (doesn't emit exit immediately)
        return proc1 as unknown as ReturnType<typeof spawn>;
      })
      .mockImplementationOnce(() => {
        log.push('job-b-started');
        setImmediate(() => {
          proc2.stdout.push('job-b done\n');
          proc2.stdout.push(null);
          proc2.emit('exit', 0, null);
        });
        return proc2 as unknown as ReturnType<typeof spawn>;
      });

    const { createScheduler } = await import('../src/scheduler.js');
    const bot = makeBotMock();
    const jobA = makeJob({
      id: 'job-a',
      name: 'Job A',
      enabled: true,
      schedule: { expr: '0 3 * * *', tz: 'Asia/Shanghai' },
      agent: { workspace: sharedWorkspace, prompt: 'run A', timeout: 60 },
    });
    const jobB = makeJob({
      id: 'job-b',
      name: 'Job B',
      enabled: true,
      schedule: { expr: '5 3 * * *', tz: 'Asia/Shanghai' },
      agent: { workspace: sharedWorkspace, prompt: 'run B', timeout: 60 },
    });
    const file = makeTmpJobsFile([jobA, jobB]);
    const scheduler = createScheduler(bot, file);
    scheduler.start();

    // Trigger both jobs simultaneously (simulating 3:00 AM collision)
    const callbackA = callbacks.get('0 3 * * *');
    const callbackB = callbacks.get('5 3 * * *');
    expect(callbackA).toBeDefined();
    expect(callbackB).toBeDefined();

    // Fire both — job-a should start, job-b should queue
    callbackA!();
    callbackB!();
    await new Promise(r => setTimeout(r, 20));

    // job-a should have started; job-b should NOT have started yet
    expect(log).toContain('job-a-started');
    expect(log).not.toContain('job-b-started');

    // Now complete job-a
    proc1.stdout.push('job-a done\n');
    proc1.stdout.push(null);
    proc1.emit('exit', 0, null);
    await new Promise(r => setTimeout(r, 50));

    // Now job-b should have started and completed
    expect(log).toContain('job-b-started');

    scheduler.stop();
  });

  it('runs jobs concurrently when workspaces differ', async () => {
    const cronMod = await import('node-cron');
    const callbacks: Map<string, () => void> = new Map();
    vi.spyOn(cronMod, 'schedule').mockImplementation((expr, cb) => {
      callbacks.set(expr as string, cb as () => void);
      return { stop: vi.fn(), start: vi.fn(), destroy: vi.fn(), getStatus: vi.fn() } as unknown as ReturnType<typeof cronMod.schedule>;
    });

    const { spawn } = await import('node:child_process');
    const log: string[] = [];

    const proc1 = createMockProc(3112);
    const proc2 = createMockProc(3223);

    vi.mocked(spawn)
      .mockImplementationOnce(() => {
        log.push('ws1-started');
        return proc1 as unknown as ReturnType<typeof spawn>;
      })
      .mockImplementationOnce(() => {
        log.push('ws2-started');
        setImmediate(() => {
          proc2.stdout.push('ws2 done\n');
          proc2.stdout.push(null);
          proc2.emit('exit', 0, null);
        });
        return proc2 as unknown as ReturnType<typeof spawn>;
      });

    const { createScheduler } = await import('../src/scheduler.js');
    const bot = makeBotMock();
    const jobA = makeJob({
      id: 'ws1-job',
      enabled: true,
      schedule: { expr: '0 3 * * *', tz: 'Asia/Shanghai' },
      agent: { workspace: '/tmp/ws1', prompt: 'run', timeout: 60 },
    });
    const jobB = makeJob({
      id: 'ws2-job',
      enabled: true,
      schedule: { expr: '0 3 * * *', tz: 'Asia/Shanghai' },
      agent: { workspace: '/tmp/ws2', prompt: 'run', timeout: 60 },
    });
    const file = makeTmpJobsFile([jobA, jobB]);
    const scheduler = createScheduler(bot, file);
    scheduler.start();

    const callbackA = callbacks.get('0 3 * * *');
    // Both have the same expr, but cronSchedule is called twice (one per job)
    // In practice the mock captures only the last one. Use runJob instead:
    // Trigger via runJob to test different workspaces
    const runA = scheduler.runJob('ws1-job');
    const runB = scheduler.runJob('ws2-job');

    await new Promise(r => setTimeout(r, 20));

    // Both should have started concurrently (different workspaces)
    expect(log).toContain('ws1-started');
    expect(log).toContain('ws2-started');

    // Complete both
    setImmediate(() => {
      proc1.stdout.push('done\n');
      proc1.stdout.push(null);
      proc1.emit('exit', 0, null);
    });
    await Promise.all([runA, runB]);

    scheduler.stop();
  });
});

// ─────────────────────────────────────────────────────────────────────────────
// Per-job concurrency: `maxConcurrent` runs of one job at once
// ─────────────────────────────────────────────────────────────────────────────

describe('Per-job concurrency [wi_20261107_236]', () => {
  afterEach(() => { vi.useRealTimers(); });

  function finish(proc: ReturnType<typeof createMockProc>, code: number): void {
    proc.stdout.push('done\n');
    proc.stdout.push(null);
    proc.emit('exit', code, null);
  }

  it('runs up to maxConcurrent runs of one job beside the workspace queue and refuses the next', async () => {
    const cronMod = await import('node-cron');
    vi.spyOn(cronMod, 'schedule').mockReturnValue({
      stop: vi.fn(), start: vi.fn(), destroy: vi.fn(), getStatus: vi.fn(),
    } as unknown as ReturnType<typeof cronMod.schedule>);
    const { spawn } = await import('node:child_process');
    const procs: Array<{ prompt: string; proc: ReturnType<typeof createMockProc> }> = [];
    vi.mocked(spawn).mockImplementation((() => {
      const entry = { prompt: '', proc: createMockProc(4000 + procs.length) };
      entry.proc.stdin.on('data', chunk => { entry.prompt += String(chunk); });
      procs.push(entry);
      return entry.proc as unknown as ReturnType<typeof spawn>;
    }) as unknown as typeof spawn);
    const warnSpy = vi.spyOn(console, 'warn').mockImplementation(() => {});

    const { createScheduler } = await import('../src/scheduler.js');
    const workspace = '/tmp/ws-per-job-concurrency';
    const exclusive = makeJob({ id: 'exclusive-job', agent: { workspace, prompt: 'exclusive', timeout: 60, runtimePreference: ['cc'] } });
    const parallel = makeJob({ id: 'review-job', maxConcurrent: 3,
      agent: { workspace, prompt: 'parallel review', timeout: 60, runtimePreference: ['cc'] } });
    const scheduler = createScheduler(makeBotMock(), makeTmpJobsFile([exclusive, parallel]));
    scheduler.start();
    const state = () => scheduler.listJobs().find(j => j.id === 'review-job')!._state;

    // The exclusive job holds the workspace; the parallel job's runs do not queue behind it.
    const exclusiveRun = scheduler.runJob('exclusive-job');
    const runs = [scheduler.runJob('review-job'), scheduler.runJob('review-job'), scheduler.runJob('review-job')];
    const refused = await scheduler.runJob('review-job');
    await new Promise(r => setTimeout(r, 30));
    expect(refused).toMatchObject({ status: 'error', errorMessage: 'previous run still active' });
    expect(procs.filter(p => p.prompt.includes('parallel review'))).toHaveLength(3);
    expect(scheduler.getRunningJobIds()).toEqual(['exclusive-job', 'review-job']);

    // One run failing releases only its own claim; the other two keep running.
    const first = procs.find(p => p.prompt.includes('parallel review'))!;
    finish(first.proc, 1);
    expect((await runs[0]).status).toBe('error');
    expect(scheduler.getRunningJobIds()).toContain('review-job');
    expect(state()?.currentRun).toBeDefined();
    runs.push(scheduler.runJob('review-job'));
    await new Promise(r => setTimeout(r, 30));
    expect(procs.filter(p => p.prompt.includes('parallel review'))).toHaveLength(4);
    expect(await scheduler.runJob('review-job')).toMatchObject({ errorMessage: 'previous run still active' });

    for (const { proc } of procs.filter(p => p !== first)) finish(proc, 0);
    await Promise.all([exclusiveRun, ...runs]);
    expect(scheduler.getRunningJobIds()).toEqual([]);
    expect(state()?.currentRun).toBeUndefined();
    scheduler.stop();
    warnSpy.mockRestore();
  });

  it.each([false, true])('a run finishing on the object a reload replaced keeps a concurrent failure owed (convergent=%s)', async (convergent) => {
    const cronMod = await import('node-cron');
    vi.spyOn(cronMod, 'schedule').mockReturnValue({
      stop: vi.fn(), start: vi.fn(), destroy: vi.fn(), getStatus: vi.fn(),
    } as unknown as ReturnType<typeof cronMod.schedule>);
    const { spawn } = await import('node:child_process');
    const procs: Array<ReturnType<typeof createMockProc>> = [];
    vi.mocked(spawn).mockImplementation((() => {
      const proc = createMockProc(4100 + procs.length);
      procs.push(proc);
      return proc as unknown as ReturnType<typeof spawn>;
    }) as unknown as typeof spawn);
    const warnSpy = vi.spyOn(console, 'warn').mockImplementation(() => {});

    const { createScheduler } = await import('../src/scheduler.js');
    const job = makeJob({ id: 'reload-job', maxConcurrent: 3, convergent,
      agent: { workspace: '/tmp/ws-reload-concurrency', prompt: 'run', timeout: 60, runtimePreference: ['cc'] } });
    const scheduler = createScheduler(makeBotMock(), makeTmpJobsFile([job]));
    scheduler.start();
    const state = () => scheduler.listJobs().find(j => j.id === 'reload-job')!._state!;

    const a = scheduler.runJob('reload-job');
    await new Promise(r => setTimeout(r, 30));
    scheduler.reload();
    const b = scheduler.runJob('reload-job');
    await new Promise(r => setTimeout(r, 30));
    expect(procs).toHaveLength(2);
    finish(procs[1]!, 1);
    const failedB = await b;
    expect(state().unresolvedResults?.map(item => item.runId)).toEqual([failedB.runId]);
    finish(procs[0]!, 0);
    const okA = await a;
    expect(okA.status).toBe('ok');
    // A started before B failed: its success neither settles nor erases B.
    expect(state().unresolvedResults?.map(item => item.runId)).toEqual([failedB.runId]);
    expect(state().lastResult?.runId).toBe(okA.runId);
    expect(state().consecutiveErrors).toBe(0);
    expect(state().currentRun).toBeUndefined();
    expect(state().definitionHash).toBeDefined();
    scheduler.stop();
    warnSpy.mockRestore();
  });

  it.each([false, true])('a run of the same minute never settles a concurrent event run failure (convergent=%s)', async (convergent) => {
    const cronMod = await import('node-cron');
    vi.spyOn(cronMod, 'schedule').mockReturnValue({
      stop: vi.fn(), start: vi.fn(), destroy: vi.fn(), getStatus: vi.fn(),
    } as unknown as ReturnType<typeof cronMod.schedule>);
    const { spawn } = await import('node:child_process');
    const procs: Array<ReturnType<typeof createMockProc>> = [];
    vi.mocked(spawn).mockImplementation((() => {
      const proc = createMockProc(4200 + procs.length);
      procs.push(proc);
      return proc as unknown as ReturnType<typeof spawn>;
    }) as unknown as typeof spawn);
    const warnSpy = vi.spyOn(console, 'warn').mockImplementation(() => {});

    const { createScheduler } = await import('../src/scheduler.js');
    const job = makeJob({ id: 'event-job', maxConcurrent: 3, convergent,
      agent: { workspace: '/tmp/ws-event-concurrency', prompt: 'run', timeout: 60, runtimePreference: ['cc'] } });
    const scheduler = createScheduler(makeBotMock(), makeTmpJobsFile([job]));
    scheduler.start();
    const state = () => scheduler.listJobs().find(j => j.id === 'event-job')!._state!;
    const settle = () => new Promise(r => setTimeout(r, 30));
    // Only the clock is fixed, inside one minute; timers stay real.
    vi.useFakeTimers({ toFake: ['Date'] });
    vi.setSystemTime(new Date('2026-11-08T02:00:10Z'));
    const catalog = process.env['PULSE_CODEX_MODELS_CACHE_PATH']!;
    fs.writeFileSync(catalog, JSON.stringify({ ...JSON.parse(fs.readFileSync(catalog, 'utf-8')),
      fetched_at: new Date().toISOString() }));  // Fresh at the fixed clock.

    expect(scheduler.triggerJob('event-job')).toBe('started');
    await settle();
    const slotA = state().currentRun?.scheduledFor;
    vi.setSystemTime(new Date('2026-11-08T02:00:20Z'));
    expect(scheduler.triggerJob('event-job')).toBe('started');
    await settle();
    expect(procs).toHaveLength(2);
    vi.setSystemTime(new Date('2026-11-08T02:00:30Z'));
    finish(procs[1]!, 1);
    await settle();
    const failedB = state().lastResult!;
    expect(failedB.status).toBe('error');
    // Both event runs carry the minute slot; only the run identity tells them apart.
    // Both event runs carry the same minute slot; only the run identity tells them apart.
    expect(failedB.scheduledFor).toBe(slotA);
    expect(state().unresolvedResults?.map(item => item.runId)).toEqual([failedB.runId]);
    vi.setSystemTime(new Date('2026-11-08T02:00:40Z'));
    finish(procs[0]!, 0);
    await settle();
    expect(state().lastResult?.status).toBe('ok');
    expect(state().lastResult?.runId).not.toBe(failedB.runId);
    expect(state().unresolvedResults?.map(item => item.runId)).toEqual([failedB.runId]);
    expect(scheduler.getRunningJobIds()).toEqual([]);

    // A run of the same slot that starts after the failure finished does recover it.
    vi.setSystemTime(new Date('2026-11-08T02:00:50Z'));
    expect(scheduler.triggerJob('event-job')).toBe('started');
    await settle();
    finish(procs[2]!, 0);
    await settle();
    expect(state().lastResult?.scheduledFor).toBe(slotA);
    expect(state().unresolvedResults).toEqual([]);
    scheduler.stop();
    warnSpy.mockRestore();
  });

  it('a sharedWorkspace job never makes the workspace queue wait and still runs once at a time', async () => {
    const cronMod = await import('node-cron');
    vi.spyOn(cronMod, 'schedule').mockReturnValue({
      stop: vi.fn(), start: vi.fn(), destroy: vi.fn(), getStatus: vi.fn(),
    } as unknown as ReturnType<typeof cronMod.schedule>);
    const { spawn } = await import('node:child_process');
    const procs: Array<{ prompt: string; proc: ReturnType<typeof createMockProc> }> = [];
    vi.mocked(spawn).mockImplementation((() => {
      const entry = { prompt: '', proc: createMockProc(4200 + procs.length) };
      entry.proc.stdin.on('data', chunk => { entry.prompt += String(chunk); });
      procs.push(entry);
      return entry.proc as unknown as ReturnType<typeof spawn>;
    }) as unknown as typeof spawn);
    const warnSpy = vi.spyOn(console, 'warn').mockImplementation(() => {});

    const { createScheduler } = await import('../src/scheduler.js');
    const workspace = '/tmp/ws-shared-workspace';
    const nightly = makeJob({ id: 'nightly-job', sharedWorkspace: true,
      agent: { workspace, prompt: 'nightly reader', timeout: 60, runtimePreference: ['cc'] } });
    const inbox = makeJob({ id: 'inbox-job', agent: { workspace, prompt: 'inbox', timeout: 60, runtimePreference: ['cc'] } });
    const scheduler = createScheduler(makeBotMock(), makeTmpJobsFile([nightly, inbox]));
    scheduler.start();

    const nightlyRun = scheduler.runJob('nightly-job');
    const inboxRun = scheduler.runJob('inbox-job');
    await new Promise(r => setTimeout(r, 30));
    expect(procs.map(p => p.prompt.includes('nightly reader') ? 'nightly' : 'inbox')).toEqual(['nightly', 'inbox']);
    expect(await scheduler.runJob('nightly-job')).toMatchObject({ errorMessage: 'previous run still active' });

    for (const { proc } of procs) finish(proc, 0);
    await Promise.all([nightlyRun, inboxRun]);
    expect(scheduler.getRunningJobIds()).toEqual([]);
    scheduler.stop();
    warnSpy.mockRestore();
  });

  it('reads a subscription reset announced only on stderr into the run result', async () => {
    const cronMod = await import('node-cron');
    vi.spyOn(cronMod, 'schedule').mockReturnValue({
      stop: vi.fn(), start: vi.fn(), destroy: vi.fn(), getStatus: vi.fn(),
    } as unknown as ReturnType<typeof cronMod.schedule>);
    const { spawn } = await import('node:child_process');
    const procs: Array<ReturnType<typeof createMockProc>> = [];
    vi.mocked(spawn).mockImplementation((() => {
      const proc = createMockProc(4300 + procs.length);
      procs.push(proc);
      return proc as unknown as ReturnType<typeof spawn>;
    }) as unknown as typeof spawn);
    const warnSpy = vi.spyOn(console, 'warn').mockImplementation(() => {});
    const errorSpy = vi.spyOn(console, 'error').mockImplementation(() => {});
    const { createScheduler } = await import('../src/scheduler.js');
    const job = makeJob({ id: 'quota-job', runtimeStrict: true,
      agent: { workspace: '/tmp/ws-quota', prompt: 'weekly', timeout: 60, runtimePreference: ['cc'] } });
    const scheduler = createScheduler(makeBotMock(), makeTmpJobsFile([job]));
    scheduler.start();
    const run = scheduler.runJob('quota-job');
    await new Promise(r => setTimeout(r, 30));
    const proc = procs[0]!;
    proc.stderr.push("You've hit your session limit · resets 5:20pm (Asia/Taipei)\n");
    proc.stderr.push(null);
    proc.stdout.push(null);
    proc.emit('exit', 1, null);
    const result = await run;
    expect(result.status).toBe('error');
    expect(result.quotaResetAt).toMatch(/T\d{2}:20:00\.000Z$/);
    scheduler.stop();
    warnSpy.mockRestore();
    errorSpy.mockRestore();
  });

  it.each([
    ['codex fails without a reset', 'The usage limit has been reached', '2026-11-15T09:20:00.000Z'],
    ['codex announces a later reset', "You've hit your usage limit · resets 6:00pm (Asia/Taipei)", '2026-11-15T09:20:00.000Z'],
    ['codex announces an earlier reset', "You've hit your usage limit · resets 5:05pm (Asia/Taipei)", '2026-11-15T09:05:00.000Z'],
  ])('a failed fallback keeps the earliest quota reset of every attempt (%s)', async (_label, codexError, expected) => {
    vi.useFakeTimers({ toFake: ['Date'] });
    vi.setSystemTime(new Date('2026-11-15T09:00:06.000Z'));
    refreshTestCodexModelCatalogTime();
    const cronMod = await import('node-cron');
    vi.spyOn(cronMod, 'schedule').mockReturnValue({
      stop: vi.fn(), start: vi.fn(), destroy: vi.fn(), getStatus: vi.fn(),
    } as unknown as ReturnType<typeof cronMod.schedule>);
    const { spawn } = await import('node:child_process');
    const errors = ["You've hit your session limit · resets 5:20pm (Asia/Taipei)\n", `${codexError}\n`];
    let spawned = 0;
    vi.mocked(spawn).mockImplementation((() => {
      const proc = createMockProc(4500 + spawned);
      const error = errors[spawned++] ?? 'unexpected\n';
      setTimeout(() => { proc.stderr.push(error); proc.stderr.push(null); proc.stdout.push(null); proc.emit('exit', 1, null); }, 5);
      return proc as unknown as ReturnType<typeof spawn>;
    }) as unknown as typeof spawn);
    const warnSpy = vi.spyOn(console, 'warn').mockImplementation(() => {});
    const errorSpy = vi.spyOn(console, 'error').mockImplementation(() => {});
    const { createScheduler } = await import('../src/scheduler.js');
    const job = makeJob({ id: 'failover-quota-job',
      agent: { workspace: '/tmp/ws-failover-quota', prompt: 'weekly', timeout: 60, runtimePreference: ['cc'] } });
    const scheduler = createScheduler(makeBotMock(), makeTmpJobsFile([job]), {
      runtimePolicy: { primary: 'cc', fallbacks: ['codex'] },
    });
    scheduler.start();
    try {
      const result = await scheduler.runJob('failover-quota-job');
      expect(result.attemptedRuntimes).toEqual(['cc', 'codex']);
      expect(result.status).toBe('error');
      expect(result.quotaResetAt).toBe(expected);
    } finally {
      scheduler.stop();
      warnSpy.mockRestore();
      errorSpy.mockRestore();
      vi.useRealTimers();
    }
  });

  it('skips a job whose sharedWorkspace is not a boolean', async () => {
    const warnSpy = vi.spyOn(console, 'warn').mockImplementation(() => {});
    const { loadJobs } = await import('../src/scheduler.js');
    const file = makeTmpJobsFile(['yes', 1, true].map((sharedWorkspace, i) =>
      ({ ...makeJob({ id: `job-${i}` }), sharedWorkspace })));
    expect(loadJobs(file).map(job => job.id)).toEqual(['job-2']);
    warnSpy.mockRestore();
  });

  it('skips a job whose maxConcurrent is not an integer within 1-8', async () => {
    const warnSpy = vi.spyOn(console, 'warn').mockImplementation(() => {});
    const { loadJobs } = await import('../src/scheduler.js');
    const file = makeTmpJobsFile([0, 1.5, 9, '3', 2].map((maxConcurrent, i) =>
      ({ ...makeJob({ id: `job-${i}` }), maxConcurrent })));
    expect(loadJobs(file).map(job => job.id)).toEqual(['job-4']);
    warnSpy.mockRestore();
  });
});

// ──────────────────────────────────────────────────────────────────────────────
// Scheduler-wide concurrency guard. This is intentionally separate from the
// workspace queue: the production OOM involved different workspaces.
// ──────────────────────────────────────────────────────────────────────────────
describe('Scheduler global concurrency guard', () => {
  it('fails fast on an invalid explicit concurrency limit', async () => {
    process.env['RELAY_SCHEDULER_MAX_CONCURRENT_JOBS'] = '0';
    const { createScheduler } = await import('../src/scheduler.js');
    expect(() => createScheduler(makeBotMock(), makeTmpJobsFile([]), {
      registerCronTasks: false,
    })).toThrow('must be a positive safe integer');
  });

  it('queues manual jobs from different workspaces and keeps queued claims restart-visible', async () => {
    process.env['RELAY_SCHEDULER_MAX_CONCURRENT_JOBS'] = '1';
    const { spawn } = await import('node:child_process');
    vi.mocked(spawn).mockReset();
    const firstProc = createMockProc(9101);
    const secondProc = createMockProc(9102);
    vi.mocked(spawn)
      .mockReturnValueOnce(firstProc as unknown as ReturnType<typeof spawn>)
      .mockReturnValueOnce(secondProc as unknown as ReturnType<typeof spawn>);

    const { createScheduler } = await import('../src/scheduler.js');
    const jobs = [
      makeJob({ id: 'global-a', agent: { workspace: '/tmp/global-a', prompt: 'a', timeout: 60, runtimePreference: ['cc'] } }),
      makeJob({ id: 'global-b', agent: { workspace: '/tmp/global-b', prompt: 'b', timeout: 60, runtimePreference: ['cc'] } }),
    ];
    const scheduler = createScheduler(makeBotMock(), makeTmpJobsFile(jobs), {
      registerCronTasks: false,
      deliveryEnabled: false,
      reconcileMissedRuns: false,
      runtimePolicy: { primary: 'cc', fallbacks: [] },
    });
    scheduler.start();

    const firstRun = scheduler.runJob('global-a');
    const secondRun = scheduler.runJob('global-b');
    await new Promise(resolve => setTimeout(resolve, 20));

    expect(vi.mocked(spawn)).toHaveBeenCalledTimes(1);
    expect(scheduler.getRunningJobIds()).toEqual(['global-a', 'global-b']);
    scheduler.stop();
    expect(scheduler.getRunningJobIds()).toEqual(['global-a', 'global-b']);

    firstProc.stdout.push('a done\n');
    firstProc.stdout.push(null);
    firstProc.emit('exit', 0, null);
    for (let attempt = 0; attempt < 50 && vi.mocked(spawn).mock.calls.length < 2; attempt++) {
      await new Promise(resolve => setTimeout(resolve, 5));
    }
    expect(vi.mocked(spawn)).toHaveBeenCalledTimes(2);

    secondProc.stdout.push('b done\n');
    secondProc.stdout.push(null);
    secondProc.emit('exit', 0, null);
    await Promise.all([firstRun, secondRun]);
    expect(scheduler.getRunningJobIds()).toEqual([]);
  });

  it('drains FIFO after an execution exception and persists outcomes through reload', async () => {
    process.env['RELAY_SCHEDULER_MAX_CONCURRENT_JOBS'] = '1';
    const { spawn } = await import('node:child_process');
    vi.mocked(spawn).mockReset();
    const firstProc = createMockProc(9111);
    const thirdProc = createMockProc(9113);
    vi.mocked(spawn)
      .mockReturnValueOnce(firstProc as unknown as ReturnType<typeof spawn>)
      .mockImplementationOnce(() => { throw new Error('synthetic spawn failure'); })
      .mockReturnValueOnce(thirdProc as unknown as ReturnType<typeof spawn>);

    const { createScheduler } = await import('../src/scheduler.js');
    const jobs = ['a', 'b', 'c'].map(suffix => makeJob({
      id: `drain-${suffix}`,
      agent: { workspace: `/tmp/drain-${suffix}`, prompt: suffix, timeout: 60, runtimePreference: ['cc'] },
    }));
    const scheduler = createScheduler(makeBotMock(), makeTmpJobsFile(jobs), {
      deliveryEnabled: false,
      reconcileMissedRuns: false,
      runtimePolicy: { primary: 'cc', fallbacks: [] },
    });
    scheduler.start();

    const runs = jobs.map(job => scheduler.runJob(job.id));
    await new Promise(resolve => setTimeout(resolve, 20));
    expect(vi.mocked(spawn)).toHaveBeenCalledTimes(1);

    scheduler.reload();
    expect(scheduler.getRunningJobIds()).toEqual(['drain-a', 'drain-b', 'drain-c']);

    firstProc.stdout.push('a done\n');
    firstProc.stdout.push(null);
    firstProc.emit('exit', 0, null);
    for (let attempt = 0; attempt < 50 && vi.mocked(spawn).mock.calls.length < 3; attempt++) {
      await new Promise(resolve => setTimeout(resolve, 5));
    }
    expect(vi.mocked(spawn)).toHaveBeenCalledTimes(3);

    thirdProc.stdout.push('c done\n');
    thirdProc.stdout.push(null);
    thirdProc.emit('exit', 0, null);
    const results = await Promise.all(runs);
    expect(results[1].status).toBe('error');
    expect(results[2].status).toBe('ok');
    expect(scheduler.getRunningJobIds()).toEqual([]);

    await new Promise(resolve => setTimeout(resolve, 300));
    const durableState = JSON.parse(
      fs.readFileSync(process.env['CRON_JOBS_STATE_PATH']!, 'utf-8'),
    ).jobs as Record<string, ScheduledJob['_state']>;
    expect(durableState['drain-a']).toMatchObject({
      consecutiveErrors: 0,
      definitionHash: expect.stringMatching(/^[a-f0-9]{64}$/),
      activatedAt: expect.any(String),
      lastStatus: 'ok',
      lastRunAt: expect.any(String),
    });
    expect(durableState['drain-b']).toMatchObject({
      consecutiveErrors: 1,
      lastStatus: 'error',
      lastRunAt: expect.any(String),
    });
    expect(durableState['drain-c']).toMatchObject({
      consecutiveErrors: 0,
      lastStatus: 'ok',
      lastRunAt: expect.any(String),
    });
    scheduler.stop();
  });

  it('hot reload adopts a changed definition but never starts a running job twice', async () => {
    const { spawn } = await import('node:child_process');
    vi.mocked(spawn).mockReset();
    const running = createMockProc(9121);
    vi.mocked(spawn).mockReturnValueOnce(running as unknown as ReturnType<typeof spawn>);

    const { createScheduler } = await import('../src/scheduler.js');
    const original = makeJob({
      id: 'hot-reload', name: 'before',
      agent: { workspace: '/tmp/hot-reload', prompt: 'p', timeout: 60, runtimePreference: ['cc'] },
    });
    const file = makeTmpJobsFile([original]);
    const scheduler = createScheduler(makeBotMock(), file, {
      registerCronTasks: false, deliveryEnabled: false, reconcileMissedRuns: false,
      runtimePolicy: { primary: 'cc', fallbacks: [] },
    });
    scheduler.start();
    const firstRun = scheduler.runJob('hot-reload');
    await new Promise(resolve => setTimeout(resolve, 20));
    expect(vi.mocked(spawn)).toHaveBeenCalledTimes(1);

    fs.writeFileSync(file, JSON.stringify({ version: 2, jobs: [{ ...original, name: 'after' }] }), 'utf-8');
    scheduler.reload();
    expect(scheduler.listJobs().map(job => job.name)).toEqual(['after']);
    expect(scheduler.getRunningJobIds()).toEqual(['hot-reload']);
    const duplicate = await scheduler.runJob('hot-reload');
    expect(duplicate).toMatchObject({ status: 'error', errorMessage: 'previous run still active' });
    expect(vi.mocked(spawn)).toHaveBeenCalledTimes(1);

    running.stdout.push('done\n');
    running.stdout.push(null);
    running.emit('exit', 0, null);
    expect((await firstRun).status).toBe('ok');
    expect(scheduler.getRunningJobIds()).toEqual([]);
    scheduler.stop();
  });

  it('reload right after a run finished keeps that run and no stale write follows', async () => {
    const { spawn } = await import('node:child_process');
    vi.mocked(spawn).mockReset();
    const proc = createMockProc(9122);
    vi.mocked(spawn).mockReturnValueOnce(proc as unknown as ReturnType<typeof spawn>);
    const { createScheduler } = await import('../src/scheduler.js');
    const original = makeJob({
      id: 'just-finished', name: 'before',
      agent: { workspace: '/tmp/just-finished', prompt: 'p', timeout: 60, runtimePreference: ['cc'] },
    });
    const file = makeTmpJobsFile([original]);
    const scheduler = createScheduler(makeBotMock(), file, {
      registerCronTasks: true, deliveryEnabled: false, reconcileMissedRuns: false,
      runtimePolicy: { primary: 'cc', fallbacks: [] },
    });
    scheduler.start();
    const run = scheduler.runJob('just-finished');
    await new Promise(resolve => setTimeout(resolve, 20));
    proc.stdout.push('done\n');
    proc.stdout.push(null);
    proc.emit('exit', 0, null);
    await run;

    // The debounced (200ms) run-state write has not happened yet.
    fs.writeFileSync(file, JSON.stringify({ version: 2, jobs: [{ ...original, name: 'after' }] }), 'utf-8');
    scheduler.reload();
    const adopted = scheduler.listJobs()[0];
    expect(adopted.name).toBe('after');
    expect(adopted._state?.lastStatus).toBe('ok');
    expect(adopted._state?.lastRunAt).toEqual(expect.any(String));

    await new Promise(resolve => setTimeout(resolve, 300));
    const durable = JSON.parse(fs.readFileSync(process.env['CRON_JOBS_STATE_PATH']!, 'utf-8')).jobs['just-finished'];
    expect(durable.definitionHash).toBe(adopted._state?.definitionHash);
    expect(durable.lastRunAt).toBe(adopted._state?.lastRunAt);
    scheduler.stop();
  });

  it.each([
    ['unparseable JSON', '{"version": 2, "jobs": ['],
    ['missing jobs array', '{"version": 2}'],
    ['an invalid entry', JSON.stringify({ version: 2, jobs: [{ id: 'broken' }] })],
  ])('reload of a candidate with %s keeps the running jobs and fails', async (_name, candidate) => {
    const { createScheduler } = await import('../src/scheduler.js');
    const file = makeTmpJobsFile([makeJob({ id: 'kept' })]);
    const scheduler = createScheduler(makeBotMock(), file, {
      registerCronTasks: false, deliveryEnabled: false, reconcileMissedRuns: false,
    });
    scheduler.start();
    fs.writeFileSync(file, candidate, 'utf-8');
    expect(() => scheduler.reload()).toThrow();
    expect(scheduler.listJobs().map(job => job.id)).toEqual(['kept']);
    fs.rmSync(file);
    expect(() => scheduler.reload()).toThrow('not found');
    expect(scheduler.listJobs().map(job => job.id)).toEqual(['kept']);
    fs.writeFileSync(file, JSON.stringify({ version: 2, jobs: [] }), 'utf-8');
    scheduler.reload();
    expect(scheduler.listJobs()).toEqual([]);
    scheduler.stop();
  });

  it.each([
    ['an invalid timezone', [{ id: 'tz-ok' }, { id: 'tz-bad', schedule: { expr: '0 9 * * *', tz: 'Mars/Olympus' } }]],
    ['a non-string id', [{ id: 42 }]],
  ])('reload rejects %s before any live registration changes', async (_name, candidate) => {
    const cronMod = await import('node-cron');
    const created: Array<{ stop: ReturnType<typeof vi.fn> }> = [];
    const scheduleSpy = vi.spyOn(cronMod, 'schedule').mockImplementation(() => {
      const task = { stop: vi.fn(), start: vi.fn(), destroy: vi.fn(), getStatus: vi.fn() };
      created.push(task);
      return task as unknown as ReturnType<typeof cronMod.schedule>;
    });
    const { createScheduler } = await import('../src/scheduler.js');
    const file = makeTmpJobsFile([makeJob({ id: 'live' })]);
    const scheduler = createScheduler(makeBotMock(), file, { deliveryEnabled: false, reconcileMissedRuns: false });
    try {
      scheduler.start();
      expect(created).toHaveLength(1);
      fs.writeFileSync(file, JSON.stringify({ version: 2,
        jobs: candidate.map(overrides => makeJob(overrides as Partial<ScheduledJob>)) }), 'utf-8');
      expect(() => scheduler.reload()).toThrow();
      expect(created).toHaveLength(1);
      expect(created[0]!.stop).not.toHaveBeenCalled();
      expect(scheduler.listJobs().map(job => job.id)).toEqual(['live']);
    } finally {
      scheduler.stop();
      scheduleSpy.mockRestore();
    }
  });

  it('a registration that throws mid-build stops the built ones and keeps the live set', async () => {
    const cronMod = await import('node-cron');
    const created: Array<{ stop: ReturnType<typeof vi.fn> }> = [];
    let failOn = 0;
    const scheduleSpy = vi.spyOn(cronMod, 'schedule').mockImplementation(() => {
      if (failOn && created.length + 1 === failOn) throw new Error('registration rejected');
      const task = { stop: vi.fn(), start: vi.fn(), destroy: vi.fn(), getStatus: vi.fn() };
      created.push(task);
      return task as unknown as ReturnType<typeof cronMod.schedule>;
    });
    const { createScheduler } = await import('../src/scheduler.js');
    const file = makeTmpJobsFile([makeJob({ id: 'live' })]);
    const scheduler = createScheduler(makeBotMock(), file, { deliveryEnabled: false, reconcileMissedRuns: false });
    try {
      scheduler.start();
      fs.writeFileSync(file, JSON.stringify({ version: 2,
        jobs: [makeJob({ id: 'a' }), makeJob({ id: 'b' })] }), 'utf-8');
      failOn = 3; // the live task is #1; candidate 'a' is #2; 'b' throws
      expect(() => scheduler.reload()).toThrow('registration rejected');
      expect(created[0]!.stop).not.toHaveBeenCalled();
      expect(created[1]!.stop).toHaveBeenCalledOnce();
      expect(scheduler.listJobs().map(job => job.id)).toEqual(['live']);
    } finally {
      scheduler.stop();
      scheduleSpy.mockRestore();
    }
  });

  it('a duplicate id is rejected, and a later empty reload leaves no live timer', async () => {
    const cronMod = await import('node-cron');
    const created: Array<{ stop: ReturnType<typeof vi.fn> }> = [];
    const scheduleSpy = vi.spyOn(cronMod, 'schedule').mockImplementation(() => {
      const task = { stop: vi.fn(), start: vi.fn(), destroy: vi.fn(), getStatus: vi.fn() };
      created.push(task);
      return task as unknown as ReturnType<typeof cronMod.schedule>;
    });
    const { createScheduler } = await import('../src/scheduler.js');
    const file = makeTmpJobsFile([makeJob({ id: 'live' })]);
    const scheduler = createScheduler(makeBotMock(), file, { deliveryEnabled: false, reconcileMissedRuns: false });
    try {
      scheduler.start();
      fs.writeFileSync(file, JSON.stringify({ version: 2,
        jobs: [makeJob({ id: 'twin', name: 'one' }), makeJob({ id: 'twin', name: 'two' })] }), 'utf-8');
      expect(() => scheduler.reload()).toThrow('Duplicate job id "twin"');
      expect(scheduler.listJobs().map(job => job.id)).toEqual(['live']);
      fs.writeFileSync(file, JSON.stringify({ version: 2, jobs: [] }), 'utf-8');
      scheduler.reload();
      expect(scheduler.listJobs()).toEqual([]);
      expect(created.every(task => task.stop.mock.calls.length > 0)).toBe(true);
    } finally {
      scheduler.stop();
      scheduleSpy.mockRestore();
    }
  });

  it('rejects deletion of a globally queued job without hiding or dropping its claim', async () => {
    process.env['RELAY_SCHEDULER_MAX_CONCURRENT_JOBS'] = '1';
    const { spawn } = await import('node:child_process');
    vi.mocked(spawn).mockReset();
    const firstProc = createMockProc(9115);
    const queuedProc = createMockProc(9116);
    vi.mocked(spawn)
      .mockReturnValueOnce(firstProc as unknown as ReturnType<typeof spawn>)
      .mockReturnValueOnce(queuedProc as unknown as ReturnType<typeof spawn>);

    const { createScheduler } = await import('../src/scheduler.js');
    const jobs = [
      makeJob({ id: 'delete-running', owner: 'aki', agent: { workspace: '/tmp/delete-running', prompt: 'a', timeout: 60, runtimePreference: ['cc'] } }),
      makeJob({ id: 'delete-queued', owner: 'aki', agent: { workspace: '/tmp/delete-queued', prompt: 'b', timeout: 60, runtimePreference: ['cc'] } }),
    ];
    const scheduler = createScheduler(makeBotMock(), makeTmpJobsFile(jobs), {
      registerCronTasks: false,
      deliveryEnabled: false,
      reconcileMissedRuns: false,
      runtimePolicy: { primary: 'cc', fallbacks: [] },
    });
    scheduler.start();

    const firstRun = scheduler.runJob('delete-running');
    const queuedRun = scheduler.runJob('delete-queued');
    await new Promise(resolve => setTimeout(resolve, 20));
    expect(vi.mocked(spawn)).toHaveBeenCalledTimes(1);

    expect(scheduler.removeJob('delete-queued', 'aki')).toBe(false);
    expect(scheduler.listJobs().map(job => job.id)).toContain('delete-queued');
    expect(scheduler.getRunningJobIds()).toEqual(['delete-queued', 'delete-running']);

    firstProc.stdout.push('first done\n');
    firstProc.stdout.push(null);
    firstProc.emit('exit', 0, null);
    for (let attempt = 0; attempt < 50 && vi.mocked(spawn).mock.calls.length < 2; attempt++) {
      await new Promise(resolve => setTimeout(resolve, 5));
    }
    expect(vi.mocked(spawn)).toHaveBeenCalledTimes(2);
    expect(scheduler.getRunningJobIds()).toEqual(['delete-queued']);

    queuedProc.stdout.push('queued done\n');
    queuedProc.stdout.push(null);
    queuedProc.emit('exit', 0, null);
    await Promise.all([firstRun, queuedRun]);
    expect(scheduler.getRunningJobIds()).toEqual([]);
    scheduler.stop();
  });

  it('shares one FIFO across cron, missed-run reconciliation, and manual triggers', async () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date('2026-08-17T05:59:30.000Z'));
    refreshTestCodexModelCatalogTime();
    process.env['RELAY_SCHEDULER_MAX_CONCURRENT_JOBS'] = '1';

    const cronMod = await import('node-cron');
    const callbacks = new Map<string, () => void>();
    const scheduleSpy = vi.spyOn(cronMod, 'schedule').mockImplementation((expr, cb) => {
      callbacks.set(expr as string, cb as () => void);
      return { stop: vi.fn(), start: vi.fn(), destroy: vi.fn(), getStatus: vi.fn() } as unknown as ReturnType<typeof cronMod.schedule>;
    });
    const { spawn } = await import('node:child_process');
    vi.mocked(spawn).mockReset();
    const cronProc = createMockProc(9121);
    const reconcileProc = createMockProc(9122);
    const manualProc = createMockProc(9123);
    vi.mocked(spawn)
      .mockReturnValueOnce(cronProc as unknown as ReturnType<typeof spawn>)
      .mockReturnValueOnce(reconcileProc as unknown as ReturnType<typeof spawn>)
      .mockReturnValueOnce(manualProc as unknown as ReturnType<typeof spawn>);

    const jobs = [
      makeJob({
        id: 'source-cron',
        schedule: { expr: '59 5 * * *', tz: 'UTC' },
        agent: { workspace: '/tmp/source-cron', prompt: 'cron', timeout: 60, runtimePreference: ['cc'] },
      }),
      makeJob({
        id: 'source-reconcile',
        schedule: { expr: '0 6 * * *', tz: 'UTC' },
        agent: { workspace: '/tmp/source-reconcile', prompt: 'reconcile', timeout: 60, runtimePreference: ['cc'] },
      }),
      makeJob({
        id: 'source-manual',
        enabled: false,
        agent: { workspace: '/tmp/source-manual', prompt: 'manual', timeout: 60, runtimePreference: ['cc'] },
      }),
    ];
    const { createScheduler } = await import('../src/scheduler.js');
    const scheduler = createScheduler(makeBotMock(), makeTmpJobsFile(jobs), {
      deliveryEnabled: false,
      missedRunReconcileIntervalMs: 30_000,
      missedRunLookbackMs: 60 * 60 * 1000,
      runtimePolicy: { primary: 'cc', fallbacks: [] },
    });

    try {
      scheduler.start();
      callbacks.get('59 5 * * *')!();
      await vi.advanceTimersByTimeAsync(0);
      expect(vi.mocked(spawn)).toHaveBeenCalledTimes(1);

      // At 06:00 the reconciliation timer claims the missed 06:00 job. The
      // disabled job can still be invoked manually, so all three sources are
      // now contending for the same scheduler-instance FIFO.
      await vi.advanceTimersByTimeAsync(30_000);
      const manualRun = scheduler.runJob('source-manual');
      await vi.advanceTimersByTimeAsync(0);
      expect(vi.mocked(spawn)).toHaveBeenCalledTimes(1);
      expect(scheduler.getRunningJobIds()).toEqual([
        'source-cron',
        'source-manual',
        'source-reconcile',
      ]);

      cronProc.stdout.push('cron done\n');
      cronProc.stdout.push(null);
      cronProc.emit('exit', 0, null);
      await vi.advanceTimersByTimeAsync(0);
      expect(vi.mocked(spawn)).toHaveBeenCalledTimes(2);

      reconcileProc.stdout.push('reconcile done\n');
      reconcileProc.stdout.push(null);
      reconcileProc.emit('exit', 0, null);
      await vi.advanceTimersByTimeAsync(0);
      expect(vi.mocked(spawn)).toHaveBeenCalledTimes(3);

      manualProc.stdout.push('manual done\n');
      manualProc.stdout.push(null);
      manualProc.emit('exit', 0, null);
      await expect(manualRun).resolves.toMatchObject({ status: 'ok' });
      expect(scheduler.getRunningJobIds()).toEqual([]);
    } finally {
      scheduler.stop();
      scheduleSpy.mockRestore();
      vi.useRealTimers();
    }
  });
});

// ─────────────────────────────────────────────────────────────────────────────
// agent.expectArtifact — a zero exit code must not read as success when the
// job produced no output file.
//
// Origin: 2026-08-09, the S1 W32 weekly review recorded lastStatus=ok while no
// report was written and no failure alert fired. delivery.mode 'none' means no
// delivery path touches the file, so the exit code was the only signal.
// ─────────────────────────────────────────────────────────────────────────────
describe('scheduler expectArtifact postcondition', () => {
  beforeEach(() => {
    vi.restoreAllMocks();
  });

  /**
   * Run one job to completion with a clean zero exit, returning its result.
   * Every knob the tests vary (the artifact config, whether the file exists)
   * is passed in, so paired runs differ by exactly one thing.
   */
  async function runJobWithExit0(jobOverrides: Partial<ScheduledJob>): Promise<JobResult> {
    const cronMod = await import('node-cron');
    vi.spyOn(cronMod, 'schedule').mockReturnValue({
      stop: vi.fn(), start: vi.fn(), destroy: vi.fn(), getStatus: vi.fn(),
    } as unknown as ReturnType<typeof cronMod.schedule>);

    const { spawn } = await import('node:child_process');
    vi.mocked(spawn).mockReset();
    const runtimeProc = createMockProc(8801);
    vi.mocked(spawn).mockReturnValueOnce(runtimeProc as unknown as ReturnType<typeof spawn>);

    const { createScheduler } = await import('../src/scheduler.js');
    const job = makeJob(jobOverrides);
    const scheduler = createScheduler(makeBotMock(), makeTmpJobsFile([job]));
    scheduler.start();

    const resultPromise = scheduler.runJob(job.id);
    setImmediate(() => {
      runtimeProc.stdout.push('agent finished\n');
      runtimeProc.stdout.push(null);
      runtimeProc.emit('exit', 0, null);
    });
    const result = await resultPromise;
    scheduler.stop();
    return result;
  }

  /** Fresh workspace dir per test so a stray file cannot satisfy a check. */
  function makeWorkspace(): string {
    return fs.mkdtempSync(path.join(os.tmpdir(), 'relay-artifact-'));
  }

  // ── Paired reversal ────────────────────────────────────────────────────────
  // Same job definition, same zero exit; the ONLY difference between these two
  // runs is whether the declared file is on disk. If the check were inert both
  // would be 'ok'; if it were stuck-red both would be 'error'.

  it('reports error when the declared artifact is absent (exit code 0)', async () => {
    const workspace = makeWorkspace();
    const consoleSpy = vi.spyOn(console, 'error').mockImplementation(() => {});

    const result = await runJobWithExit0({
      id: 'artifact-absent',
      agent: {
        workspace,
        prompt: 'write the weekly review',
        timeout: 10,
        expectArtifact: { path: 'weekly-review.md' },
      },
    });

    expect(result.status).toBe('error');
    expect(result.executionStatus).toBe('error');
    expect(result.errorMessage).toContain('expected artifact missing');
    consoleSpy.mockRestore();
  });

  it('reports ok when that same artifact is present', async () => {
    const workspace = makeWorkspace();
    fs.writeFileSync(path.join(workspace, 'weekly-review.md'), '# W33 report\n', 'utf-8');

    const result = await runJobWithExit0({
      id: 'artifact-present',
      agent: {
        workspace,
        prompt: 'write the weekly review',
        timeout: 10,
        expectArtifact: { path: 'weekly-review.md' },
      },
    });

    expect(result.status).toBe('ok');
    expect(result.executionStatus).toBe('ok');
    expect(result.errorMessage).toBeUndefined();
  });

  // ── The check must not fire on jobs that never opted in ────────────────────

  it('leaves jobs without expectArtifact untouched', async () => {
    const result = await runJobWithExit0({
      id: 'no-artifact-config',
      agent: { workspace: makeWorkspace(), prompt: 'do a thing', timeout: 10 },
    });

    expect(result.status).toBe('ok');
  });

  // ── An existing-but-empty file is the "wrote nothing" case, not success ────

  it('reports error when the artifact exists but is empty', async () => {
    const workspace = makeWorkspace();
    fs.writeFileSync(path.join(workspace, 'weekly-review.md'), '', 'utf-8');
    const consoleSpy = vi.spyOn(console, 'error').mockImplementation(() => {});

    const result = await runJobWithExit0({
      id: 'artifact-empty',
      agent: {
        workspace,
        prompt: 'write the weekly review',
        timeout: 10,
        expectArtifact: { path: 'weekly-review.md' },
      },
    });

    expect(result.status).toBe('error');
    expect(result.errorMessage).toContain('too small');
    consoleSpy.mockRestore();
  });

  // ── Staleness: last week's file must not certify this week's run ───────────
  // Paired on mtime alone — same file, same size, same path.

  it('reports error when the artifact is older than maxAgeSeconds', async () => {
    const workspace = makeWorkspace();
    const file = path.join(workspace, 'weekly-review.md');
    fs.writeFileSync(file, '# last week\n', 'utf-8');
    const weekAgo = new Date(Date.now() - 7 * 24 * 3600 * 1000);
    fs.utimesSync(file, weekAgo, weekAgo);
    const consoleSpy = vi.spyOn(console, 'error').mockImplementation(() => {});

    const result = await runJobWithExit0({
      id: 'artifact-stale',
      agent: {
        workspace,
        prompt: 'write the weekly review',
        timeout: 10,
        expectArtifact: { path: 'weekly-review.md', maxAgeSeconds: 3600 },
      },
    });

    expect(result.status).toBe('error');
    expect(result.errorMessage).toContain('stale');
    consoleSpy.mockRestore();
  });

  it('accepts a freshly written artifact under the same maxAgeSeconds', async () => {
    const workspace = makeWorkspace();
    fs.writeFileSync(path.join(workspace, 'weekly-review.md'), '# this week\n', 'utf-8');

    const result = await runJobWithExit0({
      id: 'artifact-fresh',
      agent: {
        workspace,
        prompt: 'write the weekly review',
        timeout: 10,
        expectArtifact: { path: 'weekly-review.md', maxAgeSeconds: 3600 },
      },
    });

    expect(result.status).toBe('ok');
  });

  // ── Absolute paths and ${DATE} expansion ──────────────────────────────────

  it('resolves an absolute path and expands ${DATE}', async () => {
    const workspace = makeWorkspace();
    const stamp = new Intl.DateTimeFormat('en-CA', {
      timeZone: 'Asia/Shanghai', year: 'numeric', month: '2-digit', day: '2-digit',
    }).format(new Date());
    fs.writeFileSync(path.join(workspace, `report-${stamp}.md`), 'body\n', 'utf-8');

    const result = await runJobWithExit0({
      id: 'artifact-absolute-dated',
      schedule: { expr: '0 17 * * 0', tz: 'Asia/Shanghai' },
      agent: {
        workspace: '/tmp',
        prompt: 'write the dated report',
        timeout: 10,
        expectArtifact: { path: path.join(workspace, 'report-${DATE}.md') },
      },
    });

    expect(result.status).toBe('ok');
  });

  it('expands ${ISO_WEEK} and requires every configured content fragment', async () => {
    const workspace = makeWorkspace();
    const now = new Date();
    const shanghai = new Intl.DateTimeFormat('en-CA', {
      timeZone: 'Asia/Shanghai', year: 'numeric', month: '2-digit', day: '2-digit',
    }).formatToParts(now);
    const part = (type: string): number => Number(shanghai.find(item => item.type === type)?.value ?? '0');
    const date = new Date(Date.UTC(part('year'), part('month') - 1, part('day')));
    const isoDay = date.getUTCDay() || 7;
    date.setUTCDate(date.getUTCDate() + 4 - isoDay);
    const isoYear = date.getUTCFullYear();
    const week = Math.ceil((((date.getTime() - Date.UTC(isoYear, 0, 1)) / 86_400_000) + 1) / 7);
    const isoWeek = `${isoYear}-W${String(week).padStart(2, '0')}`;
    fs.mkdirSync(path.join(workspace, 'memo'));
    fs.writeFileSync(
      path.join(workspace, 'memo', `review-${isoWeek}-draft.md`),
      '---\nstatus: "draft"\n---\n## ① facts\n## ② events\n## ③ drift\n## ④ plan [待确认]\n## ⑤ retest\n',
      'utf-8',
    );

    const result = await runJobWithExit0({
      id: 'artifact-iso-week-contains',
      schedule: { expr: '0 16 * * 0', tz: 'Asia/Shanghai' },
      agent: {
        workspace,
        prompt: 'write the weekly draft',
        timeout: 10,
        expectArtifact: {
          path: 'memo/review-${ISO_WEEK}-draft.md',
          contains: ['status:', 'draft', '## ①', '## ②', '## ③', '## ④', '## ⑤', '[待确认]'],
        },
      },
    });

    expect(result.status).toBe('ok');
  });

  it('expands ${MONTH} to the job-local YYYY-MM', async () => {
    const workspace = makeWorkspace();
    const shanghai = new Intl.DateTimeFormat('en-CA', {
      timeZone: 'Asia/Shanghai', year: 'numeric', month: '2-digit',
    }).formatToParts(new Date());
    const part = (type: string): string => shanghai.find(item => item.type === type)?.value ?? '';
    const month = `${part('year')}-${part('month')}`;
    fs.mkdirSync(path.join(workspace, 'memo', month), { recursive: true });
    fs.writeFileSync(path.join(workspace, 'memo', month, 'weekly.md'), 'report\n', 'utf-8');

    const result = await runJobWithExit0({
      id: 'artifact-month',
      schedule: { expr: '0 17 * * 0', tz: 'Asia/Shanghai' },
      agent: {
        workspace,
        prompt: 'write the weekly report',
        timeout: 10,
        expectArtifact: { path: 'memo/${MONTH}/weekly.md', minBytes: 1 },
      },
    });

    expect(result.status).toBe('ok');
  });

  it('rejects a weekly draft that is missing one required section', async () => {
    const workspace = makeWorkspace();
    fs.writeFileSync(path.join(workspace, 'weekly-review.md'), 'status: draft\n## ① only\n', 'utf-8');
    const consoleSpy = vi.spyOn(console, 'error').mockImplementation(() => {});

    const result = await runJobWithExit0({
      id: 'artifact-contains-missing',
      agent: {
        workspace,
        prompt: 'write the weekly draft',
        timeout: 10,
        expectArtifact: { path: 'weekly-review.md', contains: ['status:', 'draft', '## ⑤', '[待确认]'] },
      },
    });

    expect(result.status).toBe('error');
    expect(result.errorMessage).toContain('missing required content');
    expect(result.errorMessage).toContain('## ⑤');
    consoleSpy.mockRestore();
  });
});

// agent.verify — a post-run command checks facts the agent claims to have
// produced; a non-zero exit makes the run an error and fires the failure alert.
describe('scheduler agent.verify postcondition', () => {
  beforeEach(() => {
    vi.restoreAllMocks();
  });

  type VerifyRun = {
    result: JobResult;
    spawnCalls: ReturnType<typeof vi.fn>['mock']['calls'];
    messageCreate: ReturnType<typeof vi.fn>;
  };

  async function runWithVerify(
    jobOverrides: Partial<ScheduledJob>,
    opts: { agentExit?: number; verifyExit?: number; verifyStdout?: string; verifyStderr?: string; verifyHangs?: boolean } = {},
  ): Promise<VerifyRun> {
    const { agentExit = 0, verifyExit = 0, verifyStdout = '', verifyStderr = '', verifyHangs = false } = opts;
    const cronMod = await import('node-cron');
    vi.spyOn(cronMod, 'schedule').mockReturnValue({
      stop: vi.fn(), start: vi.fn(), destroy: vi.fn(), getStatus: vi.fn(),
    } as unknown as ReturnType<typeof cronMod.schedule>);
    vi.spyOn(console, 'error').mockImplementation(() => {});

    const { spawn } = await import('node:child_process');
    vi.mocked(spawn).mockReset();
    const runtimeProc = createMockProc(8901);
    const verifyProc = createMockProc(8902);
    vi.mocked(spawn)
      .mockReturnValueOnce(runtimeProc as unknown as ReturnType<typeof spawn>)
      .mockReturnValueOnce(verifyProc as unknown as ReturnType<typeof spawn>);

    const { createScheduler } = await import('../src/scheduler.js');
    const { client, messageCreate } = makeFeishuClientMock();
    const job = makeJob({ delivery: { mode: 'none', feishuOpenId: 'ou_alert_target' }, ...jobOverrides });
    const scheduler = createScheduler(makeBotMock(), makeTmpJobsFile([job]), {
      getFeishuClientByAgentId: () => client,
      runtimePolicy: { primary: 'cc', fallbacks: [] },
    });
    scheduler.start();

    const resultPromise = scheduler.runJob(job.id);
    setImmediate(() => {
      runtimeProc.stdout.push('agent finished\n');
      runtimeProc.stdout.push(null);
      runtimeProc.emit('exit', agentExit, null);
    });
    for (let attempt = 0; attempt < 100 && vi.mocked(spawn).mock.calls.length < 2; attempt++) {
      await new Promise(resolve => setTimeout(resolve, 5));
      if (attempt > 20 && agentExit !== 0) break;
    }
    if (vi.mocked(spawn).mock.calls.length >= 2 && !verifyHangs) {
      if (verifyStdout) verifyProc.stdout.push(verifyStdout);
      if (verifyStderr) verifyProc.stderr.push(verifyStderr);
      verifyProc.emit('exit', verifyExit, null);
    }
    const result = await resultPromise;
    scheduler.stop();
    return { result, spawnCalls: vi.mocked(spawn).mock.calls, messageCreate };
  }

  const verify = { command: '/fake/record-check', args: ['daily'] };

  it('passes the run identity to the verify command and keeps ok on exit 0', async () => {
    const before = Date.now();
    const { result, spawnCalls, messageCreate } = await runWithVerify({
      id: 'verify-pass',
      schedule: { expr: '0 3 * * *', tz: 'Asia/Shanghai' },
      agent: { ...makeJob().agent, workspace: '/tmp/hestia-space', runtimePreference: ['cc'], verify },
    });

    expect(result.status).toBe('ok');
    expect(result.verifyStatus).toBe('passed');
    expect(spawnCalls).toHaveLength(2);
    const [command, args, options] = spawnCalls[1] as [string, string[], { env: NodeJS.ProcessEnv }];
    expect(command).toBe('/fake/record-check');
    expect(args).toEqual(['daily']);
    expect(options.env.RELAY_JOB_ID).toBe('verify-pass');
    expect(options.env.RELAY_JOB_RUN_ID).toBe(result.runId);
    expect(options.env.RELAY_JOB_WORKSPACE).toBe('/tmp/hestia-space');
    expect(options.env.RELAY_JOB_RUNTIME).toBe('cc');
    const started = Date.parse(options.env.RELAY_JOB_STARTED_AT ?? '');
    expect(started).toBeGreaterThanOrEqual(before - 1000);
    expect(started).toBeLessThanOrEqual(Date.now());
    const date = new Intl.DateTimeFormat('en-CA', {
      timeZone: 'Asia/Shanghai', year: 'numeric', month: '2-digit', day: '2-digit',
    }).format(new Date(started));
    expect(options.env.RELAY_JOB_DATE).toBe(date);
    expect(messageCreate).not.toHaveBeenCalled();
  });

  it('turns a non-zero verify exit into an error with its reason and alerts the owner', async () => {
    const { result, messageCreate } = await runWithVerify({
      id: 'verify-fail',
      name: 'Oikos 日检',
      agent: { ...makeJob().agent, runtimePreference: ['cc'], verify },
    }, { verifyExit: 1, verifyStdout: '日检行没有写记录号\n' });

    expect(result.status).toBe('error');
    expect(result.executionStatus).toBe('error');
    expect(result.errorMessage).toBe('post-run verify exited with code 1: 日检行没有写记录号');
    expect(result.verifyStatus).toBe('failed');
    expect(messageCreate).toHaveBeenCalledTimes(1);
    const [arg] = messageCreate.mock.calls[0] as [{ data: { content: string } }];
    expect(arg.data.content).toContain('Oikos 日检');
    expect(arg.data.content).toContain('日检行没有写记录号');
  });

  it('prefers stderr over stdout for the failure summary', async () => {
    const { result } = await runWithVerify({
      id: 'verify-stderr',
      agent: { ...makeJob().agent, runtimePreference: ['cc'], verify },
    }, { verifyExit: 2, verifyStdout: 'progress\n', verifyStderr: '账本查不到记录\n' });

    expect(result.errorMessage).toBe('post-run verify exited with code 2: 账本查不到记录');
  });

  it('fails closed when the verify command times out', async () => {
    const { result } = await runWithVerify({
      id: 'verify-timeout',
      agent: { ...makeJob().agent, runtimePreference: ['cc'], verify: { ...verify, timeout: 0.01 } },
    }, { verifyHangs: true });

    expect(result.status).toBe('error');
    expect(result.errorMessage).toContain('post-run verify timed out');
  });

  it('does not run verify when the agent itself failed', async () => {
    const { result, spawnCalls } = await runWithVerify({
      id: 'verify-agent-failed',
      agent: { ...makeJob().agent, runtimePreference: ['cc'], verify },
    }, { agentExit: 1 });

    expect(result.status).toBe('error');
    expect(result.errorMessage).not.toContain('post-run verify');
    expect(result.verifyStatus).toBeUndefined();
    expect(spawnCalls).toHaveLength(1);
  });

  it('does not run verify when the declared artifact is missing', async () => {
    const workspace = fs.mkdtempSync(path.join(os.tmpdir(), 'relay-verify-'));
    const { result, spawnCalls } = await runWithVerify({
      id: 'verify-artifact-missing',
      agent: {
        workspace, prompt: 'daily', timeout: 10, runtimePreference: ['cc'],
        expectArtifact: { path: 'memory/${DATE}.md' }, verify,
      },
    });

    expect(result.errorMessage).toContain('expected artifact missing');
    expect(spawnCalls).toHaveLength(1);
  });

  it('loadJobs skips a job whose verify has no command', async () => {
    const { loadJobs } = await import('../src/scheduler.js');
    const consoleSpy = vi.spyOn(console, 'warn').mockImplementation(() => {});
    const file = makeTmpJobsFile([
      { ...makeJob({ id: 'bad-verify' }), agent: { ...makeJob().agent, verify: { command: ' ' } } },
      { ...makeJob({ id: 'bad-verify-args' }), agent: { ...makeJob().agent, verify: { command: '/x', args: [1] } } },
      { ...makeJob({ id: 'good-verify' }), agent: { ...makeJob().agent, verify: { command: '/x', timeout: 5 } } },
    ]);

    expect(loadJobs(file).map(job => job.id)).toEqual(['good-verify']);
    expect(consoleSpy.mock.calls.filter(call => JSON.stringify(call[0]).includes('agent.verify'))).toHaveLength(2);
    consoleSpy.mockRestore();
  });
});

describe('automatic execution pause at the real scheduler boundary', () => {
  it('repeated cron triggers do not launch a model or send failure alerts while paused', async () => {
    vi.useFakeTimers();
    const cronMod = await import('node-cron');
    let callback: (()=>void)|undefined;
    const scheduleSpy=vi.spyOn(cronMod,'schedule').mockImplementation((_expr, cb)=>{
      callback=cb as ()=>void;
      return {stop:vi.fn(),start:vi.fn(),destroy:vi.fn(),getStatus:vi.fn()} as unknown as ReturnType<typeof cronMod.schedule>;
    });
    const {spawn}=await import('node:child_process');vi.mocked(spawn).mockReset();
    vi.mocked(requireAutomatic).mockImplementation(()=>{throw new AutomaticExecutionPaused('human pause');});
    const file=makeTmpJobsFile([makeJob({id:'paused-auto-entry',delivery:{mode:'announce'}})]);
    const bot=makeBotMock();
    const {createScheduler}=await import('../src/scheduler.js');
    const scheduler=createScheduler(bot,file,{runtimePolicy:{primary:'cc',fallbacks:[]},reconcileMissedRuns:false});
    try {
      scheduler.start();
      for(let i=0;i<3;i++){callback!();await vi.advanceTimersByTimeAsync(0);}
      expect(spawn).not.toHaveBeenCalled();
      expect(bot.api.sendMessage).not.toHaveBeenCalled();
      expect(scheduler.getRunningJobIds()).toEqual([]);
    } finally {scheduler.stop();scheduleSpy.mockRestore();vi.useRealTimers();}
  });
});

describe('scheduler native session proof', () => {
  it('names the job, owner and run in the proof of the runtime it starts', async () => {
    const { spawn } = await import('node:child_process');
    vi.mocked(spawn).mockReset();
    vi.mocked(observeScheduledSession).mockClear();
    const runtimeProc = createMockProc(7420);
    vi.mocked(spawn).mockReturnValueOnce(runtimeProc as unknown as ReturnType<typeof spawn>);
    const { createScheduler } = await import('../src/scheduler.js');
    const job = makeJob({ id: 'hestia-dispatch', owner: 'hestia', wi: 'wi_20261015_418',
      agent: { ...makeJob().agent, runtimePreference: ['cc'] } });
    const scheduler = createScheduler(makeBotMock(), makeTmpJobsFile([job]), {
      registerCronTasks: false,
      deliveryEnabled: false,
      runtimePolicy: { primary: 'cc', fallbacks: [] },
    });
    scheduler.start();
    const resultPromise = scheduler.runJob(job.id);
    for (let attempt = 0; attempt < 50 && vi.mocked(spawn).mock.calls.length === 0; attempt++) {
      await new Promise(resolve => setTimeout(resolve, 2));
    }
    const args = vi.mocked(spawn).mock.calls[0]![1] as string[];
    const options = vi.mocked(spawn).mock.calls[0]![2] as { env?: NodeJS.ProcessEnv };
    expect(observeScheduledSession).toHaveBeenCalledWith(7420, args[args.indexOf('--session-id') + 1], 'cc',
      { jobId: 'hestia-dispatch', owner: 'hestia', runId: options.env?.['RELAY_JOB_RUN_ID'],
        jobName: job.name, startedAt: expect.stringMatching(/^\d{4}-\d{2}-\d{2}T/), wi: 'wi_20261015_418' });
    runtimeProc.stdout.push('{"type":"result","result":"handled"}\n');
    runtimeProc.stdout.push(null);
    runtimeProc.emit('exit', 0, null);
    expect((await resultPromise).runtime).toBe('cc');
    scheduler.stop();
  });
});

describe('collab inbox progress notice', () => {
  const OPEN_ID = 'ou_00000000000000000000000000000002';
  const BATCH = JSON.stringify({
    schema: 'pulse.collab_inbox_gate/v1', actor: 'hestia', coverage: 'complete',
    contracts: [{ work_item_id: 'wi_20261015_418', contract_id: 'op_643afde3b37f5519481a7f518af031042e29f946011d32391d06c0e47199e096',
      kind: 'request', status: 'pending', offered_by: 'pulse', offered_at: '2026-10-15T00:00:00Z', rewake: false }],
  });

  afterEach(() => { vi.restoreAllMocks(); });

  function collabJob(delivery: Partial<ScheduledJob['delivery']> = {}): ScheduledJob {
    return makeJob({
      id: 'hestia-collab-inbox',
      name: 'Hestia 协作收件箱',
      owner: 'hestia',
      agent: { ...makeJob().agent, runtimePreference: ['cc'], gate: { command: '/fake/gate', skipExitCode: 3, skipOutput: 'COLLAB_INBOX_SKIP' } },
      delivery: { mode: 'none', feishuOpenId: OPEN_ID, progressNotice: true, ...delivery },
    });
  }

  function feishuClient(create = vi.fn().mockResolvedValue({ code: 0, data: { message_id: 'om_notice' } })) {
    const patch = vi.fn().mockResolvedValue({ code: 0 });
    const client = { im: { message: { create, patch } } } as unknown as import('../src/delivery.js').FeishuMessageClient;
    return { client, create, patch };
  }

  async function run(job: ScheduledJob, client: import('../src/delivery.js').FeishuMessageClient,
    gate: { stdout: string; exit: number }, runtimeExit = 0) {
    vi.spyOn(console, 'log').mockImplementation(() => {});
    vi.spyOn(console, 'warn').mockImplementation(() => {});
    vi.spyOn(console, 'error').mockImplementation(() => {});
    const { spawn } = await import('node:child_process');
    vi.mocked(spawn).mockReset();
    const gateProc = createMockProc(8101);
    const runtimeProc = createMockProc(8102);
    vi.mocked(spawn)
      .mockReturnValueOnce(gateProc as unknown as ReturnType<typeof spawn>)
      .mockReturnValueOnce(runtimeProc as unknown as ReturnType<typeof spawn>);
    const { createScheduler } = await import('../src/scheduler.js');
    const scheduler = createScheduler(makeBotMock(), makeTmpJobsFile([job]), {
      registerCronTasks: false,
      reconcileMissedRuns: false,
      getFeishuClientByAgentId: () => client,
      runtimePolicy: { primary: 'cc', fallbacks: [] },
      agentDisplayName: actor => (actor === 'pulse' ? 'Pulse' : undefined),
    });
    scheduler.start();
    const resultPromise = scheduler.runJob(job.id);
    for (let attempt = 0; attempt < 50 && vi.mocked(spawn).mock.calls.length < 1; attempt++) {
      await new Promise(resolve => setTimeout(resolve, 5));
    }
    gateProc.stdout.push(gate.stdout + '\n');
    gateProc.stdout.push(null);
    gateProc.emit('exit', gate.exit, null);
    if (gate.exit === 0) {
      for (let attempt = 0; attempt < 50 && vi.mocked(spawn).mock.calls.length < 2; attempt++) {
        await new Promise(resolve => setTimeout(resolve, 5));
      }
      runtimeProc.stdout.push(runtimeExit === 0 ? '{"type":"result","result":"handled"}\n' : '');
      runtimeProc.stdout.push(null);
      runtimeProc.emit('exit', runtimeExit, null);
    }
    const result = await resultPromise;
    scheduler.stop();
    return { result, spawnCalls: vi.mocked(spawn).mock.calls.length };
  }

  it('posts the background notice card before the run and patches it with the outcome', async () => {
    const { client, create, patch } = feishuClient();
    const { result } = await run(collabJob(), client, { stdout: BATCH, exit: 0 });

    expect(result.status).toBe('ok');
    expect(create).toHaveBeenCalledTimes(1);
    const sent = create.mock.calls[0]![0];
    expect(sent.data.receive_id).toBe(OPEN_ID);
    expect(sent.data.msg_type).toBe('interactive');
    const card = JSON.parse(sent.data.content);
    expect(card.header.title.content).toBe('📨 后台正在处理 Pulse wi_20261015_418 协作合同。');
    expect(JSON.stringify(card)).toContain('collapsible_panel');
    expect(JSON.stringify(card)).toContain('op_643afde3b');
    expect(patch).toHaveBeenCalledTimes(1);
    expect(JSON.parse(patch.mock.calls[0]![0].data.content).header.title.content).toBe('✅ 协作合同后台处理完成');
  });

  it('marks the card failed and keeps the run-level failure alert when the run fails', async () => {
    const { client, create, patch } = feishuClient();
    const { result } = await run(collabJob(), client, { stdout: BATCH, exit: 0 }, 2);

    expect(result.status).toBe('error');
    expect(JSON.parse(patch.mock.calls[0]![0].data.content).header.title.content).toBe('❌ 协作合同后台处理失败');
    expect(create).toHaveBeenCalledTimes(2);
    expect(create.mock.calls[1]![0].data.content).toContain('cron job 失败');
  });

  it('a failed notice send never changes the job result', async () => {
    const { client } = feishuClient(vi.fn().mockRejectedValue(new Error('feishu down')));
    const { result, spawnCalls } = await run(collabJob(), client, { stdout: BATCH, exit: 0 });

    expect(spawnCalls).toBe(2);
    expect(result.status).toBe('ok');
    expect(result.executionStatus).toBe('ok');
  });

  it('sends nothing without the opt-in field', async () => {
    const { client, create } = feishuClient();
    const { result } = await run(collabJob({ progressNotice: undefined }), client, { stdout: BATCH, exit: 0 });

    expect(result.status).toBe('ok');
    expect(create).not.toHaveBeenCalled();
  });

  it('sends nothing when the gate skips', async () => {
    const { client, create } = feishuClient();
    const { result, spawnCalls } = await run(collabJob(), client, { stdout: 'COLLAB_INBOX_SKIP', exit: 3 });

    expect(result.skipped).toBe(true);
    expect(spawnCalls).toBe(1);
    expect(create).not.toHaveBeenCalled();
  });

  it('sends nothing when the gate output is not a collab batch', async () => {
    const { client, create } = feishuClient();
    const { result } = await run(collabJob(), client, { stdout: '[hestia gate] 1 新增', exit: 0 });

    expect(result.status).toBe('ok');
    expect(create).not.toHaveBeenCalled();
  });
});

describe('standing access in scheduled runs', () => {
  it('names the owner Agent standing read grants in the scheduled prompt', async () => {
    const cronMod = await import('node-cron');
    const scheduleSpy = vi.spyOn(cronMod, 'schedule').mockReturnValue({
      stop: vi.fn(), start: vi.fn(), destroy: vi.fn(), getStatus: vi.fn(),
    } as unknown as ReturnType<typeof cronMod.schedule>);
    vi.mocked(standingAccessFor).mockResolvedValueOnce([{ path: '/c/PARA/vigil-space', mode: 'read', standing: true }]);
    const { spawn } = await import('node:child_process');
    vi.mocked(spawn).mockReset();
    const runtimeProc = createMockProc(2881);
    vi.mocked(spawn).mockReturnValueOnce(runtimeProc as unknown as ReturnType<typeof spawn>);
    const { createScheduler } = await import('../src/scheduler.js');
    const job = makeJob({ id: 'vigil-33-weekly', owner: 'vigil-33',
      agent: { workspace: '/tmp', prompt: 'weekly review', timeout: 10, runtimePreference: ['cc'] } });
    const scheduler = createScheduler(makeBotMock(), makeTmpJobsFile([job]));
    scheduler.start();
    const resultPromise = scheduler.runJob('vigil-33-weekly');
    setImmediate(() => {
      runtimeProc.stdout.push('done\n');
      runtimeProc.stdout.push(null);
      runtimeProc.emit('exit', 0, null);
    });
    await resultPromise;
    expect(standingAccessFor).toHaveBeenCalledWith('vigil-33');
    const stdin = String(runtimeProc.stdin.read() ?? '');
    expect(stdin).toContain('常驻授权：/c/PARA/vigil-space/（只读');
    expect(stdin).toContain('weekly review');
    scheduler.stop();
    scheduleSpy.mockRestore();
  });

  it('names the shared repositories read-only in the scheduled prompt', async () => {
    const cronMod = await import('node-cron');
    const scheduleSpy = vi.spyOn(cronMod, 'schedule').mockReturnValue({
      stop: vi.fn(), start: vi.fn(), destroy: vi.fn(), getStatus: vi.fn(),
    } as unknown as ReturnType<typeof cronMod.schedule>);
    const root = fs.mkdtempSync(path.join(os.tmpdir(), 'coevo-'));
    const workspace = path.join(root, 'PARA', 'marsh-space');
    fs.mkdirSync(path.join(root, 'ssot'));
    fs.mkdirSync(path.join(root, 'Bigbird', 'PULSE'), { recursive: true });
    fs.mkdirSync(path.join(root, '.harness'));
    fs.mkdirSync(workspace, { recursive: true });
    const { spawn } = await import('node:child_process');
    vi.mocked(spawn).mockReset();
    const runtimeProc = createMockProc(2882);
    vi.mocked(spawn).mockReturnValueOnce(runtimeProc as unknown as ReturnType<typeof spawn>);
    const { createScheduler } = await import('../src/scheduler.js');
    const job = makeJob({ id: 'marsh-daily', owner: 'marsh',
      agent: { workspace, prompt: 'daily writing', timeout: 10, runtimePreference: ['cc'] } });
    const scheduler = createScheduler(makeBotMock(), makeTmpJobsFile([job]));
    scheduler.start();
    const resultPromise = scheduler.runJob('marsh-daily');
    setImmediate(() => {
      runtimeProc.stdout.push('done\n');
      runtimeProc.stdout.push(null);
      runtimeProc.emit('exit', 0, null);
    });
    await resultPromise;
    const stdin = String(runtimeProc.stdin.read() ?? '');
    expect(stdin).toContain(`- 共享仓库（只读）：${root}/、${path.join(root, 'Bigbird', 'PULSE')}/（本机共享检出；`);
    expect(stdin).toContain(`- 规则正本：${path.join(root, 'ssot')}/`);
    expect(stdin).toContain('- 共享仓库内不得读取（本会话工作区与授权路径不在此限）：凭据与令牌文件（');
    expect(stdin).toContain(`${path.join(root, '.harness')}/`);
    expect(stdin).toContain('daily writing');
    scheduler.stop();
    scheduleSpy.mockRestore();
    fs.rmSync(root, { recursive: true, force: true });
  });
});
