/**
 * Tell the human that a scheduled job is handling collaboration contracts in
 * the background.
 *
 * A `*-collab-inbox` job runs its LLM turn as a detached batch with
 * `delivery.mode: 'none'`, so nothing reaches the Agent's human conversation.
 * When the job opts in with `delivery.progressNotice: true` and its gate
 * printed a non-empty `pulse.collab_inbox_gate/v1` batch, the scheduler posts
 * one notice before the run (Feishu card with a collapsible panel, Telegram
 * expandable blockquote) naming who offered the contracts and, when the run
 * ends, rewrites that same message with the outcome and duration. No second
 * message is sent: a notice that cannot be rewritten stays as posted, and
 * failures and timeouts reach the human through the scheduler failure alert.
 * A failed send or edit is logged and never changes the job result.
 *
 * The same layout announces a contract delivered into the Agent's human
 * conversation (`collab-delivery.ts`): one headline naming the offering or
 * answering Agent and the WI, with the contract details folded below it.
 */
import type { Bot } from 'grammy';
import type { FeishuMessageClient } from './delivery.js';

export const COLLAB_GATE_SCHEMA = 'pulse.collab_inbox_gate/v1';
/** Title prefix; `progressNoticeTitle` completes it with who offered the contracts. */
export const PROGRESS_NOTICE_TITLE = '📨 后台正在处理';
const SEND_TIMEOUT_MS = 15_000;
/** Keeps a Telegram message under its 4096-character limit. */
const MAX_LISTED = 20;

export interface CollabNoticeContract {
  contractId: string;
  offeredBy: string;
  workItemId: string;
  kind: string;
  status: string;
  rewake: boolean;
}

export type NoticeOutcome = 'ok' | 'error' | 'timeout' | 'paused';

/** A contract delivered into a human conversation, with Agent display names resolved. */
export interface CollabTurnNotice {
  role: 'recipient' | 'requester' | 'submitter' | 'wake';
  /** For `submitter`: the round's candidate ID. */
  contractId: string;
  /** The WI the contract was offered from. */
  workItemId: string;
  kind: string;
  status: string;
  deadline?: string;
  /** The WI the turn handles the contract on. */
  targetWorkItemId?: string;
  mainOnly?: boolean;
  /** Display name of the offering Agent; absent when the gate did not send it. */
  offeredBy?: string;
  /** Display names of the answering Agents (requester deliveries). */
  answeredBy?: string[];
  /** `owner/name#number` of a pull request result (`submitter`). */
  pullRequest?: string;
  /** The scheduled job of a `wake` delivery. */
  jobId?: string;
}

/** Resolves a Harness actor to the Agent's display name. */
export type AgentDisplayName = (actor: string) => string | undefined;

export interface ProgressNoticeTargets {
  feishu?: { client: FeishuMessageClient; openId: string };
  telegram?: { bot: Bot; chatId: string };
}

export interface ProgressNotice {
  /** Rewrite the notice with the run outcome. Never throws. */
  finish(outcome: NoticeOutcome, durationMs: number): Promise<void>;
}

type Log = (event: Record<string, unknown>) => void;

/** Contracts of a collab-inbox gate batch, or undefined for any other output. */
export function parseCollabBatch(gateOutput: string): CollabNoticeContract[] | undefined {
  let batch: unknown;
  try {
    batch = JSON.parse(gateOutput);
  } catch {
    return undefined;
  }
  if (!batch || typeof batch !== 'object' || (batch as Record<string, unknown>)['schema'] !== COLLAB_GATE_SCHEMA) return undefined;
  const contracts = (batch as Record<string, unknown>)['contracts'];
  if (!Array.isArray(contracts)) return undefined;
  const text = (value: unknown): string => (typeof value === 'string' && value ? value : '?');
  const parsed = contracts
    .filter((c): c is Record<string, unknown> => Boolean(c) && typeof c === 'object')
    .map(c => ({
      contractId: text(c['contract_id']),
      offeredBy: text(c['offered_by']),
      workItemId: text(c['work_item_id']),
      kind: text(c['kind']),
      status: text(c['status']),
      rewake: c['rewake'] === true,
    }));
  return parsed.length > 0 ? parsed : undefined;
}

/** `op_` plus the first 9 characters after it. */
export function shortContractId(contractId: string): string {
  return contractId.startsWith('op_') ? contractId.slice(0, 12) : contractId;
}

const STATUS_LABEL: Record<string, string> = { pending: 'pending（待答复）', accepted: 'accepted（已接受，待结清）' };

function listed(contracts: CollabNoticeContract[], render: (c: CollabNoticeContract) => string): string {
  const extra = contracts.length - MAX_LISTED;
  return contracts.slice(0, MAX_LISTED).map(render).join('\n\n') + (extra > 0 ? `\n\n另有 ${extra} 份未列出` : '');
}

/** `… Amanda wi_… 协作合同。` for one contract, `… 3 份协作合同，来自 Amanda、Hestia。` for several. */
export function progressNoticeTitle(contracts: CollabNoticeContract[], displayName?: AgentDisplayName): string {
  const name = (actor: string) => displayName?.(actor) ?? actor;
  const [only] = contracts;
  if (contracts.length === 1 && only) return `${PROGRESS_NOTICE_TITLE} ${name(only.offeredBy)} ${only.workItemId} 协作合同。`;
  const from = [...new Set(contracts.map(c => name(c.offeredBy)))];
  return `${PROGRESS_NOTICE_TITLE} ${contracts.length} 份协作合同，来自 ${from.join('、')}。`;
}

function statusLabel(c: CollabNoticeContract): string {
  return `${STATUS_LABEL[c.status] ?? c.status}${c.rewake ? '，再次唤醒' : ''}`;
}

export function formatDuration(ms: number): string {
  const seconds = Math.max(0, Math.round(ms / 1000));
  const m = Math.floor(seconds / 60);
  const s = seconds % 60;
  return m > 0 ? `${m} 分 ${s} 秒` : `${s} 秒`;
}

const OUTCOME: Record<NoticeOutcome, { title: string; template: string }> = {
  ok: { title: '✅ 协作合同后台处理完成', template: 'green' },
  error: { title: '❌ 协作合同后台处理失败', template: 'red' },
  timeout: { title: '⏱️ 协作合同后台处理超时', template: 'orange' },
  paused: { title: '⏸️ 自动执行已暂停，协作合同未处理', template: 'grey' },
};

/** Free text in Feishu card markdown: drop characters that open markup. */
function plain(value: string): string {
  return value.replace(/[<>*`~[\]\\]/g, ' ');
}

/** Identifiers as inline code, so `_` in `wi_…` never turns into emphasis. */
function code(value: string): string {
  return `\`${value.replace(/[`\r\n]/g, ' ')}\``;
}

export function feishuNoticeCard(
  jobName: string,
  contracts: CollabNoticeContract[],
  finished?: { outcome: NoticeOutcome; durationMs: number },
  displayName?: AgentDisplayName,
): Record<string, unknown> {
  const head = finished ? OUTCOME[finished.outcome] : { title: progressNoticeTitle(contracts, displayName), template: 'blue' };
  const summary = finished
    ? `${plain(jobName)} · ${contracts.length} 份合同 · 耗时 ${formatDuration(finished.durationMs)}`
    : `${plain(jobName)} · ${contracts.length} 份合同`;
  const details = listed(contracts, c => [
    `合同 ${code(shortContractId(c.contractId))}`,
    `发起方：${code(c.offeredBy)}　WI：${code(c.workItemId)}`,
    `类型：${code(c.kind)}　状态：${plain(statusLabel(c))}`,
  ].join('\n'));
  return foldedCard(head.title, head.template, summary, details);
}

/** Feishu 2.0 card: a header, an optional summary line and a collapsed details panel. */
function foldedCard(title: string, template: string, summary: string | undefined, details: string): Record<string, unknown> {
  return {
    schema: '2.0',
    config: { update_multi: true, width_mode: 'fill' },
    header: { title: { tag: 'plain_text', content: title }, template },
    body: {
      elements: [
        ...(summary ? [{ tag: 'markdown', content: summary }] : []),
        {
          tag: 'collapsible_panel',
          expanded: false,
          header: {
            title: { tag: 'markdown', content: '展开查看合同详情' },
            vertical_align: 'center',
            icon: { tag: 'standard_icon', token: 'down-small-ccm_outlined', size: '16px 16px' },
            icon_position: 'right',
            icon_expanded_angle: -180,
          },
          border: { color: 'grey', corner_radius: '5px' },
          vertical_spacing: '8px',
          padding: '8px 8px 8px 8px',
          elements: [{ tag: 'markdown', content: details }],
        },
      ],
    },
  };
}

function escapeHtml(value: string): string {
  return value.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
}

export function telegramNoticeHtml(
  jobName: string,
  contracts: CollabNoticeContract[],
  finished?: { outcome: NoticeOutcome; durationMs: number },
  displayName?: AgentDisplayName,
): string {
  const title = finished
    ? `${OUTCOME[finished.outcome].title}（耗时 ${formatDuration(finished.durationMs)}）`
    : progressNoticeTitle(contracts, displayName);
  const details = listed(contracts, c =>
    `${shortContractId(c.contractId)} · 发起方 ${c.offeredBy}\nWI ${c.workItemId} · ${c.kind} · ${statusLabel(c)}`);
  return `${escapeHtml(title)}\n${escapeHtml(jobName)} · ${contracts.length} 份合同\n<blockquote expandable>${escapeHtml(details)}</blockquote>`;
}

const ANSWER_LABEL: Record<string, string> = { completed: '已完成', returned: '已退回', rejected: '已拒绝' };

/** One line naming who sent the contract, from which WI, and what happens next. */
const PR_LABEL: Record<string, string> = {
  merged: '已由内核合并，继续后续步骤', changes_requested: '审核未通过，开始修改',
  checks_failed: '检查失败，开始处理', merge_failed: '合并失败，开始处理',
  merge_blocked: '合并受阻，开始处理', human_confirmation_required: '需要你确认合并',
};

export function collabTurnHeadline(n: CollabTurnNotice): string {
  if (n.role === 'wake') return `⏰ 定时唤醒 ${n.jobId ?? ''}（${n.targetWorkItemId ?? n.workItemId}），开始处理。`;
  if (n.role === 'submitter') return `🔀 ${n.pullRequest ?? 'PR'} ${PR_LABEL[n.status] ?? n.status}（${n.workItemId}）。`;
  if (n.role === 'requester') {
    const by = n.answeredBy?.length ? n.answeredBy.join('、') : '对方';
    return `📨 ${by} 已答复你在 ${n.workItemId} 的协作合同（${ANSWER_LABEL[n.status] ?? n.status}），开始验收。`;
  }
  return `📨 收到 ${n.offeredBy ? `${n.offeredBy} ` : ''}${n.workItemId} 协作合同，开始处理。`;
}

function turnDetails(n: CollabTurnNotice, value: (v: string) => string): string {
  if (n.role === 'wake') return `任务：${value(n.jobId ?? '?')}　WI：${value(n.targetWorkItemId ?? n.workItemId)}`;
  if (n.role === 'submitter') {
    return [`PR：${value(n.pullRequest ?? '?')}　状态：${value(n.status)}`,
      `轮次：${value(shortContractId(n.contractId))}　WI：${value(n.workItemId)}`].join('\n');
  }
  return [
    `合同：${value(shortContractId(n.contractId))}`,
    `类型：${value(n.kind)}　状态：${value(n.status)}`,
    ...(n.deadline ? [`截止：${value(n.deadline)}`] : []),
    ...(n.targetWorkItemId ? [`处理 WI：${value(n.targetWorkItemId)}`] : []),
    ...(n.mainOnly !== undefined ? [`只限主身：${n.mainOnly ? '是' : '否'}`] : []),
  ].join('\n');
}

export function telegramTurnNoticeHtml(n: CollabTurnNotice): string {
  return `${escapeHtml(collabTurnHeadline(n))}\n<blockquote expandable>${escapeHtml(turnDetails(n, v => v))}</blockquote>`;
}

export function feishuTurnNoticeCard(n: CollabTurnNotice): Record<string, unknown> {
  return foldedCard(collabTurnHeadline(n), 'blue', undefined, turnDetails(n, code));
}

function bounded<T>(work: Promise<T>): Promise<T> {
  let timer: NodeJS.Timeout | undefined;
  const timeout = new Promise<never>((_, reject) => {
    timer = setTimeout(() => reject(new Error(`notice send timed out after ${SEND_TIMEOUT_MS}ms`)), SEND_TIMEOUT_MS);
    timer.unref?.();
  });
  return Promise.race([work, timeout]).finally(() => clearTimeout(timer));
}

function feishuMessageId(response: unknown): string | undefined {
  const r = response as { code?: number; data?: { message_id?: unknown } } | null;
  if (r && typeof r.code === 'number' && r.code !== 0) throw new Error(`Feishu rejected notice: ${r.code}`);
  const id = r?.data?.message_id;
  return typeof id === 'string' ? id : undefined;
}

/**
 * Post the start notice without blocking the caller. Returns undefined when the
 * job has no human target, so nothing is sent and nothing is awaited.
 */
export function startProgressNotice(
  job: { id: string; name: string },
  contracts: CollabNoticeContract[],
  targets: ProgressNoticeTargets,
  log: Log = event => console.log({ ts: new Date().toISOString(), module: 'collab-progress-notice', ...event }),
  displayName?: AgentDisplayName,
): ProgressNotice | undefined {
  const { feishu, telegram } = targets;
  if (!feishu && !telegram) return undefined;
  const fail = (channel: string, step: string) => (error: unknown) => {
    log({ level: 'warn', msg: 'collab progress notice failed', job: job.id, channel, step, error: String(error) });
    return undefined;
  };

  const feishuSent = feishu
    ? bounded(feishu.client.im.message.create({
      params: { receive_id_type: 'open_id' },
      data: { receive_id: feishu.openId, msg_type: 'interactive', content: JSON.stringify(feishuNoticeCard(job.name, contracts, undefined, displayName)) },
    })).then(feishuMessageId).catch(fail('feishu', 'send'))
    : Promise.resolve(undefined);
  const telegramSent = telegram
    ? bounded(telegram.bot.api.sendMessage(Number(telegram.chatId), telegramNoticeHtml(job.name, contracts, undefined, displayName), { parse_mode: 'HTML' }))
      .then(message => (typeof message?.message_id === 'number' ? message.message_id : undefined))
      .catch(fail('telegram', 'send'))
    : Promise.resolve(undefined);

  async function finishFeishu(outcome: NoticeOutcome, durationMs: number): Promise<void> {
    if (!feishu) return;
    const messageId = await feishuSent;
    const patch = feishu.client.im.message.patch;
    if (messageId && patch) {
      const content = JSON.stringify(feishuNoticeCard(job.name, contracts, { outcome, durationMs }, displayName));
      await bounded(patch.call(feishu.client.im.message, { path: { message_id: messageId }, data: { content } }))
        .then(feishuMessageId).catch(fail('feishu', 'patch'));
    }
  }

  async function finishTelegram(outcome: NoticeOutcome, durationMs: number): Promise<void> {
    if (!telegram) return;
    const chatId = Number(telegram.chatId);
    const messageId = await telegramSent;
    if (messageId === undefined) return;
    await bounded(telegram.bot.api.editMessageText(chatId, messageId,
      telegramNoticeHtml(job.name, contracts, { outcome, durationMs }, displayName), { parse_mode: 'HTML' }))
      .catch(fail('telegram', 'edit'));
  }

  let finished = false;
  return {
    async finish(outcome, durationMs) {
      if (finished) return;
      finished = true;
      await Promise.all([finishFeishu(outcome, durationMs), finishTelegram(outcome, durationMs)])
        .catch(fail('any', 'finish'));
    },
  };
}
