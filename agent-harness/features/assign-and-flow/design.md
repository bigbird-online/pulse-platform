---
feature_name: "assign-and-flow"
version: "0.0.1"
created: "2026-04-16T14:00:00+0800"
modified: "2026-04-16T14:00:00+0800"
upstream_dependency:
  - "project-specs/project-architecture.md"
  - "features/assign-and-flow/requirements.md"
tags: [design, l2-spec, assign, flow, collaboration]
---

# assign-and-flow - 技术设计文档

> **设计概述**：在协作域模型上新增两个通用字段（source, attachment），新增 `assign` 复合命令和 `--assign-to` 选项，将 4 步操作压缩为 1 步。不修改状态机逻辑和存储层。
>
> **关联文档**：
> - L1 项目架构：`project-specs/project-architecture.md`
> - L2 功能需求：`features/assign-and-flow/requirements.md`
> - L2 任务拆分：`features/assign-and-flow/tasks.md`

---

## 1. 技术栈（Tech Stack）

### 技术栈引用

**基于 L1 项目架构**：`project-specs/project-architecture.md`

- **编程语言**：Python 3.11+（遵循 L1 定义）
- **数据模型**：Pydantic v2（遵循 L1 定义）
- **序列化**：PyYAML（遵循 L1 定义）
- **CLI 框架**：Click（遵循 L1 定义）
- **测试**：pytest + pytest-cov（遵循 L1 定义）

### 技术选型说明

- 无新增依赖。全部变更在现有 models.py + cli.py 内完成。
- 不新增模块文件。assign 逻辑作为 CLI 层的复合命令，调用现有 machine.py 函数。

---

## 2. 架构设计（Architecture Design）

### 变更范围

```
harness/
├── models.py            # 变更：WorkItem + HandoffContract 各加 2/1 个 Optional 字段
├── machine.py           # 不变
├── store.py             # 不变（Pydantic model_dump → YAML 自动适配新字段）
├── cli.py               # 变更：新增 assign 命令 + 扩展 create / compliance / ssot 选项
├── guardrail.py         # 不变
├── gate_engine.py       # 不变
├── compliance_engine.py # 不变
├── extractor.py         # 不变
├── receipt.py           # 不变
├── ssot.py              # 不变
└── ssot_engine.py       # 不变
```

### 模块关系（变更部分）

```
cli.py (assign command)
  ├── models.py       — WorkItem(source, attachment) + HandoffContract(attachment)
  ├── machine.py      — transition_work_item() × 2 + 现有 handoff 创建逻辑
  └── store.py        — save_work_item() + append_event() + save_handoff()
```

assign 命令不引入新的状态机路径。它编排现有的原子操作：

```
assign(to, title, input, output, ...)
  │
  ├─ 1. WorkItem(status=draft, source=?, attachment=?)  → store.save_work_item
  ├─ 2. transition_work_item(draft → ready)             → store.save + append_event
  ├─ 3. transition_work_item(ready → in_progress)       → store.save + append_event
  └─ 4. HandoffContract(to_owner=to, attachment=?)      → store.save_handoff + append_event
```

---

## 3. 数据模型（Data Model Changes）

### Model-001：WorkItem 扩展

```python
# models.py — WorkItem 新增字段
class WorkItem(BaseModel):
    # ... 现有字段不变 ...
    source: str | None = None        # 新增：来源标记（自由字符串）
    attachment: str | None = None    # 新增：附件路径（自由字符串）
```

**设计决策**：
- `source` 不是枚举。harness 不做分支逻辑，不根据 source 值改变行为。上层工具自由填写。
- `attachment` 不校验文件存在性。原因：(1) 文件可能是相对路径；(2) 创建时文件可能尚未生成；(3) harness 不解析附件内容。
- 两个字段均为 `Optional[str]` + 默认 `None`，不破坏现有 YAML 文件的反序列化。

### Model-002：HandoffContract 扩展

```python
# models.py — HandoffContract 新增字段
class HandoffContract(BaseModel):
    # ... 现有字段不变 ...
    attachment: str | None = None    # 新增：附件路径（自由字符串）
```

**设计决策**：
- HandoffContract 不加 source（source 是 WorkItem 级概念，一个 WorkItem 可能产生多个 Handoff，来源只需标记一次）。

### YAML 向后兼容性

Pydantic v2 的 `Optional[str] = None` 字段在反序列化时自动处理缺失 key → None。现有 YAML 文件无需迁移。序列化时新字段会出现在输出中（值为 null 或实际值），不影响现有工具读取。

---

## 4. CLI 设计（Command Interface）

### CMD-001：`harness assign`（新命令）

```
harness assign
  --to TEXT              (required)   目标 Agent
  --title TEXT           (required)   任务标题
  --input TEXT           (required)   输入上下文摘要
  --output TEXT          (required)   期望产出
  --criteria TEXT        (repeatable) 额外验收标准
  --source TEXT          (optional)   来源标记
  --attachment TEXT      (optional)   附件路径
```

**内部流程**：

```python
def assign(to, title, input_summary, expected_output, criteria, source, attachment):
    store = ctx.obj["store"]
    now = datetime.now(tz=timezone.utc)
    owner = "harness"  # 系统发起

    # 1. Create WorkItem (draft)
    wi_id = generate_id("wi")
    all_criteria = list(criteria) if criteria else []
    if not all_criteria:
        all_criteria = [expected_output]  # 保证 ≥1 criterion for ready transition

    item = WorkItem(
        work_item_id=wi_id,
        title=title,
        owner=owner,
        acceptance_criteria=all_criteria,
        source=source,
        attachment=attachment,
        created_at=now,
        updated_at=now,
    )
    store.save_work_item(item)
    store.append_event(create_event)

    # 2. Transition draft → ready
    item, event = transition_work_item(item, READY, operator=owner)
    store.save_work_item(item)
    store.append_event(event)

    # 3. Transition ready → in_progress
    item, event = transition_work_item(item, IN_PROGRESS, operator=owner)
    store.save_work_item(item)
    store.append_event(event)

    # 4. Create HandoffContract
    ho_id = generate_id("ho")
    handoff = HandoffContract(
        handoff_id=ho_id,
        work_item_id=wi_id,
        from_owner=owner,
        to_owner=to,
        input_summary=input_summary,
        expected_output=expected_output,
        attachment=attachment,
        created_at=now,
    )
    store.save_handoff(handoff)
    store.append_event(handoff_event)

    click.echo(f"Assigned {wi_id} → {to} (handoff {ho_id})")
```

**错误处理**：
- `--to` 等于 owner ("harness") → 退出码 1（from_owner == to_owner 由 HandoffContract validator 拦截）
- ValidationError / TransitionError / PreconditionError → 退出码 1
- 文件系统错误 → 退出码 2

**退出码**：0 成功 / 1 业务错误 / 2 系统错误

### CMD-002：`harness create` 扩展

```
harness create
  --title TEXT           (required, 现有)
  --owner TEXT           (required, 现有)
  --criteria TEXT        (repeatable, 现有)
  --source TEXT          (optional, 新增)
  --attachment TEXT      (optional, 新增)
```

仅将 source 和 attachment 传递给 WorkItem 构造函数。其余逻辑不变。

### CMD-003：`compliance create-work-item --assign-to`

```
harness compliance create-work-item VERDICT_PATH
  --owner TEXT           (required, 现有)
  --assign-to TEXT       (optional, 新增)
```

当 `--assign-to` 提供时：
1. 创建 WorkItem（现有逻辑，source="compliance-audit"，attachment=verdict_path）
2. transition draft → ready（criteria 已由 verdict 提取保证 ≥1）
3. transition ready → in_progress
4. create HandoffContract（to_owner=assign_to，input_summary=verdict_path，expected_output="Fix all VIOLATED items"，attachment=verdict_path）

不提供 `--assign-to` 时：行为与当前完全一致。

### CMD-004：`ssot create-work-item --assign-to`

同 CMD-003 模式，source="ssot-drift"，expected_output="Fix all critical/major findings"。

---

## 5. 内部辅助函数

### Helper-001：`_assign_work_item`

为避免 assign / compliance --assign-to / ssot --assign-to 三处重复 transition + handoff 逻辑，抽取一个内部 helper：

```python
def _assign_work_item(
    store: HarnessStore,
    item: WorkItem,
    to_owner: str,
    input_summary: str,
    expected_output: str,
    attachment: str | None = None,
) -> tuple[WorkItem, HandoffContract]:
    """Transition a draft WorkItem to in_progress and create a HandoffContract.

    Executes: draft → ready → in_progress → handoff.
    Caller is responsible for initial WorkItem creation and event logging.
    """
```

这个函数在 cli.py 内部定义，不暴露为公开 API。三个调用点（assign / compliance / ssot）共用此逻辑。

---

## 6. 架构权衡

### TD-01：assign 是 CLI 语法糖 vs 新状态机路径

- **选择**：CLI 语法糖——assign 内部调用现有的 transition_work_item × 2 + HandoffContract 创建
- **获得**：不修改 machine.py，状态机规则不变，不增加测试负担
- **牺牲**：assign 内部的 4 步不是原子操作（中间步骤失败会留下半成品）
- **为什么值得**：work item <100，失败概率极低（纯文件写入）；半成品可通过手动 transition 修复。引入原子操作需要事务机制，复杂度不值得。

### TD-02：source 自由字符串 vs 枚举

- **选择**：自由字符串
- **获得**：上层工具可以自由迭代（新增来源不需要改 harness 代码）
- **牺牲**：无法做类型安全检查，可能出现拼写不一致
- **为什么值得**：上层 Malik 管理工具处于密集迭代期。约定优于约束——文档化推荐值即可。

### TD-03：attachment 不校验文件存在

- **选择**：不校验
- **获得**：支持相对路径、延迟生成的文件、非本机路径
- **牺牲**：可能存储无效路径
- **为什么值得**：harness 是管道，不是文件管理器。校验责任在调用方。

### TD-04：_assign_work_item helper vs 直接复制

- **选择**：抽取 helper（3 调用点共用）
- **获得**：逻辑单点维护
- **牺牲**：多一层间接
- **为什么值得**：assign / compliance --assign-to / ssot --assign-to 的 transition+handoff 逻辑完全相同，不抽取就是三份拷贝。三处调用正好是抽取的甜蜜点。

---

## 7. 测试策略

### 测试范围

| 层级 | 测试内容 | 文件 |
|------|---------|------|
| 单元 | WorkItem 新字段序列化/反序列化/默认值 | test_models.py（扩展） |
| 单元 | HandoffContract 新字段 | test_models.py（扩展） |
| 集成 | `harness assign` 端到端 | test_cli.py（扩展） |
| 集成 | `harness create --source --attachment` | test_cli.py（扩展） |
| 集成 | `compliance create-work-item --assign-to` | test_cli_guardrail.py（扩展） |
| 集成 | `ssot create-work-item --assign-to` | test_cli_ssot.py（扩展） |
| 回归 | 现有 389 个测试全部通过 | 全部测试文件 |

### 预估新增测试数量

| 类别 | 数量 |
|------|------|
| models 单元测试 | ~6（source/attachment 字段 × round-trip/default/backward-compat） |
| assign 集成测试 | ~6（端到端/最小参数/自分配拒绝/source+attachment/错误路径） |
| create 扩展测试 | ~2（--source/--attachment） |
| compliance --assign-to 测试 | ~3（有 assign/无 assign/无 VIOLATED） |
| ssot --assign-to 测试 | ~3（有 assign/无 assign/无 findings） |
| **合计** | **~20** |

---
