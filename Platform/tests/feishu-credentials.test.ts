import { describe, it, expect } from 'vitest';
import { verifyFeishuAppCredentials } from '../src/feishu-credentials.js';

function jsonResponse(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  });
}

describe('verifyFeishuAppCredentials', () => {
  it('returns ok on Feishu code=0', async () => {
    const fetchImpl = (async () =>
      jsonResponse(200, {
        code: 0,
        msg: 'ok',
        tenant_access_token: 't-abc-token',
        expire: 7200,
      })) as unknown as typeof fetch;
    const r = await verifyFeishuAppCredentials({ appId: 'cli_x', appSecret: 'sec', fetchImpl });
    expect(r.ok).toBe(true);
    if (r.ok) {
      expect(r.tenantAccessToken).toBe('t-abc-token');
      expect(r.expire).toBe(7200);
    }
  });

  it('forwards Feishu error code/msg verbatim on invalid secret', async () => {
    const fetchImpl = (async () =>
      jsonResponse(200, {
        code: 99991663,
        msg: 'app_secret invalid, please check it.',
      })) as unknown as typeof fetch;
    const r = await verifyFeishuAppCredentials({ appId: 'cli_x', appSecret: 'wrong', fetchImpl });
    expect(r.ok).toBe(false);
    if (!r.ok) {
      expect(r.error).toBe('invalid_credentials');
      expect(r.code).toBe(99991663);
      expect(r.msg).toContain('app_secret invalid');
    }
  });

  it('returns feishu_unreachable on network error', async () => {
    const fetchImpl = (async () => {
      throw new Error('ECONNREFUSED 127.0.0.1:443');
    }) as unknown as typeof fetch;
    const r = await verifyFeishuAppCredentials({ appId: 'cli_x', appSecret: 'sec', fetchImpl });
    expect(r.ok).toBe(false);
    if (!r.ok) {
      expect(r.error).toBe('feishu_unreachable');
      expect(r.detail).toContain('ECONNREFUSED');
    }
  });

  it('honors timeout and reports feishu_unreachable', async () => {
    const fetchImpl = ((_url: string, init?: RequestInit) =>
      new Promise<Response>((_resolve, reject) => {
        init?.signal?.addEventListener('abort', () => reject(new Error('aborted')));
      })) as unknown as typeof fetch;
    const r = await verifyFeishuAppCredentials({
      appId: 'cli_x',
      appSecret: 'sec',
      fetchImpl,
      timeoutMs: 20,
    });
    expect(r.ok).toBe(false);
    if (!r.ok) {
      expect(r.error).toBe('feishu_unreachable');
    }
  });

  it('returns feishu_unexpected_response on non-2xx HTTP', async () => {
    const fetchImpl = (async () =>
      new Response('Bad Gateway', { status: 502 })) as unknown as typeof fetch;
    const r = await verifyFeishuAppCredentials({ appId: 'cli_x', appSecret: 'sec', fetchImpl });
    expect(r.ok).toBe(false);
    if (!r.ok) {
      expect(r.error).toBe('feishu_unexpected_response');
      expect(r.detail).toContain('http_502');
    }
  });

  it('returns feishu_unexpected_response when token absent (code=0 but token missing)', async () => {
    const fetchImpl = (async () =>
      jsonResponse(200, { code: 0, msg: 'ok' })) as unknown as typeof fetch;
    const r = await verifyFeishuAppCredentials({ appId: 'cli_x', appSecret: 'sec', fetchImpl });
    expect(r.ok).toBe(false);
    if (!r.ok) expect(r.error).toBe('feishu_unexpected_response');
  });
});
