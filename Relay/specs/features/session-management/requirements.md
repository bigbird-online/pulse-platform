---
feature_name: "session-management"
version: "0.1.0"
created: "2026-04-07T10:45:00+0800"
modified: "2026-06-12T13:15:00+0800"
upstream_dependency: ["project-vision.md", "project-architecture.md"]
tags: [requirements, l2-spec, session, relay-m3]
changelog:
  - "v0.1.0: 150 同步——新增跨渠道 session 访问需求（故事3/FR-P0-004/AC-P0-003）"
---

# session-management - 功能需求文档

> **功能概述**：管理 CC 子进程的 session 持久化和恢复，确保租户对话在进程重启/空闲回收后能通过 `--resume` 无缝继续，用户无感知中断。
>
> **关联文档**：
> - L1 项目架构：`project-specs/project-architecture.md`
> - 上游依赖：`features/cc-process-pool/`、`features/telegram-channel/`

---

## 1️⃣ 用户故事

**故事1：对话跨进程持续**
- **作为**：Telegram 用户
- **我希望**：即使 AI 后台进程重启，我的对话历史仍然保留
- **以便**：不需要重复说过的话，体验连续自然
- **优先级**：P0

**故事2：session-id 与租户绑定**
- **作为**：Relay 运维者
- **我希望**：每个租户有唯一且稳定的 session-id，存储在 tenants.json 中
- **以便**：CC 的 `--session-id` + `--resume` 能可靠恢复上下文
- **优先级**：P0

**故事3：跨渠道 session 访问**
- **作为**：管理员/开发者
- **我希望**：通过飞书 bot 与 agent 对话后，能在 CC 终端通过 `claude -r` 找到并恢复同一 session
- **以便**：在终端执行 undo/rollback/权限提升等操作，无需重复上下文
- **优先级**：P0
- **详细需求**：`req-cross-channel-session-access.md`

---

## 2️⃣ 功能需求

### P0 功能

**FR-P0-001：session-id 绑定**
- **描述**：每个租户在 tenants.json 中配置唯一 `sessionId`（UUID v4）。spawn CC 时通过 `--session-id` 传入，CC 自动将对话持久化到 `~/.claude/projects/` 下
- **来源**：故事2

**FR-P0-002：resume 恢复**
- **描述**：当 CC 进程因空闲回收或崩溃后重新 spawn 时，自动携带 `--resume <sessionId>` 参数。CC 恢复对话历史上下文
- **来源**：故事1

**FR-P0-003：session 状态可追踪**
- **描述**：pool.getStatus() 返回的信息中包含 sessionId，运维可查看哪个租户绑定了哪个 session
- **来源**：故事2

**FR-P0-004：跨渠道 session 可发现**
- **描述**：非 linuxUser agent 的 CC 进程注入 `userHome` 作为 HOME 目录，使 session JSONL 文件存储在用户的 `~/.claude/projects/` 下。用户可通过 `claude -r` 跨渠道发现并恢复 session
- **来源**：故事3

---

## 3️⃣ 验收标准

**AC-P0-001：resume 后上下文保留**
- **条件**：租户发消息 → CC 进程处理 → 进程被 kill → 重新 spawn + resume → 租户再发消息
- **预期结果**：CC 回复包含对之前对话的引用，证明历史上下文已恢复
- **验证方式**：Integration Test（真实 CC 进程）

**AC-P0-002：sessionId 传参正确**
- **条件**：spawn CC 进程
- **预期结果**：spawn 参数中包含 `--session-id <tenantConfig.sessionId>`
- **验证方式**：Unit Test（已在 cc-process-pool 测试中覆盖）

**AC-P0-003：跨渠道 session 文件可发现**
- **条件**：管理员 agent 配置 `userHome`，通过飞书 bot 发起对话
- **预期结果**：session JSONL 文件出现在 `userHome/.claude/projects/<workspace>/` 下，`claude -r` 可列出并恢复
- **验证方式**：手动验证（需飞书 bot + CC 终端）

---

## 4️⃣ 非功能需求

- **持久化路径**：CC 自动管理（`~/.claude/projects/`），Relay 通过 `userHome` 字段控制 HOME 注入
- **恢复延迟**：resume 首次加载较慢（需读取历史），可接受 ≤ 15 秒

---

## 5️⃣ 约束条件

- CC 原生支持 `--session-id` + `--resume`，Relay 只需正确传参
- session 存储路径由 CC 管理，Relay 通过 `userHome` 字段控制 HOME 目录
- `linuxUser` 隔离场景已有独立的 HOME 注入（PT0-018），`userHome` 仅用于非 linuxUser 场景

---

## 6️⃣ 排除范围

- ❌ 多 session 切换（一个租户只有一个 session）
- ❌ session 过期/清理（由 CC 管理）
- ❌ session 导出/迁移
