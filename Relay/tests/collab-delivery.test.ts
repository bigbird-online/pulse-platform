import { mkdtempSync, readFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { describe, expect, it, vi } from 'vitest';
import {
  COLLAB_DELIVERY_SCHEMA, DEADLINE_REMINDER_MS, DELIVERY_LEDGER_LIMIT, DELIVERY_LEDGER_TTL_MS,
  agentDisplayName, collabPrompt, createCollabDelivery, holderKey, parseDeliveryRequest,
  type CollabDeliveryDeps, type CollabDeliveryRequest, type DeliveryConversation,
} from '../src/collab-delivery.js';
import { fencedInternalTurn, settleInternalTurn } from '../src/internal-turn-fence.js';

/** What the runtime does where the turn starts: settle the delivery under its reference. */
function settle(ref: string | undefined, outcome: 'consumed' | 'refused'): void {
  if (ref) settleInternalTurn(fencedInternalTurn({ type: 'human' }, [], ref), outcome);
}

// Computed by the kernel: collaboration_inbox.holder_key(native_session_ref('team','hestia','native-1','ownermac')).
const KERNEL_KEY = 'f549314eaf0c2daafdda1c0970903af72098d8ff48c9e2ec35957233a5d2af38';

function request(nativeId = 'native-1', extra: Partial<CollabDeliveryRequest> = {}): CollabDeliveryRequest {
  return {
    actor: 'hestia', namespace: 'team', surface: 'ownermac',
    holder_key: holderKey('team', 'hestia', nativeId, 'ownermac'),
    contract_id: 'op_1', work_item_id: 'wi_20261024_001', target_work_item_id: 'wi_20261024_002',
    kind: 'request', status: 'pending', ...extra,
  };
}

function rig(sessions: Record<string, { sessionId?: string; codexThreadId?: string;
  agyConversation?: { sessionId: string; conversationId: string } }>,
  live: Record<string, DeliveryConversation> = {}, principals: Record<string, string> = {},
  extra: Partial<CollabDeliveryDeps> = {}) {
  // The turn starts in the routed conversation at once, as an idle runtime does.
  const deliverInternalTurn = vi.fn(async (_poolKey: string, _prompt: string, _ids: string[], _notice?: unknown, ref?: string) => {
    settle(ref, 'consumed');
    return true;
  });
  const deps: CollabDeliveryDeps = {
    sessions: () => sessions,
    live: poolKey => live[poolKey],
    principalFor: poolKey => principals[poolKey] ?? poolKey.split(':')[1],
    channelFor: () => ({ deliverInternalTurn }),
    displayName: actor => ({ amanda: 'Amanda', pulse: 'Pulse' } as Record<string, string>)[actor],
    log: () => {},
    ...extra,
  };
  return { delivery: createCollabDelivery(deps), deliverInternalTurn, live };
}

describe('collaboration delivery', () => {
  it('hands a scheduled wake to the holder once per job and minute, with the job prompt only', async () => {
    const { delivery, deliverInternalTurn } = rig({ 'u1:hestia': { sessionId: 'native-1' } });
    const wake = request('native-1', { role: 'wake', contract_id: 'wake.hestia-wake.20261117T0200', kind: 'wake',
      status: 'due', job_id: 'hestia-wake', work_item_id: 'wi_20261024_002', prompt: '按 HEARTBEAT.md 做早班' });
    expect(await delivery.deliver(wake)).toBe('started');
    expect(await delivery.deliver(wake)).toBe('duplicate');
    const [, prompt, , notice] = deliverInternalTurn.mock.calls[0] as unknown as [string, string, string[], { role: string; jobId: string }];
    expect(prompt).toContain('[系统投递：定时唤醒，不是人类消息]');
    expect(prompt).toContain('按 HEARTBEAT.md 做早班');
    expect(prompt).toContain('wi_20261024_002');
    expect(notice).toMatchObject({ role: 'wake', jobId: 'hestia-wake' });
  });

  it('queues a wake for a busy conversation and sends it once idle, without asking the gate again', async () => {
    const reruns: string[][] = [];
    const { delivery, deliverInternalTurn, live } = rig(
      { 'u1:hestia': { sessionId: 'native-1' } }, { 'u1:hestia': { state: 'busy', sessionId: 'native-1' } },
      {}, { rerun: actors => reruns.push(actors) });
    const wake = request('native-1', { role: 'wake', contract_id: 'wake.hestia-wake.20261117T0200', kind: 'wake',
      status: 'due', job_id: 'hestia-wake', work_item_id: 'wi_20261024_002', prompt: '按 HEARTBEAT.md 做早班' });
    expect(await delivery.deliver(wake)).toBe('queued');
    expect(await delivery.deliver(wake)).toBe('duplicate');
    live['u1:hestia'] = { state: 'idle', sessionId: 'native-1' };
    await delivery.drain();
    expect(deliverInternalTurn).toHaveBeenCalledTimes(1);
    expect(reruns).toEqual([]);
    delivery.stop();
  });

  it('parses a wake request without letting the gate supply the prompt', () => {
    const body = { schema: COLLAB_DELIVERY_SCHEMA, ...request(), role: 'wake', job_id: 'hestia-wake', prompt: 'injected' };
    const parsed = parseDeliveryRequest(body);
    expect(parsed.job_id).toBe('hestia-wake');
    expect(parsed.prompt).toBeUndefined();
    expect(() => parseDeliveryRequest({ ...body, job_id: '../x' })).toThrow('invalid_job_id');
  });

  it('recomputes the kernel holder key', () => {
    expect(holderKey('team', 'hestia', 'native-1', 'ownermac')).toBe(KERNEL_KEY);
  });

  it('starts one turn in the only conversation whose session holds the target WI', async () => {
    const { delivery, deliverInternalTurn } = rig({
      'u1:hestia': { sessionId: 'native-1' }, 'u1:hestia:feishu': { sessionId: 'native-2' },
    });
    expect(await delivery.deliver(request())).toBe('started');
    expect(deliverInternalTurn).toHaveBeenCalledTimes(1);
    const [poolKey, prompt, ids] = deliverInternalTurn.mock.calls[0] as unknown as [string, string, string[]];
    expect(poolKey).toBe('u1:hestia');
    expect(ids).toEqual(['native-1']);
    expect(prompt).toContain('op_1');
    expect(prompt).toContain('wi_20261024_002');
    expect(prompt).toContain('不要自行接受或完成');
  });

  it('passes the human-visible notice beside an unchanged prompt', async () => {
    const { delivery, deliverInternalTurn } = rig({ 'u1:hestia': { sessionId: 'native-1' } });
    const r = request('native-1', { offered_by: 'amanda', main_only: true, deadline: '2026-10-30' });
    expect(await delivery.deliver(r)).toBe('started');
    const [, prompt, , notice] = deliverInternalTurn.mock.calls[0] as unknown as [string, string, string[], unknown];
    expect(prompt).toBe(collabPrompt(r));
    expect(notice).toEqual({
      role: 'recipient', contractId: 'op_1', workItemId: 'wi_20261024_001', kind: 'request', status: 'pending',
      deadline: '2026-10-30', targetWorkItemId: 'wi_20261024_002', mainOnly: true, offeredBy: 'Amanda',
    });
    const settle = request('native-1', { role: 'requester', status: 'completed', offered_by: 'hestia',
      answered_by: ['pulse', 'aki'], options: ['adopt'] });
    await delivery.deliver(settle);
    expect((deliverInternalTurn.mock.calls[1] as unknown as unknown[])[3])
      .toEqual(expect.objectContaining({ role: 'requester', offeredBy: 'hestia', answeredBy: ['Pulse', 'aki'] }));
  });

  it('names an actor by its configured Agent name, falling back to nothing', () => {
    const agents = [
      { id: 'amanda', name: 'Amanda' },
      { id: 'hestia-bot', name: 'Hestia', harnessAgentId: 'hestia' },
      { id: 'blank', name: '' },
    ];
    expect(agentDisplayName(agents, 'amanda')).toBe('Amanda');
    expect(agentDisplayName(agents, 'hestia')).toBe('Hestia');
    expect(agentDisplayName(agents, 'hestia-bot')).toBeUndefined();
    expect(agentDisplayName(agents, 'blank')).toBeUndefined();
    expect(agentDisplayName(agents, 'nobody')).toBeUndefined();
  });

  it('matches a Codex conversation by its thread id', async () => {
    const { delivery } = rig({ 'u1:hestia': { sessionId: 'relay-session', codexThreadId: 'thread-9' } });
    expect(await delivery.deliver(request('thread-9'))).toBe('started');
  });

  it('matches an agy conversation by the conversation agy named, saved or live', async () => {
    // The core names an agy session by its agy conversation; Relay's session ID is not that.
    const saved = { 'u1:asobi': { sessionId: 'relay-session', agyConversation: { sessionId: 'relay-session', conversationId: 'conv-1' } } };
    const r = (candidate: string, id: string) => request(id, { actor: 'asobi', holder_key: undefined,
      author_key: holderKey('team', 'asobi', id, 'ownermac'), role: 'submitter', contract_id: candidate,
      kind: 'pull_request', status: 'changes_requested', repository: 'o/r', number: 1013, head_sha: 'a'.repeat(40) });
    const first = rig(saved);
    expect(await first.delivery.deliver(r('op_r1', 'conv-1'))).toBe('started');
    expect((first.deliverInternalTurn.mock.calls[0] as unknown as unknown[])[2]).toEqual(['conv-1']);
    // A contract for the WI the agy session holds goes to the same conversation.
    expect(await first.delivery.deliver(request('conv-1', { actor: 'asobi',
      holder_key: holderKey('team', 'asobi', 'conv-1', 'ownermac') }))).toBe('started');
    // The live process has named a newer conversation than the one saved.
    const live = rig(saved, { 'u1:asobi': { state: 'idle', sessionId: 'relay-session', agyConversationId: 'conv-2' } });
    expect(await live.delivery.deliver(r('op_r2', 'conv-2'))).toBe('started');
    expect((live.deliverInternalTurn.mock.calls[0] as unknown as unknown[])[2]).toEqual(['conv-2']);
    // A conversation saved for another Relay session does not name this one.
    const stale = rig({ 'u1:asobi': { sessionId: 'relay-new', agyConversation: { sessionId: 'relay-session', conversationId: 'conv-1' } } });
    expect(await stale.delivery.deliver(r('op_r3', 'conv-1'))).toBe('not_hosted');
    for (const x of [first, live, stale]) x.delivery.stop();
  });

  it('records a delivery only once its turn starts in the routed conversation, and offers a refused one again', async () => {
    const ledgerPath = join(mkdtempSync(join(tmpdir(), 'collab-ledger-')), 'ledger.json');
    const refs: string[] = [];
    const { delivery, deliverInternalTurn } = rig({ 'u1:hestia': { sessionId: 'native-1' } }, {}, {}, { ledgerPath });
    deliverInternalTurn.mockImplementation(async (_p: string, _pr: string, _i: string[], _n?: unknown, ref?: string) => {
      refs.push(ref!);
      return true;  // handed to the runtime; it settles later, where the turn starts
    });
    const recorded = () => { try { return JSON.parse(readFileSync(ledgerPath, 'utf8')).entries; } catch { return {}; } };
    expect(await delivery.deliver(request())).toBe('started');
    // Handed over and not yet started: not offered again meanwhile, and not recorded.
    expect(await delivery.deliver(request())).toBe('duplicate');
    expect(Object.values(recorded())).toEqual([]);
    // The conversation changed before the turn started: refused there, still unrecorded, offered again.
    settle(refs[0], 'refused');
    expect(Object.values(recorded())).toEqual([]);
    expect(await delivery.deliver(request())).toBe('started');
    expect(refs).toHaveLength(2);
    expect(refs[1]).not.toBe(refs[0]);
    settle(refs[1], 'consumed');
    expect(Object.values(recorded())).toHaveLength(1);
    expect(await delivery.deliver(request())).toBe('duplicate');
    // A refusal before the channel returns is no delivery either.
    const quick = rig({ 'u1:hestia': { sessionId: 'native-1' } });
    quick.deliverInternalTurn.mockImplementation(async (_p: string, _pr: string, _i: string[], _n?: unknown, ref?: string) => {
      settle(ref, 'refused');
      return true;
    });
    expect(await quick.delivery.deliver(request())).toBe('not_hosted');
    delivery.stop(); quick.delivery.stop();
  });

  it('delivers nothing when no conversation, another agent, or several conversations match', async () => {
    expect(await rig({ 'u1:hestia': { sessionId: 'native-2' } }).delivery.deliver(request())).toBe('not_hosted');
    const other = rig({ 'u1:pulse': { sessionId: 'native-1' } });
    expect(await other.delivery.deliver(request())).toBe('not_hosted');
    expect(other.deliverInternalTurn).not.toHaveBeenCalled();
    const two = rig({ 'u1:hestia': { sessionId: 'native-1' }, 'u2:hestia': { sessionId: 'native-1' } });
    expect(await two.delivery.deliver(request())).toBe('ambiguous');
    expect(two.deliverInternalTurn).not.toHaveBeenCalled();
  });

  it('defers a busy conversation and asks the gate again once it is idle, sending nothing it read before', async () => {
    const reruns: string[][] = [];
    const { delivery, deliverInternalTurn, live } = rig(
      { 'u1:hestia': { sessionId: 'native-1' } }, { 'u1:hestia': { state: 'busy', sessionId: 'native-1' } },
      {}, { rerun: actors => reruns.push(actors) });
    // The requester settles in the busy turn; the snapshot read before it must not arrive afterwards.
    const settle = request('native-1', { role: 'requester', status: 'completed', answered_by: ['pulse'], options: ['adopt'] });
    expect(await delivery.deliver(settle)).toBe('deferred');
    expect(await delivery.deliver(request())).toBe('deferred');
    await delivery.drain();
    expect(reruns).toEqual([]);
    live['u1:hestia'] = { state: 'idle', sessionId: 'native-1' };
    await delivery.drain();
    // One fresh gate run per Agent; the turn itself was never kept.
    expect(reruns).toEqual([['hestia']]);
    expect(deliverInternalTurn).not.toHaveBeenCalled();
    await delivery.drain();
    expect(reruns).toHaveLength(1);
    // The fresh gate run no longer lists the settled contract; the pending one is still due.
    expect(await delivery.deliver(request())).toBe('started');
    expect(deliverInternalTurn).toHaveBeenCalledTimes(1);
    expect(await delivery.deliver(request())).toBe('duplicate');
    delivery.stop();
  });

  it('asks again for a pull request result only when the gate reads it again', async () => {
    const reruns: string[][] = [];
    const { delivery, deliverInternalTurn, live } = rig(
      { 'u1:pulse': { sessionId: 'native-1' } }, { 'u1:pulse': { state: 'busy', sessionId: 'native-1' } },
      {}, { rerun: actors => reruns.push(actors) });
    const round = (candidate: string, status: string) => request('native-1', {
      actor: 'pulse', holder_key: holderKey('team', 'pulse', 'native-1', 'ownermac'), role: 'submitter',
      contract_id: candidate, kind: 'pull_request', status, repository: 'o/r', number: 7, head_sha: 'a'.repeat(40) });
    expect(await delivery.deliver(round('op_r2', 'changes_requested'))).toBe('deferred');
    live['u1:pulse'] = { state: 'idle', sessionId: 'native-1' };
    await delivery.drain();
    expect(reruns).toEqual([['pulse']]);
    // Round 3 was registered in the busy turn: the gate now reads only its result.
    expect(await delivery.deliver(round('op_r3', 'merged'))).toBe('started');
    const [, prompt] = deliverInternalTurn.mock.calls[0] as unknown as [string, string];
    expect(prompt).toContain('已由内核合并');
    expect(deliverInternalTurn).toHaveBeenCalledTimes(1);
    delivery.stop();
  });

  it('delivers a pull request result only to the session that registered its head', async () => {
    const sessions: Record<string, { sessionId?: string }> = {
      'u1:pulse': { sessionId: 'holder-1' }, 'u1:pulse:feishu': { sessionId: 'author-1' } };
    const { delivery, deliverInternalTurn } = rig(sessions);
    const round = (candidate: string, extra: Partial<CollabDeliveryRequest> = {}) => request('holder-1', {
      actor: 'pulse', holder_key: undefined,
      author_key: holderKey('team', 'pulse', 'author-1', 'ownermac'), role: 'submitter',
      contract_id: candidate, kind: 'pull_request', status: 'merged', repository: 'o/r', number: 7,
      head_sha: 'a'.repeat(40), ...extra });
    expect(await delivery.deliver(round('op_r1'))).toBe('started');
    expect((deliverInternalTurn.mock.calls[0] as unknown as string[])[0]).toBe('u1:pulse:feishu');
    // The registering session is not here any more: the holder here does not get it.
    sessions['u1:pulse:feishu'] = { sessionId: 'author-2' };
    expect(await delivery.deliver(round('op_r2', { holder_key: holderKey('team', 'pulse', 'holder-1', 'ownermac') })))
      .toBe('not_hosted');
    // The kernel says the registering session ended: the gate names the holder alone.
    expect(await delivery.deliver(round('op_r3', { author_key: undefined,
      holder_key: holderKey('team', 'pulse', 'holder-1', 'ownermac') }))).toBe('started');
    expect((deliverInternalTurn.mock.calls[1] as unknown as string[])[0]).toBe('u1:pulse');
    expect(deliverInternalTurn).toHaveBeenCalledTimes(2);
    delivery.stop();
  });

  it('hands the channel only the routed session ID while a switch shows the old and new beside each other', async () => {
    // The switch persists the new session before the old CC process exits.
    const { delivery, deliverInternalTurn } = rig(
      { 'u1:pulse': { sessionId: 'author-new' } }, { 'u1:pulse': { state: 'idle', sessionId: 'author-1' } });
    const r = (candidate: string, id: string) => request('author-1', {
      actor: 'pulse', holder_key: undefined, author_key: holderKey('team', 'pulse', id, 'ownermac'), role: 'submitter',
      contract_id: candidate, kind: 'pull_request', status: 'merged', repository: 'o/r', number: 7, head_sha: 'a'.repeat(40) });
    expect(await delivery.deliver(r('op_r1', 'author-1'))).toBe('started');
    expect((deliverInternalTurn.mock.calls[0] as unknown as unknown[])[2]).toEqual(['author-1']);
    expect(await delivery.deliver(r('op_r2', 'author-new'))).toBe('started');
    expect((deliverInternalTurn.mock.calls[1] as unknown as unknown[])[2]).toEqual(['author-new']);
    delivery.stop();
  });

  it('defers a result for a busy registering session instead of handing it to the holder', async () => {
    const { delivery, deliverInternalTurn } = rig(
      { 'u1:pulse': { sessionId: 'holder-1' }, 'u1:pulse:feishu': { sessionId: 'author-1' } },
      { 'u1:pulse:feishu': { state: 'busy', sessionId: 'author-1' } });
    const r = request('holder-1', {
      actor: 'pulse', holder_key: holderKey('team', 'pulse', 'holder-1', 'ownermac'),
      author_key: holderKey('team', 'pulse', 'author-1', 'ownermac'), role: 'submitter',
      contract_id: 'op_r1', kind: 'pull_request', status: 'merged', repository: 'o/r', number: 7, head_sha: 'a'.repeat(40) });
    expect(await delivery.deliver(r)).toBe('deferred');
    expect(deliverInternalTurn).not.toHaveBeenCalled();
    delivery.stop();
  });

  it('parses the registering session key only for a pull request result', () => {
    const body = { schema: COLLAB_DELIVERY_SCHEMA, ...request(), role: 'submitter', kind: 'pull_request',
      status: 'merged', repository: 'o/r', number: 7, head_sha: 'a'.repeat(40), author_key: 'c'.repeat(64) };
    expect(parseDeliveryRequest(body).author_key).toBe('c'.repeat(64));
    const { holder_key: _held, ...unheld } = body;
    expect(parseDeliveryRequest(unheld)).not.toHaveProperty('holder_key');
    expect(() => parseDeliveryRequest({ ...unheld, author_key: undefined })).toThrow('invalid_holder_key');
    expect(() => parseDeliveryRequest({ ...body, author_key: 'short' })).toThrow('invalid_author_key');
    expect(() => parseDeliveryRequest({ ...body, role: 'recipient', kind: 'request', status: 'pending' })).toThrow('invalid_author_key');
  });

  it('delivers one contract to each of its recipients hosted by the same Relay', async () => {
    const { delivery, deliverInternalTurn } = rig(
      { 'u1:hestia': { sessionId: 'native-1' }, 'u1:aki': { sessionId: 'native-2' } });
    expect(await delivery.deliver(request())).toBe('started');
    const aki = request('native-2', { actor: 'aki', holder_key: holderKey('team', 'aki', 'native-2', 'ownermac') });
    expect(await delivery.deliver(aki)).toBe('started');
    expect(await delivery.deliver(aki)).toBe('duplicate');
    expect(deliverInternalTurn).toHaveBeenCalledTimes(2);
    delivery.stop();
  });

  it('leaves a conversation waited on past the limit to the schedule, and one that moved', async () => {
    let clock = 0;
    const reruns: string[][] = [];
    const sessions = { 'u1:hestia': { sessionId: 'native-1' } };
    const { delivery, live } = rig(sessions, { 'u1:hestia': { state: 'busy', sessionId: 'native-1' } },
      {}, { rerun: actors => reruns.push(actors), now: () => clock });
    expect(await delivery.deliver(request())).toBe('deferred');
    clock += 2 * 60 * 60 * 1000 + 1;
    live['u1:hestia'] = { state: 'idle', sessionId: 'native-1' };
    await delivery.drain();
    expect(reruns).toEqual([]);
    // The holder session moved: the fresh request matches no conversation.
    sessions['u1:hestia'] = { sessionId: 'native-3' };
    live['u1:hestia'] = { state: 'idle', sessionId: 'native-3' };
    expect(await delivery.deliver(request())).toBe('not_hosted');
    delivery.stop();
  });

  it('accepts only the published request schema', () => {
    const body = { schema: COLLAB_DELIVERY_SCHEMA, ...request() };
    expect(parseDeliveryRequest(body).contract_id).toBe('op_1');
    expect(() => parseDeliveryRequest({ ...body, schema: 'other' })).toThrow('invalid_schema');
    expect(() => parseDeliveryRequest({ ...body, holder_key: 'short' })).toThrow('invalid_holder_key');
    expect(() => parseDeliveryRequest({ ...body, actor: 'bad actor' })).toThrow('invalid_actor');
    expect(parseDeliveryRequest(body).role).toBe('recipient');
    expect(() => parseDeliveryRequest({ ...body, role: 'judge' })).toThrow('invalid_role');
    expect(() => parseDeliveryRequest({ ...body, main_only: 'yes' })).toThrow('invalid_main_only');
    expect(parseDeliveryRequest({ ...body, offered_by: 'amanda' }).offered_by).toBe('amanda');
    expect(parseDeliveryRequest(body)).not.toHaveProperty('offered_by');
    expect(() => parseDeliveryRequest({ ...body, offered_by: 'bad actor' })).toThrow('invalid_offered_by');
    expect(() => parseDeliveryRequest({ ...body, role: 'requester', options: ['adopt'] })).toThrow('invalid_answered_by');
    expect(() => parseDeliveryRequest({ ...body, role: 'requester', answered_by: ['pulse'], options: ['Adopt!'] }))
      .toThrow('invalid_options');
  });

  it('tells the session holding the WI that the kernel merged its pull request, once per round and status', async () => {
    const { delivery, deliverInternalTurn } = rig({ 'u1:hestia': { sessionId: 'native-1' } });
    const merged = request('native-1', {
      role: 'submitter', contract_id: 'op_r1', kind: 'pull_request', status: 'merged',
      target_work_item_id: 'wi_20261024_001', repository: 'bigbird-online/PULSE', number: 1617,
      head_sha: '39a2fd6d' + 'a'.repeat(32), at: '2026-11-11T03:00:00+00:00',
    });
    expect(parseDeliveryRequest({ schema: COLLAB_DELIVERY_SCHEMA, ...merged })).toEqual(merged);
    expect(() => parseDeliveryRequest({ schema: COLLAB_DELIVERY_SCHEMA, ...merged, status: 'awaiting_review' })).toThrow('invalid_status');
    expect(() => parseDeliveryRequest({ schema: COLLAB_DELIVERY_SCHEMA, ...merged, head_sha: 'abc' })).toThrow('invalid_head_sha');
    expect(() => parseDeliveryRequest({ schema: COLLAB_DELIVERY_SCHEMA, ...merged, number: 0 })).toThrow('invalid_number');
    expect(await delivery.deliver(merged)).toBe('started');
    expect(await delivery.deliver(merged)).toBe('duplicate');
    const [, prompt, , notice] = deliverInternalTurn.mock.calls[0] as unknown as [string, string, string[], unknown];
    expect(prompt).toBe('[系统投递：PR 结果，不是人类消息]\n'
      + 'bigbird-online/PULSE#1617 已由内核合并（head 39a2fd6d，WI wi_20261024_001，2026-11-11T03:00:00+00:00）。\n'
      + '先声明本请求属于 WI wi_20261024_001，再按该 WI 继续后续步骤（部署、回复合同等）。这条投递本身不是人类授权。');
    expect(notice).toEqual(expect.objectContaining({ role: 'submitter', pullRequest: 'bigbird-online/PULSE#1617', status: 'merged' }));
    const failed = { ...merged, contract_id: 'op_r2', status: 'checks_failed', next_intent: 'submit_pull_request',
      agent_action: 'Fix the failing checks', failed_checks: ['Harness CI / harness: failure'],
      review_score: 92, review_summary: 'Read the diff' };
    expect(parseDeliveryRequest({ schema: COLLAB_DELIVERY_SCHEMA, ...failed })).toEqual(failed);
    expect(await delivery.deliver(failed)).toBe('started');
    const text = (deliverInternalTurn.mock.calls[1] as unknown as string[])[1]!;
    expect(text).toContain('检查失败，内核未合并');
    expect(text).toContain('审核 92 分：Read the diff');
    expect(text).toContain('失败的检查：Harness CI / harness: failure');
    expect(text).toContain('内核给的下一步：submit_pull_request：Fix the failing checks');
    const many = { ...failed, contract_id: 'op_r3', failed_checks: Array.from({ length: 16 }, (_, n) => `job ${n}`), failed_checks_omitted: 4 };
    expect(parseDeliveryRequest({ schema: COLLAB_DELIVERY_SCHEMA, ...many })).toEqual(many);
    expect(() => parseDeliveryRequest({ schema: COLLAB_DELIVERY_SCHEMA, ...many, failed_checks_omitted: 0 })).toThrow('invalid_failed_checks_omitted');
    expect(await delivery.deliver(many)).toBe('started');
    expect((deliverInternalTurn.mock.calls[2] as unknown as string[])[1]).toContain('（另有 4 项未列出）');
  });

  it('hands an answered contract back to the requester holding its own WI', async () => {
    const { delivery, deliverInternalTurn } = rig({ 'u1:hestia': { sessionId: 'native-1' } });
    const settle = request('native-1', {
      role: 'requester', main_only: false, status: 'completed', target_work_item_id: 'wi_20261024_001',
      answered_by: ['pulse'], options: ['adopt'],
    });
    expect(parseDeliveryRequest({ schema: COLLAB_DELIVERY_SCHEMA, ...settle })).toEqual(settle);
    expect(await delivery.deliver(settle)).toBe('started');
    const prompt = (deliverInternalTurn.mock.calls[0] as unknown as [string, string])[1];
    expect(prompt).toContain('pulse');
    expect(prompt).toContain('completed');
    expect(prompt).toContain('withdraw_collaboration');
    expect(prompt).not.toContain('respond_collaboration');
    expect(prompt).toContain('直接结清');
    const main = collabPrompt({ ...settle, main_only: true });
    expect(main).toContain('由人类决定后再结清');
  });

  it('delivers the recipient and requester side of one contract separately', async () => {
    const { delivery, deliverInternalTurn } = rig({ 'u1:hestia': { sessionId: 'native-1' } });
    expect(await delivery.deliver(request())).toBe('started');
    const settle = request('native-1', { role: 'requester', status: 'returned', answered_by: ['pulse'], options: ['resend'] });
    expect(await delivery.deliver(settle)).toBe('started');
    expect(await delivery.deliver(settle)).toBe('duplicate');
    expect(deliverInternalTurn).toHaveBeenCalledTimes(2);
  });

  describe('once per contract, role and status', () => {
    const HOUR = 60 * 60 * 1000;
    const T0 = Date.parse('2026-10-31T00:00:00Z');
    function persisted(clock: { t: number }, ledgerPath = join(mkdtempSync(join(tmpdir(), 'collab-ledger-')), 'ledger.json')) {
      const sessions = { 'u1:hestia': { sessionId: 'native-1' } };
      const make = () => rig(sessions, {}, {}, { now: () => clock.t, ledgerPath });
      return { make, ledgerPath };
    }

    it('stays a duplicate after the old window and across a Relay restart', async () => {
      const clock = { t: T0 };
      const { make } = persisted(clock);
      const first = make();
      expect(await first.delivery.deliver(request())).toBe('started');
      clock.t += 3 * HOUR;
      expect(await first.delivery.deliver(request())).toBe('duplicate');
      const restarted = make();
      expect(await restarted.delivery.deliver(request())).toBe('duplicate');
      expect(first.deliverInternalTurn).toHaveBeenCalledTimes(1);
      expect(restarted.deliverInternalTurn).not.toHaveBeenCalled();
    });

    it('delivers again only when the status changes', async () => {
      const clock = { t: T0 };
      const { make } = persisted(clock);
      const { delivery, deliverInternalTurn } = make();
      const settle = (status: string) => request('native-1', {
        role: 'requester', status, answered_by: ['pulse'], options: ['resend'] });
      expect(await delivery.deliver(settle('returned'))).toBe('started');
      expect(await delivery.deliver(settle('returned'))).toBe('duplicate');
      expect(await delivery.deliver(settle('completed'))).toBe('started');
      expect(await make().delivery.deliver(settle('completed'))).toBe('duplicate');
      expect(deliverInternalTurn).toHaveBeenCalledTimes(2);
    });

    it('does not push an accepted recipient contract without a deadline', async () => {
      const clock = { t: T0 };
      const { make } = persisted(clock);
      const { delivery, deliverInternalTurn } = make();
      expect(await delivery.deliver(request('native-1', { status: 'accepted', main_only: true }))).toBe('suppressed');
      clock.t += 30 * 24 * HOUR;
      expect(await delivery.deliver(request('native-1', { status: 'accepted', main_only: true }))).toBe('suppressed');
      expect(deliverInternalTurn).not.toHaveBeenCalled();
    });

    it('reminds an accepted recipient contract once when its deadline is near', async () => {
      const clock = { t: T0 };
      const { make } = persisted(clock);
      const { delivery, deliverInternalTurn } = make();
      const accepted = request('native-1', { status: 'accepted', main_only: true, deadline: '2026-11-03T00:00:00Z' });
      expect(await delivery.deliver(accepted)).toBe('suppressed');
      clock.t = Date.parse(accepted.deadline!) - DEADLINE_REMINDER_MS + HOUR;
      expect(await delivery.deliver(accepted)).toBe('started');
      expect(await delivery.deliver(accepted)).toBe('duplicate');
      clock.t += 6 * HOUR;
      expect(await make().delivery.deliver(accepted)).toBe('duplicate');
      expect(deliverInternalTurn).toHaveBeenCalledTimes(1);
      const prompt = (deliverInternalTurn.mock.calls[0] as unknown as [string, string])[1];
      expect(prompt).toContain('已接受');
      expect(prompt).toContain('临近截止');
      expect(prompt).toContain('complete');
      expect(prompt).toContain('return');
      expect(prompt).not.toContain('由人类决定后再用 respond_collaboration 答复');
    });

    it('keeps the pending prompt asking the human, neutrally', () => {
      const prompt = collabPrompt(request('native-1', { main_only: true }));
      expect(prompt).toContain('向人类说明合同内容和你的建议，由人类决定后再用 respond_collaboration 答复');
    });

    it('forgets records no longer asked for and bounds the ledger', async () => {
      const clock = { t: T0 };
      const { make, ledgerPath } = persisted(clock);
      const { delivery } = make();
      expect(await delivery.deliver(request('native-1', { contract_id: 'op_old' }))).toBe('started');
      clock.t += DELIVERY_LEDGER_TTL_MS / 2;
      expect(await delivery.deliver(request('native-1', { contract_id: 'op_kept' }))).toBe('started');
      clock.t += DELIVERY_LEDGER_TTL_MS / 2 + HOUR;
      // A duplicate refreshes the record of a contract the gate still asks for.
      expect(await delivery.deliver(request('native-1', { contract_id: 'op_kept' }))).toBe('duplicate');
      const keys = () => Object.keys((JSON.parse(readFileSync(ledgerPath, 'utf8')) as { entries: object }).entries);
      expect(keys()).toEqual(['team:hestia:recipient:op_kept']);
      expect(await make().delivery.deliver(request('native-1', { contract_id: 'op_old' }))).toBe('started');

      const bounded = make();
      for (let i = 0; i < DELIVERY_LEDGER_LIMIT + 5; i++) {
        clock.t += 1000;
        await bounded.delivery.deliver(request('native-1', { contract_id: `op_n${i}` }));
      }
      expect(keys()).toHaveLength(DELIVERY_LEDGER_LIMIT);
      expect(keys()).not.toContain('team:hestia:recipient:op_n0');
      expect(keys()).toContain(`team:hestia:recipient:op_n${DELIVERY_LEDGER_LIMIT + 4}`);
    });
  });
});
