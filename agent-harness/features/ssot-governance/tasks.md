---
feature_name: "ssot-governance"
version: "0.0.1"
created: "2026-04-16T12:00:00+0800"
modified: "2026-04-16T12:00:00+0800"
upstream_dependency:
  - "features/ssot-governance/design.md"
  - "features/ssot-governance/requirements.md"
tags: [tasks, l2-spec, tdd, ssot, governance]
---

# ssot-governance - 任务拆分文档

> **任务概述**：基于 design.md 实现 SSOT 治理模块的模型层、引擎层和 CLI 层，为全局 SSOT 提供机械化漂移检测能力。
>
> **关联文档**：
> - L2 技术设计：`features/ssot-governance/design.md`
> - L2 功能需求：`features/ssot-governance/requirements.md`
> - TDD 测试文件：`tests/test_ssot.py`、`tests/test_ssot_engine.py`、`tests/test_cli_ssot.py`

---

## 1. 任务拆分（Task Breakdown）

### P0 任务（MVP 必需）

| 任务 ID | 任务名称 | 预估工时 | 依赖任务 | 负责人 | 状态 |
|---------|---------|---------|---------|--------|------|
| P0-001 | Registry 模型（SSOTRegistry + SSOTEntry + AgentDecl + Projection） | 4h | 无 | Ken | 待开始 |
| P0-002 | DriftReport 模型（DriftFinding + DriftSummary + DriftReport） | 3h | 无 | Ken | 待开始 |
| P0-003 | Registry 加载与校验（load_registry + 引用完整性 + 环检测） | 4h | P0-001 | Ken | 待开始 |
| P0-004 | Static check 引擎（S1-S5） | 5h | P0-001, P0-002 | Ken | 待开始 |
| P0-005 | Content check 引擎（C1-C2） | 4h | P0-001, P0-002 | Ken | 待开始 |
| P0-006 | 引擎编排（check_all + check_agent + check_entry + verdict） | 4h | P0-004, P0-005 | Ken | 待开始 |
| P0-007 | CLI 命令（ssot check） | 4h | P0-003, P0-006 | Ken | 待开始 |
| P0-008 | 端到端验证（真实 registry + 真实 workspace） | 3h | P0-007 | Ken | 待开始 |

### P1 任务（增强功能）

| 任务 ID | 任务名称 | 预估工时 | 依赖任务 | 负责人 | 状态 |
|---------|---------|---------|---------|--------|------|
| P1-001 | CLI 命令（ssot repair） | 4h | P0-007 | Ken | 待开始 |
| P1-002 | Content check — Agent upstream 一致性（C3） | 2h | P0-006 | Ken | 待开始 |
| P1-003 | DriftReport 持久化（ssot check --save + ssot history） | 3h | P0-007 | Ken | 待开始 |

---

## 2. 任务详情（Task Details）

### P0-001：Registry 模型

**任务描述**：
- 基于 design.md 的 Model-001，在 `harness/ssot.py` 中实现 Registry 模型
- 实现以下 Pydantic 模型：`ProjectionMethod`（enum）、`SSOTType`（enum）、`Projection`、`SSOTEntry`、`AgentDecl`、`SSOTRegistry`
- `SSOTRegistry` 包含 `schema_version`、`repo_root`、`entries`（dict）、`agents`（dict）
- 暂不实现 model_validator（P0-003 做）

**输入**：
- design.md 的 Model-001 定义

**输出**：
- `harness/ssot.py`：模型定义
- `tests/test_ssot.py`：模型测试

**验证标准（TDD 要求）**：
- [ ] **Unit Test-001**：SSOTRegistry 合法 YAML 反序列化成功（含 entries + agents）
- [ ] **Unit Test-002**：SSOTEntry 各 type（package/document）正确解析
- [ ] **Unit Test-003**：Projection 各 method（symlink/reference/embed）正确解析
- [ ] **Unit Test-004**：AgentDecl 含 depends_on 列表正确解析
- [ ] **Unit Test-005**：缺必填字段时 Pydantic 校验拒绝
- [ ] **Unit Test-006**：type 和 method 非法值时拒绝

**完成标准**：
- [ ] 所有模型类已实现，字段类型与 design.md 一致
- [ ] Unit Tests 覆盖率 >=90%

---

### P0-002：DriftReport 模型

**任务描述**：
- 基于 design.md 的 Model-002，在 `harness/ssot.py` 中追加 DriftReport 模型
- 实现：`DriftSeverity`（enum）、`DriftVerdict`（enum）、`CheckLayer`（enum）、`DriftFinding`、`DriftSummary`、`DriftReport`
- 实现 `_compute_verdict(findings)` 工具函数
- 实现序列化函数：`drift_report_to_dict(report)` → 可 JSON/YAML 序列化的 dict

**输入**：
- design.md 的 Model-002 定义
- audit.md 的严重度和 verdict 定义

**输出**：
- `harness/ssot.py`：追加模型 + 序列化函数
- `tests/test_ssot.py`：追加模型测试

**验证标准（TDD 要求）**：
- [ ] **Unit Test-007**：DriftFinding 完整字段构造成功
- [ ] **Unit Test-008**：DriftSummary 计数正确
- [ ] **Unit Test-009**：DriftReport 序列化/反序列化往返一致
- [ ] **Unit Test-010**：_compute_verdict — 无 findings → pass
- [ ] **Unit Test-011**：_compute_verdict — 仅 minor → pass_with_findings
- [ ] **Unit Test-012**：_compute_verdict — 存在 major → pass_with_findings
- [ ] **Unit Test-013**：_compute_verdict — 存在 critical → fail
- [ ] **Unit Test-014**：drift_report_to_dict 输出可被 json.dumps 序列化

**完成标准**：
- [ ] 所有模型和工具函数已实现
- [ ] verdict 判定规则与 design.md 一致
- [ ] Unit Tests 覆盖率 >=90%

---

### P0-003：Registry 加载与校验

**任务描述**：
- 在 `harness/ssot.py` 中实现 `load_registry(path)` 函数
- 实现 `SSOTRegistry.validate_references()` model_validator：
  (1) agents[*].depends_on 中每个 id 在 entries 中存在
  (2) entries[*].upstream 中每个 id 在 entries 中存在
  (3) upstream 图无环（拓扑排序或 DFS 检测）
- 定义 `RegistryValidationError` 异常，携带具体的错误信息

**输入**：
- P0-001 的模型
- design.md 的 load_registry 接口定义

**输出**：
- `harness/ssot.py`：load_registry + model_validator + 异常类
- `tests/test_ssot.py`：追加加载与校验测试

**验证标准（TDD 要求）**：
- [ ] **Unit Test-015**：合法 YAML 文件加载成功
- [ ] **Unit Test-016**：文件不存在 → FileNotFoundError
- [ ] **Unit Test-017**：YAML 语法错误 → 明确错误信息
- [ ] **Unit Test-018**：agents.depends_on 引用不存在的 entry → RegistryValidationError
- [ ] **Unit Test-019**：entries.upstream 引用不存在的 entry → RegistryValidationError
- [ ] **Unit Test-020**：循环依赖（A→B→A）→ RegistryValidationError，信息含环路径
- [ ] **Unit Test-021**：三层依赖（A→B→C）合法，无误报

**完成标准**：
- [ ] load_registry 可靠处理合法和非法输入
- [ ] 环检测算法正确
- [ ] 错误信息对人类可读

---

### P0-004：Static check 引擎（S1-S5）

**任务描述**：
- 在 `harness/ssot_engine.py` 中实现 5 个 static check 函数：
  - `_check_entry_exists(entry, repo_root)` → S1
  - `_check_agent_workspace(agent, repo_root)` → S2
  - `_check_agent_identity(agent, repo_root)` → S3
  - `_check_minimum_fields(workspace_path)` → S4 兼容入口（已退役，返回空列表）
  - `_check_projections_exist(entry, repo_root)` → S5
- 组合为 `_check_entry_static(entry, repo_root)` 和 `_check_agent_static(agent, repo_root)`

**输入**：
- P0-001 的模型
- P0-002 的 DriftFinding 模型
- design.md 的 S4 退役规则

**输出**：
- `harness/ssot_engine.py`：static check 函数
- `tests/test_ssot_engine.py`：引擎测试

**验证标准（TDD 要求）**：
- [ ] **Unit Test-022**：S1 — entry path 存在（package=dir）→ 无 finding
- [ ] **Unit Test-023**：S1 — entry path 不存在 → DriftFinding(S1, critical)
- [ ] **Unit Test-024**：S1 — document 文件存在但为空 → DriftFinding(S1, critical)
- [ ] **Unit Test-025**：S2 — workspace 目录存在 → 无 finding
- [ ] **Unit Test-026**：S2 — workspace 不存在 → DriftFinding(S2, critical)
- [ ] **Unit Test-027**：S3 — identity 文件存在且非空 → 无 finding
- [ ] **Unit Test-028**：S3 — identity 不存在 → DriftFinding(S3, critical)
- [ ] **Unit Test-029**：S4 — 启发式名称保留为兼容常量，但不强制
- [ ] **Unit Test-030**：S4 — 缺启发式锚点 → 无 DriftFinding
- [ ] **Unit Test-031**：S4 — 中文历史锚点不影响结果
- [ ] **Unit Test-032**：S4 — 大小写历史锚点不影响结果
- [ ] **Unit Test-033**：S5 — projection target 存在 → 无 finding
- [ ] **Unit Test-034**：S5 — projection target 不存在 → DriftFinding(S5, major)

**完成标准**：
- [ ] static check 函数实现完整；S4 不做程序化裁决
- [ ] MINIMUM_GOVERNANCE_DIMENSIONS 仅作历史兼容，不参与 pass/fail
- [ ] ssot_engine 覆盖率 >=95%

---

### P0-005：Content check 引擎（C1-C2）

**任务描述**：
- 在 `harness/ssot_engine.py` 中追加 2 个 content check 函数：
  - `_check_symlink_integrity(projection, entry, repo_root)` → C1
  - `_check_content_hash(projection, entry, repo_root)` → C2
- C1：对 method=symlink 的 projection，`os.readlink()` 获取实际 target，比较是否指向 SSOT 源路径
- C2：对 method=reference/embed 的 projection，计算源和目标的 SHA-256 哈希，不一致时报告
- 组合为 `_check_entry_content(entry, repo_root)`

**输入**：
- P0-001 的模型
- P0-002 的 DriftFinding 模型

**输出**：
- `harness/ssot_engine.py`：追加 content check 函数
- `tests/test_ssot_engine.py`：追加测试

**验证标准（TDD 要求）**：
- [ ] **Unit Test-035**：C1 — symlink 指向正确源 → 无 finding
- [ ] **Unit Test-036**：C1 — symlink 断链 → DriftFinding(C1, major)
- [ ] **Unit Test-037**：C1 — symlink 指向错误源 → DriftFinding(C1, major)，evidence 含实际 target
- [ ] **Unit Test-038**：C2 — 内容一致 → 无 finding
- [ ] **Unit Test-039**：C2 — 内容不一致 → DriftFinding(C2, major)，evidence 含两者哈希
- [ ] **Unit Test-040**：C1/C2 — method 不匹配（非 symlink 调 C1）→ 跳过

**完成标准**：
- [ ] 两个 content check 函数实现完整
- [ ] 哈希使用 SHA-256
- [ ] ssot_engine 覆盖率 >=95%

---

### P0-006：引擎编排（check_all + check_agent + check_entry + verdict）

**任务描述**：
- 在 `harness/ssot_engine.py` 中实现 3 个公开编排函数：
  - `check_all(registry, repo_root, mode)` — 遍历 entries + agents，汇总 findings，计算 verdict
  - `check_agent(registry, agent_id, repo_root, mode)` — 检查特定 agent + 其依赖 entries
  - `check_entry(registry, entry_id, repo_root)` — 检查特定 entry + projections
- mode="quick" 时只调 static checks，mode="full" 时调 static + content checks
- 调用 P0-002 的 `_compute_verdict()` 计算 verdict
- 构建 DriftReport

**输入**：
- P0-003 的 load_registry
- P0-004 的 static checks
- P0-005 的 content checks
- P0-002 的 verdict 计算

**输出**：
- `harness/ssot_engine.py`：追加编排函数
- `tests/test_ssot_engine.py`：追加集成测试

**验证标准（TDD 要求）**：
- [ ] **Integration Test-001**：check_all(full) — 构造 2 entries + 2 agents，验证 findings 包含 S+C checks
- [ ] **Integration Test-002**：check_all(quick) — 验证不含 C-checks findings
- [ ] **Integration Test-003**：check_agent — 只包含该 agent 和其依赖 entries 的 findings
- [ ] **Integration Test-004**：check_agent(quick) — 只包含 S-checks
- [ ] **Integration Test-005**：check_entry — 只包含该 entry + projections 的 findings
- [ ] **Integration Test-006**：agent_id 不存在 → KeyError
- [ ] **Integration Test-007**：DriftReport.scope 正确设置（"all" / "agent:duke" / "entry:agent-constitution"）

**完成标准**：
- [ ] 三个编排函数实现完整
- [ ] quick/full 模式行为一致于 design.md
- [ ] DriftReport 结构正确

---

### P0-007：CLI 命令（ssot check）

**任务描述**：
- 在 `harness/cli.py` 中新增 `ssot` 子命令组，实现 `ssot check` 命令
- 支持参数：`--agent`、`--entry`、`--quick`、`--json`、`--registry`
- `--agent` 和 `--entry` 互斥
- 默认 registry 路径逻辑：`--registry` > `$SSOT_REGISTRY` 环境变量 > `$HARNESS_ROOT/../ssot/registry.yaml`
- 人类可读输出格式对齐 design.md Command-001 示例
- JSON 输出：调用 `drift_report_to_dict()` + `json.dumps()`
- 退出码：0/1/2

**输入**：
- P0-003 的 load_registry
- P0-006 的编排函数
- P0-002 的序列化函数
- design.md 的 Command-001 定义

**输出**：
- `harness/cli.py`：追加 ssot 子命令组
- `tests/test_cli_ssot.py`：CLI 集成测试

**验证标准（TDD 要求）**：
- [ ] **Integration Test-008**：全量检查，退出码 0（全 pass）
- [ ] **Integration Test-009**：全量检查，退出码 1（有 findings）
- [ ] **Integration Test-010**：registry 不存在，退出码 2
- [ ] **Integration Test-011**：`--agent duke` 正确路由到 check_agent
- [ ] **Integration Test-012**：`--entry agent-constitution` 正确路由到 check_entry
- [ ] **Integration Test-013**：`--quick` 跳过 C-checks
- [ ] **Integration Test-014**：`--json` 输出可被 json.loads 解析
- [ ] **Integration Test-015**：`--agent` 和 `--entry` 同时指定 → 错误提示
- [ ] **Integration Test-016**：`--agent` 指定不存在的 id → 退出码 2

**完成标准**：
- [ ] CLI 覆盖率 >=70%
- [ ] 退出码约定严格遵守
- [ ] 人类可读输出清晰可扫

---

### P0-008：端到端验证

**任务描述**：
- 使用真实的 `ssot/agent-constitution/` 和真实的 Agent workspace，构建一份初始 `registry.yaml`
- 执行 `harness ssot check` 全量检查，验证：
  (1) agent-constitution entry → PASS
  (2) 不因 WS 治理启发式标题缺失生成 S4 finding
  (3) DriftReport verdict 和 summary 正确
  (4) JSON 输出可解析
- 执行 `harness ssot check --agent duke --quick`，验证 quick 模式正常
- 不提交 registry.yaml 到仓库（那是行动项 #1），只在测试中使用

**输入**：
- 真实的 ssot/ 目录和 Agent workspace
- P0-007 的 CLI

**输出**：
- 测试证据：至少一次全量检查的 DriftReport
- `tests/test_cli_ssot.py`：追加 E2E 测试

**验证标准（TDD 要求）**：
- [ ] **E2E Test-001**：全量检查输出 DriftReport，agent-constitution entry PASS
- [ ] **E2E Test-002**：workspace 启发式标题缺失不产生 S4 finding
- [ ] **E2E Test-003**：quick 模式正常返回且不含 C-checks
- [ ] **E2E Test-004**：JSON 输出可被 json.loads 解析且结构与 DriftReport 一致

**完成标准**：
- [ ] 至少 1 次真实 workspace 端到端验证通过
- [ ] 检测结果与人工核查一致

---

## 3. 任务依赖（Task Dependencies）

### 依赖关系图

```
P0-001 (Registry 模型) ──┬──→ P0-003 (加载+校验) ──→ P0-007 (CLI) ──→ P0-008 (E2E)
                         │                                  ↑
P0-002 (DriftReport 模型)┼──→ P0-004 (S-checks) ──┐       │
                         │                         ├→ P0-006 (编排) ─┘
                         └──→ P0-005 (C-checks) ──┘
```

P0-001 和 P0-002 无依赖可并行。P0-004 和 P0-005 依赖两者但彼此独立可并行。P0-006 等待 P0-004+P0-005。P0-007 等待 P0-003+P0-006。P0-008 最后执行。

---

## 4. 完成标准与执行顺序

### TDD 测试金字塔

65% Unit（模型+引擎）/ 25% Integration（CLI+编排）/ 10% E2E（真实 workspace）

### 覆盖率要求

ssot.py >=90% / ssot_engine.py >=95% / cli.py(ssot 部分) >=70% / 整体 >=80%

代码规范：PEP 8 + ruff + mypy strict。公开函数有 docstring（Google style）。

### 执行顺序

1. 模型层（P0-001 + P0-002 并行）
2. 加载校验（P0-003）+ 引擎层（P0-004 + P0-005 并行）
3. 编排层（P0-006）
4. CLI（P0-007）
5. 端到端验证（P0-008）

---

## 5. 风险识别（Risk Identification）

| 风险 | 概率 | 影响 | 缓解 |
|------|:---:|------|------|
| 把启发式误作审核项 | 中 | Harness 把标题缺失误判为治理缺失 | S4 退役；治理启发式交给 Duke/Amanda/Reeve 人工使用 |
| Agent workspace 路径不统一 | 低 | 路径硬编码在 registry 中，workspace 移动后失效 | registry 声明为相对路径，移动后更新 registry |
| registry.yaml 维护负担 | 中 | 新增 Agent 后忘记更新 registry | P2 考虑 `harness ssot init` 自动生成；v0.1 靠 HEARTBEAT 提醒 |
| 启发式证据分散 | 低 | 人工审查需要跨文件理解 | Amanda 在配方层提供支架；Reeve 后验审查运行效果 |

---
