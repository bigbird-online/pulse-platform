---
feature_name: "runtime-guardrail"
version: "0.0.1"
created: "2026-04-06T12:50:00+0800"
modified: "2026-04-06T12:50:00+0800"
upstream_dependency: ["design.md", "requirements.md"]
tags: [tasks, l2-spec, tdd, guardrail, constraint]
---

# runtime-guardrail - 任务拆分文档

> **任务概述**：基于 design.md 实现 runtime-guardrail 的模型层、引擎层、CLI 层、Agent 定义层和实例层，为 Emi 提供完整的运行时护栏能力。
>
> **关联文档**：
> - L2技术设计：`features/runtime-guardrail/design.md`
> - L2功能需求：`features/runtime-guardrail/requirements.md`
> - TDD测试文件：`tests/test_guardrail.py`、`tests/test_gate_engine.py`、`tests/test_compliance_engine.py`、`tests/test_cli_guardrail.py`

---

## 1️⃣ 任务拆分（Task Breakdown）

### P0任务（MVP必需）

| 任务ID | 任务名称 | 预估工时 | 依赖任务 | 负责人 | 状态 |
|--------|---------|---------|---------|--------|------|
| P0-001 | GuardrailProfile + GateResult 模型 | 4h | 无 | Ken | 待开始 |
| P0-002 | BehavioralFact + ComplianceVerdict 模型 | 4h | 无 | Ken | 待开始 |
| P0-003 | Gate check 引擎 | 4h | P0-001 | Ken | 待开始 |
| P0-004 | Compliance check 引擎 | 6h | P0-001, P0-002 | Ken | 待开始 |
| P0-005 | CLI 命令（gate check + compliance check） | 4h | P0-003, P0-004 | Ken | 待开始 |
| P0-006 | Extractor 执行规则与模版 | 4h | P0-002 | Ken | 待开始 |
| P0-007 | Verifier 执行规则与模版 | 4h | P0-001, P0-002 | Ken | 待开始 |
| P0-008 | Emi 护栏 Profiles 实例化 | 3h | P0-001 | Ken | 待开始 |
| P0-009 | /wrap 集成 + 端到端验证 | 6h | P0-005, P0-006, P0-007, P0-008 | Ken | 待开始 |

### P1任务（增强功能）

| 任务ID | 任务名称 | 预估工时 | 依赖任务 | 负责人 | 状态 |
|--------|---------|---------|---------|--------|------|
| P1-001 | compliance create-work-item CLI | 3h | P0-005 | Ken | 待开始 |
| P1-002 | HEARTBEAT 合规巡检 | 2h | P0-009 | Ken | 待开始 |

### P2任务（锦上添花）

| 任务ID | 任务名称 | 预估工时 | 依赖任务 | 负责人 | 状态 |
|--------|---------|---------|---------|--------|------|
| P2-001 | 护栏模版库 | 3h | P0-008 | Ken | 待开始 |
| P2-002 | 合规趋势统计 CLI | 4h | P0-009 | Ken | 待开始 |

---

## 2️⃣ 任务详情（Task Details）

### P0-001：GuardrailProfile + GateResult 模型

**任务描述**：
- 基于 design.md 的"4️⃣ 数据模型/Model-001、Model-002"，在 `harness/guardrail.py` 中实现 GuardrailProfile（含 GuardrailType enum、CheckAction）和 GateResult（含 GateVerdict enum）Pydantic 模型
- 实现 `load_guardrails(directory, type_filter)` 函数，从目录加载 YAML 文件并反序列化为模型列表
- 实现 `save_gate_result(result, directory)` 序列化函数

**输入**：
- design.md 的 Model-001（GuardrailProfile）和 Model-002（GateResult）定义
- project-architecture.md 的技术栈（Pydantic + PyYAML）

**输出**：
- `harness/guardrail.py`：模型定义 + 加载/序列化函数
- `tests/test_guardrail.py`：模型测试

**验证标准（TDD要求）**：
- [ ] **Unit Test-001**：GuardrailProfile 合法 YAML 反序列化成功（gate 类型 + behavioral 类型各 1 例）
- [ ] **Unit Test-002**：GuardrailProfile 非法 YAML 被 Pydantic 拒绝（缺必填字段、type 非法值）
- [ ] **Unit Test-003**：GateResult 序列化/反序列化往返一致
- [ ] **Unit Test-004**：load_guardrails 从目录加载多个 YAML，按文件名排序
- [ ] **Unit Test-005**：load_guardrails 的 type_filter 过滤功能（只加载 gate / 只加载 behavioral）
- [ ] **Unit Test-006**：load_guardrails 空目录返回空列表，非 YAML 文件被忽略

**完成标准**：
- [ ] 模型类已实现，字段类型和约束与 design.md 一致
- [ ] 序列化/反序列化函数已实现
- [ ] Unit Tests 覆盖率 ≥90%

---

### P0-002：BehavioralFact + ComplianceVerdict 模型

**任务描述**：
- 基于 design.md 的"4️⃣ 数据模型/Model-003、Model-004"，在 `harness/guardrail.py` 中追加 BehavioralFact（含 SpawnRecord、FileOperation、UserConfirmation、FactDecision）和 ComplianceVerdict（含 ComplianceResult enum、GuardrailResult、ComplianceSummary）模型
- 实现 `save_facts(facts, directory)` 和 `load_facts(path)` 序列化函数
- 实现 `save_compliance_verdict(verdict, directory)` 和 `load_compliance_verdict(path)` 序列化函数

**输入**：
- design.md 的 Model-003（BehavioralFact）和 Model-004（ComplianceVerdict）定义

**输出**：
- `harness/guardrail.py`：追加模型 + 序列化函数
- `tests/test_guardrail.py`：追加模型测试

**验证标准（TDD要求）**：
- [ ] **Unit Test-007**：BehavioralFact 完整 YAML 反序列化（含 spawns、file_operations、user_confirmations、decisions）
- [ ] **Unit Test-008**：BehavioralFact 空列表字段合法（spawns=[], file_operations=[] 等）
- [ ] **Unit Test-009**：ComplianceVerdict 序列化/反序列化往返一致
- [ ] **Unit Test-010**：ComplianceSummary 计数正确（total = compliant + violated + unverifiable）
- [ ] **Unit Test-011**：save_facts 生成正确的文件名和 YAML 内容
- [ ] **Unit Test-012**：load_facts 文件不存在时抛 FileNotFoundError

**完成标准**：
- [ ] 所有子模型和序列化函数已实现
- [ ] Unit Tests 覆盖率 ≥90%

---

### P0-003：Gate check 引擎

**任务描述**：
- 基于 design.md 的"3️⃣ 接口定义/Function-002"和"5️⃣ 数据流/Flow-001"，在 `harness/gate_engine.py` 中实现 `run_gate_check(guardrail, context)` 函数
- 支持 check.type = "file_exists"：检查 paths 中的文件是否存在且非空，支持 `{placeholder}` 替换
- 支持 check.type = "command"：执行 shell 命令，根据退出码判断 PASS/FAIL
- 占位符未替换时抛出明确错误

**输入**：
- design.md 的 Function-002（run_gate_check）签名和数据流 Flow-001
- P0-001 的 GuardrailProfile 和 GateResult 模型

**输出**：
- `harness/gate_engine.py`：gate check 引擎
- `tests/test_gate_engine.py`：引擎测试

**验证标准（TDD要求）**：
- [ ] **Unit Test-013**：file_exists 类型，所有文件存在且非空 → PASS
- [ ] **Unit Test-014**：file_exists 类型，某文件缺失 → FAIL，missing_items 包含缺失文件
- [ ] **Unit Test-015**：file_exists 类型，文件存在但为空 → FAIL
- [ ] **Unit Test-016**：占位符正确替换（`{experiment_dir}` → 实际路径）
- [ ] **Unit Test-017**：占位符未提供 → ValueError 明确提示缺失的 key
- [ ] **Unit Test-018**：command 类型，退出码 0 → PASS
- [ ] **Unit Test-019**：command 类型，退出码非 0 → FAIL
- [ ] **Unit Test-020**：guardrail.type != "gate" → ValueError
- [ ] **Integration Test-001**：端到端 gate check（加载 YAML → 执行检查 → 返回 GateResult）

**完成标准**：
- [ ] gate_engine 覆盖率 ≥95%
- [ ] 所有 check.type 实现

---

### P0-004：Compliance check 引擎

**任务描述**：
- 基于 design.md 的"3️⃣ 接口定义/Function-003"，在 `harness/compliance_engine.py` 中实现 `run_compliance_check(facts, guardrails)` 函数
- 对每条 behavioral 类型护栏，从 BehavioralFact 中匹配对应行为记录
- 支持 check.type = "pattern_match"：在 facts 的指定 field 中搜索符合 pattern 的记录
- 支持 check.type = "temporal_check"：比较 file_operations 和 user_confirmations 的时序关系
- 无法判定时返回 UNVERIFIABLE
- 自动计算 ComplianceSummary 并提取 action_items

**输入**：
- design.md 的 Function-003（run_compliance_check）签名
- P0-001 的 GuardrailProfile 模型、P0-002 的 BehavioralFact 和 ComplianceVerdict 模型

**输出**：
- `harness/compliance_engine.py`：compliance check 引擎
- `tests/test_compliance_engine.py`：引擎测试

**验证标准（TDD要求）**：
- [ ] **Unit Test-021**：pattern_match 类型，facts 中有匹配记录 → COMPLIANT
- [ ] **Unit Test-022**：pattern_match 类型，facts 中无匹配记录 → VIOLATED
- [ ] **Unit Test-023**：temporal_check 类型，确认时间早于落盘时间 → COMPLIANT
- [ ] **Unit Test-024**：temporal_check 类型，落盘时间早于确认时间 → VIOLATED
- [ ] **Unit Test-025**：facts 中缺少相关字段 → UNVERIFIABLE
- [ ] **Unit Test-026**：summary 计数正确（自动汇总 compliant/violated/unverifiable）
- [ ] **Unit Test-027**：action_items 从 VIOLATED 结果中正确提取
- [ ] **Unit Test-028**：guardrail.type != "behavioral" → 跳过该护栏
- [ ] **Integration Test-002**：端到端 compliance check（5 条 Emi 护栏 + mock facts → ComplianceVerdict）

**完成标准**：
- [ ] compliance_engine 覆盖率 ≥80%
- [ ] 所有 check.type 实现

---

### P0-005：CLI 命令（gate check + compliance check）

**任务描述**：
- 基于 design.md 的"3️⃣ 接口定义/Command-001、Command-002"，在 `harness/cli.py` 中新增 `gate` 和 `compliance` 子命令组
- `harness gate check --agent --gate [--context ...]`：调用 gate_engine，输出人类可读结果
- `harness compliance check --agent --facts`：调用 compliance_engine，输出逐条结果 + 汇总
- 退出码约定：0=通过，1=有违规，2=系统错误

**输入**：
- design.md 的 Command-001 和 Command-002 定义
- P0-003 的 gate_engine 和 P0-004 的 compliance_engine

**输出**：
- `harness/cli.py`：扩展 CLI
- `tests/test_cli_guardrail.py`：CLI 集成测试

**验证标准（TDD要求）**：
- [ ] **Integration Test-003**：`harness gate check` 正常 PASS 场景，退出码 0
- [ ] **Integration Test-004**：`harness gate check` FAIL 场景，退出码 1，输出包含缺失项
- [ ] **Integration Test-005**：`harness gate check` 护栏不存在，退出码 2
- [ ] **Integration Test-006**：`harness compliance check` 全 COMPLIANT，退出码 0
- [ ] **Integration Test-007**：`harness compliance check` 有 VIOLATED，退出码 1
- [ ] **Integration Test-008**：`harness compliance check` facts 文件不存在，退出码 2
- [ ] **Integration Test-009**：`--context key=value` 参数正确传递到 gate_engine

**完成标准**：
- [ ] CLI 覆盖率 ≥70%
- [ ] 退出码约定严格遵守

---

### P0-006：Extractor 执行规则与模版

**任务描述**：
- 基于 requirements.md 的 FR-P0-006，为 session-extractor 编写 OC sub-agent 定义文件
- agent 定义包含：(1) 严格的 system prompt，定义提取的四类行为事实（spawns、file_operations、user_confirmations、decisions）；(2) 每类事实的提取规则和边界情况；(3) BehavioralFact YAML 输出模版（内嵌在 prompt 中）；(4) 禁止事项清单；(5) 具体的提取示例（正确示例 + 错误示例）
- tools 为空（extractor 不访问文件系统）

**输入**：
- requirements.md 的 FR-P0-006
- P0-002 的 BehavioralFact schema

**输出**：
- `.claude/agents/session-extractor.md`

**验证标准（TDD要求）**：
- [ ] **Manual-001**：审查 agent 定义，确认 tools 列表为空
- [ ] **Manual-002**：审查 system prompt，确认四类事实的提取规则完整
- [ ] **Manual-003**：审查 prompt 中包含 BehavioralFact YAML 模版
- [ ] **Manual-004**：构造一段 mock 对话，spawn extractor，检查输出符合 schema

**完成标准**：
- [ ] agent 定义文件完整，包含规则 + 模版 + 示例 + 禁止事项
- [ ] 至少 1 次 mock 对话端到端测试通过

---

### P0-007：Verifier 执行规则与模版

**任务描述**：
- 基于 requirements.md 的 FR-P0-007，为 session-verifier 编写 OC sub-agent 定义文件
- agent 定义包含：(1) 严格的 system prompt，定义按护栏 YAML 逐条验证的流程；(2) 每种 check.type 的验证方法说明；(3) ComplianceVerdict YAML 输出模版；(4) UNVERIFIABLE 的判定标准（何时可以标记为不可验证）；(5) 禁止事项清单；(6) 逐条验证的完整示例
- tools：Read, Bash, Grep（可读取 facts 和护栏文件，可执行验证命令）

**输入**：
- requirements.md 的 FR-P0-007
- P0-001 的 GuardrailProfile schema，P0-002 的 ComplianceVerdict schema

**输出**：
- `.claude/agents/session-verifier.md`

**验证标准（TDD要求）**：
- [ ] **Manual-005**：审查 agent 定义，确认 tools 包含 Read/Bash/Grep，不含 session 访问能力
- [ ] **Manual-006**：审查 system prompt，确认逐条验证流程完整
- [ ] **Manual-007**：审查 prompt 中包含 ComplianceVerdict YAML 模版
- [ ] **Manual-008**：构造 mock facts + 护栏，spawn verifier，检查输出符合 schema

**完成标准**：
- [ ] agent 定义文件完整，包含规则 + 模版 + 示例 + 禁止事项
- [ ] 至少 1 次 mock 端到端测试通过

---

### P0-008：Emi 护栏 Profiles 实例化

**任务描述**：
- 基于 requirements.md 的 FR-P0-008 和 ami-space 的 AGENTS.md / SOUL.md，创建 5 条护栏 YAML 文件
- 每条护栏包含：guardrail_id、agent=emi、type（gate 或 behavioral）、description、check（含 type + 条件）、pass_condition、failure_action、source_ref
- 文件存放在 `.harness/guardrails/emi/`

**输入**：
- ami-space/AGENTS.md 和 ami-space/SOUL.md 中的行为约束
- P0-001 的 GuardrailProfile schema

**输出**：
- `.harness/guardrails/emi/gate-s1-complete.yaml`
- `.harness/guardrails/emi/spawn-enforcement.yaml`
- `.harness/guardrails/emi/confirm-before-persist.yaml`
- `.harness/guardrails/emi/perfect-score-alert.yaml`
- `.harness/guardrails/emi/no-orchestrator-injection.yaml`

**验证标准（TDD要求）**：
- [ ] **Unit Test-029**：5 个 YAML 全部通过 GuardrailProfile Pydantic 校验
- [ ] **Unit Test-030**：gate-s1-complete 类型为 gate，其余 4 条类型为 behavioral
- [ ] **Unit Test-031**：所有 YAML 包含 source_ref 引用到 ami-space 的具体文件和行号

**完成标准**：
- [ ] 5 个文件已创建且 schema 合法
- [ ] 每条护栏的 check 条件可操作（不是模糊描述）

---

### P0-009：/wrap 集成 + 端到端验证

**任务描述**：
- 基于 requirements.md 的 FR-P0-009 和 design.md 的 Flow-002，修改 Emi 的 AGENTS.md `/wrap` 段
- 新增步骤：sessions_history → spawn extractor → 写 facts → spawn verifier → 写 verdict → 汇报
- 更新 `.harness/PROTOCOL.md`，追加护栏协议段
- 在 Emi 的真实或模拟 session 上执行完整 /wrap，验证全链路

**输入**：
- P0-005 CLI、P0-006 extractor、P0-007 verifier、P0-008 护栏 profiles

**输出**：
- 修改：`ami-space/AGENTS.md`（/wrap 段）
- 修改：`.harness/PROTOCOL.md`（追加护栏协议）
- 验证证据：一次完整的 facts + verdict 文件

**验证标准（TDD要求）**：
- [ ] **E2E Test-001**：完整 /wrap 流程产出 facts YAML + verdict YAML，两者通过 schema 校验
- [ ] **E2E Test-002**：verdict 包含 5 条结果，每条有明确的 verdict 和 evidence
- [ ] **E2E Test-003**：/wrap 现有步骤（progress.md、git commit）未被破坏
- [ ] **E2E Test-004**：gate check 在 checkpoint 场景可独立调用并返回正确结果

**完成标准**：
- [ ] AGENTS.md 和 PROTOCOL.md 已更新
- [ ] 至少 1 次端到端验证通过（facts + verdict 文件存在且合法）
- [ ] /wrap 总耗时增加 ≤3 分钟

---

### P1-001：compliance create-work-item CLI

基于 design.md Command-003，在 CLI 中新增 `harness compliance create-work-item`，解析 ComplianceVerdict 对 VIOLATED 项创建 WorkItem（复用 Phase 1 store）。

输出：`harness/cli.py` 追加命令 + `tests/test_cli_guardrail.py` 追加测试

验证：verdict 含 VIOLATED → 创建 work item(退出码0) / 无 VIOLATED → 不创建 / 文件不存在 → 退出码2

---

## 3️⃣ 任务依赖（Task Dependencies）

### 依赖关系图

```
P0-001 (models:guardrail+gate) ──┬──→ P0-003 (gate engine) ──┐
                                 │                            │
                                 ├──→ P0-008 (emi profiles)   │
                                 │                            ├──→ P0-005 (CLI) ──┐
P0-002 (models:fact+verdict) ──┬─┤                            │                   │
                               │ └──→ P0-004 (compliance eng) ┘                   │
                               │                                                   │
                               ├──→ P0-006 (extractor rules) ─────────────────────┤
                               │                                                   │
                               └──→ P0-007 (verifier rules) ──────────────────────┤
                                                                                   │
                                                                                   ▼
                                                                       P0-009 (/wrap + E2E)
                                                                                   │
                                                                                   ▼
                                                                       P1-001 (create-work-item)
                                                                                   │
                                                                                   ▼
                                                                       P1-002 (HEARTBEAT)
```

依赖见图，P0-001/002 无依赖可并行，P0-009 需所有前置组件就绪。

---

## 4️⃣ 完成标准与执行顺序

### TDD 测试金字塔

70% Unit（模型+引擎）/ 20% Integration（CLI+真实护栏）/ 10% E2E+Manual（agent 定义+/wrap 链路）

### 覆盖率要求

guardrail.py ≥90% / gate_engine.py ≥95% / compliance_engine.py ≥80% / cli.py ≥70% / 整体 ≥80%

代码规范：PEP 8 + ruff + mypy strict。公开函数有 docstring（Google style）。

### UAT 验收

- 大鸟在 Emi 真实场景调用 `harness gate check`，验证门控生效
- 大鸟审查 /wrap 产出的 facts + verdict，确认判定合理
- 确认 Emi 无法修改 `.harness/guardrails/emi/`

### 执行顺序

1. 模型层（P0-001+002 并行）→ 2. 引擎层（P0-003+004）→ 3. CLI+实例（P0-005+008 并行）→ 4. Agent 定义（P0-006+007 并行）→ 5. 集成验证（P0-009）→ 6. 增强（P1，MVP 后）

---

## 6️⃣ 风险识别（Risk Identification）

| 风险 | 概率 | 影响 | 缓解 |
|------|:---:|------|------|
| Extractor 提取质量不稳定 | 中 | 遗漏事实 → 误判 COMPLIANT | 规则含详尽示例；fallback 自提取；首次人工对比 |
| Verifier 判定主观性 | 中 | 同一 facts 不同次判定不一致 | check 条件具体可操作，减少主观空间 |
| sessions_history 截断 | 低 | 后半段事实缺失 | 标注 truncated=true；缺失项判 UNVERIFIABLE |
| Agent 定义调试耗时 | 中 | prompt 多轮迭代 | 先 mock 测试；P0-009 预留时间 |

---
