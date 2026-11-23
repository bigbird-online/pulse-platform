import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { EventEmitter } from 'node:events';
import { PassThrough } from 'node:stream';
import { mkdirSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { dirname, join } from 'node:path';
import { createPool, getSessionJsonlPath } from '../src/pool.js';
import { createConversationRuntimeManager, createPoolConversationAdapter, type ConversationRuntimeAdapter } from '../src/conversation-runtime.js';
import type { ConversationTurnContext, StreamJsonMessage } from '../src/types.js';
import { CC_SESSION_WI_TOOL } from '../src/cc-session-wi-mcp.js';
import { compactionSessionRef } from '../src/context-rollover-hint.js';

// The rollover hint through the real CC print-mode adapter: what CC writes to
// stdout, what Relay forwards to the runtime manager, and what reaches stdin.
type MockProcess = EventEmitter & { stdin: PassThrough; stdout: PassThrough; stderr: PassThrough; pid: number;
  stdio: PassThrough[]; kill: ReturnType<typeof vi.fn>; killed: boolean; written: string[] };
let mockSpawn: ReturnType<typeof vi.fn>;
vi.mock('node:child_process', () => ({ spawn: (...args: unknown[]) => mockSpawn(...args) }));

function createMockProcess(): MockProcess {
  const proc = new EventEmitter() as MockProcess;
  proc.stdin = new PassThrough(); proc.stdout = new PassThrough(); proc.stderr = new PassThrough();
  // The owned-process launcher adds a lifetime and a startup pipe.
  proc.stdio = [proc.stdin, proc.stdout, proc.stderr, new PassThrough(), new PassThrough()];
  proc.pid = 4321; proc.killed = false; proc.written = [];
  proc.stdin.on('data', chunk => proc.written.push(String(chunk)));
  proc.kill = vi.fn(() => { proc.killed = true; proc.emit('exit', 0, 'SIGTERM'); return true; });
  return proc;
}
const tick = () => new Promise(resolve => setTimeout(resolve, 30));
const tenant = 'owner:pulse';
const line = (record: unknown) => `${JSON.stringify(record)}\n`;
const compact = line({ type: 'system', subtype: 'compact_boundary', compact_metadata: { trigger: 'auto' } });
const result = (session: string) => line({ type: 'result', subtype: 'success', session_id: session,
  usage: { input_tokens: 1, output_tokens: 1 } });
// CC print mode writes a tool call as its own tool-only assistant record.
const declareCall = (id: string, wi: string) => line({ type: 'assistant', message: { id: `msg-${id}`, stop_reason: 'tool_use',
  content: [{ type: 'tool_use', id, name: CC_SESSION_WI_TOOL, input: { wi } }] } });
const declareResult = (id: string, text: Record<string, unknown>, isError = false) => line({ type: 'user',
  message: { role: 'user', content: [{ type: 'tool_result', tool_use_id: id, is_error: isError,
    content: [{ type: 'text', text: JSON.stringify(text) }] }] } });
const SESSION = '5f4add7e-4eff-4878-9c67-5c09e94f17f2';

describe('context rollover hint on the CC print-mode adapter', () => {
  let dir: string;
  let path: string;
  beforeEach(() => {
    mockSpawn = vi.fn();
    dir = mkdtempSync(join(tmpdir(), 'relay-rollover-cc-'));
    path = join(dir, 'context', 'context-compactions-v1.json');
    vi.stubEnv('PULSE_CONTEXT_COMPACTIONS_PATH', path);
    vi.stubEnv('PULSE_CC_QUOTA_PATH', join(dir, 'quota', 'cc-quota-v1.json'));
  });
  afterEach(() => { vi.unstubAllEnvs(); rmSync(dir, { recursive: true, force: true }); });

  function start(proc: MockProcess, ...later: MockProcess[]) {
    mockSpawn.mockReturnValue(proc);
    for (const next of [proc, ...later]) mockSpawn.mockReturnValueOnce(next);
    const pool = createPool({ cleanupIntervalMs: 999999, ccMode: 'print' });
    const manager = createConversationRuntimeManager();
    manager.register(createPoolConversationAdapter(pool));
    return { pool, manager };
  }
  const hint = 'Host 上下文提醒';
  const init = (session: string) => line({ type: 'system', subtype: 'init', session_id: session });
  /** A session with three compactions, whose turn A went out with the hint and is still running. */
  async function compactedTurnInFlight(first: MockProcess, second: MockProcess) {
    const { pool, manager } = start(first, second);
    const spawned = manager.spawn({ tenantId: tenant, runtime: 'cc', workspace: dir, userHome: dir, sessionId: SESSION,
      expectedLastActivity: Date.now() });
    first.stdout.write(init(SESSION));
    await spawned;
    for (let i = 0; i < 3; i++) first.stdout.write(compact);
    await tick();
    void manager.send(tenant, { type: 'human', content: 'turn A' });
    await tick();
    expect(first.written.join('')).toContain('本会话已压缩 3 次');
    return { pool, manager };
  }
  /** The transcript now ends days before the session's last activity: a restart opens a fresh session. */
  function staleTranscript() {
    const jsonl = getSessionJsonlPath(dir, SESSION, dir);
    mkdirSync(dirname(jsonl), { recursive: true });
    writeFileSync(jsonl, line({ type: 'user', uuid: 'u1', timestamp: '2026-01-01T00:00:00.000Z', message: { role: 'user', content: 'hi' } }));
  }
  const restartedSession = () => {
    const args = mockSpawn.mock.calls[1][1] as string[];
    return args.includes('--session-id') ? args[args.indexOf('--session-id') + 1] : args[args.indexOf('--resume') + 1];
  };
  const rows = () => JSON.parse(readFileSync(path, 'utf8')).sessions as Array<{ session_ref: string; wi: string | null; compactions: number }>;

  it('records the WI a successful declaration names, though print mode drops the tool-only call', async () => {
    const proc = createMockProcess();
    const { pool, manager } = start(proc);
    const spawned = manager.spawn({ tenantId: tenant, runtime: 'cc', workspace: dir, sessionId: SESSION,
      meteringWorkItemId: 'wi_20261101_001' });
    proc.stdout.write(line({ type: 'system', subtype: 'init', session_id: SESSION }));
    await spawned;
    const forwarded: string[] = [];
    manager.on('response', (_tenant, msg) => forwarded.push(msg.type));
    void manager.send(tenant, { type: 'human', content: '修 #1734' });
    await tick();
    proc.stdout.write(declareCall('toolu_ok', 'wi_20261118_988'));
    proc.stdout.write(declareResult('toolu_ok', { origin: 'agent_declaration', schema: 'cc_request_wi/v1', wi: 'wi_20261118_988' }));
    // A rejected call to another WI must not replace it.
    proc.stdout.write(declareCall('toolu_bad', 'wi_20261102_002'));
    proc.stdout.write(declareResult('toolu_bad', { status: 'unavailable', reason: 'host_request_identity_conflict' }, true));
    proc.stdout.write(compact);
    proc.stdout.write(result(SESSION));
    await tick();

    expect(forwarded).not.toContain('assistant');
    expect(rows()).toEqual([expect.objectContaining({ session_ref: compactionSessionRef(SESSION), wi: 'wi_20261118_988', compactions: 1 })]);
    pool.dispose();
  });

  it('counts under the session CC actually opened when the requested one was stale', async () => {
    const stale = 'aaaaaaaa-1111-2222-3333-444444444444';
    // The requested session already has three compactions on record.
    const earlier = new Date(Date.now() - 86400_000).toISOString();
    mkdirSync(dirname(path), { recursive: true });
    writeFileSync(path, JSON.stringify({ schema: 'pulse.stats.context_compactions/v1', source: 'relay_cc_compact_boundary',
      updated_at: earlier, sessions: [{ session_ref: compactionSessionRef(stale), runtime: 'cc', wi: null, compactions: 3,
        by_wi: [{ wi: null, compactions: 3 }], context_ratio: null, first_compaction_at: earlier, last_compaction_at: earlier }] }));
    // Its transcript ends days before the session's last activity: the pool opens a fresh session.
    const jsonl = getSessionJsonlPath(dir, stale, dir);
    mkdirSync(dirname(jsonl), { recursive: true });
    writeFileSync(jsonl, line({ type: 'user', uuid: 'u1', timestamp: '2026-01-01T00:00:00.000Z', message: { role: 'user', content: 'hi' } }));

    const proc = createMockProcess();
    const { pool, manager } = start(proc);
    const spawned = manager.spawn({ tenantId: tenant, runtime: 'cc', workspace: dir, userHome: dir, sessionId: stale,
      resume: true, expectedLastActivity: Date.now() });
    await tick();
    const args = mockSpawn.mock.calls[0][1] as string[];
    const fresh = args[args.indexOf('--session-id') + 1];
    expect(fresh).toBeTruthy();
    expect(fresh).not.toBe(stale);
    proc.stdout.write(init(fresh));
    await spawned;

    void manager.send(tenant, { type: 'human', content: 'go on' });
    await tick();
    expect(proc.written.join('')).not.toContain('Host 上下文提醒');
    proc.stdout.write(compact);
    proc.stdout.write(result(fresh));
    await tick();

    const byRef = new Map(rows().map(row => [row.session_ref, row.compactions]));
    expect(byRef.get(compactionSessionRef(stale))).toBe(3);
    expect(byRef.get(compactionSessionRef(fresh))).toBe(1);
    pool.dispose();
  });

  it('decides the hint as a queued turn reaches CC, after the compaction ahead of it', async () => {
    const proc = createMockProcess();
    const { pool, manager } = start(proc);
    const spawned = manager.spawn({ tenantId: tenant, runtime: 'cc', workspace: dir, sessionId: SESSION });
    proc.stdout.write(line({ type: 'system', subtype: 'init', session_id: SESSION }));
    await spawned;
    proc.stdout.write(compact);
    proc.stdout.write(compact);
    await tick();

    void manager.send(tenant, { type: 'human', content: 'turn A' });
    await tick();
    // B arrives while A runs and waits in the pool's queue.
    const sentB = manager.send(tenant, { type: 'human', content: 'turn B' });
    await tick();
    expect(proc.written.join('')).not.toContain('turn B');
    proc.stdout.write(compact);
    proc.stdout.write(result(SESSION));
    await sentB;
    await tick();

    const inputs = proc.written.join('').split('\n').filter(Boolean).map(raw => JSON.parse(raw).message?.content as unknown);
    const text = (content: unknown) => typeof content === 'string' ? content : JSON.stringify(content);
    const a = inputs.find(content => text(content).includes('turn A'));
    const b = inputs.find(content => text(content).includes('turn B'));
    expect(text(a)).not.toContain('Host 上下文提醒');
    expect(text(b)).toContain('本会话已压缩 3 次');
    pool.dispose();
  });

  it('replays a turn into a fresh session the pool opened without the old session\'s hint', async () => {
    const first = createMockProcess();
    const second = createMockProcess();
    const { pool } = await compactedTurnInFlight(first, second);
    staleTranscript();
    // CC dies mid-turn; the pool restarts it on a fresh UUID and replays turn A before any init.
    first.emit('exit', 1, null);
    for (let i = 0; i < 4; i++) await tick();
    const fresh = restartedSession();
    expect(fresh).not.toBe(SESSION);
    const written = second.written.join('');
    expect(written).toContain('turn A');
    expect(written).not.toContain(hint);
    pool.dispose();
  });

  it('a queued turn the pool drains into its fresh session carries no hint of the old one', async () => {
    const first = createMockProcess();
    const second = createMockProcess();
    const { pool, manager } = await compactedTurnInFlight(first, second);
    const sentB = manager.send(tenant, { type: 'human', content: 'turn B' });
    await tick();
    expect(first.written.join('')).not.toContain('turn B');
    staleTranscript();
    first.emit('exit', 1, null);
    for (let i = 0; i < 4; i++) await tick();
    await sentB;
    expect(restartedSession()).not.toBe(SESSION);
    const written = second.written.join('');
    expect(written).toContain('turn B');
    expect(written).not.toContain(hint);
    pool.dispose();
  });

  it('a replay into the same session keeps the one hint it was written with', async () => {
    const first = createMockProcess();
    const second = createMockProcess();
    const { pool } = await compactedTurnInFlight(first, second);
    first.emit('exit', 1, null);
    for (let i = 0; i < 4; i++) await tick();
    expect(restartedSession()).toBe(SESSION);
    const written = second.written.join('');
    expect(written).toContain('turn A');
    expect(written.split(hint)).toHaveLength(2);
    pool.dispose();
  });
});

describe('context rollover hint outside CC print mode', () => {
  it('an interactive CC process receives the turn without a hint', async () => {
    const dir = mkdtempSync(join(tmpdir(), 'relay-rollover-interactive-'));
    vi.stubEnv('PULSE_CONTEXT_COMPACTIONS_PATH', join(dir, 'context', 'context-compactions-v1.json'));
    const events = new EventEmitter();
    const written: StreamJsonMessage[] = [];
    let process: { tenantId: string; runtime: 'cc'; sessionId: string; state: 'ready'; lastActivity: number; spawnedAt: number;
      restartCount: number; restartWindowStart: number } | undefined;
    const adapter: ConversationRuntimeAdapter = {
      id: 'cc', preparesInput: true,
      spawn: vi.fn(async config => (process = { tenantId: config.tenantId, runtime: 'cc', sessionId: SESSION, state: 'ready',
        lastActivity: 1, spawnedAt: 1, restartCount: 0, restartWindowStart: 1 })),
      send: vi.fn(async (_tenant: string, message: StreamJsonMessage, turn?: ConversationTurnContext) => {
        written.push(turn?.prepareInput ? turn.prepareInput(message, { sessionId: SESSION, mode: 'interactive' }) : message);
      }),
      kill: vi.fn(async () => {}), killAll: vi.fn(async () => {}),
      get: vi.fn(() => process), getAll: vi.fn(() => (process ? [process] : [])), getStatus: vi.fn(() => []),
      on: vi.fn((event: string, handler: (...args: unknown[]) => void) => { events.on(event, handler); }) as ConversationRuntimeAdapter['on'],
      emitShutdown: vi.fn(), dispose: vi.fn(),
    };
    const manager = createConversationRuntimeManager();
    manager.register(adapter);
    await manager.spawn({ tenantId: tenant, runtime: 'cc', workspace: dir, sessionId: SESSION });
    for (let i = 0; i < 3; i++) events.emit('response', tenant, { type: 'system', subtype: 'compact_boundary', session_id: SESSION });
    await manager.send(tenant, { type: 'human', content: 'go on' });
    expect(written.map(message => message.content)).toEqual(['go on']);
    vi.unstubAllEnvs();
    rmSync(dir, { recursive: true, force: true });
  });
});
