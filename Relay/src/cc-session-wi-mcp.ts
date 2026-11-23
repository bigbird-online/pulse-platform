/**
 * ComputeCost session WI MCP for CC sessions.
 *
 * Relay registers the same `declare_session_wi` server Codex uses, started in
 * CC host mode. Its rule reaches the model once, as the server's MCP
 * instructions, which CC shows in every session; the probe only checks the
 * server answers with that rule and the one tool. The
 * collector reads the declaration from the CC transcript; Relay never classifies
 * purpose. Agents with `sessionWiGuard` are reminded and write-guarded from that
 * same transcript (session-wi-guard.ts). Missing resources skip registration only.
 */
import { spawnSync } from 'node:child_process';
import { existsSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { managedRelayPath } from './managed-paths.js';
import { nodeWorkItemCore } from './native-work-item-command.js';

export const CC_SESSION_WI_SERVER = 'pulse-request-purpose';
const TOOL = 'declare_session_wi';
export const CC_SESSION_WI_TOOL = `mcp__${CC_SESSION_WI_SERVER}__${TOOL}`;

export interface CcSessionWiMcp {
  server: { type: 'stdio'; command: string; args: string[] };
}

type Runner = (command: string, args: string[], input: string) => { status: number | null; stdout: string };

const defaultRunner: Runner = (command, args, input) => {
  const result = spawnSync(command, args, {
    input, encoding: 'utf8', timeout: 15_000, env: { PATH: process.env['PATH'] ?? '' },
  });
  return { status: result.status, stdout: result.stdout ?? '' };
};

/** The reviewed ComputeCost host resources Relay already uses for CC measurement. */
export function resolveCcSessionWiMcp(
  host: { python: string; pythonPath: string } | undefined,
  run: Runner = defaultRunner,
): CcSessionWiMcp | undefined {
  if (!host) return undefined;
  const args = [join(host.pythonPath, 'runtime', 'codex-mcp-request-purpose.py'), '--host', 'cc'];
  try {
    if (!existsSync(args[0])) throw new Error('session WI MCP entry is missing');
    const input = [
      { jsonrpc: '2.0', id: 1, method: 'initialize', params: {} },
      { jsonrpc: '2.0', id: 2, method: 'tools/list', params: {} },
    ].map(message => JSON.stringify(message) + '\n').join('');
    const output = run(host.python, args, input);
    if (output.status !== 0) throw new Error(`session WI MCP probe exited ${output.status}`);
    const replies = output.stdout.split('\n').filter(Boolean).map(line => JSON.parse(line) as {
      id?: number; result?: { instructions?: unknown; tools?: Array<{ name?: unknown }> };
    });
    const instructions = replies.find(reply => reply.id === 1)?.result?.instructions;
    const tools = replies.find(reply => reply.id === 2)?.result?.tools;
    if (typeof instructions !== 'string' || !instructions.trim()
      || !Array.isArray(tools) || tools.length !== 1 || tools[0]?.name !== TOOL) {
      throw new Error('session WI MCP probe returned an unexpected contract');
    }
    return { server: { type: 'stdio', command: host.python, args } };
  } catch (error) {
    console.warn({
      ts: new Date().toISOString(), level: 'warn',
      msg: 'CC session WI MCP unavailable; CC sessions start without it',
      error: error instanceof Error ? error.message : String(error),
    });
    return undefined;
  }
}

const CORE_BRIDGE = join(dirname(fileURLToPath(import.meta.url)), '..', 'runtime', 'cc-work-item-core.mjs');

/**
 * The Host MCP servers of every Relay CC session, conversation or scheduled:
 * the native core bridge (`pulse_work_item`, `pulse_stats`, `pulse_jobs`) when the node
 * selects a core, and the session WI declaration when it resolved. A
 * conversation's CC process carries the core environment the bridge inherits;
 * a scheduled run (`scheduled`) gives the bridge that environment itself and
 * claims no tenant, since the core reads the run from its scheduler proof.
 * Without the Relay data directory a scheduled run starts without the bridge.
 */
export function ccHostMcp(
  sessionWiMcp: CcSessionWiMcp | undefined, options: { scheduled?: boolean } = {},
): { args: string[]; tools: string[] } {
  const servers: Record<string, unknown> = {};
  const tools: string[] = [];
  const core = nodeWorkItemCore();
  const data = core && options.scheduled ? managedRelayPath('dataDir') : undefined;
  if (core && (!options.scheduled || data)) {
    const env = data ? { env: {
      PULSE_WORK_ITEM_CORE_HOST: JSON.stringify(core), PULSE_CORE_REQUEST_STATE_ROOT: join(data, 'native-requests'),
    } } : {};
    servers['pulse'] = { type: 'stdio', command: process.execPath, args: [CORE_BRIDGE], ...env };
    // pulse_jobs needs the conversation's key; a scheduled run has none.
    tools.push('mcp__pulse__pulse_work_item', 'mcp__pulse__pulse_stats', ...(options.scheduled ? [] : ['mcp__pulse__pulse_jobs']));
  }
  if (sessionWiMcp) {
    servers[CC_SESSION_WI_SERVER] = sessionWiMcp.server;
    tools.push(CC_SESSION_WI_TOOL);
  }
  return { args: tools.length ? ['--mcp-config', JSON.stringify({ mcpServers: servers })] : [], tools };
}
