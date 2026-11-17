# spec-anchor —— spec 漂移检测器（阶段一）

把 spec 里的契约声明绑到**机器可查的锚点**上，一条命令查出哪些声明已经不成立。

> **成熟度（诚实）**：`building`。已在**两个真实仓库**跑通——PULSE Relay（TypeScript/vitest，
> 80 份 spec / 806 条声明）与 finance-engine（Python/pytest，75 份 / 1207 条），
> 两仓 broken findings 逐条回源复核**零误报**，自测 33 条全绿。
>
> 换仓实测证明了一件重要的事：**锚点思想通用，写法必须按仓库方言适配**。
> 未适配前 finance-engine 覆盖率从 48.3% 塌到 5.8%，适配 pytest 方言后才恢复。
> 第三个项目大概率仍要加方言规则。勿据此宣称"开箱通用"。

## 它解决什么

不是"没文档"，是**文档不可信**——半准的文档比没文档更害人：没文档你会去读代码，半准文档把你带沟里。

2026-07-25 在 PULSE Relay 实测到的病根：

| 同一批 302 次代码提交，同时改了什么 | 比例 |
|---|---|
| 带 tests | **62.6%** |
| 带 specs | **9.3%** |

同一个 Agent、同一套纪律。测试跟得上，spec 跟不上。差别不是自觉，是**测试有机器闸，spec 没有**。

所以本工具的核心主张：**别给 spec 另立一套文档纪律，让它搭上测试这趟已经在跑的车。**

## 用法

```bash
python3 src/scan.py --repo <仓库根> \
  --spec-glob 'specs/**/*.md' \
  --json run/drift-map.json \
  --md  run/drift-map.md
```

参数：

| 参数 | 说明 |
|---|---|
| `--repo` | 目标仓库根目录（必填） |
| `--spec-glob` | spec 文件匹配式，默认 `specs/**/*.md` |
| `--test-glob` | 测试文件匹配式，可重复，默认 `tests/**/*.ts` 等 |
| `--json` / `--md` | 输出路径；都不给则 markdown 打到 stdout |

自测：`python3 src/test_anchors.py`

## 五种锚点

按「能不能不靠人判断就得出真/假」选取：

| 类型 | 绑什么 | 抓哪类漂移 |
|---|---|---|
| `test` | **测试用例名**（不是文件） | 契约是否真被测试覆盖 |
| `symbol` | 导出符号 / 接口字段 | 接口消失、改名 |
| `const` | 常量或默认值 | **值漂移**（实测 timeout 12×/20× 失真） |
| `wire` | 某符号在生产代码里有无调用点 | **空接线**（有定义、有测试、零调用点） |
| `manual` | 人类可观察现象 | 不判定，只登记 |

写法（两种等价）：

```markdown
<!-- anchor: test tests/metering.test.ts :: "records result message to daily JSONL file" -->
`@anchor const RATE_LIMIT_MAX_RETRY = 8`
`@anchor wire checkBalance :: wired`
`@anchor manual 每进程约 250MB :: 用 top 观察 RSS 持续 10 分钟`
```

`manual` 是**刻意保留的诚实出口**。实测 32% 的断言是性能/属性类（「每进程约 250MB」「无跨租户泄漏」），
全仓无计时器无指标。标成 manual 好过假装能自动验——**假装可验正是假勾的温床**。

## 存量 spec 不用重写

工具会从既有写法**推断**隐式锚点（`**测试文件**：\`tests/x.test.ts\``、`**Unit Test-002**：…`、
反引号包的 `someFn()`、常量赋值），所以能直接体检没改过的老 spec。

要求先把 80 份 spec 按新语法重写才能体检，等于要求先治好病才能看病。

推断项在报告里与显式锚点**分开统计**——推断有误判风险，不能和人明确写下的锚点混为一谈。

## 读数怎么看

四个判定：

- `holds` 锚点成立
- `broken` 锚点失效 = **漂移**
- `unverifiable` 不可自动验（manual、或锚点粒度不足）
- `error` 校验器自身出错（**与漂移严格区分**，否则工具故障会伪装成发现）

`broken` 再分三类，处置完全不同：

| 类别 | 含义 | 该做什么 |
|---|---|---|
| **真漂移** | 符号曾存在后被删 | spec 落后于代码，改 spec |
| **设计未落地** | git 全历史从未出现 | spec 描述了没造出来的东西，补实现或降级 spec |
| **引用失效** | spec 点名的测试文件不存在 | 查是删了还是没写 |

### 最重要的一条：覆盖缺口与漂移必须并列看

**一份零锚点的 spec，漂移数永远是 0。** 只报漂移数会让最烂的 spec 显示为 100% 健康——
那正是假勾的机制。所以总览里「未绑锚点」这个数字比「漂移」更重要。

## 能力边界（硬声明）

**锚点解析只能证明「这个 ID/符号指向真实存在的东西」，不能证明「它断言的正是 spec 那句话」。**

实证：`session-management/tasks.md:46` 标称

> `- [x] Unit Test-002：pool.getStatus() 返回的条目包含 sessionId 字段（已覆盖，确认）`

`[Unit Test-002]` 确实存在于 `tests/pool.test.ts:2314`，测试通过。但它断言的 6 个字段
（tenantId/state/pid/lastActivity/spawnedAt/restartCount）**无一是 sessionId**，
且 `ProcessStatusInfo` 在**全部历史版本中从未有过该字段**——这是编造，不是过期。

这类「引用真实测试的假勾」需要理解散文语义，超出机器判定范围。工具的处理是：
判 `holds` 但**强制标注 `needs_human_review`**，在报告里单列一节。
不假装能查，也不假装查过了。

## 两仓实跑读数（2026-07-25）

| | PULSE Relay | finance-engine |
|---|---|---|
| 语言 / 测试框架 | TypeScript / vitest | Python / pytest |
| spec 文件 | 80 | 75 |
| 契约声明 | 806 | 1207 |
| 已绑锚点 | 639（79.3%） | 149（12.3%） |
| 漂移 broken | 47 | 65 |
| ├ 真漂移（曾存在后被删） | 16 | 0 |
| ├ 设计未落地（从未写进代码） | 27 | 37 |
| └ 引用/测试失效 | 4 | 28 |
| **勾选率** | **63%（390/616）** | **33%（356/1064）** |
| spec 内部矛盾 | 0 | 2 |

两仓 broken 均逐条回源复核，**零误报**。

> 覆盖率从初版的 48.3% / 5.8% 提到现在，靠的是识别**裸符号**（没加反引号的
> `handleStart`、`calculateCost` 之类）。详见 `SPEC-RULES.md` 规则二——
> 这也是本项目最反直觉的发现：所谓"六成是不可锚的散文"是伪结论，
> 大部分是可锚内容的不规范写法。

### 换仓换出来的三件事

**一、覆盖率低不一定是工具不行，可能是仓库真没绑。**
finance-engine 的 5.8% 是诚实读数：它 1207 条声明里大部分是 `- [ ]` 未勾项，
而未勾项本就没声称成立，不该被当作"已验证的契约"去绑锚点。

**二、勾选框的可信度是仓库属性，不能跨仓当统一读数。**
Relay 勾选率 63%（还在维护），finance-engine 33%（已弃用）。抽查证实后者
`income-manager/tasks.md` 头部写 `status: "verified"`、正文 94 个框只勾 1 个，
而 `ProfitSummary` 已实现、`calculate_revenue` 有 3 个测试——**活干完了，框没人勾**。
所以工具增加了 `consistency.py`：不看代码，只查 spec 自己跟自己矛不矛盾。

**三、同一套三件套模板，两个仓库的失败模式相反。**
Relay 是"勾了但没做"（假勾），finance-engine 是"做了但没勾"（弃用）。
只跑一个仓会把其中一种当成全部——这正是不能用单一样本定产品标准的理由。

### 早期误报修正记录

Relay 首版曾报 128 条漂移，其中 106 条是同一类误报：spec 用了 `Unit Test-00x` 编号，
但该测试文件整体没采用 `[ID]` 标签约定——那是**从未绑定**（覆盖缺口），不是**绑了但断了**（漂移）。

finance-engine 首版曾报 62 条，其中 28 条是重复计数：同一个 `test_xxx()`
既产出 test 锚点又产出 symbol 锚点。已去重。

## 目录

```
src/anchors.py       锚点解析与五种校验器
src/infer.py         从既有写法推断隐式锚点（含 vitest / pytest 两种方言）
src/consistency.py   spec 内部矛盾检查（不看代码）
src/scan.py          全量扫描、漂移图渲染
src/test_anchors.py  自测 33 条
run/drift-map.{json,md}      PULSE Relay 跑动产物
run/ifm-drift-map.{json,md}  finance-engine 跑动产物
```

## 真漂移 / 设计未落地 的分类特别容易错

这两类**处置方向相反**——真漂移该改 spec，设计未落地该补实现或降级 spec。
判据是"这个符号在**本项目自己的代码**历史里出现过吗"，实现时踩过两个坑：

1. **git pathspec 相对 cwd，不是相对仓根。** Relay 没有自己的 `.git`（嵌在 PULSE 内），
   但因为 `cwd` 就设在 Relay，传裸 `src/` 是**对的**。
   曾有人报"要加 `Bigbird/Relay/` 前缀"，照做反而全查不到——
   在 PULSE 仓根验证会得 0，在 Relay 目录下验证得 3，**换个 cwd 就能证伪**。

2. **monorepo 里兄弟项目的同名符号不算数。** `markPaid`、`ensureTenantLinuxUser`
   在 `Bigbird/Platform/` 真实存在，但对 Relay 而言从未落地。
   全仓回退搜索必须按项目路径前缀过滤，否则 16 条里会错 5 条。

两条都有回归测试锁住（`TestGitPathspec` / `TestMonorepoSibling`）。

## 性能

单仓全量扫描实测（取包目录，非作者磁盘）：

| 仓库 | 规模 | 耗时 |
|---|---|---|
| PULSE Relay | 80 spec / 806 声明 / 24k 行源码 | **1.8s** |
| finance-engine | 75 spec / 1207 声明 | **2.5s** |

早期版本慢得不可用（finance-engine 9 分钟、Relay 86 秒），两处瓶颈都是
**O(锚点 × 行数)**，与语言无关：

- `_ever_existed` 每个缺失符号跑一次 `git log -S` → 改一次性历史标识符索引
- `_grep` 每个符号全表扫（476 × 48k = 2286 万次正则）→ 改倒排索引

两轮优化累计约 47×，**两仓判定结果零变化**（有等价性测试锁住）。
慢会让工具被弃用，所以这不是锦上添花——是可用性门槛。

## 移植到新仓库要做什么

1. 确认 `--spec-glob` / `--test-glob` 指对
2. **看覆盖率**：若骤降，多半是该仓的测试引用方言没被 `infer.py` 认出，
   先 grep 几份 spec 看它怎么写测试，再加一条正则
3. 跑完**逐条回源复核 broken**，别直接采信——本工具两次首跑都出过成批误报

## 写 spec 的规则

见 **`SPEC-RULES.md`** —— 八条规则，每条都从两仓漂移图读出来，附证据。
没有证据支撑的一律没写进去；唯一一条"待验证"（外行可验收）已显式标注。

核心三条：

1. **每条声明必须可锚 / 可数 / 标 manual**，不许有裸断言
2. **提到代码就加反引号** —— 一个反引号换回三成覆盖率
3. **完成的证据是锚点跑得过，不是框打了勾** —— 两仓从相反方向证明打勾不承载信息
