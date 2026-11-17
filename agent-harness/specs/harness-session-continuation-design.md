---
feature_name: "session-continuation"
version: "0.3.4"
status: "completed"
root_work_item: "wi_20260820_272"
successor_feature: "harness-autonomous-rollover"
successor_work_item: "wi_20260822_730"
created: "2026-08-18T00:00:00+0800"
modified: "2026-10-07T17:40:00+0800"
owner: "Pulse"
upstream_dependency:
  - "specs/harness-session-continuation-requirements.md"
  - "features/heartbeat-runtime/design.md"
  - "specs/harness-runtime.md"
external_dependency:
  - "bigbird-online/PULSE:specs/pulse-session-context-lifecycle-design.md"
tags: [design, continuation, wi-execution, checkpoint, human-review, state-machine, relay-seam]
---

# Harness Session Continuation — Design

> Resilience correction (2026-08-29): the continuation data model and privacy invariants remain,
> while the synchronous Git-resident transaction, hard-blocking ingress and user-driven recovery
> design are superseded by `harness-scl-resilience-design.md`. New implementation uses a local
> durable operation journal, exact registration fencing and asynchronously reconciled effects.

> Terminal publication correction (2026-09-03): closeout publication is split into a
> pre-publication v4 settlement and a post-publication authority receipt; current head and
> Actions run identities are never embedded in the Git object whose identity they describe.

## 0. Machine-checkable design index

| ID | 设计契约 | 锚点 |
|---|---|---|
| REQ-DES-001 | 同 owner 接班必须使用新 `ContinuationContract`，不得放宽 `HandoffContract`。 | `@anchor symbol HandoffContract` `@anchor symbol ContinuationContract` |
| REQ-DES-002 | 根结果责任必须继续留在一个 `WorkItem`，rollover 不得按 phase 新建 WI。 | `@anchor symbol WorkItem` |
| REQ-DES-003 | continuation 必须使用五态与唯一迁移表。 | `@anchor symbol ContinuationState` `@anchor symbol VALID_CONTINUATION_TRANSITIONS` |
| REQ-DES-004 | bundle 必须先于 contract 持久化，并由 SHA-256 与 32768-byte budget 约束。 | `@anchor symbol ContinuationBundle` `@anchor symbol MAX_CONTINUATION_BUNDLE_BYTES` |
| REQ-DES-005 | bundle 必须只含仓库相对引用和 opaque run/session refs。 | `@anchor symbol ContinuationArtifactRef` |
| REQ-DES-006 | create 必须按事实 identity 幂等，随机 `ct_` ID 不得充当幂等依据。 | `@anchor symbol continuation_identity` |
| REQ-DES-007 | claim 必须在 per-continuation lock 内 single-winner。 | `@anchor symbol acquire_lock` `@anchor symbol claim_continuation` |
| REQ-DES-008 | continuation 状态变化必须复用 `SignalEvent`，不得建立第二事件账本。 | `@anchor symbol SignalEvent` |
| REQ-DES-009 | Relay seam 必须是 CLI JSON，Relay 不得直接写 Harness YAML。 | `@anchor symbol continuation_json_contract` |
| REQ-DES-010 | `continuation_ready` / autonomous policy 不属于本 feature 完成条件；由 successor `wi_20260822_730` 承担，当前保持 absent/default-off。 | `@anchor absent list_ready_continuations_for` |
| REQ-DES-011 | `CONTINUATION_PROFILE` 的 autonomous consumer 已移至 successor `wi_20260822_730`；本 feature 以其 absent 作为边界。 | `@anchor absent CONTINUATION_PROFILE` |
| REQ-DES-012 | spawn 前失败必须 release；consume 后业务失败不得复活 continuation。 | `@anchor symbol spawn_autonomous_session` `@anchor symbol release_continuation` `@anchor symbol consume_continuation` |
| REQ-DES-013 | 时间不得自动让 continuation 失效，废弃必须显式 supersede。 | `@anchor symbol supersede_continuation` |
| REQ-DES-014 | 普通 rollover 不得运行完整 outcome UAT，根 WI 完成仍走现有验收门。 | `@anchor symbol transition_work_item` |

## 1. 设计结论

本 feature 新增 `ContinuationContract`，不修改 `WorkItem` 的结果生命周期，也不改变
`HandoffContract` 的跨 owner 语义。

```text
一个成果                   一个跨 Agent 交付              一次同 Agent 接班
WorkItem                   HandoffContract                 ContinuationContract
owner/status/outcome       responsibility transfer        phase/run/session continuation
```

`@anchor symbol WorkItem`
`@anchor symbol HandoffContract`
`@anchor symbol ContinuationContract`

Harness 负责逻辑继续执行事实；Relay 负责物理 Session kill/spawn/reset。二者通过 Harness CLI JSON
交互，不共享内存，不直接编辑对方状态。

`@anchor symbol HarnessStore`
`@anchor symbol continuation_json_contract`

## 2. 目录与模块

实现后的 P0 结构如下。为避免在没有必要时扩大共享 `store.py` / `cli.py` blast radius，continuation
的 durable facade 放在 `ContinuationManager`；它仍复用 `HarnessStore._atomic_write_text`、
`HarnessStore.append_event`、现有 site policy 与 lock，因此没有第二套 ledger 或写入协议。

```text
.harness/
├── continuations/
│   └── ct_YYYYMMDD_NNN.yaml
├── evidence/
│   └── continuations/
│       └── ct_YYYYMMDD_NNN.yaml
└── events/
    └── <wi_id>.events.yaml

agent-harness/harness/
├── continuation.py        # model、validator、identity、state transition、durable facade
├── continuation_cli.py    # continuation 子命令组
├── cli_ext.py             # 在既有 main 上注册 continuation；不改旧命令语义
├── store.py               # 复用 atomic writer / SignalEvent append primitives
├── scan.py                # continuation_ready collector（P1，尚未接入）
├── policy.py              # ScanItemType 扩展（P1，尚未接入）
└── dispatcher.py          # CONTINUATION_PROFILE（P1，尚未接入）
```

新增模块保持无新依赖，复用 Pydantic、Click、PyYAML、`HarnessStore`、`SignalEvent` 和 lock 机制。

`@anchor symbol HarnessStore`
`@anchor symbol SignalEvent`
`@anchor symbol ContinuationContract`
`@anchor absent CONTINUATION_PROFILE`

## 3. 数据模型

### 3.1 `ContinuationState`

```python
class ContinuationState(str, Enum):
    READY = "ready"
    CLAIMED = "claimed"
    CONSUMED = "consumed"
    FAILED = "failed"
    SUPERSEDED = "superseded"
```

`@anchor symbol ContinuationState`

### 3.2 `ContinuationContract`

当前实现包含设计所需字段，并把 source / claimed / consumed 的 run 与 session refs 分开保存：

```python
class ContinuationContract(BaseModel):
    schema_version: Literal["1.0"] = "1.0"
    continuation_id: str
    work_item_id: str
    agent_id: str

    from_phase: str
    to_phase: str
    phase_attempt: int = 1
    state: ContinuationState = ContinuationState.READY
    attempt: int = 1

    bundle_ref: str
    bundle_sha256: str
    bundle_size_bytes: int

    source_run_ref: str
    source_session_ref: str
    claimed_run_ref: str | None = None
    claimed_session_ref: str | None = None
    consumed_run_ref: str | None = None
    consumed_session_ref: str | None = None

    failure_reason: str | None = None
    superseded_by: str | None = None
    created_at: datetime
    updated_at: datetime
    claimed_at: datetime | None = None
    consumed_at: datetime | None = None
```

约束：

- `continuation_id` 必须匹配 `ct_YYYYMMDD_NNN`。
- `work_item_id` 必须加载为 open WI。
- `agent_id` 必须等于根 WI owner，且 continuation 不写 owner。
- `phase_attempt >= 1` 且 `attempt >= 1`。
- `bundle_ref` 必须精确等于 `evidence/continuations/{continuation_id}.yaml`。
- `bundle_sha256` 必须为小写 64 位 SHA-256。
- `bundle_size_bytes <= 32768`。
- source/claimed/consumed run/session refs 必须是 domain-separated opaque ref 或允许的 `unknown`。
- `claimed` 必须有 claimant refs/timestamp；离开 claimed 时清空 claimant。
- `consumed` 必须有 consumed refs/timestamp；其他状态不得伪造。
- `failed` 必须有 `failure_reason`；其他状态不得遗留。
- `superseded` 必须有 `superseded_by`；其他状态不得遗留。

`@anchor symbol ContinuationContract`
`@anchor symbol MAX_CONTINUATION_BUNDLE_BYTES`

### 3.3 `ContinuationBundle`

bundle 是继续执行包，不是新的 truth store。它只引用已存在的事实和工件：

```yaml
schema_version: continuation_bundle/v1
continuation_id: ct_...
work_item_id: wi_...
agent_id: pulse

phase:
  completed: design
  next: implementation

objective: "实现 Harness ContinuationContract"
acceptance_criteria:
  - "..."

confirmed_facts:
  - statement: "Handoff 不允许 self-transfer"
    evidence_refs:
      - repo: CollaborateWithAI
        path: Bigbird/Lab/BigbirdEnvironment/agent-harness/harness/models.py
        revision: <git sha>
        sha256: <file sha>
decisions:
  - statement: "同 owner rollover 使用 ContinuationContract"
    rationale: "保持 Handoff 责任语义单一"
    evidence_refs: [...]

artifacts: [...]
current_state:
  completed: [...]
  not_completed: [...]
open_questions: [...]
risks: [...]

next_action:
  action: "实现 continuation.py"
  read_refs: [...]
  write_scope: [...]
```

`ContinuationArtifactRef`：

```python
class ContinuationArtifactRef(BaseModel):
    repo: str
    path: str
    revision: str
    sha256: str
    role: Literal["evidence", "implementation", "spec", "test", "decision"] | None
```

路径必须是正斜杠仓库相对路径。validator 拒绝空 revision、绝对路径、`..`、目录逃逸和 digest 格式错误。

`@anchor symbol ContinuationBundle`
`@anchor symbol ContinuationArtifactRef`

### 3.4 禁止材料

validator 必须拒绝：

- 顶层或嵌套字段名 `prompt`、`transcript`、`raw_session_id`、`thread_id`、`user_id`、`secret`。
- 绝对文件路径。
- bundle 内联大块工具输出或消息历史。
- 未经 domain separation 的 session/run id。

内容级隐私不能只靠字段名证明，因此保留人工/fixture privacy gate。

`@anchor manual continuation privacy validator :: 运行正负 fixture；grep bundle 确认无 prompt/transcript/原始 session-thread-user id/绝对路径/secret`

## 4. 状态机

```text
                 ┌──────── release（attempt+1） ────────┐
                 │                                      │
ready ──claim──> claimed ──consume──> consumed          │
  │              │   │                                  │
  │              │   └──fail────────> failed            │
  │              └────supersede──────> superseded       │
  └────────────────supersede─────────> superseded       │
```

`VALID_CONTINUATION_TRANSITIONS` 是唯一迁移真源；CLI 不自行手改 state。

```python
VALID_CONTINUATION_TRANSITIONS = {
    READY: {CLAIMED, SUPERSEDED},
    CLAIMED: {READY, CONSUMED, FAILED, SUPERSEDED},
    CONSUMED: set(),
    FAILED: set(),
    SUPERSEDED: set(),
}
```

`@anchor symbol VALID_CONTINUATION_TRANSITIONS`

### 4.1 Create identity

stable identity 输入：

```text
domain = "harness-continuation-v1"
work_item_id
agent_id
from_phase
to_phase
phase_attempt
bundle_sha256
```

create 先查相同 identity，命中则返回已有对象。ID 仍使用人类可读 `ct_YYYYMMDD_NNN`，identity digest
只作为事实幂等键和事件 metadata，不把随机 ID 当幂等依据。

`@anchor symbol continuation_identity`

### 4.2 Claim

claim 在 per-continuation lock 内完成：

1. load contract；
2. validate bundle 与 open WI；
3. 若相同 holder 已 claimed 则幂等返回；若不同 holder 则 conflict；
4. 写 `claimed_run_ref` / opaque session ref；
5. transition `ready→claimed`；
6. atomic save；
7. append `continuation.claimed` SignalEvent。

claim conflict 是业务冲突，不能覆盖或 fail-open。

`@anchor symbol acquire_lock`
`@anchor symbol claim_continuation`

### 4.3 Release / Consume

- release 只允许 claimant 执行；它清空 claimant、`attempt += 1`、回到 `ready`。
- consume 只允许 claimant 执行；它写 consumer refs 与 `consumed_at`，进入终态；相同 holder 重放幂等。
- consume 只表示新 Session/run 已理解并接管，不表示 next phase 或 WI 完成。

`@anchor symbol release_continuation`
`@anchor symbol consume_continuation`
`@anchor symbol WorkItemStatus`

## 5. Store 与事件

`ContinuationManager` 提供 continuation durable facade：

```python
save_continuation(contract)
load_continuation(continuation_id)
list_continuations(agent_id=None, state=None, work_item_id=None)
continuation_path(continuation_id)
continuation_bundle_path(continuation_id)
```

底层写入复用 `HarnessStore._atomic_write_text` 的同目录 tempfile + fsync + `os.replace`。bundle 先写，contract
后写，因此不会出现新 contract 指向尚未持久的 bundle；状态迁移在 per-continuation lock 内进行。

`@anchor symbol HarnessStore`
`@anchor symbol save_continuation`

事件复用 `SignalEvent`，事件类型：

```text
continuation.created
continuation.validated
continuation.claimed
continuation.released
continuation.consumed
continuation.failed
continuation.superseded
```

payload 只含 continuation metadata、opaque run/session refs、bundle digest；reason 只保存 SHA-256，
不得复制 bundle 正文或原始 Session ID。

`@anchor symbol SignalEvent`

## 6. CLI

```text
harness continuation create
  --work-item <wi>
  --agent <agent>
  --from-phase <name>
  --to-phase <name>
  --phase-attempt <n>
  --bundle <path>
  --source-run-ref <opaque>
  --source-session-ref <opaque>
  --json

harness continuation validate <ct> [--read-only] --json
harness continuation show <ct> --json
harness continuation list [--agent <agent>] [--state <state>] --json
harness continuation claim <ct> --run-ref <opaque> [--session-ref <opaque>] --json
harness continuation release <ct> --run-ref <opaque> --reason <text> --json
harness continuation consume <ct> --run-ref <opaque> [--session-ref <opaque>] --json
harness continuation fail <ct> --run-ref <opaque> --reason <text> --json
harness continuation supersede <ct> --by <new_ct> --reason <text> [--run-ref <opaque>] --json
```

机器输出采用稳定 envelope：

```json
{
  "ok": true,
  "continuation": {},
  "continuations": null,
  "validation": null,
  "error": null
}
```

错误时 `ok=false`，`error.code` 从 `invalid_contract`、`invalid_bundle`、`state_conflict`、
`lock_conflict`、`not_found`、`permission_denied`、`io_error` 中选择，并使用稳定 exit code。

默认 validate 保留 `continuation.validated` 审计事件；需要在紧邻 canonical mutation
之前执行 preflight 的控制面必须使用 `--read-only`。该模式返回相同 validation envelope，
但不写 event log，避免只读 preflight 自己制造 Git dirt 并阻断随后的 claim。

`@anchor symbol continuation_command`
`@anchor symbol continuation_json_contract`

## 7. Scan / Policy

`ScanHit.hit_type` 与 `ScanItemType` 增加 `continuation_ready`。collector 目标：

```python
def list_ready_continuations_for(agent_id, store):
    return [
        c for c in store.list_continuations(agent_id=agent_id, state=READY)
        if store.load_work_item(c.work_item_id).status is non-terminal
    ]
```

它只能把 continuation metadata 放入 `ScanHit`，不得读取或复制 bundle 正文。

`@anchor symbol ScanHit`
`@anchor symbol ScanItemType`
`@anchor absent list_ready_continuations_for`

policy 示例：

```yaml
scan_items:
  - type: continuation_ready
    compute_profile: platform_default
    default_autonomy: autonomous_llm
```

未声明即不扫描。首个 live opt-in 只允许 Pulse，避免同一 ready continuation 被多个 dispatcher 消费。

`@anchor symbol ScanItemPolicy`

## 8. Dispatcher

### 8.1 Profile

```python
ContextProfile = Literal["legacy", "night_patrol", "continuation"]
CONTINUATION_PROFILE = "continuation"
```

`_context_profile_for_hits()` 遇到 continuation 时必须要求 batch 只有一条；混合 batch 必须分流或 defer。

`@anchor symbol ContextProfile`
`@anchor symbol _context_profile_for_hits`
`@anchor absent CONTINUATION_PROFILE`

### 8.2 Minimal prompt

`CONTINUATION_PROFILE` prompt 只包括：

1. continuation ID、WI、phase；
2. bundle ref / 经验证的最小 continuation 内容；
3. acceptance criteria；
4. next action / read refs / write scope；
5. policy 允许工具和路径；
6. 第一项 durable 动作必须 consume；
7. failure/release 契约。

它不得包括 legacy 的近期 commits、完整 anti-pattern 教材、旧 HO 原文或旧 transcript。

`@anchor symbol _build_prompt`
`@anchor absent _build_continuation_prompt`

### 8.3 Spawn lifecycle

```text
ready
  → dispatcher claim
  → spawn
      ├─ 未启动 / import error / rate limit before consume → release
      ├─ run 首先 consume → continuation terminal consumed
      └─ consume 后业务失败 → 走既有 AutonomousExecutionEvent/熔断，不复活 continuation
```

dispatcher 结束时必须重新读取 contract：

- state=`consumed`：继续既有 status 判定；
- state=`claimed` 且 process 未完成 consume：release + status=error；
- state 被其他 actor 改变：state conflict，不能覆盖。

`@anchor symbol spawn_autonomous_session`
`@anchor symbol AutonomousExecutionEvent`
`@anchor symbol consume_continuation`

## 9. Relay 边界

Relay 只通过 CLI JSON：

```text
validate(ct)
show(ct)
claim(ct, consumer_run_ref, consumer_session_ref)
consume(ct, consumer_run_ref, consumer_session_ref)
release(ct, consumer_run_ref, reason)
```

Relay 不调用 Python 内部函数，不直接写 YAML。source/consumer 原始 Session ID 由 Relay 留在私有 state；
传给 Harness 的只能是 domain-separated opaque refs。

`@anchor manual relay CLI seam :: PULSE 隔离 UAT 记录 harness argv、JSON、exit code 与 `.harness` diff，确认无直接 YAML 写入`

## 10. 错误处理

| 场景 | 处理 |
|---|---|
| bundle 不存在或 digest 错 | validate 失败，contract 不可 claim |
| root WI 不存在/已终态 | create 或 validate fail closed |
| claim conflict | 返回 state_conflict，不覆盖 |
| spawn 未启动 | 若已 claim 则 release |
| consume 前 timeout/rate limit | release，attempt+1 |
| consume 后业务失败 | continuation 保持 consumed，沿用现有 autonomous failure |
| bundle 含私密/绝对路径 | invalid_bundle |
| event append 失败 | 返回 IO error；不得声称迁移已被完整审计 |
| Git 同步失败 | 状态可本地存在，但不得声明跨机可见；走既有 closeout/retry |

## 11. 关键决策

### Decision-001：新对象，不扩 Handoff

`HandoffContract` 的 receiver inequality 和责任转移是已有稳定语义。允许 self-handoff 会让 ack、owner
传播、main_only、cross_tenant 和 reverse handback 全部变得两义。

`@anchor symbol HandoffContract`
`@anchor symbol apply_handoff_acceptance`

### Decision-002：一个根 WI，不按 phase 建 WI

phase continuation 只是执行上下文，不是新的结果责任。最终 outcome 仍由根 WI 验收，避免 rollover
数量直接变成未关闭 WI 数量。

`@anchor symbol WorkItem`

### Decision-003：显式 supersede，不按 TTL 失效

时间不证明 continuation 已无效。长时间未消费可由观察层报告，但只有新 continuation 或 owner 明确动作
才能 supersede。

`@anchor symbol supersede_continuation`

### Decision-004：普通 rollover 不跑完整 outcome UAT

普通 continuation 只做 schema/digest/privacy/fresh consumer ACK；最终 WorkItem 仍走既有 independent
outcome receipt。把七格验收塞进每次换班会把换班本身变成新的成本大户。

`@anchor symbol transition_work_item`


## 12. Phase 2 — WI Execution Control Plane Design

### 12.1 Phase layering

```text
Phase 1 (已验证 primitive)
  WorkItem ── creates ──> ContinuationContract ── strict ACK/consume ──> fresh Session

Phase 2 (当前)
  WorkItem = durable execution identity
      │
      ├─ WorkItemExecutionBinding = 当前 epoch/phase/runtime/session 的 durable truth
      ├─ ExecutionCheckpoint      = 当前执行状态的最小 durable snapshot
      ├─ ContinuationContract     = 仅在 source→target Session transfer 时产生的事件
      └─ WorkItemReviewDecision   = exact candidate 的 human terminal review gate

Phase 3 (非本阶段)
  cost/quality/context signal ──shadow/suggestion──> 是否建议 rollover
```

Phase 2 不重写 Phase 1 continuation state machine；它在 continuation 上方增加 WI-centric control plane。

`@anchor symbol WorkItem`
`@anchor symbol ContinuationContract`
`@anchor symbol WorkItemExecutionBinding`
`@anchor symbol WorkItemReviewDecision`

### 12.2 Durable models

#### `ExecutionLifecycleState`

```python
class ExecutionLifecycleState(str, Enum):
    ACTIVE = "active"
    DORMANT = "dormant"
    TRANSITIONING = "transitioning"
    RECOVERY_REQUIRED = "recovery_required"
    TERMINAL = "terminal"
```

`checkpoint` 不是 lifecycle state；它是 durable snapshot 更新，所以 active Session checkpoint 后仍是 `active`。`terminal` 只表示 execution authority 已按 exact settlement 永久退休，不复制 WorkItem 的业务 outcome。

`@anchor symbol ExecutionLifecycleState`

#### `WorkItemExecutionBinding`

```python
class WorkItemExecutionBinding(BaseModel):
    schema_version: Literal["work_item_execution_binding/v1"]
    work_item_id: str
    agent_id: str
    epoch: int                         # >= 1
    phase: str
    runtime: str
    session_ref: str | None            # opaque, never raw Session ID
    continuation_id: str | None        # pending transfer or latest transfer lineage
    operation_ref: str | None = None   # only while TRANSITIONING
    lifecycle_state: ExecutionLifecycleState

    checkpoint_revision: int = 0              # WI-wide monotonic allocated sequence
    latest_checkpoint_ref: str | None = None  # current-epoch recoverable pointer; may be None while revision > 0
    latest_checkpoint_sha256: str | None = None
    checkpointed_at: datetime | None = None
    terminal_settlement_ref: str | None = None
    terminal_settlement_sha256: str | None = None
    authority_retired_at: datetime | None = None
    created_at: datetime
    updated_at: datetime
```

持久路径：

```text
.harness/executions/{work_item_id}.yaml
```

约束：

- 一个非终态 WI 最多一个 authoritative binding。
- `agent_id == WorkItem.owner`；owner 改变时必须停止同-owner execution flow 并先走既有 handoff/ownership 规则。
- `epoch` 单调递增，只能在 target ACK + continuation consumed 证据成立后 `+1`。
- `checkpoint_revision` 是 WI-wide append-only 序号，任何 rollover/adopt/restart 都不得回绕或复用；`latest_checkpoint_ref/sha/checkpointed_at` 才表示当前 execution container 是否已有可恢复 checkpoint。
- 显式 human reinit 创建新的 lineage，因此只允许新 lineage 从 revision 1 开始；旧 lineage 的
  checkpoint 与 identity 保持只读且不得重新激活。
- `session_ref` 必须是 domain-separated opaque ref；raw UUID/session/thread/user id fail closed。
- `TRANSITIONING` 必须有 `continuation_id + operation_ref`；正常 `ACTIVE/DORMANT` 不得遗留 pending operation。
- `TERMINAL` 必须完整绑定 repository-relative settlement ref、SHA-256 与 retirement timestamp；其它 lifecycle 不得携带这些字段。
- Phase-2 terminal settlement 先退休 execution authority，再写 root WI terminal event；中断后只可按同一 settlement digest 重放，所有 entry/resume/checkpoint/rollover mutation fail closed。

`@anchor symbol WorkItemExecutionBinding`
`@anchor symbol WorkItem`

#### `ExecutionCheckpoint`

Checkpoint 复用 ContinuationBundle 的“最小充分状态”原则，但它不是 transfer 事件：

```yaml
schema_version: execution_checkpoint/v1
work_item_id: wi_...
agent_id: pulse
epoch: 1
phase: phase2-specs
runtime: cc
session_ref: session_v1_...
operation_ref: operation_v1_...
acceptance_fingerprint: "sha256:..."
current_state:
  completed: [...]
  not_completed: [...]
decisions: [...]
artifacts: [...]
open_questions: [...]
risks: [...]
next_action: "..."
# Harness persists revision/input_sha256/created_at around this validated input.
```

路径：

```text
.harness/evidence/executions/{work_item_id}/checkpoints/000002.yaml
```

revision 在整个 WI 生命周期内单调分配。allocator 必须同时检查 binding counter 与已有 checkpoint 文件的最高 revision；即使 legacy/partial state 曾把 binding counter 归零，也必须写下一个新文件，禁止覆盖旧审计证据。binding 缺失但存在不同 operation 的 orphan checkpoint 时 fail closed 为 recovery-required。

Checkpoint 使用与 continuation bundle 同级的 32768-byte 上限与 privacy validator。它可以被后续 rollover
派生为标准 `continuation_bundle/v1`，但 checkpoint 自身不得包含 `continuation_id`，从语义上避免“checkpoint=已经发生交接”。

`scl_local_resume_packet/v1` 从 checkpoint 生成同一份最小充分状态投影。除 objective、acceptance
criteria、completed/pending、decisions、open questions、risks 与 business next action 外，投影必须包含按
checkpoint 原顺序保留的 `artifacts`，每项继续使用 `repo + path + revision + sha256 + role`。fresh continue、
applied-successor Host rebind 和 verified read-only snapshot 不得各自实现不同的字段子集；三条路径共享同一
artifact validator/projection，缺失或非法 artifact material 必须 fail closed，不能返回貌似完整的 ResumePacket。

`@anchor symbol ExecutionCheckpoint`
`@anchor symbol LocalResumePacket`
`@anchor symbol project_checkpoint_fields`
`@anchor symbol MAX_CONTINUATION_BUNDLE_BYTES`

#### `WorkItemReviewDecision`

```python
class WorkItemReviewDecision(BaseModel):
    schema_version: Literal["work_item_review/v1"]
    work_item_id: str
    reviewer: str
    decision: Literal["approved", "changes_requested"]
    acceptance_fingerprint: str
    artifact_set_sha256: str
    repo_heads: list[ReviewRepoHead]
    candidate_fingerprint: str
    evidence_refs: list[str]              # repository-relative only
    reviewed_at: datetime
```

当前 decision 路径：

```text
.harness/reviews/{work_item_id}.yaml
```

历史不靠多份 current file 保存；每次 decision 必须追加现有 `SignalEvent`，因此 review 变更有审计链。
Phase 2 的 human boundary 是“只有显式人类交互动作可记录 approved”，不是密码学身份认证；cron/autonomous
路径必须硬拒 approve，模型输出中出现 `approved` 字样不具有任何状态效力。

`@anchor symbol WorkItemReviewDecision`
`@anchor symbol SignalEvent`

### 12.3 Candidate fingerprint

Human review 必须像 PR review 一样绑定 exact candidate，而不是绑定一句 claim：

```text
candidate_fingerprint = sha256(canonical_json({
  work_item_id,
  acceptance_fingerprint,
  artifact_set_sha256,                    # normalized artifact refs + exact file-content SHA-256
  repo_heads: [{repo, head_revision}, ...]
}))
```

规则：

1. `repo_heads` 至少覆盖本 WI 改动过的每个仓库；revision 使用 full Git SHA。
2. artifact ref 在进入 fingerprint 前必须归一为 repository-relative identity，并读取 regular-file 精确字节计算 SHA-256；缺失、不可读、symlink、目录逃逸均 fail closed。
3. 任何 head revision、AC、artifact path 或 artifact 内容字节改变都得到新 fingerprint；canonical Outcome receipt/evidence 的 post-freeze mutation 必须使 review stale。
4. 旧 review record 保留审计，但 status 计算为 `stale`，不能满足 closeout。
5. merge 后默认分支验证必须证明 reviewed candidate revisions 已进入目标 default branch 历史；不能拿“另一个 green HEAD”代替。

`@anchor symbol review_candidate_fingerprint`

### 12.4 Binding state machine

```text
(no binding) ── bind/fresh ──> active(epoch=N)
                                  │
                 checkpoint ─────┤  (state stays active)
                                  │
 runtime gone ── reconcile ──> dormant(epoch=N)
        │                         │
        └──────── resume ─────────┘

active/dormant ── prepare rollover ──> transitioning(epoch=N, ct=X)
                                           │
             ACK + consume proven ─────────┼──> active(epoch=N+1, target ref)
             source recovery proven ───────┼──> active(epoch=N, source ref)
             cannot recover ────────────────└──> recovery_required(epoch=N)

active/dormant ── validated settlement ──> terminal(ref+digest; no execution exits)
terminal root WI ── execution mutation blocked; terminal binding remains read-only audit evidence
```

`VALID_EXECUTION_TRANSITIONS` 必须是唯一 lifecycle 迁移真源。checkpoint revision 更新与 lifecycle transition
分开实现，避免 checkpoint 意外改变 Session authority。

`@anchor symbol VALID_EXECUTION_TRANSITIONS`
`@anchor symbol checkpoint_execution`

### 12.5 Locking and idempotency

Phase 2 以 `work_item_id` 为 execution lock key。建议 operation identity：

```text
domain = "harness-wi-execution-v1"
action
work_item_id
epoch
checkpoint_revision
phase
```

`rollover` prepare 在 WI lock 内写 `TRANSITIONING + operation_ref + continuation_id` 后释放 WI lock；Phase 1
continuation claim/consume 继续使用自己的 ct lock。apply/recover 再取得 WI lock并核 continuation truth。这样不嵌套
WI lock 与 ct lock，避免 lock-order deadlock。

重复同一 operation：

- checkpoint：返回同 revision/current snapshot。
- continue：返回同 resolution/current authoritative session。
- rollover：返回现有 pending continuation/transition，不创建第二 target。
- review：相同 candidate + decision 幂等；不同 candidate 是新 review。

`@anchor absent execution_identity`
`@anchor symbol acquire_lock`

### 12.6 Harness machine API

底层 CLI 仍以机器 JSON 为准，建议 surface：

```text
harness execution status <wi> --json
harness execution bind <wi> --agent ... --phase ... --runtime ... --session-ref ... --json
harness execution checkpoint <wi> --checkpoint <checkpoint.yaml> --json
harness execution resolve-continue <wi> --runtime-state <runtime-observation.json> --json
harness execution prepare-rollover <wi> --to-phase ... --operation-ref ... --json
harness execution apply-rollover <wi> --continuation <ct> --target-session-ref ... --json
harness execution recover-rollover <wi> --continuation <ct> --source-session-ref ... --json
harness execution mark-dormant <wi> --session-ref ... --json
harness execution resume <wi> --session-ref ... --json
harness execution recovery-required <wi> --continuation <ct> --reason ... --json

harness review show <wi> --json
harness review record <wi> --decision approved|changes_requested \\
  --reviewer <human> --candidate <fingerprint> --evidence <path> --json
```

普通用户不直接使用这些细节；PULSE `WorkItemSessionController` 提供 WI-centric interaction。Harness JSON 中可返回
`continuation_id` 供诊断，但任何 user-facing command 都不要求 ct。

`@anchor symbol register_execution_runtime_commands`
`@anchor symbol review_command`

### 12.7 `continue` resolution contract

Harness 不直接 kill/spawn Session；PULSE 提供 runtime observation，Harness/PULSE 合并 durable truth 后必须得到一个确定 resolution：

| Durable binding | Runtime evidence | Checkpoint | Resolution |
|---|---|---|---|
| active | same opaque session live | any | `reuse_current` |
| dormant/active stale | Relay has matching private resume material | any | `resume_current_epoch` |
| no live/resume | valid checkpoint for current epoch | yes | `fresh_from_checkpoint` |
| legacy dormant binding | registered WI worktree dirty | any | fail closed with `worktree_dirty`; commit/push intended WI and repository-resident ledger changes, re-anchor exact-head review, then retry |
| transitioning/recovery_required | any | any | conflict/reconcile first |
| no live/resume | none | no | fail closed, human recovery needed |

`fresh_from_checkpoint` 必须内部创建/消费 ContinuationContract，并沿 Phase 1 strict ACK；它不是“直接 new”。

该 dormant settlement/clean gate 只适用于仍使用旧 execution binding 的继续路径；不得通过
stash/reset/clean 或忽略全部 `.harness` dirty 绕过它。settlement 后的新 commit 必须成为
exact-head review 的新锚点。已采用 SCL 当前有效授权的 reinit、注册修复及正常业务继续按
harness-scl-resilience 的共享授权契约执行，保留工作树与索引中的未提交成果；不得套用旧
clean gate。新工作树初始化、候选和终态结算各自保留原有边界。

`@anchor symbol resolve_execution_continue`
`@anchor symbol ContinuationContract`

### 12.8 Checkpoint flow

```text
/wi checkpoint <wi>
  → load WI + binding
  → current Session receives fixed checkpoint request
  → returns structured execution_checkpoint/v1 payload
  → validate AC fingerprint / privacy / size
  → atomic write checkpoint artifact
  → under WI lock: checkpoint_revision = max(binding sequence, durable history max)+1; latest_checkpoint_ref/digest/timestamp update
  → append SignalEvent
  → Session continues unchanged
```

任何 checkpoint 失败都不得修改 epoch、session_ref 或 continuation state。

`@anchor symbol checkpoint_execution`
`@anchor symbol ExecutionCheckpoint`

### 12.9 Rollover flow

```text
/wi rollover <wi>
  → resolve binding, no raw Session ID/ct from user
  → ensure a valid current-epoch checkpoint (may request one internally)
  → under WI lock prepare operation + derive Phase 1 ContinuationBundle/Contract
  → binding state=transitioning, continuation_id=ct
  → PULSE executes existing RolloverCoordinator strict validate/kill/spawn/ACK/consume
  → on consume proof: apply_execution_rollover(epoch+1, target opaque ref/runtime); preserve checkpoint_revision sequence, clear only current latest_checkpoint pointer
  → on source recovery proof: recover_execution_rollover(epoch unchanged)
```

`prepare-rollover` 不能把 epoch 先加一；`apply-rollover` 必须重新读取 continuation 并确认 `consumed`。

`@anchor symbol prepare_execution_rollover`
`@anchor symbol apply_execution_rollover`
`@anchor symbol consume_continuation`

### 12.10 Restart reconciliation

Harness 只保存逻辑 execution truth；Relay 保存物理 `RolloverState` 和 raw resume material。重启后 PULSE 必须把两边证据
一起核：

- binding active + same target live → no-op。
- binding transitioning + continuation consumed + Relay target authoritative → idempotent apply epoch+1。
- binding transitioning + source recovered + continuation ready/released → idempotent recover same epoch。
- binding transitioning + 两边事实不完整/矛盾 → `recovery_required`，禁止再 spawn target。
- Relay target_started 但 Harness 未 consume → 沿 Phase 1 原 deadline/恢复规则，不重置 timer。

`@anchor absent reconcile_execution_binding`

### 12.11 Human review and closeout

Phase 2 review 不是现有 independent outcome UAT 的替代，而是额外的人类“是否接受这个 exact candidate”门：

```text
implementation/tests ready
  → existing ready-for-uat + canonical outcome receipt
  → exact review candidate (producer corrections remain possible)
  → human review
       ├─ changes_requested → execution continues; candidate changes; old review stale
       └─ approved          → final freeze → merge reviewed candidate
  → derive required consumers from the enrolled publication repository’s versioned registry
  → exact consumer install/readback/real-effect probes
  → cleanup temp resources, then digest-revalidate all cleanup/probe/projection artifacts
  → write phase2_closeout/v4 settlement with only pre-publication facts
  → publish that unchanged settlement on current default and wait for exact-head CI/smoke
  → write phase2_publication_receipt/v1 with settlement ref/digest + resulting heads/run ids
  → query GitHub authority: reviewed→integration→receipt head ancestry + settlement Git blob
  → verify receipt-bound head ancestry, unchanged tested source/current WI inputs, and every exact-head Actions run
  → retire execution authority as terminal(settlement ref+digest)
  → write exactly one root WI terminal event
```

The receipt pins the successfully tested publication, not an endlessly moving main.
A later default head is eligible only after ancestry and complete immutable Git-tree
comparison prove the difference is limited to other WIs' namespaced regular ledger
files. Source, workflows, shared policy and this WI remain unchanged; existing
approval, candidate and evidence-closure validation is not bypassed. A source change
or incomplete tree requires new exact-version evidence, not an invented pass.
Manual CI is accepted only for the same named repository workflow at the pinned SHA,
with an event-neutral push/dispatch contract and every static declared job successful.
Dynamic/reusable workflows, event-dependent subsets and unrelated light probes are
not automatically qualified. No new review, settlement schema or authority is added.

`phase2_closeout/v4` deliberately excludes `resulting_head`、CI/smoke run ids and
other facts that can only exist after the settlement commit. The stage-two publication
receipt is repository-relative durable terminal evidence, but is not required to be in
the commit it describes. Harness binds its exact ref/digest into the terminal event and
revalidates live GitHub authority before the terminal write. This removes the impossible
fixed point in v3 while retaining fail-closed authority checks and exact replay.

Frozen, accepted and approved `main_only` candidates use `closeout prepare`,
`closeout publish` and `closeout complete` through the registered service authority.
Prepare reads Host files and produces a portable `phase2_settlement_bundle/v1`
outside the repository. It captures exact artifact bytes and relocates references to
content-addressed files under `.harness/evidence/closeout/<WI>/`. The canonical command
envelope captures this bundle, and the broker verifies its exact file closure before
publication. No producer executor, new review, reopen or reinit is required.

Publish revalidates the current acceptance/candidate, accepted Outcome, human approval,
integration ancestry, consumer inventory and evidence digests. It writes only the bound
settlement, artifacts and `phase2_settlement_binding/v1`, with no terminal state change.
Its JSON response includes the published SHA and publication transaction/replication
references. The settlement JSON path triggers Agent Harness CI on its exact default-branch
push head; stage-two publication receipts do not trigger another settlement CI run.
Complete accepts a Host-local publication receipt, binds it into the same
WI namespace, rechecks the stage-one approval/Outcome binding and invokes the existing
fixed-publication-head CI validator and unique terminal event. Missing CI returns a retryable
business failure with the validator reason; the canonical failed request remains immutable.
Evidence bytes are preserved even when Git whitespace checks would reject their formatting.

The consumer inventory repository supplies configuration authority independently of
the reviewed deliverables. Its repo/path/SHA-256 must match the local registry and a
blob resolved at its live GitHub default-branch head. It need not appear in settlement
or publication receipt repos; those sets remain exactly the human-approved repository
set. If the inventory repository is also a deliverable, its already-validated publication
head is used. A missing default head or different inventory blob fails closed.

The local inventory defaults to `config/scl-surfaces.yaml` in the canonical
repository; sparse projections read the clean HEAD blob without materializing
another file. `HARNESS_SCL_SURFACE_REGISTRY` is an explicit external-deployment
override, not a required caller input. The settlement cannot select that local
source. One captured byte sequence supplies parsing, obligation validation and
publication checks. Missing, modified or symlinked originals remain failures.

`@anchor test agent-harness/tests/test_closeout_publication.py :: test_default_inventory_rejects_missing_or_replaced_canonical_original`

Review and settlement retain the exact logical repository names approved by the human;
their bytes and candidate fingerprint are never rewritten for closeout. At consumption,
a logical name resolves by repo id, full slug or basename to exactly one enrolled service
topology. The enrolled slug is used for GitHub ancestry/CI/blob queries, consumer inventory
matching and the PULSE publication guard. Fully qualified names retain their existing
meaning. Missing or ambiguous enrollment and multiple entries resolving to one repository
fail closed. This lookup uses durable service topology, so retiring a WI worktree cannot
remove the identity needed to validate its settlement. Consumer manifests and publication
receipts may use canonical slugs while settlement reviewed keys remain exactly approved.

对没有启用 Phase 2 review gate 的 legacy WI，`transition_work_item(...COMPLETED...)` 的既有 receipt/evidence 语义保持原样。
Phase 2 gate 可通过 binding/review-required marker 显式 opt-in，不能无迁移地把所有存量 WI 一次性卡死。对 open Phase-2 WI，inventory path 缺失、producer omission/downgrade、未发布 settlement、cleanup 后证据缺失或 binding 未退休均 fail closed。旧 completed record 的 exact replay保持只读，但不能制造新的 active-authority terminal completion。

`@anchor symbol transition_work_item`
`@anchor symbol validate_execution_closeout`

### 12.12 Observe reconstructability contract

Phase 2 durable truth 的目标不是建立 Execution Evidence Plane，而是让真实 UAT 自然留下可 join 的生命周期骨架。

每个 binding/checkpoint/continuation event 至少稳定暴露：

```text
work_item_id + agent_id + epoch + phase + runtime
+ opaque session_ref + optional continuation_id + timestamps
```

上层 `wi_20260820_079` 的 Evidence Harvest 只允许引用 existing producer：Harness WI/continuation/review/outcome、Relay/runtime state/log、
ComputeCost usage、GitHub PR/merge/CI 与 smoke/cleanup evidence。Phase 2 不复制这些正文，也不创造第二 ledger。

若某事实无法由当前 producer 稳定证明，source index 写：

```text
status: unknown
reason: no_authoritative_producer | no_stable_join | source_unavailable
```

禁止把 absence 写成 `0`，禁止读当前 route/config 反推历史 model/effort/route，禁止用参与者回忆填洞。
execution binding 的 lifecycle 只回答执行 authority，不表达 business completed/failed/review/outcome；后者继续由根 WI 与既有 outcome/review 真源负责。

`@anchor symbol WorkItemStatus`
`@anchor symbol SignalEvent`
`@anchor manual wi272-observe-source-index :: 不读 transcript/参与者回忆，生成 source/ref/digest/authority/known-status seed；缺项 unknown`

### 12.13 ChatGPT+GitHub dogfood execution surface

`/wi <wi>` 在 ChatGPT+GitHub dogfood 中是 execution-entry intent，而不是 Relay `/wi status` 的别名。ChatGPT 产品当前不暴露 provider raw Session ID 或 actual model token usage，因此 Harness 只创建自己的 opaque surface segment identity：

```text
bare /wi <wi>
  → resolve root WI
  → no binding: bind current visible surface, epoch=1, runtime=chatgpt_web
  → existing chatgpt_web binding: reuse current durable authority
  → other runtime / transitioning / recovery_required: fail closed
  → load latest durable checkpoint if present
  → resolve continue from binding
  → create/update execution_surface_observation/v1
```

surface observation 固定：

```text
execution_surface = chatgpt_web_github
source = chatgpt_pro
cash_basis = same_chatgpt_pro_monthly_subscription
usage_truth = visible_export_reference_only
```

`shared_chatgpt_pro_subscription` 作为产品描述映射到上面的 ComputeCost canonical cash-basis 枚举，不新增同义枚举。Pro 月度现金不能按单 WI/turn 虚构分摊；`cash_allocation_to_work_item=unavailable`。

从 entry timestamp 开始记录 visible user/assistant turns 和 `visible_text_reference_v1`。reference units 的稳定规则是：每个 CJK/Hiragana/Katakana/Hangul code point、每个 Latin/digit/underscore run、每个其余非空白 code point 各计 1；它只用于可重复的可见文本比较，绝不是 actual model tokens。原始可见文本不持久化。

GitHub 只记录从 entry 开始由当前 Agent 实际执行/观察的 operation counters、commit/PR/review/CI facts；WI/epoch/phase/opaque session/continuation 作为 lineage spine。每个可见 turn 可标 `productive|recovery|verification|adjudication`，显式用户纠错可计 human-correction；rework/outcome 优先从 Harness 权威事件派生。

这张 surface snapshot 是现有 producer 缺口上的最小观测 companion，不表达 execution state transition，也不进入 ComputeCost usage ledger。`provider_session_identity`、`actual_model_tokens` 缺 producer 时必须 unavailable，不能用模型名称、当前配置或参考 token 反推。

`@anchor symbol ExecutionSurfaceManager`
`@anchor symbol ExecutionSurfaceObservation`
`@anchor test test_surface_cli_enter_then_observe_round_trip`

### 12.14 Compatibility and Phase 3 seam

- `harness continuation ...` 全部保留；Phase 2 只封装，不改 Phase 1 contract/state。
- `/new`、`/compact` 与 PULSE `/rollover <ct>` legacy primitive 继续各自语义。
- `continuation_ready` autonomous scan/profile 在 Phase 2 不启用；原 P1 设想重新归类为 Phase 3 candidate。
- ComputeCost 可读 WI/epoch/checkpoint/continuation lineage 做 shadow report，但不得反馈为自动 rollover action。

`@anchor symbol continuation_command`
`@anchor absent CONTINUATION_PROFILE`

### 12.15 Phase 2 test matrix

必须至少覆盖：

1. normal：bind → checkpoint → continue/reuse → rollover → ACK/consume → epoch+1。
2. failure：checkpoint invalid、ACK mismatch、consume failure、source recovery failure。
3. restart：transitioning 各中间态 reconciliation，不产生第二 target。
4. duplicate/idempotency：重复 checkpoint/continue/rollover/review。
5. concurrency：两个 rollover/continue writer 只一人赢。
6. privacy：raw Session/thread/user id、prompt、secret、绝对路径 fixture 全拒。
7. regression：legacy WI completion、continuation CLI、`/new`、`/compact`、`/rollover <ct>`。
8. dogfood entry：ChatGPT+GitHub bare `/wi wi_20260820_272` 从真实 unbound WI bind epoch=1，并从 entry 起记录 visible reference/GitHub/activity evidence；provider tokens/session/cash allocation 保持 unavailable。
9. dogfood rollover：`wi_20260820_272` 至少两次 real fresh CC rollover，source transcript target 阶段不可读。
10. Observe harvestability：不读 transcript/回忆，只靠现有 source refs 尝试重建 lifecycle→runtime→candidate/review→outcome→merge/default；缺 model/effort/usage/route/human-attention 时保持 unknown，false-zero=0。
11. terminal publication：真实 Git commit 先发布不含 future head/run ids 的 v4 settlement，随后 receipt 绑定实际 commit；缺失/篡改 receipt 与非 exact replay 均 fail closed。

`@anchor test test_execution_rollover_epoch_and_recovery`
`@anchor test test_human_review_fingerprint_gates_closeout`
`@anchor test test_phase2_two_stage_publication_uses_post_commit_receipt`
`@anchor manual wi272-two-fresh-rollovers :: 保存两次 real fresh CC 证据与 epoch/cost/correction/rework 读数`

## 13. 回滚

- policy 移除 `continuation_ready` 后 autonomous chaining 关闭；在 P1 未接入前该 collector 本来就是关闭的。
- CLI/model/manager 文件可保留只读；ready contract 不会自行执行。
- Relay feature flag 关闭后，Session Context Lifecycle 只保证 `/compact`、`/new` 与 legacy rollover 的既有边界；`/wrap` 不属于本 feature 的回滚或兼容性前提。
- 已 consumed continuation 保留审计，不回退为 ready。
- 未 consumed continuation 可显式 supersede，不批量删除。
- Phase 2 若回滚：停止 WI-centric controller，binding/checkpoint/review 保留只读审计；legacy continuation 与 WI/HO 路径继续可用。
- 已发生的 epoch/review/event 记录不得删除或倒写；恢复行为通过新 event/decision 追加。


### 12.15 WI028 G0 consumer projection contract

`wi_20260820_272` 的 `s1_measurement_pilot.v1` 是 approval 后的 read-side consumer projection，不进入 reviewed candidate artifact set。closeout 必须机械要求正好一份 projection，并验证其 `evidence.as_of >= human review reviewed_at`。读数遵循已 adopt 的 `wi_20260820_028 / Phase A` G0：result cycle 以首次真实 `execution_started_at` 为 start、`delivery_accepted_at` 为 end；post-approval correction 仅统计 delivery acceptance 后原 scope 内事实/方向/质量缺陷导致的必须修正，新需求、计划迭代、普通开发修改、测试失败、planned rollover、runtime restart、单纯补证据均不计。缺成熟 correction window/authority join 时保持 unknown/unavailable。human attention 并列 raw human turns 与 HAU v1 0.25/1/2/4，并保持可重算 effort-band counts；不保存 transcript。每个 measurement leaf 必须携带 G0 的 window/as_of/eligible_denominator/observed_denominator/coverage/status/value/unknown_count/source_refs。
