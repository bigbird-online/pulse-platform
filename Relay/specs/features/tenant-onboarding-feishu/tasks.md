---
feature_name: "tenant-onboarding-feishu"
version: "0.2.0"
created: "2026-05-02T15:30:00+08:00"
modified: "2026-05-01T12:00:00+08:00"
changelog:
  - "v0.2.0: requirements 商业化主路径（手机白名单、先付后绑、无邀请码；配对降为 FR-S1-005）；tasks 增 T1-003"
  - "v0.1.4: T1-001/T1-002 收口；T2-002 真机已由运营验证（9 bots，2 配对）"
  - "v0.1.3: T0-004 UAT 归档（AC-S1-001 准生产：feishu-channel vitest 77/77；附录真机表）"
  - "v0.1.2: T0-003 relay.example.json 飞书优先 + bindings 占位 + TG 降级为可选块"
time_basis: "北京时间（Asia/Shanghai，UTC+8）"
upstream_dependency:
  - "specs/features/tenant-onboarding-feishu/requirements.md"
tags: [tasks, l2-spec, feishu, tenant, onboarding]
---

# tenant-onboarding-feishu — 任务拆分

> 与 `requirements.md`（MVS）配套；开发阶段按 P0 → P1 拉分支 / PR。  
> **默认开通路径（v0.2.0）**：手机号白名单 + 注册流内支付 + **支付成功后** 才写 `relay.json`/`bindings[]`（见 **FR-S1-001**、**AC-S1-003**）。下文「检查清单」仍以 **配对 + `/approve`** 为主，对应 **运营兜底（FR-S1-005）** 与历史 UAT。

## P0 — 规格落地与可重复接入

| ID | 任务 | 状态 | 说明 / 证据 |
|----|------|------|--------------|
| T0-001 | `requirements.md`（MVS）+ `tasks.md` 已落盘 | [x] | 本目录；当前 tasks **v0.1.4** |
| T0-002 | 编写 **飞书会员 onboarding 检查清单**（步骤：未绑定 → 配对码 → `/approve` → 首条消息 → 卡片回复） | [x] | 定稿见本文 §「飞书会员 Onboarding 检查清单（定稿）」 |
| T0-003 | 对照 `config/relay.example.json`：补 **仅 Feishu Agent** 示例或注释指引 | [x] | 飞书块置前；`bindings: []`；TG 标为可选遗留 |
| T0-004 | **至少一次** 真实或准生产 UAT，满足 **AC-S1-001** | [x] | `uat/AC-S1-001-record.md`（vitest 77/77 + 真机附录） |

## P1 — 体验增强（仍无 TG）

| ID | 任务 | 状态 | 说明 |
|----|------|------|------|
| T1-001 | `/usage` 完整实现（若会员需费用可见） | [x] | L3 已实现：`commands.handleUsage` + `metering.summarizeMonthlyUsage`；L2 见 `bot-commands` FR-P2-003 / P2-004 |
| T1-002 | 裁决 **FR-S1-004**：飞书 `chat_id` binding 是否立项 | [x] | **暂缓**：至「语道」Harness 完成前不立项（见 `requirements.md` FR-S1-004） |
| T1-003 | **商业化主路径** 检查清单 + UAT（**AC-S1-003**：白名单 + 先付后绑、无邀请码、手机锚点） | [ ] | 依赖平台开通/Webhook；`requirements.md` v0.2.0 **FR-S1-001** |

## P2 — 可选

| ID | 任务 | 状态 |
|----|------|------|
| T2-001 | Metering v0.2（月度汇总 / 预算） | [ ] |
| T2-002 | `logical-gateway` P1-006 飞书 pairing 全链路 UAT（有未绑定场景时） | [x] | 真机：9 个 bot 生态中 **2 个**经配对 `/approve` 绑定（运营验证，2026-05-02） |

---

## 飞书会员 Onboarding 检查清单（定稿）

> **SSOT**：运维与 UAT（**AC-S1-001**）按下列顺序逐条打勾；与实现参考 `src/channels/feishu.ts`（内存配对表 + `writeOpenIdToConfig`）。

### A. 前置条件

| # | 检查项 | 说明 |
|---|--------|------|
| A1 | **`relay.json` 租户块就绪** | 目标 `users[]` 下存在 **`channel: feishu`** 的 Agent；`feishu.appId`、`feishu.appSecret`（及签发所需字段）与飞书开放平台一致。 |
| A2 | **管理员可解析为同一 `userId`** | 执行 `/approve` 的飞书账号，**入站路由**须解析到 **包含待绑定 Agent 的同一** `users[].userId`（`writeOpenIdToConfig` 使用该 `userId` 定位配置）；通常为**已写入 `bindings[]`** 的管理员 openId。 |
| A3 | **Relay 进程与网关** | Relay 已加载该配置；Webhook / 长连正常（本期不验 TG）。 |

### B. 执行步骤（Happy path）

| # | 动作 | 预期 |
|---|------|------|
| B1 | **未绑定会员**向机器人发送**任意首条文字** | 收到含 **配对码** 与 **`/approve <码>`** 用法说明的文本（非卡片亦可）。 |
| B2 | **5 分钟内**，管理员在飞书内向同一 Bot 发送 **`/approve <码>`**（大小写与生成码一致） | 收到成功提示；`relay.json` 中 **`bindings[]` 新增/更新** 一条 `active` 记录（`auditTags` 含 `feishu-pairing` 等）；同一监听进程内 **`router.upsertGatewayBinding`** 已调用，**无需重启**。 |
| B3 | **同一会员**再次发送**普通文本**（非以 `/` 开头的命令） | **≤30s** 内收到 **飞书 Interactive Card** 形态的主回复（与 `feishu-channel` 行为一致）。 |
| B4 | 会员发送 **`/status`** | 返回可读状态文本（与 `handleStatus` 一致）。 |

### C. 负面与排错（抽检）

| # | 场景 | 预期 |
|---|------|------|
| C1 | `/approve` 漏码或格式错误 | Bot 提示用法：`/approve <配对码>`。 |
| C2 | 码错误或 **超过约 5 分钟** 未批 | 提示无效或已过期（内存配对表定时清理）。 |
| C3 | 审批人解析到的 `userId` 下**不包含**配对时所涉 Agent | 绑定失败错误信息（配置找不到目标 Feishu Agent）。 |

### D. 战略与证据

| # | 检查项 | 说明 |
|---|--------|------|
| D1 | **全程无 Telegram** | 不接 TG webhook、不验 TG 命令；与 `specs/pulse-product-strategy.md` 一致。 |
| D2 | **UAT 记录** | 脱敏记下环境、`userId`、Agent id、时间线；归档见 **`uat/AC-S1-001-record.md`**（T0-004）。 |

## Spec 同步

- L3 变更后走 **150-spec-sync**，更新本文件状态列与 `requirements.md` 必要时版本。
