import { describe, it, expect, vi } from 'vitest';
import { chmodSync, existsSync, mkdirSync, mkdtempSync, readdirSync, readFileSync, realpathSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { basename, dirname, join } from 'node:path';
import {
  createBatchRuntimeOrchestrator,
  createCcBatchRuntimeAdapter,
  createCcInteractiveBatchRuntimeAdapter,
  createCodexBatchRuntimeAdapter,
  extractInteractiveRunOutcome,
  type BatchRuntimeAdapter,
  type BatchRunResult,
} from '../src/runtime.js';
import type { RuntimeId } from '../src/types.js';
import { ccPiState, ccPiWrites, ccSandboxEnv } from '../src/cc-write-boundary.js';

function makeResult(runtime: RuntimeId, overrides: Partial<BatchRunResult> = {}): BatchRunResult {
  return {
    runtime,
    stdout: '',
    stderr: '',
    exitCode: 0,
    durationMs: 10,
    timedOut: false,
    ...overrides,
  };
}

function makeAdapter(
  id: RuntimeId,
  run: BatchRuntimeAdapter['run'],
  classifyFailure: BatchRuntimeAdapter['classifyFailure'],
): BatchRuntimeAdapter {
  return { id, run, classifyFailure };
}

describe('batch runtime orchestrator', () => {
  it('falls back when primary runtime is unavailable', async () => {
    const ccRun = vi.fn().mockResolvedValue(makeResult('cc', {
      exitCode: 1,
      spawnErrorCode: 'ENOENT',
      spawnErrorMessage: 'claude not found',
    }));
    const codexRun = vi.fn().mockResolvedValue(makeResult('codex', {
      stdout: 'fallback ok',
    }));

    const orchestrator = createBatchRuntimeOrchestrator({
      primary: 'cc',
      fallbacks: ['codex'],
      adapters: [
        makeAdapter('cc', ccRun, (result) => result.spawnErrorCode ? {
          reason: 'binary-missing',
          message: 'cc unavailable',
        } : null),
        makeAdapter('codex', codexRun, () => null),
      ],
    });

    const result = await orchestrator.run({
      workspace: '/tmp',
      prompt: 'test',
      timeoutMs: 1000,
    });

    expect(result.runtime).toBe('codex');
    expect(result.attemptedRuntimes).toEqual(['cc', 'codex']);
    expect(result.stdout).toBe('fallback ok');
    expect(ccRun).toHaveBeenCalledOnce();
    expect(codexRun).toHaveBeenCalledOnce();
  });

  it('does not fall back on task-level non-zero exit', async () => {
    const ccRun = vi.fn().mockResolvedValue(makeResult('cc', {
      exitCode: 1,
      stderr: 'task failed',
    }));
    const codexRun = vi.fn().mockResolvedValue(makeResult('codex', {
      stdout: 'should not run',
    }));

    const orchestrator = createBatchRuntimeOrchestrator({
      primary: 'cc',
      fallbacks: ['codex'],
      adapters: [
        makeAdapter('cc', ccRun, () => null),
        makeAdapter('codex', codexRun, () => null),
      ],
    });

    const result = await orchestrator.run({
      workspace: '/tmp',
      prompt: 'test',
      timeoutMs: 1000,
    });

    expect(result.runtime).toBe('cc');
    expect(result.attemptedRuntimes).toEqual(['cc']);
    expect(result.exitCode).toBe(1);
    expect(codexRun).not.toHaveBeenCalled();
  });

  it('sticks to fallback while primary is cooling down', async () => {
    const ccRun = vi.fn().mockResolvedValue(makeResult('cc', {
      stdout: 'primary ok',
    }));
    const codexRun = vi.fn().mockResolvedValue(makeResult('codex', {
      stdout: 'fallback ok',
    }));

    const orchestrator = createBatchRuntimeOrchestrator({
      primary: 'cc',
      fallbacks: ['codex'],
      adapters: [
        makeAdapter('cc', ccRun, () => null),
        makeAdapter('codex', codexRun, () => null),
      ],
    });

    orchestrator.markUnavailable('cc', 60_000);

    const duringCooldown = await orchestrator.run({
      workspace: '/tmp',
      prompt: 'cooldown',
      timeoutMs: 1000,
    });
    expect(duringCooldown.runtime).toBe('codex');
    expect(duringCooldown.attemptedRuntimes).toEqual(['codex']);
    expect(ccRun).not.toHaveBeenCalled();

    orchestrator.markHealthy('cc');

    const afterRecovery = await orchestrator.run({
      workspace: '/tmp',
      prompt: 'recovered',
      timeoutMs: 1000,
    });
    expect(afterRecovery.runtime).toBe('cc');
    expect(ccRun).toHaveBeenCalledOnce();
  });

  it('falls back on rate limit and marks runtime unavailable', async () => {
    const ccRun = vi.fn().mockResolvedValue(makeResult('cc', {
      exitCode: 1,
      stderr: 'API Error: Request rejected (429) · Rate limit reached for requests',
    }));
    const codexRun = vi.fn().mockResolvedValue(makeResult('codex', {
      stdout: 'fallback ok',
    }));

    const orchestrator = createBatchRuntimeOrchestrator({
      primary: 'cc',
      fallbacks: ['codex'],
      adapters: [
        makeAdapter('cc', ccRun, (result) => result.stderr.includes('429') ? {
          reason: 'rate-limit',
          message: 'cc rate limited',
          cooldownMs: 60_000,
        } : null),
        makeAdapter('codex', codexRun, () => null),
      ],
    });

    const first = await orchestrator.run({
      workspace: '/tmp',
      prompt: 'rate limit',
      timeoutMs: 1000,
    });

    expect(first.runtime).toBe('codex');
    expect(first.attemptedRuntimes).toEqual(['cc', 'codex']);
    expect(first.exitCode).toBe(0);
    expect(first.stdout).toBe('fallback ok');
    expect(ccRun).toHaveBeenCalledOnce();
    expect(codexRun).toHaveBeenCalledOnce();

    const second = await orchestrator.run({
      workspace: '/tmp',
      prompt: 'after cooldown mark',
      timeoutMs: 1000,
    });

    expect(second.runtime).toBe('codex');
    expect(second.attemptedRuntimes).toEqual(['codex']);
    expect(codexRun).toHaveBeenCalledTimes(2);
    expect(ccRun).toHaveBeenCalledOnce();
  });

  it('falls back on cc context-window exhaustion', async () => {
    const ccRun = vi.fn().mockResolvedValue(makeResult('cc', {
      exitCode: 1,
      stdout: 'API Error: The model has reached its context window limit.',
    }));
    const codexRun = vi.fn().mockResolvedValue(makeResult('codex', {
      stdout: 'codex generated report',
    }));
    const ccAdapter = createCcBatchRuntimeAdapter();

    const orchestrator = createBatchRuntimeOrchestrator({
      primary: 'cc',
      fallbacks: ['codex'],
      adapters: [
        makeAdapter('cc', ccRun, ccAdapter.classifyFailure),
        makeAdapter('codex', codexRun, () => null),
      ],
    });

    const result = await orchestrator.run({
      workspace: '/tmp',
      prompt: 'daily report',
      timeoutMs: 1000,
    });

    expect(result.runtime).toBe('codex');
    expect(result.attemptedRuntimes).toEqual(['cc', 'codex']);
    expect(result.exitCode).toBe(0);
    expect(result.stdout).toBe('codex generated report');
    expect(ccRun).toHaveBeenCalledOnce();
    expect(codexRun).toHaveBeenCalledOnce();
  });

  it('falls back to cc when the codex model is at capacity', async () => {
    const codexRun = vi.fn().mockResolvedValue(makeResult('codex', {
      exitCode: 1,
      stderr: 'OpenAI Codex v0.165.0\nhook: UserPromptSubmit Failed\n'
        + 'ERROR: Selected model is at capacity. Please try a different model.\n',
    }));
    const ccRun = vi.fn().mockResolvedValue(makeResult('cc', { stdout: 'inbox handled' }));
    const codexAdapter = createCodexBatchRuntimeAdapter();

    const orchestrator = createBatchRuntimeOrchestrator({
      primary: 'codex',
      fallbacks: ['cc'],
      adapters: [
        makeAdapter('codex', codexRun, codexAdapter.classifyFailure),
        makeAdapter('cc', ccRun, () => null),
      ],
    });

    const result = await orchestrator.run({
      workspace: '/tmp',
      prompt: 'collab inbox',
      timeoutMs: 1000,
    }, ['codex', 'cc']);

    expect(result.runtime).toBe('cc');
    expect(result.attemptedRuntimes).toEqual(['codex', 'cc']);
    expect(result.exitCode).toBe(0);
  });

  it('uses danger-full-access arguments for codex batch yolo fallback', async () => {
    const tempDir = mkdtempSync(join(tmpdir(), 'relay-codex-yolo-test-'));
    const fakeCodex = join(tempDir, 'codex-fake.cjs');
    const argsPath = join(tempDir, 'args.json');
    const oldCodexBin = process.env['CODEX_BIN'];
    const oldArgsPath = process.env['RELAY_TEST_CODEX_ARGS_PATH'];

    writeFileSync(fakeCodex, `#!/usr/bin/env node
const fs = require('node:fs');
const args = process.argv.slice(2);
fs.writeFileSync(process.env.RELAY_TEST_CODEX_ARGS_PATH, JSON.stringify(args));
const outputFlag = args.indexOf('--output-last-message');
if (outputFlag >= 0 && args[outputFlag + 1]) {
  fs.writeFileSync(args[outputFlag + 1], 'fake codex ok');
}
process.stdin.resume();
process.stdin.on('end', () => process.exit(0));
`, 'utf-8');
    chmodSync(fakeCodex, 0o755);

    try {
      process.env['CODEX_BIN'] = fakeCodex;
      process.env['RELAY_TEST_CODEX_ARGS_PATH'] = argsPath;

      const adapter = createCodexBatchRuntimeAdapter();
      const result = await adapter.run({
        workspace: tempDir,
        prompt: 'test',
        timeoutMs: 1000,
        codexYolo: true,
      });
      // Retry: child process may not have flushed args.json yet under load
      let args: string[] | undefined;
      for (let attempt = 0; attempt < 20; attempt++) {
        if (existsSync(argsPath)) { args = JSON.parse(readFileSync(argsPath, 'utf-8')) as string[]; break; }
        await new Promise(r => setTimeout(r, 50));
      }
      if (!args) throw new Error(`args.json not written by child process at ${argsPath}`);

      expect(result.exitCode).toBe(0);
      expect(result.stdout).toBe('fake codex ok');
      expect(args).toContain('--dangerously-bypass-approvals-and-sandbox');
      expect(args).toContain('-m');
      expect(args[args.indexOf('-m') + 1]).toBe('gpt-6-astra');
      expect(args).toContain('-c');
      expect(args).toContain('model_reasoning_effort="xhigh"');
      expect(args).not.toContain('-s');
      expect(args).not.toContain('workspace-write');
    } finally {
      if (oldCodexBin === undefined) delete process.env['CODEX_BIN'];
      else process.env['CODEX_BIN'] = oldCodexBin;
      if (oldArgsPath === undefined) delete process.env['RELAY_TEST_CODEX_ARGS_PATH'];
      else process.env['RELAY_TEST_CODEX_ARGS_PATH'] = oldArgsPath;
      rmSync(tempDir, { recursive: true, force: true });
    }
  });

  it('does not judge a Codex run by text on its stderr', async () => {
    // Trigger phrases are assembled at runtime: a reviewer printing this file
    // under an older Relay must not be aborted by its own literal text.
    const trigger = ['login', 'expired'].join(' ');
    const cases: Array<[string, number]> = [
      [`    text.includes('${trigger}') ||\n`, 0],
      // A tool output line may itself start with ERROR:; Codex reports its own
      // provider failures by exiting non-zero, which the orchestrator classifies.
      [`ERROR: OAuth ${trigger}; please sign in again\n`, 0],
    ];
    for (const [stderrText, expected] of cases) {
      const tempDir = mkdtempSync(join(tmpdir(), 'relay-codex-stderr-test-'));
      const fakeCodex = join(tempDir, 'codex-fake.cjs');
      const oldCodexBin = process.env['CODEX_BIN'];
      writeFileSync(fakeCodex, `#!/usr/bin/env node
const fs = require('node:fs');
const args = process.argv.slice(2);
const outputFlag = args.indexOf('--output-last-message');
if (outputFlag >= 0 && args[outputFlag + 1]) fs.writeFileSync(args[outputFlag + 1], 'review recorded');
process.stderr.write(${JSON.stringify(stderrText)});
process.stdin.resume();
process.stdin.on('end', () => setTimeout(() => process.exit(0), 50));
`, 'utf-8');
      chmodSync(fakeCodex, 0o755);
      try {
        process.env['CODEX_BIN'] = fakeCodex;
        const result = await createCodexBatchRuntimeAdapter().run({ workspace: tempDir, prompt: 'review', timeoutMs: 5000 });
        expect(result.exitCode === 0 ? 0 : 1).toBe(expected);
      } finally {
        if (oldCodexBin === undefined) delete process.env['CODEX_BIN'];
        else process.env['CODEX_BIN'] = oldCodexBin;
        rmSync(tempDir, { recursive: true, force: true });
      }
    }
  });

  it('passes explicit model and reasoning effort to the Codex batch process', async () => {
    const tempDir = mkdtempSync(join(tmpdir(), 'relay-codex-model-test-'));
    const fakeCodex = join(tempDir, 'codex-fake.cjs');
    const argsPath = join(tempDir, 'args.json');
    const oldCodexBin = process.env['CODEX_BIN'];

    writeFileSync(fakeCodex, `#!/usr/bin/env node
const fs = require('node:fs');
const args = process.argv.slice(2);
fs.writeFileSync(${JSON.stringify(argsPath)}, JSON.stringify(args));
const outputFlag = args.indexOf('--output-last-message');
if (outputFlag >= 0 && args[outputFlag + 1]) {
  fs.writeFileSync(args[outputFlag + 1], 'fake codex ok');
}
process.stdin.resume();
process.stdin.on('end', () => process.exit(0));
`, 'utf-8');
    chmodSync(fakeCodex, 0o755);

    try {
      process.env['CODEX_BIN'] = fakeCodex;
      const adapter = createCodexBatchRuntimeAdapter();
      await adapter.run({
        workspace: tempDir,
        prompt: 'daily report',
        timeoutMs: 1000,
        codexModel: 'gpt-5.6-terra',
        codexReasoningEffort: 'medium',
      });
      const args = JSON.parse(readFileSync(argsPath, 'utf-8')) as string[];
      expect(args).toContain('-m');
      expect(args[args.indexOf('-m') + 1]).toBe('gpt-5.6-terra');
      expect(args).toContain('model_reasoning_effort="medium"');
    } finally {
      if (oldCodexBin === undefined) delete process.env['CODEX_BIN'];
      else process.env['CODEX_BIN'] = oldCodexBin;
      rmSync(tempDir, { recursive: true, force: true });
    }
  });

  it('passes --model and --effort to the CC batch process when explicitly resolved', async () => {
    const tempDir = mkdtempSync(join(tmpdir(), 'relay-cc-model-test-'));
    const fakeClaude = join(tempDir, 'claude-fake.cjs');
    const argsPath = join(tempDir, 'args.json');
    const oldClaudeBin = process.env['CLAUDE_BIN'];

    writeFileSync(fakeClaude, `#!/usr/bin/env node
const fs = require('node:fs');
fs.writeFileSync(${JSON.stringify(argsPath)}, JSON.stringify(process.argv.slice(2)));
process.stdin.resume();
process.stdin.on('end', () => process.exit(0));
`, 'utf-8');
    chmodSync(fakeClaude, 0o755);

    try {
      process.env['CLAUDE_BIN'] = fakeClaude;
      const adapter = createCcBatchRuntimeAdapter();
      await adapter.run({
        workspace: tempDir,
        prompt: 'daily report',
        timeoutMs: 1000,
        ccModel: 'sonnet',
        ccReasoningEffort: 'medium',
      });
      const args = JSON.parse(readFileSync(argsPath, 'utf-8')) as string[];
      expect(args).toContain('--model');
      expect(args[args.indexOf('--model') + 1]).toBe('sonnet');
      expect(args).toContain('--effort');
      expect(args[args.indexOf('--effort') + 1]).toBe('medium');
    } finally {
      if (oldClaudeBin === undefined) delete process.env['CLAUDE_BIN'];
      else process.env['CLAUDE_BIN'] = oldClaudeBin;
      rmSync(tempDir, { recursive: true, force: true });
    }
  });

  it('does not pass inherited provider routing env to the CC batch process', async () => {
    const tempDir = mkdtempSync(join(tmpdir(), 'relay-cc-env-test-'));
    const fakeClaude = join(tempDir, 'claude-fake.cjs');
    const envPath = join(tempDir, 'env.json');
    const inherited: Record<string, string> = {
      ANTHROPIC_BASE_URL: 'https://api.z.ai/api/anthropic',
      ANTHROPIC_AUTH_TOKEN: 'inherited-token',
      ANTHROPIC_API_KEY: 'inherited-key',
      ANTHROPIC_DEFAULT_HAIKU_MODEL: 'retired-haiku',
      ANTHROPIC_DEFAULT_SONNET_MODEL: 'retired-sonnet',
      ANTHROPIC_DEFAULT_OPUS_MODEL: 'retired-opus',
      ANTHROPIC_CUSTOM_MODEL_OPTION: 'retired-custom',
      GLM_ANTHROPIC_AUTH_TOKEN: 'glm-token',
      KIMI_ANTHROPIC_BASE_URL: 'https://kimi.invalid',
      MINIMAX_ANTHROPIC_AUTH_TOKEN: 'minimax-token',
      DEEPSEEK_ANTHROPIC_BASE_URL: 'https://deepseek.invalid',
      XFYUN_ANTHROPIC_AUTH_TOKEN: 'xfyun-token',
      RELAY_TEST_KEEP_ME: 'kept',
    };
    const saved = new Map<string, string | undefined>();
    for (const key of [...Object.keys(inherited), 'CLAUDE_BIN']) saved.set(key, process.env[key]);

    writeFileSync(fakeClaude, `#!/usr/bin/env node
const fs = require('node:fs');
fs.writeFileSync(${JSON.stringify(envPath)}, JSON.stringify(process.env));
process.stdin.resume();
process.stdin.on('end', () => process.exit(0));
`, 'utf-8');
    chmodSync(fakeClaude, 0o755);

    try {
      Object.assign(process.env, inherited, { CLAUDE_BIN: fakeClaude });
      const adapter = createCcBatchRuntimeAdapter();
      await adapter.run({ workspace: tempDir, prompt: 'daily report', timeoutMs: 1000 });
      const childEnv = JSON.parse(readFileSync(envPath, 'utf-8')) as Record<string, string>;
      for (const key of Object.keys(inherited)) {
        if (key === 'RELAY_TEST_KEEP_ME') continue;
        expect(childEnv).not.toHaveProperty(key);
      }
      expect(childEnv['RELAY_TEST_KEEP_ME']).toBe('kept');

      await adapter.run({
        workspace: tempDir,
        prompt: 'daily report',
        timeoutMs: 1000,
        env: { ANTHROPIC_BASE_URL: 'https://openrouter.ai/api' },
      });
      const explicitEnv = JSON.parse(readFileSync(envPath, 'utf-8')) as Record<string, string>;
      expect(explicitEnv['ANTHROPIC_BASE_URL']).toBe('https://openrouter.ai/api');
      expect(explicitEnv).not.toHaveProperty('ANTHROPIC_AUTH_TOKEN');
    } finally {
      for (const [key, value] of saved) {
        if (value === undefined) delete process.env[key];
        else process.env[key] = value;
      }
      rmSync(tempDir, { recursive: true, force: true });
    }
  });

  it('applies the declared CC subscription credential to the CC batch process', async () => {
    const tempDir = mkdtempSync(join(tmpdir(), 'relay-cc-oauth-test-'));
    const fakeClaude = join(tempDir, 'claude-fake.cjs');
    const envPath = join(tempDir, 'env.json');
    const argsPath = join(tempDir, 'args.json');
    const oauthFile = join(tempDir, 'cc-oauth.env');
    const saved = new Map<string, string | undefined>();
    for (const key of ['CLAUDE_BIN', 'HOME', 'RELAY_CC_OAUTH_ENV_FILE', 'ANTHROPIC_BASE_URL', 'RELAY_DATA_DIR', 'PULSE_RELAY_PATHS']) {
      saved.set(key, process.env[key]);
    }

    writeFileSync(oauthFile, 'CLAUDE_CODE_OAUTH_TOKEN=declared-subscription-token\n', 'utf-8');
    writeFileSync(fakeClaude, `#!/usr/bin/env node
const fs = require('node:fs');
fs.writeFileSync(${JSON.stringify(envPath)}, JSON.stringify(process.env));
fs.writeFileSync(${JSON.stringify(argsPath)}, JSON.stringify(process.argv.slice(2)));
process.stdin.resume();
process.stdin.on('end', () => process.exit(0));
`, 'utf-8');
    chmodSync(fakeClaude, 0o755);

    try {
      Object.assign(process.env, {
        CLAUDE_BIN: fakeClaude,
        HOME: tempDir,
        RELAY_CC_OAUTH_ENV_FILE: oauthFile,
        ANTHROPIC_BASE_URL: 'https://api.z.ai/api/anthropic',
        RELAY_DATA_DIR: join(tempDir, 'data'),
      });
      delete process.env['PULSE_RELAY_PATHS'];
      const adapter = createCcBatchRuntimeAdapter();
      await adapter.run({ workspace: tempDir, prompt: 'daily report', timeoutMs: 1000 });
      const childEnv = JSON.parse(readFileSync(envPath, 'utf-8')) as Record<string, string>;
      const args = JSON.parse(readFileSync(argsPath, 'utf-8')) as string[];
      expect(childEnv['CLAUDE_CODE_OAUTH_TOKEN']).toBe('declared-subscription-token');
      expect(childEnv['ANTHROPIC_BASE_URL']).toBe('https://api.anthropic.com');
      expect(args).toContain('--settings');
      expect(JSON.stringify(args)).not.toContain('declared-subscription-token');
      // Without the boundary the sandbox environment stays out of the settings.
      expect(JSON.parse(args[args.indexOf('--settings') + 1]).env).not.toHaveProperty('NODE_USE_ENV_PROXY');

      await adapter.run({
        workspace: tempDir,
        prompt: 'daily report',
        timeoutMs: 1000,
        env: { ANTHROPIC_BASE_URL: 'https://openrouter.ai/api' },
      });
      const routedEnv = JSON.parse(readFileSync(envPath, 'utf-8')) as Record<string, string>;
      const routedArgs = JSON.parse(readFileSync(argsPath, 'utf-8')) as string[];
      expect(routedEnv['ANTHROPIC_BASE_URL']).toBe('https://openrouter.ai/api');
      expect(routedEnv).not.toHaveProperty('CLAUDE_CODE_OAUTH_TOKEN');
      expect(routedArgs).not.toContain('--settings');

      // The owner's write boundary joins the same single --settings value.
      await adapter.run({ workspace: tempDir, prompt: 'daily report', timeoutMs: 1000, ccWriteBoundary: true });
      const bounded = JSON.parse(readFileSync(argsPath, 'utf-8')) as string[];
      expect(bounded.filter((arg) => arg === '--settings')).toHaveLength(1);
      const settings = JSON.parse(bounded[bounded.indexOf('--settings') + 1]);
      const pi = ccPiState(tempDir, tempDir, join(tempDir, 'data', 'cc-pi-agent'), basename(settings.env.PI_CODING_AGENT_DIR))!;
      expect(settings.sandbox).toMatchObject({ enabled: true, allowUnsandboxedCommands: false,
        filesystem: { allowWrite: [realpathSync(tempDir), ...ccPiWrites(pi)] } });
      // The run's own Pi agent directory and owner record go when the run ends.
      expect(readdirSync(dirname(pi.agentDir))).toEqual([]);
      expect(settings.hooks.PreToolUse[0].matcher).toBe('Edit|Write|MultiEdit|NotebookEdit');
      expect(settings.env).toEqual(expect.objectContaining({ ANTHROPIC_BASE_URL: 'https://api.anthropic.com', ...ccSandboxEnv() }));
    } finally {
      for (const [key, value] of saved) {
        if (value === undefined) delete process.env[key];
        else process.env[key] = value;
      }
      rmSync(tempDir, { recursive: true, force: true });
    }
  });

  it('refuses a bounded run it cannot bound, without cooldown or fallback', async () => {
    const tempDir = realpathSync(mkdtempSync(join(tmpdir(), 'relay-cc-boundary-refusal-')));
    const saved = new Map(['HOME', 'RELAY_DATA_DIR', 'PULSE_RELAY_PATHS'].map((key) => [key, process.env[key]]));
    Object.assign(process.env, { HOME: tempDir, RELAY_DATA_DIR: join(tempDir, 'data') });
    delete process.env['PULSE_RELAY_PATHS'];
    try {
      // --bare skips hooks: the interactive batch is refused before it starts.
      const interactive = await createCcInteractiveBatchRuntimeAdapter()
        .run({ workspace: tempDir, prompt: 'x', timeoutMs: 1000, ccWriteBoundary: true });
      expect(interactive).toMatchObject({ runtime: 'cc-interactive', exitCode: 1 });
      expect(interactive.stderr).toContain('--bare');
      // Settings that widen writes refuse the CC run; the job is not retried elsewhere.
      mkdirSync(join(tempDir, '.claude'));
      writeFileSync(join(tempDir, '.claude', 'settings.json'), JSON.stringify({ sandbox: { excludedCommands: ['git'] } }));
      const cc = createCcBatchRuntimeAdapter();
      const refused = await cc.run({ workspace: tempDir, prompt: 'x', timeoutMs: 1000, ccWriteBoundary: true });
      expect(refused).toMatchObject({ runtime: 'cc', exitCode: 1 });
      expect(refused.stderr).toContain('sandbox.excludedCommands');
      expect(cc.classifyFailure(refused)).toBeNull();
    } finally {
      for (const [key, value] of saved) {
        if (value === undefined) delete process.env[key];
        else process.env[key] = value;
      }
      rmSync(tempDir, { recursive: true, force: true });
    }
  });

  it('omits --model for the CC batch process when ccModel is unset', async () => {
    const tempDir = mkdtempSync(join(tmpdir(), 'relay-cc-nomodel-test-'));
    const fakeClaude = join(tempDir, 'claude-fake.cjs');
    const argsPath = join(tempDir, 'args.json');
    const oldClaudeBin = process.env['CLAUDE_BIN'];

    writeFileSync(fakeClaude, `#!/usr/bin/env node
const fs = require('node:fs');
fs.writeFileSync(${JSON.stringify(argsPath)}, JSON.stringify(process.argv.slice(2)));
process.stdin.resume();
process.stdin.on('end', () => process.exit(0));
`, 'utf-8');
    chmodSync(fakeClaude, 0o755);

    try {
      process.env['CLAUDE_BIN'] = fakeClaude;
      const adapter = createCcBatchRuntimeAdapter();
      await adapter.run({
        workspace: tempDir,
        prompt: 'daily report',
        timeoutMs: 1000,
      });
      const args = JSON.parse(readFileSync(argsPath, 'utf-8')) as string[];
      expect(args).not.toContain('--model');
    } finally {
      if (oldClaudeBin === undefined) delete process.env['CLAUDE_BIN'];
      else process.env['CLAUDE_BIN'] = oldClaudeBin;
      rmSync(tempDir, { recursive: true, force: true });
    }
  });

  it('falls back on cc provider safety rejection', async () => {
    const ccRun = vi.fn().mockResolvedValue(makeResult('cc', {
      exitCode: 1,
      stdout: 'API Error: 400 System detected potentially unsafe or sensitive content in input or generation.',
    }));
    const codexRun = vi.fn().mockResolvedValue(makeResult('codex', {
      stdout: 'codex generated report',
    }));
    const ccAdapter = createCcBatchRuntimeAdapter();

    const orchestrator = createBatchRuntimeOrchestrator({
      primary: 'cc',
      fallbacks: ['codex'],
      adapters: [
        makeAdapter('cc', ccRun, ccAdapter.classifyFailure),
        makeAdapter('codex', codexRun, () => null),
      ],
    });

    const result = await orchestrator.run({
      workspace: '/tmp',
      prompt: 'daily report',
      timeoutMs: 1000,
    });

    expect(result.runtime).toBe('codex');
    expect(result.attemptedRuntimes).toEqual(['cc', 'codex']);
    expect(result.exitCode).toBe(0);
    expect(codexRun).toHaveBeenCalledOnce();
  });

  // 2026-08-05 假绿事故：CC 登录过期/周额度耗尽时只回一句合成文本就退出 0。
  // orchestrator 在 exitCode===0 处直接短路返回、根本不问 classifyFailure，
  // 所以这一层必须由 runCommand 把退出码改写成 1 才拦得住——故意跑真 adapter
  // （假 CLI 退出 0 打这句话），mock 掉 run 就会绕过被测的那一行。
  it.each([
    ['login expiry', 'Login expired · Please run /login'],
    ['weekly quota exhaustion', "You've hit your weekly limit · resets 7am (UTC)"],
    ['session quota exhaustion', "You've hit your session limit · resets 5:20pm (Asia/Taipei)"],
  ])('marks a zero-exit cc %s synthetic turn as failure', async (_label, ccStdout) => {
    const tempDir = mkdtempSync(join(tmpdir(), 'relay-cc-quota-test-'));
    const fakeClaude = join(tempDir, 'claude-fake.cjs');
    const oldClaudeBin = process.env['CLAUDE_BIN'];

    writeFileSync(fakeClaude, `#!/usr/bin/env node
process.stdout.write(${JSON.stringify(ccStdout)});
process.exit(0);
`);
    chmodSync(fakeClaude, 0o755);

    try {
      process.env['CLAUDE_BIN'] = fakeClaude;
      const result = await createCcBatchRuntimeAdapter().run({
        workspace: tempDir,
        prompt: 'daily report',
        timeoutMs: 10_000,
      });

      // 真实事故里这里是 0，于是调度器记 lastStatus=ok 而日巡零产出。
      expect(result.exitCode).toBe(1);
      expect(createCcBatchRuntimeAdapter().classifyFailure(result)).not.toBeNull();
    } finally {
      if (oldClaudeBin === undefined) delete process.env['CLAUDE_BIN'];
      else process.env['CLAUDE_BIN'] = oldClaudeBin;
      rmSync(tempDir, { recursive: true, force: true });
    }
  });
});

describe('cc batch rate_limit_event', () => {
  it.each([
    ['allowed_warning', false],
    ['rejected', true],
  ])('classifies status %s like the conversation pool and records quota', async (status, limited) => {
    const tempDir = mkdtempSync(join(tmpdir(), 'relay-cc-rate-limit-test-'));
    const fakeClaude = join(tempDir, 'claude-fake.cjs');
    const quotaPath = join(tempDir, 'quota.json');
    const oldClaudeBin = process.env['CLAUDE_BIN'];
    const oldQuotaPath = process.env['PULSE_CC_QUOTA_PATH'];
    const event = { type: 'rate_limit_event', rate_limit_info: { status,
      unifiedWindows: { seven_day: { utilization: 0.96, resetsAt: 1790906400 } } } };
    writeFileSync(fakeClaude, `#!/usr/bin/env node
process.stdout.write(${JSON.stringify(`${JSON.stringify(event)}\n${JSON.stringify({ type: 'result', result: 'done' })}\n`)});
process.exit(0);
`);
    chmodSync(fakeClaude, 0o755);
    try {
      process.env['CLAUDE_BIN'] = fakeClaude;
      process.env['PULSE_CC_QUOTA_PATH'] = quotaPath;
      const result = await createCcBatchRuntimeAdapter().run({ workspace: tempDir, prompt: 'daily report', timeoutMs: 10_000 });
      expect(result.stderr.includes('API retry')).toBe(limited);
      expect(JSON.parse(readFileSync(quotaPath, 'utf-8')).rate_limit_status).toBe(status);
    } finally {
      if (oldClaudeBin === undefined) delete process.env['CLAUDE_BIN'];
      else process.env['CLAUDE_BIN'] = oldClaudeBin;
      if (oldQuotaPath === undefined) delete process.env['PULSE_CC_QUOTA_PATH'];
      else process.env['PULSE_CC_QUOTA_PATH'] = oldQuotaPath;
      rmSync(tempDir, { recursive: true, force: true });
    }
  });
});

describe('cc-interactive jsonl outcome extraction', () => {
  it('does not treat a tool_use-only turn as terminal success', () => {
    const dir = mkdtempSync(join(tmpdir(), 'relay-runtime-jsonl-'));
    const jsonlPath = join(dir, 'session.jsonl');
    const after = Date.parse('2026-04-25T00:00:00.000Z');

    writeFileSync(jsonlPath, [
      JSON.stringify({
        type: 'user',
        timestamp: '2026-04-25T00:00:01.000Z',
        message: { role: 'user', content: 'run job' },
      }),
      JSON.stringify({
        type: 'assistant',
        timestamp: '2026-04-25T00:00:02.000Z',
        message: {
          role: 'assistant',
          stop_reason: 'tool_use',
          content: [
            { type: 'text', text: 'Checking marker.' },
            { type: 'tool_use', id: 'call_check', name: 'Bash', input: { command: 'test -f marker' } },
          ],
        },
      }),
      JSON.stringify({
        type: 'user',
        timestamp: '2026-04-25T00:00:03.000Z',
        message: {
          role: 'user',
          content: [{ type: 'tool_result', tool_use_id: 'call_check', content: 'EXISTS' }],
        },
      }),
    ].join('\n') + '\n');

    const incomplete = extractInteractiveRunOutcome(jsonlPath, after);
    expect(incomplete.stdout).toBe('Checking marker.');
    expect(incomplete.terminal).toBe(false);
    expect(incomplete.pendingToolUseIds).toEqual([]);
    expect(incomplete.lastStopReason).toBe('tool_use');

    writeFileSync(jsonlPath, `${[
      JSON.stringify({
        type: 'assistant',
        timestamp: '2026-04-25T00:00:04.000Z',
        message: {
          role: 'assistant',
          stop_reason: 'end_turn',
          content: [{ type: 'text', text: 'Done.' }],
        },
      }),
    ].join('\n')}\n`, { flag: 'a' });

    const complete = extractInteractiveRunOutcome(jsonlPath, after);
    expect(complete.stdout).toBe('Checking marker.\nDone.');
    expect(complete.terminal).toBe(true);

    rmSync(dir, { recursive: true, force: true });
  });
});
