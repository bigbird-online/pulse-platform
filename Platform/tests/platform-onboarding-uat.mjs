#!/usr/bin/env node
import { existsSync } from 'node:fs';
import { mkdtemp, readFile, rm, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';

const scriptDir = dirname(fileURLToPath(import.meta.url));
const repoRoot = resolve(scriptDir, '../..');

function moduleUrl(path) {
  return pathToFileURL(join(repoRoot, path)).href;
}

function assert(condition, message, details) {
  if (!condition) {
    const err = new Error(message);
    err.details = details;
    throw err;
  }
}

async function readJson(path) {
  return JSON.parse(await readFile(path, 'utf8'));
}

async function requestJson(baseUrl, path, opts = {}) {
  const res = await fetch(`${baseUrl}${path}`, {
    ...opts,
    headers: {
      ...(opts.body ? { 'Content-Type': 'application/json' } : {}),
      ...(opts.headers ?? {}),
    },
  });
  const text = await res.text();
  let body;
  try {
    body = text ? JSON.parse(text) : {};
  } catch {
    body = { raw: text };
  }
  return { status: res.status, body };
}

function listen(server) {
  return new Promise((resolveListen, reject) => {
    server.once('error', reject);
    server.listen(0, '127.0.0.1', () => {
      const addr = server.address();
      assert(addr && typeof addr === 'object', 'server did not expose a TCP address');
      resolveListen(`http://127.0.0.1:${addr.port}`);
    });
  });
}

function closeServer(server) {
  return new Promise((resolveClose, reject) => {
    server.close((err) => (err ? reject(err) : resolveClose()));
  });
}

function noopPool() {
  return {
    spawn: async () => ({}),
    send: async () => undefined,
    kill: () => undefined,
    killAll: () => undefined,
    get: () => undefined,
    getAll: () => [],
    getStatus: () => [],
    on: () => undefined,
    emitShutdown: () => undefined,
    dispose: () => undefined,
  };
}

const requiredBuildFiles = [
  'Platform/dist/http-server.js',
  'Platform/dist/phone-allowlist.js',
  'Platform/dist/tenant-id-allocator.js',
  'Relay/dist/http-server.js',
  'Relay/dist/channels/feishu.js',
];

for (const file of requiredBuildFiles) {
  assert(existsSync(join(repoRoot, file)), `missing build artifact: ${file}`, {
    hint: 'Run Platform and Relay builds before UAT.',
  });
}

const [
  platformHttp,
  platformAllowlist,
  platformTenantId,
  relayHttp,
  relayFeishu,
] = await Promise.all([
  import(moduleUrl('Platform/dist/http-server.js')),
  import(moduleUrl('Platform/dist/phone-allowlist.js')),
  import(moduleUrl('Platform/dist/tenant-id-allocator.js')),
  import(moduleUrl('Relay/dist/http-server.js')),
  import(moduleUrl('Relay/dist/channels/feishu.js')),
]);

const { createHttpServer } = platformHttp;
const { PhoneAllowlist } = platformAllowlist;
const { TenantIdAllocator } = platformTenantId;
const { startRelayHttpServer } = relayHttp;
const { handleFeishuMessage } = relayFeishu;

const originalEnv = { ...process.env };
const tmpRoot = await mkdtemp(join(tmpdir(), 'pulse-platform-uat-'));
const relayConfigPath = join(tmpRoot, 'relay.json');
const tenantsRoot = join(tmpRoot, 'Tenants');
const evidence = {
  tmpRoot,
  relayConfigPath,
  tenantsRoot,
  checks: [],
};

let relayHandle;
let platformServer;

function pass(id, data = {}) {
  evidence.checks.push({ id, ok: true, ...data });
}

try {
  process.env = {
    ...process.env,
    NODE_ENV: 'test',
    PLATFORM_ALLOW_MOCK_PAY: '1',
    RELAY_INTERNAL_TOKEN: 'platform-uat-secret',
  };

  await writeFile(relayConfigPath, '{\n  "users": [],\n  "bindings": []\n}\n', 'utf8');

  let reloadCount = 0;
  let runningAgentIds = [];
  const upsertedBindings = [];
  const fakeFeishuBots = {
    start: async () => undefined,
    stop: () => undefined,
    reload: async () => {
      reloadCount += 1;
      const doc = await readJson(relayConfigPath);
      runningAgentIds = (doc.users ?? []).flatMap((u) =>
        (u.agents ?? []).filter((a) => a.feishu).map((a) => a.id),
      );
      return { added: runningAgentIds, total: runningAgentIds.length };
    },
    runningAgentIds: () => runningAgentIds,
  };
  const approvalRouter = {
    upsertGatewayBinding: (binding) => {
      upsertedBindings.push(binding);
      return true;
    },
  };

  relayHandle = await startRelayHttpServer(
    {
      feishuBots: fakeFeishuBots,
      router: approvalRouter,
      configPath: relayConfigPath,
      startedAtMs: Date.now() - 1000,
    },
    0,
  );
  const relayUrl = `http://127.0.0.1:${relayHandle.port}`;

  platformServer = createHttpServer({
    allowlist: new PhoneAllowlist(new Set(['+8613800138000'])),
    relayConfigPath,
    tenantsRoot,
    tenantIdAllocator: new TenantIdAllocator(0),
    relayHttpUrl: relayUrl,
    verifyFeishuCredentials: async () => ({
      ok: true,
      tenantAccessToken: 'uat-token',
      expire: 7200,
    }),
  });
  const platformUrl = await listen(platformServer);

  const platformHealth = await requestJson(platformUrl, '/health');
  assert(platformHealth.status === 200 && platformHealth.body.status === 'ok', 'Platform health failed', platformHealth);
  pass('platform_health', platformHealth.body);

  const capabilities = await requestJson(platformUrl, '/internal/capabilities');
  assert(
    capabilities.status === 200 &&
      capabilities.body.mockPay === true &&
      capabilities.body.provisionConfigured === true &&
      capabilities.body.pairingConfigured === true,
    'Platform capabilities are not UAT-ready',
    capabilities,
  );
  pass('platform_capabilities', capabilities.body);

  const relayHealth = await requestJson(relayUrl, '/internal/health', {
    headers: { 'x-relay-token': 'platform-uat-secret' },
  });
  assert(relayHealth.status === 200 && relayHealth.body.status === 'ok', 'Relay health failed', relayHealth);
  pass('relay_health', relayHealth.body);

  const rejectedOrder = await requestJson(platformUrl, '/internal/orders', {
    method: 'POST',
    body: JSON.stringify({ phone: '13900139000', displayName: 'Blocked' }),
  });
  assert(rejectedOrder.status === 403 && rejectedOrder.body.error === 'not_allowlisted', 'non-allowlisted phone was not rejected', rejectedOrder);
  pass('allowlist_rejection', { status: rejectedOrder.status, error: rejectedOrder.body.error });

  const order = await requestJson(platformUrl, '/internal/orders', {
    method: 'POST',
    body: JSON.stringify({ phone: '13800138000', displayName: 'Eric' }),
  });
  assert(order.status === 200 && order.body.status === 'pending' && order.body.orderId, 'order creation failed', order);
  const orderId = order.body.orderId;
  pass('step1_order_pending', { orderId, displayName: order.body.displayName });

  const unpaidProvision = await requestJson(platformUrl, '/internal/provision', {
    method: 'POST',
    body: JSON.stringify({ orderId, feishuAppId: 'cli_uat', feishuAppSecret: 'secret_uat' }),
  });
  assert(unpaidProvision.status === 409 && unpaidProvision.body.error === 'not_paid', 'unpaid provision was not blocked', unpaidProvision);
  pass('step2_unpaid_blocked', { status: unpaidProvision.status, error: unpaidProvision.body.error });

  const paid = await requestJson(platformUrl, '/internal/mock-pay', {
    method: 'POST',
    body: JSON.stringify({ orderId }),
  });
  assert(paid.status === 200 && paid.body.status === 'paid', 'mock payment failed', paid);
  pass('mock_pay_paid', { orderId, already: paid.body.already });

  const provision = await requestJson(platformUrl, '/internal/provision', {
    method: 'POST',
    body: JSON.stringify({ orderId, feishuAppId: 'cli_uat', feishuAppSecret: 'secret_uat' }),
  });
  assert(provision.status === 200 && provision.body.ok === true, 'paid provision failed', provision);
  assert(reloadCount === 1, 'Relay reload was not triggered exactly once', { reloadCount });
  assert(existsSync(join(tenantsRoot, provision.body.tenantId)), 'tenant workspace was not created', provision.body);

  const afterProvision = await readJson(relayConfigPath);
  const pendingBindings = (afterProvision.bindings ?? []).filter((b) => b.status === 'pending');
  assert(afterProvision.users?.length === 1, 'provision did not create one user', afterProvision);
  assert(pendingBindings.length === 1 && pendingBindings[0].address.value === '', 'provision did not create one empty pending binding', afterProvision);
  pass('step2_provision_pending_binding', {
    tenantId: provision.body.tenantId,
    agentId: provision.body.agentId,
    reloadCount,
    pendingBindings: pendingBindings.length,
  });

  const sent = [];
  const pairingRouter = {
    resolveByFeishuAgent: () => undefined,
  };
  await handleFeishuMessage(
    {
      agent: afterProvision.users[0].agents[0],
      pool: noopPool(),
      router: pairingRouter,
      poolKeyToOpenId: new Map(),
      configPath: relayConfigPath,
      sendTextMessage: async (openId, text) => {
        sent.push({ openId, text });
      },
      sendCardMessage: async () => undefined,
      downloadImage: async () => ({ base64: '', mediaType: 'image/png' }),
      downloadFileResource: async () => ({ buffer: Buffer.from('') }),
      transcribeInboundAudio: async () => '',
    },
    {
      message: {
        message_type: 'text',
        message_id: 'uat-pairing-message',
        content: JSON.stringify({ text: 'hello' }),
      },
      sender: { sender_id: { open_id: 'ou_uat_open_id_123456789' } },
    },
  );
  const pairingText = sent[0]?.text ?? '';
  const code = /\/approve\s+([A-Z2-9]{6})/.exec(pairingText)?.[1];
  assert(code, 'Relay did not issue a pairing code for an unbound Feishu open_id', { sent });
  pass('relay_pairing_code_issued', { codeMasked: `${code.slice(0, 2)}****` });

  const pair = await requestJson(platformUrl, '/internal/pair', {
    method: 'POST',
    body: JSON.stringify({ orderId, code }),
  });
  assert(pair.status === 200 && pair.body.ok === true, 'Platform Step 3 pairing failed', pair);
  assert(pair.body.openId === 'ou_uat_open_id_123456789', 'Step 3 returned the wrong openId', pair);

  const afterPair = await readJson(relayConfigPath);
  const activeBindings = (afterPair.bindings ?? []).filter((b) => b.status === 'active');
  const remainingPending = (afterPair.bindings ?? []).filter((b) => b.status === 'pending');
  assert(activeBindings.length === 1, 'pairing did not create one active binding', afterPair.bindings);
  assert(activeBindings[0].address.value === 'ou_uat_open_id_123456789', 'active binding did not store the paired openId', activeBindings[0]);
  assert(upsertedBindings.length === 1, 'Relay router was not refreshed with the active binding', { upsertedBindings });
  pass('step3_pairing_active_binding', {
    orderId,
    activeBindings: activeBindings.length,
    remainingPendingBindings: remainingPending.length,
    openIdMasked: 'ou_uat_o...',
  });

  const reloadRetry = await requestJson(platformUrl, '/internal/relay/reload', { method: 'POST' });
  assert(reloadRetry.status === 200 && reloadRetry.body.ok === true, 'Platform reload retry endpoint failed', reloadRetry);
  pass('platform_reload_retry', { total: reloadRetry.body.total, added: reloadRetry.body.added });

  console.log(JSON.stringify({
    ok: true,
    uat: 'platform-onboarding-local-loopback',
    relayUrl,
    platformUrl,
    evidence,
  }, null, 2));
} catch (err) {
  console.error(JSON.stringify({
    ok: false,
    uat: 'platform-onboarding-local-loopback',
    error: err instanceof Error ? err.message : String(err),
    details: err?.details,
    evidence,
  }, null, 2));
  process.exitCode = 1;
} finally {
  if (platformServer) await closeServer(platformServer).catch(() => undefined);
  if (relayHandle) await relayHandle.stop().catch(() => undefined);
  process.env = originalEnv;
  if (process.env['KEEP_PULSE_PLATFORM_UAT_TMP'] !== '1') {
    await rm(tmpRoot, { recursive: true, force: true }).catch(() => undefined);
  }
}
