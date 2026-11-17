# SPEC：Relay 调度与 jobs — cron / handoff watcher / job 存储

> version: `0.2.1`
> status: `reverse_engineered_from_code`
> authority: cron 调度、错过补偿、handoff 事件驱动链与 job 存储的**现行实现契约**
> owner: Pulse（wi_20260804_070 · KR2a）
> modified: `2026-11-10`
> 版本史：只看 git log

**取证基础**：2026-08-09 代码普查 + spec-anchor 机器验证全绿。
**结构事实**：cron 求值器有两套并存——真实触发走 node-cron，错过补偿走自研
解析器；两者在日/月字段语义上有已知分歧（自研是与语义，标准是或语义），
登记于 §5。

## 1. 调度与错过补偿

- [x] 飞书和显式配置 `localOnly` Agent 的节点都初始化调度器。首装读取
  `/internal/cc-status` 验证调度器可用且在途运行已清空。
  `@anchor symbol shouldStartScheduler`
  `@anchor test Relay/tests/index.test.ts :: "starts scheduler observability for Feishu and local-only deployments"`
- [x] 补偿解析链：别名表、五或六字段（五字段前补秒位）、逐分钟扫描取窗口内
  最晚命中槽（只补一次不补全部）
  `@anchor symbol cronExpressionMatches`
  `@anchor symbol cronFieldMatches`
  `@anchor symbol findLatestDueRun`
  `@anchor symbol zonedCronParts`
  `@anchor test Relay/tests/scheduler.test.ts :: "IT-001b: scheduler reconciliation catches a run missed by node-cron"`
- [x] 定义指纹防回放：canonical 序列化取哈希，定义变更即把激活地板设为当下
  **主动放弃历史补偿**；语义等价的字段重排不动地板；禁用期的槽不回放
  `@anchor symbol jobDefinitionHash`
  `@anchor symbol canonicalizeJson`
  `@anchor symbol activationFloorForDefinition`
  `@anchor test Relay/tests/scheduler.test.ts :: "IT-001c: reload does not backfill a slot before the changed job definition became active"`
  `@anchor test Relay/tests/scheduler.test.ts :: "IT-001d: semantically identical field reordering preserves the activation floor"`
  `@anchor test Relay/tests/scheduler.test.ts :: "IT-001e: re-enabling a job does not replay slots from its disabled gap"`
- [x] 并发防重四层：per-job 认领、per-workspace 串行（CC 会话锁竞争）、
  per-owner 去抖冷却、全局并发上限
  `@anchor symbol claimJob`
  `@anchor symbol acquireWorkspace`
  `@anchor test Relay/tests/scheduler.test.ts :: "IT-006: overlap protection — second trigger skips while first is running"`
  `@anchor test Relay/tests/scheduler.test.ts :: "queues second job behind first when sharing workspace"`
  `@anchor test Relay/tests/scheduler.test.ts :: "runs jobs concurrently when workspaces differ"`
- [x] per-job 认领按任务的 `maxConcurrent`（1–8 的整数，缺省 1）计数：同一任务至多这么多次运行同时
  认领，满额时 cron、补偿与手动触发都跳过（事件触发答 `running`），一次运行结束只释放它自己的认领。
  大于 1 的任务声明各次运行互不依赖、也不与同一 workspace 的其他任务争用（如 `<agent>-pr-review-*`，
  每次 gate 各领不同请求），所以不进 workspace 队列。错过补偿仍只在任务没有运行时补。观察状态
  `currentRun` 是仍在运行的最近一次开始的运行，全部结束才清空。每次运行结束都把结果并入当前任务对象的
  状态（reload 前开始的运行也是），按本次运行的定义解读，不用旧对象覆盖其他运行的结果。运行以 `runId`
  标识：未了结的失败只由在它结束之后才开始的运行了结，同槽同定义的这种运行取代它，`convergent` 任务的
  这种成功了结全部此类失败；并行的运行（含同一分钟、槽位相同的事件运行）互不了结。不合法的值读入时
  跳过该任务。
  `@anchor const MAX_JOB_CONCURRENCY = 8`
  `@anchor test Relay/tests/scheduler.test.ts :: "runs up to maxConcurrent runs of one job beside the workspace queue and refuses the next"`
  `@anchor test Relay/tests/scheduler.test.ts :: "a run finishing on the object a reload replaced keeps a concurrent failure owed (convergent=%s)"`
  `@anchor test Relay/tests/scheduler.test.ts :: "a run of the same minute never settles a concurrent event run failure (convergent=%s)"`
  `@anchor test Relay/tests/scheduler.test.ts :: "skips a job whose maxConcurrent is not an integer within 1-8"`
- [x] `sharedWorkspace: true` 的任务只读自己的 workspace、改动写在别处（如 `hypnos`），不进 workspace
  队列：同一 workspace 的其他任务不排在它后面，它也不等它们；自身运行仍按 `maxConcurrent` 认领。
  非布尔值读入时跳过该任务。
  `@anchor test Relay/tests/scheduler.test.ts :: "a sharedWorkspace job never makes the workspace queue wait and still runs once at a time"`
  `@anchor test Relay/tests/scheduler.test.ts :: "skips a job whose sharedWorkspace is not a boolean"`
- [x] 订阅用量上限后补跑：计划槽（cron 或补偿触发）的运行因 Claude 订阅用量上限失败，且标准输出或标准错误里写明
  恢复时刻（如 `You've hit your session limit · resets 5:20pm (Asia/Taipei)`）时，状态 `quotaRetries` 按槽记一条
  （原计划槽、所属定义、恢复后 2 分钟、次数）；补偿循环到点按原计划槽再跑一次，同槽同定义的成功按既有规则了结
  这次失败；同一任务同时有正常漏跑槽时先补正常槽（它推进 lastRunAt），待补跑槽不依赖该游标，下一轮再跑。
  恢复时刻落在当前这一分钟内时按当天计。每槽最多 2 次，同时最多保留 8 个槽；不同槽互不覆盖。定义变更后旧定义的待补跑作废，早于本定义生效时间
  的槽不补，热加载后才结束的旧定义运行不新增；恢复时刻超过 48 小时、无法解析或手动触发都不补跑；该槽以其他结果
  结束即清除。
  `@anchor symbol subscriptionResetAt`
  `@anchor symbol nextQuotaRetries`
  `@anchor test Relay/tests/quota-retry.test.ts :: "keeps distinct slots apart and clears a slot on any other finish"`
  `@anchor test Relay/tests/quota-retry.test.ts :: "binds retries to the current definition"`
  `@anchor test Relay/tests/quota-retry.test.ts :: "never runs a slot under another definition or before the definition activated"`
  `@anchor test Relay/tests/scheduler.test.ts :: "reads a subscription reset announced only on stderr into the run result"`
  `@anchor test Relay/tests/scheduler.test.ts :: "a due quota retry never hides a missed regular slot of the same job"`
- [x] 运行时故障切换：定时任务按 `agent.runtimePreference` 依次尝试（未写则从平台运行时优先级起），其后自动
  补上未列出的批处理运行时（Codex 与 CC 互为兜底），CC 以退出码 0 回的合成额度提示同样计为失败，模型额度用尽（含 Claude session/weekly limit 与 Codex usage limit）、
  限流或运行时不可用时换下一个；任务自身失败不切换。`runtimeStrict: true` 只跑所列运行时（如异构审核对），
  非布尔值读入时跳过该任务。各运行时仍用该任务计算档位里对应的模型。没有成功时（含兜底以未归类错误结束），
  各次尝试报出的订阅恢复时刻按各自结束时刻解读、取最早者登记用量上限补跑；切换成功则不登记。
  `@anchor symbol failoverRuntimes`
  `@anchor test Relay/tests/runtime-failover.test.ts :: "appends every other runtime after a declared preference unless strict"`
  `@anchor test Relay/tests/runtime-failover.test.ts :: "a strict job stays on its runtime when its quota is exhausted"`
  `@anchor test Relay/tests/runtime-failover.test.ts :: "a job without a preference still fails over when codex quota is exhausted"`
  `@anchor test Relay/tests/runtime-failover.test.ts :: "a zero-exit cc session-limit turn from the real adapter moves the run to codex"`
  `@anchor test Relay/tests/scheduler.test.ts :: "a failed fallback keeps the earliest quota reset of every attempt (%s)"`
  `@anchor test Relay/tests/runtime-failover.test.ts :: "an unclassified fallback failure still carries the earlier quota notice"`
  `@anchor test Relay/tests/quota-retry.test.ts :: "reads each notice against the moment its attempt ended, not when the run finished"`
- [x] 耐久性门：运行状态落盘失败时抑制 startup/reload 补偿
  `@anchor symbol persistRunStateNow`

## 2. handoff watcher（跨系统缝：Relay ↔ agent-harness）

事件驱动接活主路径。十一层过滤后才叫醒 dispatcher；watcher 只管何时叫醒，
有无活由 harness scan 决定——漏放终态 handoff 代价是一次空跑，误拦活 handoff
才真丢活（非对称，闸方向必须偏放行）。

- [x] 触发链关键判据：只认 rename 事件、文件名形态 ho_ 前缀 yaml、顶层
  to_owner 可解析、**ack_state 非终态**（803 事件修复本体——认状态不认
  mtime，事故原文留在源码注释）
  `@anchor symbol startHandoffWatcher`
  `@anchor symbol parseHandoffToOwner`
  `@anchor symbol parseHandoffAckState`
  `@anchor symbol TERMINAL_ACK_STATES`
  `@anchor symbol HANDOFF_FILENAME_RE`
- [x] 节奏常量：去抖五秒、冷却六十秒、全局并发五（803 事件第二修复——13 个
  dispatcher 齐发压死单核机）
  `@anchor const DEFAULT_HANDOFF_DEBOUNCE_MS = 5_000`
  `@anchor const DEFAULT_HANDOFF_COOLDOWN_MS = 60_000`
  `@anchor const DEFAULT_HANDOFF_MAX_CONCURRENT = 5`
  `@anchor test Relay/tests/scheduler-handoff-watcher.test.ts :: "collapses 3 rapid handoff writes for the same agent into a single dispatcher call (debounce)"`
  `@anchor test Relay/tests/scheduler-handoff-watcher.test.ts :: "never runs more than maxConcurrent dispatchers at once"`
  `@anchor test Relay/tests/scheduler-handoff-watcher.test.ts :: "queued owners still run once slots free up"`
  `@anchor test Relay/tests/scheduler-handoff-watcher.test.ts :: "a throwing dispatcher releases its slot"`
  `@anchor test Relay/tests/scheduler-handoff-watcher.test.ts :: "retries a dispatcher result that was deferred by an agent lock"`
- [x] watcher 模块独占通知、队列、定时器与并发槽；scheduler 仅提供 jobs、配置及真实
  dispatcher。同一 owner 在途时的新通知合并为一次后续扫描。停止立即丢弃尚未启动的工作，
  迟到结果不得重建重试；已启动 dispatch 自然结束后释放槽，reload 的新队列仍受同一并发上限约束。
  `@anchor symbol createHandoffWatcher`
  `@anchor test Relay/tests/scheduler-handoff-lifecycle.test.ts :: "stopping discards queued work and a late completion cannot dispatch it"`
  `@anchor test Relay/tests/scheduler-handoff-lifecycle.test.ts :: "a deferred result after stop cannot recreate a retry timer"`
  `@anchor test Relay/tests/scheduler-handoff-lifecycle.test.ts :: "a later write for an in-flight owner waits for that owner to finish"`
  `@anchor test Relay/tests/scheduler-handoff-lifecycle.test.ts :: "reload retains occupied slots but only drains the current queue"`
  `@anchor test Relay/tests/scheduler-handoff-lifecycle.test.ts :: "a new generation retains notifications for an owner still running in the old generation"`
- [x] 与 harness 的隐式契约显式化：handoff 由对侧原子写（tmp+rename 故只发一个
  rename 事件、读不到半截文件）；to_owner/ack_state 是顶层零缩进标量，嵌套与
  块标量视为契约外判 undefined 放行
- [x] dispatch 实体 = harness scan → dispatch 两段子进程；scan 报 skipped 判
  deferred 按冷却重排
  `@anchor symbol executeHarnessDispatchJob`
  `@anchor test Relay/tests/scheduler.test.ts :: "marks a lock-skipped harness scan as deferred instead of clean success"`
  `@anchor test Relay/tests/scheduler.test.ts :: "pushes a TG alert after 3 consecutive harness_dispatch failures, with cooldown"`
  `@anchor test Relay/tests/scheduler.test.ts :: "marks job error when Harness escalation delivery fails"`

## 3. job 存储

- [x] 双文件切分：定义与运行状态分离；state 路径三级解析；合并优先级
  state 文件 > 定义内嵌 > 零值初始
  `@anchor symbol resolveCronJobsStatePath`
  `@anchor symbol mergeCronJobsState`
  `@anchor symbol stripRunState`
  `@anchor symbol persistConfig`
- [x] 读入校验逐条跳过不报错（缺 id/表达式/工作区/prompt/投递模式，顶层
  prompt 视为配置误写拒收）；direct job 出现 `ccModel` / `codexModel` / effort
  字面量同样拒收，只允许引用中央 `computeProfile`；v1 定义读入时自动补 owner 并**回写盘**
  `@anchor test Relay/tests/scheduler.test.ts :: "UT-002c: loadJobs rejects top-level prompt so config mistakes are not silent"`
  `@anchor test Relay/tests/scheduler.test.ts :: "UT-002d: loadJobs rejects cron-level model literals"`
- [x] CRUD：重复 id 拒、非法 cron 拒、只有 owner 本人能删（system 归属的
  job 任何 agent 都删不掉）
  `@anchor symbol createJobStore`
  `@anchor test Relay/tests/scheduler.test.ts :: "should reject system job removal"`

## 4. 关键默认值

超时兜底 3600 秒（类型注释写 300 是错的，登记 §5）；scan 段默认 60 秒；
失败告警阈值：harness_dispatch 连败 3 次冷却 2 小时，普通 job 连败 1 次
冷却 30 分钟。
普通 batch job 的 runtime 默认顺序读取 `data/runtime-priority.json`，当前为
`[codex]`；`relay-jobs` CLI 在 job 未声明 `runtimePreference` 时读同一顺序。
模型与 effort 只从 `Relay/config/compute-profiles.json` 解析；job 只写档位名，
未写时取 `platform_default`。schema v2 禁止 Codex `model` 字面量：`media` 只声明
CC `claude-sonnet-latest/medium` 与 Codex `codex-terra-latest/medium`，`platform_default`
只声明 `codex-sol-latest/xhigh`。`review` 是 PR 异构审核档位（CC `claude-opus-latest/high`，
Codex `codex-sol-latest/high`），内核用它的解析结果核对审核会话是否为当前旗舰；该档位只能写
latest alias，两侧 effort 必须是 high、xhigh 或 max，否则解析失败。`review` 的 CC 侧不用短别名，
而解析为 Relay 模型目录（受管路径 `models`，否则 `RELAY_MODEL_CATALOG_PATH`）中版本最高的
`claude-opus-*` 完整型号；目录未配置、不可读或没有该家族条目时解析失败。`review` 只接受
`claude-opus-latest` 与 `codex-sol-latest`；Codex 侧先取目录中版本最高的可见 Sol，再取它支持的、
不低于配置值的最低强度，最新 Sol 不支持 high 及以上时解析失败，不降级到旧 Sol。CC latest alias 交给 Claude Code 原生短别名；Codex latest
alias 从运行用户的 Codex `models_cache.json` 选择同 family、支持目标 effort 且
`visibility=list` 的最高数值版本。目录不可读、超过 7 天未刷新、alias 无匹配或 effort
不支持均 fail closed，不回退到 Sol；alias 与
解析后的具体 model 同时写运行日志。具体 model 是时点证据，不是 profile 契约。
Relay 外的消费者（如 `/wrap` 收口员）用只读入口 `pulse-relay compute-profile resolve <name>
--provider cc|codex --json` 按同一目录与常驻账户解析，输出 `model`、`model_alias`、
`reasoning_effort`、`source`；只解析所请求 provider，失败非零退出且不输出 JSON。
`relay-jobs add`／`del` 只写文件，不通知常驻 Relay；输出提示须经受管 `pulse-relay reload`
后才采用。两种模式都在该入口重读任务定义：非 node-placement 模式不停调度器，正在运行的任务
按旧定义跑完本轮且不会被重复启动；候选文件缺失、不可解析、含无效条目（id 非字符串、id 重复、时区非法等）或任一 cron 注册
预建失败时整次拒绝并保留原任务与注册；启动时同类条目跳过并告警，
只有合法的空 `jobs` 数组表示清空；node-placement 模式有任务在跑时整次重载被拒，稍后重试。
`codexYolo=true` 时仍下发危险 bypass 长参数。

带 `agent.gate` 的 job 先以 argv（无 shell）运行机械 gate：只有未超时、退出码 3
（可配置）且 stdout 精确匹配 `skipOutput` 才记为成功 no-op，直接返回且不解析 profile、
不启动 CC/Codex；退出 0 也必须有非空 stdout 才作为已筛选证据注入 prompt；超时、
空/错标记、证据损坏或其他退出码 fail closed，同样不启动 LLM。

`delivery.progressNotice: true` 的 job（用于 `*-collab-inbox`）在 gate 退出 0、输出为非空
`pulse.collab_inbox_gate/v1` 批次、即将启动 LLM 时，向人类会话发一条进度提示。首行写明来源：
一份合同为“📨 后台正在处理 <发起方> <WI> 协作合同。”，多份为“📨 后台正在处理 N 份协作合同，
来自 A、B。”；发起方用 Relay 配置中 Harness 主体（principal）对应 Agent 的 `name`，取不到时用
actor ID。`delivery.feishuOpenId` 收飞书 2.0 卡片，合同详情（短合同号 `op_` 加 9 位、发起方、
WI、kind、状态）在默认收起的折叠面板里；`delivery.to`（`channel` 不是 `feishu`）收 Telegram
HTML 可展开引用。发送不阻塞 LLM 启动。运行结束后原地改写同一条消息为完成、失败、超时或暂停及
耗时（飞书 patch，Telegram editMessageText）；无法改写时不补发任何消息，失败与超时由
`feishuOpenId` 的失败告警通知。未设字段、手动/CLI 关闭投递、gate 跳过、输出不是协作批次或
没有目标与客户端时不发。提示的任何发送失败只记警告，不改变运行结果。

带 `agent.verify` 的 job 在 agent 退出 0、`expectArtifact` 通过后，以 argv（无 shell）运行
运行后核验命令，默认超时 60 秒；环境变量传入 `RELAY_JOB_ID`、`RELAY_JOB_RUN_ID`、
`RELAY_JOB_STARTED_AT`（本轮开始，UTC ISO-8601）、`RELAY_JOB_DATE`（本轮开始时刻在
`schedule.tz` 的日期）、`RELAY_JOB_WORKSPACE` 与 `RELAY_JOB_RUNTIME`。退出 0 才维持成功；
非零退出、超时或无法启动都记本轮 error，错误信息取 stderr（为空时取 stdout）末尾 500 字符，
由 `delivery.feishuOpenId` 的失败告警按运行号推送。agent 失败或产物检查失败时不运行核验。
`command` 为空、`args` 不是字符串数组或 `timeout` 非正数的 job 读入时跳过。

`telegram_document` 与 `feishu_document` 的 `delivery.documentPath` 写绝对路径时按原样使用；写相对路径时
按 `agent.workspace` 解析，与 `agent.expectArtifact.path` 同样展开 `${DATE}`、`${MONTH}`、`${ISO_WEEK}`，
解析结果（含跟随符号链接后的真实路径）必须仍在工作区内，越界即本次投递失败、不发文件。
可选的 `delivery.captionPath` 是相对工作区的 UTF-8 文本文件路径（同样展开、同样不得越界），在文档发出后
作为一条文字消息发给同一收件人；文件在发送前读取，缺失、不是普通文件、为空、超过 4096 字符（Telegram 文字
上限）或不是合法 UTF-8 时不发文档，文字发送失败时文档虽已发出也记本轮投递失败，均走投递失败的
`errorMessage` 与 `delivery.feishuOpenId` 失败告警。`captionPath` 写成绝对路径、空串或用在其他投递模式，
以及 `documentPath` 为空串的 job 读入时跳过。静态 `delivery.caption` 行为不变。

`@anchor test Relay/tests/scheduler.test.ts :: "resolves a relative documentPath against agent.workspace with ${DATE} expansion"`
`@anchor test Relay/tests/scheduler.test.ts :: "rejects a relative documentPath that escapes agent.workspace"`
`@anchor test Relay/tests/scheduler.test.ts :: "rejects a relative documentPath whose symlink leaves agent.workspace"`
`@anchor test Relay/tests/scheduler.test.ts :: "sends the captionPath text to Feishu after the document"`
`@anchor test Relay/tests/scheduler.test.ts :: "sends the captionPath text to Telegram after the document"`
`@anchor test Relay/tests/scheduler.test.ts :: "fails the delivery and sends nothing when the captionPath file is missing"`
`@anchor test Relay/tests/scheduler.test.ts :: "fails the delivery and sends nothing when the captionPath escapes agent.workspace"`
`@anchor test Relay/tests/scheduler.test.ts :: "fails the delivery when the captionPath text cannot be sent after the document"`
`@anchor test Relay/tests/scheduler.test.ts :: "skips a job whose captionPath is absolute or set on a non-document mode"`

`@anchor const HARNESS_DISPATCH_ALERT_THRESHOLD = 3`
`@anchor const DEFAULT_MISSED_RUN_LOOKBACK_MS = 26 * 60 * 60 * 1000`
`@anchor symbol createBatchRuntimeOrchestrator`
`@anchor symbol resolvePulseRuntimePriority`
`@anchor symbol resolveComputeProfile`
`@anchor test Relay/tests/compute-profiles.test.ts :: "resolves latest aliases from runtime-owned model catalogs"`
`@anchor test Relay/tests/compute-profiles.test.ts :: "forbids Codex model literals in schema v2"`
`@anchor test Relay/tests/scheduler.test.ts :: "pre-LLM gate skip returns ok without spawning a runtime"`
`@anchor test Relay/tests/scheduler.test.ts :: "a timed-out gate cannot impersonate a successful skip with exit code 3"`
`@anchor test Relay/tests/scheduler.test.ts :: "positive gate resolves media profile before spawning CC"`
`@anchor symbol startProgressNotice`
`@anchor test Relay/tests/scheduler.test.ts :: "posts the background notice card before the run and patches it with the outcome"`
`@anchor test Relay/tests/scheduler.test.ts :: "a failed notice send never changes the job result"`
`@anchor test Relay/tests/collab-progress-notice.test.ts :: "posts an expandable Telegram message and edits it with the outcome"`
`@anchor symbol detectsFailedPostRunVerification`
`@anchor test Relay/tests/scheduler.test.ts :: "passes the run identity to the verify command and keeps ok on exit 0"`
`@anchor test Relay/tests/scheduler.test.ts :: "turns a non-zero verify exit into an error with its reason and alerts the owner"`
`@anchor test Relay/tests/scheduler.test.ts :: "does not run verify when the declared artifact is missing"`

## 4a. 原生会话证明

- [x] 调度器拉起的 Codex 以 `-c mcp_servers.*` 注册对话 Codex 的同一组 Host MCP，不依赖用户级
  `~/.codex/config.toml`：节点选定内核且有 Relay 数据目录时注册 `pulse`（`pulse_work_item`、`pulse_stats`）
  与 `pulse-request-purpose`（`declare_session_wi`），否则两者都不注册。`pulse` 的服务配置自带
  `PULSE_WORK_ITEM_CORE_HOST` 与 `PULSE_CORE_REQUEST_STATE_ROOT`，不带租户，内核从下条的原生会话证明认出这次运行。
  `@anchor symbol scheduledCodexCoreArguments`
  `@anchor test Relay/tests/scheduled-codex-host-mcp.test.ts :: "registers declare_session_wi and pulse_work_item without a user config.toml, before exec"`
  `@anchor test Relay/tests/scheduled-codex-host-mcp.test.ts :: "matches the conversation servers apart from the tenant and jobs key the conversation names"`
- [x] 调度器拉起 CC、Codex 与 cc-interactive 时，像对话会话一样在 `data/native-session-proofs/`
  写原生会话证明，键为调度器启动的进程（CC 本身、Codex npm 启动脚本或 PTY 桥）；会话号取
  CC 的 `--session-id` 与 Codex 标准错误头部的 `session id:`；记录 `entry = "scheduler"`、
  `tenant = "scheduler:<jobId>"`、`owner` 与运行号。写入失败只记警告，不改变本次运行；进程
  结束时只删除本 Relay 为同一会话写的记录。内核如何认这份证明见
  `agent-harness/project-specs/native-entry-binding.md` §3。
  `@anchor symbol observeScheduledSession`
  `@anchor symbol forgetNativeSession`
  `@anchor test Relay/tests/scheduler-native-session-proof.test.ts :: "records the Codex thread from its stderr header and removes the proof at exit"`
  `@anchor test Relay/tests/scheduler-native-session-proof.test.ts :: "keys a cc-interactive proof by the PTY bridge that launched CC"`
  `@anchor test Relay/tests/scheduler.test.ts :: "names the job, owner and run in the proof of the runtime it starts"`
- [x] 对话会话（cc、cc-sdk、codex、pi）的原生进程退出时，Relay 删除自己为该进程写的最新一份证明；
  记录已被另一写入者替换（Relay、会话号或进程出生戳不同）时保留。Relay 启动时、任何会话开始前清扫
  证明目录，只删明确无主的 `<pid>.json`：该 PID 已无进程或现进程出生戳与记录不符，或写记录的 Relay
  既不是本 Relay 也已不在运行。进程与写入 Relay 都还活着的记录可能属于同一数据目录上的另一 Relay，
  保留。单条读取或删除失败只记警告，不阻断启动。内核按出生戳忽略同号旧记录，见
  `agent-harness/project-specs/native-entry-binding.md` §3。
  `@anchor symbol forgetNativeProofOnExit`
  `@anchor symbol sweepNativeSessionProofs`
  `@anchor test Relay/tests/native-session-proof-cleanup.test.ts :: "removes a conversation proof when its process exits"`
  `@anchor test Relay/tests/native-session-proof-cleanup.test.ts :: "keeps a proof another writer replaced before the process exited"`
  `@anchor test Relay/tests/native-session-proof-cleanup.test.ts :: "sweeps only proofs with no live process and Relay"`
  `@anchor test Relay/tests/native-session-proof-cleanup.test.ts :: "logs an unreadable proof and keeps starting"`
- [x] 调度器拉起的 CC 与 cc-interactive 以 `--mcp-config` 注册对话会话的同一组 Host MCP：节点选定内核时注册
  `pulse`（`pulse_work_item`、`pulse_stats`），会话 WI MCP 解析成功时注册 `pulse-request-purpose`
  （`declare_session_wi`，与对话会话同一个解析结果）。对话的 CC 进程自带内核环境，桥接服务继承即可；调度运行
  的进程不带，由服务配置自带 `PULSE_WORK_ITEM_CORE_HOST` 与 `PULSE_CORE_REQUEST_STATE_ROOT`，不带租户，
  内核从上述证明认出这次运行。没有 Relay 数据目录时只缺 `pulse`。cc-interactive 的 `--bare` 不做 MCP
  自动发现，但仍启动 `--mcp-config` 指定的服务。写入边界照旧并入唯一的 `--settings`。
  `@anchor symbol ccHostMcp`
  `@anchor test Relay/tests/scheduled-cc-host-mcp.test.ts :: "the CC batch registers declare_session_wi and pulse_work_item, the core bridge carrying its own environment"`
  `@anchor test Relay/tests/scheduled-cc-host-mcp.test.ts :: "matches the conversation servers apart from the core environment the conversation process carries"`
  `@anchor test Relay/tests/scheduled-cc-host-mcp.test.ts :: "keeps the write boundary in the single --settings value beside the servers"`

## 4b. 自动任务的成本归属记录

- [x] job 可写顶层 `wi`（规范 `wi_YYYYMMDD_NNN`）声明其用量所属 WI；格式不符的 job 读入时跳过。
  调度运行得到原生会话号时，若设置了 `PULSE_COMPUTECOST_RECEIPT_DIR`，同时向该 Host 回执目录写一条
  `scheduled_session` 记录：会话范围引用 `collaborationRef('native_session', [runtime, 会话号])`、
  运行引用、本轮开始时间、job ID、job 名称（去控制字符、至多 120 字）与配置的 `wi`（未配置为 null）。
  不写原生会话号与 prompt；job ID 不合规、名称为空或写入失败只丢证据，不改变本次运行。
  ComputeCost 据此把该会话用量记为任务配置的 WI，或“自动任务：<名称>”，规则见
  [归属设计](../../specs/stats-attribution-identity.md#声明优先的归属补充)。
  `@anchor symbol captureScheduledSession`
  `@anchor test Relay/tests/scheduler-native-session-proof.test.ts :: "records the job of a Codex run for ComputeCost without its session ID"`
  `@anchor test Relay/tests/scheduler.test.ts :: "UT-002e: loadJobs keeps a canonical job WI and skips a malformed one"`

## 4c. 协作收件箱投递的入口记录

- [x] 协作收件箱经 `/internal/collab-deliver` 把合同投进人类会话（`deliverInternalTurn`）时，若设置了
  `PULSE_COMPUTECOST_RECEIPT_DIR`，投递正文末尾加一次性引用
  `[Host internal delivery reference: <64 位十六进制>. Relay delivery, not a human message.]`，
  并向 Host 回执目录写一条 `attribution`：目标为
  `collaborationRef('internal_delivery', [引用, collaborationRef('native_session', [runtime, 会话号])])`、
  `policy = "relay_internal_delivery.v1"`、`state = "unassigned"`、WI 与执行者均为 null。
  这条记录不指向任何用量，不授予 WI 或执行权；不写原生会话号与正文。未设置回执目录时正文不变。
  ComputeCost 只在同一原生会话中恰有一条输入带此引用、且记录不晚于该输入时，把这一请求的入口记为
  `automation`（显示“无”），规则见[归属设计](../../specs/stats-attribution-identity.md#cc-子代理与压缩后的入口继承)。
  `@anchor symbol internalDeliveryPrompt`
  `@anchor test Relay/tests/internal-delivery-capture.test.ts :: "binds a one-off reference in the prompt to the native session in the Host spool"`
- [x] 同一投递开始前，Relay 先向人类发一条提示，首行写明来源：接收方投递为“📨 收到 <发起方>
  <发起方 WI> 协作合同，开始处理。”（gate 未送 `offered_by` 时省略发起方）；发起方验收投递
  （`role: requester`）为“📨 <答复方> 已答复你在 <WI> 的协作合同（已完成/已退回/已拒绝），
  开始验收。”。Agent 名取法同进度提示。合同短号、kind、状态、截止时间（有则写）、处理所在的
  目标 WI 与是否只限主身折叠在下方：Telegram 为 HTML 可展开引用，飞书为默认收起的折叠面板卡片。
  Telegram HTML 发送失败时改发首行纯文本，飞书卡片发送失败时改发首行文本；渠道调用方未给结构化
  信息时发“📨 收到一份协作合同，开始处理。”。提示只给人看，投给 Agent 的正文不变。
  `@anchor symbol collabTurnNotice`
  `@anchor test Relay/tests/collab-delivery.test.ts :: "passes the human-visible notice beside an unchanged prompt"`
  `@anchor test Relay/tests/telegram.test.ts :: "names the sender and folds the contract details when a notice is given"`
  `@anchor test Relay/tests/feishu-channel.test.ts :: "falls back to the headline as text, and to the generic line without a notice"`

- [x] 同一合同的同一角色、同一状态只投递一次。已投递标记按“角色:合同号”记状态，存于 Relay 数据目录的
  `collab-delivery-ledger.json`（原子替换写入），Relay 重启后仍生效；gate 再问同一状态答 `duplicate`，
  状态变了（如发起方从 returned 变为 completed）才再投。gate 仍在问的合同每次刷新记录；7 天未再被问到
  的记录（合同已结清或不在本机持有）删除，总数超过 1000 条时删最久未问的。
  接收方合同处于 `accepted` 时不推送，答 `suppressed`（gate 视同已处理）；合同带截止时间且剩余不足
  24 小时（含已过期）时提醒一次，正文说明“已接受，临近截止”，要求推进并用 respond_collaboration
  complete 或 return。`pending` 正文仍请 Agent 向人类说明后由人类决定；发起方结清正文不变。
  内核收件箱仍把 accepted 合同列为欠着，供 `next` 与 Stats 看板使用，过滤只在 Relay 投递层。
  `@anchor symbol deliveryMark`
  `@anchor test Relay/tests/collab-delivery.test.ts :: "stays a duplicate after the old window and across a Relay restart"`
  `@anchor test Relay/tests/collab-delivery.test.ts :: "reminds an accepted recipient contract once when its deadline is near"`
  `@anchor test Relay/tests/collab-delivery.test.ts :: "forgets records no longer asked for and bounds the ledger"`
- [x] 持有会话正忙时不打断，也不留存这次请求：Relay 答 `deferred`，gate 不记已投递。Relay 记下“该 Agent
  在等这个会话”，会话出结果（或每 30 秒轮询）发现空闲后，按 4e 的事件运行规则立即再跑该 Agent 的收件箱
  任务；那次 gate 重新读内核收件箱，投的是此刻仍欠着的状态。忙的那一轮里已结清的合同、已被新一轮取代的
  PR 结果，因此不再迟到投递。等待超过 2 小时或本节点没有账本监视时留给定时槽；持有会话变了则新请求匹配
  不到会话，答 `not_hosted`。定时唤醒（`role: wake`）的正文是任务自己的 prompt，不读内核状态，不会过期：
  忙时仍答 `queued`，空闲后原样送出。
  `@anchor test Relay/tests/collab-delivery.test.ts :: "defers a busy conversation and asks the gate again once it is idle, sending nothing it read before"`
  `@anchor test Relay/tests/collab-delivery.test.ts :: "asks again for a pull request result only when the gate reads it again"`
  `@anchor test Relay/tests/collab-delivery.test.ts :: "leaves a conversation waited on past the limit to the schedule, and one that moved"`
  `@anchor test Relay/tests/collab-delivery.test.ts :: "queues a wake for a busy conversation and sends it once idle, without asking the gate again"`
  `@anchor test Scripts/tests/test_collab_inbox_gate.py :: test_a_deferred_delivery_records_nothing_and_is_asked_again_by_the_next_run`
- [x] PR 结果投递（`role: submitter`）：内核收件箱的 `pull_requests` 列出 Agent 登记的 PR 在近 24 小时内的结果，
  gate 把本机持有该 WI 的一项经同一接口交给 Relay，`contract_id` 填该轮的 candidate_id，`kind` 为
  `pull_request`，另带 `repository`、`number`、`head_sha`、`at`，以及可选的 `next_intent`、`agent_action`、
  `failed_checks`、`review_score`、`review_summary`。状态只收 `merged`、`changes_requested`、`checks_failed`、
  `merge_failed`、`merge_blocked`、`human_confirmation_required`。合并的正文为“[系统投递：PR 结果，不是人类消息]
  <仓库>#<编号> 已由内核合并（head <前 8 位>…）”，并请 Agent 声明该 WI 后继续后续步骤；其余状态写明原因、
  审核分与一行摘要、失败的检查和内核给的下一步，需人类确认时请 Agent 向人类说明。人类提示首行为
  “🔀 <仓库>#<编号> <结果>（<WI>）。”。去重与合同相同：每轮每个状态只投一次。
  `@anchor symbol pullRequestPrompt`
  `@anchor test Relay/tests/collab-delivery.test.ts :: "tells the session holding the WI that the kernel merged its pull request, once per round and status"`
- [x] 定时唤醒：任务带 `wake: {workItemId}` 时到点不起批处理会话。它的 gate 是 `collab-inbox-gate.py wake --actor
  <Agent> --wi <WI> --job <任务 id>`：读同一收件箱的 `held`（本 Agent 持有的 WI 与持有者），把该 WI 交给本机握着它的
  会话，经 `/internal/collab-deliver`（`role: wake`，`contract_id` 为 `wake.<任务>.<UTC 分钟>`，每个任务每分钟一次）。
  正文由 Relay 取该任务自己的 `agent.prompt`，gate 不能代填；只在任务启用、`owner` 等于请求方、`wake.workItemId`
  等于目标 WI 时投递。正文首行“[系统投递：定时唤醒，不是人类消息]”，人类提示首行“⏰ 定时唤醒 <任务>（<WI>），开始处理。”；
  会话忙时同样排队不打断。gate 总以 3 退出并打印 `WAKE_DELIVERED <状态>` 或 `WAKE_NOT_DELIVERED <原因>`；后者由调度器
  经任务的 `delivery.to`（Telegram）或 `feishuOpenId` 给人类发一条提醒，运行记 `reasonCode` 为 `wake_delivered`/
  `wake_not_delivered`，提醒发出时 `deliveryStatus: sent`。gate 以 0 退出记为错误（唤醒任务从不起会话）。缺
  `wake.workItemId`、`agent.gate` 或提醒目标的唤醒任务不载入。
  `@anchor symbol finishWake`
  `@anchor test Relay/tests/collab-delivery.test.ts :: "hands a scheduled wake to the holder once per job and minute, with the job prompt only"`
  `@anchor test Relay/tests/scheduler.test.ts :: "a wake job never starts a runtime and reminds the human when nobody holds the WI"`
  `@anchor test Relay/tests/scheduler.test.ts :: "rejects a wake job without a WI, gate or reminder target"`

## 4c-1. Agent 自管定时任务

- [x] CC/Codex 会话的 pulse 工具（MCP 桥）提供 `pulse_jobs`（Pi 会话暂不提供）：`list`、`add`、`edit`、`enable`、`disable`、
  `delete`。工具只把请求交本机 Relay `POST /internal/agent-jobs`，不带 Relay 内部令牌。Relay 给它托管的每个会话
  发一把密钥（数据目录下私有的 `agent-jobs-keys/`，与原生会话证明同级保护），只把文件路径交给该会话的 MCP 桥
  （`PULSE_AGENT_JOBS_KEY_FILE`，SDK、Codex 与 pool 起的 CC 对话都带；定时运行没有，也不放行该工具；发放失败时对话照常启动，只是 `pulse_jobs` 不可用）；工具用它对密钥引用、时间、一次性随机数和请求正文做 HMAC 签名，
  密钥本身不随请求发出（`agent-jobs-auth.ts`）。Relay 按验证通过的密钥认出会话，owner 取该会话的 Agent 主体，
  不取正文；未签名、签名不符、时间偏离 60 秒以上、随机数重用、或密钥不属于本机当前托管的会话，一律 401。
  正文里核心身份（`identity`）给出的 `session` 与 `surface` 还须是该会话的原生会话。规则在 `agent-jobs.ts` 执行：只动 owner 为调用方的任务，id 以 `<owner>-` 开头；
  平台任务族（`-collab-inbox`、`-collab-deliver`、`-pr-review-*`）不受理；`add`/`edit`/`enable`/`delete` 只对本工具建的
  任务（`managedBy: "agent"`），其他自有任务只能 `disable`，重新启用仍归 PULSE；每个 owner 至多 8 个自管任务，cron 的分钟
  字段须是单个数字（至多每小时一次），批处理任务超时 60–3600 秒、工作区是该 Agent 配置的工作区、注入文件在工作区内；
  运行时自管任务的注入文件按真实路径读取，经链接落到工作区外即拒读（`readInjectFile`）。
  带 `wake.workItemId` 的是定时唤醒任务，gate 与提醒目标取自该 owner 的 `-collab-inbox` 任务。
- [x] 每次变更先用调度器自己的严格载入校验候选配置并预读 `automation-control.json`，通过后写 `cron-jobs.json`
  与其 `automation-control.json` 条目（`decision_ref: agent:<owner>`，删除时移除条目），再热加载调度器；任一步失败
  两份文件恢复原样、报“未生效”。节点分配模式或维护切换屏障持有时一律拒绝。
  `@anchor test Relay/tests/agent-jobs.test.ts :: "answers only a conversation this Relay hosts for the caller"`
  `@anchor test Relay/tests/agent-jobs.test.ts :: "writes nothing when the control file is broken and restores both files when reload fails"`
  `@anchor test Relay/tests/agent-jobs.test.ts :: "never follows a link out of the workspace"`
  `@anchor test Relay/tests/agent-jobs.test.ts :: "adds a wake job with the inbox gate and reminder target, registers it and reloads"`
  `@anchor test Relay/tests/agent-jobs.test.ts :: "may stop an old own job but never re-enable, edit or delete it"`
  `@anchor test Relay/tests/agent-jobs.test.ts :: "refuses other owners, platform jobs, frequent schedules, long runs and too many jobs"`
  `@anchor test Relay/tests/agent-jobs.test.ts :: "signs the session identity with the conversation key, never sending the key nor a Relay credential"`
  `@anchor test Relay/tests/agent-jobs.test.ts :: "accepts only a request signed with the calling conversation key, once and in time"`
  `@anchor test Relay/tests/agent-jobs.test.ts :: "refuses an unsigned HTTP request that copies another session reference"`
  `@anchor test Relay/tests/scheduled-cc-host-mcp.test.ts :: "gives a pool conversation its jobs key and pulse_jobs, and a scheduled run neither"`

## 4d. 手动运行

- [x] `relay-jobs run <id>`（经 `pulse-relay jobs run <id>`）请运行中的 Relay 执行，是一次真实运行：
  常驻调度器 `runJob` 认领、按任务 `delivery` 投递、写运行状态，与 cron 和 Telegram `/job_run` 同一语义。
  入口为 `POST /internal/jobs/run`，请求体 `{"jobId": "<id>"}`，`x-relay-token` 鉴权同其他 `/internal/*`。
  认领成功即答 202（`jobId`、`name`、`acceptedAt`），运行在 Relay 后台继续；任务可能跑一小时，
  命令行不等待。结果看 `relay-jobs list` 该任务的“上次 / 投递 / 运行中”，或 Relay 日志中该任务的
  `job finished` 与 `manual job run finished`（含 `runId`、`deliveryStatus`）。
  拒绝时不启动：任务正在运行或排队答 409 `job_running`，维护屏障中答 503 `maintenance_hold`，
  任务不在运行中的调度器（未 reload 或不属本节点）答 404 `job_not_found`，没有调度器答 503
  `scheduler_unavailable`，缺 `jobId` 答 400。
  `@anchor symbol handleJobRun`
  `@anchor test Relay/tests/relay-http-jobs-run.test.ts :: "claims the run on the resident scheduler, answers 202 at once, and refuses a second run"`
  `@anchor test Relay/tests/relay-http-jobs-run.test.ts :: "on the real scheduler, a maintenance barrier is refused with 503 and nothing starts"`
  `@anchor test Relay/tests/relay-http-jobs-run.test.ts :: "on the real scheduler, the claim is visible as soon as runJob returns"`
- [x] 命令行取常驻 Relay 地址的方式与 Relay 自身相同：端口 `resolveRelayHttpPort`（`RELAY_HTTP_PORT`），
  主机先取 `RELAY_STATUS_HOST`，否则 `resolveRelayHttpHost`，`0.0.0.0`/`::` 改连 `127.0.0.1`；令牌取
  `RELAY_INTERNAL_TOKEN`。这些值由 `pulse-relay jobs` 从常驻进程环境带入。取不到端口或连不上时报错并提示
  `--local`，不自动改为本地运行。
  `@anchor symbol residentRelayUrl`
  `@anchor test Relay/tests/relay-jobs-cli.test.ts :: "asks the resident Relay to run the job with its token and returns once accepted"`
  `@anchor test Relay/tests/relay-jobs-cli.test.ts :: "fails with a --local hint when the resident Relay is unknown or unreachable"`
- [x] `relay-jobs run <id> --local` 在命令行进程里另建调度器（不注册 cron、`deliveryEnabled=false`）执行，
  输出标明“本地运行，不投递”；它不与常驻 Relay 共享认领与并发闸，只用于试跑。
  `@anchor test Relay/tests/relay-jobs-cli.test.ts :: "--local runs in this process, says it does not deliver, and makes no Relay request"`

## 4e. 合同入账即跑协作收件箱

- [x] 事件运行：`triggerJob(id)` 立即启动一次无人值守运行，与 cron 走同一认领、自动化控制、全局与
  workspace 队列，prompt 同样带 `trigger=cron`，观察槽记为当前分钟。它不等待也不记跳过日志：任务不存在答
  `not_found`，禁用答 `disabled`，维护屏障持有答 `held`，已认领或排队答 `running`，由调用方重试。
  `@anchor symbol triggerJob`
  `@anchor test Relay/tests/scheduler.test.ts :: "starts one unattended run now, marked trigger=cron, and reports a claimed job as running"`
  `@anchor test Relay/tests/scheduler.test.ts :: "answers held while the maintenance barrier holds new runs"`
- [x] 合同入账后立即跑对应 Agent 的协作收件箱任务，定时槽保留为兜底。Relay 从已安装核心旁的
  `host-policy.json` 读生产账本绑定，每 5 秒 stat 该检出的远端跟踪引用 `refs/remotes/<remote>/<branch>`
  （松散引用与 `packed-refs`）。本节点内核的每次发布与读取刷新都会由 Git 顺带移动这个引用，所以监视只做
  本地读取，从不访问 GitHub。引用移动时，Relay 在本地对比新增日志条目
  （`.harness/core/journals/<wi>/<序号>.json`，内联或分块存放）：`CollaborationOffered` 关系到各接收方，
  `CollaborationResponded` 中 `accept` 以外的答复关系到发起方（从同一 WI 的日志找回其发起记录）；
  `PullRequestReviewed`（审核未过、需人类确认）与合并效果（`effect_id` 以 `.merge` 结尾）的
  `EffectObserved`（已合并、检查失败、合并失败或受阻）关系到登记该轮的 Agent（合并效果经
  `EffectRequested` 找到轮次，轮次经 `PullRequestSubmitted.author` 找到 Agent，同批或同一 WI 的日志）。
  对每个这样的 Agent，触发本节点 gate 为 `collab-inbox-gate.py check --actor <Agent>` 的已启用任务。
  投递仍只走这次 gate 运行：主身合同、发起方待结清项与 PR 结果照旧精确匹配持有会话、忙时答 `deferred`，其余照旧唤醒 Agent；
  去重由 gate 状态与 `collab-delivery-ledger.json` 决定，规则不变。任务正在运行或被维护屏障挡住时每次
  轮询重试，15 分钟仍未启动即放弃；同一任务的事件运行间隔至少 60 秒。Relay 启动时只记基线；单次移动
  新增条目超过 256 条、条目不可读、引用读不到，以及其它节点发出而本节点尚未读到的合同，都留给定时槽。
  `@anchor symbol createCollabLedgerWatch`
  `@anchor symbol collaborationActors`
  `@anchor symbol collabInboxActor`
  `@anchor test Relay/tests/collab-ledger-watch.test.ts :: "names recipients of an offer and the requester of an answer, never an acceptance"`
  `@anchor test Relay/tests/collab-ledger-watch.test.ts :: "triggers the inbox job of each concerned Agent once the tracking ref moves"`
  `@anchor test Relay/tests/collab-ledger-watch.test.ts :: "gives up on a job held past the pending limit and leaves it to the schedule"`
  `@anchor test Relay/tests/collab-ledger-watch.test.ts :: "names the Agent that registered a round when it is reviewed or its merge is read back"`
  `@anchor test Relay/tests/collab-ledger-watch.test.ts :: "runs the inbox job of an Agent on request, as for a ledger event"`

## 5. 已知两义与债（登记待裁）

`@anchor manual 终态过滤零测试 :: 803 事件两个修复里并发上限有测试,认状态不认时间戳的终态过滤完全没有;这是 watcher 域第一顺位的补测点`
`@anchor manual 并发上限环境变量丢参 :: 调度器组装路径向 watcher 手工转发配置时漏了最大并发字段,生产环境变量完全无效永远走默认 5;测试直调 watcher 传全量配置所以绿——测试绿生产哑火实锤`
`@anchor manual 两套 cron 求值器口径分歧 :: 补偿器日/月字段是与语义,node-cron 与标准 crontab 是或语义;两者都非通配的表达式上补偿器与真实触发会分歧`
`@anchor manual 六字段表达式在 TG 命令被截断 :: 类型注释称五字段,补偿器收五或六,TG 建 job 命令硬取前五个 token 把第六段并进 prompt;三处口径不一`
`@anchor manual 超时默认注释错十二倍 :: 类型注释写默认 300 秒,实现三处都是 3600;spec 以实现为准`
`@anchor manual stop 不清运行中集合 :: watcher 停止时清理四类状态但不清在飞集合与等待队列,跨启停周期残留`
`@anchor manual 配置样例是旧版式 :: 样例文件是 v1 无 owner 带内嵌状态,与现行 schema 脱节,照抄即触发迁移写盘`
`@anchor manual CLI 双实现读入不等价 :: 命令行工具自带的 job 读入无任何字段校验,会显示被调度器静默跳过的非法 job;同名函数两义`

## 6. 覆盖声明

本 spec 给补偿防回放指纹、四层并发防重、watcher 十一层过滤与 803 修复判据、
job 存储契约首次挂显式锚点。存量 agent-scheduler 目录现行契约由本文接管。
