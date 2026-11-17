---
name: requirements
version: 0.0.2
created: 2026-04-02T14:00:00+0800
modified: 2026-08-04T21:45:00+0800
changelog:
  - "0.0.2: work item 完成前增加 awaiting_verification 与 Outcome UAT 回执门禁"
feature_name: "core-state-machine"
upstream_dependency: "project-specs/project-vision.md, project-specs/project-architecture.md"
tags: [requirements, l2-spec, core-state-machine]
---

# core-state-machine — 功能需求

> **一句话**：定义 agent-harness 的三个核心数据模型和状态迁移逻辑，用代码强制合法性。

---

## 功能概述

本功能实现 agent-harness 的 domain layer：

1. **三个 Pydantic models**：WorkItem、HandoffContract、SignalEvent
2. **状态机**：WorkItem 和 HandoffContract 的状态迁移逻辑，非法迁移由代码拒绝
3. **事件生成**：每次状态迁移自动产出 SignalEvent

这是整个工具的地基——后续的 store（M2）、CLI（M2）、真实链路（M3）全部依赖这一层。

---

## 用户故事

| ID | 角色 | 故事 | 优先级 |
|----|------|------|--------|
| US-01 | Ken | 我希望创建一个 work item 对象，包含 title / owner / acceptance_criteria 等字段，以便追踪跨 agent 协作 | P0 |
| US-02 | Ken | 我希望对 work item 执行状态迁移时，系统自动校验前置条件（如 complete 需要 evidence），非法迁移被拒绝 | P0 |
| US-03 | Ken | 我希望创建 handoff contract，记录 from/to/input/expected_output，以便交接有契约可查 | P0 |
| US-04 | Ken | 我希望 handoff 的 ack/reject 操作被记录，且 pending handoff 阻止 work item complete | P0 |
| US-05 | Ken | 我希望每次状态变更自动生成 SignalEvent，以便事后审计 | P0 |

---

## 功能需求

### FR-01：WorkItem Model（P0）

**描述**：Pydantic model 定义 work item 的字段和校验规则。

**字段**：
- `work_item_id: str` — 格式 `wi_YYYYMMDD_NNN`
- `title: str` — 非空
- `owner: str` — agent 名称
- `status: WorkItemStatus` — 枚举：draft / ready / in_progress / awaiting_verification / blocked / completed / failed
- `acceptance_criteria: list[str]` — 完成条件列表
- `artifact_refs: list[str]` — 相对路径列表
- `outcome_claim: str | None` — 待核验的精确结果声明
- `outcome_change_type: str | None` — 变更类型（code / rule / doc / mixed 等）
- `outcome_producer: str | None` — 产出方稳定身份
- `outcome_acceptance_fingerprint: str | None` — 冻结验收标准的 SHA-256 指纹
- `blocked_reason: str | None` — blocked 时必填
- `failure_reason: str | None` — failed 时必填
- `created_at: datetime`
- `updated_at: datetime`

**校验规则**：
- title 不能为空字符串
- work_item_id 必须匹配 `wi_\d{8}_\d{3}` 格式
- blocked_reason 和 failure_reason 在非对应状态时必须为 None

**来源**：US-01，project-architecture.md § 数据模型 / WorkItem

### FR-02：HandoffContract Model（P0）

**描述**：Pydantic model 定义 handoff contract 的字段。

**字段**：
- `handoff_id: str` — 格式 `ho_YYYYMMDD_NNN`
- `work_item_id: str`
- `from_owner: str`
- `to_owner: str`
- `input_summary: str` — 非空
- `expected_output: str` — 非空
- `acceptance_criteria: list[str]`
- `ack_state: AckState` — 枚举：pending / accepted / rejected / timed_out
- `reject_reason: str | None` — rejected 时必填
- `created_at: datetime`

**校验规则**：
- from_owner ≠ to_owner
- reject_reason 在非 rejected 状态时必须为 None

**来源**：US-03，project-architecture.md § 数据模型 / HandoffContract

### FR-03：SignalEvent Model（P0）

**描述**：Pydantic model 定义事件记录。

**字段**：
- `event_id: str` — 格式 `ev_YYYYMMDD_NNN`
- `work_item_id: str`
- `event_type: str` — 如 `work_item.created`、`handoff.accepted`
- `operator: str` — 触发者
- `timestamp: datetime`
- `payload: dict` — 自由结构，记录变更内容

**来源**：US-05，project-architecture.md § 数据模型 / SignalEvent

### FR-04：WorkItem 状态机（P0）

**描述**：实现 work item 的状态迁移逻辑。

**合法迁移**：
| 从 | 到 | 前置条件 |
|----|-----|---------|
| draft | ready | acceptance_criteria 非空（≥1 条） |
| ready | in_progress | owner 非空 |
| in_progress | blocked | blocked_reason 非空 |
| blocked | in_progress | 清除 blocked_reason |
| in_progress | awaiting_verification | 已冻结 claim、change type、producer、acceptance fingerprint |
| awaiting_verification | completed | 独立 Outcome UAT 回执通过严格校验；artifact_refs 中至少 1 个 evidence 路径；无 pending handoff |
| awaiting_verification | in_progress | UAT 不通过或需要返工；清除候选结果字段 |
| awaiting_verification | blocked | blocked_reason 非空 |
| awaiting_verification | failed | failure_reason 非空 |
| in_progress | failed | failure_reason 非空 |

**非法迁移**：上表之外的所有组合一律拒绝（抛出异常）。

**副作用**：每次合法迁移自动生成 SignalEvent。

**来源**：US-02，project-architecture.md § 状态机 / Work Item

### FR-05：Handoff 状态机（P0）

**描述**：实现 handoff contract 的状态迁移逻辑。

**合法迁移**：
| 从 | 到 | 前置条件 |
|----|-----|---------|
| pending | accepted | — |
| pending | rejected | reject_reason 非空 |
| pending | timed_out | — |

**副作用**：
- `accepted` 时，关联 work item 的 owner 更新为 `to_owner`
- 每次迁移自动生成 SignalEvent

**来源**：US-04，project-architecture.md § 状态机 / Handoff

---

## 验收标准

| ID | 标准 | 度量方式 |
|----|------|---------|
| AC-01 | WorkItem model 拒绝非法字段值（空 title、格式错误的 id） | ≥5 个 Pydantic ValidationError 测试用例 |
| AC-02 | 所有 6 条合法迁移路径测试通过 | 6 个 happy path 测试 |
| AC-03 | 所有非法迁移组合被拒绝 | ≥20 个 bad path 测试（6×6=36 减去 6 条合法 = 30 种，覆盖 ≥20 种） |
| AC-04 | 前置条件缺失时迁移被拒绝 | ≥5 个前置条件缺失测试（如 complete 无 evidence） |
| AC-05 | pending handoff 阻止 work item complete | 1 个专项测试 |
| AC-06 | 每次合法迁移产出 SignalEvent，event_type 正确 | 每条迁移路径检查 event 产出 |
| AC-07 | machine.py 测试覆盖率 ≥95% | pytest --cov |
| AC-08 | models.py 测试覆盖率 ≥90% | pytest --cov |

---

## 非功能需求

- **性能**：单次状态迁移执行时间 <10ms（纯内存操作，不含 IO）
- **可测试性**：models 和 machine 不依赖文件系统或外部服务
- **类型安全**：通过 mypy strict 检查

---

## 约束条件

- 技术栈：Python 3.11 + Pydantic v2（引用 project-architecture.md）
- 不引入文件 IO（store 是 M2 的事）
- 不引入 CLI（CLI 是 M2 的事）

## 排除范围

- ❌ 文件持久化（store.py → M2）
- ❌ CLI 命令（cli.py → M2）
- ❌ 超时自动化（→ v1.0）
- ❌ projection 生成（→ v1.0）

---

## 版本历史

| 版本 | 日期 | 变更内容 | 变更人 |
|------|------|---------|--------|
| 0.0.1 | 2026-04-02 | 初始版本 | Ken |
