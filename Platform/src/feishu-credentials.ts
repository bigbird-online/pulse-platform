// Feishu credential validation (FR-P0-011 / AC-P0-005).
//
// Calls the official Lark/Feishu API:
//   POST https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal
//   body: { app_id, app_secret }
//   docs: https://open.feishu.cn/document/server-docs/authentication-management/access-token/tenant_access_token_internal
//
// Returns the original code/msg from Feishu when validation fails so the user can debug.

const FEISHU_TENANT_TOKEN_ENDPOINT =
  'https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal';
const DEFAULT_TIMEOUT_MS = 15000;

export interface FeishuVerifyOk {
  ok: true;
  tenantAccessToken: string;
  expire: number;
}

export interface FeishuVerifyFail {
  ok: false;
  error: 'invalid_credentials' | 'feishu_unreachable' | 'feishu_unexpected_response';
  /** Feishu's original numeric error code, when present. */
  code?: number;
  /** Feishu's original error message, when present. */
  msg?: string;
  /** Underlying technical detail (timeout / parse error / non-2xx). */
  detail?: string;
}

export type FeishuVerifyResult = FeishuVerifyOk | FeishuVerifyFail;

export interface VerifyFeishuAppCredentialsOptions {
  appId: string;
  appSecret: string;
  /** Override for tests. */
  fetchImpl?: typeof fetch;
  /** Override for tests; default from `FEISHU_TENANT_TOKEN_ENDPOINT`. */
  endpoint?: string;
  /** Override for tests; default `DEFAULT_TIMEOUT_MS`. */
  timeoutMs?: number;
}

interface FeishuTenantTokenResponse {
  code?: number;
  msg?: string;
  tenant_access_token?: string;
  expire?: number;
}

/**
 * Hits Feishu's tenant_access_token/internal endpoint. Treats Feishu's `code === 0`
 * as success; anything else (or HTTP failure / timeout) is a typed failure with
 * the original Feishu code/msg surfaced for the UI.
 */
export async function verifyFeishuAppCredentials(
  opts: VerifyFeishuAppCredentialsOptions,
): Promise<FeishuVerifyResult> {
  const endpoint = opts.endpoint ?? FEISHU_TENANT_TOKEN_ENDPOINT;
  const fetchImpl = opts.fetchImpl ?? fetch;
  const timeoutMs = opts.timeoutMs ?? DEFAULT_TIMEOUT_MS;

  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  try {
    const res = await fetchImpl(endpoint, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json; charset=utf-8' },
      body: JSON.stringify({ app_id: opts.appId, app_secret: opts.appSecret }),
      signal: controller.signal,
    });
    if (!res.ok) {
      const text = await safeText(res);
      return {
        ok: false,
        error: 'feishu_unexpected_response',
        detail: `http_${res.status}: ${truncate(text, 200)}`,
      };
    }
    let body: FeishuTenantTokenResponse;
    try {
      body = (await res.json()) as FeishuTenantTokenResponse;
    } catch (e) {
      return {
        ok: false,
        error: 'feishu_unexpected_response',
        detail: e instanceof Error ? e.message : String(e),
      };
    }
    if (typeof body.code === 'number' && body.code !== 0) {
      return {
        ok: false,
        error: 'invalid_credentials',
        code: body.code,
        msg: body.msg,
      };
    }
    if (!body.tenant_access_token) {
      return {
        ok: false,
        error: 'feishu_unexpected_response',
        detail: 'missing_tenant_access_token',
      };
    }
    return {
      ok: true,
      tenantAccessToken: body.tenant_access_token,
      expire: typeof body.expire === 'number' ? body.expire : 0,
    };
  } catch (e) {
    const detail = e instanceof Error ? e.message : String(e);
    return { ok: false, error: 'feishu_unreachable', detail };
  } finally {
    clearTimeout(timer);
  }
}

async function safeText(res: Response): Promise<string> {
  try {
    return await res.text();
  } catch {
    return '';
  }
}

function truncate(s: string, max: number): string {
  return s.length > max ? `${s.slice(0, max)}...` : s;
}
