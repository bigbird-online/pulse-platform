// Thin client for the Relay loopback HTTP (`feishu-pairing-http`).
//
// Used by Step 2 (`reload`) and Step 3 (`approvePairing`) of the platform
// provisioning flow. Errors are typed so the HTTP layer can map them to user
// feedback without leaking infra details.

const DEFAULT_TIMEOUT_MS = 5000;

export interface RelayClientOptions {
  baseUrl: string;
  fetchImpl?: typeof fetch;
  timeoutMs?: number;
}

export interface RelayReloadResult {
  ok: true;
  added: string[];
  total: number;
}

export interface RelayApproveOk {
  ok: true;
  openId: string;
  agentId: string;
  principalId: string;
}

export type RelayApproveError =
  | { ok: false; error: 'code_unknown' }
  | { ok: false; error: 'agent_mismatch'; expectedAgentId?: string }
  | { ok: false; error: 'config_missing_agent'; message?: string }
  | { ok: false; error: 'relay_unreachable'; detail?: string }
  | { ok: false; error: 'unexpected'; status?: number; detail?: string };

export type RelayApproveResult = RelayApproveOk | RelayApproveError;

export type RelayReloadError =
  | { ok: false; error: 'reload_failed'; message?: string }
  | { ok: false; error: 'relay_unreachable'; detail?: string }
  | { ok: false; error: 'unexpected'; status?: number; detail?: string };

export type RelayReloadResponse = RelayReloadResult | RelayReloadError;

type PostJsonResult = { unreachable: false; status: number; body: unknown } | { unreachable: true; error: string };

async function postJson(
  opts: RelayClientOptions,
  path: string,
  body: unknown,
): Promise<PostJsonResult> {
  const fetchImpl = opts.fetchImpl ?? fetch;
  const timeoutMs = opts.timeoutMs ?? DEFAULT_TIMEOUT_MS;
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  try {
    const res = await fetchImpl(`${opts.baseUrl.replace(/\/$/, '')}${path}`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json; charset=utf-8' },
      body: JSON.stringify(body ?? {}),
      signal: controller.signal,
    });
    let parsed: unknown = undefined;
    try {
      parsed = await res.json();
    } catch {
      parsed = undefined;
    }
    return { unreachable: false, status: res.status, body: parsed };
  } catch (err) {
    return { unreachable: true, error: err instanceof Error ? err.message : String(err) };
  } finally {
    clearTimeout(timer);
  }
}

/** Trigger Relay to spawn newly-added feishu bots (idempotent). */
export async function reloadRelayAgents(opts: RelayClientOptions): Promise<RelayReloadResponse> {
  const r = await postJson(opts, '/internal/agents/reload', {});
  if (r.unreachable) return { ok: false, error: 'relay_unreachable', detail: r.error };
  if (r.status === 200) {
    const b = (r.body ?? {}) as { ok?: boolean; added?: string[]; total?: number };
    if (b.ok) return { ok: true, added: Array.isArray(b.added) ? b.added : [], total: typeof b.total === 'number' ? b.total : 0 };
  }
  if (r.status === 500) {
    const b = (r.body ?? {}) as { error?: string; message?: string };
    return { ok: false, error: 'reload_failed', message: b.message };
  }
  return { ok: false, error: 'unexpected', status: r.status };
}

/** Step 3: forward a pairing code from the user to Relay and finalize the binding. */
export async function approvePairing(
  opts: RelayClientOptions,
  body: { code: string; principalId: string; agentId: string },
): Promise<RelayApproveResult> {
  const r = await postJson(opts, '/internal/pairing/approve', body);
  if (r.unreachable) return { ok: false, error: 'relay_unreachable', detail: r.error };
  if (r.status === 200) {
    const b = (r.body ?? {}) as { ok?: boolean; openId?: string; agentId?: string; principalId?: string };
    if (b.ok && typeof b.openId === 'string' && typeof b.agentId === 'string' && typeof b.principalId === 'string') {
      return { ok: true, openId: b.openId, agentId: b.agentId, principalId: b.principalId };
    }
    return { ok: false, error: 'unexpected', status: 200, detail: 'malformed_relay_response' };
  }
  const b = (r.body ?? {}) as { error?: string; expectedAgentId?: string; message?: string };
  if (r.status === 404 && b.error === 'code_unknown') return { ok: false, error: 'code_unknown' };
  if (r.status === 409 && b.error === 'agent_mismatch') {
    return { ok: false, error: 'agent_mismatch', expectedAgentId: b.expectedAgentId };
  }
  if (r.status === 404 && b.error === 'config_missing_agent') {
    return { ok: false, error: 'config_missing_agent', message: b.message };
  }
  return { ok: false, error: 'unexpected', status: r.status, detail: b.error ?? '' };
}
