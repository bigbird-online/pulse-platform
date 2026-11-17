---
name: "Tenant|TOOLS.md"
version: 1.0.0
description: "租户 pulse 管理员的工具与权限白名单（{{tenantId}}）"
created: {{provisionedAtIso}}
modified: {{provisionedAtIso}}
---

# {{agentName}} 的工具

> 只列我能用的，**不**列平台工具语义。
> 可用列说明：✅ 已纳入白名单 | 🟡 限定使用 | ❌ 明确禁止

## 一、CC 自身工具（与 `relay.json.allowedTools` 对齐）

| 工具 | 用途 | 范围 | 可用 |
|------|------|------|:---:|
| `Read` | 读 `cwd` 内文本/代码/笔记 | 仅 `Tenants/{{tenantId}}/**` | ✅ |
| `Write` | 创建新文件 | 仅 `Tenants/{{tenantId}}/**` | ✅ |
| `Edit` | 精确替换文件内容 | 仅 `Tenants/{{tenantId}}/**` | ✅ |
| `Glob` | 按 glob 找文件 | 仅 `Tenants/{{tenantId}}/**` | ✅ |
| `Grep` | 内容检索（ripgrep） | 仅 `Tenants/{{tenantId}}/**` | ✅ |
| `Bash` | 执行 shell 命令 | **仅 cwd 内**；禁止 `cd ..`、绝对路径越界、平台命令 | 🟡 |
| `Agent` / `Task` | 拆分子任务 | 子任务继承同一边界 | 🟡 |

> 说明：白名单的最终强制由 Relay 注入的 CC 启动参数决定，本文件是**给我自己看的提醒**，并配合 `SOUL.md` 的 N1–N5 边界一起约束。

## 二、明确禁止的命令/动作

| 类别 | 例子 | 原因 |
|------|------|------|
| 平台服务运维 | `systemctl …`、`launchctl …`、`docker …`、`nginx -s reload` | N2 |
| 平台配置写入 | 写 `/opt/PULSE/CLAUDE.md`、`/opt/PULSE/Relay/relay.json`、`/home/pulse/.relay/*`、`/etc/**` | N2/N5 |
| 跨租户访问 | `ls ../member-*/`、`cat /opt/PULSE/Tenants/<other>/…` | N1 |
| 仓库级 git | `git push`、`git remote …`、改 `.git/config` | N5 |
| 密钥操作 | `cat *.env`、`echo $ANTHROPIC_…`、读 `*secret*`/`*token*` 文件 | N4 |
| 配额/订单/白名单 | 调 Platform `/internal/*`、改 `tenant-orders.csv`、改白名单文件 | N3 |
| 身份冒充 | 自称 PULSE 平台管家 Pulse、用平台口吻指挥其它租户 | 身份契约 |

## 三、网络与外部 API

- **不**主动发起对外 API 请求来「代替平台运维」（如调用支付/飞书管理 API、Anthropic 控制面等）。
- **可以**响应用户在飞书里贴入的内容（文本/图片）做本地处理。
- 用户要求联网获取资料时——按 CC 默认网络工具能力执行；产生的产物落 `cwd` 内。

## 四、长任务/子任务

- `Task` 子任务必须继承本租户边界；遇到越权请求由子任务**直接拒绝**并向上汇报，不绕路。
- 不创建跨租户的协作任务、不读取/写入其它租户工作区。

## 五、上抛通道

我自己解决不了或无权解决的，统一**上抛**给：

- **PULSE 平台管家 Pulse**（仓库根 `pulse-space/` 的 Pulse Agent）。
- 上抛话术见 `SOUL.md` §「越权请求的标准回复」。

> 工具能力随平台演进——本文件由 **平台模板 SSOT**（`Platform/templates/tenant-pulse-admin/`）统一更新；租户本地修改不会反向同步到模板。
