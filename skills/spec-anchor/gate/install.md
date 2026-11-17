# 挂载说明 —— spec 漂移闸

给 Pulse。三个文件,零 config 改动,五分钟装完。

## 为什么要这道闸

近 90 天 Relay 302 次 `src/` 提交里:

| 同一批提交同时改了什么 | 比例 |
|---|---|
| 带 tests | **62.6%** |
| 带 specs | **9.3%** |

同一个 Agent、同一套纪律。测试跟得上,spec 跟不上——差别不是自觉,是**测试有机器闸,spec 没有**。
这道闸就是让 spec 搭上测试这趟已经在跑的车。

## 装什么

从 `ken-space/outputs/spec-anchor/gate/` 拷三个文件到 Relay 的 `tests/`:

| 从 | 到 | 作用 |
|---|---|---|
| `spec-drift.ts` | `tests/spec-drift.ts` | 检测引擎(零依赖,不导出测试,vitest 不会当测试文件收) |
| `spec-drift.test.ts` | `tests/spec-drift.test.ts` | 闸本身 |
| `spec-drift.baseline.json` | `tests/spec-drift.baseline.json` | 基线,**要进 git** |

```bash
cd /Users/owner/Coevolution/Bigbird/PULSE/Bigbird/Relay
G=/Users/owner/Coevolution/Bigbird/ken-space/outputs/spec-anchor/gate
cp $G/spec-drift.ts $G/spec-drift.test.ts $G/spec-drift.baseline.json tests/
npm test -- tests/spec-drift.test.ts
```

## 不用改 vitest.config.ts

现有配置:

```ts
test: { globals: true, root: '.', include: ['tests/**/*.test.ts'], env: { RELAY_OBS_ENABLED: 'false' } }
```

`tests/spec-drift.test.ts` 天然落进 `include`。**config 一个字都不用动**,`package.json` 也不用动。

引擎文件 `spec-drift.ts` 不带 `.test.` 后缀,不会被当成测试文件收走,只作为模块被 import。

## 挂了之后你会看到什么

### 平时(没引入新漂移)

跟现在一样,只是测试文件数从 60 变 61、用例数 +2。**实测全量 61 文件 1086 用例全绿。**

### 改代码删/改名了 spec 引用的符号

假设你把 `src/commands.ts` 里的 `handleStart` 改名成 `handleStartCommand`,忘了同步 spec。
`npm test` 会红,输出如下(**这是真跑出来的,不是示意**):

```
 FAIL  tests/spec-drift.test.ts > spec 漂移闸 > 新增 spec 漂移数不得增加
Error: spec 漂移增加: 新增 1 条(基线 48 → 当前 49)

  specs/features/bot-commands/tasks.md:115
    spec 引用的符号 `handleStart` 在 src/ 中已不存在(可能被改名或删除)

  处置二选一:
    1. 改 spec —— 代码是对的,spec 落后了,把上面这几行改对
    2. 降基线 —— 若你确认这些是既有问题非本次引入,跑:
       node gate/update-baseline.mjs
```

**一行指出哪份 spec、哪一行、哪个符号。** 改完 spec 再跑就绿了(实测走通)。

若同一个符号在 spec 里被引用多处,闸会一处一处报——修完一处报下一处,直到全部同步。

> **验证闸有效,请挑一个 spec 里绑了锚点的符号改名。** 改一个 spec 没引用过的死代码,
> 闸不会响——不是闸坏了,是那个符号本来就不在闸的判定范围内(2026-07-29 Pulse 实测:
> 先改 `markPendingInteractive` 测出假绿,换成 spec 真引用的 `addJob` 才测出真红)。

## 基线怎么更新

基线是"当前已知的 48 条存量漂移"。闸只拦**新增**,不要求归零。

**为什么不要求归零**:Relay 现有 167 条未绑锚点 + 48 条存量漂移。
要求归零会让闸永远红,最后必然被人加 `.skip`——
一道被 skip 的闸比没有闸更坏,因为它制造"已经在把关"的错觉。

你主动修了 spec、漂移降下来了,跑下面这段把成果锁住。

**这段是实测走通的**(在 Relay 完整副本上跑出 `新基线: 48`,基线文件正确覆盖):

```bash
cd /Users/owner/Coevolution/Bigbird/PULSE/Bigbird/Relay
cat > tests/rebase.test.ts <<'EOF'
import { it } from 'vitest';
import { writeFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';
import { scanDrift } from './spec-drift.js';
it('rebaseline', () => {
  const HERE = dirname(fileURLToPath(import.meta.url));
  const r = scanDrift({ repo: join(HERE, '..') });
  writeFileSync(join(HERE, 'spec-drift.baseline.json'),
    JSON.stringify({ generated: new Date().toISOString(),
      driftCount: r.broken.length,
      knownKeys: r.broken.map((a) => a.key).sort() }, null, 2) + '\n');
}, 60000);
EOF
npm test -- tests/rebase.test.ts && rm tests/rebase.test.ts
```

> 为什么不做成常驻脚本:`gate/update-baseline.mjs` 需要已编译的 `spec-drift.js`,
> 而 Relay 的 `tsc` 不把 `tests/` 编进 `dist/`。与其让你多装一个 tsx 依赖,
> 不如用上面这段一次性 test —— 它借的正是你已经有的 vitest。
> `update-baseline.mjs` 仍留在 gate/ 里,供**已编译场景**或别的仓库用,
> **在 Relay 里开箱即用这点我没验证过**,别照着它排障。

基线上升时,`git diff` 里 `driftCount` 变大是**看得见的**——
那正是 code review 里"谁把漂移放进来了"这个问题的答案。

## 耗时

| 项 | 实测 |
|---|---|
| 单次扫描(80 份 spec / 639 锚点 / 24227 行源码) | **中位 239ms**(5 次:155–407ms) |
| 闸文件单跑(含 vitest 启动,跑两次扫描) | **约 2.0s** |
| 对照:既有 `tests/commands.test.ts` 单跑 | 3.33s |
| 全量 61 文件 1086 用例 | 全绿 |

**闸比一个普通既有测试文件还便宜**,且与其余 60 个文件并行跑,边际成本可忽略。

> 全量耗时不作为对比读数:同机多次跑 Relay 全量在 108s–133s 间波动(共享机器负载),
> 波动幅度远大于闸本身的成本,拿它算差值会得出没有意义的数字。

## 卸载

删三个文件即可,无残留。
