/** The native session proof names the conversation entry; the core never parses pool keys. */
import { describe, it, expect, vi, afterAll } from 'vitest';
import { mkdtempSync, readFileSync, realpathSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';

const data = realpathSync(mkdtempSync(join(tmpdir(), 'relay-proof-')));
vi.mock('../src/managed-paths.js', () => ({ managedRelayPath: () => data }));
vi.mock('../src/native-work-item-command.js', () => ({
  nodeWorkItemCore: () => ({ python: '/usr/bin/python3', entrypoint: '/x/harness.pyz', extension: '/x/runtime/pi.mjs' }),
}));
const { observeNativeSession } = await import('../src/native-core-runtime.js');

const proof = () => JSON.parse(readFileSync(join(data, 'native-session-proofs', `${process.pid}.json`), 'utf8'));

describe('native session proof entry', () => {
  afterAll(() => rmSync(data, { recursive: true, force: true }));

  it('records Telegram, Feishu, or an unparsed Relay key', () => {
    observeNativeSession(process.pid, 'session-a', 'owner:pulse', 'cc');
    expect(proof()).toMatchObject({ tenant: 'owner:pulse', entry: 'telegram' });
    observeNativeSession(process.pid, 'session-b', 'owner:pulse:feishu', 'pi');
    expect(proof()).toMatchObject({ tenant: 'owner:pulse:feishu', entry: 'feishu' });
    observeNativeSession(process.pid, 'session-c', 'cron-job-key', 'cc');
    expect(proof()).toMatchObject({ tenant: 'cron-job-key', entry: 'relay' });
  });
});
