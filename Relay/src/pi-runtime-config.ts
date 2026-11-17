import { isRelayInternalSecret } from './relay-secrets.js';
import { createHash, randomUUID } from 'node:crypto';
import { execFileSync, spawn as nodeSpawn, type ChildProcessWithoutNullStreams, type SpawnOptionsWithoutStdio } from 'node:child_process';
import { EventEmitter } from 'node:events';
import { existsSync, mkdirSync, readFileSync, realpathSync, statSync } from 'node:fs';
import { basename, isAbsolute, join, parse, relative, resolve, sep } from 'node:path';
import { StringDecoder } from 'node:string_decoder';
import { admitPiWorkspace, requirePiWorkspaceProtocol } from './pi-workspace-admission.js';
import type {
  ConversationProcess,
  ConversationRuntimeAdapter,
  ConversationSpawnConfig,
  ConversationStatusInfo,
} from './conversation-runtime.js';
import type {
  ConversationTurnContext,
  ConversationTurnEffect,
  StreamJsonMessage,
} from './types.js';
import { CANONICAL_MEASUREMENT_BASELINE_ENV, validateMeasurementBaselineReference, type CanonicalMeasurementBaselineReference } from './canonical-measurement-baseline.js';
import {
  RUNTIME_AUTHORITY_REF_ENV,
  RUNTIME_USAGE_BINDING_SCHEMA,
  runtimeAuthorityRef,
  runtimeUsageBindingEnvironment,
  type RuntimeUsageBinding,
} from './runtime-usage-binding.js';
import { PUBLIC_PROGRESS_POLICY } from './public-progress-policy.js';
import type { PiUsageCaptureEnvelope, RuntimeUsageBaselineReadback } from './runtime-usage-seal.js';
import { relayHostSessionRef } from './session-identity.js';
import { piSessionArgs } from './pi-session-file.js';
import { sharedAccessPromptLines, standingAccessPromptLine } from './config-paths.js';

export const PI_RUNTIME_MANIFEST_SCHEMA = 'relay_pi_runtime_manifest/v1';
const PI_HOST_VERSION_CHECK_TIMEOUT_MS = 30_000;

/**
 * `host-native` is a first-class Pi execution profile, not a fallback from an
 * unavailable sandbox.  The profile still has to name its workspace, session
 * root, toolchain and credential allowlist explicitly.
 */
export type PiIsolationKind = 'host-native' | 'container' | 'vm' | 'policy-sandbox';

export interface PiIsolationProfile {
  kind: PiIsolationKind;
  unattended: true;
  /** Required for externally isolated profiles; forbidden for host-native. */
  launcher?: {
    command: string;
    args?: string[];
  };
  piBinary: string;
  /**
   * The host-native command search path.  Keeping this explicit provides the
   * real resident toolchain without inheriting arbitrary service PATH state.
   */
  toolchainPaths?: string[];
  /**
   * Containment allowlist for task-supplied workspaces. This does not select a
   * checkout or require Relay to pre-create a worktree; that remains the
   * executing Agent's task-time decision.
   */
  workspaceRoots: string[];
  /**
   * Host-native, node-owned resource roots. The pinned Pi binary, explicit
   * toolchain, shared agent config, extensions, skills, fixed context files,
   * and ComputeCost host files must resolve inside this allowlist. Tenant
   * workspaces and writable session state remain governed separately.
   */
  nodeResourceRoots?: string[];
  sessionRoot: string;
  /**
   * Optional reviewed Pi configuration directory shared by host-native tenant
   * processes. HOME remains tenant-local; only PI_CODING_AGENT_DIR is shared,
   * so Pi's own auth.json locking and OAuth refresh remain single-source.
   */
  agentConfigDir?: string;
  envAllowlist: string[];
  extensions: string[];
  /** Required for WI-bound Pi sessions. Relay injects these reviewed host paths
   * before Pi starts so lifecycle capture uses the same bound epoch. */
  computeCostHost?: {
    extension: string;
    python: string;
    pythonPath: string;
    sclExecutable: string;
    sclHarnessRoot: string;
    sclKernelRoot: string;
  };
  /** Operator-selected daily WI backend; never selected by business input. */
  workItemCoreHost?: { extension: string; python: string; entrypoint: string };
  /** Reviewed Oracle provider binding, also usable with all conversation tools disabled. */
  piCodexOAuthHost?: { extension: string; entrypoint: string };
  skills: string[];
  contextFiles: string[];
  /** Optional filenames discovered from the matched workspace root down to the
   * concrete agent workspace. This keeps node-wide capability scalable while
   * preventing one agent's local instructions from leaking into another. */
  workspaceContextFileNames?: string[];
}

export interface PiRuntimeManifest {
  workItemCoreHost?: PiIsolationProfile['workItemCoreHost'];
  schemaVersion: typeof PI_RUNTIME_MANIFEST_SCHEMA;
  piVersion: string;
  piCommit: string;
  profiles: Record<string, PiIsolationProfile>;
}

export interface PiCommandSpec {
  conversationOnly?: boolean;
  command: string;
  args: string[];
  cwd: string;
  env: NodeJS.ProcessEnv;
  sessionDir: string;
  profileId: string;
}

type PiLauncher = (
  command: string,
  args: readonly string[],
  options: SpawnOptionsWithoutStdio,
) => ChildProcessWithoutNullStreams;

export interface PiConversationRuntimeOptions {
  manifest?: PiRuntimeManifest;
  launcher?: PiLauncher;
  now?: () => number;
  idleTimeoutMs?: number;
  cleanupIntervalMs?: number;
  requestTimeoutMs?: number;
  turnTimeoutMs?: number;
  killGraceMs?: number;
  usageSnapshotDir?: string;
}

export interface PiConversationRuntimeAdapter extends ConversationRuntimeAdapter {
  steer(tenantId: string, message: import('./types.js').StreamJsonMessage): Promise<void>;
  getNativeState(tenantId: string): Promise<Record<string, unknown>>;
  abortTurn(tenantId: string): Promise<void>;
  setModel(tenantId: string, model: import('./types.js').ConversationModelRoute): Promise<void>;
  listModels(tenantId: string): Promise<import('./conversation-runtime.js').ConversationRuntimeModelOption[]>;
  setThinkingLevel(tenantId: string, level: import('./types.js').ConversationReasoningLevel): Promise<void>;
  listThinkingLevels(tenantId: string): Promise<import('./types.js').ConversationReasoningLevel[]>;
  compact(tenantId: string, customInstructions?: string): Promise<Record<string, unknown>>;
  getNativeSessionStats(tenantId: string): Promise<Record<string, unknown>>;
  switchSession(tenantId: string, sessionPath: string): Promise<void>;
}

type PendingRequest = {
  resolve: (value: Record<string, unknown>) => void;
  reject: (error: Error) => void;
  timer: ReturnType<typeof setTimeout>;
  onSuccess?: () => void;
};

type UsageCounters = {
  input: number;
  output: number;
  cacheRead: number;
  cacheWrite: number;
};

export type PiSession = ConversationProcess & {
  child: ChildProcessWithoutNullStreams;
  profile: PiIsolationProfile;
  workspace: string;
  provider: string;
  model: string;
  reasoning: import('./types.js').ConversationReasoningLevel;
  sessionDir: string;
  /** Agent's `sessionWiGuard`: each new prompt carries the WI-declaration reminder. */
  sessionWiGuard?: boolean;
  decoder: StringDecoder;
  stdoutBuffer: string;
  stderrTail: string;
  pending: Map<string, PendingRequest>;
  activeTurn?: ConversationTurnContext;
  /** Native model/usage metadata only, no assistant text. */
  modelUsageMessages?: unknown[];
  nativeTurnFailure?: { code: string; message: string; detail?: string };
  pendingAssistantText: string;
  pendingAssistantEventId?: string;
  publicMessageSequence?: number;
  intentionalExit: boolean;
  settling: boolean;
  usageBinding?: RuntimeUsageBinding;
  measurementBaselineReference?:CanonicalMeasurementBaselineReference;
  usageBaselineActivated: boolean;
  usageBaselineActivation?: Promise<RuntimeUsageBaselineReadback>;
  usageSealed?: boolean;
  usageBaselineReadbacks: PiUsageCaptureEnvelope[];
  usageBaselineReadbackError?: Error;
  usageSealReadbacks: PiUsageCaptureEnvelope[];
  usageSealReadbackError?: Error;
};

function finiteCounter(value: unknown): number {
  return typeof value === 'number' && Number.isFinite(value) && value >= 0 ? value : 0;
}

export function usageCounters(payload: unknown): UsageCounters {
  const data = payload && typeof payload === 'object' ? payload as Record<string, unknown> : {};
  const tokens = data.tokens && typeof data.tokens === 'object' ? data.tokens as Record<string, unknown> : {};
  return {
    input: finiteCounter(tokens.input),
    output: finiteCounter(tokens.output),
    cacheRead: finiteCounter(tokens.cacheRead),
    cacheWrite: finiteCounter(tokens.cacheWrite),
  };
}

export function usageDelta(previous: UsageCounters, current: UsageCounters): UsageCounters {
  return {
    input: Math.max(0, current.input - previous.input),
    output: Math.max(0, current.output - previous.output),
    cacheRead: Math.max(0, current.cacheRead - previous.cacheRead),
    cacheWrite: Math.max(0, current.cacheWrite - previous.cacheWrite),
  };
}

export function safeTenantDirectory(tenantId: string): string {
  const prefix = tenantId.replace(/[^A-Za-z0-9_.-]/g, '_').slice(0, 48) || 'tenant';
  const digest = createHash('sha256').update(tenantId).digest('hex').slice(0, 12);
  return `${prefix}-${digest}`;
}

/** Read the same tenant transcript selected by the launcher, without spawning
 * a process, creating directories, or changing runtime authority. */
export function existingPiSessionFile(manifest:PiRuntimeManifest,profileId:string,tenantId:string,sessionId:string):string|undefined{
  const profile=validateProfile(validateManifest(manifest),profileId);
  const sessionRoot=existingRealPath(profile.sessionRoot,'session root');
  const sessionDir=join(sessionRoot,safeTenantDirectory(tenantId));
  if(!existsSync(sessionDir))return;
  if(!pathWithin(sessionRoot,realpathSync(sessionDir)))throw new Error('Pi session directory escaped its tenant root');
  const args=piSessionArgs(sessionDir,sessionId);
  return args[0]==='--session'?args[1]:undefined;
}

export function pathWithin(root: string, candidate: string): boolean {
  return candidate === root || candidate.startsWith(`${root}${sep}`);
}

export function existingRealPath(path: string, label: string): string {
  if (!isAbsolute(path) || !existsSync(path)) throw new Error(`Pi ${label} must be an existing absolute path`);
  return realpathSync(path);
}

// Resolve deployment directory aliases once, but preserve the executable name:
// Python uses the adjacent pyvenv.cfg to select its installed dependencies.
function existingPythonPath(path: string): string {
  existingRealPath(path, 'ComputeCost Python');
  return join(realpathSync(parse(path).dir), basename(path));
}

function pathWithinAny(roots: string[], candidate: string): boolean {
  return roots.some((root) => pathWithin(root, candidate));
}

type PiResourceScope =
  | { kind: 'host-native'; roots: string[] }
  | { kind: 'tenant'; workspace: string };

/** Startup and launch resolve reviewed resources through the same boundary.
 * Recheck at launch so a changed symlink cannot reuse an earlier admission. */
function reviewedPiResource(path: string, kind: 'extension' | 'skill', scope: PiResourceScope): string {
  const real = existingRealPath(path, kind);
  if (scope.kind === 'host-native') {
    if (!pathWithinAny(scope.roots, real)) {
      throw new Error(`Pi reviewed ${kind} is outside the node resource allowlist`);
    }
  } else if (!pathWithin(scope.workspace, real)) {
    throw new Error(`Pi reviewed ${kind}s must live inside the tenant workspace`);
  }
  return real;
}

function hostNativeNodeResourceRoots(profile: PiIsolationProfile): string[] {
  if (profile.kind !== 'host-native' || !profile.nodeResourceRoots?.length) {
    throw new Error('Pi host-native profile requires explicit node resource roots');
  }
  return [...new Set(profile.nodeResourceRoots.map((path) => {
    const real = existingRealPath(path, 'node resource root');
    if (!statSync(real).isDirectory()) throw new Error('Pi node resource root must be a directory');
    if (real === parse(real).root) throw new Error('Pi node resource root may not be a filesystem root');
    return real;
  }))];
}

export function validateManifest(manifest: PiRuntimeManifest | undefined): PiRuntimeManifest {
  if (!manifest) throw new Error('Pi runtime isolation manifest is not configured');
  if (manifest.schemaVersion !== PI_RUNTIME_MANIFEST_SCHEMA) throw new Error('Pi runtime manifest schema mismatch');
  if (typeof manifest.piVersion !== 'string' || manifest.piVersion.length > 64
    || !/^[0-9]+\.[0-9]+\.[0-9]+(?:-[0-9A-Za-z.-]+)?$/.test(manifest.piVersion)
    || typeof manifest.piCommit !== 'string' || !/^[a-f0-9]{40}$/.test(manifest.piCommit)) {
    throw new Error('Pi runtime manifest version or commit is invalid');
  }
  return manifest;
}

export function loadPiRuntimeManifest(path: string): PiRuntimeManifest {
  if (!isAbsolute(path)) throw new Error('Pi runtime manifest path must be absolute');
  const parsed = JSON.parse(readFileSync(path, 'utf8')) as PiRuntimeManifest;
  return validateManifest(parsed);
}

function assertComputeCostExtensionIsConditional(profile: PiIsolationProfile): void {
  const configured = profile.computeCostHost?.extension;
  if (!configured) return;
  const computeCostExtension = existsSync(configured) ? realpathSync(configured) : resolve(configured);
  if (profile.extensions.some((extension) => (
    existsSync(extension) ? realpathSync(extension) : resolve(extension)
  ) === computeCostExtension)) {
    throw new Error('Pi ComputeCost extension must not be configured as an always-on extension');
  }
}

/** Host ancestry is independent of either lifecycle implementation. */
function relayHostContext(sessionRef: string, sourceEnv: NodeJS.ProcessEnv): string | undefined {
  const surfaceId = sourceEnv['PULSE_SCL_SURFACE_ID']?.trim();
  const runtimeClass = sourceEnv['PULSE_SCL_RUNTIME_CLASS']?.trim() || 'resident_harness';
  if (!surfaceId || surfaceId.length > 128 || runtimeClass.length > 128
      || /[\0\r\n]/.test(surfaceId + runtimeClass)) return;
  return JSON.stringify({
    schema_version:'scl_relay_host_context/v1', pid:process.pid,
    session_ref:sessionRef, runtime_class:runtimeClass, surface_id:surfaceId,
  });
}

function reviewedNativeEntrypoint(path: string, roots: string[]): string {
  const target = existingRealPath(path, 'native WI entrypoint');
  // Retain the installer-owned active selection for the whole conversation.
  const entrypoint = basename(parse(path).dir) === 'active'
    ? join(realpathSync(join(parse(path).dir, '..')), 'active', basename(path))
    : target;
  for (const file of [entrypoint, target]) {
    if (!pathWithinAny(roots, file) || !statSync(file).isFile())
      throw new Error('Native WI resource is outside reviewed node files');
  }
  return entrypoint;
}

function reviewedPiCodexOAuth(profile: PiIsolationProfile): PiIsolationProfile['piCodexOAuthHost'] {
  const host = profile.piCodexOAuthHost;
  if (!host) return;
  if (profile.kind !== 'host-native') throw new Error('Codex OAuth requires a host-native Pi profile');
  const roots = hostNativeNodeResourceRoots(profile);
  return {
    extension: reviewedPiResource(host.extension, 'extension', {kind:'host-native', roots}),
    entrypoint: reviewedNativeEntrypoint(host.entrypoint, roots),
  };
}

export function reviewedWorkItemCore(profile: PiIsolationProfile): PiIsolationProfile['workItemCoreHost'] {
  const host = profile.workItemCoreHost;
  if (!host) return;
  if (profile.kind !== 'host-native') throw new Error('Native WI core requires a host-native profile');
  const roots = hostNativeNodeResourceRoots(profile);
  const extension = reviewedPiResource(host.extension, 'extension', {kind:'host-native', roots});
  const python = existingPythonPath(host.python);
  const entrypoint = reviewedNativeEntrypoint(host.entrypoint, roots);
  for (const path of [extension, python, realpathSync(python)]) {
    if (!pathWithinAny(roots,path) || !statSync(path).isFile())
      throw new Error('Native WI resource is outside reviewed node files');
  }
  if ((statSync(python).mode & 0o111) === 0) throw new Error('Native WI Python must be executable');
  return {extension, python, entrypoint};
}

export function validateProfile(manifest: PiRuntimeManifest, profileId: string): PiIsolationProfile {
  const profile = manifest.profiles[profileId];
  if (!profile) throw new Error(`Pi isolation profile not found: ${profileId}`);
  if (!['host-native', 'container', 'vm', 'policy-sandbox'].includes(profile.kind) || profile.unattended !== true) {
    throw new Error('Pi runtime profile kind or unattended policy is invalid');
  }
  if (!isAbsolute(profile.piBinary)) {
    throw new Error('Pi binary path must be absolute');
  }
  if (profile.workspaceRoots.length === 0) throw new Error('Pi isolation profile requires workspace roots');
  assertComputeCostExtensionIsConditional(profile);
  reviewedWorkItemCore(profile);
  reviewedPiCodexOAuth(profile);

  if (profile.kind === 'host-native') {
    if (profile.launcher) throw new Error('Pi host-native profile must execute its pinned Pi binary directly');
    if (!profile.toolchainPaths?.length) throw new Error('Pi host-native profile requires explicit toolchain paths');
    const nodeResourceRoots = hostNativeNodeResourceRoots(profile);
    for (const path of profile.toolchainPaths) {
      const real = existingRealPath(path, 'host toolchain path');
      if (!statSync(real).isDirectory()) throw new Error('Pi host toolchain path must be a directory');
      if (!pathWithinAny(nodeResourceRoots, real)) {
        throw new Error('Pi host toolchain path is outside the node resource allowlist');
      }
    }
    const piBinary = existingRealPath(profile.piBinary, 'host Pi binary');
    if (!pathWithinAny(nodeResourceRoots, piBinary)) {
      throw new Error('Pi host Pi binary is outside the node resource allowlist');
    }
    const piStat = statSync(piBinary);
    if (!piStat.isFile() || (piStat.mode & 0o111) === 0) throw new Error('Pi host binary must be an executable file');
    return profile;
  }

  if (!profile.launcher || !isAbsolute(profile.launcher.command)) {
    throw new Error('Pi isolation launcher path must be absolute');
  }
  if (profile.nodeResourceRoots !== undefined) {
    throw new Error('Pi node resource roots are only supported for host-native profiles');
  }
  const launcher = existingRealPath(profile.launcher.command, 'isolation launcher');
  const launcherStat = statSync(launcher);
  if (!launcherStat.isFile() || (launcherStat.mode & 0o111) === 0) {
    throw new Error('Pi isolation launcher must be an executable file');
  }
  const hostPiBinary = existsSync(profile.piBinary) ? realpathSync(profile.piBinary) : resolve(profile.piBinary);
  if (launcher === hostPiBinary || basename(launcher) === 'pi') {
    throw new Error('Pi isolation launcher may not execute the Pi binary directly on the host');
  }
  return profile;
}

/** Validate every configured Pi profile before Relay advertises readiness.
 *
 * Spawn-time validation is too late for durable WI entry: Harness may already
 * have prepared authority by then. This preflight checks host-native resources
 * and WI-bound ComputeCost paths while startup can still fail without mutating
 * any WorkItem lifecycle state.
 */
export function validateConfiguredPiProfiles(
  manifestInput: PiRuntimeManifest | undefined,
  profileIds: string[],
  options: { requireComputeCostHost: boolean },
): PiRuntimeManifest {
  const manifest=validateManifest(manifestInput);
  for(const profileId of [...new Set(profileIds)]){
    const profile=validateProfile(manifest,profileId);
    const nodeRoots=profile.kind==='host-native'?hostNativeNodeResourceRoots(profile):[];
    if(profile.kind==='host-native') {
      const scope: PiResourceScope = {kind: 'host-native', roots: nodeRoots};
      for(const path of profile.extensions)reviewedPiResource(path, 'extension', scope);
      for(const path of profile.skills)reviewedPiResource(path, 'skill', scope);
      verifyHostPiVersion(profile, existingRealPath(profile.piBinary,'host Pi binary'),manifest.piVersion);
    }
    for(const root of profile.workspaceRoots){
      const real=existingRealPath(root,'workspace root');
      if(!statSync(real).isDirectory())throw new Error('Pi workspace root must be a directory');
    }
    const sessionRoot=existingRealPath(profile.sessionRoot,'session root');
    if(!statSync(sessionRoot).isDirectory())throw new Error('Pi session root must be a directory');
    // Native identity was checked by validateProfile; metering cannot require a retired writer.
    if(profile.workItemCoreHost || !options.requireComputeCostHost)continue;
    const host=profile.computeCostHost;
    if(!host)throw new Error('WI-bound Pi runtime requires reviewed ComputeCost host configuration');
    const extension=existingRealPath(host.extension,'ComputeCost extension');
    const python=existingPythonPath(host.python);
    const pythonPath=existingRealPath(host.pythonPath,'ComputeCost Python path');
    const sclExecutable=existingRealPath(host.sclExecutable,'canonical SCL executable');
    const sclHarnessRoot=existingRealPath(host.sclHarnessRoot,'canonical SCL Harness root');
    const sclKernelRoot=existingRealPath(host.sclKernelRoot,'canonical SCL kernel root');
    if(!statSync(extension).isFile())throw new Error('Pi ComputeCost extension must be a file');
    if(!statSync(python).isFile()||(statSync(python).mode&0o111)===0){
      throw new Error('Pi ComputeCost Python must be executable');
    }
    if(!statSync(pythonPath).isDirectory())throw new Error('Pi ComputeCost Python path must be a directory');
    if(!statSync(sclExecutable).isFile()||(statSync(sclExecutable).mode&0o111)===0){
      throw new Error('Canonical SCL executable must be executable');
    }
    if(!statSync(sclHarnessRoot).isDirectory()||!statSync(sclKernelRoot).isDirectory()){
      throw new Error('Canonical SCL state roots must be directories');
    }
    if(profile.kind==='host-native'&&[
      extension,python,realpathSync(python),pythonPath,sclExecutable,sclHarnessRoot,sclKernelRoot,
    ].some(path=>!pathWithinAny(nodeRoots,path))){
      throw new Error('Pi ComputeCost host resource is outside the node resource allowlist');
    }
    if(profile.kind==='host-native')requirePiWorkspaceProtocol(sclExecutable);
  }
  return manifest;
}

function hostToolchainPath(profile: PiIsolationProfile): string {
  if (profile.kind !== 'host-native' || !profile.toolchainPaths?.length) {
    throw new Error('Pi host-native profile requires explicit toolchain paths');
  }
  return profile.toolchainPaths.map((path) => existingRealPath(path, 'host toolchain path')).join(':');
}

function hostNativeAgentConfigDir(profile: PiIsolationProfile, nodeResourceRoots: string[]): string | undefined {
  if (!profile.agentConfigDir) return undefined;
  if (profile.kind !== 'host-native') throw new Error('Pi shared agent config directory is only supported for host-native profiles');
  const source = existingRealPath(profile.agentConfigDir, 'shared agent config directory');
  if (!pathWithinAny(nodeResourceRoots, source)) {
    throw new Error('Pi shared agent config directory is outside the node resource allowlist');
  }
  const sourceStat = statSync(source);
  if (!sourceStat.isDirectory()) throw new Error('Pi shared agent config source must be a directory');
  if ((sourceStat.mode & 0o077) !== 0) throw new Error('Pi shared agent config directory must not be accessible by group or other');
  if (typeof process.getuid === 'function' && sourceStat.uid !== process.getuid()) {
    throw new Error('Pi shared agent config directory must be owned by the Relay user');
  }
  const auth = existingRealPath(join(source, 'auth.json'), 'shared auth file');
  if (!pathWithin(source, auth)) throw new Error('Pi shared auth file escaped its reviewed config directory');
  const authStat = statSync(auth);
  if (!authStat.isFile()) throw new Error('Pi shared auth source must be a file');
  if ((authStat.mode & 0o077) !== 0) throw new Error('Pi shared auth source must not be accessible by group or other');
  if (typeof process.getuid === 'function' && authStat.uid !== process.getuid()) {
    throw new Error('Pi shared auth source must be owned by the Relay user');
  }
  const parsed = JSON.parse(readFileSync(auth, 'utf8')) as unknown;
  if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) {
    throw new Error('Pi shared auth source must contain a JSON object');
  }
  return source;
}

function verifyHostPiVersion(profile: PiIsolationProfile, path: string, version: string): void {
  try {
    const output = execFileSync(path, ['--version'], {
      encoding: 'utf8',
      timeout: PI_HOST_VERSION_CHECK_TIMEOUT_MS,
      env: { PATH: hostToolchainPath(profile) },
      windowsHide: true,
    });
    const versionPattern = version.replaceAll('.', '\\.');
    if (!new RegExp(`(^|[^0-9])${versionPattern}([^0-9]|$)`).test(output)) {
      throw new Error('version mismatch');
    }
  } catch {
    throw new Error('Pi host binary version does not match the pin');
  }
}

function reviewedContextPaths(
  profile: PiIsolationProfile,
  workspaceRoot: string,
  workspace: string,
  nodeResourceRoots: string[],
): string[] {
  const paths = profile.contextFiles.map((path) => {
    const real = existingRealPath(path, 'context source');
    if (profile.kind === 'host-native' && !pathWithinAny(nodeResourceRoots, real)) {
      throw new Error('Pi context source is outside the node resource allowlist');
    }
    return real;
  });
  const relativeWorkspace = relative(workspaceRoot, workspace);
  const directories = [workspaceRoot];
  let current = workspaceRoot;
  for (const segment of relativeWorkspace.split(sep).filter(Boolean)) {
    current = join(current, segment);
    directories.push(current);
  }
  for (const name of profile.workspaceContextFileNames ?? []) {
    if (!name || name === '.' || name === '..' || basename(name) !== name) {
      throw new Error('Pi workspace context filename must be a plain filename');
    }
    for (const directory of directories) {
      const candidate = join(directory, name);
      if (!existsSync(candidate)) continue;
      const real = existingRealPath(candidate, 'workspace context source');
      if (!pathWithin(workspaceRoot, real)) throw new Error('Pi workspace context source escaped the workspace root');
      paths.push(real);
    }
  }
  return [...new Set(paths.map((path) => existingRealPath(path, 'context source')))];
}

function contextText(paths: string[]): string[] {
  let totalBytes = 0;
  return paths.map((real, index) => {
    const stats = statSync(real);
    if (!stats.isFile()) throw new Error('Pi context source must be a file');
    totalBytes += stats.size;
    if (stats.size > 64 * 1024 || totalBytes > 128 * 1024) throw new Error('Pi reviewed context sources exceed the size limit');
    const text = readFileSync(real, 'utf8');
    if (text.includes('\0')) throw new Error('Pi context source contains a NUL byte');
    const body = text.endsWith('\n') ? text : `${text}\n`;
    const source = index + 1;
    return `[Relay reviewed context ${source} begins: ${JSON.stringify(real)}]\n${body}[Relay reviewed context ${source} ends]\n`;
  });
}

export function buildPiCommand(
  config: ConversationSpawnConfig,
  manifestInput: PiRuntimeManifest | undefined,
  sourceEnv: NodeJS.ProcessEnv = process.env,
): PiCommandSpec {
  const manifest = validateManifest(manifestInput);
  const route = config.conversationRuntime;
  if (!route || route.runtime !== 'pi') throw new Error('Pi runtime requires a structured conversationRuntime route');
  if (!route.model?.provider || !route.model.model || !route.model.reasoning || !route.isolationProfile) {
    throw new Error('Pi route must explicitly set provider, model, reasoning, and isolationProfile');
  }
  const profile = validateProfile(manifest, route.isolationProfile);
  // The installed native core owns WI authority. A retained legacy metering
  // binding is historical evidence, never admission to another controller.
  // Do not mutate the persisted binding or infer a replacement one.
  if (profile.workItemCoreHost) {
    config = {...config, computeCostBinding: undefined, measurementBaselineReference: undefined};
  }
  const hostNative = profile.kind === 'host-native';
  const nodeResourceRoots = hostNative ? hostNativeNodeResourceRoots(profile) : [];
  let conversationOnly = false;
  let workspace = existingRealPath(config.workspace, 'workspace');
  const allowedRoots = profile.workspaceRoots
    .map((root) => existingRealPath(root, 'workspace root'))
    .filter((root) => pathWithin(root, workspace))
    .sort((left, right) => right.length - left.length);
  let workspaceRoot = allowedRoots[0];
  if (!workspaceRoot && !(hostNative && config.computeCostBinding)) {
    throw new Error('Pi workspace is outside the isolation profile allowlist');
  }

  const sessionRoot = existingRealPath(profile.sessionRoot, 'session root');
  const sessionDir = join(sessionRoot, safeTenantDirectory(config.tenantId));
  mkdirSync(sessionDir, { recursive: true, mode: 0o700 });
  const resolvedSessionDir = realpathSync(sessionDir);
  if (!pathWithin(sessionRoot, resolvedSessionDir)) throw new Error('Pi session directory escaped its tenant root');

  const env: NodeJS.ProcessEnv = {
    PATH: hostNative ? hostToolchainPath(profile) : '/usr/local/bin:/usr/bin:/bin',
  };
  for (const key of profile.envAllowlist) {
    if (!/^[A-Z][A-Z0-9_]*$/.test(key)) throw new Error('Pi environment allowlist contains an invalid key');
    if (isRelayInternalSecret(key)) throw new Error(`Pi environment allowlist names Relay-internal credential ${key}`);
    const value = sourceEnv[key];
    if (value !== undefined) env[key] = value;
  }
  env['PULSE_PI_TENANT_ID'] = config.tenantId;
  if(config.nativeHandoverRef) env['PULSE_NATIVE_HANDOVER_REF']=config.nativeHandoverRef;
  // The SCL client prefers this host-owned opaque ref over Pi's private
  // session identity. Direct Agent lifecycle calls and channel /wi control
  // therefore address the same Host attachment on Telegram and Feishu.
  env['SCL_SESSION_REF'] = relayHostSessionRef(config.sessionId);
  // Both ordinary recovery and WI-bound children use the same reviewed storage.
  // A usage binding must not be needed to locate the existing SCL kernel.
  const preserveStorageLocators = (sclHarnessRoot: string, sclKernelRoot: string): void => {
    // Deployment storage is a reviewed locator, never execution authority.
    // Keep it in the exact client invocation instead of borrowing process HOME.
    const configuredPulseHome = sourceEnv['PULSE_HARNESS_HOME'];
    if (hostNative && configuredPulseHome) {
      const pulseHome = existingRealPath(configuredPulseHome, 'configured PULSE home');
      // This is a placement hint for already-reviewed private state, not
      // permission to read the PULSE repository. The concrete SCL resources
      // above still have to pass the original node allowlist independently.
      const privateState = join(pulseHome, 'data', 'agent-harness');
      if (!statSync(pulseHome).isDirectory()
          || ![sclHarnessRoot, sclKernelRoot].some(path => pathWithin(privateState, path))) {
        throw new Error('Configured PULSE home does not describe reviewed private SCL state');
      }
      env['PULSE_HARNESS_HOME'] = pulseHome;
    }
    const configuredWorktrees = sourceEnv['HARNESS_WORKTREE_STATE_ROOT'];
    if (hostNative && configuredWorktrees) {
      const worktrees = existingRealPath(configuredWorktrees, 'configured worktree state root');
      if (!pathWithinAny(nodeResourceRoots, worktrees) || !statSync(worktrees).isDirectory()) {
        throw new Error('Configured worktree state root is outside reviewed Host resources');
      }
      env['HARNESS_WORKTREE_STATE_ROOT'] = worktrees;
    }
  };
  // Validate historical bindings without rewriting their signed attribution.
  const usageEnvironment = runtimeUsageBindingEnvironment(config.computeCostBinding, 'pi');
  if(config.measurementBaselineReference){
    if(!config.computeCostBinding)throw new Error('Canonical measurement origin requires a current runtime binding');
    env[CANONICAL_MEASUREMENT_BASELINE_ENV]=JSON.stringify(validateMeasurementBaselineReference(
      config.measurementBaselineReference,config.computeCostBinding));
  }
  const legacyUsageBinding = !!config.computeCostBinding
    && config.computeCostBinding.schema !== RUNTIME_USAGE_BINDING_SCHEMA;
  if (!legacyUsageBinding) Object.assign(env, usageEnvironment);
  let computeCostExtension: string | undefined;
  if (config.computeCostBinding) {
    const authorityRef = runtimeAuthorityRef(config.sessionId);
    if (
      config.computeCostBinding.authority_ref !== authorityRef
      || (!legacyUsageBinding && !config.computeCostBinding.execution_surface)
    ) {
      throw new Error('Pi ComputeCost binding does not match native session/surface authority');
    }
    const hostSurface = sourceEnv['PULSE_SCL_SURFACE_ID']?.trim();
    if (!hostNative && !legacyUsageBinding && hostSurface && hostSurface !== config.computeCostBinding?.execution_surface) {
      throw new Error('Pi ComputeCost binding does not match the current Host surface');
    }
    env[RUNTIME_AUTHORITY_REF_ENV] = authorityRef;
    if (!legacyUsageBinding) env['PULSE_RUNTIME_EXECUTION_SURFACE'] = config.computeCostBinding.execution_surface;
    const host = profile.computeCostHost;
    if (!host) throw new Error('WI-bound Pi runtime requires reviewed ComputeCost host configuration');
    computeCostExtension = existingRealPath(host.extension, 'ComputeCost extension');
    if (hostNative && !pathWithinAny(nodeResourceRoots, computeCostExtension)) {
      throw new Error('Pi ComputeCost extension is outside the node resource allowlist');
    }
    if (!hostNative && !pathWithin(workspace, computeCostExtension)) {
      throw new Error('Pi ComputeCost extension must live inside the tenant workspace');
    }
    const python = existingPythonPath(host.python);
    if (!statSync(python).isFile() || (statSync(python).mode & 0o111) === 0) {
      throw new Error('Pi ComputeCost Python must be executable');
    }
    if (hostNative && [python, realpathSync(python)].some(path => !pathWithinAny(nodeResourceRoots, path))) {
      throw new Error('Pi ComputeCost Python is outside the node resource allowlist');
    }
    const pythonPath = existingRealPath(host.pythonPath, 'ComputeCost Python path');
    if (!statSync(pythonPath).isDirectory()) {
      throw new Error('Pi ComputeCost Python path must be a directory');
    }
    if (hostNative && !pathWithinAny(nodeResourceRoots, pythonPath)) {
      throw new Error('Pi ComputeCost Python path is outside the node resource allowlist');
    }
    if (!hostNative && !pathWithin(workspace, pythonPath)) {
      throw new Error('Pi ComputeCost Python path must be a workspace directory');
    }
    const sclExecutable = existingRealPath(host.sclExecutable, 'canonical SCL executable');
    const sclHarnessRoot = existingRealPath(host.sclHarnessRoot, 'canonical SCL Harness root');
    const sclKernelRoot = existingRealPath(host.sclKernelRoot, 'canonical SCL kernel root');
    if (!statSync(sclExecutable).isFile() || (statSync(sclExecutable).mode & 0o111) === 0) {
      throw new Error('Canonical SCL executable must be executable');
    }
    if (!statSync(sclHarnessRoot).isDirectory() || !statSync(sclKernelRoot).isDirectory()) {
      throw new Error('Canonical SCL state roots must be directories');
    }
    if (hostNative && [sclExecutable, sclHarnessRoot, sclKernelRoot].some(
      path => !pathWithinAny(nodeResourceRoots, path),
    )) {
      throw new Error('Canonical SCL host resource is outside the node resource allowlist');
    }
    preserveStorageLocators(sclHarnessRoot, sclKernelRoot);
    if (hostNative) {
      try {
      workspaceRoot=admitPiWorkspace({
        executable:sclExecutable,harnessRoot:sclHarnessRoot,kernelRoot:sclKernelRoot,
        workItemId:config.computeCostBinding.work_item_id,
        agentId:config.computeCostBinding.agent_id,epoch:config.computeCostBinding.epoch,
        sessionRef:relayHostSessionRef(config.sessionId),workspace,
        worktreeStateRoot:env['HARNESS_WORKTREE_STATE_ROOT'],
        pulseHome:env['PULSE_HARNESS_HOME'],
        executionSurface:legacyUsageBinding?undefined:config.computeCostBinding.execution_surface,
      });
      } catch (error) {
        // A failed WI admission never grants tool access. An already existing
        // native conversation may still resume with all filesystem/effect tools
        // disabled by both --no-tools and the reviewed extension below.
        if (legacyUsageBinding || config.runtimeSessionBinding?.runtime !== 'pi'
          || config.runtimeSessionBinding.sessionId !== config.sessionId
          || piSessionArgs(resolvedSessionDir, config.sessionId)[0] !== '--session') throw error;
        conversationOnly = true;
        workspaceRoot = workspace;
        env['PULSE_PI_CONVERSATION_ONLY'] = '1';
      }
      workspace = workspaceRoot;
    }
    const snapshotDir = join(resolvedSessionDir, 'compute-cost');
    mkdirSync(snapshotDir, { recursive: true, mode: 0o700 });
    env['PULSE_COMPUTE_COST_PYTHON'] = python;
    env['PULSE_COMPUTE_COST_PYTHONPATH'] = pythonPath;
    env['PULSE_COMPUTE_COST_PI_SNAPSHOT_DIR'] = snapshotDir;
    env['PULSE_COMPUTE_COST_SCL_BIN'] = sclExecutable;
    env['PULSE_COMPUTE_COST_SCL_HARNESS_ROOT'] = sclHarnessRoot;
    env['PULSE_COMPUTE_COST_SCL_KERNEL_ROOT'] = sclKernelRoot;
    // The admitted WI supplies every ordinary child tool's context, not only
    // a privileged recovery probe. Harness still verifies each action grant.
    env['HARNESS_AGENT_ID'] = config.computeCostBinding.agent_id;
    env['HARNESS_WORK_ITEM_ID'] = config.computeCostBinding.work_item_id;
    env['HARNESS_ROOT'] = sclHarnessRoot;
    env['SCL_KERNEL_ROOT'] = sclKernelRoot;
    env['PULSE_SCL_BIN'] = sclExecutable;
    env['HARNESS_RUNTIME_CLASS'] = 'resident_harness';
    const executionSurface = config.computeCostBinding.execution_surface ?? hostSurface;
    if (executionSurface) env['HARNESS_SURFACE_ID'] = executionSurface;
    delete env['HARNESS_BRANCH_REF'];
    delete env['BROKER_TX_REF'];
    if (legacyUsageBinding) {
      // v2 has no execution surface or immutable baseline. Keep the original
      // session/workspace authority, but do not claim v3 bounded measurement.
      computeCostExtension = undefined;
    }
  }
  if (!workspaceRoot) throw new Error('Pi workspace admission is unavailable');
  const agentDir = join(resolvedSessionDir, 'agent');
  mkdirSync(agentDir, { recursive: true, mode: 0o700 });
  env['HOME'] = agentDir;
  env['PI_CODING_AGENT_DIR'] = hostNativeAgentConfigDir(profile, nodeResourceRoots) ?? agentDir;

  const nativeSessionArgs=piSessionArgs(resolvedSessionDir,config.sessionId);
  const piArgs = [
    '--mode', 'rpc',
    '--session-dir', resolvedSessionDir,
    ...nativeSessionArgs,
    '--provider', route.model.provider,
    '--model', route.model.model,
    '--thinking', route.model.reasoning,
    '--no-approve',
    '--no-extensions',
    '--no-skills',
    '--no-prompt-templates',
    '--no-themes',
    '--no-context-files',
  ];
  const tools = config.allowedTools?.map((tool) => tool.trim().toLowerCase()).filter(Boolean);
  // Workspace admission is the execution boundary. Metering must not hide
  // tools or require an extension to restore them after startup.
  if (conversationOnly) {
    piArgs.push('--no-tools');
  } else if (tools?.length) {
    const allowedTools = new Set(tools);
    // The WI core extension registers both tools; an allowlist that hid the
    // declaration would leave the session WI guard unable to ever pass.
    if (profile.workItemCoreHost) for (const tool of ['pulse_work_item', 'declare_session_wi']) allowedTools.add(tool);
    piArgs.push('--tools', [...allowedTools].join(','));
  }
  const nativeCore = conversationOnly ? undefined : reviewedWorkItemCore(profile);
  const codexOAuth = reviewedPiCodexOAuth(profile);
  delete env['PULSE_PI_CODEX_OAUTH_HOST'];
  if (codexOAuth) env['PULSE_PI_CODEX_OAUTH_HOST'] = JSON.stringify({entrypoint: codexOAuth.entrypoint});
  delete env['PULSE_PI_MEASUREMENT_TOOLS'];
  delete env['PULSE_WORK_ITEM_CORE_HOST'];
  if (nativeCore) {
    const context = relayHostContext(env['SCL_SESSION_REF']!, sourceEnv);
    if (!context) throw new Error('Native WI core requires a reviewed Relay Host surface');
    env['SCL_RELAY_HOST_CONTEXT'] = context;
    env['PULSE_WORK_ITEM_CORE_HOST'] = JSON.stringify(config.sessionWiGuard === true ? { ...nativeCore, sessionWiGuard: true } : nativeCore);
    piArgs.push('--append-system-prompt',
      'This conversation uses the installed native WorkItem core. Use the native section of work-item-lifecycle. '+
      'Use pulse_work_item for native list, inspect, resume and decisions; results return in this turn. '+
      'When unsure of an intent\'s input, read pulse_work_item inspect view guide with that intent first. '+
      'When a decision is required, review original decisions and ask only what remains undecided; never claim background activation. '+
      'Never use compatibility harness-scl, pulse_wi_continue or a legacy WI controller. '+
      'Authenticated /wi commands are executed by the command extension; explain its exact result without replaying it.');
  }
  const reviewedExtensions = [
    ...(nativeCore ? [nativeCore.extension] : []),
    ...(codexOAuth ? [codexOAuth.extension] : []),
    ...(conversationOnly ? [] : profile.extensions),
    ...(computeCostExtension ? [computeCostExtension] : []),
  ];
  const resourceScope: PiResourceScope = hostNative
    ? {kind: 'host-native', roots: nodeResourceRoots}
    : {kind: 'tenant', workspace};
  for (const extension of [...new Set(reviewedExtensions)]) {
    piArgs.push('--extension', reviewedPiResource(extension, 'extension', resourceScope));
  }
  for (const skill of conversationOnly ? [] : profile.skills) {
    piArgs.push('--skill', reviewedPiResource(skill, 'skill', resourceScope));
  }
  for (const text of conversationOnly ? [] : contextText(reviewedContextPaths(profile, workspaceRoot, workspace, nodeResourceRoots))) {
    piArgs.push('--append-system-prompt', text);
  }
  // Pi has no Relay file sandbox; this names the shared repositories and SSOT the same way CC's does.
  for (const line of conversationOnly ? [] : sharedAccessPromptLines(config.workspace, (config.sessionAccess ?? []).map((row) => row.path)))
    piArgs.push('--append-system-prompt', line.replace(/^- /, ''));
  for (const row of conversationOnly ? [] : config.sessionAccess ?? []) if (row.standing)
    piArgs.push('--append-system-prompt', standingAccessPromptLine(row.path).replace(/^- /, ''));
  if (conversationOnly) {
    piArgs.push('--append-system-prompt', 'Current WI workspace admission could not be verified. Continue answering in this original native conversation using its existing history. Do not access workspace files or execute effects; no WI execution authority is granted. Host intake may recover the original request. Measurement is unavailable, not a verified zero.');
  } else if(hostNative&&config.computeCostBinding){
    piArgs.push('--append-system-prompt',[
      'Harness managed_workspaces/v1 is the current WI directory contract, including when checkout instructions are older.',
      'The current working directory has exact registered admission; its parent and siblings are not thereby authorized.',
      'Before creating ANY additional WI linked worktree, obtain its exact path with:',
      `harness worktree allocation ${config.computeCostBinding.work_item_id} --agent ${config.computeCostBinding.agent_id} --repository-slug <org/repo> --json`,
      'Reuse an existing registered path. For a planned path, obtain capacity admission, create only at that allocation, then register before execution.',
      'Do not invent worktree roots or nest another repository worktree inside this WI directory. Allocation is read-only, not a creation or execution receipt.',
      'Do not relocate or clean historical directories during adoption. Reclaim only exact supported terminal-cleanup inventory with preservation checks and retirement receipts.',
    ].join('\n'));
  }

  // The pinned Pi CLI accumulates repeatable append-system-prompt arguments.
  // Keep reviewed persona/context and managed-WI instructions intact.
  piArgs.push('--append-system-prompt', PUBLIC_PROGRESS_POLICY);

  if (hostNative) {
    const piBinary = existingRealPath(profile.piBinary, 'host Pi binary');
    verifyHostPiVersion(profile, piBinary, manifest.piVersion);
    return {
      ...(conversationOnly ? {conversationOnly:true} : {}),
      command: piBinary,
      args: piArgs,
      cwd: workspace,
      env,
      sessionDir: resolvedSessionDir,
      profileId: route.isolationProfile,
    };
  }

  return {
    command: profile.launcher!.command,
    args: [
      ...(profile.launcher!.args ?? []),
      '--workspace', workspace,
      '--session-dir', resolvedSessionDir,
      '--isolation', profile.kind,
      '--expected-pi-version', manifest.piVersion,
      '--expected-pi-commit', manifest.piCommit,
      ...profile.envAllowlist.flatMap((key) => ['--env-key', key]),
      '--',
      profile.piBinary,
      ...piArgs,
    ],
    cwd: workspace,
    env,
    sessionDir: resolvedSessionDir,
    profileId: route.isolationProfile,
  };
}


/** Restore history, not WI authority, when control resources cannot be read. */
export function buildPiDialogueCommand(config:ConversationSpawnConfig, manifest:PiRuntimeManifest):PiCommandSpec {
  if (config.runtimeSessionBinding?.runtime !== 'pi'
    || config.runtimeSessionBinding.sessionId !== config.sessionId
    || config.computeCostBinding?.schema !== RUNTIME_USAGE_BINDING_SCHEMA
    || config.computeCostBinding.authority_ref !== runtimeAuthorityRef(config.sessionId)) {
    throw new Error('Original native conversation identity is unavailable');
  }
  runtimeUsageBindingEnvironment(config.computeCostBinding,'pi');
  const route=config.conversationRuntime;
  if (!route?.isolationProfile) throw new Error('Original Pi route is unavailable');
  const profileId=route.isolationProfile;
  const profile=validateProfile(manifest,profileId);
  if(profile.kind!=='host-native')throw new Error('No local native conversation fallback for this profile');
  const root=existingRealPath(profile.sessionRoot,'session root');
  const directory=existingRealPath(join(root,safeTenantDirectory(config.tenantId)),'original session directory');
  if(!pathWithin(root,directory)||piSessionArgs(directory,config.sessionId)[0]!=='--session'){
    throw new Error('Original native conversation transcript is unavailable');
  }
  const spec=buildPiCommand({...config,workspace:directory,computeCostBinding:undefined,
    allowedTools:[],measurementBaselineReference:undefined},
    {...manifest,profiles:{...manifest.profiles,[profileId]:{
      ...profile,workspaceRoots:[directory],computeCostHost:undefined,workItemCoreHost:undefined,
      extensions:[],skills:[],contextFiles:[],workspaceContextFileNames:[],
    }}},process.env);
  spec.args.push('--no-tools','--append-system-prompt',
    'Only the original conversation history is available. SCL/workspace readiness could not be verified; all tools are disabled. Answer questions and accept corrections normally, without claiming work execution, recovery, exact measurement or requesting a replacement WI/session.');
  return {...spec,conversationOnly:true};
}
