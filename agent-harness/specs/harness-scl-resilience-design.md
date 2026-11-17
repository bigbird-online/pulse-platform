---
feature_name: "harness-scl-resilience"
version: "1.2.14"
status: "authorization_closure_redesign_pending"
work_item: "wi_20260827_282"
owner: "Pulse"
created: "2026-08-29T00:00:00+08:00"
tags: [design, scl, local-first, journal, reconciler]
---

# SCL Local-First Resilience — Design

## Business continuity and correction architecture

The following architecture adopts the reviewed PR377 design at 4c60763c6a55f42279bd79f4812194393448454c. It replaces conflicting recovery and authorization prerequisites in the older sections below. Implementation and adoption remain pending; proposed commands are not installed capabilities. No historical receipt or currently unknown external effect is rewritten.

## 0. 最高原则：权限可交接，人类可重新授权

本节优先于本文其他章节的旧绑定、归属、执行代际和恢复前置条件。SCL 不是业务授权的最终决定者：人类明确决定及其授予的可转授权限是权限来源；SCL 的登记和 capability 是对这些决定的记录与执行投影，不是另一次审批。

### 0.1 已有权限包含范围内的交接权

Agent 已获 WI 及关联 worktree（任务工作目录）的权限时，默认有权通过 HO（跨 Agent 交接）等明确方式，把自己持有的全部或部分权限交给其他 Agent；不逐次重新请求人类批准，除非原授权明确禁止转授。交接不能扩大原授权，也不靠传递密码或复制 session/fence 字符串实现。

交接内容必须能回答：同一 WI、交给谁、哪些目录和动作、接管还是协作、何时生效。Host/产品从已有上下文补齐机器字段，不让 Agent 编造完整证明包。默认转移指定范围的执行权；业务目标、验收标准和结果责任不随一条通知暗中改变，明确的责任交接按原授权处理。

接收者接受后，应直接得到完成受托工作所需的实际入口、目录及能力；不能先要求它已是 owner/executor，或先拥有被转交的 worktree 权限。HO 的送达、接受、权限落实和实际执行分别如实记录；仅 ACK 或消息送达不算权限交接完成。同机接管优先保留原目录、分支和未提交成果，不强制另建 WI、清空工作树或重走首次准入。跨机由适配器保留成果并映射实际目录，不假装远端已有同一路径。

独占接管生效后，原 Agent 不再使用已交出的写权限；并行协作则按声明的资源范围隔离，不因两名 Agent 参与就冻结整个 WI。接收者拒绝或尚未接受时，不把交接报成功，也不无故撤销原 Agent 的权限。登记漂移、发布等待、计量或旧状态机不支持交接，只能触发提示、记录与修复，不能否决已有授权内的交接；普通路径失效时仍能通过人类通道裁决并落实。

同机共享 worktree 的 HO 不迁移资源：接收者的 `surface_id` / runtime 标识执行入口，既有 `resource_surface_id` 仍定位原工作树登记。接受后的落实、运行时准入、受管执行和再次转授均消费同一原登记；不新建目标登记、不覆盖目标 surface 原有目录。每个调用者仍按自己的 Session、动作和资源范围校验。登记锁释放后才落实接受的资源；忙碌或未就绪时续办原操作，不重新授权。

交接后的旧会话释放与 WI 终态分开：旧会话的 `/new`、`/reset` 通过现有
`plan-entry-local --conversation-epoch` 只读核对其历史 Host、Agent、runtime、epoch 与原 surface。
确认没有尚存或待落实的资源范围、且接收端已接管后，只归档 Relay 的原会话绑定，沿用普通会话重置；
原 WI、接收端授权、检查点及工作树不变。旧计量绑定缺少 surface 时从同一历史来源核对，
不借用接收端或进程默认身份。证据未知、仍持部分范围或并发变化时保留绑定；不要求已转出的 WI
先进入终态，也不让旧会话重新 rollover/reinit。

### 0.2 缺少权限时，reinit 请求权始终存在

任何 Agent 遇到缺权、归属冲突、退休绑定或损坏的控制状态，都有权通过 reinit 请求人类授权，包括没有 WI 执行权、没有 worktree 权限、不是 owner、旧会话已退休的调用者。请求不需要先证明旧绑定有效、取得旧执行者同意、跑完自动恢复或提交完整重建草稿。

**请求授权不等于已经获权。** Agent 说明要做什么、缺少什么权限、涉及哪个 WI/资源以及已知影响；SCL 可以补充冲突与风险，不能过滤或拒绝把这项请求交给人类。人类尚未同意时，不执行依赖新增权限的动作；拒绝后保留原有无关能力，不自动反复索权。

人类可以用正常对话给出明确决定，无须特殊口令、手抄内部 ID 或再签一份机器证明。实际用户决定及其来源被记录；Agent 自填 `human_confirmed=true`、历史摘要或第三方文本不能冒充新的授权。授权后，产品直接按该决定落实指定权限、交接或必要的前沿重建，不再用刚被授权替换的旧 owner、executor、epoch、HO/ACK 或登记缺失拒绝，也不再次询问同一决定。

reinit 是人类重新授权入口，不等于每次都清空重建整个 WI。能够只修正委托、资源归属或执行绑定，就只做这一项；确需重建时保留原 WI、成果和已知历史，未知历史保持未知。退休会话可凭新的明确授权获得新权限，但不是让旧凭据重新有效；人类有权决定新执行者，不必等已经消失的旧执行者回来批准。

### 0.3 逃生通道不得依赖要修复的控制面

reinit 的请求/展示阶段必须在旧执行准入、完整草稿校验及活动内核初始化之前可达。解析失败、内核打不开、登记损坏或该渠道不认识命令时，原用户消息仍进入正常会话，Agent 直接说明所需授权；不能先修好 SCL 才能请求修好 SCL。

已有 HO、用户会话、GitHub 决定等可用载体保存授权来源；SCL 恢复后补录并关联原决定，不重放已完成动作，不另建长期授权数据库。记录失败只能如实报告记录未完成，不能撤销已明确作出的业务授权。整个通信或存储都不可用时，不谎称请求已送达、记录已持久化或授权已执行。

SCL 对有效交接和人类决定只作提示、记录及执行协调，不保留“旧状态机不同意”的否决权。真实文件系统拒绝、外部服务权限、进程仍在写同一资源或不可撤回的外部请求，不会因一句授权而物理消失：产品应按决定自动办理实际授权、停止自己管理的旧写入者或隔离资源，只报告具体尚未落实的动作，不把机器故障重新解释成人类无权决定。保存历史、拒绝假凭据和不虚报完成，是事实完整性，不是对上述决定另设审批。

#### 原生输入漏接与最早存储故障

原生 Codex 的真实用户输入必须先保存到既有 Host 私有目录，再解析接收者、登记和业务账本。
同一 native Session/turn/原文产生同一决定身份，重复 Hook、丢响应和后续读取不能随机生成另一份授权。
接收者来自当前 Host 与明确仓库路由，不以待替换的旧 WI owner/executor 作为输入捕获的先决条件；
路由暂不可读时保存原输入并续办，不能撤销人类决定或要求重发。

Hook 未执行时，公开 reinit-local 在读取/初始化生命周期内核之前，从当前 native Host 的只读用户事件
恢复同一输入。不得接收 Agent 提供的 transcript、消息正文或他人的 Session 来签发决定；原生导出的
schema 不是接口已实施的证据。原生 reader 使用实际可运行的只读协议，保留原事件时间，拒绝助手文本、
不完整用户内容、跨会话借用和更新的 Stop/替代决定；正常接续不要求人类重新批准。临时 native reader
不 resume/fork 业务会话、不启动模型、不改 Hook 信任。

活动内核无法打开，或正常 reinit 已保存授权但旧登记仍使资源落实失败时，已验证的原生决定自动进入已有 Host 隔离恢复机制，在原生 Host 验真的非共享工作树
恢复 read/write/test。原库和业务文件保留，checkpoint、全局排他性和 publication 分别保持真实状态；
原地执行沿用可验证的原 active kernel 资源锁域，不能在外层根新建锁来写旧树。原 pointer 损坏、共享
primary、资源被占用或源 Git 失效时，自动调用已有独立资源恢复：从真实 Git 观察或保全的精确登记确认来源，
在新的物理 Git/锁域继承可读文件并通过受管消费者检查；不夺取旧锁，不丢弃坏控制原件。原请求重试复用已落实
副本及新成果，不重复分配。无法确认来源、真实外部权限或尚未获准撤回冻结/终态候选时，记录具体条件，不把
机器故障重新解释为需要同一授权。真实用户的更新决定仍约束后续隔离 launcher，不因已有恢复回执永久授权。

`@anchor pytest agent-harness/tests/test_codex_reinit_ingress.py :: 原生漏 Hook、输入先持久化、旧 owner、坏库、原请求重试与实际受管读写`

### 0.4 统一授权落实契约

接续是进入当前内核所决定的工作模式。`observe_candidate`、`observe_terminal` 不需要执行 grant 或工作树启动器即可完成；导入失败原因必须跨最终只读回读保留，不得包装成自动等待执行权。已拥有内核的历史导入直接读取内核封存的资源身份，跳过旧执行绑定与登记投影；若已批准候选的生产者树已清理，可从登记的共享仓库读取固定提交，验证原提交事件、Outcome 和最终批准后接纳。外部发布已发出的原请求绑定投递目标；补齐业务策略不能重算该请求目标，Broker 与前端使用同一投递选择边界。

一次性业务导入直接返回内核的原提交凭据，无变更时返回空结果。接续响应保留该凭据；只因最终工作模式为观察，不能把已提交的导入报告成只读。若同次接续还落实其它操作，导入凭据与主操作分别保留，不从修订号或派生文件推测操作身份。

`@anchor test agent-harness/tests/test_scl_client.py :: test_continue_observation_does_not_require_execution_or_drop_import_failure`
`@anchor test agent-harness/tests/test_scl_business_import.py :: test_old_kernel_freeze_keeps_original_outcome_and_final_human_review`
`@anchor test agent-harness/tests/test_candidate_remote_audit_recovery.py :: test_original_candidate_recovers_unadopted_remote_audit`

**不能用“内核有执行者”代替“执行入口已采用有效授权”。** owner（结果责任人）、被授予的动作范围、
当前运行会话、机器资源是否就绪是四个不同事实。全局 `active_executor` 和单个
`execution_delegate_ref` 只能表达兼容摘要，不能继续充当所有资源、动作及授权变更的唯一判据。

在现有内核、HandoffContract 和操作日志中追加授权来源与范围，不建立第二套授权数据库：

- 来源：真实人类决定，或持权者的明确委托；关联原会话/消息或原交接，不以 Agent 自填布尔值证明授权。
- 范围：原 WI、接收者、仓库/目录/资源、动作、独占接管或并行协作，以及原授权的转授限制。
- 顺序：父授权、接受/撤回/替代关系及生效版本；不能按消息到达时间让晚到旧决定覆盖新决定。
- 落实：每项资源的旧写入停止/隔离、实际路径映射、登记、调用者绑定及消费者读回。决定已成立与落实未完成分开记录。

普通执行统一调用同一个授权解析与校验接口，输入当前调用者、动作和资源，输出授权来源、有效范围、
当前写入版本及尚未落实的机器条件。每个消费者只增加自己的物理检查，不再另外比较旧 owner/executor。
授权变更使用独立入口：检查人类决定或授予方所持范围及接收方接受，不能先检查接收方已有待授权限。
结果责任、验收决定和对外发送权限不会随普通执行委托暗中扩大。

| 消费者/当前缺口 | 必须替换的判据与行为 |
|---|---|
| `worktree.py` 的 `_registration_candidate`、首次登记、重登记、扩展登记 | 用当前资源授权创建/修复登记；旧 YAML 空 executor 不得否决；保留目录身份和他人文件检查 |
| `scl_workspace_extension.py`、`scl_lifecycle.py` 的目录采用与 Host 采用 | 委托接收者和人类新指定者按授权范围办理；缺登记是待落实步骤，不能要求先登记才能获得登记权 |
| `scl_handoff.py` 与普通 HO 收发/接受 | 同一交接落实动作与资源权限；支持范围内再转授及不重叠并行范围；接受前不要求已有 WI/目录执行权 |
| `scl_entry.py`、`execution_entry.py`、`workspace_policy.py`、checkpoint/replacement | 当前绑定是授权采用结果；只读、存档和各资源写入分别校验，不以责任人等同所有执行者 |
| Git guards、Broker、effect claim/send、驻留旧 worker | 动作发送前核对当前授权及相关资源写入版本；旧版本失效实际到达消费者，不只更新内核字段 |
| Relay/Hook、普通对话、独立恢复读取 | 缺权请求在旧准入和内核初始化前可达；保留决定来源，机器恢复后续办同一决定 |
| 遗留 YAML、登记缓存和只读快照 | 已迁移 WI 的派生展示，不参与重新审批；仅未切换安装中的未接管 WI 保留单一旧控制器，切换后原件只读导入 |

旧 execution controller 的全部写入区间共用退休边界，工作区入口的直接持久化和旧 continue/runtime-launch 决策同样适用。安装已切换或 WI 已被内核接管时返回 `legacy_execution_retired`，沿现有 SCL 入口操作；不比较、修补旧绑定，也不启动兼容恢复分支。旧成本写入口也共用该退休判定，不能凭旧绑定补写归因、用量或回执；新归因与原生用量走既有内核计量边界，历史成本原件继续可读。历史读取与一次性导入保留。公开 execution status 先读内核当前定义、绑定和终态；删除副本终态覆盖与 definition_alignment 字段对比，旧副本不能改变 active/terminal 或当前负责人。资源登记的真实身份核验仍独立保留。

三条路径共用上述契约：

1. **既有授权的投影修复。** 授权仍有效，只修复必要登记/绑定，保留 WI、进度、分支及未提交成果。
   不要求重新 claim、改 YAML 或再次向人类索权；损坏投影先留证再由产品受控重建。
2. **范围内交接。** 持权者明确授予 → 接收者接受 → 产品落实原目录或跨机映射、停止/隔离被转交范围的旧写入 →
   接收者实际执行。新范围之外、尚未接受或已撤销的授权不能使用；不重叠范围不冻结整个 WI。
3. **人类重新授权。** 任意调用者可请求 → 正常对话中的真实决定 → 追加新授权 → 最小范围落实 → 原业务继续。
   缺失/损坏的历史仍标未知；新决定无需旧 owner、旧会话或旧权限同意。只有确需重建前沿才进入完整重建模式。

写入排他性按资源范围落实；共享进度存档通过内核已有事务排序并保留各方贡献，不另设冻结整个 WI 的审批锁。
交接接受与旧动作发送有确定性排序。产品能管理的旧进程按已授权接管停止或隔离；不能控制的真实写入冲突、
文件系统拒绝与未决外部请求只影响相关动作。新授权不证明旧外部请求未执行，也不允许重发未知效果。

### 原生计量沿用当前执行者的授权

普通原生会话及恢复入口在尚未挂接计量 binding 时，同样传递已审核的私有状态定位与登记根。读取和保存已有 WI 不得以先建立计量绑定为条件；普通与已绑定子进程共用同一定位校验，不扩大资源范围、不补签授权、不传递人类密钥。

原生运行时启动及用量快照记录保留已登记的 PULSE 私有存储定位、工作树登记根和实际调用者 surface；这些定位不是授权，不携带人类密钥或无关环境。 PULSE 私有存储定位只解释已审核 SCL 状态根所在的私有命名空间，不能因此要求放行整个 PULSE 仓库；实际可执行文件、账本、kernel 与工作树登记根仍分别按原允许列表核验。接收者通过 HO 获得共享目录后，计量使用该实际 Session 的有效资源授权和执行代际，不把 WI 负责人或全局执行者当作所有协作者。计量身份只是读取投影，不修改 owner、WI 前沿或范围权限；已撤回、未就绪、已交回的范围不能借计量恢复权利。基线、原生任务和封存必须属于同一真实执行区间，旧主执行者的归属与历史保持不变。

### 0.5 reinit 的业务完成条件

reinit 先确定人类决定及需要落实的最小变更，再执行同一可恢复操作。输入修正、重试、进程重启与丢响应
不丢决定，不重复请示，也不重新执行已完成的业务步骤。完整重建与普通权限修正不能共用“每次清空前沿”的语义。

若登记缺失，产品根据已授权范围、可核实的仓库身份和原目录生成/恢复登记；不得依赖旧登记先通过审批。
路径缺失只补缺失的机器信息；确有资源归属冲突时只呈现那项需人类决定的差异。

响应分别给出请求、授权、落实和业务验证事实。`local_committed` 可以证明存档已落盘，但不得把
`reinit_degraded_ready`、executor 非空或生成 checkpoint 当作权限修复已交付。对于“恢复登记并继续”的请求，
只有当前接收者在原目录实际登记、编辑、测试、存档并通过相应 Git 写入检查，才能声明该请求修复成功。
尚未落实时保留授权与已完成步骤，由产品续办具体故障，不把旧投影拒绝改称缺少人类授权。

恢复完成以当前 Agent 实际继续业务为准。正常入口失败后，Host 从原会话和真实 Git 目录取得精确资源事实，
沿同一人类决定自动落实既有独立隔离入口；本地读、改、测与文件保存可先恢复，检查点、提交和发布分别读回。
保留全部业务数据及历史，失效的执行登记和环境可以受控重建。人类明确允许必要时创建合法 successor WI 时，
新 WI 必须继承完整成果与来源关系，不能把坏控制状态复制过去或把未决外部效果当作未发生。

普通受管进程从当前 grant、精确登记、Host 身份与资源锁取得准入，保留 canonical ledger 的读取定位；
只有真正的旧账本写入调用才要求映射工作树账本副本。恢复检查与日常命令共用正式 launcher，并核验真实子进程回执。

`scl_host_recovery_admission/v1` 把原人类回执原样嵌入 Host 签名的受限执行材料，单独保存实现证据。
Relay 重启后的原生会话启动必须能够消费同一隔离恢复证明：只准入已验真的原会话、原工作树和
Host 资源范围，不依赖损坏的 kernel 再次证明启动权。进程启动证明不授予业务写入；实际命令仍
逐次经过隔离 launcher 的动作校验及资源排他锁，不能借恢复关闭权限检查。无法读回 canonical
计量时保留原归因材料，但不能伪报计量已恢复。
正常通道接管先办理旧写入停止和资源锁握手，只有工作树落实及真实消费者执行验真后才完成接管。
任一阶段失败须撤回待接管状态，让原隔离通道可继续；不能同时关闭两条执行通道。
Host 已停止原会话的失败运行时后，使用 `restore-isolated-workspace-local` 和原已接受恢复证明，
在决定索引锁及同一资源锁内撤回该决定对应的接管 sidecar；正在写入的动作返回可重试冲突。
这一步不改 kernel、grant 或人类范围，重复请求幂等，更新决定、异会话及不同 grant 不可撤回。
下一次正常接续以 `admit-runtime-workspace-local --recovery-decision` 验证同一现存就绪 grant、
执行代际、会话、surface 和登记，在原锁内恢复接管并通过真实 launcher 消费者检查；即使原接管
sidecar 尚未生成也可办理。失败再次撤回，成功不重做 reinit、不新建 grant 或改变执行代际。
两种变更均返回 `local_committed`；普通工作区只读准入仍返回 `read_only`。
`@anchor pytest agent-harness/tests/test_scl_consumer_recovery.py :: 正常就绪后失败、原隔离执行、正常再接回以及忙碌和旧决定拒绝`
原回执和事件标识不变，自动范围仅为已核实的非共享工作树上的 read/write/test；正常恢复仍消费原决定。
重启后的维护入口只定位并续办已持久化的真实请求，经原渠道返回 Agent；更新的人类输入、会话变化和重复派发
均由原输入边界与持久请求进度约束，不能伪造一条新的用户消息。

### 0.6 原执行资源不可用时的独立资源恢复

原地修复和原地隔离均不能产生可执行消费者时，同一已验证的人类 reinit 决定必须升级到独立资源恢复，
不能只对同一失败路径无限重试。优先保留合法原 WI，在 Host 私有恢复目录分配新工作副本和独立锁域；
该动作不要求坏 active-lineage 指针、坏 kernel、旧 Git 元数据、原资源写锁或发布器先恢复。
共享 primary 只能作为保全来源，不能获得原地写入豁免；已有 writer 的锁不撤销、不偷取。
仓库来源和基线必须来自可验证的既有登记、不可变来源材料或正式 SSOT，不能猜测仓库地址。原 Git 元数据或
锁域失效时，新副本使用不同的物理 Git common-dir；新锁根来自已有 Host 安装绑定，绝不能用新锁域写旧树。

独立恢复是原业务授权在新本地资源上的实现，不是新的全局权限来源。Host 验证原回执、原渠道最新输入、
实际 native Session 与当前 WI，保存源资源和新资源的精确对应、原内容清单摘要及实现回执后签发派生材料。
只能准入 Host 分配并验真的非共享目标，动作限 read/write/test。原回执原样保存，不能改 WI、agent、
Session 或人类事件字段重新签成另一项授权；显式撤销、新决定、Stop、不同业务范围及已冻结候选不得借故障绕过。
新资源采用版本化派生证明和独立派生标识，不改写原有不可变 scope 或 decision_ref；先物化和验真，再签发目标
资源权限。普通后续消息使正在执行的尝试重新核对边界，不永久撤销已经落实的本地恢复能力。

资源保全必须保留原目录、原 Git/控制元数据、全部可读取业务文件、脏文件和未追踪成果，以及 checkpoint、
业务下一步、原会话历史和未决外部效果的来源引用。复制不得跟随符号链接扩张范围，不删除原资源、不覆盖已有
恢复目标，也不将坏控制文件投入新执行环境。可读内容以逐文件摘要及前后稳定性校验验真；不可读取的原件保留
为明确的待继承来源，不能伪称完整复制或把未知外部效果重新发送。新工作副本必须显式区分已继承内容与待继承内容。
逐文件前后摘要不能冒充整个忙碌来源的一致快照；无法确认全树稳定时，清单必须明确标记 partial 并保留原件定位。

恢复事务位于已有 Host 私有恢复状态目录，按原决定和会话生成持久幂等标识。物化、校验、签发、消费者切换
分别保存回执；崩溃后复用已完成资源，未知进度先核对，不再分配另一个副本。原会话仅在空闲且最新输入未变时
重建到新目录，原 native 历史和 WI 计量归因保持。切换之前及之后再次校验会话/输入边界；新消费者实际受管命令
通过后才可派发业务。失败不删除原资源，不声称恢复完成；该 bot 的恢复不能阻塞其他 bot。
旧 usage binding、measurement origin 和封账事实原样保留；工作区迁移是独立 Host 投影，不伪造新 canonical epoch。

新副本首先只承接本地工作。远端 push、发布、跨端权利交接及终态结算仍须各自正式授权和实际可用性读回。
正常控制路径恢复后，产品协调接回，保留新副本中的新成果，不把原资源旧快照覆盖回来。真正需要新 WI 时，
必须经正式 SSOT 创建/继承事务取得合法身份；没有离线身份签发契约时不得随机造 WI 或伪报发布成功，原 WI
独立副本仍继续本地工作。原 WI 的本地恢复不以新 WI 发布为前置条件。

安装态故障验收覆盖首次恢复即损坏 lineage 指针、只存在 primary、源 Git 元数据损坏、原锁被占用、脏文件与
未追踪成果、重启/丢响应幂等、最新输入/Stop 竞争和派生材料篡改。必须观察模型消费者在新副本实际读写测试，
同时证明原资源不变；单独生成 manifest 或返回 ready 不构成验收。

#### 原登记资源恢复后的自动成果对账与接回

公开接口的 `plan` 是准备事务，可在原证明及当前 Host 边界验真后继续原幂等 reinit，返回真实 `local_committed` 状态；只有 `readback` 是不打开或写入 canonical kernel 的只读阶段。

`canonical_sync_pending` 是有持久进度、失败原因与重试触发的待办，不是恢复流程的终点。Host 在启动读回、正常控制路径恢复或原会话空闲后，自动续办同一恢复事务的接回阶段；采用有界退避，锁忙或会话忙时等待下一次可用机会，不要求人类重新 reinit。不重放已派发的业务，不以旧人类输入替换当前消息边界。

本阶段只以原封存登记中、当前正式 SCL 授权仍允许执行的 canonical 工作树为接回目标。源指恢复副本，目标指原登记工作树；两者必须与原派生证明的来源映射相符。不能选择另一目录代替损坏目标，不能将 Host 私有空基线 Git 直接登记成正常发布仓库，不能增加仓库、替换 allocation、创建新 WI 或扩大原动作范围。目标 Git、登记、路径或权限仍不可用时，本地副本继续可用，接回记录给出实际阻碍；恢复后自动重试。

接回必须保留原人类决定、渠道与 pool、WI、native Session 和物理历史文件、已完成业务及未知外部效果。普通后续输入不撤销既有授权，但使进行中的维护尝试重新核对；Stop、撤销、新授权代际、不同 WI/session、候选冻结或业务范围变化按现有正式规则阻断，不凭旧回执夺回新会话。每个异步边界和实际文件写入、消费者切换前均复核当前边界与事务所有权。

1. **正式控制事实。** 先读回原决定是否已有 canonical reinit 提交。已有提交只用原幂等操作的正式落实/enter/plan/admit 路径补齐未完成步骤，不制造第二次授权或 epoch；尚未提交时，在原人类授权仍有效的前提下，用原决定、原 native 身份和保留的原启动事实办理正式 reinit/enter，保存其真实结果，再评估目标工作树。不得手写 executor、grant、登记、baseline 或 epoch。原权限只有 read/write/test 时，恢复不得自行增加 commit/publish。
2. **稳定的三方事实。** 对原继承清单 B、恢复副本当前数据 R、原登记目标当前数据 C 做逐路径比较；B 的哈希不能被当作完整 Git 提交或全树一致快照。捕获真实文件类型、内容、执行位、目录成员和精确 Git 身份；所有路径均受已验证 scope 限制，不跟随 symlink，不复制 Git/Host 控制目录。只对明确 inherited 的普通文件定义基线；未读、跳过或特殊文件保留逐路径未知状态。partial 是保全覆盖事实，不是全局否决条件；仅与本次变更路径或子树相交、且不能由可靠逐文件 B/R/C 判定的未知项阻断相应计划。未涉及业务变更的 `.git`/控制目录 preserved_reference 和 non-atomic snapshot 标记本身，不得永久阻止接回。
3. **变更规则。** R 等于 B 时保留 C；R 等于 C 时不重复写；R 改变且 C 仍等于 B 时可应用 R，包括已继承文件的明确删除。副本新增普通文件仅在目标不存在同路径且祖先类型安全时写入。两边同一文件发生不同变化、类型/执行位冲突、无法证明删除基线、与本次变更路径或子树相交的未读目录或其他不可判定继承，均不得被整树覆盖或推断为删除；保留两边与逐路径差异，维持本地工作。第一阶段不自动解决语义冲突。接回不能把原树旧版本覆盖到副本，也不能丢掉原树独有的新成果。
4. **成果事务。** 在现有 Host 恢复事务中持久化对账计划、原证明/manifest 摘要、原登记与权限版本、两个资源的前置摘要、逐路径前后内容引用及完成回执。先完整检查本次计划的全部冲突和相交未知项；无冲突且没有影响本次计划的不可判定项才应用。计划外未知项保留审计，不阻断已可靠判定的计划；partial 清单缺项不得推断为删除。读写窗口须同时取得派生目标的真实资源锁和原 canonical 资源的真实资源锁，使用固定锁顺序与有界等待，绝不拿新的锁域写旧树。锁内重新核对授权、Git、文件摘要和目录成员；目标写入采用 no-follow、独占临时文件、持久落盘和前置摘要比较。除本次获准的已验证路径外不修改资产，不运行业务 commit/push，不撤销原 writer 的锁。
5. **丢响应与失败。** 计划及幂等身份跨重启保留。已写路径必须能从实际前/后摘要判定“未写/已写/被第三方改变”，只续办原计划未完成步骤；未知进度不能重复覆盖。部分失败保留原件、恢复副本、已经落盘的成果和精确进度，不盲目回滚覆盖后来写入。若原资源、授权或计划内容变化，旧计划停止写入并重新对账；期间合法本地副本仍能执行读写测试。
6. **实际消费者与提交。** 成果已对账、目标再次通过正式 admission 后，才在空闲窗口准备同 native Session 的 normal 消费者；本地副本保留作为补偿来源。实际进程必须读回正确 canonical cwd、同一 native 文件、真实 normal admission 和受管 launcher 执行结果。已有 usage binding（历史用量绑定）、measurement origin（原始计量边界）和 seal（封账事实）原样保留；正式 reinit 合法产生新 canonical epoch 时，执行只消费其正式授权结果。计量另行保存真实证据，不把副本的未归账阶段伪造成 canonical baseline，也不以计量缺失阻塞正常消费者采用。
7. **完成或补偿。** 只有实际 normal 消费者验证成功、本次维护捕获的当前输入/session CAS 仍一致且 Host 绑定已严格持久化，才将派生活动投影转为保留的历史，并将本阶段记为 ready。保留原派生证明、manifest、副本和旧进程/绑定的补偿材料；不以记录 ready 代替能力。spawn、执行准入或最后 flush 失败时，经正式入口恢复同一合法本地 consumer；计量读回失败只影响计量，不触发业务消费者回退。未完成恢复不得删除其投影、成果或本地证明。后台接回失败不能使原本可用的本地工作永久失效。

ready 表示原合法范围内的正常本地执行与 Git 动作能力恢复。实际业务 commit/publish 仍由原业务意图触发；接回本身不提交、不 push、不重试结果未知的外部发送。发布依赖暂不可用时单独保持发布待恢复，不能把本地接回也判为失败。

本阶段不能自动消除以下事实：原登记 Git/存储持续不可用；没有可验证的原登记目标；源或目标锁持续被合法 writer 占用；双方不同的文件变化；与本次变更路径或子树相交的 partial 继承导致无法判定的删除或缺失；符号链接/特殊文件/控制资产的迁移；原授权被有效撤销或相关资源已正式转交新会话；冻结候选、封账或未决外部效果需要各自正式处理。这些必须给出具体资源/阶段/差异与重试条件，保留本地合法工作，不伪报已接回。语义冲突由当前 Agent 在已授权范围内处理，只有确实缺少业务判断或新权限时才向人类提出具体问题，不能把机器维修作为重新授权前提。

## 1. 责任、权利与硬边界

用户决定目标、授权范围、真实资源投入和不可逆事项。Agent 在该范围内决定业务方法、语义分类、自己的可逆纠错和权限交接。Host（承载会话的程序）提供真实会话和环境信息。SCL 保存事实、协调资源并落实授权，不以控制字段是否齐全替代业务授权。

| 动作 | 默认负责者及权限 | 必须保留的边界 |
|---|---|---|
| 补字段、改格式、依既有规则补分类 | 请求负责 Agent 可直接修正；不要求 WI 已有执行绑定 | 不暗中改变业务归属、验收标准或资源范围；明确交接按第 0 节办理 |
| 改尚未消费的仓库选择、业务草稿 | 同一授权范围内直接修订 | 已消费字段不能覆写；新增仓库超出授权时只询问新增范围 |
| 通过 HO 转授 WI/worktree 权限 | 权限持有者可决定，接收者接受后使用所授范围 | 接收前不要求已有目标权限；不扩大原授权；不是只通知不落实权限 |
| 请求人类重新授权/reinit | 所有缺权或需要裁决的 Agent 都能提出 | 请求不自行授予权限；不依赖旧绑定，不强迫先重建整个 WI |
| 改已保存的进展、自己的可逆产物 | 当前执行者，包括经交接或人类新授权的接续者，追加修订或有授权的撤回/补偿 | 旧 checkpoint（进度存档）、成果和消费者已经采用的事实可追溯 |
| 读回已完成操作 | 有该结果读取权限的调用者 | 不要求重新获得写权限；缺读取权限也能请求授权，不自动披露他人私有成果 |
| 恢复丢响应、暂时故障、自己的投影漂移 | 产品恢复器自动完成，Agent 不必研究内部状态 | 不覆盖他人文件；未知外部结果不猜测、不重复执行 |
| 停止尚未生效的工作 | 请求负责 Agent 可停止自己的未发出尝试；用户可撤销或收窄授权 | 已发出请求按第 5 节报告；停止不等于回滚已发生效果 |
| 改变业务目标、裁决共享资源、批准验收或不可逆动作 | 具有该项决定权者 | SCL 呈现影响并落实决定，不重复索取已有授权 |

系统记录 Agent 对上下文的判断、理由与来源，校验机器事实。旧会话不能自行恢复旧写权，但始终保留第 0 节的人类请求权；新授权成立后按新权限执行，不以退休身份作永久禁令。

## 2. 故障只能限制依赖它的能力

以下矩阵区分授权与实际可用性；不是对未授权新业务的放行。`local_work` 只含本地阅读、编辑、测试和可恢复提交，不含远端 push、验收或对外发送；交接和 reinit 请求权独立于该能力，不能随其不可用而消失。

| 故障 | 仍允许 | 只暂停/标记未知 | 恢复负责者与出口 |
|---|---|---|---|
| 分类缺项、枚举/格式错误 | 语义修正；不依赖该分类且已授权的本地业务 | 消费错误分类的准入/发布步骤 | Agent 按返回 schema 修正同一请求；涉及新的真实授权时直接请示 |
| 审计投影、远端同步、凭据或网络失败 | 已授权本地业务、权限交接决定及人类请示；可写内核上的 checkpoint | 对应发布/复制；不声明全局完成 | 对应效果适配器读回或有限重试，不因审计未同步否决交接 |
| 计量初始化或原生用量缺失 | 本地业务、checkpoint、会话接续、交接/请示 | 计量标为 unavailable/待补，不产出确定成本 | 计量适配器补证据；不得抹掉主操作凭据 |
| 一个资源或目录确有写冲突 | 无冲突资源；保存隔离成果；提出交接或人类裁决 | 具体冲突写入未完成，不将授权请求拒绝 | 工作区管理器落实已作决定、停止旧写入或隔离；不重复索权 |
| 当前全局执行代际无法证实，但未证明调用者已退休 | 依可信本地归属做隔离进展；第 0 节交接/请示 | 未证的全局归属、发布、验收与终态 | 补录/读回；有效交接或新授权优先于陈旧投影，不伪造全局同步 |
| 调用者退休、不是 owner/executor 或缺 worktree 权限 | 已获准的读取；无需旧权限即可向人类请求指定新权限 | 未获授权的新业务写入 | 按第 0 节处理授权；不能要求先获得所请求的权限才能请示 |
| 启动日志、Host 关联或目录投影损坏 | 独立凭据读取、直接人类请示；已安全隔离的业务 | 未能落实的控制写入/记录 | 第 6 节恢复与第 0 节授权通道均可用，不形成循环依赖 |
| 内核不可写/损坏 | 对话、reinit 请示、可读快照；文件系统允许时保存隔离成果 | durable checkpoint 和相关控制记录 | 安装态恢复器恢复；无持久化证据不报成功，也不拒绝递交授权请求 |
| 验收等待或清理失败 | 阅读成果、授权内准备修订、交接/请示、无关业务 | 相应验收/回收状态 | 原验收者/资源负责人作决定，产品落实，不回退已证业务结果 |

实现必须替换具体耦合：`scl_start.py` 不再用六阶段全部完成作为所有能力的总闸门；`scl_start_adapter.py` 的发布等待不再挡住独立的本地步骤；`scl_client.py` 在主操作提交后立即保存返回凭据；普通 entry、plan、continue 及资源对账不读、创建或重开计量，不调用 `ensure_epoch`、`read_existing_epoch` 或计量物化。显式计量仍严格核验证据，其失败不能改变已提交的业务结果；展示失败只降低展示能力。新建 WI 的全局创建未确认时，可以保存本地请求草稿并继续独立且已授权的工作，但不得编造全局 WI 或假称首次准入成功。

## 3. 在现有内核中增加请求修订，不另建状态机平台

### 3.1 稳定业务身份

一个 WI 可有多个业务请求；启动、一次 checkpoint、一次 rollover（会话替换）不是同一个请求。每个请求有一个由产品生成的 `request_ref`，第一次可能产生效果前持久化，绑定授权来源及其版本、已知 WI、当前修订和完成步骤引用。负责者可按第 0 节交接，保留原提交者与新负责者的关系；不得因换 Agent、标题或仓库拼写重新计算业务身份。

在现有本地内核增加请求记录与索引，不另建数据库、全局协调服务或第二份 WI 真源。Host 只保存入口消息/工具调用到 `request_ref` 的持久映射；它不是唯一记录者。普通入口自动携带该引用，换会话可按 WI 与未完成请求查询；不要求用户复制内部字符串。多个候选时返回精确差异，由 Agent 按实际任务选择，不凭时间或标题猜测。尚无 WI 的首次启动也必须能通过入口映射或原创建凭据恢复。原记录不可用不阻止人类作出新的授权决定；缺失历史与新授权分别记录，不伪造旧请求。

### 3.2 尝试与已提交效果

尝试保存不可变输入、前置条件、执行步骤和来源修订。原样重试沿用原尝试；修改则派生新版本，并原子更新当前可执行版本。校验错误不会形成可执行外部效果。

步骤去重身份由产品生成，绑定 `request_ref + step + step_revision + dependencies_digest`。只修改受影响且未生效步骤及其未执行依赖，不能因请求修订号或负责 Agent 变化重新执行已成功 create、发布或其他前置步骤。未受影响步骤保留原尝试和凭据。

精确重放须比较请求/WI、动作、原提交者及交接关系、规范化输入和相关前置条件，且重新检查结果读取权限。相同尝试标识配不同内容绝不能返回旧成功。合法接收者可读回原结果，不必冒充原提交者或恢复旧写权；缺权限时保留请示出口。

已提交内容的更正通过新 checkpoint 或后续业务修订关联原记录；候选已被采用、目标已消费或验收已冻结时，使用相应撤回/后续修订语义，不修改旧事实。这些状态不能阻止第 0 节的交接或人类决定，新决定以追加记录生效。预留未消费时修订草稿与认领操作比较同一版本，不能让目标读到混合新旧内容。

### 原工作区步骤的前置失败恢复

已登记工作区所在的仓库以经验证的 Git 边界解析；注册表的根仓别名与服务拓扑内部 ID 不要求同名。
根仓身份取自该注册表所属的已登记 human-work-root，业务子树仍取原 owner 的 workspace；不会因别名转换扩大 sparse scope。
零个服务拓扑匹配报告 `repository_not_registered`，多个匹配报告 `repository_ambiguous`。

普通 `start-local` 接收原请求的仓库或 scope 修订时，在同一请求锁下先核查旧 workspace 尝试。
只有原计划、全部登记和物化记录均不存在，且原请求解析出的确定性分支及分配路径也均不存在，才由协调器
把零效果读回写进原 revision 的历史并允许修订未消费字段。路径缺席必须由逐级元数据核查证明；核查不完整、不可读、路径中存在符号链接或已有部分效果均不构成零效果证明。
已完成 create/admit/activate 不重放；已存在计划沿原计划恢复。`recover-local observe` 仍是只读，不用展示动作暗中改状态。
这条普通恢复不要求重新授权或 reinit；没有替代真正 reinit 所需的当前 Host 决定，也不创建另一会话。

原请求选择使用权威账本中的当前 owner，不按最后更新时间或列表顺序猜测。其它 owner 的请求只有未进入执行步骤时
才作为保留历史排除；激活、工作区、准备或应用存在已提交或未明效果时仍需核查，不能据 owner 变化丢弃。
当前 owner 对应多个请求仍报告歧义。获授权的 reinit 直接把原请求及修订交给同一协调器，不先执行将被修订的旧输入。
发布恢复记录必须保留非空错误码；原异常已有错误码时沿用，未提供时使用明确的发布失败码，不以记录校验异常覆盖原失败。


## 4. 普通入口、恢复入口与不受其约束的人类通道

普通 `start-local`、checkpoint、rollover 等入口直接接收修正后的语义输入并沿原请求继续，不要求 Agent 先执行“申请纠错”。HO 由持权 Agent 发起，接收者无需先取得被交接权限。入口在响应中带回接受的请求版本。

另提供一个拟议的 `recover-local` 入口族，动作限定为 `observe / revise / cancel / restore`，分别表示查询、修订、停止和恢复材料；它是现有内核和适配器的薄入口，不是新的审批服务。普通错误不默认重建；但“默认不重建”绝不能解释成禁止随时通过 reinit 请求人类授权。该请求可直接经正常会话提出，不要求先成功调用 recover-local。

响应采用版本协商，不向严格 v1 对象直接塞字段。v2 必须含以下信息；字段名在首次实现提交中冻结，CLI、Relay（聊天渠道服务）和 Hook（会话入口钩子）共享同一 schema：

| 字段组 | 必须表达的内容 |
|---|---|
| 请求 | `request_ref`、WI（尚未创建时为空）、接受的修订、授权范围版本及交接/人类决定来源 |
| 提交事实 | 各已完成步骤的 operation/receipt 引用、失败步骤、部分成功；不是一个 success 布尔值 |
| 能力 | 本地读/工作/checkpoint、外部发布、交接、验收和计量各自状态、原因与新鲜度；区分已授权与机器尚未落实 |
| 问题 | 同一次失败的原因链、字段路径/约束/合法值或可独立读取的 schema 引用 |
| 恢复与请示 | 允许修正字段、负责者、下一动作、预计版本及自动重试；缺权时说明一个具体人类决定，不要求旧权限 |
| 外部效果 | 每一步的 not_sent / rejected_no_effect / in_flight / unknown / succeeded，以及证明引用 |

下一动作是类型化操作和参数，不是从 stderr 拼出来的 shell。敏感值只提供安全引用；请求人类授权不意味着可先读取无权访问的私有材料。帮助/schema 和人类请求出口在没有业务输入、旧执行绑定或可写内核时可达。客户端截断展示时保留完整响应引用；rejected/degraded 不得丢掉已完成步骤、恢复与请示信息。旧客户端不能解析时保留普通对话，不把升级作为向人类请示的前置条件。

确定性字段错误一次列出可识别的全部问题，不原样重试；暂时故障有限重试；未知效果先观察；真实冲突提供双方、资源和待决定事项。达到响应预算后返回事实，不让用户消息等待后台修复。有效人类决定是输入事实，后续错误只表示落实未完成，不可将它改写为授权未获批准。

## 5. 已领取、已发出与修订/停止的竞争

复用现有 effect（外部动作）、claim（领取记录）和 fence（拒绝旧执行代际的校验），检查当前授权、请求范围版本和步骤版本；不加冻结整个 WI 的全局锁。它们用于执行交接/人类决定，不用于否决新的权限来源。

1. worker 领取步骤后，实际发送前在同一事务比较 claim、执行代际、范围版本及步骤版本，并记录发送准入。交接生效、修订/取消与这次准入串行化；若权限已交出或撤销，旧 worker 不得获得准入。领取成功本身不是发送许可。
2. 若发送准入先发生，该步骤即视为 in_flight，即使随后崩溃、尚不能证明网络发送。取消只阻止尚未准入的步骤，响应必须带未决步骤；不能报告“全部已停止”。适配器支持下游 fencing/幂等时同时传递并验证原身份；不支持时不能声称本地校验能撤回已经发出的请求。
3. 存在未决步骤时，可保存新草稿、交接工作和请求人类裁决，但不得自动激活会重复该效果的新尝试。原身份读回、下游取消凭据或可靠零效果证明消除不确定性后才按原语义继续；另需补偿或改变业务动作时说明影响并按相应授权执行。读回暂时不存在、超时、worker 租约过期都不是零效果证明。
4. 零效果证明限于本地未取得发送准入、权威适配器明确的执行前拒绝，或下游确认原请求不可能再执行的取消/终结记录。错误名称含 invalid_request 不自动构成证明。无法查询且不支持幂等的外部服务，保留未知并定位具体未决动作，不以无限重试解困，也不因此取消人类请求权。
5. 已成功效果关联原凭据；晚到结果追加到原尝试，不覆盖后续进展。新执行者继承这些事实，不重发旧动作。新的明确授权以新权限生效，不复活旧代际；旧 worker 退出、机器权限切换和资源隔离由产品完成，不要求人类手工搬状态。

必须用确定性暂停点验证：领取后暂停→交接/修订/取消→恢复；发送准入后暂停→取消→晚到成功；两名修订者竞争；旧租约过期但远端请求尚未终结。取消检查的是新动作是否还能发生，不只是某一行状态是否变了。

## 6. 恢复工具自身失效时的最后路径

恢复读取依次使用：请求/操作原始记录 → 内核的校验快照与不可变提交凭据 → 配置明确的 canonical（全局正式事实）只读来源。Host 映射、目录投影和展示缓存只是索引，不能取代原始事实。快照与凭据标明来源、修订和摘要，不成为可独立写入的第二真源。这一顺序用于找回历史，不是第 0 节人类作出新授权的先决证明链。

安装态 `recover-local observe/restore` 的最小读取部分必须能在启动日志或活动内核打不开时运行，不依赖源码检出、旧执行绑定、计量、远端可用性或重建前的主命令初始化。使用现有快照/安装恢复机制；缺少这条启动边界时先抽取最小读取器，不新建常驻服务。最小读取器也失效时，普通会话中的 reinit 请示仍成立。

| 损坏范围 | 同一 WI 的恢复方法 | 不得做什么 |
|---|---|---|
| Host 消失、映射丢失 | 从请求索引/原凭据找回；按有效交接或人类新授权接续 | 不凭相似标题另建 WI，不把旧 Host 消失当作人类无权重新指定执行者 |
| StartJournal 损坏，但 create 凭据存在 | 保留坏文件诊断副本；以精确请求—创建凭据重建派生进度 | 不重发 create，不要求旧 journal 先通过校验 |
| 目录投影/登记不一致 | 据已有授权、资源事实或人类裁决修复投影，必要时隔离成果 | 不覆盖无关他人文件；旧登记缺失不能否决新的明确授权 |
| 内核损坏但快照/操作凭据完整 | 只读观察，产品重建已证事实并落实当前授权，停止旧写入者 | 不靠 Agent 手改 SQLite；不提升快照为当前全局权威 |
| 身份或外部效果证据不足 | 保留原 WI 引用与可读成果，说明缺失，直接请求人类指定新权限或处置；继续独立已授权工作 | 不伪造旧历史或外部成功，不要求人类恢复全部旧证据后才能决定 |

恢复许可来自有效业务授权、范围内的交接或新的明确人类决定，不来自损坏的旧绑定。真正执行者竞争交人类裁决，裁决后产品落实，不再要求输掉权限的一方批准。reinit 可只更新权限，也可按决定重建前沿；只有实际需要重建时才补齐相应语义，缺字段可在同一授权下纠正。

## 7. 复用、删除与存量迁移

### 7.1 具体删改清单

| 对象 | 处理 | 替代及退出条件 |
|---|---|---|
| HO 仅送达/ACK、接收者准入要求已拥有待交接权限 | 删除循环前提；HO 同时落实所授 WI/worktree 能力 | 接收者实际编辑/测试/存档；独占范围原写入者退出；原成果不丢 |
| reinit 请示依赖 owner/executor、旧 epoch、完整草稿或活动内核 | 请求路径前移，旧状态只作诊断 | 所有缺权身份均可经正常会话请示；明确授权后无需第二次批准，最小范围落实 |
| 将 R1.13 的退休限制泛化为禁止人类再授权、将 R1.14 的投递限制用于持权 Agent 的明确委托 | 原地修正规范及入口 | 无新授权不自行夺权；范围内委托无需逐次人审；机器状态不能否决人类新决定 |
| `scl_start.py` StartJournal | 不再独立持有可写业务请求进度 | 请求/阶段凭据进入现有内核；迁移后只读兼容导入，旧写入路径停止 |
| 六阶段全部完成才可工作、准入发布同步总门 | 删除总门，不删除各动作真实约束 | 按第 2 节能力与依赖执行，实际业务测试证明断开依赖 |
| 内容哈希作为业务身份、admission 单一补全例外 | 替换 | 稳定请求引用与按未消费字段修订，存量保留旧引用别名 |
| `_exact_retry` 及未校验的成功快捷返回 | 替换为同一请求比较器 | 完整重放、交接后合法读取测试通过，删除分散特例 |
| 普通 entry 自动读写计量及其异常吞掉主操作事实 | 删除普通入口的计量调用 | `test_entry_never_reads_or_initializes_measurement` 验证计量缺失、待基线、开放、封账均不影响 entry；显式计量保留严格证据检查 |
| outbox（待发布请求）、效果执行器、Broker | 保留原效果证据和真实发布保护 | 增加修订来源/执行版本；输入错误、等待、未知、冲突分流，不另建平行队列 |
| v1 CLI/Relay/Hook 与 legacy writer | 有期限的协议兼容，单一状态写入者 | v1 受控翻译；不能表达的新动作保留会话请示和查询，不能封死 reinit 请求 |
| 原探针和结果 | 保留为历史诊断 | 后续增加期望业务成功的产品测试，不重写结果冒充验证 |

### 7.2 迁移顺序与回滚界限

以一个 WI 及其关联请求/效果为最小切换单元；迁移是本次实现的交付内容，不转交另一个 WI。先安装兼容读取和受控写入的版本，再切数据，不把多机同时升级当作前提。迁移暂停某项写入时，人类请求和授权记录通道仍可达。

| 旧记录 | 确定性映射与校验 | 缺证据时 |
|---|---|---|
| StartJournal 与部分阶段凭据 | 按原 authority/site、intent_ref、明确 WI/创建事务建立稳定别名，核对原输入摘要和阶段凭据；重复导入不增请求 | 同一 WI 多个旧启动不凭标题合并；缺创建关联则观察，不重发 |
| 内核 operation/checkpoint | 保留 ID、内容、顺序和摘要；每个原逻辑操作成为独立请求原尝试 | 不安全推断业务关联，不改历史 |
| HO、委托和人类授权 | 保留来源、授予方/接收方、范围、生效与撤回事实；已接受委托关联原 WI/目录 | 缺记录不捏造旧委托；直接交人类决定新权限，不强迫重走旧审批 |
| outbox/Broker/effect | 保留原幂等身份、领取/发布/复制凭据；只有已证共同来源才关联步骤 | conflicting 不批量改可重试；已领取、未知不按无效果迁移 |
| 既有候选、交接包、终态 | 保留原语义与消费者采用事实，只补引用 | 不重新发布、不伪造终态；新授权追加表达，不复活旧凭据 |

切换过程：只读盘点和 dry-run → 记录原记录/成果摘要及备份 → 停止该单元旧写入与调度，核对已领取步骤 → 事务内建立映射并提高存储/写入代际 → 新消费者读回验证 → 激活新执行路径。崩溃后依迁移凭据重入同一步，不能部分映射后两套都写。未切换 WI 仍用单一旧路径；新旧请求由同一受控恢复器观察，不双发。

不支持新代际检查的旧二进制必须退出该写入路径，安装器和入口拒绝其再写已迁移存储；仅写一个旧代码不读取的版本字段不算隔离。无法证明旧进程停止或外部请求终结时，产品按新授权停止所管理进程或隔离资源，保留未决效果并说明具体执行限制；不得把整项人类授权退回或要求旧 owner 再批准。

**回滚分界：** 激活前且没有新写入，可在停写后恢复字节一致备份；产生任何新写入后，禁止回到旧快照丢掉进展或新授权。此后只能回退到保留新格式的已验证运行包，或停用问题特性、保留兼容读取并向前修复。切换记录最低可读/可写版本、原/新代际和效果清单。旧别名只读保留至相关请求终结；兼容入口在支持消费者升级、旧待执行请求清空且真实回归通过后删除，但不删除普通会话的人类请求权。


## 1. Design decision

SCL becomes a small local lifecycle kernel with durable operations and asynchronous external
effects. Git remains the human-auditable and cross-site canonical publication format; it is no
longer the synchronous transaction engine for local execution continuity.

The design deliberately separates three facts:

```text
local kernel commit ──> local effective frontier ──> ordinary local work can continue
          │
          └──> effect queue ──> fenced global publication ──> replication/readback
                                      │
                                      └──> global canonical frontier
```

A network partition can therefore leave the local and global frontiers at different revisions
without lying about either. Safety comes from fencing external effects; availability comes from not
requiring those effects to finish before local work proceeds.

`@anchor manual architecture-decision-review :: verify implementation has one local transaction boundary and external adapters cannot be called from inside it`

## 2. Components

### 2.1 LifecycleKernel

Cross-surface sealing captures business facts from the same bounded kernel frontier:
original lifecycle events, effective collaboration contracts, task receipts, adopted
plans, and all admitted input versions at their original archive locations. The
capsule digest binds this history; the existing source-fence CAS rejects concurrent
progress. Target hydration imports history and the non-executable frontier in one
transaction. It does not read a source ledger, create another human decision, or
replay source effects. Repeated transfers preserve original evidence locations and
versions. Scoped Session identities are privatized with the source identity;
resource grants are realized against the target registration only on apply.

`@anchor pytest agent-harness/tests/test_cross_surface_rollover.py :: independent kernels retain business history and original evidence versions while fencing the old writer`

The kernel is the only local writer. It validates an asserted operation and its machine
preconditions, checks the current local fence, appends an operation, advances the local effective
projection when safe, and enqueues effects in one transaction. It does not determine whether the
human or contextual Session Agent internally meant the submitted decision. It exposes a versioned
local API to CLI and host adapters.

Clients submit domain intents and original input facts, never a caller-selected
replacement lifecycle state. Draft creation, owner activation, state transitions,
candidate submission, independent Outcome, human
review, definition amendment and terminal settlement use the same decision
boundary. The committed record contains the resulting WorkItem image, events,
original response and required delivery effects. Delivery can render or confirm
that record; it cannot execute the business transition again or adopt a published
ledger back into the kernel as a second decision.

Ordinary continuation performs any still-missing one-time business-history import,
including an older native frozen candidate's original Outcome and human review.
The importer preserves execution state, original operation identities and pending
effects, and stops consulting views after import. Planning and entry against an
existing kernel remain read-only. Retained scoped grants must not overwrite
`observe_candidate` with source-identity repair when no execution is granted;
actual replacement reservations and legitimate pre-review correction keep their
existing boundaries. A CLI command carrying a frozen candidate operation is a
delivery request: the canonical publisher verifies the original operation and
recovers its request instead of redirecting it to first-time submission.

Independent verification admission reads the original frozen image through the shared
candidate projector, including an explicitly admitted legacy image bound to that same
operation. Publication policy rechecks use the existing verification workspace binding's
owner and candidate digest. Neither reader selects a current WI YAML image, whose
timestamps or content may differ from the frozen candidate. Missing original input still
refuses admission without refreezing, copying ledger state or changing the assignment.
The same snapshot reads bounded original collaboration records from the kernel, including
locally committed assignment creation, prior archival and ACK. Git handoff delivery is a
separate effect, not a prerequisite for local verification. No handoff YAML is parsed on the
managed frozen path. The reader rechecks its kernel revision; an intervening decision cannot
produce a mixed result. Published source and physical workspace proofs retain their roles.

`@anchor test agent-harness/tests/test_registered_verification_admission.py :: test_registered_admission_reads_admitted_legacy_candidate`
`@anchor test agent-harness/tests/test_registered_verification_admission.py :: test_committed_assignment_does_not_wait_for_or_trust_git_copy`
`@anchor test agent-harness/tests/test_registered_verification_admission.py :: test_kernel_assignment_change_during_admission_is_rejected`
`@anchor test agent-harness/tests/test_registered_verification_admission.py :: test_projection_validates_original_source_instead_of_temporary_ledger`

`@anchor pytest agent-harness/tests/test_scl_business_import.py :: normal continuation imports an old approved native candidate while read-only planning and original decisions stay unchanged`
`@anchor pytest agent-harness/tests/test_scl_candidate_source_recovery.py :: public CLI recovery publishes the original frozen request without another submission`
`@anchor pytest agent-harness/tests/test_scl_lifecycle.py :: candidate observation retains lifecycle orientation without granting execution`

Terminal decisions read responsibility contracts from the kernel's original
history and committed operations. Completion and the resulting handoff closures
commit atomically; the CLI does not finalize handoff ledgers afterwards. Exact
retries return the original completion response while rebuilding the current
projection, including when a previous projection write failed.

A draft and its creation event enter the kernel atomically, before execution or
Git delivery. Definition changes and responsibility acknowledgements may advance
its local revision without starting execution. First workspace registration and
owner entry check the kernel's actual binding, never a zero revision or the
absence of an execution YAML file. Physical workspace identity and authorized
resource scope remain separately enforced. A retried acknowledgement retains its
original response while rebuilding the current view, so it cannot restore an old
owner or draft status after activation.

Assignment commits creation, readiness, in-progress status and the original
responsibility offer as one decision. Batch assignment and diagnostic-report
creation submit the same intents before writing any view; report provenance is
retained in the creation event. Full acknowledgement of an unstarted assignment
changes responsibility without issuing a workspace grant. Explicit owner claims
and releases also enter the kernel; replaying an old claim returns its original
response and cannot reclaim work released afterwards. Discarding a projection
does not release its WI or handoff identity for reuse.

Cancellation, supersession and migration also commit the terminal decision and
contract closure together. A successor must still be open inside the commit
transaction; unrelated revisions of that successor do not invalidate selection.
The human stop is effective locally before publication. A later publisher may
attach delivery to that original decision in a state-preserving transaction,
without repeating the stop or copying its historical views over later facts.
For a legacy execution row whose business original has not been imported, the
same input first terminates execution and retains its original signed content.
It does not fabricate a business cancellation without its required facts. Once
original intake succeeds, the normal disposition decides cancellation and
contract closure. A stopped WI without a prior binding is terminal in execution,
not an unstarted WI that ordinary activation may claim. Canonical status
observations cannot overwrite that local business state.
Cancellation authorization binds the responsible and executing identities and
the real resource fence, not the entire state digest or checkpoint. Later human
decisions and changed execution bindings still invalidate an older authorization.
Post-publication legacy adoption cannot overwrite kernel-owned business state.

CLI observations use a read-only lifecycle reader over captured kernel rows and
bounded original operation history. WI detail, lists, contracts and events remain
visible without any ledger files. The reader excludes owned projection identities
before parsing legacy files; a valid but forged projection cannot introduce a
new WI or contract. Unimported historical originals remain separately readable.
Reading does not import facts, write views, or grant execution.

Heartbeat scans, dispatch routing and completion notifications share this reader.
Their pending-contract index comes from the same captured original history;
deleting or editing a projection cannot hide a task, change its receiver or lose
a completed assignment's notice. A timed-out subprocess records its attempt's
timeout only. It cannot roll an accepted contract back to pending: ACK and later
withdrawal/reassignment are explicit kernel decisions, not inferred from exit.

Terminal-contract maintenance uses the same reader and submits an archive intent
to the kernel. The writer rechecks the actual WI terminal state at commit; an
edited terminal YAML cannot archive an active WI's contract. Filing remains
separate from timeout or executor-death decisions.

Pending-contract timeout commits the updated contract, original event and notice
in the same kernel transaction. The existing effect reconciler delivers that
notice; later sweeps resume it even when no pending contract remains. A missing
policy leaves delivery pending. A send with uncertain outcome is only read back;
an absent external receipt is not proof of no send. Such receipts are external
effect evidence, not disposable lifecycle projections. Attention routing also
reads current WI/HO facts through the lifecycle reader.

Executor maintenance cannot infer death of an actual kernel session or grant
from a Relay lookup by agent name. Without proof for that execution identity, it
retains authority and records at most one stale notice per identity. Checkpoints
and repeated sweeps neither revoke the grant nor issue duplicate notices. The
old Relay classifier remains limited to legacy stores without a local kernel.

Terminal delivery binds the original completion operation and its retained
result. Later contract filing or checkpoint metadata does not create another
completion and cannot invalidate its delivery or exact retry. Both standard and
lightweight callers use this same check. A reopened WI, a later completion, a
different publication request, and unknown in-flight effects remain distinct
constraints; confirmation CASes the current kernel state without overwriting it.
Once delivery is admitted, exact retry and preview read its original kernel
receipt. Historical terminal adoptions survive one-time business intake and
later bookkeeping; they are not new completion operations and do not re-read
mutable projections or re-admit their original external proofs. A later reopen
or another completion still prevents treating the older receipt as current.

An admitted independent result retains the receipt and its declared evidence
buffers inside the immutable decision. It captures the buffers used by the
validator, not a later read of reusable file paths. Later human review consumes
that admitted result. A missing or edited evidence projection cannot revoke an
acceptance; a new candidate requires its own independent admission. Historical
receipt bundles remain reproducible even when another round reuses the same name.

Explicit human waivers retain the instruction and the independently observed
receipt bundle at admission, including an observed absence. Editing a delivery
file cannot revoke that instruction; another human decision can. Task-outcome
receipts, their evidence buffers and measurement events enter the same atomic
decision boundary. Their JSON files and events are reconstructible views.

An existing local writer imports missing business content and original history
once from the selected original source. A named Git revision is read directly,
independent of missing or dirty working files. This import preserves every owned
lifecycle field, operation identity and pending effect; source lifecycle values
cannot overwrite them. Migration and normal operation are separate boundaries.
An old revision-zero cache row with no owned content, original history, operation,
authority, checkpoint or execution metadata may be replaced by the selected
original import. This absence is checked again inside the import transaction;
concurrent decisions prevent replacement. A human-reinit baseline is not such a
legacy cache and does not gain execution rights through this migration.

A legacy submission retains its original submission event, independent receipt
and declared evidence, and human decision with its original provenance. The
one-time admission validates those inputs against the selected revision and
stores them with the immutable imported history. It never creates replacement
freeze, verifier or review operations. Review, Outcome and completion share one
submission reader over imported originals and subsequent kernel decisions.
Published review and evidence files remain disposable after this admission;
changing them cannot revoke acceptance. Explicit human rework remains a new
kernel decision and invalidates final delivery immediately.

An old kernel freeze does not replace its subsequent verifier or human originals.
Their original submission event must name that exact freeze and agree with its
claim, producer and acceptance fingerprint; an already final review must retain
the kernel's review digest. One-time admission preserves the freeze, final-review
operation and pending effects. The shared submission reader attaches those old
decisions to their original native candidate without manufacturing new approvals.
A later candidate with the same claim cannot reuse them, and historical review
readback keeps the original candidate identity after rework.

`@anchor test agent-harness/tests/test_scl_business_import.py::test_old_kernel_freeze_keeps_original_outcome_and_final_human_review`

There is one authority per field: lifecycle, owner, executor, epoch and acceptance
belong to the kernel state; the remaining business content contains none of those
fields. Git main remains the product source of truth, not another local execution
writer. Cross-host publication and transfer retain fencing and original external
receipts; a generated ledger cannot confer a new execution generation.

The default storage is SQLite in WAL mode under a site-canonical, Git-root-external state directory.
The choice is implementation-specific but the contract is not: atomic local transactions,
checksums, single-writer serialization, concurrent readers, crash recovery and online schema
migration are required. A different backend must satisfy the same tests.

The cutover ownership query is shared by legacy execution/cost writers and terminal
reconciliation. `reconcile-terminal` checks it before reading ledger inputs or acquiring its
write lock. A managed installation, including a cold WI, or a kernel-owned WI retires that
direct writer. This does not create another reconciliation mechanism or fabricate old events.

`@anchor test agent-harness/tests/test_kernel_legacy_reconciliation.py :: test_legacy_terminal_reconciliation_is_retired_for_owned_work_item`

### 2.2 OperationJournal

The journal stores immutable intent and mutable derived progress. It owns idempotency, per-WI
ordering and the mapping from a user action to all effects. It never infers success from process
exit alone.

### 2.3 CapabilityProjector

The projector evaluates each capability independently from current local storage, fence and
dependency probes. It returns reasons and freshness, not a single product-wide health bit. The
external-publish input comes from a bounded concrete probe of the fenced WI registration (or a
fresh durable effect receipt); entry paths do not substitute a constant unknown value for an
available adapter.

Entry and mutation responses inspect the physical workspace directory for local work,
never the disposable WI YAML or ledger directory. Checkpoint availability reads kernel
frontier/persistence alone; the checkpoint operation still enforces the caller's binding.
Projection loss cannot suppress either capability, and observation does not repair files.

### 2.4 Reconciler

The reconciler leases pending effects, calls their adapters, persists attempt evidence, performs
readback for unknown results and schedules bounded backoff. It runs in the resident service and can
also be triggered opportunistically by any client. Losing the resident service affects latency,
not correctness: the next client restarts reconciliation.

### 2.5 Effect adapters

Adapters implement one provider-neutral protocol:

```text
probe(effect) -> dependency facts
plan(effect, facts) -> no-op | apply plan | conflict
apply(plan, operation_id, fence) -> acknowledged | unknown | rejected
readback(operation_id, fence) -> absent | committed(receipt) | conflict
```

Git audit publication, global CAS, surface replication, runtime activation/readback, handoff
delivery and review lookup are adapters. Provider details stay outside kernel state transitions.

The Git audit adapter resolves an authority root per effect. It first reads the effect's WI,
authority generation/fence and exact registered repositories, then identifies the unique
repository whose WI-local Harness registration contains that WI. A constructor-supplied Harness
root is only a legacy optimization when it agrees with the registration; it is never a semantic
selector. This permits one resident reconciler to process effects spanning multiple repositories
without being relaunched per repository.
For a legacy business-only registration with exactly one sealed repository and no local Harness directory, immutable audit receipts use that registered branch under `.harness/evidence/scl-operations`. Directory absence is not a missing external dependency. This does not create canonical WorkItem state or select the process-wide service root as a publication target. Multiple unresolved repositories and redirected or non-directory audit roots remain rejected; all Git identity, fence, ancestry, write and exact readback checks still apply.

### 2.6 Materializers

Materializers create compatible `.harness` YAML/JSONL and read models from the journal. Generated
views carry source operation ids and a projection watermark. They are disposable and can be rebuilt;
legacy evidence imported before cutover remains immutable.

Missing, stale, malformed or edited owned views are replaced from committed
records, including their index entries when Git replication requires it. Such
differences are not lifecycle conflicts and do not require a separate recovery
branch for each pair of copies. A materializer must not infer a withdrawal,
amendment, review or completion from the destination file. Real business files,
original human decisions, independent receipts and unknown external effects are
inputs, not disposable views. Mixed historical files become disposable only after
their original facts have been imported once with provenance into kernel-owned
storage and every continuing writer has moved to that boundary.

A delivery view may cover several committed decisions and records that immutable
prefix. A later checkpoint does not invalidate the decisions already covered.
Coverage proves content only; actual Git publication and endpoint replication
still need their own observed receipts. Rebuilding a private delivery view may
restore evidence aliases. Rendering at a producer's source must preserve any
unadmitted next-round input already present under a reusable name.

### 2.7 Ingress adapters

CLI, Codex hook, Relay, Pi and future surfaces are clients of the same local API. Hooks can detect
and enrich a command, but cannot become the sole route to the kernel. Skills explain user intent;
they do not implement state transitions or recovery.

Relay owns one channel-neutral `SclChannelIngress` between channel authorization and the local
client. Feishu and Telegram pass only a normalized command, resolved user/Agent identity, channel
name and current host-runtime observation. The ingress owns every lifecycle verb and returns a
channel-neutral result:

```text
Feishu parser  --\
                  > SclChannelIngress -> sealed harness-scl -> scl_response/v1
Telegram parser --/          |                    |
                             |                    +-> local kernel + LKG
                             +-> safe channel result + structured diagnostic
```

The ingress never imports the legacy `Scripts/scl-resume.py` state machine. Runtime discovery is
co-versioned deployment data: the active Relay release names a compatible sealed SCL release and
its manifest digest. Activation probes `handshake`; failure selects only a verified compatible LKG
or leaves the lifecycle capability degraded while the channel remains usable.

Entry can resolve mechanically from a Host observation. Checkpoint and rollover first obtain
orientation, then deliver the authenticated intent and ResumePacket to the current Session Agent;
that Agent constructs the semantic draft and calls the canonical local mutation. No direct channel
handler calls legacy checkpoint/rollover, and no deterministic adapter invents decisions from a
read model.

One typed diagnostic mapper retains `request_id`, `status`, stable error code, retryability and
capabilities. It emits a bounded safe reason to the user and a structured log containing node,
channel, Agent and WI. Raw Session/thread identities, credentials, command stderr and internal
replacement refs are excluded by construction.

Semantic input models are validated at their input boundary. Their bounded field/type diagnostics
return `invalid_request`, allowing correction of the same request; they never fall through to the
generic local-material exception mapper. Reinit fills omitted runtime/surface from the current
Host rather than reading the execution state that it is authorized to replace.

For host-native Pi tools, Relay exports an opaque Session, reviewed execution surface/runtime and
its process id in `scl_relay_host_context/v1`. The local client verifies matching entry identity and
live process ancestry before renewing an observation. This is a local Host integration contract,
not an OS isolation boundary. Conversation remains available if the Host configuration is absent.

### 2.8 SourceAttachmentResolver

This component owns the privacy-safe join between a logical bound source and a Host-derived opaque
Session. Current-format bind/apply writes the join transactionally. Legacy import either proves and
imports the join or leaves it unresolved. The resolver never treats opaque-ref inequality as
positive evidence of another live Session.

For an unresolved imported source, a contextual human or Session Agent may submit
`attach_current_host_to_source` with a rationale. The kernel checks effective actor/scope
authorization and exact epoch, logical source, runtime/surface, replacement and frontier
preconditions before recording the join. This continuity operation is not the admission path for
a new grant; section 0 applies independently of an old attachment.
The operation does not advance epoch, change candidate state, replace the source or transfer
responsibility. Distinct-source liveness remains a Host readback fact.

`@anchor manual component-boundary-review :: dependency graph review proves ingress and adapters depend on the kernel API while the kernel imports neither UI hooks nor provider SDKs`

### 2.9 LineageManager and ReinitCoordinator

`LineageManager` owns the release-level live-lineage pointer. Cutover seals the previous kernel
lineage with a manifest containing database, projection, effect and runtime digests, retires all of
its leases, and atomically activates a fresh lineage directory. Readers may inspect the archive, but
no scheduler, hydrate path, authority resolver or compatibility consumer can select it as current.

`ReinitCoordinator` records the signed Host decision and normally preserves the existing business
frontier while adopting the affected resource grant and binding. Complete-draft reconstruction is
reserved for an explicitly authorized rebuild. Neither path asks the previous executor for approval.
Exact existing registration bytes are reused after Git identity validation, including on dirty
worktrees; discovery is only for a missing registration. Realization resumes the same decision.
A pending resource cannot grant execution merely because the decision was accepted.

## 3. Durable data model

All records include `schema_version`, UTC timestamp, payload digest and writer runtime revision.
Opaque identities never contain raw provider Session, thread or user identifiers.

### 3.1 `work_items`

| field | meaning |
|---|---|
| `wi_id` | stable WorkItem identity |
| `global_revision` | last reconciled global canonical revision |
| `local_revision` | monotonic site-local effective revision |
| `global_state_digest` | digest of imported/read-back global state |
| `local_state_digest` | digest after locally committed operations |
| `mode` | `fenced` or `local_isolated` |
| `last_checkpoint_ref` | current locally durable frontier |
| `projection_watermark` | latest materialized operation sequence |

Domain lifecycle status, owner, executor, candidate and phase remain typed state fields, not generic
JSON patches. State-machine validation occurs before an operation is committed.

### 3.2 `authorities`

| field | meaning |
|---|---|
| `wi_id` | owning WI |
| `authority_home` | registered site identity |
| `generation` | monotonic fence generation |
| `registration_id` | exact current registration |
| `registration_digest` | digest of normalized registration bytes |
| `base_global_revision` | global revision on which this fence was granted |
| `lease_until` | optional time bound for externally publishable effects |
| `state` | `valid`, `expired`, `retired`, `conflicting` |

Time is not the sole safety proof: a publisher must also compare generation and base revision at
the global CAS boundary. Clock uncertainty can shorten availability but cannot create two winners.

### 3.3 `operations`

| field | meaning |
|---|---|
| `operation_id` | stable opaque id |
| `idempotency_key` | deterministic digest of intent identity |
| `wi_id` / `sequence` | per-WI total order |
| `intent_type` / `intent_payload` | typed requested action |
| `precondition_digest` | state/fence assumptions |
| `authority_generation` | fence used at acceptance |
| `local_status` | `accepted`, `committed`, `rejected`, `reconciliation_required`, `human_decision_required` |
| `global_status` | `not_required`, `pending`, `committed`, `conflicting` |
| `result_digest` | immutable result identity |

The unique keys are `(operation_id)`, `(idempotency_key)` and `(wi_id, sequence)`. An exact retry
returns the existing result. A changed intent under the same idempotency key is a stable conflict.

### 3.4 `effects`

| field | meaning |
|---|---|
| `effect_id` | deterministic child of operation id + effect kind + target |
| `operation_id` | parent operation |
| `kind` / `target` | adapter and normalized destination |
| `fence_digest` | exact external-write fence |
| `status` | `pending`, `leased`, `unknown`, `succeeded`, `retry_wait`, `conflicting`, `cancelled` |
| `attempt` / `next_attempt_at` | retry control |
| `request_digest` / `receipt_digest` | idempotency and readback evidence |
| `last_error_code` | stable provider-neutral class |

Effect payloads are immutable. Retry metadata and receipts update transactionally. Secrets are
looked up at apply time and are never stored in payloads or evidence.

### 3.5 `checkpoints`

Checkpoints are immutable records keyed by WI, epoch and monotonically allocated revision. Content
is canonicalized and digest-addressed. A checkpoint contains:

- objective and all active acceptance requirements;
- accepted decisions and their rationale;
- current domain artifact refs and exact revisions/digests where available;
- open questions and external effects still pending;
- first next action and required reads;
- authority/freshness assumptions;
- source opaque execution ref and creation operation id.

Checkpoint bodies do not contain transcript text, secrets or raw provider identity.

### 3.6 `registrations`

Registrations have explicit lifecycle `active`, `retired` or `quarantined`. A binding stores the
exact registration id, generation and digest. Historical rows are queryable for audit but never
participate in current authority resolution.

### 3.7 `source_attachments`

| field | meaning |
|---|---|
| `wi_id` / `epoch` | exact lifecycle frontier |
| `logical_source_ref` | source identity fenced by the binding |
| `host_session_ref` | Host-derived opaque Session identity |
| `surface_id` / `runtime_class` | compatibility precondition |
| `frontier_digest` | binding/checkpoint/replacement state attached |
| `state` | `attached`, `retired`, `conflicting` |
| `decided_by` / `rationale_digest` | audit provenance when legacy evidence required contextual assertion |
| `created_by_operation` / `created_at` | durable join evidence |

The existing attachment key is `(wi_id, epoch, logical_source_ref)`. The target authorization model
must also identify the grant and resource scope: conflicting exclusive attachments require
fencing, while separate authorized sources for disjoint scopes may coexist. Journal serialization
does not confer exclusive ownership of every business resource. Raw provider
identifiers and transcript material are forbidden.

### 3.8 `lineages` and reinit drafts

The live-lineage record contains a generation, schema/runtime compatibility digest, activated-at
timestamp and optional retired-predecessor manifest. A retired manifest is immutable and never
eligible for work-item lookup, effect leasing or authority adoption.

A full-rebuild `reinit_draft/v1` contains the WI id, human command provenance, objective, acceptance
criteria, responsibility and granted action/resource scopes, phase, known workspace identity and
registration evidence, preserved artifact refs, completed and pending work, decisions, risks and
first business action. Missing registration is materialized from the authorized resource and
verified repository identity; an old exact registration is not a grant prerequisite. A bounded
grant or projection repair does not require a full-rebuild draft. No raw provider identity is
stored. The Host adapter supplies the opaque current Session observation separately at apply time.

`@anchor manual durable-schema-review :: schema migration and canonical serialization tests cover every identity, unique constraint, digest and forbidden sensitive field`

## 4. Operation protocol

### 4.1 Accepting local intent

```text
1. ingress canonicalizes a typed intent and idempotency identity
2. kernel begins an immediate per-WI transaction
3. read available effective state and action-specific registration/fence evidence
4. validate grant provenance/scope for a grant change, or effective permission for an execution; classify realization dependencies
5. append operation and immutable checkpoint/domain event payloads
6. advance local effective state when locally safe
7. enqueue separately fenced effects
8. commit
9. return one machine envelope immediately
10. wake reconciler; never wait synchronously for all effects
```

Grant requests and valid grant changes enter before old execution admission. An absent or expired
execution fence cannot veto the decision: persist its provenance and scope, then realize only the
resources whose exclusion and physical readiness are established. If storage is unavailable, retain
the existing conversation/handoff decision for recovery without claiming it was persisted. The
envelope distinguishes accepted authorization, local realization and withheld external effects.

Validation failure before step 5 changes no state. Process death before step 8 rolls back the whole
transaction. Process death after step 8 leaves one recoverable committed operation.

### 4.2 External publication

The publication adapter builds an immutable projection from committed journal records. It performs
a global compare-and-swap on `(wi_id, base_global_revision, authority_home, generation)`, attaches
the operation id, and reads the result back.

- exact existing receipt: idempotent success;
- base, exact registered worktree HEAD and remote unchanged with a current fence: publish once;
- remote advanced from the exact registered worktree HEAD by compatible receipts: durably record
  the ancestry/path readback, requeue, and publish from the observed remote head;
- remote product history is absent from or diverges with the exact registered worktree: retain the
  conflict without auto-merge or branch selection;
- competing fence or incompatible state: mark conflict and require resolution;
- unavailable or denied: retain pending and back off.

Local domain work and new checkpoints continue in all but a real local storage failure. Operations
whose meaning depends on global exclusivity remain isolated until the fence is restored.

### 4.3 Handoff and terminal settlement

Handoff records an in-scope offer, its recipient acceptance, the resulting grant and resource
realization separately. Delivery or ACK alone does not transfer rights. The recipient needs no
pre-existing owner/executor role or target registration. Acceptance activates the scoped grant
without another human approval; exclusive realization stops or isolates the prior writer for that
scope before the recipient writes. Disjoint authorized scopes remain executable. Network receipts
prove remote realization where needed, not whether the delegation was authorized. A subsequent
in-scope delegation uses the same rule. Unknown remote effects remain pending and are not repeated.

Terminal settlement records a locally complete candidate and enqueues the normative gates for
that workflow profile. Standard / legacy Phase2 work keeps its review, Outcome, publication and
readback chain. A WorkItem whose original startup and current effective profile are both
`lightweight` instead uses one bounded fast terminal mutation after **explicit human acceptance**. That mutation is not a second
Phase2: it must prove the original startup profile from the claimed-owner prepare/apply journal; a
legacy WI may be backfilled only from an immutable `portfolio_default` admission. The workflow
profile module resolves that original accepted operation for all later queries, retries and
completion; it does not reopen the legacy admission file as a second authority. Missing original
admission before import and incomplete recorded proof still reject. Completion atomically
commits `status=completed` in the local kernel, clears `active_executor`, records a digest-bound
profile proof plus the human acceptance fact, archives accepted pure `outcome_verification`
handoffs and withdraws pending ones. The same operation owns the resulting ledger projection
and pending publication effect. Replication readback proves delivery of the original completion;
it neither authorizes a second decision nor restores execution while publication is pending.
Standard-profile WIs, including formally promoted WIs carrying original lightweight proof, fail closed on this path.

Fast terminal settlement never deletes a worktree or branch. After canonical terminal readback,
physical cleanup must first preserve and deliver accepted results still unique to the temporary
branch, then prove the exact worktree and temporary refs have been removed. Only afterwards does
`worktree retire-terminal` archive the registration. It does not delete Git resources. A published
terminal ledger and a recovery bundle alone do not prove accepted-result integration or cleanup.

The WI is not globally terminal until its profile's normative publication/readback gates succeed.
Pending gates do not prevent reading, explaining or correcting the same WI through an explicit
nonterminal operation.

`@anchor manual operation-protocol-model :: deterministic simulator explores crashes and retries for local mutation, publication, handoff and terminal settlement and verifies safety invariants`

### Same-WI promotion

`promote-workflow-local <wi> --reason <reason>` resolves the current Host through the existing
client and validates active owner/executor, runtime/surface attachment and lifecycle grant.
`workflow_profile_transition` is an optional immutable typed value in `LocalWorkItemState`:
original startup operation/ref digest, actual startup request when available, conversion operation
ref, reason digest and original generated-checkpoint reference. Absence preserves old sealed bytes.
Its fixed direction is lightweight to standard; the startup request and original checkpoints are
never rewritten. The transition is carried inside existing sealed frontier transfer, not a new
identity store or a synthesized target startup.

The operation uses the original startup proof as its idempotency identity, preserves every other
state field and commits through the existing kernel CAS and Git audit effect. It shares the
publisher transaction lock and checks the exact prior lightweight completion outbox request;
unknown terminal effects are read back, not overwritten. Later kernel mutations and reinit retain
the typed proof. Original audit-delivery status changes are not part of the immutable startup
proof digest. Source records are validated when present; a receiving Host consumes the already
sealed portable frontier without manufacturing the source's operation history.

ResumePacket exposes separate startup/effective fields and the original conversion ref. Only
startup-generated lightweight guidance is superseded; later business actions remain intact.
Stats v2 evidence carries the same transition, accepts old v1 observations, and merges a promotion
only against the same original startup identity. Both lightweight completion entry and canonical
proof replay reject the transition before a terminal change.

## 5. Entry, reopen, checkpoint and replacement

### 5.1 `/wi <wi>`

Entry reads the local kernel and returns one of:

- `execute_fenced`: same-site execution with a valid external fence;
- `execute_local_isolated`: local continuation, external effects withheld;
- `observe_candidate`: frozen candidate remains current and no execution authority is granted;
- `source_identity_unresolved`: a readable logical source lacks sufficient Host attachment evidence;
- `source_session_active`: a distinct source/successor has positive live runtime readback;
- `resume_replacement`: an existing replacement operation is continued;
- `human_decision_required`: only when competing authority or ambiguous user intent cannot be
  resolved mechanically;
- `local_material_unavailable`: neither local kernel/snapshot nor workspace material exists.

Remote freshness and replication appear as capability/effect facts, not entry blockers.

#### 5.1.1 Attach an imported logical source

When a current-format operation created the binding, entry resolves through its durable source
attachment. When a legacy resident-surface import has no such evidence, entry remains read-only and
returns `source_identity_unresolved`. It does not compare the logical surface token directly with a
provider-derived ref and infer another live Session.

The human-facing adapter or contextual Session Agent can submit
`attach_current_host_to_source(logical_source, host_session, rationale)`. In one transaction the
kernel validates effective actor/scope authorization and compares epoch, surface/runtime, source,
checkpoint, candidate and replacement digests; rejects an existing incompatible attachment; and records the join. Epoch,
candidate, logical source and responsibility remain unchanged. The same operation replay is a
no-op. This operation repairs identity continuity only and cannot replace `reopen` or rollover.

#### 5.1.2 Recover an applied successor attachment

`replacement_stage=applied` is a stable active frontier, not an in-flight replacement. Claim and
apply may already be durable when a Codex Hook identity and the later standalone-client identity do
not compare equal. The first response still reports the committed apply. A subsequent explicit
`/wi continue` may rebind only the current epoch's Host attachment when all of these hold:

1. the WI is active and the Agent has effective execution rights for the binding scope on the
   same runtime/surface; result ownership is not required;
2. the replacement is already `applied`, with exactly one logical source and current attachment;
3. the new Host identity is not attached to an earlier epoch of this WI;
4. no target reservation or claim is in flight;
5. the contextual Session Agent supplies a bounded rationale and the exact frontier digest is
   unchanged.

The rebind is its own idempotent operation. In the same transaction it archives the superseded Host
attachment in durable history and updates only the current source-attachment projection; prior
operation payload digests remain unchanged. The history prevents a same-epoch identity from
reclaiming the frontier later. The operation does not change epoch, checkpoint content, logical
source, owner/executor, candidate, authority or replacement result. After the transaction the
client reruns entry plus worktree/Git consumer readback; only that complete response may return
`continue_ready`. An identity attached to an older epoch is the fenced source and cannot rebind the
successor using retired credentials. These are continuity checks only: a valid new human grant
follows section 0 and may authorize a new binding without approval from the retired binding.

### 5.2 Candidate withdrawal

Candidate withdrawal is a lifecycle decision made by the human or by a Session Agent acting within
its delegated context. The program cannot know a party's intent. It receives an asserted decision
such as `withdraw_current_candidate_and_continue_same_wi`, together with a rationale and audit-only
submitter/session provenance; none of those fields is a machine proof of intent. The ordinary path
uses the current source binding. A recovery path is also required for the observed legacy shape
`awaiting_verification + active_executor=null + binding points to another or unavailable Session`.
In that shape only, an explicit decision submitted through `/wi reopen <wi>` or the canonical client
may atomically commit `withdraw_candidate_and_adopt_source`:

1. compare the exact candidate, owner, binding, replacement and local-frontier digests;
2. preserve the candidate and all checkpoints as immutable history;
3. advance the epoch and bind the current Host-derived opaque Session identity;
4. clear the candidate, realize the decision's authorized execution scope, preserve the WI-wide checkpoint
   sequence and clear only the current checkpoint pointer;
5. force the local frontier to `local_isolated` and enqueue audit/authority-adoption effects;
6. fence the prior Session immediately in the local projection.

This is not implicitly authorized by ordinary `/wi` or a discovered historical registration. The
legacy shortcut above only covers its stated shape. Other shapes use the effective grant and
minimum-change path in section 0; old owner/executor mismatch cannot veto a valid new decision.
Changed preconditions require readback and replanning of the same decision. A conflicting writer
or incompatible surface is a realization dependency, not proof that human authorization is absent.
The kernel verifies authorization provenance and scope, not a caller-supplied claim of human intent. A Session Agent with clear
conversation context submits the decision; if context is ambiguous, that Agent asks the human
before calling the kernel. No `human_confirmed` flag or `human_intent_ref` can replace that judgment.
Retry reuses the same operation identity derived from the decision and exact frontier.

`@anchor manual frozen-source-adoption-uat :: installed true-shape fixture proves explicit reopen from a different Session advances one epoch, preserves history, enters local-isolated, enables checkpoint and rollover, and leaves ordinary entry unable to take over`

Historical source usage seals read the corresponding immutable apply operation and its
operation/epoch attachment history. A later preparation or attachment rebind cannot invalidate an
earlier completed local retirement. Authority adoption readback has a resident consumer: only
exact successful audit publication plus attachment facts may clear the pending transition.

### 5.3 Checkpoint

Every mutator derives its next state from one transactional state/authority/token snapshot and
retains that token through probes and commit. A competing frontier change rejects the commit;
refreshing the token separately from the derived state is forbidden.

Checkpoint allocation and content persistence occur in one local transaction. Publication and
replication are effects. New decisions are therefore recoverable even when every remote surface is
offline. A checkpoint may honestly say that some external facts are stale or unknown.

Interactive checkpoint begins with sealed orientation. Only when the packet grants source
authority and exposes the exact frontier does the current Session Agent construct the semantic
draft from its conversation. `checkpoint-local` commits that draft. Channel adapters never call a
legacy checkpoint writer or synthesize decisions from status output.

### 5.4 Session replacement

Replacement is one composite operation with durable stages:

```text
source_active
  -> checkpoint_committed
  -> successor_slot_reserved
  -> target_claimed
  -> target_readiness_and_live_runtime_read_back
  -> replacement_applied_locally
  -> source_retirement_and_usage_seal_effects_settled
```

The source cannot name a Session that has not been created yet. Preparation therefore records one
target-neutral successor slot under the source fence. A fresh Session claims that slot using its
host-derived opaque identity. The claim grants only replacement coordination; the target receives
ordinary execution authority after its target readiness response is digested and exact
epoch/runtime/surface/workspace readback says it is ready. A runtime settled receipt and observed
business continuation are separate later evidence; a control response hash proves neither.

The public `enter` / `enter-local` call is the composite boundary. Inside that call, the client
claims once, builds the exact first machine result in memory, digests those canonical bytes, performs
live readback and applies the replacement. Only the final envelope crosses the public process/API
boundary. Raw claim/confirm refs and digest parameters are not public Agent workflow. If the process
dies before final output, retry resumes the durable claim and refreshes target readiness; it does not replay a previously emitted intermediate response because none is
emitted.

Before local activation, the source remains usable for work or recovery. The stages are
monotonically recorded under stable operation ids; retries by either Session reuse the same slot and
claim rather than create a second target. For a same-authority replacement whose registration digest
is unchanged, workspace selection and Harness-store routing are deliberately separate operations.
A multi-repository registration selects the one linked worktree whose registered remote matches the
canonical Harness repository's exact Git identity; this identity-only selection does not require the
canonical `.harness` relative path to be materialized in that sparse worktree because no ledger write
occurs there. A new/independent authority adoption continues to require `resolve_store_route` and its
full physical path checks. After local activation, the old Session is fenced from new work.
Provider-specific usage seal and audit publication remain effects: missing receipts keep the
replacement globally pending, but do not roll back the proven local target or strand ordinary local
work. No step requires the user to copy an internal identity.

Interactive rollover uses the same orientation and Session-Agent draft boundary, then calls
`rollover-local`. Relay does not invoke a parallel legacy coordinator. Before mutation, the client
checks that the imported continuation/replacement view agrees with the binding; disagreement is
`reconciliation_required` and cannot create another successor slot.

Checkpoint prose and lifecycle orientation are separate data. The checkpoint field
`business_next_action` means the first domain action after a successor is executable. ResumePacket
projects a machine-owned `lifecycle_next_action` while reservation/claim coordination remains, and
projects `business_next_action` when execution authority is active. It never removes sentences from
human prose or guesses which words describe lifecycle.

For candidate observation, the same projection reads the kernel's final-review freeze. A frozen
review points to continuing the existing publication and settlement; other candidates point to
the current verification/review records. Neither path requests a fresh human decision merely
because the lifecycle status remains `awaiting_verification`. Effect admission stays independent.

Before first formally creating or starting a standard workflow, the Agent presents the proposed
route and asks for explicit human confirmation. Workspace preparation, read-only investigation and
a discussion sketch remain available. Checkpoint metadata records the bound Agent's report of the
actual response and proposal; it is not a Host-verified authorization or a filesystem permission gate.
Absent alignment, both checkpoint and ResumePacket guidance request that conversation and retain the
proposed business action separately. Normal checkpoints and cross-Session readback inherit aligned
metadata. New optional draft fields are omitted when unused to preserve existing retry identities.
Existing checkpoints are never rewritten to manufacture a missing confirmation.

The projected `business_next_action` is durable recovery context, not a command that can override
newer human conversation. Every continuation or reinit consumer reconciles it with the latest
authenticated human instruction and current authorization scope before dispatch. A newer instruction
wins without rewriting immutable checkpoint history; if no newer business direction exists, the
checkpoint remains the fallback frontier.

ResumePacket is a lossless semantic projection of the checkpoint frontier, not a prose-only
summary. It carries artifact refs in their durable order and preserves each ref's repository,
repository-relative path, revision, digest and role. Fresh replacement apply, a bounded Host
attachment rebind and verified read-only snapshot recovery all use the same projection function;
none may silently replace a non-empty artifact set with `[]` or omit the field. Invalid durable
artifact material fails the projection instead of returning an apparently complete frontier.

`@anchor symbol project_checkpoint_fields`

`@anchor manual entry-replacement-uat :: true-shape UAT interrupts each stage and proves exact retry, one target, no lost checkpoint and no false completion`

### 5.5 `/wi reinit <wi>`

Reinit records a real Host human decision independently of the old executor's permission.
The normal route preserves WI definition, checkpoints and effects, then adopts only the new
execution grant and affected binding. If the active kernel row is absent but the canonical WorkItem
still exists, the product first projects only its title, responsible owner and acceptance criteria
as an unbound baseline; progress/checkpoint fields remain unknown/empty until normal business work
records them. This is canonical adoption, not full reconstruction. The human decision is durably
committed before registration discovery, workspace allocation, Git probing or consumer readback.
Missing registration leaves the decision committed with a `scoped_grant_realization` effect; the
same decision resumes when exact registration is available. A Host-resolved routing repository may
materialize the standard sparse managed-workspace allocation during realization; it is technical
routing context, not authorization or new business semantics.
Resource locks are acquired independently. A grant may have `ready_resources` while other
resources remain pending. A scoped decision supersedes overlapping resources while preserving
unrelated grants and existing locks.

Public broker guard, installed Git hooks, worktree launch and workspace admission resolve current
permissions through `scl_authorization`. Explicit grants take precedence over compatibility binding;
legacy binding resolves only with the exact physical Host attachment. Owner names and allocation
basenames never establish execution identity. An absent `HARNESS_BRANCH_REF` is machine context:
read the branch from the sealed registration and exact live Git identity. A supplied conflicting
branch, Agent, WI, Session or surface remains rejected. Protected branches still require a validated
`BROKER_TX_REF`; grants cannot substitute for that transaction.

The shared registration reader authenticates and decodes the kernel-admitted raw bytes against
its current fence. It ignores a legacy stored parsed copy; new admissions retain only the original
and its decision metadata. Worktree load, validation, repeat registration and resource consumers
use this original even if the external registry is absent, malformed or contains different facts.
The registry writer atomically rebuilds that disposable view under its existing lock without
creating a damaged-copy ledger. Original schema, digest, WI/surface and live Git checks remain;
a redirected or nonregular destination is still rejected. Historical JSON-sealed registrations
without raw bytes retain their original full-payload digest contract.

Reinit retains previous resource evidence in its kernel operation metadata, so realization needs
no second side archive or old/current projection parity. Unadmitted resource proposals and independent
verification carriers retain their original identity checks. Existing local kernels own their WI
regardless of whether a managed-lineage pointer has been installed. Resource-original reads use
the exact resource fence without first decoding the whole WI execution state; that read grants no
execution permission and lets an explicit human reinit reuse intact originals during state repair.
A cross-host capsule awaiting target apply has no admitted local resource record: its reserved
local proposal still supplies placement-conflict evidence and cannot grant execution.

Readback executes the installed `pre-commit` and `pre-push` wrappers with the observed Host environment. It does
not inject an Agent or branch, invoke the coordinator's interpreter in place of the installed one,
or create a commit. The hook receipt must identify `effective_execution_permission/v1`; missing,
failed or incompatible interpreter/hook readback remains degraded. Hook wrappers retain conflicting
caller context for rejection. Identity reads avoid Git optional index writes and `write-tree`.
Normal commit/push scope checks retain revocation, partial realization, excluded/delegated scopes,
current Host binding, publication fence and unknown prior publication constraints. Reinit accepts
and retries the same signed decision independently from this consumer readiness result.

An explicit `withdraw_candidate_and_continue` disposition in the signed scope clears a frozen
candidate in the same bounded request. A binding-only decision cannot withdraw it, and neither
route implicitly reopens terminal work. Full frontier reconstruction remains available when its
business definition is actually unavailable, with its existing complete-draft validation.

For an unreadable kernel and snapshot, the isolated public route consumes `HumanRecoveryScope`
before kernel initialization. Host signed-decision files preserve authorization evidence only;
they contain no parallel WI frontier or external-effect outcomes. Scope binds exact Session,
surface, recipient, action-lock root, resource paths and exact Git worktree identities.
The default requires a linked non-primary workspace; an explicit signed
`allow_primary_local_progress` permits the original primary workspace only for read/write/test,
without commit or publication. Acceptance
is stored before independent physical probes. `worktree launch --human-decision` holds the original
resource locks while a real local process runs. Git guards reject out-of-scope commits and all
publishing. No check opens the damaged lifecycle database, resets it or switches healthy WIs.
An already-running managed child watches WorkItem, registration and source-attachment state through
SQLite read-only connections. These reads include committed WAL state, but never create a missing
database, change journal mode, or checkpoint the WAL on observer close. Read failure remains unknown;
only an explicit current superseding decision retires that child.


Observer isolation is checked with private databases and committed WAL:
`@anchor test agent-harness/tests/test_scl_observer_readonly.py :: test_observer_close_preserves_damage_with_pending_wal`
`@anchor test agent-harness/tests/test_scl_observer_readonly.py :: test_observer_does_not_recreate_missing_database`
`@anchor test agent-harness/tests/test_scl_observer_readonly.py :: test_observer_opens_readonly_without_ignoring_wal`
The response states which resources are usable and explicitly withholds checkpoint, publication,
global exclusivity and runtime/business completion claims. The execution mode is an explicit
client flag; normal rejoin reuses the immutable signed decision. An atomic accepted-input index
retains disjoint scoped decisions without consulting historical files. A new signed exact-digest
override can preserve and supersede an unreadable index; it does not resolve unknown effects.

Normal rejoin records adoption requests only after the accepted grant commits, then realizes
resources under their original locks. Per-resource completion receipts plus current ready grants
admit normal actions and retire the corresponding isolated actions, without deleting input history.
Both managed and isolated launchers supervise their own process group: explicit overlapping
supersession stops and reaps that group before releasing its resource locks. Kernel unreadability
alone is not revocation. Terminating a child does not prove an external send failed or was undone.

`@anchor manual reinit-state-machine :: copied split-frontier fixtures prove human authority bypasses corrupt historical permission, no half-draft becomes live, and success implies real installed consumer readiness`

## 6. Capability projection

Capability results use this shape:

```json
{
  "name": "external_publish",
  "state": "pending",
  "reason_code": "credential_unavailable",
  "as_of": "2026-08-29T00:00:00Z",
  "source": "effect:<opaque>",
  "retry": {"automatic": true, "next_attempt_at": "<timestamp>"}
}
```

Allowed states are `available`, `pending`, `unavailable`, `conflicting` and `not_applicable`.
`pending` means the requested action is durable and expected to recover. `unavailable` means a
current prerequisite truly does not exist. `conflicting` means automation cannot select one safe
winner. A capability projection is never used as a substitute for the underlying receipt.

The overall response chooses the least misleading operation status. For example, a committed local
checkpoint with failed GitHub credentials is `local_committed`, not `failed`; its
`external_publish` capability is `pending`.

Entry performs the same bounded Git adapter dependency probe used by reconciliation. A reachable
registered remote yields `available`; an unreachable dependency yields `pending` with automatic
retry; authority or registration ambiguity remains a distinct fenced reason. Probe exceptions are
contained to the external capability and cannot erase safe local execution.

`@anchor manual capability-envelope-review :: schema contract tests reject missing freshness, source, reason and retry fields and distinguish pending from unavailable/conflicting`

## 7. Machine envelope and hook behaviour

Every local client writes exactly one JSON object to a dedicated machine stream or file descriptor:

```json
{
  "schema": "scl_response/v1",
  "request_id": "<opaque>",
  "operation_id": "<opaque-or-null>",
  "status": "local_committed",
  "work_item": {"id": "<wi>", "local_revision": 4, "global_revision": 2},
  "capabilities": [],
  "effects": [],
  "error": null
}
```

Parser and preflight errors use the same envelope with `operation_id=null`. Exit codes are coarse
process signals; consumers rely on schema and stable error codes.

The Codex hook follows this algorithm:

1. if the prompt is not a recognized lifecycle command, allow unchanged; `/wi continue` and
   `/wi reinit` are first-class verbs and cannot be silently normalized to ordinary `/wi`;
2. unwrap at most one matching bounded inline-code fence around the complete single-line command,
   then normalize only presentation escaping within the parsed command token grammar;
3. locate the client in the same sealed installed environment as the Hook; never resolve a venv
   interpreter into its shared base prefix and never repair discovery by writing into that prefix;
4. on a kernel miss, request one bounded hydrate of that exact WI from the configured canonical
   local source before classifying it as absent; never scan repositories, registrations or hosts;
5. resolve the current Host Session through a durable source attachment; an unresolved join is
   `source_identity_unresolved`, not proof that another Session is active;
6. forward the explicit lifecycle decision, rationale and audit-only submitter/session provenance;
7. call the local client once with a short budget; the client privately performs claim, exact
   first-result capture, readback and confirm/apply before returning one final envelope;
8. on a valid final response, inject the bounded ResumePacket or result and allow the prompt;
9. on timeout, import failure or invalid response, allow the prompt and inject a degraded warning
   plus a local request id if one exists;
10. never manufacture success, never swallow the user's message, never launch later-phase work.

After a degraded Hook, the Session Agent that received the command can resubmit the same lifecycle
decision through the canonical client because it knows the conversation. The user does not repeat
the command or provide an internal reference. The client does not decide intent; correctness comes
from the submitted decision, exact state preconditions, idempotency and fencing. The Agent never
emulates replacement confirmation or supplies a first-result digest.

Relay follows the same one-call rule, with a host adapter around it:

1. the channel resolves its authorized Agent and forwards the normalized command to the shared
   ingress;
2. the host adapter reads the current Relay runtime/session observation without starting a dormant
   process and resolves the sealed client paired with the active immutable Relay release;
3. the sealed client hydrates an exact cold-miss WI as one parity unit and resolves source identity
   through durable attachment evidence before returning orientation;
4. a read-only plan response decides whether the observed runtime needs activation, no host runtime
   is needed, one target runtime must be created, or contextual source attachment is required;
   planning may import a legacy frontier but cannot claim/apply replacement. A no-authority plan is
   returned unchanged before provider, usage-binding or runtime-start checks;
5. checkpoint and rollover orientation is delivered to the current authenticated Session Agent,
   which supplies the semantic draft to the same sealed client; the channel handler never switches
   to a legacy writer;
6. when a target is needed, Relay durably records the host plan, starts the exact target without a
   business turn and obtains live runtime readback; one mutating client entry then privately claims,
   captures the canonical first result and applies against that readback;
7. Relay supplies the final ResumePacket to the exact runtime and records dispatch readback; a failed
   dispatch does not rewrite the kernel result or ask the user to repeat the lifecycle intent;
8. for rejection or degradation, Relay schedules mechanical recovery where permitted and logs the
   safe diagnostic; its human presenter leads with whether anything changed, whether the durable
   frontier is safe and exactly one next action. Raw enum dumps are optional diagnostics;
9. Feishu and Telegram render that same channel-neutral result without changing lifecycle meaning.

No presenter may turn ref inequality, missing attachment or unknown liveness into the sentence
"this WI is active in another Session". `source_session_active` is renderable only when the result
contains positive live readback for a distinct attached source or successor.

Release rollout is a three-node transaction set, not a distributed atomic fiction. Each node stages
the exact Relay artifact and compatible SCL artifact, verifies both manifests and handshake, then
atomically activates its local pointers. A failed node stays on its LKG and is reported partial;
successful nodes are not rolled back merely to manufacture identical availability. Every actual
post-activation bot route remains unaccepted until its own visible readback is present; an absent
route remains explicit and is not populated by a duplicate long-poller for test convenience.

`@anchor manual machine-hook-blackbox :: installed hook tests run the exact production discovery environment without override, assert no shared-prefix mutation, and prove allow semantics plus one bounded final response for recognition, parser, timeout, import, execution and retry outcomes`

## 8. Reconciliation state machine

```text
pending -> leased -> succeeded
   │          │
   │          ├-> unknown -> readback -> succeeded | retry_wait | conflicting
   │          └-> retry_wait -> pending
   └-> cancelled  (only when a superseding operation proves the effect no longer applies)
```

Each worker leases one effect immediately before processing, renews the exact owner/attempt
before expiry, and validates that lease before sending and settling. An OS-held per-effect lock
prevents another worker from declaring an in-flight send absent after logical lease expiry.
Expired attempts become unknown and require readback; unknown is never interpreted as not sent.
Leases have an owner and expiry so a crashed reconciler cannot hold an effect forever. Backoff is
bounded and jittered by effect class. Authentication failures retry after credential-change signals
and a slow timer; network failures retry on connectivity change; disk pressure triggers local
compaction/reserve recovery; runtime activation failure selects last-known-good.

Cancellation never deletes evidence. It records the superseding operation and is prohibited after
an irreversible remote effect might have occurred unless readback proves absence.

On startup the reconciler:

1. validates database integrity and schema;
2. releases expired effect leases;
3. repairs operations committed without derived projections;
4. readbacks `unknown` effects before retry;
5. rebuilds stale disposable views;
6. processes due effects under per-provider and per-WI limits.

Repository selection occurs after an effect is leased and is repeated from its durable
registration, so resident process configuration cannot pin all effects to the repository from
which that process happened to start. Connectivity restoration wakes the same queue; attempts
retain their original operation, effect and fence identities.

`@anchor manual reconciler-restart-contract :: kill tests at every state prove expired leases recover, unknown effects read back first and cancellation retains evidence`

## 9. Storage and installation

The site state root contains:

```text
state/
  kernel.sqlite3
  kernel.sqlite3-wal
  runtime/
    active -> releases/<digest>
    previous -> releases/<digest>
    releases/<digest>/manifest.json
  reserve/emergency.bin
  projections/<consumer>/...
  diagnostics/...
```

Activation verifies manifest, file digests, importability and a local protocol handshake before an
atomic pointer switch. The previous verified release is retained. The resident service opens its
own release by digest; editable source paths cannot shadow it.

The formal Harness installer stages a digest-addressed canonical SCL wrapper that targets the exact
new immutable Harness release, handshakes the staged wrapper, atomically swaps `runtime/active`, and
handshakes through the active pointer before returning. The receipt contains both the outer Harness
release and inner canonical SCL digest/revision. Runtime reclaim treats outer releases referenced
by valid canonical `active` and `previous` manifests as protected. A no-op Harness install still
repairs and verifies a dangling canonical pointer.

Emergency reserve size is configurable within a safe bound. On ENOSPC, the kernel first removes or
truncates only its own reserve, finishes the smallest recovery transaction, then recreates reserve
after capacity returns. It may evict regenerable projections and expired diagnostic caches. It does
not remove journal pages, unresolved effect evidence or user files.

SQLite corruption, schema incompatibility or unrecoverable local storage is a distinct incident:
clients fall back to a verified read-only materialized snapshot where possible and permit the
conversation. No write success is reported until durable recovery completes.

`@anchor manual storage-installation-drill :: isolated install tests verify digest-pinned imports, atomic fallback, reserve ownership and read-only degraded entry after unrecoverable corruption`

## 10. Migration and lineage cutover

The corrective release does not continue an unprovable mixed lineage. Installation first:

1. inventories the prior kernel database, projections, authorities, attachments, leases and effects
   without editing them;
2. writes and fsyncs a checksum-addressed retired-lineage manifest;
3. disables every prior-lineage lease and resident scheduling source;
4. atomically switches the live-lineage pointer to an empty compatible kernel;
5. exposes old WIs as `reinit_required`, never as absent or implicitly active;
6. retains Git work, legacy ledger bytes and all audit history unchanged;
7. accepts new live WI state only through `reinit` or a proven clean new-lineage creation.

A cold miss first checks the retired-lineage manifest. A known retired WI returns
`reinit_required`; it is never hydrated as current state. Only a WI proven to have been created or
reinitialized in the active lineage may run bounded, idempotent `hydrate_frontier(wi_id)` against
its exact configured canonical source. That hydrate imports the active-lineage WI, binding,
checkpoint, replacement and writer authority atomically. Real absence is returned only after both
the active lineage and exact canonical source say it is absent. Changed bytes or evidence of two
active-lineage writers return `reconciliation_required`; they do not select a historical winner.

Legacy logical source identity may be consulted as evidence while the Session Agent constructs a
reinit draft, but it is never imported as current authority after lineage cutover. Every installed
lifecycle verb writes only through the new kernel; legacy readers are compatibility materializers
and cannot advance an independent epoch, checkpoint or continuation.

If cutover fails before pointer activation, the old installed runtime and lineage remain active. If
it succeeds, rollback may change runtime only to a version that understands retired/new lineage
separation; it may not reactivate old effects or legacy execution authority.

Rollback changes runtime, not storage authority. A previous runtime must understand the current
journal schema or use a forward-compatible read/write shim declared in the release manifest.

`@anchor manual migration-rollout-drill :: per-WI canary, failed import, mixed-version access and runtime rollback are exercised with byte-identical legacy fixtures`

## 11. Failure classification

| failure | automatic response | local work | global effect |
|---|---|---|---|
| hook/import/runtime candidate failure | allow prompt; use local API or LKG | available | unchanged/pending |
| GitHub permission/network failure | retain effect and retry | available | pending |
| remote CAS advanced compatibly with the exact registered worktree frontier | durably record compatibility readback, requeue and publish on the observed remote head | available | retry |
| remote product history is absent from or diverges from the exact registered worktree | preserve both; request resolution | isolated | conflicting |
| competing authority generation | preserve both; request resolution | isolated | conflicting |
| historical registration ambiguity | use exact bound id; quarantine drift | available if exact binding valid | pending if repair needed |
| existing canonical WI missing from cold kernel | bounded exact-WI hydrate; prove parity | available after import or read only | unchanged/pending |
| logical source lacks Host attachment | request contextual attachment; do not infer liveness | read only until joined | unchanged |
| kernel and legacy frontiers disagree before cutover | seal old lineage; require human reinit | read only | retired |
| old WI after successful lineage cutover | return `reinit_required`; if canonical business definition exists, adopt it deterministically and apply the current human grant; discuss semantics only when that business definition itself is unavailable | conversation/read only until authorized; then capability-specific | unchanged/pending |
| reinit workspace/consumer readback incomplete after local commit | preserve new frontier | available, isolated | unavailable |
| endpoint dirty/unreachable | do not touch endpoint; retry adapter | available | partial/pending |
| process crash | WAL recovery; resume same operation | available after restart | resume |
| disk pressure | reclaim own reserve/cache; compact | available if durable write recovers | pending |
| true local persistence loss | read-only snapshot and truthful error | conversation/read only | unavailable |
| missing remote-only source bytes | state material unavailable | unavailable for that artifact | pending |

The Git audit adapter creates its deterministic receipt commit in a detached, adapter-owned
worktree. That commit uses an empty hooks directory: a human-worktree pre-commit probe is bound to
the registered checkout's index and therefore cannot validate the detached index. This exemption
does not bypass publication authority. The adapter has already revalidated the exact registration
and lifecycle fence, stages only the immutable receipt path, and the subsequent push continues to
run the registered pre-push guard.

No row authorizes resetting, cleaning, stashing, overwriting or silently committing unrelated work.

`@anchor manual generic-failure-matrix :: each row has a provider-neutral fixture and asserts preserved user bytes plus unaffected capability availability`

## 12. Invariants

1. At most one globally accepted authority generation can publish a WI operation.
2. A locally committed operation is never lost because an effect failed.
3. An external effect is applied at most once per operation/fence, or readback proves the same
   receipt before success is recorded.
4. A source remains recoverable until its replacement is proven usable.
5. Historical registrations cannot become current by scan order.
6. Pending is never projected as completed.
7. Local isolated progress is never projected as global canonical truth.
8. Unrelated dirty bytes are never changed by lifecycle recovery.
9. Hook, skill, model and provider implementations are replaceable clients, not authorities.
10. Every user-visible result can be traced to durable local evidence and, where claimed, external
    readback evidence.
11. A distinct active source is projected only from positive live readback, never from ref
    inequality, missing attachment or unknown state.
12. Every installed lifecycle verb reads and writes the same per-WI frontier through one writer.
13. A canonically existing WI is not reported absent until one bounded exact-WI hydrate has either
    imported a parity unit or returned truthful reconciliation evidence.
14. Repairing a logical-source attachment cannot advance epoch, withdraw a candidate, reserve a
    successor or transfer execution responsibility.
15. A retired lineage can never lease an effect, grant authority or become current through hydrate.
16. Reinit authorization never depends on the historical fields it replaces.
17. Reinit applies an authenticated human decision to the smallest necessary control/resource
    scope. A canonical WI with a missing active kernel row is deterministic adoption, not business
    reconstruction. A complete human draft is required only for an explicitly authorized full
    rebuild when the canonical business definition is truly unavailable or intentionally replaced;
    worktree/runtime/Git consumer readiness gates capabilities, never the existence of authorization.
18. Continue success implies the fresh Session has the complete checkpoint, including every ordered
    artifact ref, and usable local execution authority; `local_isolated` is truthful success for
    local work, never proof of external mutation authority.

`@anchor manual resilience-invariant-model :: property tests generate concurrent operations, partitions, crashes, reinit cutovers and retries and check all eighteen invariants after every step`

### Adopt delivered closeout without reopening the producer

The terminal adapter validates the original Broker `closeout complete` transaction, the bound convergence aggregate and fixed-SHA receipt, Git publication ancestry, the committed terminal WorkItem and unique completion event, and the current canonical candidate. It reads only the original service repository and retained receipts; producer workspace retirement is irrelevant. The client exposes `adopt-terminal-local <wi> --transaction-ref <tx> [--dry-run]` with an explicit service `--harness-root`.

The kernel performs one compare-and-swap local commit with no external effects: terminal state and observed global revision, retired authority/registration, cancelled pending effects, and an idempotent proof-bound operation. Existing checkpoints and audit records remain. No effect worker is awakened for this operation. A terminal resume projects `observe_terminal` and no producer execution or external publication capability. This is a typed delivered-result adoption, not a general legacy YAML refresh.

### Host decision reference wire v1

`human_decision_v1_` identifies SHA-256 of compact ASCII-escaped JSON for the ordered
string tuple `[channel, event_id, human_ref, work_item_id]`. Every non-ASCII UTF-16
code unit and DEL is escaped as lowercase `\uXXXX`; non-BMP characters use a
surrogate pair. Standard JSON control/quote escapes apply; no Unicode normalization
is performed. This preserves existing Python-generated IDs exactly. The UTF-8 HMAC
payload and existing Host input receipt filenames remain separate and unchanged.
Consumers MUST reuse original signed inputs and accepted references on technical
retry; they MUST NOT re-sign, rename historical decisions, or ask for new human
approval to repair a reference serializer mismatch. JSON input is parsed as JSON;
legacy YAML input remains supported. Unicode and historical-input replay tests
must pass across the actual JavaScript issuer, Python consumer and Relay readback.


### Explicit publisher adoption without moving the WI

A migrated canonical reader does not migrate an active WI's Git publication target or base.
`adopt-workspace-authority-local --publication-repository <registered-repo-id>` binds one
already registered publisher in the existing workspace manifest and rotates its fence to that
repository's verified current HEAD. The original actor/Host attachment, checkpoint, business
state, resources and both repository histories remain. The selected publisher survives later
workspace adoption and authority rotation; an unselected legacy multi-repository registration
remains ambiguous rather than being resolved by file order, cwd or a global repository name.

The operation uses the existing registration lock and kernel CAS transaction. It verifies the
expected kernel fence, exact Git identity and registered-base ancestry, refuses retired
canonical destinations, and records original target/base and effect evidence in its immutable
operation. Its projection can finish after interruption through the same idempotent request;
a newer kernel resource generation or source is not overwritten. Registry bytes are a disposable
view, so their loss or divergence does not require a new adoption. The legacy expected-registration
argument remains part of request identity only; it is not a second compare-and-swap authority.
Extension and replacement use the same rule and do not veto replay after an unrelated checkpoint.

Unsettled audit effects may change to this adopted target only when their complete attempt
history proves no apply reached the provider: pending with zero attempts, or complete recorded
pre-apply dependency/probe/plan failures. The inventory is checked again in the same transaction
as the fence change. Leased, unknown, previously sent and non-audit unsettled effects retain
original meaning and prevent this change until their original outcome is resolved. No receipt,
request identity, attempt history or business state is removed or falsely marked successful.
The resident reconciler publishes the same original audit requests after adoption. Local work
and checkpoint capability remain separate from publication readiness.

A `candidate_publication` from an older frozen frontier is never redirected to a newly selected
publisher. If a later explicit candidate withdrawal or human-authorized reinit supersedes that
frontier, the resident reconciler may settle the old candidate effect as `cancelled` only after
durable local attempt history or the canonical Git Broker proves that no provider-side
prepare/publish/integration/replication effect occurred. The settlement records a readback proof
and does not rewrite the candidate operation, mutation outbox, checkpoint, epoch or current
frontier. An unknown, prepared, pushed or otherwise possibly sent candidate remains a blocker.
Only after that independent settlement may the existing unsent Git-audit effects participate in
publisher adoption under the rules above.
