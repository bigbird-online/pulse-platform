/**
 * A Relay internal delivery (a contract, a pull request result) is meant for one native conversation:
 * the one the core named. The turn carries the IDs that conversation is known by, and the runtime
 * that finally takes the turn to start the model checks them against the conversation it would run
 * it in. Every wait before that point (the channel's notice, the maintenance barrier, a runtime's
 * own queue, a conversation agy renamed after the turn ahead) is covered by that one check.
 *
 * The delivery is settled there: `consumed` once the turn has really started in the routed
 * conversation, `refused` when it is routed elsewhere or fails before it starts. The collaboration ledger records the delivery only when consumed,
 * so a refused one is offered again by the next gate run.
 */
import { conversationNativeIds, type NativeIdSource } from './session-identity.js';
import type { StreamJsonMessage } from './types.js';

const TARGET = 'expect_native_ids';
const REF = 'internal_delivery_ref';
/** Result subtype of a refused internal turn; the result is control-only, no channel shows it. */
export const INTERNAL_TURN_REFUSED = 'internal_turn_refused';

export type InternalTurnOutcome = 'consumed' | 'refused';

const watchers = new Map<string, (outcome: InternalTurnOutcome) => void>();

/** The message as an internal delivery for the conversation known by one of `ids`. */
export function fencedInternalTurn(message: StreamJsonMessage, ids: string[], ref?: string): StreamJsonMessage {
  return { ...message, [TARGET]: [...ids], ...(ref ? { [REF]: ref } : {}) };
}

/** The message without the fence, as written to a runtime. */
export function withoutInternalTurnFence(message: StreamJsonMessage): StreamJsonMessage {
  if (!(TARGET in message) && !(REF in message)) return message;
  const { [TARGET]: _target, [REF]: _ref, ...rest } = message;
  return rest as StreamJsonMessage;
}

function target(message: StreamJsonMessage): string[] | undefined {
  const ids = message[TARGET];
  return Array.isArray(ids) ? ids.filter((id): id is string => typeof id === 'string') : undefined;
}

/** True when the message is an internal delivery and `live` is not the conversation it is meant for. */
export function internalTurnRefused(message: StreamJsonMessage, live: NativeIdSource | undefined): boolean {
  const ids = target(message);
  if (!ids) return false;
  return !live || !conversationNativeIds(live).some(id => ids.includes(id));
}

/** Call `onSettled` once, when the turn carrying `ref` is consumed or refused. Returns a cancel. */
export function watchInternalTurn(ref: string, onSettled: (outcome: InternalTurnOutcome) => void): () => void {
  watchers.set(ref, onSettled);
  return () => { if (watchers.get(ref) === onSettled) watchers.delete(ref); };
}

/** Report the outcome of an internal delivery; only the first report counts, other messages are ignored. */
export function settleInternalTurn(message: StreamJsonMessage, outcome: InternalTurnOutcome): void {
  const ref = message[REF];
  if (typeof ref !== 'string') return;
  const watcher = watchers.get(ref);
  watchers.delete(ref);
  if (!watcher) return;
  try { watcher(outcome); } catch (error) {
    console.error({ ts: new Date().toISOString(), level: 'error', msg: 'internal turn settlement failed', error: String(error) });
  }
}

/**
 * The runtime's check where it takes the turn to start the model. A turn routed elsewhere is
 * settled `refused` here; a turn that passes is settled by the runtime only once it has really
 * started (`consumed`), or `refused` if it fails before that, so the delivery is offered again.
 */
export function refuseMisroutedTurn(message: StreamJsonMessage, live: NativeIdSource | undefined, tenantId: string): boolean {
  if (!internalTurnRefused(message, live)) return false;
  settleInternalTurn(message, 'refused');
  console.warn({ ts: new Date().toISOString(), level: 'warn', tenant: tenantId,
    msg: 'internal delivery refused: the conversation is no longer the one it was routed to' });
  return true;
}

/** The control-only result that closes a refused turn for the runtime manager. */
export function refusedTurnResult(hostAttemptRef?: string, correlationId?: string): StreamJsonMessage {
  return { type: 'result', subtype: INTERNAL_TURN_REFUSED, is_error: false, delivery_visibility: 'control',
    ...(hostAttemptRef ? { host_attempt_ref: hostAttemptRef } : {}),
    ...(correlationId ? { correlation_id: correlationId } : {}) };
}
