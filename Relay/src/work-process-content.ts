import { createHash } from 'node:crypto';
import type { WorkProcessRecord } from './work-process-archive.js';
import { TURN_GENERATED_STATUS_TEXT } from './channel-rendering.js';
import { compileRichDoc } from './rich-doc.js';
import { renderTelegramRichMessageHtml } from './channel-rich-rendering.js';

export const escapeProcessHtml = (text: string): string => text.replace(/&/g, '&amp;')
  .replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
/** Short callback identity derived only from the opaque event id, never its body. */
export const toolDisclosureKey = (id: string): string => createHash('sha256').update(id).digest('hex').slice(0, 16);
export type ProcessDetailItem = { id: string; kind: 'commentary' | 'operation'; title: string; detail: string; expanded: boolean; source?: 'reasoning_summary' };
export type ProcessDetails = { items: ProcessDetailItem[]; notice?: string; emptyText: string };

function toolLabel(name = ''): string {
  if (/^(bash|shell|exec_command|command_execution|执行命令)$/i.test(name)) return '运行了命令';
  if (/^(read|read_file|读取文件)$/i.test(name)) return '读取了文件';
  if (/^(edit|write|apply_patch|file_change|修改文件)$/i.test(name)) return '修改了文件';
  if (/search|grep|glob|搜索/i.test(name)) return '执行了搜索';
  if (/todo|计划/i.test(name)) return '更新了计划';
  return name ? `调用了工具 · ${name}` : '调用了工具';
}
export function workProcessSummary(record: WorkProcessRecord): string {
  if (!record.finishedAt) return record.summary;
  const seconds = Math.max(0, Math.floor((record.finishedAt - record.createdAt) / 1000));
  const duration = seconds < 60 ? `${seconds} 秒` : `${Math.floor(seconds / 60)} 分 ${seconds % 60} 秒`;
  return `用时 ${duration} · ${record.summary}`;
}

/** Complete retained sequence. The transport budgets presentation, not storage. */
export function workProcessDetails(record: WorkProcessRecord): ProcessDetails {
  const items = record.events.map((event): ProcessDetailItem => {
    const status = event.state === 'running' ? record.terminal ? '未确认完成' : '执行中'
      : event.state === 'failed' ? '失败' : '';
    // Historic releases used these exact generated IDs before source was stored.
    const summary = event.kind === 'commentary' && (event.source === 'reasoning_summary'
      || (event.source === undefined && /^(?:pi|codex):summary:/.test(event.id)));
    return { id: event.id, kind: event.kind, ...(summary ? { source: 'reasoning_summary' as const } : {}),
      title: event.kind === 'commentary' ? '' : [toolLabel(event.title), status].filter(Boolean).join(' · '),
      detail: event.kind === 'commentary' ? event.output ?? '' : [
        event.input && `操作\n${event.input}`, event.output && `结果\n${event.output}`,
      ].filter(Boolean).join('\n\n') || (event.state === 'running' && !record.terminal
        ? '等待工具返回结果。' : '工具未返回可展示的文本。'),
      expanded: event.kind === 'commentary' || (record.openTools ?? []).includes(event.id) };
  });
  const emptyText = !record.terminal ? '等待过程说明或工具活动…'
    : record.activityObserved === false ? (record.summary === TURN_GENERATED_STATUS_TEXT
      ? '本轮直接生成了答复，没有工具调用。' : '本轮没有留下过程记录，回复未完成。')
    : record.activityObserved ? '本轮有工具活动，但这条消息没有保留可展开的记录。'
    : '这条历史消息没有保存过程详情，无法补回。';
  return { items, emptyText, ...(record.omitted ? { notice: `${record.omitted} 项超出安全保留容量。` } : {}) };
}

/** A recipient-scoped, script-free overflow document; no public hosting service. */
export function workProcessDocument(record: WorkProcessRecord): Buffer {
  const details = workProcessDetails(record);
  const body = details.items.map(item => item.kind === 'commentary'
    ? `<section>${item.source === 'reasoning_summary' ? '<p class=origin>原生推理摘要</p>' : ''}${renderTelegramRichMessageHtml(compileRichDoc(item.detail))}</section>`
    : `<details><summary>${escapeProcessHtml(item.title)}</summary><pre>${escapeProcessHtml(item.detail)}</pre></details>`).join('\n');
  return Buffer.from(`<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'; base-uri 'none'; form-action 'none'">
<title>工作过程</title><style>
:root{color-scheme:light dark}body{font:17px/1.7 system-ui,sans-serif;max-width:900px;margin:32px auto;padding:0 20px;overflow-wrap:anywhere}
.origin{opacity:.65;font-size:14px}header{opacity:.7;border-bottom:1px solid #8886;padding-bottom:12px}section{margin:26px 0}summary{cursor:pointer;opacity:.75}details{margin:14px 0}
pre{font:13px/1.5 ui-monospace,monospace;white-space:pre-wrap;overflow-wrap:anywhere;padding:16px;background:#8881;border-radius:8px}
code{font-family:ui-monospace,monospace}table{border-collapse:collapse}td,th{padding:5px;border:1px solid #8886}
</style></head><body><header>${escapeProcessHtml(workProcessSummary(record))}</header>${body || `<p>${escapeProcessHtml(details.emptyText)}</p>`}
${details.notice ? `<p>${escapeProcessHtml(details.notice)}</p>` : ''}</body></html>`, 'utf8');
}
