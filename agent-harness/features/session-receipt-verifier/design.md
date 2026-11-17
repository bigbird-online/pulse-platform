---
feature_name: "session-receipt-verifier"
version: "0.0.1"
created: "2026-04-03T12:00:00+0800"
modified: "2026-04-03T12:00:00+0800"
upstream_dependency: ["project-architecture.md", "requirements.md"]
tags: [design, l2-spec, phase2, verification, observability]
---

# session-receipt-verifier - 技术设计文档

> **设计概述**：在 agent-harness 项目中新增 receipt/verdict 数据模型（Pydantic），定义一个 OC session-verifier sub-agent，修改 /wrap 协议集成提取和验证流程。
>
> **关联文档**：
> - L1项目架构：`project-specs/project-architecture.md`
> - L2功能需求：`features/session-receipt-verifier/requirements.md`
> - L2任务拆分：`features/session-receipt-verifier/tasks.md`

---

## 1️⃣ 技术栈（Tech Stack）

### 技术栈引用

**基于L1项目架构**：`project-specs/project-architecture.md`

- **编程语言**：Python 3.11+（遵循 L1 定义）
- **数据模型**：Pydantic v2（遵循 L1 定义，schema 即校验）
- **序列化**：PyYAML（遵循 L1 定义，YAML 文件存储）
- **CLI 框架**：Click（遵循 L1 定义，新增 `create-from-verdict` 子命令）
- **OC sub-agent**：`.claude/agents/*.md` 定义文件（OC 原生机制，无新依赖）

### 技术选型说明

- receipt/verdict 复用 harness 现有的 Pydantic + YAML 模式——同一个项目内，同一套工具链，同一种存储格式
- verifier 作为 OC sub-agent 而非 Python 脚本：verifier 需要理解自然语言 claims 并选择验证策略（读文件 vs 跑脚本 vs 判断不可验证），这是 LLM 的能力，不是规则引擎能做的

---

## 2️⃣ 架构设计（Architecture Design）

### 组件划分

```
session-receipt-verifier 功能
├── Schema 层（receipt.py）── Pydantic models 定义 receipt + verdict 结构
├── Agent 定义层           ── session-verifier.md sub-agent 定义
├── CLI 层（cli.py 扩展）  ── create-from-verdict 命令
└── 协议层（AGENTS.md）    ── /wrap 流程变更
```

### 与现有模块的关系

```
harness/
├── models.py      # 现有：WorkItem / HandoffContract / SignalEvent
├── machine.py     # 现有：状态机（不修改）
├── store.py       # 现有：YAML CRUD（不修改）
├── cli.py         # 现有：CLI 命令（扩展：加 create-from-verdict）
└── receipt.py     # 新增：Receipt + Verdict Pydantic models + 序列化

.claude/agents/
├── ssot-judge.md          # 现有
├── session-extractor.md   # 新增：从原始对话提取结构化 receipt
└── session-verifier.md    # 新增：独立验证 receipt claims
```

### 数据流总览

```
[Agent session 工作中...]
         │
         ▼
[/wrap 触发]
         │
    ┌────▼──────────┐
    │ 主 agent       │
    │ sessions_history│── 调用 OC sessions_history 获取自身对话原文
    └────┬──────────┘
         │ 对话原文作为 attachment
         ▼
    ┌────────────────────┐
    │ session-extractor   │── 独立 sub-agent，收到原始对话 attachment
    │ (OC sub-agent)      │── 按 Receipt schema 提取 claims/decisions/outputs
    └────┬───────────────┘
         │ 返回 receipt YAML
         ▼
  memo/receipts/YYYY-MM-DD-NNN.yaml  (receipt)
         │
    ┌────▼────────────┐
    │ session-verifier │── 独立 sub-agent，只看到 receipt + output 文件
    │ (OC sub-agent)   │── 看不到对话内容，也不知道 extractor 的过程
    └────┬────────────┘
         │ 返回 verdict
         ▼
  memo/receipts/YYYY-MM-DD-NNN.verdict.yaml
         │
    ┌────▼────┐
    │ 主 agent │── 读取 verdict
    │ 汇报结果 │── 如有 FAIL → harness create-from-verdict (P1)
    └────┬────┘
         │
         ▼
  [继续现有 /wrap 步骤：progress.md → git commit]
```

### 上下文传递机制

**为什么 extractor 不能自己读取父 session 对话？**

OC 的 `sessions_history` 工具受 visibility 策略限制（默认 `tree`）。sub-agent 只能读取自身 session 和自己 spawn 的子 session，不能读取**父 session**。源码确认：`sessions-access.ts` 中 `tree` 级别检查 `spawnedKeys?.has(targetSessionKey)`——父 session 不在 spawned 集合中。

**解决方案：attachment 传递原始对话**

OC spawn 支持 `attachments` 参数（`subagent-spawn.ts` L65-71）：

```typescript
attachments?: Array<{
  name: string;
  content: string;
  encoding?: "utf8" | "base64";
  mimeType?: string;
}>;
```

主 agent 在 /wrap 时：
1. 调用 `sessions_history` 读取自己的完整对话记录（主 agent 有权读取自身 session）
2. 将原始对话文本作为 `attachment` 传给 session-extractor
3. extractor 从 attachment 中读取原始对话，独立提取

**为什么这不等于自提取？**

关键区别：主 agent 做的是**机械性的转录传递**（调用 `sessions_history` 获取原文），不做任何筛选或摘要。提取判断（哪些是 claims、哪些是 decisions、什么该标记什么该忽略）由 extractor 独立完成。类比：法庭上录音带是客观证据，但做庭审记录的书记员是独立于当事人的。

**sessions_history 的限制**

`sessions_history` 有 80KB 上限（`SESSIONS_HISTORY_MAX_BYTES`），文本每条消息截断到 4000 字符。对于超长 session，部分对话内容可能被截断。这是机械性限制，不是选择性偏差。缓解：extractor 在 receipt 中标注 `transcript_truncated: true`（如果 attachment 包含 `…(truncated)…` 标记）。

**Fallback：无 sessions_history 的环境**

某些环境（如 Cowork、CC 直连）没有 `sessions_history` 工具。Fallback 方案：主 agent 从自己的上下文窗口中构造对话摘要作为 attachment 传给 extractor。这不如原始转录客观，但仍然比自提取好——提取判断依然由独立 agent 完成。receipt 中标注 `transcript_source: "context_window"` 以区分 `transcript_source: "sessions_history"`。

---

## 3️⃣ 接口定义（Interface Definition）

### Pydantic Models（harness/receipt.py）

**Model-001：ReceiptOutput**

```python
class ReceiptOutput(BaseModel):
    """A single output artifact from the session."""
    path: str           # 文件路径（相对于 workspace 根）
    type: str           # 产出物类型：report / code / config / data / design_discussion
```

**Model-002：ReceiptTask**

```python
class ReceiptTask(BaseModel):
    """Task description extracted from the session."""
    description: str        # 一句话任务描述
    requester_intent: str   # 发起者的真实意图
```

**Model-003a：Decision**

```python
class Decision(BaseModel):
    """A decision made during the session — what was chosen, what was rejected, and why."""
    what: str               # 决策主题（一句话）
    chose: str              # 选择了什么
    rejected: str           # 拒绝了什么（可多项用逗号分隔）
    rationale: str          # 为什么这么选
```

**Model-003b：Receipt**

```python
class Receipt(BaseModel):
    """Structured session behavior record."""
    session_id: str                     # 格式：YYYY-MM-DD-{agent}-NNN
    agent: str                          # agent 名称
    period: str                         # ISO 8601 interval: start/end+tz
    task: ReceiptTask
    outputs: list[ReceiptOutput]        # 可以为空（纯讨论 session）
    claims: list[str]                   # ≥1 条，agent 的显式声明
    decisions: list[Decision]           # ≥0 条，session 中做出的决策（选了什么、拒绝了什么、为什么）
    verification_hints: list[str]       # ≥0 条，验证提示
    transcript_source: str              # "sessions_history" | "context_window"——标注对话来源
    transcript_truncated: bool          # sessions_history 是否发生了截断

    @field_validator("claims")
    @classmethod
    def validate_claims_nonempty(cls, v: list[str]) -> list[str]:
        if len(v) < 1:
            raise ValueError("claims must have at least 1 entry")
        return v
```

> **decisions 字段设计说明**：decisions 是行为审计的核心字段。没有它，receipt 只能记录"做了什么"，不能回答"为什么这么做"。对于纯讨论 session，decisions 往往是最有价值的信息——讨论的目的就是做决策。字段设为 list（≥0）而非必填：有些 session 纯执行不做决策（如跑测试），此时 decisions 为空是合理的。

**Model-004：ClaimResult**

```python
class Verdict(str, Enum):
    PASS = "PASS"
    FAIL = "FAIL"
    UNVERIFIABLE = "UNVERIFIABLE"

class ClaimResult(BaseModel):
    """Verification result for a single claim."""
    claim: str              # 原文复制自 receipt
    verdict: Verdict
    evidence: str           # 判定依据（事实陈述，非主观评价）
    verification_method: str # "profile:<profile_id>" 或 "ad_hoc"
```

**Model-005：VerdictSummary**

```python
class VerdictSummary(BaseModel):
    """Aggregated verification result."""
    total: int
    passed: int             # verdict == PASS
    failed: int             # verdict == FAIL
    unverifiable: int       # verdict == UNVERIFIABLE
```

**Model-006：SessionVerdict**

```python
class SessionVerdict(BaseModel):
    """Complete verification output from session-verifier."""
    receipt_path: str
    verified_at: datetime
    results: list[ClaimResult]
    summary: VerdictSummary
    action_items: list[str]     # 从 FAIL 结果中提取的修复项
```

### Verification Profiles

**Model-007：VerificationAction**

```python
class VerificationAction(BaseModel):
    """How to verify a claim that matches this profile."""
    type: str                       # "command" | "file_exists" | "grep"
    command: str | None = None      # type=command: shell 命令（支持 {project_root} 占位符）
    path_extract: str | None = None # type=file_exists: 从 claim 文本中提取路径的正则
    grep_pattern: str | None = None # type=grep: 搜索的内容模式
    grep_target: str | None = None  # type=grep: 搜索的目标文件/目录
    pass_condition: str             # 人类可读的通过条件描述，verifier 据此判断
```

**Model-008：VerificationProfile**

```python
class VerificationProfile(BaseModel):
    """A reusable rule: when a claim matches these patterns, verify it this way."""
    profile_id: str                 # 唯一标识，如 "test-pass", "coverage", "file-exists"
    description: str                # 一句话说明
    claim_patterns: list[str]       # 正则模式列表，任一匹配即激活此 profile
    verification: VerificationAction
    tags: list[str] = []            # 分类标签，如 ["code", "test"] 或 ["doc", "structure"]
```

> **profile 匹配逻辑**：verifier 对每个 claim，按 profiles 列表顺序逐个匹配 `claim_patterns`。第一个匹配的 profile 生效。无 profile 匹配时 fallback 到 ad_hoc（verifier 临场判断）。

**Profile 文件格式（YAML）**

每个 profile 是一个独立的 YAML 文件，存在 agent workspace 的 `verification-profiles/` 目录下：

```yaml
# verification-profiles/test-pass.yaml
profile_id: test-pass
description: "验证测试全部通过的声明"
claim_patterns:
  - "\\d+\\s*tests?.*pass"
  - "tests?\\s*(all\\s*)?pass"
  - "\\d+p/\\d+f"
  - "全部通过"
verification:
  type: command
  command: "cd {project_root} && python -m pytest --tb=short -q 2>&1 | tail -5"
  pass_condition: "pytest 退出码为 0，且输出中 'failed' 数为 0"
tags: [code, test]
```

```yaml
# verification-profiles/coverage.yaml
profile_id: coverage
description: "验证测试覆盖率声明"
claim_patterns:
  - "\\d+%\\s*cov"
  - "coverage\\s*[≥>=]?\\s*\\d+"
  - "覆盖率"
verification:
  type: command
  command: "cd {project_root} && python -m pytest --cov --cov-report=term-missing -q 2>&1 | grep TOTAL"
  pass_condition: "输出中的覆盖率百分比 ≥ claim 中声称的百分比"
tags: [code, test]
```

```yaml
# verification-profiles/lint-clean.yaml
profile_id: lint-clean
description: "验证代码检查通过声明"
claim_patterns:
  - "ruff.*(clean|pass|通过|0\\s*error)"
  - "mypy.*(clean|pass|通过|0\\s*error)"
  - "lint.*(clean|pass|通过)"
verification:
  type: command
  command: "cd {project_root} && ruff check . 2>&1; echo '---'; mypy --strict . 2>&1 | tail -3"
  pass_condition: "ruff 和 mypy 退出码均为 0"
tags: [code, quality]
```

```yaml
# verification-profiles/file-exists.yaml
profile_id: file-exists
description: "验证文件已生成/已创建的声明"
claim_patterns:
  - "(生成|创建|写入|saved?|created?|wrote|produced?).*\\.(md|yaml|py|ts|json|csv|html)"
  - "\\.(md|yaml|py|ts|json).*已(生成|创建|写入)"
  - "文件.*存在"
verification:
  type: file_exists
  path_extract: "[\\w/.-]+\\.\\w+"
  pass_condition: "文件存在且大小 > 0 字节"
tags: [doc, file]
```

```yaml
# verification-profiles/structure-complete.yaml
profile_id: structure-complete
description: "验证文档结构完整性声明"
claim_patterns:
  - "(三件套|L2.*完成|requirements.*design.*tasks)"
  - "\\d+\\s*(个|条).*章节"
  - "结构.*完整"
verification:
  type: grep
  grep_pattern: "^## "
  grep_target: "{claimed_file}"
  pass_condition: "grep 匹配到 ≥ 预期数量的 H2 标题"
tags: [doc, structure]
```

**目录结构**

```
{oa-workspace}/
├── verification-profiles/           # 本 agent 的验证规则集
│   ├── test-pass.yaml
│   ├── coverage.yaml
│   ├── lint-clean.yaml
│   ├── file-exists.yaml
│   └── structure-complete.yaml
├── memo/receipts/                   # receipt + verdict 文件
└── ...

agent-harness/
├── harness/
│   └── receipt.py                   # 含 VerificationProfile model
└── templates/
    └── verification-profiles/       # 通用模板（各 agent 复制后定制）
        ├── test-pass.yaml
        ├── coverage.yaml
        ├── lint-clean.yaml
        ├── file-exists.yaml
        └── structure-complete.yaml
```

- `agent-harness/templates/verification-profiles/`：通用模板，各 agent 的起点
- `{oa-workspace}/verification-profiles/`：各 agent 实例化后的版本，可按领域定制（如 Steward 加金融数据校验 profile，Aki 加知识完整性 profile）

**Verifier 验证策略变更**

verifier 的验证流程从"临场发挥"变为"先匹配 profile，匹配不到才 ad_hoc"：

```
对每个 claim:
    1. 加载 {oa-workspace}/verification-profiles/*.yaml
    2. 按文件名排序，逐个匹配 claim_patterns
    3. 如果匹配到 profile:
       - 按 profile.verification 执行验证
       - ClaimResult.verification_method = "profile:{profile_id}"
    4. 如果无 profile 匹配:
       - verifier 自行选择验证策略（读文件/跑脚本/标记 UNVERIFIABLE）
       - ClaimResult.verification_method = "ad_hoc"
```

> **为什么 ad_hoc 保留？** 不可能预定义所有 claim 类型的 profile。新类型的 claim 先走 ad_hoc，如果某类 claim 反复出现且 ad_hoc 验证不稳定，就为它创建一个 profile——这就是 Phase 3 "每次发现错误就改 harness"（Hashimoto 原则）的具体体现。

### CLI 命令（P1：cli.py 扩展）

**Command-001：create-from-verdict**

```python
@main.command("create-from-verdict")
@click.argument("verdict_path", type=click.Path(exists=True))
@click.option("--owner", required=True, help="Work item owner")
@click.pass_context
def create_from_verdict(ctx, verdict_path, owner):
    """Create a work item from FAIL results in a verdict file."""
    # 1. 读取 verdict YAML，解析为 SessionVerdict
    # 2. 过滤 verdict == FAIL 的 results
    # 3. 如果无 FAIL，输出 "No failures, no work item created" 并退出
    # 4. 创建 WorkItem，title = "Fix: {n} failed claims from {receipt_path}"
    # 5. acceptance_criteria = action_items
    # 6. 调用 store.save_work_item + store.append_event
    # 7. 输出 work_item_id
```

### OC Agent 定义

**Agent-001：session-extractor**

文件：`.claude/agents/session-extractor.md`

```yaml
---
name: session-extractor
description: 从原始对话记录中提取结构化 session receipt。独立于主 agent，客观提取 claims 和 decisions。
tools: []
model: sonnet
color: orange
---
```

System prompt 要点：
- 输入：原始对话记录（通过 attachment 传入）
- 从对话中提取：task 描述、outputs 列表、claims（agent 的显式声明）、decisions（选了什么/拒绝了什么/为什么）、verification_hints
- claims 提取原则：**宁多不漏**——任何 agent 声称做到的、声称正确的、声称完成的都算 claim；不确定是否属实的也提取出来让 verifier 判断
- decisions 提取原则：对话中出现"选择 A 而不是 B"、"决定做 X"、"放弃 Y"、用户纠正后改变方向——这些都是 decisions
- 输出：严格遵循 Receipt YAML schema
- 禁止：不做任何验证判断（这是 verifier 的事）、不遗漏不利的 claims、不美化失败或偏差
- 特殊：tools 为空——extractor 不需要读文件或跑脚本，它的唯一输入是对话文本 attachment

> **为什么 tools 为空？** extractor 的职责是从对话文本中提取信息，不需要访问文件系统。给它工具反而会引入干扰（比如它可能去读 MEMORY.md 来"补充"信息，而不是客观提取对话中实际说了什么）。

**Agent-002：session-verifier**

文件：`.claude/agents/session-verifier.md`

```yaml
---
name: session-verifier
description: 独立验证 session receipt 中的 claims。只看 receipt YAML + 产出物文件，不看生成过程。
tools: Read, Bash, Grep
model: sonnet
color: green
---
```

System prompt 要点：
- 输入：receipt YAML 文件路径 + verification-profiles 目录路径
- 对 receipt 中每个 claim：先加载 verification-profiles/*.yaml，按 claim_patterns 匹配；匹配到 profile 则按 profile.verification 执行；无匹配则 ad_hoc 验证
- ad_hoc 验证方式优先级：文件存在性检查 > 脚本执行验证 > 内容搜索验证 > 标记 UNVERIFIABLE
- 输出：verdict YAML（严格遵循 SessionVerdict schema），每个 ClaimResult 含 verification_method 字段
- 禁止：访问 session 对话内容、修改任何文件、执行破坏性命令、忽略 profile 匹配结果

---

## 4️⃣ 数据模型（Data Model）

### 文件存储布局

```
{oa-workspace}/memo/receipts/
├── 2026-04-03-ken-001.yaml           # Receipt
├── 2026-04-03-ken-001.verdict.yaml   # Verdict
├── 2026-04-04-ken-001.yaml
├── 2026-04-04-ken-001.verdict.yaml
└── ...
```

### 命名规则

- Receipt：`YYYY-MM-DD-{agent}-NNN.yaml`
- Verdict：`YYYY-MM-DD-{agent}-NNN.verdict.yaml`
- NNN：同一天同一 agent 的序号，从 001 开始

### 与现有数据模型的关系

```
WorkItem (现有)
    ↑
    │ create-from-verdict (P1) 创建
    │
SessionVerdict (新增)
    ↑
    │ verifier 生成
    │
Receipt (新增)
    ↑
    │ 主 agent /wrap 时生成
    │
[Session 对话] (不存储)
```

Receipt 和 Verdict 不进入 `.harness/` 目录——它们是 agent 行为记录，归 agent 自己的 workspace。只有当 verdict 发现 FAIL 并触发 `create-from-verdict` 时，才通过 WorkItem 进入 `.harness/` 跨 agent 协作层。

---

## 5️⃣ 数据流（Data Flow）

### Flow-001：/wrap 收据生成与验证

```
/wrap 触发
    │
    ├─→ [1] 主 agent 调用 sessions_history 获取自身对话原文
    │       • 机械性操作，不做筛选
    │       • 如 sessions_history 不可用，fallback 到 context window 摘要
    │
    ├─→ [2] Spawn session-extractor sub-agent
    │       • 传入：对话原文作为 attachment（name: "transcript.md"）
    │       • extractor 独立从对话中提取 claims/decisions/outputs
    │       • extractor 返回 receipt YAML
    │
    ├─→ [3] 主 agent 将 receipt 写入 memo/receipts/YYYY-MM-DD-{agent}-NNN.yaml
    │
    ├─→ [4] Spawn session-verifier sub-agent
    │       • 传入：receipt 文件路径
    │       • 不传入：对话内容（verifier 不知道 session 发生了什么）
    │
    ├─→ [5] Verifier 逐条验证 claims
    │       • 对每个 claim 选择验证策略
    │       • 读取产出物文件 / 跑验证脚本
    │       • 输出 PASS / FAIL / UNVERIFIABLE + evidence
    │
    ├─→ [6] 主 agent 将 verdict 写入 memo/receipts/...verdict.yaml
    │
    ├─→ [7] 主 agent 汇报验证结果摘要
    │       • "Receipt: 4 claims — 3 PASS, 0 FAIL, 1 UNVERIFIABLE"
    │
    └─→ [8] 继续现有 /wrap 步骤
            • progress.md
            • git commit（含 receipt + verdict）
```

### Flow-002：FAIL → harness work item（P1）

```
Verdict 包含 FAIL
    │
    ├─→ [1] 主 agent 执行 `harness create-from-verdict <verdict-path> --owner <agent>`
    │
    ├─→ [2] CLI 解析 verdict，提取 FAIL 项 + action_items
    │
    ├─→ [3] 创建 WorkItem（draft），criteria = action_items
    │
    └─→ [4] git commit（.harness/ 变更）
```

---

## 6️⃣ 错误处理（Error Handling）

### 异常场景处理

**Error-001：Receipt 校验失败**
- **触发条件**：主 agent 生成的 receipt YAML 不符合 Pydantic schema（如缺少 claims 字段）
- **处理策略**：/wrap 流程不中断，但在汇报中警告 "Receipt validation failed: {error}"，写入一份带 `_invalid` 后缀的文件供人工检查
- **影响**：verifier 不被 spawn（无有效 receipt 可验证）

**Error-002：Verifier spawn 失败**
- **触发条件**：OC sub-agent 机制故障、模型配额不足
- **处理策略**：/wrap 流程不中断，receipt 已写入，verdict 缺失。在汇报中说明 "Verifier unavailable, receipt saved without verification"
- **影响**：receipt 存在但无 verdict——HEARTBEAT 审计可检测到（receipt 存在但 verdict 缺失）

**Error-003：Verifier 输出格式不符**
- **触发条件**：verifier 返回的内容不符合 SessionVerdict schema
- **处理策略**：主 agent 将原始输出存为 `...verdict.raw.yaml`，在汇报中警告
- **影响**：verdict 不可机读，但原始内容保留供人工查看

**Error-004：Extractor spawn 失败或输出格式不符**
- **触发条件**：session-extractor sub-agent spawn 失败、超时或返回不符合 Receipt schema 的内容
- **处理策略**：/wrap 流程不中断。主 agent fallback 到自提取——自己按 Receipt schema 填写一份 receipt，标注 `transcript_source: "self_extract_fallback"`。自提取的 receipt 仍然交给 verifier 验证。在汇报中警告 "Extractor unavailable, fell back to self-extraction"
- **影响**：receipt 质量降级（回到自提取模式），但 verifier 验证仍然独立

**Error-005：Verdict 文件路径不存在（create-from-verdict）**
- **触发条件**：CLI 收到不存在的 verdict 路径
- **处理策略**：输出 "Error: verdict file not found: {path}" 并退出码 1

---
