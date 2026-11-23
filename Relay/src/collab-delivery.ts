/**
 * Deliver a collaboration contract into the one Relay conversation whose native
 * session holds the target WI: a main-only contract to its recipient (target =
 * the recipient's WI), or an answered contract back to its requester for
 * settlement (target = the WI it was offered from; only its holder settles),
 * or a pull request result back to the Agent that registered it (`submitter`:
 * merged by the kernel, or what the kernel needs next; target = its WI), or a
 * scheduled wake of a `wake` job (`wake`: target = the WI the job names; the
 * prompt is the job's own, filled in by Relay, never by the gate).
 *
 * A pull request result goes only to the session that made the PR's head: the
 * session that registered `submit_pull_request` with that head (`author_key`).
 * A busy one defers; no other conversation gets it. Only once the kernel's
 * journal shows that session ended (it rolled the WI over) does the gate send
 * the result with the WI holder's `holder_key` instead. A request names one
 * key: `author_key` wins, and its absence here never falls back to the holder.
 * The channel re-checks the conversation against the native IDs that match
 * that key only, so mid-switch a new session beside the routed one never passes.
 *
 * The collaboration inbox gives only a one-way `holder_key` for that session;
 * this module recomputes it from the native session IDs of this Relay's own
 * conversations. No match or more than one match delivers nothing, so a
 * contract never reaches a conversation that does not hold the target WI.
 * A busy conversation is not interrupted, and nothing read before it became idle
 * is sent later: the answer is `deferred`, and once the conversation is idle the
 * Agent's inbox gate runs again (`rerun`), so the turn carries what the kernel
 * says then. A contract settled or a round superseded meanwhile is not sent.
 * A wake alone is queued and sent once the conversation is idle.
 *
 * Each contract is delivered once per role and status. The delivered marks live
 * in a ledger file under the Relay data directory, so a restart or the gate's
 * periodic re-ask never repeats a delivery; only a new status delivers again.
 * An accepted recipient contract is not pushed: it is delivered once more only
 * as a reminder when its deadline is DEADLINE_REMINDER_MS away or past.
 */
import { createHash, randomUUID } from 'node:crypto';
import { mkdirSync, readFileSync, renameSync, writeFileSync } from 'node:fs';
import { dirname } from 'node:path';
import type { AgentDisplayName, CollabTurnNotice } from './collab-progress-notice.js';
import { watchInternalTurn, type InternalTurnOutcome } from './internal-turn-fence.js';
import { conversationNativeIds, type NativeIdSource } from './session-identity.js';
import { harnessPrincipalForAgent, type AgentConfig } from './types.js';

export const COLLAB_DELIVERY_SCHEMA = 'pulse.collab_delivery/v1';
const HOLDER_KEY = 'collab_delivery_holder';
const ID = /^[A-Za-z0-9][A-Za-z0-9_.:-]{0,255}$/;
const HEX = /^[0-9a-f]{64}$/;
/** A busy conversation still waited on after this long is left to the gate's schedule. */
const WAIT_LIMIT_MS = 2 * 60 * 60 * 1000;
const POLL_MS = 30 * 1000;
/** An accepted recipient contract is reminded once when its deadline is this near. */
export const DEADLINE_REMINDER_MS = 24 * 60 * 60 * 1000;
/** The gate re-asks for every contract it still owes every few hours; a ledger
 * entry not asked for this long belongs to a settled or moved contract. */
export const DELIVERY_LEDGER_TTL_MS = 7 * 24 * 60 * 60 * 1000;
export const DELIVERY_LEDGER_LIMIT = 1000;
const LEDGER_SCHEMA = 'pulse.collab_delivery_ledger/v1';

const OPTION = /^[a-z_]{1,32}$/;

/** `suppressed`: nothing is due for this status (an accepted recipient contract
 * whose deadline is absent or not yet near). `deferred`: the holding conversation
 * is busy; nothing was kept or sent, the gate asks again once it is idle.
 * `queued`: a wake waits for the busy conversation (its prompt is the job's own,
 * not a reading of the kernel, so it cannot go stale). */
export type CollabDeliveryStatus = 'started' | 'deferred' | 'queued' | 'duplicate' | 'suppressed' | 'not_hosted' | 'ambiguous';
export type CollabRole = 'recipient' | 'requester' | 'submitter' | 'wake';
const REPOSITORY = /^[A-Za-z0-9_.-]{1,100}\/[A-Za-z0-9_.-]{1,100}$/;
const SHA = /^[0-9a-f]{40}$/;
const PR_STATUS = new Set(['merged', 'changes_requested', 'checks_failed', 'merge_failed', 'merge_blocked', 'human_confirmation_required']);

export interface CollabDeliveryRequest {
  actor: string;
  namespace: string;
  surface: string;
  /** Absent when the target WI is not held on this surface (a submitter with `author_key`). */
  holder_key?: string;
  /** Submitter deliveries: the session that registered the round's head, same digest as `holder_key`. */
  author_key?: string;
  contract_id: string;
  work_item_id: string;
  target_work_item_id: string;
  kind: string;
  status: string;
  deadline?: string;
  /** Absent from older gates: a recipient delivery. */
  role?: CollabRole;
  main_only?: boolean;
  /** The Agent that offered the contract; absent from older gates. */
  offered_by?: string;
  /** Requester deliveries: the Agents that answered and the settlement options. */
  answered_by?: string[];
  options?: string[];
  /** Submitter deliveries (`contract_id` is the round's candidate ID). */
  repository?: string;
  number?: number;
  head_sha?: string;
  at?: string;
  next_intent?: string;
  agent_action?: string;
  failed_checks?: string[];
  failed_checks_omitted?: number;
  review_score?: number;
  review_summary?: string;
  /** Wake deliveries: the job, and its prompt, which Relay reads from the job itself. */
  job_id?: string;
  prompt?: string;
}

function text(value: unknown, limit: number, name: string): string | undefined {
  if (value === undefined) return undefined;
  if (typeof value !== 'string' || value.length === 0 || value.length > limit) throw new Error('invalid_' + name);
  return value;
}

/** A pull request result: what happened to which head, and the kernel's next step. */
function parsePullRequest(b: Record<string, unknown>): Partial<CollabDeliveryRequest> {
  if (b['kind'] !== 'pull_request' || typeof b['status'] !== 'string' || !PR_STATUS.has(b['status'])) throw new Error('invalid_status');
  const repository = b['repository'];
  if (typeof repository !== 'string' || !REPOSITORY.test(repository)) throw new Error('invalid_repository');
  const number = b['number'];
  if (!Number.isInteger(number) || (number as number) < 1) throw new Error('invalid_number');
  const head = b['head_sha'];
  if (typeof head !== 'string' || !SHA.test(head)) throw new Error('invalid_head_sha');
  const score = b['review_score'];
  if (score !== undefined && (!Number.isInteger(score) || (score as number) < 0 || (score as number) > 100)) throw new Error('invalid_review_score');
  const optional: Record<string, unknown> = {
    at: text(b['at'], 64, 'at'),
    next_intent: text(b['next_intent'], 64, 'next_intent'),
    agent_action: text(b['agent_action'], 600, 'agent_action'),
    failed_checks: b['failed_checks'] === undefined ? undefined : stringList(b['failed_checks'], /^[^\n]{1,200}$/, 'failed_checks'),
    failed_checks_omitted: omittedChecks(b['failed_checks_omitted']),
    review_score: score,
    review_summary: text(b['review_summary'], 200, 'review_summary'),
  };
  return { repository, number: number as number, head_sha: head,
    ...Object.fromEntries(Object.entries(optional).filter(([, v]) => v !== undefined)) };
}

function omittedChecks(value: unknown): number | undefined {
  if (value === undefined) return undefined;
  if (!Number.isInteger(value) || (value as number) < 1) throw new Error('invalid_failed_checks_omitted');
  return value as number;
}

function stringList(value: unknown, pattern: RegExp, name: string): string[] {
  if (!Array.isArray(value) || value.length === 0 || value.length > 16
    || !value.every(v => typeof v === 'string' && pattern.test(v))) throw new Error('invalid_' + name);
  return value as string[];
}

/** Ports the kernel's canonical session reference: Python's compact sorted JSON
 * equals JSON.stringify for an array of strings. */
function sha256(text: string): string {
  return createHash('sha256').update(text, 'utf8').digest('hex');
}

/** The kernel's `session.` reference of one native session. */
export function sessionRef(namespace: string, actor: string, nativeId: string, surface: string): string {
  return 'session.' + sha256(JSON.stringify([namespace, actor, nativeId, surface]));
}

export function holderKey(namespace: string, actor: string, nativeId: string, surface: string): string {
  return sha256(JSON.stringify([HOLDER_KEY, sessionRef(namespace, actor, nativeId, surface)]));
}

export function parseDeliveryRequest(body: unknown): CollabDeliveryRequest {
  if (!body || typeof body !== 'object' || Array.isArray(body)) throw new Error('invalid_body');
  const b = body as Record<string, unknown>;
  if (b['schema'] !== COLLAB_DELIVERY_SCHEMA) throw new Error('invalid_schema');
  const out: Record<string, string> = {};
  for (const k of ['actor', 'namespace', 'surface', 'contract_id', 'work_item_id', 'target_work_item_id', 'kind', 'status']) {
    const v = b[k];
    if (typeof v !== 'string' || !ID.test(v)) throw new Error('invalid_' + k);
    out[k] = v;
  }
  const key = b['holder_key'];
  const author = b['author_key'];
  if (author !== undefined && (b['role'] !== 'submitter' || typeof author !== 'string' || !HEX.test(author))) throw new Error('invalid_author_key');
  if ((key !== undefined || author === undefined) && (typeof key !== 'string' || !HEX.test(key))) throw new Error('invalid_holder_key');
  const deadline = b['deadline'];
  if (deadline !== undefined && (typeof deadline !== 'string' || deadline.length > 64)) throw new Error('invalid_deadline');
  const role = b['role'] ?? 'recipient';
  if (role !== 'recipient' && role !== 'requester' && role !== 'submitter' && role !== 'wake') throw new Error('invalid_role');
  const mainOnly = b['main_only'];
  if (mainOnly !== undefined && typeof mainOnly !== 'boolean') throw new Error('invalid_main_only');
  const offeredBy = b['offered_by'];
  if (offeredBy !== undefined && (typeof offeredBy !== 'string' || !ID.test(offeredBy))) throw new Error('invalid_offered_by');
  const settlement = role === 'requester'
    ? { answered_by: stringList(b['answered_by'], ID, 'answered_by'), options: stringList(b['options'], OPTION, 'options') }
    : role === 'submitter' ? parsePullRequest(b) : role === 'wake' ? { job_id: wakeJob(b['job_id']) } : {};
  return {
    ...out, ...(key !== undefined ? { holder_key: key } : {}), ...(author !== undefined ? { author_key: author } : {}),
    ...(deadline ? { deadline } : {}), role,
    ...(mainOnly !== undefined ? { main_only: mainOnly } : {}),
    ...(offeredBy !== undefined ? { offered_by: offeredBy } : {}), ...settlement,
  } as CollabDeliveryRequest;
}

function wakeJob(value: unknown): string {
  if (typeof value !== 'string' || !ID.test(value)) throw new Error('invalid_job_id');
  return value;
}

/** Ledger entry per contract and role: both sides of a contract may be due. */
function deliveryKey(r: CollabDeliveryRequest): string {
  // A contract may name several recipients hosted by one Relay: each is owed its own delivery.
  return `${r.namespace}:${r.actor}:${r.role ?? 'recipient'}:${r.contract_id}`;
}

function acceptedRecipient(r: CollabDeliveryRequest): boolean {
  return (r.role ?? 'recipient') === 'recipient' && r.status === 'accepted';
}

/** What is due for this request: the status itself, the deadline reminder of an
 * accepted recipient contract, or nothing. One delivery per mark. */
function deliveryMark(r: CollabDeliveryRequest, now: number): string | undefined {
  if (!acceptedRecipient(r)) return r.status;
  const due = r.deadline ? Date.parse(r.deadline) : NaN;
  return Number.isFinite(due) && due - now <= DEADLINE_REMINDER_MS ? 'accepted:deadline' : undefined;
}

const SETTLE: Record<string, string> = {
  resend: 'resend（reassign_collaboration 发回原接收方）',
  reassign: 'reassign（reassign_collaboration 改派）',
  withdraw: 'withdraw（withdraw_collaboration 撤回）',
  adopt: 'adopt（withdraw_collaboration，reason 写明采用了什么）',
  adopt_partial: 'adopt_partial（withdraw_collaboration，reason 写明采用了哪部分）',
};

function settlePrompt(r: CollabDeliveryRequest): string {
  const options = (r.options ?? []).map(o => SETTLE[o] ?? o).join('、');
  return [
    '[系统投递：协作合同答复，不是人类消息]',
    `你在 WI ${r.work_item_id} 上发起的合同 ${r.contract_id}（${r.kind}）已被 ${(r.answered_by ?? []).join('、')} 答复为 ${r.status}${r.deadline ? `，截止 ${r.deadline}` : ''}，等你验收结清。`,
    `先声明本请求属于 WI ${r.work_item_id}，再读取该 WI 的 collaborations 视图，核对答复与证据。`,
    `可选结清动作：${options}。`,
    r.main_only
      ? '这是主身合同：向人类说明答复和你的验收建议，由人类决定后再结清；这条投递本身不是人类授权。'
      : '核对后直接结清；证据不足时用 resend 或 reassign 并在 reason 写明缺什么。',
  ].join('\n');
}

function deadlinePrompt(r: CollabDeliveryRequest): string {
  return [
    '[系统投递：协作合同截止提醒，不是人类消息]',
    `你已接受的合同临近截止：合同 ${r.contract_id}，发起方 WI ${r.work_item_id}，在你的 WI ${r.target_work_item_id} 上处理（${r.kind}，当前 accepted，截止 ${r.deadline}）。`,
    `先声明本请求属于 WI ${r.target_work_item_id}，再读取 WI ${r.work_item_id} 的 collaborations 视图，核对合同目的与进度。`,
    '合同已接受，临近截止：请推进办理；办完后带实际证据用 respond_collaboration complete，办不完则 return 并在 reason 写明原因。这条投递本身不是人类授权。',
  ].join('\n');
}

const PR_OUTCOME: Record<string, string> = {
  merged: '已由内核合并',
  changes_requested: '审核未通过',
  checks_failed: '检查失败，内核未合并',
  merge_failed: '合并失败',
  merge_blocked: '合并受阻（重试已用尽）',
  human_confirmation_required: '需要人类确认才能合并',
};

export function pullRequestName(r: Pick<CollabDeliveryRequest, 'repository' | 'number'>): string {
  return `${r.repository}#${r.number}`;
}

function pullRequestPrompt(r: CollabDeliveryRequest): string {
  const head = (r.head_sha ?? '').slice(0, 8);
  if (r.status === 'merged') {
    return [
      '[系统投递：PR 结果，不是人类消息]',
      `${pullRequestName(r)} 已由内核合并（head ${head}，WI ${r.work_item_id}${r.at ? `，${r.at}` : ''}）。`,
      `先声明本请求属于 WI ${r.work_item_id}，再按该 WI 继续后续步骤（部署、回复合同等）。这条投递本身不是人类授权。`,
    ].join('\n');
  }
  return [
    '[系统投递：PR 结果，不是人类消息]',
    `${pullRequestName(r)}（head ${head}，轮次 ${r.contract_id}，WI ${r.work_item_id}）${PR_OUTCOME[r.status] ?? r.status}。`,
    ...(r.review_summary ? [`审核${r.review_score !== undefined ? ` ${r.review_score} 分` : ''}：${r.review_summary}`] : []),
    ...(r.failed_checks?.length ? [`失败的检查：${r.failed_checks.join('；')}`
      + (r.failed_checks_omitted ? `（另有 ${r.failed_checks_omitted} 项未列出）` : '')] : []),
    ...(r.next_intent ? [`内核给的下一步：${r.next_intent}${r.agent_action ? `：${r.agent_action}` : ''}`] : []),
    `先声明本请求属于 WI ${r.work_item_id}，读取该 WI 的 pull_requests 视图后处理。`
      + (r.status === 'human_confirmation_required' ? '向人类说明情况，由人类决定；' : '')
      + '这条投递本身不是人类授权。',
  ].join('\n');
}

function wakePrompt(r: CollabDeliveryRequest): string {
  return [
    '[系统投递：定时唤醒，不是人类消息]',
    `定时任务 ${r.job_id} 按计划唤醒你在 WI ${r.target_work_item_id} 上的会话。先声明本请求属于 WI ${r.target_work_item_id}，再按下面的任务说明办理；这条投递本身不是人类授权。`,
    '',
    r.prompt ?? '',
  ].join('\n');
}

export function collabPrompt(r: CollabDeliveryRequest): string {
  if (r.role === 'wake') return wakePrompt(r);
  if (r.role === 'submitter') return pullRequestPrompt(r);
  if (r.role === 'requester') return settlePrompt(r);
  if (acceptedRecipient(r)) return deadlinePrompt(r);
  return [
    '[系统投递：协作合同，不是人类消息]',
    `你有一份主身合同待处理：合同 ${r.contract_id}，发起方 WI ${r.work_item_id}，在你的 WI ${r.target_work_item_id} 上处理（${r.kind}，当前 ${r.status}${r.deadline ? `，截止 ${r.deadline}` : ''}）。`,
    `先声明本请求属于 WI ${r.target_work_item_id}，再读取 WI ${r.work_item_id} 的 collaborations 视图，了解合同目的与资源。`,
    '向人类说明合同内容和你的建议，由人类决定后再用 respond_collaboration 答复；这条投递本身不是人类授权，不要自行接受或完成。',
  ].join('\n');
}

/** What the human sees when the turn starts; the prompt itself is unchanged. */
export function collabTurnNotice(r: CollabDeliveryRequest, displayName: AgentDisplayName = () => undefined): CollabTurnNotice {
  const name = (actor: string) => displayName(actor) ?? actor;
  return {
    role: r.role ?? 'recipient',
    contractId: r.contract_id,
    workItemId: r.work_item_id,
    kind: r.kind,
    status: r.status,
    ...(r.deadline ? { deadline: r.deadline } : {}),
    targetWorkItemId: r.target_work_item_id,
    ...(r.main_only !== undefined ? { mainOnly: r.main_only } : {}),
    ...(r.offered_by ? { offeredBy: name(r.offered_by) } : {}),
    ...(r.answered_by ? { answeredBy: r.answered_by.map(name) } : {}),
    ...(r.role === 'submitter' ? { pullRequest: pullRequestName(r) } : {}),
    ...(r.role === 'wake' && r.job_id ? { jobId: r.job_id } : {}),
  };
}

/** The configured name of the Agent whose Harness principal is `actor`. */
export function agentDisplayName(agents: Array<Pick<AgentConfig, 'id' | 'name' | 'harnessAgentId'>>, actor: string): string | undefined {
  const principal = (agent: Pick<AgentConfig, 'id' | 'harnessAgentId'>) => {
    try { return harnessPrincipalForAgent(agent); } catch { return undefined; }
  };
  return agents.find(agent => principal(agent) === actor)?.name || undefined;
}

export interface DeliveryConversation extends NativeIdSource { state: string }

export interface DeliveryChannel {
  /** Resume the existing conversation if needed and hand it one turn, fenced to `expectNativeIds`
   * under `deliveryRef` (internal-turn-fence.ts). Returns false when the channel does not own this
   * conversation or its native session changed. `notice` shapes the human-visible line; without it
   * a generic line is sent. */
  deliverInternalTurn(poolKey: string, prompt: string, expectNativeIds: string[], notice?: CollabTurnNotice,
    deliveryRef?: string): Promise<boolean>;
}

export interface CollabDeliveryDeps {
  /** Persisted conversations: pool key -> current native IDs. */
  sessions(): Record<string, NativeIdSource>;
  live(poolKey: string): DeliveryConversation | undefined;
  /** Harness principal of the agent a conversation belongs to, if known. */
  principalFor(poolKey: string): string | undefined;
  channelFor(poolKey: string): DeliveryChannel | undefined;
  /** Display name of a Harness actor; the actor ID is shown when undefined. */
  displayName?: AgentDisplayName;
  now?: () => number;
  log?: (event: Record<string, unknown>) => void;
  /** Ledger of delivered marks; without it the marks live only in memory. */
  ledgerPath?: string;
  /** Run the Agents' inbox gates again: deferred conversations became idle.
   * Without it a deferred item waits for the gate's schedule. */
  rerun?: (actors: string[]) => void;
}

interface LedgerEntry { mark?: string; sentAt?: number; seenAt: number }

/** Delivered marks per contract and role, kept across restarts. */
function createLedger(path: string | undefined, log: (event: Record<string, unknown>) => void) {
  const entries = new Map<string, LedgerEntry>();
  if (path) {
    try {
      const raw = JSON.parse(readFileSync(path, 'utf8')) as { schema?: unknown; entries?: Record<string, LedgerEntry> };
      if (raw.schema !== LEDGER_SCHEMA || !raw.entries || typeof raw.entries !== 'object') throw new Error('invalid ledger');
      for (const [key, entry] of Object.entries(raw.entries)) {
        if (entry && typeof entry.seenAt === 'number') entries.set(key, entry);
      }
    } catch (error) {
      if ((error as NodeJS.ErrnoException).code !== 'ENOENT') {
        log({ level: 'warn', msg: 'collaboration delivery ledger unreadable; starting empty', error: String(error) });
      }
    }
  }
  function save(): void {
    if (!path) return;
    try {
      mkdirSync(dirname(path), { recursive: true });
      const temporary = `${path}.${process.pid}.tmp`;
      writeFileSync(temporary, JSON.stringify({ schema: LEDGER_SCHEMA, entries: Object.fromEntries(entries) }));
      renameSync(temporary, path);
    } catch (error) {
      log({ level: 'error', msg: 'collaboration delivery ledger not written', error: String(error) });
    }
  }
  return {
    get: (key: string) => entries.get(key),
    /** Record a delivered mark for `key`, or that the gate still asks for a recorded
     * `key`; drop entries not asked for within the TTL and the oldest beyond the limit. */
    touch(key: string, now: number, sent?: string): void {
      const previous = entries.get(key);
      if (sent === undefined && previous === undefined) return;
      entries.delete(key);
      entries.set(key, sent !== undefined ? { mark: sent, sentAt: now, seenAt: now } : { ...previous, seenAt: now });
      for (const [k, entry] of entries) if (now - entry.seenAt > DELIVERY_LEDGER_TTL_MS) entries.delete(k);
      const oldest = [...entries].sort((a, b) => a[1].seenAt - b[1].seenAt);
      for (const [k] of oldest.slice(0, Math.max(0, entries.size - DELIVERY_LEDGER_LIMIT))) entries.delete(k);
      save();
    },
  };
}

/** Every native ID a conversation is known by, persisted or live; see conversationNativeIds. */
export const nativeIds = conversationNativeIds;

export function createCollabDelivery(deps: CollabDeliveryDeps) {
  const now = deps.now ?? Date.now;
  const log = deps.log ?? (event => console.log({ ts: new Date().toISOString(), module: 'collab-delivery', ...event }));
  /** Busy conversations owed a fresh gate run: one wait per Agent and conversation. */
  const waiting = new Map<string, { actor: string; poolKey: string; since: number }>();
  /** Wakes waiting for a busy conversation, one per wake key. */
  const queued = new Map<string, { poolKey: string; request: CollabDeliveryRequest; mark: string; since: number }>();
  const ledger = createLedger(deps.ledgerPath, log);
  /** Handed to a conversation, not yet started or refused there: not offered again meanwhile.
   *  The ledger records a delivery only once its turn starts in the routed conversation. */
  const handed = new Map<string, { mark: string; since: number; outcome?: InternalTurnOutcome; cancel: () => void }>();
  let timer: NodeJS.Timeout | undefined;

  function handedMark(id: string): string | undefined {
    const entry = handed.get(id);
    if (!entry) return undefined;
    if (now() - entry.since <= WAIT_LIMIT_MS) return entry.mark;
    entry.cancel();
    handed.delete(id);
    return undefined;
  }

  /** The one session a request goes to: the registering session of a pull request result, else the holder. */
  function sessionKey(r: CollabDeliveryRequest): string | undefined {
    return r.author_key ?? r.holder_key;
  }

  function matchesKey(poolKey: string, r: CollabDeliveryRequest, key: string): string[] | undefined {
    if (deps.principalFor(poolKey) !== r.actor) return undefined;
    // Only the IDs that are the routed session: the channel re-checks the conversation
    // against them, so a new session seen beside it mid-switch never passes.
    const ids = nativeIds(deps.sessions()[poolKey], deps.live(poolKey))
      .filter(id => holderKey(r.namespace, r.actor, id, r.surface) === key);
    return ids.length ? ids : undefined;
  }

  function matches(poolKey: string, r: CollabDeliveryRequest): string[] | undefined {
    const key = sessionKey(r);
    return key ? matchesKey(poolKey, r, key) : undefined;
  }

  function find(r: CollabDeliveryRequest): { poolKey: string; ids: string[] } | 'not_hosted' | 'ambiguous' {
    const key = sessionKey(r);
    if (!key) return 'not_hosted';
    const found = Object.keys(deps.sessions())
      .map(poolKey => ({ poolKey, ids: matchesKey(poolKey, r, key) }))
      .filter((m): m is { poolKey: string; ids: string[] } => m.ids !== undefined);
    if (found.length === 0) return 'not_hosted';
    return found.length === 1 ? found[0]! : 'ambiguous';
  }

  function busy(poolKey: string): boolean {
    const state = deps.live(poolKey)?.state;
    return state === 'busy' || state === 'starting';
  }

  /** Hand the turn over; true unless the channel or the runtime refused it. */
  async function dispatch(poolKey: string, ids: string[], r: CollabDeliveryRequest, mark: string): Promise<boolean> {
    const id = deliveryKey(r);
    const ref = randomUUID();
    handed.get(id)?.cancel();
    const entry: { mark: string; since: number; outcome?: InternalTurnOutcome; cancel: () => void } = {
      mark, since: now(), cancel: () => {} };
    entry.cancel = watchInternalTurn(ref, outcome => {
      entry.outcome = outcome;
      if (handed.get(id) === entry) handed.delete(id);
      if (outcome === 'consumed') ledger.touch(id, now(), mark);
      log({ level: outcome === 'consumed' ? 'info' : 'warn', contract: r.contract_id, msg: outcome === 'consumed'
        ? 'collaboration delivered'
        : 'collaboration delivery refused where the turn would start; the next gate run offers it again' });
    });
    handed.set(id, entry);
    let sent = false;
    try {
      sent = await deps.channelFor(poolKey)!.deliverInternalTurn(poolKey, collabPrompt(r), ids,
        collabTurnNotice(r, deps.displayName), ref);
    } finally {
      if (!sent) {
        entry.cancel();
        if (handed.get(id) === entry) handed.delete(id);
      }
    }
    if (!sent) log({ level: 'warn', msg: 'collaboration delivery refused by channel', contract: r.contract_id });
    return sent && entry.outcome !== 'refused';
  }

  /** Idle conversations: queued wakes go out, and the Agents waited on read the inbox again. */
  async function drain(): Promise<void> {
    for (const [key, entry] of [...queued]) {
      if (now() - entry.since > WAIT_LIMIT_MS) { queued.delete(key); continue; }
      if (busy(entry.poolKey)) continue;
      queued.delete(key);
      // The holder may have moved (rollover, another session) while queued.
      const ids = matches(entry.poolKey, entry.request);
      if (!ids || !deps.channelFor(entry.poolKey)) {
        log({ level: 'warn', msg: 'collaboration delivery dropped: holder no longer here', contract: entry.request.contract_id });
        continue;
      }
      await dispatch(entry.poolKey, ids, entry.request, entry.mark).catch(error =>
        log({ level: 'error', msg: 'collaboration delivery failed', contract: key, error: String(error) }));
    }
    const actors = new Set<string>();
    for (const [key, entry] of [...waiting]) {
      if (now() - entry.since > WAIT_LIMIT_MS) { waiting.delete(key); continue; }
      if (busy(entry.poolKey)) continue;
      waiting.delete(key);
      actors.add(entry.actor);
    }
    if (actors.size > 0) {
      log({ level: 'info', msg: 'collaboration inbox asked again after a busy conversation became idle', actors: [...actors] });
      deps.rerun?.([...actors]);
    }
    if (waiting.size === 0 && queued.size === 0 && timer) { clearInterval(timer); timer = undefined; }
  }

  return {
    async deliver(r: CollabDeliveryRequest): Promise<CollabDeliveryStatus> {
      const id = deliveryKey(r);
      const mark = deliveryMark(r, now());
      ledger.touch(id, now());
      if (mark === undefined) return 'suppressed';
      if (queued.get(id)?.mark === mark || ledger.get(id)?.mark === mark || handedMark(id) === mark) return 'duplicate';
      const target = find(r);
      if (typeof target === 'string') return target;
      if (!deps.channelFor(target.poolKey)) return 'not_hosted';
      if (busy(target.poolKey) && r.role === 'wake') {
        queued.set(id, { poolKey: target.poolKey, request: r, mark, since: now() });
        timer ??= setInterval(() => { void drain(); }, POLL_MS);
        timer.unref?.();
        return 'queued';
      }
      if (busy(target.poolKey)) {
        const key = `${r.actor}\0${target.poolKey}`;
        if (!waiting.has(key)) waiting.set(key, { actor: r.actor, poolKey: target.poolKey, since: now() });
        timer ??= setInterval(() => { void drain(); }, POLL_MS);
        timer.unref?.();
        return 'deferred';
      }
      return await dispatch(target.poolKey, target.ids, r, mark) ? 'started' : 'not_hosted';
    },
    /** Call when a conversation may have become idle (turn result). */
    drain,
    stop(): void { if (timer) clearInterval(timer); timer = undefined; },
  };
}

export type CollabDelivery = ReturnType<typeof createCollabDelivery>;
