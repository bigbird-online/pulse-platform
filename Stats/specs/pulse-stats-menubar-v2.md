# SPEC：PULSE Stats v2 — 双图标菜单栏与用量视图

> Version: 0.1.4 · Created: 2026-07-25 · Modified: 2026-08-01 ·
> **Status: 历史设计稿（部分已实现、部分已偏离）——不得当作现行契约读**
>
> **现行契约看** `stats-app-presentation.md`（面板呈现与传输，从代码提炼、机器闸守着）。
> 2026-08-01 实测的偏离（详表见该文 §9）：本文说两个 `MenuBarExtra`、代码只有一个；
> 本文 §2.2/§2.5 描述的两个面板 `RelayPanelView`/`HarnessPanelView` 现为**零引用死代码**；
> 本文 §3「上小下大两行槽」上屏失败后已改为单行合成 `Text`。
> 本文保留不删——它记录了判据为何这样立，但读它必须知道它已不是现行的。
> 版本史见 `git log`。0.1.1 = 宽度门 160→280（主身参照系取偏拍错，非形态变更）。
> 0.1.2 = 补记已采用的固定订阅账单归因契约；不改变 API 等效成本的 non-goal。
> 0.1.3 = 冻结 CC 账单归因的 event-level provenance gate。
> **本 spec 是 `pulse-stats.md`（v1）的增量**，不重复其内容。
> v1 的技术架构（§4）、安全与隐私（§8）、无障碍（§9）、回滚（§12）**全部继承**；
> v2 覆盖其 §5 信息架构，并新增数据层。
> 缘起：v1 交付的是「状态判定摘要器」，大鸟要的是 Stats 那种「菜单栏活数字 +
> 展开详情」。形态对了内核错了。2026-07-25 大鸟评「很糙」后细化到本版。

## 0. 一句话

菜单栏两个图标，**⚡ Relay**（算力）和 **🔧 harness**（协作），
各自常驻显示活数字与状态灯；点开逐层下钻到 agent 与 session。

## 1. Minimum Viable Spec

### 目标

让大鸟不点开就知道**要不要操心**，点开三秒内知道**该操心什么**。

### 边界

- 只读。不改 Relay / harness / policy / systemd / 任何生产源。
- 采集入口仍是**一条固定 SSH 命令**调 exporter（继承 v1 §4），不新增网络入口。
- 不做告警推送（escalation 通道是生产可用性专用，观察/用量属工程事务）。
- 不做「省着用」类的配额调度建议，只呈现事实。

### 验收

- 菜单栏两图标总宽 ≤ **280pt**。

  > **v0.1.1 修正**：初版写 ≤160pt，参照「Wi-Fi+电池+时钟 ≈ 120pt」——
  > **这个参照系是错的，是我（主身）拍脑袋定的**。2026-07-25 用 AppKit
  > 真字体度量（`NSFont.monospacedDigitSystemFont(ofSize:12)`）实测：
  > 系统那几项的**文本部分只有 67.6pt**（`100%`=33.8 + `13:52`=33.8），
  > 其余都是图标与内边距。拿混了图标的估值去约束纯文本+圆点布局，从根上不成立。
  >
  > 实测各态总宽：真实生产数据 217.1 / mockup 草样 206.0 / 全未知态 240.2 /
  > 理论最坏（`9999万100%` + `999/999/999`）266.5。即便把灯与间距压到极限
  > （灯 6pt、间距 2pt、无 slot 间隙），真实数据仍需 180.1pt，字号降到 11pt
  > 也要 172.8pt —— **§2.1 的五数字形态与 160pt 数学上不可兼得**。
  >
  > 另：ownermac 屏幕宽 2048pt，217pt 仅占 10.6%，原先「太宽」的担心不成立。
  >
  > **门定 280。** 中途先定 240、又议 260，两次都是在「刚好够」的边缘调数字，
  > 等于对着答案画靶子——只是靶心从未知态挪到峰值态。查 obs 全量历史后作罢：
  > 单日输出 token 峰值 **66.9 万**（07-19）、64.2 万（07-16）、63.2 万（07-24），
  > 已是 4 字符宽，距 `9999万` 只差一位；wi 周新建今日 58，而 07-22 单日建过 25 个，
  > 三位数并非假设。真实可达场景实测约 246（峰值日）与 254（三位数 wi），
  > 260 只余 6pt，下次峰值即破。
  >
  > 280 覆盖真实态 / 未知态 / 历史峰值 / 三位数 wi，并留一档余量。
  > 真实约束是屏幕宽度（2048pt），280 占 13.7% —— **把菜单栏宽度当成需要
  > 精算的约束，这个定位本身就是错的**。
  >
  > **形态不为这个错数让路** —— 大鸟明确要过「三个灯三个数，否则挂起多天的
  > wi 没人知道」，砍格会砍掉他点名要的能力。
- 单次刷新端到端 ≤ 10s（当前 exporter 基线 6–7s）。
- 所有数字可溯源到本 spec §3 的数据契约，**无估算值、无占位假数**。
- 拿不到的维度显式缺席或标 unknown，不以 0 或猜测填充。

### 明确不做

- 按模型分解、按模型/token 单价计算 API 等效成本 —— 见 §3.5，
  数据源缺 `model` 字段。固定订阅账单归因另见 §3.4。
- 后台定时刷新、Launch at Login、签名分发 —— 仍按 v1 §13 切片顺序，
  各自独立 gate。
- 写操作、重启、跨机协调。

## 2. 信息架构（覆盖 v1 §5）

### 2.1 菜单栏

```
⚡ ●39万 ●98%        🔧 ●60 ●3 ●3
   CC    Codex          周  日  完成
```

两个独立 `MenuBarExtra`，`.menuBarExtraStyle(.window)`。

> **实现约束**：两个 MenuBarExtra **必须共享同一个 store 实例**
> （`@StateObject` 提升到 App 层，用 `@ObservedObject` 注入）。
> 否则会各自触发一次 SSH，采集翻倍。这是本切片最容易踩的坑。

#### ⚡ Relay 栏

| 位 | 显示 | 灯色判据 |
|---|---|---|
| 1 | CC 今日输出 token（人类可读，如 `39万`） | 绿=进程可用且近期无 429；红=近期 weekly limit 429；灰=进程不可用 |
| 2 | Codex 周配额百分比 | <90% 绿 / 90–100% 黄 / 耗尽或撞墙 红 |

#### 🔧 harness 栏

| 位 | 显示 | 灯色判据 |
|---|---|---|
| 1 | 本周新建 wi 数 | 该批有 wi 停滞 >7 天 → 黄；>14 天 → 红 |
| 2 | 今日新建 wi 数 | 同上，看今日这批 |
| 3 | 今日完成 wi 数 | 完成率 ≥95% 绿 / <80% 红 / 中间黄；**当日新建为 0 → 灰** |

### 2.2 Relay 面板（点 ⚡）

五块，按「要不要动手」排序：

1. **要你操心的** — 平时空着写「没事」。有事一句人话，禁用内部代号
   （继承 auto-memory `cron-notify-three-tier-plain-language` 的措辞纪律）。
   触发项：Codex 配额 >90%、CC 撞 429、同一 agent 多 session、失败数 >0。

   > **措辞纪律做成可执行检查，不靠人工自查。** 禁词表至少含
   > `s1.` `s2.` `capability` `metric` `ledger` `invariants` `handoff`
   > `work_item` `unknown` `wi_`，对所有用户可见文案跑断言。
   > `UTC` / `Codex` 不在禁列 —— 前者是通用时间标准，后者是产品真名，
   > 纪律禁的是内部代号与自造隐喻，不是一切英文。
   >
   > 实测通过样例（2026-07-25，真 payload）：
   > 「pulse 同时开着 3 个对话 / 2 个可能是没关掉的旧对话」、
   > 「今天有 4 次回合没跑完」、「3 件活挂着超过一周没动」。
2. **算力** — CC 进程忙/闲 · Codex 配额条 + 重置时间 · 在线 bot 数。
3. **今天用量** — 见 §2.3。
4. **谁在烧** — agent 排行 + 可展开的 session 下钻，见 §2.4。
5. **投递质量** — 送达数/总数、重试分布、入站类型分布。

### 2.3 今日用量块

**输入与输出必须分列**，不合并成单一 token 数：

```
输入  6,903     走 resume，历史不重计
输出  390,952   占总量 98.3%
```

> 输入远小于输出是 resume 生效的正常态。**该比例本身是健康信号**：
> 输入异常暴涨说明 resume 断了、历史在重复计费。合并成一个数就看不见。

四格：入站数 / 回合数 / 失败数（非 0 标红）/ 回合中位时长。

一条 24 小时曲线：按小时聚合的输出 token。**这是本面板唯一需要曲线的地方**。

### 2.4 下钻层级

```
agent 排行（按输出 token 降序）
  └─ session 列表（该 agent 名下，含最后活动时间）
```

同一 agent 出现多个 session 时，在 agent 行标记（如 `3 session`）并在
「要你操心的」提示 —— 残留 session 是真实存在的现象（实测 pulse 今日 3 个），
现在没有任何地方会显示。

session 标注规则：最后活动在最近一个回合内 → `当前`；否则 → `残留`。

### 2.5 harness 面板（点 🔧）

1. **要你操心的** — 停滞 wi 数、超时待接 handoff。
2. **工作项** — 周/日/完成三行 + 待接 handoff。
3. **挂着不动的** — 停滞 >7 天的 wi 清单（天数 + 标题，可点进）。
4. **近 7 天完成率** — 折线 + 95%/80% 阈值虚线。
   完成率放这里而非菜单栏灯，因为**分母要足够大才有意义**（近 7 天 60 个）。

## 3. 数据契约

所有字段**已实测可得**（2026-07-25 于生产核验）。

### 3.1 Relay 侧

| 字段 | 来源 | 取法 |
|---|---|---|
| `cc.processes` / `busy` / `idle` | `GET http://127.0.0.1:8787/internal/cc-status` | `counts.*`，实测 `{busy:1, idle:1}` |
| `cc.bots_online` | `GET /internal/health` | `feishuBots`，实测 15 |
| `cc.uptime_seconds` | 同上 | `uptime` |
| `cc.rate_limited` | `~/.claude/projects/*/*.jsonl` | 近 24h 内是否出现 `"error":"rate_limit"` + `apiErrorStatus:429` |
| `codex.used_percent` | codex session jsonl | `payload.type=="token_count"` → `rate_limits.primary.used_percent`，取**时间戳最新**的一条 |
| `codex.resets_at` | 同上 | `rate_limits.primary.resets_at`（Unix 秒） |
| `codex.plan_type` | 同上 | `rate_limits.plan_type`，实测 `plus` |

> **Codex 两机都要扫**：vultr `/home/pulse/.codex/sessions/`（有 `/usr/bin/codex`）
> 与 ownermac `/Users/owner/.codex/sessions/`。配额是**账号级共享**，
> 显示一个值，但须取两边最新的那条。
> 实测 2026-07-25：`used_percent=98.0`，`resets_at=2026-07-29T02:34:49Z`。

### 3.2 用量侧（`~/.relay/obs/*.jsonl`）

事件类型与字段（实测今日 288 条）：

| 事件 | 条数 | 关键字段 |
|---|---|---|
| `turn.end` | 61 | `agent` `session` `poolKey` `inputTokens` `outputTokens` `durationMs` `isError` `stopReason` |
| `req.inbound` | 75 | `who`（飞书 open_id）`msgType` `channel` |
| `delivery.receipt` | 149 | `status` `attempts` `channel` |
| `degen.observed` | 3 | `kind` `disposition` |

聚合规则：
- 「今日」= UTC 自然日（与 obs 的 `ts` 一致，**不做时区转换**，面板注明 UTC）。
- agent 排行按 `outputTokens` 降序。
- session key = `(agent, session)`，显示 session 前 8 字符。
- 中位时长取 `durationMs` 的中位数，非平均（长尾会拉偏平均）。

### 3.3 harness 侧（`.harness/`）

| 字段 | 来源 | 注意 |
|---|---|---|
| wi 三数 | `work-items/*.yaml` | **id 字段名是 `work_item_id`，不是 `id`** |
| wi 状态 | 同上 `status` | 取值：`completed` `in_progress` `draft` `ready` `failed` |
| 停滞天数 | `updated_at` 缺失则回落 `created_at` | 仅对 `in_progress`/`ready`/`draft` 计算 |
| 待接 handoff | `handoffs/*.yaml` 的 `ack_state=="pending"` | 实测 5 |

坏 YAML 必须跳过并计数，**不得整体失败**（继承 v1 的失败语义：
局部不可读 → 该项 unknown，不是全盘 error）。

### 3.4 固定订阅账单归因

本节定义的是**固定订阅账单归因**：回答「本月固定订阅账单按现有证据可归到谁」。
它不是实际 API 等效成本、按 model/token 单价计费、边际成本，也不是 ROI、产出价值或
agent 效率判断。输出 token 只作为固定账单的确定性分配权重，不能写成「agent 花掉了
这些 token 的 API 费用」。

#### 账单组件与期间

| 组件 | 当前金额 | 金额来源 | agent 归因来源 |
|---|---:|---|---|
| Claude Code（CC）固定订阅 | USD 250 / 月 | owner-reviewed 固定订阅账单配置 | 能证明来源为 CC 的 MTD `turn.end.agent + outputTokens` |
| Codex 固定订阅 | USD 20 / 月 | owner-reviewed 固定订阅账单配置 | 当前无可验证的 Codex-agent source，必须未归因 |

- 金额不能从 token、quota 或 API 单价反推；账单配置至少带组件 id、币种、月金额、
  生效期间与来源引用。金额变化须先更新该可审查配置。
- 配置的 `effective_from` / `effective_until` 均为 `Asia/Shanghai` 自然日，首尾
  两日都包含；`effective_until=null` 表示无结束日期。
- 期间是 `Asia/Shanghai` 自然月。快照同时给出月份、`days_covered` 和
  `days_expected`；后者是月初至快照日（含当日）的自然日数。
- covered day 必须能证明相应日分区存在、可读且覆盖完整；缺失/不可读分区不按零使用
  补齐。若生产者不能证明一个空分区完整，该日也不能计 covered。

#### 可归因池、未归因池与失败语义

- CC USD 250 是**候选可归因池**。当前唯一 event-level 放行信号是同一条
  `turn.end` 显式携带 `billing_component_id="claude_code"`；`runtime="cc"`、
  provider/model、文件名或「来自 Relay」单独都不足，因为 CC runtime 仍可能走
  非订阅 provider 或 fallback。缺少该字段必须标 `insufficient`，不得猜测。
- 放行记录还必须在同一条事件内携带可解析的 `ts` 与非负整数 `outputTokens`；
  只有 `agent` 也有效时才进入命名 agent 行。`agent` 缺失/无效时沿用下述规则：
  已知 output token 进入未归因 denominator，不生成伪 agent。
  `billing_component_id` 只作归因输入证据，不进入 snapshot 或 UI。
- Codex USD 20 在当前数据契约下全部进入**未归因池**，不得摊给 Relay agents。
- `days_covered < days_expected` 时，若已有可读、正值的有效 CC evidence，可按已覆盖
  观测给出 provisional 分配，但必须标 `available + descriptive` 并展示覆盖比；
  不得补零、外推或写成完整月份。
- 无有效 CC evidence、零 agent、有效 output token 总和为零、或 CC source
  不可读/来源不可证时，不创建均分行或 `unknown` agent 行。只有每个 expected day
  都有完整、可读且来源可证的零样本，才能发布「本期验证为零」；缺天的零样本不能
  表述为「零使用」或伪造一个确定的归因结果。
- 已知 output token 但 agent id 缺失/无效的 CC evidence 不生成伪 agent；它对应的
  金额份额留在未归因池。无法解析 output token、月份或来源的 usage record 使本次
  CC 归因不可验证，不得静默跳过后仍宣称完整。

#### 确定性分配与守恒验收

1. 只聚合期间内、来源可证、字段有效的 CC evidence；同名 agent 先合并
   `outputTokens`，分母包含 agent id 缺失但 token 有效的 CC output。
2. 以**整数美分**分配候选 CC 池。先取每个 agent（以及必要的未归因份额）的精确比例
   下取整，余下美分用 largest-remainder 分配；余数相同按稳定 agent id 升序，
   未归因桶使用固定排序键。
3. 展示顺序固定为 `outputTokens` 降序、agent id 升序；不得有重复 agent、负数、
   `NaN` 或依赖输入文件遍历顺序的结果。
4. 对每个 `available` attribution bundle，必须以配置组件动态求和并精确满足
   （不得用近似断言）：
   `configured_total_cents = Σ components.amount_cents`
   且 `configured_total_cents = attributed_cents + unattributed_cents`。
   `Σ by_agent.attributed_cents = attributed_cents`。当前配置实例是
   `25000 + 2000 = 27000 cents`，不是不可变常量。
   - 当前 CC evidence 可分配且 agent id 完整时：agents 合计 25000 cents，
     Codex 未归因 2000 cents。
   - 当前有缺失 agent 的有效 CC evidence 时：agents 与其 CC 未归因份额合计
     25000 cents；再加 Codex 未归因 2000 cents。
   - 完整覆盖且验证为零时发布 agents 空 rows、`attributed_cents=0`、
     `unattributed_cents=configured_total_cents`；这是已验证的零，不是 unknown。
   - 缺天且零样本时不发布数值归因结果；使用 `unknown + insufficient`，
     不能用 `0 + configured_total_cents` 冒充完整覆盖。

#### 唯一状态映射

账单配置事实与 usage 归因结果是两个独立状态域，不得互相降级：

- `billing.mtd.components` 与 `billing.mtd.total_cents` 是账单配置事实。配置可读且解析
  成功时始终为 `available + validated`；只有配置不可读或解析失败时才为
  `unknown + unavailable`、`value=null`。usage 缺天、零样本或来源不可证不影响
  这两个 facts，所以 UI 仍可显示已知组件与总额；Codex 组件的 attribution 仍明确为
  `unattributed_no_agent_source`。
- `billing.mtd.period`、`billing.mtd.by_agent`、`billing.mtd.attributed_cents`、
  `billing.mtd.unattributed_cents` 组成 attribution bundle。四者必须使用同一对
  `status + quality`，只允许：

| 证据状态 | `status` | `quality` | `value` |
|---|---|---|---|
| 账单配置、CC 来源与期间完整可证，且覆盖完整（含验证为零） | `available` | `validated` | 按 §4 形状发布；验证为零时 period=`full`、by_agent=`[]`、attributed=`0`、unattributed=`total` |
| CC 来源与字段有效，但 `days_covered < days_expected` | `available` | `descriptive` | 按 §4 形状发布 provisional 结果 |
| 缺天且零样本，或来源可读但不足/不能证明属于 CC | `unknown` | `insufficient` | 四个 `value` 均为 `null` |
| 账单或 usage 来源不可读，或任一必要字段解析失败 | `unknown` | `unavailable` | 四个 `value` 均为 `null` |

不得发明 `partial` quality；`partial` 只可作为
`billing.mtd.period.value[0].coverage` 的覆盖标签。`reason` 必须指出具体缺口。

### 3.5 拿不到的（不编）

| 想要 | 为何不做 |
|---|---|
| 按模型分解 | obs 事件**无 `model` 字段**（已 grep 全量确认） |
| 按模型/token 单价计算 API 等效成本 | 依赖模型名 + 单价，前者都没有 |

要做需先给 Relay `relay-request-trace.ts` 的 `turn.end` 增记模型名 ——
**属独立改动，不在本切片**。补上后本 spec 的 §2.3 可增列「按模型」维度，
界面结构不用改。

## 4. Snapshot schema 演进

沿用 `pulse.monitoring.snapshot.v2` 的顶层结构，在 `metrics` 数组内新增条目
（v1 §6 已定义 metric 结构：`id` `capability_id` `value` `unit` `window`
`quality` `status` `observed_at`）。

新增 metric id 命名空间：

```
relay.cc.processes / relay.cc.busy / relay.bots_online / relay.cc.rate_limited
relay.codex.used_percent / relay.codex.resets_at
usage.today.input_tokens / usage.today.output_tokens
usage.today.turns / usage.today.failures / usage.today.duration_p50
usage.hourly.output_tokens        # 数组，24 点
usage.by_agent                     # 数组
usage.by_session                   # 数组
harness.wi.week_created / harness.wi.day_created / harness.wi.day_completed
harness.wi.stalled                 # 数组，含 work_item_id/title/days
harness.handoff.pending
delivery.today.sent / delivery.today.attempts_histogram
billing.mtd.period / billing.mtd.components / billing.mtd.by_agent
billing.mtd.attributed_cents / billing.mtd.unattributed_cents / billing.mtd.total_cents
```

账单归因 metrics 的 wire shape 冻结如下。全部
`capability_id="s1.request_trace_capture"`、`window="month_to_date_cst"`；
币种固定为 `USD`，金额一律是 JSON 整数 cents，不允许浮点美元。

| metric id | `value` wire shape | `unit` |
|---|---|---|
| `billing.mtd.period` | rows：`[{"month":"YYYY-MM","timezone":"Asia/Shanghai","days_covered":int,"days_expected":int,"coverage":"full|partial"}]` | `list` |
| `billing.mtd.components` | rows：`[{"component_id":string,"amount_cents":int,"currency":"USD","attribution":string}]` | `list` |
| `billing.mtd.by_agent` | rows：`[{"agent":string,"output_tokens":int,"attributed_cents":int}]` | `list` |
| `billing.mtd.attributed_cents` | JSON integer number | `cents` |
| `billing.mtd.unattributed_cents` | JSON integer number | `cents` |
| `billing.mtd.total_cents` | JSON integer number | `cents` |

`period` 使用 singleton rows 而不是任意 object map：现有 Swift `MetricValue`
只支持 number / bool / number-array / rows / numeric counts map，rows 内的
`MetricScalar` 才支持 string / number / bool / null；上述三种 rows 因而可由现有
decoder 解码。当前组件 id 与 attribution 值固定为：

```json
[
  {
    "component_id": "claude_code",
    "amount_cents": 25000,
    "currency": "USD",
    "attribution": "cc_mtd_output_share"
  },
  {
    "component_id": "codex",
    "amount_cents": 2000,
    "currency": "USD",
    "attribution": "unattributed_no_agent_source"
  }
]
```

发布时六个 metric 必须同时存在，但状态分成两个域：

- `components` / `total_cents` 按账单配置事实发布，`sample_count` 为 `null`；
  配置可读时其值不因 attribution bundle unknown 而消失。
- attribution bundle 四个 metric 在 `available` 时按 §3.4 的确定性算法和动态守恒
  公式填值；在 `unknown` 时四值为 `null`，不得以空 rows 或 0 代替未知。
  `billing.mtd.period.coverage` 只允许 `full|partial`；unknown 不另造第三个 coverage
  值。四者的 `sample_count` 是本次归因实际纳入的、来源可证且字段有效的 CC
  `turn.end` 条数：attribution bundle 为 `available` 时使用非负整数，其中 `0`
  只允许 `coverage=full` 且来源完整可证的 verified-zero；
  `unknown + insufficient`（含缺天零样本、来源不可证）与
  `unknown + unavailable` 的 `sample_count` 一律为 `null`，即使底层恰可数为 0。

**不 bump major**：只增字段不改语义，v1 客户端读到未知 metric 应忽略而非报错。
若客户端当前不是这个行为，本切片须先修容错再加字段。

## 5. 历史存储

曲线需要历史，当前采集是一次性的。

- 落盘：`~/Library/Application Support/PulseStats/history-v1.jsonl`，
  权限 0600、目录 0700（同现有 cache）。
- 每次成功刷新追加一行精简记录（**只存聚合值，不存 session 明细**，
  避免文件膨胀与隐私面扩大）。
- 保留 7 天，超期行在写入时顺带裁剪。
- 文件损坏 → 丢弃重建，**不阻断当次刷新**（曲线暂缺不影响当下值）。

## 6. 隐私边界（在 v1 §8 之上追加）

- 飞书 `who` 是 open_id，面板**只显示计数与分布，不显示 open_id 本身**。
- session id 只显示前 8 字符。
- wi 标题会进面板（大鸟自己的工作项，非租户数据）；
  **租户消息内容、prompt、token 明文一律不进 snapshot**（v1 既定，不放宽）。
- 历史文件同样受此约束。

## 7. 测试矩阵（在 v1 §11 之上追加）

| 用例 | 期望 |
|---|---|
| 两个 MenuBarExtra 共享 store | 单次刷新只触发**一次** SSH |
| Codex session 文件缺失 | 配额显示 unknown，灯灰，**不崩溃、不显示 0%** |
| 两机 Codex 都有数据 | 取时间戳更新的那条 |
| obs 目录为空 | 用量块显示 unknown，曲线空白 |
| 当日新建 wi 为 0 | 完成灯**灰**，不红（0/0 无分母） |
| 存在停滞 >14 天 wi | 周灯红 |
| 坏 YAML 混入 work-items | 跳过并计数，其余照常聚合 |
| 历史文件损坏 | 丢弃重建，当次刷新成功 |
| 历史超 7 天 | 写入时裁剪，文件不无限增长 |
| CC 近 24h 有 429 | CC 灯红 |

## 8. 与 Slice 1 的关系

Slice 1（观察中，07-26 判 go/no-go）验证的是**链路**：
生产采数 → SSH 回传 → 本地缓存 → 菜单栏渲染。那是地基，v2 在其上加数据层。

**必须先判掉 Slice 1 的 go/no-go 再改 App 二进制**，否则观察期样本口径不一致。

## 9. 切片顺序

| 切片 | 内容 | gate |
|---|---|---|
| v2-A | exporter 加 §3 全部指标 + §4 schema 扩展 | 离线测试 + 生产 canary 双跑幂等 |
| v2-B | 历史存储（§5） | 损坏/裁剪/权限用例通过 |
| v2-C | 双 MenuBarExtra + 面板重做（§2） | 单次刷新一次 SSH；形态对照本 spec |
| v2-D | 下钻交互（§2.4） | 展开/收起、多 session 标记正确 |

A 与 B 可并行；C 依赖 A 的 schema；D 依赖 C。

## 10. 风险

| 风险 | 处置 |
|---|---|
| 两个 MenuBarExtra 各自刷新导致采集翻倍 | store 提升到 App 层，测试守住 |
| exporter 耗时随指标增加而变长（当前 6–7s，门 20s） | 每加一类指标测一次端到端；超 12s 须优化再继续 |
| ack-probe 已知抖动（11–13s vs 4s 子超时）污染 runtime | 已知问题，go 后第一批修，见 `memo/2026-07/2026-07-25-stats-observation-blocked-root-cause.md` |
| 生产机单核 1.6Gi 过载致 SSH 超时 | 平台级问题，与 Relay OOM 同根，不在本切片解决 |
