import { describe, expect, it } from 'vitest';
import {
  compileRichDoc,
  renderRichDocMarkdown,
  richDocToPlainText,
  sanitizeLinkTarget,
  splitRichDoc,
  validateRichDoc,
  type RichBlock,
  type RichDoc,
  type RichInline,
} from '../src/rich-doc.js';

function visitInlines(doc: RichDoc): RichInline[] {
  const found: RichInline[] = [];
  const visitInline = (inline: RichInline): void => {
    found.push(inline);
    if ('children' in inline) inline.children.forEach(visitInline);
  };
  const visitBlock = (block: RichBlock): void => {
    if (block.type === 'heading' || block.type === 'paragraph') block.children.forEach(visitInline);
    if (block.type === 'list') block.items.forEach((item) => item.children.forEach(visitBlock));
    if (block.type === 'quote') block.children.forEach(visitBlock);
    if (block.type === 'table') {
      block.header.flat().forEach(visitInline);
      block.rows.flat(2).forEach(visitInline);
    }
  };
  doc.blocks.forEach(visitBlock);
  return found;
}

function semanticLeafText(doc: RichDoc): string {
  const values: string[] = [];
  const inline = (node: RichInline): void => {
    if (node.type === 'text' || node.type === 'code') values.push(node.value);
    else node.children.forEach(inline);
  };
  const block = (node: RichBlock): void => {
    if (node.type === 'paragraph' || node.type === 'heading') node.children.forEach(inline);
    else if (node.type === 'code_block') values.push(node.value);
    else if (node.type === 'quote') node.children.forEach(block);
    else if (node.type === 'list') node.items.forEach((item) => item.children.forEach(block));
    else if (node.type === 'table') {
      node.header.flat().forEach(inline);
      node.rows.flat(2).forEach(inline);
    }
  };
  doc.blocks.forEach(block);
  return values.join('');
}

function codeText(doc: RichDoc): string {
  const values: string[] = [];
  const visit = (block: RichBlock): void => {
    if (block.type === 'code_block') values.push(block.value);
    if (block.type === 'quote') block.children.forEach(visit);
    if (block.type === 'list') block.items.forEach((item) => item.children.forEach(visit));
  };
  doc.blocks.forEach(visit);
  return values.join('');
}

function expectClosedFences(payload: string): void {
  let open: { marker: string; length: number } | null = null;
  for (const line of payload.split('\n')) {
    const match = /^( {0,3})(`{3,}|~{3,})(.*)$/.exec(line);
    if (!match) continue;
    const marker = match[2][0];
    const length = match[2].length;
    if (!open) open = { marker, length };
    else if (marker === open.marker && length >= open.length && match[3].trim() === '') open = null;
  }
  expect(open).toBeNull();
}

describe('channel-neutral RichDoc compiler', () => {
  it('normalizes all supported block and inline syntax into one typed semantic document', () => {
    const source = [
      '# Heading',
      '',
      'Paragraph with **bold**, *italic*, ~~removed~~, `inline code`, and [safe link](https://example.com/a?b=1).',
      '',
      '> Quoted **text**',
      '> continues here.',
      '',
      '1. ordered one',
      '2. ordered two',
      '   - nested bullet',
      '   - nested `code`',
      '',
      '- unordered one',
      '- unordered two',
      '',
      '| Name | Value |',
      '| :--- | ---: |',
      '| alpha | **one** |',
      '| beta | two |',
      '',
      '---',
      '',
      '```ts',
      'const answer = 42;',
      '```',
    ].join('\n');

    const doc = compileRichDoc(source);

    expect(validateRichDoc(doc)).toEqual({ valid: true, errors: [] });
    expect(doc.blocks.map((block) => block.type)).toEqual([
      'heading',
      'paragraph',
      'quote',
      'list',
      'list',
      'table',
      'thematic_break',
      'code_block',
    ]);
    expect(doc.blocks[0]).toMatchObject({ type: 'heading', level: 1 });
    expect(doc.blocks[3]).toMatchObject({ type: 'list', ordered: true, start: 1 });
    expect(doc.blocks[4]).toMatchObject({ type: 'list', ordered: false });
    expect(doc.blocks[5]).toMatchObject({ type: 'table', align: ['left', 'right'] });
    expect(doc.blocks[7]).toMatchObject({ type: 'code_block', language: 'ts', value: 'const answer = 42;\n' });

    const inlineTypes = new Set(visitInlines(doc).map((inline) => inline.type));
    expect(inlineTypes).toEqual(new Set(['text', 'strong', 'emphasis', 'strikethrough', 'code', 'link']));
    expect(visitInlines(doc).find((inline) => inline.type === 'link')).toMatchObject({
      type: 'link',
      href: 'https://example.com/a?b=1',
    });
  });

  it('uses a strict common URL allowlist and fails closed for unsafe or ambiguous targets', () => {
    expect(sanitizeLinkTarget('https://example.com/a?b=1#c')).toBe('https://example.com/a?b=1#c');
    expect(sanitizeLinkTarget('http://example.com')).toBe('http://example.com/');
    expect(sanitizeLinkTarget('mailto:person@example.com')).toBe('mailto:person@example.com');

    for (const unsafe of [
      'javascript:alert(1)',
      'JaVaScRiPt:alert(1)',
      'java\u0000script:alert(1)',
      'vbscript:msgbox(1)',
      'data:text/html,<script>alert(1)</script>',
      'file:///etc/passwd',
      '//example.com/scheme-relative',
      '/relative/path',
      'https://exa mple.com',
      'custom:opaque',
      `https://example.com/${'x'.repeat(2049)}`,
    ]) {
      expect(sanitizeLinkTarget(unsafe), unsafe).toBeNull();
    }
  });

  it('treats raw HTML and unsafe links as inert text in every fallback representation', () => {
    const source = [
      '<script>alert("x")</script>',
      '',
      '<img src=x onerror=alert(1)>',
      '',
      '[unsafe](javascript:alert(1)) [unknown](custom:payload) [safe](https://example.com)',
    ].join('\n');
    const doc = compileRichDoc(source);
    const rendered = renderRichDocMarkdown(doc);
    const plain = richDocToPlainText(doc);

    expect(validateRichDoc(doc)).toEqual({ valid: true, errors: [] });
    expect(visitInlines(doc).filter((inline) => inline.type === 'link')).toHaveLength(1);
    expect(visitInlines(doc).find((inline) => inline.type === 'link')).toMatchObject({
      type: 'link',
      href: 'https://example.com/',
    });
    expect(rendered).not.toMatch(/<script|<img|javascript:|custom:/i);
    expect(rendered).toContain(String.raw`&lt;script&gt;alert\(\"x\"\)&lt;/script&gt;`);
    expect(plain).toContain('<script>alert("x")</script>');
    expect(plain).toContain('unsafe');
  });

  it('compiles every incomplete streaming prefix independently into a valid closed payload', () => {
    const samples = [
      '**half bold then complete**',
      '*half emphasis then complete*',
      '[label](https://example.com/a_(nested))',
      '| a | b |\n| --- | --- |\n| one | two |',
      '1. outer\n   - nested\n   - second',
      '> quote with **formatting**',
      '```ts\nconst emoji = "🦅";\nconsole.log(emoji);\n```',
      '<script>alert(1)</script>',
    ];

    for (const sample of samples) {
      const codePoints = Array.from(sample);
      for (let end = 0; end <= codePoints.length; end += 1) {
        const snapshot = codePoints.slice(0, end).join('');
        const doc = compileRichDoc(snapshot);
        const payload = renderRichDocMarkdown(doc);
        expect(validateRichDoc(doc), JSON.stringify({ sample, end })).toEqual({ valid: true, errors: [] });
        expect(payload, JSON.stringify({ sample, end })).not.toContain('\uFFFD');
        expect(payload, JSON.stringify({ sample, end })).not.toMatch(/<script/i);
        expectClosedFences(payload);
      }
    }
  });
});

describe('channel-neutral semantic splitter', () => {
  it('prefers complete block boundaries and keeps links, lists, and tables structurally intact', () => {
    const source = [
      'First paragraph with [a safe link](https://example.com/path).',
      '',
      '- outer one',
      '  - nested child',
      '- outer two',
      '',
      '| Key | Value |',
      '| --- | --- |',
      '| alpha | one |',
      '',
      'Last paragraph.',
    ].join('\n');
    const doc = compileRichDoc(source);
    const render = renderRichDocMarkdown;
    const maxLength = Math.max(...doc.blocks.map((block) => Array.from(render({ type: 'doc', blocks: [block] })).length));
    const chunks = splitRichDoc(doc, { maxLength, render });

    expect(chunks.length).toBeGreaterThan(1);
    expect(chunks.flatMap((chunk) => chunk.blocks).map((block) => block.type)).toEqual(
      doc.blocks.map((block) => block.type),
    );
    expect(chunks.every((chunk) => Array.from(render(chunk)).length <= maxLength)).toBe(true);
    expect(chunks.flatMap(visitInlines).filter((inline) => inline.type === 'link')).toHaveLength(1);
    expect(chunks.flatMap((chunk) => chunk.blocks).filter((block) => block.type === 'list')).toHaveLength(1);
    expect(chunks.flatMap((chunk) => chunk.blocks).filter((block) => block.type === 'table')).toHaveLength(1);
  });

  it('hard-splits an indivisible long code block by code point and closes/reopens its fence', () => {
    const code = Array.from({ length: 80 }, (_, index) => `行${index} = "🦅-${index}";`).join('\n') + '\n';
    const doc = compileRichDoc(`\`\`\`typescript\n${code}\`\`\``);
    const maxLength = 120;
    const chunks = splitRichDoc(doc, { maxLength, render: renderRichDocMarkdown });

    expect(chunks.length).toBeGreaterThan(2);
    expect(chunks.every((chunk) => Array.from(renderRichDocMarkdown(chunk)).length <= maxLength)).toBe(true);
    expect(chunks.every((chunk) => chunk.blocks.length === 1 && chunk.blocks[0].type === 'code_block')).toBe(true);
    expect(chunks.every((chunk) => (chunk.blocks[0] as Extract<RichBlock, { type: 'code_block' }>).language === 'typescript')).toBe(true);
    expect(codeText({ type: 'doc', blocks: chunks.flatMap((chunk) => chunk.blocks) })).toBe(code);
    for (const chunk of chunks) expectClosedFences(renderRichDocMarkdown(chunk));
  });

  it('preserves text order and formatting nodes when a long paragraph must be split', () => {
    const source = [
      '**alpha beta gamma delta**',
      '[linked words stay atomic](https://example.com/safe)',
      '~~one two three four~~',
      '`inline code stays atomic`',
      '中文 emoji 🦅 finish',
    ].join(' ');
    const doc = compileRichDoc(source);
    const chunks = splitRichDoc(doc, { maxLength: 90, render: renderRichDocMarkdown });

    expect(chunks.length).toBeGreaterThan(1);
    expect(chunks.every((chunk) => Array.from(renderRichDocMarkdown(chunk)).length <= 90)).toBe(true);
    expect(chunks.map(richDocToPlainText).join('')).toBe(richDocToPlainText(doc));
    expect(chunks.flatMap(visitInlines).filter((inline) => inline.type === 'link')).toHaveLength(1);
    expect(chunks.flatMap(visitInlines).filter((inline) => inline.type === 'code')).toHaveLength(1);
  });

  it('close/reopens oversized inline code and links without losing fallback text', () => {
    const source = `[${'linked label '.repeat(20)}](https://example.com/path) \`${'inline 🦅 code '.repeat(20)}\``;
    const doc = compileRichDoc(source);
    const chunks = splitRichDoc(doc, { maxLength: 80, render: renderRichDocMarkdown });

    expect(chunks.length).toBeGreaterThan(3);
    expect(chunks.every((chunk) => Array.from(renderRichDocMarkdown(chunk)).length <= 80)).toBe(true);
    expect(semanticLeafText({ type: 'doc', blocks: chunks.flatMap((chunk) => chunk.blocks) })).toBe(semanticLeafText(doc));
    expect(chunks.map(richDocToPlainText).join('')).toBe(richDocToPlainText(doc));
    expect(chunks.flatMap(visitInlines).filter((inline) => inline.type === 'link').length).toBeGreaterThan(1);
    expect(chunks.flatMap(visitInlines).filter((inline) => inline.type === 'code').length).toBeGreaterThan(1);
  });

  it('splits oversized nested lists and table cells with source leaves in exact order once', () => {
    const source = [
      '- outer',
      `  - ${'nested 🦅 content '.repeat(25)}`,
      '',
      '| Key | Value |',
      '| --- | --- |',
      `| alpha | ${'long cell 内容 '.repeat(30)} |`,
      `| beta | ${'second row '.repeat(20)} |`,
    ].join('\n');
    const doc = compileRichDoc(source);
    const chunks = splitRichDoc(doc, { maxLength: 100, render: renderRichDocMarkdown });
    const recombined: RichDoc = { type: 'doc', blocks: chunks.flatMap((chunk) => chunk.blocks) };

    expect(chunks.length).toBeGreaterThan(4);
    expect(chunks.every((chunk) => Array.from(renderRichDocMarkdown(chunk)).length <= 100)).toBe(true);
    expect(semanticLeafText(recombined)).toBe(semanticLeafText(doc));
    expect(chunks.flatMap((chunk) => chunk.blocks).some((block) => block.type === 'list')).toBe(true);
    expect(chunks.flatMap((chunk) => chunk.blocks).filter((block) => block.type === 'table').length).toBeGreaterThan(1);
  });

  it('rejects impossible or invalid budgets instead of producing an illegal payload', () => {
    const doc = compileRichDoc('content');
    expect(() => splitRichDoc(doc, { maxLength: 0, render: renderRichDocMarkdown })).toThrow(/maxLength/i);
    expect(() => splitRichDoc(doc, { maxLength: 2, render: () => 'always too large' })).toThrow(/cannot fit/i);
  });
});
