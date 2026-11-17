import { sanitizeWorkText, type WorkProcessEvent } from './work-process-events.js';

/** Only completed public assistant text enters the work-process view. */
export function publicCommentaryEvent(id: string, output: string, source?: WorkProcessEvent['source']): WorkProcessEvent {
  return { id: id.slice(0, 160), kind: 'commentary', state: 'completed',
    ...(source ? { source } : {}), output: sanitizeWorkText(output, 512_000) };
}

export type PublicAssistantPart = { kind: 'commentary' | 'candidate'; text: string; id?: string; source?: WorkProcessEvent['source'] };
const object = (value: unknown): Record<string, unknown> => value && typeof value === 'object' && !Array.isArray(value)
  ? value as Record<string, unknown> : {};
function signature(value: unknown): Record<string, unknown> {
  if (typeof value !== 'string' || value.length > 2_000_000) return {};
  try { return object(JSON.parse(value)); } catch { return {}; }
}
/** The pinned Responses adapter stores public summary AND private replay data
 * together. Select only summary_text; never copy thinking/content/encrypted data.
 * Source: Pi 914cf147, ai/src/api/openai-responses-shared.ts output_item.done. */
export function publicPiAssistantParts(value: unknown): PublicAssistantPart[] {
  const message = object(value);
  if (message.role !== 'assistant' || !Array.isArray(message.content)) return [];
  const responses = ['openai-responses', 'openai-codex-responses', 'azure-openai-responses'].includes(String(message.api));
  return message.content.flatMap((value): PublicAssistantPart[] => {
    const block = object(value);
    if (block.type === 'text' && typeof block.text === 'string') {
      const mark = signature(block.textSignature);
      if (mark.v === 1 && mark.phase !== undefined && !['commentary', 'final_answer'].includes(String(mark.phase))) return [];
      return [{ kind: mark.v === 1 && mark.phase === 'commentary' ? 'commentary' : 'candidate',
        text: block.text, ...(typeof mark.id === 'string' ? { id: mark.id } : {}) }];
    }
    if (responses && block.type === 'thinking') {
      const item = signature(block.thinkingSignature);
      if (item.type !== 'reasoning' || !Array.isArray(item.summary)) return [];
      const text = item.summary.map(object).filter(part => part.type === 'summary_text' && typeof part.text === 'string')
        .map(part => part.text).join('\n\n');
      return text ? [{ kind: 'commentary', source: 'reasoning_summary', text, ...(typeof item.id === 'string' ? { id: `summary:${item.id}` } : {}) }] : [];
    }
    return [];
  });
}
