---
name: proposal-reeve-first-customer
version: 0.4.1
created: 2026-04-18T12:00:00+0800
modified: 2026-04-18T17:00:00+0800
status: ready-for-signoff
author: Ken
reviewers:
  - Reeve (round 1 complete, accept-with-modifications @ 2026-04-18)
  - Amanda (R-1 系统层 review complete, accept-with-modifications @ 2026-04-18)
  - Reeve (round 2 complete, accept @ 2026-04-18)
  - Amanda (v0.4 notification review complete, accept + 2 非阻塞备注 @ 2026-04-18)
  - Reeve (v0.4 notification review complete, accept + 2 观察 @ 2026-04-18)
approver: 大鸟 (sign-off pending)
tags: [positioning, strategy, observability, heartbeat-runtime]
changelog:
  - v0.4.1 (2026-04-18T17:00): Reeve + Amanda 完成 v0.4 notification review，全 accept 无实质异议。新增 §附录 B 收纳 4 条非阻塞补充（Amanda 备注 A/B + Reeve 观察 O-A/O-B），作 feature spec 阶段输入。status 升 ready-for-signoff。
  - v0.4.0 (2026-04-18T16:45): 用户识别根因——proposal 所有"自动"承诺（数据周报 / 阈值 Push / 邮递员自报 / 自动自拾）全部隐含依赖 HEARTBEAT runtime，但 HEARTBEAT 当前只是配置文件非 daemon。sign-off 不应在此层模糊时推进。新增 §HEARTBEAT Runtime 实现路径 章节：定位 + 层次 + 6 阻塞点 + feature 规划（实现细节延后到独立 feature spec，本章节只保证 sign-off 含实现路径而非空头）。
  - v0.3.0 (2026-04-18T16:10): 基于 Amanda R-1 review 整合 6 修订 M1-M6（分层落地：harness 不进宪法 + 6 枚举不入 §三 + 新建 escalation-vocabulary.md）
  - v0.2.0 (2026-04-18T15:40): 基于 Reeve round-1 review 整合 5 修订 + R-1 章节
  - v0.1.0 (2026-04-18T12:00): 初稿
---

# Proposal: Reeve 作为 harness 第一客户 — 定位升级

> **状态**：v0.4 新增 HEARTBEAT Runtime 实现路径章节（闭合 proposal 所有"自动"承诺的实现依赖）。Reeve round-2 已 accept 定位+分层方案；v0.4 对 Reeve/Amanda 作 notification review（仅新增章节，不改前置共识）；无异议后大鸟 sign-off；sign-off 后并行启动 L1 四件套 + features/observability-for-reeve/ + features/heartbeat-runtime/ + 系统层 + Reeve/Amanda ws 迭代。

## 问题陈述

**现状定位**（L1 四件套 v0.2.0，2026-04-16 写入）：
> agent-harness 是 Agent 团队的**操作系统** …… 管理 Agent 团队的生命周期：Context / 工具 / 验证 / 恢复。

**大鸟 2026-04-18 澄清**：
> Reeve 不管基础设施，对他而言 Harness 是一项运营工具，以此理解"Agent 管理"是否真的有效——VMS 框架下的 System 1/2 是否解决了绝大部分事务，而不是事事都需要大鸟 System 3 介入，这也是我们开发 Harness 的初衷。所有 Agent 都是 Harness 的客户，但是 Reeve 是最大的那个，是第一客户。

**差距**：当前定位说清了"Harness 做什么"（团队 OS 的四大职能），没说清"Harness 为什么重要"的**量化目标**——System 3 介入频次下降。量化目标对应的**数据消费者 = Reeve**，但 L1 文档未识别 Reeve 的这一独特角色，也未规划支持她读温度的可观测性能力。

## 定位升级

### 核心新增

1. **产品价值函数明确**
   - **harness 的成功标准 = 大鸟（System 3）介入频次持续下降**
   - 其他所有工程特性都是手段，服务于这个核心指标
2. **客户分层**
   - **第一客户 Reeve**（唯一）：不参与 A2A 业务执行，以 harness 数据评估 VMS System 1/2 的运转有效性，产出"Agent 管理有效性报告"
   - **协作用户 Agent（含 Reeve 本人做审计时）**：发起/接收 handoff、处置 wi，是 harness 数据的**生产者**
   - 两类客户的需求场景本质不同——协作用户要"用得顺手"，Reeve 要"读得清楚"
3. **可观测性升级为一等公民**
   - 从 v0.2.0+ 起，"Reeve 能否读到温度"与"协作闭环能否跑"同等重要
   - 不提供聚合观测能力的 harness = 温度计不出数，产品价值无法被验证

### KPI 结构（v0.2 新增，source: Reeve round-1 review M2）

**单主 KPI + 次级看板**，禁综合加权（防 Goodhart）：

- **主 KPI**：**System 3 介入频次**（大鸟事件 / 周期）——单一、可读、不可伪造
- **次级看板**：6 + 2 = 8 个指标（见下表）作为主 KPI 的分解解释维度，**不做加权综合**
- **决策规则**：先改善主指标，再用次级看板解释改善路径；不允许用次级分数抵消主指标恶化

**S3 介入事件的边界硬定义**（解决 KPI 失真）：

| 类型 | 是否计入 | 理由 |
|------|---------|------|
| 纯 sign-off（流程必需的终审） | **不计** | 必经环节，无"减少"意义 |
| 纠偏介入（指出 Agent 跑偏） | **计** | S2 失效信号 |
| 调停介入（Agent 间冲突仲裁） | **计** | S1/S2 未消解 |
| 代为决策（Agent 不敢/不能决时） | **计** | S2 判断能力缺失 |

此边界通过 `escalation_type` 枚举字段代码化（见 §未决 U-2）。

### 报告节奏（v0.2 新增，source: Reeve round-1 review M3 + Q3；合并 U-4）

**双轨并行**：

| 轨 | 频率 | 产出者 | 形式 | 角色 |
|----|------|-------|------|------|
| **自动数据周报** | 每周 | harness（内置 `stats` / `report` 命令） | 结构化数据 dump，含 8 指标当周值 + 环比 | 素材，非结论 |
| **判断性周期报告** | 每 2-3 次审计或月度（取先到者） | Reeve（用 write-operating-report 骨架） | VSM 五步判断 + Recursion Shift 观察 + 修复建议 | 结论 |

**合并决策**：Reeve 的管理有效性报告 = 判断性周期报告，**不另造独立产出**。数据周报是素材，不是结论——避免读者困惑。

### 客户需求拆解（v0.2 更新，source: Reeve round-1 review M1）

**Reeve 作为第一客户需要什么**（对应 `.harness/reeve-space/IDENTITY.md` 使命："独立验证 Agent 管理的有效性"）：

| 指标 | 定义 | VSM 含义 | harness 当前能否给 |
|------|------|---------|--------------------|
| **System 3 介入频次**（**主 KPI**）| 触发 Upgrade Rules → 大鸟事件 / 周期（按上述边界） | S5 监督介入率（越低越好） | ✗ 事件日志无 `escalation_type` 标签 |
| **Handoff reject 率** | rejected / total | 发起侧质量 × 接收侧专业度 | ~ 需聚合命令（B-5 / stats 缺） |
| **反向 handoff 轮次** | 闭环平均来回次数 | S2 协作摩擦度 | ~ 需 log 聚合能力 |
| **邮递员动作数** | 带外通信事件数 | 自足原则违反程度 | ✗ 无机制识别，当前靠 UAT 附录 A 人工登记 |
| **集成覆盖率** | 三件套完整的 Agent / 总 Agent | S2 参与度 | ~ `ssot check` 部分覆盖，未聚合 |
| **wi 完成周期** | created → completed 时长分布 | S2 吞吐效率 | ~ 原始数据在 event，缺聚合 |
| **审计发现闭环率**（v0.2 新增）| 审计窗口内"完全闭环"发现数 / 总发现数 | **AEO（发展能力）改善转化度** | ✗ 需跨数据源聚合（审计报告 + 工单） |
| **跨 Agent 消费粘性**（v0.2 新增）| 某 Agent 产出被其他 Agent 反复消费密度 | **Recursion Shift 前置信号**——子系统是否需升格 | ✗ 需 `source` 字段严格结构化 |

**说明**：后 2 条不要求 harness 层直接输出，但 harness **必须为此预留数据钩子**（`wi.source` 结构化标签规范，支持粘性计算）。

**协作用户 Agent 需要什么**（本 UAT 已验证）：`assign / handoffs / ack / handoff / complete` + skill 指引 + ws 集成三件套。这部分 v0.1.0 已达基本可用。

## 影响面

### L1 四件套（`project-specs/`）

| 文件 | 改动摘要 |
|------|---------|
| `project-vision.md` | 增"产品价值函数 = System 3 介入减少"；"客户分层"段（Reeve 第一客户 + 协作用户）；定位从"团队 OS"扩为"团队 OS + 管理有效性度量基础设施" |
| `project-architecture.md` | 架构图增"可观测性层"（stats/report/aggregation），作为与协作层/验证层/治理层并列的第四层；说明数据流：event → aggregation → Reeve report |
| `project-roadmap.md` | v0.2.0 规划按 Reeve 优先级重排——可观测性特性上移到首要；B-8/B-1/B-4/B-5 各自排位需重议 |
| `project-standards.md` | 数据结构化标准新增"聚合查询友好性"要求（event 需带结构化标签，Upgrade Rules 触发事件需可识别）**+ 隐私边界条款**（v0.2 新增）：结构化元数据可采（时间戳/状态转移/wi_id/handoff_id/from/to/state/source），自由文本内容不采（input_summary / expected_output 原文、CC session thinking、用户敏感字段、业务细节）。**结构化友好 ≠ 隐私豁免** |

### README.md

增"客户分层"一节，主目录导航中把"Reeve 视角"单列。

### skill (`Bigbird/skills/harness/SKILL.md`)

增"Reeve 使用 harness 的独特场景"章节（不同于其他 Agent 的使用路径——查数、聚合、出报告而非协作）。

### features/

新建 `features/observability-for-reeve/` L2 三件套，承接可观测性能力设计。

### Reeve ws（由 Reeve 自己改）

- `TOOLS.md § harness` 从"协作工具"扩展到"协作工具 + 运营评估工具"，加入 stats/report 命令（待 v0.2.0 实现后填）
- `AGENTS.md` 或 `IDENTITY.md`：明确"harness 第一客户"身份和义务（周期性出管理有效性报告）
- `tasks/` 新增任务："harness 驱动的 Agent 管理有效性评估"

### 系统层（v0.3 重写，source: Amanda R-1 review M1-M6 分层落地）

**关键纠偏**（Amanda 事实核查）：`ssot/agent-constitution/` 整个目录**不存在** "harness" 字样。所以不是"改措辞"，是"要不要新增"——讨论焦点根本不同。

**结论**：harness 这个具体工具名**不进宪法**（避撞 `constitution.md 第六条` "沟通不属三环" + 抗工具替换原则）。改为**分层落地**：

| 层 | 动作 | 位置 |
|----|------|------|
| **宪法概念层** | 新增 **Communication Infrastructure** 抽象条目（承载 Communication 与 Handoff 的工程化实现；必须至少存在一套可审计的沟通基础设施，选型由运营层决定） | `ssot/agent-constitution/glossary.md`（**M1** 必做） |
| **宪法功能层** | "独立验证"条加前置条件："必须存在一套可审计的沟通基础设施（见 glossary.md: Communication Infrastructure），否则独立验证无客观证据源" | `ssot/agent-constitution/governance.md §五`（**M2** 应做）|
| **注册层** | harness 的**具体定位**（"团队 OS + 管理有效性度量基础设施"）写此处，不进宪法 | `ssot/agent-roster.md` 或 `ssot/registry.yaml` 或 harness 自己的 `project-vision.md`（**M3** 必做）|
| **词汇层**（新 SSOT 文件）| 新建 `escalation-vocabulary.md`：共通集合 3 条 + 领域扩展模板（见 U-2 分层）| `ssot/agent-constitution/escalation-vocabulary.md`（**M5** 必做）|
| **字段层** | `governance.md §三` 8 字段本体**不动**（§三 规定字段名不规定字段值）；仅加对 `escalation-vocabulary.md` 的交叉引用 | `governance.md §三` + `§九`（**M4 必做 + M6 应做**）|
| **工具层** | harness wi/handoff schema `escalation_type` 字段 enum 引用 `escalation-vocabulary.md` | `agent-harness/project-specs/project-standards.md` |
| **运营层** | 各 Agent 在自己 Upgrade Rules 里按需引用共通集合 + 领域扩展 | 各 Agent workspace |

**其他系统层动作**：
- Ken MEMORY.md 记录此定位升级决策
- UAT 报告追加设计前提

## R-1: Amanda 协作回路（v0.3 更新：已完成 + 结果）

### 背景

v0.1 proposal 的 §影响面 → 系统层触及 **Amanda 的 SSOT owner 边界**，原回路缺 Amanda。Reeve round-1 识别此结构风险并要求 Ken 在 sign-off 前新开 handoff → Amanda。

### 执行结果 ✅

**已完成**：
- Ken assign `wi_20260418_520 / ho_20260418_459` → Amanda（2026-04-18T15:43）
- Amanda review 完成（2026-04-18T15:47）：**accept-with-modifications**
- 产出：`Doc/amanda-space/memo/review-proposal-v0.2-2026-04-18.md`（298 行）
- 反向 handoff `ho_20260418_867` → Ken accept（2026-04-18T16:00）

### Amanda review 结论要点

**方向 accept，落法需修订**——原措辞撞宪法红线：

1. **影响点 #1**（harness 写进宪法）：**事实纠偏** —— grep `ssot/agent-constitution/` 全目录"harness"零匹配，"如有"前提不成立，是"新增"不是"升级"。拒绝措辞：撞 `constitution.md 第六条`（沟通不属三环）+ `glossary.md` Communication 定位 + 抗工具替换原则。**替代 M1/M2/M3**：分层落地（glossary 加抽象概念 + governance §五 加前置 + registry/roster 写具体定位）
2. **影响点 #2**（6 枚举入 §三）：**分类筛选** —— 6 枚举中 3 条跨 Agent 普适（`cross_agent_boundary` / `methodology_major_revision` / `disagreement_without_evidence`），3 条审计场景专有（`audit_result_fail` / `no_consumption_3x` / `self_compliance_debt_2sessions`）。把审计者视角泛化为所有 Agent 默认是滥用普适性。**替代 M4/M5/M6**：§三 不动 + 新建 `escalation-vocabulary.md` 分层 + 交叉引用

### 6 修订 M1-M6 落入 §影响面 → 系统层

见上表。Amanda 自认"无歧义"→ 不需二轮 Amanda review。**但 Ken 判断**：Amanda 的 M4/M5/M6 实质改动了 Reeve round-1 的 M5 方案（从"统一 6 枚举入 §三"变为"3+3 分层独立文件"）——应走 Reeve round-2 确认。

### P0 自拾能力 ⚠️ 未真正关闭

Amanda review 如实披露：**本次 R-1 review 的自拾未发生，由用户 nudge 触发手动查**。差距：HEARTBEAT 扫描不自动 + session 准备工作未列查 pending。Amanda 自提修复（加 `Doc/amanda-space/CLAUDE.md §准备工作` 查 pending 条），**另起 wi 追踪**（见 §下一步）。

UAT 报告 **附录 A 第 1 行"P0 完成后永久关闭"的条件尚未真正达成**——P0 加了 HEARTBEAT scan 步骤，但 session 启动不自动触发 HEARTBEAT。**P0 修复不完整**，需追加 session 启动流程改造。

## 元 Dogfooding 更新（v0.1 → v0.2 → v0.3）

本 proposal 的整个修订过程完全在 harness 内承载：

- **round-1 Reeve**（✓ 完成）：Ken assign → Reeve 自拾 ✓ → review（5 修订 + R-1）→ 反向 handoff → Ken accept
- **R-1 Amanda**（✓ 完成）：Ken 新 assign → Amanda **自拾失败**（用户 nudge）→ review（6 修订 M1-M6）→ 反向 handoff → Ken accept
- **round-2 Reeve**（⏳ pending）：Ken 反向 handoff 回 Reeve → Reeve 确认分层修订符合 round-1 意图 → 反向 handoff 回 Ken
- **sign-off**（⏳ pending）：大鸟终审

### 本 proposal 流程产出的三次接收方纠错（UAT 价值累积）

| # | 发起 → 接收 | 类型 | 修订深度 |
|---|------------|------|---------|
| 1 | Ken → Amanda (wi_722) | **实质层** | 整体 reject：过早抽象 + 时序倒置 + 跨 owner 边界 |
| 2 | Ken → Amanda (wi_845) | **形式层** | accept-with-偏离：criteria 列名无意偏离 Reeve 真实结构 |
| 3 | Ken → Amanda (wi_520 R-1) | **红线层** | accept-with-modifications：撞 constitution.md 第六条（沟通不属三环）+ §三 字段名/字段值混淆 |

**第 3 次最深**——Amanda 的修订触及宪法红线检测（而非仅形式或判断），证明 harness 可承载"具宪法防护作用的协作博弈"。若没有 R-1 回路补上 Amanda，proposal 会带着宪法错误进 sign-off。

### 零带外通信的例外（如实登记）

- **Amanda 自拾失败 1 次**：本轮 R-1 review 靠用户 nudge 触发（UAT 附录 A 第 1 行未真正关闭）
- 已规划修复（另起 wi），修复 + 下次实证后方能永久关闭

此流程本身证明 harness 能承载"战略级多轮多方协作 + 红线级纠错"，不只是工程派发或审计闭环。

---

## 附录 B: Feature Spec 阶段输入备注（v0.4.1 新增）

> Reeve + Amanda 在 v0.4 notification review 中均 accept 无实质异议，**本 proposal sign-off 不阻塞于以下补充**。备注/观察在相关 feature spec 起草时消化即可。

### Amanda 非阻塞备注（来自 `ho_20260418_737`）

**备注 A — wi_722_v2 owner 归属**

`features/agent-workspace-harness-integration/` (wi_722_v2) owner 应明确为 **Amanda**（agent-workspace-builder 是 Amanda owner 范围，Ken 是 harness 侧协作者）。Ken 原 §下一步 标为 Ken+Pulse 共 owner 为误。`wi_906`（CLAUDE.md §准备工作 加 pending handoff 扫描）正是此模板化要 bake 进去的内容——实际 `wi_906 + wi_722_v2` **可能合并或 wi_906 作为后者的 L1 先行实证**。

**落地路径**：`features/agent-workspace-harness-integration/` 起草时 Ken 开 handoff → Amanda 正式交接 owner，含 wi_906 处置决策（合并 vs 独立）。

**备注 B — B-R5 autonomy 尺度触及配方层**

B-R5（cron 自动处理 = Agent 跨 VSM 层级自主决策？）触及 **decision rights + Upgrade Rules 配方层**——超出 Ken 纯工程决策范围，需 Amanda 进回路：
- 就"cron 上下文下哪些操作类型可自主"给**配方级建议**
- 判断是否需在 `escalation-vocabulary.md` 新增 autonomy 相关枚举（例：`autonomous_decision_required` / `out_of_autonomy_scope`）

**落地路径**：`features/heartbeat-runtime/` design.md 阶段 Ken 开 Amanda handoff 协作。

### Reeve 观察（来自 `ho_20260418_855`）

**观察 O-A — B-R5 Reeve 专用 autonomy 策略**

B-R5 在 Reeve（审计场景）下建议分层：**看 / 通知 可自主**（cron 周期扫发现 + 轻推送通知）；**判 / 发 需 session**（审计判断、assign 发派需用户在场的正式 session）。

**落地路径**：`features/heartbeat-runtime/` design.md 中设计 autonomy 权限枚举时，先用 Reeve 场景作样板落地，再扩展到其他 Agent（Amanda 可能有不同切分）。

**观察 O-B — B-R3 并发范围扩展**

B-R3（活动 session 干扰）不应只覆盖"cron vs 人工 session"，应扩至**任意多 session 共存**——本 round-2 session 本身就是**人工 + 人工并发**的实证（Ken 在和用户对话 + Reeve 也在独立 session 工作）。

**落地路径**：`features/heartbeat-runtime/` requirements.md 中 B-R3 的范围定义扩展为"任意 N-session 并发"，不限 cron 触发类。

### 4 条补充的集中落地规划

| 备注/观察 | 目标 feature spec | 阶段 | Ken 动作 |
|----------|-----------------|------|---------|
| Amanda 备注 A | `features/agent-workspace-harness-integration/` | 起草开篇 | 开 handoff → Amanda 正式交 owner + wi_906 处置决策 |
| Amanda 备注 B | `features/heartbeat-runtime/` | design.md | 开 handoff → Amanda 协作配方级 autonomy |
| Reeve 观察 O-A | `features/heartbeat-runtime/` | design.md | 以 Reeve 场景作样板，扩展其他 Agent |
| Reeve 观察 O-B | `features/heartbeat-runtime/` | requirements.md | B-R3 范围扩至任意多 session 并发 |

Ken 将在 `ken-space/MEMORY.md` 同步本附录作"feature spec 阶段待办索引"，避免遗漏。

## 给 Reeve 的 review questions

1. 上表"指标"清单是否反映你理解的"Agent 管理有效性"？有无遗漏？
2. "System 3 介入频次" 作为核心 KPI 是否合理？还是应该用综合指标（多维度加权）？
3. 周期性报告的节奏（周报 / 月报）你倾向哪种？
4. 这个定位升级是否扩张了你的使命边界？（大鸟认定不扩，但你作为第一当事人可表异议）
5. 除了读数据，你是否需要 harness 主动 push 某些告警（例：某 Agent 邮递员动作频次超阈值）？还是仅 pull 模式够了？
6. 协作用户视角下，哪些指标不应被收集或聚合（隐私 / 可能有副作用）？

## HEARTBEAT Runtime 实现路径（v0.4 新增 — sign-off 前置）

### 问题（大鸟 2026-04-18 识别）

proposal 里 "自动数据周报 / 阈值 Push / 邮递员自报 / 自动自拾" 全部承诺**隐含依赖 HEARTBEAT runtime**——但当前 HEARTBEAT 只是 **Agent ws 下的配置文件**（定义"应该做什么"），**不是 running daemon**（不自动跑）。没有 runtime，所有"自动"承诺都是空头。**sign-off 不应在此层模糊时推进。**

### 三层触发机制

| 层 | 触发时机 | 覆盖场景 | 现状 |
|----|---------|---------|------|
| **L1: session 启动扫** | 用户手动开 session | 用户在场 | Amanda ✓ (wi_906)；Reeve 自起修复中；其他 Agent 未覆盖（依赖 wi_722 v2） |
| **L2: HEARTBEAT 定时触发** | 系统后台 cron | 用户不在场、纯后台派发 | **✗ 未启用**（Relay `agent-scheduler` 有能力，未接入） |
| **L3: Push 告警** | 阈值事件主动推送 | 紧急信号（S3 频次 / reject 率 / 邮递员动作）| ✗ 未设计（依赖 L2） |

**L1 只覆盖"有用户"场景**——无人开 session = pending 永远躺着。**L2 是"自动自拾"本义**，也是 proposal "第一客户可读温度"承诺的物理前提。

### 基础设施现状

- **Relay `agent-scheduler`**：已完成 2026-04-12~13，3 jobs 生产运行（早报×3 + 血压提醒 disabled + 其他）。**有 cron 能力但未接 HEARTBEAT。**
- **harness CLI**：可在 CC session 内被调用（`harness handoffs --to <agent> --state pending`）
- **CC session**：非 daemon，需要 spawn / 通过 Relay pool 复用

### 架构接缝（初步方向，非最终设计）

```
Relay agent-scheduler（cron）
  → (per-Agent 定时触发)
  → spawn CC session (或复用 Relay pool) 跑初筛
     │
     ├── harness handoffs --to <agent> --state pending
     │
     ├── 有命中 → 进入正式 HEARTBEAT 流程（Agent 按 HEARTBEAT.md 走）
     │            → 处置 pending（ack / 反向 handoff / 回复）
     │            → 产出 session wrap
     │
     └── 无命中 → 静默退出 + 轻量 event log
```

### 6 个阻塞点（feature spec 阶段必解，本章只显式登记）

| # | 阻塞点 | 可能方向（非决定） |
|---|-------|------------------|
| B-R1 | **Session 生命周期 vs cron**：每次 cron spawn 新 session 还是复用 Relay pool？ | Pool 复用降成本，但需解决 context 隔离 / 清状态 |
| B-R2 | **频率 × 成本**：N Agent × T 分钟 = CC session 消耗 | 按 Agent 分类设频率（高频 Reeve / 中频 Amanda / 低频 Aki）？ |
| B-R3 | **活动 session 干扰**：用户对话中 cron 触发并发 session | 检测活跃性 + 跳过 / 串行队列 |
| B-R4 | **通知通道**：扫到 pending 怎么告诉用户 | TG bot / 静默处理（Agent 自主）/ spawn 阻塞 session / 分级（紧急 push 常规静默） |
| B-R5 | **Agent autonomy 尺度**：cron 自动处理 = Agent 跨 VSM 层级自主决策？ | 区分"看/处置"：扫归扫，处置须用户确认 / 授权某些操作类型可自主 |
| B-R6 | **职责分工接缝**：Relay cron / harness 扫描逻辑 / CC session 三方接缝 | Relay 只做触发 + 成本核算；harness 封装扫描 + 事件；CC 承载决策 |

### 与 proposal v0.3 各承诺的依赖映射

| proposal 承诺 | 依赖层 | 没有该层就是 |
|--------------|-------|-----------|
| 自动数据周报（stats/report 定时产出） | L2 HEARTBEAT 定时触发 | 得 Reeve 手动跑命令 |
| 阈值 Push（S3 +50% / reject 30% / 邮递员 3+）| L2 + L3 | 得人肉盯看板 |
| 邮递员自报（/wrap hook 声明带外 N） | L2 同层（/wrap 定时或 session 结束 hook）| 得人工登记附录 A |
| 自动自拾（Agent HEARTBEAT 自动扫 pending）| **L2**（L1 只在用户开 session 时生效）| 得用户口头告知（本 UAT 现状）|
| 集成覆盖率扫描（`ssot check` 定时）| L2 | 得 Ken 手动全量扫 |

**结论**：**L2 是 proposal 所有"自动"承诺的公共前置**。

### 下阶段 feature 规划

**feature**: `Bigbird/Lab/BigbirdEnvironment/agent-harness/features/heartbeat-runtime/`
**L2 三件套**: requirements / design / tasks
**Co-owners**: Ken (harness 侧) + Pulse (Relay 侧)
**依赖**: Relay `agent-scheduler`（已就绪） + harness CLI（已就绪） + wi_722 v2（Agent ws 集成模板化，减少重复 session 启动层工作）
**交付目标**: v0.2.0 关键路径
**前置任务**: 6 阻塞点逐条作设计决策 → 小步先 spike（例：一个 Agent 低频试跑验证接缝）→ 全面铺开

### 本章节的 sign-off 含义

大鸟 sign-off 本 proposal **不等于 sign-off HEARTBEAT Runtime 的完整设计**——那是后续 feature spec 的事。本章节 sign-off 的是**方向 + 阻塞点清单 + 推进路径**：

- ✓ 认同 L2 是 proposal 承诺的物理前置
- ✓ 认同 Relay `agent-scheduler` 是触发基础设施
- ✓ 认同 6 阻塞点需由 Ken + Pulse 在独立 feature spec 中解决
- ✓ 认同 v0.2.0 为交付目标
- ✗ 不 sign-off 具体 cron 频率 / session 复用策略 / 通知通道选型等实现细节

这样 proposal sign-off 不是空头，也不被实现细节卡住。

---

## 已决方案候选 + 剩余未决（v0.2 更新）

### U-1：可观测性实现模式 → **倾向内置命令（Reeve round-1 意向）**

- **主方案**：harness 内置 `stats / report` 命令（紧凑优先）。理由：分工清晰（Reeve 消费不造 skill）+ 复用性高（未来 Reeve-type 审计者可复用）+ 语义封装（指标定义在 harness 一处）
- **次优方案（过渡）**：若 v0.2.0 工程容量不够，先暴露结构化 event stream，Reeve 写最小聚合 script 作过渡。L1 project-vision 必须写明"内置命令是目标，stream 是过渡"
- **剩余未决**：features/observability-for-reeve/ L2 设计时定具体命令签名和输出 schema

### U-2：escalation_type 字段 → **分层方案**（v0.3 按 Amanda M4-M6 修订）

**核心修订**：Reeve round-1 提出的 6 枚举**不统一纳入 `governance.md §三`**（§三 规定字段名不规定字段值；把审计视角泛化为所有 Agent 默认 = 滥用普适性 + 治理噪音）。改为分层：

**共通集合**（所有 Agent 的 Upgrade Rules 应识别这三类）：

```yaml
cross_agent_boundary:         # 判断触及其他 Agent 的 owner 范围
methodology_major_revision:   # 方法论/配方需要大改（超出 patch 级）
disagreement_without_evidence: # 被审对象强烈异议且审计方无新证据
```

**审计者扩展**（Reeve 类 Agent 专有）：

```yaml
audit_result_fail:            # 审计结论达 fail
no_consumption_3x:            # 连续 3 次无消费（粘性失效）
self_compliance_debt_2sessions: # 审计者自身合规欠账 2 session 未闭环
```

**其他领域扩展**（配方者 Amanda 类 / 其他）：TBD，由各 Agent 下次 Upgrade Rules review 时落地

**落地位置**：

- **词汇表主体**：新建 `ssot/agent-constitution/escalation-vocabulary.md`（**M5**）
- **宪法字段层**：`governance.md §三` 8 字段**不扩不塞枚举**（**M4**）；仅在 §三 + §九 加交叉引用到 escalation-vocabulary（**M6**）
- **harness schema**：wi / handoff 新增 `escalation_type: enum + nullable` 字段，enum 源引 `escalation-vocabulary.md`

**字段使用格式**（Amanda review 建议）：Agent 的每条升级触发建议写成 `trigger: <enum>` + `to: <owner>` + `payload: <minimum info>` 三要素。

**剩余未决**：
- Reeve round-2 确认此分层修订是否符合其 round-1 "cross-agent 词汇统一层"意图
- 共通集合 3 条是否已充分（Amanda 判断够，Reeve 需复核）

### U-3：邮递员动作识别 → **HEARTBEAT 自报 + harness 一致性验证**（Reeve round-1 意向）

- 每次 /wrap，Agent 在 HEARTBEAT 输出显式声明"本 session 带外通信次数：N"
- harness 在 `ssot check` 时比对"声明的带外 N" vs "实际 ack 量 vs session 动作量"，严重失配标红
- **不走全局 hook**（侵入性强 + 维护复杂 + 跨 Agent 行为差异大）
- **反对纯人工附录 A 登记**：会 under-report
- **过渡**：Agent 未实现 HEARTBEAT 自报前，harness 提供启发式扫描（检测 Agent session 中"直接调用其他 Agent 而非通过 harness"迹象）作兜底

**剩余未决**：HEARTBEAT 自报字段的具体 schema + 启发式扫描的准确率基线

### U-4：报告频率与审计周期合并 → **合并**（Reeve round-1 Q3）

Reeve 审计报告本身即管理有效性报告，**不造两份文档**。见 §报告节奏双轨表。

### Push / Pull 混合模式（Reeve round-1 Q5 新增已决）

- **默认 Pull**：Reeve 周期性主动查询 harness stats/report
- **阈值 Push**（三类必要）：
  - S3 介入频次**周环比 +50%** → push 大鸟 + Reeve
  - 邮递员动作**单日超 3 次** → push 涉事 Agent 的 HEARTBEAT
  - Handoff reject 率**周值超 30%** → push Reeve
- 阈值为建议值，features/observability-for-reeve/ L2 设计时校准

## 下一步（v0.3 更新）

1. ~~Ken 通过 harness assign 本 proposal handoff 给 Reeve review~~ ✓ 已完成（ho_20260418_695）
2. ~~Reeve round-1 review~~ ✓ 已完成（accept-with-modifications，`.harness/reeve-space/memo/review-proposal-2026-04-18.md`）
3. ~~Ken 按 Reeve review 起草 v0.2~~ ✓ 已完成（commit `6ccc20f33`）
4. ~~Ken 新开 handoff → Amanda（R-1 修复）~~ ✓ 已完成（ho_20260418_459）
5. ~~Amanda R-1 review~~ ✓ 已完成（accept-with-modifications，`Doc/amanda-space/memo/review-proposal-v0.2-2026-04-18.md`）
6. ~~Ken 按 Amanda review 起草 v0.3~~ ✓ 已完成（commit `e77c80b2e`）
7. ~~Ken 反向 handoff 回 Reeve round-2~~ ✓ 已完成（ho_20260418_640）
8. ~~Reeve round-2 review~~ ✓ **accept**（commit `331c905e6`，`.harness/reeve-space/memo/review-proposal-round2-2026-04-18.md`）
9. ✅ **Ken 起草 v0.4 补 HEARTBEAT Runtime 章节**（本次提交，大鸟 2026-04-18 识别根因）
10. ⏳ **Ken notification review → Reeve + Amanda**：v0.4 新增章节是否有异议（不触及她们已 accept 的前置共识；轻量 review，无异议默认 accept）
11. ⏳ **大鸟 sign-off**（本章节 sign-off 含义见 §HEARTBEAT Runtime 实现路径 末段）
12. ⏳ **并行追踪**：Amanda session 启动流程修复（wi_906）已完成；Reeve 同类修复自起进行中
13. ⏳ **按通过后版本启动**：
    - L1 四件套迭代（`project-specs/` 四份）
    - `features/observability-for-reeve/` L2 三件套（含 stats/report/escalation/coverage 命令设计）
    - `features/heartbeat-runtime/` L2 三件套（Ken + Pulse 共 owner，解 6 阻塞点）
    - `features/agent-workspace-harness-integration/`（wi_722_v2 模板化，覆盖 L1 session 启动三件套 + HEARTBEAT scan 段）
    - 系统层：`glossary.md` 加 Communication Infrastructure + `governance.md §五` 加前置条件 + 新建 `escalation-vocabulary.md` + `registry.yaml` 或 `agent-roster.md` 加 harness 定位
    - Reeve ws 扩展（`TOOLS.md` / `IDENTITY.md` / `tasks/`）
    - skill `Bigbird/skills/harness/SKILL.md` 加 Reeve 专用章节

## 另追踪项（非本 wi 范围，独立处理）

### P0 自拾能力真正闭合

Amanda review 披露：P0 完成的 HEARTBEAT § harness 扫描**未在 session 启动时自动触发**，导致本轮 R-1 review 的自拾失败（需用户 nudge）。

Amanda 自提修复：`Doc/amanda-space/CLAUDE.md §准备工作` 加一条"查 pending handoff"。

**Ken 将另起 wi 派 Amanda 落地此修复**——附录 A 第 1 行的"永久关闭"条件要此修复 + 下一次派发 Amanda 成功自拾双实证才真正达成。

