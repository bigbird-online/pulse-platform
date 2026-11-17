import { InputFile, type Bot } from 'grammy';
import { processCallback, type ProcessWriter } from './work-process-view.js';
import type { WorkProcessRecord } from './work-process-archive.js';
import { compileRichDoc, renderRichDocMarkdown, richDocToPlainText } from './rich-doc.js';
import { renderTelegramRichMessageHtml } from './channel-rich-rendering.js';
import { escapeProcessHtml as html, toolDisclosureKey, workProcessDetails, workProcessDocument,
  type ProcessDetailItem } from './work-process-content.js';
import { workProcessHeader } from './work-process-header.js';

const OVERFLOW = '超出本条显示上限；可查看完整过程（只读文件，不会重新运行任务）。';
type Control = { text: string; data: string };
function controls(record: WorkProcessRecord): Control {
  return { text: workProcessHeader(record).control, data: processCallback(record, record.expanded ? 'close' : 'open') };
}
function toolControl(record: WorkProcessRecord, item: ProcessDetailItem): Control {
  return { text: `${item.expanded ? '▾' : '▸'} ${item.title}`,
    data: processCallback(record, `t${toolDisclosureKey(item.id)}${item.expanded ? 'c' : 'o'}`) };
}
const allControl = (record: WorkProcessRecord): Control => ({ text: '查看完整过程', data: processCallback(record, 'all') });
const richButton = (control: Control, primary = false): string => `<tg-button type="callback_data" style="${primary ? 'primary' : 'link'}" data="${html(control.data)}">${html(control.text)}</tg-button>`;
const plainButton = (control: Control) => ({ text: control.text, callback_data: control.data });

/** Pick whole chronological paragraphs and tool rows first. Tool output can
 * consume only the remaining budget; it never pushes commentary out of view. */
function selectItems<T>(record: WorkProcessRecord, render: (items: ProcessDetailItem[], notice?: string) => T,
  fits: (payload: T) => boolean): T {
  const details = workProcessDetails(record);
  const selected: ProcessDetailItem[] = [];
  let overflow = false;
  const reservedNotice = [details.notice, OVERFLOW].filter(Boolean).join('\n');
  for (const item of details.items) {
    const skeleton = item.kind === 'operation' ? { ...item, detail: '' } : item;
    if (!fits(render([...selected, skeleton], reservedNotice))) { overflow = true; break; }
    selected.push(skeleton);
  }
  for (const item of selected) {
    if (item.kind !== 'operation' || !item.expanded) continue;
    item.detail = details.items.find(candidate => candidate.id === item.id)!.detail;
    if (!fits(render(selected, reservedNotice))) {
      item.detail = '';
      overflow = true;
    }
  }
  return render(selected, [details.notice, overflow ? OVERFLOW : ''].filter(Boolean).join('\n') || undefined);
}

function telegramPayload(record: WorkProcessRecord, rich: boolean): { html: string; text: string; reply_markup: object } {
  const control = controls(record), header = workProcessHeader(record);
  const render = (items: ProcessDetailItem[], notice?: string) => {
    // Native accent button and inline metadata; never pretend Bot API HTML supports CSS.
    let body = `<p>${richButton(control, true)}&#8194;│&#8194;${header.marker} ${html(header.status)}${header.meta ? ` · ${html(header.meta)}` : ''}</p>`;
    let text = `${header.marker} ${header.status}${header.meta ? ` · ${header.meta}` : ''}`;
    const buttons = [[plainButton(control)]];
    if (record.expanded) {
      for (const item of items) {
        if (item.kind === 'commentary') {
          const doc = compileRichDoc(item.detail);
          if (item.source === 'reasoning_summary') {
            body += '<p><i>原生推理摘要</i></p>'; text += '\n\n原生推理摘要';
          }
          body += renderTelegramRichMessageHtml(doc);
          text += `\n\n${richDocToPlainText(doc)}`;
        } else {
          const tool = toolControl(record, item);
          body += `<p>${richButton(tool)}</p>`;
          text += `\n\n${tool.text}`;
          buttons.push([plainButton(tool)]);
          if (item.expanded && item.detail) {
            body += `<pre>${html(item.detail)}</pre>`;
            text += `\n${item.detail}`;
          }
        }
      }
      if (!record.events.length) {
        const empty = workProcessDetails(record).emptyText;
        body += `<p>${html(empty)}</p>`; text += `\n\n${empty}`;
      }
      if (notice) {
        body += `<p>${html(notice)}</p><p>${richButton(allControl(record))}</p>`;
        text += `\n\n${notice}`; buttons.push([plainButton(allControl(record))]);
      }
    }
    return { html: body, text, reply_markup: { inline_keyboard: buttons } };
  };
  if (!record.expanded) return render([]);
  return selectItems(record, render, payload => rich
    ? Buffer.byteLength(payload.html) <= 28_000 && (payload.html.match(/<(?:p|h[1-6]|pre|li|tr|blockquote|details)\b/g) ?? []).length <= 450
    : payload.text.length <= 4_000 && payload.reply_markup.inline_keyboard.length <= 90);
}
export function telegramProcessPayload(record: WorkProcessRecord): { html: string; text: string; reply_markup: object } {
  const rich = telegramPayload(record, true), plain = telegramPayload(record, false);
  return { html: rich.html, text: plain.text, reply_markup: plain.reply_markup };
}
export function telegramProcessWriter(bot: Bot): ProcessWriter {
  const writer: ProcessWriter = async record => {
    const payload = telegramProcessPayload(record);
    const raw = bot.api.raw as any;
    if (record.mode !== 'plain') {
      try {
        if (record.messageId) {
          await raw.editMessageText({ chat_id: Number(record.address), message_id: Number(record.messageId),
            rich_message: { html: payload.html, skip_entity_detection: true } });
          record.mode = 'rich'; return record.messageId;
        }
        const sent = await raw.sendRichMessage({ chat_id: Number(record.address),
          rich_message: { html: payload.html, skip_entity_detection: true }, disable_notification: true });
        record.mode = 'rich';
        return typeof sent?.message_id === 'number' ? String(sent.message_id) : undefined;
      } catch (error) {
        if (/message is not modified/i.test(String(error))) return record.messageId;
        if (!/can't parse|unsupported.*tag|method.*(?:unavailable|not found)|404|is not a function/i.test(String(error))) throw error;
      }
    }
    record.mode = 'plain';
    const api = bot.api as any;
    if (record.messageId) {
      try { await api.editMessageText(Number(record.address), Number(record.messageId), payload.text, { reply_markup: payload.reply_markup }); }
      catch (error) { if (!/message is not modified/i.test(String(error))) throw error; }
      return record.messageId;
    }
    const sent = await api.sendMessage(Number(record.address), payload.text, { reply_markup: payload.reply_markup, disable_notification: true });
    return typeof sent?.message_id === 'number' ? String(sent.message_id) : undefined;
  };
  writer.export = async record => {
    const sent = await bot.api.sendDocument(Number(record.address),
      new InputFile(workProcessDocument(record), 'work-process.html'), { disable_notification: true });
    if (!sent.message_id) throw new Error('Process document delivery lacks confirmation');
  };
  return writer;
}

function feishuButton(control: Control, primary = false): Record<string, unknown> {
  return { tag: 'button', type: primary ? 'primary' : 'default', size: 'small', text: { tag: 'plain_text', content: control.text },
    behaviors: [{ type: 'callback', value: { relay_work_process: control.data } }] };
}
export function feishuProcessCard(record: WorkProcessRecord): Record<string, unknown> {
  const header = workProcessHeader(record);
  const render = (items: ProcessDetailItem[], notice?: string): Record<string, unknown> => {
    // Feishu owns native columns/buttons. Do not reuse Telegram markup or clip
    // the disclosure into the narrow fixed-width icon column that failed UAT.
    // Keep status + metadata as plain Markdown text: desktop Feishu renders an
    // inline <font> span here as a separate block, forcing duration onto row 2.
    const elements: Record<string, unknown>[] = [{ tag: 'column_set', horizontal_spacing: '8px', columns: [
      { tag: 'column', width: 'auto', vertical_align: 'center', elements: [feishuButton(controls(record), true)] },
      { tag: 'column', width: 'auto', vertical_align: 'center', elements: [{ tag: 'markdown', content: '<font color="grey">│</font>' }] },
      { tag: 'column', width: 'weighted', weight: 1, vertical_align: 'center', elements: [
        { tag: 'markdown', content: `${header.marker} ${renderRichDocMarkdown(compileRichDoc(header.status))}${header.meta ? ` · ${html(header.meta)}` : ''}` },
      ] },
    ] }];
    if (record.expanded) {
      for (const item of items) {
        if (item.kind === 'commentary') {
          if (item.source === 'reasoning_summary') elements.push({ tag: 'markdown', text_size: 'notation', content: '<font color="grey">原生推理摘要</font>' });
          elements.push({ tag: 'markdown', content: renderRichDocMarkdown(compileRichDoc(item.detail)) });
        } else {
          elements.push(feishuButton(toolControl(record, item)));
          if (item.expanded && item.detail) {
            const fence = '`'.repeat(Math.max(3, ...Array.from(item.detail.matchAll(/`+/g), match => match[0].length + 1)));
            elements.push({ tag: 'markdown', content: `${fence}\n${item.detail}\n${fence}` });
          }
        }
      }
      if (!record.events.length) elements.push({ tag: 'markdown', content: workProcessDetails(record).emptyText });
      if (notice) elements.push({ tag: 'markdown', content: notice }, feishuButton(allControl(record)));
    }
    return { schema: '2.0', config: { update_multi: true }, body: { padding: '12px', elements } };
  };
  if (!record.expanded) return render([]);
  return selectItems(record, render, card => Buffer.byteLength(JSON.stringify(card)) <= 26_000
    && ((card.body as { elements: unknown[] }).elements.length <= 190));
}

type FeishuProcessClient = { im: {
  message: { create: (...args: any[]) => Promise<any>; patch: (...args: any[]) => Promise<any> };
  file?: { create: (...args: any[]) => Promise<{ file_key?: string } | null> };
} };
export function feishuProcessWriter(client: FeishuProcessClient): ProcessWriter {
  const writer: ProcessWriter = async record => {
    const content = JSON.stringify(feishuProcessCard(record));
    const response = record.messageId
      ? await client.im.message.patch({ path: { message_id: record.messageId }, data: { content } })
      : await client.im.message.create({ params: { receive_id_type: 'open_id' }, data: { receive_id: record.address, msg_type: 'interactive', content } });
    if (response.code !== 0) throw new Error(`Feishu process message rejected: ${response.code}`);
    return record.messageId ?? response.data?.message_id;
  };
  writer.export = async record => {
    if (!client.im.file) throw new Error('Process file transport unavailable');
    const uploaded = await client.im.file.create({ data: { file_type: 'stream', file_name: 'work-process.html', file: workProcessDocument(record) } });
    // The pinned SDK unwraps file uploads to { file_key }, unlike message APIs.
    if (!uploaded?.file_key) throw new Error('Process document upload failed');
    const delivered = await client.im.message.create({ params: { receive_id_type: 'open_id' }, data: {
      receive_id: record.address, msg_type: 'file', content: JSON.stringify({ file_key: uploaded.file_key }) } });
    if (delivered.code !== 0 || !delivered.data?.message_id) throw new Error('Process document delivery lacks confirmation');
  };
  return writer;
}
