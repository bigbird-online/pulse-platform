/**
 * /wi in agy conversations. agy runs without the core's tools (no per-session MCP), so the
 * Host does for it what a cc or codex session does through pulse_work_item: Relay names the
 * conversation to the core (session-identity.ts `nativeCommandSource`), attributes the
 * conversation's turns to the WI with a runtime usage binding, and tells the Agent plainly
 * what it cannot do.
 */
import { harnessPrincipalForAgent, type AgentConfig, type StateManager } from './types.js';
import { buildRuntimeUsageBinding, runtimeProcurementSource } from './runtime-usage-binding.js';
import type { CommandRuntime } from './commands.js';

/** The honest capability line every agy WI prompt carries; it never asks for a tool call. */
export const AGY_NO_WORK_ITEM_TOOLS = '本运行时（agy）不能调用 pulse_work_item：读不到检查点，也不能自己记检查点、改 WI 状态或提交 PR 登记。'
  + '请按本对话已有的上下文和人类的要求推进；需要内核操作时，告诉人类用 /wi 命令或到 cc、codex 会话里办。';

/**
 * How an agy commit is proved for automatic review (harness_rewrite/session_compute.py
 * `agy_commit_shape`): agy's output has no source marking, so only this one shape is read.
 */
export const AGY_COMMIT_CONVENTION = '要自动派审，每个提交单独一步执行 `cd <字面绝对路径> && git commit --no-verify -m ...`，'
  + '不加 -q，不与其他命令组合；命令输出直接读 stdout，不要重定向到工作区外的文件。';

/**
 * The terminal's reach (agy-conversation-runtime.ts `agyRestricted`): the same permissions as the Agent's
 * CC session.
 */
export const AGY_SANDBOX_REACH = '本会话终端的权限与本 Agent 的 CC 会话相同：CC 不受限时这里也不受限；CC 有写边界时，'
  + '这里同样只能写工作区和授权目录、只连配置的域名，内核操作用 `harness-native`。';

export const AGY_NO_CONVERSATION = 'agy 会话还没有对话：先发一条消息让 agy 建立对话，再执行这条 /wi 命令；未做任何改动。';

export function agyTakeoverPrompt(target: string, readback: string): string {
  return '接手 ' + target + '：人类已用 /wi 命令把这个工作项交给本会话（' + readback + '）。本会话之后的用量记在 '
    + target + '。' + AGY_NO_WORK_ITEM_TOOLS + AGY_COMMIT_CONVENTION + AGY_SANDBOX_REACH + '先简述你对这项工作的理解和下一步，再继续推进。';
}

/** `who` names the switch (`人类用 /wi switch` or `你用 request_switch`). */
export function agySwitchPrompt(target: string, who = '人类用 /wi switch', resumed = true): string {
  return resumed
    ? '切回 ' + target + '：' + who + ' 回到本会话，本会话之前在办这个工作项，之后的用量记在 ' + target + '。'
      + AGY_NO_WORK_ITEM_TOOLS + AGY_COMMIT_CONVENTION + AGY_SANDBOX_REACH + '接着本对话里的进度推进。'
    : '接手 ' + target + '：' + who + ' 在本会话接手这个工作项，之后的用量记在 ' + target + '。'
      + AGY_NO_WORK_ITEM_TOOLS + AGY_COMMIT_CONVENTION + AGY_SANDBOX_REACH + '先简述你对这项工作的理解和下一步，再继续推进。';
}

/** The successor's first turn; the marker line is what the core reads as its bootstrap. */
export function agyRolloverPrompt(ref: string, wi: string): string {
  return '[Host session continuation; this is not a new human decision.]\n'
    + '[Host continuation operation: ' + ref + ']\n'
    + '本会话接续工作项 ' + wi + '（原会话的会话轮换，原请求 ' + ref + '），之后的用量记在 ' + wi + '。'
    + '原会话的对话内容这里看不到。' + AGY_NO_WORK_ITEM_TOOLS + AGY_COMMIT_CONVENTION + AGY_SANDBOX_REACH
    + '只继续原来已获准的工作，不新建 WI；缺少上下文时先请人类补充。';
}

/**
 * Attribute the agy conversation of Relay session `sessionId` to `wi`: persisted for later
 * spawns (conversation-launch.ts) and given to the live process for its next turns. The binding
 * names `conversationId`, the native conversation the core recognized (by default the one saved
 * for the session); it holds only while agy continues that conversation. Returns a notice for
 * the human when no binding could be issued; the WI command itself has committed. A failed state
 * write throws and leaves the session and the live process as they were.
 */
export function bindAgyWorkItem(options: { poolKey: string; agent: AgentConfig; runtime: CommandRuntime;
  stateManager?: StateManager }, wi: string, sessionId: string, conversationId?: string): string | undefined {
  let usageBinding;
  try {
    usageBinding = buildRuntimeUsageBinding({ workItemId: wi, agentId: harnessPrincipalForAgent(options.agent),
      phase: 'conversation', epoch: 1, runtime: 'agy',
      procurementSource: runtimeProcurementSource(process.env, 'agy') ?? 'unknown' });
  } catch (error) {
    console.warn({ ts: new Date().toISOString(), level: 'warn', msg: 'agy WI usage binding not issued',
      poolKey: options.poolKey, wi, error: String(error) });
    return '注意：' + wi + ' 已交给本会话，但用量绑定未能发出，本会话的用量暂不计入该 WI。';
  }
  const session = options.stateManager?.getSession(options.poolKey);
  const saved = session?.agyConversation;
  const conversation = conversationId ?? (saved?.sessionId === sessionId ? saved.conversationId : undefined);
  const previous = session?.agyWorkItemBinding;
  options.stateManager?.updateSession(options.poolKey, { agyWorkItemBinding: { sessionId,
    ...(conversation ? { conversationId: conversation } : {}), usageBinding } });
  try {
    options.stateManager?.flushStrict();
  } catch (error) {
    // Not saved: the in-memory state goes back too, so a retry binds again rather than finding it done.
    options.stateManager?.updateSession(options.poolKey, { agyWorkItemBinding: previous });
    throw error;
  }
  const live = options.runtime.get(options.poolKey);
  if (live && live.sessionId === sessionId && live.state !== 'dead') options.runtime.bindRuntimeUsage?.(options.poolKey, usageBinding);
  return undefined;
}
