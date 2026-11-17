/**
 * repairSessionJsonl tool-call leak repair tests (Layer 2, 2026-06-26).
 *
 * Production root cause:
 *   交互式 CC（opus print mode `claude -p --resume`）偶发把工具调用写成纯文本而非
 *   结构化 tool_use block。API 以 stop_reason=end_turn 收尾（不是 tool_use），CC 不执行
 *   文本里的 invoke 标签，session 永久静默停摆。print mode 每轮 --resume 重载整个 jsonl，
 *   泄漏 turn 落盘后被当 few-shot 范例反复复学 → 越来越频繁。
 *
 *   治本：spawn --resume 前从泄漏起点截掉尾巴，保留前半截真正文；整条只剩泄漏的行删除 + rewire。
 *
 * 检测签名：assistant 消息，stop_reason ∈ {end_turn, stop_sequence}，无真 tool_use block，
 * text block 含 invoke 标签。若该 turn 有真 tool_use block → 不是泄漏（模型在讨论/展示语法）。
 */
import { describe, it, expect, beforeEach, afterEach } from 'vitest';
import { mkdtempSync, readFileSync, writeFileSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { repairSessionJsonl, validateSessionChain } from '../src/pool.js';

// 用拼接构造 invoke 标签字面，避免源码里出现裸 markup 干扰编辑器/工具。
const LT = '<';
const GT = '>';
function invokeTag(name = 'Read'): string {
  return `${LT}invoke name="${name}"${GT}\n${LT}parameter name="file_path"${GT}/path/x.md${LT}/parameter${GT}\n${LT}/invoke${GT}`;
}
function antmlInvokeTag(): string {
  return `${LT}antml:invoke name="Read"${GT}\n${LT}/antml:invoke${GT}`;
}
// 孤立提及：只有开标签、后面没有 parameter / 闭合 → 不算结构完整泄漏。
function isolatedInvokeMention(): string {
  return `${LT}invoke name="Read"${GT}`;
}
// 泄漏正则（与 pool.ts TOOLCALL_LEAK_TEXT_RE 等价），断言用。
const INVOKE_RE = /<(?:antml:)?invoke name=/;

function writeJsonl(path: string, records: Array<Record<string, unknown>>): void {
  writeFileSync(path, records.map((r) => JSON.stringify(r)).join('\n') + '\n');
}

function readJsonlLines(path: string): Array<Record<string, unknown>> {
  return readFileSync(path, 'utf-8')
    .split('\n')
    .filter((l) => l.trim())
    .map((l) => JSON.parse(l) as Record<string, unknown>);
}

function textOf(record: Record<string, unknown> | undefined): string {
  const content = (record?.message as { content?: Array<{ type?: string; text?: string }> } | undefined)?.content;
  if (!Array.isArray(content)) return '';
  return content
    .filter((b) => b?.type === 'text' && typeof b.text === 'string')
    .map((b) => b.text ?? '')
    .join('');
}

describe('repairSessionJsonl — tool-call leak repair', () => {
  let tmpDir: string;
  let jsonlPath: string;

  beforeEach(() => {
    tmpDir = mkdtempSync(join(tmpdir(), 'repair-leak-'));
    jsonlPath = join(tmpDir, 'session.jsonl');
    delete process.env['RELAY_ENABLE_TOOLCALL_LEAK_REPAIR'];
  });

  afterEach(() => {
    rmSync(tmpDir, { recursive: true, force: true });
    delete process.env['RELAY_ENABLE_TOOLCALL_LEAK_REPAIR'];
  });

  it('A: trims the leaked invoke tail and keeps the leading real prose', () => {
    const leakTail = `\n\ncore\n${invokeTag()}`;
    writeJsonl(jsonlPath, [
      { type: 'user', uuid: 'u1', message: { role: 'user', content: '帮我读一下文件' } },
      { type: 'assistant', uuid: 'a1', parentUuid: 'u1',
        message: { model: 'claude-opus-4-7', role: 'assistant', stop_reason: 'end_turn',
          content: [{ type: 'text', text: `好的，我来读一下这个文件。${leakTail}` }] } },
    ]);
    repairSessionJsonl(jsonlPath);
    const after = readJsonlLines(jsonlPath);
    expect(after).toHaveLength(2);
    const a1 = after.find((r) => r.uuid === 'a1');
    const t = textOf(a1);
    // (a) 泄漏 invoke 不再出现；(b) 真正文完整保留。
    expect(t).not.toMatch(INVOKE_RE);
    expect(t).toBe('好的，我来读一下这个文件。');
  });

  it('B: drops the row when text is purely a leak and rewires parentUuid', () => {
    writeJsonl(jsonlPath, [
      { type: 'user', uuid: 'u1', message: { role: 'user', content: '读文件' } },
      { type: 'assistant', uuid: 'a1', parentUuid: 'u1',
        message: { model: 'claude-opus-4-7', role: 'assistant', stop_reason: 'end_turn',
          content: [{ type: 'text', text: '正常回复一' }] } },
      // 整条只剩泄漏（无前导正文）→ 删除。
      { type: 'assistant', uuid: 'a_leak', parentUuid: 'a1',
        message: { model: 'claude-opus-4-7', role: 'assistant', stop_reason: 'end_turn',
          content: [{ type: 'text', text: `core\n${invokeTag()}` }] } },
      { type: 'user', uuid: 'u2', parentUuid: 'a_leak', message: { role: 'user', content: '下一条' } },
      { type: 'assistant', uuid: 'a2', parentUuid: 'u2',
        message: { model: 'claude-opus-4-7', role: 'assistant', stop_reason: 'end_turn',
          content: [{ type: 'text', text: '真回复' }] } },
    ]);
    repairSessionJsonl(jsonlPath);
    const after = readJsonlLines(jsonlPath);
    // a_leak 被删，剩 4 行。
    expect(after.map((r) => r.uuid)).toEqual(['u1', 'a1', 'u2', 'a2']);
    // (c) parentUuid 链不破：u2 原指 a_leak（被删）→ rewire 到 a1。
    const u2 = after.find((r) => r.uuid === 'u2')!;
    expect(u2.parentUuid).toBe('a1');
    expect(validateSessionChain(jsonlPath).valid).toBe(true);
  });

  it('C: rebases last-prompt leafUuid when it points at a fully-deleted leak assistant', () => {
    writeJsonl(jsonlPath, [
      { type: 'user', uuid: 'u1', message: { role: 'user', content: 'hi' } },
      { type: 'assistant', uuid: 'a1', parentUuid: 'u1',
        message: { model: 'claude-opus-4-7', role: 'assistant', stop_reason: 'end_turn',
          content: [{ type: 'text', text: '正常回复' }] } },
      { type: 'assistant', uuid: 'a_leak', parentUuid: 'a1',
        message: { model: 'claude-opus-4-7', role: 'assistant', stop_reason: 'end_turn',
          content: [{ type: 'text', text: invokeTag('Bash') }] } },
      // last-prompt 元数据行，leafUuid 指向被删的 a_leak。
      { type: 'last-prompt', leafUuid: 'a_leak' },
    ]);
    repairSessionJsonl(jsonlPath);
    const after = readJsonlLines(jsonlPath);
    const lastPrompt = after.find((r) => r.type === 'last-prompt')!;
    // (d) leafUuid rebase 回 a_leak 的 parent = a1。
    expect(lastPrompt.leafUuid).toBe('a1');
    expect(after.some((r) => r.uuid === 'a_leak')).toBe(false);
  });

  it('D: flag=false leaves the leak untouched (behavior is reversible)', () => {
    process.env['RELAY_ENABLE_TOOLCALL_LEAK_REPAIR'] = 'false';
    const leakTail = `\n\ncore\n${invokeTag()}`;
    const records = [
      { type: 'user', uuid: 'u1', message: { role: 'user', content: '读文件' } },
      { type: 'assistant', uuid: 'a1', parentUuid: 'u1',
        message: { model: 'claude-opus-4-7', role: 'assistant', stop_reason: 'end_turn',
          content: [{ type: 'text', text: `好的。${leakTail}` }] } },
    ];
    writeJsonl(jsonlPath, records);
    const before = readFileSync(jsonlPath, 'utf-8');
    repairSessionJsonl(jsonlPath);
    const after = readFileSync(jsonlPath, 'utf-8');
    // flag 关 → 泄漏原样保留。
    expect(after).toBe(before);
    expect(after).toMatch(INVOKE_RE);
  });

  it('E: does NOT touch invoke text when the turn has a real tool_use block (false-positive guard)', () => {
    // 模型在讨论/展示 invoke 语法，同时真的调用了工具 → 不是泄漏。
    const records = [
      { type: 'user', uuid: 'u1', message: { role: 'user', content: '解释工具调用语法' } },
      { type: 'assistant', uuid: 'a1', parentUuid: 'u1',
        message: { model: 'claude-opus-4-7', role: 'assistant', stop_reason: 'tool_use',
          content: [
            { type: 'text', text: `工具调用的语法是 ${LT}invoke name="Read"${GT}...，我现在调用一下：` },
            { type: 'tool_use', id: 't1', name: 'Read', input: { file_path: '/x' } },
          ] } },
      // 配上 tool_result 让 unclosed-tool_use pass 不截断。
      { type: 'user', uuid: 'u2', parentUuid: 'a1',
        message: { role: 'user', content: [{ type: 'tool_result', tool_use_id: 't1', content: 'ok' }] } },
    ];
    writeJsonl(jsonlPath, records);
    const before = readFileSync(jsonlPath, 'utf-8');
    repairSessionJsonl(jsonlPath);
    const after = readFileSync(jsonlPath, 'utf-8');
    // 有真 tool_use → text 里的 invoke 不被误伤，文件不动。
    expect(after).toBe(before);
    expect(textOf(readJsonlLines(jsonlPath).find((r) => r.uuid === 'a1'))).toMatch(INVOKE_RE);
  });

  it('F: bare invoke tail (no fence prefix) is trimmed via fallback regex', () => {
    writeJsonl(jsonlPath, [
      { type: 'user', uuid: 'u1', message: { role: 'user', content: 'go' } },
      { type: 'assistant', uuid: 'a1', parentUuid: 'u1',
        message: { model: 'claude-opus-4-7', role: 'assistant', stop_reason: 'end_turn',
          content: [{ type: 'text', text: `我去读取文件。\n${invokeTag()}` }] } },
    ]);
    repairSessionJsonl(jsonlPath);
    const a1 = readJsonlLines(jsonlPath).find((r) => r.uuid === 'a1');
    expect(textOf(a1)).toBe('我去读取文件。');
    expect(textOf(a1)).not.toMatch(INVOKE_RE);
  });

  it('G: antml-prefixed invoke variant is detected and trimmed', () => {
    writeJsonl(jsonlPath, [
      { type: 'user', uuid: 'u1', message: { role: 'user', content: 'go' } },
      { type: 'assistant', uuid: 'a1', parentUuid: 'u1',
        message: { model: 'claude-opus-4-7', role: 'assistant', stop_reason: 'stop_sequence',
          content: [{ type: 'text', text: `处理中。\n\ncore_tools\n${antmlInvokeTag()}` }] } },
    ]);
    repairSessionJsonl(jsonlPath);
    const a1 = readJsonlLines(jsonlPath).find((r) => r.uuid === 'a1');
    expect(textOf(a1)).toBe('处理中。');
    expect(textOf(a1)).not.toMatch(INVOKE_RE);
  });

  it('H: clean jsonl (no leak) is untouched', () => {
    const records = [
      { type: 'user', uuid: 'u1', message: { role: 'user', content: '正常对话' } },
      { type: 'assistant', uuid: 'a1', parentUuid: 'u1',
        message: { model: 'claude-opus-4-7', role: 'assistant', stop_reason: 'end_turn',
          content: [{ type: 'text', text: '正常回复，无任何泄漏字面' }] } },
    ];
    writeJsonl(jsonlPath, records);
    const before = readFileSync(jsonlPath, 'utf-8');
    repairSessionJsonl(jsonlPath);
    expect(readFileSync(jsonlPath, 'utf-8')).toBe(before);
  });

  it('I: "call" fence (not just "core") is eaten cleanly, CJK prose preserved', () => {
    // 全机真泄漏 fence 词不止 core，还有 call；中文正文末句完整保留，fence 词不残留。
    const text = `好的，我现在去读取这个文件看看内容。\n\ncall\n${invokeTag()}`;
    writeJsonl(jsonlPath, [
      { type: 'user', uuid: 'u1', message: { role: 'user', content: '读文件' } },
      { type: 'assistant', uuid: 'a1', parentUuid: 'u1',
        message: { model: 'claude-opus-4-7', role: 'assistant', stop_reason: 'end_turn',
          content: [{ type: 'text', text }] } },
    ]);
    repairSessionJsonl(jsonlPath);
    const t = textOf(readJsonlLines(jsonlPath).find((r) => r.uuid === 'a1'));
    // 中文正文完整保留，call fence + invoke 全被吃掉，无残留。
    expect(t).toBe('好的，我现在去读取这个文件看看内容。');
    expect(t).not.toMatch(/\bcall\b/);
    expect(t).not.toMatch(INVOKE_RE);
  });

  it('J: multi-word fence (core_directive_acknowledged) is eaten, CJV prose preserved', () => {
    const text = `这是正文最后一句正常的中文句子。\ncore_directive_acknowledged\n${invokeTag('Bash')}`;
    writeJsonl(jsonlPath, [
      { type: 'user', uuid: 'u1', message: { role: 'user', content: 'go' } },
      { type: 'assistant', uuid: 'a1', parentUuid: 'u1',
        message: { model: 'claude-opus-4-7', role: 'assistant', stop_reason: 'end_turn',
          content: [{ type: 'text', text }] } },
    ]);
    repairSessionJsonl(jsonlPath);
    const t = textOf(readJsonlLines(jsonlPath).find((r) => r.uuid === 'a1'));
    expect(t).toBe('这是正文最后一句正常的中文句子。');
    expect(t).not.toMatch(/core_directive_acknowledged/);
    expect(t).not.toMatch(INVOKE_RE);
  });

  it('K: isolated <invoke name=> mention with NO parameter/close is NOT a leak (struct guard, false-positive defense)', () => {
    // 正文在【讨论】invoke 语法、只孤立提了一句开标签、后面没 parameter 也没闭合 → 不判泄漏、不截断。
    const text = `工具调用的开始标签写作 ${isolatedInvokeMention()}，这是它的语法说明，我先解释到这里。`;
    const records = [
      { type: 'user', uuid: 'u1', message: { role: 'user', content: '讲讲 invoke 语法' } },
      { type: 'assistant', uuid: 'a1', parentUuid: 'u1',
        message: { model: 'claude-opus-4-7', role: 'assistant', stop_reason: 'end_turn',
          content: [{ type: 'text', text }] } },
    ];
    writeJsonl(jsonlPath, records);
    const before = readFileSync(jsonlPath, 'utf-8');
    repairSessionJsonl(jsonlPath);
    const after = readFileSync(jsonlPath, 'utf-8');
    // 文件一字未动，正文（含孤立 invoke 提及）完整保留。
    expect(after).toBe(before);
    expect(textOf(readJsonlLines(jsonlPath).find((r) => r.uuid === 'a1'))).toBe(text);
  });

  it('L: full invoke+parameter structure IS a leak and gets trimmed (struct guard keeps original function)', () => {
    // 与 K 对照：完整结构（invoke + parameter）即真泄漏 → 截断。
    const text = `我来读取文件。\ncall\n${invokeTag()}`;
    writeJsonl(jsonlPath, [
      { type: 'user', uuid: 'u1', message: { role: 'user', content: 'go' } },
      { type: 'assistant', uuid: 'a1', parentUuid: 'u1',
        message: { model: 'claude-opus-4-7', role: 'assistant', stop_reason: 'end_turn',
          content: [{ type: 'text', text }] } },
    ]);
    repairSessionJsonl(jsonlPath);
    const t = textOf(readJsonlLines(jsonlPath).find((r) => r.uuid === 'a1'));
    expect(t).toBe('我来读取文件。');
    expect(t).not.toMatch(INVOKE_RE);
  });

  it('M: invoke open tag with closing </invoke> but no parameter still counts as leak (close satisfies struct)', () => {
    const text = `处理一下。\ncore\n${antmlInvokeTag()}`;
    writeJsonl(jsonlPath, [
      { type: 'user', uuid: 'u1', message: { role: 'user', content: 'go' } },
      { type: 'assistant', uuid: 'a1', parentUuid: 'u1',
        message: { model: 'claude-opus-4-7', role: 'assistant', stop_reason: 'end_turn',
          content: [{ type: 'text', text }] } },
    ]);
    repairSessionJsonl(jsonlPath);
    const t = textOf(readJsonlLines(jsonlPath).find((r) => r.uuid === 'a1'));
    expect(t).toBe('处理一下。');
    expect(t).not.toMatch(INVOKE_RE);
  });

  // --- 第三形态（2026-06-27）：stop_reason=tool_use + 结构完整 <invoke> 文本 ---
  // 模型把工具调用写成裸文本被 CC SDK 拦下 retry，但泄漏文本仍落盘，record 的
  // stop_reason 是 'tool_use'（不是 end_turn）。旧判据只认 end_turn 漏了它，resume
  // 时仍被当 few-shot 种子复学。Pulse 自身 session 5c5912c7 L114 即此形态。
  it('N: stop_reason=tool_use with a STRUCTURALLY-COMPLETE invoke leak IS trimmed (third form)', () => {
    const leakTail = `\n\ncall\n${invokeTag('Bash')}`;
    writeJsonl(jsonlPath, [
      { type: 'user', uuid: 'u1', message: { role: 'user', content: '继续' } },
      { type: 'assistant', uuid: 'a1', parentUuid: 'u1',
        message: { model: 'claude-opus-4-7', role: 'assistant', stop_reason: 'tool_use',
          content: [{ type: 'text', text: `先给你结论。${leakTail}` }] } },
    ]);
    repairSessionJsonl(jsonlPath);
    const t = textOf(readJsonlLines(jsonlPath).find((r) => r.uuid === 'a1'));
    expect(t).toBe('先给你结论。');
    expect(t).not.toMatch(INVOKE_RE);
  });

  it('O: stop_reason=tool_use streaming intermediate (text only, NO complete invoke) is NOT touched', () => {
    // 流式中间态：text block 先落盘、真 tool_use 在后续 record。全机 30954 处，绝不能动。
    // 无 <invoke> 文本 → 既不命中第三形态（无结构），也不命中 bare-fence（非 fence 词）。
    const prose = '我搜索一下相关文件，scp 上去看看。';
    writeJsonl(jsonlPath, [
      { type: 'user', uuid: 'u1', message: { role: 'user', content: 'go' } },
      { type: 'assistant', uuid: 'a1', parentUuid: 'u1',
        message: { model: 'claude-opus-4-7', role: 'assistant', stop_reason: 'tool_use',
          content: [{ type: 'text', text: prose }] } },
    ]);
    const before = readFileSync(jsonlPath, 'utf-8');
    repairSessionJsonl(jsonlPath);
    expect(readFileSync(jsonlPath, 'utf-8')).toBe(before);
  });
});

/**
 * Bare-fence leak variant (2026-06-26, second form).
 * 模型只吐工具调用前的 fence 引导词（course / core），真 <invoke> block 没出来，
 * stop_reason 仍是 'tool_use' 却无 tool_use block。旧 <invoke> 检测器看不到。
 * 签名零误伤靠白名单 fence 词 + 段首位置；正常 "stop=tool_use 无 tool_use block"
 * 的中间态文本（"我搜索一下"…）与句中讨论（"course." 加引号）都不命中。
 */
describe('repairSessionJsonl — bare-fence leak variant', () => {
  let tmpDir: string;
  let jsonlPath: string;

  beforeEach(() => {
    tmpDir = mkdtempSync(join(tmpdir(), 'repair-fence-'));
    jsonlPath = join(tmpDir, 'session.jsonl');
    delete process.env['RELAY_ENABLE_TOOLCALL_LEAK_REPAIR'];
  });
  afterEach(() => {
    rmSync(tmpDir, { recursive: true, force: true });
    delete process.env['RELAY_ENABLE_TOOLCALL_LEAK_REPAIR'];
  });

  function fenceRecord(text: string) {
    return {
      type: 'assistant', uuid: 'a1', parentUuid: 'u1',
      message: { model: 'claude-opus-4-8', role: 'assistant', stop_reason: 'tool_use',
        content: [{ type: 'text', text }] },
    };
  }
  const userRow = { type: 'user', uuid: 'u1', message: { role: 'user', content: '继续' } };

  it('trims a leaked bare "course." prelude and keeps the leading prose', () => {
    writeJsonl(jsonlPath, [userRow, fenceRecord('先看当前 busy 状态：\n\ncourse. tsc + 跑测试：')]);
    repairSessionJsonl(jsonlPath);
    const t = textOf(readJsonlLines(jsonlPath).find((r) => r.uuid === 'a1'));
    expect(t).toBe('先看当前 busy 状态：');
    expect(t).not.toContain('course');
  });

  it('trims a leaked "course" prelude with CC turn number', () => {
    writeJsonl(jsonlPath, [userRow, fenceRecord('好，我整理一下。\ncourse 161 产物是 onboarding-review.md')]);
    repairSessionJsonl(jsonlPath);
    const t = textOf(readJsonlLines(jsonlPath).find((r) => r.uuid === 'a1'));
    expect(t).toBe('好，我整理一下。');
  });

  it('trims a leaked "course of action:" prelude (Duke 07d2d390 变体，2026-07-04)', () => {
    // Opus 解码退化的新措辞：段首 `course of action: <中文祈使>`。整行是机器计划残文，
    // 前面才是真正文 → 切到块尾。旧 BARE_FENCE_ASCII_RE 因"course+空格+词"边界漏放。
    writeJsonl(jsonlPath, [userRow, fenceRecord('我先核当前真实文件状态,不凭记忆推。\n\ncourse of action: 核 640 三件套改到哪了。')]);
    repairSessionJsonl(jsonlPath);
    const t = textOf(readJsonlLines(jsonlPath).find((r) => r.uuid === 'a1'));
    expect(t).toBe('我先核当前真实文件状态,不凭记忆推。');
    expect(t).not.toContain('course of action');
  });

  it('does NOT touch "course of action:" quoted / mid-sentence (讨论态零误伤)', () => {
    // 本 session 讨论这个 bug 本身：引号/反引号包裹 或 句中出现，都不是段首泄漏。
    for (const text of [
      '我认为正确的 course of action: 先修 bug 再重启。',      // 句中
      '这个短语 `course of action:` 是机器泄漏签名。',          // 反引号
      'the fixed phrase "course of action:" as a signature',    // 英文引号
    ]) {
      writeJsonl(jsonlPath, [userRow, fenceRecord(text)]);
      repairSessionJsonl(jsonlPath);
      expect(textOf(readJsonlLines(jsonlPath).find((r) => r.uuid === 'a1'))).toBe(text);
    }
  });

  it('preserves interleaved prose in scattered court form (切到底不误删穿插正文)', () => {
    // court\n\n真正文\n\ncourt 散落型：cut-to-end 必须只切 fence 行，穿插的真正文保留。
    writeJsonl(jsonlPath, [userRow, fenceRecord('真正文一。\n\ncourt\n\n真正文二。\n\ncourt')]);
    repairSessionJsonl(jsonlPath);
    const t = textOf(readJsonlLines(jsonlPath).find((r) => r.uuid === 'a1'));
    expect(t).toContain('真正文一');
    expect(t).toContain('真正文二');
    expect(t).not.toContain('court');
  });

  it('drops the row + rewires when the block is purely a fence leak', () => {
    writeJsonl(jsonlPath, [
      userRow,
      { type: 'assistant', uuid: 'a_keep', parentUuid: 'u1',
        message: { role: 'assistant', stop_reason: 'end_turn', content: [{ type: 'text', text: '上一轮正常回复' }] } },
      { type: 'assistant', uuid: 'a_leak', parentUuid: 'a_keep',
        message: { model: 'claude-opus-4-8', role: 'assistant', stop_reason: 'tool_use',
          content: [{ type: 'text', text: 'course. commit：' }] } },
      { type: 'assistant', uuid: 'a_next', parentUuid: 'a_leak',
        message: { role: 'assistant', stop_reason: 'end_turn', content: [{ type: 'text', text: '后续' }] } },
    ]);
    repairSessionJsonl(jsonlPath);
    const after = readJsonlLines(jsonlPath);
    expect(after.find((r) => r.uuid === 'a_leak')).toBeUndefined(); // dropped
    // 链 rewire：a_next 的 parentUuid 重指到 a_keep（穿越被删的 a_leak）。
    expect(after.find((r) => r.uuid === 'a_next')!.parentUuid).toBe('a_keep');
    expect(validateSessionChain(jsonlPath).valid).toBe(true);
  });

  // --- 零误伤守卫 ---
  it('does NOT touch a normal "stop=tool_use, no tool_use block" intermediate text', () => {
    // 流式中间态：文本块先落盘，tool_use 在后续 record。海量正常，绝不能动。
    const text = '我搜索一下三款车的最新信息。';
    writeJsonl(jsonlPath, [userRow, fenceRecord(text)]);
    repairSessionJsonl(jsonlPath);
    expect(textOf(readJsonlLines(jsonlPath).find((r) => r.uuid === 'a1'))).toBe(text);
  });

  it('does NOT touch a normal Chinese sentence starting with an ASCII term', () => {
    // "outbox 文件…" / "harness CLI…" / "scp 上去…" 都以英文术语开头但是正常句子。
    for (const text of ['outbox 文件被自动清理掉了，我重写一份。', 'harness CLI 真可用。现在查注册状态：', 'scp 上去，然后翻 done。']) {
      writeJsonl(jsonlPath, [userRow, fenceRecord(text)]);
      repairSessionJsonl(jsonlPath);
      expect(textOf(readJsonlLines(jsonlPath).find((r) => r.uuid === 'a1'))).toBe(text);
    }
  });

  it('does NOT touch "of course" or a course.md filename', () => {
    for (const text of ['这当然 of course 没问题，我继续。', '我已写入 ref-andrew-ng-claude-code-course.md 并加索引。']) {
      writeJsonl(jsonlPath, [userRow, fenceRecord(text)]);
      repairSessionJsonl(jsonlPath);
      expect(textOf(readJsonlLines(jsonlPath).find((r) => r.uuid === 'a1'))).toBe(text);
    }
  });

  it('does NOT touch "course." quoted mid-sentence (discussing the phenomenon)', () => {
    // e51bd9e2 case：本 session 在引号里讨论 "course." 现象，不是泄漏。
    const text = '我发现飞书经常出现"course."字样，这是什么意思？';
    writeJsonl(jsonlPath, [userRow, fenceRecord(text)]);
    repairSessionJsonl(jsonlPath);
    expect(textOf(readJsonlLines(jsonlPath).find((r) => r.uuid === 'a1'))).toBe(text);
  });

  it('does NOT touch a fence word when stop_reason is not tool_use', () => {
    // 没有 "声明调工具" 的硬前置 → 不命中（course 作正常词）。
    writeJsonl(jsonlPath, [
      userRow,
      { type: 'assistant', uuid: 'a1', parentUuid: 'u1',
        message: { role: 'assistant', stop_reason: 'end_turn',
          content: [{ type: 'text', text: 'course 这个词在这里是正常内容。' }] } },
    ]);
    repairSessionJsonl(jsonlPath);
    expect(textOf(readJsonlLines(jsonlPath).find((r) => r.uuid === 'a1'))).toBe('course 这个词在这里是正常内容。');
  });

  it('does NOT touch a fence word when a real tool_use block is present', () => {
    // 真 tool_use → 正常回合（前奏 + 真调用），bare-fence 不能命中。配套 tool_result
    // 让 tool_use 链闭合，避免触发既有的 unclosed-chain 截断。
    writeJsonl(jsonlPath, [
      userRow,
      { type: 'assistant', uuid: 'a1', parentUuid: 'u1',
        message: { role: 'assistant', stop_reason: 'tool_use',
          content: [
            { type: 'text', text: 'course 先看文件' },
            { type: 'tool_use', id: 't1', name: 'Read', input: { file_path: '/x' } },
          ] } },
      { type: 'user', uuid: 'u2', parentUuid: 'a1',
        message: { role: 'user', content: [{ type: 'tool_result', tool_use_id: 't1', content: 'ok' }] } },
    ]);
    repairSessionJsonl(jsonlPath);
    expect(textOf(readJsonlLines(jsonlPath).find((r) => r.uuid === 'a1'))).toBe('course 先看文件');
  });

  // --- fence 词紧贴 CJK 变体（2026-07-09，Amanda `course核`/Pulse `count关键点` 新形态）---
  // 段首 ASCII fence 词（course/core/court/care/count）直接粘一个中文字（无标点/空格边界），
  // 三个既有 bare-fence 判据全漏。逐条即命中；命中点取 fence 词起始 → 整行是降解计划残文切到块尾。
  it('trims a leaked "course核" prelude glued to CJK (Amanda 05:32 变体)', () => {
    writeJsonl(jsonlPath, [userRow, fenceRecord('先看当前状态。\n\ncourse核 ssot.md 剩余残留 + PROGRESS 是否已改。')]);
    repairSessionJsonl(jsonlPath);
    const t = textOf(readJsonlLines(jsonlPath).find((r) => r.uuid === 'a1'));
    expect(t).toBe('先看当前状态。');
    expect(t).not.toContain('course核');
  });

  it('trims leaked "count查" / "court这" glued-CJK preludes (全 c- 短词降解族)', () => {
    for (const [leaked, gone] of [
      ['count查 05:32 STOP 前的退出原因。', 'count查'],
      ['court这是"跑规则集",交 sub-agent。', 'court这'],
      ['core一下 610 规则怎么定输入源:', 'core一下'],
      ['care一点:图别画太密变教条。', 'care一点'],
    ] as const) {
      // 整条只有泄漏行 → 被删（无前置正文可留）。断言泄漏词不复存于该 session。
      writeJsonl(jsonlPath, [userRow, fenceRecord(leaked)]);
      repairSessionJsonl(jsonlPath);
      const row = readJsonlLines(jsonlPath).find((r) => r.uuid === 'a1');
      // 要么整条被删（找不到），要么 text 已不含泄漏词。
      const t = row ? textOf(row) : '';
      expect(t).not.toContain(gone);
    }
  });

  // 回归钉子：正常内容零误伤（本切片成败关键——宁窄勿误伤）。
  it('does NOT touch normal 课件/课程 (单字课/課 + CJK 不纳入本判据，防 课上/课前 误伤)', () => {
    // 注意：`课件` 命中的是**既有** BARE_FENCE_CJK_RE（课件/课题/課件/課題 整词），非本切片新判据；
    // 但 `课程` 不在既有整词表、也不该被新判据碰。本用例锁 `课程` 类正常词零误伤。
    const text = '课程大纲我看过了，没问题。下一步按计划推进。';
    writeJsonl(jsonlPath, [userRow, fenceRecord(text)]);
    repairSessionJsonl(jsonlPath);
    expect(textOf(readJsonlLines(jsonlPath).find((r) => r.uuid === 'a1'))).toBe(text);
  });

  it('does NOT touch normal English "Core thesis" / "of course" / course.md (无 CJK 紧贴)', () => {
    for (const text of [
      'Core thesis is that the pool leaks memory across resumes.',
      '这当然 of course 没问题，我继续。',
      '我已写入 course.md 并加索引。',
    ]) {
      writeJsonl(jsonlPath, [userRow, fenceRecord(text)]);
      repairSessionJsonl(jsonlPath);
      expect(textOf(readJsonlLines(jsonlPath).find((r) => r.uuid === 'a1'))).toBe(text);
    }
  });

  it('does NOT touch spaced "count 一下" dispatch outline (Duke 段首口头禅，本判据只认无空格紧贴)', () => {
    // 全机实测：`count 一下…` / `care 一点…` 是 Duke 等正常调度提纲（词后带空格），前一切片已判"正常"。
    // 本判据只匹配"fence 词无空格直接粘 CJK"，故空格形态天然不命中 → 不打断正常回合。
    for (const text of ['count 一下 commit 前自检清单。', 'care 一点:图别画太密变教条。', 'course 先查对端有没有未 push 的 commit。']) {
      writeJsonl(jsonlPath, [userRow, fenceRecord(text)]);
      repairSessionJsonl(jsonlPath);
      expect(textOf(readJsonlLines(jsonlPath).find((r) => r.uuid === 'a1'))).toBe(text);
    }
  });

  it('does NOT touch a glued fence word quoted inside a ``` code fence (讨论态引用非泄漏)', () => {
    // Pulse 主身在 ``` 块里引用 Amanda 的降解样本分析 bug —— 代码围栏内的 fence 词是引用不是泄漏。
    const text = '看她重启后的泄漏样本:\n```\ncourse核 ssot.md\ncourse修 D6 条\n```\n这就是既有判据漏掉的新形态。';
    writeJsonl(jsonlPath, [userRow, fenceRecord(text)]);
    repairSessionJsonl(jsonlPath);
    expect(textOf(readJsonlLines(jsonlPath).find((r) => r.uuid === 'a1'))).toBe(text);
  });
});
