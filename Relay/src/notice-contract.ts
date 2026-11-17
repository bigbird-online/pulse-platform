import { createHash } from 'node:crypto';

export const NOTICE_CONTRACT = 'relay-notice-single-attempt/v1';

export interface NoticeBinding {
  contract: typeof NOTICE_CONTRACT;
  requestKey: string;
  routeDigest: string;
  textSha256: string;
}

const HEX = /^[a-f0-9]{64}$/;

/** A dedicated endpoint: old push servers cannot silently ignore this contract. */
export function parseNoticeBinding(body: unknown): NoticeBinding | null {
  if (!body || typeof body !== 'object' || Array.isArray(body)) return null;
  const obj = body as Record<string, unknown>;
  const allowed = new Set(['channel', 'agentId', 'to', 'text', 'source', 'notice',
    'requiresHuman', 'humanDecisionKind', 'authorityFingerprint']);
  if (Object.keys(obj).some(key => !allowed.has(key))) return null;
  if (obj.channel !== 'telegram' && obj.channel !== 'feishu') return null;
  if (typeof obj.agentId !== 'string' || !/^[a-zA-Z0-9_-]{1,128}$/.test(obj.agentId)) return null;
  if (typeof obj.to !== 'string' || obj.to.length > 128) return null;
  if (obj.channel === 'telegram'
    ? !/^-?[1-9][0-9]*$/.test(obj.to) || !Number.isSafeInteger(Number(obj.to))
    : !/^ou_[a-zA-Z0-9_-]+$/.test(obj.to)) return null;
  if (typeof obj.text !== 'string' || !obj.text.trim() || obj.text.includes('\0')
    || Buffer.byteLength(obj.text, 'utf8') > 4096) return null;
  if (obj.source !== 'harness-escalation' && obj.source !== 'harness-operational') return null;
  if (obj.source === 'harness-operational'
    && ['requiresHuman', 'humanDecisionKind', 'authorityFingerprint'].some(key => key in obj)) return null;
  const value = obj.notice;
  if (!value || typeof value !== 'object' || Array.isArray(value)) return null;
  const notice = value as Record<string, unknown>;
  if (Object.keys(notice).sort().join(',') !== 'contract,requestKey,routeDigest,textSha256'
    || notice.contract !== NOTICE_CONTRACT
    || !['requestKey', 'routeDigest', 'textSha256'].every(
      key => typeof notice[key] === 'string' && HEX.test(notice[key] as string),
    )
    || notice.textSha256 !== createHash('sha256').update(obj.text).digest('hex')) return null;
  return notice as unknown as NoticeBinding;
}
