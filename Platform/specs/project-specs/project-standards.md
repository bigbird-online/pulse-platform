---
product: "Platform"
layer: "L1"
version: "0.2.0"
created: "2026-05-01T13:00:00+08:00"
modified: "2026-05-13T00:30:00+08:00"
changelog:
  - "v0.2.0: 150-spec-sync — 目录结构补订阅相关文件；密钥约束补订阅 API key；测试数更新"
  - "v0.1.1: 目录结构补 vitest/tests"
  - "v0.1.0: 初版，继承 Relay 工程基线"
inherits: "Relay/specs/project-specs/project-standards.md"
tags: [l1, platform, standards]
---

# Platform — 项目规范（L1）

## 基线

**默认对齐** **`Relay/specs/project-specs/project-standards.md`**（Node、TypeScript strict、npm、测试与目录纪律），除非本文 **增量约束** 另有规定。

## Platform 增量约束

| 项 | 约束 |
|----|------|
| 密钥 | 支付、订阅与飞书 `app_secret` **禁止** 提交到 git；本地示例仅用占位符；`PLATFORM_APP_API_KEY` 用于 App Platform 支付 + 订阅 API 认证 |
| 日志 | 订单号、手机号日志需 **脱敏**（具体格式在 L2 细化） |
| 配置写 | 写 `relay.json` **仅经** Provision 编排单一路径；禁止多进程无序并发写 |
| Spec | L2 在 `Platform/specs/features/`；引用 Relay 规格用 **仓库根相对路径** |

## 目录结构（目标）

```
Platform/
├── README.md
├── specs/
│   ├── README.md
│   ├── project-specs/       # L1（本目录）
│   └── features/            # L2
├── templates/               # 租户 Agent 模板
│   └── tenant-pulse-admin/
├── package.json
├── tsconfig.json
├── vitest.config.ts
├── tests/                   # 21 test files / 128 tests
└── src/
    ├── index.ts
    ├── http-server.ts
    ├── order-store.ts
    ├── subscription-client.ts    # v0.6.0: App Platform 订阅 API 客户端
    ├── payment-create-order.ts
    ├── app-platform-payment.ts
    ├── payment-provider.ts
    ├── mock-pay-policy.ts
    ├── provision.ts
    ├── tenant-materialize.ts
    ├── tenant-template.ts
    └── web/                      # React 前端（Vite）
        └── src/
            ├── App.tsx
            ├── types.ts
            ├── components/
            │   ├── Step1PlanSelection.tsx  # v0.6.0: 订阅套餐选择（替代 Step1PhonePayment）
            │   ├── Step2FeishuCredentials.tsx
            │   ├── Step3PairingCode.tsx
            │   ├── Step4Success.tsx
            │   └── SubscriptionStatus.tsx  # v0.6.0: 订阅状态卡片
            └── index.css
```
