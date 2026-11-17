// relay-request-trace.ts — 交互算力供给的结构化埋点（request / turn / delivery）
//
// 监控系统切片 1（交互算力供给为主轴）。规划过 Duke R4:
//   memo/2026-07/2026-07-14-SPEC-monitoring-system-mvs-and-slices.md
//
// 职责边界（与 relay-observability.ts 同源铁律,别越界）：
//   本模块只**打**三类结构化事件到 obs 同款 append-only jsonl（复用 emitObsEvent 的单
//   写者机制）。「**读**事件流 + 出入站配对 + 判阈值 + 告警」是独立外部探针（cron）的活,
//   不在 Relay 进程内——进程死了进程内自指告警跟着死。故本模块**只写不判不报警**。
//
// 三类事件（对齐承诺清单 responsive 类的对账需求）：
//   req.inbound   —— 入站请求抵达（谁/哪个 session/什么通道）。出入站配对的"入"锚点。
//   turn.end      —— 一个回合收尾（session/stopReason/耗时/token）。答没答、答得好不好的信号。
//   delivery.receipt —— 一次投递结果（session/status=sent|failed/attempts）。答复到没到用户手里。
//
// ── 生产安全（最高优先级,绝不违反）─────────────────────────────────────────────
//   1) dark deploy 开关：默认**关**。仅当 RELAY_OBS_REQUEST_TRACE=true 才落盘。
//      关闭时零开销（提前 return）、零行为变化——主链路一字节不变。
//   2) 全隔离：每个入口全程 try/catch,**永不抛**。埋点自身任何异常都被吞,绝不能因为
//      记日志把用户的请求/回复搞挂。emitObsEvent 本身也吞异常,这里是第二层保险。
//   3) 只旁路观测：不读也不改路由/CC/投递的任何业务对象,不产生副作用（除 append jsonl）。

import { emitObsEvent, type ObsSeverity } from './relay-observability.js';

/**
 * 三类埋点的总开关（独立于 relay-observability 的 RELAY_OBS_ENABLED）。
 * 默认**关**——dark deploy:代码进生产但零行为变化,主身审核后单独打开。
 * 设 `RELAY_OBS_REQUEST_TRACE=true`（大小写不敏感）才开启。
 */
export function isRequestTraceEnabled(env: NodeJS.ProcessEnv = process.env): boolean {
  return (env['RELAY_OBS_REQUEST_TRACE'] ?? '').trim().toLowerCase() === 'true';
}

/** ISO8601 UTC 时间戳（与 obs 事件流一致,便于探针按 ts 分桶）。 */
function nowIso(): string {
  return new Date().toISOString();
}

/** 入站通道类型。 */
export type TraceChannel = 'feishu' | 'telegram';

/**
 * 入站请求埋点。用户经通道发来一条请求,记 {who/session/channel/ts}。
 *
 * @param opts.channel  通道（feishu/telegram）。
 * @param opts.agent    承接的 agent id（承诺主体 unit,对齐 relay.json agents[].id）。
 * @param opts.session  会话定位键（poolKey 或 sessionId）——探针据此把入站配对到出站。
 * @param opts.who      发起方脱敏标识（openId/chatId 前缀,**不落全量 PII**）。
 */
export function traceInboundRequest(
  opts: { channel: TraceChannel; agent?: string; session?: string; who?: string; detail?: Record<string, unknown> },
  env: NodeJS.ProcessEnv = process.env,
): void {
  if (!isRequestTraceEnabled(env)) return;
  try {
    emitObsEvent({
      ts: nowIso(),
      evt: 'req.inbound',
      agent: opts.agent,
      session: opts.session,
      severity: 'info' satisfies ObsSeverity,
      channel: opts.channel,
      ...(opts.who ? { who: opts.who } : {}),
      ...(opts.detail ?? {}),
    }, env);
  } catch {
    // 埋点绝不上抛——第二层保险(emitObsEvent 已吞,这里兜住构造事件时的意外)。
  }
}

/**
 * turn.end 去重判据（缺陷 B,2026-07-15 canary）。
 *
 * 一个 print-mode turn 会产生两条 `type==='result'` 消息：
 *   1) pool.ts finishTurn emit 的合成 `{type:'result', result:''}`——无 subtype/usage/is_error,
 *      是"空"事件（仅用来把 busy→idle,不带回合信号）。
 *   2) 紧接着 stdout emit 的 CC 原始 result——带 subtype/duration_ms/usage/is_error,是"完整"事件。
 * 若两条都打点 → turn.end 计数虚高 + 一条空事件污染对账。
 *
 * 判据=「带实质回合信号」：有 stopReason(subtype) 或 有 usage 即算实质。
 *   - print-mode 空合成 result（既无 subtype 又无 usage）→ false,跳过。
 *   - print-mode 真 result（有 subtype+usage）→ true,打点。
 *   - interactive-runtime 合成 result（无 subtype 但带 usage,见 pool.ts finishTurn 传 usageRecord）
 *     → true,打点（有 usage 即实质,不误伤 interactive 的唯一 turn.end）。
 *
 * 传入的是 CC result 消息（或其等价形态）。只读,不产生副作用。
 */
export function shouldTraceTurnEnd(msg: Record<string, unknown>): boolean {
  const hasSubtype = typeof msg['subtype'] === 'string' && (msg['subtype'] as string).length > 0;
  const usage = msg['usage'];
  const hasUsage = usage !== undefined && usage !== null && typeof usage === 'object';
  return hasSubtype || hasUsage;
}

/**
 * 回合结束埋点。一个 turn 收尾,记 {session/stopReason/耗时/token}。
 * 探针据此判"入站后阈值内有没有合格回复"(responsive 承诺对账)。
 *
 * @param opts.poolKey     带 scope 的池键（切片A 配对主键,如 owner:pulse:feishu）。turn.end 同时带
 *                          poolKey+sessionId,是 req.inbound(poolKey) ↔ delivery(poolKey) 与 CC sessionId 的桥。
 * @param opts.stopReason  回合终止原因（CC result 的 subtype / stop_reason,如 end_turn / max_turns / error）。
 * @param opts.durationMs  回合耗时毫秒（CC result.duration_ms,拿不到给 undefined）。
 * @param opts.inputTokens / outputTokens  token 用量（result.usage,拿不到给 undefined）。
 *                          注意 inputTokens 是**裸**输入,不含缓存;真实输入量 =
 *                          inputTokens + cacheReadTokens + cacheCreationTokens。
 * @param opts.cacheReadTokens / cacheCreationTokens  缓存命中读 / 缓存写入的输入 token
 *                          （result.usage.cache_read_input_tokens / cache_creation_input_tokens）。
 *                          长上下文会话里这两项常比裸 input 高数个量级,漏记会让用量少报上万倍。
 * @param opts.isError     回合是否报错（result.is_error）。
 */
export function traceTurnEnd(
  opts: {
    agent?: string;
    session?: string;
    poolKey?: string;
    stopReason?: string;
    durationMs?: number;
    inputTokens?: number;
    outputTokens?: number;
    cacheReadTokens?: number;
    cacheCreationTokens?: number;
    isError?: boolean;
    detail?: Record<string, unknown>;
  },
  env: NodeJS.ProcessEnv = process.env,
): void {
  if (!isRequestTraceEnabled(env)) return;
  try {
    emitObsEvent({
      ts: nowIso(),
      evt: 'turn.end',
      agent: opts.agent,
      session: opts.session,
      // 报错回合 warn,正常回合 info——探针可据此快速过滤异常终止。
      severity: (opts.isError ? 'warn' : 'info') satisfies ObsSeverity,
      ...(opts.poolKey !== undefined ? { poolKey: opts.poolKey } : {}),
      ...(opts.stopReason !== undefined ? { stopReason: opts.stopReason } : {}),
      ...(typeof opts.durationMs === 'number' ? { durationMs: opts.durationMs } : {}),
      ...(typeof opts.inputTokens === 'number' ? { inputTokens: opts.inputTokens } : {}),
      ...(typeof opts.outputTokens === 'number' ? { outputTokens: opts.outputTokens } : {}),
      ...(typeof opts.cacheReadTokens === 'number' ? { cacheReadTokens: opts.cacheReadTokens } : {}),
      ...(typeof opts.cacheCreationTokens === 'number' ? { cacheCreationTokens: opts.cacheCreationTokens } : {}),
      ...(opts.isError !== undefined ? { isError: opts.isError } : {}),
      ...(opts.detail ?? {}),
    }, env);
  } catch {
    // 永不上抛。
  }
}

/**
 * 投递 receipt 埋点。一次投递结果,记 {session/status/attempts/channel}。
 * 探针据此判"合格回复有没有真的送达用户"(delivery=sent 是 responsive pass 的一环)。
 *
 * @param opts.status    投递状态（sent / failed / skipped）——直接取自 DeliveryReceipt.status。
 * @param opts.attempts  尝试次数（DeliveryReceipt.attempts）。
 * @param opts.channel   投递通道（GatewayChannel,可能是 harness/system,非仅 feishu/telegram）。
 */
export function traceDeliveryReceipt(
  opts: {
    agent?: string;
    session?: string;
    status: string;
    attempts?: number;
    channel?: string;
    detail?: Record<string, unknown>;
  },
  env: NodeJS.ProcessEnv = process.env,
): void {
  if (!isRequestTraceEnabled(env)) return;
  try {
    emitObsEvent({
      ts: nowIso(),
      evt: 'delivery.receipt',
      agent: opts.agent,
      session: opts.session,
      // 投递失败 warn,其余 info——探针据此快速抓"回复没送出去"。
      severity: (opts.status === 'failed' ? 'warn' : 'info') satisfies ObsSeverity,
      status: opts.status,
      ...(typeof opts.attempts === 'number' ? { attempts: opts.attempts } : {}),
      ...(opts.channel ? { channel: opts.channel } : {}),
      ...(opts.detail ?? {}),
    }, env);
  } catch {
    // 永不上抛。
  }
}
