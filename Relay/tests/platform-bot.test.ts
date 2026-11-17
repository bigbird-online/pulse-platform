/** Platform confirmations: owner only, bound to one request, recorded as Host human input. */
import { describe, it, expect, afterAll } from 'vitest';
import { createHash } from 'node:crypto';
import { mkdirSync, mkdtempSync, readFileSync, realpathSync, rmSync, statSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { cardText, decide, readRequest, revoke, unsentRequests, markSent, REQUEST_TTL_MS, startPlatformBot } from '../src/platform-bot.js';
import type { Bot } from 'grammy';

const root = realpathSync(mkdtempSync(join(tmpdir(), 'platform-bot-')));
const inputs = join(root, 'human-inputs');
const env = { PULSE_CORE_HUMAN_INPUTS: inputs } as NodeJS.ProcessEnv;
const settings = { root: join(root, 'entries'), owner: '1000000001', entries: ['chatgpt_web'] };
const now = Date.parse('2026-10-16T10:00:00Z');

function request(id: string, value: Record<string, unknown>) {
  mkdirSync(join(settings.root, 'requests'), { recursive: true });
  writeFileSync(join(settings.root, 'requests', `${id}.json`), JSON.stringify({
    schema: 'pulse.platform-request/v1', request_id: id, created_at: new Date(now).toISOString(), ...value }));
}
const ref = (event: string, pool: string) =>
  createHash('sha256').update(JSON.stringify(['telegram', event, settings.owner, pool])).digest('hex');

describe('platform bot', () => {
  afterAll(() => rmSync(root, { recursive: true, force: true }));

  it('confirms a binding once, for the owner only', () => {
    const id = 'a'.repeat(32);
    request(id, { kind: 'binding', entry: 'chatgpt_web', actor: 'asobi' });
    expect(unsentRequests(settings).map(r => r.request_id)).toEqual([id]);
    expect(cardText(readRequest(settings, id))).toContain('asobi');
    markSent(settings, id, settings.owner, 7);
    expect(unsentRequests(settings)).toEqual([]);
    expect(decide(settings, id, 'y', '1234', 'cb-1', now, env)).toBeUndefined();
    const result = decide(settings, id, 'y', settings.owner, 'cb-1', now, env)!;
    expect(result.status).toBe('confirmed');
    if (result.status !== 'confirmed') throw new Error('unreachable');
    expect(result.human_input_ref).toBe(ref('cb-1', `entry-binding:${id}`));
    const record = JSON.parse(readFileSync(join(inputs, `${result.human_input_ref}.json`), 'utf8'));
    expect(record.poolKey).toBe(`entry-binding:${id}`);
    expect(JSON.parse(record.source.message)).toEqual({ schema: 'pulse.platform-confirmation/v1',
      kind: 'binding', request_id: id, entry: 'chatgpt_web', actor: 'asobi' });
    const bindingFile = join(settings.root, 'bindings', `${result.binding_id}.json`);
    expect(statSync(bindingFile).mode & 0o077).toBe(0);
    expect(JSON.parse(readFileSync(bindingFile, 'utf8'))).toMatchObject({ actor: 'asobi',
      request_id: id, human_input_ref: result.human_input_ref });
    // A repeated press returns the same result and writes no second binding.
    expect(decide(settings, id, 'y', settings.owner, 'cb-2', now, env)).toEqual(result);
    expect(revoke(settings, result.binding_id!, '1234', now)).toBe(false);
    expect(revoke(settings, result.binding_id!, settings.owner, now)).toBe(true);
    expect(JSON.parse(readFileSync(bindingFile, 'utf8')).revoked_at).toBeTruthy();
  });

  it('binds a decision to its session and exact input digest', () => {
    const id = 'b'.repeat(32);
    request(id, { kind: 'decision', entry: 'chatgpt_web', actor: 'asobi', binding_id: 'c'.repeat(32),
      intent: 'add_requirement', work_item_id: 'wi_x', input_digest: 'd'.repeat(64), summary: 'add_requirement wi_x' });
    const result = decide(settings, id, 'y', settings.owner, 'cb-3', now, env)!;
    if (result.status !== 'confirmed') throw new Error('expected confirmation');
    const record = JSON.parse(readFileSync(join(inputs, `${result.human_input_ref}.json`), 'utf8'));
    expect(record.poolKey).toBe(`entry:${'c'.repeat(32)}`);
    expect(JSON.parse(record.source.message)).toMatchObject({ kind: 'decision', intent: 'add_requirement',
      work_item_id: 'wi_x', input_digest: 'd'.repeat(64) });
  });

  it('a polling conflict stops only the platform bot', async () => {
    const rejected = new Error('409: Conflict: terminated by other getUpdates request');
    let stopped = 0;
    const bot = { api: { sendMessage: async () => ({ message_id: 1 }) }, on: () => undefined,
      catch: () => undefined, start: () => Promise.reject(rejected),
      stop: async () => { stopped += 1; } } as unknown as Bot;
    const handle = startPlatformBot(bot, { ...settings, root: join(root, 'empty') }, 60_000);
    await new Promise(resolve => setTimeout(resolve, 10));
    handle.stop();
    expect(stopped).toBe(1);
  });

  it('expires and declines without any human input record', () => {
    request('e'.repeat(32), { kind: 'binding', entry: 'chatgpt_web', actor: 'asobi' });
    expect(decide(settings, 'e'.repeat(32), 'y', settings.owner, 'cb-4', now + REQUEST_TTL_MS + 1, env))
      .toEqual({ status: 'expired' });
    request('f'.repeat(32), { kind: 'binding', entry: 'chatgpt_web', actor: 'asobi' });
    expect(decide(settings, 'f'.repeat(32), 'n', settings.owner, 'cb-5', now, env)).toEqual({ status: 'declined' });
    expect(() => readFileSync(join(inputs, `${ref('cb-4', `entry-binding:${'e'.repeat(32)}`)}.json`))).toThrow();
  });
});
