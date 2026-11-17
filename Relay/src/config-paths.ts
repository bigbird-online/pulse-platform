// Source-root resolution is for code and isolated development only.
import { existsSync, readdirSync, statSync } from 'node:fs';
import { basename, dirname, isAbsolute, join, relative, resolve, sep } from 'node:path';
import { fileURLToPath } from 'node:url';
import { managedRelayPath, RELAY_PATHS_ENV } from './managed-paths.js';

/** 环境变量名——三处入口共用同一批键，改名只改这里。 */
export const RELAY_CONFIG_PATH_ENV = 'RELAY_CONFIG_PATH';
export const CRON_JOBS_PATH_ENV = 'CRON_JOBS_PATH';
export const METERING_DIR_ENV = 'METERING_DIR';
export const COEVOLUTION_ROOT_ENV = 'PULSE_COEVOLUTION_ROOT';

/**
 * PULSE 仓根（<PULSE>，即 data/ 与 Relay/ 的父目录）。
 * 从本模块编译产物位置回推：<PULSE>/Relay/dist/config-paths.js → 上溯两层。
 * src 下 tsx 直跑时布局同形（<PULSE>/Relay/src/config-paths.ts），故同一算式成立。
 */
export function resolvePulseRoot(): string {
  const here = dirname(fileURLToPath(import.meta.url)); // <...>/Relay/{dist,src}
  return resolve(here, '..', '..'); // → <PULSE>
}

/**
 * Coevolution monorepo 根，用作 Codex 沙箱的额外可写根。
 *
 * 这是安全边界，不是普通路径。canonical 布局 `<coevo>/Bigbird/PULSE` 才允许
 * 自动上溯到 `<coevo>`；独立 clone / 临时 worktree 不满足该结构时，默认收窄为
 * PULSE 仓根，避免把 `/tmp`、`/home` 等宽父目录意外加入 `--add-dir`。
 * 非 canonical 部署若确需 monorepo 写域，必须显式设置 PULSE_COEVOLUTION_ROOT。
 */
export function resolveCoevolutionRoot(): string {
  const explicit = process.env[COEVOLUTION_ROOT_ENV];
  if (explicit) return resolve(explicit);

  const pulseRoot = resolvePulseRoot();
  const parent = dirname(pulseRoot);
  if (basename(pulseRoot) === 'PULSE' && basename(parent) === 'Bigbird') {
    return resolve(parent, '..');
  }
  return pulseRoot;
}

/**
 * Codex 沙箱的额外可写根（`--add-dir`）。
 *
 * 显式 PULSE_COEVOLUTION_ROOT 或 canonical 布局：取 resolveCoevolutionRoot()。
 * 受管实例（设了 PULSE_RELAY_PATHS）沿用既有取值（PULSE 仓根），生产节点行为不变。
 * 独立部署（以上皆无）不加额外写根，Codex 只写 Agent 工作区，不能改 Relay 自身代码。
 */
export function resolveCodexAdditionalWritableRoots(env: NodeJS.ProcessEnv = process.env): string[] {
  if (env[COEVOLUTION_ROOT_ENV]) return [resolve(env[COEVOLUTION_ROOT_ENV]!)];
  const pulseRoot = resolvePulseRoot();
  const parent = dirname(pulseRoot);
  if (basename(pulseRoot) === 'PULSE' && basename(parent) === 'Bigbird') return [resolve(parent, '..')];
  if (env[RELAY_PATHS_ENV] !== undefined) return [pulseRoot];
  return [];
}

function isDirectory(path: string): boolean {
  try { return statSync(path).isDirectory(); } catch { return false; }
}

/**
 * 共享 SSOT（Coevolution 根下的 `ssot/`），所有会话只读可达；写走各自分支的 PR。
 *
 * 先取 resolveCoevolutionRoot()（显式 PULSE_COEVOLUTION_ROOT 或 canonical 源码布局）。
 * 原生部署从 release 目录运行，不满足该布局，再从 Agent 工作区上溯，取第一个同时含
 * `ssot/` 与 `Bigbird/PULSE/` 的祖先。这里只决定读域；Codex 写根仍只由
 * resolveCoevolutionRoot 决定，不因此放宽。找不到时返回 undefined。
 */
export function resolveSharedSsotRoot(workspace?: string): string | undefined {
  if (!workspace) return [join(resolveCoevolutionRoot(), 'ssot')].find(isDirectory);
  // An Agent's own workspace decides: every workspace inside the Coevolution
  // tree sees its SSOT, a tenant's Agent included (大鸟 2026-11-15); other
  // tenants' directories stay excluded by sharedReadExclusions.
  let dir = resolve(workspace);
  for (;;) {
    if (isDirectory(join(dir, 'Bigbird', 'PULSE')) && isDirectory(join(dir, 'ssot'))) return join(dir, 'ssot');
    if (dirname(dir) === dir) return undefined;
    dir = dirname(dir);
  }
}

/**
 * 共享仓库的本机检出根：Coevolution、PULSE（`Bigbird/PULSE`）、Oikos（`Bigbird/Oikos`），只读可达。
 *
 * Coevolution 根即共享 SSOT 的父目录，可见范围与 SSOT 相同（resolveSharedSsotRoot）；
 * PULSE、Oikos 按 canonical 布局从该根推出，本节点上不存在的不列。各节点（ownermac、TCloud）
 * 走同一推导，不另设配置。这里只决定读域：CC 写入硬边界与 Codex 写根都不因此放宽。
 */
export function resolveSharedRepoRoots(workspace?: string): string[] {
  const ssot = resolveSharedSsotRoot(workspace);
  if (!ssot) return [];
  const coevolution = dirname(ssot);
  return [coevolution, join(coevolution, 'Bigbird', 'PULSE'), join(coevolution, 'Bigbird', 'Oikos')].filter(isDirectory);
}

/** 凭据与令牌文件的文件名模式；共享仓库内任何位置都不得读取。 */
export const CREDENTIAL_NAME_PATTERNS = ['.env*', '*credential*', '*token*', 'secrets.yaml*', '*.key', '*.pem'];

/** 共享仓库内的私有数据，相对 Coevolution 根；本节点上存在才列。 */
const PRIVATE_SHARED_PATHS: string[][] = [
  ['Bigbird', 'PULSE', 'data', 'agent-harness', 'scl-host'],
  ['Bigbird', 'Oikos', 'hestia-space', 'skills', 'mirror-community-activities', 'data', 'archive'],
  ['Bigbird', 'Oikos', 'hestia-space', 'skills', 'aqara', 'skills', 'aqara-agent', 'assets', 'user_account.json'],
];

function within(path: string, root: string): boolean {
  const rel = relative(root, path);
  return rel === '' || (rel !== '..' && !rel.startsWith(`..${sep}`) && !isAbsolute(rel));
}

/**
 * 共享仓库里不得读取的具体路径：各仓库根下的 `.harness/`、私有数据（PRIVATE_SHARED_PATHS）、
 * Oikos `tenants/` 下非本租户的目录。全部从 resolveSharedRepoRoots 推出，本节点上不存在的不列。
 * 本租户只按工作区判断。一条排除项只在被工作区或某条授权路径整个覆盖时才不列；
 * 授权或工作区只覆盖其中一部分时排除项照列，由提示里「本会话工作区与授权路径不在此限」开精确例外。
 */
export function sharedReadExclusions(workspace?: string, granted: string[] = []): string[] {
  const roots = resolveSharedRepoRoots(workspace);
  if (roots.length === 0) return [];
  const coevolution = roots[0]!;
  const home = workspace ? resolve(workspace) : undefined;
  const covering = [...(home ? [home] : []), ...granted.map((path) => resolve(path))];
  const covered = (path: string) => covering.some((root) => within(path, root));
  const tenantsDir = join(coevolution, 'Bigbird', 'Oikos', 'tenants');
  const tenants = isDirectory(tenantsDir)
    ? readdirSync(tenantsDir).sort().map((name) => join(tenantsDir, name)).filter(isDirectory)
      .filter((tenant) => !(home && within(home, tenant)))
    : [];
  const paths = [
    ...roots.map((root) => join(root, '.harness')).filter(isDirectory),
    ...PRIVATE_SHARED_PATHS.map((parts) => join(coevolution, ...parts)).filter((path) => existsSync(path)),
    ...tenants,
  ].filter((path) => !covered(path));
  return paths.map((path) => (isDirectory(path) ? `${path}/` : path));
}

/**
 * 会话提示里的共享仓库、读取排除与规则正本三行；CC 文件沙箱、Pi 系统提示、Codex 新线程与定时任务共用同一措辞。
 * 排除只是提示层约束：CC 的读取没有技术拦截。`granted` 是本会话的授权路径，不受排除。
 */
export function sharedAccessPromptLines(workspace?: string, granted: string[] = []): string[] {
  const ssot = resolveSharedSsotRoot(workspace);
  if (!ssot) return [];
  const repos = resolveSharedRepoRoots(workspace).map((root) => `${root}/`).join('、');
  const excluded = sharedReadExclusions(workspace, granted);
  return [
    `- 共享仓库（只读）：${repos}（本机共享检出；工作区与可读写授权以外只能读，不得修改，不得在其中建 worktree；`
      + '要改代码，用 pulse_work_item 的 allocate_workspaces 申请稀疏工作区，或在自己分支的工作区内改）',
    '- 共享仓库内不得读取（本会话工作区与授权路径不在此限）：'
      + `凭据与令牌文件（${CREDENTIAL_NAME_PATTERNS.join('、')}，同名的源代码与文档除外）；`
      + `其他 Agent 工作区的 .relay/、.local/${excluded.length ? `；${excluded.join('、')}` : ''}`,
    `- 规则正本：${ssot}/（共享 SSOT，修改走自己分支的 PR）`,
  ];
}

/** 会话提示里的一条常驻只读授权；CC、Pi 与定时任务共用同一措辞。 */
export function standingAccessPromptLine(path: string): string {
  return `- 常驻授权：${path}/（只读，内核登记的本 Agent 常驻授权，撤销前一直有效）`;
}

/** relay.json 路径：显式参数 → $RELAY_CONFIG_PATH → <PULSE>/data/relay.json */
export function resolveConfigPath(explicit?: string): string {
  return managedRelayPath('relayConfig', explicit ?? process.env[RELAY_CONFIG_PATH_ENV])
    ?? join(resolvePulseRoot(), 'data', 'relay.json');
}

/** cron-jobs.json 路径：显式参数 → $CRON_JOBS_PATH → <PULSE>/data/cron-jobs.json */
export function resolveCronJobsPath(explicit?: string): string {
  return managedRelayPath('cronConfig', explicit ?? process.env[CRON_JOBS_PATH_ENV])
    ?? join(resolvePulseRoot(), 'data', 'cron-jobs.json');
}

/** metering 目录：显式参数 → $METERING_DIR → <PULSE>/data/metering/ */
export function resolveMeteringDir(explicit?: string): string {
  return managedRelayPath('meteringDir', explicit ?? process.env[METERING_DIR_ENV])
    ?? join(resolvePulseRoot(), 'data', 'metering/');
}

/** Writable Relay state; never move the external runtime's HOME. */
export function resolveRelayDataDir(): string {
  return managedRelayPath('dataDir', process.env['RELAY_DATA_DIR'])
    ?? join(process.env['HOME'] ?? '', '.relay');
}
