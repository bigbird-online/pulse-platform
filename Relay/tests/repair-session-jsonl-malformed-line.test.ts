/**
 * repairSessionJsonl 畸形行恢复 + splitConcatenatedJsonl 单测。
 *
 * 生产事故（2026-07-04，Pulse 主身 session 0978e17a 被丢弃致失忆）：
 *   busy CC 被 SIGTERM 打断时 jsonl 写盘停在半个 record；下次重启从同一 offset
 *   追加，一行里出现 `{完整user对象}{完整queue-op对象}`（中间无换行）。
 *   validateSessionChain 建 uuids 索引时对该行 JSON.parse 抛错 → 整行被 catch 跳过
 *   → 该行上合法 record 的 uuid 丢失 → 后续指向它的 record 被误判 parentUuid 悬空
 *   → 判 chain broken → 丢弃 session + 推「对话历史链断裂」→ agent 失忆。
 *
 *   治法 = Pass 0 畸形行恢复：splitConcatenatedJsonl 把粘连的多个 JSON 拆回独立行，
 *   救回合法 record；不可救回的截断残片丢弃。此 Pass 在污染扫描/链校验前跑。
 */
import { describe, it, expect, beforeEach, afterEach } from 'vitest';
import { mkdtempSync, readFileSync, writeFileSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { repairSessionJsonl, splitConcatenatedJsonl, validateSessionChain } from '../src/pool.js';

function readLines(path: string): Array<Record<string, unknown>> {
  return readFileSync(path, 'utf-8')
    .split('\n')
    .filter((l) => l.trim())
    .map((l) => JSON.parse(l) as Record<string, unknown>);
}

describe('splitConcatenatedJsonl', () => {
  it('拆开两个粘连的完整 JSON 对象', () => {
    const a = { type: 'user', uuid: 'u1', message: { role: 'user', content: '重启' } };
    const b = { type: 'queue-operation', operation: 'enqueue', ts: 'x' };
    const glued = JSON.stringify(a) + JSON.stringify(b);
    const out = splitConcatenatedJsonl(glued);
    expect(out).toHaveLength(2);
    expect(JSON.parse(out[0]!)).toEqual(a);
    expect(JSON.parse(out[1]!)).toEqual(b);
  });

  it('救回合法段、丢弃尾部未闭合的截断残片', () => {
    const a = { type: 'user', uuid: 'u1', content: 'hi' };
    // a 后面粘了半个截断对象（无闭合）
    const glued = JSON.stringify(a) + '{"type":"queue-operation","ts":"2026-07-04T09:01:1';
    const out = splitConcatenatedJsonl(glued);
    expect(out).toHaveLength(1);
    expect(JSON.parse(out[0]!)).toEqual(a);
  });

  it('字符串内的花括号不误切', () => {
    const a = { type: 'assistant', uuid: 'u1', text: 'code: function(){ return {a:1} }' };
    const b = { type: 'user', uuid: 'u2' };
    const out = splitConcatenatedJsonl(JSON.stringify(a) + JSON.stringify(b));
    expect(out).toHaveLength(2);
    expect(JSON.parse(out[0]!)).toEqual(a);
    expect(JSON.parse(out[1]!)).toEqual(b);
  });

  it('转义引号内的花括号不误切', () => {
    const a = { type: 'user', uuid: 'u1', text: 'quote \\" then { brace' };
    const out = splitConcatenatedJsonl(JSON.stringify(a));
    expect(out).toHaveLength(1);
    expect(JSON.parse(out[0]!)).toEqual(a);
  });

  it('完全无法闭合的单个残片返回空数组', () => {
    expect(splitConcatenatedJsonl('{"type":"user","uuid":"u1')).toEqual([]);
  });
});

describe('repairSessionJsonl — Pass 0 畸形行恢复', () => {
  let tmpDir: string;
  let jsonlPath: string;

  beforeEach(() => {
    tmpDir = mkdtempSync(join(tmpdir(), 'malformed-repair-'));
    jsonlPath = join(tmpDir, 'session.jsonl');
  });
  afterEach(() => {
    rmSync(tmpDir, { recursive: true, force: true });
  });

  it('复刻生产事故：粘连行拆开后 validateSessionChain 不再误判 chain broken', () => {
    // 链：root(u-root) → user(u-mid, 内容"重启") → assistant(u-leaf, parent=u-mid)
    // 但 u-mid 那行被粘连了一个 queue-operation，整行 parse 失败。
    const root = { type: 'user', uuid: 'u-root', message: { role: 'user', content: 'start' } };
    const midUser = { parentUuid: 'u-root', type: 'user', uuid: 'u-mid', message: { role: 'user', content: '重启' } };
    const queueOp = { type: 'queue-operation', operation: 'enqueue', ts: 'x' };
    const leaf = { parentUuid: 'u-mid', type: 'assistant', uuid: 'u-leaf', message: { role: 'assistant', content: [] } };

    const raw =
      JSON.stringify(root) + '\n' +
      // 粘连行：midUser + queueOp 挤成一行，无换行分隔
      JSON.stringify(midUser) + JSON.stringify(queueOp) + '\n' +
      JSON.stringify(leaf) + '\n';
    writeFileSync(jsonlPath, raw);

    // 修复前：链断裂（leaf 的 parent u-mid 丢失）
    const before = validateSessionChain(jsonlPath);
    expect(before.valid).toBe(false);
    expect(before.missingParentUuid).toBe('u-mid');

    // 跑修复
    const changed = repairSessionJsonl(jsonlPath);
    expect(changed).toBe(true);

    // 修复后：粘连行拆回两行，u-mid 重新可见，链完整
    const after = validateSessionChain(jsonlPath);
    expect(after.valid).toBe(true);

    // 内容完好：root/midUser/queueOp/leaf 四条都在
    const lines = readLines(jsonlPath);
    const uuids = lines.map((l) => l['uuid']).filter(Boolean);
    expect(uuids).toContain('u-root');
    expect(uuids).toContain('u-mid');
    expect(uuids).toContain('u-leaf');
    expect(lines.some((l) => l['type'] === 'queue-operation')).toBe(true);
  });

  it('孤立空行不触发修复也不破坏文件', () => {
    const a = { type: 'user', uuid: 'u1', message: { role: 'user', content: 'hi' } };
    const b = { parentUuid: 'u1', type: 'assistant', uuid: 'u2', message: { role: 'assistant', content: [] } };
    // 中间夹一个空行（line 355 那种），是良性的
    writeFileSync(jsonlPath, JSON.stringify(a) + '\n\n' + JSON.stringify(b) + '\n');
    const changed = repairSessionJsonl(jsonlPath);
    // 空行不算畸形（快路径 trim 后为空直接保留），无 tool_use/污染 → 不写盘
    expect(changed).toBe(false);
    expect(validateSessionChain(jsonlPath).valid).toBe(true);
  });

  it('全合法文件走快路径，不改动', () => {
    const a = { type: 'user', uuid: 'u1', message: { role: 'user', content: 'hi' } };
    const b = { parentUuid: 'u1', type: 'assistant', uuid: 'u2', message: { role: 'assistant', content: [] } };
    const original = JSON.stringify(a) + '\n' + JSON.stringify(b) + '\n';
    writeFileSync(jsonlPath, original);
    const changed = repairSessionJsonl(jsonlPath);
    expect(changed).toBe(false);
    expect(readFileSync(jsonlPath, 'utf-8')).toBe(original);
  });
});
