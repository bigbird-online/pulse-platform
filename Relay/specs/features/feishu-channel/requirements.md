---
feature_name: "feishu-channel"
version: "0.4.6"
created: "2026-04-15T12:00:00+0800"
modified: "2026-09-10T11:57:00+0800"
upstream_dependency: ["project-vision.md", "project-architecture.md"]
tags: [requirements, l2-spec, feishu, channel, relay-v0.2]
---

# feishu-channel - 功能需求文档

> **功能概述**：基于 Lark SDK WebSocket 模式实现飞书消息收发，将飞书用户消息转发给对应的 CC 子进程处理，并将 CC 的回复以 Interactive Card 形式送回飞书。使飞书渠道租户（member-001 Eric、member-002 kuoran）接入 Relay。
>
> **关联文档**：
> - L1 项目愿景：`project-specs/project-vision.md`
> - L1 项目架构：`project-specs/project-architecture.md`
> - L2 技术设计：`features/feishu-channel/design.md`
> - L2 任务拆分：`features/feishu-channel/tasks.md`
> - **会员→租户（飞书 MVS）**：`features/tenant-onboarding-feishu/requirements.md`（与 `specs/pulse-product-strategy.md` 战略约束对齐）
> - **与 Agent 命令对等**：`features/bot-commands/requirements.md`（FR-P2-001 / FR-P2-002 ↔ FR-P1-005 / FR-P1-006；业务逻辑 `commands.ts`）
> - 上游依赖：`features/cc-process-pool/`（已完成）、`features/telegram-channel/`（已完成）
> - 参考实现：OC feishu plugin（`@larksuiteoapi/node-sdk` 使用模式）

---

## 1️⃣ 用户故事（User Stories）

**故事1：飞书用户与 AI 对话**
- **作为**：飞书用户（已注册租户）
- **我希望**：在飞书中给 Bot 发送文本消息，收到 AI 助手的回复（Markdown 渲染）
- **以便**：无需使用终端即可与个性化的 AI 助手交互
- **优先级**：P0

**故事2：飞书用户发送图片**
- **作为**：飞书用户
- **我希望**：给 Bot 发送图片，AI 能理解图片内容并回复
- **以便**：通过图片与 AI 交互（如识别文档、分析截图）
- **优先级**：P0

**故事3：未注册用户收到配对引导**
- **作为**：未注册的飞书用户
- **我希望**：给 Bot 发消息时收到配对码和绑定指引，管理员可通过已绑定 Agent 批准绑定
- **以便**：自助完成 Agent 绑定，无需管理员手动编辑配置文件
- **优先级**：P0

**故事4：飞书用户使用命令**
- **作为**：飞书用户
- **我希望**：通过飞书 Bot 菜单或输入 `/` 命令使用常用功能（/new、/status、/help 等）
- **以便**：管理我的对话（新建、查看状态等）
- **优先级**：P0

**故事5：长文本回复以卡片形式展示**
- **作为**：飞书用户
- **我希望**：AI 回复中的代码块、表格、加粗等 Markdown 格式正确渲染
- **以便**：清晰地阅读 AI 的回复
- **优先级**：P0

---

## 2️⃣ 功能需求（Functional Requirements）

### P0 功能（v0.2 必需）

**FR-P0-001：飞书 WebSocket 连接**
- **描述**：使用 `@larksuiteoapi/node-sdk` 的 `WSClient` 建立 WebSocket 长连接接收消息。每个飞书 Agent 创建独立的 WSClient 实例（appId + appSecret）。无需公网 URL 或 challenge 验证
- **来源**：故事1
- **验收**：启动 Relay → WSClient 连接成功 → 控制台打印 "Feishu bot {name} WebSocket connected"

**FR-P0-002：飞书用户路由（open_id → tenant）**
- **描述**：收到飞书消息后，用 `sender.open_id` 查询 relay.json 中用户的 openId 映射，找到对应的 UserConfig 和 AgentConfig。openId 支持两个层级：AgentConfig 级别（`agent.feishuOpenId`，per-app 优先）和 UserConfig 级别（`user.feishuOpenId`，多租户 fallback）。飞书 openId 是 per-app 分配的，同一用户在不同飞书 Bot 里 openId 不同，因此多 Agent 场景下必须使用 Agent 级别 openId。未找到则触发配对流程（FR-P0-009）
- **来源**：故事1、故事3
- **验收**：open_id 匹配 → 获取 workspace + sessionId；不匹配 → 返回配对码

**FR-P0-003：文本消息转发（飞书 → CC）**
- **描述**：将飞书文本消息（msg_type=text）提取纯文本内容，封装为 `{type:"human", content:text}` 通过 pool.send() 发给 CC 子进程。若进程不存在或已 dead，自动 spawn
- **来源**：故事1
- **验收**：用户发"你好" → pool.send(poolKey, {type:"human",content:"你好"})

**FR-P0-004：回复转发（CC → 飞书 Interactive Card）**
- **描述**：监听 pool 的 `response` 事件；assistant frame 先由 TG/飞书共享的 RichDoc compiler 从完整 authoritative snapshot 解析、安全化和语义切分，再映射为 Interactive Card（Schema 2.0）Markdown。原始 HTML 不执行，URL 采用共享 allowlist；命令卡、按钮和媒体不进入 RichDoc 路径
- **来源**：故事1、故事5
- **验收**：CC 回复含 `# Title\n```code```` 等内容 → 飞书收到正确渲染的卡片

**FR-P0-005：图片消息处理**
- **描述**：收到飞书图片消息（msg_type=image）后，通过 `im.messageResource.get` API 下载图片，转为 base64，封装为 `{type:"image", source:{type:"base64", media_type, data}}` content block 发给 CC
- **来源**：故事2
- **验收**：用户发图片 → CC 收到 image content block → AI 识别并回复

**FR-P0-006：命令处理**
- **描述**：飞书消息以 `/` 开头时，解析为命令（/start、/status、/new、/reset、/help、/stop、/kill、/restart、/session、/runtime、/yolo、/usage、/jobs、/job_add、/job_del、/job_run），复用现有 `commands.ts` 和 `job-commands.ts` 的 handler。飞书 Bot 菜单在开发者后台配置，点击时发送对应命令文本。**注意**：/list 和 /switch 在飞书独立 bot 模式下已禁用（每个 bot 绑定固定 Agent，无需切换）。**CC 内部命令透传**：`/compact`、`/context` 等 CC 内置斜杠命令不经过 `handleCommand` 拦截，直接作为文本消息发送给 CC 进程处理
- **来源**：故事4
- **验收**：用户发送 "/status" → 回复进程状态信息

**FR-P0-007：长文本分片**
- **描述**：单卡预算、计量单位、element budget、rollover threshold 和 update throttle 必须来自显式 capability profile；历史 4000 字符仅为待真实 UAT 校准的默认候选，不宣称平台硬上限。共享 splitter 优先完整语义块，超长 code/inline 结构 close/reopen，飞书按序 rollover 多张 CardKit 卡片
- **来源**：故事5
- **验收**：边界自动化 + 真实桌面/移动 UAT 共同证明配置值；超限回复无丢字、重字、乱序或非法卡 payload

**FR-P0-009：Agent 配对流程（Pairing）**
- **描述**：未注册用户给飞书 Bot 发消息时，系统自动生成 6 位配对码（大写字母+数字，排除易混淆字符 I/O/0/1），返回配对指引。配对码 5 分钟有效，同一 agent+openId 重复触发会覆盖旧码。管理员在已绑定的 Agent 中发送 `/approve <配对码>` 完成绑定，系统将 openId 写入 relay.json 并更新内存索引，无需重启 Relay
- **来源**：故事3
- **验收**：未注册用户发消息 → 收到配对码；管理员发 `/approve <code>` → 回复"已成功绑定"；绑定后用户下次发消息正常路由

**FR-P0-010：/approve 命令**
- **描述**：新增 `/approve <配对码>` 命令，仅限已绑定的飞书 Agent 使用。执行时验证配对码有效性，将会员 openId 与 Agent 关联并写入配置，同时更新 Gateway 内存索引（**主路径为 `relay.json` 的 `bindings[]` 显式记录**；历史文档中仅写 `feishuOpenId` 字段的表述与实现对齐见 `logical-gateway` + `tenant-onboarding-feishu` FR-S1-002）
- **来源**：故事3
- **验收**：有效配对码 → 绑定成功；无效/过期码 → 回复"配对码无效或已过期"；无参数 → 回复用法提示

**FR-P0-008：relay.json 配置模型扩展**
- **描述**：UserConfig 新增 `feishuOpenId` 字段（可选，多租户场景飞书用户共用同一 Bot 时使用）。AgentConfig 新增 `feishuOpenId` 字段（可选，per-app openId，多 Agent 场景下每个 Agent 独立 Bot 时使用）。AgentConfig 新增 `channel` 字段（"telegram" | "feishu"）和 `feishu` 块（appId + appSecret + sessionId）。校验：channel 字段与对应渠道块必须一致，一个 Agent 只能绑定一种渠道。飞书 Agent 的 `feishuOpenId` 为可选字段——启动时不强制要求（可通过 FR-P0-009 配对流程在运行时绑定）。`botToken`、`chatId`、`sessionId` 改为可选字段（飞书 Agent 不需要 TG 字段）
- **来源**：故事1
- **验收**：v1 配置（无 channel 字段）→ 自动识别为 telegram，向后兼容；飞书 Agent 无 openId → 启动不报错，等待配对

### P1 功能（增强功能）

**FR-P1-001：Streaming Card**
- **描述**：CC 多段 `assistant` 流式输出时，每帧从完整 authoritative snapshot 重新编译为共享 RichDoc，再使用飞书 **Card Kit streaming**（`cardkit/v1/cards` 创建 + `elements/.../content` 更新 + `settings` PATCH 结束流式）。长答按共享语义 chunk rollover 多卡；每轮在 `result` 时按序关闭。
- **来源**：故事5
- **优先级**：P1
- **状态**：已实现（`src/feishu-streaming-card.ts` + `channels/feishu.ts` `createFeishuStreamingSink`；参考 OpenClaw `streaming-card.ts` API 形态）
- **开关**：默认开启；设 `RELAY_FEISHU_STREAMING_CARD=0|false|off|no` 时回退为每段 assistant 发送完整静态 Interactive Card（原行为）。**国际版 Lark** 可通过 `FEISHU_OPEN_API_BASE` 指向 `https://open.larksuite.com/open-apis`（若与默认域名不一致）。
- **RichDoc 开关**：`RELAY_FEISHU_RICHDOC_MODE=legacy|off|shadow|canary|on`，与 Telegram 独立；默认 off，关闭不迁移 session/binding/WI。
- **降级**：RichDoc 新路径 start/update/server-closed 只合并最新 authoritative state，终态至多发送一份完整静态答案；definite format failure 按 CardKit Markdown → post Markdown → plain text 单向退化。旧路径保持原静态 Card fallback。
- **回答卡界面**：普通回答卡只包含回答所需的 Markdown/状态内容，不生成整答或代码块复制按钮。接受回合后，首个可见卡片帧必须与 Telegram 共用 `▶️ 已收到，开始处理…`；后续工具活动只显示共享执行步骤里程碑。成功终态必须在回答卡末尾显示共享 `✅ 本轮回复已生成`，且该标记只属于 presentation，不得进入权威 answer state 或 reply-context cache；重放 result 不得重复追加。CardKit keepalive 仅重放最新可见帧，不得追加旧 `任务执行中（已 XmYs）`、工具名或工具参数。飞书 CardKit JSON 2.0 的 Markdown 代码块没有原生复制 action slot，button `behaviors` 也没有客户端 clipboard 行为；禁止用独立 callback 按钮冒充 Telegram 的原生复制体验。既有 `relay_copy` / `relay_copy_code` callback 仅为已发旧卡保留兼容。菜单、问答和确认等业务交互按钮不受此限制。能力调研见 `../../PULSE-飞书卡片内容可复制优化需求.md`。

**FR-P1-002：富文本消息（post 类型）**
- **描述**：支持飞书富文本消息（msg_type=post），提取纯文本后转发 CC
- **来源**：故事1
- **优先级**：P1
- **状态**：已实现（`src/channels/feishu.ts: handleFeishuMessage` 支持 `msg_type=post`；`extractPostText()` 负责解析；单测覆盖见 `tests/feishu-channel.test.ts`）

**FR-P1-003：文件消息（PDF / 文档）**
- **描述**：支持飞书文件消息（`msg_type=file`）。解析 `content` JSON 中的 `file_key`（及可选 `file_name`），调用 `im.messageResource.get`（`params.type=file`）下载二进制；按 **Content-Type 与扩展名** 分类后，封装为与 Telegram `message:document` **同一套** CC content block（`image` / `document` / `text_file` + `text` 段落），经 `pool.send` 交给 CC。
- **来源**：故事2
- **优先级**：P1
- **状态**：已实现（落地见 `design.md` Flow-005；入口 `src/channels/feishu.ts`；协议细节可参考 OpenClaw `extensions/feishu/src/media.ts` `downloadMessageResourceFeishu`、**非**照搬其落盘/插件链路）
- **约束**：
  - **默认单文件上限**：环境变量 `RELAY_FEISHU_FILE_MAX_BYTES`（字节），缺省 25MiB；超限则提示用户，不送入 CC（防 OOM）。
  - **支持类型（与 Telegram document 对齐）**：`image/*`、`application/pdf`、`text/*`；其余 MIME 若扩展名可归为上述类型（如 `.pdf`、常见文本后缀）则放行，否则回复「暂不支持该文件类型」。

**FR-P1-004：语音 / 音频消息**
- **描述**：支持飞书 `msg_type=audio`。解析 `file_key`，通过 `im.messageResource.get`（`type=file`，与 OpenClaw `toMessageResourceType` 一致）下载音频字节；**与 Telegram 语音一致**：使用 `src/stt.ts` `transcribeAudio`（Anthropic STT + 本机 `ffmpeg`）转写为文本后，以 `{type:"human", content: text}` 送入 CC（含 `buildFeishuRuntimeText` 回复上下文）。凭证：`CLAUDE_CODE_OAUTH_TOKEN`。
- **优先级**：P1
- **状态**：已实现（`handleFeishuMessage` audio 分支；`MessageHandlerContext.transcribeInboundAudio`）
- **约束**：单段音频二进制上限与 `RELAY_FEISHU_FILE_MAX_BYTES`（缺省 25MiB）一致；空转写或 STT 失败时中文提示用户。

**FR-P1-005：Agent 点选切换（与 Telegram Inline 对等）**
- **描述**：多 Agent 场景下，**用户需求与 Telegram 一致**——需能通过 **点选** 切换活跃 Agent。**飞书独立 bot 模式下每个 bot 绑定固定 Agent，/list 和 /switch 已禁用**，此功能仅在多 Agent 共享单 bot 场景下有意义（当前不适用）。
- **优先级**：P1
- **状态**：不适用（飞书独立 bot 模式下无需切换；未来若有多 Agent 共享单 bot 场景再实现）
- **关联**：`features/bot-commands` **FR-P2-001**

**FR-P1-006：per-Agent CC 模型配置（与 bot-commands 对等）** [x]
- **描述**：与 `bot-commands` **FR-P2-002** 对齐：不同 Agent 配置不同 CC **`--model`**（或等价 spawn 参数）。**飞书与 Telegram 共用** `AgentConfig`，**无「仅飞书生效」的模型分叉**；飞书用户发消息时应与 TG 用户得到 **同一模型行为**。
- **优先级**：P1
- **实现**：`AgentConfig.ccModel` → `pool.ts` `appendModelArg()`；飞书 spawn 路径传入 `routing.ccModel`（与 TG 一致）
- **关联**：`features/bot-commands` **FR-P2-002**

**FR-P1-007：妙记链接自动识别与转写**
- **描述**：用户发送包含飞书妙记 URL（`*.feishu.cn/minutes/{token}`）的文本消息时，自动提取 `minute_token`，调用飞书 OpenAPI `GET /open-apis/minutes/v1/minutes/:token/transcript` 获取转写文本（`txt` 格式，包含说话人 `need_speaker=true` 和时间戳 `need_timestamp=true`），直接作为 `human` 消息送入 CC。不经过命令解析，不影响普通文本消息流程。
- **来源**：运营需求
- **优先级**：P1
- **状态**：已实现（`src/channels/feishu.ts` `FEISHU_MINUTES_URL_RE` + `handleMinutesTranscript`）
- **权限需求**：`minutes:minutes:readonly`、`minutes:minutes.transcript:export`
- **错误处理**：
  - 权限不足（2091005）→ 提示用户检查 Bot 权限
  - 转写未生成（2091003）→ 提示稍后重试
  - 其他错误 → 通用失败提示

**FR-P1-008：Runtime 出站文件显式投递**
- **描述**：只有 CC/Pi 明确写入当前 tenant `{workspace}/.relay/outbox/` 的顶层、非隐藏常规文件才可通过 `tool-files(tenantId, filePaths, workspace)` 投递。Pi 在 `agent_settled` 后单次扫描；普通 workspace `write/edit` 不表示用户可见附件意图。
- **安全约束**：事件必须携带产出文件的 exact runtime workspace（WI-bound session 不得退回 Agent 静态默认 workspace）；飞书 adapter 根据 tenant/workspace 独立复验 lexical/real path ownership。outbox 外文件、路径逃逸与目录一律不上传、不删除；仅已验证 outbox 文件在成功投递后可清理。
- **来源**：生产缺陷回放（普通 evidence 被上传、删除、恢复后重复上传）
- **优先级**：P1

## 3️⃣ 验收标准（Acceptance Criteria）

### P0 功能验收标准

**AC-P0-001：端到端对话（飞书文本）**
- **条件**：已注册飞书租户通过飞书 Bot 发送文本消息
- **预期结果**：≤ 30 秒内收到 AI 回复（Interactive Card 格式，Markdown 正确渲染）
- **验证方式**：Integration Test + 手动 UAT

**AC-P0-002：未注册用户配对引导**
- **条件**：未注册 open_id 给 Bot 发消息
- **预期结果**：≤ 3 秒内收到配对码和绑定指引文本
- **验证方式**：Unit Test

**AC-P0-003：图片消息端到端**
- **条件**：已注册用户发送图片
- **预期结果**：AI 识别图片内容并回复
- **验证方式**：手动 UAT

**AC-P0-004：命令响应**
- **条件**：用户发送 /status、/new、/help 等命令
- **预期结果**：各命令返回预期结果（进程状态、新建对话、帮助信息等）
- **验证方式**：Unit Test

**AC-P0-005：多租户隔离**
- **条件**：两个飞书租户同时发消息
- **预期结果**：各自路由到独立的 CC 进程，消息不串扰
- **验证方式**：Integration Test

**AC-P0-006：配置向后兼容**
- **条件**：使用 v1 格式 relay.json（无 channel 字段，无 feishu 块）
- **预期结果**：TG 渠道正常工作，无报错
- **验证方式**：现有 TG 测试全部通过

**AC-P0-007：单渠道绑定校验**
- **条件**：Agent 同时配置了 telegram 和 feishu 块
- **预期结果**：启动时报错并退出，日志明确提示"Agent can only bind one channel"
- **验证方式**：Unit Test

**AC-P1-001：飞书 Streaming Card**
- **条件**：启用流式卡片（默认），CC interactive 模式连续产出多条 `assistant` 后再 `result`
- **预期结果**：用户侧同一条卡片引用随输出更新；`result` 后流式结束（summary 更新）。关闭 `RELAY_FEISHU_STREAMING_CARD` 时行为与旧版「每条 assistant 一张完整卡片」一致。
- **验证方式**：`tests/feishu-channel.test.ts`（streaming sink 委托）；`tests/feishu-streaming-card.test.ts`（`mergeStreamingText`）；生产飞书 UAT（iPhone 14 Pro Max / iOS 26.6.1 / 飞书 V7.75.35-858036094；macOS 15.7.3 / Lark 143.0.7499.203）

**AC-P1-003：飞书文件消息（PDF / 文本）**
- **条件**：已绑定租户通过飞书 Bot 发送 `msg_type=file`（例如 PDF、小体积纯文本）
- **预期结果**：Relay 成功下载并完成类型判定后，`pool.send` 的 human 消息与 Telegram 文档路径一致（`document` / `text_file` / `image` 的 base64 source 或 UTF-8 文本块）；整文件不超过 `RELAY_FEISHU_FILE_MAX_BYTES`（缺省 25MiB）
- **验证方式**：`tests/feishu-channel.test.ts` 单测覆盖成功、超上限、不支持的 MIME/扩展名、下载失败提示

**AC-P1-004：飞书语音消息**
- **条件**：已绑定租户发送 `msg_type=audio`（含有效 `file_key`）
- **预期结果**：下载成功 → STT 非空 → `pool.send` human 内容为转写文本（可与回复链 `buildFeishuRuntimeText` 合并）；失败/空转写有明确中文错误提示
- **验证方式**：`tests/feishu-channel.test.ts` mock `downloadFileResource` + `transcribeInboundAudio`

**AC-P1-005：飞书 Agent 点选切换** [ ]
- **条件**：同一飞书用户名下注册 ≥2 个 Agent；用户收到带 **切换按钮** 的卡片（或等价 UI）
- **预期结果**：点击某一 Agent 后，后续消息路由到该 Agent（与 `router.switchAgent` / TG inline 语义一致）
- **验证方式**：单测（模拟点击 payload）+ 手动 UAT

**AC-P1-006：飞书与 TG per-Agent 模型一致** [x]
- **条件**：配置中两个 Agent 使用不同 `model`；同一用户分别从飞书与 TG 连接各自 Agent
- **预期结果**：spawn 参数反映各自配置，无渠道偏差
- **验证方式**：`ccModel` 全链路代码审查（`feishu.ts` spawn 传入 `routing.ccModel` 与 TG 一致）

**AC-P1-008：出站文件不误投、不破坏工作区**
- **条件**：Pi 在一个回合内写/改普通 evidence，并多次更新显式 outbox 文件
- **预期结果**：普通文件不产生附件且保持存在；outbox 文件仅在 `agent_settled` 后投递最终版本一次，成功后只删除该 outbox 文件
- **验证方式**：`tests/pi-conversation-runtime.test.ts`、`tests/tool-file-outbox.test.ts`、`tests/feishu-channel.test.ts`、`tests/pool.test.ts`（kill 保留未投递文件）+ 真实飞书移动端 UAT（普通文件不投递且保留，合法 outbox 仅投递并清理一次）

---

## 4️⃣ 非功能需求（Non-Functional Requirements）

### 性能需求
- **端到端延迟**：用户发消息到收到回复 ≤ 30 秒（含 CC 处理时间）
- **WebSocket 重连**：连接断开后 ≤ 30 秒自动重连（Lark SDK 内建）
- **卡片渲染**：单张卡片内容 ≤ 4000 字符（性能友好）

### 安全需求
- **凭证安全**：appId / appSecret 通过 relay.json 配置，不进 git（relay.json 在 .gitignore 中）
- **租户隔离**：不同 open_id 路由到不同 CC 进程，无跨租户数据泄漏
- **单渠道约束**：启动时校验 Agent 只绑定一种渠道

### 可维护性需求
- **代码规范**：遵循 project-standards.md（TypeScript strict, kebab-case）
- **测试覆盖率**：核心模块（channel adapter, config validation）≥ 80%
- **单文件行数**：≤ 350 行
- **复用 commands.ts**：飞书命令复用 TG 的命令 handler，不重复实现

---

## 5️⃣ 约束条件（Constraints）

### 技术约束
- **技术栈**：基于 project-architecture.md — Node.js ≥ 18 + TypeScript + @larksuiteoapi/node-sdk ^1.59
- **连接模式**：WebSocket（非 Webhook）— 无需公网 URL / challenge 验证
- **上游依赖**：cc-process-pool（已实现）、commands.ts（已实现）

### 设计约束
- **poolKey 不变**：保持 `userId:agentId` 格式。单渠道绑定保证唯一性
- **配置向后兼容**：v1 格式（无 channel 字段）自动识别为 telegram
- **依赖新增**：`@larksuiteoapi/node-sdk` 是本模块唯一新增的生产依赖（cumulative: 2, limit: ≤ 5）

### 时间约束
- **开发周期**：预计 3-4 天完成 P0 功能

---

## 6️⃣ 排除范围（Out of Scope）

- ~~❌ Streaming Card（实时更新）— P1~~ → 已实现（FR-P1-001）
- ~~❌ 飞书富文本消息（post）— P1~~ → 已实现（FR-P1-002）
- ~~❌ 飞书文件消息（PDF/文档）— P1~~ → 已实现（FR-P1-003）
- ~~❌ 语音/音频消息 — P1~~ → 已实现（FR-P1-004）
- ❌ 飞书群聊 — 当前只支持私聊（p2p）
- ❌ 飞书 Bot 菜单自动注册 — 在飞书开发者后台手动配置
- ❌ 用户自助换绑渠道 — 管理员通过修改配置 + 重启实现（注：新 Agent 绑定已通过 FR-P0-009 配对流程支持）
- ❌ 飞书工具集成（doc/wiki/drive/bitable）— 不在 Relay 范围

Conversation startup and restart recovery use the same canonical terminal-binding retirement as Telegram. A proved terminal WI releases its Relay workspace/usage binding while preserving native conversation history; unavailable proof or an outstanding entry does not release the binding.
