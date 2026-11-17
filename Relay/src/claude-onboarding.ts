import { execSync } from 'node:child_process';
import { relayChildEnv } from './relay-secrets.js';
import { readFileSync, writeFileSync, statSync, existsSync, chmodSync } from 'node:fs';
import { join } from 'node:path';

/** CC 2.x `--bare` records registry cwd as `/root` regardless of process chdir. */
export const CLAUDE_BARE_REGISTRY_CWD = '/root';

/**
 * CC 2.x 通过 ~/.claude.json 中若干关键字段判断是否需要展示 first-run wizard。
 * 当 PULSE 在云端用 systemd 启动 Relay 并以 PTY-bridge 方式 spawn `claude --bare`
 * 时，租户首次会话会卡在 theme picker 里——没有真人按方向键 + 回车，CC
 * 永远不会进入对话循环（参见 bugs/bug-20260509-001）。
 *
 * 这里只补「null/missing」的字段，已被用户主动设置的值（例如 theme: 'light'）
 * 一律保留；任何 IO 异常都吞掉并返回 changed=false，绝不阻断 spawn。
 */
export const CLAUDE_ONBOARDING_DEFAULTS = {
  hasCompletedOnboarding: true,
  theme: 'dark',
  tipsHistory: {} as Record<string, unknown>,
  hasIDEOnboardingBeenShown: true,
  customApiKeyResponses: {} as Record<string, unknown>,
  shiftEnterKeyBindingInstalled: true,
} as const;

export type ClaudeOnboardingResult = {
  path: string;
  changed: boolean;
  error?: Error;
};

function isMissingOrNull(v: unknown): boolean {
  return v === null || v === undefined;
}

/**
 * CC and Relay must update ~/.claude.json at runtime (projects trust, onboarding).
 * Some hosts mistakenly apply `chattr +i`; strip immutable best-effort before writes.
 */
export function ensureClaudeJsonWritable(home: string | undefined): void {
  if (!home || process.platform !== 'linux') return;
  const path = join(home, '.claude.json');
  if (!existsSync(path)) return;
  try {
    execSync(`chattr -i '${path.replace(/'/g, `'\\''`)}'`, { stdio: ['pipe', 'pipe', 'pipe'], env: relayChildEnv() });
  } catch { /* not immutable or chattr unavailable */ }
}

export function ensureClaudeOnboardingComplete(home: string | undefined): ClaudeOnboardingResult {
  if (!home) return { path: '', changed: false };
  ensureClaudeJsonWritable(home);
  const path = join(home, '.claude.json');

  let current: Record<string, unknown> = {};
  let fileExists = false;
  if (existsSync(path)) {
    fileExists = true;
    try {
      const raw = readFileSync(path, 'utf-8');
      const parsed = JSON.parse(raw) as unknown;
      if (parsed && typeof parsed === 'object' && !Array.isArray(parsed)) {
        current = parsed as Record<string, unknown>;
      }
    } catch (e) {
      return { path, changed: false, error: e as Error };
    }
  }

  let changed = false;
  for (const [key, defaultValue] of Object.entries(CLAUDE_ONBOARDING_DEFAULTS)) {
    if (isMissingOrNull(current[key])) {
      current[key] = defaultValue;
      changed = true;
    }
  }

  if (!changed) return { path, changed: false };

  try {
    writeFileSync(path, JSON.stringify(current, null, 2), { mode: 0o600 });
    if (fileExists) {
      try { chmodSync(path, 0o600); } catch { /* best-effort */ }
    }
    return { path, changed: true };
  } catch (e) {
    return { path, changed: false, error: e as Error };
  }
}

/**
 * 第三方 provider 认证走 `ANTHROPIC_AUTH_TOKEN`。
 * 切勿镜像到 `ANTHROPIC_API_KEY`：CC interactive 模式会弹出
 * "Detected a custom API key" TUI，PTY-bridge 无人应答导致阻塞。
 *
 * @deprecated 保留函数名以兼容旧调用点；现仅 strip API_KEY。
 */
export function mirrorAnthropicAuthTokenToApiKey(env: NodeJS.ProcessEnv): void {
  delete env['ANTHROPIC_API_KEY'];
}

/**
 * CC 2.x 在 cwd 加载 project-level CLAUDE.md 之前会检查
 * `~/.claude.json#projects[<cwd>]` 中的 trust 状态。常规交互模式下首次进入 cwd
 * 会弹 "Allow Claude Code to read files in this directory?" 让用户接受，写入
 * `hasTrustDialogAccepted: true`；`--bare` 模式下该 dialog 被静默跳过，导致 cwd
 * 永远没有 entry，CC 视其为「未受信项目」，跳过 CLAUDE.md / settings 的加载。
 *
 * 表现：用户问「你是谁」时 CC 用通用身份「Claude Code」作答，租户 Agent 身份
 * 完全失效（参见 bugs/bug-20260510-001）。
 *
 * 与 ensureClaudeOnboardingComplete 同款：只补 null/missing 字段；用户已显式
 * 设置（包括 hasTrustDialogAccepted: false 的明确拒绝）一律保留；IO 异常吞掉。
 */
export const CLAUDE_PROJECT_DEFAULTS = {
  allowedTools: [] as string[],
  mcpContextUris: [] as string[],
  mcpServers: {} as Record<string, unknown>,
  enabledMcpjsonServers: [] as string[],
  disabledMcpjsonServers: [] as string[],
  hasTrustDialogAccepted: true,
  hasCompletedProjectOnboarding: true,
  hasClaudeMdExternalIncludesApproved: true,
  hasClaudeMdExternalIncludesWarningShown: true,
  projectOnboardingSeenCount: 0,
} as const;

export function ensureClaudeProjectRegistered(
  home: string | undefined,
  cwd: string | undefined,
): ClaudeOnboardingResult {
  if (!home || !cwd) return { path: '', changed: false };
  ensureClaudeJsonWritable(home);
  const path = join(home, '.claude.json');

  let current: Record<string, unknown> = {};
  let fileExists = false;
  if (existsSync(path)) {
    fileExists = true;
    try {
      const raw = readFileSync(path, 'utf-8');
      const parsed = JSON.parse(raw) as unknown;
      if (parsed && typeof parsed === 'object' && !Array.isArray(parsed)) {
        current = parsed as Record<string, unknown>;
      }
    } catch (e) {
      return { path, changed: false, error: e as Error };
    }
  }

  let changed = false;
  let projects = current['projects'];
  if (!projects || typeof projects !== 'object' || Array.isArray(projects)) {
    projects = {} as Record<string, unknown>;
    current['projects'] = projects;
    changed = true;
  }
  const projectsMap = projects as Record<string, unknown>;

  let entry = projectsMap[cwd];
  if (!entry || typeof entry !== 'object' || Array.isArray(entry)) {
    entry = {} as Record<string, unknown>;
    projectsMap[cwd] = entry;
    changed = true;
  }
  const entryMap = entry as Record<string, unknown>;

  for (const [key, defaultValue] of Object.entries(CLAUDE_PROJECT_DEFAULTS)) {
    if (isMissingOrNull(entryMap[key])) {
      entryMap[key] = defaultValue;
      changed = true;
    }
  }

  if (!changed) return { path, changed: false };

  try {
    writeFileSync(path, JSON.stringify(current, null, 2), { mode: 0o600 });
    if (fileExists) {
      try { chmodSync(path, 0o600); } catch { /* best-effort */ }
    }
    return { path, changed: true };
  } catch (e) {
    return { path, changed: false, error: e as Error };
  }
}

/**
 * CC `--bare` interactive checks `projects['/root']` for trust — not the real workspace cwd.
 * Register alongside workspace trust (bug-20260510-001 follow-up).
 */
export function ensureClaudeBareRootTrust(home: string | undefined): ClaudeOnboardingResult {
  return ensureClaudeProjectRegistered(home, CLAUDE_BARE_REGISTRY_CWD);
}

/** Re-export utility used by tests / future consumers. */
export function readClaudeOnboardingState(home: string | undefined): Record<string, unknown> | null {
  if (!home) return null;
  const path = join(home, '.claude.json');
  if (!existsSync(path)) return null;
  try {
    const parsed = JSON.parse(readFileSync(path, 'utf-8')) as unknown;
    if (parsed && typeof parsed === 'object' && !Array.isArray(parsed)) {
      return parsed as Record<string, unknown>;
    }
  } catch { /* swallow */ }
  return null;
}

/** Allow tests to assert mtime didn't drift. */
export function tryStatMtimeMs(path: string): number | null {
  try { return statSync(path).mtimeMs; } catch { return null; }
}

/**
 * CC 2.x shows an interactive "Detected a custom API key … Do you want to use it?"
 * prompt whenever `ANTHROPIC_API_KEY` is set in the environment and the key suffix
 * is not recorded in `~/.claude.json#customApiKeyResponses.accepted` (or `.rejected`).
 *
 * In PTY-bridge (interactive) mode there is no human to press Enter, so CC stalls.
 * This function records the key suffix as "accepted" so CC skips the prompt.
 *
 * No-op when `apiKey` is empty or home is unset.
 */
/** CC compares env ANTHROPIC_API_KEY against several suffix forms in customApiKeyResponses. */
export function customApiKeySuffixVariants(apiKey: string): string[] {
  const variants = new Set<string>();
  variants.add(apiKey);
  if (apiKey.length > 20) variants.add(apiKey.slice(-20));
  const dot = apiKey.lastIndexOf('.');
  if (dot >= 0 && dot < apiKey.length - 1) {
    variants.add(apiKey.slice(dot + 1));
  }
  return [...variants];
}

export function recordCustomApiKeyAccepted(home: string | undefined, apiKey: string | undefined): void {
  if (!home || !apiKey) return;
  ensureClaudeJsonWritable(home);
  const variants = customApiKeySuffixVariants(apiKey);

  const path = join(home, '.claude.json');
  let current: Record<string, unknown> = {};
  if (existsSync(path)) {
    try {
      const raw = readFileSync(path, 'utf-8');
      const parsed = JSON.parse(raw) as unknown;
      if (parsed && typeof parsed === 'object' && !Array.isArray(parsed)) {
        current = parsed as Record<string, unknown>;
      }
    } catch { return; }
  }

  let responses = current['customApiKeyResponses'];
  if (!responses || typeof responses !== 'object' || Array.isArray(responses)) {
    responses = {};
    current['customApiKeyResponses'] = responses;
  }
  const map = responses as Record<string, unknown>;

  let accepted = map['accepted'];
  if (!Array.isArray(accepted)) {
    accepted = [];
    map['accepted'] = accepted;
  }
  const acceptedList = accepted as string[];

  let rejected = map['rejected'];
  if (!Array.isArray(rejected)) {
    rejected = [];
    map['rejected'] = rejected;
  }
  const rejectedList = rejected as string[];

  let changed = false;
  for (const suffix of variants) {
    if (!acceptedList.includes(suffix)) {
      acceptedList.push(suffix);
      changed = true;
    }
    const rejIdx = rejectedList.indexOf(suffix);
    if (rejIdx >= 0) {
      rejectedList.splice(rejIdx, 1);
      changed = true;
    }
  }

  if (!changed) return;

  try {
    writeFileSync(path, JSON.stringify(current, null, 2), { mode: 0o600 });
  } catch { /* best-effort */ }
}
