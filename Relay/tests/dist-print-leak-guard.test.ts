// dist-level 真链路验证（2026-07-05）
// ---------------------------------------------------------------------------
// 背景：0047a48c 把 print-mode A+B 自愈接进 src/pool.ts，测试全绿——但 dist/pool.js
// 没跟着 build，生产跑的 dist 里根本没这段代码，Hestia/Duke 反复停摆。根因不是逻辑
// 错，是"提交≠部署"：既有测试全 import `../src/pool.js`（vitest 解析到 .ts 源码），
// 永远碰不到"编译后的 dist 是否与 src 一致"这个真正坏掉的面。
//
// 本文件唯一的区别、也是全部价值：它 import 的是 **`../dist/pool.js`**（编译产物、
// 生产 relay 真正加载的那份），用生产同一条 stdout→createStreamReader→print-mode
// 拦截路径喂真实泄漏 record，断言 3528 拦截在**编译后的代码里**确实生效。
//
// 这不是重复 src 的逻辑测试——src 测试证明"逻辑对"，本测试证明"部署的 dist 生效"。
// 若有人再改了 src 忘了 build，src 测试仍绿，但本测试会红（dist 落后于 src）。
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { EventEmitter } from 'node:events';
import { PassThrough } from 'node:stream';
// ↓↓↓ 关键：import dist（编译产物），不是 src。这是整个文件存在的理由。
import { createPool } from '../dist/pool.js';
import type { SpawnConfig, StreamJsonMessage } from '../src/types.js';

interface MockProcess extends EventEmitter {
  stdin: PassThrough;
  stdout: PassThrough;
  stderr: PassThrough;
  pid: number;
  kill: ReturnType<typeof vi.fn>;
  killed: boolean;
}

function createMockProcess(pid = 4321): MockProcess {
  const proc = new EventEmitter() as MockProcess;
  proc.stdin = new PassThrough();
  proc.stdout = new PassThrough();
  proc.stderr = new PassThrough();
  proc.pid = pid;
  proc.killed = false;
  proc.kill = vi.fn((signal?: string) => {
    proc.killed = true;
    proc.emit('exit', signal === 'SIGKILL' ? 137 : 0, signal ?? 'SIGTERM');
    return true;
  });
  return proc;
}

function captureStdin(proc: MockProcess): { text: () => string } {
  let buf = '';
  proc.stdin.on('data', (chunk: Buffer | string) => { buf += chunk.toString(); });
  return { text: () => buf };
}

let mockSpawn: ReturnType<typeof vi.fn>;
vi.mock('node:child_process', () => ({
  spawn: (...args: unknown[]) => mockSpawn(...args),
}));

const baseConfig: SpawnConfig = {
  tenantId: 'member-000',
  workspace: '/opt/PULSE/Tenants/member-000/',
  sessionId: 'dist-verify-session-uuid',
};

// end_turn + 结构完整 <invoke>（含 parameter/闭合）、无真 tool_use = 泄漏。
// 这是 yurukusa 在 issue #67945/#71776 指出的最凶形态：end_turn 路径被当成最终答案，
// textual <invoke> 永不被识别 → --print 进程 alive-but-idle 停摆。也是 Hestia 现场形态。
const LEAK_TEXT = '这里我要停一下做个归属判断。\n\ncall\n<invoke name="Bash">\n<parameter name="command">ls</parameter>\n</invoke>';
function leakLine(stopReason: string): string {
  return JSON.stringify({
    type: 'assistant',
    message: { model: 'claude-opus-4-8', stop_reason: stopReason, content: [{ type: 'text', text: LEAK_TEXT }] },
  }) + '\n';
}

describe('dist/pool.js print-mode tool-call leak guard (真链路：验证编译产物生效)', () => {
  let pool: ReturnType<typeof createPool>;

  beforeEach(() => {
    mockSpawn = vi.fn();
    pool = createPool({ cleanupIntervalMs: 999999, ccMode: 'print' });
  });

  afterEach(() => {
    pool?.dispose?.();
    delete process.env['RELAY_ENABLE_PRINT_LEAK_GUARD'];
    vi.restoreAllMocks();
  });

  // end_turn 形态（生产 Hestia 停摆的那条路径）：dist 里 3528 拦截必须生效。
  it('end_turn 泄漏 record 在 dist 编译码里被拦截（不 emit + nudge 重发）', async () => {
    const proc = createMockProcess();
    mockSpawn.mockReturnValue(proc);
    const stdin = captureStdin(proc);
    const responses: Array<{ msg: StreamJsonMessage }> = [];
    pool.on('response', (_t, msg) => responses.push({ msg }));

    const spawnPromise = pool.spawn(baseConfig);
    proc.stdout.write('{"type":"system","content":"ready"}\n');
    await spawnPromise;

    pool.send(baseConfig.tenantId, { type: 'user', content: [{ type: 'text', text: 'hi' }] }).catch(() => {});
    await new Promise((r) => setTimeout(r, 20));
    proc.stdout.write(leakLine('end_turn'));
    await new Promise((r) => setTimeout(r, 100));

    // 生产真正要防的：泄漏 <invoke> 文本不能 emit 到 channel（否则漏进飞书 + 沉淀成 few-shot 种子）。
    const leakedToChannel = responses.find(
      (r) => r.msg.type === 'assistant' && JSON.stringify(r.msg).includes('<invoke name='),
    );
    expect(leakedToChannel).toBeUndefined();
    // A：nudge 已写进 stdin（要求 CC 重发真工具调用）。
    expect(stdin.text()).toContain('把工具调用写成了文本');
    // A 不杀进程。
    expect(proc.killed).toBe(false);
  });

  // tool_use 形态（Hestia jsonl 实际 stop_reason=tool_use 的那条）：同样必须在 dist 里被拦。
  it('tool_use 泄漏 record 在 dist 编译码里被拦截（不 emit）', async () => {
    const proc = createMockProcess();
    mockSpawn.mockReturnValue(proc);
    const responses: Array<{ msg: StreamJsonMessage }> = [];
    pool.on('response', (_t, msg) => responses.push({ msg }));

    const spawnPromise = pool.spawn(baseConfig);
    proc.stdout.write('{"type":"system","content":"ready"}\n');
    await spawnPromise;

    pool.send(baseConfig.tenantId, { type: 'user', content: [{ type: 'text', text: 'go' }] }).catch(() => {});
    await new Promise((r) => setTimeout(r, 20));
    proc.stdout.write(leakLine('tool_use'));
    await new Promise((r) => setTimeout(r, 100));

    const leakedToChannel = responses.find(
      (r) => r.msg.type === 'assistant' && JSON.stringify(r.msg).includes('<invoke name='),
    );
    expect(leakedToChannel).toBeUndefined();
  });
});

// Pool behavior is isolated from OS process ownership, tested with real processes separately.
vi.mock('../dist/owned-process.js', async () => ({
  spawnOwnedProcess: (await import('node:child_process')).spawn,
}));
