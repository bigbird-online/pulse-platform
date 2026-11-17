import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { EventEmitter } from 'node:events';
import { PassThrough } from 'node:stream';
import { mkdirSync, mkdtempSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { GUARD_MATCHER, SESSION_WI_REMINDER } from '../src/session-wi-guard.js';
import { createPool } from '../src/pool.js';
import { createCodexConversationRuntime } from '../src/codex-conversation-runtime.js';

const { spawnMock } = vi.hoisted(() => ({ spawnMock: vi.fn() }));
vi.mock('node:child_process', async (original) => ({
  ...await original<typeof import('node:child_process')>(),
  spawn: spawnMock,
}));
vi.mock('../src/owned-process.js', async () => ({
  spawnOwnedProcess: (await import('node:child_process')).spawn,
}));

function child() {
  const value = Object.assign(new EventEmitter(), {
    stdin: new PassThrough(), stdout: new PassThrough(), stderr: new PassThrough(),
    pid: 1234567, killed: false,
    kill: vi.fn((signal?: string) => { value.killed = true; value.emit('exit', 0, signal); return true; }),
  });
  return value;
}
const tick = () => new Promise(resolveTick => setTimeout(resolveTick, 10));

describe('CC conversation with sessionWiGuard', () => {
  let home: string;
  let pool: ReturnType<typeof createPool> | undefined;
  beforeEach(() => {
    home = mkdtempSync(join(tmpdir(), 'cc-wi-guard-'));
    vi.stubEnv('HOME', home);
    vi.stubEnv('PULSE_COMPUTECOST_RECEIPT_DIR', '');
    spawnMock.mockReset();
    spawnMock.mockImplementation(() => child());
  });
  afterEach(async () => {
    await pool?.killAll();
    pool?.dispose();
    vi.unstubAllEnvs();
    rmSync(home, { recursive: true, force: true });
  });

  async function start(sessionWiGuard: boolean) {
    pool = createPool({ cleanupIntervalMs: 999999, ccMode: 'print' });
    const pending = pool.spawn({ tenantId: 'fixture', sessionId: 'cc-session', workspace: home, sessionWiGuard });
    const proc = spawnMock.mock.results.at(-1)!.value as ReturnType<typeof child>;
    proc.stdout.write('{"type":"system","subtype":"init","content":"ready"}\n');
    await pending;
    const sent: string[] = [];
    proc.stdin.on('data', (chunk: Buffer) => sent.push(...chunk.toString().split('\n').filter(Boolean)));
    return { proc, args: spawnMock.mock.calls.at(-1)![1] as string[], sent };
  }
  const contentOf = (line: string) => JSON.parse(line).message.content as string;

  it('reminds every new request and registers the write guard', async () => {
    const { proc, args, sent } = await start(true);
    expect(JSON.parse(args[args.lastIndexOf('--settings') + 1]).hooks.PreToolUse[0].matcher).toBe(GUARD_MATCHER);
    await pool!.send('fixture', { type: 'human', content: '第一件事' });
    await tick();
    expect(contentOf(sent.at(-1)!)).toBe(`第一件事\n\n${SESSION_WI_REMINDER}`);
    proc.stdout.write('{"type":"result","subtype":"success","result":"ok","session_id":"cc-session"}\n');
    await tick();
    await pool!.send('fixture', { type: 'human', content: '第二件事' });
    await tick();
    expect(contentOf(sent.at(-1)!)).toBe(`第二件事\n\n${SESSION_WI_REMINDER}`);
  });

  it('leaves agents without the setting unchanged', async () => {
    const { args, sent } = await start(false);
    expect(args.join(' ')).not.toContain('cc-session-wi-guard');
    await pool!.send('fixture', { type: 'human', content: '第一件事' });
    await tick();
    expect(contentOf(sent.at(-1)!)).toBe('第一件事');
  });
});

describe('Codex conversation with sessionWiGuard', () => {
  it('reminds until the thread rollout holds a completed declaration', async () => {
    const root = mkdtempSync(join(tmpdir(), 'codex-wi-guard-'));
    const runtime = createCodexConversationRuntime({ nativeSessionsDirectory: root });
    try {
      const prompts: string[] = [];
      const turn = async (content: string) => {
        const proc = child();
        proc.stdin.on('data', (chunk: Buffer) => prompts.push(chunk.toString()));
        spawnMock.mockImplementationOnce(() => proc);
        await runtime.send('fixture', { type: 'human', content });
        await tick();
        proc.stdout.write(JSON.stringify({ type: 'thread.started', thread_id: 'native-thread' }) + '\n');
        proc.emit('exit', 0);
        await tick();
      };
      await runtime.spawn({ tenantId: 'fixture', workspace: root, sessionId: 'logical', sessionWiGuard: true } as never);
      await turn('第一件事');
      expect(prompts.at(-1)).toBe(`第一件事\n\n${SESSION_WI_REMINDER}`);
      mkdirSync(root, { recursive: true });
      writeFileSync(join(root, 'native-thread.jsonl'), '{"type":"session_meta","payload":{"id":"native-thread"}}\n');
      await turn('第二件事');
      expect(prompts.at(-1)).toBe(`第二件事\n\n${SESSION_WI_REMINDER}`);
      const completed = { type: 'event_msg', payload: { type: 'item_completed', item: { type: 'McpToolCall',
        server: 'pulse-request-purpose', tool: 'declare_session_wi', status: 'completed', result: { isError: false } } } };
      writeFileSync(join(root, 'native-thread.jsonl'),
        '{"type":"session_meta","payload":{"id":"native-thread"}}\n' + JSON.stringify(completed) + '\n');
      await turn('第三件事');
      expect(prompts.at(-1)).toBe('第三件事');
    } finally { runtime.dispose(); rmSync(root, { recursive: true, force: true }); }
  });
});
