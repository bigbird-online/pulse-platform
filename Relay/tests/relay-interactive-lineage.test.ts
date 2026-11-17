import { createHash } from 'node:crypto';
import { mkdtempSync, readFileSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { afterEach, describe, expect, it } from 'vitest';
import { createMetering } from '../src/metering.js';
import { buildRelayInteractiveLineage } from '../src/relay-interactive-lineage.js';

const dirs: string[] = [];
afterEach(() => {
  for (const dir of dirs.splice(0)) rmSync(dir, { recursive: true, force: true });
});

function expectedRef(domain: string, sessionId: string): string {
  const material = ['pulse.compute_cost.lineage.v1', domain, sessionId].join('\0');
  return `lineage_v1_${createHash('sha256').update(material, 'utf8').digest('hex')}`;
}

describe('Relay interactive lineage producer', () => {
  it('uses runtime-compatible domains and never returns raw producer ids', () => {
    const rawSession = 'private-session-id';
    const cc = buildRelayInteractiveLineage({
      tenantId: 'human-private:duke:feishu',
      sessionId: rawSession,
      runtime: 'cc',
    });
    const codex = buildRelayInteractiveLineage({
      tenantId: 'human-private:duke:telegram',
      sessionId: rawSession,
      runtime: 'codex',
    });
    const pi = buildRelayInteractiveLineage({
      tenantId: 'human-private:duke:telegram',
      sessionId: rawSession,
      runtime: 'pi',
    });

    expect(cc).toMatchObject({
      root_agent: 'duke',
      execution_mode: 'human',
      session_role: 'main',
      parent_run_ref: 'unknown',
      cron_job_ref: 'unknown',
      scan_ref: 'unknown',
    });
    expect(cc.root_run_ref).toBe(expectedRef('cc_root_session', rawSession));
    expect(codex.root_run_ref).toBe(expectedRef('codex_run', rawSession));
    expect(pi.root_run_ref).toBe(expectedRef('pi_rpc_session', rawSession));
    expect(codex.root_run_ref).not.toBe(cc.root_run_ref);
    expect(pi.root_run_ref).not.toBe(codex.root_run_ref);
    expect(JSON.stringify({ cc, codex, pi })).not.toContain(rawSession);
    expect(JSON.stringify({ cc, codex, pi })).not.toContain('human-private');
  });

  it('keeps unverifiable producer keys unknown', () => {
    expect(buildRelayInteractiveLineage({
      tenantId: 'malformed', sessionId: 'unknown', runtime: 'cc',
    })).toMatchObject({ root_agent: 'unknown', root_run_ref: 'unknown' });
    expect(buildRelayInteractiveLineage({
      tenantId: 'user:pulse', sessionId: 'private', runtime: undefined,
    }).root_run_ref).toBe('unknown');
  });

  it('writes an explicit seven-field lineage object without changing legacy rows', () => {
    const dir = mkdtempSync(join(tmpdir(), 'interactive-lineage-'));
    dirs.push(dir);
    const metering = createMetering({ meteringDir: dir });
    const lineage = buildRelayInteractiveLineage({
      tenantId: 'human-private:pulse:feishu',
      sessionId: 'private-session',
      runtime: 'cc-sdk',
    });

    metering.record('human-private:pulse:feishu', 'private-session', {
      type: 'result', usage: { input_tokens: 2, output_tokens: 1 },
    }, { lineage });
    metering.record('legacy:pulse', 'legacy-session', {
      type: 'result', usage: { input_tokens: 1, output_tokens: 1 },
    });

    const day = new Date().toISOString().slice(0, 10);
    const rows = readFileSync(join(dir, `${day}.jsonl`), 'utf8')
      .trim().split('\n').map(line => JSON.parse(line) as Record<string, unknown>);
    expect(rows[0]?.['lineage']).toEqual(lineage);
    expect(JSON.stringify(rows[0]?.['lineage'])).not.toContain('private-session');
    expect(rows[1]).not.toHaveProperty('lineage');
  });
});
