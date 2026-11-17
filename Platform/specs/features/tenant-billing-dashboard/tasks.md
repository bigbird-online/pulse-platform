---
feature_name: "tenant-billing-dashboard"
version: "0.4.0"
created: "2026-05-29T19:00:00+08:00"
modified: "2026-06-01T22:30:00+08:00"
upstream_dependency: ["requirements.md"]
tags: [tasks, l2-spec, platform, billing, recharge]
spec_root: "Platform/specs"
changelog:
  - version: "0.4.0"
    date: "2026-06-01"
    changes: "150-spec-sync：P0-009 shadow account 消费追踪；P0-010 tracked 响应"
  - version: "0.3.0"
    date: "2026-05-31"
    changes: "150-spec-sync：Order.source 充值来源区分；灰度测试隔离；国外模型禁用；hashchange 路由；document.title 同步"
  - version: "0.2.0"
    date: "2026-05-31"
    changes: "新增充值页任务，标注已实现组件状态"
  - version: "0.1.0"
    date: "2026-05-29"
    changes: "初始版本"
---

# tenant-billing-dashboard — 任务拆分

## P0 — 后端 API 与存储

| ID | 任务 | 状态 | 说明 |
|----|------|------|------|
| P0-001 | 创建 `src/consumption-store.ts` + 测试 | ✅ | 消费记录存储，进程内 Map |
| P0-002 | 修改 `/internal/balance/deduct`，记录消费明细 | ✅ | deduct 时写入 consumption-store |
| P0-003 | 新增 `GET /api/billing/balance` | ✅ | 查询余额、总充值、总消费 |
| P0-004 | 新增 `GET /api/billing/recharges` | ✅ | 从 OrderStore 查询充值记录 |
| P0-005 | 新增 `GET /api/billing/consumptions` | ✅ | 从 consumption-store 查询 |
| P0-006 | 新增 `GET /api/admin/billing/summary` | ✅ | 管理员财务概览 |
| P0-007 | 新增 `POST /api/billing/recharge` | ✅ | 独立充值订单（标记 `source: 'recharge'`） |
| P0-008 | Order 新增 `source` 字段 + onPaid 按来源区分灰度 | ✅ | 充值订单跳过灰度测试，保留信用倍率 |
| P0-009 | BalanceStore shadow account（自动创建 + DeductResult.shadow） | ✅ | 管理员消费追踪，不扣余额 |
| P0-010 | handleBalanceDeduct tracked 响应 | ✅ | shadow account insufficient_balance 含 tracked: true |

## P0 — 前端页面

| ID | 任务 | 状态 | 说明 |
|----|------|------|------|
| P0-009 | 创建 `web/src/components/BillingPage.tsx` | ✅ | 账户中心页（余额 + Tab + 分页） |
| P0-010 | 创建 `web/src/components/AdminBillingPage.tsx` | ✅ | 管理员 Dashboard |
| P0-011 | 创建 `web/src/components/RechargePage.tsx` | ✅ | 独立充值页（parseFloat + 二维码 + 轮询） |
| P0-012 | 创建 `web/src/components/Paginator.tsx` | ✅ | 通用分页组件 |
| P0-013 | 更新 `web/src/App.tsx`，添加路由 | ✅ | hashchange 监听 + document.title 同步 |
| P0-014 | 前端 API 调用封装 | ✅ | 各组件内联 `api()` 调用，无需独立文件 |
| P0-015 | `index.css` 充值页 + disabled 模型样式 | ✅ | Tab + 分页器 + `.step1-plan-disabled` / `.landing-pricing-disabled` |
| P0-016 | 国外模型禁用（LandingPage + Step1） | ✅ | `disabled: true` + "即将开放"标记 |

## P1 — 测试与 UAT

| ID | 任务 | 状态 | 说明 |
|----|------|------|------|
| P1-001 | 后端 API 测试 | ✅ | ConsumptionStore 8 个 vitest 用例 |
| P1-002 | 前端组件测试 | [ ] | 如有时间 |
| P1-003 | UAT | [ ] | 走完整充值流程验证 |

---

*Spec：`Platform/specs/features/tenant-billing-dashboard/`*
