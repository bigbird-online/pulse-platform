import { buildRuntimeUsageBinding } from '../src/runtime-usage-binding.js';
import { EventEmitter } from 'node:events';
import { existsSync, mkdirSync, mkdtempSync, readFileSync, readdirSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { PassThrough } from 'node:stream';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import {
  buildCodexCommand,
  buildCodexEnv,
  codexMessageToPrompt,
  createCodexConversationRuntime,
} from '../src/codex-conversation-runtime.js';
import { createConversationRuntimeManager } from '../src/conversation-runtime.js';
import { createCollaborationCapture } from '../src/collaboration-capture.js';
import { createStateManager as createRealStateManager } from '../src/state.js';
import { replayPendingTurns } from '../src/shutdown-interrupts.js';
import { resolveCodexAdditionalWritableRoots } from '../src/config-paths.js';
import type { ModelCatalog } from '../src/model-catalog.js';
import type { StateManager, StreamJsonMessage } from '../src/types.js';

/** Minimal in-memory ModelCatalog stub for runtime-compat tests. */
function createCatalogStub(
  byModel: Record<string, { provider: string; protocol: 'anthropic' | 'openai' }>,
): ModelCatalog {
  return {
    entries: [],
    providers: undefined,
    findById: (id: string) => (byModel[id] ? { id, display_name: id, provider: byModel[id].provider } : undefined),
    findProvider: () => undefined,
    getProviderForModel: (id: string) =>
      byModel[id] ? { protocol: byModel[id].protocol, base_url_env: 'X_BASE_URL', api_key_pool: ['X_KEY'] } : undefined,
    isProviderAvailable: () => true,
    validateModelId: (input: string) => ({ valid: !!byModel[input], resolvedId: input }),
    listIds: () => [],
    runtimeForModel: (id: string) =>
      byModel[id] ? (byModel[id].protocol === 'openai' ? 'codex' : 'cc') : undefined,
    getDefaultModelForProtocol: () => undefined,
    reload: () => {},
  };
}

interface MockProcess extends EventEmitter {
  stdin: PassThrough;
  stdout: PassThrough;
  stderr: PassThrough;
  pid: number;
  killed: boolean;
  kill: ReturnType<typeof vi.fn>;
}

let mockSpawn: ReturnType<typeof vi.fn>;
let nextPid = 4100;

vi.mock('node:child_process', () => ({
  spawn: (...args: unknown[]) => mockSpawn(...args),
}));

function createMockProcess(options: { exitOnKill?: boolean } = {}): MockProcess {
  const proc = new EventEmitter() as MockProcess;
  proc.stdin = new PassThrough();
  proc.stdout = new PassThrough();
  proc.stderr = new PassThrough();
  proc.pid = nextPid++;
  proc.killed = false;
  proc.kill = vi.fn((signal?: string) => {
    proc.killed = true;
    if (options.exitOnKill !== false) {
      proc.emit('exit', signal === 'SIGKILL' ? 137 : 1, signal ?? 'SIGTERM');
    }
    return true;
  });
  return proc;
}

function emitJsonl(proc: MockProcess, events: unknown[]): void {
  for (const event of events) {
    proc.stdout.write(`${JSON.stringify(event)}\n`);
  }
}

async function tick(): Promise<void> {
  await new Promise((resolve) => setImmediate(resolve));
}

function createStateManager(existing = true): StateManager & { updates: unknown[]; flushed: number } {
  const updates: unknown[] = [];
  const stateManager = {
    updates,
    flushed: 0,
    load: vi.fn(),
    getSession: vi.fn((poolKey: string) => existing ? {
      poolKey,
      agentId: 'aki',
      runtime: 'codex' as const,
      sessionId: 'logical-session',
      turnCount: 0,
      maxTurns: 100,
      lastActivity: 1,
      status: 'active' as const,
    } : undefined),
    updateSession: vi.fn((_poolKey: string, update: unknown) => {
      updates.push(update);
    }),
    removeSession: vi.fn(),
    getAllSessions: vi.fn(() => ({})),
    flush: vi.fn(() => {
      stateManager.flushed += 1;
    }),
    dispose: vi.fn(),
  };
  return stateManager as unknown as StateManager & { updates: unknown[]; flushed: number };
}

describe('Codex command builder', () => {
  it('builds first-turn codex exec command with stdin prompt placeholder', () => {
    const spec = buildCodexCommand({
      codexBin: 'codex-test',
      workspace: '/workspace',
      outputPath: '/tmp/last-message.txt',
    });

    expect(spec.command).toBe('codex-test');
    expect(spec.args).toEqual([
      '-a', 'never',
      '-s', 'workspace-write',
      '-m', 'gpt-6-astra',
      '-c', 'model_reasoning_effort="xhigh"',
      '-C', '/workspace',
      'exec',
      '--skip-git-repo-check',
      '--json',
      '--output-last-message', '/tmp/last-message.txt',
      '-',
    ]);
    expect(spec.cwd).toBe('/workspace');
  });

  it('injects privacy-safe WI attribution without exposing provider thread identity', () => {
    const env = buildCodexEnv(undefined, {
      schema: 'pulse_runtime_usage_binding/v1',
      work_item_id: 'wi_20260822_730',
      agent_id: 'pulse',
      phase: 'work_item',
      epoch: 12,
      runtime: 'codex',
      procurement_source: 'chatgpt_subscription',
      issued_at: '2026-08-25T01:00:00Z',
      binding_sha256: 'd98b401739f14a2990790944020e0692fe839767d459d4ad4ca6753538293061',
    });
    const serialized = env.PULSE_RUNTIME_USAGE_BINDING ?? '';
    expect(JSON.parse(serialized)).toMatchObject({
      work_item_id: 'wi_20260822_730',
      phase: 'work_item',
      epoch: 12,
      runtime: 'codex',
      procurement_source: 'chatgpt_subscription',
    });
    expect(serialized).not.toContain('thread_id');
    expect(serialized).not.toContain('session_id');
  });

  it('skips a binding issued for another runtime instead of failing the spawn', () => {
    const warn = vi.spyOn(console, 'warn').mockImplementation(() => {});
    const binding = buildRuntimeUsageBinding({ workItemId: 'wi_20260822_730', agentId: 'pulse', phase: 'work_item', epoch: 1,
      runtime: 'cc', procurementSource: 'claude_subscription' });
    expect(buildCodexEnv(undefined, binding).PULSE_RUNTIME_USAGE_BINDING).toBeUndefined();
    expect(warn).toHaveBeenCalledOnce();
    warn.mockRestore();
  });

  it('builds resume command with thread id', () => {
    const spec = buildCodexCommand({
      workspace: '/workspace',
      outputPath: '/tmp/last-message.txt',
      threadId: 'thread-123',
    });

    expect(spec.args).toContain('resume');
    expect(spec.args).toContain('thread-123');
    expect(spec.args.indexOf('resume')).toBeLessThan(spec.args.indexOf('--skip-git-repo-check'));
  });

  it('adds extra writable roots before exec', () => {
    const spec = buildCodexCommand({
      workspace: '/workspace',
      outputPath: '/tmp/last-message.txt',
      additionalWritableRoots: ['/Users/owner/Coevolution', '/Users/owner/Coevolution'],
    });

    expect(spec.args).toEqual([
      '-a', 'never',
      '-s', 'workspace-write',
      '-m', 'gpt-6-astra',
      '-c', 'model_reasoning_effort="xhigh"',
      '-C', '/workspace',
      '--add-dir', '/Users/owner/Coevolution',
      'exec',
      '--skip-git-repo-check',
      '--json',
      '--output-last-message', '/tmp/last-message.txt',
      '-',
    ]);
  });

  it('uses the dangerous bypass flag when YOLO mode is enabled', () => {
    const spec = buildCodexCommand({
      workspace: '/workspace',
      outputPath: '/tmp/last-message.txt',
      yoloMode: true,
    });

    expect(spec.args).toContain('--dangerously-bypass-approvals-and-sandbox');
    expect(spec.args).toContain('gpt-6-astra');
    expect(spec.args).toContain('model_reasoning_effort="xhigh"');
    expect(spec.args).not.toContain('-a');
    expect(spec.args).not.toContain('never');
    expect(spec.args).not.toContain('-s');
    expect(spec.args).not.toContain('workspace-write');
    expect(spec.args).toContain('exec');
  });

  it('keeps explicit YOLO overrides separate from defaults', () => {
    const runtime = createCodexConversationRuntime();

    runtime.setYoloDefaultMode?.('owner:pulse', true);
    expect(runtime.getYoloMode?.('owner:pulse')).toBe(true);
    expect(runtime.getYoloOverrideMode?.('owner:pulse')).toBeUndefined();

    runtime.setYoloMode?.('owner:pulse', false);
    expect(runtime.getYoloMode?.('owner:pulse')).toBe(false);
    expect(runtime.getYoloOverrideMode?.('owner:pulse')).toBe(false);

    runtime.clearYoloMode?.('owner:pulse');
    expect(runtime.getYoloMode?.('owner:pulse')).toBe(true);
    expect(runtime.getYoloOverrideMode?.('owner:pulse')).toBeUndefined();
  });

  it('adds image inputs before stdin prompt when attachments are present', () => {
    const spec = buildCodexCommand({
      workspace: '/workspace',
      outputPath: '/tmp/last-message.txt',
      imagePaths: ['/tmp/photo.jpg'],
    });

    expect(spec.args.slice(-4)).toEqual(['-i', '/tmp/photo.jpg', '--', '-']);
  });

  it('converts text blocks and rejects non-text P0 content', () => {
    expect(codexMessageToPrompt({ type: 'human', content: [{ type: 'text', text: 'a' }, { type: 'text', text: 'b' }] }))
      .toBe('a\n\nb');
    expect(() => codexMessageToPrompt({ type: 'human', content: [{ type: 'image' }] }))
      .toThrow('only supports text');
  });
});

describe('CodexConversationRuntime', () => {
  beforeEach(() => {
    mockSpawn = vi.fn();
    nextPid = 4100;
    delete process.env['TELEGRAM_BOT_TOKEN'];
    delete process.env['BOT_TOKEN_TEST'];
  });

  afterEach(() => {
    vi.useRealTimers();
    vi.restoreAllMocks();
  });

  it('names the shared repositories read-only in a new thread without widening its write roots', async () => {
    const root = mkdtempSync(join(tmpdir(), 'coevo-'));
    const workspace = join(root, 'PARA', 'aki-space');
    mkdirSync(join(root, 'ssot'));
    mkdirSync(join(root, 'Bigbird', 'PULSE'), { recursive: true });
    mkdirSync(join(root, 'Bigbird', 'Oikos', 'tenants', 'xb'), { recursive: true });
    mkdirSync(workspace, { recursive: true });
    const first = createMockProcess();
    const second = createMockProcess();
    mockSpawn.mockReturnValueOnce(first).mockReturnValueOnce(second);
    const runtime = createCodexConversationRuntime({ codexBin: 'codex-test', stateManager: createStateManager() });
    await runtime.spawn({ tenantId: 'owner:aki', runtime: 'codex', workspace, sessionId: 'shared-repos' });
    const firstStdin: Buffer[] = [];
    first.stdin.on('data', (chunk: Buffer) => firstStdin.push(chunk));
    await runtime.send('owner:aki', { type: 'human', content: 'read the code' });
    emitJsonl(first, [{ type: 'thread.started', thread_id: 'thread-shared' }, { type: 'turn.completed', usage: {} }]);
    first.emit('exit', 0, null);
    await tick();

    const prompt = Buffer.concat(firstStdin).toString();
    expect(prompt).toContain(`- 共享仓库（只读）：${root}/、${join(root, 'Bigbird', 'PULSE')}/、${join(root, 'Bigbird', 'Oikos')}/（`);
    expect(prompt).toContain(`- 规则正本：${join(root, 'ssot')}/`);
    expect(prompt).toContain('- 共享仓库内不得读取（本会话工作区与授权路径不在此限）：凭据与令牌文件（');
    expect(prompt).toContain(`${join(root, 'Bigbird', 'Oikos', 'tenants', 'xb')}/`);
    expect(prompt.endsWith('\n\nread the code')).toBe(true);
    const args = mockSpawn.mock.calls[0][1] as string[];
    expect(args.filter((arg, index) => args[index - 1] === '--add-dir')).toEqual(resolveCodexAdditionalWritableRoots());

    const secondStdin: Buffer[] = [];
    second.stdin.on('data', (chunk: Buffer) => secondStdin.push(chunk));
    await runtime.send('owner:aki', { type: 'human', content: 'again' });
    second.emit('exit', 0, null);
    await tick();
    expect(Buffer.concat(secondStdin).toString()).toBe('again');
    rmSync(root, { recursive: true, force: true });
  });

  it('saves thread id, emits assistant/result, and writes prompt to stdin', async () => {
    const proc = createMockProcess();
    mockSpawn.mockReturnValue(proc);
    const stateManager = createStateManager();
    const runtime = createCodexConversationRuntime({ codexBin: 'codex-test', stateManager });
    const events: Array<[string, unknown]> = [];
    runtime.on('turn-accepted', (tenantId) => events.push(['turn-accepted', tenantId]));
    runtime.on('response', (_tenantId, msg) => events.push(['response', msg]));
    runtime.on('error', (_tenantId, err) => events.push(['error', err.message]));

    await runtime.spawn({
      tenantId: 'owner:aki',
      runtime: 'codex',
      workspace: '/workspace',
      sessionId: 'logical-session',
    });
    const stdinChunks: Buffer[] = [];
    proc.stdin.on('data', (chunk: Buffer) => stdinChunks.push(chunk));

    await runtime.send('owner:aki', { type: 'human', content: 'hello codex' });
    emitJsonl(proc, [
      { type: 'thread.started', thread_id: 'thread-123' },
      { type: 'turn.started' },
      { type: 'item.completed', item: { type: 'agent_message', text: 'CODEX_OK' } },
      { type: 'turn.completed', usage: { input_tokens: 1, output_tokens: 2 } },
    ]);
    proc.emit('exit', 0, null);
    await tick();

    expect(Buffer.concat(stdinChunks).toString()).toBe('hello codex');
    const args = mockSpawn.mock.calls[0][1] as string[];
    for (const root of resolveCodexAdditionalWritableRoots()) expect(args).toEqual(expect.arrayContaining(['--add-dir', root]));
    expect(runtime.get('owner:aki')?.codexThreadId).toBe('thread-123');
    expect(stateManager.updates[0]).toMatchObject({ codexThreadId: 'thread-123', runtime: 'codex' });
    expect(stateManager.flushed).toBe(1);
    expect(events).toEqual([
      ['turn-accepted', 'owner:aki'],
      ['response', expect.objectContaining({ type: 'assistant', content: 'CODEX_OK' })],
      ['response', expect.objectContaining({ type: 'result', runtime: 'codex' })],
    ]);
    expect(runtime.get('owner:aki')?.state).toBe('idle');
  });

  it('propagates a fresh native Codex thread id into assistant and result metadata', async () => {
    const proc = createMockProcess(); mockSpawn.mockReturnValue(proc);
    const runtime = createCodexConversationRuntime({ codexBin: 'codex-test' });
    const responses: StreamJsonMessage[] = [];
    runtime.on('response', (_tenantId, message) => responses.push(message));
    await runtime.spawn({tenantId:'owner:aki',runtime:'codex',workspace:'/workspace',sessionId:'logical-session'});
    await runtime.send('owner:aki',{type:'human',content:'fresh thread'});
    emitJsonl(proc,[
      {type:'thread.started',thread_id:'native-codex-thread'},
      {type:'item.completed',item:{id:'assistant-1',type:'agent_message',text:'done'}},
      {type:'turn.completed',usage:{input_tokens:7,output_tokens:3}},
    ]);
    proc.emit('exit',0,null); await tick();
    expect(responses).toContainEqual(expect.objectContaining({type:'assistant',session_id:'native-codex-thread'}));
    expect(responses).toContainEqual(expect.objectContaining({type:'result',session_id:'native-codex-thread'}));
  });

  it('labels the result with the model passed to codex exec', async () => {
    const proc = createMockProcess(); mockSpawn.mockReturnValue(proc);
    const runtime = createCodexConversationRuntime({ codexBin: 'codex-test' });
    const responses: StreamJsonMessage[] = [];
    runtime.on('response', (_tenantId, message) => responses.push(message));
    runtime.on('error', () => undefined);
    await runtime.spawn({tenantId:'owner:aki',runtime:'codex',workspace:'/workspace',sessionId:'logical-session'});
    await runtime.send('owner:aki',{type:'human',content:'which model'});
    emitJsonl(proc,[{type:'turn.completed',usage:{input_tokens:7,output_tokens:3}}]);
    proc.emit('exit',2,null); await tick();
    const args = mockSpawn.mock.calls[0][1] as string[];
    const launched = args[args.indexOf('-m') + 1];
    expect(launched).toBeTruthy();
    expect(responses).toContainEqual(expect.objectContaining({type:'result',requested_model:launched}));
  });

  it('publishes settled usage before a non-zero Codex exit', async () => {
    const proc = createMockProcess(); mockSpawn.mockReturnValue(proc);
    const runtime = createCodexConversationRuntime({ codexBin: 'codex-test' });
    const responses: StreamJsonMessage[] = []; const errors: Error[] = [];
    runtime.on('response', (_tenantId, message) => responses.push(message));
    runtime.on('error', (_tenantId, error) => errors.push(error));
    await runtime.spawn({tenantId:'owner:aki',runtime:'codex',workspace:'/workspace',sessionId:'logical-session'});
    await runtime.send('owner:aki',{type:'human',content:'settle then fail'});
    emitJsonl(proc,[{type:'turn.completed',usage:{input_tokens:7,output_tokens:3}}]);
    proc.emit('exit',2,null); await tick();
    expect(responses).toContainEqual(expect.objectContaining({type:'result',is_error:true,usage:{input_tokens:7,output_tokens:3}}));
    expect(errors).toHaveLength(1);
  });

  it('applies YOLO mode to the next Codex turn for a tenant', async () => {
    const proc = createMockProcess();
    mockSpawn.mockReturnValue(proc);
    const runtime = createCodexConversationRuntime({ codexBin: 'codex-test' });

    await runtime.spawn({
      tenantId: 'owner:aki',
      runtime: 'codex',
      workspace: '/workspace',
      sessionId: 'logical-session',
    });
    runtime.setYoloMode?.('owner:aki', true);
    await runtime.send('owner:aki', { type: 'human', content: 'maintenance' });

    expect(mockSpawn.mock.calls[0][1]).toContain('--dangerously-bypass-approvals-and-sandbox');
    expect(mockSpawn.mock.calls[0][1]).not.toContain('workspace-write');
    proc.emit('exit', 0, null);
    await tick();
  });

  it('uses spawn codexYoloDefault for the turn command', async () => {
    const proc = createMockProcess();
    mockSpawn.mockReturnValue(proc);
    const runtime = createCodexConversationRuntime({ codexBin: 'codex-test' });

    await runtime.spawn({
      tenantId: 'owner:pulse',
      runtime: 'codex',
      workspace: '/workspace',
      sessionId: 'logical-session',
      codexYoloDefault: true,
    });
    await runtime.send('owner:pulse', { type: 'human', content: 'maintenance' });

    expect(mockSpawn.mock.calls[0][1]).toContain('--dangerously-bypass-approvals-and-sandbox');
    expect(mockSpawn.mock.calls[0][1]).not.toContain('workspace-write');
    proc.emit('exit', 0, null);
    await tick();
  });

  it('passes image attachments to codex -i and cleans temporary files after the turn', async () => {
    const proc = createMockProcess();
    mockSpawn.mockReturnValue(proc);
    const runtime = createCodexConversationRuntime({ codexBin: 'codex-test' });
    const tempDir = join(tmpdir(), `relay-codex-image-test-${Date.now()}`);
    mkdirSync(tempDir, { recursive: true });
    const imagePath = join(tempDir, 'photo.jpg');
    writeFileSync(imagePath, 'fake image bytes');

    await runtime.spawn({
      tenantId: 'owner:aki',
      runtime: 'codex',
      workspace: '/workspace',
      sessionId: 'logical-session',
    });
    const stdinChunks: Buffer[] = [];
    proc.stdin.on('data', (chunk: Buffer) => stdinChunks.push(chunk));

    await runtime.send('owner:aki', {
      type: 'human',
      content: 'caption prompt',
      attachments: [{ kind: 'image', path: imagePath, mediaType: 'image/jpeg', cleanup: true, cleanupRoot: tempDir }],
    });
    emitJsonl(proc, [{ type: 'turn.completed', usage: { input_tokens: 1 } }]);
    proc.emit('exit', 0, null);
    await tick();

    const args = mockSpawn.mock.calls[0][1] as string[];
    expect(args).toEqual(expect.arrayContaining(['-i', imagePath, '--', '-']));
    expect(Buffer.concat(stdinChunks).toString()).toBe('caption prompt');
    expect(existsSync(imagePath)).toBe(false);
    expect(existsSync(tempDir)).toBe(false);
    rmSync(tempDir, { recursive: true, force: true });
  });

  it('uses resume command when process already has a thread id', async () => {
    const proc = createMockProcess();
    mockSpawn.mockReturnValue(proc);
    const runtime = createCodexConversationRuntime({ codexBin: 'codex-test' });
    await runtime.spawn({
      tenantId: 'owner:aki',
      runtime: 'codex',
      workspace: '/workspace',
      sessionId: 'logical-session',
      codexThreadId: 'thread-existing',
    });

    await runtime.send('owner:aki', { type: 'human', content: 'second turn' });

    const args = mockSpawn.mock.calls[0][1] as string[];
    expect(args).toEqual(expect.arrayContaining(['exec', 'resume', 'thread-existing']));
  });

  it('emits error for codex error JSONL and does not emit result', async () => {
    const proc = createMockProcess();
    mockSpawn.mockReturnValue(proc);
    const runtime = createCodexConversationRuntime({ codexBin: 'codex-test' });
    const errors: string[] = [];
    const sessionEnds: string[] = [];
    const responses: StreamJsonMessage[] = [];
    runtime.on('error', (_tenantId, error) => errors.push(error.message));
    runtime.on('session-end', (_tenantId, reason) => sessionEnds.push(reason.type));
    runtime.on('response', (_tenantId, msg) => responses.push(msg));

    await runtime.spawn({ tenantId: 'owner:aki', runtime: 'codex', workspace: '/workspace', sessionId: 's' });
    await runtime.send('owner:aki', { type: 'human', content: 'fail' });
    emitJsonl(proc, [{ type: 'turn.failed', error: { message: 'bad turn' } }]);
    proc.emit('exit', 0, null);
    await tick();

    expect(errors).toEqual(['bad turn']);
    expect(sessionEnds).toEqual(['crash']);
    expect(responses).toEqual([]);
  });

  it('aborts the active turn with SIGINT without crashing or discarding the Codex thread', async () => {
    const proc = createMockProcess();
    mockSpawn.mockReturnValue(proc);
    const runtime = createCodexConversationRuntime({ codexBin: 'codex-test' });
    const errors: string[] = [];
    const sessionEnds: string[] = [];
    const responses: StreamJsonMessage[] = [];
    runtime.on('error', (_tenantId, error) => errors.push(error.message));
    runtime.on('session-end', (_tenantId, reason) => sessionEnds.push(reason.type));
    runtime.on('response', (_tenantId, msg) => responses.push(msg));

    await runtime.spawn({
      tenantId: 'owner:aki',
      runtime: 'codex',
      workspace: '/workspace',
      sessionId: 'logical-session',
      codexThreadId: 'thread-existing',
    });
    await runtime.send('owner:aki', { type: 'human', content: 'long running task' });

    await runtime.abortTurn?.('owner:aki');
    await tick();

    expect(proc.kill).toHaveBeenCalledWith('SIGINT');
    expect(errors).toEqual([]);
    expect(sessionEnds).toEqual([]);
    expect(responses).toEqual([
      expect.objectContaining({
        type: 'result',
        subtype: 'interrupted',
        runtime: 'codex',
        interrupted: true,
      }),
    ]);
    expect(runtime.get('owner:aki')).toMatchObject({
      state: 'idle',
      codexThreadId: 'thread-existing',
    });
  });

  it('keeps exitCode 0 stderr rollout warning as success and uses output-last-message fallback', async () => {
    const proc = createMockProcess();
    mockSpawn.mockReturnValue(proc);
    const runtime = createCodexConversationRuntime({ codexBin: 'codex-test' });
    const errors: string[] = [];
    const responses: StreamJsonMessage[] = [];
    runtime.on('error', (_tenantId, error) => errors.push(error.message));
    runtime.on('response', (_tenantId, msg) => responses.push(msg));

    await runtime.spawn({ tenantId: 'owner:aki', runtime: 'codex', workspace: '/workspace', sessionId: 's' });
    await runtime.send('owner:aki', { type: 'human', content: 'fallback' });
    const args = mockSpawn.mock.calls[0][1] as string[];
    const outputPath = args[args.indexOf('--output-last-message') + 1];
    mkdirSync(join(outputPath, '..'), { recursive: true });
    writeFileSync(outputPath, 'FALLBACK_OK\n');
    proc.stderr.write('failed to record rollout items: thread not found\n');
    emitJsonl(proc, [{ type: 'turn.completed', usage: { output_tokens: 1 } }]);
    proc.emit('exit', 0, null);
    await tick();

    expect(errors).toEqual([]);
    expect(responses).toEqual([
      expect.objectContaining({ type: 'assistant', content: 'FALLBACK_OK' }),
      expect.objectContaining({ type: 'result', runtime: 'codex' }),
    ]);
  });

  it('serializes queued turns while one codex child is in-flight', async () => {
    const first = createMockProcess();
    const second = createMockProcess();
    mockSpawn.mockReturnValueOnce(first).mockReturnValueOnce(second);
    const runtime = createCodexConversationRuntime({ codexBin: 'codex-test' });

    await runtime.spawn({ tenantId: 'owner:aki', runtime: 'codex', workspace: '/workspace', sessionId: 's' });
    await runtime.send('owner:aki', { type: 'human', content: 'one' });
    await runtime.send('owner:aki', { type: 'human', content: 'two' });

    expect(mockSpawn).toHaveBeenCalledTimes(1);
    emitJsonl(first, [{ type: 'thread.started', thread_id: 'thread-1' }, { type: 'turn.completed' }]);
    first.emit('exit', 0, null);
    await tick();

    expect(mockSpawn).toHaveBeenCalledTimes(2);
    const secondArgs = mockSpawn.mock.calls[1][1] as string[];
    expect(secondArgs).toEqual(expect.arrayContaining(['resume', 'thread-1']));
    expect(runtime.get('owner:aki')?.state).toBe('busy');
  });

  it('cleans in-flight and queued image attachments when killed', async () => {
    const proc = createMockProcess({ exitOnKill: false });
    mockSpawn.mockReturnValue(proc);
    const runtime = createCodexConversationRuntime({ codexBin: 'codex-test' });
    const currentDir = join(tmpdir(), `relay-codex-current-${Date.now()}`);
    const queuedDir = join(tmpdir(), `relay-codex-queued-${Date.now()}`);
    mkdirSync(currentDir, { recursive: true });
    mkdirSync(queuedDir, { recursive: true });
    const currentImage = join(currentDir, 'current.jpg');
    const queuedImage = join(queuedDir, 'queued.jpg');
    writeFileSync(currentImage, 'current image');
    writeFileSync(queuedImage, 'queued image');

    await runtime.spawn({ tenantId: 'owner:aki', runtime: 'codex', workspace: '/workspace', sessionId: 's' });
    await runtime.send('owner:aki', {
      type: 'human',
      content: 'current',
      attachments: [{ kind: 'image', path: currentImage, cleanup: true, cleanupRoot: currentDir }],
    });
    await runtime.send('owner:aki', {
      type: 'human',
      content: 'queued',
      attachments: [{ kind: 'image', path: queuedImage, cleanup: true, cleanupRoot: queuedDir }],
    });

    await runtime.kill('owner:aki');

    expect(existsSync(currentDir)).toBe(false);
    expect(existsSync(queuedDir)).toBe(false);
    expect(proc.kill).toHaveBeenCalledWith('SIGTERM');
  });

  it('persists the in-flight prompt when Relay shuts down during a Codex turn', async () => {
    const proc = createMockProcess({ exitOnKill: false });
    mockSpawn.mockReturnValue(proc);
    const stateManager = createStateManager();
    const runtime = createCodexConversationRuntime({ codexBin: 'codex-test', stateManager });
    const sessionEnds: string[] = [];
    runtime.on('session-end', (_tenantId, reason) => sessionEnds.push(reason.type));

    await runtime.spawn({
      tenantId: 'owner:pulse',
      runtime: 'codex',
      workspace: '/workspace',
      sessionId: 'logical-session',
      codexThreadId: 'thread-pulse',
    });
    await runtime.send('owner:pulse', { type: 'human', content: 'resume this after restart' });

    expect(runtime.emitShutdown()).toEqual(new Set(['owner:pulse']));
    expect(stateManager.updates.at(-1)).toMatchObject({
      runtime: 'codex',
      codexThreadId: 'thread-pulse',
      interruptedUserMessage: 'resume this after restart',
      status: 'active',
    });
    expect(stateManager.flushed).toBe(1);
    expect(sessionEnds).toEqual(['relay-shutdown']);
  });

  it('records turns queued behind the busy Codex turn at shutdown and replays them in order', async () => {
    const proc = createMockProcess({ exitOnKill: false });
    mockSpawn.mockReturnValue(proc);
    const dir = mkdtempSync(join(tmpdir(), 'relay-codex-shutdown-queue-'));
    try {
      const state = createRealStateManager(join(dir, 'state.json'));
      state.updateSession('owner:pulse', { poolKey: 'owner:pulse', agentId: 'pulse', sessionId: 's', runtime: 'codex',
        turnCount: 0, maxTurns: 100, lastActivity: 1, status: 'active' } as never);
      const runtime = createCodexConversationRuntime({ codexBin: 'codex-test', stateManager: state });
      await runtime.spawn({ tenantId: 'owner:pulse', runtime: 'codex', workspace: '/workspace', sessionId: 's', codexThreadId: 'thread-pulse' });
      await runtime.send('owner:pulse', { type: 'human', content: 'in flight' });
      await runtime.send('owner:pulse', { type: 'human', content: 'queued one' });
      await runtime.send('owner:pulse', { type: 'human', content: 'queued two' });

      runtime.emitShutdown();
      // The interrupted turn keeps its own mechanism; the queued ones are recorded whole.
      const session = createRealStateManager(join(dir, 'state.json'));
      session.load();
      const saved = session.getSession('owner:pulse');
      expect(saved?.wasBusyAtShutdown).toBe(true);
      expect(saved?.interruptedUserMessage).toBe('in flight');
      expect(saved?.pendingMaintenanceTurns?.map(turn => turn.message.content)).toEqual(['queued one', 'queued two']);
      // Nothing queued is started by the stopping runtime.
      emitJsonl(proc, [{ type: 'thread.started', thread_id: 'thread-pulse' }, { type: 'turn.completed' }]);
      proc.emit('exit', 0, null);
      await tick();
      expect(mockSpawn).toHaveBeenCalledTimes(1);

      const replayed: unknown[] = [];
      expect(await replayPendingTurns(session, 'owner:pulse', async message => { replayed.push(message.content); })).toBe(2);
      expect(replayed).toEqual(['queued one', 'queued two']);
    } finally {
      rmSync(dir, { recursive: true, force: true });
    }
  });

  it('sends SIGTERM on timeout and SIGKILL after grace period', async () => {
    vi.useFakeTimers();
    const proc = createMockProcess({ exitOnKill: false });
    mockSpawn.mockReturnValue(proc);
    const runtime = createCodexConversationRuntime({ codexBin: 'codex-test', timeoutMs: 100, killGraceMs: 50 });

    await runtime.spawn({ tenantId: 'owner:aki', runtime: 'codex', workspace: '/workspace', sessionId: 's' });
    await runtime.send('owner:aki', { type: 'human', content: 'slow' });

    await vi.advanceTimersByTimeAsync(100);
    expect(proc.kill).toHaveBeenCalledWith('SIGTERM');
    await vi.advanceTimersByTimeAsync(50);
    expect(proc.kill).toHaveBeenCalledWith('SIGKILL');
  });

  it('refreshes idle timeout on assistant activity and only kills after later inactivity', async () => {
    vi.useFakeTimers();
    const proc = createMockProcess({ exitOnKill: false });
    mockSpawn.mockReturnValue(proc);
    const runtime = createCodexConversationRuntime({ codexBin: 'codex-test', timeoutMs: 100, killGraceMs: 50 });
    const responses: StreamJsonMessage[] = [];
    runtime.on('response', (_tenantId, msg) => responses.push(msg));

    await runtime.spawn({ tenantId: 'owner:aki', runtime: 'codex', workspace: '/workspace', sessionId: 's' });
    await runtime.send('owner:aki', { type: 'human', content: 'slow but active' });

    await vi.advanceTimersByTimeAsync(90);
    emitJsonl(proc, [{ type: 'item.completed', item: { type: 'agent_message', text: 'still working' } }]);
    await vi.advanceTimersByTimeAsync(0);

    await vi.advanceTimersByTimeAsync(99);
    expect(proc.kill).not.toHaveBeenCalled();
    expect(responses).toEqual([]); // Unclassified candidate is retained until the next native boundary.

    await vi.advanceTimersByTimeAsync(1);
    expect(proc.kill).toHaveBeenCalledWith('SIGTERM');
    await vi.advanceTimersByTimeAsync(50);
    expect(proc.kill).toHaveBeenCalledWith('SIGKILL');
  });

  it('rejects an anthropic-protocol ccModel without spawning and does not get stuck busy', async () => {
    const modelCatalog = createCatalogStub({ 'deepseek/deepseek-v4-flash-0731': { provider: 'openrouter', protocol: 'anthropic' } });
    const runtime = createCodexConversationRuntime({ codexBin: 'codex-test', modelCatalog });
    const errors: string[] = [];
    const sessionEnds: string[] = [];
    const responses: StreamJsonMessage[] = [];
    runtime.on('error', (_tenantId, error) => errors.push(error.message));
    runtime.on('session-end', (_tenantId, reason) => sessionEnds.push(reason.type));
    runtime.on('response', (_tenantId, msg) => responses.push(msg));

    await runtime.spawn({
      tenantId: 'owner:aki',
      runtime: 'codex',
      workspace: '/workspace',
      sessionId: 's',
      ccModel: 'deepseek/deepseek-v4-flash-0731',
    });
    await runtime.send('owner:aki', { type: 'human', content: 'hi openrouter on codex' });
    await tick();

    expect(mockSpawn).not.toHaveBeenCalled();
    expect(errors).toHaveLength(1);
    expect(errors[0]).toContain('deepseek/deepseek-v4-flash-0731');
    expect(errors[0]).toContain('CC 运行时');
    expect(sessionEnds).toEqual(['crash']);
    expect(responses).toEqual([]);
    // State must be reset to idle so the queue is not stuck busy.
    expect(runtime.get('owner:aki')?.state).toBe('idle');
  });

  it('spawns normally for an openai-protocol ccModel on Codex runtime', async () => {
    const proc = createMockProcess();
    mockSpawn.mockReturnValue(proc);
    const modelCatalog = createCatalogStub({ 'example-openai-model': { provider: 'example-openai', protocol: 'openai' } });
    const runtime = createCodexConversationRuntime({ codexBin: 'codex-test', modelCatalog });
    const errors: string[] = [];
    runtime.on('error', (_tenantId, error) => errors.push(error.message));

    await runtime.spawn({
      tenantId: 'owner:aki',
      runtime: 'codex',
      workspace: '/workspace',
      sessionId: 's',
      ccModel: 'example-openai-model',
    });
    await runtime.send('owner:aki', { type: 'human', content: 'hi' });
    emitJsonl(proc, [{ type: 'turn.completed' }]);
    proc.emit('exit', 0, null);
    await tick();

    expect(mockSpawn).toHaveBeenCalledTimes(1);
    expect(errors).toEqual([]);
  });

  it('passes through an unknown ccModel (CC alias like opus) without rejecting', async () => {
    const proc = createMockProcess();
    mockSpawn.mockReturnValue(proc);
    const modelCatalog = createCatalogStub({ 'deepseek/deepseek-v4-flash-0731': { provider: 'openrouter', protocol: 'anthropic' } });
    const runtime = createCodexConversationRuntime({ codexBin: 'codex-test', modelCatalog });
    const errors: string[] = [];
    runtime.on('error', (_tenantId, error) => errors.push(error.message));

    await runtime.spawn({
      tenantId: 'owner:aki',
      runtime: 'codex',
      workspace: '/workspace',
      sessionId: 's',
      ccModel: 'opus',
    });
    await runtime.send('owner:aki', { type: 'human', content: 'hi opus' });
    emitJsonl(proc, [{ type: 'turn.completed' }]);
    proc.emit('exit', 0, null);
    await tick();

    expect(mockSpawn).toHaveBeenCalledTimes(1);
    expect(errors).toEqual([]);
  });

  it('supports a mocked Telegram text flow through the runtime manager', async () => {
    const proc = createMockProcess();
    mockSpawn.mockReturnValue(proc);
    const manager = createConversationRuntimeManager();
    manager.register(createCodexConversationRuntime({ codexBin: 'codex-test' }));
    const responses: StreamJsonMessage[] = [];
    manager.on('response', (_tenantId, msg) => responses.push(msg));

    await manager.spawn({
      tenantId: 'owner:aki',
      runtime: 'codex',
      workspace: '/workspace',
      sessionId: 's',
    });
    await manager.send('owner:aki', { type: 'human', content: 'TG hello' });
    emitJsonl(proc, [
      { type: 'thread.started', thread_id: 'thread-tg' },
      { type: 'turn.started' },
      { type: 'item.completed', item: { type: 'agent_message', text: 'TG_CODEX_OK' } },
      { type: 'turn.completed' },
    ]);
    proc.emit('exit', 0, null);
    await tick();

    expect(responses).toEqual([
      expect.objectContaining({ type: 'assistant', content: 'TG_CODEX_OK' }),
      expect.objectContaining({ type: 'result', runtime: 'codex' }),
    ]);
  });
});


describe('WI568 Codex native model evidence through exec translation', () => {
  it('joins the actual returned thread and native task instead of the configured model', async () => {
    const root=mkdtempSync(join(tmpdir(),'codex-model-wire-'));
    const proc=createMockProcess(); mockSpawn=vi.fn(()=>proc);
    const runtime=createCodexConversationRuntime({nativeSessionsDirectory:root});
    try {
      const messages:StreamJsonMessage[]=[];
      runtime.on('response',(_id,message)=>messages.push(message));
      await runtime.spawn({tenantId:'fixture',workspace:root,sessionId:'logical',ccModel:'configured-other'} as never);
      await runtime.send('fixture',{type:'human',content:'synthetic'});
      const usage={input_tokens:100,cached_input_tokens:20,output_tokens:10};
      const rows=[{type:'session_meta',payload:{id:'native-thread'}},
        {type:'event_msg',payload:{type:'task_started',turn_id:'t'}},
        {type:'turn_context',payload:{turn_id:'t',model:'actual-native',model_provider:'openai'}},
        {type:'event_msg',payload:{type:'token_count',info:{total_token_usage:usage}}},
        {type:'event_msg',payload:{type:'task_complete',turn_id:'t'}}];
      writeFileSync(join(root,'native-thread.jsonl'),rows.map(r=>JSON.stringify(r)+'\n').join(''));
      emitJsonl(proc,[{type:'thread.started',thread_id:'native-thread'},{type:'turn.started'},
        {type:'turn.completed',usage}]);
      proc.emit('exit',0); await tick();
      expect(messages.at(-1)?.native_model_usage).toMatchObject({runtime:'codex',models:[{model:'actual-native'}]});
    } finally { runtime.dispose(); rmSync(root,{recursive:true,force:true}); }
  });
});

it('separates Codex public commentary from the one terminal answer by native item boundaries', async () => {
  const proc = createMockProcess(); mockSpawn.mockReturnValue(proc);
  const runtime = createCodexConversationRuntime({ codexBin: 'codex-test' });
  const responses: StreamJsonMessage[] = [];
  runtime.on('response', (_id, event) => responses.push(event));
  await runtime.spawn({ tenantId: 'owner:codex', runtime: 'codex', workspace: '/workspace', sessionId: 's' });
  await runtime.send('owner:codex', { type: 'human', content: 'check' }, {
    correlationId: 'sequence', hostAttemptRef: 'attempt', idempotencyKey: 'sequence', effect: 'no-effect' });
  emitJsonl(proc, [
    { type: 'item.completed', item: { id: 'summary', type: 'reasoning', text: '公开的思考摘要。' } },
    { type: 'item.completed', item: { id: 'a', type: 'agent_message', text: '先检查。' } },
    { type: 'item.started', item: { id: 't', type: 'command_execution', command: 'echo fixture' } },
    { type: 'item.completed', item: { id: 't', type: 'command_execution', aggregated_output: 'OK', exit_code: 0 } },
    { type: 'item.completed', item: { id: 'b', type: 'agent_message', phase: 'commentary', text: '检查完成，整理结果。' } },
    { type: 'item.completed', item: { id: 'final', type: 'agent_message', text: '最终答复。' } },
    { type: 'item.completed', item: { id: 'private', type: 'raw_reasoning', text: 'NEVER_PUBLIC' } },
    { type: 'turn.completed', usage: { input_tokens: 1, output_tokens: 1 } },
  ]);
  await tick();
  expect(responses.filter(event => event.type === 'assistant')).toHaveLength(0);
  proc.emit('exit', 0); await tick();
  const work = responses.filter(event => event.type === 'work_process');
  expect(work.map(event => (event.process_event as any).kind)).toEqual(['commentary', 'commentary', 'operation', 'operation', 'commentary']);
  expect(work.filter(event => (event.process_event as any).kind === 'commentary').map(event => (event.process_event as any).output))
    .toEqual(['公开的思考摘要。', '先检查。', '检查完成，整理结果。']);
  expect(work.every(event => event.correlation_id === 'sequence' && event.host_attempt_ref === 'attempt')).toBe(true);
  expect(responses.filter(event => event.type === 'assistant')).toEqual([
    expect.objectContaining({ content: '最终答复。', delivery_phase: 'final', event_id: 'codex:final' })]);
  expect(JSON.stringify(responses)).not.toContain('NEVER_PUBLIC');
});

describe('Codex exec thread-cumulative usage becomes per-turn receipts', () => {
  // Real TCloud thread 2026-10-18: exec `turn.completed` totals and each turn's
  // own usage (sum of native last_token_usage within the turn).
  const totals = [[16114,12160,13],[117302,106496,475],[367377,345984,2334],[1333848,1230336,7572],
    [2722328,2561920,12405],[44620622,44050432,131648],[47225165,46466176,153942],[47969596,47198848,160197],
    [49261168,48285952,177255],[49795464,48698752,179029],[50204935,49106560,180259]];
  const turns = [[16114,12160,13],[101188,94336,462],[250075,239488,1859],[966471,884352,5238],
    [1388480,1331584,4833],[41898294,41488512,119243],[2604543,2415744,22294],[744431,732672,6255],
    [1291572,1087104,17058],[534296,412800,1774],[409471,407808,1230]];
  const counts = ([input_tokens, cached_input_tokens, output_tokens]: number[]) => ({input_tokens, cached_input_tokens, output_tokens});
  const roots: string[] = [];
  beforeEach(() => { mockSpawn = vi.fn(); });
  afterEach(() => { for (const root of roots.splice(0)) rmSync(root, {recursive: true, force: true}); });

  function host(native = false) {
    const root = mkdtempSync(join(tmpdir(), 'codex-thread-usage-')); roots.push(root);
    const sessions = join(root, 'sessions'); mkdirSync(sessions);
    const rollout = join(sessions, 'rollout-thread-1.jsonl');
    let nativeTurn = 0, nativeTotal = [0, 0, 0];
    /** Native rollout rows for one turn: model context plus its own token counts. */
    function appendNative(total: number[]) {
      const turn = `turn-${++nativeTurn}`, last = total.map((n, i) => n - nativeTotal[i]!); nativeTotal = total;
      const rows = [...(nativeTurn === 1 ? [{type:'session_meta',payload:{id:'thread-1'}}] : []),
        {type:'event_msg',payload:{type:'task_started',turn_id:turn}},
        {type:'turn_context',payload:{turn_id:turn,model:'gpt-native',model_provider:'openai'}},
        {type:'event_msg',payload:{type:'token_count',info:{total_token_usage:counts(total),last_token_usage:counts(last)}}},
        {type:'event_msg',payload:{type:'task_complete',turn_id:turn}}];
      writeFileSync(rollout, rows.map(r => JSON.stringify(r) + '\n').join(''), {flag: 'a'});
    }
    const statePath = join(root, 'state.json');
    const capture = createCollaborationCapture(join(root, 'receipts'))!;
    const receipts: Record<string, unknown>[] = [];
    function start(codexThreadId?: string) {
      const stateManager = createRealStateManager(statePath); stateManager.load();
      if (!stateManager.getSession('t')) stateManager.updateSession('t', {poolKey:'t',agentId:'aki',runtime:'codex',sessionId:'s',
        turnCount:0,maxTurns:100,lastActivity:1,status:'active'});
      const runtime = createCodexConversationRuntime({codexBin:'codex-test', stateManager,
        ...(native ? {nativeSessionsDirectory: sessions} : {})});
      let attempt: ReturnType<typeof capture.begin> | undefined;
      runtime.on('response', (_id, message) => { if (message.type === 'result' && attempt) capture.response(attempt, message); });
      runtime.on('error', () => undefined);
      return {stateManager, runtime, async spawn() {
        await runtime.spawn({tenantId:'t',runtime:'codex',workspace:'/workspace',sessionId:'s',codexThreadId});
      }, async turn(total: number[] | undefined, thread = 'thread-1') {
        // pid 0: no native-session proof side channel; this test is about usage only.
        const proc = createMockProcess(); proc.pid = 0; mockSpawn.mockReturnValueOnce(proc);
        attempt = capture.begin('codex', undefined, `turn-${receipts.length}-${Math.random()}`);
        await runtime.send('t', {type:'human', content:'go'});
        if (native && total) appendNative(total);
        emitJsonl(proc, [{type:'thread.started', thread_id:thread},
          ...(total ? [{type:'turn.completed', usage:counts(total)}] : [])]);
        proc.emit('exit', 0, null); await tick();
      }};
    }
    const rows = () => readdirSync(join(root, 'receipts')).flatMap(f => readFileSync(join(root, 'receipts', f), 'utf8')
      .trim().split('\n').map(line => JSON.parse(line))).filter(r => r.kind === 'usage');
    return {start, usage: () => rows().map(r => r.usage), rows, statePath};
  }

  it('replays the real thread: first turn, increasing turns and a Relay restart all record each turn only', async () => {
    const h = host();
    let relay = h.start(); await relay.spawn();
    for (const total of totals.slice(0, 6)) await relay.turn(total);
    relay.stateManager.dispose();
    // A new Relay process resumes the persisted thread from state.json.
    relay = h.start('thread-1'); await relay.spawn();
    for (const total of totals.slice(6)) await relay.turn(total);
    relay.stateManager.dispose();
    expect(h.usage()).toEqual(turns.map(t => ({...counts(t), cache_write_input_tokens: 0})));
  });

  it('accepts the native per-turn partition for resumed turns, across a Relay restart', async () => {
    const h = host(true);
    let relay = h.start(); await relay.spawn();
    for (const total of totals.slice(0, 6)) await relay.turn(total);
    relay.stateManager.dispose();
    relay = h.start('thread-1'); await relay.spawn();
    for (const total of totals.slice(6)) await relay.turn(total);
    relay.stateManager.dispose();
    expect(h.rows().map(r => [r.model, r.provider, r.usage])).toEqual(turns.map(t =>
      ['gpt-native', 'openai', {...counts(t), cache_write_input_tokens: 0}]));
  });

  it('records unknown usage, never the cumulative total, when the previous turn total is missing', async () => {
    const h = host();
    const relay = h.start('thread-1'); await relay.spawn();
    await relay.turn(totals[1]);
    // A resumed turn that ended without a readable total also loses the next baseline.
    await relay.turn(undefined);
    await relay.turn(totals[3]);
    // A total below the baseline (reset or another writer) is unknown too.
    await relay.turn(totals[2]);
    relay.stateManager.dispose();
    const unknown = {input_tokens:null, cached_input_tokens:null, output_tokens:null};
    // The turn without a total keeps the existing "missing usage" receipt.
    expect(h.usage()).toEqual([unknown, {}, unknown, unknown]);
  });
});
