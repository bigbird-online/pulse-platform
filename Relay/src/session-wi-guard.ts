/**
 * Mechanical WI-declaration guard for agents with `sessionWiGuard` enabled.
 *
 * A reminder line enters the prompt of an undeclared request; for CC a
 * PreToolUse hook additionally denies listed write operations until the
 * request is declared. Both read the existing declaration records — the CC
 * transcript and the Codex rollout — and keep no state of their own.
 */
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import type { StreamJsonMessage } from './types.js';

/** A trigger only: the rule itself is the declaration tool's own (MCP instructions or tool description). */
export const SESSION_WI_REMINDER = '[Relay] 本请求尚未调用 declare_session_wi 声明归属。';

/** The tools runtime/cc-session-wi-guard.mjs judges; all other tools never reach the hook. */
export const GUARD_MATCHER = 'Bash|mcp__pulse__pulse_work_item';
const GUARD_SCRIPT = join(dirname(fileURLToPath(import.meta.url)), '..', 'runtime', 'cc-session-wi-guard.mjs');

function shellQuote(value: string): string {
  return `'${value.replace(/'/g, `'\\''`)}'`;
}

function messageText(message: StreamJsonMessage): string {
  const content = message.content;
  if (typeof content === 'string') return content;
  if (!Array.isArray(content)) return '';
  return content.map(block => (block && typeof block === 'object' && (block as { type?: unknown }).type === 'text'
    ? String((block as { text?: unknown }).text ?? '') : '')).join('');
}

/** Append the reminder as the prompt's last line. Slash commands stay intact. */
export function withSessionWiReminder(message: StreamJsonMessage): StreamJsonMessage {
  if (messageText(message).trimStart().startsWith('/')) return message;
  const content = message.content;
  if (Array.isArray(content)) return { ...message, content: [...content, { type: 'text', text: SESSION_WI_REMINDER }] };
  return { ...message, content: `${content ?? ''}\n\n${SESSION_WI_REMINDER}` };
}

/** The CC settings fragment that registers the PreToolUse guard. */
export function ccSessionWiGuardHooks(node: string = process.execPath): Record<string, unknown> {
  return {
    PreToolUse: [{
      matcher: GUARD_MATCHER,
      hooks: [{ type: 'command', command: `${shellQuote(node)} ${shellQuote(GUARD_SCRIPT)}`, timeout: 10 }],
    }],
  };
}

/**
 * Merge a settings fragment into CC's single `--settings` value: hook groups are
 * appended per event, `env` variables are added over the existing ones, and
 * every other key, `sandbox` included, is replaced by the fragment's value. Inline JSON is merged; a settings file path is the
 * tenant's own user settings, which CC loads anyway as its user source, so the
 * path is replaced by the inline value.
 */
export function mergeCcSettingsArg(args: string[], fragment: Record<string, unknown>): void {
  const index = args.lastIndexOf('--settings');
  const present = index >= 0 && index + 1 < args.length;
  const settings = present && args[index + 1].trimStart().startsWith('{')
    ? JSON.parse(args[index + 1]) as Record<string, unknown> : {};
  const merged: Record<string, unknown> = { ...settings };
  for (const [key, value] of Object.entries(fragment)) {
    if (key === 'hooks') {
      const existing = (settings['hooks'] ?? {}) as Record<string, unknown[]>;
      const hooks: Record<string, unknown[]> = { ...existing };
      for (const [event, groups] of Object.entries(value as Record<string, unknown[]>)) hooks[event] = [...(existing[event] ?? []), ...groups];
      merged['hooks'] = hooks;
    } else if (key === 'env') {
      merged['env'] = { ...(settings['env'] as Record<string, unknown> | undefined), ...(value as Record<string, unknown>) };
    } else {
      merged[key] = value;
    }
  }
  if (present) args[index + 1] = JSON.stringify(merged);
  else args.push('--settings', JSON.stringify(merged));
}

/** Add the guard hooks to CC's single `--settings` value. */
export function addSessionWiGuardSettings(args: string[], node: string = process.execPath): void {
  mergeCcSettingsArg(args, { hooks: ccSessionWiGuardHooks(node) });
}

/** Codex declares once per session: a completed successful call in the rollout counts. */
export function codexSessionDeclared(rollout: Buffer | string | undefined): boolean {
  if (!rollout) return false;
  for (const line of rollout.toString().split('\n')) {
    if (!line.includes('declare_session_wi')) continue;
    try {
      const row = JSON.parse(line) as { type?: unknown; payload?: { type?: unknown; item?: Record<string, unknown> } };
      const item = row.payload?.item;
      if (row.type === 'event_msg' && row.payload?.type === 'item_completed' && item?.['type'] === 'McpToolCall'
          && item['server'] === 'pulse-request-purpose' && item['tool'] === 'declare_session_wi'
          && item['status'] === 'completed' && (item['result'] as { isError?: unknown } | undefined)?.isError === false) {
        return true;
      }
    } catch { /* an incomplete line is no declaration */ }
  }
  return false;
}
