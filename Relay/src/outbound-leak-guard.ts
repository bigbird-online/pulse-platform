/**
 * Outbound tool-call-leak guard (2026-06-28).
 *
 * 背景（彻查链路见 session cbb8cc40 调查 memo）：CC 偶发把工具调用写成纯文本
 * `<invoke name="...">...</invoke>` 而非结构化 tool_use block（即 toolcall-leak
 * 家族，见 pool.ts detectToolcallLeak）。pool.ts 已在 **spawn 前** 从 jsonl 尾部
 * 截掉泄漏，治"中毒 session 被 few-shot 复学"那一维。
 *
 * 但泄漏文本若先一步**发到了飞书卡片**，它就沉淀进共享的飞书会话里，随后被用户
 * 的下一条消息（引用/复制）原样带回，喂给**另一个** session — 这就是 2026-06-28
 * 观测到的"中毒产物跨 session 回灌"。pool.ts 的入站守卫拦不住这种看似正常的
 * msgType=text user 消息。唯一能斩断整条链的点是 **出站**：不让泄漏文本发出门。
 *
 * 本模块提供纯文本级（非 jsonl-record 级）的泄漏检测 + 截断，作用在 CC assistant
 * 已渲染好的卡片文本上。正则签名与 pool.ts TOOLCALL_LEAK_* 同源（保持判据一致），
 * 但这里只看文本本身、不依赖 stop_reason / tool_use block（出站时已无 record 结构）。
 *
 * 策略与 pool.ts de-poison 一致：**截断而非整条丢弃** —— 保留泄漏起点之前的正文
 * （那部分通常是有效回复），只切掉从 `<invoke name=` 开始的泄漏尾巴。
 */

// 与 pool.ts:327/331/337 同源（改一处两处都要改 —— 见 CLAUDE.md 同源近邻双扫纪律）。
// 导出供 inbound-leak-guard.ts 复用，保持单一真相源（出站截断 + 入站布尔检测共享同一组签名）。
export const LEAK_TEXT_RE = /<(?:antml:)?invoke name=/;
// 泄漏起点 fence：紧贴 <invoke 上方那一行若是裸标记词（core / call / course 等
// ≤3 个 ASCII 短词、无 CJK、无句子标点），连同一并切掉，让正文收尾干净。
const LEAK_FENCE_RE = /(?:^|\n)[ \t]*[a-z][a-z0-9_]*(?:[ _-][a-z0-9_]+){0,2}[ \t]*\n+<(?:antml:)?invoke name=/i;
// 回退：没有 fence 前缀时直接从 <invoke 截断。
const LEAK_BARE_RE = /\n*<(?:antml:)?invoke name=/;
// 结构完整性约束：真泄漏的 <invoke name="X"> 后几乎总紧跟 <parameter name= 或
// </invoke>（模型完整粘贴工具调用结构）；纯讨论提及（如本注释、文档里讲语法）
// 通常不会粘完整 parameter 块。要求 invoke 后 400 字符内出现其一，降低误伤。
export const LEAK_STRUCT_RE = /<(?:antml:)?invoke name=[^]{0,400}?(?:<(?:antml:)?parameter name=|<\/(?:antml:)?invoke>)/;

// ============================================================================
// 逐行降解签名 —— 好词豁免表版（2026-07-05，Duke 二裁 B'+E 落地）
// ----------------------------------------------------------------------------
// 单一真相源：pool.ts 的 spawn-前 repair 与本文件的飞书出站闸共用 scrubDegenerateLines。
//
// 背景与范式翻转：Opus 长上下文解码退化会把工具前奏词吐成散落的整行降解 token
// （court/course/core/call/count/care/課題/exotic 脚本），穿插在正文段首。旧判据
// **逐词枚举坏词**（`course|core|court|care|…`），是打地鼠——坏词无界，新变体（care
// 之后还会有别的 c- 开头短词）永远追不上。全机 943 jsonl 复验坐实：旧判据漏 `call`
// 107 条、`call:` 83 条、`count` 27 条（recall 95.7%）。
//
// Duke 二裁 B'：**翻转成枚举好词豁免表**。好词（Summary/Note/TODO/Context/Bug/Good…）
// 有限低频可枚举；坏词无界。判据 = 行首独占短 token + 终端边界 + **非好词** + 非文件名
// + 非代码围栏。全机复验（下）：新判据 recall 98.9% / precision 100%（569 条正常态零误伤）。
//
// 两档 strictness（同一函数，共用真相源）：
//   - strict=true（实时出站闸，飞书 send 前）：**收紧档**——只在最高置信形态开火：
//       单个小写字母短 token（≤8）且满足〔正下方是 <invoke>〕或〔token 洪水成员〕或
//       〔本 block 最后一行非空〕。`Word:` 之类后接正文的标题不删（防 ICT:/Resolved: 误伤）。
//   - strict=false（spawn-前扫盘）：**放宽档**——非好词的裸 token 行（≤3 短 token）都删。
//       安全因为 spawn 扫盘只在 detectBareFenceLeak 已判降解的 record 上跑。
//
// 保留项（Duke 规则 6/7）：exotic 非常规脚本 fence、token 洪水、CJK 同音错字组、
//   course-of-action/turn# 短语——形态判据样板，行为不变。
// 排除项（B' 第一版实测缺陷，已补）：好词表（含 good）、文件名后缀、代码围栏 ``` 内、
//   snake_case config key（path_constraints 等）、CJK 单独成组（不混进 ASCII 组）。
// 复验脚本 + 完整数据见 wrap-log / memo（2026-07-05 B' 落地）。
// ============================================================================

// —— 好词豁免表 —— 正常段首会独占出现的短词，从全机 943 jsonl 正常文本归纳。
// 匹配规则：整行剥尾部标点后小写，命中即豁免（永不删）。Duke B' 硬约束：必须含 good。
export const DEGEN_GOOD_WORDS = new Set<string>([
  // 章节/标签头（正常 prose 独占成行的高频词）
  'summary', 'note', 'notes', 'todo', 'todos', 'context', 'bug', 'bugs', 'good',
  'done', 'ok', 'okay', 'yes', 'no', 'result', 'results', 'output', 'input',
  'status', 'update', 'updates', 'plan', 'plans', 'goal', 'goals', 'observation',
  'observations', 'evidence', 'verdict', 'reasoning', 'sources', 'end', 'card', 'try',
  'commit', 'commits', 'write', 'push', 'build', 'continue', 'currently', 'given',
  'pass', 'passed', 'typecheck', 'escalation', 'draft', 'body', 'paths', 'handoff',
  'telegram', 'feishu', 'dimensions', 'metadata', 'pong', 'alpha', 'beta',
  'key', 'facts', 'findings', 'answer', 'question', 'questions', 'analysis',
  'approach', 'background', 'cause', 'change', 'changes', 'check', 'conclusion',
  'detail', 'details', 'diff', 'issue', 'issues', 'log', 'logs', 'option', 'options',
  'problem', 'problems', 'reason', 'reasons', 'risk', 'risks', 'root', 'scope',
  'solution', 'step', 'steps', 'task', 'tasks', 'verify', 'verification', 'wrap',
  'test', 'tests', 'testing', 'fix', 'fixes', 'fixed', 'error', 'errors',
  'warning', 'warnings', 'warn', 'info', 'debug', 'example', 'examples', 'tip', 'tips',
  'now', 'next', 'first', 'then', 'finally', 'also', 'but', 'and', 'so', 'thus',
  'however', 'otherwise', 'therefore', 'meanwhile', 'why', 'what', 'how', 'when',
  'where', 'who', 'before', 'after', 'title', 'name', 'value', 'type', 'usage',
  'command', 'commands', 'file', 'files', 'path', 'edit', 'edits',
  'branch', 'ci', 'pr', 'mvs', 'caveat', 'caveats', 'trace',
  'fail', 'failed', 'skip', 'skipped', 'true', 'false', 'null', 'none', 'all',
  'both', 'either', 'neither', 'yep', 'nope', 'decision', 'decisions', 'reeve',
  'channels',
]);

// 排除：ASCII 短 token 其实是文件名（core.ts / course.jsonl / good.md 等）。任意 stem。
const DEGEN_FILENAME_RE = /^[a-z0-9_.-]+\.(md|jsonl|ts|js|py|json|yaml|yml|txt|sh|toml|cfg|ini|lock|log|csv|tsx|jsx|mjs|cjs|go|rs|rb|java|c|h|cpp|html|css|xml|sql)\b/i;

// —— 保留的形态判据（Duke 规则 6/7，行为不变）——
// exotic 非常规脚本 fence：整行 ≥2 个天城文/西里尔/阿拉伯/泰文/亚美尼亚/格鲁吉亚字符。
const DEGEN_EXOTIC_LINE_RE = /^[ \t]*[ऀ-ॿЀ-ӿ؀-ۿ฀-๿԰-֏Ⴀ-ჿ]{2,}[ \t.:：．]*$/;
// CJK 同音错字组（課件/课件/课题/課題，kèjiàn/kètí）——闭合集，独占整行（≤20 尾随字符）。
// CJK 是**有界闭合集**（同音错字就这几个），故保留枚举；不与 ASCII 开放集混用。
const DEGEN_CJK_LINE_RE = /^[ \t]*(課件|课件|课题|課題)[^\n]{0,20}[ \t.:：．]*$/;
// CJK **单字**独占整行（2026-08-07 `課` run=2 逃逸事故补）。
// 上面那条是二字闭合集，裸单字 `課` 判 false；pool.ts 的游程判据又因繁简交替(課/课)恒
// run=2 卡在阈值 3 下 → 检测与清洗两侧同时够不着,三层防线全程零响应。
// 换不依赖游程/字表的不变量：**正常中文写作不把单个汉字单独成行**。
// 全机实测(2999 文件/64844 block)：孤立 CJK 单字共 100 个,94 个是真中毒(課/课),
// `古`/`据` 各 1 亦为同族单发退化,`或` 4 个是代码围栏间连接词(```\n或\n``` )——
// 后者由 isDegenLine 的围栏上下文豁免排除,不靠本正则。误报 0。
const DEGEN_CJK_SOLO_CHAR_RE = /^[ \t]*[一-鿿][ \t.:：．]*$/;
// course-of-action / course<turn#> 机器泄漏计划前奏（course 后接 " of action:" 或 turn 号）。
const DEGEN_COURSE_PHRASE_RE = /^[ \t]*course(?:\s+of\s+action[:：]|\s+\d)[^\n]*$/i;
// 行首 call: 调度提纲泄漏（2026-07-08，大鸟点破 call: 与 court 洪水同源 + 全机 88 行实证：
//   duke/pulse 等多 agent 都在犯，且全是「call: 提纲文字」泄漏、无一是正常英文句子）。
//   行首独占 `call:` / `call：`（后接空格+提纲），句中 `` `call:` `` 反引号讨论天然不匹配。
//   单次可能偶发 → 靠 scrub 预扫「复读 ≥2 次」才删（与探针 marker-flood 同源、与 token 洪水
//   同结构）；这条判据必须能与真 tool_use 共存时仍触发（call: 提纲在 text、真调用在 tool_use）。
const DEGEN_CALL_LEAK_RE = /^[ \t]*call[:：]\s*\S/i;
// 裸 ASCII 短 token 行形态：1~3 个短 token 后接终端边界。这是好词表所门禁的**开放集**。
const DEGEN_BARE_ASCII_RE = /^[ \t]*([a-z][a-z0-9_]{0,14})(?:[ _-][a-z0-9_]{1,14}){0,2}[ \t.:：．]*$/i;
const DEGEN_INVOKE_RE = /<(?:antml:)?invoke name=/;

// 兼容保留：旧 DEGEN_LINE_RE 导出（无外部引用，但保留以防下游/测试引用）。
// 新逻辑不依赖它；它仅表达"独占整行的已知坏词"这个子集。
export const DEGEN_LINE_RE = new RegExp(
  '^[ \\t]*('
  + '(?:course|core|court|call|count|care|core_directive_acknowledged)\\.?'
  + '|(?:course(?:\\s+of\\s+action[:：]|\\s+\\d)[^\\n]*)'
  + '|(?:課件|课件|课题|課題)[^\\n]{0,20}'
  + '|[\\u0900-\\u097F\\u0400-\\u04FF\\u0600-\\u06FF\\u0E00-\\u0E7F\\u0530-\\u058F\\u10A0-\\u10FF]{2,}[^\\n]{0,20}'
  + ')[ \\t.:：．]*$',
  'iu',
);

/** 整行剥尾部标点 → 小写。用于好词表/文件名匹配。 */
function bareLineKey(line: string): string {
  return line.trim().replace(/[ \t.:：．]+$/, '').toLowerCase();
}

/** 该裸 token 行是否属好词 / 文件名 / snake_case config key → 豁免（永不删）。 */
function isGoodOrExcluded(line: string): boolean {
  const key = bareLineKey(line);
  if (DEGEN_FILENAME_RE.test(line.trim())) return true;
  if (DEGEN_GOOD_WORDS.has(key)) return true;
  const tokens = key.split(/[ _-]+/).filter(Boolean);
  if (tokens.length > 1 && tokens.every((t) => DEGEN_GOOD_WORDS.has(t))) return true;
  // snake_case config 标识符（path_constraints / excluded_agents…）——绝非降解。
  if (/^[a-z][a-z0-9]*(_[a-z0-9]+)+$/.test(key)) return true;
  return false;
}

function nextNonEmpty(lines: string[], i: number): string {
  for (let j = i + 1; j < lines.length; j++) { if (lines[j]!.trim()) return lines[j]!; }
  return '';
}
function prevNonEmpty(lines: string[], i: number): string {
  for (let j = i - 1; j >= 0; j--) { if (lines[j]!.trim()) return lines[j]!; }
  return '';
}
/** 该行是否是代码围栏起止行（``` / ~~~）。用于 CJK 单字连接词豁免。 */
function isFenceLine(line: string): boolean {
  return /^[ \t]*(?:```|~~~)/.test(line);
}
function isLastNonEmpty(lines: string[], i: number): boolean {
  for (let j = i + 1; j < lines.length; j++) { if (lines[j]!.trim()) return false; }
  return true;
}
function hasPrecedingNonEmpty(lines: string[], i: number): boolean {
  for (let j = i - 1; j >= 0; j--) { if (lines[j]!.trim()) return true; }
  return false;
}

/**
 * 行级判据（好词豁免）：给定一行 + 其在 block 中的上下文，判断该行是否降解 fence 需删。
 * @param strict true=实时出站闸（收紧档）；false=spawn 扫盘（放宽档）。
 * @param floodSet 本 block 内出现 ≥3 次的裸 token（洪水成员），null 表示无洪水信息。
 */
function isDegenLine(
  line: string, lines: string[], i: number, strict: boolean, floodSet: Set<string> | null,
  callFlood = false,
): boolean {
  // 保留的形态判据（两档都开火，形态安全）。
  if (DEGEN_EXOTIC_LINE_RE.test(line)) return true;
  if (DEGEN_CJK_LINE_RE.test(line)) return true;
  // CJK 单字独占行：唯一合法用法是代码围栏之间的连接词（```\n或\n``` ），故前后
  // 紧邻围栏时豁免；其余一律判降解。围栏**内**的行由 scrubDegenerateLines 外层跳过。
  if (DEGEN_CJK_SOLO_CHAR_RE.test(line)
      && !(isFenceLine(prevNonEmpty(lines, i)) && isFenceLine(nextNonEmpty(lines, i)))) {
    return true;
  }
  if (DEGEN_COURSE_PHRASE_RE.test(line)) return true;
  // 行首 call: 提纲泄漏：仅当本 block 内 call: 复读 ≥2（callFlood）才删——单次偶发不动，
  // 与 token 洪水/marker-flood 同源。行首独占天然排除句中 `` `call:` `` 反引号讨论。
  if (callFlood && DEGEN_CALL_LEAK_RE.test(line)) return true;

  // 裸 ASCII 短 token 行 → 好词豁免门禁。
  if (!DEGEN_BARE_ASCII_RE.test(line)) return false;
  if (isGoodOrExcluded(line)) return false;

  const key = bareLineKey(line);
  const tokens = key.split(/[ _-]+/).filter(Boolean);
  const singleAlpha = tokens.length === 1 && /^[a-z]+$/.test(tokens[0]!) && tokens[0]!.length >= 2 && tokens[0]!.length <= 12;

  // 最高置信降解上下文（两档都开火）：
  //   (a) 裸 token 正下方是泄漏的 <invoke>（fence-above-leak），或
  //   (b) 裸 token 是 token 洪水成员（court\ncourt\n…）。
  const fenceAboveLeak = DEGEN_INVOKE_RE.test(nextNonEmpty(lines, i));
  const isFlood = !!floodSet && floodSet.has(key);
  if (singleAlpha && (fenceAboveLeak || isFlood)) return true;

  if (strict) {
    // 收紧档：只删单个小写字母短 token（≤8），且满足〔正下方 <invoke>〕（上面已处理）
    // 或〔洪水〕（上面已处理）或〔fence 吐出但 tool block 没跟上：本 block 最后一行非空
    // **且前面有正文**〕。要求"前面有正文"是防整条消息本身就是一个单词（`streamed` 之类
    // 合法短回复）被误删——真 fence 尾巴总是跟在正文后面（`正文\n\ncall:`）。
    // 后接正文的 `Word:` 标题不删（防 ICT:/Resolved: 类 prose 误伤）。
    if (!singleAlpha) return false;
    if (tokens[0]!.length > 8) return false;
    if (isLastNonEmpty(lines, i) && hasPrecedingNonEmpty(lines, i)) return true;
    return false;
  }
  // 放宽档（spawn 扫盘）：非好词的裸 token 行（≤3 短 token，每 token ≤14）都删。
  if (tokens.some((t) => t.length > 14)) return false;
  return true;
}

// 段首前缀降解（2026-07-04，出站实证形态）：飞书渲染会把 `court\n\n正文` 的 `\n\n` 吃掉，
// 变成 `court正文` 粘连——court/course/core/care 作为**段落开头前缀**紧贴下段正文（大鸟看到的
// courtdigest / courtan_confirm / court§16 / course：ack / courseはまだ 等）。判据三条同时满足：
//   ① 段首是 court/course/core/care 且后面【无空格】直接紧跟（\S）——正常英中混写有空格分隔。
//   ② 段内前 60 字符含 CJK / § / **（markdown）——中文工作环境里紧贴中文必是降解。
//   ③ 排除正常英文词：其后 ≥4 连续英文字母且前 15 字符内无 CJK/§ → 保守不删。
// 前缀词仍枚举 c- 家族：这是**已确认粘连的降解词**（court/course/core/care），非开放判据；
// 好词表不管前缀粘连（前缀总是已知 c- 降解词），两者正交。
const DEGEN_PREFIX_LEAD_RE = /^(court|course|core|care)(\S)/i;
function stripDegeneratePrefix(seg: string): { seg: string; removed: boolean } {
  const m = DEGEN_PREFIX_LEAD_RE.exec(seg);
  if (!m) return { seg, removed: false }; // ① 后面是空格 → 正常混写，不动
  if (!/[一-鿿§]|\*\*/.test(seg.slice(0, 60))) return { seg, removed: false }; // ② 段内无 CJK/§/** → 不动
  const after = seg.slice(m[1]!.length);
  // ③ c-词后 ≥4 连续英文字母 且 前 15 字符内无 CJK/§ → 疑似正常英文词组，保守不删。
  if (/^[a-z]{4,}/i.test(after) && !/[一-鿿§]/.test(after.slice(0, 15)) && !/^\*\*/.test(after)) {
    return { seg, removed: false };
  }
  return { seg: after.replace(/^[\s.:：_]*/, ''), removed: true };
}

/**
 * 就地删除穿插在正文里的降解 token（散落型），保留有效正文。三段处理：
 *   ① 逐行删独占整行降解（好词豁免判据 isDegenLine）——court/call/count/課題/exotic/洪水。
 *   ② 段首前缀粘连（court正文，飞书吃掉 \n\n 后）→ stripDegeneratePrefix 逐段删前缀。
 * 代码围栏 ``` 内的行永不删（Duke B' 排除项）。
 * @param strict true=实时出站闸（飞书 send，收紧档）；false=spawn 扫盘（放宽档）。
 *   缺省 true：出站是主用途，收紧档 precision 100%（全机复验）最安全。
 * removed=删除的行/段总数（0 表示原样返回）。删除后折叠多余空行。
 */
export function scrubDegenerateLines(
  text: string, strict = true,
): { text: string; removed: number } {
  if (!text) return { text, removed: 0 };
  let removed = 0;
  const src = text.split('\n');
  // 预扫本 block 的 token 洪水集（≥3 次的裸 token）——供 fence 洪水判据用。
  const counts = new Map<string, number>();
  for (const ln of src) {
    if (!DEGEN_BARE_ASCII_RE.test(ln)) continue;
    const key = bareLineKey(ln);
    counts.set(key, (counts.get(key) ?? 0) + 1);
  }
  const floodSet = new Set<string>();
  for (const [k, c] of counts) if (c >= 3) floodSet.add(k);

  // 预扫本 block 行首 call: 复读次数（≥2 = 泄漏且自我复制，与 marker-flood/token 洪水同源）。
  // 与真 tool_use 共存的 call: 提纲（本函数只收到 text，tool_use 在 record 层）也在此覆盖。
  let callLeakCount = 0;
  for (const ln of src) if (DEGEN_CALL_LEAK_RE.test(ln)) callLeakCount++;
  const callFlood = callLeakCount >= 2;

  // Pass ①：逐行删独占整行降解（跳过代码围栏 ``` 内）。
  let inFence = false;
  const kept: string[] = [];
  for (let i = 0; i < src.length; i++) {
    const ln = src[i]!;
    if (/^[ \t]*```/.test(ln)) { inFence = !inFence; kept.push(ln); continue; }
    if (inFence) { kept.push(ln); continue; } // 代码围栏内永不删
    if (isDegenLine(ln, src, i, strict, floodSet, callFlood)) { removed++; continue; }
    kept.push(ln);
  }
  let out = kept.join('\n');

  // Pass ②：逐段删段首前缀粘连（按 \n\n 分段）。
  out = out.split(/(\n\n)/).map((seg) => {
    if (seg === '\n\n') return seg;
    const r = stripDegeneratePrefix(seg);
    if (r.removed) removed++;
    return r.seg;
  }).join('');

  if (removed === 0) return { text, removed: 0 };
  out = out.replace(/\n{3,}/g, '\n\n').replace(/^\n+|\n+$/g, '');
  return { text: out, removed };
}

export interface OutboundLeakResult {
  /** True iff a structurally-complete tool-call leak was detected in the text. */
  leaked: boolean;
  /** Text with the leak tail removed (== input when not leaked). */
  text: string;
  /** Chars removed (0 when not leaked). */
  removedChars: number;
}

/**
 * Detect + strip a tool-call leak from outbound card text.
 *
 * Returns the original text untouched unless BOTH a `<invoke name=` marker and
 * the structural-completeness signature are present — a bare mention of the
 * literal `<invoke name=` (e.g. this very module's docs, or an agent explaining
 * the bug) does NOT trip it.
 */
export function stripOutboundToolcallLeak(text: string): OutboundLeakResult {
  if (!text || !LEAK_TEXT_RE.test(text)) {
    return { leaked: false, text, removedChars: 0 };
  }
  // 结构完整性闸：只有"完整粘贴的工具调用结构"才算泄漏，孤立提及不算。
  if (!LEAK_STRUCT_RE.test(text)) {
    return { leaked: false, text, removedChars: 0 };
  }

  // 优先按 fence 行截断（连引导词一起切），否则裸截。取最早的截断点。
  const fenceMatch = LEAK_FENCE_RE.exec(text);
  const bareMatch = LEAK_BARE_RE.exec(text);
  let cutAt = -1;
  if (fenceMatch) cutAt = fenceMatch.index;
  if (bareMatch && (cutAt === -1 || bareMatch.index < cutAt)) cutAt = bareMatch.index;
  if (cutAt < 0) {
    // LEAK_TEXT_RE + LEAK_STRUCT_RE 命中但两个截断正则都没匹配 —— 理论上不该发生
    // （三条正则共享 `<invoke name=` 核心）。保守起见不截，但标记 leaked 供上游告警。
    return { leaked: true, text, removedChars: 0 };
  }

  const kept = text.slice(0, cutAt).trimEnd();
  return { leaked: true, text: kept, removedChars: text.length - kept.length };
}
