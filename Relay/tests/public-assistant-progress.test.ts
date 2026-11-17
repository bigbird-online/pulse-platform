import { expect, it } from 'vitest';
import { publicPiAssistantParts } from '../src/public-assistant-progress.js';

const signature = JSON.stringify({ type: 'reasoning', id: 'rs', summary: [{ type: 'summary_text', text: '公开摘要。' }],
  content: [{ type: 'reasoning_text', text: 'PRIVATE_CONTENT' }], encrypted_content: 'PRIVATE_CIPHER' });
it.each(['openai-responses', 'openai-codex-responses', 'azure-openai-responses'])('selects only documented summary text for %s', api => {
  const parts = publicPiAssistantParts({ role: 'assistant', api, content: [
    { type: 'thinking', thinking: 'PRIVATE_CONTAINER', thinkingSignature: signature },
  ] });
  expect(parts).toEqual([{ kind: 'commentary', source: 'reasoning_summary', text: '公开摘要。', id: 'summary:rs' }]);
  expect(JSON.stringify(parts)).not.toContain('PRIVATE_');
});
it.each(['anthropic-messages', 'unknown', undefined])('does not reinterpret an unknown thinking container for %s', api => {
  expect(publicPiAssistantParts({ role: 'assistant', api, content: [
    { type: 'thinking', thinking: 'PRIVATE_CONTAINER', thinkingSignature: signature },
  ] })).toEqual([]);
});
it('does not fall back from an absent public summary to raw reasoning or encrypted replay', () => {
  expect(publicPiAssistantParts({ role: 'assistant', api: 'openai-codex-responses', content: [
    { type: 'thinking', thinking: 'PRIVATE_CONTAINER', thinkingSignature: JSON.stringify({
      type: 'reasoning', summary: [], content: [{ text: 'PRIVATE_CONTENT' }], encrypted_content: 'PRIVATE_CIPHER' }) },
  ] })).toEqual([]);
});
it('classifies native commentary separately while retaining all ordinary final text blocks', () => {
  const parts = publicPiAssistantParts({ role: 'assistant', content: [
    { type: 'text', text: '阶段说明。', textSignature: JSON.stringify({ v: 1, id: 'm1', phase: 'commentary' }) },
    { type: 'text', text: '最终' }, { type: 'text', text: '答复。' },
  ] });
  expect(parts.map(part => part.kind)).toEqual(['commentary', 'candidate', 'candidate']);
  expect(parts.filter(part => part.kind === 'candidate').map(part => part.text).join('')).toBe('最终答复。');
});
