import { describe, expect, it, vi } from 'vitest';
import manifest from '../config/model-release.json';
import {
  canonicalizeSource,
  compareVersions,
  discoverRuntimeCandidates,
  sha256,
  validateManifest,
  verifyLocalRuntime,
  verifyOfficialSources,
} from '../scripts/model-release-gate.mjs';

function commandResult(stdout: string, status = 0) {
  return { status, stdout, stderr: '', signal: null, output: [] } as never;
}

describe('GPT-6 model release gate', () => {
  it('accepts only the exact official route and staged human-gated rollout', () => {
    expect(validateManifest(structuredClone(manifest)).model.id).toBe('gpt-6-astra');
    expect(() => validateManifest({ ...structuredClone(manifest), model: { provider: 'openai-codex', id: 'gpt-6' } }))
      .toThrow('official openai-codex/gpt-6-astra route');
    expect(() => validateManifest({ ...structuredClone(manifest), rollout: { ...manifest.rollout, human_gate_before_activation: false } }))
      .toThrow('human gate');
    expect(() => validateManifest({ ...structuredClone(manifest), runtimes: undefined }))
      .toThrow('Codex runtime contract');
    expect(() => validateManifest({ ...structuredClone(manifest), rollout: { ...manifest.rollout, rollback: undefined } }))
      .toThrow('rollback contract');
    expect(() => validateManifest({ ...structuredClone(manifest), rollout: { ...manifest.rollout, alert_on: [] } }))
      .toThrow('alert contract');
    expect(() => validateManifest({ ...structuredClone(manifest), rollout: { ...manifest.rollout, alerting: undefined } }))
      .toThrow('alert delivery and ownership contract');
    expect(() => validateManifest({
      ...structuredClone(manifest),
      rollout: { ...manifest.rollout, alerting: { ...manifest.rollout.alerting, ack_sla_minutes: 31 } },
    })).toThrow('alert delivery and ownership contract');
    expect(() => validateManifest({
      ...structuredClone(manifest),
      rollout: { ...manifest.rollout, alerting: { ...manifest.rollout.alerting, owner_login: undefined } },
    })).toThrow('alert delivery and ownership contract');
  });

  it('pins every official authority to its exact repository or documentation URL', () => {
    const changed = structuredClone(manifest);
    changed.sources[1].url = 'https://api.github.com/repos/untrusted-owner/untrusted-repo/releases/tags/rust-v0.153.4';
    expect(() => validateManifest(changed)).toThrow('untrusted source URL');
  });

  it('compares runtime versions without treating prerelease suffixes as a newer numeric version', () => {
    expect(compareVersions('codex-cli 0.153.4', '0.153.4')).toBe(0);
    expect(compareVersions('codex-cli 0.145.0-alpha.9', '0.153.4')).toBeLessThan(0);
    expect(compareVersions('codex-cli 0.153.4-alpha.1', '0.153.4')).toBeLessThan(0);
    expect(compareVersions('2.1.261', '2.1.260')).toBeGreaterThan(0);
  });

  it('canonicalizes official JSON fields and HTML text deterministically', () => {
    const source = { canonical_json_fields: ['tag_name', 'body'] };
    expect(canonicalizeSource('{"body":"Astra","tag_name":"rust-v0.153.4","volatile":1}', source))
      .toBe('{"body":"Astra","tag_name":"rust-v0.153.4"}');
    expect(canonicalizeSource('<h1>GPT-6&nbsp; Astra</h1>  gpt-6-astra', {}))
      .toContain('gpt-6');
    expect(sha256('same')).toBe(sha256('same'));
  });

  it('fails closed when an official assertion disappears and reports digest drift separately', async () => {
    const oneSource = {
      ...structuredClone(manifest),
      sources: [{
        authority: 'OpenAI',
        url: 'https://developers.openai.com/model',
        canonical_sha256: sha256('GPT-6 Astra gpt-6-astra'),
        required_text: ['GPT-6 Astra', 'gpt-6-astra'],
      }],
    };
    const goodFetch = vi.fn(async () => new Response('GPT-6 Astra gpt-6-astra'));
    await expect(verifyOfficialSources(oneSource, goodFetch)).resolves.toEqual([
      expect.objectContaining({ source_drift: false }),
    ]);
    const changedFetch = vi.fn(async () => new Response('GPT-6 Astra gpt-6-astra changed'));
    await expect(verifyOfficialSources(oneSource, changedFetch)).resolves.toEqual([
      expect.objectContaining({ source_drift: true }),
    ]);
    const badFetch = vi.fn(async () => new Response('GPT-6 Astra'));
    await expect(verifyOfficialSources(oneSource, badFetch)).rejects.toThrow('official source assertion failed');
  });

  it('separates latest candidate discovery from pinned release evidence', async () => {
    const latest = vi.fn(async () => new Response(JSON.stringify({
      name: '@anthropic-ai/claude-agent-sdk', version: '0.3.263',
    })));
    await expect(discoverRuntimeCandidates(manifest, latest)).resolves.toEqual([{
      runtime: 'claude_agent_sdk',
      pinned_version: '0.3.261',
      latest_version: '0.3.263',
      candidate_available: true,
    }]);
  });

  it('requires runtime version readback and exact Pi model catalog membership', () => {
    const spawnSync = vi.fn((command: string, args: string[]) => {
      if (command === 'codex') return commandResult('codex-cli 0.153.4\n');
      if (args[0] === '--version') return commandResult('0.84.2\n');
      return commandResult('provider      model\nopenai-codex  gpt-6-astra\n');
    });
    expect(verifyLocalRuntime(manifest, { spawnSync, codexBin: 'codex', sdkVersion: '0.3.261' })).toEqual({
      codex_version: 'codex-cli 0.153.4',
      pi_version: '0.84.2',
      claude_agent_sdk_version: '0.3.261',
      model: 'openai-codex/gpt-6-astra',
    });

    const oldCodex = vi.fn(() => commandResult('codex-cli 0.145.0-alpha.9\n'));
    expect(() => verifyLocalRuntime(manifest, {
      spawnSync: oldCodex,
      codexBin: 'codex',
      sdkVersion: '0.3.261',
    })).toThrow('Codex runtime too old');

    const suffixImpostor = vi.fn((command: string, args: string[]) => {
      if (command === 'codex') return commandResult('codex-cli 0.153.4\n');
      if (args[0] === '--version') return commandResult('0.84.2\n');
      return commandResult('provider      model\nopenai-codex  gpt-6-astra-impostor\n');
    });
    expect(() => verifyLocalRuntime(manifest, {
      spawnSync: suffixImpostor,
      codexBin: 'codex',
      sdkVersion: '0.3.261',
    })).toThrow('model missing from Pi catalog');
  });
});
