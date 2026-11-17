import { createServer } from 'node:http';
import { afterEach, describe, expect, it } from 'vitest';
import { Bot } from 'grammy';
import { Client, LoggerLevel, withTenantToken } from '@larksuiteoapi/node-sdk';
import { deliverFeishuText, deliverTelegramText } from '../src/delivery.js';
import type { DeliveryRequest } from '../src/types.js';

const servers: ReturnType<typeof createServer>[] = [];
afterEach(async () => {
  for (const server of servers.splice(0)) {
    await new Promise<void>((resolve, reject) => server.close(error => error ? reject(error) : resolve()));
  }
});

async function ambiguousChannel(): Promise<{ url: string; calls: () => number }> {
  let count = 0;
  const server = createServer(req => {
    count++;
    // The receiver observed the complete request but its reply is lost.
    req.resume();
    req.on('end', () => req.socket.destroy());
  });
  servers.push(server);
  await new Promise<void>(resolve => server.listen(0, '127.0.0.1', resolve));
  const address = server.address();
  if (!address || typeof address === 'string') throw new Error('fixture server unavailable');
  return { url: `http://127.0.0.1:${address.port}`, calls: () => count };
}

describe('installed channel SDKs preserve single-attempt notice semantics', () => {
  it('grammy with its real HTTP client does not repeat an ambiguous send', async () => {
    const fixture = await ambiguousChannel();
    const bot = new Bot('12345:fixture', { client: { apiRoot: fixture.url } });
    const request: DeliveryRequest = {
      id: 'fixture-notice', correlationId: 'fixture-notice', channel: 'telegram',
      address: { channel: 'telegram', kind: 'telegram_chat', value: '12345' },
      payload: { kind: 'text', text: 'Synthetic transport test' },
    };
    const receipt = await deliverTelegramText(request, bot, 0);
    expect(receipt).toMatchObject({ status: 'failed', attempts: 1 });
    expect(fixture.calls()).toBe(1);
  });

  it('Feishu SDK with its real axios client does not repeat an ambiguous send', async () => {
    const fixture = await ambiguousChannel();
    const sdk = new Client({
      appId: 'fixture', appSecret: 'fixture', domain: fixture.url,
      loggerLevel: LoggerLevel.fatal,
    });
    const client = { im: { message: { create: (input: Parameters<typeof sdk.im.message.create>[0]) =>
      sdk.im.message.create(input, withTenantToken('synthetic-local-token')) } } };
    const request: DeliveryRequest = {
      id: 'fixture-notice', correlationId: 'fixture-notice', channel: 'feishu',
      address: { channel: 'feishu', kind: 'feishu_open_id', value: 'ou_fixture' },
      payload: { kind: 'text', text: 'Synthetic transport test' },
    };
    const receipt = await deliverFeishuText(request, client);
    expect(receipt).toMatchObject({ status: 'failed', attempts: 1 });
    expect(fixture.calls()).toBe(1);
  });
});
