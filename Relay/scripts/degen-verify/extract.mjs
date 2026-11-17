#!/usr/bin/env node
// Offline corpus extractor for the B' good-word-exemption redesign.
// Walks all jsonl, pulls assistant text blocks, splits into lines, and emits
// a compact JSONL of candidate lines with context flags for downstream judging.
//
// A "candidate line" = a line that COULD plausibly be a degen line: it is short
// (<=40 chars trimmed), non-empty, and starts at column 0 (no leading indent that
// would mark it as code/list). We keep the whole set so we can measure false
// positives of any judge against the full normal-line population too.

import fs from 'node:fs';
import path from 'node:path';
import readline from 'node:readline';

const ROOT = '/home/pulse/.claude/projects';
const OUT = '/tmp/degen-verify/lines.jsonl';

// ---- current record-level bare-fence detection (mirrors pool.ts) ----
const BARE_FENCE_ASCII_RE = /(^|\n)(course|core|court|care|core_directive_acknowledged)(\.?)(?=[.:：]|\n|$|\s*<(?:antml:)?invoke)/i;
const BARE_FENCE_CJK_RE = /(^|\n)[ \t]*(課件|课件|课题|課題)/;
const BARE_FENCE_COURSE_PHRASE_RE = /(^|\n)[ \t]*course(?:\s+of\s+action[:：]|\s+\d)/i;
const DEGEN_EXOTIC_FENCE_RE = /(^|\n)[ \t]*[ऀ-ॿЀ-ӿ؀-ۿ฀-๿԰-֏Ⴀ-ჿ]{2,}[ \t.:：．]*(?=\n|$|\s*<(?:antml:)?invoke)/;
const DEGEN_TOKEN_FLOOD_RE = /(^|\n)[ \t]*([\p{L}]{2,12})[ \t.:：．]*(\s+\2[ \t.:：．]*){2,}/u;
const LEAK_TEXT_RE = /<(?:antml:)?invoke name=/;

function recordHasRealToolUse(blocks) {
  return Array.isArray(blocks) && blocks.some((b) => b && b.type === 'tool_use');
}

function classifyRecordDegen(record) {
  // returns {isDegenRecord: bool, reasons: [...]}
  const reasons = [];
  if (record?.type !== 'assistant') return { isDegenRecord: false, reasons };
  const stop = record.message?.stop_reason;
  const blocks = record.message?.content;
  if (!Array.isArray(blocks)) return { isDegenRecord: false, reasons };
  if (recordHasRealToolUse(blocks)) return { isDegenRecord: false, reasons };
  const stopOk = stop === 'tool_use' || stop === 'max_tokens' || stop === 'end_turn' || stop === 'refusal';
  for (const b of blocks) {
    if (b?.type !== 'text' || typeof b.text !== 'string') continue;
    const text = b.text;
    if (stopOk) {
      if (BARE_FENCE_ASCII_RE.test(text)) reasons.push('ascii');
      if (BARE_FENCE_CJK_RE.test(text)) reasons.push('cjk');
      if (BARE_FENCE_COURSE_PHRASE_RE.test(text)) reasons.push('course-phrase');
      if (DEGEN_EXOTIC_FENCE_RE.test(text)) reasons.push('exotic');
      if (DEGEN_TOKEN_FLOOD_RE.test(text)) reasons.push('flood');
      if (LEAK_TEXT_RE.test(text)) reasons.push('invoke-text');
    }
  }
  return { isDegenRecord: reasons.length > 0, reasons: [...new Set(reasons)] };
}

const files = [];
for (const dir of fs.readdirSync(ROOT)) {
  const dp = path.join(ROOT, dir);
  let st;
  try { st = fs.statSync(dp); } catch { continue; }
  if (!st.isDirectory()) continue;
  for (const f of fs.readdirSync(dp)) {
    if (f.endsWith('.jsonl')) files.push(path.join(dp, f));
  }
}

const out = fs.createWriteStream(OUT);
let nFiles = 0, nRecords = 0, nAssistantText = 0, nLinesEmitted = 0;
let nDegenRecords = 0;

async function processFile(fp) {
  const rl = readline.createInterface({ input: fs.createReadStream(fp), crlfDelay: Infinity });
  for await (const raw of rl) {
    if (!raw.trim()) continue;
    let rec;
    try { rec = JSON.parse(raw); } catch { continue; }
    nRecords++;
    if (rec?.type !== 'assistant') continue;
    const blocks = rec.message?.content;
    if (!Array.isArray(blocks)) continue;
    const cls = classifyRecordDegen(rec);
    if (cls.isDegenRecord) nDegenRecords++;
    for (const b of blocks) {
      if (b?.type !== 'text' || typeof b.text !== 'string') continue;
      nAssistantText++;
      const lines = b.text.split('\n');
      for (const ln of lines) {
        const trimmed = ln.trim();
        if (!trimmed) continue;
        // Only keep "short-ish" lines (candidate degen surface). Long prose lines
        // are never degen fence lines; keeping them all would bloat output. But we
        // DO keep them for false-positive accounting via a sampled flag.
        const isShort = trimmed.length <= 40;
        out.write(JSON.stringify({
          l: ln,
          s: isShort ? 1 : 0,
          dr: cls.isDegenRecord ? 1 : 0,       // line came from a degen-flagged record
          rr: cls.reasons,                     // record-level reasons
        }) + '\n');
        nLinesEmitted++;
      }
    }
  }
}

for (const fp of files) {
  await processFile(fp);
  nFiles++;
  if (nFiles % 200 === 0) process.stderr.write(`  ...${nFiles}/${files.length} files\n`);
}
out.end();
await new Promise((r) => out.on('finish', r));

console.log(JSON.stringify({
  nFiles, nRecords, nAssistantText, nLinesEmitted, nDegenRecords,
}, null, 2));
