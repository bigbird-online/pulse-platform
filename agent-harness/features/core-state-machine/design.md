---
name: design
version: 0.0.2
created: 2026-04-02T14:00:00+0800
modified: 2026-08-04T21:45:00+0800
changelog:
  - "0.0.2: 设计 awaiting_verification 与已验证 Outcome UAT 回执门禁"
feature_name: "core-state-machine"
upstream_dependency: "project-specs/project-architecture.md, features/core-state-machine/requirements.md"
tags: [design, l2-spec, core-state-machine]
---

# core-state-machine — 技术设计

---

## 技术栈

基于 project-architecture.md 定义的技术栈：

- **Python 3.11+**：语言
- **Pydantic v2**：model 定义 + 字段校验
- **pytest**：测试框架
- 本功能**不使用** Click / PyYAML / 文件 IO（这些属于 M2 store + CLI）

---

## 模块划分

```
harness/
├── models.py      ← 本功能：Pydantic models + enums
└── machine.py     ← 本功能：状态迁移逻辑
```

**依赖关系**：`machine.py` → `models.py`（单向）

---

## 详细设计

### 1. models.py

#### 1.1 枚举定义

```python
class WorkItemStatus(str, Enum):
    DRAFT = "draft"
    READY = "ready"
    IN_PROGRESS = "in_progress"
    AWAITING_VERIFICATION = "awaiting_verification"
    BLOCKED = "blocked"
    COMPLETED = "completed"
    FAILED = "failed"

class AckState(str, Enum):
    PENDING = "pending"
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    TIMED_OUT = "timed_out"
```

#### 1.2 WorkItem

```python
class WorkItem(BaseModel):
    work_item_id: str           # wi_YYYYMMDD_NNN
    title: str                  # 非空
    owner: str
    status: WorkItemStatus = WorkItemStatus.DRAFT
    acceptance_criteria: list[str] = []
    artifact_refs: list[str] = []
    outcome_claim: str | None = None
    outcome_change_type: str | None = None
    outcome_producer: str | None = None
    outcome_acceptance_fingerprint: str | None = None
    blocked_reason: str | None = None
    failure_reason: str | None = None
    created_at: datetime
    updated_at: datetime
```

**Pydantic validators**：
- `work_item_id`：`@field_validator` 校验格式 `wi_\d{8}_\d{3}`
- `title`：`@field_validator` 校验非空（`strip() != ""`）
- `blocked_reason`：`@model_validator(mode='after')` — 当 status=blocked 时必须非空；当 status≠blocked 时必须为 None
- `failure_reason`：同上逻辑，绑定 status=failed

#### 1.3 HandoffContract

```python
class HandoffContract(BaseModel):
    handoff_id: str             # ho_YYYYMMDD_NNN
    work_item_id: str
    from_owner: str
    to_owner: str
    input_summary: str          # 非空
    expected_output: str        # 非空
    acceptance_criteria: list[str] = []
    ack_state: AckState = AckState.PENDING
    reject_reason: str | None = None
    created_at: datetime
```

**Pydantic validators**：
- `handoff_id`：格式校验 `ho_\d{8}_\d{3}`
- `from_owner ≠ to_owner`：`@model_validator(mode='after')`
- `input_summary` / `expected_output`：非空校验
- `reject_reason`：当 ack_state=rejected 时必须非空；否则必须为 None

#### 1.4 SignalEvent

```python
class SignalEvent(BaseModel):
    event_id: str               # ev_YYYYMMDD_NNN
    work_item_id: str
    event_type: str
    operator: str
    timestamp: datetime
    payload: dict[str, Any] = {}
```

**Pydantic validators**：
- `event_id`：格式校验 `ev_\d{8}_\d{3}`
- `event_type`：非空校验

---

### 2. machine.py

#### 2.1 核心接口

```python
class TransitionError(Exception):
    """非法状态迁移时抛出"""
    pass

class PreconditionError(Exception):
    """前置条件不满足时抛出"""
    pass

def transition_work_item(
    item: WorkItem,
    to_status: WorkItemStatus,
    *,
    operator: str,
    blocked_reason: str | None = None,
    failure_reason: str | None = None,
    evidence_paths: list[str] | None = None,
    pending_handoffs: list[HandoffContract] | None = None,
    outcome_receipt_validated: bool = False,
) -> tuple[WorkItem, SignalEvent]:
    """
    执行 work item 状态迁移。
    
    Returns: (更新后的 WorkItem, 生成的 SignalEvent)
    Raises: TransitionError（非法迁移）, PreconditionError（前置条件缺失）
    """

def transition_handoff(
    handoff: HandoffContract,
    to_state: AckState,
    *,
    operator: str,
    reject_reason: str | None = None,
) -> tuple[HandoffContract, SignalEvent]:
    """
    执行 handoff 状态迁移。
    
    Returns: (更新后的 HandoffContract, 生成的 SignalEvent)
    Raises: TransitionError, PreconditionError
    """

def apply_handoff_acceptance(
    item: WorkItem,
    handoff: HandoffContract,
) -> WorkItem:
    """
    handoff accepted 后，更新 work item 的 owner。
    """
```

#### 2.2 合法迁移表（WorkItem）

```python
VALID_TRANSITIONS: dict[WorkItemStatus, set[WorkItemStatus]] = {
    WorkItemStatus.DRAFT: {WorkItemStatus.READY},
    WorkItemStatus.READY: {WorkItemStatus.IN_PROGRESS},
    WorkItemStatus.IN_PROGRESS: {
        WorkItemStatus.BLOCKED,
        WorkItemStatus.AWAITING_VERIFICATION,
        WorkItemStatus.FAILED,
    },
    WorkItemStatus.AWAITING_VERIFICATION: {
        WorkItemStatus.COMPLETED,
        WorkItemStatus.IN_PROGRESS,
        WorkItemStatus.BLOCKED,
        WorkItemStatus.FAILED,
    },
    WorkItemStatus.BLOCKED: {WorkItemStatus.IN_PROGRESS},
    WorkItemStatus.COMPLETED: set(),
    WorkItemStatus.FAILED: set(),
}
```

#### 2.3 前置条件校验逻辑

`transition_work_item` 内部流程：

1. 检查 `(from_status, to_status)` 在 VALID_TRANSITIONS 中 → 否则 `TransitionError`
2. 按 to_status 检查前置条件：
   - `ready`：`len(item.acceptance_criteria) >= 1` → 否则 `PreconditionError`
   - `in_progress`：`item.owner != ""` → 否则 `PreconditionError`
   - `awaiting_verification`：候选 `claim / change type / producer / acceptance fingerprint` 均已冻结 → 否则 `PreconditionError`
   - `blocked`：`blocked_reason is not None and blocked_reason.strip() != ""` → 否则 `PreconditionError`
   - `completed`：仅允许从 `awaiting_verification` 进入；`outcome_receipt_validated=True`，且 `evidence_paths` 至少 1 条、无 pending handoff → 否则 `PreconditionError`
   - `failed`：`failure_reason is not None and failure_reason.strip() != ""` → 否则 `PreconditionError`
3. 从 `awaiting_verification` 返回 `in_progress` 时清除候选结果字段，防止旧回执复用。
4. 构造更新后的 WorkItem（使用 `model_copy(update=...)`）
5. 生成 SignalEvent
6. 返回 `(updated_item, event)`

#### 2.4 合法迁移表（Handoff）

```python
VALID_HANDOFF_TRANSITIONS: dict[AckState, set[AckState]] = {
    AckState.PENDING: {AckState.ACCEPTED, AckState.REJECTED, AckState.TIMED_OUT},
    AckState.ACCEPTED: set(),
    AckState.REJECTED: set(),
    AckState.TIMED_OUT: set(),
}
```

#### 2.5 事件生成

每次合法迁移生成 SignalEvent：

```python
SignalEvent(
    event_id=generate_event_id(),  # ev_YYYYMMDD_NNN
    work_item_id=item.work_item_id,
    event_type=f"work_item.{to_status.value}",  # 或 f"handoff.{to_state.value}"
    operator=operator,
    timestamp=datetime.now(tz=timezone.utc),
    payload={"from_status": from_status.value, "to_status": to_status.value, ...},
)
```

#### 2.6 ID 生成

```python
def generate_id(prefix: str) -> str:
    """生成 {prefix}_YYYYMMDD_NNN 格式的 ID（NNN 为随机 3 位数）"""
```

> v0.1 用随机 3 位数足够（冲突概率在 <100 item 场景下可忽略）。v1.0 如需要改为递增。

---

## 错误处理

| 异常 | 触发场景 | 处理方式 |
|------|---------|---------|
| `pydantic.ValidationError` | model 字段不合法 | 由 Pydantic 自动抛出，调用方捕获 |
| `TransitionError` | 非法状态迁移 | machine.py 抛出，包含 from/to 信息 |
| `PreconditionError` | 前置条件缺失 | machine.py 抛出，包含缺失条件描述 |

---

## 版本历史

| 版本 | 日期 | 变更内容 | 变更人 |
|------|------|---------|--------|
| 0.0.1 | 2026-04-02 | 初始版本 | Ken |
