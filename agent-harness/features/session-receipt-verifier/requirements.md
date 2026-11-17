---
feature_name: "session-receipt-verifier"
version: "0.0.1"
created: "2026-04-03T12:00:00+0800"
modified: "2026-04-03T12:00:00+0800"
upstream_dependency: ["project-vision.md", "project-architecture.md"]
tags: [requirements, l2-spec, phase2, verification, observability]
---

# session-receipt-verifier - 功能需求文档

> **功能概述**：让 agent session 的行为过程可审计——每次 /wrap 时生成结构化收据（receipt），由独立评估器（verifier）验证 agent 的声明是否属实，失败项自动进入 harness 追踪闭环。
>
> **关联文档**：
> - L1项目愿景：`project-specs/project-vision.md`
> - L1项目架构：`project-specs/project-architecture.md`
> - L2技术设计：`features/session-receipt-verifier/design.md`
> - L2任务拆分：`features/session-receipt-verifier/tasks.md`

---

## 1️⃣ 用户故事（User Stories）

### 主要用户故事

**故事1：session 行为可审计**
- **作为**：大鸟（系统所有者）
- **我希望**：每个 agent session 结束时自动生成一份结构化收据，记录做了什么任务、产出了什么文件、声称了什么结论
- **以便**：不用翻聊天记录就能知道 agent 做了什么、声称了什么
- **优先级**：P0

**故事2：声明独立验证**
- **作为**：大鸟
- **我希望**：有一个独立评估器自动验证收据中的声明（claims），它看不到生成过程，只看到结构化收据和产出物文件
- **以便**：不用自己逐项检查 agent 的产出是否属实
- **优先级**：P0

**故事3：验证失败自动追踪**
- **作为**：Ken（工程管家）
- **我希望**：当评估器发现声明为 FAIL 时，自动创建 harness work item
- **以便**：质量问题进入追踪闭环，不丢失、不遗忘
- **优先级**：P1

---

## 2️⃣ 功能需求（Functional Requirements）

### P0功能（MVP必需）

**FR-P0-001：Receipt Schema 定义**
- **描述**：定义 receipt YAML 的结构化 schema，包含 session 元数据（agent、时间）、任务描述（task）、产出物列表（outputs）、agent 声明（claims）、验证提示（verification_hints）。schema 由 Pydantic model 强制校验。
- **来源**：故事1
- **示例**：
  ```yaml
  session_id: "2026-04-03-ken-001"
  agent: ken
  period: "2026-04-03T11:35/15:00+0800"
  task:
    description: "生成 2026-03 月度财务报告"
    requester_intent: "准确的月度收支和资产负债表"
  outputs:
    - path: "memo/2026-03-report.md"
      type: report
  claims:
    - "所有 3 月交易已入库"
    - "资产分类覆盖率 100%"
  verification_hints:
    - "期末余额 = 期初 + 本期变动"
  ```

**FR-P0-002：Verdict Schema 定义**
- **描述**：定义 verdict YAML 的结构化 schema，包含对每个 claim 的判定（PASS / FAIL / UNVERIFIABLE）、判定证据、总体摘要、行动项列表。schema 由 Pydantic model 强制校验。
- **来源**：故事2
- **示例**：
  ```yaml
  receipt_path: "memo/receipts/2026-04-03-ken-001.yaml"
  verified_at: "2026-04-03T15:05:00+0800"
  results:
    - claim: "所有 3 月交易已入库"
      verdict: PASS
      evidence: "交易表 307 条，与银行对账单一致"
    - claim: "资产分类覆盖率 100%"
      verdict: FAIL
      evidence: "3 个持仓未分类: AAPL, MSFT, GOOG"
  summary:
    total: 2
    pass: 1
    fail: 1
    unverifiable: 0
  action_items:
    - "修复 3 个未分类持仓"
  ```

**FR-P0-003：Session Extractor（独立 sub-agent）**
- **描述**：定义一个 OC sub-agent（`.claude/agents/session-extractor.md`），在 /wrap 时由主 agent spawn。主 agent 通过 `sessions_history` 获取自身原始对话记录，作为 attachment 传给 extractor。extractor 独立于主 agent，从原始对话中提取结构化 receipt——判断哪些是 claims、哪些是 decisions、产出物是什么。extractor 看到的是**原始对话转录**，不是主 agent 的摘要，防止选择性偏差。
- **来源**：故事1
- **示例**：Ken 完成 harness M2 开发后执行 /wrap，Ken 调用 `sessions_history` 获取对话原文，传给 session-extractor。extractor 从对话中提取：task="实现 store + CLI"、outputs=[models.py, store.py, cli.py]、claims=["183 tests", "100% coverage"]、decisions=[{chose: "Click CLI", rejected: "argparse"}]、verification_hints=["pytest --cov 输出覆盖率"]

**FR-P0-004：Session Verifier（独立 sub-agent）**
- **描述**：定义一个 OC sub-agent（`.claude/agents/session-verifier.md`），它接收 receipt YAML 路径和产出物文件路径作为输入，逐条验证 claims。verifier 可以读文件、跑脚本（Bash）、搜索内容（Grep），但看不到生成过程的任何对话。对每个 claim 输出 PASS / FAIL / UNVERIFIABLE + 证据。
- **来源**：故事2
- **示例**：verifier 收到 receipt，看到 claim "183 tests, 100% coverage"，跑 `pytest --cov` 验证测试数和覆盖率是否属实

**FR-P0-005：/wrap 协议集成**
- **描述**：修改 OA 的 AGENTS.md Session Commands `/wrap` 段，在现有步骤（汇报→ws 维护→写 progress.md→git commit）之前插入三步：(1) 主 agent 调用 `sessions_history` 获取原始对话；(2) spawn session-extractor（传入对话 attachment）生成 receipt；(3) spawn session-verifier（传入 receipt）生成 verdict。verdict 结果纳入 /wrap 汇报。
- **来源**：故事1 + 故事2
- **示例**：Ken /wrap 时先拉取对话记录，spawn extractor 生成 receipt，再 spawn verifier 验证 receipt，最后在汇报中包含验证结果摘要

**FR-P0-006：Verification Profiles 定义与加载**
- **描述**：定义 VerificationProfile YAML schema（profile_id、claim_patterns 正则列表、verification 动作定义）。verifier 在验证每个 claim 前先加载 `{oa-workspace}/verification-profiles/*.yaml`，按 claim_patterns 匹配。匹配到 profile 则按预定义的验证命令/检查执行；无匹配则 fallback 到 ad_hoc。verdict 中每个 ClaimResult 记录 `verification_method`（"profile:test-pass" 或 "ad_hoc"）。
- **来源**：故事2（独立验证的可重复性和可审计性）
- **示例**：claim "183 tests, 100% coverage" 匹配 `test-pass` profile（pattern `\d+\s*tests`），verifier 执行 `pytest --tb=short -q`，验证退出码为 0；如果同时匹配 `coverage` profile，再执行 `pytest --cov` 验证覆盖率数值

**FR-P0-007：Ken Verification Profiles 实例化**
- **描述**：基于通用模板（`agent-harness/templates/verification-profiles/`），为 Ken 创建 5 个验证规则：test-pass（测试通过）、coverage（覆盖率）、lint-clean（代码检查通过）、file-exists（文件已生成）、structure-complete（文档结构完整）。放在 `ken-space/verification-profiles/` 下。
- **来源**：故事2
- **示例**：Ken 的 `test-pass.yaml` 指定 `command: "cd {project_root} && python -m pytest --tb=short -q"`，当 claim 提到 tests/测试通过时自动用此命令验证

### P1功能（增强功能）

**FR-P1-001：harness create-from-verdict**
- **描述**：新增 CLI 命令 `harness create-from-verdict <verdict-path>`，解析 verdict YAML，对 FAIL 项自动创建 harness work item，acceptance_criteria 从 action_items 提取。
- **来源**：故事3
- **示例**：verdict 包含 1 个 FAIL（"3 个持仓未分类"），命令自动创建 work item，criteria 为 "修复 3 个未分类持仓"

**FR-P1-002：HEARTBEAT 审计检查项**
- **描述**：在 HEARTBEAT 模板中新增 2 个检查项：(1) 最近 receipt 是否存在且 ≤7 天；(2) harness 中是否有超过 14 天未 complete 的 FAIL 跟进 work item。
- **来源**：故事3

### P2功能（锦上添花）

**FR-P2-001：Receipt 批量分析**
- **描述**：`harness receipt-stats` 命令，扫描 `memo/receipts/` 下所有 receipt + verdict，输出趋势统计（PASS/FAIL/UNVERIFIABLE 分布、claims 数量趋势、常见 FAIL 模式）。
- **来源**：大鸟公式产出度量需求

---

## 3️⃣ 验收标准（Acceptance Criteria）

> **重要提示**：验收标准必须可量化、可测试、可验证

### P0功能验收标准

**AC-P0-001：Receipt 生成正确性**
- **条件**：agent 执行 /wrap 时
- **预期结果**：`memo/receipts/` 下生成 1 个 YAML 文件，文件通过 Pydantic schema 校验，包含 ≥1 个 claim
- **验证方式**：在 Ken 的真实 session 上执行 /wrap，检查生成的 receipt 文件

**AC-P0-005：纯讨论 session 的 receipt 有效性**
- **条件**：session 无文件产出（纯设计讨论、方案评审、决策对话）
- **预期结果**：receipt 通过 schema 校验，outputs 为空列表，claims ≥1 条（记录讨论达成的决策或结论），decisions 字段 ≥1 条（记录选了什么、拒绝了什么、为什么）
- **验证方式**：构造一个 outputs=[] 且 claims=["达成 Phase 2 切入点共识"] 且 decisions 非空的 receipt，确认通过 Pydantic 校验；verifier 能针对 claims 检查 MEMORY.md 或 progress.md 中是否记录了对应决策

**AC-P0-002：Verifier 隔离性**
- **条件**：session-verifier sub-agent 被 spawn 时
- **预期结果**：verifier 的输入仅为 receipt YAML 路径 + 产出物路径，不包含任何 session 对话内容。verifier agent 定义中 tools 列表不包含获取 session history 的能力。
- **验证方式**：审查 session-verifier.md agent 定义，确认无 session 访问能力

**AC-P0-003：Verifier 判定完整性**
- **条件**：verifier 收到含 N 个 claims 的 receipt
- **预期结果**：verdict 包含恰好 N 个 results，每个 result 有 verdict（PASS/FAIL/UNVERIFIABLE）和 evidence 字段，summary.total = N
- **验证方式**：构造含 3 个 claims（1 可验证真、1 可验证假、1 不可验证）的 receipt，检查 verdict 输出

**AC-P0-004：/wrap 集成不中断现有流程**
- **条件**：agent 执行包含 receipt + verifier 的 /wrap 流程
- **预期结果**：progress.md 仍被正确写入，git commit 仍执行，/wrap 总耗时增加 ≤3 分钟
- **验证方式**：在 Ken 的真实 session 上执行完整 /wrap，验证 progress.md 和 git commit 正常

**AC-P0-006：Profile 匹配优先于 ad_hoc**
- **条件**：claim 文本匹配某个 verification profile 的 claim_patterns
- **预期结果**：verdict 中该 claim 的 `verification_method` 为 `"profile:<profile_id>"`（非 `"ad_hoc"`），且验证使用了 profile 定义的命令/检查方式
- **验证方式**：构造 claim "157 tests passed" + test-pass profile，检查 verdict 中 verification_method == "profile:test-pass"

**AC-P0-007：无 profile 匹配时 fallback 到 ad_hoc**
- **条件**：claim 文本不匹配任何 verification profile
- **预期结果**：verdict 中该 claim 的 `verification_method` 为 `"ad_hoc"`
- **验证方式**：构造 claim "达成了 Phase 2 切入点共识"（无 profile 匹配），检查 verdict 中 verification_method == "ad_hoc"

### P1功能验收标准

**AC-P1-001：FAIL 自动创建 work item**
- **条件**：verdict 包含 ≥1 个 FAIL 结果
- **预期结果**：`harness create-from-verdict` 创建 1 个 work item，acceptance_criteria 包含所有 FAIL 项的 action_items
- **验证方式**：构造含 FAIL 的 verdict，执行命令，检查 `.harness/work-items/` 下新增文件

---

## 4️⃣ 非功能需求（Non-Functional Requirements）

### 性能需求

- **receipt 生成耗时**：主 agent 生成 receipt ≤30 秒（提取+写文件）
- **verifier 耗时**：sub-agent 验证 ≤2 分钟（含脚本执行）
- **总 /wrap 增加耗时**：≤3 分钟

### 可维护性需求

- **代码规范**：遵循 project-standards.md（PEP 8 + ruff + mypy strict）
- **测试覆盖率**：schema 校验 ≥90%，CLI 命令 ≥70%
- **文档**：公开函数有 docstring（Google style）

### 安全需求

- **verifier 隔离**：session-verifier agent 不能访问 session 对话内容
- **不泄露敏感信息**：receipt 中不记录 API 密钥、用户凭据等敏感数据

---

## 5️⃣ 约束条件（Constraints）

### 技术约束

- **技术栈**：遵循 project-architecture.md——Python 3.11 + Pydantic + PyYAML + Click
- **OC sub-agent 机制**：verifier 定义为 `.claude/agents/session-verifier.md`，通过 OC ACP spawn
- **存储**：receipt 和 verdict 为 YAML 文件，存在各 OA 的 `memo/receipts/` 下

### 时间约束

- **P0 开发周期**：1 周
- **P1 开发周期**：P0 验证后 2-3 天

### 资源约束

- **开发**：Ken 主导，sub-agent 协助实现
- **运行**：extractor + verifier 均用 sonnet 模型（每次 /wrap 增加 2 次 sub-agent spawn）

---

## 6️⃣ 排除范围（Out of Scope）

- ❌ 不直接访问 OC session 存储——通过 sessions_history + attachment 传递对话原文给独立 extractor
- ❌ 不做 Web UI 或 dashboard——通过 CLI 和 YAML 文件查看
- ❌ 不跨 OC 环境（不支持云端 agent 的 receipt/verdict）
- ❌ 不修改现有 harness 状态机（WorkItem / HandoffContract 不变）
- ❌ 不自动推广到所有 OA——先在 Ken 上验证，手动推广
- ❌ 不做 receipt 的自动合规审计（对照 Decision Tree 检查路由合理性）——P2 或后续

---
