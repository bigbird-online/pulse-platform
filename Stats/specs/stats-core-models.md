# SPEC：PULSE Stats — Swift Core 数据模型与派生层（从代码提炼）

> Version: 0.1.2 · Created: 2026-08-01 · Modified: 2026-08-16 · Status: 已实现（本 spec 描述现状，非计划）
> 版本史见 `git log`。
>
> **靶子**：`Bigbird/Stats/PulseStats/Core/` 共 9 个文件（行数会漂不写死，`wc -l` 现算）。
> 内容是逐文件读出来的现存契约，不是设计目标；与旧 spec 冲突处一律以代码为准。
>
> **与另外三份 spec 的分工**（避免重复，也避免留缝）：
>
> | spec | 管什么 | 本文与它的边界 |
> |---|---|---|
> | `stats-exporter-collection.md` | Python 侧采集与归约，**snapshot 怎么产生** | 本文只管 App 侧**怎么读**，不复述产法 |
> | `stats-billing-usage.md` | 计费口径与 Python 侧守恒 | 本文只管 Swift 侧 `BillingMetrics` 的**接收端校验** |
> | `stats-app-presentation.md` | 传输 / 缓存 / 刷新 / 灯 / 菜单栏 / 面板文案 | 本文不复述 `RefreshPolicy`、`StatusLamp`、`LampRules`、`PanelThresholds` 的阈值 |
>
> 交叉引用而不复述的部分：`RefreshPolicy`（间隔与夜间静默窗）、`LampRules` 四条灯判据、
> `PanelThresholds` 三个天数/行数常量 —— 全部见 `stats-app-presentation.md` §2 §4。
> 本文只在**派生逻辑消费它们**的地方点名，不重写数值。
>
> **本文覆盖的缺口**：`BillingMetrics` 在既有三份 spec 里 **0 次命中**（实测 grep），
> `SnapshotModels` / `PanelViewModels` / `StatsFormatting` 同为 0 次。
> 也就是说 Swift 侧最大的目录此前只有「呈现结果」有 spec，「数据怎么变成结果」没有。

> **金额边界**：下文 `$270` / `$20` 描述 Swift 当前收到和渲染的配置值，不是已核验
> 发票；外部现金账真相见 `stats-billing-usage.md` §7.8，缺凭据时必须保持 `unknown`。

---

## 0. 贯穿全文的一条总规则

**缺数一律是 `nil`，`nil` 渲染成字面量 `未知`，任何路径都不得把缺失变成 0。**

这条在 `stats-app-presentation.md` §3.4 已作为呈现契约登记。本文补的是**实现侧为什么做得到**：
`Core/` 里一共只有四处允许出现默认值，每一处都写明了理由（§2.4），除此之外
`SnapshotMetrics` 全部访问器返回 `Optional`，`StatsFormat` 全部函数吃 `Optional`。

`@anchor const unknown :: "未知"`

---

## 1. 解码契约（`SnapshotDecoder` + `SnapshotModels` 的 `init(from:)`）

### 1.1 三态错误，不存在第四种失败

`SnapshotDecodeError` 只有三个分支，任何解码失败必须落到其中之一，不得吞掉：

- `invalidJSON` —— 连 `schema_version` 这一个字段都读不出来。
- `unsupportedSchema(String)` —— 读出来了但版本不对，**带上实际读到的版本字符串**。
- `invalidPayload(String)` —— 版本对，正文违约，**带上原因串**。

`@anchor symbol SnapshotDecodeError`
`@anchor test Bigbird/Stats/PulseStatsTests/SnapshotDecoderTests.swift :: testRejectsInvalidJSON`

三个分支都实现了 `errorDescription`，措辞是英文（面向日志，不面向大鸟）：
`"Snapshot is not valid JSON."` / `"Unsupported snapshot schema: <version>"` /
`"Snapshot violates the v2 contract: <reason>"`。

### 1.2 两遍解码，版本闸在前

`decode(_:)` 刻意解两遍，不是笔误：

1. 第一遍只解私有的 `SchemaEnvelope`，它**只有 `schemaVersion` 一个字段**。
   这一遍抛任何错都归 `invalidJSON`。
2. 版本必须**全等**于 `PulseSnapshot.supportedSchemaVersion`，不做前缀匹配、不做大版本比较。
   不等即 `unsupportedSchema`，**不继续解正文**。
3. 第二遍才解整个 `PulseSnapshot`。

`@anchor const supportedSchemaVersion :: "pulse.monitoring.snapshot.v2"`
`@anchor test Bigbird/Stats/PulseStatsTests/SnapshotDecoderTests.swift :: testRejectsUnknownMajorBeforePayloadDecode`

> 代价是同一份 `Data` 被解析两次。这是**故意用性能换失败语义**：
> 一个未知版本的快照里可能有任意结构，先解正文再回头判版本，
> 报出来的会是「某字段类型不符」而不是「版本不认识」——后者才是可行动的信息。

### 1.3 唯一的跨字段守恒校验

第二遍解码成功后还有一道闸：`overall.open_incident_count` 必须**恰好等于**
`incidents` 数组里 `state == .open` 的条数。不等即 `invalidPayload("open_incident_count does not match incidents")`，
**整份快照拒收**。

`@anchor symbol openIncidentCount`
`@anchor test Bigbird/Stats/PulseStatsTests/PanelViewModelTests.swift :: testUnknownStateCannotHideAReportedOpenIncidentCount`

这是 Swift 侧**唯一**一条跨字段守恒检查。计费侧的守恒不在解码层，在 `BillingMetrics`（§4）。

### 1.4 键名转换：snake_case → camelCase，`id` 特判

`makeDecoder()` 用 `keyDecodingStrategy = .custom`，**不是**内置的
`.convertFromSnakeCase`。差别只有一处但很关键：下划线后的 `id` 段转成 **`ID` 全大写**
而不是 `Id`。

所以 `episode_id` → `episodeID`、`capability_id` → `capabilityID`、`event_id` → `eventID`。
若换成内置策略，这三个字段会全部解不出来。

`@anchor symbol SnapshotCodingKey`

`SnapshotCodingKey.intValue` 恒为 `nil`，`init?(intValue:)` 恒返回 `nil`
—— 数组下标键不参与转换。

### 1.5 时间戳：两段 ISO-8601，不猜

`dateDecodingStrategy = .custom`，走 `parseISO8601(_:)`：

1. 先试 `[.withInternetDateTime, .withFractionalSeconds]`（带小数秒）。
2. 再试 `[.withInternetDateTime]`（不带）。
3. 两者都不成 → `DecodingError.dataCorruptedError`，
   `debugDescription` 为 `"Invalid ISO-8601 date: <value>"`。

`@anchor symbol parseISO8601`

**不接受** Unix 时间戳数字、不接受本地时区无偏移串、不做任何兜底猜测。
`SessionUsage.parseDate(_:)`（§3.3）是同一套两段逻辑的独立第二份实现——
两处逻辑相同但**代码不共享**，登记为已知重复（§7）。

### 1.6 缺字段 / 类型不符时到底是 unknown 还是 0 还是抛错 —— 逐字段表

**这是本 spec 最该被机器盯住的一节**：同一个「缺失」在不同字段有三种不同的处置，
写错一处就是一块空白面板或一个伪造的 0。

| 字段 | 缺失时 | 类型/取值不符时 | 判据 |
|---|---|---|---|
| `PulseSnapshot.schemaVersion` | **抛** `invalidJSON` | 抛 `unsupportedSchema` | 必填，无兜底 |
| `PulseSnapshot.generatedAt` / `expiresAt` / `baselineVersion` / `overall` / `capabilities` / `incidents` / `metrics` / `alerts` / `provenance` | **抛** `invalidPayload` | 抛 `invalidPayload` | 九个必填字段，一个不到位整份拒收 |
| `PulseSnapshot.alertTiers` | `AlertTiers.zero` | 抛 | 整个 key 可缺（v3 前的 exporter 没有它） |
| `AlertTiers.act` / `.watch` / `.done` | **各自** 默认 `0` | 抛 | 三个计数器独立 `decodeIfPresent` |
| `Alert.tier` | `AlertTier.fallback`（= `.watch`） | **也是** `.watch` | 缺失与「未来新档」两条险都落同一处 |
| `Incident.state` | `.unknown` | `.unknown` | `try?` + `flatMap`，绝不猜成 `.open` |
| `Incident.title` | `"未知事故"` | 同左 | 空串/全空白也算缺 |
| `Incident.impact` | `"影响未知"` | 同左 | 同上 |
| `Incident.suggestedAction` | `"建议动作未知"` | 同左 | 同上 |
| `Incident.updatedAt` | `nil` | `nil`（`try?` 吞掉） | 渲染成「未知」，绝不用 `distantPast` |
| `Incident.resolvedAt` | `nil` | **抛** | `decodeIfPresent`：可缺但不可错 |
| `Incident` 其余 11 个字段（`incidentKey` `episodeID` `severity` `capabilityID` `scope` `whatHappened` `openedAt` `occurrenceCount` `reopenCount` `evidence` `recoveryEvidence`） | **抛** | 抛 | 身份与生命周期证据保持严格 |
| `Metric.value` | `MetricValue.null` | 降级 `.unsupported`（§1.7） | 有 fixture 确实不带这个 key |
| `Metric.sampleCount` / `observedAt` / `reason` | `nil` | **抛** | `decodeIfPresent` |
| `Metric` 其余 7 个字段（`id` `capabilityID` `status` `unit` `window` `quality` `scope`） | **抛** | 抛 | 必填 |

`@anchor const fallback :: .watch`
`@anchor test Bigbird/Stats/PulseStatsTests/SnapshotDecoderTests.swift :: testPreV3SnapshotDecodesWithSafeDefaults`
`@anchor test Bigbird/Stats/PulseStatsTests/SnapshotDecoderTests.swift :: testUnknownTierFallsBackToWatchRatherThanFailing`
`@anchor test Bigbird/Stats/PulseStatsTests/SnapshotDecoderTests.swift :: testKnownTierIsPreserved`
`@anchor test Bigbird/Stats/PulseStatsTests/SnapshotDecoderTests.swift :: testPartialAlertTiersDefaultsMissingCountersToZero`
`@anchor test Bigbird/Stats/PulseStatsTests/PanelViewModelTests.swift :: testMissingDisplayFieldsStayVisibleAndUnknownStateStaysOut`
`@anchor test Bigbird/Stats/PulseStatsTests/MetricValueTests.swift :: testMissingValueKeyDecodesAsAbsent`

三条设计规律，写进 spec 是为了让下一个加字段的人照着做：

1. **新增字段一律不得必填。** 一个必填的新字段会让老 exporter 的快照整份解不开，
   一块空白面板比一条降级的告警坏得多。
2. **降级落点必须是「安全的中间档」**，两个方向都不能出错：
   `AlertTier` 降到 `.watch` 而不是 `.act`（会狼来了）也不是 `.done`（会丢失）；
   `IncidentState` 降到 `.unknown` 而不是 `.open`（会伪造事故）也不是 `.resolved`（会掩盖）。
3. **只有面板要显示的四个文字字段允许本地降级**（`title`/`impact`/`suggestedAction`/`updatedAt`），
   身份键（`incidentKey`/`episodeID`）与生命周期证据（`openedAt`/`evidence`/计数）保持严格
   —— 前者错了是一行字难看，后者错了是编造了一个事故。

### 1.7 `MetricValue`：六种形状 + 一种「不认识」，永不抛

`Metric.value` 在 v2-A 之后不再是标量：`usage.by_agent` / `usage.by_session` /
`harness.wi.stalled` 是对象数组，`usage.hourly.output_tokens` 是数字数组，
`usage.today.inbound_types` / `delivery.today.attempts_histogram` 是 map，
`relay.cc.rate_limited` 是 bool。若把这些解成 `Double?` 会抛，而 `SnapshotDecoder`
把任何抛都翻成 `invalidPayload` —— **一个不认识的 metric 会拒掉整份快照**。

所以 `MetricValue.init(from:)` 按固定顺序试，全部失败落 `.unsupported`：

1. `decodeNil()` → `.null`
2. `Bool` → `.boolean`（**必须在 `Double` 之前**，否则 `true` 会被读成 `1.0`）
3. `Double` → `.number`
4. `[Double]` → `.numbers`
5. `[[String: MetricScalar]]` → `.rows`
6. `[String: Double]` → `.counts`
7. 以上都不是 → `.unsupported`，**不抛**

`@anchor symbol MetricValue`
`@anchor test Bigbird/Stats/PulseStatsTests/MetricValueTests.swift :: testBooleanMetricDecodesAsBooleanNotOneOrZero`
`@anchor test Bigbird/Stats/PulseStatsTests/MetricValueTests.swift :: testUnknownShapeDegradesToUnsupportedWithoutThrowing`
`@anchor test Bigbird/Stats/PulseStatsTests/MetricValueTests.swift :: testOneUnreadableMetricDoesNotRejectTheWholeSnapshot`

编码侧（写缓存）：`.unsupported` 与 `.null` 都写成 JSON `null`。
理由是缓存必须保持可解，而这个值本来就渲染不出来。
`@anchor test Bigbird/Stats/PulseStatsTests/MetricValueTests.swift :: testRealPayloadSurvivesACacheRoundTrip`

七个访问器（`number` / `boolean` / `numbers` / `rows` / `counts` / `isAbsent`）
一律形状不符即 `nil`，**没有一个会替换默认值**。
`isAbsent` 只对 `.null` 与 `.unsupported` 为真。
`@anchor test Bigbird/Stats/PulseStatsTests/MetricValueTests.swift :: testNullValueIsAbsentNotZero`

### 1.8 `MetricScalar`：行内标量四态，兜底是 string

行（`.rows`）里的每个字段走 `MetricScalar`，顺序是
`nil → Bool → Double → String`。前三个都不是就**强解 String 并允许抛**
—— 与 `MetricValue` 不同，这里没有 `.unsupported` 出口，
因为一行里出现嵌套对象本身就是行契约被破坏。

`@anchor symbol MetricScalar`

`MetricScalar.int` 走 `safeInt(number)`，不是 `Int(number)`。

### 1.9 `safeInt(_:)`：唯一允许的 Double→Int 通道

`Int(Double)` 在 NaN、±无穷、超出 `Int` 范围时**直接 trap（进程崩溃）**。
快照来自我们不控制的 JSON 文件，所以 `Core/` 里所有 Double→Int 转换必须走
`safeInt(_:)`：非有限、越界一律返回 `nil`（＝未知），**不崩、不截断**。

`@anchor symbol safeInt`
`@anchor test Bigbird/Stats/PulseStatsTests/MetricValueTests.swift :: testSafeIntBoundaries`
`@anchor test Bigbird/Stats/PulseStatsTests/MetricValueTests.swift :: testOutOfRangeNumbersDegradeToUnknownInsteadOfTrapping`
`@anchor test Bigbird/Stats/PulseStatsTests/MetricValueTests.swift :: testHostileNumbersRenderAsUnknownNotGarbage`

---

## 2. 数据模型（`SnapshotModels.swift`，584 行）

### 2.1 十一个 `String`-raw 枚举（另有 `MetricValue` / `MetricScalar` 两个值枚举，共 13 个）

| 枚举 | 取值 | 未知取值时 |
|---|---|---|
| `RuntimeHealth` | `ok` `warning` `critical` `unknown` `stale` | 抛（必填字段） |
| `Coverage` | `full` `partial` `unknown` | 抛 |
| `ImplementationStatus` | `active_validated` `active_partial` `retired` `not_implemented` | 抛 |
| `CapabilityHealth` | `ok` `warning` `critical` `unknown` `stale` `retired` `not_applicable` | 抛 |
| `AggregationRole` | `runtime` `coverage_only` | 抛 |
| `FreshnessState` | `fresh` `stale` `unknown` `error` `not_applicable` | 抛 |
| `IncidentState` | `open` `resolved` `unknown` | **降级 `.unknown`** |
| `Severity` | `info` `warning` `critical` | 抛 |
| `MetricStatus` | `available` `warming_up` `unknown` `not_applicable` | 抛 |
| `MetricQuality` | `validated` `descriptive` `insufficient` `unavailable` | 抛 |
| `AlertTier` | `act` `watch` `done` | **降级 `.watch`** |

上表 11 个是 `String`-raw + `Codable`；另有 `MetricValue`（§1.7）与 `MetricScalar`（§1.8）
两个非 raw 的值枚举，`SnapshotModels.swift` 顶层共 13 个 `enum`。

只有 `IncidentState` 与 `AlertTier` 两个枚举有降级路径 —— 见 §1.6 的规律 2。
`RuntimeHealth` / `Coverage` / `StatusLamp` / `AlertTier` 标了 `CaseIterable`，
其余没有；`CaseIterable` 的用处是测试遍历全部取值。
`@anchor test Bigbird/Stats/PulseStatsTests/StatusPresentationTests.swift :: testEveryRuntimeStateHasStablePresentation`
`@anchor test Bigbird/Stats/PulseStatsTests/SnapshotDecoderTests.swift :: testDecodesEveryRuntimeStateAndCoverageAxis`

### 2.2 结构体清单与 `id` 来源

「严格必填」＝缺失即抛、整份拒收；带默认与可选的另列。

| 类型 | `Identifiable` 的 `id` 是什么 | 严格必填字段数 | 有默认 / 可选的字段 |
|---|---|---|---|
| `PulseSnapshot` | 否 | 10（共 11 字段） | `alertTiers` → `.zero` |
| `OverallStatus` | 否 | 4 | 无 |
| `Freshness` | 否 | 1（`state`） | `observedAt` `maxAgeSeconds` |
| `Capability` | `id`（即 capability id 本身） | 8 | 无 |
| `Incident` | **`episodeID`**，不是 `incidentKey` | 11 严格必填（共 17 字段，其余 6 个走降级/可选） | `updatedAt` `resolvedAt` |
| `Metric` | `id`（metric id 本身） | 7 严格必填（共 11 字段） | `value`（→`.null`）`sampleCount` `observedAt` `reason` |
| `Alert` | **`eventID`** | 7（共 8 字段） | `tier` → `.watch` |
| `AlertTiers` | 否 | 3（各自默认 0） | 无 |
| `Producer` | 否 | 2 | 无 |
| `Source` | `id` | 2 | `observedAt` `errorCode` |
| `Provenance` | 否 | 3 | 无 |

`Incident` 有**两个**标识字段：`incidentKey`（同一件事跨轮次的稳定键）与
`episodeID`（这一次发作的实例）。`Identifiable.id` 取后者 ——
一件事重开三次在列表里是三行，不是一行覆盖两行。

`@anchor symbol episodeID`

### 2.3 `AlertTiers` 保留 producer 报的数，App 不自己数

`TriageViewModel.actCount` 读 `tiers.act`，**不是** `act.count`。
两者不一致时以 producer 为准，不悄悄用本地 `count` 抹平
—— 不一致的诚实读法是「producer 截断或过滤了什么」，抹平会把这个损失藏起来。

`@anchor symbol AlertTiers`
`@anchor test Bigbird/Stats/PulseStatsTests/LiveTriageContractTests.swift :: testProducerTierCountsMatchTheAlertsThemselves`
`@anchor test Bigbird/Stats/PulseStatsTests/PanelViewModelTests.swift :: testStatusTextUsesReportedActCount`

唯一的例外在 `TriageViewModel.doneSummary`：取 `max(tiers.done, done.count)`。
—— 但该属性目前**无生产调用点**（§6）。

### 2.4 全 `Core/` 只有四处默认值

穷举，每一处都有理由：

1. `AlertTiers` 三个计数器 → `0`。理由：老 producer 不做分档，
   「0 件要你出手」是它的诚实读数；且菜单栏必须印出点什么，这里没有「未知」可渲染。
2. `Alert.tier` → `.watch`。理由见 §1.6 规律 2。
3. `Incident` 四个展示字段 → 中文占位串。理由：一条残缺记录不该让整块面板空白。
4. `Metric.value` 缺 key → `.null`。理由：`.null` 的 `isAbsent` 为真，
   下游照样读成「未知」，与 `nil` 等效。

**除这四处外，解码路径上没有别的默认值。**

派生路径上另有 6 处 `?? 0`，实测穷举（`grep -rn "?? 0\b" PulseStats/Core/` 共 9 行命中，
减去上面第 1 项的 3 行）。它们**都不落在「一个缺失的读数变成 0」这条险上**，逐条说明：

| 位置 | 表达式 | 为什么不是伪造 0 |
|---|---|---|
| `SnapshotMetrics.swift:154` | `(sessionCount ?? 0) > 1` | 缺 ⇒ **不**判多会话，保守方向（不告警） |
| `SnapshotMetrics.swift:178` | `share = row["share"]?.number ?? 0` | **唯一一处真的把缺失读成 0 的数值**，登记于 §7 |
| `PanelViewModels.swift:165` | `(mtdAgentCosts?.count ?? 0) - rows` 外套 `max(0,)` | 数的是**行数**不是读数，缺 ⇒ 隐藏 0 行 |
| `PanelViewModels.swift:287` | `agent.sessionCount ?? 0` | 只进文案，且已由 154 那条闸筛过 `> 1` |
| `PanelViewModels.swift:451` | `snapshot?.alertTiers.act ?? 0` | 无快照 ⇒ 0 件要出手，与 §2.4 第 1 项同理 |
| `PanelViewModels.swift:736` | `stalled.map(\.days).max() ?? 0` | 已 `guard !stalled.isEmpty`，`max()` 必非 nil，`?? 0` 是死分支 |

后四处全在 §6.3 登记的传递性死代码里（`concerns` 一族），当前无生产渲染点。

---

## 3. 派生层之一：`SnapshotMetrics`（344 行）

### 3.1 索引与 `status` 闸

`SnapshotMetrics.init(_ metrics:)` 把扁平数组建成 `[String: Metric]`，
**重复 id 取最后一条**（`uniquingKeysWith: { _, latest in latest }`）。

两个取值口，语义不同、不可混用：

- `metric(_:)` —— 带 `status` 闸：`status != .available` 一律返回 `nil`。
  `warming_up` / `unknown` / `not_applicable` 都没有可用读数。
  所有 `double` / `int` / `bool` / `numbers` / `counts` / `rows` / `observedAt` 走这个。
- `rawMetric(_:)` —— **不带闸**，返回原始 `Metric`。
  仅用于「为什么未知」这类要读 `quality` / `reason` 的场合，**永不暴露可用值**。

`@anchor symbol rawMetric`
`@anchor test Bigbird/Stats/PulseStatsTests/PanelViewModelTests.swift :: testUnavailableMetricStatusIsTreatedAsMissing`

`rawMetric` 的生产调用点全部在 `Core/` 内部：`BillingMetrics.swift` 三处、
`PanelViewModels.swift` 两处（读 `billingByAgent` 的 `quality` 与 `reason`）。

### 3.2 `MetricID` 命名空间：45 个 metric id 字符串常量（2026-08-10 现算）

全部 metric id 在 `SnapshotMetrics.swift` 的 `enum MetricID` 里集中声明，
**视图与 view model 一律不得写裸字符串**。条数现算 **45**（口径 = 该 enum 体内
`static let` 的条数，复算：awk 取 enum MetricID 到收尾花括号的段落再
grep -c 'static let'，2026-08-10 实测 45）。
分七组，各组之和 = 45：relay 运行时 10 · 今日用量与会话 13 · 计费 6 ·
月累计 4 · 投递 2 · harness 8 · 数据质量注脚 2
（`harnessUnreadableRecords` / `usageMalformedLines`，2026-08-01 接线，
见 `stats-snapshot-contract.md` §5.5）。

其中三对最容易混，逐条写清窗口（照 auto-memory `counter-label-must-name-unit-and-window`）：

| 常量 | metric id | 数什么 | 窗口 | 时区 |
|---|---|---|---|---|
| `inputTokens` | `usage.today.input_tokens` | token | 自然日 | UTC |
| `inputTokensUncached` | `usage.today.input_tokens_uncached` | token，**真正的新输入** | 自然日 | UTC |
| `cacheReadTokens` | `usage.today.cache_read_tokens` | token，缓存复读 | 自然日 | UTC |
| `wiWeekCreated` | `harness.wi.week_created` | wi 条数 | **滚动 7 天** | — |
| `wiIsoWeekCreated` | `harness.wi.iso_week_created` | wi 条数 | **自然周**（北京周一清零） | UTC+8 |
| `mtdByAgent` | `usage.mtd.by_agent` | token + 摊销美元 | 月初至今 | Asia/Shanghai |

`@anchor const inputTokensUncached :: "usage.today.input_tokens_uncached"`
`@anchor const wiWeekCreated :: "harness.wi.week_created"`
`@anchor const wiIsoWeekCreated :: "harness.wi.iso_week_created"`
`@anchor const mtdByAgent :: "usage.mtd.by_agent"`

`inputTokens` 与 `inputTokensUncached` 的差额有实测量级：
2026-07-29 的 1.2245 亿 `input_tokens` 里 **1.2069 亿是 cache_read**。
把前者当「今天用了多少」会把「重读了多少遍旧上下文」报成工作量。
面板口径见 §5.1 与 `stats-app-presentation.md` §6.1。

`wiWeekCreated`（滚动 7 天）与 `wiIsoWeekCreated`（自然周）是**两个窗口，数值必然不等**。

### 3.3 五个行模型：谁必填、谁可缺、缺了怎么办

行模型的 `init?(row:)` 一律**失败即返回 `nil`**，由 `compactMap` 丢掉该行
（`BillingMetrics` 的三个例外见 §4.1）。

| 行模型 | metric | 必填字段（缺即整行丢） | 可选字段 → 缺时 |
|---|---|---|---|
| `AgentUsage` | `usage.by_agent` | `agent` `output_tokens` | `input_tokens` `turns` `session_count` → `nil` |
| `AgentMonthlyCost` | `usage.mtd.by_agent` | `agent` `output_tokens` `amortized_usd` | `share` → **`0`**（登记于 §7） |
| `SessionUsage` | `usage.by_session` | `agent` `session_prefix` `output_tokens` | `turns` `last_activity` → `nil` |
| `InFlightWorkItem` | `harness.wi.in_flight` | `work_item_id` `days` | `title` → **回落成 id**；`blocked_by` `status` → **`"?"`**；`needs_owner` → **`false`** |
| `StalledWorkItem` | `harness.wi.stalled` | `work_item_id` `days` | `title` → **回落成 id**；`status` → `nil` |

`@anchor symbol AgentUsage`
`@anchor symbol InFlightWorkItem`
`@anchor test Bigbird/Stats/PulseStatsTests/MetricValueTests.swift :: testAgentRowsMetricDecodes`
`@anchor test Bigbird/Stats/PulseStatsTests/MetricValueTests.swift :: testSessionRowsMetricDecodes`
`@anchor test Bigbird/Stats/PulseStatsTests/MetricValueTests.swift :: testStalledRowsMetricDecodesUsingWorkItemIDField`

id 字段名是 **`work_item_id` 不是 `id`** —— 两个 wi 行模型都靠它，写错就是整列丢空。

`AgentUsage.hasMultipleSessions` 判据是 `(sessionCount ?? 0) > 1`：
`session_count` 读不到时**不算**多会话（不告警），这是刻意的保守方向。
`@anchor test Bigbird/Stats/PulseStatsTests/PanelViewModelTests.swift :: testMultiSessionAgentIsDetected`

`SessionUsage.id` 是合成键 `"<agent>/<sessionPrefix>"` —— 同一个 session 前缀
在两个 agent 下不会撞。

### 3.4 六个派生集合的排序与「空 vs nil」

| 属性 | 排序 | `nil` 的含义 | 空数组的含义 |
|---|---|---|---|
| `agentUsage` | `outputTokens` 降序 | metric 不可用 | 量过了，没人烧 |
| `mtdAgentCosts` | `amortizedUSD` 降序 | 同上 | 同上 |
| `sessionUsage` | `outputTokens` 降序 | 同上 | 同上 |
| `stalledWorkItems` | `days` 降序 | 同上 | 没有卡住的 |
| `inFlightWorkItems` | `days` 降序 | 同上 | 没有在飞的 |
| `hourlyOutputTokens` | 原序（24 桶） | 不可用**或长度 ≠ 24** | —— |

**空数组与 `nil` 必须区分**，两者渲染不同。
`@anchor test Bigbird/Stats/PulseStatsTests/PanelViewModelTests.swift :: testEmptyStalledListDiffersFromMissingStalledList`
`@anchor test Bigbird/Stats/PulseStatsTests/PanelViewModelTests.swift :: testAgentsAreSortedByOutputDescending`

小时序列长度不对**整条拒收，不补零** —— 补出来的零是从没测过的小时。
`@anchor test Bigbird/Stats/PulseStatsTests/PanelViewModelTests.swift :: testWrongLengthHourlySeriesIsRejectedRatherThanPadded`

排序不稳定的登记：五个降序排序都**没有二级键**，同值行的相对次序由
`Array.sorted` 的实现决定，非确定。唯一有完整确定性排序的是
`TriageViewModel.incidentComesFirst`（§5.3）。登记于 §7。

### 3.5 两个配额重置时刻：Unix 秒 → `Date`，`<= 0` 读作未知

`ccResetsAt` / `codexResetsAt` 把 `relay.cc.resets_at` / `relay.codex.resets_at`
的秒数转 `Date`，判据是 `seconds > 0`：
**`0` 与负数一律读成 `nil`（未知）**，不会渲染成 1970-01-01。

`@anchor symbol ccResetsAt`

---

## 4. 派生层之二：`BillingMetrics`（207 行）

> 计费口径的真源在 `stats-billing-usage.md`。本节只写 **Swift 接收端做了哪些校验**
> —— 它是一道**独立于 exporter 的第二道闸**，exporter 侧的守恒过了不代表这里放行。

### 4.1 全或无（fail-closed）：任何一条不合即整块返回 `nil`

`billingFacts` 与 `billingAttribution` 都是长 `guard` 链，
**中途任一条件不成立就返回 `nil`，不返回半个结果**。
行解码走 `decodeAll(_:_:)`：`decoded.count == rows.count` 才通过 ——
**丢一行就整块作废**，与 §3.3 那五个「丢一行继续」的行模型**方向相反**。

理由：用量表少一行是少看一个 agent；账单表少一行是金额对不上，
而金额对不上时唯一诚实的呈现是「不给数」。

`@anchor symbol decodeAll`
`@anchor test Bigbird/Stats/PulseStatsTests/BillingMetricsTests.swift :: testMalformedFactsFailClosedAsAWhole`
`@anchor test Bigbird/Stats/PulseStatsTests/BillingMetricsTests.swift :: testMalformedAttributionFailsClosedAsAWhole`
`@anchor test Bigbird/Stats/PulseStatsTests/PanelViewModelTests.swift :: testMalformedBillingFactsFailClosedAsAWhole`
`@anchor test Bigbird/Stats/PulseStatsTests/PanelViewModelTests.swift :: testMalformedAttributionBundleFailsClosedAsAWhole`

### 4.2 `billingInteger(_:)`：金额只认非负整数

`amount_cents` / `days_covered` / `output_tokens` / `attributed_cents` 等
一律走 `billingInteger(_:)`，四条判据全过才收：
`isFinite` **且** `>= 0` **且** `rounded() == value`（必须是整数）**且** `safeInt` 不越界。

`@anchor symbol billingInteger`

浮点分、负数分、`3.5` 分一律作废整块。

### 4.3 `exactBillingSum(_:)`：溢出即未知，不回绕

求和走 `addingReportingOverflow`，任一步溢出立刻返回 `nil`。
**不用 `reduce(0, +)`** —— 那在溢出时 trap。

`@anchor symbol exactBillingSum`

### 4.4 `billingFacts` 的准入条件（照抄代码，`BillingMetrics.swift:120-139` 共 17 个 guard 子句）

两个 metric（`billing.mtd.components` + `billing.mtd.total_cents`）必须同时满足
（下面按语义归并成 9 条，逐条对应上述子句）：

- 两者 `status == .available`
- 两者 `quality == .validated`（**`descriptive` 不够**）
- 两者 `window == "month_to_date_cst"`
- `components.unit == "list"`，`total_cents.unit == "cents"`
- 两者 `sampleCount == nil`（**账单事实不带样本数**，带了就说明来源不对）
- `components` 的值必须是 `.rows` 且**非空**
- 每一行都解得出 `BillingComponent`（`decodeAll`）
- `componentID` 集合**无重复**
- **`exactBillingSum(各行 amountCents) == totalCents`** —— 核心恒等式

`@anchor const billingComponents :: "billing.mtd.components"`
`@anchor const billingTotalCents :: "billing.mtd.total_cents"`
`@anchor test Bigbird/Stats/PulseStatsTests/BillingMetricsTests.swift :: testValidBundleUsesIntegerCentsAndConserves`

`BillingComponent.init?(row:)` 另有五条：
键集合必须**恰好等于** `{component_id, amount_cents, currency, attribution}`（多一个键即作废）、
`component_id` 非空且无首尾空白、`currency` 必须**字面量 `"USD"`**、
`attribution` 必须 ∈ `{cc_mtd_output_share, unattributed_no_agent_source}`。

`@anchor symbol BillingComponent`

### 4.5 `billingAttribution` 的准入条件

先要 `billingFacts` 成立（**归因是账单的下游，账单不成立归因必不成立**），
再对四个 metric（`billing.mtd.period` / `by_agent` / `attributed_cents` / `unattributed_cents`）：

- 四个**全都在**（`metrics.count == ids.count`）
- 全部 `status == .available`
- 全部 `window == "month_to_date_cst"`
- `quality` **四者必须一致**，且 ∈ `{validated, descriptive}`
- `sampleCount` **四者必须一致**且 `>= 0`（与 `billingFacts` 要求 `nil` 相反）
- unit 依次是 `list` / `list` / `cents` / `cents`
- `period` 的行数**必须恰好 1**
- **`coverage == full` ⟺ `quality == validated`**；`coverage == partial` ⟺ `quality == descriptive`
- `coverage == full` 或 `sampleCount > 0`
- agent 名无重复
- `exactBillingSum(各 agent attributedCents) == attributed`
- **`attributed + unattributed == facts.totalCents`** —— 第二条核心恒等式
- `unattributed >= ` 所有 `attribution == unattributed_no_agent_source` 组件之和

`@anchor const billingPeriod :: "billing.mtd.period"`
`@anchor const billingAttributedCents :: "billing.mtd.attributed_cents"`

`BillingPeriod.init?(row:)` 另有：键集合恰好五个、
`month` 必须匹配正则 `^\d{4}-(0[1-9]|1[0-2])$`、
`timezone` 必须**字面量 `"Asia/Shanghai"`**、
`daysExpected > 0` 且 `daysCovered <= daysExpected`、
`coverage` 与天数一致（`full` ⟺ 相等，`partial` ⟺ 小于）。

`@anchor const timezone :: "Asia/Shanghai"`
`@anchor test Bigbird/Stats/PulseStatsTests/PanelViewModelTests.swift :: testPartialPeriodNamesZeroCompleteDaysAndMissingCompletenessMarker`

输出排序：`outputTokens` 降序，**同值按 `agent` 字典序** —— 这是全 `Core/` 唯一带二级键的排序。

### 4.6 缺数绝不变 0

`billingScalarInteger(_:)` 只接受 `case .number`，
其余形状（含 `.null` / `.unsupported`）一律 `nil`。

`@anchor test Bigbird/Stats/PulseStatsTests/BillingMetricsTests.swift :: testUnknownMetricValueNeverBecomesZero`
`@anchor test Bigbird/Stats/PulseStatsTests/PanelViewModelTests.swift :: testWrongMissingOrUnknownBillingValuesNeverBecomeZero`

---

## 5. 派生层之三：`PanelViewModels`（760 行）

### 5.1 `RelayViewModel`：31 个存储属性，除 `hasSnapshot` 外全部可选

构造函数**纯映射、零副作用** —— 它在 SwiftUI `body` 里每次重绘都会重建，
构造时取数就是无限刷新。
（副作用契约的锚点在 `stats-app-presentation.md` §1.1。）

**`workingTokens`（「今天真用掉」）是本类型最该被盯的派生**：

- 定义：`inputTokensUncached + cacheCreationTokens + outputTokens`
- **不含** `cacheReadTokens`（缓存复读）
- **三项缺任一即整体 `nil`**，不把老快照缺失的 `cache_creation_tokens` 当 0
- 两步加法都走 `addingReportingOverflow`，溢出即 `nil`
- 窗口＝自然日，时区＝**UTC**（口径随 exporter，见 `stats-exporter-collection.md` §6.1）

`@anchor symbol workingTokens`
`@anchor test Bigbird/Stats/PulseStatsTests/PanelViewModelTests.swift :: testWorkingTokensIncludeCacheCreationButExcludeCacheRead`
`@anchor test Bigbird/Stats/PulseStatsTests/PanelViewModelTests.swift :: testMissingCacheCreationMakesWorkingTokensUnknown`

实测依据：2026-07-29 面板曾按 `inputTokens + outputTokens` 显示「1.2 亿」，
真实工作量 37 万，**大三百倍**，且涨跌主要取决于上下文被重读几遍。

### 5.2 两条钱的线必须分得清

`RelayViewModel` 同时持有两组钱，答的**不是同一个问题**：

| | `billingFacts` / `billingAttribution` | `mtdAgentCosts` |
|---|---|---|
| 答什么 | 配置总额由哪些组件构成、归给谁 | token 花在谁身上 |
| 精度 | **整数分，精确守恒** | 按输出占比**估算**，带小数 |
| 无证据时 | 说「证据不足」，不给数 | 照给（它本来就是估算） |
| 格式化 | `StatsFormat.usdCents` | `StatsFormat.usd` |
| 截断 | `visibleBillingAgents` 取前 `monthlyCostRows` 行 | `visibleMtdAgentCosts` 同 |
| 被截掉的钱 | `hiddenBillingAttributedCents` 累加 | `hiddenMtdAmortizedUSD` 累加 |

`@anchor test Bigbird/Stats/PulseStatsTests/PanelViewModelTests.swift :: testPerAgentMonthlyUsageSurvivesAlongsideTheBill`
`@anchor test Bigbird/Stats/PulseStatsTests/PanelViewModelTests.swift :: testBillingUsesIntegerCentsAndConservesAcrossTopSixAndTail`
`@anchor test Bigbird/Stats/PulseStatsTests/PanelViewModelTests.swift :: testKnownBillFactsSurviveInsufficientAgentAttribution`
`@anchor test Bigbird/Stats/PulseStatsTests/PanelViewModelTests.swift :: testAbsentMonthlyUsageIsUnknownNotEmpty`

行数上限走 `PanelThresholds.monthlyCostRows`（数值见 `stats-app-presentation.md` §6.1）；
被截掉的行**必须**以「另有 N 个，合计 $X」出现，钱不得静默消失。

`mtdUsageNote` 是口径声明句，两种形态：
无 `mtdDaysCovered` 时 `"按各家输出 token 占比估算，非账单归因"`；
有则追加 `"；数据覆盖本月 N 天"`。**报「本月」却只覆盖 17 天而不说，读者会当成整月的数。**
`@anchor symbol mtdUsageNote`

`billingAttributionMessage` 四档措辞，判据是 `quality` 与 `reason` 里的关键词：

| `quality` | `reason` 含 | 文案 |
|---|---|---|
| `.unavailable` | `invalid JSON` 或 `unreadable`（不分大小写） | `Agent归因源不可用；历史观测损坏` |
| `.unavailable` | 其余 | `Agent归因源不可用` |
| 其余 | `billing_component_id` 或 `provenance`（不分大小写） | `Agent归因证据不足；CC待来源确认` |
| 其余 | 其余 | `Agent归因证据不足` |

前置条件：`billingFacts != nil && billingAttribution == nil`
—— **账单事实还在但归因失败**才说话；账单本身就没有时保持沉默（另有渲染路径）。
`@anchor symbol billingAttributionMessage`

`codexUnattributedCents` 单列 Codex 的配置固定值 $20：筛
`componentID == "codex" && attribution == "unattributed_no_agent_source"`。
**不得摊给 Relay agent** —— 摊出去那一刻读者就分不清哪笔有证据、哪笔是猜的。
`@anchor test Bigbird/Stats/PulseStatsTests/PanelViewModelTests.swift :: testCodexTwentyDollarsRemainExplicitlyUnattributed`

### 5.3 `TriageViewModel`：分档、排序、能力四分

- `alerts` 先全部映射成 `TriagedAlert`，**按 `occurredAt` 降序**（新的在前），
  再按 `tier` 切成 `act` / `watch` / `done` 三段。
  三段**必然构成完整划分**（每条 alert 恰好落一段，因为 `tier` 无第四值且缺失落 `.watch`）。
  `@anchor test Bigbird/Stats/PulseStatsTests/LiveTriageContractTests.swift :: testTiersPartitionEveryAlert`
  `@anchor test Bigbird/Stats/PulseStatsTests/PanelViewModelTests.swift :: testSplitsAlertsIntoThreeSections`
  `@anchor test Bigbird/Stats/PulseStatsTests/PanelViewModelTests.swift :: testSectionsAreNewestFirst`

- `openIncidents` 只取 `state == .open`（`.unknown` **不进**可见列表），
  排序走 `incidentComesFirst`，四级判据、**全确定**：
  1. `severityRank` 降序：`critical`=3 > `warning`=2 > `info`=1
  2. `updatedAt` 降序，**`nil` 排最后**（诚实的未知不装新）
  3. `incidentKey` 字典序升序
  4. `episodeID` 字典序升序
  `@anchor symbol incidentComesFirst`
  `@anchor test Bigbird/Stats/PulseStatsTests/PanelViewModelTests.swift :: testOpenIncidentsExcludeResolvedAndSortBySeverityThenRecentUpdate`
  `@anchor test Bigbird/Stats/PulseStatsTests/PanelViewModelTests.swift :: testNoOpenIncidentsProducesNoVisibleIncidentCards`
  `@anchor test Bigbird/Stats/PulseStatsTests/LiveTriageContractTests.swift :: testEveryReportedOpenIncidentHasOneVisibleCardSource`

- 能力四分：`capabilityValidated` / `capabilityPartial` / `capabilityNotImplemented` /
  `capabilityRetired` 各自 `filter().count`，四者之和**必须等于** `capabilityTotal`
  （`ImplementationStatus` 只有四个取值，构成完整互斥划分）。
  `capabilityFooter` 无快照时读 `"能力清单还没拿到"`；有快照即使全零也印四个 0。
  `@anchor symbol capabilityFooter`
  `@anchor test Bigbird/Stats/PulseStatsTests/PanelViewModelTests.swift :: testCapabilityFooterPartitionsDynamicInventoryIntoFourGroups`
  `@anchor test Bigbird/Stats/PulseStatsTests/PanelViewModelTests.swift :: testCapabilityFooterShowsAllFourZeroGroupsForMeasuredEmptyInventory`
  `@anchor test Bigbird/Stats/PulseStatsTests/LiveTriageContractTests.swift :: testCapabilityTallyDoesNotInflateCoverage`

- 两个 `init`：只带 `snapshot:` 的便利构造器**仅供测试与非传输调用方**，
  它用 `overall` 直接构造 `StatusPresentation`；生产 App 必须走带
  `presentation:` 的指定构造器（那条路加了缓存/过期降级）。
  `@anchor symbol StatusPresentation`

### 5.4 `TriagedAlert`：在 `｜` 处切一刀

exporter 把整行打包进 `Alert.reason`，形如
`07-26 10:15 起 · 最近 10:20 · 21 次 · pulse｜发生什么：…影响：…行动：…`

切分规则（分隔符是 **U+FF5C 全角竖线**，只切**第一个**）：

| 输入 | `meta` | `body` |
|---|---|---|
| 有分隔符，两侧都非空 | 前半（trim 空格） | 后半（trim 空格） |
| 有分隔符，后半为空 | `nil` | **前半**（文字仍可见，不留孤立 meta） |
| 有分隔符，前半为空 | `nil` | 后半 |
| 无分隔符 | `nil` | 整个 `reason` |

`@anchor symbol TriagedAlert`
`@anchor test Bigbird/Stats/PulseStatsTests/PanelViewModelTests.swift :: testSplitsReasonAtTheFullWidthBar`
`@anchor test Bigbird/Stats/PulseStatsTests/PanelViewModelTests.swift :: testReasonWithoutSeparatorBecomesBodyOnly`
`@anchor test Bigbird/Stats/PulseStatsTests/PanelViewModelTests.swift :: testReasonWithEmptyTailKeepsTheTextVisible`
`@anchor test Bigbird/Stats/PulseStatsTests/PanelViewModelTests.swift :: testOnlyTheFirstSeparatorSplits`

**`meta` 里的时刻已经是北京时间**（exporter 按 `DISPLAY_TZ` 渲染）。
`TriagedAlert` **全类型不碰任何时区** —— 再转一次就会偏移第二遍。

### 5.5 `MenuBarViewModel`：两条线各管各的

菜单栏只有这些东西：CODEX、CC、GROK 三个配额百分比，WI 要你出手计数，以及一个可选状态标记。

- `actCount` 直接读 `alertTiers.act`（App 不自己数）。
- `statusLamp` / `statusText` 全部来自 `SnapshotStatusSummary`，
  **配额不参与状态词着色** —— 100% 配额 + 运行正常 + 覆盖完整 仍读「正常」，
  因为「额度烧完不等于有事要人干」。
  `@anchor test Bigbird/Stats/PulseStatsTests/PanelViewModelTests.swift :: testExhaustedQuotaStillReadsNormalWhenNothingNeedsAction`
  `@anchor test Bigbird/Stats/PulseStatsTests/PanelViewModelTests.swift :: testActCountDrivesStatusWord`
  `@anchor test Bigbird/Stats/PulseStatsTests/PanelViewModelTests.swift :: testWatchAndDoneDoNotReachTheMenuBar`
- `shortStatusText` 是**空间不够时的降级**：按 `" · "` 切，只留第一段。
  取舍方向写死在这里 —— 挤的时候**牺牲句子、保住数字**，
  因为配额才是看这个菜单栏项的理由，「运行异常」「覆盖未知」是会持续几天、
  单独看不促成任何动作的内部诊断。
  `@anchor symbol shortStatusText`

### 5.6 `HarnessViewModel`：`todayLine` 的净额口径

- `todayLine`：`dayCreated` 与 `dayCompleted` **任一读不到即整句退回**
  `"今天的进出还没读到"`，不猜、不用 0 顶替。
  两个数都在时净额 = `dayCompleted - dayCreated`，
  `> 0` 读「净减 N 件」、`< 0` 读「净增 N 件」、`== 0` 读「持平」。
  `@anchor symbol todayLine`
- `throughputStats` 固定输出**四格**，id 依次 `done` / `day` / `wkdone` / `wk`，
  文案依次「今天收口」「今天新建」「本周收口」「本周新建」。
  后两格读的是 `isoWeek*`（自然周），**不是** `weekCreated`（滚动 7 天）。
  `@anchor test Bigbird/Stats/PulseStatsTests/PanelViewModelTests.swift :: testAllFourThroughputNumbersStayVisible`
  `@anchor test Bigbird/Stats/PulseStatsTests/PanelViewModelTests.swift :: testUnknownThroughputCountsStayUnknown`
- `completionRateText`：`dayCreated == 0` 时读 `未知`（没有分母就没有完成率）。
- `maxStalledDays`：`stalled == nil` 时 `nil`，空数组时 `nil`（`max()` 对空数组返回 `nil`）
  —— 这两种情况在**灯的判据里被同等对待**（`workItemBatch` 收到 `nil` 判绿）。
  登记于 §7：空数组与 `nil` 在这一处**没有**被区分开，与 §3.4 的通则不一致。

`concerns` 的构造规则（`RelayViewModel` 与 `HarnessViewModel` 各一份）见 §6
—— 两处的 `concerns` 目前**都无生产调用点**。

---

## 6. 空接线登记（有实现、有测试、生产零调用点）

> **判定方法**：不用工具的 `wire` 判据（主身已实测它在 Swift 上不可靠，
> 见 `stats-app-presentation.md` §13）。本节每一条都是
> **`grep -rn` 全仓独立复核 + 从 `@main` 出发的手工可达性判断**，方法写在 §9 取证命令里。
>
> 活的消费面只有两处（`PulseStatsApp.swift` 里唯一的 `MenuBarExtra`）：
> `StatsPopoverView`（面板）与 `PulseMenuBarLabel`（菜单栏标题）。
> `RelayPanelView` / `HarnessPanelView` / `RelayMenuBarLabel` / `HarnessMenuBarLabel`
> 四个 View 已在 `stats-app-presentation.md` §8 登记为死代码。
> **凡只被这四个死 View 消费的 Core 派生，就是传递性死代码。**

### 6.1 全仓零引用（连测试都没有）—— 用 `wire` 锚死

四个都是**属性/方法级**符号，符合 `wire` 的适用边界；每条都另用 grep 复核过。

- `StatsFormat.minutesOnly(milliseconds:)` —— 全仓仅**定义那一行**（`Core/StatsFormatting.swift:79`），
  无任何调用点，连测试都没有。
  **这一条刻意不绑 `wire` 锚点**：实跑过，工具会判 `broken` 并报「发现 1 处疑似调用点」，
  而那处正是 `static func minutesOnly(` 这行定义自己 —— `wire` 的 `call_like` 判据认 `符号(`，
  Swift 的**函数定义行天然带括号**，于是它把定义当成了调用。
  （另外四条能判对，只因为它们是计算属性 `var x: T {`，语法上没有括号。）
  结论正确、判据够不着，故降级为人工验：
  `manual`：跑 `grep -rn "minutesOnly" --include=*.swift Bigbird/Stats/`，
  **只该有 1 行输出且行首是 `static func`**。多出任何一行即本条失效。
- `HarnessViewModel.inFlightNotEscalated` —— 全仓仅定义。
  它算的正是大鸟 2026-07-28 问的「在飞 − 要你出手」那 8 件，
  但面板实际渲染的是 `harness.inFlight` 全量。
  `@anchor wire inFlightNotEscalated :: unwired`
- `HarnessViewModel.inFlightStale` —— 全仓仅定义。它是唯一消费
  `PanelThresholds.staleWorkItemDays` 做「停太久」筛选的地方；无人调 ⇒
  **面板上「停太久该扎眼」这条产品意图没有落点**。
  `@anchor wire inFlightStale :: unwired`
- `TriageViewModel.doneSummary` —— 全仓仅定义。对应 v3 spec §4「已经过去一句话」，
  与 §7 表格第 5 行（`watch`/`done` 不再渲染）是同一件事的两个证据。
  `@anchor wire doneSummary :: unwired`

### 6.2 只被测试引用，生产零调用点

- `RelayViewModel.codexUnattributedCents` —— 仍是空接线，**且经核查不必接**。
  `@anchor wire codexUnattributedCents :: unwired`

  2026-08-01 一度接进 `StatsPopoverView` 账单段，当天即撤回，两条理由：
  ① **冗余**——面板早已单列 Codex $20，由既有的 `componentAttribution()`
  渲染成「Codex　未归因（无 Agent 来源）　$20.00」（`StatsPopoverView.swift:503`，
  引入于 `279024b`）。产品红线「$20 必须单列、不得摊给 Relay agent」**已由该实现满足**。
  ② **接错了分支**——新增那行落在 `attribution` 分支内，而该分支要求
  `billing.mtd.attributed_cents` / `by_agent` / `unattributed_cents` 有值；
  生产实测三者 `status=unknown, value=None`（根因＝`billing_component_id`
  Relay 侧从未写过，见 `stats-billing-usage.md`）。**代码活着，但分支进不去**，
  真接上反而会在债修好后与既有那行重复显示。

  留下这条记录是因为它值得记：**「有实现无调用点」不等于「该补调用点」**。
  先查现状有没有别的实现已经满足契约——本例正是。判错代价＝加一行说同样话的死代码。
  与 `HistoryStore` 那笔**不同型**：那笔是真缺口（历史一行没写过，接线后生产实证已产出
  `history-v1.jsonl`），这笔是伪缺口。
  两笔现已都接上（`HistoryStore` 见 `stats-app-presentation.md` §11.1）。

  接线时踩到的坑值得留字：这个计算属性唯一能接的 `RelayPanelView` **本身
  是零引用死代码**，接到那上面等于白做。活面板是 `StatsPopoverView`
  （`PulseStatsApp.body` 里唯一被实例化的那个）。**「接线」的验收标准是
  接到活的渲染路径上，不是「有调用点」**。

  呈现上守两条：`nil`（快照里没有 Codex 组件）整行不显示，不渲染成 `$0`
  ——前者是「没在跑 Codex」，后者会是「跑了但没花钱」；且这一行既不并入
  上面的 Agent 行，也不并入未归因合计，混排就等于把它摊掉了。

### 6.3 传递性死代码（`wire` 判不出，故不绑锚点，只登记）

以下 Core 派生**有调用点，但调用者本身是死的**。工具的 `wire` 判据是「有没有人调我」，
答不了「调我的人自己是不是活的」，所以这几条**刻意不绑 `wire`** ——
绑了会得到误导性的 `holds`。

| Core 符号 | 唯一消费者 | 消费者状态 |
|---|---|---|
| `RelayViewModel.concerns`（含 `quotaConcern` / `leftoverDescription`） | `RelayPanelView:16` | 死 |
| `HarnessViewModel.concerns` | `HarnessPanelView:16` | 死 |
| `RelayViewModel.multiSessionAgents` | 仅 `concerns` 内部 | 传递死 |
| `RelayViewModel.outputShare` | `RelayPanelView:112` | 死 |
| `RelayViewModel.ccLivenessLamp` | `RelayPanelView:60` | 死 |
| `SnapshotMetrics.sessions(for:)` | 无 —— `RelayPanelView:179` 调的是它**自己的同名私有方法**（`:201`） | 名字撞车，Core 那份零引用 |
| `StatsFormat.duration(milliseconds:)` | `RelayPanelView:126` | 死（另有 6 处测试） |
| `StatsFormat.timeOnlyUTC(_:)` | `PanelViewModels:347`（在 `leftoverDescription` 里） | 传递死 |
| `HarnessViewModel.weekLamp` / `dayLamp` / `completionLamp` / `weekMenuBarText` / `dayMenuBarText` / `completedMenuBarText` / `completionRateText` | `HarnessPanelView` + `HarnessMenuBarLabel` | 两者皆死 |
| `RelayViewModel.ccMenuBarText` / `codexMenuBarText` | `RelayMenuBarLabel` | 死 |

**连带结论（重要）**：`RelayViewModel.concerns` 与 `HarnessViewModel.concerns`
是整个「要你操心的」派生逻辑 —— 配额告急、进程为 0、撞限流、多会话残留、
今日失败回合、停滞 wi、待接 handoff **共七类 Concern 的判据全在这里**，
`PanelViewModelTests.swift` 有 5 个测试盯着它们的措辞与阈值，
**而现在的面板一条都不渲染**。
`stats-app-presentation.md` §8 只登记了死 View，没登记它们背后这套活着的判据 —— 本节补上。

清不清、恢不恢复是产品判断，本 spec 只如实登记，**但不得把它们当作「已实现的能力」**。

---

## 7. 已知不一致与债（如实登记，不粉饰）

1. **两份 ISO-8601 解析器并存**：`SnapshotDecoder.parseISO8601(_:)` 与
   `SessionUsage.parseDate(_:)` 逻辑完全相同、代码不共享。
   改一处忘另一处，行内 `last_activity` 与顶层时间戳会有不同的容错行为。
2. **`AgentMonthlyCost.share` 有 `?? 0`**，是 §2.4 四处默认值之外的第五处。
   它只用于展示占比、不参与守恒，但破坏了「Core 里没有 `?? 0`」这条通则的整洁性。
3. **五个降序排序没有二级键**（§3.4），同值行次序非确定。
   `BillingAttribution.agents` 是唯一带二级键的。
4. **`maxStalledDays` 没区分「空数组」与「nil」**（§5.6），
   与 §3.4 立的通则不一致。后果轻微（都判绿灯），但判据不统一。
5. **`billingFacts` 要求 `sampleCount == nil`，`billingAttribution` 要求
   `sampleCount != nil` 且四者一致** —— 两块相邻数据对同一个字段的要求正好相反。
   代码里没写理由，我不编（列入 §8 未决）。
6. **`Metric.window` 是自由字符串**，计费侧硬校验 `"month_to_date_cst"`，
   用量侧（`SnapshotMetrics` 全部访问器）**完全不校验 window**。
   即 `usage.today.*` 的窗口若被 exporter 改了，Swift 侧不会发现。
7. **`spec-anchor` 的 `wire` 在 Swift 函数上会把定义误判成调用**（本轮新发现，
   补 `stats-app-presentation.md` §13 那两条之后的第三条）：
   判据 `call_like` 认 `符号(`，而 Swift 的 `static func 符号(` 定义行天然满足它。
   后果：**函数级的 `unwired` 永远绑不上**，只有计算属性（`var x: T {`，无括号）能判对。
   实证：`minutesOnly` 全仓仅 1 行命中（就是定义），工具却报「发现 1 处疑似调用点」判 `broken`。
   实操结论：Swift 仓里 `wire :: unwired` **只对计算属性与整文件级死代码可用**，
   绑函数必然假阳性，应改用 §11 的 grep 复核 + `manual`。
8. **时区在三处各钉一次**：`StatsFormat.displayTimeZone`（`Asia/Shanghai`）、
   `RefreshPolicy.timeZone`（`TimeZone(secondsFromGMT: 8 * 3600)`）、
   `BillingPeriod` 校验的字面量 `"Asia/Shanghai"`。三者当前一致，但没有共同真源，
   且第二个用固定偏移而非 tz 数据库标识符 —— 若中国改夏令时会与前两者分叉。
   （`RefreshPolicy` 本身归 `stats-app-presentation.md`，此处只登记这个跨文件的不一致。）

---

## 8. 未决问题（我判断不了的，不猜）

1. §7 第 5 条：`sampleCount` 在 facts 与 attribution 上要求相反，代码零注释。
   是刻意的语义区分（事实无样本 / 归因有样本），还是历史遗留？未决。
2. §6.2 的 `codexUnattributedCents`：是被面板重构时漏接，还是刻意先实现后接？
   git 历史我没查（本轮不做），未决。
3. `SnapshotMetrics` 完全不校验 `window`（§7 第 6 条）是刻意信任 exporter，
   还是缺一道闸？我倾向后者，但没有证据，不写成结论。
4. `MetricID` 的常量里（条数见 §3.2，不在此复刻），`inputTokens`（含 cache_read 的那个）在
   `RelayViewModel` 里仍被读进 `inputTokens` 属性，但活面板只渲染
   `inputTokensUncached` / `cacheCreationTokens` / `outputTokens` / `cacheReadTokens`。
   `inputTokens` 是留着给谁的？未决 —— 它有生产赋值点，故不算空接线。
5. `Incident.recoveryEvidence` 与 `Capability.evidence` 在 Swift 侧被解出来后
   有没有渲染点，本轮只查了 Core，未追到 View 层。未决。

---

## 9. 明确不覆盖的

- Python exporter 侧的任何逻辑 —— 见 `stats-exporter-collection.md`。
- 配置计费口径本身（配置 $270 怎么摊、最大余额法）—— 见 `stats-billing-usage.md`；
  外部现金账漂移见同文件 §7.8。
- 传输 / 缓存 / 历史 / 刷新策略 —— 见 `stats-app-presentation.md` §1 §2。
- `LampRules` 四条灯判据与 `PanelThresholds` 三个常量的**数值** —— 同上 §4 §6.1。
- View 层布局、字号、宽度预算 —— 同上 §5 §6。
- `StatusPresentation` / `SnapshotStatusSummary` 的文案矩阵 —— 同上 §3.3。
- 本轮**没跑** `xcodebuild`（ssh 跑 GUI 测试必崩，auto-memory
  `swift-gui-tests-unrunnable-over-ssh-compile-core-standalone`）。
  本 spec 引用的测试名全部经 grep 核对存在，**但本轮未实跑验证它们通过**。

---

## 10. 必须由真人看的（`manual`）

以下三条代码门与 fixture 发现不了，只有装机后看一眼才知道。
写观察步骤，不写「应当良好」。

1. **`manual`：残缺事故卡片的四个占位串真的出现在屏幕上。**
   观察步骤：等一条 `title` / `impact` / `suggested_action` 缺失的真实事故（或临时改
   `~/Library/Application Support/PulseStats/snapshot-v2.json` 里某条 incident 删掉这三个 key，
   然后打开面板）。屏幕上该出现「未知事故」「影响未知」「建议动作未知」三段中文，
   **不该**出现空白行或英文 `nil`。没做到会怎样：一条真事故在面板上变成一张空卡片。

2. **`manual`：`workingTokens` 三缺一时面板读「未知」而不是一个偏低的数。**
   观察步骤：把缓存里 `usage.today.cache_creation_tokens` 那条 metric 的
   `status` 改成 `warming_up`，打开面板看「今天真用掉」那一格。
   该显示 `未知`。没做到会怎样：显示一个看着精确、实际偏低的总量，而且没人会怀疑它。

3. **`manual`：面板上「本月固定订阅」与「本月各 Agent 用量」两块，
   外行读者能分清哪个是账单、哪个是估算。**
   观察步骤：打开面板，找一个不知道内情的人问一句「这两块钱是同一笔吗」。
   答不出「一个是账单一个是估算」即为未达标。
   没做到会怎样：估算被当成账单，一个月差出好几美元还没人发现。
   （这条是 §5.2 那张对照表的人类验收面；代码只能保证两者用不同的格式化函数，
   保证不了读者分得清。）

---

## 11. 取证命令

```bash
# 本 spec 的锚点自检（vultr 上可跑，不需要 GUI）
cd /Users/owner/Coevolution/Bigbird/PULSE/skills/spec-anchor
python3 src/scan.py --repo /Users/owner/Coevolution/Bigbird/PULSE \
  --spec-glob 'Stats/specs/stats-core-models.md' \
  --claim-dialect prose \
  --src-glob 'Bigbird/Stats/PulseStats/**/*.swift' \
  --test-glob 'Bigbird/Stats/PulseStatsTests/**/*.swift' \
  --history-path 'Bigbird/Stats/'

# §6 空接线的独立复核方法（不依赖工具的 wire 判据）
cd /Users/owner/Coevolution/Bigbird/PULSE/Bigbird/Stats
# 1) 全仓命中，看是不是只有定义那一行
grep -rn "minutesOnly\|inFlightNotEscalated\|inFlightStale\|doneSummary" --include=*.swift .
# 2) 只被测试引用的，看生产侧是否为空
grep -rn "codexUnattributedCents" --include=*.swift PulseStats/
# 3) 活的消费面只有这两个（从 @main 出发）
grep -n "MenuBarExtra" -A4 PulseStats/App/PulseStatsApp.swift

# 测试名核对（本 spec 引用的每个测试名都必须有一行命中）
grep -rho 'func test[A-Za-z0-9_]*' PulseStatsTests/ | sort -u
```
