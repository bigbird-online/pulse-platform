# SPEC：PULSE Stats exporter — 采集与归约侧真实契约

> version: `0.2.9`
> status: `reverse_engineered_from_code`（§6.7.1 已落地：9325c52 采集侧 + d2c4006 呈现侧）
> authority: exporter 采集/归约层的实现契约（由代码反推，非设计前瞻）
> owner: Pulse
> modified: `2026-08-28`
> code_root: `/Users/owner/Coevolution/Bigbird/PULSE/Bigbird/Stats/exporter/pulse_stats/`
> tests: `/Users/owner/Coevolution/Bigbird/PULSE/Bigbird/Stats/exporter/tests/`
> 版本史：只看 git log，不在文件头复制 changelog

## 0. 这份 spec 是什么，不是什么

**是**：从现存代码反推出的契约。覆盖 `producer.py` `reducer.py` `state.py`
`usage_sources.py` `live_sources.py` `live_contract.py` `global_authority.py` `live.py` 八个模块
（行数会漂不写死，`wc -l` 现算），对应 `tests/` 下全部用例
（2026-08-10 实跑 `python3 -m unittest discover -s tests`：245 个全过；
用例数以实跑输出为准，不沿用本行）。

**不是**：产品定义或设计前瞻。产品侧契约在 `specs/pulse-stats.md`（v1 状态栏）、
`specs/pulse-stats-menubar-v2.md`（v2 用量视图）、`specs/pulse-stats-v3-alert-triage.md`
（v3 分档）。**那三份写的是当时的意图，本份写的是此刻的代码。**两者冲突处在 §9 逐条列出。

写作理由：Stats 有 9382 行 Python + 8712 行 Swift（2026-08-01 时点实测，行数会漂），此前只有 README，无 spec。
大鸟判词「越优化问题越多」＝设计漂移。治法是先把真实契约固化成可机检的东西。

**金额边界**：本文件描述 exporter 当前实现；`MONTHLY_SUBSCRIPTION_USD=270.0` 是配置估算
常量，不是已核验发票。外部现金账真相与漂移见 `stats-billing-usage.md` §7.8。

---

## 1. 数据流：源 → 归约 → snapshot

一次 exporter 运行是一条无分支的直线，入口 `LiveSnapshotService.render_json()`：

```text
IncidentStateStore.single_writer()   ← 排他锁，全程持有
  └─ IncidentStateStore.load()        ← 读上一轮 incident 投影
       └─ LiveCollector.collect(existing)
            ├─ 并发（ThreadPoolExecutor, max_workers=2）
            │    ├─ _safe_codex_remote()   ssh 对端读 Codex 额度
            │    └─ _safe_cc_quota()       HTTPS 读 CC 额度响应头
            └─ 串行本地采集
                 _collect_heartbeats → _collect_ack → _collect_ledger
                 → _collect_systemd → _collect_alerts → _collect_revision
                 → _collect_usage_metrics → _collect_harness_metrics
                 → _collect_relay_metrics（此处 join 两个 future）
       └─ load_global_authority_evidence(carrier, surface_registry)
            └─ project_global_authority(evidence, harness source SHA)
                                                   → 四端聚合证明/降级原因
       └─ reduce_incident_events(events, existing)  → incidents + alerts
       └─ build_snapshot(...)                        → dict
       └─ json.dumps → 大小闸 → IncidentStateStore.save()
```

### 契约

- `LiveSnapshotService.render_json()` 必须在 `IncidentStateStore.single_writer()`
  的锁内完成读—归约—写全过程，不得先释放锁再落盘。
- `LiveCollector.collect()` 必须先提交两个远端 future 再跑本地采集，使远端延迟与
  本地采集重叠而非相加。
- 归约层 `reducer.py` 不得含任何 I/O：它只接收 `IncidentEvent` 序列返回投影。
- `producer.py` 只消费 caller 已归一化的数据，不得自行读盘或发请求。
- `global_authority.py` 只读固定 carrier 与版本化 surface registry，不得枚举或合并
  本机 `.harness`。carrier 不可读或与 registry 冲突时，live path 必须把 authority
  置为 unknown，而不是以空集或本机数据冒充 global。
- Harness WI/handoff/policy 指标只读登记的 Vultr service checkout；human checkout
  即使存在更新、更脏或包含尚未 canonical publish 的 WI，也不进入 Stats global
  数据面。collector 必须读取该 service checkout 的完整 40 位 Git SHA，producer
  以 `sha256(bigbird-online/CollaborateWithAI@sha)` 重算 carrier authority revision；
  二者不一致或 SHA 缺失时，即使 carrier 自身 fresh 且四端一致也必须降级。

<!-- anchor: symbol LiveSnapshotService -->
<!-- anchor: symbol LiveCollector -->
<!-- anchor: symbol reduce_incident_events -->
<!-- anchor: symbol build_snapshot -->
<!-- anchor: symbol load_global_authority_evidence -->
<!-- anchor: symbol project_global_authority -->
<!-- anchor: symbol IncidentStateStore -->
<!-- anchor: wire reduce_incident_events :: wired -->
<!-- anchor: wire build_snapshot :: wired -->

### 1.1 固定源清单（`LivePaths`，全部只读）

| 字段 | 路径 | 供给 |
|---|---|---|
| `registry` | `vultr-pulse-space/systemd/probe-heartbeats.json` | 心跳注册表（哪些源该活、各自 max_age） |
| `heartbeats` | `/home/pulse/.relay/heartbeats.json` | 心跳实测值 |
| `ledger` | `/home/pulse/.cache/pulse-ledger-lint-probe/incident-state.json` | 账本体检发现 |
| `alerts_dir` | `/Users/owner/Coevolution/.harness/events` | escalation 与 autonomous 事件流 |
| `state` | `/home/pulse/.relay/monitoring/pulse-stats/incident-state-v1.json` | 自有派生投影 |
| `work_items` | `.harness/work-items` | wi 计数、停滞、在飞、闭环证据 |
| `handoffs` | `.harness/handoffs` | 待接数、main_only 红点、接活人 |
| `policies` | `.harness/policies` | 各 agent 有无 `stale_work_items` 兜底扫描（§6.7.1 判 `unowned` 用；只读该字段，不读授权白名单） |
| `obs` | `/home/pulse/.relay/obs` | 用量事件流 |
| `cc_projects` | `/home/pulse/.claude/projects` | CC 429 判定 |
| `codex_sessions` | `/home/pulse/.codex/sessions` | Codex 额度 |
| `surface_registry` | `config/scl-surfaces.yaml` | versioned 四端 inventory authority |
| `global_authority` | `/home/pulse/.local/state/pulse/stats/global-authority-evidence-v1.json` | SCL/agent-harness 产出的只读聚合证明 carrier |
| `harness_repo` | `/home/pulse/.local/share/pulse/service-worktrees/collaboratewithai/doc-para/vultr` | GitHub-authoritative Harness service checkout 与完整 source SHA |
| `compute_cost_shadow` | 默认 `None`；正式 App 显式传 `/home/pulse/.local/state/pulse/compute-cost/managed-cost-current.json` | 只读 `managed_cost_snapshot.v1` shadow |

- 除 `compute_cost_shadow` 外，`LivePaths` 的每个字段必须是编译期固定的默认值，不得接受 host / path /
  command 入参。该 shadow 仅接受 exporter 的显式绝对路径参数或既有双环境变量门；两者均无时关闭。
- exporter 唯一允许的写入是 `state`（incident 投影）与 `CC_QUOTA_CACHE_PATH`
  （额度回退缓存），不得写任何被监控源。
- `global_authority` carrier schema 必须是 `pulse_stats_global_authority_evidence/v1`，
  且 required/materialized endpoint inventory 必须逐项等于 registry 的四个
  `surface_id`。Stats 不负责生产、修补或发布该 carrier；缺 authority revision、
  exact service-checkout source revision、四端覆盖、同 digest 或 freshness 任一证明时，所有依赖 global authority 的
  Harness 指标和能力必须 fail closed。ComputeCost 不属于这条 carrier。

<!-- anchor: symbol LivePaths -->
<!-- anchor: symbol HARNESS_SERVICE_REPO -->
<!-- anchor: test Bigbird/Stats/exporter/tests/test_external_input_contract.py :: test_global_wi_metrics_read_the_registered_service_checkout -->
<!-- anchor: test Bigbird/Stats/exporter/tests/test_global_authority.py :: test_fresh_carrier_for_another_harness_revision_fails_closed -->
<!-- anchor: const CC_QUOTA_CACHE_PATH -->

---

## 2. 归约不变量（`reducer.py`）

这一节是全 exporter 语义最密的地方，也是最该被机器守住的地方。

### 2.1 incident key 的构造与校验

`incident_key(capability_id, condition_key, normalized_scope_key)` 返回
`"<capability_id>:<condition_key>:<normalized_scope_key>"`。

- `normalized_scope_key` 长度必须落在 1–128 字符，否则抛 `ValueError`。
- `normalized_scope_key` 只允许 `a-z0-9._-`，且首字符必须是小写字母或数字；
  违反必须抛 `ValueError` 而非静默归一。
- key 里不得含 event id、`occurred_at`、severity 或展示文案——这四样每次都变，
  进 key 就等于每条事件开一个新 incident。

调用方一律先过 `_short_hash()`（SHA-256 取前 20 位十六进制），因此实际
scope key 恒为 20 字符小写十六进制，天然满足上面三条。

<!-- anchor: symbol incident_key -->
<!-- anchor: symbol _short_hash -->
<!-- anchor: test Bigbird/Stats/exporter/tests/test_reducer.py :: test_scope_key_must_be_explicitly_safe -->

### 2.2 状态机

`reduce_incident_events(events, existing)` 先按 `(occurred_at, event_id)` 全序
排序再逐条应用，因此**同一批事件的任意输入顺序产出同一投影**。

| 输入 kind | 原状态 | 新状态 | 必须成立 |
|---|---|---|---|
| `breach` | 不存在 | `open` | `occurrence_count=1`、`reopen_count=0`、`episode_id` 后缀 `#episode-1` |
| `breach` | `open` | `open` | `occurrence_count` +1；`updated_at`/`last_breach_at` 取 `max` |
| `breach` | `resolved` 且事件晚于 `resolved_at` | `open` | `reopen_count` +1，`episode_id` 变 `#episode-{reopen_count+1}`，`occurrence_count` 重置为 1 |
| `breach` | `resolved` 且事件**不晚于** `resolved_at` | 保持 `resolved` | 迟到的旧 breach 不得掀开已恢复的 incident |
| `recovery` | `open` 且事件晚于 `last_breach_at` | `resolved` | 写 `resolved_at` 与 `recovery_evidence` |
| `recovery` | `open` 但事件早于 `last_breach_at` | 保持 `open` | 只累加 `recovery_evidence`，不得改状态 |
| `recovery` | 不存在该 key | 不创建 | 恢复事件不得凭空造 incident |
| `event_only` | — | 不进 incidents | 落 `AlertProjection`，不计入 `open_incident_count` |

- **幂等**：`event_id` 一旦进入 `seen_event_ids`，同一 id 再来必须原样返回当前投影，
  `occurrence_count` 不得增加。
- **严重度单调**：`_apply_breach` 只在新事件 severity 排名更高时抬升
  （`_SEVERITY_RANK` = `{"info": 0, "warning": 1, "critical": 2}`），已升不得回落。
- **文案取新**：`title` / `what_happened` / `impact` / `suggested_action` 只在
  `event.occurred_at > current.updated_at` 时被覆盖，迟到事件不得改写更新的描述。
- **沉默不算恢复**：不存在任何「N 小时没再报即 resolved」的路径；只有显式
  `kind="recovery"` 事件能让 incident 转 `resolved`。
- `event_only` 事件按 `event_id` 去重，同一 id 只产生一条 `AlertProjection`。

<!-- anchor: test Bigbird/Stats/exporter/tests/test_reducer.py :: test_breach_opens_incident_with_stable_key -->
<!-- anchor: test Bigbird/Stats/exporter/tests/test_reducer.py :: test_repeated_breach_updates_one_incident -->
<!-- anchor: test Bigbird/Stats/exporter/tests/test_reducer.py :: test_same_event_is_idempotent -->
<!-- anchor: test Bigbird/Stats/exporter/tests/test_reducer.py :: test_positive_recovery_resolves_only_when_later_than_breach -->
<!-- anchor: test Bigbird/Stats/exporter/tests/test_reducer.py :: test_breach_after_resolution_reopens_new_episode -->
<!-- anchor: test Bigbird/Stats/exporter/tests/test_reducer.py :: test_late_old_breach_does_not_reopen_resolved_incident -->
<!-- anchor: test Bigbird/Stats/exporter/tests/test_reducer.py :: test_batch_order_does_not_change_projection -->
<!-- anchor: test Bigbird/Stats/exporter/tests/test_reducer.py :: test_event_only_is_alert_and_never_open_incident -->
<!-- anchor: symbol _apply_breach -->
<!-- anchor: symbol _apply_recovery -->
<!-- anchor: symbol _SEVERITY_RANK -->

### 2.3 tier 默认档

`IncidentEvent.tier` 与 `AlertProjection.tier` 的默认值必须是 `TIER_WATCH`：
未被显式分档的事件不得因遗漏而抵达大鸟。三个档常量 `TIER_ACT = "act"`、
`TIER_WATCH = "watch"`、`TIER_DONE = "done"` 定义在 `reducer.py`——该模块无内部
import，采集侧与产出侧都能引用而不成环。

<!-- anchor: const TIER_ACT = "act" -->
<!-- anchor: const TIER_WATCH = "watch" -->
<!-- anchor: const TIER_DONE = "done" -->
<!-- anchor: test Bigbird/Stats/exporter/tests/test_live.py :: test_an_event_without_an_assigned_tier_defaults_to_watch -->

---

## 3. 总体判据（`producer.py`）

### 3.1 `reduce_runtime_health(capabilities, incidents)`

判定顺序不可调换：

1. 有 `state == "open"` 且 severity 为 `critical` 的 incident → `"critical"`；
2. 有 open 且 `warning` → `"warning"`；
3. `aggregation_role == "runtime"` 的能力为空集 → `"unknown"`；
4. 任一 runtime 能力 freshness 为 `stale` → `"stale"`；
5. 任一为 `unknown` 或 `error` → `"unknown"`；
6. 以上都不成立 → `"ok"`。

- open incident 的严重度必须优先于 source freshness——有事故时不得因源新鲜而报 `ok`。
- runtime 能力集为空必须是 `unknown`，不得是 `ok`：没有可信信号 ≠ 一切正常。

<!-- anchor: symbol reduce_runtime_health -->
<!-- anchor: test Bigbird/Stats/exporter/tests/test_producer.py :: test_runtime_uses_open_incident_severity_before_source_freshness -->
<!-- anchor: test Bigbird/Stats/exporter/tests/test_producer.py :: test_runtime_is_stale_or_unknown_instead_of_false_ok -->

### 3.2 `reduce_coverage(capabilities, required_capability_ids)`

- required 集为空 → `"unknown"`；required 里一个都没出现在 capabilities → `"unknown"`。
- 任一在场能力的 freshness 为 `unknown` / `error` → `"unknown"`（先于 partial 判定）。
- 在场数少于 required 数 → `"partial"`。
- 任一能力 `implementation_status != "active_validated"` → `"partial"`。
- 任一能力 freshness 不是 `"fresh"` → `"partial"`。
- 全部齐备且全 fresh 才是 `"full"`。

**`partial` / `unknown` 永远不得被渲染成绿色**——这条在 exporter 侧的落点是：
coverage 与 runtime_health 是两个独立字段，producer 不得把 coverage 折叠进 health。

<!-- anchor: symbol reduce_coverage -->
<!-- anchor: test Bigbird/Stats/exporter/tests/test_producer.py :: test_coverage_requires_every_required_capability_validated_and_fresh -->

### 3.3 `build_snapshot()` 产出结构

固定顶层键：`schema_version` `generated_at` `expires_at` `baseline_version`
`overall` `capabilities` `incidents` `metrics` `alerts` `alert_tiers` `provenance`。

- `schema_version` 恒为 `"pulse.monitoring.snapshot.v2"`。
- `overall.open_incident_count` 只数 `state == "open"` 的 incident；
  `event_only` 的 alert 一律不计入。
- `alert_tiers` 是预先算好的三档计数字典，键为 `act` / `watch` / `done`，
  使菜单栏无需自行遍历 alert 列表。
- `done` 档的 alert 仍留在 `alerts` 数组内，不得被丢弃——分档只决定呈现，不决定留存。
- `baseline_version` 默认 `"0.2.1"`，但生产路径由 `live.py` 显式传入
  `BASELINE_VERSION = "0.2.2"`。
- `expires_at` 由 `live.py` 固定为 `generated_at + 45 分钟`。45 = 消费端
  `RefreshPolicy.interval`（30 分钟）+ 15 分钟宽限，TTL 与刷新周期是一个决定
  落在两个文件，来由与历史（2026-08-02 前为 180 秒无来由）见
  `stats-snapshot-contract.md` §1，不在此复述。

<!-- anchor: const BASELINE_VERSION = "0.2.2" -->
<!-- anchor: test Bigbird/Stats/exporter/tests/test_producer.py :: test_one_shot_snapshot_is_v2_json_and_event_only_does_not_increment_open_count -->

### 3.4 失败语义（`live.py`）

- `collected.core_successes == 0` 时必须抛 `LiveSnapshotError("all core sources
  unavailable")`，不得输出伪完整 snapshot。core 源恰为四个：heartbeat、ack、
  ledger、systemd（`core_successes` 是这四个布尔量之和）。
- 编码后 UTF-8 字节数超过 `MAX_PAYLOAD_BYTES = 1048576`（1 MiB）必须抛
  `LiveSnapshotError("snapshot payload exceeds 1 MiB")`。
- `projection_complete`（= heartbeat 且 ack 且 ledger 三者全成功）为假时：
  `runtime_health` 必须被 `runtime_health_override` 强制为 `"unknown"`，
  headline 写 `"Incident state is unknown"`，且能力
  `incident.current_state_projection` 的 freshness 记为 `error`。
- state 落盘条件：`loaded.status == "ok"` 或 `projection_complete` 为真。
  两者都不成立（首次运行且采集不全）时不得写盘——避免用残缺投影覆盖历史。
- `loaded.status == "corrupt"` 且将要写盘时，必须先 `backup_corrupt()` 备份原文件。

<!-- anchor: symbol LiveSnapshotError -->
<!-- anchor: const MAX_PAYLOAD_BYTES = 1024 * 1024 -->
<!-- anchor: symbol backup_corrupt -->
<!-- anchor: test Bigbird/Stats/exporter/tests/test_live.py :: test_all_core_sources_fail_and_payload_limit_fail_closed -->
<!-- anchor: test Bigbird/Stats/exporter/tests/test_live.py :: test_corrupt_state_is_backed_up_before_complete_rebuild -->

---

## 4. 派生状态持久化（`state.py`）

### 4.1 写入契约

- `STATE_SCHEMA_VERSION = 1`；load 时 `schema_version` 不等于该值必须判 `corrupt`。
- 写盘必须走同目录 `tempfile.mkstemp` → `os.fchmod(0o600)` → `write` → `flush` →
  `os.fsync` → `os.replace` → 再 `os.chmod(0o600)` → 对父目录 `fsync`。
  不得就地覆盖原文件。
- 父目录以 `mode=0o700` 创建。
- `single_writer()` 用 `fcntl.flock(LOCK_EX | LOCK_NB)`；已被占用必须抛
  `StateLockBusy` 而非阻塞等待——exporter 是一次性进程，等锁等于挂住调用方。

<!-- anchor: symbol single_writer -->
<!-- anchor: symbol StateLockBusy -->
<!-- anchor: const STATE_SCHEMA_VERSION = 1 -->
<!-- anchor: test Bigbird/Stats/exporter/tests/test_state.py :: test_atomic_0600_round_trip_with_bounded_state -->
<!-- anchor: test Bigbird/Stats/exporter/tests/test_state.py :: test_single_writer_lock_rejects_overlap -->

### 4.2 有界性（防止 state 无限膨胀）

| 常量 | 值 | 约束对象 |
|---|---|---|
| `MAX_EVIDENCE` | 32 | 单 incident 的 `evidence` 与 `recovery_evidence` 各自上限 |
| `MAX_SEEN_EVENT_IDS` | 128 | 单 incident 幂等集上限 |
| `MAX_SOURCE_POSITIONS` | 64 | `source_watermarks` / `source_cursors` 各自条目上限 |

- 写入时按上述上限取尾部切片（`[-MAX_EVIDENCE:]` 等）。
- 读取时超界必须判 `corrupt` 而非静默截断：`len(seen) > MAX_SEEN_EVENT_IDS` 或
  evidence 超界一律抛 `ValueError`。
- `source_watermarks` / `source_cursors` 非 dict、或条目数超 `MAX_SOURCE_POSITIONS`、
  或键值非字符串，一律判 `corrupt`。

<!-- anchor: const MAX_EVIDENCE = 32 -->
<!-- anchor: const MAX_SEEN_EVENT_IDS = 128 -->
<!-- anchor: const MAX_SOURCE_POSITIONS = 64 -->

### 4.3 解码严格性

- `state` 必须 ∈ `{"open", "resolved"}`，`severity` 必须 ∈
  `{"info", "warning", "critical"}`，否则整份 state 判 `corrupt`。
- 四个时间字段（`opened_at` `updated_at` `resolved_at` `last_breach_at`）必须
  timezone-aware；naive datetime 必须使整份 state 判 `corrupt`，不得默认补 UTC。
- 任何解码异常都收敛成 `StateLoadResult(status="corrupt", ...)` 并带 `reason`，
  不得向上抛——损坏的 state 应导致降级，不应导致 exporter 崩溃。
- 文件不存在返回 `status="missing"`，与 `corrupt` 分开：前者是首次运行，后者要备份。

<!-- anchor: symbol StateLoadResult -->
<!-- anchor: symbol _aware_datetime -->
<!-- anchor: test Bigbird/Stats/exporter/tests/test_state.py :: test_invalid_enum_or_naive_datetime_makes_whole_state_corrupt -->

---

## 5. 采集器逐源契约（`live_sources.py`）

通用规则：**任一源失败只降级自己那条能力／指标，不得中断整轮采集**；
拿不到的值一律 `None`（渲染成 `unknown`），**不得代以 0**。

### 5.1 心跳（`_collect_heartbeats`）

- 注册表 `schema_version` 必须为 1 且 `sources` 是 list，否则整条能力判
  `freshness="error"` 并返回失败。
- 先整体预扫一遍：任一 `enabled: true` 条目缺 `source`（str）或
  `max_age_seconds`（int），必须整源判错返回，不得只跳过坏条目继续。
- 单源 fresh 判据：`0 ≤ now - observed ≤ max_age_seconds`。**未来时刻不算新鲜**
  （下界是 `timedelta(0)`）。
- 时间戳字段按 `receivedAt` → `ts` 顺序取第一个可解析的。
- 任一启用源不 fresh，或启用源数为 0 → 能力 freshness 记 `stale`。
- 不 fresh 时产 `kind="breach"` 事件；此前该 key 为 `open` 且现已 fresh 时产
  `kind="recovery"` 事件。
- 有心跳时间的 stale breach 以 `observed + max_age_seconds` 作为稳定的状态起点
  和事件身份；不得把旧的成功心跳时间当作 breach 时间。这样恢复后同一成功心跳
  再次过期时会重开新 episode，而重复刷新仍保持幂等。没有心跳时间时，首轮以本次
  检测时间开 episode，后续 open 轮次复用该 episode 的 `opened_at`。
- 派生两条独立能力，各自 max_age：`s2.executor_reaper` ← `vultr-executor-reaper`
  （8100 秒）、`notify.daily_flush` ← `vultr-harness-daily-flush`（97200 秒）。

<!-- anchor: symbol _collect_heartbeats -->
<!-- anchor: test Bigbird/Stats/exporter/tests/test_live.py :: test_unknown_alert_label_is_dropped_and_future_heartbeat_is_not_fresh -->
<!-- anchor: test Bigbird/Stats/exporter/tests/test_live.py :: test_resolved_heartbeat_reopens_when_the_same_success_becomes_stale -->

### 5.2 ack 探针（`_collect_ack`）

- 调用 `ACK_ARGV` 固定命令，超时 `ACK_TIMEOUT_SECONDS = 20` 秒。
  该值远高于 runner 默认 4 秒，因为 ack-probe 要遍历全部 handoff（2026-07-27 实测 7.0s）；
  超时的后果不是少一个数，而是 `s2.ack_overdue` 整条降 `unknown` 进而拖垮
  `overall.runtime_health`。
- 返回码非 0、JSON 不可解析、缺 `ts`、`breaches` 非 list —— 任一成立即判
  `freshness="error"` 返回失败。
- **面板门限与探针门限分离**：探针的 15 分钟超期门限是给 dispatcher 的运维信号；
  进大鸟面板必须挂满 `OWNER_VISIBLE_ACK_OVERDUE_DAYS = 3.0` 天。未满 3 天的
  breach 必须从 `current_keys` 中移除（否则会被误判成「已恢复」而产恢复事件）。
- incident 的 `scope` 与 `title` 必须写明单号（`ho_...`），使大鸟能直接拿去查；
  `normalized_scope_key` 仍用哈希。
- 上轮 open 但本轮不在 `current_keys` 的，产 `kind="recovery"` 事件。
- 能力 health：`breaches` 非空 → `warning`，空 → `ok`；max_age 2100 秒。

<!-- anchor: symbol _collect_ack -->
<!-- anchor: const ACK_TIMEOUT_SECONDS = 20 -->
<!-- anchor: const OWNER_VISIBLE_ACK_OVERDUE_DAYS = 3.0 -->
<!-- anchor: test Bigbird/Stats/exporter/tests/test_live.py :: test_a_fresh_handoff_is_not_an_incident_for_the_owner -->

### 5.3 账本体检（`_collect_ledger`）——违例已退出事故面板（2026-08-04 大鸟裁，063a697）

**读取契约（仍然生效）**：

- state 文件存在时：`schema_version` 必须为 1、`keys` 必须是 list，否则判 error。
- state 文件**不存在**时只有一条合法路径：`vultr-ledger-lint-probe` 心跳 fresh，
  此时视为 `keys=[]`、fingerprint `"clean"`，观测时刻取心跳时刻。
  心跳不 fresh 则必须判 error——「没有发现文件」不等于「没有发现」。
- `source_cursors["ledger-state"]` 记 fingerprint 的短哈希，供跨轮比对。

**投影现状（2026-08-04 起）**：账本 finding **不再产生任何 breach 事件**——
`current_keys` 恒为空集，且消解循环会把存量 open 的账本事故全部关掉
（不留尸体在面板上）。台账违例是 agent 的日常活，第一出口是账上的单，
由日巡和 dispatcher 消化；真正该惊动大鸟的「自动处置链失灵」由探针在
24h 未自愈时推一次（11adfc1），不走本区块。**只关显示不关 capability**：
`s2.ledger_invariants` 的 health 仍照常上报（有 finding 记 `warning`），
仪表盘看得见「账本体检 N 条」。

- 消解事件的幂等 id 必须带 `observed` 时间戳：账本 clean 时 fingerprint 恒为
  `"clean"`，不带时间戳的 recovery id 每轮相同，第一条因时序没关成单后
  会被 `seen_event_ids` 永久吞掉（2026-08-04 实测五条事故永挂 open；
  auto-memory `idempotency-key-constant-in-healthy-state-blocks-retry`）。

**遗留函数（生产零调用，测试吊命）**：`_is_tenant_dispatcher_finding` /
`_ledger_finding_label` / `_tenant_agent_ids` 是 finding 还进面板那个时代的
投影管线，现只被测试引用。**没删是有意的**：它们承载「租户 agent 没有
dispatcher 是设计不是故障」这条判据（2026-07-31 大鸟定），若账本类日后以
别的形式回面板，判据仍成立、不该重踩。下次系统性舍弃一并处置。
下面三条 symbol 锚点因此只断言「还在」，不再对应任何生产行为。

<!-- anchor: symbol _collect_ledger -->
<!-- anchor: symbol _is_tenant_dispatcher_finding -->
<!-- anchor: symbol _ledger_finding_label -->
<!-- anchor: symbol _tenant_agent_ids -->
<!-- anchor: test Bigbird/Stats/exporter/tests/test_live.py :: test_a_tenant_agent_without_a_dispatcher_is_not_a_defect -->

### 5.4 systemd（`_collect_systemd`）

- 查询固定 7 个 unit（`SYSTEMD_UNITS`）：`relay.service` `degen-probe.timer`
  `ack-probe.timer` `harness-ledger-lint.timer` `executor-reaper.timer`
  `dual-heartbeat-watcher.timer` `harness-daily-flush.timer`。
- healthy 判据：7 个 unit 全部出现在输出中**且** `ActiveState == "active"` 的
  计数 ≥ 7。缺一即不 healthy。
- 不 healthy 时 health 记 `unknown`、freshness 记 `error`——不得记 `warning`，
  因为「查不到 unit」与「unit 挂了」在此不可区分。
- 额外派生 `s1.reply_quality_degen`：仅当 `degen-probe.timer` 为 `active` 时 ok。

<!-- anchor: symbol _collect_systemd -->
<!-- anchor: symbol SYSTEMD_UNITS -->
<!-- anchor: symbol SYSTEMD_ARGV -->

### 5.5 事件流与分档（`_collect_alerts`）

**读取**

- 按月分片读 `escalation_event-YYYY-MM.jsonl`，往回 `DECISION_REQUEST_SHARD_MONTHS = 13`
  个月。仅当月分片缺失才记源不可用；历史分片不存在是正常的。
- 当月缺失且一行都没读到时，记 `_source("escalation-events", "unknown",
  error_code="missing")` 但**必须继续执行**：「要你出手」的真源是 main_only handoff，
  与 escalation 流是两个独立源，不得共用一条失败出口。
- 逐行过滤，任一条件命中即跳过：`occurred_at < now - ALERT_WINDOW_DAYS(7 天)`、
  `match_label` 不在 `ALERT_TEMPLATES`（`KeyError` → continue）、
  带 `resolved_at`（已显式处置）、`is_drill is True`（演练自测）。
- 单行解析失败必须只跳过该行，不得中断整轮。

**收敛**

- 一件事 = `(match_label, subject)`。`subject` 取序：message 里的
  `_ALERT_SUBJECT_RE`（`wi_\d+_\d+` / `ho_\d+_\d+`）→ `agent_id` → 占位符 `"-"`。
  该函数必须对任意输入返回字符串，使分组无遗漏。
- `_AlertGroup.observe()`：`first_seen` 取 min，`last_seen` 取 max，
  message 取**最新那条**，severity 取最高档，`occurrence_count` 累加。
- 按 `last_seen` 降序保留前 `MAX_ALERT_GROUPS = 200` 组；被截断的条数必须写进
  能力 reason，不得静默丢弃。
- alert 正文摘录上限 `ALERT_MESSAGE_CHARS = 220` 字符，取首段并折叠空白。
- 展示时间必须带 `(UTC+8)` 后缀：同一份 snapshot 里 `occurred_at` 是 UTC 而
  reason 里是本地时间，不标注会被读成「未来告警」。

**恢复判定（`_AlertGroup.recovered_at`）** —— 四条独立证据，取最新者：

| # | 证据 | 适用 | 时间比较 |
|---|---|---|---|
| 1 | 心跳（`RECOVERY_HEARTBEAT_SOURCES` 三个映射） | 自报心跳的探针 | 必须晚于 `last_seen` |
| 2 | systemd `ExecMainStatus == 0` + 退出时刻（`RECOVERY_UNIT_NAMES` 两个映射） | 不发心跳的 unit | 必须晚于 `last_seen` |
| 3 | 该 agent 下一次 `status == "success"` 的自治运行 | `RECOVERY_AGENT_LABELS` 四个 label | 必须晚于 `last_seen` |
| 4 | subject 对应的 wi/ho 到达终态 | subject 是票据号时 | **不做时间比较** |

- 非零退出码必须**不贡献**任何恢复证据——那是 unit 在确认自己还坏着。
- 证据源读不到必须返回 `None` 而非空字典，使其退回原时效规则而不是把一切判成已恢复。
- 路线 3 的 subject 必须是 agent 本身：以 `wi_` / `ho_` 开头的一律排除，
  一张卡死的 wi 不会因为它的执行者在别处成功而被修好。
- 路线 4 刻意不做 `> last_seen` 比较：闭环是状态不是心跳。wrap_missing / ledger
  finding 这类告警是在收口**过程中**发出的，抱怨必然晚于闭环，加时间比较会让这条路
  永不触发（2026-07-28 实测 ho_20260727_294 07:48 闭环、告警 08:43 仍在要求出手）。
- 只认 `RECOVERY_AGENT_SUCCESS_STATUS = "success"`，不认 `started`。
- systemd 时间戳只接受 `UTC` / `GMT` / `Z` 三种后缀，其它时区缩写一律当无证据
  （`_parse_systemd_timestamp` 返回 `None`）——猜偏移可能把还坏着的探针判成已恢复。

**分档（`_AlertGroup.tier`）** —— 当前实现只有两个出口：

- `match_label ∈ TIER_DONE_LABELS`（6 个 label）→ `done`；
- **其余一律 `watch`**。escalation 标签**没有任何路径**能到达 `act`。
- `act` 不走 `tier()`：`_collect_alerts` 里 `collect_main_only_pending()` 返回的
  main_only 行（含 handoff 载体与裸 wi 载体，见 §5.6）直接以 `tier=TIER_ACT`
  产事件。
- 每张 act 卡的 `reason` 正文**不得为空**：标题答「这是什么事」，正文答「要我做什么」，
  空正文会让卡片只剩一句标题。

<!-- anchor: symbol _collect_alerts -->
<!-- anchor: symbol _AlertGroup -->
<!-- anchor: symbol recovered_at -->
<!-- anchor: symbol _alert_subject -->
<!-- anchor: symbol _parse_systemd_timestamp -->
<!-- anchor: const ALERT_WINDOW_DAYS = 7 -->
<!-- anchor: const MAX_ALERT_GROUPS = 200 -->
<!-- anchor: const ALERT_MESSAGE_CHARS = 220 -->
<!-- anchor: const DECISION_REQUEST_SHARD_MONTHS = 13 -->
<!-- anchor: const RECOVERY_AGENT_SUCCESS_STATUS = "success" -->
<!-- anchor: const RECOVERY_UNIT_TIMEOUT_SECONDS = 3 -->
<!-- anchor: test Bigbird/Stats/exporter/tests/test_live.py :: test_no_escalation_label_ever_reaches_the_owner -->
<!-- anchor: test Bigbird/Stats/exporter/tests/test_live.py :: test_a_main_only_handoff_is_what_reaches_the_owner -->
<!-- anchor: test Bigbird/Stats/exporter/tests/test_live.py :: test_labels_land_in_their_owner_decided_tier -->
<!-- anchor: test Bigbird/Stats/exporter/tests/test_live.py :: test_an_untemplated_label_never_reaches_the_tiering_at_all -->
<!-- anchor: test Bigbird/Stats/exporter/tests/test_live.py :: test_unreadable_recovery_evidence_falls_back_instead_of_healing_everything -->
<!-- anchor: test Bigbird/Stats/exporter/tests/test_live.py :: test_a_started_run_is_not_proof_the_agent_recovered -->
<!-- anchor: test Bigbird/Stats/exporter/tests/test_live.py :: test_unparseable_systemd_timestamps_are_not_evidence -->
<!-- anchor: test Bigbird/Stats/exporter/tests/test_live.py :: test_the_fresher_of_the_two_recovery_signals_wins -->
<!-- anchor: test Bigbird/Stats/exporter/tests/test_live.py :: test_an_explicitly_resolved_request_stops_being_reported -->
<!-- anchor: test Bigbird/Stats/exporter/tests/test_live.py :: test_top_level_tier_counts_match_the_alert_list -->

### 5.6 「要你出手」真源（`collect_main_only_pending` / `main_only_still_open`）——两个载体

判据只有三条，全在 `main_only_still_open()`：

1. `main_only is not True` → 不进（这是唯一入口条件，不看标签、不看严重度、不看归属）；
2. `cross_tenant is True` → 不进（跨租户默认带 main_only，但那个「人」是 Ken 不是大鸟）；
3. 挂的 wi 的 `status ∈ WI_OPEN_STATUSES = ("in_progress", "ready", "draft")` → 进。

**载体有两个，判据同一套**（漏第二个正是 2026-08-05 wi_851 隐身的根因，
auto-memory `main-only-true-source-has-two-carriers-collector-read-one`）：

- **handoff 载体**：`main_only: true` 的 handoff，`collect_main_only_pending()`
  主循环扫 `handoffs/` 产出；
- **裸 wi 载体**：wi 自带 `main_only: true` 但从未产生过 handoff，由
  `_bare_main_only_rows()` 补行，`ack_state` 记 `no_handoff`、两端 owner 同值。
  渲染侧（`live_sources.py`）对这族不渲染 `from→to`（`ken→ken` 是噪音）而
  直报「`{from_owner}` 名下」。

- **闭环与否必须看 wi 的 `status`，不得看 handoff 的 `ack_state`**。
  `main_only: true` 的语义是「分身按设计不能代答」，而 `ack_state` 恰恰是分身能改的字段——
  分身 ack 一下，本该给大鸟看的单就静默移出视野。
- `wi_status` 为 `None`（wi 文件缺失／坏 YAML／无 `work_item_id`）时必须判**仍开着**：
  宁可多报一条让大鸟看见，不可因读不到而隐藏。
- 「大意」取 wi 的 `title`（人手写的一句话主旨），取不到才退回 `expected_output` 首行。
  `expected_output` 是写给收方 agent 的验收规格，压成一行只会被截断成省略号。
- 目录不可读返回 `None`（未知），读到但没有返回 `[]`（确实没有）——两者不得混同。
- 按 `days` 降序排，等最久的排最前。
- `first_line()` 默认上限 110 字符，act 卡正文调用时传 `limit=60`。

<!-- anchor: symbol main_only_still_open -->
<!-- anchor: symbol collect_main_only_pending -->
<!-- anchor: symbol _bare_main_only_rows -->
<!-- anchor: symbol first_line -->
<!-- anchor: const WI_OPEN_STATUSES = ("in_progress", "ready", "draft") -->
<!-- anchor: test Bigbird/Stats/exporter/tests/test_live.py :: test_a_cross_tenant_handoff_is_the_peers_job_not_the_owners -->

### 5.7 闭环证据（`collect_subject_closures`）

- wi：`status` 不在 `WI_OPEN_STATUSES` 即视为终态，闭环时刻取 `updated_at`。
- handoff：`ack_state ∈ HANDOFF_TERMINAL_STATES = ("archived", "rejected",
  "timed_out")` 才算终态。**`accepted` 必须排除**——已接不等于已办完，
  把它算作恢复会在活还在做的时候把告警灭掉。
- handoff 无 `updated_at`，只能取 `created_at`。该值必然早于真实闭环时刻，
  因此只会让恢复显得**更旧**（可能漏灭一条告警），永不会提前灭灯——这个不对称是安全的那侧。
- 两个目录都不可读返回 `None`（无证据，退回时效规则）；读到但无闭环返回 `{}`。

<!-- anchor: symbol collect_subject_closures -->
<!-- anchor: const HANDOFF_TERMINAL_STATES = ("archived", "rejected", "timed_out") -->
<!-- anchor: test Bigbird/Stats/exporter/tests/test_usage_sources.py :: test_a_closure_clears_the_alert_even_though_it_predates_the_complaint -->
<!-- anchor: test Bigbird/Stats/exporter/tests/test_usage_sources.py :: test_the_later_of_closure_and_liveness_wins -->
<!-- anchor: test Bigbird/Stats/exporter/tests/test_usage_sources.py :: test_terminal_work_item_reports_its_closure -->

---

## 6. 用量与额度（`usage_sources.py`）

### 6.1 时区口径（最易错的一处）

**「今日」= `DISPLAY_TZ` 北京自然日，不是 UTC 日。**

- `DISPLAY_TZ = timezone(timedelta(hours=8))`，是写死的固定偏移而非机器本地时区：
  生产机跑 UTC、对端跑 CST，任何依赖本地时区的东西会让同一事件在两机渲染成不同时间。
- 一个北京日横跨两个 UTC 日期，因此 `collect_usage()` 必须把**两个** UTC 日期后缀的
  obs 文件都列为候选，再按每条事件自己的时间戳换算到 `DISPLAY_TZ` 过滤。
- `hourly_output_tokens` 是 24 个北京小时桶。
- `collect_harness()` 的 `day_created` / `day_completed` / `iso_week_*` 同样按北京日算；
  metric 的 `window` 字段必须标 `today_cst` / `iso_week_cst`，不得标 UTC。
- `iso_week_*` 是自然周（周一 00:00 清零），`week_created` 是滚动 7 天，
  两者并列且各标各的 window，不得混用一个标签。

<!-- anchor: const DISPLAY_TZ = 8 -->
<!-- anchor: symbol collect_usage -->
<!-- anchor: test Bigbird/Stats/exporter/tests/test_usage_sources.py :: test_beijing_day_spans_two_utc_files_and_conserves_every_counter -->
<!-- anchor: test Bigbird/Stats/exporter/tests/test_usage_sources.py :: test_hourly_buckets_use_beijing_hour -->
<!-- anchor: test Bigbird/Stats/exporter/tests/test_usage_sources.py :: test_today_follows_beijing_not_utc -->
<!-- anchor: test Bigbird/Stats/exporter/tests/test_usage_sources.py :: test_event_in_previous_utc_file_is_included_when_beijing_day_matches -->
<!-- anchor: test Bigbird/Stats/exporter/tests/test_usage_sources.py :: test_iso_week_starts_monday_and_is_not_the_rolling_window -->

### 6.2 token 口径

- 对外的 `input_tokens` = 裸 `inputTokens` + `cacheReadTokens` + `cacheCreationTokens`；
  裸值单独留在 `input_tokens_uncached` 供排查缓存命中率。
- 缓存两项是 2026-07-27 才补的埋点，历史事件无这两个字段 → 缺失记 0，
  老数据仍只反映裸 input（会偏低）。这是已知历史断层，不是口径错误。
- `duration_p50_ms` 取 `durationMs` 的**中位数**不是平均（长尾会拉偏）；无样本时为 `None`。
- `by_agent` 取前 `USAGE_TOP_AGENTS = 10`，`by_session` 取前 `USAGE_TOP_SESSIONS = 20`，
  均按 output tokens 降序。
- session 标识只输出前 `SESSION_ID_PREFIX_CHARS = 8` 个字符，完整 session id 不得进快照。
- `req.inbound` 只统计 `msgType`；`who`（飞书 open_id）**永不进快照**。
- 目录不可读返回 `None`；读到但空返回全零计数——**「读不到」与「今天没活动」必须可区分**。
- 单行解析失败计入 `malformed_lines` 并使该批 metric 的 `quality` 降为 `descriptive`；
  坏行不参与任何日期归属。

<!-- anchor: const USAGE_TOP_AGENTS = 10 -->
<!-- anchor: const USAGE_TOP_SESSIONS = 20 -->
<!-- anchor: const SESSION_ID_PREFIX_CHARS = 8 -->
<!-- anchor: test Bigbird/Stats/exporter/tests/test_usage_sources.py :: test_input_tokens_counts_cache_not_just_the_bare_field -->
<!-- anchor: test Bigbird/Stats/exporter/tests/test_usage_sources.py :: test_events_without_cache_fields_still_aggregate -->
<!-- anchor: test Bigbird/Stats/exporter/tests/test_usage_sources.py :: test_input_and_output_tokens_stay_separate -->

### 6.3 月度分摊（`collect_month_to_date`）

- `MONTHLY_SUBSCRIPTION_USD = 270.0` 是当前实现里的**配置估算常量**，按输出 token 占比
  分摊到 agent；它不证明实际订阅现金支出。
- 分摊基准取输出而非输入：输入侧 99%+ 是缓存复读，按它分摊等于按上下文长度分摊。
- 这是**估算不是账单归因**，metric 的 `scope` 必须写明
  `"subscription amortized by share of output tokens"` 以与 `billing.mtd.*` 区分。
- 总输出为 0 时 share 记 0.0 不得除零；「本月还没人干活」的正确呈现是零不是 unknown。
- `days_covered` 必须如实报覆盖天数——obs 文件可能缺失，报「本月累计」却只覆盖
  15 天而不说，读者会当成整月数。

<!-- anchor: const MONTHLY_SUBSCRIPTION_USD = 270.0 -->
<!-- anchor: symbol collect_month_to_date -->

### 6.4 CC 额度

- 唯一数据源是一次最小 API 调用的 `anthropic-ratelimit-unified-*` 响应头；
  CC 不在盘上发布额度文件。请求用 `CC_QUOTA_MODEL = "claude-haiku-4-5-20251001"`、
  `CC_QUOTA_MAX_TOKENS = 1`，**响应体从不读取**——付的钱只买响应头。每轮恰发一次。
- 凭证读取顺序：先 `CC_RELAY_ENV_PATH`（`/home/pulse/.relay/relay.env` 的
  `CLAUDE_CODE_OAUTH_TOKEN`，生产真正在用、长期有效），取不到才退
  `CC_CREDENTIALS_PATH`（`.credentials.json`，CC TUI 交互登录副产物、8 小时过期）。
  **读错源是「CC 未知」发作三次的真根因**：探针拿一份没人用的过期凭证去问，而生产一直好着。
- token 只许出现在请求头里，`_rate_limit_headers()` 必须只复制前缀为
  `CC_QUOTA_HEADER_PREFIX = "anthropic-ratelimit-unified"` 的头，其余一律丢弃，
  使凭证在物理上无法抵达调用方。
- `_utilization_percent()` 把 0–1 的分数换算成 0–100 百分比并保留 2 位小数，
  与 Codex 侧（本就是 0–100）同尺度；超出 0–1 或 NaN 一律判不可用而**不得钳位**。
- HTTP 429 仍必须解析其额度头——那正是最该显示的状态；401/403 无头，降 `None`。
- 7 天利用率缺失或不可解析时整体返回 `None`（降 unknown），不得伪造 0。
- `CC_QUOTA_TIMEOUT_SECONDS = 5`，与本地采集并发因此延迟重叠不叠加。
- **过期空窗回退**：拿不到时读 `read_cc_quota_cache()` 的上次读数，并如实写入其
  原始 `observed_at`，由 `CC_QUOTA_MAX_AGE_SECONDS = 32400`（9 小时，略长于令牌
  8 小时有效期）判老。不得假装它是现值。
- `cc_token_expiry()` 在 relay.env 有 token 时必须返回 `None`（＝「说不上」）：
  生产用的那份没有过期戳，拿隔壁闲置文件的时间去解释生产状态正是误诊帮凶。
  调用方不得把 `None` 当作「没问题」。

<!-- anchor: symbol read_relay_env_token -->
<!-- anchor: symbol read_cc_oauth_token -->
<!-- anchor: symbol parse_cc_quota_headers -->
<!-- anchor: symbol fetch_cc_quota_headers -->
<!-- anchor: symbol _rate_limit_headers -->
<!-- anchor: symbol _utilization_percent -->
<!-- anchor: symbol read_cc_quota_cache -->
<!-- anchor: symbol write_cc_quota_cache -->
<!-- anchor: symbol cc_token_expiry -->
<!-- anchor: const CC_QUOTA_MAX_TOKENS = 1 -->
<!-- anchor: const CC_QUOTA_TIMEOUT_SECONDS = 5 -->
<!-- anchor: const CC_QUOTA_MAX_AGE_SECONDS = 9 * 3600 -->
<!-- anchor: const CC_QUOTA_HEADER_PREFIX = "anthropic-ratelimit-unified" -->
<!-- anchor: test Bigbird/Stats/exporter/tests/test_live.py :: test_available_quota_is_reported_as_percent_not_fraction -->
<!-- anchor: test Bigbird/Stats/exporter/tests/test_usage_sources.py :: test_fetch_without_a_token_makes_no_request -->
<!-- anchor: test Bigbird/Stats/exporter/tests/test_usage_sources.py :: test_expiry_reports_unknown_when_production_token_is_in_use -->
<!-- anchor: test Bigbird/Stats/exporter/tests/test_live.py :: test_an_expired_token_falls_back_to_the_last_good_reading -->

### 6.5 Codex 额度

- Codex 只在**干活的副作用**里发布额度：每个 turn 往 session 日志写一条
  `token_count`。没人用 Codex 时最新记录只会变老（2026-07-28 实测一条 07-20 的
  读数活到 07-28 仍被当作现值发布）。
- 因此探针**自己造天气**：`run_codex_probe()` 跑一次最小无头 turn 纯粹为了写一条新记录。
  该动作花真实订阅额度，故必须限流：`CODEX_PROBE_MIN_INTERVAL_SECONDS = 21600`（6 小时）。
  `codex_probe_is_due()` 只在无读数、读数超过该间隔、或时间戳在未来（时钟偏斜）时返回 True。
  探针命令从 `data/runtime-cost-route.json` 的 `stats_quota_probe.effective_route.profile`
  读取 alias；经 schema-v2 compute catalog 与当前 Codex cache 解析真实 model/effort。
  phase 2 canary 指向 `economy`（Luna/low）。active route/catalog/cache/effort 任一不可验证时
  `CODEX_PROBE_ARGV=None`、runner 不调用 subprocess，绝不退 Sol/xhigh。探针显式传
  `--sandbox read-only` 与全局选项 `--ask-for-approval never` 覆盖用户全局 YOLO；后者必须放在
  `exec` 子命令之前，当前 CLI 的 `exec` parser 不接收该选项。不因平台 agent 权限迁移扩大
  固定 quota probe 的命令面。active manifest 必须是三档 preset/status 对应、根字段
  与五条 route 精确齐全的 schema；Stats route 的 verifier、base/floor/effective/rollback 还须与
  所选 preset 一致，其他 route 即使仅缺一条或多一字段也不得形成 probe argv。
- 2026-08-16 生产 phase 2 真探针解析并执行为 Luna/low，session 发布
  10080 分钟窗口、48% 与 reset；同次 canonical exporter 将 `relay.codex.used_percent`
  与 `relay.codex.resets_at` 发布为 `available/validated`。这是单次生产路径验收，
  不是 7 日节省或 Swift UI 人工验收。
- **默认必须 no-op**：`LiveCollector` 的 `codex_probe` 缺省是 `lambda: False`，
  只有 `LiveSnapshotService.default()` 这条生产装配显式传入 `usage_sources.run_codex_probe`。
  忘记打桩的测试因此不会给大鸟账单计费。
- 探测顺序不可调换：先读盘 → 判是否够新 → 才花 turn → **探测后必须重读**
  （新记录落在全新 session 文件里，探测前那次扫描不可能看见）。
- 探测失败时保留旧读数，不得退化成 `None`。
- 两台机器的记录都要看（额度是账号级共享），`merge_codex()` 取 `observed_at` 最新者；
  胜者缺 `resets_at` 时可从另一台的读数补——同一个周窗口，重置时刻通用。
- **额度耗尽是数据在别处不是数据缺失**：耗尽后服务端拒绝执行 turn，`token_count`
  的 `primary`/`secondary` 全为 null，看起来像「未知」；真相在同 session 的
  `task_complete` 记录里。`_extract_exhaustion()` 认 `codex_error_info ==
  CODEX_USAGE_LIMIT_ERROR("usage_limit_exceeded")` 这个稳定枚举（不认英文散文），
  报 `used_percent = 100.0`。
- `CODEX_QUOTA_MAX_AGE_SECONDS = 43200`（12 小时，＝探测间隔的两倍）：一次探测失败
  可存活（继续显示上个好读数），连续两次失败则数值置空。
- 超龄读数必须**扣住不发**（`_metric()` 把 `value` 置 `None`）而非附上免责声明一起发：
  每个消费者都得记着先看 `status` 才行，而菜单栏实测没有做到；缺席是唯一不会被误读的信号。
- 远端探针 `CODEX_REMOTE_TIMEOUT_SECONDS = 7`，扫最近 `CODEX_LOOKBACK_DAYS = 3`
  天内的 `CODEX_SCAN_FILES = 5` 个文件；失败一律降 unknown，不得伪造 0。

<!-- anchor: symbol collect_codex -->
<!-- anchor: symbol codex_probe_is_due -->
<!-- anchor: symbol run_codex_probe -->
<!-- anchor: symbol _resolve_codex_probe_route -->
<!-- anchor: test Bigbird/Stats/exporter/tests/test_live_contract_cost_route.py :: test_stats_route_resolves_luna_low_from_visible_fresh_catalog -->
<!-- anchor: test Bigbird/Stats/exporter/tests/test_live_contract_cost_route.py :: test_missing_route_materializes_no_probe_argv_and_starts_no_process -->
<!-- anchor: symbol merge_codex -->
<!-- anchor: symbol _extract_exhaustion -->
<!-- anchor: symbol _merge_exhaustion_resets -->
<!-- anchor: symbol _metric -->
<!-- anchor: const CODEX_PROBE_MIN_INTERVAL_SECONDS = 6 * 60 * 60 -->
<!-- anchor: const CODEX_PROBE_TIMEOUT_SECONDS = 45 -->
<!-- anchor: symbol CODEX_PROBE_MODEL -->
<!-- anchor: symbol CODEX_PROBE_REASONING_EFFORT -->
<!-- anchor: test Bigbird/Stats/exporter/tests/test_live_contract_cost_route.py :: test_valid_economy_route_materializes_luna_low_argv_in_clean_process -->
<!-- anchor: const CODEX_REMOTE_TIMEOUT_SECONDS = 7 -->
<!-- anchor: const CODEX_LOOKBACK_DAYS = 3 -->
<!-- anchor: const CODEX_SCAN_FILES = 5 -->
<!-- anchor: const CODEX_USAGE_LIMIT_ERROR = "usage_limit_exceeded" -->

### 6.5.1 GROK 周额度（`grok_quota`）

- 采集与发布沿用 Codex / CC 额度的同一路径：成本发布任务
  `io.coevolution.pulse.stats.cost-refresh` 每个周期调用一次 `refresh_grok_quota()`，
  校验后私有写入 `~/.local/state/pulse/stats/grok-quota-v1.json`（`pulse.stats.grok_quota/v1`），
  有 destination（远端接收方）时以 `receive-grok-quota` 转发，拒绝 `observed_at` 更旧的发布物；
  周期结果记入 `last-refresh.json` 的 `grok_quota`。不新增常驻轮询服务。
  `refresh.json` 无 `grok_quota` 项时不读取，状态为 `source_not_configured`；
  原生发布写入器 `prepare_stats()` 为账户补上默认项（已有显式项则保留）。
- 数据源是 Cursor 内部接口 `GetSandUsageStatus`（POST `ENDPOINT`，请求体 `{}`，
  头 `Authorization: Bearer <token>`、`Content-Type`/`Accept: application/json`、
  `Connect-Protocol-Version: 1`）。它没有稳定契约，因此严格校验：
  响应须是 JSON 对象且只含 `KNOWN_FIELDS`，布尔字段须是布尔，`billingBrand` 须为
  `SAND_BILLING_BRAND_GROK`，`usagePercent` 须是 0–100 的有限数，周期起点与下次重置须带时区
  （或为纪元秒/毫秒），周期长度不超过 `MAX_PERIOD_SECONDS`（8 天），读取时刻在周期内。
  任一不符为不可用并给出稳定原因码，**不模拟、不补 0**；`usagePercent` 缺失也不当作 0%。
- 凭证：`read_access_token()` 以 SQLite 只读 URI（`file:…?mode=ro`）读取 Cursor
  `state.vscdb` 的 `ItemTable` 中 `cursorAuth/accessToken`。token 只存在于内存和请求头，
  不写日志、不刷新、不回写；请求不跟随重定向，以免把 token 带到别处。
  401/403 记为 `grok_login_invalid`，续期留给原登录方。
- 账号核对：`read_active_account()` 读 Grok Bot `sand-secrets.json` 顶层 `cursor-accounts`
  （其值是 JSON 字符串，需再解析）的 `active`。`fingerprint()` 取 JWT `sub` 的 SHA-256 十六进制
  （不验签；无 `sub` 时取 token 本身的 SHA-256）。请求前不一致为 `account_mismatch`，
  不发请求；请求后重读 token 与 `active`，任一变化为 `account_changed_during_read`，拒绝发布。
- 发布物只含脱敏事实：`status`、`observed_at`、`account_ref`（指纹再加盐哈希，不等于 Grok Bot
  存的指纹）、`plan_label`、`weekly.used_percent / period_start / resets_at`、`reason`。
  `validate()` 拒绝任何多余字段，token、邮箱、账号 id、原始认证库都不可能进入发布物。
- 三种发布状态：
  - `available`：当前周期读数；与 Codex / CC 同一新鲜度规则，在 `resets_at` 之前、
    且读取时刻不早于 `period_start` 时有效，保留原始读取时间。
  - `not_applicable`：`usesPooledEnterpriseAllowance=true` 记 `pooled_enterprise_allowance`；
    `hasNonZeroIncludedLimit` 非 `true`（proto3 JSON 省略 false）记 `no_personal_allowance`。
    不编造个人周额度。
  - `unavailable`：确定性失败（`DEFINITIVE`：登录失效、账号不符或中途切换、读不到凭证或账号、
    接口形态变化）以带原因的事实**替换**旧读数，因为旧读数可能属于别的账号或已无法确认。
  瞬时失败（`TRANSIENT`：网络、429、5xx、响应过大）不写文件，旧读数保留，到周期结束自然过期。
- 这是账号级周额度池，不是单个机器人的成本或 token，**不得计入任何成本合计**。
- exporter 读发布物产出 `relay.grok.used_percent`、`relay.grok.resets_at`、
  `relay.grok.period_start`（unknown 时同带原因码，失败事实保留其读取时间）与
  `relay.grok.plan_label`（`unit=text`）。未配置文件路径时不产出任何 GROK 指标。
- 手动单次读取：`pulse-stats-cost-refresh.py grok-quota --config <含 grok_quota 的配置>`
  写一次事实文件并只打印状态词（`published` / `not_applicable` / `unavailable` /
  `read_or_publish_failed`）；失败原因码与未知字段名（只有名字，不含值）在 stderr 的进度行里。

<!-- anchor: symbol read_account_quota -->
<!-- anchor: symbol read_access_token -->
<!-- anchor: symbol read_active_account -->
<!-- anchor: symbol fingerprint -->
<!-- anchor: symbol projection -->
<!-- anchor: symbol failure_document -->
<!-- anchor: symbol refresh_grok_quota -->
<!-- anchor: symbol _append_grok_quota_metrics -->
<!-- anchor: const MAX_PERIOD_SECONDS = 8 * 24 * 3600 -->
<!-- anchor: test Bigbird/Stats/exporter/tests/test_grok_quota.py :: test_account_switch_during_request_refuses_publication -->
<!-- anchor: test Bigbird/Stats/exporter/tests/test_grok_quota.py :: test_unknown_or_changed_schema_is_unavailable_not_simulated -->
<!-- anchor: test Bigbird/Stats/exporter/tests/test_grok_quota.py :: test_pooled_or_missing_personal_allowance_is_not_applicable -->
<!-- anchor: test Bigbird/Stats/exporter/tests/test_grok_quota.py :: test_definitive_failure_replaces_the_reading_with_its_reason -->
<!-- anchor: test Bigbird/Stats/exporter/tests/test_grok_quota.py :: test_transient_failure_keeps_the_previous_reading -->

### 6.6 CC 429 判定（`detect_cc_rate_limited`）

- 文件 mtime 只作**该开哪些文件**的廉价预筛，时间窗必须再按每条记录自己的
  `timestamp` 重判：今天被追加过的 transcript 里routinely 还存着数周前的错误记录
  （2026-07-25 实测盘上仅有的两条 429 是 06-11 与 06-18，只按 mtime 判会误报红灯）。
- 真 429 判据两条同时成立：`apiErrorStatus == 429` 且序列化后含 `"error":"rate_limit`。
- 窗口 `CC_RATE_LIMIT_WINDOW_HOURS = 24`，最多打开 `CC_RATE_LIMIT_MAX_FILES = 40`
  个文件（按 mtime 降序）。
- 目录不存在返回 `None`（unknown）；无候选文件返回 `False`（确实没有）。

<!-- anchor: symbol detect_cc_rate_limited -->
<!-- anchor: symbol _record_is_rate_limit_429 -->
<!-- anchor: const CC_RATE_LIMIT_WINDOW_HOURS = 24 -->
<!-- anchor: const CC_RATE_LIMIT_MAX_FILES = 40 -->
<!-- anchor: test Bigbird/Stats/exporter/tests/test_usage_sources.py :: test_recent_429_is_detected -->
<!-- anchor: test Bigbird/Stats/exporter/tests/test_usage_sources.py :: test_old_429_in_recently_touched_file_is_not_counted -->

### 6.7 harness 计数（`collect_harness` / `collect_in_flight`）

- wi 的标识字段是 `work_item_id` **不是 `id`**。
- 「完成」判据用 `updated_at` 而非 `completed_at`：全库 463 条都有前者，后者只有 6 条。
  收口与新建必须各算各的——`day_completed` 数的是「今天关的」，含昨天建今天关的那件
  （2026-07-28 实测面板报 5、真值 6，漏的正是这种）。
- 停滞判据：`status ∈ WI_OPEN_STATUSES` 且 `now - (updated_at or created_at) >
  WI_STALLED_DAYS(7)` 天；列表截断至 `WI_STALLED_MAX_ITEMS = 20`，按天数降序。
- 坏 YAML 必须跳过并计数（`unreadable_work_items` / `unreadable_handoffs`），
  不得整体失败；有坏记录时该批 metric 的 `quality` 降为 `descriptive`。
- `collect_in_flight()` 的 `blocked_by` 取「下一行动人」而非结果责任 owner，取序：
  `status == "in_progress"` 且有 `active_executor` → 它；否则取最新
  pending/accepted handoff 的全部 receiver；都没有才回退 `owner`。
  **注意**：`blocked_by` 只回答「该找谁」，**不得**被读成「此人正在做」——
  后者的判据见 §6.7.1，两者不同源。
- `_handoff_receivers()` 必须同时认 `to_owners`（fan-out）与 `to_owner`（首个收方的
  遗留投影）；只读 `to_owner` 会静默丢掉其余行动人。字段冲突、重复或非字符串时
  必须整条 handoff 弃用而非猜测。
- **2026-08-16 修正后的硬约束**：`collect_in_flight()` 必须输出
  `WI_IN_FLIGHT_STATUSES` 的全集，每个非终态 WI 恰好一行；不得因它不需人工就从
  快照删除。人工判据仍是两个信号的并集：① 非跨租户 WI 自身 `main_only: true`，
  或名下存在非跨租户 `main_only` handoff；② WI `status == "blocked"`。App 以该
  并集展开人工层，其余行进入默认折叠的自动层。
- `needs_owner` 只投影信号①，`disposition == "blocked_needs_read"` 只投影信号②；
  两者可同时成立。故 blocked 非 main_only 行必须是 `needs_owner == false` 加
  `blocked_needs_read`；blocked main_only 行必须是 `needs_owner == true` 加
  `blocked_needs_read`；其余人工行必须是 `needs_owner == true` 加
  `manual_required`。
- 非人工行继续按 §6.7.1 投影 `waiting_peer` / `sweep_watching` /
  `sweep_overdue` / `unowned`，并保留 `quiet_days`、`circuit_broken`。人工信号优先：
  blocked 恒为 `blocked_needs_read`；其余 main_only 恒为 `manual_required`，不能被
  pending handoff 或推进态覆盖。
- 日巡收尾从真实快照验证合法形态：人工两类，以及 `needs_owner == false` 的
  `waiting_peer` / `sweep_watching` 可通过；`unowned`、`sweep_overdue`、熔断、
  字段矛盾与未知取值都是 Pulse 自有巡检债。
- 不得从 `blocked_reason` 关键词猜理由真假。`status == blocked` 本身就是机械准入
  判据；理由原文只供人读取，不能由展示层制造第二套真假判据。
- 目录不可读一律返回 `None`（未知 ≠ 没有）。

<!-- anchor: test Bigbird/Stats/exporter/tests/test_usage_sources.py :: test_blocked_without_main_only_is_kept_for_human_read -->
<!-- anchor: test Bigbird/Stats/exporter/tests/test_usage_sources.py :: test_every_nonterminal_status_is_exported -->
<!-- anchor: test Bigbird/Stats/exporter/tests/test_usage_sources.py :: test_manual_and_automatic_rows_share_one_lossless_export -->
<!-- anchor: test Bigbird/Stats/exporter/tests/test_usage_sources.py :: test_completed_items_are_excluded_from_in_flight_export -->

### 6.7.1 自动层推进态分档（`in_flight_disposition`）

> 这些分档不等于必须人工参与，但必须随全部非终态 WI 留在快照中。当前 App 不把
> 它们恢复为五个顶层分组，而是统一收入默认折叠的「自动队列」；日巡闸据此区分
> 正常推进 (`waiting_peer` / `sweep_watching`) 与尚未处理的债
> (`sweep_overdue` / `unowned` / circuit broken)。

**起因（2026-08-07 大鸟问）**：「在飞项还是很多，这些 wi 分身都无法处理？」
逐条查证后发现**不是处理不了，是大部分没有任何机制在推**，而面板把「在等人」
与「没人管」混在一段里，读不出区别。大鸟定：「需要分清不同的在飞项，标记
『在等人』和『没人管』」。

#### 判据只用机制产生的信号（本节第一原则）

被判方能自己改写的字段，**一律不得**单独作为「有人在推」的判据。

- **`active_executor` 不得单独作为「在做」的证据**。它是 ack 时写入、干完不清的
  遗留字段，记录的是「谁曾经接过」不是「谁在做」。2026-08-07 实测：6 件
  `active_executor` 非空的 wi 里 5 件的持有者已数日未开工
  （ken 最近真开工 08-01、steward 08-04、hestia 08-05，而判定时点为 08-07）。
  此为 auto-memory `gate-criteria-must-not-key-on-field-the-gated-party-controls`
  的同族第 N 次复发，spec 层显式封死。
- 可用的机制信号只有两类：`autonomous_execution_event`（该 agent 真开工）与
  wi 关联的**非 `skipped_*`** dispatch 事件（该 wi 真被处置过）。

#### `skipped_*` 事件不得计入「有动静」

dispatcher 每小时扫描一次，扫到不处置也记一条 `dispatch_event`。2026-08-07 实测
`wi_20260725_586` 连续 6 小时每小时一条 `skipped_autonomous_covered`，而该 wi
实际六天无人推进。**把 dispatch 事件不加过滤地当心跳，会让每一件被反复跳过的
wi 看起来最活跃。** 计算「静默天数」时必须排除全部 `skipped_*` outcome。

#### 「无事件」与「有事件但全是 skip」必须可区分

前者是刚建单（正常），后者是被反复跳过（危险）。合并成同一个「静默 N 天」
会把两者读成一样。故 `quiet_days` 为 `None`（从无真事件）与为整数（有过真事件、
距今 N 天）是两种语义，渲染侧必须分开处置。

#### 分档（按序短路，先命中先归档）

| 档 | 判据 | 2026-08-07 实测 |
|---|---|---|
| `waiting_peer`（在等人） | 有 `ack_state == pending` 的 handoff；或 `active_executor` 持有者在 `IN_FLIGHT_ACTIVE_DAYS` 内有真开工**且**该 wi 有非 skip 事件 | 3 |
| `blocked_needs_read`（阻塞待人看） | `status == "blocked"` | 3 |
| `sweep_overdue`（等兜底·已超期） | owner 的 policy 声明了 `stale_work_items` 且龄 ≥ `IN_FLIGHT_SWEEP_DAYS` | 2 |
| `sweep_watching`（新建观察中） | owner 有 `stale_work_items` 但龄 < 阈值 | 7 |
| `unowned`（真没人管） | owner 的 policy **未**声明 `stale_work_items` | 2 |

`unowned` 的含义是**没有任何机制会主动碰它**：dispatcher 是 handoff 驱动的
（`scan.py::list_stale_work_items_for` docstring 原文："it only ever wakes on a
PENDING handoff"），handoff 全终态后唯一的兜底就是 `stale_work_items` 扫描，
而该扫描按 agent opt-in（`scan.py::_resolve_stale_wi_age_days`，无声明 → `None`
→ 采集器整个不跑）。2026-08-07 实测未声明的有 amanda / duke / hume / reeve /
cz-pulse 五家。

#### 熔断必须单独标，不得混入 `sweep_*`

被兜底扫描捡起、但已达 `CIRCUIT_BREAKER_THRESHOLD` 连续失败的 wi，机制**已经
放弃它了**。归进「等兜底」会让读者以为还有机制在管。带 `circuit_broken: true`
独立标记。2026-08-07 实测 `wi_20260802_282` 从 12:40Z 空转至 23:40Z。

#### `blocked_reason` 不做语义判断

真假只有人能判。exporter **不得**用正则或关键词猜测理由是否成立——那只会制造
第二种假绿。原文透出（截断保留可读长度），判真假是读面板的人的活。

#### 阈值不复用 `WI_STALLED_DAYS`

`WI_STALLED_DAYS(7)` 是「停滞列表」的判据，服务另一个功能，改它会波及
`collect_harness` 的 stalled 段。本节新增独立常量 `IN_FLIGHT_SWEEP_DAYS = 3`，
对齐 harness 侧 `STALE_WI_DEFAULT_AGE_DAYS`——**判据必须与机制真正动手的那个
时点一致**，用 7 天去描述一个 3 天触发的机制会让「已超期」这个词失真。
`IN_FLIGHT_ACTIVE_DAYS = 1`（昨今），用于判 executor 是否真在场。

#### 新增数据源：policy 目录

判 `unowned` 需读 `.harness/policies/*.yaml`。这是本节唯一扩大 exporter 读取面的
地方——`LivePaths` 增 `policies` 字段，§3 源表同步加行。目录不可读时该档降级为
`sweep_watching`（**不得**误报成 `unowned`：读不到 ≠ 没开）。

<!-- anchor: symbol _agents_with_stale_sweep -->
<!-- anchor: symbol _real_activity_from_events -->
<!-- anchor: symbol _pending_handoffs_by_wi -->
<!-- anchor: const IN_FLIGHT_SWEEP_DAYS = 3 -->
<!-- anchor: const IN_FLIGHT_ACTIVE_DAYS = 1 -->
<!-- anchor: test Bigbird/Stats/exporter/tests/test_usage_sources.py :: test_active_executor_alone_is_not_waiting_peer -->
<!-- anchor: test Bigbird/Stats/exporter/tests/test_usage_sources.py :: test_skipped_dispatch_events_do_not_count_as_activity -->
<!-- anchor: test Bigbird/Stats/exporter/tests/test_usage_sources.py :: test_never_touched_is_distinct_from_all_skipped -->
<!-- anchor: test Bigbird/Stats/exporter/tests/test_usage_sources.py :: test_owner_without_stale_sweep_is_unowned -->
<!-- anchor: test Bigbird/Stats/exporter/tests/test_usage_sources.py :: test_unreadable_policy_dir_degrades_to_watching_not_unowned -->
<!-- anchor: test Bigbird/Stats/exporter/tests/test_usage_sources.py :: test_circuit_broken_is_marked_separately -->
<!-- anchor: manual 分档抽样复核 :: 拿当日真实快照，对 waiting_peer 每一条去 .harness/events/ 查该 executor 最近一次 autonomous_execution_event；若有任何一条的持有者超过 IN_FLIGHT_ACTIVE_DAYS 未开工，判据即已失效 -->

<!-- anchor: symbol collect_harness -->
<!-- anchor: symbol collect_in_flight -->
<!-- anchor: symbol _handoff_receivers -->
<!-- anchor: symbol _current_handoff_receivers -->
<!-- anchor: const WI_STALLED_DAYS = 7 -->
<!-- anchor: const WI_STALLED_MAX_ITEMS = 20 -->
<!-- anchor: test Bigbird/Stats/exporter/tests/test_usage_sources.py :: test_counts_stalled_and_pending -->
<!-- anchor: test Bigbird/Stats/exporter/tests/test_usage_sources.py :: test_completed_today_counts_regardless_of_when_it_was_created -->
<!-- anchor: test Bigbird/Stats/exporter/tests/test_usage_sources.py :: test_completed_on_another_day_is_not_todays_number -->
<!-- anchor: test Bigbird/Stats/exporter/tests/test_usage_sources.py :: test_stalled_falls_back_to_created_at_when_updated_at_missing -->
<!-- anchor: test Bigbird/Stats/exporter/tests/test_usage_sources.py :: test_completed_items_are_never_stalled -->

---

## 7. 能力与指标的构造规则（`live_contract.py`）

### 7.1 `_capability()`

- `implementation_status` 传入 `"active_validated"` 但 `health` 或 `freshness`
  为 `"unknown"` 时，必须自动降级为 `"active_partial"`——一条能力不能在任一
  结构化观测轴显式未知的同时自称已验证。
- 已测到的**失败**（warning / critical / stale / error）仍保持 validated：
  它证明集成在观测一个坏结果，而不是证明集成缺席。

<!-- anchor: symbol _capability -->

### 7.2 `_metric()`

- `value is None` → `status="unknown"`，绝不强转 0。
- 只有调用方显式传 `max_age_seconds` 且同时传了 `observed_at` 与 `now` 时才判超龄；
  多数 metric 以采集时刻打戳、构造上不会变旧，对每个调用点强制要求策略只会制造噪音。
- 超龄时 `status="unknown"` **且 `value` 置 `None`**，`reason` 写人话年龄
  （`_format_age()`：≥1 天记「N 天」、≥1 小时记「N 小时」、≥1 分钟记「N 分钟」，否则「N 秒」）。

<!-- anchor: symbol _format_age -->

### 7.3 时间戳归一（`_parse_date`）

- 所有源时间戳必须先归一到 UTC 再比较或存储。escalation 流实测混着 `Z`（916 条）
  与 `+08:00`（552 条）两种偏移，因为各 emitter 用自己机器的时钟。
- naive（无时区）时间戳必须抛 `ValueError`，不得默认补 UTC。
  （注意 `usage_sources._parse_ts()` 是另一套：它对 naive 值补 UTC 并对无法解析者
  返回 `None`，用于容错度更高的用量流。两者行为不同是有意的。）

<!-- anchor: symbol _parse_date -->
<!-- anchor: symbol _parse_ts -->

### 7.4 白名单与模板

- 只有 `ALERT_TEMPLATES` 里逐条登记过的 `match_label` 才能从 append-only 事件流
  跨进用户可见快照；未登记的 label 留在源账本但不进 App 缓存。
- 该表 2026-08-10 现算登记 **36** 个 label（口径 = `live_contract.py` 里
  `ALERT_TEMPLATES` 字典键数，复算命令见 `stats-snapshot-contract.md` §9）。
  **新建通道必须端到端验**：2026-07-28 查出
  `daily_patrol_action_required` 此前不在表内，而 `_collect_alerts` 用
  `ALERT_TEMPLATES[match_label]` 取模板、`KeyError` 直接 continue ——
  日巡推给大鸟的每一条都被 exporter 静默丢弃，菜单栏一个字都看不到。
  推送侧通了、展示侧没登记，是同一个「机制只做了一半」。

<!-- anchor: const ALERT_TEMPLATES -->

---

## 8. 边界与失败模式总表

| 情形 | 行为 | 不得 |
|---|---|---|
| 单源读取/解析失败 | 该能力 `freshness="error"`、`health="unknown"`，provenance 记脱敏 error_code | 悄悄丢字段；用 0 顶替 |
| 四个 core 源全失败 | 抛 `LiveSnapshotError`，非零退出 | 输出伪完整 snapshot |
| snapshot 超 1 MiB | 抛 `LiveSnapshotError` | 截断后照发 |
| incident 投影源不全 | `runtime_health` 强制 `unknown`，headline 说明 | 用空表给绿 |
| state 文件损坏 | 判 `corrupt`，写盘前先 `backup_corrupt()` | 直接覆盖；把删 state 当 resolved |
| state 锁被占 | 抛 `StateLockBusy` | 阻塞等待 |
| 恢复证据源读不到 | 返回 `None`，退回原时效规则 | 返回 `{}` 把一切判成已恢复 |
| unit 非零退出 | 不贡献恢复证据 | 当作「跑过了＝好了」 |
| 额度读数超龄 | `status="unknown"` 且 `value=None` | 附免责声明照发数值 |
| 令牌空窗 | 回退缓存读数并如实写原 `observed_at` | 假装是现值；整格变未知 |
| obs / harness 目录不可读 | 返回 `None` → unknown | 返回 0 或空列表 |
| 单行 JSON/YAML 损坏 | 跳过并计数，`quality` 降 `descriptive` | 中断整轮采集 |
| 未登记的 `match_label` | 不进快照 | 用默认模板放行 |

### 隐私边界（采集侧硬约束）

- 快照中不得出现：完整 session id（只出前 8 字符）、飞书 `open_id`（`who` 字段
  只统计 `msgType`）、message 正文（只出 `ALERT_MESSAGE_CHARS = 220` 字符摘录）、
  任何 token（`_rate_limit_headers()` 只复制额度头）。
- lint key 的上游自述文本不得整串回显，只允许我们自己生成的 `wi_` / `ho_` id。

---

## 9. 与既有 spec 的冲突（代码为准，spec 已漂）

以下每条都是**既有 spec 写了 A、代码做的是 B**。列在这里是为了让下一个读 spec 的人
不被误导，不是主张代码错了——多数情况下代码是后来被大鸟纠正过的那版。

| # | 既有 spec 说 | 代码实际 | 判断 |
|---|---|---|---|
| 1 | v2 §3.2：「今日」= UTC 自然日，**不做时区转换**，面板注明 UTC | 全部按 `DISPLAY_TZ` 北京日算，window 标 `today_cst` | 代码对（2026-07-28 大鸟纠正，UTC 口径让北京 00:00–08:00 那八小时的「今日」指昨天） |
| 2 | v3 §2：`act` 需 `match_label ∈ TIER_ACT_LABELS` | 该常量 08-01 已删；标签无任何路径可达 `act` | 代码对（2026-07-28 大鸟第四轮定案），**v3 spec 未同步** |
| 3 | v3 §2：`act` 第二必要条件 `last_seen ≤ TIER_ACT_MAX_AGE_HOURS(48)` | 该常量 08-01 已删；`tier()` 里已无老化逻辑 | 死配置已清理，见 §10 |
| 4 | v3 §2「跨档升级」：`occurrence_count ≥ 20` 且 24h 内 → 升 `act` | 两常量 08-01 均已删；重复不再升档 | 死配置已清理，见 §10 |
| 5 | v3 §2：`act` = 「自己好不了且已影响真实的人或活」 | `act` 唯一来源是 `main_only` handoff（＝「发起时就声明必须人来判断」） | 语义已被大鸟重定，v3 的定义已过期 |
| 6 | v1 §3 P0-B：reducer state 只保存 incident projection 与 cursor | 属实，但另有 `CC_QUOTA_CACHE_PATH` 一份额度缓存也在写盘 | v1 的「唯一写入」表述已不完整 |
| 7 | v1 §3 P0-A：alert 上限沿用 `MAX_ALERTS = 20` | 该常量 08-01 已删；实际按组收敛且上限 `MAX_ALERT_GROUPS = 200` | 死配置已清理，见 §10 |

---

## 10. 设计未落地 / 已死配置

**死配置已清理**——下列常量曾在 `live_contract.py` 里定义而全仓零读取点，
已于 2026-08-01 **全部删除**（删前逐个 grep 复核，删后 224 tests 全过，
并用生产真实源前后对跑 `--json` 证实 `alert_tiers` 与 (tier, title) 多重集
逐条一致）：

| 常量 | 原值 | 为什么能删 |
|---|---|---|
| `TIER_ACT_LABELS` | `frozenset()` | 已是空集，`tier()` 不读它 |
| `TIER_ACT_MAX_AGE_HOURS` | 48 | 老化逻辑 2026-07-28 撤销 |
| `TIER_ESCALATE_COUNT` | 20 | 重复升档 2026-07-28 撤销 |
| `TIER_ESCALATE_WINDOW_HOURS` | 24 | 同上 |
| `MAX_ALERTS` | 20 | 被 `MAX_ALERT_GROUPS` 取代 |
| `DECISION_REQUEST_WINDOW_DAYS` | 400 | 从未接上 |

守它们「保持删除」的锚点见
`stats-snapshot-contract.md` §6.1 那条 `manual`——不在这里重复挂，
两处各挂一遍会让同一件事有两个可能不同步的判据。
（为什么不能用 `const` / `wire` 锚点守一个已删除的符号，同见该节。）

**分片范围与读取窗口不一致**（真实缺陷，非仅命名问题）：
`_collect_alerts()` 读 `DECISION_REQUEST_SHARD_MONTHS = 13` 个月的分片文件，
但随后对**每一行**施加 `ALERT_WINDOW_DAYS = 7` 天的 cutoff。
两者不一致意味着 12 个月的分片被读进内存后全部丢弃。
这在 act 真源迁到 `main_only` handoff 之后不再造成漏报（决策请求不再走这条路），
但 13 个月的读盘是纯开销。曾有一个 `DECISION_REQUEST_WINDOW_DAYS = 400` 常量
是为对齐这一点而设的，从未被接上，已随本次清理删除；**缺陷本身仍在**，
现以散文形式注在 `DECISION_REQUEST_SHARD_MONTHS` 上。

**设计未落地**——`_add_baseline_gaps()` 显式登记为未实现的能力，它们在每份 snapshot
里都以 `not_implemented` / `not_applicable` 出现，不得被任何弱信号冒充：

- `s1.stable_interaction_identity`（v1 §3 P1 的 `interaction_id` 全套）
- `s1.external_input_delivery_canary`（两头 canary）
- `s1.headless_artifact_reconciliation`（无头产物对账）
- `s2.cross_node_reconciliation`（跨节点对账）
- `slo.commitment_curves`
- `ops.incident_ledger`

另两条登记为 `active_partial` 但 freshness `unknown`（源未接入）：
`s1.request_trace_capture`（trace 源未适配）、`policy.commitment_registry`（policy 未适配）。
`s1.request_reply_reconciliation` 登记为 `retired`。

**v1 §3 P1 的 `InteractionContext` 字段族全部未落地**：`interaction_id`
`source_event_ref` `request_kind` `metric_eligibility` `exclusion_reason`
在本轮七个模块中**零命中**（grep 确认）。v1 spec 把它们写成了结构体定义，
容易被读成已存在——实际是设计稿。

<!-- anchor: symbol _add_baseline_gaps -->

**覆盖盲区登记（2026-08-10，只登债不补 spec）**：

- `pulse-stats-snapshot.py` CLI 入口的契约（参数面、退出码、`--json` 之外的
  行为）没有任何 spec 认领——本文只管它调用的库层。
- `tests/test_spec_capability_drift.py` 是一道「spec 与能力清单对不上就红」的
  机制闸，但该机制自身未在任何 spec 登记——读 spec 的人不知道有它在守。
- 部署面（`Bigbird/Stats/install.sh` 与 launchd 加载路径）连 manual 锚点都
  没有；「代码已推 ≠ 已部署」在这条链上无任何 spec 层防线（auto-memory
  `code-pushed-is-not-deployed-when-env-vars-missing`）。
- `verify-live-snapshot.py` 自身契约未立，登记在 `stats-snapshot-contract.md` §7。

---

## 11. 未决问题（我判断不了，不编）

1. **`DECISION_REQUEST_SHARD_MONTHS = 13` 是否该降回 1。**
   act 已不走 escalation 流，13 个月的读盘看不出还在服务什么。但我没有实测过
   watch 档里是否有依赖跨月分片的条目，不敢直接判定为无用。

2. **`_collect_alerts` 里 `source_cursors["escalation-events"]` 用的 `path` 是
   循环结束后残留的最后一个分片路径**（最老那个月），而 `len(lines)` 是全部分片
   合计行数。这个组合作为 cursor 语义可疑，但我不确定下游是否真在消费它。

3. **`reduce_coverage` 的 required 集在生产路径上等于全部 capabilities**
   （`live.py` 取 `[item["id"] for item in collected.capabilities]`）。
   于是「required 里有几个没出现」这个分支在生产上恒不触发，coverage 实际只由
   status 与 freshness 决定。这是有意简化还是接线残留，代码里看不出。

4. **`build_snapshot` 的 `baseline_version` 默认 `"0.2.1"` 与生产传入的
   `BASELINE_VERSION = "0.2.2"` 不一致。** 默认值是否还有别的调用方需要，我没查到。

5. **`collect_subject_closures` 对 handoff 用 `created_at` 当闭环时刻**，
   代码注释说这个不对称是安全的。我同意方向，但没有实测过「因此漏灭」的实际发生率。

6. **`OWNER_VISIBLE_ACK_OVERDUE_DAYS = 3.0` 与探针自身 15 分钟门限的关系**
   目前靠 exporter 侧过滤实现。若探针侧将来改门限，两处会各自漂移，
   代码里没有任何机制保证它们对齐。

---

## 12. 人工观察项（不可自动验）

<!-- anchor: manual codex-probe-billing :: 连续两天在 UTC 00:00 与 12:00 各跑一次 pulse-stats-snapshot.py --json，核对 Codex 账户周用量增量不超过 2 次最小 turn 的消耗；若增量明显更大说明 CODEX_PROBE_MIN_INTERVAL_SECONDS 的限流没生效 -->

<!-- anchor: manual state-file-permissions :: 生产跑一次 exporter 后对 /home/pulse/.relay/monitoring/pulse-stats/ 跑 ls -l，确认 incident-state-v1.json 为 0600、目录为 0700，且无遗留的 incident-state-v1.json 临时文件 -->

<!-- anchor: manual no-secret-in-snapshot :: 生产跑 pulse-stats-snapshot.py --json 并把输出管进 grep -iE 的 sk- / Bearer / oauth / open_id / token 五个模式，应无任何命中；这条只能靠真实输出验，fixture 里没有真凭证 -->

<!-- anchor: manual end-to-end-latency :: 生产用 time 跑 pulse-stats-snapshot.py --json，端到端应在 20 秒内返回（ack 探针 20 秒是最慢的串行源，两个远端探针与本地采集并发）；显著超出说明并发装配退化成串行 -->

<!-- anchor: manual act-card-readability :: 有 main_only handoff 在飞时打开菜单栏，确认每张「要你出手」卡片的标题是那句大意（wi title）而非「要你参与的协作」，且正文非空；这条只有真人看一眼能发现 -->
