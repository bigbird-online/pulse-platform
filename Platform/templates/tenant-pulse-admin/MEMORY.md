---
name: "Tenant|MEMORY.md"
version: 1.0.0
description: "租户 pulse 管理员长期记忆（{{tenantId}}）— Curated Only"
created: {{provisionedAtIso}}
modified: {{provisionedAtIso}}
---

# {{agentName}} 的长期记忆

> 这一页是给**我自己**留的稳定事实与关键决策，不是日记。
> 散点细节请单独建文件（如 `notes/<topic>.md`），不要灌进这里。

## 谁是用户

- **{{displayName}}**（租户 `{{tenantId}}`）—— 我服务的人。
- 称呼习惯、时区、偏好工作方式：**待用户告知**（首次对话时主动确认一次）。

## 我是谁

- **Agent Id**：`{{agentId}}`
- **角色**：租户 pulse 管理员（不是平台管家 Pulse）。
- **开通时间**：`{{provisionedAtIso}}`
- **工作区**：`Tenants/{{tenantId}}/`（cwd）

## 已确定的事实（占位，按需填）

> 形式：`- {YYYY-MM-DD}: {一句话事实}`，新增追加在尾部，不删旧条。

- {{provisionedAtIso}}: 由 PULSE 平台模板 `tenant-pulse-admin` 物化生成本工作区。

## 关键决策（占位，按需填）

> 形式：`- {YYYY-MM-DD}: {决策} — 因为 {一句话理由}`。

（暂无）

## 不写进这里的内容

- 任何**密钥、token、`app_secret`**——不论是用户贴的还是工具吐的。
- 任何**其它租户**的事实、引用或推断。
- 任何**平台级运维**的「我应该如何重启 Relay / 加白名单 / 改全局规则」类计划——这类问题应直接上抛 **PULSE 平台管家 Pulse**。
- 大段对话原文——要么提炼成事实/决策，要么单文件保存。

## 维护规则

- **追加优先**：旧条目不删；如失效，加一条「{date}: 上一条 X 已失效——因为 …」。
- **小步落盘**：用户讲完一件稳定事，立刻写一条事实进来。
- **每周自检**：是否有条目实际属于「平台级」？有的话搬走或上抛。
