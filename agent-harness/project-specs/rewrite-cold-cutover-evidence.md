# WI018 冷切换与生产读回

固定生产水位：`053bea15b8f4674145f5e5e615d90d265d6b09bd`。首发节点为 ownermac；TCloud、Muse 后续采用同一候选。
2026-10-14 18:23:52（UTC+8）已按人类“切换、ownermac 优先”采用 `2b69b38fb50fc905f45829463a127ebe70bc3c13`。Relay PID 15225、核心、ComputeCost 与 Stats publisher 已切换；Stats App 仍为 `33d6d62f`，历史计量回填仍 pending，完整真实联调与稳定窗口未验收。以下准备段落是切换前的历史证据，不再代表当前部署状态。

正式读回：`wi018-cold-cutover/executed/ownermac-activation-readback.json`、`four-components-current-readback.json`；安装事务在 `~/.local/state/pulse-release-writer/native-attempts/2b69b38fb50fc905f45829463a127ebe70bc3c13.json`。ownermac、TCloud、Muse 的旧写者冻结已有各自回执；后两节点的运行版本尚未升级，Vultr 本轮未操作。标准 1–2 仍未全部完成。

第四轮复核确认：旧执行回执可能仅在登记分支/另一已登记仓库；原 JSON 排版差异不是决定冲突。904、070 等原回执已通过只读来源复核，原会话接续仍须由其真实消费者完成。另确认 CC `/wi` 没有自动核心调用，以及 Relay 原输入保管路径与核心配置不一致；原取消 `channel.842e139d5c6ecb62a7e84a69b65ad56c26dcec9632bcc991bd15d8faf75e2e6d` 保留，本线程不代取消 WI340。修复采用记录须另读实际安装回执，不以本文件的源码状态冒充部署。

## 存量处置计划

共 23 个生产非终态 WI：21 个旧记录、2 个已接纳新日志。保留原 ID、原 owner、原候选及未结义务。本表不授权将未完成工作标为完成或取消。

| WI | 原 owner | 生产状态 | 处置 | 本地观察节点 |
| --- | --- | --- | --- | --- |
| wi_20260820_028 | amanda | in_progress | 同 ID 接纳原事实 | 没有本次本地记录 |
| wi_20260820_358 | duke | in_progress | 同 ID 接纳原事实 | 没有本次本地记录 |
| wi_20260820_568 | pulse | in_progress | 同 ID 接纳原事实 | ownermac |
| wi_20260822_730 | pulse | blocked | 同 ID 接纳原事实 | 没有本次本地记录 |
| wi_20260823_184 | pulse | in_progress | 保留新日志 | tcloud |
| wi_20260824_615 | duke | in_progress | 同 ID 接纳原事实 | tcloud |
| wi_20260826_825 | hestia | awaiting_verification | 同 ID 接纳原事实 | ownermac |
| wi_20260827_520 | aki | in_progress | 同 ID 接纳原事实 | ownermac、tcloud |
| wi_20260827_799 | pulse | in_progress | 同 ID 接纳原事实 | 没有本次本地记录 |
| wi_20260831_013 | hestia | in_progress | 同 ID 接纳原事实 | ownermac |
| wi_20260906_779 | pulse | in_progress | 同 ID 接纳原事实 | ownermac |
| wi_20260907_326 | emi | in_progress | 同 ID 接纳原事实 | ownermac、tcloud |
| wi_20260908_843 | aki | awaiting_verification | 同 ID 接纳原事实 | ownermac |
| wi_20260922_018 | pulse | in_progress | 同 ID 接纳原事实 | ownermac |
| wi_20260923_228 | amanda | draft | 同 ID 接纳原事实 | ownermac |
| wi_20260924_219 | hestia | awaiting_verification | 同 ID 接纳原事实 | ownermac |
| wi_20260928_219 | asobi | in_progress | 同 ID 接纳原事实 | ownermac |
| wi_20261005_654 | pulse | in_progress | 同 ID 接纳原事实 | ownermac |
| wi_20261005_904 | pulse | in_progress | 同 ID 接纳原事实 | ownermac |
| wi_20261006_340 | pulse | in_progress | 同 ID 接纳原事实 | ownermac |
| wi_20261008_550 | pulse | awaiting_verification | 保留新日志 | 没有本次本地记录 |
| wi_20261011_070 | hestia | in_progress | 同 ID 接纳原事实 | ownermac |
| wi_20261013_505 | hestia | in_progress | 同 ID 接纳原事实 | ownermac |

本地仍显示活动、生产已经终态的记录如下。保留生产原终态，不再启动业务；切换时只退役过期的本地执行。

| 节点 | WI | 生产原终态 |
| --- | --- | --- |
| ownermac | wi_20260830_264 | completed |
| ownermac | wi_20260830_727 | completed |
| ownermac | wi_20260830_583 | completed |
| ownermac | wi_20260902_145 | cancelled |
| ownermac | wi_20260902_678 | cancelled |
| ownermac | wi_20260904_368 | cancelled |
| ownermac | wi_20260906_415 | completed |
| ownermac | wi_20260911_261 | completed |
| ownermac | wi_20260925_247 | completed |
| ownermac | wi_20261001_297 | completed |
| ownermac | wi_20260829_520 | cancelled |
| ownermac | wi_20261004_159 | completed |
| tcloud | wi_20260905_037 | cancelled |
| tcloud | wi_20260905_820 | cancelled |

## 在途效果

本轮盘点含 24 条未结效果。逐条原 effect_id、状态、尝试次数及来源已保存到私有 preparation.json。未知结果不能当失败重发。

| WI | 节点 | 效果类型 | 状态 | 原身份处理 |
| --- | --- | --- | --- | --- |
| wi_20260820_568 | ownermac | scoped_grant_realization × 1 | conflicting | 保留，按真实回执续接 |
| wi_20260831_013 | ownermac | scoped_grant_realization × 2 | conflicting | 保留，按真实回执续接 |
| wi_20260907_326 | ownermac | git_audit_publication × 1 | leased | 保留，按真实回执续接 |
| wi_20260907_326 | ownermac | git_audit_publication × 17 | unknown | 保留，按真实回执续接 |
| wi_20260924_219 | ownermac | candidate_publication × 1 | conflicting | 保留，按真实回执续接 |
| wi_20261013_505 | ownermac | committed_publication × 2 | conflicting | 保留，按真实回执续接 |

## 已有演练证据

- 固定水位 676 个 WI 的读取/重放通过；原 Git 终态与业务归属保持。
- 真实安装的 Pi、Codex、Claude SDK、Claude CLI 通过 Relay 的隔离闭环：创建轻量 WI → 原生保存 → 真实新会话 → 同 WI、代际 2 继续；重复 rollover 不产生额外模型请求。模型仅访问确定性回环 fixture，不是人类 UAT。
- ownermac 原 SQLite 的私有副本：19 张表、44,573 行摘要不变，55 次旧式写入被拒；已打开的旧连接也不能写。生产数据库未改表、未冻结。
- 本线程真实 Codex 进程只读验证 WI018 原 owner 绑定：原代际 5、两个原受管工作树。没有因旧 schema 缺少 execution_grants 而重建授权。
- 独立 Pi 0.87.1 经正式启动器、原生进程及会话源验证完成创建、保存、退出后同会话接续；使用回环模型，共 7 次请求，原代际保持 1。原生工具不借用 Relay 身份。
- 独立入口安装策略已从统一 Agent 注册表及工具链编译：12 个真实 Agent，48 个来源绑定，包含 Codex Desktop；仍是准备状态，未安装。
- 固定私有原库中，14 个活动旧 WI 的业务候选/历史输入/效果能翻译并保留，7 个没有本节点的完整执行来源；随后在 TCloud 只读核对了 Duke615、Aki520 的原记录，均可翻译。因此合计 16 个旧 WI 已核对，另 5 个只有生产 Git 来源，需要在冷切换中保留原状态并走原 owner 的正式接续。15 份原候选保留。此项不冒充执行授权或已接纳。
- WI326 的 28,746 条重试曾使原执行输入超过 16 MiB；现以流式完整摘要与每个效果的最后回执引用原历史。原数据库摘要不变，真实副本翻译通过。
- 回退的首个安全约束已验证：首次新核采用没有兼容前驱时，不能删掉新入口并恢复旧写者。此项不等于完整服务回滚已可用。

## ownermac 整套安装与数据量演练补充

- 受管 CD 已补齐 ownermac 的新核分支：新核、Relay manifest、launchd、独立 Pi/Codex/CC 入口及 Stats 发布器/配置作为同一组准备。`--prepare-native-only` 只准备；不修改现役入口或启动服务。实际激活仍要求生产旧库已封写、独立旧后台服务和旧 SCL cron 已退出。
- 兼容回退必须是另一个精确源码版本的完整新核安装组。在停服务前验证原权威、namespace、surface、观察事实来源及全部入口集合一致；观察事实来源比较不计逐来源读取选项 `session_wi_mcp_since`，候选登记表可缺少其自身已退役来源声明列出的回退来源（ID 与类型都相同），其它增删改都拒绝；校验程序/策略摘要和原入口字节。回退只换程序和消费者，不回退 Git 事实、已接纳计量或恢复旧写者。
- 使用真实封装程序、原生 Git 和实际文件执行了部分安装失败后的整套恢复及重复恢复；原事实 Git HEAD、原档案与已采用观察来源保持。此演练仅替代 macOS 服务控制，不能冒充现役 launchd 或自然周期采用。
- 现有 Stats 定时发布器已确认采用四参数包装入口；安装准备保留原频率、日志、provider/destination 配置，并显式改接同组版本的 `refresh`。原生模式下 Stats 只读内核观察事实，不再扫描本机来源或导入旧 peer 账本。
- 原生产计量的私有 APFS 副本有 1,488 份事务、2,564,045,392 字节。预检约 215 秒；迁入隔离 Git 约 544 秒，重复迁接新增提交为 0。1,721,316 个事实的原/新读取水位、记录时间和快照摘要完全一致；最大的 Git blob 为 1,048,576 字节。
- 全量 Stats 重建已通过：修复后原路径约 228 秒、峰值 1,764 MiB；新核路径约 285 秒、峰值 1,576 MiB。水位 1,488 的 9 个视图及全部业务字段相同，未生成本地权威事务文件。两次报告只有运行时填写的源码版本标签和由此生成的总摘要不同，等价比较只排除这两项；原始报告保留。此前两条路径都超过隔离进程的 2 GiB 保护线，删除全部当前 payload 的重复常驻对象图后通过。这不是实际生产 OOM 证据，也不声称满足普通对话时限。
- 所有已登记旧 runtime release 的绝对 `harness`、`harness-scl`、reconciler 和 Codex 旧钩子已纳入整套退役准备；逐项保留原字节/链接及摘要，再以无旧写能力的入口替代。23 项安装测试覆盖真实文件退役与程序保全；这是隔离演练，现役旧入口尚未替换。
- 新核增加仅消费既有资源/源码效果的账户后台入口，不创建 WI、执行权、终态或清理意图；不启动模型或发送通知。定时器游标只决定扫描顺序，可删除重建，所有操作由原日志再次裁定。整套安装和兼容回退均包括该服务；19 项服务测试通过，含真实封装 CLI、实际 Git 发布/采纳、删除中断、未知结果保留、损坏 WI 隔离及游标重建。真实生产服务尚未注册。
- 两个独立旧消费者的退出路径已准备：`com.pulse.workspace-sync` 经旧绝对 harness 运行；worktree-reclaimer 的 apply cron 仍使用旧 SCL。整组准备保存原服务及完整 crontab 的私有原件，激活可在旧业务库已经封写后显式执行 `--retire-legacy-background`；只退役所列旧命令、保留其它 cron，运行中则暂缓，部分失败只按原计划续办。退出检查包含 launchd、残留子进程与登录时可能复活的 plist，不恢复旧写者作为回滚。
- **明确的维护影响：**三个旧自动同步授权引用已完成的 WI479。这项自动提交共享人类目录的旧定时服务在冷切换时暂停；原授权、原文件、未提交改动完整保留。新后台只消费已获新核裁定的 Agent 源码发布及资源回收，不声称已替代人类目录的全部自动提交功能，不把 WI479 的旧权限变成长驻授权。这项影响须随维护清单展示，不能暗中停工。当前生产服务与 cron 均未变。
- 配套 lifecycle skill 4.8.0 与 Desktop Codex 来源绑定已经通过 CollaborateWithAI #550 合入 main，固定提交 `996febedf9a36ec92adcb7155f92a0fc480d1650`；PULSE 发布合同采用该精确提交。源码发布不代表全节点已加载该 skill。

- 真实安装配置的预检发现并修复两个绑定缺口：原编译器只登记 PULSE/共享 skill 仓库，也未配置任何源码采纳消费者。现在从统一仓库与 Agent 注册表生成 ownermac 实际使用的 Coevolution、PULSE、Oikos 三仓、三个发布目标和三个源码消费者。14 个有完整本机原登记的旧活动 WI 全部通过仓库覆盖核对，原库摘要未变。可扫描但没有已配置 Agent 消费的合伙人仓库不自动增加写入口。发布目标准入不替代单个 WI 的执行权或人审。
- 新工作区沿用受保护的 host placement 与原 `/Volumes/Worktrees` 卷身份，不改为系统盘临时目录。原 48 个独立入口、原 Pi 资源根及原分配保留；同一个已审安装器为候选与兼容前驱编译当前注册配置，避免回退漏掉新登记仓库。程序版本、配置摘要和编译器摘要分开记录。

- Linux CI 的原 PID 复用测试失败已在同一 runner 复现：procps 4.0.4 对刚启动的进程偶发年龄下溢，200 次中 25 次返回四亿多天。Linux 改读内核启动 ticks 与小数 uptime；同机 200 次真实进程验证全部拒绝活跃原主、允许陈旧 PID 恢复，未杀其它进程。macOS 仍用原生时间观察；无法确认时保持拒绝。正式候选 CI 尚须通过。

- 回退校验现从各安装组自己的精确 main 检出核对源码，避免以候选目录核验前驱而拒绝全部真实回退。真实 Git 两版本与远端推进的回归通过，冒用候选目录仍被拒绝；部署相关测试共 55 项及 4 个子案例通过。

- 全量计量预检只在短锁内截取不可变文件前缀，耗时验证不占用生产写锁；安装器为兼容前驱提供私有预检原件副本，原生产来源选择不变。并发真实追加、固定前缀、忙时暂缓与不发布回归通过；不让几分钟的预检卡住生产计量。

本段所有准备及演练均未重启生产、修改生产 cron/账本、发起真实对话或部署其它节点。步骤 1–2 尚未全部完成；最终候选未冻结。

## 17 类旧权威退出矩阵

这里逐项列出已验证部分和仍须完成的真实消费者证据。未通过项继续阻止生产冷切换。

| 类别 | 已有准备/演练证据 | 正式切换时仍须取得的生产证据 |
| --- | --- | --- |
| 1 创建/定义/编号 | 四运行时隔离新建；旧 store native_only 反例 | 独立 Codex/Pi/自动化的正式安装入口与所有旧发行入口退役 |
| 2 生命周期/SQLite/YAML | 676 历史重放；SQLite 全表反向写入 | 生产 YAML/旧绝对入口退役及新核逐项读回 |
| 3 执行代际/grant | 原 owner 绑定与显式 grant 分开验证；撤销/受限拒绝 | 未绑定旧 WI 及跨节点原 scope 的受控接纳 |
| 4 Relay 绑定/rollover | 四运行时真实物理 rollover；重复不重发；未知请求保管 | 现有 pending intent 原身份承接及删除投影重建 |
| 5 工作区/回收 | 原注册/物理 worktree 身份检查 | Hestia 回收器与新资源协议的真实消费 |
| 6 checkpoint/continue | 原 checkpoint 保留；后继同 WI 保存 | 全部未结旧 WI 接续与中断矩阵 |
| 7 candidate/review/Outcome | 原候选、人审与 Outcome 翻译回归 | 全部现有待验收候选固定摘要读回 |
| 8 handoff/ACK | 原 ACK 和未结义务保留回归 | 实际旧交接逐项接纳读回 |
| 9 admission/guard | 新核身份/授权分离；间接写表拒绝 | 三节点原写者冻结回执及无 fallback 分发物 |
| 10 Broker/outbox | 新核 Relay 不加载旧控制器/恢复器；原未知效果保留 | 全部在途效果由新消费者观察/续办；退休旧进程 |
| 11 终态/清理 | 生产终态与本地活动差异已列出 | 资源占用与清理消费者读回，不能靠删除掩盖 |
| 12 人类输入 | 原生进程/原始消息认证；工具结果不当批准 | 独立客户端正式绑定与全部原决定保管可用 |
| 13 计量/Stats | 原/新 1,488 事务快照同摘要；重复迁接不增提交；Stats 原生只读 | 全量等价已通过；本机旧计量写者正式退役 |
| 14 发布/回退 | ownermac 完整安装组；真实程序/Git 的部分安装与整套兼容回退测试 | 最终精确 main 候选和前驱、实际 launchd/Stats 自然周期读回 |
| 15 自动化 | 原 cron 文件路径/摘要保留盘点 | 调度入口统一新核，保留暂停决定及未结触发身份 |
| 16 通知 | 原效果 ID/未知送达保留 | 实际通知消费者重复观察不重发读回 |
| 17 SSOT 检查 | 新核窄查询接口；Relay 不加载旧 WI 控制器 | 旧检查器和其全部调用点退出证明 |

## 下一执行边界

先完成准备/演练缺口并冻结精确候选。上表第三列的实际安装、进程退出及生产读回属于后续有界维护窗口，不能以准备测试冒充，也不能因尚未操作生产而重复同一准备。全部通过后，才进入人类已指定的 ownermac 首发。跨协议冷切换须在明确维护窗口停止所有旧写者；不能在其他节点继续旧写的情况下声称单一内核。

私有证据目录：`/Users/owner/Coevolution/Bigbird/PULSE/data/agent-harness/runtime/runtime-artifacts/wi018-cold-cutover`。此处只引用脱敏事实和摘要；没有原始会话、私有 remote 或凭据。
