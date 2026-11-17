import {nativeCoreFixture} from './helpers/native-work-item-core.js';
import { createStateManager } from '../src/state.js';
import { humanMessageProof } from '../src/collaboration-capture.js';
import { resolveToolFileOutboxPath } from '../src/tool-file-outbox.js';
import {isolateHumanDecisions} from './helpers/scl-human-decision.js';
isolateHumanDecisions();
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { existsSync, mkdirSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import {
  buildMarkdownCard,
  splitCardText,
  stripCCNoise,
  extractPostText,
  collectPostImageKeys,
  describeFeishuResourceContent,
  extractCardText,
  handleFeishuMessage,
  admitFeishuInboundMessage,
  createFeishuIntake,
  replayFeishuIntake,
  FEISHU_MESSAGE_DEDUPE_WINDOW_MS,
  __resetFeishuInboundDedupeForTests,
  handleCommand,
  ensureProcess,
  handleFeishuPoolResponse,
  handleSessionEnd,
  classifyFeishuInboundFile,
  humanContentBlocksFromFeishuFile,
  createFeishuStreamingSink,
  wirePoolEvents,
  buildFeishuResumeSpawnConfig,
  captureFeishuBusinessDispatch,
  handleFeishuCardAction,
  sendCollabTurnNotice,
  type MessageHandlerContext,
} from '../src/channels/feishu.js';
import { readdirSync } from 'node:fs';
import lark from '@larksuiteoapi/node-sdk';
import { createChatLanes } from '../src/channels/chat-lanes.js';
import { createChannelIntakeJournal, intakeAwareRuntime } from '../src/channels/intake-journal.js';
import { persistQueuedTurns, replayPendingTurns } from '../src/shutdown-interrupts.js';
import { nextTurnSeq } from '../src/maintenance-barrier.js';
import type { AgentConfig, RoutePolicyResult, UserConfig } from '../src/types.js';
import type { Router } from '../src/router.js';
import type { StreamJsonMessage, StateManager, TurnSubmitOptions } from '../src/types.js';
import type { CommandRuntime } from '../src/commands.js';
import {buildRuntimeUsageBinding,runtimeAuthorityRef} from '../src/runtime-usage-binding.js';
import {feishuRecoveryBoundaryToken,recordFeishuRecoveryBoundary} from '../src/feishu-inbound-recovery.js';
import { resolveRichDocChannelProfiles } from '../src/channel-rich-rendering.js';
import {
  __resetFeishuOutboundCacheForTests,
  recordFeishuOutboundText,
} from '../src/feishu-outbound-cache.js';

// --- Helpers ---

const temporaryWorkspaces: string[] = [];

beforeEach(() => {
  __resetFeishuInboundDedupeForTests();
  __resetFeishuOutboundCacheForTests();
});

afterEach(() => {
  for (const workspace of temporaryWorkspaces.splice(0)) {
    rmSync(workspace, { recursive: true, force: true });
  }
});

const feishuAgent: AgentConfig = {
  id: 'pulse',
  name: 'Pulse',
  emoji: '\u{1F52E}',
  channel: 'feishu',
  workspace: '/tmp/ws',
  sessionId: 'uuid-tg-fallback',
  feishu: { appId: 'cli_test', appSecret: 'secret', sessionId: 'uuid-feishu' },
};

const feishuUser: UserConfig = {
  userId: 'eric',
  feishuOpenId: 'ou_eric123',
  defaultAgent: 'pulse',
  agents: [feishuAgent],
};

function makePool(overrides?: Partial<CommandRuntime>): CommandRuntime {
  let current: {runtime:string;state:string;sessionId:string;workspace:string}|undefined;
  return {
    spawn: vi.fn(async (config:Parameters<CommandRuntime['spawn']>[0]) => {
      current={runtime:config.runtime,state:'ready',sessionId:config.sessionId,workspace:config.workspace};
      return current;
    }),
    send: vi.fn().mockResolvedValue(undefined),
    kill: vi.fn(async()=>{current=undefined;}),
    killAll: vi.fn(),
    get: vi.fn(()=>current),
    getAll: vi.fn().mockReturnValue([]),
    getStatus: vi.fn().mockReturnValue([]),
    on: vi.fn(),
    emitShutdown: vi.fn(),
    dispose: vi.fn(),
    ...overrides,
  } as unknown as CommandRuntime;
}

it('dispatches without waiting for metering but still drops a turn superseded by Stop',async()=>{
  const key='fixture:cold-baseline';recordFeishuRecoveryBoundary(key,'original-message',undefined,{kind:'input'});
  const boundary=feishuRecoveryBoundaryToken(key);
  const activation=new Promise<void>(()=>{});
  const live={runtime:'pi',state:'ready',sessionId:'original-native',workspace:'/tmp/ws',usageBaselineActivation:activation};
  const pool=makePool({get:vi.fn(()=>live as never)});
  const stopTyping=vi.fn();
  const dispatch=captureFeishuBusinessDispatch({pool,stopTyping},key,boundary,'original-message');
  const pending=dispatch({type:'human',content:'continue original authorized work'});
  expect(pool.send).toHaveBeenCalledTimes(1); // The measurement Promise is unresolved.
  expect(await pending).toBe(true);
  const stale=captureFeishuBusinessDispatch({pool,stopTyping},key,boundary,'original-message');
  recordFeishuRecoveryBoundary(key,'stop-button',undefined,{kind:'control',control:'stop'});
  expect(await stale({type:'human',content:'stale work'})).toBe(false);
  expect(pool.send).toHaveBeenCalledTimes(1);
  expect(stopTyping).toHaveBeenCalledWith('original-message');
});

it('dispatches to a CC process whose adapter returns a fresh wrapper per lookup', async () => {
  const key='ou_cc:pulse', child={pid:4242}, other={pid:4343};
  let live:object=child;
  const pool={get:vi.fn(()=>({tenantId:key,runtime:'cc',state:'idle',sessionId:'cc-session',process:live})),
    send:vi.fn().mockResolvedValue(undefined)} as unknown as MessageHandlerContext['pool'];
  const stopTyping=vi.fn(), warn=vi.spyOn(console,'warn').mockImplementation(()=>undefined);
  const boundary=feishuRecoveryBoundaryToken(key);
  expect(await captureFeishuBusinessDispatch({pool,stopTyping},key,boundary,'cc-message')({type:'human',content:'时区'})).toBe(true);
  expect(pool.send).toHaveBeenCalledTimes(1);
  const replaced=captureFeishuBusinessDispatch({pool,stopTyping},key,boundary,'cc-replaced');
  live=other;
  expect(await replaced({type:'human',content:'stale'})).toBe(false);
  expect(pool.send).toHaveBeenCalledTimes(1);
  expect(stopTyping).toHaveBeenCalledWith('cc-replaced');
  expect(warn).toHaveBeenCalledWith(expect.objectContaining({msg:'Feishu business dispatch fenced; message not sent',poolKey:key}));
  warn.mockRestore();
});

function makeRouter(overrides?: Partial<Router>): Router {
  return {
    resolveUser: vi.fn(),
    getActiveAgent: vi.fn(),
    switchAgent: vi.fn(),
    poolKey: (userId: string, agentId: string) => `${userId}:${agentId}`,
    getAgents: vi.fn().mockReturnValue([feishuAgent]),
    getAllUsers: vi.fn().mockReturnValue([feishuUser]),
    resolveByBotAgent: vi.fn(),
    resolveByFeishuAgent: vi.fn().mockReturnValue({ user: feishuUser, agent: feishuAgent }),
    resetSession: vi.fn(),
    overrideSessionId: vi.fn(),
    getAllAgentBots: vi.fn().mockReturnValue([]),
    getAllFeishuBots: vi.fn().mockReturnValue([{ agent: feishuAgent, authorizedOpenIds: ['ou_eric123'] }]),
    getAgentChannel: vi.fn().mockReturnValue('feishu' as const),
    ...overrides,
  } as unknown as Router;
}

function makeCtx(overrides?: Partial<MessageHandlerContext>): MessageHandlerContext {
  const sent: Array<{ openId: string; text: string; card?: Record<string, unknown> }> = [];
  return {
    agent: feishuAgent,
    pool: makePool(),
    router: makeRouter(),
    poolKeyToOpenId: new Map([['eric:pulse', 'ou_eric123']]),
    sendTextMessage: vi.fn(async (openId, text) => { sent.push({ openId, text }); }),
    sendCardMessage: vi.fn(async (openId, card) => { sent.push({ openId, text: '', card }); }),
    downloadImage: vi.fn().mockResolvedValue({ base64: 'abc123', mediaType: 'image/jpeg' }),
    downloadFileResource: vi.fn().mockResolvedValue({
      buffer: Buffer.from('%PDF-1.4 minimal'),
      contentType: 'application/pdf',
    }),
    transcribeInboundAudio: vi.fn().mockResolvedValue('\u8F6C\u5199\u7ED3\u679C'),
    larkClient: { request: vi.fn() } as any,
    _sent: sent,
    ...overrides,
  } as any;
}

// --- buildMarkdownCard ---

describe('buildMarkdownCard', () => {
  it('returns valid Schema 2.0 card with text', () => {
    const card = buildMarkdownCard('Hello world');
    expect(card.schema).toBe('2.0');
    expect((card.config as any).width_mode).toBe('fill');
    expect(card.header).toBeUndefined();
    const elements = (card.body as any).elements;
    expect(elements).toEqual([{ tag: 'markdown', content: 'Hello world' }]);
  });

  it('keeps fenced code visually clean without answer-level or code-level copy buttons', () => {
    const card = buildMarkdownCard('```ts\nconst answer = 42;\n```');
    const elements = (card.body as any).elements;

    expect(elements).toEqual([{ tag: 'markdown', content: '```ts\nconst answer = 42;\n```' }]);
    expect(JSON.stringify(card)).not.toMatch(/relay_copy|发送可复制|发送代码/);
  });

  it('custom header title', () => {
    const card = buildMarkdownCard('Hi', { header: { title: '\u{1F3B2} Duke' } });
    expect((card.header as any).title.content).toBe('\u{1F3B2} Duke');
  });

  it('custom header template', () => {
    const card = buildMarkdownCard('Hi', { header: { title: 'Test', template: 'green' } });
    expect((card.header as any).template).toBe('green');
  });

  it('empty text returns empty elements', () => {
    const card = buildMarkdownCard('');
    const elements = (card.body as any).elements;
    expect(elements.length).toBe(0);
  });

  it('with note element', () => {
    const card = buildMarkdownCard('Hello', { note: 'some note' });
    const elements = (card.body as any).elements;
    expect(elements.length).toBe(3);
    expect(elements[1].tag).toBe('hr');
    expect(elements[2].tag).toBe('markdown');
    expect(elements[2].content).toContain('some note');
  });
});

describe('legacy code-block copy action compatibility', () => {
  it('still serves an already-delivered card without generating controls on new cards', async () => {
    const ctx = makeCtx();
    recordFeishuOutboundText(
      'message-with-code',
      '说明\n\n```sh\necho first\n```\n\n正文\n\n```python\nprint("second")\n```',
    );

    await handleFeishuCardAction(ctx, {
      event: {
        operator: { open_id: 'ou_eric123' },
        open_message_id: 'message-with-code',
        action: { value: { relay_copy_code: 1 } },
      },
    });

    expect(ctx.sendTextMessage).toHaveBeenCalledOnce();
    expect(ctx.sendTextMessage).toHaveBeenCalledWith('ou_eric123', 'print("second")\n');
  });

  it('still serves an already-delivered whole-answer copy callback', async () => {
    const ctx = makeCtx();
    recordFeishuOutboundText('legacy-whole-answer', '旧卡完整回答');

    await handleFeishuCardAction(ctx, {
      event: {
        operator: { open_id: 'ou_eric123' },
        open_message_id: 'legacy-whole-answer',
        action: { value: { relay_copy: true } },
      },
    });

    expect(ctx.sendTextMessage).toHaveBeenCalledOnce();
    expect(ctx.sendTextMessage).toHaveBeenCalledWith('ou_eric123', '旧卡完整回答');
  });
});

// --- splitCardText ---

describe('splitCardText', () => {
  it('short text returns single chunk', () => {
    expect(splitCardText('Hello')).toEqual(['Hello']);
  });

  it('text at maxLength returns single chunk', () => {
    const text = 'a'.repeat(4000);
    expect(splitCardText(text, 4000)).toEqual([text]);
  });

  it('over-max text splits at paragraph boundary', () => {
    const text = 'a'.repeat(3000) + '\n\n' + 'b'.repeat(3000);
    const result = splitCardText(text, 4000);
    expect(result.length).toBe(2);
    expect(result[0].length).toBeLessThanOrEqual(4000);
    expect(result[1].length).toBeLessThanOrEqual(4000);
  });

  it('over-max text splits at newline boundary', () => {
    const text = 'a'.repeat(3000) + '\n' + 'b'.repeat(3000);
    expect(splitCardText(text, 4000).length).toBe(2);
  });

  it('over-max text without newlines splits at maxLength', () => {
    const text = 'a'.repeat(5000);
    const result = splitCardText(text, 4000);
    expect(result.length).toBe(2);
    expect(result[0].length).toBe(4000);
    expect(result[1].length).toBe(1000);
  });

  it('respects custom maxLength', () => {
    const text = 'a'.repeat(200);
    expect(splitCardText(text, 100).length).toBe(2);
    expect(splitCardText(text, 100)[0].length).toBe(100);
  });
});

// --- stripCCNoise ---

describe('stripCCNoise', () => {
  it('removes OAuth noise lines', () => {
    const input = 'normal text\nOAuth轮询超时，正在重试\nmore text';
    const result = stripCCNoise(input);
    expect(result).toBe('normal text\nmore text');
  });

  it('removes apiKeySource noise', () => {
    const result = stripCCNoise('hello\napiKeySource: env\nworld');
    expect(result).toBe('hello\nworld');
  });

  it('keeps clean text unchanged', () => {
    expect(stripCCNoise('just normal text')).toBe('just normal text');
  });

  it('returns empty string when all lines are noise', () => {
    const result = stripCCNoise('OAuth轮询超时\nKey已经在用了');
    expect(result).toBe('');
  });

  it('preserves blank lines', () => {
    const result = stripCCNoise('line1\n\nline2');
    expect(result).toBe('line1\n\nline2');
  });

  it('removes standalone [Request interrupted] marker lines', () => {
    expect(stripCCNoise('before\n[Request interrupted]\nafter')).toBe('before\nafter');
    expect(stripCCNoise('before\n[Request interrupted by user]\nafter')).toBe('before\nafter');
  });

  it('does not strip prose that merely contains the word interrupted', () => {
    const input = 'The connection was interrupted briefly.';
    expect(stripCCNoise(input)).toBe(input);
  });
});

// --- classifyFeishuInboundFile / humanContentBlocksFromFeishuFile ---

describe('classifyFeishuInboundFile', () => {
  it('classifies PDF by mime', () => {
    expect(classifyFeishuInboundFile({ contentType: 'application/pdf' })).toEqual({
      kind: 'document',
      mediaType: 'application/pdf',
    });
  });

  it('classifies PDF by extension when octet-stream', () => {
    expect(classifyFeishuInboundFile({ contentType: 'application/octet-stream', fileName: 'a.pdf' })).toEqual({
      kind: 'document',
      mediaType: 'application/pdf',
    });
  });

  it('classifies text by extension', () => {
    expect(classifyFeishuInboundFile({ fileName: 'readme.md' }).kind).toBe('text_file');
  });

  it('rejects unknown types', () => {
    const c = classifyFeishuInboundFile({ contentType: 'application/zip', fileName: 'x.bin' });
    expect(c.kind).toBe('unsupported');
  });
});

describe('humanContentBlocksFromFeishuFile', () => {
  it('builds document block', () => {
    const buf = Buffer.from('%PDF');
    const blocks = humanContentBlocksFromFeishuFile(buf, { kind: 'document', mediaType: 'application/pdf' }, 'f.pdf');
    expect(blocks).toEqual([
      { type: 'document', source: { type: 'base64', media_type: 'application/pdf', data: buf.toString('base64') } },
    ]);
  });
});

// --- extractPostText ---

describe('extractPostText', () => {
  it('extracts text from zh_cn post with emoji', () => {
    const content = JSON.stringify({
      zh_cn: {
        title: '',
        content: [
          [{ tag: 'text', text: 'Hello ' }, { tag: 'text', text: '🟢' }],
        ],
      },
    });
    expect(extractPostText(content)).toBe('Hello 🟢');
  });

  it('extracts text from en_us post', () => {
    const content = JSON.stringify({
      en_us: {
        title: 'Title',
        content: [
          [{ tag: 'text', text: 'Line 1' }],
          [{ tag: 'text', text: 'Line 2' }],
        ],
      },
    });
    expect(extractPostText(content)).toBe('Title\nLine 1\nLine 2');
  });

  it('extracts link href from a tag', () => {
    const content = JSON.stringify({
      zh_cn: {
        title: '',
        content: [[{ tag: 'a', text: 'Click', href: 'https://example.com' }]],
      },
    });
    expect(extractPostText(content)).toBe('https://example.com');
  });

  it('extracts @mention from at tag', () => {
    const content = JSON.stringify({
      zh_cn: {
        title: '',
        content: [[{ tag: 'at', user_id: 'ou_123', user_name: 'Alice' }]],
      },
    });
    expect(extractPostText(content)).toBe('@Alice');
  });

  it('returns null for invalid JSON', () => {
    expect(extractPostText('not json')).toBeNull();
  });

  it('returns null for missing content array', () => {
    const content = JSON.stringify({ zh_cn: { title: 'No content' } });
    expect(extractPostText(content)).toBeNull();
  });

  it('returns null for empty post', () => {
    const content = JSON.stringify({ zh_cn: { title: '', content: [[]] } });
    expect(extractPostText(content)).toBeNull();
  });

  it('falls back to first locale key if no zh_cn/en_us/ja_jp', () => {
    const content = JSON.stringify({
      custom_locale: {
        title: '',
        content: [[{ tag: 'text', text: 'fallback' }]],
      },
    });
    expect(extractPostText(content)).toBe('fallback');
  });
});

// --- collectPostImageKeys ---
// 2026-07-27 大鸟指出：发图片带文字，Agent 往往只看得到文字、看不到图片。
// 根因是 extractBlockText 对 tag:'img' 返回空串，图片块被静默丢弃。

describe('collectPostImageKeys', () => {
  it('picks up image blocks mixed with text', () => {
    const content = JSON.stringify({
      zh_cn: {
        title: '',
        content: [[
          { tag: 'text', text: '看这张图' },
          { tag: 'img', image_key: 'img_v3_abc' },
        ]],
      },
    });
    expect(collectPostImageKeys(content)).toEqual(['img_v3_abc']);
  });

  it('collects multiple images in order and dedupes', () => {
    const content = JSON.stringify({
      zh_cn: {
        title: '',
        content: [
          [{ tag: 'img', image_key: 'img_a' }],
          [{ tag: 'text', text: '对比' }, { tag: 'img', image_key: 'img_b' }],
          [{ tag: 'img', image_key: 'img_a' }],
        ],
      },
    });
    expect(collectPostImageKeys(content)).toEqual(['img_a', 'img_b']);
  });

  it('returns empty for text-only post and for malformed json', () => {
    const textOnly = JSON.stringify({
      zh_cn: { title: '', content: [[{ tag: 'text', text: 'no image' }]] },
    });
    expect(collectPostImageKeys(textOnly)).toEqual([]);
    expect(collectPostImageKeys('not json')).toEqual([]);
  });
});

// --- describeFeishuResourceContent ---
// wi_20260727_397 / aki：引用一条文件消息时只透出 [Feishu file message]，
// Agent 认不出被引用的是哪个附件。

describe('describeFeishuResourceContent', () => {
  it('prefers file_name for file messages', () => {
    const content = JSON.stringify({ file_key: 'file_v3_xyz', file_name: 'companion-2026-07-27.epub' });
    expect(describeFeishuResourceContent('file', content))
      .toBe('[Feishu file: companion-2026-07-27.epub]');
  });

  it('falls back to image_key for images', () => {
    const content = JSON.stringify({ image_key: 'img_v3_0123456789abcdefXXXX' });
    const out = describeFeishuResourceContent('image', content);
    // key 截断到 16 字符，只为区分不同附件，不是给人读的。
    expect(out).toBe('[Feishu image: img_v3_012345678…]');
  });

  it('returns undefined when nothing identifiable', () => {
    expect(describeFeishuResourceContent('audio', JSON.stringify({}))).toBeUndefined();
    expect(describeFeishuResourceContent('file', 'not json')).toBeUndefined();
  });
});

// --- handleFeishuMessage ---

describe('handleFeishuMessage', () => {
  it('text message from registered user sends to pool', async () => {
    const ctx = makeCtx();
    await handleFeishuMessage(ctx, {
      message: { message_type: 'text', message_id: 'msg1', content: JSON.stringify({ text: 'hi' }) },
      sender: { sender_id: { open_id: 'ou_eric123' } },
    });
    expect(ctx.pool.send).toHaveBeenCalledWith('eric:pulse', { type: 'human', content: expect.stringContaining('hi') });
    const outgoing = vi.mocked(ctx.pool.send).mock.calls[0]![1];
    expect(humanMessageProof(outgoing)?.ref).toMatch(/^[a-f0-9]{64}$/);
    expect(humanMessageProof(JSON.parse(JSON.stringify(outgoing)))).toBeUndefined();
  });

  it('陈旧重投消息被丢弃——2026-08-05 跨 /new 串场实况回放', async () => {
    // 大鸟 02:19:30 发的一句打分，被飞书在 03:24:55 重投（隔 65 分钟）。彼时
    // message_id 早被清出 2 分钟去重表，遂被当成全新消息放行，投进了 02:32
    // `/new` 之后换好的新 session——上一场的消息凭空出现在新会话里。
    const ctx = makeCtx();
    const staleCreateTime = String(Date.now() - 65 * 60 * 1000);
    await handleFeishuMessage(ctx, {
      message: {
        message_type: 'text',
        message_id: 'om_x100b681d95e11cb0de39e7f124',
        create_time: staleCreateTime,
        content: JSON.stringify({ text: '78。迭代了 MALIK，更新了 8 月 OKR' }),
      },
      sender: { sender_id: { open_id: 'ou_eric123' } },
    });
    expect(ctx.pool.send).not.toHaveBeenCalled();
  });

  it('正常新消息不被陈旧闸误杀', async () => {
    // 阴性对照：闸必须只拦重投，不能把刚发的真消息一起吃掉。
    const ctx = makeCtx();
    await handleFeishuMessage(ctx, {
      message: {
        message_type: 'text',
        message_id: 'msg-fresh',
        create_time: String(Date.now() - 3 * 1000),
        content: JSON.stringify({ text: 'hi' }),
      },
      sender: { sender_id: { open_id: 'ou_eric123' } },
    });
    expect(ctx.pool.send).toHaveBeenCalledWith('eric:pulse', { type: 'human', content: expect.stringContaining('hi') });
  });

  it('create_time 缺失或不可解析时放行——判不出就不拦', async () => {
    // 宁可漏拦一条，不可因为读不到字段就把真消息吃掉（静默丢弃是红线）。
    for (const createTime of [undefined, '', 'not-a-number', '0']) {
      const ctx = makeCtx();
      await handleFeishuMessage(ctx, {
        message: {
          message_type: 'text',
          message_id: `msg-nocreate-${String(createTime)}`,
          create_time: createTime,
          content: JSON.stringify({ text: 'hi' }),
        },
        sender: { sender_id: { open_id: 'ou_eric123' } },
      });
      expect(ctx.pool.send).toHaveBeenCalledWith('eric:pulse', { type: 'human', content: expect.stringContaining('hi') });
    }
  });

  it('a redelivery 2–5 minutes after arrival is still a duplicate; older ones stay stale', () => {
    // Before: ids were kept 2 minutes, the stale gate cut at 5, so a redelivery
    // in between passed both gates and ran the message a second time.
    const now = Date.now();
    const data = { message: { message_type: 'text', message_id: 'om_redelivered', create_time: String(now - 1000),
      content: JSON.stringify({ text: 'hi' }) }, sender: { sender_id: { open_id: 'ou_eric123' } } };
    expect(FEISHU_MESSAGE_DEDUPE_WINDOW_MS).toBeGreaterThan(5 * 60 * 1000);
    expect(admitFeishuInboundMessage(feishuAgent.id, data, now)).toBe(true);
    for (const later of [2 * 60 * 1000 + 1, 3 * 60 * 1000, 4 * 60 * 1000 + 59 * 1000, 5 * 60 * 1000 + 30 * 1000]) {
      expect(admitFeishuInboundMessage(feishuAgent.id, data, now + later)).toBe(false);
    }
    expect(admitFeishuInboundMessage(feishuAgent.id, data, now + 65 * 60 * 1000)).toBe(false);
  });

  it('acknowledges the event at arrival while the turn waits behind a busy session; the sender keeps order', async () => {
    const dispatched: string[] = [];
    const pool = makePool({ send: vi.fn((_key: string, message: StreamJsonMessage) => {
      dispatched.push(String(message.content));
      return new Promise<void>(() => {}); // a busy session: dispatch never completes here
    }) as never });
    const ctx = makeCtx({ pool });
    const receive = createFeishuIntake(ctx, createChatLanes(() => {}), createChannelIntakeJournal(undefined, 'feishu:pulse'));
    const dispatcher = new lark.EventDispatcher({}).register({ 'im.message.receive_v1': (data: any) => { receive(data); } });
    const event = (id: string, text: string) => ({ schema: '2.0', header: { event_id: `ev_${id}`, event_type: 'im.message.receive_v1' },
      event: { message: { message_type: 'text', message_id: id, create_time: String(Date.now()), content: JSON.stringify({ text }) },
        sender: { sender_id: { open_id: 'ou_eric123' } } } });
    // The SDK sends its acknowledgement when invoke settles: it must not wait for the turn.
    await dispatcher.invoke(event('om_first', 'first'), { needCheck: false });
    await dispatcher.invoke(event('om_second', 'second'), { needCheck: false });
    await vi.waitFor(() => expect(dispatched).toHaveLength(2));
    expect(dispatched[0]).toContain('first');
    expect(dispatched[1]).toContain('second');
    // A redelivery of an acknowledged message changes nothing.
    await dispatcher.invoke(event('om_first', 'first'), { needCheck: false });
    await new Promise(resolve => setTimeout(resolve, 20));
    expect(dispatched).toHaveLength(2);
  });

  it('a slow image finishes during shutdown: after restart it resumes, then the acknowledged texts, once each and in order', async () => {
    const dir = mkdtempSync(join(tmpdir(), 'feishu-intake-restart-'));
    const statePath = join(dir, 'state.json');
    const key = 'eric:pulse';
    try {
      // --- the stopping process ---
      const before = createStateManager(statePath);
      before.load();
      before.updateSession(key, { poolKey: key, agentId: 'pulse', sessionId: 'feishu-session', lastActivity: Date.now(), status: 'active' } as never);
      let barrierClosed = false;
      const sentBefore: string[] = [];
      // Like the runtime manager over a closed maintenance barrier: a turn
      // reaching the runtime then gets its receipt, is recorded for resume at
      // once and is never dispatched.
      const poolBefore = makePool({ send: vi.fn((tenantId: string, message: StreamJsonMessage, submit?: TurnSubmitOptions) => {
        if (barrierClosed) {
          submit?.onAccepted?.();
          persistQueuedTurns(before, [{ tenantId, runtime: 'cc', queuedAt: Date.now(), seq: submit?.seq ?? nextTurnSeq(), message }], Date.now());
          before.flush();
          return new Promise<void>(() => {});
        }
        submit?.onAccepted?.();
        sentBefore.push(JSON.stringify(message.content));
        return Promise.resolve();
      }) as never });
      let releaseImage!: () => void;
      const image = new Promise<void>(resolve => { releaseImage = resolve; });
      const ctxBefore = makeCtx({ pool: intakeAwareRuntime(poolBefore), stateManager: before,
        downloadImage: vi.fn(async () => { await image; return { base64: 'abc123', mediaType: 'image/jpeg' }; }) as never });
      const journalBefore = createChannelIntakeJournal(before, 'feishu:pulse');
      const receive = createFeishuIntake(ctxBefore, createChatLanes(() => {}), journalBefore);
      const dispatcher = new lark.EventDispatcher({}).register({ 'im.message.receive_v1': (data: any) => { receive(data); } });
      const event = (id: string, type: string, content: Record<string, unknown>) => ({ schema: '2.0',
        header: { event_id: `ev_${id}`, event_type: 'im.message.receive_v1' },
        event: { message: { message_type: type, message_id: id, create_time: String(Date.now()), content: JSON.stringify(content) },
          sender: { sender_id: { open_id: 'ou_eric123' } } } });
      // Each invoke settling is the SDK's acknowledgement.
      await dispatcher.invoke(event('om_image', 'image', { image_key: 'img_slow' }), { needCheck: false });
      await dispatcher.invoke(event('om_second', 'text', { text: 'second' }), { needCheck: false });
      await dispatcher.invoke(event('om_third', 'text', { text: 'third' }), { needCheck: false });
      // Shutdown order of cli.ts: barrier closed, bots stopped, state flushed.
      barrierClosed = true;
      journalBefore.close();
      before.flush();
      releaseImage();
      await vi.waitFor(() => expect(before.getSession(key)?.pendingMaintenanceTurns).toHaveLength(1));
      expect(before.getChannelIntake!('feishu:pulse').map(record => record.id)).toEqual(['om_second', 'om_third']);
      expect(sentBefore).toEqual([]);
      before.dispose();

      // --- the restarted process ---
      __resetFeishuInboundDedupeForTests();
      const after = createStateManager(statePath);
      after.load();
      const sentAfter: string[] = [];
      const poolAfter = makePool({ send: vi.fn(async (_tenantId: string, message: StreamJsonMessage, submit?: TurnSubmitOptions) => {
        submit?.onAccepted?.();
        sentAfter.push(JSON.stringify(message.content));
      }) as never });
      const ctxAfter = makeCtx({ pool: intakeAwareRuntime(poolAfter), stateManager: after });
      const lanesAfter = createChatLanes(() => {});
      const journalAfter = createChannelIntakeJournal(after, 'feishu:pulse');
      let resumeDone!: () => void;
      const resumed = new Promise<void>(resolve => { resumeDone = resolve; });
      expect(replayFeishuIntake(ctxAfter, lanesAfter, journalAfter, resumed)).toBe(2);
      const receiveAfter = createFeishuIntake(ctxAfter, lanesAfter, journalAfter);
      // Feishu sends the third one again, as after a lost acknowledgement.
      receiveAfter(event('om_third', 'text', { text: 'third' }).event);
      await new Promise(resolve => setTimeout(resolve, 20));
      expect(sentAfter).toEqual([]);
      // The resume pass (notifyResumed) replays the turns shutdown recorded.
      await replayPendingTurns(after, key, (message, submit) => poolAfter.send(key, message, submit));
      resumeDone();
      await vi.waitFor(() => expect(sentAfter).toHaveLength(3));
      await new Promise(resolve => setTimeout(resolve, 20));
      expect(sentAfter).toHaveLength(3);
      expect(sentAfter[0]).toContain('image');
      expect(sentAfter[1]).toContain('second');
      expect(sentAfter[2]).toContain('third');
      expect(after.getChannelIntake!('feishu:pulse')).toEqual([]);
      after.dispose();
    } finally {
      rmSync(dir, { recursive: true, force: true });
    }
  });

  it('a message still downloading when the bot is replaced is dispatched once, by the bot that started it', async () => {
    const dir = mkdtempSync(join(tmpdir(), 'feishu-intake-reload-'));
    try {
      const state = createStateManager(join(dir, 'state.json'));
      state.load();
      const sent: string[] = [];
      const pool = makePool({ send: vi.fn(async (_tenantId: string, message: StreamJsonMessage, submit?: TurnSubmitOptions) => {
        submit?.onAccepted?.();
        sent.push(JSON.stringify(message.content));
      }) as never });
      let releaseImage!: () => void;
      const image = new Promise<void>(resolve => { releaseImage = resolve; });
      let downloads = 0;
      const ctx = makeCtx({ pool: intakeAwareRuntime(pool), stateManager: state,
        downloadImage: vi.fn(async () => { downloads++; await image; return { base64: 'abc123', mediaType: 'image/jpeg' }; }) as never });
      const older = createChannelIntakeJournal(state, 'feishu:pulse');
      createFeishuIntake(ctx, createChatLanes(() => {}), older)({ message: { message_type: 'image', message_id: 'om_img',
        create_time: String(Date.now()), content: JSON.stringify({ image_key: 'img_slow' }) }, sender: { sender_id: { open_id: 'ou_eric123' } } });
      await vi.waitFor(() => expect(downloads).toBe(1));
      // A reload replaces the bot: the old one closes, a new one replays.
      older.close();
      const newer = createChannelIntakeJournal(state, 'feishu:pulse');
      expect(replayFeishuIntake(ctx, createChatLanes(() => {}), newer, Promise.resolve())).toBe(0);
      releaseImage();
      await vi.waitFor(() => expect(sent).toHaveLength(1));
      await new Promise(resolve => setTimeout(resolve, 20));
      expect(sent).toHaveLength(1);
      expect(downloads).toBe(1);
      expect(state.getChannelIntake!('feishu:pulse')).toEqual([]);
      state.dispose();
    } finally {
      rmSync(dir, { recursive: true, force: true });
    }
  });

  it('keeps an acknowledged message in the journal until the runtime admits its turn', async () => {
    const dir = mkdtempSync(join(tmpdir(), 'feishu-intake-receipt-'));
    try {
      const state = createStateManager(join(dir, 'state.json'));
      state.load();
      let admit!: () => void;
      // Sent, but still being prepared: no receipt yet (the CC pool reading session access).
      const pool = makePool({ send: vi.fn((_tenantId: string, _message: StreamJsonMessage, submit?: TurnSubmitOptions) => {
        admit = () => submit?.onAccepted?.();
        return new Promise<void>(() => {});
      }) as never });
      const journal = createChannelIntakeJournal(state, 'feishu:pulse');
      const receive = createFeishuIntake(makeCtx({ pool: intakeAwareRuntime(pool), stateManager: state }), createChatLanes(() => {}), journal);
      receive({ message: { message_type: 'text', message_id: 'om_wait', create_time: String(Date.now()), content: JSON.stringify({ text: 'wait' }) },
        sender: { sender_id: { open_id: 'ou_eric123' } } });
      await vi.waitFor(() => expect(pool.send).toHaveBeenCalled());
      await new Promise(resolve => setTimeout(resolve, 20));
      expect(state.getChannelIntake!('feishu:pulse').map(record => record.id)).toEqual(['om_wait']);
      admit();
      expect(state.getChannelIntake!('feishu:pulse')).toEqual([]);
      state.dispose();
    } finally {
      rmSync(dir, { recursive: true, force: true });
    }
  });

  it('/stop bypasses a sender lane that is still preparing an earlier message', async () => {
    const ctx = makeCtx({ downloadImage: vi.fn(() => new Promise(() => {})) as never });
    const receive = createFeishuIntake(ctx, createChatLanes(() => {}), createChannelIntakeJournal(undefined, 'feishu:pulse'));
    const msg = (id: string, type: string, content: Record<string, unknown>) => ({ message: { message_type: type, message_id: id,
      create_time: String(Date.now()), content: JSON.stringify(content) }, sender: { sender_id: { open_id: 'ou_eric123' } } });
    receive(msg('om_image', 'image', { image_key: 'img_slow' }));
    receive(msg('om_later', 'text', { text: 'after the image' }));
    receive(msg('om_stop', 'text', { text: '/stop' }));
    await vi.waitFor(() => expect(vi.mocked(ctx.sendTextMessage).mock.calls.length
      + vi.mocked(ctx.sendCardMessage).mock.calls.length).toBeGreaterThan(0));
    // The later message still waits behind the image in its lane.
    expect(ctx.pool.send).not.toHaveBeenCalled();
  });

  it('切片A：req.inbound 埋点落带 scope 的 feishuPoolKey（dual-channel 带 :feishu），与 CC turn/delivery 对齐', async () => {
    // 真实入站路径 UAT(单测层)：驱动 handleFeishuMessage，断言 req.inbound 事件的 session
    // 落带 scope 的池键——证明改动3 生效，与 turn.end/delivery(:feishu poolKey) 逐字符一致。
    const obsDir = mkdtempSync(join(tmpdir(), 'feishu-reqtrace-'));
    const saved = {
      trace: process.env.RELAY_OBS_REQUEST_TRACE,
      obs: process.env.RELAY_OBS_ENABLED,
      dir: process.env.RELAY_OBS_DIR,
    };
    process.env.RELAY_OBS_REQUEST_TRACE = 'true';
    process.env.RELAY_OBS_ENABLED = 'true';
    process.env.RELAY_OBS_DIR = obsDir;
    try {
      // dual-channel agent：有 botToken → feishuPoolKey 追加 :feishu scope。
      const dualAgent: AgentConfig = { ...feishuAgent, botToken: 'tg-bot-token-xyz' };
      const dualUser: UserConfig = { ...feishuUser, agents: [dualAgent] };
      const dualRouter = makeRouter({
        getAgents: vi.fn().mockReturnValue([dualAgent]),
        getAllUsers: vi.fn().mockReturnValue([dualUser]),
        resolveByFeishuAgent: vi.fn().mockReturnValue({ user: dualUser, agent: dualAgent }),
        getAllFeishuBots: vi.fn().mockReturnValue([{ agent: dualAgent, authorizedOpenIds: ['ou_eric123'] }]),
      });
      const ctx = makeCtx({ agent: dualAgent, router: dualRouter });

      await handleFeishuMessage(ctx, {
        message: { message_type: 'text', message_id: 'msg-slice-a', content: JSON.stringify({ text: 'hi' }) },
        sender: { sender_id: { open_id: 'ou_eric123' } },
      });

      // 读回 req.inbound 事件。
      const files = readdirSync(obsDir).filter((f) => f.startsWith('degen-') && f.endsWith('.jsonl'));
      const events: Record<string, unknown>[] = [];
      for (const f of files) {
        for (const l of readFileSync(join(obsDir, f), 'utf8').trim().split('\n').filter(Boolean)) events.push(JSON.parse(l));
      }
      const inbound = events.find((e) => e.evt === 'req.inbound');
      expect(inbound).toBeTruthy();
      // 关键断言：落带 scope 的池键（eric:pulse:feishu），不再是 bare eric:pulse。
      expect(inbound!.session).toBe('eric:pulse:feishu');
      expect(inbound!.channel).toBe('feishu');
      // 与 CC turn 实际 spawn 的池键同口径：dual-channel 池键即 eric:pulse:feishu。
      expect(ctx.pool.send).toHaveBeenCalledWith('eric:pulse:feishu', { type: 'human', content: expect.stringContaining('hi') });
    } finally {
      if (saved.trace === undefined) delete process.env.RELAY_OBS_REQUEST_TRACE; else process.env.RELAY_OBS_REQUEST_TRACE = saved.trace;
      if (saved.obs === undefined) delete process.env.RELAY_OBS_ENABLED; else process.env.RELAY_OBS_ENABLED = saved.obs;
      if (saved.dir === undefined) delete process.env.RELAY_OBS_DIR; else process.env.RELAY_OBS_DIR = saved.dir;
      rmSync(obsDir, { recursive: true, force: true });
    }
  });

  it('text reply includes fetched parent context in pool message', async () => {
    const ctx = makeCtx({
      fetchReplyText: vi.fn().mockResolvedValue('Amanda 发来的 WI 摘要'),
    } as Partial<MessageHandlerContext>);
    await handleFeishuMessage(ctx, {
      message: {
        message_type: 'text',
        message_id: 'msg1',
        parent_id: 'msg-parent',
        content: JSON.stringify({ text: '看一下这是什么消息' }),
      },
      sender: { sender_id: { open_id: 'ou_eric123' } },
    });

    expect(ctx.pool.send).toHaveBeenCalledWith('eric:pulse', {
      type: 'human',
      content: expect.stringContaining('Amanda 发来的 WI 摘要'),
    });
    expect(ctx.pool.send).toHaveBeenCalledWith('eric:pulse', {
      type: 'human',
      content: expect.stringContaining('看一下这是什么消息'),
    });
    expect(ctx.fetchReplyText).toHaveBeenCalledWith('msg-parent');
  });

  it('post with image + text delivers both image block and text to pool', async () => {
    const ctx = makeCtx();
    await handleFeishuMessage(ctx, {
      message: {
        message_type: 'post',
        message_id: 'msg-post-img',
        content: JSON.stringify({
          zh_cn: {
            title: '',
            content: [[
              { tag: 'text', text: '这个报错怎么看' },
              { tag: 'img', image_key: 'img_v3_err' },
            ]],
          },
        }),
      },
      sender: { sender_id: { open_id: 'ou_eric123' } },
    });

    expect(ctx.downloadImage).toHaveBeenCalledWith('msg-post-img', 'img_v3_err');
    const sendArgs = (ctx.pool.send as any).mock.calls.at(-1);
    const blocks = sendArgs[1].content as any[];
    expect(Array.isArray(blocks)).toBe(true);
    // 图片必须真的送进去——这正是大鸟报的「只看到文字」。
    expect(blocks.some(b => b.type === 'image')).toBe(true);
    expect(blocks.some(b => b.type === 'text' && String(b.text).includes('这个报错怎么看'))).toBe(true);
    const proof = humanMessageProof(sendArgs[1]);
    expect(proof).toBeDefined();
    const record = JSON.parse(readFileSync(join(process.env.SCL_HUMAN_DECISION_KEY_FILE!.replace(/[^/]+$/, ''), 'human-inputs', proof!.ref + '.json'), 'utf8'));
    expect(record.source).toEqual({channel:'feishu',eventId:'msg-post-img',humanRef:'eric',message:'这个报错怎么看'});
    expect(blocks.some(b => b.type === 'text' && String(b.text).includes(proof!.ref))).toBe(true);
  });

  it('text-only post still sends a plain string (no regression)', async () => {
    const ctx = makeCtx();
    await handleFeishuMessage(ctx, {
      message: {
        message_type: 'post',
        message_id: 'msg-post-text',
        content: JSON.stringify({
          zh_cn: { title: '', content: [[{ tag: 'text', text: '只有文字' }]] },
        }),
      },
      sender: { sender_id: { open_id: 'ou_eric123' } },
    });

    expect(ctx.downloadImage).not.toHaveBeenCalled();
    const sendArgs = (ctx.pool.send as any).mock.calls.at(-1);
    expect(typeof sendArgs[1].content).toBe('string');
    expect(sendArgs[1].content).toContain('只有文字');
    const proof = humanMessageProof(sendArgs[1]);
    expect(proof).toBeDefined();
    expect(sendArgs[1].content).toContain('Host human input reference: ' + proof!.ref);
    const record = JSON.parse(readFileSync(join(process.env.SCL_HUMAN_DECISION_KEY_FILE!.replace(/[^/]+$/, ''), 'human-inputs', proof!.ref + '.json'), 'utf8'));
    expect(record.poolKey).toBe('eric:pulse');
    expect(record.source).toEqual({channel:'feishu',eventId:'msg-post-text',humanRef:'eric',message:'只有文字'});
  });

  it('image-only post is not rejected as unparseable', async () => {
    const ctx = makeCtx();
    await handleFeishuMessage(ctx, {
      message: {
        message_type: 'post',
        message_id: 'msg-post-imgonly',
        content: JSON.stringify({
          zh_cn: { title: '', content: [[{ tag: 'img', image_key: 'img_only' }]] },
        }),
      },
      sender: { sender_id: { open_id: 'ou_eric123' } },
    });

    expect(ctx.sendTextMessage).not.toHaveBeenCalledWith(
      'ou_eric123',
      expect.stringContaining('无法解析富文本消息'),
    );
    const sendArgs = (ctx.pool.send as any).mock.calls.at(-1);
    expect((sendArgs[1].content as any[]).some(b => b.type === 'image')).toBe(true);
  });

  it('reply context reaches the pool verbatim when parent is a file message', async () => {
    // fetchReplyText 的真实实现在 createSingleFeishuBot 内部（未导出），其资源类
    // 解析由 describeFeishuResourceContent 用例直接锁死；这里锁的是「解出来的
    // 文件名能原样送达 Agent」这一段，即 aki 现场踩的那条链路。
    const ctx = makeCtx({
      fetchReplyText: vi.fn().mockResolvedValue('[Feishu file: weekly.epub]'),
    } as Partial<MessageHandlerContext>);
    await handleFeishuMessage(ctx, {
      message: {
        message_type: 'text',
        message_id: 'msg-reply-file',
        parent_id: 'msg-file-parent',
        content: JSON.stringify({ text: '这个附件讲了什么' }),
      },
      sender: { sender_id: { open_id: 'ou_eric123' } },
    });

    expect(ctx.fetchReplyText).toHaveBeenCalledWith('msg-file-parent');
    const sendArgs = (ctx.pool.send as any).mock.calls.at(-1);
    const sent = String(sendArgs[1].content);
    expect(sent).toContain('weekly.epub');
    expect(sent).not.toContain('[Feishu file message]');
  });

  it('text message from unregistered user starts pairing flow', async () => {
    const noRouter = makeRouter({
      resolveByFeishuAgent: vi.fn().mockReturnValue(undefined),
    });
    const ctx = makeCtx({ router: noRouter });
    await handleFeishuMessage(ctx, {
      message: { message_type: 'text', message_id: 'msg1', content: JSON.stringify({ text: 'hi' }) },
      sender: { sender_id: { open_id: 'ou_unknown' } },
    });
    expect(ctx.sendTextMessage).toHaveBeenCalledWith('ou_unknown', expect.stringContaining('配对码：'));
    expect(ctx.pool.send).not.toHaveBeenCalled();
  });

  it('dedupes duplicate Feishu delivery by message_id (pairing flow)', async () => {
    const noRouter = makeRouter({
      resolveByFeishuAgent: vi.fn().mockReturnValue(undefined),
    });
    const ctx = makeCtx({ router: noRouter });
    await handleFeishuMessage(ctx, {
      message: { message_type: 'text', message_id: 'dup_msg', content: JSON.stringify({ text: 'hi' }) },
      sender: { sender_id: { open_id: 'ou_unknown' } },
    });
    await handleFeishuMessage(ctx, {
      message: { message_type: 'text', message_id: 'dup_msg', content: JSON.stringify({ text: 'hi' }) },
      sender: { sender_id: { open_id: 'ou_unknown' } },
    });
    expect(vi.mocked(ctx.sendTextMessage).mock.calls.length).toBe(1);
  });

  it('image message downloads and sends to pool', async () => {
    const ctx = makeCtx();
    await handleFeishuMessage(ctx, {
      message: { message_type: 'image', message_id: 'msg2', content: JSON.stringify({ image_key: 'img_abc' }) },
      sender: { sender_id: { open_id: 'ou_eric123' } },
    });
    expect(ctx.downloadImage).toHaveBeenCalledWith('msg2', 'img_abc');
    expect(ctx.pool.send).toHaveBeenCalledWith('eric:pulse', {
      type: 'human',
      content: expect.arrayContaining([
        expect.objectContaining({
          type: 'image',
          source: { type: 'base64', media_type: 'image/jpeg', data: 'abc123' },
          filePath: expect.stringMatching(/relay-images-[^/]+\/\d+-[a-f0-9]+\.jpg$/),
        }),
      ]),
    });
  });

  it('image download failure sends error message', async () => {
    const ctx = makeCtx({
      downloadImage: vi.fn().mockRejectedValue(new Error('download failed')),
    });
    await handleFeishuMessage(ctx, {
      message: { message_type: 'image', message_id: 'msg2', content: JSON.stringify({ image_key: 'img_abc' }) },
      sender: { sender_id: { open_id: 'ou_eric123' } },
    });
    expect(ctx.sendTextMessage).toHaveBeenCalledWith('ou_eric123', '\u26A0\uFE0F \u56FE\u7247\u5904\u7406\u5931\u8D25\uFF0C\u8BF7\u7A0D\u540E\u91CD\u8BD5\u3002');
  });

  it('file message (PDF) downloads and sends document block to pool', async () => {
    const pdfBytes = Buffer.from('%PDF-1.4');
    const ctx = makeCtx({
      downloadFileResource: vi.fn().mockResolvedValue({
        buffer: pdfBytes,
        contentType: 'application/pdf',
      }),
    });
    await handleFeishuMessage(ctx, {
      message: {
        message_type: 'file',
        message_id: 'msg_file1',
        content: JSON.stringify({ file_key: 'fk_pdf', file_name: 'x.pdf' }),
      },
      sender: { sender_id: { open_id: 'ou_eric123' } },
    });
    expect(ctx.downloadFileResource).toHaveBeenCalledWith('msg_file1', 'fk_pdf');
    expect(ctx.pool.send).toHaveBeenCalledWith('eric:pulse', {
      type: 'human',
      content: [
        {
          type: 'document',
          source: {
            type: 'base64',
            media_type: 'application/pdf',
            data: pdfBytes.toString('base64'),
          },
        },
      ],
    });
  });

  it('file message text/plain decodes as text block', async () => {
    const ctx = makeCtx({
      downloadFileResource: vi.fn().mockResolvedValue({
        buffer: Buffer.from('hello feishu', 'utf-8'),
        contentType: 'text/plain',
      }),
    });
    await handleFeishuMessage(ctx, {
      message: {
        message_type: 'file',
        message_id: 'msg_file2',
        content: JSON.stringify({ file_key: 'fk_txt', file_name: 'n.txt' }),
      },
      sender: { sender_id: { open_id: 'ou_eric123' } },
    });
    expect(ctx.pool.send).toHaveBeenCalledWith('eric:pulse', {
      type: 'human',
      content: [{ type: 'text', text: '[文件: n.txt]\nhello feishu' }],
    });
  });

  it('file over RELAY_FEISHU_FILE_MAX_BYTES notifies user', async () => {
    const prev = process.env.RELAY_FEISHU_FILE_MAX_BYTES;
    process.env.RELAY_FEISHU_FILE_MAX_BYTES = '10';
    const ctx = makeCtx({
      downloadFileResource: vi.fn().mockResolvedValue({
        buffer: Buffer.alloc(20),
        contentType: 'application/pdf',
      }),
    });
    try {
      await handleFeishuMessage(ctx, {
        message: {
          message_type: 'file',
          message_id: 'msg_big',
          content: JSON.stringify({ file_key: 'fk_big' }),
        },
        sender: { sender_id: { open_id: 'ou_eric123' } },
      });
      expect(ctx.pool.send).not.toHaveBeenCalled();
      expect(ctx.sendTextMessage).toHaveBeenCalledWith('ou_eric123', expect.stringContaining('过大'));
    } finally {
      if (prev === undefined) delete process.env.RELAY_FEISHU_FILE_MAX_BYTES;
      else process.env.RELAY_FEISHU_FILE_MAX_BYTES = prev;
    }
  });

  it('file unsupported mime sends user hint', async () => {
    const ctx = makeCtx({
      downloadFileResource: vi.fn().mockResolvedValue({
        buffer: Buffer.from('x'),
        contentType: 'application/zip',
      }),
    });
    await handleFeishuMessage(ctx, {
      message: {
        message_type: 'file',
        message_id: 'msg_zip',
        content: JSON.stringify({ file_key: 'fk_z' }),
      },
      sender: { sender_id: { open_id: 'ou_eric123' } },
    });
    expect(ctx.pool.send).not.toHaveBeenCalled();
    expect(ctx.sendTextMessage).toHaveBeenCalledWith('ou_eric123', expect.stringContaining('application/zip'));
  });

  it('file download failure sends error message', async () => {
    const ctx = makeCtx({
      downloadFileResource: vi.fn().mockRejectedValue(new Error('api')),
    });
    await handleFeishuMessage(ctx, {
      message: {
        message_type: 'file',
        message_id: 'msg_fail',
        content: JSON.stringify({ file_key: 'fk_x' }),
      },
      sender: { sender_id: { open_id: 'ou_eric123' } },
    });
    expect(ctx.sendTextMessage).toHaveBeenCalledWith('ou_eric123', '\u26A0\uFE0F \u6587\u4EF6\u5904\u7406\u5931\u8D25\uFF0C\u8BF7\u7A0D\u540E\u91CD\u8BD5\u3002');
  });

  it('audio message downloads, transcribes, sends text to pool', async () => {
    const audioBuf = Buffer.from('fake-audio');
    const ctx = makeCtx({
      downloadFileResource: vi.fn().mockResolvedValue({ buffer: audioBuf, contentType: 'audio/ogg' }),
      transcribeInboundAudio: vi.fn().mockResolvedValue('\u4F60\u597D\u8BED\u97F3'),
    });
    await handleFeishuMessage(ctx, {
      message: {
        message_type: 'audio',
        message_id: 'msg_aud',
        content: JSON.stringify({ file_key: 'fk_voice' }),
      },
      sender: { sender_id: { open_id: 'ou_eric123' } },
    });
    expect(ctx.downloadFileResource).toHaveBeenCalledWith('msg_aud', 'fk_voice');
    expect(ctx.transcribeInboundAudio).toHaveBeenCalledWith(audioBuf);
    expect(ctx.pool.send).toHaveBeenCalledWith('eric:pulse', {
      type: 'human',
      content: '\u4F60\u597D\u8BED\u97F3',
    });
  });

  it('audio empty transcript notifies user', async () => {
    const ctx = makeCtx({
      transcribeInboundAudio: vi.fn().mockResolvedValue('   '),
    });
    await handleFeishuMessage(ctx, {
      message: {
        message_type: 'audio',
        message_id: 'msg_aud2',
        content: JSON.stringify({ file_key: 'fk_v2' }),
      },
      sender: { sender_id: { open_id: 'ou_eric123' } },
    });
    expect(ctx.pool.send).not.toHaveBeenCalled();
    expect(ctx.sendTextMessage).toHaveBeenCalledWith('ou_eric123', expect.stringContaining('\u672A\u80FD\u8BC6\u522B'));
  });

  it('audio transcribe failure sends error message', async () => {
    const ctx = makeCtx({
      transcribeInboundAudio: vi.fn().mockRejectedValue(new Error('stt down')),
    });
    await handleFeishuMessage(ctx, {
      message: {
        message_type: 'audio',
        message_id: 'msg_aud3',
        content: JSON.stringify({ file_key: 'fk_v3' }),
      },
      sender: { sender_id: { open_id: 'ou_eric123' } },
    });
    expect(ctx.sendTextMessage).toHaveBeenCalledWith('ou_eric123', '\u26A0\uFE0F \u97F3\u9891\u5904\u7406\u5931\u8D25\uFF0C\u8BF7\u7A0D\u540E\u91CD\u8BD5\u3002');
  });

  it('post message with emoji sends extracted text to pool', async () => {
    const ctx = makeCtx();
    await handleFeishuMessage(ctx, {
      message: {
        message_type: 'post',
        message_id: 'msg_post1',
        content: JSON.stringify({
          zh_cn: {
            title: '',
            content: [[{ tag: 'text', text: '状态 🟢' }]],
          },
        }),
      },
      sender: { sender_id: { open_id: 'ou_eric123' } },
    });
    expect(ctx.pool.send).toHaveBeenCalledWith('eric:pulse', { type: 'human', content: expect.stringContaining('状态 🟢') });
  });

  it('post message with command is handled as command', async () => {
    const ctx = makeCtx();
    await handleFeishuMessage(ctx, {
      message: {
        message_type: 'post',
        message_id: 'msg_post2',
        content: JSON.stringify({
          zh_cn: {
            title: '',
            content: [[{ tag: 'text', text: '/status' }]],
          },
        }),
      },
      sender: { sender_id: { open_id: 'ou_eric123' } },
    });
    expect(ctx.pool.send).not.toHaveBeenCalled();
    expect(ctx.sendTextMessage).toHaveBeenCalledWith('ou_eric123', expect.stringContaining(''));
  });

  it('post message with unparseable content sends error', async () => {
    const ctx = makeCtx();
    await handleFeishuMessage(ctx, {
      message: { message_type: 'post', message_id: 'msg_post3', content: 'not json' },
      sender: { sender_id: { open_id: 'ou_eric123' } },
    });
    expect(ctx.sendTextMessage).toHaveBeenCalledWith('ou_eric123', expect.stringContaining('无法解析'));
    expect(ctx.pool.send).not.toHaveBeenCalled();
  });

  it('unsupported message type sends hint', async () => {
    const ctx = makeCtx();
    await handleFeishuMessage(ctx, {
      message: { message_type: 'video', message_id: 'msg3', content: '' },
      sender: { sender_id: { open_id: 'ou_eric123' } },
    });
    expect(ctx.sendTextMessage).toHaveBeenCalledWith('ou_eric123', expect.stringContaining('video'));
  });

  it('missing openId is ignored', async () => {
    const ctx = makeCtx();
    await handleFeishuMessage(ctx, {
      message: { message_type: 'text', message_id: 'msg1', content: JSON.stringify({ text: 'hi' }) },
      sender: { sender_id: {} },
    });
    expect(ctx.pool.send).not.toHaveBeenCalled();
    expect(ctx.sendTextMessage).not.toHaveBeenCalled();
  });

  it('pool send failure sends error message', async () => {
    const ctx = makeCtx({
      pool: makePool({ send: vi.fn().mockRejectedValue(new Error('send failed')) }),
    });
    await handleFeishuMessage(ctx, {
      message: { message_type: 'text', message_id: 'msg1', content: JSON.stringify({ text: 'hi' }) },
      sender: { sender_id: { open_id: 'ou_eric123' } },
    });
    expect(ctx.sendTextMessage).toHaveBeenCalledWith('ou_eric123', '\u26A0\uFE0F \u6D88\u606F\u53D1\u9001\u5931\u8D25\uFF0C\u8BF7\u7A0D\u540E\u91CD\u8BD5\u3002');
  });

  it('stateManager.updateSession called after successful text send', async () => {
    const sm = { getSession: vi.fn().mockReturnValue(undefined), updateSession: vi.fn(), flush: vi.fn() } as unknown as StateManager;
    const ctx = makeCtx({ stateManager: sm });
    await handleFeishuMessage(ctx, {
      message: { message_type: 'text', message_id: 'msg1', content: JSON.stringify({ text: 'hi' }) },
      sender: { sender_id: { open_id: 'ou_eric123' } },
    });
    expect(sm.updateSession).toHaveBeenCalledWith('eric:pulse', expect.objectContaining({
      poolKey: 'eric:pulse',
      agentId: 'pulse',
      status: 'active',
    }));
  });

  it('spawn failure gives a safe explicit diagnostic', async () => {
    const ctx = makeCtx({
      pool: makePool({
        get: vi.fn().mockReturnValue(undefined),
        spawn: vi.fn().mockRejectedValue(new Error('spawn failed')),
      }),
    });
    await handleFeishuMessage(ctx, {
      message: { message_type: 'text', message_id: 'msg1', content: JSON.stringify({ text: 'hi' }) },
      sender: { sender_id: { open_id: 'ou_eric123' } },
    });
    expect(ctx.sendTextMessage).toHaveBeenCalledWith('ou_eric123', expect.stringContaining('[runtime_start_failed]'));
    expect(ctx.sendTextMessage).toHaveBeenCalledWith('ou_eric123', expect.stringContaining('未派发'));
  });
});

// --- handleCommand ---

describe('handleCommand', () => {
  it('/stop uses the runtime-native abort path for a busy Codex turn', async () => {
    const abortTurn = vi.fn();
    const ctx = makeCtx({
      pool: makePool({
        abortTurn,
        get: vi.fn().mockReturnValue({
          tenantId: 'eric:pulse',
          runtime: 'codex',
          sessionId: 'codex-session',
          state: 'busy',
          lastActivity: Date.now(),
          spawnedAt: Date.now(),
          restartCount: 0,
          restartWindowStart: Date.now(),
        }),
      }),
    });

    await handleCommand(ctx, '/stop', 'ou_eric123', { user: feishuUser });

    expect(abortTurn).toHaveBeenCalledWith('eric:pulse');
    expect(ctx.sendTextMessage).toHaveBeenCalledWith('ou_eric123', expect.stringContaining('已中断'));
  });

  it('/status calls command handler and sends result', async () => {
    const ctx = makeCtx();
    await handleCommand(ctx, '/status', 'ou_eric123', { user: feishuUser });
    expect(ctx.sendTextMessage).toHaveBeenCalledWith('ou_eric123', expect.stringContaining(''));
  });

  it('/help sends help text', async () => {
    const ctx = makeCtx();
    await handleCommand(ctx, '/help', 'ou_eric123', { user: feishuUser });
    expect(ctx.sendTextMessage).toHaveBeenCalled();
  });

  it('unsupported command sends hint', async () => {
    const ctx = makeCtx();
    await handleCommand(ctx, '/not-a-command', 'ou_eric123', { user: feishuUser });
    expect(ctx.sendTextMessage).toHaveBeenCalledWith('ou_eric123', expect.stringContaining('暂不支持'));
  });

  it('/approve writes an explicit active binding instead of agent feishuOpenId', async () => {
    const dir = mkdtempSync(join(tmpdir(), 'relay-feishu-approve-binding-'));
    try {
      const configPath = join(dir, 'relay.json');
      const configUser: UserConfig = {
        ...feishuUser,
        agents: [{ ...feishuAgent, feishuOpenId: undefined }],
      };
      const otherUser: UserConfig = {
        userId: 'other',
        feishuOpenId: 'ou_other_admin',
        defaultAgent: 'pulse',
        agents: [{ ...feishuAgent, feishuOpenId: 'ou_other_bound' }],
      };
      writeFileSync(configPath, JSON.stringify({ users: [configUser, otherUser] }, null, 2));

      const pairingCtx = makeCtx({
        router: makeRouter({
          resolveByFeishuAgent: vi.fn().mockReturnValue(undefined),
        }),
      });
      await handleFeishuMessage(pairingCtx, {
        message: { message_type: 'text', message_id: 'msg-pair', content: JSON.stringify({ text: 'pair me' }) },
        sender: { sender_id: { open_id: 'ou_new_user' } },
      });
      const pairingText = String(vi.mocked(pairingCtx.sendTextMessage).mock.calls[0][1]);
      const code = /配对码：([A-Z2-9]+)/.exec(pairingText)?.[1];
      expect(code).toBeDefined();

      const upsertGatewayBinding = vi.fn().mockReturnValue(true);
      const approveCtx = makeCtx({
        configPath,
        router: makeRouter({ upsertGatewayBinding }),
      } as Partial<MessageHandlerContext>);

      await handleCommand(approveCtx, `/approve ${code}`, 'ou_eric123', { user: configUser });

      const updated = JSON.parse(readFileSync(configPath, 'utf-8'));
      expect(updated.users[0].agents[0].feishuOpenId).toBeUndefined();
      expect(updated.users[1].agents[0].feishuOpenId).toBe('ou_other_bound');
      expect(updated.bindings).toHaveLength(1);
      expect(updated.bindings[0]).toMatchObject({
        principalId: 'eric',
        tenantId: 'eric',
        defaultAgentId: 'pulse',
        allowedAgentIds: ['pulse'],
        status: 'active',
        approvedBy: 'ou_eric123',
        address: {
          channel: 'feishu',
          kind: 'feishu_open_id',
          value: 'ou_new_user',
          appId: 'cli_test',
          botId: 'pulse',
        },
      });
      expect(upsertGatewayBinding).toHaveBeenCalledWith(expect.objectContaining({
        principalId: 'eric',
        status: 'active',
        address: expect.objectContaining({ value: 'ou_new_user' }),
      }));
    } finally {
      rmSync(dir, { recursive: true, force: true });
    }
  });
});

// --- ensureProcess ---

describe('ensureProcess', () => {
  it('resumes the bound WI identity despite stale route runtime and configured session', async () => {
    const sessionId='bound-pi-session';
    const usageBinding=buildRuntimeUsageBinding({workItemId:'wi_20990101_271',agentId:'pulse',
      phase:'work_item',epoch:1,runtime:'pi',procurementSource:'chatgpt_subscription',
      authorityRef:runtimeAuthorityRef(sessionId),issuedAt:'2026-09-01T00:00:00Z'});
    const persisted={sessionId,runtime:'pi',activeWorkItemRuntimeBinding:{
      schema:'pulse_active_work_item_runtime_binding/v1',workItemId:usageBinding.work_item_id,
      sessionId,sessionRef:runtimeAuthorityRef(sessionId),runtime:'pi',usageBinding,
      workspace:'/registered/wi-worktree'}};
    const ctx=makeCtx({stateManager:{getSession:()=>persisted,updateSession:vi.fn(),flush:vi.fn()} as unknown as StateManager});
    vi.mocked(ctx.pool.spawn).mockResolvedValue({sessionId,runtime:'pi',state:'ready',usageBindingSha256:usageBinding.binding_sha256} as any);
    const route={runtimePreference:['cc'],binding:{address:{channel:'feishu',value:'ou_eric123'}}} as RoutePolicyResult;
    expect(await ensureProcess(ctx,{user:feishuUser},route)).toBe('eric:pulse');
    expect(ctx.pool.spawn).toHaveBeenCalledWith(expect.objectContaining({runtime:'pi',
      conversationRuntimePreference:['pi'],sessionId,workspace:'/registered/wi-worktree'}));
    expect(vi.mocked(ctx.pool.spawn).mock.calls[0][0].computeCostBinding).toBeUndefined();
  });
  it('preserves the Codex default policy when rebuilding a process after Relay restart', () => {
    const codexAgent: AgentConfig = {
      ...feishuAgent,
      runtime: 'codex',
      conversationRuntimePreference: ['codex'],
      codexYoloDefault: true,
    };

    expect(buildFeishuResumeSpawnConfig(codexAgent, 'eric:pulse', {
      poolKey: 'eric:pulse',
      agentId: 'pulse',
      runtime: 'codex',
      sessionId: 'codex-session',
      codexThreadId: 'codex-thread',
      turnCount: 1,
      maxTurns: 1000,
      lastActivity: 123,
      status: 'active',
    })).toMatchObject({
      tenantId: 'eric:pulse',
      runtime: 'codex',
      conversationRuntimePreference: ['codex'],
      codexYoloDefault: true,
      codexThreadId: 'codex-thread',
      resume: true,
    });
  });

  it('returns poolKey when process exists', async () => {
    const ctx = makeCtx({
      pool: makePool({
        get: vi.fn().mockReturnValue({ state: 'ready' }),
      }),
    });
    const result = await ensureProcess(ctx, { user: feishuUser });
    expect(result).toBe('eric:pulse');
  });

  it('spawns process when none exists', async () => {
    const ctx = makeCtx({
      pool: makePool({
        get: vi.fn().mockReturnValue(undefined),
        spawn: vi.fn().mockResolvedValue({}),
      }),
    });
    const result = await ensureProcess(ctx, { user: feishuUser });
    expect(result).toBe('eric:pulse');
    expect(ctx.pool.spawn).toHaveBeenCalledWith(expect.objectContaining({
      tenantId: 'eric:pulse',
      sessionId: 'uuid-feishu',
    }));
  });

  it('passes configured conversation runtime to spawn', async () => {
    const codexAgent: AgentConfig = {
      ...feishuAgent,
      runtime: 'codex',
      conversationRuntimePreference: ['codex', 'cc'],
      codexYoloDefault: true,
    };
    const spawned = { runtime: 'codex', state: 'ready' };
    let current: typeof spawned | undefined;
    const spawn = vi.fn().mockImplementation(async () => { current = spawned; return spawned; });
    const ctx = makeCtx({
      agent: codexAgent,
      pool: makePool({
        get: vi.fn().mockImplementation(() => current),
        spawn,
      }),
    });
    const result = await ensureProcess(ctx, { user: { ...feishuUser, agents: [codexAgent] } });
    expect(result).toBe('eric:pulse');
    expect(spawn).toHaveBeenCalledWith(expect.objectContaining({
      tenantId: 'eric:pulse',
      runtime: 'codex',
      conversationRuntimePreference: ['codex', 'cc'],
      sessionId: 'uuid-feishu',
      codexYoloDefault: true,
    }));
  });

  it('passes runtime: cc-sdk through to spawn so the SDK adapter is selected', async () => {
    // Feishu was collapsing cc-sdk agents onto the legacy cc pool path; the
    // spawn request must carry cc-sdk (no route → routing.runtime path).
    const sdkAgent: AgentConfig = {
      ...feishuAgent,
      runtime: 'cc-sdk',
      conversationRuntimePreference: undefined,
    };
    const spawn = vi.fn().mockResolvedValue({ runtime: 'cc-sdk', state: 'ready' });
    const ctx = makeCtx({
      agent: sdkAgent,
      pool: makePool({
        get: vi.fn().mockReturnValue(undefined),
        spawn,
      }),
    });
    const result = await ensureProcess(ctx, { user: { ...feishuUser, agents: [sdkAgent] } });
    expect(result).toBe('eric:pulse');
    expect(spawn).toHaveBeenCalledWith(expect.objectContaining({
      tenantId: 'eric:pulse',
      runtime: 'cc-sdk',
      sessionId: 'uuid-feishu',
    }));
  });

  it('carries cc-sdk from a RoutePolicyResult runtimePreference into spawn', async () => {
    // When the gateway route resolves cc-sdk, ensureProcess must forward it
    // instead of falling back to cc (the route path is what production uses).
    const sdkAgent: AgentConfig = {
      ...feishuAgent,
      runtime: 'cc-sdk',
      conversationRuntimePreference: undefined,
    };
    const route: RoutePolicyResult = {
      principal: { id: 'eric', type: 'human', tenantId: 'eric' },
      binding: {
        id: 'binding-sdk-feishu-pulse',
        address: { channel: 'feishu', kind: 'feishu_open_id', value: 'ou_eric123', appId: 'cli_test', botId: 'pulse' },
        principalId: 'eric',
        tenantId: 'eric',
        workspaceId: '/tmp/ws',
        defaultAgentId: 'pulse',
        allowedAgentIds: ['pulse'],
      },
      agentId: 'pulse',
      workspace: '/tmp/ws',
      sessionId: 'uuid-feishu',
      runtimePreference: ['cc-sdk'],
      conversationKey: 'eric:pulse',
      audit: { correlationId: 'evt-sdk', source: 's1-inbound' },
    };
    const spawn = vi.fn().mockResolvedValue({ runtime: 'cc-sdk', state: 'ready' });
    const ctx = makeCtx({
      agent: sdkAgent,
      pool: makePool({
        get: vi.fn().mockReturnValue(undefined),
        spawn,
      }),
    });
    const result = await ensureProcess(ctx, { user: { ...feishuUser, agents: [sdkAgent] } }, route);
    expect(result).toBe('eric:pulse');
    expect(spawn).toHaveBeenCalledWith(expect.objectContaining({
      tenantId: 'eric:pulse',
      runtime: 'cc-sdk',
      conversationRuntimePreference: ['cc-sdk'],
      sessionId: 'uuid-feishu',
    }));
  });

  it('uses a channel-scoped pool key for dual-channel agents', async () => {
    const dualChannelAgent: AgentConfig = {
      ...feishuAgent,
      botToken: 'fake-tg-token',
    };
    const dualChannelUser: UserConfig = {
      ...feishuUser,
      agents: [dualChannelAgent],
    };
    const spawn = vi.fn().mockResolvedValue({ runtime: 'codex', state: 'ready' });
    const ctx = makeCtx({
      agent: dualChannelAgent,
      pool: makePool({
        get: vi.fn().mockReturnValue(undefined),
        spawn,
      }),
    });

    const result = await ensureProcess(ctx, { user: dualChannelUser });

    expect(result).toBe('eric:pulse:feishu');
    expect(spawn).toHaveBeenCalledWith(expect.objectContaining({
      tenantId: 'eric:pulse:feishu',
      sessionId: 'uuid-feishu',
    }));
    expect(ctx.poolKeyToOpenId.get('eric:pulse:feishu')).toBe('ou_eric123');
  });

  it('maps explicit route binding openId when legacy openId fields are absent', async () => {
    const userWithoutLegacyOpenId: UserConfig = {
      userId: 'eric',
      defaultAgent: 'pulse',
      agents: [feishuAgent],
    };
    const route: RoutePolicyResult = {
      principal: { id: 'eric', type: 'human', tenantId: 'eric' },
      binding: {
        id: 'binding-explicit-feishu-pulse',
        address: { channel: 'feishu', kind: 'feishu_open_id', value: 'ou_explicit', appId: 'cli_test', botId: 'pulse' },
        principalId: 'eric',
        tenantId: 'eric',
        workspaceId: '/tmp/ws',
        defaultAgentId: 'pulse',
        allowedAgentIds: ['pulse'],
      },
      agentId: 'pulse',
      workspace: '/tmp/ws',
      sessionId: 'uuid-feishu',
      runtimePreference: ['cc'],
      conversationKey: 'eric:pulse',
      audit: { correlationId: 'evt-explicit', source: 's1-inbound' },
    };
    const ctx = makeCtx({
      pool: makePool({
        get: vi.fn().mockReturnValue(undefined),
        spawn: vi.fn().mockResolvedValue({ runtime: 'cc', state: 'ready' }),
      }),
    });

    const result = await ensureProcess(ctx, { user: userWithoutLegacyOpenId }, route);

    expect(result).toBe('eric:pulse');
    expect(ctx.poolKeyToOpenId.get('eric:pulse')).toBe('ou_explicit');
  });

  it('uses RoutePolicyResult runtimePreference when handling a text message', async () => {
    const routeAgent: AgentConfig = {
      ...feishuAgent,
      runtime: 'codex',
      conversationRuntimePreference: ['codex', 'cc'],
    };
    const routeUser: UserConfig = {
      ...feishuUser,
      agents: [routeAgent],
    };
    const spawned = { runtime: 'codex', state: 'ready' };
    let current: typeof spawned | undefined;
    const spawn = vi.fn().mockImplementation(async () => { current = spawned; return spawned; });
    const ctx = makeCtx({
      agent: { ...feishuAgent, runtime: 'cc', conversationRuntimePreference: undefined },
      router: makeRouter({
        getAllUsers: vi.fn().mockReturnValue([routeUser]),
        resolveByFeishuAgent: vi.fn().mockReturnValue({ user: routeUser, agent: routeAgent }),
      }),
      pool: makePool({
        get: vi.fn().mockImplementation(() => current),
        spawn,
      }),
    });

    await handleFeishuMessage(ctx, {
      message: { message_type: 'text', message_id: 'msg-route-runtime', content: JSON.stringify({ text: 'hi' }) },
      sender: { sender_id: { open_id: 'ou_eric123' } },
    });

    expect(spawn).toHaveBeenCalledWith(expect.objectContaining({
      tenantId: 'eric:pulse',
      runtime: 'codex',
      conversationRuntimePreference: ['codex', 'cc'],
    }));
    expect(ctx.pool.send).toHaveBeenCalledWith('eric:pulse', { type: 'human', content: expect.stringContaining('hi') });
  });

  it('returns null on spawn failure', async () => {
    const ctx = makeCtx({
      pool: makePool({
        get: vi.fn().mockReturnValue(undefined),
        spawn: vi.fn().mockRejectedValue(new Error('fail')),
      }),
    });
    const result = await ensureProcess(ctx, { user: feishuUser });
    expect(result).toBeNull();
  });
});

// --- handlePoolResponse ---

describe('handlePoolResponse', () => {
  it.each([
    { name: 'dual-channel scoped', key: 'eric:pulse:feishu', agent: { ...feishuAgent, botToken: 'fake-telegram-token' }, scope: 'feishu' },
    { name: 'Feishu-only legacy', key: 'eric:pulse', agent: feishuAgent, scope: undefined },
  ])('restores the $name session to the correct agent and channel', ({ key, agent, scope }) => {
    const handlers = new Map<string, (...args: unknown[]) => void>();
    const pool = makePool({
      on: vi.fn((event: string, handler: (...args: unknown[]) => void) => handlers.set(event, handler)),
    });
    const router = makeRouter({ overrideSessionId: vi.fn(), getAgents: vi.fn().mockReturnValue([agent]) });
    const stateManager = {
      updateSession: vi.fn(),
      flush: vi.fn(),
    } as unknown as StateManager;
    const sendText = vi.fn();
    wirePoolEvents(agent, pool, new Map([[key, 'ou_eric123']]), router,
      sendText, vi.fn(), undefined, undefined, undefined, stateManager);

    handlers.get('session-chain-recovery')?.(key, { newSessionId: 'recovered-feishu-session' });

    expect(router.overrideSessionId).toHaveBeenCalledWith('eric', 'pulse', 'recovered-feishu-session', scope);
    expect(stateManager.updateSession).toHaveBeenCalledWith(key, expect.objectContaining({ sessionId: 'recovered-feishu-session' }));
    expect(stateManager.flush).toHaveBeenCalledOnce();
    expect(sendText).toHaveBeenCalledWith('ou_eric123', expect.stringContaining('已自动开启新对话'));
  });

  it('delivers one safe native failure notice to Feishu and suppresses the generic exit toast', async () => {
    const handlers = new Map<string, (...args: unknown[]) => void>();
    const pool = makePool({
      get: vi.fn(() => ({ runtime: 'cc-sdk' } as never)),
      on: vi.fn((event: string, handler: (...args: unknown[]) => void) => handlers.set(event, handler)),
    });
    const sendText = vi.fn();
    const log = vi.spyOn(console, 'error').mockImplementation(() => undefined);
    try {
      wirePoolEvents(feishuAgent, pool, new Map([['eric:pulse', 'ou_eric123']]),
        makeRouter(), sendText, vi.fn());
      handlers.get('turn-accepted')?.('eric:pulse');
      handlers.get('response')?.('eric:pulse', {
        type: 'result', is_error: true, errors: ['upstream ECONNRESET token=secret-key'],
      });
      await vi.waitFor(() => expect(sendText).toHaveBeenCalledTimes(1));
      handlers.get('error')?.('eric:pulse', new Error('upstream ECONNRESET token=secret-key'));
      handlers.get('exit')?.('eric:pulse', 1);
      expect(sendText).toHaveBeenCalledTimes(1);
      expect(sendText.mock.calls[0]?.[1]).toContain('ECONNRESET');
      expect(sendText.mock.calls[0]?.[1]).not.toContain('secret-key');
      handlers.get('turn-accepted')?.('eric:pulse');
      handlers.get('error')?.('eric:pulse', new TypeError('fetch failed', { cause: new Error('ENOTFOUND token=secret-key') }));
      await vi.waitFor(() => expect(sendText).toHaveBeenCalledTimes(2));
      expect(sendText.mock.calls[1]?.[1]).toContain('ENOTFOUND');
      expect(sendText.mock.calls[1]?.[1]).not.toContain('secret-key');
      handlers.get('turn-accepted')?.('eric:pulse');
      handlers.get('response')?.('eric:pulse', { type: 'result', is_error: true,
        errors: [{ message: 'Native parser rejected response schema: missing output_items token=secret-key',
          code: 'invalid_response', status: 422 }], subtype: 'error_during_execution' });
      await vi.waitFor(() => expect(sendText).toHaveBeenCalledTimes(3));
      expect(sendText.mock.calls[2]?.[1]).toContain('missing output_items');
      expect(sendText.mock.calls[2]?.[1]).toContain('invalid_response');
      expect(sendText.mock.calls[2]?.[1]).toContain('422');
      expect(sendText.mock.calls[2]?.[1]).not.toContain('secret-key');
      handlers.get('error')?.('eric:pulse', new Error('Native parser rejected response schema'));
      expect(sendText).toHaveBeenCalledTimes(3);
    } finally {
      log.mockRestore();
    }
  });

  it('opens a visible transient busy card as soon as a turn is accepted', async () => {
    const handlers = new Map<string, (...args: unknown[]) => void>();
    const pool = makePool({
      on: vi.fn((event: string, handler: (...args: unknown[]) => void) => handlers.set(event, handler)),
    });
    const beginTurn = vi.fn();
    const onAssistant = vi.fn(async () => undefined);
    const streaming = {
      enabled: true as const,
      beginTurn,
      onAssistant,
      onPulse: vi.fn(),
      onResult: vi.fn(async () => undefined),
    };
    wirePoolEvents(
      feishuAgent,
      pool,
      new Map([['eric:pulse', 'ou_eric123']]),
      makeRouter(),
      vi.fn(),
      vi.fn(),
      undefined,
      streaming,
    );

    handlers.get('turn-accepted')?.('eric:pulse');
    await Promise.resolve();

    expect(beginTurn).toHaveBeenCalledWith('eric:pulse', undefined);
    expect(onAssistant).toHaveBeenCalledWith(
      'eric:pulse',
      'ou_eric123',
      '▶️ 已收到，开始处理…',
      'status',
      'eric:pulse:accepted',
    );
  });

  it('surfaces every actual shared activity count for transport coalescing for tool-only runtime work', async () => {
    const handlers = new Map<string, (...args: unknown[]) => void>();
    const pool = makePool({
      on: vi.fn((event: string, handler: (...args: unknown[]) => void) => handlers.set(event, handler)),
    });
    const onAssistant = vi.fn(async () => undefined);
    const streaming = {
      enabled: true as const,
      beginTurn: vi.fn(),
      onAssistant,
      onPulse: vi.fn(),
      onResult: vi.fn(async () => undefined),
    };
    wirePoolEvents(
      feishuAgent,
      pool,
      new Map([['eric:pulse', 'ou_eric123']]),
      makeRouter(),
      vi.fn(),
      vi.fn(),
      undefined,
      streaming,
    );

    handlers.get('turn-accepted')?.('eric:pulse');
    onAssistant.mockClear();
    for (let step = 1; step <= 6; step += 1) handlers.get('turn-effect')?.('eric:pulse', 'no-effect');
    await Promise.resolve();

    expect(onAssistant).toHaveBeenCalledTimes(6);
    expect(onAssistant).toHaveBeenNthCalledWith(
      1,
      'eric:pulse',
      'ou_eric123',
      '⏳ 仍在工作，已推进 1 个执行步骤…',
      'activity',
      'eric:pulse:activity:1',
    );
    expect(onAssistant).toHaveBeenNthCalledWith(
      5,
      'eric:pulse',
      'ou_eric123',
      '⏳ 仍在工作，已推进 5 个执行步骤…',
      'activity',
      'eric:pulse:activity:5',
    );
  });

  it('rejects workspace tool files and only deletes validated outbox artifacts', async () => {
    const workspace = mkdtempSync(join(tmpdir(), 'relay-feishu-outbox-'));
    temporaryWorkspaces.push(workspace);
    const ordinary = join(workspace, 'evidence.yaml');
    const outbox = resolveToolFileOutboxPath('eric:pulse', workspace);
    const deliverable = join(outbox, 'report.yaml');
    // Another channel session of the same agent shares the workspace.
    const otherSessionOutbox = resolveToolFileOutboxPath('eric:pulse:telegram', workspace);
    const otherSessionFile = join(otherSessionOutbox, 'not-mine.md');
    mkdirSync(outbox, { recursive: true });
    mkdirSync(otherSessionOutbox, { recursive: true });
    writeFileSync(ordinary, 'must survive');
    writeFileSync(deliverable, 'send me');
    writeFileSync(otherSessionFile, 'belongs to another conversation');

    const handlers = new Map<string, (...args: unknown[]) => void>();
    const pool = makePool({
      on: vi.fn((event: string, handler: (...args: unknown[]) => void) => handlers.set(event, handler)),
    });
    const upload = vi.fn().mockImplementation(async (payload: { data: { file: NodeJS.ReadableStream } }) => {
      await new Promise<void>((resolve, reject) => {
        payload.data.file.once('error', reject);
        payload.data.file.once('end', resolve);
        payload.data.file.resume();
      });
      return { file_key: 'file-key' };
    });
    const send = vi.fn().mockResolvedValue({ data: { message_id: 'message-id' } });
    const warn = vi.spyOn(console, 'warn').mockImplementation(() => undefined);

    wirePoolEvents(
      feishuAgent,
      pool,
      new Map([['eric:pulse', 'ou_eric123']]),
      makeRouter(),
      vi.fn(),
      vi.fn(),
      undefined,
      undefined,
      { im: { message: { create: send }, file: { create: upload } } },
    );

    handlers.get('tool-files')?.('eric:pulse', [ordinary, otherSessionFile, deliverable], workspace);
    await vi.waitFor(() => expect(existsSync(deliverable)).toBe(false));

    expect(existsSync(ordinary)).toBe(true);
    expect(existsSync(otherSessionFile)).toBe(true);
    expect(warn).toHaveBeenCalledWith(expect.objectContaining({
      msg: 'Tool file delivery rejected',
      path: otherSessionFile,
    }));
    expect(upload).toHaveBeenCalledTimes(1);
    expect(send).toHaveBeenCalledTimes(1);
    expect(warn).toHaveBeenCalledWith(expect.objectContaining({
      msg: 'Tool file delivery rejected',
      path: ordinary,
    }));
  });

  it('sends card for assistant response with end_turn', async () => {
    const cards: Array<{ openId: string; card: Record<string, unknown> }> = [];
    const sendCard = (openId: string, card: Record<string, unknown>) => { cards.push({ openId, card }); };
    const poolKeyToOpenId = new Map([['eric:pulse', 'ou_eric123']]);

    await handleFeishuPoolResponse(feishuAgent, 'eric:pulse', {
      type: 'assistant',
      message: { stop_reason: 'end_turn', content: [{ type: 'text', text: 'Hello!' }] },
    } as StreamJsonMessage, poolKeyToOpenId, sendCard);

    expect(cards.length).toBe(1);
    expect(cards[0].openId).toBe('ou_eric123');
    expect(cards[0].card.header).toBeUndefined();
  });

  it('sends card for print-mode assistant response (stop_reason=null with text)', async () => {
    const cards: Array<{ openId: string; card: Record<string, unknown> }> = [];
    const sendCard = (openId: string, card: Record<string, unknown>) => { cards.push({ openId, card }); };
    const poolKeyToOpenId = new Map([['eric:pulse', 'ou_eric123']]);

    await handleFeishuPoolResponse(feishuAgent, 'eric:pulse', {
      type: 'assistant',
      message: { stop_reason: null, content: [{ type: 'text', text: '你好！' }] },
    } as StreamJsonMessage, poolKeyToOpenId, sendCard);

    expect(cards.length).toBe(1);
    expect(cards[0].openId).toBe('ou_eric123');
  });

  it('ignores tool_use-only assistant records (print mode intermediate)', async () => {
    const cards: Array<{ openId: string; card: Record<string, unknown> }> = [];
    const sendCard = (openId: string, card: Record<string, unknown>) => { cards.push({ openId, card }); };
    const poolKeyToOpenId = new Map([['eric:pulse', 'ou_eric123']]);

    await handleFeishuPoolResponse(feishuAgent, 'eric:pulse', {
      type: 'assistant',
      message: { stop_reason: null, content: [{ type: 'tool_use', id: 'x', name: 'Bash', input: {} }] },
    } as StreamJsonMessage, poolKeyToOpenId, sendCard);

    expect(cards.length).toBe(0);
  });

  // AskUserQuestion interactive card was removed 2026-06-26 (dormant since the
  // 2026-06-19 print-mode disable, commits 2397a1ed/679e14fc). The tool_use block
  // is now suppressed — no card, no pulse line. This is the current contract.
  it('suppresses AskUserQuestion tool_use (card feature removed)', async () => {
    const cards: Array<{ openId: string; card: Record<string, unknown> }> = [];
    const sendCard = (openId: string, card: Record<string, unknown>) => { cards.push({ openId, card }); };
    const poolKeyToOpenId = new Map([['eric:pulse', 'ou_eric123']]);

    await handleFeishuPoolResponse(feishuAgent, 'eric:pulse', {
      type: 'assistant',
      message: {
        stop_reason: 'tool_use',
        content: [{
          type: 'tool_use',
          id: 'call_color_1',
          name: 'AskUserQuestion',
          input: {
            questions: [{
              question: '你喜欢哪种颜色？',
              header: '颜色偏好',
              options: [{ label: '红色' }, { label: '蓝色' }],
            }],
          },
        }],
      },
    } as StreamJsonMessage, poolKeyToOpenId, sendCard);

    expect(cards).toHaveLength(0);
  });

  it('ignores non-assistant messages', async () => {
    const cards: Array<{ openId: string; card: Record<string, unknown> }> = [];
    const sendCard = (openId: string, card: Record<string, unknown>) => { cards.push({ openId, card }); };
    const poolKeyToOpenId = new Map([['eric:pulse', 'ou_eric123']]);

    await handleFeishuPoolResponse(feishuAgent, 'eric:pulse', {
      type: 'result',
    } as StreamJsonMessage, poolKeyToOpenId, sendCard);

    expect(cards.length).toBe(0);
  });

  it('ignores messages for other agents', async () => {
    const cards: Array<{ openId: string; card: Record<string, unknown> }> = [];
    const sendCard = (openId: string, card: Record<string, unknown>) => { cards.push({ openId, card }); };
    const poolKeyToOpenId = new Map([['owner:main', 'ou_eric123']]);

    await handleFeishuPoolResponse(feishuAgent, 'owner:main', {
      type: 'assistant',
      message: { stop_reason: 'end_turn', content: [{ type: 'text', text: 'Hello!' }] },
    } as StreamJsonMessage, poolKeyToOpenId, sendCard);

    expect(cards.length).toBe(0);
  });

  it('does not deliver artificer pool key from a different feishu-only bot (stale map)', async () => {
    const backyard: AgentConfig = {
      ...feishuAgent,
      id: 'backyard',
      name: 'Backyard',
    };
    const cards: Array<{ openId: string; card: Record<string, unknown> }> = [];
    const sendCard = (openId: string, card: Record<string, unknown>) => { cards.push({ openId, card }); };
    const poolKeyToOpenId = new Map([['eric:artificer:feishu', 'ou_user_on_backyard_app']]);

    await handleFeishuPoolResponse(backyard, 'eric:artificer:feishu', {
      type: 'assistant',
      message: { stop_reason: 'end_turn', content: [{ type: 'text', text: 'Wrong personality' }] },
    } as StreamJsonMessage, poolKeyToOpenId, sendCard);

    expect(cards).toHaveLength(0);
  });

  it('isolates dual-channel Feishu responses from the Telegram pool key', async () => {
    const dualChannelAgent: AgentConfig = {
      ...feishuAgent,
      botToken: 'fake-tg-token',
    };
    const cards: Array<{ openId: string; card: Record<string, unknown> }> = [];
    const sendCard = (openId: string, card: Record<string, unknown>) => { cards.push({ openId, card }); };
    const poolKeyToOpenId = new Map([
      ['eric:pulse', 'ou_tg_should_not_receive'],
      ['eric:pulse:feishu', 'ou_eric123'],
    ]);

    await handleFeishuPoolResponse(dualChannelAgent, 'eric:pulse', {
      type: 'assistant',
      message: { stop_reason: 'end_turn', content: [{ type: 'text', text: 'TG key should be ignored' }] },
    } as StreamJsonMessage, poolKeyToOpenId, sendCard);

    expect(cards).toHaveLength(0);

    await handleFeishuPoolResponse(dualChannelAgent, 'eric:pulse:feishu', {
      type: 'assistant',
      message: { stop_reason: 'end_turn', content: [{ type: 'text', text: 'Feishu only' }] },
    } as StreamJsonMessage, poolKeyToOpenId, sendCard);

    expect(cards).toHaveLength(1);
    expect(cards[0].openId).toBe('ou_eric123');
  });

  it('strips CC noise from response', async () => {
    const cards: Array<{ openId: string; card: Record<string, unknown> }> = [];
    const sendCard = (openId: string, card: Record<string, unknown>) => { cards.push({ openId, card }); };
    const poolKeyToOpenId = new Map([['eric:pulse', 'ou_eric123']]);

    await handleFeishuPoolResponse(feishuAgent, 'eric:pulse', {
      type: 'assistant',
      message: { stop_reason: 'end_turn', content: [{ type: 'text', text: 'real answer\nOAuth轮询超时' }] },
    } as StreamJsonMessage, poolKeyToOpenId, sendCard);

    expect(cards.length).toBe(1);
    const mdContent = ((cards[0].card as any).body.elements as any[])[0].content;
    expect(mdContent).toBe('real answer');
  });

  it('ignores empty text content', async () => {
    const cards: Array<{ openId: string; card: Record<string, unknown> }> = [];
    const sendCard = (openId: string, card: Record<string, unknown>) => { cards.push({ openId, card }); };
    const poolKeyToOpenId = new Map([['eric:pulse', 'ou_eric123']]);

    await handleFeishuPoolResponse(feishuAgent, 'eric:pulse', {
      type: 'assistant',
      message: { stop_reason: 'end_turn', content: [] },
    } as StreamJsonMessage, poolKeyToOpenId, sendCard);

    expect(cards.length).toBe(0);
  });

  it('ignores when no openId mapping exists', async () => {
    const cards: Array<{ openId: string; card: Record<string, unknown> }> = [];
    const sendCard = (openId: string, card: Record<string, unknown>) => { cards.push({ openId, card }); };
    const poolKeyToOpenId = new Map<string, string>();

    await handleFeishuPoolResponse(feishuAgent, 'eric:pulse', {
      type: 'assistant',
      message: { stop_reason: 'end_turn', content: [{ type: 'text', text: 'Hello!' }] },
    } as StreamJsonMessage, poolKeyToOpenId, sendCard);

    expect(cards.length).toBe(0);
  });

  it('delegates to streaming sink when enabled', async () => {
    const sendCard = vi.fn();
    const poolKeyToOpenId = new Map([['eric:pulse', 'ou_eric123']]);
    const onAssistant = vi.fn().mockResolvedValue(undefined);
    const onResult = vi.fn().mockResolvedValue(undefined);
    const onPulse = vi.fn();
    const streaming = { enabled: true as const, onAssistant, onResult, onPulse };

    await handleFeishuPoolResponse(feishuAgent, 'eric:pulse', {
      type: 'assistant',
      message: { stop_reason: 'end_turn', content: [{ type: 'text', text: 'streamed' }] },
    } as StreamJsonMessage, poolKeyToOpenId, sendCard, streaming);

    expect(sendCard).not.toHaveBeenCalled();
    expect(onAssistant).toHaveBeenCalledWith('eric:pulse', 'ou_eric123', 'streamed', 'final', undefined);
    expect(onPulse).toHaveBeenCalledWith('eric:pulse', null);

    await handleFeishuPoolResponse(feishuAgent, 'eric:pulse', { type: 'result' } as StreamJsonMessage, poolKeyToOpenId, sendCard, streaming);
    expect(onResult).toHaveBeenCalledWith('eric:pulse');
  });

  it('forwards tool_use blocks as a pulse line (no text)', async () => {
    const sendCard = vi.fn();
    const poolKeyToOpenId = new Map([['eric:pulse', 'ou_eric123']]);
    const onAssistant = vi.fn().mockResolvedValue(undefined);
    const onResult = vi.fn().mockResolvedValue(undefined);
    const onPulse = vi.fn();
    const streaming = { enabled: true as const, onAssistant, onResult, onPulse };

    await handleFeishuPoolResponse(feishuAgent, 'eric:pulse', {
      type: 'assistant',
      message: { stop_reason: null, content: [{ type: 'tool_use', name: 'Bash', input: { command: 'ssh ownermac echo hi', description: 'echo' } }] },
    } as StreamJsonMessage, poolKeyToOpenId, sendCard, streaming);

    expect(onAssistant).not.toHaveBeenCalled();
    expect(sendCard).not.toHaveBeenCalled();
    expect(onPulse).toHaveBeenCalledTimes(1);
    const [pk, line] = onPulse.mock.calls[0];
    expect(pk).toBe('eric:pulse');
    expect(line).toMatch(/^🔧 Bash\(/);
    expect(line).toContain('ssh ownermac');
  });
});

// --- handleSessionEnd ---

describe('handleSessionEnd', () => {
  it('turns-exhausted sends notification', () => {
    const messages: Array<{ openId: string; text: string }> = [];
    const sendText = (openId: string, text: string) => { messages.push({ openId, text }); };
    const poolKeyToOpenId = new Map([['eric:pulse', 'ou_eric123']]);

    handleSessionEnd(feishuAgent, 'eric:pulse', { type: 'turns-exhausted', turnCount: 100, maxTurns: 100 }, poolKeyToOpenId, sendText, makeRouter());
    expect(messages.length).toBe(1);
    expect(messages[0].text).toContain('轮次上限');
  });

  it('crash sends notification', () => {
    const messages: Array<{ openId: string; text: string }> = [];
    const sendText = (openId: string, text: string) => { messages.push({ openId, text }); };
    const poolKeyToOpenId = new Map([['eric:pulse', 'ou_eric123']]);

    handleSessionEnd(feishuAgent, 'eric:pulse', { type: 'crash', exitCode: 1 }, poolKeyToOpenId, sendText, makeRouter());
    expect(messages.length).toBe(1);
    expect(messages[0].text).toContain('自动恢复');
  });

  it('rate-limited sends user-actionable notification', () => {
    const messages: Array<{ openId: string; text: string }> = [];
    const sendText = (openId: string, text: string) => { messages.push({ openId, text }); };
    const poolKeyToOpenId = new Map([['eric:pulse', 'ou_eric123']]);

    handleSessionEnd(feishuAgent, 'eric:pulse', { type: 'rate-limited', retryCount: 0 }, poolKeyToOpenId, sendText, makeRouter());
    expect(messages.length).toBe(1);
    expect(messages[0].text).toContain('限流');
    expect(messages[0].text).toContain('/new');
  });

  it('crash-unrecoverable sends notification', () => {
    const messages: Array<{ openId: string; text: string }> = [];
    const sendText = (openId: string, text: string) => { messages.push({ openId, text }); };
    const poolKeyToOpenId = new Map([['eric:pulse', 'ou_eric123']]);

    handleSessionEnd(feishuAgent, 'eric:pulse', { type: 'crash-unrecoverable', exitCode: 1 }, poolKeyToOpenId, sendText, makeRouter());
    expect(messages.length).toBe(1);
    expect(messages[0].text).toContain('多次崩溃');
  });

  it('session-in-use sends actionable hint', () => {
    const messages: Array<{ openId: string; text: string }> = [];
    const sendText = (openId: string, text: string) => { messages.push({ openId, text }); };
    const poolKeyToOpenId = new Map([['eric:pulse', 'ou_eric123']]);

    handleSessionEnd(feishuAgent, 'eric:pulse', { type: 'session-in-use' }, poolKeyToOpenId, sendText, makeRouter());
    expect(messages.length).toBe(1);
    expect(messages[0].text).toContain('/new');
    expect(messages[0].text).toContain('进程');
  });

  it('relay-shutdown sends notification only when user is mid-turn (wasBusy)', () => {
    const poolKeyToOpenId = new Map([['eric:pulse', 'ou_eric123']]);

    // wasBusy=true: user is waiting → notify
    const messagesBusy: Array<{ openId: string; text: string }> = [];
    handleSessionEnd(
      feishuAgent,
      'eric:pulse',
      { type: 'relay-shutdown', wasBusy: true },
      poolKeyToOpenId,
      (openId, text) => { messagesBusy.push({ openId, text }); },
      makeRouter(),
    );
    expect(messagesBusy.length).toBe(1);
    expect(messagesBusy[0].text).toContain('重启');

    // wasBusy=false: idle agent → no notification (would be noise)
    const messagesIdle: Array<{ openId: string; text: string }> = [];
    handleSessionEnd(
      feishuAgent,
      'eric:pulse',
      { type: 'relay-shutdown', wasBusy: false },
      poolKeyToOpenId,
      (openId, text) => { messagesIdle.push({ openId, text }); },
      makeRouter(),
    );
    expect(messagesIdle.length).toBe(0);
  });

  it('timeout sends notification (parity with Telegram)', () => {
    const messages: Array<{ openId: string; text: string }> = [];
    const sendText = (openId: string, text: string) => { messages.push({ openId, text }); };
    const poolKeyToOpenId = new Map([['eric:pulse', 'ou_eric123']]);

    handleSessionEnd(feishuAgent, 'eric:pulse', { type: 'timeout', timeoutMs: 120_000 }, poolKeyToOpenId, sendText, makeRouter());
    expect(messages.length).toBe(1);
    expect(messages[0].text).toContain('超时');
    expect(messages[0].text).toContain('120');
  });

  it('idle-reclaimed is silent', () => {
    const messages: Array<{ openId: string; text: string }> = [];
    const sendText = (openId: string, text: string) => { messages.push({ openId, text }); };
    const poolKeyToOpenId = new Map([['eric:pulse', 'ou_eric123']]);

    handleSessionEnd(feishuAgent, 'eric:pulse', { type: 'idle-reclaimed' }, poolKeyToOpenId, sendText, makeRouter());
    expect(messages.length).toBe(0);
  });

  it('ignores events for other agents', () => {
    const messages: Array<{ openId: string; text: string }> = [];
    const sendText = (openId: string, text: string) => { messages.push({ openId, text }); };
    const poolKeyToOpenId = new Map([['eric:pulse', 'ou_eric123']]);

    handleSessionEnd(feishuAgent, 'owner:main', { type: 'crash', exitCode: 1 }, poolKeyToOpenId, sendText, makeRouter());
    expect(messages.length).toBe(0);
  });

  it('ignores when no openId mapping', () => {
    const messages: Array<{ openId: string; text: string }> = [];
    const sendText = (openId: string, text: string) => { messages.push({ openId, text }); };
    const poolKeyToOpenId = new Map<string, string>();

    handleSessionEnd(feishuAgent, 'eric:pulse', { type: 'crash', exitCode: 1 }, poolKeyToOpenId, sendText, makeRouter());
    expect(messages.length).toBe(0);
  });
});

// --- Minutes transcript auto-detection ---

describe('handleFeishuMessage minutes transcript', () => {
  it('detects minutes URL and sends transcript to CC', async () => {
    const pool = makePool({
      get: vi.fn().mockReturnValue({ state: 'ready' }),
      send: vi.fn().mockResolvedValue(undefined),
    });
    const requestMock = vi.fn().mockResolvedValue({
      code: 0,
      data: '[00:01:23] Alice: Hello\n[00:01:45] Bob: Hi there',
    });
    const ctx = makeCtx({
      pool,
      larkClient: { request: requestMock } as any,
    });

    await handleFeishuMessage(ctx, {
      message: {
        message_type: 'text',
        message_id: 'msg-minutes-1',
        content: JSON.stringify({ text: 'https://sample.feishu.cn/minutes/obcnaaaaaaaaaaaaaaaaaaaa' }),
      },
      sender: { sender_id: { open_id: 'ou_eric123' } },
    });

    expect(requestMock).toHaveBeenCalledWith({
      method: 'GET',
      url: '/open-apis/minutes/v1/minutes/obcnaaaaaaaaaaaaaaaaaaaa/transcript',
      params: { file_format: 'txt', need_speaker: true, need_timestamp: true },
    });
    expect(pool.send).toHaveBeenCalledWith(
      'eric:pulse',
      expect.objectContaining({
        type: 'human',
        content: expect.stringContaining('妙记转写'),
      }),
    );
    const content = String(vi.mocked(pool.send).mock.calls[0][1].content);
    expect(content).toContain('Alice: Hello');
    expect(content).toContain('Bob: Hi there');
  });

  it('replies permission error when API returns 2091005', async () => {
    const requestMock = vi.fn().mockRejectedValue(new Error('2091005 no permission'));
    const ctx = makeCtx({
      larkClient: { request: requestMock } as any,
    });

    await handleFeishuMessage(ctx, {
      message: {
        message_type: 'text',
        message_id: 'msg-minutes-2',
        content: JSON.stringify({ text: 'https://sample.feishu.cn/minutes/obcnaaaaaaaaaaaaaaaaaaaa' }),
      },
      sender: { sender_id: { open_id: 'ou_eric123' } },
    });

    expect(ctx.sendTextMessage).toHaveBeenCalledWith(
      'ou_eric123',
      expect.stringContaining('无权限'),
    );
  });

  it('replies not-ready error when API returns 2091003', async () => {
    const requestMock = vi.fn().mockRejectedValue(new Error('2091003 not ready'));
    const ctx = makeCtx({
      larkClient: { request: requestMock } as any,
    });

    await handleFeishuMessage(ctx, {
      message: {
        message_type: 'text',
        message_id: 'msg-minutes-3',
        content: JSON.stringify({ text: 'https://sample.feishu.cn/minutes/obcnaaaaaaaaaaaaaaaaaaaa' }),
      },
      sender: { sender_id: { open_id: 'ou_eric123' } },
    });

    expect(ctx.sendTextMessage).toHaveBeenCalledWith(
      'ou_eric123',
      expect.stringContaining('尚未生成'),
    );
  });

  it('ignores regular text without minutes URL', async () => {
    const pool = makePool({
      get: vi.fn().mockReturnValue({ state: 'ready' }),
      send: vi.fn().mockResolvedValue(undefined),
    });
    const requestMock = vi.fn();
    const ctx = makeCtx({
      pool,
      larkClient: { request: requestMock } as any,
    });

    await handleFeishuMessage(ctx, {
      message: {
        message_type: 'text',
        message_id: 'msg-normal',
        content: JSON.stringify({ text: 'Hello world' }),
      },
      sender: { sender_id: { open_id: 'ou_eric123' } },
    });

    expect(requestMock).not.toHaveBeenCalled();
    expect(pool.send).toHaveBeenCalledWith('eric:pulse', { type: 'human', content: expect.stringContaining('Hello world') });
  });
});

describe('extractCardText', () => {
  it('round-trips text from a buildMarkdownCard payload', () => {
    const card = buildMarkdownCard('这是机器人的上一条回复', { note: 'pulse · sonnet' });
    const content = JSON.stringify(card);
    const extracted = extractCardText(content);
    expect(extracted).toContain('这是机器人的上一条回复');
    // grey <font> note wrapper is stripped, inner note text kept
    expect(extracted).toContain('pulse · sonnet');
    expect(extracted).not.toContain('<font');
  });

  it('includes the header title when present', () => {
    const card = buildMarkdownCard('正文内容', { header: { title: 'Pulse' } });
    const extracted = extractCardText(JSON.stringify(card));
    expect(extracted?.startsWith('Pulse')).toBe(true);
    expect(extracted).toContain('正文内容');
  });

  it('returns undefined for non-card / unparsable content', () => {
    expect(extractCardText('not json')).toBeUndefined();
    expect(extractCardText(JSON.stringify({ schema: '2.0', body: { elements: [{ tag: 'hr' }] } }))).toBeUndefined();
  });
});

// --- 缺陷 A (2026-07-15 canary)：交互回复流式卡片路径的 delivery.receipt 埋点 ---
//
// 交互回复走 createFeishuStreamingSink → onResult 里的 FeishuStreamingCardSession.close(),
// 绕过 delivery.ts 的 receipt() 工厂,故此前零 delivery 埋点。这里验证:
//   - close 成功 → 落一条 delivery.receipt status=sent, session=poolKey(与 req.inbound 配对)。
//   - close 失败 → 落一条 status=failed。
//   - 开关关时不落盘(dark-deploy)。

describe('createFeishuStreamingSink — 交互回复 delivery.receipt 埋点 (缺陷 A)', () => {
  const streamingAgent: AgentConfig = {
    id: 'pulse',
    name: 'Pulse',
    emoji: '\u{1F52E}',
    channel: 'feishu',
    workspace: '/tmp/ws',
    sessionId: 'uuid-tg',
    feishu: { appId: 'cli_test', appSecret: 'secret', sessionId: 'uuid-feishu' },
  };

  function mockLarkClient() {
    return {
      im: { message: { create: async () => ({ code: 0, data: { message_id: 'msg-A-1' } }) } },
    } as any;
  }

  function readDeliveryReceipts(dir: string): Record<string, unknown>[] {
    const events: Record<string, unknown>[] = [];
    for (const f of readdirSync(dir).filter((n) => n.startsWith('degen-') && n.endsWith('.jsonl'))) {
      for (const line of readFileSync(join(dir, f), 'utf8').trim().split('\n').filter(Boolean)) {
        const e = JSON.parse(line);
        if (e.evt === 'delivery.receipt') events.push(e);
      }
    }
    return events;
  }

  let dir: string;
  let savedEnv: Record<string, string | undefined>;
  let savedFetch: typeof global.fetch;

  beforeEach(() => {
    dir = mkdtempSync(join(tmpdir(), 'sliceA-'));
    savedEnv = {
      RELAY_OBS_REQUEST_TRACE: process.env['RELAY_OBS_REQUEST_TRACE'],
      RELAY_OBS_DIR: process.env['RELAY_OBS_DIR'],
      RELAY_OBS_ENABLED: process.env['RELAY_OBS_ENABLED'],
      RELAY_FEISHU_STREAMING_CARD: process.env['RELAY_FEISHU_STREAMING_CARD'],
    };
    savedFetch = global.fetch;
  });

  afterEach(() => {
    for (const [k, v] of Object.entries(savedEnv)) {
      if (v === undefined) delete process.env[k]; else process.env[k] = v;
    }
    global.fetch = savedFetch;
    rmSync(dir, { recursive: true, force: true });
  });

  function stubFetchAllOk(): void {
    global.fetch = (async (url: string | URL) => {
      const u = url.toString();
      if (u.includes('/auth/v3/tenant_access_token/internal')) {
        return { ok: true, json: async () => ({ code: 0, tenant_access_token: 'tok', expire: 7200 }) } as any;
      }
      if (u.includes('/cardkit/v1/cards') && !u.includes('/elements/') && !u.includes('/settings')) {
        return { ok: true, json: async () => ({ code: 0, data: { card_id: 'card-A-1' } }) } as any;
      }
      return { ok: true, json: async () => ({ code: 0 }) } as any;
    }) as any;
  }

  it('close 成功 → delivery.receipt status=sent, session=poolKey', async () => {
    process.env['RELAY_OBS_REQUEST_TRACE'] = 'true';
    process.env['RELAY_OBS_DIR'] = dir;
    process.env['RELAY_OBS_ENABLED'] = 'true';
    process.env['RELAY_FEISHU_STREAMING_CARD'] = 'true';
    stubFetchAllOk();

    const sink = createFeishuStreamingSink(streamingAgent, mockLarkClient(), async () => true)!;
    expect(sink).toBeDefined();

    const poolKey = 'eric:pulse';
    await sink.onAssistant(poolKey, 'ou_eric123', '你好，这是一条足够长的交互回复内容用于触发卡片更新');
    await sink.onResult(poolKey);

    const receipts = readDeliveryReceipts(dir);
    expect(receipts).toHaveLength(1);
    expect(receipts[0].status).toBe('sent');
    expect(receipts[0].session).toBe(poolKey);        // 与 req.inbound 的 session 键配对
    expect(receipts[0].channel).toBe('feishu');
    expect(receipts[0].agent).toBe('pulse');
    expect(receipts[0].severity).toBe('info');
  });

  it('removes a transient activity overlay before closing the authoritative card', async () => {
    process.env['RELAY_FEISHU_STREAMING_CARD'] = 'true';
    const updates: string[] = [];
    global.fetch = (async (url: string | URL, init?: RequestInit) => {
      const u = url.toString();
      if (u.includes('/auth/v3/tenant_access_token/internal')) {
        return { ok: true, json: async () => ({ code: 0, tenant_access_token: 'tok', expire: 7200 }) } as any;
      }
      if (u.includes('/cardkit/v1/cards') && !u.includes('/elements/') && !u.includes('/settings')) {
        return { ok: true, json: async () => ({ code: 0, data: { card_id: 'card-activity' } }) } as any;
      }
      if (u.includes('/elements/content/content') && init?.body) {
        updates.push((JSON.parse(String(init.body)) as { content: string }).content);
      }
      return { ok: true, json: async () => ({ code: 0 }) } as any;
    }) as any;

    const sink = createFeishuStreamingSink(streamingAgent, mockLarkClient(), async () => true)!;
    const poolKey = 'eric:pulse';
    sink.beginTurn(poolKey);
    await sink.onAssistant(poolKey, 'ou_eric123', '先检查现场。', 'commentary', 'commentary-1');
    await sink.onAssistant(poolKey, 'ou_eric123', '⏳ 仍在工作，已推进 5 个执行步骤…', 'activity', 'activity-5');
    await sink.onResult(poolKey);

    expect(updates).toContain('先检查现场。\n\n⏳ 仍在工作，已推进 5 个执行步骤…');
    expect(updates.at(-1)).toBe('先检查现场。\n\n✅ 本轮回复已生成');
  });

  it('流式卡片建卡失败 → 降级 fallback 静态卡, 交互回复仍落 delivery.receipt (不再盲)', async () => {
    // 场景: 流式卡片 start() 失败 → onAssistant 内 catch 降级到 fallback,经
    // sendCardMessage → delivery.ts 的 deliverFeishuCard → receipt() 工厂埋点。
    // 生产里 sendCardMessage 就是 delivery.ts-backed 的那个,故此处注入等价物验证:
    // 即便流式路径挂掉,这次交互回复的投递结果也不再零埋点(缺陷 A 的另一半覆盖)。
    process.env['RELAY_OBS_REQUEST_TRACE'] = 'true';
    process.env['RELAY_OBS_DIR'] = dir;
    process.env['RELAY_OBS_ENABLED'] = 'true';
    process.env['RELAY_FEISHU_STREAMING_CARD'] = 'true';

    // 让流式卡片 start() 的 card-create 失败(HTTP 500) → onAssistant 走 fallback。
    global.fetch = (async (url: string | URL) => {
      const u = url.toString();
      if (u.includes('/auth/v3/tenant_access_token/internal')) {
        return { ok: true, json: async () => ({ code: 0, tenant_access_token: 'tok', expire: 7200 }) } as any;
      }
      if (u.includes('/cardkit/v1/cards') && !u.includes('/elements/') && !u.includes('/settings')) {
        return { ok: false, status: 500, json: async () => ({ code: 1, msg: 'create boom' }) } as any;
      }
      return { ok: true, json: async () => ({ code: 0 }) } as any;
    }) as any;

    // fallback sendCardMessage 走真正的 delivery.ts(deliverFeishuCard),其 receipt()
    // 工厂会打 delivery.receipt。lark client 的 message.create 成功 → status=sent。
    const { deliverFeishuCard } = await import('../src/delivery.js');
    const larkClient = {
      im: { message: { create: async () => ({ code: 0, data: { message_id: 'fb-msg-1' } }) } },
    } as any;
    const deliveryBackedSendCard = async (openId: string, card: Record<string, unknown>) => {
      const r = await deliverFeishuCard(
        {
          id: `delivery:feishu:pulse:card:${Date.now()}`,
          correlationId: 'eric:pulse',   // 用 poolKey 作 correlationId → delivery.receipt.session=poolKey
          channel: 'feishu',
          address: { channel: 'feishu', kind: 'feishu_open_id', value: openId } as any,
          payload: { kind: 'card', markdown: '', title: 'Pulse', card },
        } as any,
        larkClient,
      );
      return r.status === 'sent';
    };

    const sink = createFeishuStreamingSink(streamingAgent, larkClient, deliveryBackedSendCard)!;
    const poolKey = 'eric:pulse';
    await sink.onAssistant(poolKey, 'ou_eric123', '流式建卡会失败因此降级到静态卡但投递仍要有埋点');
    await sink.onResult(poolKey);

    const receipts = readDeliveryReceipts(dir);
    const forTurn = receipts.filter((e) => e.session === poolKey);
    // 交互回复的投递结果不再零埋点: fallback 静态卡经 delivery.ts 记了一条 receipt。
    expect(forTurn.length).toBeGreaterThanOrEqual(1);
    expect(forTurn.some((e) => e.status === 'sent' && e.channel === 'feishu')).toBe(true);
  });

  it('永久 card update 拒绝后只在终态发送一次完整 fallback，不落地递进前缀副本', async () => {
    process.env['RELAY_FEISHU_STREAMING_CARD'] = 'true';
    let updateCalls = 0;
    global.fetch = (async (url: string | URL) => {
      const u = url.toString();
      if (u.includes('/auth/v3/tenant_access_token/internal')) {
        return { ok: true, json: async () => ({ code: 0, tenant_access_token: 'tok', expire: 7200 }) } as any;
      }
      if (u.includes('/cardkit/v1/cards') && !u.includes('/elements/') && !u.includes('/settings')) {
        return { ok: true, json: async () => ({ code: 0, data: { card_id: 'card-timeout' } }) } as any;
      }
      if (u.includes('/elements/content/content')) {
        updateCalls += 1;
        if (updateCalls >= 2) {
          return { ok: true, json: async () => ({ code: 1, msg: 'card format invalid' }) } as any;
        }
      }
      return { ok: true, json: async () => ({ code: 0 }) } as any;
    }) as any;

    const sentCards: Record<string, unknown>[] = [];
    const sink = createFeishuStreamingSink(
      streamingAgent,
      mockLarkClient(),
      async (_openId, card) => { sentCards.push(card); return true; },
    )!;
    const poolKey = 'eric:pulse';
    await sink.onAssistant(poolKey, 'ou_eric123', '已开工。');
    await expect(sink.onAssistant(poolKey, 'ou_eric123', '已开工。第一竖切候选已推送。')).resolves.toBeUndefined();
    await sink.onAssistant(poolKey, 'ou_eric123', '已开工。第一竖切候选已推送。这还不是完成。');
    expect(sentCards).toHaveLength(0);

    await sink.onResult(poolKey);
    expect(sentCards).toHaveLength(1);
    expect(JSON.stringify(sentCards[0])).toContain('已开工。第一竖切候选已推送。这还不是完成。');
    await sink.onResult(poolKey);
    expect(sentCards).toHaveLength(1);
  });

  it('dark-deploy: 开关关 → 交互回复不落 delivery.receipt', async () => {
    delete process.env['RELAY_OBS_REQUEST_TRACE'];   // 开关关
    process.env['RELAY_OBS_DIR'] = dir;
    process.env['RELAY_OBS_ENABLED'] = 'true';
    process.env['RELAY_FEISHU_STREAMING_CARD'] = 'true';
    stubFetchAllOk();

    const sink = createFeishuStreamingSink(streamingAgent, mockLarkClient(), async () => true)!;
    const poolKey = 'eric:pulse';
    await sink.onAssistant(poolKey, 'ou_eric123', '你好，这是一条足够长的交互回复内容用于触发卡片更新');
    await sink.onResult(poolKey);

    // 开关关时 emitObsEvent 提前 return,零落盘。
    const files = readdirSync(dir).filter((n) => n.startsWith('degen-'));
    const receipts = files.length ? readDeliveryReceipts(dir) : [];
    expect(receipts).toHaveLength(0);
  });
});

describe('createFeishuStreamingSink — RichDoc rollout', () => {
  const streamingAgent: AgentConfig = {
    id: 'pulse',
    name: 'Pulse',
    emoji: '🔮',
    channel: 'feishu',
    workspace: '/tmp/ws',
    sessionId: 'uuid-tg',
    feishu: { appId: 'cli_richdoc_test', appSecret: 'secret', sessionId: 'uuid-feishu' },
  };
  let savedFetch: typeof global.fetch;

  beforeEach(() => {
    savedFetch = global.fetch;
    process.env['RELAY_FEISHU_STREAMING_CARD'] = 'true';
  });

  afterEach(() => {
    global.fetch = savedFetch;
    delete process.env['RELAY_FEISHU_STREAMING_CARD'];
  });

  it('streams sanitized CardKit Markdown and rolls a long semantic answer across cards', async () => {
    let cardNumber = 0;
    const cardMessages: string[] = [];
    const initialContents: string[] = [];
    const updates: string[] = [];
    const replacementCards: Record<string, unknown>[] = [];
    global.fetch = (async (url: string | URL, init?: RequestInit) => {
      const target = url.toString();
      if (target.includes('/auth/v3/tenant_access_token/internal')) {
        return { ok: true, json: async () => ({ code: 0, tenant_access_token: 'tok', expire: 7200 }) } as any;
      }
      if (target.endsWith('/cardkit/v1/cards') && init?.method === 'POST') {
        cardNumber += 1;
        const request = JSON.parse(String(init.body)) as { data: string };
        const card = JSON.parse(request.data) as { body?: { elements?: Array<{ content?: string }> } };
        const initial = card.body?.elements?.[0]?.content;
        if (initial) initialContents.push(initial);
        return { ok: true, json: async () => ({ code: 0, data: { card_id: `rich-card-${cardNumber}` } }) } as any;
      }
      if (/\/cardkit\/v1\/cards\/rich-card-\d+$/u.test(target) && init?.method === 'PUT') {
        const body = JSON.parse(String(init.body)) as { card: { data: string } };
        replacementCards.push(JSON.parse(body.card.data) as Record<string, unknown>);
        return { ok: true, json: async () => ({ code: 0 }) } as any;
      }
      if (target.includes('/elements/content/content') && init?.body) {
        updates.push((JSON.parse(String(init.body)) as { content: string }).content);
      }
      return { ok: true, json: async () => ({ code: 0 }) } as any;
    }) as any;
    const client = {
      im: { message: { create: vi.fn(async (input: { data: { msg_type: string } }) => {
        cardMessages.push(input.data.msg_type);
        return { code: 0, data: { message_id: `rich-message-${cardMessages.length}` } };
      }) } },
    } as any;
    const profile = resolveRichDocChannelProfiles({
      RELAY_FEISHU_RICHDOC_MODE: 'on',
      RELAY_FEISHU_RICHDOC_MAX_LENGTH: '180',
      RELAY_FEISHU_RICHDOC_ROLLOVER_THRESHOLD: '160',
      RELAY_FEISHU_RICHDOC_UPDATE_THROTTLE_MS: '50',
    }).feishu;
    const observations: Record<string, unknown>[] = [];
    const staticCards: Record<string, unknown>[] = [];
    const sink = createFeishuStreamingSink(
      streamingAgent,
      client,
      async (_openId, card) => { staticCards.push(card); return true; },
      undefined,
      { richDocProfile: profile, observe: (event) => observations.push(event) },
    )!;
    const source = Array.from(
      { length: 12 },
      (_, index) => `## 第 ${index} 节\n\n段落 **${index}** ${'内容'.repeat(12)}`,
    ).join('\n\n') + '\n\n```ts\nconst answer = 42;\n```\n\n<img src=x onerror=alert(1)>';

    sink.beginTurn('eric:pulse');
    await sink.onAssistant('eric:pulse', 'ou_eric123', source, 'final', 'final-1');
    await sink.onResult('eric:pulse');

    expect(cardMessages.length).toBeGreaterThan(2);
    expect(cardMessages.every((type) => type === 'interactive')).toBe(true);
    expect(staticCards).toHaveLength(0);
    const visibleContents = [...initialContents, ...updates];
    expect(visibleContents.some((text) => text.includes('**0**'))).toBe(true);
    expect(visibleContents.some((text) => text.includes('&lt;img'))).toBe(true);
    expect(visibleContents.some((text) => text.includes('✅ 本轮回复已生成'))).toBe(true);
    expect(visibleContents.every((text) => Array.from(text).length <= 160)).toBe(true);
    expect(replacementCards).toHaveLength(0);
    expect(observations).toEqual(expect.arrayContaining([
      expect.objectContaining({ channel: 'feishu', layer: 'renderer', action: 'rollover' }),
      expect.objectContaining({ channel: 'feishu', layer: 'transport', terminal: true, result: 'delivered' }),
    ]));
    expect(JSON.stringify(observations)).not.toMatch(/第 0 节|内容|eric:pulse/);
  });

  it('falls back CardKit Markdown to post Markdown to plain text exactly once after definite failures', async () => {
    global.fetch = (async (url: string | URL) => {
      const target = url.toString();
      if (target.includes('/auth/v3/tenant_access_token/internal')) {
        return { ok: true, json: async () => ({ code: 0, tenant_access_token: 'tok', expire: 7200 }) } as any;
      }
      if (target.includes('/cardkit/v1/cards') && !target.includes('/elements/')) {
        return { ok: false, status: 400, json: async () => ({ code: 1, msg: 'invalid card format' }) } as any;
      }
      return { ok: true, json: async () => ({ code: 0 }) } as any;
    }) as any;
    const create = vi.fn(async (input: { data: { msg_type: string } }) => input.data.msg_type === 'post'
      ? { code: 230001, msg: 'invalid post format' }
      : { code: 0, data: { message_id: 'plain-1' } });
    const client = { im: { message: { create } } } as any;
    const sendCard = vi.fn(async () => false);
    const profile = resolveRichDocChannelProfiles({ RELAY_FEISHU_RICHDOC_MODE: 'on' }).feishu;
    const observations: Record<string, unknown>[] = [];
    const sink = createFeishuStreamingSink(
      streamingAgent,
      client,
      sendCard,
      undefined,
      { richDocProfile: profile, observe: (event) => observations.push(event) },
    )!;

    sink.beginTurn('eric:pulse');
    await sink.onAssistant('eric:pulse', 'ou_eric123', '**完整答案** [link](https://example.com)', 'final', 'final-1');
    await sink.onResult('eric:pulse');
    await sink.onResult('eric:pulse');
    await sink.onAssistant('eric:pulse', 'ou_eric123', '迟到的重复终态', 'final', 'late-final');

    expect(sendCard).toHaveBeenCalledTimes(1);
    expect(create.mock.calls.map((call) => call[0].data.msg_type)).toEqual(['post', 'text']);
    expect(JSON.stringify(create.mock.calls[1][0])).toContain('完整答案 link (https://example.com/)');
    expect(JSON.stringify(create.mock.calls[1][0])).toContain('✅ 本轮回复已生成');
    expect(observations.filter((event) => event['action'] === 'fallback')).toEqual(expect.arrayContaining([
      expect.objectContaining({ fromMode: 'cardkit_markdown', toMode: 'post_markdown' }),
      expect.objectContaining({ fromMode: 'post_markdown', toMode: 'plain_text' }),
    ]));
  });
});

// Preserve explicit rollback-path coverage; default folded UI is exercised in folded-process-channels.test.ts.
beforeEach(() => { vi.stubEnv('RELAY_WORK_PROCESS_DETAILS', 'off'); });


it('routes authenticated native Feishu /wi into its original Pi session without old writers',async()=>{
  const dir=mkdtempSync(join(tmpdir(),'native-feishu-command-'));
  const previous=process.env.RELAY_PI_RUNTIME_MANIFEST_PATH;
  try {
    process.env.RELAY_PI_RUNTIME_MANIFEST_PATH=nativeCoreFixture(dir);
    const agent={...feishuAgent,workspace:dir,runtime:'pi' as const,
      conversationRuntime:{runtime:'pi' as const,isolationProfile:'daily',
        model:{provider:'openai',model:'fixture',reasoning:'low' as const}},
      feishu:{...feishuAgent.feishu!,runtime:'pi' as const,
        conversationRuntime:{runtime:'pi' as const,isolationProfile:'daily',
          model:{provider:'openai',model:'fixture',reasoning:'low' as const}}}};
    const user={...feishuUser,agents:[agent]};
    const pool=makePool();
    const ctx=makeCtx({agent,pool,router:makeRouter({getAgents:()=>[agent],getAllUsers:()=>[user]})});
    await handleCommand(ctx,'/wi status wi_native_fixture','ou_eric123',{user},'native-event');
    expect(pool.send).not.toHaveBeenCalled();
    expect(pool.spawn).toHaveBeenCalledWith(expect.objectContaining({sessionId:'uuid-feishu',runtime:'pi'}));
    expect(ctx.sendTextMessage).toHaveBeenCalledWith('ou_eric123',expect.stringContaining('local_committed'));
  } finally {
    if(previous===undefined)delete process.env.RELAY_PI_RUNTIME_MANIFEST_PATH;
    else process.env.RELAY_PI_RUNTIME_MANIFEST_PATH=previous;
    rmSync(dir,{recursive:true,force:true});
  }
});

describe('Feishu collaboration delivery notice', () => {
  const notice = { role: 'requester' as const, contractId: 'op_643afde3b37f', workItemId: 'wi_20261023_675',
    kind: 'request', status: 'completed', targetWorkItemId: 'wi_20261023_675', mainOnly: false, answeredBy: ['Hestia'] };
  const headline = '📨 Hestia 已答复你在 wi_20261023_675 的协作合同（已完成），开始验收。';

  it('sends a card titled by the headline with the details folded', async () => {
    const card = vi.fn(async (_card: Record<string, unknown>) => true);
    const text = vi.fn(async (_text: string) => {});
    await sendCollabTurnNotice({ card, text }, notice);
    expect(text).not.toHaveBeenCalled();
    const sent = card.mock.calls[0]![0] as { header: { title: { content: string } }; body: { elements: Array<{ tag: string }> } };
    expect(sent.header.title.content).toBe(headline);
    expect(sent.body.elements[0]!.tag).toBe('collapsible_panel');
  });

  it('falls back to the headline as text, and to the generic line without a notice', async () => {
    const text = vi.fn(async (_text: string) => {});
    await sendCollabTurnNotice({ card: vi.fn(async () => false), text }, notice);
    await sendCollabTurnNotice({ card: vi.fn(async () => { throw new Error('down'); }), text }, notice);
    const card = vi.fn(async () => true);
    await sendCollabTurnNotice({ card, text });
    expect(card).not.toHaveBeenCalled();
    expect(text.mock.calls.map(call => call[0])).toEqual([headline, headline, '📨 收到一份协作合同，开始处理。']);
  });
});
