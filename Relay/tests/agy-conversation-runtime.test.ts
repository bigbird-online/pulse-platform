// Stream and log shapes are trimmed from the agy 1.5.13 fixtures recorded for
// wi_20261120_666 N01 (a02 new, a04 resume, b01 tool, e01/e03/e04 egress, g04 denied).
import { EventEmitter } from 'node:events';
import { existsSync, mkdirSync, mkdtempSync, readdirSync, readFileSync, realpathSync, rmSync, statSync, writeFileSync } from 'node:fs';
import { tmpdir, userInfo } from 'node:os';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { PassThrough } from 'node:stream';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import {
  AGY_SANDBOX_ENABLED,
  AGY_SANDBOX_REFUSAL,
  agyEgressFailure,
  agyTurnUsageFromTotals,
  agyUsageCounts,
  agyAccess,
  buildAgyCommand,
  buildAgyEnv,
  agyModelForLevel,
  agyModelLevels,
  agyModelOptions,
  createAgyConversationRuntime,
  DEFAULT_AGY_MODEL,
} from '../src/agy-conversation-runtime.js';
import { resolveConversationRuntimePreference } from '../src/conversation-runtime.js';
import { resolveCodexAdditionalWritableRoots } from '../src/config-paths.js';
import { ccBashAccess, ccPolicyPaths, ccWriteBoundarySettings, ccWriteRoots } from '../src/cc-write-boundary.js';
import { conversationLaunch } from '../src/conversation-launch.js';
import { captureReportedSessionId } from '../src/reported-session-capture.js';
import { publicRuntimeExceptionNotice } from '../src/public-runtime-failure.js';
import { publicWorkEvents } from '../src/work-process-events.js';
import { validateNativeModelUsage } from '../src/collaboration-capture.js';
import { buildRuntimeUsageBinding } from '../src/runtime-usage-binding.js';
import { INTERNAL_TURN_REFUSED, fencedInternalTurn, watchInternalTurn } from '../src/internal-turn-fence.js';
import type { AgentConfig, PersistedSession, StateManager, StreamJsonMessage } from '../src/types.js';

interface MockProcess extends EventEmitter {
  stdout: PassThrough;
  stderr: PassThrough;
  pid: number;
  killed: boolean;
  kill: ReturnType<typeof vi.fn>;
}

let mockSpawn: ReturnType<typeof vi.fn>;
vi.mock('node:child_process', () => ({ spawn: (...args: unknown[]) => mockSpawn(...args) }));
/** The kernel's per-turn access readback; no core unless a test installs one. */
const kernel = vi.hoisted(() => ({ core: false, reads: [] as unknown[][],
  readback: undefined as undefined | (() => Promise<unknown>) }));
vi.mock('../src/native-work-item-command.js', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../src/native-work-item-command.js')>()),
  nodeWorkItemCore: () => (kernel.core ? { python: '/fixture/python', entrypoint: '/fixture/harness.pyz' } : null),
  readRetainedSessionAccess: (...args: unknown[]) => { kernel.reads.push(args); return kernel.readback!(); },
}));
const proofs = vi.hoisted(() => ({ observed: [] as unknown[][], forgotten: [] as unknown[], retained: [] as unknown[][] }));
vi.mock('../src/native-core-runtime.js', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../src/native-core-runtime.js')>()),
  observeNativeSession: (...args: unknown[]) => { proofs.observed.push(args); },
  forgetNativeProofOnExit: (child: unknown) => { proofs.forgotten.push(child); },
  retainNativeConversation: (...args: unknown[]) => { proofs.retained.push(args); return undefined; },
}));

function createMockProcess(onKill?: (proc: MockProcess, signal?: string) => void): MockProcess {
  const proc = new EventEmitter() as MockProcess;
  proc.stdout = new PassThrough();
  proc.stderr = new PassThrough();
  proc.pid = 7100;
  proc.killed = false;
  proc.kill = vi.fn((signal?: string) => {
    proc.killed = true;
    if (onKill) onKill(proc, signal);
    else proc.emit('exit', 1, signal ?? 'SIGTERM');
    return true;
  });
  return proc;
}

const tick = () => new Promise((resolve) => setImmediate(resolve));
async function settle(): Promise<void> { for (let i = 0; i < 5; i++) await tick(); }

/** agy writes this line at start when the restricted run's terminal sandbox is on. */
function sandboxOn(args: string[]): void {
  if (args.includes('--sandbox')) writeFileSync(args[args.indexOf('--log-file') + 1]!, `${AGY_SANDBOX_ENABLED}\n`);
}

function emitLines(proc: MockProcess, events: unknown[]): void {
  for (const event of events) proc.stdout.write(`${JSON.stringify(event)}\n`);
}
async function finishProcess(proc: MockProcess, code: number): Promise<void> {
  await settle();
  proc.stdout.end();
  await settle();
  proc.emit('exit', code, null);
  await settle();
}

const CONV = '8f0067ec-b62d-4ecf-bd95-04cae57649ac';
const OTHER = '1b2c3d4e-0000-4000-8000-000000000002';
const init = (conversation: string, model: string) => ({ event: 'init', conversation_id: conversation,
  init: { model, cwd: '/ws', tools: ['view_file'], permission_mode: 'request-review' } });
const step = (conversation: string, fields: Record<string, unknown>) => ({ event: 'step_update',
  step_update: { conversation_id: conversation, ...fields } });
const result = (conversation: string, fields: Record<string, unknown>) => ({ event: 'result',
  result: { conversation_id: conversation, status: 'SUCCESS', duration_seconds: 2.7, num_turns: 1, ...fields } });

/** a02: a fresh conversation answering in one step. */
function newConversationStream(conversation = CONV, model = 'gemini-3.8-flash-low'): unknown[] {
  const usage = { input_tokens: 9772, output_tokens: 3, thinking_tokens: 0, cache_read_tokens: 0, total_tokens: 9775 };
  return [
    init(conversation, model),
    step(conversation, { step_index: 0, state: 'DONE', step_type: 'user_input' }),
    step(conversation, { step_index: 1, state: 'ACTIVE', step_type: 'agent_response', text_delta: 'Hello there!' }),
    step(conversation, { step_index: 1, state: 'DONE', step_type: 'agent_response', text_delta: '\n', usage }),
    result(conversation, { response: 'Hello there!\n', usage }),
  ];
}

/** a04: second turn; result.usage is the conversation total (9766 + 10044). */
function resumedStream(withSteps = true): unknown[] {
  const turn = { input_tokens: 10044, output_tokens: 38, thinking_tokens: 36, cache_read_tokens: 0, total_tokens: 10082 };
  return [
    init(CONV, 'gemini-4-neon-high'),
    step(CONV, { step_index: 2, state: 'DONE', step_type: 'user_input' }),
    ...(withSteps ? [
      step(CONV, { step_index: 4, state: 'ACTIVE', step_type: 'agent_response', text_delta: 'pong' }),
      step(CONV, { step_index: 4, state: 'DONE', step_type: 'agent_response', text_delta: '\n', usage: turn }),
    ] : []),
    result(CONV, { response: 'pong\n', num_turns: 2, duration_seconds: 40.1,
      usage: { input_tokens: 19810, output_tokens: 39, thinking_tokens: 36, cache_read_tokens: 0, total_tokens: 19849 } }),
  ];
}

const E03_LOG = [
  'W1120 18:39:02.458134 75 cache.go:157] Cache(peopleInfo): Singleflight refresh failed: Get "https://www.googleapis.com/oauth2/v2/userinfo": proxyconnect tcp: dial tcp 127.0.0.1:9: connect: connection refused',
  'E1120 18:39:02.460259 73 errorreport.go:285] Post "https://daily-cloudcode-pa.googleapis.com/v1internal:loadCodeAssist": proxyconnect tcp: dial tcp 127.0.0.1:9: connect: connection refused',
].join('\n');
const E04_LOG = 'W1120 18:42:09.131754 267 cache.go:157] Cache(loadCodeAssistResponse): Singleflight refresh failed: Post "https://daily-cloudcode-pa.googleapis.com/v1internal:loadCodeAssist": context deadline exceeded';

function createState(initial?: PersistedSession): StateManager & { sessions: Record<string, PersistedSession> } {
  const sessions: Record<string, PersistedSession> = initial ? { 'owner:main': initial } : {};
  return {
    sessions,
    load: vi.fn(),
    getSession: (key: string) => sessions[key],
    updateSession: (key: string, update: Partial<PersistedSession>) => {
      sessions[key] = { ...sessions[key], ...update } as PersistedSession;
    },
    removeSession: vi.fn(),
    getAllSessions: () => sessions,
    flush: vi.fn(),
    flushStrict: vi.fn(),
    dispose: vi.fn(),
  } as unknown as StateManager & { sessions: Record<string, PersistedSession> };
}

const baseSession = (extra: Partial<PersistedSession> = {}): PersistedSession => ({
  poolKey: 'owner:main', agentId: 'main', runtime: 'agy', sessionId: 'relay-session',
  turnCount: 0, maxTurns: 100, lastActivity: 1, status: 'active', ...extra });

let dir: string;
let agyBin: string;
let spawned: Array<{ command: string; args: string[]; options: Record<string, any>; proc: MockProcess }>;

beforeEach(() => {
  kernel.core = false;
  kernel.reads.length = 0;
  kernel.readback = undefined;
  dir = mkdtempSync(join(tmpdir(), 'agy-runtime-'));
  agyBin = join(dir, 'agy');
  writeFileSync(agyBin, '#!/bin/sh\n', { mode: 0o755 });
  spawned = [];
  proofs.observed.length = 0;
  proofs.forgotten.length = 0;
  proofs.retained.length = 0;
  mockSpawn = vi.fn((command: string, args: string[], options: Record<string, any>) => {
    const proc = createMockProcess();
    sandboxOn(args);
    spawned.push({ command, args, options, proc });
    return proc;
  });
});
afterEach(() => { rmSync(dir, { recursive: true, force: true }); });

function runtimeWith(state?: StateManager, extra: Record<string, unknown> = {}) {
  const runtime = createAgyConversationRuntime({ agyBin, stateManager: state, logDirectory: join(dir, 'logs'),
    ...extra });
  const responses: StreamJsonMessage[] = [];
  const errors: Error[] = [];
  const exits: Array<number | null> = [];
  const sessionEnds: unknown[] = [];
  /** Event kinds in emission order, as a channel sees them. */
  const order: string[] = [];
  runtime.on('response', (_tenant, msg) => { responses.push(msg); order.push(`response:${(msg as StreamJsonMessage).type}`); });
  runtime.on('error', (_tenant, error) => { errors.push(error); order.push('error'); });
  runtime.on('exit', (_tenant, code) => { exits.push(code); order.push('exit'); });
  runtime.on('session-end', (_tenant, reason) => { sessionEnds.push(reason); order.push('session-end'); });
  return { runtime, responses, errors, exits, sessionEnds, order };
}

async function start(runtime: ReturnType<typeof createAgyConversationRuntime>, ccModel?: string): Promise<void> {
  // An Agent whose CC sessions run inside the write boundary: agy runs restricted too (agyRestricted).
  await runtime.spawn({ tenantId: 'owner:main', sessionId: 'relay-session', workspace: '/ws', ccModel,
    ccWriteBoundary: true } as never);
}
const argAfter = (args: string[], flag: string) => args[args.indexOf(flag) + 1];
/** The CC write denials of these roots (cc-write-boundary.ts `ccPolicyPaths`), as agy rules. */
const policy = (roots: string[]) => ccPolicyPaths(roots).map((path) => `write_file(${path})`);

describe('agy command', () => {
  it('runs stream-json with the model, a turn log, only the kernel entry unsandboxed and writes scoped to the workspace', () => {
    const spec = buildAgyCommand({ agyBin: '/bin/agy', workspace: '/ws', prompt: 'hi', model: 'gemini-4-neon-high',
      logFile: '/logs/t.log', additionalWritableRoots: ['/coevo'] });
    expect(spec.command).toBe('/bin/agy');
    expect(spec.cwd).toBe('/ws');
    expect(spec.args.slice(0, 2)).toEqual(['-p', 'hi']);
    expect(argAfter(spec.args, '--output-format')).toBe('stream-json');
    expect(argAfter(spec.args, '--model')).toBe('gemini-4-neon-high');
    expect(argAfter(spec.args, '--log-file')).toBe('/logs/t.log');
    expect(spec.args).not.toContain('--conversation');
    expect(spec.args).not.toContain('--dangerously-skip-permissions');
    expect(argAfter(spec.args, '--add-dir')).toBe('/coevo');
    // The restricted mode is pinned, never left to the account settings.json: default mode
    // (no auto review of commands leaving the sandbox), the terminal sandbox on, sandboxed
    // commands without review. No glob in a write rule: agy derives the sandbox from them.
    expect(spec.args).toContain('--sandbox');
    expect(spec.args).not.toContain('--mode');
    expect(JSON.parse(argAfter(spec.args, '--settings'))).toEqual({
      agentMode: '', enableTerminalSandbox: true, toolPermission: 'proceed-in-sandbox',
      permissions: { allow: ['command(harness-native)', 'write_file(/ws)', 'write_file(/coevo)', 'read_file(/)'] } });
    // Nothing but the kernel entry leaves the sandbox: no gh, no git (CC: allowUnsandboxedCommands false).
    expect(argAfter(spec.args, '--settings')).not.toMatch(/command\((gh|git)\)/);
  });

  it('allows no other unsandboxed command and no write rule beyond the deduplicated roots', () => {
    const spec = buildAgyCommand({ agyBin: '/bin/agy', workspace: '/ws/', prompt: 'hi', model: 'm',
      logFile: '/l', additionalWritableRoots: ['/ws/', '', '/coevo/', '/coevo/'] });
    const allow: string[] = JSON.parse(argAfter(spec.args, '--settings')).permissions.allow;
    expect(allow).toEqual(['command(harness-native)', 'write_file(/ws)', 'write_file(/coevo)', 'read_file(/)']);
    expect(allow).not.toContain('command(*)');
    expect(allow.filter((rule) => rule.includes('*'))).toEqual([]);
  });

  it('resumes by conversation id and maps yolo to skip-permissions', () => {
    const spec = buildAgyCommand({ agyBin: '/bin/agy', workspace: '/ws', prompt: 'hi', model: 'm',
      logFile: '/l', conversationId: CONV, yoloMode: true });
    expect(argAfter(spec.args, '--conversation')).toBe(CONV);
    expect(spec.args).toContain('--dangerously-skip-permissions');
    expect(spec.args).not.toContain('--settings');
    expect(spec.args).not.toContain('--sandbox');
  });

  it('runs with the account HOME and no proxy address of its own', () => {
    const before = process.env.HOME;
    process.env.HOME = '/somewhere/agent-home';
    try {
      const env = buildAgyEnv();
      expect(env.HOME).toBe(userInfo().homedir);
    } finally { process.env.HOME = before; }
    const source = readFileSync(join(dirname(fileURLToPath(import.meta.url)), '..', 'src', 'agy-conversation-runtime.ts'), 'utf8');
    expect(source).not.toMatch(/https?:\/\/\d+\.\d+\.\d+\.\d+/);
  });
});

describe('agy session write boundary', () => {
  const settingsOf = (args: string[]) => JSON.parse(argAfter(args, '--settings')).permissions;

  it('never opens the shared Coevolution checkout that Codex adds by default', async () => {
    const shared = mkdtempSync(join(tmpdir(), 'agy-shared-coevolution-'));
    const previous = process.env['PULSE_COEVOLUTION_ROOT'];
    process.env['PULSE_COEVOLUTION_ROOT'] = shared;
    try {
      // The production default for Codex is exactly this shared root (config-paths.ts).
      expect(resolveCodexAdditionalWritableRoots()).toEqual([shared]);
      const { runtime } = runtimeWith(createState(baseSession()));
      await start(runtime);
      await runtime.send('owner:main', { type: 'human', content: 'hi' });
      const args = spawned[0]!.args;
      expect(settingsOf(args)).toEqual({ allow: ['command(harness-native)', 'write_file(/ws)', 'read_file(/)'], deny: policy(['/ws']) });
      expect(args.join('\n')).not.toContain(shared);
    } finally {
      if (previous === undefined) delete process.env['PULSE_COEVOLUTION_ROOT'];
      else process.env['PULSE_COEVOLUTION_ROOT'] = previous;
      rmSync(shared, { recursive: true, force: true });
    }
  });

  it('opens what the kernel opened to the session, as the CC write boundary does', async () => {
    const { runtime } = runtimeWith(createState(baseSession()));
    await runtime.spawn({ tenantId: 'owner:main', sessionId: 'relay-session', workspace: '/ws', ccWriteBoundary: true,
      sessionAccess: [{ path: '/granted/write', mode: 'write' }, { path: '/granted/read', mode: 'read' },
        { path: '/standing/read', mode: 'read', standing: true }],
      allocatedWorkspaces: [{ workItemId: 'wi_20261122_316', resourceId: 'workspace.op_1.0', path: '/managed/wi',
        write: ['/managed/wi', '/managed/common/worktrees/wi'], deny: ['/managed/wi/.git/hooks'] }],
    } as never);
    await runtime.send('owner:main', { type: 'human', content: 'hi' });
    const args = spawned[0]!.args;
    // Write grants and the WI's workspaces are written; reads are open, as in CC.
    expect(settingsOf(args)).toEqual({
      allow: ['command(harness-native)', 'write_file(/ws)', 'write_file(/granted/write)', 'write_file(/managed/wi)',
        'write_file(/managed/common/worktrees/wi)', 'read_file(/)'],
      deny: [...new Set(['write_file(/managed/wi/.git/hooks)',
        ...policy(['/ws', '/granted/write', '/managed/wi', '/managed/common/worktrees/wi'])])] });
    expect(agyAccess({ workspace: '/ws' })).toEqual({ writeRoots: [], deniedWrites: ccPolicyPaths(['/ws']), domains: [] });
  });
});

describe('agy permissions are the CC session permissions', () => {
  const settingsOf = (args: string[]) => JSON.parse(argAfter(args, '--settings')).permissions;

  it('runs without restriction for an Agent whose CC sessions skip permissions with no write boundary', async () => {
    // Asobi's relay.json entry: no ccWriteBoundary. Its CC sessions run with --dangerously-skip-permissions only.
    const agent = { id: 'asobi', runtime: 'agy', workspace: '/ws' } as unknown as AgentConfig;
    const launch = conversationLaunch({ poolKey: 'owner:main', agent, session: undefined, channel: 'telegram',
      intent: 'inbound' });
    expect(launch.ccWriteBoundary).toBe(false);
    const { runtime, errors } = runtimeWith(createState(baseSession()));
    mockSpawn = vi.fn((command: string, args: string[], options: Record<string, any>) => {
      const proc = createMockProcess();  // No sandbox line: none is needed.
      spawned.push({ command, args, options, proc });
      return proc;
    });
    await runtime.spawn(launch);
    await runtime.send('owner:main', { type: 'human', content: 'hi' });
    const args = spawned[0]!.args;
    expect(args).toContain('--dangerously-skip-permissions');
    expect(args).not.toContain('--settings');
    expect(args).not.toContain('--sandbox');
    emitLines(spawned[0]!.proc, newConversationStream());
    await finishProcess(spawned[0]!.proc, 0);
    expect(errors).toEqual([]);
  });

  it('runs inside the CC write boundary for an Agent whose CC sessions have one, from the same data', async () => {
    const repoAccess = { commitRepos: [], allowedDomains: ['github.com', 'api.github.com'] };
    const agent = { id: 'vigil', runtime: 'agy', workspace: '/ws', ccWriteBoundary: true,
      allowedDomains: repoAccess.allowedDomains } as unknown as AgentConfig;
    const launch = conversationLaunch({ poolKey: 'owner:main', agent, session: undefined, channel: 'telegram',
      intent: 'inbound' });
    expect(launch.ccWriteBoundary).toBe(true);
    const { runtime } = runtimeWith(createState(baseSession()));
    await runtime.spawn(launch);
    await runtime.send('owner:main', { type: 'human', content: 'hi' });
    const args = spawned[0]!.args;
    expect(args).toContain('--sandbox');
    expect(args).not.toContain('--dangerously-skip-permissions');
    const cc = ccWriteBoundarySettings(ccWriteRoots({ workspace: '/ws' }), process.execPath, [],
      ccBashAccess(launch.repoAccess)) as { sandbox: { allowUnsandboxedCommands: boolean;
        filesystem: { allowWrite: string[]; denyWrite: string[] }; network?: { allowedDomains: string[] } } };
    const rules = settingsOf(args);
    // CC: reads open (no read rules), writes and domains from these lists, nothing outside its sandbox.
    expect(cc.sandbox.allowUnsandboxedCommands).toBe(false);
    expect(rules.allow).toEqual(['command(harness-native)',
      ...cc.sandbox.filesystem.allowWrite.map((path) => `write_file(${path})`), 'read_file(/)',
      ...cc.sandbox.network!.allowedDomains.map((domain) => `read_url(${domain})`)]);
    expect(rules.deny).toEqual(cc.sandbox.filesystem.denyWrite.map((path) => `write_file(${path})`));
    // Malformed repository access refuses the start, as it refuses a restricted CC session.
    const second = runtimeWith(createState(baseSession()));
    await expect(second.runtime.spawn({ tenantId: 'owner:other', sessionId: 'relay-session', workspace: '/ws',
      ccWriteBoundary: true, repoAccess: { allowedDomains: ['NOT A DOMAIN'] } } as never)).rejects.toThrow();
  });

  it('a /yolo override never widens the permissions of an Agent with a write boundary', async () => {
    const { runtime } = runtimeWith(createState(baseSession()));
    await runtime.spawn({ tenantId: 'owner:main', sessionId: 'relay-session', workspace: '/ws', ccWriteBoundary: true,
      codexYoloDefault: true } as never);
    await runtime.send('owner:main', { type: 'human', content: 'hi' });
    expect(spawned[0]!.args).toContain('--sandbox');
    expect(spawned[0]!.args).not.toContain('--dangerously-skip-permissions');
  });
});

describe('agy restricted run', () => {
  it('stops a turn whose terminal sandbox agy did not enable, before any tool runs', async () => {
    const { runtime, errors } = runtimeWith(createState(baseSession()));
    mockSpawn = vi.fn((command: string, args: string[], options: Record<string, any>) => {
      const proc = createMockProcess();  // No sandbox line: agy ignored --sandbox on this host.
      spawned.push({ command, args, options, proc });
      return proc;
    });
    await start(runtime);
    await runtime.send('owner:main', { type: 'human', content: 'hi' });
    const call = spawned[0]!;
    emitLines(call.proc, [init(CONV, DEFAULT_AGY_MODEL)]);
    await settle();
    expect(call.proc.kill.mock.calls.map((c) => c[0])).toEqual(['SIGTERM']);
    await finishProcess(call.proc, 1);
    expect(errors.map((error) => error.message)).toEqual([AGY_SANDBOX_REFUSAL]);
    // The refused turn's conversation was never adopted.
    expect(proofs.retained).toEqual([]);
  });

  it('an unrestricted turn has no sandbox to prove', async () => {
    const { runtime, errors } = runtimeWith(createState(baseSession()));
    await runtime.spawn({ tenantId: 'owner:main', sessionId: 'relay-session', workspace: '/ws' } as never);
    await runtime.send('owner:main', { type: 'human', content: 'hi' });
    expect(spawned[0]!.args).not.toContain('--sandbox');
    emitLines(spawned[0]!.proc, newConversationStream());
    await finishProcess(spawned[0]!.proc, 0);
    expect(errors).toEqual([]);
  });
});

describe('agy per-turn kernel access', () => {
  const allowOf = (args: string[]) => JSON.parse(argAfter(args, '--settings')).permissions;

  it('launches each turn with the grants the kernel holds for the conversation now', async () => {
    const state = createState(baseSession({ agyConversation: { sessionId: 'relay-session', conversationId: CONV } }));
    const agent = { id: 'main', runtime: 'agy', workspace: '/ws', ccWriteBoundary: true } as unknown as AgentConfig;
    // The production entry: a launch that names no grants and no allocated workspaces.
    const launch = conversationLaunch({ poolKey: 'owner:main', agent, session: state.sessions['owner:main'],
      channel: 'telegram', intent: 'inbound' });
    expect(launch.sessionAccess).toBeUndefined();
    expect(launch.allocatedWorkspaces).toBeUndefined();
    kernel.core = true;
    kernel.readback = async () => ({
      access: [{ path: '/granted/write', mode: 'write' }, { path: '/granted/read', mode: 'read' }], standing: [],
      workspaces: [{ workItemId: 'wi_20261122_316', resourceId: 'workspace.op_1.0', path: '/managed/wi',
        write: ['/managed/wi'], deny: ['/managed/wi/.git/hooks'] }] });
    const { runtime } = runtimeWith(state);
    await runtime.spawn(launch);
    await runtime.send('owner:main', { type: 'human', content: 'one' });
    await settle();
    expect(kernel.reads).toEqual([[CONV, 'owner:main']]);
    expect(allowOf(spawned[0]!.args)).toEqual({
      allow: ['command(harness-native)', 'write_file(/ws)', 'write_file(/granted/write)', 'write_file(/managed/wi)',
        'read_file(/)'],
      deny: [...new Set(['write_file(/managed/wi/.git/hooks)', ...policy(['/ws', '/granted/write', '/managed/wi'])])] });
    emitLines(spawned[0]!.proc, resumedStream());
    await finishProcess(spawned[0]!.proc, 0);

    // Revoked before the next turn: it launches without them.
    kernel.readback = async () => ({ access: [], standing: [], workspaces: [] });
    await runtime.send('owner:main', { type: 'human', content: 'two' });
    await settle();
    expect(kernel.reads).toHaveLength(2);
    expect(allowOf(spawned[1]!.args)).toEqual({ allow: ['command(harness-native)', 'write_file(/ws)', 'read_file(/)'], deny: policy(['/ws']) });
    emitLines(spawned[1]!.proc, resumedStream());
    await finishProcess(spawned[1]!.proc, 0);

    // An unavailable readback keeps the last boundary, as CC does.
    kernel.readback = async () => { throw new Error('session_access_unavailable'); };
    await runtime.send('owner:main', { type: 'human', content: 'three' });
    await settle();
    expect(allowOf(spawned[2]!.args)).toEqual({ allow: ['command(harness-native)', 'write_file(/ws)', 'read_file(/)'], deny: policy(['/ws']) });
  });

  it('refuses a turn whose fresh grants cannot be turned into rules, rather than reuse the last ones', async () => {
    const repo = realpathSync(mkdtempSync(join(dir, 'repo-')));
    // node:child_process is mocked above; this test needs a real repository.
    (await vi.importActual<typeof import('node:child_process')>('node:child_process')).execFileSync('git', ['init', '-q', repo]);
    const state = createState(baseSession({ agyConversation: { sessionId: 'relay-session', conversationId: CONV } }));
    kernel.core = true;
    kernel.readback = async () => ({ access: [{ path: '/granted/write', mode: 'write' }], standing: [], workspaces: [] });
    const { runtime, errors } = runtimeWith(state);
    await runtime.spawn({ tenantId: 'owner:main', sessionId: 'relay-session', workspace: '/ws', ccWriteBoundary: true,
      repoAccess: { commitRepos: [repo] } } as never);
    await runtime.send('owner:main', { type: 'human', content: 'one' });
    await settle();
    expect(allowOf(spawned[0]!.args).allow).toContain('write_file(/granted/write)');
    emitLines(spawned[0]!.proc, resumedStream());
    await finishProcess(spawned[0]!.proc, 0);

    // The grant is revoked and the commit repository can no longer be verified before the next turn.
    kernel.readback = async () => ({ access: [], standing: [], workspaces: [] });
    rmSync(join(repo, '.git'), { recursive: true, force: true });
    await runtime.send('owner:main', { type: 'human', content: 'two' });
    await settle();
    expect(spawned).toHaveLength(1);
    expect(errors.map((error) => error.message).join('\n')).toContain('the turn was not run');
  });

  it('leaves a delivery whose permissions could not be applied to be offered again, and counts it once agy starts', async () => {
    const repo = realpathSync(mkdtempSync(join(dir, 'repo-')));
    const git = (await vi.importActual<typeof import('node:child_process')>('node:child_process')).execFileSync;
    git('git', ['init', '-q', repo]);
    const state = createState(baseSession({ agyConversation: { sessionId: 'relay-session', conversationId: CONV } }));
    kernel.core = true;
    kernel.readback = async () => ({ access: [], standing: [], workspaces: [] });
    const { runtime } = runtimeWith(state);
    await runtime.spawn({ tenantId: 'owner:main', sessionId: 'relay-session', workspace: '/ws', ccWriteBoundary: true,
      repoAccess: { commitRepos: [repo] } } as never);
    const outcomes: string[] = [];
    // The commit repository cannot be verified: the turn is not run.
    rmSync(join(repo, '.git'), { recursive: true, force: true });
    watchInternalTurn('perm-1', outcome => outcomes.push(outcome));
    await runtime.send('owner:main', fencedInternalTurn({ type: 'human', content: 'pr result' }, [CONV], 'perm-1'));
    await settle();
    expect(spawned).toHaveLength(0);
    expect(outcomes).toEqual(['refused']);
    // Repaired: the same delivery offered again starts, and counts as received only once agy names the turn.
    git('git', ['init', '-q', repo]);
    watchInternalTurn('perm-2', outcome => outcomes.push(outcome));
    await runtime.send('owner:main', fencedInternalTurn({ type: 'human', content: 'pr result' }, [CONV], 'perm-2'));
    await settle();
    expect(spawned).toHaveLength(1);
    expect(outcomes).toEqual(['refused']);
    emitLines(spawned[0]!.proc, resumedStream());
    await finishProcess(spawned[0]!.proc, 0);
    expect(outcomes).toEqual(['refused', 'consumed']);
  });

  it('a first turn has no conversation to read yet', async () => {
    kernel.core = true;
    kernel.readback = async () => { throw new Error('not expected'); };
    const { runtime } = runtimeWith(createState(baseSession()));
    await start(runtime);
    await runtime.send('owner:main', { type: 'human', content: 'hi' });
    expect(kernel.reads).toEqual([]);
    expect(allowOf(spawned[0]!.args)).toEqual({ allow: ['command(harness-native)', 'write_file(/ws)', 'read_file(/)'], deny: policy(['/ws']) });
  });
});

describe('agy conversation runtime', () => {
  it('starts a new conversation, replies and persists its id with the cumulative total', async () => {
    const state = createState(baseSession());
    const { runtime, responses, errors, exits } = runtimeWith(state);
    await start(runtime, 'gemini-3.8-flash-low');
    await runtime.send('owner:main', { type: 'human', content: 'Say hello' });
    const call = spawned[0]!;
    expect(call.command).toBe(agyBin);
    expect(call.args).not.toContain('--conversation');
    expect(argAfter(call.args, '--model')).toBe('gemini-3.8-flash-low');
    expect(call.args[1]).toContain('Say hello');
    expect(call.options.env.HOME).toBe(userInfo().homedir);

    emitLines(call.proc, newConversationStream());
    await finishProcess(call.proc, 0);

    expect(errors).toEqual([]);
    expect(exits).toEqual([0]);
    const final = responses.find((msg) => msg.type === 'assistant');
    expect(final?.content).toBe('Hello there!');
    expect(final?.delivery_phase).toBe('final');
    const res = responses.find((msg) => msg.type === 'result')!;
    expect(res.usage).toEqual({ input_tokens: 9772, cache_read_input_tokens: 0, output_tokens: 3, thinking_tokens: 0 });
    expect(res.runtime).toBe('agy');
    expect(res.model).toBe('gemini-3.8-flash-low');
    // session_id means the Relay session to the entry; the agy conversation has its own field.
    expect(res.session_id).toBeUndefined();
    expect(res.agy_conversation_id).toBe(CONV);
    expect(final).not.toHaveProperty('session_id');
    expect(validateNativeModelUsage(res.native_model_usage, res.usage, 'agy')?.models).toEqual([
      { model: 'gemini-3.8-flash-low', provider: null, usage: { input_tokens: 9772, cache_read_input_tokens: 0, output_tokens: 3 } }]);
    expect(state.sessions['owner:main']!.agyConversation).toEqual({ sessionId: 'relay-session', conversationId: CONV,
      usage: { input_tokens: 9772, cache_read_input_tokens: 0, output_tokens: 3, thinking_tokens: 0 } });
    // A successful turn leaves no log behind.
    const logRoot = join(dir, 'logs');
    const files = existsSync(logRoot) ? readdirSync(logRoot, { recursive: true }).map(String)
      .filter((name) => statSync(join(logRoot, name)).isFile()) : [];
    expect(files).toEqual([]);
  });

  it('resumes the persisted conversation and reports only this turn, not the conversation total', async () => {
    const state = createState(baseSession({ agyConversation: { sessionId: 'relay-session', conversationId: CONV,
      usage: { input_tokens: 9766, cache_read_input_tokens: 0, output_tokens: 1, thinking_tokens: 0 } } }));
    const { runtime, responses } = runtimeWith(state);
    await start(runtime, 'gemini-4-neon-high');
    await runtime.send('owner:main', { type: 'human', content: 'What did you reply?' });
    const call = spawned[0]!;
    expect(argAfter(call.args, '--conversation')).toBe(CONV);
    emitLines(call.proc, resumedStream(true));
    await finishProcess(call.proc, 0);

    const res = responses.find((msg) => msg.type === 'result')!;
    expect(res.usage).toEqual({ input_tokens: 10044, cache_read_input_tokens: 0, output_tokens: 38, thinking_tokens: 36 });
    expect(state.sessions['owner:main']!.agyConversation?.usage).toEqual(
      { input_tokens: 19810, cache_read_input_tokens: 0, output_tokens: 39, thinking_tokens: 36 });
  });

  it('falls back to the cumulative delta when no step usage was streamed', async () => {
    const state = createState(baseSession({ agyConversation: { sessionId: 'relay-session', conversationId: CONV,
      usage: { input_tokens: 9766, cache_read_input_tokens: 0, output_tokens: 1, thinking_tokens: 0 } } }));
    const { runtime, responses } = runtimeWith(state);
    await start(runtime, 'gemini-4-neon-high');
    await runtime.send('owner:main', { type: 'human', content: 'again' });
    emitLines(spawned[0]!.proc, resumedStream(false));
    await finishProcess(spawned[0]!.proc, 0);
    expect(responses.find((msg) => msg.type === 'result')!.usage).toEqual(
      { input_tokens: 10044, cache_read_input_tokens: 0, output_tokens: 38, thinking_tokens: 36 });
  });

  it('does not take a partial step sum: a finished step without usage falls back to the cumulative delta', async () => {
    const state = createState(baseSession({ agyConversation: { sessionId: 'relay-session', conversationId: CONV,
      usage: { input_tokens: 9766, cache_read_input_tokens: 0, output_tokens: 1, thinking_tokens: 0 } } }));
    const { runtime, responses } = runtimeWith(state);
    await start(runtime, 'gemini-4-neon-high');
    await runtime.send('owner:main', { type: 'human', content: 'again' });
    const stream = resumedStream(true);
    // One more finished response step, without usage, before the measured one.
    stream.splice(2, 0, step(CONV, { step_index: 3, state: 'DONE', step_type: 'agent_response', text_delta: '' }));
    const measured = stream[4] as { step_update: { usage: Record<string, number> } };
    measured.step_update.usage = { ...measured.step_update.usage, input_tokens: 1 };
    emitLines(spawned[0]!.proc, stream);
    await finishProcess(spawned[0]!.proc, 0);
    expect(responses.find((msg) => msg.type === 'result')!.usage).toEqual(
      { input_tokens: 10044, cache_read_input_tokens: 0, output_tokens: 38, thinking_tokens: 36 });
  });

  it('settles on the final result even when exit is reported before stdout is read', async () => {
    const state = createState(baseSession());
    const { runtime, responses, errors } = runtimeWith(state);
    await start(runtime, 'gemini-3.8-flash-low');
    await runtime.send('owner:main', { type: 'human', content: 'Say hello' });
    const proc = spawned[0]!.proc;
    const stream = newConversationStream();
    emitLines(proc, stream.slice(0, -1));
    await settle();
    proc.emit('exit', 0, null);
    await settle();
    expect(responses.find((msg) => msg.type === 'result')).toBeUndefined();
    emitLines(proc, stream.slice(-1));
    proc.stdout.end();
    await settle();
    expect(errors).toEqual([]);
    expect(responses.find((msg) => msg.type === 'assistant')?.content).toBe('Hello there!');
    expect(responses.find((msg) => msg.type === 'result')!.usage).toEqual(
      { input_tokens: 9772, cache_read_input_tokens: 0, output_tokens: 3, thinking_tokens: 0 });
  });

  it('leaves the turn usage unknown when a resumed turn has neither steps nor a baseline', async () => {
    const state = createState(baseSession({ agyConversation: { sessionId: 'relay-session', conversationId: CONV } }));
    const { runtime, responses } = runtimeWith(state);
    await start(runtime, 'gemini-4-neon-high');
    await runtime.send('owner:main', { type: 'human', content: 'again' });
    emitLines(spawned[0]!.proc, resumedStream(false));
    await finishProcess(spawned[0]!.proc, 0);
    const res = responses.find((msg) => msg.type === 'result')!;
    expect(res.usage).toBeUndefined();
    expect(res.native_model_usage).toBeUndefined();
  });

  it('writes a runtime=agy session proof for the turn process once agy names its conversation', async () => {
    const state = createState(baseSession());
    const { runtime } = runtimeWith(state);
    await start(runtime);
    await runtime.send('owner:main', { type: 'human', content: 'one' });
    const call = spawned[0]!;
    // Removal is armed at spawn, before any proof exists, as for Codex and Pi.
    expect(proofs.forgotten).toEqual([call.proc]);
    expect(proofs.observed).toEqual([]);
    emitLines(call.proc, newConversationStream());
    await finishProcess(call.proc, 0);
    expect(proofs.observed).toEqual([[call.proc.pid, CONV, 'owner:main', 'agy', 'relay-session']]);
    // The retained record outlives the process, so /wi still recognizes the idle conversation.
    expect(proofs.retained).toEqual([[CONV, 'owner:main', 'relay-session']]);

    await runtime.send('owner:main', { type: 'human', content: 'two' });
    emitLines(spawned[1]!.proc, resumedStream(true));
    await finishProcess(spawned[1]!.proc, 0);
    expect(proofs.forgotten).toEqual([call.proc, spawned[1]!.proc]);
    expect(proofs.observed[1]).toEqual([spawned[1]!.proc.pid, CONV, 'owner:main', 'agy', 'relay-session']);
  });

  it('keeps the Relay session through the entry result capture and resumes the conversation after a rebuild', async () => {
    const state = createState(baseSession());
    const router = { getAgents: () => [{ id: 'main' }], overrideSessionId: vi.fn() };
    const first = runtimeWith(state);
    // The Relay entry consumes every result as index.ts does.
    first.runtime.on('response', (tenant, msg) => {
      if ((msg as StreamJsonMessage).type === 'result') {
        captureReportedSessionId(tenant as string, msg as StreamJsonMessage, { router: router as never, stateManager: state });
      }
    });
    await start(first.runtime);
    await first.runtime.send('owner:main', { type: 'human', content: 'one' });
    emitLines(spawned[0]!.proc, newConversationStream());
    await finishProcess(spawned[0]!.proc, 0);
    expect(router.overrideSessionId).not.toHaveBeenCalled();
    expect(state.sessions['owner:main']).toMatchObject({ sessionId: 'relay-session', runtime: 'agy',
      agyConversation: { sessionId: 'relay-session', conversationId: CONV } });

    // /kill or a Relay restart: the next process is launched from the persisted session alone.
    await first.runtime.kill('owner:main');
    const agent = { id: 'main', runtime: 'agy', workspace: '/ws' } as unknown as AgentConfig;
    const launch = conversationLaunch({ poolKey: 'owner:main', agent, session: state.sessions['owner:main'],
      channel: 'telegram', intent: 'recovery' });
    expect(launch.sessionId).toBe('relay-session');
    const second = runtimeWith(state);
    await second.runtime.spawn(launch);
    await second.runtime.send('owner:main', { type: 'human', content: 'two' });
    expect(argAfter(spawned[1]!.args, '--conversation')).toBe(CONV);
    emitLines(spawned[1]!.proc, resumedStream(true));
    await finishProcess(spawned[1]!.proc, 0);
    expect(second.responses.find((msg) => msg.type === 'result')!.usage)
      .toEqual({ input_tokens: 10044, cache_read_input_tokens: 0, output_tokens: 38, thinking_tokens: 36 });
  });

  it('lets a child that exits after its session was replaced change nothing', async () => {
    const state = createState(baseSession());
    const { runtime, responses, errors, exits } = runtimeWith(state);
    await start(runtime);
    await runtime.send('owner:main', { type: 'human', content: 'long' });
    const old = spawned[0]!.proc;
    // SIGTERM is ignored for now; the child outlives the kill.
    old.kill = vi.fn(() => true) as never;
    emitLines(old, [init(CONV, DEFAULT_AGY_MODEL)]);
    await settle();
    const before = responses.length;
    expect(state.sessions['owner:main']!.agyConversation?.conversationId).toBe(CONV);

    // /new: the old process is killed and the channel records a fresh codex session.
    await runtime.kill('owner:main');
    state.sessions['owner:main'] = baseSession({ sessionId: 'new-session', runtime: 'codex' });
    const renewed = JSON.parse(JSON.stringify(state.sessions['owner:main']));

    emitLines(old, newConversationStream().slice(1));
    await finishProcess(old, 0);
    expect(state.sessions['owner:main']).toEqual(renewed);
    expect(responses.length).toBe(before);
    expect(errors).toEqual([]);
    expect(exits).toEqual([]);

    // A replacement agy process of the same tenant is not disturbed by the old child either.
    await runtime.spawn({ tenantId: 'owner:main', sessionId: 'new-session', workspace: '/ws' } as never);
    old.emit('exit', 1, null);
    await settle();
    expect(runtime.get('owner:main')?.state).toBe('idle');
  });

  it('refuses to spawn for an Agent isolated under its own Linux user, even when called directly', async () => {
    const { runtime } = runtimeWith(createState(baseSession()));
    await expect(runtime.spawn({ tenantId: 'owner:main', sessionId: 'relay-session', workspace: '/ws',
      linuxUser: 'agent-iso' } as never)).rejects.toThrow('独立 Linux 用户隔离');
    expect(runtime.get('owner:main')).toBeUndefined();
    expect(spawned).toEqual([]);
  });

  it('/stop escalates to SIGKILL when SIGTERM is ignored and stays busy until the child exits', async () => {
    const { runtime, responses, errors, exits } = runtimeWith(createState(baseSession()), { killGraceMs: 20 });
    mockSpawn = vi.fn((command: string, args: string[], options: Record<string, any>) => {
      const proc = createMockProcess((p, signal) => {
        if (signal !== 'SIGKILL') return; // SIGTERM is ignored
        p.stdout.end();
        setImmediate(() => p.emit('exit', null, 'SIGKILL'));
      });
      sandboxOn(args);
      spawned.push({ command, args, options, proc });
      return proc;
    });
    await start(runtime);
    await runtime.send('owner:main', { type: 'human', content: 'long' });
    const call = spawned[0]!;
    emitLines(call.proc, [init(CONV, DEFAULT_AGY_MODEL)]);
    await settle();
    await runtime.abortTurn!('owner:main');
    await settle();
    expect(call.proc.kill.mock.calls.map((c) => c[0])).toEqual(['SIGTERM']);
    expect(runtime.get('owner:main')?.state).toBe('busy');
    await new Promise((resolve) => setTimeout(resolve, 40));
    await settle();
    expect(call.proc.kill.mock.calls.map((c) => c[0])).toEqual(['SIGTERM', 'SIGKILL']);
    expect(runtime.get('owner:main')?.state).toBe('idle');
    expect(responses.find((msg) => msg.type === 'result')).toMatchObject({ subtype: 'interrupted', interrupted: true });
    expect(errors).toEqual([]);
    expect(exits).toEqual([0]);
  });

  it('/stop settles the turn a bounded time after SIGKILL when the child never reports its exit', async () => {
    const { runtime, responses, exits } = runtimeWith(createState(baseSession()), { killGraceMs: 15 });
    mockSpawn = vi.fn((command: string, args: string[], options: Record<string, any>) => {
      const proc = createMockProcess(() => { /* ignores every signal */ });
      spawned.push({ command, args, options, proc });
      return proc;
    });
    const warn = vi.spyOn(console, 'warn').mockImplementation(() => undefined);
    try {
      await start(runtime);
      await runtime.send('owner:main', { type: 'human', content: 'long' });
      await runtime.abortTurn!('owner:main');
      await new Promise((resolve) => setTimeout(resolve, 60));
      await settle();
      expect(spawned[0]!.proc.kill.mock.calls.map((c) => c[0])).toEqual(['SIGTERM', 'SIGKILL']);
      expect(runtime.get('owner:main')?.state).toBe('idle');
      expect(responses.find((msg) => msg.type === 'result')).toMatchObject({ subtype: 'interrupted' });
      expect(exits).toEqual([0]);
      // A late exit of the stuck child changes nothing.
      spawned[0]!.proc.emit('exit', null, 'SIGKILL');
      await settle();
      expect(exits).toEqual([0]);
    } finally {
      warn.mockRestore();
    }
  });

  it('kill (/new, /reset, runtime switch) stops a child that ignores SIGTERM and reports nothing', async () => {
    const { runtime, responses, errors, exits, sessionEnds } = runtimeWith(createState(baseSession()), { killGraceMs: 15 });
    mockSpawn = vi.fn((command: string, args: string[], options: Record<string, any>) => {
      const proc = createMockProcess((p, signal) => {
        if (signal !== 'SIGKILL') return;
        p.stdout.end();
        setImmediate(() => p.emit('exit', null, 'SIGKILL'));
      });
      spawned.push({ command, args, options, proc });
      return proc;
    });
    await start(runtime);
    await runtime.send('owner:main', { type: 'human', content: 'long' });
    const before = responses.length;
    await runtime.kill('owner:main');
    expect(runtime.get('owner:main')).toBeUndefined();
    await new Promise((resolve) => setTimeout(resolve, 40));
    await settle();
    expect(spawned[0]!.proc.kill.mock.calls.map((c) => c[0])).toEqual(['SIGTERM', 'SIGKILL']);
    expect(responses.length).toBe(before);
    expect([errors, exits, sessionEnds]).toEqual([[], [], []]);
  });

  it('skips stream lines that are valid JSON but no event object, and still replies', async () => {
    const { runtime, responses, errors } = runtimeWith(createState(baseSession()));
    await start(runtime, 'gemini-3.8-flash-low');
    await runtime.send('owner:main', { type: 'human', content: 'Say hello' });
    const call = spawned[0]!;
    for (const raw of ['null', '[]', '1', '"text"', '{not json']) call.proc.stdout.write(`${raw}\n`);
    emitLines(call.proc, [{ event: 'init', init: 5 }, { event: 'step_update', step_update: null }, { event: 'result', result: [] }]);
    emitLines(call.proc, newConversationStream());
    await finishProcess(call.proc, 0);
    expect(errors).toEqual([]);
    expect(responses.find((msg) => msg.type === 'assistant')?.content).toBe('Hello there!');
  });

  it('retains a resumed conversation at spawn, before any new turn, so an idle upgraded session takes /wi', async () => {
    const state = createState(baseSession({ agyConversation: { sessionId: 'relay-session', conversationId: CONV } }));
    const { runtime } = runtimeWith(state);
    await start(runtime);
    expect(spawned).toHaveLength(0);
    expect(proofs.retained).toEqual([[CONV, 'owner:main', 'relay-session']]);
    expect(runtime.get('owner:main')).toMatchObject({ agyConversationId: CONV });
    proofs.retained.length = 0;
    // A conversation saved for another Relay session is neither resumed nor retained for this one.
    const other = runtimeWith(createState(baseSession({ agyConversation: { sessionId: 'older-session', conversationId: CONV } })));
    await start(other.runtime);
    expect(proofs.retained).toEqual([]);
  });

  it('does not resume a conversation that belongs to another Relay session', async () => {
    const state = createState(baseSession({ agyConversation: { sessionId: 'older-session', conversationId: CONV } }));
    const { runtime } = runtimeWith(state);
    await start(runtime);
    await runtime.send('owner:main', { type: 'human', content: 'fresh' });
    expect(spawned[0]!.args).not.toContain('--conversation');
  });

  it('continues the same conversation on the next turn and switches model in place', async () => {
    const state = createState(baseSession());
    const { runtime } = runtimeWith(state);
    await start(runtime, 'claude-opus-5-5');
    await runtime.send('owner:main', { type: 'human', content: 'one' });
    expect(argAfter(spawned[0]!.args, '--model')).toBe(DEFAULT_AGY_MODEL);
    emitLines(spawned[0]!.proc, newConversationStream(CONV, DEFAULT_AGY_MODEL));
    await finishProcess(spawned[0]!.proc, 0);

    await runtime.setModel!('owner:main', { provider: 'agy', model: 'gemini-3.8-flash-medium', reasoning: 'medium' });
    await expect(runtime.setModel!('owner:main', { provider: 'agy', model: 'gpt-6-sol', reasoning: 'medium' }))
      .rejects.toThrow('agy does not offer model gpt-6-sol');
    await runtime.send('owner:main', { type: 'human', content: 'two' });
    expect(argAfter(spawned[1]!.args, '--conversation')).toBe(CONV);
    expect(argAfter(spawned[1]!.args, '--model')).toBe('gemini-3.8-flash-medium');
    expect((await runtime.listModels!('owner:main'))[0]).toEqual(
      { provider: 'agy', model: 'gemini-4-neon-high', displayName: 'Gemini 4 Neon (High)' });
  });

  it('runs the session /think level as the model family variant, and an explicit model drops it', async () => {
    const state = createState(baseSession({ reasoningLevel: 'low' }));
    const { runtime } = runtimeWith(state);
    await runtime.spawn({ tenantId: 'owner:main', sessionId: 'relay-session', workspace: '/ws',
      ccModel: 'gemini-4-argon-high', reasoningLevel: 'low' } as never);
    await runtime.send('owner:main', { type: 'human', content: 'one' });
    expect(argAfter(spawned[0]!.args, '--model')).toBe('gemini-4-argon-low');
    expect(spawned[0]!.args).not.toContain('--effort');
    emitLines(spawned[0]!.proc, newConversationStream(CONV, 'gemini-4-argon-low'));
    await finishProcess(spawned[0]!.proc, 0);

    expect(await runtime.listThinkingLevels!('owner:main')).toEqual(['low', 'medium', 'high']);
    await expect(runtime.setThinkingLevel!('owner:main', 'xhigh')).rejects.toThrow('no xhigh variant');
    await runtime.setThinkingLevel!('owner:main', 'medium');
    await runtime.send('owner:main', { type: 'human', content: 'two' });
    expect(argAfter(spawned[1]!.args, '--model')).toBe('gemini-4-argon-medium');
    expect(argAfter(spawned[1]!.args, '--conversation')).toBe(CONV);
    emitLines(spawned[1]!.proc, newConversationStream(CONV, 'gemini-4-argon-medium'));
    await finishProcess(spawned[1]!.proc, 0);

    await runtime.setModel!('owner:main', { provider: 'agy', model: 'claude-opus-5-5-high', reasoning: 'medium' });
    await runtime.send('owner:main', { type: 'human', content: 'three' });
    expect(argAfter(spawned[2]!.args, '--model')).toBe('claude-opus-5-5-high');
  });

  it('attributes the next turns to the WI binding a /wi command gave the live conversation', async () => {
    const state = createState(baseSession());
    const { runtime } = runtimeWith(state);
    await start(runtime);
    const binding = buildRuntimeUsageBinding({ workItemId: 'wi_20261122_053', agentId: 'asobi', phase: 'conversation',
      epoch: 1, runtime: 'agy', procurementSource: 'unknown' });
    runtime.bindRuntimeUsage!('owner:main', binding);
    expect(runtime.get('owner:main')?.usageBindingSha256).toBe(binding.binding_sha256);
    await runtime.send('owner:main', { type: 'human', content: 'one' });
    expect(JSON.parse(spawned[0]!.options.env.PULSE_RUNTIME_USAGE_BINDING)).toMatchObject({ work_item_id: 'wi_20261122_053' });
  });

  it('runs a delivery queued behind a turn only in the conversation it was routed to', async () => {
    const state = createState(baseSession({ agyConversation: { sessionId: 'relay-session', conversationId: CONV } }));
    const { runtime, responses } = runtimeWith(state);
    await start(runtime);
    const outcomes: string[] = [];
    // A turn is running when the delivery (routed to CONV) is queued; that turn then lands in another conversation.
    await runtime.send('owner:main', { type: 'human', content: 'one' });
    watchInternalTurn('ref-1', outcome => outcomes.push(outcome));
    await runtime.send('owner:main', fencedInternalTurn({ type: 'human', content: 'pr result' }, [CONV], 'ref-1'));
    emitLines(spawned[0]!.proc, newConversationStream(OTHER));
    await finishProcess(spawned[0]!.proc, 0);
    expect(runtime.get('owner:main')).toMatchObject({ agyConversationId: OTHER, state: 'idle' });
    // Refused where it would start: the new conversation never sees it, and the manager sees a control result.
    expect(spawned).toHaveLength(1);
    expect(outcomes).toEqual(['refused']);
    expect(responses.at(-1)).toMatchObject({ type: 'result', subtype: INTERNAL_TURN_REFUSED, delivery_visibility: 'control' });
    // A delivery routed to the conversation that runs now starts, and its prompt carries no fence.
    watchInternalTurn('ref-2', outcome => outcomes.push(outcome));
    await runtime.send('owner:main', fencedInternalTurn({ type: 'human', content: 'pr result' }, [OTHER], 'ref-2'));
    expect(spawned).toHaveLength(2);
    expect(argAfter(spawned[1]!.args, '--conversation')).toBe(OTHER);
    // Received once agy has started the turn, not when it was launched.
    expect(outcomes).toEqual(['refused']);
    emitLines(spawned[1]!.proc, newConversationStream(OTHER));
    await finishProcess(spawned[1]!.proc, 0);
    expect(outcomes).toEqual(['refused', 'consumed']);
    expect(spawned[1]!.args.join(' ')).not.toContain('expect_native_ids');
  });

  it('withdraws the WI binding when agy answers a resumed turn in another conversation, live and on recovery', async () => {
    const binding = buildRuntimeUsageBinding({ workItemId: 'wi_20261122_053', agentId: 'asobi', phase: 'conversation',
      epoch: 1, runtime: 'agy', procurementSource: 'unknown' });
    const state = createState(baseSession({ agyConversation: { sessionId: 'relay-session', conversationId: CONV },
      agyWorkItemBinding: { sessionId: 'relay-session', conversationId: CONV, usageBinding: binding } }));
    const agent = { id: 'main', runtime: 'agy', workspace: '/ws' } as unknown as AgentConfig;
    const launch = () => conversationLaunch({ poolKey: 'owner:main', agent, session: state.sessions['owner:main'],
      channel: 'telegram', intent: 'recovery' });
    // The recovery launch carries the binding while the session resumes the bound conversation.
    expect(launch().computeCostBinding).toEqual(binding);
    const { runtime } = runtimeWith(state);
    await runtime.spawn(launch());
    expect(runtime.runtimeUsageBinding!('owner:main')).toEqual(binding);
    await runtime.send('owner:main', { type: 'human', content: 'one' });
    expect(argAfter(spawned[0]!.args, '--conversation')).toBe(CONV);
    expect(JSON.parse(spawned[0]!.options.env.PULSE_RUNTIME_USAGE_BINDING)).toMatchObject({ work_item_id: 'wi_20261122_053' });
    // agy replaced the requested conversation.
    emitLines(spawned[0]!.proc, newConversationStream(OTHER));
    await finishProcess(spawned[0]!.proc, 0);
    expect(runtime.get('owner:main')).toMatchObject({ agyConversationId: OTHER, usageBindingSha256: undefined });
    expect(runtime.runtimeUsageBinding!('owner:main')).toBeUndefined();
    expect(state.sessions['owner:main']!.agyWorkItemBinding).toBeUndefined();
    expect(state.sessions['owner:main']!.agyConversation).toMatchObject({ conversationId: OTHER });
    // The live process's next turn runs without it ...
    await runtime.send('owner:main', { type: 'human', content: 'two' });
    expect(spawned[1]!.options.env.PULSE_RUNTIME_USAGE_BINDING).toBeUndefined();
    // ... and so does a recovery launch.
    expect(launch().computeCostBinding).toBeUndefined();
  });

  it('a saved binding for another conversation than the one the session resumes is never launched', () => {
    const binding = buildRuntimeUsageBinding({ workItemId: 'wi_20261122_053', agentId: 'asobi', phase: 'conversation',
      epoch: 1, runtime: 'agy', procurementSource: 'unknown' });
    const agent = { id: 'main', runtime: 'agy', workspace: '/ws' } as unknown as AgentConfig;
    const session = (bound: string | undefined, saved: string | undefined) => baseSession({
      ...(saved ? { agyConversation: { sessionId: 'relay-session', conversationId: saved } } : {}),
      agyWorkItemBinding: { sessionId: 'relay-session', ...(bound ? { conversationId: bound } : {}), usageBinding: binding } });
    const carried = (bound: string | undefined, saved: string | undefined) => conversationLaunch({ poolKey: 'owner:main',
      agent, session: session(bound, saved), channel: 'telegram', intent: 'recovery' }).computeCostBinding;
    expect(carried(CONV, CONV)).toEqual(binding);
    expect(carried(CONV, OTHER)).toBeUndefined();   // A failed write left the old binding behind.
    expect(carried(undefined, undefined)).toEqual(binding);  // A rollover successor before its first conversation.
    expect(carried(undefined, OTHER)).toBeUndefined();
  });

  it('a binding issued before the first conversation adopts the one agy starts', async () => {
    const binding = buildRuntimeUsageBinding({ workItemId: 'wi_20261122_053', agentId: 'asobi', phase: 'conversation',
      epoch: 1, runtime: 'agy', procurementSource: 'unknown' });
    const state = createState(baseSession({ agyWorkItemBinding: { sessionId: 'relay-session', usageBinding: binding } }));
    const { runtime } = runtimeWith(state);
    await runtime.spawn({ tenantId: 'owner:main', sessionId: 'relay-session', workspace: '/ws', computeCostBinding: binding } as never);
    await runtime.send('owner:main', { type: 'human', content: 'bootstrap' });
    emitLines(spawned[0]!.proc, newConversationStream());
    await finishProcess(spawned[0]!.proc, 0);
    expect(state.sessions['owner:main']!.agyWorkItemBinding).toMatchObject({ conversationId: CONV, usageBinding: binding });
    expect(runtime.runtimeUsageBinding!('owner:main')).toEqual(binding);
  });

  it('catalogs every model of the measured agy 1.5.13 list', () => {
    // `agy models` stdout, wi_20261120_666 N01 fixture 00-models.txt.
    const measured = [
      'gemini-4-neon-high\tGemini 4 Neon (High)',
      'gemini-4-neon-xhigh\tGemini 4 Neon (XHigh)',
      'gemini-4-neon-medium\tGemini 4 Neon (Medium)',
      'gemini-4-neon-low\tGemini 4 Neon (Low)',
      'gemini-4-argon-medium\tGemini 4 Argon (Medium)',
      'gemini-4-argon-high\tGemini 4 Argon (High)',
      'gemini-4-argon-low\tGemini 4 Argon (Low)',
      'gemini-4-hydrogen-high\tGemini 4 Hydrogen (High)',
      'gemini-4-hydrogen-xhigh\tGemini 4 Hydrogen (XHigh)',
      'gemini-4-hydrogen-medium\tGemini 4 Hydrogen (Medium)',
      'gemini-4-hydrogen-low\tGemini 4 Hydrogen (Low)',
      'gemini-pro-orchestrator-neon\tOrchestrator (Neon)',
      'gemini-pro-orchestrator-g1-pro\tOrchestrator (Argon)',
      'gemini-3.8-flash-high\tGemini 3.8 Flash (High)',
      'gemini-3.8-flash-medium\tGemini 3.8 Flash (Medium)',
      'gemini-3.8-flash-low\tGemini 3.8 Flash (Low)',
      'claude-opus-5-5-low\tClaude Opus 5.5 (Low)',
      'claude-opus-5-5-medium\tClaude Opus 5.5 (Medium)',
      'claude-opus-5-5-high\tClaude Opus 5.5 (High)',
      'claude-sonnet-5-5-low\tClaude Sonnet 5.5 (Low)',
      'claude-sonnet-5-5-medium\tClaude Sonnet 5.5 (Medium)',
      'claude-sonnet-5-5-high\tClaude Sonnet 5.5 (High)',
      'gpt-oss-120b-medium\tGPT-OSS 120B (Medium)',
    ].flatMap((line) => line.split('\t'));
    const listed = agyModelOptions().flatMap((option) => [option.model, option.displayName]);
    expect(listed).toEqual(measured);
    expect(agyModelLevels('gpt-oss-120b-medium')).toEqual(['medium']);
    expect(agyModelLevels('gemini-pro-orchestrator-neon')).toEqual([]);
    expect(agyModelForLevel('claude-sonnet-5-5-medium', 'low')).toBe('claude-sonnet-5-5-low');
  });

  it('reports a refused proxy as an egress failure from the turn log, not as an unknown model', async () => {
    const { runtime, responses, errors, sessionEnds, order } = runtimeWith(createState(baseSession()));
    await start(runtime, 'gemini-3.8-flash-low');
    await runtime.send('owner:main', { type: 'human', content: 'hi' });
    const call = spawned[0]!;
    writeFileSync(argAfter(call.args, '--log-file'), E03_LOG);
    emitLines(call.proc, [{ event: 'result', result: { conversation_id: '', status: 'ERROR', response: '',
      error: 'invalid model selection (--model "gemini-3.8-flash-low" --effort ""): model gemini-3.8-flash-low is not recognized as a known model or custom model in settings',
      usage: { input_tokens: 0, output_tokens: 0, thinking_tokens: 0, cache_read_tokens: 0, total_tokens: 0 }, finished_reason: 'error' } }]);
    await finishProcess(call.proc, 1);

    expect(errors).toHaveLength(1);
    expect(errors[0]!.message).toMatch(/^agy egress unavailable: proxyconnect/);
    expect(errors[0]!.message).toContain('not retried through another egress');
    const notice = publicRuntimeExceptionNotice('agy', errors[0]);
    expect(notice).toContain('agy 运行失败');
    expect(notice).toContain('agy 出口不可用');
    expect(notice).toContain('未改走其他出口');
    expect(spawned).toHaveLength(1);
    expect(responses.find((msg) => msg.type === 'result')?.is_error).toBe(true);
    // One failed turn, one explicit notice: no session-end, so no channel reports an automatic recovery.
    expect(order).toEqual(['response:result', 'error', 'exit']);
    expect(sessionEnds).toEqual([]);
    // The failed turn keeps its log for diagnosis.
    expect(existsSync(argAfter(call.args, '--log-file'))).toBe(true);
  });

  it('times out a turn that hangs before it starts and names the stalled egress', async () => {
    const { runtime, errors, exits, sessionEnds } = runtimeWith(createState(baseSession()), { startTimeoutMs: 30, killGraceMs: 10 });
    mockSpawn = vi.fn((command: string, args: string[], options: Record<string, any>) => {
      const proc = createMockProcess((p) => {
        writeFileSync(argAfter(args, '--log-file'), E04_LOG);
        setImmediate(() => p.emit('exit', 1, null));
      });
      spawned.push({ command, args, options, proc });
      return proc;
    });
    await start(runtime, 'gemini-3.8-flash-low');
    await runtime.send('owner:main', { type: 'human', content: 'hi' });
    await new Promise((resolve) => setTimeout(resolve, 80));
    await settle();
    expect(spawned[0]!.proc.kill).toHaveBeenCalledWith('SIGTERM');
    expect(errors[0]!.message).toMatch(/^agy egress unavailable: context deadline exceeded \(daily-cloudcode-pa\.googleapis\.com\)/);
    expect(exits).toEqual([1]);
    expect(sessionEnds).toEqual([]);
  });

  it('reports a plain start timeout when the log shows no egress fault', async () => {
    const { runtime, errors } = runtimeWith(createState(baseSession()), { startTimeoutMs: 30, killGraceMs: 10 });
    await start(runtime);
    await runtime.send('owner:main', { type: 'human', content: 'hi' });
    await new Promise((resolve) => setTimeout(resolve, 80));
    await settle();
    expect(errors[0]!.message).toBe('agy conversation runtime timed out after 30ms');
    expect(publicRuntimeExceptionNotice('agy', errors[0])).toContain('agy 运行超时');
  });

  it('turns denied permissions with an empty reply into an explicit failure', async () => {
    const { runtime, responses, errors, sessionEnds } = runtimeWith(createState(baseSession()));
    await start(runtime, 'gemini-3.8-flash-low');
    await runtime.send('owner:main', { type: 'human', content: 'write a file' });
    const usage = { input_tokens: 9803, output_tokens: 42, thinking_tokens: 0, cache_read_tokens: 0, total_tokens: 9845 };
    emitLines(spawned[0]!.proc, [init(CONV, 'gemini-3.8-flash-low'),
      step(CONV, { step_index: 1, state: 'DONE', step_type: 'agent_response', usage }),
      result(CONV, { response: '', usage, denied_actions: [{ action: 'write_file', display_name: 'Edit' }] })]);
    await finishProcess(spawned[0]!.proc, 0);
    expect(errors[0]!.message).toBe('agy permission denied: write_file');
    expect(sessionEnds).toEqual([]);
    expect(publicRuntimeExceptionNotice('agy', errors[0])).toContain('拒绝了所需的工具权限');
    const res = responses.find((msg) => msg.type === 'result')!;
    expect(res.is_error).toBe(true);
    expect(res.denied_actions).toEqual([{ action: 'write_file', display_name: 'Edit' }]);
    expect(res.usage).toEqual({ input_tokens: 9803, cache_read_input_tokens: 0, output_tokens: 42, thinking_tokens: 0 });
  });

  it('delivers a reply with a notice when only some tools were denied', async () => {
    const { runtime, responses, errors } = runtimeWith(createState(baseSession()));
    await start(runtime);
    await runtime.send('owner:main', { type: 'human', content: 'try' });
    emitLines(spawned[0]!.proc, [init(CONV, DEFAULT_AGY_MODEL),
      result(CONV, { response: 'I could not run the command.', usage: { input_tokens: 1, output_tokens: 1, thinking_tokens: 0, cache_read_tokens: 0, total_tokens: 2 },
        denied_actions: [{ action: 'run_command', display_name: 'Bash' }] })]);
    await finishProcess(spawned[0]!.proc, 0);
    expect(errors).toEqual([]);
    const final = responses.find((msg) => msg.type === 'assistant')!;
    expect(final.content).toContain('I could not run the command.');
    expect(final.content).toContain('run_command（Bash）');
  });

  it('publishes tool steps and the text before them as work process', async () => {
    const { runtime, responses } = runtimeWith(createState(baseSession()));
    await start(runtime);
    await runtime.send('owner:main', { type: 'human', content: 'read' });
    emitLines(spawned[0]!.proc, [init(CONV, DEFAULT_AGY_MODEL),
      step(CONV, { step_index: 1, state: 'ACTIVE', step_type: 'agent_response', text_delta: 'Reading the file.' }),
      step(CONV, { step_index: 1, state: 'DONE', step_type: 'agent_response', usage: { input_tokens: 9804, output_tokens: 35, thinking_tokens: 0, cache_read_tokens: 0, total_tokens: 9839 } }),
      step(CONV, { step_index: 2, state: 'ACTIVE', step_type: 'tool', tool_name: 'read_file', tool_info: { name: 'read_file', parameters: { Path: '/ws/a.txt' } } }),
      step(CONV, { step_index: 2, state: 'DONE', step_type: 'tool', tool_name: 'read_file', tool_info: { name: 'read_file', parameters: { Path: '/ws/a.txt' }, output: '1 lines, 14 bytes' } }),
      step(CONV, { step_index: 3, state: 'ACTIVE', step_type: 'agent_response', text_delta: 'alpha-token-A' }),
      step(CONV, { step_index: 3, state: 'DONE', step_type: 'agent_response', text_delta: '\n', usage: { input_tokens: 2168, output_tokens: 5, thinking_tokens: 0, cache_read_tokens: 7761, total_tokens: 2173 } }),
      result(CONV, { response: 'alpha-token-A\n', usage: { input_tokens: 11972, output_tokens: 40, thinking_tokens: 0, cache_read_tokens: 7761, total_tokens: 12012 } })]);
    await finishProcess(spawned[0]!.proc, 0);
    const events = responses.filter((msg) => msg.type === 'work_process').map((msg) => msg.process_event as Record<string, unknown>);
    expect(events[0]).toMatchObject({ kind: 'commentary', output: 'Reading the file.' });
    expect(events.at(-1)).toMatchObject({ kind: 'operation', title: 'read_file', input: 'Path: /ws/a.txt', state: 'completed' });
    expect(responses.find((msg) => msg.type === 'assistant')?.content).toBe('alpha-token-A');
    expect(responses.find((msg) => msg.type === 'result')!.usage).toEqual(
      { input_tokens: 11972, cache_read_input_tokens: 7761, output_tokens: 40, thinking_tokens: 0 });
  });

  it('refuses to start where the agy launcher is missing', async () => {
    const runtime = createAgyConversationRuntime({ agyBin: join(dir, 'missing'), logDirectory: join(dir, 'logs') });
    await expect(runtime.spawn({ tenantId: 't', sessionId: 's', workspace: '/ws' } as never)).rejects.toThrow('agy launcher not found');
  });
});

describe('agy helpers', () => {
  it('maps agy usage fields and refuses partial counters', () => {
    expect(agyUsageCounts({ input_tokens: 1, output_tokens: 2, thinking_tokens: 1, cache_read_tokens: 3 }))
      .toEqual({ input_tokens: 1, cache_read_input_tokens: 3, output_tokens: 2, thinking_tokens: 1 });
    expect(agyUsageCounts({ input_tokens: 1, output_tokens: 2 })).toBeUndefined();
  });

  it('differences cumulative totals and refuses a total below its baseline', () => {
    expect(agyTurnUsageFromTotals({ input_tokens: 19810, output_tokens: 39 }, { input_tokens: 9766, output_tokens: 1 }))
      .toEqual({ input_tokens: 10044, output_tokens: 38 });
    expect(agyTurnUsageFromTotals({ input_tokens: 5 }, { input_tokens: 9 })).toBeUndefined();
    expect(agyTurnUsageFromTotals({ input_tokens: 5 }, undefined)).toBeUndefined();
  });

  it('recognizes egress faults only on Google service or proxy lines', () => {
    expect(agyEgressFailure(E03_LOG)).toBe('proxyconnect (www.googleapis.com)');
    expect(agyEgressFailure('I1120 something else: connection refused to 127.0.0.1:5432')).toBeUndefined();
  });

  it('maps agy tool steps to public operations', () => {
    expect(publicWorkEvents('agy', step(CONV, { step_index: 7, state: 'ACTIVE', step_type: 'tool', tool_name: 'run_command',
      tool_info: { name: 'run_command', parameters: { CommandLine: 'ls' } } }))).toEqual([expect.objectContaining({
      id: `agy:${CONV}:7`, kind: 'operation', state: 'running', title: 'run_command', input: 'CommandLine: ls' })]);
  });

  it('keeps agy out of automatic runtime fallback', () => {
    expect(resolveConversationRuntimePreference({ runtime: 'agy' })).toEqual(['agy']);
    expect(() => resolveConversationRuntimePreference({ conversationRuntimePreference: ['agy', 'codex'] }))
      .toThrow('agy runtime is opt-in only');
  });
});

describe('retired Gemini CLI provider cleanup', () => {
  it('leaves no residue of the retired Gemini CLI provider id in Relay source or tests', () => {
    const retired = ['google', 'gemini', 'cli'].join('-');
    const root = join(dirname(fileURLToPath(import.meta.url)), '..');
    const hits: string[] = [];
    for (const sub of ['src', 'tests']) {
      for (const name of readdirSync(join(root, sub), { recursive: true }).map(String)) {
        const path = join(root, sub, name);
        if (!statSync(path).isFile() || !/\.(ts|js|json|mjs)$/.test(name)) continue;
        if (readFileSync(path, 'utf8').includes(retired)) hits.push(`${sub}/${name}`);
      }
    }
    expect(hits).toEqual([]);
  });
});
