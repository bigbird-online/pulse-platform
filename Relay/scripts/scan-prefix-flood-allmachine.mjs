// 全机 jsonl 复扫：用 dist 真码的 extractLeadingWords + 真阈值/窗口，跑过所有
// ~/.claude/projects/*/*.jsonl，输出 prefix-flood 命中词分布。证明收窄后：
//   (a) Amanda card 仍命中；(b) 非退化家族词零命中（Duke count/power/review 等词后带正文提纲不误伤）。
// 判据来源 = dist（import），不重实现——防判据漂移。窗口/命中/清零逻辑与 pool.ts
// recordAndCheckPrefixFlood 一致（滑动窗口 = 近 N 条 record，命中即清窗口）。
import { readFileSync, readdirSync, statSync } from 'node:fs';
import { join } from 'node:path';
import { homedir } from 'node:os';
import {
  extractLeadingWords,
  DEGEN_PREFIX_FLOOD_THRESHOLD,
  DEGEN_PREFIX_FLOOD_WINDOW,
} from '../dist/pool.js';

const PROJECTS = join(homedir(), '.claude', 'projects');

// 已知真退化家族词（散落降解 token）。用于把命中分成"真退化"vs"疑似误伤"两栏，方便人肉核对。
// 此表仅用于**分栏展示**，不参与判定——判定完全由 dist extractLeadingWords + 阈值决定。
// 家族成员（全机实证，均为"孤立独占整行 + 重复"的真降解形态）：
//   - fence 降解族：course/core/court/care/county 及变体（detectBareFenceLeak 同族）；
//   - card：Amanda scorecard 语境词降解；
//   - call/count：**孤立整行**的调度关键词降解——注意与 `call 一下…`/`count 一下…`（词后带正文
//     的正常调度提纲）区分：收窄判据只计孤立整行，提纲被排除、不计数（同文件 duke count：
//     孤立 count 15 条计数、count+正文提纲 16 条排除）。bare `call`/`call:` 独占行重复即真泄漏
//     （既有 DEGEN_CALL_LEAK_LINE_RE 逐条判据也管 call:，这里是跨-record 兜底）。
const KNOWN_DEGEN_FAMILY = new Set([
  'card', 'course', 'core', 'court', 'care', 'county', 'cour', 'cours',
  'call', 'count',
]);

function listJsonlFiles(root) {
  const out = [];
  let dirs;
  try { dirs = readdirSync(root); } catch { return out; }
  for (const d of dirs) {
    const dir = join(root, d);
    let st;
    try { st = statSync(dir); } catch { continue; }
    if (!st.isDirectory()) continue;
    let files;
    try { files = readdirSync(dir); } catch { continue; }
    for (const f of files) {
      if (f.endsWith('.jsonl')) out.push(join(dir, f));
    }
  }
  return out;
}

// 复现 pool.ts recordAndCheckPrefixFlood 的滑动窗口 + 命中清零，逐文件跑。
// 返回该文件命中的 { word, count, agentDir } 列表（一个词一次连续 burst 记一次）。
function scanFile(path, agentDir) {
  const win = [];               // Array<Set<string>>
  const counts = new Map();      // word -> record 数
  const hits = [];
  let raw;
  try { raw = readFileSync(path, 'utf8'); } catch { return hits; }
  for (const line of raw.split('\n')) {
    const t = line.trim();
    if (!t) continue;
    let rec;
    try { rec = JSON.parse(t); } catch { continue; }
    if (rec.type !== 'assistant') continue;
    const words = extractLeadingWords(rec); // ← dist 真码
    win.push(words);
    for (const w of words) counts.set(w, (counts.get(w) ?? 0) + 1);
    while (win.length > DEGEN_PREFIX_FLOOD_WINDOW) {
      const oldest = win.shift();
      for (const w of oldest) {
        const c = (counts.get(w) ?? 0) - 1;
        if (c <= 0) counts.delete(w); else counts.set(w, c);
      }
    }
    let hitWord;
    for (const [w, c] of counts) {
      if (c >= DEGEN_PREFIX_FLOOD_THRESHOLD) { hitWord = w; break; }
    }
    if (hitWord) {
      hits.push({ word: hitWord, count: counts.get(hitWord), agentDir });
      // 命中即清窗口（生产行为），避免连片重复计数。
      win.length = 0;
      counts.clear();
    }
  }
  return hits;
}

const files = listJsonlFiles(PROJECTS);
console.log(`扫描 ${files.length} 个 jsonl 文件（${PROJECTS}）`);
console.log(`判据：dist extractLeadingWords（孤立整行短词）+ 阈值 ${DEGEN_PREFIX_FLOOD_THRESHOLD} + 窗口 ${DEGEN_PREFIX_FLOOD_WINDOW}`);

const allHits = [];
for (const f of files) {
  const agentDir = f.split('/').slice(-2, -1)[0];
  const hits = scanFile(f, agentDir);
  for (const h of hits) allHits.push({ ...h, file: f.split('/').pop() });
}

// 命中词分布
const byWord = new Map();
for (const h of allHits) byWord.set(h.word, (byWord.get(h.word) ?? 0) + 1);
const ranked = [...byWord.entries()].sort((a, b) => b[1] - a[1]);

console.log(`\n总命中 burst 数：${allHits.length}`);
console.log(`命中词分布（word × burst 数）：`);
const degenHits = [];
const nonDegenHits = [];
for (const [w, n] of ranked) {
  const fam = KNOWN_DEGEN_FAMILY.has(w) ? '真退化家族' : '★非退化家族(疑误伤)';
  console.log(`  ${w.padEnd(14)} × ${String(n).padStart(3)}   ${fam}`);
  if (KNOWN_DEGEN_FAMILY.has(w)) degenHits.push(w); else nonDegenHits.push(w);
}

console.log(`\n真退化家族命中词：${degenHits.length ? degenHits.join(', ') : '(无)'}`);
console.log(`非退化家族命中词（应为 0）：${nonDegenHits.length ? nonDegenHits.join(', ') : '(无) ✅'}`);

// Amanda card 专项断言
const amandaCard = allHits.filter((h) => h.word === 'card' && h.agentDir.includes('amanda'));
console.log(`\nAmanda card 命中：${amandaCard.length} 次` + (amandaCard.length ? ` ✅ (count 示例 ${amandaCard[0].count})` : ' ❌ 未命中！'));

console.log('\n=== 判定 ===');
console.log(`(a) Amanda card 仍命中: ${amandaCard.length > 0 ? 'PASS ✅' : 'FAIL ❌'}`);
console.log(`(b) 非退化家族零命中: ${nonDegenHits.length === 0 ? 'PASS ✅' : 'FAIL ❌ — ' + nonDegenHits.join(',')}`);
