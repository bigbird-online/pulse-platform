import MarkdownIt from 'markdown-it';
import type Token from 'markdown-it/lib/token.mjs';

export type RichInline =
  | { type: 'text'; value: string }
  | { type: 'strong'; children: RichInline[] }
  | { type: 'emphasis'; children: RichInline[] }
  | { type: 'strikethrough'; children: RichInline[] }
  | { type: 'code'; value: string }
  | { type: 'link'; href: string; title?: string; fallbackHref?: boolean; children: RichInline[] };

export type RichListItem = {
  type: 'list_item';
  children: RichBlock[];
};

export type RichBlock =
  | { type: 'heading'; level: 1 | 2 | 3 | 4 | 5 | 6; children: RichInline[] }
  | { type: 'paragraph'; children: RichInline[] }
  | { type: 'code_block'; value: string; language?: string }
  | { type: 'list'; ordered: boolean; start?: number; items: RichListItem[] }
  | { type: 'quote'; children: RichBlock[] }
  | {
      type: 'table';
      align: Array<'left' | 'center' | 'right' | null>;
      header: RichInline[][];
      rows: RichInline[][][];
    }
  | { type: 'thematic_break' };

export type RichDoc = {
  type: 'doc';
  blocks: RichBlock[];
};

export type RichDocValidation = {
  valid: boolean;
  errors: string[];
};

export type RichDocSplitOptions = {
  /** Maximum measured units accepted by the supplied channel renderer. */
  maxLength: number;
  /** Pure renderer used only for capability-aware payload measurement. */
  render: (doc: RichDoc) => string;
  /** Channel-specific unit counter. Defaults to Unicode code points. */
  measure?: (payload: string) => number;
  /** Optional channel element budget evaluated from the shared AST. */
  maxElements?: number;
  /** Pure element counter required when maxElements is set. */
  measureElements?: (doc: RichDoc) => number;
};

const markdown = new MarkdownIt('default', {
  html: false,
  linkify: false,
  typographer: false,
  breaks: false,
});
(markdown.options as typeof markdown.options & { maxNesting: number }).maxNesting = 32;

// Parse link-shaped Markdown consistently, then apply our stricter shared
// allowlist while converting tokens. Unsafe destinations are unwrapped to inert
// label text instead of being left for each channel renderer to reinterpret.
markdown.validateLink = () => true;

const SAFE_PROTOCOLS = new Set(['http:', 'https:', 'mailto:']);
const CONTROL_CHARACTER = /[\u0000-\u001f\u007f-\u009f]/u;
const SAFE_LANGUAGE = /^[A-Za-z0-9_+#.-]{1,64}$/u;

export function sanitizeLinkTarget(rawTarget: string): string | null {
  if (!rawTarget || rawTarget !== rawTarget.trim() || CONTROL_CHARACTER.test(rawTarget)) return null;
  // Destinations longer than any tested channel atom cannot be split without
  // corrupting the link. Fail closed to inert label text instead.
  if (Array.from(rawTarget).length > 2048) return null;
  try {
    const target = new URL(rawTarget);
    if (!SAFE_PROTOCOLS.has(target.protocol.toLowerCase())) return null;
    if ((target.protocol === 'http:' || target.protocol === 'https:') && !target.hostname) return null;
    if (target.protocol === 'mailto:' && !target.pathname) return null;
    return target.href;
  } catch {
    return null;
  }
}

function appendInline(target: RichInline[], inline: RichInline): void {
  if (inline.type === 'text' && !inline.value) return;
  const previous = target.at(-1);
  if (previous?.type === 'text' && inline.type === 'text') {
    previous.value += inline.value;
    return;
  }
  target.push(inline);
}

function appendInlines(target: RichInline[], inlines: RichInline[]): void {
  for (const inline of inlines) appendInline(target, inline);
}

type InlineParseResult = { inlines: RichInline[]; next: number };

function parseInlineTokens(tokens: Token[], start = 0, stopType?: string): InlineParseResult {
  const inlines: RichInline[] = [];
  let index = start;

  while (index < tokens.length) {
    const token = tokens[index];
    if (stopType && token.type === stopType) return { inlines, next: index + 1 };

    if (token.type === 'text') {
      appendInline(inlines, { type: 'text', value: token.content });
      index += 1;
      continue;
    }
    if (token.type === 'softbreak' || token.type === 'hardbreak') {
      appendInline(inlines, { type: 'text', value: '\n' });
      index += 1;
      continue;
    }
    if (token.type === 'code_inline') {
      appendInline(inlines, { type: 'code', value: token.content });
      index += 1;
      continue;
    }
    if (token.type === 'image') {
      appendInline(inlines, { type: 'text', value: token.content });
      index += 1;
      continue;
    }

    const containers: Record<string, { close: string; type: 'strong' | 'emphasis' | 'strikethrough' }> = {
      strong_open: { close: 'strong_close', type: 'strong' },
      em_open: { close: 'em_close', type: 'emphasis' },
      s_open: { close: 's_close', type: 'strikethrough' },
    };
    const container = containers[token.type];
    if (container) {
      const nested = parseInlineTokens(tokens, index + 1, container.close);
      appendInline(inlines, { type: container.type, children: nested.inlines });
      index = nested.next;
      continue;
    }

    if (token.type === 'link_open') {
      const nested = parseInlineTokens(tokens, index + 1, 'link_close');
      const href = sanitizeLinkTarget(token.attrGet('href') ?? '');
      const title = token.attrGet('title') ?? undefined;
      if (href) appendInline(inlines, { type: 'link', href, ...(title ? { title } : {}), children: nested.inlines });
      else appendInlines(inlines, nested.inlines);
      index = nested.next;
      continue;
    }

    // html=false means raw HTML normally arrives as text. Unknown extension
    // tokens are also reduced to inert content/children rather than becoming an
    // executable or channel-specific node.
    if (token.children) appendInlines(inlines, parseInlineTokens(token.children).inlines);
    else if (token.content) appendInline(inlines, { type: 'text', value: token.content });
    index += 1;
  }

  return { inlines, next: index };
}

function tokenInlines(token: Token | undefined): RichInline[] {
  return token?.children ? parseInlineTokens(token.children).inlines : [];
}

function sanitizeLanguage(info: string): string | undefined {
  const language = info.trim().split(/\s+/u, 1)[0] ?? '';
  return SAFE_LANGUAGE.test(language) ? language : undefined;
}

type BlockCursor = { index: number };

function parseTable(tokens: Token[], cursor: BlockCursor): Extract<RichBlock, { type: 'table' }> {
  cursor.index += 1; // table_open
  let inHeader = false;
  let currentRow: RichInline[][] | null = null;
  const header: RichInline[][] = [];
  const rows: RichInline[][][] = [];
  const align: Array<'left' | 'center' | 'right' | null> = [];

  while (cursor.index < tokens.length && tokens[cursor.index].type !== 'table_close') {
    const token = tokens[cursor.index];
    if (token.type === 'thead_open') inHeader = true;
    else if (token.type === 'thead_close') inHeader = false;
    else if (token.type === 'tr_open') currentRow = [];
    else if (token.type === 'tr_close' && currentRow) {
      if (inHeader) header.push(...currentRow);
      else rows.push(currentRow);
      currentRow = null;
    } else if ((token.type === 'th_open' || token.type === 'td_open') && currentRow) {
      const inline = tokens[cursor.index + 1];
      currentRow.push(tokenInlines(inline));
      if (token.type === 'th_open') {
        const style = token.attrGet('style') ?? '';
        if (/text-align\s*:\s*center/iu.test(style)) align.push('center');
        else if (/text-align\s*:\s*right/iu.test(style)) align.push('right');
        else if (/text-align\s*:\s*left/iu.test(style)) align.push('left');
        else align.push(null);
      }
    }
    cursor.index += 1;
  }
  if (tokens[cursor.index]?.type === 'table_close') cursor.index += 1;
  return { type: 'table', align, header, rows };
}

function parseBlocks(tokens: Token[], cursor: BlockCursor, stopType?: string): RichBlock[] {
  const blocks: RichBlock[] = [];

  while (cursor.index < tokens.length) {
    const token = tokens[cursor.index];
    if (stopType && token.type === stopType) {
      cursor.index += 1;
      break;
    }

    if (token.type === 'heading_open') {
      const level = Number(token.tag.slice(1)) as 1 | 2 | 3 | 4 | 5 | 6;
      blocks.push({ type: 'heading', level, children: tokenInlines(tokens[cursor.index + 1]) });
      cursor.index += 3;
      continue;
    }
    if (token.type === 'paragraph_open') {
      blocks.push({ type: 'paragraph', children: tokenInlines(tokens[cursor.index + 1]) });
      cursor.index += 3;
      continue;
    }
    if (token.type === 'fence' || token.type === 'code_block') {
      const language = token.type === 'fence' ? sanitizeLanguage(token.info) : undefined;
      blocks.push({ type: 'code_block', value: token.content, ...(language ? { language } : {}) });
      cursor.index += 1;
      continue;
    }
    if (token.type === 'hr') {
      blocks.push({ type: 'thematic_break' });
      cursor.index += 1;
      continue;
    }
    if (token.type === 'blockquote_open') {
      cursor.index += 1;
      blocks.push({ type: 'quote', children: parseBlocks(tokens, cursor, 'blockquote_close') });
      continue;
    }
    if (token.type === 'ordered_list_open' || token.type === 'bullet_list_open') {
      const ordered = token.type === 'ordered_list_open';
      const close = ordered ? 'ordered_list_close' : 'bullet_list_close';
      const start = ordered ? Number(token.attrGet('start') ?? 1) : undefined;
      const items: RichListItem[] = [];
      cursor.index += 1;
      while (cursor.index < tokens.length && tokens[cursor.index].type !== close) {
        if (tokens[cursor.index].type !== 'list_item_open') {
          cursor.index += 1;
          continue;
        }
        cursor.index += 1;
        items.push({ type: 'list_item', children: parseBlocks(tokens, cursor, 'list_item_close') });
      }
      if (tokens[cursor.index]?.type === close) cursor.index += 1;
      blocks.push({ type: 'list', ordered, ...(ordered ? { start } : {}), items });
      continue;
    }
    if (token.type === 'table_open') {
      blocks.push(parseTable(tokens, cursor));
      continue;
    }

    // No supported source construct is silently discarded. Content from an
    // unknown block token is retained as inert paragraph text.
    if (token.content) blocks.push({ type: 'paragraph', children: [{ type: 'text', value: token.content }] });
    cursor.index += 1;
  }

  return blocks;
}

export function compileRichDoc(authoritativeSnapshot: string): RichDoc {
  const tokens = markdown.parse(authoritativeSnapshot, {});
  return { type: 'doc', blocks: parseBlocks(tokens, { index: 0 }) };
}

function hasUnpairedSurrogate(value: string): boolean {
  for (let index = 0; index < value.length; index += 1) {
    const unit = value.charCodeAt(index);
    if (unit >= 0xd800 && unit <= 0xdbff) {
      const next = value.charCodeAt(index + 1);
      if (!(next >= 0xdc00 && next <= 0xdfff)) return true;
      index += 1;
    } else if (unit >= 0xdc00 && unit <= 0xdfff) return true;
  }
  return false;
}

export function validateRichDoc(doc: RichDoc): RichDocValidation {
  const errors: string[] = [];
  const seen = new WeakSet<object>();
  const checkString = (value: string, path: string): void => {
    if (hasUnpairedSurrogate(value)) errors.push(`${path}: unpaired surrogate`);
  };
  const checkInlines = (inlines: RichInline[], path: string): void => {
    for (let index = 0; index < inlines.length; index += 1) {
      const inline = inlines[index];
      const itemPath = `${path}[${index}]`;
      if (seen.has(inline)) {
        errors.push(`${itemPath}: repeated or cyclic node`);
        continue;
      }
      seen.add(inline);
      if (inline.type === 'text' || inline.type === 'code') checkString(inline.value, `${itemPath}.value`);
      else {
        if (inline.type === 'link') {
          if (sanitizeLinkTarget(inline.href) !== inline.href) errors.push(`${itemPath}.href: unsafe target`);
          if (inline.title) checkString(inline.title, `${itemPath}.title`);
        }
        checkInlines(inline.children, `${itemPath}.children`);
      }
    }
  };
  const checkBlocks = (blocks: RichBlock[], path: string): void => {
    for (let index = 0; index < blocks.length; index += 1) {
      const block = blocks[index];
      const itemPath = `${path}[${index}]`;
      if (seen.has(block)) {
        errors.push(`${itemPath}: repeated or cyclic node`);
        continue;
      }
      seen.add(block);
      if (block.type === 'heading') {
        if (!Number.isInteger(block.level) || block.level < 1 || block.level > 6) errors.push(`${itemPath}.level: invalid`);
        checkInlines(block.children, `${itemPath}.children`);
      } else if (block.type === 'paragraph') checkInlines(block.children, `${itemPath}.children`);
      else if (block.type === 'code_block') {
        checkString(block.value, `${itemPath}.value`);
        if (block.language && !SAFE_LANGUAGE.test(block.language)) errors.push(`${itemPath}.language: invalid`);
      } else if (block.type === 'quote') checkBlocks(block.children, `${itemPath}.children`);
      else if (block.type === 'list') {
        if (block.ordered && (!Number.isInteger(block.start) || (block.start ?? 0) < 1)) errors.push(`${itemPath}.start: invalid`);
        block.items.forEach((item, itemIndex) => checkBlocks(item.children, `${itemPath}.items[${itemIndex}].children`));
      } else if (block.type === 'table') {
        if (block.header.length !== block.align.length) errors.push(`${itemPath}: header/alignment width mismatch`);
        block.header.forEach((cell, cellIndex) => checkInlines(cell, `${itemPath}.header[${cellIndex}]`));
        block.rows.forEach((row, rowIndex) => {
          if (row.length !== block.header.length) errors.push(`${itemPath}.rows[${rowIndex}]: width mismatch`);
          row.forEach((cell, cellIndex) => checkInlines(cell, `${itemPath}.rows[${rowIndex}][${cellIndex}]`));
        });
      }
    }
  };

  if (!doc || doc.type !== 'doc' || !Array.isArray(doc.blocks)) errors.push('doc: invalid root');
  else checkBlocks(doc.blocks, 'doc.blocks');
  return { valid: errors.length === 0, errors };
}

function escapeMarkdownText(value: string): string {
  return value
    .replace(/&/gu, '&amp;')
    .replace(/</gu, '&lt;')
    .replace(/>/gu, '&gt;')
    .replace(/([\\`*_[\]{}()#+\-.!|~="'])/gu, '\\$1');
}

function renderInlineCode(value: string): string {
  const longest = Math.max(0, ...Array.from(value.matchAll(/`+/gu), (match) => match[0].length));
  const fence = '`'.repeat(Math.max(1, longest + 1));
  const needsPadding = /^\s|\s$/u.test(value) || value.startsWith('`') || value.endsWith('`');
  return `${fence}${needsPadding ? ' ' : ''}${value}${needsPadding ? ' ' : ''}${fence}`;
}

function renderInlines(inlines: RichInline[]): string {
  return inlines.map((inline) => {
    if (inline.type === 'text') return escapeMarkdownText(inline.value);
    if (inline.type === 'code') return renderInlineCode(inline.value);
    if (inline.type === 'strong') return `**${renderInlines(inline.children)}**`;
    if (inline.type === 'emphasis') return `_${renderInlines(inline.children)}_`;
    if (inline.type === 'strikethrough') return `~~${renderInlines(inline.children)}~~`;
    const title = inline.title ? ` \"${inline.title.replace(/[\\"]/gu, '\\$&')}\"` : '';
    return `[${renderInlines(inline.children)}](<${inline.href.replace(/>/gu, '%3E')}>${title})`;
  }).join('');
}

function longestBacktickRun(value: string): number {
  return Math.max(0, ...Array.from(value.matchAll(/`+/gu), (match) => match[0].length));
}

function renderList(block: Extract<RichBlock, { type: 'list' }>): string {
  return block.items.map((item, index) => {
    const marker = block.ordered ? `${(block.start ?? 1) + index}. ` : '- ';
    const continuation = ' '.repeat(marker.length);
    const body = renderBlocks(item.children);
    const lines = body.split('\n');
    return lines.map((line, lineIndex) => `${lineIndex === 0 ? marker : continuation}${line}`).join('\n');
  }).join('\n');
}

function renderTable(block: Extract<RichBlock, { type: 'table' }>): string {
  const row = (cells: RichInline[][]): string => `| ${cells.map(renderInlines).join(' | ')} |`;
  const separator = `| ${block.align.map((alignment) => {
    if (alignment === 'left') return ':---';
    if (alignment === 'right') return '---:';
    if (alignment === 'center') return ':---:';
    return '---';
  }).join(' | ')} |`;
  return [row(block.header), separator, ...block.rows.map(row)].join('\n');
}

function renderBlock(block: RichBlock): string {
  if (block.type === 'heading') return `${'#'.repeat(block.level)} ${renderInlines(block.children)}`;
  if (block.type === 'paragraph') return renderInlines(block.children);
  if (block.type === 'thematic_break') return '---';
  if (block.type === 'list') return renderList(block);
  if (block.type === 'table') return renderTable(block);
  if (block.type === 'quote') return renderBlocks(block.children).split('\n').map((line) => `> ${line}`).join('\n');
  const fence = '`'.repeat(Math.max(3, longestBacktickRun(block.value) + 1));
  const body = block.value.endsWith('\n') ? block.value : `${block.value}\n`;
  return `${fence}${block.language ?? ''}\n${body}${fence}`;
}

function renderBlocks(blocks: RichBlock[]): string {
  return blocks.map(renderBlock).join('\n\n');
}

export function renderRichDocMarkdown(doc: RichDoc): string {
  const validation = validateRichDoc(doc);
  if (!validation.valid) throw new Error(`Invalid RichDoc: ${validation.errors.join('; ')}`);
  return renderBlocks(doc.blocks);
}

function plainInlines(inlines: RichInline[]): string {
  return inlines.map((inline) => {
    if (inline.type === 'text' || inline.type === 'code') return inline.value;
    if (inline.type === 'link') {
      return inline.fallbackHref === false
        ? plainInlines(inline.children)
        : `${plainInlines(inline.children)} (${inline.href})`;
    }
    return plainInlines(inline.children);
  }).join('');
}

function plainBlock(block: RichBlock): string {
  if (block.type === 'heading' || block.type === 'paragraph') return plainInlines(block.children);
  if (block.type === 'code_block') return block.value;
  if (block.type === 'thematic_break') return '---';
  if (block.type === 'quote') return plainBlocks(block.children).split('\n').map((line) => `> ${line}`).join('\n');
  if (block.type === 'list') {
    return block.items.map((item, index) => {
      const marker = block.ordered ? `${(block.start ?? 1) + index}. ` : '- ';
      const continuation = ' '.repeat(marker.length);
      return plainBlocks(item.children).split('\n').map((line, lineIndex) => `${lineIndex === 0 ? marker : continuation}${line}`).join('\n');
    }).join('\n');
  }
  const row = (cells: RichInline[][]): string => cells.map(plainInlines).join('\t');
  return [row(block.header), ...block.rows.map(row)].join('\n');
}

function plainBlocks(blocks: RichBlock[]): string {
  return blocks.map(plainBlock).join('\n\n');
}

export function collectRichDocCodeBlocks(
  doc: RichDoc,
): Array<Extract<RichBlock, { type: 'code_block' }>> {
  const result: Array<Extract<RichBlock, { type: 'code_block' }>> = [];
  const visit = (blocks: RichBlock[]): void => {
    for (const block of blocks) {
      if (block.type === 'code_block') result.push(block);
      else if (block.type === 'quote') visit(block.children);
      else if (block.type === 'list') {
        for (const item of block.items) visit(item.children);
      }
    }
  };
  visit(doc.blocks);
  return result;
}

export function richDocToPlainText(doc: RichDoc): string {
  const validation = validateRichDoc(doc);
  if (!validation.valid) throw new Error(`Invalid RichDoc: ${validation.errors.join('; ')}`);
  return plainBlocks(doc.blocks);
}

function renderedLength(doc: RichDoc, options: RichDocSplitOptions): number {
  const payload = options.render(doc);
  return options.measure ? options.measure(payload) : Array.from(payload).length;
}

function fitsDocument(doc: RichDoc, options: RichDocSplitOptions): boolean {
  if (renderedLength(doc, options) > options.maxLength) return false;
  if (options.maxElements === undefined) return true;
  return (options.measureElements?.(doc) ?? 0) <= options.maxElements;
}

function asDoc(blocks: RichBlock[]): RichDoc {
  return { type: 'doc', blocks };
}

function cannotFit(kind: string, options: RichDocSplitOptions): never {
  const elementBudget = options.maxElements === undefined ? '' : ` maxElements=${options.maxElements}`;
  throw new Error(`RichDoc ${kind} cannot fit maxLength=${options.maxLength}${elementBudget}`);
}

function largestFittingPrefix(
  value: string,
  fits: (prefix: string) => boolean,
  kind: string,
  options: RichDocSplitOptions,
): [string, string] {
  const points = Array.from(value);
  let low = 1;
  let high = points.length;
  let best = 0;
  while (low <= high) {
    const middle = Math.floor((low + high) / 2);
    if (fits(points.slice(0, middle).join(''))) {
      best = middle;
      low = middle + 1;
    } else high = middle - 1;
  }
  if (best === 0) cannotFit(kind, options);

  // Prefer a complete textual boundary without ever sacrificing progress.
  const prefix = points.slice(0, best).join('');
  const boundary = Math.max(prefix.lastIndexOf('\n') + 1, prefix.lastIndexOf(' ') + 1, prefix.lastIndexOf('\t') + 1);
  const preferredCut = boundary >= Math.ceil(best / 2) ? Array.from(prefix.slice(0, boundary)).length : best;
  const cut = fits(points.slice(0, preferredCut).join('')) ? preferredCut : best;
  return [points.slice(0, cut).join(''), points.slice(cut).join('')];
}

function splitTextInline(
  inline: Extract<RichInline, { type: 'text' }>,
  makeBlock: (children: RichInline[]) => RichBlock,
  options: RichDocSplitOptions,
): RichInline[] {
  const pieces: RichInline[] = [];
  let remaining = inline.value;
  while (remaining) {
    const [head, tail] = largestFittingPrefix(
      remaining,
      (candidate) => renderedLength(asDoc([makeBlock([{ type: 'text', value: candidate }])]), options) <= options.maxLength,
      'text node',
      options,
    );
    pieces.push({ type: 'text', value: head });
    remaining = tail;
  }
  return pieces;
}

function splitCodeInline(
  inline: Extract<RichInline, { type: 'code' }>,
  makeBlock: (children: RichInline[]) => RichBlock,
  options: RichDocSplitOptions,
): RichInline[] {
  const pieces: RichInline[] = [];
  let remaining = inline.value;
  while (remaining) {
    const [head, tail] = largestFittingPrefix(
      remaining,
      (candidate) => renderedLength(asDoc([makeBlock([{ type: 'code', value: candidate }])]), options) <= options.maxLength,
      'inline code',
      options,
    );
    pieces.push({ type: 'code', value: head });
    remaining = tail;
  }
  return pieces;
}

function splitInlineChildren(
  inlines: RichInline[],
  makeBlock: (children: RichInline[]) => RichBlock,
  options: RichDocSplitOptions,
): RichInline[][] {
  const groups: RichInline[][] = [];
  let current: RichInline[] = [];
  const fits = (children: RichInline[]): boolean => renderedLength(asDoc([makeBlock(children)]), options) <= options.maxLength;
  const flush = (): void => {
    if (current.length > 0) groups.push(current);
    current = [];
  };

  const addPiece = (piece: RichInline): void => {
    if (fits([...current, piece])) {
      current.push(piece);
      return;
    }
    flush();
    if (!fits([piece])) cannotFit(`${piece.type} inline`, options);
    current = [piece];
  };

  for (const inline of inlines) {
    if (fits([...current, inline])) {
      current.push(inline);
      continue;
    }
    flush();
    if (fits([inline])) {
      current = [inline];
      continue;
    }
    if (inline.type === 'text') {
      splitTextInline(inline, makeBlock, options).forEach(addPiece);
      continue;
    }
    if (inline.type === 'code') {
      splitCodeInline(inline, makeBlock, options).forEach(addPiece);
      continue;
    }
    if (inline.type === 'strong' || inline.type === 'emphasis' || inline.type === 'strikethrough') {
      const wrap = (children: RichInline[]): RichInline => ({ type: inline.type, children });
      const nestedGroups = splitInlineChildren(inline.children, (children) => makeBlock([wrap(children)]), options);
      nestedGroups.map(wrap).forEach(addPiece);
      continue;
    }
    if (inline.type === 'link') {
      const wrap = (children: RichInline[], fallbackHref: boolean): RichInline => ({
        ...inline,
        children,
        fallbackHref,
      });
      const nestedGroups = splitInlineChildren(
        inline.children,
        (children) => makeBlock([wrap(children, false)]),
        options,
      );
      nestedGroups.forEach((children, index) => {
        addPiece(wrap(children, index === nestedGroups.length - 1 && inline.fallbackHref !== false));
      });
      continue;
    }
    cannotFit('unknown inline', options);
  }
  flush();
  return groups;
}

function splitInlineBlock(
  block: Extract<RichBlock, { type: 'paragraph' | 'heading' }>,
  options: RichDocSplitOptions,
): RichBlock[] {
  const makeBlock = (children: RichInline[]): RichBlock => block.type === 'heading'
    ? { type: 'heading', level: block.level, children }
    : { type: 'paragraph', children };
  return splitInlineChildren(block.children, makeBlock, options).map(makeBlock);
}

function splitCodeBlock(block: Extract<RichBlock, { type: 'code_block' }>, options: RichDocSplitOptions): RichBlock[] {
  const pieces: RichBlock[] = [];
  let remaining = block.value;
  if (!remaining) cannotFit('empty code block', options);
  while (remaining) {
    const [head, tail] = largestFittingPrefix(
      remaining,
      (candidate) => renderedLength(asDoc([{ ...block, value: candidate }]), options) <= options.maxLength,
      'code block',
      options,
    );
    pieces.push({ ...block, value: head });
    remaining = tail;
  }
  return pieces;
}

function splitQuote(block: Extract<RichBlock, { type: 'quote' }>, options: RichDocSplitOptions): RichBlock[] {
  const nested = splitRichDoc(asDoc(block.children), {
    ...options,
    render: (doc) => options.render(asDoc([{ type: 'quote', children: doc.blocks }])),
  });
  return nested.map((doc) => ({ type: 'quote', children: doc.blocks }));
}

function listAt(
  block: Extract<RichBlock, { type: 'list' }>,
  items: RichListItem[],
  offset: number,
): Extract<RichBlock, { type: 'list' }> {
  return {
    type: 'list',
    ordered: block.ordered,
    ...(block.ordered ? { start: (block.start ?? 1) + offset } : {}),
    items,
  };
}

function splitList(block: Extract<RichBlock, { type: 'list' }>, options: RichDocSplitOptions): RichBlock[] {
  const pieces: RichBlock[] = [];
  let current: RichListItem[] = [];
  let currentOffset = 0;
  const flush = (): void => {
    if (current.length > 0) pieces.push(listAt(block, current, currentOffset));
    current = [];
  };

  for (let index = 0; index < block.items.length; index += 1) {
    const item = block.items[index];
    const candidate = listAt(block, [...current, item], current.length > 0 ? currentOffset : index);
    if (renderedLength(asDoc([candidate]), options) <= options.maxLength) {
      if (current.length === 0) currentOffset = index;
      current.push(item);
      continue;
    }
    flush();
    const single = listAt(block, [item], index);
    if (renderedLength(asDoc([single]), options) <= options.maxLength) {
      currentOffset = index;
      current = [item];
      continue;
    }
    const nested = splitRichDoc(asDoc(item.children), {
      ...options,
      render: (doc) => options.render(asDoc([listAt(block, [{ type: 'list_item', children: doc.blocks }], index)])),
    });
    for (const chunk of nested) pieces.push(listAt(block, [{ type: 'list_item', children: chunk.blocks }], index));
  }
  flush();
  return pieces;
}

function splitTable(block: Extract<RichBlock, { type: 'table' }>, options: RichDocSplitOptions): RichBlock[] {
  const continuationHeader: RichInline[][] = block.header.map(() => []);
  if (block.rows.length === 0) cannotFit('table header', options);
  const pieces: RichBlock[] = [];
  let header = block.header;
  let rows: RichInline[][][] = [];
  const makeTable = (tableHeader: RichInline[][], tableRows: RichInline[][][]): Extract<RichBlock, { type: 'table' }> => ({
    ...block,
    header: tableHeader,
    rows: tableRows,
  });
  const flush = (): void => {
    if (rows.length > 0) {
      pieces.push(makeTable(header, rows));
      header = continuationHeader;
    }
    rows = [];
  };

  for (const row of block.rows) {
    const candidate = makeTable(header, [...rows, row]);
    if (renderedLength(asDoc([candidate]), options) <= options.maxLength) {
      rows.push(row);
      continue;
    }
    flush();
    const single = makeTable(header, [row]);
    if (renderedLength(asDoc([single]), options) <= options.maxLength) {
      rows = [row];
      continue;
    }

    // Preserve the source header exactly once, then use empty structural
    // headers for continuation tables. This keeps every chunk valid Markdown
    // without duplicating visible answer content.
    if (pieces.length === 0) {
      const headerOnly = makeTable(block.header, []);
      if (renderedLength(asDoc([headerOnly]), options) > options.maxLength) cannotFit('table header', options);
      pieces.push(headerOnly);
      header = continuationHeader;
    }

    let emittedCell = false;
    for (let cellIndex = 0; cellIndex < row.length; cellIndex += 1) {
      const cell = row[cellIndex];
      if (cell.length === 0) continue;
      const makeCellTable = (children: RichInline[]): RichBlock => {
        const cells: RichInline[][] = row.map(() => []);
        cells[cellIndex] = children;
        return makeTable(continuationHeader, [cells]);
      };
      const groups = splitInlineChildren(cell, makeCellTable, options);
      for (const group of groups) {
        pieces.push(makeCellTable(group));
        emittedCell = true;
      }
    }
    if (!emittedCell) cannotFit('empty table row', options);
  }
  flush();
  return pieces;
}

function splitBlock(block: RichBlock, options: RichDocSplitOptions): RichBlock[] {
  if (block.type === 'paragraph' || block.type === 'heading') return splitInlineBlock(block, options);
  if (block.type === 'code_block') return splitCodeBlock(block, options);
  if (block.type === 'quote') return splitQuote(block, options);
  if (block.type === 'list') return splitList(block, options);
  if (block.type === 'table') return splitTable(block, options);
  cannotFit(block.type, options);
}

export function splitRichDoc(doc: RichDoc, options: RichDocSplitOptions): RichDoc[] {
  if (!Number.isSafeInteger(options.maxLength) || options.maxLength < 1) {
    throw new Error('RichDoc maxLength must be a positive safe integer');
  }
  if (options.maxElements !== undefined && (
    !Number.isSafeInteger(options.maxElements)
    || options.maxElements < 1
    || typeof options.measureElements !== 'function'
  )) {
    throw new Error('RichDoc maxElements requires a positive safe integer and measureElements');
  }
  const validation = validateRichDoc(doc);
  if (!validation.valid) throw new Error(`Invalid RichDoc: ${validation.errors.join('; ')}`);
  if (doc.blocks.length === 0) return [];

  const chunks: RichDoc[] = [];
  let current: RichBlock[] = [];
  const flush = (): void => {
    if (current.length > 0) chunks.push(asDoc(current));
    current = [];
  };

  for (const block of doc.blocks) {
    const candidate = asDoc([...current, block]);
    if (fitsDocument(candidate, options)) {
      current.push(block);
      continue;
    }
    flush();
    if (fitsDocument(asDoc([block]), options)) {
      current = [block];
      continue;
    }
    const pieces = splitBlock(block, options);
    for (let index = 0; index < pieces.length; index += 1) {
      const piece = pieces[index];
      if (!fitsDocument(asDoc([piece]), options)) cannotFit(piece.type, options);
      if (index === pieces.length - 1) current = [piece];
      else chunks.push(asDoc([piece]));
    }
  }
  flush();

  for (const chunk of chunks) {
    if (!fitsDocument(chunk, options)) cannotFit('chunk', options);
  }
  return chunks;
}
