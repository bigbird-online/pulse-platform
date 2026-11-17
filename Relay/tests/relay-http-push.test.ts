import { createHash } from 'node:crypto';
import { Bot as RealBot } from 'grammy';
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { mkdtempSync, writeFileSync, rmSync, type ReadStream } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import type { Bot } from 'grammy';
import { startRelayHttpServer, startNoticeOnlyServer, type RelayHttpServerHandle } from '../src/http-server.js';
import type { FeishuMessageClient } from '../src/delivery.js';
import type { Router } from '../src/router.js';
import { JobTerminalRegistry } from '../src/job-terminal-registry.js';

vi.mock('../src/channels/feishu.js', async (importOriginal) => {
  const orig = await importOriginal<typeof import('../src/channels/feishu.js')>();
  return {
    ...orig,
    peekPendingPairingForHttp: vi.fn(),
    consumePendingPairingForHttp: vi.fn(),
    approveFeishuPairingViaHttp: vi.fn(),
  };
});

let handle: RelayHttpServerHandle;
let testDir: string;
let configPath: string;
let tgSendMessage: ReturnType<typeof vi.fn>;
let feishuMessageCreate: ReturnType<typeof vi.fn>;
let feishuImageCreate: ReturnType<typeof vi.fn>;
let feishuFileCreate: ReturnType<typeof vi.fn>;
let getTelegramBot: ((agentId: string) => Bot | null) | undefined;
let getFeishuClient: ((agentId: string) => FeishuMessageClient | null) | undefined;
let terminalRegistry: JobTerminalRegistry;

const fakeRouter = { upsertGatewayBinding: vi.fn() } as unknown as Router;

function makeFakeBot(): Bot {
  return {
    api: { sendMessage: tgSendMessage },
  } as unknown as Bot;
}

function makeFakeFeishuClient(): FeishuMessageClient {
  return {
    im: {
      message: { create: feishuMessageCreate },
      image: { create: feishuImageCreate },
      file: { create: feishuFileCreate },
    },
  } as unknown as FeishuMessageClient;
}

function consumeUploadStream<T>(stream: ReadStream, result: T): Promise<T> {
  return new Promise((resolve, reject) => {
    stream.once('open', () => {
      stream.destroy();
      resolve(result);
    });
    stream.once('error', reject);
  });
}

async function startServer(): Promise<RelayHttpServerHandle> {
  const fakeFeishuBots = {
    start: async () => {},
    stop: () => {},
    reload: async () => ({ added: [], total: 0 }),
    runningAgentIds: () => [],
  };
  return startRelayHttpServer(
    {
      feishuBots: fakeFeishuBots,
      router: fakeRouter,
      configPath,
      startedAtMs: Date.now(),
      ...(getTelegramBot ? { getTelegramBot } : {}),
      ...(getFeishuClient ? { getFeishuClient } : {}),
      terminalRegistry,
    },
    0,
  );
}

beforeEach(async () => {
  testDir = mkdtempSync(join(tmpdir(), 'relay-http-push-test-'));
  configPath = join(testDir, 'relay.json');
  writeFileSync(configPath, JSON.stringify({ users: [], bindings: [] }, null, 2));
  tgSendMessage = vi.fn().mockResolvedValue({ message_id: 999 });
  feishuMessageCreate = vi.fn().mockResolvedValue({ data: { message_id: 'om_xxx' } });
  feishuImageCreate = vi.fn().mockImplementation((arg: { data: { image: ReadStream } }) => (
    consumeUploadStream(arg.data.image, { image_key: 'img_key_abc' })
  ));
  feishuFileCreate = vi.fn().mockImplementation((arg: { data: { file: ReadStream } }) => (
    consumeUploadStream(arg.data.file, { file_key: 'file_key_abc' })
  ));
  getTelegramBot = (id) => (id === 'pulse' ? makeFakeBot() : null);
  getFeishuClient = (id) => (id === 'pulse' ? makeFakeFeishuClient() : null);
  terminalRegistry = new JobTerminalRegistry();
  delete process.env['RELAY_INTERNAL_TOKEN'];
  handle = await startServer();
});

afterEach(async () => {
  await handle.stop();
  rmSync(testDir, { recursive: true, force: true });
  delete process.env['RELAY_INTERNAL_TOKEN'];
  vi.clearAllMocks();
});

function url(path: string): string {
  return `http://127.0.0.1:${handle.port}${path}`;
}

async function postPush(body: unknown, headers: Record<string, string> = {}): Promise<Response> {
  return fetch(url('/internal/push'), {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', ...headers },
    body: typeof body === 'string' ? body : JSON.stringify(body),
  });
}

describe('Relay HTTP — /internal/push happy paths', () => {
  it('telegram-only: 200 + single sent receipt', async () => {
    const res = await postPush({
      channel: 'telegram',
      agentId: 'pulse',
      to: '12345',
      text: 'hello tg',
      source: 'unit-test',
    });
    expect(res.status).toBe(200);
    const body = (await res.json()) as { ok: boolean; receipts: Array<{ channel: string; status: string; externalMessageId?: string; attempts: number }> };
    expect(body.ok).toBe(true);
    expect(body.receipts).toHaveLength(1);
    expect(body.receipts[0].channel).toBe('telegram');
    expect(body.receipts[0].status).toBe('sent');
    expect(body.receipts[0].externalMessageId).toBe('999');
    expect(body.receipts[0].attempts).toBe(1);
    expect(tgSendMessage).toHaveBeenCalledWith(12345, 'hello tg');
  });

  it('feishu-only: 200 + single sent receipt', async () => {
    const res = await postPush({
      channel: 'feishu',
      agentId: 'pulse',
      to: 'ou_abc',
      text: 'hello feishu',
    });
    expect(res.status).toBe(200);
    const body = (await res.json()) as { ok: boolean; receipts: Array<{ channel: string; status: string; externalMessageId?: string }> };
    expect(body.ok).toBe(true);
    expect(body.receipts).toHaveLength(1);
    expect(body.receipts[0].channel).toBe('feishu');
    expect(body.receipts[0].status).toBe('sent');
    expect(body.receipts[0].externalMessageId).toBe('om_xxx');
    expect(feishuMessageCreate).toHaveBeenCalledWith({
      params: { receive_id_type: 'open_id' },
      data: { receive_id: 'ou_abc', msg_type: 'text', content: JSON.stringify({ text: 'hello feishu' }) },
    });
  });

  it('both: 200 + two sent receipts (toTelegram + toFeishu)', async () => {
    const res = await postPush({
      channel: 'both',
      agentId: 'pulse',
      toTelegram: '12345',
      toFeishu: 'ou_abc',
      text: 'broadcast',
    });
    expect(res.status).toBe(200);
    const body = (await res.json()) as { ok: boolean; receipts: Array<{ channel: string; to: string; status: string }> };
    expect(body.ok).toBe(true);
    expect(body.receipts).toHaveLength(2);
    expect(body.receipts.every((r) => r.status === 'sent')).toBe(true);
    expect(new Set(body.receipts.map((r) => r.channel))).toEqual(new Set(['telegram', 'feishu']));
    const tg = body.receipts.find((r) => r.channel === 'telegram');
    const fs = body.receipts.find((r) => r.channel === 'feishu');
    expect(tg?.to).toBe('12345');
    expect(fs?.to).toBe('ou_abc');
    expect(tgSendMessage).toHaveBeenCalledWith(12345, 'broadcast');
    expect(feishuMessageCreate).toHaveBeenCalledWith({
      params: { receive_id_type: 'open_id' },
      data: { receive_id: 'ou_abc', msg_type: 'text', content: JSON.stringify({ text: 'broadcast' }) },
    });
  });

  it('both: 200 when only `to` provided — both legs fall back to the same address', async () => {
    // Per contract: format mismatch is the caller's problem; deliver layer
    // surfaces per-leg failures via receipts. With mocks both legs succeed.
    const res = await postPush({
      channel: 'both',
      agentId: 'pulse',
      to: 'shared-addr',
      text: 'fallback both',
    });
    expect(res.status).toBe(200);
    const body = (await res.json()) as { ok: boolean; receipts: Array<{ channel: string; to: string; status: string }> };
    expect(body.ok).toBe(true);
    expect(body.receipts).toHaveLength(2);
    expect(body.receipts.every((r) => r.to === 'shared-addr')).toBe(true);
    // TG fallback parses 'shared-addr' → NaN; deliver layer would normally fail,
    // but our tgSendMessage mock accepts anything. Verify it was at least invoked.
    expect(tgSendMessage).toHaveBeenCalledTimes(1);
    expect(feishuMessageCreate).toHaveBeenCalledTimes(1);
  });

  it('feishu: toFeishu overrides `to`', async () => {
    const res = await postPush({
      channel: 'feishu',
      agentId: 'pulse',
      to: 'ou_fallback',
      toFeishu: 'ou_explicit',
      text: 'override fs',
    });
    expect(res.status).toBe(200);
    const body = (await res.json()) as { ok: boolean; receipts: Array<{ channel: string; to: string }> };
    expect(body.ok).toBe(true);
    expect(body.receipts[0].to).toBe('ou_explicit');
    expect(feishuMessageCreate).toHaveBeenCalledWith({
      params: { receive_id_type: 'open_id' },
      data: { receive_id: 'ou_explicit', msg_type: 'text', content: JSON.stringify({ text: 'override fs' }) },
    });
  });

  it('both with one failure: 200 + ok=false + mixed receipts', async () => {
    tgSendMessage = vi.fn().mockRejectedValue(new Error('bad chat'));
    getTelegramBot = () => makeFakeBot();
    await handle.stop();
    handle = await startServer();

    const res = await postPush({
      channel: 'both',
      agentId: 'pulse',
      toTelegram: '999',
      toFeishu: 'ou_x',
      text: 'partial',
    });
    expect(res.status).toBe(200);
    const body = (await res.json()) as { ok: boolean; receipts: Array<{ channel: string; status: string; error?: string }> };
    expect(body.ok).toBe(false);
    expect(body.receipts).toHaveLength(2);
    const tg = body.receipts.find((r) => r.channel === 'telegram');
    const fs = body.receipts.find((r) => r.channel === 'feishu');
    expect(tg?.status).toBe('failed');
    expect(tg?.error).toContain('bad chat');
    expect(fs?.status).toBe('sent');
  });

  it('toTelegram overrides `to` for telegram channel', async () => {
    const res = await postPush({
      channel: 'telegram',
      agentId: 'pulse',
      to: 'fallback',
      toTelegram: '777',
      text: 'override',
    });
    expect(res.status).toBe(200);
    const body = (await res.json()) as { ok: boolean; receipts: Array<{ to: string }> };
    expect(body.receipts[0].to).toBe('777');
    expect(tgSendMessage).toHaveBeenCalledWith(777, 'override');
  });
});

describe('Relay HTTP — run terminal contract', () => {
  it('records a sent terminal receipt and deduplicates a retry for the same run', async () => {
    const run = terminalRegistry.startRun('pulse-cron', 'pulse');
    const body = {
      channel: 'feishu',
      agentId: 'pulse',
      toFeishu: 'ou_abc',
      text: 'terminal failure',
      runId: run.runId,
      terminal: true,
    };

    const first = await postPush(body);
    expect(first.status).toBe(200);
    expect(await first.json()).toMatchObject({ ok: true, runId: run.runId, terminal: true });
    expect(terminalRegistry.get(run.runId)).toMatchObject({
      status: 'sent',
      receipts: [{ channel: 'feishu', status: 'sent', externalMessageId: 'om_xxx' }],
    });

    const retry = await postPush(body);
    expect(retry.status).toBe(200);
    expect(await retry.json()).toMatchObject({ ok: true, deduplicated: true, terminalStatus: 'sent' });
    expect(feishuMessageCreate).toHaveBeenCalledTimes(1);
  });

  it('reserves before delivery so a concurrent retry cannot send a second message', async () => {
    let resolveSend!: (value: { data: { message_id: string } }) => void;
    feishuMessageCreate = vi.fn().mockImplementation(() => new Promise(resolve => { resolveSend = resolve; }));
    getFeishuClient = () => makeFakeFeishuClient();
    await handle.stop();
    handle = await startServer();

    const run = terminalRegistry.startRun('pulse-cron', 'pulse');
    const body = {
      channel: 'feishu', agentId: 'pulse', toFeishu: 'ou_abc', text: 'terminal', runId: run.runId, terminal: true,
    };
    const firstPromise = postPush(body);
    for (let attempt = 0; attempt < 50 && feishuMessageCreate.mock.calls.length === 0; attempt++) {
      await new Promise(resolve => setTimeout(resolve, 2));
    }
    const retry = await postPush(body);
    expect(await retry.json()).toMatchObject({ ok: true, deduplicated: true, terminalStatus: 'sending' });
    expect(feishuMessageCreate).toHaveBeenCalledTimes(1);

    resolveSend({ data: { message_id: 'om_concurrent' } });
    expect((await firstPromise).status).toBe(200);
  });

  it('fails closed when terminal run id is unknown or belongs to another owner', async () => {
    const unknown = await postPush({
      channel: 'feishu', agentId: 'pulse', toFeishu: 'ou_abc', text: 'terminal', runId: 'missing', terminal: true,
    });
    expect(unknown.status).toBe(409);
    expect(await unknown.json()).toMatchObject({ error: 'terminal_run_unavailable', reason: 'not_found' });
    expect(feishuMessageCreate).not.toHaveBeenCalled();

    const run = terminalRegistry.startRun('amanda-cron', 'amanda');
    const mismatch = await postPush({
      channel: 'feishu', agentId: 'pulse', toFeishu: 'ou_abc', text: 'terminal', runId: run.runId, terminal: true,
    });
    expect(mismatch.status).toBe(409);
    expect(await mismatch.json()).toMatchObject({ reason: 'owner_mismatch' });
    expect(feishuMessageCreate).not.toHaveBeenCalled();
  });
});

describe('Relay HTTP — /internal/push validation', () => {
  it('rejects Harness owner delivery when only label/severity prose claims urgency', async () => {
    const res = await postPush({
      channel: 'feishu',
      agentId: 'pulse',
      to: 'ou_abc',
      text: 'red_line_flag critical 要你出手',
      severity: 'high',
      source: 'harness-escalation',
    });
    expect(res.status).toBe(409);
    expect(await res.json()).toMatchObject({
      ok: false,
      error: 'human_attention_receipt_required',
    });
    expect(feishuMessageCreate).not.toHaveBeenCalled();
  });

  it('accepts Harness owner delivery with a reviewed routing receipt', async () => {
    const res = await postPush({
      channel: 'feishu',
      agentId: 'pulse',
      to: 'ou_abc',
      text: 'choose one option',
      source: 'harness-escalation',
      requiresHuman: true,
      humanDecisionKind: 'owner_decision',
      authorityFingerprint: 'a'.repeat(64),
    });
    expect(res.status).toBe(200);
    expect(await res.json()).toMatchObject({ ok: true });
    expect(feishuMessageCreate).toHaveBeenCalledTimes(1);
  });

  it('rejects an invented Harness decision kind', async () => {
    const res = await postPush({
      channel: 'feishu', agentId: 'pulse', to: 'ou_abc', text: 'x',
      source: 'harness-escalation', requiresHuman: true,
      humanDecisionKind: 'red_line_flag', authorityFingerprint: 'b'.repeat(64),
    });
    expect(res.status).toBe(409);
    expect(feishuMessageCreate).not.toHaveBeenCalled();
  });

  it('400 missing_fields when channel absent', async () => {
    const res = await postPush({ agentId: 'pulse', to: '1', text: 'x' });
    expect(res.status).toBe(400);
    expect(((await res.json()) as { error: string }).error).toBe('missing_fields');
  });

  it('400 missing_fields when agentId absent', async () => {
    const res = await postPush({ channel: 'telegram', to: '1', text: 'x' });
    expect(res.status).toBe(400);
    expect(((await res.json()) as { error: string }).error).toBe('missing_fields');
  });

  it('400 missing_to when telegram has no to/toTelegram', async () => {
    const res = await postPush({ channel: 'telegram', agentId: 'pulse', text: 'x' });
    expect(res.status).toBe(400);
    const body = (await res.json()) as { error: string; missing: string };
    expect(body.error).toBe('missing_to');
    expect(body.missing).toBe('toTelegram');
  });

  it('400 missing_to when feishu has no to/toFeishu', async () => {
    const res = await postPush({ channel: 'feishu', agentId: 'pulse', text: 'x' });
    expect(res.status).toBe(400);
    const body = (await res.json()) as { error: string; missing: string };
    expect(body.error).toBe('missing_to');
    expect(body.missing).toBe('toFeishu');
  });

  it('400 missing_to when both has only toTelegram (feishu leg has nothing)', async () => {
    const res = await postPush({ channel: 'both', agentId: 'pulse', toTelegram: '1', text: 'x' });
    expect(res.status).toBe(400);
    const body = (await res.json()) as { error: string; missing: string };
    expect(body.error).toBe('missing_to');
    expect(body.missing).toBe('toFeishu');
  });

  it('400 missing_to when both has only toFeishu (telegram leg has nothing)', async () => {
    const res = await postPush({ channel: 'both', agentId: 'pulse', toFeishu: 'ou_x', text: 'x' });
    expect(res.status).toBe(400);
    const body = (await res.json()) as { error: string; missing: string };
    expect(body.error).toBe('missing_to');
    expect(body.missing).toBe('toTelegram');
  });

  it('400 missing_to with missing=both when both legs have no address at all', async () => {
    const res = await postPush({ channel: 'both', agentId: 'pulse', text: 'x' });
    expect(res.status).toBe(400);
    const body = (await res.json()) as { error: string; missing: string };
    expect(body.error).toBe('missing_to');
    expect(body.missing).toBe('both');
  });

  it('400 empty_text when text is whitespace', async () => {
    const res = await postPush({ channel: 'telegram', agentId: 'pulse', to: '1', text: '   ' });
    expect(res.status).toBe(400);
    expect(((await res.json()) as { error: string }).error).toBe('empty_text');
  });

  it('400 empty_text when text missing', async () => {
    const res = await postPush({ channel: 'telegram', agentId: 'pulse', to: '1' });
    expect(res.status).toBe(400);
    expect(((await res.json()) as { error: string }).error).toBe('empty_text');
  });

  it('400 unknown_channel for bad channel value', async () => {
    const res = await postPush({ channel: 'sms', agentId: 'pulse', to: '1', text: 'x' });
    expect(res.status).toBe(400);
    expect(((await res.json()) as { error: string }).error).toBe('unknown_channel');
  });

  it('400 invalid_json for bad body', async () => {
    const res = await postPush('{not-json', {});
    expect(res.status).toBe(400);
    expect(((await res.json()) as { error: string }).error).toBe('invalid_json');
  });

  it('413 payload_too_large for oversized body', async () => {
    const giant = 'x'.repeat(20 * 1024); // > 16 KB BODY_LIMIT
    const res = await postPush({ channel: 'telegram', agentId: 'pulse', to: '1', text: giant });
    expect(res.status).toBe(413);
    expect(((await res.json()) as { error: string }).error).toBe('payload_too_large');
  });
});

describe('Relay HTTP — /internal/push agent resolution', () => {
  it('404 agent_not_found for unknown TG agent', async () => {
    const res = await postPush({ channel: 'telegram', agentId: 'ghost', to: '1', text: 'x' });
    expect(res.status).toBe(404);
    const body = (await res.json()) as { error: string; missing: string };
    expect(body.error).toBe('agent_not_found');
    expect(body.missing).toBe('telegram_bot');
  });

  it('404 agent_not_found for unknown Feishu agent', async () => {
    const res = await postPush({ channel: 'feishu', agentId: 'ghost', to: 'ou_x', text: 'x' });
    expect(res.status).toBe(404);
    const body = (await res.json()) as { error: string; missing: string };
    expect(body.error).toBe('agent_not_found');
    expect(body.missing).toBe('feishu_client');
  });

  it('404 when no resolvers configured at all', async () => {
    getTelegramBot = undefined;
    getFeishuClient = undefined;
    await handle.stop();
    handle = await startServer();

    const res = await postPush({ channel: 'telegram', agentId: 'pulse', to: '1', text: 'x' });
    expect(res.status).toBe(404);
    expect(((await res.json()) as { error: string }).error).toBe('agent_not_found');
  });
});

describe('Relay HTTP — /internal/push token auth', () => {
  beforeEach(async () => {
    process.env['RELAY_INTERNAL_TOKEN'] = 'secret-abc';
    await handle.stop();
    handle = await startServer();
  });

  afterEach(() => {
    delete process.env['RELAY_INTERNAL_TOKEN'];
  });

  it('401 when X-Relay-Token header missing', async () => {
    const res = await postPush({ channel: 'telegram', agentId: 'pulse', to: '1', text: 'x' });
    expect(res.status).toBe(401);
    expect(((await res.json()) as { error: string }).error).toBe('unauthorized');
  });

  it('401 when X-Relay-Token header wrong', async () => {
    const res = await postPush(
      { channel: 'telegram', agentId: 'pulse', to: '1', text: 'x' },
      { 'X-Relay-Token': 'wrong' },
    );
    expect(res.status).toBe(401);
    expect(((await res.json()) as { error: string }).error).toBe('unauthorized');
  });

  it('200 when X-Relay-Token header matches', async () => {
    const res = await postPush(
      { channel: 'telegram', agentId: 'pulse', to: '1', text: 'x' },
      { 'X-Relay-Token': 'secret-abc' },
    );
    expect(res.status).toBe(200);
    expect(((await res.json()) as { ok: boolean }).ok).toBe(true);
  });
});

describe('Relay HTTP — /internal/push without token configured', () => {
  it('200 with no header when RELAY_INTERNAL_TOKEN unset (dev mode)', async () => {
    // beforeEach already ensures env is unset
    const res = await postPush({ channel: 'telegram', agentId: 'pulse', to: '1', text: 'x' });
    expect(res.status).toBe(200);
    expect(((await res.json()) as { ok: boolean }).ok).toBe(true);
  });
});

describe('Relay HTTP — /internal/push feishu attachments', () => {
  function writeTmpFile(name: string, bytes: Buffer | string): string {
    const filePath = join(testDir, name);
    writeFileSync(filePath, bytes);
    return filePath;
  }

  it('feishu image: 200 + uploads image_key + sends image message', async () => {
    const imgPath = writeTmpFile('shot.png', Buffer.from([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]));
    const res = await postPush({ channel: 'feishu', agentId: 'pulse', toFeishu: 'ou_abc', path: imgPath });
    expect(res.status).toBe(200);
    const body = (await res.json()) as { ok: boolean; receipts: Array<{ channel: string; status: string }> };
    expect(body.ok).toBe(true);
    expect(body.receipts).toHaveLength(1);
    expect(body.receipts[0]).toMatchObject({ channel: 'feishu', status: 'sent' });
    expect(feishuImageCreate).toHaveBeenCalledTimes(1);
    expect(feishuFileCreate).not.toHaveBeenCalled();
    expect(feishuMessageCreate).toHaveBeenCalledWith({
      params: { receive_id_type: 'open_id' },
      data: { receive_id: 'ou_abc', msg_type: 'image', content: JSON.stringify({ image_key: 'img_key_abc' }) },
    });
  });

  it('feishu file (non-image extension): 200 + uploads file_key + sends file message', async () => {
    const filePath = writeTmpFile('report.pdf', 'fake pdf content');
    const res = await postPush({ channel: 'feishu', agentId: 'pulse', toFeishu: 'ou_abc', path: filePath });
    expect(res.status).toBe(200);
    const body = (await res.json()) as { ok: boolean; receipts: Array<{ channel: string; status: string }> };
    expect(body.ok).toBe(true);
    expect(body.receipts).toHaveLength(1);
    expect(feishuFileCreate).toHaveBeenCalledTimes(1);
    expect(feishuImageCreate).not.toHaveBeenCalled();
    expect(feishuMessageCreate).toHaveBeenCalledWith({
      params: { receive_id_type: 'open_id' },
      data: { receive_id: 'ou_abc', msg_type: 'file', content: JSON.stringify({ file_key: 'file_key_abc' }) },
    });
  });

  it('feishu image + text caption: 200 + two receipts (image then text)', async () => {
    const imgPath = writeTmpFile('shot.jpg', Buffer.from([0xff, 0xd8, 0xff, 0xe0]));
    const res = await postPush({ channel: 'feishu', agentId: 'pulse', toFeishu: 'ou_abc', path: imgPath, text: 'see this' });
    expect(res.status).toBe(200);
    const body = (await res.json()) as { ok: boolean; receipts: Array<{ channel: string; status: string }> };
    expect(body.ok).toBe(true);
    expect(body.receipts).toHaveLength(2);
    expect(feishuImageCreate).toHaveBeenCalledTimes(1);
    expect(feishuMessageCreate).toHaveBeenCalledTimes(2);
    const calls = feishuMessageCreate.mock.calls.map((c) => (c[0] as { data: { msg_type: string } }).data.msg_type);
    expect(calls).toEqual(expect.arrayContaining(['image', 'text']));
  });

  it('400 attachment_unsupported_channel when channel=telegram with path', async () => {
    const imgPath = writeTmpFile('shot.png', Buffer.from([1, 2, 3]));
    const res = await postPush({ channel: 'telegram', agentId: 'pulse', to: '1', path: imgPath });
    expect(res.status).toBe(400);
    expect(((await res.json()) as { error: string }).error).toBe('attachment_unsupported_channel');
  });

  it('400 attachment_unsupported_channel when channel=both with path', async () => {
    const imgPath = writeTmpFile('shot.png', Buffer.from([1, 2, 3]));
    const res = await postPush({ channel: 'both', agentId: 'pulse', to: 'x', path: imgPath, text: 't' });
    expect(res.status).toBe(400);
    expect(((await res.json()) as { error: string }).error).toBe('attachment_unsupported_channel');
  });

  it('400 path_not_absolute when relative path', async () => {
    const res = await postPush({ channel: 'feishu', agentId: 'pulse', toFeishu: 'ou_abc', path: 'relative/x.png' });
    expect(res.status).toBe(400);
    expect(((await res.json()) as { error: string }).error).toBe('path_not_absolute');
  });

  it('400 invalid_path when file missing', async () => {
    const res = await postPush({ channel: 'feishu', agentId: 'pulse', toFeishu: 'ou_abc', path: '/tmp/__definitely_not_exist__.png' });
    expect(res.status).toBe(400);
    expect(((await res.json()) as { error: string }).error).toBe('invalid_path');
  });

  it('400 empty_text when neither text nor path provided', async () => {
    const res = await postPush({ channel: 'feishu', agentId: 'pulse', toFeishu: 'ou_abc' });
    expect(res.status).toBe(400);
    expect(((await res.json()) as { error: string }).error).toBe('empty_text');
  });

  it('feishu attachment with upload returning no image_key: failed receipt', async () => {
    feishuImageCreate.mockImplementationOnce((arg: { data: { image: ReadStream } }) => (
      consumeUploadStream(arg.data.image, {})
    ));
    const imgPath = writeTmpFile('shot.png', Buffer.from([1, 2, 3, 4]));
    const res = await postPush({ channel: 'feishu', agentId: 'pulse', toFeishu: 'ou_abc', path: imgPath });
    expect(res.status).toBe(200);
    const body = (await res.json()) as { ok: boolean; receipts: Array<{ status: string; error?: string }> };
    expect(body.ok).toBe(false);
    expect(body.receipts[0].status).toBe('failed');
    expect(feishuMessageCreate).not.toHaveBeenCalled();
  });
});

describe('Relay HTTP — durable notice single-attempt boundary', () => {
  const makeNotice = (changes: Record<string, unknown> = {}) => {
    const text = 'Synthetic WI018 notice';
    return {
      channel: 'telegram', agentId: 'pulse', to: '12345', text,
      source: 'harness-operational',
      notice: {
        contract: 'relay-notice-single-attempt/v1', requestKey: 'a'.repeat(64),
        routeDigest: 'b'.repeat(64), textSha256: createHash('sha256').update(text).digest('hex'),
      },
      ...changes,
    };
  };
  const postNotice = (body: unknown, headers: Record<string, string> = {}) => fetch(url('/internal/notice'), {
    method: 'POST', headers: { 'Content-Type': 'application/json', ...headers }, body: JSON.stringify(body),
  });

  it('returns the original binding and actual message ID with one send', async () => {
    const input = makeNotice();
    const body = await (await postNotice(input)).json();
    expect(body).toMatchObject({ ok: true, notice: input.notice, sendAttempted: true });
    expect(body.receipts).toEqual([{ channel: 'telegram', to: '12345', status: 'sent', attempts: 1, externalMessageId: '999' }]);
    expect(tgSendMessage).toHaveBeenCalledTimes(1);
  });

  it.each(['fetch failed', 'ECONNRESET', '429 flood retry_after 1'])(
    'does not retry an ambiguous or throttled channel result: %s', async (message) => {
      tgSendMessage.mockRejectedValue(new Error(message + ' private-url'));
      const body = await (await postNotice(makeNotice())).json();
      expect(body.sendAttempted).toBe(true);
      expect(body.receipts[0]).toMatchObject({ status: 'failed', attempts: 1, error: 'channel_result_unknown' });
      expect(tgSendMessage).toHaveBeenCalledTimes(1);
      expect(JSON.stringify(body)).not.toContain('private-url');
    },
  );

  it('retains auth and owner-attention gating before any send', async () => {
    process.env['RELAY_INTERNAL_TOKEN'] = 'synthetic-notice-token';
    expect((await postNotice(makeNotice())).status).toBe(401);
    const input = makeNotice({ source: 'harness-escalation' });
    const refused = await (await postNotice(input, { 'x-relay-token': 'synthetic-notice-token' })).json();
    expect(refused).toMatchObject({ sendAttempted: false, notice: input.notice, error: 'human_attention_receipt_required' });
    expect(tgSendMessage).not.toHaveBeenCalled();
    const passed = await (await postNotice(makeNotice({
      source: 'harness-escalation', requiresHuman: true,
      humanDecisionKind: 'owner_decision', authorityFingerprint: 'c'.repeat(64),
    }), { 'x-relay-token': 'synthetic-notice-token' })).json();
    expect(passed.ok).toBe(true);
  });

  it.each([
    { channel: 'both' }, { path: '/tmp/file' }, { runId: 'made-up', terminal: true },
    { source: 'another-label' }, { toTelegram: '12345' }, { to: 'not-a-chat' },
    { requiresHuman: true }, { notice: {} }, { text: 'changed after hashing' },
    { text: '中'.repeat(2000) }, { agentId: '../other' },
  ])('rejects unsupported route or payload without a channel call: %j', async (changes) => {
    const result = await postNotice(makeNotice(changes));
    expect(result.status).toBe(400);
    expect((await result.json()).sendAttempted).toBe(false);
    expect(tgSendMessage).not.toHaveBeenCalled();
    expect(feishuMessageCreate).not.toHaveBeenCalled();
  });

  it('reports a matching pre-send missing-agent rejection without claiming success', async () => {
    const input = makeNotice({ agentId: 'missing' });
    const body = await (await postNotice(input)).json();
    expect(body).toMatchObject({ ok: false, notice: input.notice, sendAttempted: false, error: 'agent_not_found' });
    expect(tgSendMessage).not.toHaveBeenCalled();
  });

  it('Feishu uses one real message receipt and one invocation on failure', async () => {
    const input = makeNotice({ channel: 'feishu', to: 'ou_fixture' });
    const body = await (await postNotice(input)).json();
    expect(body.receipts[0]).toMatchObject({ externalMessageId: 'om_xxx', attempts: 1 });
    feishuMessageCreate.mockRejectedValueOnce(new Error('socket hang up private-error'));
    const failed = await (await postNotice(input)).json();
    expect(failed.sendAttempted).toBe(true);
    expect(failed.receipts[0]).toMatchObject({ status: 'failed', attempts: 1 });
    expect(feishuMessageCreate).toHaveBeenCalledTimes(2);
    expect(JSON.stringify(failed)).not.toContain('private-error');
  });


  it('notice-only experiment pins destinations and exposes no legacy endpoints', async () => {
    process.env['RELAY_HTTP_HOST'] = '0.0.0.0';
    const isolated = await startNoticeOnlyServer({
      getTelegramBot: id => id === 'pulse' ? makeFakeBot() : null,
    }, [{ agentId: 'pulse', channel: 'telegram', to: '12345' }]);
    try {
      expect(isolated.server.address()).toMatchObject({ address: '127.0.0.1' });
      expect(tgSendMessage).not.toHaveBeenCalled();
      const base = `http://127.0.0.1:${isolated.port}`;
      const post = (path: string, body: unknown) => fetch(base + path, {
        method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body),
      });
      expect((await post('/internal/push', makeNotice())).status).toBe(404);
      expect((await post('/internal/agents/reload', {})).status).toBe(404);
      const denied = await post('/internal/notice', makeNotice({ to: '54321' }));
      expect(denied.status).toBe(403);
      expect(await denied.json()).toMatchObject({
        sendAttempted: false, error: 'notice_route_not_admitted',
      });
      expect(tgSendMessage).not.toHaveBeenCalled();
      const sent = await post('/internal/notice', makeNotice());
      expect((await sent.json()).receipts[0].externalMessageId).toBe('999');
      expect(tgSendMessage).toHaveBeenCalledTimes(1);
    } finally {
      await isolated.stop();
      delete process.env['RELAY_HTTP_HOST'];
    }
  });

  it('admits bounded text even when JSON escaping expands its wire size', async () => {
    const text = '\u0001'.repeat(4096);
    const input = makeNotice({
      text, notice: { contract: 'relay-notice-single-attempt/v1', requestKey: 'a'.repeat(64),
        routeDigest: 'b'.repeat(64), textSha256: createHash('sha256').update(text).digest('hex') },
    });
    const response = await postNotice(input);
    expect(response.status).toBe(200);
    expect((await response.json()).ok).toBe(true);
    expect(tgSendMessage).toHaveBeenCalledWith(12345, text);
  });

  it('the installed grammy client makes one fetch on ambiguous failure', async () => {
    const channelFetch = vi.fn().mockRejectedValue(new Error('fetch failed'));
    const bot = new RealBot('12345:fixture', { client: { fetch: channelFetch } });
    getTelegramBot = () => bot;
    await handle.stop();
    handle = await startServer();
    const body = await (await postNotice(makeNotice())).json();
    expect(body.receipts[0]).toMatchObject({ status: 'failed', attempts: 1 });
    expect(channelFetch).toHaveBeenCalledTimes(1);
  });
});
