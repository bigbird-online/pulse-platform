import { describe, expect, it, vi } from 'vitest';
import {
  publicRuntimeErrorDetail,
  publicRuntimeExceptionNotice,
  publicRuntimeResultNotice,
} from '../src/public-runtime-failure.js';

describe('public runtime failure boundary', () => {
  it('passes known transport and provider facts without raw secrets', () => {
    expect(publicRuntimeErrorDetail('fetch failed')).toContain('fetch failed');
    expect(publicRuntimeErrorDetail('TypeError: fetch failed: token=secret-key')).toContain('fetch failed（模型请求传输失败）');
    expect(publicRuntimeErrorDetail('WebSocket error')).toContain('WebSocket');
    expect(publicRuntimeErrorDetail('request failed ECONNRESET at https://provider.test/key/secret-key'))
      .toContain('网络错误 ECONNRESET');
    expect(publicRuntimeErrorDetail('502 upstream failed; token=secret-key')).toContain('HTTP 502（模型服务异常）');
    expect(publicRuntimeErrorDetail('unknown: token=secret-key path=/private/data')).toContain('unknown:');
    expect(publicRuntimeExceptionNotice('codex', new Error('unknown: token=secret-key path=/private/data')))
      .not.toContain('secret-key');
    const withCause = new TypeError('fetch failed', { cause: new Error('connect ECONNRESET token=secret-key') });
    expect(publicRuntimeExceptionNotice('pi', withCause)).toContain('网络错误 ECONNRESET');
    expect(publicRuntimeExceptionNotice('pi', withCause)).not.toContain('secret-key');
  });

  it('renders a native failed result once and leaves Pi and Codex adapter notices to their own terminal paths', () => {
    const result = { type: 'result', is_error: true, errors: ['fetch failed; token=secret-key ECONNRESET'], error_code: 'provider_request_failed' };
    const notice = publicRuntimeResultNotice(result, 'cc-sdk');
    expect(notice).toContain('Claude SDK');
    expect(notice).toContain('ECONNRESET');
    expect(notice).toContain('[provider_request_failed]');
    expect(notice).not.toContain('secret-key');
    expect(publicRuntimeResultNotice({ ...result, error_code: 'secret_key_123' }, 'cc-sdk')).not.toContain('secret_key_123');
    expect(publicRuntimeResultNotice({ ...result, public_failure_reported: true }, 'pi')).toBeUndefined();
    expect(publicRuntimeResultNotice({ type: 'result', is_error: true }, 'codex')).toBeUndefined();
    expect(publicRuntimeResultNotice({ type: 'result', is_error: true, errors: [] }, 'codex')).toBeUndefined();
  });

  it('reports Claude subscription limits without exposing private diagnostics', () => {
    for (const error of [
      "You've hit your session limit · resets 11:50am (Asia/Shanghai)",
      "You've hit your weekly limit · resets Oct 2 at 11am; token=secret-key path=/private/data",
    ]) {
      const notice = publicRuntimeResultNotice({ type: 'result', is_error: true, errors: [error] }, 'cc');
      expect(notice).toContain('Claude 订阅用量已达上限');
      expect(notice).toContain('等待额度重置后重试');
      expect(notice).not.toContain('安全诊断规则');
      expect(notice).not.toContain('secret-key');
      expect(notice).not.toContain('/private/data');
    }
    expect(publicRuntimeErrorDetail("User said: You've hit your session limit; token=secret-key"))
      .not.toContain('secret-key');
  });

  it('passes unfamiliar native failure messages and the original quota reset time', () => {
    const notice = publicRuntimeResultNotice({ type: 'result', is_error: true,
      errors: ["You've hit your session limit · resets 11:50am (Asia/Shanghai)",
        'Native parser rejected response schema: missing output_items'], subtype: 'error_during_execution' }, 'cc');
    expect(notice).toContain('11:50am (Asia/Shanghai)');
    expect(notice).toContain('missing output_items');
    expect(notice).toContain('error_during_execution');
    expect(notice).not.toContain('安全诊断规则');
  });

  it('preserves structured error messages, codes and nested causes', () => {
    const error = Object.assign(new Error('Native output schema rejected', {
      cause: { message: 'Missing output_items', code: 'invalid_response', status: 422 },
    }), { code: 'ERR_NATIVE_OUTPUT' });
    const notice = publicRuntimeExceptionNotice('cc-sdk', error);
    expect(notice).toContain('Native output schema rejected');
    expect(notice).toContain('Missing output_items');
    expect(notice).toContain('ERR_NATIVE_OUTPUT');
    expect(notice).toContain('invalid_response');
    expect(notice).toContain('422');
    const result = publicRuntimeResultNotice({ type: 'result', is_error: true,
      error: { message: 'Invalid model route', code: 'model_not_found', status: 404 } }, 'cc');
    expect(result).toContain('Invalid model route');
    expect(result).toContain('model_not_found');
    const long = publicRuntimeExceptionNotice('cc', { message: 'Native rejection ' + 'x'.repeat(4000),
      code: 'invalid_response', status: 422 });
    expect(long).toContain('invalid_response');
    expect(long).toContain('422');
  });

  it('uses native failed-result text and subtype when separate errors are absent', () => {
    expect(publicRuntimeResultNotice({ type: 'result', is_error: true,
      result: 'Unable to open the configured output stream' }, 'cc')).toContain('configured output stream');
    expect(publicRuntimeResultNotice({ type: 'result', is_error: true,
      subtype: 'error_max_turns' }, 'cc')).toContain('error_max_turns');
    const scalar = publicRuntimeResultNotice({ type: 'result', is_error: true,
      errors: 'Native request rejected: invalid output format', error_status: 422 }, 'cc');
    expect(scalar).toContain('invalid output format');
    expect(scalar).toContain('422');
    expect(publicRuntimeResultNotice({ type: 'result', is_error: true,
      errorMessage: 'Native response signature does not match' }, 'cc')).toContain('signature does not match');
  });

  it('redacts credentials before bounding unknown diagnostics', () => {
    const native = 'Native schema rejected; Authorization: Bearer bearer-secret-value; ' +
      '"api_key": "quoted-secret-value"; cookie=session-secret-value; ' +
      'https://user:password@provider.test/secret-path?access_token=query-secret-value; ' +
      'sk-ant-oat01-' + 'a'.repeat(50) + '; bot 1234567890:' + 'b'.repeat(35) +
      '; path=/Users/example/private/file.json; ' + 'x'.repeat(4000);
    const notice = publicRuntimeExceptionNotice('cc', new Error(native));
    expect(notice).toContain('Native schema rejected');
    for (const secret of ['bearer-secret-value', 'quoted-secret-value', 'session-secret-value',
      'password', 'secret-path', 'query-secret-value', 'a'.repeat(50), 'b'.repeat(35), '/Users/example/private']) {
      expect(notice).not.toContain(secret);
    }
    expect(notice!.length).toBeLessThan(2500);
    expect(publicRuntimeResultNotice({ type: 'result', is_error: true,
      errors: [{ prompt: 'private prompt', headers: { authorization: 'private key' } }] }, 'cc'))
      .not.toContain('private prompt');
    const colored = publicRuntimeExceptionNotice('cc', new Error(
      'Native request failed: \x1b[31msk-ant-oat01-' + 'c'.repeat(50) + '\x1b[0m; ' +
      'Authorization: Bearer \x1b[31mopaque-colored-credential\x1b[0m'));
    expect(colored).toContain('Native request failed');
    expect(colored).not.toContain('c'.repeat(50));
    expect(colored).not.toContain('opaque-colored-credential');
  });

  it('removes known environment credentials and bounds cyclic structured causes', () => {
    vi.stubEnv('RELAY_INTERNAL_TOKEN', 'opaque-fixture-credential-value');
    try {
      const error = { message: 'Unknown upstream failure opaque-fixture-credential-value', cause: {} as unknown };
      error.cause = error;
      const notice = publicRuntimeExceptionNotice('cc', error);
      expect(notice).toContain('Unknown upstream failure');
      expect(notice).not.toContain('opaque-fixture-credential-value');
      expect(notice.length).toBeLessThan(2500);
    } finally { vi.unstubAllEnvs(); }
  });

  it('redacts authentication and private data from non-HTTP URLs without inventing HTTP failures', () => {
    for (const url of [
      'wss://alice:short-password@stream.test/private/401?session=opaque-private-value',
      'postgresql://alice:short-password@database.test/private/401?session=opaque-private-value',
      'file:///Users/example/private/401?session=opaque-private-value',
    ]) {
      const notice = publicRuntimeExceptionNotice('cc', new Error('Native negotiation failed at ' + url));
      expect(notice).toContain('Native negotiation failed');
      for (const value of ['alice', 'short-password', '/private/401', 'opaque-private-value', 'HTTP 401']) {
        expect(notice).not.toContain(value);
      }
    }
  });
});
