import { createHash } from 'node:crypto';

export const RUNTIME_USAGE_BINDING_SCHEMA_V1 = 'pulse_runtime_usage_binding/v1' as const;
export const RUNTIME_USAGE_BINDING_SCHEMA_V2 = 'pulse_runtime_usage_binding/v2' as const;
export const RUNTIME_USAGE_BINDING_SCHEMA = 'pulse_runtime_usage_binding/v3' as const;
export const RUNTIME_USAGE_BINDING_ENV = 'PULSE_RUNTIME_USAGE_BINDING' as const;
export const RUNTIME_AUTHORITY_REF_ENV = 'PULSE_RUNTIME_AUTHORITY_REF' as const;

export type RuntimeUsageRuntime = 'pi' | 'codex' | 'chatgpt_work' | 'chatgpt_web' | 'cc' | 'cc_sdk';
export type RuntimeProcurementSource =
  | 'chatgpt_subscription'
  | 'openrouter_api'
  | 'claude_subscription'
  | 'cursor_subscription'
  | 'unknown';

export interface RuntimeUsageBinding {
  schema: typeof RUNTIME_USAGE_BINDING_SCHEMA | typeof RUNTIME_USAGE_BINDING_SCHEMA_V2 | typeof RUNTIME_USAGE_BINDING_SCHEMA_V1;
  work_item_id: string;
  agent_id: string;
  phase: string;
  epoch: number;
  runtime: RuntimeUsageRuntime;
  procurement_source: RuntimeProcurementSource;
  issued_at: string;
  authority_ref?: string;
  execution_surface?: string;
  binding_sha256: string;
}

const FIELDS_V1 = [
  'schema',
  'work_item_id',
  'agent_id',
  'phase',
  'epoch',
  'runtime',
  'procurement_source',
  'issued_at',
  'binding_sha256',
] as const;
const FIELDS_V2 = [...FIELDS_V1, 'authority_ref'] as const;
const FIELDS_V3 = [...FIELDS_V2, 'execution_surface'] as const;
const RUNTIMES = new Set<RuntimeUsageRuntime>(['pi', 'codex', 'chatgpt_work', 'chatgpt_web', 'cc', 'cc_sdk']);
const PROCUREMENT = new Set<RuntimeProcurementSource>([
  'chatgpt_subscription',
  'openrouter_api',
  'claude_subscription',
  'cursor_subscription',
  'unknown',
]);
const WI_RE = /^wi_\d{8}_\d{3}$/;
const TOKEN_RE = /^[a-z][a-z0-9_.-]{0,127}$/;
const SHA_RE = /^[0-9a-f]{64}$/;
const AUTHORITY_RE = /^session_v1_[0-9a-f]{64}$/;
const SURFACE_RE = /^surface_v1_[a-z0-9_]{1,96}$/;
const UTC_TIMESTAMP_RE = /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})(?:\.\d{1,6})?Z$/;

function canonical(value: Record<string, unknown>): string {
  const ordered: Record<string, unknown> = {};
  for (const key of Object.keys(value).sort()) ordered[key] = value[key];
  return JSON.stringify(ordered);
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return !!value && typeof value === 'object' && !Array.isArray(value);
}

function isCanonicalUtcTimestamp(value: unknown): value is string {
  if (typeof value !== 'string') return false;
  const match = UTC_TIMESTAMP_RE.exec(value);
  if (!match) return false;
  const parsed = new Date(value);
  if (!Number.isFinite(parsed.getTime())) return false;
  const [, year, month, day, hour, minute, second] = match;
  return parsed.getUTCFullYear() === Number(year)
    && parsed.getUTCMonth() + 1 === Number(month)
    && parsed.getUTCDate() === Number(day)
    && parsed.getUTCHours() === Number(hour)
    && parsed.getUTCMinutes() === Number(minute)
    && parsed.getUTCSeconds() === Number(second);
}

export function buildRuntimeUsageBinding(input: {
  workItemId: string;
  agentId: string;
  phase: string;
  epoch: number;
  runtime: RuntimeUsageRuntime;
  procurementSource: RuntimeProcurementSource;
  authorityRef?: string;
  executionSurface?: string;
  issuedAt?: string;
}): RuntimeUsageBinding {
  if (input.executionSurface && !input.authorityRef) throw new Error('Runtime usage surface requires session authority');
  const schema = input.executionSurface
    ? RUNTIME_USAGE_BINDING_SCHEMA
    : input.authorityRef
      ? RUNTIME_USAGE_BINDING_SCHEMA_V2
      : RUNTIME_USAGE_BINDING_SCHEMA_V1;
  const unsigned = {
    schema,
    work_item_id: input.workItemId,
    agent_id: input.agentId,
    phase: input.phase,
    epoch: input.epoch,
    runtime: input.runtime,
    procurement_source: input.procurementSource,
    issued_at: input.issuedAt ?? new Date().toISOString(),
    ...(input.authorityRef ? { authority_ref: input.authorityRef } : {}),
    ...(input.executionSurface ? { execution_surface: input.executionSurface } : {}),
  };
  return validateRuntimeUsageBinding({
    ...unsigned,
    binding_sha256: createHash('sha256').update(canonical(unsigned)).digest('hex'),
  });
}

export function validateRuntimeUsageBinding(value: unknown): RuntimeUsageBinding {
  if (!isRecord(value)) throw new Error('Runtime usage binding fields are invalid');
  const expectedFields = value.schema === RUNTIME_USAGE_BINDING_SCHEMA
    ? FIELDS_V3
    : value.schema === RUNTIME_USAGE_BINDING_SCHEMA_V2
      ? FIELDS_V2
      : value.schema === RUNTIME_USAGE_BINDING_SCHEMA_V1
        ? FIELDS_V1
        : undefined;
  if (!expectedFields) throw new Error('Runtime usage binding schema is unsupported');
  if (Object.keys(value).sort().join('\0') !== [...expectedFields].sort().join('\0')) {
    throw new Error('Runtime usage binding fields are invalid');
  }
  if (typeof value.work_item_id !== 'string' || !WI_RE.test(value.work_item_id)) throw new Error('Runtime usage binding WI is invalid');
  if (typeof value.agent_id !== 'string' || !TOKEN_RE.test(value.agent_id)) throw new Error('Runtime usage binding agent is invalid');
  if (typeof value.phase !== 'string' || !TOKEN_RE.test(value.phase)) throw new Error('Runtime usage binding phase is invalid');
  if (!Number.isSafeInteger(value.epoch) || Number(value.epoch) < 1) throw new Error('Runtime usage binding epoch is invalid');
  if (typeof value.runtime !== 'string' || !RUNTIMES.has(value.runtime as RuntimeUsageRuntime)) throw new Error('Runtime usage binding runtime is invalid');
  if (typeof value.procurement_source !== 'string' || !PROCUREMENT.has(value.procurement_source as RuntimeProcurementSource)) {
    throw new Error('Runtime usage binding procurement source is invalid');
  }
  if (!isCanonicalUtcTimestamp(value.issued_at)) {
    throw new Error('Runtime usage binding timestamp is invalid');
  }
  if ((value.schema === RUNTIME_USAGE_BINDING_SCHEMA || value.schema === RUNTIME_USAGE_BINDING_SCHEMA_V2) && (
    typeof value.authority_ref !== 'string' || !AUTHORITY_RE.test(value.authority_ref)
  )) throw new Error('Runtime usage binding authority is invalid');
  if (value.schema === RUNTIME_USAGE_BINDING_SCHEMA && (
    typeof value.execution_surface !== 'string' || !SURFACE_RE.test(value.execution_surface)
  )) throw new Error('Runtime usage binding surface is invalid');
  if (typeof value.binding_sha256 !== 'string' || !SHA_RE.test(value.binding_sha256)) throw new Error('Runtime usage binding digest is invalid');
  const unsigned = Object.fromEntries(Object.entries(value).filter(([key]) => key !== 'binding_sha256'));
  const expected = createHash('sha256').update(canonical(unsigned)).digest('hex');
  if (expected !== value.binding_sha256) throw new Error('Runtime usage binding digest does not match');
  return value as unknown as RuntimeUsageBinding;
}

export function runtimeUsageBindingEnvironment(
  binding: RuntimeUsageBinding | undefined,
  expectedRuntime: RuntimeUsageRuntime,
): NodeJS.ProcessEnv {
  if (!binding) return {};
  const validated = validateRuntimeUsageBinding(binding);
  if (validated.runtime !== expectedRuntime) throw new Error('Runtime usage binding targets another runtime');
  return { [RUNTIME_USAGE_BINDING_ENV]: canonical(validated as unknown as Record<string, unknown>) };
}

/** Conversation runtimes that can carry a WI runtime usage binding. */
export type BoundConversationRuntime = 'pi' | 'codex' | 'cc' | 'cc-sdk';

/** Relay 的会话运行时 id 与 ComputeCost/SCL 的用量运行时名：仅 cc-sdk 拼写不同。 */
export function usageRuntimeForConversation(runtime: BoundConversationRuntime): RuntimeUsageRuntime {
  return runtime === 'cc-sdk' ? 'cc_sdk' : runtime;
}

export function isBoundConversationRuntime(runtime: unknown): runtime is BoundConversationRuntime {
  return runtime === 'pi' || runtime === 'codex' || runtime === 'cc' || runtime === 'cc-sdk';
}

const CLAUDE_PROCUREMENT_ENV = 'PULSE_PROCUREMENT_SOURCE_CC' as const;

/**
 * 采购来源按运行时决定：CC/cc-sdk 走 Claude 订阅（可用 PULSE_PROCUREMENT_SOURCE_CC
 * 覆盖），其余运行时沿用主机级 PULSE_PROCUREMENT_SOURCE。价格依据仍是 ComputeCost 的
 * OpenRouter 目录，这里只记录凭据/计费路径。
 */
export function runtimeProcurementSource(
  env: NodeJS.ProcessEnv,
  runtime?: RuntimeUsageRuntime,
): RuntimeProcurementSource | undefined {
  const value = runtime === 'cc' || runtime === 'cc_sdk'
    ? env[CLAUDE_PROCUREMENT_ENV]?.trim() || 'claude_subscription'
    : env['PULSE_PROCUREMENT_SOURCE']?.trim() || 'unknown';
  return PROCUREMENT.has(value as RuntimeProcurementSource) ? value as RuntimeProcurementSource : undefined;
}

export function runtimeAuthorityRef(rawSessionId: string): string {
  if (!rawSessionId.trim()) throw new Error('Runtime session authority is unavailable');
  return `session_v1_${createHash('sha256').update(['pulse-session-v1', rawSessionId].join('\0')).digest('hex')}`;
}

interface PersistedBoundRuntime {
  terminalWorkItemRuntimeBinding?: {binding:{runtime:BoundConversationRuntime;sessionId:string}};
  runtime?: string;
  sessionId: string;
  pendingWorkItemEntry?: {sessionId:string;launchAuthorityState?:string};
  activeWorkItemRuntimeBinding?: {
    schema: string;
    workItemId: string;
    sessionId: string;
    sessionRef: string;
    runtime: BoundConversationRuntime;
    workspace?: string;
    usageBinding?: RuntimeUsageBinding;
  };
}

/** Retained routing identity is not a measurement receipt or an execution grant. */
export function assertPersistedRuntimeIdentity(session: PersistedBoundRuntime | undefined,
  runtime: string, sessionId: string): void {
  const active = session?.activeWorkItemRuntimeBinding;
  if (!active) return;
  if (active.schema !== 'pulse_active_work_item_runtime_binding/v1'
    || active.sessionId !== sessionId || active.runtime !== runtime
    || session?.sessionId !== sessionId || session.runtime !== runtime) {
    throw new Error('Persisted runtime identity does not match the resumed Session');
  }
  if (active.sessionRef !== runtimeAuthorityRef(sessionId)) {
    throw new Error('Persisted WorkItem session reference does not match the resumed Session');
  }
}

/** A live candidate is not automatically the conversation's active WI runtime. */
export function assertAdoptedConversationRuntime(session:PersistedBoundRuntime|undefined,
  runtime:{sessionId:string;runtime?:string;usageBindingSha256?:string;workspace?:string;conversationGuarded?:boolean}):void {
  const pending=session?.pendingWorkItemEntry;
  const active=session?.activeWorkItemRuntimeBinding;
  if(pending&&pending.sessionId===runtime.sessionId&&pending.launchAuthorityState!=='active'
    &&active?.sessionId!==runtime.sessionId && runtime.conversationGuarded !== true){
    throw new Error('WorkItem target has not adopted canonical execution authority');
  }
  if(!active)return;
  assertPersistedRuntimeIdentity(session,runtime.runtime??active.runtime,runtime.sessionId);
}

/** A resumed WI keeps its bound runtime and Session; route defaults are not a migration. */
export function persistedWorkItemSession(session:PersistedBoundRuntime|undefined):
  {runtime:BoundConversationRuntime;sessionId:string}|undefined {
  const active=session?.activeWorkItemRuntimeBinding;
  if(!session)return undefined;
  if(!active){
    const historical=session.terminalWorkItemRuntimeBinding?.binding;
    return historical&&historical.sessionId===session.sessionId&&historical.runtime===session.runtime
      ?{runtime:historical.runtime,sessionId:session.sessionId}:undefined;
  }
  assertPersistedRuntimeIdentity(session,active.runtime,session.sessionId);
  return {runtime:active.runtime,sessionId:session.sessionId};
}

/** Selection only: the spawn-time Harness admission still validates this exact path. */
export function persistedRuntimeWorkspace(session:PersistedBoundRuntime|undefined,
  runtime:string,sessionId:string,fallback:string):string {
  const active=session?.activeWorkItemRuntimeBinding;
  assertPersistedRuntimeIdentity(session,runtime,sessionId);
  return active?.workspace??fallback;
}

/** Measurement-only consumer: invalid observations still cannot be collected. */
export function persistedRuntimeUsageBinding(
  session: PersistedBoundRuntime | undefined,
  runtime: BoundConversationRuntime,
  sessionId: string,
): RuntimeUsageBinding | undefined {
  const active = session?.activeWorkItemRuntimeBinding;
  if (!active) return undefined;
  assertPersistedRuntimeIdentity(session,runtime,sessionId);
  const binding = validateRuntimeUsageBinding(active.usageBinding);
  if (binding.work_item_id !== active.workItemId || binding.runtime !== usageRuntimeForConversation(runtime)) {
    throw new Error('Persisted runtime usage binding does not match active WorkItem authority');
  }
  return binding;
}
