---
feature_name: "heartbeat-runtime"
version: "0.0.1"
created: "2026-04-19T10:40:00+0800"
modified: "2026-04-19T10:40:00+0800"
upstream_dependency:
  - "features/heartbeat-runtime/requirements.md"
  - "features/heartbeat-runtime/design.md"
tags: [tasks, l2-spec, heartbeat, runtime, tdd]
---

# heartbeat-runtime - 任务拆分文档

> **任务概述**：按 design.md §2 模块拆分 + §6 关键决策，分 8 个任务实现 P0。先写基础层（policy + events），再并行三个引擎（scan / dispatcher / escalation），最后配置与 spike。

---

## 1. 任务总览

| # | 任务 | 优先级 | 工时 | 依赖 | 负责人 | 状态 |
|---|------|-------|------|------|-------|------|
| T1 | policy.py + HeartbeatPolicy schema + `harness policy validate` | P0 | 4h | — | Ken | todo |
| T2 | events.py + EventBase + 5 类事件 schema + append_event | P0 | 3h | — | Ken | todo |
| T3 | scan.py + run_scan + `harness scan` CLI | P0 | 6h | T1, T2 | Ken | todo |
| T4 | dispatcher.py + dispatch + spawn_autonomous_session + `harness dispatch` CLI | P0 | 8h | T1, T2, T3 | Ken | todo |
| T5 | escalation.py + push_now + flush_daily_aggregate + `harness escalation` CLI | P0 | 5h | T2 | Ken | todo |
| T6 | 锁协议（`.harness/locks/<agent>.lock` 实现 + 3 场景行为） | P0 | 3h | T3, T4 | Ken | todo |
| T7 | Reeve `heartbeat-policy.yaml` + Relay `cron-jobs.json` 两条 job | P0 | 2h | T1-T6 | Ken + Pulse | todo |
| T8 | 集成测试（AC-P0-004/005/007）+ Reeve 一周 spike + `spike-report.md` | P0 | 10h + 一周观察 | T1-T7 | Ken | todo |

**总工时**：~41 小时编码 + 一周 spike 观察期。

---

## 2. 任务详情

### T1：policy.py + HeartbeatPolicy schema + `harness policy validate`

**描述**：实现 design.md §4 Model-001 HeartbeatPolicy Pydantic 模型 + `load_policy(agent_id)` 函数 + CLI 子命令 `harness policy validate <agent_id>`。引用 design.md §3 CMD-004。

**输入**：
- design.md §4 Model-001 schema 定义
- design.md §3 CMD-004 CLI 规格

**输出**：
- `harness/policy.py`（含 HeartbeatPolicy / PerActionRule / ScanItemPolicy / AutonomousLLMConfig / EscalationRules 五个 Pydantic 模型 + `load_policy()` + `policy_path()`）
- `harness/cli.py` 扩展（新增 `policy` 子命令组 + `validate` 命令）
- `tests/test_policy.py`

**TDD 要求**（Unit Test，覆盖率 ≥90%）：
1. 合法 YAML 加载成功返回 HeartbeatPolicy 对象
2. 5 类非法 YAML 各自抛明确错误：缺 agent_id / 非法 autonomy 值 / allowed_tools 空列表 / path_constraints 键非法 / recipient 空
3. `policy_path("reeve")` 返回 `.harness/policies/reeve.yaml`
4. CLI `harness policy validate reeve` 对合法/非法 policy 返回退出码 0/1
5. `model_validator` 覆盖全部字段交叉校验

**验证标准**：
- pytest 全过 ≥90% cov
- 对应 AC-P0-002 通过

**完成标准**：
- 代码 commit（`feat(bigbird/agent-harness): T1 heartbeat policy schema + validate CLI`）
- 测试计入全局统计（445 → ≥470）

---

### T2：events.py + EventBase + 5 类事件 schema + append_event

**描述**：实现 design.md §4 Model-003 五类事件 Pydantic 模型 + `append_event(event_type, payload)` helper（按月分片 JSONL 追加写）。

**输入**：
- design.md §4 Model-003 schema
- design.md §6 决策-005（按 event_type 分文件 + 按月分片）

**输出**：
- `harness/events.py`（含 EventBase + ScanEvent + DispatchEvent + AutonomousExecutionEvent + EscalationEvent + `append_event()` + `events_path(event_type, month)`）
- `tests/test_events.py`

**TDD 要求**（Unit Test，覆盖率 ≥95%）：
1. EventBase 必填字段验证
2. 5 类事件分别序列化反序列化往返
3. append_event 追加写创建新月份文件
4. append_event 幂等性：同 event_id 不重复追加（P1 留扩展点，P0 可仅文档化）
5. 文件路径格式 `.harness/events/<event_type>-YYYY-MM.jsonl`
6. 多线程/多进程 append 原子性（用 `os.write` + O_APPEND 模式；或 `open("a")` 自带原子小写）

**验证标准**：
- pytest 全过 ≥95% cov
- 并发 append 100 次无数据损坏

**完成标准**：
- 代码 commit（`feat(bigbird/agent-harness): T2 heartbeat events module`）
- 与 T1 并行开发

---

### T3：scan.py + run_scan + `harness scan` CLI

**描述**：实现 design.md §4 Model-002 ScanReport + `run_scan(agent_id, trigger_mode)` + CMD-001。复用 `store.py` 和 `ssot_engine.py` 已有 API；不新增 CRUD。

**输入**：
- design.md §4 Model-002 schema
- design.md §3 CMD-001 CLI 规格
- design.md §5 Flow-001（无命中路径）

**输出**：
- `harness/scan.py`（含 ScanHit + ScanReport + `run_scan()` + `_classify_hit()` 内部 classifier：把 raw handoff/violation/drift 映射到 match_label 如 `format_compliant` / `cross_owner_boundary` / `critical` 等）
- 如 `store.py` 缺 `list_unresolved_violations`，新增薄 wrapper 在 `scan.py` 内（不改 store 核心）
- `harness/cli.py` 扩展 `harness scan` 子命令
- `tests/test_scan.py`

**TDD 要求**（Unit + Integration，覆盖率 ≥90%）：
1. `run_scan` 无命中场景：verdict=clean，hits=[]，退出码 0
2. 有命中场景：pending / violation / drift 三种各构造数据 → 返回对应 ScanHit
3. store 异常 → verdict=error，错误入 scan_report.error，退出码 2
4. ssot_engine 异常 → 同上
5. classifier 把 handoff 的 input_summary 扫描为 format_compliant vs cross_owner_boundary 的启发式（P0 最小集：基于 from_agent/to_agent 跨 owner、handoff reason 含特定关键词等）
6. append scan_event 后 JSONL 内容正确
7. CLI `harness scan reeve --mode cron --json` 输出合法 JSON

**验证标准**：
- pytest 全过 ≥90% cov
- 对应 AC-P0-001 通过

**完成标准**：
- 代码 commit（`feat(bigbird/agent-harness): T3 heartbeat scan engine + CLI`）
- 能手动跑 `harness scan reeve --mode manual` 返回真实 Reeve workspace 扫描结果

---

### T4：dispatcher.py + dispatch + spawn_autonomous_session + `harness dispatch` CLI

**描述**：实现 design.md §2 dispatcher 模块 + `dispatch(scan_report, policy)` + `spawn_autonomous_session()` + CMD-002。**核心安全点**：`--allowedTools` / `--disallowedTools` / `--permission-mode bypassPermissions` 参数注入。

**输入**：
- design.md §3 CMD-002
- design.md §5 Flow-002（S1 自动化路径）
- design.md §6 决策-001/002/004（direct spawn / tool allow-list / prompt 模板）

**输出**：
- `harness/dispatcher.py`（含 `dispatch()` / `spawn_autonomous_session()` / `_match_rule()` / `_build_prompt()` / `_detect_tool_denied()` / `_detect_wrap_missing()`）
- `harness/cli.py` 扩展 `harness dispatch` 子命令
- `tests/test_dispatcher.py`

**TDD 要求**（Unit 主 + 少量 Integration，覆盖率 ≥90%）：
1. `_match_rule` 按 per_action 顺序返回第一个匹配档位；均不匹配返回 default
2. `dispatch` 三档分流正确：mechanical 只产 dispatch_event；autonomous_llm batch 合并成一次 spawn；escalate 产 escalation_event
3. `spawn_autonomous_session` 用 `mock subprocess.Popen` 验证：
   - Popen args 含 `--allowedTools` + `--disallowedTools` + `--permission-mode bypassPermissions`
   - env 剥离 `TELEGRAM_BOT_TOKEN`
   - env 注入 `HARNESS_TRIGGER_MODE=cron` / `HARNESS_SCAN_ID`
   - legacy cwd = policy.workspace；scheduled_night_patrol 使用 NIGHT_PATROL_PROFILE，cwd = 临时 capsule，并通过 HARNESS_WORKSPACE / --add-dir 暴露目标 workspace
4. `_detect_tool_denied` 识别 stderr 中的 tool not allowed 模式
5. `_detect_wrap_missing` 用 `mock git log --since=T0` 检测 `[cron autonomous]` commit 存在
6. spawn timeout → SIGTERM 流程 → status=timeout
7. spawn 失败 → status=error，该批次全部转升级事件
8. --dry-run 模式只打印决策，不 spawn
9. CLI 退出码矩阵

**集成测试（走真实 CC）**：
- T8 覆盖（AC-P0-004 工具越界硬约束）

**验证标准**：
- pytest 全过 ≥90% cov（mock subprocess）
- 对应 AC-P0-003 通过（AC-P0-004 由 T8 端到端验证）

**完成标准**：
- 代码 commit（`feat(bigbird/agent-harness): T4 heartbeat dispatcher + autonomous session spawn`）

---

### T5：escalation.py + push_now + flush_daily_aggregate + `harness escalation` CLI

**描述**：实现 design.md §2 escalation 模块 + `record_escalation()` / `push_now()` / `flush_daily_aggregate()` + CMD-003。TG 推送用 curl 调 Relay bot token（复用早报 EPUB push 模式）。

**输入**：
- design.md §3 CMD-003
- design.md §5 Flow-003

**输出**：
- `harness/escalation.py`（含 `record_escalation()` / `push_now()` / `flush_daily_aggregate()` / `_build_daily_digest()` / `_curl_telegram()`）
- `harness/cli.py` 扩展 `harness escalation` 子命令组（`flush-daily` 子命令）
- `tests/test_escalation.py`

**TDD 要求**（Unit，覆盖率 ≥90%）：
1. `record_escalation` 按 policy.push_immediately 规则触发立即 push；按 aggregate_daily 仅入队（push_status=pending）
2. `push_now` mock curl 返回 ok → event.push_status=sent；失败 → 重试 1 次 → 仍失败 push_status=failed
3. `flush_daily_aggregate` 读当日 escalation_event.jsonl 的 pending + aggregate_daily → 合并日报 → push → 更新 push_status=sent
4. `_build_daily_digest` 按 match_label 分组计数（例："cross_owner 1 / major_drift 2"）
5. 零事件情形下 flush 静默退出 0
6. curl 命令的 TG token 从环境变量读（`RELAY_BOT_TOKEN_<AGENT>`），缺失时失败并产 failed 事件

**验证标准**：
- pytest 全过 ≥90% cov
- 对应 AC-P0-006 通过

**完成标准**：
- 代码 commit（`feat(bigbird/agent-harness): T5 heartbeat escalation + TG push`）

---

### T6：锁协议实现（`.harness/locks/<agent>.lock`）

**描述**：实现 design.md §6 决策-003 锁协议：flock + 非阻塞 scan skip + 10s defer 分身 + 30s 人工等待。提取为 `harness/locks.py` 独立模块；scan 和 dispatcher 都调。

**输入**：
- design.md §6 决策-003
- design.md §7 错误处理表（锁获取失败三场景）

**输出**：
- `harness/locks.py`（含 `acquire_lock(agent_id, mode)` context manager + `LockHeldError` 异常）
- `harness/scan.py` 调 `acquire_lock(agent_id, mode="nonblock")`
- `harness/dispatcher.py` 调 `acquire_lock(agent_id, mode="defer")`
- 人工 session 的 30s 等待模式 `acquire_lock(agent_id, mode="wait", timeout=30)` — Agent 自行调（P0 不强制集成到人工 session）
- `tests/test_locks.py`

**TDD 要求**（Unit + Integration，覆盖率 ≥90%）：
1. 单进程抢锁成功、释放
2. 两个进程并发抢锁：第二个 nonblock 立即失败；defer 10s 后重试；wait 等待
3. 进程崩溃 → kernel 自动释放（用 `os.kill + test 其他进程可抢`）
4. Context manager 异常路径：raise 时也释放锁
5. 锁文件目录不存在时自动创建 `.harness/locks/`
6. 集成：scan 抢锁失败 → exit 0 + scan_skipped 事件；dispatcher 分身抢锁失败 → 10s 后再试仍失败 → 升级事件

**验证标准**：
- pytest 全过 ≥90% cov
- 对应 AC-P0-007 通过

**完成标准**：
- 代码 commit（`feat(bigbird/agent-harness): T6 heartbeat lock protocol`）

---

### T7：Reeve `heartbeat-policy.yaml` + Relay `cron-jobs.json` 两条 job

**描述**：配置层落地。Reeve 样板 policy + Relay cron job。无代码改动，纯配置。

**输入**：
- requirements.md FR-P0-002（Reeve policy 完整样例）
- design.md §10（Relay cron job 规格）
- Relay `config/cron-jobs.json` 已有格式（3 条早报 job 作参考）

**输出**：
- `.harness/policies/reeve.yaml`（Reeve HeartbeatPolicy，按 requirements 样例 + design schema 落地）
- `PULSE/Relay/config/cron-jobs.json` 追加 2 条：
  - `heartbeat-reeve-scan`（`*/15 * * * *`，workspace=`.harness/reeve-space/`，prompt=chain command）
  - `heartbeat-daily-flush`（`0 20 * * *`，workspace=`.harness/`，prompt=`harness escalation flush-daily`）
- 如 Reeve 无对应 TG bot token，与 Pulse 协调配置 `RELAY_BOT_TOKEN_REEVE` 或复用 Aki token（P0 阶段不介意接收方是大鸟自己）

**TDD 要求**：
- `harness policy validate reeve` 对 reeve.yaml 返回退出码 0
- `cron-jobs.json` schema 符合 scheduler 加载规范（手动跑 Relay 重启验证加载成功）

**验证标准**：
- Relay 启动日志显示两条 heartbeat job 注册成功
- 手动触发 `harness scan reeve --mode manual --json` 能通

**完成标准**：
- 两份配置分别 commit：
  - harness 仓库：`chore(bigbird/agent-harness): T7 Reeve heartbeat-policy.yaml`
  - Relay 仓库（submodule）：`chore(pulse): T7 heartbeat cron jobs`

---

### T8：集成测试 + Reeve 一周 spike + spike-report.md

**描述**：端到端验证 + 真实 spike。这是 feature 能否 sign-off 的关键门。

**输入**：
- requirements.md AC-P0-004 / AC-P0-005 / AC-P0-007 / AC-P0-012
- T1-T7 全部完成

**输出**：

**阶段 A：集成测试（≤2 天）**
- `tests/test_integration_heartbeat.py`
  - AC-P0-004：构造诱导分身越界 prompt → spawn 真实 CC 分身 → 验证 stderr 含 tool denied + 无 Write 副作用 + 事件 status=tool_denied
  - AC-P0-005：构造 3 条合规 review handoff → cron chain 触发 → 15 分钟内 state 变更 + commit 含 `[cron autonomous]` + autonomous_execution_event.status=success
  - AC-P0-007：3 锁场景（scan 抢不到 skip / 分身 defer / 人工等待）

**阶段 B：Reeve 一周 spike（持续观察）**
- D0：打开 cron job（enabled=true），记录 baseline CC 配额
- D1-D7：每日检查
  - `.harness/events/*.jsonl` 增长正常（无空白）
  - 统计 scan 触发次数 / clean 率 / autonomous spawn 成功率 / 升级 push 数
  - 如发现异常（tool 越界尝试 / /wrap 缺失高发 / 锁死锁）即时 debug
- D7：汇总数据，产 `features/heartbeat-runtime/spike-report.md`

**Spike Report 结构**：
```markdown
# Reeve Heartbeat Spike Report (D0..D7)

## 触发统计
- scan 触发总次数：N（预算 672）
- clean 率：%
- 有命中次数：
  - 机械档：
  - 受限 LLM 档 spawn：
  - 升级档事件：
    - 即时 push：
    - 日粒度 push：
## S1 自动化量化（核心）
- Reeve 分身实际处置 pending 数：N ≥ 5 ✅/❌
## 安全验证
- Tool allow-list 违规尝试：N（应为 0）
## 成本
- Reeve 额度消耗：$ （≤ baseline + $5）
## 锁协议
- scan_skipped 次数：
- deferred_autonomous_session 次数：
## 异常 + 改进
- Incident 1: ...
- 下一步调整建议（policy frequency / allow_tools / 升级阈值）
## Sign-off 建议
- P0 达标 ✅/❌
- 是否进入 P1 模板化 + 其他 Agent 扩展
```

**验证标准**：
- AC-P0-004 / AC-P0-005 / AC-P0-007 集成测试全过
- AC-P0-012 七项指标全达标

**完成标准**：
- 代码 commit：`test(bigbird/agent-harness): T8 heartbeat integration tests`
- spike-report.md commit：`docs(bigbird/agent-harness): T8 Reeve heartbeat spike report`
- 向大鸟汇报 spike 结果，确认是否进入 P1

---

## 3. 任务依赖关系

```
   T1 (policy)    T2 (events)
      │                │
      ├────────┬───────┤
      │        │       │
      ▼        ▼       ▼
   T3 (scan) T5 (escalation)
      │        │
      │        │
      ▼        │
   T4 (dispatcher)
      │        │
      ▼        │
   T6 (locks) ─┘
      │
      ▼
   T7 (config) ─► T8 (integration + spike)
```

**并行**：T1 & T2 完全独立；T3 / T5 可在 T1 T2 完成后并行；T4 依赖 T3；T6 在 T3/T4 完成后加挂。

---

## 4. 完成标准

### TDD 要求（测试金字塔）

- **Unit Tests（≥70%）**：policy / scan / dispatcher / escalation / events / locks 的模型校验 + 核心函数逻辑
- **Integration Tests（≥20%）**：CLI 端到端（走真实 subprocess 但 mock CC）；锁三场景；事件 JSONL 读写
- **E2E / Spike（≥10%）**：T8 阶段 A 真实 CC 分身（AC-P0-004/005）；阶段 B Reeve 一周真实运行

**全局覆盖率**：保持整体 ≥80%；新增模块各自 ≥90%（除 CLI ≥70%）。

### 代码质量（遵循 project-standards.md）

- PEP 8 + ruff clean
- mypy strict 通过
- 公开函数 Google style docstring
- 单文件不超过 project-standards 推荐上限（500 行，否则拆分）

### UAT 验收

- 大鸟对 spike-report.md 的数据认可（AC-P0-012 七项达标）
- S1 自动化可量化证据呈现（Reeve 分身实际处置 ≥ 5 个 pending）
- 无 tool allow-list 越界（=0）

---

## 5. 执行顺序（建议）

### 阶段 1：基础层（并行，2 天）
- T1 policy.py + validate CLI
- T2 events.py + 5 类事件

### 阶段 2：引擎层（并行，2.5 天）
- T3 scan.py + CLI
- T5 escalation.py + CLI（独立路径，可提前完成）

### 阶段 3：调度核心（1.5 天）
- T4 dispatcher.py + autonomous session spawn
- T6 locks.py（T4 完成后 1 天补齐）

### 阶段 4：配置落地（0.5 天）
- T7 Reeve policy.yaml + Relay cron jobs

### 阶段 5：验证（2 天 + 一周观察）
- T8 阶段 A 集成测试
- T8 阶段 B Reeve 一周 spike
- spike-report.md

**总时间**：~7 天编码 + 一周 spike，共 ~2 周对齐 requirements §5 P0 时间约束。

---

## 6. 风险 + 缓解

| 风险 | 概率 | 影响 | 缓解 |
|------|:---:|------|------|
| CC `--allowedTools` 运行时行为与预期不符（实际可能不拒绝某些工具） | 中 | AC-P0-004 失败 | T8 阶段 A 尽早跑端到端测试；备用方案：在 prompt 内添加"禁用工具"条款 + Reeve 异步审计兜底 |
| Reeve 分身 /wrap 缺失频发（commit message 未含标记） | 中 | AC-P0-010 失败 | T4 dispatcher 的 prompt 显式要求；检测缺失时即时升级；Reeve workspace 的 AGENTS.md 可能需强化 /wrap 纪律 |
| Reeve spike 期间无足够 pending handoff 触发 S1 自动化（clean 率过高） | 中 | AC-P0-012 S1 量化指标达不到 | spike 开始前 Ken 主动安排几条 test handoff（标注为 spike 测试）给 Reeve |
| TG push 被 Relay token 限流 | 低 | AC-P0-006 失败 | 复用早报 EPUB push 已验证链路；失败重试 + 本地 log 兜底 |
| 锁协议在 macOS flock 行为与 Linux 有差异 | 低 | 并发验证失败 | T6 Integration Test 直接在 macOS 跑；必要时换 `portalocker` 依赖（仅此一处） |
| autonomous spawn subprocess 占用 CC token 导致其他场景配额不足 | 中 | 成本超预算 | FR-P0-008 硬上限 + 熔断；spike D3 中期 review，若超预算立即降频至 */60 |

---

## 7. 不在任务范围（对齐 requirements §6）

- ❌ L1 session 启动扫（wi_722_v2 独立任务）
- ❌ 其他 Agent 的 policy.yaml（P1）
- ❌ Policy schema 抽象化 + 通用模板（P1，待 Amanda 配方回路）
- ❌ 滚动窗口阈值算法（`features/observability-for-reeve/`）
- ❌ 数据周报生成（同上）
- ❌ 修改 Relay scheduler.ts 源码
