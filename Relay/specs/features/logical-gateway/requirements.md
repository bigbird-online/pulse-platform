---
feature_name: "logical-gateway"
version: "0.1.0"
created: "2026-04-27T11:07:46+0800"
modified: "2026-04-29T10:00:00+0800"
upstream_dependency:
  - "specs/project-specs/project-architecture.md"
  - "specs/features/telegram-channel/design.md"
  - "specs/features/feishu-channel/design.md"
  - "specs/features/conversation-runtime/design.md"
  - "specs/features/agent-scheduler/design.md"
tags: ["relay", "gateway", "identity", "multi-user", "delivery", "runtime", "s1-s2", "requirements"]
changelog:
  - version: "0.1.0"
    date: "2026-04-29"
    note: "P0 全部完成，P1 部分完成；验收标准更新反映生产 canary 与真实入口 UAT"
  - version: "0.0.2"
    date: "2026-04-27"
    note: "P1-001b 飞书 /approve 写路径补入需求"
  - version: "0.0.1"
    date: "2026-04-27"
    note: "初始需求文档"
---

# logical-gateway - 功能需求文档

> **功能概述**：Relay v1.1 在现有进程内引入内部逻辑 Gateway 层，统一渠道地址绑定、路由策略、runtime 编排和投递成功/失败语义。第一阶段不拆独立服务，不重写全部渠道，只用最小切片消除多用户接入、身份分叉、runtime 漂移和通知静默失败。
>
> **工程风险**：Feature risk = high-risk；当前 spec 切片风险 = normal。后续任何接入 `index.ts`、channel handler、scheduler、runtime registry、delivery/escalation path 的实现切片均按 high-risk 处理，必须有真实入口 UAT 和回滚路径。
>
> **决策来源**：`pulse-space/memo/decision-2026-04-27-relay-logical-gateway.md`

---

## 1. 用户故事

**故事1：多用户通过多个 TG chat_id 接入**
- **作为**：PULSE 平台维护者
- **我希望**：多个 Telegram `chat_id` 可以绑定到不同 human / tenant / workspace / default agent
- **以便**：PULSE 不再假设单用户单 chat_id
- **优先级**：P0

**故事2：飞书 open_id/chat_id 与平台身份解耦**
- **作为**：飞书用户或管理员
- **我希望**：飞书 `open_id`、群聊 `chat_id` 只是渠道地址，不直接复制 Agent 身份
- **以便**：Duke 仍是 Duke，不因飞书入口长期变成 `duke-feishu`
- **优先级**：P0

**故事3：统一 runtime selection 与 fallback**
- **作为**：PULSE 平台维护者
- **我希望**：TG/飞书 channel 不各自理解 CC/Codex fallback
- **以便**：runtime preference、fallback、thread/session state 由平台层统一编排
- **优先级**：P0

**故事4：投递失败不再静默**
- **作为**：定时任务和 Harness 的消费者
- **我希望**：任务产物生成成功但 TG/飞书/Harness 投递失败时，系统明确记录 failure 并升级
- **以便**：避免平台错误被误判为任务成功
- **优先级**：P0

**故事5：S1/S2 共享 delivery contract**
- **作为**：Relay 与 Harness 维护者
- **我希望**：实时交互入口和无人任务都使用一致的 delivery request / receipt 语义
- **以便**：S1/S2 边界清楚，Harness 消费平台投递结果而不是拥有渠道入口
- **优先级**：P0

---

## 2. 功能需求

### P0 功能（全部已完成）

**FR-P0-001：ChannelAddress 标准化** [已完成]
- **描述**：Gateway 将 Telegram `chat_id`、飞书 `open_id` 表达为统一 `ChannelAddress`，包含 `channel`、`kind`、`value`、可选 `botId`/`appId`。实现通过 `normalizeTelegramChatAddress()` 和 `normalizeFeishuOpenIdAddress()` 完成；`channelAddressKey()` 按 `channel:kind:appId:botId:value` 生成唯一键。
- **验收**：同一 principal 可拥有多个 address；不同 app 下同一飞书用户的不同 open_id 不会互相覆盖。

**FR-P0-002：ChannelBinding 注册表** [已完成]
- **描述**：Gateway 维护 address 到 platform principal 的绑定。实现包括 legacy-derived view（从旧 `relay.json` 结构派生）和 explicit binding record（`relay.json.bindings[]`）。`createGatewayBindingView()` 优先加载 active explicit bindings，pending/revoked 会压制同 address 的 legacy fallback。
- **验收**：两个 TG chat_id 可绑定到不同 principal；同一个 principal 可同时绑定 TG 与飞书地址；dual-channel agent（如 Duke）同时拥有 TG 与 Feishu binding。

**FR-P0-003：RoutePolicy 决策** [已完成]
- **描述**：`resolveInboundRoute()` 根据 inbound event 的 address、目标 agent 解析 route policy。返回 `ok`（含 `RoutePolicyResult`）、`pairing_required`（未绑定地址）或 `access_denied`（agent 不在 allowlist 或 agent 不存在）。
- **验收**：未绑定地址返回 `pairing_required`；未授权 principal 返回 `access_denied`。

**FR-P0-004：Runtime 编排归一** [已完成]
- **描述**：TG 与飞书 handler 从 `RoutePolicyResult.runtimePreference` 获取 runtime preference，传入 `ConversationRuntimeManager`。channel adapter 不做 fallback 选择。runtime preference 类型为 `ConversationRuntimeId[]`（即 `('cc' | 'codex')[]`）。
- **验收**：TG 与飞书对同一 principal/agent 使用一致 runtime preference；channel handler 不直接选择 CC/Codex。

**FR-P0-005：DeliveryRequest / DeliveryReceipt** [已完成]
- **描述**：所有外发消息通过 typed delivery 函数投递：`deliverTelegramText`、`deliverTelegramDocument`、`deliverFeishuText`、`deliverFeishuCard`。TG delivery 支持 429 flood retry（`withRetry`）；Feishu delivery 返回原始 card shape。Receipt 包含 `status`、`channel`、`address`、`externalMessageId`、`error`、`attempts`、`deliveredAt`。
- **验收**：发送失败返回 `status=failed`，调用者不得把业务任务标记为完整成功。

**FR-P0-006：任务成功语义拆分** [已完成]
- **描述**：`JobResult` 同时暴露聚合 `status`、`executionStatus`、`deliveryStatus` 和可选 `deliveryReceipt`。execution ok 但 delivery failed 时，聚合 `status=error`。
- **验收**：测试覆盖 `executionStatus=ok && deliveryStatus=failed`；生产 smoke 已验证无效 chat_id 场景。

**FR-P0-007：S1/S2 边界** [已完成]
- **描述**：S1 channel inbound 和 S2 scheduled/escalation flow 都使用 Gateway contract。Gateway 归 Relay 平台层所有，Harness 只消费 delivery/escalation result。
- **验收**：Harness 不直接依赖 Telegram/Feishu SDK；Relay channel adapter 不直接写 Harness 审计状态。

**FR-P0-008：向后兼容现有配置** [已完成]
- **描述**：第一阶段兼容现有 relay.json 中 `chatId`、`feishuOpenId`、agent channel 配置。`createGatewayBindingView()` 通过 legacy-derived fallback 读取旧字段生成 binding view。
- **验收**：真实配置无 `bindings` 块时 legacy fallback 正常工作；生产 dry-run 通过。

### P1 功能

**FR-P1-001：Pairing 流程归一**
- **描述**：TG 与飞书未绑定地址的 pairing/approval 流程使用同一 binding registry。Explicit binding read path 已部署；飞书 `/approve` 已改为写 `bindings[]` active record。
- **状态**：暂停。Read/write path 已部署，real pairing UAT 因当前没有可触发的未绑定飞书用户场景而延期。

**FR-P1-002：多租户配置存储迁移**
- **描述**：当 JSON 配置难以承载 binding registry、policy、audit 时，评估 SQLite 或其他持久层。
- **状态**：已完成。决策为继续使用 `relay.json` 顶层 `bindings` 块，SQLite 延后。

**FR-P1-003：跨渠道会话连续性**
- **描述**：同一 principal 可选择在 TG 与飞书之间共享或隔离 conversation thread，策略由 route policy 显式定义。
- **状态**：已完成（部分）。Dual-channel Feishu 使用 channel-scoped tenant（`owner:main:feishu`）实现 session 隔离；跨渠道共享未实现，当前默认隔离。

**FR-P1-004：Dual-channel Agent 多地址绑定**
- **描述**：同一 logical agent 可同时拥有 TG bot token 与 Feishu credentials，Gateway 为其派生多个 address binding。移除 `duke-feishu` 长期身份复制。
- **状态**：已完成。`duke-feishu` 已迁入 `main` agent；production 已验证。

**FR-P1-005：飞书 `/approve` 写入 explicit binding**
- **描述**：飞书审批从不稳定的 `agent.feishuOpenId` 字段写入改为写入 `relay.json.bindings[]` active binding record，包含 `principalId`、`tenantId`、`defaultAgentId`、`allowedAgentIds`、`approvedAt`、`approvedBy`、`auditTags`。
- **状态**：已完成。Write path 已部署；read path 优先加载 active explicit bindings。

---

## 3. 验收标准

**AC-P0-001：多 TG chat_id 路由** [已验证]
- **条件**：配置两个 Telegram address，分别绑定不同 principal 和 default agent
- **预期**：两个 chat_id 的消息进入不同 route result，workspace/session/runtime preference 不串扰
- **验证**：Unit test (gateway.test.ts) + integration test；生产 canary 已通过

**AC-P0-002：飞书 open_id per-app 绑定** [已验证]
- **条件**：同一 human 在两个 Feishu app 下有不同 open_id
- **预期**：binding 按 `channel + appId + open_id` 区分，不互相覆盖
- **验证**：Unit test (gateway.test.ts: channelAddressKey 含 appId)

**AC-P0-003：身份不分叉** [已验证]
- **条件**：Duke 同时有 TG 与飞书入口
- **预期**：route result 的 `agentId=main`，`duke-feishu` 身份已迁移消除
- **验证**：Config/router test + 生产 dry-run + 人工 inbound UAT

**AC-P0-004：runtime 选择不在 channel 内** [已验证]
- **条件**：同一 principal/agent 通过 TG 和飞书发消息
- **预期**：两者都生成相同 runtime preference，channel 只传递 message/content/address
- **验证**：Channel tests + conversation-runtime tests

**AC-P0-005：投递失败可观测** [已验证]
- **条件**：job execution 成功，Telegram 或飞书 API 返回失败
- **预期**：delivery receipt 为 failed，总体 job result 可见 partial/failure，不静默 ok
- **验证**：Unit test + 生产 smoke（无效 chat_id 触发 `status=error`, `executionStatus=ok`, `deliveryStatus=failed`）

**AC-P0-006：真实入口 UAT** [已验证]
- **条件**：本地或生产 canary Relay 启动
- **预期**：TG、飞书、cron/Harness 各至少一条路径通过 Gateway contract 留痕
- **验证**：P0-007 canary 已完成。TG text delivery receipt `7d961d9a...`，`status=sent`；Feishu text delivery receipt `5a9a3a24...`，`status=sent`；Cron job receipt `d8cd56df...`，`status=sent`；Harness escalation `ev_1777271857177_5ac2`，`push_status=sent`

**AC-P1-001：飞书双通道 session 隔离** [已验证]
- **条件**：Duke main agent 同时拥有 TG 与 Feishu binding
- **预期**：Feishu response 不被 TG chat mapping 消费；Feishu 使用 scoped tenant `owner:main:feishu`
- **验证**：生产验证 + 自动化测试 (feishu-channel.test.ts, telegram.test.ts)

---

## 4. 非功能需求

- **可靠性**：关键 delivery failure 不可被 catch 后丢弃；必须返回 receipt 或进入 escalation。
- **可回滚**：第一阶段必须允许旧 router/config 路径作为 fallback view；生产入口切换需可 feature-flag。
- **审计性**：Gateway event、route result、runtime selection、delivery receipt 必须有可串联 correlation id。
- **安全性**：channel address 不是身份；所有 Agent 访问必须经 binding/policy 判断。
- **兼容性**：旧 TG 单用户配置、飞书 app 配置、scheduler job delivery 在迁移期继续工作。
