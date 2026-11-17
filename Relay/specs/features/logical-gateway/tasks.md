---
feature_name: "logical-gateway"
version: "0.1.0"
created: "2026-04-27T11:07:46+0800"
modified: "2026-04-29T10:00:00+0800"
upstream_dependency:
  - "specs/features/logical-gateway/requirements.md"
  - "specs/features/logical-gateway/design.md"
tags: ["relay", "gateway", "tasks", "s1-s2", "delivery", "runtime"]
changelog:
  - version: "0.1.0"
    date: "2026-04-29"
    note: "Spec 对齐代码实现：P0 全部完成，P1 补充 dual-channel/Feishu approve/explicit binding 任务"
  - version: "0.0.2"
    date: "2026-04-27"
    note: "P1-001a/001b 执行记录补入"
  - version: "0.0.1"
    date: "2026-04-27"
    note: "初始任务拆分"
---

# logical-gateway - Tasks

> 任务概述：按 spec-driven-engineering 分片实现 Relay 内部逻辑 Gateway。完整 feature risk = high-risk；每个实现切片必须单独声明 slice risk、真实入口影响和回滚方式。

## 1. 任务拆分

### P0 任务（全部已完成）

| 任务ID | 任务名称 | 预估工时 | 依赖任务 | 负责人 | 状态 |
|---|---|---:|---|---|---|
| P0-001 | 定义 Gateway contract 类型与只读 binding view | 4h | 无 | Pulse | 完成 |
| P0-002 | 为 TG/飞书旧配置补 address normalization 与 binding tests | 3h | P0-001 | Pulse | 完成 |
| P0-003 | 新增 inbound route facade（`resolveInboundRoute`），先由 channel handler 调用但保持旧行为 | 6h | P0-001, P0-002 | Pulse | 完成 |
| P0-004 | 新增 delivery typed 函数（`deliverTelegramText/Document`、`deliverFeishuText/Card`），含 429 retry | 8h | P0-001 | Pulse | 完成 |
| P0-005 | Scheduler/Harness delivery failure 不静默：executionStatus 与 deliveryStatus 分离 | 6h | P0-004 | Pulse | 完成 |
| P0-006 | TG + 飞书 runtime preference 从 RoutePolicyResult 传入 manager | 5h | P0-003 | Pulse / Engineer | 完成 |
| P0-007 | 真实入口 canary：TG、飞书、cron/Harness 各一条 receipt 留痕 | 4h | P0-004, P0-005, P0-006 | Pulse | 完成 |

### P1 任务

| 任务ID | 任务名称 | 预估工时 | 依赖任务 | 负责人 | 状态 |
|---|---|---:|---|---|---|
| P1-001 | Pairing/approval 归一到 ChannelBinding registry | 8h | P0 全部 | Pulse / Engineer | 暂停（read/write path 已部署，real pairing UAT 因暂无场景延期） |
| P1-001a | Explicit `relay.json.bindings[]` read path：route/router/bot authorized 均读取 active binding | 4h | P0 全部 | Pulse | 完成 |
| P1-001b | 飞书 `/approve` 写入 explicit binding record，router 内存索引即时生效 | 4h | P1-001a | Pulse | 完成 |
| P1-002 | 移除 `duke-feishu` 长期配置，让 `main` agent 同时拥有 TG 与 Feishu binding | 4h | P0 全部 | Pulse / Duke review | 完成 |
| P1-003 | 评估 binding registry 持久层：决策为 `relay.json.bindings[]`，SQLite 延后 | 4h | P0 全部 | Pulse / Ken review | 完成 |
| P1-004 | Feishu dual-channel session 隔离：scoped tenant `owner:main:feishu` | 3h | P1-002 | Pulse | 完成 |
| P1-005 | 飞书 `/approve` 写路径：从不稳定的 `agent.feishuOpenId` 改为写 active binding record | 4h | P1-001a | Pulse | 完成 |

### 待定任务

| 任务ID | 任务名称 | 预估工时 | 依赖任务 | 负责人 | 状态 |
|---|---|---:|---|---|---|
| P1-006 | 真实 pairing UAT：需要未绑定飞书用户触发 pairing + 管理员 `/approve` 全链路验证 | 4h | P1-001b | Pulse / 用户配合 | 待定（当前无可用场景） |
| P1-007 | 跨渠道会话共享（非隔离）：同一 principal 在 TG 与飞书之间共享 conversation thread | 6h | P1-004 | Pulse | 待定 |

## 2. 第一实现切片 MVS

**目标**：在不改变生产行为的前提下，新增 Gateway contract 类型、只读 binding view 和测试，证明现有 TG/飞书配置可以被表达为统一 `ChannelBinding`。

**边界**：

- 不接 `index.ts` 生产启动入口。
- 不改 Telegram/Feishu handler。
- 不改真实 `relay.json` schema。
- 不触发生产重启。

**上下文**：

- `src/types.ts`
- `src/router.ts`
- `src/channels/telegram.ts`
- `src/channels/feishu.ts`
- `tests/router.test.ts`
- `tests/feishu-router.test.ts`
- `tests/feishu-config.test.ts`

**验收**：

- Unit test：多个 TG `chatId` 可生成不同 `ChannelBinding`。
- Unit test：飞书 `agent.feishuOpenId` 优先于 `user.feishuOpenId`，并带上 appId。
- Unit test：同一 principal 可拥有 TG 与飞书两个 address。
- `npm run typecheck` 通过。
- 相关 router/config tests 通过。

**风险**：

- Feature risk = high-risk，因为完整 feature 会接入生产 channel、runtime、scheduler、delivery。
- 当前 slice risk = normal，因为只新增未接生产入口的 contract/view 和测试。

**执行记录（2026-04-27）**：

- 新增 contract 类型：`GatewayChannel`、`Principal`、`ChannelAddress`、`ChannelBinding`、`GatewayEvent`、`RoutePolicyResult`、`DeliveryRequest`、`DeliveryReceipt`。
- 新增只读 binding view：`createGatewayBindingView(config)` 从旧 `relay.json` 结构派生 bindings，不读写真实配置。
- 验收证据：`npm test -- --run tests/gateway.test.ts tests/router.test.ts tests/feishu-router.test.ts tests/feishu-config.test.ts` -> 4 files / 45 tests passed；`npm run typecheck` passed；`npm run build` passed；`HOME=/tmp npm test` -> 25 files / 372 tests passed。
- 生产边界：未接 `src/index.ts`，未改 TG/飞书 handler，未改真实 `relay.json` schema，未重启生产。

**P0-002 执行记录（2026-04-27）**：

- 新增 address normalization helpers：`normalizeTelegramChatAddress`、`normalizeFeishuOpenIdAddress`、`channelAddressKey`。
- Binding view 改为通过 normalization helpers 生成 address 与 binding id；Feishu key 显式包含 `appId`，避免同一 `open_id` 跨 app 碰撞。
- 新增测试覆盖：TG chat address bot scope、Feishu open_id app scope、user-level Feishu openId fallback、同一 openId 在不同 Feishu app 下保持不同 binding。
- 验收证据：`npm test -- --run tests/gateway.test.ts tests/router.test.ts tests/feishu-router.test.ts tests/feishu-config.test.ts` -> 4 files / 49 tests passed；`npm run typecheck` passed；`npm run build` passed；`HOME=/tmp npm test` -> 25 files / 376 tests passed。
- 生产边界：仍未接 `src/index.ts`，未改 TG/飞书 handler，未改真实 `relay.json` schema，未重启生产。

**P0-003 MVS / 执行记录（2026-04-27）**：

- 目标：新增 inbound route facade，让 channel handler 可构造 Gateway `RoutePolicyResult`，但本切片仍以旧 router 结果驱动真实行为。
- 风险：Feature risk = high-risk；Slice risk = high-risk，因为触碰 TG/飞书 handler。降风险方式：facade 只旁路观察和 warning，不改变授权、spawn、send、runtime、delivery 结果。
- 改动：新增 `resolveInboundRoute()`，返回 `ok` / `pairing_required` / `access_denied`；TG text handler、Feishu text/post/image handler 在旧 router 成功后调用 facade，若 route drift 只记录 warning。
- 验收证据：`npm test -- --run tests/gateway.test.ts tests/telegram.test.ts tests/feishu-channel.test.ts tests/feishu-router.test.ts` -> 4 files / 110 tests passed；`npm run typecheck` passed；`npm run build` passed；`HOME=/tmp npm test` -> 25 files / 380 tests passed。
- 生产边界：未改真实 `relay.json` schema，未改 runtime preference/fallback，未改 delivery，未重启生产；本切片未做真实入口 UAT，因此不能宣称 Gateway 已承载生产路由。

**P0-004 MVS / 执行记录（2026-04-27）**：

- 目标：新增 delivery request/receipt dispatcher，覆盖 TG text/document 与 Feishu text/card；外部 API 成败统一折成 `DeliveryReceipt`。
- 风险：Feature risk = high-risk；Slice risk = high-risk，因为接入 scheduler delivery 与 Feishu send path。降风险方式：不改变 P0-005 的 job 总体状态语义，不改配置 schema，不改 runtime。
- 改动：新增 `src/delivery.ts`；TG announce/document 通过 dispatcher 发送并记录 receipt；Feishu text/card 通过 dispatcher 发送并保留原 interactive card shape。
- 验收证据：`npm test -- --run tests/delivery.test.ts tests/scheduler.test.ts tests/feishu-channel.test.ts` -> 3 files / 119 tests passed；`npm run typecheck` passed；`npm run build` passed；`HOME=/tmp npm test` -> 26 files / 386 tests passed。
- 生产验证：本机 `com.pulse.relay` 已重启，PID `77275`；真实 TG text、TG document、Feishu text、Feishu card delivery smoke 均返回 `status=sent`、`attempts=1`、有外部 message id。
- 生产边界：此切片尚未把 delivery failed 改为 scheduler 总体 failed；该语义留到 P0-005。真实 smoke 验证的是 dispatcher outbound delivery，不等价于 P0-005 的 failure-semantics 验收。

**P0-005 MVS / 执行记录（2026-04-27）**：

- 目标：Scheduler/Harness delivery failure 不再静默为完整成功；`JobResult` 同时暴露聚合 `status`、`executionStatus`、`deliveryStatus` 和可选 `deliveryReceipt`。
- 风险：Feature risk = high-risk；Slice risk = high-risk，因为改变 scheduler job result 语义。兼容策略：保留旧 `status` 字段，新增字段承载拆分语义。
- 改动：当 execution ok 但 announce / telegram_document / harness_escalation delivery failed 时，聚合 `status=error`，同时保留 `executionStatus=ok`、`deliveryStatus=failed`。
- 验收证据：`npm test -- --run tests/scheduler.test.ts tests/delivery.test.ts` -> 2 files / 65 tests passed；`npm run typecheck` passed；`npm run build` passed；`HOME=/tmp npm test` -> 26 files / 388 tests passed。
- 生产验证：本机 `com.pulse.relay` 已重启到 PID `79636`；临时 scheduler 配置 + 真实 Codex runtime + 真实 Telegram API 无效 chat smoke 返回 `status=error`、`executionStatus=ok`、`deliveryStatus=failed`、`deliveryReceipt.status=failed`。未改真实 cron 配置。

**P0-006 MVS / 执行记录（2026-04-27）**：

- 目标：TG 与飞书入口在 legacy router 授权成功后，使用 `RoutePolicyResult.runtimePreference` 作为 spawn/fallback preference 来源。
- 风险：Feature risk = high-risk；Slice risk = high-risk，因为触碰 TG/飞书生产 handler 的 runtime 入口。兼容策略：route facade 非 `ok` 或 route drift 时回落旧行为；旧 router 仍负责授权和 poolKey。
- 改动：TG text/media 与飞书 text/post/image handler 从 `resolveInboundRoute()` 取得 route，并把 route runtime preference 传入 conversation runtime manager。
- 验收证据：`npm test -- --run tests/telegram.test.ts tests/feishu-channel.test.ts tests/gateway.test.ts` -> 3 files / 98 tests passed；`npm run typecheck` passed；`npm run build` passed；`HOME=/tmp npm test` -> 26 files / 390 tests passed。
- 生产边界：本记录先完成代码与自动化验收；真实入口 canary 留给 P0-007，避免把局部 handler smoke 误认作全链路 Gateway canary。

**P0-007 MVS / 执行记录（2026-04-27）**：

- 目标：对 P0-004/P0-005/P0-006 的生产加载与真实 API 路径做 canary 留痕，覆盖 TG、飞书、cron/Harness。
- 风险：Feature risk = high-risk；Slice risk = high-risk，因为执行真实生产重启、真实 TG/飞书 API、真实 scheduler cron、真实 Harness escalation。回滚方式：还原到 P0-006 前提交并重启 `com.pulse.relay`；本切片未改真实 cron 配置。
- 生产重启：`com.pulse.relay` 已 kickstart 到 PID `82165`，`LastExitStatus=0`。
- TG canary：真实 `relay.json` + `resolveInboundRoute()` 返回 `status=ok`、`agentId=pulse`、`conversationKey=owner:pulse`、`runtimePreference=["codex","cc"]`；真实 TG text delivery receipt `7d961d9a-0518-44ab-bc20-19224679565e`，`correlationId=gateway-p0-007:tg:1777271525823`，`status=sent`，`attempts=1`。
- 飞书 canary：真实 `relay.json` + `resolveInboundRoute()` 返回 `status=ok`、`agentId=duke-feishu`、`conversationKey=owner:duke-feishu`、`runtimePreference=["codex","cc"]`；真实 Feishu text delivery receipt `5a9a3a24-bb51-42e3-8f9f-87bdcedbc8a4`，`correlationId=gateway-p0-007:feishu:1777271527856`，`status=sent`，`attempts=1`。
- Cron canary：临时 `/tmp` cron config，`node-cron` callback 真实触发 job `gateway-p0-007-cron-1777271786676`；Codex batch runtime 成功，`status=ok`、`executionStatus=ok`、`deliveryStatus=sent`、`runtime=codex`、`durationMs=7581`；TG announce receipt `d8cd56df-ea82-4162-bbe6-269846bd620d`，`correlationId=job:gateway-p0-007-cron-1777271786676`。
- Harness canary：真实 `harness escalation emit` 事件 `ev_1777271857177_5ac2`，`push_status=sent`；一次 sandbox DNS 失败事件 `ev_1777271842177_2bf2` 已用真实网络 `retry-failed` 成功重推，复查 `list-failed` -> `no failed events`。
- 生产边界：TG/飞书 canary 是真实 API outbound delivery + route contract 核验，不是人工从 TG/飞书客户端输入的 inbound UAT；cron canary 使用临时 config，未改真实 `config/cron-jobs.json`。

**P1-002 MVS / 执行记录（2026-04-27）**：

- 目标：移除 Duke 飞书长期 `duke-feishu` 身份复制，让真实 Duke `main` agent 同时拥有 TG 与飞书 address binding。
- 风险：Feature risk = high-risk；Slice risk = high-risk，因为放宽 config schema、改 router/gateway route 语义、迁真实 `~/.relay/relay.json`、重启生产 Relay。回滚方式：恢复自动备份 `relay.json.bak-*-p1-duke-feishu-migration` / `state.json.bak-*-p1-duke-feishu-migration`，回滚代码提交并重启 `com.pulse.relay`。
- 改动：`AgentConfig` 允许同一 logical agent 同时拥有 `botToken` 与 `feishu` credentials；router/gateway 以 credential presence 生成 TG/飞书 bindings；dual-channel agent 的 session reset/restore 同步 TG 与 Feishu session；启动时清理配置里已不存在 agent 的 stale state。
- 配置迁移：真实 `~/.relay/relay.json` 已把 `duke-feishu` 的 Feishu credentials/openId 迁到 `main`，移除 `duke-feishu` agent；真实 `state.json` 已清理 `owner:duke-feishu`，保留 `owner:main`。
- 验收证据：`npm test -- --run tests/feishu-config.test.ts tests/feishu-integration.test.ts tests/feishu-router.test.ts tests/gateway.test.ts tests/feishu-channel.test.ts tests/telegram.test.ts` -> 6 files / 131 tests passed；`npm run typecheck` passed；`npm run build` passed；`HOME=/tmp npm test` -> 26 files / 393 tests passed。
- 生产验证：`com.pulse.relay` 已重启到 PID `86482`，`LastExitStatus=0`；日志显示 `🎲 Duke feishu WSClient started`、`All 1 Feishu bots started (WebSocket)`；真实 route dry-run 返回 `agentId=main`、`conversationKey=owner:main`、`runtimePreference=["codex","cc"]`。
- 真实 Feishu canary：delivery receipt `fcc526b1-7610-407c-8436-4752708fd193`，`correlationId=gateway-p1-duke-feishu:1777273820426`，`botId=main`，`status=sent`，`attempts=1`。
- 人工 inbound UAT：用户已从飞书客户端向 Duke 发送消息并确认成功；这补齐 Feishu 客户端 -> WSClient -> Relay route -> `main` agent 的真实入口证据。
- UAT 后问题与修复：用户发现飞书消息会同时触发飞书和 TG 回复。根因是 dual-channel Duke 复用 `owner:main` runtime tenant，TG handler 保留了该 poolKey 的 chat mapping。修复为 dual-channel Feishu 使用 channel-scoped tenant `owner:main:feishu`，Feishu event handlers 只消费 scoped tenant，Relay 启动时 scoped session 不再回写共享 agent session。
- 修复验收：`npm test -- --run tests/feishu-channel.test.ts tests/telegram.test.ts tests/gateway.test.ts tests/feishu-router.test.ts tests/feishu-integration.test.ts` -> 5 files / 125 tests passed；`npm run typecheck` passed；`npm run build` passed；`HOME=/tmp npm test` -> 26 files / 395 tests passed；生产 `com.pulse.relay` 已重启到 PID `89913`，日志显示 Duke TG polling 与 Duke Feishu WSClient 均启动。
- 生产边界：已验证真实 Feishu API outbound delivery、真实 route contract、生产 WSClient 启动和人工 inbound；双渠道默认 session 隔离已修复。尚未把 P1 registry 持久层从派生 view 迁出。

**P1-003 MVS / 执行记录（2026-04-27）**：

- 目标：评估 binding registry 持久层，决定 P1-001 pairing/approval 归一的存储路径。
- 风险：Feature risk = high-risk；Slice risk = normal，本切片只产出设计决策，不改生产代码或真实配置。
- 决策：P1-001 采用 `relay.json` 顶层显式 `bindings` 块；继续保留 legacy-derived binding fallback；SQLite 延后到高并发、多写者或审计查询需求明确时。
- 依据：仅派生 view 不能承载 pending/revoked/approvedBy/audit；SQLite 当前会增加部署、备份和迁移复杂度；顶层 `bindings` 块与当前单机 Relay、人工 review、备份回滚模型匹配。
- 记录：`pulse-space/memo/decision-2026-04-27-binding-registry-storage.md`。
- 下一步：P1-001 先实现 explicit binding read path，再把 Feishu `/approve` 从写 `agent.feishuOpenId` 改为写 active binding record；生产 rollout 保留 legacy fallback 直到真实 pairing UAT 成功。

**P1-001a MVS / 执行记录（2026-04-27）**：

- 目标：实现 explicit `relay.json.bindings[]` read path，让 route、router 索引、bot authorized 列表和 Feishu response mapping 都能读取 active binding record；legacy-derived binding 保持 fallback。
- 风险：Feature risk = high-risk；Slice risk = high-risk，因为改 router/channel 生产读路径。边界：本切片不改 `/approve` 写路径、不改真实 `relay.json` schema、不创建真实 binding record。
- 改动：新增 `ChannelBindingRecord` / `ChannelBindingStatus`；`createGatewayBindingView()` 优先加载 active explicit bindings；pending/revoked explicit binding 会压制同 address 的 legacy fallback；router 从 active explicit bindings 建 TG/Feishu 索引和 bot authorized address；TG/飞书 route facade 传入 `router.getGatewayBindings()`；Feishu ensureProcess 使用 route binding address 回填 response mapping。
- 验收证据：`npm test -- --run tests/gateway.test.ts tests/feishu-router.test.ts tests/feishu-channel.test.ts tests/telegram.test.ts tests/feishu-config.test.ts tests/feishu-integration.test.ts` -> 6 files / 139 tests passed；`npm run typecheck` passed；`npm run build` passed；`HOME=/tmp npm test` -> 26 files / 401 tests passed。
- 生产验证：`com.pulse.relay` 已重启到 PID `92986`，`LastExitStatus=0`；日志显示 `All 13 TG bots started`、`Duke feishu WSClient started`、`All 1 Feishu bots started`；真实配置无 `bindings` 块时 legacy fallback dry-run 返回 `agentId=main`、`conversationKey=owner:main`、`runtimePreference=["codex","cc"]`。
- 生产边界：explicit binding read path 已有自动化验证，生产只验证 legacy fallback 未回归；真实 explicit binding UAT 留给 P1-001b 写路径完成后执行。

**P1-001b MVS / 执行记录（2026-04-27）**：

- 目标：把飞书 `/approve` 从写 `agent.feishuOpenId` 改为写 `relay.json.bindings[]` active binding record，并在当前 router 内存索引中即时生效。
- 风险：Feature risk = high-risk；Slice risk = high-risk，因为改 Feishu 生产审批写配置路径并重启生产。回滚方式：回滚提交 `0cdcce0`，如真实配置新增了错误 binding，则人工删除对应 `bindings[]` record 或恢复审批前备份，再重启 `com.pulse.relay`。
- 改动：`/approve` 生成带 `principalId`、`tenantId`、`defaultAgentId`、`allowedAgentIds`、`approvedAt`、`approvedBy`、`auditTags` 的 explicit active binding；写配置使用 tmp + rename；只清理被审批主体对应 agent 的 legacy `feishuOpenId`；router 新增 `upsertGatewayBinding()` 以更新运行中索引。
- 验收证据：`npm test -- --run tests/feishu-channel.test.ts tests/feishu-router.test.ts tests/gateway.test.ts tests/telegram.test.ts` -> 4 files / 124 tests passed；`npm run typecheck` passed；`npm run build` passed；`HOME=/tmp npm test` -> 26 files / 402 tests passed。
- 生产验证：`com.pulse.relay` 已重启到 PID `95871`，`LastExitStatus=0`；日志显示 `All 13 TG bots started (long polling)`、`Duke feishu WSClient started`、`All 1 Feishu bots started (WebSocket)`、scheduler initialized。真实配置 dry-run 在无 `bindings` 块时仍返回 `agentId=main`、`conversationKey=owner:main`、`runtimePreference=["codex","cc"]`。
- 生产边界：写路径已部署，但真实新增 binding 需要一个未绑定飞书 open_id 发起 pairing，再由已绑定管理员 `/approve`。当前 Duke 飞书自建应用没有明确“分享给新外部用户”的实际场景，因此 real pairing UAT 暂停，不为测试而造场景；P1-001 不能完全关闭，但也不继续扩开发。

## 3. 后续高风险切片门禁

以下任一切片开始前必须写单独 MVS，并在完成后做真实入口 UAT：

- 改 `src/index.ts` 启动 wiring。
- 改 `channels/telegram.ts` 或 `channels/feishu.ts` 的生产 handler。
- 改 `scheduler.ts` job result 语义。
- 改 Harness escalation delivery path。
- 改 runtime preference/fallback 入口。

## 4. Review / Handoff

- Duke：review principal、Agent identity、registry 关系，防止 channel-specific agent 身份固化。
- Reeve：review delivery receipt、静默失败升级、correlation id 是否满足审计。
- Ken：review implementation slice、迁移风险和回滚路径。
- Amanda：如需写入全局协作协议或 S1/S2 稳定化叙述，再请求 wording review。
