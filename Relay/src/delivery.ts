import { randomUUID } from 'node:crypto';
import { createReadStream, statSync } from 'node:fs';
import { basename, extname } from 'node:path';
import { InputFile, type Bot } from 'grammy';
import type { ChannelAddress, DeliveryReceipt, DeliveryRequest } from './types.js';
import { traceDeliveryReceipt } from './relay-request-trace.js';

export interface FeishuMessageClient {
  im: {
    message: {
      create(input: {
        params: { receive_id_type: 'open_id' };
        data: { receive_id: string; msg_type: string; content: string };
      }): Promise<unknown>;
      /** Rewrite a sent interactive card in place (lark SDK `im.message.patch`). */
      patch?(input: {
        path: { message_id: string };
        data: { content: string };
      }): Promise<unknown>;
    };
    image?: {
      create(payload?: {
        data: { image_type: 'message' | 'avatar'; image: Buffer | NodeJS.ReadableStream };
      }, options?: unknown): Promise<{ image_key?: string } | null>;
    };
    file?: {
      create(payload?: {
        data: {
          file_type: 'opus' | 'mp4' | 'pdf' | 'doc' | 'xls' | 'ppt' | 'stream';
          file_name: string;
          file: Buffer | NodeJS.ReadableStream;
        };
      }, options?: unknown): Promise<{ file_key?: string } | null>;
    };
  };
}

function nowIso(): string {
  return new Date().toISOString();
}

function externalMessageId(result: unknown): string | undefined {
  if (!result || typeof result !== 'object') return undefined;
  const record = result as Record<string, unknown>;
  const direct = record.message_id ?? record.messageId;
  if (typeof direct === 'string') return direct;
  if (typeof direct === 'number') return String(direct);
  const data = record.data;
  if (!data || typeof data !== 'object') return undefined;
  const nested = (data as Record<string, unknown>).message_id ?? (data as Record<string, unknown>).messageId;
  return typeof nested === 'string' ? nested : undefined;
}

function receipt(
  request: DeliveryRequest,
  status: DeliveryReceipt['status'],
  attempts: number,
  extra: Partial<Pick<DeliveryReceipt, 'externalMessageId' | 'error'>> = {},
): DeliveryReceipt {
  const built: DeliveryReceipt = {
    id: randomUUID(),
    requestId: request.id,
    correlationId: request.correlationId,
    status,
    channel: request.channel,
    address: request.address,
    attempts,
    ...(status === 'sent' ? { deliveredAt: nowIso() } : {}),
    ...extra,
  };

  // 切片1 投递 receipt 埋点：所有 DeliveryReceipt 都经此工厂（含 invalid() 失败路径）,
  // 是投递结果的唯一收口点。旁路观测,dark-deploy 开关控制,全隔离永不抛
  // （traceDeliveryReceipt 内部已 try/catch）。只读 built,不改任何投递语义。
  // session 用 correlationId（与本次 turn 共享,探针据此把投递配回请求）。
  traceDeliveryReceipt({
    session: request.correlationId,
    status: built.status,
    attempts: built.attempts,
    channel: built.channel,
  });

  return built;
}

const DEFAULT_TELEGRAM_RETRIES = 2;
const MAX_RETRY_DELAY_MS = 30_000;
const NETWORK_RETRY_BASE_DELAY_MS = 5_000;
const TRANSIENT_NETWORK_ERROR = /Network request|fetch failed|ECONNRESET|ETIMEDOUT|ENOTFOUND|EAI_AGAIN|ECONNREFUSED|socket hang up|aborted/i;

function retryDelayMs(error: unknown, attempt: number): number | undefined {
  const text = String(error);
  if (!/429|flood/i.test(text)) {
    if (TRANSIENT_NETWORK_ERROR.test(text)) {
      return Math.min(MAX_RETRY_DELAY_MS, NETWORK_RETRY_BASE_DELAY_MS * 2 ** attempt);
    }
    return undefined;
  }
  const retryAfter = /retry.after.(\d+)/i.exec(text);
  return retryAfter ? Math.min(MAX_RETRY_DELAY_MS, parseInt(retryAfter[1], 10) * 1000) : MAX_RETRY_DELAY_MS;
}

async function withRetry<T>(operation: () => Promise<T>, retries: number): Promise<{ result?: T; attempts: number; error?: unknown }> {
  for (let attempt = 0; attempt <= retries; attempt++) {
    try {
      return { result: await operation(), attempts: attempt + 1 };
    } catch (error) {
      const delayMs = retryDelayMs(error, attempt);
      if (delayMs === undefined || attempt === retries) {
        return { attempts: attempt + 1, error };
      }
      await new Promise(resolve => setTimeout(resolve, delayMs));
    }
  }
  return { attempts: retries + 1, error: new Error('delivery retry loop exhausted') };
}

function invalid(request: DeliveryRequest, error: string): DeliveryReceipt {
  return receipt(request, 'failed', 0, { error });
}

function addressValue(address: ChannelAddress): string {
  return address.value;
}

export async function deliverTelegramText(request: DeliveryRequest, bot: Bot, retries = DEFAULT_TELEGRAM_RETRIES): Promise<DeliveryReceipt> {
  if (request.channel !== 'telegram' || request.payload.kind !== 'text') {
    return invalid(request, 'telegram text delivery requires channel=telegram and payload.kind=text');
  }
  const payload = request.payload;
  const delivered = await withRetry(
    () => bot.api.sendMessage(Number(addressValue(request.address)), payload.text),
    retries,
  );
  if (delivered.error) {
    return receipt(request, 'failed', delivered.attempts, { error: String(delivered.error) });
  }
  return receipt(request, 'sent', delivered.attempts, { externalMessageId: externalMessageId(delivered.result) });
}

export async function deliverTelegramDocument(request: DeliveryRequest, bot: Bot, retries = DEFAULT_TELEGRAM_RETRIES): Promise<DeliveryReceipt> {
  if (request.channel !== 'telegram' || request.payload.kind !== 'document') {
    return invalid(request, 'telegram document delivery requires channel=telegram and payload.kind=document');
  }
  const payload = request.payload;
  const delivered = await withRetry(
    () => bot.api.sendDocument(
      Number(addressValue(request.address)),
      new InputFile(payload.path, basename(payload.path)),
      payload.caption ? { caption: payload.caption } : undefined,
    ),
    retries,
  );
  if (delivered.error) {
    return receipt(request, 'failed', delivered.attempts, { error: String(delivered.error) });
  }
  return receipt(request, 'sent', delivered.attempts, { externalMessageId: externalMessageId(delivered.result) });
}

export async function deliverFeishuText(request: DeliveryRequest, client: FeishuMessageClient): Promise<DeliveryReceipt> {
  if (request.channel !== 'feishu' || request.payload.kind !== 'text') {
    return invalid(request, 'feishu text delivery requires channel=feishu and payload.kind=text');
  }
  try {
    const result = await client.im.message.create({
      params: { receive_id_type: 'open_id' },
      data: { receive_id: addressValue(request.address), msg_type: 'text', content: JSON.stringify({ text: request.payload.text }) },
    });
    return receipt(request, 'sent', 1, { externalMessageId: externalMessageId(result) });
  } catch (error) {
    return receipt(request, 'failed', 1, { error: String(error) });
  }
}

export async function deliverFeishuCard(request: DeliveryRequest, client: FeishuMessageClient): Promise<DeliveryReceipt> {
  if (request.channel !== 'feishu' || request.payload.kind !== 'card') {
    return invalid(request, 'feishu card delivery requires channel=feishu and payload.kind=card');
  }
  try {
    const card = request.payload.card ?? {
      schema: '2.0',
      body: { elements: [{ tag: 'markdown', content: request.payload.markdown }] },
      ...(request.payload.title ? { header: { title: { tag: 'plain_text', content: request.payload.title } } } : {}),
    };
    const result = await client.im.message.create({
      params: { receive_id_type: 'open_id' },
      data: { receive_id: addressValue(request.address), msg_type: 'interactive', content: JSON.stringify(card) },
    });
    return receipt(request, 'sent', 1, { externalMessageId: externalMessageId(result) });
  } catch (error) {
    return receipt(request, 'failed', 1, { error: String(error) });
  }
}

// --- Feishu file / image delivery (tool-output file push) ---

const IMAGE_EXTENSIONS = new Set(['.jpg', '.jpeg', '.png', '.gif', '.webp', '.bmp', '.ico', '.tiff', '.tif']);

const FEISHU_IMAGE_MAX_BYTES = 10 * 1024 * 1024;   // 10 MB
const FEISHU_FILE_MAX_BYTES = 30 * 1024 * 1024;     // 30 MB

export function isImagePath(filePath: string): boolean {
  return IMAGE_EXTENSIONS.has(extname(filePath).toLowerCase());
}

export function resolveFeishuFileType(filePath: string): 'opus' | 'mp4' | 'pdf' | 'doc' | 'xls' | 'ppt' | 'stream' {
  const ext = extname(filePath).toLowerCase();
  switch (ext) {
    case '.pdf': return 'pdf';
    case '.doc': case '.docx': return 'doc';
    case '.xls': case '.xlsx': return 'xls';
    case '.ppt': case '.pptx': return 'ppt';
    case '.opus': case '.ogg': return 'opus';
    // mp4/mov: upload as 'stream' to avoid API 230055 (im.file.create does not
    // accept file_type='mp4' — that requires im.video.create which is unavailable).
    // Users receive a downloadable file attachment instead of inline video.
    case '.mp4': case '.mov': return 'stream';
    default: return 'stream';
  }
}

export function checkFileForDelivery(filePath: string): { ok: true; size: number } | { ok: false; error: string } {
  let stat;
  try {
    stat = statSync(filePath);
  } catch {
    return { ok: false, error: `File not found: ${filePath}` };
  }
  if (!stat.isFile()) {
    return { ok: false, error: `Not a regular file: ${filePath}` };
  }
  const isImage = isImagePath(filePath);
  const limit = isImage ? FEISHU_IMAGE_MAX_BYTES : FEISHU_FILE_MAX_BYTES;
  if (stat.size > limit) {
    return { ok: false, error: `File too large (${Math.round(stat.size / 1024 / 1024)}MB, limit ${Math.round(limit / 1024 / 1024)}MB): ${basename(filePath)}` };
  }
  if (stat.size === 0) {
    return { ok: false, error: `Empty file: ${basename(filePath)}` };
  }
  return { ok: true, size: stat.size };
}

export async function deliverFeishuImage(request: DeliveryRequest, client: FeishuMessageClient): Promise<DeliveryReceipt> {
  if (request.channel !== 'feishu' || request.payload.kind !== 'document') {
    return invalid(request, 'feishu image delivery requires channel=feishu and payload.kind=document');
  }
  if (!client.im.image?.create) {
    return invalid(request, 'feishu client does not support image upload');
  }
  const payload = request.payload;
  try {
    const uploadResult = await client.im.image.create({
      data: { image_type: 'message', image: createReadStream(payload.path) },
    });
    const imageKey = uploadResult?.image_key;
    if (!imageKey) {
      return receipt(request, 'failed', 1, { error: 'image upload returned no image_key' });
    }
    const sendResult = await client.im.message.create({
      params: { receive_id_type: 'open_id' },
      data: {
        receive_id: addressValue(request.address),
        msg_type: 'image',
        content: JSON.stringify({ image_key: imageKey }),
      },
    });
    return receipt(request, 'sent', 1, { externalMessageId: externalMessageId(sendResult) });
  } catch (error) {
    return receipt(request, 'failed', 1, { error: String(error) });
  }
}

export async function deliverFeishuFile(request: DeliveryRequest, client: FeishuMessageClient): Promise<DeliveryReceipt> {
  if (request.channel !== 'feishu' || request.payload.kind !== 'document') {
    return invalid(request, 'feishu file delivery requires channel=feishu and payload.kind=document');
  }
  if (!client.im.file?.create) {
    return invalid(request, 'feishu client does not support file upload');
  }
  const payload = request.payload;
  const fileType = resolveFeishuFileType(payload.path);
  const fileName = basename(payload.path);
  // Feishu requires different msg_type per file_type:
  //   opus → 'audio', stream/pdf/doc/xls/ppt → 'file'
  const msgType = fileType === 'opus' ? 'audio' : 'file';
  try {
    const uploadResult = await client.im.file.create({
      data: { file_type: fileType, file_name: fileName, file: createReadStream(payload.path) },
    });
    const fileKey = uploadResult?.file_key;
    if (!fileKey) {
      return receipt(request, 'failed', 1, { error: 'file upload returned no file_key' });
    }
    const sendResult = await client.im.message.create({
      params: { receive_id_type: 'open_id' },
      data: {
        receive_id: addressValue(request.address),
        msg_type: msgType,
        content: JSON.stringify({ file_key: fileKey }),
      },
    });
    return receipt(request, 'sent', 1, { externalMessageId: externalMessageId(sendResult) });
  } catch (error) {
    return receipt(request, 'failed', 1, { error: String(error) });
  }
}
