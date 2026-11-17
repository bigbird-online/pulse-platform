# SPEC：Relay 配置/模型/HTTP 面 — relay.json / 模型链 / 内部端点 / web 终端

> version: `0.4.0`
> status: `reverse_engineered_from_code`
> authority: 配置 schema、模型传参链（校验层≠生效层）、HTTP 端点面的**现行实现契约**
> owner: Pulse（wi_20260804_070 · KR2a）
> modified: `2026-11-11`
> 版本史：只看 git log

**取证基础**：2026-08-09 代码普查 + spec-anchor 机器验证全绿。

## 1. relay.json schema 与校验

- [x] 类型真源与唯一校验函数：飞书通道缺凭据抛、TG 通道（含缺省）缺 botToken
  抛、两者全无抛；openId 缺失不阻断（配对流程补）且注释声称的 warn 并不存在
  `@anchor symbol AgentConfig`
  `@anchor symbol UserConfig`
  `@anchor symbol RelayConfig`
  `@anchor symbol validateConfig`
  `@anchor symbol ConfigValidationError`
- [x] provision 写入侧：id 形态正则、五类前置校验、备份后原子写、dry-run
  不落盘（但内存已污染——登记 §5）
  `@anchor symbol provisionAgent`
  `@anchor symbol buildAgentConfig`
  `@anchor symbol ID_PATTERN`
  `@anchor test Relay/tests/provision.test.ts :: "rejects duplicate agent id under same user"`
  `@anchor test Relay/tests/provision.test.ts :: "writes new TG agent, creates workspace, leaves backup"`
  `@anchor test Relay/tests/provision.test.ts :: "dry-run does not write config or backup"`
- [x] 模型覆盖三态（undefined 继承 / null 显式默认 / 字符串覆盖）落
  agent-models 文件（`{RELAY_HOME 或 HOME}/.relay/agent-models/{userId}.json`）
  原子写，重启存续
  `@anchor symbol agentModelOverridePath`
  `@anchor test Relay/tests/router.test.ts :: "setAgentModel(undefined) persists null sentinel so default survives restart"`

## 2. 模型传参链（六层，校验层≠生效层——0805 事故的机器化）

- [x] L0 目录装载（环境变量指路径，未设即整链退化 legacy）；L1 默认值注入
  内存；L2 用户输入校验（目录在位时只认精确 id）；L3 落盘不做格式校验
  `@anchor symbol createModelCatalog`
  `@anchor symbol validateModelId`
  `@anchor symbol CLAUDE_ALIASES`
  `@anchor test Relay/tests/model-catalog.test.ts :: "rejects unknown model ID"`
  `@anchor test Relay/tests/model-catalog.test.ts :: "accepts Claude short alias case-insensitively"`
  `@anchor test Relay/tests/model-catalog.test.ts :: "does not fuzzy match"`
- [x] L4 provider 接入：目录 `providers` 节逐个声明 `protocol`、`base_url_env`、
  `api_key_pool`、`force_interactive`；模型条目以 `provider` 引用，可带 `priority`、
  `currency` 与每百万 token 单价，计量按同一目录定价。目录只写环境变量名，不存 key 值。
  spawn 时按 ccModel 注入所属 provider 的端点与 key，无 provider 的条目走默认
  Anthropic env。key 以 `{tenantId}:{provider}` 为粘性单位，429 后按错误文本中的
  重置时间冷却（解析不出时 60 秒）。`/model auto` 每次 spawn 选第一个有可用 key 的
  模型；当前 provider 的 key 全部冷却或返回 provider 错误时，按 `priority` 从头扫描
  切到可用 provider 并新开 session。经 OpenRouter 的模型 id 带上游前缀（如
  `deepseek/deepseek-v4-flash-0731`）。示例目录：`Relay/config/models.example.json`
  `@anchor symbol resolveModelProviderEnv`
  `@anchor symbol resolveApiKey`
  `@anchor symbol clearStickyKey`
  `@anchor symbol resolveAutoModel`
  `@anchor symbol findNextAvailableModel`
  `@anchor symbol parseResetTime`
- [x] **L5 生效层**：进程池拼 argv 前有格式正则，不匹配即**静默丢弃**只写
  控制台警告（0805 带斜杠 id 被旧正则丢掉即此处；修复后正则含斜杠）。该正则
  至今无单测，且批任务/网页终端/cc-sdk 三条旁路不过它——登记 §5
  `@anchor symbol appendModelArg`
  `@anchor symbol VALID_MODEL_ID`
  `@anchor symbol resolveConfiguredCcModelEnv`
- [x] 上下文额度并行链：内置前缀规则族 + 环境覆盖 + 默认 128k
  `@anchor symbol resolveContextWindowLimit`
  `@anchor symbol BUILTIN_CONTEXT_RULES`
  `@anchor const DEFAULT_CONTEXT_LIMIT = 128_000`
  `@anchor test Relay/tests/model-context.test.ts :: "resolveContextWindowLimit returns 1M only for claude-opus-4-7"`
  `@anchor test Relay/tests/model-context.test.ts :: "resolveConfiguredCcModelEnv matches pool env keys"`

## 3. HTTP 内部端点

- 原飞书会话恢复入口为 `POST /internal/wi/recover`，必须配置非空内部 token 并通过
  `X-Relay-Token` 验证；空 token 不开放开发态。请求只接受 `poolKey`、`workItemId`、
  `humanInputRef` 三个选择字段。Host 读取并验证已持久化的真实人类输入及原 reinit
  签名回执，核对原 bot、WI、原生会话和当前绑定；调用方不能提交消息正文或授权内容。
  尚未派发的旧恢复请求受最新人类输入和 Stop 边界约束，重启不能重放已被覆盖的业务。
  请求进度持久化；忙碌和暂时故障由 Host 接续。同一请求重复调用或重启后重试不重复
  派发已被原生会话接受的工作。恢复必须准备原会话运行环境，并走正常 reinit／自动
  隔离恢复与原 bot 响应链；`dispatched` 只证明续办已派发，不等于业务恢复验收通过。
  原生工具实际执行、原工作数据保全及飞书回复仍须分别读回。
  已 `dispatched` 且保存了 canonical 提交和隔离恢复结果的请求，由同一 Host worker 自动尝试
  接回正式能力，兼容没有接回字段的既有 v1 记录。`canonicalAttachment` 分别报告 `pending`、
  `ready` 或 `superseded`；`ready` 必须来自 fresh plan、工作区准入、实际正式进程与计量基线读回。
  接回不重复 reinit、不重复派发业务，也不把旧输入恢复为当前输入。后续普通业务问句不撤销
  已提交的 grant：Host 保留原已接受 proof，由官方准入核验其当前 grant 和精确资源范围。
  每次接回以最新持久输入边界为本次比较基准，异步期间的新普通输入使本次退出并退避重试。
  明确的 Stop、kill、new、reset、rollover 等控制或已确认的原会话身份变化则停止旧接回；
  控制边界在既有边界记录中持久保留，后续普通问句不能清除它。旧版因普通输入误记的
  `human_decision_superseded` 可依据这些真实控制事实重新判断。机械故障持久退避并保留本地能力。
  隔离 consumer 因空闲回收或 Host 重启而消失时，普通新消息的正常启动失败可从原
  `dispatched` 隔离结果恢复它，即使当时没有 canonical 提交或 kernel 仍不可读。
  恢复只接回同一 pool、WI、原生 Session、工作区和已接受的签名 scope，经正式
  restore/admit 校验当前 proof 与资源锁，不重新 reinit、不派发旧业务，也不替换任何存活进程。
  本次入站边界与启动前的持久绑定跨 await 保持一致；新的控制边界、pending entry 或
  terminal 投影拒绝旧 consumer 恢复。纯进程 Stop/kill 后复用旧 proof 仍采用保守拒绝策略。
  `@anchor test Relay/tests/feishu-host-recovery.test.ts :: "discovers an old dispatched v1 isolated result after restart and attaches canonical without replaying work"`
  `@anchor test Relay/tests/feishu-host-recovery.test.ts :: "checks Stop after asynchronous canonical readback before claiming ready"`
- [x] 推送端点（harness 告警链的对侧）：鉴权 = 环境 token 非空时头部精确比对，
  **空即全放行**；限体 16K；先解析目标后发送防半发；**部分失败仍回 200 仅
  body ok:false**——调用方必须读 body
  `@anchor symbol handlePush`
  `@anchor const BODY_LIMIT = 16 * 1024`
  `@anchor test Relay/tests/relay-http-push.test.ts :: "both: 200 + two sent receipts (toTelegram + toFeishu)"`
  `@anchor test Relay/tests/relay-http-push.test.ts :: "400 missing_to with missing=both when both legs have no address at all"`
  `@anchor test Relay/tests/relay-http-push.test.ts :: "401 when X-Relay-Token header missing"`
  `@anchor test Relay/tests/relay-http-push.test.ts :: "200 with no header when RELAY_INTERNAL_TOKEN unset (dev mode)"`
  `@anchor test Relay/tests/relay-http-push.test.ts :: "413 payload_too_large for oversized body"`
  `@anchor test Relay/tests/relay-http-push.test.ts :: "feishu image + text caption: 200 + two receipts (image then text)"`
- [x] 持久通知采用独立 `POST /internal/notice`：单渠道文本、原请求键和内容/路由
  digest，限体32K、文本UTF-8 4096bytes；保留内部token和harness-escalation人类
  注意力门。Telegram重试数为0，飞书一次SDK调用，返回原binding、sendAttempted
  及实际message ID；HTTP 200不等于送达。普通push的默认重试行为保持。
  端点本身不声称幂等；调用端须在I/O前持久独占原attempt，未知结果只读回不重发。
  `startNoticeOnlyServer`为隔离验收复用同一handler，固定loopback和确切目标集合，
  不开放其它管理端点或启动polling/WS。源码覆盖不等于生产部署或真实渠道验收。
  `@anchor symbol parseNoticeBinding`
  `@anchor symbol startNoticeOnlyServer`
  `@anchor test Relay/tests/relay-http-push.test.ts :: "notice-only experiment pins destinations and exposes no legacy endpoints"`
- [x] 部署维护屏障：`POST /internal/maintenance/hold {ttlMs, token?}`（上限 10 分钟，到期
  自动释放）与 `/internal/maintenance/release {token}`，均需 internal token。每次持有返回
  唯一凭证，同一响应附带持有之下的 cc-status 快照；他人持有时拒绝（409），续期与释放都
  必须带当前凭证。持有期间新对话轮次按到达顺序排队、不派发也不丢弃；cron、漏跑补跑与手动
  运行共用的领取处不领取任何任务（手动运行明确报错，漏跑对账在释放后补）。cc-status 增加
  `busySessions`（tenantId 与实际运行时）与 `maintenance.held/queued`（只给 tenantId 与
  runtime，不含消息正文、不含凭证）。关闭时先关闭接入：排队中和此后到达的轮次在到达时即把完整消息
  （含图片、文档等内容块）写入该会话独立的 `pendingMaintenanceTurns`，不再派发，也不与
  运行时回调写的中断回合字段共用；CC 进程池、Codex、cc-sdk 中排在忙碌回合之后、尚未派发的轮次（cc-sdk
  在回合进行中不向引擎送新轮次，在 Relay 内按到达顺序等待），以及 CC 进程池中正在读取会话授权（派发准备）
  的轮次随后同样写入，此后才到运行时的轮次不再写给停止中的进程。每个轮次在交给运行时时
  取得进程内单调递增的到达序号（起点取时钟，下一进程的序号大于上一进程记下的），列表按到达序号
  排序，同一毫秒内屏障与运行时分别记下的轮次也不错序；同一序号只记一次。升级前记下、没有序号的记录在首次读写时
  按列表顺序补上各自不同的序号并保存（不低于其时间换算值，且大于前一条），同一毫秒的两条仍是两条。渠道停止后再保存一次。
  重启后 Telegram/飞书先续跑被中断的回合，再按到达顺序重放这些消息：每条沿用原序号，运行时接收
  （见下条回执）时才从列表删去，接收后即发下一条；重放中再次关停时，已接收的轮次按原序号交回、
  碰到已关闭屏障的轮次原位记下，列表不被旧副本覆盖，不丢不重。本次中断的会话写
  `shutdownInterruptedAt`，Telegram/飞书续跑窗口从中断时刻起算
  `@anchor symbol createMaintenanceBarrier`
  `@anchor symbol persistQueuedTurns`
  `@anchor symbol nextTurnSeq`
  `@anchor test Relay/tests/maintenance-barrier.test.ts :: "queues a new Pi turn while held and dispatches it on release, never dropping it"`
  `@anchor test Relay/tests/pool.test.ts :: "records the busy turn as interrupted and hands the queued turns over in order"`
  `@anchor test Relay/tests/codex-conversation-runtime.test.ts :: "records turns queued behind the busy Codex turn at shutdown and replays them in order"`
  `@anchor test Relay/tests/cc-sdk-conversation-runtime.test.ts :: "records sends held behind the busy turn at shutdown, in arrival order, and keeps the interrupted-turn flag"`
  `@anchor test Relay/tests/cc-sdk-conversation-runtime.test.ts :: "holds a send in Relay until the turn in flight ends, then serves it as its own turn, in order"`
  `@anchor test Relay/tests/pool.test.ts :: "keeps arrival order within one millisecond: the pool hands over its older turn after the barrier recorded a newer one"`
  `@anchor test Relay/tests/pool.test.ts :: "a second shutdown during the replay of recorded turns loses none and records none twice"`
  `@anchor test Relay/tests/maintenance-barrier.test.ts :: "replays records saved before arrival numbers existed, two in one millisecond, each once and in order"`
  `@anchor test Relay/tests/cc-sdk-conversation-runtime.test.ts :: "a second shutdown while recorded turns are replayed behind the resumed turn loses none and records none twice"`
- [x] 运行时接收回执：交给运行时的轮次带一个接收回执（`TurnArrival.accepted`），在轮次进入关停会
  接手的记录时发出：维护屏障排队或记下、CC 进程池排队、派发准备（会话授权读取期间）或在途回合、
  Codex/cc-sdk 排队或开始执行；其余运行时在发送返回后由运行时管理器补发。关停开始后到达进程池的轮次先
  发回执再记下。渠道接收日志与重放都只凭回执删除记录，发送调用本身不算接收
  `@anchor symbol TurnArrival`
  `@anchor test Relay/tests/pool-session-access.test.ts :: "a shutdown while a turn waits for its access readback records it for resume; the receipt came first, nothing reaches the process"`
- [x] 受管切换屏障（`relay.switch-barrier` v1）：`GET /internal/maintenance/switch` 返回
  `capability {name, version}` 与当前状态（不支持的 Relay 答 404）；`POST .../switch/acquire|renew
  {holder, transaction, token, leaseMs}`、`.../switch/release {holder, transaction, token}`、
  `.../switch/break {transaction, operator, reason}`，均需 internal token。token 由调用方生成
  （32–256 位），Relay 只存其 SHA-256，响应不回显；同一三元组重发 acquire 幂等，acquire 同一响应附带
  持有之下的 cc-status 快照；leaseMs 5 秒至 60 分钟。记录落在 `$RELAY_DATA_DIR/maintenance/switch-barrier.json`
  （目录 0700、文件 0600，临时文件 fsync 后改名再 fsync 目录），先落盘后应答；写失败答 500 `persist_failed`，
  内存按磁盘回读，原状态保持可重试；记录已落盘、之后才报错（如目录 fsync）按写成功处理。状态 `absent/held/expired_unreleased/released/invalid`：租约到期转
  `expired_unreleased`（写明 `expiredAt`、`reason: lease_expired`）并继续阻断，只有原持有者 renew（记
  `lastExpiry`，答 `recovered`）或 release、或人工 break（transaction 精确匹配，损坏记录为 `null`，记
  `releasedBy: operator`、operator、reason）能结束；文件损坏或不可读为 `invalid`，阻断至 break。他人
  acquire 答 409 `held_by_another`，三元组不符答 409 `not_holder`，同一事务重复 release 答 200
  `alreadyReleased` 并再次唤醒本进程的等待轮次。新进程在启动渠道、调度、恢复与 HTTP 之前读取记录，`readAtStartup` 写明所读状态；
  阻断期间与短屏障共用同一闸门（新轮次等待、任何任务不领取），启动后的渠道续跑与接收日志重放、原生会话交接恢复
  都推迟到闸门重开。阻断期间仍有上游持久记录（渠道接收日志、待续跑列表、持久入口）的轮次不发接收回执，
  关停时留给该记录由下一进程重放；其余轮次到达即写入会话 `pendingMaintenanceTurns`，放行后由运行时接收回执
  删去该副本，接收前派发失败则保留同一编号供重放；取得切换屏障时已在短屏障下等待的轮次同样写入。只存于内存
  （回执已发或本无上游记录）且写入失败的轮次记为未保管：acquire 已落盘持有但答 409 `pending_unsecured`（附 cc-status
  快照），同一三元组重发 acquire 即重写这些轮次，全部写入后才答 200，放弃则由持有者 release；cc-status 的
  `maintenance.unsecured` 给出其数，非零时 `safeToRestart` 为 false；写入失败而留在内存状态里的副本随派发一并删去。
  启动时在开放任何接收之前设启动续跑闸门：渠道续跑结束前只放行续跑发出的轮次，切换屏障提前释放也不放行其余轮次；
  没有屏障的启动同样如此。闸门重开时
  先跑渠道续跑：续跑未结束前只放行它发出的轮次（续跑提示与待续跑记录，`TurnSubmitOptions.resume`），其余轮次
  照阻断期间的规则等待并保持持久；续跑重放的记录若正由本进程等待，不再重复派发；续跑结束后等待轮次按到达编号
  放行，前一进程记下的轮次排在本进程后到的轮次之前。短屏障的请求、响应、`held` 含义与到期自动释放不变，
  两者互不释放；cc-status 的 `maintenance` 另给 `capabilities`、`blocking` 与 `switch`
  `@anchor symbol createSwitchBarrierStore`
  `@anchor symbol startupResume`
  `@anchor symbol acquireSwitch`
  `@anchor symbol unrecordPendingTurn`
  `@anchor test Relay/tests/switch-barrier.test.ts :: "persists the hold with private permissions and a new process reads it before taking work"`
  `@anchor test Relay/tests/switch-barrier.test.ts :: "a lapsed lease never opens: it stays blocking across a restart until the holder recovers or releases"`
  `@anchor test Relay/tests/switch-barrier.test.ts :: "withholds the receipt of journaled turns, records the rest at arrival, and dispatches each once on release"`
  `@anchor test Relay/tests/switch-barrier.test.ts :: "keeps its own token and expiry; neither barrier opens the gate while the other is in force"`
  `@anchor test Relay/tests/switch-barrier.test.ts :: "keeps a recorded turn when its dispatch fails before the runtime receipt, for a later replay"`
  `@anchor test Relay/tests/switch-barrier.test.ts :: "on release, turns recorded by the earlier process go out before turns that waited in this one"`
  `@anchor test Relay/tests/switch-barrier.test.ts :: "a release that reached the disk before the write reported an error opens the gate"`
  `@anchor test Relay/tests/switch-barrier.test.ts :: "serves capability, acquire, renew and release with readback and error codes"`
  `@anchor test Relay/tests/switch-barrier.test.ts :: "a waiting turn whose only copy cannot be written refuses the switch acquire and keeps restart unsafe until a retry writes it"`
  `@anchor test Relay/tests/switch-barrier.test.ts :: "a release before the startup resume pass has started still runs turns of the earlier process first"`
  `@anchor test Relay/tests/switch-barrier.test.ts :: "a startup without a barrier also runs pending turns before turns that arrived first in the new process"`
- [x] 手动运行入口 `POST /internal/jobs/run {jobId}` 复用 `checkInternalToken`，在常驻调度器上认领后答 202；
  状态码与命令行契约见 [relay-scheduler-jobs.md §4d](relay-scheduler-jobs.md#4d-手动运行)
  `@anchor symbol handleJobRun`
- [x] health / cc-status 与心跳读写均复用 `checkInternalToken`（环境 token 非空时
  精确比对，空配置仅作开发态放行）；心跳另有原子写、目录 700、文件 600。health
  与 cc-status 的受管本机消费者从实际服务进程或 launchd 读取监听和鉴权输入，
  不把 token 写进日志或快照
  `@anchor symbol handleHealth`
  `@anchor test Relay/tests/relay-http.test.ts :: "rejects a request without the internal token"`
  `@anchor symbol checkInternalToken`
- [x] pairing approve / agents reload 仍**无鉴权**，而 bind host 可配 0.0.0.0——
  Vultr 即全接口 bind，当前靠防火墙限制网络可达面；这两条写端点是未被本单掩盖的
  残余安全债
  `@anchor symbol handlePairingApprove`
  `@anchor symbol handleAgentsReload`
  `@anchor symbol resolveRelayHttpPort`
  `@anchor symbol resolveRelayHttpHost`
  `@anchor test Relay/tests/relay-http.test.ts :: "409 when agent does not match pending pairing"`
- [x] web 终端边界：token 十分钟滑动过期、单连接排他、PTY 连接时才 spawn、
  凭据前缀过滤注入
  `@anchor symbol createToken`
  `@anchor symbol validateToken`
  `@anchor symbol attachWebSocket`
  `@anchor const TTL_MS = 10 * 60 * 1000`
  `@anchor symbol AUTH_PREFIXES`

## 4. 已知两义与债（登记待裁）

`@anchor manual 模型生效层正则零测试三旁路 :: argv 格式正则无任何单测,批任务/网页终端/cc-sdk 三条路各自拼参不过它,坏 id 在旁路仍能穿透;0805 事故形态只堵了主路`
`@anchor manual 静默丢弃的用户文案说反 :: 模型命令提示称无效 id 会在启动时报错,实际是静默丢弃仅控制台警告,用户无感回落默认模型;文案直接掩盖事故形态`
`@anchor manual 两个写端点仍无鉴权撞全接口 bind :: health/cc-status 已于 wi_20260810_912 补应用层 token;pairing approve/agents reload 仍只靠网络隔离,实际 host 可配 0.0.0.0 且生产就这么配`
`@anchor manual 鉴权逻辑两份内联 :: 推送端点与心跳端点各有一份等价 token 校验,改一处静默分叉`
`@anchor manual 运行时枚举同名不同集 :: 批任务用的运行时枚举与会话用的交集只有两值,各自独有一值;混用会把两条链搅在一起`
`@anchor manual 三份别名集合独立维护 :: Claude 短别名在目录/命令/上下文额度三处各一份,第三份故意少 opus(走 1M 分支),无交叉引用改不传导`
`@anchor manual 热加载缺口 :: agents reload 端点重读 relay.json 与 TG/飞书绑定,模型目录改盘后无任何端点能热加载只能重启`
受管生产实例的服务和 provision 使用同一 `PULSE_RELAY_PATHS`；未声明映射的开发默认不构成生产入口，不能据此选择历史配置。
`@anchor manual 终端凭据双路注入 :: 同一份 ccEnv 既序列化进 argv 的 settings 又走 env 注入,argv 明文可被进程表读到,而孤儿清理逻辑恰在读进程表`
`@anchor manual 关机不回收终端 :: 顶层 stop 拆解链不调终端会话清理,PTY 子进程靠信号传播兜底`

## 5. 覆盖声明

本 spec 给 relay.json 校验、provision 写入、模型六层传参链、HTTP 端点矩阵与
web 终端边界首次挂显式锚点。存量 config-hot-reload / feishu-pairing-http /
web-terminal 三目录现行契约由本文接管。

## 6. 受管实例配置与运维入口

生产实例显式设置 `PULSE_RELAY_PATHS`，选择本服务账户平台根中的
`config/relay/paths.json`（`pulse_relay_paths/v1`）。映射保存实际配置、Pi 清单、
模型目录、调度定义及状态、会话、观测和计量的位置，以及必须存在的渠道入口。
映射不存在或冲突时不能回退到源码相对目录；未接管的开发/测试实例保留显式路径。
完整 `relay.json` 仍使用原 schema，因包含渠道凭据而放在 `secrets/relay/`，
目录 0700、文件 0600；不把凭据正文写进路径映射或 Git。

普通发布由现有发布器保留实例映射与当前配置，不能复活旧 Pi/cron 路径。
一次性搬位使用 `Scripts/relay-config-layout.py` 的计划、执行和读回，保留原
会话、暂停任务、SCL 根和身份。旧配置按角色保全；恢复范围取用户指定的原配置，
“恢复原有多 Telegram bot”必须覆盖其完整 Telegram 集合，不得硬编码为只恢复 `pulse`。
原文件已归档时，按原绝对路径查找备份记录并核对原样内容，不以 mtime 猜选；
逐项保留原 bot 身份、收件人、工作区和运行配置，保全已推进的会话及当前飞书入口。
范围外历史飞书与暂停任务不因 TG 恢复而启用。
调度原登记与历史回执字节不变，生产写入器通过已记录的旧→新位置核对当前文件。
关联采集器和 OS 启动定义引用随同修改，不把备份当第二份活动配置。

必须提供的入口取独立核对的原业务配置或后续明确授权，不能从临时缩减的当前配置反推。
受管实例健康读回比较配置集合、实际运行客户端与必须提供的入口；缺 Telegram
客户端时不能仅因飞书可用而返回健康。内部健康接口仍鉴权，不暴露凭据或会话正文。
受管操作入口为服务账户的 `pulse-relay status|check|restart|reload|jobs|provision`，
它从 OS 实际服务定义选出当前发布物并传入相同环境，不依赖调用者 cwd 或重设 HOME。
真实渠道往返、重启与配置更新分别验收；客户端存在不冒充模型回答或用户价值验收。

受管 Linux 发布、重启和回退的维护参数必须从当前进程读回；沿用同一 `PULSE_RELAY_PATHS` 与 `RELAY_DATA_DIR`，仅传递所需监听参数和令牌，不读取已退役的 `~/.relay/relay.env`。缺失、重复或实例不一致拒绝，不以旧文件或操作者环境兜底。迁移后必须验证一次正式发布入口，而不只验证迁移命令重跑。回归见 `Scripts/tests/test_managed_maintenance_environment.py`。

直接调用受管实例的原始 restart-safe 脚本时，在读取旧环境文件或写日志之前转交已安装的 `pulse-relay`。该入口选择实际服务环境并只调用一次原维护检查；递归标记不是授权凭据，不改变 busy/cron 限制，不接受强制旁路。
启动前检查与发布/回退调用同样使用实际实例的数据目录和监听地址，不新建 `HOME/.relay/restart-audit.log`。缺失、冲突或不可读的受管输入明确失败，不能借用操作者的端口/令牌或退回旧目录。

### 6.1 停止状态下的事务回退

已核验回执的回退先恢复原 OS 启动定义与配套绑定，再处理进程。`current_binding`
对 MainPID=0 会抛错，不是返回空字典；受管回退必须另行读回 OS 的 MainPID 和
ActiveState。只有存在正整数的原进程前像、MainPID=0 且状态为 inactive／failed
时，才允许一次受控 start；failed 先 reset-failed。活动或 activating 等过渡状态
不允许走这条恢复分支。调用结束仍须在原回退流程中核对原程序及绑定，不能把
`start` 返回零当作恢复成功。该路径不要求已经停止的候选提供 HTTP，也不使用
旧环境文件、强制重启、旧源码重编译或数据快照覆盖。

### 6.2 范围、证据与旧文件的处理

路径接管以活动对象和业务身份为单位，不能把最近修改或内容最多的 JSON 当作
正确配置。跨节点可有不同渠道集合；`requiredChannels` 表达本实例必需入口，
不代表全部历史 bot 都应启用。已停用的调度及其他历史配置保持原状态；备份、
版本化默认配置和外部 Runtime 设置不因路径不同而删除。

入口读取、业务字段更新、客户端创建、原生模型执行和消息投递是不同验证层。
隔离模型探针使用已安装运行时和实际模型配置，但不改原用户 session／WI；直接
渠道投递只证明发送路径。二者分别通过，不得描述成真实用户入站至回复已经
通过。自然往返应从实际用户入口与匹配的运行证据核对；没有事件不反复空扫，也不声称通过。不保存私密正文，不伪造上游用户事件，
不为制造通过证据修改路由、授权或临时关闭生产保护。

### 6.1 Muse 隔离试验的源码输入

`prepare --source-checkout` 只允许已登记为指定 WI 的专用试验节点。
源必须是干净 Git 根，HEAD、提交对象与请求的完整 SHA 一致；固定提交的完整 Git archive
复用原 runtime roots、安全解包和正式 npm 构建，归档后重新核对源。Git replace 或调用方
Git 根环境不能替换候选身份。manifest 保存 commit/tree/archive 摘要，复用发布仍验证
CLI、全部声明运行资产与编译后的 Relay 文件全集。`relay_build_sha256` 在正式编译后生成，复用和新版 guardian 恢复前拒绝模块修改、删除、额外文件或重定向；本地 archive 发布缺此记录也拒绝。历史生产包原校验边界保留，不能宣称它们已有新模块绑定。失败不发布半成品，也不改变活动指针、CD 安装或服务状态。

工具 shell 的 HOME 不作为新试验入口的必需身份。显式 `--workspace` 只绑定已有
同 WI 专用节点的绝对物理根，核对节点配置的 guardian 路径及无重定向的管理路径，
在建目录或锁前拒绝不符；不修改 HOME、代理、节点归属或生产入口默认路径。

`experiment --source-checkout` 的激活继续要求本仓完整精确 trusted Linux completed/success
原始回执、同一试验 WI 和原维护守卫。生产部署不接受该本地源参数。本入口不写 Pi Skill
manifest；源码准备、配置采用、实际 Pi 加载和真实渠道结果分别验收。

来源：[正式消费者](../../Scripts/muse-cd.py)、[合同测试](../../Scripts/tests/test_muse_cd.py)、
[运维说明](../../Scripts/muse-node.md#实验源码下载失败时的精确-git-输入)。

### 6.2 发布复用与实验退出

`prepare_release` 复用不可变发布时读取一次原 manifest，校验精确 SHA、CLI 摘要及
原 runtime inventory 的完整文件集合；新增构建要求的资产不追溯为旧包的必需文件。
新构建仍必须满足当前 `REQUIRED_RUNTIME_ASSETS`，并绑定完整 runtime 和 compiled
Relay 集合。原 v1 CLI-only 包没有 runtime 清单、相关目录或后续 source/build 字段，
只保持其原 CLI 摘要合同，不宣称具备后来的完整绑定；新版包缺失清单不得退化为旧包。
文件缺失、改动或额外源文件仍拒绝复用，不通过删除旧 release 回避校验。

`exit-experiment --work-item` 在原 CD 锁内核对既有节点配置及当前实验 WI，只将
purpose 改回 production 并移除当前实验归属；保留 guardian/DC 字段、选定发布、
原回执、配置与会话。它不构建、安装或重启。之后普通 poll 必须重新取得既有
ownermac main 部署授权才可采用发布，没有授权则继续保留当前版本。归属不符或
已退出时明确报错；显式管理命令遇锁占用返回 75，不能把没有执行当成功。
支持的 `--workspace` 仍先核对同 WI 的已登记物理节点，写入前再次核对归属。

来源：[正式 CD](../../Scripts/muse-cd.py)、[合同测试](../../Scripts/tests/test_muse_cd.py)、
[操作者说明](../../Scripts/muse-node.md#专用实验节点)。

### 6.3 精确候选的验证接续

`muse-cd.py verify --sha <SHA> [--ref <已推送分支或标签>]` 与 `experiment`
共用 `pulse_release_verification`。它通过同版本 `pulse-gh.py` 的本地优先控制通道，
直接查询本仓、精确 SHA、指定 trusted Linux workflow 的原始运行事实。已有成功运行
直接复用；运行中只返回运行 URL 和 `verification_pending`；失败返回
`verification_failed`。没有运行且提供的远端 ref 仍指向精确 SHA 时才派发一次。
不需要 Agent 选择主机、手工导出 JSON 或根据本地预检推定可信测试已经通过。

GitHub 是验证状态的唯一权威。磁盘只保存原始派发意图与可重新取得的成功回执，
不能以本地 `success` 标志放行。派发前持久化意图；响应丢失或进程中断后只查询，
不能盲目重复派发。没有可对应运行时返回 `dispatch_unconfirmed`，明确需要核对原
派发；不编造成功或无限重试。ref 漂移、错误仓库/workflow/event/SHA 的运行均不得放行。
每次命令有界返回，调用相同命令即可接续；不会创建 WI、修改计量状态或启动真实对话。

`experiment` 不提供 `--verification` 时使用此接口；未验证成功前不构建、不安装、
不重启。已有调用者传入回执仍使用原合同。验证调用不占用重启锁；激活仍在原 CD 锁内
核对现场，繁忙返回 `activation_deferred`，并在准备结束、写启动器或 manifest 前再次
检查空闲、进程身份与启动器原字节。未知状态不能当作空闲。

源码验证、组件安装、实际加载与人类 UAT 是不同结果。这个接口负责前者并接入已有
实验消费者，不代表原生核心、计量接线、Stats 和 skill 已成套采用，更不授予业务批准。
同版本消费者安装须包含验证模块与 GitHub 通道；失败恢复这组工具的原字节。

来源：[验证模块](../../Scripts/pulse_release_verification.py)、
[合同测试](../../Scripts/tests/test_release_verification.py)、
[Muse 消费者](../../Scripts/muse-cd.py)。

### 6.4 服务凭据环境文件

ownermac 的 launchd plist（`com.pulse.relay.plist`）不放凭据值，只放
`RELAY_SECRETS_ENV_FILE`，指向 `~/.local/share/pulse/secrets/relay/relay-service.env`。
该目录 0700、文件 0600，每行一项 `KEY='value'`；只收凭据形名字（`*_TOKEN`、
`*_API_KEY`、`*_SECRET`、`*_PASSWORD`、`RELAY_BOT_TOKEN_*`），不能借它注入
`PATH`、`NODE_OPTIONS` 等配置。格式与 `RELAY_CC_OAUTH_ENV_FILE` 相同（可带
`export`、引号与行尾注释）；引号必须闭合，闭合后只能是空白或 `#` 注释，否则按格式错误处理。

Relay 进程在读取其他配置文件之前加载该文件。声明了文件，而文件缺失、属于其他账户、
权限宽于 0600/0700、是符号链接、格式错误，或同名凭据仍以明文出现在服务环境里，
Relay 以退出码 78 拒绝启动；错误只写路径和键名，不写值，也不回退到明文。未声明时
不加载（TCloud 由 systemd `EnvironmentFile=` 注入私有文件）。
`@anchor symbol loadRelaySecrets`
`@anchor test Relay/tests/relay-secrets.test.ts :: "refuses a missing file instead of falling back to plaintext"`
`@anchor test Relay/tests/relay-secrets.test.ts :: "refuses a credential that is also present in plaintext"`

子进程只拿必要变量。Agent 会话（CC、CC SDK、Codex、Web 终端；Pi 本来按清单白名单
传递）和 Relay 的辅助程序（含语音转写的 ffmpeg、`ps`、`sudo` 等系统命令）不继承文件里的任何键、`RELAY_BOT_TOKEN_*`、
`TELEGRAM_BOT_TOKEN`、`BOT_TOKEN_*`、凭据形的 `OPENROUTER_*`（模型路由密钥池）与定位变量；
除文件里的键外都按名字识别，TCloud 未声明文件、由 systemd 注入时同样剥离。所选模型路由由 Relay 显式注入。
`RELAY_INTERNAL_TOKEN` 只留给需要回调本机接口的 Relay 自有辅助程序（调度 gate／
verify 命令、原生内核调用），Agent 会话拿不到。Pi 清单白名单列出这些名字时拒绝启动。
`@anchor symbol stripRelaySecrets`
`@anchor test Relay/tests/relay-secrets.test.ts :: "keeps Relay credentials out of CC SDK, Codex and helper children"`
`@anchor test Relay/tests/relay-secrets.test.ts :: "strips provider key pools and service credentials without a secrets file (systemd EnvironmentFile)"`
`Relay/src` 里每个 `child_process`／node-pty 调用都显式传入过滤后的环境，或由调用方传入。
`@anchor test Relay/tests/relay-child-env.test.ts :: "starts ffmpeg without Relay credentials"`
`@anchor test Relay/tests/relay-child-env.test.ts :: "pass an explicit env (or a caller-supplied options object)"`

发布器（`Scripts/pulse-release-writer.py`）准备 plist 候选时，把 plist 中仍有的
明文凭据与文件现值合并，明文优先（首次迁移、回退后再前进都靠它）。目标发布物含
`Relay/dist/relay-secrets.js` 时候选只留定位变量；不含时（回退到旧版）把值重新内联
并删除定位变量，旧版照常启动。准备阶段不写生产文件，但核实目录链可建：从 `secrets/`
到文件所在目录，每级须是本账户所有的真实目录（不是符号链接），缺失的一级其上级须可写，
否则在停 Relay 之前就失败。激活与回退在安装 plist 之前建齐缺失目录（0700）、把更宽的
目录收紧到 0700，再原子写入该文件，只增不删。文件不是部署 cohort 的消费者，回退不恢复它，旧版忽略它。
维护入口（`pulse-relay`、发布器健康读回、`Scripts/ops/pulse-health.sh`）从该文件读
`RELAY_INTERNAL_TOKEN`，不把它写回 plist。
`@anchor test Scripts/tests/test_relay_secrets_env.py :: test_first_migration_moves_every_credential_out_of_the_plist`
`@anchor test Scripts/tests/test_relay_secrets_env.py :: test_rollback_to_a_relay_without_the_loader_inlines_the_values`
`@anchor test Scripts/tests/test_relay_secrets_env.py :: test_first_migration_builds_the_whole_private_directory_chain`
`@anchor test Scripts/tests/test_relay_secrets_env.py :: test_unusable_directory_chain_fails_at_preparation`

## 7. 验证记录与边界

下面的基线只说明记录时已取得的证据，后续版本须重新读回，不是实时健康承诺。

| 范围 | 已证结果与证据 | 未覆盖的范围 |
| --- | --- | --- |
| 早期三端路径接管 | [Mac](https://github.com/bigbird-online/PULSE/actions/runs/35205091403)、[TCloud](https://github.com/bigbird-online/PULSE/actions/runs/35205095608)、[Vultr](https://github.com/bigbird-online/PULSE/actions/runs/35205338394)记录实际映射、受管对象、原状态保全和采集引用；对应接管范围为 1 TG＋1飞书、1 TG＋10飞书、7飞书 | 当时 Mac 仅恢复 Pulse，不能作为其完整原 TG 业务恢复证明；不代表全部历史任务恢复，不代表其他模块已完成目录迁移 |
| ownermac 完整原 TG 恢复 | [35225964865](https://github.com/bigbird-online/PULSE/actions/runs/35225964865) 使用原 `~/.relay/relay.json` 的记录式原样备份；全部 12 个 TG 身份、收件人、路由、Pi 配置及实际加载核对一致，现有 Pulse 飞书保留；[35226318115](https://github.com/bigbird-online/PULSE/actions/runs/35226318115) 为 12/12 实际 Relay 客户端发送成功 | 不代替逐个模型任务及真实用户上游往返；不启用旧飞书或暂停任务 |
| Mac 运维与模型 | [35214007648](https://github.com/bigbird-online/PULSE/actions/runs/35214007648)，2026-09-17，程序 `2a4238349a48`：干净调用环境下原脚本和管理入口检查、正常重启、旧审计路径未重现；隔离实际模型请求及应用路由、TG／飞书直接投递通过 | 上游真实用户入站和手机／桌面交互没有由这批测试证明 |
| 两云维护退出 | [TCloud 35224985473](https://github.com/bigbird-online/PULSE/actions/runs/35224985473)、[Vultr 35224991223](https://github.com/bigbird-online/PULSE/actions/runs/35224991223)，2026-09-17，程序 `6c8a763e6926`：旧日志全部字节保全；原脚本及管理入口在干净环境检查通过，正常重启后配置／渠道不变，旧审计路径没有重建 | Vultr 整个运行仍为 failure：维护步骤通过，后续模型步骤失败；不能把分步骤结果混为整轮成功 |
| 两云模型与投递 | TCloud 同轮实际模型、TG／飞书投递通过；Vultr 应用飞书路由匹配，但 [35226178542](https://github.com/bigbird-online/PULSE/actions/runs/35226178542) 对 `pi / openai-codex / gpt-6-astra` 的请求返回 `provider_auth_failed`；原用户会话和服务进程未修改 | Vultr 当前模型认证未通过；真实上游用户往返未覆盖。不得静默换模型、复制其他节点身份或将渠道客户端在线当作模型可用 |

当前 ownermac 的 12 个 TG 为 `aki`、`amanda`、`asobi`、`co-writer`、`emi`、`hume`、`ken`、`main`、`pulse`、`reeve`、`steward`、`vigil`。活动配置保持平台根内 `secrets/relay/relay.json`，不重新启用旧路径。

配置及维护实现的回归为 `Relay/tests/managed-paths.test.ts`、
`Relay/tests/managed-readiness.test.ts`、`Scripts/tests/test_relay_config_layout.py`、
`Scripts/tests/test_managed_maintenance_environment.py` 和
`Scripts/tests/test_deploy_live_root_contract.py`。故障分支以隔离状态／OS 替身验证，
不在生产制造故障。整机重启、全渠道的真实用户往返及视觉效果只有各自取证后
才报告通过；这些边界不允许被默认健康或测试总数抹掉。

## 原生 WorkItem 日常命令

Pi manifest 的 host-native profile 可声明 workItemCoreHost：
extension、python、entrypoint 均须为 nodeResourceRoots 内真实文件，
Python 须可执行。没有此声明的节点保持当前入口；配置了却不可用时拒绝命令，不回退旧写者。
Relay 仅从该配置向 Pi 注入公开路径 PULSE_WORK_ITEM_CORE_HOST，不采用外部环境中的同名值。
真实 Relay 父进程证明独立于旧命令扩展提供：使用当前 PID、Host session ref 与节点 surface，
不继承外部环境给出的 ancestry；停用旧写者不能丢掉新核心的原生进程准入证明。

Telegram/Feishu 的 /wi 共用 native-work-item-command 路由：
先保存原渠道输入，再获取原会话，确认真实 Pi runtime 且没有后来输入覆盖边界，才派发。
这一分支绕过旧 WorkItemSessionController、SclIntentRetryCoordinator 和 pi-scl-intent。
扩展在原生 user 消息落盘后执行新核心的 channel 入口；Agent 仅解释结果。
同一上下文重试复用原执行结果，unknown 不自动重发；普通对话的工具在命令结束后恢复。

支持词汇、原请求幂等、业务 skill 和真实渠道验收见
[新核心操作合同](../../agent-harness/project-specs/rewrite-native-operations.md#relay-daily-command-contract)。

### 新内核节点发布

已配置 `workItemCoreHost` 的 profile 先核验新核心的受管资源，再启动；旧 ComputeCost/SCL
路径缺失、退役或旧执行开关遗留不得成为启动条件。云端新核心发布不安装旧 Harness/SCL，
不要求旧 reconciler 存活；新核心采用、唯一生产账本、实际进程绑定和健康分别读回。
发布器显式关闭旧执行与恢复进程。失败回退仍保留兼容的新事实读取者。

Muse 的可信 runner 核验指定完整 SHA 的成功 CI 和 main 祖先关系，再创建指向该 SHA 的
GitHub Actions deployment。main 上新增账本提交不改变已核验候选；消费者仍核对应用、
环境、45 分钟时效、成功状态和 main 祖先关系，不接受另一分支或不同 SHA。
显式 REST 发布记录的 App 字段为空时，必须核对 GitHub Actions 官方 Bot 身份，以及对应
main 发布 workflow 的成功运行和包含同一候选 SHA 的运行标题；普通用户记录不获准采用。

节点全局内核选择和各 profile 必须经过同一个资源审核与路径归一化再比较。目录别名指向
同一 Python 环境不构成第二内核；不同解释器、扩展或内核文件仍必须拒绝。
发布前的 manifest 校验同时调用实际 Relay 节点内核入口，避免只验 profile 却在停服后
才发现节点级绑定不一致。旧版本回退的检查保持其原有接口。
