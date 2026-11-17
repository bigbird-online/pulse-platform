// config-paths 回归：路径推导必须是**一处真源**，三个入口不得各写各的默认值。
//
// 债批次 1（2026-08-10，wi_20260810_949）。原状：index.ts 默认 '/opt/PULSE/data/relay.json'、
// provision-agent 默认 '$HOME/.relay/relay.json'、relay-jobs 默认 dist 相对 config/。
// live 靠 systemd 传 env 覆盖才收敛，裸跑发散。本测试钉三件事：
//   1. 优先级三段（显式 > env > 默认）在每个 resolver 上一致；
//   2. 默认值指向真实存在的仓内布局，不是 /opt/PULSE 这种不存在的前缀；
//   3. **三个入口源码里不得再出现自己的字面量默认路径**（源码级断言，防复制回来）。
// 第 3 条是本文件的价值所在——前两条只测 helper 自身，测不出「有人绕过 helper」。

import { describe, it, expect, beforeEach, afterEach } from 'vitest';
import { existsSync, mkdirSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { basename, dirname, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import {
  resolveConfigPath,
  resolveCronJobsPath,
  resolveMeteringDir,
  resolvePulseRoot,
  resolveCoevolutionRoot,
  resolveCodexAdditionalWritableRoots,
  resolveSharedRepoRoots,
  resolveSharedSsotRoot,
  sharedAccessPromptLines,
  sharedReadExclusions,
  COEVOLUTION_ROOT_ENV,
} from '../src/config-paths.js';

const SRC_DIR = resolve(dirname(fileURLToPath(import.meta.url)), '../src');

const ENV_KEYS = [
  'RELAY_CONFIG_PATH',
  'CRON_JOBS_PATH',
  'METERING_DIR',
  COEVOLUTION_ROOT_ENV,
] as const;
const saved: Record<string, string | undefined> = {};

beforeEach(() => {
  for (const k of ENV_KEYS) {
    saved[k] = process.env[k];
    delete process.env[k];
  }
});

afterEach(() => {
  for (const k of ENV_KEYS) {
    if (saved[k] === undefined) delete process.env[k];
    else process.env[k] = saved[k];
  }
});

describe('config-paths resolver 优先级', () => {
  const cases = [
    { name: 'relay.json', fn: resolveConfigPath, env: 'RELAY_CONFIG_PATH', leaf: 'relay.json' },
    { name: 'cron-jobs.json', fn: resolveCronJobsPath, env: 'CRON_JOBS_PATH', leaf: 'cron-jobs.json' },
    { name: 'metering dir', fn: resolveMeteringDir, env: 'METERING_DIR', leaf: 'metering/' },
  ] as const;

  for (const c of cases) {
    it(`${c.name}: 显式参数优先于 env 与默认值`, () => {
      process.env[c.env] = '/tmp/from-env';
      expect(c.fn('/tmp/explicit')).toBe('/tmp/explicit');
    });

    it(`${c.name}: 无显式参数时读 env`, () => {
      process.env[c.env] = '/tmp/from-env';
      expect(c.fn()).toBe('/tmp/from-env');
    });

    it(`${c.name}: 裸跑（无参无 env）落在仓内 data/ 且不含 /opt/PULSE`, () => {
      const got = c.fn();
      expect(got).toBe(join(resolvePulseRoot(), 'data', c.leaf));
      expect(got).not.toContain('/opt/PULSE');
    });
  }

  it('resolvePulseRoot() 指向同级模块仓根，不要求私有 data/ 随源码检出', () => {
    const root = resolvePulseRoot();
    expect(root).toBe(resolve(SRC_DIR, '..', '..'));
    expect(existsSync(join(root, 'Relay', 'package.json'))).toBe(true);
  });
});

describe('Codex 沙箱可写根（安全边界，非 canonical checkout 必须收窄）', () => {
  it('canonical <coevo>/Bigbird/PULSE 才自动上溯，否则收窄为 PULSE 根', () => {
    const pulseRoot = resolvePulseRoot();
    const parent = dirname(pulseRoot);
    const canonical = basename(pulseRoot) === 'PULSE' && basename(parent) === 'Bigbird';
    const expected = canonical ? resolve(parent, '..') : pulseRoot;
    expect(resolveCoevolutionRoot()).toBe(expected);
    if (canonical) {
      expect(resolvePulseRoot()).toBe(join(resolveCoevolutionRoot(), 'Bigbird', 'PULSE'));
    } else {
      expect(resolveCoevolutionRoot()).toBe(resolvePulseRoot());
    }
  });

  it('显式 PULSE_COEVOLUTION_ROOT 可为非 canonical 部署授权精确根', () => {
    process.env[COEVOLUTION_ROOT_ENV] = '/tmp/explicit-coevolution-root';
    expect(resolveCoevolutionRoot()).toBe('/tmp/explicit-coevolution-root');
  });

  it('codex runtime 不再自带该路径字面量', () => {
    const text = readFileSync(join(SRC_DIR, 'codex-conversation-runtime.ts'), 'utf-8');
    const code = text
      .split('\n')
      .filter((l) => !l.trimStart().startsWith('//') && !l.trimStart().startsWith('*'))
      .join('\n');
    expect(code).not.toContain("'/Users/owner/Coevolution'");
    expect(code).toContain('resolveCodexAdditionalWritableRoots()');
  });

  it('额外可写根：显式 env 取该根；canonical 取 monorepo 根；受管实例保持 PULSE 根；独立部署为空', () => {
    const pulseRoot = resolvePulseRoot();
    const parent = dirname(pulseRoot);
    const canonical = basename(pulseRoot) === 'PULSE' && basename(parent) === 'Bigbird';
    expect(resolveCodexAdditionalWritableRoots({ [COEVOLUTION_ROOT_ENV]: '/tmp/explicit-root' }))
      .toEqual(['/tmp/explicit-root']);
    if (canonical) {
      expect(resolveCodexAdditionalWritableRoots({})).toEqual([resolve(parent, '..')]);
    } else {
      expect(resolveCodexAdditionalWritableRoots({ PULSE_RELAY_PATHS: '/srv/pulse/config/relay/paths.json' }))
        .toEqual([pulseRoot]);
      expect(resolveCodexAdditionalWritableRoots({})).toEqual([]);
    }
  });
});

describe('共享 SSOT 只读根（所有会话可达，不改写域）', () => {
  const temps: string[] = [];
  afterEach(() => { for (const dir of temps.splice(0)) rmSync(dir, { recursive: true, force: true }); });
  function coevolution(): string {
    const root = mkdtempSync(join(tmpdir(), 'coevo-'));
    temps.push(root);
    mkdirSync(join(root, 'ssot'));
    mkdirSync(join(root, 'Bigbird', 'PULSE'), { recursive: true });
    mkdirSync(join(root, 'PARA', 'aki-space'), { recursive: true });
    return root;
  }

  it('Agent 工作区决定：Coevolution 树外的工作区即使设了 PULSE_COEVOLUTION_ROOT 也不列 SSOT', () => {
    const root = coevolution();
    process.env[COEVOLUTION_ROOT_ENV] = root;
    expect(resolveSharedSsotRoot()).toBe(join(root, 'ssot'));
    expect(resolveSharedSsotRoot('/home/pulse/Tenants/xb/cc-space')).toBeUndefined();
    expect(resolveSharedSsotRoot(join(root, 'PARA', 'aki-space'))).toBe(join(root, 'ssot'));
  });

  it('租户 Agent 的工作区同样列 SSOT 与共享仓库，其他租户目录照旧排除', () => {
    const root = coevolution();
    const xb = join(root, 'Bigbird', 'Oikos', 'tenants', 'xb', 'deux-space');
    mkdirSync(xb, { recursive: true });
    mkdirSync(join(root, 'Bigbird', 'Oikos', 'tenants', 'owner', 'bb-space'), { recursive: true });
    mkdirSync(join(root, 'Bigbird', 'Oikos', 'hestia-space'), { recursive: true });
    expect(resolveSharedSsotRoot(xb)).toBe(join(root, 'ssot'));
    expect(resolveSharedRepoRoots(xb)).toContain(root);
    const excluded = sharedReadExclusions(xb);
    expect(excluded).toContain(`${join(root, 'Bigbird', 'Oikos', 'tenants', 'owner')}/`);
    expect(excluded).not.toContain(`${join(root, 'Bigbird', 'Oikos', 'tenants', 'xb')}/`);
    expect(sharedAccessPromptLines(xb)[2]).toContain(join(root, 'ssot'));
    expect(resolveSharedSsotRoot(join(root, 'Bigbird', 'Oikos', 'tenants', 'owner', 'bb-space'))).toBe(join(root, 'ssot'));
    expect(resolveSharedSsotRoot(join(root, 'Bigbird', 'Oikos', 'hestia-space'))).toBe(join(root, 'ssot'));
  });

  it('release 目录运行时从 Agent 工作区上溯到 Coevolution 根，Codex 写根不变', () => {
    const root = coevolution();
    const writable = resolveCoevolutionRoot();
    expect(resolveSharedSsotRoot(join(root, 'PARA', 'aki-space'))).toBe(join(root, 'ssot'));
    expect(resolveCoevolutionRoot()).toBe(writable);
    expect(sharedAccessPromptLines(join(root, 'PARA', 'aki-space'))[2])
      .toBe(`- 规则正本：${join(root, 'ssot')}/（共享 SSOT，修改走自己分支的 PR）`);
  });

  it('共享仓库：Coevolution、PULSE、Oikos 中本节点存在的检出，只进读域', () => {
    const root = coevolution();
    const workspace = join(root, 'PARA', 'aki-space');
    const writable = resolveCoevolutionRoot();
    expect(resolveSharedRepoRoots(workspace)).toEqual([root, join(root, 'Bigbird', 'PULSE')]);
    mkdirSync(join(root, 'Bigbird', 'Oikos'));
    const repos = [root, join(root, 'Bigbird', 'PULSE'), join(root, 'Bigbird', 'Oikos')];
    expect(resolveSharedRepoRoots(workspace)).toEqual(repos);
    const [line] = sharedAccessPromptLines(workspace);
    expect(line).toBe(`- 共享仓库（只读）：${repos.map((repo) => `${repo}/`).join('、')}（本机共享检出；`
      + '工作区与可读写授权以外只能读，不得修改，不得在其中建 worktree；'
      + '要改代码，用 pulse_work_item 的 allocate_workspaces 申请稀疏工作区，或在自己分支的工作区内改）');
    expect(resolveCoevolutionRoot()).toBe(writable);
  });

  it('读取排除：凭据、.harness、scl-host、私有数据与非本租户目录，本工作区与授权路径不受排除', () => {
    const root = coevolution();
    const pulse = join(root, 'Bigbird', 'PULSE');
    const oikos = join(root, 'Bigbird', 'Oikos');
    const sclHost = join(pulse, 'data', 'agent-harness', 'scl-host');
    const archive = join(oikos, 'hestia-space', 'skills', 'mirror-community-activities', 'data', 'archive');
    const account = join(oikos, 'hestia-space', 'skills', 'aqara', 'skills', 'aqara-agent', 'assets', 'user_account.json');
    for (const dir of [join(root, '.harness'), join(pulse, '.harness'), sclHost, archive, dirname(account),
      join(oikos, 'tenants', 'owner', 'bb-space'), join(oikos, 'tenants', 'xb', 'deux-space')]) mkdirSync(dir, { recursive: true });
    writeFileSync(account, '{}');
    const aki = join(root, 'PARA', 'aki-space');
    const protectedPaths = [`${join(root, '.harness')}/`, `${join(pulse, '.harness')}/`, `${sclHost}/`, `${archive}/`, account,
      `${join(oikos, 'tenants', 'xb')}/`, `${join(oikos, 'tenants', 'owner')}/`];
    expect(sharedReadExclusions(aki).sort()).toEqual([...protectedPaths].sort());
    const line = sharedAccessPromptLines(aki)[1]!;
    expect(line.startsWith('- 共享仓库内不得读取（本会话工作区与授权路径不在此限）：凭据与令牌文件（.env*、*credential*、*token*、secrets.yaml*、*.key、*.pem'))
      .toBe(true);
    expect(line).toContain('其他 Agent 工作区的 .relay/、.local/');
    for (const path of protectedPaths) expect(line).toContain(path);
    expect(line).not.toContain(`${aki}/`);

    // 本租户、本工作区之内的条目与授权路径不受排除。
    const bb = sharedReadExclusions(join(oikos, 'tenants', 'owner', 'bb-space'));
    expect(bb).toContain(`${join(oikos, 'tenants', 'xb')}/`);
    expect(bb).not.toContain(`${join(oikos, 'tenants', 'owner')}/`);
    const hestia = sharedReadExclusions(join(oikos, 'hestia-space'), [sclHost]);
    expect(hestia).not.toContain(`${archive}/`);
    expect(hestia).not.toContain(account);
    expect(hestia).not.toContain(`${sclHost}/`);
    expect(hestia).toContain(`${join(root, '.harness')}/`);
  });

  it('局部 grant 不撤销祖先排除项：跨租户局部授权时同级私有目录仍被排除，整项覆盖才不列', () => {
    const root = coevolution();
    const pulse = join(root, 'Bigbird', 'PULSE');
    const xb = join(root, 'Bigbird', 'Oikos', 'tenants', 'xb');
    const sclHost = join(pulse, 'data', 'agent-harness', 'scl-host');
    const publicDocs = join(xb, 'deux-space', 'public-docs');
    for (const dir of [join(xb, 'private-records'), publicDocs, join(sclHost, 'codex-inputs'),
      join(root, '.harness', 'reeve-space')]) mkdirSync(dir, { recursive: true });
    const aki = join(root, 'PARA', 'aki-space');

    const partial = sharedReadExclusions(aki, [publicDocs, join(sclHost, 'codex-inputs'), join(root, '.harness', 'reeve-space')]);
    expect(partial).toContain(`${xb}/`);
    expect(partial).toContain(`${sclHost}/`);
    expect(partial).toContain(`${join(root, '.harness')}/`);
    const line = sharedAccessPromptLines(aki, [publicDocs])[1]!;
    expect(line).toContain('（本会话工作区与授权路径不在此限）');
    expect(line).toContain(`${xb}/`);

    // 授权覆盖整个租户或整项时才不列；本租户只按工作区判断。
    const whole = sharedReadExclusions(aki, [xb, sclHost]);
    expect(whole).not.toContain(`${xb}/`);
    expect(whole).not.toContain(`${sclHost}/`);
    expect(sharedReadExclusions(join(xb, 'deux-space'))).not.toContain(`${xb}/`);
    // 工作区位于排除项之内时排除项照列，靠「不在此限」放行本工作区。
    expect(sharedReadExclusions(join(root, '.harness', 'reeve-space'))).toContain(`${join(root, '.harness')}/`);
  });

  it('祖先只有 ssot/ 而无 Bigbird/PULSE 时不认作 Coevolution 根', () => {
    const root = mkdtempSync(join(tmpdir(), 'not-coevo-'));
    temps.push(root);
    mkdirSync(join(root, 'ssot'));
    mkdirSync(join(root, 'space'));
    expect(resolveSharedSsotRoot(join(root, 'space'))).toBeUndefined();
    expect(sharedAccessPromptLines(join(root, 'space'))).toEqual([]);
    expect(resolveSharedRepoRoots(join(root, 'space'))).toEqual([]);
  });
});

describe('三入口不得自带默认路径字面量（防止真源被复制回去）', () => {
  const entries = ['index.ts', 'provision-agent.ts', 'relay-jobs.ts'];

  for (const file of entries) {
    it(`${file}: 不出现 /opt/PULSE 字面量`, () => {
      const text = readFileSync(join(SRC_DIR, file), 'utf-8');
      // 注释里解释历史的行不算复活——只看非注释代码行。
      const codeLines = text
        .split('\n')
        .filter((l) => !l.trimStart().startsWith('//') && !l.trimStart().startsWith('*'));
      expect(codeLines.join('\n')).not.toContain('/opt/PULSE');
    });

    it(`${file}: 不自己拼 .relay/relay.json 或 ../config/cron-jobs.json 兜底`, () => {
      const text = readFileSync(join(SRC_DIR, file), 'utf-8');
      const codeLines = text
        .split('\n')
        .filter((l) => !l.trimStart().startsWith('//') && !l.trimStart().startsWith('*'));
      const code = codeLines.join('\n');
      expect(code).not.toContain('.relay/relay.json');
      expect(code).not.toContain('../config/cron-jobs.json');
    });

    it(`${file}: 若读这三个 env 键，必须经 config-paths 而非直接 process.env`, () => {
      const text = readFileSync(join(SRC_DIR, file), 'utf-8');
      const codeLines = text
        .split('\n')
        .filter((l) => !l.trimStart().startsWith('//') && !l.trimStart().startsWith('*'));
      const code = codeLines.join('\n');
      for (const key of ENV_KEYS) {
        expect(code).not.toContain(`process.env['${key}']`);
        expect(code).not.toContain(`process.env["${key}"]`);
      }
      expect(code).toContain("from './config-paths.js'");
    });
  }
});
