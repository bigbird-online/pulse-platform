---
feature_name: "assign-and-flow"
version: "0.0.1"
created: "2026-04-16T14:00:00+0800"
modified: "2026-04-16T14:00:00+0800"
upstream_dependency:
  - "project-specs/project-vision.md"
  - "project-specs/project-architecture.md"
  - "project-specs/project-standards.md"
tags: [requirements, l2-spec, assign, flow, collaboration]
---

# assign-and-flow - 功能需求文档

> **功能概述**：降低 harness 协作域的使用摩擦，让管理工具的 actionable 产出（审计报告、决策记录、评估结论等）能通过一条命令流向目标 Agent，目标 Agent 通过 HEARTBEAT 自动发现并拾取任务。解决"大鸟是信息搬运瓶颈"的问题。
>
> **核心原则**：harness 是管道，不是上层工具的 adapter。提供通用的流转能力（assign + attachment + source 标记），不耦合任何特定工具的字段结构或业务逻辑。上层工具处于密集迭代期，harness 不应该知道它们的内部结构。
>
> **关联文档**：
> - L1 项目愿景：`project-specs/project-vision.md`
> - L1 项目架构：`project-specs/project-architecture.md`
> - L2 技术设计：`features/assign-and-flow/design.md`
> - L2 任务拆分：`features/assign-and-flow/tasks.md`

---

## 1. 问题陈述

### 现状

1. **协作域空转**：harness v0.1 的 WorkItem/Handoff 机制已实现并通过验证（389 tests），但生产环境中 work-items=0, handoffs=0, events=0。机制存在但无人使用。

2. **入口摩擦过高**：把一个任务交给另一个 Agent 需要 5 步 CLI + 1 次 git commit（create → transition ready → transition in_progress → handoff → git add → git commit）。而大鸟当前只需开一个 session 说一句话。

3. **消费回路未接通**：PROTOCOL.md 文档了 HEARTBEAT 应扫描 harness，但 0 个 Agent 的 HEARTBEAT.md 实际包含 harness 扫描步骤。即使创建了 handoff，目标 Agent 也不知道。

4. **大鸟是搬运瓶颈**：典型场景——Reeve 审计 Amanda 的 workspace 生成 verdict，Amanda 不知道；Amanda 修复后，Reeve 不知道。两个断裂点都依赖大鸟手动搬运。

### 目标

管理工具产出 actionable 结论后，一条命令让结论流向目标 Agent；目标 Agent 在下次 HEARTBEAT 时自动发现并拾取。大鸟从信息搬运链路中消失。

---

## 2. 用户故事（User Stories）

### 主要用户故事

**故事 1：一条命令分配任务**
- **作为**：任意 Agent（或大鸟）
- **我希望**：运行一条 `harness assign` 命令，指定目标 Agent、任务标题、输入上下文和期望产出
- **以便**：不需要手动执行 create → transition → transition → handoff 四步操作
- **优先级**：P0

**故事 2：附件跟随任务流转**
- **作为**：管理工具的产出方（如 Reeve 产出 verdict.yaml）
- **我希望**：分配任务时能附带一个文件路径（审计报告、决策记录、评估模板等），目标 Agent 打开 handoff 时能直接找到这个文件
- **以便**：目标 Agent 不需要猜测"上下文在哪"，附件就是完整上下文
- **优先级**：P0

**故事 3：标记任务来源**
- **作为**：大鸟（系统所有者）
- **我希望**：创建 WorkItem 时能标记来源（如 compliance-audit、ssot-drift、manual 等自由字符串），后续查询时能按来源过滤
- **以便**：区分"这是审计来的"还是"这是我手动派的"，理解任务的成因
- **优先级**：P0

**故事 4：管理工具产出一步到位创建任务**
- **作为**：管理工具产出方
- **我希望**：`harness compliance create-work-item` 和 `harness ssot create-work-item` 支持 `--assign-to` 选项，创建 WorkItem 的同时自动 handoff 给目标 Agent
- **以便**：审计发现问题后一步到位分配修复任务，不需要再手动 assign
- **优先级**：P0

**故事 5：HEARTBEAT 自动发现任务**
- **作为**：目标 Agent（如 Amanda）
- **我希望**：HEARTBEAT 运行时自动检查 `harness handoffs --to <自己> --state pending`，发现有 pending handoff 时提示我处理
- **以便**：不依赖大鸟来通知我有待办任务
- **优先级**：P0（文档 + 模板变更，不涉及 harness 代码）

**故事 6：按来源和目标筛选任务**
- **作为**：大鸟
- **我希望**：`harness list` 支持 `--source` 过滤（如 `harness list --source compliance-audit`），`harness handoffs` 已支持 `--to` 过滤
- **以便**：快速看到所有审计产生的未完成任务
- **优先级**：P1

---

## 3. 功能需求（Functional Requirements）

### P0 功能（MVP 必需）

**FR-P0-001：WorkItem 模型扩展 — source 字段**
- **描述**：WorkItem 新增 `source: str | None = None` 可选字段。自由字符串，harness 不做枚举约束，不据此做分支逻辑。由调用方自行填写（如 `compliance-audit`、`ssot-drift`、`performance-review`、`decision-meeting`、`manual`）。
- **来源**：故事 3
- **兼容性**：字段可选且有默认值 None，现有 YAML 文件无需迁移。

**FR-P0-002：WorkItem 模型扩展 — attachment 字段**
- **描述**：WorkItem 新增 `attachment: str | None = None` 可选字段。存储文件路径字符串，harness 不校验文件是否存在（创建时可能尚未生成，或路径为相对路径）。
- **来源**：故事 2
- **兼容性**：同上。

**FR-P0-003：HandoffContract 模型扩展 — attachment 字段**
- **描述**：HandoffContract 新增 `attachment: str | None = None` 可选字段。语义与 WorkItem.attachment 相同。
- **来源**：故事 2
- **兼容性**：同上。

**FR-P0-004：`harness assign` 命令**
- **描述**：新增 `harness assign` 顶级命令，一条命令完成 create(draft) → transition(ready) → transition(in_progress) → handoff(to target)。
- **接口**：
  ```
  harness assign
    --to TEXT              (required) 目标 Agent
    --title TEXT           (required) 任务标题
    --input TEXT           (required) 输入上下文摘要
    --output TEXT          (required) 期望产出
    --criteria TEXT        (repeatable) 验收标准
    --source TEXT          (optional) 来源标记
    --attachment TEXT      (optional) 附件路径
  ```
- **内部行为**：
  1. create WorkItem（owner=调用方 inferred 或 "harness"，source=`--source`，attachment=`--attachment`）
  2. transition draft → ready（criteria ≥ 1 由 --output 保证）
  3. transition ready → in_progress
  4. create HandoffContract（to_owner=`--to`，input_summary=`--input`，expected_output=`--output`，attachment=`--attachment`）
  5. 输出 work_item_id 和 handoff_id
- **退出码**：0=成功，1=业务错误（如 --to 与 owner 相同），2=系统错误
- **来源**：故事 1, 2

**FR-P0-005：`compliance create-work-item --assign-to` 选项**
- **描述**：现有 `harness compliance create-work-item` 命令新增 `--assign-to` 可选参数。提供时，在创建 WorkItem 后自动执行 transition(ready) → transition(in_progress) → handoff(to `--assign-to`)。HandoffContract 的 input_summary 为 verdict 路径，expected_output 为 "Fix all VIOLATED items"，attachment 为 verdict 路径。
- **来源**：故事 4
- **不提供 --assign-to 时**：行为与当前完全一致（只创建 draft WorkItem）。

**FR-P0-006：`ssot create-work-item --assign-to` 选项**
- **描述**：同 FR-P0-005，但针对 `harness ssot create-work-item`。input_summary 为 report 路径，expected_output 为 "Fix all critical/major findings"。
- **来源**：故事 4

**FR-P0-007：`harness create` 命令扩展**
- **描述**：现有 `harness create` 命令新增 `--source` 和 `--attachment` 可选参数，传递给 WorkItem 模型。
- **来源**：故事 3, 2

### P1 功能（增强功能）

**FR-P1-001：`harness list --source` 过滤**
- **描述**：现有 `harness list` 命令新增 `--source` 可选参数，按 source 字段过滤 WorkItem 列表。
- **来源**：故事 6

**FR-P1-002：`harness assign --from` 选项**
- **描述**：`harness assign` 默认 owner 为 "harness"（系统自动分配）。`--from` 可选参数允许显式指定发起方。
- **来源**：故事 1 扩展

---

## 4. 验收标准（Acceptance Criteria）

### P0 功能验收标准

**AC-P0-001：WorkItem 新字段向后兼容**
- **条件**：加载不含 source/attachment 字段的现有 YAML 文件
- **预期结果**：正常加载，source=None, attachment=None
- **验证方式**：单元测试

**AC-P0-002：WorkItem 新字段序列化**
- **条件**：创建含 source 和 attachment 的 WorkItem 并保存
- **预期结果**：YAML 中包含 source 和 attachment 字段；重新加载后字段值一致
- **验证方式**：单元测试（round-trip）

**AC-P0-003：HandoffContract 新字段**
- **条件**：同 AC-P0-001/002，针对 HandoffContract
- **预期结果**：同上
- **验证方式**：单元测试

**AC-P0-004：`harness assign` 端到端**
- **条件**：执行 `harness assign --to aki --title "Test" --input "context" --output "result" --source manual --attachment /tmp/test.md`
- **预期结果**：
  1. `.harness/work-items/` 下新增一个 YAML 文件，status=in_progress, source=manual, attachment=/tmp/test.md
  2. `.harness/handoffs/` 下新增一个 YAML 文件，to_owner=aki, attachment=/tmp/test.md
  3. `.harness/events/` 下新增事件日志（含 create + 两次 transition + handoff 共 4 条事件）
  4. stdout 输出 work_item_id 和 handoff_id
- **验证方式**：CLI 集成测试

**AC-P0-005：`harness assign` 最小参数**
- **条件**：执行 `harness assign --to aki --title "Test" --input "context" --output "result"`（不带 source/attachment/criteria）
- **预期结果**：成功创建，source=None, attachment=None, acceptance_criteria 包含 expected_output
- **验证方式**：CLI 集成测试

**AC-P0-006：`harness assign --to` 不能等于 owner**
- **条件**：执行 `harness assign --to harness --title "Test" --input "x" --output "y"`
- **预期结果**：退出码 1，错误信息明确
- **验证方式**：CLI 集成测试

**AC-P0-007：`compliance create-work-item --assign-to`**
- **条件**：提供含 VIOLATED findings 的 verdict 文件，执行 `harness compliance create-work-item verdict.yaml --owner reeve --assign-to amanda`
- **预期结果**：创建 WorkItem(status=in_progress) + HandoffContract(to_owner=amanda, attachment=verdict路径)
- **验证方式**：CLI 集成测试

**AC-P0-008：`compliance create-work-item` 无 --assign-to 不变**
- **条件**：不提供 --assign-to 参数
- **预期结果**：行为与当前完全一致（创建 draft WorkItem，无 handoff）
- **验证方式**：现有测试继续通过

**AC-P0-009：`ssot create-work-item --assign-to`**
- **条件**：同 AC-P0-007，但使用 ssot drift report
- **预期结果**：同上模式
- **验证方式**：CLI 集成测试

**AC-P0-010：`harness create --source --attachment`**
- **条件**：执行 `harness create --title "Test" --owner ken --source manual --attachment /tmp/f.md`
- **预期结果**：WorkItem 包含 source=manual, attachment=/tmp/f.md
- **验证方式**：CLI 集成测试

**AC-P0-011：现有测试全部通过**
- **条件**：运行完整测试套件
- **预期结果**：现有 389 个测试全部通过，无修改
- **验证方式**：pytest

---

## 5. 非功能需求（Non-Functional Requirements）

### 兼容性需求

- **向后兼容**：WorkItem / HandoffContract 新增字段均为 `Optional` + 默认 `None`，不破坏现有 YAML 文件的加载
- **现有测试不受影响**：389 个测试全部通过，不修改任何已有测试
- **CLI 扩展**：新增 `assign` 命令 + 扩展 `create` / `compliance create-work-item` / `ssot create-work-item` 的选项

### 性能需求

- `harness assign` 响应 <2 秒（纯文件系统操作）

### 可维护性需求

- 遵循 project-standards.md — PEP 8 + ruff + mypy strict
- 新增测试覆盖率 ≥90%
- 公开函数有 docstring（Google style）

---

## 6. 约束条件（Constraints）

### 设计约束

- **harness 是管道，不是 adapter**：source 是自由字符串（不是枚举）、attachment 是路径字符串（不校验内容）、harness 不解析 attachment 的格式或结构
- **不耦合上层工具**：harness 不知道 Malik 管理工具的存在、不知道 verdict 的结构、不知道 performance review 的状态分类。上层工具可以自由迭代而不需要 harness 改代码
- **assign 是语法糖**：内部调用的完全是现有的 create + transition + handoff 逻辑，不引入新的状态机路径

### 技术约束

- 技术栈遵循 project-architecture.md — Python 3.11+ / Pydantic v2 / Click / pytest
- 仅修改 `models.py`（加字段）和 `cli.py`（加命令/选项），不修改 machine.py / store.py / 验证域 / 治理域

### 资源约束

- Ken 主导开发
- 不依赖外部服务或 LLM

---

## 7. 排除范围（Out of Scope）

- 不修改状态机逻辑（machine.py 不变）
- 不修改存储层（store.py 不变，YAML 序列化自动适配新字段）
- 不做 HEARTBEAT.md 的自动修改（HEARTBEAT 接入是文档/模板变更，不是 harness 代码变更）
- 不做 complete 后自动反向 handoff（v1.1 考虑）
- 不做 attachment 内容校验或格式解析
- 不做 source 枚举约束或基于 source 的分支逻辑
- 不做 Web UI / Dashboard
- 不修改验证域或治理域模块

---
