---
feature_name: "agent-registry-extended"
version: "0.0.1"
created: "2026-04-21T13:40:00+0800"
modified: "2026-04-21T13:40:00+0800"
upstream_dependency:
  - "ssot/registry.yaml"
  - "features/heartbeat-runtime/design.md"
  - "PULSE/Relay/specs/features/agent-scheduler/"
tags: [requirements, l2-spec, ssot, registry, agent-metadata]
co_owners: [ken]
changelog:
  - "v0.0.1 (2026-04-21): 初版。触发事件：wi_20260421_634 Relay bot routing bug 暴露 Agent 身份元数据散落 5 处、绑定关系隐式（agent.id == job.owner == policy.agent_id == registry.key == relay.agents[].id 全是约定，无校验）。"
---

# agent-registry-extended — 功能需求文档

> **功能概述**：扩展 `ssot/registry.yaml` 的 `agents:` 段为 Agent 运行时元数据的 SSOT——新增 5 字段（`tg_bot` / `tg_chat_id` / `policy_path` / `runtime_status` / `cron_enabled`），把散落在 Relay config / harness policy / workspace IDENTITY 的隐式绑定关系集中到一处声明。消费方（Relay scheduler / harness ssot check / 新 Agent 上线 checklist）从 registry 读权威事实，避免 "硬编码第一个 bot" 类结构错误。**不入 registry**：botToken（secret，仍在 Relay config）。
>
> **关联**：
> - 触发事件 memo：`ken-space/memo/evidence-2026-04-21-wi_634-complete.md`
> - SSOT 当前状态：`ssot/registry.yaml`（schema_version=1.0，agents 段仅 workspace/identity/depends_on 三字段）
> - 上游 feature：`features/heartbeat-runtime/`（policy schema）、`PULSE/.../agent-scheduler/`（Relay cron）
> - L2 设计：`design.md`
> - L2 任务：`tasks.md`

---

## 0. 问题、定位

### 问题（wi_20260421_634 暴露的根因，不止 symptom）

Amanda cron (owner=amanda) 首次 run 推到 Duke bot——表面是 `src/index.ts:102 agentBotList[0]` 硬编码，根因是**没有"owner → bot 绑定"的权威 SSOT**可查，工程师只能走隐式索引顺序。

**当前 Agent 身份元数据分布（5 处）**：

| 位置 | 字段 | 用途 | 绑定键 |
|------|------|------|--------|
| `ssot/registry.yaml::agents[<id>]` | workspace / identity / depends_on | 静态依赖图 | key=agent_id |
| `PULSE/.../relay.config.json::users[].agents[]` | id / botToken / sessionId | bot 运行时 | id |
| `PULSE/.../cron-jobs.json::jobs[].owner` | owner (string) | cron 调度分流 | owner |
| `.harness/policies/<agent>.yaml` | agent_id / workspace / escalation.recipient | harness 运行时 | agent_id |
| `Doc/<space>/IDENTITY.md` 等 | name / mission | 人读文档 | name |

**全是约定，无强校验**：`agent.id == job.owner == policy.agent_id == registry.key` 只靠 code 风格保持一致。wi_634 能成立就是因为 Relay 不需要通过 agent.id 查 bot——它用索引。

### 定位：registry.yaml 升级为"Agent 运行时 SSOT"

registry 本是 SSOT 治理的坐标系（Ken 4/17 的认知"SSOT 没定全，漂移从何谈起"）。`agents:` 段现在只含静态绑定（workspace / identity），把运行时绑定（tg_bot / policy_path / cron_enabled）也纳入后：

1. Relay scheduler 能按 agent_id 查 bot（不靠索引）
2. harness ssot check 能做"身份一致性"校验（registry key ↔ policy.agent_id ↔ IDENTITY.md name）
3. 新 Agent 上线必须先在 registry 注册（上线 gate）
4. `harness registry show <agent>` 一站式查元数据

### 范围与边界

**in-scope (P0)**：
- registry.yaml agents 段新字段：tg_bot / tg_chat_id / policy_path / runtime_status / cron_enabled
- Pydantic schema 扩展（向后兼容：默认值）
- harness ssot check 新规则：agent.id 一致性
- `harness registry show <agent>` 新 CLI
- amanda / reeve / aki 现有 entry 迁移（填新字段）

**in-scope (P1, 非本 feature 但登记)**：
- Relay startup 读 registry 校验 tg_bot（跨仓库依赖，PULSE 改动）—— 本 feature 提 interface contract，Relay 侧 feature 分开立项
- 新 Agent 上线 checklist（Amanda 的 `agent-workspace-builder` skill 对接）—— 本 feature 只定字段，Amanda 更新 skill 归她域

**out-of-scope**：
- botToken 入 registry（secret 不入）
- 多租户扩展（当前所有 agent 服务 member-000 Owner，单租户）
- 动态 hot reload（registry 改动要求 agent workspace 相关 tool 重启）
- registry.entries 段改动（本 feature 只改 agents 段）

---

## 1. 用户故事

### Story-001 — Relay scheduler 按 registry 校验 bot 绑定

**As** Relay startup 流程，
**When** 初始化 scheduler + 构造 agentId→Bot map，
**Then** 对每个 Agent 从 registry 读 `tg_bot`，若 Relay config 里该 agent.id 对应的 bot username 与 registry.tg_bot 不一致 → startup error，阻止错发。

（P1 交付，本 feature 定 schema；PULSE 侧 feature 单独立项消费）

### Story-002 — harness ssot check 抓 agent.id 不一致

**As** 任一 Agent 开 session（L1 startup scan）或 cron tick（L2 scan），
**When** `harness ssot check` 运行，
**Then** 若 `registry.agents[<id>].policy_path` 指向的 policy 文件的 `agent_id` 字段与 registry key 不一致（Agent 被重命名、policy 未同步）→ drift 事件；critical → 立即 escalate。

### Story-003 — 新 Agent 上线先注册 registry

**As** Ken / Amanda 给新 Agent（比如 Emi）做 workspace，
**When** `agent-workspace-builder` 流程跑到 "Agent 上线前置"，
**Then** skill 要求先在 registry.yaml 填 agents 段完整 5 字段 + `runtime_status=bootstrap`；`harness ssot register` 校验字段完整性后才能进 active。

### Story-004 — 一站式查 Agent 元数据

**As** 任何 Agent（人工或 cron 分身），
**When** 需要知道 Amanda 的 TG bot / policy 路径 / 是否已启 cron，
**Then** 跑 `harness registry show amanda` 拿到结构化输出，不用自己拼装 5 处信息。

---

## 2. 功能需求（FR）

### FR-P0-001 — registry agents 段扩展 5 字段

`AgentEntry` 模型新增字段（命名沿用现有 snake_case 风格）：

| 字段 | 类型 | 必填 | 默认 | 说明 |
|------|------|------|------|------|
| `tg_bot` | str? | optional | None | TG bot username（含 `@`，人读 + Relay 可选对照）|
| `tg_chat_id` | str? | optional | None | TG chat_id，作 `policy.escalation.recipient` 来源 |
| `policy_path` | str? | optional | None | 指向 `.harness/policies/<agent>.yaml` 的仓库相对路径 |
| `runtime_status` | Literal["active","bootstrap","retired"] | optional | "active" | 生命周期状态 |
| `cron_enabled` | bool | optional | False | 该 Agent 是否已在 Relay 注册至少一条 enabled cron job |

**向后兼容**：Pydantic 默认值保证旧 registry（仅 workspace/identity/depends_on）能 load。

### FR-P0-002 — Pydantic schema 扩展

`harness/ssot.py`（或相邻的 registry 模型文件）里 `AgentEntry`（或等效类）加上述字段。验证规则：
- `runtime_status` 枚举值外报 ValidationError
- `policy_path` 非空时格式校验（以 `.harness/policies/` 开头 + `.yaml` 结尾）
- `tg_chat_id` 非空时须是纯数字 string
- `tg_bot` 非空时须以 `@` 开头

### FR-P0-003 — harness ssot check 新规则：agent.id 一致性

扫描 `ssot/registry.yaml::agents` 每个 entry：
- 若 `policy_path` 非空 → load 该 policy → 校验 `policy.agent_id == registry key`；不匹配则记 drift（severity=critical）
- `runtime_status="retired"` 的 entry → 若 `.harness/policies/<id>.yaml` 仍存在且 `agent_id` 匹配 → drift（警告："retired agent 的 policy 未归档"）

不破坏现有 drift 检测规则。累加新规则。

### FR-P0-004 — `harness registry show <agent_id>` CLI

输出结构化信息：

```
Agent: amanda
  workspace: Doc/amanda-space/
  identity: Doc/amanda-space/IDENTITY.md
  tg_bot: @example_amanda_bot
  tg_chat_id: 1000000001
  policy_path: .harness/policies/amanda.yaml
  runtime_status: active
  cron_enabled: true
  depends_on: [agent-constitution]
```

选项：`--json` 机读格式（便于 Relay / 其他工具消费）。

### FR-P0-005 — 现有 entry 迁移

本 feature L3 交付时同步 update `ssot/registry.yaml` 里 amanda/reeve/aki entry 填新字段：

- **amanda**：tg_bot=@example_amanda_bot / tg_chat_id=1000000001 / policy_path=.harness/policies/amanda.yaml / runtime_status=active / cron_enabled=true
- **reeve**：tg_bot=@... (待查 relay.config) / tg_chat_id=1000000001 / policy_path=.harness/policies/reeve.yaml (若存在) / runtime_status=bootstrap / cron_enabled=false
- **aki**：tg_bot=@... / tg_chat_id=1000000001 / policy_path=.harness/policies/aki.yaml (若存在) / runtime_status=active（已有早报 cron）/ cron_enabled=true

---

## 3. 非功能需求（NFR）

### NFR-001 — 向后兼容

- 旧 registry.yaml 不改也能 load（Pydantic 默认值）
- 老消费方（只读 workspace/identity/depends_on）继续工作
- 本 feature 不改 `registry.entries`（SSOT 条目表）段

### NFR-002 — 安全

- botToken **永不**入 registry（secret-in-config）
- registry.yaml 作 monorepo 入 git——内容属"公开元数据级别"（不含 secret）

### NFR-003 — 测试覆盖

- Pydantic schema 新字段 UT
- `harness ssot check` agent.id 一致性规则 UT + 集成测试
- `harness registry show` CLI UT（text + json 两种输出）
- amanda/reeve/aki 迁移后 registry.yaml 能通过 `harness ssot check`

---

## 4. 接受标准（AC）

### AC-P0-001 — schema 扩展

- 老 registry.yaml（仅 3 字段）→ load 成功，新字段默认值
- 新字段齐全 registry → load 成功，所有字段可查
- 错误 `runtime_status=invalid` → ValidationError
- 错误 `tg_bot=no_at_sign` → ValidationError
- 错误 `tg_chat_id=abc` → ValidationError
- 错误 `policy_path=random.yaml` → ValidationError

### AC-P0-002 — harness ssot check 一致性

- registry.amanda.policy_path 指向 amanda.yaml，amanda.yaml.agent_id=amanda → 无 drift
- 故意把 amanda.yaml.agent_id 改为 reeve → drift critical（identity mismatch）
- registry.retired.entry 但 policy 仍在 → drift 警告

### AC-P0-003 — registry show CLI

- `harness registry show amanda` → text 格式含全字段
- `harness registry show amanda --json` → JSON 机读
- `harness registry show unknown` → exit 非零 + error 消息
- `harness registry show --help` → 显示帮助

### AC-P0-004 — 回归

- 现有 harness 测试不破（964 基线全过）
- `harness ssot check` 对现有 registry 行为不变（新规则对老 registry 是 no-op）

### AC-P0-005 — 迁移

- amanda / reeve / aki 三个 entry 迁移完后，`harness ssot check` 通过
- `harness registry show amanda` 输出含全 5 字段（非 None）

---

## 5. 依赖与约束

### 上游

- `ssot/registry.yaml` v1.0 schema（现有）
- `harness/ssot.py` 的 Registry 模型（现有）
- `harness/ssot_engine.py` 的 drift 检测引擎（现有）
- `harness ssot check` + `harness ssot register` 命令（现有）

### 下游（P1 消费方，本 feature 不实现）

- Relay startup 读 registry 校验 tg_bot：PULSE 仓独立 feature，本 feature 定 schema 契约
- agent-workspace-builder skill "上线前置" 步骤：Amanda 域，本 feature 发通知请她更新

### 技术约束

- Python stdlib + 现有 pydantic/pyyaml，无新 pip 依赖
- 不改 `relay.config.json` schema
- 不改 `cron-jobs.json` schema
- 不改 `.harness/policies/<agent>.yaml` schema

---

## 6. 风险与对策

| 风险 | 概率 | 对策 |
|------|------|------|
| 字段漂移（registry.tg_bot vs relay.config.botToken 对应 bot 不匹配）| 中 | P1 Relay startup check 就是防这个；本 feature 定 schema 契约 |
| Amanda / Reeve 不接受在 registry 填他们的 tg_bot（越权担忧）| 低 | registry.yaml 属 Ken 域（SSOT 治理），Agent workspace 不变；消费方可选用 |
| runtime_status 状态机演进（bootstrap → active → retired → ?）| 低 | P0 只定三值；后续状态扩展用 schema migration |
| 字段冗余（policy 里已有 workspace，registry 又重复）| 低 | registry 的 workspace 本就存在；新字段不重复 policy 字段（escalation.recipient 不在 registry，仍在 policy） |
| cron_enabled 状态漂移（Amanda 真删 cron 但 registry 说 true）| 中 | P1 加一条 drift 规则：对比 registry.cron_enabled vs Relay cron-jobs.json 里是否有 enabled cron of that owner |

---

## 7. 未来扩展（非本期）

- Relay startup 读 registry 校验（P1，PULSE 仓 feature）
- cron_enabled 动态同步（从 cron-jobs.json 反推）
- 多租户（每 Agent 多 chat_id）
- runtime_status 状态机（bootstrap → active 需审批）
- `harness registry audit` 全量健康扫描
