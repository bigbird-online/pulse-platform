/** Same persistent contract as Harness. Never cache execution permission. */
import { lstatSync, readFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { resolveCronJobsPath } from './config-paths.js';

export interface AutomationDecision {
  allowed: boolean; status: 'manual' | 'enabled' | 'paused' | 'unavailable';
  entry: string; source?: string; revision?: number; reason?: string;
}
export class AutomaticExecutionPaused extends Error {}

export function automationDecision(entry: string, owner: string, manual = false,
  path = join(dirname(resolveCronJobsPath()), 'automation-control.json')): AutomationDecision {
  if (!/^(job|dispatch|resource):[a-zA-Z0-9_.-]{1,160}$/.test(entry)) throw new Error('invalid automation entry');
  if (manual) return { allowed: true, status: 'manual', entry, source: 'explicit_manual_request' };
  try {
    const info = lstatSync(path);
    if (!info.isFile() || info.nlink !== 1 || info.uid !== process.geteuid?.() || (info.mode & 0o022)) throw new Error('unsafe control');
    if (info.size > 65536) throw new Error('oversized control');
    const data = JSON.parse(readFileSync(path, 'utf8'));
    if (data.schema !== 'pulse_automation_control/v1' || typeof data.automatic_enabled !== 'boolean'
      || !Number.isInteger(data.revision) || data.revision < 1 || typeof data.reason !== 'string' || !data.reason
      || typeof data.decision_ref !== 'string' || !data.decision_ref || typeof data.entries !== 'object' || !data.entries || Array.isArray(data.entries)
      || !Number.isFinite(Date.parse(data.updated_at)) || !/(Z|[+-]\d\d:\d\d)$/.test(data.updated_at)) throw new Error('invalid control');
    for (const [key, value] of Object.entries(data.entries)) {
      const v = value as { enabled?: unknown; owner?: unknown };
      if (!/^(job|dispatch|resource):[a-zA-Z0-9_.-]{1,160}$/.test(key) || !v
        || typeof v.enabled !== 'boolean' || typeof v.owner !== 'string' || !v.owner) throw new Error('invalid entry');
    }
    const registered = data.entries[entry];
    const allowed = data.automatic_enabled && registered?.enabled === true && registered.owner === owner;
    return { allowed, status: allowed ? 'enabled' : 'paused', entry, source: path,
      revision: data.revision, reason: data.reason };
  } catch {
    return { allowed: false, status: 'unavailable', entry, reason: 'automatic execution requires valid explicit registration' };
  }
}
export function requireAutomatic(entry: string, owner: string, manual = false): void {
  if (!automationDecision(entry, owner, manual).allowed) throw new AutomaticExecutionPaused('automatic execution paused');
}
