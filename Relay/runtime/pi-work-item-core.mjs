import { execFile } from 'node:child_process';
import { createHash, randomUUID } from 'node:crypto';
import {mkdirSync,statSync,realpathSync,writeFileSync,renameSync,openSync,closeSync} from 'node:fs';
import {join} from 'node:path';
import { guardedCommand, guardedWorkItem } from './cc-session-wi-guard.mjs';
import {STATS_TOOL_NAME,statsAvailable,statsDescription,statsParameters,runStatsQuery} from './pulse-stats-tool.mjs';

// A large WI (WI422: ~3.7 MB result, writes over 120 s) must finish rather than be killed
// before its write commits; a killed write reads back absent and the caller's work is lost.
export const CORE_CALL_TIMEOUT_MS = 600_000;
export const CORE_CALL_MAX_BUFFER = 64 * 1024 * 1024;

function execute(host, args, input) {
  return new Promise(resolve => {
    const child = execFile(host.python, ['-I', '-B', host.entrypoint, ...args],
      {encoding:'utf8', timeout:CORE_CALL_TIMEOUT_MS, maxBuffer:CORE_CALL_MAX_BUFFER},
      (error, stdout, stderr) => {
        try {
          const result = JSON.parse(stdout);
          if (!result || typeof result !== 'object') throw new Error('Invalid result');
          resolve(result);
        } catch {
          // Still unknown: the core may have failed after committing. The exit and the tail
          // of stderr let the caller tell a crash on its input from a lost write.
          const process = error ? {exit_code:typeof error.code === 'number' ? error.code : null,
            signal:error.signal ?? null, timed_out:Boolean(error.killed && error.signal)} : {exit_code:0};
          // Only the exception class leaves the process: stderr may carry URLs, tokens or private paths.
          const last = String(stderr ?? '').trim().split('\n').at(-1) ?? '';
          const exception = /^([A-Za-z_][A-Za-z0-9_.]*(?:Error|Exception|Exit|Interrupt|Warning))(?::|$)/.exec(last)?.[1];
          resolve({status:'unknown', error:{code:'native_command_result_unavailable',
            reason:'Read back the same request before retrying; no compatibility fallback.',
            process, ...(exception ? {exception} : {})}});
        }
      });
    child.stdin?.end(input === undefined ? undefined : JSON.stringify(input));
  });
}

function invoke(host, ref) {
  return execute(host, ['channel', '--human-input-ref', ref]);
}

export function invokeHandover(host, ref) {
  return execute(host,['relay-session','--input','-'],{stage:'accept',ref});
}

export function invokeOperation(host, operation) {
  if (operation.action === 'execute') return execute(host, ['execute', '--input', '-'], operation.envelope);
  const args = [operation.action];
  if (operation.work_item_id) args.push('--wi', operation.work_item_id);
  if (operation.action === 'inspect' && operation.view) args.push('--view', operation.view);
  if (operation.action === 'inspect' && operation.request_id) args.push('--request-id', operation.request_id);
  if (operation.action === 'inspect' && operation.view === 'guide' && operation.intent) args.push('--intent', operation.intent);
  if (operation.after !== undefined) args.push('--after', String(operation.after));
  if (operation.limit !== undefined) args.push('--limit', String(operation.limit));
  return execute(host, args);
}

function latestCommand(messages) {
  const user = messages.findLast(message => message.role === 'user');
  const text = typeof user?.content === 'string' ? user.content
    : (user?.content ?? []).filter(part => part.type === 'text').map(part => part.text).join('\n');
  if (!/^\/wi(?:@[A-Za-z0-9_]+)?(?:\s|$)/.test(text)) return;
  const refs = [...text.matchAll(/\[Host human input reference: ([a-f0-9]{64})\./g)];
  return refs.length === 1 ? refs[0][1] : undefined;
}

// Pi keeps a new Session in memory until its first assistant reply, but the
// core reads the original command from the Session log. Write the same entries
// Pi would flush; Pi then appends to the existing file as it normally does.
export function persistOriginalSession(manager) {
  if (!manager?.persist || manager.flushed !== false || !Array.isArray(manager.fileEntries)) return;
  const file = manager.getSessionFile?.();
  if (!file) return;
  const fd = openSync(file, 'wx');
  try {
    for (const entry of manager.fileEntries) writeFileSync(fd, `${JSON.stringify(entry)}\n`);
  } finally {
    closeSync(fd);
  }
  manager.flushed = true;
}

const RESULT_GUIDANCE =
  'This is the installed native core result. A rejected, pending, unknown, or human_decision_required result is not successful takeover. '+
  'When business affiliation is missing, review the existing requirements and recorded decisions now; ask the human only about undecided choices. '+
  'Do not finish with a promise of background activation. No background retry is implied. '+
  'After an actual decision, record it through record_affiliation with its Host human input reference and resume the same WI. '+
  'Preserve existing decisions and completed work. A request to understand and align does not authorize implementation.';

// The core returns whole WI views (megabytes for a large WI). What the model
// sees by default is a brief of at most BRIEF_LIMIT characters: the decision,
// this request's own result and where the rest is; `path` and `page` read any
// part on demand in pages of PAGE_SIZE. The full result stays in details.
export const BRIEF_LIMIT = 512;
export const PAGE_SIZE = 4000;

const clip = (text, size) => text.length <= size ? text : text.slice(0, size - 1) + '…';
const json = value => JSON.stringify(value ?? null);

// The parts of the result an Agent may read next: the requested view's items
// first, then the largest objects and lists (a list is one part, never split).
// Breadth first, so a copy of a part already seen (latest_observation repeats
// the result) is named once, by its shortest path.
function parts(result) {
  const size = value => {
    const length = json(value).length;
    return length >= 1000 ? Math.round(length / 1000) + 'k' : String(length);
  };
  const found = [];
  const seen = new Set();
  let level = [[result, '']];
  for (let depth = 0; depth <= 2 && level.length; depth++) {
    const next = [];
    for (const [value, path] of level) for (const [key, child] of Object.entries(value)) {
      const at = path ? path + '.' + key : key;
      if (!child || typeof child !== 'object' || at === 'result.items') continue;
      const text = json(child);
      if (text.length <= 400 || seen.has(text)) continue;
      seen.add(text);
      if (Array.isArray(child) || depth === 2) found.push([at, text.length]);
      else next.push([child, at]);
    }
    level = next;
  }
  const items = Array.isArray(result.result?.items)
    ? ['result.items(' + result.result.items.length + ' items, ' + size(result.result.items) + ')'] : [];
  // A resume names the handover checkpoint first: a successor reads it before anything else.
  if (result.result?.latest_checkpoint) items.unshift('result.latest_checkpoint(' + size(result.result.latest_checkpoint) + ')');
  return [...items, ...found.filter(([at]) => at !== 'result.latest_checkpoint')
    .sort((a, b) => b[1] - a[1]).slice(0, 4 - items.length)
    .map(([at, length]) => at + (length >= 1000 ? '(' + Math.round(length / 1000) + 'k)' : '(' + length + ')'))];
}

// Fields that name an action or an object: kept whole, never cut in the middle.
const KEY_FIELDS = ['intent', 'action', 'reason', 'view', 'work_item_id', 'request_id', 'contract_id',
  'candidate_id', 'effect_id', 'operation_id', 'decision', 'status', 'state', 'revision', 'number',
  'repository', 'head_sha', 'record_id', 'created', 'parent', 'agent_action'];

// A small object keeps its whole JSON; a large one keeps its key fields, whole.
function essential(value, size) {
  const whole = json(value);
  if (whole.length <= size || !value || typeof value !== 'object' || Array.isArray(value)) return whole;
  const kept = Object.fromEntries(KEY_FIELDS.filter(key => value[key] !== undefined && json(value[key]).length <= 140)
    .map(key => [key, value[key]]));
  return json(kept) + (Object.keys(kept).length < Object.keys(value).length ? '+' : '');
}

// The few fields a reader needs to decide, per response shape.
function decisions(result) {
  const out = [];
  if (result.principal) out.push('identity: actor=' + result.principal.actor + ' session=' + result.principal.session
    + ' latest_human_input_ref=' + (result.latest_human_input_ref ?? 'none'));
  const observed = result.result?.request_observation?.state;
  if (observed) out.push('original request: ' + observed);
  const read = result.result;
  if (read && Array.isArray(read.items)) out.push('items: ' + read.items.length
    + (read.coverage ? ' coverage=' + read.coverage : '')
    + (read.next_offset !== undefined && read.next_offset !== null ? ' next_offset=' + read.next_offset : '')
    + (Array.isArray(read.unavailable_items) && read.unavailable_items.length
      ? ' unavailable=' + read.unavailable_items.length : ''));
  // history pages carry their cursor inside the read result.
  if (result.schema === 'harness.intent_guide/v1') out.push(result.intent
    ? 'guide ' + result.intent + ': ' + result.input + '; read path "guide" for fields, example and refusals'
    : 'intents with guides: ' + (read?.items ?? []).map(item => item.intent).join(', ') + '; read one with intent');
  const cursor = read?.next_cursor ?? result.next_cursor;
  if (cursor !== undefined && cursor !== null) out.push('next_cursor: ' + cursor);
  return out;
}

// The latest checkpoint's record and as much of its summary as `room` allows,
// so a successor starts from the handover at once; null when not even the
// record and an opening fit.
function checkpointLine(checkpoint, room) {
  if (!checkpoint || typeof checkpoint.summary !== 'string') return null;
  const prefix = 'latest checkpoint ' + checkpoint.record_id + ': ';
  const size = Math.min(220, room - prefix.length);
  return size >= 16 ? prefix + clip(checkpoint.summary.replace(/\s+/g, ' '), size) : null;
}

export function resultText(result, note='', execute=false) {
  // Priority: the status line, next_action, the error, the latest checkpoint
  // (cut to the room left), then the rest. A line is added whole or not at
  // all; the read-back line, next_action and a resume's checkpoint path are
  // reserved so they always fit.
  const head = 'status: ' + result.status + (result.request_id ? ' request_id: ' + result.request_id : '')
    + (result.work_item?.id ? ' wi: ' + result.work_item.id + '@' + result.work_item.local_revision : '');
  // The head already names request_id in full; the read-back line refers to it.
  const readBack = 'read more: ' + (execute && result.request_id
    ? 'inspect view=request, the request_id above and path (this original response)'
    : 'repeat this read with path ("" for all) and page');
  // A resume's parts line naming the checkpoint's full text is reserved too.
  const more = parts(result);
  const fullPath = more[0]?.startsWith('result.latest_checkpoint(') ? 'parts: '.length + more[0].length + 1 : 0;
  const base = BRIEF_LIMIT - readBack.length - 1 - fullPath;
  const lines = [head];
  const add = (line, room = base) => {
    if (lines.join('\n').length + 1 + line.length <= room) lines.push(line);
  };
  // next_action is kept whole; the error reason takes the room left beside it
  // (its full text stays readable through the read-back line).
  const next = result.next_action ? 'next_action: ' + essential(result.next_action, 120) : null;
  // A shape refusal names where its input guide is; the guide itself stays in error.input_guide.
  const guided = result.error?.input_guide;
  const guide = guided ? 'input guide: path error.input_guide' + (guided.intent
    ? ', or inspect view=guide intent=' + guided.intent : ', or inspect view=guide') : null;
  if (result.error) {
    const prefix = 'error: ' + result.error.code + ': ';
    const room = base - head.length - prefix.length - 1 - (next ? next.length + 1 : 0)
      - (guide ? guide.length + 1 : 0);
    add(prefix + clip(String(result.error.reason ?? ''), Math.max(40, Math.min(180, room))));
  }
  const reserved = base - (next ? next.length + 1 : 0);
  if (guide) add(guide, reserved);
  if (result.status === 'human_decision_required')
    add('Ask the human only about undecided choices; this is not authorization to implement.', reserved);
  const checkpoint = checkpointLine(result.result?.latest_checkpoint, reserved - lines.join('\n').length - 1);
  if (checkpoint) add(checkpoint, reserved);
  for (const line of decisions(result)) add(line, reserved);
  if (next) add(next);
  const own = result.result?.original_request_result;
  if (own) add('result: ' + essential(own, 140));
  if (note) add(note.trim());
  for (let count = more.length; count > 0; count--) {
    const line = 'parts: ' + more.slice(0, count).join(', ');
    if (lines.join('\n').length + 1 + line.length + 1 + readBack.length <= BRIEF_LIMIT) { lines.push(line); break; }
  }
  lines.push(readBack);
  return lines.join('\n');
}

// This process's own execute responses, by request_id, so a brief's cut parts
// stay readable: the core journals no rejected request and cannot read a
// creation that returned no WI ID. Reading one never repeats the execute.
const ORIGINALS = new Map();
const ORIGINALS_KEPT = 32;
function keepOriginal(requestId, result) {
  ORIGINALS.delete(requestId);
  ORIGINALS.set(requestId, result);
  while (ORIGINALS.size > ORIGINALS_KEPT) ORIGINALS.delete(ORIGINALS.keys().next().value);
}

// One page of the part of the result at a dotted path ('' for the whole result).
export function pageText(result, path, page=0) {
  let value = result;
  for (const key of path ? path.split('.') : []) {
    if (value === null || typeof value !== 'object' || !(key in value))
      return 'path not found: ' + path + '; available: ' + clip(Object.keys(value ?? {}).join(', '), 300);
    value = value[key];
  }
  const text = json(value);
  const pages = Math.max(1, Math.ceil(text.length / PAGE_SIZE));
  if (!Number.isInteger(page) || page < 0 || page >= pages) return 'page out of range: ' + page + ' of ' + pages;
  return 'path: ' + (path || '(result)') + ' page ' + page + '/' + (pages - 1) + ' (' + text.length + ' chars)\n'
    + text.slice(page * PAGE_SIZE, (page + 1) * PAGE_SIZE);
}

async function observeStandalone(host, ctx) {
  if (!host.standaloneBindingRoot) return;
  const sessionId=ctx?.sessionManager?.getSessionId?.();
  if (!sessionId || !/^[A-Za-z0-9-]{1,128}$/.test(sessionId)) throw new Error('Original Pi session identity unavailable');
  if (host.launcherPid!==process.ppid) throw new Error('Original native launcher ended or changed');
  const root=host.standaloneBindingRoot;
  mkdirSync(root,{recursive:true,mode:0o700});
  const metadata=statSync(root);
  if(realpathSync(root)!==root || metadata.uid!==process.getuid?.() || metadata.mode&0o077) throw new Error('Standalone process proof must be private');
  const {nativeProcessStamp}=await import('../dist/native-core-runtime.js');
  const record={schema:'pulse.standalone-session-source/v1',runtime:'pi',session_id:sessionId,
    process:nativeProcessStamp(process.pid),launcher_pid:process.ppid,launcher_process:nativeProcessStamp(process.ppid)};
  const target=join(root,String(process.pid)+'.json');const temporary=target+'.'+randomUUID();
  writeFileSync(temporary,JSON.stringify(record)+'\n',{flag:'wx',mode:0o600});renameSync(temporary,target);
}

// A WI, or "none" for chat, read-only queries and explanations.
const DECLARED = /^(?:wi_[0-9]{8}_[0-9]{3}|none)$/;

/** The only accepted declaration shape, filled with a WI the Agent already supplied. */
export function retryArguments(params) {
  const found = [...new Set(Object.values(params ?? {}).filter(v => typeof v === 'string' && DECLARED.test(v)))];
  return {wi: found.length === 1 ? found[0] : 'wi_YYYYMMDD_NNN'};
}

/** The guarded write operation a Pi tool call performs, if any (same list as the CC guard). */
export function piGuardedOperation(toolName, input) {
  if (toolName === 'bash') return guardedCommand(input?.command);
  if (toolName === 'pulse_work_item') return guardedWorkItem(input);
  return undefined;
}

// A write operation waits for the current request's WI declaration, as CC's
// PreToolUse guard does. A new prompt opens a request; steering continues it.
// Before the first observed prompt the state is unknown and never blocks.
export function installSessionWiGuard(pi) {
  // false: undeclared; 'none': declared chat/read-only; true: declared a WI.
  let declared;
  pi.on('input', event => {
    if (event.streamingBehavior === undefined && !String(event.text ?? '').trimStart().startsWith('/')) declared = false;
  });
  pi.on('tool_result', event => {
    if (event.toolName === 'declare_session_wi' && event.isError !== true)
      declared = (event.details?.wi ?? event.input?.wi) === 'none' ? 'none' : true;
  });
  pi.on('tool_call', event => {
    const operation = piGuardedOperation(event.toolName, event.input);
    if (!operation || (declared !== false && declared !== 'none')) return;
    return {block:true, reason: declared === 'none'
      ? `本请求声明为 none，写操作需先调用 declare_session_wi 声明所属 WI，再执行 ${operation}。`
      : `本请求尚未声明 WI：先调用 declare_session_wi（参数 wi）登记本请求所属 WI，再执行 ${operation}。`};
  });
}

export function installPiWorkItemCore(pi, host, run = invoke, runOperation = invokeOperation) {
  if (!host?.python || !host?.entrypoint) throw new Error('Missing reviewed native WI core');
  const results = new Map();
  const pending = new Map();
  let handoverAccepted=false;
  const entryType = 'pulse.native-request/v1';
  const canonical = value => JSON.stringify(value, function (_key, item) {
    return item && typeof item === 'object' && !Array.isArray(item)
      ? Object.fromEntries(Object.entries(item).sort(([a],[b])=>a.localeCompare(b))) : item;
  });
  const restore = ctx => {
    for (const row of ctx?.sessionManager?.getEntries?.() ?? []) {
      if (row.type !== 'custom' || row.customType !== entryType) continue;
      const {identity, request_id, unresolved} = row.data ?? {};
      if (!identity || !request_id) continue;
      if (unresolved) pending.set(identity, request_id);
      else if (pending.get(identity) === request_id) pending.delete(identity);
    }
  };
  pi.on('session_start', (_event, ctx)=>{ pending.clear(); restore(ctx); });
  if (host.sessionWiGuard === true) installSessionWiGuard(pi);
  // Read-only Stats query, the same tool CC and Codex see through the pulse MCP server.
  if (statsAvailable()) pi.registerTool({
    name:STATS_TOOL_NAME, label:'PULSE Stats', description:statsDescription,
    promptSnippet:'Use pulse_stats to read cost, outcomes, quotas, health and the work board PulseStats shows.',
    parameters:statsParameters,
    async execute(_toolCallId, params) {
      const {isError, text} = await runStatsQuery(params ?? {});
      return {content:[{type:'text',text}], details:{backend:'pulse_stats'}, isError};
    },
  });
  // Request purpose for ComputeCost, as CC's declare_session_wi. The
  // collector reads this call and result from the Pi session; nothing is
  // written here and no WI authority is granted or checked.
  pi.registerTool({
    name:'declare_session_wi', label:'Declare request WI',
    description:'登记当前原生请求的归属。要改文件、提 PR 或动生产时声明所属 WI；闲聊、只读查询、解释说明声明 none，不要为此新建 WI。每个新的人类请求或独立任务各自声明；复用会话不继承前一请求的归属。当前请求的子任务保留原始请求归属。'+
      '一个会话只为一个 WI 谱系（一个 WI 及其子 WI）写代码、提 PR：声明过谱系外 WI 的会话，其提交登记会被拒；谱系外 WI 的写工作请人类 /new 后在新会话声明。'+
      '本请求已声明 none 后不再改声明，写操作留到下一条请求。首行为 [WI: 子WI] 的子代理为该子 WI 工作，其提交与成本归该子 WI；一个请求可派多个这样的子代理，并行推进多个子 WI；谱系内的提交可登记在谱系任一 WI 上。',
    promptSnippet:'At the start of each new human request or independent task, call declare_session_wi once: the WI it belongs to, or none for chat and read-only work.',
    parameters:{type:'object', properties:{wi:{type:'string',pattern:'^(?:wi_[0-9]{8}_[0-9]{3}|none)$'}},
      required:['wi'], additionalProperties:false},
    async execute(_toolCallId, params) {
      if (!params || Object.keys(params).join() !== 'wi' || !DECLARED.test(params.wi))
        throw new Error('wi_only_arguments_required: retry with '+JSON.stringify(retryArguments(params)));
      return {content:[{type:'text',text:JSON.stringify({schema:'pi_request_wi/v1',wi:params.wi,origin:'agent_declaration'})}],
        details:{wi:params.wi}};
    },
  });
  pi.registerTool({
    name:'pulse_work_item', label:'Native WorkItem',
    description:'Read, resume, or execute a WorkItem decision through the installed new SCL core on the production ledger. The result returns in this turn.',
    promptSnippet:'Use the native SCL for WorkItem operations; handle its decision result in this turn.',
    promptGuidelines:[RESULT_GUIDANCE,
      'Every result is a brief of at most 512 characters: status, request_id, WI, error, next_action, this request\'s own result and "more:" naming the largest parts with their sizes. Read a part only when you need it: repeat the read (inspect, resume or list) with path (a dotted path from more:, or "" for the whole result) and page (0, 1, … pages of 4000 characters); an execute is never repeated, read its result back with inspect view request, its request_id and path: this process keeps its original execute responses (rejected and unknown ones included, which the core may not have journaled), otherwise the core\'s record of the request is read. When unsure of an intent\'s input, first read its guide: action inspect, view guide, intent <name> (no WI needed; without intent it lists the intents): fields, a complete example envelope and common refusals with fixes; a refusal for input shape carries error.input_guide. The usage guide is also the work-item-lifecycle skill (ssot/skills/ops/work-item-lifecycle/SKILL.md): its decision tree and its table of rejection texts with their fixes.',
      'For an unknown command result, follow its next_action. It inspects the original request, or reuses the exact request when creation has not returned a WI ID. If absent, repeat the same execute input with the returned request_id; if committed, resume the WI. Do not use a new request ID or shell reconstruction.',
      'Use inspect/resume to understand an existing WI before WI implementation. Do not invoke an old SCL or create a replacement WI. Before writing on a WI, check capability local_work: current_grant means this session holds it; held_by_same_agent_in_another_session means the human must send /wi continue <WI> in this session first (checkpoint and record_work stay open meanwhile).',
      'retry_effect only restarts an effect read back as observed_absence. For unknown or attempt_started, resume the WI to read it back; an interrupted independent verifier continues through the holder\'s resume, not retry_effect.',
      'submit_pull_request takes {repository, number, head_sha (40 hex)}; the head must be a git commit made by this session or a sub-agent it spawned, one command per commit starting with cd <literal absolute path> &&. The request_id is the identity of a request: reuse it only to read back or resume an unknown result. After a settled failure (e.g. checks failed, then rerun green) registering the same head again is a new attempt: give it a new request_id; the passed review of that head is reused. immediately only changes review dispatch timing.',
      'Before creating a WI, decide whether the new matter belongs to the current WI; if it does, record or plan it there instead of creating one. Chat, read-only queries and explanations need no WI.',
      'A new matter with a different goal or acceptance becomes a child WI: the session holding the current WI executes create_child with the create_lightweight input plus parent: <current WI> (one level only; a child cannot have children), then spawns a sub-agent whose prompt starts with the line [WI: <child WI>]; the sub-agent does not declare a WI. A child is lightweight and is held by the same session. When the sub-agent reports back, the main session executes review_child on the child WI (work_item_id: <child WI>) with input {decision: approved|returned, reason}; approved checks the child work records and closes it, returned keeps it active. A parent WI cannot end while a child is open.',
      'To start new work, execute intent create_lightweight (or create_draft for a plan the human confirms) with input {summary: one plain line of at most 120 characters, why, what, acceptance: [nonempty texts], resources: [logical scopes such as "repo"]}; the owner is you. The result returns the new WI ID: declare it, then start_work with {}.',
      'To attach result files (record_work artifacts), pass files {name: {path: absolute file under your admitted roots, logical: "repo/<path>", version: label, access?: "team"}} and reference each in input as {file: name}; the core captures the bytes and fills sha256/size. Never write hashes yourself.',
      'To offer a collaboration contract, execute offer_collaboration with main_only stated; default to true with target_work_item_id. true sends it to the recipient\'s main session: choose it when the work uses the recipient\'s ongoing work (unmerged changes, authorizations it holds, a WI it is advancing) or needs write access. false lets an unattended fork handle it: choose it only when read-only work on what main already has completes the contract. A main-only Agent recipient takes target_work_item_id, the WI it handles the contract on; omitted, the one active WI it holds is used, and a refusal lists the candidates.',
      'When the context is nearly full or compacted repeatedly (the Host may say so) and work on the WI you hold remains, hand it to a fresh session of yourself: checkpoint {summary: done, pending, next action, resources}, then at once execute request_rollover with input {summary}, record nothing more and end the turn. No human decision is needed; the Host changes sessions after the turn ends and the successor resumes from that checkpoint.',
      'To move this conversation to another WI of yours (as /wi switch does; never ask the human to switch), checkpoint {summary: done, pending, next action, resources} on the WI you hold, then at once execute request_switch on it with input {target: the other WI, summary}, record nothing more and end the turn. The Host then opens the target\'s session or a new one and hands it to you there; the WI you leave stays held by this session. A target that is your WI or its child answers no_switch: keep working here.',
      'To close a lightweight WI after human acceptance, execute complete_lightweight with input {record_ids: every WorkRecorded record_id (not checkpoints), acceptance_refs: ["AC-1", … "AC-n"] in definition order} and the acceptance message as top-level human_input_ref. Each intent\'s fields: inspect view guide with intent; the full operations guide is OPERATIONS.md inside the installed harness.pyz (read it with unzip -p <scl-core>/active/harness.pyz OPERATIONS.md).',
      'This tool governs WI lifecycle and recorded resources, not OS permissions or separately authorized Host maintenance. Missing WI scope is not proof of filesystem denial. Follow explicit user authorization, actual Host limits and the target writer contract; do not invent an SCL production-file admission requirement.'],
    parameters:{type:'object', properties:{
      action:{type:'string',enum:['identity','list','inspect','resume','execute']},
      work_item_id:{type:'string',pattern:'^wi_[A-Za-z0-9_]+$'},
      view:{type:'string',enum:['state','request','history','plan','tasks','collaborations','candidates','records','effects','measurements','watch','notices','guide']},
      request_id:{type:'string',pattern:'^[A-Za-z0-9_.:-]+$'}, after:{type:'integer',minimum:0}, limit:{type:'integer',minimum:1,maximum:500},
      path:{type:'string',maxLength:200,description:'Read this dotted part of the result instead of the brief ("" for the whole result)'},
      page:{type:'integer',minimum:0,description:'Page of the part read with path, 4000 characters each'},
      intent:{type:'string',description:'execute: the business intent; inspect view guide: the intent whose input guide to read'}, input:{type:'object'}, files:{type:'object'}, sources:{type:'object'}, human_input_ref:{type:'string',pattern:'^[a-f0-9]{64}$'},
    }, required:['action'], additionalProperties:false},
    async execute(toolCallId, params, _signal, _onUpdate, ctx) {
      restore(ctx);
      await observeStandalone(host,ctx);
      let handover;
      if(!handoverAccepted && process.env.PULSE_NATIVE_HANDOVER_REF) {
        handover=await invokeHandover(host,process.env.PULSE_NATIVE_HANDOVER_REF);
        handoverAccepted=handover.session_handover?.accepted===true;
        // Handover errors do not remove tools or even read capability. The
        // same core independently checks each requested business operation.
      }
      if (!['identity','list','inspect','resume','execute'].includes(params.action)) throw new Error('Unknown native action');
      if (params.work_item_id && !/^wi_[A-Za-z0-9_]+$/.test(params.work_item_id)) throw new Error('Invalid WorkItem identity');
      // path and page only choose what the model reads; the core never sees them.
      // An execute is never repeated to read it: its result is read back by request_id.
      const {path, page, ...call} = params;
      if (call.action === 'execute' && (path !== undefined || page !== undefined))
        throw new Error('path and page read inspect, resume or list results; read an execute result back with action inspect, view request and its request_id');
      if (call.action === 'inspect' && call.view === 'request' && path !== undefined && ORIGINALS.has(call.request_id))
        return {content:[{type:'text',text:'original execute response, ' + pageText(ORIGINALS.get(call.request_id), path, page ?? 0)}],
          details:{backend:'native_core',result:ORIGINALS.get(call.request_id)}};
      params = call;
      const operation = {...params};
      const identity = createHash('sha256').update(canonical(Object.fromEntries(
        Object.entries(params).filter(([key])=>key !== 'request_id')))).digest('hex');
      if (params.action === 'execute') {
        if (!params.intent || !params.input) throw new Error('A native business intent and input are required');
        operation.envelope = {request_id:pending.get(identity) ?? params.request_id ?? 'tool.'+createHash('sha256').update(
          (process.env.SCL_SESSION_REF ?? '')+'\0'+toolCallId).digest('hex'),
          work_item_id:params.work_item_id, intent:params.intent, input:params.input,
          ...(params.files ? {files:params.files} : {}), ...(params.sources ? {sources:params.sources} : {}),
          ...(params.human_input_ref ? {human_input_ref:params.human_input_ref} : {})};
      }
      if (operation.envelope) {
        // The Pi Session records the transport intention before invocation.
        // This is recovery metadata, never a second business decision ledger.
        pending.set(identity, operation.envelope.request_id);
        pi.appendEntry?.(entryType, {identity, request_id:operation.envelope.request_id, unresolved:true});
      }
      const result = await runOperation(host, operation);
      if(handover && !handoverAccepted) result.host_handover_diagnostic={status:handover.status,error:handover.error};
      if (operation.envelope && !['unknown','pending'].includes(result.status) &&
          !result.error?.retryable && result.error?.code !== 'ledger_commit_unknown') {
        pending.delete(identity);
        pi.appendEntry?.(entryType, {identity, request_id:operation.envelope.request_id, unresolved:false});
      }
      if (operation.envelope) keepOriginal(result.request_id ?? operation.envelope.request_id, result);
      if (result.status === 'unknown' && !result.next_action) {
        result.request_id ??= operation.envelope?.request_id;
        result.next_action = result.request_id && !params.work_item_id
          ? {...params,request_id:result.request_id}
          : {action:'inspect',work_item_id:params.work_item_id,
            ...(result.request_id ? {view:'request',request_id:result.request_id} : {view:'effects'})};
      }
      return {content:[{type:'text',text:path === undefined ? resultText(result, '', params.action === 'execute')
        : pageText(result, path, page ?? 0)}],
        details:{backend:'native_core',result}, isError:result.status === 'rejected'};
    },
  });
  // Persisted channel provenance is consumed exactly once, including unknown results.
  pi.on('context', async (event, ctx) => {
    const ref = latestCommand(event.messages);
    if (!ref) return;
    if (!results.has(ref)) {
      persistOriginalSession(ctx?.sessionManager);
      results.set(ref, run(host, ref));
    }
    const result = await results.get(ref);
    return {messages:[...event.messages, {
      role:'custom', customType:'native-work-item-command', display:false, timestamp:Date.now(),
      content:resultText(result,' The authenticated slash command has already been handled; do not replay it.'),
    }]};
  });
}

export default function install(pi) {
  installPiWorkItemCore(pi, JSON.parse(process.env.PULSE_WORK_ITEM_CORE_HOST ?? '{}'));
}
