import { describe, it, expect } from 'vitest';
import { buildReplyContextPrompt, extractReplyContextUserTail } from '../src/reply-context.js';

describe('extractReplyContextUserTail', () => {
  it('returns plain prompt when there is no reply wrapper', () => {
    expect(extractReplyContextUserTail('你好')).toBe('你好');
    expect(extractReplyContextUserTail('  hello\n')).toBe('hello');
  });

  it('returns only the user utterance after --- user message ---', () => {
    const bridged = buildReplyContextPrompt('你好', '上文');
    expect(bridged).toContain('--- user message ---');
    expect(extractReplyContextUserTail(bridged)).toBe('你好');
  });
});

describe('buildReplyContextPrompt cross-session guard', () => {
  it('uses legacy header when origin is missing (back-compat)', () => {
    const out = buildReplyContextPrompt('继续', '历史卡内容');
    expect(out).toContain('用户正在回复上一条消息');
    expect(out).not.toContain('历史消息');
  });

  it('uses legacy header when same-session', () => {
    const out = buildReplyContextPrompt('继续', '本 session 的话', {
      sameSession: true,
      quotedSessionId: 'sess-current',
    });
    expect(out).toContain('用户正在回复上一条消息');
    expect(out).not.toContain('历史消息');
  });

  it('uses cross-session header when origin says different session', () => {
    const out = buildReplyContextPrompt('继续', '上个 session 的话', {
      sameSession: false,
      quotedSessionId: '12345678-aaaa',
      quotedAtMs: Date.now() - 12 * 3600 * 1000, // 12h ago
    });
    expect(out).toContain('用户引用了一条历史消息');
    expect(out).toContain('其他 session');
    expect(out).toContain('小时前');
    expect(out).toContain('12345678');
  });
});
