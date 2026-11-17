/**
 * spec-drift.ts — spec 漂移检测核心(零依赖 TypeScript 实现)
 *
 * 为什么用 TS 重写而不是调 Python 的 src/scan.py:
 *   1. Relay 是纯 TS 仓,加 python3 依赖等于给 CI 环境多一个前提。
 *   2. 实测 Python 版全量扫描 50.9s(Relay 全量测试 136s,占 37%)——不可接受。
 *      瓶颈已定位:476 个 symbol 锚点 × 24227 行源码逐行正则 = 2286 万次 re.search(51s),
 *      外加 35 次 `git log -S` 子进程(9.3s)。
 *   3. 本实现改用**倒排索引**:源码一次分词建 Set,锚点查表 O(1),消掉 O(锚点×行数)。
 *      并且**不跑 git 历史**——闸只需要知道"漂移数有没有增加",
 *      不需要区分"曾存在后被删"与"从未落地"(那是人读报告时才需要的分类,
 *      由 Python 版全量扫描提供)。
 *
 * 能力范围(诚实声明,与 Python 版的差异):
 *   实现  symbol / test 两类锚点 —— 覆盖 Relay 实测 47 条漂移的全部
 *         (symbol 43 + test 4;const/wire/manual 三类在 Relay 当前 spec 里
 *          推断不出任何锚点,实现了也是零收益)。
 *   不实现 git 历史回溯、consistency 检查、逐份 spec 报表 —— 那些是"体检"用途,
 *         不是"闸"用途。闸要快、要只回答一个问题:漂移数增加了吗。
 *
 * 推断规则与 Python 版 infer.py 逐条对齐,任何一边改了都要同步(见 README 已知限制)。
 */

import { readFileSync, readdirSync, statSync } from 'node:fs';
import { join, relative, sep } from 'node:path';

// ---------------------------------------------------------------- 类型

export type Verdict = 'holds' | 'broken' | 'unverifiable';

export interface Anchor {
  type: 'symbol' | 'test';
  specFile: string;
  specLine: number;
  target: string;
  claim: string;
  verdict: Verdict;
  detail: string;
  /** 稳定身份:基线按这个 key 比对,而不是按总数。行号变动不算新漂移。 */
  key: string;
}

export interface ScanResult {
  anchors: Anchor[];
  broken: Anchor[];
  specFiles: number;
  elapsedMs: number;
}

export interface ScanOptions {
  repo: string;
  specGlobDir?: string; // 默认 'specs'
  testDirs?: string[]; // 默认 ['tests']
  srcDirs?: string[]; // 默认 ['src']
}

// ---------------------------------------------------------------- 文件遍历

const SKIP_DIRS = new Set(['node_modules', '.git', 'dist', 'coverage', '__pycache__']);

function walk(dir: string, ext: string[], out: string[] = []): string[] {
  let entries: string[];
  try {
    entries = readdirSync(dir);
  } catch {
    return out;
  }
  for (const name of entries) {
    if (SKIP_DIRS.has(name)) continue;
    const full = join(dir, name);
    let st;
    try {
      st = statSync(full);
    } catch {
      continue;
    }
    if (st.isDirectory()) walk(full, ext, out);
    else if (ext.some((e) => name.endsWith(e))) out.push(full);
  }
  return out;
}

// ---------------------------------------------------------------- 倒排索引

/**
 * 源码标识符集合。把 24227 行源码一次性分词成 Set,
 * 锚点校验从"逐行正则"降为"查 Set",这是 51s → 亚秒的关键。
 */
const IDENT_RE = /[A-Za-z_$][A-Za-z0-9_$]*/g;

/**
 * 建标识符集合。**src/ 与 tests/ 必须分开建**,不能合成一个。
 *
 * 这个区分是实测撞出来的(2026-07-25),过程值得记下来:
 *
 * 第一版只索引 src/,比 Python 版多报 1 条(`feishu-pairing-http/tasks.md:19`
 * 的 `runningIds` —— 该符号在 src/ 确实不存在,只作为局部变量出现在
 * tests/relay-http.test.ts)。为了和 Python 版读数对齐,我把 tests/ 并进了同一个集合。
 *
 * **那是个错误的取舍**,并且立刻被端到端测试抓住:把 src/commands.ts 里的
 * `handleStart` 改名后,spec 已经指空,闸却仍判 holds ——因为 tests/ 里还留着旧名字。
 * 为了让数字好看,我把闸最该抓的场景(改代码忘了同步 spec)放过去了。
 *
 * 现在的口径:
 *   srcIdents  —— **唯一的判定依据**。spec 引用的符号在不在生产代码里,只问它。
 *   testIdents —— 只用于降级:符号不在 src/ 但在 tests/ 里存在时,判 unverifiable
 *                 而非 broken。因为这类多半是 spec 点名的**计划中的测试函数**,
 *                 报成"漂移"是误报;但它也确实没有生产实现,不该判 holds。
 *
 * 代价:比 Python 版全量扫描多出 `runningIds` 这类条目的分类差异。
 * 这是有意接受的——闸的正确性标准是"能不能抓住新引入的漂移",不是"数字和报告一样"。
 */
function buildIdentIndex(repo: string, srcDirs: string[]): Set<string> {
  const idents = new Set<string>();
  for (const d of srcDirs) {
    for (const f of walk(join(repo, d), ['.ts', '.js', '.mts', '.cts'])) {
      let text: string;
      try {
        text = readFileSync(f, 'utf8');
      } catch {
        continue;
      }
      const m = text.match(IDENT_RE);
      if (m) for (const s of m) idents.add(s);
    }
  }
  return idents;
}

/** 测试用例名索引: 绝对路径 -> 该文件里所有 it/test/describe 的名字 */
const TEST_NAME_RE = /\b(?:it|test|describe)\s*\(\s*(['"`])([\s\S]+?)\1/g;

function buildTestIndex(repo: string, testDirs: string[]): Map<string, string[]> {
  const idx = new Map<string, string[]>();
  for (const d of testDirs) {
    for (const f of walk(join(repo, d), ['.ts', '.js'])) {
      let text: string;
      try {
        text = readFileSync(f, 'utf8');
      } catch {
        continue;
      }
      const names: string[] = [];
      TEST_NAME_RE.lastIndex = 0;
      let m: RegExpExecArray | null;
      while ((m = TEST_NAME_RE.exec(text)) !== null) names.push(m[2].trim());
      idx.set(f, names);
    }
  }
  return idx;
}

// ---------------------------------------------------------------- 锚点推断
// 与 Python 版 infer.py 的规则逐条对齐。存量 spec 不用重写,从既有写法反推。

/** 契约声明行: 已勾选的 `- [x]`,或表格行 */
const CLAIM_LINE_RE = /^\s*(?:[-*]\s*\[[xX]\]|\|)/;
/** 明确写"待实现/计划"的行不算契约 */
const TODO_HINT_RE = /(待实现|未实现|计划|TODO|尚未|N\/A)/;

/** 反引号里的函数调用 `foo()` / `Obj.foo()` */
const SYMBOL_RE = /`([A-Za-z_][\w]*(?:\.[A-Za-z_][\w]*)?)\(\)`/g;
/** 裸符号: camelCase 或 snake_case,没加反引号但明显是标识符 */
const BARE_SYMBOL_RE =
  /(?<![`\w.])([a-z][a-zA-Z0-9]{3,}[A-Z][a-zA-Z0-9]*|[a-z][a-z0-9]{2,}_[a-z0-9_]+)(?![`\w])/g;
/** 中文语境里的常见误命中(不是代码标识符) */
const BARE_STOPWORDS = new Set([
  'javascript', 'typescript', 'changelog', 'workspace', 'timestamp',
  'username', 'hostname', 'filename', 'pathname', 'endpoint', 'runtime',
  'playbook', 'codebase', 'baseline', 'rollback', 'fallback',
]);

/** `**测试文件**：\`tests/xxx.test.ts\`` */
const TEST_FILE_RE = /\*\*测试文件\*\*[：:]\s*`?(tests?\/[\w./-]+\.(?:test|spec)\.[jt]s)`?/g;
/** 裸引用 \`tests/xxx.test.ts\` */
const BARE_TEST_RE = /`(tests?\/[\w./-]+\.(?:test|spec)\.[jt]s)`/g;
/** `**Unit Test-002**：描述` */
const TEST_CASE_RE = /\*\*((?:Unit|Integration|E2E)\s+Test-[\w.]+)\*\*[：:]\s*([^\n]+)/g;

function lineOf(text: string, pos: number): number {
  let n = 1;
  for (let i = 0; i < pos; i++) if (text.charCodeAt(i) === 10) n++;
  return n;
}

function lineTextAt(text: string, pos: number): string {
  const start = text.lastIndexOf('\n', pos) + 1;
  let end = text.indexOf('\n', pos);
  if (end === -1) end = text.length;
  return text.slice(start, end);
}

function inferAnchors(specRel: string, text: string): Anchor[] {
  const out: Anchor[] = [];
  const seen = new Set<string>();

  const add = (a: Omit<Anchor, 'verdict' | 'detail' | 'key'>) => {
    const key = `${a.type}|${a.specFile}|${a.target}|${a.claim}`;
    if (seen.has(key)) return;
    seen.add(key);
    out.push({ ...a, verdict: 'unverifiable', detail: '', key });
  };

  // 宿主测试文件: 同一份 spec 里出现的第一个测试文件
  const hosts: string[] = [];
  for (const rx of [TEST_FILE_RE, BARE_TEST_RE]) {
    rx.lastIndex = 0;
    let m: RegExpExecArray | null;
    while ((m = rx.exec(text)) !== null) hosts.push(m[1]);
  }
  const host = hosts[0] ?? '';

  // 用例级锚点优先(比文件级强)
  TEST_CASE_RE.lastIndex = 0;
  let m: RegExpExecArray | null;
  while ((m = TEST_CASE_RE.exec(text)) !== null) {
    const line = lineTextAt(text, m.index);
    if (TODO_HINT_RE.test(line)) continue;
    if (/\[\s\]/.test(line)) continue; // 未勾选的本就没声称成立
    add({
      type: 'test', specFile: specRel, specLine: lineOf(text, m.index),
      target: host || 'tests/', claim: `[${m[1].trim()}]`,
    });
  }

  // 文件级测试锚点
  for (const rx of [TEST_FILE_RE, BARE_TEST_RE]) {
    rx.lastIndex = 0;
    while ((m = rx.exec(text)) !== null) {
      const line = lineTextAt(text, m.index);
      if (TODO_HINT_RE.test(line)) continue;
      add({
        type: 'test', specFile: specRel, specLine: lineOf(text, m.index),
        target: m[1], claim: '',
      });
    }
  }

  // 反引号符号
  SYMBOL_RE.lastIndex = 0;
  while ((m = SYMBOL_RE.exec(text)) !== null) {
    const line = lineTextAt(text, m.index);
    if (TODO_HINT_RE.test(line)) continue;
    const sym = m[1].split('.').pop()!;
    if (sym.length < 4 || sym.startsWith('test_')) continue;
    add({
      type: 'symbol', specFile: specRel, specLine: lineOf(text, m.index),
      target: sym, claim: '',
    });
  }

  // 裸符号 —— 只在已勾选的契约声明行上找。
  // 未勾行里的标识符常是测试入参名,对它推断会把"计划中的场景"误报成"符号缺失"。
  BARE_SYMBOL_RE.lastIndex = 0;
  while ((m = BARE_SYMBOL_RE.exec(text)) !== null) {
    const line = lineTextAt(text, m.index);
    if (!CLAIM_LINE_RE.test(line) || TODO_HINT_RE.test(line)) continue;
    const sym = m[1];
    if (BARE_STOPWORDS.has(sym.toLowerCase()) || sym.startsWith('test_')) continue;
    // 出现在 `foo(bar)` 形参位置的是调用示例入参,不是被断言的符号
    const paramLike = new RegExp(`\\w+\\s*\\([^)]*\\b${escapeRe(sym)}\\b[^)]*\\)`);
    if (paramLike.test(line)) continue;
    add({
      type: 'symbol', specFile: specRel, specLine: lineOf(text, m.index),
      target: sym, claim: '',
    });
  }

  return out;
}

function escapeRe(s: string): string {
  return s.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
}

// ---------------------------------------------------------------- 校验

const ID_CLAIM_RE = /^\[(?:Unit|Integration|E2E)\s+Test-/;
const ID_TAG_RE = /\[(?:Unit|Integration|E2E)\s+Test-/;

function verifySymbol(a: Anchor, srcIdents: Set<string>, testIdents: Set<string>): void {
  if (srcIdents.has(a.target)) {
    a.verdict = 'holds';
    a.detail = `符号存在于 src/`;
    return;
  }
  // 只在 tests/ 里存在。这里必须再分一层,分错了闸就废:
  //
  //   - spec 点名的是**测试函数**(`test_xxx` / 名字里带 test) → 判 unverifiable。
  //     这类是"计划中的测试",报 broken 是误报。
  //   - 其余情况 → 判 broken。**生产符号从 src/ 消失就是漂移**,
  //     哪怕 tests/ 里还留着旧名字——那恰恰是"改代码没同步 spec"的典型现场。
  //
  // 第二版曾把"只在 tests/ 出现"一律降级成 unverifiable,结果端到端测试里
  // 把 src/ 全域改名 handleStart→handleStartCommand 后,闸依然是绿的
  // (tests/commands.test.ts 还引用旧名)。那等于把闸最该抓的场景漏掉。
  if (testIdents.has(a.target) && /^test_|[Tt]est/.test(a.target)) {
    a.verdict = 'unverifiable';
    a.detail = `\`${a.target}\` 是测试函数名,只在 tests/ 中出现 → 不作为生产契约判定`;
    return;
  }
  a.verdict = 'broken';
  a.detail = `spec 引用的符号 \`${a.target}\` 在 src/ 中已不存在(可能被改名或删除)`;
}

function verifyTest(
  a: Anchor,
  repo: string,
  testIndex: Map<string, string[]>,
  conventionInUse: boolean,
): void {
  const target = a.target.trim();
  const abs = join(repo, target);

  // 点名了具体测试文件但文件不存在 → 真漂移
  if (/\.(ts|js)$/.test(target) && !testIndex.has(abs)) {
    a.verdict = 'broken';
    a.detail = `spec 引用的测试文件不存在: ${target}`;
    return;
  }

  const named = testIndex.has(abs);

  if (!named) {
    // 未点名宿主 → 在全仓测试里找这个 ID
    if (a.claim) {
      for (const [f, names] of testIndex) {
        if (names.some((n) => n.includes(a.claim))) {
          a.verdict = 'holds';
          a.detail = `ID 指向真实用例(${relative(repo, f)});仅证明该 ID 存在,未证明断言相符`;
          return;
        }
      }
      if (ID_CLAIM_RE.test(a.claim) && conventionInUse) {
        a.verdict = 'broken';
        a.detail =
          `spec 声称 ${a.claim} 已覆盖,但全仓 ${testIndex.size} 个测试文件中无任何用例带此 ID`;
        return;
      }
    }
    a.verdict = 'unverifiable';
    a.detail = `spec 未点名具体测试文件,无法定位宿主 → 覆盖缺口`;
    return;
  }

  const names = testIndex.get(abs)!;
  if (!a.claim) {
    a.verdict = 'unverifiable';
    a.detail = `仅绑到文件粒度,未指定用例名`;
    return;
  }
  if (names.some((n) => n === a.claim || n.includes(a.claim) || a.claim.includes(n))) {
    a.verdict = 'holds';
    a.detail = `用例存在于 ${target}`;
    return;
  }
  // 关键区分: 该文件整体没采用 [ID] 标签约定 → 是"从未绑定"(覆盖缺口),不是"绑了但断了"
  const fileUsesConvention = names.some((n) => ID_TAG_RE.test(n));
  if (!fileUsesConvention && ID_CLAIM_RE.test(a.claim)) {
    a.verdict = 'unverifiable';
    a.detail = `${target} 未采用 [ID] 标签约定 → 该断言从未与测试绑定,属覆盖缺口`;
    return;
  }
  a.verdict = 'broken';
  a.detail = `${target} 中无此用例: ${a.claim}`;
}

// ---------------------------------------------------------------- 主入口

export function scanDrift(opts: ScanOptions): ScanResult {
  const t0 = Date.now();
  const repo = opts.repo;
  const specDir = join(repo, opts.specGlobDir ?? 'specs');
  const testDirs = opts.testDirs ?? ['tests'];
  const srcDirs = opts.srcDirs ?? ['src'];

  // 两个索引严格分开(见 buildIdentIndex 注释):src/ 定判定,tests/ 只用于降级
  const srcIdents = buildIdentIndex(repo, srcDirs);
  const testIdents = buildIdentIndex(repo, testDirs);
  const testIndex = buildTestIndex(repo, testDirs);
  const conventionInUse = [...testIndex.values()].some((ns) => ns.some((n) => ID_TAG_RE.test(n)));

  const specs = walk(specDir, ['.md']);
  const anchors: Anchor[] = [];

  for (const f of specs) {
    let text: string;
    try {
      text = readFileSync(f, 'utf8');
    } catch {
      continue;
    }
    const rel = relative(repo, f).split(sep).join('/');
    for (const a of inferAnchors(rel, text)) {
      if (a.type === 'symbol') verifySymbol(a, srcIdents, testIdents);
      else verifyTest(a, repo, testIndex, conventionInUse);
      anchors.push(a);
    }
  }

  return {
    anchors,
    broken: anchors.filter((a) => a.verdict === 'broken'),
    specFiles: specs.length,
    elapsedMs: Date.now() - t0,
  };
}

// ---------------------------------------------------------------- 基线

export interface Baseline {
  /** 生成时间,只作参考不参与判定 */
  generated: string;
  /** 基线漂移条数 */
  driftCount: number;
  /**
   * 已知漂移的稳定 key 列表。按 key 比对而不是只比数字,
   * 这样"修好一条 + 引入一条"不会因为总数不变而蒙混过关。
   */
  knownKeys: string[];
}

export interface GateVerdict {
  pass: boolean;
  /** 基线里没有的新漂移 */
  newDrift: Anchor[];
  /** 基线里有、现在已修好的 */
  fixed: string[];
  current: number;
  baseline: number;
  elapsedMs: number;
}

export function evaluate(result: ScanResult, baseline: Baseline): GateVerdict {
  const known = new Set(baseline.knownKeys);
  const currentKeys = new Set(result.broken.map((a) => a.key));
  const newDrift = result.broken.filter((a) => !known.has(a.key));
  const fixed = baseline.knownKeys.filter((k) => !currentKeys.has(k));
  return {
    pass: newDrift.length === 0,
    newDrift,
    fixed,
    current: result.broken.length,
    baseline: baseline.driftCount,
    elapsedMs: result.elapsedMs,
  };
}

/** 失败信息必须可行动: 指出哪份 spec 哪一行、引用的什么、为什么断了。 */
export function formatFailure(v: GateVerdict): string {
  const lines: string[] = [];
  lines.push(`spec 漂移增加: 新增 ${v.newDrift.length} 条(基线 ${v.baseline} → 当前 ${v.current})`);
  lines.push('');
  for (const a of v.newDrift) {
    lines.push(`  ${a.specFile}:${a.specLine}`);
    lines.push(`    ${a.detail}`);
  }
  lines.push('');
  lines.push('  处置二选一:');
  lines.push('    1. 改 spec —— 代码是对的,spec 落后了,把上面这几行改对');
  lines.push('    2. 降基线 —— 若你确认这些是既有问题非本次引入,跑:');
  lines.push('       node gate/update-baseline.mjs');
  if (v.fixed.length > 0) {
    lines.push('');
    lines.push(`  (顺带: 你已修好 ${v.fixed.length} 条旧漂移,记得跑上面的命令把基线降下来)`);
  }
  return lines.join('\n');
}
