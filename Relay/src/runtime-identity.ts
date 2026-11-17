/**
 * Identity of a conversation runtime behind its wrappers.
 *
 * A channel may hand its handlers a wrapper of the shared runtime (the intake
 * journal's receipt-carrying runtime). State kept per runtime, such as the
 * per-address acquisition queue, must belong to the runtime itself, so every
 * path into one runtime, wrapped or not, meets the same state.
 */
export const WRAPPED_RUNTIME: unique symbol = Symbol('wrapped conversation runtime');

/** The runtime itself: unwraps every wrapper that names what it wraps. */
export function underlyingRuntime<T extends object>(runtime: T): T {
  let current = runtime;
  for (;;) {
    const inner = (current as { [WRAPPED_RUNTIME]?: T })[WRAPPED_RUNTIME];
    if (!inner || inner === current) return current;
    current = inner;
  }
}
