---
feature_name: "platform-tenant-provisioning"
version: "0.8.0"
created: "2026-05-01T12:00:00+08:00"
modified: "2026-05-29T14:45:00+08:00"
changelog:
  - "v0.8.0: 150-spec-sync — §1.9.2 支付状态主动同步；§1.5 Relay reload 重建 router；§1.4 错误码 feishu_app_id_in_use→409；§1.11 超时 5s→15s；§1.8.4 schema v2；§1.16 灰度测试开关；§1.8.1 组件树 BalanceInfo 替代 SubscriptionStatus"
  - "v0.7.0: 150-spec-sync 预付费按量扣减——§1.7 订阅标记废弃；§1.14 新增 BalanceStore + 余额 HTTP API + onPaid 充值到账；§3 数据流更新"
  - "v0.6.0: 150-spec-sync — §1.7 扩展订阅 API 客户端；§1.8 组件树更新（Step1PhonePayment → Step1PlanSelection + SubscriptionStatus）；§1.9 新增订阅端点与 Webhook"
  - "v0.5.0: §1.8 扩展——前端组件化、进度持久化、表单验证、移动端优化、内部文本清理"
  - "v0.4.2: P0-014 执行器落地——`OrderStore.onPaid` + `tenant-template.ts` + `tenant-materialize.ts` + `http-server.ts` eager allocator 接线；`provision.ts` `ensureTenantWorkspaceDir` 仅作为 Step 2 fallback 保留"
  - "v0.4.1: §1.13 模板目录 `Platform/templates/tenant-pulse-admin/` 已落地（5 文件 + README）；占位符语法定为 `{{var}}`；明确不引入 `.cursor/rules/`"
  - "v0.4.0: 150-spec-sync — §1.4 两阶段物化、§1.13 Agent 模板调研；数据流与 requirements v0.4.0 对齐"
upstream_dependency: ["requirements.md"]
tags: [design, l2-spec, platform, provisioning, billing, pre-paid]
spec_root: "Platform/specs"
---

# platform-tenant-provisioning — 技术设计（骨架）

> **状态**：M1 可测骨架已落地；数据模型与威胁模型仍随生产支付迭代。

## 1. 组件

| 组件 | 职责 |
|------|------|
| **Platform 服务**（`Platform`） | HTTP/Webhook 或 CLI；白名单；订单存储；provision 编排 |
| **Relay** | 读配置；长连；**reload** 入口待定（SIGHUP / admin API / 进程管理器） |

### 1.1 Mock 支付（P0-002，dev/staging 专用）

- **路由**：`POST /internal/mock-pay`，JSON body：`{ "orderId": string }`；成功返回 `{ orderId, status: "paid", already?: boolean }`（与 `OrderStore.markPaid` 语义一致；`failed` / `refunded` 订单返回 409）。
- **环境**：仅当 **`PLATFORM_ALLOW_MOCK_PAY` 精确为 `"1"`** 且 **`NODE_ENV` 不是 `production`** 时允许；生产环境即使误设开关也一律拒绝（403 `mock_pay_disabled`）。
- **实现落点**：`src/mock-pay-policy.ts`、`src/order-store.ts`、`src/http-server.ts`。

### 1.2 手机号白名单（P0-003）

- **模块**：`src/phone-allowlist.ts` — `normalizeCnPhone`、`PhoneAllowlist`、`PLATFORM_PHONE_ALLOWLIST_PATH`（文件，一行一号，支持 `#` 注释）、`PLATFORM_PHONE_ALLOWLIST_INLINE`（逗号分隔，与文件**合并**）。
- **门禁**：`POST /internal/orders` 仅对白名单内手机号创建 `pending` 订单（403 `not_allowlisted`）；`provisionFeishuTenantAfterPaid` 对订单手机号二次校验。

### 1.3 relay.json 合并与锁（P0-004）

- **锁**：`withFileLock` 使用与配置同卷的 `*.platform-lock`（`O_CREAT|O_EXCL` + 重试）。
- **合并**：`mergeFeishuTenantIntoRelayJson` — 幂等 upsert `users[]`（Feishu agent）与 `bindings[]`（`feishu_open_id` + `appId`）；JSON 写盘为临时文件 + `rename`。
- **备份**：`backupRelayConfig` 供编排层可选调用（写入 `.../backups/relay-<iso>.json`）。

### 1.4 Provision 编排 — 两阶段（v0.4.0）

**阶段 A — 订单 `paid`：租户工作区物化（FR-P0-014）**

- **触发**：支付终态 **`paid`**（Webhook、`markPaid`、或与 `paid` 等价的原子迁移）。
- **动作**：分配 **`tenantId`**（`tenant-id-allocator` + §1.10 规则）并 **`orderStore.setTenantId`**；在 **`PLATFORM_TENANTS_ROOT/{tenantId}/`** 按 **§1.13** 模板 **物化**「租户 pulse 管理员」workspace 文件集。
- **不做**：`mergeFeishuTenantIntoRelayJson`、**不**产生可参与路由的 **`bindings[]`**（飞书 `app_id` / secret 尚未经 Step 2 校验）。
- **幂等**：同一订单重复进入 `paid` 或回调重放 → 目录与订单上的 `tenantId` **不重复分配**、模板 **不覆盖用户已改文件**（具体策略见 `tasks.md` P0-014）。

**阶段 B — Step 2：Relay 配置（FR-P0-003）**

- **前置**：订单 **`paid`** + 白名单 + **订单已带 `tenantId`**（阶段 A 已执行）；已 `provisionedAt` 则短路 `already`（不重复 merge，仍可按需 **补全** workspace 模板缺失文件）。
- **侧效**：`mergeFeishuTenantIntoRelayJson` — 写 **`relay.json`**（Feishu agent + `bindings[]` pending）；审计见 §1.6；Relay reload 见 §1.5。
- **HTTP**：`POST /internal/provision` body `{ orderId, feishuAppId, feishuAppSecret }`；**其它字段**由服务端按 §1.10 **从订单与 `displayName` 派生**（`tenantId` 以订单为准，**非** Step 2 首次分配）。未配置路径时 **503** `provision_unconfigured`。
- **错误码**：飞书凭证无效 → **400**；`feishu_app_id_in_use`（同一 appId 已被其他租户占用）→ **409**；飞书 API 不可达 / 意外响应 → **502**；relay.json merge 失败 → **502**。

> **L3 落点（v0.4.2）**：阶段 A 由 `OrderStore.onPaid` 钩子驱动，HTTP 层 `createHttpServer` 在配置了 `tenantsRoot` 时同步初始化 `TenantIdAllocator` 并注册监听器；监听器调用 `tenant-materialize.ts → tenant-template.ts` 完成模板递归拷贝 + `{{var}}` 替换。阶段 B（`provision.ts`）保留 `ensureTenantWorkspaceDir` 作为 fallback（内联生成最小 `CLAUDE.md`），仅在阶段 A 未跑过的旧订单 / 单测路径起兜底作用，**不**与模板冲突（模板若先生成则 `existsSync` 命中，`ensureTenantWorkspaceDir` 自动短路）。

### 1.5 Relay reload（P0-006）

- **首选**：HTTP `POST $PLATFORM_RELAY_HTTP_URL/internal/agents/reload`（loopback；详见 `Relay/specs/features/feishu-pairing-http/design.md`）。Platform 在 Step 2 写盘成功后立即触发；失败仅记 audit `reload_failed`，**不**回滚 binding。
- **Relay 侧行为（v0.8.0）**：Relay reload 端点先调用 `router.reload()` 从磁盘重新读取 `relay.json` 并重建所有索引，再调用 `feishuBots.reload()` spawn 新飞书 WS client。无需重启 Relay 即可加载新 provision 的飞书 bot。
- **兜底**：未配置 `PLATFORM_RELAY_HTTP_URL` 时回落 `PLATFORM_RELAY_RELOAD_CMD`（shell 字符串，`spawn` detached + `unref`）。具体 systemd/IPC 由运维与 Relay 仓库约定。

### 1.6 审计（P1-002，M1）

- **`PLATFORM_AUDIT_LOG_PATH`**：JSONL，关键事件 `provision_ok` / `provision_failed`（含 `orderId`、`tenantId`、`detail.message`）。

### 1.7 生产支付（P1-001，契约）+ 订阅（v0.6.0 → v0.7.0 废弃）

- **`PaymentProvider.verifyNotification`**：由未来微信/支付宝 webhook 实现；当前 **`UnconfiguredPaymentProvider`** 返回 `ignored`，避免静默误当成功。
- **`ProductionPaymentNotImplementedError`**：供生产接 SDK 时显式 fail-closed 使用。
- **~~v0.6.0 订阅 API 客户端~~（v0.7.0 废弃）**：`src/subscription-client.ts` 保留但不再使用。计费模型已改为预付费按量扣减（requirements v0.6.0），订阅相关路由返回 410 Gone。

### 1.8 Web 用户界面（M1 → v0.5.0 组件化重构）

- **技术**：`web/` — Vite 6 + React 18；`npm run build:web` 输出至 **`dist/web`**，由 `http-server` 与 API **同源**提供。
- **视觉**：对齐 Coevolution「大鸟聪聪官网」**Aurora**（深色、琥珀橙 `#F59E0B`、深紫辅助、玻璃卡片、Inter、圆角药丸按钮 / Hero 光晕）。

#### 1.8.1 组件架构（v0.5.0）

前端从单文件 `App.tsx`（693 行）拆分为组件树：

```
App（协调器，状态 + hooks + 组件编排）
├── NavBar                    — 固定顶栏
├── HeroHeader                — Hero 区 + 脉冲背景
├── ProgressBar               — 三步进度指示器（圆点 + 连线）
├── Step1PlanSelection        — 手机号/用户名 + 模型组选择 + 充值金额 + 支付二维码（v0.8.0 预付费）
├── Step2FeishuCredentials    — 飞书凭证表单
├── Step3PairingCode          — 配对码输入 + 重试
├── Step4Success              — 完成卡片
│   └── BalanceInfo           — 余额信息子卡片（余额/模型组/充值提示）
├── StatusMessage             — 错误/成功消息横幅
└── Footer                    — 简化页脚
```

共享模块：
- `types.ts` — 共享 TypeScript 类型
- `api.ts` — `baseUrlJoin`、`WEB_BASE`、`api<T>()` fetch 封装
- `useWizardProgress.ts` — localStorage 进度持久化 hook
- `transitions.css` — 步骤切换动画 keyframes

#### 1.8.2 用户界面原则（v0.5.0）

- **不暴露内部状态**：服务端配置 badges（模拟支付/扫码收银/Provision/Pairing）、内部订单号、支付订单号、环境变量名等技术文本已从用户视图移除。
- **按钮文案简洁**："下一步"、"验证并继续"、"重试"、"完成绑定"——不含技术动词。
- **步骤导航**：用圆点进度条替代文字 badges；移动端仅显示圆点。

#### 1.8.3 表单验证（v0.5.0）

| 字段 | 规则 | 时机 |
|------|------|------|
| 手机号 | 11 位数字 | 失焦 + 提交 |
| 用户名 | 非空 | 提交 |
| App ID | 非空 | 提交 |
| App Secret | 非空 | 提交 |
| 配对码 | 6 位字母数字 + 字符计数显示 | 实时 |

验证反馈：红色 `.input-error` 边框 + `.field-error` 内联文本。

#### 1.8.4 进度持久化（v0.5.0 → v0.8.0 预付费 schema v2）

- **存储**：`localStorage` 键 `pulse_wizard`，JSON。
- **版本**：`_v: 2`（v1 不兼容，恢复时丢弃）。
- **存储字段**：`_v`、`step`、`phone`、`displayName`、`orderId`、`orderStatus`、`topUpAmount`、`modelGroup`、`feishuAppId`、`tenantId`、`agentId`、`agentName`。
- **绝不存储**：`feishuAppSecret`、`pairingCode`、`nativeData`（QR base64）、`caps`。
- **过期**：24h 自动丢弃；`step === 4` 时立即清除。
- **恢复验证**：恢复 `orderId` 后调 `GET /internal/orders/{id}` 确认订单仍存在；不存在则从头开始。

#### 1.8.5 移动端优化（v0.5.0）

- `input font-size: 16px`（阻止 iOS 自动缩放）
- 主按钮 `width: 100%`；scenario 按钮垂直堆叠
- QR 图像 `max-width: 200px`
- `touch-action: manipulation` 消除 300ms 点击延迟
- `env(safe-area-inset-bottom)` 适配刘海屏
- Hero 区缩短 `min-height: 28vh`

- **开发联调**：终端 1 `npm run dev`（Platform）；终端 2 `npm run dev:web`（Vite 5173，反向代理 `/internal`、`/health`、`/ping`、**`/api`** 至 `127.0.0.1:8787`）。
- **安全**：与 §4 一致；生产应对控制平面做网络隔离或鉴权，**不在**本页内置登录态。

### 1.9 微信 Native 收银台契约（对齐「大鸟聪聪官网」`payment/index.html`）

- **路由**：`GET /api/payment/create-order`
  - **透传**：若设置 **`PLATFORM_PAYMENT_PROXY_URL`**（完整 URL，含 path），将查询参数合并后 **fetch 转发**，响应体/状态码原样返回（便于直挂现有 `create-order` 网关，字段 `success` / `qrcode_base64` / `order_no` / `generated_at` 与官网一致）。
  - **内聚 Mock**：未配置代理时，非生产且 **`PLATFORM_NATIVE_PAY_MOCK=1`**：校验 `internalOrderId` + `phone` 与 `OrderStore` 白名单后，生成 **PNG 二维码 Base64**（载荷为 JSON，**非**真实 `weixin://`；仅 UI/联调用）。
  - **查询参数（内聚模式）**：`type`（`test` \| `member`）、`phone`、`internalOrderId`（Platform 内部 `ord_*`）。
- **能力位**：`/internal/capabilities` 增加 `nativePayment`、`paymentInstructions`、`paymentRefreshInterval`、`paymentScenarios`（与 `static-site/site/content/payment.json` 文案对齐）。
- **实现落点**：`src/payment-create-order.ts`、`src/http-server.ts`；前端步骤 2 复刻官网支付卡与倒计时刷新节奏。
- **v0.6.0 向后兼容**：订阅模式启用时（`subscriptionPlans` 非空），前端不再调用此端点，改为 `POST /internal/subscription/create`。

#### 1.9.2 支付状态主动同步（v0.8.0）

> App Platform webhook 回调不可靠（生产实测：微信支付成功后回调未到达）。前端轮询订单状态时，服务端主动查 App Platform 支付 API 作为补偿。

- **触发**：`GET /internal/orders/{orderId}` 且订单仍为 `pending` 时。
- **行为**：调用 App Platform `GET /payments/?order_id={scopedOrderId}`，若任一记录 `status === 'paid'`，立即 `markPaid` 再返回。
- **模块**：`src/app-platform-payment.ts` → `syncPaymentStatusFromProvider()`。
- **效果**：支付确认延迟从 ~30s（等下一次前端刷新 QR 触发）降至 ~3s（下一次前端轮询周期即捕获）。

### 1.9.1 ~~订阅端点（v0.6.0 → v0.7.0 废弃）~~

- **~~创建订阅~~**：`POST /internal/subscription/create` → 返回 **410 Gone**（计费模型已改为预付费）
- **~~订阅 Webhook~~**：`POST /internal/webhooks/subscription` → 返回 **200 OK**（静默忽略重放）
- **能力位变更**：`GET /internal/capabilities` 新增 `billingModel: 'prepaid'`、`topUpPresets: [50,100,200,500]`、`modelGroups`；`subscriptionPlans` 字段移除

### 1.10 自动命名规则（v0.3.0，FR-P0-010）

| 字段 | 来源 | 规则 |
|------|------|------|
| `tenantId` | 服务端分配 | `member-NNNN`（至少 4 位，已有租户最大 +1；冷启动扫描 `PLATFORM_TENANTS_ROOT/member-*`） |
| `principalId` | 等同 `tenantId` | — |
| `workspaceId` | 等同 `tenantId` | — |
| `agentId` | 派生 | `${tenantId}-pulse`（替代旧 `BigBird Agent` / `feishu-${tenantId}`） |
| `bindingId` | 派生 | `bind-${tenantId}` |
| `name` | 用户输入 + 模板 | `${displayName}的pulse管理员` |
| `emoji` | 默认 🐦 | 暂不开放 UI 修改 |
| `feishuOpenId` | Step 3 写入 | Step 2 写盘时 `address.value=''`、`status='pending'` |

> **tenantId 分配时机（v0.4.0）**：在订单 **`paid`（阶段 A）** 分配并写入订单；Step 2 **消费**订单上的 `tenantId`，**不**再重新抽签。v0.3.x 实现 Drift：历史上曾在 Step 2 内首次分配，以 L3 迁移为准。  
> 序列分配器实现：`tenant-id-allocator.ts` —— 启动时扫描 `PLATFORM_TENANTS_ROOT`，**也**扫一遍 `relay.json.bindings[].tenantId`（防止 Tenants 目录被人工删除导致编号回退碰撞）。

### 1.11 Feishu 凭证校验（v0.3.0，FR-P0-011）

- **模块**：`feishu-credentials.ts`，方法 `verifyFeishuAppCredentials({appId, appSecret, fetchImpl?})`：
  - POST `https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal`，body `{ app_id, app_secret }`，超时 15s。
  - 返回 `{ ok: true, tenantAccessToken, expire }` 或 `{ ok: false, code, msg }`（飞书原文）。
- **HTTP 表面**：Step 2 后端调用前置在 `/internal/provision` 内；校验失败 **不**写 `relay.json`，HTTP 返回 `400 { error: 'invalid_feishu_credentials', code, msg }`。
- **测试**：`tests/feishu-credentials.test.ts` 注入 mock `fetch`，覆盖正确凭证、错误 secret、网络超时三条路径。

### 1.12 配对 HTTP（v0.3.0，FR-P0-009）

- **新增 HTTP**：`POST /internal/pair`（Platform）— body `{ orderId, code }`；服务端：
  1. 校验 `orderStore.get(orderId)?.status === 'paid'` 且 `provisionedAt != null`（即 Step 2 已完成）。
  2. 通过 `relay-pairing-client.ts` POST `$PLATFORM_RELAY_HTTP_URL/internal/pairing/approve`，body `{ code, principalId, agentId }`；超时 5s。
  3. 成功 → audit `pair_ok`；失败 → 透传 Relay 返回的 4xx/5xx + 文案，audit `pair_failed`。
- **错误分类**：`pair_code_unknown`（Relay 无该配对码）、`pair_agent_mismatch`（配对码所属 agent 与 orderId 不一致）、`pair_relay_unreachable`（HTTP 错误）。

### 1.13 Agent 模板调研与 SSOT（v0.4.0 · 150-spec-sync）

#### 调研结论：是否已有「会员租户」Agent 模板？

| 来源 | 结论 |
|------|------|
| **`Platform`（当前主线）** | **已** 具备版本化模板目录 SSOT：`Platform/templates/tenant-pulse-admin/`（FR-P0-015）；**阶段 A（`paid`）递归拷贝**（P0-014）由 `OrderStore.onPaid` + `tenant-template.ts` 落地（v0.4.2）。`src/relay-merge.ts` 的 `ensureTenantWorkspaceDir` 仍在阶段 B / 旧路径兜底：目标目录 **不存在** `CLAUDE.md` 时 **内联生成** 最小租户身份 Markdown；由 `provisionFeishuStep2`（含 **already** 分支）调用。 |
| **`Bigbird/_archived/BADPulse/`** | 归档代码与 Spec 中存在 **租户卷部署 / shared templates / factory-director** 等与「从模板复制 workspace」相关的设想与任务；**不**作为当前 Platform 实现依赖，仅考古参考。 |
| **仓库根 `pulse-space/`** | **平台** Pulse OA 工作区（多文件：`CLAUDE.md`、`MEMORY.md`、`IDENTITY.md`、`AGENTS.md` 等）。Spec **禁止**将其整棵复制为会员租户默认模板（职责混同 + 体积与敏感信息风险）。 |

#### 目标 SSOT（FR-P0-015）

- **路径（已落地，v0.4.1）**：`Platform/templates/tenant-pulse-admin/`。
- **初版最小文件集（v0.4.1，5 内容文件 + 1 README）**：
  - **`CLAUDE.md`**：租户 pulse 管理员身份 + **非**平台管家 + 路由决策树 + 失败话术。
  - **`IDENTITY.md`**：使命 + 上下游（租户 → 上抛 PULSE 平台管家 Pulse）。
  - **`SOUL.md`**：行为边界 N1–N5（跨租户 / 平台运维 / 支付与账户 / 密钥 / 仓库级写入）+ 越权回复模板。
  - **`TOOLS.md`**：CC 工具白名单（与 `relay.json.allowedTools` 对齐：`Read/Write/Edit/Glob/Grep/Bash/Agent/Task`），明确禁止命令清单。
  - **`MEMORY.md`**：长期记忆占位（仅本租户事实，不存秘密；追加优先）。
  - **`README.md`**（仅模板源；**不**拷贝到租户目录）：本目录 SSOT 说明 + **非** `pulse-space/` 拷贝声明。
- **占位符语法**：**`{{var}}`**（Mustache 风格），可用变量 `tenantId` / `agentId` / `agentName` / `displayName` / `provisionedAtIso`；不引入 shell `${var}` 防混淆。
- **暂不引入**：`.cursor/rules/`、`AGENTS.md`、`HEARTBEAT.md`、`role-definitions.yaml`、`memo/`、`memory/`、`backups/`（避免把平台运维语义下放给租户）。
- **物化**：阶段 A（`paid`）从模板 **递归拷贝** 并做占位符替换；**不覆盖** 已存在且非占位文件；**`README.md`** 排除在拷贝范围外（策略在 P0-014 任务中细化）。

### 1.14 余额账户 BalanceStore（v0.7.0）

> 对齐 requirements v0.6.0 FR-P0-020~024。替代已废弃的订阅制。

- **模块**：`src/balance-store.ts` — 进程内 `Map<string, BalanceAccount>` + JSON 文件持久化（路径由 `PLATFORM_DATA_DIR` 配置，进程退出时自动保存）
- **数据模型**：
  ```typescript
  interface BalanceAccount {
    tenantId: string;
    balance: number;        // 当前余额（CNY 分）
    totalRecharged: number; // 累计充值（CNY 分）
    totalConsumed: number;  // 累计消费（CNY 分）
    createdAt: number;
    updatedAt: number;
  }
  ```
- **核心方法**：
  - `createOrTopUp(tenantId, amountFen)` — 首次创建或追加充值（幂等：同一 tenantId 仅创建一次账户，后续为 top-up）
  - `deduct(tenantId, amountFen)` — 扣减余额，返回 `DeductResult`（`ok` / `insufficient_balance` / `account_not_found`）；`totalConsumed` 同步累加
  - `getBalance(tenantId)` → `number | null`（余额，分；未找到返回 null）
  - `hasBalance(tenantId)` → `boolean`（余额 > 0）
- **onPaid 充值到账**：`http-server.ts` 在 `OrderStore.onPaid` 钩子中检查订单 `topUpAmount`，有则调用 `balanceStore.createOrTopUp(tenantId, topUpAmount * 100)`
- **Unit**：内部一律使用 **CNY 分（fen）** 避免 IEEE 754 浮点精度问题；对外展示时才除以 100 转元
- **测试**：`tests/balance-store.test.ts`（11 tests）

### 1.15 余额 HTTP API（v0.7.0）

> 供 Relay 回调扣减/查询租户余额（FR-P0-022/024）。

| 路由 | 方法 | 说明 |
|------|------|------|
| `/internal/balance/:tenantId` | GET | 查询余额 → `{ balance, totalRecharged, totalConsumed }` 或 `404` |
| `/internal/balance/deduct` | POST | 扣减余额 → `{ tenantId, costFen }` → `{ ok: true }` 或 `{ ok: false, reason: 'insufficient_balance' }` |
| `/internal/capabilities` | GET | `billingModel: 'prepaid'`、`topUpPresets`、`modelGroups` 替代旧 `subscriptionPlans` |

**Relay 集成**（`index.ts` wire）：
- `deductBalance` 回调 → `POST /internal/balance/deduct`（fire-and-forget）
- `getBalance` 回调 → `GET /internal/balance/:tenantId`（fail-open）

### 1.16 灰度测试开关（v0.8.0）

> 生产上线前的测试支撑。¥0.01 测试金额和 ×10 到账倍率仅在灰度模式下启用。

- **环境变量**：`PLATFORM_GRAYSCALE_TEST=1`
- **启用时行为**：
  - `topUpPresets` 增加 ¥0.01 选项：`[0.01, 50, 100, 200, 500]`
  - `minTopUpYuan` 降至 `0.01`
  - ¥0.01 支付到账 ¥10（乘 1000 倍）
  - `paymentScenarios.test` 场景出现
- **未启用时行为**（生产默认）：
  - `topUpPresets: [50, 100, 200, 500]`
  - `minTopUpYuan: 50`
  - 所有金额按 1:1 到账
  - 无 test 场景

## 2. 待决问题（必须在 M1 前收敛）

- ~~`relay.json` **merge 策略**~~（M1：Platform 单写者 + 文件锁；多实例需队列/K8s lease）
- **支付** 第一期：**Mock** 与 **生产**（微信/支付宝）接口边界 — **契约已就绪**，SDK 待接
- **密钥**：飞书 `app_secret` 存放（环境变量、密钥文件、未来 KMS）
- **tenantId** 与 **飞书 app_id** 映射表 schema

## 3. 数据流（v0.7.0 预付费按量扣减）

```
手机号验证 + 白名单 → 选择模型组 + 充值金额 → 创建充值订单 pending → 微信支付 → paid
    → 阶段 A：分配 tenantId + 余额到账(createOrTopUp) + 模板物化 Tenants/{tenantId}/
    → Step 2：校验飞书凭证 → merge relay.json（binding pending）→ Reload Relay
    → Step 3：配对码 → binding active → CC cwd = Tenants/{tenantId}/

CC 运行时（每 turn）:
    CC assistant end_turn → pool.ts finishTurn(embed usage) → metering.record()
    → pricing.calculateCost() → balanceStore.deduct() → JSONL 日志写入
    → 低余额告警 / 余额耗尽停服
```

## 4. 安全

- Webhook **签名校验**、重放防护、**幂等键**（`order_id` + payment provider id）
- 最小权限写 `relay.json`（专用系统用户）
- **内部 HTTP**（`/internal/*`）假定受网络层或后续 mTLS/鉴权保护；本包未内置 API Key

## 5. 参考

- `Relay/specs/features/logical-gateway/design.md` — `ChannelBinding` 与 `relay.json.bindings[]`
