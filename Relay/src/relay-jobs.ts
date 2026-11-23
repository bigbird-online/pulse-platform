#!/usr/bin/env node
// relay-jobs — CLI for managing Relay scheduled jobs.
// CRUD remains JSON-backed. `run` asks the resident Relay to run the job
// (POST /internal/jobs/run), so it delivers like cron and Telegram /job_run;
// `run --local` reuses scheduler.runJob() in this process without delivery.

import { readFileSync, existsSync } from 'node:fs';
import { pathToFileURL } from 'node:url';
import type { Bot } from 'grammy';
import { createScheduler } from './scheduler.js';
import { mergeCronJobsState, resolveCronJobsStatePath } from './job-store.js';
import { resolveConfigPath, resolveCronJobsPath } from './config-paths.js';
import { createRouter } from './router.js';
import { ownerIsolationFromRouter, type OwnerIsolation } from './agent-isolation.js';
import { resolvePulseRuntimePriority } from './codex-defaults.js';
import { writeCronJobsConfig } from './runtime-artifact-receipt.js';
import { resolveRelayHttpHost, resolveRelayHttpPort } from './http-server.js';

const NOOP_BOT = {
  api: {
    sendMessage: async () => undefined,
  },
} as unknown as Bot;

// 曾在此写 `import.meta.dirname/../config/cron-jobs.json`——而 config/ 下只有
// cron-jobs.sample.json，裸跑读不到真配置。现走共享真源 resolveCronJobsPath()。
const CRON_JOBS_PATH = resolveCronJobsPath();
const CRON_JOBS_STATE_PATH = resolveCronJobsStatePath(CRON_JOBS_PATH);

function loadJobs(): { version: number; jobs: any[] } {
  if (!existsSync(CRON_JOBS_PATH)) {
    return { version: 2, jobs: [] };
  }
  const data = JSON.parse(readFileSync(CRON_JOBS_PATH, 'utf-8'));
  return { ...data, jobs: mergeCronJobsState(data.jobs ?? [], CRON_JOBS_STATE_PATH) };
}

function saveJobs(data: { version: number; jobs: any[] }): void {
  const jobs = data.jobs.map(({ _state, ...job }) => {
    void _state;
    return job;
  });
  writeCronJobsConfig(CRON_JOBS_PATH, { ...data, jobs }, 'relay-jobs-cli');
}

// The resident Relay adopts cron-jobs.json only through the managed reload,
// in both placement and legacy mode; a signal to a PID file is not an adoption
// path. Say so instead of implying the change is live.
export const ADOPTION_NOTICE = '配置已写入，常驻 Relay 尚未采用：执行 `pulse-relay reload` 后生效'
  + '（正在运行的任务跑完本轮、不被中断；node-placement 模式有任务在跑时重载被拒，稍后重试）；'
  + '自动运行另需 PULSE 在 automation-control.json 登记该任务。';

function notifyRelay(): void {
  console.warn(ADOPTION_NOTICE);
}

function parseArgs(argv: string[]): Record<string, string> {
  const result: Record<string, string> = {};
  for (let i = 0; i < argv.length; i++) {
    if (argv[i].startsWith('--') && i + 1 < argv.length) {
      const key = argv[i].slice(2);
      result[key] = argv[i + 1];
      i++;
    }
  }
  return result;
}

function usage(): number {
  console.log(`用法:
  relay-jobs list [--owner <agentId>]
  relay-jobs add --cron "<expr>" --prompt "<text>" [--name <name>] [--timeout <sec>] [--owner <agentId>] [--workspace <path>] [--inject <file>] [--delivery announce --to <chatId>]
  relay-jobs del <id> --owner <agentId>
  relay-jobs run <id> [--local]
`);
  return 1;
}

function cmdList(opts: Record<string, string>): number {
  const data = loadJobs();
  let jobs = data.jobs;
  if (opts.owner) {
    jobs = jobs.filter((j: any) => j.owner === opts.owner || j.owner === 'system');
  }
  if (jobs.length === 0) {
    console.log('暂无计划任务。');
    return 0;
  }
  for (const job of jobs) {
    const status = job.enabled ? '✅' : '⏸';
    const owner = job.owner === 'system' ? '[system]' : `[${job.owner}]`;
    const delivery = job._state?.lastResult?.deliveryStatus;
    const lastStatus = (job._state?.lastStatus ? ` | 上次: ${job._state.lastStatus}` : '')
      + (delivery ? ` 投递: ${delivery}` : '')
      + (job._state?.currentRun ? ` | 运行中，始于 ${job._state.currentRun.startedAt}` : '');
    console.log(`${status} ${job.id} ${owner} — ${job.name}`);
    console.log(`   cron: ${job.schedule.expr} (${job.schedule.tz})${lastStatus}`);
    if (job.agent.inject_file) {
      console.log(`   inject: ${job.agent.inject_file}`);
    }
  }
  return 0;
}

function cmdAdd(opts: Record<string, string>): number {
  if (!opts.cron || !opts.prompt) {
    console.error('错误: --cron 和 --prompt 为必填参数。');
    return 1;
  }

  const owner = opts.owner ?? 'system';
  const id = `${owner}-${Date.now().toString(16)}`;
  const tz = opts.tz ?? 'Asia/Shanghai';

  const newJob = {
    id,
    name: opts.name ?? opts.prompt.slice(0, 20),
    enabled: true,
    owner,
    schedule: { expr: opts.cron, tz },
    agent: {
      workspace: opts.workspace ?? process.cwd(),
      prompt: opts.prompt,
      timeout: parseInt(opts.timeout ?? '3600', 10),
      ...(opts.inject ? { inject_file: opts.inject } : {}),
    },
    delivery: {
      mode: opts.delivery ?? 'none',
      ...(opts.delivery === 'announce' && opts.to ? { channel: 'telegram', to: opts.to } : {}),
    },
  };

  const data = loadJobs();
  if (data.jobs.some((j: any) => j.id === id)) {
    console.error(`错误: id "${id}" 已存在。`);
    return 1;
  }
  data.jobs.push(newJob);
  saveJobs(data);
  console.log(`✅ 任务已创建: ${id}`);
  notifyRelay();
  return 0;
}

function cmdDel(id: string | undefined, opts: Record<string, string>): number {
  if (!id) { console.error('错误: 请指定任务 ID。'); return 1; }
  if (!opts.owner) { console.error('错误: 请指定 --owner。'); return 1; }

  const data = loadJobs();
  const idx = data.jobs.findIndex((j: any) => j.id === id);
  if (idx === -1) { console.error(`错误: 未找到任务 "${id}"。`); return 1; }
  if (data.jobs[idx].owner !== opts.owner) {
    console.error(`错误: 任务 "${id}" 不属于 ${opts.owner}。`);
    return 1;
  }
  data.jobs.splice(idx, 1);
  saveJobs(data);
  console.log(`✅ 任务 "${id}" 已删除。`);
  notifyRelay();
  return 0;
}

const RELAY_REQUEST_TIMEOUT_MS = 10_000;

/** The resident Relay's address. `pulse-relay jobs` runs this CLI with the
 *  resident process environment (RELAY_HTTP_PORT, RELAY_INTERNAL_TOKEN and a
 *  loopback RELAY_STATUS_HOST), so the values are read the way Relay reads them. */
function residentRelayUrl(env: NodeJS.ProcessEnv): string | undefined {
  const port = resolveRelayHttpPort(env);
  if (port === undefined) return undefined;
  let host = (env['RELAY_STATUS_HOST'] ?? '').trim() || resolveRelayHttpHost(env);
  if (host === '0.0.0.0' || host === '::') host = '127.0.0.1';
  if (host.includes(':') && !host.startsWith('[')) host = `[${host}]`;
  return `http://${host}:${port}/internal/jobs/run`;
}

const LOCAL_HINT = '如只需本机试跑、不投递，加 --local。';

async function cmdRunResident(id: string): Promise<number> {
  const url = residentRelayUrl(process.env);
  if (!url) {
    console.error('错误: 未取得运行中 Relay 的 RELAY_HTTP_PORT。请经 `pulse-relay jobs run <id>` 调用'
      + `（它带入常驻 Relay 的端口与令牌）。${LOCAL_HINT}`);
    return 1;
  }
  let response: Response;
  try {
    response = await fetch(url, {
      method: 'POST',
      headers: { 'content-type': 'application/json', 'x-relay-token': process.env['RELAY_INTERNAL_TOKEN'] ?? '' },
      body: JSON.stringify({ jobId: id }),
      signal: AbortSignal.timeout(RELAY_REQUEST_TIMEOUT_MS),
    });
  } catch (err) {
    console.error(`错误: 连不上运行中的 Relay（${url}）：${err instanceof Error ? err.message : String(err)}。${LOCAL_HINT}`);
    return 1;
  }
  const body = await response.json().catch(() => ({})) as { error?: string; message?: string; name?: string; acceptedAt?: string };
  if (response.status === 202) {
    console.log(`已交运行中的 Relay 执行: ${body.name ?? id} (${id})，受理于 ${body.acceptedAt ?? '?'}；按任务 delivery 投递。`);
    console.log('任务在 Relay 后台运行；结果看 `pulse-relay jobs list` 中该任务的“上次/投递”，或 Relay 日志中该任务的 `job finished`。');
    return 0;
  }
  const reasons: Record<string, string> = {
    unauthorized: 'Relay 拒绝了令牌（RELAY_INTERNAL_TOKEN 不符）',
    job_not_found: `运行中的 Relay 没有任务 "${id}"（配置改动未 reload，或不属于本节点）`,
    job_running: `任务 "${id}" 正在运行或排队，未重复启动`,
    maintenance_hold: 'Relay 处于维护屏障中，未启动；屏障解除后重试',
    scheduler_unavailable: '运行中的 Relay 没有启用调度器',
    not_found: '运行中的 Relay 版本没有 /internal/jobs/run 入口',
  };
  const reason = (body.error && reasons[body.error]) ?? `${body.error ?? 'unknown'}${body.message ? `: ${body.message}` : ''}`;
  console.error(`❌ Relay 未执行 (HTTP ${response.status}): ${reason}`);
  return 1;
}

async function cmdRun(args: string[]): Promise<number> {
  const local = args.includes('--local');
  const id = args.find(arg => !arg.startsWith('--'));
  if (!id) { console.error('错误: 请指定任务 ID。'); return 1; }
  if (!local) return cmdRunResident(id);

  // The same owner isolation as the resident Relay; without a readable Relay configuration it is unknown.
  let ownerIsolationFor: (owner: string) => OwnerIsolation = () => 'unknown';
  try { ownerIsolationFor = ownerIsolationFromRouter(createRouter(resolveConfigPath())); } catch { /* stays unknown */ }
  const scheduler = createScheduler(NOOP_BOT, CRON_JOBS_PATH, {
    registerCronTasks: false,
    deliveryEnabled: false,
    ownerIsolationFor,
  });
  scheduler.start();

  const job = scheduler.listJobs().find((j: any) => j.id === id);

  try {
    if (!job) {
      console.error(`错误: 未找到任务 "${id}"。`);
      return 1;
    }

    const preference = job.agent.runtimePreference ?? resolvePulseRuntimePriority();
    console.log(`本地运行，不投递: ${job.name} (${job.id}), runtime 优先: [${preference.join(', ')}]`);

    const result = await scheduler.runJob(id);
    if (result.output.trim()) {
      console.log(result.output.trim());
    }
    if (result.status === 'ok') {
      console.log(`✅ 本地执行完成（未投递）。runtime=${result.runtime ?? 'unknown'} attempted=[${result.attemptedRuntimes?.join(', ') ?? ''}]`);
      return 0;
    }
    console.error(`❌ 执行失败: ${result.errorMessage ?? result.status}`);
    return 1;
  } finally {
    scheduler.stop();
  }
}

export async function main(argv = process.argv.slice(2)): Promise<number> {
  const command = argv[0];
  switch (command) {
    case 'list':
      return cmdList(parseArgs(argv.slice(1)));
    case 'add':
      return cmdAdd(parseArgs(argv.slice(1)));
    case 'del':
      return cmdDel(argv[1], parseArgs(argv.slice(2)));
    case 'run':
      return await cmdRun(argv.slice(1));
    default:
      return usage();
  }
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  try {
    process.exitCode = await main();
  } catch (err) {
    console.error(err);
    process.exitCode = 1;
  }
}
