import type { ConversationTurnEffect } from './types.js';

/** Public work records only. Never project model thinking or raw protocol frames. */
export type WorkProcessEvent = {
  id: string;
  kind: 'operation' | 'commentary';
  state: 'running' | 'completed' | 'failed';
  title?: string;
  input?: string;
  output?: string;
  restricted?: boolean;
  /** Absent on ordinary public commentary; never infer origin from prose. */
  source?: 'reasoning_summary';
};
const object = (value: unknown): Record<string, any> =>
  value !== null && typeof value === 'object' && !Array.isArray(value) ? value as Record<string, any> : {};
const internalTarget = /(?:^|[\s/\\'"`(=])(?:\.env(?:\.[\w.-]+)?|credentials(?:\.\w+)?|hosts\.yml|id_(?:rsa|ed25519)|(?:CLAUDE|AGENTS|SYSTEM|SOUL|IDENTITY)\.md)(?=$|[\s/\\'"`;|&<>()])/i;

export function sanitizeWorkText(value: unknown, limit = 32_000): string {
  if (typeof value !== 'string') return '';
  const clean = value
    .replace(/-----BEGIN [^-]*PRIVATE KEY-----[\s\S]*?-----END [^-]*PRIVATE KEY-----/g, '[私钥已隐藏]')
    .replace(/<(system-reminder|system|thinking|analysis)>[\s\S]*?<\/\1>/gi, '[内部内容已隐藏]')
    .replace(/\b(?:sk-[\w-]{12,}|gh[pousr]_[\w]{12,}|github_pat_[\w_]{12,})\b/g, '[凭据已隐藏]')
    .replace(/\b(Bearer|Basic)\s+[\w.+/=:-]+/gi, '$1 [凭据已隐藏]')
    .replace(/((?:(?:[\w-]+[_-])?(?:api[_-]?key|secret|token|password|passwd)|authorization)\s*["']?\s*[:=]\s*["']?)[^\s"',;]+/gi, '$1[凭据已隐藏]')
    .replace(/(https?:\/\/)[^\s/@]+:[^\s/@]+@/g, '$1[凭据已隐藏]@')
    .replace(/[\u0000-\u0008\u000b\u000c\u000e-\u001f\u007f]/g, '');
  const points = Array.from(clean);
  return points.length <= limit ? clean : `${points.slice(0, limit).join('')}\n[内容过长；此处仅展示前 ${limit} 字符]`;
}
export function suppressWorkOutput(input?: string): boolean { return internalTarget.test(input ?? ''); }
function text(value: unknown): string {
  if (typeof value === 'string') return value;
  if (Array.isArray(value)) return value.filter(v => object(v).type === 'text')
    .map(v => typeof object(v).text === 'string' ? object(v).text : '').join('\n');
  const nested = object(value).content;
  return nested === undefined ? '' : text(nested);
}
function toolInput(value: unknown): string {
  const args = object(value);
  return ['command', 'file_path', 'path', 'pattern', 'query', 'url', 'offset', 'limit']
    .filter(key => ['string', 'number'].includes(typeof args[key]))
    .map(key => `${key}: ${args[key]}`).join('\n');
}
function normalized(event: WorkProcessEvent): WorkProcessEvent {
  const restricted = suppressWorkOutput(event.input);
  const input = sanitizeWorkText(event.input, 2_000);
  return { ...event, ...(restricted ? { restricted: true } : {}), id: event.id.slice(0, 160), title: sanitizeWorkText(event.title, 120), input,
    output: restricted ? '[敏感或内部文件内容不展示]' : sanitizeWorkText(event.output) };
}
const readOnlyTools = new Set(['read', 'grep', 'find', 'ls', 'glob']);
/**
 * Claude Code 原生工具中不改变外部状态的那些（名字区分大小写，不会命中 Pi 的小写工具）。
 * Bash、Write、Edit、MultiEdit、NotebookEdit、KillShell、未知 mcp__* 仍算副作用；
 * Task/Agent 会派生可能改动工作区的子代理，同样算副作用。
 */
const ccReadOnlyTools = new Set([
  'TodoWrite', 'WebSearch', 'WebFetch', 'AskUserQuestion', 'ToolSearch', 'Skill', 'NotebookRead',
  'BashOutput', 'TaskOutput', 'ListMcpResources', 'ListMcpResourcesTool', 'ReadMcpResource', 'ReadMcpResourceTool',
]);
/** One tool call is one execution step; only listed lookups count as read-only. */
export function toolTurnEffect(name: string): ConversationTurnEffect {
  return readOnlyTools.has(name.toLowerCase()) || ccReadOnlyTools.has(name) ? 'read-only-effect' : 'side-effect-observed';
}
/** Native CC/SDK assistant record → its tool_use blocks (id + name). */
export function ccToolUses(value: unknown): Array<{ id: string; name: string }> {
  const record = object(value);
  const blocks = object(record.message).content ?? record.content;
  if (record.type !== 'assistant' || !Array.isArray(blocks)) return [];
  return blocks.map(object).filter(block => block.type === 'tool_use' && typeof block.id === 'string')
    .map(block => ({ id: block.id as string, name: typeof block.name === 'string' ? block.name : '' }));
}
export function publicWorkEvents(runtime: string, value: unknown): WorkProcessEvent[] {
  const record = object(value);
  if (runtime === 'pi') {
    if (!['tool_execution_start', 'tool_execution_update', 'tool_execution_end'].includes(record.type)
      || typeof record.toolCallId !== 'string') return [];
    return [normalized({ id: `pi:${record.toolCallId}`, kind: 'operation',
      title: typeof record.toolName === 'string' ? record.toolName : undefined,
      input: toolInput(record.args), output: text(record.result ?? record.partialResult),
      state: record.type === 'tool_execution_end' ? record.isError === true ? 'failed' : 'completed' : 'running' })];
  }
  if (runtime === 'agy') {
    // stream-json `step_update` of type tool; parameters use agy's own key names (Path, CommandLine…).
    const step = object(record.step_update);
    if (record.event !== 'step_update' || step.step_type !== 'tool' || typeof step.step_index !== 'number') return [];
    const info = object(step.tool_info);
    const input = Object.entries(object(info.parameters))
      .filter(([, value]) => ['string', 'number'].includes(typeof value))
      .map(([key, value]) => `${key}: ${value}`).join('\n');
    const conversation = typeof step.conversation_id === 'string' ? step.conversation_id : 'conversation';
    return [normalized({ id: `agy:${conversation}:${step.step_index}`, kind: 'operation',
      title: typeof step.tool_name === 'string' ? step.tool_name : '工具调用', input,
      output: text(info.output ?? info.error),
      state: step.state !== 'DONE' ? 'running' : info.error ? 'failed' : 'completed' })];
  }
  if (runtime === 'codex') {
    if (!['item.started', 'item.updated', 'item.completed'].includes(record.type)) return [];
    const item = object(record.item);
    if (typeof item.id !== 'string') return [];
    if (!['command_execution', 'file_change', 'mcp_tool_call', 'web_search', 'todo_list'].includes(item.type)) return [];
    const titles: Record<string, string> = { command_execution: '执行命令', file_change: '修改文件',
      mcp_tool_call: '工具调用', web_search: '搜索', todo_list: '执行计划' };
    const changes = Array.isArray(item.changes) ? item.changes.map((v: unknown) => {
      const change = object(v); return `${String(change.kind ?? '')} ${String(change.path ?? '')}`;
    }).join('\n') : '';
    const input = item.command ?? item.query ?? (changes || toolInput(item.arguments));
    const plan = Array.isArray(item.items) ? item.items.map((v: unknown) => {
      const task = object(v); return `${task.completed ? '✓' : '·'} ${String(task.text ?? '')}`;
    }).join('\n') : '';
    return [normalized({ id: `codex:${item.id}`, kind: 'operation', title: titles[item.type],
      input: String(input ?? ''), output: typeof item.aggregated_output === 'string'
        ? `${item.aggregated_output}${typeof item.exit_code === 'number' ? `\n退出码：${item.exit_code}` : ''}`
        : text(item.result) || plan,
      state: record.type !== 'item.completed' ? 'running'
        : item.status === 'failed' || item.error || (typeof item.exit_code === 'number' && item.exit_code !== 0) ? 'failed' : 'completed' })];
  }
  if (!['cc', 'cc-sdk'].includes(runtime) || !['assistant', 'user'].includes(record.type)) return [];
  const blocks = object(record.message).content ?? record.content;
  if (!Array.isArray(blocks)) return [];
  return blocks.flatMap((value: unknown) => {
    const block = object(value);
    if (record.type === 'assistant' && block.type === 'tool_use' && typeof block.id === 'string') {
      return [normalized({ id: `${runtime}:${block.id}`, kind: 'operation', state: 'running',
        title: typeof block.name === 'string' ? block.name : '工具调用', input: toolInput(block.input) })];
    }
    if (record.type === 'user' && block.type === 'tool_result' && typeof block.tool_use_id === 'string') {
      return [normalized({ id: `${runtime}:${block.tool_use_id}`, kind: 'operation',
        state: block.is_error === true ? 'failed' : 'completed', output: text(block.content) })];
    }
    return [];
  });
}
