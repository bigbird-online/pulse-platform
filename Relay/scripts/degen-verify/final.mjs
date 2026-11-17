#!/usr/bin/env node
// DEFINITIVE offline verification with LINE-CONTEXT ground truth.
//
// Ground truth per candidate line (judge-independent of any bad-word list):
//   TRUE-DEGEN iff the line is a bare short-token / exotic / cjk-fence / course-phrase
//   line AND its context corroborates machine degeneration:
//     (a) the NEXT non-empty line is `<invoke name=` (fence-above-leak), OR
//     (b) the line participates in a token flood (>=3 identical bare-token lines in
//         the block), OR
//     (c) the line is exotic-script (always degen in a zh/en workspace), OR
//     (d) the line is a CJK homophone fence (課件/课件/课题/課題) at line head, OR
//     (e) course-of-action: / course<turn#> phrase, OR
//     (f) prefix-粘连 (court/course/core/care + non-space + CJK/§ within 60ch).
//   NORMAL otherwise.
//
// A judge is scored:
//   TP  = removes a TRUE-DEGEN line
//   FN  = leaves a TRUE-DEGEN line   (miss)
//   FP  = removes a NORMAL line      (prose damage)
//
// Both judges run per-line with the same context, so scoring is apples-to-apples.

import fs from 'node:fs';
import path from 'node:path';
import readline from 'node:readline';
import { lineJudgeCurrent, lineJudgeNew } from './judges-line.mjs';

const ROOT = '/home/pulse/.claude/projects';
const INVOKE_RE = /<(?:antml:)?invoke name=/;
const EXOTIC = /^[ \t]*[ऀ-ॿЀ-ӿ؀-ۿ฀-๿԰-֏Ⴀ-ჿ]{2,}[ \t.:：．]*$/;
const CJK = /^[ \t]*(課件|课件|课题|課題)/;
const COURSE_PHRASE = /^[ \t]*course(?:\s+of\s+action[:：]|\s+\d)/i;
const PREFIX_LEAD = /^(court|course|core|care)(\S)/i;
const BARE_ASCII_SHAPE = /^[ \t]*([a-z][a-z0-9_]{0,14})(?:[ _-][a-z0-9_]{1,14}){0,2}[ \t.:：．]*$/i;

function nextNonEmpty(lines, i) {
  for (let j = i + 1; j < lines.length; j++) { if (lines[j].trim()) return lines[j]; }
  return '';
}

function isLastNonEmpty(lines, i) {
  for (let j = i + 1; j < lines.length; j++) { if (lines[j].trim()) return false; }
  return true;
}
function groundTruth(lines, i, floodSet, ctx) {
  // ctx = { stopDegen, hasTU, blockHasInvoke }
  const ln = lines[i];
  if (!ln.trim()) return false;
  if (EXOTIC.test(ln)) return true;
  if (CJK.test(ln)) return true;
  if (COURSE_PHRASE.test(ln)) return true;
  // prefix-粘连
  const pm = PREFIX_LEAD.exec(ln.trim());
  if (pm && /[一-鿿§]|\*\*/.test(ln.trim().slice(0, 60))) {
    const after = ln.trim().slice(pm[1].length);
    if (!(/^[a-z]{4,}/i.test(after) && !/[一-鿿§]/.test(after.slice(0, 15)) && !/^\*\*/.test(after))) return true;
  }
  // bare short-token line
  if (BARE_ASCII_SHAPE.test(ln)) {
    const rawTrim = ln.trim().replace(/[ \t.:：．]+$/, '');
    const form = rawTrim.toLowerCase();
    // (a) fence above a leaked <invoke> — any bare token here is a fence.
    if (INVOKE_RE.test(nextNonEmpty(lines, i))) return true;
    // (b) token flood participant.
    if (floodSet.has(form)) return true;
    // The tail/invoke-context signals only corroborate degen for the actual fence
    // family shape: a SINGLE lowercase alpha token (court/course/call/count/core…).
    // Uppercase echoes (PONG/ALPHA/GLM_OK) and prose headings (`Now push:`) are NOT
    // fence degen even at a tool_use mid-turn — exclude them from ground truth.
    const isFenceShape = /^[a-z]{2,12}$/.test(form);
    if (isFenceShape) {
      // (c) block is a leak (has <invoke>) and record is degen-stop, no real tool_use.
      if (ctx.blockHasInvoke && ctx.stopDegen && !ctx.hasTU) return true;
      // (d) fence token is the LAST non-empty line of a degen-stop no-tool-use record.
      if (ctx.stopDegen && !ctx.hasTU && isLastNonEmpty(lines, i)) return true;
    }
  }
  return false;
}

const files = [];
for (const dir of fs.readdirSync(ROOT)) {
  const dp = path.join(ROOT, dir);
  let st; try { st = fs.statSync(dp); } catch { continue; }
  if (!st.isDirectory()) continue;
  for (const f of fs.readdirSync(dp)) if (f.endsWith('.jsonl')) files.push(path.join(dp, f));
}

const M = {
  candidateLines: 0, trueDegen: 0, normal: 0,
  cur:   { tp: 0, fn: 0, fp: 0 },
  strict:{ tp: 0, fn: 0, fp: 0 },
  relaxed:{tp: 0, fn: 0, fp: 0 },
};
const curFP = new Map(), strictFP = new Map(), relaxedFP = new Map();
const curFN = new Map(), strictFN = new Map(), relaxedFN = new Map();
const bump = (m, k) => m.set(k, (m.get(k) || 0) + 1);

for (const fp of files) {
  const rl = readline.createInterface({ input: fs.createReadStream(fp), crlfDelay: Infinity });
  for await (const raw of rl) {
    if (!raw.trim()) continue;
    let r; try { r = JSON.parse(raw); } catch { continue; }
    if (r?.type !== 'assistant') continue;
    const blocks = r.message?.content;
    if (!Array.isArray(blocks)) continue;
    const hasTU = blocks.some((b) => b && b.type === 'tool_use');
    const stop = r.message?.stop_reason;
    const stopDegen = stop === 'tool_use' || stop === 'max_tokens' || stop === 'refusal' || stop === 'end_turn';
    for (const b of blocks) {
      if (b?.type !== 'text' || typeof b.text !== 'string') continue;
      const lines = b.text.split('\n');
      const blockHasInvoke = INVOKE_RE.test(b.text);
      const ctx = { stopDegen, hasTU, blockHasInvoke };
      // build flood set for this block
      const counts = new Map();
      for (const ln of lines) {
        if (!BARE_ASCII_SHAPE.test(ln)) continue;
        const form = ln.trim().replace(/[ \t.:：．]+$/, '').toLowerCase();
        counts.set(form, (counts.get(form) || 0) + 1);
      }
      const floodSet = new Set([...counts.entries()].filter(([, c]) => c >= 3).map(([k]) => k));
      // track code-fence state
      let inFence = false;
      for (let i = 0; i < lines.length; i++) {
        const ln = lines[i];
        if (/^[ \t]*```/.test(ln)) { inFence = !inFence; continue; }
        if (!ln.trim()) continue;
        // only score "candidate" lines: short-ish OR matches a degen shape
        const isCandidate = ln.trim().length <= 60 && (
          BARE_ASCII_SHAPE.test(ln) || EXOTIC.test(ln) || CJK.test(ln) ||
          COURSE_PHRASE.test(ln) || PREFIX_LEAD.test(ln.trim())
        );
        if (!isCandidate) continue;
        // CJK-fence-with-long-trailing (课题:ssh…) is "cut-to-end" degen handled by
        // pool.ts hasSameLineTrailingAfterFence, NOT by scrubDegenerateLines. Exclude
        // from scrub-judge scoring (it's a different removal mechanism, still handled).
        const cjkTrailing = /^[ \t]*(課件|课件|课题|課題)[^\n]{5,}/.test(ln);
        if (cjkTrailing) { M.cutToEndDegen = (M.cutToEndDegen || 0) + 1; continue; }
        M.candidateLines++;
        const gt = !inFence && groundTruth(lines, i, floodSet, ctx);
        if (gt) M.trueDegen++; else M.normal++;

        const c = lineJudgeCurrent(ln, lines, i, inFence);
        const s = lineJudgeNew(ln, lines, i, inFence, true, floodSet);
        const x = lineJudgeNew(ln, lines, i, inFence, false, floodSet);

        for (const [judge, hit, FPm, FNm, key] of [
          ['cur', c, curFP, curFN, 'cur'],
          ['strict', s, strictFP, strictFN, 'strict'],
          ['relaxed', x, relaxedFP, relaxedFN, 'relaxed'],
        ]) {
          if (hit && gt) M[key].tp++;
          else if (hit && !gt) { M[key].fp++; bump(FPm, ln.trim().slice(0, 80)); }
          else if (!hit && gt) { M[key].fn++; bump(FNm, ln.trim().slice(0, 80)); }
        }
      }
    }
  }
}

function top(m, n = 40) { return [...m.entries()].sort((a, b) => b[1] - a[1]).slice(0, n); }
function show(title, m) {
  console.log(`\n=== ${title} (distinct ${m.size}) ===`);
  for (const [k, v] of top(m)) console.log(`${String(v).padStart(6)}  ${JSON.stringify(k)}`);
}

console.log('=== POPULATION ===');
console.log(JSON.stringify({ candidateLines: M.candidateLines, trueDegen: M.trueDegen, normal: M.normal }, null, 2));
console.log('\n=== SCORES (per candidate line) ===');
console.log('judge     |    TP     FN(miss)   FP(prose damage)   recall    precision');
for (const k of ['cur', 'strict', 'relaxed']) {
  const p = M[k];
  const recall = p.tp / (p.tp + p.fn || 1);
  const prec = p.tp / (p.tp + p.fp || 1);
  console.log(`${k.padEnd(9)} | ${String(p.tp).padStart(6)}  ${String(p.fn).padStart(9)}  ${String(p.fp).padStart(16)}   ${recall.toFixed(4)}   ${prec.toFixed(4)}`);
}
show('CURRENT false positives', curFP);
show('CURRENT misses (FN)', curFN);
show('NEW-STRICT false positives', strictFP);
show('NEW-STRICT misses (FN)', strictFN);
show('NEW-RELAXED false positives', relaxedFP);
show('NEW-RELAXED misses (FN)', relaxedFN);
