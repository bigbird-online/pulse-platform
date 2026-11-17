import { describe, it, expect } from 'vitest';
import { stripOutboundToolcallLeak, scrubDegenerateLines } from '../src/outbound-leak-guard.js';

// Build the namespaced prefix at runtime so the source file itself doesn't carry
// the literal tag (keeps tooling/grep noise down; the regex is what matters).
const NS = 'ant' + 'ml:';

describe('stripOutboundToolcallLeak', () => {
  it('passes clean text through untouched', () => {
    const t = '好的，我已经把四个软链建好了，全部可解析。';
    const r = stripOutboundToolcallLeak(t);
    expect(r.leaked).toBe(false);
    expect(r.text).toBe(t);
    expect(r.removedChars).toBe(0);
  });

  it('does NOT trip on a bare mention of the literal marker (no full structure)', () => {
    // An agent explaining the bug, or docs — mentions the marker but pastes no
    // parameter/closing structure. Must not be treated as a leak.
    const t = '出站闸会检测文本里是否含 <invoke name= 这种工具调用泄漏签名。';
    const r = stripOutboundToolcallLeak(t);
    expect(r.leaked).toBe(false);
    expect(r.text).toBe(t);
  });

  it('strips a structurally-complete leak tail, keeping the prose before it', () => {
    const prose = '我先做安全核查再动手，确认 A/B 文件差异。';
    const leak =
      '\ncall\n<invoke name="Bash">\n' +
      '<parameter name="command">rm -f /home/pulse/Tenants/oikos/CLAUDE.md</parameter>\n' +
      '</invoke>';
    const r = stripOutboundToolcallLeak(prose + leak);
    expect(r.leaked).toBe(true);
    expect(r.text).toBe(prose);
    expect(r.removedChars).toBeGreaterThan(0);
    expect(r.text).not.toContain('<invoke');
  });

  it('eats the bare fence word (call/core) above the <invoke>', () => {
    const r = stripOutboundToolcallLeak(
      '正文\ncore\n<invoke name="Read"><parameter name="file_path">/x</parameter></invoke>',
    );
    expect(r.leaked).toBe(true);
    expect(r.text).toBe('正文');
    expect(r.text).not.toContain('core');
  });

  it('handles the namespaced invoke variant', () => {
    const r = stripOutboundToolcallLeak(
      `收尾\n<${NS}invoke name="Bash"><${NS}parameter name="command">ls</${NS}parameter></${NS}invoke>`,
    );
    expect(r.leaked).toBe(true);
    expect(r.text).toBe('收尾');
  });

  it('strips the real cross-session re-injection payload (89373f33 leak)', () => {
    // The actual leaked text 89373f33 sent to Feishu, which then got carried
    // back into a later session. This is the exact chain the guard must break.
    const t =
      '好,选项 1 的链接实现——把 A 整个变成指向 B 的软链。\n\n' +
      'call\n<invoke name="Bash">\n' +
      '<parameter name="command">rm -f /home/pulse/Tenants/oikos/oikos-registry.yaml</parameter>\n' +
      '<parameter name="description">删 A 的 oikos-registry.yaml 副本</parameter>\n' +
      '</invoke>';
    const r = stripOutboundToolcallLeak(t);
    expect(r.leaked).toBe(true);
    expect(r.text).toBe('好,选项 1 的链接实现——把 A 整个变成指向 B 的软链。');
    expect(r.text).not.toContain('rm -f');
  });

  it('treats empty / undefined input safely', () => {
    expect(stripOutboundToolcallLeak('').leaked).toBe(false);
    // @ts-expect-error runtime guard for nullish text
    expect(stripOutboundToolcallLeak(undefined).leaked).toBe(false);
  });
});

describe('scrubDegenerateLines — course of action 变体（2026-07-04 Duke 07d2d390）', () => {
  it('删除段首 "course of action: …" 整行，保留前面真正文', () => {
    for (const [input, expected] of [
      ['我先核当前真实文件状态,不凭记忆推。\n\ncourse of action: 核 640 三件套改到哪了。', '我先核当前真实文件状态,不凭记忆推。'],
      ['前段正常正文。\n\ncourse of action: 收口全事。', '前段正常正文。'],
    ] as const) {
      const r = scrubDegenerateLines(input);
      expect(r.removed).toBeGreaterThan(0);
      expect(r.text).toBe(expected);
    }
  });

  it('删除段首 "course <turn 号> …" 整行', () => {
    const r = scrubDegenerateLines('好，我整理一下。\ncourse 161 产物是 onboarding-review.md');
    expect(r.removed).toBeGreaterThan(0);
    expect(r.text).toBe('好，我整理一下。');
  });

  it('零误伤：句中 / 引号内 / 正常 course 用法不动', () => {
    for (const text of [
      '我认为正确的 course of action: 先修 bug 再重启。',   // 句中
      '这个短语 `course of action:` 是机器泄漏签名。',       // 反引号
      'the fixed phrase "course of action:" as a signature', // 英文引号
      'This course covers 5 topics.',                        // 正常 "course "
      'Of course. Let me proceed.',                          // of-course
    ]) {
      const r = scrubDegenerateLines(text);
      expect(r.removed).toBe(0);
      expect(r.text).toBe(text);
    }
  });
});

// ============================================================================
// scrubDegenerateLines — 好词豁免表版（2026-07-05, Duke 二裁 B'+E）
// 范式翻转：枚举坏词 → 枚举好词豁免表。全机 943 jsonl 复验：strict recall 98.6% /
// precision 100%（569 正常态零误伤）。下列用例锁定 B' 各分层判据。
// ============================================================================
describe('scrubDegenerateLines — 好词豁免 (B\' 收紧档=strict 默认)', () => {
  const NS2 = 'ant' + 'ml:';

  it('删旧判据漏掉的新 fence 变体 call/count/care（打地鼠→好词豁免的核心收益）', () => {
    // 旧枚举坏词表没有 call/count，全机各漏 107/27 条。好词豁免自动覆盖（非好词 → 删）。
    for (const [word] of [['call'], ['count'], ['care']] as const) {
      const input = `正文收尾。\n\n${word}\n<${NS2}invoke name="Bash"><${NS2}parameter name="command">ls</${NS2}parameter></${NS2}invoke>`;
      const r = scrubDegenerateLines(input);
      expect(r.removed).toBeGreaterThan(0);
      expect(r.text).not.toContain(`\n${word}\n`);
      expect(r.text).toContain('正文收尾');
    }
  });

  it('好词豁免表：Summary/Note/TODO/Context/Bug/Good 独占整行不删', () => {
    for (const good of ['Summary', 'Note', 'TODO', 'Context', 'Bug', 'Good', 'summary', 'note', 'done', 'OK']) {
      const input = `${good}\n后续正文内容在这里，是一段正常的说明。`;
      const r = scrubDegenerateLines(input);
      expect(r.removed).toBe(0);
      expect(r.text).toBe(input);
    }
  });

  it('好词豁免必须含 good（Duke B\' 第一版漏 good 致误伤，硬要求补齐）', () => {
    const r = scrubDegenerateLines('Good\n这是本轮的结论段落，一切正常。');
    expect(r.removed).toBe(0);
    expect(r.text).toContain('Good');
  });

  it('文件名排除：core.ts / course.jsonl / good.md 独占整行不删', () => {
    for (const fn of ['core.ts', 'course.jsonl', 'good.md', 'call.py', 'count.json']) {
      const input = `修改文件如下：\n${fn}\n然后重新构建。`;
      const r = scrubDegenerateLines(input);
      expect(r.removed).toBe(0);
      expect(r.text).toContain(fn);
    }
  });

  it('代码围栏 ``` 内的降解词不删（B\' 排除项）', () => {
    const input = '正文。\n\n```\ncourt\ncall\ncore\n```\n\n围栏外的正文。';
    const r = scrubDegenerateLines(input);
    expect(r.removed).toBe(0);
    expect(r.text).toContain('court');
    expect(r.text).toContain('```');
  });

  it('snake_case config key 不删（path_constraints / excluded_agents）', () => {
    const input = 'policy 字段：\npath_constraints\nexcluded_agents\n都要配。';
    const r = scrubDegenerateLines(input);
    expect(r.removed).toBe(0);
  });

  it('CJK 同音错字组独占整行删（課件/课件/课题/課題），与 ASCII 组分离', () => {
    const input = '好，我整理一下。\n\n課件\n\n真正的结论段落。';
    const r = scrubDegenerateLines(input);
    expect(r.removed).toBeGreaterThan(0);
    expect(r.text).not.toContain('課件');
    expect(r.text).toContain('真正的结论段落');
  });

  it('exotic 非常规脚本 fence 独占整行删（天城文等）', () => {
    const input = '正文段落。\n\nकाम\n\n结论段落。';
    const r = scrubDegenerateLines(input);
    expect(r.removed).toBeGreaterThan(0);
    expect(r.text).not.toContain('काम');
  });

  it('token 洪水删（court\\ncourt\\ncourt…），保留洪水前正文', () => {
    const input = '真正的回复内容。\ncourt\ncourt\ncourt\ncourt';
    const r = scrubDegenerateLines(input);
    expect(r.removed).toBeGreaterThan(0);
    expect(r.text).toContain('真正的回复内容');
    expect((r.text.match(/court/g) || []).length).toBe(0);
  });

  it('整条消息本身就是一个单词的合法短回复不删（防 streamed 类误删）', () => {
    // 单词消息、前面无正文、后面无 <invoke> → 不是 fence，保留。
    for (const word of ['streamed', 'processed', 'rendered']) {
      const r = scrubDegenerateLines(word);
      expect(r.removed).toBe(0);
      expect(r.text).toBe(word);
    }
  });

  it('后接正文的 Word: 标题不删（防 ICT: / Resolved: 类 prose 误伤，strict）', () => {
    for (const head of ['ICT:', 'Resolved:', 'Note:']) {
      const input = `${head}\n1. 第一条内容\n2. 第二条内容`;
      const r = scrubDegenerateLines(input);
      expect(r.removed).toBe(0);
      expect(r.text).toContain(head.replace(/:$/, ''));
    }
  });
});

describe('scrubDegenerateLines — 两 strictness 档差异（同一函数，共用真相源）', () => {
  it('收紧档（strict，缺省）: 段尾多 token 短行不删；放宽档删', () => {
    // `call spawn` = 两 token；非好词。strict 只删单 token → 保留；relaxed 删。
    // 用段尾形态（前有正文），两档差异体现在 token 数门槛。
    const input = '正文说明。\n\ncall spawn';
    const strictR = scrubDegenerateLines(input);          // 缺省 strict=true
    const relaxedR = scrubDegenerateLines(input, false);  // 放宽档
    expect(strictR.removed).toBe(0);
    expect(strictR.text).toContain('call spawn');
    expect(relaxedR.removed).toBeGreaterThan(0);
    expect(relaxedR.text).not.toContain('call spawn');
  });

  it('两档对最高置信 fence-above-<invoke> 一致开火（收紧不放过真泄漏）', () => {
    const input = `正文。\n\ncall\n<${NS}invoke name="Bash"><${NS}parameter name="command">ls</${NS}parameter></${NS}invoke>`;
    const strictR = scrubDegenerateLines(input);
    const relaxedR = scrubDegenerateLines(input, false);
    expect(strictR.removed).toBeGreaterThan(0);
    expect(relaxedR.removed).toBeGreaterThan(0);
    expect(strictR.text).not.toContain('\ncall\n');
    expect(relaxedR.text).not.toContain('\ncall\n');
  });
});

// 行首 call: 提纲泄漏（2026-07-08，大鸟点破 call: 与 court 洪水同源；全机 88 行实证多 agent 都在犯）。
// 判据：行首独占 `call:` / `call：`，本 block 内复读 ≥2 才删（单次偶发不动），句中反引号
// `call:` / 代码块内 call: 天然豁免。与探针 degen-probe.py 的 marker-flood 同源。
describe('scrubDegenerateLines — 行首 call: 提纲泄漏（marker-flood 同源）', () => {
  it('call: 复读 ≥2 删提纲行，保留穿插的正文', () => {
    const input = '正文分析一段。\n\ncall: 跑测试\n\n继续说明。\n\ncall: 提交代码';
    const r = scrubDegenerateLines(input);
    expect(r.removed).toBeGreaterThanOrEqual(2);
    expect(r.text).not.toMatch(/(^|\n)[ \t]*call[:：]/);
    expect(r.text).toContain('正文分析一段');
    expect(r.text).toContain('继续说明');
  });

  it('call: 单次不删（可能是偶发，非污染模式）', () => {
    const input = '正文一段。\n\ncall: 跑测试\n\n正文继续。';
    const r = scrubDegenerateLines(input);
    expect(r.removed).toBe(0);
    expect(r.text).toContain('call: 跑测试');
  });

  it('全角 call：也算复读计数', () => {
    const input = '开头。\n\ncall： 甲\n\ncall: 乙';
    const r = scrubDegenerateLines(input);
    expect(r.removed).toBeGreaterThanOrEqual(2);
    expect(r.text).not.toMatch(/(^|\n)[ \t]*call[:：]/);
  });

  it('句中反引号 `call:` 讨论不误伤（行首独占才判）', () => {
    const input = '要抓 `call:` 但不误伤，`call:` 在句中是正常讨论，再提一次 `call:`。';
    const r = scrubDegenerateLines(input);
    expect(r.removed).toBe(0);
    expect(r.text).toBe(input);
  });

  it('代码块 ``` 内 call: 复读不删（fence 内豁免）', () => {
    const input = '示例：\n```\ncall: x\ncall: y\ncall: z\n```\n正文结尾。';
    const r = scrubDegenerateLines(input);
    expect(r.removed).toBe(0);
    expect(r.text).toContain('call: x');
  });

  it('call: 与 court 洪水混合，两者都清', () => {
    const input = '开头正文。\ncall: A\ncourt\ncourt\ncourt\ncall: B';
    const r = scrubDegenerateLines(input);
    expect(r.text).not.toMatch(/(^|\n)[ \t]*call[:：]/);
    expect(r.text).not.toMatch(/(^|\n)[ \t]*court\b/);
    expect(r.text).toContain('开头正文');
  });
});
