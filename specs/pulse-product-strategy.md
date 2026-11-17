---
name: PULSE Product Strategy
version: 1.0.0
modified: 2026-08-23
authority: stable product and delivery constraints
---

# PULSE Product Strategy

## Stable constraints

- 首要产品目标是让会员成为租户，完成 onboarding 与可用环境交付。
- 租户默认使用不翻墙、境内可达的入口；当前会员 onboarding 主路线是飞书。
- Telegram 兼容能力可以维护，但不是当前会员 onboarding 的默认路径或 P0/P1 验收项；解冻须单独产品决议。
- 激活租户绑定以 Relay `bindings[]` / Gateway 的 active record 为真源，不能以 workspace 状态文件代替。

## Fact boundaries

本文件只保存稳定产品约束，不保存当前 milestone、已完成工程、open task 或 next action。模块验收回相关 Requirements/Design/Tasks，动态工作回 GitHub task 与 canonical WI，当前 runtime 状态回可重复 evidence。

旧根 `progress.md` 的 2026-05 工程快照已退役；历史可从 Git 追溯，不作为新 session 启动输入。
