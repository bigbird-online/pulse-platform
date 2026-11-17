---
product: "Platform"
layer: "L1"
version: "0.2.0"
created: "2026-05-01T13:00:00+08:00"
modified: "2026-05-31T09:40:00+08:00"
changelog:
  - "v0.2.0: 150-spec-sync — 补充 billing dashboard 里程碑（M1.5）"
  - "v0.1.0: 初版里程碑"
related_relay: "Relay/specs/project-specs/project-roadmap.md"
tags: [l1, platform, roadmap]
---

# Platform — 项目路线图（L1）

> **说明**：Relay 产品路线图见 **`Relay/specs/project-specs/project-roadmap.md`**。本文仅列 **Platform 自有里程碑**。

## 里程碑（草案）

| 阶段 | 目标 | 规格/证据 |
|------|------|-----------|
| **M1** | Mock 支付 + 白名单 + **先付后写** `relay.json` / `bindings[]` + reload 约定（可手工兜底） | `platform-tenant-provisioning` tasks P0-001～007 |
| **M1.5** | 租户账户中心 + 独立充值 + 管理员 Dashboard + 充值/注册来源区分 | `tenant-billing-dashboard` tasks P0-001～016 |
| **M2** | 生产支付渠道（沙箱→生产）、回调幂等与对账 | L2 扩展 + 安全评审 |
| **M3** | 运营最小集（订单查询、重试、吊销） | 新建 L2 可选 |
| **defer** | 飞书应用 **API 自动创建**、全自助企业入驻、**国外模型节点** | 单独决议 |

## 与 member onboarding 的衔接

- 会员侧验收：**`Relay/specs/features/tenant-onboarding-feishu/requirements.md`**（**AC-S1-003** 等）。
- Platform 交付必须满足 **FR-S1-001**（先支付成功再持久化 binding）。
