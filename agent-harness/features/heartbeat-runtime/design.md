---
feature_name: "heartbeat-runtime"
version: "0.0.1"
created: "2026-04-19T10:30:00+0800"
modified: "2026-04-19T10:30:00+0800"
upstream_dependency:
  - "project-specs/project-architecture.md"
  - "features/heartbeat-runtime/requirements.md"
  - "../../PULSE/Relay/specs/features/agent-scheduler/design.md"
tags: [design, l2-spec, heartbeat, runtime, autonomy, dispatcher, cc-spawn]
---

# heartbeat-runtime - 技术设计文档

> **设计概述**：在 agent-harness（Python）新增 4 个模块——`policy.py`（声明式策略模型）、`scan.py`（机械档扫描引擎）、`dispatcher.py`（三档分流 + CC 分身 spawn）、`escalation.py`（升级档 aggregator + push），扩展 `cli.py` 暴露 `scan / dispatch / escalation` 三个子命令组。Relay 侧仅新增两条 `cron-jobs.json` 条目（外层 scan 触发 + daily flush），不改 `scheduler.ts` 源码。受限 LLM 档的 CC 分身由 harness dispatcher **直接 subprocess.spawn**（不经 Relay scheduler），以 `--allowedTools` 硬约束 autonomy 边界。
>
> **关联文档**：
> - L1 架构：`project-specs/project-architecture.md`（三域模块架构 + CC/Relay 基础设施）
> - L2 需求：`features/heartbeat-runtime/requirements.md`（三档 autonomy + Reeve 样板 + AC 12 条）
> - Relay 侧接口：`PULSE/Relay/specs/features/agent-scheduler/design.md`

---

## 1. 技术栈（Tech Stack）

**基于 `project-specs/project-architecture.md` 定义的技术栈**：

| 层 | 选型 | 来源 |
|----|------|------|
| 语言 | Python 3.11+ | L1 约束 |
| CLI 框架 | Click（复用已有子命令组） | L1 约束 |
| 数据模型 | Pydantic v2 | L1 约束 |
| 序列化 | PyYAML（policy）+ JSON（events）| L1 约束 |
| 测试 | pytest + pytest-cov | L1 约束 |
| 子进程 | `subprocess.Popen`（标准库）— 用于 spawn CC 分身 | feature 新增依赖点（无新 pip 依赖）|
| 文件锁 | `fcntl.flock`（Unix 标准库） | feature 新增依赖点 |
| TG 推送 | 本机 `curl` + Relay bot token（复用 Relay 早报 EPUB push 的方式，不引入 Python TG SDK） | feature 新增依赖点 |

**无新 pip 依赖**。所有新增能力走标准库。

---

## 2. 架构设计（Architecture Design）

### 新增模块（harness/）

```
harness/
├── policy.py              # 新增：HeartbeatPolicy Pydantic 模型 + load_policy
├── scan.py                # 新增：ScanReport 模型 + 扫描引擎（复用 store/ssot_engine）
├── dispatcher.py          # 新增：Dispatcher + spawn_autonomous_session + tool allow-list 注入
├── escalation.py          # 新增：EscalationEvent 模型 + aggregator + push（curl TG）
├── events.py              # 新增：统一事件 JSONL append helper（5 类事件共用）
├── cli.py                 # 扩展：新增 scan / dispatch / escalation / policy 子命令组
├── store.py               # 复用（list pending handoffs / unresolved VIOLATED）
├── ssot_engine.py         # 复用（quick check 获取 SSOT drift）
└── ...                    # 其他模块不动
```

### 与现有模块的关系

| 现有模块 | 本 feature 交互方式 |
|---------|-------------------|
| `models.py` | 只读 WorkItem / HandoffContract / SignalEvent 定义；本 feature 新模型独立 |
| `store.py` | 调 `list_handoffs(to_agent=..., state="pending")` 与 `list_verdicts(agent=..., status="VIOLATED", unresolved=True)`（后者若不存在则本 feature 增薄 wrapper，不改 store 核心）|
| `ssot_engine.py` | 调 `check_agent(agent_id, quick=True)` 获取漂移 findings |
| `guardrail.py` / `gate_engine.py` / `compliance_engine.py` | 不交互 |
| `cli.py` | 追加新子命令组，不改已有命令 |

### 模块关系图

```
Relay agent-scheduler (cron)
 └─ heartbeat-<agent>-scan job (*/15 * * * *)
    └─ spawn `claude -p "harness scan <agent> --mode cron && harness dispatch <scan_id>"`
       │   (外层壳复用 scheduler spawn + timeout + 熔断)
       │
       └─► harness.cli: scan 子命令
             └─► scan.py :: run_scan(agent_id)
                    ├─ store.list_handoffs(to=agent, state=pending)
                    ├─ store.list_unresolved_violations(agent)
                    ├─ ssot_engine.check_agent(agent, quick=True)
                    ├─ policy.load_policy(agent)     # 为后续 dispatcher 准备
                    └─► events.append("scan_event", ...)
                         返回 ScanReport（JSON 到 stdout）

       └─► harness.cli: dispatch 子命令 (scan 返回 exit=1 时执行)
             └─► dispatcher.py :: dispatch(scan_report)
                    ├─ 按 policy.per_action 分类每条命中
                    ├─ 机械档 → events.append("dispatch_event", disposition=recorded)
                    ├─ 受限 LLM 档 → spawn_autonomous_session(合并 context)
                    │    └─ subprocess.Popen(
                    │         ["claude", "-p", prompt,
                    │          "--allowedTools", ",".join(policy.allowed_tools),
                    │          "--disallowedTools", ",".join(policy.disallowed_tools),
                    │          "--permission-mode", "bypassPermissions"],
                    │         cwd=agent_workspace 或 NIGHT_PATROL_PROFILE 临时 capsule,
                    │         env={...without TELEGRAM_BOT_TOKEN,
                    │              HARNESS_TRIGGER_MODE=cron,
                    │              HARNESS_CONTEXT_PROFILE=<profile>})
                    │    └─ events.append("autonomous_execution_event", ...)
                    └─ 升级档 → events.append("escalation_event", ...)
                         └─ 若 push_immediately → escalation.push_now(event)

Relay agent-scheduler (cron)
 └─ heartbeat-daily-flush job (0 20 * * *)
    └─ spawn `claude -p "harness escalation flush-daily"`
       └─► escalation.py :: flush_daily_aggregate()
              ├─ 读 .harness/events/escalation-YYYY-MM.jsonl 当日 aggregate_daily 事件
              ├─ 合并成一条日报
              └─ curl TG bot 发送
```

---

## 3. 接口定义（Interface Definition）

### CLI 命令

**CMD-001：`harness scan <agent_id>`**
- **选项**：`--mode [cron|manual|session_start]`（默认 manual）/ `--json`（JSON 输出，cron 模式默认开）/ `--registry <path>`（SSOT registry 路径）
- **行为**：执行机械扫描，产 ScanReport，追加 scan_event
- **输出**（--json 模式）：
  ```json
  {
    "scan_id": "sc_20260419_103000_reeve_a1b2",
    "agent_id": "reeve",
    "trigger_mode": "cron",
    "scanned_at": "2026-04-19T10:30:00+0800",
    "duration_ms": 1234,
    "hits": {
      "pending_handoffs": [{"handoff_id": "ho_...", "wi_id": "wi_...", "from_agent": "ken"}],
      "unresolved_violations": [],
      "ssot_drift": []
    },
    "hits_count": 1,
    "verdict": "hits"
  }
  ```
- **退出码**：0=clean / 1=有命中 / 2=错误

**CMD-002：`harness dispatch <scan_id>`**
- **选项**：`--scan-report <path>`（如不提供，从 `.harness/scan-reports/<scan_id>.yaml` 读）/ `--dry-run`（只打印决策不 spawn）
- **行为**：读 scan report + 目标 Agent policy，按 per_action 分流；机械档直接 event；受限 LLM 档 spawn CC 分身；升级档 event + 可选即时 push
- **退出码**：0=全部成功分流 / 1=有分身异常退出 / 2=policy 缺失或非法

**CMD-003：`harness escalation flush-daily`**
- **行为**：聚合今日 aggregate_daily 事件 → 一条 TG 汇总 push → 标记已发送
- **退出码**：0=成功（含零事件情形 silently exit） / 1=push 失败

**CMD-004：`harness policy validate <agent_id>`**
- **行为**：读 Agent policy.yaml → Pydantic 校验 → 报告合法/非法 + 详细错误
- **退出码**：0=valid / 1=invalid
- （P1 扩展为 `harness policy validate-all`）

### 内部函数接口

```python
# policy.py
def load_policy(agent_id: str, policy_root: Path = ...) -> HeartbeatPolicy: ...
def policy_path(agent_id: str) -> Path:  # 返回 .harness/policies/<agent>.yaml

# scan.py
def run_scan(agent_id: str, trigger_mode: str = "manual") -> ScanReport: ...

# dispatcher.py
def dispatch(scan_report: ScanReport, policy: HeartbeatPolicy) -> DispatchResult: ...
def spawn_autonomous_session(
    agent_id: str,
    policy: HeartbeatPolicy,
    hits: list[ScanHit],
    scan_id: str,
) -> AutonomousExecutionEvent: ...

# escalation.py
def record_escalation(event: EscalationEvent) -> None: ...       # 写 jsonl，按 policy 决定是否 push_now
def push_now(event: EscalationEvent) -> bool: ...                # 即时 TG curl
def flush_daily_aggregate(today: date | None = None) -> int: ... # 返回推送条数

# events.py
def append_event(event_type: str, payload: dict) -> None: ...    # 统一写 .harness/events/<type>-YYYY-MM.jsonl
```

---

## 4. 数据模型（Data Model）

### Model-001：HeartbeatPolicy（policy.py）

```python
class PerActionRule(BaseModel):
    match: str                              # 命中类型，如 "format_compliant" / "cross_owner_boundary"
    autonomy: Literal["mechanical", "autonomous_llm", "escalate"]

class ScanItemPolicy(BaseModel):
    type: Literal["pending_handoffs", "unresolved_violations", "ssot_drift"]
    per_action: list[PerActionRule]         # 第一条匹配的生效（顺序敏感）
    default_autonomy: Literal["mechanical", "autonomous_llm", "escalate"] = "mechanical"

class AutonomousLLMConfig(BaseModel):
    allowed_tools: list[str]
    disallowed_tools: list[str] = Field(default_factory=list)
    path_constraints: dict[str, list[str]]  # {"read": [...], "write": [...]}，仅文档化不强制（强制在 CC tool 层）
    max_duration_seconds: int = 180
    session_wrap_required: bool = True

class EscalationRules(BaseModel):
    push_immediately: list[str]             # 匹配类型枚举：red_line_flag / circuit_broken / critical_count>=3 / ...
    aggregate_daily: list[str]
    channel: Literal["telegram"] = "telegram"
    recipient: str                          # TG chat_id

class HeartbeatPolicy(BaseModel):
    schema_version: str = "1.0"
    agent_id: str
    frequency_hint: str                     # cron expr，仅供 cron-jobs.json 对齐参考
    workspace: Path                         # Agent workspace 绝对路径（cwd for spawn）
    scan_items: list[ScanItemPolicy]
    autonomous_llm_config: AutonomousLLMConfig
    escalation: EscalationRules

    @model_validator(mode="after")
    def _validate_per_action_match(self) -> "HeartbeatPolicy":
        # 校验：allowed_tools 非空；recipient 非空；path_constraints 键值合法
        ...
        return self
```

**校验规则**：
- `schema_version` 必须 "1.0"（P1 演进时 bump）
- `autonomous_llm_config.allowed_tools` 非空（否则受限 LLM 档无法做任何事）
- `escalation.recipient` 非空
- `per_action` 中 `match` 取值限定在 **matched types registry**（scan.py 暴露的已知命中类型枚举）

### Model-002：ScanReport（scan.py）

```python
class ScanHit(BaseModel):
    hit_type: str                           # "pending_handoffs" / "unresolved_violations" / "ssot_drift"
    match_label: str                        # 由 classifier 决定，如 "format_compliant" / "red_line_flag" / "critical"
    ref_id: str                             # handoff_id / verdict_id / drift_finding_id
    wi_id: str | None = None
    from_agent: str | None = None           # handoff 场景
    severity: Literal["critical", "major", "minor", "info"] = "info"
    evidence_path: str | None = None        # 不内嵌原文

class ScanReport(BaseModel):
    scan_id: str                            # sc_<YYYYMMDD>_<HHMMSS>_<agent>_<rand4>
    agent_id: str
    trigger_mode: Literal["cron", "manual", "session_start"]
    scanned_at: datetime
    duration_ms: int
    hits: list[ScanHit]
    verdict: Literal["clean", "hits", "error"]
    error: str | None = None
```

### Model-003：5 类事件（events.py + dispatcher.py + escalation.py）

所有事件共享 base schema，追加到 `.harness/events/<event_type>-YYYY-MM.jsonl`：

```python
class EventBase(BaseModel):
    schema_version: str = "1.0"
    event_id: str                           # ev_<scan_id>_<seq>
    event_type: str
    agent_id: str
    scan_id: str | None = None              # scan-related 事件含；daily-flush 不含
    trigger_mode: str | None = None
    occurred_at: datetime

class ScanEvent(EventBase):
    event_type: Literal["scan_event", "scan_skipped"] = "scan_event"
    duration_ms: int
    hits_count: int
    verdict: Literal["clean", "hits", "error"]
    skip_reason: str | None = None          # 仅 scan_skipped 场景

class DispatchEvent(EventBase):
    event_type: Literal["dispatch_event"] = "dispatch_event"
    dispositions: list[dict]                # [{hit_ref: ..., autonomy: ..., outcome: ...}]

class AutonomousExecutionEvent(EventBase):
    event_type: Literal["autonomous_execution_event"] = "autonomous_execution_event"
    started_at: datetime
    duration_ms: int
    exit_code: int
    status: Literal["success", "wrap_missing", "timeout", "tool_denied", "error", "deferred", "blocked"]
    tool_calls_count: int | None = 0   # None = 没数过；0 = 数出来是 0
    tool_denied_calls: list[str] = []

class EscalationEvent(EventBase):
    event_type: Literal["escalation_event"] = "escalation_event"
    severity: Literal["critical", "major", "minor"]
    match_label: str
    aggregate_policy: Literal["push_immediately", "aggregate_daily"]
    pushed_at: datetime | None = None       # 若已 push
    push_status: Literal["pending", "sent", "failed"] = "pending"
    ref_ids: list[str]                      # 涉及的 handoff/wi/drift 引用
    message: str                            # 精简提示文本（不含原文内容）
```

**隐私边界**：所有事件 schema **不含** handoff 的 input_summary / expected_output 原文、WorkItem 业务字段、CC session thinking；`evidence_path` 仅指向文件路径（消费方自担读取责任，但默认不读）。

### 存储布局

```
.harness/
├── policies/
│   └── <agent_id>.yaml              # per-Agent HeartbeatPolicy YAML
├── scan-reports/
│   └── <scan_id>.json               # 每次 scan 生成，dispatcher 读；保留 7 天后可清
├── events/
│   ├── scan_event-2026-04.jsonl
│   ├── scan_skipped-2026-04.jsonl
│   ├── dispatch_event-2026-04.jsonl
│   ├── autonomous_execution_event-2026-04.jsonl
│   └── escalation_event-2026-04.jsonl
└── locks/
    └── <agent_id>.lock              # flock 保护 workspace 数据一致性
```

---

## 5. 数据流（Data Flow）

### Flow-001：无命中快速退出（85% 场景）

```
Relay cron → scheduler spawn CC 壳 → harness scan reeve --mode cron --json
    ↓
scan.run_scan("reeve"):
    ├─ 抢 .harness/locks/reeve.lock (flock non-blocking)
    │   └─ 占用 → append scan_skipped → exit 0
    ├─ 拿到锁
    ├─ store.list_handoffs(to="reeve", state="pending") → []
    ├─ store.list_unresolved_violations("reeve") → []
    ├─ ssot_engine.check_agent("reeve", quick=True) → DriftReport{findings: []}
    ├─ 构造 ScanReport(hits=[], verdict="clean", duration_ms=870)
    ├─ events.append_event("scan_event", payload)
    ├─ 释放锁
    └─ 打印 JSON，退出 0
调用方（shell）检测 exit=0 → 不 chain dispatch → CC 壳退出
总耗时 ~2s，0 LLM 调用（scan 本身不 spawn CC）
```

### Flow-002：S1 自动化（受限 LLM 档）

```
scan 发现 2 条 pending handoffs（from=ken，review 邀请）→ exit=1
    ↓
shell chain: harness dispatch <scan_id>
    ↓
dispatcher.dispatch(scan_report, policy):
    for hit in scan_report.hits:
        rule = match_rule(hit, policy.scan_items)
        # 2 条 handoff 均匹配 format_compliant → autonomous_llm
        collect into autonomous_batch
    ↓
dispatcher.spawn_autonomous_session("reeve", policy, autonomous_batch, scan_id):
    context_profile = "night_patrol" if batch contains scheduled_night_patrol else "legacy"
    prompt = build_prompt(batch, scan_id, context_profile)   # 见 §6 prompt 模板
    env = os.environ.copy()
    env.pop("TELEGRAM_BOT_TOKEN", None)
    env.update({
        "HARNESS_TRIGGER_MODE": "cron",
        "HARNESS_SCAN_ID": scan_id,
        "HARNESS_CONTEXT_PROFILE": context_profile,
        "HARNESS_WORKSPACE": policy.workspace,
    })
    cwd = policy.workspace
    add_dir_args = []
    if context_profile == "night_patrol":
        cwd = temporary_empty_capsule()
        add_dir_args = ["--add-dir", policy.workspace]
    proc = subprocess.Popen(
        ["claude", "-p", prompt,
         "--allowedTools", ",".join(policy.autonomous_llm_config.allowed_tools),
         "--disallowedTools", ",".join(policy.autonomous_llm_config.disallowed_tools),
         "--permission-mode", "bypassPermissions",
         "--output-format", "text",
         *add_dir_args],
        cwd=cwd,
        env=env,
        stdout=PIPE, stderr=PIPE,
        timeout=policy.autonomous_llm_config.max_duration_seconds + 30,
    )
    stdout, stderr = proc.communicate()
    ↓
    解析 stdout/stderr 检测：
        - exit_code
        - tool_denied 记录（stderr 扫 "tool not allowed" 模式）
        - /wrap 完整性（commit 是否在 cron 分身启动时间之后产生）
    ↓
    events.append_event("autonomous_execution_event", {
        agent_id: "reeve",
        scan_id,
        duration_ms,
        exit_code,
        status: "success" | "wrap_missing" | ...
    })

Reeve 分身侧发生的事：
    claude -p 读 prompt
    → legacy profile 按 Reeve workspace 的 CLAUDE.md 加载人格
    → NIGHT_PATROL_PROFILE 不自动读取目标 workspace CLAUDE.md / AGENTS.md；
      dispatcher 显式注入 MANAGEMENT.md
    → 读 2 条 handoff 的 evidence 文件
    → 对每条调 harness-handoff-ack 或 handoff-reply（allowed_tools 中）
    → /wrap：写 memo、git add、git commit（message 含 [cron autonomous]）
    → 退出 0

大鸟毫无感知，2 条 handoff 已清。
总耗时 ~120s，1 LLM 调用
```

### Flow-003：升级档 + 日粒度 push

```
scan 扫到：1 条 cross_owner_boundary + 3 条 critical drift
    ↓
dispatcher 分流：
    1 条 cross_owner → aggregate_daily
    3 条 critical → match push_immediately (critical_count >= 3)
    ↓
for each:
    escalation.record_escalation(event)
        - 写 escalation_event.jsonl
        - 若 event.aggregate_policy == push_immediately:
            escalation.push_now(event)
                curl TG bot: "⚠️ Reeve 检测 3 条 critical drift..."
                更新 event push_status → sent
        - 若 aggregate_daily: push_status 保持 pending，等 flush
    ↓
当日 20:00 cron：heartbeat-daily-flush
    escalation.flush_daily_aggregate():
        今日 pending + aggregate_daily 事件全取出
        合并模板："今日升级汇总：cross_owner 1 / ..."
        curl TG 发送
        逐条更新 push_status → sent
```

---

## 6. 关键设计决策

### 决策-001：受限 LLM 档的 CC 分身由 harness dispatcher 直接 spawn（不经 Relay scheduler）

**理由**：
- scheduler 职责是"按 cron 表达式唤醒",一次一个静态 job；受限 LLM 档 spawn 是扫描命中后**动态触发**（每次参数不同），不适合静态 job
- scheduler 直接 spawn 会把 Agent 语义泄漏到 Relay 层，违反 B-R6 分工
- harness 自己管 subprocess 天然可控：读 policy 注入 tool allow-list、处理 exit code、写事件——不借他人之手

**代价**：harness 与 subprocess 管理耦合，但 Python 标准库已足够，无新依赖

### 决策-002：tool allow-list 走 CC 原生 `--allowedTools`（不在 prompt 层）

**理由**：
- prompt "请不要调用 Write" 不是约束，是建议，不可靠
- CC 原生 `--allowedTools` 是运行时硬拒绝，不执行就是不执行
- 安全边界由工程机制保障（requirements 故事 3）

### 决策-003：锁协议 — flock + 10s 超时 defer（不死锁）

**理由**：
- `fcntl.flock(LOCK_EX | LOCK_NB)`：非阻塞抢锁，失败立即返回
- cron scan 抢失败 → skip（exit 0）；不等待
- cron 分身抢失败 → 10s 后重试 1 次，仍失败 → 升级事件（deferred_autonomous_session）
- 人工 session 抢失败 → 等 30s（blocking flock 带超时）；仍失败 → 报错提示"另一 cron 正在运行"让用户稍后重试
- Agent 进程崩溃 → flock 内核自动释放（不留孤儿锁）

### 决策-004：dispatcher prompt 模板

```
你被系统 cron 自动唤起，这是你的一次正常工作 session（与人工 session 同等地位）。

【扫描发现】scan_id: <scan_id>，时间: <scanned_at>
以下 N 条事项按你的 heartbeat-policy.yaml 已判定为 autonomous_llm 档，请按你 workspace 下
CLAUDE.md / AGENTS.md 的人格与流程处置。

若命中 scheduled_night_patrol，dispatcher 使用 NIGHT_PATROL_PROFILE：
- runtime cwd 是临时 capsule，避免目标 workspace CLAUDE.md / AGENTS.md 自动发现
- prompt 显式注入目标 workspace 的 MANAGEMENT.md，且它是唯一注入文件
- 真实目标 workspace 通过 HARNESS_WORKSPACE 与 --add-dir 暴露

<逐条列出：hit_type / match_label / ref_id / evidence_path>

【Autonomy 边界】
- 允许工具：<tools>
- 禁用工具：<tools>
- 最大执行时长：<seconds> 秒

【必须动作】
1. 对每条命中按你的人格做出处置（ack / reply / reject / 创建 wi）
2. 处置过程如遇超出 allowed_tools 的需求，产生升级事件而非越界：调 harness-work-item-create 并标 escalation_type
3. 处置完成后执行 /wrap：写 memo、commit（message 前缀 [cron autonomous]）
4. 退出前确认 /wrap 执行完毕
```

### 决策-005：事件 JSONL 按 event_type 分文件 + 按月分片

**理由**：
- 一个事件类型一个文件：消费方（Reeve 报告生成）按类型读，不混读
- 按月分片：单文件 ≤ 10MB，grep 快
- JSONL 追加无锁安全（单次 write 原子）

### 决策-006：autonomous session 的 /wrap 完整性检测

**机制**：dispatcher 记录 spawn 时间 T0，subprocess 退出后调 `git log --since=T0 -- <workspace>` 检查是否有 cron 分身产生的 commit（message 含 `[cron autonomous]`）；无 commit 即 `wrap_missing=True`，产即时升级事件

---

## 7. 错误处理（Error Handling）

| 场景 | 处理 | 事件 |
|------|------|------|
| Policy 文件缺失 / 非法 | scan 正常跑（不依赖 policy），dispatch 退出 2 + 升级事件 | `escalation_event(match_label=policy_invalid)` |
| store / ssot_engine 异常 | scan 失败计入 duration，verdict=error，hits 为空 | `scan_event(verdict=error, error=msg)` |
| CC subprocess spawn 失败 | dispatch 退出 1，该批次命中转升级 | `autonomous_execution_event(status=error)` + `escalation_event` |
| CC timeout | SIGTERM → 等 5s SIGKILL | `autonomous_execution_event(status=timeout)` + 即时升级 |
| Tool denied（stderr 检测） | 记录被拒工具列表 | `autonomous_execution_event(status=tool_denied)` + 即时升级 |
| /wrap 缺失 | git log 检测无 commit | `autonomous_execution_event(status=wrap_missing)` + 即时升级 |
| 夜巡结构化阻塞 | NIGHT_PATROL_PROFILE 输出 `RESULT=blocked` / `OUTPUT=blocked` 且无未提交 scoped dirty | `autonomous_execution_event(status=blocked)` + 即时升级 |
| 锁获取失败（cron scan）| exit 0，skip | `scan_skipped(reason=lock_held)` |
| 锁获取失败（cron 分身）| defer 1 次；仍失败 → exit 升级 | `deferred_autonomous_session` 事件（复用 escalation_event） |
| TG push 失败 | 重试 1 次；仍失败 → 写本地 log、事件 push_status=failed | `escalation_event(push_status=failed)` |
| 熔断（连续 5 scan 失败）| scheduler 侧自动 disable job | `escalation_event(match_label=circuit_broken)` 即时 push |

---

## 8. 安全设计

- **tool allow-list 硬约束**：`--allowedTools` / `--disallowedTools` 由 CC 运行时拒绝；prompt 层只做辅助提示
- **path_constraints 文档化 + CC tool 落地**：`Read/Write` 工具默认无路径限制，Python 侧不二次校验（依赖 allowed_tools 已剥离 Write 即可）；P1 可考虑在 CC MCP 层加路径白名单
- **环境变量剥离**：spawn 前剥离 `TELEGRAM_BOT_TOKEN`（复用 Relay scheduler 纪律，防 409）
- **隐私边界**：所有事件 payload 白名单字段，禁 input_summary/原文/业务描述
- **Policy git tracked**：policy 变更走 PR review；runtime 不支持动态注入
- **TG recipient 防篡改**：policy 中的 recipient 字段任何变更由 git blame 可查

---

## 9. 性能

| 环节 | 目标 P95 |
|------|---------|
| scan CLI 单次（无 CC 壳） | ≤ 2s |
| scan CLI 单次（CC 壳 cron 触发） | ≤ 60s |
| dispatcher 单次（不含 spawn） | ≤ 1s |
| autonomous session 单次 | ≤ 180s（policy 可调） |
| 事件 JSONL append 单行 | < 50ms |
| 锁检测 | < 10ms |
| flush_daily_aggregate 单次 | ≤ 5s |

---

## 10. 测试策略

| 域 | 模块 | 最低覆盖率 | 关键测试场景 |
|----|------|-----------|-----------|
| policy | `policy.py` | 90% | 合法 YAML 1 例 + 非法 5 例（缺字段 / 非法 autonomy 值 / 空 allowed_tools / 非法 cron expr / recipient 空）|
| scan | `scan.py` | 90% | 无命中 / 有各类命中 / store/ssot_engine 异常 / 锁已占 |
| dispatcher | `dispatcher.py` | 90% | 三档分流准确性 / batch 合并 / spawn 失败 / tool_denied 检测 / wrap_missing 检测（mock subprocess）|
| escalation | `escalation.py` | 90% | 即时 push 分支 / aggregate 入队 / flush 合并 / TG 失败重试 |
| events | `events.py` | 95% | 幂等 / 按月分片 / schema_version 字段齐 |
| cli | 新增子命令 | 70% | 退出码矩阵 / --json 格式 / --dry-run |

**集成测试**：
- AC-P0-004 Tool allow-list 硬约束：端到端 spawn 真 CC 分身，prompt 诱导越界 → 验证被拒
- AC-P0-005 S1 自动化实证：构造 3 条合规 handoff → 触发 cron chain → 检查 15min 内 state 变更 + commit
- AC-P0-007 锁协议：3 场景并发测试

**Spike 实测**（不计入代码 CI）：
- AC-P0-012 Reeve 一周 spike

---

## 11. 不在设计范围（对应 requirements §6）

- 不设计 L1 session 启动扫（wi_722_v2）
- 不设计 L3 滚动窗口阈值算法（observability-for-reeve）
- 不设计 Policy schema 抽象化 + 通用模板（P1，待 Amanda 配方回路）
- 不修改 Relay scheduler 源码

---

## 12. 下一步

- tasks.md：按本设计拆分可执行任务（policy schema → scan → dispatcher → escalation → Reeve policy.yaml → cron job → spike）
- 先写 `policy.py` 因为 scan 和 dispatcher 都依赖；其次 `events.py`；再并行 `scan.py` / `dispatcher.py` / `escalation.py`
