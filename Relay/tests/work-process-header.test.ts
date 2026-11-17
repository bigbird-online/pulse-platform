import { afterEach, describe, expect, it, vi } from 'vitest';
import { workProcessHeader } from '../src/work-process-header.js';
import { feishuProcessCard, telegramProcessPayload } from '../src/work-process-transports.js';
import { createTelegramRenderingSink } from '../src/channels/telegram.js';
import { createFeishuStreamingSink } from '../src/channels/feishu.js';
import { TURN_ACCEPTED_STATUS_TEXT, TURN_GENERATED_STATUS_TEXT } from '../src/channel-rendering.js';
import type { WorkProcessRecord } from '../src/work-process-archive.js';

const record = (patch: Partial<WorkProcessRecord> = {}): WorkProcessRecord => ({
  version: 1, id: 'a'.repeat(24), scope: 'fixture', address: '42', messageId: '1',
  createdAt: Date.now() - 292_000, summary: TURN_ACCEPTED_STATUS_TEXT, expanded: false,
  page: 0, terminal: false, events: [], omitted: 0, ...patch,
});
afterEach(() => vi.unstubAllEnvs());

describe('scheme 1: one hierarchy, native channel layouts', () => {
  it.each([false, true])('uses non-emoji disclosure and separated metadata (expanded=%s)', expanded => {
    const r = record({ expanded, summary: '⏳ 仍在工作，已推进 5 个执行步骤…' });
    const before = JSON.stringify(r), header = workProcessHeader(r, r.createdAt + 292_000);
    expect(header).toEqual({ control: expanded ? '⌃ 收起过程' : '⌄ 展开过程', marker: '◌',
      status: '仍在工作…', meta: '已推进 5 个执行步骤 · 已用 4 分 52 秒' });
    const tg = telegramProcessPayload(r), card: any = feishuProcessCard(r);
    for (const wire of [tg.html, JSON.stringify(card)]) {
      expect(wire).toContain(header.control); expect(wire).toContain(header.status);
      expect(wire).toContain('已推进 5 个执行步骤'); expect(wire).not.toContain('▶');
    }
    expect(tg.html).toMatch(/<tg-button[^>]*style="primary"/);
    expect(tg.html).not.toContain('<footer>'); expect(tg.html).not.toMatch(/border-radius|<div|style="[^"]*:/);
    const columns = card.body.elements[0].columns;
    expect(columns[0]).toMatchObject({ width: 'auto', elements: [{ tag: 'button', type: 'primary' }] });
    expect(columns.at(-1)).toMatchObject({ width: 'weighted', weight: 1 });
    expect(columns.at(-1).elements).toHaveLength(1);
    expect(columns.at(-1).elements[0].content).toContain('仍在工作… · 已推进 5');
    expect(columns.at(-1).elements[0].content).not.toContain('<font');
    expect(JSON.stringify(r)).toBe(before);
  });
  it('removes the duplicate play emoji without changing accepted state', () => {
    const r = record();
    expect(workProcessHeader(r).status).toBe('已收到，开始处理…');
    expect(telegramProcessPayload(r).html).not.toContain('▶');
    expect(JSON.stringify(feishuProcessCard(r))).not.toContain('▶');
    expect(r.summary).toBe(TURN_ACCEPTED_STATUS_TEXT);
  });
  it('keeps final duration fixed and old unknown duration honest', () => {
    const r = record({ terminal: true, summary: TURN_GENERATED_STATUS_TEXT });
    r.finishedAt = r.createdAt + 292_000;
    expect(workProcessHeader(r, r.finishedAt + 86400_000)).toMatchObject({ marker: '✓', meta: '用时 4 分 52 秒' });
    delete r.finishedAt; expect(workProcessHeader(r).meta).toBe('');
    r.summary = '⚠️ 本轮回复未完成'; expect(workProcessHeader(r).marker).toBe('!');
  });
});

it.each(['telegram', 'feishu'] as const)('%s actual sink callback opens prose, preserves choice and hides tool output', async channel => {
  vi.stubEnv('RELAY_WORK_PROCESS_DETAILS', 'on'); vi.stubEnv('RELAY_FEISHU_STREAMING_CARD', 'true');
  let latest = '', first = '';
  const save = (content: string) => { latest = content; first ||= content; };
  const raw = { sendRichMessage: vi.fn(async (p: any) => { save(p.rich_message.html); return { message_id: 1 }; }),
    editMessageText: vi.fn(async (p: any) => { save(p.rich_message.html); return { message_id: 1 }; }) };
  const api = { raw, sendMessage: vi.fn(async () => ({ message_id: 2 })) };
  const message = { create: vi.fn(async (p: any) => { save(p.data.content); return { code: 0, data: { message_id: '1' } }; }),
    patch: vi.fn(async (p: any) => { expect(p.path.message_id).toBe('1'); save(p.data.content); return { code: 0 }; }) };
  const tg = createTelegramRenderingSink({ api } as any, { processThrottleMs: 0 });
  const fs = createFeishuStreamingSink({ id: 'test', feishu: { appId: 'fixture', appSecret: 'fixture' } } as any,
    { im: { message } } as any, vi.fn(), undefined, { processThrottleMs: 0 })!;
  const key = 'owner:test', count = '⏳ 仍在工作，已推进 5 个执行步骤…';
  if (channel === 'telegram') { tg.begin(key); await tg.activity(key, 42, count); }
  else { fs.beginTurn(key); await fs.onAssistant(key, '42', count, 'activity'); }
  const note = '先检查入口，再核对认证状态。';
  const event = { id: 'tool', kind: 'operation' as const, state: 'completed' as const, title: 'bash', output: 'hidden-output' };
  if (channel === 'telegram') { await tg.assistant(key, 42, note, 'commentary', 'note'); await tg.process!(key, 42, event); }
  else { await fs.onAssistant(key, '42', note, 'commentary', 'note'); await fs.onProcess!(key, '42', event); }
  expect(latest).not.toContain(note);
  const open = /wp:[a-f0-9]{24}:open/.exec(first)![0];
  const action = (value: string, owner = '42') => channel === 'telegram'
    ? tg.processAction!(value, owner, '1')
    : fs.onAction!({ schema: '2.0', event: { operator: { open_id: owner }, context: { open_message_id: '1' },
        action: { value: { relay_work_process: value } } } });
  expect(await action(open, 'other')).toBe(false);
  expect(await action(open)).toBe(true);
  await vi.waitFor(() => expect(latest).toContain(note));
  expect(latest).toContain('⌃ 收起过程'); expect(latest).not.toContain('hidden-output');
  if (channel === 'telegram') await tg.activity(key, 42, count.replace('5', '6'));
  else await fs.onAssistant(key, '42', count.replace('5', '6'), 'activity');
  expect(latest).toContain('已推进 6 个执行步骤'); expect(latest).toContain(note);
  expect(await action(open.replace(/open$/, 'close'))).toBe(true);
  await vi.waitFor(() => expect(latest).not.toContain(note));
  expect(latest).toContain('⌄ 展开过程');
  expect(channel === 'telegram' ? raw.editMessageText.mock.calls.length : message.patch.mock.calls.length).toBeGreaterThanOrEqual(3);
});


it.each([false, true])('keeps 41-second duration after completion in the same native text row (expanded=%s)', expanded => {
  const r = record({ expanded, terminal: true, summary: TURN_GENERATED_STATUS_TEXT });
  r.finishedAt = r.createdAt + 41_000;
  const tg = telegramProcessPayload(r), fs: any = feishuProcessCard(r);
  const firstParagraph = tg.html.match(/^<p>(.*?)<\/p>/s)![1];
  expect(firstParagraph).toContain('✓ 本轮回复已生成 · 用时 41 秒');
  expect(tg.html).not.toContain('<footer>');
  expect(tg.text.split('\n')[0]).toBe('✓ 本轮回复已生成 · 用时 41 秒');
  const statusColumn = fs.body.elements[0].columns.at(-1);
  expect(statusColumn.elements).toHaveLength(1);
  expect(statusColumn.elements[0].content).toBe('✓ 本轮回复已生成 · 用时 41 秒');
  expect(statusColumn.elements[0].content).not.toContain('<font');
  expect(statusColumn.elements[0].content).not.toContain('\n');
});
