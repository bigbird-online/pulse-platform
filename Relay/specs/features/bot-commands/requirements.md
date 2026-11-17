---
feature_name: "bot-commands"
version: "0.2.1"
created: "2026-04-07T13:20:00+0800"
modified: "2026-09-28T19:30:00+0800"
upstream_dependency: ["project-vision.md", "project-architecture.md"]
tags: [requirements, l2-spec, bot-commands, multi-agent, relay-m5]
changelog:
  - version: "0.2.1"
    date: "2026-09-28"
    note: "Telegram /wrap 从当前 Host 已登记工作区定位真实 Skill；缺失时拒绝并不记收口请求"
  - version: "0.2.0"
    date: "2026-08-23"
    note: "统一四 runtime 命令控制面；/model 按 runtime/access/model 分层；有界 /models 与飞书卡片失败兜底"
  - version: "0.1.4"
    date: "2026-05-13"
    note: "150-spec-sync — CC 内部斜杠命令（/compact /context）透传说明；GLM context limit 修正为 200K"
  - version: "0.1.3"
    date: "2026-05-12"
    note: "150-spec-sync — 飞书补齐 /reset /runtime /yolo /jobs /job_add /job_del /job_run；飞书禁用 /list /switch；FR-P2-002 标记已实现；FR-P0-002/003 补飞书独立 bot 模式说明"
  - version: "0.1.2"
    date: "2026-05-02"
    note: "FR-P2-001/P2-002 多渠道对等；AC-P2-002/P2-003；Out of Scope 收紧"
  - version: "0.1.1"
    date: "2026-05-02"
    note: "FR-P2-003 更正为 /usage 完整实现与 AC-P2-001"
  - version: "0.1.0"
    date: "2026-04-29"
    note: "同步代码实现：新增 /session /usage /runtime /yolo /stop 命令 + job-commands + command-menu；已有功能标记已完成"
---

# bot-commands - 功能需求文档

> **功能概述**：为 **Telegram 与飞书** 提供统一的 Bot 命令与 **多 Agent 路由** 能力（业务逻辑在 `commands.ts`，飞书侧 UI 载体见 `feishu-channel`）。**Telegram**：单对话切换多个 OA。v0.5 起支持 CC/Codex runtime、YOLO、计划任务等。
>
> **关联文档**：
> - L1 项目愿景：`project-specs/project-vision.md`
> - L1 项目架构：`project-specs/project-architecture.md`
> - L2 技术设计：`features/bot-commands/design.md`
> - L2 任务拆分：`features/bot-commands/tasks.md`
> - **飞书侧交互载体**：`features/feishu-channel/requirements.md`（FR-P1-005 / FR-P1-006 与本文 FR-P2-001 / FR-P2-002 对等）
> - 上游依赖：`features/telegram-channel/`（已完成）、`features/cc-process-pool/`（已完成）

---

## 1. 用户故事（User Stories）

**故事1：用户查看可用 OA 列表**
- **作为**：Telegram 用户（大鸟）
- **我希望**：发送 `/list` 查看所有可用的 AI Agent 及其状态
- **以便**：知道有哪些 Agent 可用，以及哪个正在运行
- **优先级**：P0

**故事2：用户切换活跃 OA**
- **作为**：Telegram 用户
- **我希望**：发送 `/switch ken` 切换到 Ken Agent
- **以便**：与不同领域的 Agent 交互，无需退出 TG 对话
- **优先级**：P0

**故事3：用户首次使用时获得引导**
- **作为**：首次使用的 Telegram 用户
- **我希望**：发送 `/start` 看到欢迎信息和可用命令列表
- **以便**：了解如何使用 bot
- **优先级**：P0

**故事4：用户查看当前 OA 状态**
- **作为**：Telegram 用户
- **我希望**：发送 `/status` 查看当前活跃 Agent 的运行状态
- **以便**：了解 Agent 是否就绪、忙碌或已断开
- **优先级**：P0

**故事5：用户识别回复来自哪个 OA**
- **作为**：Telegram 用户
- **我希望**：每条 AI 回复都带有 Agent 标识前缀（如 `🎲 Duke`）
- **以便**：在切换 Agent 后能清楚区分回复来源
- **优先级**：P0

**故事6：用户直接发消息自动路由到默认 OA**
- **作为**：Telegram 用户
- **我希望**：不发命令直接发消息时，自动路由到默认 Agent
- **以便**：最常用场景零摩擦
- **优先级**：P0

**故事7：用户终止或重启 OA 进程**
- **作为**：Telegram 用户
- **我希望**：发送 `/kill` 终止当前 Agent，或 `/restart` 重启
- **以便**：Agent 卡死时能手动恢复
- **优先级**：P1

**故事8：用户查看命令帮助**
- **作为**：Telegram 用户
- **我希望**：发送 `/help` 查看所有可用命令的说明
- **以便**：记不住命令时快速查阅
- **优先级**：P1

**故事9：用户开启新对话**
- **作为**：Telegram 用户
- **我希望**：发送 `/new` 开启一个全新的 session
- **以便**：在上下文过载或想重置对话时清空历史
- **优先级**：P0

**故事10：用户查看 session 详细信息**
- **作为**：Telegram 用户
- **我希望**：发送 `/session` 查看当前 session 的 ID、runtime、运行时长等
- **以便**：调试或了解 session 状态
- **优先级**：P1

**故事11：用户查看费用统计**
- **作为**：Telegram 用户
- **我希望**：发送 `/usage` 查看当前 Agent 的费用统计
- **以便**：了解用量情况
- **优先级**：P2

**故事12：用户切换 conversation runtime**
- **作为**：Telegram 用户
- **我希望**：发送 `/runtime codex` 将当前 Agent 的 runtime 从 CC 切换到 Codex
- **以便**：在不同 runtime 之间切换以利用各自优势
- **优先级**：P1

**故事13：用户控制 Codex YOLO 模式**
- **作为**：Telegram 用户
- **我希望**：发送 `/yolo on` 开启 Codex 的 YOLO 模式（跳过审批和沙箱）
- **以便**：在需要快速执行时绕过安全限制
- **优先级**：P1

**故事14：用户中断当前工作**
- **作为**：Telegram 用户
- **我希望**：发送 `/stop` 中断 Agent 当前正在进行的 turn
- **以便**：在 Agent 做错事时及时止损，不必 kill 整个进程
- **优先级**：P1

**故事15：用户管理计划任务**
- **作为**：Telegram 用户
- **我希望**：通过 `/jobs`、`/job_add`、`/job_del`、`/job_run` 管理定时任务
- **以便**：让 Agent 按计划自动执行任务
- **优先级**：P2

---

## 2. 功能需求（Functional Requirements）

### P0 功能（MVP 必需）

**FR-P0-001：/start 命令 — 欢迎 + 命令列表** [x]
- **描述**：用户发送 `/start` 后，回复当前 Agent 就绪信息并列出所有可用命令（/status, /session, /runtime, /yolo, /new, /stop, /kill, /restart, /usage, /help）
- **来源**：故事3
- **实现**：`src/commands.ts` handleStart — 显示当前 Agent 名称 + emoji + 命令列表
- **示例**：
  ```
  🎲 Duke 已就绪。直接发消息开始对话。

  /status — 查看进程状态
  /session — 查看 session 信息
  /runtime — 查看/切换 conversation runtime
  /yolo — 查看/切换 Codex YOLO 模式
  /new — 开启新对话
  /stop — 中断当前工作
  /kill — 终止进程
  /restart — 重启进程
  /usage — 查看费用统计
  /help — 命令帮助
  ```

**FR-P0-002：/list 命令 — OA 列表 + 进程状态** [x]
- **描述**：列出所有可用 Agent，标注进程状态（idle / busy / ready / dead / 未启动）和当前活跃 Agent
- **来源**：故事1
- **实现**：`src/commands.ts` handleList
- **示例**：
  ```
  可用 Agent:

  ▶ 🎲 Duke [idle]
    🔧 Ken [未启动]
    🍂 Aki [未启动]
    ...
  ▶ = 当前活跃
  ```
- **注意**：在独立 bot 模式下（飞书），/list 和 /switch 命令被禁用，返回"当前 bot 已绑定固定 Agent，无需切换"（每个 bot 对应一个 Agent）

**FR-P0-003：/switch <id> 命令 — 切换活跃 OA** [x]
- **描述**：切换当前用户的活跃 Agent。如果目标 Agent 的 CC 进程未启动，按需 spawn。切换不终止原 Agent 进程（保持 idle）
- **来源**：故事2
- **实现**：`src/commands.ts` handleSwitch
- **注意**：在独立 bot 模式下（飞书）不可用，返回"当前 bot 已绑定固定 Agent，无需切换"

**FR-P0-004：/status 命令 — 当前 OA 状态** [x]
- **描述**：显示当前活跃 Agent 的详细状态（名称、进程状态、PID、session ID、上次活动时间、重启次数）。支持可选的 agentId 参数查看指定 Agent
- **来源**：故事4
- **实现**：`src/commands.ts` handleStatus — 支持持久化 session 的待恢复状态
- **示例**：
  ```
  当前 Agent: 🎲 Duke
  状态: idle
  PID: 12345
  Session: abc-def-123
  上次活动: 2 分钟前
  重启次数: 0
  ```

**FR-P0-005：回复消息带 OA 标识前缀** [x]
- **描述**：CC 回复转发给 TG 用户时，在首条消息前添加 Agent 标识（`emoji name`）。后续分片消息不重复前缀
- **来源**：故事5
- **实现**：`src/channels/telegram.ts` prefixMessage
- **示例**：CC 回复 "你好！" → TG 收到 "🎲 Duke:\n你好！"

**FR-P0-006：配置格式升级 — 支持 per-user 多 Agent** [x]
- **描述**：配置文件从 tenants.json（chatId→tenant 1:1）升级为 relay.json（user→agents 1:N）。每个 user 有 chatId、defaultAgent、agents 列表。每个 agent 有 id、name、emoji、workspace、sessionId，可选 botToken、runtime、conversationRuntimePreference 等
- **来源**：故事2、故事6
- **向后兼容**：不兼容旧 tenants.json 格式（v0.1 只有测试租户，无需迁移）

**FR-P0-007：默认 OA 自动路由** [x]
- **描述**：用户直接发送文本消息（非命令）时，路由到当前活跃 Agent。若未曾 /switch 过，路由到 defaultAgent
- **来源**：故事6

**FR-P0-008：/new 命令 — 开启新对话** [x]
- **描述**：普通会话直接终止当前 Agent 进程，重置 session ID（生成新 UUID），清理持久化状态，开始全新的对话；不需要历史 `/wrap` 标记。活动或待接续 WI 仍由 SCL 绑定守卫拒绝直接替换，须 rollover 或正式 recovery。通过 StateManager 持久化新 sessionId 以支持 Relay 重启后恢复
- **来源**：故事9
- **实现**：`src/commands.ts` handleNew — 调用 router.resetSession() 生成新 session ID

### P1 功能（增强功能）

**FR-P1-001：/kill 命令 — 终止当前 OA 进程** [x]
- **描述**：终止当前活跃 Agent 的 CC 进程。支持可选 agentId 参数
- **来源**：故事7
- **实现**：`src/commands.ts` handleKill
- **示例**：`/kill` → `已终止 🎲 Duke 进程。下次发消息时自动重启。`

**FR-P1-002：/restart 命令 — 重启当前 OA** [x]
- **描述**：先 kill 当前 Agent 进程，再以 resume 模式重新 spawn。支持可选 agentId 参数。重启时使用持久化的 runtime 和 codexThreadId
- **来源**：故事7
- **实现**：`src/commands.ts` handleRestart
- **示例**：`/restart` → `已重启 🔧 Ken。`

**FR-P1-003：/help 命令 — 命令帮助** [x]
- **描述**：列出所有可用命令及简要说明
- **来源**：故事8
- **实现**：`src/commands.ts` handleHelp

**FR-P1-004：/session 命令 — 查看 session 信息** [x]
- **描述**：显示当前 session 的详细信息：session ID、状态、runtime、工作区路径、运行时长、PID。对 Codex runtime 额外显示 threadId。支持持久化 session 的待恢复状态
- **来源**：故事10
- **实现**：`src/commands.ts` handleSession

**FR-P1-005：/stop 命令 — 中断当前工作** [x]
- **描述**：向当前 busy 的 Agent 进程发送 SIGINT 信号，中断当前 turn（不终止进程）。仅在 Agent 处于 busy 状态时生效
- **来源**：故事14
- **实现**：`src/commands.ts` handleStop — 通过 proc.process.kill('SIGINT') 实现

**FR-P1-006：/runtime [cc|codex] 命令 — 查看/切换 runtime** [x]
- **描述**：无参数时显示当前 runtime 配置和状态。带参数时切换到指定 runtime（cc 或 codex），自动终止当前进程、清理 session 状态、同步 conversationRuntimePreference，下一条消息使用新 runtime
- **来源**：故事12
- **实现**：`src/commands.ts` handleRuntime — 调用 router.setAgentRuntime() 更新配置

**FR-P1-007：/yolo [on|off|status] 命令 — YOLO 模式控制** [x]
- **描述**：仅对 Codex runtime 生效。`status` 显示当前 YOLO 状态；`on` 开启（使用 --dangerously-bypass-approvals-and-sandbox）；`off` 关闭（恢复 workspace-write + approval=never）。YOLO 模式为 session 内存状态，Relay 重启或 /new 后失效
- **来源**：故事13
- **实现**：`src/commands.ts` handleYolo — 通过 runtime.setYoloMode/getYoloMode 控制

**FR-P1-008：历史 Telegram `/wrap` 兼容** [~]
- **描述**：旧入口仍可解释旧状态，但用户已决定不再需要该功能，也不再以它作 UAT 或 `/new` 的前置。`wrapRequestedAt` 不等于收口完成；普通会话换会话不依赖它，活动 WI 另由 SCL 授权事实约束。
- **实现**：`src/channels/telegram.ts` 暂保留历史入口和旧字段的安全读回；退役后的实际渠道行为须在精确候选上验收。

### P2 功能（未来增强）

**FR-P2-001：多渠道 Agent 点选切换（TG Inline + 飞书交互按钮）**
- **描述**：多 Agent 场景下，用户需能不依赖记忆命令完成切换。**用户需求在 Telegram 与飞书上一致**。**Telegram**：`/list`、`/start` 等输出 **inline keyboard**，点击即切换（grammy）。**飞书**：在同等语义下提供 **Interactive Card / 消息按钮**（或 Lark 支持的等价交互），点击后执行与 `/switch <agentId>` 相同的路由（复用 `router` + `commands.ts`，不在飞书侧复制业务规则）。
- **关联实现切片**：`features/feishu-channel` **FR-P1-005**（飞书载体）；`src/channels/telegram.ts`（TG 载体）
- **状态**：待实现

**FR-P2-002：per-Agent CC 模型配置（多渠道共用配置）** [x]
- **描述**：不同 Agent 可在 relay.json（或 SSOT）中配置不同 CC **`--model`**；**Telegram 与飞书用户看到的模型行为一致**（同一 `AgentConfig`，无「渠道专属模型」分叉）。飞书侧无需单独命令即可继承 pool spawn 参数。
- **关联实现切片**：`AgentConfig.ccModel` → `SpawnConfig.ccModel` → `pool.ts` `appendModelArg()` → `--model {ccModel}`；TG (`telegram.ts`) 和飞书 (`feishu.ts`) 均传 `routing.ccModel`
- **实现**：`src/types.ts` `ccModel?: string`；`src/pool.ts` `appendModelArg()`；`src/channels/telegram.ts` + `src/channels/feishu.ts` spawn 路径均传入

**FR-P2-003：/usage 命令 — 费用统计（完整实现）** [x]
- **描述**：对 **当前 Agent**（poolKey=`userId:agentId`）展示 **按 UTC 自然月** 聚合的 metering 汇总：调用次数、`total_cost_usd`、各 token 字段合计、`daysCovered`、损坏行计数。数据源为 `METERING_DIR` 下按日 `YYYY-MM-DD.jsonl`（见 `features/metering`）。未配置 `meteringDir` 时返回启用指引，不谎称有数据。
- **非目标（本期）**：任意起止日期的 ad-hoc 区间查询——可作为后续 enhancement。
- **来源**：故事11
- **实现**：`src/commands.ts` `handleUsage`；汇总 `src/metering.ts` `summarizeMonthlyUsage`；单测 `tests/commands.test.ts` `describe('handleUsage')`

**FR-P2-004：计划任务命令** [x]
- **描述**：支持 4 个计划任务命令：
  - `/jobs` — 列出当前 Agent 的计划任务
  - `/job_add <cron> | <prompt> [| name=xxx] [| timeout=xxx] [| delivery=announce]` — 注册计划任务
  - `/job_del <id>` — 删除计划任务
  - `/job_run <id>` — 立即执行任务
- **来源**：故事15
- **实现**：`src/job-commands.ts` — handleListJobs, handleAddJob, handleDelJob, handleRunJob

**FR-P2-005：命令菜单注册** [x]
- **描述**：启动时通过 Telegram Bot API `setMyCommands` 注册所有命令到 TG 命令菜单，使用户在聊天界面能看到命令列表
- **实现**：`src/command-menu.ts` registerCommandMenu — 注册 15 个命令

**FR-P2-006：CC 内部斜杠命令透传** [x]
- **描述**：CC（Claude Code）内置斜杠命令（如 `/compact`、`/context`）不是 Relay 命令，应直接透传给 CC 进程处理。飞书侧在 `handleCommand` 拦截前过滤这些命令，使其落入正常 CC 消息流。Telegram 侧因 grammy 只注册已知命令，未知 `/xxx` 自然透传，无需额外处理
- **实现**：`src/channels/feishu.ts` text/post 消息处理中，`handleCommand` 调用前检查 `cmd === 'compact' || cmd === 'context'` 时跳过拦截
- **适用渠道**：飞书（TG 天然支持透传）

**FR-P2-007：四 Runtime 统一命令控制面** [x]
- **描述**：Relay 是 `/命令` 的统一控制面，不能把 `/model`、`/stop` 等命令定义成某个 runtime 的附属功能。命令先解析当前 live/persisted/configured runtime，再按能力执行 Relay 生命周期操作、runtime-native RPC 或原生命令透传。
- **Runtime 范围**：`cc`、`codex`、`cc-sdk`、`pi`。
- **模型交互**：`/model` 依次选择 `runtime → 接入方式（订阅/OpenRouter API）→ model`；不得把 provider 的完整模型目录平铺为一张卡片。跨 runtime 必须先显式切换并告知 session 会被清理。
- **规模约束**：模型 scope 卡片最多展示 8 个模型；`/models [关键词]` 最多返回 30 条并提示继续筛选。
- **能力真实性**：未实现的组合必须显示“尚未接入”，不得生成可点击的假选项。例如当前 `cc-sdk + OpenRouter API` 尚未实现。
- **失败语义**：所有飞书命令卡片统一执行元素预算检查；Card Kit 发送失败必须自动回退文字反馈，不能让用户收到零响应。
- **通用生命周期**：`/new`、`/stop`、`/kill`、`/restart` 面向当前 runtime；`/stop` 只中断 active turn 并保留 session，`/new` 生成新 session，`/restart` 恢复 exact persisted binding。
- **Runtime 专属能力**：`/think` 当前仅 Pi 原生支持；`/yolo` 仅 Codex；`/compact`、`/context` 对 CC 系走原生命令透传、对 Pi 走原生 RPC。

---

## 3. 验收标准（Acceptance Criteria）

### P0 功能验收标准

**AC-P0-001：/start 展示命令列表** [x]
- **条件**：已注册用户发送 `/start`
- **预期结果**：收到当前 Agent 就绪信息 + 命令列表
- **验证方式**：Unit Test

**AC-P0-002：/list 展示进程状态** [x]
- **条件**：用户发送 `/list`，其中 1 个 Agent 处于 idle 状态
- **预期结果**：列表中该 Agent 标注 `[idle]`，其余标注 `[未启动]`，活跃 Agent 有 `▶` 标记
- **验证方式**：Unit Test

**AC-P0-003：/switch 切换 Agent** [x]
- **条件**：用户发送 `/switch ken`
- **预期结果**：收到切换确认消息；后续文本消息路由到 Ken 的 workspace
- **验证方式**：Unit Test + Integration Test

**AC-P0-004：/switch 未知 Agent** [x]
- **条件**：用户发送 `/switch nobody`
- **预期结果**：收到错误提示，包含可用 Agent 提示
- **验证方式**：Unit Test

**AC-P0-005：/status 显示进程详情** [x]
- **条件**：用户有活跃 Agent（idle 状态）
- **预期结果**：显示 Agent 名称、状态、PID、session ID、上次活动时间
- **验证方式**：Unit Test

**AC-P0-006：回复带 Agent 标识** [x]
- **条件**：当前活跃 Agent 是 Duke (🎲)，CC 回复 "你好"
- **预期结果**：TG 消息为 "🎲 Duke:\n你好"
- **验证方式**：Unit Test

**AC-P0-007：默认路由** [x]
- **条件**：用户未执行 /switch，直接发送文本消息
- **预期结果**：消息路由到 defaultAgent 指定的 Agent
- **验证方式**：Unit Test

**AC-P0-008：配置加载多 Agent** [x]
- **条件**：relay.json 包含 1 个 user 和多个 agent 定义
- **预期结果**：router 正确解析所有 agent
- **验证方式**：Unit Test

**AC-P0-009：/new 开启新对话** [x]
- **条件**：普通会话用户发送 `/new`，包括旧状态仍留有 `wrapRequestedAt` 的情形
- **预期结果**：旧进程被终止，session ID 被重置，持久化状态更新，用户收到新 session 确认；若仍绑定活动或待接续 WI，则拒绝并指向正式 rollover/recovery，不以旧标记放行
- **验证方式**：Unit Test

**AC-P1-008：/wrap 跨 Host 路径与缺失拒绝** [x]
- **当前状态**：历史兼容测试，不是本轮 UAT 或 `/new` 准入要求
- **条件**：分别以 canonical Coevolution 根、`CollaborateWithAI` 子仓布局及未安装 Skill 的工作区运行 Telegram 入口
- **预期结果**：前两者向 Agent 传递该 Host 实际存在的 Skill 路径；缺失时用户收到拒绝提示，运行时没有收到 `/wrap`，session 不记录 `wrapRequestedAt`
- **验证方式**：`tests/telegram.test.ts` 的 TG human session wrap guard

**AC-P2-001：/usage 展示自然月 metering 汇总** [x]
- **条件**：已配置 `meteringDir`，且本月 JSONL 中存在该 tenant（poolKey）的 `result` 记录
- **预期结果**：`/usage` 返回本月 UTC 周期、调用次数、Cost USD、input/output/cache 类 token 合计、`daysCovered`；无数据时有明确提示；未配置 metering 目录时提示启用 `METERING_DIR` 而非报错
- **验证方式**：`tests/commands.test.ts` — `describe('handleUsage')`

**AC-P2-002：多渠道 Agent 点选切换语义一致** [ ]
- **条件**：同一 `userId` 下注册多个 Agent；用户在 **Telegram** 点 inline 按钮与在 **飞书** 点卡片按钮（或等价交互）
- **预期结果**：两侧切换后的 `getActiveAgent` / 后续文本路由一致（同一 `poolKey` 语义）
- **验证方式**：Unit Test + 飞书手动 UAT

**AC-P2-003：per-Agent 模型在双渠道生效一致** [x]
- **条件**：两个 Agent 配置不同 `model`（或等价 spawn 参数）；用户分别在 TG 与飞书与该 Agent 对话
- **预期结果**：CC 子进程使用的模型与配置一致，无「仅某一渠道生效」分叉
- **验证方式**：`ccModel` 全链路代码审查（`types.ts` → `pool.ts` `appendModelArg` → `telegram.ts` + `feishu.ts` spawn 路径均传入）

---

## 4. 非功能需求（Non-Functional Requirements）

### 性能需求
- **命令响应延迟**：/start、/list、/switch、/status、/help ≤ 500ms（纯本地操作，不涉及 CC）
- **Agent 切换后首次消息**：若需 spawn CC，延迟 ≤ 8 秒（含 CC 启动）
- **/runtime 切换延迟**：含进程终止 ≤ 3 秒

### 安全需求
- **用户隔离**：不同 chatId 的用户只能操作自己注册的 Agent
- **命令注入防护**：/switch 参数需校验为合法 agentId（alphanumeric + hyphen）
- **YOLO 模式安全**：YOLO 仅对 Codex runtime 生效，session 内存状态不持久化，Relay 重启后自动恢复安全模式

### 可维护性需求
- **代码规范**：遵循 project-standards.md
- **单文件行数**：≤ 300 行；命令处理逻辑可拆为独立文件
- **测试覆盖率**：命令处理 + 路由 ≥ 80%

---

## 5. 约束条件（Constraints）

### 技术约束
- **技术栈**：基于 project-architecture.md — Node.js ≥ 18 + TypeScript + grammy
- **上游依赖**：cc-process-pool + telegram-channel（均已实现）
- **Pool key 策略**：以 `${userId}:${agentId}` 为 pool key，支持同一用户多 Agent 进程共存
- **Runtime 支持**：cc（Claude Code CLI）和 codex（OpenAI Codex CLI），可按 Agent 配置

### 业务约束
- **本机优先**：配置路径为本机 workspace 路径
- **独立 bot 模式**：每个 Agent 可配置独立 botToken，拥有独立的 TG Bot 实例

---

## 6. 排除范围（Out of Scope）

- **多用户同时在线** — v0.1 本机单用户场景
- Subagent 路由（OA 间调用）— v0.2+
- 月预算检查 — 属于 metering feature（**硬阻断策略**若以平台为准则由 PULSE 与 Relay 协同另议）
