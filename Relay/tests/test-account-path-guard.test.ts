import { afterEach, describe, expect, it, vi } from 'vitest';
import { existsSync, readdirSync, realpathSync } from 'node:fs';
import { join, relative } from 'node:path';
import { tmpdir, userInfo } from 'node:os';
import { randomUUID } from 'node:crypto';
import { annotatedHumanMessage, recordHumanInput } from '../src/scl-human-decision.js';
import { latestDurableFeishuInput } from '../src/feishu-inbound-recovery.js';
import { guardAccountStatePath, takeAccountStatePathViolations } from '../src/test-account-path-guard.js';

const accountInputs = join(userInfo().homedir, '.local/state/agent-harness/scl-host/human-inputs');
const unisolated = (): NodeJS.ProcessEnv => {
  vi.stubEnv('SCL_HUMAN_DECISION_KEY_FILE', undefined);
  vi.stubEnv('PULSE_CORE_HUMAN_INPUTS', undefined);
  return process.env;
};
const source = () => ({ channel: 'telegram' as const, eventId: 'guard-' + randomUUID(),
  humanRef: 'synthetic-test-human', message: 'synthetic guard probe' });
const accountInputCount = () => existsSync(accountInputs) ? readdirSync(accountInputs).length : 0;
const inAccountInputs = (path: string) => !relative(accountInputs, path).startsWith('..');

afterEach(() => { vi.unstubAllEnvs(); });

describe('account-level Host state under test', () => {
  it('gives every test file its own temporary Host state root', () => {
    for (const name of ['SCL_HUMAN_DECISION_KEY_FILE', 'PULSE_CORE_HUMAN_INPUTS']) {
      const value = process.env[name]!;
      expect(value.startsWith(tmpdir()) || value.startsWith(realpathSync(tmpdir()))).toBe(true);
    }
  });

  it('refuses a human input write that falls back to the system account home', () => {
    const before = accountInputCount();
    expect(() => recordHumanInput(source(), 'guard:pulse', unisolated())).toThrow(/refused account-level Host state/);
    const refused = takeAccountStatePathViolations();
    expect(refused).toHaveLength(1);
    expect(inAccountInputs(refused[0]!)).toBe(true);
    expect(existsSync(refused[0]!)).toBe(false);
    expect(accountInputCount()).toBe(before);
  });

  it('records the refusal even when the caller degrades instead of throwing', () => {
    const message = annotatedHumanMessage('hello', source(), 'guard:pulse', unisolated());
    expect(String(message.content)).toContain('SCL_HOST_ADAPTER_DEGRADED');
    const refused = takeAccountStatePathViolations();
    expect(refused).toHaveLength(1);
    expect(existsSync(refused[0]!)).toBe(false);
  });

  it('refuses reading account-level recovery state', () => {
    expect(() => latestDurableFeishuInput('guard:pulse:feishu', unisolated())).toThrow(/refused account-level Host state/);
    expect(takeAccountStatePathViolations()).toHaveLength(1);
  });

  it('leaves temporary paths and non-test processes unchanged', () => {
    const temporary = join(tmpdir(), 'relay-guard-probe', 'x.json');
    expect(guardAccountStatePath(temporary)).toBe(temporary);
    vi.stubEnv('VITEST', '');
    expect(guardAccountStatePath(join(accountInputs, 'x.json'))).toBe(join(accountInputs, 'x.json'));
    expect(takeAccountStatePathViolations()).toEqual([]);
  });
});
