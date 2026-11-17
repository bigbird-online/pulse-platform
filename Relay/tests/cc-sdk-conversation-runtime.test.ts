import { existsSync, mkdirSync, mkdtempSync, readFileSync, readdirSync, rmSync, writeFileSync } from 'node:fs';
import { createConversationRuntimeManager } from '../src/conversation-runtime.js';
import { createMaintenanceBarrier } from '../src/maintenance-barrier.js';
import { persistQueuedTurns, replayPendingTurns } from '../src/shutdown-interrupts.js';
import { createCollaborationCapture } from '../src/collaboration-capture.js';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import {
  buildSdkEngineEnv,
  createCcSdkConversationRuntime,
  resolveProviderEnv,
  type SdkMessage,
  type SdkQueryOptions,
  type SdkUserMessage,
} from '../src/cc-sdk-conversation-runtime.js';
import type { StateManager, StreamJsonMessage } from '../src/types.js';

/**
 * A controllable mock of the SDK `query`. It records the options it was called
 * with (so tests can assert resume / env / model), drains the streaming-input
 * prompt iterable, and for each user turn yields a scripted (init?, assistant,
 * result) sequence. Session id is stable per query invocation unless overridden.
 */
interface MockQueryCall {
  options: SdkQueryOptions;
  turns: string[];
}

function createMockSdk(opts: { sessionId?: string; assistantReply?: (turn: string) => string } = {}) {
  const calls: MockQueryCall[] = [];
  const sessionId = opts.sessionId ?? 'sdk-session-fixed';
  const reply = opts.assistantReply ?? ((t: string) => `echo:${t}`);

  const query = (args: { prompt: AsyncIterable<SdkUserMessage>; options: SdkQueryOptions }): AsyncIterable<SdkMessage> => {
    const call: MockQueryCall = { options: args.options, turns: [] };
    calls.push(call);
    // Prefer the resume id if provided so the "same session id survives" invariant holds.
    const sid = (args.options.resume as string) || sessionId;
    let emittedInit = false;
    return {
      async *[Symbol.asyncIterator](): AsyncGenerator<SdkMessage> {
        for await (const userMsg of args.prompt) {
          const text = typeof userMsg.message.content === 'string'
            ? userMsg.message.content
            : JSON.stringify(userMsg.message.content);
          call.turns.push(text);
          if (!emittedInit) {
            emittedInit = true;
            yield { type: 'system', subtype: 'init', session_id: sid } as SdkMessage;
          }
          yield {
            type: 'assistant',
            session_id: sid,
            message: { role: 'assistant', content: [{ type: 'text', text: reply(text) }] },
          } as SdkMessage;
          yield { type: 'result', subtype: 'success', session_id: sid, is_error: false } as SdkMessage;
        }
      },
    };
  };

  return { query, calls, sessionId };
}

/** In-memory StateManager stub recording the last persisted patch per poolKey. */
function createStateStub(): StateManager & { store: Map<string, Record<string, unknown>>; flushCount: number } {
  const store = new Map<string, Record<string, unknown>>();
  const mgr = {
    store,
    flushCount: 0,
    load: () => ({ version: 1 as const, updatedAt: '', sessions: {} }),
    getSession: (poolKey: string) => store.get(poolKey) as never,
    updateSession: (poolKey: string, patch: Record<string, unknown>) => {
      store.set(poolKey, { ...(store.get(poolKey) ?? {}), ...patch });
    },
    removeSession: (poolKey: string) => { store.delete(poolKey); },
    getAllSessions: () => ({}),
    flush: () => { mgr.flushCount++; },
    dispose: () => {},
  } as StateManager & { store: Map<string, Record<string, unknown>>; flushCount: number };
  return mgr;
}

const humanMsg = (content: string): StreamJsonMessage => ({ type: 'human', content });

/** Wait for a predicate to become true (poll), for driving async engine loop. */
async function waitFor(pred: () => boolean, timeoutMs = 2000): Promise<void> {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    if (pred()) return;
    await new Promise((r) => setTimeout(r, 5));
  }
  throw new Error('waitFor timed out');
}

describe('cc-sdk conversation runtime — provider env injection', () => {
  const ROUTE = { baseUrl: 'https://fixture.example/anthropic', authToken: 'fixture-token' };

  it('subscription mode injects NO base_url/token (subscription OAuth path)', () => {
    // Simulate a poisoned parent env.
    const prev = { url: process.env.ANTHROPIC_BASE_URL, tok: process.env.ANTHROPIC_AUTH_TOKEN };
    process.env.ANTHROPIC_BASE_URL = 'https://poison.example';
    process.env.ANTHROPIC_AUTH_TOKEN = 'poison-token';
    try {
      const { env, model } = resolveProviderEnv('subscription', undefined);
      expect(env.ANTHROPIC_BASE_URL).toBeUndefined();
      expect(env.ANTHROPIC_AUTH_TOKEN).toBeUndefined();
      expect(env.ANTHROPIC_API_KEY).toBeUndefined();
      expect(model).toBeUndefined();
    } finally {
      if (prev.url === undefined) delete process.env.ANTHROPIC_BASE_URL; else process.env.ANTHROPIC_BASE_URL = prev.url;
      if (prev.tok === undefined) delete process.env.ANTHROPIC_AUTH_TOKEN; else process.env.ANTHROPIC_AUTH_TOKEN = prev.tok;
    }
  });

  it('an explicit engine route sets ANTHROPIC_BASE_URL/AUTH_TOKEN and keeps the configured model', () => {
    const { env, model } = resolveProviderEnv('subscription', 'sonnet', ROUTE);
    expect(env.ANTHROPIC_BASE_URL).toBe(ROUTE.baseUrl);
    expect(env.ANTHROPIC_AUTH_TOKEN).toBe(ROUTE.authToken);
    expect(model).toBe('sonnet');
  });

  it('apikey mode is reserved — inert, behaves like subscription (no base_url)', () => {
    const { env } = resolveProviderEnv('apikey', undefined);
    expect(env.ANTHROPIC_BASE_URL).toBeUndefined();
  });

  it('buildSdkEngineEnv strips inherited routing and retired provider env unless an override is given', () => {
    vi.stubEnv('ANTHROPIC_BASE_URL', 'https://poison.example');
    vi.stubEnv('ANTHROPIC_DEFAULT_OPUS_MODEL', 'retired-opus');
    vi.stubEnv('GLM_ANTHROPIC_AUTH_TOKEN', 'retired-token');
    vi.stubEnv('OPENROUTER_ANTHROPIC_BASE_URL', 'https://openrouter.ai/api');
    try {
      const env = buildSdkEngineEnv();
      expect(env.ANTHROPIC_BASE_URL).toBeUndefined();
      expect(env.ANTHROPIC_DEFAULT_OPUS_MODEL).toBeUndefined();
      expect(env.GLM_ANTHROPIC_AUTH_TOKEN).toBeUndefined();
      expect(env.OPENROUTER_ANTHROPIC_BASE_URL).toBe('https://openrouter.ai/api');
      expect(buildSdkEngineEnv({ ANTHROPIC_BASE_URL: 'https://ok' }).ANTHROPIC_BASE_URL).toBe('https://ok');
    } finally {
      vi.unstubAllEnvs();
    }
  });

  it('adapter passes the explicit engine route to the SDK at spawn', async () => {
    const sdk = createMockSdk();
    const adapter = createCcSdkConversationRuntime({ sdk, engineRoute: ROUTE });
    await adapter.spawn({ tenantId: 't-route', workspace: '/tmp', sessionId: 's1' });
    await adapter.send('t-route', humanMsg('hi'));
    await waitFor(() => sdk.calls.length >= 1 && sdk.calls[0].turns.length >= 1);
    expect(sdk.calls[0].options.env?.ANTHROPIC_BASE_URL).toBe(ROUTE.baseUrl);
    adapter.dispose();
  });
});

describe('cc-sdk conversation runtime — session lifecycle', () => {
  it('spawn does NOT start a resident engine until the first send', async () => {
    const sdk = createMockSdk();
    const adapter = createCcSdkConversationRuntime({ sdk });
    await adapter.spawn({ tenantId: 't1', workspace: '/tmp', sessionId: 's1' });
    // No query yet — lazy: engine only starts on first message.
    expect(sdk.calls.length).toBe(0);
    expect(adapter.get('t1')?.state).toBe('ready');
    adapter.dispose();
  });

  it('first turn of a brand-new session does NOT resume; a completed turn flips resume on', async () => {
    const sdk = createMockSdk();
    const adapter = createCcSdkConversationRuntime({ sdk });
    await adapter.spawn({ tenantId: 't1', workspace: '/tmp', sessionId: 's1' });
    await adapter.send('t1', humanMsg('first'));
    await waitFor(() => sdk.calls.length >= 1 && sdk.calls[0].turns.length >= 1);
    // Brand-new session: no resume on the first engine start.
    expect(sdk.calls[0].options.resume).toBeUndefined();
    adapter.dispose();
  });

  it('emits turn-accepted + response(assistant) + response(result) for a turn', async () => {
    const sdk = createMockSdk({ assistantReply: () => 'the-answer' });
    const adapter = createCcSdkConversationRuntime({ sdk });
    const events: string[] = [];
    const assistantTexts: string[] = [];
    adapter.on('turn-accepted', () => events.push('turn-accepted'));
    adapter.on('response', (_t: string, msg: StreamJsonMessage) => {
      events.push(`response:${msg.type}`);
      if (msg.type === 'assistant') assistantTexts.push(String(msg.content));
    });
    await adapter.spawn({ tenantId: 't1', workspace: '/tmp', sessionId: 's1' });
    await adapter.send('t1', humanMsg('q'));
    await waitFor(() => events.includes('response:result'));
    expect(events[0]).toBe('turn-accepted');
    expect(events).toContain('response:assistant');
    expect(events).toContain('response:result');
    expect(assistantTexts).toContain('the-answer');
    adapter.dispose();
  });

  it('counts each SDK tool call once, tags the result runtime and records quota', async () => {
    const quotaDir = mkdtempSync(join(tmpdir(), 'cc-sdk-quota-'));
    vi.stubEnv('PULSE_CC_QUOTA_PATH', join(quotaDir, 'cc-quota-v1.json'));
    const toolUse = (id: string, name: string) => ({ type: 'assistant', session_id: 's',
      message: { role: 'assistant', content: [{ type: 'tool_use', id, name, input: {} }] } } as SdkMessage);
    const sdk = { query: (args: { prompt: AsyncIterable<SdkUserMessage> }): AsyncIterable<SdkMessage> => ({
      async *[Symbol.asyncIterator](): AsyncGenerator<SdkMessage> {
        for await (const _turn of args.prompt) {
          yield { type: 'rate_limit_event', session_id: 's', rate_limit_info: { status: 'allowed_warning',
            unifiedWindows: { seven_day: { utilization: 0.81, resetsAt: 1790906400 } } } } as SdkMessage;
          yield toolUse('toolu_1', 'Grep');
          yield toolUse('toolu_1', 'Grep');
          yield toolUse('toolu_2', 'Edit');
          yield { type: 'result', subtype: 'success', session_id: 's', is_error: false } as SdkMessage;
        }
      },
    }) };
    try {
      const adapter = createCcSdkConversationRuntime({ sdk });
      const effects: string[] = [];
      const results: StreamJsonMessage[] = [];
      adapter.on('turn-effect', (_t: string, effect: string) => effects.push(effect));
      adapter.on('response', (_t: string, msg: StreamJsonMessage) => { if (msg.type === 'result') results.push(msg); });
      await adapter.spawn({ tenantId: 't1', workspace: '/tmp', sessionId: 's1' });
      await adapter.send('t1', humanMsg('q'), { correlationId: 'c1', idempotencyKey: 'k1', effect: 'no-effect' });
      await waitFor(() => results.length > 0);
      expect(effects).toEqual(['read-only-effect', 'side-effect-observed']);
      expect(results[0]?.runtime).toBe('cc-sdk');
      const quota = JSON.parse(readFileSync(join(quotaDir, 'cc-quota-v1.json'), 'utf8'));
      expect(quota).toMatchObject({ rate_limit_status: 'allowed_warning', weekly: { used_percent: 81 }, five_hour: null });
      adapter.dispose();
    } finally {
      vi.unstubAllEnvs();
      rmSync(quotaDir, { recursive: true, force: true });
    }
  });

  it('persists sessionId + runtime to state.json on spawn and after each turn', async () => {
    const sdk = createMockSdk({ sessionId: 'sdk-abc' });
    const state = createStateStub();
    const adapter = createCcSdkConversationRuntime({ sdk, stateManager: state });
    await adapter.spawn({ tenantId: 't1', workspace: '/tmp', sessionId: 'sdk-abc' });
    expect(state.store.get('t1')?.runtime).toBe('cc-sdk');
    expect(state.store.get('t1')?.sessionId).toBe('sdk-abc');
    await adapter.send('t1', humanMsg('q'));
    await waitFor(() => state.store.get('t1')?.status === 'active');
    expect(state.store.get('t1')?.sessionId).toBe('sdk-abc');
    adapter.dispose();
  });

  it('send to an unknown / dead tenant throws', async () => {
    const sdk = createMockSdk();
    const adapter = createCcSdkConversationRuntime({ sdk });
    await expect(adapter.send('nope', humanMsg('x'))).rejects.toThrow(/No active cc-sdk/);
    adapter.dispose();
  });

  it('rejects a duplicate spawn for a live tenant', async () => {
    const sdk = createMockSdk();
    const adapter = createCcSdkConversationRuntime({ sdk });
    await adapter.spawn({ tenantId: 't1', workspace: '/tmp', sessionId: 's1' });
    await expect(adapter.spawn({ tenantId: 't1', workspace: '/tmp', sessionId: 's1' })).rejects.toThrow(/already exists/);
    adapter.dispose();
  });
});

describe('cc-sdk conversation runtime — failure is observable, never silent', () => {
  it('an engine loop crash emits error + exit (not a silent hang)', async () => {
    // SDK query() whose iterator throws mid-stream — the invisible-failure
    // shape that left feishu cc-sdk agents dark. It must now surface as events.
    const boomSdk = {
      query: (): AsyncIterable<SdkMessage> => ({
        // eslint-disable-next-line require-yield
        async *[Symbol.asyncIterator](): AsyncGenerator<SdkMessage> {
          throw new Error('SIMULATED engine crash');
        },
      }),
    };
    const adapter = createCcSdkConversationRuntime({ sdk: boomSdk });
    const errors: string[] = [];
    const exits: Array<number | null> = [];
    adapter.on('error', (_t: string, e: Error) => errors.push(e.message));
    adapter.on('exit', (_t: string, code: number | null) => exits.push(code));

    await adapter.spawn({ tenantId: 't1', workspace: '/tmp', sessionId: 's1' });
    await adapter.send('t1', humanMsg('hi'));
    await waitFor(() => errors.length > 0 && exits.length > 0, 2000);

    expect(errors[0]).toMatch(/SIMULATED engine crash/);
    expect(exits[0]).toBe(1);
    // The session is marked dead so a later send() re-spawns rather than hanging.
    expect(adapter.get('t1')?.state).toBe('dead');
    adapter.dispose();
  });

  it('a silently stalled engine (no SDK output), retries disabled, fires the watchdog → error + exit', async () => {
    // The exact live shape: SDK query() starts but yields NOTHING and never
    // throws. Without the watchdog the turn hangs forever with no signal.
    // stallRetries:0 isolates the pure watchdog assertion (no self-heal).
    const stallSdk = {
      query: (): AsyncIterable<SdkMessage> => ({
        [Symbol.asyncIterator](): AsyncIterator<SdkMessage> {
          // Never resolves → the for-await loop blocks forever.
          return { next: () => new Promise<IteratorResult<SdkMessage>>(() => {}) };
        },
      }),
    };
    const adapter = createCcSdkConversationRuntime({ sdk: stallSdk, firstChunkTimeoutMs: 60, stallRetries: 0 });
    const errors: string[] = [];
    const exits: Array<number | null> = [];
    adapter.on('error', (_t: string, e: Error) => errors.push(e.message));
    adapter.on('exit', (_t: string, code: number | null) => exits.push(code));

    await adapter.spawn({ tenantId: 't1', workspace: '/tmp', sessionId: 's1' });
    await adapter.send('t1', humanMsg('hi'));
    await waitFor(() => errors.length > 0 && exits.length > 0, 2000);

    expect(errors[0]).toMatch(/no output within|silent stall/);
    expect(exits[0]).toBe(1);
    expect(adapter.get('t1')?.state).toBe('dead');
    adapter.dispose();
  });

  it('a first-chunk stall SELF-HEALS: restart the engine, replay the in-flight turn, reply arrives (no error)', async () => {
    // Reproduces the in-situ cold-start stall: the FIRST query() hangs silently,
    // the SECOND (post-restart) behaves. The user must get their reply with no
    // error surfaced and no message lost — the whole point of the self-heal.
    let queryCall = 0;
    const replayedTurns: string[] = [];
    const sdk = {
      query: (args: { prompt: AsyncIterable<SdkUserMessage>; options: SdkQueryOptions }): AsyncIterable<SdkMessage> => {
        queryCall++;
        const call = queryCall;
        if (call === 1) {
          // Silent stall: yields nothing, never throws.
          return { [Symbol.asyncIterator]: (): AsyncIterator<SdkMessage> => ({ next: () => new Promise<IteratorResult<SdkMessage>>(() => {}) }) };
        }
        return {
          async *[Symbol.asyncIterator](): AsyncGenerator<SdkMessage> {
            for await (const userMsg of args.prompt) {
              const text = typeof userMsg.message.content === 'string' ? userMsg.message.content : JSON.stringify(userMsg.message.content);
              replayedTurns.push(text);
              yield { type: 'system', subtype: 'init', session_id: 'healed-sess' } as SdkMessage;
              yield { type: 'assistant', message: { role: 'assistant', content: [{ type: 'text', text: `echo:${text}` }] } } as SdkMessage;
              yield { type: 'result', subtype: 'success', is_error: false } as SdkMessage;
            }
          },
        };
      },
    };
    const adapter = createCcSdkConversationRuntime({ sdk, firstChunkTimeoutMs: 40, stallRetries: 1 });
    const responses: StreamJsonMessage[] = [];
    const errors: string[] = [];
    adapter.on('response', (_t: string, m: StreamJsonMessage) => responses.push(m));
    adapter.on('error', (_t: string, e: Error) => errors.push(e.message));

    await adapter.spawn({ tenantId: 't1', workspace: '/tmp', sessionId: 's1' });
    await adapter.send('t1', humanMsg('hello-there'));
    await waitFor(() => responses.some((m) => m.type === 'result'), 3000);

    // Restarted exactly once; the replayed turn carried the ORIGINAL content.
    expect(queryCall).toBe(2);
    expect(replayedTurns).toContain('hello-there');
    // User got their reply, and NO error was surfaced (self-heal is invisible).
    expect(responses.some((m) => m.type === 'assistant' && m.content === 'echo:hello-there')).toBe(true);
    expect(errors).toHaveLength(0);
    expect(adapter.get('t1')?.state).toBe('ready');
    adapter.dispose();
  });

  it('manager kill retains bounded old-attempt routing for buffered SDK settlement', async () => {
    const directory=mkdtempSync(join(tmpdir(),'cc-sdk-kill-usage-'));
    let release!:()=>void;let entered=false;
    const gate=new Promise<void>(r=>{release=r;});
    const sdk={query(args:{prompt:AsyncIterable<SdkUserMessage>;options:SdkQueryOptions}):AsyncIterable<SdkMessage>{
      return {async *[Symbol.asyncIterator](){for await(const _m of args.prompt){
        entered=true;await gate;
        yield {type:'result',subtype:'success',session_id:'native-s',is_error:false,
          usage:{input_tokens:7,output_tokens:3}} as SdkMessage;return;
      }}};
    }};
    const capture=createCollaborationCapture(directory)!;
    const persisted=vi.spyOn(capture,'response');
    const manager=createConversationRuntimeManager({collaborationCapture:capture});
    manager.register(createCcSdkConversationRuntime({sdk,firstChunkTimeoutMs:5000}));
    const channel=vi.fn();manager.on('response',channel);
    try {
      await manager.spawn({tenantId:'late-test',workspace:'/tmp',sessionId:'configured-s',runtime:'cc-sdk'});
      await manager.send('late-test',humanMsg('synthetic'));
      await waitFor(()=>entered);
      await manager.kill('late-test');channel.mockClear();release();
      await waitFor(()=>persisted.mock.calls.some(([,msg])=>msg.host_usage_only===true));
      expect(channel).not.toHaveBeenCalled();
      const rows=readdirSync(directory).filter(f=>f.endsWith('.jsonl')).flatMap(f=>
        readFileSync(join(directory,f),'utf8').trim().split('\n').map(line=>JSON.parse(line)));
      const usage=rows.find(r=>r.kind==='usage'&&r.usage?.input_tokens===7);
      expect(usage?.usage.output_tokens).toBe(3);
      expect(rows.some(r=>r.kind==='execution'&&r.state==='cancelled'&&r.attempt_ref===usage.attempt_ref)).toBe(true);
    } finally {release();manager.dispose();rmSync(directory,{recursive:true,force:true});}
  });

  it('preserves aborted-generation usage as old-attempt telemetry without completing replay', async () => {
    let releaseOld!:()=>void;let releaseNew!:()=>void;let calls=0;
    const oldGate=new Promise<void>(r=>{releaseOld=r;});
    const newGate=new Promise<void>(r=>{releaseNew=r;});
    const sdk={query(args:{prompt:AsyncIterable<SdkUserMessage>;options:SdkQueryOptions}):AsyncIterable<SdkMessage>{
      const call=++calls;
      return {async *[Symbol.asyncIterator](){
        for await(const _message of args.prompt){
          if(call===1) await oldGate;
          else {yield {type:'system',subtype:'init',session_id:'native-s'} as SdkMessage;await newGate;}
          yield {type:'result',subtype:'success',session_id:'native-s',is_error:false,
            usage:{input_tokens:7,output_tokens:3}} as SdkMessage;
          return;
        }
      }};
    }};
    const adapter=createCcSdkConversationRuntime({sdk,firstChunkTimeoutMs:40,stallRetries:1});
    const context={correlationId:'dispatch',idempotencyKey:'dispatch',effect:'no-effect' as const,hostAttemptRef:'old-attempt'};
    const responses:StreamJsonMessage[]=[];
    adapter.on('response',(_tenant,msg)=>responses.push(msg));
    adapter.on('turn-replay',()=>{context.hostAttemptRef='new-attempt';});
    await adapter.spawn({tenantId:'late-test',workspace:'/tmp',sessionId:'configured-s',runtime:'cc-sdk'});
    try {
      await adapter.send('late-test',humanMsg('synthetic'),context);
      await waitFor(()=>calls===2);
      releaseOld();
      await waitFor(()=>responses.some(r=>r.host_usage_only===true));
      expect(responses.find(r=>r.host_usage_only===true)).toMatchObject({
        host_attempt_ref:'old-attempt',session_id:'native-s',usage:{input_tokens:7,output_tokens:3}});
      expect(responses.filter(r=>r.type==='result'&&r.host_usage_only!==true)).toHaveLength(0);
      releaseNew();
      await waitFor(()=>responses.some(r=>r.type==='result'&&r.host_usage_only!==true));
      expect(responses.find(r=>r.type==='result'&&r.host_usage_only!==true)?.host_attempt_ref).toBe('new-attempt');
    } finally {releaseOld();releaseNew();await adapter.killAll();adapter.dispose();}
  });

  it('a persistent stall exhausts self-heal retries then fails loud (bounded, never infinite)', async () => {
    // Every query() stalls → the adapter restarts stallRetries times, then MUST
    // surface an error (never an infinite restart loop). "静默失败不可接受".
    let queryCall = 0;
    const stallSdk = {
      query: (): AsyncIterable<SdkMessage> => {
        queryCall++;
        return { [Symbol.asyncIterator]: (): AsyncIterator<SdkMessage> => ({ next: () => new Promise<IteratorResult<SdkMessage>>(() => {}) }) };
      },
    };
    const adapter = createCcSdkConversationRuntime({ sdk: stallSdk, firstChunkTimeoutMs: 40, stallRetries: 2 });
    const errors: string[] = [];
    const exits: Array<number | null> = [];
    adapter.on('error', (_t: string, e: Error) => errors.push(e.message));
    adapter.on('exit', (_t: string, code: number | null) => exits.push(code));

    await adapter.spawn({ tenantId: 't1', workspace: '/tmp', sessionId: 's1' });
    await adapter.send('t1', humanMsg('hi'));
    await waitFor(() => errors.length > 0 && exits.length > 0, 3000);

    // Initial attempt + 2 restarts = 3 query() calls, then error (bounded).
    expect(queryCall).toBe(3);
    expect(errors[0]).toMatch(/2 restart\(s\)|silent stall/);
    expect(exits[0]).toBe(1);
    expect(adapter.get('t1')?.state).toBe('dead');
    adapter.dispose();
  });

  it('an SDK load failure at engine start rejects send() and emits error (not swallowed)', async () => {
    // getQuery throws in startEngineLoop BEFORE the loop. The guard must reject
    // send() so the channel send-catch can notify the user, and emit an error event.
    const sdk = { get query(): never { throw new Error('sdk load failed'); } };
    const adapter = createCcSdkConversationRuntime({ sdk });
    const errors: string[] = [];
    adapter.on('error', (_t: string, e: Error) => errors.push(e.message));

    await adapter.spawn({ tenantId: 't1', workspace: '/tmp', sessionId: 's1' });
    await expect(adapter.send('t1', humanMsg('hi'))).rejects.toThrow();
    expect(errors.length).toBeGreaterThan(0);
    adapter.dispose();
  });

  it('two concurrent cold sends start ONE engine (no duplicate) and BOTH turns reply', async () => {
    // The production race: two feishu messages arrive nearly simultaneously on a
    // non-resident session. Before the fix, `resident` was only set after
    // `await getQuery()`, so both sends passed the `!resident` guard and each
    // spawned an engine (query() called twice → duplicate loops trample shared
    // state → silence). The start latch must serialize them onto ONE engine.
    let queryCalls = 0;
    const base = createMockSdk();
    const countingSdk = {
      query: (args: { prompt: AsyncIterable<SdkUserMessage>; options: SdkQueryOptions }): AsyncIterable<SdkMessage> => {
        queryCalls++;
        return base.query(args);
      },
    };
    const adapter = createCcSdkConversationRuntime({ sdk: countingSdk });
    const replies: string[] = [];
    let resultCount = 0;
    adapter.on('response', (_t: string, m: StreamJsonMessage) => {
      if (m.type === 'assistant' && typeof m.content === 'string' && m.content) replies.push(m.content);
      if (m.type === 'result') resultCount++;
    });

    await adapter.spawn({ tenantId: 't1', workspace: '/tmp', sessionId: 's1' });
    // Fire both WITHOUT awaiting the first — the exact concurrent-arrival shape.
    const p1 = adapter.send('t1', humanMsg('alpha'));
    const p2 = adapter.send('t1', humanMsg('beta'));
    await Promise.all([p1, p2]);
    await waitFor(() => resultCount >= 2, 2000);

    // Exactly one engine started, and BOTH user turns were served in order.
    expect(queryCalls).toBe(1);
    expect(resultCount).toBe(2);
    expect(replies).toEqual(['echo:alpha', 'echo:beta']);
    adapter.dispose();
  });
});

describe('cc-sdk conversation runtime — busy state drives the restart-safe guard chain', () => {
  /**
   * A mock SDK whose `result` message is GATED: the turn stays mid-flight
   * (assistant emitted, result withheld) until the test releases it. This lets
   * us observe getStatus().state at a moment when the session is genuinely
   * replying — the exact window the external restart-safe guard must see as busy.
   *
   * Root cause under test: cc-sdk used to leave session.state = 'ready' while a
   * turn was in flight (only a separate session.busy bool flipped), so
   * /internal/cc-status (which counts state === 'busy') reported busy=0 and
   * pulse-relay-restart-safe.sh would SIGKILL a replying session → "重启失忆".
   */
  function createGatedSdk() {
    let release!: () => void;
    const gate = new Promise<void>((r) => { release = r; });
    const query = (args: { prompt: AsyncIterable<SdkUserMessage>; options: SdkQueryOptions }): AsyncIterable<SdkMessage> => {
      return {
        async *[Symbol.asyncIterator](): AsyncGenerator<SdkMessage> {
          for await (const userMsg of args.prompt) {
            const text = typeof userMsg.message.content === 'string' ? userMsg.message.content : JSON.stringify(userMsg.message.content);
            yield { type: 'system', subtype: 'init', session_id: 'gated-sess' } as SdkMessage;
            yield { type: 'assistant', session_id: 'gated-sess', message: { role: 'assistant', content: [{ type: 'text', text: `echo:${text}` }] } } as SdkMessage;
            // Hold the turn open until the test releases the gate — the session
            // is "busy/replying" for this whole window.
            await gate;
            yield { type: 'result', subtype: 'success', session_id: 'gated-sess', is_error: false } as SdkMessage;
          }
        },
      };
    };
    return { query, release: () => release() };
  }

  it('getStatus reports state === "busy" while a turn is in flight, then "ready" after result', async () => {
    const sdk = createGatedSdk();
    const adapter = createCcSdkConversationRuntime({ sdk });
    const results: StreamJsonMessage[] = [];
    adapter.on('response', (_t: string, m: StreamJsonMessage) => { if (m.type === 'result') results.push(m); });

    await adapter.spawn({ tenantId: 't1', workspace: '/tmp', sessionId: 's1' });
    await adapter.send('t1', humanMsg('q'));

    // Engine started, turn pushed, assistant emitted, result withheld: the
    // session is mid-reply. cc-status counts state === 'busy' — it MUST be busy.
    const statusBusy = adapter.getStatus().find((s) => s.tenantId === 't1');
    expect(statusBusy?.state).toBe('busy');
    // Cross-check the get() view (used by getAll/get) agrees.
    expect(adapter.get('t1')?.state).toBe('busy');

    // Release the turn: result arrives, state resets to ready.
    sdk.release();
    await waitFor(() => results.length >= 1);
    const statusReady = adapter.getStatus().find((s) => s.tenantId === 't1');
    expect(statusReady?.state).toBe('ready');
    adapter.dispose();
  });

  it('a cc-status style busy count is >= 1 mid-turn and 0 after result (guard-chain contract)', async () => {
    const sdk = createGatedSdk();
    const adapter = createCcSdkConversationRuntime({ sdk });
    const results: StreamJsonMessage[] = [];
    adapter.on('response', (_t: string, m: StreamJsonMessage) => { if (m.type === 'result') results.push(m); });

    await adapter.spawn({ tenantId: 't1', workspace: '/tmp', sessionId: 's1' });
    await adapter.send('t1', humanMsg('q'));

    // Replicate handleCcStatus's counting: busy = statuses filtered by state==='busy'.
    const busyMid = adapter.getStatus().filter((s) => s.state === 'busy').map((s) => s.tenantId);
    expect(busyMid).toContain('t1');
    expect(busyMid.length).toBeGreaterThanOrEqual(1);

    sdk.release();
    await waitFor(() => results.length >= 1);
    const busyAfter = adapter.getStatus().filter((s) => s.state === 'busy');
    expect(busyAfter.length).toBe(0);
    adapter.dispose();
  });

  it('after idle eviction the session is state === "idle" AND busy is false (invariant preserved)', async () => {
    const sdk = createMockSdk({ sessionId: 'sdk-evict' });
    const adapter = createCcSdkConversationRuntime({ sdk, idleResidenceMs: 40 });
    const ended: string[] = [];
    adapter.on('session-end', (_t: string, r: { type: string }) => ended.push(r.type));

    await adapter.spawn({ tenantId: 't1', workspace: '/tmp', sessionId: 'sdk-evict' });
    await adapter.send('t1', humanMsg('turn-1'));
    await waitFor(() => ended.includes('idle-reclaimed'), 2000);

    // Evicted while idle: state is 'idle' and the session is not busy — the
    // guard chain must not see an evicted (engine-gone) session as busy, and
    // the busy↔state invariant (busy → not 'idle') holds.
    const s = adapter.getStatus().find((st) => st.tenantId === 't1');
    expect(s?.state).toBe('idle');
    // No turn in flight after eviction: cc-status busy count excludes it.
    expect(adapter.getStatus().filter((st) => st.state === 'busy').length).toBe(0);
    adapter.dispose();
  });

  it('a stall self-heal keeps state === "busy" across the engine restart (never a spurious ready window)', async () => {
    // The FIRST query() stalls (no output); the watchdog restarts the engine and
    // replays the turn. Throughout the restart the turn is still in flight, so
    // the session must never flip to a 'ready'/'idle' state the restart-safe
    // guard could mistake for "safe to kill". We gate the SECOND (healed) query's
    // result so we can observe state mid-replay after the restart.
    let queryCall = 0;
    let releaseHealed!: () => void;
    const healedGate = new Promise<void>((r) => { releaseHealed = r; });
    const sdk = {
      query: (args: { prompt: AsyncIterable<SdkUserMessage>; options: SdkQueryOptions }): AsyncIterable<SdkMessage> => {
        queryCall++;
        const call = queryCall;
        if (call === 1) {
          // Silent stall: yields nothing, never throws.
          return { [Symbol.asyncIterator]: (): AsyncIterator<SdkMessage> => ({ next: () => new Promise<IteratorResult<SdkMessage>>(() => {}) }) };
        }
        return {
          async *[Symbol.asyncIterator](): AsyncGenerator<SdkMessage> {
            for await (const userMsg of args.prompt) {
              const text = typeof userMsg.message.content === 'string' ? userMsg.message.content : JSON.stringify(userMsg.message.content);
              yield { type: 'system', subtype: 'init', session_id: 'healed' } as SdkMessage;
              yield { type: 'assistant', session_id: 'healed', message: { role: 'assistant', content: [{ type: 'text', text: `echo:${text}` }] } } as SdkMessage;
              await healedGate;
              yield { type: 'result', subtype: 'success', session_id: 'healed', is_error: false } as SdkMessage;
            }
          },
        };
      },
    };
    const adapter = createCcSdkConversationRuntime({ sdk, firstChunkTimeoutMs: 40, stallRetries: 1 });
    const responses: StreamJsonMessage[] = [];
    const errors: string[] = [];
    adapter.on('response', (_t: string, m: StreamJsonMessage) => responses.push(m));
    adapter.on('error', (_t: string, e: Error) => errors.push(e.message));

    await adapter.spawn({ tenantId: 't1', workspace: '/tmp', sessionId: 's1' });
    await adapter.send('t1', humanMsg('hello'));
    // Wait until the restart has happened and the healed engine emitted its
    // assistant chunk (turn genuinely in flight again on the new engine).
    await waitFor(() => queryCall === 2 && responses.some((m) => m.type === 'assistant'), 3000);

    // Mid-replay on the restarted engine: state must be 'busy', not a window
    // where the guard would think it's safe to restart.
    expect(adapter.get('t1')?.state).toBe('busy');
    expect(adapter.getStatus().find((s) => s.tenantId === 't1')?.state).toBe('busy');

    releaseHealed();
    await waitFor(() => responses.some((m) => m.type === 'result'), 3000);
    expect(adapter.get('t1')?.state).toBe('ready');
    expect(errors).toHaveLength(0);
    adapter.dispose();
  });

  it('emitShutdown on a mid-turn session reports wasBusy AND leaves busy=false (drain cannot hang on a torn-down engine)', async () => {
    // A shutdown mid-reply tears the engine down; the mid-turn interrupt must be
    // reported (wasBusy in the session-end event, for resume-time wake), but busy
    // must be cleared so a subsequent drainBusySessions does not spin forever on
    // a session whose engine no longer exists.
    const sdk = createGatedSdk();
    const state = createStateStub();
    const adapter = createCcSdkConversationRuntime({ sdk, stateManager: state });
    const shutdownWasBusy: boolean[] = [];
    adapter.on('session-end', (_t: string, r: { type: string; wasBusy?: boolean }) => {
      if (r.type === 'relay-shutdown') shutdownWasBusy.push(!!r.wasBusy);
    });
    await adapter.spawn({ tenantId: 't1', workspace: '/tmp', sessionId: 's1' });
    await adapter.send('t1', humanMsg('q'));
    // Mid-turn (result gated): session is busy.
    expect(adapter.get('t1')?.state).toBe('busy');

    const notified = adapter.emitShutdown();
    expect(notified.has('t1')).toBe(true);
    // The mid-turn interrupt was reported as busy in the shutdown notice.
    expect(shutdownWasBusy).toContain(true);
    // And drain now converges immediately (engine torn down, busy cleared) — the
    // invariant fix (clearing busy after evict) keeps drain from spinning on a
    // session whose engine no longer exists.
    const remaining = await adapter.drainBusySessions(500);
    expect(remaining.size).toBe(0);
    adapter.dispose();
  });
});

describe('cc-sdk conversation runtime — sends during a turn', () => {
  function createGatedEchoSdk() {
    const engineTurns: string[] = [];
    const gates: Array<() => void> = [];
    const query = (args: { prompt: AsyncIterable<SdkUserMessage>; options: SdkQueryOptions }): AsyncIterable<SdkMessage> => ({
      async *[Symbol.asyncIterator](): AsyncGenerator<SdkMessage> {
        for await (const userMsg of args.prompt) {
          const text = typeof userMsg.message.content === 'string' ? userMsg.message.content : JSON.stringify(userMsg.message.content);
          engineTurns.push(text);
          yield { type: 'system', subtype: 'init', session_id: 'gated' } as SdkMessage;
          yield { type: 'assistant', session_id: 'gated', message: { role: 'assistant', content: [{ type: 'text', text: `echo:${text}` }] } } as SdkMessage;
          await new Promise<void>((release) => { gates.push(release); });
          yield { type: 'result', subtype: 'success', session_id: 'gated', is_error: false } as SdkMessage;
        }
      },
    });
    const release = async (): Promise<void> => { await waitFor(() => gates.length > 0); gates.shift()!(); };
    return { query, engineTurns, release };
  }

  it('holds a send in Relay until the turn in flight ends, then serves it as its own turn, in order', async () => {
    const sdk = createGatedEchoSdk();
    const adapter = createCcSdkConversationRuntime({ sdk });
    await adapter.spawn({ tenantId: 't1', workspace: '/tmp', sessionId: 's1' });
    await adapter.send('t1', humanMsg('first'));
    let secondSent = false;
    const second = adapter.send('t1', humanMsg('second')).then(() => { secondSent = true; });
    const third = adapter.send('t1', humanMsg('third'));
    await waitFor(() => sdk.engineTurns.length === 1);
    await new Promise((r) => setTimeout(r, 20));
    expect(sdk.engineTurns).toEqual(['first']);
    expect(secondSent).toBe(false);
    await sdk.release();
    await second;
    await waitFor(() => sdk.engineTurns.length === 2);
    expect(sdk.engineTurns).toEqual(['first', 'second']);
    await sdk.release();
    await third;
    await waitFor(() => sdk.engineTurns.length === 3);
    expect(sdk.engineTurns).toEqual(['first', 'second', 'third']);
    await sdk.release();
    adapter.dispose();
  });

  it('records sends held behind the busy turn at shutdown, in arrival order, and keeps the interrupted-turn flag', async () => {
    const sdk = createGatedEchoSdk();
    const state = createStateStub();
    const adapter = createCcSdkConversationRuntime({ sdk, stateManager: state });
    await adapter.spawn({ tenantId: 't1', workspace: '/tmp', sessionId: 's1' });
    await adapter.send('t1', humanMsg('in flight'));
    await waitFor(() => sdk.engineTurns.length === 1);
    const held = [adapter.send('t1', humanMsg('queued one')), adapter.send('t1', humanMsg('queued two'))];

    adapter.emitShutdown();
    // Held callers settle: their turns now live in state, not in this process.
    await Promise.all(held);
    const saved = state.store.get('t1') as { wasBusyAtShutdown?: boolean; pendingMaintenanceTurns?: Array<{ message: StreamJsonMessage }> };
    expect(saved.wasBusyAtShutdown).toBe(true);
    expect(saved.pendingMaintenanceTurns?.map((turn) => turn.message.content)).toEqual(['queued one', 'queued two']);
    expect(sdk.engineTurns).toEqual(['in flight']);
    adapter.dispose();
  });

  it('a second shutdown while recorded turns are replayed behind the resumed turn loses none and records none twice', async () => {
    const sdk = createGatedEchoSdk();
    const state = createStateStub();
    const barrier = createMaintenanceBarrier();
    const manager = createConversationRuntimeManager({ barrier });
    manager.register(createCcSdkConversationRuntime({ sdk, stateManager: state }));
    await manager.spawn({ tenantId: 't1', workspace: '/tmp', sessionId: 's1', runtime: 'cc-sdk' });
    state.updateSession('t1', { pendingMaintenanceTurns: [
      { queuedAt: 1, seq: 1_000, message: humanMsg('B') },
      { queuedAt: 2, seq: 2_000, message: humanMsg('C') },
    ] } as never);
    // The resume turn A is busy; the replay parks B and C behind it.
    await manager.send('t1', humanMsg('A'));
    await waitFor(() => sdk.engineTurns.length === 1);
    await replayPendingTurns(state, 't1', (message, submit) => manager.send('t1', message, submit));
    const pending = () => ((state.store.get('t1')?.pendingMaintenanceTurns ?? []) as Array<{ seq?: number; message: StreamJsonMessage }>);
    expect(pending()).toEqual([]);
    // Shutdown again (index.ts order): the barrier closes, the runtime hands B and C back.
    barrier.close(turn => { persistQueuedTurns(state, [turn], Date.now()); });
    manager.emitShutdown();
    expect(pending().map(turn => [turn.seq, turn.message.content])).toEqual([[1_000, 'B'], [2_000, 'C']]);
    // A replay that meets the closed barrier leaves each record once, in place.
    await replayPendingTurns(state, 't1', (message, submit) => manager.send('t1', message, submit));
    expect(pending().map(turn => [turn.seq, turn.message.content])).toEqual([[1_000, 'B'], [2_000, 'C']]);
    expect(sdk.engineTurns).toEqual(['A']);
    manager.dispose();
  });

  it('kill rejects held sends instead of leaving them pending', async () => {
    const sdk = createGatedEchoSdk();
    const adapter = createCcSdkConversationRuntime({ sdk });
    await adapter.spawn({ tenantId: 't1', workspace: '/tmp', sessionId: 's1' });
    await adapter.send('t1', humanMsg('in flight'));
    const held = adapter.send('t1', humanMsg('queued'));
    await adapter.kill('t1');
    await expect(held).rejects.toThrow(/killed/);
    adapter.dispose();
  });
});

describe('cc-sdk conversation runtime — lazy residence (eviction + resume wake)', () => {
  it('evicts the idle engine after idleResidenceMs, keeping sessionId; emits idle-reclaimed', async () => {
    const sdk = createMockSdk({ sessionId: 'sdk-keep' });
    const state = createStateStub();
    const adapter = createCcSdkConversationRuntime({ sdk, stateManager: state, idleResidenceMs: 40 });
    const ended: string[] = [];
    adapter.on('session-end', (_t: string, reason: { type: string }) => ended.push(reason.type));

    await adapter.spawn({ tenantId: 't1', workspace: '/tmp', sessionId: 'sdk-keep' });
    await adapter.send('t1', humanMsg('turn-1'));
    await waitFor(() => state.store.get('t1')?.status === 'active');
    // Idle timer armed on result; wait past idleResidenceMs for eviction.
    await waitFor(() => ended.includes('idle-reclaimed'), 2000);

    // sessionId survives, state is idle, engine no longer resident.
    expect(adapter.get('t1')?.sessionId).toBe('sdk-keep');
    expect(adapter.get('t1')?.state).toBe('idle');
    expect(state.store.get('t1')?.sessionId).toBe('sdk-keep');
    expect(state.store.get('t1')?.status).toBe('idle-reclaimed');
    adapter.dispose();
  });

  it('a new message after eviction RESUMES the same session id (wake-on-demand)', async () => {
    const sdk = createMockSdk({ sessionId: 'sdk-resume-me' });
    const adapter = createCcSdkConversationRuntime({ sdk, idleResidenceMs: 40 });
    const ended: string[] = [];
    adapter.on('session-end', (_t: string, r: { type: string }) => ended.push(r.type));

    await adapter.spawn({ tenantId: 't1', workspace: '/tmp', sessionId: 'sdk-resume-me' });
    await adapter.send('t1', humanMsg('seed'));
    await waitFor(() => sdk.calls.length >= 1 && sdk.calls[0].turns.length >= 1);
    await waitFor(() => ended.includes('idle-reclaimed'), 2000);

    // Second message re-spawns a resident engine WITH resume set.
    await adapter.send('t1', humanMsg('after-restart'));
    await waitFor(() => sdk.calls.length >= 2 && sdk.calls[1].turns.length >= 1);
    expect(sdk.calls[1].options.resume).toBe('sdk-resume-me');
    adapter.dispose();
  });

  it('eviction then resume does NOT leak a spurious error event (abort-race regression)', async () => {
    // Behavioral guard: an evicted engine loop whose query rejects with an
    // abort-style error (as the real SDK does when its AbortController fires)
    // must NOT surface as an `error` event — the abort was deliberate.
    // NOTE: the authoritative proof of the timing-race fix is the real-link
    // smoke (poc/cc-sdk/05-adapter-smoke.ts): the spurious "Operation aborted"
    // error was present before the abort.signal.aborted guard and absent after.
    // This unit test pins the observable contract; the mock cannot fully
    // reproduce the real SDK's async-rejection timing.
    const abortingSdk = (() => {
      const calls: { options: SdkQueryOptions }[] = [];
      const query = (args: { prompt: AsyncIterable<SdkUserMessage>; options: SdkQueryOptions }): AsyncIterable<SdkMessage> => {
        calls.push({ options: args.options });
        const signal = (args.options as { abortController?: AbortController }).abortController?.signal;
        return {
          async *[Symbol.asyncIterator](): AsyncGenerator<SdkMessage> {
            for await (const _userMsg of args.prompt) {
              yield { type: 'system', subtype: 'init', session_id: 'sid-race' } as SdkMessage;
              yield { type: 'assistant', message: { role: 'assistant', content: [{ type: 'text', text: 'ok' }] } } as SdkMessage;
              yield { type: 'result', subtype: 'success', is_error: false } as SdkMessage;
            }
            // Prompt iterator ended (eviction closed it). Mirror the REAL SDK's
            // timing: the abort rejection lands asynchronously, AFTER the resume
            // loop has already reset session.closing to false. A guard keyed on
            // session.closing would then wrongly emit `error`; a guard keyed on
            // THIS loop's abort.signal.aborted correctly swallows it.
            await new Promise((r) => setTimeout(r, 40));
            if (signal?.aborted) throw new Error('Operation aborted');
          },
        };
      };
      return { query, calls };
    })();

    const adapter = createCcSdkConversationRuntime({ sdk: abortingSdk, idleResidenceMs: 30 });
    const errors: string[] = [];
    const ended: string[] = [];
    adapter.on('error', (_t: string, e: Error) => errors.push(e.message));
    adapter.on('session-end', (_t: string, r: { type: string }) => ended.push(r.type));

    await adapter.spawn({ tenantId: 't1', workspace: '/tmp', sessionId: 'sid-race' });
    await adapter.send('t1', humanMsg('turn-1'));
    await waitFor(() => ended.includes('idle-reclaimed'), 2000);
    await adapter.send('t1', humanMsg('turn-2'));
    await waitFor(() => abortingSdk.calls.length >= 2, 2000);
    // Give the old (aborted) loop time to reject (40ms delay in the mock) and
    // (wrongly) emit if the guard is keyed on the racy session.closing flag.
    await new Promise((r) => setTimeout(r, 200));
    expect(errors).toEqual([]);
    adapter.dispose();
  });
});

describe('cc-sdk conversation runtime — resume across a full restart (new adapter instance)', () => {
  it('spawn(resume:true) on a fresh adapter resumes the persisted sessionId on first send', async () => {
    // Simulate a Relay restart: brand-new adapter, sessionId restored from state.json,
    // resume flag set (same contract as the legacy cc path uses).
    const sdk = createMockSdk({ sessionId: 'persisted-sid' });
    const adapter = createCcSdkConversationRuntime({ sdk });
    await adapter.spawn({ tenantId: 't1', workspace: '/tmp', sessionId: 'persisted-sid', resume: true });
    await adapter.send('t1', humanMsg('what did we say before?'));
    await waitFor(() => sdk.calls.length >= 1 && sdk.calls[0].turns.length >= 1);
    // First engine start after a restart resumes the persisted id (no fresh session).
    expect(sdk.calls[0].options.resume).toBe('persisted-sid');
    adapter.dispose();
  });
});

describe('cc-sdk conversation runtime — shutdown drain', () => {
  it('emitShutdown ends resident engines gracefully and reports notified tenants', async () => {
    const sdk = createMockSdk();
    const state = createStateStub();
    const adapter = createCcSdkConversationRuntime({ sdk, stateManager: state });
    await adapter.spawn({ tenantId: 't1', workspace: '/tmp', sessionId: 's1' });
    await adapter.send('t1', humanMsg('q'));
    await waitFor(() => state.store.get('t1')?.status === 'active');
    const notified = adapter.emitShutdown();
    expect(notified.has('t1')).toBe(true);
    adapter.dispose();
  });

  it('drainBusySessions returns empty once no turn is in-flight', async () => {
    const sdk = createMockSdk();
    const adapter = createCcSdkConversationRuntime({ sdk });
    await adapter.spawn({ tenantId: 't1', workspace: '/tmp', sessionId: 's1' });
    await adapter.send('t1', humanMsg('q'));
    // Turn completes quickly with the mock; drain should observe not-busy.
    const remaining = await adapter.drainBusySessions(1000);
    expect(remaining.size).toBe(0);
    adapter.dispose();
  });
});
