---
feature_name: "dispatcher-context-hygiene"
version: "0.0.1"
created: "2026-04-20T15:00:00+0800"
modified: "2026-04-20T15:00:00+0800"
upstream_dependency:
  - "project-specs/project-architecture.md"
  - "features/dispatcher-context-hygiene/requirements.md"
  - "features/heartbeat-runtime/design.md"
tags: [design, l2-spec, dispatcher, context-hygiene, hooks]
---

# dispatcher-context-hygiene — 技术设计文档

> **设计概述**：在 `agent-harness/harness/` 新增 1 个模块 `context_hygiene.py`，暴露 3 个纯函数——`check_wi_terminal(hit) → SkipDecision`、`check_workspace_active(workspace, window) → SkipDecision`、`recent_commits_summary(workspace, limit) → str`。修改 `dispatcher.py` 的 `dispatch()` 在判分流后、入 `autonomous_batch` 前调两个 check，按结果改 disposition；修改 `_build_prompt()` 追加"【近期 workspace 动向】"段。扩 `policy.py` 两个字段（向后兼容）。所有能力靠 stdlib + 现有 harness 模块，无新 pip 依赖。
>
> **关联文档**：
> - L1 架构：`project-specs/project-architecture.md`
> - L2 需求：`features/dispatcher-context-hygiene/requirements.md`
> - 上游 feature：`features/heartbeat-runtime/design.md`（dispatcher §2 / §5 Flow-002 / §6 Decision-003）

---

## 1. 技术栈

**完全复用现有栈**：

| 层 | 选型 | 备注 |
|----|------|------|
| 语言 | Python 3.11+ | L1 约束 |
| CLI | Click（扩现有 `dispatch` 命令） | 复用 |
| 数据模型 | Pydantic v2 | 复用 |
| Git 查询 | `subprocess.run(["git", "log", ...])` | 复用 heartbeat-runtime 的 `_detect_wrap_missing` 模式 |
| WorkItem 查询 | `harness.store.load_work_item` | 复用 |
| 测试 | pytest + pytest-cov | 复用 |

**无新 pip 依赖 / 无新外部二进制依赖**（git 已是 heartbeat-runtime 前提）。

---

## 2. 架构设计

### 新增模块

```
harness/
├── context_hygiene.py     # 新增：3 个纯函数 + SkipDecision 模型
├── dispatcher.py          # 改：dispatch() 加前置检查；_build_prompt() 加 commit 摘要段
├── policy.py              # 改：HeartbeatPolicy 新增 workspace_activity_window_minutes
                           #     AutonomousLLMConfig 新增 recent_commits_limit
└── store.py               # 不动（复用 load_work_item）
```

### 数据流

```
ScanReport                              HeartbeatPolicy
    │                                        │
    └────────────┬───────────────────────────┘
                 ↓
         dispatcher.dispatch()
                 │
                 │ for each hit:
                 │   tier ← _match_rule(hit, sip)
                 │   ↓
                 │   if tier == autonomous_llm:
                 │       ┌── check_wi_terminal(hit) ─────→ terminal? → outcome=skipped_wi_terminal
                 │       └── check_workspace_active(ws) ─→ active?   → outcome=skipped_workspace_active
                 │   if still autonomous_llm: append to autonomous_batch
                 │
                 ↓
         spawn_autonomous_session(batch, policy)
                 │
                 │ prompt = _build_prompt(hits, scan_id, policy)
                 │   └── recent_commits_summary(policy.workspace, limit) 段嵌入
                 ↓
         CC subprocess with prompt
```

---

## 3. 数据模型

### Model-001 — SkipDecision（新）

`harness.context_hygiene.SkipDecision` — 前置检查的统一返回类型：

```python
class SkipDecision(BaseModel):
    should_skip: bool
    reason_code: Literal["wi_terminal", "workspace_active", "none"] = "none"
    reason_detail: str = ""  # human-readable for event trail
```

`should_skip=False` 时 `reason_code="none"`。

### Model-002 — HeartbeatPolicy 扩展（改）

`harness.policy.HeartbeatPolicy` 新增：

```python
workspace_activity_window_minutes: int = Field(
    default=30, ge=0, le=1440,  # 0..24h
    description="Skip dispatch if workspace has commits within this window."
                " Set 0 to disable workspace activity check.",
)
```

`harness.policy.AutonomousLLMConfig` 新增：

```python
recent_commits_limit: int = Field(
    default=10, ge=0, le=100,
    description="Number of recent commits to inject into spawn prompt."
                " Set 0 to disable commit summary section.",
)
```

**向后兼容**：Pydantic 默认值保证现有 amanda.yaml 不改也能 load；`0` 是显式禁用值。

### Model-003 — DispatchEvent.dispositions[].outcome（扩取值）

不改 schema（`list[dict[str, Any]]`），累加两个合法 `outcome` 字符串：

- `"skipped_wi_terminal"` 附加 `skip_reason: str`
- `"skipped_workspace_active"` 附加 `skip_reason: str`

（其他字段 `hit_type` / `match_label` / `ref_id` / `autonomy` 不动。）

---

## 4. 模块接口

### CMD-001 — `context_hygiene.check_wi_terminal`

```python
def check_wi_terminal(
    hit: ScanHit,
    harness_root: Path | None = None,
) -> SkipDecision:
    """Return skip decision based on target work item lifecycle.

    Logic:
      - Determine wi_id from hit. For hit_type='pending_handoff', load handoff
        yaml by hit.ref_id, read its work_item_id. For other hit types that
        directly reference a wi (future), use ref_id as wi_id.
      - If wi not found (yaml missing / parse error) → return SkipDecision(
          should_skip=False, reason_code='none', reason_detail='wi not found')
        (defensive: fall through to normal logic).
      - If wi.status in {completed, failed} → return SkipDecision(
          should_skip=True, reason_code='wi_terminal',
          reason_detail=f'wi {wi_id} status={status}').
      - Else → should_skip=False.
    """
```

**输入**：ScanHit（只处理 `hit_type='pending_handoff'` 类型；其他类型返回 `should_skip=False`）
**输出**：SkipDecision
**错误**：所有 IO/parse 错误吞掉，返回 `should_skip=False`（NFR-002 失败容忍）

### CMD-002 — `context_hygiene.check_workspace_active`

```python
def check_workspace_active(
    workspace: Path,
    window_minutes: int,
) -> SkipDecision:
    """Return skip decision based on recent commits in workspace.

    Logic:
      - If window_minutes <= 0 → skip check, return should_skip=False
      - Run `git -C <workspace> log --since=<window_minutes> minutes ago
        --pretty=%H -- .` with timeout 10s
      - If git fails (non-zero, not-a-repo, timeout, missing binary) →
        return should_skip=False (NFR-002)
      - If output has ≥1 line → should_skip=True, reason_code='workspace_active',
        reason_detail=f'{N} commits in last {window_minutes}min'
      - Else → should_skip=False
    """
```

### CMD-003 — `context_hygiene.recent_commits_summary`

```python
def recent_commits_summary(
    workspace: Path,
    limit: int,
) -> str:
    """Return markdown-bullet list of recent N commits, or empty string.

    Logic:
      - If limit <= 0 → return ""
      - Run `git -C <workspace> log -<limit> --no-merges --pretty=format:"- %h %s"`
        with timeout 10s
      - If git fails → return ""
      - Else → return stdout (strip trailing newline)
    """
```

### CMD-004 — `dispatcher.dispatch()` 改动

在 `for hit in scan_report.hits:` 循环的 `disposition = {...}` 之后、追加到 `autonomous_batch` 之前插入：

```python
if tier == "autonomous_llm":
    # Hygiene gate 1: wi lifecycle
    wi_decision = check_wi_terminal(hit, harness_root=harness_root)
    if wi_decision.should_skip:
        disposition["outcome"] = "skipped_wi_terminal"
        disposition["skip_reason"] = wi_decision.reason_detail
        dispositions.append(disposition)
        continue
    # Hygiene gate 2: workspace activity (batch-level, run once per dispatch)
    # — actually moved out of loop for efficiency; see below
    autonomous_batch.append(hit)
else:
    ...existing logic
```

Workspace 活性检测是 **per-dispatch 一次**（不是 per-hit），因为 workspace 是 policy-level 的：

```python
# After loop, if autonomous_batch non-empty:
if autonomous_batch and policy.workspace_activity_window_minutes > 0:
    ws_decision = check_workspace_active(
        policy.workspace, policy.workspace_activity_window_minutes
    )
    if ws_decision.should_skip:
        # Convert batch to skipped dispositions
        for hit in autonomous_batch:
            for d in dispositions:
                if d["ref_id"] == hit.ref_id and d["autonomy"] == "autonomous_llm":
                    d["outcome"] = "skipped_workspace_active"
                    d["skip_reason"] = ws_decision.reason_detail
        autonomous_batch = []
```

### CMD-005 — `dispatcher._build_prompt()` 改动

在当前 prompt 的 【Autonomy 边界】之前、【扫描发现】之后插入：

```python
commits_summary = recent_commits_summary(
    policy.workspace, policy.autonomous_llm_config.recent_commits_limit
)
commits_section = ""
if commits_summary:
    commits_section = (
        "\n【近期 workspace 动向】\n"
        f"{commits_summary}\n"
        "\n"
        "如你判断上述 hit 已被近期 commit 实质覆盖，应 "
        "`harness ack --reject --reason '已由 commit X 完成'`，"
        "不做重复工作。\n"
    )
```

---

## 5. 流程

### Flow-001 — 正常 spawn（无 skip）

1. `dispatch(scan_report, policy)` 入口
2. 遍历 hits，判 tier（逻辑不变）
3. 对 `autonomous_llm` 档 hit：调 `check_wi_terminal` → None skip → 入 batch
4. batch 非空 + `workspace_activity_window_minutes > 0` → `check_workspace_active` → None skip
5. `spawn_autonomous_session(batch, policy)`
6. `_build_prompt` 含 "【近期 workspace 动向】" 段
7. CC 启动、处置、/wrap、commit

### Flow-002 — 硬契约 1 拦截

步骤 3 里 `check_wi_terminal(hit).should_skip=True` → disposition.outcome = `skipped_wi_terminal`，不入 batch，continue 下一 hit。

### Flow-003 — 硬契约 3 拦截

步骤 4 里 `check_workspace_active(...).should_skip=True` → batch 全部 hit 的 disposition.outcome 改写为 `skipped_workspace_active`，batch 清空，不 spawn。

### Flow-004 — 软契约 5 兜底

步骤 6 分身拿到 prompt 含近期 commit 摘要 + 提示文案；分身按人格做判断（reject / reply），不依赖 dispatcher 强制。

---

## 6. 关键决策

### Decision-001 — Hygiene check 按 per-hit vs per-batch 分

- **wi 生命周期**是 per-hit（每条 ho 对应独立 wi）→ 循环内检查
- **workspace 活性**是 per-agent（policy.workspace 是 Agent 级）→ 循环外检查一次

**理由**：workspace git log 有成本（shell out 10s 超时），每 hit 都跑会堆叠延迟；wi 查询是本地文件读，开销可忽略。

### Decision-002 — 跳过不降档

Skip hit 的 disposition `autonomy` 保持原值（`"autonomous_llm"`），只改 `outcome`。不把它改成 `"mechanical"`。

**理由**：`autonomy` 字段反映"policy 判定档"，`outcome` 反映"实际发生"。后者的 `skipped_*` 值本身就已经表达 "决策是 autonomous_llm 但 dispatcher 前置拦截未执行"，语义清晰。若降档到 `mechanical` 会丢失"这本来是要 spawn 的"信息。

### Decision-003 — 默认值选择

- `workspace_activity_window_minutes=30` — 够长能捕住典型人工 session（15-30min），够短不会无限期禁 spawn
- `recent_commits_limit=10` — prompt 里 10 条 commit 约 500-1000 字，成本可接受；Agent 能看到一周量级的动向

Amanda 可按自己节奏调（她 workspace 人工 session 强度不同时）。

### Decision-004 — `_detect_wrap_missing` 的模式复用

既有 `dispatcher._detect_wrap_missing()` 已经用 `subprocess.run(["git", "log", ...], cwd=workspace, timeout=10)` 模式；新增的两个 git 查询（workspace_active + recent_commits_summary）走同一模式，错误处理统一（OSError / FileNotFoundError / TimeoutExpired → 保守返回）。

### Decision-005 — Not-a-git-repo 场景

Amanda workspace（`Doc/amanda-space/`）是 monorepo 子路径，`git -C` 会沿路径向上找到根 `.git`，正常工作。若未来某 Agent 的 workspace 独立仓库或非 git，`git log` 返回非零 → NFR-002 降格，不阻塞。

### Decision-006 — SkipDecision 不产升级事件

本 feature 新增的 skip 不立即 push 大鸟（升级档那条路径）。skip 事件进 DispatchEvent trail，日度聚合 flush 时作为一行汇总推。

**理由**：skip 是"系统自我保护"成功路径，不是异常；刷屏会让用户失焦。若 3 天内 skip 率异常（>50%），由 observability-for-reeve feature 识别并 escalate。

---

## 7. 错误处理

| 场景 | 行为 |
|------|------|
| wi yaml 文件缺失 | `check_wi_terminal` 返回 `should_skip=False`（降格），stderr warning |
| wi yaml 解析失败 | 同上 |
| workspace 非 git 仓库 | `check_workspace_active` 返回 `should_skip=False`；`recent_commits_summary` 返回 "" |
| `git` 二进制缺失 | 同上（FileNotFoundError） |
| `git log` 超时（>10s） | 同上（TimeoutExpired） |
| `git log` 非零退出 | 同上 |
| handoff yaml 读失败（用于 hit → wi_id 映射） | `check_wi_terminal` 返回 `should_skip=False` |
| policy 缺 `workspace_activity_window_minutes`（老 policy）| Pydantic 默认 30，正常工作 |
| policy 缺 `recent_commits_limit`（老 policy）| Pydantic 默认 10，正常工作 |

核心原则：**hygiene 检查本身永不阻塞 dispatch**。检查失败 = 退回原 heartbeat-runtime 行为。

---

## 8. 测试策略

### 单元测试（`tests/test_context_hygiene.py`）

- `check_wi_terminal`：mock `store.load_work_item`，各 status 覆盖（completed/failed/in_progress/draft/blocked/not_found）
- `check_workspace_active`：用 `tmp_path` + 真实 `git init` + `git commit`，控制 commit 时间戳（`GIT_COMMITTER_DATE` / `GIT_AUTHOR_DATE` 环境变量）
- `recent_commits_summary`：同上 + 各 limit 边界（0 / 10 / 100）
- 错误路径：mock `subprocess.run` 抛 FileNotFoundError / TimeoutExpired / 非零退出

### 集成测试（`tests/test_dispatcher_integration_hygiene.py`）

- AC-P0-007：tmp git repo + tmp harness root + 真实 handoff yaml + 真实 wi yaml，跑 `dispatch()` 全链路
- 变体矩阵：wi terminal / workspace active / 两者都是 / 两者都不是（4 场景）
- 验证 DispatchEvent.dispositions 正确 + `spawn_autonomous_session` 不被 mock 调用

### Ken 自测（AC-P0-007 的人工扩展）

复现 4/20 事故的 tmp 快照：
1. 模拟 Amanda workspace（copy relevant files to tmp）
2. 模拟 ho_071 pending + wi_576 completed（覆盖链）
3. 模拟 Amanda 人工 commit 刚完成（workspace active）
4. 跑 `harness dispatch <scan_id>` 验证：
   - disposition.outcome = `skipped_workspace_active` 或 `skipped_wi_terminal`（任一即可）
   - CC 子进程**不启动**（通过 mock / 或观察无新进程）

### 回归

- 现有 711 tests 全过
- `tests/test_dispatcher.py`（若存在）所有 cases 不动——新 skip 逻辑在原路径之前，不破坏原路径

---

## 9. 监控、部署与演进

**指标（留给 observability-for-reeve）**：`skip_rate = skip 总数 / autonomous 命中总数`（目标 <30%，>50% 报警）；`skip_by_reason` 分类计数；`autonomous_empty_delta_rate`（分身 exit=0 但 commit 为空比率，AC-P0-010 依据）。所有 skip 在 `DispatchEvent.dispositions` + `.harness/events/*.jsonl` 可 grep。

**部署**：harness 包内 3 文件改动（新 1 + 改 2）；Amanda amanda.yaml 可选调整新字段不改也能跑；不碰 cron-jobs.json。顺序：L3 合入 → 跑 711+ 测试 → Ken 自测 → 合 main → Amanda /60 3 天观察期。

**演进**（AC-P0-010 观察结果驱动）：skip 率过高 → 调 window_minutes；accept 空 delta 仍出现 → 硬契约 2（supersedes_ho_id）P1 立项；跨 workspace 协作 → 扩多路径。
