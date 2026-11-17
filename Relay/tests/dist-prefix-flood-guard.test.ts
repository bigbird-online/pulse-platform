// dist-level 真链路验证：散落型词头复读（prefix-flood）自愈 —— 2026-07-09
// ---------------------------------------------------------------------------
// 背景：Amanda（飞书 print-mode，--model opus）出现"轻度退化/泄漏"——连续多条 assistant
// record 的段首漏出孤立短词 `card`（自身 scorecard 语境词降解，与 court/course/county 同族）。
// session 未停摆（工具照发）但飞书卡片显示脏。真实数据 jsonl：
//   /home/pulse/.claude/projects/-Users-owner-Coevolution-Doc-amanda-space/
//   9c0eeb67-d988-4124-9641-3d7709c662dc.jsonl
// 12 条含 `card` 的 record 里，只有 1 条（`card\n\ncard\n\ncard`）命中 DEGEN_TOKEN_FLOOD_RE；
// 其余 11 条每条只漏 1–2 个 `card`，**单条内够不到任何逐条判据门槛**。detectBareFenceLeak /
// detectToolcallLeak 都逐条判定，结构上抓不到"每条只漏 1-2、但持续漏"的散落型泄漏。
//
// 修法（本文件验证的对象）：pool.ts 新增进程级跨-record 滑动窗口判据 recordAndCheckPrefixFlood，
// 语义源自外部探针 degen-probe.py 的 prefix-flood（阈值=5 / 停用词表同源）——某短词在 ≥5 条不同
// record 里**孤立独占整行**出现 → 命中 → 交既有 A+B 自愈（nudge → 超限清盘 respawn）。这样活进程
// 遇散落型泄漏能实时自愈，不必等 15min 一轮的外部探针。
//
// 收窄（2026-07-09 主身复核，全机 943 jsonl 实测）：探针 LEADING_WORD_RE 是宽判据（词后跟正文也
// 算），Relay 侧照搬会误伤 Duke 段首口头禅 `count 一下…` / `power:对端…`（词后带正文的正常调度
// 提纲），连刷 5 条被判 flood 打断正常回合（非-card 误伤 108）。收窄到"孤立独占整行的短词"
// （DEGEN_SOLO_WORD_LINE_RE：整行 strip 后就一个短词 + 可选标点、无正文），全机误伤 108→0、
// Amanda card 仍命中。用例 (e) 是这次收窄的回归钉子。
//
// 本测试的价值（同 dist-print-leak-guard.test.ts）：import 的是**编译产物 `../dist/pool.js`**
// （生产 relay 真正加载的那份），喂**真实 Amanda 泄漏 record**，断言累积到阈值时 dist 编译码里
// 确实拦截（不 emit 泄漏正文 + nudge 重发），且阈值以下的 record 正常放行、正常内容零误伤。
//
// 数据来源诚实性：下方 AMANDA_CARD_RECORDS 是从上述真实 jsonl 抽出的 6 条 record 原文
// （**刻意排除了那条会单独命中 token-flood 的 record**，只留下靠 prefix-flood 才抓得到的
// 散落型 record），证明本判据独立于既有逐条判据、专治散落型。
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { EventEmitter } from 'node:events';
import { PassThrough } from 'node:stream';
// ↓↓↓ 关键：import dist（编译产物），不是 src。这是整个文件存在的理由。
import { createPool } from '../dist/pool.js';
import type { SpawnConfig, StreamJsonMessage } from '../src/types.js';

interface MockProcess extends EventEmitter {
  stdin: PassThrough;
  stdout: PassThrough;
  stderr: PassThrough;
  pid: number;
  kill: ReturnType<typeof vi.fn>;
  killed: boolean;
}

function createMockProcess(pid = 5321): MockProcess {
  const proc = new EventEmitter() as MockProcess;
  proc.stdin = new PassThrough();
  proc.stdout = new PassThrough();
  proc.stderr = new PassThrough();
  proc.pid = pid;
  proc.killed = false;
  proc.kill = vi.fn((signal?: string) => {
    proc.killed = true;
    proc.emit('exit', signal === 'SIGKILL' ? 137 : 0, signal ?? 'SIGTERM');
    return true;
  });
  return proc;
}

function captureStdin(proc: MockProcess): { text: () => string } {
  let buf = '';
  proc.stdin.on('data', (chunk: Buffer | string) => { buf += chunk.toString(); });
  return { text: () => buf };
}

let mockSpawn: ReturnType<typeof vi.fn>;
vi.mock('node:child_process', () => ({
  spawn: (...args: unknown[]) => mockSpawn(...args),
}));

const baseConfig: SpawnConfig = {
  tenantId: 'member-000',
  workspace: '/opt/PULSE/Tenants/member-000/',
  sessionId: 'prefix-flood-verify-session-uuid',
};

// 真实 Amanda 泄漏 record（6 条，全部 stop_reason=tool_use、含 `card` **独占整行**）。
// 这 6 条**没有一条**单独命中 token-flood / bare-fence / <invoke> 判据——只有跨这 6 条
// 累积 `card` 孤立整行次数（1→6）才越过阈值 5，纯靠 prefix-flood 抓。收窄后 `card` 仍命中
// （它本就独占一行），噪声词（gate12/gate13 等词后带正文的行）被收窄判据排除、不再计数。
const AMANDA_CARD_RECORDS: Array<{ uuid: string; stop: string; text: string }> = [
  {"uuid":"f1023920","stop":"tool_use","text":"gate12 表格行被我上一次编辑挪到了 gate13 说明之后,顺序有点乱但不影响功能。先把 gate11 内嵌示例的 pattern 更新(它现在检\"透镜不是零件\",但规则里三要害第3条已改;不过 gate 检的是规则文件含这些词,规则文件里\"不抢运营/导向行动\"仍在——我加\"只写结论\"进 pattern 让它匹配新三要害),并修 gate10 注释里的\"六原则透镜\"笔误。\n\n同时把表格顺序理顺:gate12 行移到 gate13 说明块之前。\n\ncard\n先修内嵌示例注释 + gate11 pattern:"},
  {"uuid":"eecd0bfc","stop":"tool_use","text":"SSOT 里 single_file_skeleton 内嵌示例还写着\"六原则透镜\",改掉,并在其后加 six_principles_as_method 内嵌示例。看当前内嵌 rubric 示例位置。\n\ncard\n\ncard\n先改 single_file_skeleton 内嵌示例的措辞（0.75/1.0 level 里的\"六原则透镜\"）+ 加新维度示例："},
  {"uuid":"00b8bde1","stop":"tool_use","text":"single_file_skeleton 内嵌示例的 weight 还是旧的 0.25,更新成 0.20,并在其后加 six_principles_as_method 示例。\n\ncard\n\ncard"},
  {"uuid":"3529baca","stop":"tool_use","text":"改 rubric.yaml 剩下三个权重：three_cautions 0.25→0.20、carries_120 保持0.15、minimize_context 0.10→0.05。同时 three_cautions 描述\"透镜非零件\"改成\"只写结论\"。\n\ncard"},
  {"uuid":"2b4ee4fd","stop":"tool_use","text":"card\n改 three_cautions 内嵌 levels（把\"透镜非零件\"改成\"只写结论\"）+ minimize_context 权重 0.10→0.05。先读这两段确认措辞。"},
  {"uuid":"adcdbdee","stop":"tool_use","text":"改 three_cautions 内嵌 levels（第76、82行\"由X原则监控Y任务\"/\"透镜非零件\"改成\"只写结论/方法不进成品\"）+ minimize_context 权重 0.10→0.05。\n\ncard"},
];

function recordLine(rec: { uuid: string; stop: string; text: string }): string {
  return JSON.stringify({
    type: 'assistant',
    uuid: rec.uuid,
    message: { model: 'claude-opus-4-8', stop_reason: rec.stop, content: [{ type: 'text', text: rec.text }] },
  }) + '\n';
}

function assistantTextLine(text: string, stopReason = 'end_turn'): string {
  return JSON.stringify({
    type: 'assistant',
    message: { model: 'claude-opus-4-8', stop_reason: stopReason, content: [{ type: 'text', text }] },
  }) + '\n';
}

async function bootPool(): Promise<{
  pool: ReturnType<typeof createPool>;
  proc: MockProcess;
  stdin: { text: () => string };
  responses: Array<{ msg: StreamJsonMessage }>;
}> {
  const proc = createMockProcess();
  mockSpawn.mockReturnValue(proc);
  const stdin = captureStdin(proc);
  const responses: Array<{ msg: StreamJsonMessage }> = [];
  const pool = createPool({ cleanupIntervalMs: 999999, ccMode: 'print' });
  pool.on('response', (_t: string, msg: StreamJsonMessage) => responses.push({ msg }));
  const spawnPromise = pool.spawn(baseConfig);
  proc.stdout.write('{"type":"system","content":"ready"}\n');
  await spawnPromise;
  pool.send(baseConfig.tenantId, { type: 'user', content: [{ type: 'text', text: 'go' }] }).catch(() => {});
  await new Promise((r) => setTimeout(r, 20));
  return { pool, proc, stdin, responses };
}

describe('dist/pool.js prefix-flood 散落型词头复读自愈（真链路 + 真实 Amanda 数据）', () => {
  afterEach(() => {
    delete process.env['RELAY_ENABLE_PREFIX_FLOOD_GUARD'];
    delete process.env['RELAY_ENABLE_PRINT_LEAK_GUARD'];
    vi.restoreAllMocks();
  });

  beforeEach(() => {
    mockSpawn = vi.fn();
  });

  it('(a) 累积到阈值时命中 prefix-flood：第 5 条起被拦截 + nudge，阈值以下正常放行', async () => {
    const { pool, proc, stdin, responses } = await bootPool();

    // 逐条喂真实泄漏 record。阈值=5，前 4 条 card 累计 1..4 < 5 → 不拦，正常 emit；
    // 第 5 条 card 累计到 5 = 阈值 → 拦截（不 emit）+ nudge 写进 stdin。
    const emittedTextsBeforeThreshold: string[] = [];
    for (let i = 0; i < 4; i++) {
      proc.stdout.write(recordLine(AMANDA_CARD_RECORDS[i]!));
      await new Promise((r) => setTimeout(r, 40));
    }
    // 前 4 条：这些是 stop_reason=tool_use、无真 tool_use 的中间态 record。print 路径对
    // 无 text-and-no-AskUserQuestion 的会 drop；但**含 text 的会 emit**。这里只断言：
    // 前 4 条里没有任何一条触发 nudge（stdin 里还没有 nudge 文案）。
    expect(stdin.text()).not.toContain('异常的重复/乱码片段');
    // 记录阈值前 emit 的正文（供人肉核对：前 4 条泄漏正文确实进了 channel —— 这正是"脏卡片"现象）。
    for (const r of responses) {
      if (r.msg.type === 'assistant') emittedTextsBeforeThreshold.push(JSON.stringify(r.msg));
    }

    // 第 5 条（card 累计到 5）：命中 prefix-flood → 拦截。
    const responsesBefore = responses.length;
    proc.stdout.write(recordLine(AMANDA_CARD_RECORDS[4]!));
    await new Promise((r) => setTimeout(r, 100));

    // A：nudge（散落乱码文案）已写进 stdin。
    expect(stdin.text()).toContain('异常的重复/乱码片段');
    // 第 5 条泄漏正文没有新 emit 到 channel（被 A 拦住）。
    const newAfterThreshold = responses.slice(responsesBefore);
    const fifthLeaked = newAfterThreshold.find(
      (r) => r.msg.type === 'assistant'
        && JSON.stringify(r.msg).includes('minimize_context 0.10→0.05')
        && JSON.stringify(r.msg).includes('card'),
    );
    expect(fifthLeaked).toBeUndefined();
    // A 不杀进程（前 maxToolcallLeakRetries 次是 nudge，不是 respawn）。
    expect(proc.killed).toBe(false);

    pool.dispose?.();
  });

  it('(b) 命中前的正常 record 不误伤：先喂 4 条正常英文/代码/中文，card 计数不受污染', async () => {
    const { pool, proc, stdin, responses } = await bootPool();

    // 正常 record：正常英文写作、正常代码讨论、正常中文回复。段首出现 the/for/note 等
    // 停用词、以及非重复的普通词——都不该累积成 flood。
    const normalRecords = [
      'Here is the plan for the refactor. The build script should compile cleanly.\nNote that we keep the old flag for one release.',
      '```ts\nfunction add(a: number, b: number) {\n  return a + b;\n}\n```\nThis helper is used across the pool module for counters.',
      '我看了一下 gates.yaml，权重分配没问题。三层判据都在，骨架检查也通过了。\n下一步是补测试。',
      'For the deployment we run the build, then restart the service. See the memo for rollback steps.',
    ];
    for (const t of normalRecords) {
      proc.stdout.write(assistantTextLine(t));
      await new Promise((r) => setTimeout(r, 40));
    }
    // 正常 record 全部放行、零 nudge、零 kill。
    expect(stdin.text()).not.toContain('异常的重复/乱码片段');
    expect(stdin.text()).not.toContain('把工具调用写成了文本');
    expect(proc.killed).toBe(false);
    // 正常 record 的正文正常 emit 给 channel（没被误拦）。
    const emittedNormal = responses.filter(
      (r) => r.msg.type === 'assistant' && JSON.stringify(r.msg).includes('refactor'),
    );
    expect(emittedNormal.length).toBeGreaterThan(0);

    pool.dispose?.();
  });

  it('(c) 停用词高频段首不误判：连喂 8 条以 the/for/note 开头的正常 record，绝不触发', async () => {
    const { pool, proc, stdin } = await bootPool();
    const stopwordLed = [
      'the release notes are ready.',
      'for the record, this is fine.',
      'note the edge case here.',
      'the pool handles it.',
      'for now we keep it.',
      'note that build passes.',
      'the guard is off.',
      'for each tenant we clear state.',
    ];
    for (const t of stopwordLed) {
      proc.stdout.write(assistantTextLine(t));
      await new Promise((r) => setTimeout(r, 30));
    }
    // 8 条 the/for/note 段首（远超阈值 5），但全在停用词表 → 绝不判 flood。
    expect(stdin.text()).not.toContain('异常的重复/乱码片段');
    expect(proc.killed).toBe(false);

    pool.dispose?.();
  });

  // 回归钉子（2026-07-09 主身复核，全机 943 jsonl 实测发现宽判据误伤后收窄）：
  // 计数判据从"行首短词"收窄到"孤立独占整行的短词"。词后**带正文**的正常调度提纲
  // （Duke 段首口头禅 `count 一下…` / `power:对端…` / `review 这份 diff` 等）连刷再多也不该
  // 进 prefix-flood 计数——否则连刷 5 条就被误判 flood 打断正常回合（收窄前全机误伤 108）。
  it('(e) 回归：词后带正文的正常调度提纲连喂 6 条同词，绝不命中（收窄钉子）', async () => {
    const { pool, proc, stdin, responses } = await bootPool();
    // 6 条都以同一个短词 count/power/review 起头，但**词后跟正文**（非孤立整行）。
    // 收窄后 DEGEN_SOLO_WORD_LINE_RE 不匹配这类 → 不计数 → 不命中。
    const dispatchOutlines = [
      'count 一下 commit 前自检清单',
      'count 一下改了几个文件',
      'count 一下 gates 数量',
      'count 一下 rubric 维度',
      'count 一下测试用例',
      'count 一下残余待办',
    ];
    for (const t of dispatchOutlines) {
      proc.stdout.write(assistantTextLine(t));
      await new Promise((r) => setTimeout(r, 30));
    }
    // 连喂 6 条同词（count）段首，但都词后带正文 → 收窄判据不计数 → 零 nudge、零 kill。
    expect(stdin.text()).not.toContain('异常的重复/乱码片段');
    expect(proc.killed).toBe(false);
    // 提纲正文正常 emit（没被误拦）。
    const emitted = responses.filter(
      (r) => r.msg.type === 'assistant' && JSON.stringify(r.msg).includes('commit 前自检'),
    );
    expect(emitted.length).toBeGreaterThan(0);

    // 再补 power:/review 两组混合，确认不是只对 count 生效。
    const stdinBefore = stdin.text();
    const moreOutlines = [
      'power:对端 reset 追平',
      'power:本端 build 重启',
      'power:双机同步',
      'power:回滚点确认',
      'power:escalation 推送',
      'review 这份 diff 的原子性',
      'review 一下测试覆盖',
      'review 残余风险',
      'review contract drift',
      'review 部署链路',
    ];
    for (const t of moreOutlines) {
      proc.stdout.write(assistantTextLine(t));
      await new Promise((r) => setTimeout(r, 25));
    }
    // power(6→11) / review(6→11) 段首各 5 条，都词后带正文 → 仍不命中。
    expect(stdin.text()).toBe(stdinBefore); // stdin 无新增（无 nudge 写入）
    expect(proc.killed).toBe(false);

    pool.dispose?.();
  });

  it('(d) flag 关闭（RELAY_ENABLE_PREFIX_FLOOD_GUARD=false）→ 散落泄漏不被此判据拦（回退旧行为）', async () => {
    process.env['RELAY_ENABLE_PREFIX_FLOOD_GUARD'] = 'false';
    const { pool, proc, stdin } = await bootPool();

    // 喂全部 6 条（card 累计到 6 > 阈值），但 flag 关 → recordAndCheckPrefixFlood 直接返回不判。
    for (const rec of AMANDA_CARD_RECORDS) {
      proc.stdout.write(recordLine(rec));
      await new Promise((r) => setTimeout(r, 40));
    }
    // flag 关：prefix-flood 判据不触发（不因散落 card 而 nudge）。
    expect(stdin.text()).not.toContain('异常的重复/乱码片段');

    pool.dispose?.();
  });
});

// Pool behavior is isolated from OS process ownership, tested with real processes separately.
vi.mock('../dist/owned-process.js', async () => ({
  spawnOwnedProcess: (await import('node:child_process')).spawn,
}));
