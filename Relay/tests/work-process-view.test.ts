import { afterEach, describe, expect, it, vi } from 'vitest';
import { mkdtempSync, rmSync, statSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { WorkProcessView, processCallback, workProcessDetails } from '../src/work-process-view.js';
import { enableWorkProcessArchive, loadWorkProcess, type WorkProcessRecord } from '../src/work-process-archive.js';
import { publicWorkEvents, sanitizeWorkText } from '../src/work-process-events.js';
import { telegramProcessPayload, feishuProcessCard } from '../src/work-process-transports.js';
import { toolDisclosureKey } from '../src/work-process-content.js';
import { TURN_GENERATED_STATUS_TEXT } from '../src/channel-rendering.js';

const count = '⏳ 仍在工作，已推进 5 个执行步骤…';
const roots: string[] = [];
afterEach(() => { enableWorkProcessArchive(undefined); for (const root of roots.splice(0)) rmSync(root, { recursive: true, force: true }); });
function harness(scope = 'telegram:agent') {
  const frames: WorkProcessRecord[] = [];
  const writer = vi.fn(async (record: WorkProcessRecord) => { frames.push(structuredClone(record)); return '42'; });
  const view = new WorkProcessView(scope, writer, 0);
  view.begin('tenant');
  return { view, writer, frames, latest: () => structuredClone((view as any).current.get("tenant").record) as WorkProcessRecord };
}

describe('work process view lifecycle', () => {
  it('never opens on events, preserves explicit opening, collapses just once after final', async () => {
    const { view, latest } = harness();
    await view.frame('tenant', 'owner', 'activity', count);
    await view.event('tenant', 'owner', { id: '1', kind: 'operation', state: 'running', title: 'read', input: 'src/test.ts' });
    expect(latest().expanded).toBe(false);
    expect(latest().summary).toBe(count);
    expect(await view.callback(processCallback(latest(), 'open'), 'owner', '42')).toBe(true);
    await view.frame('tenant', 'owner', 'activity', count.replace('5', '6'));
    expect(latest().expanded).toBe(true);
    await view.terminal('tenant', true, 'result');
    expect(latest().expanded).toBe(false);
    expect(latest().summary).toBe(TURN_GENERATED_STATUS_TEXT);
    await view.callback(processCallback(latest(), 'open'), 'owner', '42');
    await view.terminal('tenant', true, 'duplicate');
    await view.event('tenant', 'owner', { id: 'late', kind: 'operation', state: 'running' });
    expect(latest().expanded).toBe(true);
    expect(latest().events).toHaveLength(1);
  });
  it('does not falsely complete an in-flight tool on final or failure', async () => {
    const { view, latest } = harness();
    await view.event('tenant', 'owner', { id: '1', kind: 'operation', state: 'running', title: 'read' });
    await view.callback(processCallback(latest(), 'open'), 'owner', '42');
    await view.terminal('tenant', false);
    expect(latest().expanded).toBe(true);
    expect(latest().summary).not.toContain('已生成');
    expect(workProcessDetails(latest()).items[0].title).toContain('未确认完成');
  });
  it('updates one record by opaque ID and refuses late regression', async () => {
    const { view, latest } = harness();
    await view.event('tenant', 'owner', { id: 'x', kind: 'operation', state: 'running', title: 'Read', input: 'path: a.ts' });
    await view.event('tenant', 'owner', { id: 'x', kind: 'operation', state: 'completed', output: 'result' });
    await view.event('tenant', 'owner', { id: 'x', kind: 'operation', state: 'running', output: 'late' });
    expect(latest().events).toEqual([{ id: 'x', kind: 'operation', state: 'completed', title: 'Read', input: 'path: a.ts', output: 'result' }]);
  });
  it('keeps sensitive file output hidden even when the end event omits the path', async () => {
    const { view, latest } = harness();
    await view.event('tenant', 'owner', { id: 'x', kind: 'operation', state: 'running', title: 'read', input: 'path: /workspace/.env' });
    await view.event('tenant', 'owner', { id: 'x', kind: 'operation', state: 'completed', output: 'arbitrary nonstandard secret' });
    expect(JSON.stringify(latest())).not.toContain('arbitrary nonstandard secret');
    expect(latest().events[0].output).toContain('不展示');
  });
  it('rejects callbacks for another recipient/message/bot and never creates a new turn', async () => {
    const { view, latest, writer } = harness();
    await view.frame('tenant', 'owner', 'activity', count);
    const callback = processCallback(latest(), 'open');
    const before = writer.mock.calls.length;
    expect(await view.callback(callback, 'intruder', '42')).toBe(false);
    expect(await view.callback(callback, 'owner', '43')).toBe(false);
    expect(await new WorkProcessView('another-bot', writer, 0).callback(callback, 'owner', '42')).toBe(false);
    expect(writer).toHaveBeenCalledTimes(before);
  });
  it('ignores an old finalizer after the next turn begins', async () => {
    const { view, latest } = harness();
    const old = view.idFor('tenant');
    await view.frame('tenant', 'owner', 'activity', count);
    view.begin('tenant');
    await view.frame('tenant', 'owner', 'status', '▶️ 已收到，开始处理…');
    await view.terminal('tenant', true, 'old final', old);
    expect(latest().summary).toContain('已收到');
    expect(latest().terminal).toBe(false);
  });
  it('shows one continuous list without splitting long operations into pages', async () => {
    const { view, latest } = harness();
    for (let i = 0; i < 8; i++) await view.event('tenant', 'owner', {
      id: String(i), kind: 'operation', state: 'completed', title: `read-${i}`, output: `result-${i}` });
    const details = workProcessDetails(latest());
    expect(details.items).toHaveLength(8);
    expect(details.items.map(item => item.detail).join('\n')).toContain('result-7');
    await view.event('tenant', 'owner', { id: 'long', kind: 'operation', state: 'completed', title: 'Test', output: '中'.repeat(8000) + 'END' });
    const record = latest();
    expect(workProcessDetails(record).items).toHaveLength(9);
    expect(workProcessDetails(record).notice).toBeUndefined();
    expect(workProcessDetails(record).items.at(-1)?.detail).toContain('END');
    expect(record.events.at(-1)?.output).toContain('END');
  });
  it('reopens saved terminal process after restart with private file permissions', async () => {
    const dir = mkdtempSync(join(tmpdir(), 'relay-process-')); roots.push(dir);
    enableWorkProcessArchive(dir);
    const { view, latest } = harness();
    await view.event('tenant', 'owner', { id: 'one', kind: 'operation', state: 'completed', output: 'kept' });
    await view.terminal('tenant', true);
    const saved = latest();
    const frames: WorkProcessRecord[] = [];
    const restarted = new WorkProcessView(saved.scope, async record => { frames.push(record); return '42'; }, 0);
    expect(await restarted.callback(processCallback(saved, 'open'), 'owner', '42')).toBe(true);
    expect(frames[0].events[0].output).toBe('kept');
    expect(statSync(join(dir, `${saved.id}.json`)).mode & 0o777).toBe(0o600);
    expect(loadWorkProcess(saved.id, 'other')).toBeUndefined();
  });
});

describe('public event projection and native presentation', () => {
  it('does not project private thinking or unclassified raw frames as public tool events', () => {
    expect(publicWorkEvents('pi', { type: 'message_update', delta: 'private thought' })).toEqual([]);
    expect(publicWorkEvents('pi', { type: 'message_end', message: { role: 'assistant', content: [{ type: 'thinking', thinking: 'private' }] } })).toEqual([]);
    expect(publicWorkEvents('codex', { type: 'item.completed', item: { id: 'x', type: 'raw_reasoning', text: 'private' } })).toEqual([]);
  });
  it('projects Pi tool start/update/end independently of the activity counter', () => {
    const start = publicWorkEvents('pi', { type: 'tool_execution_start', toolCallId: 'a', toolName: 'read', args: { path: 'src/a.ts', secret: 'not copied' } });
    const end = publicWorkEvents('pi', { type: 'tool_execution_end', toolCallId: 'a', isError: true, result: { content: [{ type: 'text', text: 'not found' }] } });
    expect(start[0].state).toBe('running');
    expect(JSON.stringify(start)).not.toContain('not copied');
    expect(end[0]).toMatchObject({ state: 'failed', id: start[0].id, output: 'not found' });
  });
  it('projects Codex command result and preserves a failed exit status', () => {
    expect(publicWorkEvents('codex', { type: 'item.completed', item: { id: 'x', type: 'command_execution', command: 'npm test', aggregated_output: 'FAIL', exit_code: 1 } })[0]).toMatchObject({ state: 'failed', title: '执行命令' });
  });
  it('projects CC and SDK tool pairs, not user text or system records', () => {
    for (const runtime of ['cc', 'cc-sdk']) {
      expect(publicWorkEvents(runtime, { type: 'assistant', message: { content: [{ type: 'tool_use', id: 'x', name: 'Read', input: { file_path: 'a.ts' } }] } })[0].state).toBe('running');
      expect(publicWorkEvents(runtime, { type: 'user', message: { content: [{ type: 'tool_result', tool_use_id: 'x', content: 'OK' }] } })[0].state).toBe('completed');
      expect(publicWorkEvents(runtime, { type: 'user', content: 'private prompt' })).toEqual([]);
    }
  });
  it('redacts credentials, escapes markup, and has only the unchanged summary while folded', async () => {
    expect(sanitizeWorkText('Authorization: Bearer abcdefgh; api_key=longsecret')).not.toContain('longsecret');
    const { view, latest } = harness();
    await view.frame('tenant', 'owner', 'activity', count);
    await view.event('tenant', 'owner', { id: '1', kind: 'operation', state: 'completed', title: '<img src=x>', output: '<b>private-tool-output</b> ```' });
    expect(telegramProcessPayload(latest()).html).toContain('已推进 5 个执行步骤');
    expect(telegramProcessPayload(latest()).html).not.toContain('private-tool-output');
    expect(JSON.stringify(feishuProcessCard(latest()))).not.toContain('private-tool-output');
    await view.callback(processCallback(latest(), 'open'), 'owner', '42');
    expect(telegramProcessPayload(latest()).html).not.toContain('private-tool-output');
    await view.callback(processCallback(latest(), `t${toolDisclosureKey('1')}o`), 'owner', '42');
    expect(telegramProcessPayload(latest()).html).toContain('&lt;b&gt;private-tool-output&lt;/b&gt;');
    expect(telegramProcessPayload(latest()).html).not.toContain('<img src=x>');
    expect(processCallback(latest(), 'p999999').length).toBeLessThanOrEqual(64);
  });
});

it('collects hidden work without redundant channel updates and counts UTF-8 storage bytes', async () => {
  const { view, writer, latest } = harness();
  await view.frame('tenant', 'owner', 'activity', count);
  for (let i = 0; i < 40; i++) {
    await view.event('tenant', 'owner', { id: String(i), kind: 'operation', state: 'completed', title: 'Read', output: '中'.repeat(31_000) });
  }
  expect(writer).toHaveBeenCalledTimes(1);
  expect(latest().omitted).toBeGreaterThan(0);
  expect(Buffer.byteLength(JSON.stringify(latest().events))).toBeLessThan(2_005_000);
  const raw = publicWorkEvents('pi', { type: 'tool_execution_start', toolCallId: 'private', toolName: 'bash', args: { command: ' '.repeat(2_100) + 'cat /workspace/.env' } });
  expect(raw[0].restricted).toBe(true);
  await view.event('tenant', 'owner', raw[0]);
  await view.event('tenant', 'owner', { id: raw[0].id, kind: 'operation', state: 'completed', output: 'secret beyond truncated input' });
  expect(JSON.stringify(latest())).not.toContain('secret beyond truncated input');
});

describe('continuous details and honest empty states', () => {
  it('has one labelled disclosure button and no page controls in either channel', async () => {
    const { view, latest } = harness();
    for (let i=0; i<8; i++) await view.event('tenant', 'owner', {
      id:String(i), kind:'operation', state:'completed', title:`read-${i}`, output:`result-${i}` });
    await view.callback(processCallback(latest(),'open'),'owner','42');
    const card=feishuProcessCard(latest()); const wire=JSON.stringify(card);
    expect(wire).toContain('⌃ 收起过程');expect(wire).toContain('"width":"auto"');
    expect(wire).not.toContain('28px');expect(wire.match(/"tag":"button"/g)).toHaveLength(9);
    expect(wire).toContain('read-0');expect(wire).toContain('read-7');expect(wire).not.toContain('result-');expect(wire).not.toMatch(/wp:[a-f0-9]+:p\d/);
    const tg=telegramProcessPayload(latest());expect((tg.reply_markup as any).inline_keyboard[0]).toHaveLength(1);
    expect(tg.text).toContain('read-0');expect(tg.text).toContain('read-7');expect(tg.text).not.toContain('result-');
    expect(await view.callback(processCallback(latest(),'p999999'),'owner','42')).toBe(true);
    expect(JSON.stringify(feishuProcessCard(latest()))).toContain('read-0');
  });
  it('distinguishes direct answers, active work and missing old records', async () => {
    const { view, latest }=harness();
    await view.frame('tenant','owner','status','accepted');
    expect(workProcessDetails(latest()).emptyText).toContain('等待');
    await view.terminal('tenant',true,'answer');
    expect(workProcessDetails(latest()).emptyText).toContain('没有工具调用');
    const old=latest();delete old.activityObserved;
    expect(workProcessDetails(old).emptyText).toContain('历史消息');
    old.activityObserved=true;
    expect(workProcessDetails(old).emptyText).toContain('没有保留');
  });
  it('respects channel budgets without paging, surrogate breaks or changing retained records', async () => {
    const { view, latest }=harness();
    for(let i=0;i<80;i++)await view.event('tenant','owner',{
      id:String(i),kind:'operation',state:'completed',title:'测🧪试'.repeat(20),output:'中🧪\\`"*'.repeat(1000) });
    await view.callback(processCallback(latest(),'open'),'owner','42');
    const record=latest(); const original=JSON.stringify(record);
    expect(Buffer.byteLength(JSON.stringify(feishuProcessCard(record)))).toBeLessThanOrEqual(26_000);
    const tg=telegramProcessPayload(record);expect(tg.text.length).toBeLessThanOrEqual(4096);
    expect(tg.text).not.toMatch(/[\uD800-\uDBFF](?![\uDC00-\uDFFF])|(?<![\uD800-\uDBFF])[\uDC00-\uDFFF]/u);
    expect(tg.text).toContain('显示上限');expect(JSON.stringify(record)).toBe(original);
  });
});
