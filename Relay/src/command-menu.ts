import type { MenuCommand } from './types.js';
import { withTelegramRateLimit } from './channels/telegram-polling.js';

const RELAY_COMMANDS: MenuCommand[] = [
  { command: 'start',   description: '欢迎信息' },
  { command: 'stop',    description: '中断当前工作' },
  { command: 'status',  description: '查看 Agent 状态' },
  { command: 'new',     description: '开启新对话' },
  { command: 'wi',       description: 'WI status / continue / switch / checkpoint / rollover' },
  { command: 'reset',   description: '强制重置 session' },
  { command: 'session', description: '查看 session 信息' },
  { command: 'runtime', description: '查看/切换 runtime' },
  { command: 'model',   description: '查看/切换模型 (sonnet/opus/haiku)' },
  { command: 'models',  description: '查看当前 runtime 原生可用模型' },
  { command: 'think',   description: '查看/切换推理档位（各运行时）' },
  { command: 'compact', description: '压缩当前对话 context' },
  { command: 'context', description: '查看当前 context 状态' },
  { command: 'yolo',    description: '查看/切换 Codex YOLO 模式' },
  { command: 'usage',   description: '查看费用统计' },
  { command: 'usage_seal', description: '幂等封存 WI 绑定的 Pi usage' },
  { command: 'kill',    description: '终止当前进程' },
  { command: 'restart', description: '重启进程（保留会话）' },
  { command: 'help',    description: '查看所有命令' },
  { command: 'jobs',    description: '列出我的计划任务' },
  { command: 'job_add', description: '注册计划任务' },
  { command: 'job_del', description: '删除计划任务' },
  { command: 'job_run', description: '立即执行任务' },
];

export function getAllMenuCommands(): MenuCommand[] {
  return [...RELAY_COMMANDS];
}

/** Registers the menu, waiting out Telegram 429 answers; a failure is only logged. */
export async function registerCommandMenu(
  bot: { api: { setMyCommands: (cmds: MenuCommand[]) => Promise<unknown> } },
  options: { sleep?: (ms: number) => Promise<void> } = {},
): Promise<void> {
  try {
    await withTelegramRateLimit(() => bot.api.setMyCommands(getAllMenuCommands()), options);
    console.log({ ts: new Date().toISOString(), level: 'info', msg: `Registered ${getAllMenuCommands().length} TG commands` });
  } catch (err) {
    console.warn({ ts: new Date().toISOString(), level: 'warn', msg: 'Failed to register TG commands', error: String(err) });
  }
}
