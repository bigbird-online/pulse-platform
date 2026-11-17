import { managedRelayPath } from './managed-paths.js';
// relay-observability.ts — 退化/中毒可观测性事件埋点（进程内 emit，单写者 append-only jsonl）
//
// 设计定案：memo/2026-07-06-REQ-pulse-platform-monitoring-system.md「2026-07-07 定案」段
// （大鸟拍口径 A + Duke 裁 poison-monitor-design-20260707）。
//
// 职责边界（关键，别越界）：
//   本模块只负责「**打**结构化事件」——进程内，因为只有 Relay 进程拿得到每条 record 的字段。
//   「**读**事件流 + 判阈值 + 告警」是**独立外部探针**的活（cron，见切片3），不在本模块，
//   也不在 Relay 进程内——铁律「告警链必须比被监控者短、比它独立」（Duke Q2 裁决）：
//   进程死了，进程内的自指告警跟着死，那正是当前失效模式。故本模块**只写不判不报警**。
//
// 可靠性铁律：埋点绝不能反过来拖垮 Relay。所有写盘 fire-and-forget、包 try/catch、永不抛；
//   落盘失败最多 console.warn 一次（限频），绝不影响主流程。

import { appendFileSync, mkdirSync } from 'node:fs';
import { homedir } from 'node:os';
import { join } from 'node:path';

/** 事件严重度分级。探针据此分流 warn / critical 告警。 */
export type ObsSeverity = 'info' | 'warn' | 'critical';

/**
 * 退化可观测事件。字段刻意扁平 + 单行 JSON，便于外部探针 grep/jq/逐行解析
 * （对照现状：console.warn 打的是多行 pretty-print 对象，解析脆）。
 */
export interface ObsEvent {
  /** ISO8601 UTC。落盘时若未提供则不自动补（调用方须传，保持可测）。 */
  ts: string;
  /** 事件类型。degen.* = 退化家族；probe.* = 探针自身留痕（预留）。 */
  evt: string;
  /** agent 归属键（= tenantId / poolKey），退化按 agent 归因的定位键。 */
  agent?: string;
  /** 会话标识（sessionId 或 jsonl 路径），追因用——探针据此直接定位现场。 */
  session?: string;
  /** 具体退化种类（如 toolcall-leak-bare-fence / decode-degen-token-flood）。 */
  kind?: string;
  /** 严重度，探针据此分级告警。 */
  severity?: ObsSeverity;
  /** 其余上下文（stopReason / model / leakTrimmed / retryCount 等），扁平合并进事件。 */
  [k: string]: unknown;
}

/**
 * 从 CC 会话 jsonl 路径推断 agent 归属（一个稳定、唯一、可定位的分组键）。
 * 路径形如 `.../projects/-Users-owner-Coevolution-Bigbird-PULSE-vultr-pulse-space/UUID.jsonl`。
 * CC 把 workspace 绝对路径的 `/` 编码成 `-` 塞进目录名，边界信息已丢失，无法可靠还原
 * "vultr-pulse-space" 到底该取几段（`vultr-pulse-space` vs `pulse-space` 无法从字面区分）。
 *
 * 因此取 **`-Coevolution-` 之后的整段**作 agent 键——对所有 agent 稳定唯一、可读、可定位
 * （如 `Bigbird-PULSE-vultr-pulse-space` / `Bigbird-LifeLab-emi-space` / `duke-space`）。
 * 探针只需"同一 agent 每次结果一致"即可正确分组，不追求还原成最短 workspace 名。
 * 取不到 `-Coevolution-` 锚点时兜底返回整段目录名（去掉起头 `-`）。纯字符串操作，无 IO。
 */
export function agentFromSessionPath(sessionPath: string | undefined): string | undefined {
  if (!sessionPath) return undefined;
  const parts = sessionPath.split('/').filter(Boolean);
  // 倒数第二段是 project 目录名（末段是 UUID.jsonl）。
  const dir = parts.length >= 2 ? parts[parts.length - 2] : undefined;
  if (!dir) return undefined;
  const anchor = '-Coevolution-';
  const idx = dir.indexOf(anchor);
  if (idx >= 0) {
    const tail = dir.slice(idx + anchor.length);
    if (tail) return tail;
  }
  // 兜底：整段目录名去掉起头的 `-`，至少留个可定位串。
  return dir.replace(/^-+/, '') || undefined;
}

/** 从 jsonl 路径取会话短标识（文件名去扩展名，通常是 UUID）。 */
export function sessionFromPath(sessionPath: string | undefined): string | undefined {
  if (!sessionPath) return undefined;
  const base = sessionPath.split('/').pop();
  if (!base) return undefined;
  return base.replace(/\.jsonl$/, '');
}

/** 事件流落盘根目录。默认 `~/.relay/obs/`——贴着 restart-audit.log，外部探针一处读全。 */
export function resolveObsDir(env: NodeJS.ProcessEnv = process.env): string {
  const raw = (env['RELAY_OBS_DIR'] ?? '').trim();
  return managedRelayPath('observabilityDir', raw || undefined) ?? join(homedir(), '.relay', 'obs');
}

/** 埋点总开关。默认开；设 `RELAY_OBS_ENABLED=false` 可关（回退到纯 console.warn 旧行为）。 */
export function isObsEnabled(env: NodeJS.ProcessEnv = process.env): boolean {
  return (env['RELAY_OBS_ENABLED'] ?? '').trim().toLowerCase() !== 'false';
}

let dirEnsured = false;
let lastWriteErrorLoggedAt = 0;

/**
 * 打一条退化可观测事件到当天的 jsonl（`{obsDir}/degen-{YYYY-MM-DD}.jsonl`，单行 append）。
 *
 * fire-and-forget：同步 append 但全程 try/catch，**永不抛**。落盘失败最多每 5 分钟
 * console.warn 一次（防刷屏），绝不影响调用方主流程——监控埋点不得拖垮被监控者。
 *
 * @param event  事件对象。ts 必填（调用方传，便于测试与时序一致）。
 */
export function emitObsEvent(event: ObsEvent, env: NodeJS.ProcessEnv = process.env): void {
  if (!isObsEnabled(env)) return;
  try {
    const obsDir = resolveObsDir(env);
    if (!dirEnsured) {
      mkdirSync(obsDir, { recursive: true });
      dirEnsured = true;
    }
    // 按 ts 的日期分桶（UTC）。ts 形如 2026-07-07T05:58:54.000Z → 取前 10 位。
    const day = (typeof event.ts === 'string' && event.ts.length >= 10)
      ? event.ts.slice(0, 10)
      : new Date().toISOString().slice(0, 10);
    const filePath = join(obsDir, `degen-${day}.jsonl`);
    appendFileSync(filePath, JSON.stringify(event) + '\n');
  } catch (err) {
    // 埋点失败绝不上抛。限频告警，避免磁盘满/权限错时刷屏。
    const now = Date.now();
    if (now - lastWriteErrorLoggedAt > 5 * 60 * 1000) {
      lastWriteErrorLoggedAt = now;
      // eslint-disable-next-line no-console
      console.warn({
        ts: new Date().toISOString(),
        level: 'warn',
        msg: 'emitObsEvent write failed (non-fatal, observability only)',
        error: err instanceof Error ? err.message : String(err),
      });
    }
  }
}

/**
 * L1 便捷入口：一条已知退化被防线**观察到**（不管是清除/截断/nudge/重启哪种处置）。
 * 打 `degen.observed` 事件。探针 L1 命中计数即数这类事件。
 *
 * @param opts.disposition  处置方式（cleansed / trimmed / nudged / restarted / stripped-outbound / blocked-inbound）。
 * @param opts.severity     默认按 disposition 推断（restarted/AUP 级 = critical，其余 warn）。
 */
export function emitDegenObserved(opts: {
  ts: string;
  agent?: string;
  session?: string;
  kind?: string;
  disposition: string;
  severity?: ObsSeverity;
  detail?: Record<string, unknown>;
}, env: NodeJS.ProcessEnv = process.env): void {
  const severity: ObsSeverity = opts.severity
    ?? (opts.disposition === 'restarted' ? 'critical' : 'warn');
  emitObsEvent({
    ts: opts.ts,
    evt: 'degen.observed',
    agent: opts.agent,
    session: opts.session,
    kind: opts.kind,
    severity,
    disposition: opts.disposition,
    ...(opts.detail ?? {}),
  }, env);
}
