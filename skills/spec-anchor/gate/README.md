# spec 漂移闸 —— 设计取舍与已知限制

把 `../src/scan.py` 的漂移检测做成一道**能挂进 Relay 现有 vitest 测试车**的闸。

挂载步骤见 `install.md`。这份只讲**为什么这么做**和**哪里还不行**。

## 一、为什么用 TypeScript 重写,而不是从 vitest 里调 scan.py

这是本次最大的一个取舍,两条理由,第二条是决定性的。

### 理由一:Python 依赖会给 Relay 的 CI 增加前提

Relay 是纯 TypeScript 仓,`package.json` 里没有任何 Python 相关内容,
`npm test` 目前只要求 node ≥18。调 `scan.py` 意味着 CI 环境必须额外保证 python3 存在
且版本兼容(`scan.py` 用了 `from __future__ import annotations` + `list[str]` 语法,
要 3.9+;本机是 3.14.4)。

一道闸的价值在于**长期没人管它也能一直跑**。多一个环境前提,就多一个未来某天
"换了 CI 镜像后它悄悄不跑了"的可能。而闸悄悄不跑,比没有闸更坏。

### 理由二(决定性):Python 版慢到不可接受

实测 `scan.py` 全量扫 Relay:

```
real  0m50.875s
```

**50.9 秒**。Relay 全量测试实测 108.59s。加这一道等于让测试车慢 47%。
没有人会容忍这个,结局必然是被 skip 掉。

profile 定位到瓶颈,两处:

| 瓶颈 | 耗时 | 成因 |
|---|---|---|
| `_grep` 逐行正则 | 51.1s | 476 个 symbol 锚点 × 24227 行源码 = **2286 万次** `re.search` |
| `_ever_existed` | 9.4s | 35 次 `git log --all -S` 子进程 |

两处都不是"Python 慢",是**算法是 O(锚点 × 行数)**。

TS 版换成**倒排索引**:源码一次分词建 `Set<string>`,锚点校验降为 `Set.has()` 的 O(1)。
并且**不跑 git 历史**——闸只需回答"漂移数增加了吗",
不需要区分"曾存在后被删"与"从未落地"(那是人读报告时的分类,由全量扫描提供)。

结果:

| | Python 全量扫描 | TS 闸 |
|---|---:|---:|
| 单次扫描 | 50,900ms | **239ms**(中位,5 次 155–407) |
| 倍数 | 1× | **约 213×** |

### 代价(诚实说)

**两份实现,两套推断规则,会漂。** 这是真代价,不粉饰。
`spec-drift.ts` 的推断正则与 `../src/infer.py` 逐条对齐,任何一边改了另一边要跟。
缓解措施是初版做了**逐条集合比对**(不是只比总数):

```
py 47  ts 47  |  TS-only: []  |  PY-only: []
```

639 个锚点、47 条漂移、symbol 43 / test 4、holds 435 / unverifiable 157 —— 全等。
后来因为修一个真 bug(见下)口径有意分叉了 1 条,现在是 py 47 / ts 48,差异已查明并记录。

## 二、为什么断言"不增加"而不是"归零"

Relay 现状:167 条未绑锚点 + 48 条存量漂移。

要求归零 = 闸永远红 = 三天内被人加 `.skip` = **比没有闸更坏**,
因为它留下一个"我们已经在把关"的错觉。

所以基线记录当前 48 条的**稳定 key**,只拦新增。

### 基线比的是 key 不是数字

这条不是洁癖,是防一个真实漏洞:**修好一条 + 引入一条,总数不变**。
只比 `driftCount` 的实现会在这里放水。

基线存 `knownKeys`(`type|specFile|target|claim`,**不含行号**——
spec 里插一行不该被算成新漂移)。自测里有一条专门锁这个场景:

> `修好一条又引入一条(总数不变) → 仍然红`

## 三、实现过程中真踩到的一个坑(值得记下来)

初版为了让 TS 读数和 Python 报告**完全一致**,我把 `tests/` 并进了符号索引
(Python 的 `_verify_symbol` 搜的是 `_CODE_GLOBS`,含 tests/)。
这样确实消掉了唯一那条差异(`runningIds`)。

**然后端到端测试当场打脸**:把 `src/` 全域 `handleStart` 改名成 `handleStartCommand`
(spec 不动)——这是闸**最该抓**的场景——闸是绿的。
因为 `tests/commands.test.ts` 里还留着旧名字,符号在合并索引里"仍然存在"。

**为了让数字好看,我把闸的核心能力废掉了。**

修法是把两个索引拆开,各司其职:

| 索引 | 用途 |
|---|---|
| `srcIdents` | **唯一判定依据**。spec 引用的符号在不在生产代码里,只问它 |
| `testIdents` | 只用于**降级**:符号名带 `test` 且只在 tests/ 出现 → `unverifiable` 而非 `broken` |

降级条件必须窄。第二版曾把"只在 tests/ 出现"一律降级,同样漏掉了改名场景——
真正该降级的只是 spec 点名的**计划中测试函数**(finance-engine 那类 `test_discrepancy_creation`),
不是任何碰巧出现在测试里的生产符号。

两条自测锁死这个行为:

- `回归: src/ 里改了名但 tests/ 还留着旧名 → 仍判 broken`
- `spec 点名的测试函数只在 tests/ 存在 → unverifiable(不误报成漂移)`

**教训**:闸的正确性标准是"能不能抓住新引入的漂移",不是"数字和报告一样"。
拿前者换后者,是把手段当成了目的。

## 四、实测读数

环境:vultr(Linux),node v22.22.2,vitest 4.1.4,2026-07-25。

### 与 Python 全量扫描的差异

| | Python | TS 闸 |
|---|---:|---:|
| spec 文件 | 80 | 80 |
| 锚点 | 639 | 639 |
| 漂移 broken | 47 | **48** |
| symbol / test | 43 / 4 | 44 / 4 |

差的一条是 `specs/features/feishu-pairing-http/tasks.md:19` 的 `runningIds`。
**回源查证过**:spec 声称 `createFeishuBots` 返回 `{ start, stop, attach, runningIds }`,
而 `src/` 里根本没有 `runningIds`(真实符号是 `runningAgentIds()`),
它只作为局部变量出现在 `tests/relay-http.test.ts:24`。

**这条 TS 判得对,Python 漏了**(Python 把 tests/ 算进了符号存在性)。
差异是有意的,不是 bug。

### 耗时

| 项 | 实测 |
|---|---|
| 单次扫描 | 中位 **239ms**(5 次:155/168/239/241/407) |
| 闸文件单跑(含 vitest 启动,跑 2 次扫描) | 约 **2.0s** |
| 对照:既有 `tests/commands.test.ts` 单跑 | 3.33s |
| 挂载后全量 | **61 文件 1086 用例全绿** |

闸比一个普通既有测试文件还便宜,且与其余 60 个文件并行,边际成本可忽略。

> 不给"全量前后差值":同机跑 Relay 全量在 108.59s–132.72s 间波动(共享机器负载),
> 波动远大于闸本身成本,做差会得出没有意义的数字。这里如实说明,不硬凑一个好看的数。

### 端到端行为(真跑,非示意)

| 场景 | 结果 |
|---|---|
| 干净 Relay | 绿 |
| `src/` 全域改名 `handleStart`→`handleStartCommand`,spec 不动 | **红**,指出 `specs/features/bot-commands/tasks.md:115` |
| 按提示改 spec 第 115 行 | 仍红,指出**第 136 行**(同符号另一处引用) |
| 改完全部 2 处引用 | **恢复绿** |
| 闸自测 | **10 条全绿** |

## 五、已知限制

1. **只实现 symbol / test 两类锚点**,不实现 const / wire / manual。
   依据:Relay 实测 48 条漂移全部落在这两类(symbol 44 + test 4);
   另三类在 Relay 当前 spec 里推断不出任何锚点,实现了是零收益。
   **换仓可能不成立**——finance-engine 有 const 类漂移,移植前先跑全量扫描看分布。

2. **两份实现会漂**。TS 推断规则与 `../src/infer.py` 是人工对齐的,没有自动一致性测试。
   改了任一边,建议重跑一次逐条集合比对(方法见本文第一节)。

3. **不查 git 历史**,所以不区分"真漂移(曾存在后被删)"与"设计未落地(从未存在)"。
   闸不需要这个区分;要看分类去跑 `../src/scan.py` 全量扫描。

4. **不做 spec 内部矛盾检查**(`consistency.py` 那套),不做逐份 spec 覆盖率报表。
   闸只回答一个问题:漂移数增加了吗。体检是另一件事。

5. **抓不到"假勾"**。工具只能证明"符号/ID 指向真实存在的东西",
   不能证明"它断言的正是 spec 那句话"。
   实证:`session-management/tasks.md:46` 标称 Unit Test-002 验证
   `getStatus()` 返回含 `sessionId`,该测试真实存在且通过,
   但它断言的 6 个字段无一是 `sessionId`。这类需要理解散文语义,超出机器判定范围。
   **闸不假装能查这个。**

6. **`update-baseline.mjs` 在 Relay 里没验证过**。它需要已编译的 `spec-drift.js`,
   而 Relay 的 `tsc` 不把 `tests/` 编进 `dist/`。
   已验证可用的降基线方法写在 `install.md`,用的是一次性 vitest 文件。

7. **未绑锚点数(167 条)这道闸不管**。
   一份零锚点的 spec 漂移永远是 0——只看漂移会让最烂的 spec 显示为 100% 健康。
   闸拦的是"别变得更糟",**提升覆盖率要靠 `../SPEC-RULES.md` 那八条写法规则**,
   不是靠这道闸。这是能力边界,不是待办事项。

## 六、目录

```
spec-drift.ts                检测引擎(零依赖 TS,倒排索引)
spec-drift.test.ts           闸本体(挂载样板,拷进 Relay 的 tests/)
spec-drift.baseline.json     基线:Relay 当前 48 条漂移的稳定 key
test_gate.test.ts            闸自测 10 条(合成夹具,不依赖 Relay 状态)
update-baseline.mjs          降基线脚本(已编译场景/别的仓库用;Relay 未验证)
install.md                   给 Pulse 的挂载说明
```

> 自测用**合成仓库**做夹具,不读 Relay。
> 闸的测试不该依赖被测仓库的当前状态,否则 Pulse 一改 spec 我的自测就跟着变色——
> 那不是测试,是天气预报。
