/**
 * PULSE platform bot (AH-083): it only asks the owner to confirm external entry
 * bindings and decisions. It routes to no Agent and carries no conversation.
 * A confirmation becomes Host human input custody bound to the exact request;
 * the core decides whether that input covers a command.
 */
import { randomBytes } from 'node:crypto';
import { existsSync, mkdirSync, readFileSync, readdirSync, renameSync, writeFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import type { Bot } from 'grammy';
import { recordHumanInput } from './scl-human-decision.js';

export const REQUEST_TTL_MS = 30 * 60 * 1000;
const ID = /^[a-f0-9]{32}$/;
const CALLBACK = /^pf:([a-f0-9]{32}):([ynr])$/;

export interface PlatformSettings { root: string; owner: string; entries: string[] }
export interface PlatformRequest {
  schema: 'pulse.platform-request/v1'; request_id: string; created_at: string; kind: 'binding' | 'decision';
  entry: string; actor: string; binding_id?: string; intent?: string; work_item_id?: string | null;
  input_digest?: string; summary?: string;
}
export type PlatformResult =
  | { status: 'confirmed'; human_input_ref: string; binding_id?: string }
  | { status: 'declined' | 'expired' };

/** The installed core's external entry settings; absent when this Host has none. */
export function platformSettings(policyPath: string): PlatformSettings | undefined {
  const policy = JSON.parse(readFileSync(policyPath, 'utf8'));
  const value = policy.external_entries;
  if (!value) return undefined;
  if (typeof value.root !== 'string' || typeof value.owner !== 'string' || !Array.isArray(value.entries))
    throw new Error('Installed external entry settings are invalid');
  return value as PlatformSettings;
}

function writePrivate(path: string, value: unknown): void {
  mkdirSync(dirname(path), { recursive: true, mode: 0o700 });
  const temporary = `${path}.${randomBytes(8).toString('hex')}`;
  writeFileSync(temporary, JSON.stringify(value) + '\n', { flag: 'wx', mode: 0o600 });
  renameSync(temporary, path);
}

const requests = (settings: PlatformSettings) => join(settings.root, 'requests');
const resultPath = (settings: PlatformSettings, id: string) => join(requests(settings), `${id}.result.json`);
const bindingPath = (settings: PlatformSettings, id: string) => join(settings.root, 'bindings', `${id}.json`);

export function readRequest(settings: PlatformSettings, id: string): PlatformRequest {
  if (!ID.test(id)) throw new Error('Invalid platform request');
  const value = JSON.parse(readFileSync(join(requests(settings), `${id}.json`), 'utf8')) as PlatformRequest;
  if (value.schema !== 'pulse.platform-request/v1' || value.request_id !== id) throw new Error('Invalid platform request');
  return value;
}

/** Requests not yet shown to the owner. */
export function unsentRequests(settings: PlatformSettings): PlatformRequest[] {
  let names: string[];
  try { names = readdirSync(requests(settings)); } catch { return []; }
  return names
    .filter(name => /^[a-f0-9]{32}\.json$/.test(name))
    .map(name => name.slice(0, 32))
    .filter(id => !existsSync(join(requests(settings), `${id}.sent.json`)) && !existsSync(resultPath(settings, id)))
    .map(id => readRequest(settings, id));
}

export function markSent(settings: PlatformSettings, id: string, chatId: string, messageId: number): void {
  writePrivate(join(requests(settings), `${id}.sent.json`), { chat_id: chatId, message_id: messageId });
}

export function cardText(request: PlatformRequest): string {
  if (request.kind === 'binding')
    return [`外部入口绑定请求`, `入口：${request.entry}`, `身份：${request.actor}`,
      `确认后，这个会话可以以 ${request.actor} 身份管理和执行 WI。30 分钟内有效。`].join('\n');
  return [`需要你的决定`, `来自：${request.actor}（${request.entry}）`, request.summary ?? '',
    `确认只对以上内容有效。`].join('\n');
}

/** The owner's answer. Others, expired requests and repeats change nothing. */
export function decide(settings: PlatformSettings, id: string, answer: 'y' | 'n', presser: string,
  eventId: string, now = Date.now(), env: NodeJS.ProcessEnv = process.env): PlatformResult | undefined {
  if (presser !== settings.owner) return undefined;
  if (existsSync(resultPath(settings, id))) return JSON.parse(readFileSync(resultPath(settings, id), 'utf8'));
  const request = readRequest(settings, id);
  let result: PlatformResult;
  if (now - Date.parse(request.created_at) > REQUEST_TTL_MS) result = { status: 'expired' };
  else if (answer === 'n') result = { status: 'declined' };
  else if (request.kind === 'binding') {
    if (!settings.entries.includes(request.entry)) throw new Error('Unknown external entry');
    const message = { schema: 'pulse.platform-confirmation/v1', kind: 'binding', request_id: id,
      entry: request.entry, actor: request.actor };
    const ref = recordHumanInput({ channel: 'telegram', eventId, humanRef: presser,
      message: JSON.stringify(message) }, `entry-binding:${id}`, env);
    const bindingId = randomBytes(16).toString('hex');
    writePrivate(bindingPath(settings, bindingId), { schema: 'pulse.entry-binding/v1', binding_id: bindingId,
      entry: request.entry, actor: request.actor, request_id: id, human_input_ref: ref,
      confirmed_at: new Date(now).toISOString() });
    result = { status: 'confirmed', human_input_ref: ref, binding_id: bindingId };
  } else {
    if (!request.binding_id || !ID.test(request.binding_id)) throw new Error('Decision has no binding');
    const message = { schema: 'pulse.platform-confirmation/v1', kind: 'decision', request_id: id,
      binding_id: request.binding_id, intent: request.intent, work_item_id: request.work_item_id ?? null,
      input_digest: request.input_digest };
    const ref = recordHumanInput({ channel: 'telegram', eventId, humanRef: presser,
      message: JSON.stringify(message) }, `entry:${request.binding_id}`, env);
    result = { status: 'confirmed', human_input_ref: ref };
  }
  writePrivate(resultPath(settings, id), result);
  return result;
}

/** The owner ends a binding; later commands from that session are refused. */
export function revoke(settings: PlatformSettings, bindingId: string, presser: string, now = Date.now()): boolean {
  if (presser !== settings.owner || !ID.test(bindingId)) return false;
  const path = bindingPath(settings, bindingId);
  const value = JSON.parse(readFileSync(path, 'utf8'));
  if (!value.revoked_at) writePrivate(path, { ...value, revoked_at: new Date(now).toISOString() });
  return true;
}

const buttons = (id: string) => ({ inline_keyboard: [[
  { text: '确认', callback_data: `pf:${id}:y` }, { text: '拒绝', callback_data: `pf:${id}:n` }]] });

/** Long-poll the platform bot and show new requests; stop() ends both. */
export function startPlatformBot(bot: Bot, settings: PlatformSettings, pollMs = 3000): { stop(): void } {
  const chat = settings.owner;
  let sending = false;
  const send = async () => {
    if (sending) return;
    sending = true;
    try {
      for (const request of unsentRequests(settings)) {
        const sent = await bot.api.sendMessage(chat, cardText(request), { reply_markup: buttons(request.request_id) });
        markSent(settings, request.request_id, chat, sent.message_id);
      }
    } catch (error) {
      console.log({ ts: new Date().toISOString(), level: 'warn', module: 'platform-bot', msg: 'request delivery failed', error: String(error) });
    } finally { sending = false; }
  };
  bot.on('callback_query:data', async ctx => {
    const match = CALLBACK.exec(ctx.callbackQuery.data);
    const presser = String(ctx.from.id);
    if (!match) { await ctx.answerCallbackQuery(); return; }
    const [, id, answer] = match;
    try {
      if (answer === 'r') {
        const done = revoke(settings, id!, presser);
        await ctx.answerCallbackQuery({ text: done ? '已撤销绑定' : '无权操作' });
        if (done) await ctx.editMessageReplyMarkup({ reply_markup: { inline_keyboard: [] } });
        return;
      }
      const result = decide(settings, id!, answer as 'y' | 'n', presser, ctx.callbackQuery.id);
      if (!result) { await ctx.answerCallbackQuery({ text: '无权操作' }); return; }
      const label = result.status === 'confirmed' ? '已确认' : result.status === 'declined' ? '已拒绝' : '已过期';
      await ctx.answerCallbackQuery({ text: label });
      const text = `${ctx.callbackQuery.message && 'text' in ctx.callbackQuery.message ? ctx.callbackQuery.message.text : ''}\n\n${label}`;
      const revocable = result.status === 'confirmed' && result.binding_id;
      await ctx.editMessageText(text, revocable
        ? { reply_markup: { inline_keyboard: [[{ text: '撤销绑定', callback_data: `pf:${result.binding_id}:r` }]] } }
        : undefined);
    } catch (error) {
      await ctx.answerCallbackQuery({ text: '处理失败，请求保持原状' });
      console.log({ ts: new Date().toISOString(), level: 'warn', module: 'platform-bot', msg: 'confirmation failed', error: String(error) });
    }
  });
  // The platform bot never takes Relay down: a polling failure, such as another
  // process still holding this token (409), stops only confirmations.
  bot.catch(error => console.log({ ts: new Date().toISOString(), level: 'warn', module: 'platform-bot',
    msg: 'update handling failed', error: String(error.error) }));
  const timer = setInterval(() => void send(), pollMs);
  bot.start({ allowed_updates: ['callback_query'] }).catch(error => {
    clearInterval(timer);
    console.log({ ts: new Date().toISOString(), level: 'error', module: 'platform-bot',
      msg: 'platform bot stopped; confirmations unavailable', error: String(error) });
  });
  void send();
  return { stop() { clearInterval(timer); void bot.stop().catch(() => undefined); } };
}
