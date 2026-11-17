---
name: design
version: 0.0.1
created: 2026-04-02T15:25:00+0800
modified: 2026-04-02T15:25:00+0800
feature_name: "store-and-cli"
upstream_dependency: "project-specs/project-architecture.md, features/store-and-cli/requirements.md"
tags: [design, l2-spec, store-and-cli]
---

# store-and-cli — 技术设计

---

## 技术栈

基于 project-architecture.md：

- **Click**：CLI 框架
- **PyYAML**：YAML 序列化/反序列化
- **pathlib**：文件路径操作
- **M1 的 harness.models + harness.machine**：domain layer

---

## 模块划分

```
harness/
├── models.py      ← M1（已完成）
├── machine.py     ← M1（已完成）
├── store.py       ← 本功能：文件系统 CRUD
└── cli.py         ← 本功能：Click 命令行
```

**依赖关系**：`cli.py` → `store.py` → `models.py` + `machine.py`

---

## 详细设计

### 1. store.py

#### 1.1 核心类

```python
class NotFoundError(Exception):
    """对象不存在"""
    pass

class HarnessStore:
    """文件系统 CRUD 管理器"""

    def __init__(self, root: Path | None = None):
        """
        root: .harness/ 目录路径。
        默认从 HARNESS_ROOT 环境变量读取，否则用 cwd/.harness/
        首次使用时自动创建子目录。
        """
```

#### 1.2 Work Item 操作

```python
def save_work_item(self, item: WorkItem) -> Path:
    """序列化为 YAML，写入 work-items/{id}.yaml。返回文件路径。"""

def load_work_item(self, work_item_id: str) -> WorkItem:
    """读取 YAML，反序列化为 WorkItem。不存在抛 NotFoundError。"""

def list_work_items(self, status: WorkItemStatus | None = None) -> list[WorkItem]:
    """遍历 work-items/ 目录，返回所有 WorkItem。可按 status 过滤。"""
```

#### 1.3 Handoff 操作

```python
def save_handoff(self, handoff: HandoffContract) -> Path:
    """写入 handoffs/{id}.yaml"""

def load_handoff(self, handoff_id: str) -> HandoffContract:
    """读取 handoff。不存在抛 NotFoundError。"""

def list_handoffs(self, work_item_id: str) -> list[HandoffContract]:
    """返回关联到指定 work_item_id 的所有 handoff。"""
```

#### 1.4 Event 操作

```python
def append_event(self, event: SignalEvent) -> None:
    """追加到 events/{work_item_id}.events.yaml。
    文件格式：YAML 文档流（多文档，用 --- 分隔）。
    """

def load_events(self, work_item_id: str) -> list[SignalEvent]:
    """读取该 work item 的所有事件，按 timestamp 排序。
    文件不存在返回空列表（不抛异常——新建的 item 没有 events 文件是正常的）。
    """
```

#### 1.5 YAML 序列化

- 使用 Pydantic 的 `model_dump(mode='python')` 转为 dict
- datetime 序列化为 ISO 8601 字符串
- 反序列化时用 `WorkItem(**data)` 让 Pydantic 重新校验
- events 文件使用 `yaml.dump_all()` / `yaml.safe_load_all()` 处理多文档

#### 1.6 目录初始化

```python
def _ensure_dirs(self) -> None:
    """创建 work-items/ handoffs/ events/ 子目录（如不存在）"""
```

---

### 2. cli.py

#### 2.1 入口

```python
@click.group()
@click.option('--root', type=click.Path(), envvar='HARNESS_ROOT',
              help='Path to .harness directory')
@click.pass_context
def main(ctx, root):
    """agent-harness: track cross-agent coordination state"""
    ctx.ensure_object(dict)
    ctx.obj['store'] = HarnessStore(Path(root) if root else None)
```

#### 2.2 命令：create

```python
@main.command()
@click.option('--title', required=True)
@click.option('--owner', required=True)
@click.option('--criteria', multiple=True, help='Acceptance criteria (repeatable)')
@click.pass_context
def create(ctx, title, owner, criteria):
    """Create a new work item in draft status"""
    # 1. generate_id("wi")
    # 2. WorkItem(...)
    # 3. store.save_work_item()
    # 4. store.append_event() — work_item.created
    # 5. click.echo(f"Created {id}")
```

#### 2.3 命令：show

```python
@main.command()
@click.argument('work_item_id')
@click.pass_context
def show(ctx, work_item_id):
    """Show work item details"""
    # 加载 item，格式化输出：id, title, owner, status, criteria, artifacts
    # 加载关联 handoffs，显示 pending/accepted 状态
```

#### 2.4 命令：list

```python
@main.command('list')
@click.option('--status', type=click.Choice([s.value for s in WorkItemStatus]))
@click.pass_context
def list_items(ctx, status):
    """List work items"""
    # 表格输出：id | title | owner | status
```

#### 2.5 命令：transition

```python
@main.command()
@click.argument('work_item_id')
@click.option('--to', 'to_status', required=True,
              type=click.Choice([s.value for s in WorkItemStatus]))
@click.option('--reason', default=None, help='Blocked/failure reason')
@click.pass_context
def transition(ctx, work_item_id, to_status, reason):
    """Transition work item to a new status"""
    # 1. load work item
    # 2. load pending handoffs (for complete check)
    # 3. transition_work_item()
    # 4. save + append event
    # 捕获 TransitionError/PreconditionError → click.echo + sys.exit(1)
```

#### 2.6 命令：complete

```python
@main.command()
@click.argument('work_item_id')
@click.option('--evidence', required=True, type=click.Path(),
              help='Path to evidence file')
@click.pass_context
def complete(ctx, work_item_id, evidence):
    """Complete a work item with evidence"""
    # 1. 校验 evidence 文件存在且非空
    # 2. load work item
    # 3. 将 evidence 加入 artifact_refs
    # 4. transition_work_item(to=completed, evidence_paths=[evidence])
    # 5. save + append event
```

#### 2.7 命令：handoff

```python
@main.command()
@click.argument('work_item_id')
@click.option('--to', 'to_agent', required=True)
@click.option('--input', 'input_summary', required=True)
@click.option('--output', 'expected_output', required=True)
@click.option('--criteria', multiple=True)
@click.pass_context
def handoff(ctx, work_item_id, to_agent, input_summary, expected_output, criteria):
    """Create a handoff contract"""
    # 1. load work item (verify exists)
    # 2. create HandoffContract
    # 3. save handoff + append event
```

#### 2.8 命令：ack

```python
@main.command()
@click.argument('handoff_id')
@click.option('--accept', 'action', flag_value='accept')
@click.option('--reject', 'action', flag_value='reject')
@click.option('--reason', default=None)
@click.pass_context
def ack(ctx, handoff_id, action, reason):
    """Accept or reject a handoff"""
    # 1. load handoff
    # 2. transition_handoff()
    # 3. if accepted: apply_handoff_acceptance() on work item, save work item
    # 4. save handoff + append event
```

#### 2.9 命令：log

```python
@main.command()
@click.argument('work_item_id')
@click.pass_context
def log(ctx, work_item_id):
    """Show event log for a work item"""
    # load_events → 按时间排序 → 逐条打印 timestamp | event_type | operator | payload摘要
```

#### 2.10 错误处理

所有命令统一捕获：
- `NotFoundError` → `click.echo("Error: ...")` + `sys.exit(1)`
- `TransitionError` → `click.echo("Error: ...")` + `sys.exit(1)`
- `PreconditionError` → `click.echo("Error: ...")` + `sys.exit(1)`
- `ValidationError` → `click.echo("Error: ...")` + `sys.exit(1)`

---

## 版本历史

| 版本 | 日期 | 变更内容 | 变更人 |
|------|------|---------|--------|
| 0.0.1 | 2026-04-02 | 初始版本 | Ken |
