---
name: tasks
version: 0.0.1
created: 2026-04-02T15:25:00+0800
modified: 2026-04-02T15:25:00+0800
feature_name: "store-and-cli"
upstream_dependency: "features/store-and-cli/design.md"
tags: [tasks, l2-spec, store-and-cli]
---

# store-and-cli — 任务拆分

---

## 任务概述

实现 design.md 定义的两个模块：`store.py`（文件 CRUD）和 `cli.py`（Click 命令行）。

**总任务数**：4
**预估总工时**：8-10 小时

---

## 任务拆分

| ID | 任务 | 优先级 | 预估 | 依赖 | 状态 |
|----|------|--------|------|------|------|
| task-010 | HarnessStore 核心（work item CRUD） | P0 | 2.5h | M1 完成 | [ ] |
| task-011 | HarnessStore 扩展（handoff + events） | P0 | 2h | task-010 | [ ] |
| task-012 | CLI 核心命令（create/show/list/transition/complete） | P0 | 2.5h | task-010, task-011 | [ ] |
| task-013 | CLI 协作命令（handoff/ack/log）+ 端到端 | P0 | 2h | task-012 | [ ] |

---

## 任务详情

### task-010：HarnessStore 核心（Work Item CRUD）

**基于**：design.md § 1.1 ~ 1.2

**TDD 步骤**：

Red：
- 测试 `HarnessStore(root)` 自动创建 `.harness/` 子目录
- 测试 `save_work_item` 写入文件，路径正确
- 测试 `load_work_item` 读回相同对象
- 测试 `load_work_item` 不存在时抛 `NotFoundError`
- 测试 `list_work_items()` 返回所有 items
- 测试 `list_work_items(status=...)` 过滤正确
- 测试写入后 YAML 文件人类可读（非 binary）

Green：
- 实现 `HarnessStore.__init__` + `_ensure_dirs`
- 实现 `save_work_item` / `load_work_item` / `list_work_items`
- 实现 YAML 序列化（datetime → ISO 8601）

**产出**：`harness/store.py`（WorkItem 部分）+ `tests/test_store.py`

**完成标准**：≥7 个测试，全部通过

---

### task-011：HarnessStore 扩展（Handoff + Events）

**基于**：design.md § 1.3 ~ 1.5

**TDD 步骤**：

Red：
- 测试 `save_handoff` / `load_handoff` 往返正确
- 测试 `load_handoff` 不存在时抛 `NotFoundError`
- 测试 `list_handoffs(work_item_id)` 返回匹配的 handoffs
- 测试 `append_event` 首次创建 events 文件
- 测试 `append_event` 多次追加不覆盖旧事件
- 测试 `load_events` 返回按时间排序的事件列表
- 测试 `load_events` 对不存在的 work_item_id 返回空列表

Green：
- 实现 handoff CRUD
- 实现 `append_event`（yaml.dump 追加模式）
- 实现 `load_events`（yaml.safe_load_all）

**产出**：`harness/store.py`（handoff + events 部分）+ `tests/test_store.py`

**完成标准**：≥7 个测试，全部通过

---

### task-012：CLI 核心命令

**基于**：design.md § 2.1 ~ 2.6

**TDD 步骤**（使用 Click 的 CliRunner 测试）：

Red：
- 测试 `harness create --title "test" --owner ken` 退出码 0，输出含 id
- 测试 `harness show <id>` 输出含 title / owner / status
- 测试 `harness list` 输出含已创建的 items
- 测试 `harness list --status draft` 过滤正确
- 测试 `harness transition <id> --to ready` 成功
- 测试 `harness transition <id> --to completed` 无 evidence 时退出码 1
- 测试 `harness complete <id> --evidence <path>` 文件存在时成功
- 测试 `harness complete <id> --evidence nonexistent` 退出码 1
- 测试 `harness show <nonexistent>` 退出码 1

Green：
- 实现 `main` group + `create` / `show` / `list_items` / `transition` / `complete` 命令
- 实现统一错误处理

**产出**：`harness/cli.py`（核心命令）+ `tests/test_cli.py`

**完成标准**：≥9 个测试，全部通过

---

### task-013：CLI 协作命令 + 端到端

**基于**：design.md § 2.7 ~ 2.10

**TDD 步骤**：

Red：
- 测试 `harness handoff <id> --to aki --input "..." --output "..."` 创建 handoff
- 测试 `harness ack <handoff_id> --accept` 成功
- 测试 `harness ack <handoff_id> --reject --reason "..."` 成功
- 测试 `harness log <id>` 输出所有事件
- 端到端测试：create → transition(ready) → transition(in_progress) → handoff → ack(accept) → complete(evidence) → log 全流程

Green：
- 实现 `handoff` / `ack` / `log` 命令
- 确保 ack --accept 更新 work item owner

**产出**：`harness/cli.py`（协作命令）+ `tests/test_cli.py`

**完成标准**：≥5 个测试（含 1 个端到端全流程），全部通过

---

## 执行顺序

```
task-010 → task-011 → task-012 → task-013
```

严格串行——每个 task 依赖前一个的产出。

---

## 完成标准（整体）

- [ ] store.py 覆盖率 ≥80%
- [ ] cli.py 覆盖率 ≥70%
- [ ] 测试总数 ≥28
- [ ] `pip install -e .` 后 `harness --help` 可用
- [ ] 全流程端到端测试通过
- [ ] 所有测试通过

---

## 版本历史

| 版本 | 日期 | 变更内容 | 变更人 |
|------|------|---------|--------|
| 0.0.1 | 2026-04-02 | 初始版本 | Ken |
