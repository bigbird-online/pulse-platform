---
feature_name: "assign-and-flow"
version: "0.0.1"
created: "2026-04-16T14:00:00+0800"
modified: "2026-04-16T14:00:00+0800"
upstream_dependency:
  - "features/assign-and-flow/design.md"
  - "features/assign-and-flow/requirements.md"
tags: [tasks, l2-spec, tdd, assign, flow]
---

# assign-and-flow - 任务拆分文档

> **任务概述**：基于 design.md 实现模型扩展 + assign 命令 + --assign-to 选项，降低协作域使用摩擦。
>
> **关联文档**：
> - L2 技术设计：`features/assign-and-flow/design.md`
> - L2 功能需求：`features/assign-and-flow/requirements.md`
> - TDD 测试文件：`tests/test_models.py`（扩展）、`tests/test_cli.py`（扩展）、`tests/test_cli_guardrail.py`（扩展）、`tests/test_cli_ssot.py`（扩展）

---

## 1. 任务拆分（Task Breakdown）

### P0 任务（MVP 必需）

| 任务 ID | 任务名称 | 预估工时 | 依赖任务 | 状态 |
|---------|---------|---------|---------|------|
| P0-001 | WorkItem 模型扩展（source + attachment） | 30min | 无 | 待开始 |
| P0-002 | HandoffContract 模型扩展（attachment） | 20min | 无 | 待开始 |
| P0-003 | 模型单元测试 | 40min | P0-001, P0-002 | 待开始 |
| P0-004 | `_assign_work_item` 内部 helper | 40min | P0-001, P0-002 | 待开始 |
| P0-005 | `harness assign` 命令 | 40min | P0-004 | 待开始 |
| P0-006 | `harness create` 扩展（--source, --attachment） | 20min | P0-001 | 待开始 |
| P0-007 | `compliance create-work-item --assign-to` | 30min | P0-004 | 待开始 |
| P0-008 | `ssot create-work-item --assign-to` | 30min | P0-004 | 待开始 |
| P0-009 | CLI 集成测试 | 60min | P0-005 ~ P0-008 | 待开始 |
| P0-010 | 回归验证 + 端到端真实场景 | 30min | P0-009 | 待开始 |

### P1 任务（增强功能）

| 任务 ID | 任务名称 | 预估工时 | 依赖任务 | 状态 |
|---------|---------|---------|---------|------|
| P1-001 | `harness list --source` 过滤 | 20min | P0-001 | 待开始 |
| P1-002 | `harness assign --from` 选项 | 15min | P0-005 | 待开始 |

---

## 2. 任务详情（Task Details）

### P0-001：WorkItem 模型扩展

**任务描述**：
- 在 `harness/models.py` 的 `WorkItem` 类中新增两个字段：
  ```python
  source: str | None = None
  attachment: str | None = None
  ```
- 不加 validator（自由字符串，不做约束）
- 不修改任何现有字段或 validator

**输入**：design.md Model-001

**产出**：`harness/models.py` 变更

**验收**：现有 test_models.py 全部通过 + mypy clean

---

### P0-002：HandoffContract 模型扩展

**任务描述**：
- 在 `harness/models.py` 的 `HandoffContract` 类中新增一个字段：
  ```python
  attachment: str | None = None
  ```
- 不修改任何现有字段或 validator

**输入**：design.md Model-002

**产出**：`harness/models.py` 变更

**验收**：现有 test_models.py 全部通过 + mypy clean

---

### P0-003：模型单元测试

**任务描述**：
- 在 `tests/test_models.py` 中新增测试：

**WorkItem 测试**：
- [ ] **Test-001**：WorkItem 新字段默认值 — 不传 source/attachment → 均为 None
- [ ] **Test-002**：WorkItem source 赋值 — source="compliance-audit" → 正确存储
- [ ] **Test-003**：WorkItem attachment 赋值 — attachment="/path/to/file" → 正确存储
- [ ] **Test-004**：WorkItem 向后兼容 — 从不含 source/attachment 的 dict 构造 → 正常，字段为 None

**HandoffContract 测试**：
- [ ] **Test-005**：HandoffContract 新字段默认值 — 不传 attachment → None
- [ ] **Test-006**：HandoffContract attachment 赋值 — attachment="/path/to/verdict.yaml" → 正确存储

**产出**：`tests/test_models.py` 扩展

---

### P0-004：`_assign_work_item` 内部 helper

**任务描述**：
- 在 `harness/cli.py` 中新增内部函数：
  ```python
  def _assign_work_item(
      store: HarnessStore,
      item: WorkItem,
      to_owner: str,
      input_summary: str,
      expected_output: str,
      attachment: str | None = None,
  ) -> tuple[WorkItem, HandoffContract]:
  ```
- 接收一个已创建（draft 状态）的 WorkItem
- 执行：transition(ready) → transition(in_progress) → create HandoffContract
- 记录所有 events（2 次 transition + 1 次 handoff.created）
- 返回更新后的 WorkItem 和创建的 HandoffContract

**输入**：design.md Helper-001

**产出**：`harness/cli.py` 变更

**验收**：被 P0-005/P0-007/P0-008 调用时行为正确

---

### P0-005：`harness assign` 命令

**任务描述**：
- 在 `harness/cli.py` 中新增 `assign` 顶级命令
- 接口遵循 design.md CMD-001
- 内部流程：
  1. 创建 WorkItem（owner="harness"，source 和 attachment 从参数传入）
  2. 如无 --criteria，自动将 --output 作为唯一 criterion（保证 ready transition 前置条件）
  3. 调用 `_assign_work_item` 完成 transition + handoff
  4. 输出 `Assigned {wi_id} → {to} (handoff {ho_id})`
- 退出码：0/1/2

**输入**：design.md CMD-001

**产出**：`harness/cli.py` 变更

---

### P0-006：`harness create` 扩展

**任务描述**：
- 给现有 `create` 命令新增两个 Click option：
  ```python
  @click.option("--source", default=None, help="Source tag (free string)")
  @click.option("--attachment", default=None, help="Attachment file path")
  ```
- 传递给 WorkItem 构造函数
- 不改变其他行为

**输入**：design.md CMD-002

**产出**：`harness/cli.py` 变更

---

### P0-007：`compliance create-work-item --assign-to`

**任务描述**：
- 给现有 `compliance create-work-item` 命令新增 Click option：
  ```python
  @click.option("--assign-to", default=None, help="Auto-assign to this agent")
  ```
- 当提供时：
  1. 创建 WorkItem 时设置 source="compliance-audit"，attachment=verdict_path
  2. 调用 `_assign_work_item(store, item, assign_to, verdict_path, "Fix all VIOLATED items", verdict_path)`
  3. 输出包含 handoff_id
- 不提供时：行为完全不变

**输入**：design.md CMD-003

**产出**：`harness/cli.py` 变更

---

### P0-008：`ssot create-work-item --assign-to`

**任务描述**：
- 同 P0-007 模式，针对 `ssot create-work-item`
- source="ssot-drift"，expected_output="Fix all critical/major findings"

**输入**：design.md CMD-004

**产出**：`harness/cli.py` 变更

---

### P0-009：CLI 集成测试

**任务描述**：

**assign 命令测试**（扩展 `tests/test_cli.py`）：
- [ ] **Test-007**：assign 端到端 — 执行 assign，验证 work-items/ 有文件（status=in_progress, source, attachment）、handoffs/ 有文件（to_owner, attachment）、events/ 有 4 条事件
- [ ] **Test-008**：assign 最小参数 — 不带 source/attachment/criteria，成功创建
- [ ] **Test-009**：assign 自分配拒绝 — --to harness → 退出码 1
- [ ] **Test-010**：assign 输出格式 — stdout 包含 wi_id 和 ho_id

**create 扩展测试**（扩展 `tests/test_cli.py`）：
- [ ] **Test-011**：create --source --attachment — WorkItem YAML 包含字段
- [ ] **Test-012**：create 无新参数 — 行为不变（回归）

**compliance --assign-to 测试**（扩展 `tests/test_cli_guardrail.py`）：
- [ ] **Test-013**：compliance create-work-item --assign-to — 创建 WI + HO
- [ ] **Test-014**：compliance create-work-item 无 --assign-to — 行为不变（回归）
- [ ] **Test-015**：compliance create-work-item --assign-to 无 VIOLATED — 无 WI 创建

**ssot --assign-to 测试**（扩展 `tests/test_cli_ssot.py`）：
- [ ] **Test-016**：ssot create-work-item --assign-to — 创建 WI + HO
- [ ] **Test-017**：ssot create-work-item 无 --assign-to — 行为不变（回归）
- [ ] **Test-018**：ssot create-work-item --assign-to 无 findings — 无 WI 创建

**产出**：test_cli.py / test_cli_guardrail.py / test_cli_ssot.py 扩展

---

### P0-010：回归验证 + 端到端真实场景

**任务描述**：
1. 运行完整测试套件（389 现有 + ~20 新增），确保 0 失败
2. 在真实环境执行一条端到端链路：
   ```bash
   # 模拟 Reeve 审计 → assign 给 Amanda
   harness assign --to amanda \
     --title "Compliance audit findings" \
     --input "See verdict at .harness/reeve-space/memo/receipts/2026-04-08-emi-002.verdict.yaml" \
     --output "Fix all VIOLATED items" \
     --source compliance-audit \
     --attachment .harness/reeve-space/memo/receipts/2026-04-08-emi-002.verdict.yaml

   # 验证
   harness show <wi_id>
   harness handoffs --to amanda --state pending
   ```

**产出**：全测试通过 + 真实 WorkItem + HandoffContract 存在

---

## 3. 执行顺序

```
P0-001 + P0-002（并行：模型扩展）
       ↓
P0-003（模型测试）
       ↓
P0-004（helper 函数）
       ↓
P0-005 + P0-006 + P0-007 + P0-008（并行：4 个 CLI 变更）
       ↓
P0-009（CLI 测试）
       ↓
P0-010（回归 + 真实场景）
```

预估总工时：~5.5h（含测试）

---
