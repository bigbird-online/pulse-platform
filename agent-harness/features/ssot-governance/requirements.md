---
feature_name: "ssot-governance"
version: "0.0.1"
created: "2026-04-16T12:00:00+0800"
modified: "2026-04-16T12:00:00+0800"
upstream_dependency:
  - "project-specs/project-vision.md"
  - "project-specs/project-architecture.md"
  - "project-specs/project-standards.md"
  - "ssot/agent-constitution/governance.md"
  - "ssot/agent-constitution/audit.md"
  - "ssot/agent-constitution/glossary.md"
tags: [requirements, l2-spec, ssot, governance, drift-detection]
---

# ssot-governance - 功能需求文档

> **功能概述**：为 agent-harness 新增 SSOT 治理模块 — 通过 `registry.yaml` 声明全局 SSOT 依赖图谱，通过 `harness ssot check` 机械检测 Agent workspace 与 SSOT 之间的漂移，通过 `harness ssot repair` 自动修复可修复项，使 SSOT 一致性保障从"人工巡检"变为"代码强制"。
>
> **关联文档**：
> - L1 项目愿景：`project-specs/project-vision.md`
> - L1 项目架构：`project-specs/project-architecture.md`
> - L1 项目规范：`project-specs/project-standards.md`
> - L2 技术设计：`features/ssot-governance/design.md`
> - L2 任务拆分：`features/ssot-governance/tasks.md`
> - 上游治理规则：`ssot/agent-constitution/governance.md`（WS 治理启发式）
> - 上游审计方法：`ssot/agent-constitution/audit.md`（三层审计 + 严重度 + 结论）
> - 上游术语表：`ssot/agent-constitution/glossary.md`
> - 决策记录：`Bigbird/ken-space/memo/decision-2026-04-15-pulse-product-vision.md`

---

## 1. 用户故事（User Stories）

### 主要用户故事

**故事 1：全局 SSOT 注册与依赖声明**
- **作为**：大鸟（系统所有者）
- **我希望**：在 `ssot/registry.yaml` 中声明所有全局 SSOT 文档的路径、owner、类型和上下游依赖关系，以及所有 Agent 的工作区和身份文件位置
- **以便**：一个文件看到全系统的 SSOT 拓扑，不需要在 11 个 Agent 的 workspace 里逐个翻找
- **优先级**：P0

**故事 2：Agent workspace 漂移自动检测**
- **作为**：大鸟
- **我希望**：运行 `harness ssot check --agent duke` 时，自动检查 Duke 的 workspace 是否存在、identity 是否存在、所依赖的 SSOT 文件是否存在且一致
- **以便**：快速发现结构性漂移；治理启发式由 Duke/Amanda/Reeve 在设计、配方和审查中人工使用
- **优先级**：P0

**故事 3：全量漂移扫描**
- **作为**：大鸟
- **我希望**：运行 `harness ssot check` 时，一次性检查所有 SSOT entry 和所有 Agent 的漂移状态，输出一份汇总报告（含严重度分级和修复建议）
- **以便**：月度回顾时快速获得系统级 SSOT 健康概览
- **优先级**：P0

**故事 4：Relay spawn 前轻量校验**
- **作为**：Relay（通信层）
- **我希望**：spawn Agent 前调用 `harness ssot check --agent <id> --quick --json`，在 5 秒内返回该 Agent 的静态检查结果（workspace 存在、identity 存在、依赖存在）
- **以便**：spawn 前知道 Agent 的基础配置是否完整，决定是否需要告警
- **优先级**：P0

**故事 5：漂移自动修复**
- **作为**：大鸟
- **我希望**：运行 `harness ssot repair` 时，自动修复可机械修复的漂移（如断链 symlink），不可自动修复的项输出修复建议
- **以便**：减少人工修复的步骤
- **优先级**：P1

**故事 6：SSOT entry 级别检查**
- **作为**：Amanda（算法 Agent，constitution owner）
- **我希望**：运行 `harness ssot check --entry agent-constitution` 时，检查该 SSOT entry 的文件完整性和所有下游 projection 的一致性
- **以便**：修改宪法后快速确认所有下游引用是否同步
- **优先级**：P1

---

## 2. 功能需求（Functional Requirements）

### P0 功能（MVP 必需）

**FR-P0-001：Registry 模型定义**
- **描述**：定义 `SSOTRegistry` 的 Pydantic schema，包含两部分：(1) `entries` — 声明全局 SSOT 文档的 id、路径、owner、类型（package/document）、描述、上游依赖、下游投影（projection）；(2) `agents` — 声明每个 Agent 的 id、workspace 路径、identity 文件路径、所依赖的 SSOT entries。所有路径相对于仓库根目录。
- **来源**：故事 1
- **示例**：
  ```yaml
  # ssot/registry.yaml
  schema_version: "1.0"
  repo_root: "/Users/owner/Coevolution"

  entries:
    agent-constitution:
      path: ssot/agent-constitution/
      owner: amanda
      type: package
      description: "Agent governance: constitution, rules, schemas, audit, glossary"
      upstream: []
      projections: []

    agent-roster:
      path: ssot/agent-roster.md
      owner: duke
      type: document
      description: "Team roster: agents, roles, scopes, workspaces"
      upstream: [agent-constitution]
      projections:
        - target: duke-space/memo/agent-roster.md
          method: symlink

  agents:
    duke:
      workspace: duke-space/
      identity: duke-space/IDENTITY.md
      depends_on: [agent-constitution, agent-roster]
    ken:
      workspace: Bigbird/ken-space/
      identity: Bigbird/ken-space/IDENTITY.md
      depends_on: [agent-constitution, agent-roster]
  ```

**FR-P0-002：Registry 加载与校验**
- **描述**：实现 `load_registry(path)` 函数，从 YAML 文件加载并校验 `SSOTRegistry`。校验包括：(1) schema 合法性（Pydantic 校验）；(2) 引用完整性 — `agents[*].depends_on` 中的每个 entry id 必须在 `entries` 中存在；`entries[*].upstream` 中的每个 entry id 必须在 `entries` 中存在；(3) 无循环依赖 — upstream 链路不得形成环。
- **来源**：故事 1
- **示例**：`load_registry("ssot/registry.yaml")` 返回校验通过的 `SSOTRegistry` 对象。如果 `agents.duke.depends_on` 引用了不存在的 entry id，抛出 `RegistryValidationError`。

**FR-P0-003：DriftReport 模型定义**
- **描述**：定义 `DriftReport` 的 Pydantic schema，对齐 `ssot/agent-constitution/audit.md` 的审计体系。包含：(1) `DriftFinding` — 单条漂移发现，含 check_id（S1-S5/C1-C3）、check_layer（static/content）、target、severity（critical/major/minor）、message、evidence、suggestion；(2) `DriftSummary` — 按 severity 统计计数；(3) `DriftReport` — 检查时间、范围、模式（quick/full）、findings 列表、verdict（pass/pass_with_findings/fail）、summary。
- **来源**：故事 2、故事 3
- **示例**：一个 Agent identity 文件缺失 → 1 条 `DriftFinding`（severity=critical），verdict=fail。

**FR-P0-004：Static check 引擎 — SSOT entry 检查（S1）**
- **描述**：检查 registry 中每个 `entries[*].path` 在文件系统中是否存在。type=package 检查目录存在，type=document 检查文件存在且非空。
- **来源**：故事 3
- **示例**：`entries.agent-constitution.path = ssot/agent-constitution/`，检查该目录存在 → PASS。路径不存在 → DriftFinding(check_id=S1, severity=critical)。

**FR-P0-005：Static check 引擎 — Agent workspace 检查（S2+S3）**
- **描述**：检查 registry 中每个 `agents[*].workspace` 目录存在（S2），以及 `agents[*].identity` 文件存在且非空（S3）。
- **来源**：故事 2
- **示例**：`agents.duke.workspace = duke-space/`，目录存在 → PASS。不存在 → DriftFinding(check_id=S2, severity=critical)。

**FR-P0-006：Static check 引擎 — S4 退役**
- **描述**：`governance.md §三` 的 WS 治理启发式不作为程序化 pass/fail 检查项。Harness 不再因缺少标题、锚点或固定字段产生 S4 finding；相关判断交给 Duke 立法、Amanda 配方支持和 Reeve 独立审查。
- **来源**：2026-04-26 governance §三 复议
- **示例**：Duke 的 workspace 没有 `Review Cadence` 标题，不产生 S4 finding；若 identity 文件缺失，仍由 S3 报 critical。

**FR-P0-007：Static check 引擎 — Projection 存在性检查（S5）**
- **描述**：检查 registry 中每个 `entries[*].projections[*].target` 在文件系统中是否存在。method=symlink 时额外检查是否为有效 symlink。
- **来源**：故事 3
- **示例**：`agent-roster` 声明 projection target=`duke-space/memo/agent-roster.md`，文件存在 → PASS。不存在 → DriftFinding(check_id=S5, severity=major)。

**FR-P0-008：Content check 引擎 — Symlink 完整性检查（C1）**
- **描述**：对 method=symlink 的 projection，检查 symlink target 是否指向声明的 SSOT 源文件。断链或指向错误源 → DriftFinding(check_id=C1, severity=major)。
- **来源**：故事 6
- **示例**：`duke-space/memo/agent-roster.md` 应 symlink 到 `ssot/agent-roster.md`，实际指向其他文件 → FAIL。

**FR-P0-009：Content check 引擎 — Projection 内容一致性检查（C2）**
- **描述**：对 method=reference 或 method=embed 的 projection，计算 SSOT 源文件与 projection 目标文件的内容哈希（SHA-256），不一致时报 DriftFinding(check_id=C2, severity=major)。
- **来源**：故事 6
- **示例**：SSOT 文件更新后 projection 未同步 → 哈希不一致 → FAIL。

**FR-P0-010：CLI 命令 — ssot check**
- **描述**：实现 `harness ssot check` CLI 命令组，支持以下模式：
  - `harness ssot check` — 全量检查（所有 entries + 所有 agents，S+C checks）
  - `harness ssot check --agent <id>` — 检查特定 Agent（S2-S3；S4 退役 + 该 Agent 依赖的 entries 的 S1+S5+C1+C2）
  - `harness ssot check --agent <id> --quick` — 仅 S-checks（适合 Relay spawn 前，<5s）
  - `harness ssot check --entry <id>` — 检查特定 SSOT entry（S1+S5+C1+C2）
  - `harness ssot check --json` — JSON 格式输出（供 Relay 消费）
  - 默认 registry 路径：`$HARNESS_ROOT/../ssot/registry.yaml` 或 `--registry` 参数指定
  - 退出码：0=pass，1=有 findings（pass_with_findings 或 fail），2=系统错误
- **来源**：故事 2、故事 3、故事 4

### P1 功能（增强功能）

**FR-P1-001：CLI 命令 — ssot repair**
- **描述**：实现 `harness ssot repair` CLI 命令，扫描漂移并自动修复可修复项：(1) 断链 symlink → 重建指向 SSOT 源的 symlink；(2) projection method=symlink 但目标是普通文件 → 提示是否替换为 symlink。不可自动修复的项（内容不一致、字段缺失）输出修复建议但不修改。
- **来源**：故事 5
- **示例**：`duke-space/memo/agent-roster.md` 的 symlink 断链 → repair 自动创建正确的 symlink。

**FR-P1-002：Content check — Agent upstream 一致性检查（C3）**
- **描述**：检查 Agent identity 文件中声明的 upstream 字段值是否与 registry 中 `agents[*].depends_on` 一致。不一致时报 DriftFinding(check_id=C3, severity=minor)。
- **来源**：故事 2

**FR-P1-003：DriftReport 持久化**
- **描述**：`harness ssot check --save` 将 DriftReport 写入 `.harness/ssot-reports/YYYY-MM-DD.yaml`。支持 `harness ssot history` 查看历史报告。
- **来源**：审计留痕需求（audit.md）

### P2 功能（未来方向）

**FR-P2-001：术语一致性检查（X1）**
- **描述**：检查 Agent workspace 文件中使用的关键术语是否与 `ssot/agent-constitution/glossary.md` 一致。需要 NLP/LLM 辅助。
- **来源**：故事 3（延伸）

**FR-P2-002：HEARTBEAT 集成**
- **描述**：在 HEARTBEAT 模板中新增检查项：最近 ssot check 报告是否存在且 ≤7 天、是否有未修复的 critical/major findings。
- **来源**：定期巡检需求

---

## 3. 验收标准（Acceptance Criteria）

### P0 功能验收标准

**AC-P0-001：Registry 模型校验**
- **条件**：提交合法和非法的 registry YAML
- **预期结果**：合法 YAML 加载成功并返回 SSOTRegistry 对象；缺必填字段、引用不存在的 entry id、存在循环依赖时抛出明确错误
- **验证方式**：自动化单元测试（合法 YAML 1 例 + 非法 YAML 4 例：缺字段、悬空引用、循环依赖、类型非法）

**AC-P0-002：Static check — SSOT entry 存在性**
- **条件**：构造 registry 声明存在和不存在的 SSOT 路径
- **预期结果**：存在 → 无 finding；不存在 → DriftFinding(check_id=S1, severity=critical)
- **验证方式**：自动化单元测试（2 例：存在/不存在）

**AC-P0-003：Static check — Agent workspace 存在性**
- **条件**：构造 registry 声明存在和不存在的 workspace
- **预期结果**：workspace 不存在 → S2(critical)；identity 不存在 → S3(critical)；identity 存在但为空 → S3(critical)
- **验证方式**：自动化单元测试（3 例）

**AC-P0-004：Static check — S4 退役**
- **条件**：构造包含或不包含 WS 治理启发式标题的 workspace markdown 文件
- **预期结果**：不因启发式标题缺失产生 DriftFinding；结构性问题仍由 S2/S3/S5/C1/C2 报告
- **验证方式**：自动化单元测试

**AC-P0-005：Content check — Symlink 完整性**
- **条件**：构造正确 symlink、断链 symlink、指向错误源的 symlink
- **预期结果**：正确 → 无 finding；断链 → C1(major)；指向错误 → C1(major) 且 evidence 包含实际 target
- **验证方式**：自动化单元测试（3 例）

**AC-P0-006：Content check — 哈希一致性**
- **条件**：构造内容一致和不一致的源/target 文件对
- **预期结果**：一致 → 无 finding；不一致 → C2(major) 且 evidence 包含两者哈希
- **验证方式**：自动化单元测试（2 例）

**AC-P0-007：DriftReport 汇总正确性**
- **条件**：构造含 2 critical + 3 major + 1 minor findings 的检查结果
- **预期结果**：summary 计数正确；verdict = fail（因存在 critical）
- **验证方式**：自动化单元测试

**AC-P0-008：CLI 退出码**
- **条件**：分别构造 pass / pass_with_findings / fail / 系统错误场景
- **预期结果**：0 / 1 / 1 / 2
- **验证方式**：CLI 集成测试（4 例）

**AC-P0-009：--quick 模式性能**
- **条件**：对含 11 个 Agent 的 registry 执行 --quick 检查
- **预期结果**：仅执行 S-checks（不执行 C-checks），耗时 <5 秒
- **验证方式**：计时测试

**AC-P0-010：--json 输出格式**
- **条件**：执行 `harness ssot check --json`
- **预期结果**：输出合法 JSON，可被 `json.loads()` 解析，结构与 DriftReport schema 一致
- **验证方式**：自动化测试

**AC-P0-011：Verdict 判定规则**
- **条件**：构造不同 findings 组合
- **预期结果**：无 findings → pass；仅 minor → pass_with_findings；存在 major → pass_with_findings；存在 critical → fail
- **验证方式**：自动化单元测试（4 例）

---

## 4. 非功能需求（Non-Functional Requirements）

### 性能需求

- **quick 模式**：`harness ssot check --agent <id> --quick` 响应 <5 秒（纯文件系统操作）
- **full 模式**：`harness ssot check`（全量）响应 <30 秒（含哈希计算）
- **无 LLM 调用**：v0.1 全部检查为机械/确定性的，不依赖 LLM

### 可维护性需求

- **代码规范**：遵循 project-standards.md — PEP 8 + ruff + mypy strict
- **测试覆盖率**：ssot.py（模型）>=90%，ssot_engine.py（引擎）>=95%，CLI >=70%，整体 >=80%
- **文档**：公开函数有 docstring（Google style）

### 兼容性需求

- **不修改现有模块**：models.py / machine.py / store.py / guardrail.py / gate_engine.py / compliance_engine.py / extractor.py 均不变
- **现有测试不受影响**：新增测试不修改、不跳过已有 316 个测试
- **CLI 扩展**：新增 `ssot` 子命令组，不影响已有 `gate` / `compliance` / `extract` 等命令

---

## 5. 约束条件（Constraints）

### 技术约束

- **技术栈**：遵循 project-architecture.md — Python 3.11+ / Pydantic v2 / PyYAML / Click / pytest
- **存储格式**：YAML 文件 + Git（与 Phase 1 一致）
- **registry 位置**：`ssot/registry.yaml`（SSOT 目录根部）
- **报告位置**：`.harness/ssot-reports/`（P1）
- **与已有模块的关系**：新增 `ssot.py` + `ssot_engine.py`，扩展 `cli.py`，不修改其他模块

### 上游约束

- **WS 治理启发式**：以 `ssot/agent-constitution/governance.md §三` 为准；不作为 harness 机械审核项
- **审计体系**：以 `ssot/agent-constitution/audit.md` 为准 — 三层 / 三级 severity / 三种 verdict
- **术语**：以 `ssot/agent-constitution/glossary.md` 为准 — Structural Drift / Severity / Audit 等

### 时间约束

- **P0 开发周期**：2 周
- **P1 开发周期**：P0 验证后 1 周

### 资源约束

- **开发**：Ken 主导
- **运行**：无 LLM 调用，纯 CLI 工具

---

## 6. 排除范围（Out of Scope）

- 不修改 Phase 1 状态机（WorkItem / HandoffContract / SignalEvent 模型不变）
- 不修改 Phase 2 护栏系统（guardrail / gate / compliance 模块不变）
- 不做术语一致性检查（需 LLM，留给 P2）
- 不做跨 Agent scope 重叠检测（留给 P2）
- 不做 Relay 侧改动（Relay spawn 前校验接口是独立行动项 #5）
- 不创建 `ssot/registry.yaml` 本身（那是行动项 #1，由 Pulse 执行，本 feature 只提供消费它的工具）
- 不自动修复 workspace 中的 WS 治理启发式表达（repair 只修复 symlink 等机械问题）
- 不做 Web UI / Dashboard

---
