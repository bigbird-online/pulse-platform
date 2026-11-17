import { describe, it, expect, vi } from 'vitest';
import { handleListJobs, handleAddJob, handleDelJob, handleRunJob } from '../src/job-commands.js';
import type { Scheduler, ScheduledJob, JobResult } from '../src/types.js';

// helper: create mock scheduler
function mockScheduler(jobs: ScheduledJob[] = []): Scheduler {
  return {
    start: vi.fn(),
    stop: vi.fn(),
    listJobs: vi.fn((owner?: string) => {
      if (!owner) return [...jobs];
      return jobs.filter(j => j.owner === owner || j.owner === 'system');
    }),
    addJob: vi.fn((input) => ({ ...input, _state: { consecutiveErrors: 0 } })),
    removeJob: vi.fn((id: string, owner: string) => {
      const job = jobs.find(j => j.id === id);
      if (!job) return false;
      return job.owner === owner;
    }),
    runJob: vi.fn(async () => ({
      jobId: 'test', status: 'ok' as const, output: 'done', durationMs: 1234, errorMessage: undefined,
    })),
  };
}

// test job fixture
function makeJob(overrides: Partial<ScheduledJob> = {}): ScheduledJob {
  return {
    id: 'test-job', name: 'Test', enabled: true, owner: 'aki',
    schedule: { expr: '0 6 * * *', tz: 'Asia/Shanghai' },
    agent: { workspace: '/tmp', prompt: 'test', timeout: 300 },
    delivery: { mode: 'none' },
    _state: { consecutiveErrors: 0 },
    ...overrides,
  };
}

describe('handleListJobs', () => {
  it('returns formatted list with job details', () => {
    const job = makeJob({ _state: { consecutiveErrors: 0, lastRunAt: '2026-01-01', lastStatus: 'ok' } });
    const scheduler = mockScheduler([job]);
    const result = handleListJobs('aki', scheduler);
    expect(result).toContain('Test');
    expect(result).toContain('0 6 * * *');
    expect(result).toContain('Asia/Shanghai');
    expect(result).toContain('test-job');
  });

  it('returns empty message when no jobs', () => {
    const scheduler = mockScheduler([]);
    const result = handleListJobs('new-agent', scheduler);
    expect(result).toBe('\u6682\u65E0\u8BA1\u5212\u4EFB\u52A1\u3002');
  });
});

describe('handleAddJob', () => {
  it('parses valid cron + prompt and calls addJob', () => {
    const scheduler = mockScheduler();
    const result = handleAddJob('aki', '/tmp', '123', '0 6 * * * | prompt text', scheduler);
    expect(scheduler.addJob).toHaveBeenCalledOnce();
    expect(result).toContain('\u2705');
    expect(result).toContain('0 6 * * *');
    const call = (scheduler.addJob as ReturnType<typeof vi.fn>).mock.calls[0][0];
    expect(call.agent.prompt).toBe('prompt text');
    expect(call.schedule.expr).toBe('0 6 * * *');
  });

  it('parses optional name/timeout/delivery params', () => {
    const scheduler = mockScheduler();
    const result = handleAddJob('aki', '/tmp', '123', '0 6 * * * | my prompt | name=custom | timeout=120 | delivery=announce', scheduler);
    expect(result).toContain('\u2705');
    const call = (scheduler.addJob as ReturnType<typeof vi.fn>).mock.calls[0][0];
    expect(call.name).toBe('custom');
    expect(call.agent.timeout).toBe(120);
    expect(call.delivery.mode).toBe('announce');
    expect(call.delivery.channel).toBe('telegram');
    expect(call.delivery.to).toBe('123');
  });

  it('returns error for invalid cron (less than 5 fields)', () => {
    const scheduler = mockScheduler();
    const result = handleAddJob('aki', '/tmp', '123', 'invalid | prompt', scheduler);
    expect(result).toContain('\u274C');
    expect(result).toContain('5');
    expect(scheduler.addJob).not.toHaveBeenCalled();
  });

  it('returns usage when args empty', () => {
    const scheduler = mockScheduler();
    const result = handleAddJob('aki', '/tmp', '123', '', scheduler);
    expect(result).toContain('/job_add');
    expect(scheduler.addJob).not.toHaveBeenCalled();
  });

  it('returns error when prompt is missing (5-field cron only)', () => {
    const scheduler = mockScheduler();
    const result = handleAddJob('aki', '/tmp', '123', '0 6 * * *', scheduler);
    expect(result).toContain('\u274C');
    expect(result).toContain('prompt');
    expect(scheduler.addJob).not.toHaveBeenCalled();
  });

  it('returns error when scheduler.addJob throws', () => {
    const scheduler = mockScheduler();
    (scheduler.addJob as ReturnType<typeof vi.fn>).mockImplementationOnce(() => { throw new Error('duplicate id'); });
    const result = handleAddJob('aki', '/tmp', '123', '0 6 * * * | my prompt', scheduler);
    expect(result).toContain('\u274C');
    expect(result).toContain('duplicate id');
  });
});

describe('handleDelJob', () => {
  it('returns success when job deleted', () => {
    const job = makeJob({ owner: 'aki' });
    const scheduler = mockScheduler([job]);
    const result = handleDelJob('aki', 'test-job', scheduler);
    expect(result).toContain('\u2705');
    expect(result).toContain('test-job');
  });

  it('returns permission error when job belongs to another owner', () => {
    const job = makeJob({ owner: 'other-agent' });
    const scheduler = mockScheduler([job]);
    const result = handleDelJob('aki', 'test-job', scheduler);
    expect(result).toContain('\u274C');
    expect(result).toContain('\u4E0D\u5C5E\u4E8E\u4F60');
  });

  it('returns usage when jobId is empty', () => {
    const scheduler = mockScheduler();
    const result = handleDelJob('aki', '', scheduler);
    expect(result).toContain('/job_del');
  });

  it('returns not-found when job does not exist', () => {
    const scheduler = mockScheduler([]);
    const result = handleDelJob('aki', 'ghost-job', scheduler);
    expect(result).toContain('\u274C');
    expect(result).toContain('\u672A\u627E\u5230');
  });
});

describe('handleRunJob', () => {
  it('returns execution summary on success', async () => {
    const job = makeJob({ owner: 'aki' });
    const scheduler = mockScheduler([job]);
    const result = await handleRunJob('aki', 'test-job', scheduler);
    expect(result).toContain('\u2705');
    expect(result).toContain('test-job');
    expect(result).toContain('ok');
    expect(result).toContain('1.2'); // 1234ms = 1.2s
  });

  it('returns not-found when job does not exist', async () => {
    const scheduler = mockScheduler([]);
    const result = await handleRunJob('aki', 'nonexistent', scheduler);
    expect(result).toContain('\u274C');
    expect(result).toContain('\u672A\u627E\u5230');
  });

  it('returns usage when jobId is empty', async () => {
    const scheduler = mockScheduler();
    const result = await handleRunJob('aki', '', scheduler);
    expect(result).toContain('/job_run');
  });

  it('returns error message when runJob throws', async () => {
    const job = makeJob({ owner: 'aki' });
    const scheduler = mockScheduler([job]);
    (scheduler.runJob as ReturnType<typeof vi.fn>).mockRejectedValueOnce(new Error('spawn failed'));
    const result = await handleRunJob('aki', 'test-job', scheduler);
    expect(result).toContain('\u274C');
    expect(result).toContain('spawn failed');
  });
});

describe('command-menu integration', () => {
  it('getAllMenuCommands includes scheduler commands', async () => {
    const { getAllMenuCommands } = await import('../src/command-menu.js');
    const cmds = getAllMenuCommands();
    const names = cmds.map(c => c.command);
    expect(names).toContain('jobs');
    expect(names).toContain('job_add');
    expect(names).toContain('job_del');
    expect(names).toContain('job_run');
  });
});
