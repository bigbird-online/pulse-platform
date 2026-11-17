import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { EventEmitter } from 'node:events';
import { PassThrough } from 'node:stream';
import { mkdtempSync, readFileSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { createPool } from '../src/pool.js';
import { createConversationRuntimeManager, createPoolConversationAdapter } from '../src/conversation-runtime.js';
import { TurnActivityCounter } from '../src/channel-rendering.js';
import { createTelegramRenderingSink } from '../src/channels/telegram.js';
import { toolTurnEffect, type WorkProcessEvent } from '../src/work-process-events.js';
import type { ConversationTurnEffect, StreamJsonMessage } from '../src/types.js';

type MockProcess = EventEmitter & { stdin: PassThrough; stdout: PassThrough; stderr: PassThrough; pid: number; kill: ReturnType<typeof vi.fn>; killed: boolean };
let mockSpawn: ReturnType<typeof vi.fn>;
vi.mock('node:child_process', () => ({ spawn: (...args: unknown[]) => mockSpawn(...args) }));

function createMockProcess(): MockProcess {
  const proc = new EventEmitter() as MockProcess;
  proc.stdin = new PassThrough(); proc.stdout = new PassThrough(); proc.stderr = new PassThrough();
  proc.pid = 4321; proc.killed = false;
  proc.kill = vi.fn(() => { proc.killed = true; proc.emit('exit', 0, 'SIGTERM'); return true; });
  return proc;
}
const tick = () => new Promise(resolve => setTimeout(resolve, 30));
const tenant = 'owner:pulse';
const line = (record: unknown) => `${JSON.stringify(record)}\n`;
// Claude Code print-mode stream-json：每个 content block 单独成一条 assistant 记录。
const toolUse = (id: string, name: string, input: Record<string, unknown>) => line({ type: 'assistant',
  message: { id: `msg-${id}`, model: 'claude-opus-4-5', stop_reason: 'tool_use', content: [{ type: 'tool_use', id, name, input }] } });
const toolResult = (id: string, content: string) => line({ type: 'user',
  message: { role: 'user', content: [{ type: 'tool_result', tool_use_id: id, content }] } });

describe('CC print-mode work process', () => {
  let tempDir: string;
  beforeEach(() => {
    mockSpawn = vi.fn();
    tempDir = mkdtempSync(join(tmpdir(), 'relay-cc-work-process-'));
    vi.stubEnv('RELAY_WORK_PROCESS_DETAILS', 'on');
    vi.stubEnv('PULSE_CC_QUOTA_PATH', join(tempDir, 'quota', 'cc-quota-v1.json'));
  });
  afterEach(() => { vi.unstubAllEnvs(); rmSync(tempDir, { recursive: true, force: true }); });

  it('counts each CC tool call once and publishes titled operations without commentary', async () => {
    const proc = createMockProcess();
    mockSpawn.mockReturnValue(proc);
    const pool = createPool({ cleanupIntervalMs: 999999, ccMode: 'print' });
    const manager = createConversationRuntimeManager();
    manager.register(createPoolConversationAdapter(pool));
    const effects: ConversationTurnEffect[] = [];
    const responses: StreamJsonMessage[] = [];
    manager.on('turn-effect', (_tenant, effect) => effects.push(effect));
    manager.on('response', (_tenant, msg) => responses.push(msg));

    const spawned = manager.spawn({ tenantId: tenant, runtime: 'cc', workspace: tempDir, sessionId: 'cc-session' });
    proc.stdout.write(line({ type: 'system', subtype: 'init', session_id: 'cc-session' }));
    await spawned;
    await manager.send(tenant, { type: 'human', content: '检查仓库' });

    proc.stdout.write(toolUse('toolu_read', 'Read', { file_path: '/repo/README.md' }));
    proc.stdout.write(toolResult('toolu_read', 'readme body'));
    // 同一 tool_use 重放不得重复计数。
    proc.stdout.write(toolUse('toolu_read', 'Read', { file_path: '/repo/README.md' }));
    proc.stdout.write(line({ type: 'assistant', message: { id: 'msg-note', stop_reason: 'tool_use',
      content: [{ type: 'text', text: '我先跑一下测试。' }] } }));
    proc.stdout.write(toolUse('toolu_bash', 'Bash', { command: 'npm test' }));
    proc.stdout.write(toolResult('toolu_bash', 'all green'));
    proc.stdout.write(line({ type: 'assistant', message: { id: 'msg-final', stop_reason: 'end_turn',
      content: [{ type: 'text', text: '测试全部通过。' }] } }));
    proc.stdout.write(line({ type: 'result', subtype: 'success', session_id: 'cc-session',
      usage: { input_tokens: 1, output_tokens: 2 }, modelUsage: { 'claude-opus-4-5': { inputTokens: 1, outputTokens: 2 } } }));
    await tick();

    expect(effects).toEqual(['read-only-effect', 'side-effect-observed']);
    const operations = responses.filter(msg => msg.type === 'work_process').map(msg => msg.process_event as WorkProcessEvent);
    expect(operations).toEqual(expect.arrayContaining([
      expect.objectContaining({ id: 'cc:toolu_read', state: 'running', title: 'Read', input: 'file_path: /repo/README.md' }),
      expect.objectContaining({ id: 'cc:toolu_read', state: 'completed', output: 'readme body' }),
      expect.objectContaining({ id: 'cc:toolu_bash', state: 'running', title: 'Bash', input: 'command: npm test' }),
      expect.objectContaining({ id: 'cc:toolu_bash', state: 'completed', output: 'all green' }),
    ]));
    // tool-only 记录不走文本路径。
    expect(responses.filter(msg => msg.type === 'assistant').every(msg =>
      (msg.message as { content: Array<{ type: string }> }).content.some(block => block.type === 'text'))).toBe(true);
    expect(responses.find(msg => msg.type === 'result')?.runtime).toBe('cc');

    // 按 Telegram 渠道的接线把事件喂给真实 sink：折叠卡只显示计数。
    const api = { sendMessage: vi.fn(async () => ({ message_id: 2 })), editMessageText: vi.fn(async () => ({})),
      deleteMessage: vi.fn(async () => true),
      raw: { sendRichMessage: vi.fn(async () => ({ message_id: 1 })), editMessageText: vi.fn(async () => ({})) } };
    const sink = createTelegramRenderingSink({ api } as any, { processThrottleMs: 0 });
    const counter = new TurnActivityCounter();
    sink.begin(tenant); counter.begin(tenant);
    for (const _effect of effects) {
      const progress = counter.advance(tenant)!;
      await sink.activity(tenant, 42, progress.text!, `activity:${progress.step}`);
    }
    for (const msg of responses) {
      if (msg.type === 'work_process') await sink.process?.(tenant, 42, msg.process_event as WorkProcessEvent);
      if (msg.type === 'assistant') {
        const text = (msg.message as { content: Array<{ text?: string }> }).content.map(block => block.text ?? '').join('');
        const stopReason = (msg.message as { stop_reason?: string }).stop_reason;
        if (stopReason !== 'end_turn') await sink.assistant(tenant, 42, text, 'commentary');
      }
    }
    const rendered = JSON.stringify([...api.raw.sendRichMessage.mock.calls, ...api.raw.editMessageText.mock.calls].at(-1));
    expect(rendered).toContain('已推进 2 个执行步骤');
    expect(rendered).toContain('⌄ 展开过程');
    expect(rendered).not.toContain('我先跑一下测试');
    expect(rendered).not.toContain('readme body');
    pool.dispose();
  });

  it('persists the subscription quota from a rate_limit_event without failing the turn', async () => {
    const proc = createMockProcess();
    mockSpawn.mockReturnValue(proc);
    const pool = createPool({ cleanupIntervalMs: 999999, rateLimitAbortTimeoutMs: 1, ccMode: 'print' });
    const spawned = pool.spawn({ tenantId: tenant, workspace: tempDir, sessionId: 'cc-session' });
    proc.stdout.write(line({ type: 'system', subtype: 'init', session_id: 'cc-session' }));
    await spawned;
    await pool.send(tenant, { type: 'human', content: 'hello' });
    proc.stdout.write(line({ type: 'rate_limit_event', session_id: 'cc-session', uuid: 'u-1', rate_limit_info: {
      status: 'allowed', resetsAt: 1790826600, rateLimitType: 'five_hour',
      unifiedWindows: { five_hour: { utilization: 0.46, resetsAt: 1790826600 }, seven_day: { utilization: 0.04, resetsAt: 1790906400 } } } }));
    await tick();
    expect(proc.kill).not.toHaveBeenCalledWith('SIGKILL');
    const document = JSON.parse(readFileSync(join(tempDir, 'quota', 'cc-quota-v1.json'), 'utf8'));
    expect(document.weekly).toEqual({ used_percent: 4, window_minutes: 10080, resets_at: 1790906400 });
    expect(document.five_hour).toEqual({ used_percent: 46, window_minutes: 300, resets_at: 1790826600 });
    expect(JSON.stringify(document)).not.toContain('cc-session');
    pool.dispose();
  });
});

describe('tool turn effect classification', () => {
  it('treats non-mutating CC tools as read-only and keeps mutating or unknown tools side-effecting', () => {
    for (const name of ['Read', 'Grep', 'Glob', 'TodoWrite', 'WebSearch', 'WebFetch', 'AskUserQuestion', 'ToolSearch',
      'Skill', 'NotebookRead', 'BashOutput', 'ListMcpResourcesTool', 'ReadMcpResourceTool']) {
      expect(toolTurnEffect(name)).toBe('read-only-effect');
    }
    for (const name of ['Bash', 'Write', 'Edit', 'MultiEdit', 'NotebookEdit', 'KillShell', 'Task', 'Agent', 'mcp__x__y', '']) {
      expect(toolTurnEffect(name)).toBe('side-effect-observed');
    }
  });
  it('keeps Pi classification unchanged', () => {
    for (const name of ['read', 'grep', 'find', 'ls']) expect(toolTurnEffect(name)).toBe('read-only-effect');
    for (const name of ['bash', 'edit', 'write', 'todowrite', 'webfetch']) expect(toolTurnEffect(name)).toBe('side-effect-observed');
  });
});

// Pool behavior is isolated from OS process ownership, tested with real processes separately.
vi.mock('../src/owned-process.js', async () => ({
  spawnOwnedProcess: (await import('node:child_process')).spawn,
}));
