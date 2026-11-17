import { PUBLIC_PROGRESS_POLICY } from '../src/public-progress-policy.js';
import { SESSION_WI_REMINDER } from '../src/session-wi-guard.js';
import { createHash } from 'node:crypto';
import { execFileSync } from 'node:child_process';
import { EventEmitter } from 'node:events';
import { chmodSync, readFileSync, existsSync, mkdirSync, mkdtempSync, realpathSync, rmSync, symlinkSync, unlinkSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join, dirname } from 'node:path';
import { PassThrough } from 'node:stream';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { PI_RUNTIME_COMMIT, PI_RUNTIME_VERSION } from './pi-runtime-fixture.js';
import {
  PI_RUNTIME_MANIFEST_SCHEMA,
  buildPiCommand,
  createPiConversationRuntime,
  type PiRuntimeManifest,
} from '../src/pi-conversation-runtime.js';
import type { ConversationSpawnConfig } from '../src/conversation-runtime.js';
import { buildPiDialogueCommand, validateConfiguredPiProfiles } from '../src/pi-runtime-config.js';
import {safeTenantDirectory} from '../src/pi-runtime-config.js';
import {verifyRetainedPiConversations} from '../src/pi-runtime-preflight.js';
import { relayHostSessionRef } from '../src/session-identity.js';
import { recordHumanInput, readHumanInput } from '../src/scl-human-decision.js';
import { buildRuntimeUsageBinding, runtimeAuthorityRef, type RuntimeUsageBinding } from '../src/runtime-usage-binding.js';
import type { ConversationTurnEffect, SessionEndReason, StreamJsonMessage } from '../src/types.js';
import { resolveToolFileOutboxPath } from '../src/tool-file-outbox.js';

interface FakeProcess extends EventEmitter {
  stdin: PassThrough;
  stdout: PassThrough;
  stderr: PassThrough;
  pid: number;
  kill: ReturnType<typeof vi.fn>;
}

type RpcRecord = Record<string, unknown> & { id?: string; type?: string };

const roots: string[] = [];
afterEach(() => {
  vi.restoreAllMocks();
  vi.unstubAllEnvs();
  for (const root of roots.splice(0)) rmSync(root, { recursive: true, force: true });
});

function fixture(): {
  root: string;
  workspace: string;
  sessionRoot: string;
  launcherPath: string;
  extensionPath: string;
  skillPath: string;
  contextPath: string;
  agentConfigDir: string;
  manifest: PiRuntimeManifest;
  config: ConversationSpawnConfig;
} {
  const root = mkdtempSync(join(tmpdir(), 'relay-pi-runtime-'));
  roots.push(root);
  const workspace = join(root, 'workspace');
  const sessionRoot = join(root, 'sessions');
  const launcherPath = join(root, 'pi-isolation-launcher');
  const extensionPath = join(workspace, 'reviewed-extension.ts');
  const skillPath = join(workspace, 'reviewed-skill.md');
  const contextPath = join(root, 'reviewed-context.md');
  const agentConfigDir = join(root, 'reviewed-agent-config');
  const hostPiPath = join(root, 'pi');
  mkdirSync(workspace);
  mkdirSync(sessionRoot);
  mkdirSync(agentConfigDir, { mode: 0o700 });
  writeFileSync(launcherPath, '#!/bin/sh\nexit 1\n');
  chmodSync(launcherPath, 0o755);
  writeFileSync(extensionPath, 'export default {};\n');
  writeFileSync(skillPath, '# reviewed skill\n');
  writeFileSync(contextPath, 'reviewed context\n');
  writeFileSync(join(agentConfigDir, 'auth.json'), '{"openai-codex":{"type":"oauth"}}\n', { mode: 0o600 });
  writeFileSync(hostPiPath, `#!/bin/sh\nprintf 'pi ${PI_RUNTIME_VERSION}\\n'\n`);
  chmodSync(hostPiPath, 0o755);
  const manifest: PiRuntimeManifest = {
    schemaVersion: PI_RUNTIME_MANIFEST_SCHEMA,
    piVersion: PI_RUNTIME_VERSION,
    piCommit: PI_RUNTIME_COMMIT,
    profiles: {
      canary: {
        kind: 'container',
        unattended: true,
        launcher: { command: launcherPath, args: ['--profile', 'canary'] },
        piBinary: '/opt/pi/bin/pi',
        workspaceRoots: [workspace],
        sessionRoot,
        envAllowlist: ['SAFE_TOKEN'],
        extensions: [],
        computeCostHost: {
          extension: extensionPath,
          python: hostPiPath,
          pythonPath: workspace,
          sclExecutable: launcherPath,
          sclHarnessRoot: workspace,
          sclKernelRoot: workspace,
        },
        skills: [skillPath],
        contextFiles: [contextPath],
      },
    },
  };
  const config: ConversationSpawnConfig = {
    tenantId: 'internal:pi-canary',
    workspace,
    sessionId: 'pi-session-1',
    allowedTools: ['read', 'bash', 'read'],
    conversationRuntime: {
      runtime: 'pi',
      model: { provider: 'anthropic', model: 'claude-sonnet-4-5', reasoning: 'high' },
      isolationProfile: 'canary',
      runtimeOptions: { pi: { idleEviction: true } },
    },
    computeCostBinding: buildRuntimeUsageBinding({
      workItemId: 'wi_20260822_730', agentId: 'pulse', phase: 'work_item', epoch: 12,
      runtime: 'pi', procurementSource: 'chatgpt_subscription',
      authorityRef: runtimeAuthorityRef('pi-session-1'), executionSurface: 'surface_v1_local_macos',
      issuedAt: '2026-08-25T01:00:00Z',
    }),
  };
  return { root, workspace, sessionRoot, launcherPath, extensionPath, skillPath, contextPath, agentConfigDir, manifest, config };
}

function emitRecord(proc: FakeProcess, record: unknown): void {
  proc.stdout.write(`${JSON.stringify(record)}\n`);
}

function reviewedContext(path: string, index = 1): string {
  const real = realpathSync(path);
  return `[Relay reviewed context ${index} begins: ${JSON.stringify(real)}]\nreviewed context\n[Relay reviewed context ${index} ends]\n`;
}

function canonicalUsageJson(value: unknown): string {
  if (Array.isArray(value)) return `[${value.map(canonicalUsageJson).join(',')}]`;
  if (value && typeof value === 'object') {
    const record = value as Record<string, unknown>;
    return `{${Object.keys(record).sort().map(key => `${JSON.stringify(key)}:${canonicalUsageJson(record[key])}`).join(',')}}`;
  }
  return JSON.stringify(value);
}

function boundedUsageSnapshot(binding: RuntimeUsageBinding, lifecycle: 'open'|'sealed') {
  const baseline = { source_bytes: 0, source_prefix_sha256: createHash('sha256').update('').digest('hex'), last_entry_ref: null };
  const final = lifecycle === 'open' ? baseline : {
    source_bytes: 1, source_prefix_sha256: 'b'.repeat(64), last_entry_ref: `pi_entry_v1_${'c'.repeat(64)}`,
  };
  const zero = lifecycle === 'open';
  const baselineSnapshot = zero ? undefined : boundedUsageSnapshot(binding, 'open');
  const totals = {
    input: zero ? 0 : 1, cache_read: 0, cache_write: 0,
    output: 0, reasoning: 0, total_tokens: zero ? 0 : 1,
  };
  const unsigned = {
    schema: 'pi_session_usage_snapshot/v2', generated_at: '2026-08-25T01:00:01Z',
    lifecycle_state: lifecycle, measurement_role: zero ? 'baseline' : 'final',
    work_item_id: binding.work_item_id, agent_id: binding.agent_id,
    execution_surface: binding.execution_surface, phase: binding.phase,
    epoch: binding.epoch, runtime: 'pi', procurement_source: binding.procurement_source,
    authority_ref: binding.authority_ref, runtime_binding_sha256: binding.binding_sha256,
    attribution_boundary: {
      policy: 'baseline_exclusive_final_inclusive',
      baseline_snapshot_sha256: zero ? null : baselineSnapshot!.snapshotSha,
      baseline, final, excluded_pre_baseline_usage_events: 0, deduplicated_entries: 0,
    },
    summary: {
      schema: 'pi_native_usage_summary/v1', event_count: zero ? 0 : 1, totals,
      by_provider_model_effort: {}, request_partitions: zero ? [] : [{}],
      pricing_contract: 'tokens_only_openrouter_authority_required',
      last_entry_ref: final.last_entry_ref,
      source_prefix_sha256: final.source_prefix_sha256,
      source_bytes: final.source_bytes,
      source_entry_count: zero ? 0 : 1,
      excluded_usage_event_count: 0,
      deduplicated_entry_count: 0,
      privacy: 'raw_session_entry_response_content_and_path_omitted',
    },
    coverage: zero ? 'exact_zero_baseline' : 'exact_bounded_sealed',
  };
  return { unsigned, snapshotSha: createHash('sha256').update(canonicalUsageJson(unsigned)).digest('hex') };
}

function fakeProcess(
  sessionId: string,
  commands: RpcRecord[],
  getUsage: () => { input: number; output: number; cacheRead: number; cacheWrite: number },
  stateOverrides: Record<string, unknown> = {},
  onCommand?: (command: RpcRecord, proc: FakeProcess) => boolean | void,
): FakeProcess {
  const proc = new EventEmitter() as FakeProcess;
  proc.stdin = new PassThrough();
  proc.stdout = new PassThrough();
  proc.stderr = new PassThrough();
  proc.pid = 938;
  proc.kill = vi.fn((signal?: string) => {
    queueMicrotask(() => proc.emit('exit', signal === 'SIGKILL' ? 137 : 0, signal ?? 'SIGTERM'));
    return true;
  });
  const configuredModel = stateOverrides.model as { provider?: string; id?: string } | undefined;
  let nativeProvider = configuredModel?.provider ?? 'anthropic';
  let nativeModel = configuredModel?.id ?? 'claude-sonnet-4-5';
  let nativeThinking = typeof stateOverrides.thinkingLevel === 'string' ? stateOverrides.thinkingLevel : 'high';
  let input = '';
  proc.stdin.on('data', (chunk: Buffer) => {
    input += chunk.toString('utf8');
    while (input.includes('\n')) {
      const newline = input.indexOf('\n');
      const line = input.slice(0, newline);
      input = input.slice(newline + 1);
      if (!line) continue;
      const command = JSON.parse(line) as RpcRecord;
      commands.push(command);
      const shouldRespond = onCommand?.(command, proc);
      if (command.type === 'set_model') {
        nativeProvider = String(command.provider);
        nativeModel = String(command.modelId);
      } else if (command.type === 'set_thinking_level') {
        nativeThinking = String(command.level);
      }
      if (shouldRespond === false) continue;
      queueMicrotask(() => {
        const data = command.type === 'get_state'
          ? {
              sessionId,
              ...stateOverrides,
              model: { provider: nativeProvider, id: nativeModel },
              thinkingLevel: nativeThinking,
            }
          : command.type === 'get_available_models'
          ? {
              models: [
                { provider: 'anthropic', id: 'claude-sonnet-4-5', name: 'Claude Sonnet 4.5' },
                { provider: 'openrouter', id: 'anthropic/claude-opus-4.1', name: 'Claude Opus 4.1' },
              ],
            }
          : command.type === 'get_available_thinking_levels'
          ? { levels: ['off', 'low', 'high'] }
          : command.type === 'get_session_stats'
          ? {
              sessionId,
              totalMessages: 8,
              tokens: { ...getUsage(), total: 42 },
              contextUsage: { tokens: 21, contextWindow: 200, percent: 10.5 },
            }
          : command.type === 'get_commands'
          ? { commands: [
              { name: 'runtime-usage-baseline', source: 'extension' },
              { name: 'runtime-usage-seal', source: 'extension' },
            ] }
          : command.type === 'compact'
          ? { summary: 'compacted', tokensBefore: 200, estimatedTokensAfter: 40 }
          : command.type === 'switch_session'
          ? { cancelled: false }
          : {};
        emitRecord(proc, {
          type: 'response',
          id: command.id,
          command: command.type,
          success: true,
          data,
        });
      });
    }
  });
  return proc;
}

async function tick(): Promise<void> {
  await new Promise((resolve) => setImmediate(resolve));
}

describe('Pi isolation command contract', () => {
  it('uses the reviewed Pi manifest, not a Relay-compiled version, for the isolated launcher', () => {
    const f = fixture();
    const spec = buildPiCommand(f.config, f.manifest, {
      SAFE_TOKEN: 'allowed',
      SECRET_TOKEN: 'must-not-cross',
      PATH: '/host/path',
    });

    expect(spec.command).toBe(f.launcherPath);
    expect(spec.args).toEqual(expect.arrayContaining([
      '--isolation', 'container',
      '--expected-pi-version', PI_RUNTIME_VERSION,
      '--expected-pi-commit', PI_RUNTIME_COMMIT,
      '--mode', 'rpc',
      '--session-id', 'pi-session-1',
      '--provider', 'anthropic',
      '--model', 'claude-sonnet-4-5',
      '--thinking', 'high',
      '--no-extensions', '--no-skills', '--no-context-files', '--no-approve',
      '--tools', 'read,bash',
      '--extension', realpathSync(f.extensionPath),
      '--skill', realpathSync(f.skillPath),
      '--append-system-prompt', reviewedContext(f.contextPath),
    ]));
    expect(spec.env).toMatchObject({
      SAFE_TOKEN: 'allowed',PULSE_PI_TENANT_ID:'internal:pi-canary',
      SCL_SESSION_REF:relayHostSessionRef(f.config.sessionId),
    });
    expect(JSON.parse(spec.env.PULSE_RUNTIME_USAGE_BINDING ?? '{}')).toEqual(f.config.computeCostBinding);
    expect(spec.env.PULSE_RUNTIME_AUTHORITY_REF).toBe(f.config.computeCostBinding?.authority_ref);
    expect(spec.env.PULSE_RUNTIME_EXECUTION_SURFACE).toBe('surface_v1_local_macos');
    expect(spec.env.PULSE_RUNTIME_USAGE_BINDING).not.toContain('session_id');
    expect(spec.env.PULSE_COMPUTE_COST_PYTHON).toBe(realpathSync(join(f.root, 'pi')));
    expect(spec.env.PULSE_COMPUTE_COST_PYTHONPATH).toBe(realpathSync(f.workspace));
    expect(spec.env.PULSE_COMPUTE_COST_PI_SNAPSHOT_DIR).toBe(join(spec.sessionDir, 'compute-cost'));
    expect(spec.env.PULSE_COMPUTE_COST_SCL_BIN).toBe(realpathSync(f.launcherPath));
    expect(spec.env.PULSE_COMPUTE_COST_SCL_HARNESS_ROOT).toBe(realpathSync(f.workspace));
    expect(spec.env.PULSE_COMPUTE_COST_SCL_KERNEL_ROOT).toBe(realpathSync(f.workspace));
    expect(spec.env.SCL_SESSION_REF).toBe(relayHostSessionRef(f.config.sessionId));
    expect(spec.env).not.toHaveProperty('SECRET_TOKEN');
    expect(spec.env).toMatchObject({
      PATH: '/usr/local/bin:/usr/bin:/bin',
      HOME: expect.stringContaining('/agent'),
    });
    expect(spec.sessionDir).toMatch(/internal_pi-canary-[a-f0-9]{12}$/);
  });

  it('accepts a different reviewed Pi release without rebuilding Relay', () => {
    const f = fixture();
    f.manifest.piVersion = '0.88.0';
    f.manifest.piCommit = 'a'.repeat(40);
    const spec = buildPiCommand(f.config, f.manifest);
    expect(spec.args).toEqual(expect.arrayContaining([
      '--expected-pi-version', '0.88.0', '--expected-pi-commit', 'a'.repeat(40),
    ]));
    expect(spec.args).not.toContain(PI_RUNTIME_VERSION);
  });

  it('rejects malformed Pi release identity before spawning', () => {
    const f = fixture();
    f.manifest.piVersion = '0.87.1;unsafe';
    expect(() => buildPiCommand(f.config, f.manifest)).toThrow('Pi runtime manifest version or commit is invalid');
    f.manifest.piVersion = PI_RUNTIME_VERSION;
    f.manifest.piCommit = 'not-a-git-commit';
    expect(() => buildPiCommand(f.config, f.manifest)).toThrow('Pi runtime manifest version or commit is invalid');
  });

  it('does not load the ComputeCost extension for an ordinary Pi session', () => {
    const f = fixture();
    f.config.computeCostBinding = undefined;

    const spec = buildPiCommand(f.config, f.manifest);
    const extensions = spec.args.filter((_arg, index) => spec.args[index - 1] === '--extension');

    expect(extensions).toEqual([]);
    expect(spec.env).not.toHaveProperty('PULSE_RUNTIME_USAGE_BINDING');
    expect(spec.env).not.toHaveProperty('PULSE_RUNTIME_AUTHORITY_REF');
    expect(spec.env).not.toHaveProperty('PULSE_COMPUTE_COST_PYTHON');
    expect(spec.env).not.toHaveProperty('PULSE_COMPUTE_COST_SCL_BIN');
  });

  it('names the shared repositories and SSOT read-only when the workspace sits in a Coevolution root', () => {
    const f = fixture();
    f.config.computeCostBinding = undefined;
    const line = `规则正本：${join(f.root, 'ssot')}/（共享 SSOT，修改走自己分支的 PR）`;
    expect(buildPiCommand(f.config, f.manifest).args).not.toContain(line);

    mkdirSync(join(f.root, 'ssot'));
    mkdirSync(join(f.root, 'Bigbird', 'PULSE'), { recursive: true });
    const args = buildPiCommand(f.config, f.manifest).args;
    expect(args[args.indexOf(line) - 1]).toBe('--append-system-prompt');
    const repos = args.find((arg) => arg.startsWith('共享仓库（只读）：'));
    expect(repos).toContain(`${f.root}/、${join(f.root, 'Bigbird', 'PULSE')}/（本机共享检出；`);
    expect(args[args.indexOf(repos!) - 1]).toBe('--append-system-prompt');
    const excluded = args.find((arg) => arg.startsWith('共享仓库内不得读取（本会话工作区与授权路径不在此限）：'));
    expect(excluded).toContain('凭据与令牌文件（.env*');
    expect(args[args.indexOf(excluded!) - 1]).toBe('--append-system-prompt');
  });

  it('rejects a manifest that also configures the ComputeCost extension as always-on', () => {
    const f = fixture();
    f.manifest.profiles.canary!.extensions = [f.extensionPath];

    expect(() => buildPiCommand(f.config, f.manifest)).toThrow(
      'Pi ComputeCost extension must not be configured as an always-on extension',
    );
  });

  it('rejects a WI-bound Pi spawn when native session authority differs from the signed epoch binding', () => {
    const f = fixture();
    f.config.sessionId = 'replacement-native-session';
    expect(() => buildPiCommand(f.config, f.manifest)).toThrow(/native session\/surface authority/);
  });

  it('rejects a WI-bound Pi spawn without an explicit post-final ComputeCost host hook', () => {
    const f = fixture();
    delete f.manifest.profiles.canary!.computeCostHost;
    expect(() => buildPiCommand(f.config, f.manifest)).toThrow(/ComputeCost host configuration/);
  });

  it('preflights configured WI-bound Pi profiles before Relay readiness', () => {
    const f = fixture();
    expect(validateConfiguredPiProfiles(f.manifest,['canary'],{requireComputeCostHost:true}))
      .toBe(f.manifest);
    delete f.manifest.profiles.canary!.computeCostHost;
    expect(()=>validateConfiguredPiProfiles(f.manifest,['canary'],{requireComputeCostHost:true}))
      .toThrow(/ComputeCost host configuration/);
  });

  it.each(['skill', 'extension'] as const)('checks reviewed host %s paths before readiness and again at launch', (kind) => {
    const f = fixture();
    const outside = mkdtempSync(join(tmpdir(), 'unreviewed-pi-resource-'));
    roots.push(outside);
    const external = join(outside, kind);
    writeFileSync(external, 'unreviewed resource');
    const configured = join(f.workspace, `configured-${kind}`);
    writeFileSync(configured, 'reviewed resource');
    const profile = f.manifest.profiles.hostNative = {
      ...f.manifest.profiles.canary!, kind: 'host-native' as const, launcher: undefined,
      piBinary: join(f.root, 'pi'), toolchainPaths: [f.root], nodeResourceRoots: [f.root],
      computeCostHost: undefined, extensions: [], skills: [],
      [kind === 'skill' ? 'skills' : 'extensions']: [configured],
    };
    f.config.conversationRuntime!.isolationProfile = 'hostNative';
    f.config.computeCostBinding = undefined;
    const preflight = () => validateConfiguredPiProfiles(f.manifest, ['hostNative'], { requireComputeCostHost: false });
    expect(preflight()).toBe(f.manifest);
    expect(buildPiCommand(f.config, f.manifest).args).toContain(realpathSync(configured));

    // An approved filename must not hide a symlink moved outside the allowlist.
    unlinkSync(configured);
    symlinkSync(external, configured);
    const outsideError = `Pi reviewed ${kind} is outside the node resource allowlist`;
    expect(preflight).toThrow(outsideError);
    expect(() => buildPiCommand(f.config, f.manifest)).toThrow(outsideError);
    profile[kind === 'skill' ? 'skills' : 'extensions'] = [external];
    expect(preflight).toThrow(outsideError);
    expect(() => buildPiCommand(f.config, f.manifest)).toThrow(outsideError);
    unlinkSync(external);
    expect(preflight).toThrow(`Pi ${kind} must be an existing absolute path`);
    expect(() => buildPiCommand(f.config, f.manifest)).toThrow(`Pi ${kind} must be an existing absolute path`);
  });

  it('loads hierarchical workspace context without leaking sibling instructions', () => {
    const f = fixture();
    const sibling = join(f.root, 'sibling');
    mkdirSync(sibling);
    writeFileSync(join(f.root, 'CLAUDE.md'), 'root instructions\n');
    writeFileSync(join(f.workspace, 'CLAUDE.md'), 'workspace instructions\n');
    writeFileSync(join(sibling, 'CLAUDE.md'), 'sibling secret\n');
    f.manifest.profiles.canary!.workspaceRoots = [f.root];
    f.manifest.profiles.canary!.contextFiles = [];
    f.manifest.profiles.canary!.workspaceContextFileNames = ['CLAUDE.md'];

    const spec = buildPiCommand(f.config, f.manifest);
    const prompts = spec.args.filter((_arg, index) => spec.args[index - 1] === '--append-system-prompt');

    expect(prompts).toHaveLength(3);
    expect(prompts[2]).toBe(PUBLIC_PROGRESS_POLICY);
    expect(prompts[0]).toContain('root instructions');
    expect(prompts[1]).toContain('workspace instructions');
    expect(prompts.join('\n')).not.toContain('sibling secret');
  });

  it('rejects unsafe workspace context filenames', () => {
    const f = fixture();
    f.manifest.profiles.canary!.workspaceRoots = [f.root];
    f.manifest.profiles.canary!.workspaceContextFileNames = ['../CLAUDE.md'];
    expect(() => buildPiCommand(f.config, f.manifest)).toThrow('must be a plain filename');
  });

  it('keeps external isolation profiles from executing Pi directly, and rejects workspace escape and unreviewed files', () => {
    const f = fixture();
    f.manifest.profiles.canary.piBinary = f.launcherPath;
    expect(() => buildPiCommand(f.config, f.manifest)).toThrow('may not execute the Pi binary directly');

    f.manifest.profiles.canary.piBinary = '/opt/pi/bin/pi';
    f.config.workspace = tmpdir();
    expect(() => buildPiCommand(f.config, f.manifest)).toThrow('outside the isolation profile allowlist');

    f.config.workspace = f.workspace;
    f.manifest.profiles.canary.extensions = [f.contextPath];
    expect(() => buildPiCommand(f.config, f.manifest)).toThrow('reviewed extensions must live inside the tenant workspace');

    f.manifest.profiles.canary.extensions = [join(f.root, 'not-reviewed-and-missing.ts')];
    expect(() => buildPiCommand(f.config, f.manifest)).toThrow('must be an existing absolute path');
  });

  it('runs a pinned host-native Pi with the reviewed workspace contract and an explicit toolchain', () => {
    const f = fixture();
    f.config.computeCostBinding = undefined;
    f.manifest.profiles.hostNative = {
      kind: 'host-native',
      unattended: true,
      piBinary: join(f.root, 'pi'),
      toolchainPaths: [f.root, '/usr/bin', '/bin'],
      workspaceRoots: [f.workspace],
      nodeResourceRoots: [f.root, '/usr/bin', '/bin'],
      sessionRoot: f.sessionRoot,
      agentConfigDir: f.agentConfigDir,
      envAllowlist: ['SAFE_TOKEN'],
      extensions: [f.extensionPath],
      skills: [f.skillPath],
      contextFiles: [f.contextPath],
    };
    f.config.conversationRuntime!.isolationProfile = 'hostNative';

    const spec = buildPiCommand(f.config, f.manifest, { SAFE_TOKEN: 'allowed', SECRET_TOKEN: 'must-not-cross' });

    expect(spec.command).toBe(realpathSync(join(f.root, 'pi')));
    expect(spec.args).toEqual(expect.arrayContaining([
      '--mode', 'rpc',
      '--session-dir', expect.stringContaining('internal_pi-canary-'),
      '--no-extensions',
      '--no-skills',
      '--no-prompt-templates',
      '--no-themes',
      '--no-context-files',
      '--extension', realpathSync(f.extensionPath),
      '--skill', realpathSync(f.skillPath),
      '--append-system-prompt', reviewedContext(f.contextPath),
    ]));
    expect(spec.env).toMatchObject({
      PATH: [f.root, '/usr/bin', '/bin'].map((path) => realpathSync(path)).join(':'),
      SAFE_TOKEN: 'allowed',
      PULSE_PI_TENANT_ID: 'internal:pi-canary',
    });
    expect(spec.env).not.toHaveProperty('SECRET_TOKEN');
    expect(spec.env['HOME']).toBe(join(spec.sessionDir, 'agent'));
    expect(spec.env['PI_CODING_AGENT_DIR']).toBe(realpathSync(f.agentConfigDir));
  });

  it.each([false,true,'legacy','foreign-surface'])('admits the exact registered host workspace (outside allowlist=%s)', (outside) => {
    const f = fixture();
    const nodeRoot = join(f.root, 'node-runtime');
    const pythonPath = join(nodeRoot, 'compute-cost');
    const extensionPath = join(nodeRoot, 'pi-usage-binding.mjs');
    const skillPath = join(nodeRoot, 'reviewed-skill.md');
    const piBinary = join(nodeRoot, 'pi');
    const sclBinary = join(nodeRoot, 'scl');
    mkdirSync(pythonPath, { recursive: true });
    writeFileSync(sclBinary, `#!${process.execPath}\nconst a=process.argv.slice(2); const v=k=>a[a.indexOf(k)+1];
if(a[0]==='handshake'){console.log(JSON.stringify({schema_version:'scl_response/v1',status:'read_only',result:{workspace_policy:'managed_workspaces/v1'}}));process.exit(0);}
if(a[0]!=='admit-runtime-workspace-local')process.exit(2);
console.log(JSON.stringify({schema_version:'scl_response/v1',status:'read_only',result:{workspace_admission:{
schema_version:'scl_workspace_admission/v1',policy:'managed_workspaces/v1',scope:'process_start_only',execution_authority:false,admitted:true,work_item_id:a[1],agent_id:v('--agent'),
surface_id:${JSON.stringify(outside === 'foreign-surface' ? 'surface_v1_wrong' : 'surface_v1_local_macos')},session_ref:v('--session-ref'),epoch:Number(v('--epoch')),requested_workspace:v('--workspace'),workspace:${JSON.stringify(outside === 'legacy' ? realpathSync(f.workspace) : null)} ?? v('--workspace'),registration_sha256:'b'.repeat(64)}}}));\n`);
    chmodSync(sclBinary, 0o755);
    writeFileSync(extensionPath, 'export default {};\n');
    writeFileSync(skillPath, '# reviewed node skill\n');
    writeFileSync(piBinary, `#!/bin/sh\nprintf 'pi ${PI_RUNTIME_VERSION}\\n'\n`);
    chmodSync(piBinary, 0o755);
    f.manifest.profiles.hostNative = {
      kind: 'host-native',
      unattended: true,
      piBinary,
      toolchainPaths: [nodeRoot, '/usr/bin', '/bin'],
      workspaceRoots: [outside?nodeRoot:f.workspace],
      nodeResourceRoots: [nodeRoot, '/usr/bin', '/bin'],
      sessionRoot: f.sessionRoot,
      envAllowlist: [],
      extensions: [],
      computeCostHost: {
        extension: extensionPath,
        python: piBinary,
        pythonPath,
        sclExecutable: sclBinary,
        sclHarnessRoot: nodeRoot,
        sclKernelRoot: nodeRoot,
      },
      skills: [skillPath],
      contextFiles: [],
    };
    f.config.conversationRuntime!.isolationProfile = 'hostNative';

    if(outside==='legacy'){
      const surface=join(f.root,'legacy-surface');mkdirSync(surface);f.config.workspace=surface;
    }
    expect(()=>validateConfiguredPiProfiles(f.manifest,['hostNative'],{requireComputeCostHost:true})).not.toThrow();
    f.manifest.profiles.hostNative.envAllowlist = ['HARNESS_AGENT_ID', 'HARNESS_WORK_ITEM_ID', 'BROKER_TX_REF', 'HARNESS_BRANCH_REF'];
    if(outside==='foreign-surface'){
      expect(()=>buildPiCommand(f.config,f.manifest,{PULSE_SCL_SURFACE_ID:'surface_v1_shared_default'}))
        .toThrow('exact runtime authority');
      return;
    }
    const spec = buildPiCommand(f.config, f.manifest, {
      PULSE_SCL_SURFACE_ID:'surface_v1_shared_default',
      HARNESS_AGENT_ID: 'other-agent', HARNESS_WORK_ITEM_ID: 'wi_20990101_001',
      HARNESS_BRANCH_REF: 'refs/heads/other', BROKER_TX_REF: 'stale-tx',
      HARNESS_WORKTREE_STATE_ROOT: nodeRoot,
    });

    expect(spec.cwd).toBe(realpathSync(f.workspace));
    expect(spec.args).toContainEqual(expect.stringContaining('harness worktree allocation'));
    expect(spec.args).toEqual(expect.arrayContaining([
      '--extension', realpathSync(extensionPath),
      '--skill', realpathSync(skillPath),
    ]));
    expect(spec.env['PULSE_COMPUTE_COST_PYTHONPATH']).toBe(realpathSync(pythonPath));
    expect(spec.env['HARNESS_AGENT_ID']).toBe(f.config.computeCostBinding!.agent_id);
    expect(spec.env['HARNESS_WORK_ITEM_ID']).toBe(f.config.computeCostBinding!.work_item_id);
    expect(spec.env['HARNESS_ROOT']).toBe(realpathSync(nodeRoot));
    expect(spec.env['SCL_KERNEL_ROOT']).toBe(realpathSync(nodeRoot));
    expect(spec.env['HARNESS_BRANCH_REF']).toBeUndefined();
    expect(spec.env['BROKER_TX_REF']).toBeUndefined();
    expect(spec.env['HARNESS_WORKTREE_STATE_ROOT']).toBe(realpathSync(nodeRoot));
    // A real ordinary child inherits the same launch environment, not a test-only
    // export added after the runtime has started. No provider or model is called.
    const childContext = JSON.parse(execFileSync(process.execPath, ['-e',
      'console.log(JSON.stringify({agent:process.env.HARNESS_AGENT_ID,wi:process.env.HARNESS_WORK_ITEM_ID,session:process.env.SCL_SESSION_REF,root:process.env.HARNESS_WORKTREE_STATE_ROOT,branch:process.env.HARNESS_BRANCH_REF??null,tx:process.env.BROKER_TX_REF??null}))'],
      {cwd:spec.cwd,env:spec.env,encoding:'utf8'}));
    expect(childContext).toEqual({agent:f.config.computeCostBinding!.agent_id,
      wi:f.config.computeCostBinding!.work_item_id, session:runtimeAuthorityRef(f.config.sessionId),
      root:realpathSync(nodeRoot),branch:null,tx:null});
    expect(()=>buildPiCommand(f.config, f.manifest, {HARNESS_WORKTREE_STATE_ROOT: tmpdir()}))
      .toThrow('outside reviewed Host resources');
  });

  it('never loads the retired WI continuation tool or its SCL recovery resources', () => {
    const f = fixture();
    f.config.computeCostBinding = undefined;
    f.config.allowedTools = ['Read', 'Bash'];
    f.manifest.profiles.hostNative = {
      kind: 'host-native',
      unattended: true,
      piBinary: join(f.root, 'pi'),
      toolchainPaths: [f.root, '/usr/bin', '/bin'],
      workspaceRoots: [f.workspace],
      nodeResourceRoots: [f.root, '/usr/bin', '/bin'],
      sessionRoot: f.sessionRoot,
      envAllowlist: [],
      extensions: [],
      skills: [],
      contextFiles: [],
      computeCostHost: f.manifest.profiles.canary!.computeCostHost!,
    };
    f.config.conversationRuntime!.isolationProfile = 'hostNative';

    const spec = buildPiCommand(f.config, f.manifest, {
      PULSE_WI_EXECUTION_CONTROL_ENABLED: '1', PULSE_SCL_SURFACE_ID: 'surface_v1_local_macos',
    });
    expect(spec.args.filter((_arg, index) => spec.args[index - 1] === '--extension')).toEqual([]);
    expect(spec.args).toEqual(expect.arrayContaining(['--tools', 'read,bash']));
    expect(spec.args.join(' ')).not.toContain('pulse_wi_continue');
    for (const key of ['PULSE_SCL_BIN', 'HARNESS_ROOT', 'SCL_KERNEL_ROOT', 'SCL_RELAY_HOST_CONTEXT', 'HARNESS_SURFACE_ID']) {
      expect(spec.env[key]).toBeUndefined();
    }
  });

  it('fails closed when a host-native reviewed resource escapes its node resource roots', () => {
    const f = fixture();
    const nodeRoot = join(f.root, 'node-runtime');
    mkdirSync(nodeRoot);
    f.config.computeCostBinding = undefined;
    writeFileSync(join(nodeRoot, 'pi'), `#!/bin/sh\nprintf 'pi ${PI_RUNTIME_VERSION}\\n'\n`);
    chmodSync(join(nodeRoot, 'pi'), 0o755);
    f.manifest.profiles.hostNative = {
      kind: 'host-native',
      unattended: true,
      piBinary: join(nodeRoot, 'pi'),
      toolchainPaths: [nodeRoot, '/usr/bin', '/bin'],
      workspaceRoots: [f.workspace],
      nodeResourceRoots: [nodeRoot, '/usr/bin', '/bin'],
      sessionRoot: f.sessionRoot,
      envAllowlist: [],
      extensions: [f.contextPath],
      skills: [],
      contextFiles: [],
    };
    f.config.conversationRuntime!.isolationProfile = 'hostNative';

    expect(() => buildPiCommand(f.config, f.manifest)).toThrow('outside the node resource allowlist');
  });

  it('resolves node resources before containment and rejects symlink escape', () => {
    const f = fixture();
    const nodeRoot = join(f.root, 'node-runtime');
    const escapedExtension = join(f.root, 'escaped-extension.mjs');
    const linkedExtension = join(nodeRoot, 'linked-extension.mjs');
    mkdirSync(nodeRoot);
    writeFileSync(escapedExtension, 'export default {};\n');
    symlinkSync(escapedExtension, linkedExtension);
    writeFileSync(join(nodeRoot, 'pi'), `#!/bin/sh\nprintf 'pi ${PI_RUNTIME_VERSION}\\n'\n`);
    chmodSync(join(nodeRoot, 'pi'), 0o755);
    f.config.computeCostBinding = undefined;
    f.manifest.profiles.hostNative = {
      kind: 'host-native',
      unattended: true,
      piBinary: join(nodeRoot, 'pi'),
      toolchainPaths: [nodeRoot, '/usr/bin', '/bin'],
      workspaceRoots: [f.workspace],
      nodeResourceRoots: [nodeRoot, '/usr/bin', '/bin'],
      sessionRoot: f.sessionRoot,
      envAllowlist: [],
      extensions: [linkedExtension],
      skills: [],
      contextFiles: [],
    };
    f.config.conversationRuntime!.isolationProfile = 'hostNative';

    expect(() => buildPiCommand(f.config, f.manifest)).toThrow('outside the node resource allowlist');
  });

  it('rejects a filesystem-wide node resource root and external-profile node roots', () => {
    const f = fixture();
    f.config.computeCostBinding = undefined;
    f.manifest.profiles.hostNative = {
      kind: 'host-native',
      unattended: true,
      piBinary: join(f.root, 'pi'),
      toolchainPaths: [f.root],
      workspaceRoots: [f.workspace],
      nodeResourceRoots: [realpathSync('/')],
      sessionRoot: f.sessionRoot,
      envAllowlist: [],
      extensions: [],
      skills: [],
      contextFiles: [],
    };
    f.config.conversationRuntime!.isolationProfile = 'hostNative';
    expect(() => buildPiCommand(f.config, f.manifest)).toThrow('may not be a filesystem root');

    f.config.conversationRuntime!.isolationProfile = 'canary';
    f.manifest.profiles.canary!.nodeResourceRoots = [f.root];
    expect(() => buildPiCommand(f.config, f.manifest)).toThrow('only supported for host-native');
  });

  it('shares one reviewed Pi agent config without sharing tenant HOME and fails closed on unsafe permissions', () => {
    const f = fixture();
    f.config.computeCostBinding = undefined;
    f.manifest.profiles.hostNative = {
      kind: 'host-native',
      unattended: true,
      piBinary: join(f.root, 'pi'),
      toolchainPaths: [f.root, '/usr/bin', '/bin'],
      workspaceRoots: [f.workspace],
      nodeResourceRoots: [f.root, '/usr/bin', '/bin'],
      sessionRoot: f.sessionRoot,
      agentConfigDir: f.agentConfigDir,
      envAllowlist: [],
      extensions: [],
      skills: [],
      contextFiles: [],
    };
    f.config.conversationRuntime!.isolationProfile = 'hostNative';

    const first = buildPiCommand(f.config, f.manifest);
    const second = buildPiCommand({ ...f.config, tenantId: 'internal:pi-second' }, f.manifest);
    expect(first.env['HOME']).not.toBe(second.env['HOME']);
    expect(first.env['PI_CODING_AGENT_DIR']).toBe(realpathSync(f.agentConfigDir));
    expect(second.env['PI_CODING_AGENT_DIR']).toBe(realpathSync(f.agentConfigDir));

    chmodSync(join(f.agentConfigDir, 'auth.json'), 0o644);
    expect(() => buildPiCommand({ ...f.config, tenantId: 'internal:pi-permissions' }, f.manifest)).toThrow('must not be accessible by group or other');
  });

  it('takes a host-native workspace from task context without requiring a prebuilt worktree', () => {
    const f = fixture();
    f.config.computeCostBinding = undefined;
    const taskWorkspace = join(f.root, 'agent-selected-checkout');
    mkdirSync(taskWorkspace);
    f.manifest.profiles.hostNative = {
      kind: 'host-native',
      unattended: true,
      piBinary: join(f.root, 'pi'),
      toolchainPaths: [f.root, '/usr/bin', '/bin'],
      workspaceRoots: [f.root],
      nodeResourceRoots: [f.root, '/usr/bin', '/bin'],
      sessionRoot: f.sessionRoot,
      envAllowlist: [],
      extensions: [],
      skills: [],
      contextFiles: [],
    };
    f.config.workspace = taskWorkspace;
    f.config.conversationRuntime!.isolationProfile = 'hostNative';

    const spec = buildPiCommand(f.config, f.manifest);

    expect(spec.cwd).toBe(realpathSync(taskWorkspace));
    expect(spec.command).toBe(realpathSync(join(f.root, 'pi')));
    expect(spec.args).toEqual(expect.arrayContaining([
      '--no-extensions',
      '--no-skills',
      '--no-prompt-templates',
      '--no-themes',
      '--no-context-files',
    ]));
  });

  it('allows a bounded cold-start window when verifying the pinned host Pi binary', () => {
    const f = fixture();
    f.config.computeCostBinding = undefined;
    writeFileSync(join(f.root, 'pi'), `#!/bin/sh\nsleep 6\nprintf 'pi ${PI_RUNTIME_VERSION}\\n'\n`);
    f.manifest.profiles.hostNative = {
      kind: 'host-native',
      unattended: true,
      piBinary: join(f.root, 'pi'),
      toolchainPaths: [f.root, '/usr/bin', '/bin'],
      workspaceRoots: [f.workspace],
      nodeResourceRoots: [f.root, '/usr/bin', '/bin'],
      sessionRoot: f.sessionRoot,
      envAllowlist: [],
      extensions: [],
      skills: [],
      contextFiles: [],
    };
    f.config.conversationRuntime!.isolationProfile = 'hostNative';

    expect(buildPiCommand(f.config, f.manifest).command).toBe(realpathSync(join(f.root, 'pi')));
  }, 10_000);

  it('fails closed when a host-native binary is not the reviewed Pi pin', () => {
    const f = fixture();
    f.config.computeCostBinding = undefined;
    writeFileSync(join(f.root, 'pi'), '#!/bin/sh\nprintf "pi 0.1.0\\n"\n');
    f.manifest.profiles.hostNative = {
      kind: 'host-native',
      unattended: true,
      piBinary: join(f.root, 'pi'),
      toolchainPaths: [f.root],
      workspaceRoots: [f.workspace],
      nodeResourceRoots: [f.root],
      sessionRoot: f.sessionRoot,
      envAllowlist: [],
      extensions: [],
      skills: [],
      contextFiles: [],
    };
    f.config.conversationRuntime!.isolationProfile = 'hostNative';
    expect(() => buildPiCommand(f.config, f.manifest)).toThrow('Pi host binary version does not match the pin');
  });
});

describe('Pi LF-JSONL RPC adapter', () => {

  it('terminates a timed-out prompt instead of leaving detached Pi work running', async () => {
    const f = fixture();
    f.config.computeCostBinding = undefined;
    const commands: RpcRecord[] = [];
    const proc = fakeProcess(
      f.config.sessionId,
      commands,
      () => ({ input: 0, output: 0, cacheRead: 0, cacheWrite: 0 }),
      {},
      (command) => command.type === 'prompt' ? false : undefined,
    );
    const runtime = createPiConversationRuntime({
      manifest: f.manifest,
      launcher: () => proc as never,
      requestTimeoutMs: 20,
    });
    const endings: SessionEndReason[] = [];
    runtime.on('session-end', (_tenantId, reason) => endings.push(reason));
    runtime.on('error', () => undefined);

    await runtime.spawn(f.config);
    await expect(runtime.send(f.config.tenantId, { type: 'human', content: 'long task' }, {
      correlationId: 'turn:timeout', idempotencyKey: 'turn:timeout', effect: 'no-effect',
    })).rejects.toThrow('Pi RPC prompt request timed out');
    await tick();

    expect(proc.kill).toHaveBeenCalledWith('SIGKILL');
    expect(runtime.get(f.config.tenantId)).toBeUndefined();
    expect(endings).toContainEqual({
      type: 'runtime-failed', runtime: 'pi', correlationId: 'turn:timeout',
    });
    runtime.dispose();
  });

  it.each([
    ['error', '402 Insufficient credits https://provider.test/key/secret-key', 'provider_quota_exceeded'],
    ['error', '429 Rate limit', 'provider_rate_limited'],
    ['error', '401 Authentication failed secret-key', 'provider_auth_failed'],
    ['error', 'maximum context length exceeded', 'provider_context_exceeded'],
    ['error', 'Your input exceeds the context window of this model. Please adjust your input and try again.', 'provider_context_exceeded'],
    ['error', 'upstream disconnected secret-key', 'provider_request_failed'],
    ['error', 'fetch failed', 'provider_request_failed'],
    ['error', 'Codex error: This request was blocked by our safety systems. Reason: Potentially unintended activity.', 'provider_safety_rejected'],
    ['aborted', '', 'runtime_turn_aborted'],
  ])('delivers %s failures at settlement and allows the next turn', async (stopReason, errorMessage, code) => {
    const f = fixture();
    f.config.computeCostBinding = undefined;
    const proc = fakeProcess(f.config.sessionId, [], () => ({ input: 0, output: 0, cacheRead: 0, cacheWrite: 0 }));
    const runtime = createPiConversationRuntime({ manifest: f.manifest, launcher: () => proc as never, requestTimeoutMs: 100 });
    const responses: StreamJsonMessage[] = [];
    runtime.on('response', (_tenant, message) => responses.push(message));
    await runtime.spawn(f.config);
    await runtime.send(f.config.tenantId, { type: 'human', content: '?' });
    emitRecord(proc, { type: 'message_end', message: { role: 'assistant', content: [], stopReason, errorMessage } });
    expect(responses).toHaveLength(0);
    emitRecord(proc, { type: 'agent_settled' });
    emitRecord(proc, { type: 'agent_settled' });
    await tick();
    expect(responses).toHaveLength(2);
    expect(responses[0]).toMatchObject({ type: 'assistant', delivery_phase: 'final', message: { content: [{ type: 'text', text: expect.stringContaining(code) }] } });
    if (errorMessage === 'fetch failed') {
      expect(String(responses[0].content)).toContain('fetch failed');
      expect(String(responses[0].content)).toContain('请求编号');
    }
    expect(responses[1]).toMatchObject({ type: 'result', subtype: 'error_during_execution', is_error: true, error_code: code, public_failure_reported: true });
    expect(JSON.stringify(responses)).not.toContain('secret-key');
    expect(runtime.get(f.config.tenantId)?.state).toBe('idle');
    responses.length = 0;
    await runtime.send(f.config.tenantId, { type: 'human', content: 'retry' });
    emitRecord(proc, { type: 'message_end', message: { role: 'assistant', content: [{ type: 'text', text: 'recovered' }], stopReason: 'stop' } });
    emitRecord(proc, { type: 'agent_settled' });
    await tick();
    expect(responses[0]?.content).toBe('recovered');
    expect(responses[1]).toMatchObject({ type: 'result', subtype: 'success' });
    expect(responses[1]?.is_error).not.toBe(true);
    runtime.dispose();
  });

  it('does not surface transient native retry failures after a successful attempt', async () => {
    const f = fixture();
    f.config.computeCostBinding = undefined;
    const proc = fakeProcess(f.config.sessionId, [], () => ({ input: 0, output: 0, cacheRead: 0, cacheWrite: 0 }));
    const runtime = createPiConversationRuntime({ manifest: f.manifest, launcher: () => proc as never, requestTimeoutMs: 100 });
    const responses: StreamJsonMessage[] = [];
    runtime.on('response', (_tenant, message) => responses.push(message));
    await runtime.spawn(f.config);
    await runtime.send(f.config.tenantId, { type: 'human', content: '?' });
    emitRecord(proc, { type: 'message_end', message: { role: 'assistant', content: [], stopReason: 'error', errorMessage: '429' } });
    emitRecord(proc, { type: 'message_end', message: { role: 'user', content: 'retry context' } });
    emitRecord(proc, { type: 'message_end', message: { role: 'assistant', content: [{ type: 'text', text: 'recovered' }], stopReason: 'stop' } });
    emitRecord(proc, { type: 'agent_settled' });
    await tick();
    expect(responses.map(x => x.type)).toEqual(['assistant', 'result']);
    expect(responses[0]?.content).toBe('recovered');
    expect(responses[1]).toMatchObject({ subtype: 'success' });
    runtime.dispose();
  });

  it('carries the WI-declaration reminder on each new prompt when sessionWiGuard is on', async () => {
    const f = fixture();
    f.config.computeCostBinding = undefined;
    f.config.sessionWiGuard = true;
    const commands: RpcRecord[] = [];
    const proc = fakeProcess(f.config.sessionId, commands, () => ({ input: 1, output: 1, cacheRead: 0, cacheWrite: 0 }));
    const runtime = createPiConversationRuntime({ manifest: f.manifest, launcher: vi.fn(() => proc as never), requestTimeoutMs: 100 });
    await runtime.spawn(f.config);
    const turn = { correlationId: 'turn:wi', idempotencyKey: 'turn:wi', effect: 'no-effect' as const };
    await runtime.send(f.config.tenantId, { type: 'human', content: 'ship it' }, turn);
    expect(commands.at(-1)).toMatchObject({ type: 'prompt', message: `ship it\n\n${SESSION_WI_REMINDER}` });
    runtime.dispose();
  });

  it('waits for agent_settled, suppresses unclassified deltas, and reports only current native calls', async () => {
    const f = fixture();
    f.config.computeCostBinding = undefined;
    const commands: RpcRecord[] = [];
    let usage = { input: 10, output: 2, cacheRead: 3, cacheWrite: 1 };
    const proc = fakeProcess(f.config.sessionId, commands, () => usage);
    const launcher = vi.fn(() => proc as never);
    const runtime = createPiConversationRuntime({ manifest: f.manifest, launcher, requestTimeoutMs: 100 });
    const responses: StreamJsonMessage[] = [];
    const deltas: string[] = [];
    runtime.on('response', (_tenantId, message) => responses.push(message));
    runtime.on('stream-delta', (_tenantId, delta) => deltas.push(delta));

    await runtime.spawn(f.config);
    const turn = { correlationId: 'turn:stable', idempotencyKey: 'turn:stable', effect: 'no-effect' as const };
    await runtime.send(f.config.tenantId, { type: 'human', content: 'hello' }, turn);
    expect(commands.at(-1)).toMatchObject({ type: 'prompt', id: 'turn:stable', message: 'hello' });
    emitRecord(proc, { type: 'message_update', assistantMessageEvent: { type: 'text_delta', delta: 'draft ' } });
    emitRecord(proc, { type: 'agent_end' });
    await tick();
    expect(responses).toHaveLength(0);

    emitRecord(proc, {
      type: 'message_end',
      message: { role: 'assistant', content: [{ type: 'text', text: 'final answer' }],
        usage: { input: 5, output: 5, cacheRead: 2, cacheWrite: 0 } },
    });
    usage = { input: 15, output: 7, cacheRead: 5, cacheWrite: 1 };
    emitRecord(proc, { type: 'agent_settled' });
    await tick();

    expect(deltas).toEqual([]);
    expect(responses).toEqual([
      expect.objectContaining({ type: 'assistant', content: 'final answer', correlation_id: 'turn:stable' }),
      expect.objectContaining({
        type: 'result',
        provider: 'anthropic',
        model: 'claude-sonnet-4-5',
        correlation_id: 'turn:stable',
        usage: {
          input_tokens: 5,
          output_tokens: 5,
          cache_read_input_tokens: 2,
          cache_creation_input_tokens: 0,
        },
      }),
    ]);

    responses.length = 0;
    await runtime.send(f.config.tenantId, { type: 'human', content: 'again' }, {
      correlationId: 'turn:two', idempotencyKey: 'turn:two', effect: 'no-effect',
    });
    usage = { input: 18, output: 9, cacheRead: 5, cacheWrite: 2 };
    emitRecord(proc, {type:'message_end',message:{role:'assistant',content:[],
      usage:{input:3,output:2,cacheRead:0,cacheWrite:1}}});
    emitRecord(proc, { type: 'agent_settled' });
    await tick();
    expect(responses.at(-1)?.usage).toEqual({
      input_tokens: 3,
      output_tokens: 2,
      cache_read_input_tokens: 0,
      cache_creation_input_tokens: 1,
    });
    runtime.dispose();
  });

  it.each(['pending','failure'])('statistics cannot delay startup, settlement or restart: %s', async mode => {
    const f=fixture();f.config.computeCostBinding=undefined;
    const commands:RpcRecord[]=[];
    const proc=fakeProcess(f.config.sessionId,commands,()=>({input:99999,output:99999,cacheRead:3,cacheWrite:1}),{},(command,p)=>{
      if(command.type==='get_session_stats'){
        if(mode==='failure')queueMicrotask(()=>emitRecord(p,{type:'response',id:command.id,command:'get_session_stats',success:false,error:'stats failed'}));
        return false;
      }
    });
    const runtime=createPiConversationRuntime({manifest:f.manifest,launcher:()=>proc as never,requestTimeoutMs:100});
    const responses:StreamJsonMessage[]=[];runtime.on('response',(_id,msg)=>responses.push(msg));
    await runtime.spawn({...f.config,resume:true});
    for(let i=0;i<3;i++){
      await runtime.send(f.config.tenantId,{type:'human',content:'hello'},{correlationId:`turn:${i}`,idempotencyKey:`turn:${i}`,effect:'no-effect'});
      if(i===1)emitRecord(proc,{type:mode==='pending'?'compaction_start':'auto_compaction_start'});
      if(i>0)emitRecord(proc,{type:'message_end',message:{role:'assistant',content:[],usage:{input:5,output:1,cacheRead:0,cacheWrite:0}}});
      emitRecord(proc,{type:'agent_settled'});await tick();
      expect(runtime.get(f.config.tenantId)?.state).toBe('idle');
    }
    const results=responses.filter(r=>r.type==='result');
    expect(results).toHaveLength(3);
    expect(results[0].usage).toBeUndefined();expect(results[1].usage).toBeUndefined();
    expect(results[2].usage).toEqual({input_tokens:5,output_tokens:1,cache_read_input_tokens:0,cache_creation_input_tokens:0});
    expect(commands.some(c=>c.type==='get_session_stats')).toBe(false);
    runtime.dispose();
  });

  it.each(['toolResult','usage','branch_summary'])('does not present an assistant-only subtotal as complete with %s',async kind=>{
    const f=fixture();f.config.computeCostBinding=undefined;
    const proc=fakeProcess(f.config.sessionId,[],()=>({}));
    const runtime=createPiConversationRuntime({manifest:f.manifest,launcher:()=>proc as never,requestTimeoutMs:100});
    const results:StreamJsonMessage[]=[];runtime.on('response',(_id,msg)=>{if(msg.type==='result')results.push(msg);});
    await runtime.spawn(f.config);
    for(let i=0;i<2;i++){
      await runtime.send(f.config.tenantId,{type:'human',content:'work'});
      emitRecord(proc,{type:'message_end',message:{role:'assistant',content:[],usage:{input:5,output:1,cacheRead:0,cacheWrite:0}}});
      if(i===0)emitRecord(proc,kind==='toolResult'?{type:'message_end',message:{role:'toolResult',usage:[{}]}}
        :{type:'entry_appended',entry:{type:kind,usage:[{}]}});
      emitRecord(proc,{type:'agent_settled'});await tick();
    }
    expect(results[0]).toMatchObject({usage_status:'unavailable'});
    expect(results[0]?.usage).toBeUndefined();
    expect(results[1]?.usage).toEqual({input_tokens:5,output_tokens:1,cache_read_input_tokens:0,cache_creation_input_tokens:0});
    runtime.dispose();
  });

  it('does not attribute a late explicit cumulative observation to another dispatch',async()=>{
    const f=fixture();f.config.computeCostBinding=undefined;
    let stats:RpcRecord|undefined;
    const proc=fakeProcess(f.config.sessionId,[],()=>({}),{},command=>{
      if(command.type==='get_session_stats'){stats=command;return false;}
    });
    const runtime=createPiConversationRuntime({manifest:f.manifest,launcher:()=>proc as never,requestTimeoutMs:1000});
    const responses:StreamJsonMessage[]=[];runtime.on('response',(_id,msg)=>responses.push(msg));
    await runtime.spawn(f.config);
    const observation=runtime.getNativeSessionStats!(f.config.tenantId);
    for(let i=0;i<2;i++){
      await runtime.send(f.config.tenantId,{type:'human',content:'work'});
      emitRecord(proc,{type:'message_end',message:{role:'assistant',content:[],usage:{input:5,output:1,cacheRead:0,cacheWrite:0}}});
      emitRecord(proc,{type:'agent_settled'});await tick();
    }
    emitRecord(proc,{type:'response',id:stats!.id,command:'get_session_stats',success:true,data:{tokens:{input:999999,output:999,cacheRead:0,cacheWrite:0}}});
    await observation;
    const results=responses.filter(r=>r.type==='result');
    expect(results).toHaveLength(2);
    for(const result of results)expect(result.usage).toEqual({input_tokens:5,output_tokens:1,cache_read_input_tokens:0,cache_creation_input_tokens:0});
    runtime.dispose();
  });

  it('preserves public assistant text before a tool and emits the settled tail as the sole final', async () => {
    const f = fixture();
    f.config.computeCostBinding = undefined;
    const commands: RpcRecord[] = [];
    const proc = fakeProcess(f.config.sessionId, commands, () => ({ input: 0, output: 0, cacheRead: 0, cacheWrite: 0 }));
    const runtime = createPiConversationRuntime({ manifest: f.manifest, launcher: () => proc as never, requestTimeoutMs: 100 });
    const responses: StreamJsonMessage[] = [];
    runtime.on('response', (_tenantId, message) => responses.push(message));

    await runtime.spawn(f.config);
    await runtime.send(f.config.tenantId, { type: 'human', content: 'work' }, {
      correlationId: 'turn:process', idempotencyKey: 'turn:process', effect: 'no-effect',
    });
    emitRecord(proc, {
      type: 'message_end',
      message: { role: 'assistant', content: [{ type: 'text', text: '先检查现场。' }, { type: 'toolCall', name: 'bash' }] },
    });
    emitRecord(proc, { type: 'tool_execution_start', toolName: 'bash', toolCallId: 'tool-1', args: { command: 'npm test' } });
    emitRecord(proc, { type: 'tool_execution_end', toolCallId: 'tool-1', isError: false, result: { content: [{ type: 'text', text: 'tests passed' }] } });
    emitRecord(proc, {
      type: 'message_end',
      message: { role: 'assistant', content: [{ type: 'text', text: '最终结论。' }] },
    });
    emitRecord(proc, { type: 'agent_settled' });
    await tick();

    const work = responses.filter(message => message.type === 'work_process');
    expect(work.map(message => (message.process_event as any).state)).toEqual(['completed', 'running', 'completed']);
    expect((work[0].process_event as any).kind).toBe('commentary');
    expect(JSON.stringify(work)).toContain('npm test');
    expect(JSON.stringify(work)).toContain('tests passed');
    expect(JSON.stringify(work)).toContain('先检查现场');
    expect(JSON.stringify(work)).not.toContain('最终结论');
    expect(responses.filter((message) => message.type === 'assistant')).toEqual([
      expect.objectContaining({ content: '最终结论。', delivery_phase: 'final' }),
    ]);
    runtime.dispose();
  });

  it('queues a busy user correction through Pi steering instead of rejecting it', async () => {
    const f = fixture();
    f.config.computeCostBinding = undefined;
    const commands: RpcRecord[] = [];
    const proc = fakeProcess(f.config.sessionId, commands, () => ({ input: 0, output: 0, cacheRead: 0, cacheWrite: 0 }));
    const runtime = createPiConversationRuntime({ manifest: f.manifest, launcher: () => proc as never, requestTimeoutMs: 100 });

    await runtime.spawn(f.config);
    await runtime.send(f.config.tenantId, { type: 'human', content: 'first' });
    await runtime.steer!(f.config.tenantId, { type: 'human', content: '补充信息' });

    expect(commands.at(-1)).toMatchObject({ type: 'steer', message: '补充信息' });
    runtime.dispose();
  });

  it('maps image blocks into Pi RPC images and rejects native route mismatch', async () => {
    const f = fixture();
    f.config.computeCostBinding = undefined;
    const commands: RpcRecord[] = [];
    const proc = fakeProcess(f.config.sessionId, commands, () => ({ input: 0, output: 0, cacheRead: 0, cacheWrite: 0 }));
    const runtime = createPiConversationRuntime({ manifest: f.manifest, launcher: () => proc as never, requestTimeoutMs: 100 });
    await runtime.spawn(f.config);
    await runtime.send(f.config.tenantId, {
      type: 'human',
      content: [
        { type: 'image', source: { type: 'base64', media_type: 'image/png', data: 'aGVsbG8=' } },
        { type: 'text', text: 'describe' },
      ],
    }, { correlationId: 'turn:image', idempotencyKey: 'turn:image', effect: 'no-effect' });
    expect(commands.at(-1)).toMatchObject({
      type: 'prompt',
      id: 'turn:image',
      message: 'describe',
      images: [{ type: 'image', mimeType: 'image/png', data: 'aGVsbG8=' }],
    });
    runtime.dispose();

    const f2 = fixture();
    const mismatch = fakeProcess(
      f2.config.sessionId,
      [],
      () => ({ input: 0, output: 0, cacheRead: 0, cacheWrite: 0 }),
      { model: { provider: 'openai', id: 'wrong-model' } },
    );
    const runtime2 = createPiConversationRuntime({ manifest: f2.manifest, launcher: () => mismatch as never, requestTimeoutMs: 100 });
    await expect(runtime2.spawn(f2.config)).rejects.toThrow('native provider/model/reasoning binding mismatch');
    expect(mismatch.kill).toHaveBeenCalled();
    runtime2.dispose();
  });

  it('correlates abort and session switch, and contains session paths', async () => {
    const f = fixture();
    f.config.computeCostBinding = undefined;
    const commands: RpcRecord[] = [];
    const proc = fakeProcess(f.config.sessionId, commands, () => ({ input: 0, output: 0, cacheRead: 0, cacheWrite: 0 }));
    const runtime = createPiConversationRuntime({ manifest: f.manifest, launcher: () => proc as never, requestTimeoutMs: 100 });
    await runtime.spawn(f.config);
    const sessionDirIndex = commands.length;
    await runtime.send(f.config.tenantId, { type: 'human', content: 'cancel me' }, {
      correlationId: 'turn:cancel', idempotencyKey: 'turn:cancel', effect: 'no-effect',
    });
    await runtime.abortTurn(f.config.tenantId);
    emitRecord(proc, { type: 'agent_settled' });
    await tick();

    const spec = buildPiCommand(f.config, f.manifest);
    const sessionFile = join(spec.sessionDir, 'resume.jsonl');
    writeFileSync(sessionFile, '{}\n');
    await runtime.switchSession(f.config.tenantId, sessionFile);
    expect(commands.slice(sessionDirIndex)).toEqual(expect.arrayContaining([
      expect.objectContaining({ type: 'abort' }),
      expect.objectContaining({ type: 'switch_session', sessionPath: sessionFile }),
    ]));

    const outside = join(f.root, 'outside.jsonl');
    writeFileSync(outside, '{}\n');
    await expect(runtime.switchSession(f.config.tenantId, outside)).rejects.toThrow('escaped the tenant session directory');
    runtime.dispose();
  });

  it('lists native models and hot-switches provider/model while preserving the Pi session', async () => {
    const f = fixture();
    const commands: RpcRecord[] = [];
    const proc = fakeProcess(f.config.sessionId, commands, () => ({ input: 0, output: 0, cacheRead: 0, cacheWrite: 0 }));
    const runtime = createPiConversationRuntime({ manifest: f.manifest, launcher: () => proc as never, requestTimeoutMs: 100 });
    const session = await runtime.spawn(f.config);

    await expect(runtime.listModels(f.config.tenantId)).resolves.toEqual([
      { provider: 'anthropic', model: 'claude-sonnet-4-5', displayName: 'Claude Sonnet 4.5' },
      { provider: 'openrouter', model: 'anthropic/claude-opus-4.1', displayName: 'Claude Opus 4.1' },
    ]);
    await runtime.setModel(f.config.tenantId, {
      provider: 'openrouter',
      model: 'anthropic/claude-opus-4.1',
      reasoning: 'high',
    });

    expect(commands).toEqual(expect.arrayContaining([
      expect.objectContaining({ type: 'get_available_models' }),
      expect.objectContaining({ type: 'set_model', provider: 'openrouter', modelId: 'anthropic/claude-opus-4.1' }),
      expect.objectContaining({ type: 'get_state' }),
    ]));
    expect(session.sessionId).toBe(f.config.sessionId);

    await expect(runtime.listThinkingLevels(f.config.tenantId)).resolves.toEqual(['off', 'low', 'high']);
    await runtime.setThinkingLevel(f.config.tenantId, 'low');
    await expect(runtime.compact(f.config.tenantId, 'keep decisions')).resolves.toMatchObject({
      tokensBefore: 200,
      estimatedTokensAfter: 40,
    });
    await expect(runtime.getNativeSessionStats(f.config.tenantId)).resolves.toMatchObject({
      totalMessages: 8,
      contextUsage: { tokens: 21, contextWindow: 200, percent: 10.5 },
    });
    expect(commands).toEqual(expect.arrayContaining([
      expect.objectContaining({ type: 'get_available_thinking_levels' }),
      expect.objectContaining({ type: 'set_thinking_level', level: 'low' }),
      expect.objectContaining({ type: 'compact', customInstructions: 'keep decisions' }),
      expect.objectContaining({ type: 'get_session_stats' }),
    ]));
    runtime.dispose();
  });

  it('emits effects without treating ordinary workspace writes as delivery intent', async () => {
    const f = fixture();
    f.config.computeCostBinding = undefined;
    const commands: RpcRecord[] = [];
    const proc = fakeProcess(f.config.sessionId, commands, () => ({ input: 0, output: 0, cacheRead: 0, cacheWrite: 0 }));
    const runtime = createPiConversationRuntime({ manifest: f.manifest, launcher: () => proc as never, requestTimeoutMs: 100 });
    const effects: ConversationTurnEffect[] = [];
    const files: string[][] = [];
    runtime.on('turn-effect', (_tenantId, effect) => effects.push(effect));
    runtime.on('tool-files', (_tenantId, paths) => files.push(paths));
    await runtime.spawn(f.config);
    await runtime.send(f.config.tenantId, { type: 'human', content: 'work' });
    const output = join(realpathSync(f.workspace), 'result.txt');
    writeFileSync(output, 'ok');
    emitRecord(proc, { type: 'tool_execution_start', toolName: 'read', toolCallId: 'read-1', args: { path: output } });
    emitRecord(proc, { type: 'tool_execution_start', toolName: 'write', toolCallId: 'write-1', args: { path: output } });
    emitRecord(proc, { type: 'tool_execution_end', toolCallId: 'write-1', isError: false });
    emitRecord(proc, { type: 'tool_execution_start', toolName: 'write', toolCallId: 'escape', args: { path: '../outside.txt' } });
    emitRecord(proc, { type: 'tool_execution_end', toolCallId: 'escape', isError: false });
    emitRecord(proc, { type: 'agent_settled' });
    await tick();
    expect(effects).toEqual(['read-only-effect', 'side-effect-observed', 'side-effect-observed']);
    expect(files).toEqual([]);
    expect(existsSync(output)).toBe(true);
    runtime.dispose();
  });

  it('scans the explicit outbox once after settlement and de-duplicates intermediate edits', async () => {
    const f = fixture();
    f.config.computeCostBinding = undefined;
    const commands: RpcRecord[] = [];
    const proc = fakeProcess(f.config.sessionId, commands, () => ({ input: 0, output: 0, cacheRead: 0, cacheWrite: 0 }));
    const runtime = createPiConversationRuntime({ manifest: f.manifest, launcher: () => proc as never, requestTimeoutMs: 100 });
    const files: string[][] = [];
    runtime.on('tool-files', (_tenantId, paths) => files.push(paths));
    await runtime.spawn(f.config);
    await runtime.send(f.config.tenantId, { type: 'human', content: 'deliver files' });

    const outbox = resolveToolFileOutboxPath(f.config.tenantId, f.workspace);
    mkdirSync(join(outbox, 'nested'), { recursive: true });
    const first = join(outbox, 'a.txt');
    const second = join(outbox, 'b.txt');
    writeFileSync(first, 'draft');
    writeFileSync(first, 'final');
    writeFileSync(second, 'second');
    writeFileSync(join(outbox, '.hidden'), 'hidden');
    writeFileSync(join(outbox, 'nested', 'ignored.txt'), 'nested');
    emitRecord(proc, { type: 'tool_execution_start', toolName: 'write', toolCallId: 'write-1', args: { path: first } });
    emitRecord(proc, { type: 'tool_execution_end', toolCallId: 'write-1', isError: false });
    expect(files).toEqual([]);

    emitRecord(proc, { type: 'agent_settled' });
    await tick();
    expect(files).toEqual([[first, second]]);
    expect(existsSync(first)).toBe(true);
    expect(existsSync(second)).toBe(true);
    runtime.dispose();
  });

  it('fails closed on malformed or unterminated non-LF protocol records', async () => {
    const f = fixture();
    const commands: RpcRecord[] = [];
    const proc = fakeProcess(f.config.sessionId, commands, () => ({ input: 0, output: 0, cacheRead: 0, cacheWrite: 0 }));
    const runtime = createPiConversationRuntime({ manifest: f.manifest, launcher: () => proc as never, requestTimeoutMs: 100 });
    const errors: string[] = [];
    runtime.on('error', (_tenantId, error) => errors.push(error.message));
    await runtime.spawn(f.config);

    proc.stdout.write('{"type":"agent_end"}\u2028{"type":"agent_settled"}\n');
    await tick();
    expect(errors).toContain('Pi RPC emitted malformed LF-delimited JSON');
    expect(proc.kill).toHaveBeenCalledWith('SIGKILL');
    runtime.dispose();

    const f2 = fixture();
    const proc2 = fakeProcess(f2.config.sessionId, [], () => ({ input: 0, output: 0, cacheRead: 0, cacheWrite: 0 }));
    const runtime2 = createPiConversationRuntime({ manifest: f2.manifest, launcher: () => proc2 as never, requestTimeoutMs: 100 });
    const errors2: string[] = [];
    runtime2.on('error', (_tenantId, error) => errors2.push(error.message));
    await runtime2.spawn(f2.config);
    proc2.stdout.write('{"type":"agent_end"}');
    proc2.stdout.end();
    await tick();
    expect(errors2).toContain('Pi RPC stdout ended with an unterminated JSONL record');
    runtime2.dispose();
  });

  it('reports a user-visible runtime failure without leaking stderr secrets', async () => {
    const f = fixture();
    const commands: RpcRecord[] = [];
    const proc = fakeProcess(f.config.sessionId, commands, () => ({ input: 0, output: 0, cacheRead: 0, cacheWrite: 0 }));
    const runtime = createPiConversationRuntime({ manifest: f.manifest, launcher: () => proc as never, requestTimeoutMs: 100 });
    const endings: SessionEndReason[] = [];
    const traces: Record<string, unknown>[] = [];
    runtime.on('session-end', (_tenantId, reason) => endings.push(reason));
    runtime.on('native-trace', (_tenantId, trace) => traces.push(trace));
    await runtime.spawn(f.config);
    proc.stderr.write('SECRET_TOKEN=must-not-leak');
    emitRecord(proc, { type: 'message_update', assistantMessageEvent: { type: 'text_delta', delta: 'private text' } });
    proc.emit('exit', 9, null);
    await tick();
    expect(endings).toEqual([{ type: 'runtime-failed', runtime: 'pi', correlationId: undefined }]);
    expect(JSON.stringify(traces)).not.toContain('must-not-leak');
    expect(JSON.stringify(traces)).not.toContain('private text');
    runtime.dispose();
  });

  it.each(['baseline', 'seal'] as const)('preserves an unknown %s result when the control acknowledgement times out', async (kind) => {
    const f = fixture();
    const commands: RpcRecord[] = [];
    const binding = f.config.computeCostBinding!;
    const { unsigned, snapshotSha } = boundedUsageSnapshot(binding, 'open');
    const artifactRef = `${binding.work_item_id}-${binding.binding_sha256}-baseline.json`;
    const proc = fakeProcess(f.config.sessionId, commands,
      () => ({ input: 0, output: 0, cacheRead: 0, cacheWrite: 0 }), {},
      (command, target) => {
        if (kind === 'seal' && command.message === '/runtime-usage-baseline') {
          emitRecord(target, { type: 'extension_ui_request', method: 'notify',
            message: `PULSE_USAGE_BASELINE/v1 ${JSON.stringify({ ok: true, receipt: {
              schema: 'pi_host_usage_capture_receipt/v1', work_item_id: binding.work_item_id,
              epoch: binding.epoch, lifecycle_state: 'open', binding_sha256: binding.binding_sha256,
              snapshot_sha256: snapshotSha, artifact_ref: artifactRef,
            } })}` });
          return;
        }
        if (command.type === 'prompt' && String(command.message).startsWith('/runtime-usage-')) return false;
      });
    const runtime = createPiConversationRuntime({
      manifest: f.manifest, launcher: () => proc as never, requestTimeoutMs: 20,
    });
    const session = await runtime.spawn(f.config);
    try {
      if (kind === 'seal') {
        const usageDir = join(session.sessionDir, 'compute-cost');
        mkdirSync(usageDir, { recursive: true, mode: 0o700 });
        writeFileSync(join(usageDir, artifactRef), JSON.stringify({ ...unsigned, snapshot_sha256: snapshotSha }), { mode: 0o600 });
        await runtime.activateRuntimeUsageBaseline!(f.config.tenantId);
      }
      const call = kind === 'baseline' ? runtime.activateRuntimeUsageBaseline! : runtime.sealRuntimeUsage!;
      await expect(call(f.config.tenantId)).rejects.toMatchObject({
        code: 'measurement_transport_unavailable', retryable: true,
      });
      expect(commands.filter(c => c.message === `/runtime-usage-${kind}`)).toHaveLength(1);
      expect(runtime.get(f.config.tenantId)).toBeDefined();
      await runtime.send(f.config.tenantId, { type: 'human', content: 'explain the unknown result' });
      expect(commands.at(-1)?.message).toBe('explain the unknown result');
      expect(proc.kill).not.toHaveBeenCalled();
    } finally {
      runtime.dispose();
    }
  });

  it('activates a canonical baseline from the per-session directory before any LLM turn', async () => {
    const f = fixture();
    const commands: RpcRecord[] = [];
    const binding = f.config.computeCostBinding!;
    const { unsigned, snapshotSha } = boundedUsageSnapshot(binding, 'open');
    const artifactRef = `${binding.work_item_id}-${binding.binding_sha256}-baseline.json`;
    const proc = fakeProcess(
      f.config.sessionId,
      commands,
      () => ({ input: 0, output: 0, cacheRead: 0, cacheWrite: 0 }),
      {},
      (command, target) => {
        if (command.type === 'prompt' && command.message === '/runtime-usage-baseline') {
          emitRecord(target, {
            type: 'extension_ui_request', method: 'notify',
            message: `PULSE_USAGE_BASELINE/v1 ${JSON.stringify({ ok: true, receipt: {
              schema: 'pi_host_usage_capture_receipt/v1', work_item_id: binding.work_item_id,
              epoch: binding.epoch, lifecycle_state: 'open', binding_sha256: binding.binding_sha256,
              snapshot_sha256: snapshotSha, artifact_ref: artifactRef,
            } })}`,
          });
        }
      },
    );
    const runtime = createPiConversationRuntime({
      manifest: f.manifest, launcher: () => proc as never, requestTimeoutMs: 100,
    });
    const session = await runtime.spawn(f.config);
    const usageDir = join(session.sessionDir, 'compute-cost');
    mkdirSync(usageDir, { recursive: true, mode: 0o700 });
    writeFileSync(join(usageDir, artifactRef), JSON.stringify({ ...unsigned, snapshot_sha256: snapshotSha }), { mode: 0o600 });

    const readback = await runtime.activateRuntimeUsageBaseline!(f.config.tenantId);

    expect(readback).toMatchObject({ status: 'open', artifact_ref: artifactRef, baseline_digest: snapshotSha });
    expect(commands).toContainEqual(expect.objectContaining({ type: 'prompt', message: '/runtime-usage-baseline' }));
    expect(session.state).toBe('ready');
    runtime.dispose();
  });

  it('returns a mode-0600 digest-validated usage artifact readback without an LLM turn', async () => {
    const f = fixture();
    const commands: RpcRecord[] = [];
    const usageDir = join(f.root, 'usage');
    mkdirSync(usageDir, { mode: 0o700 });
    const binding = f.config.computeCostBinding!;
    const baseline = boundedUsageSnapshot(binding, 'open');
    const baselineRef = `${binding.work_item_id}-${binding.binding_sha256}-baseline.json`;
    writeFileSync(
      join(usageDir, baselineRef),
      JSON.stringify({ ...baseline.unsigned, snapshot_sha256: baseline.snapshotSha }),
      { mode: 0o600 },
    );
    const { unsigned, snapshotSha } = boundedUsageSnapshot(binding, 'sealed');
    const artifactRef = `${binding.work_item_id}-${binding.binding_sha256}-sealed.json`;
    writeFileSync(join(usageDir, artifactRef), JSON.stringify({ ...unsigned, snapshot_sha256: snapshotSha }), { mode: 0o600 });
    const proc = fakeProcess(
      f.config.sessionId,
      commands,
      () => ({ input: 1, output: 1, cacheRead: 0, cacheWrite: 0 }),
      {},
      (command, target) => {
        if (command.type === 'prompt' && command.message === '/runtime-usage-seal') {
          emitRecord(target, {
            type: 'extension_ui_request', method: 'notify',
            message: `PULSE_USAGE_SEAL/v1 ${JSON.stringify({ ok: true, receipt: {
              schema: 'pi_host_usage_capture_receipt/v1', work_item_id: binding.work_item_id,
              epoch: binding.epoch, lifecycle_state: 'sealed', binding_sha256: binding.binding_sha256,
              snapshot_sha256: snapshotSha, artifact_ref: artifactRef,
            } })}`,
          });
        }
      },
    );
    const runtime = createPiConversationRuntime({
      manifest: f.manifest, launcher: () => proc as never, requestTimeoutMs: 100, usageSnapshotDir: usageDir,
    });
    const session = await runtime.spawn(f.config);
    (session as unknown as { usageBaselineActivated: boolean }).usageBaselineActivated = true;

    const readback = await runtime.sealRuntimeUsage!(f.config.tenantId);

    expect(readback).toMatchObject({ status: 'sealed', artifact_ref: artifactRef, sealed_digest: snapshotSha });
    expect(commands).toContainEqual(expect.objectContaining({ type: 'prompt', message: '/runtime-usage-seal' }));
    expect(session.state).toBe('ready');
    runtime.dispose();
  });

  it('rejects usage sealing while a native Pi turn is active', async () => {
    const f = fixture();
    const commands: RpcRecord[] = [];
    const proc = fakeProcess(f.config.sessionId, commands, () => ({ input: 1, output: 1, cacheRead: 0, cacheWrite: 0 }));
    const runtime = createPiConversationRuntime({ manifest: f.manifest, launcher: () => proc as never, requestTimeoutMs: 100 });
    const session = await runtime.spawn(f.config);
    await runtime.send(f.config.tenantId, { type: 'human', content: 'explain while metering is unavailable' });
    expect(session.state).toBe('busy');

    await expect(runtime.sealRuntimeUsage!(f.config.tenantId)).rejects.toThrow(/cannot seal before/);
    runtime.dispose();
  });

  it('restarts with the same tenant session binding and evicts an idle process', async () => {
    const f = fixture();
    f.config.computeCostBinding = undefined;
    const commands: RpcRecord[][] = [[], []];
    const procs = [
      fakeProcess(f.config.sessionId, commands[0], () => ({ input: 1, output: 1, cacheRead: 0, cacheWrite: 0 })),
      fakeProcess(f.config.sessionId, commands[1], () => ({ input: 1, output: 1, cacheRead: 0, cacheWrite: 0 })),
    ];
    let launchIndex = 0;
    const launchArgs: string[][] = [];
    const runtime = createPiConversationRuntime({
      manifest: f.manifest,
      launcher: (_command, args) => {
        launchArgs.push([...args]);
        return procs[launchIndex++] as never;
      },
      requestTimeoutMs: 100,
      idleTimeoutMs: 10,
      cleanupIntervalMs: 5,
      killGraceMs: 20,
    });
    const endings: SessionEndReason[] = [];
    runtime.on('session-end', (_tenantId, reason) => endings.push(reason));

    await runtime.spawn(f.config);
    await runtime.kill(f.config.tenantId);
    await runtime.spawn({
      ...f.config,
      resume: true,
      runtimeSessionBinding: { runtime: 'pi', sessionId: f.config.sessionId },
    });
    const firstSessionDir = launchArgs[0]?.[launchArgs[0].indexOf('--session-dir') + 1];
    const secondSessionDir = launchArgs[1]?.[launchArgs[1].indexOf('--session-dir') + 1];
    expect(secondSessionDir).toBe(firstSessionDir);
    expect(commands[1]).toEqual(expect.arrayContaining([
      expect.objectContaining({ type: 'get_state' }),
    ]));
    expect(commands[1]?.some(c=>c.type==='get_session_stats')).toBe(false);

    await vi.waitFor(
      () => expect(endings).toContainEqual({ type: 'idle-reclaimed' }),
      { timeout: 1_000, interval: 10 },
    );
    expect(runtime.get(f.config.tenantId)).toBeUndefined();
    runtime.dispose();
  });
});

it('builds a resumable Pi command when legacy restarts left multiple native generations',()=>{
 const {config,manifest}=fixture();const first=buildPiCommand(config,manifest,{});
 for(const [name,timestamp] of [['older','2099-01-01T00:00:00.000Z'],['latest','2099-01-02T00:00:00.000Z']]){
  writeFileSync(join(first.sessionDir,`${name}_${config.sessionId}.jsonl`),JSON.stringify({type:'session',id:config.sessionId,timestamp})+'\n');
 }
 const resumed=buildPiCommand(config,manifest,{});
 expect(resumed.args).toContain('--session');
 expect(resumed.args[resumed.args.indexOf('--session')+1]).toBe(join(first.sessionDir,`latest_${config.sessionId}.jsonl`));
 expect(resumed.args).not.toContain('--session-id');
});


describe('conversation recovery across runtime upgrades', () => {
  it('resumes a valid v2 binding without fabricating v3 measurement authority', async () => {
    const f = fixture();
    f.config.computeCostBinding = buildRuntimeUsageBinding({
      workItemId: 'wi_20260101_123', agentId: 'pulse', phase: 'work_item', epoch: 1,
      runtime: 'pi', procurementSource: 'unknown',
      authorityRef: runtimeAuthorityRef(f.config.sessionId), issuedAt: '2026-01-01T00:00:00Z',
    });
    const original = JSON.stringify(f.config.computeCostBinding);
    const spec = buildPiCommand(f.config, f.manifest);
    expect(spec.env.PULSE_RUNTIME_USAGE_BINDING).toBeUndefined();
    mkdirSync(spec.sessionDir,{recursive:true});
    const sessionFile=join(spec.sessionDir,'legacy.jsonl');
    writeFileSync(sessionFile,JSON.stringify({type:'session',id:f.config.sessionId})+'\n');
    const commands: RpcRecord[] = [];
    const proc = fakeProcess(f.config.sessionId, commands, () => ({input:0,output:0,cacheRead:0,cacheWrite:0}),{sessionFile});
    const runtime = createPiConversationRuntime({manifest:f.manifest,launcher:()=>proc as never});
    try {
      await runtime.spawn(f.config);
      await runtime.send(f.config.tenantId,{type:'human',content:'continue the conversation'});
      expect(commands.some(c=>c.type==='prompt')).toBe(true);
      emitRecord(proc,{type:'agent_settled'});
      await vi.waitFor(()=>expect(runtime.get(f.config.tenantId)?.state).toBe('idle'));
      const retired=await runtime.sealRuntimeUsage!(f.config.tenantId);
      expect(retired.status).toBe('legacy_unmeasured');
      const artifact=JSON.parse(readFileSync(retired.artifact_ref,'utf8'));
      expect(artifact).toMatchObject({coverage:'unavailable',reason:'legacy_binding_without_bounded_baseline',binding_sha256:f.config.computeCostBinding.binding_sha256});
      expect(artifact).not.toHaveProperty('totals');
      expect(await runtime.sealRuntimeUsage!(f.config.tenantId)).toEqual(retired);
      await expect(runtime.send(f.config.tenantId,{type:'human',content:'continue authorized work with legacy usage unavailable'})).resolves.toBeUndefined();
      expect(commands.some(c=>c.type==='prompt'&&c.message==='/runtime-usage-seal')).toBe(false);
      expect(JSON.stringify(f.config.computeCostBinding)).toBe(original);
    } finally { runtime.dispose(); }
  });

  it('accepts the native supported thinking level when the requested level is unavailable', async () => {
    const f=fixture(); f.config.computeCostBinding=undefined;
    const commands:RpcRecord[]=[];
    const proc=fakeProcess(f.config.sessionId,commands,()=>({input:0,output:0,cacheRead:0,cacheWrite:0}),
      {thinkingLevel:'off'},(command,child)=>{
        if(command.type==='get_available_thinking_levels'){
          queueMicrotask(()=>emitRecord(child,{type:'response',id:command.id,command:command.type,success:true,data:{levels:['off']}}));
          return false;
        }
      });
    const runtime=createPiConversationRuntime({manifest:f.manifest,launcher:()=>proc as never});
    try {
      const session=await runtime.spawn(f.config);
      expect(session.state).toBe('ready');
      await runtime.send(f.config.tenantId,{type:'human',content:'hello after reset'});
      expect(commands.at(-1)?.type).toBe('prompt');
    } finally {runtime.dispose();}
  });
});


it('a late exit from the replaced process cannot remove the fresh conversation', async () => {
  const f=fixture();f.config.computeCostBinding=undefined;
  const commands:RpcRecord[][]=[[],[]];
  const old=fakeProcess(f.config.sessionId,commands[0],()=>({input:0,output:0,cacheRead:0,cacheWrite:0}));
  old.kill=vi.fn(()=>true);
  const fresh=fakeProcess('fresh-session',commands[1],()=>({input:0,output:0,cacheRead:0,cacheWrite:0}));
  let launches=0;
  const runtime=createPiConversationRuntime({manifest:f.manifest,launcher:()=>[old,fresh][launches++] as never,killGraceMs:1});
  try {
    await runtime.spawn(f.config);
    await runtime.kill(f.config.tenantId);
    await runtime.spawn({...f.config,sessionId:'fresh-session'});
    old.emit('exit',0);
    expect(runtime.get(f.config.tenantId)?.sessionId).toBe('fresh-session');
    await runtime.send(f.config.tenantId,{type:'human',content:'hello after new'});
    expect(commands[1].at(-1)?.type).toBe('prompt');
  } finally {runtime.dispose();}
});
  it.each([false,true])('restores an existing v3 baseline after restart (sealed=%s)', async (sealed) => {
    const f = fixture();
    const commands: RpcRecord[] = [];
    const binding = f.config.computeCostBinding!;
    const { unsigned, snapshotSha } = boundedUsageSnapshot(binding, 'open');
    const artifactRef = `${binding.work_item_id}-${binding.binding_sha256}-baseline.json`;
    const proc = fakeProcess(
      f.config.sessionId,
      commands,
      () => ({ input: 0, output: 0, cacheRead: 0, cacheWrite: 0 }),
      {},
      (command, target) => {
        if (command.type === 'prompt' && command.message === '/runtime-usage-baseline') {
          emitRecord(target, {
            type: 'extension_ui_request', method: 'notify',
            message: `PULSE_USAGE_BASELINE/v1 ${JSON.stringify({ ok: true, receipt: {
              schema: 'pi_host_usage_capture_receipt/v1', work_item_id: binding.work_item_id,
              epoch: binding.epoch, lifecycle_state: 'open', binding_sha256: binding.binding_sha256,
              snapshot_sha256: snapshotSha, artifact_ref: artifactRef,
              ...(sealed?{idempotent_replay:true,canonical_lifecycle_state:'sealed'}:{}),
            } })}`,
          });
        }
      },
    );
    const runtime = createPiConversationRuntime({
      manifest: f.manifest, launcher: () => proc as never, requestTimeoutMs: 100,
    });
    f.config.runtimeSessionBinding={runtime:'pi',sessionId:f.config.sessionId};
    const usageDir=join(buildPiCommand(f.config,f.manifest).sessionDir,'compute-cost');
    mkdirSync(usageDir, { recursive: true, mode: 0o700 });
    writeFileSync(join(usageDir, artifactRef), JSON.stringify({ ...unsigned, snapshot_sha256: snapshotSha }), { mode: 0o600 });

    const session=await runtime.spawn(f.config);
    await runtime.activateRuntimeUsageBaseline!(f.config.tenantId);
    await runtime.send(f.config.tenantId,{type:'human',content:'explain after restart'});
    expect(commands).toContainEqual(expect.objectContaining({ type: 'prompt', message: '/runtime-usage-baseline' }));
    expect(session.state).toBe('busy');
    if(sealed){
      await expect(runtime.compact(f.config.tenantId)).rejects.toThrow('cannot compact while a turn is active');
      expect(commands.filter(c=>c.type==='prompt'&&c.message!=='/runtime-usage-baseline')).toHaveLength(1);
    }
    runtime.dispose();
  });


describe('WI568 Pi native model evidence through the runtime', () => {
  it('carries only same-turn native model usage and invalidates unobserved compaction', async () => {
    const f=fixture(); f.config.computeCostBinding=undefined;
    let usage={input:0,output:0,cacheRead:0,cacheWrite:0};
    const proc=fakeProcess(f.config.sessionId, [], ()=>usage);
    const runtime=createPiConversationRuntime({manifest:f.manifest,launcher:()=>proc as never,requestTimeoutMs:100});
    const responses: StreamJsonMessage[]=[];
    runtime.on('response',(_id,message)=>responses.push(message));
    await runtime.spawn(f.config);
    await runtime.send(f.config.tenantId,{type:'human',content:'synthetic'});
    emitRecord(proc,{type:'message_end',message:{role:'assistant',model:'actual-native-model',provider:'native-provider',
      usage:{input:100,output:10,cacheRead:20,cacheWrite:0,totalTokens:130},content:[]}});
    usage={input:100,output:10,cacheRead:20,cacheWrite:0};
    emitRecord(proc,{type:'agent_settled'}); await tick();
    expect(responses.at(-1)?.native_model_usage).toMatchObject({runtime:'pi',models:[{model:'actual-native-model',provider:'native-provider'}]});
    await runtime.send(f.config.tenantId,{type:'human',content:'synthetic next'});
    emitRecord(proc,{type:'auto_compaction_start'});
    emitRecord(proc,{type:'message_end',message:{role:'assistant',model:'next-model',provider:'native-provider',
      usage:{input:10,output:1,cacheRead:0,cacheWrite:0,totalTokens:11},content:[]}});
    usage={input:110,output:11,cacheRead:20,cacheWrite:0};
    emitRecord(proc,{type:'agent_settled'}); await tick();
    expect(responses.at(-1)?.native_model_usage).toBeUndefined();
    runtime.dispose();
  });
});


it('preserves the selected virtualenv and its dependency through a stable directory symlink', () => {
  const f = fixture();
  const venv = join(f.root, 'python-release');
  // Use the declared standalone host Python; CI bootstrap Python may need loader variables filtered by Pi.
  const standalonePython = process.env['PULSE_COMPUTE_COST_PYTHON']
    ?? (process.platform === 'linux' ? '/usr/bin/python3' : 'python3');
  execFileSync(standalonePython, ['-m', 'venv', '--without-pip', '--symlinks', venv], { timeout: 20000 });
  const python = join(venv, 'bin', 'python');
  const site = execFileSync(python, ['-I', '-c', 'import sysconfig; print(sysconfig.get_path("purelib"))'], { encoding: 'utf8' }).trim();
  writeFileSync(join(site, 'pulse_venv_dependency_for_test.py'), 'VALUE = "selected virtualenv"\n');
  const stable = join(f.root, 'active-python');
  symlinkSync(venv, stable, 'dir');
  f.manifest.profiles.canary!.computeCostHost!.python = join(stable, 'bin', 'python');
  const spec = buildPiCommand(f.config, f.manifest, {});
  // A later stable-pointer switch must not alter this already-selected environment.
  unlinkSync(stable);
  symlinkSync(f.workspace, stable, 'dir');
  const observed = JSON.parse(execFileSync(spec.env.PULSE_COMPUTE_COST_PYTHON!, ['-c',
    'import json,sys,pulse_venv_dependency_for_test as dep; print(json.dumps({"prefix":sys.prefix,"dependency":dep.VALUE}))'],
    { cwd: spec.cwd, env: spec.env, encoding: 'utf8' }));
  expect(observed).toEqual({ prefix: realpathSync(venv), dependency: 'selected virtualenv' });
}, 30000);

it.each(['interpreter', 'environment'] as const)('rejects an unreviewed Python %s even when the other path is allowed', (outside) => {
  const f = fixture();
  const unreviewed = mkdtempSync(join(tmpdir(), 'unreviewed-python-'));
  roots.push(unreviewed);
  const base = outside === 'interpreter' ? join(unreviewed, 'python') : join(f.root, 'pi');
  if (outside === 'interpreter') writeFileSync(base, '#!/bin/sh\nexit 0\n', { mode: 0o755 });
  const directory = join(outside === 'environment' ? unreviewed : f.root, 'environment', 'bin');
  mkdirSync(directory, { recursive: true });
  const requested = join(directory, 'python');
  symlinkSync(base, requested);
  f.manifest.profiles.hostNative = {
    ...f.manifest.profiles.canary!, kind: 'host-native', launcher: undefined,
    piBinary: join(f.root, 'pi'), toolchainPaths: [f.root],
    nodeResourceRoots: [f.root],
    computeCostHost: { ...f.manifest.profiles.canary!.computeCostHost!, python: requested },
  };
  f.config.conversationRuntime!.isolationProfile = 'hostNative';
  expect(() => validateConfiguredPiProfiles(f.manifest, ['hostNative'], { requireComputeCostHost: true }))
    .toThrow('outside the node resource allowlist');
  expect(() => buildPiCommand(f.config, f.manifest, {})).toThrow('outside the node resource allowlist');
});

it('retains public tool details when prompt acknowledgement and events share one stdout batch', async () => {
  const { WorkProcessView } = await import('../src/work-process-view.js');
  const f = fixture(); f.config.computeCostBinding = undefined;
  const commands: RpcRecord[] = [];
  const proc = fakeProcess(f.config.sessionId, commands,
    () => ({ input: 0, output: 0, cacheRead: 0, cacheWrite: 0 }), {}, command => {
      if (command.type !== 'prompt') return undefined;
      queueMicrotask(() => proc.stdout.write([
        { type: 'response', id: command.id, command: 'prompt', success: true },
        { type: 'tool_execution_start', toolName: 'read', toolCallId: 'batched-tool', args: { path: 'src/example.ts' } },
        { type: 'tool_execution_end', toolCallId: 'batched-tool', isError: false,
          result: { content: [{ type: 'text', text: 'public tool result' }] } },
      ].map(record => JSON.stringify(record)).join('\n')+'\n'));
      return false;
    });
  const runtime = createPiConversationRuntime({ manifest: f.manifest, launcher: () => proc as never, requestTimeoutMs: 100 });
  const frames: any[] = [];
  const view = new WorkProcessView('feishu:fixture', async record => { frames.push(structuredClone(record)); return 'message'; }, 0);
  const order: string[] = [];
  runtime.on('turn-accepted', () => {
    order.push('accepted'); view.begin(f.config.tenantId);
    void view.frame(f.config.tenantId, 'fixture-owner', 'status', 'accepted');
  });
  runtime.on('response', (_id, message) => {
    if (message.type !== 'work_process') return;
    order.push('detail');
    void view.event(f.config.tenantId, 'fixture-owner', message.process_event as any);
  });
  try {
    await runtime.spawn(f.config);
    await runtime.send(f.config.tenantId, { type: 'human', content: 'read the example' },
      { correlationId: 'turn:batch', idempotencyKey: 'turn:batch', effect: 'no-effect' });
    await tick(); await view.drain(f.config.tenantId);
    await view.callback(`wp:${view.idFor(f.config.tenantId)}:open`, 'fixture-owner', 'message');
    expect(order).toEqual(['accepted', 'detail', 'detail']);
    expect(frames.at(-1).events).toEqual([expect.objectContaining({
      title: 'read', state: 'completed', input: 'path: src/example.ts', output: 'public tool result',
    })]);
  } finally { runtime.dispose(); }
});

it('does not announce acceptance for a rejected prompt', async () => {
  const f = fixture(); f.config.computeCostBinding = undefined;
  const proc = fakeProcess(f.config.sessionId, [],
    () => ({ input: 0, output: 0, cacheRead: 0, cacheWrite: 0 }), {}, command => {
      if (command.type !== 'prompt') return undefined;
      queueMicrotask(() => emitRecord(proc, { type: 'response', id: command.id, command: 'prompt', success: false }));
      return false;
    });
  const runtime = createPiConversationRuntime({ manifest: f.manifest, launcher: () => proc as never, requestTimeoutMs: 100 });
  const accepted = vi.fn(); runtime.on('turn-accepted', accepted);
  try {
    await runtime.spawn(f.config);
    await expect(runtime.send(f.config.tenantId, { type: 'human', content: 'fixture' },
      { correlationId: 'turn:rejected', idempotencyKey: 'turn:rejected', effect: 'no-effect' })).rejects.toThrow('Pi RPC prompt failed');
    expect(accepted).not.toHaveBeenCalled();
  } finally { runtime.dispose(); }
});

// User's Codex-App sequence: public prose surrounds tools; only the settled tail is an answer.
it('retains every public Pi paragraph in order, including a standalone paragraph before a tool', async () => {
  const f = fixture(); f.config.computeCostBinding = undefined;
  const proc = fakeProcess(f.config.sessionId, [], () => ({ input: 0, output: 0, cacheRead: 0, cacheWrite: 0 }));
  const runtime = createPiConversationRuntime({ manifest: f.manifest, launcher: () => proc as never, requestTimeoutMs: 100 });
  const responses: StreamJsonMessage[] = [];
  runtime.on('response', (_id, event) => responses.push(event));
  await runtime.spawn(f.config);
  await runtime.send(f.config.tenantId, { type: 'human', content: 'check' }, {
    correlationId: 'public-sequence', idempotencyKey: 'public-sequence', hostAttemptRef: 'attempt-fixture', effect: 'no-effect' });
  const note = (text: string, tool = false) => emitRecord(proc, { type: 'message_end', message: { role: 'assistant',
    content: [{ type: 'thinking', thinking: 'NEVER_PUBLIC' }, { type: 'text', text },
      ...(tool ? [{ type: 'toolCall', id: 'tool-2', name: 'bash' }] : [])] } });
  note('先检查真实请求链路。');
  emitRecord(proc, { type: 'tool_execution_start', toolName: 'read', toolCallId: 'tool-1', args: { path: 'status.txt' } });
  emitRecord(proc, { type: 'tool_execution_end', toolCallId: 'tool-1', isError: false, result: { content: [{ type: 'text', text: 'OK' }] } });
  note('请求到达了运行时，继续核对认证。', true);
  emitRecord(proc, { type: 'tool_execution_start', toolName: 'bash', toolCallId: 'tool-2', args: { command: 'echo fixture' } });
  note('认证更新了，正在确认调用结果。');
  note('最终答复。');
  emitRecord(proc, { type: 'agent_settled' }); await tick();
  const work = responses.filter(event => event.type === 'work_process');
  expect(work.map(event => (event.process_event as any).kind)).toEqual(['commentary', 'operation', 'operation', 'commentary', 'operation', 'commentary']);
  expect(work.filter(event => (event.process_event as any).kind === 'commentary').map(event => (event.process_event as any).output))
    .toEqual(['先检查真实请求链路。', '请求到达了运行时，继续核对认证。', '认证更新了，正在确认调用结果。']);
  expect(work.every(event => event.correlation_id === 'public-sequence' && event.host_attempt_ref === 'attempt-fixture')).toBe(true);
  expect(JSON.stringify(responses)).not.toContain('NEVER_PUBLIC');
  expect(responses.filter(event => event.type === 'assistant').map(event => event.content)).toEqual(['最终答复。']);
  runtime.dispose();
});

it('retains Responses public summaries and explicit commentary without copying private replay or merging them into final', async () => {
  const f = fixture(); f.config.computeCostBinding = undefined;
  const proc = fakeProcess(f.config.sessionId, [], () => ({ input: 0, output: 0, cacheRead: 0, cacheWrite: 0 }));
  const runtime = createPiConversationRuntime({ manifest: f.manifest, launcher: () => proc as never, requestTimeoutMs: 100 });
  const responses: StreamJsonMessage[] = []; runtime.on('response', (_id, event) => responses.push(event));
  await runtime.spawn(f.config);
  await runtime.send(f.config.tenantId, { type: 'human', content: 'check' }, {
    correlationId: 'public-summary', idempotencyKey: 'public-summary', effect: 'no-effect' });
  emitRecord(proc, { type: 'message_end', message: { role: 'assistant', api: 'openai-codex-responses', content: [
    { type: 'thinking', thinking: 'PRIVATE_RAW_CONTAINER', thinkingSignature: JSON.stringify({
      id: 'rs_1', type: 'reasoning', summary: [{ type: 'summary_text', text: '公开的判断依据。' }],
      content: [{ type: 'reasoning_text', text: 'PRIVATE_RAW_REASONING' }], encrypted_content: 'PRIVATE_ENCRYPTED_REPLAY' }) },
    { type: 'text', text: '先核对认证，再测试调用。', textSignature: JSON.stringify({ v: 1, id: 'msg_1', phase: 'commentary' }) },
    { type: 'text', text: '最终答复。', textSignature: JSON.stringify({ v: 1, id: 'msg_2', phase: 'final_answer' }) },
  ] } });
  expect(responses.filter(event => event.type === 'assistant')).toHaveLength(0);
  expect(responses.filter(event => event.type === 'work_process').map(event => (event.process_event as any).output))
    .toEqual(['公开的判断依据。', '先核对认证，再测试调用。']);
  emitRecord(proc, { type: 'agent_settled' }); await tick();
  expect(responses.filter(event => event.type === 'assistant').map(event => event.content)).toEqual(['最终答复。']);
  expect(JSON.stringify(responses)).not.toContain('PRIVATE_');
  runtime.dispose();
});


it.each([false, true])('adds natural communication without replacing reviewed context (resume=%s)', resume => {
  const f = fixture();
  const spec = buildPiCommand({ ...f.config, resume, computeCostBinding: undefined }, f.manifest, {});
  const prompts = spec.args.filter((_arg, index) => spec.args[index - 1] === '--append-system-prompt');
  expect(prompts.filter(text => text === PUBLIC_PROGRESS_POLICY)).toHaveLength(1);
  expect(prompts).toContain(reviewedContext(f.contextPath));
  expect(spec.args).not.toContain('--system-prompt');
  expect(spec.args).toContain('--no-context-files');
  expect(PUBLIC_PROGRESS_POLICY).toContain("Use the user's language");
  expect(PUBLIC_PROGRESS_POLICY).toContain('Simple questions should be answered directly');
  expect(PUBLIC_PROGRESS_POLICY).toContain('Do not rewrite or expand provider reasoning summaries');
  expect(PUBLIC_PROGRESS_POLICY).not.toMatch(/阶段 [ABC]|每.{0,4}秒|always.*preamble/i);
});


it.each(['measurement_fact_conflict', 'measurement_transport_unavailable', 'measurement_authority_unavailable', 'command_hung'])(
  'keeps the same native conversation answering after %s, including restart', async (failure) => {
    const f=fixture(); f.config.runtimeSessionBinding={runtime:'pi',sessionId:f.config.sessionId};
    const original=JSON.stringify(f.config.computeCostBinding); const answers:string[]=[];
    const commands:RpcRecord[]=[]; const children:FakeProcess[]=[];
    const runtime=createPiConversationRuntime({manifest:f.manifest,requestTimeoutMs:25,turnTimeoutMs:1000,
      launcher:()=>{
        const child=fakeProcess(f.config.sessionId,commands,()=>({input:1,output:1,cacheRead:0,cacheWrite:0}),{},
          (command,proc)=>{
            if(command.message==='/runtime-usage-baseline'){
              if(failure==='command_hung')return false;
              emitRecord(proc,{type:'extension_ui_request',method:'notify',
                message:'PULSE_USAGE_BASELINE/v1 '+JSON.stringify({ok:false,error:{code:failure,retryable:failure==='measurement_transport_unavailable'}})});
            }else if(command.type==='prompt'){
              setImmediate(()=>{
                emitRecord(proc,{type:'message_end',message:{role:'assistant',content:[{type:'text',text:'Answer: '+command.message}],stopReason:'stop'}});
                emitRecord(proc,{type:'agent_settled'});
              });
            }
          }); children.push(child);return child as never;
      }});
    runtime.on('response',(_key,response)=>{if(response.type==='assistant')answers.push(String(response.content));});
    runtime.on('error',()=>{});
    try{
      for(let restart=0;restart<2;restart++){
        const proc=await runtime.spawn(f.config);
        expect(proc.sessionId).toBe(f.config.sessionId);
        for(const question of ['why did the WI fail?', 'keep my original scope']){
          await runtime.send(f.config.tenantId,{type:'human',content:question});
          await vi.waitFor(()=>expect(runtime.get(f.config.tenantId)?.state).toBe('idle'));
        }
        expect(children.at(-1)!.kill).not.toHaveBeenCalled();
        await runtime.kill(f.config.tenantId);
      }
      expect(answers).toEqual(['Answer: why did the WI fail?','Answer: keep my original scope',
        'Answer: why did the WI fail?','Answer: keep my original scope']);
      expect(JSON.stringify(f.config.computeCostBinding)).toBe(original);
      expect(commands.filter(c=>c.message==='/runtime-usage-baseline')).toHaveLength(2);
    }finally{runtime.dispose();}
  });


it('dispatches while the original measurement command remains pending', async () => {
  const f=fixture(); f.config.runtimeSessionBinding={runtime:'pi',sessionId:f.config.sessionId};
  const calls:RpcRecord[]=[];
  const child=fakeProcess(f.config.sessionId,calls,()=>({input:0,output:0,cacheRead:0,cacheWrite:0}),{},
    command=>command.message==='/runtime-usage-baseline' ? false : undefined);
  const runtime=createPiConversationRuntime({manifest:f.manifest,requestTimeoutMs:5000,launcher:()=>child as never});
  try{
    await runtime.spawn(f.config);
    await vi.waitFor(()=>expect(calls.some(c=>c.message==='/runtime-usage-baseline')).toBe(true));
    let sent=false;
    const sending=runtime.send(f.config.tenantId,{type:'human',content:'inspect the original WI'}).then(()=>{sent=true;});
    await vi.waitFor(()=>expect(sent).toBe(true),{timeout:250,interval:5});
    await sending;
    expect(calls.at(-1)?.message).toBe('inspect the original WI');
    expect(child.kill).not.toHaveBeenCalled();
  }finally{runtime.dispose();}
});

it.each([false,true])('restores only an existing native conversation when workspace control fails (binary missing=%s)', async (missing) => {
  const f=fixture();const root=realpathSync(f.root);
  f.manifest.profiles.canary={...f.manifest.profiles.canary!,kind:'host-native',launcher:undefined,piBinary:join(root,'pi'),
    workspaceRoots:[realpathSync(f.workspace)],nodeResourceRoots:[root,'/usr/bin','/bin'],toolchainPaths:['/usr/bin','/bin']};
  if (missing) {
    f.manifest.profiles.canary.piCodexOAuthHost = { extension: f.extensionPath, entrypoint: f.skillPath };
  }
  const unbound=buildPiCommand({...f.config,computeCostBinding:undefined},f.manifest,{});
  const sessionFile=join(unbound.sessionDir,`original_${f.config.sessionId}.jsonl`);
  const header=JSON.stringify({type:'session',version:3,id:f.config.sessionId,cwd:f.workspace,timestamp:'2026-09-22T00:00:00Z'})+'\n';
  writeFileSync(sessionFile,header);
  if(missing)unlinkSync(f.launcherPath);
  expect(()=>buildPiCommand(f.config,f.manifest,{})).toThrow();
  f.config.runtimeSessionBinding={runtime:'pi',sessionId:f.config.sessionId};
  const spec=missing ? buildPiDialogueCommand(f.config,f.manifest) : buildPiCommand(f.config,f.manifest,{});
  expect(spec.conversationOnly).toBe(true);
  expect(spec.args).toContain('--no-tools');
  if (missing) {
    expect(spec.args).toContain(realpathSync(f.extensionPath));
    expect(JSON.parse(spec.env.PULSE_PI_CODEX_OAUTH_HOST!)).toEqual({ entrypoint: realpathSync(f.skillPath) });
    expect(spec.env.PULSE_WORK_ITEM_CORE_HOST).toBeUndefined();
  } else {
    expect(spec.env.PULSE_PI_CODEX_OAUTH_HOST).toBeUndefined();
  }
  if(!missing)expect(spec.env.PULSE_PI_CONVERSATION_ONLY).toBe('1');
  expect(spec.args).not.toContain('--skill');
  expect(spec.args).not.toContain(reviewedContext(f.contextPath));
  expect(spec.args[spec.args.indexOf('--session')+1]).toBe(sessionFile);
  const commands:RpcRecord[]=[];
  const proc=fakeProcess(f.config.sessionId,commands,()=>({input:0,output:0,cacheRead:0,cacheWrite:0}));
  const runtime=createPiConversationRuntime({manifest:f.manifest,launcher:()=>proc as never});
  try{
    const live=await runtime.spawn(f.config);
    expect(live.conversationOnly).toBe(true);
    await runtime.send(f.config.tenantId,{type:'human',content:'what failed in the original request?'});
    expect(commands.at(-1)?.message).toBe('what failed in the original request?');
    expect(commands.some(c=>c.message==='/runtime-usage-baseline')).toBe(false);
    expect(readFileSync(sessionFile,'utf8')).toBe(header);
  }finally{runtime.dispose();}
});


it('missing measurement extension preserves the same process, tools and native entry', async () => {
  const f=fixture();const calls:RpcRecord[]=[];const specs:string[][]=[];const children:FakeProcess[]=[];
  const runtime=createPiConversationRuntime({manifest:f.manifest,requestTimeoutMs:100,
    launcher:(_command,args)=>{
      specs.push([...args]);
      const child=fakeProcess(f.config.sessionId,calls,()=>({input:0,output:0,cacheRead:0,cacheWrite:0}),{},
        (request,proc)=>{if(request.type==='get_commands'){
          emitRecord(proc,{type:'response',id:request.id,command:'get_commands',success:true,data:{commands:[]}});return false;
        }});children.push(child);return child as never;
    }});
  try{
    const live=await runtime.spawn(f.config);
    expect(live.conversationOnly).not.toBe(true);
    expect(live.sessionId).toBe(f.config.sessionId);
    expect(specs).toHaveLength(1);
    expect(specs[0]).not.toContain('--no-tools');
    expect(specs[0]).not.toContain('--no-builtin-tools');
    expect(specs[0]).toEqual(expect.arrayContaining(['--tools','read,bash']));
    expect(specs[0]).toContain('--extension');
    expect(specs[0]).toContain('--skill');
    await runtime.send(f.config.tenantId,{type:'human',content:'explain the original error'});
    expect(calls.at(-1)?.message).toBe('explain the original error');
    expect(children[0]!.kill).not.toHaveBeenCalled();
  }finally{runtime.dispose();}
});

it.each([false,true])('resumes an overwritten active v3 with its verified original baseline (retirement=%s)',async(sealed)=>{
 const f=fixture();const originBinding=f.config.computeCostBinding!;
 const {unsigned,snapshotSha}=boundedUsageSnapshot(originBinding,'open');
 const active=buildRuntimeUsageBinding({workItemId:originBinding.work_item_id,agentId:originBinding.agent_id,
   phase:originBinding.phase,epoch:originBinding.epoch,runtime:'pi',procurementSource:originBinding.procurement_source,
   authorityRef:originBinding.authority_ref,executionSurface:originBinding.execution_surface,issuedAt:'2026-09-22T12:21:06.265Z'});
 const reference={schema:'pulse_canonical_measurement_baseline_reference/v1' as const,status:'open' as const,
   work_item_id:active.work_item_id,agent_id:active.agent_id,epoch:active.epoch,phase:active.phase,runtime:'pi' as const,
   procurement_source:active.procurement_source,authority_ref:active.authority_ref!,execution_surface:active.execution_surface!,
   measurement_epoch_ref:'measurement_epoch_v1_'+'a'.repeat(64),baseline_binding_sha256:originBinding.binding_sha256,
   baseline_sha256:snapshotSha,active_runtime_binding_sha256:active.binding_sha256};
 f.config.computeCostBinding=active;f.config.measurementBaselineReference=reference;
 f.config.runtimeSessionBinding={runtime:'pi',sessionId:f.config.sessionId};
 const spec=buildPiCommand(f.config,f.manifest);
 expect(JSON.parse(spec.env.PULSE_CANONICAL_MEASUREMENT_BASELINE_REFERENCE!)).toEqual(reference);
 expect(JSON.parse(spec.env.PULSE_RUNTIME_USAGE_BINDING!)).toEqual(active);
 const artifactRef=`${active.work_item_id}-${originBinding.binding_sha256}-baseline.json`;
 const usageDir=join(spec.sessionDir,'compute-cost');mkdirSync(usageDir,{recursive:true,mode:0o700});
 const originalBytes=JSON.stringify({...unsigned,snapshot_sha256:snapshotSha});
 writeFileSync(join(usageDir,artifactRef),originalBytes,{mode:0o600});
 const commands:RpcRecord[]=[];
 const child=fakeProcess(f.config.sessionId,commands,()=>({input:0,output:0,cacheRead:0,cacheWrite:0}),{},(command,target)=>{
   if(command.type==='prompt'&&command.message==='/runtime-usage-baseline')emitRecord(target,{
     type:'extension_ui_request',method:'notify',message:`PULSE_USAGE_BASELINE/v1 ${JSON.stringify({ok:true,receipt:{
       schema:'pi_host_usage_capture_receipt/v1',work_item_id:active.work_item_id,epoch:active.epoch,lifecycle_state:'open',
       binding_sha256:originBinding.binding_sha256,active_runtime_binding_sha256:active.binding_sha256,
       snapshot_sha256:snapshotSha,artifact_ref:artifactRef,idempotent_replay:true,...(sealed?{canonical_lifecycle_state:'sealed'}:{})}})}`});
 });
 const adapter=createPiConversationRuntime({manifest:f.manifest,launcher:()=>child as never,requestTimeoutMs:100});
 try{
   const proc=await adapter.spawn(f.config);
   expect(proc.sessionId).toBe(f.config.sessionId);expect(proc.usageBindingSha256).toBe(active.binding_sha256);
   expect(proc.measurementBaselineReference).toEqual(reference);
   const proof=await adapter.activateRuntimeUsageBaseline!(f.config.tenantId);
   expect(proof).toMatchObject({baseline_digest:snapshotSha,measurement_origin_binding_sha256:originBinding.binding_sha256,
     active_runtime_binding_sha256:active.binding_sha256,...(sealed?{sealed:true}:{})});
   if(sealed){
     await expect(adapter.compact(f.config.tenantId)).resolves.toMatchObject({summary:'compacted'});
     await adapter.send(f.config.tenantId,{type:'human',content:'continue authorized work after measurement sealing'});
     expect(commands).toContainEqual(expect.objectContaining({type:'compact'}));
     await expect(adapter.compact(f.config.tenantId)).rejects.toThrow('while a turn is active');
   }else{
     await adapter.send(f.config.tenantId,{type:'human',content:'resume authorized work'});
     expect(commands).toContainEqual(expect.objectContaining({type:'prompt',message:'resume authorized work'}));
   }
   expect(readFileSync(join(usageDir,artifactRef),'utf8')).toBe(originalBytes);expect(f.config.computeCostBinding).toEqual(active);
 }finally{adapter.dispose();}
});


it.each(['pending','sealed','conversation-only'])('retains native usage while canonical measurement is %s', async mode => {
  const f=fixture();const commands:RpcRecord[]=[];
  let totals={input:100,output:20,cacheRead:30,cacheWrite:0};
  const child=fakeProcess(f.config.sessionId,commands,()=>totals);
  const runtime=createPiConversationRuntime({manifest:f.manifest,launcher:()=>child as never});
  const results:StreamJsonMessage[]=[];
  runtime.on('response',(_tenant,message)=>{if(message.type==='result')results.push(message);});
  try{
    const live=await runtime.spawn(f.config);
    live.usageBaselineActivated=false;
    live.usageSealed=mode==='sealed';
    live.conversationOnly=mode==='conversation-only';
    await runtime.send(f.config.tenantId,{type:'human',content:'explain the existing WI without side effects'});
    totals={input:105,output:22,cacheRead:33,cacheWrite:0};
    emitRecord(child,{type:'message_end',message:{role:'assistant',model:'claude-sonnet-4-5',provider:'anthropic',
      content:[{type:'text',text:'The existing request remains pending.'}],
      usage:{input:5,output:2,cacheRead:3,cacheWrite:0,totalTokens:10}}});
    emitRecord(child,{type:'agent_settled'});
    await vi.waitFor(()=>expect(results).toHaveLength(1));
    expect(results[0]).toMatchObject({usage_status:'observed_aggregate',usage:{input_tokens:5,
      output_tokens:2,cache_read_input_tokens:3,cache_creation_input_tokens:0}});
    expect(results[0]!.native_model_usage).toBeDefined();
    expect(live.usageBaselineActivated).toBe(false);
    expect(live.usageSealed).toBe(mode==='sealed');
    expect(commands.some(c=>c.message==='/runtime-usage-baseline'||c.message==='/runtime-usage-seal')).toBe(false);
  }finally{runtime.dispose();}
});

describe('operator-selected native WorkItem entrypoint',()=>{
  it('preflights original bound workspaces and transcripts without granting scope from persisted state',()=>{
    const f=fixture(), profile=f.manifest.profiles.canary!;
    Object.assign(profile,{kind:'host-native',launcher:undefined,piBinary:join(f.root,'pi'),
      toolchainPaths:[f.root],nodeResourceRoots:[f.root],contextFiles:[],
      workItemCoreHost:{extension:f.extensionPath,python:join(f.root,'pi'),entrypoint:f.skillPath}});
    const historical=join(f.root,'historical'); mkdirSync(historical);
    const workspace=join(historical,'original-wi'); mkdirSync(workspace);
    const poolKey='person:pulse:feishu', sessionId=f.config.sessionId;
    const sessionDir=join(f.sessionRoot,safeTenantDirectory(poolKey)); mkdirSync(sessionDir);
    const transcript=join(sessionDir,`original_${sessionId}.jsonl`);
    writeFileSync(transcript,JSON.stringify({type:'session',id:sessionId})+'\n');
    const configPath=join(f.root,'relay.json'),statePath=join(f.root,'state.json');
    writeFileSync(configPath,JSON.stringify({users:[{userId:'person',defaultAgent:'pulse',agents:[{
      id:'pulse',workspace:f.workspace,runtime:'pi',sessionId:'other-channel',
      conversationRuntime:f.config.conversationRuntime,allowedTools:['read','write','bash']}]}]}));
    const saved=JSON.stringify({sessions:{[poolKey]:{poolKey,agentId:'pulse',runtime:'pi',sessionId,
      activeWorkItemRuntimeBinding:{schema:'pulse_active_work_item_runtime_binding/v1',
        workItemId:'wi_20260822_730',sessionId,sessionRef:runtimeAuthorityRef(sessionId),runtime:'pi',workspace}}}});
    writeFileSync(statePath,saved);
    const check=()=>verifyRetainedPiConversations(configPath,statePath,f.manifest,{PULSE_SCL_SURFACE_ID:'surface_v1_muse'});
    expect(check).toThrow(/workspace.*allowlist/);
    profile.workspaceRoots.push(historical);
    expect(check()).toEqual({verified:1});
    expect(readFileSync(statePath,'utf8')).toBe(saved);
    expect(readFileSync(transcript,'utf8')).toContain(sessionId);
    unlinkSync(transcript);
    expect(check).toThrow(/transcript is unavailable/);
    // A configured but never-started channel has no history to resume.
    writeFileSync(statePath,JSON.stringify({sessions:{[poolKey]:{poolKey,agentId:'pulse',
      runtime:'pi',sessionId,turnCount:0}}}));
    expect(check()).toEqual({verified:1});
  });
  it('loads the native extension and pinned paths instead of the legacy intent tool',()=>{
    const f=fixture();
    const profile=f.manifest.profiles.canary!;
    delete f.config.computeCostBinding;
    Object.assign(profile,{kind:'host-native',launcher:undefined,piBinary:join(f.root,'pi'),
      toolchainPaths:[f.root],nodeResourceRoots:[f.root],contextFiles:[],
      workItemCoreHost:{extension:f.extensionPath,python:join(f.root,'pi'),entrypoint:f.skillPath}});
    const spec=buildPiCommand(f.config,f.manifest,{PULSE_WI_EXECUTION_CONTROL_ENABLED:'1',
      PULSE_SCL_SURFACE_ID:'surface_v1_muse', SCL_RELAY_HOST_CONTEXT:'untrusted ancestry',
      PULSE_WORK_ITEM_CORE_HOST:'untrusted ambient selection'});
    expect(spec.args).toContain(realpathSync(f.extensionPath));
    expect(spec.args.join(' ')).not.toContain('pi-scl-intent.mjs');
    expect(spec.env.PULSE_WORK_ITEM_CORE_HOST).toBe(JSON.stringify({
      extension:realpathSync(f.extensionPath),python:join(realpathSync(f.root),'pi'),entrypoint:realpathSync(f.skillPath),
    }));
    expect(spec.env.SCL_SESSION_REF).toBe(runtimeAuthorityRef(f.config.sessionId));
    expect(JSON.parse(spec.env.SCL_RELAY_HOST_CONTEXT!)).toEqual({
      schema_version:'scl_relay_host_context/v1',pid:process.pid,
      session_ref:runtimeAuthorityRef(f.config.sessionId),runtime_class:'resident_harness',
      surface_id:'surface_v1_muse',
    });
    expect(spec.env.PULSE_SCL_BIN).toBeUndefined();
    expect(()=>buildPiCommand(f.config,f.manifest,{})).toThrow('reviewed Relay Host surface');
    profile.workItemCoreHost!.entrypoint='/missing/unreviewed.pyz';
    expect(()=>buildPiCommand(f.config,f.manifest)).toThrow('native WI entrypoint');
  });
  it('native core ignores retired measurement admission but retains actual workspace restrictions',()=>{
    const f=fixture(), profile=f.manifest.profiles.canary!;
    Object.assign(profile,{kind:'host-native',launcher:undefined,piBinary:join(f.root,'pi'),
      toolchainPaths:[f.root],nodeResourceRoots:[f.root],contextFiles:[],
      workItemCoreHost:{extension:f.extensionPath,python:join(f.root,'pi'),entrypoint:f.skillPath}});
    // No old SCL operation or measurement extension may be required by this entry.
    profile.computeCostHost!.sclExecutable='/missing/retired-scl';
    const spec=buildPiCommand(f.config,f.manifest,{PULSE_SCL_SURFACE_ID:'surface_v1_muse'});
    expect(spec.args).toEqual(expect.arrayContaining(['--tools','read,bash,pulse_work_item,declare_session_wi']));
    expect(spec.args).not.toContain('--no-builtin-tools');
    expect(spec.args).not.toContain('--no-tools');
    expect(spec.env.PULSE_PI_MEASUREMENT_TOOLS).toBeUndefined();
    expect(spec.env.PULSE_SCL_BIN).toBeUndefined();
    expect(JSON.parse(spec.env.PULSE_WORK_ITEM_CORE_HOST!).entrypoint).toBe(realpathSync(f.skillPath));
    const outside=join(f.root,'outside'); mkdirSync(outside);
    expect(()=>buildPiCommand({...f.config,workspace:outside},f.manifest,
      {PULSE_SCL_SURFACE_ID:'surface_v1_muse'})).toThrow(/workspace.*allowlist/);
  });
  it('does not accept an ambient backend selector without operator profile configuration',()=>{
    const f=fixture();
    delete f.config.computeCostBinding;
    const spec=buildPiCommand(f.config,f.manifest,{PULSE_WORK_ITEM_CORE_HOST:'untrusted'});
    expect(spec.env.PULSE_WORK_ITEM_CORE_HOST).toBeUndefined();
  });
});


it('keeps the installer active pointer live across core upgrades in an existing Pi host',()=>{
  const f=fixture(); const profile=f.manifest.profiles.canary!;
  delete f.config.computeCostBinding;
  const core=join(f.root,'core'); mkdirSync(join(core,'releases','one'),{recursive:true});
  mkdirSync(join(core,'releases','two'),{recursive:true});
  for(const version of ['one','two'])writeFileSync(join(core,'releases',version,'harness.pyz'),version);
  symlinkSync('releases/one',join(core,'active'));
  Object.assign(profile,{kind:'host-native',launcher:undefined,piBinary:join(f.root,'pi'),
    toolchainPaths:[f.root],nodeResourceRoots:[f.root],contextFiles:[],
    workItemCoreHost:{extension:f.extensionPath,python:join(f.root,'pi'),
      entrypoint:join(core,'active','harness.pyz')}});
  const spec=buildPiCommand(f.config,f.manifest,{PULSE_SCL_SURFACE_ID:'surface_v1_muse'});
  const selected=JSON.parse(spec.env.PULSE_WORK_ITEM_CORE_HOST!);
  expect(selected.entrypoint).toBe(join(realpathSync(core),'active','harness.pyz'));
  expect(readFileSync(selected.entrypoint,'utf8')).toBe('one');
  unlinkSync(join(core,'active'));symlinkSync('releases/two',join(core,'active'));
  expect(readFileSync(selected.entrypoint,'utf8')).toBe('two');
});

it('dispatches with the Host allowlist while a baseline control request is unresolved', async () => {
  const f=fixture();const calls:RpcRecord[]=[];
  f.config.allowedTools=['read','edit'];
  const spec=buildPiCommand(f.config,f.manifest,{});
  expect(spec.args[spec.args.indexOf('--tools')+1]).toBe('read,edit');
  expect(spec.args).not.toContain('--no-builtin-tools');
  const child=fakeProcess(f.config.sessionId,calls,()=>({input:0,output:0,cacheRead:0,cacheWrite:0}),{},
    (request)=>request.message==='/runtime-usage-baseline'?false:undefined);
  const runtime=createPiConversationRuntime({manifest:f.manifest,requestTimeoutMs:5000,launcher:()=>child as never});
  try {
    await runtime.spawn(f.config);
    const pending=runtime.activateRuntimeUsageBaseline!(f.config.tenantId).catch(()=>undefined);
    await vi.waitFor(()=>expect(calls.some(c=>c.message==='/runtime-usage-baseline')).toBe(true));
    await Promise.race([
      runtime.send(f.config.tenantId,{type:'human',content:'perform the authorized edit'}),
      new Promise((_,reject)=>setTimeout(()=>reject(new Error('metering blocked dispatch')),300)),
    ]);
    expect(calls.at(-1)?.message).toBe('perform the authorized edit');
    await runtime.kill(f.config.tenantId);
    await pending;
  } finally {runtime.dispose();}
});
