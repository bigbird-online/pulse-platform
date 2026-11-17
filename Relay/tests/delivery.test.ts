import { describe, expect, it, vi } from 'vitest';
import {
  deliverFeishuCard,
  deliverFeishuText,
  deliverTelegramDocument,
  deliverTelegramText,
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
