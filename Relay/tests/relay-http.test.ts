import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { mkdtempSync, writeFileSync, readFileSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { createMaintenanceBarrier, type MaintenanceBarrier } from '../src/maintenance-barrier.js';
import { startRelayHttpServer, type RelayHttpServerHandle } from '../src/http-server.js';
import * as feishu from '../src/channels/feishu.js';
import type { Router } from '../src/router.js';
import type { ConversationRuntimeManager } from '../src/conversation-runtime.js';
import type { Scheduler } from '../src/types.js';

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
let reloadResult: { added: string[]; total: number };
let reloadShouldThrow: boolean;
let runningIds: string[];
let runtimeStatuses: Array<{ tenantId: string; state: string }>;
let schedulerRunningIds: string[];
let schedulerStop: ReturnType<typeof vi.fn>;
let schedulerReload: ReturnType<typeof vi.fn>;
let barrier: MaintenanceBarrier;
let sclIntentStates:Record<'recognized'|'attempting'|'retry_wait'|'result_ready',number>;

const fakeRouter = {
  upsertGatewayBinding: vi.fn(),
  reload: vi.fn(() => ({ users: 0, bindings: 0 })),
} as unknown as Router;

beforeEach(async () => {
  process.env['RELAY_INTERNAL_TOKEN'] = 'test-secret';
  testDir = mkdtempSync(join(tmpdir(), 'relay-http-test-'));
  configPath = join(testDir, 'relay.json');
  writeFileSync(configPath, JSON.stringify({ users: [], bindings: [] }, null, 2));
  reloadResult = { added: [], total: 0 };
  reloadShouldThrow = false;
  runningIds = [];
  runtimeStatuses = [];
  schedulerRunningIds = [];
  schedulerStop = vi.fn();
  schedulerReload = vi.fn();
  barrier = createMaintenanceBarrier();
  sclIntentStates={recognized:0,attempting:0,retry_wait:0,result_ready:0};

  const fakeFeishuBots = {
    start: async () => {},
    stop: () => {},
    reload: async () => {
      if (reloadShouldThrow) throw new Error('boom');
      return reloadResult;
    },
    runningAgentIds: () => runningIds,
  };

  handle = await startRelayHttpServer(
    {
      feishuBots: fakeFeishuBots,
      router: fakeRouter,
      configPath,
      startedAtMs: Date.now() - 5000,
      runtimeManager: {
        getStatus: () => runtimeStatuses,
      } as unknown as ConversationRuntimeManager,
      scheduler: {
        getRunningJobIds: () => schedulerRunningIds,
        stop: schedulerStop,
        reload: schedulerReload,
      } satisfies Pick<Scheduler, 'getRunningJobIds' | 'stop' | 'reload'>,
      maintenance: barrier,
      sclIntentStatus:()=>({
        available:true,busy:Object.values(sclIntentStates).reduce((sum,value)=>sum+value,0),
        restartBlocking:sclIntentStates.recognized+sclIntentStates.attempting+sclIntentStates.result_ready,
        states:{...sclIntentStates},
      }),
    },
    0,
  );
});

afterEach(async () => {
  await handle.stop();
  delete process.env['RELAY_INTERNAL_TOKEN'];
  rmSync(testDir, { recursive: true, force: true });
  vi.clearAllMocks();
});

function url(path: string): string {
  return `http://127.0.0.1:${handle.port}${path}`;
}

const authHeaders = { 'x-relay-token': 'test-secret' };

describe('Relay HTTP — retired Host recovery endpoint',()=>{
  it('no longer serves the legacy WI recovery route',async()=>{
    const response=await fetch(url('/internal/wi/recover'),{method:'POST',
      headers:{...authHeaders,'content-type':'application/json'},
      body:JSON.stringify({poolKey:'user:pulse:feishu',workItemId:'wi_20260922_184',humanInputRef:'a'.repeat(64)})});
    expect(response.status).toBe(404);
  });
});

describe('Relay HTTP — /internal/health', () => {
  it('returns ok with uptime + bot count', async () => {
    runningIds = ['agent-a', 'agent-b'];
    const res = await fetch(url('/internal/health'), { headers: authHeaders });
    expect(res.status).toBe(200);
    const body = (await res.json()) as { status: string; uptime: number; feishuBots: number };
    expect(body.status).toBe('ok');
    expect(body.feishuBots).toBe(2);
    expect(body.uptime).toBeGreaterThanOrEqual(0);
  });

  it('rejects a request without the internal token', async () => {
    const res = await fetch(url('/internal/health'));
    expect(res.status).toBe(401);
    expect(await res.json()).toEqual({ ok: false, error: 'unauthorized' });
  });
});

describe('Relay HTTP — /internal/cc-status', () => {
  it('affirms restart safety only when CC and scheduler are both idle', async () => {
    runtimeStatuses = [
      { tenantId: 'owner:pulse:feishu', state: 'idle' },
      { tenantId: 'aki:pulse:feishu', state: 'ready' },
    ];
    const res = await fetch(url('/internal/cc-status'), { headers: authHeaders });
    expect(res.status).toBe(200);
    const body = await res.json() as {
      safeToRestart: boolean;
      counts: Record<string, number>;
      scheduler: { available: boolean; busy: number; running: string[] };
      sclIntents:{available:boolean;busy:number;states:typeof sclIntentStates};
    };
    expect(body.counts.busy).toBe(0);
    expect(body.scheduler).toEqual({ available: true, busy: 0, running: [] });
    expect(body.sclIntents).toEqual({available:true,busy:0,restartBlocking:0,states:sclIntentStates});
    expect(body.safeToRestart).toBe(true);
  });

  it('reports a running Marsh job and makes the maintenance gate unsafe', async () => {
    schedulerRunningIds = ['marsh-vultr-dispatcher-20260619'];
    const res = await fetch(url('/internal/cc-status'), { headers: authHeaders });
    const body = await res.json() as {
      safeToRestart: boolean;
      scheduler: { available: boolean; busy: number; running: string[] };
    };
    expect(body.scheduler.busy).toBe(1);
    expect(body.scheduler.running).toEqual(['marsh-vultr-dispatcher-20260619']);
    expect(body.safeToRestart).toBe(false);
  });

  it('allows a durable retry wait to survive a maintenance restart',async()=>{
    sclIntentStates={recognized:0,attempting:0,retry_wait:1,result_ready:0};
    const res=await fetch(url('/internal/cc-status'),{headers:authHeaders});
    const body=await res.json() as {
      safeToRestart:boolean;sclIntents:{available:boolean;busy:number;restartBlocking:number;states:typeof sclIntentStates};
    };
    expect(body.sclIntents).toEqual({available:true,busy:1,restartBlocking:0,states:sclIntentStates});
    expect(body.safeToRestart).toBe(true);
  });

  it('keeps an attempting SCL intent restart-blocking',async()=>{
    sclIntentStates={recognized:0,attempting:1,retry_wait:0,result_ready:0};
    const res=await fetch(url('/internal/cc-status'),{headers:authHeaders});
    const body=await res.json() as {safeToRestart:boolean;sclIntents:{restartBlocking:number}};
    expect(body.sclIntents.restartBlocking).toBe(1);
    expect(body.safeToRestart).toBe(false);
  });

  it('makes the maintenance gate unsafe when a CC session is busy', async () => {
    runtimeStatuses = [{ tenantId: 'owner:pulse:feishu', state: 'busy' }];
    const res = await fetch(url('/internal/cc-status'), { headers: authHeaders });
    const body = await res.json() as { safeToRestart: boolean; busy: string[] };
    expect(body.busy).toEqual(['owner:pulse:feishu']);
    expect(body.safeToRestart).toBe(false);
  });

  it('holds with a token, reports the snapshot under the hold and refuses other holders', async () => {
    const post = (path: string, body?: unknown) => fetch(url(path), {
      method: 'POST', headers: { ...authHeaders, 'content-type': 'application/json' },
      body: JSON.stringify(body ?? {}) });
    expect((await fetch(url('/internal/maintenance/hold'), { method: 'POST', body: '{"ttlMs":1000}' })).status).toBe(401);
    expect((await post('/internal/maintenance/hold', {})).status).toBe(400);
    runtimeStatuses = [{ tenantId: 'owner:aki', state: 'busy', runtime: 'pi' } as never];
    const response = await post('/internal/maintenance/hold', { ttlMs: 60_000 });
    const held = await response.json() as { token: string; maintenance: { held: boolean };
      status: { busySessions: unknown[]; maintenance: { held: boolean } } };
    expect(response.status).toBe(200);
    expect(held.token).toMatch(/[0-9a-f-]{36}/);
    expect(held.maintenance.held).toBe(true);
    expect(held.status.busySessions).toEqual([{ tenantId: 'owner:aki', runtime: 'pi' }]);
    expect(held.status.maintenance.held).toBe(true);
    expect((await post('/internal/maintenance/hold', { ttlMs: 60_000 })).status).toBe(409);
    const queued = barrier.admit('owner:aki', 'pi', { type: 'human', content: 'secret text' });
    const status = await (await fetch(url('/internal/cc-status'), { headers: authHeaders })).json() as Record<string, unknown>;
    expect(status['maintenance']).toEqual({ held: true, expiresAt: expect.any(Number),
      queued: [{ tenantId: 'owner:aki', runtime: 'pi' }] });
    expect(JSON.stringify(status)).not.toContain('secret text');
    expect(JSON.stringify(status)).not.toContain(held.token);
    expect((await post('/internal/maintenance/release', { token: 'stale' })).status).toBe(409);
    expect(barrier.isHeld()).toBe(true);
    const released = await post('/internal/maintenance/release', { token: held.token });
    expect(released.status).toBe(200);
    await queued;
  });

  it('rejects a request without the internal token', async () => {
    const res = await fetch(url('/internal/cc-status'));
    expect(res.status).toBe(401);
    expect(await res.json()).toEqual({ ok: false, error: 'unauthorized' });
  });
});

describe('Relay HTTP — /internal/agents/reload', () => {
  it('returns added ids and current total', async () => {
    reloadResult = { added: ['member-0001-pulse'], total: 3 };
    const res = await fetch(url('/internal/agents/reload'), { method: 'POST' });
    expect(res.status).toBe(200);
    const body = (await res.json()) as { ok: boolean; added: string[]; total: number; addedIds: string[] };
    expect(body.ok).toBe(true);
    expect(body.added).toEqual(['member-0001-pulse']);
    expect(body.addedIds).toEqual(['member-0001-pulse']);
    expect(body.total).toBe(3);
  });

  it('re-reads scheduled jobs outside placement without stopping running work', async () => {
    schedulerRunningIds = ['job-in-flight'];
    const res = await fetch(url('/internal/agents/reload'), { method: 'POST' });
    expect(res.status).toBe(200);
    expect(schedulerReload).toHaveBeenCalledOnce();
    expect(schedulerStop).not.toHaveBeenCalled();
  });

  it('returns 500 when reload throws', async () => {
    reloadShouldThrow = true;
    const res = await fetch(url('/internal/agents/reload'), { method: 'POST' });
    expect(res.status).toBe(500);
    const body = (await res.json()) as { ok: boolean; error: string };
    expect(body.ok).toBe(false);
    expect(body.error).toBe('reload_failed');
  });

  it('an invalid cron candidate fails the reload before any channel changes', async () => {
    schedulerReload.mockImplementation(() => { throw new Error('Failed to parse cron-jobs.json'); });
    const res = await fetch(url('/internal/agents/reload'), { method: 'POST' });
    expect(res.status).toBe(500);
    expect(((await res.json()) as { message: string }).message).toContain('cron-jobs.json');
    expect(fakeRouter.reload).not.toHaveBeenCalled();
  });

  it('placement reload with an in-flight cron job performs zero mutations', async () => {
    await handle.stop();
    const preflight = vi.fn();
    const routerReload = vi.fn(() => ({ users: 0, bindings: 0 }));
    const telegramReload = vi.fn(async () => ({ added: [], removed: [], total: 0 }));
    const feishuReload = vi.fn(async () => ({ added: [], removed: [], total: 0 }));
    const schedulerStop = vi.fn();
    const schedulerReload = vi.fn();
    handle = await startRelayHttpServer({
      feishuBots: {
        start: async () => {}, stop: () => {}, reload: feishuReload,
        runningAgentIds: () => [], getMessageClient: () => null,
        notifyShutdown: async () => {}, notifyResumed: async () => {},
      },
      telegramBots: {
        start: async () => {}, stop: () => {}, reload: telegramReload,
        runningAgentIds: () => [], getApiBot: () => null, notifyResumed: async () => {},
      },
      router: { ...fakeRouter, reload: routerReload } as unknown as Router,
      configPath,
      preflightPlacement: preflight,
      scheduler: {
        getRunningJobIds: () => ['job-in-flight'],
        stop: schedulerStop,
        reload: schedulerReload,
      } satisfies Pick<Scheduler, 'getRunningJobIds' | 'stop' | 'reload'>,
    }, 0);

    const res = await fetch(url('/internal/agents/reload'), { method: 'POST' });
    expect(res.status).toBe(500);
    expect(preflight).toHaveBeenCalledOnce();
    expect(routerReload).not.toHaveBeenCalled();
    expect(telegramReload).not.toHaveBeenCalled();
    expect(feishuReload).not.toHaveBeenCalled();
    expect(schedulerStop).not.toHaveBeenCalled();
    expect(schedulerReload).not.toHaveBeenCalled();
  });
});

describe('Relay HTTP — /internal/pairing/approve', () => {
  it('400 on missing fields', async () => {
    const res = await fetch(url('/internal/pairing/approve'), {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ code: 'AB1234' }),
    });
    expect(res.status).toBe(400);
    const body = (await res.json()) as { error: string };
    expect(body.error).toBe('missing_fields');
  });

  it('404 when pairing code unknown', async () => {
    vi.mocked(feishu.peekPendingPairingForHttp).mockReturnValue(undefined);
    const res = await fetch(url('/internal/pairing/approve'), {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ code: 'AB1234', principalId: 'member-0001', agentId: 'member-0001-pulse' }),
    });
    expect(res.status).toBe(404);
    const body = (await res.json()) as { error: string };
    expect(body.error).toBe('code_unknown');
  });

  it('409 when agent does not match pending pairing', async () => {
    vi.mocked(feishu.peekPendingPairingForHttp).mockReturnValue({
      agentId: 'member-0042-pulse',
      openId: 'ou_xyz',
    });
    const res = await fetch(url('/internal/pairing/approve'), {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ code: 'AB1234', principalId: 'member-0001', agentId: 'member-0001-pulse' }),
    });
    expect(res.status).toBe(409);
    const body = (await res.json()) as { error: string; expectedAgentId: string };
    expect(body.error).toBe('agent_mismatch');
    expect(body.expectedAgentId).toBe('member-0042-pulse');
    expect(vi.mocked(feishu.consumePendingPairingForHttp)).not.toHaveBeenCalled();
  });

  it('200 + consumes pairing on happy path', async () => {
    vi.mocked(feishu.peekPendingPairingForHttp).mockReturnValue({
      agentId: 'member-0001-pulse',
      openId: 'ou_happy_path_open_id_xyz',
    });
    vi.mocked(feishu.approveFeishuPairingViaHttp).mockReturnValue(undefined);
    const res = await fetch(url('/internal/pairing/approve'), {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ code: 'AB1234', principalId: 'member-0001', agentId: 'member-0001-pulse' }),
    });
    expect(res.status).toBe(200);
    const body = (await res.json()) as { ok: boolean; openId: string; agentId: string; principalId: string };
    expect(body.ok).toBe(true);
    expect(body.openId).toBe('ou_happy_path_open_id_xyz');
    expect(body.agentId).toBe('member-0001-pulse');
    expect(body.principalId).toBe('member-0001');
    expect(vi.mocked(feishu.consumePendingPairingForHttp)).toHaveBeenCalledWith('AB1234');
    expect(vi.mocked(feishu.approveFeishuPairingViaHttp)).toHaveBeenCalledWith({
      configPath,
      router: fakeRouter,
      principalId: 'member-0001',
      agentId: 'member-0001-pulse',
      openId: 'ou_happy_path_open_id_xyz',
    });
  });

  it('404 config_missing_agent when writeOpenIdToConfig blames missing agent', async () => {
    vi.mocked(feishu.peekPendingPairingForHttp).mockReturnValue({
      agentId: 'member-0001-pulse',
      openId: 'ou_x',
    });
    vi.mocked(feishu.approveFeishuPairingViaHttp).mockImplementation(() => {
      throw new Error('Cannot approve binding for missing Feishu agent "member-0001-pulse"');
    });
    const res = await fetch(url('/internal/pairing/approve'), {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ code: 'AB1234', principalId: 'member-0001', agentId: 'member-0001-pulse' }),
    });
    expect(res.status).toBe(404);
    const body = (await res.json()) as { error: string };
    expect(body.error).toBe('config_missing_agent');
  });
});

describe('Relay HTTP — unknown route', () => {
  it('404', async () => {
    const res = await fetch(url('/internal/unknown'));
    expect(res.status).toBe(404);
  });
});

describe('relay.json sanity', () => {
  it('config file remains valid json after no-op test cycle', () => {
    const raw = readFileSync(configPath, 'utf-8');
    const parsed = JSON.parse(raw);
    expect(parsed.users).toEqual([]);
  });
});
