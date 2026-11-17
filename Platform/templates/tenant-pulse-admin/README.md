---
name: "Tenant Pulse Admin Template|README"
version: 1.0.0
description: "租户 pulse 管理员 workspace 模板说明（非 pulse-space 拷贝）"
created: 2026-05-08T09:55:00+0800
modified: 2026-05-08T09:55:00+0800
---

# 租户 pulse 管理员模板（`tenant-pulse-admin`）

> SSOT：本目录是 **`FR-P0-015`** 指定的「会员租户初始 workspace 文件集」单一来源。
> Spec 锚点：`Platform/specs/features/platform-tenant-provisioning/{requirements.md,design.md}`（v0.4.0）。

## 1. 这是什么

这是 **PULSE 平台**为新开通的**会员租户**物化 `Tenants/{{tenantId}}/` 工作区时**递归拷贝**的模板源。
拷贝时机：**订单 `paid` 阶段 A**（`FR-P0-014`），由 `Platform/src/provision.ts` / `relay-merge.ts` 触发。

## 2. 这是 **不是什么**

- **不是** 仓库根 `pulse-space/` 的拷贝。`pulse-space/` 是 **平台管家 Pulse 自己**的 OA 工作区（含 SSOT 角色定义、租户清单、平台运维流程、跨租户记忆等），**不得**作为会员租户默认模板（信息泄露 + 职责混同）。
- **不是** `Bigbird/_archived/BADPulse/` 中残留的「shared templates / factory-director」概念的实现 —— 那部分仅作考古参考。

## 3. 文件清单（首版，5 文件）

| 文件 | 角色 | 必备 |
|------|------|------|
| `CLAUDE.md` | CC 入口视图（身份 + 路由决策树 + cwd） | ✅ |
| `IDENTITY.md` | 身份名片（Mission + Vibe + 上下游） | ✅ |
| `SOUL.md` | 行为边界（5 条「不能做」 + 失败可见） | ✅ |
| `TOOLS.md` | 租户能用的 CC 工具白名单（不下放平台工具语义） | ✅ |
| `MEMORY.md` | 长期记忆占位（仅本租户事实，不存秘密） | ✅ |

> 显式不放进首版：`AGENTS.md`、`HEARTBEAT.md`、`role-definitions.yaml`、`memo/`、`memory/`、`backups/`、`.cursor/rules/`。

## 4. 占位符（拷贝时由 Provisioner 替换）

使用 **`{{var}}`** 风格（Mustache），**单一字符串替换**即可，避免与 shell `${var}` 混淆。

| 占位符 | 示例 | 来源 |
|--------|------|------|
| `{{tenantId}}` | `member-0001` | `tenant-id-allocator` |
| `{{agentId}}` | `member-0001-pulse` | `FR-P0-010`：`{tenantId}-pulse` |
| `{{agentName}}` | `Eric的pulse管理员` | `FR-P0-010`：`{displayName}的pulse管理员` |
| `{{displayName}}` | `Eric` | Step 1 用户输入 |
| `{{provisionedAtIso}}` | `2026-05-08T10:00:00+0800` | `markPaid` 时的服务器时间 |

> Provisioner 实现要点（P0-014）：
> - **递归拷贝**模板目录到 `PLATFORM_TENANTS_ROOT/{tenantId}/`；
> - 对每个文本文件做 `{{var}}` 替换；
> - **目标已存在**的非占位文件（用户可能已编辑）→ **跳过不覆盖**；
> - **README.md 本身不拷贝**到租户目录（仅在模板源内说明用）。

## 5. 边界：模板里**不能**写什么

- 不写平台运维入口（systemctl / launchd / docker / `relay.json` / 白名单）。
- 不写跨租户能力或对其它租户的引用。
- 不写任何密钥占位（飞书 secret / Anthropic token 等）。
- 不写「平台管家 Pulse」的口吻或职责描述（只在「上抛对象」处提到）。

## 6. 后续演进

| 演进项 | 触发条件 |
|--------|----------|
| 增加 `.cursor/rules/` | 仅当某条规则被多租户复用且与平台 `Doc/rules/` 冲突时 |
| 增加 skills/ | 仅当出现真正可复用、且不带平台运维语义的租户级 skill |
| 把内联 `ensureTenantWorkspaceDir` 退役 | P0-014 落地、回归绿后整列删除 `relay-merge.ts` 中的内联生成块 |
