# WI300 — 用量身份与业务归属设计

状态：现行归属实现与生产消费路径已采用；最近验收基线及其限制见[Stats 已证基线](stats-query-dashboard.md#已证基线与边界)。本文件规定证据和标签语义，不是实时状态表，不重复创建 WI、身份名册、账本或计价器。

## 1. 五个身份维度与一个业务去向

PULSE 是平台，Pulse 是 canonical ID 为 `pulse` 的 Agent。Local Pulse 不是另一身份。相同 Agent 可跨节点/渠道/模型；只按所选结构化维度分组，不能由显示昵称、Local/TCloud 前缀或目录子串猜身份。

| 维度 | 证据与约束 |
| --- | --- |
| Agent | Host 冻结路由身份，或原生启动工作区与既有 workspace/surface 登记精确匹配；不是采集者身份。 |
| 节点 | 已登记的实际执行节点；远端日志集中到 ownermac 不改变原节点。 |
| Runtime | 原生用量协议及启动记录；Codex/Pi/CC 与模型、入口不同。 |
| 模型 | 本笔调用及其原生上下文；压缩/分支总结的模型状态必须有证据，不能套配置默认值。 |
| 入口 | 触发本次消费的 Host/原生输入：Codex App、飞书、Telegram 等；确认没有交互入口时显示“无”。 |
| WI 业务去向 | 独立任务绑定，或有证据的非 WI 类型；不因某身份维度已知就猜 WI。 |

Relay 的渠道 alias 与计量 Agent 分开：Host 使用已加载路由中显式 `harnessAgentId`，缺少映射时保留原 id，不从 `tcloud-` 等前缀猜主身。历史修正只使用私人来源登记中的 alias、principal、有效时间和配置摘要；早于已证边界的记录不倒灌。修正沿原 ledger 追加 executor-only revision，Token、WI、模型、价格与旧 watermark 不变。

重复采集必须把已知原始版本视为历史祖先，不能反向覆盖已修正身份。新观察在同一 collector 边界使用同一映射，再由原有生命周期/数量校验决定能否接续；不得把旧 accepted 状态重放成与 completed 并列的新分支。回归覆盖冷启动、旧账迟到修正、writer 升级和连续三次重新打开 collector。

### 原生绑定接续与请求用途

Pi 延续同一 canonical measurement baseline 时，原生捕获回执中的 `binding_sha256` 与 `active_runtime_binding_sha256` 是不同的绑定引用。只有同一原生 session、WI、Agent、epoch、phase、surface，且回执点名的已接受 snapshot 或既有核验事实、原/新 baseline 摘要、源文件前缀全部可复核，ComputeCost 才采用这条直接对应关系。相同 Token 或相同 WI/Agent 单独不能证明绑定等价；竞争对应关系、强归属相异、数量或前缀不一致仍拒绝核销。

Host 的精确请求清单和四类 Token 守恒先证明物理用量是否重复；仅由 session 时间窗产生的纯 `ambiguous` 推断不得阻断这一步。一个确证 Host dispatch 可恢复其请求用途，独立人工裁定或另一个真实任务主张不被覆盖。对应关系只服务计量核销，不改变执行权、SCL 时间边界或原用量。

历史请求的启动、接续、退休和收尾可能横跨执行权绑定边界；执行权取得时刻不等同业务用途起点。已有 `reviewed-activity.json` 可按原始请求或正式交接凭据，逐笔更正这类纯弱推断：必须同时固定原 usage fact、旧 attribution fact、完整用量区间及证据摘要；在旧推断的同一 revision 链追加更正，不改 Token、不覆盖独立 Actor 身份、不生成未来 session/WI 默认规则。缺少原证据或出现竞争的新裁定时不应用。旧水位仍可复算，重复采集不得恢复已更正祖先。


各维度可独立未知；已知 Agent 不能因 WI 未关联而被抹掉。分别输出 Agent 未识别、WI 未关联及相应记录数/已计价金额，不用混合粒度条数比例代替金额覆盖。
工作区是识别依据而非新增展示列。启动 cwd 与执行中切目录分开；linked worktree 必须由相同 Git common directory 和真实 worktree 登记证明，相似路径/拷贝 URL 不能证明身份。

Pi与Codex都使用已登记启动工作区规则：节点、Runtime、精确cwd及真实linked worktree关系分别核验。Actor匹配在原生解析缓存之外，每轮重读登记，不因复用解析结果而漏掉新身份依据；匹配只补Agent，不生成WI或入口。
绕过Host的独立诊断若没有派发绑定，须以正式Actions的固定代码/产物与精确native消费对应，再通过既有 `reviewed-activity.json` 追加非WI/Actor证据。#591修复的独立Pi连通性测试显示“Pulse／WI无／入口无”，不能因控制脚本目录含wi184而记入WI184，也不能为未来调用设置该默认。

同一 usage/执行边界可能被 Host 与 native SCL reader 独立观察。**证据份数本身不是冲突**：若 WI、Agent角色、state、work kind 和 effective boundary 一致，且 optional execution/assignment 引用至多只有一个非空值，查询层按字段补全为同一归属并保留全部来源引用。两个已知 WI/Agent/assignment 不同、state 不同或边界不同仍 fail closed 为冲突；不得按“最后写入”获胜。该合并只发生在读侧，不改历史 fact、水位或授权。

受管 Pi runtime 的 unattended: true 是另一类正证据：只有当 session 文件确实位于 runtime manifest 明确选中的 sessionRoot、当前输入没有任何 Host human-input claim，且没有已验证渠道时，入口可记为 automation（UI 显示“无”）。若多个明确选中的 profile 共享同一 sessionRoot，则必须全部声明 unattended=true 才可使用这条证据；任一 profile 可交互或 roots 不一致都保持未知。普通 Pi/TUI、交互 profile、Host marker 缺失/损坏、来源不匹配均继续保持未知；不能从 WI、Agent、目录名或“没看到渠道”反推“无”。manifest 内容参与解析缓存签名，修改 profile 会使旧缓存失效。

## 2. 原生 session 到现有 SCL 绑定

`receipt_sources.py` 使用原生稳定 session/thread ID；`scl_session_attribution.py` 计算对应 Host 引用，再读取明确登记的 kernel 与 `active-lineage.json`。不读取旧 service-repositories execution 来冒充活动状态，不实例化会初始化/迁移数据库的生命周期控制器。
Codex 复用 hook 的 `opaque_host_session_ref(..., adapter="codex")` 规则，即 `scl-codex-session-v1` 域加 NUL 分隔；Pi 使用其既有引用规则。ComputeCost 的 `native_scope_ref` 不直接等于 SCL 的 Host ref，二者由同一原生 ID 精确连接。
物理 rollout 换段或归档不必然换 thread；下划线后的物理 UUID 不得当成新 session。先处理父子/归档复制和累计起点，再把原生用量的完整区间关联到 attachment/history 的有效区间。
同一只读数据库快照中读取 `source_attachments`、`source_attachment_history`、epoch 计量/checkpoint 的历史 Agent 证据，保留 rollover、同 epoch 换 Host、终态与同 session 换任务边界。不能把最后一条当前绑定倒灌整个会话。
唯一且覆盖完整区间时追加原格式 attribution；跨边界或多 WI 重叠保持不确定，不按时间均摊。绑定根不可读时报告 `scl_session_binding_unavailable`，保留原数量和独立 Agent 证据；不回退旧根或补默认 WI。
独立的受委派执行不必替换主会话attachment。当前登记kernel中的measurement_epochs及
measurement_native_facts也是权威身份来源：在同一SQLite只读快照校验原身份摘要、WI、
Host、surface、运行类别、原生快照及有效时间窗。SCL的resident_harness按其原协议对应Pi，
不把当前WI主执行者或主epoch套给受委派者。已有open进度足够，不要求最终seal或人审。
只有收到有效原生计量窗的部分进入归属；baseline前热身、没有计量证据、窗外、错误来源和
冲突继续保留，不能用源码目录或当前授权补填历史。历史归属更正沿原账本追加，原用量/
模型/价格和旧水位不改。此次暴露的边界与候选回放见[2026-09-19复查](evidence/stats-attribution-regression-20260919.md)。

归属是对消费的解释，不是执行授权；不要求封存 Token、完整 WI 计量或人审结束才允许正确记账。

## 3. 输入凭据、飞书与 Telegram

已认证 Host dispatch 的输入关联不以 WI 或 SCL assignment 成功为前提。采集在既有 execution 记录中保存同一 attempt 的不透明 human_ref 和输入时间；旧 reader 仍只看到同一个 execution。新 reader 核验原输入、事件摘要、路由字段和时间后提供独立 Actor/入口证据；缺少任务绑定不能抹掉已知身份，也不能由此生成 WI、授权或默认非 WI 分类。

复用 `pi_context.py` 的 `parentId` 祖先链、精确 Host 输入引用及已保存渠道元数据。对每次触发输入核验来源、正文/包装边界与 session 关系；新输入缺证据时不得沿用上一条的渠道。
运行环境必须传递 `human_input_directory` 及明确的历史目录，Host/Pi 读取使用同一定位。ownermac 已采用 `PULSE/data/agent-harness/scl-host/human-inputs` 与 `PULSE/data/agent-harness/scl-kernel`；以运行登记确定绝对根，不用旧 CWA 目录兜底。两云过渡状态以各自实际登记为准，不借文档更新迁库或声称所有目录已经完成迁移。
Host 输入脚注只提取末尾稳定的引用标记，说明文字不是协议；必须继续核验原输入哈希、正文、tenant 和时间。回归直接读取 Relay 生产脚注，不能仅复制旧说明作为 fixture。
飞书与 Telegram 都必须分别验证；同 Agent、同目录可能有不同渠道，当前配置不是历史证据。SCL 合成接续文本不能当作新的真人输入，缺脚注也不能凭文本猜渠道。
`SCL_HOST_RECOVERY_RESULT` 明确属于 Host 控制结果，不是新的人类入口边界。它只能在祖先上下文已有精确验证的 `host_input_token` 时延续渠道，且正文携带的 `human_input_ref` 必须与该 token 完全相同，并再次通过已登记 input store 的哈希、tenant 与时间校验；不同 token、缺失/损坏凭据或未知父渠道都保持未知，也不得因此归成 `automation`。若真人消息复制这段文字，Relay 追加的外层 Host footer 仍以新的真实 human input 为准，不能借 recovery 文本继承旧渠道。
对既有未带脚注的飞书输入，只有早于该输入的已登记 Relay Host 元数据、`user:agent:feishu` 专用池与 safeTenantDirectory 精确匹配，才可独立证明路由；裸池、相邻时间、提示词自称或不同渠道反证不成立。该路由证据不冒充人类输入凭据，不授予 WI/执行权。

## 4. WI、cron、无与未关联

| WI 栏 | 存储与证据 | 是否计 WI 未关联 |
| --- | --- | --- |
| `wi_…` | 有效范围内的真实任务绑定；定时触发有 WI 时也显示该 WI。 | 否 |
| `cron` | 原始 wi 仍 null，精确 attribution 的 work_kind=cron；有明确任务/调度或人审证据。 | 否 |
| 自动任务：<任务名> | Relay 调度运行的会话记录（`scheduled_job.v1`），任务未配置 WI；键为 `cron:<任务 ID>`，计为 cron。 | 否 |
| `无` | 原始 wi 仍 null，work_kind=none；已确认普通非 WI 工作。 | 否 |
| `未关联` / `待核验` | 没有足够证据、跨界或冲突；不能用 none 覆盖。`ambiguous` 不能仅凭标签判作相互矛盾的证据。 | 是 |

标签仍计入总量、金额和筛选。cron 执行失败不改变其用途，也不能删除已经发生的消费；无 WI 的 session 是正常业务形态。
已确认的历史裁定保存在 `ComputeCost/reviewed-activity.json`，绑定精确 usage 和证据摘要；不是按日期、目录、session 或当前 Agent 为未来消费设默认映射。
普通非 WI 自动分类要求 Host 对该次 execution 明确为空 WI/assignment、有稳定执行者和可用状态，再由原生完整前缀、同 session/区间和四类 Token 守恒核销。裸原生日志 WI 为空、缺 Host/库存或存在 WI/执行冲突均不足以分类。
入口“无”独立于 WI 栏：内部 `automation` 来源编码显示为“无”，WI 仍可为 cron；普通非 WI 聊天可显示“WI 无 / 入口飞书”。未知入口不得显示为无，“无”筛选不包含未知。
新 non-WI 凭据仍遵循 `required_receipt_features` 读取兼容检查；所有需要的 reader 支持后才启用，保留旧水位与原始字段。

### 声明优先的归属补充

WI 归属以声明为准：请求或会话的 WI 声明、Host 分配和已审非 WI 分类都是主张。某笔用量没有主张，或只有 `unassigned` 结论时，查询才采用两类补充；Agent 自己声明的 `none`（请求用途或会话 WI 声明）只表示未声明 WI，持有补充或阶段成员关系指名 WI 时以该 WI 为准，已审分类和自动任务标签不在此列。补充在行级 `attribution.sources` 中与声明分开计数（`declared`、`holder_supplement`、`scheduled_job`、`classified`、`undeclared`、`unattributed`）。`undeclared` 指人类请求本身没有任何 WI 声明、Agent 已知的用量，显示“未声明 WI”，金额照计，不算归属异常；`unattributed` 指有主张却判不出 WI（冲突、来源缺失）的用量。

- 持有补充（`holder_supplement.v1`）：会话自己向内核写过哪些 WI。CC 与 Pi 按请求判断，请求从主会话一条人类输入起，到下一条人类输入止；子代理输入、压缩摘要、任务完成通知和中断标记不开启新请求。请求期间该会话只写过一个 WI 记该 WI，写过多个记 `ambiguous`，未写不补充。Codex 按整会话判断：会话只写过一个 WI，全部用量记该 WI。
- 自动任务（`scheduled_job.v1`）：Relay 调度运行的会话。任务配置了 `wi` 时记该 WI，否则记“自动任务：<任务名>”。该会话中调度开始后、没有已证渠道的用量，入口记 `automation`（显示“无”）。

同一用量有多个补充时依次取：任务配置的 WI、唯一持有 WI、任务名、多 WI 持有。补充的生效时间须不晚于用量边界；同级补充互不一致时不决定归属。补充在 Host/原生用量核销之后才套用，核销只比较原始主张；补充不改写原始用量、声明或已审分类，之后到达的声明照常优先；没有补充事实的旧账本查询结果不变。

## 5. 数量、来源与在途边界

修身份不改 usage、Token、模型或价格。若发现父子复制、累计差分等数量错误，必须另外保留明确数量修正与守恒证据，不能仅补标签宣称统计正确。
来源 ID 必须区分节点；历史同名 Host/Harness 来源依据保留的明确来源身份核销，不把来源撞名传播成全局 WI 执行冲突。哈希证明内容完整，不代替来源权威。
Host 期望数与实收数不一致时，accepted/running 的有界待回报可以是正常在途，而不是读取错误。按 `host_usage_coverage` 的原分母、状态和捕获缺口范围分类；partial 与完整成本限制不变。真实终态缺用量、unknown、坏文件和断线仍报错。
早于待回报执行的历史 gap 不推断为未来消费遗漏；这些 gap 仍保存并影响完整覆盖，不能凭“读取错误=0”宣布历史已修复。判断规则与[仪表盘验收](stats-query-dashboard.md#验证与发布门)一致。

## 6. 价表与统计口径

价表不是归属证据。默认 WI 与 Stats 使用 `catalog_source.py` 的同一优先级与校验；显式快照错误不能悄悄借托管价表。模型不同按原模型计价，不为匹配用户预期改模型标签。
缺价不删 Token，零数量不补未知；四类明确为零可得零标准 Token 金额，但不修补身份、来源或完整 WI 成本。标准估值不等于订阅/API 实际支出。
Actor/task 归属与来源核销每轮读取；缓存仅复用内容和依赖不变的解释。补上迟到 WI/渠道后必须更新对应分组、保留旧水位，不重复计价既有数量。

## 7. 验证证据与测试入口

从原生文件/实际形状的 SCL/Host 记录开始走 collector → ledger → query → publisher → App，不能预先在测试准备里给出归属答案。正反例覆盖工作区相似/冲突、切目录、切模型、跨任务/跨回合、父历史/归档、渠道切换/缺凭据、迁移、新输入及迟到绑定。
关键回归位于 `test_scl_session_attribution.py`、`test_actor_attribution.py`、渠道/非 WI/数量核销及默认价表相关测试；具体文件以 `ComputeCost/tests/` 为准。真实数据回放由 `ComputeCost/uat/weekly_reconciliation.py` 和 `Stats/exporter/tests/verify_installed_dashboard.py` 分别验证候选与生产。
用户最初的三条核验路径为 Pulse/ownermac/Codex/WI282、Pulse/TCloud/Pi/WI184、Aki/TCloud/Pi/WI520；只是历史核验集合，不是每天必须活动或产品默认映射。Aki 原生模型与早期预期不同须保留原事实，不填成 gpt-6-astra。
本次发布门同时覆盖北京时间“今日”和“本周”的已登记实际用量，两窗口在同一发布水位独立校验并分别报告；未活动的 Agent 不要求出现。任一窗口的身份缺失、未计价、数量冲突或真实来源读取错误都不得通过，诊断使用完整未计价数量，不以有界样本长度代替。月度历史、SCL 质量及完整 WI ROI 暂缓。最新归属修正及两次自然发布、App重启的逐项结果和原捕获限制仅在[已证基线](stats-query-dashboard.md#已证基线与边界)维护。不是全入口或所有历史的完整覆盖声明。


## 8. 集成接线补全（PR #635）

SCL受委派的原生计量可以不占父WI的主attachment。只读适配器将其已保存的Host、
Agent、WI和已接受snapshot摘要接回现有Pi边界连接器；继续验证原生前缀、精确请求
分片、模型和互斥Token，不以时间邻近代替已计量请求，不新增计量账本或SCL授权。

付费邀请/独立诊断须经现有ConversationRuntimeManager与Host捕获。`meteringAgentId`
和可选`meteringWorkItemId`是发起时已知的计量上下文，不是computeCostBinding或执行权；
真实绑定存在时优先，不从prompt/环境/目录猜。原生去重后保留其已证用途，不要求先有
受邀者的写权或基线。底层Pi adapter不单独负责Host捕获，不能直接调用后期待配置字段
自动持久化；维护中的原生验收脚本复用同一manager，不把捕获下沉造成双计。

物理采集节点与所打开的逻辑surface工作区分开。同一已登记PULSE根下的明确surface
目录可提供既有Agent规则，仍要求精确目录或Git真实linked-worktree关系；不匹配子目录
或改变实际node/kernel。受管来源计划同步已启用读者的规则，不因此启用新来源或覆盖
已有不同Agent的工作区所有权。历史控制器的有限遗漏通过原reviewed-activity补充精确
fact_ref与固定Actions/原journal/原始内容依据；不为未来同session请求设置默认归属。

源路径接管还必须保全已有身份上下文：runtime manifest selector重建不得只留下路径，
丢掉actor_attribution、SCL kernel/surface、人类输入目录、workspace规则或source元数据。
新旧来源同一ID时更新selector并保留非路径上下文；若多个根的身份上下文不同，需明确
核对而不能合并后任取一份。已有读者的常规重装不得把已启用归属悄悄变回未启用。


### 混合计量版本的读取边界

既有 kernel 可同时包含有效 `pi_session_usage_snapshot/v1` 与有精确原生边界的 v2。
受委派原生连接器先复用原 validator 校验摘要与形状；合法 v1 不提供 v2 请求级身份，
也不能因缺少 v2 字段而阻断同源有效 v2。损坏、未知版本、身份冲突仍为真实缺口。
此规则不升级历史为精确计量，不改 Token/价格，不用时间相邻或原任务 owner 补归属。


同一 Pi session id 可以对应多份实际登记文件。Snapshot 必须匹配本文件的 baseline/final 两个前缀；不匹配时，只有同一来源登记中的另一文件同时匹配两份前缀和 session 身份，才判定该 snapshot 不属于本文件。未登记副本、不同 session 或丢失最后一个匹配原件仍报错；不按文件名、日期或长度挑赢家。前缀 inventory 按来源/session/文件独立定址，保留旧水位证明，防止两份文件在重复采集时反复覆盖同一个 inventory head；用量仍按原生请求身份去重。


### Codex 子代理入口与历史原请求校准（2026-09-23）

子代理的 native `id` 是自身身份，`session_id` 可能是父线程，二者不能互换。
入口仅沿已解析且无歧义的 native parent 链继承；父线程缺失、两种 parent 声明矛盾、启动身份冲突或循环时仍为 unknown。
这条链只证明触发入口，不继承父线程当前 WI、Agent、权限、模型、数量或价格。直接来源与子线程来源事实分别保留。

送验前对 waterline 559 中 23 条用量补充原请求用途：WI282 11 条、WI825 7 条、WI018 5 条。
每一条由原始用量 fact_ref、原人类请求摘要、native 父子关系（若适用）及 canonical WI 定义摘要绑定。
它们是有限历史会计用途校准，不是新的人类批准、execution grant 或今后同一会话的默认归属；执行者身份事实独立保留。
其余缺少唯一 WI 用途凭据的记录继续未关联，不能仅因没有 SCL 绑定就改成已确认无 WI。

### CC 子代理与压缩后的入口继承

CC 子代理（`subagents/agent-*.jsonl`，`isSidechain`）首条输入和压缩摘要（`isCompactSummary`）都带发起它的请求的 `promptId`，都不是新请求。入口沿该 `promptId` 回到主会话唯一一条非压缩、非中断标记的输入，再按主会话规则继续：遇到后台任务完成通知则回到启动该任务的请求，直到已证的人类输入。任意层子代理都回到同一条主会话输入；同一 `promptId` 有多条主输入、父输入晚于子输入或链断裂时保持未知。压缩摘要本身不能作为输入凭据，即使正文复制了原输入脚注。请求用途（WI）与持有补充按同一请求边界取值，子代理用量随发起请求得到入口、Relay 租户与请求 WI。

祖先链按 `parentUuid` 走，父记录须在同一文件中先于子记录。CC 原生写法有三处例外，入口与请求用途共用：

- 分叉子代理的文件以父代理记录的副本开头，副本改写为分叉自己的 `agentId`。记录身份不含 `agentId`，副本不算冲突记录；子代理链可跨入本文件中先于它的副本。副本缺失或内容改动时不成立。
- 子代理的上下文附件先于它所指的父附件写入；附件指向附件时，父记录时间不晚于子记录即可。
- 后台任务以原生结果中的任务 ID 为准：超时后被移到后台的 shell 没有 `run_in_background` 输入，只有 `backgroundTaskId`；被终止的任务以 `killed` 状态完成。以 worktree 隔离启动的子代理，完成通知末尾另有 `<worktree>`（绝对路径与分支），不参与路由。

Relay 自己投进会话的协作收件箱合同不是人类输入：Relay 在正文末尾加一次性投递引用，并在 Host 回执中记下该引用与原生会话的绑定（[Relay 4c](../Relay/specs/relay-scheduler-jobs.md#4c-协作收件箱投递的入口记录)）。同一会话恰有一条输入带此引用、且记录不晚于该输入时，这一请求入口为 `automation`（显示“无”）；缺记录、跨会话或引用重复仍为未知。

CC 会话计数器的残差是两次计数器之间的增量，入口与节点只由该区间内的逐条请求判定；区间内没有逐条请求时才看整组。

CC 用 Bash 前台启动的 Codex/CC/Pi 子会话，执行者取发起请求的用量已证执行者；该用量的 Host 与 transcript 两个版本都算发起用量，二者矛盾则不取。命令可在 CLI 前带若干 `&&` 步骤，每步只能是字面绝对目录的 `cd` 或只读的 `git log`／`rev-parse`／`status`。

原生输入凭据要求 Relay 已认证派发（input→execution→原生会话）点名执行者。Relay 续轮会以同一输入再次派发，晚于原生输入记录；只要全部派发不早于输入、且原始派发不晚于原生输入，续轮不否定原输入路由。来源登记中的 Relay 别名历史在输入时刻覆盖该路由别名时必须与之一致；别名在登记之后才出现（如 `bb-hestia`、`bb-duke`、`scherzoo`）时没有历史条目，不构成相反证据，执行者取 Host 派发所点名者。路由池格式错误仍不成立。

@anchor impl ComputeCost/pulse_compute_cost/cc_input_context.py :: cc_input_actor_receipts
@anchor test ComputeCost/tests/test_cc_input_context.py :: test_subagent_launched_by_a_task_completion_inherits_the_original_input
@anchor test ComputeCost/tests/test_cc_input_context.py :: test_compaction_summary_continues_its_request_for_main_and_subagent
@anchor test ComputeCost/tests/test_cc_input_context.py :: test_route_alias_without_history_uses_the_host_named_executor
@anchor test ComputeCost/tests/test_cc_input_context.py :: test_subagent_out_of_order_context_attachment_and_fork_copy_keep_the_route
@anchor test ComputeCost/tests/test_cc_input_context.py :: test_relay_continuation_redispatch_keeps_the_original_input_route
@anchor test ComputeCost/tests/test_cc_request_purpose.py :: RequestPurposeTests.test_forked_subagent_keeps_its_parent_agents_request
@anchor test ComputeCost/tests/test_cc_input_context.py :: test_completion_of_a_worktree_isolated_subagent_keeps_the_original_input
@anchor test ComputeCost/tests/test_cc_input_context.py :: test_relay_delivered_turn_has_no_interactive_entrypoint
@anchor test ComputeCost/tests/test_cc_session_counters.py :: ResidualFamilyContextTests.test_an_earlier_intervals_route_is_no_evidence_for_a_later_residual
@anchor test ComputeCost/tests/test_runtime_subcalls_cc.py :: CcParentLaunchTests.test_host_and_transcript_versions_of_the_launch_both_name_the_parent


### Missing Host measurement is not a conflicting counter

A Relay Pi Host turn with no normalized counters, no raw counters, no reported total and no explicit cover list asserts missing measurement, not a second numeric usage. Retain that observation and its unknown/unpriced diagnostics; do not suppress it as a proven duplicate, propagate its WI/actor, or quarantine independently measured native requests merely because their session scope overlaps. Partial or zero numeric counters, explicit covers, and actual contradictions still require the existing conservation proof and fail closed when it is absent.


### 当前缺口的证据恢复

原生启动工作区的 Agent 归属读取显式登记的团队 `ssot/registry.yaml`，不能把 Pulse 专用 surface 清单当作全团队清单。`workspace_registry_path` 经正常 source prepare/select 的原始摘要比较与回滚流程登记；不存在该配置时不搜索目录或猜 owner。后续激活保留既有可信工作区规则，所有权矛盾拒绝，不以新规则悄悄覆盖。采纳时同时记下团队登记文件的 SHA-256（`workspace_registry_sha256`）。

Oracle 的来源计划（`Stats/deploy.py prepare-sources --site oracle-node-1`）给 relay_host 与 Relay Pi（runtime.pi）来源加上与 TCloud 相同的归属字段：`actor_attribution`、`host_native_input`、`human_input_directory`，Relay Pi 另有 `scl_surface_id` 与 `workspace_actors`；relay_host 的 `principal_alias_history` 按运行中 Relay 的 `relay.json` 补齐全部 Agent 别名。`human_input_directory` 取运行中 Host 的输入目录（`SCL_HUMAN_DECISION_KEY_FILE` 同级的 `human-inputs`），之前登记的目录移入 `human_input_directories` 继续读取，做法与 `activate_actor_sources` 相同。团队登记正本写的是作者机器的 `repo_root`（`/Users/owner/Coevolution`），本机读自己的检出副本，所以用 `config/scl-surfaces.yaml` 该站点部署项的 `team_registry_root`（`{repo_root, checkout_root}`，Oracle 为 `/home/pulse/Coevolution`）显式映射。未另行指定时，来源计划取 `checkout_root/ssot/registry.yaml`，即 Oracle 发布器读取的同一份，并把映射记为来源登记的 `workspace_registry_root`。读取时，正本的 `repo_root` 必须等于映射的 `repo_root`，文件必须位于 `checkout_root/ssot/registry.yaml`，工作区路径按 `checkout_root` 解析；正本字节不改，摘要按原字节计。不符则拒绝，不放宽校验。映射只跟登记的检出路径走：显式改用其他路径（`--workspace-registry`）时，来源登记去掉 `workspace_registry_root`，按文件自身的 `repo_root` 校验；再指回登记的检出路径时恢复映射。采纳摘要随每次采纳重写，没有团队登记就不留摘要。这样 Oracle 上 `owner_aki-*` 等租户目录下的 Pi 会话经 Host 原输入的路由池归到对应 Agent；别名历史缺该 Agent 时保持未归属。

`@anchor test Stats/exporter/tests/test_stats_deploy.py :: SourcePlanRetryTests.test_oracle_host_and_relay_pi_get_the_tcloud_actor_wiring`
`@anchor test ComputeCost/tests/test_oracle_pi_actor.py :: OraclePiActorTests.test_pi_session_in_its_tenant_directory_resolves_to_its_agent`
`@anchor test ComputeCost/tests/test_oracle_pi_actor.py :: OraclePiActorTests.test_source_plan_moves_to_the_running_host_input_directory`
`@anchor test Stats/exporter/tests/test_stats_deploy.py :: SourcePlanRetryTests.test_team_registry_of_another_root_is_refused`
`@anchor test Stats/exporter/tests/test_stats_deploy.py :: SourcePlanRetryTests.test_explicit_registry_path_replaces_an_adopted_mapping`

工作区改名或团队登记变更后，已采纳的规则要等重新采纳才更新，期间新路径的用量没有 Agent。发布器每轮用 `workspace_actor_drift` 检查已开启归属的 codex/pi/cc 来源，结果写入 `last-refresh.json` 的 `workspace_actor_drift`（只报告，不阻断发布）：`workspace_registry_changed`（登记文件摘要与已采纳的不同）、`workspace_registry_unadopted`（采纳时未记摘要）、`workspace_root_missing`（登记的工作区根目录在磁盘上不存在）、`workspace_rule_unadopted`（登记的工作区未进入该来源的规则）、`workspace_registry_unreadable`。重新采纳后，旧路径规则照旧保留，新路径归到对应 Agent，漂移清零。

`@anchor test ComputeCost/tests/test_workspace_actor_drift.py :: WorkspaceActorDriftTests.test_renamed_workspace_is_drift_until_adopted_again`

Pi 的原生请求计数和模型证据不依赖 SCL 计量 baseline/seal 是否可用。原生前后计数有效且守恒时，dialogue-only、pending、sealed 都保留真实用量；不因此授予执行或 seal 权限。原生计数缺失、倒退、模型分项不守恒时仍保持 unknown。

自动价格目录每个正常发布周期获取一次，不因未满 24 小时继续漏计新模型。显式固定目录和历史查询不刷新；获取失败保留旧字节及原获取时间，不伪装新鲜或补价格。

Host 恢复结果若引用独立的新输入，必须凭原 Host 派发记录核验 request、pool、native session、输入引用、dispatched 状态和实际控制结果；不要求它等于上一条聊天输入，也不只凭命令文本推断渠道。该派发凭据纳入 native parser cache 依赖。

### Host/native 原输入核销（WI300）

`host_native_input.v1` 在全部 reader 兼容后启用。actor context 可携带原输入元数据摘要 `input_ref`；Pi `turn_final` 仅表示其原生 `stopReason=stop`，不是推定用量或执行授权。Host 端须通过原始 human_assignment event/assignment 双摘要及输入时间验证；原生端须通过原输入正文、租户和 parent ancestry 验证。只有同节点、同入口、同原输入、唯一 Host attempt、唯一原生 session、正确时间边界及无相反归属才能核销。时间不能单独建立连接，重放同输入到多个 attempt 不择优猜测。

已终态 Host 缺回报而原生完整 Token 仍在时，现有来源覆盖分母不变，按已证原生调用核销回报；在途请求和采集缺口仍保留。只有完整物理 prefix、同一输入的全部调用和原生最终 stop 都存在，且 Host 已 completed 时，才允许将全空 Host 汇总作为已覆盖占位；有已知相反数值、缺模型/Token、末尾非 stop、缺输入凭据均不适用。只投影归属与排重，不修改原始用量、补零或补默认模型，旧水位不重算。

恢复派发从既有 pulse_feishu_host_recovery/v1 journal 读取 request/session，并与 Host 已记录的 dispatch_ref 严格匹配，再与原生父链共享同一 input metadata 摘要。不得要求恢复消息伪装成新真人输入，不按 WI/时间相近猜关联，不创建新派发或回填旧记录；缺 journal、非 dispatched、错 session/dispatch、同 dispatch 多 attempt、相反原始输入仍保留未知。


## 全平台租户展示（tenant_context.v1）

全平台保留全部已登记活动。租户与 Agent 绑定，按两级来源确定：

1. 会话绑定（`session`）：Relay 已认证渠道输入（`tenant_relay_input.v1`）或下述显式成员声明证明的租户，优先。
2. Agent 登记（`registry`）：用量的实际 Agent 已确定时，团队登记表 `ssot/registry.yaml`（来源登记的 `workspace_registry_path`）中该 Agent 的 `tenant` 给出租户；取值必须是 `owners` 的键。

两者不同时保留会话租户，并在分组 `tenant.registry_conflicts` 列出登记租户，App 显示“与登记冲突”，不以登记覆盖会话。Agent 为 `unassigned`/`ambiguous` 时不推断租户。poolKey、工作区路径、WI owner、渠道默认与账号凭据都不能提供租户。登记租户在查询时补全，不写账本，不改用量、WI、Agent 或金额；`binding_ref` 只由 Agent 与租户这一对决定，登记表其他编辑不改变引用，任何租户绑定变化使缓存的租户视图重算。登记表不可读时会话租户照常发布，登记租户保持未知。

@anchor impl ComputeCost/pulse_compute_cost/agent_tenants.py :: apply_agent_tenants
@anchor test ComputeCost/tests/test_agent_tenants.py :: AgentTenantTests.test_session_tenant_takes_precedence_and_a_differing_registry_is_a_conflict

显式成员声明是单独登记的 `pulse_stats_tenant_registry/v1` 私有声明：公共安全
tenant_id/name、Agent、实际 node/runtime、opaque native_scope_ref 和有效半开区间。
声明不授予 WI 或执行权限；实际演员不匹配或用量区间不能被完整证明时不成立。
声明文件为 owned/mode0600 regular file，封闭字段排除完整路由、chat/session/body/credential；
canonical 声明和条目分别生成 opaque registry_ref/binding_ref。每条membership必须保存已验证
usage_fact_ref，只能命名同一usage版本；新的累计范围、native scope或runtime版本必须重证并追加，
旧水位与原始facts不变。实际Agent/node歧义或与membership不一致时，保留binding引用并显示待核验。
已知native metering逐通知验证first <= at < last；粗区间必须证明全部范围在声明内，结束边界
相等不足以排除末时刻用量，保持未知。多个不同声明保留并显示待核验。

`tenant_candidate_registry`/`source_registration --tenant-registry` 只创建独立候选源登记。
reader-first 兼容检查必须覆盖所有登记reader，再开启required_receipt_features；不得在旧reader
上写新metadata，不自动更改生产metadata/采集，也不默认开启。新事实只有actor context，不改
原usage数量、WI、模型或定价。已有actor声明不能覆盖独立task/quantity冲突。

新增可选 tenant 联合分组：新App接受精确七维键 tenant/agent/wi/entrypoint/node/runtime/model，
仍接受原精确六维键。不同租户同名Agent的行标识独立，未经登记的租户显示未知；旧标量
active_agents不足以证实跨租户同名principal数量时计数保持未提供，不能把两者当一个。
发布开关tenant_identity默认为false；正式CLI仅在reader能力已登记时启用七维发布。
三视图摘要成本与用量保持同一原账本查询，不从行表重新计价；Outcome责任者不推定租户。
Python exporter同样只接受正式六维或租户七维合同，保留已发布的早期查询字段版本；七维必须携带
与筛选一致的population_scope，租户必须有匹配id、可选安全name和不重复的binding_refs，
可成对携带有序的sources与registry_conflicts（冲突只随会话来源出现，不含本行租户）；
正式声明歧义携带ambiguous/null name及原引用，名称缺失保持待核验，不删除用量或成本。
原始发布夹具通过Python snapshot附加、Swift解码、原始字节缓存与三个期间的原生表格回归；
第八维、未知合同字段、不一致水位、非法身份和数值继续拒绝，不重新投影归属或补全未知成本。

@anchor impl ComputeCost/pulse_compute_cost/tenant_context.py :: collect_tenant_context
@anchor test ComputeCost/tests/test_tenant_context.py :: TenantContextTests.test_same_named_agent_in_two_tenants_has_distinct_joint_keys_and_conserved_totals
@anchor test ComputeCost/tests/test_tenant_context.py :: TenantContextTests.test_only_exact_declared_actor_node_runtime_scope_and_window_match
