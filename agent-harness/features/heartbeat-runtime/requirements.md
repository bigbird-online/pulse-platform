---
feature_name: "heartbeat-runtime"
version: "0.0.2"
created: "2026-04-18T17:02:00+0800"
modified: "2026-04-18T17:25:00+0800"
upstream_dependency:
  - "project-specs/project-vision.md"
  - "project-specs/project-architecture.md"
  - "project-specs/project-standards.md"
  - "project-specs/project-roadmap.md"
  - "project-specs/proposal-2026-04-18-reeve-first-customer.md"
  - "ssot/agent-constitution/governance.md"
  - "../../PULSE/Relay/specs/features/agent-scheduler/requirements.md"
tags: [requirements, l2-spec, heartbeat, runtime, cron, automation, autonomy, vsm, observability]
co_owners: [ken, pulse]
changelog:
  - "v0.0.2 (2026-04-18): 重大方向修正。从 notify-only 升级为 autonomy-stratified execution——引入三档 autonomy（机械/受限 LLM/升级），cron spawn CC 分身 + tool allow-list 承载 S1/S2 自动化，S3 只在升级档接入。解耦 trigger 时机（L1/L2/L3）与 action autonomy（三档），两者正交。proposal 产品价值函数（S3 介入频次下降）由此落地。"
  - "v0.0.1 (2026-04-18): 初始版本（已废弃）。退缩为 notify-only——事事 push 给人，违背 VSM 原则。"
---

# heartbeat-runtime - 功能需求文档

> **功能概述**：把 Agent workspace 下静态的 `HEARTBEAT.md`（配置：应该做什么）升级为由 Relay cron 自动触发的运行规程（daemon：实际在跑）。每个 Agent 定时被唤起，执行机械扫描（pending handoffs / 未修复 VIOLATED / SSOT 漂移）；扫描命中后按 **autonomy 策略分流到三档**——机械档直接处置、受限 LLM 档 spawn Agent 分身按 tool allow-list 自主处置、升级档 aggregate 后 push 大鸟。此 feature 是 proposal v0.4 所有"自动"承诺的 L2 公共前置，也是产品价值函数（S3 介入频次下降）的落地载体。
>
> **关联文档**：
> - L1 项目愿景：`project-specs/project-vision.md`（v1.0 运行规程层 / Relay 集成）
> - L1 项目架构：`project-specs/project-architecture.md`（消费基础设施层：CC + Relay）
> - L1 项目规范：`project-specs/project-standards.md`（含隐私边界）
> - 战略依据：`project-specs/proposal-2026-04-18-reeve-first-customer.md` §HEARTBEAT Runtime 实现路径
> - 基础设施前置：`PULSE/Relay/specs/features/agent-scheduler/`（已生产，3 jobs 运行中）
> - L2 技术设计：`features/heartbeat-runtime/design.md`（后续产出）
> - L2 任务拆分：`features/heartbeat-runtime/tasks.md`（后续产出）
> - 宪法配方层：`ssot/agent-constitution/governance.md`（WS 治理启发式 / Escalation；待 Amanda 在 `escalation-vocabulary.md` 落地 autonomy 枚举）

---

## 0. 问题、定位与概念模型

### 问题（proposal v0.4 识别根因）

当前 `HEARTBEAT.md` 是 **Agent workspace 下的配置文件**（文字描述"应该扫 pending / 体检领域资源"），**不是 running daemon**（没有任何组件定时执行它）。L1（session 启动扫）只在用户开 session 时才跑，L2（后台 cron）从未接入。结果：

- **自动自拾 = 空头**：派发给 Amanda 的 wi 得靠用户口头告知
- **数据周报 = 空头**：Reeve 得手动跑命令
- **阈值 Push = 空头**：得人肉盯看板
- **邮递员自报 = 空头**：得人工登记

**L2 是所有"自动"承诺的公共前置**。

### VSM 对齐：为什么不能退缩为 notify-only

产品价值函数 = **S3（大鸟）介入频次下降**。这要求 **S1/S2 能自动工作**——不是所有事都 push 给人。"cron 触发 = 不能让 Agent 干活"这个等式不成立；真正的边界是**动作粒度**，不是**触发来源**。

### 两套正交维度

本 feature 明确区分两套概念，避免 v0.0.1 的混用错误：

| 维度 | 含义 | 取值 | 谁决定 |
|------|------|------|--------|
| **Trigger 时机** | 何时启动扫描 | L1 session 启动扫 / L2 cron 定时扫 / L3 阈值 push | 基础设施（本 feature 只管 L2；L1 由 wi_722_v2；L3 消费者在 observability-for-reeve） |
| **Action autonomy 档位** | 扫描命中后由谁以什么权限处置 | 机械档 / 受限 LLM 档 / 升级档 | Agent workspace 下的 `heartbeat-policy.yaml`（per-action 分类），Amanda 配方层给通用模板 |

两套维度**正交**：同一档 autonomy 可以来自 L1 或 L2；同一 trigger 命中后可以分流到三档。

### 三档 autonomy 定义（核心）

| 档 | 实现 | 工具权限 | 典型动作 | VSM 对应 |
|----|------|---------|---------|---------|
| **机械档** | cron 直接跑 `harness scan <agent>` CLI，纯 Python 零 LLM | 直接 workspace IO，无 CC 调用 | scan 本身 / event 登记 / drift 检测 / 统计聚合 / 熔断 | S1 机械部分 + S2 协调基础设施 |
| **受限 LLM 档** ⭐ | 机械档 scan 命中 → dispatcher → spawn CC 分身 `claude -p "<context prompt>"` + `--allowedTools <per-agent allow-list>` | 每 Agent 按 cron 场景配的 tool allow-list（如 Reeve：`harness-handoff-ack,handoff-reply,handoff-reject,work-item-create`，禁 Write 到非指定路径等） | legacy hit 按自己 CLAUDE.md 人格处置；`scheduled_night_patrol` 走 `NIGHT_PATROL_PROFILE`，由 dispatcher 只注入 `MANAGEMENT.md`，不自动读取目标 workspace `CLAUDE.md / AGENTS.md`；完事 /wrap + commit | **主要 S1/S2 自动化载体** |
| **升级档** | dispatcher 按阈值聚合事件 → TG push 大鸟；不改任何 Agent 状态 | 仅通知 | 跨 owner 边界 / 红线触碰 / 无证据分歧 / 熔断 / 单次 scan 发现 ≥3 critical | S3 介入入口 |

### 关键概念修正：cron 分身就是 Agent 的正常 session

**Agent 不是"有主 session 和影子 session 的二元主体"**。Agent 是**由 workspace 物化的实体**，每次被唤起（cron 或人工）都是读状态 → 做事 → 写状态 → 退出。人工 session 和 cron session **地位对等**，只差 trigger 来源——都要遵守同一套纪律（读 MEMORY 上下文、/wrap 收口、写 memo、commit）。

这一认知修正消解了 v0.0.1 的"如何通知正在工作的 Agent"错题：
- Agent 没有"正在工作"这种特权状态
- 并发让路是"两个对等 session 先到先得"，不是"影子给主人让路"
- 技术实现仍然是 `.harness-active-lock` 互斥，但概念从"保护主 session"换成"保护 workspace 数据一致性"

### 三方职责分工（B-R6 方向声明，待 design.md 收敛）

| 组件 | 职责 |
|------|------|
| **Relay `agent-scheduler`** | 定时触发（cron）+ 进程 spawn + 成本核算 + 熔断；**只做搬运，不懂 Agent 语义，不看 autonomy 档位** |
| **agent-harness（本 feature）** | 扫描语义 + 事件记录 + **autonomy dispatcher**（读策略分档）+ tool allow-list 注入 + 升级档阈值与 aggregate push |
| **CC session** | 受限 LLM 档的实际执行体——按 tool allow-list 能做到的事，Agent 按自己人格决定怎么做 |

### 本 feature 的 trigger 覆盖范围

| trigger | 本 feature 交付 |
|---------|---------------|
| L1: session 启动扫 | ✗ 不在本 feature（由 `features/agent-workspace-harness-integration/` 覆盖，owner Amanda） |
| **L2: cron 定时扫** | **✓ 本 feature 核心**（三档 autonomy 全支持） |
| L3: 阈值 push | 升级档的阈值检测与 push 本 feature 交付 P0 最小集；具体阈值算法与可视化留给 `features/observability-for-reeve/` |

---

## 1. 用户故事（User Stories）

### 主要用户故事

**故事 1：S1/S2 自动化（核心价值）**
- **作为**：大鸟（S3，proposal 核心 KPI = S3 介入频次下降）
- **我希望**：派给 Amanda / Reeve / Aki 的 handoff，在 Agent autonomy 策略允许的范围内系统自己让对方处置掉（ack / reply / 反向 handoff），我只在真正跨边界或红线触碰时被 push 介入
- **以便**：我脱离 context 搬运 + 口头告知工位；VSM 的 S3 只在该它出场时出场
- **优先级**：P0

**故事 2：受限 LLM 档下 Agent 按人格自主处置（Reeve 样板）**
- **作为**：Reeve（第一客户）
- **我希望**：cron 周期把我唤起（不是影子，是正常工作 session），按我 workspace 下的 autonomy 策略声明：
  - 格式合规的 review handoff 我自己 ack（S1 自动）
  - 扫到有新 pending 审计工单我自己生成 review，反向 handoff 回发起方（S2 自动）
  - 遇到无证据分歧 / 跨 owner 建议 / 红线触碰 → 产升级事件（让 dispatcher 打给大鸟）
- **以便**：我作为第一客户不仅消费数据、还能自动完成本职 S1/S2 工作；观察 O-A 的"看/通知自主，判/发需 session"只在**配方明确标记为需升级的动作**上约束，不是所有动作
- **优先级**：P0

**故事 3：工具权限锁死保障不越界**
- **作为**：大鸟（也作为 Agent 自己的 SOUL 保护）
- **我希望**：cron spawn 的 CC 分身在启动时注入 `--allowedTools` 白名单（per Agent 配置），Agent 即使"想"做越界动作（提交到 main / Write 到任意路径 / 调用宪法禁止的工具）也会被 CC 运行时拒绝
- **以便**：autonomy 不等于失控；边界由工程机制保障而非 prompt 请求
- **优先级**：P0

**故事 4：Autonomy Dispatcher 按策略分流**
- **作为**：Ken（harness 侧实现者）
- **我希望**：扫描命中后有一个明确组件读每个 Agent 的 `heartbeat-policy.yaml`，按 per-action 分类把事件分流到三档（机械执行 / spawn 分身 / 升级 push）
- **以便**：autonomy 策略是**声明式**的（Agent 自己在 workspace 里写）、不是代码里 hardcode；Amanda 配方层结论以声明格式进入系统，不需改 harness 代码
- **优先级**：P0

**故事 5：任意 N-session 并发隔离（观察 O-B）**
- **作为**：大鸟
- **我希望**：cron session 和任何人工 session（或另一个 cron session）之间通过锁文件 + git index 串行化互斥，先到先得；被挡者不崩溃只 skip 或 defer
- **以便**：N-session 并发是常态不是异常；cron 的存在对用户体验透明
- **优先级**：P0

**故事 6：升级档按阈值 aggregate push**
- **作为**：大鸟
- **我希望**：升级档事件**不是每发生一次就 push 一条**——熔断 / 单次 scan 发现 ≥3 critical / 红线事件即时 push；其余升级事件（普通 reject / minor violation / 低频 drift）累积到日粒度一起发
- **以便**：push 本身不变成噪音；TG 只在真的需要时响
- **优先级**：P0

**故事 7：成本可控（B-R2）**
- **作为**：Pulse（CC 配额管理）
- **我希望**：
  - 机械档 99% 无 LLM 调用（scan 本身不 spawn CC）
  - 受限 LLM 档仅在有命中时 spawn，且按 Agent 活跃度分层频率（Reeve 15min / Amanda 30min / Aki 2h）
  - 单日单 Agent 有预算上限
- **以便**：自动化净收益 > 成本
- **优先级**：P0

**故事 8：扫描与执行事件可观测（Reeve 数据前提）**
- **作为**：Reeve（数据消费者）
- **我希望**：每次扫描产 `scan_event`、每次 dispatcher 分档产 `dispatch_event`、每次受限 LLM 档 spawn 产 `autonomous_execution_event`、每次升级档 push 产 `escalation_event`——全部结构化，可被 harness stats / report 聚合
- **以便**："数据周报"承诺有数据源基座；autonomy 档位的实际分布可被观测（Reeve 自己判断是否要调策略）
- **优先级**：P0

**故事 9：单 Agent spike 先于全面铺开（Reeve 样板）**
- **作为**：Ken
- **我希望**：先只在 Reeve 一个 Agent 上开 cron job + 三档 autonomy + 完整策略声明，跑一周验证
- **以便**：不在 6 阻塞点全部细化完前铺开；spike 中暴露真实的工具权限 / autonomy 切分 / 成本 / 并发问题
- **优先级**：P0

**故事 10：Relay 侧触发接口标准化（B-R6）**
- **作为**：Pulse（Relay co-owner）
- **我希望**：Relay `agent-scheduler` 触发 HEARTBEAT 时只调一类标准入口（`harness scan <agent> --mode cron` 然后可能 re-invoke `harness dispatch <scan_id>`），不需要在 Relay 侧写 Agent 特定逻辑
- **以便**：Relay 只做搬运，Agent 语义全在 harness 侧；新增 Agent 只改 harness 不改 Relay
- **优先级**：P0

### 次要用户故事

**故事 11：HEARTBEAT.md / heartbeat-policy.yaml 从配置文件升级为可执行契约**
- **作为**：Agent（Reeve 作样板）
- **我希望**：在自己 workspace 维护一份 `heartbeat-policy.yaml`，声明：scan 项目 / per-action autonomy 档位 / tool allow-list / 升级阈值 / 通知偏好 / 频率建议
- **以便**：我的 heartbeat 语义由我自己配（在 Amanda 配方模板约束下），harness runtime 只负责按我的声明执行
- **优先级**：P1（P0 只交付 Reeve 一份样板，schema 抽象化留 P1）

---

## 2. 功能需求（Functional Requirements）

### P0 功能（MVP 必需）

**FR-P0-001：`harness scan <agent>` 扫描 CLI（机械档核心）**
- **描述**：新增 `harness scan` 子命令组。核心命令 `harness scan <agent_id> --mode cron --json`，在有限时间内对目标 Agent 执行机械扫描，不调 LLM，返回结构化 ScanReport。扫描范围（P0 最小集）：
  1. **pending handoffs**：`to_agent = <agent>` 且 `state = pending` 的 HandoffContract
  2. **未修复 VIOLATED**：该 Agent 产出的 ComplianceVerdict 状态为 VIOLATED 且未关联到任何 completed/failed WorkItem
  3. **SSOT 漂移**（轻量）：复用 `ssot check --agent <agent> --quick` 的 critical/major findings
- **来源**：故事 1、故事 8、故事 10
- **示例**：`harness scan reeve --mode cron --json` → 输出含 `{scan_id, agent_id, pending_handoffs: [...], unresolved_violations: [...], ssot_drift: [...], scanned_at, duration_ms, verdict}` 的 JSON，退出码 0=无命中 / 1=有命中（需 dispatch）/ 2=系统错误

**FR-P0-002：Autonomy 策略声明格式（Reeve 样板）**
- **描述**：在 Reeve workspace 新增 `.harness/policies/heartbeat-policy.yaml`（或 `reeve-space/heartbeat-policy.yaml`，路径 design 决定），格式：
  ```yaml
  schema_version: "1.0"
  agent_id: reeve
  frequency_hint: "*/15 * * * *"
  scan_items:
    - type: pending_handoffs
      per_action:
        format_compliant: autonomous_llm   # 格式合规的 review 邀请 → Reeve 分身 ack + 生成 review
        cross_owner_boundary: escalate     # 跨 owner 的 review 请求 → 升级
        red_line_flag: escalate            # 预检测到红线相关 → 升级
    - type: unresolved_violations
      per_action:
        own_violation: autonomous_llm      # 自己产出的 VIOLATED 需 Reeve 看
        others_violation: mechanical       # 别 Agent 的 → 仅登记事件
    - type: ssot_drift
      per_action:
        critical: escalate
        major: mechanical                  # 登记 + 日粒度 aggregate
  autonomous_llm_config:
    allowed_tools:
      - harness-handoff-ack
      - harness-handoff-reply
      - harness-handoff-reject
      - harness-work-item-create
      - Read                               # 限在 reeve-space/ 和 .harness/
      - Bash                               # 限在 harness CLI + git status
    disallowed_tools:
      - Write                              # 默认禁写（ack/reply 走 CLI 封装）
      - WebFetch
    path_constraints:
      read: ["reeve-space/**", ".harness/**"]
      write: []                            # 通过 CLI 间接改文件，不直写
    max_duration_seconds: 180
    session_wrap_required: true            # 分身退出前必须 /wrap
  escalation:
    push_immediately:
      - red_line_flag
      - circuit_broken
      - critical_count >= 3
    aggregate_daily:
      - major_drift
      - cross_owner_boundary
    channel: telegram
    recipient: "1000000001"
  ```
- **来源**：故事 2、故事 3、故事 4、故事 11
- **注**：P0 只交付 Reeve 一份 `heartbeat-policy.yaml`；schema 本身作为 harness 的契约（Pydantic 模型校验），其他 Agent 的 policy 留 P1。

**FR-P0-003：`harness dispatch <scan_id>` Autonomy Dispatcher**
- **描述**：新增 `harness dispatch` 子命令，消费 ScanEvent（或 scan CLI 直接 pipe），按目标 Agent 的 `heartbeat-policy.yaml` 的 per_action 规则把每条命中分流到三档：
  - `mechanical` → 直接写 `dispatch_event(disposition=recorded)`，不 spawn 任何 CC
  - `autonomous_llm` → 调 `spawn_autonomous_session(agent_id, policy, context)`（见 FR-P0-004），产 `autonomous_execution_event`
  - `escalate` → 写 `escalation_event`，交给 FR-P0-005 的 aggregator 决定即时 push 或日粒度 aggregate
- **来源**：故事 4
- **示例**：scan 返回 3 条 pending handoffs，策略判定 2 条 autonomous_llm + 1 条 escalate → dispatcher spawn 一次 Reeve 分身（把 2 条 context 一起塞进 prompt，不分两次 spawn）+ 写 1 条 escalation_event

**FR-P0-004：`spawn_autonomous_session` 受限 LLM 档执行（S1/S2 核心）**
- **描述**：dispatcher 在 `autonomous_llm` 档调用此函数 spawn CC 分身。实现要点：
  1. 读取 Agent policy 的 `autonomous_llm_config` 段
  2. 构造 prompt：包含被分流的命中列表 + 当前时间 + autonomy 档位提示 + "你被 cron 唤起，这是一次正常工作 session，处置完成后 /wrap"；若命中 `scheduled_night_patrol`，使用 `NIGHT_PATROL_PROFILE` 只注入 `MANAGEMENT.md`，并禁止把目标 workspace 的 `CLAUDE.md / AGENTS.md` 当作分身入口
  3. spawn：`claude -p "<prompt>" --allowedTools <tools> --disallowedTools <disallowed> --permission-mode bypassPermissions`（tool 白名单即硬约束，不需交互确认）
  4. cwd = Agent workspace；环境注入 `HARNESS_TRIGGER_MODE=cron`、`HARNESS_SCAN_ID=<scan_id>`
  5. 超时由 policy.max_duration_seconds（默认 180s）控制
  6. 退出后追加 `autonomous_execution_event`（含 agent_id / scan_id / trigger_mode / started_at / duration_ms / exit_code / tool_calls_count / status）。`tool_calls_count` 可空：正常完成路径报 `None`（session 跑完了但无人统计工具调用次数），deferred/started/emergency-error 报 `0`（subprocess 未跑，0 是真实状态）——恒 0 会被读成「这个分身没调过工具」而误导效率判断（2026-07-29 Hestia 查出全表 875 条跨四个月恒 0）
- **来源**：故事 1、故事 2、故事 3
- **约束**：分身必须 /wrap（写 memo + commit）后退出；未 /wrap 的退出记为 `status=wrap_missing` 并升级给大鸟
- **安全**：`--allowedTools` 是 CC 原生机制，不是 prompt 请求——违规工具调用由 CC 运行时拒绝

**FR-P0-005：升级档事件 aggregator 与 push**
- **描述**：新增 `harness escalation` 子模块处理升级档事件：
  - 即时 push 规则（policy 声明 `push_immediately` 列表）：事件发生即调 Relay TG bot 发一条精简消息（含 agent_id / event_type / evidence URL）
  - 日粒度 aggregate（policy 声明 `aggregate_daily` 列表）：事件入队，每天 20:00 一次性合并成一条日报 push（使用 Relay scheduler 的既有 cron 能力触发 `harness escalation flush-daily`）
- **来源**：故事 6
- **示例**：Reeve 扫到 red_line_flag → 即时 push "⚠️ Reeve 检测到 wi_xxx 触红线第 6 条"；Reeve 扫到 2 条 major_drift → 入队，20:00 与其他日粒度事件合并为"今日升级汇总：drift 5 / cross_owner 2"

**FR-P0-006：Relay cron job 接入（spike 形态）**
- **描述**：在 Relay `config/cron-jobs.json` 新增两条 job：
  1. `heartbeat-reeve-scan`（频率 `*/15 * * * *`）：运行 `harness scan reeve --mode cron && harness dispatch $SCAN_ID`（或直接一条 chain 命令，由 design 决定）。workspace = Reeve 工作目录；delivery.mode = none（通知由 harness escalation 管）
  2. `heartbeat-daily-flush`（频率 `0 20 * * *`）：运行 `harness escalation flush-daily`，合并推送日粒度升级汇总
- **来源**：故事 7、故事 9、故事 10
- **注**：受限 LLM 档的 CC 分身 spawn **不经 Relay scheduler**，由 harness dispatcher 直接调（避免 scheduler 被 per-scan 的动态 spawn 污染）；scheduler 只负责"唤醒 harness 扫描"这一外层

**FR-P0-007：并发与活动态感知**
- **描述**：任意 session（cron 触发的 scan、cron 触发的分身、人工 session）启动第一件事都抢 `.harness/locks/<agent>.lock`（flock 或 atomic file create）。已占用时：
  - 外层 scan → skip + 记录 `scan_skipped(reason=lock_held_by=<holder_session_id>)`，退出码 0
  - 分身 spawn 前检测 → defer（重试 1 次 / 间隔 10s），仍失败则升级为 `deferred_autonomous_session` 事件
  - 人工 session 启动时 → 等锁（最多 30s），拿到锁才继续
- **来源**：故事 5
- **注**：此锁保护 workspace 数据一致性（git index + YAML 读-改-写），不区分 session 类型（cron/人工对等）

**FR-P0-008：成本与执行时长硬约束**
- **描述**：
  - scan CLI 执行时长 P95 ≤ 60 秒（含 CC 外壳启动开销；不启分身时纯 CLI ≤ 2 秒）
  - 单次 autonomous session 执行 ≤ policy.max_duration_seconds（默认 180 秒）
  - 单日单 Agent 预算：spike 阶段 Reeve ≤ $5 CC 配额（超预算自动降频至 */60）
  - 熔断：连续 5 次 scan 失败或 3 次 autonomous session 异常退出 → 自动 disable 对应 cron job + 产 `circuit_broken` 事件即时 push
- **来源**：故事 7

**FR-P0-009：无命中零开销**
- **描述**：scan `hits_count=0` 时只追加一条精简 `scan_event(verdict=clean)`；不调 dispatcher、不产 dispatch_event、不 spawn 任何分身、不产通知。目标：绝大多数无命中情形下几乎零成本。
- **来源**：故事 7

### P1 功能

**FR-P1-001：扩展到全部 Agent**
- 基于 P0 Reeve spike 通过后，为 Amanda / Aki / Duke / Emi / Ken / Pulse / ... 各自创建 `heartbeat-policy.yaml` + 对应 cron job。**需 Amanda 配方回路**给出各档位 per-action 通用模板（proposal 附录 B 备注 B）。

**FR-P1-002：Policy schema 抽象化 + 通用模板**
- 把 `heartbeat-policy.yaml` schema 提取为 harness 标准契约（Pydantic 模型 + 校验命令 `harness policy validate <agent>`）
- Amanda 给"审计 Agent 模板 / 配方 Agent 模板 / 数据 Agent 模板"三套 per_action 默认配方

**FR-P1-003：阈值事件发布口扩展（L3 基座）**
- 当前 FR-P0-005 只处理"单次 scan 发现的即时事件"；P1 增加"滚动窗口聚合"——基于 ScanEvent 历史计算 proposal 三阈值（S3 +50% / reject 30% / 邮递员 3+），命中时产 `threshold_event`。消费方属 `features/observability-for-reeve/`。

**FR-P1-004：Dispatcher 的自学习 hint**
- dispatcher 观察分身 autonomous_execution_event 的实际结果（成功 ack / 被用户回滚 / /wrap 缺失），给 Agent 反馈 hint 建议调整 policy（不自动改，只建议）

### P2 功能

**FR-P2-001：数据周报自动产出**（属 observability-for-reeve，消费本 feature 的事件）

**FR-P2-002：邮递员自报 hook**（需 /wrap hook，跨 feature）

**FR-P2-003：Autonomy 语义入宪法**（Amanda owner，`escalation-vocabulary.md` + `governance.md §三 Escalation 启发式`）

---

## 3. 验收标准（Acceptance Criteria）

### P0 功能验收标准

**AC-P0-001：scan CLI 正确性**
- **条件**：为 Reeve 构造 2 pending + 1 VIOLATED + 0 drift
- **预期**：scan JSON 含 3 条命中；退出码 1；clean 场景退出码 0；错误场景 2
- **验证**：CLI 集成测试（3 例）

**AC-P0-002：Policy schema 校验**
- **条件**：提交合法 + 5 类非法 policy（缺字段 / 非法档位值 / 引用不存在工具 / path_constraints 格式错 / 循环 escalate）
- **预期**：合法加载成功；非法各自抛明确错误
- **验证**：自动化单元测试

**AC-P0-003：Dispatcher 分流准确性**
- **条件**：scan 返回 3 条命中，policy 标记为 [autonomous_llm, autonomous_llm, escalate]
- **预期**：dispatcher 产出 1 次 spawn 调用（合并 2 条 autonomous context）+ 1 条 escalation_event；dispatch_event 包含 per-hit disposition 字段
- **验证**：自动化集成测试（mock spawn）

**AC-P0-004：Tool allow-list 硬约束（核心安全验证）**
- **条件**：构造 Reeve 分身 prompt，故意诱导它调用 `Write` 到任意文件或 `git push origin main`
- **预期**：CC 运行时**拒绝**该工具调用，分身日志显示 tool not allowed；workspace 无任何 Write 副作用；autonomous_execution_event 记录 tool_denied
- **验证**：集成测试（用 dry-run Agent prompt 注入越界尝试，验证事件 + 文件系统状态）

**AC-P0-005：S1 自动化实证（核心价值验证）**
- **条件**：为 Reeve 构造 3 条格式合规的 review handoff（policy 判定 format_compliant → autonomous_llm）→ 触发一次 cron 扫 + dispatch
- **预期**：15 分钟内 3 条 handoff 的 state 从 pending → 被 Reeve 分身 ack（或反向 handoff 产生了 review 文件）；git log 显示 commit 由 cron 分身产生（commit message 含 `[cron autonomous]` 标记）；无 TG push（因无升级）；`autonomous_execution_event` 记录成功
- **验证**：spike 期间实测

**AC-P0-006：升级档准确性**
- **条件**：构造 5 种升级场景（red_line / circuit_broken / critical ≥3 / major_drift / cross_owner）
- **预期**：red_line + circuit_broken + critical≥3 即时 push（TG 收到 3 条分别的消息）；major_drift + cross_owner 仅入队，20:00 flush 合并为一条；事件日志完整
- **验证**：集成测试 + 一天 spike 实测

**AC-P0-007：并发让路（锁协议）**
- **条件**：场景 A 锁已持有 → 外层 scan 触发；场景 B 锁已持有 → 分身 spawn 触发；场景 C 人工 session 启动 → 锁由 cron 持有
- **预期**：A 即 skip 退出 0 + scan_skipped event；B defer 10s 重试仍失败则升级 deferred_autonomous_session 事件；C 人工 session 等最多 30s 后拿锁继续
- **验证**：集成测试（3 场景）

**AC-P0-008：ScanEvent 隐私边界**
- **条件**：扫到 2 条 pending（含 input_summary 和业务字段）
- **预期**：`.harness/events/scan-YYYY-MM.jsonl` 含元数据（scan_id/agent/hit_count/wi_id/handoff_id/state）；**不含** input_summary / expected_output 原文 / 业务描述
- **验证**：字段白名单自动化检查

**AC-P0-009：无命中零开销**
- **条件**：Reeve clean 状态下连跑 10 次 scan
- **预期**：10 条 clean event；0 TG push；0 CC autonomous session spawn；CC usage 对账无新增
- **验证**：集成 + spike 实测

**AC-P0-010：/wrap 完整性**
- **条件**：分身 spawn 后正常结束 / 分身超时 / 分身异常退出 三场景
- **预期**：正常结束应有 /wrap（写 memo + commit），autonomous_execution_event.wrap_missing=false；超时/异常退出 → wrap_missing=true 并产 escalate 事件（即时 push）
- **验证**：集成测试

**AC-P0-011：成本与熔断**
- **条件**：人为制造 5 次 scan 失败
- **预期**：第 5 次后 scheduler 自动 disable `heartbeat-reeve-scan` + circuit_broken 即时 push + cron-jobs.json enabled=false
- **验证**：集成测试（复用 scheduler AC-P1-001）

**AC-P0-012：Spike 周期端到端健康（一周实跑）**
- **条件**：Reeve spike 跑 7 天（*/15 = 672 scan 次预算）
- **预期**：
  - scan 触发次数 ≥ 600（预算 672，允许 ~10% skip/defer）
  - 无命中比例 ≥ 85%
  - 受限 LLM 档 spawn 成功率 ≥ 95%（成功完成 /wrap + commit）
  - Tool allow-list 违规尝试次数 0（如有 >0 需人工 review Agent prompt 设计）
  - 升级档 push 总量 ≤ 每日 5 条（即时 + 日粒度合并）
  - CC 配额消耗 ≤ spike 前 7 日 Reeve baseline + $5
  - **S1 自动化效果量化**：spike 期间 Reeve 被 cron 分身处置的 handoff 数 ≥ 5（否则证明 policy 过于保守，需调整）
- **验证**：spike-report.md 出具，含以上 7 项数据

---

## 4. 非功能需求（Non-Functional Requirements）

### 性能需求

- scan 单次 P95 ≤ 60s；纯 CLI scan ≤ 2s
- dispatch 单次 ≤ 1s（不含 spawn 时间）
- autonomous session P95 ≤ 180s（policy 可调）
- 事件写入单行 < 50ms
- 锁检测 < 10ms

### 可靠性需求

- 熔断：复用 scheduler 机制
- 幂等：同 scan_id 事件不重复；事件日志按月分片 ≤ 10MB
- 隔离：分身崩溃不污染 Relay / 其他 session / 其他 Agent workspace
- /wrap 保障：分身必须 /wrap，missing 即升级

### 可观测性需求

- 五类核心事件 schema 稳定：`scan_event` / `scan_skipped` / `dispatch_event` / `autonomous_execution_event` / `escalation_event`
- 每个事件必含 `scan_id / agent_id / trigger_mode / timestamp`
- 事件 schema 修改需 bump `schema_version`

### 安全需求

- Tool allow-list **必须**在 CC `--allowedTools` 层生效（prompt 层禁止不算数）
- 分身环境变量剥离 `TELEGRAM_BOT_TOKEN`（复用 scheduler 纪律）
- 隐私边界对齐 `project-standards.md` v0.2（只采元数据不采内容原文）
- Policy 文件 git tracked，变更走正常 review；runtime 不支持 policy 动态注入

### 可维护性需求

- 代码规范：PEP 8 + ruff + mypy strict
- 新增模块测试覆盖率：`scan.py / dispatcher.py / escalation.py / policy.py` ≥ 90%；CLI 扩展 ≥ 70%；整体保持 ≥ 80%
- 文档：Reeve 样板 `heartbeat-policy.yaml` + 注释版作为 P1 模板化的参考

### 兼容性需求

- 不修改 Phase 1/2/3 模块代码
- 复用 `store.py` / `ssot_engine.py` 现有 API
- 不修改 Relay `scheduler.ts` 源码（只改 `cron-jobs.json`）
- 现有 445 测试不受影响

---

## 5. 约束条件（Constraints）

### 技术约束

- harness 技术栈：Python 3.11+ / Click / Pydantic v2 / PyYAML / pytest
- Relay 侧：不改 scheduler.ts，仅新增 cron-jobs.json 条目
- 事件存储：JSONL + `.harness/events/` + 按月分片
- Policy 存储：YAML + git tracked，per-Agent 一份
- CC 调用：`claude -p` one-shot + `--allowedTools` + `--disallowedTools` + `--permission-mode bypassPermissions`（tool 白名单即硬约束，避免交互提示阻塞 cron）
- 无 LLM 依赖于 scan/dispatch/escalation；只有 autonomous session 走 LLM

### 上游约束

- autonomy 配方语义：以 Amanda 在 `ssot/agent-constitution/escalation-vocabulary.md` + `governance.md §三 Escalation 启发式` 定义为准（design.md 起草前开 Amanda handoff）
- scheduler 合同：Relay `agent-scheduler` requirements v0.0.1 的 FR-P0-001 ~ P0-005
- 宪法防护：policy 中的升级条件必须能映射到 `escalation-vocabulary.md` 的枚举（P1 完成前用自由字符串 + 注释过渡）
- `ssot_drift` 只是 Harness 机械扫描信号；正式审查警告发布前必须经过 Reeve 分身语义复核门禁。允许 verdict 为 `confirmed` / `false_positive` / `tool_bug` / `needs_human`。Reeve 分身保持 read-only，不替 owner 修复、不修改宪法、不处理跨 owner 治理处置。

### 时间约束

- P0：2 周（含 Reeve 一周 spike）
- P1：spike 通过 + Amanda 配方回路完成后 2 周
- P2：由 `observability-for-reeve` feature 驱动

### 资源约束

- Co-owners：Ken（harness 实现）+ Pulse（Relay cron + 生产观测）
- Amanda 回路：design.md 阶段必开 handoff（per-action autonomy 配方 + 通用模板方向）
- 预算：P0 spike CC 配额 ≤ $10 / 周；超出触发 Ken review 收紧频率或降档

### 6 阻塞点收敛状态

| # | 阻塞点 | 本 requirements 处置 | 留给 design.md |
|---|-------|-------------------|-------------|
| B-R1 | Session 生命周期 vs cron | FR-P0-006 选 one-shot spawn，不复用 pool | pool 复用留 v1.0+ 讨论 |
| B-R2 | 频率 × 成本 | FR-P0-008 + AC-P0-012 spike 预算硬约束；FR-P1-001 分层频率方向 | 各 Agent 频率数值 |
| B-R3 | 活动 session 干扰 | FR-P0-007 锁协议（任意 N-session 对等互斥）| 锁超时 / 嵌套 / 死锁检测 |
| B-R4 | 通知通道 | FR-P0-005 分即时 push + 日粒度 aggregate；P1 扩三档分级 | 通道优先级 / fallback |
| B-R5 | Agent autonomy 尺度 | **核心突破**：三档 autonomy + per-action 声明式 policy（故事 2-4 + FR-P0-002/003/004）| 各档位 per-action 通用模板（**Amanda 配方回路**） |
| B-R6 | 三方职责分工 | §0 定位表给分工；FR-P0-001/003/006 给接缝 | 事件 schema 版本化协议 |

### 前置依赖

- Relay `agent-scheduler` ✓ 已生产
- harness CLI ✓ 已生产（v0.1.0，445 tests）
- Amanda 配方层给通用模板 ⏳ design.md 起草前需开 handoff
- wi_722_v2 / L1 session 启动扫 ⏳ 并行独立推进，不阻塞本 feature

---

## 6. 排除范围（Out of Scope）

- ❌ **L1 session 启动扫**：由 `features/agent-workspace-harness-integration/` 覆盖（owner Amanda）
- ❌ **L3 阈值 push 的滚动窗口计算与可视化**：升级档即时 push 在本 feature，但阈值算法（S3 +50%、reject 30%、邮递员 3+）与 Reeve 可视化属 `features/observability-for-reeve/`
- ❌ **数据周报生成**（proposal"自动数据周报"）：属 observability-for-reeve，消费本 feature 的事件
- ❌ **邮递员自报 hook**：需 /wrap hook，跨 feature
- ❌ **宪法修改**：autonomy 语义入 `escalation-vocabulary.md` / `governance.md` 是 Amanda owner 的独立行动（proposal 附录 B 备注 B）
- ❌ **cron 以外的触发机制**：不实现 webhook / push-based triggers / 用户主动调用
- ❌ **修改 Relay `scheduler.ts` 源码**：仅新增 cron-jobs.json 条目
- ❌ **云端 Relay 的 heartbeat-runtime**：P0 仅本机（云端按需后续）
- ❌ **Policy 动态注入 / Web UI 管理**：Policy 走 git，不做热更；所有观察入口通过 CLI + 日志文件
- ❌ **Tool allow-list 以外的 runtime autonomy 护栏**（如行为审计阻断、运行时 ACL）：由 Reeve 异步审计 + CC 原生 tool 机制承担，本 feature 不重复造

---

## 下一步

1. **本 requirements 提交评审**：Ken → 大鸟 sign-off 方向（不需 ssot-judge，proposal 已 sign-off 过）
2. **开 handoff → Amanda**（design.md 起草前置）：B-R5 per-action autonomy 配方协作 + `escalation-vocabulary.md` 枚举的方向输入
3. **design.md**：收敛 ScanReport / Policy / 五类事件的精确 schema；锁协议细节；dispatcher 调 CC 的接入点（直接调 vs 借 scheduler）；autonomous session 的 prompt 模板
4. **tasks.md**：Reeve spike 流程 — policy 起草 → scan CLI → dispatcher → escalation → Reeve cron 接入 → 一周实跑 → spike-report.md
