---
feature_name: "conversation-runtime"
version: "0.1.3"
created: "2026-04-25T11:45:00+0800"
modified: "2026-06-01T14:30:44+0800"
changelog:
  - "v0.1.3: 150-spec-sync — FR-P0-002 示例更新为模型绑定 resolveCCMode（GLM→interactive，其他→print；Agent ccMode 可覆盖）"
  - "v0.1.2: 150-spec-sync — CC runtime busy timeout 改为 busy-progress 通知（不再自动 kill）；AC-P0-006 更新"
  - "v0.1.1: 150-spec-sync — GLM context limit 修正为 200K（glm-* 前缀规则）；内置前缀规则明确列出"
upstream_dependency:
  - "specs/project-specs/project-vision.md"
  - "specs/project-specs/project-architecture.md"
  - "specs/project-specs/project-standards.md"
  - "specs/project-specs/project-roadmap.md"
  - "specs/features/runtime-fallback/design.md"
tags: ["relay", "telegram", "runtime", "conversation", "codex", "cc"]
---

# conversation-runtime - Requirements

> 功能概述：把 Relay 的 Telegram 在线对话链路从 CC-only process pool 演进为可插拔 conversation runtime，使 CC 继续作为默认运行时，同时允许 Codex 作为另一个可选 TG 会话运行时。

## 1. 用户故事

### 故事1：保留 Relay+CC+TG 主链路

- 作为：Relay 运维者
- 我希望：引入 runtime 抽象后，现有 Telegram -> CC -> Telegram 多轮对话行为保持不变
- 以便：生产中的 13 个 bot 不因为架构重构发生行为回退
- 优先级：P0

### 故事2：Codex 作为 TG 会话运行时

- 作为：Relay 运维者
- 我希望：可以给某个 Agent 配置 Codex runtime，让用户继续通过 Telegram 对话
- 以便：当 CC provider/mode 不适合某类任务时，Relay 仍能提供另一条算力路径
- 优先级：P0

### 故事3：统一会话生命周期与可观测性

- 作为：Relay 开发者
- 我希望：TG channel 不感知 CC/Codex 细节，只消费统一的 `assistant/result/session-end` 事件
- 以便：typing、排队、超时、错误提示、状态查询可以复用
- 优先级：P0

### 故事4：附件兼容

- 作为：Telegram 用户
- 我希望：在 Codex runtime 下也能发送图片并得到回复
- 以便：Codex Agent 能覆盖与现有 CC Agent 接近的基础交互体验
- 优先级：P1

## 2. 功能需求

### P0 功能

**FR-P0-001：conversation runtime contract**

- 描述：定义统一的在线会话 runtime 接口，覆盖 `spawn/resume/send/kill/status/dispose` 与事件输出。
- 来源：故事1、故事3。
- 示例：TG 文本消息处理器调用 `runtimeManager.send(poolKey, message)`，不直接关心底层是 CC 长进程还是 Codex 每 turn 子进程。

**FR-P0-002：CC runtime 兼容适配**

- 描述：把现有 `pool.ts` 的 CC print/interactive 行为纳入默认 runtime 实现，保持当前 `claude -p`、PTY、session jsonl、token fallback、crash replay 语义。
- 来源：故事1。
- 示例：未配置 runtime 的 Agent 仍按 `resolveCCMode()` 自动选择：`ccModel` 所属 provider 声明 `force_interactive: true` → `interactive`；其他模型 → `print`；`AgentConfig.ccMode` 可显式覆盖。

**FR-P0-003：Codex text conversation runtime**

- 描述：新增 Codex runtime，支持 Telegram 文本消息多轮会话。首轮用 `codex exec` 创建 thread，后续用 `codex exec resume <thread_id>` 延续上下文。
- 来源：故事2。
- 示例：Agent 配置 `runtime: "codex"` 后，用户在 TG 连续发送 3 条文本，Codex 能记住前两轮内容。

**FR-P0-004：runtime selection config**

- 描述：为 `AgentConfig` 增加可选 runtime 配置。缺省值必须等价于当前 CC 行为。
- 来源：故事1、故事2。
- 示例：`relay.json` 中未配置 runtime 的 Agent 仍走 CC；配置 `runtime: "codex"` 的 Agent 走 Codex。

**FR-P0-005：统一 TG event semantics**

- 描述：Codex runtime 必须输出与当前 TG channel 兼容的事件：`turn-accepted`、`response(assistant)`、`response(result)`、`session-end`、`error`。
- 来源：故事3。
- 示例：TG channel 收到 Codex `assistant` 事件后，仍通过现有 `splitMessage()` 与 `sendWithRetry()` 推送到用户。

**FR-P0-006：错误分类与用户提示**

- 描述：Codex runtime 必须把 CLI error、`turn.failed`、timeout、rate-limit/auth/quota 信号映射到统一 session-end/error 行为，不允许静默失败。
- 来源：故事3。
- 示例：Codex 返回 JSONL `turn.failed` 时，TG 用户收到明确失败提示，Relay 日志记录 runtime、threadId、exitCode、error head。

### P1 功能

**FR-P1-001：Codex image attachment support**

- 描述：支持 TG photo 在 Codex runtime 下落临时文件并以 `codex exec -i <file> -- -` 发送。
- 来源：故事4。
- 示例：用户发送一张图片和 caption，Codex 收到图片文件与文本 prompt。

**FR-P1-002：runtime status visibility**

- 描述：`/session` 或等价状态命令显示当前 Agent runtime、session/thread id、状态、最近活动时间。
- 来源：故事3。
- 示例：Codex runtime 显示 `runtime=codex`、`threadId=...`，CC runtime 显示 `runtime=cc`、`sessionId=...`。

**FR-P1-003：controlled TG runtime switch command**

- 描述：Telegram 支持受控 `/runtime` 命令。`/runtime` 显示当前 Agent 配置 runtime 与 active session runtime；`/runtime cc` 或 `/runtime codex` 只切换当前 TG 用户当前 Agent 的 conversation runtime。
- 来源：生产 Codex canary 运维需求。
- 示例：用户发送 `/runtime cc` 后，Relay 将当前 Agent 的 `runtime` 写回 `relay.json`，并把 TG conversation `conversationRuntimePreference` 同步为 `["cc"]`，终止并清理当前 active conversation session；下一条消息使用 CC runtime。
- 边界：不得改 batch scheduler `runtimePreference`，不得改 token/chatId/workspace/sessionId，不做任意 Agent 切换或远程配置同步。

**FR-P1-004：CC 上下文软告警（模型感知，TG / 飞书）**

- 描述：当 **CC runtime**（非 Codex）单次 `result` 用量显示「估算输入上下文」超过配置比例时，Relay 触发 **软告警**：结构化日志 + 用户侧一条提示（建议 `/new` 等），不阻断对话。
- 估算口径：`usage.input_tokens + usage.cache_read_input_tokens`（不累加 `cache_creation_input_tokens`，避免 prompt caching 语义下重复计数高估）。
- 告警分母（上下文预算）：按 **模型 id** 解析。优先取 `result.model` 或 `usage.model`；若缺失则与 `pool.ts` 一致，回退 `RELAY_CLAUDE_MODEL` / `CLAUDE_CODE_MODEL`。解析顺序：`RELAY_MODEL_CONTEXT_LIMITS`（JSON 精确 key，小写）→ 内置规则（`claude-opus-4-7` 与 `opus` 1M、其他 `claude-*` 及 `sonnet`/`haiku` 200K、`gpt-*` 128K）→ `RELAY_CONTEXT_LIMIT_DEFAULT` → 默认 128000。
- 比例：`RELAY_CONTEXT_SOFT_WARN`（默认 0.8）；仅对 CC adapter 生效。
- 去重：每 `(tenantId, sessionId, limit)` 至多告警一次；`kill`/重启 manager 清理。
- UI：百分比与括号内 `currentSize/limit` 一致，**不得**人为封顶为 99%；可选展示事件中的 `modelId / 模型 …`。
- 来源：生产上多模型混用与飞书告警文案一致性。
- 示例：`claude-sonnet-4-0` 命中内置 `claude-*` 规则 200000 窗口；用户在飞书看到「已用 x%（a/b tokens，模型 claude-sonnet-4-0）」与 ratio 一致。

### P2 功能

**FR-P2-001：runtimePreference for TG conversation**

- 描述：支持 TG 会话 runtime fallback，例如 Codex 不可用时回到 CC 或反向 fallback。
- 来源：故事2。
- 示例：Agent 可配置 `conversationRuntimePreference: ["codex", "cc"]`；当 Codex 在 spawn 阶段失败且尚未产生 assistant 输出时，Relay 自动切到 CC 重试本轮。
- 边界：本切片只支持 spawn failure fallback；turn 已开始、已发送 assistant 或 turn-level error/timeout 后不自动 fallback，避免重复回复风险。

## 3. 验收标准

### P0 验收标准

**AC-P0-001：CC 行为零回退**

- 条件：Agent 未配置 runtime，发送 1 条 TG 文本消息。
- 预期结果：spawn 命令、输入协议、输出事件、session-end 行为与当前 CC pool 等价；现有 `pool.test.ts`、`telegram.test.ts`、`token-fallback.test.ts` 通过率为 100%。
- 验证方式：自动化单元/集成测试。

**AC-P0-002：Codex 首轮文本会话**

- 条件：Agent 配置 `runtime: "codex"`，TG 发送文本 `Reply exactly: CODEX_TG_EXPERIMENT_OK`。
- 预期结果：Relay 在 1 个 turn 内启动 Codex，记录 1 个 `thread_id`，TG 收到 `CODEX_TG_EXPERIMENT_OK`，`Job/turn` exitCode 为 0。
- 验证方式：mock CLI integration test + 本地 UAT。

**AC-P0-003：Codex resume 多轮会话**

- 条件：同一 `poolKey` 连续发送 4 条 TG 文本，其中第 3 条要求记住 marker，第 4 条要求复述。
- 预期结果：第 4 条回复准确包含第 3 条 marker；同一 `thread_id` 被复用 3 次以上。
- 验证方式：mock CLI integration test + 本地 UAT。

**AC-P0-004：TG channel 复用现有事件语义**

- 条件：Codex runtime 返回一次 assistant 文本并完成 turn。
- 预期结果：TG channel 收到 1 次 `assistant` 响应和 1 次 `result` 响应；typing 在 `turn-accepted` 后启动，在 assistant/result 后停止。
- 验证方式：`telegram.test.ts` 或新增 conversation runtime integration test。

**AC-P0-005：错误不静默**

- 条件：Codex CLI 返回 exitCode=1，并输出 JSONL `error` 或 `turn.failed`。
- 预期结果：Relay 结构化日志包含 `runtime=codex`、`threadId`、`exitCode=1`、`errorHead`；TG 用户在 1 次失败 turn 内收到失败提示；pending queue 不丢消息。
- 验证方式：mock CLI unit/integration test。

**AC-P0-006：busy-progress 通知（CC runtime）**

- 条件：CC 进程处于 busy 状态超过 `busyTimeoutMs`（默认 600s）。
- 预期结果：Relay emit `busy-progress` 事件（含 `checkCount`、`elapsedMs`、`isFinalCheck`），通道层向用户发送状态通知；**不**自动终止进程；用户可发 `/stop` 主动终止。
- 验证方式：fake timer unit test。

**AC-P0-006b：timeout 兜底（Codex runtime）**

- 条件：Codex 子进程超过 configured runtime timeout。
- 预期结果：Relay SIGTERM 后最多 5 秒 SIGKILL；触发 `session-end timeout`；当前 in-flight 被清理，后续消息可重新发起。（Codex runtime 保留原有超时行为不变）
- 验证方式：fake timer unit test。

### P1 验收标准

**AC-P1-001：Codex 图片输入**

- 条件：TG photo + caption 发给 Codex runtime。
- 预期结果：Relay 下载图片到临时文件，Codex CLI 参数包含 `-i <file> -- -`；turn 完成后临时文件在 60 秒内被删除。
- 验证方式：mock Telegram file download + mock CLI test。

**AC-P1-002：状态可见**

- 条件：Codex runtime 会话完成至少 1 turn 后调用 `/session`。
- 预期结果：状态文本包含 runtime 类型、thread id 前 8 位、state、lastActivity，且不泄漏 auth token。
- 验证方式：命令单元测试。

**AC-P1-003：CC 上下文软告警**

- 条件：CC runtime 收到带 Anthropic 风格 `usage` 的 `result`，且 `input_tokens + cache_read_input_tokens` ≥ `limit * RELAY_CONTEXT_SOFT_WARN`；`limit` 由当前模型 id 按 FR-P1-004 解析。
- 预期结果：`onContextSoftWarning` 仅触发一次（同 session + 同 limit）；事件含 `currentSize`、`limit`、`ratio`、`modelId`（若有）；飞书/TG 文案中百分比与 `currentSize/limit` 无矛盾（允许 >100% 当 `currentSize` 大于参考 `limit`）；Codex runtime 不产生该告警。
- 验证方式：`tests/conversation-runtime.test.ts`、`tests/model-context.test.ts`。

### P2 验收标准

**AC-P2-001：spawn failure fallback**

- 条件：Agent 配置 `conversationRuntimePreference: ["codex", "cc"]`，Codex adapter 在 spawn 阶段抛错，且本轮尚未产生 assistant 输出。
- 预期结果：manager 按 preference 顺序尝试 Codex 后尝试 CC；TG 用户收到 1 次明确 fallback notice；本轮消息发送给 CC；state 中该 session 的 `runtime` 记录为 `cc`。
- 验证方式：manager unit test + mock Telegram integration test。

**AC-P2-002：fallback 安全边界**

- 条件：spawn error 中包含 token/auth/key/secret 样式字符串。
- 预期结果：fallback event、结构化日志和 TG notice 不包含原始敏感值；notice 不透出错误详情。
- 验证方式：安全单元测试。

**AC-P2-003：turn-level failure 不 fallback**

- 条件：首选 runtime 已 spawn 成功并已发送 assistant，随后发生 error/session-end。
- 预期结果：不自动切换到下一 runtime，不重放本轮消息；沿用现有 error/session-end 提示。
- 验证方式：manager unit test；真实 TG UAT 留给后续生产切片。

## 4. 非功能需求

### 性能需求

- 文本 Codex turn 在正常网络下首条 TG 回复目标 ≤ 12 秒；实验观测文本 turn wall time 约 5-6 秒。
- CC 默认路径不得增加额外 runtime 分派进程；正常消息处理只多 1 层 TypeScript 方法调用。
- 单个 poolKey 同时最多 1 个 active turn，后续消息进入 FIFO queue。

### 安全需求

- 继续清理 `TELEGRAM_BOT_TOKEN` 与 `BOT_TOKEN_*`，不得把 bot token 暴露给 runtime 子进程。
- Codex runtime 默认使用 `-a never -s workspace-write -C <workspace> --skip-git-repo-check`。
- Codex runtime 不注入 `CLAUDE_CODE_OAUTH_TOKEN`；CC token fallback 只属于 CC runtime。

### 可用性需求

- 任何 runtime 关键路径失败必须产生至少 1 个可观测信号：结构化日志、TG 用户提示、`session-end` 事件三者至少一个。
- Relay 重启后，CC sessionId 与 Codex threadId 均可从 state 恢复；恢复失败时下一条消息创建新会话并记录 warn。

### 可维护性需求

- 遵循 `project-standards.md`：TypeScript strict、named export、结构化日志、核心模块测试覆盖 ≥ 80%。
- P0 实现不得扩大 `telegram.ts` 对 runtime 细节的感知；runtime 差异应留在 adapter/manager 层。

## 5. 约束条件

### 技术约束

- 基于 `project-architecture.md` 的 Node.js ≥ 18 + TypeScript + `child_process.spawn` 架构实现。
- 不新增生产依赖；测试可继续使用 vitest mock child_process。
- Codex CLI 实验基线为 `codex-cli 0.125.0`。
- Codex CLI 事件格式以 2026-04-25 本机实验为准：
  - `thread.started.thread_id`
  - `turn.started`
  - `item.completed.item.text`
  - `turn.completed.usage`
  - `error.message`
  - `turn.failed.error.message`

### 时间与资源约束

- P0 目标控制在 2-3 个主开发 session 内完成。
- P0 不要求一次性重写全部 `pool.ts`，允许先以 adapter façade 包住现有 CC pool 行为，再逐步拆分。

## 6. 排除范围

- 不在 P0 实现 TG conversation runtime fallback 链。
- 不在 P0 支持 Codex PDF/document content block；现阶段只确认 Codex CLI `-i` 图片输入。
- 不在 P0 实现 Codex 长驻 TUI/PTTY 驱动；Codex runtime 使用每 turn 子进程模型。
- 不把 scheduler batch runtime 与 TG conversation runtime 合并成一个接口；两者生命周期不同。
- 不改变现有 CC session jsonl repair、token fallback、interactive PTY 行为，除非为适配 contract 做无行为变化封装。
