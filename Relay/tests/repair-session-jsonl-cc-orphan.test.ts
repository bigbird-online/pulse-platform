/**
 * CC 自产孤儿 parentUuid 补链 + leaf-reachability 判据单测。
 *
 * 生产事故（2026-07-05，Pulse 主身 session ff326e52 反复断链失忆）：
 *   CC SDK 在高频 tool_use / print-mode 下写 jsonl 会**丢行**——某条正常 assistant/user 记录的
 *   parentUuid 指向一个从没落盘的父记录（孤儿）。旧 validateSessionChain 判据是「存在任一孤儿
 *   parentUuid → 整个 session 判 invalid」→ pool.ts 丢弃 session + 起新 sessionId + 推用户
 *   「对话历史链断裂」→ agent 失忆。但 CC 自己 resume 这些文件完全正常（只从 leaf 回溯能走到的
 *   链，孤儿旁支忽略）。全机 948 jsonl 有 55 个（5%）带这种孤儿，都是「下次 resume 即失忆」的雷。
 *
 *   治法两件套：
 *     A（leaf-reachability 判据）：validateSessionChain 从 leaf 回溯，撞主链 missing 才算断，
 *        旁支孤儿不影响；无 leaf 的文件放行。
 *     B（orphan-rewire pass）：repairSessionJsonl 把 CC 自产孤儿 rewire 到「物理前一条 uuid
 *        记录」→ 主链补齐、直达 root、下次校验/resume 都干净。
 *   实证：全机 55 个真实孤儿文件跑 B 后 55/55 主链全部回溯直达 root。
 */
import { describe, it, expect, beforeEach, afterEach } from 'vitest';
import { mkdtempSync, readFileSync, writeFileSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { repairSessionJsonl, validateSessionChain } from '../src/pool.js';

function readLines(path: string): Array<Record<string, unknown>> {
  return readFileSync(path, 'utf-8')
    .split('\n')
    .filter((l) => l.trim())
    .map((l) => JSON.parse(l) as Record<string, unknown>);
}

/**
 * 复刻旧判据（「存在任一孤儿 parentUuid → invalid」），用来对照证明新判据的差异。
 * 这是 2026-07-05 之前 validateSessionChain 的行为快照，不是被测代码，只作对照。
 */
function oldStrictValidate(path: string): boolean {
  const lines = readFileSync(path, 'utf-8').split('\n');
  const uuids = new Set<string>();
  for (const line of lines) {
    const t = line.trim();
    if (!t) continue;
    try { const r = JSON.parse(t) as { uuid?: string }; if (r.uuid) uuids.add(r.uuid); } catch { /* skip */ }
  }
  for (const line of lines) {
    const t = line.trim();
    if (!t) continue;
    try {
      const r = JSON.parse(t) as { parentUuid?: string };
      if (r.parentUuid && !uuids.has(r.parentUuid)) return false;
    } catch { /* skip */ }
  }
  return true;
}

describe('validateSessionChain — leaf-reachability 判据（A）', () => {
  let tmpDir: string;
  let jsonlPath: string;

  beforeEach(() => {
    tmpDir = mkdtempSync(join(tmpdir(), 'cc-orphan-validate-'));
    jsonlPath = join(tmpDir, 'session.jsonl');
  });
  afterEach(() => {
    rmSync(tmpDir, { recursive: true, force: true });
  });

  it('旁支孤儿不在主链上 → 旧判据误判 invalid，新判据判 valid', () => {
    // 主链：u1(root) → a1 → u2(leaf)。另有一条旁支 orphan s1(parent=ghost)，不在 leaf 回溯路径上。
    writeFileSync(jsonlPath, [
      JSON.stringify({ type: 'user', uuid: 'u1', message: { role: 'user', content: 'hi' } }),
      JSON.stringify({ type: 'assistant', uuid: 'a1', parentUuid: 'u1', message: { role: 'assistant', content: [] } }),
      // 旁支孤儿：parent 悬空，但它不是任何后续记录的祖先（leaf 回溯不经过它）
      JSON.stringify({ type: 'assistant', uuid: 's1', parentUuid: 'ghost', message: { role: 'assistant', content: [] } }),
      JSON.stringify({ type: 'user', uuid: 'u2', parentUuid: 'a1', message: { role: 'user', content: 'bye' } }),
    ].join('\n') + '\n');

    // 旧判据：见 s1 悬空即 invalid（误判）
    expect(oldStrictValidate(jsonlPath)).toBe(false);
    // 新判据：leaf=u2 回溯 u2→a1→u1(root)，s1 是旁支不影响 → valid
    expect(validateSessionChain(jsonlPath).valid).toBe(true);
  });

  it('leaf 直接 parent 悬空 → 新判据仍判 invalid（主链立刻断，与现存 pool.test 预期一致）', () => {
    writeFileSync(jsonlPath, [
      JSON.stringify({ type: 'user', uuid: 'u1', message: { role: 'user', content: 'hi' } }),
      JSON.stringify({ type: 'assistant', uuid: 'a1', parentUuid: 'ghost', message: { role: 'assistant', content: [] } }),
    ].join('\n') + '\n');
    const r = validateSessionChain(jsonlPath);
    expect(r.valid).toBe(false);
    expect(r.missingParentUuid).toBe('ghost');
  });

  it('完整链（到 root）→ valid', () => {
    writeFileSync(jsonlPath, [
      JSON.stringify({ type: 'user', uuid: 'u1', message: { role: 'user', content: 'hi' } }),
      JSON.stringify({ type: 'assistant', uuid: 'a1', parentUuid: 'u1', message: { role: 'assistant', content: [] } }),
    ].join('\n') + '\n');
    expect(validateSessionChain(jsonlPath)).toEqual({ valid: true });
  });

  it('尾部有无 uuid 的 meta 行（last-prompt/ai-title/mode）→ leaf 取最后一条对话记录，判 valid', () => {
    writeFileSync(jsonlPath, [
      JSON.stringify({ type: 'user', uuid: 'u1', message: { role: 'user', content: 'hi' } }),
      JSON.stringify({ type: 'assistant', uuid: 'a1', parentUuid: 'u1', message: { role: 'assistant', content: [] } }),
      // 复刻真实 jsonl 尾部：无 uuid 的 meta 行
      JSON.stringify({ type: 'last-prompt', leafUuid: 'a1' }),
      JSON.stringify({ type: 'ai-title', title: 'x' }),
      JSON.stringify({ type: 'mode', mode: 'default' }),
    ].join('\n') + '\n');
    expect(validateSessionChain(jsonlPath).valid).toBe(true);
  });

  it('parentUuid 环 → 判 invalid（真·不可修复，交给降级）', () => {
    writeFileSync(jsonlPath, [
      JSON.stringify({ type: 'assistant', uuid: 'a1', parentUuid: 'a2', message: { role: 'assistant', content: [] } }),
      JSON.stringify({ type: 'assistant', uuid: 'a2', parentUuid: 'a1', message: { role: 'assistant', content: [] } }),
    ].join('\n') + '\n');
    expect(validateSessionChain(jsonlPath).valid).toBe(false);
  });

  it('空文件 / 纯 meta 行（无 leaf）→ valid（无可断的链）', () => {
    writeFileSync(jsonlPath, JSON.stringify({ type: 'last-prompt', leafUuid: 'x' }) + '\n');
    expect(validateSessionChain(jsonlPath).valid).toBe(true);
  });

  it('根记录 parentUuid 显式为 null → 判 valid（CC 首条 user message 就写 null，真实语料回归抓到的 bug）', () => {
    // 实测 ff326e52 line 2 = {parentUuid: null, uuid, type:'user'}。若把 null 当「指向不存在
    // 的 uuid」处理，回溯到根反而判 invalid（missingParentUuid=null）——真实语料回归专门抓到这条。
    writeFileSync(jsonlPath, [
      JSON.stringify({ type: 'user', uuid: 'u1', parentUuid: null, message: { role: 'user', content: 'hi' } }),
      JSON.stringify({ type: 'assistant', uuid: 'a1', parentUuid: 'u1', message: { role: 'assistant', content: [] } }),
    ].join('\n') + '\n');
    expect(validateSessionChain(jsonlPath)).toEqual({ valid: true });
  });
});

describe('repairSessionJsonl — Pass 2 CC 自产孤儿补链（B）', () => {
  let tmpDir: string;
  let jsonlPath: string;

  beforeEach(() => {
    tmpDir = mkdtempSync(join(tmpdir(), 'cc-orphan-repair-'));
    jsonlPath = join(tmpDir, 'session.jsonl');
  });
  afterEach(() => {
    rmSync(tmpDir, { recursive: true, force: true });
  });

  it('复刻真实事故：主链孤儿 rewire 到物理前一条后，文件 leaf-reachable', () => {
    // 复刻 ff326e52 事故形态：一条正常 assistant，parentUuid 指向全文不存在的 uuid，
    // 且它就在 leaf→root 主链上。物理前一条 uuid 记录是 u2。
    writeFileSync(jsonlPath, [
      JSON.stringify({ type: 'user', uuid: 'u1', message: { role: 'user', content: 'q1' } }),
      JSON.stringify({ type: 'assistant', uuid: 'a1', parentUuid: 'u1', message: { role: 'assistant', content: [] } }),
      JSON.stringify({ type: 'user', uuid: 'u2', parentUuid: 'a1', message: { role: 'user', content: 'q2' } }),
      // 事故行：parentUuid 指向从没落盘的 5c9ba2e5（孤儿），物理前一条是 u2
      JSON.stringify({ type: 'assistant', uuid: 'a2', parentUuid: '5c9ba2e5', message: { role: 'assistant', content: [] } }),
      JSON.stringify({ type: 'user', uuid: 'u3', parentUuid: 'a2', message: { role: 'user', content: 'q3' } }),
    ].join('\n') + '\n');

    // (a) 改前的旧判据会判 invalid（对照函数复刻旧行为）
    expect(oldStrictValidate(jsonlPath)).toBe(false);

    // repair 前——新判据：leaf=u3 回溯 u3→a2→(a2.parent=5c9ba2e5 缺失) 撞主链断点 → invalid
    expect(validateSessionChain(jsonlPath).valid).toBe(false);

    // (c) repairSessionJsonl 后孤儿被 rewire、文件 leaf-reachable
    const changed = repairSessionJsonl(jsonlPath);
    expect(changed).toBe(true);

    const lines = readLines(jsonlPath);
    const a2 = lines.find((l) => l['uuid'] === 'a2')!;
    expect(a2['parentUuid']).toBe('u2'); // rewire 到物理前一条 uuid 记录

    // (b) 新 validateSessionChain 判 valid
    expect(validateSessionChain(jsonlPath).valid).toBe(true);

    // 内容完好：5 条记录一条不少
    const uuids = lines.map((l) => l['uuid']).filter(Boolean);
    expect(uuids).toEqual(['u1', 'a1', 'u2', 'a2', 'u3']);
  });

  it('文件开头第一条就是孤儿（无物理前驱）→ 删 parentUuid 字段视为根', () => {
    writeFileSync(jsonlPath, [
      // 第一条带 uuid 的记录 parentUuid 悬空，前面没有任何 uuid 记录
      JSON.stringify({ type: 'user', uuid: 'u1', parentUuid: 'ghost', message: { role: 'user', content: 'hi' } }),
      JSON.stringify({ type: 'assistant', uuid: 'a1', parentUuid: 'u1', message: { role: 'assistant', content: [] } }),
    ].join('\n') + '\n');

    const changed = repairSessionJsonl(jsonlPath);
    expect(changed).toBe(true);
    const lines = readLines(jsonlPath);
    const u1 = lines.find((l) => l['uuid'] === 'u1')!;
    expect(u1['parentUuid']).toBeUndefined(); // 无 live 前驱 → 删字段变根
    expect(validateSessionChain(jsonlPath).valid).toBe(true);
  });

  it('多个孤儿（含旁支）都被补链，best-effort 不破坏文件', () => {
    // 复刻 ff326e52 的真实分布：2 个旁支孤儿 + 1 个主链孤儿
    writeFileSync(jsonlPath, [
      JSON.stringify({ type: 'user', uuid: 'u1', message: { role: 'user', content: 'q1' } }),
      // 旁支孤儿 1（不是后续任何记录祖先）
      JSON.stringify({ type: 'assistant', uuid: 's1', parentUuid: 'ghostA', message: { role: 'assistant', content: [] } }),
      JSON.stringify({ type: 'assistant', uuid: 'a1', parentUuid: 'u1', message: { role: 'assistant', content: [] } }),
      // 旁支孤儿 2
      JSON.stringify({ type: 'assistant', uuid: 's2', parentUuid: 'ghostB', message: { role: 'assistant', content: [] } }),
      // 主链孤儿
      JSON.stringify({ type: 'user', uuid: 'u2', parentUuid: 'ghostC', message: { role: 'user', content: 'q2' } }),
    ].join('\n') + '\n');

    const changed = repairSessionJsonl(jsonlPath);
    expect(changed).toBe(true);
    const lines = readLines(jsonlPath);
    // 全部 5 条保留
    expect(lines.map((l) => l['uuid'])).toEqual(['u1', 's1', 'a1', 's2', 'u2']);
    // 每个孤儿都补到物理前一条 uuid
    expect(lines.find((l) => l['uuid'] === 's1')!['parentUuid']).toBe('u1');
    expect(lines.find((l) => l['uuid'] === 'a1')!['parentUuid']).toBe('u1');
    expect(lines.find((l) => l['uuid'] === 's2')!['parentUuid']).toBe('a1');
    expect(lines.find((l) => l['uuid'] === 'u2')!['parentUuid']).toBe('s2');
    // 补链后 leaf-reachable
    expect(validateSessionChain(jsonlPath).valid).toBe(true);
  });

  it('无孤儿的干净文件不触发 Pass 2、不改动', () => {
    const original = [
      JSON.stringify({ type: 'user', uuid: 'u1', message: { role: 'user', content: 'hi' } }),
      JSON.stringify({ type: 'assistant', uuid: 'a1', parentUuid: 'u1', message: { role: 'assistant', content: [] } }),
    ].join('\n') + '\n';
    writeFileSync(jsonlPath, original);
    const changed = repairSessionJsonl(jsonlPath);
    expect(changed).toBe(false);
    expect(readFileSync(jsonlPath, 'utf-8')).toBe(original);
  });
});
