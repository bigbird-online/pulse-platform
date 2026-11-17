import { mkdirSync, readFileSync, writeFileSync, renameSync, readdirSync, statSync, unlinkSync, chmodSync } from 'node:fs';
import { join } from 'node:path';
import type { WorkProcessEvent } from './work-process-events.js';

export type WorkProcessRecord = {
  version: 1; id: string; scope: string; address: string; messageId?: string;
  createdAt: number; finishedAt?: number; openTools?: string[]; finalEventId?: string; summary: string; expanded: boolean; page: number;
  terminal: boolean; events: WorkProcessEvent[]; omitted: number; mode?: string; activityObserved?: boolean;
};
const TTL = 7 * 24 * 60 * 60 * 1_000;
let directory: string | undefined;
const validId = (id: string): boolean => /^[a-f0-9]{24}$/.test(id);
/** Uses the same runtime-owned directory as Relay state, never a source checkout. */
export function enableWorkProcessArchive(path?: string): void {
  directory = path;
  if (!path) return;
  mkdirSync(path, { recursive: true, mode: 0o700 });
  chmodSync(path, 0o700);
  prune();
}
function prune(): void {
  if (!directory) return;
  const entries = readdirSync(directory).filter(name => /^[a-f0-9]{24}\.json$/.test(name))
    .map(name => ({ name, time: statSync(join(directory!, name)).mtimeMs }))
    .sort((a, b) => b.time - a.time);
  for (const [index, entry] of entries.entries()) {
    if (index >= 512 || Date.now() - entry.time > TTL) unlinkSync(join(directory, entry.name));
  }
}
export function saveWorkProcess(record: WorkProcessRecord): void {
  if (!directory || !validId(record.id)) return;
  try {
    const path = join(directory, `${record.id}.json`);
    const temp = `${path}.tmp`;
    writeFileSync(temp, JSON.stringify(record), { mode: 0o600 });
    renameSync(temp, path);
    if (record.terminal || record.events.length === 0) prune();
  } catch { console.warn({ msg: 'Work process archive write failed' }); }
}
export function loadWorkProcess(id: string, scope: string): WorkProcessRecord | undefined {
  if (!directory || !validId(id)) return;
  try {
    const path = join(directory, `${id}.json`);
    if (statSync(path).size > 4_000_000) return;
    const record = JSON.parse(readFileSync(path, 'utf8')) as WorkProcessRecord;
    if (record.version !== 1 || record.id !== id || record.scope !== scope
      || !Number.isFinite(record.createdAt) || Date.now() - record.createdAt > TTL
      || typeof record.address !== 'string' || typeof record.summary !== 'string'
      || typeof record.messageId !== 'string' || typeof record.expanded !== 'boolean'
      || (record.activityObserved !== undefined && typeof record.activityObserved !== 'boolean')
      || (record.finishedAt !== undefined && (!Number.isFinite(record.finishedAt) || record.finishedAt < record.createdAt))
      || (record.openTools !== undefined && (!Array.isArray(record.openTools) || record.openTools.length > 2_000
        || !record.openTools.every(id => typeof id === 'string' && id.length <= 160)))
      || (record.finalEventId !== undefined && typeof record.finalEventId !== 'string')
      || !Number.isSafeInteger(record.page) || record.page < 0
      || !Array.isArray(record.events) || record.events.length > 2_000
      || !record.events.every(event => event && typeof event.id === 'string'
        && ['operation', 'commentary'].includes(event.kind)
        && ['running', 'completed', 'failed'].includes(event.state)
        && (event.source === undefined || event.source === 'reasoning_summary'))) return;
    // A restart does not prove the old Agent is still executing.
    if (!record.terminal) { record.terminal = true; record.summary = '⚠️ 本轮回复未完成'; }
    return record;
  } catch { return; }
}
