# Cross-Channel Session Access — 需求文档

> **来源**：2026-06-09 合伙人例会（语道提出）
> **Owner**: Relay 主身
> **Status**: requirement captured, not yet designed
> **Created**: 2026-06-11

## 适用范围

本需求覆盖两个场景，优先级不同：

| 场景 | 优先级 | 说明 |
|------|--------|------|
| **本机（管理员/开发者）** | P0 | 语道的直接痛点：在飞书上和 agent 对话后，需要切到 CC 终端做 undo/rollback/权限提升，但找不到飞书 session |
| **多租户环境** | P1 | 租户也需要按 agent + 年月管理自己的 session，架构上必须兼容 |

语道原话（多租户确认）：

> 长征问："放到租户底下能行吗？"
> 语道答："有啥不行的？比如有租户了，你就放在租户的 session 文件夹底下。对我现在的需求，你就放在我的 `.claude/projects` 下面，或者放到我的一个租户底下也可以。"

语道建议的多租户组织方式：

> "每个 agent 肯定都是一个文件夹，文件夹最好还是分年月份——2026 年 6 月份的 session 都在这里面，7 月份再起一个新的文件夹。"

**注意**：CC 的 `~/.claude/projects/<space>/` 目录结构是固定的，不能建子目录（CC 不认）。按年月组织只能通过索引/重命名实现，不能通过物理目录结构。

---

## 问题陈述

### 现状

Relay 通过飞书 bot / TG bot 拉起的 CC session，其 JSONL 文件存储在 Relay 进程的工作目录下（由 `--session-id` 参数控制）。而用户直接在终端使用 CC 时，session 文件存储在 CC 原生的 `~/.claude/projects/<space-name>/` 目录结构中。

**两个存储位置不一致**，导致：

1. **无法跨端接续**：用户在飞书 bot 上做了 3 轮对话后，想在 CC 终端里继续同一个 session（做 undo / rollback / 权限提升等飞书端无法完成的操作），但 `claude -r <session_id>` 找不到该 session 文件。
2. **无法快速定位**：飞书 session 按 Relay 内部 ID 命名，没有日期/主题索引，用户在文件系统中无法快速找到"昨天下午在飞书上和 agent 讨论定价的那次 session"。
3. **租户管理困难**：tenant 的 session 分散在 Relay 目录，运维无法用 CC 原生工具查看/管理。

### 语道的原话

> "我的需求就是要快速找到"——快速找到并打开一个飞书/TG session，在 CC 中继续工作。

### 飞书端缺失的关键操作（只有 CC 能做）

| 操作 | 飞书 bot | CC 终端 |
|------|---------|---------|
| Undo / Rollback | ❌ 不支持 | ✅ 支持 |
| 权限提升（Y/N 确认） | ❌ 不支持 | ✅ 支持 |
| Checkpoint 回退 | ❌ 不支持 | ✅ 支持 |
| 多工具并行审批 | ❌ 不支持 | ✅ 支持 |
| Session 历史浏览 | ❌ 受限 | ✅ 完整 |

## 用户故事

**故事 1：跨端 session 接续**
- **作为**：Relay 用户（飞书/TG 端）
- **我希望**：能在 CC 终端中用 `claude -r` 打开并继续我在飞书 bot 上开始的 session
- **以便**：做飞书端无法完成的操作（undo、rollback、权限提升）
- **优先级**：P0

**故事 2：Session 快速查找**
- **作为**：Relay 用户
- **我希望**：通过日期、主题或关键词快速找到特定的飞书/TG session
- **以便**：不需要在文件系统中手动翻找 JSONL 文件
- **优先级**：P1

**故事 3：租户 session 管理**
- **作为**：Relay 运维者
- **我希望**：能查看每个 tenant 的所有 session 列表及其状态
- **以便**：排查问题、清理过期 session
- **优先级**：P2

## 需求

### FR-1：Session 文件存储统一（P0）

Relay 拉起的 CC session 的 JSONL 文件应存储在 CC 原生的 `~/.claude/projects/<space>/` 目录结构中（或等效的可被 `claude -r` 直接发现的位置），使得用户可以在 CC 终端中无缝接续。

**关键约束**：
- CC 的 `~/.claude/projects/<space-name>/` 目录结构是固定的，不能在内部添加子目录
- `--session-id` 参数控制 CC 将 session 文件存在哪里
- 需要确认：Relay 是否可以通过 `--session-id` 让 CC 将 JSONL 存到原生目录？还是 Relay 自己在 spawn 后做文件移动/软链接？

### FR-2：Session 索引（P1）

建立 session 元数据索引，包含：
- session ID
- channel（飞书/TG）
- tenant / user
- 创建时间
- 最近活跃时间
- 主题/摘要（可选，从首条消息提取）

索引可存储为：
- JSON 文件（Relay 本地）
- 或利用 CC 原生 session 元数据（如果有）

### FR-3：查找命令/接口（P1）

提供一种方式让用户快速查找 session：
- 方案 A：Relay bot 命令（如 `/sessions` 列出最近 session）
- 方案 B：CLI 工具（如 `relay sessions --recent`）
- 方案 C：Web UI
- 方案 D：利用 CC 原生 `claude --resume` 的 session 列表能力

优先级由实现成本决定，P1 阶段至少需要方案 A 或 B。

## 会议中讨论的可能方案

1. **统一存储位置**：让 Relay 的 CC session 直接存在 CC 原生目录下
2. **软链接 / 文件映射**：在 CC 原生目录创建指向 Relay session 文件的软链接
3. **索引服务**：不移动文件，建立 session ID → 文件路径的索引映射
4. **按 agent + 日期组织**：`~/.claude/projects/<agent-name>/<YYYY-MM>/` 结构

**语道的判断**：具体实现方式由开发者决定，核心诉求是"快速找到并打开"。

## 边界

**做**：
- 解决飞书/TG session 在 CC 中不可发现、不可接续的问题
- 建立 session 索引或查找能力

**不做**：
- 不改动 CC 原生的 session 存储机制（那是 Anthropic 的事）
- 不做 session 迁移（旧 session 可保持原位，新 session 走新路径即可）
- 不做跨 tenant 的 session 共享

## 与现有 session-management 的关系

现有 `requirements.md`（v0.0.1）覆盖的是：
- session-id 绑定到 tenant
- crash/idle 后的 resume 恢复
- session 状态可追踪

本文档新增的是：
- **跨端可发现性**：飞书/TG 创建的 session 能在 CC 中被找到
- **跨端可接续性**：在 CC 中打开飞书/TG session 做高级操作
- **session 索引与查找**

这些是对现有 FR-P0-001/002/003 的扩展，不冲突。

## 验收标准

**AC-1：跨端 session 接续**
- **条件**：用户在飞书 bot 上与 agent 对话 3 轮 → 在 CC 终端执行 `claude -r <session_id>` → CC 加载该 session → 用户可以继续对话并执行 undo
- **预期结果**：CC 成功加载飞书 session，对话历史完整，可执行 CC 专有操作
- **验证方式**：手动 UAT

**AC-2：Session 可查找**
- **条件**：用户通过某种接口查找"最近的飞书 session"
- **预期结果**：返回 session 列表（含 ID、时间、channel），可从中选择一个打开
- **验证方式**：手动 UAT

## 风险

| 风险 | 等级 | 说明 |
|------|------|------|
| CC `--session-id` 路径不可控 | 🟡 中 | CC 可能强制将 session 存到 `~/.claude/projects/` 下的固定结构，Relay 无法通过参数改变存储位置。需要验证。 |
| 文件移动/软链接导致 CC 行为异常 | 🟡 中 | CC 可能依赖 session 文件在特定路径，移动后可能无法正确读取或写入。 |
| 索引维护成本 | 🟢 低 | 索引本身是轻量操作，但需要处理 Relay 重启后的索引重建。 |
