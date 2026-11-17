/**
 * Inbound tool-call-leak guard (2026-06-28).
 *
 * 对称于 outbound-leak-guard.ts。出站闸堵的是"自家 CC 把工具调用泄漏成纯文本、不让
 * 它发出门"；但那条链还有一个出站闸够不着的入口：泄漏文本若**已经**沉淀进某个 agent
 * 的飞书卡片，被人工**复制粘贴**（往往还加了前缀，如"X 停顿需要手工介入清理"）贴给
 * **另一个** agent，对接收方而言它就是一条看起来正常的 user text 消息。接收方无法分辨
 * 这是别的 session 的中毒输出，照单全收 —— 2026-06-28 Hestia→Pulse 事故即此。
 *
 * 唯一的拦截点是**入站**：在把 user prompt 喂给 CC 之前检测。
 *
 * 关键差异 vs 出站：入站**不截断**。出站截断保留泄漏起点之前的有效正文照发；但入站
 * 若截断，剩下的前缀（如"X 停顿需要介入清理"）照样会误导 CC 去执行别人的活。所以
 * 入站只做**布尔检测**，处置（替换为安全提示 / 告警）交给调用方（pool.ts:doSend）。
 *
 * 判据与出站**检测部分**完全一致：复用 outbound-leak-guard.ts 导出的 LEAK_TEXT_RE
 * + LEAK_STRUCT_RE 同一组签名（单一真相源 —— 见 CLAUDE.md 同源近邻双扫纪律）。必须
 * 两者同时命中：孤立提及 `<invoke name=`（文档讲语法、agent 讨论这个 bug）不算泄漏。
 */

import { LEAK_TEXT_RE, LEAK_STRUCT_RE } from './outbound-leak-guard.js';

export interface InboundLeakResult {
  /** True iff a structurally-complete tool-call leak was detected in the text. */
  leaked: boolean;
  /** Short human-readable reason (only set when leaked). */
  reason?: string;
}

/**
 * Detect a tool-call leak in an inbound user prompt.
 *
 * Returns `{ leaked: false }` unless BOTH a `<invoke name=` marker and the
 * structural-completeness signature are present — a bare mention of the literal
 * `<invoke name=` (docs, or someone explaining the bug) does NOT trip it.
 *
 * Unlike the outbound guard, this performs detection only (no truncation): the
 * leftover prefix of a re-injected message would itself mislead CC, so the
 * caller must reject/replace the whole message rather than keep a slice.
 */
export function detectInboundToolcallLeak(text: string): InboundLeakResult {
  if (!text || !LEAK_TEXT_RE.test(text)) {
    return { leaked: false };
  }
  // 结构完整性闸：只有"完整粘贴的工具调用结构"才算泄漏，孤立提及不算。
  if (!LEAK_STRUCT_RE.test(text)) {
    return { leaked: false };
  }
  return {
    leaked: true,
    reason: 'inbound text contains a structurally-complete tool-call leak (<invoke name= ...)',
  };
}
