/**
 * repairSessionJsonl pre-spawn session de-poisoning tests.
 *
 * Production root cause (2026-06-17, Ken & Reeve):
 *   The earlier stream-guard fix (commit a868e55) dropped "No response requested."
 *   from channel emit but could not stop CC SDK from writing those records into
 *   the session jsonl. With 3+ such records accumulated, opus reads them as
 *   in-context examples and starts generating "No response requested." itself
 *   — the session becomes permanently poisoned and any new user message returns
 *   the same noop reply.
 *
 *   The real fix has to operate on the jsonl file directly *before* CC --resume,
 *   removing the three classes of poisoning:
 *     1. CC SDK's auto-injected user "Continue from where you left off."
 *     2. synthetic stub assistant "No response requested." (model="<synthetic>")
 *     3. real-model "No response requested." (claude-opus-4-7 etc.)
 *
 *   These tests verify repairSessionJsonl()'s new tail-cleansing pass.
 */
import { describe, it, expect, beforeEach, afterEach } from 'vitest';
import { mkdtempSync, readFileSync, writeFileSync, rmSync, existsSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { repairSessionJsonl } from '../src/pool.js';

function writeJsonl(path: string, records: Array<Record<string, unknown>>): void {
  writeFileSync(path, records.map((r) => JSON.stringify(r)).join('\n') + '\n');
}

function readJsonlLines(path: string): Array<Record<string, unknown>> {
  return readFileSync(path, 'utf-8')
    .split('\n')
    .filter((l) => l.trim())
    .map((l) => JSON.parse(l) as Record<string, unknown>);
}

describe('repairSessionJsonl — session de-poisoning', () => {
  let tmpDir: string;
  let jsonlPath: string;

  beforeEach(() => {
    tmpDir = mkdtempSync(join(tmpdir(), 'repair-poison-'));
    jsonlPath = join(tmpDir, 'session.jsonl');
  });

  afterEach(() => {
    rmSync(tmpDir, { recursive: true, force: true });
  });

  it('A: removes trailing synthetic noop stub', () => {
    writeJsonl(jsonlPath, [
      { type: 'user', uuid: 'u1', message: { role: 'user', content: '正常对话' } },
      { type: 'assistant', uuid: 'a1', parentUuid: 'u1',
        message: { model: 'claude-opus-4-7', role: 'assistant', stop_reason: 'end_turn',
          content: [{ type: 'text', text: '正常回复' }] } },
      { type: 'assistant', uuid: 'a2', parentUuid: 'a1',
        message: { model: '<synthetic>', role: 'assistant', stop_reason: 'stop_sequence',
          content: [{ type: 'text', text: 'No response requested.' }] } },
    ]);
    repairSessionJsonl(jsonlPath);
    const after = readJsonlLines(jsonlPath);
    expect(after).toHaveLength(2);
    expect(after[1]?.uuid).toBe('a1');
  });

  it('B: removes trailing real-model "No response requested." (the actual production bug)', () => {
    writeJsonl(jsonlPath, [
      { type: 'user', uuid: 'u1', message: { role: 'user', content: '正常对话' } },
      { type: 'assistant', uuid: 'a1', parentUuid: 'u1',
        message: { model: 'claude-opus-4-7', role: 'assistant', stop_reason: 'end_turn',
          content: [{ type: 'text', text: '正常回复' }] } },
      // opus 在被前面 synthetic noop 污染后真模型也输出 noop
      { type: 'assistant', uuid: 'a2', parentUuid: 'a1',
        message: { model: 'claude-opus-4-7', role: 'assistant', stop_reason: 'end_turn',
          content: [{ type: 'text', text: 'No response requested.' }] } },
    ]);
    repairSessionJsonl(jsonlPath);
    const after = readJsonlLines(jsonlPath);
    expect(after).toHaveLength(2);
    expect(after[1]?.uuid).toBe('a1');
  });

  it('C: removes CC SDK auto-injected "Continue from where you left off." user message', () => {
    writeJsonl(jsonlPath, [
      { type: 'user', uuid: 'u1', message: { role: 'user', content: '正常对话' } },
      { type: 'assistant', uuid: 'a1', parentUuid: 'u1',
        message: { model: 'claude-opus-4-7', role: 'assistant', stop_reason: 'end_turn',
          content: [{ type: 'text', text: '正常回复' }] } },
      // CC SDK 在 resume 时自注入的
      { type: 'user', uuid: 'u2', parentUuid: 'a1',
        message: { role: 'user', content: [{ type: 'text', text: 'Continue from where you left off.' }] } },
    ]);
    repairSessionJsonl(jsonlPath);
    const after = readJsonlLines(jsonlPath);
    expect(after).toHaveLength(2);
    expect(after[1]?.uuid).toBe('a1');
  });

  it('D: removes a mixed tail (Continue + multiple synthetic + real-model noop) all at once', () => {
    // 这是 Ken 06:47-06:57 真实场景的精简复现
    writeJsonl(jsonlPath, [
      { type: 'user', uuid: 'u1', message: { role: 'user', content: '真用户消息' } },
      { type: 'assistant', uuid: 'a1', parentUuid: 'u1',
        message: { model: 'claude-opus-4-7', role: 'assistant', stop_reason: 'tool_use',
          content: [{ type: 'tool_use', id: 't1', name: 'Bash', input: { command: 'ls' } }] } },
      // tool_result 缺失 → 这是 rate-limit kill 截断
      { type: 'user', uuid: 'u2', parentUuid: 'a1',
        message: { role: 'user', content: [{ type: 'text', text: 'Continue from where you left off.' }] } },
      { type: 'assistant', uuid: 'a2', parentUuid: 'u2',
        message: { model: '<synthetic>', stop_reason: 'stop_sequence',
          content: [{ type: 'text', text: 'No response requested.' }] } },
      { type: 'assistant', uuid: 'a3', parentUuid: 'a2',
        message: { model: '<synthetic>', stop_reason: 'stop_sequence',
          content: [{ type: 'text', text: 'No response requested.' }] } },
      { type: 'assistant', uuid: 'a4', parentUuid: 'a3',
        message: { model: 'claude-opus-4-7', stop_reason: 'end_turn',
          content: [{ type: 'text', text: 'No response requested.' }] } },
    ]);
    repairSessionJsonl(jsonlPath);
    const after = readJsonlLines(jsonlPath);
    // 污染段全清 → 然后 unclosed tool_use (t1) 触发第二阶段截断到 u1 之前
    // 结果：u1 之前全部保留（这里没有内容），u1 本身是 unclosed tool_use 的 parent → truncate to u1 (exclusive)
    // 实际：repair 第二阶段会把 truncateFromLine 设到 u1（parentUuid 链），保留 0 行（u1 之前没东西）。
    // 所以这条测试我们只断言污染行被清掉、不存在 noop / Continue 行了。
    const hasNoop = after.some((r) => {
      const c = (r.message as { content?: Array<{ text?: string }> } | undefined)?.content;
      return Array.isArray(c) && c.some((b) => b.text?.trim() === 'No response requested.');
    });
    const hasContinue = after.some((r) => {
      const c = (r.message as { content?: Array<{ text?: string }> } | undefined)?.content;
      return Array.isArray(c) && c.some((b) => b.text?.trim() === 'Continue from where you left off.');
    });
    expect(hasNoop).toBe(false);
    expect(hasContinue).toBe(false);
  });

  it('E: clean jsonl is untouched (no false-positive truncation)', () => {
    const records = [
      { type: 'user', uuid: 'u1', message: { role: 'user', content: '正常对话' } },
      { type: 'assistant', uuid: 'a1', parentUuid: 'u1',
        message: { model: 'claude-opus-4-7', role: 'assistant', stop_reason: 'end_turn',
          content: [{ type: 'text', text: '正常回复，不含污染字面' }] } },
    ];
    writeJsonl(jsonlPath, records);
    const before = readFileSync(jsonlPath, 'utf-8');
    repairSessionJsonl(jsonlPath);
    const after = readFileSync(jsonlPath, 'utf-8');
    expect(after).toBe(before);
  });

  it('F: missing file is a no-op (returns false, does not crash)', () => {
    expect(existsSync(jsonlPath)).toBe(false);
    expect(repairSessionJsonl(jsonlPath)).toBe(false);
  });

  it('G: removes mid-history poisoning too (to prevent opus in-context learning from picking it up)', () => {
    // 中段污染也清。理由：opus 在 resume 时 in-context learning 会读完整上下文，
    // 中段的 noop 也可能让它学到 noop 模式。
    writeJsonl(jsonlPath, [
      { type: 'user', uuid: 'u1', message: { role: 'user', content: '某条历史' } },
      { type: 'assistant', uuid: 'a1', parentUuid: 'u1',
        message: { model: 'claude-opus-4-7', stop_reason: 'end_turn',
          content: [{ type: 'text', text: 'No response requested.' }] } },  // 中段污染
      { type: 'user', uuid: 'u2', parentUuid: 'a1', message: { role: 'user', content: '新真用户消息' } },
      { type: 'assistant', uuid: 'a2', parentUuid: 'u2',
        message: { model: 'claude-opus-4-7', stop_reason: 'end_turn',
          content: [{ type: 'text', text: '真回复' }] } },
    ]);
    repairSessionJsonl(jsonlPath);
    const after = readJsonlLines(jsonlPath);
    // 中段污染被移除，剩 3 行真对话
    expect(after).toHaveLength(3);
    expect(after.map((r) => r.uuid)).toEqual(['u1', 'u2', 'a2']);
  });

  // H/I/J/K：parentUuid rewire — 防止 validateSessionChain 误判触发 "对话历史链断裂" 通知。
  //
  // 真实事件 (2026-06-18 08:56 ken-space)：Relay 重启 → pre-spawn de-poison 删 1 条
  // synthetic noop → 后续 record parentUuid 指向被删行 → validateSessionChain 判 broken
  // → 起新 sessionId + 推用户通知 → 用户感知"历史消失"。
  // 治法：删行同时 rewire 后续 parentUuid 到 live 祖先。

  it('H: rewires parentUuid past single deleted poison row (the production ken-space bug)', () => {
    writeJsonl(jsonlPath, [
      { type: 'user', uuid: 'u1', message: { role: 'user', content: 'hi' } },
      { type: 'assistant', uuid: 'a1', parentUuid: 'u1',
        message: { model: '<synthetic>', stop_reason: 'stop_sequence',
          content: [{ type: 'text', text: 'No response requested.' }] } },  // 被删
      { type: 'user', uuid: 'u2', parentUuid: 'a1', message: { role: 'user', content: '下一条真消息' } },
      { type: 'assistant', uuid: 'a2', parentUuid: 'u2',
        message: { model: 'claude-opus-4-7', stop_reason: 'end_turn',
          content: [{ type: 'text', text: '真回复' }] } },
    ]);
    repairSessionJsonl(jsonlPath);
    const after = readJsonlLines(jsonlPath);
    expect(after).toHaveLength(3);
    const u2 = after.find((r) => r.uuid === 'u2')!;
    // u2.parentUuid 原本指向 a1（被删），rewire 后应指向 a1 的 parent = u1
    expect(u2.parentUuid).toBe('u1');
    // a2 不动（指向 u2 本来就是 live）
    const a2 = after.find((r) => r.uuid === 'a2')!;
    expect(a2.parentUuid).toBe('u2');
  });

  it('I: rewires through multiple consecutive deleted poison rows', () => {
    // 连续 3 条污染：Continue 注入 + 2 条 synthetic noop
    writeJsonl(jsonlPath, [
      { type: 'user', uuid: 'u1', message: { role: 'user', content: 'hi' } },
      { type: 'assistant', uuid: 'a1', parentUuid: 'u1',
        message: { model: 'claude-opus-4-7', stop_reason: 'end_turn',
          content: [{ type: 'text', text: '正常回复' }] } },
      { type: 'user', uuid: 'u_inj', parentUuid: 'a1',
        message: { role: 'user', content: [{ type: 'text', text: 'Continue from where you left off.' }] } }, // 删
      { type: 'assistant', uuid: 'a_syn1', parentUuid: 'u_inj',
        message: { model: '<synthetic>', stop_reason: 'stop_sequence',
          content: [{ type: 'text', text: 'No response requested.' }] } }, // 删
      { type: 'assistant', uuid: 'a_syn2', parentUuid: 'a_syn1',
        message: { model: '<synthetic>', stop_reason: 'stop_sequence',
          content: [{ type: 'text', text: 'No response requested.' }] } }, // 删
      { type: 'user', uuid: 'u2', parentUuid: 'a_syn2', message: { role: 'user', content: '真用户继续' } },
      { type: 'assistant', uuid: 'a2', parentUuid: 'u2',
        message: { model: 'claude-opus-4-7', stop_reason: 'end_turn',
          content: [{ type: 'text', text: '真回复' }] } },
    ]);
    repairSessionJsonl(jsonlPath);
    const after = readJsonlLines(jsonlPath);
    expect(after).toHaveLength(4);
    const u2 = after.find((r) => r.uuid === 'u2')!;
    // u2.parentUuid 原本 a_syn2 → a_syn1 → u_inj → a1 (live)。穿越三层。
    expect(u2.parentUuid).toBe('a1');
  });

  it('J: poison at very beginning — root record loses parentUuid', () => {
    // 文件开头就被污染，后面第一条 live record 没有 live 祖先 → delete parentUuid
    writeJsonl(jsonlPath, [
      { type: 'user', uuid: 'u_inj',
        message: { role: 'user', content: [{ type: 'text', text: 'Continue from where you left off.' }] } }, // 删
      { type: 'user', uuid: 'u1', parentUuid: 'u_inj', message: { role: 'user', content: '真消息' } },
      { type: 'assistant', uuid: 'a1', parentUuid: 'u1',
        message: { model: 'claude-opus-4-7', stop_reason: 'end_turn',
          content: [{ type: 'text', text: '真回复' }] } },
    ]);
    repairSessionJsonl(jsonlPath);
    const after = readJsonlLines(jsonlPath);
    expect(after).toHaveLength(2);
    const u1 = after.find((r) => r.uuid === 'u1')!;
    // 没有 live 祖先 → parentUuid 字段被删
    expect(u1.parentUuid).toBeUndefined();
  });

  it('K: rewired jsonl passes validateSessionChain (the actual end-state we care about)', async () => {
    // 端到端：repair 后 validateSessionChain 必须判 valid，
    // 否则 ensureProcess 会 emit session-chain-recovery 推送 "对话历史链断裂" 通知给用户。
    const { validateSessionChain } = await import('../src/pool.js');
    writeJsonl(jsonlPath, [
      { type: 'user', uuid: 'u1', message: { role: 'user', content: 'hi' } },
      { type: 'assistant', uuid: 'a1', parentUuid: 'u1',
        message: { model: '<synthetic>', stop_reason: 'stop_sequence',
          content: [{ type: 'text', text: 'No response requested.' }] } },
      { type: 'user', uuid: 'u2', parentUuid: 'a1', message: { role: 'user', content: '真消息' } },
      { type: 'assistant', uuid: 'a2', parentUuid: 'u2',
        message: { model: 'claude-opus-4-7', stop_reason: 'end_turn',
          content: [{ type: 'text', text: '真回复' }] } },
    ]);
    repairSessionJsonl(jsonlPath);
    const chain = validateSessionChain(jsonlPath);
    expect(chain.valid).toBe(true);
  });
});
