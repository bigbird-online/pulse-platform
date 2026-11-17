import { closeSync, constants, fsyncSync, lstatSync, mkdirSync, openSync, renameSync, unlinkSync, writeSync } from 'node:fs';
import { randomUUID } from 'node:crypto';
import { dirname, join } from 'node:path';

/**
 * Claude Code 订阅额度观测：把 stream-json `rate_limit_event` 归约为
 * Stats 共享契约 `pulse.stats.cc_quota/v1`。文档只含额度事实，
 * 不含 token、session id 或任何凭据。
 */
export const CC_QUOTA_SCHEMA = 'pulse.stats.cc_quota/v1';
export const CC_QUOTA_PATH_ENV = 'PULSE_CC_QUOTA_PATH';

export type CcQuotaWindow = { used_percent: number; window_minutes: number; resets_at: number };
export type CcQuotaDocument = {
  schema: typeof CC_QUOTA_SCHEMA;
  status: 'available';
  source: 'relay_cc_rate_limit_event';
  observed_at: string;
  weekly: CcQuotaWindow;
  five_hour: CcQuotaWindow | null;
  rate_limit_status: string;
};

type RateLimitMessage = { type?: unknown; subtype?: unknown; error_status?: unknown; rate_limit_info?: unknown };

const object = (value: unknown): Record<string, unknown> =>
  value !== null && typeof value === 'object' && !Array.isArray(value) ? value as Record<string, unknown> : {};

/**
 * 判定 CC 流事件是否为真实限流失败。`rate_limit_event.status`：
 *   - "allowed"         → 请求通过；
 *   - "allowed_warning" → 请求通过，仅是用量提醒（如 7 天窗口利用率越过 0.75）；
 *   - "rejected" 等     → 真被限流。
 * 任何以 "allowed" 开头的状态都不是失败；旧的 `!== 'allowed'` 判法曾把
 * allowed_warning 当成 429，令越过阈值的 agent 陷入 SIGKILL→respawn 死循环。
 */
export function isRateLimitFailureMessage(msg: RateLimitMessage): boolean {
  if (msg.type === 'system' && msg.subtype === 'api_retry' && Number(msg.error_status) === 429) return true;
  if (msg.type !== 'rate_limit_event') return false;
  const info = msg.rate_limit_info;
  if (!info) return true;
  return !String(object(info).status ?? '').toLowerCase().startsWith('allowed');
}

/** utilization 是 0..1 小数，resetsAt 是 unix 秒；越界即视为不可用。 */
function quotaWindow(window: unknown, minutes: number): CcQuotaWindow | null {
  const { utilization, resetsAt } = object(window);
  if (typeof utilization !== 'number' || !Number.isFinite(utilization) || utilization < 0 || utilization > 1) return null;
  // 上界排除毫秒时间戳（≈1e12）被误当成秒。
  if (typeof resetsAt !== 'number' || !Number.isFinite(resetsAt) || resetsAt <= 0 || resetsAt >= 1e11) return null;
  return { used_percent: Math.round(utilization * 1000) / 10, window_minutes: minutes, resets_at: Math.floor(resetsAt) };
}

/** 新形态读 unifiedWindows；旧形态只有 rateLimitType + utilization 单窗口。 */
function namedWindow(info: Record<string, unknown>, name: 'seven_day' | 'five_hour', minutes: number): CcQuotaWindow | null {
  const unified = object(info.unifiedWindows);
  if (unified[name] !== undefined) return quotaWindow(unified[name], minutes);
  return info.rateLimitType === name ? quotaWindow(info, minutes) : null;
}

/** 无可用 seven_day 窗口时返回 undefined：宁缺不写半份文档。 */
export function parseCcQuotaObservation(msg: unknown, observedAt = new Date()): CcQuotaDocument | undefined {
  const record = object(msg);
  if (record.type !== 'rate_limit_event') return undefined;
  const info = object(record.rate_limit_info);
  if (typeof info.status !== 'string' || !info.status.trim()) return undefined;
  const weekly = namedWindow(info, 'seven_day', 10080);
  if (!weekly) return undefined;
  return {
    schema: CC_QUOTA_SCHEMA,
    status: 'available',
    source: 'relay_cc_rate_limit_event',
    observed_at: observedAt.toISOString().replace(/\.\d{3}Z$/, 'Z'),
    weekly,
    five_hour: namedWindow(info, 'five_hour', 300),
    rate_limit_status: info.status.trim().slice(0, 64),
  };
}

/** 显式 PULSE_CC_QUOTA_PATH 优先；否则仅在 RELAY_DATA_DIR 存在时启用默认位置。 */
export function resolveCcQuotaPath(env: NodeJS.ProcessEnv = process.env): string | undefined {
  const explicit = env[CC_QUOTA_PATH_ENV]?.trim();
  if (explicit) return explicit;
  const dataDir = env['RELAY_DATA_DIR']?.trim();
  return dataDir ? join(dataDir, 'quota', 'cc-quota-v1.json') : undefined;
}

/** tmp + rename 原子替换，权限 0600；不跟随目标或临时文件上的符号链接。 */
export function writeCcQuotaDocument(path: string, document: CcQuotaDocument): void {
  mkdirSync(dirname(path), { recursive: true, mode: 0o700 });
  try {
    if (lstatSync(path).isSymbolicLink()) throw new Error('refusing to replace symlinked quota document');
  } catch (error) {
    if ((error as NodeJS.ErrnoException).code !== 'ENOENT') throw error;
  }
  const tmp = `${path}.${process.pid}.${randomUUID()}.tmp`;
  const fd = openSync(tmp, constants.O_WRONLY | constants.O_CREAT | constants.O_EXCL | constants.O_NOFOLLOW, 0o600);
  try {
    writeSync(fd, `${JSON.stringify(document, null, 2)}\n`);
    fsyncSync(fd);
  } catch (error) {
    closeSync(fd);
    try { unlinkSync(tmp); } catch { /* already absent */ }
    throw error;
  }
  closeSync(fd);
  try {
    renameSync(tmp, path);
  } catch (error) {
    try { unlinkSync(tmp); } catch { /* already absent */ }
    throw error;
  }
}

/** 流事件入口：解析并落盘最新观测。失败只记 warn，绝不打断回合。 */
export function observeCcRateLimitEvent(msg: unknown, env: NodeJS.ProcessEnv = process.env, observedAt = new Date()): boolean {
  if (object(msg).type !== 'rate_limit_event') return false;
  const path = resolveCcQuotaPath(env);
  if (!path) return false;
  const document = parseCcQuotaObservation(msg, observedAt);
  if (!document) return false;
  try {
    writeCcQuotaDocument(path, document);
    return true;
  } catch (error) {
    console.warn({
      ts: new Date().toISOString(), level: 'warn', msg: 'CC quota observation write failed',
      error: error instanceof Error ? error.message : String(error),
    });
    return false;
  }
}
