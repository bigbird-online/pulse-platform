import { afterEach, beforeEach, describe, expect, it } from 'vitest';
import { mkdtempSync, readFileSync, readdirSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import {
  isRequestTraceEnabled,
  traceInboundRequest,
  traceTurnEnd,
  traceDeliveryReceipt,
  shouldTraceTurnEnd,
} from '../src/relay-request-trace.js';

// 事件流按天分桶 degen-{YYYY-MM-DD}.jsonl（复用 emitObsEvent 的落盘约定）。
// 读回今天的桶（trace 用 new Date().toISOString() 打 ts）。
function readTodayEvents(dir: string): Record<string, unknown>[] {
  const files = readdirSync(dir).filter((f) => f.startsWith('degen-') && f.endsWith('.jsonl'));
  const events: Record<string, unknown>[] = [];
  for (const f of files) {
    const lines = readFileSync(join(dir, f), 'utf8').trim().split('\n').filter(Boolean);
    for (const l of lines) events.push(JSON.parse(l));
  }
  return events;
}

describe('isRequestTraceEnabled — dark-deploy 开关默认关', () => {
  it('defaults to disabled (dark deploy)', () => {
    expect(isRequestTraceEnabled({})).toBe(false);
  });
  it('only true (case-insensitive) enables', () => {
    expect(isRequestTraceEnabled({ RELAY_OBS_REQUEST_TRACE: 'true' })).toBe(true);
    expect(isRequestTraceEnabled({ RELAY_OBS_REQUEST_TRACE: 'TRUE' })).toBe(true);
    expect(isRequestTraceEnabled({ RELAY_OBS_REQUEST_TRACE: 'false' })).toBe(false);
    expect(isRequestTraceEnabled({ RELAY_OBS_REQUEST_TRACE: '1' })).toBe(false);
    expect(isRequestTraceEnabled({ RELAY_OBS_REQUEST_TRACE: 'yes' })).toBe(false);
  });
});

describe('shouldTraceTurnEnd — turn.end 去重判据（缺陷 B）', () => {
  it('print-mode 空合成 result（无 subtype 无 usage）→ 跳过', () => {
    // pool.ts finishTurn emit 的 {type:'result', result:''} 形态。
    expect(shouldTraceTurnEnd({})).toBe(false);
    expect(shouldTraceTurnEnd({ type: 'result', result: '' } as any)).toBe(false);
    expect(shouldTraceTurnEnd({ subtype: '' })).toBe(false);
    expect(shouldTraceTurnEnd({ subtype: undefined, usage: undefined })).toBe(false);
    expect(shouldTraceTurnEnd({ usage: null } as any)).toBe(false);
  });

  it('print-mode 真 result（有 subtype+usage）→ 打点', () => {
    expect(shouldTraceTurnEnd({ subtype: 'success', usage: { input_tokens: 8, output_tokens: 2743 } })).toBe(true);
    expect(shouldTraceTurnEnd({ subtype: 'error_max_turns' })).toBe(true);
  });

  it('interactive-runtime 合成 result（无 subtype 但带 usage）→ 打点(不误伤)', () => {
    // pool.ts finishTurn(proc, {model, usage}) → {type:'result', result:'', usage}。
    expect(shouldTraceTurnEnd({ usage: { input_tokens: 120, output_tokens: 88 } })).toBe(true);
    // 空 usage 对象仍算实质（有 usage 键即视为完整回合信号）。
    expect(shouldTraceTurnEnd({ usage: {} })).toBe(true);
  });

  it('去重效果：一个 print-mode turn 的两条 result 恰好一条通过', () => {
    const emptySynthetic = { type: 'result', result: '' };                                  // finishTurn 合成
    const realCcResult = { type: 'result', subtype: 'success', duration_ms: 57176, usage: { input_tokens: 8, output_tokens: 2743 }, is_error: false };
    const passed = [emptySynthetic, realCcResult].filter((m) => shouldTraceTurnEnd(m as any));
    expect(passed).toHaveLength(1);
    expect(passed[0]).toBe(realCcResult);
  });
});

describe('三类事件序列化正确（开关开）', () => {
  let dir: string;
  let env: NodeJS.ProcessEnv;
  beforeEach(() => {
    dir = mkdtempSync(join(tmpdir(), 'reqtrace-test-'));
    // 开 trace 开关 + 指定落盘目录 + 确保 obs 底层开关不是 false。
    env = { RELAY_OBS_REQUEST_TRACE: 'true', RELAY_OBS_DIR: dir, RELAY_OBS_ENABLED: 'true' };
  });
  afterEach(() => rmSync(dir, { recursive: true, force: true }));

  it('req.inbound 记 channel/agent/session/who + severity=info', () => {
    traceInboundRequest({ channel: 'feishu', agent: 'bb', session: 'u:bb:feishu', who: 'ou_12345', detail: { msgType: 'text' } }, env);
    const events = readTodayEvents(dir);
    expect(events).toHaveLength(1);
    const e = events[0]!;
    expect(e.evt).toBe('req.inbound');
    expect(e.channel).toBe('feishu');
    expect(e.agent).toBe('bb');
    expect(e.session).toBe('u:bb:feishu');
    expect(e.who).toBe('ou_12345');
    expect(e.msgType).toBe('text');
    expect(e.severity).toBe('info');
    expect(typeof e.ts).toBe('string');
  });

  it('turn.end 记 stopReason/durationMs/token；正常回合 severity=info', () => {
    traceTurnEnd({ agent: 'bb', session: 'sid-1', stopReason: 'end_turn', durationMs: 4200, inputTokens: 120, outputTokens: 88, isError: false }, env);
    const e = readTodayEvents(dir)[0]!;
    expect(e.evt).toBe('turn.end');
    expect(e.stopReason).toBe('end_turn');
    expect(e.durationMs).toBe(4200);
    expect(e.inputTokens).toBe(120);
    expect(e.outputTokens).toBe(88);
    expect(e.isError).toBe(false);
    expect(e.severity).toBe('info');
  });

  it('turn.end 报错回合 severity=warn', () => {
    traceTurnEnd({ agent: 'bb', session: 'sid-1', stopReason: 'error', isError: true }, env);
    const e = readTodayEvents(dir)[0]!;
    expect(e.isError).toBe(true);
    expect(e.severity).toBe('warn');
  });

  it('切片A：turn.end 带 poolKey → 同事件同时带 poolKey(配对主键)+session(CC sessionId)', () => {
    traceTurnEnd(
      { agent: 'pulse', poolKey: 'owner:pulse:feishu', session: 'cc-uuid-abc', stopReason: 'end_turn', isError: false },
      env,
    );
    const e = readTodayEvents(dir)[0]!;
    expect(e.evt).toBe('turn.end');
    // 桥字段：poolKey = 带 scope 的配对主键；session 仍是 CC sessionId(不变)。
    expect(e.poolKey).toBe('owner:pulse:feishu');
    expect(e.session).toBe('cc-uuid-abc');
  });

  it('切片A：turn.end 缺 poolKey 时不落 poolKey 键（与其他可选字段同风格）', () => {
    traceTurnEnd({ agent: 'pulse', session: 'cc-uuid-abc' }, env);
    const e = readTodayEvents(dir)[0]!;
    expect(e).not.toHaveProperty('poolKey');
  });

  it('turn.end 缺省字段不落盘（不写 undefined 键）', () => {
    traceTurnEnd({ agent: 'bb', session: 'sid-1' }, env);
    const e = readTodayEvents(dir)[0]!;
    expect(e).not.toHaveProperty('stopReason');
    expect(e).not.toHaveProperty('durationMs');
    expect(e).not.toHaveProperty('inputTokens');
    expect(e).not.toHaveProperty('isError');
    // 缺 isError 时按正常回合处理。
    expect(e.severity).toBe('info');
  });

  it('delivery.receipt sent → severity=info；failed → warn', () => {
    traceDeliveryReceipt({ session: 'corr-1', status: 'sent', attempts: 1, channel: 'telegram' }, env);
    traceDeliveryReceipt({ session: 'corr-2', status: 'failed', attempts: 3, channel: 'feishu' }, env);
    const events = readTodayEvents(dir);
    expect(events).toHaveLength(2);
    const sent = events.find((e) => e.session === 'corr-1')!;
    const failed = events.find((e) => e.session === 'corr-2')!;
    expect(sent.evt).toBe('delivery.receipt');
    expect(sent.status).toBe('sent');
    expect(sent.attempts).toBe(1);
    expect(sent.channel).toBe('telegram');
    expect(sent.severity).toBe('info');
    expect(failed.status).toBe('failed');
    expect(failed.severity).toBe('warn');
  });

  it('每行都是单行合法 JSON（探针逐行 parse 前提）', () => {
    traceInboundRequest({ channel: 'telegram', agent: 'a', session: 's' }, env);
    traceTurnEnd({ agent: 'a', session: 's', stopReason: 'end_turn' }, env);
    traceDeliveryReceipt({ session: 's', status: 'sent' }, env);
    const files = readdirSync(dir);
    for (const f of files) {
      for (const line of readFileSync(join(dir, f), 'utf8').trim().split('\n')) {
        expect(() => JSON.parse(line)).not.toThrow();
      }
    }
  });
});

describe('dark deploy：开关关时零落盘、零行为', () => {
  let dir: string;
  beforeEach(() => {
    dir = mkdtempSync(join(tmpdir(), 'reqtrace-off-'));
  });
  afterEach(() => rmSync(dir, { recursive: true, force: true }));

  it('开关未设 → 不落盘', () => {
    const env = { RELAY_OBS_DIR: dir, RELAY_OBS_ENABLED: 'true' }; // 无 RELAY_OBS_REQUEST_TRACE
    traceInboundRequest({ channel: 'feishu', agent: 'a', session: 's' }, env);
    traceTurnEnd({ agent: 'a', session: 's', stopReason: 'end_turn' }, env);
    traceDeliveryReceipt({ session: 's', status: 'sent' }, env);
    expect(readdirSync(dir)).toHaveLength(0);
  });

  it('开关显式 false → 不落盘', () => {
    const env = { RELAY_OBS_REQUEST_TRACE: 'false', RELAY_OBS_DIR: dir, RELAY_OBS_ENABLED: 'true' };
    traceInboundRequest({ channel: 'telegram', agent: 'a', session: 's' }, env);
    expect(readdirSync(dir)).toHaveLength(0);
  });
});

describe('切片A 配对可行性证明：同一次交互三事件经 poolKey group-by 配成一条链路', () => {
  let dir: string;
  let env: NodeJS.ProcessEnv;
  beforeEach(() => {
    dir = mkdtempSync(join(tmpdir(), 'reqtrace-pair-'));
    env = { RELAY_OBS_REQUEST_TRACE: 'true', RELAY_OBS_DIR: dir, RELAY_OBS_ENABLED: 'true' };
  });
  afterEach(() => rmSync(dir, { recursive: true, force: true }));

  it('req.inbound / turn.end / delivery 落同一 poolKey 字符串 → 探针可 group-by 配对', () => {
    // 同一次 feishu 交互（dual-channel bot,带 :feishu scope）。三事件的配对键都是这个完整池键：
    //  - req.inbound: session = feishuPoolKey(...) = 带 scope 的完整池键（改动3 scope 归一后）
    //  - turn.end:    poolKey = on('response') 回传的池键（改动2 补落）；session 仍是 CC sessionId
    //  - delivery:    session = poolKey（流式卡片路径既有埋点）
    // feishuPoolKey 与 CC spawn/delivery 走的是同一函数同一入参 → 逐字符相同。
    const poolKey = 'owner:pulse:feishu';       // 带 scope 的统一主键
    const ccSessionId = 'cc-uuid-9f3a';         // turn.end 另带的 CC sessionId

    // 1) 入站：改动3 后 req.inbound.session 落 feishuPoolKey（带 :feishu），不再是 bare。
    traceInboundRequest({ channel: 'feishu', agent: 'pulse', session: poolKey, who: 'ou_12345', detail: { msgType: 'text' } }, env);
    // 2) 回合收尾：改动2 后 turn.end 同时带 poolKey(配对主键) + session(CC sessionId)。
    traceTurnEnd({ agent: 'pulse', poolKey, session: ccSessionId, stopReason: 'end_turn', durationMs: 4200, isError: false }, env);
    // 3) 投递：流式卡片 close 成功，delivery.session = poolKey（既有埋点）。
    traceDeliveryReceipt({ agent: 'pulse', session: poolKey, status: 'sent', attempts: 1, channel: 'feishu' }, env);

    const events = readTodayEvents(dir);
    expect(events).toHaveLength(3);

    const inbound = events.find((e) => e.evt === 'req.inbound')!;
    const turn = events.find((e) => e.evt === 'turn.end')!;
    const delivery = events.find((e) => e.evt === 'delivery.receipt')!;

    // 三事件的配对键（探针实际 group-by 用的字段）逐字符相同——这是整个切片的目的。
    const inboundKey = inbound.session as string;
    const turnKey = turn.poolKey as string;          // turn.end 用 poolKey 作配对键
    const deliveryKey = delivery.session as string;
    expect(inboundKey).toBe(poolKey);
    expect(turnKey).toBe(poolKey);
    expect(deliveryKey).toBe(poolKey);
    // 三者两两严格相等（不靠 scope 兼容/前缀匹配，逐字符）。
    expect(inboundKey).toBe(turnKey);
    expect(turnKey).toBe(deliveryKey);

    // turn.end 是桥：同时带配对主键(poolKey) + CC sessionId，让链路也能连到 metering/state。
    expect(turn.session).toBe(ccSessionId);
    expect(turn.session).not.toBe(turn.poolKey);

    // 模拟探针 group-by poolKey：三事件归入同一 bucket → 一条 req→turn→delivery 完整链路。
    const byPairKey = new Map<string, Record<string, unknown>[]>();
    const pairKeyOf = (e: Record<string, unknown>): string | undefined =>
      e.evt === 'turn.end' ? (e.poolKey as string) : (e.session as string);
    for (const e of events) {
      const k = pairKeyOf(e);
      if (!k) continue;
      (byPairKey.get(k) ?? byPairKey.set(k, []).get(k)!).push(e);
    }
    expect(byPairKey.size).toBe(1);                    // 只有一个配对桶
    const chain = byPairKey.get(poolKey)!;
    expect(chain.map((e) => e.evt).sort()).toEqual(['delivery.receipt', 'req.inbound', 'turn.end']);
  });

  it('反证：改动前 req.inbound 落 bare poolKey → 与 turn.end/delivery 配不上（分裂成两桶）', () => {
    // 复现改动前故障：req.inbound 用 router.poolKey()=bare owner:pulse，其余带 :feishu scope。
    const bare = 'owner:pulse';
    const scoped = 'owner:pulse:feishu';
    traceInboundRequest({ channel: 'feishu', agent: 'pulse', session: bare }, env);       // 旧行为：bare
    traceTurnEnd({ agent: 'pulse', poolKey: scoped, session: 'cc-uuid', stopReason: 'end_turn' }, env);
    traceDeliveryReceipt({ agent: 'pulse', session: scoped, status: 'sent' }, env);

    const events = readTodayEvents(dir);
    const pairKeyOf = (e: Record<string, unknown>): string =>
      e.evt === 'turn.end' ? (e.poolKey as string) : (e.session as string);
    const keys = new Set(events.map(pairKeyOf));
    // bare 与 scoped 不等 → 分裂成两桶，配不成一条链路（正是切片A 要修的病）。
    expect(keys.size).toBe(2);
    expect(keys.has(bare)).toBe(true);
    expect(keys.has(scoped)).toBe(true);
  });
});

describe('埋点异常被隔离不冒泡（生产安全最高优先级）', () => {
  it('落盘路径不可写时,三个入口都不抛（emitObsEvent 自吞 + 本模块二层 try/catch）', () => {
    // 指向父是文件的不可能路径,mkdir/append 都会失败——trace 必须吞掉,绝不上抛。
    const badEnv = { RELAY_OBS_REQUEST_TRACE: 'true', RELAY_OBS_DIR: '/dev/null/cannot/mkdir' };
    expect(() => traceInboundRequest({ channel: 'feishu', agent: 'a', session: 's' }, badEnv)).not.toThrow();
    expect(() => traceTurnEnd({ agent: 'a', session: 's', stopReason: 'end_turn' }, badEnv)).not.toThrow();
    expect(() => traceDeliveryReceipt({ session: 's', status: 'sent' }, badEnv)).not.toThrow();
  });
});
