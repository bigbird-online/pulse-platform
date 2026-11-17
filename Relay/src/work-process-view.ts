import { randomBytes } from 'node:crypto';
import { TURN_ACCEPTED_STATUS_TEXT, TURN_GENERATED_STATUS_TEXT } from './channel-rendering.js';
import { sanitizeWorkText, suppressWorkOutput, type WorkProcessEvent } from './work-process-events.js';
import { loadWorkProcess, saveWorkProcess, type WorkProcessRecord } from './work-process-archive.js';

export const WORK_PROCESS_FAILED_STATUS = '⚠️ 本轮回复未完成';
export function workProcessEnabled(): boolean {
  return !['off', 'false', '0'].includes(process.env['RELAY_WORK_PROCESS_DETAILS']?.toLowerCase() ?? 'on');
}
export { workProcessDetails } from './work-process-content.js';
export type { ProcessDetails, ProcessDetailItem } from './work-process-content.js';
import { workProcessDetails, workProcessSummary, toolDisclosureKey } from './work-process-content.js';
export const processCallback = (record: WorkProcessRecord, action: string): string => `wp:${record.id}:${action}`;
export type ProcessWriter = ((record: WorkProcessRecord) => Promise<string | undefined>) & {
  export?: (record: WorkProcessRecord) => Promise<void>;
};
type View = { record: WorkProcessRecord; chain?: Promise<void>; dirty: boolean; lastWrite: number;
  revision: number; bytes: number; creationUncertain: boolean; visible?: string;
  streamText?: string; streamId?: string; streamBase?: string; exporting?: Promise<void>;
  /** Last answer-text commentary and whether the runtime named a final frame. */
  lastAnswerId?: string; finalFramed?: boolean };

/** One process message per turn. Callbacks never invoke the Agent. */
export class WorkProcessView {
  private readonly current = new Map<string, View>();
  private readonly history = new Map<string, View>();
  constructor(private readonly scope: string, private readonly write: ProcessWriter, private readonly throttleMs = 1_500) {}

  begin(poolKey: string): string {
    const old = this.current.get(poolKey);
    if (old && !old.record.terminal) {
      old.record.terminal = true;
      old.record.finishedAt = Date.now();
      old.record.summary = WORK_PROCESS_FAILED_STATUS;
      void this.publish(old);
    }
    const record: WorkProcessRecord = { version: 1, id: randomBytes(12).toString('hex'), scope: this.scope,
      address: '', createdAt: Date.now(), summary: TURN_ACCEPTED_STATUS_TEXT, expanded: false,
      page: 0, terminal: false, events: [], omitted: 0, activityObserved: false };
    const view: View = { record, dirty: false, lastWrite: 0, revision: 0, bytes: 0, creationUncertain: false };
    this.current.set(poolKey, view);
    this.history.set(record.id, view);
    // Terminal views also remain on disk; bound only the in-memory cache here.
    if (this.history.size > 64) {
      for (const [id, entry] of this.history) {
        if (entry.record.terminal && !entry.chain) { this.history.delete(id); break; }
      }
    }
    return record.id;
  }
  async drain(poolKey: string, expectedId?: string): Promise<void> {
    const view = this.current.get(poolKey);
    if (view && (!expectedId || view.record.id === expectedId)) await view.chain;
  }
  idFor(poolKey: string): string | undefined { return this.current.get(poolKey)?.record.id; }
  frame(poolKey: string, address: string, phase: string, text: string, eventId?: string): Promise<void> {
    const view = this.current.get(poolKey);
    if (!view || view.record.terminal) return Promise.resolve();
    view.record.address = address;
    if (phase === 'activity') view.record.activityObserved = true;
    if (phase === 'status' || phase === 'activity') view.record.summary = text;
    else if (phase === 'final') { view.finalFramed = true; view.record.finalEventId = eventId ?? view.streamId; }
    else {
      let id = eventId ?? `note:${view.revision}`;
      let body = text;
      if (phase === 'snapshot' || phase === 'delta') {
        const full = phase === 'delta' ? `${view.streamText ?? ''}${text}` : text;
        if (!view.streamId || !full.startsWith(view.streamText ?? '')) {
          view.streamId = `stream:${id}`;
          view.streamBase = full.startsWith(view.streamText ?? '') ? view.streamText ?? '' : '';
        }
        id = view.streamId;
        body = full.slice((view.streamBase ?? '').length);
        view.streamText = full;
      }
      if (body) {
        this.append(view, { id, kind: 'commentary', state: 'completed', output: body });
        view.lastAnswerId = id;
      }
    }
    return this.publish(view);
  }
  event(poolKey: string, address: string, event: WorkProcessEvent): Promise<void> {
    const view = this.current.get(poolKey);
    if (!view || view.record.terminal) return Promise.resolve();
    view.record.address = address;
    if (event?.kind === 'operation' && !view.record.events.some(item => item.id === event.id)) view.streamId = undefined;
    this.append(view, event);
    return this.publish(view);
  }
  private append(view: View, event: WorkProcessEvent): void {
    if (!event || typeof event.id !== 'string' || !['operation', 'commentary'].includes(event.kind)
      || !['running', 'completed', 'failed'].includes(event.state)) return;
    const id = event.id.slice(0, 160);
    const old = view.record.events.find(item => item.id === id);
    if (old && old.state !== 'running' && event.state === 'running') return;
    if (!old && view.record.events.length >= 2_000) { view.record.omitted += 1; return; }
    const input = sanitizeWorkText(event.input, 2_000) || old?.input;
    const restricted = event.restricted === true || old?.restricted === true || suppressWorkOutput(input);
    const merged: WorkProcessEvent = { id, kind: event.kind, state: event.state,
      ...((event.source ?? old?.source) === 'reasoning_summary' ? { source: 'reasoning_summary' as const } : {}),
      title: sanitizeWorkText(event.title, 120) || old?.title,
      input, ...(restricted ? { restricted: true } : {}), output: restricted ? '[敏感或内部文件内容不展示]'
        : sanitizeWorkText(event.output, event.kind === 'commentary' ? 512_000 : 32_000) || old?.output };
    const size = Buffer.byteLength(JSON.stringify(merged));
    const priorSize = old ? Buffer.byteLength(JSON.stringify(old)) : 0;
    // Reserve the upper half of the existing turn capacity for later public
    // commentary; hidden tool output must not consume the entire narrative store.
    const contentLimit = event.kind === 'operation' ? 1_000_000 : 2_000_000;
    if (view.bytes + size - priorSize > contentLimit) {
      merged.input = undefined;
      merged.output = event.kind === 'operation'
        ? '[工具正文达到安全保留边界；优先保留后续过程说明]'
        : '[本轮内容超过安全展示容量；正文未保留]';
      view.record.omitted += 1;
      if (view.bytes + Buffer.byteLength(JSON.stringify(merged)) - priorSize > 2_000_000) return;
    }
    view.bytes += Buffer.byteLength(JSON.stringify(merged)) - priorSize;
    if (old) Object.assign(old, merged); else view.record.events.push(merged);
  }
  async terminal(poolKey: string, success: boolean, answer = '', expectedId?: string): Promise<void> {
    const view = this.current.get(poolKey);
    if (!view || view.record.terminal || (expectedId && view.record.id !== expectedId)) return;
    view.record.terminal = true;
    view.record.finishedAt = Date.now();
    view.record.summary = success ? TURN_GENERATED_STATUS_TEXT : WORK_PROCESS_FAILED_STATUS;
    if (success) {
      view.record.expanded = false;
      // Final exclusion is by message identity, never by text equality. A
      // runtime without a final phase (CC stream-json) delivers its last answer
      // text as the reply, so that commentary is the final message.
      const id = view.record.finalEventId ?? (view.finalFramed ? undefined : view.lastAnswerId);
      if (id) view.record.events = view.record.events.filter(event => event.kind !== 'commentary' || event.id !== id);
    }
    await this.publish(view);
  }
  async callback(data: string, address: string, messageId: string, waitForUpdate = true): Promise<boolean> {
    const match = /^wp:([a-f0-9]{24}):(open|close|all|t[a-f0-9]{16}[oc]|p\d{1,6})$/.exec(data);
    if (!match) return false;
    const [, id, action] = match;
    let view = this.history.get(id);
    if (!view) {
      const record = loadWorkProcess(id, this.scope);
      if (record) {
        view = { record, dirty: false, lastWrite: 0, revision: 0, bytes: Buffer.byteLength(JSON.stringify(record.events)), creationUncertain: false };
        this.history.set(id, view);
      }
    }
    if (!view || view.record.address !== address || view.record.messageId !== messageId || view.record.scope !== this.scope) return false;
    if (action === 'all') {
      if (!this.write.export) return false;
      if (!view.exporting) {
        const current = view;
        current.exporting = this.write.export(structuredClone(current.record))
          .catch(() => console.warn({ msg: 'Work process document delivery failed' }))
          .finally(() => { current.exporting = undefined; });
      }
      if (waitForUpdate) await view.exporting;
      return true;
    }
    if (action.startsWith('t')) {
      const matches = view.record.events.filter(event => event.kind === 'operation' && toolDisclosureKey(event.id) === action.slice(1, -1));
      if (matches.length !== 1) return false;
      const opened = new Set(view.record.openTools ?? []);
      if (action.endsWith('o')) opened.add(matches[0].id); else opened.delete(matches[0].id);
      view.record.openTools = [...opened];
    }
    else if (action === 'open') view.record.expanded = true;
    else if (action === 'close') view.record.expanded = false;
    else view.record.expanded = true; // Old page callbacks reopen the continuous view.
    view.record.page = 0;
    const update = this.publish(view);
    // The Feishu callback deadline must not include API latency or edit retries.
    // Scope and state are validated synchronously; the same view owns the update.
    if (waitForUpdate) await update;
    else void update.catch(() => console.warn({ msg: 'Work process callback update failed' }));
    return true;
  }
  private publish(view: View): Promise<void> {
    view.dirty = true;
    view.revision += 1;
    saveWorkProcess(view.record);
    if (view.chain) return view.chain;
    view.chain = Promise.resolve().then(async () => {
      let failures = 0;
      while (view.dirty && view.record.address && !view.creationUncertain) {
        const delay = Math.max(0, this.throttleMs - (Date.now() - view.lastWrite));
        if (view.record.messageId && delay) await new Promise(resolve => setTimeout(resolve, delay));
        view.dirty = false;
        // Snapshot after any wait. Count/expanded state may have changed meanwhile.
        const snapshot = structuredClone(view.record);
        const visible = JSON.stringify({ summary: workProcessSummary(snapshot), expanded: snapshot.expanded,
          details: snapshot.expanded ? { ...workProcessDetails(snapshot),
            items: workProcessDetails(snapshot).items.map(item => ({ ...item, detail: item.expanded ? item.detail : undefined })) } : undefined });
        if (view.record.messageId && visible === view.visible) continue;
        try {
          const id = await this.write(snapshot);
          if (!view.record.messageId) {
            if (!id) throw new Error('Process message send returned no identity');
            view.record.messageId = id;
          }
          view.visible = visible;
          view.record.mode = snapshot.mode;
          failures = 0;
          saveWorkProcess(view.record);
        } catch {
          // Retrying an edit is safe; retrying an ambiguous creation is not.
          if (!view.record.messageId) view.creationUncertain = true;
          else if (++failures <= 3) {
            view.dirty = true;
            await new Promise(resolve => setTimeout(resolve, 1_500 * 2 ** (failures - 1)));
          }
          console.warn({ msg: 'Work process view update failed', phase: snapshot.terminal ? 'terminal' : 'active' });
        } finally { view.lastWrite = Date.now(); }
      }
    }).finally(() => {
      view.chain = undefined;
      // A second disclosure callback can arrive after the drain loop exits but
      // before its promise settles. Do not strand that last dirty revision.
      if (view.dirty && view.record.address && !view.creationUncertain) return this.publish(view);
    });
    return view.chain;
  }
}
