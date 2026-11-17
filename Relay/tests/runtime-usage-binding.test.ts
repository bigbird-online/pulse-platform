import { describe, expect, it } from 'vitest';
import {
  RUNTIME_USAGE_BINDING_ENV,
  assertAdoptedConversationRuntime,
  buildRuntimeUsageBinding,
  persistedRuntimeUsageBinding,
  persistedRuntimeWorkspace,
  persistedWorkItemSession,
  runtimeAuthorityRef,
  runtimeUsageBindingEnvironment,
  validateRuntimeUsageBinding,
  type RuntimeUsageBinding,
} from '../src/runtime-usage-binding.js';

const PI_BINDING: RuntimeUsageBinding = {
  schema: 'pulse_runtime_usage_binding/v1',
  work_item_id: 'wi_20260822_730',
  agent_id: 'pulse',
  phase: 'work_item',
  epoch: 12,
  runtime: 'pi',
  procurement_source: 'chatgpt_subscription',
  issued_at: '2026-08-25T01:00:00Z',
  binding_sha256: '85fcc7240350aaf4929917cd6760bda4e8079a64f2411749766e04e25c0d9f06',
};

it('restores only the exact active-session workspace and leaves admission to spawn',()=>{
  const session={sessionId:'session',runtime:'pi',activeWorkItemRuntimeBinding:{
    schema:'pulse_active_work_item_runtime_binding/v1',workItemId:PI_BINDING.work_item_id,
    sessionId:'session',sessionRef:runtimeAuthorityRef('session'),runtime:'pi' as const,
    usageBinding:PI_BINDING,workspace:'/registered/exact',
  }};
  expect(persistedRuntimeWorkspace(session,'pi','session','/agent/default')).toBe('/registered/exact');
  expect(()=>persistedRuntimeWorkspace(session,'pi','other','/agent/default')).toThrow();
  expect(()=>persistedRuntimeWorkspace(session,'codex','session','/agent/default')).toThrow();
  expect(persistedRuntimeWorkspace(undefined,'pi','session','/agent/default')).toBe('/agent/default');
  expect(persistedWorkItemSession(session)).toEqual({runtime:'pi',sessionId:'session'});
  expect(persistedWorkItemSession(undefined)).toBeUndefined();
  expect(()=>persistedWorkItemSession({...session,sessionId:'other'})).toThrow();
  expect(()=>persistedWorkItemSession({...session,activeWorkItemRuntimeBinding:{
    ...session.activeWorkItemRuntimeBinding,sessionRef:runtimeAuthorityRef('other'),
  }})).toThrow('session reference');
});

const CODEX_BINDING: RuntimeUsageBinding = {
  ...PI_BINDING,
  runtime: 'codex',
  binding_sha256: 'd98b401739f14a2990790944020e0692fe839767d459d4ad4ca6753538293061',
};

describe('runtime usage host binding', () => {
  it('validates the cross-language canonical digest and emits one env contract', () => {
    expect(validateRuntimeUsageBinding(PI_BINDING)).toEqual(PI_BINDING);
    const env = runtimeUsageBindingEnvironment(PI_BINDING, 'pi');
    const serialized = env[RUNTIME_USAGE_BINDING_ENV] ?? '';
    expect(JSON.parse(serialized)).toEqual(PI_BINDING);
    for (const forbidden of ['session_id', 'thread_id', 'turn_id', 'message_id', 'cwd']) {
      expect(serialized).not.toContain(forbidden);
    }
  });

  it('binds v2 authority to one host session and rejects substitution', () => {
    const authorityRef = runtimeAuthorityRef('host-private-runtime');
    const binding = buildRuntimeUsageBinding({
      workItemId: PI_BINDING.work_item_id,
      agentId: PI_BINDING.agent_id,
      phase: PI_BINDING.phase,
      epoch: PI_BINDING.epoch,
      runtime: PI_BINDING.runtime,
      procurementSource: PI_BINDING.procurement_source,
      authorityRef,
      issuedAt: '2026-09-01T11:03:35.428Z',
    });
    expect(binding).toMatchObject({ schema: 'pulse_runtime_usage_binding/v2', authority_ref: authorityRef });
    expect(() => validateRuntimeUsageBinding({ ...binding, authority_ref: runtimeAuthorityRef('replacement') }))
      .toThrow(/digest does not match/);
  });

  it('binds managed Pi v3 to one Host surface and session', () => {
    const authorityRef = runtimeAuthorityRef('managed-private-runtime');
    const binding = buildRuntimeUsageBinding({
      workItemId: PI_BINDING.work_item_id,
      agentId: PI_BINDING.agent_id,
      phase: PI_BINDING.phase,
      epoch: PI_BINDING.epoch,
      runtime: 'pi',
      procurementSource: PI_BINDING.procurement_source,
      authorityRef,
      executionSurface: 'surface_v1_local_macos',
      issuedAt: '2026-09-07T12:00:00.000Z',
    });
    expect(binding).toMatchObject({
      schema: 'pulse_runtime_usage_binding/v3',
      authority_ref: authorityRef,
      execution_surface: 'surface_v1_local_macos',
    });
    expect(() => validateRuntimeUsageBinding({
      ...binding,
      execution_surface: 'surface_v1_cloud_linux_b',
    })).toThrow(/digest does not match/);
  });

  it('preserves Python and JavaScript canonical UTC precision', () => {
    for (const issuedAt of [
      '2026-09-01T11:03:35.428Z',
      '2026-09-01T11:03:35.428000Z',
    ]) {
      const candidate = buildRuntimeUsageBinding({
        workItemId: PI_BINDING.work_item_id,
        agentId: PI_BINDING.agent_id,
        phase: PI_BINDING.phase,
        epoch: PI_BINDING.epoch,
        runtime: 'pi',
        procurementSource: PI_BINDING.procurement_source,
        issuedAt,
      });
      expect(candidate.issued_at).toBe(issuedAt);
      expect(validateRuntimeUsageBinding(candidate)).toEqual(candidate);
    }
  });

  it('fails closed on tamper, extra identity, or runtime mismatch', () => {
    expect(() => validateRuntimeUsageBinding({ ...PI_BINDING, epoch: 13 })).toThrow(/digest/);
    expect(() => validateRuntimeUsageBinding({ ...PI_BINDING, thread_id: 'private' })).toThrow(/fields/);
    expect(() => validateRuntimeUsageBinding({ ...PI_BINDING, issued_at: '2026-08-25T09:00:00+08:00' })).toThrow(/timestamp/);
    expect(() => validateRuntimeUsageBinding({ ...PI_BINDING, issued_at: '2026-08-25T01:00:00.1234567Z' })).toThrow(/timestamp/);
    expect(() => validateRuntimeUsageBinding({ ...PI_BINDING, issued_at: '2026-02-30T01:00:00Z' })).toThrow(/timestamp/);
    expect(() => runtimeUsageBindingEnvironment(PI_BINDING, 'codex')).toThrow(/another runtime/);
    expect(runtimeUsageBindingEnvironment(CODEX_BINDING, 'codex')).toHaveProperty(RUNTIME_USAGE_BINDING_ENV);
  });

  it('restores only an exact persisted WorkItem/runtime/Session binding', () => {
    const session = {
      runtime: 'pi' as const,
      sessionId: 'private-target',
      activeWorkItemRuntimeBinding: {
        schema: 'pulse_active_work_item_runtime_binding/v1',
        workItemId: PI_BINDING.work_item_id,
        sessionId: 'private-target',
        sessionRef: runtimeAuthorityRef('private-target'),
        runtime: 'pi' as const,
        usageBinding: PI_BINDING,
      },
    };
    expect(persistedRuntimeUsageBinding(session, 'pi', 'private-target')).toEqual(PI_BINDING);
    expect(() => persistedRuntimeUsageBinding(session, 'pi', 'other-target')).toThrow(/does not match/);
    expect(() => persistedRuntimeUsageBinding({
      ...session,
      activeWorkItemRuntimeBinding: {
        ...session.activeWorkItemRuntimeBinding,
        workItemId: 'wi_20260822_999',
      },
    }, 'pi', 'private-target')).toThrow(/WorkItem authority/);
  });

  it('keeps unbound hosts un-attributed rather than inventing defaults', () => {
    expect(runtimeUsageBindingEnvironment(undefined, 'pi')).toEqual({});
    expect(persistedRuntimeUsageBinding(undefined, 'pi', 'private-target')).toBeUndefined();
  });
});


it('only a native guarded conversation can accept input while WI adoption is pending',()=>{
  const pending={sessionId:'original',pendingWorkItemEntry:{sessionId:'original',runtime:'pi',
    workItemId:'wi_20260922_856',sessionRef:runtimeAuthorityRef('original'),launchAuthorityState:'prepared'}} as never;
  expect(()=>assertAdoptedConversationRuntime(pending,{runtime:'pi',sessionId:'original'})).toThrow('not adopted');
  expect(()=>assertAdoptedConversationRuntime(pending,{runtime:'pi',sessionId:'original',conversationGuarded:true})).not.toThrow();
});
