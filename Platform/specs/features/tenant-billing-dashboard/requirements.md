---
feature_name: "tenant-billing-dashboard"
version: "0.5.0"
created: "2026-05-29T19:00:00+08:00"
modified: "2026-06-01T22:30:00+08:00"
upstream_dependency: ["platform-tenant-provisioning"]
tags: [requirements, l2-spec, platform, billing, dashboard, recharge]
spec_root: "Platform/specs"
changelog:
  - version: "0.5.0"
    date: "2026-06-01"
    changes: "150-spec-sync：FR-P0-009 shadow account 消费追踪；FR-P0-010 tracked 响应约定；DeductResult.shadow 标志"
  - version: "0.3.0"
    date: "2026-05-31"
    changes: "150-spec-sync：Order.source 区分充值/注册；灰度测试仅限注册订单；国外模型禁用；hashchange 路由监听；document.title 同步"
  - version: "0.2.0"
    date: "2026-05-31"
    changes: "新增独立充值页（RechargePage）、路由方案、支付流程、已实现组件清单"
  - version: "0.1.0"
    date: "2026-05-29"
    changes: "初始版本：余额查询、充值/消费记录、Billing 页面"
---

# tenant-billing-dashboard - 功能需求文档

> **功能概述**：为租户和管理员提供余额查看、充值、消费记录的完整账户中心。已开通用户可独立充值（不触发 provisioning）。
>
> **关联文档**：
> - platform-tenant-provisioning: `Platform/specs/features/platform-tenant-provisioning/`
> - balance-store: `Platform/src/balance-store.ts`
> - order-store: `Platform/src/order-store.ts`

---

## 1. 用户故事

### 租户视角
**故事1：查看我的余额**
- **作为**：已开通的 PULSE 租户
- **我希望**：随时查看当前账户余额
- **以便**：了解剩余可用额度，及时充值

**故事2：查看充值记录**
- **作为**：租户
- **我希望**：看到所有充值记录（时间、金额、状态）
- **以便**：对账和审计

**故事3：查看消费明细**
- **作为**：租户
- **我希望**：看到每笔消费的详情（时间、金额、用途）
- **以便**：了解成本构成

**故事4：独立充值**
- **作为**：已开通的租户
- **我希望**：通过独立页面选择金额、扫码支付完成充值
- **以便**：余额不足时快速续费，无需联系管理员

### 管理员视角
**故事5：查看所有租户财务概览**
- **作为**：PULSE 平台管理员
- **我希望**：看到所有租户的余额、充值、消费统计
- **以便**：掌握平台财务状况

---

## 2. 路由方案

基于 hash 路由，通过 `hashchange` 事件监听实现页面切换（无需整页刷新）。`document.title` 随页面自动同步。

| Hash 路由 | 页面 | 浏览器标题 | 说明 |
|-----------|------|-----------|------|
| `#` 或空 | LandingPage | PULSE Platform | 首页（模型与定价介绍） |
| _(无 hash)_ | Signup wizard | 开通专属 AI Agent · PULSE Platform | 注册向导（Step 1-4） |
| `#billing?token=xxx` | BillingPage | 余额与账单 · PULSE Platform | 账户中心（余额 + Tab 充值/消费） |
| `#recharge?token=xxx` | RechargePage | 充值 · PULSE Platform | 独立充值页（需 token 认证） |
| `#admin` | AdminBillingPage | 管理后台 · PULSE Platform | 管理员 Dashboard |

**路由机制**：`useHashPage()` hook 监听 `hashchange` → `setPage(pageFromHash())` → React 重渲染。各页面组件通过 `window.location.hash` 读取 `?token=` 参数。

---

## 3. 功能需求

### P0 - 核心功能

**FR-P0-001：余额查询 API**
- **端点**：`GET /api/billing/balance?tenantId={tenantId}`
- **响应**：`{ tenantId, balance: number, totalRecharged: number, totalConsumed: number, updatedAt, phone?: string }`
- **权限**：租户本人或管理员

**FR-P0-002：充值记录查询 API**
- **端点**：`GET /api/billing/recharges?tenantId={tenantId}&limit={n}&offset={m}`
- **响应**：`{ items: [{ orderId, amount, paidAt, status }], total }`
- **数据来源**：OrderStore 中 status='paid' 且 topUpAmount > 0 的订单

**FR-P0-003：消费记录存储与查询**
- **端点**：`GET /api/billing/consumptions?tenantId={tenantId}&limit={n}&offset={m}`
- **响应**：`{ items: [{ id, amount, description, createdAt }], total }`
- **存储**：`consumption-store.ts`（进程内 Map + JSON 文件持久化，路径由 `PLATFORM_DATA_DIR` 配置）
- **写入**：Relay 调用 `/internal/balance/deduct` 时同步记录消费明细

**FR-P0-004：租户 Billing 查看页面（BillingPage）**
- **路由**：`#billing?token=xxx`
- **内容**：
  - 当前余额（大数字显示）
  - Tab 切换：充值记录 / 消费明细
  - 分页器（Paginator 组件）
  - "立即充值" 按钮 → 跳转 `#recharge?token=xxx`

**FR-P0-005：管理员 Dashboard 页面**
- **路由**：`#admin`
- **内容**：
  - 所有租户余额汇总表
  - 今日/本周/本月充值统计
  - 今日/本周/本月消费统计
  - 低余额租户提醒

**FR-P0-006：独立充值页（RechargePage）**
- **路由**：`#recharge?token=xxx`
- **认证**：复用 BillingPage 的 token 验证逻辑
- **流程**：
  1. token 验证 → 获取 tenantId
  2. 加载 capabilities 获取 presets + billingNotice
  3. 加载当前余额（顶部小字提示）
  4. 选择充值金额（presets 网格 + 自定义输入，使用 `parseFloat` 支持小数）
  5. 点击"立即支付" → `POST /api/billing/recharge` 创建订单（标记 `source: 'recharge'`）
  6. 创建订单成功 → 调用 `/api/payment/create-order` 获取微信二维码
  7. 显示二维码 → 轮询 `/internal/orders/{orderId}` 检查支付状态
  8. 支付成功 → 显示成功提示 → 跳回 `#billing?token=xxx`
- **模型组限制**：充值页不涉及模型组选择。首页和注册向导的"国外模型"选项当前为禁用状态（`disabled: true`），显示"即将开放"标记。
- **页面结构**：

```
┌─────────────────────────────────────────────┐
│ 账户中心 · 充值                              │ ← Hero（简化标题）
├─────────────────────────────────────────────┤
│ 当前余额 ¥25.00                              │ ← 顶部余额提示
│ ┌───────────────────────────────────────┐   │
│ │ 选择充值金额                           │   │
│ │ ┌──────┐ ┌──────┐ ┌──────┐           │   │
│ │ │ ¥50  │ │ ¥100 │ │ ¥200 │           │   │ ← capabilities presets
│ │ │到账250│ │到账500│ │到账1000│          │   │
│ │ └──────┘ └──────┘ └──────┘           │   │
│ │ ┌──────┐ ┌──────┐                     │   │
│ │ │ ¥500 │ │ 自定义 │                     │   │
│ │ │到账2500│ │      │                     │   │
│ │ └──────┘ └──────┘                     │   │
│ │                                       │   │
│ │ [ 立即支付 ¥100 ]                      │   │ ← 创建订单
│ │                                       │   │
│ │ 💡 充值按 5 倍到账，每月最低充值 ¥100     │   │ ← billingNotice
│ └───────────────────────────────────────┘   │
│                                              │
│ ┌───────────────────────────────────────┐   │
│ │ ¥100                                   │   │ ← 支付区（订单创建后）
│ │ 预付费充值 · 按量扣减                    │   │
│ │ [ 微信支付二维码 ]                      │   │
│ │ 支付成功后页面将自动跳转…                │   │
│ └───────────────────────────────────────┘   │
│                                              │
│ ← 返回账户中心                               │
└─────────────────────────────────────────────┘
```

**FR-P0-007：充值订单 API**
- **端点**：`POST /api/billing/recharge`
- **请求**：`{ tenantId, topUpAmount }`
- **响应**：`{ orderId, status, phone, topUpAmount }` 或 `{ error }`
- **订单来源标记**：创建的订单 `source: 'recharge'`，用于 onPaid 回调区分充值和注册订单
- **onPaid 信用规则**：
  - **注册订单**（source 缺省或 `'signup'`）：灰度测试（`PLATFORM_GRAYSCALE_TEST=1`，¥0.01→¥10 基础额度）+ 信用倍率均生效
  - **充值订单**（`source: 'recharge'`）：仅信用倍率生效，跳过灰度测试
  - **信用倍率**：`PLATFORM_CREDIT_MULTIPLIER`（默认 5），用户充值 ¥X → 到账 ¥X × multiplier
- **注意**：已开通用户充值不触发 provisioning，`balanceStore.createOrTopUp()` 幂等叠加余额

**FR-P0-008：Dashboard Token 认证 API**
- **生成 token**：`POST /api/auth/dashboard-token` — `{ tenantId }` → `{ token }`（24h 有效期，HS256 签名）
- **验证 token**：`GET /api/auth/verify-token?token={token}` → `{ tenantId }` 或 401
- **用途**：飞书 Bot `/balance` 命令返回带 token 的 billing 链接；Step4Success 页面也生成 billing 链接
- **配置**：`PLATFORM_APP_API_KEY` 作为签名密钥

**FR-P0-009：Admin Shadow Account 消费追踪**
- **描述**：管理员（无充值记录）的余额扣减由 `BalanceStore.deduct()` 自动创建 shadow account（`balance=0, totalRecharged=0`）。Shadow account 不实际扣减余额，但记录 `totalConsumed` 用于追踪管理员的模型调用消费。`DeductResult` 新增 `shadow?: boolean` 标志，区分 shadow account（`totalRecharged === 0`）和真实租户余额不足。
- **来源**：管理员不需要扣费，但需要记录消费金额（bug-20260601 account_not_found）
- **示例**：eric 发消息 → `deduct("eric", 15)` → shadow account 自动创建 → `{ ok: false, error: 'insufficient_balance', balance: 0, shadow: true }`

**FR-P0-010：Insufficient Balance tracked 响应**
- **描述**：`POST /internal/balance/deduct` 对 shadow account 的 `insufficient_balance` 在响应中附加 `tracked: true`，表示消费已记录但无实际扣减。Relay 收到 `tracked: true` 时视为非阻塞（返回 `true`）。真实租户余额耗尽的 `insufficient_balance` 不含 `tracked`，Relay 按原策略返回 `false`。
- **来源**：FR-P0-009 联动
- **响应格式**：
  - Shadow: `{ ok: false, error: 'insufficient_balance', balance: 0, tracked: true }`
  - 真实租户余额耗尽: `{ ok: false, error: 'insufficient_balance', balance: 0 }`
  - 扣减成功: `{ ok: true, newBalance: xxx }`

---

## 4. 数据模型

### ConsumptionRecord（新增）
```typescript
interface ConsumptionRecord {
  id: string;           // 唯一ID
  tenantId: string;     // 租户ID
  amountFen: number;    // 消费金额（分）
  description: string;  // 描述（如"模型调用: claude-sonnet"）
  createdAt: number;    // 时间戳
  metadata?: {          // 扩展信息
    modelId?: string;
    tokensIn?: number;
    tokensOut?: number;
  };
}
```

### Order.source（新增，区分注册/充值）
```typescript
// order-store.ts Order 接口扩展
interface Order {
  // ...existing fields...
  /** v0.8.0: 订单来源。'recharge' = 充值页面创建（跳过灰度），缺省 = 注册流程 */
  source?: 'signup' | 'recharge';
}
```

### PresetItem（已有，来自 capabilities）
```typescript
interface PresetItem {
  amount: number;    // 充值金额（元）
  credit: number;    // 到账金额（元）
  bonus: number;     // 赠送金额（元）
}
```

---

## 5. 已实现组件清单

| 文件 | 状态 | 说明 |
|------|------|------|
| `web/src/components/BillingPage.tsx` | ✅ | 账户中心页，余额 + Tab（充值/消费）+ 分页 |
| `web/src/components/RechargePage.tsx` | ✅ | 独立充值页，选金额（parseFloat）→ 二维码 → 轮询 |
| `web/src/components/AdminBillingPage.tsx` | ✅ | 管理员 Dashboard |
| `web/src/components/BalanceInfo.tsx` | ✅ | 余额展示组件 |
| `web/src/components/Paginator.tsx` | ✅ | 通用分页组件 |
| `web/src/components/LandingPage.tsx` | ✅ | 首页，国外模型 `disabled: true` |
| `web/src/components/Step1PlanSelection.tsx` | ✅ | 注册向导 Step1，国外模型组 disabled |
| `web/src/App.tsx` | ✅ | 路由（hashchange 监听 + document.title 同步） |
| `web/src/index.css` | ✅ | 新增 `.step1-plan-disabled` / `.landing-pricing-disabled` 样式 |
| `src/order-store.ts` | ✅ | Order 新增 `source` 字段 |
| `src/http-server.ts` | ✅ | onPaid 按来源区分灰度/倍率逻辑 |

---

## 6. 验收标准

### AC-P0-001：余额查询
- 租户访问 `/api/billing/balance?tenantId=self` 返回正确余额
- 余额为 0 时显示 0，不存在时返回 404

### AC-P0-002：充值记录
- 只显示 status='paid' 的订单
- 按 paidAt 倒序排列
- 分页正常工作

### AC-P0-003：消费记录
- 每次扣费后自动记录
- 租户只能看到自己的消费
- 管理员可以看到所有

### AC-P0-004：页面可访问
- 租户能在飞书开通完成后访问 billing 页面
- 页面样式与 wizard 一致
- 移动端适配

### AC-P0-005：独立充值流程
- token 验证失败时显示"需要验证身份"提示
- 预设金额按钮可点击，显示到账金额
- 自定义金额支持小数（parseFloat），低于最低充值时显示错误
- 创建订单标记 `source: 'recharge'`
- 创建订单后加载微信支付二维码
- 支付成功后自动跳回 `#billing?token=xxx`
- 已开通用户充值不触发 provisioning（余额幂等叠加）
- 充值订单不触发灰度测试（¥0.01→¥10 仅限注册），信用倍率正常生效

### AC-P0-006：充值不影响 provisioning
- `POST /api/billing/recharge` 只创建订单（`source: 'recharge'`），不触发 provision 流程
- `onPaid` 中 `balanceStore.createOrTopUp()` 幂等叠加
- 不会重复开通或覆盖已有配置

### AC-P0-007：国外模型不可选
- 首页（LandingPage）国外模型卡片灰显 + "即将开放"标记，按钮禁用
- 注册向导（Step1）国外模型组卡片灰显 + "即将开放"标记，不可点击
- 默认模型组为 `domestic`

### AC-P0-008：Admin shadow account 消费追踪
- 管理员（如 eric）调用 deduct 时自动创建 shadow account（balance=0, totalRecharged=0）
- Shadow account 的 totalConsumed 累加消费金额，balance 不变（不负债）
- DeductResult 含 `shadow: true` 标志

### AC-P0-009：Shadow account tracked 响应
- Shadow account insufficient_balance 响应含 `tracked: true`
- 真实租户余额耗尽不含 `tracked`
- Relay 收到 `tracked: true` 返回 `true`（非阻塞）

---

## 7. 非功能需求

- **性能**：API 响应 < 100ms（进程内存储）
- **安全**：租户只能访问自己的数据；充值页需 token 认证
- **审计**：所有查询记录 audit log
- **移动端**：充值页适配移动端扫码场景
