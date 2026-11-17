/**
 * Shared state for pending interactive asks (AskUserQuestion).
 * Used by both feishu.ts (registers pending) and pool.ts (checks before idle reclaim).
 * Separate module to avoid circular dependencies.
 */

const pendingPoolKeys = new Set<string>();

export function markPendingInteractive(poolKey: string): void {
  pendingPoolKeys.add(poolKey);
}

export function clearPendingInteractive(poolKey: string): void {
  pendingPoolKeys.delete(poolKey);
}

export function isPoolKeyPendingInteractive(poolKey: string): boolean {
  return pendingPoolKeys.has(poolKey);
}
