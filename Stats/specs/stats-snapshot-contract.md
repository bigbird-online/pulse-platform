# SPEC：`pulse.monitoring.snapshot.v2` — 快照负载契约与分诊判据

> version: `0.1.4`
> status: `reverse_engineered_from_code`
> authority: Python 产、Swift 消费的**跨语言线上契约**（由代码反推，非设计前瞻）
> owner: Pulse
> modified: `2026-08-27`
> producer_root: `/Users/owner/Coevolution/Bigbird/PULSE/Bigbird/Stats/exporter/pulse_stats/`
> consumer_root: `/Users/owner/Coevolution/Bigbird/PULSE/Bigbird/Stats/PulseStats/Core/`
> 版本史：只看 git log，不在文件头复制 changelog

## 0. 这份 spec 管什么，不管什么

**管**：`pulse.monitoring.snapshot.v2` 这个 JSON 负载**在线上长什么样**——
顶层与每层嵌套的字段名、必填性、类型、取值域、真实数值，
以及 Python 侧写下的字段与 Swift 侧解出的属性**能不能对上**。
定义处是 `live_contract.py` 与 `producer.py`，
消费处是 `SnapshotModels.swift` 与 `SnapshotDecoder.swift`
（行数会漂不写死，`wc -l` 现算）。

**不管**（已有 spec 覆盖，本文只交叉引用不复述）：

| 主题 | 去哪看 |
|---|---|
| 采集流程、归约算法、`_collect_*` 各源口径、恢复判定四条路线 | `specs/stats-exporter-collection.md` |
| 账单归因、`billing.mtd.*` 六个 metric 的算法 | `specs/stats-billing-usage.md` |
| App 拿到快照之后怎么渲染、灯的判据、刷新策略、缓存 | `specs/stats-app-presentation.md` |

**取证基础**：本文每条字段级声明都对着 **2026-08-01 实跑**的两个东西核过——
（a）用生产 state 文件 `/home/pulse/.relay/monitoring/pulse-stats/incident-state-v1.json`
（78 条 incident）走 `build_snapshot()` 真实构造一份快照，逐层 `sorted(keys())`；
（b）线上抓下来的生产负载 fixture
`Bigbird/Stats/PulseStats/Resources/Fixtures/live-v3-triage.json`
（146 条 alert / 34 条 metric / 19 条 capability）。
两者**字段集完全一致**。凡本文写「实测」，指的就是这两份。

---

## 1. 顶层负载结构

`build_snapshot()` 当前返回的 dict 恰有 **12 个顶层键**。前 11 个键由
2026-08-01 的生产 fixture 取证；`global_authority` 是 2026-08-26 增加的
只读审计包络，由当前 producer 回归测试取证：

| 键 | 类型 | 必填 | Swift 属性 | Swift 解码方式 |
|---|---|---|---|---|
| `schema_version` | str | 是 | `schemaVersion` | `decode` 必填 |
| `generated_at` | ISO-8601 str | 是 | `generatedAt` | `decode` 必填 |
| `expires_at` | ISO-8601 str | 是 | `expiresAt` | `decode` 必填 |
| `baseline_version` | str | 是 | `baselineVersion` | `decode` 必填 |
| `overall` | object | 是 | `overall` | `decode` 必填 |
| `capabilities` | array | 是 | `capabilities` | `decode` 必填 |
| `incidents` | array | 是 | `incidents` | `decode` 必填 |
| `metrics` | array | 是 | `metrics` | `decode` 必填 |
| `alerts` | array | 是 | `alerts` | `decode` 必填 |
| `alert_tiers` | object | **产出侧恒有，消费侧可选** | `alertTiers` | `decodeIfPresent ?? .zero` |
| `provenance` | object | 是 | `provenance` | `decode` 必填 |
| `global_authority` | object | **产出侧恒有，消费侧可选** | `globalAuthority` | `decodeIfPresent`，缺失即证据 unknown |

### 契约

- `schema_version` 恒为字符串 `"pulse.monitoring.snapshot.v2"`，由 `build_snapshot()`
  硬编码，不随 `baseline_version` 变动。Swift 侧同名常量
  `PulseSnapshot.supportedSchemaVersion` 必须逐字节相等。
- `alert_tiers` 与 `global_authority` 是 Swift 已建模但允许缺席的两个顶层键。前者是 v3 分诊在 v2 schema 内的加法式扩展，
  加它时**没有**升 schema 版本号，所以老 exporter 产的快照必须仍能解开——
  Swift 因此对整个键用 `decodeIfPresent`，缺失读作 `AlertTiers.zero`。
  实测生产 fixture `live-v2a.json` 就没有这个键、`live-v3-triage.json` 有。
- 其余 10 个键在 Swift 侧一律 `decode` 必填：任一缺失整份快照解不开
  （`SnapshotDecoder` 把任何 throw 转成 `invalidPayload`，全份拒收）。
- `global_authority` 是 producer-required 的加法式审计包络；Swift 以
  `decodeIfPresent` 兼容旧缓存，但缺失时只能显示「真源范围未知」。只有嵌套 schema
  为 `pulse_stats_global_read_model/v1`、producer 已标 `validated/global/fresh` 且端覆盖
  守恒时，App 才显示「四端真源已验证」；版本不兼容、partial 或缺失都不得画绿。
- `expires_at` 由 `live.py` 固定为 `generated_at` 加 **45 分钟**，不可配置。
  45 不是拍的：消费端 `RefreshPolicy.interval`（`PulseStats/Core/RefreshPolicy.swift`）
  每 30 分钟取一次数，45 = 一个完整刷新周期 + 15 分钟宽限——「错过了整整一个
  刷新周期还没有更新的」才是 stale 的诚实起点。TTL 与刷新周期**是一个决定落在
  两个文件**，谁改 `RefreshPolicy.interval` 必须连这里一起改（`live.py` 注释原文）。
  历史：2026-08-02 之前是 180 秒且无来由，30 分钟刷新周期下约 90% 的时间
  假报「数据已过期」（auto-memory `undefended-constant-becomes-permanent-false-alarm`）。
- `baseline_version` 生产值 `"0.2.2"`（`live.py` 显式传 `BASELINE_VERSION`）；
  `build_snapshot()` 的形参默认值是 `"0.2.1"`，两者不一致见 §7 未决问题 1。

`@anchor symbol build_snapshot`
`@anchor const BASELINE_VERSION = "0.2.2"`
`@anchor symbol PulseSnapshot`
`@anchor const supportedSchemaVersion :: "pulse.monitoring.snapshot.v2"`
`@anchor test Bigbird/Stats/exporter/tests/test_fixture_contract.py :: test_all_v2_fixtures_share_required_shape_and_no_sensitive_fields`
`@anchor test Bigbird/Stats/PulseStatsTests/SnapshotDecoderTests.swift :: testPreV3SnapshotDecodesWithSafeDefaults`

### 1.1 `global_authority`（9 键）

`schema_version` `status` `scope` `authority_revision` `required_endpoint_count`
`materialized_endpoint_count` `materialization_digest` `freshness` `degraded_reasons`

- `schema_version` 恒为 `pulse_stats_global_read_model/v1`。
- 只有 authority revision 非空、它等于当前 Harness service checkout 完整 Git SHA
  所派生的 `sha256(bigbird-online/CollaborateWithAI@sha)`、surface registry 的四端
  全覆盖、每端 revision 相同、每端 digest 相同、且
  `observed_at <= generated_at < expires_at` 时，才允许 `status="validated"` 与
  `scope="global"`。任一证明缺失或冲突都必须降为
  `partial`/`unknown`，并将依赖 global authority 的指标与能力 fail closed。
- `authority_revision` 与 `materialization_digest` 在证明不成立时可为 `null`；
  `degraded_reasons` 只登记证明轴，不泄露四端路径、主机名或 WI 内容。
- Stats 是只读消费者。它从固定路径
  `/home/pulse/.local/state/pulse/stats/global-authority-evidence-v1.json` 读取
  `pulse_stats_global_authority_evidence/v1` carrier，并以版本化
  `config/scl-surfaces.yaml` 作为四端 inventory authority。carrier 必须逐项、同顺序
  覆盖 registry 的四个 `surface_id`；缺失、重复、额外、重排、未知 schema、超限或
  不可读均视为整份 carrier 无效，禁止回退扫描单一本地 Harness root。
- carrier 只负责把 SCL/agent-harness 已形成的聚合证明送进 Stats；revision、同 digest
  与 freshness 的语义校验仍由 `project_global_authority()` 统一执行。producer 从
  `provenance.source_revisions.harness` 取得实际数据源 SHA；缺失或与 carrier 不匹配时
  `degraded_reasons` 包含 `source_revision`，carrier provenance source 标为
  `freshness=error` / `error_code=source_revision_mismatch`。

`@anchor symbol load_global_authority_evidence`
`@anchor symbol project_global_authority`
`@anchor test Bigbird/Stats/exporter/tests/test_global_authority_carrier.py`
`@anchor test Bigbird/Stats/exporter/tests/test_global_authority.py`
`@anchor test Bigbird/Stats/exporter/tests/test_live.py :: test_fresh_carrier_cannot_validate_a_different_harness_checkout`

### 1.2 版本闸的方向性

- `SnapshotDecoder.decode()` 必须先只解 `schema_version` 一个字段（内部
  `SchemaEnvelope`），不等于 `pulse.monitoring.snapshot.v2` 即抛
  `unsupportedSchema` 并**停止解正文**。
- 闸是**精确相等**不是前缀或大版本比较：`"pulse.monitoring.snapshot.v3"` 会被拒。
  仓内确有一份 `unknown-major.json` fixture 写着 v3，它的**唯一用途是被拒**——
  `snapshot.v3` 在全仓没有任何产出方，Python 侧零处产生该字符串。
  别把它读成「已经有 v3 了」。

`@anchor symbol SnapshotDecoder`
`@anchor test Bigbird/Stats/PulseStatsTests/SnapshotDecoderTests.swift :: testRejectsUnknownMajorBeforePayloadDecode`

### 1.3 键名转换规则（跨语言对齐的机制）

Python 全用 snake_case，Swift 全用 camelCase，靠 `SnapshotDecoder.makeDecoder()`
里的自定义 `keyDecodingStrategy` 桥接。规则**不是** Foundation 内置的
`.convertFromSnakeCase`，是手写的：按 `_` 切分，首段原样，其余各段
**若恰等于 `id` 则转 `ID`，否则首字母大写**。

- 这条 `id → ID` 特例是必须的：`capability_id` 要落到 `capabilityID`、
  `episode_id` 要落到 `episodeID`、`event_id` 要落到 `eventID`。
  内置策略会给出 `capabilityId`，与 Swift 侧属性名不符，整份解不开。
- 2026-08-01 实测：把当时 Swift 已建模的 Python 侧 **11 组共 60 个字段名**逐个喂进这条规则的等价实现，
  产出与 Swift 侧声明的属性名**逐一相等，零失配**。详见 §5。
  2026-08-26 新增的 `global_authority` 9 个字段现由
  `GlobalAuthorityProjection` 建模，并由真实 snake_case JSON 解码回归逐项覆盖。

`@anchor symbol SnapshotCodingKey`

### 1.4 时间戳格式

- Python 侧一律 `datetime.isoformat()` 输出，且**必须**是 aware（带偏移）：
  `_parse_date()` 对 naive 时间戳抛 `ValueError`，不默认补 UTC。
- Swift 侧 `dateDecodingStrategy` 是自定义两段式：先试带小数秒的 ISO-8601
  （`.withInternetDateTime, .withFractionalSeconds`），再试不带的；两者都不成
  抛 `dataCorruptedError`，不猜。
- 两段式是必须的，因为 Python 的 `isoformat()` **带不带小数秒取决于
  microsecond 是否为 0**——同一个生产者会在两种形状之间来回跳。

`@anchor symbol _parse_date`
`@anchor symbol parseISO8601`

---

## 2. 各段负载的字段级契约

以下每个 key 集都是 2026-08-01 实测所得，非从类型声明誊抄。

### 2.1 `overall`（4 键）

`runtime_health` `coverage` `open_incident_count` `headline`

- `runtime_health` 取值域恰为 5 个：`ok` `warning` `critical` `stale` `unknown`
  （Swift `RuntimeHealth` 5 个 case 一一对应）。
- `coverage` 取值域恰为 3 个：`full` `partial` `unknown`（Swift `Coverage`）。
- `open_incident_count` 只数 `state == "open"` 的 incident，
  `event_only` 的 alert 一律不计。判据算法见 collection spec §3.1/§3.2，本文不复述。
- **守恒闸**：`SnapshotDecoder.decode()` 在解完正文后必须校验
  `overall.open_incident_count` 等于 `incidents` 里 `state == .open` 的条数，
  不等即 `invalidPayload("open_incident_count does not match incidents")`、整份拒收。
  这是全链路上唯一一条跨字段守恒检查。

`@anchor symbol reduce_runtime_health`
`@anchor symbol reduce_coverage`
`@anchor test Bigbird/Stats/exporter/tests/test_producer.py :: test_one_shot_snapshot_is_v2_json_and_event_only_does_not_increment_open_count`

### 2.2 `capabilities[]`（8 键）

`id` `implementation_status` `health` `aggregation_role` `scope` `reason`
`freshness` `evidence`

- 由 `_capability()` 单点构造，全字段恒在场，无一可选。
- `freshness` 是嵌套 object，恰 3 键：`state` `observed_at` `max_age_seconds`。
  `observed_at` 与 `max_age_seconds` 可为 `null`，Swift 侧对应 `Date?` / `Int?`。
- `scope` 恒为字面量 `"fixed production source"`（`_capability()` 的形参默认值，
  全仓无任何调用点覆盖它）。实测生产 fixture 19 条 capability 全是这个值。
- `evidence` 恒为字符串数组，可空数组但**不可缺席**（`list(evidence)`）。

取值域（左为 Python 实际产出，右为 Swift 枚举声明）：

| 字段 | Python 实际产出 | Swift 枚举 | 是否对齐 |
|---|---|---|---|
| `implementation_status` | `active_validated` `active_partial` `retired` `not_implemented` | `ImplementationStatus` 同 4 个 | 对齐 |
| `health` | `ok` `warning` `unknown` `stale` `not_applicable` | `CapabilityHealth` 6 个（多 `critical`） | 见 §5.2 |
| `aggregation_role` | `runtime`（`_capability()` 默认）、`coverage_only` | `AggregationRole` 同 2 个 | 对齐 |
| `freshness.state` | `fresh` `stale` `unknown` `error` `not_applicable` | `FreshnessState` 同 5 个 | 对齐 |

- `implementation_status` 的降级是**构造期强制**的：传入 `"active_validated"`
  但 `health` 或 `freshness` 为 `"unknown"` 时，`_capability()` 自动改写为
  `"active_partial"`。一条能力不得在任一结构化观测轴显式未知的同时自称已验证。
- 已测到的**失败**（`warning` / `critical` / `stale` / `error`）**不**触发降级：
  它证明集成在观测一个坏结果，而非证明集成缺席。

`@anchor symbol _capability`
`@anchor test Bigbird/Stats/exporter/tests/test_live.py :: test_unknown_current_evidence_downgrades_validated_to_partial`
`@anchor test Bigbird/Stats/exporter/tests/test_live.py :: test_measured_bad_state_does_not_downgrade_validated_maturity`

### 2.3 `metrics[]`（11 键）

`id` `capability_id` `status` `value` `unit` `window` `sample_count`
`quality` `scope` `observed_at` `reason`

- 由 `_metric()` 单点构造，11 键**恒在场**（值可为 `null`，键不缺）。
- Swift 侧却对 4 个用 `decodeIfPresent`：`value` `sample_count` `observed_at`
  `reason`。这不是失配——是消费侧比产出侧更宽松，为的是老 fixture 能解开。
  注释写明「`value` is absent in some fixtures」。
- `status` 的取值域**只有 2 个**：`available` 与 `unknown`。
  `_metric()` 唯一一处写 `status` 的代码是
  `"unknown" if unknown else "available"`，别的值无路径可达。见 §5.1。
- `quality` 实际产出 `validated` `descriptive` `unavailable` 三个
  （`insufficient` 在 Swift 声明里有，Python 侧零产出）。
- `unit` 实测 9 个值：`boolean` `count` `list` `map` `milliseconds`
  `percent` `seconds` `tokens` `unix_seconds`。
- `window` 实测 6 个值：`current`（`_metric()` 默认）、`5h` `24h` `7d`
  `today_cst` `month_to_date_cst`。
  生产 fixture `live-v3-triage.json` 里另有 `today_utc`——那是 2026-07-28
  时区口径纠正**之前**抓的快照，现行代码已不产该值（见 collection spec §9 冲突 1）。
  **别把它当现行取值域**。
- `scope` 是自由文本不是枚举，`_metric()` 默认同样是 `"fixed production source"`，
  但多数调用点会覆盖成一句说明。

**三态语义**（这段是 `_metric()` 的全部判断逻辑）：

- `value is None` → `status="unknown"`，绝不强转 0。
- 调用方显式传了 `max_age_seconds` **且** `observed_at` **且** `now`，
  且 `now - observed_at` 的秒数 `>= max_age_seconds` → 判超龄。
- 超龄时 `status="unknown"` **且 `value` 一并置 `None`**，`reason` 写人话年龄。
  超龄读数**扣住不发**而非附免责声明一起发：否则每个消费者都得记着先看
  `status` 才敢用 `value`，而菜单栏实测没做到。缺席是唯一不会被误读的信号。

`@anchor symbol _metric`
`@anchor symbol _format_age`

### 2.4 `alerts[]`（8 键）

`event_id` `projection_status` `severity` `capability_id` `title`
`occurred_at` `reason` `tier`

- 由 `_alert_dict()` 从 `AlertProjection` 单点构造，8 键恒在场。
- `projection_status` 恒为字面量 `"event_only"`——`AlertProjection` 的类型
  标注就是 `Literal["event_only"]`，无第二种取值。Swift 侧却把它解成裸 `String`
  而非枚举，见 §5.3。
- `severity` 取值域恰 3 个：`info` `warning` `critical`（Python `Severity`
  与 Swift `Severity` 一致）。
- `tier` 取值域恰 3 个：`act` `watch` `done`。Swift 侧对它用
  `decodeIfPresent` 且外套 `try?`——**缺失**与**取值不认识**两种情况都落
  `AlertTier.fallback`（= `watch`），不让整份快照解不开。
- `done` 档的 alert **仍留在 `alerts` 数组内**，不得被丢弃：分档只决定呈现、
  不决定留存。实测生产 fixture 146 条里 69 条是 `done`，全在数组里。

`@anchor symbol _alert_dict`
`@anchor symbol AlertProjection`
`@anchor test Bigbird/Stats/PulseStatsTests/SnapshotDecoderTests.swift :: testUnknownTierFallsBackToWatchRatherThanFailing`
`@anchor test Bigbird/Stats/PulseStatsTests/SnapshotDecoderTests.swift :: testKnownTierIsPreserved`

### 2.5 `incidents[]`（17 键）

`incident_key` `episode_id` `state` `severity` `capability_id` `scope`
`title` `what_happened` `impact` `suggested_action` `opened_at` `updated_at`
`resolved_at` `occurrence_count` `reopen_count` `evidence` `recovery_evidence`

- 由 `IncidentProjection.as_snapshot_dict()` 单点构造，17 键恒在场。
- `resolved_at` 可为 `null`（未闭环时）；其余时间戳恒为字符串。
- Python 侧 `state` 类型标注是 `Literal["open", "resolved"]`——**只有两个值**。
  Swift `IncidentState` 却有三个 case（多一个 `unknown`）。这是有意的前向兼容，
  见 §5.4。
- `evidence` / `recovery_evidence` 恒为字符串数组，元素形如 `event:<event_id>`。
- Swift 侧对 4 个展示字段做了局部降级（`title` `impact` `suggested_action`
  `updated_at`），身份与生命周期字段仍严格必填。降级细节属呈现侧，
  见 `specs/stats-app-presentation.md`。

`@anchor symbol as_snapshot_dict`
`@anchor symbol IncidentProjection`

### 2.6 `alert_tiers`（3 键）与 `provenance`（3 键）

- `alert_tiers` 恰 3 键 `act` `watch` `done`，值为 int。
  由 `build_snapshot()` 遍历 `alert_dicts` **当场数出来**，
  不是采集侧传进来的。实测生产 fixture：声明
  `{act: 3, watch: 74, done: 69}`，与 146 条 alert 逐条统计**完全相等**。
- Swift 侧 `AlertTiers` 三个计数器**各自**用 `decodeIfPresent ?? 0`，
  不是整体缺失才回退——单个计数器缺也能解开。
- **App 端不得自己数**：`PanelViewModels.swift` 读的是 `alertTiers.act`
  而非 `alerts.filter { $0.tier == .act }.count`。两者若不一致，
  诚实的读法是「产出侧截断或过滤了什么」，本地重算会把这种丢失盖掉。
- `provenance` 恰 3 键：`producer`（`{name, version}` 两键）、
  `source_revisions`（str→str 映射）、`sources`（数组，每条 4 键
  `id` `freshness` `observed_at` `error_code`）。
- `provenance.producer.version` 恒为 `"0.1.0"`，在 `live.py` 里硬编码，
  **与 `baseline_version`（`0.2.2`）是两个独立的号**，别混。

`@anchor symbol AlertTiers`
`@anchor symbol _source`
`@anchor test Bigbird/Stats/PulseStatsTests/SnapshotDecoderTests.swift :: testPartialAlertTiersDefaultsMissingCountersToZero`
`@anchor test Bigbird/Stats/PulseStatsTests/LiveTriageContractTests.swift :: testProducerTierCountsMatchTheAlertsThemselves`
`@anchor test Bigbird/Stats/PulseStatsTests/LiveTriageContractTests.swift :: testTiersPartitionEveryAlert`

---

## 3. 分诊判据（真实现行逻辑）

> 这一节写的是 `_AlertGroup.tier()` **此刻真在跑的代码**，
> 不是 `specs/pulse-stats-v3-alert-triage.md` 声称的那套。两者冲突处见 §6。

### 3.1 `tier()` 的现行逻辑：只有两个出口

`live_sources.py` 的 `_AlertGroup.tier()` 现行函数体，去掉注释后**只剩三行**：

1. `self.match_label in TIER_DONE_LABELS` → 返回 `TIER_DONE`；
2. 计算 `recovered = self.recovered_at(...) is not None`；
3. **无条件**返回 `TIER_WATCH`。

### 契约

- escalation 标签**没有任何路径**能到达 `act`。凡是走 `tier()` 的告警，
  出口只有 `done` 与 `watch` 两个。
- 第 2 步算出的 `recovered` 局部变量在函数体内**再无使用**——它不参与返回值。
  它仍被计算是因为 `_collect_alerts()` 另行调用 `recovered_at()` 统计
  `recovered_count` 用于 reason 文案（「N 件已自愈降档」）。
  换句话说：**恢复证据现在只影响那句人话，不影响分档**，因为无论恢复与否都是 `watch`。
- `TIER_DONE_LABELS` 恰 6 个 label：`autonomous_success_with_output`、
  `autonomous_assigned_work_completed`、
  `autonomous_success_with_output_invalidated`、`executor_reaped`、
  `autonomous_rate_limited`、`autonomous_deferred`。
- 未被显式分档的事件默认落 `watch`：`IncidentEvent.tier` 与
  `AlertProjection.tier` 的字段默认值都是 `TIER_WATCH`。
  举证责任在告警身上——证明不了自己值得打扰大鸟，就待在下面。

`@anchor symbol tier`
`@anchor symbol recovered_at`
`@anchor symbol TIER_DONE_LABELS`
`@anchor test Bigbird/Stats/exporter/tests/test_live.py :: test_labels_land_in_their_owner_decided_tier`
`@anchor test Bigbird/Stats/exporter/tests/test_live.py :: test_a_reviewed_label_in_neither_whitelist_defaults_to_watch`
`@anchor test Bigbird/Stats/exporter/tests/test_live.py :: test_no_escalation_label_ever_reaches_the_owner`
`@anchor test Bigbird/Stats/exporter/tests/test_live.py :: test_repetition_alone_never_reaches_the_owner`

### 3.2 `act` 的真源：`main_only`（两个载体）

`act` 不经 `tier()` 产生。真源判据是一个——`main_only: true`（**发起时就声明
「必须人来判断」**，分身按设计不能代答，与 wi 归谁无关）——但**载体有两个**，
缺一个都会漏报：

1. **有 handoff 的**：`_collect_alerts()` 调
   `usage_sources.collect_main_only_pending()` 拿未闭环的 `main_only: true`
   handoff，**直接**以 `tier=TIER_ACT` 造事件。
2. **裸 `main_only` wi（无 handoff）**：`collect_main_only_pending()` 内部再经
   `_bare_main_only_rows()`（`usage_sources.py`）补行——wi 自带 `main_only: true`
   但从未产生 handoff 时自成一行，`ack_state` 记 `no_handoff`。
   这是 2026-08-05 wi_851 隐身事故补上的第二载体（auto-memory
   `main-only-true-source-has-two-carriers-collector-read-one`：判据全对、
   采集器只扫 `handoffs/` 漏掉 wi 自带的）。

- 每条 act 事件的 `capability_id` 恒为 `"notify.action_package"`，
  `severity` 恒为 `"warning"`，`occurred_at` 恒为采集时刻 `self.now`
  （不是 handoff 创建时刻）。
- `event_id` 前缀 `mainonly-`，与走 `tier()` 那族的 `alert-` 前缀不同，
  两族在 `alerts` 数组里可凭前缀区分。
- 标题取序：`gist`（wi title）→ `asks`（`expected_output` 首行）→
  字面量 `"要你参与的协作"`。正文**不得为空**，格式**按载体分两种**
  （`live_sources.py` 造 reason 处）：
  有 handoff 时 `{from_owner}→{to_owner} · {waited} · {id}｜{body}`；
  裸 wi（`ack_state == "no_handoff"`）时两端 owner 同值，渲染 `ken→ken`
  是噪音，改直报归属 `{from_owner} 名下 · {waited} · {id}｜{body}`。
  其中 `waited` 是 `挂 N 天` 或 `今天新建`。
- 分档判据是「这条要不要人拍板」，**不是**「严不严重」。一个 P0 故障若修法明确、
  归 Pulse 执行，再严重也不进 `act`。

`@anchor symbol collect_main_only_pending`
`@anchor symbol _bare_main_only_rows`
`@anchor test Bigbird/Stats/exporter/tests/test_live.py :: test_a_main_only_handoff_is_what_reaches_the_owner`
`@anchor test Bigbird/Stats/exporter/tests/test_live.py :: test_a_cross_tenant_handoff_is_the_peers_job_not_the_owners`
`@anchor test Bigbird/Stats/PulseStatsTests/LiveTriageContractTests.swift :: testEveryActAlertIsActionable`
`@anchor test Bigbird/Stats/PulseStatsTests/LiveTriageContractTests.swift :: testActAlertsCarryHumanReadableCause`

### 3.3 进入分诊之前的两道前置闸

一条 escalation 记录要走到 `tier()`，先得过两关，**任一不过即整条消失**：

1. **label 白名单**：`match_label` 必须是 `ALERT_TEMPLATES` 的键
   （2026-08-10 现算 **36** 个；口径 = `live_contract.py` 里 `ALERT_TEMPLATES`
   字典的键数，复算命令见 §9）。
   不在表里的 label 被 `_collect_alerts()` 直接 `continue`
   丢弃，**连分档都到不了**，菜单栏一个字都看不到。
   `ALERT_TEMPLATES` 的值是 `(capability_id, title)` 二元组——
   alert 的 `capability_id` 与 `title` 都从这里取，不从源记录取。
2. **时间窗**：每条记录按自己的时间戳过 `ALERT_WINDOW_DAYS = 7` 天 cutoff。

`@anchor symbol ALERT_TEMPLATES`
`@anchor test Bigbird/Stats/exporter/tests/test_live.py :: test_an_untemplated_label_never_reaches_the_tiering_at_all`

### 3.4 严重度与分组

- 收敛单位是 `(match_label, subject)` 二元组一组，不是一条原始记录一条 alert。
- 组的 `severity` 取组内**见过的最高**值，不是最新那条的值。
- 组的 `occurred_at` 取组内 `last_seen`（最近一次），不是首次。

`@anchor symbol _AlertGroup`
`@anchor test Bigbird/Stats/exporter/tests/test_live.py :: test_group_severity_is_the_highest_seen_not_the_latest`

---

## 4. 数值契约（照抄代码真实值）

| 常量 | 值 | 管什么 |
|---|---|---|
| `ALERT_WINDOW_DAYS` | 7 | escalation 记录的读取时间窗（天） |
| `MAX_ALERT_GROUPS` | 200 | 收敛后的告警组上限；截断条数写进 reason，不静默丢 |
| `ALERT_MESSAGE_CHARS` | 220 | 源消息带进 alert reason 的最长切片 |
| `DECISION_REQUEST_SHARD_MONTHS` | 13 | 往回读几个月的事件分片文件 |
| `SESSION_ID_PREFIX_CHARS` | 8 | session id 在负载里只留前 8 字符 |
| `RECOVERY_UNIT_TIMEOUT_SECONDS` | 3 | systemd 恢复证据查询的自有预算 |
| `RECOVERY_AGENT_SUCCESS_STATUS` | `"success"` | 只认完成且成功的自治运行；不认 `started` |
| `RECOVERY_UNIT_TIMESTAMP_FORMAT` | `"%Y-%m-%d %H:%M:%S"` | systemd 时间戳专用解析格式 |
| `AUTONOMOUS_EVENTS_PREFIX` | `"autonomous_execution_event"` | 自治运行事件流的文件名前缀 |
| `BASELINE_VERSION` | `"0.2.2"` | 生产传入的基线号 |

- `MAX_PAYLOAD_BYTES` 写作 `1024 * 1024`，即 **1048576 字节（1 MiB）**。
  编码后 UTF-8 字节数超过它必须抛
  `LiveSnapshotError("snapshot payload exceeds 1 MiB")`，不得输出截断的伪快照。
  （源码是乘法表达式不是字面量，故本条绑 symbol 锚点而非 const 锚点。）
- `DISPLAY_TZ` 是 `timezone(timedelta(hours=8))`，**固定偏移**而非读机器本地时区：
  生产机跑 UTC、对端跑 CST，任何依赖本地时区的东西会让同一事件在两台机器上渲染成
  两个时间。

`@anchor const ALERT_WINDOW_DAYS = 7`
`@anchor const MAX_ALERT_GROUPS = 200`
`@anchor const ALERT_MESSAGE_CHARS = 220`
`@anchor const DECISION_REQUEST_SHARD_MONTHS = 13`
`@anchor const SESSION_ID_PREFIX_CHARS = 8`
`@anchor const RECOVERY_UNIT_TIMEOUT_SECONDS = 3`
`@anchor const RECOVERY_AGENT_SUCCESS_STATUS = "success"`
`@anchor const AUTONOMOUS_EVENTS_PREFIX = "autonomous_execution_event"`
`@anchor symbol MAX_PAYLOAD_BYTES`
`@anchor symbol DISPLAY_TZ`
`@anchor symbol LiveSnapshotError`
`@anchor test Bigbird/Stats/exporter/tests/test_live.py :: test_all_core_sources_fail_and_payload_limit_fail_closed`

### 4.1 负载里不得出现的东西

`PROHIBITED_KEYS` 共 **6 个**键名（2026-08-10 现算；口径 =
`tests/test_fixture_contract.py:9` 那个集合字面量的元素数）——
`message` `prompt` `token` `user_id` `session_id` `command` ——
在整份快照的**任意嵌套层**都不得出现。
这条由 `test_all_v2_fixtures_share_required_shape_and_no_sensitive_fields`
对每份 fixture 递归全键扫描守住。

`@anchor test Bigbird/Stats/exporter/tests/test_live.py :: test_no_credential_material_can_reach_the_rendered_snapshot`

---

## 5. 跨语言一致性登记（本文最有价值的部分）

方法：把 2026-08-01 时 Swift 已建模的 Python 侧 11 组共 60 个字段名逐个喂进 Swift `keyDecodingStrategy`
的等价实现，与 Swift 侧声明的属性名比对；再把 Python 实际产出的枚举取值域
与 Swift 枚举声明的 case 集比对。2026-08-01 实跑。

### 5.0 字段名：60/60 对得上

当时的 11 个 object（snapshot / overall / capability / freshness / incident /
metric / alert / alert_tiers / provenance / producer / source）
共 60 个字段，转换后**逐一命中** Swift 属性名，**零失配**。
`_id → ID` 那条特例覆盖了 `capability_id` `episode_id` `event_id` 三处。

`global_authority` 是后续增加的第 12 个 object；原 60/60 仍是 2026-08-01 的历史取证，
新增 9 个字段另由 `testGlobalAuthorityProjectionDecodesWithoutChangingSnapshotV2`
覆盖，并由 `GlobalAuthorityViewModel` 的缺失、版本不兼容与 validated 回归守住呈现边界。

**字段名层面没有漂移。** 下面登记的全是**取值域**与**方向性**的不对称。

### 5.1 `MetricStatus`：Swift 认 4 个，Python 只产 2 个

| 值 | Swift `MetricStatus` | Python 产出 |
|---|---|---|
| `available` | 有 | **有** |
| `unknown` | 有 | **有** |
| `warming_up` | 有 | **零产出** |
| `not_applicable` | 有 | **零产出** |

`_metric()` 里写 `status` 的代码只有一处：
`"status": "unknown" if unknown else "available"`。另两个值在 Python 侧
**没有任何产生路径**（全仓 grep `warming_up` 只命中两份测试的合法值集合
`VALID_METRIC_STATUSES`，无一处产出）。

判断：**不是缺陷，是消费侧比产出侧宽**。Swift 多认两个不会出错。
但两份测试把 4 个值都列进「合法」，会让读的人以为产出侧真会给出 4 种。
登记在此以免下次有人按 `warming_up` 写渲染分支——那个分支永远不会执行。

`@anchor symbol MetricStatus`

### 5.2 `CapabilityHealth`：Swift 多一个 `critical`，Python 侧不产

Python `_capability()` 的 `health` 实参穷举（静态 AST 扫全部调用点 + 生产
fixture 实测）得 5 个值：`ok` `warning` `unknown` `stale` `not_applicable`。
Swift `CapabilityHealth` 声明 6 个 case，多一个 `critical`。

判断：同 §5.1，消费侧宽于产出侧，不致错。但注意 **`critical` 在
`overall.runtime_health` 这一层是真会出现的**（`reduce_runtime_health()`
遇 open 且 critical 的 incident 会返回它）——只是**单条 capability 的
`health` 字段**从不为 `critical`。两个层级同名不同域，容易看混。

`@anchor symbol CapabilityHealth`

### 5.3 `projection_status`：Python 是单值 Literal，Swift 解成裸 String

Python `AlertProjection.projection_status` 的类型标注是
`Literal["event_only"]`——**只有一个合法值**，且 `reducer.py` 里唯一的构造点
写死 `projection_status="event_only"`。

Swift `Alert.projectionStatus` 却声明成 `let projectionStatus: String`，
不是枚举。

判断：**方向性不对称，不是错误但丢了约束**。Swift 侧因此无法在解码期发现
产出侧写错了这个字段（比如打错成 `event-only`），只能等到渲染时才可能露馅。
其余同类字段（`severity` `tier` `state`）Swift 都用了枚举，唯独这个没有。
是有意保守还是疏漏，代码注释没说 —— 列入 §7 未决问题 2。

`@anchor symbol AlertProjection`

### 5.4 `IncidentState`：Swift 多一个 `unknown`，是**有意**的前向兼容

Python `IncidentProjection.state` 是 `Literal["open", "resolved"]`，两个值。
Swift `IncidentState` 三个 case，多 `unknown`，且解码方式特殊：

```
let rawState = try? container.decode(String.self, forKey: .state)
state = rawState.flatMap(IncidentState.init(rawValue:)) ?? .unknown
```

判断：**这个不对称是设计而非漂移**，Swift 注释写明了理由——
未来 exporter 若加第三种状态，老 App 应把它落在 `unknown` 保留待审，
而不是猜进用户可见的 open 列表。与 `AlertTier.fallback` 同一套思路。

`@anchor symbol IncidentState`

### 5.5 两个 metric：Python 发出去，Swift 一行都不读

全仓比对「Python 侧产出的 id 字面量」与「Swift 侧出现过的字符串字面量」，
得两条 Python 产、Swift 零引用的 metric：

| metric id | 产出条件 | Swift 引用数 |
|---|---|---|
| `harness.unreadable_records` | 仅当 `unreadable` 非零（有坏 YAML 记录被跳过） | 2（2026-08-01 接线前为 0） |
| `usage.today.malformed_lines` | 仅当 `usage["malformed_lines"]` 非零（有不可解析的观测行） | 2（同上） |

两条都是**数据质量指标**：它们恰恰在「采集踩到坏数据」时才出现，
而那正是最该让人看见的时刻。接线前 App 侧对它们没有任何渲染分支，
于是这两个信号发出来即石沉大海。

**2026-08-01 已闭合**，修法取的是本节末尾说的第二条路——折进已有的注脚机制，
而不是给它们各开一个常驻格子：

| metric id | Swift 侧落点 | 呈现 |
|---|---|---|
| `harness.unreadable_records` | `HarnessViewModel.unreadableRecords` → `.ledgerQualityNote` | 四格吞吐量下方一句「另有 N 条记录无法解析已跳过，上列计数是下界」 |
| `usage.today.malformed_lines` | `RelayViewModel.malformedLines` → `.usageQualityNote` | 本月用量注脚后一句「另有 N 行日志无法解析已跳过，今日用量是下界」 |

不做常驻格子的理由：它们是**读数的可信度修饰符**，不是独立指标——「跳过了 3 条」
本身不构成行动，它的意义全在于让「今天新建 11」被读成下界而非准数。而常驻格子
99% 的时间显示 `--`，违反「面板每一行都得对大鸟构成行动」。两个计算属性都在
`> 0` 时才返回非 `nil`，显式的 `0` 同样不显示。

对照：生产 fixture `live-v3-triage.json` 里 **34 个 metric id
Swift 全都有引用**——因为那次抓快照时恰好没有坏数据，这两条没被产出。
**只看一份健康快照对不出这个缺口**，必须比对代码里的产出点。

判断：这是真缺口，不是设计。修法归属呈现侧（要么 App 加渲染、
要么把它们折进已有的 `quality` 降级信号），本文只登记不主张——
2026-08-01 由呈现侧取了后者，见上表。

两条 metric id 都是**带点的字符串字面量**（`"harness.unreadable_records"`），
不是标识符，spec-anchor 的 symbol 锚点按完整标识符匹配、绑不上带点的名字。
故绑各自产出点的宿主符号 + 一条人工观察步骤：

`@anchor symbol _collect_harness_metrics`
`@anchor symbol malformed_lines`
`@anchor manual 两个数据质量 metric 已在 Swift 侧接上渲染 :: 跑 grep -rn "unreadable_records\|malformed_lines" Bigbird/Stats/PulseStats/，应命中 MetricID 的两条常量定义（HistoryStore.swift 里那句 "an unreadable file" 是英文注释不算）；若回落到零命中，说明渲染被谁摘掉了，§5.5 的缺口重新张开

### 5.6 `alert_tiers` 的计数由谁算：产出侧两处各算一遍

- `_collect_alerts()` 内部维护 `tier_counts`，**只统计
  `ordered[:MAX_ALERT_GROUPS]` 这一截**，用于拼那句人话 reason
  （「要你出手 N · 我在盯 N · 已过去 N」）。
- `build_snapshot()` 另行遍历**全部** `alert_dicts` 算出 `alert_tiers`。

两者在告警组数**不超过 200** 时必然相等（实测生产 fixture 146 组，相等）；
超过 200 时 `tier_counts` 会少数被截断那部分，而 `alert_tiers` 数的是
实际进数组的条数。加上 act 那族是在 `tier_counts` 里单独 `+= 1` 的，
两条路径的口径本就不同源。

判断：**当前不构成 bug**（截断路径没被触发过），但两处各算一遍是漂移的种子。
列入 §7 未决问题 3。

`@anchor symbol tier_counts`

---

## 6. 与 `pulse-stats-v3-alert-triage.md` 的冲突（已证实）

v3 spec 仍把下列三个常量写成 `act` 的**现行判据**（48 小时窗在 v3 spec 里
被引用四处）。它们在 2026-07-28 就已零读取点，**2026-08-01 连常量本身一起从
`live_contract.py` 删除**：

| v3 spec 声称 | 代码实际 |
|---|---|
| `act` 需 `match_label ∈ TIER_ACT_LABELS` | 该常量已删；`tier()` 从不读它，标签无任何路径可达 `act` |
| `act` 第二必要条件 `last_seen ≤ TIER_ACT_MAX_AGE_HOURS`（48 小时） | 该常量已删；`tier()` 里已无任何老化逻辑 |
| 跨档升级：24 小时内 `occurrence_count ≥ TIER_ESCALATE_COUNT`（20）升 `act` | 两常量均已删；重复不再升档 |

**判断：代码对，v3 spec 已漂。** 三条都是 2026-07-28 大鸟第四轮重定
`act` 语义（从「严重的事」改成「等你或 Duke 裁决的决策请求」）后被拆掉的。
读 v3 spec 的人会以为菜单栏还在按 48 小时老化，实际不是。

### 6.1 死配置清理（2026-08-01 已执行）

以下 6 个常量曾在 `live_contract.py` 有定义而全仓零读取点，已于 2026-08-01
**全部删除**。判据保留在此，是为了让下一个读 v3 spec 的人知道它们**不是被漏
掉、是被有意拿走的**，不要照着 v3 再把它们加回来：

| 常量 | 原值 | 为什么能删 |
|---|---|---|
| `TIER_ACT_LABELS` | `frozenset()` | 已是空集，`tier()` 不读；判断本身（这一族都不该打扰大鸟）改以散文留在 `live_contract.py` 的 tier 段注释里 |
| `TIER_ACT_MAX_AGE_HOURS` | 48 | 老化规则 07-28 撤销 |
| `TIER_ESCALATE_COUNT` | 20 | 重复升档规则 07-28 撤销 |
| `TIER_ESCALATE_WINDOW_HOURS` | 24 | 同上 |
| `MAX_ALERTS` | 20 | 被 `MAX_ALERT_GROUPS = 200` 取代 |
| `DECISION_REQUEST_WINDOW_DAYS` | 400 | 从未接上；它想对齐的那件事改以散文注在 `DECISION_REQUEST_SHARD_MONTHS` 上 |

删除后 `tier()` 的对外行为逐字节不变——2026-08-01 用生产真实源前后各跑一次
`pulse-stats-snapshot.py --json`，`alert_tiers` 与 (tier, title) 多重集完全一致
（act 2 / done 66 / watch 67），metrics 与 capabilities 的 id 集合亦完全一致。

六条各挂一条 `absent` 锚点，机器判：

`@anchor absent TIER_ACT_LABELS :: 2026-07-28 大鸟定不用老化功能，随重复升档规则一并撤销`
`@anchor absent TIER_ACT_MAX_AGE_HOURS :: 同上，老化窗口概念已撤`
`@anchor absent TIER_ESCALATE_COUNT :: 重复升档规则 2026-07-28 撤销`
`@anchor absent TIER_ESCALATE_WINDOW_HOURS :: 同上`
`@anchor absent MAX_ALERTS :: 被 MAX_ALERT_GROUPS = 200 取代`
`@anchor absent DECISION_REQUEST_WINDOW_DAYS :: 从未接上；它想对齐的那件事改以散文注在 DECISION_REQUEST_SHARD_MONTHS 上`

判据是「代码里不该再有它的定义/赋值」。留档注释与字符串字面量里的追述**不算复活**
（靠命中行分类，见下），仅有活代码里重新出现赋值才判 `broken`。

原先这六条挂的是 `wire :: unwired` + `const <名字> = <值>`。**常量删掉之后这两
类锚点都不能再用**，原因值得写下来，这是本次清理最容易翻车的地方：

- `const` 锚点断言的是「源码里存在这个名字且值等于 N」。名字没了，它要么直接
  报「不存在」，要么——更糟——退而匹配到我为留档而写的**中文注释里那句
  「曾有一个 `DECISION_REQUEST_WINDOW_DAYS = 400`」**。后者会让闸看起来是绿的，
  实际锚的是一句散文而不是代码。2026-08-01 实测确实发生了：删完常量后
  `TIER_ESCALATE_COUNT` / `TIER_ESCALATE_WINDOW_HOURS` 两条 `const` 锚点仍报
  `holds`，靠的正是注释文本。**绿得毫无意义的锚点比红的更危险。**
- `wire :: unwired` 断言的是「有定义但没人调用」。定义都没了，这句话不再有
  真值可言——它此刻仍报 `holds` 纯属巧合（无定义自然也无调用点）。

这里原先退回 `manual`（判据写死成一条可照抄的 grep 由人跑），因为当时
spec-anchor 的五类锚点**全部是存在性断言，没有一类能表达「这个符号不该存在」**。

**2026-08-08 已补上 `absent` 类，本节改由机器判**（wi_20260805_616）。
这个缺口正是本节 2026-08-01 暴露出来后上报的，上游 Ken 落码、我移植进本副本。
移植时有一处必须记的实质改写：absent 依赖**命中行分类**（把命中分成
code / comment / string），而分类层在本副本原先不存在——上游那版把取数口径硬编成
一个模块级常量，本副本是方言参数化的函数（由 src-glob / test-glob 派生），
故仓库方言参数要从校验入口一路透传进去，不是拷函数能了事。

> 本段刻意不用反引号包源码符号名：本 spec 走 `--claim-dialect prose`，
> 散文里的反引号符号会被推断成 symbol 锚点，而这些名字属于 spec-anchor 工具自身、
> 不在 Stats 代码里——包了就会凭空多出一条永远 broken 的假锚点（2026-08-08 实测撞到）。

**没有分类层的 `absent` 一上就是一堆误报**：实测本节这六个名字在
`Bigbird/Stats/exporter/pulse_stats/` 下共有 6 处留档注释命中，
若不分类会逐条被当成「活代码里还有它」而误报 `broken`——比不合更坏。
所以分类层是 `absent` 的**前置件而非可选项**，收益排序在这里不适用。

### 6.2 一个已漂的测试名

`tests/test_live.py` 里有个用例叫
`test_a_probe_still_silent_since_it_broke_stays_act`，
**函数体断言的是 `watch`**（`self.assertEqual(alerts[0]["tier"], "watch")`）。
名字是 07-28 语义重定之前留下的，body 改了、名字没改。
测试本身是对的（它守的是「还坏着的不算自愈」），只是名字会误导。

`@anchor test Bigbird/Stats/exporter/tests/test_live.py :: test_a_probe_still_silent_since_it_broke_stays_act`

---

## 7. 未决问题（判断不了的，如实挂着，不注水）

1. **`build_snapshot()` 的 `baseline_version` 默认值 `"0.2.1"` 与生产传入的
   `BASELINE_VERSION = "0.2.2"` 不一致。** 生产路径永远显式传参，所以默认值
   只对不传参的调用方生效——我没查到还有谁不传。是留给测试的还是接线残留，
   代码里看不出。

2. **`Alert.projectionStatus` 在 Swift 侧为什么是裸 `String` 而不是枚举**
   （§5.3）。同一个结构体里 `severity` 和 `tier` 都用了枚举。是刻意给
   将来的 `incident` 类 projection 留口，还是写的时候漏了，注释没说。

3. **`tier_counts` 与 `alert_tiers` 两处各算一遍**（§5.6）是否该合并成一处。
   当前不构成 bug，因为 200 的截断从未触发过。但我没有实测过告警组数的
   历史峰值，判断不了这个截断到底有多远。

4. **`_metric()` 的 `scope` 字段是自由文本还是应该收敛成枚举。**
   `_capability()` 那边实测 19 条全是同一个字面量
   `"fixed production source"`，而 `_metric()` 这边多数调用点会写一句说明。
   两个同名字段一个像枚举一个像散文，是有意区分还是长歪的，看不出来。

5. ~~**`harness.unreadable_records` / `usage.today.malformed_lines` 该谁修**~~
   —— 2026-08-01 已定：呈现侧取「折进已有注脚」，不加常驻格子。见 §5.5。

6. **（覆盖盲区登记，2026-08-10）`verify-live-snapshot.py` 自身零契约。**
   它是本文 §2.1 守恒闸之外唯一对真实负载跑校验的活闸，但它的行为
   （查什么、SKIP 与 PASS 怎么区分、退出码语义）没有任何 spec 认领、
   也没有锚点守着——闸自己没人守。待立锚，本轮只登记不补。

---

## 8. 人工观察项（不可自动验）

`@anchor manual 顶层键集与线上负载一致 :: 生产跑 pulse-stats-snapshot.py --json，把输出管进 python3 -c "import json,sys;print(sorted(json.load(sys.stdin).keys()))"，应恰好输出 §1 表里那 11 个键；多一个或少一个都说明产出侧改了结构而这份 spec 没跟`

`@anchor manual alert_tiers 与 alerts 数组守恒 :: 生产跑一次 --json，用 python3 分别取 alert_tiers 三个计数之和、与 len(alerts)，两者应相等；不等说明 §5.6 那条截断路径第一次被真实触发了`

`@anchor manual Swift 能解开当天的真实负载 :: 在 ownermac 上把当天生产快照存成 fixture 跑 SnapshotDecoderTests，应全绿；ssh 环境跑不了 Swift GUI 测试，这条只能在本机做，且必须用真实负载而非手写 fixture——手写的形状是我以为的形状`

`@anchor manual 坏数据时两个 metric 发得出来且面板真的说了那句话 :: 往 .harness/work-items/ 放一份故意写坏的 YAML，跑 --json，确认 metrics 数组里出现 harness.unreadable_records；再打开菜单栏，四格吞吐量下方应出现「另有 N 条记录无法解析已跳过，上列计数是下界」。接线在 2026-08-01（§5.5），但这句话长什么样、够不够醒目，只有真人看一眼才知道。验完把坏文件删掉`

---

## 9. 取证命令

```bash
# 顶层与各层字段集（不花额度，用生产 state 走纯构造路径）
cd /Users/owner/Coevolution/Bigbird/PULSE/Bigbird/Stats/exporter

# 线上真实负载（已抓下来的生产快照）
python3 -c "import json;d=json.load(open('../PulseStats/Resources/Fixtures/live-v3-triage.json'));print(sorted(d.keys()))"

# 死配置逐个核
grep -rn "TIER_ACT_LABELS\|TIER_ACT_MAX_AGE_HOURS\|TIER_ESCALATE_COUNT\|TIER_ESCALATE_WINDOW_HOURS\|MAX_ALERTS\|DECISION_REQUEST_WINDOW_DAYS" pulse_stats/ --include=*.py

# ALERT_TEMPLATES 键数现算（§3.3 那个 36 的口径）
python3 -c "from pulse_stats.live_contract import ALERT_TEMPLATES; print(len(ALERT_TEMPLATES))"

# 全量测试
python3 -m unittest discover -s tests
```

### 9.1 跑 spec-anchor 必须带 Swift glob

本份 spec 是**跨语言**契约，锚点一半绑在 `.swift` 上。
只传 Python 的 `--src-glob` 会让 10 个真实存在的 Swift 符号被报成
「真漂移：曾存在但已被移除」——那是工具看不见文件，不是符号没了。
正确调用：

```bash
cd /Users/owner/Coevolution/Bigbird/PULSE/skills/spec-anchor
python3 src/scan.py --repo /Users/owner/Coevolution/Bigbird/PULSE \
  --spec-glob 'Stats/specs/stats-snapshot-contract.md' \
  --claim-dialect prose \
  --src-glob 'Bigbird/Stats/exporter/pulse_stats/**/*.py' \
  --src-glob 'Bigbird/Stats/PulseStats/**/*.swift' \
  --test-glob 'Bigbird/Stats/exporter/tests/**/*.py' \
  --test-glob 'Bigbird/Stats/PulseStatsTests/**/*.swift' \
  --history-path 'Bigbird/Stats/'
```
