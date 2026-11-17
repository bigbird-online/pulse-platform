import { describe, expect, it } from 'vitest';
import {
  compileFeishuPayloads,
  compileTelegramPayloads,
  isRichDocRolloutEnabled,
  isTelegramRichMessageRolloutEnabled,
  resolveRichDocChannelProfiles,
  resolveTelegramRichMessageProfile,
  type TelegramHtmlMode,
} from '../src/channel-rich-rendering.js';
import type { RichBlock } from '../src/rich-doc.js';

function expectValidTelegramHtml(payload: string, mode: TelegramHtmlMode): void {
  const allowed = mode === 'rich_message_html'
    ? new Set([
      'b', 'i', 'u', 's', 'code', 'pre', 'a', 'blockquote',
      'h1', 'h2', 'h3', 'h4', 'h5', 'h6', 'p', 'br', 'hr',
      'ul', 'ol', 'li', 'table', 'tr', 'th', 'td',
    ])
    : mode === 'rich_html'
      ? new Set(['b', 'i', 'u', 's', 'code', 'pre', 'a', 'blockquote'])
      : new Set(['b', 'i', 's', 'code', 'pre']);
  const voidTags = new Set(['br', 'hr']);
  const stack: string[] = [];
  for (const match of payload.matchAll(/<\/?([a-z][a-z0-9]*)(?:\s[^>]*)?\s*\/?>/giu)) {
    const tag = match[1].toLowerCase();
    expect(allowed.has(tag), `${mode}: unsupported <${tag}>`).toBe(true);
    if (match[0].startsWith('</')) expect(stack.pop(), `${mode}: closing <${tag}>`).toBe(tag);
    else if (!voidTags.has(tag) && !match[0].endsWith('/>')) stack.push(tag);
  }
  expect(stack, `${mode}: unclosed tags`).toEqual([]);
  expect(payload).not.toMatch(/<script|<img|javascript:|data:/iu);
}

function codeText(blocks: RichBlock[]): string {
  return blocks
    .filter((block): block is Extract<RichBlock, { type: 'code_block' }> => block.type === 'code_block')
    .map((block) => block.value)
    .join('');
}

describe('channel capability and rollout profiles', () => {
  it('keeps Telegram and Feishu rollout independently off by default', () => {
    const profiles = resolveRichDocChannelProfiles({});
    expect(profiles.telegram).toMatchObject({
      rollout: 'off',
      lengthUnit: 'utf16_code_units',
      maxPayloadLength: 4096,
      maxElements: 1,
    });
    expect(profiles.feishu).toMatchObject({
      rollout: 'off',
      maxPayloadLength: 4000,
    });
    expect(profiles.telegram.updateThrottleMs).toBeGreaterThan(0);
    expect(profiles.feishu.updateThrottleMs).toBeGreaterThan(profiles.telegram.updateThrottleMs);
    expect(profiles.feishu.rolloverThreshold).toBeLessThanOrEqual(profiles.feishu.maxPayloadLength);
    expect(resolveTelegramRichMessageProfile({})).toEqual({ rollout: 'off', canaryPercent: 0 });
  });

  it('accepts bounded channel-specific overrides without coupling rollout state', () => {
    const profiles = resolveRichDocChannelProfiles({
      RELAY_TELEGRAM_RICHDOC_MODE: 'on',
      RELAY_TELEGRAM_RICHDOC_MAX_LENGTH: '3800',
      RELAY_TELEGRAM_RICHDOC_UPDATE_THROTTLE_MS: '900',
      RELAY_FEISHU_RICHDOC_MODE: 'shadow',
      RELAY_FEISHU_RICHDOC_MAX_LENGTH: '12000',
      RELAY_FEISHU_RICHDOC_ROLLOVER_THRESHOLD: '11000',
      RELAY_FEISHU_RICHDOC_MAX_ELEMENTS: '16',
    });

    expect(profiles.telegram).toMatchObject({ rollout: 'on', maxPayloadLength: 3800, updateThrottleMs: 900 });
    expect(profiles.feishu).toMatchObject({
      rollout: 'shadow',
      maxPayloadLength: 12000,
      rolloverThreshold: 11000,
      maxElements: 16,
    });
    expect(resolveTelegramRichMessageProfile({
      RELAY_TELEGRAM_RICH_MESSAGE_MODE: 'canary',
      RELAY_TELEGRAM_RICH_MESSAGE_CANARY_PERCENT: '25',
    })).toEqual({ rollout: 'canary', canaryPercent: 25 });
  });

  it('maps legacy to off, rejects invalid values, and makes canary selection identity-only and deterministic', () => {
    expect(resolveRichDocChannelProfiles({ RELAY_TELEGRAM_RICHDOC_MODE: 'legacy' }).telegram.rollout).toBe('off');
    expect(() => resolveRichDocChannelProfiles({ RELAY_TELEGRAM_RICHDOC_MODE: 'sometimes' })).toThrow(/mode/i);
    expect(() => resolveRichDocChannelProfiles({ RELAY_FEISHU_RICHDOC_MAX_LENGTH: 'NaN' })).toThrow(/max length/i);
    expect(() => resolveRichDocChannelProfiles({ RELAY_FEISHU_RICHDOC_LENGTH_UNIT: 'bytes' })).toThrow(/length unit/i);
    expect(resolveRichDocChannelProfiles({ RELAY_FEISHU_RICHDOC_MAX_ELEMENTS: '1' }).feishu.maxElements).toBe(1);
    expect(() => resolveRichDocChannelProfiles({ RELAY_FEISHU_RICHDOC_MAX_ELEMENTS: '0' })).toThrow(/max elements/i);

    const profile = resolveRichDocChannelProfiles({
      RELAY_TELEGRAM_RICHDOC_MODE: 'canary',
      RELAY_TELEGRAM_RICHDOC_CANARY_PERCENT: '37',
    }).telegram;
    const first = isRichDocRolloutEnabled(profile, 'tenant/turn/channel');
    expect(isRichDocRolloutEnabled(profile, 'tenant/turn/channel')).toBe(first);
    expect(isRichDocRolloutEnabled({ ...profile, canaryPercent: 0 }, 'tenant/turn/channel')).toBe(false);
    expect(isRichDocRolloutEnabled({ ...profile, canaryPercent: 100 }, 'tenant/turn/channel')).toBe(true);

    const richMessage = resolveTelegramRichMessageProfile({
      RELAY_TELEGRAM_RICH_MESSAGE_MODE: 'canary',
      RELAY_TELEGRAM_RICH_MESSAGE_CANARY_PERCENT: '37',
    });
    const richMessageFirst = isTelegramRichMessageRolloutEnabled(richMessage, 'tenant/turn/channel');
    expect(isTelegramRichMessageRolloutEnabled(richMessage, 'tenant/turn/channel')).toBe(richMessageFirst);
    expect(() => resolveTelegramRichMessageProfile({ RELAY_TELEGRAM_RICH_MESSAGE_MODE: 'sometimes' })).toThrow(/mode/i);
    expect(() => resolveTelegramRichMessageProfile({ RELAY_TELEGRAM_RICH_MESSAGE_CANARY_PERCENT: '101' })).toThrow(/percent/i);
  });
});

describe('Telegram RichDoc renderer', () => {
  it('maps the shared AST to rich HTML, conservative HTML, and plain text without executable input', () => {
    const source = [
      '# 标题',
      '',
      '正文 **粗体** *斜体* ~~删除~~ `inline` [安全链接](https://example.com/path).',
      '',
      '> 引用',
      '',
      '- 一级',
      '  - 二级',
      '',
      '| 列一 | 列二 |',
      '| --- | ---: |',
      '| 值 | 2 |',
      '',
      '<img src=x onerror=alert(1)> [危险](javascript:alert(1))',
      '',
      '```ts',
      'const bird = "🦅";',
      '```',
    ].join('\n');
    const result = compileTelegramPayloads(source, { maxLength: 4096 });

    expect(result.chunks).toHaveLength(1);
    const chunk = result.chunks[0];
    expect(chunk.richMessageHtml).toContain('<h1>标题</h1>');
    expect(chunk.richMessageHtml).toContain('<p>正文 <b>粗体</b> <i>斜体</i> <s>删除</s> <code>inline</code> <a href="https://example.com/path">安全链接</a>.</p>');
    expect(chunk.richMessageHtml).toContain('<blockquote><p>引用</p></blockquote>');
    expect(chunk.richMessageHtml).toContain('<ul><li><p>一级</p><ul><li><p>二级</p></li></ul></li></ul>');
    expect(chunk.richMessageHtml).toContain('<table bordered striped compact>');
    expect(chunk.richMessageHtml).toContain('<tr><th>列一</th><th align="right">列二</th></tr>');
    expect(chunk.richMessageHtml).toContain('<tr><td>值</td><td align="right">2</td></tr>');
    expect(chunk.richMessageHtml).not.toContain('| 列一 |');
    expect(chunk.richHtml).toContain('<b>标题</b>');
    expect(chunk.richHtml).toContain('<a href="https://example.com/path">安全链接</a>');
    expect(chunk.richHtml).toContain('<pre>| 列一 | 列二 |');
    expect(chunk.richHtml).not.toContain('<table');
    expect(chunk.richHtml).toContain('<blockquote>引用</blockquote>');
    expect(chunk.richHtml).toContain('<pre><code class="language-ts">');
    expect(chunk.richHtml).toContain('&lt;img src=x onerror=alert(1)&gt; 危险');
    expect(chunk.safeHtml).not.toContain('<a ');
    expect(chunk.safeHtml).not.toContain('<blockquote>');
    expect(chunk.plainText).not.toContain('<b>');
    expectValidTelegramHtml(chunk.richMessageHtml, 'rich_message_html');
    expectValidTelegramHtml(chunk.richHtml, 'rich_html');
    expectValidTelegramHtml(chunk.safeHtml, 'safe_html');
  });

  it('preserves Rich HTML block boundaries instead of relying on collapsed source whitespace', () => {
    const source = [
      '## 第二级标题',
      '',
      '第一行',
      '第二行',
      '',
      '3. 有序项',
      '4. 下一项',
      '',
      '---',
      '',
      '> 引用一',
      '> 引用二',
    ].join('\n');
    const html = compileTelegramPayloads(source, { maxLength: 4096 }).chunks[0].richMessageHtml;

    expect(html).toContain('<h2>第二级标题</h2>');
    expect(html).toContain('<p>第一行<br>第二行</p>');
    expect(html).toContain('<ol start="3"><li><p>有序项</p></li><li><p>下一项</p></li></ol>');
    expect(html).toContain('<hr/>');
    expect(html).toContain('<blockquote><p>引用一<br>引用二</p></blockquote>');
    expectValidTelegramHtml(html, 'rich_message_html');
  });

  it('keeps tables beyond the native 20-column limit readable without sending an invalid rich block', () => {
    const headers = Array.from({ length: 21 }, (_, index) => `H${index + 1}`);
    const values = Array.from({ length: 21 }, (_, index) => String(index + 1));
    const source = [
      `| ${headers.join(' | ')} |`,
      `| ${headers.map(() => '---').join(' | ')} |`,
      `| ${values.join(' | ')} |`,
    ].join('\n');
    const chunk = compileTelegramPayloads(source, { maxLength: 4096 }).chunks[0];

    expect(chunk.document.blocks[0]).toMatchObject({ type: 'table' });
    expect(chunk.richMessageHtml).toContain('<pre>| H1 | H2 |');
    expect(chunk.richMessageHtml).not.toContain('<table');
    expectValidTelegramHtml(chunk.richMessageHtml, 'rich_message_html');
  });

  it('enforces the tested default rollover budgets at their exact measured boundary', () => {
    const profiles = resolveRichDocChannelProfiles({});
    const telegram = compileTelegramPayloads('a'.repeat(9000), {
      maxLength: profiles.telegram.rolloverThreshold,
      lengthUnit: profiles.telegram.lengthUnit,
    });
    const feishu = compileFeishuPayloads('中'.repeat(9000), {
      maxLength: profiles.feishu.rolloverThreshold,
      lengthUnit: profiles.feishu.lengthUnit,
    });

    expect(telegram.chunks.every((chunk) => chunk.richMessageHtml.length <= profiles.telegram.rolloverThreshold)).toBe(true);
    expect(telegram.chunks.every((chunk) => chunk.richHtml.length <= profiles.telegram.rolloverThreshold)).toBe(true);
    expect(telegram.chunks.map((chunk) => chunk.plainText).join('')).toBe('a'.repeat(9000));
    expect(feishu.chunks.every((chunk) => chunk.cardKitMarkdown.length <= profiles.feishu.rolloverThreshold)).toBe(true);
    expect(feishu.chunks.map((chunk) => chunk.plainText).join('')).toBe('中'.repeat(9000));
  });

  it('counts UTF-16 budgets without ever cutting an emoji surrogate pair', () => {
    const source = '🦅'.repeat(20);
    const result = compileTelegramPayloads(source, { maxLength: 9, lengthUnit: 'utf16_code_units' });

    expect(result.chunks.length).toBeGreaterThan(1);
    expect(result.chunks.every((chunk) => chunk.richHtml.length <= 9)).toBe(true);
    expect(result.chunks.map((chunk) => chunk.plainText).join('')).toBe(source);
    expect(result.chunks.every((chunk) => !chunk.richHtml.includes('\uFFFD'))).toBe(true);
  });

  it('recompiles every authoritative snapshot without carrying incomplete syntax between frames', () => {
    const incomplete = compileTelegramPayloads('answer **half', { maxLength: 256 }).chunks[0];
    const complete = compileTelegramPayloads('answer **whole**', { maxLength: 256 }).chunks[0];

    expect(incomplete.richHtml).toBe('answer **half');
    expect(complete.richHtml).toBe('answer <b>whole</b>');
    expect(complete.richHtml).not.toContain('half');
    expectValidTelegramHtml(incomplete.richHtml, 'rich_html');
    expectValidTelegramHtml(complete.richHtml, 'rich_html');
  });

  it('keeps every streaming prefix and every semantic chunk within a valid HTML payload', () => {
    const sample = '```ts\n' + Array.from({ length: 60 }, (_, index) => `const v${index} = "🦅";`).join('\n') + '\n```';
    for (let end = 0; end <= Array.from(sample).length; end += 1) {
      const snapshot = Array.from(sample).slice(0, end).join('');
      const result = compileTelegramPayloads(snapshot, { maxLength: 180 });
      for (const chunk of result.chunks) {
        expect(Array.from(chunk.richMessageHtml).length).toBeLessThanOrEqual(180);
        expect(Array.from(chunk.richHtml).length).toBeLessThanOrEqual(180);
        expect(Array.from(chunk.safeHtml).length).toBeLessThanOrEqual(180);
        expect(Array.from(chunk.plainText).length).toBeLessThanOrEqual(180);
        expectValidTelegramHtml(chunk.richMessageHtml, 'rich_message_html');
        expectValidTelegramHtml(chunk.richHtml, 'rich_html');
        expectValidTelegramHtml(chunk.safeHtml, 'safe_html');
      }
    }

    const complete = compileTelegramPayloads(sample, { maxLength: 180 });
    expect(complete.chunks.length).toBeGreaterThan(2);
    expect(codeText(complete.chunks.flatMap((chunk) => chunk.document.blocks))).toBe(
      Array.from({ length: 60 }, (_, index) => `const v${index} = "🦅";`).join('\n') + '\n',
    );
  });
});

describe('Feishu RichDoc renderer', () => {
  it('emits CardKit Markdown, post Markdown, and plain text from the same sanitized document', () => {
    const result = compileFeishuPayloads(
      '# 标题\n\n| A | B |\n| --- | --- |\n| 1 | **2** |\n\n[safe](https://example.com) [bad](data:text/html,x)',
      { maxLength: 4000 },
    );
    const chunk = result.chunks[0];

    expect(chunk.cardKitMarkdown).toContain('# 标题');
    expect(chunk.cardKitMarkdown).toContain('| A | B |');
    expect(chunk.cardKitMarkdown).toContain('[safe](<https://example.com/>)');
    expect(chunk.cardKitMarkdown).not.toContain('data:');
    expect(chunk.postMarkdown).toBe(chunk.cardKitMarkdown);
    expect(chunk.plainText).toContain('safe (https://example.com/) bad');
  });

  it('treats one CardKit Markdown body as one element regardless of fenced-code count', () => {
    const source = Array.from(
      { length: 10 },
      (_, index) => `\`\`\`ts\nconst value${index} = ${index};\n\`\`\``,
    ).join('\n\n');
    const result = compileFeishuPayloads(source, { maxLength: 4000, maxElements: 1 });

    expect(result.chunks).toHaveLength(1);
    expect(result.chunks[0].document.blocks.filter((block) => block.type === 'code_block')).toHaveLength(10);
  });

  it('rolls long answers only at shared semantic boundaries when possible', () => {
    const source = Array.from({ length: 20 }, (_, index) => `## Section ${index}\n\nParagraph ${index} ${'内容'.repeat(12)}`).join('\n\n');
    const result = compileFeishuPayloads(source, { maxLength: 180 });

    expect(result.chunks.length).toBeGreaterThan(2);
    expect(result.chunks.every((chunk) => Array.from(chunk.cardKitMarkdown).length <= 180)).toBe(true);
    expect(result.chunks.flatMap((chunk) => chunk.document.blocks).map((block) => block.type)).toEqual(
      result.document.blocks.map((block) => block.type),
    );
  });
});
