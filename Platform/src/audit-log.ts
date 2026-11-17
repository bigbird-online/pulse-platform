import { appendFileSync, mkdirSync } from 'node:fs';
import { dirname } from 'node:path';

export type AuditEvent = {
  ts: string;
  action: string;
  orderId?: string;
  tenantId?: string;
  detail?: Record<string, unknown>;
};

/** 若设置 `PLATFORM_AUDIT_LOG_PATH`，追加单行 JSON（JSONL）。 */
export function appendAuditLog(event: AuditEvent, env: NodeJS.ProcessEnv = process.env): void {
  const path = env['PLATFORM_AUDIT_LOG_PATH']?.trim();
  if (!path) return;
  mkdirSync(dirname(path), { recursive: true });
  appendFileSync(path, `${JSON.stringify(event)}\n`, 'utf8');
}
