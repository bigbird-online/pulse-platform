import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { EventEmitter } from 'node:events';
import { PassThrough } from 'node:stream';
import { mkdtempSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join, resolve } from 'node:path';
import {
  CC_SESSION_WI_SERVER, CC_SESSION_WI_TOOL, resolveCcSessionWiMcp, type CcSessionWiMcp,
} from '../src/cc-session-wi-mcp.js';
import { createPool } from '../src/pool.js';
import { canonicalPython } from './helpers/canonical-python.js';

const { spawnMock } = vi.hoisted(() => ({ spawnMock: vi.fn() }));
vi.mock('node:child_process', async (original) => ({
  ...await original<typeof import('node:child_process')>(),
  spawn: spawnMock,
}));

const RULE = '使用 declare_session_wi：登记当前原生请求的归属。要改文件、提 PR 或动生产时声明所属 WI；闲聊、只读查询、解释说明声明 none，不要为此新建 WI。每个新的人类请求或独立任务各自声明；复用会话不继承前一请求的归属。当前请求的子任务保留原始请求归属；子代理不需要声明，继承原请求的 WI 或其首行 [WI: 子WI] 指定的子 WI。一个会话只为一个 WI 谱系（一个 WI 及其子 WI）写代码、提 PR：声明过谱系外 WI 的会话，其提交登记会被拒；谱系外 WI 的写工作请人类 /new 后在新会话声明。本请求已声明 none 后不再改声明，写操作留到下一条请求。子 WI 的提交在人类消息触发的回合里由带标记的子代理做。';
const PYTHON = canonicalPython;

describe('session WI MCP resolution', () => {
  it('reads the rule and tool from the actual CC-mode server', () => {
    const resolved = resolveCcSessionWiMcp({ python: PYTHON, pythonPath: resolve('../ComputeCost') });
    expect(resolved?.instructions).toBe(RULE);
    expect(resolved?.server).toEqual({ type: 'stdio', command: PYTHON,
      args: [resolve('../ComputeCost/runtime/codex-mcp-request-purpose.py'), '--host', 'cc'] });
  });

  it('skips with a warning when host resources are absent or the contract differs', () => {
    const warn = vi.spyOn(console, 'warn').mockImplementation(() => {});
    expect(resolveCcSessionWiMcp(undefined)).toBeUndefined();
    expect(resolveCcSessionWiMcp({ python: PYTHON, pythonPath: '/absent' })).toBeUndefined();
    const wrong = () => ({ status: 0, stdout: '{"id":1,"result":{"instructions":"x"}}\n{"id":2,"result":{"tools":[]}}\n' });
    expect(resolveCcSessionWiMcp({ python: PYTHON, pythonPath: resolve('../ComputeCost') }, wrong)).toBeUndefined();
    expect(warn).toHaveBeenCalledTimes(2);
    warn.mockRestore();
  });
});

function child() {
  const value = Object.assign(new EventEmitter(), {
    stdin: new PassThrough(), stdout: new PassThrough(), stderr: new PassThrough(),
    pid: 1234567, killed: false,
    kill: vi.fn((signal?: string) => { value.killed = true; value.emit('exit', 0, signal); return true; }),
  });
  return value;
}

describe('CC spawn registers the session WI MCP', () => {
  const sessionWiMcp: CcSessionWiMcp = {
    server: { type: 'stdio', command: '/reviewed/python', args: ['/release/ComputeCost/runtime/codex-mcp-request-purpose.py', '--host', 'cc'] },
    instructions: RULE,
  };
  let home: string;
  let pool: ReturnType<typeof createPool> | undefined;
  beforeEach(() => {
    home = mkdtempSync(join(tmpdir(), 'cc-session-wi-'));
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

  async function spawnArgs(mode: 'print' | 'interactive', mcp?: CcSessionWiMcp): Promise<string[]> {
    pool = createPool({ cleanupIntervalMs: 999999, ccMode: mode, sessionWiMcp: mcp });
    const pending = pool.spawn({ tenantId: 'fixture', sessionId: 'cc-session', workspace: home, ccMode: mode });
    spawnMock.mock.results.at(-1)!.value.stdout.write('{"type":"system","subtype":"init","content":"ready"}\n');
    await pending;
    return spawnMock.mock.calls.at(-1)![1] as string[];
  }

  it.each(['print', 'interactive'] as const)('adds server, allowed tool and resident rule for %s CC', async (mode) => {
    const args = await spawnArgs(mode, sessionWiMcp);
    const config = JSON.parse(args[args.indexOf('--mcp-config') + 1]);
    expect(Object.keys(config.mcpServers)).toEqual([CC_SESSION_WI_SERVER]);
    expect(config.mcpServers[CC_SESSION_WI_SERVER]).toEqual(sessionWiMcp.server);
    expect(args[args.indexOf('--allowed-tools') + 1].split(',')).toContain(CC_SESSION_WI_TOOL);
    expect(args[args.indexOf('--append-system-prompt') + 1].endsWith('\n\n' + RULE)).toBe(true);
  });

  it('leaves spawn unchanged when the MCP is unavailable', async () => {
    const args = await spawnArgs('print');
    expect(args).not.toContain('--mcp-config');
    expect(args[args.indexOf('--allowed-tools') + 1]).not.toContain(CC_SESSION_WI_TOOL);
    expect(args[args.indexOf('--append-system-prompt') + 1]).not.toContain('declare_session_wi');
  });
});

// Pool behavior is isolated from OS process ownership, tested with real processes separately.
vi.mock('../src/owned-process.js', async () => ({
  spawnOwnedProcess: (await import('node:child_process')).spawn,
}));
