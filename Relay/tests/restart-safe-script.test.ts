// pulse-relay-restart-safe.sh 的行为回归（债批次 1 / wi_20260810_949，2026-08-10）。
//
// 这个脚本是「重启纪律」的载体：它判错一次，就会在有会话正在回复时重启 Relay，
// SIGTERM 掉 CC 子进程 → 还没落 jsonl 的回复丢失（memo 2026-06-24 红线）。
// 所以它的两条性质必须有机器闸钉住，而不是靠人记得：
//   1. **端口单一真源**：不写死 8787；取值序 = 调用方 env → relay.env → 报错退出。
//      写死兜底的坏处不是「不优雅」，是探测会打到一个没人监听的端口。
//   2. **fail-closed**：探不到状态时必须拒绝（exit 3），绝不能报「无 busy」放行。
//      2026-08-10 实测原实现在这里是 fail-open：--show-error 把 curl 错误文本混进
//      $http_response，哨兵串比对失配 → 闸整条跳过 → 死 Relay 也报 "OK: no busy" 退 0。
//
// 全部用临时 HOME + 死端口跑，不碰 live relay.service，也不会真的重启任何东西
// （所有断言都停在 restart 之前的判定分支上）。

import { describe, it, expect, beforeAll, afterAll } from 'vitest';
import { spawnSync } from 'node:child_process';
import { chmodSync, existsSync, mkdtempSync, mkdirSync, readFileSync, writeFileSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const SCRIPT = resolve(
  dirname(fileURLToPath(import.meta.url)),
  '../scripts/pulse-relay-restart-safe.sh',
);

let home: string;
let mockBin: string;
let systemctlLog: string;
let launchctlLog: string;

/** 跑脚本：env 全清（env -i 语义），只给必要键，避免宿主 RELAY_HTTP_PORT 泄漏进来。 */
function run(args: string[], env: Record<string, string> = {}) {
  const res = spawnSync('bash', [SCRIPT, ...args], {
    env: {
      PATH: process.env['PATH'] ?? '',
      HOME: home,
      USER: 'vitest',
      // Existing restart-order cases specify the Linux contract. Darwin is
      // covered explicitly below so the suite is host-independent.
      RELAY_RESTART_PLATFORM: 'Linux',
      // actions/setup-python installs a dynamically linked interpreter at the
      // front of PATH. Clearing its library path makes the fixture test a
      // broken interpreter rather than the restart wrapper.
      LD_LIBRARY_PATH: process.env['LD_LIBRARY_PATH'] ?? '',
      ...env,
    },
    encoding: 'utf-8',
  });
  return { code: res.status, out: (res.stdout ?? '') + (res.stderr ?? '') };
}

function writeRelayEnv(contents: string) {
  writeFileSync(join(home, '.relay', 'relay.env'), contents);
}

/** 用假的 curl/systemctl 走到 restart 入口，但只记命令，不接触 live systemd。 */
function runWithMocks(
  args: string[],
  curlResponse: string,
  curlExit = '0',
  extraEnv: Record<string, string> = {},
) {
  writeFileSync(systemctlLog, '');
  writeFileSync(launchctlLog, '');
  const launchctlFailMarker = join(home, 'launchctl-fail-once.marker');
  const launchctlPrintMarker = join(home, 'launchctl-print.marker');
  rmSync(launchctlFailMarker, { force: true });
  rmSync(launchctlPrintMarker, { force: true });
  const result = run(args, {
    PATH: `${mockBin}:${process.env['PATH'] ?? ''}`,
    MOCK_CURL_RESPONSE: curlResponse,
    MOCK_CURL_EXIT: curlExit,
    MOCK_SYSTEMCTL_LOG: systemctlLog,
    MOCK_LAUNCHCTL_LOG: launchctlLog,
    MOCK_LAUNCHCTL_FAIL_MARKER: launchctlFailMarker,
    MOCK_LAUNCHCTL_PRINT_MARKER: launchctlPrintMarker,
    RELAY_LAUNCHD_BOOTSTRAP_RETRY_DELAY_SEC: '0',
    RELAY_LAUNCHD_UNLOAD_RETRY_DELAY_SEC: '0',
    ...extraEnv,
  });
  return {
    ...result,
    systemctl: readFileSync(systemctlLog, 'utf-8'),
    launchctl: readFileSync(launchctlLog, 'utf-8'),
    launchctlPrintAttempts: existsSync(launchctlPrintMarker)
      ? readFileSync(launchctlPrintMarker, 'utf-8')
      : '0',
  };
}

beforeAll(() => {
  home = mkdtempSync(join(tmpdir(), 'restart-safe-'));
  mkdirSync(join(home, '.relay'), { recursive: true });
  mockBin = join(home, 'bin');
  systemctlLog = join(home, 'systemctl.log');
  launchctlLog = join(home, 'launchctl.log');
  mkdirSync(mockBin);
  writeFileSync(
    join(mockBin, 'curl'),
    `#!/bin/bash
required="\${MOCK_REQUIRED_TOKEN:-}"
if [[ -n "$required" ]]; then
  seen=false
  while (($#)); do
    if [[ "$1" == "-H" && "\${2:-}" == "x-relay-token: $required" ]]; then
      seen=true
      break
    fi
    shift
  done
  $seen || exit 22
fi
printf "%s" "\${MOCK_CURL_RESPONSE:-}"
exit "\${MOCK_CURL_EXIT:-0}"
`,
  );
  writeFileSync(
    join(mockBin, 'systemctl'),
    '#!/bin/bash\nprintf "%s\\n" "$*" >> "$MOCK_SYSTEMCTL_LOG"\n',
  );
  writeFileSync(
    join(mockBin, 'launchctl'),
    `#!/bin/bash
if [[ "\${1:-}" == "print" ]]; then
  count=0
  [[ -r "$MOCK_LAUNCHCTL_PRINT_MARKER" ]] && count="$(cat "$MOCK_LAUNCHCTL_PRINT_MARKER")"
  if [[ "$count" -lt "\${MOCK_LAUNCHCTL_PRINT_BUSY_ATTEMPTS:-0}" ]]; then
    count=$((count + 1))
    printf '%s' "$count" > "$MOCK_LAUNCHCTL_PRINT_MARKER"
    exit 0
  fi
  exit 1
fi
printf "%s\\n" "$*" >> "$MOCK_LAUNCHCTL_LOG"
if [[ -n "\${MOCK_LAUNCHCTL_FAIL_MATCH:-}" && "$*" == *"$MOCK_LAUNCHCTL_FAIL_MATCH"* ]]; then
  if [[ -n "\${MOCK_LAUNCHCTL_FAIL_TIMES:-}" ]]; then
    count=0
    [[ -r "$MOCK_LAUNCHCTL_FAIL_MARKER" ]] && count="$(cat "$MOCK_LAUNCHCTL_FAIL_MARKER")"
    if [[ "$count" -lt "$MOCK_LAUNCHCTL_FAIL_TIMES" ]]; then
      count=$((count + 1))
      printf '%s' "$count" > "$MOCK_LAUNCHCTL_FAIL_MARKER"
      exit 5
    fi
    exit 0
  fi
  if [[ "\${MOCK_LAUNCHCTL_FAIL_ONCE:-}" == "true" && ! -e "$MOCK_LAUNCHCTL_FAIL_MARKER" ]]; then
    : > "$MOCK_LAUNCHCTL_FAIL_MARKER"
    exit 5
  fi
  [[ "\${MOCK_LAUNCHCTL_FAIL_ONCE:-}" == "true" ]] && exit 0
  exit 5
fi
`,
  );
  chmodSync(join(mockBin, 'curl'), 0o755);
  chmodSync(join(mockBin, 'systemctl'), 0o755);
  chmodSync(join(mockBin, 'launchctl'), 0o755);
});

afterAll(() => {
  rmSync(home, { recursive: true, force: true });
});

describe('端口真源', () => {
  it('RELAY_HTTP_PORT 未设时从 relay.env 读，而不是写死 8787', () => {
    writeRelayEnv('RELAY_HTTP_PORT=12345\n');
    const { out } = run(['--check']);
    expect(out).toContain('127.0.0.1:12345');
    expect(out).not.toContain('8787');
  });

  it('调用方 env 覆盖 relay.env', () => {
    writeRelayEnv('RELAY_HTTP_PORT=12345\n');
    const { out } = run(['--check'], { RELAY_HTTP_PORT: '54321' });
    expect(out).toContain('127.0.0.1:54321');
  });

  it('RELAY_STATUS_HOST 可指向只绑定 Tailscale 地址的 resident Relay', () => {
    writeRelayEnv('RELAY_HTTP_PORT=12345\n');
    const { out } = run(['--check'], { RELAY_STATUS_HOST: '192.0.2.2' });
    expect(out).toContain('192.0.2.2:12345');
  });

  it('两处都没有端口时报错退出，不猜一个默认值', () => {
    writeRelayEnv('RELAY_INTERNAL_TOKEN=x\n');
    const { code, out } = run(['--check']);
    expect(code).toBe(1);
    expect(out).toContain('RELAY_HTTP_PORT 未设');
    expect(out).not.toContain('8787');
  });
});

describe('fail-closed：探不到状态必须拒绝', () => {
  // 端口 9（discard）在本机无人监听 —— curl 必然失败。
  it('HTTP 不可达时 --check 退 3，且绝不打印「no busy」', () => {
    writeRelayEnv('RELAY_HTTP_PORT=9\n');
    const { code, out } = run(['--check']);
    expect(code).toBe(3);
    expect(out).toContain('cannot reach Relay HTTP');
    // 这一条是本文件的核心：撤掉 curl 的 stderr 隔离，这里会变成 "OK: no busy" + exit 0
    expect(out).not.toContain('no busy');
  });

  it('curl 的错误文本不得混进状态取值（哨兵串比对必须精确命中）', () => {
    writeRelayEnv('RELAY_HTTP_PORT=9\n');
    const { out } = run(['--check']);
    expect(out).not.toContain('curl:');
  });
});

describe('cc-status 鉴权', () => {
  it('从 relay.env 读取 token 并只作为请求头传给 curl', () => {
    writeRelayEnv('RELAY_HTTP_PORT=12345\nRELAY_INTERNAL_TOKEN=test-secret\n');
    const { code, out } = runWithMocks(
      ['--check'],
      '',
      '7',
      { MOCK_REQUIRED_TOKEN: 'test-secret' },
    );
    expect(code).toBe(3);
    expect(out).toContain('cannot reach Relay HTTP');
    expect(out).not.toContain('test-secret');
  });

  it('调用方 token 覆盖 relay.env', () => {
    writeRelayEnv('RELAY_HTTP_PORT=12345\nRELAY_INTERNAL_TOKEN=file-secret\n');
    const result = run(['--check'], {
      PATH: `${mockBin}:${process.env['PATH'] ?? ''}`,
      RELAY_INTERNAL_TOKEN: 'caller-secret',
      MOCK_REQUIRED_TOKEN: 'caller-secret',
      MOCK_CURL_EXIT: '7',
    });
    expect(result.code).toBe(3);
    expect(result.out).toContain('cannot reach Relay HTTP');
    expect(result.out).not.toContain('caller-secret');
    expect(result.out).not.toContain('file-secret');
  });
});

describe('维护重启不会累积 systemd start-rate 计数', () => {
  const idle = '{"ok":true,"counts":{"busy":0},"busy":[],"scheduler":{"available":true,"busy":0,"running":[]},"safeToRestart":true}';
  const busy = '{"ok":true,"counts":{"busy":2},"busy":["pulse:a","pulse:b"],"scheduler":{"available":true,"busy":0,"running":[]},"safeToRestart":false}';
  const schedulerBusy = '{"ok":true,"counts":{"busy":0},"busy":[],"scheduler":{"available":true,"busy":1,"running":["marsh-vultr-dispatcher-20260619"]},"safeToRestart":false}';
  const sclBusy = '{"ok":true,"counts":{"busy":0},"busy":[],"scheduler":{"available":true,"busy":0,"running":[]},"sclIntents":{"available":true,"busy":2,"states":{"recognized":0,"attempting":0,"retry_wait":1,"result_ready":1}},"safeToRestart":false}';
  const sclRetryWait = '{"ok":true,"counts":{"busy":0},"busy":[],"scheduler":{"available":true,"busy":0,"running":[]},"sclIntents":{"available":true,"busy":1,"states":{"recognized":0,"attempting":0,"retry_wait":1,"result_ready":0}},"safeToRestart":false}';

  it('真正 restart 前先 reset-failed，同一服务且顺序固定', () => {
    writeRelayEnv('RELAY_HTTP_PORT=12345\n');
    const { code, systemctl } = runWithMocks([], idle);
    expect(code).toBe(0);
    expect(systemctl.trim().split('\n')).toEqual([
      '--user reset-failed relay.service',
      '--user restart relay.service',
    ]);
  });

  it('--check 只读且安全时不调用 systemctl', () => {
    writeRelayEnv('RELAY_HTTP_PORT=12345\n');
    const { code, out, systemctl } = runWithMocks(['--check'], idle);
    expect(code).toBe(0);
    expect(out).toContain('OK: no busy CC sessions, scheduler jobs, or SCL intents');
    expect(systemctl).toBe('');
  });

  it('busy 且无 --force 时退 2，不 reset 也不 restart', () => {
    writeRelayEnv('RELAY_HTTP_PORT=12345\n');
    const { code, out, systemctl } = runWithMocks([], busy);
    expect(code).toBe(2);
    expect(out).toContain('Busy CC sessions: 2');
    expect(out).toContain('pulse:a,pulse:b');
    expect(systemctl).toBe('');
  });

  it('scheduler job 正在运行时退 2，不 reset 也不 restart', () => {
    writeRelayEnv('RELAY_HTTP_PORT=12345\n');
    const { code, out, systemctl } = runWithMocks([], schedulerBusy);
    expect(code).toBe(2);
    expect(out).toContain('Running scheduler jobs: 1');
    expect(out).toContain('marsh-vultr-dispatcher-20260619');
    expect(systemctl).toBe('');
  });

  it('pending SCL intent 未完成投递时退 2，不 reset 也不 restart',()=>{
    writeRelayEnv('RELAY_HTTP_PORT=12345\n');
    const {code,out,systemctl}=runWithMocks([],sclBusy);
    expect(code).toBe(2);
    expect(out).toContain('Pending SCL intents: 2');
    expect(systemctl).toBe('');
  });

  it('durable retry_wait 可跨重启恢复，不与修复部署形成自锁',()=>{
    writeRelayEnv('RELAY_HTTP_PORT=12345\n');
    const {code,out,systemctl}=runWithMocks([],sclRetryWait);
    expect(code).toBe(0);
    expect(out).toContain('Durable retry-wait SCL intents preserved: 1');
    expect(systemctl.trim().split('\n')).toEqual([
      '--user reset-failed relay.service',
      '--user restart relay.service',
    ]);
  });

  it('SCL intent 状态不可读时 fail-closed 退 3',()=>{
    writeRelayEnv('RELAY_HTTP_PORT=12345\n');
    const unavailable='{"ok":true,"counts":{"busy":0},"busy":[],"scheduler":{"available":true,"busy":0,"running":[]},"sclIntents":{"available":false,"busy":0,"states":{}},"safeToRestart":false}';
    const {code,out,systemctl}=runWithMocks([],unavailable);
    expect(code).toBe(3);
    expect(out).toContain('SCL intent maintenance status unavailable');
    expect(systemctl).toBe('');
  });

  it('旧端点缺 scheduler 状态时 fail-closed 退 3', () => {
    writeRelayEnv('RELAY_HTTP_PORT=12345\n');
    const legacy = '{"ok":true,"counts":{"busy":0},"busy":[]}';
    const { code, out, systemctl } = runWithMocks([], legacy);
    expect(code).toBe(3);
    expect(out).toContain('scheduler maintenance status unavailable');
    expect(systemctl).toBe('');
  });

  it('HTTP 不可达且无 --force 时退 3，不 reset 也不 restart', () => {
    writeRelayEnv('RELAY_HTTP_PORT=12345\n');
    const { code, out, systemctl } = runWithMocks([], '', '7');
    expect(code).toBe(3);
    expect(out).toContain('cannot reach Relay HTTP');
    expect(systemctl).toBe('');
  });

  it('--force 仍可越过 busy 拒绝，并按 reset-failed → restart 执行', () => {
    writeRelayEnv('RELAY_HTTP_PORT=12345\n');
    const { code, out, systemctl } = runWithMocks(['--force'], busy);
    expect(code).toBe(0);
    expect(out).toContain('restart will interrupt in-flight work');
    expect(systemctl.trim().split('\n')).toEqual([
      '--user reset-failed relay.service',
      '--user restart relay.service',
    ]);
  });

  it('--force 仍可越过 HTTP 不可达，并按 reset-failed → restart 执行', () => {
    writeRelayEnv('RELAY_HTTP_PORT=12345\n');
    const { code, out, systemctl } = runWithMocks(['--force'], '', '7');
    expect(code).toBe(0);
    expect(out).toContain('HTTP unreachable, --force was given');
    expect(systemctl.trim().split('\n')).toEqual([
      '--user reset-failed relay.service',
      '--user restart relay.service',
    ]);
  });
});

describe('macOS launchd 安全重启后端', () => {
  const idle = '{"ok":true,"counts":{"busy":0},"busy":[],"scheduler":{"available":true,"busy":0,"running":[]},"safeToRestart":true}';

  it('Darwin 在同一 maintenance 闸之后只 kickstart 指定 LaunchAgent', () => {
    writeRelayEnv('RELAY_HTTP_PORT=12345\n');
    const { code, systemctl, launchctl } = runWithMocks([], idle, '0', {
      RELAY_RESTART_PLATFORM: 'Darwin',
      RELAY_LAUNCHD_TARGET: 'gui/501/com.pulse.relay',
    });
    expect(code).toBe(0);
    expect(systemctl).toBe('');
    expect(launchctl.trim()).toBe('kickstart -k gui/501/com.pulse.relay');
  });

  it('显式提供已验证 plist 时在同一 maintenance 闸之后重新装载 LaunchAgent', () => {
    writeRelayEnv('RELAY_HTTP_PORT=12345\n');
    const plist = join(home, 'com.pulse.relay.plist');
    writeFileSync(plist, '<plist/>\n');
    const { code, systemctl, launchctl } = runWithMocks([], idle, '0', {
      RELAY_RESTART_PLATFORM: 'Darwin',
      RELAY_LAUNCHD_TARGET: 'gui/501/com.pulse.relay',
      RELAY_LAUNCHD_PLIST: plist,
    });
    expect(code).toBe(0);
    expect(systemctl).toBe('');
    expect(launchctl.trim().split('\n')).toEqual([
      'bootout gui/501/com.pulse.relay',
      `bootstrap gui/501 ${plist}`,
    ]);
  });

  it('bootout 后等待旧 label 真正移除才 bootstrap canonical plist', () => {
    writeRelayEnv('RELAY_HTTP_PORT=12345\n');
    const plist = join(home, 'com.pulse.relay.wait.plist');
    writeFileSync(plist, '<plist/>\n');
    const { code, launchctl, launchctlPrintAttempts } = runWithMocks([], idle, '0', {
      RELAY_RESTART_PLATFORM: 'Darwin',
      RELAY_LAUNCHD_TARGET: 'gui/501/com.pulse.relay',
      RELAY_LAUNCHD_PLIST: plist,
      MOCK_LAUNCHCTL_PRINT_BUSY_ATTEMPTS: '2',
    });
    expect(code).toBe(0);
    expect(launchctlPrintAttempts).toBe('2');
    expect(launchctl.trim().split('\n')).toEqual([
      'bootout gui/501/com.pulse.relay',
      `bootstrap gui/501 ${plist}`,
    ]);
  });

  it('bootout 后首次 bootstrap 瞬时 EIO 时有界重试并恢复同一新 plist', () => {
    writeRelayEnv('RELAY_HTTP_PORT=12345\n');
    const plist = join(home, 'com.pulse.relay.retry.plist');
    writeFileSync(plist, '<plist/>\n');
    const { code, launchctl } = runWithMocks([], idle, '0', {
      RELAY_RESTART_PLATFORM: 'Darwin',
      RELAY_LAUNCHD_TARGET: 'gui/501/com.pulse.relay',
      RELAY_LAUNCHD_PLIST: plist,
      MOCK_LAUNCHCTL_FAIL_MATCH: plist,
      MOCK_LAUNCHCTL_FAIL_ONCE: 'true',
    });
    expect(code).toBe(0);
    expect(launchctl.trim().split('\n')).toEqual([
      'bootout gui/501/com.pulse.relay',
      `bootstrap gui/501 ${plist}`,
      `bootstrap gui/501 ${plist}`,
    ]);
  });

  it('新 plist bootstrap 失败时自动恢复事务提供的旧 plist', () => {
    writeRelayEnv('RELAY_HTTP_PORT=12345\n');
    const nextPlist = join(home, 'com.pulse.relay.next.plist');
    const rollbackPlist = join(home, 'com.pulse.relay.before.plist');
    writeFileSync(nextPlist, '<plist/>\n');
    writeFileSync(rollbackPlist, '<plist><dict/></plist>\n');
    const { code, launchctl } = runWithMocks([], idle, '0', {
      RELAY_RESTART_PLATFORM: 'Darwin',
      RELAY_LAUNCHD_TARGET: 'gui/501/com.pulse.relay',
      RELAY_LAUNCHD_PLIST: nextPlist,
      RELAY_LAUNCHD_ROLLBACK_PLIST: rollbackPlist,
      MOCK_LAUNCHCTL_FAIL_MATCH: nextPlist,
      MOCK_LAUNCHCTL_FAIL_TIMES: '3',
    });
    expect(code).toBe(5);
    expect(launchctl.trim().split('\n')).toEqual([
      'bootout gui/501/com.pulse.relay',
      `bootstrap gui/501 ${nextPlist}`,
      `bootstrap gui/501 ${nextPlist}`,
      `bootstrap gui/501 ${nextPlist}`,
      'bootout gui/501/com.pulse.relay',
      `bootstrap gui/501 ${nextPlist}`,
    ]);
    expect(readFileSync(nextPlist, 'utf-8')).toBe('<plist><dict/></plist>\n');
  });
});

describe('autonomous 分身闸不受本次改动影响', () => {
  it('HARNESS_TRIGGER_MODE=cron 且非 --check 时退 4', () => {
    writeRelayEnv('RELAY_HTTP_PORT=9\n');
    const { code, out } = run([], { HARNESS_TRIGGER_MODE: 'cron' });
    expect(code).toBe(4);
    expect(out).toContain('不得直接重启');
  });

  it('cron 下 --check 只读探忙仍放行到端口判定（不被 exit 4 拦）', () => {
    writeRelayEnv('RELAY_HTTP_PORT=9\n');
    const { code } = run(['--check'], { HARNESS_TRIGGER_MODE: 'cron' });
    expect(code).toBe(3); // 走到了 HTTP 探测，而不是 4
  });
});
