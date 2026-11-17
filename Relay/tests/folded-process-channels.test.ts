import { toolDisclosureKey } from '../src/work-process-content.js';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { createTelegramRenderingSink } from '../src/channels/telegram.js';
import { createFeishuStreamingSink } from '../src/channels/feishu.js';
import { RUNTIME_NOTIFICATION_REPLY_LEAD, TURN_GENERATED_STATUS_TEXT } from '../src/channel-rendering.js';
import { RUNTIME_NOTIFICATION_TURN, tagRuntimeNotificationTurn } from '../src/runtime-background-work.js';

const key = 'owner:pulse';
const progress = '⏳ 仍在工作，已推进 5 个执行步骤…';
const final = '这是本轮的独立最终答复。';
const agent: any = { id: 'pulse', name: 'Pulse', emoji: '⚡️', channel: 'feishu',
  workspace: '/tmp/ws', sessionId: 'session', feishu: { appId: 'fold-test', appSecret: 'test' } };

beforeEach(() => {
  vi.stubEnv('RELAY_WORK_PROCESS_DETAILS', 'on');
  vi.stubEnv('RELAY_FEISHU_STREAMING_CARD', 'true');
  vi.stubGlobal('fetch', vi.fn(async () => new Response(JSON.stringify({ code: 0,
    tenant_access_token: 'test', expire: 7200, data: { card_id: 'card' } }))));
});
afterEach(() => { vi.unstubAllEnvs(); vi.unstubAllGlobals(); });

function telegram() {
  const api = { sendMessage: vi.fn(async () => ({ message_id: 2 })),
    editMessageText: vi.fn(async () => ({})), deleteMessage: vi.fn(async () => true),
    raw: { sendRichMessage: vi.fn(async () => ({ message_id: 1 })), editMessageText: vi.fn(async () => ({})) } };
  return { api, sink: createTelegramRenderingSink({ api } as any, { processThrottleMs: 0 }) };
}
function feishu() {
  const sendFinal = vi.fn(async (_payload?: unknown) => true);
  const create = vi.fn(async (input: any) => {
    if (String(input.data.content).includes(final)) {
      const accepted = await sendFinal(input);
      return accepted ? { code: 0, data: { message_id: 'final-message' } } : { code: 230027, msg: 'rejected' };
    }
    return { code: 0, data: { message_id: 'msg' } };
  });
  const patch = vi.fn(async () => ({ code: 0 }));
  const sink = createFeishuStreamingSink(agent, { im: { message: { create, patch } } } as any, sendFinal, undefined, { processThrottleMs: 0 })!;
  return { create, patch, sendFinal, sink };
}

describe('approved folded process: unchanged count, independent final', () => {
  it('Telegram hides process prose behind a real disclosure control', async () => {
    const { api, sink } = telegram();
    sink.begin(key);
    await sink.activity(key, 42, progress, 'count-5');
    await sink.assistant(key, 42, '我正在核对两个渠道。', 'commentary', 'note-1');
    const payload = api.raw.sendRichMessage.mock.calls[0]?.[0] as any;
    expect(payload?.rich_message?.html).toContain('已推进 5 个执行步骤');
    expect(payload?.rich_message?.html).toContain('callback_data');
    expect(payload?.rich_message?.html).toContain('⌄ 展开过程');
    expect(payload?.rich_message?.html).not.toContain('我正在核对');
  });
  it('Telegram keeps the process, emits one final, and does not delete it', async () => {
    const { api, sink } = telegram();
    sink.begin(key);
    await sink.activity(key, 42, progress, 'count-5');
    await sink.assistant(key, 42, final, 'final', 'final-1');
    await sink.result(key, 42);
    expect(api.deleteMessage).not.toHaveBeenCalled();
    expect(api.sendMessage.mock.calls.filter((call: any[]) => call[1] === final)).toHaveLength(1);
    expect(JSON.stringify(api.raw.editMessageText.mock.calls)).toContain('本轮回复已生成');
  });
  it('Feishu shows only the count and disclosure button before user expansion', async () => {
    const { create, patch, sink } = feishu();
    sink.beginTurn(key);
    await sink.onAssistant(key, 'ou_owner', progress, 'activity', 'count-5');
    await sink.onAssistant(key, 'ou_owner', '正在读取源码，稍后运行测试。', 'commentary', 'note-1');
    const first = JSON.parse((create.mock.calls[0]?.[0] as any)?.data?.content ?? '{}');
    expect(JSON.stringify(first)).toContain('已推进 5 个执行步骤');
    expect(JSON.stringify(first)).toContain('⌄ 展开过程');
    expect(JSON.stringify(patch.mock.calls)).not.toContain('正在读取源码');
  });
  it('Feishu sends the final independently and preserves a folded process', async () => {
    const { sendFinal, patch, sink } = feishu();
    sink.beginTurn(key);
    await sink.onAssistant(key, 'ou_owner', progress, 'activity', 'count-5');
    await sink.onAssistant(key, 'ou_owner', final, 'final', 'final-1');
    await sink.onResult(key);
    await sink.onResult(key);
    expect(sendFinal).toHaveBeenCalledTimes(1);
    expect(JSON.stringify(sendFinal.mock.calls)).toContain(final);
    expect(JSON.stringify(sendFinal.mock.calls)).not.toContain(progress);
    expect(JSON.stringify(patch.mock.calls)).toContain('本轮回复已生成');
  });
});

describe('folded channel delivery boundaries', () => {
  it('Telegram preserves user expansion until the independent final is confirmed', async () => {
    const { api, sink } = telegram();
    sink.begin(key);
    await sink.activity(key, 42, progress);
    await sink.process?.(key, 42, { id: 'tool', kind: 'operation', title: 'Read', state: 'completed', output: 'visible only when opened' });
    const first = api.raw.sendRichMessage.mock.calls[0]?.[0] as any;
    const open = /data="(wp:[a-f0-9]+:open)"/.exec(first.rich_message.html)![1];
    expect(await sink.processAction?.(open, '42', '1')).toBe(true);
    expect(JSON.stringify(api.raw.editMessageText.mock.calls.at(-1))).not.toContain('visible only when opened');
    await sink.processAction?.(open.replace(/open$/, `t${toolDisclosureKey('tool')}o`), '42', '1');
    await sink.activity(key, 42, progress.replace('5', '6'));
    expect(JSON.stringify(api.raw.editMessageText.mock.calls.at(-1))).toContain('visible only when opened');
    await sink.assistant(key, 42, final, 'final');
    expect(JSON.stringify(api.raw.editMessageText.mock.calls.at(-1))).not.toContain('visible only when opened');
    expect(await sink.processAction?.(open, '42', '1')).toBe(true);
    expect(JSON.stringify(api.raw.editMessageText.mock.calls.at(-1))).toContain('visible only when opened');
    await sink.result(key, 42);
    expect(JSON.stringify(api.raw.editMessageText.mock.calls.at(-1))).toContain('visible only when opened');
  });
  it('Telegram does not fold an opened view as successful after final send failure', async () => {
    const { api, sink } = telegram();
    sink.begin(key); await sink.activity(key, 42, progress);
    const first = api.raw.sendRichMessage.mock.calls[0]?.[0] as any;
    const open = /data="(wp:[a-f0-9]+:open)"/.exec(first.rich_message.html)![1];
    await sink.processAction?.(open, '42', '1');
    api.sendMessage.mockRejectedValueOnce(new Error('network uncertain'));
    await sink.assistant(key, 42, final, 'final');
    expect(JSON.stringify(api.raw.editMessageText.mock.calls.at(-1))).toContain('⌃ 收起过程');
    expect(JSON.stringify(api.raw.editMessageText.mock.calls.at(-1))).not.toContain('本轮回复已生成');
  });
  it('Feishu callback is recipient/message scoped and stays readable across count updates', async () => {
    const { create, patch, sink } = feishu();
    sink.beginTurn(key); await sink.onAssistant(key, 'ou_owner', progress, 'activity');
    await sink.onProcess?.(key, 'ou_owner', { id: 'tool', kind: 'operation', state: 'completed', title: 'Read', output: 'read result' });
    const first = (create.mock.calls[0]?.[0] as any).data.content;
    const open = /wp:[a-f0-9]+:open/.exec(first)![0];
    const event = { operator: { open_id: 'ou_owner' }, context: { open_message_id: 'msg' }, action: { value: { relay_work_process: open } } };
    expect(await sink.onAction?.({ ...event, operator: { open_id: 'ou_other' } })).toBe(false);
    expect(await sink.onAction?.(event)).toBe(true);
    await sink.onAction?.({ ...event, action: { value: { relay_work_process: open.replace(/open$/, `t${toolDisclosureKey('tool')}o`) } } });
    await sink.onAssistant(key, 'ou_owner', progress.replace('5', '6'), 'activity');
    expect(JSON.stringify(patch.mock.calls.at(-1))).toContain('read result');
    await sink.onAssistant(key, 'ou_owner', final, 'final'); await sink.onResult(key);
    expect(JSON.stringify(patch.mock.calls.at(-1))).not.toContain('read result');
    expect(await sink.onAction?.(event)).toBe(true);
    expect(JSON.stringify(patch.mock.calls.at(-1))).toContain('read result');
  });
  it('Feishu cannot claim final delivery after an ambiguous failed send', async () => {
    const { sendFinal, patch, sink } = feishu();
    sink.beginTurn(key); await sink.onAssistant(key, 'ou_owner', progress, 'activity');
    sendFinal.mockRejectedValueOnce(new Error('network uncertain'));
    await sink.onAssistant(key, 'ou_owner', final, 'final');
    await sink.onResult(key); await sink.onResult(key);
    expect(sendFinal).toHaveBeenCalledTimes(1);
    expect(JSON.stringify(patch.mock.calls)).not.toContain('本轮回复已生成');
    expect(JSON.stringify(patch.mock.calls)).toContain('未完成');
  });
});

it('routes native public records through the real Feishu event wiring without changing count', async () => {
  const { EventEmitter } = await import('node:events');
  const { wirePoolEvents } = await import('../src/channels/feishu.js');
  const pool = new EventEmitter();
  const { create, patch, sink, sendFinal } = feishu();
  wirePoolEvents(agent, pool as any, new Map([[key, 'ou_owner']]), {} as any, vi.fn(), vi.fn(), undefined, sink);
  pool.emit('turn-accepted', key);
  for (let i = 0; i < 5; i++) pool.emit('turn-effect', key, 'read-only-effect');
  pool.emit('response', key, { type: 'work_process', process_event: { id: 'native', kind: 'operation', state: 'completed', title: 'read', output: 'native result' } });
  await new Promise(resolve => setImmediate(resolve));
  const open = /wp:[a-f0-9]+:open/.exec((create.mock.calls[0]?.[0] as any).data.content)![0];
  await sink.onAction?.({ operator: { open_id: 'ou_owner' }, context: { open_message_id: 'msg' }, action: { value: { relay_work_process: open } } });
  expect(JSON.stringify(patch.mock.calls.at(-1))).toContain('已推进 5 个执行步骤');
  expect(JSON.stringify(patch.mock.calls.at(-1))).not.toContain('native result');
  await sink.onAction?.({ operator: { open_id: 'ou_owner' }, context: { open_message_id: 'msg' }, action: { value: { relay_work_process: open.replace(/open$/, `t${toolDisclosureKey('native')}o`) } } });
  await new Promise(resolve => setImmediate(resolve));
  expect(JSON.stringify(patch.mock.calls.at(-1))).toContain('native result');
  pool.emit('response', key, { type: 'assistant', delivery_phase: 'final', message: { content: [{ type: 'text', text: final }] } });
  pool.emit('response', key, { type: 'result' });
  await new Promise(resolve => setImmediate(resolve));
  expect(sendFinal).toHaveBeenCalledTimes(1);
  expect(JSON.stringify(sendFinal.mock.calls)).not.toContain('native result');
});

it('Feishu renders a runtime-notification turn like a normal turn: folded card, reply once, notice leads the reply', async () => {
  const { EventEmitter } = await import('node:events');
  const { wirePoolEvents } = await import('../src/channels/feishu.js');
  const pool = new EventEmitter();
  const { create, patch, sink, sendFinal } = feishu();
  wirePoolEvents(agent, pool as any, new Map([[key, 'ou_owner']]), {} as any, vi.fn(), vi.fn(), undefined, sink);
  // CC stream-json: no stop_reason and no delivery_phase on the answer.
  pool.emit('turn-accepted', key, RUNTIME_NOTIFICATION_TURN);
  pool.emit('response', key, tagRuntimeNotificationTurn({ type: 'assistant', message: { stop_reason: null, content: [{ type: 'text', text: final }] } }));
  pool.emit('response', key, tagRuntimeNotificationTurn({ type: 'result', result: '' }));
  await vi.waitFor(() => expect(JSON.stringify(patch.mock.calls.at(-1))).toContain('本轮回复已生成'));
  expect(sendFinal).toHaveBeenCalledTimes(1);
  expect(JSON.stringify(sendFinal.mock.calls)).toContain(RUNTIME_NOTIFICATION_REPLY_LEAD);
  const cards = [...create.mock.calls.filter(call => !String((call[0] as any).data.content).includes(final)), ...patch.mock.calls]
    .map(call => JSON.stringify(call));
  expect(cards.at(-1)).toContain('⌄ 展开过程');
  for (const card of cards) expect(card).not.toContain(final);
  const open = /wp:[a-f0-9]+:open/.exec((create.mock.calls[0]?.[0] as any).data.content)![0];
  await sink.onAction?.({ operator: { open_id: 'ou_owner' }, context: { open_message_id: 'msg' }, action: { value: { relay_work_process: open } } });
  await new Promise(resolve => setImmediate(resolve));
  expect(JSON.stringify(patch.mock.calls.at(-1))).toContain('收起过程');
  expect(JSON.stringify(patch.mock.calls.at(-1))).not.toContain(final);
});

it.each(['permission', 'missing_identity'])('does not collapse on Feishu %s acknowledgement', async failure => {
  const { create, patch, sink } = feishu();
  const original = create.getMockImplementation()!;
  create.mockImplementation(async (input: any) => {
    if (!String(input.data.content).includes(final)) return original(input);
    return (failure === 'permission' ? { code: 230027, msg: 'permission denied' } : { code: 0, data: {} }) as any;
  });
  sink.beginTurn(key);
  await sink.onAssistant(key, 'ou_owner', progress, 'activity');
  await sink.onAssistant(key, 'ou_owner', final, 'final');
  await sink.onResult(key);
  expect(create.mock.calls.filter(call => String(call[0].data.content).includes(final))).toHaveLength(1);
  expect(JSON.stringify(patch.mock.calls)).not.toContain('本轮回复已生成');
  expect(JSON.stringify(patch.mock.calls)).toContain('未完成');
});

it('Telegram sends only the authoritative final even when it repeats a phrase from longer commentary', async () => {
  const { api, sink } = telegram(); sink.begin(key);
  await sink.activity(key, 42, progress);
  await sink.assistant(key, 42, `检查过程：${final} 后续仍需复测。`, 'commentary', 'note');
  await sink.process?.(key, 42, { id: 't', kind: 'operation', state: 'completed', title: 'bash', output: 'hidden tool result' });
  await sink.assistant(key, 42, final, 'final', 'answer'); await sink.result(key, 42);
  expect(api.sendMessage.mock.calls.filter((call: any[]) => call[1] === final)).toHaveLength(1);
  expect(JSON.stringify(api.sendMessage.mock.calls)).not.toContain('检查过程');
  const first = api.raw.sendRichMessage.mock.calls[0]?.[0] as any;
  const open = /data="(wp:[a-f0-9]+:open)"/.exec(first.rich_message.html)![1];
  await sink.processAction?.(open, '42', '1');
  const reopened = JSON.stringify(api.raw.editMessageText.mock.calls.at(-1));
  expect(reopened).toContain('检查过程'); expect(reopened).not.toContain('hidden tool result');
});
it('Feishu sends only the authoritative final and retains the complete prior commentary after reopening', async () => {
  const { create, patch, sink, sendFinal } = feishu(); sink.beginTurn(key);
  await sink.onAssistant(key, 'ou_owner', progress, 'activity');
  await sink.onAssistant(key, 'ou_owner', `检查过程：${final} 后续仍需复测。`, 'commentary', 'note');
  await sink.onProcess?.(key, 'ou_owner', { id: 't', kind: 'operation', state: 'completed', title: 'bash', output: 'hidden tool result' });
  await sink.onAssistant(key, 'ou_owner', final, 'final', 'answer'); await sink.onResult(key);
  expect(sendFinal).toHaveBeenCalledOnce(); expect(JSON.stringify(sendFinal.mock.calls)).not.toContain('检查过程');
  const open = /wp:[a-f0-9]+:open/.exec((create.mock.calls[0]?.[0] as any).data.content)![0];
  await sink.onAction?.({ operator: { open_id: 'ou_owner' }, context: { open_message_id: 'msg' }, action: { value: { relay_work_process: open } } });
  await new Promise(resolve => setImmediate(resolve));
  const reopened = JSON.stringify(patch.mock.calls.at(-1));
  expect(reopened).toContain('检查过程'); expect(reopened).not.toContain('hidden tool result');
});

it('Feishu does not deduplicate distinct public paragraphs solely because their text is equal', async () => {
  const { create, patch, sink } = feishu(); sink.beginTurn(key);
  await sink.onAssistant(key, 'ou_owner', progress, 'activity');
  await sink.onAssistant(key, 'ou_owner', '再次确认结果。', 'commentary', 'message-1');
  await sink.onAssistant(key, 'ou_owner', '再次确认结果。', 'commentary', 'message-2');
  const open = /wp:[a-f0-9]+:open/.exec((create.mock.calls[0]?.[0] as any).data.content)![0];
  await sink.onAction?.({ operator: { open_id: 'ou_owner' }, context: { open_message_id: 'msg' }, action: { value: { relay_work_process: open } } });
  await new Promise(resolve => setImmediate(resolve));
  const card = JSON.parse((patch.mock.calls.at(-1) as any)[0].data.content);
  expect(card.body.elements.filter((element: any) => element.tag === 'markdown' && element.content === '再次确认结果。')).toHaveLength(2);
});
