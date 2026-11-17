---
feature_name: "platform-tenant-provisioning"
version: "0.5.0"
created: "2026-05-01T12:00:00+08:00"
modified: "2026-05-10T20:30:00+08:00"
changelog:
  - "v0.5.0: P2-001～005 前端 UI 增强（组件化 + 进度持久化 + 表单验证 + 移动端 + 文本清理）"
  - "v0.4.2: P0-014/015/016 全部落地——`OrderStore.onPaid` + `tenant-template.ts` + `tenant-materialize.ts` + http-server eager allocator 接线；新增 22 + 3 单测；Platform 21/127、Relay 30/512 全过"
  - "v0.4.1: P0-015 模板目录已落地（`Platform/templates/tenant-pulse-admin/`，5 文件 + README）"
  - "v0.4.0: 150-spec-sync — M2 任务 P0-009～013 标完成；新增 P0-014/015（paid 模板物化 + tenantId 时机）"
upstream_dependency: ["requirements.md", "design.md"]
tags: [tasks, l2-spec, platform, provisioning]
spec_root: "Platform/specs"
---

# platform-tenant-provisioning — 任务拆分

## P0 — M1：可测的「先付后绑」骨架（已完成）

| ID | 任务 | 状态 | 说明 |
|----|------|------|------|
| P0-001 | 创建 `Platform` package + 最小入口（health / ping） | [x] | `src/http-server.ts`、`src/index.ts` |
| P0-002 | **Mock 支付**：内部接口将订单标为 `paid`（仅 dev/staging） | [x] | `PLATFORM_ALLOW_MOCK_PAY=1`；`NODE_ENV=production` 恒拒 |
| P0-003 | **白名单** 校验模块 + fixture | [x] | `phone-allowlist.ts` |
| P0-004 | **relay.json merge** + 文件锁 | [x] | `file-lock.ts` + `mergeFeishuTenantIntoRelayJson` |
| P0-005 | **Provision 编排**：`paid` → Tenants 路径 + merge bindings | [x] | `provision.ts`；`POST /internal/provision`（注：v0.4 Spec 拆为阶段 A/B，见 P0-014） |
| P0-006 | **Relay reload** 钩子 | [x] | `PLATFORM_RELAY_RELOAD_CMD`；及 Step 2 HTTP reload |
| P0-007 | **单元/集成测试** | [x] | `onboarding-http.test.ts`、`provision.test.ts` |
| P0-008 | **Web 用户向导**（同源托管） | [x] | Aurora；`GET /api/payment/create-order` |

## P1 — 生产支付与审计（已完成）

| ID | 任务 | 状态 | 说明 |
|----|------|------|------|
| P1-001 | 接入支付渠道（沙箱 → 生产） | [x] | `payment-provider.ts` 契约 |
| P1-002 | 审计日志最小集 | [x] | `PLATFORM_AUDIT_LOG_PATH` JSONL |

## P0 — M2：三步开通流（v0.3.0 · L3 已落地，150 同步 2026-05-08）

| ID | 任务 | 状态 | 说明 |
|----|------|------|------|
| P0-009 | **OrderStore 扩展**：`displayName`；`tenantId`；`tenant-id-allocator` | [x] | `order-store.ts`、`tenant-id-allocator.ts` |
| P0-010 | **Feishu 凭证校验**：`tenant_access_token/internal` | [x] | `feishu-credentials.ts` |
| P0-011 | **HTTP**：`/internal/orders`、`/internal/provision`、`/internal/pair`、`/internal/relay/reload` | [x] | `http-server.ts`、`provision.ts`、`relay-pairing-client.ts` |
| P0-012 | **Web Stepper UI** | [x] | `web/src/App.tsx` |
| P0-013 | **集成测试**：三步流、`tenant-workspace.test.ts` 等 | [x] | `three-step-flow.test.ts`、`onboarding-http.test.ts` |

## P0 — M3：付款即物化租户 workspace + 模板 SSOT（v0.4.2 · **已落地**）

| ID | 任务 | 状态 | 说明 |
|----|------|------|------|
| P0-014 | **`paid` 阶段 A**：`markPaid`/Webhook 路径内 **幂等**：分配 `tenantId` → `setTenantId`；从 **FR-P0-015** 模板目录递归拷贝到 `Tenants/{tenantId}/`（占位符 `{{var}}` 替换；不覆盖用户已改文件；排除 `README.md`） | [x] | 实现：`order-store.ts`（`onPaid` 钩子）+ `tenant-template.ts`（递归拷贝/替换）+ `tenant-materialize.ts`（编排）+ `http-server.ts`（eager `TenantIdAllocator` + 注册 `onPaid` 监听器）。覆盖 mock-pay / webhook / provider-already-paid 三条路径 |
| P0-015 | **模板目录**：`Platform/templates/tenant-pulse-admin/`（`CLAUDE.md` / `IDENTITY.md` / `SOUL.md` / `TOOLS.md` / `MEMORY.md` + `README.md`，占位符 `{{var}}`） | [x] | v0.4.1 已落地；与 `requirements` FR-P0-015 + `design` §1.13 一致 |
| P0-016 | **回归测试**：`paid` 后目录存在 + 5 文件齐全 + 占位符已替换 + Step 2 消费已有 `tenantId`；AC-P0-008 | [x] | `tests/tenant-template.test.ts`（11 cases）+ `tests/tenant-materialize.test.ts`（11 cases，含 `onPaid` 钩子）+ `tests/stage-a-http.test.ts`（3 cases，HTTP 端到端 + 隔离开关） |

---

## 半自动验收（对齐 AC-P0-004～008）

1. `npm run build`（含 **build:web**）使 `dist/web` 存在；部署或本地 `npm start` 后浏览器打开 **`http://<host>:<port>/`**。
2. 准备空 `relay.json`（仅 `users:[]` `bindings:[]`）+ env：`PLATFORM_RELAY_CONFIG_PATH`、`PLATFORM_TENANTS_ROOT`、`PLATFORM_PHONE_ALLOWLIST_INLINE`、`PLATFORM_RELAY_HTTP_URL`（Relay loopback HTTP，例 `http://127.0.0.1:8788`）。
3. 启动 Relay（`RELAY_HTTP_PORT=8788`）+ Platform。
4. **Step 1**：白名单内 phone + displayName → 支付至 `paid` → **（v0.4）** 验证 `Tenants/{tenantId}/` 已按模板物化。
5. **Step 2**：飞书凭证 → `relay.json` pending → reload。
6. **Step 3**：配对码 → binding `active`。
7. 飞书对话验证身份为 **租户** pulse 管理员（**AC-P0-008**）。

## 145 自动化 UAT

- **报告**：[`uat/automated-uat-report.md`](uat/automated-uat-report.md)（v0.4.0 起增加 AC-P0-008 追踪）

## P2 — 前端 UI 增强（v0.5.0 · 150-spec-sync 2026-05-10）

| ID | 任务 | 状态 | 说明 |
|----|------|------|------|
| P2-001 | **前端组件化**：`App.tsx` 693 行 → 协调器 ~270 行 + 10 组件 + 3 共享模块 | [x] | `web/src/components/`（NavBar, HeroHeader, ProgressBar, Step1~4, StatusMessage, LoadingBlock, Footer）+ `types.ts` + `api.ts` |
| P2-002 | **内部文本清理**：移除服务端配置 badges、内部订单号、环境变量名等技术文本 | [x] | Step1 移除订单号/支付订单号；Step2 简化 API 说明；Step3 简化重试文案；Footer 精简 |
| P2-003 | **表单验证**：手机号 11 位、用户名非空、配对码字符计数、红色内联提示 | [x] | `index.css` 新增 `.field-error` / `.input-error` / `.char-count` |
| P2-004 | **进度持久化**：`useWizardProgress` hook + localStorage + 24h 过期 + 订单验证 | [x] | `web/src/useWizardProgress.ts`；绝不存储 `feishuAppSecret` |
| P2-005 | **移动端优化**：16px 输入框、全宽按钮、QR 缩小、safe-area、touch-action | [x] | `index.css` `@media (max-width: 767px)` 扩展 |
| P2-006 | **前端测试基础设施** | [ ] | vitest + @testing-library/react；覆盖 useWizardProgress、表单验证、组件渲染 |
| P2-007 | **前端 UAT** | [ ] | 走完整向导流程验证；更新 UAT 报告 |

---

*Spec：`Platform/specs/features/platform-tenant-provisioning/` · 代码：`Platform/`*
