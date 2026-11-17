---
feature_name: "multi-node-session-continuity"
version: "1.0.0"
created: "2026-05-27T14:30:00+0800"
modified: "2026-05-27T16:00:00+0800"
upstream_dependency: ["cc-process-pool", "session-resilience", "feishu-channel"]
tags: [requirements, l2-spec, multi-node, session, continuity, relay]
changelog:
  - "v1.0.0: 经完整讨论后定稿。核心变更：(1) 仅适用于国外节点（Claude 账号有封号风险）；(2) 冷迁移方案取代热迁移；(3) 直接复用 CC JSONL 提取最后上下文窗口，不另建行动日志系统；(4) 每个 turn 后 auto git push 实现工作区持久化"
---

# multi-node-session-continuity - 功能需求文档

> **功能概述**：为使用国外模型（Claude）的国外节点集群提供冷迁移能力。节点账号被封时，将租户迁移到其他节点，通过 CC JSONL 最后上下文窗口恢复对话上下文。
>
> **关联文档**：
> - CC 进程池：`features/cc-process-pool/requirements.md`
> - 会话弹性：`features/session-resilience/requirements.md`
> - 飞书通道：`features/feishu-channel/requirements.md`
> - L2 技术设计：`features/multi-node-session-continuity/design.md`
> - L2 任务拆分：`features/multi-node-session-continuity/tasks.md`

---

## 1. 背景与约束

### 1.1 适用范围

**本 feature 仅适用于国外节点（使用 Claude 等国外模型）**。

| 节点类型 | 模型 | 封号风险 | 本 feature 适用 |
|---------|------|---------|---------------|
| 国外节点（美国/尼日利亚/日本等） | Claude Pro | **有** | ✅ 适用 |


### 1.2 架构约束（硬约束）

以下约束来自 2026-05-26 合伙人例会的讨论结论：

| 编号 | 约束 | 来源 | 说明 |
|------|------|------|------|
| HC-1 | **Relay + CC 必须同节点** | 语道 | Relay 通过 `claude -p` / `claude --bare` 调用 CC，必须是本机进程。跨节点调 API 会被模型厂商封禁 |
| HC-2 | **合法性调用** | 语道 | 禁止透传 API（中转站模式）。每个节点必须跑完整的 CC 进程 |
| HC-3 | **节点 = 账号（1:1 绑定）** | 长征 | 一个国外节点只绑一个 Claude 账号。同节点切多账号 = 行为异常 = 更容易被封 |
| HC-4 | **冷迁移** | 长征 | 不追求热迁移/无缝切换。账号被封时通知租户等待，冷迁移到其他节点 |

### 1.3 技术事实

| 事实 | 数据 | 影响 |
|------|------|------|
| CC JSONL 存储位置 | `~/.claude/projects/<path-hash>/<session-id>.jsonl` | 不在工作区内，但节点活着时可读取 |
| CC 上下文窗口 | ~1M tokens | JSONL 可能包含多个上下文窗口（累计 5MB+） |
| CC 上下文溢出时 | 自动生成 "continued from a previous conversation" 摘要（6~10KB） | 可用于定位最后一个有效上下文窗口 |
| CC 不锁 JSONL 文件 | 可并发读取 | Relay 可在 CC 写入时同步读取 |
| 最后上下文窗口典型大小 | 50~200KB（实测 BestYearly: 50KB） | 注入新会话无压力，远小于 1M token 上限 |
| CC 需要 本地 文件系统 | Read/Write/Edit/Bash 全部操作本地文件 | CC 不可能直接在 GitHub 上运行，必须 git clone 到本地 |

---

## 2. 用户故事

**故事1：账号被封时冷迁移**
- **作为**：使用 Claude 的 PULSE 租户
- **我希望**：当我的 Agent 所在节点的 Claude 账号被封时，能收到通知并迁移到其他节点继续对话
- **以便**：虽然需要等待一会儿，但工作进度不丢失
- **优先级**：P0

**故事2：迁移后上下文恢复**
- **作为**：刚经历冷迁移的 PULSE 租户
- **我希望**：迁移后的 Agent 知道之前在做什么
- **以便**：不需要重新描述上下文就能继续工作
- **优先级**：P0

**故事3：对话历史不丢失**
- **作为**：PULSE 租户
- **我希望**：飞书聊天记录本身就是完整的对话历史
- **以便**：随时翻看之前的讨论
- **优先级**：P1（已由飞书本身满足，无需额外开发）

---

## 3. 核心设计

### 3.1 整体架构

```
┌──────────────────────────────────────────────────────────┐
│  国外节点集群（本 feature 范围）                           │
│                                                          │
│  Node US-1 (claude-pro@account-1) ← 1 节点 1 账号       │
│  Node NG-1 (claude-pro@account-2)                        │
│  Node JP-1 (claude-pro@account-3)                        │
│                                                          │
│  租户开通时：根据负载分配到一个节点                        │
│  账号被封时：冷迁移到其他节点                             │
│  节点宕机时：手动迁移（Level 2）                          │
└──────────────────────────────────────────────────────────┘
```

### 3.2 租户 GitHub 仓库

每个国外节点的租户拥有一个专属 GitHub 私有仓库。CC 的工作区就是这个 repo 的 clone：

```
tenant-{tenant-id}/                    ← 租户 GitHub 私有仓库
├── CLAUDE.md                          ← Agent 身份/规则
├── MEMORY.md                          ← Agent 长期记忆
├── memory/                            ← 每日日志
├── TOOLS.md                           ← 工具链
├── ...                                ← Agent 其他工作文件
└── .relay/
    └── last-context.md                ← 冷迁移时注入的上下文（由旧节点写入）
```

**注意**：JSONL 归档不放入此仓库。原因：
- JSONL 体积大（单文件 1~5MB，累积可达数百 MB）
- git clone 时间会随仓库体积膨胀
- 飞书聊天记录本身就是完整的对话历史，无需额外归档
- 对话回溯通过飞书完成，不依赖 JSONL

### 3.3 工作区持久化：每 turn 后 auto git push

```
CC 完成一个 turn（finishTurn 事件）
  ↓
Relay 在工作区目录执行:
  git add -A
  git commit -m "auto: turn {n}" 
  git push
  ↓
即使节点马上崩溃，workspace 文件已持久化到 GitHub
```

**失败处理**：git push 失败不阻断服务，记录告警日志，下次 turn 再试。本地文件不丢失。

### 3.4 冷迁移流程

```
                    故障节点                              接管节点
                 ┌──────────┐                       ┌──────────┐
                 │ Node A   │                       │ Node B   │
                 │ CC 运行中 │                       │          │
                 └─────┬────┘                       └──────────┘
                       │ 检测到 Claude 账号被封
                       │ (持久 401/403)
                       ↓
                 ① 飞书通知租户: "服务迁移中，预计 1-3 分钟"
                 ② 读取 CC session JSONL
                 ③ 提取最后一个完整上下文窗口:
                    - 找到最后一个 "continued from..." 消息
                    - 取其后所有内容（如无 summary，取整个 JSONL）
                 ④ 格式化为 .relay/last-context.md
                 ⑤ git add + commit + push
                 ⑥ 断开飞书 Bot WS
                 
                                                       ↓
                                              ⑦ 管理员分配租户到 Node B
                                              ⑧ git clone tenant repo
                                              ⑨ 读取 .relay/last-context.md
                                              ⑩ 启动飞书 Bot WS
                                              ⑪ 启动 CC，第一条消息注入 last-context
                                              ⑫ 飞书通知租户: "服务已恢复"
```

### 3.5 JSONL 最后上下文窗口提取

```python
def extract_last_context_window(jsonl_path):
    """
    从 CC session JSONL 中提取最后一个有效上下文窗口。
    
    CC 在上下文溢出时会插入 "continued from a previous conversation" 消息。
    最后一个此类消息之后的所有内容 = 最后一个完整上下文窗口。
    如果没有此类消息 = 整个 JSONL 就是最后一个上下文窗口。
    """
    lines = read_all_lines(jsonl_path)
    last_summary_idx = -1
    
    for i, line in enumerate(lines):
        obj = parse_json(line)
        if obj.type == 'user' and 'continued from a previous conversation' in obj.content:
            last_summary_idx = i
    
    start = last_summary_idx if last_summary_idx >= 0 else 0
    return format_as_human_readable(lines[start:])
```

**实测数据**：

| 指标 | 数据 |
|------|------|
| BestYearly 4.8MB JSONL 最后上下文窗口 | 50KB（18 行） |
| 注入新会话后上下文占用 | ~12K tokens（远小于 1M 上限） |

---

## 4. 功能需求

### P0 功能

**FR-P0-001：每 turn 后 auto git push**
- **描述**：Relay 在每个 CC turn 结束时（`finishTurn` 事件），在工作区目录执行 `git add -A && git commit -m "auto: turn {n}" && git push`。确保 workspace 文件实时持久化到 GitHub。
- **来源**：故事1
- **失败处理**：push 失败不阻断服务，下次 turn 重试。连续 N 次（默认 5 次）失败时飞书告警管理员。

**FR-P0-002：Claude 账号被封检测**
- **描述**：Relay 检测 CC 进程的认证失败信号（持久 401/403，非临时 429 限流）。区分以下场景：
  - 429（限流）→ 走现有 token fallback 路径（`cc-process-pool` FR-P0-005）
  - 401/403（认证失败/被封）→ 判定为账号被封，触发冷迁移流程
- **来源**：故事1
- **检测逻辑**：同一会话内连续 3 次收到 401/403 且重试间隔内无恢复 → 判定账号被封

**FR-P0-003：账号被封时上下文抢救**
- **描述**：检测到账号被封后，Relay 执行以下操作（节点还活着，JSONL 可读）：
  1. 飞书通知受影响租户："Claude 服务暂时不可用，正在迁移，预计 1-3 分钟"
  2. 读取该租户当前 CC session 的 JSONL 文件
  3. 提取最后一个完整上下文窗口（见 3.5 节）
  4. 格式化为人类可读的 Markdown
  5. 保存到工作区 `.relay/last-context.md`
  6. `git add -A && git commit -m "cold-migrate: save context" && git push`
  7. 停止该租户的 CC 进程
- **来源**：故事1、故事2

**FR-P0-004：冷迁移续接注入**
- **描述**：当租户在新节点上首次启动 CC 时（git clone 后），Relay 检查工作区内是否有 `.relay/last-context.md`。如有，以其内容构造续接消息：
  ```
  ## 会话续接 — 从其他节点迁移
  
  {last-context.md 的内容}
  
  ---
  你刚刚从节点 A 迁移到节点 B。请基于以上上下文继续为用户服务。
  ```
  以此消息作为新 CC 会话的第一条 human message。
- **来源**：故事2

**FR-P0-005：标准化工作区路径**
- **描述**：采用上述 CC 冷迁移路径的租户在国外节点统一挂载到 `/opt/pulse/tenants/{tenant-id}/`。
- **来源**：运维一致性

**FR-P0-006：保留 Codex 历史工作区**
Oracle 从受审节点登记读取 Codex 历史的原工作区，匹配同一 Agent 当前已登记的工作区和原始会话文件。
历史 JSONL 保持原字节。新请求的实际工作目录和文件访问范围由目标节点的工作区登记决定。
`@anchor test agent-harness/tests_rewrite/test_session_sources.py :: test_registered_codex_history_keeps_the_original_header`

### P1 功能

**FR-P1-001：冷迁移管理命令**
- **描述**：管理员通过命令行或飞书命令触发冷迁移：
  - `relay migrate <tenant-id> --to <node-id>`：将租户迁移到指定节点
  - `relay status`：查看各节点租户分布
- **来源**：运维需求

**FR-P1-002：迁移完成通知**
- **描述**：冷迁移完成后，飞书通知租户"服务已恢复，你的 Agent 已迁移到新节点"。
- **来源**：故事1

### P2 功能

**FR-P2-001：节点负载均衡**
- **描述**：租户开通时根据各节点租户数量自动分配（负载最少的节点优先）。
- **来源**：运维自动化

---

## 5. 验收标准

### P0 验收标准

**AC-P0-001：auto git push 正常工作**
- **条件**：CC 完成一个 turn，修改了工作区文件
- **预期结果**：GitHub 仓库中可见新的 commit，包含工作区变更
- **验证方式**：单元测试（mock git）+ 集成测试

**AC-P0-002：auto git push 失败不阻断**
- **条件**：CC 完成一个 turn，GitHub 不可达
- **预期结果**：本地文件不丢失，CC 继续正常工作。告警日志记录 push 失败。
- **验证方式**：单元测试

**AC-P0-003：账号被封检测准确**
- **条件**：CC 收到连续 3 次 401/403
- **预期结果**：Relay 判定为账号被封，触发冷迁移流程。不与 429 限流混淆。
- **验证方式**：单元测试

**AC-P0-004：上下文抢救成功**
- **条件**：账号被封，节点仍然存活，CC session JSONL 存在
- **预期结果**：`.relay/last-context.md` 成功写入并 push 到 GitHub，包含最后一个完整上下文窗口
- **验证方式**：集成测试

**AC-P0-005：冷迁移续接有效**
- **条件**：新节点 clone 租户仓库，存在 `.relay/last-context.md`
- **预期结果**：新 CC 会话的第一条 human message 包含 last-context 内容，CC 能理解之前的工作上下文
- **验证方式**：端到端测试

**AC-P0-006：续接消息大小合理**
- **条件**：CC session JSONL 为 5MB（含 4 个上下文窗口）
- **预期结果**：提取的 last-context ≤ 200KB，注入新会话后占用 ≤ 50K tokens
- **验证方式**：单元测试

### P1 验收标准

**AC-P1-001：迁移命令可用**
- **条件**：管理员执行 `relay migrate tenant-001 --to node-b`
- **预期结果**：tenant-001 被迁移到 node-b，服务在新节点恢复
- **验证方式**：手动测试

**AC-P1-002：迁移通知**
- **条件**：冷迁移完成
- **预期结果**：租户在飞书中收到"服务已恢复"通知
- **验证方式**：集成测试

---

## 6. 非功能需求

### 性能需求
- **auto git push**：每 turn 增加延迟 ≤ 2s（git 操作异步执行，不阻塞 CC）
- **上下文提取**：从 5MB JSONL 提取最后上下文窗口 ≤ 1s
- **冷迁移总耗时**：≤ 3 分钟（含 git clone + CC 启动 + 续接注入）

### 安全需求
- **租户仓库必须为 GitHub 私有仓库**
- **last-context.md 包含完整对话内容**，不得公开暴露
- **认证方式**：SSH Deploy Key（每个国外节点一对），只用于 git push/pull

### 可用性需求
- **GitHub 不可达时**：auto git push 失败不阻断服务，本地文件不丢失
- **节点宕机（Level 2）**：管理员手动迁移。last-context.md 可能未 push，依赖 MEMORY.md 兜底
- **飞书聊天记录 = 完整对话历史**：不需要额外归档机制

---

## 7. 边界条件与风险

### 7.1 两层故障处理

| | Level 1：Claude 账号被封 | Level 2：节点宕机/断网 |
|---|---|---|
| **频率** | 中等（封号风险） | 低频 |
| **节点状态** | 存活（可读写文件） | 不可达 |
| **JSONL** | ✅ 可读取和抢救 | ❌ 无法访问 |
| **处理方式** | 自动：检测 → 抢救上下文 → 通知 → 管理员分配新节点 | 手动：管理员确认后分配新节点 |
| **数据保障** | last-context.md 已 push | workspace 已 push（auto git push），上下文依赖 MEMORY.md |
| **用户感知** | 等待 1-3 分钟 | 等待 1-5 分钟 |

### 7.2 已知边界条件

| 条件 | 影响 | 处理 |
|------|------|------|
| 节点 A 账号被封但 git push last-context 失败 | 上下文抢救不成功 | 新节点启动时使用 MEMORY.md 兜底，CC 读 MEMORY.md 仍能恢复基本状态 |
| 节点 A 直接宕机（Level 2） | last-context.md 未生成 | 同上，依赖 MEMORY.md + 飞书聊天记录 |
| git push 长期失败 | workspace 文件未同步到 GitHub | CC 继续正常工作，本地文件不丢失。管理员收到连续失败告警后处理 |
| CC 上下文窗口恰好为空（刚启动就被封） | last-context.md 内容很少 | CC 启动时就读了 CLAUDE.md 和 MEMORY.md，这些已在 repo 中 |

### 7.3 不解决（Out of Scope）

| 项目 | 原因 |
|------|------|
| 热迁移 / 无缝切换 | git clone 大仓库耗时不可控，不追求 |
| 行动日志系统（独立于 JSONL） | CC JSONL 已包含完整信息，不需要重复记录 |
| CC 自带摘要截获（独立功能） | 直接从 JSONL 提取，不需要运行时截获 |
| JSONL 归档到 GitHub | 体积膨胀影响 git clone 速度；飞书聊天记录已是完整历史 |
| 节点间直接通信 | 增加复杂度，通过 GitHub + 管理员操作已足够 |
| 自动故障检测与节点接管 | V1 通过管理员手动操作；V2 可接入 PULSE 平台自动化 |
| 多 Agent 共享 repo 的迁移 | 未来可用 git worktree 支持，V1 不做 |
| 节点预热（所有节点提前 clone 所有 repo） | 冷迁移方案下不需要，新节点按需 clone |
| 共享路由状态（pulse-cluster repo） | 冷迁移方案下不需要，管理员手动分配 |

---

## 8. 实现优先级与里程碑

### M1: auto git push（最核心，独立可用）
- FR-P0-001：每 turn 后 auto git push
- FR-P0-005：标准化工作区路径
- **交付标准**：CC 在 clone 的租户 repo 中工作，每个 turn 后 workspace 变更自动 push 到 GitHub

### M2: 账号被封检测 + 上下文抢救
- FR-P0-002：Claude 账号被封检测
- FR-P0-003：上下文抢救（读 JSONL → 提取最后上下文 → push 到 GitHub）
- **交付标准**：账号被封时自动抢救上下文到 GitHub，通知租户

### M3: 冷迁移续接
- FR-P0-004：续接注入（新节点读 last-context.md → 注入新 CC 会话）
- FR-P1-002：迁移完成通知
- **交付标准**：管理员将租户分配到新节点后，CC 能恢复之前的工作上下文

### M4: 管理工具
- FR-P1-001：冷迁移管理命令
- FR-P2-001：节点负载均衡（开通时自动分配）
