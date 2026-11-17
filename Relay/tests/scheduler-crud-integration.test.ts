import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { mkdtempSync, writeFileSync, readFileSync, rmSync } from 'node:fs';
import { join } from 'node:path';
import { tmpdir } from 'node:os';
import { createScheduler } from '../src/scheduler.js';
import type { Bot } from 'grammy';

describe('Scheduler CRUD Integration', () => {
  let tmpDir: string;
  let configPath: string;
  let mockBot: Bot;

  beforeEach(() => {
    tmpDir = mkdtempSync(join(tmpdir(), 'relay-crud-'));
    configPath = join(tmpDir, 'cron-jobs.json');
    process.env['CRON_JOBS_STATE_PATH'] = join(tmpDir, 'cron-jobs-state.json');
    mockBot = { api: { sendMessage: vi.fn() } } as unknown as Bot;
  });

  afterEach(() => {
    delete process.env['CRON_JOBS_STATE_PATH'];
    rmSync(tmpDir, { recursive: true, force: true });
  });

  it('should complete full CRUD lifecycle: add → list → run → del', async () => {
    // 1. 创建空的 v2 JSON
    writeFileSync(configPath, JSON.stringify({ version: 2, jobs: [] }));

    // 2. 启动 scheduler
    const scheduler = createScheduler(mockBot, configPath);
    scheduler.start();

    // 3. addJob
    const job = scheduler.addJob({
      id: 'test-crud',
      name: 'CRUD Test',
      enabled: true,
      owner: 'aki',
      schedule: { expr: '0 6 * * *', tz: 'Asia/Shanghai' },
      agent: { workspace: '/tmp', prompt: 'test prompt', timeout: 60 },
      delivery: { mode: 'none' },
    });
    expect(job.id).toBe('test-crud');
    expect(job._state?.consecutiveErrors).toBe(0);

    // 4. listJobs — 应可见
    const listed = scheduler.listJobs('aki');
    expect(listed.some(j => j.id === 'test-crud')).toBe(true);

    // 5. listJobs — 其他 owner 不可见（除非 system）
    const otherList = scheduler.listJobs('ken');
    expect(otherList.some(j => j.id === 'test-crud')).toBe(false);

    // 6. removeJob — 错误 owner 应拒绝
    expect(scheduler.removeJob('test-crud', 'ken')).toBe(false);

    // 7. removeJob — 正确 owner 应成功
    expect(scheduler.removeJob('test-crud', 'aki')).toBe(true);

    // 8. 验证 JSON 持久化（等 debounce 200ms + margin）
    await new Promise(r => setTimeout(r, 350));
    const persisted = JSON.parse(readFileSync(configPath, 'utf-8'));
    expect(persisted.jobs.find((j: any) => j.id === 'test-crud')).toBeUndefined();

    // 9. 清理
    scheduler.stop();
  });

  it('should migrate v1 JSON and support CRUD on migrated jobs', () => {
    // v1 JSON（无 owner 字段）
    const v1Data = {
      version: 1,
      jobs: [{
        id: 'legacy-job',
        name: 'Legacy',
        enabled: true,
        schedule: { expr: '0 8 * * *', tz: 'Asia/Shanghai' },
        agent: { workspace: '/tmp', prompt: 'legacy task', timeout: 300 },
        delivery: { mode: 'none' },
      }],
    };
    writeFileSync(configPath, JSON.stringify(v1Data));

    const scheduler = createScheduler(mockBot, configPath);
    scheduler.start();

    // 迁移后可 list
    const jobs = scheduler.listJobs();
    expect(jobs[0].owner).toBe('system');

    // system job 不可被非管理员删除
    expect(scheduler.removeJob('legacy-job', 'aki')).toBe(false);

    // 可以新增自己的 job
    scheduler.addJob({
      id: 'new-job', name: 'New', enabled: true, owner: 'aki',
      schedule: { expr: '30 6 * * *', tz: 'Asia/Shanghai' },
      agent: { workspace: '/tmp', prompt: 'new task' },
      delivery: { mode: 'none' },
    });

    const allJobs = scheduler.listJobs('aki');
    expect(allJobs.length).toBe(2); // legacy (system) + new (aki)

    scheduler.stop();
  });
});
