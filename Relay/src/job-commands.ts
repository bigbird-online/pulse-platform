import type { Scheduler, ScheduledJob } from './types.js';

/**
 * /jobs -- list scheduled jobs for the current agent
 */
export function handleListJobs(agentId: string, scheduler: Scheduler): string {
  const jobs = scheduler.listJobs(agentId);
  if (jobs.length === 0) {
    return '\u6682\u65E0\u8BA1\u5212\u4EFB\u52A1\u3002';
  }
  const lines = ['\uD83D\uDCCB \u8BA1\u5212\u4EFB\u52A1\u5217\u8868\uFF1A\n'];
  for (const job of jobs) {
    const status = job.enabled ? '\u2705' : '\u23F8';
    const lastRun = job._state?.lastRunAt
      ? ` | \u4E0A\u6B21: ${job._state.lastStatus ?? '?'}`
      : '';
    const ownerTag = job.owner === 'system' ? ' [system]' : '';
    lines.push(`${status} \`${job.id}\`${ownerTag}`);
    lines.push(`   ${job.name} | ${job.schedule.expr} (${job.schedule.tz})${lastRun}`);
  }
  return lines.join('\n');
}

/**
 * /job_add <cron_expr> | <prompt> [| name=xxx] [| timeout=xxx] [| delivery=announce]
 */
export function handleAddJob(
  agentId: string,
  workspace: string,
  chatId: string,
  args: string,
  scheduler: Scheduler,
): string {
  if (!args || !args.trim()) {
    return '\u7528\u6CD5: /job_add <cron\u8868\u8FBE\u5F0F> | <prompt> [| name=\u540D\u79F0] [| timeout=\u79D2] [| delivery=announce]\n\n\u793A\u4F8B:\n/job_add 0 6 * * * | \u6267\u884C\u6BCF\u65E5\u6570\u636E\u91C7\u96C6';
  }

  const segments = args.split('|').map(s => s.trim());

  // First segment: first 5 tokens are cron expression
  const firstSegment = segments[0];
  const tokens = firstSegment.split(/\s+/);
  if (tokens.length < 5) {
    return '\u274C cron \u8868\u8FBE\u5F0F\u9700\u8981 5 \u4E2A\u5B57\u6BB5\uFF08\u5206 \u65F6 \u65E5 \u6708 \u5468\uFF09\u3002';
  }
  const cronExpr = tokens.slice(0, 5).join(' ');
  const cronTail = tokens.slice(5).join(' ');

  // Second segment: prompt (required)
  let prompt = '';
  if (segments.length >= 2) {
    prompt = segments[1];
    if (cronTail) prompt = cronTail + ' ' + prompt;
  } else if (cronTail) {
    prompt = cronTail;
  }

  if (!prompt) {
    return '\u274C \u7F3A\u5C11 prompt\u3002\u683C\u5F0F: /job_add <cron> | <prompt>';
  }

  // Parse optional parameters
  let name = prompt.slice(0, 20).replace(/\n/g, ' ');
  let timeout = 3600;
  let deliveryMode: 'announce' | 'none' = 'none';

  for (let i = 2; i < segments.length; i++) {
    const seg = segments[i];
    const eqIdx = seg.indexOf('=');
    if (eqIdx === -1) continue;
    const key = seg.slice(0, eqIdx).trim().toLowerCase();
    const val = seg.slice(eqIdx + 1).trim();
    if (key === 'name') name = val;
    else if (key === 'timeout') {
      const n = parseInt(val, 10);
      if (!isNaN(n) && n > 0) timeout = n;
    }
    else if (key === 'delivery' && val === 'announce') deliveryMode = 'announce';
  }

  const id = `${agentId}-${Date.now().toString(16)}`;

  const newJob: Omit<ScheduledJob, '_state'> = {
    id,
    name,
    enabled: true,
    owner: agentId,
    schedule: { expr: cronExpr, tz: 'Asia/Shanghai' },
    agent: { workspace, prompt, timeout },
    delivery: {
      mode: deliveryMode,
      ...(deliveryMode === 'announce' ? { channel: 'telegram' as const, to: chatId } : {}),
    },
  };

  try {
    const created = scheduler.addJob(newJob);
    return `\u2705 \u4EFB\u52A1\u5DF2\u521B\u5EFA\nID: \`${created.id}\`\n\u540D\u79F0: ${created.name}\nCron: ${created.schedule.expr}\n\u6267\u884C\u65F6\u533A: ${created.schedule.tz}`;
  } catch (err) {
    return `\u274C \u521B\u5EFA\u5931\u8D25: ${err instanceof Error ? err.message : String(err)}`;
  }
}

/**
 * /job_del <id>
 */
export function handleDelJob(agentId: string, jobId: string, scheduler: Scheduler): string {
  if (!jobId || !jobId.trim()) {
    return '\u7528\u6CD5: /job_del <\u4EFB\u52A1ID>';
  }
  const trimmed = jobId.trim();
  const success = scheduler.removeJob(trimmed, agentId);
  if (success) {
    return `\u2705 \u4EFB\u52A1 \`${trimmed}\` \u5DF2\u5220\u9664\u3002`;
  }
  // Check whether it exists but belongs to someone else
  const jobs = scheduler.listJobs();
  const exists = jobs.some(j => j.id === trimmed);
  if (exists) {
    return `\u274C \u4EFB\u52A1 \`${trimmed}\` \u4E0D\u5C5E\u4E8E\u4F60\uFF0C\u65E0\u6CD5\u5220\u9664\u3002`;
  }
  return `\u274C \u672A\u627E\u5230\u4EFB\u52A1 \`${trimmed}\`\u3002`;
}

/**
 * /job_run <id>
 */
export async function handleRunJob(agentId: string, jobId: string, scheduler: Scheduler): Promise<string> {
  if (!jobId || !jobId.trim()) {
    return '\u7528\u6CD5: /job_run <\u4EFB\u52A1ID>';
  }
  const trimmed = jobId.trim();

  // Permission check: allow own jobs or system jobs
  const jobs = scheduler.listJobs(agentId);
  const job = jobs.find(j => j.id === trimmed);
  if (!job) {
    return `\u274C \u672A\u627E\u5230\u4EFB\u52A1 \`${trimmed}\`\uFF0C\u6216\u8BE5\u4EFB\u52A1\u4E0D\u5C5E\u4E8E\u4F60\u3002`;
  }

  try {
    const result = await scheduler.runJob(trimmed);
    const statusEmoji = result.status === 'ok' ? '\u2705' : result.status === 'timeout' ? '\u23F0' : '\u274C';
    const duration = (result.durationMs / 1000).toFixed(1);
    return `${statusEmoji} \u4EFB\u52A1 \`${trimmed}\` \u6267\u884C\u5B8C\u6210\n\u72B6\u6001: ${result.status}\n\u8017\u65F6: ${duration}s`;
  } catch (err) {
    return `\u274C \u6267\u884C\u5931\u8D25: ${err instanceof Error ? err.message : String(err)}`;
  }
}
