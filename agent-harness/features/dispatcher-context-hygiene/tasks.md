---
feature_name: "dispatcher-context-hygiene"
version: "0.0.1"
created: "2026-04-20T15:15:00+0800"
modified: "2026-04-20T15:15:00+0800"
upstream_dependency:
  - "features/dispatcher-context-hygiene/requirements.md"
  - "features/dispatcher-context-hygiene/design.md"
tags: [tasks, l2-spec, dispatcher, tdd]
---

# dispatcher-context-hygiene — 任务拆分文档

> **任务概述**：按 design.md §2 模块拆分 + §4 接口规格，分 8 个任务实现 P0。基础层 policy 字段扩 + 新模块 context_hygiene，再改 dispatcher，最后 Ken 自测复现 4/20 事故 + 启 cron 观察期。

---

## 1. 任务总览

| # | 任务 | 优先级 | 工时 | 依赖 | 负责人 | 状态 |
|---|------|-------|------|------|-------|------|
| T1 | policy.py 扩两字段（workspace_activity_window_minutes + recent_commits_limit）+ 测试 | P0 | 1.5h | — | Ken | todo |
| T2 | context_hygiene.py 新模块（SkipDecision + 3 函数）+ 单元测试 | P0 | 5h | T1 | Ken | todo |
| T3 | dispatcher.py 改 dispatch() 加前置检查（CMD-004）+ 回归测试 | P0 | 3h | T2 | Ken | todo |
| T4 | dispatcher.py 改 _build_prompt() 加 commit 摘要段（CMD-005）+ 单元测试 | P0 | 1.5h | T2 | Ken | todo |
| T5 | 集成测试 test_dispatcher_integration_hygiene.py（AC-P0-007 + 变体矩阵） | P0 | 3h | T3, T4 | Ken | todo |
| T6 | Ken 自测复现 4/20 事故（tmp repo + 全链路手工验证） | P0 | 2h | T5 | Ken | todo |
| T7 | 合入 main + 更新 CHANGELOG + `harness` CLI 文档 | P0 | 1h | T6 | Ken | todo |
| T8 | 启 cron `/60` 3 天观察期（Amanda 分阶段阶段 1）+ spike-report | P0 | 3 天观察 + 2h 报告 | T7 | Ken | todo |

**总工时**：~19 小时编码 + 3 天观察期。

**并行策略**：T3 和 T4 可并行（都依赖 T2）；T5 是两者汇合点。

---

## 2. 任务详情

### T1：policy.py 扩两字段

**描述**：实现 design.md §3 Model-002。在 `HeartbeatPolicy` 加 `workspace_activity_window_minutes: int = 30`；在 `AutonomousLLMConfig` 加 `recent_commits_limit: int = 10`。

**输入**：
- design.md §3 Model-002 字段定义（含 ge/le 约束）
- 现有 `harness/policy.py` 文件

**输出**：
- `harness/policy.py` 两字段（Pydantic `Field` 含 default + 约束）
- `tests/test_policy.py` 增测（缺字段用默认值 / 显式 0 / 超边界报错）

**TDD 要求**（Unit Test）：
- 老 policy yaml（不含新字段）能 load，字段为默认
- `workspace_activity_window_minutes=0` 合法（禁用语义）
- `workspace_activity_window_minutes=-1` 报 ValidationError
- `workspace_activity_window_minutes=1441` 报 ValidationError（>24h）
- `recent_commits_limit=0` 合法
- `recent_commits_limit=101` 报 ValidationError

**验收**：
- 711 + 新测试全过
- 现有 amanda.yaml load 不出错

---

### T2：context_hygiene.py 新模块

**描述**：实现 design.md §3 Model-001 + §4 CMD-001/002/003。新模块 `harness/context_hygiene.py`，暴露 `SkipDecision` 模型 + 3 个纯函数。

**输入**：
- design.md §4 CMD-001/002/003 函数签名 + 逻辑规格
- `harness/store.py`（`load_work_item` 复用）
- `harness/dispatcher.py` 的 `_detect_wrap_missing` git 调用模式（复用）

**输出**：
- `harness/context_hygiene.py`
- `tests/test_context_hygiene.py`

**TDD 要求**（Unit Test，覆盖率 ≥95%）：

`check_wi_terminal`：
- hit_type != 'pending_handoff' → should_skip=False
- wi status=completed → should_skip=True, reason_code=wi_terminal
- wi status=failed → should_skip=True
- wi status=in_progress/draft/blocked → should_skip=False
- handoff yaml 缺失 → should_skip=False
- wi yaml 缺失 → should_skip=False
- handoff yaml 损坏 → should_skip=False

`check_workspace_active`：
- window=0 → should_skip=False（禁用）
- workspace 无 commit in window → should_skip=False
- workspace 有 1 commit in window → should_skip=True, reason_detail 含 "1 commits"
- workspace 有多 commits → reason_detail 含计数
- workspace 非 git 目录 → should_skip=False
- `git` 命令 timeout → should_skip=False
- `git` 命令非零退出 → should_skip=False
- `git` 二进制缺失（FileNotFoundError）→ should_skip=False

`recent_commits_summary`：
- limit=0 → ""
- limit=10 且 repo 有 20 commits → 输出 10 行
- limit=10 且 repo 有 5 commits → 输出 5 行
- 非 git 目录 → ""
- git 超时 → ""
- 返回格式：以 `- ` 开头 + 短 hash + space + subject

**测试技术要点**：
- 用 `pytest.fixture` 创建 tmp git repo：`git init` + `git commit` 链
- 控制 commit 时间戳：`env={'GIT_COMMITTER_DATE': ..., 'GIT_AUTHOR_DATE': ...}`
- 或 `tmp_path` + `subprocess.run(["git", "commit", "--date=<iso>"], ...)`
- mock `subprocess.run` 的失败场景走 `unittest.mock.patch`

**验收**：
- 新测试 ≥ 15 条（函数级 + 错误路径）
- 覆盖率 ≥ 95%
- 运行 `pytest tests/test_context_hygiene.py -v` 全绿

---

### T3：dispatcher.py 改 dispatch() 加前置检查

**描述**：实现 design.md §4 CMD-004。在 `dispatch()` 循环内插入 `check_wi_terminal` 调用；循环外（autonomous_batch 非空后）插入 `check_workspace_active` 调用，按 decision 改 disposition。

**输入**：
- design.md §4 CMD-004 伪代码
- design.md §6 Decision-001（per-hit vs per-batch）
- design.md §6 Decision-002（跳过不降档）
- `harness/dispatcher.py` 现有 `dispatch()` 函数（830 行文件）

**输出**：
- `harness/dispatcher.py` 改动
- `tests/test_dispatcher.py` 增测（skip 场景）

**TDD 要求**（Unit Test）：
- wi terminal → disposition.outcome=skipped_wi_terminal，autonomous_batch 不含该 hit
- workspace active → 所有 autonomous_llm 档 disposition.outcome 改为 skipped_workspace_active，batch 清空
- wi terminal 且 workspace active → skipped_wi_terminal 优先（per-hit 先于 per-batch）
- 两者都不触发 → 原路径不变（spawn 正常）
- workspace_activity_window_minutes=0 → 不调 check_workspace_active
- `mechanical` 档 hit 不走 hygiene 检查（只对 autonomous_llm）
- `escalate` 档 hit 不走 hygiene 检查
- dry_run 模式下 skip 场景仍体现在 dispositions

**回归要求**：
- 现有 `tests/test_dispatcher.py` 所有 cases 全过
- `spawn_autonomous_session` mock 断言：skip 场景下 **0 次调用**

**验收**：
- 新测试 ≥ 10 条
- dispatcher.py 覆盖率不降
- 运行 `pytest tests/test_dispatcher.py -v` 全绿

---

### T4：dispatcher.py 改 _build_prompt() 加 commit 摘要段

**描述**：实现 design.md §4 CMD-005。在 `_build_prompt()` 中插入"【近期 workspace 动向】"段。

**输入**：
- design.md §4 CMD-005 伪代码
- `harness/dispatcher.py` 现有 `_build_prompt()` 函数

**输出**：
- `harness/dispatcher.py` 改动
- `tests/test_dispatcher.py` 增测（prompt 构造）

**TDD 要求**（Unit Test）：
- `recent_commits_limit=0` → prompt 不含"【近期 workspace 动向】"
- `recent_commits_limit=10` + workspace 有 5 commits → prompt 含该段 + 5 条 bullet
- `recent_commits_limit=10` + workspace 非 git → prompt 不含该段（静默省略）
- prompt 含"如你判断上述 hit 已被近期 commit 实质覆盖"文案
- 段落位置在【扫描发现】之后、【Autonomy 边界】之前（断言字符串顺序）

**验收**：
- 新测试 ≥ 5 条
- 运行 `pytest tests/test_dispatcher.py -v` 全绿

---

### T5：集成测试 test_dispatcher_integration_hygiene.py

**描述**：实现 AC-P0-007 + AC-P0-008。端到端跑 `dispatch()`，验证复现 4/20 事故被拦住 + Smoke #2 场景被软契约兜底。

**输入**：
- requirements.md §4 AC-P0-007/008
- design.md §8 测试策略

**输出**：
- `tests/test_dispatcher_integration_hygiene.py`

**TDD 要求**（Integration Test）：

**场景矩阵**（4 组）：

| # | wi status | workspace commits | 期望 outcome |
|---|-----------|------------------|------------|
| 1 | completed | 无近期 | skipped_wi_terminal |
| 2 | in_progress | 近 5min 有 | skipped_workspace_active |
| 3 | completed | 近 5min 有 | skipped_wi_terminal（per-hit 优先） |
| 4 | in_progress | 无近期 | 正常 spawn |

**测试设置**：
- `tmp_path` 作 harness root
- `tmp_path/ws/` 真实 `git init` + commit 链
- 真实 handoff.yaml + wi.yaml 文件（按 harness 格式）
- 真实 scan-reports/*.yaml
- `spawn_autonomous_session` mock（避免实际 CC 调用）

**AC-P0-008 变体**：
- 场景 4（正常 spawn）prompt 含近期 10 条 commit 摘要 + 其中有"HB 精简"类信号 commit
- 断言：mock 接到的 prompt 参数包含该 commit subject

**验收**：
- 4 场景全过
- prompt 结构断言过
- 运行 `pytest tests/test_dispatcher_integration_hygiene.py -v` 全绿

---

### T6：Ken 自测复现 4/20 事故

**描述**：在 tmp 环境复现 2026-04-20 事故现场，手工跑 `harness dispatch` CLI 验证拦截生效。这是 Ken 自约束第 1 条"未自测端到端前不扩范围"的直接兑现。

**输入**：
- `ken-space/memo/lessons-2026-04-20-ken-wi-quality-reset.md`（事故现场）
- T5 集成测试（已证代码路径通）
- `harness dispatch --dry-run` CLI

**输出**：
- `ken-space/memo/verification-2026-04-20-dispatcher-hygiene-selftest.md`
- 含 CLI 输出截取 + 断言 + 通过/不通过

**测试步骤**：

```bash
# Setup
TMP=$(mktemp -d)
cd $TMP && git init
mkdir ws
# mock handoff/wi yaml 对应 4/20 事故：
# - wi_576 status=completed
# - ho_071 ack_state=pending, work_item_id=wi_576
# - workspace 有近 10min commit

# Test 1: 硬契约 1 拦截（wi terminal）
HARNESS_ROOT=$TMP/.harness harness dispatch scan_test_1 --dry-run
# 断言：输出含 "skipped_wi_terminal" 且 "wi_576 status=completed"

# Test 2: 硬契约 3 拦截（workspace active）
# 把 wi 改回 in_progress，workspace 有近 commit
HARNESS_ROOT=$TMP/.harness harness dispatch scan_test_2 --dry-run
# 断言：输出含 "skipped_workspace_active"

# Test 3: 正常 spawn（都不触发）
# wi in_progress + workspace 静默 >30min
HARNESS_ROOT=$TMP/.harness harness dispatch scan_test_3 --dry-run
# 断言：输出含 "→ autonomous_llm (planned)"（dry-run 下不真 spawn）
```

**验收**：
- 3 个 test 行为符合预期
- 写 `verification-...md` 含截取 + Ken 亲验签名
- 在 commit message 里引用该 memo

---

### T7：合入 + 文档

**描述**：

**输入**：T1-T6 全过

**输出**：
- `harness/CHANGELOG.md` 或 `REVIEW-NOTES.md` 加本 feature 条目
- `skills/harness/SKILL.md`（如需）加 hygiene 说明
- 合入 `feature/doc-para` 分支（Ken 默认分支）
- git tag（可选）

**Commit 策略**：
- 按 T1-T4 每个任务 1 commit（原子化）
- T5/T6 合并 1 commit（测试验证）
- T7 1 commit（文档）

**验收**：
- `pytest tests/ -v --cov=harness --cov-report=term` 覆盖率 100%
- `harness dispatch --help` 输出正常
- `git log` 5-6 条原子 commit

---

### T8：启 cron `/60` 3 天观察期

**描述**：Amanda 分阶段方案阶段 1。Ken 手工改 `PULSE/Relay/config/cron-jobs.json` 的 `heartbeat-amanda-scan.enabled=true` + `schedule.expr="*/60 * * * *"`。观察 3 天，收集 AC-P0-010 指标。

**输入**：
- T7 已合入
- `cron-jobs.json` 当前状态：`heartbeat-amanda-scan.enabled=false`（已确认 4/20）
- Pulse 交接已获用户授权（2026-04-20 14:15）

**输出**：
- `cron-jobs.json` 改动 commit
- `systemctl --user restart relay.service` 或等价 launchctl kickstart（让 Relay 重载 cron 配置）
- 3 天后：`ken-space/memo/spike-report-2026-04-XX-dispatcher-hygiene-phase1.md`

**观察指标**（AC-P0-010）：
- `skipped_wi_terminal` 事件计数 → 期望 ≥ 1（硬契约 1 真实触发证据）
- `skipped_workspace_active` 事件计数 → 期望 ≥ 1（硬契约 3 真实触发证据）
- `autonomous_execution_event` with exit=0 但 commit 为空（通过 scan 事件 jsonl 分析）→ 期望 = 0（软契约 5 让分身走严格 reject）
- 分身覆盖人工工作事故 → 期望 = 0

**熔断**（复用 Relay 现有）：
- 若连续 5 次 spawn 失败（status != success）→ Relay 自动 disable 该 job
- 若发生分身覆盖事故 → Ken 人工 disable + 回滚

**转阶段**（3 天后）：
- 指标达标 → 阶段 2：改 `/30`，再观察 3 天
- 指标不达标 → 硬契约 2（supersedes_ho_id）P1 立项

---

## 3. 验收清单（交付时逐项勾）

- [ ] T1 policy 字段扩 + 测试全过
- [ ] T2 context_hygiene.py + 15+ 测试 + 覆盖率 ≥95%
- [ ] T3 dispatch() 前置检查 + 10+ 测试
- [ ] T4 prompt 注入 + 5+ 测试
- [ ] T5 集成测试 4 场景全过
- [ ] T6 Ken 自测 3 test 通过 + verification memo 签名
- [ ] T7 合入 feature/doc-para + CHANGELOG
- [ ] T8 cron 启用 + 3 天 spike-report
- [ ] harness 包整体测试覆盖率保持 100%
- [ ] 711+ 现有测试不破

---

## 4. 风险与对策

| 风险 | 概率 | 对策 |
|------|------|------|
| T2 git 测试在 CI 不稳（时间戳精度） | 中 | 用环境变量 `GIT_COMMITTER_DATE` 精确控制；fixture 加重试 |
| T3 dispatcher 既有复杂逻辑（锁 / seq / defer）被误改 | 中 | T3 前先跑 `pytest tests/test_dispatcher.py` 记录基线，改动后对比 |
| T6 自测环境 vs 生产环境差异 | 低 | tmp 环境保持 `HARNESS_ROOT` env 隔离；CLI 模式与生产一致 |
| T8 3 天内用户需要紧急启 cron 产生压力 | 低 | `/60` 本身就是非紧急节奏；若有需要可人工手动触发 scan+dispatch |
| T8 观察期 skip 率 0（拦截从未触发） | 中 | 不算失败——说明人工 + cron 时序天然不撞；仍可延长观察或手工构造场景验证 |

---

## 5. 执行顺序

```
T1 ────────┐
           ├──→ T2 ──→ T3 ──┐
           │                ├──→ T5 ──→ T6 ──→ T7 ──→ T8
           └──→ T4 ─────────┘
```

T1/T4 可并行起步；T2 之后 T3/T4 可并行；T5 汇合。单会话内可完成 T1-T7（估 ~19h，分 2 个 session），T8 跨天观察。
