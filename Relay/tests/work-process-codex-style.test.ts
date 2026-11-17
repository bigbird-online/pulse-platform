import { afterEach, describe, expect, it, vi } from 'vitest';
import { WorkProcessView, processCallback, type ProcessWriter } from '../src/work-process-view.js';
import { type WorkProcessRecord, enableWorkProcessArchive } from '../src/work-process-archive.js';
import { toolDisclosureKey, workProcessDocument, workProcessSummary } from '../src/work-process-content.js';
import { telegramProcessPayload, telegramProcessWriter, feishuProcessCard, feishuProcessWriter } from '../src/work-process-transports.js';
import { publicCommentaryEvent } from '../src/public-assistant-progress.js';

afterEach(() => { enableWorkProcessArchive(undefined); vi.restoreAllMocks(); });
function harness(scope = 'telegram:fixture') {
  const writer = vi.fn(async () => '42') as unknown as ProcessWriter;
  const view = new WorkProcessView(scope, writer, 0);
  view.begin('turn');
  const latest = (): WorkProcessRecord => structuredClone((view as any).current.get('turn').record);
  const action = (name: string) => view.callback(processCallback(latest(), name), 'owner', '42');
  const note = (id: string, text: string) => view.event('turn', 'owner', publicCommentaryEvent(id, text));
  const tool = (id: string, output: string, state: 'running' | 'completed' = 'completed') => view.event('turn', 'owner',
    { id, kind: 'operation', state, title: 'bash', input: 'command: fixture command', output });
  return { view, writer, latest, action, note, tool };
}
const channelViews = (record: WorkProcessRecord): string[] => [telegramProcessPayload(record).html,
  telegramProcessPayload(record).text, JSON.stringify(feishuProcessCard(record))];

describe('Codex-style public process', () => {
  it('keeps all public paragraphs in order, with tool inputs and results hidden by default', async () => {
    const h = harness();
    await h.note('a', '首先核对服务状态。'); await h.tool('t1', 'INTERNAL_TOOL_RESULT_1');
    await h.note('b', '服务在线，但认证失败。'); await h.tool('t2', 'INTERNAL_TOOL_RESULT_2');
    await h.note('c', '认证恢复后，真实调用成功。');
    await h.action('open');
    for (const wire of channelViews(h.latest())) {
      expect(wire.indexOf('首先核对')).toBeLessThan(wire.indexOf('服务在线'));
      expect(wire.indexOf('服务在线')).toBeLessThan(wire.indexOf('认证恢复'));
      expect(wire).toContain('运行了命令'); expect(wire).not.toContain('INTERNAL_TOOL_RESULT');
      expect(wire).not.toContain('fixture command'); expect(wire).not.toContain('进展说明');
    }
    await h.action(`t${toolDisclosureKey('t1')}o`);
    for (const wire of channelViews(h.latest())) {
      expect(wire).toContain('INTERNAL_TOOL_RESULT_1'); expect(wire).not.toContain('INTERNAL_TOOL_RESULT_2');
    }
    await h.action(`t${toolDisclosureKey('t1')}c`);
    expect(telegramProcessPayload(h.latest()).html).not.toContain('INTERNAL_TOOL_RESULT');
  });
  it('retains more than the previous 12/40 item limits without chopping commentary', async () => {
    const h = harness();
    for (let i = 0; i < 60; i++) await h.note(`m${i}`, `阶段 ${i} 的完整公开说明。`);
    await h.action('open');
    for (const wire of channelViews(h.latest())) {
      for (let i = 0; i < 60; i++) expect(wire).toContain(`阶段 ${i} 的完整公开说明。`);
      expect(wire).not.toContain('节选');
    }
  });
  it('gives commentary priority over even explicitly opened long tool output', async () => {
    const h = harness(); await h.note('a', '开头的说明。'); await h.tool('t', '结果'.repeat(15_000));
    await h.note('b', '末尾的重要发现。'); await h.action('open'); await h.action(`t${toolDisclosureKey('t')}o`);
    for (const wire of channelViews(h.latest())) {
      expect(wire).toContain('开头的说明'); expect(wire).toContain('末尾的重要发现'); expect(wire).toContain('查看完整过程');
    }
  });
  it('offers a complete, script-free continuous document instead of silently cutting an oversized paragraph', async () => {
    const h = harness(); const text = '完整说明'.repeat(9_000) + '最后一个字符';
    await h.note('long', text); await h.tool('tool', 'tool output'); await h.action('open');
    for (const wire of channelViews(h.latest())) expect(wire).toContain('查看完整过程');
    const document = workProcessDocument(h.latest()).toString();
    expect(document).toContain(text); expect(document).toContain('<details><summary>运行了命令');
    expect(document).not.toContain('<details open'); expect(document).not.toMatch(/<script|<iframe/);
    expect(document).toContain("default-src 'none'");
  });
  it('never exposes markup, secrets or system contents in public commentary', async () => {
    const h = harness(); await h.note('a', '<script>alert(1)</script>\n\napi_key=synthetic-private-token\n<system>PRIVATE_RULE</system>\n正常说明。');
    await h.action('open');
    for (const wire of [...channelViews(h.latest()), workProcessDocument(h.latest()).toString()]) {
      expect(wire).not.toContain('synthetic-private-token'); expect(wire).not.toContain('PRIVATE_RULE');
      expect(wire).toContain('正常说明');
    }
    expect(telegramProcessPayload(h.latest()).html).not.toContain('<script>');
    expect(workProcessDocument(h.latest()).toString()).not.toContain('<script>');
  });
  it('does not overwrite prior narrative snapshots across tool boundaries', async () => {
    const h = harness();
    await h.view.frame('turn', 'owner', 'snapshot', '第一段。', 'd1');
    await h.tool('t', 'OK');
    await h.view.frame('turn', 'owner', 'snapshot', '第一段。第二段', 'd2');
    await h.view.frame('turn', 'owner', 'snapshot', '第一段。第二段完整。', 'd3');
    await h.view.frame('turn', 'owner', 'snapshot', '独立的第三段。', 'd4');
    expect(h.latest().events.map(event => event.output)).toEqual(['第一段。', 'OK', '第二段完整。', '独立的第三段。']);
  });
  it('keeps every independent paragraph even when it repeats the final answer text', async () => {
    const h = harness(); await h.note('first', '相同文字'); await h.tool('t', 'OK');
    await h.note('second', '相同文字'); await h.view.frame('turn', 'owner', 'final', '相同文字', 'native-final');
    await h.view.terminal('turn', true, '相同文字');
    expect(h.latest().events.filter(event => event.kind === 'commentary')).toHaveLength(2);
  });
  it('retains manual tool choices and freezes elapsed time after one successful collapse', async () => {
    const clock = vi.spyOn(Date, 'now').mockReturnValue(1_000_000);
    const h = harness(); await h.tool('t', 'OK'); await h.action('open'); await h.action(`t${toolDisclosureKey('t')}o`);
    clock.mockReturnValue(1_292_000); await h.view.terminal('turn', true, 'final');
    expect(h.latest().expanded).toBe(false); expect(workProcessSummary(h.latest())).toContain('4 分 52 秒');
    clock.mockReturnValue(2_000_000); await h.action('open'); await h.view.terminal('turn', true, 'duplicate');
    expect(h.latest().expanded).toBe(true); expect(h.latest().openTools).toEqual(['t']);
    expect(workProcessSummary(h.latest())).toContain('4 分 52 秒');
  });
  it('does not re-edit a displayed timeline for hidden result-only updates', async () => {
    const h = harness(); await h.tool('t', 'one', 'running'); await h.action('open');
    const count = vi.mocked(h.writer).mock.calls.length;
    await h.tool('t', 'two', 'running'); await h.tool('t', 'three', 'running');
    expect(vi.mocked(h.writer).mock.calls.length).toBe(count);
    expect(h.latest().events[0].output).toBe('three');
  });
  it('scopes tool opening and document export to the original bot, recipient and message', async () => {
    const h = harness(); h.writer.export = vi.fn(async () => {}); await h.tool('t', 'OK');
    const all = processCallback(h.latest(), 'all'), tool = processCallback(h.latest(), `t${toolDisclosureKey('t')}o`);
    expect(await h.view.callback(tool, 'intruder', '42')).toBe(false);
    expect(await h.view.callback(all, 'owner', '99')).toBe(false);
    expect(await h.view.callback(processCallback(h.latest(), 't0000000000000000o'), 'owner', '42')).toBe(false);
    expect(await new WorkProcessView('other-bot', h.writer, 0).callback(all, 'owner', '42')).toBe(false);
    expect(h.writer.export).not.toHaveBeenCalled(); await h.action('open');
    expect(h.writer.export).not.toHaveBeenCalled(); await h.action('all');
    expect(h.writer.export).toHaveBeenCalledOnce(); expect(vi.mocked(h.writer.export).mock.calls[0][0].address).toBe('owner');
  });
  it('coalesces simultaneous document requests without delaying a Feishu callback', async () => {
    const h = harness(); let release!: () => void;
    h.writer.export = vi.fn(() => new Promise<void>(resolve => { release = resolve; }));
    await h.note('a', '正文。');
    const all = processCallback(h.latest(), 'all');
    expect(await h.view.callback(all, 'owner', '42', false)).toBe(true);
    expect(await h.view.callback(all, 'owner', '42', false)).toBe(true);
    expect(h.writer.export).toHaveBeenCalledOnce(); release();
  });
});

describe('native document transports', () => {
  it('delivers the same static document only to the bound Telegram recipient', async () => {
    const sendDocument = vi.fn(async () => ({ message_id: 8 }));
    const writer = telegramProcessWriter({ api: { sendDocument } } as any);
    const h = harness(); await h.note('a', '完整说明。');
    await writer.export!({ ...h.latest(), address: '123' });
    expect(sendDocument.mock.calls[0][0]).toBe(123);
  });
  it('uploads and delivers a Feishu file with confirmed upload and message identities', async () => {
    const upload = vi.fn(async () => ({ file_key: 'file-fixture' }));
    const send = vi.fn(async () => ({ code: 0, data: { message_id: 'file-message' } }));
    const writer = feishuProcessWriter({ im: { message: { create: send, patch: vi.fn() }, file: { create: upload } } });
    const h = harness(); await h.note('a', '完整说明。'); await writer.export!(h.latest());
    const uploaded = upload.mock.calls[0] as any;
    expect(uploaded[0].data.file.toString()).toContain('完整说明');
    expect((send.mock.calls[0] as any)[0].data.receive_id).toBe('owner');
    upload.mockResolvedValueOnce(null as any);
    await expect(writer.export!(h.latest())).rejects.toThrow('upload failed');
    expect(send).toHaveBeenCalledTimes(1);
  });
});

it('reserves narrative capacity when many hidden tool results hit the storage boundary', async () => {
  const h = harness();
  for (let i = 0; i < 30; i++) await h.tool(`tool-${i}`, '中'.repeat(31_000));
  const narrative = '重要的过程说明。'.repeat(2_000) + '完整保留的末尾';
  await h.note('late-narrative', narrative);
  expect(h.latest().events.at(-1)?.output).toBe(narrative);
  expect(h.latest().omitted).toBeGreaterThan(0);
  expect(Buffer.byteLength(JSON.stringify(h.latest().events))).toBeLessThan(2_000_000);
  expect(workProcessDocument(h.latest()).toString()).toContain('完整保留的末尾');
});
