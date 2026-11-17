/**
 * CC PreToolUse guard: a write operation waits for the current request's WI declaration.
 *
 * The declaration record is the CC transcript itself, read with the collector's
 * request boundary: the latest main-chain human input and its promptId, and a
 * successful paired declare_session_wi tool_use/tool_result under that promptId.
 * Only listed write operations are judged; everything else passes untouched.
 * An unreadable transcript or a missing request boundary never blocks work.
 */
import { readFileSync } from 'node:fs';
import { basename, dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

export const DECLARE_TOOL = 'mcp__pulse-request-purpose__declare_session_wi';
export const WORK_ITEM_TOOL = 'mcp__pulse__pulse_work_item';
const ACKS = new Set(['cc_request_wi/v1', 'cc_session_wi/v1']);
// CC writes these under the interrupted request's promptId; they open no request.
const INTERRUPTS = new Set(['[Request interrupted by user]', '[Request interrupted by user for tool use]']);
const WRAPPERS = new Set(['sudo', 'env', 'command', 'exec', 'time', 'nohup', 'nice']);
const SHELLS = new Set(['sh', 'bash', 'zsh']);
const GIT_VALUE_OPTIONS = new Set(['-C', '-c', '--git-dir', '--work-tree', '--namespace', '--exec-path']);

function textInput(row) {
  if (row.type !== 'user' || row.isMeta === true) return undefined;
  const content = row.message?.content;
  if (typeof content === 'string') return content;
  if (Array.isArray(content) && content.length > 0
      && content.every(block => block && typeof block === 'object' && (block.type === 'text' || block.type === 'image'))) {
    return content.filter(block => block.type === 'text' && typeof block.text === 'string').map(block => block.text).join('');
  }
  return undefined;
}

/** A main-chain human request in the collector's sense (cc_request_purpose.request_bindings). */
function isHumanRequest(row) {
  if (row.isSidechain === true || row.isCompactSummary) return false;
  const text = textInput(row);
  if (text === undefined || INTERRUPTS.has(text.trim())) return false;
  // A runtime task completion continues its original request.
  if (row.origin?.kind === 'task-notification' || text.trimStart().startsWith('<task-notification>')) return false;
  return true;
}

function resultText(block) {
  const content = block.content;
  if (typeof content === 'string') return content;
  if (Array.isArray(content) && content.length === 1 && content[0]?.type === 'text') return content[0].text;
  return undefined;
}

/**
 * 'declared' | 'none' | 'undeclared' for the transcript's current request;
 * 'unknown' when the transcript holds no request boundary. The latest
 * acknowledged declaration decides: a request may move from none to a WI.
 */
export function requestDeclarationState(transcript) {
  let request;
  const uses = new Map();
  let declared = false;
  for (const line of transcript.split('\n')) {
    if (!line.trim()) continue;
    let row;
    try { row = JSON.parse(line); } catch { continue; }
    if (!row || typeof row !== 'object') continue;
    if (isHumanRequest(row)) {
      request = { promptId: typeof row.promptId === 'string' && row.promptId ? row.promptId : undefined };
      declared = false;
      continue;
    }
    if (!request || row.isSidechain === true) continue;
    const blocks = row.message?.content;
    if (!Array.isArray(blocks)) continue;
    for (const block of blocks) {
      if (!block || typeof block !== 'object') continue;
      if (row.type === 'assistant' && block.type === 'tool_use' && block.name === DECLARE_TOOL) {
        uses.set(block.id, block.input?.wi);
      } else if (row.type === 'user' && block.type === 'tool_result' && uses.has(block.tool_use_id)
          && block.is_error !== true && (request.promptId === undefined || row.promptId === request.promptId)) {
        try {
          const ack = JSON.parse(resultText(block) ?? '');
          if (ACKS.has(ack?.schema) && ack.wi === uses.get(block.tool_use_id)) declared = ack.wi === 'none' ? 'none' : true;
        } catch { /* not this server's ack */ }
      }
    }
  }
  return request ? (declared === 'none' ? 'none' : declared ? 'declared' : 'undeclared') : 'unknown';
}

// Segments are cut before quoting is resolved, so a quote may be left at either end of a word.
function words(segment) {
  return (segment.match(/'[^']*'|"(?:[^"\\]|\\.)*"|\S+/g) ?? [])
    .map(word => word.replace(/^['"]+|['"]+$/g, ''));
}

function guardedArgv(argv) {
  let index = 0;
  while (index < argv.length && (/^[A-Za-z_][A-Za-z0-9_]*=/.test(argv[index]) || WRAPPERS.has(argv[index]))) index++;
  const tokens = argv.slice(index);
  const program = basename(tokens[0] ?? '');
  if (SHELLS.has(program)) {
    const flag = tokens.findIndex(token => /^-[a-z]*c[a-z]*$/.test(token));
    return flag > 0 && tokens[flag + 1] !== undefined ? guardedCommand(tokens[flag + 1]) : undefined;
  }
  if (program === 'git') {
    let at = 1;
    while (at < tokens.length && tokens[at].startsWith('-')) at += GIT_VALUE_OPTIONS.has(tokens[at]) ? 2 : 1;
    return tokens[at] === 'commit' || tokens[at] === 'push' ? `git ${tokens[at]}` : undefined;
  }
  if (program === 'gh' || tokens.some(token => basename(token) === 'pulse-gh.py')) {
    const pr = tokens.findIndex((token, at) => token === 'pr' && tokens[at + 1] === 'merge');
    return pr >= 0 ? 'pr merge' : undefined;
  }
  if (program === 'launchctl' && (tokens[1] === 'bootstrap' || tokens[1] === 'bootout')) return `launchctl ${tokens[1]}`;
  return undefined;
}

/** The guarded write operation a shell command contains, if any. */
export function guardedCommand(command) {
  if (typeof command !== 'string') return undefined;
  for (const segment of command.split(/\|\||&&|[;|&\n()`]|\$\(/)) {
    const found = guardedArgv(words(segment));
    if (found) return found;
  }
  return undefined;
}

// Creating a WI is how a request obtains the number it then declares; the
// kernel accepts these intents without a WI, so they cannot wait for one.
export const CREATE_INTENTS = new Set(['create_draft', 'create_lightweight']);

/** The guarded work-item write a `pulse_work_item` call performs, if any. */
export function guardedWorkItem(input) {
  if (input?.action !== 'execute' || CREATE_INTENTS.has(input?.intent)) return undefined;
  return 'pulse_work_item execute';
}

/** The guarded write operation a tool call performs, if any. */
export function guardedOperation(toolName, toolInput) {
  if (toolName === 'Bash') return guardedCommand(toolInput?.command);
  if (toolName === WORK_ITEM_TOOL) return guardedWorkItem(toolInput);
  return undefined;
}

/** A subagent's hook may name its own transcript; the request lives in the main one. */
export function mainTranscriptPath(path) {
  if (basename(dirname(path)) !== 'subagents') return path;
  const sessionDir = dirname(dirname(path));
  return join(dirname(sessionDir), `${basename(sessionDir)}.jsonl`);
}

export function denialReason(operation, state = 'undeclared') {
  if (state === 'none') return `本请求声明为 none，写操作需先由主会话调用 ${DECLARE_TOOL} 声明所属 WI，再执行 ${operation}。`;
  return `本请求尚未声明 WI：先由主会话调用 ${DECLARE_TOOL}（参数 wi）登记本请求所属 WI，再执行 ${operation}。`;
}

/** Hook decision for one PreToolUse input; undefined lets the call proceed. */
export function preToolUseDecision(input, read = path => readFileSync(path, 'utf8')) {
  const operation = guardedOperation(input?.tool_name, input?.tool_input);
  if (!operation || typeof input?.transcript_path !== 'string') return undefined;
  let state;
  try { state = requestDeclarationState(read(mainTranscriptPath(input.transcript_path))); } catch { return undefined; }
  if (state !== 'undeclared' && state !== 'none') return undefined;
  return { hookSpecificOutput: { hookEventName: 'PreToolUse', permissionDecision: 'deny',
    permissionDecisionReason: denialReason(operation, state) } };
}

async function main() {
  let raw = '';
  for await (const chunk of process.stdin) raw += chunk;
  let input;
  try { input = JSON.parse(raw); } catch { return; }
  const decision = preToolUseDecision(input);
  if (decision) process.stdout.write(JSON.stringify(decision));
}

if (process.argv[1] && fileURLToPath(import.meta.url) === process.argv[1]) await main();
