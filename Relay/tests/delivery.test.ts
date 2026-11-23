import { describe, expect, it, vi } from 'vitest';
import {
  deliverFeishuCard,
  deliverFeishuText,
  deliverTelegramDocument,
  deliverTelegramText,
  FEISHU_TEXT_MAX,
  splitPlainText,
  TELEGRAM_TEXT_MAX,
} from '../src/delivery.js';
import type { DeliveryRequest } from '../src/types.js';

function tgTextRequest(): DeliveryRequest {
  return {
    id: 'req-tg-text',
    correlationId: 'corr-tg-text',
    channel: 'telegram',
    address: { channel: 'telegram', kind: 'telegram_chat', value: '111222333', botId: 'duke' },
    payload: { kind: 'text', text: 'hello' },
  };
}

function tgDocumentRequest(): DeliveryRequest {
  return {
    id: 'req-tg-doc',
    correlationId: 'corr-tg-doc',
    channel: 'telegram',
    address: { channel: 'telegram', kind: 'telegram_chat', value: '111222333', botId: 'duke' },
    payload: { kind: 'document', path: '/tmp/report.md', caption: 'report' },
  };
}

function feishuTextRequest(): DeliveryRequest {
  return {
    id: 'req-fs-text',
    correlationId: 'corr-fs-text',
    channel: 'feishu',
    address: { channel: 'feishu', kind: 'feishu_open_id', value: 'ou_abc', botId: 'duke', appId: 'cli_duke' },
    payload: { kind: 'text', text: 'hello' },
  };
}

function feishuCardRequest(card: Record<string, unknown>): DeliveryRequest {
  return {
    id: 'req-fs-card',
    correlationId: 'corr-fs-card',
    channel: 'feishu',
    address: { channel: 'feishu', kind: 'feishu_open_id', value: 'ou_abc', botId: 'duke', appId: 'cli_duke' },
    payload: { kind: 'card', markdown: '', card },
  };
}

describe('delivery dispatcher', () => {
  it('returns a sent receipt for Telegram text delivery', async () => {
    const bot = { api: { sendMessage: vi.fn(async () => ({ message_id: 42 })) } };

    const receipt = await deliverTelegramText(tgTextRequest(), bot as any);

    expect(bot.api.sendMessage).toHaveBeenCalledWith(111222333, 'hello');
    expect(receipt).toMatchObject({
      requestId: 'req-tg-text',
      correlationId: 'corr-tg-text',
      status: 'sent',
      channel: 'telegram',
      externalMessageId: '42',
      attempts: 1,
    });
    expect(receipt.deliveredAt).toBeDefined();
  });

  it('returns a failed receipt for Telegram text delivery errors', async () => {
    const bot = { api: { sendMessage: vi.fn(async () => { throw new Error('telegram down'); }) } };

    const receipt = await deliverTelegramText(tgTextRequest(), bot as any, 0);

    expect(receipt.status).toBe('failed');
    expect(receipt.attempts).toBe(1);
    expect(receipt.error).toContain('telegram down');
  });

  it('returns a sent receipt for Telegram document delivery', async () => {
    const bot = { api: { sendDocument: vi.fn(async () => ({ message_id: 43 })) } };

    const receipt = await deliverTelegramDocument(tgDocumentRequest(), bot as any);

    expect(bot.api.sendDocument).toHaveBeenCalledWith(111222333, expect.any(Object), { caption: 'report' });
    expect(receipt).toMatchObject({
      requestId: 'req-tg-doc',
      status: 'sent',
      externalMessageId: '43',
      attempts: 1,
    });
  });

  it('retries transient Telegram document network failures', async () => {
    vi.useFakeTimers();
    try {
      const bot = {
        api: {
          sendDocument: vi
            .fn()
            .mockRejectedValueOnce(new Error("HttpError: Network request for 'sendDocument' failed!"))
            .mockResolvedValueOnce({ message_id: 44 }),
        },
      };

      const receiptPromise = deliverTelegramDocument(tgDocumentRequest(), bot as any, 1);
      await vi.advanceTimersByTimeAsync(5_000);
      const receipt = await receiptPromise;
      expect(receipt.status).toBe('sent');
      expect(receipt.attempts).toBe(2);
      expect(bot.api.sendDocument).toHaveBeenCalledTimes(2);
    } finally {
      vi.useRealTimers();
    }
  });

  it('returns a sent receipt for Feishu text delivery', async () => {
    const client = { im: { message: { create: vi.fn(async () => ({ data: { message_id: 'om_1' } })) } } };

    const receipt = await deliverFeishuText(feishuTextRequest(), client);

    expect(client.im.message.create).toHaveBeenCalledWith({
      params: { receive_id_type: 'open_id' },
      data: { receive_id: 'ou_abc', msg_type: 'text', content: JSON.stringify({ text: 'hello' }) },
    });
    expect(receipt).toMatchObject({
      requestId: 'req-fs-text',
      status: 'sent',
      externalMessageId: 'om_1',
      attempts: 1,
    });
  });

  it('returns a failed receipt for Feishu text delivery errors', async () => {
    const client = { im: { message: { create: vi.fn(async () => { throw new Error('feishu down'); }) } } };

    const receipt = await deliverFeishuText(feishuTextRequest(), client);

    expect(receipt.status).toBe('failed');
    expect(receipt.error).toContain('feishu down');
  });

  it('splits long text at blank lines, then line breaks, then spaces, never inside a surrogate pair', () => {
    expect(splitPlainText('short', 10)).toEqual(['short']);
    expect(splitPlainText('aaaa\n\nbbbb\ncccc', 10)).toEqual(['aaaa', 'bbbb\ncccc']);
    expect(splitPlainText('aaaa\nbbbb\ncccc', 10)).toEqual(['aaaa\nbbbb', 'cccc']);
    expect(splitPlainText('aaa bbb ccc ddd', 10)).toEqual(['aaa bbb', 'ccc ddd']);
    const emoji = `${'x'.repeat(9)}😀tail`;
    const parts = splitPlainText(emoji, 10);
    expect(parts).toEqual(['x'.repeat(9), '😀tail']);
    expect(parts.join('')).toBe(emoji);
  });

  it('sends a Telegram text longer than one message as ordered parts within the limit', async () => {
    const bot = { api: { sendMessage: vi.fn(async () => ({ message_id: bot.api.sendMessage.mock.calls.length })) } };
    const lines = Array.from({ length: 300 }, (_, i) => `第${i}行 ${'数据'.repeat(10)}`);
    const request = { ...tgTextRequest(), payload: { kind: 'text' as const, text: lines.join('\n') } };

    const receipt = await deliverTelegramText(request, bot as any, 0);

    const sent = bot.api.sendMessage.mock.calls.map(([, text]) => text as string);
    expect(sent.length).toBeGreaterThan(1);
    expect(sent.every(text => text.length <= TELEGRAM_TEXT_MAX)).toBe(true);
    expect(sent.join('\n')).toBe(lines.join('\n'));
    expect(receipt).toMatchObject({ status: 'sent', externalMessageId: '1', attempts: sent.length });
  });

  it('fails the whole Telegram text when any part fails and sends nothing after it', async () => {
    const bot = { api: { sendMessage: vi.fn()
      .mockResolvedValueOnce({ message_id: 1 })
      .mockRejectedValueOnce(new Error('400: Bad Request'))
      .mockResolvedValue({ message_id: 3 }) } };
    const request = { ...tgTextRequest(), payload: { kind: 'text' as const, text: `${'a'.repeat(3000)}\n${'b'.repeat(3000)}\n${'c'.repeat(3000)}` } };

    const receipt = await deliverTelegramText(request, bot as any, 0);

    expect(bot.api.sendMessage).toHaveBeenCalledTimes(2);
    expect(receipt.status).toBe('failed');
    expect(receipt.error).toBe('part 2/3 (1 sent before it): Error: 400: Bad Request');
  });

  it('splits a Feishu text over its body limit and fails when a part fails', async () => {
    const create = vi.fn().mockResolvedValueOnce({ data: { message_id: 'om_1' } }).mockRejectedValueOnce(new Error('feishu 400'));
    const text = `${'甲'.repeat(FEISHU_TEXT_MAX - 10)}\n${'乙'.repeat(100)}`;
    const receipt = await deliverFeishuText({ ...feishuTextRequest(), payload: { kind: 'text', text } }, { im: { message: { create } } });
    expect(create).toHaveBeenCalledTimes(2);
    expect(JSON.parse(create.mock.calls[1]![0].data.content).text).toBe('乙'.repeat(100));
    expect(receipt.status).toBe('failed');
    expect(receipt.error).toContain('part 2/2');
  });

  it('keeps every Feishu request body under 150 KB, even for text of control characters', async () => {
    const create = vi.fn(async () => ({ data: { message_id: 'om_x' } }));
    const text = '\u0001'.repeat(50_000);
    const receipt = await deliverFeishuText({ ...feishuTextRequest(), payload: { kind: 'text', text } }, { im: { message: { create } } });
    expect(receipt.status).toBe('sent');
    expect(create.mock.calls.length).toBeGreaterThan(1);
    for (const [request] of create.mock.calls as any[]) {
      expect(Buffer.byteLength(JSON.stringify(request.data), 'utf8')).toBeLessThan(150_000);
    }
    expect(create.mock.calls.map(([request]: any) => JSON.parse(request.data.content).text).join('')).toBe(text);
  });

  it('sends existing Feishu interactive card shape unchanged', async () => {
    const client = { im: { message: { create: vi.fn(async () => ({ data: { message_id: 'om_card' } })) } } };
    const card = { schema: '2.0', body: { elements: [{ tag: 'markdown', content: 'hello' }] } };

    const receipt = await deliverFeishuCard(feishuCardRequest(card), client);

    expect(client.im.message.create).toHaveBeenCalledWith({
      params: { receive_id_type: 'open_id' },
      data: { receive_id: 'ou_abc', msg_type: 'interactive', content: JSON.stringify(card) },
    });
    expect(receipt).toMatchObject({
      requestId: 'req-fs-card',
      status: 'sent',
      externalMessageId: 'om_card',
    });
  });
});
