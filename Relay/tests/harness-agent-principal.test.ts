import { describe, expect, it, vi } from 'vitest';
import { ConfigValidationError, harnessPrincipalForAgent, validateConfig } from '../src/types.js';
import { resolveMeteringPrincipal } from '../src/index.js';

describe('operator-provisioned Harness principal', () => {
  it('uses the canonical principal for Host metering without renaming the channel alias', () => {
    const agents = [
      { id: 'tcloud-pulse', harnessAgentId: 'pulse' },
      { id: 'aki' },
    ];
    const router = { getAgents: vi.fn(() => agents) };
    expect(resolveMeteringPrincipal(router as never, 'u:tcloud-pulse')).toBe('pulse');
    expect(resolveMeteringPrincipal(router as never, 'u:aki')).toBe('aki');
    expect(resolveMeteringPrincipal(router as never, 'u:missing')).toBeUndefined();
    expect(agents[0]!.id).toBe('tcloud-pulse');
  });

  it('preserves the existing identity unless an explicit mapping is configured', () => {
    expect(harnessPrincipalForAgent({ id: 'pulse' })).toBe('pulse');
    expect(harnessPrincipalForAgent({ id: 'tcloud-pulse' })).toBe('tcloud-pulse');
    expect(harnessPrincipalForAgent({ id: 'other-pulse' })).toBe('other-pulse');
  });

  it('maps authority without modifying channel identity', () => {
    const agent = { id: 'tcloud-pulse', harnessAgentId: 'pulse' };
    expect(harnessPrincipalForAgent(agent)).toBe('pulse');
    expect(agent.id).toBe('tcloud-pulse');
  });

  it.each([null, 42, '', ' pulse', 'pulse ', '../pulse', 'pulse\n', 'a'.repeat(129)])(
    'rejects an invalid explicitly configured principal %j at startup and use', (harnessAgentId) => {
      const agent = { id: 'tcloud-pulse', harnessAgentId: harnessAgentId as string, name: 'Pulse', emoji: 'P',
        workspace: '/workspace', botToken: 'test-only' };
      expect(() => harnessPrincipalForAgent(agent)).toThrow(ConfigValidationError);
      expect(() => validateConfig({ users: [{ userId: 'u', defaultAgent: agent.id, agents: [agent] }] }))
        .toThrow(ConfigValidationError);
    },
  );
});
