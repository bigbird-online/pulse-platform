import { describe, it, expect } from 'vitest';
import {
  buildMenuCard,
  buildModelOptionCard,
  buildRuntimeOptionCard,
  buildYoloOptionCard,
  type CommandDeps,
} from '../src/commands.js';
import type { AgentConfig } from '../src/types.js';

const agent: AgentConfig = {
  id: 'main',
  name: 'Duke',
  emoji: '\u{1F3B2}',
  workspace: '/tmp',
};

/** Recursively collect all elements with a given tag from a feishuCardElements tree. */
function collectByTag(elements: unknown[], tag: string): Record<string, unknown>[] {
  const out: Record<string, unknown>[] = [];
  const visit = (list: unknown[]) => {
    for (const node of list) {
      const n = node as Record<string, unknown>;
      if (n?.tag === tag) out.push(n);
      // column_set → columns[] → elements[]
      const cols = n?.columns;
      if (Array.isArray(cols)) {
        for (const c of cols) {
          const ce = (c as Record<string, unknown>)?.elements;
          if (Array.isArray(ce)) visit(ce);
        }
      }
      // nested elements (e.g. column.elements)
      const nested = n?.elements;
      if (Array.isArray(nested) && n?.tag !== 'column_set') visit(nested);
    }
  };
  visit(elements);
  return out;
}

function allButtons(result: { feishuCardElements?: Record<string, unknown>[] }) {
  return collectByTag(result.feishuCardElements ?? [], 'button');
}

describe('buildMenuCard', () => {
  it('contains 4 column_set groups (one per section)', () => {
    const result = buildMenuCard(agent);
    const colSets = collectByTag(result.feishuCardElements ?? [], 'column_set');
    expect(colSets).toHaveLength(4);
  });

  it('action buttons carry { relay_menu } value with no relay_menu_arg', () => {
    const result = buildMenuCard(agent);
    const buttons = allButtons(result);
    expect(buttons.length).toBeGreaterThan(0);
    for (const btn of buttons) {
      const value = (btn.behaviors as Record<string, unknown>[])?.[0]?.value as Record<string, unknown>;
      expect(value).toBeDefined();
      expect(typeof value.relay_menu).toBe('string');
      expect(value.relay_menu_arg).toBeUndefined();
    }
  });

  it('renders exactly 11 buttons (session2 + process3 + view3 + config3)', () => {
    const result = buildMenuCard(agent);
    expect(allButtons(result)).toHaveLength(11);
  });

  it('reset button uses danger type, others default', () => {
    const result = buildMenuCard(agent);
    const buttons = allButtons(result);
    const findBtn = (action: string) => buttons.find(b => {
      const v = (b.behaviors as Record<string, unknown>[])?.[0]?.value as Record<string, unknown>;
      return v?.relay_menu === action;
    });
    const reset = findBtn('reset');
    const next = findBtn('new');
    expect(reset?.type).toBe('danger');
    expect(next?.type).toBe('default');
  });

  it('covers all expected menu actions', () => {
    const result = buildMenuCard(agent);
    const actions = allButtons(result).map(b => {
      const v = (b.behaviors as Record<string, unknown>[])?.[0]?.value as Record<string, unknown>;
      return v.relay_menu as string;
    });
    expect(actions).toEqual(
      expect.arrayContaining([
        'new', 'reset', 'stop', 'kill', 'restart',
        'status', 'session', 'usage',
        'runtime', 'model', 'yolo',
      ]),
    );
    // switch/list must NOT appear
    expect(actions).not.toContain('switch');
    expect(actions).not.toContain('list');
  });
});

// --- P0-002: second-level option cards ---

interface MockModel { id: string; display_name: string; runtimeLabel?: string; available?: boolean; }

function makeDeps(catalog?: { listIds: () => MockModel[] }): CommandDeps {
  return {
    router: {} as never,
    runtime: {} as never,
    modelCatalog: catalog as never,
  } as CommandDeps;
}

const catalogModels: MockModel[] = [
  { id: 'claude-opus-5-5', display_name: 'Opus 5.5' },
  { id: 'claude-fable-5', display_name: 'Fable 5' },
  { id: 'opus', display_name: 'Opus latest' },
];

describe('buildModelOptionCard', () => {
  it('starts with runtime and access scopes instead of flattening catalog models', () => {
    const deps = makeDeps({ listIds: () => catalogModels });
    const result = buildModelOptionCard(agent, 'owner', deps);
    const buttons = allButtons(result);
    const actions = buttons.map(b => {
      const v = (b.behaviors as Record<string, unknown>[])?.[0]?.value as Record<string, unknown>;
      return `${String(v.relay_menu)}:${String(v.relay_menu_arg)}`;
    });
    expect(actions).toEqual(
      expect.arrayContaining(['model_scope:cc|subscription', 'runtime:codex', 'runtime:cc-sdk']),
    );
    expect(JSON.stringify(result.feishuCardElements)).not.toContain('claude-sonnet-4-6');
    expect(result.text).toContain('runtime → 接入方式 → model');
  });

  it('keeps a bounded runtime-first control surface when modelCatalog is undefined', () => {
    const deps = makeDeps(undefined);
    const result = buildModelOptionCard(agent, 'owner', deps);
    const buttons = allButtons(result);
    expect(buttons).toHaveLength(3);
    expect(result.feishuCardElements).toHaveLength(11);
    expect(JSON.stringify(result.feishuCardElements)).toContain('模型目录未就绪');
  });

  it('groups a Pi canary into four runtimes and two access paths without model buttons', () => {
    const piAgent: AgentConfig = {
      ...agent,
      conversationRuntimePreference: ['pi'],
      conversationRuntime: {
        runtime: 'pi',
        model: { provider: 'openai-codex', model: 'gpt-5.6-sol', reasoning: 'medium' },
        isolationProfile: 'internal-canary',
      },
    };
    const result = buildModelOptionCard(piAgent, 'owner', makeDeps(undefined), undefined, 'pi');
    const values = allButtons(result).map((button) => (
      (button.behaviors as Record<string, unknown>[])?.[0]?.value as Record<string, unknown>
    ));
    expect(values.map((value) => `${String(value.relay_menu)}:${String(value.relay_menu_arg)}`)).toEqual(
      expect.arrayContaining([
        'runtime:cc',
        'runtime:codex',
        'runtime:cc-sdk',
        'model_scope:pi|subscription',
        'model_scope:pi|openrouter',
      ]),
    );
    expect(JSON.stringify(result.feishuCardElements)).not.toContain('gpt-5.6-terra');
    expect(result.text).toContain('openai-codex/gpt-5.6-sol');
  });
});

describe('buildRuntimeOptionCard', () => {
  it('renders all generally available runtimes without exposing unconfigured Pi', () => {
    const result = buildRuntimeOptionCard(agent);
    const buttons = allButtons(result);
    expect(buttons).toHaveLength(3);
    const args = buttons.map(b => {
      const v = (b.behaviors as Record<string, unknown>[])?.[0]?.value as Record<string, unknown>;
      expect(v.relay_menu).toBe('runtime');
      return v.relay_menu_arg;
    });
    expect(args).toEqual(expect.arrayContaining(['cc', 'codex', 'cc-sdk']));
    expect(args).not.toContain('pi');
  });

  it('exposes Pi only for an approved structured canary route', () => {
    const result = buildRuntimeOptionCard({
      ...agent,
      conversationRuntime: {
        runtime: 'pi',
        model: { provider: 'openrouter', model: 'anthropic/claude-sonnet-4.5', reasoning: 'high' },
        isolationProfile: 'internal-canary',
      },
    });
    const buttons = allButtons(result);
    const pi = buttons.find((button) => {
      const value = (button.behaviors as Record<string, unknown>[])?.[0]?.value as Record<string, unknown>;
      return value.relay_menu_arg === 'pi';
    });
    expect(pi).toBeDefined();
    expect(pi?.type).toBe('primary');
  });

  it('exposes Pi for every agent when the node supplies a reviewed default', () => {
    const result = buildRuntimeOptionCard(agent, {
      router: {
        getConversationRuntimeDefault: () => ({
          model: { provider: 'openai-codex', model: 'gpt-5.6-sol', reasoning: 'medium' },
          isolationProfile: 'resident-host-native',
        }),
      } as any,
    });
    const args = allButtons(result).map((button) => {
      const value = (button.behaviors as Record<string, unknown>[])?.[0]?.value as Record<string, unknown>;
      return value.relay_menu_arg;
    });
    expect(args).toContain('pi');
  });
});

describe('buildYoloOptionCard', () => {
  it('renders on/off/status buttons with relay_menu_arg', () => {
    const result = buildYoloOptionCard(agent);
    const buttons = allButtons(result);
    expect(buttons).toHaveLength(3);
    const args = buttons.map(b => {
      const v = (b.behaviors as Record<string, unknown>[])?.[0]?.value as Record<string, unknown>;
      expect(v.relay_menu).toBe('yolo');
      return v.relay_menu_arg;
    });
    expect(args).toEqual(expect.arrayContaining(['on', 'off', 'status']));
  });
});
