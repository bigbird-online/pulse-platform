---
feature_name: "agent-scheduler"
version: "0.2.0"
created: "2026-04-12T19:00:00+0800"
modified: "2026-08-17T15:00:00+0800"
upstream_dependency: ["project-vision.md", "project-architecture.md"]
tags: [requirements, l2-spec, scheduler, cron, crud, self-service]
changelog:
  - "v0.2.0: node-placement 模式 runNode、显式 owner/delivery mapping 与受控写回"
  - "v0.1.1: 新增 run-level terminal receipt；业务内部 push 与 scheduler fallback 每个 run 最多一个用户可见终态"
  - "v0.1.0: 新增消费者自服务 CRUD——Bot 命令注册/查看/删除/触发 job，Job 归属模型"
  - "v0.0.1: 初始版本，统一覆盖早报迁移 + HEARTBEAT 兼容两类使用场景"
---

# agent-scheduler - 功能需求文档

> **2026-08-17 node-placement addendum**：placement 模式下 `owner` 是显式 owner ID，不等于 logical agent 或 bot ID。每个 job 必须带 `runNode`，只在 current node 注册；delivery bot 必须显式映射且与 run node 共置，禁止同名/首 bot fallback。为防 SSOT/source/local projection 分叉，直接 `/job_add`、`/job_del` 和 config CRUD 被拒绝，只允许受控 staging 双写流程。legacy 非 placement 模式维持本文既有自助 CRUD 语义。

> **功能概述**：为 Relay 增加定时 Agent 唤醒能力——在指定时间自动生成 CC 子进程、注入 prompt、在指定 workspace 下执行，并将结果投递到 Telegram 或静默结束。**消费者（各 Agent）通过 Bot 命令自行注册和管理定时任务，无需人工编辑配置文件。**
>
> **关联文档**：
> - L1 项目愿景：`project-specs/project-vision.md`
> - L1 项目架构：`project-specs/project-architecture.md`
> - L2 技术设计：`features/agent-scheduler/design.md`
> - L2 任务拆分：`features/agent-scheduler/tasks.md`

---

## 1️⃣ 用户故事（User Stories）

### 已完成（v0.0.1 交付）

**故事1：定时执行 Agent 任务** ✅
- **作为**：系统管理员（大鸟）
- **我希望**：按 cron 表达式在指定时间自动唤醒某个 Agent，让它执行特定 prompt
- **以便**：无需手动触发，早报生成、HEARTBEAT 巡检等任务自动完成
- **优先级**：P0 ✅

**故事2：投递结果到 Telegram** ✅
- **作为**：系统管理员
- **我希望**：Agent 执行完毕后，把结果推送到我的 Telegram
- **以便**：我能在手机上看到早报 EPUB、血压提醒等产出，无需主动查看
- **优先级**：P0 ✅

### 本次新增（v0.1.0）

**故事7：消费者自助注册定时任务**
- **作为**：Aki（或任何 Agent 消费者）
- **我希望**：通过 Telegram Bot 命令注册一个定时任务，指定 cron 表达式、prompt 和执行参数
- **以便**：我不依赖 Ken 或管理员帮我手编配置文件，能自主管理生产工具的调度
- **优先级**：P0

**故事8：查看自己的定时任务**
- **作为**：消费者
- **我希望**：通过 `/jobs` 命令查看属于我的所有定时任务及其状态
- **以便**：了解我的哪些任务在运行、下次何时执行、最近是否有失败
- **优先级**：P0

**故事9：删除定时任务**
- **作为**：消费者
- **我希望**：通过 `/job_del <id>` 删除自己注册的定时任务
- **以便**：清理不再需要的任务，保持任务列表整洁
- **优先级**：P0

**故事10：手动触发定时任务**
- **作为**：消费者
- **我希望**：通过 `/job_run <id>` 立即执行某个任务（不等 cron 时间）
- **以便**：调试新注册的任务或补跑今天遗漏的执行
- **优先级**：P0

**故事12：每次运行只有一个用户可见终态**
- **作为**：接收 cron 结果或失败告警的用户
- **我希望**：业务层已发送终态后，scheduler 不因同一运行稍后的 runtime 非零退出再发第二条告警
- **以便**：一次业务失败不会被误读成两次独立事故，同时未处理 crash 仍有兜底
- **优先级**：P0

**故事11：修改定时任务**
- **作为**：消费者
- **我希望**：通过 `/job_edit <id>` 修改任务的 cron 表达式、prompt 或其他参数
- **以便**：调整任务而不必删除后重建
- **优先级**：P1

### 待实现（v0.0.1 遗留）

**故事3：控制 Job 的启用/禁用**
- **作为**：系统管理员
- **我希望**：随时启用或禁用某个 job，无需重启 Relay
- **以便**：临时暂停某项任务（如出差期间暂停血压提醒）而不影响其他任务
- **优先级**：P1

**故事4：一次性 Job**
- **作为**：系统管理员
- **我希望**：创建只运行一次的 job（执行后自动删除）
- **以便**：设置非重复的延时任务或一次性提醒
- **优先级**：P1

**故事5：故障自动熔断**
- **作为**：系统管理员
- **我希望**：某个 job 连续失败超过阈值时自动禁用并通知我
- **以便**：避免持续失败刷日志，同时能及时发现问题
- **优先级**：P1

---

## 2️⃣ 功能需求（Functional Requirements）

### P0 功能（已完成，v0.0.1）

**FR-P0-001：从配置文件加载 Jobs** ✅
**FR-P0-002：Cron 表达式时区感知调度** ✅
**FR-P0-003：Isolated CC 进程执行** ✅
**FR-P0-004：Telegram 投递（announce 模式）** ✅
**FR-P0-005：静默执行（none 模式）** ✅

### P0 功能（本次新增，v0.1.0）

**FR-P0-006：Bot 命令 — 列出 Jobs（/jobs）**
- **描述**：消费者在任意 Agent Bot 中发送 `/jobs`，返回**该 Agent 名下**所有 job 的列表，包含 id、名称、cron 表达式（人性化描述）、enabled 状态、上次执行状态和时间
- **来源**：故事8
- **示例**：Aki 在 aki-bot 中发 `/jobs` → 显示 Aki 注册的 3 个任务；Ken 在 ken-bot 中发 `/jobs` → 显示 Ken 名下的任务（两者互不可见）

**FR-P0-007：Bot 命令 — 注册 Job（/job_add）**
- **描述**：消费者发送 `/job_add` 后，Bot 引导填写必要参数（cron 表达式、prompt、超时时间），创建一个属于当前 Agent 的新 job。创建成功后 job 立即注册到 cron 引擎，无需重启 Relay
- **来源**：故事7
- **参数**：
  - `cron`（必填）：5 字段 cron 表达式
  - `prompt`（必填）：Agent 执行指令
  - `name`（可选）：人类可读名称，默认从 prompt 前 20 字生成
  - `timeout`（可选）：秒，默认 300
  - `delivery`（可选）：`announce` 或 `none`，默认 `none`
- **示例**：Aki 发送 `/job_add 0 6 * * * | 执行每日新闻数据采集，运行 news_feed_snapshot.py ...` → 创建 job，归属 aki，workspace 自动绑定 aki 的工作区

**FR-P0-008：Bot 命令 — 删除 Job（/job_del）**
- **描述**：消费者发送 `/job_del <id>`，删除**自己名下**的指定 job。从 cron 引擎注销并从 JSON 中移除，即时生效
- **来源**：故事9
- **约束**：不能删除其他 Agent 的 job
- **示例**：Aki 发送 `/job_del briefing-data-collect` → 删除成功；Aki 发送 `/job_del ken-heartbeat` → 拒绝（不是你的 job）

**FR-P0-009：Bot 命令 — 立即执行 Job（/job_run）**
- **描述**：消费者发送 `/job_run <id>`，立即触发**自己名下**的指定 job 执行一次（不影响正常 cron 调度）
- **来源**：故事10
- **示例**：Aki 发送 `/job_run briefing-data-collect` → 立即开始执行，Bot 返回"已触发"

**FR-P0-010：Job 归属模型**
- **描述**：每个 job 关联一个 `owner`（agentId），标识创建者。通过 Bot 命令创建的 job 自动设 owner 为当前 Bot 的 agentId；现有 JSON 中无 owner 字段的 job 视为 `system`（全局可见，仅管理员可操作）
- **来源**：故事7、故事8、故事9
- **规则**：
  - `/jobs` 只返回 owner = 当前 agentId 或 owner = system 的 job
  - `/job_del` 和 `/job_edit` 只允许操作 owner = 当前 agentId 的 job
  - `/job_run` 允许操作 owner = 当前 agentId 或 owner = system 的 job

**FR-P0-011：Job 创建时即时生效**
- **描述**：通过 Bot 命令创建/删除 job 后，scheduler 立即更新内存中的 cron 注册和 JSON 持久化，不需要 SIGHUP 或重启 Relay
- **来源**：故事7
- **示例**：`/job_add` 创建 job → 立刻注册 cron → 到点即触发

**FR-P0-012：Run-level terminal receipt**
- **描述**：scheduler 为每次执行生成唯一 `runId`，通过 `RELAY_JOB_RUN_ID` 暴露给 batch 子进程；业务层 `/internal/push` 可用 `runId + terminal:true` 声明用户可见终态。内部 push 与 scheduler failure fallback 在真正投递前共享原子预留，同一 run 的并发与重试不得产生第二条真实发送。
- **兜底**：内部终态未发送或投递失败后 runtime crash，scheduler 仍发送一条；不同 run 分别发送，不得被旧的 per-job 30 分钟冷却吞掉。
- **关联**：故事12；`wi_20260816_539`

### P1 功能（增强功能）

**FR-P1-001：Bot 命令 — 编辑 Job（/job_edit）**
- **描述**：消费者发送 `/job_edit <id> <field>=<value>` 修改自己 job 的 cron、prompt、timeout、enabled 等字段。修改后即时更新 cron 注册
- **来源**：故事11
- **示例**：`/job_edit briefing-data-collect cron=30 6 * * *` → 修改采集时间为 06:30

**FR-P1-002：一次性 Job（deleteAfterRun）**
- **描述**：`deleteAfterRun: true` 的 job 执行一次后，从 `cron-jobs.json` 中移除并从 cron 引擎注销
- **来源**：故事4

**FR-P1-003：连续失败熔断**
- **描述**：job 连续失败次数达到 `maxConsecutiveErrors`（默认 5）时自动设 `enabled: false` 并写回 JSON，同时通过 Telegram 通知管理员
- **来源**：故事5

**FR-P1-004：Cron 表达式人性化显示**
- **描述**：`/jobs` 列表中将 cron 表达式转为人类可读描述（如 `0 6 * * *` → "每天 06:00"），同时显示下次执行时间
- **来源**：故事8

### P2 功能（锦上添花）

**FR-P2-001：跨 Agent Job 查看（管理员视角）**
- **描述**：管理员可查看所有 Agent 的 job（不限 owner），用于全局监控
- **来源**：运维需求

**FR-P2-002：Job 执行历史**
- **描述**：记录每个 job 最近 N 次执行的状态、耗时、输出摘要，供排查使用
- **来源**：运维需求

---

## 3️⃣ 验收标准（Acceptance Criteria）

### P0 功能验收标准（已完成，v0.0.1）

**AC-P0-001：调度精度** ✅
**AC-P0-002：人格注入正确性** ✅
**AC-P0-003：Telegram 投递时效** ✅
**AC-P0-004：超时保护** ✅
**AC-P0-005：Jobs 互不影响** ✅

### P0 功能验收标准（本次新增，v0.1.0）

**AC-P0-006：/jobs 列表正确性**
- **条件**：Agent A 有 2 个 job，Agent B 有 3 个 job，另有 1 个 system job
- **预期结果**：Agent A 发 `/jobs` 看到 3 条（自己 2 + system 1）；Agent B 看到 4 条（自己 3 + system 1）
- **验证方式**：Unit Test（mock 多 owner 数据）

**AC-P0-007：/job_add 创建并即时生效**
- **条件**：消费者发送合法的 `/job_add` 命令
- **预期结果**：job 在 ≤1s 内注册到 cron 引擎；cron-jobs.json 同步更新；`/jobs` 立即可见新 job
- **验证方式**：Integration Test（创建后检查 cron 注册 + JSON 持久化）

**AC-P0-008：/job_add 参数校验**
- **条件**：消费者发送非法 cron 表达式或缺少必填参数
- **预期结果**：返回明确错误提示（如"cron 表达式无效"），不创建 job
- **验证方式**：Unit Test（覆盖 ≥3 种非法输入）

**AC-P0-009：/job_del 归属隔离**
- **条件**：Agent A 尝试删除 Agent B 的 job
- **预期结果**：返回"该 job 不属于你"提示，job 不被删除
- **验证方式**：Unit Test

**AC-P0-010：/job_run 立即执行**
- **条件**：消费者对自己的 job 发送 `/job_run`
- **预期结果**：job 在 ≤3s 内开始执行（CC 进程 spawn），不影响下次 cron 调度
- **验证方式**：Integration Test（验证 CC 进程 spawn + cron 不受影响）

**AC-P0-011：Job owner 自动绑定**
- **条件**：通过 aki-bot 发送 `/job_add`
- **预期结果**：新 job 的 owner = "aki"，workspace = aki 的配置工作区
- **验证方式**：Unit Test

**AC-P0-012：单 run 单终态且保留兜底**
- **条件**：同一 run 内业务失败终态已成功发送，随后 runtime exit 1
- **预期结果**：scheduler 记录并压制同 run fallback，用户可见终态总数为 1；日志同时保留业务主失败与次生 runtime 失败的同一 `runId`
- **反例覆盖**：内部终态未发、内部投递失败、同 run 并发/重试、不同 run 连续失败
- **验证方式**：Registry + HTTP integration + scheduler integration tests；生产 Feishu canary 另行验收

### P1 功能验收标准

**AC-P1-001：/job_edit 修改即时生效**
- **条件**：修改 job 的 cron 表达式
- **预期结果**：旧 cron task 被销毁，新 cron task 按新表达式注册；JSON 同步更新
- **验证方式**：Unit Test

**AC-P1-002：熔断触发**
- **条件**：某 job 连续失败 5 次（`maxConsecutiveErrors` 默认值）
- **预期结果**：该 job 被自动设为 `enabled: false`，`cron-jobs.json` 已更新，TG 收到熔断通知
- **验证方式**：Unit Test（mock 5 次失败）

---

## 4️⃣ 非功能需求（Non-Functional Requirements）

### 性能需求
- **启动开销**：Relay 启动时加载并注册所有 jobs ≤1s（≤50 个 jobs 场景）
- **命令响应**：Bot 命令（/jobs、/job_add 等）响应时间 ≤2s
- **执行开销**：scheduler 模块的调度逻辑本身（不含 CC 执行时间）开销 ≤100ms/job
- **并发容量**：未配置全局上限时保留跨 workspace 并发；配置实例级上限 N 后，同时进入执行路径的 jobs 不超过 N，超限任务按 FIFO 排队且不丢弃

### 可靠性需求
- **Relay 重启不丢 job**：jobs 持久化在 JSON 文件，重启后自动重新注册
- **CC 进程崩溃不影响 Relay**：CC 子进程错误不能 crash Relay 主进程
- **排队可观测**：已认领但等待全局容量的 job 必须出现在 running job 列表中，并使 `safeToRestart=false`
- **队列清理**：执行成功、失败或 spawn 异常后必须释放全局容量与 workspace 队列；stop/reload 不得静默丢弃已认领队列
- **删除安全**：running 或全局 queued job 的删除请求必须明确拒绝，定义与 claim 保持不变，直到该次执行完成
- **Reload 状态连续**：reload 前已认领 job 完成时，`lastRunAt`、`lastStatus`、错误连续计数必须合并并持久化到 reload 后当前 job store，不得因对象替换丢 cursor 或触发重复 reconciliation
- **CRUD 原子性**：创建/删除操作要么完全成功（内存 + JSON 同步），要么完全失败（不留半成品）

### 可维护性需求
- **遵循 project-standards.md**：TypeScript strict，vitest 测试，核心模块覆盖率 ≥80%
- **单文件 ≤300 行**：超出则按职责拆分

### 安全需求
- **Token 隔离**：spawn CC 子进程时从环境变量中移除 `TELEGRAM_BOT_TOKEN`（防止 409 冲突），与 pool.ts 保持一致
- **归属隔离**：消费者只能操作自己 owner 的 job，不能越权操作其他 Agent 的 job

---

## 5️⃣ 约束条件（Constraints）

### 技术约束
- **技术栈**：遵循 `project-architecture.md`：Node.js ≥18，TypeScript 5.x strict
- **新增生产依赖**：本次不新增生产依赖（node-cron 已在 v0.0.1 引入）
- **CC 调用方式**：`claude -p "prompt" --output-format text`（one-shot 模式），不使用 stream-json session 模式
- **Bot 命令格式**：遵循 Telegram Bot 命令规范（`/command` 或 `/command args`），命令名只允许小写字母和下划线

### 部署约束
- **本机 Relay 优先**：首批部署本机（`~/Library/LaunchAgents/com.pulse.relay.plist`），云端 Relay 按需同步
- **Agent workspace 来源**：Bot 命令创建 job 时，workspace 从 relay.json 中当前 Agent 的配置读取，不允许消费者任意指定路径

### 向后兼容
- **现有 cron-jobs.json 兼容**：无 `owner` 字段的 job 视为 `system`，不影响现有功能
- **现有 Bot 命令不受影响**：新增命令不与已有的 /start、/stop、/status 等冲突

---

## 6️⃣ 排除范围（Out of Scope）

- ❌ 任务间依赖编排（如 A 完成后触发 B）—— 通过 `.done` 文件约定在 prompt 层处理
- ❌ 基于时间点（at）的一次性调度语法 —— 用 cron 表达式的具体 dom/month 字段替代
- ❌ 飞书投递 —— P2 按需扩展
- ❌ Web UI / API 管理界面 —— Bot 命令已足够
- ❌ 云端 Relay 的 agent-scheduler —— 首批仅本机
- ❌ 跨 Agent 操作（A 管理 B 的 job）—— P2 管理员视角覆盖
