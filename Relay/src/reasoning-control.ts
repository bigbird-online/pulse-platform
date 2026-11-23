/**
 * /think 的统一接口：每个会话运行时把同一组档位名映射到自己的推理控制。
 *
 * - cc、cc-sdk：Claude Code 的 effort（`--effort` / SDK `effort`），在进程启动时生效，
 *   所以改档位后要在空闲时重启进程（会话保留，下一轮 resume）。
 * - codex：`model_reasoning_effort`，每轮 `codex exec` 读取，下一轮生效。
 * - agy：档位即模型变体（`gemini-4-neon-high` → `-low`），每轮拉起时换用，下一轮生效；
 *   当前模型族没有该变体时明确拒绝。
 * - pi：原生 RPC 档位，由 Pi 运行时自己列出与热切换（见 commands.ts 的 Pi 分支）。
 *
 * 档位存在会话状态（PersistedSession.reasoningLevel）里，/new 后回到运行时默认。
 * 未设档位时：codex 的默认由 Relay 显式传入，可以说出值与来源；cc、cc-sdk 由 Claude Code 自定，
 * 只报会话 transcript 里上一轮实际记下的档位，不预测。
 */
import { closeSync, fstatSync, openSync, readSync } from 'node:fs';
import type { ConversationReasoningLevel, ConversationRuntimeId } from './types.js';
import { agyModelLevels } from './agy-conversation-runtime.js';
import { resolvePulseCodexReasoningEffortSource } from './codex-defaults.js';

export const REASONING_LEVELS: readonly ConversationReasoningLevel[] = ['off', 'minimal', 'low', 'medium', 'high', 'xhigh', 'max'];

export function parseReasoningLevel(input: string | undefined): ConversationReasoningLevel | undefined {
  const value = input?.trim().toLowerCase();
  return REASONING_LEVELS.find((level) => level === value);
}

export interface ReasoningControl {
  /** 用户看到的控制名。 */
  label: string;
  /** 当前模型可选的档位；agy 依当前模型族而定。 */
  levels(model?: string): ConversationReasoningLevel[];
  /** 档位在进程启动时读取：空闲进程须重启（会话保留）才能在下一轮生效。 */
  restartToApply: boolean;
}

const CLAUDE_EFFORTS: ConversationReasoningLevel[] = ['low', 'medium', 'high', 'xhigh', 'max'];
const CODEX_EFFORTS: ConversationReasoningLevel[] = ['low', 'medium', 'high', 'xhigh'];

const CONTROLS: Record<Exclude<ConversationRuntimeId, 'pi'>, ReasoningControl> = {
  cc: { label: 'effort', levels: () => [...CLAUDE_EFFORTS], restartToApply: true },
  'cc-sdk': { label: 'effort', levels: () => [...CLAUDE_EFFORTS], restartToApply: true },
  codex: { label: 'reasoning effort', levels: () => [...CODEX_EFFORTS], restartToApply: false },
  agy: { label: '模型变体', levels: (model) => agyModelLevels(model), restartToApply: false },
};

export function reasoningControl(runtime: Exclude<ConversationRuntimeId, 'pi'>): ReasoningControl {
  return CONTROLS[runtime];
}

/** Codex 未设档位时 Relay 显式传入的 reasoning effort 及其来源。 */
export function describeCodexDefaultReasoning(): string {
  const { value, source } = resolvePulseCodexReasoningEffortSource();
  return `默认（${value}，来自 ${source}）`;
}

/** Claude Code 在 transcript 里记下的一轮实际档位。 */
export interface ClaudeTurnEffort {
  effort: string;
  model?: string;
}

const TRANSCRIPT_CHUNK_BYTES = 256 * 1024;
const TRANSCRIPT_SCAN_LIMIT_BYTES = 8 * 1024 * 1024;

function effortOfRecord(line: string): ClaudeTurnEffort | undefined {
  if (!line.includes('"effort"')) return undefined;
  try {
    const record = JSON.parse(line) as Record<string, unknown>;
    if (record['type'] !== 'assistant' || typeof record['effort'] !== 'string' || !record['effort']) return undefined;
    const message = record['message'] as Record<string, unknown> | undefined;
    const model = typeof record['requestedModel'] === 'string' && record['requestedModel']
      ? record['requestedModel'] : typeof message?.['model'] === 'string' ? message['model'] : undefined;
    return { effort: record['effort'], ...(model ? { model } : {}) };
  } catch {
    return undefined;
  }
}

/**
 * 会话 transcript 中最后一条带 effort 的 assistant 记录。从文件尾倒着读，最多读 8 MiB；
 * 第一个能读的文件说了算，读不到或没有这样的记录返回 undefined。
 */
export function lastClaudeTurnEffort(paths: string[]): ClaudeTurnEffort | undefined {
  for (const path of paths) {
    let fd: number;
    try {
      fd = openSync(path, 'r');
    } catch {
      continue;
    }
    try {
      let end = fstatSync(fd).size;
      const floor = Math.max(0, end - TRANSCRIPT_SCAN_LIMIT_BYTES);
      let carry = Buffer.alloc(0);
      while (end > floor) {
        const start = Math.max(floor, end - TRANSCRIPT_CHUNK_BYTES);
        const chunk = Buffer.alloc(end - start);
        readSync(fd, chunk, 0, chunk.length, start);
        const text = Buffer.concat([chunk, carry]);
        end = start;
        // Bytes before the first newline may be the tail of a record that starts further back.
        let body = text;
        carry = Buffer.alloc(0);
        if (start > 0) {
          const newline = text.indexOf(0x0a);
          if (newline < 0) {
            carry = text;
            continue;
          }
          carry = text.subarray(0, newline);
          body = text.subarray(newline + 1);
        }
        const lines = body.toString('utf8').split('\n');
        for (let i = lines.length - 1; i >= 0; i -= 1) {
          const found = effortOfRecord(lines[i]!);
          if (found) return found;
        }
      }
      return undefined;
    } catch {
      return undefined;
    } finally {
      closeSync(fd);
    }
  }
  return undefined;
}

/** cc、cc-sdk 卡片"当前"行：上一轮实际档位，读不到就不报具体值。 */
export function describeClaudeDefaultReasoning(turn: ClaudeTurnEffort | undefined): string {
  if (!turn) return '默认（由 Claude Code 决定，首轮回复后可见实际档位）';
  return `默认（上一轮实际 ${turn.effort}${turn.model ? `，模型 ${turn.model}` : ''}）`;
}
