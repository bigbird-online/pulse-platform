import { describe, it, expect, beforeEach, afterEach } from 'vitest';
import { mkdirSync, rmSync } from 'node:fs';
import { join } from 'node:path';
import { tmpdir } from 'node:os';
import { createStateManager } from '../src/state.js';

// Regression coverage for the 2026-07-25 记忆错乱 (session-crossing) incident.
// memo: vultr-pulse-space/memo/2026-07/2026-07-25-session-crossing-memory-loss-rootcause.md
//
// 现场：飞书 Pulse 谈了 55 分钟 stats（CC 自建 session 3c501b48），进程懒驻留驱逐后，
// 下一条消息 --resume 到了昨天的 88f6a13a，用户看到"记忆错乱"。
//
// 根因是三点合流：
//   1. 入站写 state 用的是 router 配置值，不是 CC 真在跑的 sessionId
//   2. index.ts 的 CC-session 捕获被 `if (!existing?.sessionId)` guard 挡死
//   3. 真 sessionId 只活在 pool 内存里，进程一驱逐就没了
//
// 本文件锁住 1 和 2 的修法：优先级顺序（live > state > config）与"值不一致就回写"。

const testDir = join(tmpdir(), 'relay-session-crossing-' + Date.now());

beforeEach(() => { mkdirSync(testDir, { recursive: true }); });
afterEach(() => { rmSync(testDir, { recursive: true, force: true }); });

// 复刻 channels/feishu.ts 的 resolveSessionIdToPersist / telegram.ts 的
// sessionIdToPersist 的优先级契约。两处实现同构，这里锁住那个顺序本身。
function resolveSessionIdToPersist(
  liveSid: string | undefined,
  stateSid: string | undefined,
  configSid: string | undefined,
): string | undefined {
  return liveSid ?? stateSid ?? configSid;
}

describe('sessionId to persist: live process wins over config (事故断链点1)', () => {
  it('CC 自产的真 session 覆盖配置值——事故的核心一条', () => {
    // 事故现场的三个真值：配置说 27a70637（jsonl 根本不存在）、state 里躺着昨天的
    // 88f6a13a、CC 实际在跑 3c501b48。要落盘的必须是 CC 真在跑的那条。
    expect(resolveSessionIdToPersist(
      '3c501b48-stats',
      '88f6a13a-ken-yesterday',
      '27a70637-config-ghost',
    )).toBe('3c501b48-stats');
  });

  it('进程已驱逐时退回 state，绝不退回配置值', () => {
    // 驱逐后 pool.get() 返回 undefined。此时 state 里存的（经上一条修好后）已经是
    // 真 session，必须用它，而不是掉回配置里的死值。
    expect(resolveSessionIdToPersist(
      undefined,
      '3c501b48-stats',
      '27a70637-config-ghost',
    )).toBe('3c501b48-stats');
  });

  it('冷启动（无进程无 state）才允许用配置值兜底', () => {
    expect(resolveSessionIdToPersist(undefined, undefined, '27a70637-config-ghost'))
      .toBe('27a70637-config-ghost');
  });

  it('三者皆空时返回 undefined，不编造值', () => {
    expect(resolveSessionIdToPersist(undefined, undefined, undefined)).toBeUndefined();
  });
});

// index.ts 捕获块的 guard 契约：旧版 `!existing?.sessionId`（只在空时写）
// → 新版 `existing?.sessionId !== ccSid`（不一致就写）。
function shouldCaptureCcSid(existingSid: string | undefined, ccSid: string): boolean {
  return existingSid !== ccSid;
}

describe('CC session_id capture guard (事故断链点2)', () => {
  it('state 已有陈旧值时必须回写——旧 guard 正是在这里失守', () => {
    // 事故当天全天 journal 里 `Captured CC session_id` 零条，就是因为
    // existing.sessionId 非空（88f6a13a），旧 guard 直接跳过整个捕获块。
    expect(shouldCaptureCcSid('88f6a13a-ken-yesterday', '3c501b48-stats')).toBe(true);
  });

  it('state 为空时照旧回写（保持旧行为，不回归）', () => {
    expect(shouldCaptureCcSid(undefined, '3c501b48-stats')).toBe(true);
  });

  it('值已一致时不重复写，避免每个 turn 都刷盘', () => {
    expect(shouldCaptureCcSid('3c501b48-stats', '3c501b48-stats')).toBe(false);
  });
});

describe('end-to-end: 事故序列在修复后不再重演', () => {
  it('驱逐→重建后拿到的仍是 stats session，不是昨天那条', () => {
    const state = createStateManager(join(testDir, 'state.json'));
    state.load();
    const poolKey = 'owner:pulse:feishu';

    // 事故前置条件：state 里躺着昨天 Ken 那条。
    state.updateSession(poolKey, {
      poolKey, agentId: 'pulse', runtime: 'cc',
      sessionId: '88f6a13a-ken-yesterday',
      lastActivity: Date.now(), status: 'active',
    });

    // 02:52 CC 自建 stats session 并在 result 里报了回来。新 guard 判不一致 → 回写。
    const ccSid = '3c501b48-stats';
    const existing = state.getSession(poolKey);
    expect(shouldCaptureCcSid(existing?.sessionId, ccSid)).toBe(true);
    state.updateSession(poolKey, { sessionId: ccSid, lastActivity: Date.now() });

    // 03:47 进程被懒驻留驱逐（live = undefined）。
    // 04:21 下一条消息重建，此刻决定 --resume 谁。
    const resumeTarget = resolveSessionIdToPersist(
      undefined,                              // 进程没了
      state.getSession(poolKey)?.sessionId,   // state 现在记的是真值
      '27a70637-config-ghost',                // 配置死值
    );

    expect(resumeTarget).toBe('3c501b48-stats');
    expect(resumeTarget).not.toBe('88f6a13a-ken-yesterday'); // 事故里就是接到了这条
  });
});
