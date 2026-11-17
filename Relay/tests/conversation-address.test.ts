import { describe, expect, it } from 'vitest';
import { formatConversationPoolKey, parseConversationPoolKey } from '../src/conversation-address.js';

describe('persisted conversation pool keys', () => {
  it.each([
    [{ userId: 'eric', agentId: 'pulse' }, 'eric:pulse'],
    [{ userId: 'owner', agentId: 'pulse', channelScope: 'feishu' }, 'owner:pulse:feishu'],
  ] as const)('keeps the legacy bytes and round-trips %s', (address, key) => {
    expect(formatConversationPoolKey(address)).toBe(key);
    expect(parseConversationPoolKey(key)).toEqual(address);
  });

  it.each(['', 'eric', ':pulse', 'eric:', 'eric:pulse:tg', 'eric:pulse:feishu:extra'])
    ('does not guess the identity of malformed key %s', (key) => {
      expect(parseConversationPoolKey(key)).toBeUndefined();
    });

  it.each([
    { userId: '', agentId: 'pulse' },
    { userId: 'eric', agentId: '' },
  ])('rejects an empty address before persistence', address => {
    expect(() => formatConversationPoolKey(address)).toThrow(TypeError);
  });

  it('preserves legacy separator bytes without guessing their parsed identity', () => {
    const key = formatConversationPoolKey({ userId: 'eric', agentId: 'a:b' });
    expect(key).toBe('eric:a:b');
    expect(parseConversationPoolKey(key)).toBeUndefined();
  });
});
