---
feature_name: "runtime-guardrail"
version: "0.0.1"
created: "2026-04-06T12:50:00+0800"
modified: "2026-04-06T12:50:00+0800"
upstream_dependency: ["project-vision.md", "project-architecture.md", "project-standards.md"]
tags: [requirements, l2-spec, guardrail, constraint, verification]
---

# runtime-guardrail - 功能需求文档

> **功能概述**：为 agent 添加运行时约束层 — 通过声明式护栏 YAML 定义行为约束，在 checkpoint 时刻由代码强制拦截违规，在 /wrap 时刻由独立 sub-agent 提取行为事实并审核合规，使 agent 行为约束从"文字约定"变为"代码强制"。
>
> **关联文档**：
> - L1项目愿景：`project-specs/project-vision.md`
> - L1项目架构：`project-specs/project-architecture.md`
> - L1项目规范：`project-specs/project-standards.md`
> - L2技术设计：`features/runtime-guardrail/design.md`
> - L2任务拆分：`features/runtime-guardrail/tasks.md`
> - 已有资产：`harness/receipt.py`（Receipt/Verdict/VerificationProfile 模型可复用）
> - 原 Phase 2 设计：`features/session-receipt-verifier/`（本 feature 继承其 extractor/verifier 隔离原则，重定义审核对象）

---

## 1️⃣ 用户故事（User Stories）

### 主要用户故事

**故事1：门控点自动拦截**
- **作为**：大鸟（系统所有者）
- **我希望**：Emi 到达门控点（如 §1→§2 边界）时，harness 自动检查前置条件是否满足，不满足则返回 FAIL 并阻止继续
- **以便**：门控不再依赖 Emi 自觉遵守，代码保证正确性
- **优先级**：P0

**故事2：/wrap 行为合规审核**
- **作为**：大鸟
- **我希望**：/wrap 时自动从 session 原始记录中独立提取行为事实（spawn 次数、sub-agent 用途、文件操作、确认记录），然后对照护栏 YAML 逐条验证合规
- **以便**：事后发现执行中遗漏的行为型违规（如该 spawn 没 spawn、该确认没确认）
- **优先级**：P0

**故事3：护栏声明式管理**
- **作为**：大鸟
- **我希望**：每个 agent 的护栏以 YAML 文件声明，存储在 `.harness/guardrails/{agent}/` 下，由我或 Amanda 维护，被约束的 agent 只读不可修改
- **以便**：被审计者不能修改审计标准，护栏定义与 agent workspace 解耦
- **优先级**：P0

**故事4：sub-agent 有严格执行规则**
- **作为**：大鸟
- **我希望**：extractor 和 verifier 各有严格的执行规则和输出模版，定义提取什么、怎么提取、验证什么、怎么验证
- **以便**：sub-agent 不裸跑（AB 测试已证明：无操作层的 sub-agent ≈ 裸跑，40% vs 90%）
- **优先级**：P0

**故事5：违规自动追踪**
- **作为**：Ken（工程管家）
- **我希望**：审核发现的违规（FAIL 项）自动创建 harness work item，进入 Phase 1 状态机追踪
- **以便**：违规进入闭环追踪，不丢失、不遗忘
- **优先级**：P1

---

## 2️⃣ 功能需求（Functional Requirements）

### P0功能（MVP必需）

**FR-P0-001：GuardrailProfile 模型定义**
- **描述**：定义 GuardrailProfile 的 Pydantic schema，描述一条护栏的元数据（id、名称、描述）、约束类型（gate 或 behavioral）、检查条件（前置条件表达式或行为模式匹配）、检查方式（命令执行、文件检查、模式匹配）、通过条件。schema 支持两种类型：`gate`（checkpoint 时刻前置条件检查）和 `behavioral`（/wrap 时刻行为模式审核）。
- **来源**：故事1、故事3
- **示例**：
  ```yaml
  # .harness/guardrails/emi/gate-s1-complete.yaml
  guardrail_id: gate-s1-complete
  agent: emi
  type: gate
  description: "§1→§2 门控：问题之王 + 北极星须用户确认后才能进入 §2"
  check:
    type: file_exists
    paths:
      - "{experiment_dir}/five_why_result.md"
      - "{experiment_dir}/north_star.md"
    pass_condition: "两个文件均存在且非空"
  failure_action: "返回 FAIL，agent 不得进入 §2 执行"
  source_ref: "ami-space/AGENTS.md L104, SOUL.md L18"
  ```

**FR-P0-002：Gate check 引擎与 CLI**
- **描述**：实现 gate check 引擎，加载指定 agent 的 gate 类型护栏 YAML，执行前置条件检查，返回结构化结果（PASS/FAIL + 证据）。提供 CLI 命令 `harness gate check --agent <name> --gate <guardrail_id>`，agent 在 checkpoint 时刻同步调用。FAIL 时输出失败原因和缺失项，退出码为 1。
- **来源**：故事1
- **示例**：Emi 准备从 §1 进入 §2，调用 `harness gate check --agent emi --gate gate-s1-complete`。harness 检查 five_why_result.md 和 north_star.md 是否存在且非空。如果 five_why_result.md 缺失，返回 FAIL 并输出 "Missing: five_why_result.md"。

**FR-P0-003：BehavioralFact 模型定义**
- **描述**：定义 BehavioralFact 的 Pydantic schema，作为 extractor 的结构化输出。记录 session 中可观测的行为事实：spawn 记录（次数、每次的目标 agent、用途、输入摘要）、文件操作记录（创建/修改的文件路径和时间）、用户确认记录（哪些产出物经过用户确认、确认时间点）、决策记录（选了什么、拒绝了什么、为什么）。BehavioralFact 只记录事实，不做判断。
- **来源**：故事2、故事4
- **示例**：
  ```yaml
  session_id: "2026-04-06-emi-001"
  agent: emi
  period: "2026-04-06T10:00/12:00+0800"
  spawns:
    - target: "sub-agent"
      purpose: "5WHY 分析"
      rule_ref: "Doc/rules/collab-pattern/301-input-preparation.md"
    - target: "sub-agent"
      purpose: "广泛提案"
      rule_ref: null
  file_operations:
    - path: "Projects/实验/five_why_result.md"
      action: created
    - path: "Projects/实验/north_star.md"
      action: created
  user_confirmations:
    - artifact: "问题之王"
      confirmed: true
      context: "用户确认'问题之王'表述"
    - artifact: "north_star.md"
      confirmed: true
  decisions:
    - what: "5WHY 由谁执行"
      chose: "spawn sub-agent"
      rejected: "Emi 自己执行"
      rationale: "确定性规则驱动任务，按 Q1 判据应 spawn"
  transcript_source: "sessions_history"
  transcript_truncated: false
  ```

**FR-P0-004：ComplianceVerdict 模型定义**
- **描述**：定义 ComplianceVerdict 的 Pydantic schema，作为 verifier 的结构化输出。对每条适用的护栏输出合规判定（COMPLIANT/VIOLATED/UNVERIFIABLE）+ 证据 + 验证方式。包含汇总摘要和违规项的修复建议（action_items）。
- **来源**：故事2
- **示例**：
  ```yaml
  facts_path: "memo/receipts/2026-04-06-emi-001.facts.yaml"
  guardrails_dir: ".harness/guardrails/emi/"
  verified_at: "2026-04-06T12:05:00+0800"
  results:
    - guardrail_id: spawn-enforcement
      verdict: COMPLIANT
      evidence: "5WHY 由 sub-agent 执行（facts.spawns[0]），符合 Q1 判据"
      verification_method: "behavioral_pattern_match"
    - guardrail_id: confirm-before-persist
      verdict: VIOLATED
      evidence: "north_star.md 在用户确认前已写入文件（file_operations 时间 < user_confirmations 时间）"
      verification_method: "temporal_sequence_check"
  summary:
    total: 5
    compliant: 3
    violated: 1
    unverifiable: 1
  action_items:
    - "north_star.md 须在用户确认后才落盘，本次违规需人工复核"
  ```

**FR-P0-005：Compliance check 引擎与 CLI**
- **描述**：实现 compliance check 引擎，加载指定 agent 的所有 behavioral 类型护栏 YAML + BehavioralFact，逐条检查合规并输出 ComplianceVerdict。提供 CLI 命令 `harness compliance check --agent <name> --facts <path>`。
- **来源**：故事2
- **示例**：/wrap 时 verifier 产出 facts YAML 后，主 agent 调用 `harness compliance check --agent emi --facts memo/receipts/2026-04-06-emi-001.facts.yaml`，harness 加载 `.harness/guardrails/emi/` 下所有 behavioral 类型的 YAML，逐条与 facts 比对，输出 ComplianceVerdict。

**FR-P0-006：Extractor 执行规则与模版**
- **描述**：为 session-extractor sub-agent 编写严格的执行规则（`.claude/agents/session-extractor.md`），定义：(1) 输入协议——通过 attachment 接收 session 原始对话记录；(2) 提取规则——从对话中提取 spawns、file_operations、user_confirmations、decisions 四类行为事实；(3) 输出模版——严格遵循 BehavioralFact YAML schema；(4) 禁止事项——不做任何判断、不遗漏不利事实、不访问文件系统。规则中包含具体的提取示例和边界情况处理。
- **来源**：故事4
- **示例**：extractor 收到 Emi 的 session 对话记录，从中识别出 3 次 spawn（5WHY、广泛提案、评审）、5 个文件操作（创建 five_why_result.md 等）、2 次用户确认，按 BehavioralFact schema 输出 YAML。

**FR-P0-007：Verifier 执行规则与模版**
- **描述**：为 session-verifier sub-agent 编写严格的执行规则（`.claude/agents/session-verifier.md`），定义：(1) 输入协议——接收 BehavioralFact YAML 路径 + guardrails 目录路径；(2) 验证规则——对每条 behavioral 类型护栏，从 facts 中找对应的行为记录，按护栏定义的 check 条件判定合规/违规；(3) 输出模版——严格遵循 ComplianceVerdict YAML schema；(4) 禁止事项——不访问 session 对话、不修改文件、不执行破坏性命令。规则中包含逐条验证的示例和 UNVERIFIABLE 的判定标准。
- **来源**：故事4
- **示例**：verifier 收到 facts YAML + guardrails 目录，加载 spawn-enforcement 护栏，从 facts.spawns 中检查 5WHY 是否由 sub-agent 执行。发现 facts 中有 spawn 记录且 purpose 为 "5WHY 分析"，判定 COMPLIANT。

**FR-P0-008：Emi 护栏 Profiles 实例化**
- **描述**：基于 ami-space AGENTS.md 和 SOUL.md 中的行为约束，为 Emi 创建 5 条护栏 YAML 文件，存放在 `.harness/guardrails/emi/` 下：(1) gate-s1-complete — §1→§2 门控；(2) spawn-enforcement — 确定性子任务必须 spawn；(3) confirm-before-persist — 关键产出物须用户确认才落盘；(4) perfect-score-alert — 满分是危险信号；(5) no-orchestrator-injection — 禁止编排者加戏。
- **来源**：故事1、故事2、故事3
- **示例**：5 个 YAML 文件各定义一条护栏，包含 guardrail_id、type（gate 或 behavioral）、check 条件、pass_condition、failure_action、source_ref。

**FR-P0-009：/wrap 集成协议**
- **描述**：修改 Emi 的 AGENTS.md `/wrap` 段，在现有步骤前插入护栏审核流程：(1) 主 agent 调用 sessions_history 获取原始对话；(2) spawn session-extractor（传入对话 attachment）生成 BehavioralFact；(3) 将 facts 写入 `memo/receipts/`；(4) spawn session-verifier（传入 facts 路径 + guardrails 目录）生成 ComplianceVerdict；(5) 将 verdict 写入 `memo/receipts/`；(6) 汇报合规结果摘要。现有 /wrap 步骤（progress.md、git commit）不受影响。
- **来源**：故事2
- **示例**：Emi 执行 /wrap，先拉取对话记录，spawn extractor 生成 facts，再 spawn verifier 审核，最后汇报 "Compliance: 5 guardrails — 4 COMPLIANT, 1 VIOLATED (confirm-before-persist)"，然后继续正常 /wrap 流程。

### P1功能（增强功能）

**FR-P1-001：违规自动创建 work item**
- **描述**：新增 CLI 命令 `harness compliance create-work-item <verdict-path>`，解析 ComplianceVerdict YAML，对 VIOLATED 项自动创建 harness work item（复用 Phase 1 WorkItem 状态机），acceptance_criteria 从 action_items 提取。
- **来源**：故事5
- **示例**：verdict 包含 1 个 VIOLATED（confirm-before-persist），命令自动创建 work item，criteria 为 "north_star.md 须在用户确认后才落盘"。

**FR-P1-002：HEARTBEAT 合规巡检**
- **描述**：在 HEARTBEAT 模板中新增 2 个检查项：(1) 最近 verdict 是否存在且 ≤7 天；(2) harness 中是否有超过 14 天未 complete 的违规跟进 work item。
- **来源**：故事5

### P2功能（锦上添花）

**FR-P2-001：护栏模版库**
- **描述**：从 Emi 的 5 条护栏中提取通用模式，创建 guardrail templates 目录（`agent-harness/templates/guardrails/`），供其他 agent（Ken、Aki、Amanda）复制后定制。
- **来源**：通用性目标

**FR-P2-002：合规趋势统计**
- **描述**：`harness compliance stats --agent <name>` 命令，扫描 `memo/receipts/` 下所有 verdict，输出合规趋势（COMPLIANT/VIOLATED/UNVERIFIABLE 分布、常见违规模式、改善趋势）。
- **来源**：度量需求

---

## 3️⃣ 验收标准（Acceptance Criteria）

> **重要提示**：验收标准必须可量化、可测试、可验证

### P0功能验收标准

**AC-P0-001：Gate check 拦截率**
- **条件**：构造 Emi 的 gate-s1-complete 场景，分别测试前置条件满足和不满足的情况
- **预期结果**：前置条件不满足时，gate check 返回 FAIL 且退出码为 1，拦截率 100%（构造 ≥5 种缺失组合，全部被拦截）
- **验证方式**：自动化单元测试 + CLI 集成测试

**AC-P0-002：BehavioralFact 提取完整性**
- **条件**：构造一个包含 3 次 spawn、4 次文件操作、2 次用户确认的 mock session 对话
- **预期结果**：extractor 输出的 BehavioralFact 包含 ≥3 条 spawns、≥4 条 file_operations、≥2 条 user_confirmations，遗漏率 ≤10%
- **验证方式**：构造标准测试对话 + 人工审核 extractor 输出

**AC-P0-003：Compliance check 判定正确性**
- **条件**：构造 BehavioralFact + 5 条 Emi 护栏，其中 3 条应判 COMPLIANT、1 条应判 VIOLATED、1 条应判 UNVERIFIABLE
- **预期结果**：ComplianceVerdict 恰好包含 5 条 results，verdict 判定与预期一致，summary 计数正确
- **验证方式**：自动化单元测试

**AC-P0-004：GuardrailProfile schema 校验**
- **条件**：提交 Emi 的 5 条护栏 YAML
- **预期结果**：全部通过 Pydantic schema 校验，包含必填字段（guardrail_id、agent、type、check、pass_condition）
- **验证方式**：自动化单元测试

**AC-P0-005：Extractor 隔离性**
- **条件**：session-extractor sub-agent 被 spawn 时
- **预期结果**：extractor 的输入仅为对话原文 attachment，不包含护栏 YAML 或产出物文件。extractor agent 定义中 tools 列表为空（不访问文件系统）
- **验证方式**：审查 session-extractor.md agent 定义

**AC-P0-006：Verifier 隔离性**
- **条件**：session-verifier sub-agent 被 spawn 时
- **预期结果**：verifier 的输入仅为 BehavioralFact YAML 路径 + guardrails 目录路径，不包含 session 对话内容。verifier agent 定义中不包含获取 session history 的能力
- **验证方式**：审查 session-verifier.md agent 定义

**AC-P0-007：/wrap 集成不中断现有流程**
- **条件**：Emi 执行包含 extractor + verifier 的 /wrap 流程
- **预期结果**：progress.md 仍被正确写入，git commit 仍执行，/wrap 总耗时增加 ≤3 分钟
- **验证方式**：在 Emi 的真实 session 上执行 /wrap，验证 progress.md 和 git commit 正常

**AC-P0-008：护栏不可被 agent 修改**
- **条件**：Emi 尝试修改 `.harness/guardrails/emi/` 下的护栏文件
- **预期结果**：通过文件权限或 PROTOCOL 约定，Emi 对护栏文件只有读取权限
- **验证方式**：文件权限检查 + PROTOCOL.md 明确声明

### P1功能验收标准

**AC-P1-001：VIOLATED 自动创建 work item**
- **条件**：ComplianceVerdict 包含 ≥1 个 VIOLATED 结果
- **预期结果**：`harness compliance create-work-item` 创建 1 个 work item，acceptance_criteria 包含所有 VIOLATED 项的 action_items
- **验证方式**：构造含 VIOLATED 的 verdict，执行命令，检查 `.harness/work-items/` 下新增文件

---

## 4️⃣ 非功能需求（Non-Functional Requirements）

### 性能需求

- **gate check 响应时间**：`harness gate check` 返回 ≤5 秒（本地文件检查，无网络调用）
- **extractor 耗时**：sub-agent 提取 ≤60 秒（单次 LLM 调用）
- **verifier 耗时**：sub-agent 验证 ≤120 秒（含文件读取和模式匹配）
- **/wrap 总增加耗时**：≤3 分钟（extractor + verifier + 写文件）

### 可维护性需求

- **代码规范**：遵循 project-standards.md — PEP 8 + ruff + mypy strict
- **测试覆盖率**：guardrail 模型 ≥90%，gate engine ≥95%（核心逻辑），compliance engine ≥80%，CLI ≥70%。整体 ≥80%
- **文档**：公开函数有 docstring（Google style）

### 安全需求

- **护栏只读隔离**：被约束的 agent 不能修改自己的护栏定义
- **verifier 隔离**：session-verifier 不能访问 session 对话内容
- **不泄露敏感信息**：BehavioralFact 中不记录 API 密钥、用户凭据

---

## 5️⃣ 约束条件（Constraints）

### 技术约束

- **技术栈**：遵循 project-architecture.md — Python 3.11+ / Pydantic / PyYAML / Click / pytest
- **存储格式**：YAML 文件 + Git（与 Phase 1 一致）
- **护栏存储位置**：`.harness/guardrails/{agent}/*.yaml`（不在 agent workspace 内）
- **sub-agent 机制**：extractor/verifier 定义为 `.claude/agents/*.md`，通过 OC ACP spawn
- **与已有模块的关系**：可复用 `receipt.py` 的 VerificationProfile 匹配机制和序列化工具；新增 `guardrail.py` 模块，不修改现有 models/machine/store

### 时间约束

- **P0 开发周期**：2 周
- **P1 开发周期**：P0 验证后 1 周

### 资源约束

- **开发**：Ken 主导，sub-agent 协助实现
- **运行**：extractor + verifier 均用 sonnet 模型（每次 /wrap 增加 2 次 sub-agent spawn）

---

## 6️⃣ 排除范围（Out of Scope）

- ❌ 不修改 Phase 1 状态机（WorkItem / HandoffContract / SignalEvent 模型不变）
- ❌ 不做实时行为拦截（behavioral 类型护栏只在 /wrap 时回顾审核，不在执行中实时阻断）
- ❌ 不自动推广到所有 agent — 先在 Emi 上验证，手动推广到其他 agent
- ❌ 不做 Web UI / Dashboard — 通过 CLI 和 YAML 文件查看
- ❌ 不跨 OC 环境（不支持云端 agent 的护栏审核）
- ❌ 不做护栏的自动生成（护栏由人类设计，工具只负责执行和验证）
- ❌ 不替代 ssot-judge — ssot-judge 审产出物质量，本 feature 审 agent 行为合规，互补不互斥

---
