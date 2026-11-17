---
feature_name: "telegram-channel"
version: "0.6.4"
created: "2026-04-07T10:30:00+0800"
modified: "2026-09-29T10:05:00+0800"
upstream_dependency: ["project-vision.md", "project-architecture.md"]
tags: [requirements, l2-spec, telegram, channel, router, relay-m2]
changelog:
  - version: "0.6.4"
    date: "2026-09-29"
    note: "退役 /wrap 作为 /new 前置；普通会话直接换会话，活动 WI 保留独立授权守卫"
  - version: "0.6.3"
    date: "2026-09-29"
    note: "收窄 /wrap 请求识别；明确阻断回执须撤销请求标记，失败不能放行 /new"
  - version: "0.6.2"
    date: "2026-09-10"
    note: "generated 标记改为在权威 final 之后新发于聊天底部，并尽力清理较早的进度消息"
  - version: "0.6.1"
    date: "2026-09-08"
    note: "记录 Rich Message 移动端与桌面端生产 UAT 通过"
  - version: "0.6.0"
    date: "2026-09-06"
    note: "恢复 Telegram 可见执行步骤计数，并与飞书共用 accepted-turn 计数和 milestone 文案"
  - version: "0.5.0"
    date: "2026-09-05"
    note: "收敛 Telegram 回合状态文案，以差异化 Emoji 准确区分开始、处理中、回复生成、重试与投递失败"
  - version: "0.4.0"
    date: "2026-09-04"
    note: "增加共享 RichDoc、安全 HTML 降级与可独立 rollout 的 Rich Message HTML 原生表格"
  - version: "0.3.0"
    date: "2026-08-25"
    note: "同步统一 channel rendering 语义与 /model、/think 原生 inline keyboard，明确单一 final、可见进度、回放幂等和 opaque callback 边界"
  - version: "0.2.1"
    date: "2026-07-20"
    note: "收窄 session wrap guard：只在显式 /new 重置时检查 /wrap，普通文本始终转发给 runtime"
  - version: "0.2.0"
    date: "2026-04-29"
    note: "同步代码实现：新增 429 重试、语音转录、图片/文件处理、hard-wrap recovery、session tail 读取、/compact 生命周期、activity-based timeout、typing indicator、session-end 通知、runtime fallback 通知"
  - version: "0.1.0"
    date: "2026-04-15"
    note: "更新路由模型：tenants.json → relay.json，单 bot → 多 bot（独立 bot 模式）"
  - version: "0.0.1"
    date: "2026-04-07"
    note: "初始版本"
---

# telegram-channel - 功能需求文档

> **功能概述**：实现 Telegram Bot 消息收发 + 多 Agent 路由，将 TG 用户消息转发给对应的 CC/Codex 子进程处理，并将回复送回 TG。支持文本、图片、语音、文件等多种消息类型，包含完善的错误恢复和会话生命周期管理。
>
> **关联文档**：
> - L1 项目愿景：`project-specs/project-vision.md`
> - L1 项目架构：`project-specs/project-architecture.md`
> - L2 技术设计：`features/telegram-channel/design.md`
> - L2 任务拆分：`features/telegram-channel/tasks.md`
> - 跨通道渲染契约：`channel-rendering-contract.md`
> - 上游依赖：`features/cc-process-pool/` (已完成)

---

## 1. 用户故事（User Stories）

**故事1：用户通过 TG 与 AI 对话**
- **作为**：Telegram 用户（已注册）
- **我希望**：在 Telegram 中发送文本消息，收到 AI 助手的回复
- **以便**：无需使用终端即可与个性化的 AI 助手交互
- **优先级**：P0

**故事2：未注册用户收到引导**
- **作为**：未注册的 Telegram 用户
- **我希望**：给 bot 发消息时收到"请联系管理员注册"的提示
- **以便**：知道如何获得服务，而不是被无响应困惑
- **优先级**：P0

**故事3：长文本回复自动分片**
- **作为**：Telegram 用户
- **我希望**：当 AI 回复超过 TG 单条消息长度限制时，自动拆分为多条消息
- **以便**：完整阅读 AI 的回复，不因平台限制丢失内容
- **优先级**：P0

**故事4：对话中 CC 崩溃时用户收到通知**
- **作为**：Telegram 用户
- **我希望**：当 AI 后端出错时收到友好的错误提示（而非无响应）
- **以便**：知道系统正在恢复，稍后可重试
- **优先级**：P1

**故事5：用户发送图片获得 AI 分析**
- **作为**：Telegram 用户
- **我希望**：发送图片给 bot，AI 能分析图片内容
- **以便**：利用多模态能力
- **优先级**：P1

**故事6：用户发送语音消息获得文字回复**
- **作为**：Telegram 用户
- **我希望**：发送语音消息后，AI 自动转录语音并回复
- **以便**：免打字直接对话
- **优先级**：P1

**故事7：用户发送文件获得 AI 处理**
- **作为**：Telegram 用户
- **我希望**：发送 PDF、文本文件等，AI 能读取并处理
- **以便**：直接在对话中处理文件内容
- **优先级**：P1

**故事8：会话上下文满时用户收到恢复引导**
- **作为**：Telegram 用户
- **我希望**：当 session context 满了或 compact 失败时，收到清晰的恢复指引
- **以便**：不会迷失在损坏的 session 中
- **优先级**：P1

**故事9：用户看到 AI 正在输入的提示**
- **作为**：Telegram 用户
- **我希望**：AI 处理消息时，TG 聊天界面显示"正在输入..."
- **以便**：知道系统在工作而非忽略我
- **优先级**：P2

**故事10：长任务持续提供可读进度并有唯一终态**
- **作为**：Telegram 用户
- **我希望**：长任务先立即显示可见状态，再在同一进度消息中演进，完成时在聊天底部收到一份权威最终答复
- **以便**：能确认 Agent 仍在工作，同时不会收到前缀、完整正文和 fallback 的重复副本
- **优先级**：P1

**故事11：通过原生按钮选择模型和思考强度**
- **作为**：Telegram 用户
- **我希望**：发送 `/model` 或 `/think` 后直接点 inline keyboard 完成选择
- **以便**：无需复制命令，并得到与飞书一致的配置语义
- **优先级**：P1

**故事12：富文本表格在 Telegram 客户端原生渲染**
- **作为**：Telegram 用户
- **我希望**：AI 回复中的安全 Markdown 表格显示为客户端原生表格，而不是管道式源码
- **以便**：能直接比较多行多列数据，同时保留代码、链接和流式回复体验
- **优先级**：P1

---

## 2. 功能需求（Functional Requirements）

### P0 功能（MVP 必需）

**FR-P0-001：Telegram Bot 启动 (long polling)** [x]
- **描述**：使用 grammy 框架启动 Telegram Bot，以 long polling 模式接收消息。每个 Agent 可配置独立 botToken，拥有独立 Bot 实例（独立 bot 模式）
- **来源**：故事1

**FR-P0-002：多 Agent 路由（chat_id → agent）** [x]
- **描述**：收到 TG 消息后，通过 `resolveByBotAgent(agentId, chatId)` 查找用户和 Agent 配置。未找到则回复引导注册消息。使用 gateway 路由策略做二次验证
- **来源**：故事1、故事2

**FR-P0-003：消息转发（TG → CC/Codex）** [x]
- **描述**：将 TG 文本消息封装为 stream-json 格式 `{type:"human", content:text}`，通过 CommandRuntime 的 `send()` 发送给对应 Agent 的子进程。若进程不存在或已 dead，自动 spawn。spawn 时使用持久化的 runtime 和 codexThreadId
- **来源**：故事1

**FR-P0-004：回复转发（CC → TG）** [x]
- **描述**：监听 CommandRuntime 的 `response` 事件，将 `type:"assistant"` 消息的 content 发送回 TG。过滤掉 `type:"result"` 等非用户可见消息。对回复文本进行 CC noise 清理（stripCCNoise），过滤 OAuth 诊断等内部信息
- **来源**：故事1

**FR-P0-005：长文本分片** [x]
- **描述**：以显式 capability profile 的保守阈值做语义分片；当前阈值兼容旧 `sendMessage` 的 4096 字符限制，不把它误报为 Rich Message 的平台硬上限。超长回复按共享 RichDoc 块边界依次发送
- **来源**：故事3

**FR-P0-006：relay.json 加载 + 独立 bot 模式** [x]
- **描述**：启动时从配置路径读取 relay.json，构建路由表。支持独立 bot 模式：每个配置了 botToken 的 Agent 创建独立 Bot 实例，`createTelegramBots()` 遍历所有 Agent 创建对应的 `createSingleBot()`
- **来源**：故事1、故事2

### P1 功能（增强功能）

**FR-P1-001：错误通知** [x]
- **描述**：监听 CommandRuntime 的 'exit'、'error'、'crash-retry'、'session-end' 事件。根据退出码和 session-end reason 向用户发送差异化通知（崩溃恢复、轮次耗尽、超时、限流等）。suppress 机制避免重复通知
- **来源**：故事4

**FR-P1-002：图片消息处理** [x]
- **描述**：接收 TG 图片消息（photo），下载最大分辨率图片，转换为 base64 发送给 CC（content block type=image）。对 Codex runtime 下载到临时文件发送。支持 caption
- **来源**：故事5

**FR-P1-003：语音转录（STT via CC）** [x]
- **描述**：接收 TG 语音消息（voice/audio），下载音频 buffer，通过 `transcribeAudio()`（使用 CC OAuth token）转录为文本，将转录文本作为 human message 发送给 CC/Codex。audio 类型额外支持 caption
- **来源**：故事6

**FR-P1-004：文件/文档处理** [x]
- **描述**：接收 TG 文档消息（document），支持图片（image/*）、PDF（application/pdf）、文本（text/*）三种类型。下载文件转 base64 发送给 CC。文本文件先解码为 UTF-8 文本再发送。不支持的 MIME 类型返回提示
- **来源**：故事7

**FR-P1-005：429 错误重试机制** [x]
- **描述**：TG API 返回 429 (FloodWait) 时，解析 retry_after 参数，等待指定秒数后重试（默认 1 次重试）。非 429 错误不重试
- **来源**：故事1

**FR-P1-006：Context recovery** [x]
- **描述**：当 session context 满了或 compact 失败时（通过 response 内容检测），自动标记 session 为 recovery-required。普通会话发送 `/new` 时检测 session tail（读取最后 256KB jsonl），开启新 session 并提示从旧日志接续。活动或待接续 WI 仍由独立的 SCL 绑定守卫拒绝直接换会话，不以 `/wrap` 请求标记放行
- **来源**：故事8

**FR-P1-007：Session tail 读取** [x]
- **描述**：在 hard-wrap recovery 流程中，读取 CC session jsonl 文件的最后 256KB（`SESSION_TAIL_BYTES`），检测 compact 失败或 context-window-limit 错误。文件路径基于 `~/.claude/projects/<cwd-encoded>/<sessionId>.jsonl`
- **来源**：故事8

**FR-P1-008：历史 `/wrap` 请求标记兼容** [~]
- **描述**：旧 `wrapRequestedAt` 只记录明确 `/wrap` 请求，不能证明收口成功，也不再决定 `/new` 准入。历史入口暂保留请求标记与阻断回滚以解释旧状态；`/new` 重置时清除旧标记。活动 WI 的会话替换由 SCL 绑定事实独立裁决。历史 `/wrap` 成功路径不再是当前 UAT 门
- **来源**：故事8

**FR-P1-009：Session-end 通知** [x]
- **描述**：监听 session-end 事件，根据 reason type 发送差异化通知：
  - turns-exhausted：提示轮次上限，发新消息自动恢复
  - crash：提示已自动恢复
  - crash-unrecoverable：提示多次崩溃暂停
  - rate-limited：提示限流，建议手动发消息继续
  - timeout：提示超时，建议手动发消息继续
  - relay-shutdown：提示 Relay 正在重启
  - idle-reclaimed：静默（下次消息自动恢复）
- **来源**：故事4

**FR-P1-010：Runtime fallback 通知** [x]
- **描述**：当 conversation runtime 发生 fallback（如 CC 不可用切换到 Codex）时，向用户发送通知，说明从哪个 runtime 切换到哪个 runtime
- **来源**：故事4

**FR-P1-011：统一回合渲染与幂等终态** [x]
- **描述**：所有 runtime 的 `status/activity/delta/snapshot/commentary/final` 先进入共享 `ChannelTurnRenderingState`。首个可见帧发送为一条进度消息，后续中间帧优先 edit；edit 失败或 429 时只在内存合并，不追加中间副本。工具活动从 accepted-turn 边界开始精确计数，每个 `turn-effect` 只递增一次，边界外事件不归入下一回合；Telegram 与飞书在第 1、5、10 步及之后每 20 步显示相同的计数文案。Telegram 对开始、处理中、回复生成、自动重试和投递失败分别使用稳定的 `▶️`、`⏳`、`✅`、`🔄`、`⚠️` 状态标记；成功终态只能确认“本轮回复已生成”，不得推断业务任务已经完成。终态先在聊天底部发送一份权威 final，随后尽力删除较早的进度消息并在底部新发唯一 generated 标记；进度删除失败不得阻止底部标记。重复 event、streaming final 与 result replay 均不得二次发送。长文本只在终态按显式 capability profile 的语义边界拆分
- **来源**：故事10

**FR-P1-012：`/model`、`/think` 原生选项** [x]
- **描述**：复用共享命令层产生的飞书选项语义，将 `runtime`、`model_scope`、`model`、`think` 四类受审动作映射为 Telegram inline keyboard。callback data 只能是 `rm:` 加短期 opaque token；option state 仅存 Relay 内存，最多 128 条、10 分钟过期，并按 tenant/chat/agent 校验且一次消费。过期、重启、跨用户或重放只提示重新打开命令，不猜测旧选项
- **来源**：故事11

**FR-P1-013：Rich Message HTML 原生表格与降级** [x]
- **描述**：仅从共享安全化 RichDoc AST 生成 Rich Message HTML；20 列以内表格映射为 `<table>/<tr>/<th>/<td>`，通过 `sendRichMessage` 与 `editMessageText.rich_message` 投递，并关闭自动实体识别。确定性 capability/format 拒绝依次降级到旧 Rich HTML、conservative HTML、plain text；ambiguous write 不跨格式重发。新 envelope 使用独立 off/shadow/canary/on rollout，默认 off，且不要求 grammY SDK 升级
- **来源**：故事12、故事10

### P2 功能（未来增强）

**FR-P2-001：Typing indicator** [x]
- **描述**：在 runtime 接受 turn（turn-accepted 事件）时开始发送 TG typing action（每 4 秒刷新），在收到 response 或 exit 时停止。typing 只能补充 FR-P1-011 的可见状态帧，不能替代它
- **来源**：故事9

**FR-P2-002：不支持的消息类型处理** [x]
- **描述**：对 video、sticker 等不支持的媒体类型，返回友好提示"暂不支持该类型消息"
- **来源**：故事1

---

## 3. 验收标准（Acceptance Criteria）

### P0 功能验收标准

**AC-P0-001：端到端对话** [x]
- **条件**：已注册用户在 TG 发送文本消息
- **预期结果**：≤ 30 秒内收到 AI 回复；回复内容与消息上下文相关
- **验证方式**：Integration Test + 手动 UAT

**AC-P0-002：未注册用户引导** [x]
- **条件**：未注册 chat_id 发送消息
- **预期结果**：≤ 3 秒内收到"请联系管理员注册"的固定文本回复
- **验证方式**：Unit Test

**AC-P0-003：长文本分片** [x]
- **条件**：AI 回复超过当前 capability profile 的 rollover threshold
- **预期结果**：按共享语义边界拆分，每片不超过配置预算，总内容无丢失、重字或乱序
- **验证方式**：Unit Test

**AC-P0-004：路由正确** [x]
- **条件**：多 Agent 配置，不同 chat_id 发消息
- **预期结果**：每个消息路由到正确的 Agent workspace
- **验证方式**：Unit Test

**AC-P0-005：进程按需 spawn** [x]
- **条件**：已注册用户首次发消息（无活跃进程）
- **预期结果**：自动 spawn CC/Codex 进程；后续消息复用进程
- **验证方式**：Unit Test（mock pool）

### P1 功能验收标准

**AC-P1-001：图片消息端到端** [x]
- **条件**：用户发送图片
- **预期结果**：图片被下载、转 base64、发送给 CC；CC 回复基于图片内容
- **验证方式**：Integration Test

**AC-P1-002：语音消息端到端** [x]
- **条件**：用户发送语音消息
- **预期结果**：语音被下载、转录为文本、发送给 CC；CC 回复基于转录内容
- **验证方式**：Integration Test

**AC-P1-003：429 重试** [x]
- **条件**：TG API 返回 429 FloodWait
- **预期结果**：等待 retry_after 秒数后重试一次
- **验证方式**：Unit Test

**AC-P1-004：Hard-wrap recovery** [x]
- **条件**：CC 回复包含 context-window-limit 错误
- **预期结果**：session 被标记为 recovery-required，用户收到恢复提示
- **验证方式**：Unit Test

**AC-P1-005：Session-end 通知** [x]
- **条件**：CC turn 轮次耗尽
- **预期结果**：用户收到轮次耗尽通知 + 自动恢复提示
- **验证方式**：Unit Test

**AC-P1-006：累积快照与终态不重复** [x]
- **条件**：Pi 依次产生短前缀、包含短前缀的更长快照和同文 final，随后 result/retry 重放
- **预期结果**：中间过程只演进一条消息；聊天底部依次出现一份完整权威 final 和一个不含正文的完成标记；较早的进度消息在 Bot API 允许时被删除
- **验证方式**：Unit Test + 真实 Telegram UAT

**AC-P1-007：原生模型/思考选项安全执行** [x]
- **条件**：用户打开 `/model` 或 `/think` 并点击有效、过期、重放、跨 chat 的按钮
- **预期结果**：有效按钮只执行一次且命令语义与飞书一致；其余按钮不执行，并提示重新打开命令；callback data 不含 model id 或敏感正文
- **验证方式**：Unit Test + 真实 Telegram UAT

**AC-P1-008：Rich Message HTML 原生表格** [x]
- **条件**：RichDoc 与 Rich Message rollout 命中，回复含 20 列以内的 Markdown 表格、链接和代码块
- **预期结果**：桌面端和移动端把表格渲染为原生 rows/cells，链接与代码效果不退化；确定性拒绝退回 legacy Rich HTML，ambiguous write 不造成重复 final
- **验证方式**：Unit Test + 生产 Telegram UAT（iPhone 14 Pro Max / iOS 26.6.1 / Telegram 12.9.3；macOS 15.7.3 / Telegram Desktop 7.2.5）

---

## 4. 非功能需求（Non-Functional Requirements）

### 性能需求
- **端到端延迟**：用户发消息到收到回复 ≤ 30 秒（含 CC 处理时间）
- **Bot 启动时间**：grammy bot 从启动到 polling 就绪 ≤ 3 秒
- **429 重试延迟**：按 TG API 指定的 retry_after 等待，默认 30 秒

### 安全需求
- **Token 安全**：TG Bot token 和 CC OAuth token 通过配置文件/环境变量注入，不进 git
- **Callback 安全**：inline callback 只暴露短期 opaque token，不持久化 model id、正文或全文哈希
- **用户隔离**：不同 chat_id 路由到不同 CC 进程，无跨用户数据泄漏
- **Gateway 路由验证**：所有消息通过 gateway 二次验证，确保路由策略正确

### 可维护性需求
- **代码规范**：遵循 project-standards.md（TypeScript strict, kebab-case）
- **测试覆盖率**：核心模块（router, channel adapter）≥ 80%
- **单文件行数**：≤ 300 行（telegram.ts 因功能复杂可适当放宽）

---

## 5. 约束条件（Constraints）

### 技术约束
- **技术栈**：基于 project-architecture.md — Node.js ≥ 18 + TypeScript + grammy ^1.41
- **通信模式**：long polling（非 webhook）— 无固定域名/SSL
- **上游依赖**：cc-process-pool 模块（已实现）
- **STT 依赖**：语音转录需要 CC OAuth token（环境变量 `CLAUDE_CODE_OAUTH_TOKEN`）
- **文件路径**：Session tail 读取依赖 `~/.claude/projects/` 目录结构

### DNS 约束
- **IPv4 优先**：模块入口设置 `dns.setDefaultResultOrder('ipv4first')`，避免 TG API IPv6 连接问题

---

## 6. 排除范围（Out of Scope）

- 飞书渠道 — 属于独立 feature
- Webhook 模式 — 使用 long polling
- 视频处理 — 当前返回"暂不支持"提示
- Sticker 处理 — 当前返回"暂不支持"提示
- 月预算检查 — 属于 `metering` feature
- 租户自助注册（/start 自动注册）— 属于 v1.0

When canonical read-only entry proves the persisted work item terminal at the bound epoch, Relay MUST retire only its active workspace/usage binding before restarting the conversation. Preserve the native session id, transcript, historical attribution and all pending unrelated work. Resume text and media in the configured conversation workspace without granting the terminal WI execution authority. Unknown state, pending entry, identity drift or failed durable persistence MUST NOT silently unbind. Startup errors MUST NOT universally instruct session replacement or recovery.
