// Per-line judges for scoring (kept in sync with the design that will land in
// outbound-leak-guard.ts). Both take (line, lines, i, inFence, strict?) and return
// whether THIS line would be removed.

// ---------------- CURRENT ----------------
const DEGEN_COURSE_PHRASE_LINE = '(?:course(?:\\s+of\\s+action[:：]|\\s+\\d)[^\\n]*)';
const CUR_DEGEN_LINE_RE = new RegExp(
  '^[ \\t]*('
  + '(?:course|core|court|core_directive_acknowledged)\\.?'
  + '|' + DEGEN_COURSE_PHRASE_LINE
  + '|(?:課件|课件|课题|課題)[^\\n]{0,20}'
  + '|[\\u0900-\\u097F\\u0400-\\u04FF\\u0600-\\u06FF\\u0E00-\\u0E7F\\u0530-\\u058F\\u10A0-\\u10FF]{2,}[^\\n]{0,20}'
  + ')[ \\t.:：．]*$',
  'iu',
);
const CUR_PREFIX_LEAD_RE = /^(court|course|core)(\S)/i;
export function lineJudgeCurrent(line, lines, i, inFence) {
  // current scrubber does NOT skip code fences; replicate faithfully.
  if (CUR_DEGEN_LINE_RE.test(line)) return true;
  // prefix-粘连 operates on \n\n-delimited segments; approximate: a line whose head
  // is court/course/core + non-space + CJK/§ within 60ch.
  const seg = line;
  const m = CUR_PREFIX_LEAD_RE.exec(seg);
  if (m && /[一-鿿§]|\*\*/.test(seg.slice(0, 60))) {
    const after = seg.slice(m[1].length);
    if (!(/^[a-z]{4,}/i.test(after) && !/[一-鿿§]/.test(after.slice(0, 15)) && !/^\*\*/.test(after))) return true;
  }
  return false;
}

// ---------------- NEW: good-word exemption ----------------
export const GOOD_WORDS = new Set([
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
const NEW_FILENAME_RE = /^[a-z0-9_.-]+\.(md|jsonl|ts|js|py|json|yaml|yml|txt|sh|toml|cfg|ini|lock|log|csv|tsx|jsx|mjs|cjs|go|rs|rb|java|c|h|cpp|html|css|xml|sql)\b/i;
const NEW_EXOTIC = /^[ \t]*[ऀ-ॿЀ-ӿ؀-ۿ฀-๿԰-֏Ⴀ-ჿ]{2,}[ \t.:：．]*$/;
const NEW_CJK = /^[ \t]*(課件|课件|课题|課題)[^\n]{0,20}[ \t.:：．]*$/;
const NEW_COURSE_PHRASE = /^[ \t]*course(?:\s+of\s+action[:：]|\s+\d)[^\n]*$/i;
const NEW_BARE_ASCII = /^[ \t]*([a-z][a-z0-9_]{0,14})(?:[ _-][a-z0-9_]{1,14}){0,2}[ \t.:：．]*$/i;
const INVOKE_RE = /<(?:antml:)?invoke name=/;
const NEW_PREFIX_LEAD = /^(court|course|core|care)(\S)/i;

function isGoodOrExcluded(line) {
  const trimmed = line.trim().replace(/[ \t.:：．]+$/, '');
  const lower = trimmed.toLowerCase();
  if (NEW_FILENAME_RE.test(trimmed)) return true;
  if (GOOD_WORDS.has(lower)) return true;
  const tokens = lower.split(/[ _-]+/).filter(Boolean);
  if (tokens.length > 1 && tokens.every((t) => GOOD_WORDS.has(t))) return true;
  // snake_case config identifiers (path_constraints, excluded_agents…) — never degen.
  if (/^[a-z][a-z0-9]*(_[a-z0-9]+)+$/.test(lower)) return true;
  return false;
}
function nextNonEmpty(lines, i) {
  for (let j = i + 1; j < lines.length; j++) { if (lines[j].trim()) return lines[j]; }
  return '';
}

function isLastNonEmpty(lines, i) {
  for (let j = i + 1; j < lines.length; j++) { if (lines[j].trim()) return false; }
  return true;
}
function hasPrecedingNonEmpty(lines, i) {
  for (let j = i - 1; j >= 0; j--) { if (lines[j].trim()) return true; }
  return false;
}
export function lineJudgeNew(line, lines, i, inFence, strict, floodSet) {
  if (inFence) return false; // never scrub inside code fences
  if (NEW_EXOTIC.test(line)) return true;
  if (NEW_CJK.test(line)) return true;
  if (NEW_COURSE_PHRASE.test(line)) return true;
  // prefix-粘连 (court/course/core/care + non-space + CJK/§)
  const pm = NEW_PREFIX_LEAD.exec(line.trim());
  if (pm && /[一-鿿§]|\*\*/.test(line.trim().slice(0, 60))) {
    const after = line.trim().slice(pm[1].length);
    if (!(/^[a-z]{4,}/i.test(after) && !/[一-鿿§]/.test(after.slice(0, 15)) && !/^\*\*/.test(after))) return true;
  }
  // bare short-token line → good-word exemption gate
  const m = NEW_BARE_ASCII.exec(line);
  if (!m) return false;
  if (isGoodOrExcluded(line)) return false;
  const trimmed = line.trim().replace(/[ \t.:：．]+$/, '');
  const lower = trimmed.toLowerCase();
  const tokens = lower.split(/[ _-]+/).filter(Boolean);
  const singleAlpha = tokens.length === 1 && /^[a-z]+$/.test(tokens[0]) && tokens[0].length >= 2 && tokens[0].length <= 12;
  // Highest-confidence degen contexts (both strictness levels):
  //   (a) bare token directly above a leaked <invoke>  (fence-above-leak)
  //   (b) bare token participating in a token flood     (court\ncourt\n…)
  const fenceAboveLeak = INVOKE_RE.test(nextNonEmpty(lines, i));
  const isFlood = floodSet && floodSet.has(lower);
  if (singleAlpha && (fenceAboveLeak || isFlood)) return true;

  if (strict) {
    // Live outbound gate: fire ONLY on a single short lowercase alpha token that is
    //   - directly above an <invoke> (handled above), OR
    //   - a flood participant (handled above), OR
    //   - the LAST non-empty line of the block (fence emitted, tool block never came).
    // A `Word:`/`Word.` heading FOLLOWED by real prose content is NOT removed
    // (that's a legit section head at a streaming mid-turn) — avoids the ICT:/Resolved:
    // class of prose damage.
    if (!singleAlpha) return false;
    if (tokens[0].length > 8) return false;
    if (isLastNonEmpty(lines, i) && hasPrecedingNonEmpty(lines, i)) return true;
    return false;
  }
  // Relaxed (spawn-time scan): looser — remove any non-good bare-token line up to 3
  // short tokens. Safe because spawn-time scrub only runs on degen-flagged records.
  if (tokens.some((t) => t.length > 14)) return false;
  return true;
}
