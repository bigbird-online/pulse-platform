import { emitObsEvent } from './relay-observability.js';
import {
  compileRichDoc,
  renderRichDocMarkdown,
  richDocToPlainText,
  splitRichDoc,
  type RichBlock,
  type RichDoc,
  type RichInline,
} from './rich-doc.js';

export type RichDocRolloutMode = 'off' | 'shadow' | 'canary' | 'on';
export type TelegramHtmlMode = 'rich_message_html' | 'rich_html' | 'safe_html';
export type RichRenderingMode = TelegramHtmlMode | 'plain_text' | 'cardkit_markdown' | 'post_markdown' | 'legacy';

export type TelegramRichMessageProfile = {
  rollout: RichDocRolloutMode;
  canaryPercent: number;
};

export type ChannelRenderingObservation = {
  channel: 'telegram' | 'feishu';
  layer: 'compiler' | 'renderer' | 'transport';
  action: 'compile' | 'chunk' | 'rollover' | 'fallback' | 'validation' | 'terminal';
  mode?: RichRenderingMode;
  fromMode?: RichRenderingMode;
  toMode?: RichRenderingMode;
  reason?: 'format' | 'network' | 'rate_limit' | 'server_closed' | 'validation' | 'unknown';
  result?: 'valid' | 'invalid' | 'delivered' | 'failed' | 'coalesced';
  chunkCount?: number;
  rolloverCount?: number;
  terminal?: boolean;
};

export type ChannelRenderingObserver = (event: ChannelRenderingObservation) => void;

/** Persist only bounded summary/transition events; per-frame compile/chunk events
 * remain available to an injected in-process observer without creating an
 * unbounded body-correlated event stream. */
export function persistChannelRenderingObservation(event: ChannelRenderingObservation): void {
  const durable = event.terminal === true
    || event.action === 'fallback'
    || event.action === 'validation'
    || event.action === 'rollover';
  if (!durable) return;
  emitObsEvent({
    ts: new Date().toISOString(),
    evt: 'channel.rendering',
    severity: event.result === 'failed' || event.result === 'invalid' ? 'warn' : 'info',
    ...event,
  });
}

export type ChannelRichDocProfile = {
  rollout: RichDocRolloutMode;
  /** Unit used for the tested payload budget; splitting still occurs only at code-point boundaries. */
  lengthUnit: 'unicode_code_points' | 'utf16_code_units';
  /** Operationally tested payload budget, not a claim about a platform hard limit. */
  maxPayloadLength: number;
  /** Operationally tested element budget for one transport payload. */
  maxElements: number;
  /** Minimum interval used by the transport adapter when coalescing updates. */
  updateThrottleMs: number;
  /** Preferred semantic rollover point; never larger than maxPayloadLength. */
  rolloverThreshold: number;
  canaryPercent: number;
};

export type RichDocChannelProfiles = {
  telegram: ChannelRichDocProfile;
  feishu: ChannelRichDocProfile;
};

export type TelegramPayloadChunk = {
  /** In-memory semantic chunk; never a receipt or durable idempotency key. */
  document: RichDoc;
  /** Bot API Rich Message HTML, including native table blocks. */
  richMessageHtml: string;
  /** Legacy sendMessage HTML fallback; tables remain preformatted text. */
  richHtml: string;
  safeHtml: string;
  plainText: string;
};

export type TelegramPayloadCompilation = {
  document: RichDoc;
  chunks: TelegramPayloadChunk[];
};

export type FeishuPayloadChunk = {
  /** In-memory semantic chunk; never a receipt or durable idempotency key. */
  document: RichDoc;
  cardKitMarkdown: string;
  postMarkdown: string;
  plainText: string;
};

export type FeishuPayloadCompilation = {
  document: RichDoc;
  chunks: FeishuPayloadChunk[];
};

const DEFAULT_PROFILES: RichDocChannelProfiles = {
  telegram: {
    rollout: 'off',
    lengthUnit: 'utf16_code_units',
    maxPayloadLength: 4096,
    maxElements: 1,
    updateThrottleMs: 750,
    rolloverThreshold: 3800,
    canaryPercent: 0,
  },
  feishu: {
    rollout: 'off',
    lengthUnit: 'utf16_code_units',
    maxPayloadLength: 4000,
    maxElements: 20,
    updateThrottleMs: 1500,
    rolloverThreshold: 3600,
    canaryPercent: 0,
  },
};

function parseRollout(value: string | undefined, name: string): RichDocRolloutMode {
  const normalized = value?.trim().toLowerCase();
  if (!normalized || normalized === 'legacy') return 'off';
  if (normalized === 'off' || normalized === 'shadow' || normalized === 'canary' || normalized === 'on') return normalized;
  throw new Error(`${name} mode must be legacy/off, shadow, canary, or on`);
}

function parseInteger(
  value: string | undefined,
  fallback: number,
  name: string,
  minimum: number,
  maximum: number,
): number {
  if (value === undefined || value.trim() === '') return fallback;
  if (!/^\d+$/u.test(value.trim())) throw new Error(`${name} must be an integer`);
  const parsed = Number(value);
  if (!Number.isSafeInteger(parsed) || parsed < minimum || parsed > maximum) {
    throw new Error(`${name} must be between ${minimum} and ${maximum}`);
  }
  return parsed;
}

function parseLengthUnit(value: string | undefined, fallback: ChannelRichDocProfile['lengthUnit'], name: string): ChannelRichDocProfile['lengthUnit'] {
  const normalized = value?.trim().toLowerCase();
  if (!normalized) return fallback;
  if (normalized === 'unicode_code_points' || normalized === 'utf16_code_units') return normalized;
  throw new Error(`${name} length unit must be unicode_code_points or utf16_code_units`);
}

function profileFromEnv(
  env: NodeJS.ProcessEnv,
  channel: 'TELEGRAM' | 'FEISHU',
  defaults: ChannelRichDocProfile,
): ChannelRichDocProfile {
  const prefix = `RELAY_${channel}_RICHDOC_`;
  const maxPayloadLength = parseInteger(
    env[`${prefix}MAX_LENGTH`],
    defaults.maxPayloadLength,
    `${channel.toLowerCase()} max length`,
    64,
    1_000_000,
  );
  const rolloverThreshold = parseInteger(
    env[`${prefix}ROLLOVER_THRESHOLD`],
    Math.min(defaults.rolloverThreshold, maxPayloadLength),
    `${channel.toLowerCase()} rollover threshold`,
    64,
    maxPayloadLength,
  );
  return {
    rollout: parseRollout(env[`${prefix}MODE`], channel.toLowerCase()),
    lengthUnit: parseLengthUnit(env[`${prefix}LENGTH_UNIT`], defaults.lengthUnit, channel.toLowerCase()),
    maxPayloadLength,
    maxElements: parseInteger(
      env[`${prefix}MAX_ELEMENTS`],
      defaults.maxElements,
      `${channel.toLowerCase()} max elements`,
      1,
      1_000,
    ),
    updateThrottleMs: parseInteger(
      env[`${prefix}UPDATE_THROTTLE_MS`],
      defaults.updateThrottleMs,
      `${channel.toLowerCase()} update throttle`,
      50,
      60_000,
    ),
    rolloverThreshold,
    canaryPercent: parseInteger(
      env[`${prefix}CANARY_PERCENT`],
      defaults.canaryPercent,
      `${channel.toLowerCase()} canary percent`,
      0,
      100,
    ),
  };
}

export function resolveRichDocChannelProfiles(env: NodeJS.ProcessEnv = process.env): RichDocChannelProfiles {
  return {
    telegram: profileFromEnv(env, 'TELEGRAM', DEFAULT_PROFILES.telegram),
    feishu: profileFromEnv(env, 'FEISHU', DEFAULT_PROFILES.feishu),
  };
}

export function resolveTelegramRichMessageProfile(env: NodeJS.ProcessEnv = process.env): TelegramRichMessageProfile {
  return {
    rollout: parseRollout(env.RELAY_TELEGRAM_RICH_MESSAGE_MODE, 'telegram rich message'),
    canaryPercent: parseInteger(
      env.RELAY_TELEGRAM_RICH_MESSAGE_CANARY_PERCENT,
      0,
      'telegram rich message canary percent',
      0,
      100,
    ),
  };
}

function stableCanaryBucket(scope: string): number {
  // FNV-1a over the already-available opaque turn scope. Body text and body
  // hashes are deliberately excluded, and the bucket is never persisted.
  let hash = 0x811c9dc5;
  for (const point of scope) {
    hash ^= point.codePointAt(0) ?? 0;
    hash = Math.imul(hash, 0x01000193) >>> 0;
  }
  return hash % 100;
}

function isRolloutEnabled(profile: { rollout: RichDocRolloutMode; canaryPercent: number }, opaqueTurnScope: string): boolean {
  if (profile.rollout === 'on') return true;
  if (profile.rollout !== 'canary') return false;
  if (profile.canaryPercent <= 0) return false;
  if (profile.canaryPercent >= 100) return true;
  return stableCanaryBucket(opaqueTurnScope) < profile.canaryPercent;
}

export function isRichDocRolloutEnabled(profile: ChannelRichDocProfile, opaqueTurnScope: string): boolean {
  return isRolloutEnabled(profile, opaqueTurnScope);
}

export function isTelegramRichMessageRolloutEnabled(
  profile: TelegramRichMessageProfile,
  opaqueTurnScope: string,
): boolean {
  return isRolloutEnabled(profile, opaqueTurnScope);
}

function escapeHtml(value: string): string {
  return value
    .replace(/&/gu, '&amp;')
    .replace(/</gu, '&lt;')
    .replace(/>/gu, '&gt;')
    .replace(/"/gu, '&quot;');
}

function renderTelegramInlines(inlines: RichInline[], mode: TelegramHtmlMode): string {
  return inlines.map((inline) => {
    if (inline.type === 'text') {
      const escaped = escapeHtml(inline.value);
      // Rich HTML follows browser whitespace semantics: source newlines inside
      // inline content collapse unless represented by an explicit break tag.
      return mode === 'rich_message_html' ? escaped.replace(/\n/gu, '<br>') : escaped;
    }
    if (inline.type === 'code') return `<code>${escapeHtml(inline.value)}</code>`;
    if (inline.type === 'strong') return `<b>${renderTelegramInlines(inline.children, mode)}</b>`;
    if (inline.type === 'emphasis') return `<i>${renderTelegramInlines(inline.children, mode)}</i>`;
    if (inline.type === 'strikethrough') return `<s>${renderTelegramInlines(inline.children, mode)}</s>`;
    const label = renderTelegramInlines(inline.children, mode);
    if (mode === 'safe_html') {
      return inline.fallbackHref === false ? label : `${label} (${escapeHtml(inline.href)})`;
    }
    return `<a href="${escapeHtml(inline.href)}">${label}</a>`;
  }).join('');
}

function plainTable(block: Extract<RichBlock, { type: 'table' }>): string {
  const cell = (inlines: RichInline[]): string => {
    const doc: RichDoc = { type: 'doc', blocks: [{ type: 'paragraph', children: inlines }] };
    return richDocToPlainText(doc).replace(/\n/gu, ' ');
  };
  const row = (cells: RichInline[][]): string => `| ${cells.map(cell).join(' | ')} |`;
  return [row(block.header), ...block.rows.map(row)].join('\n');
}

function richTableAlignment(alignment: 'left' | 'center' | 'right' | null): string {
  return alignment ? ` align="${alignment}"` : '';
}

function renderTelegramRichTable(block: Extract<RichBlock, { type: 'table' }>): string {
  // Bot API Rich Messages accept at most 20 table columns. Keep the same
  // sanitized document readable instead of knowingly sending an invalid block.
  if (block.header.length === 0 || block.header.length > 20) return `<pre>${escapeHtml(plainTable(block))}</pre>`;
  const row = (cells: RichInline[][], tag: 'th' | 'td'): string => `<tr>${cells.map((cell, index) => (
    `<${tag}${richTableAlignment(block.align[index] ?? null)}>${renderTelegramInlines(cell, 'rich_message_html')}</${tag}>`
  )).join('')}</tr>`;
  return `<table bordered striped compact>${row(block.header, 'th')}${block.rows.map((cells) => row(cells, 'td')).join('')}</table>`;
}

function renderTelegramList(block: Extract<RichBlock, { type: 'list' }>, mode: TelegramHtmlMode): string {
  if (mode === 'rich_message_html') {
    const tag = block.ordered ? 'ol' : 'ul';
    const start = block.ordered && block.start !== undefined && block.start !== 1
      ? ` start="${block.start}"`
      : '';
    const items = block.items.map((item) => `<li>${renderTelegramBlocks(item.children, mode)}</li>`).join('');
    return `<${tag}${start}>${items}</${tag}>`;
  }
  return block.items.map((item, index) => {
    const marker = block.ordered ? `${(block.start ?? 1) + index}. ` : '• ';
    const continuation = ' '.repeat(Array.from(marker).length);
    const body = renderTelegramBlocks(item.children, mode);
    return body.split('\n').map((line, lineIndex) => `${lineIndex === 0 ? marker : continuation}${line}`).join('\n');
  }).join('\n');
}

function renderTelegramBlock(block: RichBlock, mode: TelegramHtmlMode): string {
  if (block.type === 'heading') {
    const body = renderTelegramInlines(block.children, mode);
    return mode === 'rich_message_html' ? `<h${block.level}>${body}</h${block.level}>` : `<b>${body}</b>`;
  }
  if (block.type === 'paragraph') {
    const body = renderTelegramInlines(block.children, mode);
    return mode === 'rich_message_html' ? `<p>${body}</p>` : body;
  }
  if (block.type === 'thematic_break') return mode === 'rich_message_html' ? '<hr/>' : '────────';
  if (block.type === 'list') return renderTelegramList(block, mode);
  if (block.type === 'table') {
    return mode === 'rich_message_html'
      ? renderTelegramRichTable(block)
      : `<pre>${escapeHtml(plainTable(block))}</pre>`;
  }
  if (block.type === 'quote') {
    const body = renderTelegramBlocks(block.children, mode);
    if (mode !== 'safe_html') return `<blockquote>${body}</blockquote>`;
    return body.split('\n').map((line) => `› ${line}`).join('\n');
  }
  const language = block.language ? ` class="language-${escapeHtml(block.language)}"` : '';
  return `<pre><code${mode !== 'safe_html' ? language : ''}>${escapeHtml(block.value)}</code></pre>`;
}

function renderTelegramBlocks(blocks: RichBlock[], mode: TelegramHtmlMode): string {
  const separator = mode === 'rich_message_html' ? '' : '\n\n';
  return blocks.map((block) => renderTelegramBlock(block, mode)).join(separator);
}

export function renderTelegramRichMessageHtml(doc: RichDoc): string {
  return renderTelegramBlocks(doc.blocks, 'rich_message_html');
}

export function renderTelegramRichHtml(doc: RichDoc): string {
  return renderTelegramBlocks(doc.blocks, 'rich_html');
}

export function renderTelegramSafeHtml(doc: RichDoc): string {
  return renderTelegramBlocks(doc.blocks, 'safe_html');
}

function measurePayload(payload: string, unit: ChannelRichDocProfile['lengthUnit']): number {
  return unit === 'utf16_code_units' ? payload.length : Array.from(payload).length;
}

function longestPayload(candidates: string[], unit: ChannelRichDocProfile['lengthUnit']): string {
  let longest = '';
  let length = -1;
  for (const candidate of candidates) {
    const candidateLength = measurePayload(candidate, unit);
    if (candidateLength > length) {
      longest = candidate;
      length = candidateLength;
    }
  }
  return longest;
}

export function compileTelegramPayloads(
  authoritativeSnapshot: string,
  options: { maxLength: number; lengthUnit?: ChannelRichDocProfile['lengthUnit'] },
): TelegramPayloadCompilation {
  const document = compileRichDoc(authoritativeSnapshot);
  const lengthUnit = options.lengthUnit ?? 'unicode_code_points';
  const chunks = splitRichDoc(document, {
    maxLength: options.maxLength,
    render: (doc) => longestPayload([
      renderTelegramRichMessageHtml(doc),
      renderTelegramRichHtml(doc),
      renderTelegramSafeHtml(doc),
      richDocToPlainText(doc),
    ], lengthUnit),
    measure: (payload) => measurePayload(payload, lengthUnit),
  }).map((chunk): TelegramPayloadChunk => ({
    document: chunk,
    richMessageHtml: renderTelegramRichMessageHtml(chunk),
    richHtml: renderTelegramRichHtml(chunk),
    safeHtml: renderTelegramSafeHtml(chunk),
    plainText: richDocToPlainText(chunk),
  }));
  return { document, chunks };
}

export function compileFeishuPayloads(
  authoritativeSnapshot: string,
  options: {
    maxLength: number;
    lengthUnit?: ChannelRichDocProfile['lengthUnit'];
    maxElements?: number;
  },
): FeishuPayloadCompilation {
  const document = compileRichDoc(authoritativeSnapshot);
  const lengthUnit = options.lengthUnit ?? 'unicode_code_points';
  const chunks = splitRichDoc(document, {
    maxLength: options.maxLength,
    render: (doc) => longestPayload([renderRichDocMarkdown(doc), richDocToPlainText(doc)], lengthUnit),
    measure: (payload) => measurePayload(payload, lengthUnit),
    ...(options.maxElements === undefined ? {} : {
      maxElements: options.maxElements,
      // Every semantic chunk is transported as one clean CardKit Markdown
      // element. Fenced code remains inside that element and adds no controls.
      measureElements: () => 1,
    }),
  }).map((chunk): FeishuPayloadChunk => {
    const markdown = renderRichDocMarkdown(chunk);
    return {
      document: chunk,
      cardKitMarkdown: markdown,
      postMarkdown: markdown,
      plainText: richDocToPlainText(chunk),
    };
  });
  return { document, chunks };
}
