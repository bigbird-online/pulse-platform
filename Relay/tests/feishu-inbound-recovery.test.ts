import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { mkdtempSync, readFileSync, readdirSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { FeishuInboundRecovery, type FeishuInboundDispatchEvidence, type FeishuInboundRecoveryRequest } from '../src/feishu-inbound-recovery.js';
import { recordFeishuRecoveryBoundary } from '../src/feishu-inbound-recovery.js';
import { invalidateHumanInput, latestHumanInput, recordHumanInput } from '../src/scl-human-decision.js';
import { buildRuntimeUsageBinding, runtimeAuthorityRef } from '../src/runtime-usage-binding.js';
import type { PersistedSession } from '../src/types.js';

describe('durable original Feishu input after a proven startup failure', () => {
  let root: string;
  let input: FeishuInboundRecoveryRequest;
  let session: PersistedSession;
  let busy: boolean;
  const poolKey = 'user:bot:feishu';
  const source = { channel: 'feishu' as const, eventId: 'actual-inbound-event', humanRef: 'user',
    message: '你的上下文是否清理干净了？是否需要 rollover 一次，新 session 开始全面审核迁移工作？' };
  const ensureRuntime = vi.fn<(...args: [string, string, string]) => Promise<void>>();
  const dispatch = vi.fn<(...args: [string, string, string]) => Promise<void>>();
  const evidence = vi.fn<(...args: [string, string, string, string]) => Promise<FeishuInboundDispatchEvidence>>();
  const env = () => ({ SCL_HUMAN_DECISION_KEY_FILE: join(root, 'key'), PULSE_CORE_HUMAN_INPUTS: join(root, 'human-inputs') });
  const manager = () => new FeishuInboundRecovery({ agentId: 'bot', env: env(),
    session: () => session, owns: key => key === poolKey, busy: () => busy,
    ensureRuntime, dispatch, readDispatchEvidence: evidence });
  const saved = () => {
    const path = join(root, 'feishu-inbound-recoveries', readdirSync(join(root, 'feishu-inbound-recoveries'))[0]!);
    return { path, record: JSON.parse(readFileSync(path, 'utf8')) };
  };
  const newer = (text = 'Follow the newest request') => {
    const ref = recordHumanInput({ ...source, eventId: 'newer-event', message: text }, poolKey, env());
    recordFeishuRecoveryBoundary(poolKey, 'newer-event', env(), { kind: 'input' });
    return ref;
  };
  beforeEach(() => {
    root = mkdtempSync(join(tmpdir(), 'feishu-inbound-')); busy = false;
    const ref = recordHumanInput(source, poolKey, env());
    recordFeishuRecoveryBoundary(poolKey, source.eventId, env(), { kind: 'input' });
    input = { poolKey, sessionId: 'original-native', humanInputRef: ref, code: 'host_recovery_scope_active' };
    const usageBinding = buildRuntimeUsageBinding({ workItemId: 'wi_20260823_184', agentId: 'pulse',
      epoch: 5, phase: 'migration_plan_pr_review', runtime: 'pi', procurementSource: 'unknown',
      authorityRef: runtimeAuthorityRef(input.sessionId), executionSurface: 'surface_v1_test' });
    session = { poolKey, agentId: 'bot', sessionId: input.sessionId, runtime: 'pi', status: 'active',
      turnCount: 0, maxTurns: 100, lastActivity: 1,
      activeWorkItemRuntimeBinding: { schema: 'pulse_active_work_item_runtime_binding/v1',
        workItemId: 'wi_20260823_184', sessionId: input.sessionId, sessionRef: usageBinding.authority_ref!,
        runtime: 'pi', workspace: '/original/work', usageBinding } };
    ensureRuntime.mockReset().mockResolvedValue(undefined);
    dispatch.mockReset().mockResolvedValue(undefined);
    evidence.mockReset().mockResolvedValue('proven_unsent');
  });
  afterEach(() => { rmSync(root, { recursive: true, force: true }); });

  it('recovers the exact authenticated text once across restart without minting another human input', async () => {
    const original = join(root, 'human-inputs', input.humanInputRef + '.json');
    const before = readFileSync(original);
    const pending = manager().recordStartupFailure(input);
    expect(pending.state).toBe('pending');
    invalidateHumanInput(poolKey, env());
    const recovered = await manager().recover(input);
    expect(recovered.state).toBe('accepted');
    expect(dispatch).toHaveBeenCalledWith(poolKey, expect.stringContaining(source.message), pending.requestId);
    const content = dispatch.mock.calls[0]![1];
    expect(content.startsWith(source.message)).toBe(true);
    expect(content).toContain(`Host human input reference: ${input.humanInputRef}`);
    expect(content).toContain(`FEISHU_INBOUND_RECOVERY_REQUEST=${pending.requestId}`);
    expect(await manager().recover(input)).toEqual(recovered);
    expect(manager().recordStartupFailure(input)).toEqual(recovered);
    expect(dispatch).toHaveBeenCalledTimes(1);
    expect(readFileSync(original)).toEqual(before);
    expect(readdirSync(join(root, 'human-inputs'))).toEqual([input.humanInputRef + '.json']);
    expect(latestHumanInput(poolKey, env())).toBeUndefined();
  });
  it('retains known-unsent work through another startup failure and retries after recovery', async () => {
    manager().recordStartupFailure(input);
    ensureRuntime.mockRejectedValueOnce(Object.assign(new Error('private detail'), { code: 'host_recovery_scope_active' }));
    expect(await manager().recover(input)).toMatchObject({ state: 'pending', error: 'host_recovery_scope_active' });
    expect(dispatch).not.toHaveBeenCalled();
    expect(await manager().recover(input)).toMatchObject({ state: 'accepted' });
    expect(dispatch).toHaveBeenCalledTimes(1);
  });
  it('does not replay input already present in the original native history', async () => {
    manager().recordStartupFailure(input); evidence.mockResolvedValue('accepted');
    expect(await manager().recover(input)).toMatchObject({ state: 'accepted' });
    expect(dispatch).not.toHaveBeenCalled();
  });
  it.each(['unknown', 'proven_unsent'] as const)('does not replay an uncertain send when later evidence is %s', async later => {
    manager().recordStartupFailure(input);
    dispatch.mockRejectedValueOnce(new Error('RPC acknowledgement lost'));
    expect(await manager().recover(input)).toMatchObject({ state: 'uncertain', error: 'inbound_dispatch_unproven' });
    evidence.mockResolvedValue(later);
    expect(await manager().recover(input)).toMatchObject({ state: 'uncertain' });
    expect(dispatch).toHaveBeenCalledTimes(1);
    evidence.mockResolvedValue('accepted');
    expect(await manager().recover(input)).toMatchObject({ state: 'accepted' });
    expect(dispatch).toHaveBeenCalledTimes(1);
  });
  it('treats a persisted dispatching crash as uncertain even without a native marker', async () => {
    manager().recordStartupFailure(input);
    const { path, record } = saved(); record.state = 'dispatching'; writeFileSync(path, JSON.stringify(record));
    expect(await manager().recover(input)).toMatchObject({ state: 'uncertain' });
    expect(dispatch).not.toHaveBeenCalled();
  });
  it.each(['ordinary', 'stop'])('honors a newer %s boundary without replacing it', async mode => {
    manager().recordStartupFailure(input);
    const ref = mode === 'ordinary' ? newer() : input.humanInputRef;
    if (mode === 'stop') recordFeishuRecoveryBoundary(poolKey, 'card-menu:stop', env(), { kind: 'control', control: 'stop' });
    expect(await manager().recover(input)).toMatchObject({ state: 'superseded' });
    expect(ensureRuntime).not.toHaveBeenCalled(); expect(dispatch).not.toHaveBeenCalled();
    expect(latestHumanInput(poolKey, env())).toBe(ref);
  });
  it.each(['runtime', 'native', 'workspace', 'pending', 'terminal'])('refuses changed %s ownership', async mode => {
    manager().recordStartupFailure(input);
    if (mode === 'runtime') session.runtime = 'cc';
    if (mode === 'native') session.sessionId = 'replacement';
    if (mode === 'workspace') session.activeWorkItemRuntimeBinding!.workspace = '/different/work';
    if (mode === 'pending') session.pendingWorkItemEntry = {} as never;
    if (mode === 'terminal') session.terminalWorkItemRuntimeBinding = {} as never;
    expect(await manager().recover(input)).toMatchObject({ state: 'superseded', error: 'inbound_session_changed' });
    expect(dispatch).not.toHaveBeenCalled();
  });
  it('metering changes cannot withhold the original pending input', async () => {
    manager().recordStartupFailure(input);
    session.activeWorkItemRuntimeBinding!.usageBinding.epoch = 6;
    expect(await manager().recover(input)).toMatchObject({ state: 'accepted' });
    expect(dispatch).toHaveBeenCalledTimes(1);
  });
  it('counts pre-spawn failures and waits for a repaired runtime after a bounded budget', async () => {
    let revision = 'broken-runtime';
    const current = new FeishuInboundRecovery({ agentId: 'bot', env: env(),
      session: () => session, owns: () => true, busy: () => false,
      runtimeRevision: () => revision, ensureRuntime, dispatch, readDispatchEvidence: evidence });
    current.recordStartupFailure(input);
    ensureRuntime.mockRejectedValue(Object.assign(new Error('private path'), { code: 'runtime_start_failed' }));
    for (let i = 0; i < 20; i++) await current.recover(input);
    expect(ensureRuntime).toHaveBeenCalledTimes(6);
    expect(saved().record).toMatchObject({ startupAttempts: 6, requiresRuntimeRepair: true,
      state: 'pending', error: 'runtime_start_failed' });
    expect(saved().record.nextAttemptAt).toBeGreaterThan(Date.now() + 400_000);
    revision = 'repaired-runtime'; ensureRuntime.mockResolvedValue(undefined);
    await current.resumePending();
    expect(saved().record.state).toBe('accepted');
    expect(dispatch).toHaveBeenCalledTimes(1);
  });
  it('permits additive canonical baseline readback while preserving the active identity', async () => {
    manager().recordStartupFailure(input);
    session.activeWorkItemRuntimeBinding!.measurementBaselineReference = { status: 'open' } as never;
    expect(await manager().recover(input)).toMatchObject({ state: 'accepted' });
  });
  it('waits for busy work without interrupting it', async () => {
    manager().recordStartupFailure(input); busy = true;
    expect(await manager().recover(input)).toMatchObject({ state: 'pending', error: 'conversation_busy' });
    expect(ensureRuntime).not.toHaveBeenCalled(); expect(dispatch).not.toHaveBeenCalled();
    busy = false; expect(await manager().recover(input)).toMatchObject({ state: 'accepted' });
  });
  it.each(['startup', 'evidence'])('rechecks a newer control after the %s await', async mode => {
    const stop = () => recordFeishuRecoveryBoundary(poolKey, 'stop-during-await', env(), { kind: 'control', control: 'stop' });
    manager().recordStartupFailure(input);
    if (mode === 'startup') ensureRuntime.mockImplementation(async () => { stop(); });
    else evidence.mockImplementation(async () => { stop(); return 'proven_unsent'; });
    expect(await manager().recover(input)).toMatchObject({ state: 'superseded' });
    expect(dispatch).not.toHaveBeenCalled();
  });
  it('serializes simultaneous retries so only one native prompt is accepted', async () => {
    manager().recordStartupFailure(input);
    let release!: () => void;
    ensureRuntime.mockReturnValue(new Promise<void>(resolve => { release = resolve; }));
    const current = manager(); const first = current.recover(input); const second = current.recover(input);
    release(); expect(await first).toEqual(await second);
    expect(dispatch).toHaveBeenCalledTimes(1);
  });
  it('does not let a replaced manager overwrite or dispatch a newer journal attempt', async () => {
    manager().recordStartupFailure(input);
    let release!: () => void;
    ensureRuntime.mockReturnValue(new Promise<void>(resolve => { release = resolve; }));
    const old = manager().recover(input);
    const { path, record } = saved(); record.state = 'accepted'; writeFileSync(path, JSON.stringify(record));
    release();
    expect(await old).toMatchObject({ state: 'accepted' });
    expect(dispatch).not.toHaveBeenCalled();
    expect(saved().record.state).toBe('accepted');
  });
  it('never invents a pending request or interprets a slash command as recoverable text', async () => {
    await expect(manager().recover(input)).rejects.toMatchObject({ code: 'inbound_recovery_record_required' });
    input.humanInputRef = newer('/rollover');
    expect(() => manager().recordStartupFailure(input)).toThrow('inbound_source_invalid');
    expect(dispatch).not.toHaveBeenCalled();
  });
  it.each(['owned', 'foreign'])('isolates a malformed %s receipt and resumes the next valid input', async owner => {
    manager().recordStartupFailure(input);
    const { record } = saved();
    const badId = 'operation_v1_feishu_inbound_' + '0'.repeat(64);
    const badPath = join(root, 'feishu-inbound-recoveries', badId + '.json');
    const raw = JSON.stringify({ ...record, requestId: badId,
      poolKey: owner === 'foreign' ? 'another:agent:feishu' : poolKey,
      privateDetail: 'never-log-original-private-content' });
    writeFileSync(badPath, raw);
    const warning = vi.spyOn(console, 'warn').mockImplementation(() => {});
    try {
      await manager().resumePending();
      expect(dispatch).toHaveBeenCalledTimes(1);
      expect(await manager().recover(input)).toMatchObject({ state: 'accepted' });
      expect(readFileSync(badPath, 'utf8')).toBe(raw);
      expect(warning).toHaveBeenCalledWith('Feishu inbound recovery deferred', {
        requestId: badId, code: 'inbound_recovery_record_invalid',
      });
      expect(JSON.stringify(warning.mock.calls)).not.toContain('never-log-original-private-content');
    } finally { warning.mockRestore(); }
  });
});
