# SPEC：PULSE Stats — 计费与用量口径

> version: `0.1.5`
> status: `reverse_engineered_from_code`
> authority: PULSE Stats 计费与用量契约（从现存代码提炼，非设计文档）
> owner: Pulse
> modified: `2026-08-16`
> upstream: `specs/pulse-stats.md` v0.1.2（产品层；本文件是其 §3.4「固定订阅账单归因」的展开）
> code_root: `/Users/owner/Coevolution/Bigbird/PULSE/Bigbird/Stats/exporter`
> 版本史：只看 git log，不在文件头复制 changelog

## 0. 这份 spec 治什么

Stats 有 9382 行 Python + 8712 行 Swift（2026-08-01 时点实测，行数会漂），只有一份 README，零 spec。大鸟 2026-07-31 判词
「越优化问题越多」＝设计漂移。计费口径是最容易漂且后果最重的一块：装机当天报出的四处
真 bug 里，「额度降级丢弃」和「Agent 月度用量整块消失」都在这一带，而**代码门与 fixture
全发现不了**——新实现的「证据不足」在它自己的测试里就是预期行为，全绿。

本文件把 `billing.py` / `billing_core.py` / `usage_sources.py` 用量与额度部分的真实契约
固化成可机检声明。每条要么绑代码符号、要么带具体数值、要么显式标 `manual`。

**边界**：只覆盖计费与用量/额度。incident reducer、harness 计数、transport/cache、
Swift App 侧不在本文件内。

**金额边界**：本文出现的 `$270` / `$20` 是当前代码与 `subscriptions.json` 的**配置值**，
不是 2026-08-16 已核验的外部实付。现金账必须另取 owner 发票；缺凭据时保持 `unknown`。

---

## 1. 两套并存的月度指标，各答各的问题

这是本域最容易被误读的一点，先说清楚。`billing.mtd.*` 与 `usage.mtd.*` 是**两套独立指标**，
数据源不同、严格程度不同、失败表现不同，**不得互相替代**。

| | `billing.mtd.*` | `usage.mtd.*` |
|---|---|---|
| 回答的问题 | 配置总额由哪些组件构成、精确到分归给谁 | 输出 token 花在谁身上 |
| 实现 | `build_billing_metrics()` | `collect_month_to_date()` |
| 数据源 | `config/subscriptions.json` + obs 中**带 `billing_component_id`** 的 `turn.end` | obs 中**全部** `turn.end` |
| 单位 | 整数分（`cents`） | token + 浮点美元（`amortized_usd`） |
| 归因语义 | 账单归因，守恒到分 | 按输出占比的**估算**，不是账单 |
| 证据不足时 | 整块 `unknown`/`insufficient` | 照常出数 |

`usage.mtd.*` 曾在 2026-07-30（commit `0ef3260`）被 `billing.mtd.*` 顶掉整块删除，面板上
「每个 Agent 花了多少」就此没了下文；大鸟 07-31 指出后于 `live_sources.py` 恢复。
两套并存是**当前设计**，不是临时状态。

`@anchor symbol build_billing_metrics`
`@anchor symbol collect_month_to_date`
`@anchor wire collect_month_to_date :: wired`
`@anchor wire build_billing_metrics :: wired`

**呈现侧硬要求（`manual`）**：`usage.mtd.by_agent` 的 `amortized_usd` 必须标明是估算，
不得让读者当成账单归因。
`@anchor manual usage-mtd-amortized-is-estimate :: 点开菜单栏面板，看 usage.mtd.by_agent 那块的文案是否写明「估算/按输出占比」，若只写金额即为违约`

---

## 2. 计费模型：固定订阅制怎么摊

### 2.1 组件配置是真源，金额不硬编码

订阅由若干 `SubscriptionComponent` 组成，配置文件为
`Bigbird/Stats/exporter/config/subscriptions.json`，由 `SUBSCRIPTIONS_CONFIG_PATH` 指向。

`@anchor symbol SubscriptionComponent`
`@anchor symbol SUBSCRIPTIONS_CONFIG_PATH`
`@anchor symbol load_subscription_components`

配置顶层**只允许**两个 key：`schema_version` 与 `components`，多一个少一个都抛
`BillingConfigError`。`schema_version` 必须**恰等于 1**。

`@anchor symbol BillingConfigError`
`@anchor symbol COMPONENT_KEYS :: frozenset`

每行组件的字段集合恰为 7 个（`COMPONENT_KEYS`）：`component_id`、`amount_cents`、
`currency`、`effective_from`、`effective_until`、`source_ref`、`attribution`。

校验规则（全部由 `load_subscription_components()` 强制，任一不满足即抛）：

- `component_id`：非空字符串，且 `component_id == component_id.strip()`；**全表不得重复**。
- `amount_cents`：非负整数，`bool` 显式排除（`isinstance(cents, bool)` 判掉）。
- `currency`：**只允许字面量 `"USD"`**，其它一律拒。
- `effective_from` / `effective_until`：`YYYY-MM-DD`，且 `parsed.isoformat() != value` 即拒
  （堵住 `2026-7-1` 这类能被 `fromisoformat` 吃下但格式不规范的写法）。`effective_until`
  可为 `None`（长期有效）；不为 `None` 时不得早于 `effective_from`。
- `source_ref`：非空且无首尾空白——**这是「大鸟审阅过」的凭据**，不允许留空。
- `attribution`：只允许 `VALID_ATTRIBUTIONS` 两个值之一，见 §2.2。

`@anchor symbol load_subscription_components`
`@anchor symbol VALID_ATTRIBUTIONS :: attribution not in VALID_ATTRIBUTIONS`
`@anchor test Bigbird/Stats/exporter/tests/test_billing_core.py :: test_invalid_config_is_rejected_strictly`

### 2.2 两种 attribution，决定钱进哪个池子

`@anchor const ATTRIBUTABLE = "cc_mtd_output_share"`
`@anchor const UNATTRIBUTED = "unattributed_no_agent_source"`
`@anchor symbol VALID_ATTRIBUTIONS`

- `cc_mtd_output_share`：可归因池。这部分钱按各 agent 的 MTD 输出 token 占比分摊。
- `unattributed_no_agent_source`：不可归因。该组件**整额**计入 `unattributed_cents`，
  永不进入任何 agent 行。

生产配置（`config/subscriptions.json`，2026-08-16 复读；只证明配置，不证明发票）：

| component_id | amount_cents | attribution |
|---|---|---|
| `claude_code` | `25000` | `cc_mtd_output_share` |
| `codex` | `2000` | `unattributed_no_agent_source` |

即配置声明 $250 CC 可归因 + $20 Codex 不可归因 = $270/月。`verify-live-snapshot.py` 里有
一条专门盯住 Codex 不许出现在 agent 行的检查（"no codex agent row (its $20 stays unattributed)"）。
2026-08-16 live Codex session 的 `plan_type` 已观察到 `pro`，与 2026-07-29 的 `$20`
`source_ref` 不一致；实付在拿到 owner 发票前是 `unknown`，不得直接用官网起售价覆盖。

`@anchor manual subscription-invoice-truth :: 读 config/subscriptions.json 的 source_ref，再读最新 Codex session token_count 的 plan_type，最后对 owner 发票；三者分别报告，冲突时现金账为 unknown`

### 2.3 生效期是**闭区间**，按北京日判定

`load_subscription_components()` 的筛选条件是 `start <= current_day and (end is None or current_day <= end)`，
其中 `current_day = now.astimezone(DISPLAY_TZ).date()`。**两端都含**：`effective_until`
等于今天的组件当天仍然生效。

`@anchor const DISPLAY_TZ = timedelta(hours=8)`
`@anchor test Bigbird/Stats/exporter/tests/test_billing_core.py :: test_inactive_rows_are_filtered_and_dates_are_inclusive`

**校验在筛选之前**：所有行先逐条验完（含未生效/已过期的行），再筛出当日生效的。一行烂配
即使不生效也会让整份配置不可用。

筛完若 `active` 为空 → 抛 `BillingConfigError("subscription config has no active components")`。
但**某个具名组件缺席不是错**：只剩 `codex` 一行也能正常加载。
`@anchor test Bigbird/Stats/exporter/tests/test_billing_core.py :: test_active_empty_is_invalid_but_named_component_may_be_absent`

`now` 必须 timezone-aware，否则抛 `BillingConfigError("collection time must be timezone-aware")`。

---

## 3. 什么算 attributed / unattributed —— 证据门

### 3.1 唯一合法证据：同事件的 `billing_component_id`

`collect_mtd_evidence()` 扫 obs 目录，只认满足**全部**下列条件的 `turn.end`：

1. `event["evt"] == "turn.end"`；
2. `event["ts"]` 可解析、带时区、且 `<= now`（未来事件跳过）；
3. 该事件按 `DISPLAY_TZ` 折算后的 `%Y-%m` 等于当前北京月；
4. **事件自身**带 `billing_component_id`，且值为 `"claude_code"`；
5. `outputTokens` 是非负 `int`（`bool` 排除）。

`@anchor symbol collect_mtd_evidence`
`@anchor const CAPABILITY = "s1.request_trace_capture"`
`@anchor const WINDOW = "month_to_date_cst"`

**判月靠事件时间戳，不靠文件名**。文件名只用于筛候选分区（`PARTITION_DATE` 正则
`(\d{4}-\d{2}-\d{2})\.jsonl$`），落在跨月边界的事件由其自身 `ts` 决定归属。
`@anchor symbol PARTITION_DATE`
`@anchor test Bigbird/Stats/exporter/tests/test_billing.py :: test_event_time_not_filename_decides_month_and_future_is_ignored`

### 3.2 硬红线：runtime / provider / 文件位置**不得**代替 `billing_component_id`

这是本域最重要的一条契约。一条 `turn.end` 即使带 `runtime: "cc"` 或
`provider: "claude_code"`，只要没有 `billing_component_id`，就**不算 CC 订阅证据**——
结论是 `insufficient`，不是「猜它是 CC」。

理由：`runtime`/`provider` 描述的是这一 turn 在哪个进程跑，与「这笔钱记在哪个订阅组件上」
是两件事；用前者冒充后者会把 API provider/Codex 路径的 turn 混进 CC 的分摊分母。

`@anchor test Bigbird/Stats/exporter/tests/test_billing.py :: test_missing_gate_is_insufficient_and_runtime_provider_never_substitute`

`billing_component_id != "claude_code"`（如 `"codex"`）→ 该事件直接跳过，不进分母，
且**不触发** `provenance_gap`。

### 3.3 agent 归属与 unattributed 分流

`turn.end` 通过证据门后，看 `agent` 字段：

- `agent` 是非空字符串且 `agent == agent.strip()` → 计入 `agents[agent]`；
- 否则（`None`、空串、带首尾空白如 `" spaced "`、非字符串）→ 其 token 计入
  `missing_tokens`，即 `AttributionEvidence.unattributed_output_tokens`。

**关键**：agent 名不合法的 turn **仍然进分母**（`valid_turn_count` 递增，token 进总量），
只是归不到具体 agent 头上。丢弃它会让分摊比例失真。
`@anchor test Bigbird/Stats/exporter/tests/test_billing.py :: test_proven_events_aggregate_agents_and_keep_invalid_agent_in_denominator`

`@anchor symbol AttributionEvidence`

---

## 4. 守恒不变量（最该被机器盯住的部分）

### 4.1 核心恒等式

`allocate_subscription_costs()` 在返回前自查一次，不成立即整块降级：

```
attributed_cents + unattributed_cents == total_cents
```

其中 `total_cents = sum(c.amount_cents for c in components)`（**全部生效组件**，含不可归因的）。
不成立时返回 `_unknown("unavailable", "Integer billing allocation failed conservation")`——
**宁可报 unknown，也不出一个不守恒的数**。

`@anchor symbol allocate_subscription_costs`
`@anchor symbol BillingResult`

### 4.2 池子定义

```
pool        = sum(c.amount_cents for c in components if c.attribution == ATTRIBUTABLE)
total_cents = sum(c.amount_cents for c in components)
```

`pool` 是可归因池（生产上 = 25000），`total_cents - pool` 是恒定的不可归因底座（= 2000）。

### 4.3 分摊分母

```
total_tokens = sum(agents.values()) + missing
```

`missing` = `evidence.unattributed_output_tokens`。**无主 token 与具名 agent 的 token 一起
构成分母**，且 `missing` 本身作为一个虚拟参与者参与分摊（键为 `UNATTRIBUTED_SORT_KEY`），
它分到的那份落进 `unattributed_cents`。

`@anchor const UNATTRIBUTED_SORT_KEY = "\uffff__unattributed__"`

该常量在源码里写成转义形式 `"￿__unattributed__"`，即以 U+FFFF 开头。取这个前缀是为了
在按 key 的字典序里排到所有真实 agent 名之后，让最大余额法的平局裁决稳定且不与任何合法
agent 名冲突。

### 4.4 最大余额法（largest remainder），整数分不丢不多

```
shares[k]  = pool * weight[k] // total_tokens          # 地板分配
remainder  = pool - sum(shares.values())               # 待补的余数分
```

余数按 `(-余数, key)` 排序后逐个 +1 分配给前 `remainder` 名。排序第二键是 key 本身，
保证**平局时结果确定**（同样输入必得同样输出，不依赖 dict 迭代序）。

`@anchor test Bigbird/Stats/exporter/tests/test_billing_core.py :: test_largest_remainder_is_stable_and_exact`

该测试的实测数值契约：`pool=25000`、三个 agent 各 1 token、`codex` 2000 不可归因 →
`{"a": 8334, "b": 8333, "c": 8333}`，且 `sum(rows) + unattributed == 27000`。
注意 a 拿到多出的那 1 分，是字典序裁决的结果。

`unattributed` 的完整算式：
```
unattributed = total_cents - pool + shares.get(UNATTRIBUTED_SORT_KEY, 0)
```
即「不可归因组件全额」+「无主 token 分到的那份」。

实测契约（`test_missing_agent_share_and_codex_are_both_unattributed`）：
`agents=(("pulse", 75),)`、`missing=25` → `pulse` 得 `18750`，`unattributed = 8250`
（= 2000 codex + 6250 无主份额），合计 27000。
`@anchor test Bigbird/Stats/exporter/tests/test_billing_core.py :: test_missing_agent_share_and_codex_are_both_unattributed`

### 4.5 输出行的排序

`by_agent` 按 `(-tokens, agent)` 排序——token 多的在前，同 token 按名字升序。

### 4.6 快照层的守恒检查（真实数据，非 fixture）

`verify-live-snapshot.py` 把上述不变量重述成对真实快照可证伪的检查：

| 检查 | 断言 |
|---|---|
| `components sum to total_cents` | `sum(c["amount_cents"]) == billing.mtd.total_cents` |
| `all component amounts are integer cents` | 每个 `amount_cents` 是 `int` |
| `component ids unique` | `component_id` 全表唯一 |
| `attributed + unattributed == total` | 三者都 available 时才检 |
| `per-agent attributed sums to attributed_cents` | `sum(a["attributed_cents"]) == attributed` |
| `agent names unique` | `by_agent` 内 agent 名唯一 |
| `no codex agent row` | 无 `agent.lower() == "codex"` 的行 |
| `unattributed covers every unattributed component` | `unattributed >= sum(不可归因组件金额)` |

**注意最后一条是 `>=` 不是 `==`**——因为无主 token 的份额也会加进去，`unattributed` 只能
大于等于不可归因组件的总额。

**已知盲区（见 §7.2）**：后 5 条全部包在 `if None not in (...)` 守卫里。生产上
`attributed_cents` 恒为 `None`，所以这 5 条在真实数据上**从未被执行过**，只在 fixture 里绿过。

### 4.7 用量侧的守恒（`collect_usage`）

```
usage.today.input_tokens == input_tokens_uncached + cache_read_tokens + cache_creation_tokens
```

由 `collect_usage()` 返回字典中 `"input_tokens": input_tokens + cache_read_tokens + cache_creation_tokens`
构造，`verify-live-snapshot.py` 对真实快照逐次校验。

`@anchor symbol collect_usage`

「真实工作量」的口径是 `input_tokens_uncached + cache_creation_tokens + output_tokens`，
**不含 `cache_read_tokens`**——缓存复读在实测中占输入的 99%+（2026-08-01 实测：
读 36,747,189 / 工作量 1,634,892，膨胀 23 倍），拿总输入当工作量正是 2026-07-29 那次虚高。

`@anchor manual working-tokens-exclude-cache-read :: 跑 verify-live-snapshot.py，看 "using total input would inflate the figure" 一行的两个数字相差量级，确认口径没退回总输入`

---

## 5. 数值契约（照抄代码真实值）

### 5.1 金额与配置

| 常量 / 配置 | 值 | 位置 |
|---|---|---|
| `MONTHLY_SUBSCRIPTION_USD` | `270.0` | `usage_sources.py` |
| `claude_code` amount_cents | `25000` | `config/subscriptions.json` |
| `codex` amount_cents | `2000` | `config/subscriptions.json` |
| `schema_version` | 必须 `== 1` | `load_subscription_components()` |
| `currency` | 必须 `== "USD"` | `load_subscription_components()` |

`@anchor const MONTHLY_SUBSCRIPTION_USD = 270.0`

**债**：`MONTHLY_SUBSCRIPTION_USD = 270.0` 是硬编码浮点，与 `subscriptions.json` 派生的
`total_cents = 27000` 是**两个独立配置真源**，改一个不会带动另一个；两者都不是外部
发票真源。见 §7.3 与 §7.8。

### 5.2 额度与窗口

| 常量 | 值 | 含义 |
|---|---|---|
| `CC_QUOTA_MAX_AGE_SECONDS` | `9 * 3600`（9 小时） | CC 额度缓存读数的新鲜度上限 |
| `CC_QUOTA_TIMEOUT_SECONDS` | `5` | CC 额度探针 HTTP 超时 |
| `CC_QUOTA_MAX_TOKENS` | `1` | 探针请求的 max_tokens，最小化消耗 |
| `CC_QUOTA_MODEL` | `"claude-haiku-4-5-20251001"` | 探针用的最便宜模型 |
| `CC_QUOTA_HEADER_PREFIX` | `"anthropic-ratelimit-unified"` | 额度头前缀 |
| `CC_QUOTA_ANTHROPIC_VERSION` | `"2023-06-01"` | |
| `CC_QUOTA_OAUTH_BETA` | `"oauth-2025-04-20"` | |
| `CC_RATE_LIMIT_WINDOW_HOURS` | `24` | 429 回看窗口 |
| `CC_RATE_LIMIT_MAX_FILES` | `40` | 429 扫描的最多文件数 |
| `CODEX_PROBE_MIN_INTERVAL_SECONDS` | `6 * 60 * 60`（6 小时） | Codex 探针最小间隔（配额配给底线） |
| `CODEX_PROBE_MODEL` | active cost route 的 profile alias 经 schema-v2 catalog/cache 解析；失败为 `None` | Codex 探针显式模型 |
| `CODEX_PROBE_REASONING_EFFORT` | 同一 profile 的受支持 effort；失败为 `None` | Codex 探针显式推理档 |
| `CODEX_QUOTA_MAX_AGE_SECONDS` | `2 * CODEX_PROBE_MIN_INTERVAL_SECONDS`（12 小时） | Codex 读数新鲜度上限 |
| `CODEX_PROBE_TIMEOUT_SECONDS` | `45` | |
| `CODEX_SCAN_FILES` | `5` | 每次扫描的 session 文件数 |
| `CODEX_LOOKBACK_DAYS` | `3` | 远端 `find -newermt` 回看天数 |
| `CODEX_REMOTE_TIMEOUT_SECONDS` | `7` | |
| `RELAY_HTTP_TIMEOUT_SECONDS` | `2` | Relay 内网端点超时 |
| `USAGE_TOP_AGENTS` | `10` | `usage.by_agent` 截断长度 |
| `USAGE_TOP_SESSIONS` | `20` | `usage.by_session` 截断长度 |
| `SESSION_ID_PREFIX_CHARS` | `8` | session id 只留前 8 位进快照 |

`@anchor const CC_QUOTA_MAX_AGE_SECONDS = 9 * 3600`
`@anchor const CC_QUOTA_TIMEOUT_SECONDS = 5`
`@anchor const CC_QUOTA_MAX_TOKENS = 1`
`@anchor const CC_QUOTA_MODEL = "claude-haiku-4-5-20251001"`
`@anchor const CC_QUOTA_HEADER_PREFIX = "anthropic-ratelimit-unified"`
`@anchor const CC_RATE_LIMIT_WINDOW_HOURS = 24`
`@anchor const CC_RATE_LIMIT_MAX_FILES = 40`
`@anchor const CODEX_PROBE_MIN_INTERVAL_SECONDS = 6 * 60 * 60`
`@anchor const CODEX_PROBE_TIMEOUT_SECONDS = 45`
`@anchor symbol CODEX_PROBE_MODEL`
`@anchor symbol CODEX_PROBE_REASONING_EFFORT`
`@anchor const CODEX_SCAN_FILES = 5`
`@anchor const CODEX_LOOKBACK_DAYS = 3`
`@anchor const RELAY_HTTP_TIMEOUT_SECONDS = 2`
`@anchor const USAGE_TOP_AGENTS = 10`
`@anchor const USAGE_TOP_SESSIONS = 20`
`@anchor const SESSION_ID_PREFIX_CHARS = 8`
`@anchor const CODEX_USAGE_LIMIT_ERROR = "usage_limit_exceeded"`

### 5.3 额度单位换算：两边不同刻度

`_utilization_percent()` 把 Anthropic 的 `*-utilization` 头（**0–1 的分数**）乘 100 变成
0–100 的百分比，保留 2 位小数（`round(fraction * 100.0, 2)`）；而 Codex 侧的
`used_percent` **本来就是 0–100**，不再换算。两条路最终进快照的刻度一致。

超出 `[0.0, 1.0]` 或 NaN（`fraction != fraction`）的输入判为**不可用**返回 `None`，
**不静默钳位**——一个越界的额度值宁可当没读到。

`@anchor symbol _utilization_percent`

`_unix_seconds()` 解析 reset 时间戳，`value > 0` 才返回，否则 `None`。
`@anchor symbol _unix_seconds`

---

## 6. 失败模式：数据缺失时是 unknown 还是 0

**总纲**（`usage_sources.py` 模块 docstring 明写）：任何一个源不可用产出 `None`，
渲染成 `unknown` metric，**从不用 0 代替一个没被观测到的值**。

### 6.1 `_metric()` 的三态

`@anchor symbol _metric`

| 条件 | `status` | `value` |
|---|---|---|
| `value is not None` 且未过期 | `available` | 原值 |
| `value is None` | `unknown` | `None` |
| 值存在但 `age >= max_age_seconds` | `unknown` | **`None`（值被扣掉）** |

第三条是 2026-07-28 才加的。此前 `status` 只看 `value is None`，导致一个 8 天前的 Codex
读数在快照里与一秒前的读数无从分辨，菜单栏照单全收当事实。

**过期读数是被扣掉而不是附免责声明发出**：`"value": None if stale_reason is not None else value`。
理由写在源码注释里——否则每个消费者都得记得先看 `status`，而菜单栏证明了它不会记得。
「缺席」是唯一不会被误读的信号。

`max_age_seconds` 只在调用方显式传入时才检查；多数 metric 用采集时刻打戳，构造上不会过期。

### 6.2 billing 的四档失败

`collect_mtd_evidence()` 的 `outcome` 三值 → `allocate_subscription_costs()` 映射成 quality：

| outcome | 触发条件 | quality | 六个 metric 的表现 |
|---|---|---|---|
| `unavailable` | obs 目录不可读 / JSON 坏行 / `ts` 非法 / `outputTokens` 非法 / `billing_component_id` 非字符串或空串 | `unavailable` | 4 个归因项 `unknown` |
| `insufficient` | 存在缺 `billing_component_id` 的 `turn.end`（`provenance_gap`）；或证据总量 `total <= 0` | `insufficient` | 4 个归因项 `unknown` |
| `usable` | 有正的、已证明的 CC 输出 | `descriptive`（部分覆盖）/ `validated`（完整覆盖） | 出数 |

**六个 metric 的分轴设计**（`build_billing_metrics()` 里 `fact = index in (1, 5)`）：

- `billing.mtd.components`（index 1）与 `billing.mtd.total_cents`（index 5）是**配置事实**，
  只要配置能加载就恒为 `validated`/`available`，**不受归因证据影响**；
- 其余四项（`period`、`by_agent`、`attributed_cents`、`unattributed_cents`）是**归因结论**，
  随证据降级。

`@anchor test Bigbird/Stats/exporter/tests/test_billing.py :: test_facts_survive_attribution_insufficient`

配置本身坏掉时（`BillingConfigError`），`_config_unavailable()` 返回**恰好 6 条**
`unknown`/`unavailable` metric，且 6 条共用同一个 `reason`——不是少发几条让消费侧去猜。
`@anchor symbol _config_unavailable`
`@anchor test Bigbird/Stats/exporter/tests/test_billing.py :: test_bad_config_makes_exactly_six_unavailable_metrics`

### 6.3 「零使用」不许被伪造

`total_tokens == 0` 且 `not evidence.coverage_complete` → 返回 `_unknown("insufficient", ...)`，
reason 缺省为 `"Partial coverage cannot prove zero usage"`。

即：**部分覆盖证明不了「这个月没人干活」**。只有 `coverage_complete == True` 时零使用才
被记成 `attributed=0, unattributed=total_cents`。

`@anchor symbol _unknown`
`@anchor test Bigbird/Stats/exporter/tests/test_billing_core.py :: test_partial_and_failure_states_never_fabricate_zero`
`@anchor test Bigbird/Stats/exporter/tests/test_billing.py :: test_empty_or_non_turn_source_cannot_claim_verified_zero`

**当前生产状态**：`collect_mtd_evidence()` 硬编码 `coverage_complete=False`（构造
`AttributionEvidence` 时第 4 位恒为 `False`），因为 obs 没有任何「当日分区完整」标记。
所以 `billing.mtd.*` 的归因项**永远不可能达到 `validated`**，最好也只是 `descriptive`。

### 6.4 CC 额度降级：缓存兜底，不整格变未知

`_append_cc_quota_metrics()` 在 `cc_quota is None` 时的处置顺序：

1. **先读缓存** `cc_quota_cache_read()`（`/home/pulse/.relay/monitoring/pulse-stats/cc-quota-last.json`）。
   命中则用缓存值发三条 metric，`observed_at` 照实写成**缓存里那个时刻**（不是 now），
   由 `CC_QUOTA_MAX_AGE_SECONDS`（9 小时）判老。
2. 缓存也没有 → 三条 `unknown`，reason 缺省 `"CC quota headers were unavailable"`。
3. 若 `cc_token_expiry()` 返回一个**已经过去**的时刻，reason 换成具体的
   `f"CC 登录令牌已过期（{local} 到期），需重新登录"`。过期时刻尚未到达则保留通用 reason
   ——那说明令牌没问题，锅在别处。

`@anchor symbol _append_cc_quota_metrics`
`@anchor symbol read_cc_quota_cache`
`@anchor symbol write_cc_quota_cache`
`@anchor symbol CC_QUOTA_CACHE_PATH`

**这条缓存兜底就是「额度降级丢弃」那个 bug 的修复**：此前令牌空窗期（实测 2026-07-29
05:55 过期、09:37 才刷回，空窗近 4 小时）内，一个几分钟前还好好的读数会被整个丢掉变
「未知」。额度是慢变量，「8 分钟前 82%」远比「未知」有用。

成功读到额度时 `cc_quota_cache_write(cc_quota, self.now)` 落盘供下次兜底；**写失败不影响
本次快照**（`except OSError: return`），它是锦上添花不是核心源。缓存文件只存额度数字与
时间戳，**不含任何凭证**。

### 6.5 凭证源：relay.env 优先，credentials.json 兜底

`read_cc_oauth_token()` 的**取值顺序是硬契约**：先 `read_relay_env_token()`
（`relay.env` 的 `CLAUDE_CODE_OAUTH_TOKEN`，长期有效、生产真正在用的那份），
取不到才退回 `~/.claude/.credentials.json` 的 `claudeAiOauth.accessToken`。

`@anchor symbol read_cc_oauth_token`
`@anchor symbol read_relay_env_token`
`@anchor symbol CC_RELAY_ENV_PATH`

「CC 额度未知」发作三次，三次都栽在读错源上：`credentials.json` 是 CC TUI 交互登录的副产物，
access token 只活约 8 小时且**过期后要等下一次交互登录才刷回**——vultr 是生产机没人天天去
TUI 登录，那个字段几乎总是过期的。退回 credentials.json 是为 ownermac 这类无 relay.env 的
场景保留的。

配套地，`cc_token_expiry()` 在 `read_relay_env_token()` 有值时**直接返回 `None`**——
relay.env 那份没有过期戳，不许拿隔壁闲置文件的过期时间去解释生产状态。`None` 的语义是
「查不出」，调用方不得当作「没问题」。
`@anchor symbol cc_token_expiry`

**凭证不得进快照**：`fetch_cc_quota_headers()` 把 token 限制在自己的请求头里，
`_rate_limit_headers()` 只复制出 `anthropic-ratelimit-unified` 前缀的头，其余一律丢弃。
`@anchor symbol fetch_cc_quota_headers`
`@anchor symbol _rate_limit_headers`
`@anchor manual no-credential-in-snapshot :: 跑 pulse-stats-snapshot.py --json，把输出 grep 一遍 token/Bearer/sk-/accessToken，应零命中`

**429 仍取头**：`HTTPError` 分支照样调 `_rate_limit_headers(error.headers)`——429 恰好带着
额度头，而那正是面板最需要显示的状态。401/403 不带头，降级为 `None`。

`parse_cc_quota_headers()` 以 **7 天利用率**为必需字段：`used_percent is None` 即整体返回
`None`。头名按 RFC 9110 大小写不敏感处理（先 `.lower()` 归一）。
`@anchor symbol parse_cc_quota_headers`

### 6.6 Codex 额度：耗尽 = 100%，不是未知

`_extract_exhaustion()` 处理一个反直觉的失败模式：周额度用尽后服务器**拒绝执行 turn**，
于是 `token_count` 依然到达但 `primary`/`secondary` 都是 null，单看像「额度未知」。
真相在同 session 的下一行 `task_complete` 记录里，`codex_error_info == "usage_limit_exceeded"`。

`@anchor symbol _extract_exhaustion`
`@anchor symbol _scan_codex_line`

该分支返回 `used_percent = 100.0` 并带 `exhausted: True` 标记，用来区分「测到 100%」与
「因用尽被拒」，百分比两者相同。

**刻意绑 `codex_error_info` 这个稳定枚举，不绑 `message` 的英文散文**——后者会被厂商随时
改写和本地化。reset 时间同理，取记录自带的 `resets_at`，不从散文里解析。

`_merge_exhaustion_resets()` 把耗尽记录缺失的 `resets_at` 从同批次里最新的**成功**读数回填
（周窗口是同一个）；否则面板会显示「100%」却说不出什么时候解除，而后者正是大鸟真正要据以
行动的那一半事实。
`@anchor symbol _merge_exhaustion_resets`

`merge_codex()` 同理跨机回填：额度是账户级共享的单一周窗口，任一台机器看到的 reset 时间
对赢家也成立。取胜规则是 `observed_at` 最新者赢。
`@anchor symbol merge_codex`

2026-08-01 实测生产快照：`relay.codex.used_percent = 100.0`、
`relay.codex.resets_at = 1785903014`，正是走的这条耗尽路径。

### 6.7 Codex 探针配给

`codex_probe_is_due()` 判是否值得花一个真 turn 去刷新读数：无读数 → due；
读数年龄 `>= CODEX_PROBE_MIN_INTERVAL_SECONDS`（6 小时）→ due；
**年龄为负（时钟偏斜）→ 也判 due**，不信任一个已知错误的时间戳去无限期抑制探针。

`@anchor symbol codex_probe_is_due`
`@anchor symbol run_codex_probe`

`collect_codex()` 的顺序是硬契约：先读盘上已有的 → 判是否够新 → 才花 turn 探。探完
**必须重读**（新记录落在全新 session 文件里，探针前的扫描不可能看到它）。
探针失败时保留旧读数而不是回退成 `None`。
`@anchor symbol collect_codex`

`run_codex_probe()` 显式 `stdin=subprocess.DEVNULL`——否则 `codex exec` 会阻塞读终端输入，
在 timer 驱动的 exporter 里那是挂死不是提示。
`--ask-for-approval never` 是 Codex 顶层选项，argv 必须先放该选项再放 `exec`；放在
`exec` 之后会由子命令 parser 以 exit 2 拒绝，不能把这类 CLI 失败当额度采集成功。

phase 2 的 canary profile 是 `economy`：Codex alias=`codex-luna-latest`、effort=`low`。
解析必须与 Relay 同守 cache 7 天 TTL、未来 5 分钟容差、`visibility=list`、family+effort；
active route、catalog、cache 或 effort 任一不可验证时 `CODEX_PROBE_ARGV=None`，runner 直接
返回 False 且不调用 subprocess。此时保留已有 quota，超 12 小时后按既有规则置 unknown，
不得退回 Sol/xhigh。2026-08-16 Vultr phase 2 已真实执行 Luna/low probe：
`token_count` 给出 10080 分钟窗口、48% 与 reset，canonical exporter 同步发布
`available/validated`。该验收不证明金额、7 日节省或质量不降。

`@anchor symbol _resolve_codex_probe_route`
`@anchor test Bigbird/Stats/exporter/tests/test_live_contract_cost_route.py :: test_stats_route_resolves_luna_low_from_visible_fresh_catalog`
`@anchor test Bigbird/Stats/exporter/tests/test_live_contract_cost_route.py :: test_stats_route_failures_never_fall_back_to_platform_default`

### 6.8 429 检测：mtime 只做预筛，判据是记录自身时间戳

`detect_cc_rate_limited()` 用文件 mtime 决定**打开哪些文件**，但窗口判定重新落到每条记录
自己的 `timestamp` 上。原因（2026-07-25 实测）：今天被追加过的 transcript 里常年躺着几周前
的错误记录——盘上仅有的两条 429 来自 06-11 和 06-18，纯 mtime 规则会误报红灯。

`@anchor symbol detect_cc_rate_limited`
`@anchor symbol _record_is_rate_limit_429`

判据是 `record["apiErrorStatus"] == 429` **且**整条记录序列化后（去空格）含
`'"error":"rate_limit'`，两条都满足才算。

返回值三态：`True`（窗口内有真 429）/ `False`（读到了，没有）/ `None`（目录不可读）。
**`False` 与 `None` 不可混同**。

### 6.9 用量侧：目录读不到 vs 读到但空

`collect_usage()` 返回 `None` 仅当 obs 目录不存在、无候选文件、或所有候选文件都打不开
（`read_any` 为假）。**「可读但当天没有事件」返回的是一份全零计数的字典**，不是 `None`。
这两种状态在快照里必须可分：前者 `unknown`，后者 `available` 且值为 0。

一个北京日跨两个 UTC 日期，所以候选文件取**两个 UTC 日期**的集合；事件时间戳仍是权威判据，
逐条折算到 `DISPLAY_TZ` 后过滤到目标本地日。

**无法解析的行**（`malformed_lines`）没有事件日归属，因此算作候选文件的**源质量证据**，
不算作有日期的事件。存在 malformed 行时该批 metric 的 `quality` 从 `validated` 降为
`descriptive`，并额外发一条 `usage.today.malformed_lines`。

`collect_month_to_date()` 的失败语义与之不同：目录不是 dir → `None`；单个文件打不开 →
`continue` 静默跳过。`days_covered` 如实报覆盖天数（只有真读到 `turn.end` 行的日期才计入
`days`），因为 obs 文件可能因轮转/清理/换机缺失，报「本月累计」却只覆盖 15 天而不说，
读者会当成整月的数。

`total == 0` 时 `share` 取 `0.0` 而非除零——「这个月还没人干活」的正确呈现是零而不是未知。
`amortized_usd` 保留 2 位小数（`round(MONTHLY_SUBSCRIPTION_USD * share, 2)`）。

---

## 7. 已知缺陷与债（如实登记，不粉饰）

### 7.1 `billing_component_id` 在 Relay 侧从未被写过 —— 归因永久停摆

**最重的一条。** 全仓 grep `billing_component_id` 在 `Bigbird/Relay/src` **零命中**；
2026-08-01 实读 `/home/pulse/.relay/obs/*.jsonl` 的 live `turn.end` 事件，字段集为
`ts/evt/agent/session/severity/poolKey/stopReason/durationMs/inputTokens/outputTokens/cacheReadTokens/cacheCreationTokens/isError`
——**没有 `billing_component_id`**。

`@anchor wire billing_component_id :: unwired`
`@anchor manual relay-never-writes-billing-component-id :: 跑 grep -rn "billing_component_id" Bigbird/Relay/ （排除 node_modules），命中数应为 0；再 grep 最新的 /home/pulse/.relay/obs/*.jsonl，同样应为 0`

> **锚点能力边界（不许被上面那条绿骗过）**：`wire ... unwired` 只在 `--src-glob` 指定的
> Stats Python 里查，**看不见 Relay 的 TypeScript**。它判 `holds` 只证明「Stats 自己不写
> 这个字段」，不证明「Relay 也不写」。真正的证据是紧随其后那条 `manual`——2026-08-01 已
> 实跑，Relay 仓与 live obs 双双零命中。

后果（2026-08-01 生产快照实测）：

| metric | status | quality |
|---|---|---|
| `billing.mtd.period` | `unknown` | `insufficient` |
| `billing.mtd.by_agent` | `unknown` | `insufficient` |
| `billing.mtd.attributed_cents` | `unknown` | `insufficient` |
| `billing.mtd.unattributed_cents` | `unknown` | `insufficient` |
| `billing.mtd.components` | `available` | `validated` |
| `billing.mtd.total_cents` | `available` | `validated`（配置值 `27000`，非发票核验） |

即**六项里四项永久 unknown**，reason 恒为
`"MTD turn.end lacks billing_component_id; runtime/provider/file location cannot prove Claude Code subscription provenance"`。

责任归属：**Relay 记账侧**。消费侧（Stats）的判据是对的且不应放宽——用 `runtime`/`provider`
猜会把非 CC 的 turn 混进 CC 分母。修复路径是 Relay 在写 `turn.end` 时补该字段，本文件不做
方案设计。

**这个缺陷在代码门下完全隐形**：`test_billing.py` 里 `_turn()` 的 fixture 默认带
`"billing_component_id": "claude_code"`，所以单测永远走 happy path；而
`test_missing_gate_is_insufficient...` 把「缺字段 → insufficient」**断言成预期行为**，
于是生产上归因整块失效在测试里是一片绿。这正是「测试只编码我的意图」那类失效。

### 7.2 真实数据上，5 条守恒检查从未跑过

`verify-live-snapshot.py` 的billing 检查里，只有 3 条（components 求和 / 整数分 / id 唯一）
在生产上真的执行。另外 5 条被 `if None not in (attributed, unattributed, total)` 和
`if by_agent is not None and attributed is not None` 守住，而生产上 `attributed_cents`
恒为 `None`（§7.1），所以：

- `attributed + unattributed == total` —— **真实数据上从未验过**
- `per-agent attributed sums to attributed_cents` —— 同上
- `agent names unique` —— 同上
- `no codex agent row` —— 同上
- `unattributed covers every unattributed component` —— 同上

**跑绿不等于验过。** 脚本会打印 "all live-snapshot checks passed"，但那 5 条根本没被计入。
守恒律目前只有 fixture 层证据（`test_billing_core.py`），符合「fixture 绿证明不了守恒」那条
教训的形态。

`@anchor manual conservation-checks-silently-skipped :: 跑 verify-live-snapshot.py，数 billing 段实际打印的 PASS 行数——应有 8 条，实际只有 3 条，差值即被守卫跳过的检查`

### 7.3 `MONTHLY_SUBSCRIPTION_USD` 与 `subscriptions.json` 是两个真源

`usage.mtd.subscription_usd` 的值来自硬编码的 `MONTHLY_SUBSCRIPTION_USD = 270.0`；
`billing.mtd.total_cents` 的值来自 `subscriptions.json` 求和 = `27000`。两者当前**恰好一致**，
但**没有任何代码或测试把它们绑在一起**。改配置里的金额，面板上的 `usage.mtd.subscription_usd`
仍会报 270.0；即使二者一致，也只能证明内部配置一致，不能证明实际订阅账单。

`@anchor manual two-sources-of-monthly-total :: 把 config/subscriptions.json 里 claude_code 改成 26000，重跑快照，看 usage.mtd.subscription_usd 是否仍为 270.0（是＝确认此债存在）`

### 7.4 `collect_month_to_date()` 零直接单元测试

全仓 `grep -rn "collect_month_to_date" tests/` **零命中**。它只被 `test_live.py` 间接触及
（断言 `usage.mtd.*` 四个 metric id 存在，以及 `by_agent` 的 agent 顺序）。

即：分摊算式、`days_covered` 计数、`total == 0` 的除零保护、月份前缀筛选，**全部没有直接
断言保护**。而这正是 2026-07-30 被整块删除、07-31 才恢复的那段代码。

`@anchor manual mtd-has-no-direct-unit-test :: 跑 grep -rn "collect_month_to_date" Bigbird/Stats/exporter/tests/ ，命中数应为 0`

### 7.5 `five_hour_resets_at` 被解析但无人消费

`parse_cc_quota_headers()` 产出 `five_hour_resets_at`，但**生产代码**（`live_sources.py` 的
三条 CC 额度 metric、`read_cc_quota_cache()`、`write_cc_quota_cache()`）**无任何读取方**。
5 小时窗口的百分比进了快照，它的重置时刻没有。

2026-08-01 全仓实测该字段共 2 处命中：`usage_sources.py:722` 是**产出**它的那一行，
`tests/test_usage_sources.py:912` 是一条断言它被正确解析的测试——**测试测的是解析，
不是消费**，所以有测试并不改变「无人消费」这个判定。

`@anchor wire five_hour_resets_at :: unwired`
`@anchor manual five-hour-resets-parsed-but-unconsumed :: 跑 grep -rn "five_hour_resets_at" Bigbird/Stats/exporter/pulse_stats/ ，应只命中 usage_sources.py 里产出它的那一行；live_sources.py 零命中即确认此债`

后果：面板能说「5 小时窗口用了 16%」，说不出它什么时候清零——与 §6.6 给 Codex 补 reset 时间
的那条理由（「后者正是大鸟真正要据以行动的那一半事实」）自相矛盾。

### 7.6 `collect_month_to_date` 的文件筛选靠文件名，跨月边界会漏/串

`collect_month_to_date()` 用 `obs_dir.glob(f"*{month_prefix}-*.jsonl")` 按**文件名**筛月份，
且用 `path.stem[-10:]` 当日期键；而 `collect_mtd_evidence()`（billing 侧）用**事件自身的 ts**
判月。两套 MTD 指标的**月份边界口径不一致**：一个北京月初凌晨落在 UTC 上月文件里的事件，
billing 侧会算进本月，usage 侧会漏掉。

`@anchor manual mtd-month-boundary-differs-between-two-paths :: 每月 1 日北京时间 00:00-08:00 之间跑一次快照，比对 usage.mtd.output_tokens 与手工按事件 ts 统计的本月总量，差值即此口径分歧`

### 7.7 远端 Codex 探针路线实际是死的

`CODEX_REMOTE_HOST = "ownermac"` 那条远端采集路径在 2026-07-28 被查实为死路：ownermac 上
根本没有 `codex` 二进制，其最新 session 文件停在 2026-03-05，`find -newermt "-3 days"`
匹配不到任何东西，探针返回空串。代码保留它（便宜、并发、有 staleness 闸后无害），
但**它不再是唯一来源**，也不应被当作有效的跨机冗余。

### 7.8 Codex 订阅现金账已漂移，当前实付未知

`config/subscriptions.json` 的 Codex 行仍是 `amount_cents=2000`，`source_ref` 指向
2026-07-29 owner-reviewed 的 `$20/month`；2026-08-16 生产最新 Codex `token_count` 却显示
`plan_type="pro"`。后者只证明运行账户自报档位，不证明发票金额；官网起售价也不是 owner
实付。因此现阶段：

- `billing.mtd.total_cents=27000` 是**配置总额**，可校验 schema 与整数分守恒；
- Codex 外部实际月费是 `unknown`，不能被 2000、官网价或 API token 估价代填；
- 优化成效先看订阅窗口用量与每个 accepted outcome 的容量消耗，现金节省要等发票闭环。

`@anchor manual codex-plan-config-drift :: 在生产读最新 Codex session token_count.plan_type，并与 subscriptions.json 的 codex source_ref/amount_cents、owner 发票三方对照；没有发票则现金账必须显示 unknown`

---

## 8. 未决问题（判断不了的，不猜）

1. **`billing_component_id` 该由 Relay 哪一层写入**：`turn.end` 的产生点在 Relay
   observability 侧，但「这一 turn 记在哪个订阅组件上」的判据是什么（按 runtime？按模型名？
   按进程启动时注入的组件 id？）——我读不出既定答案，需 Relay 侧定。
2. **`coverage_complete` 的完整性标记怎么产生**：`collect_mtd_evidence()` 硬编码 `False`，
   代码注释说 obs「没有 day-completeness marker」。这个 marker 该由谁写、写在哪、判据是
   「当天有 N 条事件」还是「当天 Relay 进程无异常退出」，无定论。在它出现前
   `billing.mtd.*` 归因项永远最高只到 `descriptive`。
3. **两套 MTD 是长期设计还是过渡态**：`usage.mtd.*` 的 `amortized_usd` 与
   `billing.mtd.attributed_cents` 一旦都能出数，会是同一笔钱的两个不同数字（一个按全部
   `turn.end` 摊、一个只按带证据的摊）。是否该在归因可用后隐藏估算版，无定论。
4. **`AttributionEvidence.days_covered` 恒为 0**：`collect_mtd_evidence()` 构造时第 2 位
   传的是字面量 `0`，`days_expected` 传 `local.day`。所以 `billing.mtd.period` 一旦能出数，
   它的 `days_covered` 会是 0 而 `days_expected` 是当月日号——这看起来像 bug，但也可能是
   「完整性未实现所以诚实报 0」的刻意选择。未在测试或注释里找到定论，不擅自定性。

---

## 9. 明确不在本 spec 范围内

- incident reducer 的状态机与 recovery 映射（见 `specs/pulse-stats.md` §3 P0-B）。
- harness 计数（`harness.wi.*`）、能力分组、snapshot transport 与本地 cache。
- Swift App 侧的呈现、菜单栏两轴、无障碍。
- Relay 侧如何补 `billing_component_id` 的实现方案。
- 任何代码改动——本轮只提炼 spec，不动代码、不 commit。
