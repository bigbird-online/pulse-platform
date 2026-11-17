import { afterEach, describe, expect, it, vi } from 'vitest';
import { mkdtempSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { startRelayHttpServer, type RelayHttpServerDeps, type RelayHttpServerHandle } from '../src/http-server.js';
import type { Router } from '../src/router.js';
import { COLLAB_DELIVERY_SCHEMA } from '../src/collab-delivery.js';

const body = {
  schema: COLLAB_DELIVERY_SCHEMA, actor: 'hestia', namespace: 'team', surface: 'ownermac',
  holder_key: 'a'.repeat(64), contract_id: 'op_1', work_item_id: 'wi_20261024_001',
  target_work_item_id: 'wi_20261024_002', kind: 'request', status: 'pending',
};
let handle: RelayHttpServerHandle | undefined;
let dir: string | undefined;

async function start(extra: Partial<RelayHttpServerDeps>): Promise<string> {
  dir = mkdtempSync(join(tmpdir(), 'relay-collab-deliver-'));
  const configPath = join(dir, 'relay.json');
  writeFileSync(configPath, JSON.stringify({ users: [], bindings: [] }));
  handle = await startRelayHttpServer({
    feishuBots: { start: async () => {}, stop: () => {}, reload: async () => ({ added: [], removed: [], total: 0 }), runningAgentIds: () => [] } as never,
    router: {} as Router, configPath, ...extra,
  }, 0);
  return `http://127.0.0.1:${handle.port}/internal/collab-deliver`;
}

const post = (url: string, payload: unknown, token?: string) => fetch(url, {
  method: 'POST', headers: { 'content-type': 'application/json', ...(token ? { 'x-relay-token': token } : {}) },
  body: JSON.stringify(payload),
});

afterEach(async () => {
  await handle?.stop();
  if (dir) rmSync(dir, { recursive: true, force: true });
  delete process.env['RELAY_INTERNAL_TOKEN'];
});

describe('Relay HTTP — /internal/collab-deliver', () => {
  it('requires the internal token and a valid request, then returns the delivery status', async () => {
    process.env['RELAY_INTERNAL_TOKEN'] = 'secret';
    const deliverCollaboration = vi.fn(async () => 'started' as const);
    const url = await start({ deliverCollaboration });
    expect((await post(url, body)).status).toBe(401);
    const bad = await post(url, { ...body, schema: 'x' }, 'secret');
    expect([bad.status, (await bad.json()).error]).toEqual([400, 'invalid_schema']);
    const ok = await post(url, body, 'secret');
    expect(await ok.json()).toEqual({ ok: true, status: 'started' });
    expect(deliverCollaboration).toHaveBeenCalledWith(expect.objectContaining({ contract_id: 'op_1' }));
    const merged = { ...body, role: 'submitter', contract_id: 'op_r1', kind: 'pull_request', status: 'merged',
      repository: 'bigbird-online/PULSE', number: 1617, head_sha: 'c'.repeat(40) };
    expect((await post(url, merged, 'secret')).status).toBe(200);
    expect(deliverCollaboration).toHaveBeenLastCalledWith(expect.objectContaining({ role: 'submitter', number: 1617 }));
  });

  it('reports an older Relay without delivery as unavailable', async () => {
    const url = await start({});
    expect((await post(url, body)).status).toBe(503);
  });
});
