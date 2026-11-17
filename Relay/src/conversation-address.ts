/** The persisted pool key is a compatibility format, not a caller-owned protocol. */
export interface ConversationAddress {
  userId: string;
  agentId: string;
  channelScope?: 'feishu';
}

export function formatConversationPoolKey(address: ConversationAddress): string {
  if (!address.userId || !address.agentId)
    throw new TypeError('Invalid conversation address');
  // Formatting preserves configured legacy identifiers. Parsing an ambiguous
  // persisted key still fails closed; it must not make Router construction fail.
  const base = `${address.userId}:${address.agentId}`;
  return address.channelScope === 'feishu' ? `${base}:feishu` : base;
}

export function parseConversationPoolKey(poolKey: string): ConversationAddress | undefined {
  const segments = poolKey.split(':');
  if ((segments.length !== 2 && segments.length !== 3)
    || !segments[0] || !segments[1]
    || (segments.length === 3 && segments[2] !== 'feishu')) return undefined;
  return segments.length === 3
    ? { userId: segments[0], agentId: segments[1], channelScope: 'feishu' }
    : { userId: segments[0], agentId: segments[1] };
}
