---
feature_name: "dispatcher-context-hygiene"
version: "0.0.1"
created: "2026-04-20T14:40:00+0800"
modified: "2026-04-20T14:40:00+0800"
upstream_dependency:
  - "project-specs/project-vision.md"
  - "project-specs/project-architecture.md"
  - "project-specs/project-standards.md"
  - "features/heartbeat-runtime/requirements.md"
  - "features/heartbeat-runtime/design.md"
  - "features/dispatcher-context-hygiene/BACKLOG.md"
tags: [requirements, l2-spec, dispatcher, context-hygiene, handoff-lifecycle]
co_owners: [ken]
changelog:
  - "v0.0.1 (2026-04-20): 初始版本。触发事件：Amanda 2026-04-20 smoke test 暴露 dispatcher 分身基于陈旧 ho 原文执行、覆盖已被人工 session commit 的工作。范围聚焦 P0 三条（硬契约 1+3、软契约 5），不动 handoff schema。"
---

# dispatcher-context-hygiene — 功能需求文档

> **功能概述**：给 heartbeat-runtime `dispatcher` 增加 **spawn 前上下文核对**，防止 cron 分身处理已失效或正在被人工 session 处理的 handoff。核心 3 条——wi 生命周期门卫（终态 wi 的 handoff 不 spawn）+ workspace 活性检测（近 N 分钟有 commit 则跳过）+ 近期 commit 摘要注入分身 prompt（让分身能识别"已做"）。不动 `HandoffContract` schema。P0 目标是解掉 2026-04-20 Amanda 事故暴露的结构缺陷，让 cron `enabled=true` 真实启用成为可能。
>
> **关联文档**：
> - 触发证据：`features/dispatcher-context-hygiene/BACKLOG.md`（4/20 事故现场 + 原始诊断）
> - 事故教训：`ken-space/memo/lessons-2026-04-20-ken-wi-quality-reset.md`
> - Amanda 人格层互补：commit `5afffbbb2`（accept=接手即锁）
> - 上游 feature：`features/heartbeat-runtime/` dispatcher 模块
> - L2 技术设计：`features/dispatcher-context-hygiene/design.md`
> - L2 任务拆分：`features/dispatcher-context-hygiene/tasks.md`

---

## 0. 问题、定位与概念模型

### 问题（2026-04-20 Amanda smoke test 暴露）

2026-04-19 Amanda 在人工 session 做完 `ho_20260419_071` 的实质工作（HEARTBEAT.md + CLAUDE.md 升级），commit 多条（f855c55c6 等），但**没 `harness ack` 这条 ho**。2026-04-20 cron 扫描按 `ack_state=pending` 命中该 ho，dispatcher 按 `autonomous_llm` 档 spawn CC 分身。分身：

- **技术指标全绿**：exit=0 / status=success / duration=485s / 无 tool_denied / /wrap 完整
- **行为方向错**：
  - 处理了已被覆盖的"死 ho"（wi_576 + 人工 commits 已替代其内容）
  - 产物反向覆盖今天精简版（HEARTBEAT.md 81→160 行，回退了"多 why 少 how"精简）
  - 按 prompt 指令 0→100% 推进，而 Amanda 人格要求 80%→100% 增量
  - 又对 Ken 发 `ho_20260420_680` 请审（与 Amanda 人工 session 刚发的 `ho_20260420_047` 重复）

Amanda 事后人工 revert + 沉淀 `accept=接手即锁` 纪律（人格层兜底）。

### 诊断（Amanda 原话采纳）

> "根因不在分身，在 context 缺口——dispatcher prompt 只给 handoff.input_summary + evidence_path，不含'此 ho 生命周期'（是否已被其他 ho / wi / 人工 commit 覆盖）。"

### Smoke #2 补充证据

Amanda 2026-04-20 下午又跑 batch 3 条，核心问题已解决；但暴露**次要缺陷**：分身在"已被 commit 覆盖"场景**倾向 accept + 空 delta** 而非严格 reject。识别出"已做"但走"软处置"，结果等价 reject 但流程不干净，浪费一次 spawn/CC 配额。Amanda 原话："sub-agent 模拟严格 reject，真 spawn 不严格。不致命但……"

### 定位：人格层 × dispatcher 层双兜底

| 层 | 归属 | 解决的问题 | 本 feature 覆盖 |
|----|------|----------|--------------|
| 人格层（Agent workspace HEARTBEAT/SOUL）| 各 Agent 自主 | 接手即 ack accept、处置已覆盖 ho 时 ack reject | ❌ 不在本 feature 范围（Amanda 已在她 ws 做完） |
| dispatcher 层（harness）| Ken | wi 已终态不 spawn、人工 session 在场不 spawn、分身能识别"已做" | ✅ 本 feature 交付 |

**两层不互斥**：即使 Agent 忘了 ack accept，dispatcher 硬契约兜底；即使硬契约边界 case 漏过，人格纪律再兜一次。

### 范围与边界

**in-scope（P0）**：
- 硬契约 1 — wi 生命周期门卫
- 硬契约 3 — workspace 活性检测（近期 commit）
- 软契约 5 — 近期 commit 摘要注入分身 prompt

**out-of-scope（P0 不做，观察期后再议）**：
- 硬契约 2 — 同 wi 新 ho 优先级（需 `supersedes_ho_id` schema 变更，446 现存 handoff 迁移风险高）
- 软契约 4 — handoff 家族上下文注入 prompt（与硬契约 2 同依赖）
- 推广到其他 Agent review-request（待 Ken 与 Reeve/Aki 协商节奏）

---

## 1. 用户故事

### Story-001 — dispatcher 拦住终态 wi 的 ho（硬契约 1）

**As** 一个 heartbeat-amanda-scan cron tick，
**When** 我在 scan 结果里命中了一条 `handoff.ack_state=pending` 的 ho，但它对应的 `work_item.status ∈ {completed, failed}`，
**Then** 我**不 spawn CC 分身**；该 hit 的 disposition 记为 `skipped_wi_terminal`；dispatch event 保留 trail；人工 owner 收到轻量通知（进入日度聚合而非立即 push）。

**动机**：2026-04-20 事故里 wi 其实已被 wi_576 覆盖，但 dispatcher 对 wi 层状态一无所知。这一条直接拦住 99% 类似场景。

### Story-002 — dispatcher 发现人工 session 在场则让路（硬契约 3）

**As** 一个 heartbeat-amanda-scan cron tick，
**When** 我命中一条 pending ho，发现对应 Agent workspace 最近 N 分钟（默认 30）内有 commit，
**Then** 我**不 spawn CC 分身**；该 hit 的 disposition 记为 `skipped_workspace_active`；下一 cron tick（N 分钟后）再 scan 时若 workspace 已静默则正常处理。

**动机**：并发秒级窗口的兜底。人工 session 可能还没 ack 但已在改文件。

### Story-003 — 分身拿到近期 commit 摘要（软契约 5）

**As** 一个被 dispatcher spawn 的 CC 分身，
**When** 我启动时收到的 prompt 不只含 handoff.input_summary + evidence_path，还含"近期 workspace commit 摘要（最近 M=10 条）"，
**Then** 我能识别"此事已有人做过"并走严格 `harness ack --reject`（或 reply 说明），而不是软处置（accept + 空 delta）。

**动机**：Smoke #2 发现 LLM 宽容倾向——光靠分身自判不够，必须在 context 里塞线索。

---

## 2. 功能需求（FR）

### FR-P0-001 — wi 生命周期门卫前置检查

`dispatcher.dispatch()` 在对每个 `hit` 判分流前，查 `hit.ref_id`（或 handoff 的 `work_item_id`）对应的 `WorkItem`。若：

- wi 不存在 → 保守按原逻辑（降格：按 hit_type 常规分流）
- wi.status ∈ {completed, failed} → 该 hit **跳过 spawn**，disposition.autonomy 仍标原档但 `outcome="skipped_wi_terminal"`，不进入 `autonomous_batch`
- wi.status ∈ {draft, in_progress, blocked} → 继续原逻辑

**副作用**：当有 hit 被 skip 时，DispatchEvent.dispositions 里该条含 `outcome="skipped_wi_terminal"` + 原因字段 `skip_reason`（可选，默认 f"wi {wi_id} status={status}"）。

### FR-P0-002 — workspace 活性检测前置检查

对 `autonomous_llm` 档的每条 hit（或整个 batch），在 spawn 前 shell out `git log` 查 `policy.workspace` 路径下最近 N 分钟的 commits。若有任一 commit 时间戳晚于 `now - N*60`s：

- 该 hit（或 batch 整体）**跳过 spawn**，disposition.outcome = `"skipped_workspace_active"`
- N 从 `policy.workspace_activity_window_minutes` 读，默认 30（P0 policy schema 新增该字段，向后兼容：缺省 = 30）
- 日度聚合通知（非立即 push）

**策略选择**：P0 采用**跳过不降档**。理由：如果 workspace 活跃，说明人工可能正在处理，下一 tick 再试比机械档登记更保守。

### FR-P0-003 — 近期 commit 摘要注入分身 prompt

`dispatcher._build_prompt()` 新增一段 "【近期 workspace 动向】"，内容 = `policy.workspace` 最近 M 条 commits 的一行摘要（`git log -M --pretty="- %h %s" --no-merges`）。

- M 默认 10，从 `policy.autonomous_llm_config.recent_commits_limit` 读（P0 新增字段，向后兼容）
- 若 `workspace` 非 git 仓库或 `git log` 失败 → 该段省略，不阻塞 spawn
- 段落位置：【扫描发现】之后、【Autonomy 边界】之前
- 段落文案附加提示："如你判断上述 hit 已被近期 commit 实质覆盖，应 `harness ack --reject --reason '已由 commit X 完成'`，不做重复工作"

### FR-P0-004 — DispatchEvent disposition 枚举扩展

`DispatchEvent.dispositions[].outcome` 字段（原 dict 无强类型）接受两个新值：
- `"skipped_wi_terminal"` — 对应 FR-P0-001
- `"skipped_workspace_active"` — 对应 FR-P0-002

不改现有枚举（`"recorded"` / `"spawned"` / `"planned"` / `"escalated"`），累加即可。

### FR-P0-005 — policy.yaml 新增两字段（向后兼容）

`HeartbeatPolicy` 新增（缺省值即默认策略，已有 amanda.yaml 不改也能工作）：

- `workspace_activity_window_minutes: int = 30` — 顶层字段
- `autonomous_llm_config.recent_commits_limit: int = 10` — 嵌套字段

Policy 变更走 agent-self-authored-policy 流程（Agent 可自撰覆盖默认）。

### FR-P0-006 — CLI 可见性

`harness dispatch <scan_id> --dry-run` 输出包含 skip 原因（如 `[pending_handoff] ... → autonomous_llm (skipped_wi_terminal: wi_576 status=completed)`）。

---

## 3. 非功能需求（NFR）

### NFR-001 — 性能

- 每次 `dispatch` 的前置检查延迟 ≤ 500ms（含若干 `store.load_work_item` + 一次 `git log`）
- workspace `git log` 使用 `--since` 参数裁剪，不全量扫描历史

### NFR-002 — 失败容忍

- 所有前置检查的错误（wi 文件损坏 / git 命令失败 / workspace 非 git 仓库）**不阻塞 dispatch**
- 错误记录到 stderr，降格为原逻辑继续；这样不因 hygiene 检查本身故障让 heartbeat 停摆

### NFR-003 — 可观测性

- 每次跳过都在 DispatchEvent 留 trail（dispositions 一条 + outcome + skip_reason）
- 支持下游（observability-for-reeve）聚合"skip 率"指标

### NFR-004 — 向后兼容

- 现有 446 handoff 文件 + 现有 amanda.yaml 不改，本 feature 能正常工作
- 旧 event log 不回填，只前向生效

---

## 4. 接受标准（AC）

### AC-P0-001 — wi 终态 skip（单元）

给定 wi_X status=completed + ho_Y ack_state=pending 指向 wi_X，调用 `dispatch(scan_with_ho_Y)` → DispatchEvent.dispositions 含一条 `outcome=skipped_wi_terminal`，`spawn_autonomous_session` **不被调用**。

### AC-P0-002 — workspace 活跃 skip（单元）

给定一个有最近 commit（时间戳 now - 5min）的 tmp git workspace + policy.workspace_activity_window_minutes=30 + autonomous_llm 档的 hit → 调用 `dispatch()` → DispatchEvent.dispositions 含 `outcome=skipped_workspace_active`，`spawn_autonomous_session` **不被调用**。

### AC-P0-003 — workspace 静默不 skip（单元）

同 AC-P0-002，但最近 commit 时间戳 now - 60min → 正常 spawn。

### AC-P0-004 — prompt 含近期 commit 摘要（单元）

mock `git log` 返回 3 条 commit → `_build_prompt()` 输出含 "【近期 workspace 动向】" 段 + 3 条一行摘要 + 重复工作提示文案。

### AC-P0-005 — prompt 无 git 仓库时静默省略（单元）

`policy.workspace` 指向非 git 目录 → `_build_prompt()` 输出**不含** "【近期 workspace 动向】" 段，dispatch 继续成功。

### AC-P0-006 — dry-run 可见 skip（CLI）

`harness dispatch <scan_id> --dry-run` 对含 skip hit 的 scan 输出行含 `(skipped_wi_terminal: …)` 或 `(skipped_workspace_active: …)`。

### AC-P0-007 — 4/20 事故复现被拦住（集成/自测）

在 tmp repo 复现：wi_X in_progress + ho_Y ack_state=pending 指向 wi_X + 随后 commit 覆盖（模拟人工 session）→ 调用 `dispatch()` → `spawn_autonomous_session` **不被调用**（硬契约 3 workspace 活性拦住）。

**补充变体**：若 wi_X 已 completed 而 workspace 静默 → 硬契约 1 拦住。若 wi_X in_progress 且 workspace 静默 → 正常 spawn（不过度保护）。

### AC-P0-008 — Amanda smoke #2 场景复现被软契约兜底（集成）

在 tmp repo 加近 10 条 commits（其中有"HB 精简 75 行"这类信号）→ spawn prompt 含该 commit 摘要 + 重复工作提示；分身（mock CC）拿到该 prompt 能识别（此条不要求真实 LLM 测试，只验证 prompt 构造含足够信号）。

### AC-P0-009 — 单元测试覆盖率

`harness/context_hygiene.py` + `dispatcher.py` 改动部分测试覆盖率 ≥ 95%；整个 harness 包覆盖率不降（保持 100%）。

### AC-P0-010 — 观察期无新事故

dispatcher-context-hygiene L3 交付后，启 cron `/60` 跑 3 天（Amanda 分阶段方案阶段 1）。观察窗口内：
- 0 起"分身覆盖人工工作"事故
- `skipped_wi_terminal` / `skipped_workspace_active` 事件数 ≥ 1（证明硬契约真实触发）
- `accept 空 delta` 事件数 = 0（证明软契约 5 让分身走严格 reject）

若 AC-P0-010 不过 → 开硬契约 2 P1 立项（supersedes_ho_id schema）。

---

## 5. 依赖与约束

### 上游依赖

- `heartbeat-runtime` P0 已交付（dispatcher.py / policy.py / scan.py / events.py 基础）
- `store.py` 已有 `load_work_item` 能力
- 本地有 `git` 可执行文件（Mac Mini / 阿里云 ECS 均已有）

### 技术约束

- **Python stdlib + 现有依赖**，不加新 pip 包
- **不改 HandoffContract schema**（P0 红线）
- **不改现有 DispatchEvent schema**（仅扩 outcome 取值，dict 字段向下兼容）

### Policy 变更（FR-P0-005）走协议

`workspace_activity_window_minutes` + `recent_commits_limit` 加入 `HeartbeatPolicy` 后，Amanda 的 amanda.yaml 可选显式覆盖。默认值不改变现有 amanda.yaml 行为（除了首次 spawn 时 prompt 多了一段——Amanda 收到 prompt 时若不喜欢该段可在 policy 里把 `recent_commits_limit=0` 禁用）。

### 兼容性红线

- 不破坏 389+ 现有测试（含 711 tests 整体基线）
- 不影响 heartbeat-runtime 已生产的 3 个 cron job（早报×3）——本 feature 不碰 `cron-jobs.json`

---

## 6. 非本期扩展（P1+ backlog）

| 项 | 触发条件 | 备注 |
|----|---------|------|
| 硬契约 2 — 同 wi 新 ho 优先级 | AC-P0-010 观察期出现 >3 次 accept 空 delta | 需 schema 加 `supersedes_ho_id` |
| 软契约 4 — handoff 家族上下文注入 prompt | 同上 | 与硬契约 2 同依赖 |
| 跨 Agent workspace 活性检测 | Reeve/Aki 启 cron 时 | 当前只对 policy.workspace 单目录，跨 Agent 协作场景可能需扩展 |
| 事件聚合可视化 | observability-for-reeve feature | skip 率 / spawn 成功率指标 |

---

## 7. 风险与对策

| 风险 | 概率 | 影响 | 对策 |
|------|------|------|------|
| workspace 活性检测窗口过大导致频繁跳过 | 中 | Agent 自主性下降 | 默认 30min 可调；Amanda 可在 policy 里改成 15min |
| wi 生命周期门卫误拦（wi 已 completed 但实际还有后续 ho 待处理） | 低 | 漏处理合法后续工作 | 业务上 completed wi 不应再派新 ho；若真有场景，通过反向 handoff + 新 wi 走 |
| `git log` 故障（超时 / 二进制缺失）阻塞 dispatch | 低 | heartbeat 停摆 | NFR-002：所有前置检查失败降格，不阻塞 |
| Amanda 觉得 prompt 多出的"近期 commit 摘要"段干扰 | 低 | 噪音 | policy 可设 `recent_commits_limit=0` 禁用 |
| 观察期未触发任何 skip（硬契约形同虚设） | 低 | 证据不足判断有效性 | Ken 自测阶段手工构造触发场景（含 AC-P0-007 集成测试） |

---

## 8. 术语表

- **终态 wi**：`WorkItem.status ∈ {completed, failed}` 的 wi
- **活性 workspace**：最近 N 分钟（默认 30）有 commit 的 Agent workspace
- **软契约**：不拦截 spawn，但往 prompt 里注入上下文让分身自判
- **硬契约**：在 spawn 前直接拦截，不给 LLM 机会犯错
- **"accept 空 delta"**：分身 accept 一条 ho 但产出为空（Smoke #2 观察到的软处置行为）
