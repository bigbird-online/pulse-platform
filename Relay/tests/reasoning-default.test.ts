import { mkdtempSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import {
  describeClaudeDefaultReasoning,
  describeCodexDefaultReasoning,
  lastClaudeTurnEffort,
} from '../src/reasoning-control.js';

let root: string;

beforeEach(() => {
  root = mkdtempSync(join(tmpdir(), 'relay-reasoning-default-'));
});

afterEach(() => {
  vi.unstubAllEnvs();
  rmSync(root, { recursive: true, force: true });
});

function transcript(name: string, records: unknown[], trailer = ''): string {
  const path = join(root, name);
  writeFileSync(path, records.map((record) => JSON.stringify(record)).join('\n') + '\n' + trailer);
  return path;
}

const user = { type: 'user', message: { role: 'user', content: 'hi' } };
function assistant(extra: Record<string, unknown>) {
  return { type: 'assistant', message: { role: 'assistant', model: 'claude-opus-5-5', content: [] }, ...extra };
}

describe('lastClaudeTurnEffort', () => {
  it('reads the last assistant record that carries effort', () => {
    const path = transcript('a.jsonl', [
      user,
      assistant({ requestedModel: 'claude-opus-5-5', effort: 'high', perTurnEffort: 'high' }),
      user,
      assistant({ requestedModel: 'claude-opus-5-5', effort: 'medium', perTurnEffort: 'medium' }),
      assistant({}),
      { type: 'system', effort: 'max' },
    ]);
    expect(lastClaudeTurnEffort([path])).toEqual({ effort: 'medium', model: 'claude-opus-5-5' });
  });

  it('falls back to message.model when requestedModel is absent', () => {
    const path = transcript('b.jsonl', [assistant({ effort: 'low' })]);
    expect(lastClaudeTurnEffort([path])).toEqual({ effort: 'low', model: 'claude-opus-5-5' });
  });

  it('returns nothing when assistant records carry no effort, or there are no turns', () => {
    expect(lastClaudeTurnEffort([transcript('c.jsonl', [user, assistant({})])])).toBeUndefined();
    expect(lastClaudeTurnEffort([transcript('d.jsonl', [])])).toBeUndefined();
  });

  it('skips missing files and uses the first one that exists', () => {
    const path = transcript('e.jsonl', [assistant({ effort: 'xhigh' })]);
    expect(lastClaudeTurnEffort([join(root, 'missing.jsonl'), path])).toEqual({ effort: 'xhigh', model: 'claude-opus-5-5' });
    expect(lastClaudeTurnEffort([join(root, 'missing.jsonl')])).toBeUndefined();
  });

  it('finds a record across read-chunk boundaries and ignores a torn final line', () => {
    const filler = { type: 'user', message: { role: 'user', content: 'x'.repeat(300 * 1024) } };
    const path = transcript('f.jsonl', [assistant({ effort: 'high', requestedModel: 'claude-opus-5-5' }), filler, filler],
      '{"type":"assistant","effort":"lo');
    expect(lastClaudeTurnEffort([path])).toEqual({ effort: 'high', model: 'claude-opus-5-5' });
  });
});

describe('default /think line', () => {
  it('names the measured Claude Code effort, or says it is not known yet', () => {
    expect(describeClaudeDefaultReasoning({ effort: 'medium', model: 'claude-opus-5-5' }))
      .toBe('默认（上一轮实际 medium，模型 claude-opus-5-5）');
    expect(describeClaudeDefaultReasoning(undefined)).toBe('默认（由 Claude Code 决定，首轮回复后可见实际档位）');
  });

  it('names the Codex effort Relay passes and where it comes from', () => {
    vi.stubEnv('PULSE_CODEX_REASONING_EFFORT', '');
    vi.stubEnv('RELAY_CODEX_REASONING_EFFORT', '');
    vi.stubEnv('PULSE_RUNTIME_PRIORITY_PATH', join(root, 'missing-runtime-priority.json'));
    expect(describeCodexDefaultReasoning()).toBe('默认（xhigh，来自 Relay 内置默认）');
    const profile = join(root, 'runtime-priority.json');
    writeFileSync(profile, JSON.stringify({
      schema_version: 1,
      runtime_priority: ['codex'],
      codex: { model: 'gpt-managed', reasoning_effort: 'medium', yolo: true },
      claude: { model: 'claude-managed' },
    }));
    vi.stubEnv('PULSE_RUNTIME_PRIORITY_PATH', profile);
    expect(describeCodexDefaultReasoning()).toBe(`默认（medium，来自 ${profile} 的 codex.reasoning_effort）`);
    vi.stubEnv('RELAY_CODEX_REASONING_EFFORT', 'low');
    expect(describeCodexDefaultReasoning()).toBe('默认（low，来自 环境变量 RELAY_CODEX_REASONING_EFFORT）');
    vi.stubEnv('PULSE_CODEX_REASONING_EFFORT', 'high');
    expect(describeCodexDefaultReasoning()).toBe('默认（high，来自 环境变量 PULSE_CODEX_REASONING_EFFORT）');
  });
});
