---
feature_name: "tenant-onboarding-feishu"
version: "0.2.2"
created: "2026-05-02T15:30:00+08:00"
modified: "2026-05-01T12:50:00+08:00"
changelog:
  - "v0.2.2: platform-tenant-provisioning Spec 迁至 Platform/specs/"
  - "v0.2.1: 关联 platform-tenant-provisioning + Platform 路径"
  - "v0.2.0: 商业化主路径—手机白名单、无邀请码、先支付后 relay/binding；配对+/approve 降为运营兜底"
  - "v0.1.2: FR-S1-004 暂缓至语道 Harness；FR-S1-003 对齐 /usage 已实现"
  - "v0.1.1: 关联文档增加 UAT 归档路径（T0-004）"
time_basis: "北京时间（Asia/Shanghai，UTC+8）"
upstream_dependency:
  - "specs/project-specs/project-vision.md"
  - "specs/project-specs/project-architecture.md"
  - "specs/features/feishu-channel/requirements.md"
  - "specs/features/logical-gateway/design.md"
tags: [requirements, l2-spec, feishu, tenant, onboarding]
---

# tenant-onboarding-feishu — 需求（MVS）

> **最小可执行规格**：支持「会员成为租户」在 **不翻墙** 环境下的 **飞书** 入口；与 `specs/pulse-product-strategy.md` 战略约束一致。
> **不替代** `feishu-channel` 全文；本文件描述 **跨 feature 的交付切片** 与 **验收**。

## 1. 目标

- 在 **仅依赖飞书（及既有 CC/Codex 算力）** 的前提下，完成**会员 → 租户**的接入闭环：**可对话、可运维审计**；绑定以 **`bindings[]` / Gateway** 为 SSOT。
- **本期不以 Telegram 为交付渠道**；TG 相关需求不作为本 MVS 验收项（与 `specs/pulse-product-strategy.md` 一致）。

## 2. 边界（非目标）

- **不做** **匿名全网**自助注册（与 `project-vision` 一致）：准入依赖 **手机号白名单**（及注册流内 **支付成功**），见 **FR-S1-001**。
- **不使用邀请码**；席位 / 控量通过 **白名单维护**（及订单/库存策略由平台层定义），不采用「一码一人」模型。
- **不兼容** **飞书登录态无法关联到已验证手机号** 的路径：全链路必须以 **已验证手机号** 作为会员身份锚点（不尝试支持「仅有飞书 open_id、无手机号」的注册开通模型）。
- **开通顺序（硬性）**：**先支付成功**，再 **写入** 租户空间与 **`relay.json` / `bindings[]`**（含自动绑定）；支付未成功 **不得** 落持久化 Relay 配置（见 **FR-S1-001**）。
- **飞书应用模型**：**一户一飞书自建应用**（独立 `app_id`/密钥）；与 Relay 配置的对应关系在平台开通流水线中保证 1:1。
- **不做** Web UI channel、不做企业级多租户 RBAC（见 vision / roadmap Out of Scope）。
- **不**在本 MVS 内要求实现 **TG explicit binding**、**多 TG chat_id**。

## 3. 用户故事

| ID | 故事 | 优先级 |
|----|------|--------|
| US-S1-01 | 作为 **白名单内用户**，我完成 **手机号验证** 与 **支付** 后，系统应 **自动** 完成开户并写入 **Relay/`bindings[]`**，使我可在 **本租户飞书 Bot** 内直接对话 | P0 |
| US-S1-02 | 作为 **运营/管理员**，在例外场景下我仍可通过 **配对码 + `/approve`** 协助绑定（详见 **FR-S1-005**） | P1 |
| US-S1-03 | 作为 **已绑定租户**，我希望 **首条普通消息** 即能收到 **AI 回复**（卡片），并能使用 **`/status`** 等已有命令 | P0 |

## 4. 功能需求

**FR-S1-001：商业化默认路径 — 手机号白名单 + 先支付后开通与 binding**

- **描述**：
  - 准入：**手机号在白名单内** + 完成 **身份校验（按平台实现，至少绑定已验证手机号）**。
  - **注册流内完成支付**；**仅当支付成功（订单终态已确认）后**，平台才允许：**创建/更新租户工作区**、**生成或挂载该租户一飞书应用凭证**、**写入 `relay.json`（含 `users`/`agents` 等与 Feishu Agent 对应项）** 与 **`bindings[]` active 记录**，并完成 Relay 侧所需的 **reload/热加载**，实现 **自动 binding**。
  - **不使用邀请码**。
  - **不兼容** 无 **已验证手机号** 可与租户身份关联的飞书客户端路径（不要求为人为飞书登录态单独开一条无手机号的注册线）。
- **验收**：存在可重复的 **集成/准生产** 证据：支付失败或未支付 → **无** 新增有效 `bindings[]` 指向该租户；支付成功 → **有**，且飞书发消息可 `resolveInboundRoute` 至正确 principal/agent。

**FR-S1-002：`relay.json` 与 Gateway 为绑定 SSOT**

- **描述**：激活绑定以 **`bindings[]` active record** 为准；legacy `user.chatId` **不是**本期会员路径依赖（TG 封存）。
- **验收**：新租户文档与示例配置优先展示 **`bindings[]` + Feishu Agent**，避免误导依赖 TG。

**FR-S1-003：可观测最小集**

- **描述**：租户侧至少可感知 **会话/进程健康**（`/status`）；费用若需透明，则使用 **`/usage`**（见 `bot-commands` **FR-P2-003** 与 `metering` 月度汇总），**不写入本 MVS P0 硬门槛**。

**FR-S1-004：飞书 `chat_id` 级绑定（暂缓立项）**

- **描述**：历史上为 **条件立项**（仅 open_id 不足、必须用群 `chat_id` 区分会话时再开切片）。
- **决议（2026-05-02）**：在 **「语道」完成 Harness** 之前 **不立项**、不展开设计与开发；与 `project-roadmap` v1.1「飞书 chat_id binding」表项一致，标注为冻结至上述里程碑之后复盘。
- **验收**：冻结期内本条 **不适用**；解冻须单独产品决议 + L2 切片。

**FR-S1-005：运营兜底 — 配对码 + 管理员 `/approve`**

- **描述**：在非自动化或历史迁移场景，仍可采用 `feishu-channel` **FR-P0-009 / FR-P0-010** 与 `logical-gateway` explicit `bindings[]` 写路径；**不是**付费会员默认路径。
- **验收**：与 `feishu-channel` 相应 AC 一致；与 **FR-S1-001** 无顺序冲突（兜底不得绕过「先付后绑」的付费租户策略）。

## 5. 验收标准

**AC-S1-001：端到端 Onboarding（运营兜底 — 配对 + `/approve`）**

- **条件**：全新飞书用户（未在 `bindings[]` / 有效 openId 映射内），走配对兜底。
- **预期**：收到配对指引 → 管理员 `/approve` 成功 → 同用户再发文本 → **≤30s** 内收到 **Interactive Card** 回复；全程无 TG。

**AC-S1-002：战略一致性**

- **条件**：回顾本 MVS 与 `specs/pulse-product-strategy.md`。
- **预期**：正文 **不得**将 TG 功能列为 P0/P1 必交付项。

**AC-S1-003：端到端 Onboarding（商业化主路径 — 先付后绑）**

- **条件**：手机号 **已验证** 且在 **白名单**；**未完成支付**。
- **预期**：**不得**出现指向该会员租户的有效 `bindings[]`（或等价判定：Relay 不得对其按正式租户解析为已开通）。

- **条件**：同用户 **支付成功**（订单终态确认）。
- **预期**：在约定时间内完成 **relay/binding 持久化**；用户在本租户 **飞书 Bot** 发文本 → **≤30s** 内收到 **Interactive Card** 回复；全程身份可追溯到 **手机号**，且无邀请码参与。

## 6. 关联文档

- **平台开通（支付 / 写 relay / binding）**：`Platform/specs/features/platform-tenant-provisioning/requirements.md`（实现：`Platform/`）
- `features/feishu-channel/requirements.md`、`tasks.md`、`design.md`
- `features/logical-gateway/design.md`
- 任务拆解：`features/tenant-onboarding-feishu/tasks.md`
- **UAT 归档（历史：配对路径 / T0-004）**：`features/tenant-onboarding-feishu/uat/AC-S1-001-record.md`
