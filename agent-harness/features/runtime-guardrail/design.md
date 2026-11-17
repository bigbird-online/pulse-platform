---
feature_name: "runtime-guardrail"
version: "0.0.1"
created: "2026-04-06T12:50:00+0800"
modified: "2026-04-06T12:50:00+0800"
upstream_dependency: ["project-architecture.md", "requirements.md"]
tags: [design, l2-spec, guardrail, constraint, verification]
---

# runtime-guardrail - 技术设计文档

> **设计概述**：在 agent-harness 中新增 guardrail 模块（Pydantic 模型 + gate/compliance 引擎 + CLI），定义 extractor/verifier 两个 OC sub-agent 及其执行规则，修改 /wrap 协议集成行为审核流程。
>
> **关联文档**：
> - L1项目架构：`project-specs/project-architecture.md`
> - L2功能需求：`features/runtime-guardrail/requirements.md`
> - L2任务拆分：`features/runtime-guardrail/tasks.md`
> - 已有资产：`harness/receipt.py`（VerificationProfile 匹配机制 + 序列化工具可复用）

---

## 1️⃣ 技术栈（Tech Stack）

### 技术栈引用

**基于L1项目架构**：`project-specs/project-architecture.md`

- **编程语言**：Python 3.11+（遵循 L1 定义）
- **数据模型**：Pydantic v2（遵循 L1 定义，schema 即校验）
- **序列化**：PyYAML（遵循 L1 定义，YAML 文件存储）
- **CLI 框架**：Click（遵循 L1 定义，新增 `gate` 和 `compliance` 子命令组）
- **测试**：pytest + pytest-cov（遵循 L1 定义）
- **OC sub-agent**：`.claude/agents/*.md` 定义文件（OC 原生机制，无新依赖）

### 技术选型说明

- guardrail 模块复用 harness 现有技术栈 — 同一个项目，同一套工具链
- gate check 作为 CLI 同步调用（不是服务/webhook）— agent 在 checkpoint 通过 exec 调用，阻塞等结果
- extractor/verifier 作为 OC sub-agent — 需要 LLM 能力理解对话内容和判断行为合规，规则引擎做不到

---

## 2️⃣ 架构设计（Architecture Design）

### 组件划分

```
runtime-guardrail 功能
├── 模型层（guardrail.py）      ── GuardrailProfile / GateResult / BehavioralFact / ComplianceVerdict
���── 引擎层（gate_engine.py）    ── Gate check 逻辑（加载 YAML + 执行条件检查）
├── 引擎层（compliance_engine.py）── Compliance check 逻辑（facts + guardrails → verdict）
├── CLI 层（cli.py 扩展）       ── gate check / compliance check / compliance create-work-item
├── Agent 定义层                ── session-extractor.md / session-verifier.md（含执行规则）
└── 实例层                      ── .harness/guardrails/emi/*.yaml（Emi 护栏实例）
```

### 与现有模块的关系

```
harness/
├── models.py        # 现有：WorkItem / HandoffContract / SignalEvent（不修改）
├── machine.py       # 现有：状态机逻辑（不修改）
├── store.py         # 现有：YAML CRUD（不修改）
├── receipt.py       # 现有：Receipt / Verdict / VerificationProfile（不修改，部分复用）
├── cli.py           # 现有：CLI 命令（扩展：加 gate + compliance 子命令组）
├── guardrail.py     # 新增：GuardrailProfile / GateResult / BehavioralFact / ComplianceVerdict
├── gate_engine.py   # 新增：Gate check 引擎
└── compliance_engine.py  # 新增：Compliance check 引擎

.claude/agents/
├── session-extractor.md   # 新增：从原始对话提取 BehavioralFact
└── session-verifier.md    # 新增：基于 facts + guardrails 审核合规

.harness/
├── work-items/            # 现有（Phase 1）
├── handoffs/              # 现有（Phase 1）
├── events/                # 现有（Phase 1）
├── guardrails/            # 新增：护栏定义（按 agent 分目录）
│   └── emi/
│       ├── gate-s1-complete.yaml
│       ├── spawn-enforcement.yaml
│       ├── confirm-before-persist.yaml
│       ├── perfect-score-alert.yaml
│       └── no-orchestrator-injection.yaml
└── PROTOCOL.md            # 现有（追加护栏协议段）
```

### 模块关系

```
[Agent session]
     │
     ├── checkpoint ──→ CLI: harness gate check
     │                       │
     │                       ▼
     │                  gate_engine.py ──→ .harness/guardrails/{agent}/ (type=gate)
     │                       │
     │                       ▼
     │                  GateResult (PASS/FAIL)
     │
     └── /wrap ──→ spawn session-extractor
                       │
                       ▼
                  BehavioralFact (facts YAML)
                       │
                       ▼
                  spawn session-verifier
                       │
                       ▼
                  compliance_engine.py ──→ .harness/guardrails/{agent}/ (type=behavioral)
                       │
                       ▼
                  ComplianceVerdict
```

---

## 3️⃣ 接口定义（Interface Definition）

### CLI 命令

**Command-001：gate check**

```
harness gate check --agent <name> --gate <guardrail_id> [--context key=value ...]
```

- `--agent`：被检查的 agent 名称（如 `emi`）
- `--gate`：护栏 ID（如 `gate-s1-complete`）
- `--context`：可选的上下文变量，用于填充护栏 YAML 中的占位符（如 `experiment_dir=/path/to/experiment`）
- 成功输出：`PASS: gate-s1-complete — all preconditions met`
- 失败输出：`FAIL: gate-s1-complete — Missing: five_why_result.md`
- 退出码：0=PASS，1=FAIL，2=系统错误（如护栏文件不存在）

**Command-002：compliance check**

```
harness compliance check --agent <name> --facts <path>
```

- `--agent`：被检查的 agent 名称
- `--facts`：BehavioralFact YAML 文件路径
- 输出：逐条护栏判定结果 + 汇总
- 退出码：0=全部 COMPLIANT，1=存在 VIOLATED，2=系统错误

**Command-003：compliance create-work-item（P1）**

```
harness compliance create-work-item <verdict-path> --owner <agent>
```

- 解析 ComplianceVerdict，对 VIOLATED 项创建 WorkItem
- 复用 Phase 1 的 store.save_work_item + store.append_event

### 内部函数接口

| 函数 | 模块 | 签名 | 说明 |
|------|------|------|------|
| load_guardrails | guardrail.py | `(dir: Path, type: str｜None) → list[GuardrailProfile]` | 从目录加载 YAML，可按 type 过滤 |
| run_gate_check | gate_engine.py | `(guardrail: GuardrailProfile, context: dict) → GateResult` | 执行单条 gate check，支持占位符替换。type != gate 时抛 ValueError |
| run_compliance_check | compliance_engine.py | `(facts: BehavioralFact, guardrails: list) → ComplianceVerdict` | 逐条检查 behavioral 护栏，自动汇总 summary + action_items |

---

## 4️⃣ 数据模型（Data Model）

### Model-001：GuardrailProfile

**GuardrailType** enum：`gate`（checkpoint 前置条件）| `behavioral`（/wrap 行为审核）

**CheckAction** 嵌套模型（字段）：type(`file_exists`/`command`/`pattern_match`/`temporal_check`), paths[]?, command?, pattern?, field?, pass_condition

**GuardrailProfile** 字段：guardrail_id, agent, type(GuardrailType), description, check(CheckAction), failure_action, source_ref, tags=[]

### Model-002：GateResult

```python
class GateVerdict(str, Enum):
    PASS = "PASS"
    FAIL = "FAIL"

class GateResult(BaseModel):
    """Result of a single gate check."""
    guardrail_id: str
    verdict: GateVerdict
    evidence: str                   # 判定依据
    checked_at: datetime
    context: dict[str, str]         # 使用的上下文变量
    missing_items: list[str] = []   # FAIL 时缺失的具体项
```

### Model-003：BehavioralFact

```python
class SpawnRecord(BaseModel):
    """Record of a sub-agent spawn during the session."""
    target: str                     # sub-agent 名称���描述
    purpose: str                    # spawn 用途
    rule_ref: str | None = None     # 引用的规则路径（如有）

class FileOperation(BaseModel):
    """Record of a file operation during the session."""
    path: str                       # 文件路径
    action: str                     # "created" | "modified" | "deleted"

class UserConfirmation(BaseModel):
    """Record of a user confirmation during the session."""
    artifact: str                   # 被确认的产出物名称
    confirmed: bool                 # 是否已确认
    context: str                    # 确认上下文

class FactDecision(BaseModel):
    """Record of a decision made during the session."""
    what: str                       # 决策主题
    chose: str                      # 选择了什么
    rejected: str                   # 拒绝了什么
    rationale: str                  # 为什么

class BehavioralFact(BaseModel):
    """Structured behavioral facts extracted from a session.

    Contains only observable facts, no judgments.
    """
    session_id: str                 # 格式：YYYY-MM-DD-{agent}-NNN
    agent: str
    period: str                     # ISO 8601 interval
    spawns: list[SpawnRecord]
    file_operations: list[FileOperation]
    user_confirmations: list[UserConfirmation]
    decisions: list[FactDecision]
    transcript_source: str          # "sessions_history" | "context_window"
    transcript_truncated: bool
```

### Model-004：ComplianceVerdict

```python
class ComplianceResult(str, Enum):
    COMPLIANT = "COMPLIANT"
    VIOLATED = "VIOLATED"
    UNVERIFIABLE = "UNVERIFIABLE"

class GuardrailResult(BaseModel):
    """Compliance result for a single guardrail."""
    guardrail_id: str
    verdict: ComplianceResult
    evidence: str                   # 判定依据（事实陈述）
    verification_method: str        # 验证方式描述

class ComplianceSummary(BaseModel):
    """Aggregated compliance results."""
    total: int
    compliant: int
    violated: int
    unverifiable: int

class ComplianceVerdict(BaseModel):
    """Complete compliance check output."""
    facts_path: str
    guardrails_dir: str
    verified_at: datetime
    results: list[GuardrailResult]
    summary: ComplianceSummary
    action_items: list[str]         # 从 VIOLATED 结果中提取的修复建议
```

### 存储布局与 ER 关系

- 护栏定义：`.harness/guardrails/{agent}/*.yaml`（gate + behavioral 类型）
- 行为事实 + 判定：`{oa-workspace}/memo/receipts/YYYY-MM-DD-{agent}-NNN.{facts|verdict}.yaml`
- ER：GuardrailProfile → BehavioralFact → ComplianceVerdict → WorkItem(P1, 复用 Phase 1)

---

## 5️⃣ 数据流（Data Flow）

### Flow-001：Gate check（checkpoint 时刻）

```
Agent 到达 checkpoint（如 §1→§2 边界）
    │
    ├─→ [1] Agent 调用 `harness gate check --agent emi --gate gate-s1-complete --context experiment_dir=/path`
    │
    ├─→ [2] gate_engine 加载 .harness/guardrails/emi/gate-s1-complete.yaml
    │
    ├─→ [3] 解析 check.type = "file_exists"
    │       替换占位符：{experiment_dir} → /path
    │       检查 /path/five_why_result.md 和 /path/north_star.md
    │
    ├─→ [4a] 两个文件都存在且非空 → GateResult(verdict=PASS)
    │        输出：PASS: gate-s1-complete — all preconditions met
    │        退出码 0，Agent 继续进入 §2
    │
    └─→ [4b] 某文件缺失 → GateResult(verdict=FAIL, missing_items=["five_why_result.md"])
             输出：FAIL: gate-s1-complete — Missing: five_why_result.md
             退出码 1，Agent 不得进入 §2
```

### Flow-002：/wrap 行为合规审核

```
/wrap 触发
    │
    ├─→ [1] 主 agent 调用 sessions_history 获取自身对话原文
    │       • 机械性操作，不做筛选
    │       • 如 sessions_history 不可用，fallback 到 context_window
    │
    ├─→ [2] Spawn session-extractor sub-agent
    │       • 传入：对话原文作为 attachment（name: "transcript.md"）
    │       • extractor 按执行规则从对话中提取 spawns / file_operations /
    │         user_confirmations / decisions
    │       • extractor 返回 BehavioralFact YAML
    │       • tools 为空（不访问文件系统）
    │
    ├─→ [3] 主 agent 将 facts 写入 memo/receipts/YYYY-MM-DD-{agent}-NNN.facts.yaml
    │
    ├─→ [4] Spawn session-verifier sub-agent
    │       • 传入：facts 文件路径 + .harness/guardrails/{agent}/ 目录路径
    │       • 不传入：对话内容（verifier 不知道 session 发生了什么）
    │       • verifier 按执行规则逐条验证 behavioral 类型护栏
    │
    ├─→ [5] Verifier 逐条验证
    │       • 对每条 behavioral 护栏，从 facts 中找对应行为记录
    │       • 按护栏 check 条件判定 COMPLIANT / VIOLATED / UNVERIFIABLE
    │       • 输出 ComplianceVerdict YAML
    │
    ├─→ [6] 主 agent 将 verdict 写入 memo/receipts/...verdict.yaml
    │
    ├─→ [7] 主 agent 汇报合规结果摘要
    │       • "Compliance: 5 guardrails — 4 COMPLIANT, 1 VIOLATED (confirm-before-persist)"
    │
    └─→ [8] 继续现有 /wrap 步骤
            • progress.md
            • git commit（含 facts + verdict）
```

### Flow-003：VIOLATED → harness work item（P1）

`harness compliance create-work-item <verdict-path> --owner emi` → 解析 verdict → 提取 VIOLATED 的 action_items → 创建 WorkItem(draft) → git commit

### 上下文传递

- **Extractor**：主 agent 通过 `sessions_history`（80KB 上限）获取对话原文，作为 OC spawn `attachments` 传入。Fallback：sessions_history 不可用时用 context_window 摘要，标注 `transcript_source`。
- **Verifier**：通过 spawn prompt 传入 facts 路径 + guardrails 目录路径，无对话访问。

---

## 6️⃣ 错误处理（Error Handling）

| 场景 | 处理策略 | 退出码/影响 |
|------|---------|------------|
| 护栏文件不存在 | 输出 `Error: guardrail not found`，退出码 2 | gate check 无法执行 |
| 护栏 YAML 校验失败 | 输出错误详情，跳过该护栏 | 不影响其他护栏 |
| 占位符未替换 | 输出 `Error: unresolved placeholder`，退出码 2 | gate check 无法执行 |
| Extractor spawn 失败 | /wrap 不中断，fallback 到自提取（标注 `self_extract_fallback`） | facts 质量降级 |
| Verifier spawn 失败 | /wrap 不中断，facts 已写入但无 verdict | HEARTBEAT 可检测（P1） |
| Verifier 输出格式不符 | 原始输出存为 `.verdict.raw.yaml` | verdict 不可机读，保留供人工查看 |

---

## 7️⃣ 安全设计与性能

### 隔离策略

| 组件 | 隔离措施 |
|------|---------|
| 护栏只读 | 存 `.harness/guardrails/`，写入权归大鸟/Amanda，agent 只读；PROTOCOL.md 声明 |
| Verifier | tools=Read/Bash/Grep，无 sessions_history；输入仅 facts 路径 + guardrails 目录 |
| Extractor | tools 为空，唯一输入是对话原文 attachment，不访问文件系统 |

### 性能目标

gate check ≤5s（纯文件检查）/ extractor ≤60s（单次 LLM）/ verifier ≤120s / /wrap 总增加 ≤3min

---
