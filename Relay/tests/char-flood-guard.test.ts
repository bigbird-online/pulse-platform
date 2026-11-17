// 字符洪水（char-flood）判据单测 —— 2026-07-26 `課`×3.2 万事故补的第三条判据。
//
// 事故形态：assistant 回复里 `課` **单字**独占一行、空行分隔，复读 3.2 万次，共五发。
// 三道防线全漏，根因都是"词形/词长"假设：
//   · pool.ts DEGEN_SOLO_WORD_LINE_RE = /^[ \t]*([A-Za-z]{2,12})…/ → ASCII-only 且 ≥2 字符；
//   · degen-probe.py TOKEN_FLOOD_RE 的 `{2,12}` 同样够不着单字；
//   · repair-toolcall-leak.py NOISE_PREFIX_CAND_RE 要"词 + 冒号"，裸字符无冒号。
// 本判据换掉这个假设：真不变量是**同一孤立 token 连续独占多行**（游程 run length），
// 与降解成什么字、字有多长无关。
//
// 阈值 3 的实测依据（全机 980 jsonl / 39204 条 assistant text block，2026-07-26）：
//   游程 ≥2 仅 8 条（全是代码块里的 `}` / ``` / `1`）；游程 ≥3 仅 6 条 =
//   Hestia 07-04 真中毒 court×6285 + 本次五发 課×25358~31979。误报 0。
//
// 注：本文件 import **src**（非 dist）。dist-prefix-flood-guard.test.ts 走 dist 是为了验证
// 生产真正加载的编译产物；本轮改动尚未 build（主身复核后统一 build + 重启），故此处验 src。
import { describe, it, expect } from 'vitest';
import { maxSoloLineRun, detectCharFlood, stripCharFloodRuns, findCjkSoloLines, DEGEN_CHAR_FLOOD_RUN_THRESHOLD } from '../src/pool.js';

const asst = (text: string, stopReason: string | null = 'end_turn') => ({
  type: 'assistant',
  message: { stop_reason: stopReason, content: [{ type: 'text', text }] },
});

describe('char-flood 游程判据（单字 / CJK / 低次数）', () => {
  it('单字 CJK 独占行复读（空行分隔）→ 命中，这是本次事故的真实形态', () => {
    const { run, token } = maxSoloLineRun('課\n\n課\n\n課\n\n課\n\n課');
    expect(run).toBe(5);
    expect(token).toBe('課');
    expect(detectCharFlood(asst('課\n\n課\n\n課')).flooded).toBe(true);
  });

  it('回归钉子：旧 DEGEN_SOLO_WORD_LINE_RE 的 [A-Za-z]{2,12} 够不着单字 CJK', () => {
    // 直接钉住"为什么当初漏"：单字既非 ASCII 也不足 2 字符。
    expect(/^[ \t]*([A-Za-z]{2,12})[ \t.:：．]*$/.test('課')).toBe(false);
    // 新判据接住。
    expect(detectCharFlood(asst('課\n課\n課')).flooded).toBe(true);
  });

  it('10 次复读的低次数场景就抓到（不必等撑爆 token 上限）', () => {
    for (const tok of ['課', 'course', 'court', 'card']) {
      const text = Array(10).fill(tok).join('\n\n');
      expect(detectCharFlood(asst(text)).flooded, tok).toBe(true);
    }
  });

  it('阈值边界：游程 3 命中、游程 2 不命中', () => {
    // 2026-08-07 改用 ASCII 多字 token 测游程语义本身：原用例是 `課`，现被并联的
    // CJK 单字判据兜住（正是本次 run=2 逃逸事故的修复），已测不出游程阈值边界。
    // 两条判据分工：游程管多字 token 复读，CJK 单字判据管单字 flood。
    expect(DEGEN_CHAR_FLOOD_RUN_THRESHOLD).toBe(3);
    expect(detectCharFlood(asst('court\ncourt\ncourt')).flooded).toBe(true);
    expect(detectCharFlood(asst('court\ncourt')).flooded).toBe(false);
    // 游程判据对 CJK 单字的真实读数仍是 2（漏的是它，由并联判据补）。
    expect(maxSoloLineRun('課\n課').run).toBe(2);
  });

  it('stop_reason=null 也命中——事故第五发既非 max_tokens 也非 end_turn', () => {
    expect(detectCharFlood(asst('課\n\n課\n\n課', null)).flooded).toBe(true);
  });

  it('非独占行使游程归零，不跨正文累加', () => {
    // 同上，改用 ASCII 多字 token 单测游程语义。
    expect(maxSoloLineRun('court\n正文一\ncourt\n正文二\ncourt').run).toBe(1);
    expect(detectCharFlood(asst('court\n正文一\ncourt\n正文二\ncourt')).flooded).toBe(false);
    // 注：同形态换成 CJK 单字**会**命中——单字散落正文间正是 `古`/`据` 那种单发退化，
    // 不是误报。见下方「CJK 单字独占正文行」describe。
    expect(detectCharFlood(asst('課\n正文一\n課\n正文二\n課')).flooded).toBe(true);
  });

  it('Hestia 2026-07-04 真中毒形态（court 独占行洪水）仍命中', () => {
    expect(detectCharFlood(asst('court\n\n'.repeat(20))).flooded).toBe(true);
  });
});

describe('char-flood 误报防线（历史误伤 108→0 不得回退）', () => {
  it('代码块里的 `}`×2 / ``` 不误命中（全机实测它们只到游程 2）', () => {
    const code = '修复如下：\n\n```ts\nfunction f() {\n  if (x) {\n    return 1;\n  }\n}\n```\n\n收尾说明。';
    expect(detectCharFlood(asst(code)).flooded).toBe(false);
    expect(detectCharFlood(asst('结果：\n}\n}\n然后继续正文。')).flooded).toBe(false);
  });

  it('既有停用词 ok/done/PONG/Sources 独占行不误命中', () => {
    for (const txt of [
      'ok\n\n已处理完毕。\n\ndone',
      'PONG',
      'Sources:\n\n- a.md\n- b.md',
      'done\n\n第一段正文。\n\ndone\n\n第二段正文。\n\ndone',
    ]) {
      expect(detectCharFlood(asst(txt)).flooded, txt.slice(0, 12)).toBe(false);
    }
  });

  it('正常中英文回复不误命中', () => {
    const txt = '这是一段正常回复，讨论 course of study 与 core 模块的设计。\n\n结论：可行。';
    expect(detectCharFlood(asst(txt)).flooded).toBe(false);
  });

  it('非 assistant record / 无 text block → 不命中', () => {
    expect(detectCharFlood({ type: 'user', message: { content: [{ type: 'text', text: '課\n課\n課' }] } }).flooded).toBe(false);
    expect(detectCharFlood({ type: 'assistant', message: { content: 'not-an-array' } }).flooded).toBe(false);
  });
});

describe('stripCharFloodRuns 清洗语义（保正文、切洪水）', () => {
  it('正常正文 + 尾部 flood → 截掉 flood 段、保留正文', () => {
    const txt = '这是正常的分析结论，共三点。\n\n第二段正文继续。\n\n' + '課\n\n'.repeat(20);
    const { text, removed } = stripCharFloodRuns(txt);
    expect(removed).toBe(20);
    expect(text).toContain('这是正常的分析结论');
    expect(text).toContain('第二段正文继续');
    expect(text).not.toContain('課');
  });

  it('整条几乎全是复读 → 剥完为空，交调用方整条丢弃', () => {
    const { text, removed } = stripCharFloodRuns('課\n\n'.repeat(500));
    expect(removed).toBe(500);
    expect(text.trim()).toBe('');
  });

  it('游程 1-2 的独占行原样保留（代码块 `}` 不被删）', () => {
    const code = '说明：\n\n```ts\nif (a) {\n}\n```\n\n收尾。';
    const { text, removed } = stripCharFloodRuns(code);
    expect(removed).toBe(0);
    expect(text).toBe(code);
  });

  it('代码围栏内即便 3 连独占行也不删（Duke B\' 排除项）', () => {
    const txt = '样本如下：\n\n```\n課\n課\n課\n課\n```\n\n以上是分析样本。';
    const { removed, text } = stripCharFloodRuns(txt);
    expect(removed).toBe(0);
    expect(text).toContain('以上是分析样本');
  });

  it('不得吃掉正常散文的 --- 分隔线 + 短小标题（全机 226 条那族，块长只到 2）', () => {
    const txt = '**请回我:**\n\n---\n\n下面是正文段落，内容较长不属于独占行。';
    const { text, removed } = stripCharFloodRuns(txt);
    expect(removed).toBe(0);
    expect(text).toBe(txt);
  });

  it('洪水穿插在正文中间 → 只删洪水段，前后短正文行都留（回归钉子）', () => {
    // 2026-07-26 实跑撞到：短正文行 ≤12 字符本身也算"独占行"，放宽判据会把它
    // 吸进洪水块一起删。严格同 token 游程天然不会。
    const txt = '前面的正文。\n\n' + '課\n\n'.repeat(10) + '后面的正文。';
    const { text, removed } = stripCharFloodRuns(txt);
    expect(removed).toBe(10);
    expect(text).toContain('前面的正文');
    expect(text).toContain('后面的正文');
    expect(text).not.toContain('課');
  });

  it('近形字纯交替：游程侧仍清不掉（结构使然），已由 CJK 单字判据接管', () => {
    // 0726 把这条钉为"已知残余（刻意不修）"，2026-08-07 它真的发作了——
    // 22 轮 run=2 逃逸，三层防线零响应。故此处只保留"游程侧确实够不着"的事实钉子，
    // 断根不走近形字归一化（0726 实测吞正文/扫不净来回摆动），改走"CJK 单字独占
    // 正文行"这条不依赖字表的不变量；实际拦截见下方 describe 与出站闸用例。
    expect(stripCharFloodRuns('課\n\n课\n\n課\n\n课').removed).toBe(0);
    const { text, removed } = stripCharFloodRuns('正文保留。\n\n' + '課\n\n'.repeat(20) + '课\n\n課');
    expect(removed).toBe(20);
    expect(text).toContain('正文保留');
    // 但检测侧现在抓得到（不再静默放过）。
    expect(detectCharFlood(asst('課\n\n课\n\n課\n\n课')).flooded).toBe(true);
  });
});

// ── 2026-08-07 补：CJK 单字独占正文行判据（run=2 逃逸事故）────────────────────
// 事故：飞书主会话 b7a31a9c 连续 22 轮吐 `\n\n課\n\n課 \n`，每轮固定两个、空行分隔，
// **游程恒为 2，稳稳卡在阈值 3 下一格**。三层防线（spawn 前 repair / 在线 turn 闸 /
// 飞书出站闸）同源同阈值 → 一起失明，全程零告警，是大鸟肉眼发现的。
// 上面那条"已知残余"测试预言过这个形态（課/课交替打断游程），当时切出范围，今天发作。
// 教训：三层冗余锚在同一个常数上，等于一层。
// 治法不再调阈值/补繁简表（0726 实测两条路都在"吞正文/扫不净"间摆动），换不变量：
//   **正常中文写作不把单个汉字单独成行。**
// 全机实测 2999 文件 / 64844 block：孤立 CJK 单字 100 个，94 个真中毒；
// `或`×4 是代码围栏间连接词（唯一合法用法，靠围栏豁免排除）。误报 0。
describe('CJK 单字独占正文行（游程判据的结构盲区）', () => {
  it('run=2 的繁简交替 flood → 命中（旧游程判据漏，这是本次事故真实形态）', () => {
    const real = '真因浮出：对端没有 pulse 的飞书客户端。\n\n課\n\n課 \n先确认。';
    // 游程判据本身仍只有 2，够不着阈值 3 —— 证明漏的是它。
    expect(maxSoloLineRun(real).run).toBe(2);
    expect(maxSoloLineRun(real).run).toBeLessThan(DEGEN_CHAR_FLOOD_RUN_THRESHOLD);
    // 新判据兜住。
    expect(findCjkSoloLines(real)).toEqual(['課', '課']);
    expect(detectCharFlood(asst(real)).flooded).toBe(true);
  });

  it('繁简纯交替（0726 钉为"已知残余"的形态）→ 现已命中', () => {
    expect(findCjkSoloLines('課\n\n课\n\n課\n\n课')).toEqual(['課', '课', '課', '课']);
    expect(detectCharFlood(asst('課\n\n课\n\n課\n\n课')).flooded).toBe(true);
  });

  it('单发单字（古/据，段末孤字）→ 命中，不需要复读', () => {
    expect(detectCharFlood(asst('我却去要 API key。直接让 codex 自己生图。\n\n古')).flooded).toBe(true);
    expect(findCjkSoloLines('这条我记忆里有。\n\n据')).toEqual(['据']);
  });

  it('代码围栏之间的连接词 `或` → 豁免（唯一合法的单字独占行）', () => {
    const legit = '**确认分区在：**\n```\nlsblk\n```\n或\n```\ndf -h\n```';
    expect(findCjkSoloLines(legit)).toEqual([]);
    expect(detectCharFlood(asst(legit)).flooded).toBe(false);
  });

  it('围栏内的单字 / 缩进代码里的单字 → 不算（不是正文）', () => {
    expect(findCjkSoloLines('```\n課\n課\n```')).toEqual([]);
    expect(findCjkSoloLines('示例：\n\n    課\n    課')).toEqual([]);
  });

  it('正常中文正文不命中（多字短行、标题、列表都不是单字独占行）', () => {
    const prose = '已完成。\n\n三端同步。\n\n**结论**\n\n- 一条\n- 两条\n\n好的\n\n收到';
    expect(findCjkSoloLines(prose)).toEqual([]);
    expect(detectCharFlood(asst(prose)).flooded).toBe(false);
  });

  it('出站闸能真删掉 run=2 的单字 flood（清洗侧同源）', async () => {
    const { scrubDegenerateLines } = await import('../src/outbound-leak-guard.js');
    const { text, removed } = scrubDegenerateLines('真因浮出。\n\n課\n\n課 \n先确认。');
    expect(removed).toBe(2);
    expect(text).not.toContain('課');
    expect(text).toContain('真因浮出');
    expect(text).toContain('先确认');
  });

  it('出站闸保留围栏间连接词（不误删正常内容）', async () => {
    const { scrubDegenerateLines } = await import('../src/outbound-leak-guard.js');
    const legit = '确认：\n```\nlsblk\n```\n或\n```\ndf -h\n```';
    expect(scrubDegenerateLines(legit).removed).toBe(0);
  });
});
