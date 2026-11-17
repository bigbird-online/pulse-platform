# SPEC：PULSE Stats — macOS 状态栏只读控制面

> version: `0.1.3`
> status: `slice_1_manual_canary_verified`（**v0 部分仍是现行契约**；P1 章节未实现，见下）
> modified: `2026-08-01`
> authority: PULSE Stats 产品与工程契约
> owner: Pulse
> modified: `2026-07-22`
> upstream: `specs/pulse-monitoring-baseline.md` v0.2.2、`memo/2026-07/2026-07-14-SPEC-monitoring-system-mvs-and-slices.md` v0.3.2
> code_root: `/Users/owner/Coevolution/Bigbird/PULSE/Bigbird/Stats`
> export_entry: `/Users/owner/Coevolution/Bigbird/PULSE/Bigbird/Stats/exporter/pulse-stats-snapshot.py`（Slice 1 已部署；只允许 one-shot/manual 调用）
> 版本史：只看 git log，不在文件头复制 changelog

> ## 2026-08-01 核对结论（与 v2/v3 不同：本文**没有**整体作废）
>
> 用 spec-anchor 跑本文得 17 锚点 / 9 broken，**逐条回源核实后结论如下**——
> 与 `pulse-stats-menubar-v2.md`、`pulse-stats-v3-alert-triage.md` 的处置**不同**：
> 那两份是「描述的东西已被撤销」，本文是「P1 那部分**本来就还没做**，而 spec 自己早说了」。
>
> **8 条属 P1 未实现，不是漂移**：`interaction_id`、`source_event_ref`、`request_kind`、
> `metric_eligibility`、`exclusion_reason`、`started_at`、`duration_ms`、`delivery_id`
> ——实测在 Stats 与 Relay 生产者侧**均 0 命中**。
> 但 §1 边界第 5 条与 §3 早已写明「P1 稳定交互身份完善前，S1 逐请求兑现率与 E2E P95/P99
> 必须为 unknown；这不阻塞 v0」。**spec 说了没做、代码也确实没做——这是一致，不是漂移。**
> 工具判它们 broken 是因为它只认「符号在不在」，读不出「§1 已声明这是 P1 未来项」。
>
> **1 条是真漂移（已订正）**：§2 事故投影表原写 `last_observed_at`，
> 实现里从来没有这个字段——`IncidentProjection` 用的是 `updated_at`（每次 breach 刷新）
> 与 `last_breach_at`（reducer.py:86/93）。已按代码改正。
>
> **现行契约的分工**：本文管产品定义、边界、验收与 P1 路线；
> 代码级契约看从代码提炼的五份 `stats-*.md`（594 锚点、pre-commit 闸守着）。
> 两者不冲突：本文说「该做什么」，那五份说「现在是什么」。

## 0. 一句话产品定义

PULSE Stats 是原生 macOS 状态栏只读控制面：像 macOS Stats 一样常驻、点击展开，让大鸟一眼分清“平台此刻是否需要关注”和“监控覆盖是否足以支持这个结论”，但不在 v0 内执行修复、重启、ack 或任何生产写操作。

## 1. Minimum Viable Spec

### 目标

把现有监控的可信能力、当前 incident、指标和覆盖缺口归约成一个稳定 snapshot，通过状态栏提供低打扰、可追因、不会假绿的日常入口。

### 边界

- v0 单用户、单站点：ownermac 读取 Vultr PULSE。
- App 只消费 canonical snapshot；不解析 raw JSONL、systemd、Git、YAML、memo 或 `.harness`。
- exporter/reducer 对 Relay、systemd、`.harness`、policy 与事件源只读；唯一允许的写入是 reducer 自有派生 state 与 App 本地 cache，不得改变被监控的 production state。
- runtime health 与 monitoring coverage 是两条独立轴；unknown/partial 不得显示成绿色。
- P1 稳定交互身份完善前，S1 逐请求兑现率与 E2E P95/P99 必须为 unknown；这不阻塞 v0。

### 上下文与已核环境

- 开发机：macOS 15.7.3、Xcode 26.3、Swift 6.2.4。
- App 技术基线：Swift 6、SwiftUI `MenuBarExtra`、deployment target macOS 15。
- SSH 别名真源：`/Users/owner/Coevolution/ssot/toolchain/ssh-config` 的 `Host vultr-pulse`。
- 2026-07-22 只读预检已确认：`ssh -o BatchMode=yes -o ConnectTimeout=5 vultr-pulse` 成功；pulse 用户可读 PULSE repo、`/home/pulse/.relay/obs`、Coevolution `.harness/events`，并可查 user systemd watcher 状态。
- 监控能力与 snapshot schema 真源：`specs/pulse-monitoring-baseline.md`。

### 验收

1. 状态栏同时表达 runtime health 与 coverage，任何 partial/unknown 组合都不会假绿。
2. 点击后能回答：发生了什么、影响什么、是否要动作、证据多新、哪些能力没覆盖。
3. SSH 超时、无交互失败、非法 JSON、schema 不兼容时保留最后成功快照并显式 stale，不清零成健康。
4. incident 列表来自 current-state reducer；重复 event 不重复开 incident，沉默不自动恢复。
5. 全部输入只读、无 secret/message content 落缓存或日志；VoiceOver 与非颜色提示可独立使用。

### 风险

- Feature risk：`high-risk`。错误绿色会掩盖真实失约；P1 还涉及 Relay 生产事件 schema 与通道投递路径。
- 当前 Slice 1 risk：`high-risk`。固定 live adapter/SSH 已进入 manual canary，但未进入后台常驻或登录项。

## 2. 用户任务与成功尺度

状态栏首要服务四个判断：

1. **要不要现在看？** runtime 是否 critical/warning/stale。
2. **这个结论可信到哪？** coverage 是 full/partial/unknown，snapshot 与各 source 是否新鲜。
3. **到底哪件事没闭环？** 当前 open incident、影响对象、最近变化与建议动作。
4. **只是来过一条事件，还是问题仍存在？** incident 与 event-only alert 明确分区。

v0 成功不是“页面做出来”，而是连续日常使用中不出现三类误导：缺数据当 0、旧缓存当 live、事件数量当 incident 数量。

## 3. Initiative 范围与依赖

| 能力 | 本次决策 | 原因与边界 |
|---|---|---|
| Canonical snapshot producer | **P0，随 Stats Slice 0 开发** | 没有单一、稳定、可版本化的输入，App 就会重新拼接多真源并制造第二套事实。 |
| Current incident reducer | **P0，随 producer 开发** | append-only event 不能回答“问题现在还在不在”；只允许写可重建的监控派生 state。 |
| Stable interaction identity | **P1，纳入同一 initiative，独立 high-risk slice** | 它是可靠 S1 denominator/E2E 的必要条件，但 Stats 可先把该指标显示为 unknown，不阻塞 P0。 |
| 两头 canary、无头产物对账、跨节点业务 canary、语义质量深探 | **暂缓，显示为 coverage gap** | 都重要，但与状态栏最小可信闭环不构成同一原子切片；不得用既有弱信号冒充。 |

```text
P0-A canonical snapshot producer ─┐
                                 ├─> Stats Slice 0: native shell + fixture
P0-B incident current-state reducer┘                └─> read-only live canary

P1 stable interaction identity ──> future S1 rate/P95 cards

Deferred high-risk slices ───────> coverage gaps only; do not block P0
```

### P0-A：Canonical Snapshot Producer

目标：在 Vultr 上提供一次性、只读 exporter，把散落真源归约为 `pulse.monitoring.snapshot.v2`，stdout 只输出一份 JSON；无常驻 daemon，不写任何被监控 source。incident reducer 可原子更新自己的派生 projection state。

必须聚合：

- `overall.runtime_health` 与 `overall.coverage`；
- capability status/health/freshness；
- incident reducer 的 current projection；
- 可靠指标与明确为 unknown/warming_up 的指标；
- event-only alerts；
- source revision、读取时刻、source freshness、producer version 与错误摘要。

失败语义：

- 单一 source 失败不得悄悄丢字段；对应 capability/metric 置 unknown，provenance 记录脱敏错误。
- 核心 source 全不可读时 exporter 非零退出，不输出伪完整 snapshot。
- exporter 不读取 token，不输出完整 message、用户标识、session 内容或命令 stdout 原文。
- snapshot payload 上限 1 MiB；超过上限 exporter 非零退出，防止 UI/缓存被事件洪水拖垮。

总体归约必须保守：先按 `aggregation_role=runtime` 的能力聚合；有 open critical/warning incident 时分别为 `critical/warning`，这些能力的必要 source 过期或不可读时为 `stale/unknown`，不得被“incident 数为 0”覆盖；runtime sources fresh 且无 open breach 时可为 `ok`，即使 coverage 仍为 partial。任一 required capability 为 active_partial/retired/not_implemented 时 coverage 至少为 `partial`；baseline 或必要 coverage source 不可读时为 `unknown`。producer 输出理由与 provenance，App 不另算第二套真相。

实现为 `Bigbird/Stats/exporter/` 下的一次性 Python 3 CLI；canonical 入口为 `/Users/owner/Coevolution/Bigbird/PULSE/Bigbird/Stats/exporter/pulse-stats-snapshot.py`。`--fixture <normalized-input.json> --json` 保留离线契约；`--json` 只进入固定生产 adapter。adapter 只读固定 heartbeat/ack/ledger/systemd/escalation/revision 源，不接受 host、path 或 command 参数；未接入的 trace/policy 能力显式 unknown。Slice 1 没有 timer/daemon。

### P0-B：Incident Current-State Reducer

#### 为什么必须与 exporter 同期

escalation/event ledger 是 append-only 历史，不是当前 incident 表。把每条事件直接列为“事故”会让定时重复告警膨胀数量，也无法回答问题是否恢复。

#### 稳定 incident key

```text
incident_key = <capability_id>:<condition_key>:<normalized_scope_key>
```

- key 只含稳定业务维度，不含 event id、occurred_at、severity 或展示文案。
- `normalized_scope_key` 采用该判据最小稳定对象：如 heartbeat source、handoff id、ledger finding key、agent+channel。
- 可能含用户或 session 身份的 scope 必须先归一/散列；message content 永不入 key。
- reducer 对同一 event id 幂等；重复读取不会增加 occurrence。

#### 状态机

| 输入 | 原状态 | 新状态 | 必须保留 |
|---|---|---|---|
| 首条 breach evidence | 不存在 | `open` | opened_at、episode_id、severity、evidence |
| 新的同 key breach | `open` | `open/update` | `updated_at`、`last_breach_at`、唯一 `occurrence_count`、最新摘要 |
| 明确 recovery evidence，且晚于最后 breach | `open` | `resolved` | resolved_at、recovery evidence |
| 再次 breach | `resolved` | `open/reopen` | 同 incident_key、新 episode_id、reopen_count+1 |
| 沉默、source stale、解析失败 | 任意 | **不改变业务状态** | freshness 变 stale/unknown，绝不自动 resolved |

正向恢复证据由 capability contract 指定，例如：

- heartbeat stale → 同 source 后续 heartbeat fresh；
- handoff overdue → 同 ho 后续 ack/archive；
- ledger finding → 后续 lint 明确不再包含该 finding key；
- delivery failure → 同 interaction 的 final/fallback sent（P1 后才可用）。

禁止：

- “超过 N 小时没再报”推断 resolved；
- 把重复 event 当多个 incident；
- 把 daily flush 成功当其承载的所有业务 incident 已恢复；
- 把只有 occurrence、没有可逆 condition 的 event 硬投影成 current incident。

无法形成 current-state 的记录放 `alerts`，标 `projection_status=event_only`，不计入 `overall.open_incident_count`。

Reducer state 固定为 `/home/pulse/.relay/monitoring/pulse-stats/incident-state-v1.json`：单写者、schema versioned、0600、同目录临时文件后原子替换。它只保存 incident projection、source cursor/watermark 与有界幂等信息，不得保存 message/secret；事件按 source occurrence time 归约，读盘顺序不得让较旧 recovery 覆盖较新 breach。重建必须证明已覆盖各 source 的必要保留窗口；state 缺失/损坏且无法完整重建时，incident capability 与 overall 必须降为 unknown，绝不能用空表给绿。回滚前备份，删除或回退 state 不等于 incident resolved。2026-07-22 manual canary 已创建 schema v1/0600 state；当前只有 heartbeat、ack overdue、ledger finding 三类有明确 recovery，其他 escalation 仍是 event-only。

### P1：Stable Interaction Identity

目标：每条**可计量人工请求**在通道入口生成一个不可变 `interaction_id`，随消息排队、turn、投递尝试一直流转，彻底替代 poolKey+FIFO 猜配。

#### 已知当前裂缝

- Feishu trace 只有 pool/session 维度；旧 reply-probe 依赖 `(agent,poolKey)+FIFO`，已退休。
- Telegram 回复在 `src/channels/telegram.ts` 直接 `sendWithRetry`，当前没有同等 delivery trace 收口。
- `src/conversation-runtime.ts` 的 `activeTurns` 以 tenant 为单值；同 tenant busy/queued message 可能覆盖当前 correlation context。

因此 P1 不能只给 trace 函数“补一个字段”。必须把 interaction context 作为消息/队列的一部分，从 ingress 到每次 delivery attempt 原样流转。

#### 入口与 context 契约

在通道完成鉴权、去重并接受路由后，为每条请求生成 UUIDv7（或等价的时间有序全局唯一 id）：

```text
InteractionContext {
  interaction_id,
  received_at,
  source_event_ref,
  channel,
  agent_id,
  tenant_scope,
  request_kind,
  metric_eligibility,
  exclusion_reason?
}
```

- `source_event_ref` 是通道原生入站 message/update/event identity 的规范化散列；不保留 raw id。相同通道事件的 redelivery 必须得到相同 ref，并在 auth/dedup 后保持幂等：不得生成第二个 `interaction_id`，也不得新增 denominator 项。
- queued message 必须携带自己的 context；禁止用 tenant/pool 的单槽 `activeTurns` 反查当前 id。
- runtime adapter 的 send/queue/result/fallback 接口都必须能拿回原 context。
- retry、runtime fallback、streaming update、static fallback 只复用同一 `interaction_id`，不得新造请求 id。

#### 事件 schema

三类事件共同必填：`schema_version`、`event_id`、`interaction_id`、`agent`、`channel`、`ts`。

| Event | 额外必填 | 契约 |
|---|---|---|
| `req.inbound` | source_event_ref、request_kind、metric_eligibility、exclusion_reason(nullable) | denominator 的唯一入口；一条 accepted request 只写一次；相同 source redelivery 幂等。 |
| `turn.end` | turn_id、attempt、started_at、duration_ms、terminal、outcome | runtime fallback 可有多个 attempt，每个 attempt 恰一条 end；attempt 从 1 单调递增，outcome=`success/error/timeout/cancelled`，每个 interaction 至多一条 `terminal=true`。 |
| `delivery.receipt` | delivery_id、kind、attempt、terminal、outcome | kind=`progress/final/fallback`；attempt 从 1 单调递增；outcome=`sent/failed/cancelled`。 |

`progress` 永远不能兑现 interaction。E2E fulfilled 只由 `final|fallback` 且 `terminal=true,outcome=sent` 产生；final failed 后 fallback sent 仍是一次 fulfilled interaction，所有 attempt 保留审计。Telegram 多分片或任何多段投递中，单段 sent 只能记 progress；必须等全部必需分片成功后才写 interaction 级 final sent，部分成功不得冒充兑现。

#### S1 denominator 与派生结果

进入 denominator：通过 auth/dedup、已路由、`metric_eligibility=eligible` 的真人普通请求。

明确排除：

- bot command 与管理命令；
- pairing/auth/setup 流程消息；
- system/cron/handoff 自动输入；
- drill/canary；
- 被 dedup 拒绝、未被系统接受的重复消息。

分类失败不得静默排除：记 `metric_eligibility=unknown`，并降低 coverage。

```text
fulfilled = eligible interaction 在窗口内出现 final/fallback terminal sent
failed    = eligible interaction 已有 terminal turn/delivery 失败且无成功 fallback
open      = 已 accepted 但尚未终结且未超窗
timed_out = 超窗仍无 terminal result
rate      = fulfilled / (fulfilled + failed + timed_out)
```

旧 7 日 trace 只可用于 turn duration 等描述性分布，不进入 request-to-final-delivery rate，也不用于回填 E2E P95/P99。

P1 schema 在全部纳入 denominator 的通道部署完成、完整性门通过后，从下一个 Asia/Shanghai 00:00 起计；连续 7 个完整日无 schema gap 才发布 rolling SLO。在此之前 snapshot metric 状态=`warming_up`、value=`null`，但 Stats v0 继续工作。

#### P1 验收与发布边界

- contract/property tests：每个 eligible req 恰一 interaction_id，跨 queue/retry/fallback 不变。
- 并发/排队 characterization：同 tenant 连续消息不会互相覆盖 context。
- Feishu streaming/fallback 与 Telegram `sendWithRetry` 都产 final delivery receipt。
- event completeness checker：缺事件、重复 terminal、attempt 倒退、unknown classification 都外显。
- 真实入口 UAT 只重验受 schema/通道改造影响的交互路径；不重跑无关的 ack/lint/notification UAT。
- Feature/Slice risk 均为 `high-risk`；dark deploy → 单通道 canary → 真实入口 → 观察 → 扩面，回滚关闭新 trace schema 并让 snapshot 指标回 unknown，禁止回启 FIFO probe。

### 本 initiative 暂不实现

以下能力继续作为 coverage gap 显示，后续各立 high-risk slice：

- 入站与最终送达的两头 canary；
- 六类无头产物对账；
- 跨节点业务 canary/跨节点 wi-ho 对账；
- 语义质量深探。

它们未实现不阻塞 Stats v0；Stats 也不得用 heartbeat、文件存在或 degen 信号替代这些能力。

## 4. v0 技术架构

```text
raw sources on Vultr
  └─ one-shot snapshot exporter (read-only reducer)
       └─ stdout: pulse.monitoring.snapshot.v2 JSON
            └─ SnapshotTransport
                 ├─ v0: ssh vultr-pulse, BatchMode, fixed command
                 └─ future: Tailscale HTTPS（同 schema，不改 UI）
                 └─ PulseStatsCore decode/cache/derive client transport state
                      └─ SwiftUI MenuBarExtra + popover
```

### Slice 1 repo layout

```text
/Users/owner/Coevolution/Bigbird/PULSE/Bigbird/Stats/
├─ PulseStats.xcodeproj/
├─ PulseStats/                  # app lifecycle / core / transport / views / fixtures
├─ PulseStatsTests/             # decoder / presentation / cache transport tests
├─ exporter/pulse-stats-snapshot.py  # fixture + fixed live one-shot entry
├─ exporter/pulse_stats/        # producer/reducer/live source/state implementation
└─ README.md                    # MVS / build / test / explicit boundary
```

### App build contract

- Xcode 26.3 / Swift 6.2.4，Swift language mode 6；macOS deployment target 15。
- SwiftUI lifecycle + `MenuBarExtra`，使用 `.menuBarExtraStyle(.window)` 承载 dashboard；App target 设 `LSUIElement=true`，不出现 Dock 图标。
- Slice 0 只做本机 ad-hoc signed/manual run；进入长期 canary、Launch at Login 或分发前，必须使用 Developer ID Application 签名、Hardened Runtime 并完成 notarization 验证。
- App Sandbox 关闭：v0 需要通过 `Process` 调固定 `/usr/bin/ssh`。固定可执行文件、alias 与远端命令，不接受用户拼接 shell。
- Launch at Login 只在 read-only canary 通过后 opt-in，使用 `SMAppService.mainApp`；不手写 LaunchAgent。
- exporter 可独立迭代；App 只看 schema major，不耦合 exporter 语言和 raw source path。
- `SnapshotTransport` 是 Core protocol；v0 只实现 SSH。未来可换 Tailscale HTTPS，但认证、证书与 endpoint 须另立 high-risk slice，不能在 v0 暗留 token 或公网入口。

## 5. 信息架构

### 状态栏首屏

状态栏 icon 必须同时编码两轴，不能只给一个绿/红点：

| runtime_health | 主符号 | 语义 |
|---|---|---|
| `ok` | checkmark | 当前可信信号无 open breach |
| `warning` | triangle/exclamation | 有需关注 incident |
| `critical` | octagon/xmark | 有 critical incident |
| `stale` | clock | 只有旧快照或关键 source 过期 |
| `unknown` | questionmark | 无法形成当前判断 |

| coverage | 辅助环/徽标 | 颜色语义 |
|---|---|---|
| `full` | 实心环 | 只有与 runtime=ok 同时成立时可绿 |
| `partial` | 半环 | amber，绝不绿 |
| `unknown` | 空心环 | gray，绝不绿 |

优先级：critical > warning > stale > unknown > ok。runtime=ok + coverage=partial 的最终 icon 仍为 amber 半环；颜色只是冗余编码，形状和 VoiceOver label 必须能独立表达。

### 点击展开 Popover

1. **Header**：Runtime、Coverage 两张并列摘要；snapshot age；SSH/cache 状态；Refresh。
2. **Open Incidents**：按 critical→warning→updated_at 排序；显示 title、scope、age、occurrences、发生什么/影响/建议动作。无 incident 时明确写“当前无已知 open incident”，不写“系统绝对健康”。
3. **Capabilities**：S1 / S2 / Meta / Notify 分组；每项显示 implementation status、health、freshness 与 coverage reason。
4. **Metrics**：只显示有可靠分母的值；unknown/warming_up 显式占位并给原因。旧 trace turn duration 可标“描述性，不是 E2E SLO”。
5. **Recent Alerts**：event-only 单独列，明确“事件，不代表仍 open”；默认最近 20 条。
6. **Footer**：当前实现 Refresh、Copy Summary、Quit；Preferences/Clear Cache 尚未实现。v0 无 Fix/Restart/Ack/Retry/Flush 按钮。

点击 incident 可展开证据摘要与 provenance；不得直接展示用户消息、完整 session id 或 secret-bearing command。

## 6. Snapshot 与客户端派生状态

Server snapshot schema 以 baseline v0.2.2 的 v2 为准。客户端另维护 transport envelope，不反写 server payload：

```text
TransportState {
  state: live | cached | failed | schema_incompatible,
  fetched_at,
  last_success_at?,
  cache_age_seconds?,
  error_code?
}
```

App 的显示 runtime state 为 server `overall.runtime_health` 与 transport/freshness 的保守合成：

- live + snapshot fresh：使用 server runtime health；
- fetch 失败但 cache 未过 snapshot `expires_at`：显示原 health，同时挂 cached 徽标；
- cache 过期：强制 `stale`，即使缓存内容原为 ok；
- 无 cache/JSON 非法/schema major 不兼容：`unknown`；
- client 不根据 capabilities 自行重算 incident truth，只做上述保守降级。

## 7. 刷新、超时与缓存

- Slice 1 canary 阶段只在 App 启动时拉一次，之后仅手动 Refresh；popover 在尚无 snapshot 时可触发一次补拉。
- 60 秒后台刷新、wake/network regain 与 popover age refresh 尚未启用；只能在 48 小时手动观察门通过后作为独立变更进入，不能由本 spec 的目标态文字冒充已实现。
- P0 exporter 默认 `expires_at = generated_at + 180s`；这只是 snapshot 整体新鲜度，各 capability/source 仍按自己的 max_age 判 fresh/stale。
- 同时最多一个 fetch；新刷新取消/合并旧请求，不积压 ssh 进程。
- 固定 `/usr/bin/ssh` 参数数组：`-o BatchMode=yes -o ConnectTimeout=5 vultr-pulse /usr/bin/python3 /Users/owner/Coevolution/Bigbird/PULSE/Bigbird/Stats/exporter/pulse-stats-snapshot.py --json`；App 不拼 shell 字符串，不允许任意 host/command 输入。
- command 总超时 10 秒；超时后 terminate，2 秒未退再 kill；stdout >1 MiB 立即失败。
- 失败不做自动高频重试；手动 Refresh 可立即再试。
- 只缓存最后一份**成功解码且 schema 可接受**的 snapshot，原子替换，文件权限 0600；不缓存失败 payload，不保留历史事件仓。
- cache 无固定删除 TTL，但超过 snapshot `expires_at` 必须 stale；Clear Cache UI 尚未实现，进入后续独立切片且只删本地缓存。
- sleep 期间不追补每个漏掉的轮次；wake 后只拉一次当前 snapshot。

## 8. 安全与隐私

- 复用 SSH config 的 `vultr-pulse` 服务用户别名；`BatchMode=yes` 禁止密码/确认交互把 UI 卡住。
- 不把 private key、token、IP、用户名复制进 App 配置；不修改用户 SSH config 或 known_hosts。
- remote command 固定、参数固定；snapshot 中的 locator 只允许稳定 evidence id，不允许可执行 shell。
- exporter 输出白名单字段；禁止 message text、prompt、open_id、chat_id、token、完整 session id。
- App 日志只记 error code、duration、payload bytes、schema version；不记录完整 JSON。
- 本地 cache 放 Application Support，0600、原子写；卸载/清缓存可完全移除。
- v0 无任何生产写能力；即使 incident 带 suggested_action，也只显示/复制人话摘要，不执行。

## 9. 无障碍与体验

- VoiceOver label 同时读出“Runtime 警告；Coverage 部分；快照 45 秒前”。
- 状态不只靠颜色：icon shape、文字 label、排序与 badge 四重表达。
- 支持 Dynamic Type、Increase Contrast、Reduce Motion；重要状态不使用闪烁。
- 键盘可遍历 popover，当前 Refresh/Copy/Quit 有明确 focus 与 shortcut；Preferences 进入实现后再纳入此门。
- 日期默认显示本地 UTC+8 相对时间，详情保留 ISO-8601；不能用“刚刚”掩盖 stale。
- 空态分别写“无 open incident”“没有 event”“source unknown”，不混成一个空白页面。

## 10. 非目标

- v0 不做生产写操作、自动修复、重启、ack/reassign、告警 retry/flush。
- 不做 APM/TSDB、历史大盘、按模型或 token 单价计算的 API 等效成本分析、日志浏览器、
  全量搜索；固定订阅账单归因由 v2 §3.4 定义。
- 不让 App 直连 Relay 内部 API、读取 raw JSONL 或执行多条诊断命令。
- 不把 Stats 当 monitor-of-record；server-side producer/reducer 才是归约真源。
- 不在本 initiative 首版实现两头 canary、无头产物对账、跨节点业务 canary、语义质量深探。
- v0 不建 Tailscale HTTPS endpoint；只保留 transport protocol，后续替换不改变 snapshot schema/UI。
- 不因 P1 未完成阻塞 P0，也不提前展示 S1 E2E rate。

## 11. 测试矩阵

| 层 | 必测场景 | 通过门 |
|---|---|---|
| Snapshot schema | v2 完整样例、可选字段、未知 enum、破坏性 major | decoder 不崩；不识别的 major 拒绝并 unknown |
| Producer unit | source ok/missing/malformed/stale、payload limit、provenance | 缺 source 显式 unknown；核心全失效非零 |
| Incident reducer | open/update/idempotent/resolved/reopen、out-of-order、重复 event、event-only | 只有正向恢复可 resolved；重复不增 incident |
| Identity contract（P1） | queue/concurrency/retry/runtime fallback/Feishu/TG | interaction_id 不变，progress 不兑现 |
| Core aggregation | runtime×coverage 全组合、critical 优先、stale override | partial/unknown 永不绿 |
| SSH transport | success、exit nonzero、BatchMode auth fail、connect timeout、command timeout、oversize、invalid JSON | UI 不挂；保留 cache 并正确 stale |
| Cache | 首次无 cache、原子替换、坏 cache、过期、clear | 坏/旧 cache 不冒充 live |
| UI | ok/warning/critical/stale/unknown、0/多 incident、长文案 | 快照测试稳定；无截断关键 action |
| Accessibility | VoiceOver、键盘、contrast、color blindness | 不靠颜色也能判两轴和 incident |
| Performance | 60 秒常驻、连续失败、sleep/wake | 单一 ssh；idle CPU p95 <0.5%，RSS <80 MiB |
| Security | log/cache secret grep、command injection、payload fuzz | 无 secret/message；固定 command 不可注入 |

自动化测试不能替代只读真实入口验收；真实入口也不清零已继承的监控 UAT。

## 12. 发布梯度与回滚

### P0 发布梯度

1. snapshot v2 fixtures + schema/incident reducer contract tests；
2. MenuBarExtra 完全用 fixtures，覆盖两轴和 stale；
3. fake ssh transport 验证 timeout/cache/invalid JSON；
4. exporter 在 Vultr dark deploy，仅手工执行、无 timer/daemon；
5. ownermac 单用户 read-only canary，手动启动、不设 login item；**2026-07-22 已通过**；
6. 观察 48 小时 CPU/RSS、SSH failure、cache stale 与错误绿色；**下一门**；
7. 通过后再 opt-in Launch at Login。

每级单独记录 build/test、签名产物、实际命令摘要、日志错误、cache 与回滚证据。P1 按其 high-risk 通道梯度独立发布，不能搭 P0 UI 顺车全开。

### 回滚

- App：退出并移除 login item；回退到上一签名 build；删除本地 cache 不影响生产。
- Transport：禁用 live fetch 后保留 fixture/debug 能力；schema 不兼容自动 unknown，不回退到 raw parsing。
- Exporter：停止调用/回退入口文件即可；一-shot producer 无 daemon/state（incident reducer state 除外）。
- Incident reducer：回退 reducer version 前备份其 derived state；不把 state 删除当 incident resolved。
- P1：关闭新 schema/metric 发布，Stats 回 unknown；禁止回启 retired FIFO reply-probe。

## 13. 切片顺序

### Stats Slice 0（fixture implementation verified）

- 创建仓内 `Bigbird/Stats`（绝对路径 `/Users/owner/Coevolution/Bigbird/PULSE/Bigbird/Stats`）工程骨架；
- 冻结 snapshot v2 Swift model + JSON fixtures；
- 实现两轴 icon、popover IA、transport/cache protocol 的 fake；
- 同期实现/验证 server producer 与 incident reducer contract，但暂不接生产定时入口；
- slice risk=`normal`，因为 fixture app 与 one-shot 未部署 exporter 不进入生产入口。

### Stats Slice 1（manual canary verified）

- 固定 ssh command 接只读 exporter；双次 exporter + 单用户 App manual canary 已通过；
- 已验 freshness、0600/原子缓存、无交互失败、超时/oversize、single-flight 与进程退出；
- 当前停在 48 小时手动观察门，不启后台刷新/login item；
- slice risk=`high-risk`，因为产品开始对生产健康给用户可见结论。

### Monitoring P1

- stable interaction identity 作为独立 high-risk Relay slice；
- 满七个完整日后只解锁 S1 rate/P95 card，不改变 Stats 其他区域。

## 14. Contract Drift 与能力声明

本次 v0.1.2 保持 snapshot v2 major 不变，把 baseline v0.2.2 / Feature v0.3.2 的 Slice 1 窄边界落成固定 live adapter、持久 incident state、固定 SSH transport 与持久本地 cache。新增生产行为仅是手工 one-shot 读取及 exporter 自有派生 state；没有 timer、daemon、login item、Relay/harness/policy/systemd 写入或 P1 identity 改动。

当前已验证：Swift 24/24、Python 28/28；Vultr exporter 连续两次得到相同 incident projection；本机 App 通过真实固定 SSH 获得同一 projection，缓存 0600、目录 0700，stdout/stderr 为空，退出后无残留 SSH。canary 当时 runtime=warning、coverage=unknown、6 个 open（2 ack、4 ledger），证明缺口没有被画绿。

当前未验证：48 小时常驻资源/失败观察、后台刷新、登录项、Developer ID/公证分发、P1 identity 与 S1 E2E rate。`slice_1_manual_canary_verified` 不是“产品已长期发布”。

不可宣称：PULSE 全绿、监控 Feature 完成、S1 request-to-delivery SLO 可用。

## 15. 参考资料

- Apple SwiftUI `MenuBarExtra`：<https://developer.apple.com/documentation/swiftui/menubarextra>
- Apple Service Management `SMAppService`：<https://developer.apple.com/documentation/servicemanagement/smappservice>
- 开源 macOS Stats（产品形态参考，不继承其数据模型）：<https://github.com/exelban/stats>

## 16. 残余风险与主身复核点

残余风险：

- v0 依赖系统 SSH 与非 sandbox 分发，签名/公证和 Process 生命周期需实测。
- producer/reducer 当前只覆盖固定 S2/meta/notify 源；trace 与 policy 未接入，coverage 因此保持 unknown。
- reducer 的 source-specific recovery mapping 若定义过宽，会错误 resolved；每个 capability 实施时须单独审查。
- P1 改动横跨 Feishu、Telegram、queue/runtime 与 delivery，不能缩成 trace helper 字段补丁。
- coverage 长期 partial 时，用户可能形成“黄灯疲劳”；UI 必须区分稳定 gap 与新 incident。

主身复核：

1. 两轴 icon 是否足够直观，尤其 runtime=ok + coverage=partial 不绿。
2. P0 incident reducer 与 producer 同仓同切片，以及固定 state path、重建失败降 unknown 的契约是否可接受。
3. P1 denominator/exclusion、七个完整日与 Telegram/queue context 边界是否完整。
4. 48 小时手动观察通过后，是否启用 60 秒刷新；10 秒总超时和资源预算是否合适。
5. v0 非 sandbox + `/usr/bin/ssh` 的分发取舍是否接受。
