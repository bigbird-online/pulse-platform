---
feature_name: "platform-tenant-provisioning"
version: "0.6.1"
created: "2026-05-01T12:00:00+08:00"
modified: "2026-05-29T14:45:00+08:00"
changelog:
  - "v0.6.1: 150-spec-sync — FR-P0-006 reload 重建 router；FR-P0-007 灰度测试金额 PLATFORM_GRAYSCALE_TEST=1"
  - "v0.6.0: 计费模型从月度订阅改为预付费按量扣减：移除 FR-P0-016/017/018/019（订阅制）；FR-P0-007 改为充值+模型选择；新增 FR-P0-020~025（余额账户、充值、扣减、低余额告警、余额耗尽停服）；关联 metering v1.0.0"
  - "v0.5.0: 150-spec-sync — 订阅制改造：Step 1 从一次性 QR 码支付改为月度订阅（¥299/月自动续费）；新增 FR-P0-016/017/018；FR-P0-007 更新；前端组件 Step1PhonePayment → Step1PlanSelection"
  - "v0.4.2: FR-P0-014 执行器落地——`OrderStore.onPaid` 钩子在 `markPaid` 成功路径同步触发租户模板物化；`AC-P0-008` 由 `tests/stage-a-http.test.ts` 端到端覆盖"
upstream_dependency:
  - "Platform/specs/project-specs/project-vision.md"
  - "Relay/specs/features/tenant-onboarding-feishu/requirements.md"
  - "Relay/specs/features/feishu-pairing-http/requirements.md"
  - "Relay/specs/features/logical-gateway/design.md"
  - "Relay/specs/project-specs/project-vision.md"
  - "Relay/specs/features/metering/requirements.md"
tags: [requirements, l2-spec, platform, provisioning, feishu]
spec_root: "Platform/specs"
---

# platform-tenant-provisioning — 需求

> **L2（平台层）**：实现 **`Platform`** 侧能力，满足 `tenant-onboarding-feishu` **FR-S1-001**、**AC-S1-003**（手机白名单、注册流内充值、**先充值成功再** 写 **`relay.json` / `bindings[]`**；无邀请码；**一户一飞书应用**；身份锚点为 **已验证手机号**；**预付费按量扣减**计费模型）。

## 1. 目标

- 提供 **三步开通流水线**，用户只需输入：**(Step 1)** 手机号 + 用户名（显示名）+ 充值金额 → **(Step 2)** 飞书 App ID + App Secret → **(Step 3)** 飞书 bot 回复的 6 位配对码。其它内部字段（`tenantId`/`principalId`/`agentId`/`bindingId`/`workspaceId`/`name`/`emoji`/`feishuOpenId`）一律按规则自动生成或从外部源回填。
- **充值成功后**：即建立 **`Tenants/{tenantId}/` 租户工作区**，创建余额账户，并按 **Agent 模板**（见 FR-P0-015 / design §1.13）物化 **租户 pulse 管理员** 所需的初始文件，使 CC **`cwd`** 下不再继承平台根 `CLAUDE.md` 身份。
- 全流程审计可追溯，**未充值成功** 不得在持久化层产生 **可用于生产的** Relay **`bindings[]`**（与 onboarding MVS 一致）。
- 配对成功后 Relay 立即 hot-reload 该飞书 bot 的 WS client + binding，**不**中断其他已活跃租户。

## 2. 边界

- **不做** Relay 进程内业务逻辑改造（除 **HTTP 配对接口** 与 **bot 热加载契约** 外）。
- **不做** 匿名全网注册；**不使用邀请码**。
- **不兼容** 无 **已验证手机号** 可关联的会员模型。
- **飞书开放平台** 应用的创建/审批：第一期允许 **人工创建 + 凭证录入**（Step 2 实时校验），自动化可后续立项。
- **租户 workspace 模板 ≠ 平台 `pulse-space/`**：不得将会员租户工作区等同于仓库根 **平台管家** OA 目录（整棵复制属越权信息泄露与设计错误），见 FR-P0-015。

## 3. 功能需求

### 3.1 流程主线

| ID | 描述 | 优先级 |
|----|------|------|
| FR-P0-001 | **白名单**：仅允许 listed 手机号进入注册/支付流（存储与运维方式见 design） | P0 |
| FR-P0-002 | **充值状态机**：充值订单 `pending` → `paid` / `failed` / `refunded`；**仅 `paid`** 触发余额到账 + **租户空间物化**（FR-P0-014） | P0 |
| FR-P0-003 | **Relay Provision（Step 2）**：飞书凭证校验通过后 **merge** **`relay.json`**（`users`/`agents`/`bindings[]` pending），并与该租户 **飞书 `app_id` 凭证** 关联；**不得早于** `paid` | P0 |
| FR-P0-004 | **先充值后绑**：任何写 **`relay.json` / 生产 binding** 的步骤不得早于充值终态确认（含失败重试与回调乱序防护） | P0 |
| FR-P0-005 | **幂等与可观测**：重复回调、重复请求不得产生重复冲突 binding；关键步骤 structured log / audit id | P0 |
| FR-P0-006 | **Relay reload**：在 **Step 2** 写 `relay.json` 成功后通过 Relay HTTP `POST /internal/agents/reload` 加载新 feishu bot；Relay 侧 reload 端点先重建 router 索引（重新读 `relay.json`）再 spawn WS client，无需重启 Relay。未配置时 fallback `PLATFORM_RELAY_RELOAD_CMD`（仅运维兜底）。**充值瞬间 alone 不触发 reload**（尚无 feishu agent 配置） | P0 |

### 3.2 三步用户流（v0.3.0）

| ID | 描述 | 优先级 |
|----|------|------|
| FR-P0-007 | **Step 1** UI 采集 `phone` + `displayName`，通过白名单后展示模型组选择 + 充值金额。模型组决定租户使用的模型（国内/国外）和定价策略（见 `metering/requirements.md` §6 定价配置）。充值金额最低 ¥50，可选预设金额（¥50/100/200/500）或自定义。灰度测试模式下（`PLATFORM_GRAYSCALE_TEST=1`）额外开放 ¥0.01 选项（到账 ¥10）。用户确认后创建充值订单，微信内浏览器直接跳转微信支付，非微信环境展示支付二维码。支付成功后订单变为 `paid`，自动进入 Step 2 | P0 |
| FR-P0-008 | **Step 2** UI 仅采集 `feishuAppId` + `feishuAppSecret`；服务端实时调用飞书 `auth/v3/tenant_access_token/internal` 校验，**失败**则返回飞书原始 `code`/`msg`；通过则 **merge** `relay.json`（binding `status = pending`、`address.value = ''`）；`tenantId` **已**在充值 `paid` 时分配并绑定订单 | P0 |
| FR-P0-009 | **Step 3** UI 仅采集 6 位配对码；服务端通过 Relay HTTP `POST /internal/pairing/approve` 完成绑定；用户即可在飞书直接对话 | P0 |
| FR-P0-010 | **自动生成命名规则**：`tenantId = member-NNNN`（按已有租户最大编号 +1）；`principalId = workspaceId = tenantId`；`agentId = {tenantId}-pulse`；`bindingId = bind-{tenantId}`；`name = {displayName}的pulse管理员`；`emoji = 🐦` | P0 |
| FR-P0-011 | **凭证校验** Step 2 必须真实命中飞书 OpenAPI；不允许仅做正则校验后落盘 | P0 |
| FR-P0-012 | **错误回滚**：Step 2 写盘后若 Relay reload 失败、或 Step 3 在 24h 内未完成，Platform 须能撤销 binding（`status = revoked`）并保留审计 | P1 |

### 3.3 租户工作区与 Agent 模板（v0.4.0 · 150 对齐）

| ID | 描述 | 优先级 |
|----|------|------|
| FR-P0-014 | **`paid` 里程碑**：充值订单进入 `paid` 后立即（与同订单幂等）：分配 **`tenantId`** 并写入订单；为租户创建 **余额账户**（初始余额 = 充值金额，见 FR-P0-020）；在 **`PLATFORM_TENANTS_ROOT/{tenantId}/`** 按 **FR-P0-015** 模板 **物化租户 pulse 管理员工作区**（含身份与边界说明）；**不**写入 `relay.json`、**不**产生可路由的 binding（飞书凭证尚未验证）。重复 `paid` / Webhook 重放须幂等 | P0 |
| FR-P0-015 | **Agent 模板 SSOT**：模板源为仓库内受版本管理的目录 **`Platform/templates/tenant-pulse-admin/`**（**v0.4.1 已落地**，5 内容文件 + README）；最小文件集与占位符（`{{var}}`：`tenantId` / `agentId` / `agentName` / `displayName` / `provisionedAtIso`）由 **design §1.13** 给出；递归拷贝由 **P0-014** 执行器落地（**v0.4.2**）。**禁止**用仓库根 **`pulse-space/`** 作为会员租户默认模板 | P0 |

### 3.4 预付费余额账户（v0.6.0）

> 计费模型从月度订阅（v0.5.0）改为预付费按量扣减。移除订阅制相关 FR-P0-016/017/018/019。

| ID | 描述 | 优先级 |
|----|------|------|
| FR-P0-020 | **余额账户**：每个租户拥有一个人民币余额账户，存储于 Platform 持久化层。字段包括 `tenantId`、`balance`（当前余额，单位 CNY 分）、`totalRecharged`（累计充值）、`totalConsumed`（累计消费）、`createdAt`、`updatedAt`。充值 `paid` 时自动创建（若不存在）并增加余额 | P0 |
| FR-P0-021 | **充值流程**：开通时 Step 1 充值（FR-P0-007）；开通后租户可通过飞书命令 `/recharge` 或管理页面发起新充值。充值渠道：微信支付扫码（v1），支付宝扫码（v2）。充值订单独立于用量扣减订单，`paid` 后余额立即到账。充值记录持久化（金额、渠道、时间、订单号） | P0 |
| FR-P0-022 | **按消息扣减**：Relay 在每条 CC 消息（`assistant` 记录，`stop_reason='end_turn'`）完成后，根据模型定价（见 `metering/requirements.md` §6 定价配置）计算本次消息费用（国内模型直接人民币定价，国外模型 USD 定价 × 实时汇率），从租户余额中扣减。扣减记录写入 metering 日志（见 `metering/requirements.md`） | P0 |
| FR-P0-023 | **低余额告警**：租户余额 < ¥10 时，通过飞书 Bot 主动发送通知："💡 余额不足 ¥{balance}，请及时充值以保证服务不中断。"每次充值后重置告警状态。余额介于 ¥10~¥50 时，在 Agent 回复末尾追加轻量提示（不阻断对话） | P1 |
| FR-P0-024 | **余额耗尽停服**：租户余额 ≤ ¥0 时，Relay 拒绝新的 CC spawn 请求，飞书 Bot 回复固定消息："⚠️ 余额已用完，请充值后继续使用。发送 /recharge 充值。"不影响进行中的 session（当前 turn 完成后再停） | P0 |
| FR-P0-025 | **模型组切换**：租户可通过飞书命令 `/switch-model` 切换模型组（国内/国外）。切换触发冷迁移流程（见 `multi-node-session-continuity/requirements.md`），上下文从旧节点迁移到新节点。切换后定价策略同步变更。模型组信息写入 `relay.json` 的 binding 配置 | P1 |

## 4. 验收标准

- **AC-P0-001**：白名单外手机号 **无法** 进入充值 `paid` 后 **租户空间物化** 闭环。
- **AC-P0-002**：充值订单未 `paid` 时，目标租户 **无** 新增可用于生产的 `bindings[]` 记录（相对测试基线）。
- **AC-P0-003**：充值订单 `paid`（含 Mock 支付）后，在约定 SLA 内：**(a)** 余额账户已创建且余额 = 充值金额；**(b)** `Tenants/{tenantId}/` 已按模板物化；**(c)** Step 2 后 **relay** + reload，飞书测试 Bot 可收到路由（与 **AC-S1-003** 联合验收）。
- **AC-P0-004**（v0.3.0）：用户从打开页面到飞书可对话，**只输入** phone、displayName、feishuAppId、feishuAppSecret、pairingCode；**任何**其它字段由服务端按 FR-P0-010 派生。
- **AC-P0-005**（v0.3.0）：Step 2 输入错误的 `feishuAppSecret` 时，UI 必须看到飞书返回的 `code`/`msg`，**不**得写 `relay.json`。
- **AC-P0-006**（v0.3.0）：Step 2 写入 `relay.json` 后 Relay 在 1s 内（loopback HTTP）spawn 该 feishu bot 的 WS client，且**不**中断其它会话。
- **AC-P0-007**（v0.3.0）：Step 3 输入正确配对码后，`bindings[].status` 由 `pending` 变 `active`，`address.value` 写入真实 `feishuOpenId`，前端在 ≤ 2s 内收到成功反馈。
- **AC-P0-008**（v0.4.0）：充值 `paid` 后 `Tenants/{tenantId}/` 存在且包含模板规定的 **至少** `CLAUDE.md`（及 §1.13 所列其它必备文件）；在该目录作为 **唯一** workspace 启动 CC 时，助手身份为 **租户 pulse 管理员**，**不得**默认自认 **PULSE 平台管家 Pulse**。
- **AC-P0-009**（v0.6.0）：充值成功后余额账户 `balance` = 充值金额，`totalRecharged` 正确累加。多次充值幂等（同一订单号不重复到账）。
- **AC-P0-010**（v0.6.0）：CC 每完成一个 turn（`assistant` + `end_turn`），租户余额按模型定价扣减，扣减金额 = `input_tokens × input_price + output_tokens × output_price`（国外模型 × 汇率）。扣减后 metering 日志包含 `cost_cny` 字段。
- **AC-P0-011**（v0.6.0）：租户余额 ≤ ¥0 时，新消息触发固定回复 "⚠️ 余额已用完"，不启动 CC 进程。进行中 session 的当前 turn 正常完成后停止。
- **AC-P0-012**（v0.6.0）：租户余额 < ¥10 时，飞书 Bot 主动推送低余额通知（仅一次，充值后重置）。

## 5. 关联

- 实现代码：`Platform/`
- 飞书通道（Relay L2）：`Relay/specs/features/feishu-channel/`
- 飞书配对 HTTP（Relay L2）：`Relay/specs/features/feishu-pairing-http/`
- Gateway SSOT：`Relay/specs/features/logical-gateway/design.md`
- 用量计费（Relay L2）：`Relay/specs/features/metering/requirements.md`
- 多节点冷迁移：`Relay/specs/features/multi-node-session-continuity/requirements.md`
