import { underlyingRuntime } from './runtime-identity.js';
import {AsyncLocalStorage} from 'node:async_hooks';
import {rolloverNativeSession} from './native-session-rollover.js';
import type { CommandRuntime } from './commands.js';
import type { PersistedSession, StateManager } from './types.js';
import { conversationLaunch } from './conversation-launch.js';
import { assertAdoptedConversationRuntime } from './runtime-usage-binding.js';

type Runtime = Pick<CommandRuntime, 'get' | 'spawn' | 'kill'>;
type Process = Awaited<ReturnType<Runtime['spawn']>>;
type Acquisition = Omit<Parameters<typeof conversationLaunch>[0], 'session'> & {
  runtime: Runtime;
  stateManager?: StateManager;
  /** The ingress boundary that requested this acquisition. A later human
   * input invalidates it before a process is adopted. */
  recovery?: {
    boundaryToken?: string;
    isCurrent(): boolean;
  };
};

export class ConversationAcquisitionError extends Error {
  constructor(readonly code: string) { super(code); }
}

function activeIdentity(active: PersistedSession['activeWorkItemRuntimeBinding']) {
  if (!active) return undefined;
  const { usageBinding, measurementBaselineReference, ...identity } = active;
  return identity;
}

/** Only identity/admission fields participate. Activity and UI bookkeeping may
 * change while a native process starts without changing the conversation. */
export function conversationConsumerProjection(session: PersistedSession | undefined): string {
  return JSON.stringify({
    poolKey: session?.poolKey, agentId: session?.agentId, sessionId: session?.sessionId, runtime: session?.runtime,
    active: activeIdentity(session?.activeWorkItemRuntimeBinding), pending: session?.pendingWorkItemEntry,
    terminal: session?.terminalWorkItemRuntimeBinding,
  });
}

type AcquisitionQueue = { tail: Promise<void>; restarts: number };
const pending = new WeakMap<Runtime, Map<string, AcquisitionQueue>>();

/** The address slot the current async operation holds, if any. */
const holding = new AsyncLocalStorage<{ runtime: Runtime; poolKey: string }>();

/** Every process mutation uses the same address queue, whichever wrapper of the runtime it comes through. */
function inConversationSlot<T>(runtime: Runtime, poolKey: string, destructive: boolean,
  operation: () => Promise<T>, allowReady = false): Promise<T> {
  const owner = underlyingRuntime(runtime);
  // The slot holder's own acquisitions run inside its slot instead of queueing behind it.
  const held = holding.getStore();
  if (held?.runtime === owner && held.poolKey === poolKey) return operation();
  let queue = pending.get(owner);
  if (!queue) { queue = new Map(); pending.set(owner, queue); }
  let entry = queue.get(poolKey);
  const live = runtime.get(poolKey);
  if (allowReady && !entry?.restarts && live && live.state !== 'dead') return operation();
  if (!entry) { entry = { tail: Promise.resolve(), restarts: 0 }; queue.set(poolKey, entry); }
  const owned = entry;
  if (destructive) owned.restarts++;
  const result = owned.tail.then(() => holding.run({ runtime: owner, poolKey }, operation));
  const tail = result.then(() => undefined, () => undefined).then(() => {
    if (destructive) owned.restarts--;
    if (owned.tail === tail) queue.delete(poolKey);
  });
  owned.tail = tail;
  return result;
}

/** Replace the conversation's process inside the same address queue as acquisition,
 * so no inbound turn starts or adopts it between the caller's checks and its change.
 * Acquisitions the operation itself awaits run in its slot; all others wait for it. */
export function replaceConversation<T>(runtime: Runtime, poolKey: string, operation: () => Promise<T>): Promise<T> {
  return inConversationSlot(runtime, poolKey, true, operation);
}

/** Acquire one admitted conversation through the shared address queue. */
export async function acquireConversation(input: Acquisition): Promise<Process> {
  if(input.stateManager?.getSession(input.poolKey)?.pendingNativeRollover){
    const runtime=input.runtime as CommandRuntime;
    if(!runtime.sendIdempotent)throw new ConversationAcquisitionError('native_rollover_transport_unavailable');
    const result=await rolloverNativeSession({...input,runtime,stateManager:input.stateManager});
    if(!result.session_handover?.accepted&&input.stateManager.getSession(input.poolKey)?.pendingNativeRollover)
      throw new ConversationAcquisitionError(
      result.error?.code??'native_rollover_pending');
  }
  return inConversationSlot(input.runtime, input.poolKey, input.intent === 'restart',
    () => acquireExclusive(input), input.intent !== 'restart');
}

async function acquireExclusive(input: Acquisition): Promise<Process> {
  const { runtime, stateManager, poolKey, recovery } = input;
  const session = () => stateManager?.getSession(poolKey);
  const projection = () => conversationConsumerProjection(session());
  const requireCurrent = () => {
    if (recovery && !recovery.isCurrent()) {
      throw new ConversationAcquisitionError('canonical_attachment_interrupted');
    }
  };
  const requireProjection = (expected: string) => {
    requireCurrent();
    if (projection() !== expected) throw new ConversationAcquisitionError('recovery_session_changed');
  };
  const adopt = (proc: Process | undefined): Process => {
    requireCurrent();
    if (!proc || proc.state === 'dead') throw new ConversationAcquisitionError('recovery_session_unavailable');
    if (input.intent === 'pi-command' && 'runtime' in proc && proc.runtime !== 'pi') {
      throw new Error('native Pi spawn returned a different runtime');
    }
    assertAdoptedConversationRuntime(session(), proc);
    return proc;
  };
  const spawn = async (launch: ReturnType<typeof conversationLaunch>, expected: string): Promise<Process> => {
    // Some adapters persist the selected identity while starting. Permit exactly
    // that initialization, captured before awaiting; every WI authority field
    // must still equal the original snapshot.
    const initialized = conversationConsumerProjection({
      ...session(), poolKey, agentId: input.agent.id, runtime: launch.runtime, sessionId: launch.sessionId,
    } as PersistedSession);
    const proc = await runtime.spawn(launch);
    requireCurrent();
    if (projection() !== expected && projection() !== initialized) {
      throw new ConversationAcquisitionError('recovery_session_changed');
    }
    return adopt(proc);
  };

  requireCurrent();
  if (input.intent === 'restart') {
    // Resolve the entire retained identity before stopping any live process.
    const expected = projection();
    const launch = conversationLaunch({ ...input, session: session() });
    if (runtime.get(poolKey)) await runtime.kill(poolKey);
    requireProjection(expected);
    return spawn(launch, expected);
  }

  const live = runtime.get(poolKey);
  if (live && live.state !== 'dead') return adopt(live);
  const persisted = session();
  return spawn(conversationLaunch({ ...input, session: persisted }), conversationConsumerProjection(persisted));
}
