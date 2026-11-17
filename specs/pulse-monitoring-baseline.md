# PULSE Monitoring Stable Baseline

> version: `0.2.3`
> status: `active_baseline`
> authority: PULSE 监控“当前能力、有效证据与 Stats snapshot 契约”的唯一汇总真源；目标契约仍以 REQ v3 / SPEC v0.3.1 为准，Stats 产品契约见 `Stats/specs/pulse-stats.md`
> owner: Pulse
> last_verified: `2026-07-22T16:25:00+08:00`
> verified_revisions: PULSE Stats code `77685cd`；Coevolution live `99f8988`；agent-harness related evidence lineage `e67df8d3`
> 版本史：只看 git log，不在文件头复制 changelog
> `[awm: object=PULSE 监控稳定基础, mode=redirect→execute]`

## 0. 基线结论

PULSE 已有一组**可继承、可读、正在生产运行的监控能力**。Stats Slice 1 已完成固定 live adapter、持久 incident state、固定 SSH transport、本地 cache 与一次 manual canary；下一门是 48 小时手动观察，不必等待原 Feature 全部完成，也不应把过去几轮 UAT 清零重做。

当前边界同时成立：

- 可以显示：Relay 与监控组件活性、S2 急性/慢性账本信号、结构化告警、日报出口、明确的缺口与退役项。
- 不可显示成可靠数字：逐请求 S1 兑现率、逐 agent P95/P99、两头 canary、无头产物总兑现率、跨节点闭环率。
- `unknown`、`retired`、`not_implemented` 是正式状态，不得折算为绿色，也不得因它们存在而阻塞 Stats 外壳和可信部分的开发。

本文件固定的是“可依赖到什么程度”，不是重新验收整个监控系统。

## 1. 工程契约

### 模式与风险

- 模式：`Change + Ship & Sync`，仅同步文档契约，不发布软件。
- Feature risk：`high-risk`。最终 Stats 会展示生产健康，错误绿色会掩盖静默失败。
- Slice risk：`normal`。本次只同步文档真源与运行状态，不改代码、生产配置、服务或数据。

### Minimum Viable Spec

| 字段 | 本次契约 |
|---|---|
| 目标 | 固定目标契约、当前实现、历史证据和能力边界，给 Stats 一个不会把未知冒充健康的稳定输入契约。 |
| 边界 | 只读核现场；不重跑 UAT，不以 Feature 未完成清零已通过证据，不改变任何运行行为。 |
| 上下文 | REQ v3、SPEC v0.3.2、Stats SPEC v0.1.2、07-15 至 07-19 验收/修复 memo、部署代码、Vultr systemd 与 live policy。 |
| 验收 | 每条当前状态有来源；证据继承与失效规则明确；能力逐项定级；Stats readiness 有明确 yes/no 和边界。 |
| 风险 | 最大风险是多真源漂移和能力越界声明；由真源优先级、显式未知态与窄能力声明控制。 |

### 明确不做

- 不重跑 UAT、不做故障注入、不手工执行 probe。
- 不改 production 配置、timer、service、Relay、harness、policy 或事件数据。
- 不更新 `MEMORY.md`、`CLAUDE.md`，不改监控目标范围。
- 不在本文件重复 Stats 的 UI、SSH、缓存与发布契约；这些由 `Stats/specs/pulse-stats.md` 管。
- 不以补齐整个监控 Feature 作为 Stats 开工前置条件。

## 2. 四层稳定基础

### 2.1 目标契约

目标契约回答“最终应做到什么”，不回答“今天什么在役”。

1. 主需求：`memo/2026-07/2026-07-14-REQ-monitoring-v3-interactive-supply-mainline.md`
   - S1 主轴是交互请求进入后得到合格回复；无头产物是次轴。
   - S2 同时覆盖单机流转与跨节点流转。
2. Feature 契约：`memo/2026-07/2026-07-14-SPEC-monitoring-system-mvs-and-slices.md` v0.3.1
   - 四类对账：交互 S1、无头产物、S2 单机、S2 跨节点。
   - 三类探针：外读、两头端到端、跨节点端到端。
   - Feature 验收包含七日真实表现、SLO 曲线、事故台账和探针自监控。

REQ/SPEC 未被废弃，但其中的规划态文字不得冒充 live 状态。

### 2.2 当前有效实现与拓扑

截至 `last_verified`，生产事实如下：

```text
Vultr Relay (active)
├─ request/turn/delivery 事件流（active，数据地基）
├─ degen-probe.timer（active，退化检测）
└─ systemd 监控面
   ├─ ack-probe.timer（enabled/active）
   ├─ harness-ledger-lint.timer（enabled/active）
   ├─ executor-reaper.timer（enabled/active）
   ├─ dual-heartbeat-watcher.timer（enabled/active）
   ├─ harness-daily-flush.timer（enabled/active）
   └─ reply-probe.timer（masked/inactive，已退休）

ownermac
└─ ownermac-probe → Vultr heartbeat registry（active peer source）
```

当前 live 证据：

- Vultr PULSE revision=`e792319`，Coevolution live revision=`ffebf503`；agent-harness 目录最近一次相关改动=`e67df8d3`。
- 五个在役 systemd timer 均为 `enabled/active`；`reply-probe.timer` 为 `masked/inactive`。
- 2026-07-22 最近自然轮次中，ack、ledger lint、executor reaper、heartbeat watcher、daily flush 均 `Result=success/ExecMainStatus=0`。
- heartbeat watcher 最近一轮把 ownermac、ack、lint、reaper、daily flush 五个 source 全判为 `fresh`。
- live policy=`0.2.0`，只有 5 条承诺；默认 reply 阈值仍为 10 分钟，条目无结构化 `probe` 字段。
- 2026-07-16 至 07-22 事件流持续存在；但三类事件均无 request/message id，receipt 也没有 final/delivery-kind 语义，不能据此生成可信的逐请求兑现率。
- Stats exporter 已在 Vultr 以 one-shot/manual 方式可用；2026-07-22 双次输出 projection 相同，未安装 timer/daemon。
- derived state=`/home/pulse/.relay/monitoring/pulse-stats/incident-state-v1.json`，schema v1、0600；App 本地 cache 0600/目录 0700。

### 2.3 历史 UAT 证据继承

UAT 的继承单位是**行为契约维度**，不是整个 Feature 的一个总 PASS/FAIL。

#### 继承规则

历史证据继续有效，须同时满足：

1. 证据写明验证的行为、入口、环境和结果；
2. 当前实现可追到该证据对应的代码/部署谱系；
3. 与该行为相关的契约、入口、部署路径或 schema 没有发生破坏性变化；
4. 此后没有生产反例推翻该项结论。

以下情形**只使相关维度失效**，不连坐其他 UAT：

- 该行为契约或错误语义改变；
- 原真实入口、调度入口或投递路径改变；
- 数据 schema/关联键改变到原断言不再成立；
- 组件被 disabled/masked/retired；
- 出现与原 PASS 直接冲突的生产反例；
- 原证据无法绑定到具体入口、版本或部署目标。

代码无关改动、文案改动、时间自然流逝、Feature 其他切片未完成，**不会自动使已通过 UAT 失效**。运行活性用 freshness 单独判断，不以周期性全量 UAT 代替。

#### 已继承的证据

| 行为维度 | 继承结论 | 证据 |
|---|---|---|
| 交互 trace 旁路落盘且不扰动回复 | 保留 | `memo/2026-07/2026-07-15-canary-findings-slice1-interactive-trace.md`：真实飞书入口证明 req/turn 落盘、主链路正常；后续 delivery 缺口已进入修复谱系。 |
| Gate 0 的结构化通知、探针失效告警、Relay 断路直退 | 保留 | `memo/2026-07/2026-07-17-monitoring-gate0-uat-fix.md`：真实/隔离入口逐门 PASS；后续无相关反例。 |
| ack 急性档判据与发送链 | 保留 | `memo/2026-07/2026-07-16-slice-P01-ack-probe-MVS.md`：live 对账与真链路；当前 timer 仍在役且自然轮次成功。 |
| ledger lint 增量判据 | 保留 | `memo/2026-07/2026-07-16-slice-L-harness-ledger-lint-MVS.md` 与 `memo/2026-07/2026-07-16-harness-ledger-lint-baseline-report.md`：live 首跑、baseline 复跑、fixture 增量；当前自然轮次仍成功。 |
| 明确派工完成通知与 daily flush | 保留 | `memo/2026-07/2026-07-18-harness-completion-notification-daily-exit-p0.md`：生产 UAT；`memo/2026-07/2026-07-19-harness-end-to-end-lifecycle-redesign.md`：真实 WI 终态、唯一通知与真实整点门。 |
| reply-probe 的 heuristic 配对 PASS | 不作为在役能力继承 | `memo/2026-07/2026-07-17-monitoring-root-fix-p0.md` 已将其正式退休并 mask；历史结果只保留为审计证据。 |

### 2.4 当前能力状态

状态只表达“能力成熟度”，不等于该能力此刻的健康值：

- `active_validated`：在役，相关 UAT 可继承，且当前没有反例。
- `active_partial`：在役或有数据，但覆盖、语义或验证边界不完整。
- `retired`：主动退役，禁止当作健康能力。
- `not_implemented`：目标契约存在，但没有生产实现。

| capability_id | 状态 | 当前可用结论 | 证据 |
|---|---|---|---|
| `s1.request_trace_capture` | `active_validated` | req/turn/delivery 三类事件持续落盘，可用于流量、turn 时延、错误和投递事件的原始观察。 | 07-15 canary；Relay `src/relay-request-trace.ts`；07-16~22 live 事件计数。 |
| `s1.request_reply_reconciliation` | `retired` | 不生成逐请求兑现率；FIFO/紧窗口算法仅供历史回放与审计。 | `scripts/reply-probe.py` 已知边界；07-17 root-fix；live timer masked/inactive。 |
| `s1.reply_quality_degen` | `active_validated` | 可显示已命中的退化事件与探针活性；不能扩写成“所有坏回复均可识别”。 | `scripts/degen-probe.py`；live `degen-probe.timer` enabled/active 与自然轮次。 |
| `s2.ack_overdue` | `active_validated` | 可显示 pending、breach、main_only、chronic 与当前告警。 | P0-1 memo；live ack 自然轮次与 heartbeat。 |
| `s2.ledger_invariants` | `active_validated` | 可显示 INV1~INV5 findings、新增集合与稳定指纹。 | Slice L + baseline report；live lint 自然轮次。 |
| `s2.executor_reaper` | `active_validated` | 可显示 reaped 数与 stale-unknown 数；不把 unknown executor 自动判死。 | 07-17 root-fix；live reaper 自然轮次与 heartbeat。 |
| `meta.probe_heartbeat` | `active_partial` | 可显示 registry 内 source 的 fresh/stale/missing；本机 watcher timer 自身被 disable 仍缺跨机对偶兜底。 | Gate 0 memo“仍非断根”第 2 条；live watcher 五 source fresh。 |
| `meta.systemd_health` | `active_validated` | 可显示在役 timer/service 的 enabled/active 与失败态；只读核对，不代表 timer 背后的探针语义正确。 | §四 live systemd 只读核对（五个在役 timer enabled/active、reply timer masked/inactive）；exporter `systemd_argv` 实采。 |
| `notify.action_package` | `active_partial` | 新链路可携带发生什么/影响/行动/演练标记；历史与相邻通知仍可能缺字段，Stats 必须允许 unknown。 | Gate 0 通知活体；07-18/19 通知链；07-22 live escalation events。 |
| `notify.daily_flush` | `active_validated` | 可显示汇总最近结果、发送条数和 freshness。 | 07-18 生产 UAT；live 最近自然轮次 `flushed 62` 并发 heartbeat。 |
| `policy.commitment_registry` | `active_partial` | policy 可加载，但只有 5 条承诺、无 `probe` 结构字段，且两条 S1 条目仍描述退休 probe。 | live `monitoring-policy.yaml`；SPEC §十承诺完备性不变量。 |
| `snapshot.canonical_producer` | `active_partial` | 固定 heartbeat/ack/ledger/systemd/escalation/revision 源可 one-shot 产 v2；trace/policy 尚未适配，且当前仅 manual，无 timer。 | Stats Slice 1 双次 Vultr canary；Stats SPEC P0-A。 |
| `incident.current_state_projection` | `active_partial` | heartbeat/ack overdue/ledger finding 支持持久 open/recovery/idempotency；其他 escalation 仍是 event-only，完整 source rebuild 范围有限。 | schema v1/0600 live state；Stats Slice 1 重复 projection canary；Feature SPEC §11.5。 |
| `s1.stable_interaction_identity` | `not_implemented` | 现有事件没有 interaction_id/final delivery kind，不能构成 S1 denominator。 | Stats SPEC P1；Feature SPEC §11.6；live trace schema。 |
| `s1.external_input_delivery_canary` | `not_implemented` | 必须显示“未实现”，不能用 Relay/trace 活性代替两头端到端证明。 | SPEC 切片 3；live 无对应 timer/registry source。 |
| `s1.headless_artifact_reconciliation` | `not_implemented` | Hume 有 policy 条目不等于有产物探针；不可显示总兑现率。 | live policy；SPEC 切片 4；live 无 artifact probe。 |
| `s2.cross_node_reconciliation` | `not_implemented` | peer heartbeat 不等于一笔 wi/ho 跨机闭环对账。 | SPEC 切片 5b；live 无 sync-audit/跨节点 probe。 |
| `slo.commitment_curves` | `not_implemented` | S1/S2 成功率及请求到最终送达的 P95/P99 必须为 `null/unknown`。 | SPEC Feature 验收 3 与切片 6；live 无曲线产物。 |
| `ops.incident_ledger` | `not_implemented` | 事故家族/复发/救火工时不可由散落 memo 冒充台账。 | SPEC Feature 验收 4 与切片 6；live 无对应 ledger。 |

## 3. Current Truth Source Precedence

本文件解决“SPEC、PROGRESS、代码、live 各说一套”的办法如下：

| 优先级 | 真源 | 负责回答 | 不负责回答 |
|---|---|---|---|
| 1 | Vultr live service/config/state | 什么正在运行、enabled/active、最近结果、当前 freshness | 最终目标与历史验收范围 |
| 2 | 与 live 对齐的 deployed source revision | 在役实现的行为、schema、回滚/安装约束 | 某次 UAT 是否真的通过 |
| 3 | 绑定入口与版本的 UAT/验收 memo | 哪个行为维度已被真实验证、证据边界是什么 | 组件今天是否仍在运行 |
| 4 | REQ v3 / SPEC v0.3.2 / Stats SPEC v0.1.2 | 最终目标、非目标、initiative 与产品契约 | 当前完成度与当前健康 |
| 5 | canonical WI / ResumePacket | objective、在飞焦点与下一步 | 监控能力的权威当前状态 |

维护规则：

- 本文件是下游 Stats 的 current capability SSOT；Stats 不直接拼接 WI/ResumePacket、历史 memo 与 live 猜结论。
- 若本文件与 live 冲突，live 事实优先，并立即把本文件视为 `stale`，更新后再供 Stats 发布。
- 目标与现状冲突不是脑裂：目标仍留 REQ/SPEC，现状在本文按四态显式表达。
- 任一能力的状态改变，必须同时更新证据指针、`last_verified` 和 snapshot 示例/测试 fixture。

## 4. Stats 最小稳定 Snapshot Contract

Stats 只依赖下面的逻辑 JSON 契约，不直接解析 systemd 输出、YAML、JSONL、Git 或 memo。v1 尚无 consumer/producer，故本轮允许破坏性修正并直接升为**未发布 v2**；第一个 consumer 发布后冻结 major 语义。以下是结构示例，不是 live snapshot。

```json
{
  "schema_version": "pulse.monitoring.snapshot.v2",
  "generated_at": "2026-07-22T12:19:17+08:00",
  "expires_at": "2026-07-22T12:22:17+08:00",
  "baseline_version": "0.2.2",
  "overall": {
    "runtime_health": "warning",
    "coverage": "partial",
    "open_incident_count": 1,
    "headline": "1 个 handoff 超窗；S1 逐请求兑现率不可用"
  },
  "capabilities": [
    {
      "id": "s2.ack_overdue",
      "implementation_status": "active_validated",
      "health": "warning",
      "aggregation_role": "runtime",
      "scope": "vultr enabled dispatchers",
      "reason": "1 pending handoff exceeded 15 minutes",
      "freshness": {
        "state": "fresh",
        "observed_at": "2026-07-22T12:00:07+08:00",
        "max_age_seconds": 2100
      },
      "evidence": ["systemd:ack-probe.service", "memo:2026-07-16-slice-P01"]
    },
    {
      "id": "s1.request_reply_reconciliation",
      "implementation_status": "retired",
      "health": "retired",
      "aggregation_role": "coverage_only",
      "scope": "pulse,bb feishu",
      "reason": "heuristic correlation retired; timer masked",
      "freshness": {
        "state": "not_applicable",
        "observed_at": null,
        "max_age_seconds": null
      },
      "evidence": ["systemd:reply-probe.timer", "memo:2026-07-17-monitoring-root-fix-p0"]
    }
  ],
  "incidents": [
    {
      "incident_key": "s2.ack_overdue:handoff_overdue:ho_example",
      "episode_id": "episode-example-1",
      "state": "open",
      "severity": "warning",
      "capability_id": "s2.ack_overdue",
      "scope": "ho_example",
      "title": "handoff 超过 15 分钟未 ack",
      "what_happened": "一笔已启用 dispatcher 的 handoff 超窗未接",
      "impact": "工作仍在，但尚无人开始处理",
      "suggested_action": "由 owner 检查该 handoff",
      "opened_at": "2026-07-22T11:45:00+08:00",
      "updated_at": "2026-07-22T12:00:07+08:00",
      "resolved_at": null,
      "occurrence_count": 2,
      "reopen_count": 0,
      "evidence": ["event:example-breach-1", "event:example-breach-2"],
      "recovery_evidence": []
    }
  ],
  "metrics": [
    {
      "id": "s2.ack_overdue.count",
      "capability_id": "s2.ack_overdue",
      "status": "available",
      "value": 1,
      "unit": "handoffs",
      "window": "current",
      "sample_count": 1,
      "quality": "validated",
      "scope": "vultr enabled dispatchers",
      "observed_at": "2026-07-22T12:00:07+08:00",
      "reason": null
    },
    {
      "id": "s1.interaction.fulfillment_rate",
      "capability_id": "s1.stable_interaction_identity",
      "status": "unknown",
      "value": null,
      "unit": "ratio",
      "window": "rolling_7_complete_days",
      "sample_count": null,
      "quality": "unavailable",
      "scope": "eligible human interactions",
      "observed_at": null,
      "reason": "stable interaction identity not implemented"
    }
  ],
  "alerts": [
    {
      "event_id": "event-example-only",
      "projection_status": "event_only",
      "severity": "info",
      "capability_id": "notify.daily_flush",
      "title": "daily flush completed",
      "occurred_at": "2026-07-22T12:00:00+08:00",
      "reason": "completion event has no open/resolved incident condition"
    }
  ],
  "provenance": {
    "producer": {
      "name": "pulse-stats-exporter",
      "version": "0.1.0"
    },
    "source_revisions": {
      "pulse": "e792319",
      "coevolution": "ffebf503",
      "agent_harness_component": "e67df8d3"
    },
    "sources": [
      {
        "id": "systemd-user",
        "freshness": "fresh",
        "observed_at": "2026-07-22T12:19:10+08:00",
        "error_code": null
      },
      {
        "id": "interaction-trace",
        "freshness": "fresh",
        "observed_at": "2026-07-22T12:18:00+08:00",
        "error_code": null
      }
    ]
  }
}
```

### v2 必守语义

1. `implementation_status` 枚举固定为 `active_validated | active_partial | retired | not_implemented`。
2. `health` 枚举固定为 `ok | warning | critical | unknown | stale | retired | not_applicable`。
3. `overall.runtime_health` 枚举=`ok | warning | critical | unknown | stale`；`overall.coverage` 枚举=`full | partial | unknown`。两轴必须独立传输，不提供一个可掩盖 coverage 的总绿值。
4. capability `aggregation_role` 枚举=`runtime | coverage_only`。runtime health 只归约 runtime 能力：先取 open incident 最高严重度；无 open incident 但必要 source stale/unknown 时降为 stale/unknown；其余为 ok。coverage 单独归约 required capability 的实现/可读状态，允许 `runtime=ok + coverage=partial`。
5. capability freshness 枚举=`fresh | stale | unknown | error | not_applicable`；incident/alert severity 统一为 `info | warning | critical`，raw `minor/major/critical` 在 provenance 可追溯地映射为 `info/warning/critical`。
6. 只有 `active_validated` 且 freshness 合格的能力可以为其 scope 提供绿色证据；`active_partial` 的成功轮次不能推出覆盖范围外的绿色结论。
7. 缺值、解析失败、source 超 freshness 一律是 `unknown/stale`，不得 fallback 为 0 或 `ok`。
8. `retired` 不参与 runtime incident 聚合但必须计入 Coverage；`not_implemented` 不是生产事故，也必须作为 gap 可见。
9. incident 状态枚举=`open | resolved`；同 key 重复事件只 update。只有晚于最后 breach 的 capability-specific 正向恢复证据可 resolved；沉默/stale/error 不能恢复。reopen 复用 incident_key、新建 episode_id。
10. 不能投影为 current condition 的事件放 `alerts`，`projection_status=event_only`；不得计入 `open_incident_count`。
11. metric status 枚举=`available | warming_up | unknown | not_applicable`；quality 枚举=`validated | descriptive | insufficient | unavailable`。每个 metric 必须同时声明 `window`、`sample_count`、`quality`；没有可靠 denominator 时 `value=null`、`sample_count=null`、`quality=unavailable|insufficient`，禁止从 req/turn/receipt 数量差推算成功率。
12. 每条 capability 必须带 `scope/reason/freshness/evidence`；incidents/metrics/alerts 必须有 capability/provenance 归属。没有 provenance 的值不进入正式 snapshot。
13. `generated_at` 是 exporter 生成时间，`expires_at` 是 snapshot 整体缓存上限；各 source/capability 仍保留自己的 freshness，不可互相覆盖。
14. payload 不含 message、prompt、用户 id、token、完整 session id 或可执行命令；evidence 只放稳定 locator。
15. v2 未发布前仍可修正；首个 consumer 发布后，破坏性改动升 v3，只增加可选字段可留 v2。

### Incident projection 约束

Canonical reducer 用 `<capability_id>:<condition_key>:<normalized_scope_key>` 生成稳定 key，支持 open/update/resolved/reopen；相同 event id 幂等。append-only escalation/event ledger 不等于 incident current state，daily flush 成功也不等于它承载的业务问题恢复。详细状态机以 `Stats/specs/pulse-stats.md` P0-B 为准。

### S1 metric 约束

`s1.interaction.fulfillment_rate` 在 stable interaction identity 完成并积满 7 个完整日前保持 `unknown/warming_up + null`。旧 trace 只可发布明确标注为 descriptive 的 turn duration，不得进入 request-to-final-delivery rate/P95/P99。

## 5. Stats Readiness

### 结论：`SLICE_1_MANUAL_CANARY_VERIFIED_48H_PENDING`

Slice 0 与 Slice 1 manual canary 已兑现：

- macOS 15 / Swift 6 `MenuBarExtra` 状态栏容器、手动刷新与 freshness 模型；
- snapshot v2 parser、fixture、schema/contract tests；
- fixed live snapshot adapter、persistent incident state、SSH transport 与 local cache；
- runtime health + coverage 双轴，以及 `unknown/retired/not_implemented` 的正式视觉语义；
- cache/schema 降级、incident/current、告警与 action_required 呈现。

下一步是 48 小时手动观察 CPU/RSS、SSH failure、cache stale 与错误绿色；在此之前不启后台刷新或 login item，manual canary 也不能推断为长期生产可用。

现在不得假装可靠：

- “PULSE 全绿”或“监控系统完整验收通过”；
- 每条交互请求的成功/失败配对；
- 全 agent 回复成功率、P95/P99；
- 入站与最终送达两头 canary；
- 六类无头产物总兑现率；
- 跨节点 wi/ho 闭环率；
- S1/S2 SLO 曲线和事故复发趋势。

Stats 的固定只读竖切片已完成 manual canary；P1 stable interaction identity 不阻塞 48 小时观察，真实采集器也不需要先补完监控 Feature 或重跑既有 UAT。具体实现边界与下一门见 `Stats/specs/pulse-stats.md`。

## 6. 责任、更新触发与残余风险

| 事项 | Owner | 动作 | 检查时点 |
|---|---|---|---|
| 维护本基线 | Pulse | live 拓扑、行为契约、部署路径、UAT 有效性或能力状态变化时原地更新并 bump version | 每次相关 Ship & Sync |
| 产出 snapshot | Stats/Pulse 实施切片 | Slice 1 已完成 fixed live adapter、cursor/state、SSH transport 与 manual canary；下一门为 48 小时观察 | 观察期结束 |
| 消费 snapshot | Stats | 只依赖 v2，不直接猜原始源语义 | 每次 UI 变更 |
| 补稳定交互身份 | Pulse | 独立 P1 high-risk slice；interaction context 贯穿 queue/runtime/delivery，满 7 完整日后解锁 SLO | P0 不等待；P1 发布后 |
| 补未实现能力 | Pulse | 各能力独立立 MVS；只重验受变更影响的契约维度 | 有明确优先级时 |

残余风险：

- 本文件是人工维护的汇总真源，尚无自动 Git↔live drift gate；超过 `last_verified` 后 Stats 必须显示 baseline freshness。
- live policy 与 SPEC 的 `probe` 不变量存在漂移，且仍描述已退休的 reply probe。
- `active_validated` 表示窄行为已验，不表示 Feature 全完成或永久无故障。
- heartbeat watcher 对自身被 disable 的情况仍缺跨机对偶证明。
- live adapter/current projection 仅覆盖固定 S2/meta/notify 子集；trace/policy 未适配，coverage 仍 unknown，不能把 manual canary 当 full coverage。
- stable interaction identity 尚未实现；Telegram delivery 与 busy queue correlation 均在 P1 范围，不能缩成 trace helper 补字段。

## 7. Contract Drift 与验证证据

### Contract Drift

- 生产行为、公开 CLI/API、错误语义、现有数据 schema、部署假设：**未改变**。
- 文档契约：本文升至 v0.2.2；Feature SPEC 升至 v0.3.2；Stats SPEC 升至 v0.1.2；`PROGRESS.md` 同步 Slice 1 manual canary 边界。
- 用户可见行为：状态栏可用固定 SSH 读取 live snapshot 并持久缓存；仍无后台 timer、登录项或分发。

### 本轮改动

- 迭代 `specs/pulse-monitoring-baseline.md` 至 v0.2.2，保持 snapshot v2 schema 不变。
- Stats SPEC 升至 v0.1.2，并兑现 Slice 1 manual canary。
- Feature SPEC 升至 v0.3.2，同步 live adapter/current projection 的 active_partial 边界。
- `PROGRESS.md` 指向 48 小时观察门。

### 验证证据

- 双次 Vultr exporter canary：payload 16354 bytes，projection digest 相同；runtime warning、coverage unknown、6 open（2 ack、4 ledger）。
- 本机 App 真实固定 SSH canary：projection digest 与 Vultr 相同；cache 0600/目录 0700；stdout/stderr 为空；退出后无残留 SSH。
- 只读核对 live systemd：五个当前在役 timer enabled/active；reply timer masked/inactive。
- 只读核对最近自然 journal：ack/lint/reaper/watcher/daily flush 均成功并续 heartbeat。
- 只读核对 live policy：version 0.2.0、5 条承诺、固定 10 分钟 reply 阈值、无结构化 `probe` 字段。
- 只读汇总七日 trace schema；没有执行 probe、故障注入或 UAT。
- 主身复核：`git diff --check`、JSON/schema/脱敏/幂等/权限/进程边界；Swift 24/24、Python 28/28。

### 主身复核点

1. 是否接受本文作为 Stats 的 current capability/snapshot SSOT，而 REQ/Feature/Stats SPEC 各守目标与产品角色。
2. 逐项核能力四态，重点看 `active_validated` 是否仍足够窄。
3. 核 snapshot v2 的 overall/incidents/metrics/alerts/provenance 与 unknown/retired 语义，防 UI 错给绿色。
4. 核 P0 不等 P1、旧 trace 不进 E2E rate、正向恢复才 resolved 三条硬边界。
5. 核四文件 diff 边界，且未把 live 工作区既有脏文件混入。
