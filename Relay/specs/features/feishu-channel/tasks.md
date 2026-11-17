---
feature_name: "feishu-channel"
version: "0.4.5"
created: "2026-04-15T12:00:00+0800"
modified: "2026-05-01T23:30:00+0800"
upstream_dependency: ["requirements.md", "design.md"]
tags: [tasks, l2-spec, feishu, channel, relay-v0.2]
---

# feishu-channel - 任务拆分

> **任务概览**：8 个 P0 任务，覆盖配置模型扩展、Router 扩展、飞书渠道实现、集成启动、测试。
>
> **关联文档**：
> - L2 功能需求：`features/feishu-channel/requirements.md`
> - L2 技术设计：`features/feishu-channel/design.md`

---

## P0 任务

### P0-001：types.ts 配置模型扩展 + 校验 ✅

**目标**：在 types.ts 中新增飞书配置类型，启动时校验单渠道约束。

**修改文件**：
- `src/types.ts` — 新增 `FeishuAgentCredentials`，扩展 `AgentConfig` / `UserConfig`

**具体步骤**：
1. 新增 `FeishuAgentCredentials` 接口（appId, appSecret, sessionId）
2. `AgentConfig` 新增可选字段：`channel?: 'telegram' | 'feishu'`、`feishu?: FeishuAgentCredentials`
3. `UserConfig` 新增可选字段：`feishuOpenId?: string`
4. 新增 `validateConfig(config: RelayConfig): void` 函数：
   - 每个 Agent 必须恰好有一种渠道凭证（`botToken` 或 `feishu`）
   - `channel` 字段与凭证块一致（有 feishu 块则 channel 必须为 "feishu"）
   - 飞书 Agent 对应的 User 必须有 `feishuOpenId`
5. 单元测试：
   - v1 配置（无 channel 字段）→ 校验通过
   - 飞书配置 → 校验通过
   - 同时有 botToken + feishu → 校验失败
   - 飞书 Agent 无 feishuOpenId → 校验失败

**验收**：`validateConfig` 覆盖 4 种场景，测试通过

**预估**：1h

---

### P0-002：router.ts 扩展 — 飞书路由 ✅

**目标**：Router 支持 open_id 查找和飞书 Agent 发现。

**修改文件**：
- `src/router.ts` — 新增飞书路由方法
- `src/router.ts` — `createRouter` 中构建 `openIdMap` + `feishuAgentBots`

**具体步骤**：
1. 新增 `openIdMap: Map<string, UserConfig>`（key = feishuOpenId）
2. 新增 `resolveByFeishuAgent(agentId, openId)` 方法：
   - 通过 openIdMap 找 UserConfig
   - 在 user.agents 中找匹配 agentId 且 channel=feishu 的 Agent
3. 新增 `getAllFeishuBots()` 方法：
   - 收集所有 channel=feishu 的 Agent
   - 返回 `Array<{ agent: AgentConfig, authorizedOpenIds: string[] }>`
4. 单元测试：
   - resolveByFeishuAgent 正确路由
   - 未注册 openId 返回 undefined
   - getAllFeishuBots 只返回飞书 Agent
   - 混合渠道配置（TG + 飞书用户）互不干扰

**验收**：4+ 新测试通过，现有 TG 路由测试不受影响

**预估**：1h

---

### P0-003：channels/feishu.ts — 核心模块实现 ✅

**目标**：实现飞书 WebSocket 连接、消息接收、文本/图片处理、Interactive Card 回复、命令分发。

**新建文件**：
- `src/channels/feishu.ts`

**具体步骤**：
1. 新增 `@larksuiteoapi/node-sdk` 依赖（`npm install @larksuiteoapi/node-sdk`）
2. 实现 `buildMarkdownCard(text, options?)` — 构建 Schema 2.0 Interactive Card JSON：
   ```json
   {
     "schema": "2.0",
     "header": { "title": {"tag":"plain_text","content":"🔮 Pulse"}, "template":"blue" },
     "elements": [{ "tag": "markdown", "content": "..." }]
   }
   ```
3. 实现 `splitCardText(text, maxLength=4000)` — 复用 TG splitMessage 的段落优先分割逻辑
4. 实现 `downloadFeishuImage(client, messageId, imageKey)` — 调用 `client.im.messageResource.get` 下载图片 → base64
5. 实现 `createSingleFeishuBot(agent, authorizedOpenIds, deps)`：
   - 创建 `lark.Client({ appId, appSecret })`
   - 创建 `lark.WSClient({ client })`
   - 注册 `im.message.receive_v1` 事件处理器
   - 消息处理逻辑：
     - 提取 sender.open_id + msg_type + content
     - router.resolveByFeishuAgent → 鉴权
     - `/` 开头 → commands.ts handler → sendTextMessage
     - text → pool.send({ type:"human", content })
     - image → downloadFeishuImage → pool.send({ type:"human", content:[image_block] })
     - 其他类型 → "暂不支持该消息类型"
   - pool response 监听 → buildMarkdownCard → client.im.message.create
   - pool exit/error/session-end 监听 → 通知用户
   - poolKeyToOpenId Map（跟踪响应路由）
6. 实现 `createFeishuBots(deps)` — 遍历 router.getAllFeishuBots()，创建所有飞书 Bot 实例
7. 导出 `createFeishuBots`、`buildMarkdownCard`、`splitCardText`

**验收**：模块可编译，核心函数单元测试通过

**预估**：3h

---

### P0-004：channels/feishu.ts — 单元测试 ✅

**目标**：核心模块 ≥ 80% 覆盖率。

**新建文件**：
- `tests/feishu-channel.test.ts`

**具体步骤**：
1. Mock Lark SDK（Client、WSClient、EventDispatcher）
2. 测试 `buildMarkdownCard`：
   - 纯文本 → 正确 Card JSON
   - 含 header → header 正确
   - 空文本 → 空 elements
3. 测试 `splitCardText`：
   - 短文本 → 1 片
   - 超长文本 → 多片，每片 ≤ 4000
   - 段落边界分割
4. 测试消息处理（mock pool + router）：
   - 已注册用户文本消息 → pool.send 被调用
   - 未注册用户 → 回复引导文本
   - `/status` 命令 → 命令 handler 被调用
   - 图片消息 → downloadFeishuImage + pool.send
   - 不支持的消息类型 → 回复提示
5. 测试 response 路由：
   - pool emit response → client.im.message.create 被调用
   - CC noise 过滤（复用 stripCCNoise）
   - 长文本分片 → 多次 send
6. 测试 session-end 通知：
   - turns-exhausted → 通知用户
   - crash → 通知用户
   - idle-reclaimed → 静默

**验收**：≥ 20 个测试，覆盖率 ≥ 80%

**预估**：2h

---

### P0-005：router.ts 单元测试补充 ✅

**目标**：新增的飞书路由方法有完整测试覆盖。

**修改文件**：
- `tests/router.test.ts`（或新建 `tests/feishu-router.test.ts`）

**具体步骤**：
1. 测试 openId 查找（有/无/空）
2. 测试混合渠道配置下 TG 和飞书路由互不干扰
3. 测试 `getAllFeishuBots()` 返回值正确
4. 测试 v1 配置（无 channel 字段）→ getAllFeishuBots 返回空

**验收**：4+ 新测试通过

**预估**：0.5h

---

### P0-006：index.ts 集成 — 启动飞书 WSClient ✅

**目标**：startRelay() 中同时启动 TG bots 和飞书 WSClient。

**修改文件**：
- `src/index.ts` — 扩展 startRelay()

**具体步骤**：
1. 在 startRelay() 中，创建 TG bots 后，调用 `createFeishuBots()`
2. await feishuBots.start() 启动所有飞书 WebSocket 连接
3. stop() 中调用 feishuBots.stop()
4. 启动时日志：`"Feishu bots started (WebSocket, N agents)"`
5. 配置校验：在 router 创建后调用 `validateConfig(config)`

**验收**：
- relay.json 只有 TG Agent → 只启动 TG，无报错
- relay.json 有飞书 Agent → 同时启动 TG + 飞书
- 配置校验失败 → 启动报错退出

**预估**：1h

---

### P0-007：relay.json 迁移 + 示例配置 ✅

**目标**：更新线上 relay.json 和示例配置，支持飞书渠道。

**修改文件**：
- `config/relay.example.json`（或创建新的飞书示例）
- 线上 `/opt/PULSE/data/relay.json`（运维手动更新）

**具体步骤**：
1. 更新 relay.example.json，新增飞书用户示例（展示 v2 格式）
2. 确保现有 TG 配置无需修改即可继续工作（v1 兼容）
3. 文档记录配置格式说明（注释在 example.json 中）

**验收**：example.json 包含 TG 和飞书两种示例

**预估**：0.5h

---

### P0-008：集成测试 + UAT

**目标**：端到端验证飞书渠道可用。

**具体步骤**：
1. 在测试环境配置飞书 Agent（appId/appSecret）
2. 通过飞书 Bot 发送文本消息 → 验证收到 Interactive Card 回复
3. 发送图片消息 → 验证 AI 识别并回复
4. 发送 /status、/new、/help 命令 → 验证命令响应
5. 未注册用户发消息 → 验证引导回复
6. 长文本回复 → 验证分片卡片
7. CC 进程崩溃 → 验证通知用户
8. 验证 TG 渠道未受影响（回归测试）

**验收**：全部 8 项 UAT 通过

**预估**：1h

---

## P1 — Agent 能力对齐（飞书 ↔ Telegram）

> 与 `features/bot-commands` **P2-005 / P2-006**、本文 `requirements.md` **FR-P1-005 / FR-P1-006** 一致；**业务逻辑在 `commands.ts` / `router`，飞书仅做交互与回传解析**。

### P1-005：飞书卡片（按钮）Agent 点选切换 ✅

**目标**：多 Agent 用户可通过 **点按钮** 切换活跃 Agent，语义同 TG inline + `/switch`。

**修改文件（落地）**：
- `src/commands.ts` — `withAgentPickerUi` 等；与 TG 共用 `router.switchAgent`
- `src/channels/feishu.ts` — `handleFeishuCardAction`、Interactive Card `trailingElements`、`card.action.trigger`、`isFeishuDeliveryPoolKey`
- `tests/feishu-channel.test.ts` — 卡片/回传相关用例

**验收**：`requirements.md` **AC-P1-005**；与 `bot-commands` **P2-005** / **AC-P2-002** 一致

**预估**：4h（与 bot-commands P2-005 合并排期）

---

### P1-006：飞书侧验证 per-Agent 模型与 TG 一致 ✅

**目标**：`AgentConfig` 模型字段在 **飞书 spawn 路径** 与 TG 同源；无渠道专属 override。

**修改文件（落地）**：`types.ts` / `pool.ts`（`ccModel` → `--model`）；`feishu.ts` 经统一 `SpawnConfig` 传入，无飞书专属分支

**验收**：**AC-P1-006**；**bot-commands P2-006**；`tests/pool.test.ts` 含 `[P2-006]` 单测

**预估**：1h（配置已由 P2-006 主任务完成时）～ 3h（若需补全 spawn）

---

## 任务依赖关系

```
P0-001 (types) ──→ P0-002 (router) ──→ P0-003 (feishu.ts) ──→ P0-006 (index.ts) ──→ P0-008 (UAT)
                 ↘                    ↘→ P0-004 (feishu tests)
                  ↘→ P0-005 (router tests)
                                                              ↘→ P0-007 (config)
```

**建议执行顺序**：001 → 002 → 003 → 004 + 005（并行）→ 006 → 007 → 008

---

## 总预估

| 任务 | 预估 |
|------|------|
| P0-001 | 1h |
| P0-002 | 1h |
| P0-003 | 3h |
| P0-004 | 2h |
| P0-005 | 0.5h |
| P0-006 | 1h |
| P0-007 | 0.5h |
| P0-008 | 1h |
| **总计** | **10h** |

---

## Spec 同步记录

### SYNC-001：per-agent feishuOpenId + 可选字段（2026-04-25）

**Drift 根因**：飞书 openId 是 per-app 分配的，同一用户在不同飞书 Bot 里 openId 不同。原 Spec 假设 openId 是 per-user 的，不适用于单用户多 Agent 场景。

**代码变更**：
- `types.ts`：`AgentConfig` 新增 `feishuOpenId?: string`；`botToken`/`sessionId` 改为可选；`UserConfig.chatId` 改为可选
- `router.ts`：openIdMap 同时索引 agent 级 + user 级 openId；`getAllFeishuBots` 优先取 agent 级 openId
- `feishu.ts`：`poolKeyToOpenId` 使用 `agent.feishuOpenId ?? user.feishuOpenId`
- `validateConfig`：飞书 Agent 需要 agent 级或 user 级 openId 二选一

**Spec 同步**：
- `requirements.md` v0.1→v0.2：FR-P0-002、FR-P0-008 更新
- `design.md` v0.1→v0.2：§2 Router/types 描述、§3 接口定义+兼容规则、§4 数据模型示例、§6 Error-005
- `tasks.md` v0.1→v0.2：新增 SYNC-001 记录

### SYNC-002：Card 格式修复 + Typing indicator + Header 优化（2026-04-26）

**Drift 根因**：
1. `buildMarkdownCard` 生成的 Schema 2.0 卡片 JSON 格式错误（根级 `elements` 在 Schema 2.0 中无效），导致飞书 API 返回 400 错误
2. 无 typing indicator，用户发送消息后无法感知 bot 正在处理
3. 私聊场景下每张卡片都显示标题栏，冗余占位

**代码变更**：
- `feishu.ts`：`buildMarkdownCard` 改为正确的 Schema 2.0 格式 `{ schema: "2.0", config: { width_mode: "fill" }, body: { elements } }`（参照 OpenClaw `send.ts:buildStructuredCard`）
- `feishu.ts`：新增 `startTyping`/`stopTyping`，使用 `im.messageReaction.create/delete` 添加 "Typing" emoji（参照 OpenClaw `typing.ts`）
- `feishu.ts`：`MessageHandlerContext` 新增 `startTyping?`/`stopTyping?` 字段
- `feishu.ts`：`handlePoolResponse` 不再默认传入 header，私聊无标题栏
- `feishu.ts`：`buildMarkdownCard` 仅在 `options?.header` 显式传入时添加 header
- `feishu.ts`：`wirePoolEvents` 新增 `stopTyping` 参数，在 `result` 事件时停止 typing
- `pool.ts`：`buildArgs` 修复空 sessionId 问题——空 sessionId 时 CC 自动生成，不传 `--session-id ""` 或 `--resume ""`

**Spec 同步**：
- `design.md` v0.2→v0.3：§2 模块职责加入 typing、§3 接口定义更新 buildMarkdownCard 返回格式+新增 Function-002b typing 接口、§4 Card JSON 格式修正为 body.elements、§5 Flow-001 加入 typing 步骤、§8 Typing 行更新
- `tasks.md`：新增 SYNC-002 记录

### SYNC-003：Agent 配对流程 + ensureProcess 修复（2026-04-26）

**Drift 根因**：
1. 未注册飞书用户给 Bot 发消息只能收到静态"未注册"提示，需要管理员手动编辑 relay.json 绑定 openId
2. 配对完成后的 Agent 因 `ensureProcess` 只检查 `user.feishuOpenId` 而忽略 `agent.feishuOpenId`，导致 poolKeyToOpenId 为空、CC 回复无法路由

**代码变更**：
- `feishu.ts`：新增配对状态管理（`PendingPairing`、`pendingPairings` Map、`createPendingPairing`、`claimPendingPairing`、5 分钟自动过期）
- `feishu.ts`：新增 `writeOpenIdToConfig` 函数——读取 relay.json → 写入 feishuOpenId → 写回磁盘 + 调用 `router.indexFeishuOpenId` 更新内存
- `feishu.ts`：`handleFeishuMessage` 未注册用户分支改为返回配对码（取代静态"未注册"提示）
- `feishu.ts`：`handleCommand` 新增 `approve` case——验证配对码、调用 writeOpenIdToConfig、回复绑定结果
- `feishu.ts`：`FeishuBotDeps` 新增 `configPath: string` 字段
- `feishu.ts`：`MessageHandlerContext` 新增 `configPath: string` 字段
- `feishu.ts`：`ensureProcess` 修复：`agent.feishuOpenId ?? resolved.user.feishuOpenId`（优先 agent 级）
- `router.ts`：新增 `indexFeishuOpenId(agentId, openId)` 方法——更新 agent config + openIdMap
- `types.ts`：`validateConfig` 放宽——飞书 Agent 缺少 feishuOpenId 不阻塞启动（待配对）
- `index.ts`：传递 `configPath` 到 `createFeishuBots`

**Spec 同步**：
- `requirements.md` v0.2→v0.3：故事3 改为配对引导、FR-P0-002 更新为触发配对流程、FR-P0-008 放宽 openId 校验、新增 FR-P0-009（配对流程）+ FR-P0-010（/approve 命令）、AC-P0-002 更新
- `design.md` v0.3→v0.4：§2 模块职责加入配对+router.indexFeishuOpenId、§3 FeishuBotDeps 新增 configPath+新增 Function-005/006+Router 扩展 indexFeishuOpenId、§5 Flow-004 改为配对流程时序图、§6 Error-005 放宽、§8 命令行加入 /approve
- `tasks.md`：新增 SYNC-003 记录

### SYNC-004：P1-002 post 富文本支持落地（2026-05-01）

**Drift 根因**：requirements.md 将 `msg_type=post` 作为 P1（增强功能）占位，但代码已支持；未在 spec 中明确“已实现”，容易被误判为欠债。

**代码现实**：
- `src/channels/feishu.ts`：`handleFeishuMessage` 支持 `msg_type=post`，调用 `extractPostText()` 提取纯文本后走与 text 同一条 send 路径；`fetchReplyText` 也支持 post 作为 reply context。
- `tests/feishu-channel.test.ts`：覆盖 `extractPostText` 多语种/emoji/@mention/link/容错；覆盖 post 消息端到端（含 `/` 命令与解析失败提示）。

**Spec 同步**：
- `requirements.md` v0.3.0→v0.3.1：在 FR-P1-002 下补 `状态：已实现`（含代码/测试锚点）。

### SYNC-005：P1-003 飞书 file 消息（PDF/文本）（2026-05-01）

**目标**：`msg_type=file` → `im.messageResource.get`（`type=file`）→ 与 Telegram `message:document` 对齐的 CC human blocks；默认 25MiB 上限（`RELAY_FEISHU_FILE_MAX_BYTES`）。

**代码变更**：
- `src/channels/feishu.ts`：`readFeishuDownloadBuffer` / `downloadFeishuMessageResource`（image+file 共用响应读取）；`downloadFeishuImage` 复用后者；`classifyFeishuInboundFile` + `humanContentBlocksFromFeishuFile`；`handleFeishuMessage` 增加 `file` 分支；`MessageHandlerContext.downloadFileResource`（测试注入）
- `tests/feishu-channel.test.ts`：覆盖 PDF、文本、超上限、unsupported、下载失败

**Spec 同步**：
- `requirements.md` v0.3.1→v0.3.2：FR-P1-003 展开约束与状态；新增 AC-P1-003；排除范围划掉已实现的 post/file
- `design.md` v0.4.0→v0.4.1：Flow-005、Error-006、模块职责与 OpenClaw 参照说明

### SYNC-006：P1-004 飞书 audio 语音（STT）（2026-05-01）

**目标**：`msg_type=audio` → `messageResource.get(type=file)` → `transcribeAudio` → `pool.send` 文本 human（与 Telegram 语音一致）；上限同 `RELAY_FEISHU_FILE_MAX_BYTES`。

**代码变更**：
- `src/channels/feishu.ts`：`import transcribeAudio from stt`；`MessageHandlerContext.transcribeInboundAudio`；`handleFeishuMessage` audio 分支
- `tests/feishu-channel.test.ts`：转写成功、空转写、STT 失败

**Spec 同步**：
- `requirements.md` v0.3.2→v0.3.3：FR-P1-004 修正为 STT 路径（非 raw base64 进 CC）；AC-P1-004；排除范围划掉语音
- `design.md` v0.4.1→v0.4.2：Flow-006、Error-007、Function-002b 补充说明

### SYNC-007：P1-001 Streaming Card（Card Kit）（2026-05-01）

**目标**：CC 多段 `assistant` 时复用飞书 Card Kit streaming；`result` 关闭流式；可环境变量禁用回退静态卡片。

**代码变更**：
- `src/feishu-streaming-card.ts`（新建）：tenant token 缓存、`FeishuStreamingCardSession`、`mergeStreamingText`、`isFeishuStreamingCardEnvEnabled`
- `src/channels/feishu.ts`：`handleFeishuPoolResponse`（async）、`createFeishuStreamingSink`、`wirePoolEvents` 注入 sink；保留 `handlePoolResponse` 同步 fire-and-forget 包装
- `tests/feishu-streaming-card.test.ts`、`tests/feishu-channel.test.ts` 调整

**Spec 同步**：
- `requirements.md` v0.3.3→v0.3.4：FR-P1-001、AC-P1-001；排除范围划掉 Streaming Card
- `design.md` v0.4.2→v0.4.3：模块职责与参考实现补充

### SYNC-008：Agent 点选 + per-Agent 模型 — 飞书与 TG 对等（2026-05-02）

**根因**：`bot-commands` 原将 P2-005/P2-006 表述为 TG 向；**飞书用户与 TG 用户对 Agent 的操作需求一致**，需在 L2 显式 **多渠道对等**。

**Spec 同步**：
- `requirements.md` v0.3.4→v0.3.5：新增 **FR-P1-005 / FR-P1-006**，**AC-P1-005 / AC-P1-006**；关联 `bot-commands` **FR-P2-001 / FR-P2-002**
- `features/bot-commands/requirements.md`：重写 **FR-P2-001 / FR-P2-002**，新增 **AC-P2-002 / AC-P2-003**；Out of Scope 收紧
- `features/bot-commands/tasks.md`：**P2-005 / P2-006** 扩展为 TG+飞书；本 `tasks.md` 新增 **§P1 — P1-005 / P1-006**

### SYNC-009：P1-005 / P1-006 实现收口（2026-05-01）

**背景**：`bot-commands` **P2-005 / P2-006** 已代码落地；飞书侧 **P1-005 / P1-006** 与之对齐，tasks 曾写「待开始」造成漂移。

**代码锚点**：
- 点选切换：`src/commands.ts`（`withAgentPickerUi`、`resolveInboundRoutingAgent`、`RELAY_SWITCH_CB_PREFIX`）；`src/channels/feishu.ts`（卡片按钮 + action）；`src/channels/telegram.ts`（`callback_query:data`）
- 模型：`AgentConfig.ccModel` → `pool.spawn` → `--model`（`tests/pool.test.ts` `[P2-006]`；`tests/commands.test.ts` `[P2-005]` 辅助函数）

**Spec 同步**：本 `tasks.md` §P1-005 / P1-006 标 ✅；`features/bot-commands/tasks.md` P2-005 / P2-006 标 [x]
