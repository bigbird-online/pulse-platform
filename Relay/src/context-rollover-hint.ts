/**
 * Tell an Agent, once per conversation, that it should consider rolling its WI
 * over to a fresh session of itself: the context is nearly full or has been
 * compacted repeatedly. The hint rides on the next human message, so it costs
 * no extra turn; the Agent decides, and the core still checks the request.
 */
import type {StreamJsonMessage} from './types.js';
import {bindHumanMessageProof,humanMessageProof} from './collaboration-capture.js';

const DEFAULT_RATIO = 0.6;
const DEFAULT_COMPACTIONS = 2;

function positive(raw: string | undefined, fallback: number): number {
  const value = Number(raw);
  return raw !== undefined && Number.isFinite(value) && value > 0 ? value : fallback;
}

type Seen = {ratio?: number; window?: number; tokens?: number; compactions: number; hinted: boolean};

export function rolloverHintText(ratio: number | undefined, compactions: number): string {
  const used = ratio === undefined ? '' : `about ${Math.round(ratio * 100)}% of its context`;
  const folded = compactions ? `${compactions} compaction${compactions === 1 ? '' : 's'}` : '';
  return `\n\n[Host context notice, not a human decision: this conversation has used ${[used, folded].filter(Boolean).join(' and ')}. `
    + 'If you hold a WI with work remaining, at the next natural boundary record a checkpoint (done, pending, next action), '
    + 'then execute request_rollover on that WI and end the turn; the Host continues the WI in a fresh session of you.]';
}

export class ContextRolloverHints {
  private readonly seen = new Map<string, Seen>();
  constructor(private readonly env: NodeJS.ProcessEnv = process.env) {}

  /** Context facts as the runtimes report them: CC result occupancy, assistant usage, compaction boundaries. */
  observe(key: string, msg: StreamJsonMessage): void {
    const row = this.seen.get(key) ?? {compactions: 0, hinted: false};
    this.seen.set(key, row);
    const m = msg as Record<string, any>;
    if (msg.type === 'system' && m.subtype === 'compact_boundary') {
      row.compactions++;
      row.tokens = undefined;
      return;
    }
    if (msg.type === 'assistant') {
      const usage = m.message?.usage;
      const tokens = ['input_tokens', 'cache_creation_input_tokens', 'cache_read_input_tokens']
        .reduce((sum, name) => sum + (Number.isFinite(Number(usage?.[name])) ? Number(usage[name]) : 0), 0);
      if (usage && tokens > 0) row.tokens = tokens;
      if (row.tokens && row.window) row.ratio = row.tokens / row.window;
      return;
    }
    if (msg.type !== 'result') return;
    const pct = Number(m.context_window?.used_percentage);
    if (Number.isFinite(pct) && pct >= 0) { row.ratio = pct / 100; return; }
    const windows = Object.values(m.modelUsage ?? {}).map((u: any) => Number(u?.contextWindow)).filter(n => n > 0);
    if (windows.length) row.window = Math.max(...windows);
    if (row.tokens && row.window) row.ratio = row.tokens / row.window;
  }

  /** The hint to append now, at most once per conversation key. */
  take(key: string): string | undefined {
    const row = this.seen.get(key);
    if (!row || row.hinted) return undefined;
    const ratio = positive(this.env.RELAY_ROLLOVER_HINT_RATIO, DEFAULT_RATIO);
    const compactions = positive(this.env.RELAY_ROLLOVER_HINT_COMPACTIONS, DEFAULT_COMPACTIONS);
    if ((row.ratio ?? 0) < ratio && row.compactions < compactions) return undefined;
    row.hinted = true;
    return rolloverHintText(row.ratio, row.compactions);
  }

  forget(prefix: string): void {
    for (const key of this.seen.keys()) if (key.startsWith(prefix)) this.seen.delete(key);
  }
}

/** A public human message that can carry the hint: not a slash command, not Host control. */
export function carriesRolloverHint(message: StreamJsonMessage): boolean {
  if (message.type !== 'human' || message.delivery_visibility === 'control') return false;
  const text = typeof message.content === 'string' ? message.content
    : Array.isArray(message.content) ? message.content.map((b: any) => b?.type === 'text' ? b.text : '').join('') : '';
  return !/^\s*(?:\/|\[Host )/.test(text);
}

/** The same human message, with its provenance, and the hint closing its text. */
export function withRolloverHint(message: StreamJsonMessage, hint: string): StreamJsonMessage {
  const next = Array.isArray(message.content)
    ? {...message, content: [...message.content, {type: 'text', text: hint}]}
    : {...message, content: String(message.content ?? '') + hint};
  const proof = humanMessageProof(message);
  if (proof) bindHumanMessageProof(next, proof);
  return next;
}
