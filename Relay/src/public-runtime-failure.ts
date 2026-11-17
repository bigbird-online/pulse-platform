import type { StreamJsonMessage } from './types.js';

const RUNTIME_NAMES: Record<string, string> = {
  pi: 'Pi',
  codex: 'Codex',
  cc: 'Claude Code',
  'cc-sdk': 'Claude SDK',
};
const PUBLIC_ERROR_CODES = new Set([
  'provider_request_failed', 'provider_quota_exceeded', 'provider_rate_limited',
  'provider_auth_failed', 'provider_context_exceeded', 'provider_safety_rejected',
  'runtime_turn_aborted', 'runtime_start_failed',
]);

const DIAGNOSTIC_LIMIT = 900;
const NOTICE_DETAIL_LIMIT = 2000;
const NO_ERROR_DETAIL = '运行时未提供错误原因';

/** Preserve native failure text, removing credentials before bounding output. */
export function redactRuntimeDiagnostic(raw: string): string {
  // Terminal colors can split secret patterns or their word boundaries.
  let text = raw.replace(/\x1b\[[0-?]*[ -/]*[@-~]|[\x00-\x08\x0b\x0c\x0e-\x1f\x7f\u202a-\u202e\u2066-\u2069]/g, '');
  for (const [name, value] of Object.entries(process.env)) {
    if (/(?:TOKEN|SECRET|PASSWORD|API_?KEY|AUTH)/i.test(name)
        && !/(?:_FILE|_PATH|_URL)$/i.test(name) && value && value.length >= 8) {
      text = text.split(value).join('[REDACTED]');
    }
  }
  text = text
    .replace(/-----BEGIN (?:[A-Z ]*PRIVATE KEY)-----[\s\S]*?(?:-----END [A-Z ]*PRIVATE KEY-----|$)/g, '[REDACTED]')
    .replace(/\b(?:Bearer|Basic)\s+[A-Za-z0-9._~+\/=-]+/gi, '[REDACTED]')
    .replace(/\b[a-z][a-z0-9+.-]*:\/\/[^\s<>"']+/gi, (url) => {
      try {
        const parsed = new URL(url);
        const origin = parsed.origin === 'null' ? `${parsed.protocol}//${parsed.host}` : parsed.origin;
        return `${origin}/[REDACTED]`;
      } catch { return '[REDACTED_URL]'; }
    })
    .replace(/["']?[\w-]*(?:token|secret|password|api[_-]?key|authorization|cookie|auth|key)[\w-]*["']?\s*[:=]\s*(?:"(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*'|[^\s,;}]+)/gi, '[REDACTED]')
    .replace(/\b(?:sk-[A-Za-z0-9_-]{10,}|(?:gh[pousr]_|github_pat_|AIza|xox[baprs]-)[A-Za-z0-9_-]{15,}|eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+(?:\.[A-Za-z0-9_-]+)?)/g, '[REDACTED]')
    .replace(/\b\d{6,}:[A-Za-z0-9_-]{20,}/g, '[REDACTED]')
    .replace(/\b(?:secret|token|password|api[_-]?key)[_-][A-Za-z0-9_-]+\b/gi, '[REDACTED]')
    .replace(/\/(?:Users|home|private|tmp|var|etc|Volumes|srv|root|opt)\/[^\s"'<>;,)}\]]+/g, '[REDACTED_PATH]')
    .replace(/\b[A-Za-z]:\\[^\s"'<>;,)}\]]+/g, '[REDACTED_PATH]')
    .trim();
  return text.length > DIAGNOSTIC_LIMIT ? `${text.slice(0, DIAGNOSTIC_LIMIT)}…` : text;
}

function publicErrorCode(value: unknown): string | undefined {
  if (typeof value !== 'string' || !/^[A-Za-z][A-Za-z0-9_-]{0,63}$/.test(value)) return undefined;
  if (!PUBLIC_ERROR_CODES.has(value) && /^(?:secret|token|password|api_key|key)(?:[_-]|$)/i.test(value)) return undefined;
  return redactRuntimeDiagnostic(value) === value ? value : undefined;
}

function nativeErrorText(value: unknown, depth = 0): string | undefined {
  if (typeof value === 'string') return value;
  if (!value || typeof value !== 'object' || depth > 2) return undefined;
  const fields = value as Record<string, unknown>;
  const message = typeof fields.message === 'string' ? fields.message
    : typeof fields.errorMessage === 'string' ? fields.errorMessage
      : nativeErrorText(fields.error, depth + 1);
  const code = publicErrorCode(fields.code ?? fields.type);
  const status = fields.status ?? fields.statusCode;
  const facts = [code, typeof status === 'number' && status >= 100 && status <= 599 ? `HTTP ${status}` : undefined]
    .filter(Boolean).join(' / ');
  // Technical facts must survive even when a long native message is bounded.
  return [facts, message].filter(Boolean).join(' · ') || undefined;
}

function classifiedErrorDetail(message: string): string | undefined {
  const networkCode = message.match(/\b(ECONNRESET|ETIMEDOUT|ECONNREFUSED|ENOTFOUND|EAI_AGAIN|EHOSTUNREACH|ENETUNREACH)\b/i)?.[1]?.toUpperCase();
  if (networkCode) return `网络错误 ${networkCode}`;
  if (/^(?:TypeError:\s*)?fetch failed(?:\b|:)/i.test(message)) return 'fetch failed（模型请求传输失败）';
  if (/^WebSocket error(?:\b|:)/i.test(message)) return 'WebSocket error（模型流连接异常）';
  if (/^You(?:'|’)ve hit your (?:session|weekly) limit\b/i.test(message)) {
    return 'Claude 订阅用量已达上限，等待额度重置后重试';
  }
  if (/^(?:Codex error:\s*)?The usage limit has been reached$/i.test(message)) return '模型订阅用量已达上限';
  if (/^Pi RPC [a-z_]+ request timed out$/i.test(message)) return 'Pi RPC 请求超时';
  if (/^codex conversation runtime timed out after \d+ms$/i.test(message)) return 'Codex 运行超时';
  if (/^cc-sdk engine produced no output within \d+ms after \d+ restart\(s\) \(silent stall\)$/i.test(message)) {
    return 'Claude SDK 未产生输出，重试后仍超时';
  }
  if (/\b(?:HTTP\s*)?429\b|rate.?limit/i.test(message)) return 'HTTP 429（模型服务限流）';
  if (/\b(?:HTTP\s*)?402\b|insufficient credits|insufficient_quota/i.test(message)) return 'HTTP 402（额度或余额不足）';
  if (/\b(?:HTTP\s*)?401\b|unauthorized|authentication failed/i.test(message)) return 'HTTP 401（认证失败）';
  if (/\b(?:HTTP\s*)?403\b|forbidden/i.test(message)) return 'HTTP 403（访问被拒绝）';
  const serverStatus = message.match(/\b(?:HTTP\s*)?(500|502|503|504)\b/i)?.[1];
  if (serverStatus) return `HTTP ${serverStatus}（模型服务异常）`;
  if (/context.{0,20}(limit|length|exceed)|maximum context/i.test(message)) return '模型上下文超限';
  return undefined;
}

/** Recognition supplies an explanation; unfamiliar messages still reach users. */
export function publicRuntimeErrorDetail(value: unknown): string | undefined {
  const raw = nativeErrorText(value);
  if (!raw?.trim()) return undefined;
  const text = redactRuntimeDiagnostic(raw);
  const summary = classifiedErrorDetail(text);
  return summary && text && summary !== text ? `${summary}\n原始错误：${text}` : summary ?? text;
}

function diagnosticDetails(values: unknown[]): string | undefined {
  const selected = [...values];
  const seen = new Set<unknown>(values);
  for (let i = 0; i < selected.length && selected.length < 9; i++) {
    const value = selected[i];
    const cause = value && typeof value === 'object' ? (value as { cause?: unknown }).cause : undefined;
    if (cause && !seen.has(cause)) { selected.push(cause); seen.add(cause); }
  }
  const details = [...new Set(selected.map(publicRuntimeErrorDetail).filter((value): value is string => !!value))];
  if (!details.length) return undefined;
  const text = details.slice(0, 3).join('\n');
  return text.length > NOTICE_DETAIL_LIMIT ? `${text.slice(0, NOTICE_DETAIL_LIMIT)}…` : text;
}

function publicRuntimeName(value: unknown): string {
  return typeof value === 'string' ? (RUNTIME_NAMES[value] ?? '运行时') : '运行时';
}

/** A terminal error event has no assistant text; send a bounded, safe notice. */
export function publicRuntimeExceptionNotice(runtime: unknown, error: unknown): string {
  const detail = diagnosticDetails([error]) ?? NO_ERROR_DETAIL;
  return `⚠️ ${publicRuntimeName(runtime)} 运行失败：${detail}。会话历史保留，可在原会话重试。`;
}

/** Forward selected native failure fields through the shared redaction boundary. */
export function publicRuntimeResultNotice(message: StreamJsonMessage, runtimeHint?: unknown): string | undefined {
  if (message.type !== 'result' || message.is_error !== true || message.public_failure_reported === true) return undefined;
  // Codex emits a more specific error event immediately after its error result.
  if ((message.runtime ?? runtimeHint) === 'codex' &&
      (!Array.isArray(message.errors) || message.errors.length === 0)) return undefined;
  const errors = Array.isArray(message.errors) ? message.errors
    : message.errors === undefined ? [] : [message.errors];
  const status = typeof message.error_status === 'number' ? { status: message.error_status } : undefined;
  const detail = diagnosticDetails([...errors.slice(0, 5), message.error, message.errorMessage, status])
    ?? publicRuntimeErrorDetail(message.result)
    ?? NO_ERROR_DETAIL;
  const codes = [...new Set([message.error_code, message.subtype].map(publicErrorCode).filter(Boolean))];
  const code = codes.length ? ` [${codes.join(' / ')}]` : '';
  return `⚠️ ${publicRuntimeName(message.runtime ?? runtimeHint)} 本次任务失败：${detail}${code}。会话历史保留，可在原会话重试。`;
}
