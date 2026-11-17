import { afterEach, expect, it } from 'vitest';
import { mkdtempSync, rmSync, readFileSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { WorkProcessView, processCallback } from '../src/work-process-view.js';
import { enableWorkProcessArchive, loadWorkProcess, type WorkProcessRecord } from '../src/work-process-archive.js';
import { publicCommentaryEvent } from '../src/public-assistant-progress.js';
import { feishuProcessCard, telegramProcessPayload } from '../src/work-process-transports.js';
import { workProcessDetails, workProcessDocument } from '../src/work-process-content.js';
const roots: string[] = [];
afterEach(() => { enableWorkProcessArchive(); for (const root of roots.splice(0)) rmSync(root, { recursive: true, force: true }); });

it.each(['fresh', 'historic'] as const)('labels %s provider summaries without altering prose or treating them as final', async mode => {
  const root = mkdtempSync(join(tmpdir(), 'relay-source-')); roots.push(root); enableWorkProcessArchive(root);
  let saved!: WorkProcessRecord;
  const view = new WorkProcessView('test', async r => { saved = structuredClone(r); return '1'; }, 0);
  view.begin('t');
  await view.frame('t', '42', 'status', '▶️ 已收到，开始处理…');
  await view.event('t', '42', publicCommentaryEvent('pi:message:first', '先核对样本与结论的关系。'));
  const body = '**Discussing causal inference limitations**\n\n**Analyzing structural holes and network dynamics**\n\n**Outlining position audit framework**';
  await view.event('t', '42', publicCommentaryEvent('pi:summary:rs_1', body, mode === 'fresh' ? 'reasoning_summary' : undefined));
  await view.event('t', '42', { id: 'tool', kind: 'operation', state: 'completed', title: 'read', output: 'HIDDEN_RESULT' });
  await view.event('t', '42', publicCommentaryEvent('pi:message:last', '目前材料支持相关性，不足以单独确认因果。'));
  await view.terminal('t', true);
  await view.callback(processCallback(saved, 'open'), '42', '1');
  const record = loadWorkProcess(saved.id, 'test')!;
  expect(record.events[1].output).toBe(body);
  if (mode === 'fresh') expect(record.events[1].source).toBe('reasoning_summary');
  const items = workProcessDetails(record).items;
  expect(items[0].source).toBeUndefined(); expect(items[1].source).toBe('reasoning_summary');
  for (const wire of [telegramProcessPayload(record).html, JSON.stringify(feishuProcessCard(record)), workProcessDocument(record).toString()]) {
    expect(wire).toContain('原生推理摘要');
    expect(wire.indexOf('先核对')).toBeLessThan(wire.indexOf('原生推理摘要'));
    expect(wire.indexOf('Outlining position')).toBeLessThan(wire.indexOf('目前材料'));
  }
  for (const wire of [telegramProcessPayload(record).html, JSON.stringify(feishuProcessCard(record))]) expect(wire).not.toContain('HIDDEN_RESULT');
  const file = join(root, `${saved.id}.json`), corrupt = JSON.parse(readFileSync(file, 'utf8'));
  corrupt.events[1].source = 'raw_thinking'; writeFileSync(file, JSON.stringify(corrupt));
  expect(loadWorkProcess(saved.id, 'test')).toBeUndefined();
});

it('does not infer summary origin from English headings or the word summary', () => {
  const event = publicCommentaryEvent('assistant:1', '**Summary of observed results**');
  expect(event.source).toBeUndefined();
});
