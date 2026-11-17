---
feature_name: "ssot-governance"
version: "0.0.1"
created: "2026-04-16T12:00:00+0800"
modified: "2026-04-16T12:00:00+0800"
upstream_dependency:
  - "project-specs/project-architecture.md"
  - "features/ssot-governance/requirements.md"
tags: [design, l2-spec, ssot, governance, drift-detection]
---

# ssot-governance - 技术设计文档

> **设计概述**：在 agent-harness 中新增 SSOT 治理模块（Pydantic 模型 + 漂移检测引擎 + CLI），通过 `registry.yaml` 描述全局 SSOT 拓扑，通过分层检查引擎机械检测漂移，对齐 `agent-constitution/audit.md` 的审计体系输出结构化报告。
>
> **关联文档**：
> - L1 项目架构：`project-specs/project-architecture.md`
> - L2 功能需求：`features/ssot-governance/requirements.md`
> - L2 任务拆分：`features/ssot-governance/tasks.md`
> - 上游审计方法：`ssot/agent-constitution/audit.md`
> - 上游治理规则：`ssot/agent-constitution/governance.md`
> - 现有模式参考：`harness/guardrail.py`（模型+I/O）、`harness/gate_engine.py`（引擎）

---

## 1. 技术栈（Tech Stack）

### 技术栈引用

**基于 L1 项目架构**：`project-specs/project-architecture.md`

- **编程语言**：Python 3.11+（遵循 L1 定义）
- **数据模型**：Pydantic v2（遵循 L1 定义，schema 即校验）
- **序列化**：PyYAML（遵循 L1 定义，YAML 文件存储）
- **CLI 框架**：Click（遵循 L1 定义，新增 `ssot` 子命令组）
- **测试**：pytest + pytest-cov（遵循 L1 定义）
- **哈希**：hashlib（标准库，SHA-256，用于内容一致性检查）

### 技术选型说明

- ssot 模块复用 harness 现有技术栈 — 同一个项目，同一套工具链
- 所有检查为纯文件系统操作 + 哈希计算，不依赖 LLM
- registry.yaml 直接在 SSOT 目录，不在 .harness/ 下 — SSOT 拓扑声明应与 SSOT 内容同级

---

## 2. 架构设计（Architecture Design）

### 组件划分

```
ssot-governance 功能
├── 模型层（ssot.py）         ── Registry / SSOTEntry / AgentDecl / Projection /
│                                DriftFinding / DriftSummary / DriftReport
├── 引擎层（ssot_engine.py）  ── 分层检查逻辑（S-checks + C-checks）
└── CLI 层（cli.py 扩展）     ── ssot check / ssot repair
```

### 与现有模块的关系

```
harness/
├── models.py            # 现有：WorkItem / HandoffContract / SignalEvent（不修改）
├── machine.py           # 现有：状态机逻辑（不修改）
├── store.py             # 现有：YAML CRUD（不修改）
├── receipt.py           # 现有：Receipt / Verdict（不修改）
├── guardrail.py         # 现有：GuardrailProfile / BehavioralFact（不修改）
├── gate_engine.py       # 现有：Gate check 引擎（不修改）
├── compliance_engine.py # 现有：Compliance check 引擎（不修改）
├── extractor.py         # 现有：BehavioralFact 提取（不修改）
├── cli.py               # 现有：CLI 命令（扩展：加 ssot 子命令组）
├── ssot.py              # 新增：Registry + DriftReport 模型 + I/O
└── ssot_engine.py       # 新增：漂移检测引擎

ssot/                    # SSOT 目录（Coevolution 根目录）
├── registry.yaml        # 全局 SSOT 注册表（本 feature 消费，行动项 #1 创建）
└── agent-constitution/  # 已存在的 SSOT 包
```

### 模块关系

```
[registry.yaml]
     │
     ▼
ssot.py ─── load_registry() ──→ SSOTRegistry（模型校验 + 引用完整性 + 环检测）
     │
     ▼
ssot_engine.py
     │
     ├── check_all(registry, repo_root, mode) ──→ DriftReport
     │       │
     │       ├── 对每个 entry: _check_entry(entry, repo_root)
     │       │     ├── S1: 文件/目录存在性
     │       │     ├── S5: projection 目标存在性
     │       │     ├── C1: symlink 完整性（full 模式）
     │       │     └── C2: 内容哈希一致性（full 模式）
     │       │
     │       └── 对每个 agent: _check_agent(agent, entries, repo_root)
     │             ├── S2: workspace 存在性
     │             ├── S3: identity 文件存在性
     │             └── S4: 已退役（WS 治理启发式不做程序裁决）
     │
     ├── check_agent(registry, agent_id, repo_root, mode) ──→ DriftReport
     │       └── 只检查该 agent + 其依赖的 entries
     │
     └── check_entry(registry, entry_id, repo_root) ──→ DriftReport
             └── 只检查该 entry + 其 projections
     │
     ▼
cli.py ─── harness ssot check [--agent|--entry] [--quick] [--json] [--registry]
           harness ssot repair [--registry]
```

---

## 3. 接口定义（Interface Definition）

### CLI 命令

**Command-001：ssot check**

```
harness ssot check [--agent <id>] [--entry <id>] [--quick] [--json] [--registry <path>]
```

- `--agent`：检查特定 Agent（互斥于 `--entry`）
- `--entry`：检查特定 SSOT entry（互斥于 `--agent`）
- `--quick`：仅执行 S-checks（跳过 C-checks），用于 Relay spawn 前
- `--json`：JSON 格式输出（默认为人类可读文本）
- `--registry`：registry.yaml 路径（默认：`$HARNESS_ROOT/../ssot/registry.yaml`）
- 无 `--agent` 和 `--entry` 时：全量检查
- 退出码：0=pass，1=pass_with_findings 或 fail，2=系统错误

人类可读输出格式：

```
SSOT Governance Check — 2026-04-16T12:00:00+0800
Mode: full | Scope: all

Entries:
  [PASS] agent-constitution
  [PASS] agent-roster

Agents:
  [PASS] duke
  [FAIL] ken — 1 finding
    S3 CRITICAL  Agent 'ken' identity file not found

Verdict: fail (1 critical, 0 major, 0 minor)
```

**Command-002：ssot repair（P1）**

```
harness ssot repair [--registry <path>] [--dry-run]
```

- `--dry-run`：只报告可修复项，不实际修复
- 输出：每个修复动作的描述 + 结果
- 退出码：0=全部修复成功，1=有不可修复项，2=系统错误

### 内部函数接口

| 函数 | 模块 | 签名 | 说明 |
|------|------|------|------|
| `load_registry` | ssot.py | `(path: Path) -> SSOTRegistry` | 加载 + 校验 registry YAML。校验失败抛 `RegistryValidationError` |
| `check_all` | ssot_engine.py | `(registry: SSOTRegistry, repo_root: Path, mode: str) -> DriftReport` | 全量检查。mode="quick" 只跑 S-checks |
| `check_agent` | ssot_engine.py | `(registry: SSOTRegistry, agent_id: str, repo_root: Path, mode: str) -> DriftReport` | 检查特定 Agent + 其依赖 entries |
| `check_entry` | ssot_engine.py | `(registry: SSOTRegistry, entry_id: str, repo_root: Path) -> DriftReport` | 检查特定 entry + projections |
| `_check_entry_static` | ssot_engine.py | `(entry: SSOTEntry, repo_root: Path) -> list[DriftFinding]` | S1 + S5 检查 |
| `_check_entry_content` | ssot_engine.py | `(entry: SSOTEntry, repo_root: Path) -> list[DriftFinding]` | C1 + C2 检查 |
| `_check_agent_static` | ssot_engine.py | `(agent: AgentDecl, repo_root: Path) -> list[DriftFinding]` | S2 + S3 检查；S4 已退役 |
| `_check_minimum_fields` | ssot_engine.py | `(workspace_path: Path) -> list[DriftFinding]` | 兼容入口；返回空列表 |
| `_compute_verdict` | ssot_engine.py | `(findings: list[DriftFinding]) -> DriftVerdict` | 根据 findings 计算 verdict |

---

## 4. 数据模型（Data Model）

### Model-001：SSOTRegistry（Registry 模型）

```python
class ProjectionMethod(str, Enum):
    """How a SSOT entry is projected to downstream locations."""
    SYMLINK = "symlink"
    REFERENCE = "reference"
    EMBED = "embed"

class SSOTType(str, Enum):
    """Type of SSOT entry."""
    PACKAGE = "package"      # directory containing multiple files
    DOCUMENT = "document"    # single file

class Projection(BaseModel):
    """A downstream projection of a SSOT entry."""
    target: str              # path relative to repo root
    method: ProjectionMethod

class SSOTEntry(BaseModel):
    """A single SSOT entry in the registry."""
    path: str                # relative to repo root
    owner: str               # agent id who owns this SSOT
    type: SSOTType
    description: str
    upstream: list[str] = []           # entry ids this depends on
    projections: list[Projection] = []

class AgentDecl(BaseModel):
    """Agent workspace declaration in the registry."""
    workspace: str           # directory path relative to repo root
    identity: str            # identity file path relative to repo root
    depends_on: list[str] = []  # entry ids this agent depends on

class SSOTRegistry(BaseModel):
    """The global SSOT registry — dependency graph + agent declarations."""
    schema_version: str
    repo_root: str                             # absolute path to repo root
    entries: dict[str, SSOTEntry]               # entry_id -> SSOTEntry
    agents: dict[str, AgentDecl]                # agent_id -> AgentDecl

    @model_validator(mode="after")
    def validate_references(self) -> "SSOTRegistry":
        """Validate referential integrity and detect cycles."""
        # 1. agents[*].depends_on must reference existing entries
        # 2. entries[*].upstream must reference existing entries
        # 3. upstream graph must be acyclic (topological sort)
        ...
```

### Model-002：DriftReport（漂移报告模型）

对齐 `ssot/agent-constitution/audit.md`。

```python
class DriftSeverity(str, Enum):
    """Severity levels — aligned with audit.md."""
    CRITICAL = "critical"    # structural error, must fix immediately
    MAJOR = "major"          # governance failure, fix in next cycle
    MINOR = "minor"          # incomplete, can schedule

class DriftVerdict(str, Enum):
    """Audit verdicts — aligned with audit.md."""
    PASS = "pass"
    PASS_WITH_FINDINGS = "pass_with_findings"
    FAIL = "fail"

class CheckLayer(str, Enum):
    """Audit layers — aligned with audit.md three-layer model."""
    STATIC = "static"        # identity + minimum fields present
    CONTENT = "content"      # projection consistency + hash match
    STRUCTURAL = "structural"  # future: terminology, scope overlap

class DriftFinding(BaseModel):
    """A single drift finding — one row in the audit report."""
    check_id: str            # "S1" / "S4" / "C1" / "C2" etc.
    check_layer: CheckLayer
    target: str              # what was checked (file path or agent id)
    severity: DriftSeverity
    message: str             # human-readable description
    evidence: str            # what was observed
    suggestion: str          # how to fix

class DriftSummary(BaseModel):
    """Counts by severity."""
    total: int
    critical: int
    major: int
    minor: int

class DriftReport(BaseModel):
    """Complete drift check output."""
    checked_at: datetime
    registry_path: str
    scope: str               # "all" / "agent:<id>" / "entry:<id>"
    mode: str                # "quick" / "full"
    findings: list[DriftFinding]
    verdict: DriftVerdict
    summary: DriftSummary
```

### Verdict 判定规则

```python
def _compute_verdict(findings: list[DriftFinding]) -> DriftVerdict:
    if not findings:
        return DriftVerdict.PASS
    if any(f.severity == DriftSeverity.CRITICAL for f in findings):
        return DriftVerdict.FAIL
    return DriftVerdict.PASS_WITH_FINDINGS
```

- 无 findings → `pass`
- 仅 minor / major → `pass_with_findings`
- 存在 critical → `fail`

### S4 退役规则

`governance.md §三` 已改为 WS 治理启发式。它们用于 Duke 立法、Amanda 配方支持与 Reeve 独立审查，不用于 harness 机械 pass/fail。

实现要求：

- `_check_minimum_fields` 作为兼容入口保留，但返回空列表。
- `MINIMUM_GOVERNANCE_DIMENSIONS` 仅作为历史 import 兼容，不参与搜索或判定。
- 不再生成 `missing canonical anchor`、`Missing governance dimension` 或任何 S4 major finding。

### 存储布局

```
ssot/
├── registry.yaml              # 全局 SSOT 注册表
└── agent-constitution/        # 已存在

.harness/
├── ssot-reports/              # P1: 漂移检查报告历史
│   └── 2026-04-16.yaml
├── work-items/                # 现有（Phase 1）
├── handoffs/                  # 现有（Phase 1）
├── events/                    # 现有（Phase 1）
├── guardrails/                # 现有（Phase 2）
└── PROTOCOL.md                # 现有
```

---

## 5. 数据流（Data Flow）

### Flow-001：全量漂移检查（harness ssot check）

```
用户执行 harness ssot check
    │
    ├─→ [1] CLI 解析参数，加载 registry.yaml
    │       load_registry(path) → SSOTRegistry
    │       校验失败 → 退出码 2
    │
    ├─→ [2] 调用 check_all(registry, repo_root, mode="full")
    │
    ├─→ [3] 遍历 entries:
    │       对每个 entry:
    │         ├── S1: 检查 entry.path 存在
    │         ├── S5: 检查 projection targets 存在
    │         ├── C1: 检查 symlink projections 指向正确（full 模式）
    │         └── C2: 检查 reference/embed projections 内容哈希一致（full 模式）
    │
    ├─→ [4] 遍历 agents:
    │       对每个 agent:
    │         ├── S2: 检查 workspace 目录存在
    │         ├── S3: 检查 identity 文件存在且非空
    │         └── S4: 已退役，不检查 WS 治理启发式标题
    │
    ├─→ [5] 汇总 findings → 计算 verdict → 构建 DriftReport
    │
    └─→ [6] 输出报告（人类可读 或 JSON）
            退出码：0=pass / 1=有 findings / 2=系统错误
```

### Flow-002：Agent 级快速检查（Relay spawn 前）

```
Relay 调用 harness ssot check --agent duke --quick --json
    │
    ├─→ [1] 加载 registry，找到 agents.duke
    │
    ├─→ [2] 调用 check_agent(registry, "duke", repo_root, mode="quick")
    │
    ├─→ [3] 只执行 S-checks:
    │       ├── S2: duke-space/ 存在
    │       ├── S3: duke-space/IDENTITY.md 存在且非空
    │       ├── S4: 已退役，不检查 WS 治理启发式标题
    │       └── 对 duke.depends_on 中的每个 entry:
    │             ├── S1: entry 路径存在
    │             └── S5: projection targets 存在
    │       （跳过 C1/C2 — quick 模式）
    │
    ├─→ [4] 汇总 → DriftReport
    │
    └─→ [5] JSON 输出
            目标耗时 <5 秒
```

### Flow-003：ssot repair（P1）

```
用户执行 harness ssot repair
    │
    ├─→ [1] 先执行 check_all(mode="full") 获取 findings
    │
    ├─→ [2] 筛选可自动修复的 findings:
    │       ├── C1（断链 symlink）→ 删除断链 + 重建 symlink
    │       ├── S5（projection 不存在且 method=symlink）→ 创建 symlink
    │       └── 其他 → 输出修复建议，不自动修改
    │
    └─→ [3] 输出修复结果
```

---

## 6. 错误处理（Error Handling）

| 场景 | 处理策略 | 退出码 |
|------|---------|--------|
| registry.yaml 不存在 | 输出 `Error: registry not found at <path>` | 2 |
| registry.yaml 校验失败（schema） | 输出 Pydantic 校验错误详情 | 2 |
| registry.yaml 引用完整性失败 | 输出具体的悬空引用 | 2 |
| registry.yaml 循环依赖 | 输出循环路径 | 2 |
| `--agent` 指定的 id 不在 registry 中 | 输出 `Error: agent '<id>' not in registry` | 2 |
| `--entry` 指定的 id 不在 registry 中 | 输出 `Error: entry '<id>' not in registry` | 2 |
| identity 文件编码错误 | 跳过该 agent，输出 warning，生成 DriftFinding(severity=critical) | 不影响退出码（由 verdict 决定） |
| 文件权限不足 | 跳过该检查，生成 DriftFinding(severity=critical, evidence="Permission denied") | 不影响退出码 |

---

## 7. 安全设计与性能

### 安全

- **只读操作**：`ssot check` 不修改任何文件
- **repair 可控**：`ssot repair` 只修复 symlink，不修改内容文件；支持 `--dry-run`
- **路径安全**：所有路径拼接使用 `Path.resolve()` + 检查不逃逸 repo_root

### 性能目标

- quick 模式 <5s（纯文件存在性检查 + 文件内容 grep）
- full 模式 <30s（额外 readlink + SHA-256 哈希计算）
- 无网络调用，无 LLM 调用

---
