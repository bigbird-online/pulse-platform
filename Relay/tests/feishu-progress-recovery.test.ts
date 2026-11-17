import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { EventEmitter } from 'node:events';
import { mkdtempSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { createFeishuStreamingSink, wirePoolEvents } from '../src/channels/feishu.js';
import { resolveRichDocChannelProfiles } from '../src/channel-rich-rendering.js';
import { TURN_ACCEPTED_STATUS_TEXT, TurnActivityCounter } from '../src/channel-rendering.js';
import { FeishuStreamingCardSession } from '../src/feishu-streaming-card.js';
import { RUNTIME_NOTIFICATION_TURN, tagRuntimeNotificationTurn } from '../src/runtime-background-work.js';
import type { AgentConfig } from '../src/types.js';
import type { CommandRuntime } from '../src/commands.js';
import { createRouter, type Router } from '../src/router.js';
import { latestHumanInput, recordHumanInput } from '../src/scl-human-decision.js';

const key = 'eric:pulse';
const openId = 'ou_progress_test';
const agent: AgentConfig = {
  id: 'pulse', name: 'Pulse', emoji: '⚡️', channel: 'feishu',
  workspace: '/tmp/relay-progress-test', sessionId: 'progress-test',
  feishu: { appId: 'progress-recovery-test', appSecret: 'test-only' },
};
const progress = (step: number) => `⏳ 仍在工作，已推进 ${step} 个执行步骤…`;
const ok = (body: object = { code: 0 }) => new Response(JSON.stringify(body), { status: 200 });
type Failure = 'timeout' | 'closed' | '503' | 'network';

function transport(failure?: Failure) {
  const visible: string[] = [];
  const requests: Array<{ sequence: number; content: string; kind: string }> = [];
  const staticAnswers: Record<string, unknown>[] = [];
  let failed = false;
  let expired = false;
  const create = vi.fn(async () => ({ code: 0, data: { message_id: 'progress-message' } }));
  const sendCard = vi.fn(async (_id: string, card: Record<string, unknown>) => {
    staticAnswers.push(card);
    return true;
  });
  const fetchMock = vi.fn(async (input: string | URL | Request, init?: RequestInit) => {
    const url = String(input);
    if (url.includes('/auth/')) return ok({ code: 0, tenant_access_token: 'test-token', expire: 7200 });
    const body = JSON.parse(String(init?.body ?? '{}'));
    if (url.endsWith('/cards') && init?.method === 'POST') {
      visible.push(JSON.parse(body.data).body.elements[0].content);
      return ok({ code: 0, data: { card_id: 'progress-card' } });
    }
    if (url.endsWith('/elements/content/content')) {
      requests.push({ sequence: body.sequence, content: body.content, kind: 'stream' });
      if (expired) return ok({ code: 300300, msg: 'streaming mode is closed; ' });
      if (!failed && failure && body.content.includes(progress(10))) {
        failed = true;
        if (failure === '503') return new Response('temporary', { status: 503 });
        if (failure === 'network') throw new TypeError('fetch failed');
        expired = true;
        return ok({ code: 300300, msg: failure === 'timeout' ? 'card streaming timeout; ' : 'streaming mode is closed; ' });
      }
      visible.push(body.content);
      return ok();
    }
    if (url.endsWith('/cards/progress-card') && init?.method === 'PUT') {
      const card = JSON.parse(body.card.data);
      expect(card.config.streaming_mode).toBe(false);
      const content = card.body.elements[0].content;
      requests.push({ sequence: body.sequence, content, kind: 'replace' });
      visible.push(content);
      return ok();
    }
    if (url.endsWith('/settings')) return ok();
    throw new Error(`Unexpected test endpoint: ${url}`);
  });
  vi.stubGlobal('fetch', fetchMock);
  return { visible, requests, staticAnswers, create, sendCard, fetchMock, client: { im: { message: { create } } } };
}

beforeEach(() => {
  vi.useFakeTimers();
  vi.stubEnv('RELAY_FEISHU_STREAMING_CARD', 'true');
  vi.stubEnv('RELAY_OBS_ENABLED', 'false');
  vi.setSystemTime(new Date('2026-09-14T00:00:00Z'));
});
afterEach(() => {
  vi.clearAllTimers();
  vi.useRealTimers();
  vi.unstubAllGlobals();
  vi.unstubAllEnvs();
});

describe.each(['off', 'on'] as const)('Feishu progress recovery, RichDoc=%s', (mode) => {
  it('delivers a runtime-notification turn that arrives after the previous turn finalized', async () => {
    const io = transport();
    const profile = resolveRichDocChannelProfiles({ RELAY_FEISHU_RICHDOC_MODE: mode }).feishu;
    const sink = createFeishuStreamingSink(agent, io.client as any, io.sendCard, undefined, { richDocProfile: profile })!;
    const pool = new EventEmitter();
    wirePoolEvents(agent, pool as unknown as CommandRuntime, new Map([[key, openId]]), {} as Router,
      vi.fn(), io.sendCard, undefined, sink);
    const delivered = () => JSON.stringify([io.visible, io.staticAnswers]);
    const answer = (text: string) => ({ type: 'assistant', message: { stop_reason: 'end_turn', content: [{ type: 'text', text }] } });
    pool.emit('turn-accepted', key);
    pool.emit('response', key, answer('后台构建已启动。'));
    pool.emit('response', key, { type: 'result', result: '' });
    await vi.advanceTimersByTimeAsync(5000);
    expect(delivered()).toContain('后台构建已启动。');

    // CC wakes on the finished background task with no Relay send.
    pool.emit('turn-accepted', key, RUNTIME_NOTIFICATION_TURN);
    pool.emit('response', key, tagRuntimeNotificationTurn(answer('后台构建完成：42 项测试通过。')));
    pool.emit('response', key, tagRuntimeNotificationTurn({ type: 'result', result: '' }));
    await vi.advanceTimersByTimeAsync(5000);
    expect(delivered()).toContain('后台构建完成：42 项测试通过。');
  });

  it.each(['timeout', 'closed', '503', 'network'] as const)('continues 5→10→40 after %s without creating prefix copies', async (failure) => {
    const io = transport(failure);
    const profile = resolveRichDocChannelProfiles({ RELAY_FEISHU_RICHDOC_MODE: mode }).feishu;
    const sink = createFeishuStreamingSink(agent, io.client as any, io.sendCard, undefined, { richDocProfile: profile })!;
    const pool = new EventEmitter();
    wirePoolEvents(agent, pool as unknown as CommandRuntime, new Map([[key, openId]]), {} as Router,
      vi.fn(), io.sendCard, undefined, sink);
    // Exercise the production accepted-turn counter AND channel sink, not an extracted copy.
    const drain = () => sink.onAssistant(key, openId, '', 'snapshot');
    pool.emit('turn-accepted', key);
    await drain();
    for (let step = 1; step <= 40; step += 1) {
      pool.emit('turn-effect', key, 'read-only-effect');
      await drain();
      await vi.advanceTimersByTimeAsync(1600);
      if (step === 5) expect(io.visible.at(-1)).toContain(progress(5));
    }
    expect(io.visible.at(-1)).toContain(progress(40));
    expect(io.create).toHaveBeenCalledTimes(1);
    expect(io.staticAnswers).toHaveLength(0);
    const seq = io.requests.map((r) => r.sequence);
    expect(seq.every((n, i) => i === 0 || n > seq[i - 1])).toBe(true);
    await sink.onAssistant(key, openId, '最终业务答案。', 'final', 'final-1');
    await sink.onResult(key);
    await sink.onResult(key);
    expect(io.visible.at(-1)).toContain('最终业务答案。');
    expect(io.visible.at(-1)).toContain('✅ 本轮回复已生成');
    expect(io.visible.at(-1)).not.toContain('执行步骤');
    expect(io.staticAnswers).toHaveLength(0);
    const count = io.fetchMock.mock.calls.length;
    await vi.advanceTimersByTimeAsync(180_000);
    expect(io.fetchMock).toHaveBeenCalledTimes(count);
  });

  it('coalesces a burst to the latest non-milestone count, without waiting for more events', async () => {
    const io = transport();
    const profile = resolveRichDocChannelProfiles({ RELAY_FEISHU_RICHDOC_MODE: mode }).feishu;
    const sink = createFeishuStreamingSink(agent, io.client as any, io.sendCard, undefined, { richDocProfile: profile })!;
    sink.beginTurn(key);
    await sink.onAssistant(key, openId, TURN_ACCEPTED_STATUS_TEXT, 'status');
    for (let step = 1; step <= 39; step += 1) {
      await sink.onAssistant(key, openId, progress(step), 'activity', `burst-${step}`);
    }
    await vi.advanceTimersByTimeAsync(1600);
    expect(io.visible.at(-1)).toBe(progress(39));
    expect(io.requests).toHaveLength(2); // First activity and latest coalesced activity.
    await sink.onAssistant(key, openId, '完成。', 'final');
    await sink.onResult(key);
  });

  it('retries a rejected latest frame even when no more runtime events arrive', async () => {
    const io = transport('503');
    const profile = resolveRichDocChannelProfiles({ RELAY_FEISHU_RICHDOC_MODE: mode }).feishu;
    const sink = createFeishuStreamingSink(agent, io.client as any, io.sendCard, undefined, { richDocProfile: profile })!;
    sink.beginTurn(key);
    await sink.onAssistant(key, openId, TURN_ACCEPTED_STATUS_TEXT, 'status');
    await sink.onAssistant(key, openId, progress(5), 'activity');
    await vi.advanceTimersByTimeAsync(1600);
    await sink.onAssistant(key, openId, progress(10), 'activity');
    await vi.advanceTimersByTimeAsync(10_000);
    expect(io.visible.at(-1)).toContain(progress(10));
    expect(io.create).toHaveBeenCalledTimes(1);
    await sink.onAssistant(key, openId, '完成。', 'final');
    await sink.onResult(key);
  });

  it.skipIf(mode === 'off')('retries a failed final content PUT on the same card after progress backoff', async () => {
    const io = transport('503');
    const original = global.fetch;
    let finalAttempts = 0;
    vi.stubGlobal('fetch', async (input: Parameters<typeof fetch>[0], init?: RequestInit) => {
      const body = String(init?.body ?? '');
      if (String(input).includes('/elements/content/content') && body.includes('最终业务答案。')) {
        finalAttempts += 1;
        if (finalAttempts === 1) throw new TypeError('final content acknowledgement lost');
      }
      return original(input, init);
    });
    const profile = resolveRichDocChannelProfiles({ RELAY_FEISHU_RICHDOC_MODE: mode }).feishu;
    const sink = createFeishuStreamingSink(agent, io.client as any, io.sendCard, undefined, { richDocProfile: profile })!;
    sink.beginTurn(key);
    await sink.onAssistant(key, openId, TURN_ACCEPTED_STATUS_TEXT, 'status');
    await sink.onAssistant(key, openId, progress(10), 'activity'); // leaves the bounded progress retry pending
    await sink.onAssistant(key, openId, '最终业务答案。', 'final');
    await sink.onResult(key);
    expect(finalAttempts).toBe(2);
    expect(io.create).toHaveBeenCalledTimes(1);
    expect(io.staticAnswers).toHaveLength(0);
    expect(io.visible.at(-1)).toContain('最终业务答案。');
    const calls = io.fetchMock.mock.calls.length;
    await sink.onResult(key);
    expect(io.fetchMock).toHaveBeenCalledTimes(calls);
  });

  it.skipIf(mode === 'off')('recovers an accepted final write with a lost acknowledgement without static replay', async () => {
    const io = transport('503');
    const original = global.fetch;
    let lost = false;
    vi.stubGlobal('fetch', async (input: Parameters<typeof fetch>[0], init?: RequestInit) => {
      const body = String(init?.body ?? '');
      if (!lost && String(input).includes('/elements/content/content') && body.includes('已接受但确认丢失。')) {
        lost = true;
        await original(input, init); // server accepted the write before the acknowledgement was lost
        throw new TypeError('lost acknowledgement');
      }
      return original(input, init);
    });
    const profile = resolveRichDocChannelProfiles({ RELAY_FEISHU_RICHDOC_MODE: mode }).feishu;
    const sink = createFeishuStreamingSink(agent, io.client as any, io.sendCard, undefined, { richDocProfile: profile })!;
    sink.beginTurn(key);
    await sink.onAssistant(key, openId, TURN_ACCEPTED_STATUS_TEXT, 'status');
    await sink.onAssistant(key, openId, progress(10), 'activity');
    await sink.onAssistant(key, openId, '已接受但确认丢失。', 'final');
    await sink.onResult(key);
    expect(lost).toBe(true);
    expect(io.create).toHaveBeenCalledTimes(1);
    expect(io.staticAnswers).toHaveLength(0);
    expect(io.visible.at(-1)).toContain('已接受但确认丢失。');
  });

  it.skipIf(mode === 'off')('does not replay a successful first rollover chunk when the final second chunk retries', async () => {
    const io = transport('503');
    const original = global.fetch;
    let failed = false;
    vi.stubGlobal('fetch', async (input: Parameters<typeof fetch>[0], init?: RequestInit) => {
      const body = String(init?.body ?? '');
      if (!failed && String(input).includes('/elements/content/content') && body.includes('第二段终态')) {
        failed = true;
        throw new TypeError('second chunk final failure');
      }
      return original(input, init);
    });
    const base = resolveRichDocChannelProfiles({ RELAY_FEISHU_RICHDOC_MODE: mode }).feishu;
    const profile = { ...base, rolloverThreshold: 24, maxPayloadLength: 24 };
    const sink = createFeishuStreamingSink(agent, io.client as any, io.sendCard, undefined, { richDocProfile: profile })!;
    sink.beginTurn(key);
    await sink.onAssistant(key, openId, '第一段进度第一段进度第一段进度\n第二段进度第二段进度第二段进度', 'activity');
    await sink.onAssistant(key, openId, '第一段终态第一段终态第一段终态\n第二段终态第二段终态第二段终态', 'final');
    await sink.onResult(key);
    expect(failed).toBe(true);
    expect(io.create.mock.calls.length).toBeGreaterThanOrEqual(2);
    expect(io.staticAnswers).toHaveLength(0);
    expect(io.requests.filter((request) => request.content.includes('第一段终态'))).toHaveLength(1);
  });

  it.skipIf(mode === 'off')('does not let a replaced turn close or terminal-mark its successor', async () => {
    const io = transport();
    const original = global.fetch;
    let release!: () => void;
    const blocked = new Promise<void>((resolve) => { release = resolve; });
    let entered!: () => void;
    const enteredFinal = new Promise<void>((resolve) => { entered = resolve; });
    vi.stubGlobal('fetch', async (input: Parameters<typeof fetch>[0], init?: RequestInit) => {
      if (String(input).includes('/elements/content/content') && String(init?.body ?? '').includes('旧终态。')) {
        entered(); await blocked;
      }
      return original(input, init);
    });
    const profile = resolveRichDocChannelProfiles({ RELAY_FEISHU_RICHDOC_MODE: mode }).feishu;
    const sink = createFeishuStreamingSink(agent, io.client as any, io.sendCard, undefined, { richDocProfile: profile })!;
    sink.beginTurn(key);
    await sink.onAssistant(key, openId, '旧终态。', 'final');
    const oldResult = sink.onResult(key);
    await enteredFinal;
    sink.beginTurn(key);
    release();
    await oldResult;
    await sink.onAssistant(key, openId, '新终态。', 'final');
    await sink.onResult(key);
    expect(io.create).toHaveBeenCalledTimes(2);
    expect(io.visible.at(-1)).toContain('新终态。');
  });

  it.skipIf(mode === 'off')('disposes a RichDoc card after terminal content retry exhaustion', async () => {
    const io = transport('503');
    const original = global.fetch;
    vi.stubGlobal('fetch', async (input: Parameters<typeof fetch>[0], init?: RequestInit) => {
      if (String(input).includes('/elements/content/content') && String(init?.body ?? '').includes('耗尽终态。')) {
        throw new TypeError('terminal content unavailable');
      }
      return original(input, init);
    });
    const profile = resolveRichDocChannelProfiles({ RELAY_FEISHU_RICHDOC_MODE: mode }).feishu;
    const sink = createFeishuStreamingSink(agent, io.client as any, io.sendCard, undefined, { richDocProfile: profile })!;
    sink.beginTurn(key);
    await sink.onAssistant(key, openId, TURN_ACCEPTED_STATUS_TEXT, 'status');
    await sink.onAssistant(key, openId, progress(10), 'activity');
    await sink.onAssistant(key, openId, '耗尽终态。', 'final');
    await sink.onResult(key);
    const calls = io.fetchMock.mock.calls.length;
    await sink.onResult(key);
    expect(io.fetchMock).toHaveBeenCalledTimes(calls);
    expect(io.staticAnswers).toHaveLength(0);
  });

});

describe('Feishu progress transport ordering and lifecycle', () => {
  it('does not let heartbeat overlap an in-flight content update', async () => {
    const io = transport();
    const session = new FeishuStreamingCardSession({ appId: 'ordering-test', appSecret: 'test' }, io.client);
    await session.start(openId);
    const original = global.fetch;
    let release!: () => void;
    let entered!: () => void;
    const started = new Promise<void>((resolve) => { entered = resolve; });
    const gate = new Promise<void>((resolve) => { release = resolve; });
    let active = 0;
    let maxActive = 0;
    let blocked = false;
    vi.stubGlobal('fetch', async (input: Parameters<typeof fetch>[0], init?: RequestInit) => {
      if (String(input).includes('/elements/')) {
        active += 1;
        maxActive = Math.max(maxActive, active);
        if (!blocked) { blocked = true; entered(); await gate; }
        const result = await original(input, init);
        active -= 1;
        return result;
      }
      return original(input, init);
    });
    const update = session.update(progress(5));
    await started;
    session.setPulseLine('opaque tool signal');
    await vi.advanceTimersByTimeAsync(1);
    try { expect(maxActive).toBe(1); } finally { release(); }
    await update;
    await session.update(progress(10));
    await vi.advanceTimersByTimeAsync(2000);
    await session.close('最终业务答案。');
    expect(io.visible.at(-1)).toBe('最终业务答案。');
    expect(maxActive).toBe(1);
  });

  it('bounds automatic retries and recovers on a later real event', async () => {
    const io = transport();
    const original = global.fetch;
    let reject = true;
    let attempts = 0;
    vi.stubGlobal('fetch', async (input: Parameters<typeof fetch>[0], init?: RequestInit) => {
      if (String(input).includes('/elements/')) {
        attempts += 1;
        if (reject) return new Response('temporary', { status: 503 });
      }
      return original(input, init);
    });
    const session = new FeishuStreamingCardSession({ appId: 'retry-budget-test', appSecret: 'test' }, io.client);
    await session.start(openId);
    await session.update(progress(10)).catch(() => undefined);
    await vi.advanceTimersByTimeAsync(180_000);
    expect(attempts).toBe(4); // Initial attempt + three delayed retries, not heartbeats.
    reject = false;
    await session.update(progress(11));
    expect(io.visible.at(-1)).toBe(progress(11));
    await session.close('完成。');
  });

  it.each(['dispose', 'close'] as const)('%s wins over an in-flight update and queued heartbeat', async (action) => {
    const io = transport();
    const session = new FeishuStreamingCardSession({ appId: `inflight-${action}`, appSecret: 'test' }, io.client);
    await session.start(openId);
    const original = global.fetch;
    let release!: () => void;
    let entered!: () => void;
    const gate = new Promise<void>((resolve) => { release = resolve; });
    const started = new Promise<void>((resolve) => { entered = resolve; });
    let block = true;
    vi.stubGlobal('fetch', async (input: Parameters<typeof fetch>[0], init?: RequestInit) => {
      if (block && String(input).includes('/elements/')) {
        block = false;
        entered();
        await gate;
      }
      return original(input, init);
    });
    const writing = session.update(progress(5));
    await started;
    session.setPulseLine('private signal');
    const closing = action === 'close' ? session.close('权威终态。') : Promise.resolve(session.dispose());
    await session.update(progress(999));
    release();
    await Promise.all([writing, closing]);
    expect(session.isClosed()).toBe(true);
    expect(session.getCurrentText()).toBe('');
    expect(io.visible.join('')).not.toContain(progress(999));
    if (action === 'close') expect(io.visible.at(-1)).toBe('权威终态。');
    const count = io.fetchMock.mock.calls.length;
    await vi.advanceTimersByTimeAsync(180_000);
    expect(io.fetchMock).toHaveBeenCalledTimes(count);
  });

  it('disposal cancels queued retries without publishing a stale frame', async () => {
    const io = transport('503');
    const session = new FeishuStreamingCardSession({ appId: 'dispose-test', appSecret: 'test' }, io.client);
    await session.start(openId);
    await session.update(progress(10)).catch(() => undefined);
    session.dispose();
    const count = io.fetchMock.mock.calls.length;
    await vi.advanceTimersByTimeAsync(180_000);
    expect(io.fetchMock).toHaveBeenCalledTimes(count);
    expect(session.getCurrentText()).toBe('');
  });

  it('counts every actual event, including non-milestones, without clock-driven increments', () => {
    const counter = new TurnActivityCounter();
    counter.begin(key);
    for (let step = 1; step <= 39; step += 1) {
      expect(counter.advance(key)).toEqual({ step, text: progress(step) });
    }
    counter.end(key);
    expect(counter.advance(key)).toBeNull();
  });
});

// Preserve explicit rollback-path coverage; default folded UI is exercised in folded-process-channels.test.ts.
beforeEach(() => { vi.stubEnv('RELAY_WORK_PROCESS_DETAILS', 'off'); });

