import type { WorkProcessRecord } from './work-process-archive.js';
import { TURN_GENERATED_STATUS_TEXT } from './channel-rendering.js';

/** Scheme 1 shares content/interaction, not Telegram HTML with Feishu JSON.
 * Time is sampled on actual rendering; no timer or synthetic activity counter. */
export function workProcessHeader(record: WorkProcessRecord, observedAt = Date.now()): {
  control: string; marker: string; status: string; meta: string;
} {
  const control = record.expanded ? '⌃ 收起过程' : '⌄ 展开过程';
  const activity = /^⏳ 仍在工作，已推进 (\d+) 个执行步骤…$/.exec(record.summary);
  const status = activity ? '仍在工作…'
    : record.summary.replace(/^(?:▶[\uFE0E\uFE0F]?|✅|⚠[\uFE0E\uFE0F]?)\s*/, '');
  const marker = record.terminal ? record.summary === TURN_GENERATED_STATUS_TEXT ? '✓' : '!' : '◌';
  // Old terminal records have no known duration; never invent one on reopening.
  const end = record.finishedAt ?? (record.terminal ? undefined : observedAt);
  const seconds = end === undefined ? undefined : Math.max(0, Math.floor((end - record.createdAt) / 1000));
  const duration = seconds === undefined || (seconds === 0 && !record.terminal) ? ''
    : seconds < 60 ? `${seconds} 秒` : `${Math.floor(seconds / 60)} 分 ${seconds % 60} 秒`;
  const meta = [activity ? `已推进 ${activity[1]} 个执行步骤` : '',
    duration && `${record.terminal ? '用时' : '已用'} ${duration}`].filter(Boolean).join(' · ');
  return { control, marker, status, meta };
}
