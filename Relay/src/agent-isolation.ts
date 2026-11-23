/**
 * Whether a job owner's Agent runs as the Relay host account. Every scheduler
 * entry (the resident Relay, `relay-jobs run --local`) resolves it the same way,
 * from the Relay configuration; an owner no configured Agent names is unknown.
 */
import { harnessPrincipalForAgent, type AgentConfig } from './types.js';
import type { Router } from './router.js';

export type OwnerIsolation = 'host' | 'isolated' | 'unknown';

export function ownerIsolation(agents: AgentConfig[], owner: string): OwnerIsolation {
  // An Agent whose principal cannot be resolved proves nothing about this owner.
  const owned = agents.filter((agent) => { try { return harnessPrincipalForAgent(agent) === owner; } catch { return false; } });
  if (owned.length === 0) return 'unknown';
  return owned.some((agent) => agent.linuxUser?.trim()) ? 'isolated' : 'host';
}

export function ownerIsolationFromRouter(router: Pick<Router, 'getAllUsers' | 'getAgents'>): (owner: string) => OwnerIsolation {
  return (owner) => ownerIsolation(router.getAllUsers().flatMap((user) => router.getAgents(user.userId)), owner);
}
