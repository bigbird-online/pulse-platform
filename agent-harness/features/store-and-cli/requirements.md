---
name: requirements
version: 0.0.1
created: 2026-04-02T15:25:00+0800
modified: 2026-04-02T15:25:00+0800
feature_name: "store-and-cli"
upstream_dependency: "project-specs/project-architecture.md, features/core-state-machine/"
tags: [requirements, l2-spec, store-and-cli]
---

# store-and-cli — 功能需求

> **一句话**：让 M1 的引擎可以落盘和通过命令行操作，产出最小可用工具。

---

## 功能概述

在 M1 的 models + machine 之上，增加两层：

1. **Store**：YAML 文件读写，把 work items / handoffs / events 持久化到 `.harness/` 目录
2. **CLI**：Click 命令行入口，agent 和人通过 `harness <command>` 操作

完成后 agent-harness 成为可安装、可日常使用的 CLI 工具。

---

## 用户故事

| ID | 角色 | 故事 | 优先级 |
|----|------|------|--------|
| US-10 | Ken | 我希望 `harness create` 创建 work item 并持久化到文件，关了终端数据还在 | P0 |
| US-11 | Ken | 我希望 `harness show <id>` 查看 work item 当前状态，不用翻文件 | P0 |
| US-12 | Ken | 我希望 `harness transition <id> --to <status>` 执行状态迁移，非法操作报错 | P0 |
| US-13 | Ken | 我希望 `harness handoff <id> --to <agent>` 创建交接契约 | P0 |
| US-14 | Ken | 我希望 `harness ack <handoff_id> --accept/--reject` 记录交接响应 | P0 |
| US-15 | Ken | 我希望 `harness complete <id> --evidence <path>` 带 evidence 标记完成 | P0 |
| US-16 | Ken | 我希望 `harness log <id>` 查看该 work item 的完整事件流 | P0 |
| US-17 | Ken | 我希望 `harness list` 列出所有活跃 work items | P0 |

---

## 功能需求

### FR-10：Store — YAML 文件持久化（P0）

**描述**：文件系统 CRUD，管理 `.harness/` 目录下的 YAML 文件。

**目录结构**：
```
{root}/.harness/
├── work-items/          # 每个 work item 一个 YAML 文件
│   └── wi_20260402_001.yaml
├── handoffs/            # 每个 handoff 一个 YAML 文件
│   └── ho_20260402_001.yaml
└── events/              # 每个 work item 一个 events 文件（append-only）
    └── wi_20260402_001.events.yaml
```

**操作**：
- `save_work_item(item)` → 写入/覆盖 YAML 文件
- `load_work_item(id)` → 读取并返回 WorkItem，不存在则抛 `NotFoundError`
- `list_work_items()` → 返回所有 WorkItem 列表
- `save_handoff(handoff)` → 写入/覆盖 YAML 文件
- `load_handoff(id)` → 读取并返回 HandoffContract
- `list_handoffs(work_item_id)` → 返回关联的 handoff 列表
- `append_event(event)` → 追加到对应 work item 的 events 文件
- `load_events(work_item_id)` → 读取该 work item 的所有事件

**约束**：
- events 文件只追加不覆盖（append-only）
- root 目录通过环境变量 `HARNESS_ROOT` 或参数指定，默认当前目录

**来源**：US-10，project-architecture.md § Store

### FR-11：CLI — Click 命令行（P0）

**描述**：Click 子命令，薄壳委托给 store + machine。

**命令列表**：

| 命令 | 参数 | 行为 |
|------|------|------|
| `harness create` | `--title --owner [--criteria ...]` | 创建 draft work item，持久化，打印 id |
| `harness show <id>` | work_item_id | 加载并打印 work item 详情 |
| `harness list` | `[--status]` | 列出所有/按状态过滤的 work items |
| `harness transition <id>` | `--to <status> [--reason ...]` | 执行状态迁移，持久化，追加 event |
| `harness complete <id>` | `--evidence <path>` | 校验 evidence 文件存在且非空，迁移到 completed |
| `harness handoff <id>` | `--to <agent> --input --output [--criteria ...]` | 创建 handoff contract，持久化 |
| `harness ack <handoff_id>` | `--accept / --reject [--reason]` | 更新 handoff 状态，accepted 时更新 work item owner |
| `harness log <id>` | work_item_id | 打印该 work item 的事件流 |

**退出码**：
- 0：成功
- 1：业务错误（非法迁移、not found、evidence 缺失）
- 2：系统错误

**输出格式**：人类可读文本（不是 JSON），一屏能看完。

**来源**：US-10~17，project-architecture.md § CLI

---

## 验收标准

| ID | 标准 | 度量方式 |
|----|------|---------|
| AC-10 | 创建 work item 后文件存在于 `.harness/work-items/` | 文件存在性检查 |
| AC-11 | 重启进程后 `harness show` 能读回之前创建的 work item | 端到端测试 |
| AC-12 | `harness transition` 非法迁移返回退出码 1 + 错误信息 | CLI 集成测试 |
| AC-13 | `harness complete --evidence` 校验文件存在且非空，不存在时退出码 1 | 测试 |
| AC-14 | `harness log` 输出包含所有状态变更事件，按时间排序 | 端到端测试 |
| AC-15 | `harness ack --accept` 后 work item owner 已更新 | 端到端测试 |
| AC-16 | events 文件只追加不覆盖（多次操作后文件内事件数递增） | 文件内容检查 |
| AC-17 | `pip install -e .` 后 `harness --help` 可用 | 安装测试 |
| AC-18 | store.py 覆盖率 ≥80% | pytest --cov |
| AC-19 | cli.py 覆盖率 ≥70% | pytest --cov |

---

## 非功能需求

- **性能**：单次 CLI 命令执行时间 <500ms（含文件 IO）
- **可测试性**：store 测试使用 tmp_path fixture，不污染真实文件系统
- **错误信息**：用户友好，包含原因和建议操作（如 "Work item wi_xxx not found. Run 'harness list' to see available items."）

---

## 约束

- 技术栈：Click（CLI）、PyYAML（序列化）、pathlib（文件操作）
- store 不做缓存——每次操作读文件，写文件
- CLI 不做交互式输入——所有参数通过命令行传入

## 排除范围

- ❌ Dashboard / 汇总视图（→ v1.0）
- ❌ 自动超时检测（→ v1.0）
- ❌ Projection 生成（→ v1.0）
- ❌ JSON 输出模式（→ v1.0，如需程序化消费）

---

## 版本历史

| 版本 | 日期 | 变更内容 | 变更人 |
|------|------|---------|--------|
| 0.0.1 | 2026-04-02 | 初始版本 | Ken |
