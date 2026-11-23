import { replayPendingTurns } from '../shutdown-interrupts.js';
import { dispatchNativeWorkItemCommand, NATIVE_WI_UNAVAILABLE } from '../native-work-item-command.js';
import { WorkProcessView, workProcessEnabled } from '../work-process-view.js';
import { parseConversationPoolKey } from '../conversation-address.js';
import { telegramProcessWriter } from '../work-process-transports.js';
import type { WorkProcessEvent } from '../work-process-events.js';
import {annotatedHumanMessage, annotatedMediaMessage, invalidateHumanInput} from '../scl-human-decision.js';
import {runtimeEntryFailureMessage} from '../runtime-entry-error.js';
import { publicRuntimeExceptionNotice, publicRuntimeResultNotice } from '../public-runtime-failure.js';
import dns from 'node:dns';
dns.setDefaultResultOrder('ipv4first');

import { randomBytes } from 'node:crypto';
import { closeSync, existsSync, mkdtempSync, openSync, readSync, realpathSync, rmSync, statSync, writeFileSync } from 'node:fs';
import { downloadTelegramFile, telegramApiAgent } from './telegram-download.js';
import { getTelegramFile, telegramPhotoFailureMessage } from './telegram-file.js';
import { readTelegramLocalFiles, telegramTextWithLinks, telegramFileLinks } from './telegram-links.js';
import { tmpdir } from 'node:os';
import { basename, join } from 'node:path';
import { Bot, InlineKeyboard } from 'grammy';
import type { StreamJsonMessage, UserConfig, AgentConfig, SessionEndReason, StateManager, Scheduler, ConversationRuntimeId, SessionRecoveryReason, RoutePolicyResult, PoolExitMeta, BusyProgressEvent, PersistedSession } from '../types.js';
import type { CommandRuntime } from '../commands.js';
import type { Router } from '../router.js';
import {
  handleStart,
  handleStatus,
  handleHelp,
  handleKill,
  handleRestart,
  handleNew,
  handleReset,
  handleStop,
  handleSession,
  handleUsage,
  handleUsageSeal,
  handleRuntime,
  handleYolo,
  handleModel,
  handleModelScope,
  handleModels,
  handleThink,
  handleCompact,
  handleContext,
  handleList,
  handleSwitch,
  resolveInboundRoutingAgent,
  resolveAgentFromPoolKey,
  withAgentPickerUi,
  RELAY_SWITCH_CB_PREFIX,
  type CommandResult,
} from '../commands.js';
import { handleListJobs, handleAddJob, handleDelJob, handleRunJob } from '../job-commands.js';
import { registerCommandMenu } from '../command-menu.js';
import { stopQuietly, superviseTelegramPolling, type PollingSupervisor } from './telegram-polling.js';
import { transcribeAudio } from '../stt.js';
import { hasWrapBlockerCode, hasWrapEvidence } from '../session-wrap-guard.js';
import { normalizeTelegramChatAddress, resolveInboundRoute } from '../gateway.js';
import { checkFileForDelivery, deliverTelegramDocument } from '../delivery.js';
import { validateToolFileOutboxPath } from '../tool-file-outbox.js';
import type { TurnAcceptedMeta } from '../runtime-background-work.js';
import { ChannelTurnRenderingState, TURN_ACCEPTED_STATUS_TEXT, TURN_GENERATED_STATUS_TEXT, TurnActivityCounter, turnAcceptedStatusText, turnReplyLead, type ChannelRenderFrame } from '../channel-rendering.js';
import {
  compileTelegramPayloads,
  isRichDocRolloutEnabled,
  isTelegramRichMessageRolloutEnabled,
  persistChannelRenderingObservation,
  resolveRichDocChannelProfiles,
  resolveTelegramRichMessageProfile,
  type ChannelRenderingObserver,
  type ChannelRichDocProfile,
  type TelegramPayloadChunk,
  type TelegramRichMessageProfile,
} from '../channel-rich-rendering.js';
import { buildReplyContextPrompt } from '../reply-context.js';
import { traceInboundRequest } from '../relay-request-trace.js';
import { acquireConversation } from '../conversation-acquisition.js';
import { resolveCoevolutionRoot } from '../config-paths.js';
import { internalDeliveryPrompt } from '../collaboration-capture.js';
import { namesConversation, type NativeIdSource } from '../session-identity.js';
import { fencedInternalTurn } from '../internal-turn-fence.js';
import { collabTurnHeadline, telegramTurnNoticeHtml, type CollabTurnNotice } from '../collab-progress-notice.js';
import { replayTelegramIntake, telegramIntakeMiddleware } from './telegram-intake.js';
import { createChannelIntakeJournal, intakeAwareRuntime } from './intake-journal.js';
import { createChatLanes } from './chat-lanes.js';
const TG_MAX_LENGTH = 4096;
// grammY can keep retrying getUpdates while its Bot remains registered. A
// configured client is not a healthy ingress path without a recent poll.
const TG_POLL_FRESHNESS_MS = 90_000;
export const TELEGRAM_TURN_STATUS_TEXT = Object.freeze({
  accepted: TURN_ACCEPTED_STATUS_TEXT,
  generated: TURN_GENERATED_STATUS_TEXT,
  retrying: '🔄 遇到暂时问题，正在自动重试…',
  deliveryFailed: '⚠️ 最终回复投递失败，请重试。',
});

const UNREGISTERED_MSG = '\u26A0\uFE0F \u60A8\u5C1A\u672A\u6CE8\u518C\uFF0C\u8BF7\u8054\u7CFB\u7BA1\u7406\u5458\u5F00\u901A\u670D\u52A1\u3002';
const HARD_WRAP_MESSAGE = '⚠️ 当前会话上下文已满，无法在旧会话内执行 /wrap。已记录 hard-wrap 恢复标记；请发送 /new 开启新会话后，再让新会话读取旧 session jsonl 或日志总结上轮成果。';
const CONTEXT_RECOVERY_MESSAGE = '⚠️ 旧会话上下文已满或 compact 失败。新会话已开启；请让它读取旧 session jsonl 或日志，接续未完成的工作。';
const RECOVERY_HINT_MESSAGE = '⚠️ 检测到旧会话 context 已满或 compact 失败。普通会话可发送 /new 并从旧 session jsonl 或日志接续；活动 WI 须使用 /wi rollover 或 Harness recovery。';
const SESSION_TAIL_BYTES = 256 * 1024;
const WRAP_SKILL_RELATIVE_PATH = join('ssot', 'skills', 'ops', 'wrap', 'SKILL.md');
const WRAP_SKILL_UNAVAILABLE_MESSAGE = '⚠️ 当前 Host 找不到已安装的 /wrap Skill，本次未执行收口；请检查 PULSE_COEVOLUTION_ROOT 指向的工作区。';
const TELEGRAM_INBOUND_ROUTING = { inboundChannel: 'telegram' as const };
const TG_RESUME_ACTIVE_WINDOW_MS = 10 * 60 * 1000;
const TELEGRAM_DISABLED_TOOLS = new Set(['AskUserQuestion']);
const TELEGRAM_DEFAULT_ALLOWED_TOOLS = ['Read', 'Write', 'Edit', 'NotebookEdit', 'Glob', 'Grep', 'Bash', 'WebSearch', 'WebFetch', 'Agent', 'Task'];
const RELAY_MENU_CB_PREFIX = 'rm:';
const TELEGRAM_MENU_CALLBACK_TTL_MS = 10 * 60 * 1000;
const TELEGRAM_MENU_CALLBACK_LIMIT = 128;

type TelegramMenuAction = {
  text: string;
  action: 'runtime' | 'model_scope' | 'model' | 'think';
  arg: string;
};

type TelegramMenuCallback = Omit<TelegramMenuAction, 'text'> & {
  chatId: string;
  userId: string;
  agentId: string;
  expiresAt: number;
};

function telegramMenuActionFromButton(value: unknown): TelegramMenuAction | undefined {
  if (!value || typeof value !== 'object') return undefined;
  const button = value as Record<string, unknown>;
  if (button['tag'] !== 'button') return undefined;
  const text = (button['text'] as Record<string, unknown> | undefined)?.['content'];
  const behaviors = button['behaviors'];
  const callbackValue = Array.isArray(behaviors)
    ? (behaviors[0] as Record<string, unknown> | undefined)?.['value'] as Record<string, unknown> | undefined
    : undefined;
  const action = callbackValue?.['relay_menu'];
  const arg = callbackValue?.['relay_menu_arg'];
  if (typeof text !== 'string' || typeof action !== 'string' || typeof arg !== 'string') return undefined;
  if (!['runtime', 'model_scope', 'model', 'think'].includes(action)) return undefined;
  return { text, action: action as TelegramMenuAction['action'], arg };
}

export function extractTelegramMenuRows(elements?: Record<string, unknown>[]): TelegramMenuAction[][] {
  if (!elements) return [];
  const rows: TelegramMenuAction[][] = [];
  for (const element of elements) {
    if (element['tag'] !== 'column_set' || !Array.isArray(element['columns'])) continue;
    const row: TelegramMenuAction[] = [];
    for (const column of element['columns']) {
      const children = column && typeof column === 'object'
        ? (column as Record<string, unknown>)['elements']
        : undefined;
      if (!Array.isArray(children)) continue;
      for (const child of children) {
        const action = telegramMenuActionFromButton(child);
        if (action) row.push(action);
      }
    }
    if (row.length > 0) rows.push(row);
  }
  return rows;
}

export function createTelegramApiBot(botToken: string): Bot {
  return new Bot(botToken, {
    client: {
      baseFetchConfig: {
        agent: telegramApiAgent,
      },
    },
  });
}

export function resolveTelegramAllowedTools(allowedTools?: string[]): string[] {
  const source = allowedTools && allowedTools.length > 0
    ? allowedTools
    : TELEGRAM_DEFAULT_ALLOWED_TOOLS;
  return source.filter((tool) => !TELEGRAM_DISABLED_TOOLS.has(tool));
}

export function resolveWrapSkillPath(coevolutionRoot = resolveCoevolutionRoot()): string | undefined {
  for (const path of [
    join(coevolutionRoot, WRAP_SKILL_RELATIVE_PATH),
    join(coevolutionRoot, 'CollaborateWithAI', WRAP_SKILL_RELATIVE_PATH),
  ]) {
    try {
      if (statSync(path).isFile()) return realpathSync(path);
    } catch { /* This layout is not installed on the current Host. */ }
  }
  return undefined;
}

function wrapCommandPrompt(skillPath: string): string {
  return [
    '用户输入了精确 /wrap。',
    '入口层已确认：用户已授权 spawn 一个 reasoning_effort=medium 的收口员 sub-agent。',
    `请立即派出该收口员，并要求其严格按 ${skillPath} 执行 session 收口。`,
    '收口员可以草拟或编辑收口所需文件，但不得 git add 或 git commit。',
    '主 Agent 必须复核收口包和实际 diff，只提交本轮有意变更。',
  ].join('\n');
}

/**
 * Send a TG message with automatic retry on 429 (FloodWait).
 */
type TelegramHtmlOptions = { parse_mode: 'HTML' };
type TelegramRichMessageInput = { html: string; skip_entity_detection: true };
type TelegramRawRichMessageApi = {
  sendRichMessage?: (payload: { chat_id: number; rich_message: TelegramRichMessageInput }) => Promise<{ message_id?: number }>;
  editMessageText?: (payload: {
    chat_id: number;
    message_id: number;
    rich_message: TelegramRichMessageInput;
  }) => Promise<unknown>;
};

function telegramRawRichMessageApi(bot: Bot): TelegramRawRichMessageApi {
  return bot.api.raw as unknown as TelegramRawRichMessageApi;
}

function richMessageInput(html: string): TelegramRichMessageInput {
  return { html, skip_entity_detection: true };
}

async function sendWithRetry(
  bot: Bot,
  chatId: number,
  text: string,
  retries = 1,
  options?: TelegramHtmlOptions,
): Promise<number | undefined> {
  for (let attempt = 0; attempt <= retries; attempt++) {
    try {
      const sent = options
        ? await bot.api.sendMessage(chatId, text, options)
        : await bot.api.sendMessage(chatId, text);
      return sent?.message_id;
    } catch (err: unknown) {
      const is429 = err instanceof Error && /429|flood/i.test(err.message);
      if (!is429 || attempt === retries) {
        if (attempt === retries) {
          console.warn({ ts: new Date().toISOString(), level: 'warn', msg: 'TG send failed after retry', chatId, error: String(err) });
        }
        throw err;
      }
      const retryAfter = /retry.after.(\d+)/i.exec(err.message);
      const delayMs = retryAfter ? parseInt(retryAfter[1], 10) * 1000 : 30_000;
      console.log({ ts: new Date().toISOString(), level: 'info', msg: 'TG 429, retrying', chatId, delayMs, attempt });
      await new Promise(r => setTimeout(r, delayMs));
    }
  }
  return undefined;
}

async function sendRichMessageWithRetry(
  bot: Bot,
  chatId: number,
  html: string,
  retries = 1,
): Promise<number | undefined> {
  const sendRichMessage = telegramRawRichMessageApi(bot).sendRichMessage;
  if (!sendRichMessage) throw new Error('Telegram sendRichMessage method unavailable');
  const payload = { chat_id: chatId, rich_message: richMessageInput(html) };
  for (let attempt = 0; attempt <= retries; attempt += 1) {
    try {
      const sent = await sendRichMessage(payload);
      return sent?.message_id;
    } catch (error) {
      const rateLimited = /429|flood/i.test(String(error));
      if (!rateLimited || attempt === retries) throw error;
      const retryAfter = /retry.after.(\d+)/i.exec(String(error));
      const delayMs = retryAfter ? Number(retryAfter[1]) * 1000 : 30_000;
      await new Promise((resolve) => setTimeout(resolve, delayMs));
    }
  }
  return undefined;
}

export type TelegramRenderingSink = {
  process?(poolKey: string, chatId: number, event: WorkProcessEvent): Promise<void>;
  processAction?(data: string, address: string, messageId: string): Promise<boolean>;
  fail?(poolKey: string): Promise<void>;
  begin(poolKey: string, scope?: string, options?: { replyLead?: string }): void;
  status(poolKey: string, chatId: number, text: string, eventId?: string): Promise<void>;
  activity(poolKey: string, chatId: number, text: string, eventId?: string): Promise<void>;
  delta(poolKey: string, chatId: number, delta: string, eventId?: string): Promise<void>;
  assistant(poolKey: string, chatId: number, text: string, phase: 'snapshot' | 'commentary' | 'final', eventId?: string): Promise<void>;
  result(poolKey: string, chatId: number): Promise<void>;
};

export type TelegramRenderingSinkOptions = {
  workProcess?: boolean;
  processScope?: string;
  processThrottleMs?: number;
  richDocProfile?: ChannelRichDocProfile;
  richMessageProfile?: TelegramRichMessageProfile;
  observe?: ChannelRenderingObserver;
};

type TelegramTransportMode = 'rich_message_html' | 'rich_html' | 'safe_html' | 'plain_text';

function isTelegramFormatError(error: unknown): boolean {
  return /can't parse|parse entities|unsupported (?:start )?tag|entity.*(?:end|offset)|bad request.*format|method unavailable|sendrichmessage.*not found|404:\s*not found/i.test(String(error));
}

/** One evolving Telegram message per turn. Failed/rate-limited edits are
 * coalesced in memory; the authoritative final is sent at most once if editing
 * remains unavailable. */
export function createTelegramRenderingSink(bot: Bot, options: TelegramRenderingSinkOptions = {}): TelegramRenderingSink {
  type State = {
    processId?: string;
    rendering: ChannelTurnRenderingState;
    messageId?: number;
    finalSent: boolean;
    chain: Promise<void>;
    pending?: ChannelRenderFrame;
    flushing: boolean;
    richDelivery: boolean;
    shadow: boolean;
    nativeRichDelivery: boolean;
    transportMode?: TelegramTransportMode;
    lastTransportAt: number;
  };
  const states = new Map<string, State>();
  const terminalPools = new Set<string>();
  const profile = options.richDocProfile ?? resolveRichDocChannelProfiles().telegram;
  const richMessageProfile = options.richMessageProfile ?? resolveTelegramRichMessageProfile();
  const processes = (options.workProcess ?? workProcessEnabled())
    ? new WorkProcessView(`telegram:${options.processScope ?? 'default'}`, telegramProcessWriter(bot), options.processThrottleMs)
    : undefined;

  function observe(event: Parameters<NonNullable<TelegramRenderingSinkOptions['observe']>>[0]): void {
    persistChannelRenderingObservation(event);
    try {
      options.observe?.(event);
    } catch {
      // Observability must never alter delivery behavior.
    }
  }

  function begin(poolKey: string, scope = poolKey, options: { replyLead?: string } = {}): void {
    terminalPools.delete(poolKey);
    const richDelivery = isRichDocRolloutEnabled(profile, scope);
    states.set(poolKey, {
      processId: processes?.begin(poolKey),
      rendering: new ChannelTurnRenderingState(`${scope}/telegram`, options),
      finalSent: false,
      chain: Promise.resolve(),
      flushing: false,
      richDelivery,
      shadow: profile.rollout === 'shadow',
      nativeRichDelivery: richDelivery && isTelegramRichMessageRolloutEnabled(richMessageProfile, scope),
      lastTransportAt: 0,
    });
  }

  function stateFor(poolKey: string): State {
    let state = states.get(poolKey);
    if (!state) {
      begin(poolKey);
      state = states.get(poolKey)!;
    }
    return state;
  }

  function cleanupTurn(poolKey: string, state: State): void {
    if (states.get(poolKey) !== state) return;
    state.rendering.dispose();
    state.pending = undefined;
    if (states.get(poolKey) === state) states.delete(poolKey);
    terminalPools.add(poolKey);
    if (terminalPools.size > 2048) {
      const oldest = terminalPools.values().next().value as string | undefined;
      if (oldest) terminalPools.delete(oldest);
    }
  }

  async function edit(chatId: number, messageId: number, text: string): Promise<boolean> {
    const api = bot.api as typeof bot.api & { editMessageText?: (chatId: number, messageId: number, text: string) => Promise<unknown> };
    if (!api.editMessageText) return false;
    try {
      await api.editMessageText(chatId, messageId, text);
      return true;
    } catch (error) {
      if (/message is not modified/i.test(String(error))) return true;
      console.warn({ ts: new Date().toISOString(), level: 'warn', msg: 'TG render edit coalesced', chatId, messageId, error: String(error) });
      return false;
    }
  }

  async function retireProgressAboveFinal(chatId: number, messageId: number): Promise<boolean> {
    const api = bot.api as typeof bot.api & { deleteMessage?: (chatId: number, messageId: number) => Promise<unknown> };
    if (!api.deleteMessage) return false;
    try {
      await api.deleteMessage(chatId, messageId);
      return true;
    } catch (error) {
      console.warn({ ts: new Date().toISOString(), level: 'warn', msg: 'TG progress delete failed; generated marker will still be sent below final', chatId, messageId, error: String(error) });
      return false;
    }
  }

  async function publishGeneratedStatusBelow(chatId: number, messageId: number): Promise<void> {
    await retireProgressAboveFinal(chatId, messageId);
    await sendWithRetry(bot, chatId, TELEGRAM_TURN_STATUS_TEXT.generated);
  }

  async function terminalizeProgress(chatId: number, messageId: number, text: string): Promise<boolean> {
    const api = bot.api as typeof bot.api & { editMessageText?: (chatId: number, messageId: number, text: string) => Promise<unknown> };
    if (!api.editMessageText) return false;
    for (let attempt = 0; attempt < 2; attempt += 1) {
      try {
        await api.editMessageText(chatId, messageId, text);
        return true;
      } catch (error) {
        if (/message is not modified/i.test(String(error))) return true;
        const rateLimited = /429|flood/i.test(String(error));
        if (attempt === 0) {
          const retryAfter = /retry.after.(\d+)/i.exec(String(error));
          const delayMs = rateLimited
            ? Math.min(30_000, retryAfter ? Number(retryAfter[1]) * 1000 : 1_000)
            : 250;
          await new Promise((resolve) => setTimeout(resolve, delayMs));
          continue;
        }
        console.warn({ ts: new Date().toISOString(), level: 'warn', msg: 'TG progress terminal edit failed', chatId, messageId, error: String(error) });
        return false;
      }
    }
    return false;
  }

  function modeText(chunk: TelegramPayloadChunk, mode: TelegramTransportMode): string {
    if (mode === 'rich_message_html') return chunk.richMessageHtml;
    if (mode === 'rich_html') return chunk.richHtml;
    if (mode === 'safe_html') return chunk.safeHtml;
    return chunk.plainText;
  }

  function modesFrom(state: State): TelegramTransportMode[] {
    const all: TelegramTransportMode[] = state.nativeRichDelivery
      ? ['rich_message_html', 'rich_html', 'safe_html', 'plain_text']
      : ['rich_html', 'safe_html', 'plain_text'];
    return all.slice(Math.max(0, state.transportMode ? all.indexOf(state.transportMode) : 0));
  }

  function observeFallback(fromMode: TelegramTransportMode, toMode: TelegramTransportMode): void {
    observe({
      channel: 'telegram',
      layer: 'transport',
      action: 'fallback',
      fromMode,
      toMode,
      reason: 'format',
      result: 'coalesced',
    });
  }

  async function sendRichChunk(
    state: State,
    chatId: number,
    chunk: TelegramPayloadChunk,
  ): Promise<number | undefined> {
    const modes = modesFrom(state);
    for (let index = 0; index < modes.length; index += 1) {
      const mode = modes[index];
      try {
        const messageId = mode === 'rich_message_html'
          ? await sendRichMessageWithRetry(bot, chatId, modeText(chunk, mode), 1)
          : await sendWithRetry(
            bot,
            chatId,
            modeText(chunk, mode),
            1,
            mode === 'plain_text' ? undefined : { parse_mode: 'HTML' },
          );
        state.transportMode = mode;
        observe({ channel: 'telegram', layer: 'renderer', action: 'chunk', mode, result: 'delivered' });
        return messageId;
      } catch (error) {
        const nextMode = modes[index + 1];
        if (!nextMode || !isTelegramFormatError(error)) throw error;
        observeFallback(mode, nextMode);
      }
    }
    return undefined;
  }

  async function editRichChunk(
    state: State,
    chatId: number,
    messageId: number,
    chunk: TelegramPayloadChunk,
  ): Promise<boolean> {
    const api = bot.api as typeof bot.api & {
      editMessageText?: (chatId: number, messageId: number, text: string, options?: TelegramHtmlOptions) => Promise<unknown>;
    };
    if (!api.editMessageText) return false;
    const modes = modesFrom(state);
    for (let index = 0; index < modes.length; index += 1) {
      const mode = modes[index];
      try {
        if (mode === 'rich_message_html') {
          const editRichMessage = telegramRawRichMessageApi(bot).editMessageText;
          if (!editRichMessage) throw new Error('Telegram rich editMessageText method unavailable');
          await editRichMessage({
            chat_id: chatId,
            message_id: messageId,
            rich_message: richMessageInput(modeText(chunk, mode)),
          });
        } else if (mode === 'plain_text') await api.editMessageText(chatId, messageId, modeText(chunk, mode));
        else await api.editMessageText(chatId, messageId, modeText(chunk, mode), { parse_mode: 'HTML' });
        state.transportMode = mode;
        observe({ channel: 'telegram', layer: 'renderer', action: 'chunk', mode, result: 'delivered' });
        return true;
      } catch (error) {
        if (/message is not modified/i.test(String(error))) return true;
        const nextMode = modes[index + 1];
        if (nextMode && isTelegramFormatError(error)) {
          observeFallback(mode, nextMode);
          continue;
        }
        console.warn({ ts: new Date().toISOString(), level: 'warn', msg: 'TG rich render edit coalesced', chatId, messageId, error: String(error) });
        return false;
      }
    }
    return false;
  }

  function compileFrame(state: State, text: string, terminal: boolean): TelegramPayloadChunk[] | null {
    if (!state.richDelivery && !state.shadow) return null;
    try {
      const compilation = compileTelegramPayloads(text, {
        maxLength: Math.min(profile.rolloverThreshold, profile.maxPayloadLength),
        lengthUnit: profile.lengthUnit,
      });
      observe({
        channel: 'telegram',
        layer: 'compiler',
        action: 'compile',
        result: 'valid',
        chunkCount: compilation.chunks.length,
        rolloverCount: Math.max(0, compilation.chunks.length - 1),
        terminal,
      });
      if (compilation.chunks.length > 1) {
        observe({
          channel: 'telegram',
          layer: 'renderer',
          action: 'rollover',
          mode: state.nativeRichDelivery ? 'rich_message_html' : 'rich_html',
          rolloverCount: compilation.chunks.length - 1,
          result: 'valid',
        });
      }
      return state.richDelivery ? compilation.chunks : null;
    } catch {
      observe({
        channel: 'telegram',
        layer: 'compiler',
        action: 'validation',
        mode: state.richDelivery ? 'plain_text' : 'legacy',
        reason: 'validation',
        result: 'invalid',
      });
      return null;
    }
  }

  function enqueue(poolKey: string, chatId: number, frame: ChannelRenderFrame | null): Promise<void> {
    if (!frame) return Promise.resolve();
    const state = stateFor(poolKey);
    state.pending = frame;
    if (state.flushing) return state.chain;
    state.flushing = true;
    const operation = state.chain.catch(() => undefined).then(async () => {
      while (state.pending) {
        const next = state.pending;
        state.pending = undefined;
        const richChunks = compileFrame(state, next.text, next.final);
        const parts = richChunks ? [] : splitMessage(next.text);
        const first = richChunks?.[0] ?? parts[0] ?? '';
        if (!first) continue;
        if (next.final) {
          if (state.finalSent) continue;
          if (processes) await processes.drain(poolKey, state.processId);
          if (states.get(poolKey) !== state) return;
          if (richChunks) {
            try {
              for (const chunk of richChunks) await sendRichChunk(state, chatId, chunk);
              state.finalSent = true;
              await processes?.terminal(poolKey, true, next.text, state.processId);
              if (state.messageId !== undefined) {
                await publishGeneratedStatusBelow(chatId, state.messageId);
              }
              observe({
                channel: 'telegram',
                layer: 'renderer',
                action: 'terminal',
                mode: state.transportMode,
                terminal: true,
                result: 'delivered',
                chunkCount: richChunks.length,
              });
              observe({
                channel: 'telegram',
                layer: 'transport',
                action: 'terminal',
                mode: state.transportMode,
                terminal: true,
                result: 'delivered',
                chunkCount: richChunks.length,
              });
            } catch (error) {
              await processes?.terminal(poolKey, false, '', state.processId);
              if (state.messageId !== undefined) {
                await terminalizeProgress(chatId, state.messageId, TELEGRAM_TURN_STATUS_TEXT.deliveryFailed);
              }
              observe({
                channel: 'telegram',
                layer: 'transport',
                action: 'terminal',
                mode: state.transportMode ?? (state.nativeRichDelivery ? 'rich_message_html' : 'rich_html'),
                reason: /429|flood/i.test(String(error)) ? 'rate_limit' : 'network',
                terminal: true,
                result: 'failed',
              });
              cleanupTurn(poolKey, state);
              throw error;
            }
            cleanupTurn(poolKey, state);
            continue;
          }
          // Legacy/off and validation fallback keep the same final-first status
          // truth as RichDoc: never claim generation before the authoritative
          // reply has been delivered.
          try {
            for (const part of parts) await sendWithRetry(bot, chatId, part);
            state.finalSent = true;
            await processes?.terminal(poolKey, true, next.text, state.processId);
            if (state.messageId !== undefined) {
              await publishGeneratedStatusBelow(chatId, state.messageId);
            }
          } catch (error) {
            await processes?.terminal(poolKey, false, '', state.processId);
            if (state.messageId !== undefined) {
              await terminalizeProgress(chatId, state.messageId, TELEGRAM_TURN_STATUS_TEXT.deliveryFailed);
            }
            cleanupTurn(poolKey, state);
            throw error;
          }
          cleanupTurn(poolKey, state);
          continue;
        }
        if (richChunks) {
          if (state.messageId !== undefined) {
            const delayMs = profile.updateThrottleMs - (Date.now() - state.lastTransportAt);
            if (delayMs > 0) {
              await new Promise((resolve) => setTimeout(resolve, delayMs));
              if (state.pending) {
                observe({
                  channel: 'telegram',
                  layer: 'transport',
                  action: 'chunk',
                  mode: state.transportMode ?? (state.nativeRichDelivery ? 'rich_message_html' : 'rich_html'),
                  result: 'coalesced',
                });
                continue;
              }
            }
          }
          if (state.messageId === undefined) state.messageId = await sendRichChunk(state, chatId, richChunks[0]);
          else await editRichChunk(state, chatId, state.messageId, richChunks[0]);
          state.lastTransportAt = Date.now();
        } else if (state.messageId === undefined) {
          state.messageId = await sendWithRetry(bot, chatId, first as string);
        } else {
          await edit(chatId, state.messageId, first as string);
        }
      }
    }).finally(() => {
      state.flushing = false;
    });
    const safeOperation = operation.catch((error) => {
      if (state.rendering.isFinalized()) cleanupTurn(poolKey, state);
      console.warn({ ts: new Date().toISOString(), level: 'warn', msg: 'TG render delivery failed', poolKey, chatId, error: String(error) });
    });
    state.chain = safeOperation;
    return safeOperation;
  }

  return {
    begin,
    process: (poolKey, chatId, event) => processes?.event(poolKey, String(chatId), event) ?? Promise.resolve(),
    processAction: (data, address, messageId) => processes?.callback(data, address, messageId) ?? Promise.resolve(false),
    fail: poolKey => processes?.terminal(poolKey, false) ?? Promise.resolve(),
    status(poolKey, chatId, text, eventId) {
      if (terminalPools.has(poolKey)) return Promise.resolve();
      const state = stateFor(poolKey);
      const frame = state.rendering.status(text, eventId);
      return processes ? processes.frame(poolKey, String(chatId), 'status', text, eventId) : enqueue(poolKey, chatId, frame);
    },
    activity(poolKey, chatId, text, eventId) {
      if (terminalPools.has(poolKey)) return Promise.resolve();
      const state = stateFor(poolKey);
      const frame = state.rendering.activity(text, eventId);
      return processes ? processes.frame(poolKey, String(chatId), 'activity', text, eventId) : enqueue(poolKey, chatId, frame);
    },
    delta(poolKey, chatId, delta, eventId) {
      if (terminalPools.has(poolKey)) return Promise.resolve();
      const state = stateFor(poolKey);
      const frame = state.rendering.delta(delta, eventId);
      return processes ? processes.frame(poolKey, String(chatId), 'snapshot', state.rendering.currentText(), eventId) : enqueue(poolKey, chatId, frame);
    },
    assistant(poolKey, chatId, text, phase, eventId) {
      if (terminalPools.has(poolKey)) return Promise.resolve();
      const state = stateFor(poolKey);
      const frame = phase === 'commentary'
        ? (processes ? state.rendering.snapshot(text, eventId) : state.rendering.commentary(text, eventId))
        : phase === 'final'
          ? state.rendering.final(text, eventId, Boolean(processes))
          : state.rendering.snapshot(text, eventId);
      if (processes && phase === 'final') void processes.frame(poolKey, String(chatId), phase, text, eventId);
      if (processes && phase !== 'final') return processes.frame(poolKey, String(chatId), phase, text, eventId);
      return enqueue(poolKey, chatId, frame);
    },
    result(poolKey, chatId) {
      if (terminalPools.has(poolKey)) return Promise.resolve();
      const state = stateFor(poolKey);
      const frame = state.rendering.final();
      if (!frame && !state.rendering.currentText()) return processes?.terminal(poolKey, false) ?? Promise.resolve();
      return enqueue(poolKey, chatId, frame);
    },
  };
}


/**
 * Split long text into chunks that fit within Telegram's message limit.
 */
export function splitMessage(text: string, maxLength: number = TG_MAX_LENGTH): string[] {
  if (text.length <= maxLength) return [text];

  const result: string[] = [];
  let remaining = text;

  while (remaining.length > 0) {
    if (remaining.length <= maxLength) { result.push(remaining); break; }

    let splitIdx = remaining.lastIndexOf('\n\n', maxLength);
    if (splitIdx > 0) { result.push(remaining.slice(0, splitIdx)); remaining = remaining.slice(splitIdx + 2); continue; }

    splitIdx = remaining.lastIndexOf('\n', maxLength);
    if (splitIdx > 0) { result.push(remaining.slice(0, splitIdx)); remaining = remaining.slice(splitIdx + 1); continue; }

    result.push(remaining.slice(0, maxLength));
    remaining = remaining.slice(maxLength);
  }

  return result;
}

/**
 * Add agent identity prefix to the first chunk of a response.
 */
export function prefixMessage(agent: AgentConfig, text: string, isFirstChunk: boolean): string {
  if (!isFirstChunk) return text;
  return `${agent.emoji} ${agent.name}:\n${text}`;
}

/**
 * CC internal diagnostic patterns that should not be forwarded to TG users.
 * These appear when CC outputs auth/system status as part of assistant content.
 */
const CC_NOISE_PATTERNS = [
  /OAuth\s*轮询.*超时/,
  /Key\s*已经在用了/,
  /second OAuth poll.*timed out/i,
  /apiKeySource/,
  // CC emits "[Request interrupted]" / "[Request interrupted by user]" as a
  // standalone marker line when a tool turn is aborted (e.g. AskUserQuestion
  // with no interactive frontend). Anchored to the whole line so normal prose
  // that merely contains "interrupted" is not stripped.
  /^\[Request interrupted(?: by user)?\]$/i,
];

/**
 * Strip CC internal diagnostic lines from response text.
 * Returns cleaned text, or empty string if entire text is noise.
 */
export function stripCCNoise(text: string): string {
  const lines = text.split('\n');
  const cleaned = lines.filter(line => {
    const trimmed = line.trim();
    if (!trimmed) return true; // keep blank lines
    return !CC_NOISE_PATTERNS.some(p => p.test(trimmed));
  });
  return cleaned.join('\n').trim();
}

export function detectContextRecoveryReason(text: string): SessionRecoveryReason | undefined {
  if (/Error during compaction|compaction failed|compact failed/i.test(text)) {
    return 'compact-failed';
  }
  if (/context window limit|context-window-limit/i.test(text)) {
    return 'context-window-limit';
  }
  return undefined;
}

export function isContextRecoverySignal(text: string): boolean {
  return Boolean(detectContextRecoveryReason(text));
}

export function isExactWrapCommand(text: string): boolean {
  return text.trim() === '/wrap';
}

export function expandWrapCommandPrompt(text: string, skillPath?: string): string {
  if (!isExactWrapCommand(text)) return text;
  const resolvedPath = skillPath ?? resolveWrapSkillPath();
  if (!resolvedPath) throw new Error('wrap_skill_unavailable');
  return wrapCommandPrompt(resolvedPath);
}

function truncateReason(reason: string, maxLen = 120): string {
  const collapsed = reason.replace(/\s+/g, ' ').trim();
  if (collapsed.length <= maxLen) return collapsed;
  return `${collapsed.slice(0, maxLen - 1)}…`;
}

function extractTelegramReplyContext(replyMessage: unknown): string | undefined {
  if (!replyMessage || typeof replyMessage !== 'object') return undefined;
  const msg = replyMessage as Record<string, unknown>;
  if (typeof msg.text === 'string' && msg.text.trim()) return telegramTextWithLinks(msg.text, msg.entities);
  if (typeof msg.caption === 'string' && msg.caption.trim()) return telegramTextWithLinks(msg.caption, msg.caption_entities);

  const labels: string[] = [];
  if (msg.photo) labels.push('[Telegram photo]');
  if (msg.document) labels.push('[Telegram document]');
  if (msg.voice) labels.push('[Telegram voice]');
  if (msg.audio) labels.push('[Telegram audio]');
  if (msg.video) labels.push('[Telegram video]');
  if (msg.sticker) labels.push('[Telegram sticker]');
  return labels[0];
}

function resolveWorkspacePath(workspace: string): string {
  try {
    return realpathSync(workspace);
  } catch {
    return workspace;
  }
}

function getClaudeSessionFilePath(workspace: string, sessionId: string): string {
  const cwdEncoded = resolveWorkspacePath(workspace).replace(/[^A-Za-z0-9_-]/g, '-');
  return join(process.env['HOME'] ?? '', '.claude', 'projects', cwdEncoded, `${sessionId}.jsonl`);
}

function readFileTail(path: string, maxBytes: number): string {
  if (!existsSync(path)) return '';
  const size = statSync(path).size;
  const start = Math.max(0, size - maxBytes);
  const length = size - start;
  if (length <= 0) return '';
  const fd = openSync(path, 'r');
  try {
    const buffer = Buffer.alloc(length);
    readSync(fd, buffer, 0, length, start);
    return buffer.toString('utf8');
  } finally {
    closeSync(fd);
  }
}

export interface RuntimePhotoInput {
  runtime: ConversationRuntimeId;
  mediaType: string;
  caption?: string;
  base64?: string;
  filePath?: string;
  cleanupRoot?: string;
}

/** The authenticated Telegram event a media message came from, if Telegram named it. */
function telegramSource(ctx: { update?: { update_id?: number } }, humanRef: string, message: string) {
  const eventId = ctx.update?.update_id;
  return eventId === undefined ? undefined : { channel: 'telegram' as const, eventId: String(eventId), humanRef, message };
}

export function buildRuntimePhotoMessage(input: RuntimePhotoInput): StreamJsonMessage {
  if (input.runtime === 'codex') {
    if (!input.filePath) throw new Error('Codex photo message requires a temporary file path');
    return {
      type: 'human',
      content: input.caption ?? '',
      attachments: [
        {
          kind: 'image',
          path: input.filePath,
          mediaType: input.mediaType,
          cleanup: true,
          ...(input.cleanupRoot ? { cleanupRoot: input.cleanupRoot } : {}),
        },
      ],
    };
  }

  if (!input.base64) throw new Error('CC photo message requires base64 image data');
  const contentBlocks: unknown[] = [
    { type: 'image', source: { type: 'base64', media_type: input.mediaType, data: input.base64 } },
  ];
  if (input.caption) {
    contentBlocks.push({ type: 'text', text: input.caption });
  }
  return { type: 'human', content: contentBlocks };
}

/** One image representation for photos, image documents and explicit local links. */
export function telegramImageMessage(bytes: Buffer, mediaType: string, runtime: ConversationRuntimeId, caption?: string): StreamJsonMessage {
  if (runtime !== 'codex') return buildRuntimePhotoMessage({ runtime, mediaType, base64: bytes.toString('base64') });
  const cleanupRoot = mkdtempSync(join(tmpdir(), 'relay-tg-photo-'));
  const extension = ({ 'image/jpeg': '.jpg', 'image/png': '.png', 'image/gif': '.gif', 'image/webp': '.webp' } as Record<string, string>)[mediaType] ?? '.jpg';
  const filePath = join(cleanupRoot, `image${extension}`);
  try { writeFileSync(filePath, bytes); }
  catch (error) { rmSync(cleanupRoot, { recursive: true, force: true }); throw error; }
  return buildRuntimePhotoMessage({ runtime, mediaType, filePath, cleanupRoot, caption });
}

export function appendTelegramLocalFiles(message: StreamJsonMessage, urls: string[], roots: string[] | undefined,
  runtime: ConversationRuntimeId): StreamJsonMessage {
  // Read/validate all files before creating runtime attachments.
  const attachments = Array.isArray(message.attachments) ? [...message.attachments] : [];
  const created: Array<{ cleanupRoot?: string }> = [];
  try {
    const files = readTelegramLocalFiles(urls, roots);
    if (!files.length) return message;
    const originalContent: unknown[] = typeof message.content === 'string'
      ? [{ type: 'text', text: message.content }] : [...(message.content as unknown[] ?? [])];
    const content: unknown[] = [];
    for (const file of files) {
      content.push({ type: 'text', text: `[用户提供的本地文件：${file.url}]` });
      if (file.mediaType) {
        const image = telegramImageMessage(file.bytes, file.mediaType, runtime);
        if (Array.isArray(image.content)) content.push(...image.content);
        if (Array.isArray(image.attachments)) { attachments.push(...image.attachments); created.push(...image.attachments); }
      } else {
        content.push({ type: 'text', text: `[文件内容开始]\n${file.bytes.toString('utf8')}\n[文件内容结束]` });
      }
    }
    // Keep the original object and its out-of-band human proof. The user's words
    // and provenance still close the message, after the attached source material.
    message.content = [...content, ...originalContent];
    if (attachments.length) message.attachments = attachments;
  } catch (error) {
    for (const attachment of created) {
      if (attachment?.cleanupRoot) rmSync(attachment.cleanupRoot, { recursive: true, force: true });
    }
    throw error;
  }
  return message;
}

// --- Multi-Bot Telegram ---

export interface TelegramBotDeps {
  runtime: CommandRuntime;
  router: Router;
  stateManager?: StateManager;
  scheduler?: Scheduler;
  meteringDir?: string;
  metering?: {
    onSoftQuotaWarning(handler: (event: import('../types.js').SoftQuotaWarningEvent) => void): void;
  };
  modelCatalog?: import('../model-catalog.js').ModelCatalog;
}

export interface TelegramBotInstance {
  start(): Promise<void>;
  stop(): void;
  notifyResumed(): Promise<void>;
  /** Start one internal turn in an existing conversation this bot owns
   * (collaboration delivery). False when not owned or its native session changed. */
  deliverInternalTurn(poolKey: string, prompt: string, expectNativeIds: string[], notice?: CollabTurnNotice, deliveryRef?: string): Promise<boolean>;
  reload(): Promise<{ added: string[]; removed: string[]; total: number }>;
  runningAgentIds(): string[];
  getApiBot(agentId: string): Bot | null;
  sendText(agentId: string, chatId: string, text: string): Promise<void>;
}

interface TelegramRegistryClient {
  start(): Promise<void>;
  stop(): void;
}

/** Shared-runtime listeners outlive an individual polling generation because
 * the runtime API has no unsubscribe handle. Gate every such listener on the
 * instance lifecycle so a removed generation cannot emit outbound traffic. */
export function activeTelegramListener<TArgs extends unknown[]>(
  isActive: () => boolean,
  listener: (...args: TArgs) => void,
): (...args: TArgs) => void {
  return (...args: TArgs): void => {
    if (!isActive()) return;
    listener(...args);
  };
}

export async function reconcileTelegramClientRegistry<TSpec, TClient extends TelegramRegistryClient>(
  running: Map<string, TClient>,
  desired: Map<string, TSpec>,
  create: (id: string, spec: TSpec) => TClient,
): Promise<{ added: string[]; removed: string[]; total: number }> {
  const added: string[] = [];
  const removed: string[] = [];
  for (const id of Array.from(running.keys()).sort()) {
    if (desired.has(id)) continue;
    const client = running.get(id)!;
    try {
      client.stop();
      running.delete(id);
      removed.push(id);
    } catch (error) {
      throw new Error(`Telegram client reconcile failed before additions: stop ${id}: ${String(error)}`);
    }
  }
  for (const id of Array.from(desired.keys()).sort()) {
    if (running.has(id)) continue;
    const client = create(id, desired.get(id)!);
    try {
      await client.start();
      running.set(id, client);
      added.push(id);
    } catch (error) {
      try { client.stop(); } catch { /* retain original start failure */ }
      for (const addedId of added) {
        try { running.get(addedId)?.stop(); } catch { /* registry deletion remains mandatory */ }
        running.delete(addedId);
      }
      throw new Error(`Telegram client reconcile failed: start ${id}: ${String(error)}`);
    }
  }
  return { added, removed, total: running.size };
}

/** Menus register one bot at a time, after and apart from polling start-up:
 * a 429 wait for one bot's menu delays neither its polling nor any other bot. */
let commandMenuQueue: Promise<void> = Promise.resolve();
function queueCommandMenu(register: () => Promise<void>): void {
  commandMenuQueue = commandMenuQueue.then(register).catch(() => undefined);
}

/**
 * Create a single TG bot for one agent.
 * Each OA has its own bot token → its own Bot instance.
 */
function createSingleBot(
  agent: AgentConfig,
  authorizedChatIds: Set<string>,
  deps: TelegramBotDeps,
  /** Created after the Relay's resume pass (a reload): nothing to wait for. */
  afterResume = false,
): TelegramBotInstance & { apiBot(): Bot } {
  const { router } = deps;
  // Every send from this bot's handlers hands its update over (intake journal).
  const runtime = intakeAwareRuntime(deps.runtime);
  let active = false;
  let pollingStarted = false;
  let lastSuccessfulPollAt = 0;
  let polling: PollingSupervisor | undefined;
  const bot = createTelegramApiBot(agent.botToken!);
  const intake = createChannelIntakeJournal(deps.stateManager, `telegram:${agent.id}`);
  const lanes = createChatLanes((chatId, error) => {
    console.error({ ts: new Date().toISOString(), level: 'error', agent: agent.id, msg: 'grammy error', chatId, error: String(error) });
  });
  let resumeDone!: () => void;
  const resumed = new Promise<void>(resolve => { resumeDone = resolve; });
  if (afterResume) resumeDone();
  bot.api.config.use(async (prev, method, payload, signal) => {
    // getUpdates acknowledges the updates handled so far: they are written
    // to the intake journal first. A failed write fails the poll, and grammY
    // retries it without acknowledging.
    if (method === 'getUpdates') intake.persist();
    const response = await prev(method, payload, signal);
    if (method === 'getUpdates' && response.ok) lastSuccessfulPollAt = Date.now();
    return response;
  });
  // Registered before every handler: polling only records updates and hands
  // them to chat lanes.
  bot.use(telegramIntakeMiddleware(lanes, intake));
  const menuCallbacks = new Map<string, TelegramMenuCallback>();

  const commandDeps = { router, runtime, stateManager: deps.stateManager, meteringDir: deps.meteringDir, modelCatalog: deps.modelCatalog, inboundChannel: 'telegram' as const };

  function agentForPoolKey(userId: string, poolKey: string): AgentConfig {
    return resolveAgentFromPoolKey(router, userId, poolKey) ?? agent;
  }

  /**
   * Resolve the sessionId to PERSIST for a poolKey: live CC process first, then
   * whatever we already recorded, and only then the router config value.
   *
   * 2026-07-25 记忆错乱事故（飞书侧同病，见 channels/feishu.ts 同名函数）：直接写
   * `ag.sessionId`（配置值）会在 CC 自产 session 时把 state 写成错的那条，进程驱逐后
   * 就 resume 到别的会话上。配置值只能当冷启动兜底。
   */
  function sessionIdToPersist(poolKey: string, ag: AgentConfig): string | undefined {
    return runtime.get(poolKey)?.sessionId
      ?? deps.stateManager?.getSession(poolKey)?.sessionId
      ?? ag.sessionId;
  }

  function pruneMenuCallbacks(now = Date.now()): void {
    for (const [token, callback] of menuCallbacks) {
      if (callback.expiresAt <= now) menuCallbacks.delete(token);
    }
  }

  function buildMenuKeyboard(
    rows: TelegramMenuAction[][],
    scope: { chatId: string; userId: string; agentId: string },
  ): InlineKeyboard {
    pruneMenuCallbacks();
    const kb = new InlineKeyboard();
    for (let i = 0; i < rows.length; i++) {
      for (const action of rows[i]!) {
        while (menuCallbacks.size >= TELEGRAM_MENU_CALLBACK_LIMIT) {
          const oldest = menuCallbacks.keys().next().value as string | undefined;
          if (!oldest) break;
          menuCallbacks.delete(oldest);
        }
        let token: string;
        do token = `${RELAY_MENU_CB_PREFIX}${randomBytes(9).toString('base64url')}`;
        while (menuCallbacks.has(token));
        menuCallbacks.set(token, {
          action: action.action,
          arg: action.arg,
          ...scope,
          expiresAt: Date.now() + TELEGRAM_MENU_CALLBACK_TTL_MS,
        });
        kb.text(action.text, token);
      }
      if (i < rows.length - 1) kb.row();
    }
    return kb;
  }

  function consumeMenuCallback(
    data: string,
    scope: { chatId: string; userId: string; agentId: string },
  ): Pick<TelegramMenuCallback, 'action' | 'arg'> | undefined {
    pruneMenuCallbacks();
    const callback = menuCallbacks.get(data);
    if (!callback) return undefined;
    if (callback.chatId !== scope.chatId || callback.userId !== scope.userId || callback.agentId !== scope.agentId) {
      return undefined;
    }
    menuCallbacks.delete(data);
    return { action: callback.action, arg: callback.arg };
  }

  async function replyCommandResult(
    ctx: { reply: (text: string, other?: Record<string, unknown>) => Promise<unknown> },
    result: CommandResult,
    scope?: { chatId: string; userId: string; agentId: string },
  ): Promise<void> {
    if (result.inlineKeyboard?.length) {
      const kb = new InlineKeyboard();
      const rows = result.inlineKeyboard;
      for (let i = 0; i < rows.length; i++) {
        const row = rows[i]!;
        for (const btn of row) kb.text(btn.text, btn.callbackData);
        if (i < rows.length - 1) kb.row();
      }
      await ctx.reply(result.text, { reply_markup: kb });
    } else if (scope) {
      const rows = extractTelegramMenuRows(result.feishuCardElements);
      if (rows.length > 0) {
        await ctx.reply(result.text, { reply_markup: buildMenuKeyboard(rows, scope) });
      } else {
        await ctx.reply(result.text);
      }
    } else {
      await ctx.reply(result.text);
    }
  }

  function resolveGatewayRoute(chatId: string, expectedUserId: string, text?: string): RoutePolicyResult | undefined {
    const route = resolveInboundRoute({
      users: router.getAllUsers(),
      bindings: router.getGatewayBindings?.(),
      event: {
        id: `tg:${agent.id}:${chatId}:${Date.now()}`,
        createdAt: new Date().toISOString(),
        channel: 'telegram',
        address: normalizeTelegramChatAddress(chatId, agent.id),
        text,
      },
      targetAgentId: agent.id,
    });
    if (route.status !== 'ok') {
      console.warn({
        ts: new Date().toISOString(),
        level: 'warn',
        msg: 'Gateway route facade did not resolve authorized TG route',
        agent: agent.id,
        chatId,
        status: route.status,
        reason: route.reason,
      });
      return undefined;
    }
    if (route.route.agentId !== agent.id || route.route.principal.id !== expectedUserId) {
      console.warn({
        ts: new Date().toISOString(),
        level: 'warn',
        msg: 'Gateway route facade drift',
        agent: agent.id,
        routeAgent: route.route.agentId,
        expectedUserId,
        routePrincipal: route.route.principal.id,
        chatId,
      });
      return undefined;
    }
    return route.route;
  }

  // --- Commands ---

  function commandSendFailed(error: unknown): void {
    console.error({ ts: new Date().toISOString(), level: 'error', agent: agent.id, msg: 'Failed to send command to conversation runtime', error: String(error) });
  }

  bot.command('start', async (ctx) => {
    const chatId = String(ctx.chat.id);
    if (!authorizedChatIds.has(chatId)) { await ctx.reply(UNREGISTERED_MSG); return; }
    const resolved = router.resolveByBotAgent(agent.id, chatId);
    const result = handleStart(resolved?.user.userId ?? '', agent.id, commandDeps);
    await replyCommandResult(ctx, result);
  });

  bot.command('status', async (ctx) => {
    const chatId = String(ctx.chat.id);
    const resolved = router.resolveByBotAgent(agent.id, chatId);
    if (!resolved) { await ctx.reply(UNREGISTERED_MSG); return; }
    invalidateHumanInput(router.poolKey(resolved.user.userId,resolveInboundRoutingAgent(router,resolved.user.userId,resolved.agent,TELEGRAM_INBOUND_ROUTING).id));
    const routing = resolveInboundRoutingAgent(router, resolved.user.userId, resolved.agent, TELEGRAM_INBOUND_ROUTING);
    const result = handleStatus(resolved.user.userId, routing.id, commandDeps);
    await ctx.reply(result.text);
  });

  bot.command('new', async (ctx) => {
    const chatId = String(ctx.chat.id);
    const resolved = router.resolveByBotAgent(agent.id, chatId);
    if (!resolved) { await ctx.reply(UNREGISTERED_MSG); return; }
    invalidateHumanInput(router.poolKey(resolved.user.userId,resolveInboundRoutingAgent(router,resolved.user.userId,resolved.agent,TELEGRAM_INBOUND_ROUTING).id));
    const routing = resolveInboundRoutingAgent(router, resolved.user.userId, resolved.agent, TELEGRAM_INBOUND_ROUTING);
    const poolKey = router.poolKey(resolved.user.userId, routing.id);
    const reconciled = deps.stateManager?.getSession(poolKey);
    // /wrap is historical. Detect context recovery for ordinary sessions, but
    // leave active and pending WI replacement to handleNew's independent guard.
    const recoveryRequired = !reconciled?.activeWorkItemRuntimeBinding
      && !reconciled?.pendingWorkItemEntry
      && recoverRequiredFromSessionTail(poolKey);
    const result = await handleNew(resolved.user.userId, routing.id, commandDeps);
    clearWrapRequested(poolKey);
    await ctx.reply(recoveryRequired && result.text.includes('已开启新对话')
      ? `${CONTEXT_RECOVERY_MESSAGE}\n\n${result.text}` : result.text);
  });

  bot.command('wi', async (ctx) => {
    const chatId = String(ctx.chat.id);
    const resolved = router.resolveByBotAgent(agent.id, chatId);
    if (!resolved) { await ctx.reply(UNREGISTERED_MSG); return; }
    invalidateHumanInput(router.poolKey(resolved.user.userId,resolveInboundRoutingAgent(router,resolved.user.userId,resolved.agent,TELEGRAM_INBOUND_ROUTING).id));
    const routing = resolveInboundRoutingAgent(router, resolved.user.userId, resolved.agent, TELEGRAM_INBOUND_ROUTING);
    try {
      await dispatchNativeWorkItemCommand({
        agent:routing, poolKey:router.poolKey(resolved.user.userId,routing.id), runtime,
        source:ctx.update?.update_id!==undefined ? {channel:'telegram', eventId:String(ctx.update.update_id),
          humanRef:resolved.user.userId, message:ctx.message?.text ?? ''} : undefined,
        stateManager:deps.stateManager,reply:text=>ctx.reply(text),
        acquire:()=>ensureProcess(chatId,resolved),
      });
    } catch { await ctx.reply(NATIVE_WI_UNAVAILABLE); }
  });

  bot.command('reset', async (ctx) => {
    const chatId = String(ctx.chat.id);
    const resolved = router.resolveByBotAgent(agent.id, chatId);
    if (!resolved) { await ctx.reply(UNREGISTERED_MSG); return; }
    invalidateHumanInput(router.poolKey(resolved.user.userId,resolveInboundRoutingAgent(router,resolved.user.userId,resolved.agent,TELEGRAM_INBOUND_ROUTING).id));
    const poolKey = router.poolKey(resolved.user.userId, agent.id);
    const result = await handleReset(resolved.user.userId, agent.id, commandDeps);
    clearWrapRequested(poolKey);
    await ctx.reply(result.text);
  });

  bot.command('stop', async (ctx) => {
    const chatId = String(ctx.chat.id);
    const resolved = router.resolveByBotAgent(agent.id, chatId);
    if (!resolved) { await ctx.reply(UNREGISTERED_MSG); return; }
    invalidateHumanInput(router.poolKey(resolved.user.userId,resolveInboundRoutingAgent(router,resolved.user.userId,resolved.agent,TELEGRAM_INBOUND_ROUTING).id));
    const routing = resolveInboundRoutingAgent(router, resolved.user.userId, resolved.agent, TELEGRAM_INBOUND_ROUTING);
    const result = await handleStop(resolved.user.userId, routing.id, commandDeps);
    await ctx.reply(result.text);
  });

  bot.command('kill', async (ctx) => {
    const chatId = String(ctx.chat.id);
    const resolved = router.resolveByBotAgent(agent.id, chatId);
    if (!resolved) { await ctx.reply(UNREGISTERED_MSG); return; }
    invalidateHumanInput(router.poolKey(resolved.user.userId,resolveInboundRoutingAgent(router,resolved.user.userId,resolved.agent,TELEGRAM_INBOUND_ROUTING).id));
    const routing = resolveInboundRoutingAgent(router, resolved.user.userId, resolved.agent, TELEGRAM_INBOUND_ROUTING);
    const result = await handleKill(resolved.user.userId, routing.id, commandDeps);
    await ctx.reply(result.text);
  });

  bot.command('restart', async (ctx) => {
    const chatId = String(ctx.chat.id);
    const resolved = router.resolveByBotAgent(agent.id, chatId);
    if (!resolved) { await ctx.reply(UNREGISTERED_MSG); return; }
    invalidateHumanInput(router.poolKey(resolved.user.userId,resolveInboundRoutingAgent(router,resolved.user.userId,resolved.agent,TELEGRAM_INBOUND_ROUTING).id));
    const routing = resolveInboundRoutingAgent(router, resolved.user.userId, resolved.agent, TELEGRAM_INBOUND_ROUTING);
    const result = await handleRestart(resolved.user.userId, routing.id, commandDeps);
    await ctx.reply(result.text);
  });

  bot.command('help', async (ctx) => {
    const result = handleHelp();
    await ctx.reply(result.text);
  });

  bot.command('session', async (ctx) => {
    const chatId = String(ctx.chat.id);
    const resolved = router.resolveByBotAgent(agent.id, chatId);
    if (!resolved) { await ctx.reply(UNREGISTERED_MSG); return; }
    invalidateHumanInput(router.poolKey(resolved.user.userId,resolveInboundRoutingAgent(router,resolved.user.userId,resolved.agent,TELEGRAM_INBOUND_ROUTING).id));
    const routing = resolveInboundRoutingAgent(router, resolved.user.userId, resolved.agent, TELEGRAM_INBOUND_ROUTING);
    const result = handleSession(resolved.user.userId, routing.id, commandDeps);
    await ctx.reply(result.text);
  });

  bot.command('runtime', async (ctx) => {
    const chatId = String(ctx.chat.id);
    const resolved = router.resolveByBotAgent(agent.id, chatId);
    if (!resolved) { await ctx.reply(UNREGISTERED_MSG); return; }
    invalidateHumanInput(router.poolKey(resolved.user.userId,resolveInboundRoutingAgent(router,resolved.user.userId,resolved.agent,TELEGRAM_INBOUND_ROUTING).id));
    const routing = resolveInboundRoutingAgent(router, resolved.user.userId, resolved.agent, TELEGRAM_INBOUND_ROUTING);
    const result = await handleRuntime(resolved.user.userId, routing.id, typeof ctx.match === 'string' ? ctx.match : undefined, commandDeps);
    await ctx.reply(result.text);
  });

  bot.command('yolo', async (ctx) => {
    const chatId = String(ctx.chat.id);
    const resolved = router.resolveByBotAgent(agent.id, chatId);
    if (!resolved) { await ctx.reply(UNREGISTERED_MSG); return; }
    invalidateHumanInput(router.poolKey(resolved.user.userId,resolveInboundRoutingAgent(router,resolved.user.userId,resolved.agent,TELEGRAM_INBOUND_ROUTING).id));
    const routing = resolveInboundRoutingAgent(router, resolved.user.userId, resolved.agent, TELEGRAM_INBOUND_ROUTING);
    const result = await handleYolo(resolved.user.userId, routing.id, typeof ctx.match === 'string' ? ctx.match : undefined, commandDeps);
    await ctx.reply(result.text);
  });

  bot.command('usage', async (ctx) => {
    const chatId = String(ctx.chat.id);
    const resolved = router.resolveByBotAgent(agent.id, chatId);
    if (!resolved) { await ctx.reply(UNREGISTERED_MSG); return; }
    invalidateHumanInput(router.poolKey(resolved.user.userId,resolveInboundRoutingAgent(router,resolved.user.userId,resolved.agent,TELEGRAM_INBOUND_ROUTING).id));
    const routing = resolveInboundRoutingAgent(router, resolved.user.userId, resolved.agent, TELEGRAM_INBOUND_ROUTING);
    const result = handleUsage(resolved.user.userId, routing.id, commandDeps);
    await ctx.reply(result.text);
  });

  bot.command('usage_seal', async (ctx) => {
    const chatId = String(ctx.chat.id);
    const resolved = router.resolveByBotAgent(agent.id, chatId);
    if (!resolved) { await ctx.reply(UNREGISTERED_MSG); return; }
    invalidateHumanInput(router.poolKey(resolved.user.userId,resolveInboundRoutingAgent(router,resolved.user.userId,resolved.agent,TELEGRAM_INBOUND_ROUTING).id));
    const routing = resolveInboundRoutingAgent(router, resolved.user.userId, resolved.agent, TELEGRAM_INBOUND_ROUTING);
    const result = await handleUsageSeal(resolved.user.userId, routing.id, commandDeps);
    await ctx.reply(result.text);
  });

  // --- Scheduler commands (v0.1.0) ---
  // Note: deps.scheduler uses a getter (lazy ref) — check at call time, not registration time
  bot.command('jobs', async (ctx) => {
    if (!deps.scheduler) { await ctx.reply('调度器未就绪。'); return; }
    const chatId = String(ctx.chat.id);
    const resolved = router.resolveByBotAgent(agent.id, chatId);
    if (!resolved) { await ctx.reply(UNREGISTERED_MSG); return; }
    invalidateHumanInput(router.poolKey(resolved.user.userId,resolveInboundRoutingAgent(router,resolved.user.userId,resolved.agent,TELEGRAM_INBOUND_ROUTING).id));
    const result = handleListJobs(agent.id, deps.scheduler);
    await ctx.reply(result);
  });

  bot.command('job_add', async (ctx) => {
    if (!deps.scheduler) { await ctx.reply('调度器未就绪。'); return; }
    const chatId = String(ctx.chat.id);
    const resolved = router.resolveByBotAgent(agent.id, chatId);
    if (!resolved) { await ctx.reply(UNREGISTERED_MSG); return; }
    invalidateHumanInput(router.poolKey(resolved.user.userId,resolveInboundRoutingAgent(router,resolved.user.userId,resolved.agent,TELEGRAM_INBOUND_ROUTING).id));
    const result = handleAddJob(agent.id, agent.workspace, chatId, ctx.match, deps.scheduler);
    await ctx.reply(result);
  });

  bot.command('job_del', async (ctx) => {
    if (!deps.scheduler) { await ctx.reply('调度器未就绪。'); return; }
    const chatId = String(ctx.chat.id);
    const resolved = router.resolveByBotAgent(agent.id, chatId);
    if (!resolved) { await ctx.reply(UNREGISTERED_MSG); return; }
    invalidateHumanInput(router.poolKey(resolved.user.userId,resolveInboundRoutingAgent(router,resolved.user.userId,resolved.agent,TELEGRAM_INBOUND_ROUTING).id));
    const result = handleDelJob(agent.id, ctx.match, deps.scheduler);
    await ctx.reply(result);
  });

  bot.command('job_run', async (ctx) => {
    if (!deps.scheduler) { await ctx.reply('调度器未就绪。'); return; }
    const chatId = String(ctx.chat.id);
    const resolved = router.resolveByBotAgent(agent.id, chatId);
    if (!resolved) { await ctx.reply(UNREGISTERED_MSG); return; }
    invalidateHumanInput(router.poolKey(resolved.user.userId,resolveInboundRoutingAgent(router,resolved.user.userId,resolved.agent,TELEGRAM_INBOUND_ROUTING).id));
    const result = await handleRunJob(agent.id, ctx.match, deps.scheduler);
    await ctx.reply(result);
  });

  bot.command('model', async (ctx) => {
    const chatId = String(ctx.chat.id);
    const resolved = router.resolveByBotAgent(agent.id, chatId);
    if (!resolved) { await ctx.reply(UNREGISTERED_MSG); return; }
    invalidateHumanInput(router.poolKey(resolved.user.userId,resolveInboundRoutingAgent(router,resolved.user.userId,resolved.agent,TELEGRAM_INBOUND_ROUTING).id));
    const routing = resolveInboundRoutingAgent(router, resolved.user.userId, resolved.agent, TELEGRAM_INBOUND_ROUTING);
    const result = await handleModel(resolved.user.userId, routing.id, typeof ctx.match === 'string' ? ctx.match : undefined, commandDeps);
    await replyCommandResult(ctx, result, { chatId, userId: resolved.user.userId, agentId: routing.id });
  });

  bot.command('models', async (ctx) => {
    const chatId = String(ctx.chat.id);
    const resolved = router.resolveByBotAgent(agent.id, chatId);
    if (!resolved) { await ctx.reply(UNREGISTERED_MSG); return; }
    invalidateHumanInput(router.poolKey(resolved.user.userId,resolveInboundRoutingAgent(router,resolved.user.userId,resolved.agent,TELEGRAM_INBOUND_ROUTING).id));
    const routing = resolveInboundRoutingAgent(router, resolved.user.userId, resolved.agent, TELEGRAM_INBOUND_ROUTING);
    const result = await handleModels(
      resolved.user.userId,
      routing.id,
      commandDeps,
      typeof ctx.match === 'string' ? ctx.match : undefined,
    );
    await replyCommandResult(ctx, result);
  });

  bot.command('think', async (ctx) => {
    const chatId = String(ctx.chat.id);
    const resolved = router.resolveByBotAgent(agent.id, chatId);
    if (!resolved) { await ctx.reply(UNREGISTERED_MSG); return; }
    invalidateHumanInput(router.poolKey(resolved.user.userId,resolveInboundRoutingAgent(router,resolved.user.userId,resolved.agent,TELEGRAM_INBOUND_ROUTING).id));
    const routing = resolveInboundRoutingAgent(router, resolved.user.userId, resolved.agent, TELEGRAM_INBOUND_ROUTING);
    const result = await handleThink(resolved.user.userId, routing.id, typeof ctx.match === 'string' ? ctx.match : undefined, commandDeps);
    await replyCommandResult(ctx, result, { chatId, userId: resolved.user.userId, agentId: routing.id });
  });

  bot.command('compact', async (ctx) => {
    const chatId = String(ctx.chat.id);
    const resolved = router.resolveByBotAgent(agent.id, chatId);
    if (!resolved) { await ctx.reply(UNREGISTERED_MSG); return; }
    invalidateHumanInput(router.poolKey(resolved.user.userId,resolveInboundRoutingAgent(router,resolved.user.userId,resolved.agent,TELEGRAM_INBOUND_ROUTING).id));
    const routing = resolveInboundRoutingAgent(router, resolved.user.userId, resolved.agent, TELEGRAM_INBOUND_ROUTING);
    const poolKey = router.poolKey(resolved.user.userId, routing.id);
    const proc = runtime.get(poolKey);
    const configuredRuntime = routing.conversationRuntime?.runtime ?? routing.runtime ?? 'cc';
    if (processRuntimeId(proc, configuredRuntime) === 'pi') {
      const result = await handleCompact(resolved.user.userId, routing.id, typeof ctx.match === 'string' ? ctx.match : undefined, commandDeps);
      await ctx.reply(result.text);
      return;
    }
    const route = resolveGatewayRoute(chatId, resolved.user.userId, `/compact ${typeof ctx.match === 'string' ? ctx.match : ''}`.trim());
    const activeKey = await ensureProcess(chatId, resolved, route);
    if (activeKey) submitTurn(activeKey, { type: 'human', content: `/compact ${typeof ctx.match === 'string' ? ctx.match : ''}`.trim() }, commandSendFailed);
  });

  bot.command('context', async (ctx) => {
    const chatId = String(ctx.chat.id);
    const resolved = router.resolveByBotAgent(agent.id, chatId);
    if (!resolved) { await ctx.reply(UNREGISTERED_MSG); return; }
    invalidateHumanInput(router.poolKey(resolved.user.userId,resolveInboundRoutingAgent(router,resolved.user.userId,resolved.agent,TELEGRAM_INBOUND_ROUTING).id));
    const routing = resolveInboundRoutingAgent(router, resolved.user.userId, resolved.agent, TELEGRAM_INBOUND_ROUTING);
    const poolKey = router.poolKey(resolved.user.userId, routing.id);
    const proc = runtime.get(poolKey);
    const configuredRuntime = routing.conversationRuntime?.runtime ?? routing.runtime ?? 'cc';
    if (processRuntimeId(proc, configuredRuntime) === 'pi') {
      const result = await handleContext(resolved.user.userId, routing.id, commandDeps);
      await ctx.reply(result.text);
      return;
    }
    const route = resolveGatewayRoute(chatId, resolved.user.userId, '/context');
    const activeKey = await ensureProcess(chatId, resolved, route);
    if (activeKey) submitTurn(activeKey, { type: 'human', content: '/context' }, commandSendFailed);
  });

  bot.command('list', async (ctx) => {
    const chatId = String(ctx.chat.id);
    if (!authorizedChatIds.has(chatId)) { await ctx.reply(UNREGISTERED_MSG); return; }
    const resolved = router.resolveByBotAgent(agent.id, chatId);
    if (!resolved) { await ctx.reply(UNREGISTERED_MSG); return; }
    invalidateHumanInput(router.poolKey(resolved.user.userId,resolveInboundRoutingAgent(router,resolved.user.userId,resolved.agent,TELEGRAM_INBOUND_ROUTING).id));
    if (router.getAgents(resolved.user.userId).length <= 1) {
      await ctx.reply('此命令在独立 bot 模式下不可用。每个 bot 对应一个 Agent，发 /help 查看可用命令。');
      return;
    }
    const list = handleList(resolved.user.userId, commandDeps);
    await replyCommandResult(ctx, withAgentPickerUi(list, resolved.user.userId, router, agent));
  });

  bot.command('switch', async (ctx) => {
    const chatId = String(ctx.chat.id);
    if (!authorizedChatIds.has(chatId)) { await ctx.reply(UNREGISTERED_MSG); return; }
    const resolved = router.resolveByBotAgent(agent.id, chatId);
    if (!resolved) { await ctx.reply(UNREGISTERED_MSG); return; }
    invalidateHumanInput(router.poolKey(resolved.user.userId,resolveInboundRoutingAgent(router,resolved.user.userId,resolved.agent,TELEGRAM_INBOUND_ROUTING).id));
    if (router.getAgents(resolved.user.userId).length <= 1) {
      await ctx.reply('此命令在独立 bot 模式下不可用。每个 bot 对应一个 Agent，发 /help 查看可用命令。');
      return;
    }
    const arg = typeof ctx.match === 'string' ? ctx.match.trim() : '';
    const sw = handleSwitch(resolved.user.userId, arg, commandDeps);
    await replyCommandResult(ctx, withAgentPickerUi(sw, resolved.user.userId, router, agent));
  });

  bot.on('callback_query:data', async (ctx) => {
    const data = ctx.callbackQuery.data;
    if (typeof data !== 'string') return;
    const chatId = String(ctx.chat?.id ?? '');
    if (!authorizedChatIds.has(chatId)) {
      await ctx.answerCallbackQuery();
      return;
    }
    const resolved = router.resolveByBotAgent(agent.id, chatId);
    if (!resolved) {
      await ctx.answerCallbackQuery();
      return;
    }
    if (data.startsWith('wp:')) {
      await ctx.answerCallbackQuery();
      await rendering.processAction?.(data, chatId, String(ctx.callbackQuery.message?.message_id ?? ''));
      return;
    }
    if (data.startsWith(RELAY_MENU_CB_PREFIX)) {
      const routing = resolveInboundRoutingAgent(router, resolved.user.userId, resolved.agent, TELEGRAM_INBOUND_ROUTING);
      const scope = { chatId, userId: resolved.user.userId, agentId: routing.id };
      const callback = consumeMenuCallback(data, scope);
      if (!callback) {
        await ctx.answerCallbackQuery({ text: '按钮已过期，请重新发送 /model 或 /think。' });
        return;
      }
      await ctx.answerCallbackQuery();
      const result = callback.action === 'runtime'
        ? await handleRuntime(scope.userId, scope.agentId, callback.arg, commandDeps)
        : callback.action === 'model_scope'
          ? await handleModelScope(scope.userId, scope.agentId, callback.arg, commandDeps)
          : callback.action === 'model'
            ? await handleModel(scope.userId, scope.agentId, callback.arg, commandDeps)
            : await handleThink(scope.userId, scope.agentId, callback.arg, commandDeps);
      await replyCommandResult(ctx, result, scope);
      return;
    }
    if (!data.startsWith(RELAY_SWITCH_CB_PREFIX)) return;
    if (router.getAgents(resolved.user.userId).length <= 1) {
      await ctx.answerCallbackQuery();
      return;
    }
    const targetId = data.slice(RELAY_SWITCH_CB_PREFIX.length);
    if (!targetId) {
      await ctx.answerCallbackQuery();
      return;
    }
    const sw = handleSwitch(resolved.user.userId, targetId, commandDeps);
    const result = withAgentPickerUi(sw, resolved.user.userId, router, agent);
    await ctx.answerCallbackQuery();
    await replyCommandResult(ctx, result);
  });

  // --- Typing indicator management ---

  const TYPING_INTERVAL_MS = 4000; // TG typing action expires after ~5s
  const typingIntervals = new Map<string, ReturnType<typeof setInterval>>();

  function startTyping(poolKey: string, chatId: string): void {
    if (!active) return;
    stopTyping(poolKey);
    // Send immediately, then repeat every 4s
    bot.api.sendChatAction(Number(chatId), 'typing').catch(() => {});
    const interval = setInterval(() => {
      if (!active) return;
      bot.api.sendChatAction(Number(chatId), 'typing').catch(() => {});
    }, TYPING_INTERVAL_MS);
    typingIntervals.set(poolKey, interval);
  }

  function stopTyping(poolKey: string): void {
    const interval = typingIntervals.get(poolKey);
    if (interval) {
      clearInterval(interval);
      typingIntervals.delete(poolKey);
    }
  }

  // --- Track chatId for response routing ---
  const poolKeyToChatId = new Map<string, string>();
  const poolKeyWrapRequestedAt = new Map<string, number>();
  const poolKeyRecoveryRequiredAt = new Map<string, number>();

  /**
   * Hand a turn to the runtime without waiting for its dispatch, so the chat
   * lane moves on. The call fixes the turn's place in its session: the
   * maintenance barrier and the runtime queues keep call order, and a busy
   * session holds the turn until the earlier ones finish.
   */
  function submitTurn(poolKey: string, message: StreamJsonMessage, onFailure: (error: unknown) => Promise<unknown> | void): void {
    let sent: Promise<void>;
    try { sent = runtime.send(poolKey, message); } catch (error) { sent = Promise.reject(error); }
    sent.catch(async (error: unknown) => {
      try { await onFailure(error); } catch { /* the failure notice is best effort */ }
    });
  }

  function processCodexThreadId(proc: ReturnType<CommandRuntime['get']>): string | undefined {
    return proc && 'codexThreadId' in proc ? proc.codexThreadId : undefined;
  }

  function processRuntimeId(proc: ReturnType<CommandRuntime['get']>, fallback: ConversationRuntimeId): ConversationRuntimeId {
    return proc && 'runtime' in proc ? proc.runtime : fallback;
  }

  function persistActiveSession(poolKey: string, runtimeId: ConversationRuntimeId, codexThreadId?: string): void {
    if (!deps.stateManager) return;
    const existing = deps.stateManager.getSession(poolKey);
    const userId = parseConversationPoolKey(poolKey)?.userId ?? '';
    const ag = agentForPoolKey(userId, poolKey);
    deps.stateManager.updateSession(poolKey, {
      poolKey,
      agentId: ag.id,
      runtime: runtimeId,
      sessionId: sessionIdToPersist(poolKey, ag),
      codexThreadId: runtimeId === 'codex' ? codexThreadId ?? existing?.codexThreadId : undefined,
      lastActivity: Date.now(),
      status: existing?.recoveryRequiredAt ? 'recovery-required' : 'active',
    });
  }

  function buildInterruptedResumePrompt(message: string): string {
    return [
      '系统提示：Relay 已重启恢复。',
      '上一条用户消息在重启时被中断，可能没有完整送达或没有完成回复。',
      `中断前的消息内容为：${message}`,
      '请优先继续处理这条消息；如果上下文不足，请简短说明需要用户重发。',
    ].join('\n');
  }

  function buildBusyResumePrompt(): string {
    return [
      '系统提示：Relay 已重启恢复。',
      '重启时你正在处理一个用户回合（具体内容未被记录到 Relay 端，可能在 LLM 思考中或重试窗口内）。',
      '请先用一句话告诉用户已回来，然后基于会话上文继续推进未完成的工作。',
    ].join('\n');
  }

  async function spawnForResumedSession(
    poolKey: string,
    resolved: { user: import('../types.js').UserConfig; agent: AgentConfig },
    route?: RoutePolicyResult,
  ): Promise<ReturnType<CommandRuntime['get']>> {
    const routing = resolveInboundRoutingAgent(router, resolved.user.userId, resolved.agent, TELEGRAM_INBOUND_ROUTING);
    return acquireConversation({
      poolKey, agent: routing, runtime, stateManager: deps.stateManager, channel: 'telegram', intent: 'recovery',
      runtimePreference: route?.runtimePreference, allowedTools: resolveTelegramAllowedTools(routing.allowedTools),
    });
  }

  async function notifyResumedSession(chatId: string, poolKey: string, resolved: { user: import('../types.js').UserConfig; agent: AgentConfig }): Promise<void> {
    if (!active) return;
    const session = deps.stateManager?.getSession(poolKey);
    if (!session) return;
    const now = Date.now();
    const pending = session.pendingMaintenanceTurns?.length ?? 0;
    const since = (session.wasBusyAtShutdown || pending) && session.shutdownInterruptedAt
      ? session.shutdownInterruptedAt : session.lastActivity;
    if (now - since > TG_RESUME_ACTIVE_WINDOW_MS) return;

    poolKeyToChatId.set(poolKey, chatId);
    // Wake judgement: pool.onShutdownInterrupt set wasBusyAtShutdown for every
    // CC process that was busy at SIGTERM. That's the canonical "user is
    // waiting" signal — independent of whether Relay still held a fresh doSend
    // (long-thinking / 429 paths clear lastInFlight before SIGTERM). Turns
    // accepted during the shutdown and never dispatched follow, in order.
    if (!session.wasBusyAtShutdown && !pending) return;
    const interrupted = session.interruptedUserMessage?.trim();

    if (session.wasBusyAtShutdown) {
      const resumeNotice = interrupted
        ? '⚡ Relay 已重启恢复，正在恢复刚才中断的请求。'
        : '⚡ Relay 已重启恢复，继续刚才被打断的工作。';
      await bot.api.sendMessage(Number(chatId), resumeNotice).catch(() => {});
    }
    try {
      const route = resolveGatewayRoute(chatId, resolved.user.userId, interrupted ?? '');
      const proc = await spawnForResumedSession(poolKey, resolved, route);
      const runtimeId = processRuntimeId(proc, session.runtime ?? 'cc');
      persistActiveSession(poolKey, runtimeId, processCodexThreadId(proc));
      if (session.wasBusyAtShutdown) {
        const prompt = interrupted ? buildInterruptedResumePrompt(interrupted) : buildBusyResumePrompt();
        await runtime.send(poolKey, { type: 'human', content: prompt }, { resume: true });
        // Clear the wake signal + best-effort interrupted text in one write.
        deps.stateManager?.updateSession(poolKey, {
          wasBusyAtShutdown: false,
          interruptedUserMessage: undefined,
          lastActivity: Date.now(),
        });
        deps.stateManager?.flush();
      }
      if (deps.stateManager) await replayPendingTurns(deps.stateManager, poolKey, (message, submit) => runtime.send(poolKey, message, submit));
      deps.stateManager?.updateSession(poolKey, { shutdownInterruptedAt: undefined, lastActivity: Date.now() });
      deps.stateManager?.flush();
    } catch (err) {
      console.error({ ts: new Date().toISOString(), level: 'error', agent: agent.id, msg: 'TG notifyResumed failed', poolKey, error: String(err) });
      await bot.api.sendMessage(Number(chatId), '⚠️ Relay 已恢复，但自动恢复刚才请求失败。请重发上一条消息。').catch(() => {});
    }
  }

  function getWrapRequestedAt(poolKey: string): number | undefined {
    return poolKeyWrapRequestedAt.get(poolKey) ?? deps.stateManager?.getSession(poolKey)?.wrapRequestedAt;
  }

  function isRecoveryRequired(poolKey: string): boolean {
    return Boolean(poolKeyRecoveryRequiredAt.get(poolKey) ?? deps.stateManager?.getSession(poolKey)?.recoveryRequiredAt);
  }

  function recoverRequiredFromSessionTail(poolKey: string): boolean {
    if (isRecoveryRequired(poolKey)) return true;
    const existing = deps.stateManager?.getSession(poolKey);
    if (!existing?.sessionId) return false;
    const userId = parseConversationPoolKey(poolKey)?.userId ?? '';
    const ag = agentForPoolKey(userId, poolKey);
    const tail = readFileTail(getClaudeSessionFilePath(ag.workspace, existing.sessionId), SESSION_TAIL_BYTES);
    const reason = detectContextRecoveryReason(tail);
    if (!reason) return false;
    markRecoveryRequired(poolKey, reason);
    return true;
  }

  function markRecoveryRequired(poolKey: string, reason: SessionRecoveryReason): boolean {
    const timestamp = Date.now();
    const existing = deps.stateManager?.getSession(poolKey);
    const alreadyMarked = Boolean(poolKeyRecoveryRequiredAt.get(poolKey) ?? existing?.recoveryRequiredAt);
    poolKeyRecoveryRequiredAt.set(poolKey, timestamp);
    if (!deps.stateManager) return !alreadyMarked;
    const userId = parseConversationPoolKey(poolKey)?.userId ?? '';
    const ag = agentForPoolKey(userId, poolKey);
    deps.stateManager.updateSession(poolKey, {
      poolKey,
      agentId: ag.id,
      runtime: existing?.runtime ?? processRuntimeId(runtime.get(poolKey), ag.runtime ?? 'cc'),
      sessionId: existing?.sessionId ?? ag.sessionId,
      codexThreadId: existing?.codexThreadId ?? processCodexThreadId(runtime.get(poolKey)),
      recoveryRequiredAt: existing?.recoveryRequiredAt ?? timestamp,
      recoveryReason: existing?.recoveryReason ?? reason,
      lastActivity: Date.now(),
      status: 'recovery-required',
    });
    deps.stateManager.flush();
    console.warn({
      ts: new Date().toISOString(),
      level: 'warn',
      agent: agent.id,
      msg: 'Session marked recovery-required',
      poolKey,
      reason,
    });
    return !alreadyMarked;
  }

  function markWrapRequested(poolKey: string, proc: ReturnType<CommandRuntime['get']>): void {
    const timestamp = Date.now();
    poolKeyWrapRequestedAt.set(poolKey, timestamp);
    if (!deps.stateManager) return;
    const userId = parseConversationPoolKey(poolKey)?.userId ?? '';
    const ag = agentForPoolKey(userId, poolKey);
    deps.stateManager.updateSession(poolKey, {
      poolKey,
      agentId: ag.id,
      runtime: processRuntimeId(proc, ag.runtime ?? 'cc'),
      sessionId: sessionIdToPersist(poolKey, ag),
      codexThreadId: processCodexThreadId(proc),
      wrapRequestedAt: timestamp,
      lastActivity: Date.now(),
      status: 'active',
    });
    deps.stateManager.flush();
  }

  function markHardWrapRequested(poolKey: string, reason: SessionRecoveryReason): void {
    const timestamp = Date.now();
    poolKeyWrapRequestedAt.set(poolKey, timestamp);
    poolKeyRecoveryRequiredAt.set(poolKey, timestamp);
    if (!deps.stateManager) return;
    const existing = deps.stateManager.getSession(poolKey);
    const proc = runtime.get(poolKey);
    const userId = parseConversationPoolKey(poolKey)?.userId ?? '';
    const ag = agentForPoolKey(userId, poolKey);
    deps.stateManager.updateSession(poolKey, {
      poolKey,
      agentId: ag.id,
      runtime: existing?.runtime ?? processRuntimeId(proc, ag.runtime ?? 'cc'),
      sessionId: existing?.sessionId ?? ag.sessionId,
      codexThreadId: existing?.codexThreadId ?? processCodexThreadId(proc),
      wrapRequestedAt: timestamp,
      recoveryRequiredAt: existing?.recoveryRequiredAt ?? timestamp,
      recoveryReason: existing?.recoveryReason ?? reason,
      lastActivity: Date.now(),
      status: 'recovery-required',
    });
    deps.stateManager.flush();
  }

  function clearWrapRequested(poolKey: string): void {
    poolKeyWrapRequestedAt.delete(poolKey);
    poolKeyRecoveryRequiredAt.delete(poolKey);
  }

  function clearBlockedWrapRequest(poolKey: string): void {
    if (!getWrapRequestedAt(poolKey)) return;
    poolKeyWrapRequestedAt.delete(poolKey);
    deps.stateManager?.updateSession(poolKey, { wrapRequestedAt: undefined });
    deps.stateManager?.flush();
  }

  // --- Pre-populate chatId mappings from router config (F1-P0-004) ---

  for (const user of router.getAllUsers()) {
    if (!user.chatId) continue;
    for (const ag of user.agents) {
      if (ag.botToken === agent.botToken) {
        poolKeyToChatId.set(router.poolKey(user.userId, ag.id), user.chatId);
      }
    }
  }

  // --- Text Message Handler ---

  bot.on('message:text', async (ctx) => {
    const chatId = String(ctx.chat.id);
    const resolved = router.resolveByBotAgent(agent.id, chatId);
    console.log({ ts: new Date().toISOString(), level: 'debug', msg: 'TG message', agent: agent.id, chatId, text: ctx.message.text.slice(0, 50) });

    if (!resolved) { await ctx.reply(UNREGISTERED_MSG); return; }
    invalidateHumanInput(router.poolKey(resolved.user.userId,resolveInboundRoutingAgent(router,resolved.user.userId,resolved.agent,TELEGRAM_INBOUND_ROUTING).id));
    const route = resolveGatewayRoute(chatId, resolved.user.userId, ctx.message.text);

    const { user } = resolved;
    const routing = resolveInboundRoutingAgent(router, user.userId, resolved.agent, TELEGRAM_INBOUND_ROUTING);
    const poolKey = router.poolKey(user.userId, routing.id);
    const text = ctx.message.text;

    // 切片1 入站埋点：一条来自已注册用户的 TG 文本请求抵达。旁路观测,dark-deploy 开关控制,
    // 全隔离永不抛（traceInboundRequest 内部已 try/catch）。chatId 只取前缀,不落全量标识。
    traceInboundRequest({
      channel: 'telegram',
      agent: routing.id,
      session: poolKey,
      who: chatId.slice(0, 8),
      detail: { msgType: 'text' },
    });

    // Store chatId for response routing
    poolKeyToChatId.set(poolKey, chatId);

    if (hasWrapEvidence(text) && recoverRequiredFromSessionTail(poolKey)) {
      markHardWrapRequested(poolKey, deps.stateManager?.getSession(poolKey)?.recoveryReason ?? 'wrap-cannot-run');
      await ctx.reply(HARD_WRAP_MESSAGE);
      return;
    }

    // Persist authenticated participation before admission/spawn can fail.
    // The proof follows this message object, never a mutable latest-input lookup.
    const wrapSkillPath = isExactWrapCommand(text) ? resolveWrapSkillPath() : undefined;
    if (isExactWrapCommand(text) && !wrapSkillPath) {
      await ctx.reply(WRAP_SKILL_UNAVAILABLE_MESSAGE);
      return;
    }
    const linkedText = telegramTextWithLinks(text, ctx.message.entities);
    const content = isExactWrapCommand(text) ? expandWrapCommandPrompt(text, wrapSkillPath) : buildReplyContextPrompt(
      expandWrapCommandPrompt(linkedText),
      extractTelegramReplyContext((ctx.message as Record<string, unknown>).reply_to_message));
    const eventId = ctx.update?.update_id;
    const source = eventId === undefined ? undefined : {channel:'telegram' as const,eventId:String(eventId),humanRef:user.userId,message:text};
    let outgoing = annotatedHumanMessage(content,source,poolKey);

    let proc: Awaited<ReturnType<typeof acquireConversation>>;
    try {
      proc = await acquireConversation({
        poolKey, agent: routing, runtime, stateManager: deps.stateManager, channel: 'telegram', intent: 'inbound',
        runtimePreference: route?.runtimePreference, allowedTools: resolveTelegramAllowedTools(routing.allowedTools),
      });
    } catch (error) {
      console.error({ ts: new Date().toISOString(), level: 'error', agent: agent.id,
        msg: 'Failed to acquire conversation runtime', runtime: routing.runtime ?? 'cc', error: String(error) });
      await ctx.reply(runtimeEntryFailureMessage(error));
      return;
    }

    // Persist active sessionId to state (survives Relay restart)
    persistActiveSession(poolKey, processRuntimeId(proc, routing.runtime ?? 'cc'), processCodexThreadId(proc));

    outgoing = await attachCurrentLocalFiles(ctx, outgoing, text, ctx.message.entities, user, processRuntimeId(proc, routing.runtime ?? 'cc'));

    const wrapRequested = hasWrapEvidence(text);
    // A runtime may emit its final response before send() acknowledges the
    // turn. Record the request first so a blocked final can clear it once.
    if (wrapRequested) markWrapRequested(poolKey, proc);
    submitTurn(poolKey, outgoing, async err => {
      if (wrapRequested) clearBlockedWrapRequest(poolKey);
      console.error({ ts: new Date().toISOString(), level: 'error', agent: agent.id, msg: 'Failed to send to conversation runtime', runtime: routing.runtime ?? 'cc', error: String(err) });
      await ctx.reply('\u26A0\uFE0F \u6D88\u606F\u53D1\u9001\u5931\u8D25\uFF0C\u8BF7\u7A0D\u540E\u91CD\u8BD5\u3002');
    });
  });

  // --- Media helpers ---

  /** Ensure conversation runtime process is alive, spawn if needed. Returns poolKey or null on failure. */
  async function ensureProcess(chatId: string, resolved: { user: import('../types.js').UserConfig; agent: AgentConfig }, route?: RoutePolicyResult, onFailure?:(error:unknown)=>void): Promise<string | null> {
    const routing = resolveInboundRoutingAgent(router, resolved.user.userId, resolved.agent, TELEGRAM_INBOUND_ROUTING);
    const poolKey = router.poolKey(resolved.user.userId, routing.id);
    poolKeyToChatId.set(poolKey, chatId);

    let proc: Awaited<ReturnType<typeof acquireConversation>>;
    try {
      proc = await acquireConversation({
        poolKey, agent: routing, runtime, stateManager: deps.stateManager, channel: 'telegram', intent: 'inbound',
        runtimePreference: route?.runtimePreference, allowedTools: resolveTelegramAllowedTools(routing.allowedTools),
      });
    } catch (error) {
      onFailure?.(error);
      return null;
    }
    persistActiveSession(poolKey, processRuntimeId(proc, routing.runtime ?? 'cc'), processCodexThreadId(proc));
    return poolKey;
  }

  async function attachCurrentLocalFiles(ctx: { chat: { id: number; type: string }; from?: { id: number }; reply: (text: string) => Promise<unknown> },
    message: StreamJsonMessage, text: string, entities: unknown, user: UserConfig, runtimeId: ConversationRuntimeId): Promise<StreamJsonMessage> {
    const urls = telegramFileLinks(text, entities);
    if (!urls.length) return message;
    try {
      if (ctx.chat.type !== 'private' || ctx.from?.id !== ctx.chat.id || String(ctx.from.id) !== user.chatId) {
        throw new Error('本地文件链接仅支持已绑定用户的私聊，请直接发送该文件。');
      }
      return appendTelegramLocalFiles(message, urls, user.telegramLocalFileRoots, runtimeId);
    } catch (error) {
      const warning = `链接读取失败：${error instanceof Error ? error.message : '请直接发送该文件。'}`;
      const original = typeof message.content === 'string' ? [{ type: 'text', text: message.content }] : message.content as unknown[];
      message.content = [{ type: 'text', text: `[Relay 附件结果：${warning} 本地文件未附加；请仅据已收到的内容回答，不要用同名文件替代。]` }, ...original];
      // A link failure (or a failure to send its notice) must not discard a valid photo/text turn.
      await ctx.reply(`⚠️ ${warning}`).catch(() => {});
      return message;
    }
  }

  /** Download a TG file and return base64 + media type. */
  async function downloadTGFile(fileId: string, fallbackMime: string): Promise<{ base64: string; mediaType: string }> {
    const file = await getTelegramFile(bot.api, fileId);
    const fileUrl = `https://api.telegram.org/file/bot${agent.botToken}/${file.file_path}`;
    const buffer = await downloadTelegramFile(fileUrl);

    const ext = file.file_path?.split('.').pop()?.toLowerCase() ?? '';
    const mimeMap: Record<string, string> = {
      jpg: 'image/jpeg', jpeg: 'image/jpeg', png: 'image/png',
      gif: 'image/gif', webp: 'image/webp',
      ogg: 'audio/ogg', oga: 'audio/ogg', mp3: 'audio/mpeg',
      m4a: 'audio/mp4', wav: 'audio/wav', flac: 'audio/flac',
    };

    return { base64: buffer.toString('base64'), mediaType: mimeMap[ext] ?? fallbackMime };
  }

  /** Download a TG file and return raw buffer. */
  async function downloadTGFileRaw(fileId: string): Promise<Buffer> {
    const file = await getTelegramFile(bot.api, fileId);
    const fileUrl = `https://api.telegram.org/file/bot${agent.botToken}/${file.file_path}`;
    return downloadTelegramFile(fileUrl);
  }

  // --- Photo Handler ---

  bot.on('message:photo', async (ctx) => {
    const chatId = String(ctx.chat.id);
    const resolved = router.resolveByBotAgent(agent.id, chatId);
    if (!resolved) { await ctx.reply(UNREGISTERED_MSG); return; }
    invalidateHumanInput(router.poolKey(resolved.user.userId,resolveInboundRoutingAgent(router,resolved.user.userId,resolved.agent,TELEGRAM_INBOUND_ROUTING).id));
    const route = resolveGatewayRoute(chatId, resolved.user.userId, ctx.message.caption);

    let startupError:unknown;
    const poolKey = await ensureProcess(chatId, resolved, route,error=>{startupError=error;});
    if (!poolKey) { await ctx.reply(runtimeEntryFailureMessage(startupError)); return; }

    try {
      const photos = ctx.message.photo;
      const largest = photos[photos.length - 1];
      const persisted = deps.stateManager?.getSession(poolKey);
      const ag = agentForPoolKey(resolved.user.userId, poolKey);
      const runtimeId = processRuntimeId(runtime.get(poolKey), persisted?.runtime ?? ag.runtime ?? 'cc');
      const downloaded = await downloadTGFile(largest.file_id, 'image/jpeg');
      let message = telegramImageMessage(Buffer.from(downloaded.base64, 'base64'), downloaded.mediaType, runtimeId,
        telegramTextWithLinks(ctx.message.caption ?? '', ctx.message.caption_entities));

      // Without a caption the person's input is the picture itself.
      const rawWords = ctx.message.caption || '[图片]';
      const words = telegramTextWithLinks(rawWords, ctx.message.caption_entities);
      message = await attachCurrentLocalFiles(ctx, message, ctx.message.caption ?? '', ctx.message.caption_entities, resolved.user, runtimeId);
      submitTurn(poolKey, annotatedMediaMessage(message, words,
        telegramSource(ctx, resolved.user.userId, rawWords), poolKey), photoFailed);
    } catch (err) {
      await photoFailed(err);
    }
    async function photoFailed(err: unknown): Promise<void> {
      console.error({ ts: new Date().toISOString(), level: 'error', agent: agent.id, msg: 'Failed to process photo', error: String(err) });
      await ctx.reply(telegramPhotoFailureMessage(err));
    }
  });

  // --- Voice / Audio Handler ---

  bot.on('message:voice', async (ctx) => {
    const chatId = String(ctx.chat.id);
    console.log({ ts: new Date().toISOString(), level: 'info', agent: agent.id, msg: 'Voice message received', chatId, fileId: ctx.message.voice.file_id });
    const resolved = router.resolveByBotAgent(agent.id, chatId);
    if (!resolved) { await ctx.reply(UNREGISTERED_MSG); return; }
    invalidateHumanInput(router.poolKey(resolved.user.userId,resolveInboundRoutingAgent(router,resolved.user.userId,resolved.agent,TELEGRAM_INBOUND_ROUTING).id));
    const route = resolveGatewayRoute(chatId, resolved.user.userId);

    let startupError:unknown;
    const poolKey = await ensureProcess(chatId, resolved, route,error=>{startupError=error;});
    if (!poolKey) { await ctx.reply(runtimeEntryFailureMessage(startupError)); return; }

    try {
      const buffer = await downloadTGFileRaw(ctx.message.voice.file_id);
      console.log({ ts: new Date().toISOString(), level: 'info', agent: agent.id, msg: 'Voice downloaded, transcribing', bytes: buffer.length });
      const token = process.env['CLAUDE_CODE_OAUTH_TOKEN'] ?? '';
      const transcript = await transcribeAudio(buffer, token);
      console.log({ ts: new Date().toISOString(), level: 'info', agent: agent.id, msg: 'Voice transcribed', transcript });

      if (!transcript.trim()) {
        await ctx.reply('⚠️ 未能识别语音内容，请重试。');
        return;
      }

      submitTurn(poolKey, annotatedMediaMessage({ type: 'human', content: transcript }, transcript,
        telegramSource(ctx, resolved.user.userId, transcript), poolKey), voiceFailed);
    } catch (err) {
      await voiceFailed(err);
    }
    async function voiceFailed(err: unknown): Promise<void> {
      console.error({ ts: new Date().toISOString(), level: 'error', agent: agent.id, msg: 'Failed to process voice', error: String(err) });
      await ctx.reply('\u26A0\uFE0F \u8BED\u97F3\u5904\u7406\u5931\u8D25\uFF0C\u8BF7\u7A0D\u540E\u91CD\u8BD5\u3002');
    }
  });

  bot.on('message:audio', async (ctx) => {
    const chatId = String(ctx.chat.id);
    const resolved = router.resolveByBotAgent(agent.id, chatId);
    if (!resolved) { await ctx.reply(UNREGISTERED_MSG); return; }
    invalidateHumanInput(router.poolKey(resolved.user.userId,resolveInboundRoutingAgent(router,resolved.user.userId,resolved.agent,TELEGRAM_INBOUND_ROUTING).id));
    const route = resolveGatewayRoute(chatId, resolved.user.userId, ctx.message.caption);

    let startupError:unknown;
    const poolKey = await ensureProcess(chatId, resolved, route,error=>{startupError=error;});
    if (!poolKey) { await ctx.reply(runtimeEntryFailureMessage(startupError)); return; }

    try {
      const buffer = await downloadTGFileRaw(ctx.message.audio.file_id);
      const token = process.env['CLAUDE_CODE_OAUTH_TOKEN'] ?? '';
      const transcript = await transcribeAudio(buffer, token);

      let text = transcript;
      if (ctx.message.caption) {
        text += `\n\n${ctx.message.caption}`;
      }

      submitTurn(poolKey, annotatedMediaMessage({ type: 'human', content: text }, text,
        telegramSource(ctx, resolved.user.userId, text), poolKey), audioFailed);
    } catch (err) {
      await audioFailed(err);
    }
    async function audioFailed(err: unknown): Promise<void> {
      console.error({ ts: new Date().toISOString(), level: 'error', agent: agent.id, msg: 'Failed to process audio', error: String(err) });
      await ctx.reply('\u26A0\uFE0F \u97F3\u9891\u5904\u7406\u5931\u8D25\uFF0C\u8BF7\u7A0D\u540E\u91CD\u8BD5\u3002');
    }
  });

  // --- Document Handler (images-as-file, PDF, text) ---

  bot.on('message:document', async (ctx) => {
    const chatId = String(ctx.chat.id);
    const resolved = router.resolveByBotAgent(agent.id, chatId);
    if (!resolved) { await ctx.reply(UNREGISTERED_MSG); return; }
    invalidateHumanInput(router.poolKey(resolved.user.userId,resolveInboundRoutingAgent(router,resolved.user.userId,resolved.agent,TELEGRAM_INBOUND_ROUTING).id));
    const route = resolveGatewayRoute(chatId, resolved.user.userId, ctx.message.caption);

    const doc = ctx.message.document;
    const mime = doc.mime_type ?? '';

    // Determine content block type from MIME
    let blockType: string;
    if (mime.startsWith('image/')) {
      blockType = 'image';
    } else if (mime === 'application/pdf') {
      blockType = 'document';
    } else if (mime.startsWith('text/')) {
      blockType = 'text_file';
    } else {
      await ctx.reply(`暂不支持该文件类型 (${mime || '未知'})，支持图片、PDF、文本文件。`);
      return;
    }

    let startupError:unknown;
    const poolKey = await ensureProcess(chatId, resolved, route,error=>{startupError=error;});
    if (!poolKey) { await ctx.reply(runtimeEntryFailureMessage(startupError)); return; }

    try {
      const { base64, mediaType } = await downloadTGFile(doc.file_id, mime);

      const runtimeId = processRuntimeId(runtime.get(poolKey), deps.stateManager?.getSession(poolKey)?.runtime ?? resolved.agent.runtime ?? 'cc');
      const rawWords = ctx.message.caption || (doc.file_name ? `[文件: ${doc.file_name}]` : '[文件]');
      const words = telegramTextWithLinks(rawWords, ctx.message.caption_entities);
      if (blockType === 'image') {
        const message = await attachCurrentLocalFiles(ctx, telegramImageMessage(Buffer.from(base64, 'base64'), mediaType, runtimeId, words),
          ctx.message.caption ?? '', ctx.message.caption_entities, resolved.user, runtimeId);
        submitTurn(poolKey, annotatedMediaMessage(message, words, telegramSource(ctx, resolved.user.userId, rawWords), poolKey), documentFailed);
        return;
      }

      let contentBlocks: unknown[];
      if (blockType === 'text_file') {
        // Decode text file and send as text content
        const text = Buffer.from(base64, 'base64').toString('utf-8');
        const label = doc.file_name ? `[文件: ${doc.file_name}]\n` : '';
        contentBlocks = [{ type: 'text', text: label + text }];
      } else {
        contentBlocks = [
          { type: blockType, source: { type: 'base64', media_type: mediaType, data: base64 } },
        ];
      }

      const message = await attachCurrentLocalFiles(ctx, { type: 'human', content: contentBlocks },
        ctx.message.caption ?? '', ctx.message.caption_entities, resolved.user, runtimeId);
      submitTurn(poolKey, annotatedMediaMessage(message, words,
        telegramSource(ctx, resolved.user.userId, rawWords), poolKey), documentFailed);
    } catch (err) {
      await documentFailed(err);
    }
    async function documentFailed(err: unknown): Promise<void> {
      console.error({ ts: new Date().toISOString(), level: 'error', agent: agent.id, msg: 'Failed to process document', error: String(err) });
      await ctx.reply('\u26A0\uFE0F \u6587\u4EF6\u5904\u7406\u5931\u8D25\uFF0C\u8BF7\u7A0D\u540E\u91CD\u8BD5\u3002');
    }
  });

  // --- Unsupported media catch-all (video, stickers, etc.) ---

  bot.on('message', async (ctx) => {
    if (ctx.message.text) return;
    const chatId = String(ctx.chat.id);
    const resolved = router.resolveByBotAgent(agent.id, chatId);
    if (!resolved) { await ctx.reply(UNREGISTERED_MSG); return; }
    invalidateHumanInput(router.poolKey(resolved.user.userId,resolveInboundRoutingAgent(router,resolved.user.userId,resolved.agent,TELEGRAM_INBOUND_ROUTING).id));

    const mediaType = ctx.message.video ? '视频'
      : ctx.message.sticker ? '贴纸'
      : '该类型消息';

    await ctx.reply(`暂不支持${mediaType}，请发送文字消息。`);
  });

  // --- Response routing: conversation runtime → TG ---
  const suppressGenericExitNotice = new Map<string, NodeJS.Timeout>();
  const rendering = createTelegramRenderingSink(bot, { processScope: agent.id });
  const streamSequence = new Map<string, number>();
  const activity = new TurnActivityCounter();
  const turnSequence = new Map<string, number>();
  const reportedRuntimeErrors = new Set<string>();

  function suppressNextGenericExitNotice(poolKey: string): void {
    const existing = suppressGenericExitNotice.get(poolKey);
    if (existing) clearTimeout(existing);
    const timer = setTimeout(() => {
      suppressGenericExitNotice.delete(poolKey);
    }, 30_000);
    timer.unref?.();
    suppressGenericExitNotice.set(poolKey, timer);
  }

  // Relay sends and runtime-notification turns (CC woke on a finished
  // background task) both open a fresh rendering turn here.
  runtime.on('turn-accepted', (poolKey: string, meta?: TurnAcceptedMeta) => {
    if (!active) return;
    if (!poolKeyToChatId.has(poolKey)) return;
    reportedRuntimeErrors.delete(poolKey);
    const chatId = poolKeyToChatId.get(poolKey);
    if (!chatId) return;
    const turn = (turnSequence.get(poolKey) ?? 0) + 1;
    turnSequence.set(poolKey, turn);
    rendering.begin(poolKey, `${agent.id}/${poolKey}/turn:${turn}`, { replyLead: turnReplyLead(meta) });
    streamSequence.delete(poolKey);
    activity.begin(poolKey);
    startTyping(poolKey, chatId);
    void rendering.status(poolKey, Number(chatId), turnAcceptedStatusText(meta), `${poolKey}:accepted`);
  });

  runtime.on('turn-effect', (poolKey: string) => {
    if (!active) return;
    const chatId = poolKeyToChatId.get(poolKey);
    if (!chatId) return;
    const progress = activity.advance(poolKey);
    if (progress?.text) {
      void rendering.activity(poolKey, Number(chatId), progress.text, `${poolKey}:activity:${progress.step}`);
    }
  });

  runtime.on('stream-delta', (poolKey: string, delta: string, correlationId?: string) => {
    if (!active) return;
    const chatId = poolKeyToChatId.get(poolKey);
    if (!chatId) return;
    stopTyping(poolKey);
    const sequence = (streamSequence.get(poolKey) ?? 0) + 1;
    streamSequence.set(poolKey, sequence);
    void rendering.delta(poolKey, Number(chatId), delta, `${correlationId ?? poolKey}:delta:${sequence}`);
  });

  runtime.on('response', (poolKey: string, msg: StreamJsonMessage) => {
    if (!active) return;
    if (!poolKeyToChatId.has(poolKey)) return;
    if (msg.delivery_visibility === 'control') return;
    console.log({ ts: new Date().toISOString(), level: 'debug', agent: agent.id, msg: 'runtime response', type: msg.type, poolKey });

    if (msg.type === 'work_process') {
      const address = poolKeyToChatId.get(poolKey);
      if (address) void rendering.process?.(poolKey, Number(address), msg.process_event as WorkProcessEvent);
      return;
    }
    if (msg.type === 'result') {
      if (msg.is_error === true) void rendering.fail?.(poolKey);
      else reportedRuntimeErrors.delete(poolKey);
      stopTyping(poolKey);
      activity.end(poolKey);
      const resultChatId = poolKeyToChatId.get(poolKey);
      if (resultChatId) {
        const process = runtime.get(poolKey);
        const runtimeId = process ? ('runtime' in process ? process.runtime : 'cc') : agent.conversationRuntime?.runtime ?? agent.runtime;
        const notice = publicRuntimeResultNotice(msg, runtimeId);
        if (notice) {
          console.error({ ts: new Date().toISOString(), level: 'error', agent: agent.id,
            poolKey, msg: 'Conversation runtime failed result', error: notice });
          reportedRuntimeErrors.add(poolKey);
          suppressNextGenericExitNotice(poolKey);
        }
        void rendering.result(poolKey, Number(resultChatId)).catch(() => {}).then(() => {
          if (notice) return bot.api.sendMessage(Number(resultChatId), notice).then(() => undefined);
        }).catch(() => {});
      }
      return;
    }

    if (msg.type !== 'assistant') return;

    // Stop typing as soon as we have an actual response to send
    stopTyping(poolKey);

    const msgObj = msg.message as { stop_reason?: string | null; content?: Array<{ type: string; text?: string }> } | undefined;

    // Only forward records that contain visible text in message.content.
    // Intermediate tool-call records have only tool_use blocks here; the top-level
    // msg.content fallback is intentionally removed to avoid leaking reasoning narration.
    const textContent = Array.isArray(msgObj?.content)
      ? msgObj.content.filter(c => c.type === 'text' && c.text).map(c => c.text).join('')
      : '';

    if (!textContent) {
      console.log({ ts: new Date().toISOString(), level: 'debug', agent: agent.id, msg: 'empty text content', raw: JSON.stringify(msg).slice(0, 200) });
      return;
    }

    if ((msg.delivery_phase === 'final' || msgObj?.stop_reason === 'end_turn') && hasWrapBlockerCode(textContent)) {
      clearBlockedWrapRequest(poolKey);
    }

    const recoveryReason = detectContextRecoveryReason(textContent);
    if (recoveryReason) {
      const newlyMarked = markRecoveryRequired(poolKey, recoveryReason);
      suppressNextGenericExitNotice(poolKey);
      if (newlyMarked) {
        const chatId = poolKeyToChatId.get(poolKey);
        if (chatId) {
          bot.api.sendMessage(Number(chatId), RECOVERY_HINT_MESSAGE).catch(() => {});
        }
      }
    }

    // Strip CC internal diagnostic noise
    const cleanedText = stripCCNoise(textContent);
    if (!cleanedText) return;

    const chatId = poolKeyToChatId.get(poolKey);
    if (!chatId) {
      console.warn({ ts: new Date().toISOString(), level: 'warn', agent: agent.id, msg: 'No chatId for poolKey', poolKey });
      return;
    }

    const stopReason = msgObj?.stop_reason;
    const explicitPhase = msg.delivery_phase;
    const phase = explicitPhase === 'commentary' || explicitPhase === 'final' || explicitPhase === 'snapshot'
      ? explicitPhase
      : stopReason === 'end_turn'
        ? 'final'
        : 'commentary';
    void rendering.assistant(poolKey, Number(chatId), cleanedText, phase, typeof msg.event_id === 'string' ? msg.event_id : undefined);
  });


  // Tool-output file delivery: send files the runtime placed in this
  // session's own outbox (validated against this poolKey) to this chat.
  runtime.on('tool-files', (poolKey: string, filePaths: string[], workspace: string) => {
    if (!active) return;
    const chatId = poolKeyToChatId.get(poolKey);
    if (!chatId) return;
    void deliverTelegramToolFiles(poolKey, chatId, workspace, filePaths).catch((err) => {
      console.error({ ts: new Date().toISOString(), level: 'error', agent: agent.id, msg: 'deliverToolFiles failed', error: String(err) });
    });
  });

  async function deliverTelegramToolFiles(poolKey: string, chatId: string, workspace: string, filePaths: string[]): Promise<void> {
    for (const filePath of filePaths) {
      const ownership = validateToolFileOutboxPath(poolKey, workspace, filePath);
      if (!ownership.ok) {
        console.warn({ ts: new Date().toISOString(), level: 'warn', agent: agent.id, channel: 'telegram', msg: 'Tool file delivery rejected', path: filePath, reason: ownership.error });
        continue;
      }
      const check = checkFileForDelivery(ownership.path);
      if (!check.ok) {
        console.warn({ ts: new Date().toISOString(), level: 'warn', agent: agent.id, channel: 'telegram', msg: 'Tool file delivery skipped', path: ownership.path, reason: check.error });
        continue;
      }
      const fileName = basename(ownership.path);
      const receipt = await deliverTelegramDocument({
        id: `delivery:telegram:${agent.id}:toolfile:${Date.now()}:${Buffer.from(ownership.path).toString('base64').slice(0, 16)}`,
        correlationId: `telegram:${agent.id}:${chatId}:${Date.now()}`,
        channel: 'telegram',
        address: normalizeTelegramChatAddress(chatId, agent.id),
        payload: { kind: 'document', path: ownership.path },
      }, bot);
      if (receipt.status === 'sent') {
        console.log({ ts: new Date().toISOString(), level: 'info', agent: agent.id, channel: 'telegram', poolKey, msg: 'Tool file delivered', path: ownership.path, size: check.size });
        // Only validated outbox-owned files are disposable after delivery.
        try { rmSync(ownership.path, { force: true }); } catch { /* best-effort */ }
      } else {
        console.warn({ ts: new Date().toISOString(), level: 'warn', agent: agent.id, channel: 'telegram', poolKey, msg: 'Tool file delivery failed', path: ownership.path, error: receipt.error });
        await bot.api.sendMessage(Number(chatId), `\u26A0\uFE0F \u6587\u4EF6\u53D1\u9001\u5931\u8D25: ${fileName}`).catch(() => {});
      }
    }
  }

  // Catch pool errors to prevent unhandled error crash
  runtime.on('error', (poolKey: string, error: Error) => {
    if (!active) return;
    if (!poolKeyToChatId.has(poolKey)) return;
    stopTyping(poolKey);
    activity.end(poolKey);
    void rendering.fail?.(poolKey);
    const chatId = poolKeyToChatId.get(poolKey);
    const process = runtime.get(poolKey);
    const runtimeId = process ? ('runtime' in process ? process.runtime : 'cc') : agent.conversationRuntime?.runtime ?? agent.runtime;
    const notice = publicRuntimeExceptionNotice(runtimeId, error);
    console.error({ ts: new Date().toISOString(), level: 'error', agent: agent.id,
      poolKey, msg: 'Conversation runtime error', error: notice });
    if (reportedRuntimeErrors.has(poolKey)) return;
    reportedRuntimeErrors.add(poolKey);
    suppressNextGenericExitNotice(poolKey);
    if (chatId) void bot.api.sendMessage(Number(chatId), notice).catch(() => {});
  });

  runtime.on('turn-warning', activeTelegramListener(() => active, (poolKey: string, currentTurn: number, maxTurns: number) => {
    if (!poolKeyToChatId.has(poolKey)) return;
    const chatId = poolKeyToChatId.get(poolKey);
    if (!chatId) return;
    const userId = parseConversationPoolKey(poolKey)?.userId ?? '';
    const label = resolveAgentFromPoolKey(router, userId, poolKey) ?? agent;
    bot.api.sendMessage(Number(chatId),
      `⚠️ ${label.name} 已用 ${currentTurn}/${maxTurns} 轮次（80%），即将达到上限。如需继续请发新消息。`
    ).catch(() => {});
  }));

  runtime.on('exit', (poolKey: string, code: number | null, meta?: PoolExitMeta) => {
    if (!active) return;
    if (!poolKeyToChatId.has(poolKey)) return;
    stopTyping(poolKey);
    activity.end(poolKey);
    const suppressTimer = suppressGenericExitNotice.get(poolKey);
    if (suppressTimer) {
      clearTimeout(suppressTimer);
      suppressGenericExitNotice.delete(poolKey);
      return;
    }
    // code 0 = normal exit, null = no code, 143 = SIGTERM (idle cleanup)
    if (code === 0 || code === null || code === 143) return;
    void rendering.fail?.(poolKey);
    if (meta?.suppressGenericRecoveryToast) return;

    const chatId = poolKeyToChatId.get(poolKey);
    if (!chatId) return;

    bot.api.sendMessage(Number(chatId), '\u26A0\uFE0F \u7CFB\u7EDF\u5F00\u5C0F\u5DEE\u4E86\uFF0C\u6B63\u5728\u6062\u590D\u4E2D...\u8BF7\u7A0D\u540E\u91CD\u8BD5\u3002').catch(() => {});
  });

  runtime.on('crash-retry', (poolKey: string) => {
    if (!active) return;
    if (!poolKeyToChatId.has(poolKey)) return;
    const chatId = poolKeyToChatId.get(poolKey);
    if (!chatId) return;

    bot.api.sendMessage(Number(chatId), TELEGRAM_TURN_STATUS_TEXT.retrying).catch(() => {});
  });

  runtime.on('provider-fallback', (poolKey: string, event: import('../types.js').ProviderFallbackEvent) => {
    if (!active) return;
    if (!poolKeyToChatId.has(poolKey)) return;
    const chatId = poolKeyToChatId.get(poolKey);
    if (!chatId) return;
    const userId = parseConversationPoolKey(poolKey)?.userId ?? '';
    const label = resolveAgentFromPoolKey(router, userId, poolKey) ?? agent;
    const reason = event.reason ? `，原因：${truncateReason(event.reason)}` : '';
    const lines = [
      `⚠️ ${label.name} 模型已从 ${event.fromModel} 切换到 ${event.toModel}${reason}。`,
      '已自动开启新对话继续处理，无需重发消息。',
    ];
    bot.api.sendMessage(Number(chatId), lines.join('\n')).catch(() => {});
  });

  runtime.onRuntimeFallback?.((event) => {
    if (!active) return;
    if (!poolKeyToChatId.has(event.tenantId)) return;
    const chatId = poolKeyToChatId.get(event.tenantId);
    if (!chatId) return;
    const userId = parseConversationPoolKey(event.tenantId)?.userId ?? '';
    const label = resolveAgentFromPoolKey(router, userId, event.tenantId) ?? agent;
    console.warn({
      ts: new Date().toISOString(),
      level: 'warn',
      agent: agent.id,
      msg: 'Conversation runtime fallback',
      poolKey: event.tenantId,
      from: event.from,
      to: event.to,
      attemptIndex: event.attemptIndex,
      reason: event.reason,
    });
    const lines = [
      `⚠️ ${label.name} 主推理通道暂时受限，已自动切到备用通道继续本轮。`,
      '无需重发消息，回复可能稍慢；本会话剩余消息默认走备用通道。',
      `（${event.from} → ${event.to}${event.reason ? `, reason: ${truncateReason(event.reason)}` : ''}）`,
    ];
    bot.api.sendMessage(Number(chatId), lines.join('\n')).catch(() => {});
  });

  deps.metering?.onSoftQuotaWarning(activeTelegramListener(() => active, (event) => {
    const chatId = poolKeyToChatId.get(event.tenantId);
    if (!chatId) return;
    const userId = parseConversationPoolKey(event.tenantId)?.userId ?? '';
    const label = resolveAgentFromPoolKey(router, userId, event.tenantId) ?? agent;
    const percent = Math.min(99, Math.round(event.ratio * 100));
    console.warn({
      ts: new Date().toISOString(),
      level: 'warn',
      agent: agent.id,
      msg: 'Soft quota warning emitted',
      poolKey: event.tenantId,
      monthKey: event.monthKey,
      currentCostUsd: event.currentCostUsd,
      budgetUsd: event.budgetUsd,
      percent,
    });
    const text = [
      `⚠️ ${label.name} 本月（${event.monthKey} UTC）累计消耗 $${event.currentCostUsd.toFixed(4)} / $${event.budgetUsd.toFixed(2)} 软配额（${percent}%）。`,
      '软配额仅做提醒，不强制阻断。如需明细请发 /usage。',
    ].join('\n');
    bot.api.sendMessage(Number(chatId), text).catch(() => {});
  }));

  // --- Session lifecycle notifications (F1-P0-004) ---

  runtime.on('session-end', (poolKey: string, reason: SessionEndReason) => {
    if (!active) return;
    if (!poolKeyToChatId.has(poolKey)) return;
    stopTyping(poolKey);
    activity.end(poolKey);
    if (['crash', 'crash-unrecoverable', 'session-in-use', 'timeout', 'runtime-failed', 'runtime-recovery-required'].includes(reason.type)
      || (reason.type === 'relay-shutdown' && reason.wasBusy)) void rendering.fail?.(poolKey);
    const chatId = poolKeyToChatId.get(poolKey);
    if (!chatId) return;

    const userId = parseConversationPoolKey(poolKey)?.userId ?? '';
    const label = resolveAgentFromPoolKey(router, userId, poolKey) ?? agent;

    let message: string | null = null;
    switch (reason.type) {
      case 'turns-exhausted':
        message = `⚠️ ${label.name} 已达到轮次上限 (${reason.turnCount}/${reason.maxTurns})，发新消息自动恢复。`;
        break;
      case 'crash':
        message = `⚠️ ${label.name} 遇到问题，已自动恢复。`;
        break;
      case 'crash-unrecoverable':
        message = `⚠️ ${label.name} 多次崩溃已暂停，发消息重试。`;
        break;
      case 'session-in-use':
        message = [
          `⚠️ ${label.name} 的 Claude 会话 ID 正被其他进程占用（或未正常释放）。`,
          '请关闭其他终端里的 claude / 复制会话，或发送 /new 换新 session 后再试。',
        ].join('\n');
        break;
      case 'rate-limited':
        message = [
          `⚠️ ${label.name} 当前推理服务被限流（rate limit），已停止自动重试。`,
          '直接再发一条消息可继续；如反复触发，建议 /new 切换 session 或稍候几分钟。',
        ].join('\n');
        break;
      case 'timeout':
        suppressNextGenericExitNotice(poolKey);
        message = `⚠️ ${label.name} 当前请求超时（>${Math.round(reason.timeoutMs / 1000)}s），已中止。直接再发一条消息继续。`;
        break;
      case 'runtime-failed':
        suppressNextGenericExitNotice(poolKey);
        message = `⚠️ ${label.name} 的 ${reason.runtime} runtime 已停止，当前请求没有自动切换或重放。请发送 /new 后重试。`;
        break;
      case 'runtime-recovery-required':
        suppressNextGenericExitNotice(poolKey);
        message = [
          `⚠️ ${label.name} 已执行可能产生外部副作用的操作，随后 runtime 失败。`,
          '为避免重复写入或重复网络操作，系统没有自动换 runtime 重放；请先核对结果，再发送 /new 或重试。',
        ].join('\n');
        break;
      case 'relay-shutdown':
        // Only tell the user about a restart if they're mid-turn waiting for us.
        if (reason.wasBusy) message = `⚡ Relay 正在重启，几秒后恢复。`;
        break;
      case 'idle-reclaimed':
        // Silent — next message auto-resumes
        break;
    }

    if (message) {
      bot.api.sendMessage(Number(chatId), message).catch(() => {});
    }
  });

  // 429 退避到达阈值时的一次性提示——不停 typing，不清状态，仅告知用户后台仍在等待。
  runtime.on('rate-limit-notice', (poolKey: string, info: { retryCount: number; nextDelayMs: number }) => {
    if (!active) return;
    if (!poolKeyToChatId.has(poolKey)) return;
    const chatId = poolKeyToChatId.get(poolKey);
    if (!chatId) return;
    const userId = parseConversationPoolKey(poolKey)?.userId ?? '';
    const label = resolveAgentFromPoolKey(router, userId, poolKey) ?? agent;
    const nextMin = Math.round(info.nextDelayMs / 60_000);
    const text = `🔄 ${label.name} 当前推理服务被限流（rate limit），后台仍在按指数退避自动重试（下次约 ${nextMin} 分钟后），无需操作。`;
    bot.api.sendMessage(Number(chatId), text).catch(() => {});
  });

  // --- Busy progress notifications (replaces auto-kill) ---
  runtime.on('busy-progress', (poolKey: string, progress: BusyProgressEvent) => {
    if (!active) return;
    if (!poolKeyToChatId.has(poolKey)) return;
    const chatId = poolKeyToChatId.get(poolKey);
    if (!chatId) return;

    const userId = parseConversationPoolKey(poolKey)?.userId ?? '';
    const label = resolveAgentFromPoolKey(router, userId, poolKey) ?? agent;

    const elapsedMin = Math.round(progress.elapsedMs / 60_000);
    let msg: string;
    if (progress.isFinalCheck) {
      msg = `⏳ ${label.name} 已持续运行 ${elapsedMin} 分钟。这是最后一次自动通知，如需终止请发送 /stop。`;
    } else {
      msg = `⏳ ${label.name} 正在执行任务（已运行 ${elapsedMin} 分钟），请耐心等待。如需终止请发送 /stop。`;
    }
    bot.api.sendMessage(Number(chatId), msg).catch(() => {});
  });

  return {
    apiBot(): Bot { return bot; },
    async start(): Promise<void> {
      bot.catch(err => {
        console.error({ ts: new Date().toISOString(), level: 'error', agent: agent.id, msg: 'grammy error', error: String(err) });
      });
      // Updates the last process acknowledged but did not hand over run
      // first, each behind its chat's resumed turns.
      const replayed = replayTelegramIntake(lanes, intake, resumed, async update => {
        await bot.init();
        await bot.handleUpdate(update);
      });
      if (replayed > 0) console.log({ ts: new Date().toISOString(), level: 'info', agent: agent.id, msg: 'Replaying Telegram updates recorded before restart', count: replayed });
      active = true;
      // Polling never waits for the menu, and restarts itself after a failure.
      polling = superviseTelegramPolling(bot, {
        onStart: () => {
          pollingStarted = true;
          lastSuccessfulPollAt = Date.now();
          console.log({ ts: new Date().toISOString(), level: 'info', msg: `${agent.emoji} ${agent.name} bot polling active` });
        },
        onEnded: (error, restartInMs) => {
          pollingStarted = false;
          console.error({ ts: new Date().toISOString(), level: 'error', agent: agent.id, msg: 'Telegram polling stopped; restarting', restartInMs, error: String(error) });
        },
      });
      // Register TG command menu (F2-P0-003) on the shared background queue.
      queueCommandMenu(() => (active ? registerCommandMenu(bot) : Promise.resolve()));
    },
    stop(): void {
      active = false;
      pollingStarted = false;
      // Queued updates stay in the journal for the next process.
      intake.close();
      resumeDone();
      for (const interval of typingIntervals.values()) clearInterval(interval);
      typingIntervals.clear();
      if (polling) polling.stop();
      else stopQuietly(bot);
    },
    async notifyResumed(): Promise<void> {
      try {
        if (!deps.stateManager) return;
        for (const user of router.getAllUsers()) {
          const chatId = user.chatId;
          if (!chatId || !authorizedChatIds.has(chatId)) continue;
          const resolved = router.resolveByBotAgent(agent.id, chatId);
          if (!resolved) continue;
          const routing = resolveInboundRoutingAgent(router, resolved.user.userId, resolved.agent, TELEGRAM_INBOUND_ROUTING);
          const poolKey = router.poolKey(resolved.user.userId, routing.id);
          await notifyResumedSession(chatId, poolKey, resolved);
        }
      } finally {
        resumeDone();
      }
    },
    async deliverInternalTurn(poolKey: string, prompt: string, expectNativeIds: string[], notice?: CollabTurnNotice, deliveryRef?: string): Promise<boolean> {
      if (!active || !deps.stateManager) return false;
      for (const user of router.getAllUsers()) {
        const chatId = user.chatId;
        if (!chatId || !authorizedChatIds.has(chatId)) continue;
        const resolved = router.resolveByBotAgent(agent.id, chatId);
        if (!resolved) continue;
        const routing = resolveInboundRoutingAgent(router, resolved.user.userId, resolved.agent, TELEGRAM_INBOUND_ROUTING);
        if (router.poolKey(resolved.user.userId, routing.id) !== poolKey) continue;
        poolKeyToChatId.set(poolKey, chatId);
        const proc = await spawnForResumedSession(poolKey, resolved);
        if (!namesConversation(proc as NativeIdSource | undefined, expectNativeIds)) return false;
        persistActiveSession(poolKey, processRuntimeId(proc, deps.stateManager.getSession(poolKey)?.runtime ?? 'cc'), processCodexThreadId(proc));
        if (notice) {
          await bot.api.sendMessage(Number(chatId), telegramTurnNoticeHtml(notice), { parse_mode: 'HTML' })
            .catch(() => bot.api.sendMessage(Number(chatId), collabTurnHeadline(notice))).catch(() => {});
        } else {
          await bot.api.sendMessage(Number(chatId), '📨 收到一份协作合同，开始处理。').catch(() => {});
        }
        const codexThreadId = processCodexThreadId(proc);
        // Fenced: the runtime runs it only in the conversation it was routed to, checked where the
        // turn starts (internal-turn-fence.ts); the check above only spares a notice.
        await runtime.send(poolKey, fencedInternalTurn({ type: 'human',
          content: internalDeliveryPrompt(prompt, codexThreadId ? 'codex' : 'cc', codexThreadId ?? proc?.sessionId ?? '', process.env) },
          expectNativeIds, deliveryRef));
        deps.stateManager.updateSession(poolKey, { lastActivity: Date.now() });
        return true;
      }
      return false;
    },
    async reload(): Promise<{ added: string[]; removed: string[]; total: number }> {
      return { added: [], removed: [], total: 1 };
    },
    runningAgentIds(): string[] {
      return pollingStarted && Date.now() - lastSuccessfulPollAt <= TG_POLL_FRESHNESS_MS ? [agent.id] : [];
    },
    getApiBot(agentId: string): Bot | null { return agentId === agent.id ? bot : null; },
    async sendText(agentId: string, chatId: string, text: string): Promise<void> {
      if(agentId!==agent.id)throw new Error(`Telegram bot ${agentId} is not active`);
      await bot.api.sendMessage(Number(chatId),text);
    },
  };
}

/**
 * Create all TG bots — one per agent.
 */
export function createTelegramBots(deps: TelegramBotDeps): TelegramBotInstance {
  const { router } = deps;
  const running = new Map<string, TelegramBotInstance & { apiBot(): Bot }>();
  let reloadChain: Promise<unknown> = Promise.resolve();
  let resumePassed = false;

  function snapshot(): Map<string, { agent: AgentConfig; authorizedChatIds: string[] }> {
    return new Map(router.getAllAgentBots().map(spec => [spec.agent.id, spec]));
  }

  function reconcile(): Promise<{ added: string[]; removed: string[]; total: number }> {
    return reconcileTelegramClientRegistry(running, snapshot(), (id, spec) => {
      if (!spec.agent.botToken) throw new Error(`Agent "${id}" has no botToken`);
      return createSingleBot(spec.agent, new Set(spec.authorizedChatIds), deps, resumePassed);
    });
  }

  return {
    async start(): Promise<void> {
      await reconcile();
    },
    stop(): void {
      for (const inst of running.values()) {
        inst.stop();
      }
      running.clear();
    },
    async notifyResumed(): Promise<void> {
      resumePassed = true;
      await Promise.all(Array.from(running.values()).map(inst => inst.notifyResumed()));
    },
    async deliverInternalTurn(poolKey: string, prompt: string, expectNativeIds: string[], notice?: CollabTurnNotice, deliveryRef?: string): Promise<boolean> {
      for (const inst of running.values()) {
        if (await inst.deliverInternalTurn(poolKey, prompt, expectNativeIds, notice, deliveryRef)) return true;
      }
      return false;
    },
    async reload(): Promise<{ added: string[]; removed: string[]; total: number }> {
      const job = reloadChain.then(reconcile);
      reloadChain = job.catch(() => undefined);
      return job;
    },
    runningAgentIds(): string[] {
      return Array.from(running.entries()).filter(([, client]) => client.runningAgentIds().length > 0)
        .map(([id]) => id).sort();
    },
    getApiBot(agentId: string): Bot | null { return running.get(agentId)?.apiBot() ?? null; },
    async sendText(agentId: string, chatId: string, text: string): Promise<void> {
      const instance=running.get(agentId);
      if(!instance)throw new Error(`Telegram bot ${agentId} is not active`);
      await instance.sendText(agentId,chatId,text);
    },
  };
}

// Keep backward compat export name for single-bot use
export const createTelegramBot = createTelegramBots;
