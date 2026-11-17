import {isolateHumanDecisions} from './helpers/scl-human-decision.js';
isolateHumanDecisions();
import { describe, it, expect, vi, beforeEach } from 'vitest';

// Partial-mock commands.js: handleXxx become spies; build*Card stay real (importActual).
vi.mock('../src/commands.js', async (importOriginal) => {
  const mod = await importOriginal<typeof import('../src/commands.js')>();
  return {
    ...mod,
    handleNew: vi.fn().mockResolvedValue({ text: 'new ok' }),
    handleReset: vi.fn().mockResolvedValue({ text: 'reset ok' }),
    handleStop: vi.fn().mockResolvedValue({ text: 'stop ok' }),
    handleKill: vi.fn().mockResolvedValue({ text: 'kill ok' }),
    handleRestart: vi.fn().mockResolvedValue({ text: 'restart ok' }),
    handleStatus: vi.fn().mockReturnValue({ text: 'status ok' }),
    handleSession: vi.fn().mockReturnValue({ text: 'session ok' }),
    handleUsage: vi.fn().mockReturnValue({ text: 'usage ok' }),
    handleModel: vi.fn().mockResolvedValue({ text: 'model ok' }),
    handleModelScope: vi.fn().mockResolvedValue({ text: 'model scope ok' }),
    handleRuntime: vi.fn().mockResolvedValue({ text: 'runtime ok' }),
    handleYolo: vi.fn().mockResolvedValue({ text: 'yolo ok' }),
    handleSwitch: vi.fn().mockReturnValue({ text: 'switch ok' }),
  };
});

import { dispatchMenuAction, handleFeishuCardAction, handleCommand } from '../src/channels/feishu.js';
import {
  handleNew,
  handleReset,
  handleStop,
  handleStatus,
  handleModel,
  handleModelScope,
  handleSwitch,
  handleHelp,
} from '../src/commands.js';
import type { AgentConfig, UserConfig } from '../src/types.js';
import type { Router } from '../src/router.js';
import type { CommandRuntime } from '../src/commands.js';
import type { MessageHandlerContext } from '../src/channels/feishu.js';
import { NATIVE_WI_UNAVAILABLE } from '../src/native-work-item-command.js';

const feishuAgent: AgentConfig = {
  id: 'pulse',
  name: 'Pulse',
  emoji: '\u{1F52E}',
  channel: 'feishu',
  workspace: '/tmp/ws',
  feishu: { appId: 'cli_test', appSecret: 'secret', sessionId: 'uuid-feishu' },
};

const feishuUser: UserConfig = {
  userId: 'eric',
  feishuOpenId: 'ou_eric123',
  defaultAgent: 'pulse',
  agents: [feishuAgent],
};

function makeCtx(overrides?: Partial<MessageHandlerContext>): MessageHandlerContext {
  const router: Router = {
    resolveUser: vi.fn(),
    getActiveAgent: vi.fn(),
    switchAgent: vi.fn(),
    poolKey: (u: string, a: string) => `${u}:${a}`,
    getAgents: vi.fn().mockReturnValue([feishuAgent]),
    getAllUsers: vi.fn().mockReturnValue([feishuUser]),
    resolveByBotAgent: vi.fn(),
    resolveByFeishuAgent: vi.fn().mockReturnValue({ user: feishuUser, agent: feishuAgent }),
    resetSession: vi.fn(),
    overrideSessionId: vi.fn(),
    getAllAgentBots: vi.fn().mockReturnValue([]),
    getAllFeishuBots: vi.fn().mockReturnValue([{ agent: feishuAgent, authorizedOpenIds: ['ou_eric123'] }]),
    getAgentChannel: vi.fn().mockReturnValue('feishu' as const),
    getAgentModel: vi.fn().mockReturnValue(undefined),
    ...overrides,
  } as unknown as Router;

  const pool: CommandRuntime = {
    spawn: vi.fn().mockResolvedValue({}),
    send: vi.fn().mockResolvedValue(undefined),
    kill: vi.fn(),
    killAll: vi.fn(),
    get: vi.fn().mockReturnValue(undefined),
    getAll: vi.fn().mockReturnValue([]),
    getStatus: vi.fn().mockReturnValue([]),
    on: vi.fn(),
    emitShutdown: vi.fn(),
    dispose: vi.fn(),
  } as unknown as CommandRuntime;

  return {
    agent: feishuAgent,
    pool,
    router,
    stateManager: { getSession: vi.fn().mockReturnValue(undefined), updateSession: vi.fn(), removeSession: vi.fn(), flush: vi.fn() } as never,
    meteringDir: '/tmp/metering',
    modelCatalog: { listIds: () => [{ id: 'claude-sonnet-4-6', display_name: 'Sonnet 4.6' }] } as never,
    sendTextMessage: vi.fn(async () => {}),
    sendCardMessage: vi.fn(async () => {}),
    ...((overrides ?? {}) as Record<string, unknown>),
  } as unknown as MessageHandlerContext;
}

beforeEach(() => {
  vi.mocked(handleNew).mockClear();
  vi.mocked(handleReset).mockClear();
  vi.mocked(handleStop).mockClear();
  vi.mocked(handleStatus).mockClear();
  vi.mocked(handleModel).mockClear();
  vi.mocked(handleModelScope).mockClear();
  vi.mocked(handleSwitch).mockClear();
});

describe('dispatchMenuAction', () => {
  it('action button (new, no arg) calls handleNew once', async () => {
    const ctx = makeCtx();
    await dispatchMenuAction(ctx, 'ou_eric123', 'new', '');
    expect(handleNew).toHaveBeenCalledOnce();
  });

  it('second-level option (model+sonnet) calls handleModel with deps containing modelCatalog', async () => {
    const ctx = makeCtx();
    await dispatchMenuAction(ctx, 'ou_eric123', 'model', 'sonnet');
    expect(handleModel).toHaveBeenCalledOnce();
    const callArgs = vi.mocked(handleModel).mock.calls[0];
    // handleModel(userId, agentId, requestedModel, deps)
    expect(callArgs[2]).toBe('sonnet');
    const deps = callArgs[3] as { modelCatalog?: unknown };
    expect(deps.modelCatalog).toBe((ctx as unknown as { modelCatalog: unknown }).modelCatalog);
  });

  it('config button without arg (model) sends a second-level card via sendCardMessage', async () => {
    const ctx = makeCtx();
    vi.mocked(handleModel).mockResolvedValueOnce({
      text: 'model ok',
      feishuCardElements: [{ tag: 'markdown', content: 'options' }],
    });
    await dispatchMenuAction(ctx, 'ou_eric123', 'model', '');
    expect(handleModel).toHaveBeenCalledWith('eric', 'pulse', undefined, expect.objectContaining({ modelCatalog: expect.anything() }));
    expect(ctx.sendCardMessage).toHaveBeenCalledOnce();
  });

  it('dispatches a runtime/access model scope without treating it as a model id', async () => {
    const ctx = makeCtx();
    await dispatchMenuAction(ctx, 'ou_eric123', 'model_scope', 'pi|subscription');
    expect(handleModelScope).toHaveBeenCalledWith(
      'eric',
      'pulse',
      'pi|subscription',
      expect.objectContaining({ modelCatalog: expect.anything() }),
    );
    expect(handleModel).not.toHaveBeenCalled();
  });

  it('returns early with a notice when resolveByFeishuAgent is null', async () => {
    const ctx = makeCtx({
      router: { resolveByFeishuAgent: vi.fn().mockReturnValue(undefined) } as never,
    });
    await dispatchMenuAction(ctx, 'ou_eric123', 'new', '');
    expect(handleNew).not.toHaveBeenCalled();
    expect(ctx.sendTextMessage).toHaveBeenCalledOnce();
  });
});

describe('handleFeishuCardAction — relay_menu branch', () => {
  function cardRaw(value: Record<string, unknown>) {
    return {
      event: {
        operator: { open_id: 'ou_eric123' },
        action: { value },
      },
    };
  }

  it('routes {relay_menu:stop} into dispatchMenuAction (handleStop called)', async () => {
    const ctx = makeCtx();
    await handleFeishuCardAction(ctx, cardRaw({ relay_menu: 'stop' }));
    expect(handleStop).toHaveBeenCalledOnce();
  });

  it('regression: {relay_switch} still handled by existing switch branch, not blocked by relay_menu', async () => {
    const ctx = makeCtx();
    await handleFeishuCardAction(ctx, cardRaw({ relay_switch: 'pulse' }));
    expect(handleSwitch).toHaveBeenCalledOnce();
  });
});

describe('handleCommand /menu + handleHelp', () => {
  it('/menu sends a card with feishuCardElements (column_set present)', async () => {
    const ctx = makeCtx();
    await handleCommand(ctx, '/menu', 'ou_eric123', { user: feishuUser });
    expect(ctx.sendCardMessage).toHaveBeenCalledOnce();
    const card = vi.mocked(ctx.sendCardMessage).mock.calls[0][1] as Record<string, unknown>;
    const body = card.body as Record<string, unknown>;
    const elements = body.elements as Record<string, unknown>[];
    const hasColumnSet = elements.some(e => e?.tag === 'column_set');
    expect(hasColumnSet).toBe(true);
  });

  it('/menu falls back to text when Feishu rejects the command card', async () => {
    const ctx = makeCtx({
      sendCardMessage: vi.fn().mockRejectedValue(new Error('element exceeds the limit')),
      sendTextMessage: vi.fn().mockResolvedValue(undefined),
    });
    await handleCommand(ctx, '/menu', 'ou_eric123', { user: feishuUser });
    expect(ctx.sendTextMessage).toHaveBeenCalledWith(
      'ou_eric123',
      expect.stringContaining('已自动回退为文字'),
    );
  });

  it('handleHelp mentions /menu', () => {
    const result = handleHelp();
    expect(result.text).toContain('/menu');
  });

  it('/wi without the native core replies unavailable and has no legacy fallback', async () => {
    const ctx = makeCtx();
    await handleCommand(ctx, '/wi status wi_20260820_272', 'ou_eric123', { user: feishuUser }, 'om_wi');
    expect(ctx.sendTextMessage).toHaveBeenCalledWith('ou_eric123', NATIVE_WI_UNAVAILABLE);
    expect(ctx.pool.send).not.toHaveBeenCalled();
  });
});


