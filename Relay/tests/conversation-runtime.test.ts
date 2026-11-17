import {buildRuntimeUsageBinding,runtimeAuthorityRef} from '../src/runtime-usage-binding.js';
import { EventEmitter } from 'node:events';
import { mkdtempSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { createCollaborationCapture } from '../src/collaboration-capture.js';
import { describe, expect, it, vi, beforeEach, afterEach } from 'vitest';
import {
  createConversationRuntimeManager,
  createPoolConversationAdapter,
  resolveConversationRuntimePreference,
  resolveConversationRuntimeId,
  type ConversationProcess,
  type ConversationRuntimeAdapter,
} from '../src/conversation-runtime.js';
import type { ContextSoftWarningEvent, Pool, ProcessStatusInfo, SessionEndReason, SpawnConfig, StreamJsonMessage } from '../src/types.js';

function processFor(tenantId: string, runtime: 'cc' | 'codex' | 'cc-sdk' | 'pi'): ConversationProcess {
  return {
    tenantId,
    runtime,
    sessionId: `${runtime}-session`,
    state: 'ready',
    lastActivity: 100,
    spawnedAt: 50,
    restartCount: 0,
    restartWindowStart: 50,
  };
}

function createMockAdapter(id: 'cc' | 'codex' | 'cc-sdk' | 'pi'): ConversationRuntimeAdapter & { events: EventEmitter; sentMessages: { type: string; content: string }[] } {
  const events = new EventEmitter();
  const processes = new Map<string, ConversationProcess>();
  const sentMessages: { type: string; content: string }[] = [];
  return {
    id,
    events,
    sentMessages,
    spawn: vi.fn(async (config) => {
      const process = processFor(config.tenantId, id);
      processes.set(config.tenantId, process);
      return process;
    }),
    send: vi.fn(async (_tenantId: string, msg: unknown) => {
      sentMessages.push(msg as { type: string; content: string });
    }),
    kill: vi.fn(async (tenantId: string) => {
      processes.delete(tenantId);
    }),
    killAll: vi.fn(async () => {
      processes.clear();
    }),
    get: vi.fn((tenantId: string) => processes.get(tenantId)),
    getAll: vi.fn(() => Array.from(processes.values())),
    getStatus: vi.fn(() => Array.from(processes.values()).map((process) => ({
      tenantId: process.tenantId,
      runtime: process.runtime,
      state: process.state,
      pid: undefined,
      lastActivity: process.lastActivity,
      spawnedAt: process.spawnedAt,
      restartCount: process.restartCount,
    }))),
    on: vi.fn((event: string, handler: (...args: unknown[]) => void) => {
      events.on(event, handler);
    }) as ConversationRuntimeAdapter['on'],
    emitShutdown: vi.fn(),
    dispose: vi.fn(),
  };
}

describe('conversation runtime contract', () => {
  it('defaults missing AgentConfig.runtime to codex', () => {
    expect(resolveConversationRuntimeId({})).toBe('codex');
    expect(resolveConversationRuntimeId({ runtime: 'codex' })).toBe('codex');
  });

  it('resolves conversationRuntimePreference as the ordered TG fallback chain', () => {
    expect(resolveConversationRuntimePreference({ conversationRuntimePreference: ['codex', 'cc'] })).toEqual(['codex', 'cc']);
    expect(resolveConversationRuntimePreference({ runtime: 'codex' })).toEqual(['codex']);
    expect(resolveConversationRuntimePreference({ runtime: 'cc', conversationRuntimePreference: ['cc', 'codex', 'cc'] })).toEqual(['cc', 'codex']);
  });

  it('preserves cc-sdk in the resolved preference instead of collapsing to cc', () => {
    // cc-sdk is a first-class runtime; it must survive preference resolution so
    // an agent configured with runtime: "cc-sdk" routes to the SDK adapter.
    expect(resolveConversationRuntimePreference({ runtime: 'cc-sdk' })).toEqual(['cc-sdk']);
    expect(resolveConversationRuntimePreference({ conversationRuntimePreference: ['cc-sdk'] })).toEqual(['cc-sdk']);
    expect(resolveConversationRuntimePreference({ runtime: 'cc-sdk', conversationRuntimePreference: ['cc-sdk', 'cc'] })).toEqual(['cc-sdk', 'cc']);
  });

  it('keeps structured Pi routing opt-in and outside automatic fallback chains', () => {
    const conversationRuntime = {
      runtime: 'pi' as const,
      model: { provider: 'anthropic', model: 'claude-sonnet-4-5', reasoning: 'high' as const },
      isolationProfile: 'pi-canary',
    };
    expect(resolveConversationRuntimeId({ conversationRuntime })).toBe('pi');
    expect(resolveConversationRuntimePreference({ conversationRuntime })).toEqual(['pi']);
    expect(() => resolveConversationRuntimePreference({
      conversationRuntimePreference: ['cc', 'pi'],
    })).toThrow('Pi runtime is opt-in only');
  });

  it('routes spawn and send to the selected adapter', async () => {
    const cc = createMockAdapter('cc');
    const codex = createMockAdapter('codex');
    const manager = createConversationRuntimeManager();
    manager.register(cc);
    manager.register(codex);

    const spawned = await manager.spawn({
      tenantId: 'member-000:aki',
      runtime: 'codex',
      workspace: '/tmp/workspace',
      sessionId: 'session-1',
    });
    await manager.send('member-000:aki', { type: 'human', content: 'hello' });

    expect(spawned.runtime).toBe('codex');
    expect(codex.spawn).toHaveBeenCalledOnce();
    expect(codex.send).toHaveBeenCalledWith(
      'member-000:aki',
      { type: 'human', content: 'hello' },
      expect.objectContaining({ correlationId: expect.stringMatching(/^turn:/), effect: 'no-effect' }),
    );
    expect(cc.spawn).not.toHaveBeenCalled();
    expect(cc.send).not.toHaveBeenCalled();
  });

  it.each([undefined,null,{}, {epoch:2}, {epoch:999}, {epoch:'invalid'}])('correlates durable entry submission independently of measurement: %j', async measurement => {
    const pi=createMockAdapter('pi');
    const manager=createConversationRuntimeManager();
    manager.register(pi);
    await manager.spawn({
      tenantId:'owner:aki',runtime:'pi',workspace:'/worktrees/wi520',sessionId:'target',
      computeCostBinding:measurement as any,
      conversationRuntime:{
        runtime:'pi',model:{provider:'openai-codex',model:'gpt-5.6',reasoning:'high'},
        isolationProfile:'resident-host-native',
      },
    });
    const operationRef=`operation_v1_${'a'.repeat(64)}`;
    await manager.sendIdempotent('owner:aki',{type:'human',content:'resume'},operationRef);
    expect(pi.send).toHaveBeenCalledWith(
      'owner:aki',
      {type:'human',content:'resume'},
      expect.objectContaining({
        idempotencyKey:operationRef,
        correlationId:`turn:entry:${operationRef}`,
      }),
    );
    const response=vi.fn();manager.on('response',response);
    pi.events.emit('response','owner:aki',{type:'result',subtype:'success',result:'actual adapter event'});
    expect(response).toHaveBeenCalledWith('owner:aki',expect.objectContaining({
      host_turn_idempotency_key:operationRef,host_session_ref:runtimeAuthorityRef('pi-session'),
    }));
    expect(response.mock.calls[0]![1]).not.toHaveProperty('host_work_item_epoch');
  });

  it('does not let shadow metering alter durable operation replay after capture restart', async () => {
    const root=mkdtempSync(join(tmpdir(),'relay-dispatch-'));
    try {
      const first=createMockAdapter('codex'); const config={tenantId:'owner:aki',runtime:'codex' as const,workspace:'/work',sessionId:'bound'};
      const operationRef=`operation_v1_${'c'.repeat(64)}`;
      const before=createConversationRuntimeManager({collaborationCapture:createCollaborationCapture(root)!});before.register(first);
      await before.spawn(config); await before.sendIdempotent(config.tenantId,{type:'human',content:'resume'},operationRef);
      const recovered=createMockAdapter('codex'); const after=createConversationRuntimeManager({collaborationCapture:createCollaborationCapture(root)!});after.register(recovered);
      await after.spawn(config);
      await after.sendIdempotent(config.tenantId,{type:'human',content:'resume'},operationRef);
      expect(recovered.send).toHaveBeenCalledOnce();
    } finally { rmSync(root,{recursive:true,force:true}); }
  });

  it('does not steer a duplicate durable entry into its active Pi turn', async () => {
    const pi=createMockAdapter('pi');
    pi.steer=vi.fn(async()=>undefined);
    const manager=createConversationRuntimeManager();
    manager.register(pi);
    await manager.spawn({
      tenantId:'owner:aki',runtime:'pi',workspace:'/worktrees/wi520',sessionId:'target',
      conversationRuntime:{
        runtime:'pi',model:{provider:'openai-codex',model:'gpt-5.6',reasoning:'high'},
        isolationProfile:'resident-host-native',
      },
    });
    const operationRef=`operation_v1_${'a'.repeat(64)}`;
    const message={type:'human' as const,content:'resume'};

    await manager.sendIdempotent('owner:aki',message,operationRef);
    await manager.sendIdempotent('owner:aki',message,operationRef);
    await expect(manager.sendIdempotent('owner:aki',message,`operation_v1_${'b'.repeat(64)}`))
      .rejects.toThrow('distinct durable entry');

    expect(pi.send).toHaveBeenCalledTimes(1);
    expect(pi.steer).not.toHaveBeenCalled();
  });

  it('routes turn abort to the selected runtime without killing its session', async () => {
    const pi = createMockAdapter('pi');
    const abortTurn = vi.fn(async () => undefined);
    pi.abortTurn = abortTurn;
    const manager = createConversationRuntimeManager();
    manager.register(pi);

    await manager.spawn({
      tenantId: 'member-000:pi-canary',
      conversationRuntime: {
        runtime: 'pi',
        model: { provider: 'openrouter', model: 'openai/gpt-5.4-mini', reasoning: 'medium' },
        isolationProfile: 'internal-canary',
      },
      workspace: '/tmp/workspace',
      sessionId: 'pi-session-1',
    });
    await manager.abortTurn('member-000:pi-canary');

    expect(abortTurn).toHaveBeenCalledWith('member-000:pi-canary');
    expect(pi.kill).not.toHaveBeenCalled();
  });

  it('routes runtime: cc-sdk to the cc-sdk adapter, not the legacy cc pool adapter', async () => {
    const cc = createMockAdapter('cc');
    const ccSdk = createMockAdapter('cc-sdk');
    const manager = createConversationRuntimeManager();
    manager.register(cc);
    manager.register(ccSdk);

    const spawned = await manager.spawn({
      tenantId: 'member-000:sdktest',
      runtime: 'cc-sdk',
      workspace: '/tmp/workspace',
      sessionId: 'session-sdk',
    });
    await manager.send('member-000:sdktest', { type: 'human', content: 'hello sdk' });

    expect(spawned.runtime).toBe('cc-sdk');
    expect(ccSdk.spawn).toHaveBeenCalledOnce();
    expect(ccSdk.send).toHaveBeenCalledWith(
      'member-000:sdktest',
      { type: 'human', content: 'hello sdk' },
      expect.objectContaining({ correlationId: expect.stringMatching(/^turn:/), effect: 'no-effect' }),
    );
    // The legacy cc pool adapter must never be touched for a cc-sdk agent.
    expect(cc.spawn).not.toHaveBeenCalled();
    expect(cc.send).not.toHaveBeenCalled();
  });

  it('routes explicit cc runtime to the cc pool adapter, not the cc-sdk adapter', async () => {
    const cc = createMockAdapter('cc');
    const ccSdk = createMockAdapter('cc-sdk');
    const manager = createConversationRuntimeManager();
    manager.register(cc);
    manager.register(ccSdk);

    const spawned = await manager.spawn({
      tenantId: 'member-000:legacy',
      runtime: 'cc',
      workspace: '/tmp/workspace',
      sessionId: 'session-legacy',
    });

    expect(spawned.runtime).toBe('cc');
    expect(cc.spawn).toHaveBeenCalledOnce();
    expect(ccSdk.spawn).not.toHaveBeenCalled();
  });

  it('falls back from Codex spawn failure to CC and records the final runtime', async () => {
    const cc = createMockAdapter('cc');
    const codex = createMockAdapter('codex');
    vi.mocked(codex.spawn).mockRejectedValueOnce(new Error('spawn ENOENT CODEX_AUTH_TOKEN=secret-value'));
    const manager = createConversationRuntimeManager();
    manager.register(codex);
    manager.register(cc);
    const fallbackEvents: unknown[] = [];
    manager.onRuntimeFallback((event) => fallbackEvents.push(event));

    const spawned = await manager.spawn({
      tenantId: 'member-000:aki',
      conversationRuntimePreference: ['codex', 'cc'],
      workspace: '/tmp/workspace',
      sessionId: 'session-1',
    });

    expect(vi.mocked(codex.spawn).mock.invocationCallOrder[0]).toBeLessThan(vi.mocked(cc.spawn).mock.invocationCallOrder[0]);
    expect(spawned.runtime).toBe('cc');
    expect(manager.get('member-000:aki')?.runtime).toBe('cc');
    expect(fallbackEvents).toEqual([
      expect.objectContaining({
        tenantId: 'member-000:aki',
        from: 'codex',
        to: 'cc',
        attemptIndex: 1,
        reason: expect.not.stringContaining('secret-value'),
      }),
    ]);
  });

  it('drops a usage binding owned by the failed runtime when falling back', async () => {
    const cc = createMockAdapter('cc');
    const codex = createMockAdapter('codex');
    vi.mocked(codex.spawn).mockRejectedValueOnce(new Error('spawn failed'));
    const manager = createConversationRuntimeManager();
    manager.register(codex);
    manager.register(cc);
    const warn = vi.spyOn(console, 'warn').mockImplementation(() => {});
    const binding = buildRuntimeUsageBinding({ workItemId: 'wi_20261001_297', agentId: 'aki', phase: 'work_item', epoch: 1,
      runtime: 'codex', procurementSource: 'chatgpt_subscription' });
    await manager.spawn({ tenantId: 'member-000:aki', conversationRuntimePreference: ['codex', 'cc'],
      workspace: '/tmp/workspace', sessionId: 'session-1', computeCostBinding: binding });
    expect(vi.mocked(codex.spawn).mock.calls[0]![0].computeCostBinding).toEqual(binding);
    expect(vi.mocked(cc.spawn).mock.calls[0]![0].computeCostBinding).toBeUndefined();
    warn.mockRestore();
  });

  it('uses preference routing order even when AgentConfig.runtime differs', async () => {
    const cc = createMockAdapter('cc');
    const codex = createMockAdapter('codex');
    const manager = createConversationRuntimeManager();
    manager.register(cc);
    manager.register(codex);

    await manager.spawn({
      tenantId: 'member-000:aki',
      runtime: 'cc',
      conversationRuntimePreference: ['codex', 'cc'],
      workspace: '/tmp/workspace',
      sessionId: 'session-1',
    });

    expect(codex.spawn).toHaveBeenCalledOnce();
    expect(cc.spawn).not.toHaveBeenCalled();
    expect(manager.get('member-000:aki')?.runtime).toBe('codex');
  });

  it('routes YOLO mode state to the Codex adapter before a process exists', () => {
    const cc = createMockAdapter('cc');
    const codex = createMockAdapter('codex');
    const yoloModes = new Map<string, boolean>();
    codex.setYoloMode = vi.fn((tenantId: string, enabled: boolean) => {
      if (enabled) yoloModes.set(tenantId, true);
      else yoloModes.delete(tenantId);
    });
    codex.getYoloMode = vi.fn((tenantId: string) => yoloModes.get(tenantId) ?? false);
    const manager = createConversationRuntimeManager();
    manager.register(cc);
    manager.register(codex);

    manager.setYoloMode?.('member-000:aki', true);

    expect(codex.setYoloMode).toHaveBeenCalledWith('member-000:aki', true);
    expect(manager.getYoloMode?.('member-000:aki')).toBe(true);
    expect(cc.setYoloMode).toBeUndefined();
  });

  it('routes Codex YOLO defaults to the Codex adapter on spawn', async () => {
    const cc = createMockAdapter('cc');
    const codex = createMockAdapter('codex');
    codex.setYoloDefaultMode = vi.fn();
    const manager = createConversationRuntimeManager();
    manager.register(cc);
    manager.register(codex);

    await manager.spawn({
      tenantId: 'member-000:pulse',
      runtime: 'codex',
      workspace: '/tmp/workspace',
      sessionId: 'session-1',
      codexYoloDefault: true,
    });

    expect(codex.setYoloDefaultMode).toHaveBeenCalledWith('member-000:pulse', true);
    expect(cc.setYoloDefaultMode).toBeUndefined();
  });

  it('falls back during a turn when the active runtime errors before any assistant response', async () => {
    const cc = createMockAdapter('cc');
    const codex = createMockAdapter('codex');
    const manager = createConversationRuntimeManager();
    manager.register(cc);
    manager.register(codex);
    const fallbackEvents: unknown[] = [];
    const errors: unknown[] = [];
    manager.onRuntimeFallback((event) => fallbackEvents.push(event));
    manager.on('error', (_tenantId, error) => errors.push(error));

    await manager.spawn({
      tenantId: 'member-000:amanda',
      conversationRuntimePreference: ['cc', 'codex'],
      workspace: '/tmp/workspace',
      sessionId: 'session-1',
    });

    const message: StreamJsonMessage = { type: 'human', content: 'continue' };
    await manager.send('member-000:amanda', message);
    cc.events.emit('error', 'member-000:amanda', new Error('429 code=1310 Weekly/Monthly Limit Exhausted'));
    await new Promise((resolve) => setImmediate(resolve));

    expect(cc.kill).toHaveBeenCalledWith('member-000:amanda');
    expect(codex.spawn).toHaveBeenCalledOnce();
    expect(codex.send).toHaveBeenCalledWith(
      'member-000:amanda',
      message,
      expect.objectContaining({ correlationId: expect.stringMatching(/^turn:/), effect: 'no-effect' }),
    );
    expect(manager.get('member-000:amanda')?.runtime).toBe('codex');
    expect(fallbackEvents).toEqual([
      expect.objectContaining({
        tenantId: 'member-000:amanda',
        from: 'cc',
        to: 'codex',
        attemptIndex: 1,
        reason: expect.stringContaining('Weekly/Monthly Limit Exhausted'),
      }),
    ]);
    expect(errors).toEqual([]);

    const onResponse = vi.fn();
    const onExit = vi.fn();
    manager.on('response', onResponse);
    manager.on('exit', onExit);
    cc.events.emit('response', 'member-000:amanda', { type: 'assistant', content: 'stale cc answer' });
    cc.events.emit('exit', 'member-000:amanda', 1);
    expect(onResponse).not.toHaveBeenCalled();
    expect(onExit).not.toHaveBeenCalled();
  });

  it('falls back during a turn when the active runtime is rate-limited before any assistant response', async () => {
    const cc = createMockAdapter('cc');
    const codex = createMockAdapter('codex');
    const manager = createConversationRuntimeManager();
    manager.register(cc);
    manager.register(codex);
    const fallbackEvents: unknown[] = [];
    const sessionEnds: unknown[] = [];
    manager.onRuntimeFallback((event) => fallbackEvents.push(event));
    manager.on('session-end', (_tenantId, reason) => sessionEnds.push(reason));

    await manager.spawn({
      tenantId: 'member-000:amanda',
      conversationRuntimePreference: ['cc', 'codex'],
      workspace: '/tmp/workspace',
      sessionId: 'session-1',
    });

    const message: StreamJsonMessage = { type: 'human', content: 'continue' };
    await manager.send('member-000:amanda', message);
    cc.events.emit('response', 'member-000:amanda', {
      type: 'system',
      subtype: 'api_retry',
      error_status: 429,
      error: 'rate_limit',
    } as StreamJsonMessage);
    cc.events.emit('session-end', 'member-000:amanda', { type: 'rate-limited', retryCount: 1 } satisfies SessionEndReason);
    await new Promise((resolve) => setImmediate(resolve));

    expect(cc.kill).toHaveBeenCalledWith('member-000:amanda');
    expect(codex.spawn).toHaveBeenCalledOnce();
    expect(codex.send).toHaveBeenCalledWith(
      'member-000:amanda',
      message,
      expect.objectContaining({ correlationId: expect.stringMatching(/^turn:/), effect: 'no-effect' }),
    );
    expect(manager.get('member-000:amanda')?.runtime).toBe('codex');
    expect(fallbackEvents).toEqual([
      expect.objectContaining({
        tenantId: 'member-000:amanda',
        from: 'cc',
        to: 'codex',
        attemptIndex: 1,
        reason: expect.stringContaining('rate limited'),
      }),
    ]);
    expect(sessionEnds).toEqual([]);
  });

  it('reuses one turn identity when a read-only turn is replayed to a fallback runtime', async () => {
    const cc = createMockAdapter('cc');
    const codex = createMockAdapter('codex');
    const manager = createConversationRuntimeManager();
    manager.register(cc);
    manager.register(codex);
    await manager.spawn({
      tenantId: 'member-000:aki',
      conversationRuntimePreference: ['cc', 'codex'],
      workspace: '/tmp/workspace',
      sessionId: 'session-1',
    });

    const message: StreamJsonMessage = { type: 'human', content: 'inspect only' };
    await manager.send('member-000:aki', message);
    cc.events.emit('turn-effect', 'member-000:aki', 'read-only-effect');
    cc.events.emit('error', 'member-000:aki', new Error('read failed'));
    await new Promise((resolve) => setImmediate(resolve));

    const firstContext = vi.mocked(cc.send).mock.calls[0]?.[2];
    const replayContext = vi.mocked(codex.send).mock.calls[0]?.[2];
    expect(firstContext).toBeDefined();
    expect(replayContext).toBe(firstContext);
    expect(replayContext).toMatchObject({
      correlationId: firstContext?.correlationId,
      idempotencyKey: firstContext?.correlationId,
      effect: 'read-only-effect',
    });
  });

  it('keeps queued legacy turns correlated when an earlier result completes', async () => {
    const cc = createMockAdapter('cc');
    const codex = createMockAdapter('codex');
    const manager = createConversationRuntimeManager();
    manager.register(cc);
    manager.register(codex);
    await manager.spawn({
      tenantId: 'member-000:aki',
      conversationRuntimePreference: ['cc', 'codex'],
      workspace: '/tmp/workspace',
      sessionId: 'session-1',
    });

    await manager.send('member-000:aki', { type: 'human', content: 'first' });
    await manager.send('member-000:aki', { type: 'human', content: 'second' });
    const firstContext = vi.mocked(cc.send).mock.calls[0]?.[2];
    const secondContext = vi.mocked(cc.send).mock.calls[1]?.[2];
    expect(secondContext?.correlationId).not.toBe(firstContext?.correlationId);

    cc.events.emit('response', 'member-000:aki', { type: 'result', subtype: 'success' });
    cc.events.emit('error', 'member-000:aki', new Error('second turn failed before output'));
    await new Promise((resolve) => setImmediate(resolve));

    expect(codex.send).toHaveBeenCalledTimes(1);
    expect(codex.send).toHaveBeenCalledWith(
      'member-000:aki',
      { type: 'human', content: 'second' },
      secondContext,
    );
  });

  it('blocks recovery instead of replaying after an observed side effect', async () => {
    const cc = createMockAdapter('cc');
    const codex = createMockAdapter('codex');
    const manager = createConversationRuntimeManager();
    manager.register(cc);
    manager.register(codex);
    const sessionEnds: SessionEndReason[] = [];
    manager.on('session-end', (_tenantId, reason) => sessionEnds.push(reason));
    await manager.spawn({
      tenantId: 'member-000:aki',
      conversationRuntimePreference: ['cc', 'codex'],
      workspace: '/tmp/workspace',
      sessionId: 'session-1',
    });

    await manager.send('member-000:aki', { type: 'human', content: 'change a file' });
    cc.events.emit('turn-effect', 'member-000:aki', 'side-effect-observed');
    cc.events.emit('error', 'member-000:aki', new Error('failed after write SECRET=do-not-leak'));
    await new Promise((resolve) => setImmediate(resolve));
    cc.events.emit('session-end', 'member-000:aki', { type: 'runtime-failed', runtime: 'cc' } satisfies SessionEndReason);

    expect(codex.spawn).not.toHaveBeenCalled();
    expect(codex.send).not.toHaveBeenCalled();
    expect(sessionEnds).toEqual([
      expect.objectContaining({
        type: 'runtime-recovery-required',
        runtime: 'cc',
        effect: 'side-effect-observed',
        correlationId: expect.stringMatching(/^turn:/),
        reason: expect.not.stringContaining('do-not-leak'),
      }),
    ]);
  });

  it('steers a second Pi message into the active turn instead of rejecting it', async () => {
    const pi = createMockAdapter('pi');
    pi.steer = vi.fn(async () => undefined);
    const manager = createConversationRuntimeManager();
    manager.register(pi);
    await manager.spawn({
      tenantId: 'member-000:aki',
      workspace: '/tmp',
      sessionId: 's',
      conversationRuntime: {
        runtime: 'pi',
        model: { provider: 'anthropic', model: 'claude-sonnet-4-5', reasoning: 'high' },
        isolationProfile: 'canary',
      },
    });
    await manager.send('member-000:aki', { type: 'human', content: 'first' });

    await expect(manager.send('member-000:aki', { type: 'human', content: '补充信息' }))
      .resolves.toBeUndefined();
    expect(pi.steer).toHaveBeenCalledWith('member-000:aki', { type: 'human', content: '补充信息' });
    expect(pi.send).toHaveBeenCalledTimes(1);
    pi.events.emit('response', 'member-000:aki', { type: 'result', subtype: 'success' });
    await expect(manager.send('member-000:aki', { type: 'human', content: 'next' })).resolves.toBeUndefined();
  });

  it('fails closed when a persisted runtime binding does not match Pi resume routing', async () => {
    const pi = createMockAdapter('pi');
    const manager = createConversationRuntimeManager();
    manager.register(pi);
    await expect(manager.spawn({
      tenantId: 'member-000:pi-canary',
      workspace: '/tmp',
      sessionId: 'pi-session',
      resume: true,
      runtimeSessionBinding: { runtime: 'codex', sessionId: 'codex-thread' },
      conversationRuntime: {
        runtime: 'pi',
        model: { provider: 'anthropic', model: 'claude-sonnet-4-5', reasoning: 'high' },
        isolationProfile: 'pi-canary',
      },
    })).rejects.toThrow('Runtime session binding mismatch');
    expect(pi.spawn).not.toHaveBeenCalled();
  });

  it('does not fallback from a rate-limited session-end after an assistant response', async () => {
    const cc = createMockAdapter('cc');
    const codex = createMockAdapter('codex');
    const manager = createConversationRuntimeManager();
    manager.register(cc);
    manager.register(codex);
    const fallbackEvents: unknown[] = [];
    const sessionEnds: unknown[] = [];
    manager.onRuntimeFallback((event) => fallbackEvents.push(event));
    manager.on('session-end', (_tenantId, reason) => sessionEnds.push(reason));

    await manager.spawn({
      tenantId: 'member-000:amanda',
      conversationRuntimePreference: ['cc', 'codex'],
      workspace: '/tmp/workspace',
      sessionId: 'session-1',
    });

    await manager.send('member-000:amanda', { type: 'human', content: 'continue' });
    cc.events.emit('response', 'member-000:amanda', {
      type: 'assistant',
      message: { content: [{ type: 'text', text: 'partial answer' }] },
    } as StreamJsonMessage);
    cc.events.emit('session-end', 'member-000:amanda', { type: 'rate-limited', retryCount: 1 } satisfies SessionEndReason);
    await new Promise((resolve) => setImmediate(resolve));

    expect(codex.spawn).not.toHaveBeenCalled();
    expect(fallbackEvents).toEqual([]);
    expect(sessionEnds).toEqual([{ type: 'rate-limited', retryCount: 1 }]);
  });

  it('does not fallback after an assistant response has already been emitted', async () => {
    const cc = createMockAdapter('cc');
    const codex = createMockAdapter('codex');
    const manager = createConversationRuntimeManager();
    manager.register(codex);
    manager.register(cc);
    const fallbackEvents: unknown[] = [];
    manager.onRuntimeFallback((event) => fallbackEvents.push(event));
    manager.on('error', () => {});

    await manager.spawn({
      tenantId: 'member-000:aki',
      conversationRuntimePreference: ['codex', 'cc'],
      workspace: '/tmp/workspace',
      sessionId: 'session-1',
    });
    codex.events.emit('response', 'member-000:aki', { type: 'assistant', content: 'partial answer' });
    codex.events.emit('error', 'member-000:aki', new Error('turn failed after assistant'));
    await new Promise((resolve) => setImmediate(resolve));

    expect(cc.spawn).not.toHaveBeenCalled();
    expect(fallbackEvents).toEqual([]);
    expect(manager.get('member-000:aki')?.runtime).toBe('codex');
  });

  it('uses codex adapter when runtime is omitted', async () => {
    const codex = createMockAdapter('codex');
    const manager = createConversationRuntimeManager();
    manager.register(codex);

    await manager.spawn({
      tenantId: 'member-000:duke',
      workspace: '/tmp/workspace',
      sessionId: 'session-1',
    });

    expect(codex.spawn).toHaveBeenCalledOnce();
    expect(manager.get('member-000:duke')?.runtime).toBe('codex');
  });

  it('re-emits adapter events without changing event arguments', async () => {
    const codex = createMockAdapter('codex');
    const manager = createConversationRuntimeManager();
    manager.register(codex);
    const onResponse = vi.fn();
    const onSessionEnd = vi.fn();
    manager.on('response', onResponse);
    manager.on('session-end', onSessionEnd);

    const response: StreamJsonMessage = { type: 'assistant', content: 'ok' };
    const reason: SessionEndReason = { type: 'idle-reclaimed' };
    codex.events.emit('response', 'member-000:aki', response);
    codex.events.emit('session-end', 'member-000:aki', reason);

    expect(onResponse).toHaveBeenCalledWith('member-000:aki', response);
    expect(onSessionEnd).toHaveBeenCalledWith('member-000:aki', reason);
  });

  it('keeps control-plane turns observable internally but invisible to channel activity streams', async () => {
    const pi = createMockAdapter('pi');
    const manager = createConversationRuntimeManager();
    manager.register(pi);
    await manager.spawn({tenantId:'member-000:pulse',runtime:'pi',workspace:'/tmp/workspace',sessionId:'pi-control'});
    const accepted=vi.fn();const delta=vi.fn();const response=vi.fn();const warning=vi.fn();const exit=vi.fn();
    manager.on('turn-accepted',accepted);manager.on('stream-delta',delta);manager.on('response',response);
    manager.on('turn-warning',warning);manager.on('exit',exit);

    await manager.send('member-000:pulse',{type:'human',content:'checkpoint',delivery_visibility:'control'});
    pi.events.emit('turn-accepted','member-000:pulse');
    pi.events.emit('stream-delta','member-000:pulse','private checkpoint bytes','turn:private');
    pi.events.emit('turn-warning','member-000:pulse',80,100);
    pi.events.emit('exit','member-000:pulse',1);
    pi.events.emit('response','member-000:pulse',{type:'assistant',content:'EXECUTION_CHECKPOINT: v1\n{}'});
    pi.events.emit('response','member-000:pulse',{type:'result',subtype:'success'});

    expect(accepted).not.toHaveBeenCalled();
    expect(delta).not.toHaveBeenCalled();
    expect(warning).not.toHaveBeenCalled();
    expect(exit).toHaveBeenCalledWith('member-000:pulse',1,expect.objectContaining({suppressGenericRecoveryToast:true}));
    expect(response).toHaveBeenNthCalledWith(1,'member-000:pulse',expect.objectContaining({type:'assistant',delivery_visibility:'control'}));
    expect(response).toHaveBeenNthCalledWith(2,'member-000:pulse',expect.objectContaining({type:'result',delivery_visibility:'control'}));
  });

  it('throws when selected adapter is not registered', async () => {
    const manager = createConversationRuntimeManager();
    manager.register(createMockAdapter('cc'));

    await expect(manager.spawn({
      tenantId: 'member-000:aki',
      runtime: 'codex',
      workspace: '/tmp/workspace',
      sessionId: 'session-1',
    })).rejects.toThrow('Conversation runtime adapter not registered: codex');
  });

  it('wraps the existing CC pool as the default conversation adapter', async () => {
    const ccProcess = {
      tenantId: 'member-000:duke',
      process: {} as never,
      sessionId: 'cc-session',
      state: 'ready' as const,
      lastActivity: 100,
      spawnedAt: 50,
      restartCount: 0,
      restartWindowStart: 50,
    };
    const status: ProcessStatusInfo = {
      tenantId: 'member-000:duke',
      state: 'ready',
      pid: 123,
      lastActivity: 100,
      spawnedAt: 50,
      restartCount: 0,
    };
    const pool: Pool = {
      spawn: vi.fn(async (_config: SpawnConfig) => ccProcess),
      send: vi.fn(async () => {}),
      kill: vi.fn(async () => {}),
      killAll: vi.fn(async () => {}),
      get: vi.fn(() => ccProcess),
      getAll: vi.fn(() => [ccProcess]),
      getStatus: vi.fn(() => [status]),
      on: vi.fn(),
      emitShutdown: vi.fn(),
      dispose: vi.fn(),
    };

    const adapter = createPoolConversationAdapter(pool);
    const spawned = await adapter.spawn({
      tenantId: 'member-000:duke',
      workspace: '/tmp/workspace',
      sessionId: 'cc-session',
    });

    expect(adapter.id).toBe('cc');
    expect(spawned.runtime).toBe('cc');
    expect(adapter.get('member-000:duke')?.runtime).toBe('cc');
    expect(adapter.getStatus()[0]?.runtime).toBe('cc');
  });
});

function restoreEnvVar(key: string, previous: string | undefined): void {
  if (previous === undefined) delete process.env[key];
  else process.env[key] = previous;
}

describe('context auto-compact injection', () => {
  function ccResultWithContextWindow(usedPct: number, windowSize = 200_000): StreamJsonMessage {
    return {
      type: 'result',
      context_window: { used_percentage: usedPct, context_window_size: windowSize },
    } as unknown as StreamJsonMessage;
  }

  it('injects /compact once when used_percentage reaches 80', async () => {
    const cc = createMockAdapter('cc');
    const manager = createConversationRuntimeManager();
    manager.register(cc);
    await manager.spawn({ tenantId: 't', runtime: 'cc', workspace: '/tmp', sessionId: 's' });

    cc.events.emit('response', 't', ccResultWithContextWindow(79));
    expect(cc.sentMessages).toHaveLength(0);

    cc.events.emit('response', 't', ccResultWithContextWindow(80));
    expect(cc.sentMessages).toHaveLength(1);
    expect(cc.sentMessages[0]).toMatchObject({ type: 'human', content: '/compact' });
  });

  it('does not inject twice for the same session', async () => {
    const cc = createMockAdapter('cc');
    const manager = createConversationRuntimeManager();
    manager.register(cc);
    await manager.spawn({ tenantId: 't', runtime: 'cc', workspace: '/tmp', sessionId: 's' });

    cc.events.emit('response', 't', ccResultWithContextWindow(85));
    cc.events.emit('response', 't', ccResultWithContextWindow(90));
    cc.events.emit('response', 't', ccResultWithContextWindow(100));

    expect(cc.sentMessages).toHaveLength(1);
  });

  it('re-injects after kill clears the dedup key', async () => {
    const cc = createMockAdapter('cc');
    const manager = createConversationRuntimeManager();
    manager.register(cc);
    await manager.spawn({ tenantId: 't', runtime: 'cc', workspace: '/tmp', sessionId: 's-1' });

    cc.events.emit('response', 't', ccResultWithContextWindow(85));
    expect(cc.sentMessages).toHaveLength(1);

    await manager.kill('t');
    await manager.spawn({ tenantId: 't', runtime: 'cc', workspace: '/tmp', sessionId: 's-2' });

    cc.events.emit('response', 't', ccResultWithContextWindow(85));
    expect(cc.sentMessages).toHaveLength(2);
  });

  it('does not inject for codex adapter', async () => {
    const cc = createMockAdapter('cc');
    const codex = createMockAdapter('codex');
    const manager = createConversationRuntimeManager();
    manager.register(cc);
    manager.register(codex);
    await manager.spawn({ tenantId: 't', workspace: '/tmp', sessionId: 's', conversationRuntimePreference: ['codex'] });

    codex.events.emit('response', 't', ccResultWithContextWindow(100));
    expect(codex.sentMessages).toHaveLength(0);
  });

  it('ignores result messages without context_window field', async () => {
    const cc = createMockAdapter('cc');
    const manager = createConversationRuntimeManager();
    manager.register(cc);
    await manager.spawn({ tenantId: 't', runtime: 'cc', workspace: '/tmp', sessionId: 's' });

    cc.events.emit('response', 't', { type: 'assistant', content: 'hi' } as StreamJsonMessage);
    cc.events.emit('response', 't', { type: 'result' } as StreamJsonMessage);
    cc.events.emit('response', 't', { type: 'result', context_window: {} } as unknown as StreamJsonMessage);

    expect(cc.sentMessages).toHaveLength(0);
  });
});

describe('ComputeCost producer integration',()=>{
  it('keeps active telemetry after the late cache expires and never delivers A late output to B',async()=>{
    vi.useFakeTimers();
    let manager: ReturnType<typeof createConversationRuntimeManager> | undefined;
    try {
      const cc=createMockAdapter('cc');let seq=0;
      const begin=vi.fn(()=>({ref:`attempt-${++seq}`,finished:false}) as any);
      const capture={begin,finish:vi.fn(),response:vi.fn(),gap:vi.fn(),human:vi.fn(),linkHuman:vi.fn()};
      manager=createConversationRuntimeManager({collaborationCapture:capture});manager.register(cc);
      const delivered=vi.fn();manager.on('response',delivered);
      await manager.spawn({tenantId:'t',runtime:'cc',workspace:'/synthetic',sessionId:'s'});
      await manager.send('t',{type:'human',content:'A'});
      const a=begin.mock.results.at(-1)!.value;
      await vi.advanceTimersByTimeAsync(6*60_000);
      const frame={type:'usage_observation',host_attempt_ref:a.ref,message:{id:'m',usage:{input_tokens:1}}};
      cc.events.emit('response','t',frame);
      expect(capture.response).toHaveBeenLastCalledWith(a,frame);
      cc.events.emit('turn-replay','t');
      const replay=begin.mock.results.at(-1)!.value;
      cc.events.emit('response','t',{...frame,host_usage_only:true});
      expect(capture.response).toHaveBeenLastCalledWith(a,{...frame,host_usage_only:true});
      cc.events.emit('response','t',{type:'result',host_attempt_ref:replay.ref});
      await manager.send('t',{type:'human',content:'B'});
      const b=begin.mock.results.at(-1)!.value;
      delivered.mockClear();
      const late={type:'result',host_attempt_ref:a.ref,host_usage_only:true,usage:{input_tokens:2}};
      cc.events.emit('response','t',late);
      expect(capture.response).toHaveBeenLastCalledWith(a,late);
      expect(delivered).not.toHaveBeenCalled();
      const resultB={type:'result',host_attempt_ref:b.ref,usage:{input_tokens:3}};
      cc.events.emit('response','t',resultB);
      expect(capture.response).toHaveBeenLastCalledWith(b,resultB);
    } finally { manager?.dispose();vi.useRealTimers(); }
  });
  it('captures a runtime-notification turn under its parent Relay dispatch',async()=>{
    const cc=createMockAdapter('cc');let seq=0;
    const begin=vi.fn(()=>({ref:`attempt-${++seq}`,finished:false}) as any);
    const capture={begin,finish:vi.fn(),response:vi.fn(),gap:vi.fn(),human:vi.fn(),linkHuman:vi.fn()};
    const manager=createConversationRuntimeManager({collaborationCapture:capture});manager.register(cc);
    try {
      // Background work woke the runtime before any Relay dispatch: nothing to declare.
      await manager.spawn({tenantId:'t',runtime:'cc',workspace:'/synthetic',sessionId:'s'});
      begin.mockClear();
      cc.events.emit('turn-accepted','t',{origin:'runtime-notification'});
      expect(begin).not.toHaveBeenCalled();
      await manager.send('t',{type:'human',content:'A'});
      const dispatched=begin.mock.results.at(-1)!.value;
      cc.events.emit('response','t',{type:'result',host_attempt_ref:dispatched.ref});
      cc.events.emit('turn-accepted','t',{origin:'runtime-notification'});
      expect(begin).toHaveBeenLastCalledWith('cc',expect.objectContaining({tenantId:'t'}),'runtime-notification',dispatched.ref);
      const notification=begin.mock.results.at(-1)!.value;
      const assistant={type:'assistant',turn_origin:'runtime-notification',message:{id:'m',usage:{input_tokens:1}}};
      cc.events.emit('response','t',assistant);
      expect(capture.response).toHaveBeenLastCalledWith(notification,assistant);
      const result={type:'result',turn_origin:'runtime-notification'};
      cc.events.emit('response','t',result);
      expect(capture.response).toHaveBeenLastCalledWith(notification,result);
      expect(capture.gap).not.toHaveBeenCalled();
      // The next notification turn gets a new attempt; the settled one is not reused.
      cc.events.emit('turn-accepted','t',{origin:'runtime-notification'});
      expect(begin.mock.results.at(-1)!.value).not.toBe(notification);
    } finally { manager.dispose(); }
  });
  it('starts a notification attempt at its first frame and ignores the stale Relay ref it carries',async()=>{
    const cc=createMockAdapter('cc');let seq=0;
    const begin=vi.fn(()=>({ref:`attempt-${++seq}`,finished:false}) as any);
    const capture={begin,finish:vi.fn(),response:vi.fn(),gap:vi.fn(),human:vi.fn(),linkHuman:vi.fn()};
    const manager=createConversationRuntimeManager({collaborationCapture:capture});manager.register(cc);
    try {
      await manager.spawn({tenantId:'t',runtime:'cc',workspace:'/synthetic',sessionId:'s'});
      await manager.send('t',{type:'human',content:'A'});
      const dispatched=begin.mock.results.at(-1)!.value;
      cc.events.emit('response','t',{type:'result',host_attempt_ref:dispatched.ref});
      // A tool step precedes the announcement; every frame carries A's ref.
      const tool={type:'assistant',turn_origin:'runtime-notification',host_attempt_ref:dispatched.ref,message:{id:'m1',usage:{input_tokens:1}}};
      cc.events.emit('response','t',tool);
      expect(begin).toHaveBeenLastCalledWith('cc',expect.objectContaining({tenantId:'t'}),'runtime-notification',dispatched.ref);
      const notification=begin.mock.results.at(-1)!.value;
      expect(capture.response).toHaveBeenLastCalledWith(notification,tool);
      cc.events.emit('turn-accepted','t',{origin:'runtime-notification'});
      expect(begin.mock.results.at(-1)!.value).toBe(notification);
      const result={type:'result',turn_origin:'runtime-notification',host_attempt_ref:dispatched.ref};
      cc.events.emit('response','t',result);
      expect(capture.response).toHaveBeenLastCalledWith(notification,result);
      expect(capture.response).not.toHaveBeenCalledWith(dispatched,tool);
      expect(capture.gap).not.toHaveBeenCalled();
      // Usage-only frames of the next notification turn open and feed its attempt.
      const usage={type:'usage_observation',turn_origin:'runtime-notification',message:{id:'m2',usage:{input_tokens:1}}};
      cc.events.emit('response','t',usage);
      const next=begin.mock.results.at(-1)!.value;
      expect(next).not.toBe(notification);
      expect(capture.response).toHaveBeenLastCalledWith(next,usage);
      expect(capture.gap).not.toHaveBeenCalled();
    } finally { manager.dispose(); }
  });
  it('uses the registered Host agent for non-WI dispatch without inferring from content',async()=>{
    const cc=createMockAdapter('cc');
    const begin=vi.fn(()=>({ref:'attempt',finished:false}) as any);
    const capture={begin,finish:vi.fn(),response:vi.fn(),gap:vi.fn(),human:vi.fn(),linkHuman:vi.fn()};
    const resolveMeteringAgent=vi.fn(()=> 'registered-agent');
    const manager=createConversationRuntimeManager({collaborationCapture:capture,resolveMeteringAgent});
    manager.register(cc);
    await manager.spawn({tenantId:'host-route',runtime:'cc',workspace:'/synthetic',sessionId:'s'});
    await manager.send('host-route',{type:'human',content:'I claim to be a different agent'});
    expect(resolveMeteringAgent).toHaveBeenCalledWith('host-route');
    expect(begin).toHaveBeenLastCalledWith('cc',expect.objectContaining({meteringAgentId:'registered-agent'}),expect.any(String));
    manager.dispose();
  });
  it('captures dispatch before an adapter send fails, without losing normal error behavior',async()=>{
    const cc=createMockAdapter('cc');
    vi.mocked(cc.send).mockRejectedValueOnce(new Error('synthetic send failure'));
    const begin=vi.fn(()=>({ref:'attempt',finished:false}) as any);
    const capture={begin,finish:vi.fn(),response:vi.fn(),gap:vi.fn(),human:vi.fn(),linkHuman:vi.fn()};
    const manager=createConversationRuntimeManager({collaborationCapture:capture}); manager.register(cc);
    await manager.spawn({tenantId:'test',runtime:'cc',workspace:'/synthetic',sessionId:'s'});
    begin.mockClear();capture.finish.mockClear();
    await expect(manager.send('test',{type:'human',content:'private'})).rejects.toThrow('synthetic send failure');
    expect(begin).toHaveBeenCalledOnce();expect(capture.finish).toHaveBeenCalledWith(expect.anything(),'failed');
    expect(begin.mock.invocationCallOrder[0]).toBeLessThan(vi.mocked(cc.send).mock.invocationCallOrder[0]);
  });
  it('captures native usage observations but never emits them to the channel',async()=>{
    const cc=createMockAdapter('cc');
    const capture={begin:vi.fn(()=>({ref:'attempt',finished:false,nativeSession:'s'}) as any),finish:vi.fn(),response:vi.fn(),gap:vi.fn(),human:vi.fn(),linkHuman:vi.fn()};
    const manager=createConversationRuntimeManager({collaborationCapture:capture});manager.register(cc);
    const displayed=vi.fn();manager.on('response',displayed);
    await manager.spawn({tenantId:'test',runtime:'cc',workspace:'/synthetic',sessionId:'s'});
    await manager.send('test',{type:'human',content:'private'});
    cc.events.emit('response','test',{type:'usage_observation',session_id:'s',host_attempt_ref:'attempt',message:{usage:{input_tokens:2}}});
    expect(capture.response).toHaveBeenCalledOnce();expect(displayed).not.toHaveBeenCalled();
    cc.events.emit('response','test',{type:'result'});expect(displayed).toHaveBeenCalledOnce();
  });
  it('routes queued same-session CC telemetry by explicit attempt ref',async()=>{
    const cc=createMockAdapter('cc');
    const first={ref:'first',finished:false,nativeSession:'s'} as any, second={ref:'second',finished:false,nativeSession:'s'} as any;
    const capture={begin:vi.fn(),finish:vi.fn(),response:vi.fn(),gap:vi.fn(),human:vi.fn(),linkHuman:vi.fn()};
    // spawn has its own entry receipt; A/B are the next two model attempts.
    capture.begin.mockReturnValueOnce({ref:'entry',finished:false} as any).mockReturnValueOnce(first).mockReturnValueOnce(second);
    const manager=createConversationRuntimeManager({collaborationCapture:capture});manager.register(cc);
    await manager.spawn({tenantId:'test',runtime:'cc',workspace:'/synthetic',sessionId:'s'});
    await manager.send('test',{type:'human',content:'A'}); await manager.send('test',{type:'human',content:'B'});
    cc.events.emit('response','test',{type:'result'});
    cc.events.emit('response','test',{type:'usage_observation',session_id:'s',host_attempt_ref:'second',message:{id:'b',usage:{input_tokens:2}}});
    cc.events.emit('response','test',{type:'usage_observation',session_id:'s',host_attempt_ref:'first',message:{id:'a',usage:{input_tokens:1}}});
    expect(capture.response).toHaveBeenCalledWith(second,expect.objectContaining({host_attempt_ref:'second'}));
    expect(capture.response).toHaveBeenCalledWith(first,expect.objectContaining({host_attempt_ref:'first'}));
    expect(capture.gap).not.toHaveBeenCalled();
  });
  it('records a failed launch as an entry attempt rather than an imaginary model request',async()=>{
    const cc=createMockAdapter('cc');vi.mocked(cc.spawn).mockRejectedValueOnce(new Error('spawn denied'));
    const capture={begin:vi.fn(()=>({ref:'entry',finished:false}) as any),finish:vi.fn(),response:vi.fn(),gap:vi.fn(),human:vi.fn(),linkHuman:vi.fn()};
    const manager=createConversationRuntimeManager({collaborationCapture:capture});manager.register(cc);
    await expect(manager.spawn({tenantId:'test',runtime:'cc',workspace:'/synthetic',sessionId:'s'})).rejects.toThrow('spawn denied');
    expect(capture.begin).toHaveBeenCalledWith('cc',expect.anything(),'spawn',undefined,false);
    expect(capture.finish).toHaveBeenCalledWith(expect.anything(),'failed');
  });
  it('isolates late cancellation settlement when A/B have no native correlation',async()=>{
    const cc=createMockAdapter('cc');cc.abortTurn=vi.fn(async()=>undefined);
    const first={ref:'first',finished:false} as any;
    const capture={begin:vi.fn(()=>({ref:'entry',finished:false}) as any),finish:vi.fn(),response:vi.fn(),gap:vi.fn(),human:vi.fn(),linkHuman:vi.fn()};
    const manager=createConversationRuntimeManager({collaborationCapture:capture});manager.register(cc);
    await manager.spawn({tenantId:'test',runtime:'cc',workspace:'/synthetic',sessionId:'s'});
    capture.begin.mockReturnValue(first);
    await manager.send('test',{type:'human',content:'A'}); await manager.abortTurn('test');
    await manager.send('test',{type:'human',content:'B'});
    cc.events.emit('response','test',{type:'usage_observation',message:{id:'a',usage:{input_tokens:2}}});
    expect(capture.response).not.toHaveBeenCalled();
    expect(capture.gap).toHaveBeenCalledWith(undefined,'unpaired_result');
  });
  it('isolates late usage after A result rather than assigning it to queued B',async()=>{
    const cc=createMockAdapter('cc');
    const first={ref:'first',finished:true,nativeSession:'s'} as any, second={ref:'second',finished:false,nativeSession:'s'} as any;
    const capture={begin:vi.fn(()=>({ref:'entry',finished:false}) as any),finish:vi.fn(),response:vi.fn(),gap:vi.fn(),human:vi.fn(),linkHuman:vi.fn()};
    const manager=createConversationRuntimeManager({collaborationCapture:capture});manager.register(cc);
    await manager.spawn({tenantId:'test',runtime:'cc',workspace:'/synthetic',sessionId:'s'});
    capture.begin.mockReturnValueOnce(first).mockReturnValueOnce(second);
    await manager.send('test',{type:'human',content:'A'});await manager.send('test',{type:'human',content:'B'});
    cc.events.emit('response','test',{type:'result'});
    cc.events.emit('response','test',{type:'usage_observation',session_id:'s',message:{id:'late-a',usage:{input_tokens:2}}});
    expect(capture.response).not.toHaveBeenCalledWith(second,expect.anything());
    expect(capture.gap).toHaveBeenCalledWith(undefined,'unpaired_result');
  });
  it('keeps the same isolation after B has started',async()=>{
    const cc=createMockAdapter('cc');
    const first={ref:'first',finished:true,nativeSession:'s'} as any, second={ref:'second',finished:false,nativeSession:'s'} as any;
    const capture={begin:vi.fn(()=>({ref:'entry',finished:false}) as any),finish:vi.fn(),response:vi.fn(),gap:vi.fn(),human:vi.fn(),linkHuman:vi.fn()};
    const manager=createConversationRuntimeManager({collaborationCapture:capture});manager.register(cc);
    await manager.spawn({tenantId:'test',runtime:'cc',workspace:'/synthetic',sessionId:'s'});
    capture.begin.mockReturnValueOnce(first).mockReturnValueOnce(second);
    await manager.send('test',{type:'human',content:'A'});await manager.send('test',{type:'human',content:'B'});
    cc.events.emit('response','test',{type:'result'});
    cc.events.emit('response','test',{type:'assistant',session_id:'s',message:{content:[]}});
    cc.events.emit('response','test',{type:'usage_observation',session_id:'s',message:{id:'late-a',usage:{input_tokens:2}}});
    expect(capture.response).not.toHaveBeenCalledWith(second,expect.objectContaining({type:'usage_observation'}));
    expect(capture.gap).toHaveBeenCalledWith(undefined,'unpaired_result');
  });
  it('creates a new Host attempt when cc-sdk transparently replays a stalled turn',async()=>{
    const sdk=createMockAdapter('cc-sdk');
    const first={ref:'first',finished:false} as any, replay={ref:'replay',finished:false} as any;
    const capture={begin:vi.fn(()=>({ref:'entry',finished:false}) as any),finish:vi.fn(),response:vi.fn(),gap:vi.fn(),human:vi.fn(),linkHuman:vi.fn()};
    const manager=createConversationRuntimeManager({collaborationCapture:capture});manager.register(sdk);
    await manager.spawn({tenantId:'test',runtime:'cc-sdk',workspace:'/synthetic',sessionId:'s'});
    capture.begin.mockReturnValueOnce(first).mockReturnValueOnce(replay);
    await manager.send('test',{type:'human',content:'A'}); sdk.events.emit('turn-replay','test');
    expect(capture.finish).toHaveBeenCalledWith(first,'failed');
    expect(capture.begin).toHaveBeenLastCalledWith('cc-sdk',expect.anything(),expect.any(String),'first');
  });
  it('does not fail queued Codex B when Codex A emits its normal exit(0)',async()=>{
    const codex=createMockAdapter('codex');
    const capture={begin:vi.fn(()=>({ref:'turn',finished:false}) as any),finish:vi.fn(),response:vi.fn(),gap:vi.fn(),human:vi.fn(),linkHuman:vi.fn()};
    const manager=createConversationRuntimeManager({collaborationCapture:capture});manager.register(codex);
    await manager.spawn({tenantId:'test',runtime:'codex',workspace:'/synthetic',sessionId:'s'});
    await manager.send('test',{type:'human',content:'A'});await manager.send('test',{type:'human',content:'B'});
    codex.events.emit('response','test',{type:'result'});codex.events.emit('exit','test',0);
    expect(capture.finish).not.toHaveBeenCalledWith(expect.anything(),'failed');
  });
});

describe('exact human message and native usage identity',()=>{
  it('uses the sent object proof even when a newer input arrived before dispatch',async()=>{
    const {bindHumanMessageProof} = await import('../src/collaboration-capture.js');
    const cc=createMockAdapter('cc');
    const capture={begin:vi.fn(()=>({ref:'attempt',finished:false}) as any),finish:vi.fn(),response:vi.fn(),gap:vi.fn(),human:vi.fn(),linkHuman:vi.fn()};
    const manager=createConversationRuntimeManager({collaborationCapture:capture});manager.register(cc);
    await manager.spawn({tenantId:'test',runtime:'cc',workspace:'/synthetic',sessionId:'logical-session'});
    const first={type:'human' as const,content:'first'},second={type:'human' as const,content:'second'};
    const a={ref:'a'.repeat(64),observedAtMs:1_789_261_200_000},b={ref:'b'.repeat(64),observedAtMs:1_789_261_200_100};
    bindHumanMessageProof(first,a);bindHumanMessageProof(second,b);
    await manager.send('test',first);await manager.send('test',second);
    expect(capture.linkHuman.mock.calls.map(c=>c[1])).toEqual([a,b]);
    // A fresh native session can differ from the configured logical session.
    expect(capture.begin).toHaveBeenLastCalledWith('cc',expect.objectContaining({sessionId:'cc-session'}),expect.any(String));
    await manager.killAll();manager.dispose();
  });
});
