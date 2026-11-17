---
feature_name: "interactive-question-card"
version: "0.3.0"
created: "2026-06-10T12:00:00+0800"
modified: "2026-06-14T19:00:00+0800"
upstream_dependency: ["project-vision.md", "project-architecture.md", "cc-process-pool/requirements.md", "feishu-channel/requirements.md"]
tags: [requirements, l2-spec, feishu, cc-interactive, ask-user-question, channels-api]
changelog:
  - "v0.3.0: 多题状态机修复 — claimPendingAsk 改为只在 Submit 路径调用；统一所有 AskUserQuestion 卡片都加 Submit 按钮（单题不再 digit-submits）；PendingAsk 新增 answeredQuestions 字段并持久化；新增文字路径 pendingAsk 短路（数字+label 双匹配）。修复 2026-06-14 运营官多题卡死 bug。注：实际实现采用 Path A（PTY 键盘注入）而非 v0.2.0 假设的 A' 方案，见 retrospective-2026-06-10.md。"
  - "v0.2.0: 探针验证后修正 — AskUserQuestion 改为 A' 方案（新文字 prompt 而非 tool_result 回注）；工具权限审批降级为 P2（CC -p 模式自动审批，需 Channels API 才能实现真正审批）；新增探针证据引用"
  - "v0.1.0: 初版（未经验证的假设：tool_result 回注、CC -p 模式 permission hang）"
---

# interactive-question-card - 功能需求文档

> **功能概述**：分阶段实现飞书用户与 CC / Codex 的交互能力透传。Phase 1 拦截 CC 的 `AskUserQuestion` tool_use（实际实现走 Path A：PTY 键盘注入，详见 retrospective-2026-06-10.md；非 v0.2.0 设想的 A' 文字 prompt 方案），渲染飞书交互卡片，用户点选后通过 `pool.injectInput` 注入键盘事件让 CC 继续。Phase 2 通过 CC Channels API 实现工具权限审批透传（Bash/Write/Edit 等）。
>
> **关联文档**：
> - L1 项目愿景：`project-specs/project-vision.md`
> - L1 项目架构：`project-specs/project-architecture.md`
> - L2 技术设计：`features/interactive-question-card/design.md`（v0.2.0）
> - L2 任务拆分：`features/interactive-question-card/tasks.md`（v0.2.0）
> - MVS 切片：`features/interactive-question-card/MVS.md`（需按 A' 方案改写）
> - 调研报告：`features/interactive-question-card/research-relay-permission-passthrough.md`
> - Path A 复盘：`features/interactive-question-card/retrospective-2026-06-10.md`
> - 探针决策：`vultr-pulse-space/memo/decision-2026-05-23-askuserquestion-feishu.md`
> - 上游依赖：`features/cc-process-pool/`（进程池）、`features/feishu-channel/`（飞书通道）
> - 需求来源：语道（2026-06-09 例会）—— 飞书用户应能审批 CC/Codex 工具调用、回答 CC 提问

---

## 探针事实（2026-06-10 验证，CC 2.1.104）

以下事实已通过真实 CC binary 探针确认，为本需求文档的不可绕过前提：

| # | 事实 | 证据 |
|---|------|------|
| F1 | CC `-p` stream-json 模式下，AskUserQuestion 调用后 CC **同步自答** `is_error: true` tool_result（content="Answer questions?"），**无外部注入窗口** | 本机探针 2026-06-10；decision-2026-05-23（CC 2.1.143 验证 5 种格式均失败） |
| F2 | CC `-p` 模式下 `--permission-mode default` **自动审批所有工具**（含 `rm -rf`、Write），与 `bypassPermissions` 行为等价，**不挂起、不弹审批** | 本机探针 2026-06-10：Bash rm-rf + Write 均自动通过 |
| F3 | CC stream-json 输出仅含 4 种 type：`system`、`assistant`、`user`（CC 自生成 tool_result）、`result`。**无 `permission_request` 事件** | 本机探针 2026-06-10 |
| F4 | CC `--channels` flag 存在（`plugin:<name>@<marketplace>` / `server:<name>`），是 Anthropic 官方权限透传机制 | `claude --channels --help` 输出 |
| F5 | AskUserQuestion tool_use **出现在 stream-json 输出中**，可被拦截检测 | 本机探针：`TOOL_USE: name=AskUserQuestion, input_keys=['questions']` |

---

## 1. 用户故事（User Stories）

**故事1：飞书用户回答 CC 提问（Phase 1）**
- **作为**：飞书用户（已绑定租户）
- **我希望**：当 CC 调用 `AskUserQuestion` 时，收到一张可点选的飞书卡片而非 `[Request interrupted]`，点选后 CC 能基于我的选择继续
- **以便**：CC 能根据我的选择工作，而不是中断
- **优先级**：P0

**故事2：飞书用户审批 CC 工具调用（Phase 2）**
- **作为**：飞书用户
- **我希望**：CC 执行 Bash / Write / Edit 等工具前，我能在飞书中看到工具详情并选择允许或拒绝
- **以便**：与终端使用 CC 有相同的审批控制权，防止误操作
- **优先级**：P2（依赖 CC Channels API，非 Relay 可独立实现）

**故事3：飞书用户审批 Codex 工具调用（Phase 2）**
- **作为**：飞书用户
- **我希望**：Codex 执行工具前，同样能在飞书中审批
- **以便**：不同 runtime 有统一的审批体验
- **优先级**：P2

**故事4：飞书用户超时未响应时获得合理回退**
- **作为**：飞书用户
- **我希望**：如果我忘记响应交互卡片，CC 不会永久挂起，而是超时后优雅降级
- **以便**：不因一次遗漏导致会话卡死
- **优先级**：P0

---

## 2. 功能需求（Functional Requirements）

### Phase 1 — AskUserQuestion A' 方案（P0）

**FR-P0-001：AskUserQuestion 拦截与飞书卡片渲染**
- **描述**：CC stream-json 输出中出现 `type==='tool_use' && name==='AskUserQuestion'` 的 content block 时（F5），Relay 拦截该 block，将其 `input.questions[]`（含 `question`、`header`、`multiSelect`、`options[].label+description`）渲染为飞书交互卡片。每个 option 一个可点按钮；`multiSelect` 题型支持多选 + 一个"提交"按钮。
- **前置**：将 `AskUserQuestion` 从 `DISALLOWED_TOOLS`（pool.ts:109）中移除，CC 才会调用该工具。当前代码全局禁用了该工具。
- **当前行为**：AskUserQuestion 被禁用。若被调用，CC 同步自答 `is_error` tool_result，输出文本含中断语义（F1）。
- **来源**：故事1、MVS.md、decision-2026-05-23
- **示例**：CC 调用 AskUserQuestion 询问"使用哪个分支？" → 飞书卡片显示 [main] [dev] [staging] → 用户点 [dev]

**FR-P0-002：A' 回注机制（新文字 prompt）**
- **描述**：用户在飞书点选后，Relay **不回注 tool_result**（F1 证实 CC 同步自答后 turn 已结束，无注入窗口）。而是将用户选择组装为**新一轮结构化文字 prompt**，经 `pool.send` 发送给同一 CC 进程。CC 在新 turn 中看到用户的文字回答并据此继续。
- **prompt 格式**：`[用户选择了以下选项]\n题目: {question}\n选择: {selected_labels}\n请根据用户的选择继续。`
- **时序**：CC 的 AskUserQuestion turn 结束（`is_error` tool_result + assistant 文本 + `result`）→ Relay 拦截 tool_use 渲染卡片 → 用户点选 → Relay 发送新文字 prompt → CC 开始新 turn
- **来源**：decision-2026-05-23 A' 方案
- **示例**：用户点 [dev] → `pool.send(poolKey, {type:'human', content: '[用户选择了以下选项]\n题目: 使用哪个分支？\n选择: dev\n请根据用户的选择继续。'})` → CC 在新 turn 中理解选择

**FR-P0-003：Pending 状态持久化**
- **描述**：拦截到 AskUserQuestion tool_use 时，将 pending 状态持久化到 `PersistedSession`，包含：
  - `pendingToolUseId`：tool_use ID
  - `pendingType`：`"question"`（Phase 1 仅此类型）
  - `cardMessageId`：飞书卡片消息 ID（用于更新卡片状态）
  - `questionSnapshot`：题目快照（用于超时回退）
  - `expiresAt`：截止时间戳
- **来源**：故事4、MVS.md
- **重启恢复**：Relay 重启后从 StateManager 恢复 pending 状态。若已过期，执行超时回退（FR-P0-006）。

**FR-P0-004：挂起期 Busy Timer 延长**
- **描述**：检测到 pending AskUserQuestion 时，该 Agent 的 busy timer 延长到可配置窗口（默认 30 分钟）。挂起期间暂停 `busy-progress` 通知（用户已收到卡片，不需要重复提醒）。交互完成或超时后恢复正常 busy timer 行为。
- **来源**：MVS.md
- **配置**：`PoolOptions.pendingInteractiveTimeoutMs`（默认 1,800,000ms = 30 分钟）
- **Clear 路径**（任一触发即清除挂起态）：用户完成点选 / 超时回退 / `/new` 新会话 / `/stop` / Relay 重启 / 进程 crash

**FR-P0-005：卡片状态更新**
- **描述**：用户点选后，将 pending 卡片更新为"已选择 X"禁用态。飞书卡片更新 token 30 分钟有效，最多更新 2 次（MVS 已识别此约束）。超时回退时更新为"已超时"禁用态。

**FR-P0-006：超时回退**
- **描述**：pending AskUserQuestion 超过窗口（默认 30 分钟）未获完整响应时：
  - **飞书卡片**：更新为"已超时"禁用态
  - **CC 进程**：不做任何操作（CC 的 AskUserQuestion turn 已正常结束，进程处于 idle 状态）
  - **用户下次发消息**：CC 开始新 turn，正常对话
- **来源**：故事4
- **注意**：与原 MVS 的"回注非 error tool_result"不同。A' 方案下 CC turn 已结束，超时仅需清理 pending 状态 + 更新卡片，不需要也不应该向 CC 发送任何消息。

**FR-P0-007：多题状态机 + 统一 Submit 按钮**（v0.3.0 新增）
- **背景**：2026-06-14 运营官多题 AskUserQuestion 卡死。原 `claimPendingAsk` 是原子 get+delete，第一次点选就清状态——单题可用，多题时第二题点选收到"已过期/已回答"错误，用户被迫改用文字回复但文字路径无 pendingAsk 短路导致入队永不消费。
- **描述**：
  1. **状态机化**：`PendingAsk` 新增 `answeredQuestions: number[]`，跟踪已答题索引，并持久化到 `PersistedSession.pendingAsk`，Relay 重启后恢复。
  2. **Submit 按钮统一**：所有 AskUserQuestion 卡片（单题 + 多题）末尾都追加 Submit 按钮（callback `{ relay_ask_submit: toolUseId }`）。
  3. **claim 时机统一**：点选项按钮 → 永不 claim，仅 `injectInput(digit)` + 更新 `answeredQuestions`；点 Submit 按钮 → 检查 `answeredQuestions.length === questions.length`，未答完提示「请先答完所有题」，答完 `injectInput("\r")` + claim。
  4. **废弃「单题 digit submits」**：v0.2.0 路径 A 注释里"press digit selects + submits a single-select option"结论废弃。新行为：press digit 只勾选，必须显式点 Submit 提交。UAT 需重新验证 CC TUI 在单题下 press digit 后是否真的只 select。
- **来源**：故事1（多题场景）+ 2026-06-14 bug 复盘
- **不变项**：超时兜底（FR-P0-006 / `cleanupExpiredPendingAsks`）不变，5min 后 Esc + resume prompt 兜底；Submit 按钮未点也不会无限卡。

**FR-P0-008：文字路径识别 pendingAsk**（v0.3.0 新增）
- **背景**：FR-P0-007 修复多题按钮路径后，用户仍可能因按钮丢失/误操作改用文字回复。原文字消息 handler 不查 pendingAsk，typed answer 直接 `pool.send` → CC busy → 入队永不消费。
- **描述**：飞书文字消息 handler 在 `pool.send` 前插入 pendingAsk 短路：
  1. 若 `hasPendingAsk(poolKey)` 为真，尝试把文字匹配到当前第一个未答题的选项：
     - 纯数字（"3"）→ oi = num - 1
     - 「选项 3」「3.」「3、」→ oi = num - 1
     - label 子串（"底座+可替换" 命中 "底座+可替换案例层"）→ oi
  2. 匹配上 → 调用与按钮路径相同的 `applyAskAnswer`（injectInput digit + 更新 answeredQuestions），不进 `pool.send`。
  3. 不匹配 → 发提示「请点击卡片按钮，或回复 Q{qi+1} 的选项编号：1 xxx / 2 yyy / ...」，不进 `pool.send`（避免污染 CC 输入）。
  4. 文字路径**不识别**「submit / 提交 / 确认」等关键词——用户要提交就点 Submit 按钮，避免误判。
- **来源**：故事1（文字回复场景）+ 2026-06-14 bug 复盘
- **边界**：纯 `/` 命令（如 `/stop` `/new`）路径不变，仍优先于 pendingAsk 短路。

### Phase 2 — CC 工具权限审批透传（P2）

> **为什么是 P2 而非 P0**：CC 2.1.104 探针证实，`-p` 模式下 `--permission-mode default` 自动审批所有工具（含 `rm -rf`），与 `bypassPermissions` 等价（F2）。`-p` stream-json 无 `permission_request` 事件（F3）。**在现有 `-p` pool 架构上无法拦截工具审批**。实现真正的审批透传需要 CC Channels API（F4），这是一个独立的架构层，不能在现有 pool 上叠加。

**FR-P2-001：CC Channels API 集成**
- **描述**：为需要工具审批的 Agent 建立独立的 Channels session（`claude --channels server:relay-feishu`），通过 MCP 协议接收 `permission_request` 通知，渲染飞书审批卡片，收集用户响应后回注 `permission` verdict。与现有 `-p` pool 并行运行，按 Agent 配置选择使用哪个路径。
- **来源**：故事2、research-relay-permission-passthrough.md Phase 2 建议
- **参考实现**：[anthropics/claude-plugins-official/external_plugins/telegram](https://github.com/anthropics/claude-plugins-official/tree/main/external_plugins/telegram)（`server.ts` permission relay）
- **架构**：
  ```
  Relay 进程
  ├── 现有 pool（-p，批量/interactive 保留，bypass 模式）
  └── 新 ChannelBridge（每 tenant 或每 session）
        ├── 启动 claude --channels server:relay-feishu
        ├── MCP server 声明 claude/channel + claude/channel/permission
        ├── 出站 permission_request → 飞书审批卡片
        └── 入站 callback → notifications/.../permission verdict
  ```
- **前置**：CC ≥ v2.1.81；GrowthBook flag `tengu_harbor_permissions`；claude.ai OAuth（纯 API key 不支持 Channels）

**FR-P2-002：租户安全基线（去 yolo）**
- **描述**：对租户 Agent 从 `--dangerously-skip-permissions` 切换到 `--permission-mode acceptEdits` 或 `dontAsk` + 显式 `permissions.allow` 规则。不做飞书审批 UI，但将"静默 auto-approve 一切"改为"显式 policy + fail-fast"。保留 Pulse 基础设施 Agent 的 yolo。
- **来源**：research-relay-permission-passthrough.md Phase 0 建议
- **注意**：此改动可在 Phase 1 之前独立完成，不影响 AskUserQuestion 功能。CC 探针证实 `-p` 模式下 `default` 权限模式也是自动审批（F2），所以切换到 `acceptEdits` 不会导致 hang，仅改变 CC 的内部 policy 判断。

### Phase 2b — 多通道 & Codex（P2）

**FR-P2-003：Telegram 通道支持**
- **描述**：将 AskUserQuestion 交互卡片适配到 Telegram（Inline Keyboard 按钮）。飞书 UAT 通过后再对齐 TG。
- **来源**：故事1
- **优先级**：P2

**FR-P2-004：Codex 工具审批**
- **描述**：Codex runtime 的工具审批拦截。Codex 使用 `--dangerously-bypass-approvals-and-sandbox` 跳过审批；Codex 的 PermissionRequest hooks（PR #17563）可能提供结构化审批接口。需独立探针确认。
- **来源**：故事3
- **优先级**：P2

---

## 3. 验收标准（Acceptance Criteria）

### Phase 1 验收标准

**AC-P0-001：AskUserQuestion 拦截与卡片渲染**
- **条件**：CC 输出含 `AskUserQuestion` tool_use block
- **预期结果**：
  - Relay 拦截该 block，解析 `questions[]`
  - 飞书用户收到可交互卡片，显示问题、选项、描述
  - 单选题 / 多选题：点选项按钮 = 勾选；点 Submit 按钮 = 提交（v0.3.0 统一行为）
- **验证方式**：Unit Test（tool_use 拦截 + 卡片 JSON 生成 + Submit 按钮存在）

**AC-P0-002：A' 回注端到端（飞书）**
- **条件**：飞书用户在 AskUserQuestion 卡片上点选
- **预期结果**：
  - 飞书 `card.action.trigger` 回调被接收
  - Relay 组装结构化文字 prompt（含题目 + 选择）
  - `pool.send` 发送新 human 消息
  - CC 在新 turn 中理解用户选择并继续
  - 飞书卡片更新为"已选择 X"禁用态
  - 用户收到 CC 后续回复
- **验证方式**：Integration Test + 真实飞书 UAT

**AC-P0-003：AskUserQuestion 移除 DISALLOWED_TOOLS**
- **条件**：移除 `pool.ts:109` 的 `DISALLOWED_TOOLS` 中 `AskUserQuestion` 条目
- **预期结果**：CC spawn args 不含 `--disallowed-tools AskUserQuestion`；CC 调用 AskUserQuestion 时 tool_use block 出现在 stream-json 输出中
- **验证方式**：Unit Test（spawn args 断言）

**AC-P0-004：Pending 状态持久化与重启恢复**
- **条件 A**：AskUserQuestion 待响应时，Relay 正常重启
- **预期结果 A**：重启后 pending 卡片已过期 → 执行超时回退 → 卡片更新为"已超时"
- **验证方式**：Unit Test（StateManager 写入/恢复）

**AC-P0-005：超时回退**
- **条件**：用户 30 分钟内未响应 AskUserQuestion 卡片
- **预期结果**：
  - 飞书卡片更新为"已超时"
  - CC 进程不被 kill（turn 已正常结束）
  - busy timer 恢复正常行为
  - pending 状态被清除
- **验证方式**：Unit Test（超时路径）

**AC-P0-006：非 AskUserQuestion tool_use 不受影响**
- **条件**：CC 输出 Bash / Write 等其他 tool_use block
- **预期结果**：行为与当前完全一致（Relay 忽略非 AskUserQuestion 的 tool_use block，仅提取 text）
- **验证方式**：Unit Test（回归保护）

**AC-P0-007：AskUserQuestion 自答文本被过滤**
- **条件**：CC 自答 AskUserQuestion 后输出 assistant 文本（如 "I've asked the question..."）
- **预期结果**：该文本被 `stripCCNoise` 过滤或以其他方式不透传给飞书用户（避免用户看到无意义的中间文本）
- **验证方式**：Unit Test（`CC_NOISE_PATTERNS` 更新）

**AC-P0-008：多题状态机 + Submit 路径**（v0.3.0 新增）
- **条件**：CC 输出含 2+ 题的 AskUserQuestion tool_use
- **预期结果**：
  - 卡片显示所有题选项 + 末尾 Submit 按钮
  - 点 Q1 选项 → `injectInput(digit)` 调用 1 次，`answeredQuestions=[0]`，`hasPendingAsk` 仍为 true
  - 再点 Q2 选项 → `injectInput(digit)` 调用第 2 次，`answeredQuestions=[0,1]`，**pendingAsk 仍在**（不自动 submit）
  - 点 Submit → `injectInput("\r")` + claim，pendingAsk 清除
  - Submit 在未答完时点击 → 提示「还有 N 题未答」，不 claim、不 inject
- **验证方式**：Unit Test M1~M4, M10

**AC-P0-009：文字路径 pendingAsk 短路**（v0.3.0 新增）
- **条件**：用户在 pendingAsk 期间发文字消息（非 `/` 命令）
- **预期结果**：
  - 纯数字「3」→ 命中当前未答题的 option index 2 → 调 `applyAskAnswer` 注入 digit "3"
  - 「选项 3」/「3.」/「3、」→ 同上
  - label 子串「底座+可替换」→ 命中含此子串的 option → 注入对应 digit
  - 不匹配任何选项 → 发提示「请点击卡片按钮，或回复 Q{qi+1} 的选项编号...」，**不调 `pool.send`**（CC 不被打扰）
- **验证方式**：Unit Test M5~M7

### Phase 2 验收标准（工具权限）

**AC-P2-001：CC Channels 权限审批端到端**
- **条件**：Agent 使用 Channels 路径，CC 欲执行 Bash 工具
- **预期结果**：
  - MCP `permission_request` 通知被接收
  - 飞书卡片显示工具名 + 参数
  - 用户点 [允许] → CC 执行工具 → 后续输出正常
  - 用户点 [拒绝] → CC 跳过工具 → 继续对话
- **验证方式**：Integration Test + 手动 UAT

**AC-P2-002：租户去 yolo 回归**
- **条件**：Agent 配置 `--permission-mode acceptEdits`（无 `--dangerously-skip-permissions`）
- **预期结果**：CC 正常工作，不 hang（F2 已证实）；未 allow 的操作 fail-fast 并在输出中可见
- **验证方式**：Integration Test（生产租户端到端）

---

## 4. 非功能需求（Non-Functional Requirements）

### 性能需求

- **卡片送达延迟**：CC 输出 AskUserQuestion tool_use → 飞书卡片送达 ≤ 5 秒
- **回注延迟**：用户点选 → `pool.send` 新文字 prompt ≤ 3 秒
- **并发 pending**：支持同时 N 个 Agent 处于 pending 状态（N = 活跃进程数上限）

### 安全需求

- **回调验证**：飞书 `card.action.trigger` 回调必须验证 signature，防止伪造选择
- **权限隔离**：交互卡片仅对绑定该 Agent 的飞书用户可见（飞书私聊天然隔离）

### 可靠性需求

- **无死等**：所有 pending 状态必须有超时回退路径，不得无限挂起
- **重启安全**：Relay 重启后 pending 状态可恢复或优雅降级
- **Backward compatible**：现有 Agent 行为零影响 — AskUserQuestion 当前已被 `--disallowed-tools` 禁用，本 feature 仅放开该工具并添加拦截逻辑

---

## 5. 约束条件（Constraints）

**CONSTRAINT-001：AskUserQuestion 不可回注 tool_result（硬约束）**

CC 2.1.104 + 2.1.143 探针均证实：CC `-p` stream-json 模式下，AskUserQuestion 被调用后 CC **同步自答** `is_error: true` tool_result 并 `end_turn`。stdin 回注的 tool_result 被当成新一轮 prompt 消费，CC 已丢失原 tool_use 上下文。**不可绕过，除非 CC 未来版本暴露挂起等待外部工具结果的能力**（见 decision-2026-05-23 探针记录）。

**CONSTRAINT-002：CC `-p` 模式无工具审批事件（硬约束）**

CC 2.1.104 探针证实：`-p` stream-json 模式下，无论 `--permission-mode` 设为 `default`、`dontAsk` 还是 `bypassPermissions`，CC 均自动审批所有工具调用（含 Bash `rm -rf`、Write）。不输出 `permission_request` 事件。不挂起。**在 `-p` pool 架构上无法实现工具审批透传**，唯一路径是 Channels API（F4）。

**CONSTRAINT-003：A' 方案为"近似体验"而非"完美透传"**

A' 方案（新文字 prompt 回注）让 CC 在新 turn 中理解用户选择，但**不是原 turn 的 tool_result**。CC 可能：
- 在自答 turn 中输出过渡文本（如 "I've asked the question..."），需过滤
- 在新 turn 中不完美理解选择（依赖 CC 对文字 prompt 的理解能力）
- 如果 CC 在自答后还做了其他操作，新 turn 的选择可能"迟到"

这是当前 CC 架构限制下的最优解。完美透传需要 CC 在 `-p` 模式下支持"挂起等待外部 tool_result"，目前无此能力。

**CONSTRAINT-004：Channels API 需 claude.ai OAuth**

CC `--channels` 模式要求 claude.ai OAuth 认证，纯 API key 不支持。对使用 API provider（如 OpenRouter）的租户，Channels 路径不可用。这些租户的工具审批需要等 CC 扩展 Channels 对非 Anthropic 后端的支持，或等 CC 在 `-p` 模式暴露 permission 事件。

**CONSTRAINT-005：飞书卡片更新限制**

飞书卡片更新 token 30 分钟有效，最多更新 2 次。超时回退更新可能因 token 过期失败（MVS 已识别此约束）。

---

## 6. 排除范围（Out of Scope）

- ❌ tool_result 回注续接同一 CC turn（F1 证实不可能）
- ❌ 在现有 `-p` pool 上实现工具审批 UI（F2 证实 `-p` 自动审批）
- ❌ CC TUI 完整渲染（Relay 不是终端模拟器）
- ❌ 代码 diff 预览（审批卡片仅显示工具名 + 参数摘要）
- ❌ PTY stdout 正则匹配审批提示（脆弱、版本强耦合，research 不推荐）
- ❌ 多用户审批工作流（单人审批/拒绝即可）
- ❌ Codex 审批（Phase 2b，需独立探针）
- ❌ CC 其他内置交互（context continuation、session recovery）— 未来可扩展
