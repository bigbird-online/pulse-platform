import { afterEach, describe, expect, it, vi } from 'vitest';
import { publicWorkEvents, sanitizeWorkText } from '../src/work-process-events.js';
import { WorkProcessView, workProcessDetails } from '../src/work-process-view.js';
import { createFeishuStreamingSink } from '../src/channels/feishu.js';
import { enableWorkProcessArchive } from '../src/work-process-archive.js';
afterEach(() => { enableWorkProcessArchive(undefined); vi.unstubAllEnvs(); });

describe('independent review regressions', () => {
  it.each(['cat "/workspace/.env"', "cat '/workspace/CLAUDE.md'", 'cat /workspace/.env; true'])('never publishes private file results for %s', async command => {
    let last: any;
    const view = new WorkProcessView('review', async record => { last = record; return '1'; }, 0);
    view.begin('owner');
    const [start] = publicWorkEvents('pi', { type: 'tool_execution_start', toolCallId: 'a', toolName: 'bash', args: { command } });
    await view.event('owner', 'ou_owner', start);
    await view.event('owner', 'ou_owner', { id: start.id, kind: 'operation', state: 'completed', output: 'synthetic private content' });
    const id = view.idFor('owner');
    await view.callback(`wp:${id}:open`, 'ou_owner', '1');
    expect(JSON.stringify(last)).not.toContain('synthetic private content');
  });

  it('acknowledges a valid Feishu callback before slow message editing finishes', async () => {
    let release!: () => void;
    const pending = new Promise<void>(resolve => { release = resolve; });
    const create = vi.fn(async () => ({ code: 0, data: { message_id: 'msg' } }));
    const patch = vi.fn(async () => { await pending; return { code: 0 }; });
    const agent: any = { id: 'review', name: 'Review', feishu: { appId: 'fixture', appSecret: 'fixture' } };
    const sink = createFeishuStreamingSink(agent, { im: { message: { create, patch } } } as any, vi.fn(), undefined, { workProcess: true, processThrottleMs: 0 })!;
    sink.beginTurn('owner');
    await sink.onAssistant('owner', 'ou_owner', '⏳ 仍在工作，已推进 5 个执行步骤…', 'activity');
    const open = /wp:[a-f0-9]+:open/.exec((create.mock.calls[0] as any)[0].data.content)![0];
    let acknowledged = false;
    const completion = sink.onAction!({ operator: { open_id: 'ou_owner' }, context: { open_message_id: 'msg' }, action: { value: { relay_work_process: open } } }).then(result => { acknowledged = result; });
    await new Promise(resolve => setImmediate(resolve));
    try { expect(acknowledged).toBe(true); } finally { release(); await completion; }
  });
});

it.each(['RELAY_INTERNAL_TOKEN=fixture-sensitive-value', 'CLIENT_SECRET: fixture-sensitive-value', '{\"auth_token\":\"fixture-sensitive-value\"}'])('redacts credential-bearing fields from ordinary tool output: %s', value => {
  expect(sanitizeWorkText(value)).not.toContain('fixture-sensitive-value');
});
