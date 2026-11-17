import { describe, expect, it, vi } from 'vitest';
import {
  COLLAB_GATE_SCHEMA, collabTurnHeadline, feishuNoticeCard, feishuTurnNoticeCard, formatDuration,
  parseCollabBatch, progressNoticeTitle, shortContractId, startProgressNotice, telegramNoticeHtml,
  telegramTurnNoticeHtml, type CollabNoticeContract, type CollabTurnNotice,
} from '../src/collab-progress-notice.js';
import type { FeishuMessageClient } from '../src/delivery.js';

const CONTRACT = 'op_643afde3b37f5519481a7f518af031042e29f946011d32391d06c0e47199e096';

function batch(contracts: object[]): string {
  return JSON.stringify({ schema: COLLAB_GATE_SCHEMA, actor: 'hestia', coverage: 'complete', contracts });
}

const ONE: CollabNoticeContract[] = [{
  contractId: CONTRACT, offeredBy: 'pulse', workItemId: 'wi_20261015_418', kind: 'request', status: 'pending', rewake: false,
}];

function feishuMock(overrides: { create?: ReturnType<typeof vi.fn>; patch?: ReturnType<typeof vi.fn> | null } = {}) {
  const create = overrides.create ?? vi.fn().mockResolvedValue({ code: 0, data: { message_id: 'om_notice' } });
  const patch = overrides.patch === null ? undefined : (overrides.patch ?? vi.fn().mockResolvedValue({ code: 0 }));
  const client = { im: { message: { create, ...(patch ? { patch } : {}) } } } as unknown as FeishuMessageClient;
  return { client, create, patch };
}

function botMock(overrides: { sendMessage?: ReturnType<typeof vi.fn>; editMessageText?: ReturnType<typeof vi.fn> } = {}) {
  const sendMessage = overrides.sendMessage ?? vi.fn().mockResolvedValue({ message_id: 42 });
  const editMessageText = overrides.editMessageText ?? vi.fn().mockResolvedValue(true);
  return { bot: { api: { sendMessage, editMessageText } } as unknown as import('grammy').Bot, sendMessage, editMessageText };
}

const quiet = () => {};

describe('collab progress notice rendering', () => {
  it('parses only non-empty collab inbox gate batches', () => {
    expect(parseCollabBatch(batch([{
      work_item_id: 'wi_20261015_418', contract_id: CONTRACT, kind: 'request', status: 'accepted',
      offered_by: 'pulse', offered_at: '2026-10-15T00:00:00Z', rewake: true,
    }]))).toEqual([{ contractId: CONTRACT, offeredBy: 'pulse', workItemId: 'wi_20261015_418',
      kind: 'request', status: 'accepted', rewake: true }]);
    expect(parseCollabBatch(batch([]))).toBeUndefined();
    expect(parseCollabBatch('[hestia 30min gate] 1 新增')).toBeUndefined();
    expect(parseCollabBatch(JSON.stringify({ schema: 'other/v1', contracts: [{}] }))).toBeUndefined();
  });

  it('shortens op_ contract ids to op_ plus 9 characters', () => {
    expect(shortContractId(CONTRACT)).toBe('op_643afde3b');
    expect(shortContractId('legacy-id')).toBe('legacy-id');
  });

  it('formats durations in minutes and seconds', () => {
    expect(formatDuration(4_400)).toBe('4 秒');
    expect(formatDuration(192_000)).toBe('3 分 12 秒');
  });

  it('builds a Feishu 2.0 card whose details sit in a collapsed panel', () => {
    const card = feishuNoticeCard('Hestia 协作收件箱', ONE);
    expect(card.schema).toBe('2.0');
    expect((card.header as { title: { content: string } }).title.content).toBe('📨 后台正在处理 pulse wi_20261015_418 协作合同。');
    const elements = (card.body as { elements: Array<Record<string, unknown>> }).elements;
    const panel = elements.find(e => e.tag === 'collapsible_panel')!;
    expect(panel.expanded).toBe(false);
    const details = (panel.elements as Array<{ content: string }>)[0]!.content;
    expect(details).toContain('op_643afde3b');
    expect(details).not.toContain(CONTRACT);
    expect(details).toContain('pulse');
    expect(details).toContain('wi_20261015_418');
    expect(details).toContain('request');
    expect(details).toContain('pending');

    const done = feishuNoticeCard('Hestia 协作收件箱', ONE, { outcome: 'timeout', durationMs: 1_800_000 });
    expect((done.header as { title: { content: string }; template: string }).template).toBe('orange');
    expect(JSON.stringify(done)).toContain('30 分 0 秒');
  });

  it('builds Telegram HTML with an escaped expandable blockquote', () => {
    const html = telegramNoticeHtml('<Hestia>', [{ ...ONE[0]!, offeredBy: 'a&b' }]);
    expect(html.startsWith('📨 后台正在处理 a&amp;b wi_20261015_418 协作合同。\n')).toBe(true);
    expect(html).toContain('<blockquote expandable>');
    expect(html).toContain('&lt;Hestia&gt;');
    expect(html).toContain('a&amp;b');
  });

  it('names who offered the contracts in the title, by display name when known', () => {
    const names = (actor: string) => ({ pulse: 'Pulse', amanda: 'Amanda' } as Record<string, string>)[actor];
    expect(progressNoticeTitle(ONE, names)).toBe('📨 后台正在处理 Pulse wi_20261015_418 协作合同。');
    const three = [ONE[0]!, { ...ONE[0]!, offeredBy: 'amanda' }, { ...ONE[0]!, offeredBy: 'hestia' }];
    expect(progressNoticeTitle(three, names)).toBe('📨 后台正在处理 3 份协作合同，来自 Pulse、Amanda、hestia。');
    expect(progressNoticeTitle([ONE[0]!, ONE[0]!])).toBe('📨 后台正在处理 2 份协作合同，来自 pulse。');
    expect(telegramNoticeHtml('J', ONE, undefined, names)).toContain('Pulse wi_20261015_418');
  });

  it('lists at most 20 contracts and counts the rest', () => {
    const many = Array.from({ length: 23 }, (_, i) => ({ ...ONE[0]!, contractId: `op_${String(i).padStart(9, '0')}x` }));
    const html = telegramNoticeHtml('Hestia', many);
    expect(html).toContain('另有 3 份未列出');
    expect(html).not.toContain('op_000000022');
  });
});

describe('collab turn notice rendering', () => {
  const RECIPIENT: CollabTurnNotice = {
    role: 'recipient', contractId: CONTRACT, workItemId: 'wi_20261023_675', kind: 'request', status: 'pending',
    deadline: '2026-10-30T12:00:00+08:00', targetWorkItemId: 'wi_20261024_002', mainOnly: true, offeredBy: 'Amanda',
  };

  it('heads a recipient delivery with the offering Agent and WI', () => {
    expect(collabTurnHeadline(RECIPIENT)).toBe('📨 收到 Amanda wi_20261023_675 协作合同，开始处理。');
    expect(collabTurnHeadline({ ...RECIPIENT, offeredBy: undefined })).toBe('📨 收到 wi_20261023_675 协作合同，开始处理。');
  });

  it('heads a requester delivery with the answering Agents and the answer', () => {
    const settle: CollabTurnNotice = { ...RECIPIENT, role: 'requester', status: 'returned', answeredBy: ['Hestia', 'Pulse'] };
    expect(collabTurnHeadline(settle)).toBe('📨 Hestia、Pulse 已答复你在 wi_20261023_675 的协作合同（已退回），开始验收。');
    expect(collabTurnHeadline({ ...settle, status: 'completed' })).toContain('（已完成）');
    expect(collabTurnHeadline({ ...settle, status: 'rejected' })).toContain('（已拒绝）');
  });

  it('folds the contract details into an escaped Telegram expandable blockquote', () => {
    const html = telegramTurnNoticeHtml({ ...RECIPIENT, offeredBy: '<A&B>' });
    const [head, ...rest] = html.split('\n');
    expect(head).toBe('📨 收到 &lt;A&amp;B&gt; wi_20261023_675 协作合同，开始处理。');
    const quote = rest.join('\n');
    expect(quote.startsWith('<blockquote expandable>')).toBe(true);
    for (const text of ['op_643afde3b', 'request', 'pending', '截止：2026-10-30T12:00:00+08:00', '处理 WI：wi_20261024_002', '只限主身：是']) {
      expect(quote).toContain(text);
    }
    expect(quote).not.toContain(CONTRACT);
    const bare = telegramTurnNoticeHtml({ ...RECIPIENT, deadline: undefined, mainOnly: undefined });
    expect(bare).not.toContain('截止');
    expect(bare).not.toContain('只限主身');
  });

  it('builds a Feishu card titled by the headline with the details collapsed', () => {
    const card = feishuTurnNoticeCard({ ...RECIPIENT, mainOnly: false });
    expect((card.header as { title: { content: string } }).title.content).toBe(collabTurnHeadline(RECIPIENT));
    const elements = (card.body as { elements: Array<Record<string, unknown>> }).elements;
    expect(elements).toHaveLength(1);
    expect(elements[0]!.tag).toBe('collapsible_panel');
    expect(elements[0]!.expanded).toBe(false);
    const details = (elements[0]!.elements as Array<{ content: string }>)[0]!.content;
    expect(details).toContain('`op_643afde3b`');
    expect(details).toContain('`wi_20261024_002`');
    expect(details).toContain('只限主身：否');
  });
});

describe('collab progress notice delivery', () => {
  it('sends nothing without a target', () => {
    expect(startProgressNotice({ id: 'j', name: 'J' }, ONE, {}, quiet)).toBeUndefined();
  });

  it('posts a Feishu card and patches the same card with the outcome', async () => {
    const { client, create, patch } = feishuMock();
    const notice = startProgressNotice({ id: 'j', name: 'J' }, ONE, { feishu: { client, openId: 'ou_x' } }, quiet)!;
    await notice.finish('ok', 65_000);
    expect(create).toHaveBeenCalledTimes(1);
    expect(create.mock.calls[0]![0].data.msg_type).toBe('interactive');
    expect(create.mock.calls[0]![0].data.receive_id).toBe('ou_x');
    expect(patch).toHaveBeenCalledTimes(1);
    expect(patch!.mock.calls[0]![0].path.message_id).toBe('om_notice');
    const patched = JSON.parse(patch!.mock.calls[0]![0].data.content);
    expect(patched.header.title.content).toBe('✅ 协作合同后台处理完成');
    expect(JSON.stringify(patched)).toContain('1 分 5 秒');
  });

  it('sends no separate result message when the card cannot be patched', async () => {
    for (const outcome of ['ok', 'paused', 'error', 'timeout'] as const) {
      const unpatchable = feishuMock({ patch: null });
      await startProgressNotice({ id: 'j', name: 'J' }, ONE, { feishu: { client: unpatchable.client, openId: 'ou_x' } }, quiet)!
        .finish(outcome, 3_000);
      expect(unpatchable.create).toHaveBeenCalledTimes(1);
      const failing = feishuMock({ patch: vi.fn().mockRejectedValue(new Error('patch down')) });
      await startProgressNotice({ id: 'j', name: 'J' }, ONE, { feishu: { client: failing.client, openId: 'ou_x' } }, quiet)!
        .finish(outcome, 3_000);
      expect(failing.create).toHaveBeenCalledTimes(1);
    }
  });

  it('swallows send failures and logs them', async () => {
    const log = vi.fn();
    const { client, create, patch } = feishuMock({ create: vi.fn().mockRejectedValue(new Error('network down')) });
    const notice = startProgressNotice({ id: 'j', name: 'J' }, ONE, { feishu: { client, openId: 'ou_x' } }, log)!;
    await expect(notice.finish('ok', 1_000)).resolves.toBeUndefined();
    expect(patch).not.toHaveBeenCalled();
    expect(create).toHaveBeenCalledTimes(1);
    expect(log).toHaveBeenCalledWith(expect.objectContaining({ channel: 'feishu', step: 'send', level: 'warn' }));
  });

  it('treats a non-zero Feishu response code as a failed send', async () => {
    const { client, patch } = feishuMock({ create: vi.fn().mockResolvedValue({ code: 99991663, data: {} }) });
    const notice = startProgressNotice({ id: 'j', name: 'J' }, ONE, { feishu: { client, openId: 'ou_x' } }, quiet)!;
    await notice.finish('timeout', 1_000);
    expect(patch).not.toHaveBeenCalled();
  });

  it('posts an expandable Telegram message and edits it with the outcome', async () => {
    const { bot, sendMessage, editMessageText } = botMock();
    const notice = startProgressNotice({ id: 'j', name: 'J' }, ONE, { telegram: { bot, chatId: '1000000001' } }, quiet)!;
    await notice.finish('error', 2_000);
    expect(sendMessage).toHaveBeenCalledTimes(1);
    expect(sendMessage.mock.calls[0]![0]).toBe(1000000001);
    expect(sendMessage.mock.calls[0]![1]).toContain('<blockquote expandable>');
    expect(sendMessage.mock.calls[0]![2]).toEqual({ parse_mode: 'HTML' });
    expect(editMessageText).toHaveBeenCalledWith(1000000001, 42, expect.stringContaining('❌ 协作合同后台处理失败'), { parse_mode: 'HTML' });
  });

  it('sends no separate Telegram result message when the edit or the first send fails', async () => {
    const log = vi.fn();
    const editFails = botMock({ editMessageText: vi.fn().mockRejectedValue(new Error('message is not modified')) });
    await startProgressNotice({ id: 'j', name: 'J' }, ONE, { telegram: { bot: editFails.bot, chatId: '1' } }, log)!.finish('ok', 2_000);
    expect(editFails.sendMessage).toHaveBeenCalledTimes(1);
    expect(log).toHaveBeenCalledWith(expect.objectContaining({ channel: 'telegram', step: 'edit' }));
    const sendFails = botMock({ sendMessage: vi.fn().mockRejectedValue(new Error('blocked')) });
    await startProgressNotice({ id: 'j', name: 'J' }, ONE, { telegram: { bot: sendFails.bot, chatId: '1' } }, quiet)!.finish('paused', 2_000);
    expect(sendFails.sendMessage).toHaveBeenCalledTimes(1);
    expect(sendFails.editMessageText).not.toHaveBeenCalled();
  });

  it('finishes only once', async () => {
    const { client, patch } = feishuMock();
    const notice = startProgressNotice({ id: 'j', name: 'J' }, ONE, { feishu: { client, openId: 'ou_x' } }, quiet)!;
    await notice.finish('ok', 1_000);
    await notice.finish('error', 2_000);
    expect(patch).toHaveBeenCalledTimes(1);
  });
});
