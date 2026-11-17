---
name: tasks
version: 0.0.2
created: 2026-04-02T14:00:00+0800
modified: 2026-08-04T21:45:00+0800
changelog:
  - "0.0.2: task-004 纳入 awaiting_verification 与 Outcome UAT 回执门禁"
feature_name: "core-state-machine"
upstream_dependency: "features/core-state-machine/design.md"
tags: [tasks, l2-spec, core-state-machine]
---

# core-state-machine — 任务拆分

> 基于 design.md 的模块划分，拆分为 5 个可执行任务。TDD：先写测试，再写实现。

---

## 任务概述

实现 design.md 定义的两个模块：`models.py`（3 个 Pydantic models）和 `machine.py`（状态迁移逻辑）。

**总任务数**：5
**预估总工时**：8-12 小时
**执行方式**：TDD（Red-Green-Refactor）

---

## 任务拆分

| ID | 任务 | 优先级 | 预估 | 依赖 | 状态 |
|----|------|--------|------|------|------|
| task-001 | WorkItem model + validators | P0 | 2h | — | [ ] |
| task-002 | HandoffContract model + validators | P0 | 1.5h | — | [ ] |
| task-003 | SignalEvent model + ID 生成器 | P0 | 1h | — | [ ] |
| task-004 | WorkItem 状态机 | P0 | 3h | task-001, task-003 | [ ] |
| task-005 | Handoff 状态机 + owner 联动 | P0 | 2.5h | task-001, task-002, task-003 | [ ] |

---

## 任务详情

### task-001：WorkItem Model + Validators

**基于**：design.md § 1.2 WorkItem

**TDD 步骤**：

Red（测试先行）：
- 测试创建合法 WorkItem 成功
- 测试 work_item_id 格式校验（非法格式 → ValidationError）
- 测试 title 非空校验（空字符串 → ValidationError）
- 测试 blocked_reason 一致性（status=blocked 但 blocked_reason=None → ValidationError）
- 测试 failure_reason 一致性（同上逻辑）
- 测试 status 不在枚举范围 → ValidationError

Green（最小实现）：
- 实现 `WorkItemStatus` 枚举
- 实现 `WorkItem` Pydantic model + field_validator + model_validator

Refactor：清理，确保 mypy strict 通过

**产出**：
- `harness/models.py`（WorkItemStatus + WorkItem 部分）
- `tests/test_models.py`（WorkItem 测试部分）

**完成标准**：≥6 个测试用例，全部通过

---

### task-002：HandoffContract Model + Validators

**基于**：design.md § 1.3 HandoffContract

**TDD 步骤**：

Red：
- 测试创建合法 HandoffContract 成功
- 测试 handoff_id 格式校验
- 测试 from_owner ≠ to_owner 校验
- 测试 input_summary / expected_output 非空校验
- 测试 reject_reason 一致性

Green：
- 实现 `AckState` 枚举
- 实现 `HandoffContract` Pydantic model + validators

**产出**：
- `harness/models.py`（AckState + HandoffContract 部分）
- `tests/test_models.py`（HandoffContract 测试部分）

**完成标准**：≥5 个测试用例，全部通过

---

### task-003：SignalEvent Model + ID 生成器

**基于**：design.md § 1.4 SignalEvent + § 2.6 ID 生成

**TDD 步骤**：

Red：
- 测试创建合法 SignalEvent 成功
- 测试 event_id 格式校验
- 测试 event_type 非空校验
- 测试 `generate_id("wi")` 返回格式正确的 ID
- 测试 `generate_id("ho")` / `generate_id("ev")` 同上

Green：
- 实现 `SignalEvent` Pydantic model
- 实现 `generate_id(prefix)` 函数

**产出**：
- `harness/models.py`（SignalEvent + generate_id 部分）
- `tests/test_models.py`（SignalEvent + generate_id 测试部分）

**完成标准**：≥5 个测试用例，全部通过

---

### task-004：WorkItem 状态机

**基于**：design.md § 2.1 ~ 2.3（transition_work_item 接口 + 合法迁移表 + 前置条件）

**TDD 步骤**：

Red（分批写）：
1. Happy path：覆盖 `in_progress → awaiting_verification → completed`、UAT 返工及原有合法迁移
2. Bad path：≥20 条非法迁移测试
3. 前置条件缺失：
   - draft → ready 但 acceptance_criteria 为空
   - in_progress → awaiting_verification 但未冻结 claim / change type / producer / acceptance fingerprint
   - awaiting_verification → completed 但回执未通过严格校验
   - awaiting_verification → completed 但无 evidence
   - awaiting_verification → completed 但有 pending handoff
   - in_progress → blocked 但无 blocked_reason
   - in_progress → failed 但无 failure_reason
4. Event 产出：每条合法迁移检查返回的 SignalEvent 的 event_type

Green：
- 实现 `VALID_TRANSITIONS` 字典
- 实现 `transition_work_item()` 函数
- 实现 `TransitionError` / `PreconditionError`

Refactor：提取公共校验逻辑

**产出**：
- `harness/machine.py`（WorkItem 状态机部分）
- `tests/test_machine.py`（WorkItem 状态机测试）

**完成标准**：
- 覆盖所有合法迁移、非法直达 completed、候选字段清除与回执门禁
- machine.py 中 WorkItem 相关代码覆盖率 ≥95%

---

### task-005：Handoff 状态机 + Owner 联动

**基于**：design.md § 2.4（transition_handoff）+ § 2.1（apply_handoff_acceptance）

**TDD 步骤**：

Red：
1. Happy path：pending → accepted / rejected / timed_out（3 条）
2. Bad path：accepted → * / rejected → * / timed_out → *（≥6 条）
3. 前置条件：rejected 但无 reject_reason
4. Owner 联动：accepted 后调用 apply_handoff_acceptance，work item owner 更新为 to_owner
5. Event 产出：3 条合法迁移各检查 SignalEvent

Green：
- 实现 `VALID_HANDOFF_TRANSITIONS` 字典
- 实现 `transition_handoff()` 函数
- 实现 `apply_handoff_acceptance()` 函数

**产出**：
- `harness/machine.py`（Handoff 状态机 + owner 联动）
- `tests/test_machine.py`（Handoff 状态机测试）

**完成标准**：
- 3 happy + ≥6 bad + 1 前置条件 + 1 owner 联动 + 3 event = ≥14 个测试
- machine.py 中 Handoff 相关代码覆盖率 ≥95%

---

## 执行顺序

```
task-001 ──→ task-004
task-002 ──→ task-005
task-003 ──→ task-004, task-005
```

建议顺序：task-001 → task-002 → task-003 → task-004 → task-005

可并行：task-001 / task-002 / task-003 互不依赖。

---

## 完成标准（整体）

- [ ] models.py 测试覆盖率 ≥90%
- [ ] machine.py 测试覆盖率 ≥95%
- [ ] 测试总数 ≥50 个
- [ ] 所有测试通过（pytest 退出码 0）
- [ ] mypy strict 通过
- [ ] ruff 检查通过

---

## 版本历史

| 版本 | 日期 | 变更内容 | 变更人 |
|------|------|---------|--------|
| 0.0.1 | 2026-04-02 | 初始版本 | Ken |
