---
product: "Platform"
layer: "L1"
version: "0.1.0"
created: "2026-05-01T13:00:00+08:00"
modified: "2026-05-01T13:00:00+08:00"
changelog:
  - "v0.1.0: 初版 Platform L1，与 Relay L1 分栈"
upstream:
  - "Relay/specs/project-specs/project-vision.md"
  - "Relay/specs/features/tenant-onboarding-feishu/requirements.md"
tags: [l1, platform, vision]
---

# Platform — 项目愿景（L1）

## 定位

**Platform** 是 PULSE 的 **会员准入与租户开通平台**：在 **手机号白名单 + 先支付成功** 前提下，完成 **自动开户**、**写入 `relay.json` / Gateway `bindings[]`**、并驱动 **Relay** 加载配置，使租户通过 **本租户飞书应用（一户一应用）** 使用 Relay 能力。

**不在此产品内**：消息网关、飞书 WS、CC/Codex 进程池、Bot 对话逻辑（归属 **Relay**）。

## 与 Relay 的关系

| Platform | Relay |
|----------|-------|
| 白名单、订单、支付回调、provision 编排 | 读配置、路由、通道、会话 |
| 写 `Tenants/`、`relay.json`（约定格式） | `resolveInboundRoute`、飞书 handler |
| 触发 reload（契约） | 热加载或进程级重启 |

Relay 的愿景与成功指标见 **`Relay/specs/project-specs/project-vision.md`**；其中 **商业准入澄清**（非匿名注册、先付后绑等）与本 L1 一致，详见 **`tenant-onboarding-feishu`** L2。

## 不做什么

- 不做 **Relay 功能规格** 的重复定义（单点 SSOT 在 `Relay/specs/`）。
- 不做 **匿名全网**自助注册；不使用 **邀请码**（见 onboarding L2）。
- 第一期不承诺 **飞书开放平台应用全自动创建**（允许人工录入凭证，见 roadmap）。

## 成功指标（草案）

| 指标 | 说明 |
|------|------|
| 开通确定性 | 支付成功 → 约定 SLA 内 `bindings[]` 可用、飞书可对话 |
| 安全 | 无支付不写生产 binding；回调幂等；密钥不落库明文（策略见 architecture） |
| Spec 分栈 | Platform L1/L2 在 `Platform/specs/`，Relay 不承载 Platform L1 |
