import { afterEach, expect, it, vi } from 'vitest';
import { mkdtempSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { acquireConversation } from '../src/conversation-acquisition.js';
import { createCcSdkConversationRuntime } from '../src/cc-sdk-conversation-runtime.js';
import { createStateManager } from '../src/state.js';

const disposers: (() => void)[] = [];
afterEach(() => { disposers.splice(0).reverse().forEach(dispose => dispose()); });

for (const retained of [false, true]) {
  it(`acquires a real cc-sdk adapter with ${retained ? 'a retained' : 'a new'} channel Session`, async () => {
    const dir = mkdtempSync(join(tmpdir(), 'relay-sdk-acquisition-'));
    disposers.push(() => rmSync(dir, { recursive: true, force: true }));
    const stateManager = createStateManager(join(dir, 'state.json'));
    disposers.push(() => stateManager.flush());
    const poolKey = 'human:pulse', sessionId = '11111111-1111-1111-1111-111111111111';
    if (retained) stateManager.updateSession(poolKey, {
      poolKey, agentId: 'pulse', runtime: 'cc-sdk', sessionId, status: 'active', lastActivity: 1,
    });
    const query = vi.fn(() => { throw new Error('engine must remain lazy during acquisition'); });
    const adapter = createCcSdkConversationRuntime({ stateManager, sdk: { query } });
    disposers.push(() => adapter.dispose());
    const proc = await acquireConversation({
      poolKey, agent: { id: 'pulse', name: 'Pulse', emoji: 'P', workspace: dir, runtime: 'cc-sdk', sessionId },
      runtime: adapter, stateManager, channel: 'telegram', intent: 'inbound',
    });
    expect(proc).toMatchObject({ runtime: 'cc-sdk', state: 'ready', sessionId });
    expect(stateManager.getSession(poolKey)).toMatchObject({ poolKey, agentId: 'pulse', runtime: 'cc-sdk', sessionId });
    expect(query).not.toHaveBeenCalled();
    await expect(acquireConversation({
      poolKey, agent: { id: 'pulse', name: 'Pulse', emoji: 'P', workspace: dir, runtime: 'cc-sdk', sessionId },
      runtime: adapter, stateManager, channel: 'telegram', intent: 'restart',
    })).resolves.toMatchObject({ runtime: 'cc-sdk', state: 'ready', sessionId });
  });
}
