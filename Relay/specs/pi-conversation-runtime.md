# SPEC：Pi Conversation Runtime（第四运行时）

> version: `0.1.4`
> status: `implementation_candidate`
> authority: `wi_20260822_938`
> portfolio mapping: `CR-02`（Relay/control-plane 扩展；不新增第四主结果）
> owner: Pulse
> modified: `2026-10-02`

## 1. 冻结语义与非目标

Pi 是 Relay 的第四个 `ConversationRuntimeAdapter`，与 `cc`、`codex`、`cc-sdk`
并列。Pi **不是** `ProviderDef`：runtime 负责会话/RPC/生命周期，provider 与 model
只负责推理端点选择，reasoning 和 isolation 也是独立路由轴。批处理 worker 继续走
`RuntimeId`/scheduler，不因本 SPEC 自动获得 Pi 路由。

`conversationRuntime` 是新运行时的唯一结构化入口；`runtimeOptions.pi` 保存 Pi 原生
选项，禁止继续向 `AgentConfig`/`SpawnConfig` 增加 `piFoo` 散字段。旧的
`runtime`、`conversationRuntimePreference`、`ccModel` 等路径保持兼容，不伪造迁移。

`@anchor symbol ConversationRuntimeRoute`
`@anchor symbol ConversationRuntimeSessionBinding`
`@anchor symbol resolveConversationRuntimePreference`
`@anchor test Relay/tests/conversation-runtime.test.ts :: "keeps structured Pi routing opt-in and outside automatic fallback chains"`

## 2. 会话绑定与 turn 单赢家

每次 turn 生成一个稳定 identity，同时作为 `correlationId` 与 `idempotencyKey`；同一
turn 如在既有三路运行时之间安全降级，必须复用 identity。每 tenant 同时只允许一个
active turn。恢复时若持久化 `runtimeSessionBinding.runtime` 与选中 runtime 不一致，
必须 fail closed；不得把 Codex thread、CC session 或 Pi session 路径互相冒充。

Pi RPC 的完成真值是 `agent_settled`，不是可能被 retry、compaction 或 queued follow-up
继续推进的 `agent_end`。恢复/切换必须使用 Pi 原生 `get_state`、`switch_session`，并验证
native session ID 与 session path containment。

`@anchor symbol createConversationRuntimeManager`
`@anchor symbol createPiConversationRuntime`
`@anchor test Relay/tests/conversation-runtime.test.ts :: "reuses one turn identity when a read-only turn is replayed to a fallback runtime"`
`@anchor test Relay/tests/conversation-runtime.test.ts :: "rejects a second concurrent turn until the first has a result"`
`@anchor test Relay/tests/pi-conversation-runtime.test.ts :: "waits for agent_settled, suppresses unclassified deltas, and reports usage deltas without recounting"`

## 3. 副作用与不可重放

turn effect 单调升级：`no-effect → read-only-effect → side-effect-observed`。只读工具
限定为 `read/grep/find/ls/glob`；未知工具、write、shell、network 与外部 API 一律视为
可能产生副作用。观察到副作用后，任何 runtime error/rate limit/crash 都不得把原消息
自动重放给另一 runtime；Relay 进入 `runtime-recovery-required`，携带去敏后的 correlation、
runtime、effect 和原因，由用户可见路径明确提示人工恢复。

Pi 不参加任何自动 fallback 链：结构化 Pi 路由只能得到 `[pi]`，既不能从 Pi 自动退到
其他 runtime，也不能把 Pi 放进其他 runtime 的 fallback 列表。

`@anchor symbol fallbackActiveTurn`
`@anchor symbol effectForTool`
`@anchor test Relay/tests/conversation-runtime.test.ts :: "blocks recovery instead of replaying after an observed side effect"`

## 4. Pi RPC 协议

Pi 版本与源码 commit 由节点的受审 runtime manifest 声明，而非编译进 Relay；当前升级候选为
`0.87.1` / `f07218c4d4bbc12bef056a7058c3dd49dfe41abe`。Relay 校验 manifest
的版本/提交号形态、宿主可执行文件实际版本或隔离镜像提交号；变更 manifest 仍须经过
受控发布与回退。subprocess 使用 `--mode rpc`，stdout
协议是**只按 LF 分隔**的 JSONL；Unicode line separator 不是记录边界，畸形或未终止
记录立即杀进程并报协议错误。每个 command 都有唯一 request ID、超时和 response
correlation。

必须实现：`prompt`、`abort`、`get_state`、`get_session_stats`、`switch_session`、
`get_available_models`、`set_model`、`get_available_thinking_levels`、`set_thinking_level`、
`compact`。未分类的 `thinking_*`、`text_delta` 和原始协议帧不直接跨越
runtime/channel 边界。完成的 assistant 公开文本必须进入正确出口：原生
`textSignature` 标为 commentary，或同条消息附有工具调用时，立即作为过程说明；
普通文本先保留为候选，后续工具或 assistant 消息出现时转为过程说明，不得丢弃。
`agent_settled` 后仅最后一份合格候选作为唯一 final，不能拼接此前的过程说明。

原生公开思考摘要也属于工作过程。已锁定 Responses API 的 `thinkingSignature`
同时携带摘要与私有回放数据，只取 `summary[]` 中 `type=summary_text` 项的 `.text`；
不回退读取 `thinking`、原始 `content` 或 `encrypted_content`。同一消息内的摘要、
commentary 和最终文本按内容块分类，避免摘要漏显或混入最终答案。完整来源、TG/飞书
展开方式及容量规则见[通道渲染契约](channel-rendering-contract.md)。accepted 状态与
精确工具步骤计数不变，过程说明不增加计数。Pi 的
session stats 是累计值，Relay 只记录相邻快照的非负 delta，避免 resume/restart 重计费。
native trace 只保留事件类型、command、success、toolName、correlation，不记录 prompt、
delta、stderr 或 credential value。

`@anchor test Relay/tests/pi-conversation-runtime.test.ts :: "preserves public assistant text before a tool and emits the settled tail as the sole final"`

官方契约来源：

- [Pi v0.87.1 release](https://github.com/earendil-works/pi/releases/tag/v0.87.1)
- [Pi RPC protocol](https://github.com/earendil-works/pi/blob/v0.87.1/packages/coding-agent/docs/rpc.md)

`@anchor symbol validateManifest`
`@anchor symbol verifyHostPiVersion`
`@anchor symbol consumeStdout`
`@anchor symbol finishSettled`
`@anchor test Relay/tests/pi-conversation-runtime.test.ts :: "fails closed on malformed or unterminated non-LF protocol records"`

## 5. 宿主一等运行时、隔离与供给链

Pi 官方不内置 sandbox，project trust 也不是 sandbox；这不把 Pi 降格为 container-only
runtime。`host-native` 是首选的 resident profile：它直接启动版本固定的 Pi binary，并在
task context 通过 `ConversationSpawnConfig.workspace` 交付的实际 workspace 中运行，使用该
workspace 的真实工具链与适用的 `AGENTS.md` / `CLAUDE.md`。它与 CC、Codex、CC SDK 一样
获得真实 workspace，而不是一个 minimal internal-canary 目录或被裁剪的工具 PATH。

host-native 的 `computeCostHost.python` 使用业务账户唯一环境的绝对 `bin/python`，
Pi 计量与 CC 会话 WI MCP（会话归属登记接口）共享此入口。发布器核对实际
`sys.executable` 和 `sys.prefix`，将已知系统 Python 入口迁入标准环境；未知入口拒绝。
资源白名单仅补入标准环境目录与实际解释器所在目录，其余配置保留。
配置仍沿原发布锁、候选校验、摘要核对、原子备份与回退流程激活；采用须读回新消费者进程。

无 WI 绑定时，Relay 验证实际 workspace 位于 profile 的 `workspaceRoots` containment allowlist；manifest
不选择、预建或强制某个 checkout/worktree。普通对话仍使用已授权 workspace；WI 工作必须消费
Harness allocation 与 registration，不能自行选择新目录。Pi 模块本身不拥有目录分配权。

有 WI 绑定的 host-native 启动统一消费 Harness `managed_workspaces/v1`。在 planning 前，sealed
client 用当前 Host surface 与 static workspace 仅定位出的 repository slug 调用
`resolve-managed-workspace-local`，选择 exact WI/owner/surface/repository registration；static
Agent workspace 和当前目录都不得进入 admission 或 spawn。随后
`admit-runtime-workspace-local` 只读证明 exact WI/owner/session/epoch、当前 fence、同一登记摘要与
resolver-returned worktree realpath；live target 以 v2 readback 再交回 workspace 与 digest。
无论路径是否在普通白名单中都须验证。既有登记可以精确准入旧路径，不能据此放行父目录或兄弟目录，
也不增加逐 WI 的 manifest 条目。新目录由 Harness allocation 分配，登记与回收遵循 Harness
`specs/managed-workspaces.md`；missing、ambiguous、drift 或错误证明在 planning/spawn 前拒绝。

同一 durable target 的重试与同一计量区间的 reinit 必须复用原 usage binding（包括 issued_at 和 digest），并验证全部身份
字段不变。缺少或不同 live digest 的同 Session idle target 可以受控重启后重验 baseline；busy
target 或身份变化保持拒绝。不得重新签发绑定后复用持旧摘要的进程，也不得跳过首提示前校验。

若旧消费者已经覆盖了有效的 v3 binding，Host 不能猜测旧 issued_at 或重建历史签名。
`CanonicalMeasurementBaselineReference` 单独引用 canonical SCL 已接受的 open baseline；
当前进程仍携带独立有效的 v3 binding。引用同时保留当前 binding digest、原 baseline binding
digest、baseline digest 和 measurement epoch，并精确匹配 WI、Agent、原生 Session、epoch、phase
与 surface。Pi 必须通过只读 `describe-measurement-local` 核对当前权威事实，并验证原私有文件、
原生历史前缀及累计边界。已 sealed、被撤销或身份冲突的区间不能重新打开；原 baseline 与账本
不可改写。后续 progress/final 沿同一原始计量边界继续，不把进程恢复当作新计量段。
已封账源的退休恢复仍可幂等读回原 baseline/final 与 canonical seal；消费者保留 sealed 标记，
不能派发业务或 compact，也不能把这类退休读回当作正式工作接回成功。

从隔离恢复进程接回正式能力时，即使当前 binding digest 相同也必须重新准入并验证实际消费者。
恢复事务覆盖进程启动、身份校验及 baseline 激活；任一步失败，在原人类边界仍有效且会话空闲时
恢复已获准的隔离通道。新输入、Stop、换会话或忙碌进程不能被补偿覆盖。只有实际正式进程与
计量读回一致才可报告接回成功。

`@anchor symbol measurementBaselineReference`
`@anchor test Relay/tests/canonical-measurement-adoption.test.ts :: "reuses the exact valid v3 binding for same epoch/session/phase/surface"`
`@anchor test Relay/tests/canonical-measurement-adoption.test.ts :: "does not revive an old escape after Stop during baseline verification"`

恢复进程使用 pending entry 的 exact launchWorkspace；首次结果落定后将该路径保存在 active runtime
binding。控制恢复、飞书与 Telegram 的普通后续消息均复用该路径并重新接受 Harness 准入，不退回
Agent 默认 workspace。记录路径只负责定位，不自行签发目录或执行权限。

独立 kernel 的显式 continuation 若首先读到 unbound target，不得调用普通 orient/bootstrap。Relay
只可委托 Host-owned dispatcher 执行 target descriptor、source seal、mode-0600 digested transport 与
target import，并要求 readback 证明 source/target 均无 execution authority、exact target epoch/surface/
workspace/registration 与 capsule/descriptor digest。随后重新 resolve registration，才可计划 target；
claim、v2 process readback 与 apply 在 canonical `continue-local` 内完成。任何 transport/admission/
readback/apply 失败均不得 dispatch target business prompt；同 surface 和已 applied stale recovery 不走
该独立 kernel 分支。

WI-bound host-native 的 reviewed runtime context 必须携带当前 managed_workspaces 规则和 allocation
入口，不依赖旧 checkout 内 CLAUDE.md 已更新。该提示只传递规则；容量、登记、运行与回收仍由各自
机器 gate 校验，不能把提示当作 sandbox 或 authority receipt。

`container`、`vm`、`policy-sandbox` 是显式可选的 isolation profile，不能因隔离不可用
而静默替代或冒充 `host-native`。反之，已选择的外部隔离 profile 仍不得回退到宿主进程。
manifest 必须精确选择 profile；unknown、缺失、路径逃逸、Pi pin 不一致或 profile/preflight
失败一律 fail closed。

host-native 仍不是“继承整个宿主”：manifest 必须指定绝对 Pi binary、版本 pin、workspace
allowlist、节点 `nodeResourceRoots`、tenant 私有 session/agent directory、显式 toolchain paths
与环境 allowlist。Pi binary/toolchain、共享 agent config、reviewed extensions/skills、固定 context
files 与 ComputeCost host 都是节点 capability，必须在 realpath 后位于 `nodeResourceRoots`；不允许
用文件系统根目录作为 node root，也不允许用 tenant workspace containment 冒充节点资源边界。
tenant workspace、session、HOME、tool/file event 则仍按 tenant 独立约束。Pi 的
认证来自该 allowlist、预配的 tenant-private agent directory，或已受审的原生 provider 绑定。
Pi 的租户 HOME 保持隔离。不得复用 Relay 服务的完整 HOME，也不得把所有宿主凭据注入 Pi。
不可变 production release checkout 不能充当
Agent writable workspace；Relay 必须从 task context 交付另一个满足 allowlist 的实际 workspace。

Oracle 的原生 Host policy 将 Pi 的 `openai-codex` provider 绑定到 `pulse` 账户已有的
Codex ChatGPT 登录。Pi 每次请求读取当前 access token，凭据留在该账户的 Codex 文件与进程内存。
即将到期时通过标准 Codex App Server 的 `account/read` 触发受管续期，先取得同一账户的本地锁，
然后重新读取 Codex 写回的文件。原生 launcher 为该节点未指定模型的交互及请求补全受审默认模型。
Oracle 的原生 launcher 和 Relay 受管 Pi profile 为 `openai-codex` 使用同一登录。
Relay 仅从该节点发布生成的 profile 启用此绑定。Oracle 原会话的只读对话恢复也使用该
provider 扩展，所有工作区工具和 WI 操作保持禁用。其他节点沿用各自登记的认证来源。
只有待启用的原生核心与实际选中的 Relay 发布包均提供该扩展时，才编译此绑定。
恢复或 fork 原生 Pi 会话时保留其模型选择，刷新入口使用同一 SSOT Node 和 Codex launcher。
`@anchor test Scripts/tests/test_pi_codex_oauth.py :: test_pi_delegates_one_expired_refresh_to_codex_account_read`

配置的 host-native extensions/skills 在 Relay readiness 前必须通过与 Pi launch 相同的
reviewed-resource realpath 校验，即使该实例不要求 WI-bound ComputeCost，也不能跳过。
启动 Pi 时重新解析，防止预检后软链接改变目标；缺失和越界资源不能到第一条业务消息时才被发现。
Relay 自有的动态 SCL intake 扩展仍由原注入边界限定；外部隔离 profile 继续按实际 tenant
workspace 检查，不能借启动预检扩大权限。

对 `container` profile，仓内 launcher 强制：digest-pinned image、OCI revision label 等于上述 commit、
容器内 `pi --version` 等于 pin、非 root uid:gid、read-only rootfs、drop all capabilities、
no-new-privileges、资源上限、显式 network、每 tenant 独立 workspace/session mount。
环境只透传 manifest allowlist；extensions/skills 默认全部关闭，仅显式 reviewed path
以只读 mount 开启（路径必须位于 tenant workspace）；context files 默认关闭，仅显式
source 注入，且 reviewed context 不得承载 secret。容器 uid:gid 必须配置为 Relay 宿主
用户的非 root uid:gid，保证 tenant 目录可写而不扩大宿主权限。
launcher 必须把 tenant-scoped `HOME` 显式传入容器，同时用 manifest 固定的 engine
host home 查找 Podman/Docker 镜像；两者不得复用，否则隔离后的 HOME 会让宿主已固定
的镜像表现为不存在。

所有 profile 的工作区必须位于 profile allowlist root；session dir 使用 tenant label + tenant hash，权限
0700，session switch 不得越出该 tenant dir。tool file event 只允许显式
本会话 outbox `{workspace}/.relay/outbox/{session}/` 中的顶层、非隐藏常规文件：普通 `write/edit` 工作区路径不是用户投递意图；
Pi 必须等 `agent_settled` 后统一扫描一次并去重，不能在每个 tool execution 结束时投递中间版本。
频道消费端必须以 tenant + workspace 重新验证 lexical/real path，仅成功投递后删除已验证的 outbox 文件，
永远不得上传或删除 workspace 源码、evidence 或 read-only tool 的输入。

官方安全来源：

- [Pi security model](https://github.com/earendil-works/pi/blob/v0.84.2/packages/coding-agent/docs/security.md)
- [Pi containerization guidance](https://github.com/earendil-works/pi/blob/v0.84.2/packages/coding-agent/docs/containerization.md)

`@anchor symbol buildPiCommand`
`@anchor symbol validateProfile`
`@anchor test Relay/tests/pi-conversation-runtime.test.ts :: "runs a pinned host-native Pi with the reviewed workspace contract and an explicit toolchain"`
`@anchor test Relay/tests/pi-conversation-runtime.test.ts :: "takes a host-native workspace from task context without requiring a prebuilt worktree"`
`@anchor test Relay/tests/pi-conversation-runtime.test.ts :: "emits effects without treating ordinary workspace writes as delivery intent"`
`@anchor test Relay/tests/pi-conversation-runtime.test.ts :: "scans the explicit outbox once after settlement and de-duplicates intermediate edits"`

## 6. 节点能力、计量与退出

TCloud 将 Pi 作为节点级可选 runtime capability 提供给该节点上的全部 Agent。节点通过
`conversationRuntimeDefaults.pi` 一次性配置 provider/model/reasoning/isolationProfile；任一 Agent
显式执行 `/runtime pi` 时，Relay 才把该受审默认值物化为该 Agent 的结构化 route。该能力不是逐
Agent canary 名单，也不改变 Agent 的既有默认 runtime，不进入自动 fallback，不在用户未选择时启动
Pi。Agent 后续通过 `/model`、`/think` 修改的 route 继续按 Agent 独立持久化。

节点默认 Pi profile 是受审的 `host-native` resident runtime；实际 workspace 由 task context 提供，
Agent 自主决定是否使用 worktree。profile 可用一个共同 containment root 覆盖节点 workspace；
`workspaceContextFileNames` 只沿匹配 root 到当前 Agent workspace 的目录链加载同名启动契约，禁止
读取 sibling workspace 的上下文。节点安装的 reviewed extension/skill/ComputeCost host 与任何具体
Agent workspace 解耦，因此 Aki、Hestia 或后来显式 `/runtime pi` 的 Agent 都消费同一份受审节点资源，
同时保留各自 workspace/session/HOME。container profile 只在明确选择它时使用。不修改 runtime-priority
v1；不把 Pi 设为缺省或 fallback。能力入口通过
`RELAY_PI_RUNTIME_MANIFEST_PATH` 指向受审 manifest；变量缺失时现有三路运行时应完全
不受影响，Pi spawn 则 fail closed。

退出顺序：停止 Pi 流量 → abort/drain active turn → SIGTERM/宽限/SIGKILL → 删除
节点 Pi default、agent 的 Pi route 与 manifest 环境变量。session/evidence 留存，不把别的 runtime 绑定
写进 Pi state。提升默认路由另需显式人类授权、版本/隔离/capability manifest、rollback
演练和真实已接受结果。

`@anchor symbol loadPiRuntimeManifest`
`@anchor symbol buildRelayInteractiveLineage`
`@anchor test Relay/tests/pi-conversation-runtime.test.ts :: "runs the Aki and Hestia topology with reviewed node resources outside the tenant workspace"`
`@anchor test Relay/tests/pi-conversation-runtime.test.ts :: "resolves node resources before containment and rejects symlink escape"`
`@anchor test Relay/tests/relay-interactive-lineage.test.ts :: "uses runtime-compatible domains and never returns raw producer ids"`

## 7. 验收矩阵

合并前必须通过：`/new`、`/restart` continuation、`/stop` native abort、
`/runtime` structured-route persistence、`/model`/`/models` native discovery/hot-switch、`/think`、
`/compact`、`/context`、crash、并发/duplicate、LF protocol、
`agent_end`/`agent_settled`、streaming、path escape、恶意/缺失 extension、secret
redaction、provider/model/reasoning、累计 usage delta、三路 legacy regression、spec-drift。飞书流式卡片
必须在 throttle window 内完整累积每个 Unicode delta，最终卡片文本必须等于 Pi
`message_end` 权威文本，“能回复但只剩标点/换行”不算通过。

上线后必须完成真实 Telegram 与飞书 UAT：至少覆盖原 Pi Agent 与一个此前没有 per-Agent Pi route
的 TCloud Agent，并验证新会话、重启续聊、取消、file/tool event、metering lineage、用户可见故障。
单条 smoke 不能证明验收；两渠道 receipt、session binding 与计量原始行必须互相对应。

Telegram 与飞书的 `/model`、`/think` 必须提供各自原生的图形化选项，而不是只返回一段待复制的命令文本。
Telegram callback data 只携带有界、短期、tenant/chat/agent scoped 的 opaque token；完整 model id 只留在
当前 Relay 内存中的 option state，callback 一次消费后重放必须失效。Relay 重启或 token 过期后按钮应明确
提示用户重新打开 `/model` 或 `/think`，不得猜测或执行旧选项。

## Recovery execution lifetime

Sealed `reinit-local` has a 600-second execution budget, separate from the
30-second read budget. It may perform canonical admission, endpoint convergence,
and worktree registration before returning. The controller serializes recovery
and entry within a conversation and reports active control work to restart
protection even while the Agent runtime is idle. The existing same-conversation
runtime activation retry stays inside this one controller operation.

A recovery transport deadline yields `recovery_outcome_unknown`: it does not
prove no mutation. The user presentation retains the original WI/request and
requires readback of durable steps before recovery. It does not claim a
background retry has been scheduled.

Waiting startup envelopes may carry a null ResumePacket and a durable startup failure. The sealed client preserves that failure and the original start request instead of reporting invalid_response. An unresolved effect remains mutation=unknown until its transaction is observed.

### 当前会话 checkpoint 恢复

`/wi checkpoint` 负责从当前会话生成语义草稿并保存，不只返回 orientation。若当前 Host Session 的 fresh sealed plan 已证明执行权，且 managed workspace admission 已验证目录、登记摘要与 surface，Relay 可以恢复缺失的同会话 usage binding，然后按原 session 恢复进程并验证绑定。不得覆盖有效冲突绑定、采用未知 pending target 或中断 busy runtime；旧 pending 证据保留。durable binding 不冒充 live readback。

模型返回失败结果时，草稿等待器立即失败并保留错误类别，不等待 timeout、不调用 checkpoint 保存。安全拒绝不提示机械重试；没有保存结果不得显示 checkpoint saved。


## 计量接线：原生adapter与Host入口

`createPiConversationRuntime`是底层适配器，不解释独立Host计量身份。生产、诊断和
原生验收需要计量时，注册到既有`createConversationRuntimeManager`并使用它的
spawn/send/kill入口；`PULSE_COMPUTECOST_RECEIPT_DIR`为已登记私有Host留痕位置。
从第一笔真实模型调用开始保留`meteringAgentId`与可选`meteringWorkItemId`，不等待
交接/写权/Token基线，也不解析prompt作为身份。它们只是会计用途；不会注入SCL权限。
实际computeCostBinding存在时优先覆盖计量提示。不能同时在adapter和manager重复捕获。
回归`Relay/tests/compute-cost-integration.test.ts`用无provider的adapter消息，通过实际
Host/native JSONL、原collector、ledger、query和Stats projection，核对重复/非WI/早期
用途；不以合成测试证明任何真实业务已执行。
