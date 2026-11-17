# WI184 · 19项禁用cron入口变更的写入准备

## 当前结果（2026-10-13）

Vultr 外置生产 `cron-jobs.json` 已在明确的人类系统维护授权下完成19处旧 `execution.harnessDispatch.harnessBin` 替换，37项仍全部 `enabled=false`。复用实际运行发布 `33d6d62f575762c4178d3e6c1e4df7530efee9ca` 的原 CAS writer、原锁和真实 producer attestor；没有修改 SCL scope、策略、身份或账本。

此前把“WI 工作树范围不含生产文件”解释为独立 Host 维护必须另获 SCL 文件准入，是本任务的判断错误，现撤回。用户已明确要求把这次作为系统文件维护，不经 SCL；该决定覆盖旧 checkpoint 的执行顺序。SCL 工作区范围不等于 OS 文件权限，实际执行仍遵守用户范围、Host 限制及原 writer 合同，不能绕锁或回执。

16:43:27 UTC实际事务只替换19个字段；全语义逆映射与原文档相等、owner/mode0600保持，mapping/enrollment/attestor及旧producer回执未变，新增1份真实producer回执。同一完整后像重试返回NOOP且无第二份回执。16:44:08 UTC另一只读连接确认37项全禁用、旧值0/标准值19、后像摘要匹配。无任务执行、信号、reload或restart；不宣称内存消费者采用、新CLI安装、独立恢复或整个WI验收完成。

- 原像 SHA256：`7d71601e73a4262a43577ad9a80943227e2ac6a7959368a3a897643afbddc8b7`
- 后像 SHA256：`d70e05b727a4673a38523c003d3f789fdb93155bf604f08c6683818ed501c822`
- 实际 producer 回执：`runtime_receipt_v1_c71d8d192a8483be8a54c7e16d60fce077c42845c313c4b2099e9cdaeee8fa63`
- 可公开读回：[限定事务证据](evidence/wi184-disabled-cron-maintenance/2026-10-13.json)。生产配置和精确前像备份留在 Vultr 的私有维护目录，不入 Git。

原CLI与resident环境绑定候选继续在原登记分支正常提交/PR/CI/合并；这是相关源代码交付，不是重做已完成的生产文件事务，也不自动授权部署或重启。

## 历史准备阶段（2026-10-10；权限判断已由上述结果纠正）

- 原 CAS writer 已通过[既有正式 Vultr 发布](https://github.com/bigbird-online/PULSE/actions/runs/37777270939)采用，固定源码 `5ad997ecf2113083040dd6fa2d7b93094f19d26d`；2026-10-08 独立实际进程、源 TS/dist JS、原生回执及 SCL handshake 读回匹配，同版本 repeat 为 NOOP。不要重做该发布或下面的原 CAS 修补。
- 本次在原授权工作树补原 jobs CLI 的单字段冷更新准备，复用同一 writer/attestor；39项隔离测试在 umask077/022 均通过，源码 typecheck 返回0。**新增 CLI 尚未发布或安装，不构成生产授权或19字段事务。**
- 2026-10-10 已安装新核心读回原 WI 的 `local_work=available/current_grant`，原两份工作树及成果保留；旧的本地继续阻断不再适用。这不扩大生产文件范围。精确非 Git cron 维护准入仍未接通，不扩 Host/reinit/登记范围，不增加授权或维护框架。最后一次生产配置读回仍为 2026-10-08 21:12 UTC+8；本轮没有生产访问，旧观察不能当作当前配置或健康判断。
- 在原 `Scripts/relay-config-layout.py::managed_command` 中连接新冷更新命令的常驻实例设置：不继承调用者的配置路径或 attestor 设置，缺少常驻 attestor 时在启动 CLI 前拒绝；其它命令不变。19项维护入口测试及原39项 CLI/writer测试在两种umask各通过，源码编译/typecheck返回0。另以真实编译 CLI、原 writer 和独立 fixture attestor 子进程验证本地链路；这些仍不是生产准入、正式 launcher 或实际 producer 证据。

## 原 CAS 准备记录（2026-10-08，保留阶段边界）

当时只补原 `Relay/src/runtime-artifact-receipt.ts::writeCronJobsConfig` 的可选前像校验，不另建安装器、配置写入器或维护框架。15项隔离测试及Relay源码类型检查通过，当时未部署、未修改生产字段。上述正式运行采用已取代当时“未部署”的结论。

2026-10-08 11:08 UTC+8，通过已登记Vultr `pulse`只读通道再次确认：原文件SHA256仍为`7d71601e73a4262a43577ad9a80943227e2ac6a7959368a3a897643afbddc8b7`，37项全禁用、19项旧CLI字段、普通文件mode0600。正在运行的Relay394ac10fe发布中的writer未提供此次前像校验；三个实际artifact-attestor配置字段均存在，不能清掉它们或以`env:{}`绕过回执。该观察不证明其它时间文件不会改变。

## 原接口与最小修补

现有writer负责锁、原子文件替换、调用既有产物回执接口并验证确切后像；回执失败走原精确前像恢复。`relay-jobs` CLI目前只有list/add/del/run；删除再添加会改变任务身份并默认启用，run会实际执行任务，均不是这19个冷字段的替换入口。

原writer在调用者准备文档与取得锁之间没有前像对比，可能覆盖另一个writer随后写入的业务配置。本次新增可选 `expectedPreimageSha256`：

1. 格式错误在创建锁/写入前拒绝。
2. 取得原锁后读取前像；存在的文件若已逐字等于拟定后像，沿原行为返回NOOP，不生成第二个回执。
3. 非NOOP的绑定前像必须仍存在且摘要匹配，否则释放锁并拒绝，不覆盖变化、不重新创建消失的配置，也不调用attestor。
4. 真正写入继续走原原子写入、原mode保留及回执/失败回退。未传参数的现有调用者行为不变。

这是字节级并发检查，不是权限授予、通用JSON编辑器、历史来源导入或已完成生产事务。它不能阻挡不遵循原锁的其它写入者，也不独立约束目标路径/业务字段；这些仍由受权调用方和正式维护边界保证。既有参数默认不变，不宣称全部旧调用者已采用CAS。

## 隔离验证

使用现有`/usr/bin/node`，显式环境白名单、单worker、临时HOME/TMPDIR均在原授权工作树的Git忽略目录下；没有补包、SSH写入、服务信号、模型请求、生产任务或正式artifact登记。attestor为fixture runner，不是真实产物签发。

除原9项writer回归外，新增6项：

- 合成37项全禁用文档，仅19个exact旧harnessBin换为标准值；将19字段逆映射后全文语义等于原文档，身份、顺序、任务内容、账本和其它字段不变，mode0600保留。
- 并发改变前像时拒绝，保全他人的新内容，无回执调用、锁正常释放。
- 前像消失时拒绝，不创建新配置。
- 错误摘要提前拒绝。
- 原请求第一次写入后，用原前像摘要及同一后像重复调用仍NOOP，回执只调用一次。
- 匹配前像但回执失败时，原紧凑JSON字节与mode精确恢复。

本地受限证据：`pulse-space/memory/wi184-20261008-cron-writer-isolation/`。

| 文件 | SHA256 |
| --- | --- |
| `Relay/src/runtime-artifact-receipt.ts` | `c83390edbfccee6a0da5b8985ba3da415503c1f9640bf14c12985a0464476bd4` |
| `Relay/tests/runtime-artifact-receipt.test.ts`（当前，显式mode fixture） | `9385c1b99aa8602898a8be095a2dc673160d274099be0bb2f17478f91dbdbea9` |
| `Relay/tests/cc-session-wi-mcp.test.ts`（已交付接口的消费者断言对齐） | `7ab01048b505da7284dd5d27d34c2b57bce394c0d172ce9a3b27ec76bbfb6eff` |
| `run.json` | `64cf59ae1a48663c95a81707fa3bc05f417ea34eb959906af40766ebf6bffdb6` |
| `vitest.json` | `db96557b30f46c6d02509dab7b704380c237b30132f1118c28919a426e7c2ce8` |
| `typecheck.json` | `8f92625e4d45cce60b99cfcbd2babe2fcef0f6add7654a937a21efbebe33753d` |
| `vultr-writer-recheck.json` | `f537f9bc91529eeaaadeb2803b191799348458a41716ae05a27340b7c84d0d63` |

`run.json`固定首次命令/环境键/源码摘要，首次Vitest记录15/15成功；其原test摘要`19200814…`不是下面fixture修正后的当前test。`typecheck.json`单独记录实际tsc退出码0，不用空日志冒充成功；writer源码未再变化。静态审查无必要更正，非独立Outcome。

### 精确候选CI发现与必要消费者对齐

候选`64b6722bd26799d0f1b6dc471a65b76c69672eb0`的Relay CI `37722531860`实际失败：2864通过、1失败、62跳过。writer15项在CI全部成功，typecheck/build等此前步骤成功，后续platform-cutover步骤被跳过，不能计成功。唯一失败是`Relay/tests/cc-session-wi-mcp.test.ts`仍期望按session归属WI的旧文案，而已合并的ComputeCost owner提交`28e8c258b17167f0ae100dd01cb79bfe2919cd9c`交付了CC按原生请求归属规则；两个相关文件在候选与固定main `b9aa1549…`之间均无实现差异。

只修改Relay测试的确切期待值，保留`toBe`及真实CC-mode initialize/tools-list探测，不改ComputeCost业务实现、度量策略、数据或部署，不宽松接受任意文案。此外让原像回退fixture显式chmod0600，不依赖默认umask。

两次单worker复验（umask077、022）均**21/21通过**：原writer15＋CC消费者6；pool子进程被mock，真实MCP只请求initialize/tools-list，无声明/业务写入。证据保留在同目录`rerun-umask-{077,022}/{run.json,vitest.json}`：

| 文件 | SHA256 |
| --- | --- |
| `ci-113133252267.log`（前候选原失败日志，受限本地） | `082fdbf84d476239dd07d30339aa34895ec427301b1b3f04a0268771c03b3a0c` |
| `rerun-umask-077/run.json` | `f81c57f5052396caadda9f8c2c8ddf8a1592300a967b330c6e0892b0aca37e22` |
| `rerun-umask-077/vitest.json` | `e70acd386663d73ef85caa58b61fd1c4f6fe1ee414647a2c11562feade847b1d` |
| `rerun-umask-022/run.json` | `457b4afeec4adc963de1a279ad4f01c24fdd78a75561ba2104bf699704f3a717` |
| `rerun-umask-022/vitest.json` | `abeda212e47a4f8e784b71a5507a0b660b013a9bab99680487d8517abc5ae186` |

再次发布后须重新取得新head CI实际结果；不抹掉前次失败，不把局部复验当全组件CI通过、main交付或维护采用。

## 原 jobs CLI 的最小更新准备

新增 `update-disabled-harness-bin`，由原 `pulse-relay jobs` 委派链使用，不增加第二个写入器或 `/job_edit` 通用框架。它只改 `execution.harnessDispatch.harnessBin`：

- 默认只生成计划，输出摘要/计数，不输出任务身份或内容，不创建锁/调用 attestor/写配置。
- 必须绑定原像 SHA、精确 job 数和匹配数；所有任务须 `enabled=false`、身份非空且唯一。仅支持全旧值或完整精确后像重试，不接受部分转换。
- apply 必须另绑定计划的完整后像 SHA。参数 `--apply` 只是执行请求，**不是维护批准、Host 准入或授权证明**；命令、同 UID 和产物回执均不提供缺失的资源权限。
- 使用原 managed cron 路径，不接受另一个目标文件，不合并或剥离 run state，保留其它字段。要求 owner-private regular0600配置及已配置 attestor；写入只调用原锁内 CAS writer。
- 回执失败由原 writer 精确恢复原字节；完整后像重试不再生成回执。已有真实 writer lock 会拒绝本操作。不重载、不发信号、不构造 scheduler、不调任务。

隔离夹具验证覆盖 CLI plan→apply→NOOP、39项（新增24＋原writer15）在两种umask各通过、全语义逆映射/状态保持、原像及后像绑定、并发锁、部分转换/启用/重复身份/错误计数/非私有文件拒绝、回执失败精确恢复。attestor/subprocess/scheduler为明确 mocks；不证明当前生产准入、原事务成功回退、独立恢复或实际内存采用。首跑37通过/1失败是CLI夹具遗漏配置resolver export，保留证据，修复fixture后重跑，未削弱断言。源码 tsc 不包括测试类型检查。

证据位于原工作树 `pulse-space/memory/wi184-20261009-cron-update-preparation/`，未产生生产事务。现有 Host worktree launcher 限制资源落在已登记 Git 仓内，不能以该 launcher、resource recovery 或 workspace adoption给这份非 Git 文件制造准入。本次只关闭字段更新源代码准备缺口；正式维护权限、发布/安装该 CLI、原生回读及安全消费者采用仍未完成。

## 原维护入口的本地消费者验证（2026-10-10）

原入口此前合并调用者与常驻进程环境，常驻进程缺少某个设置时会保留调用者值。本次只对 `jobs update-disabled-harness-bin` 收紧这一边界：所有已声明实例路径与三个 attestor 配置只取常驻进程，缺少/空白 attestor 不借用 shell 设置。继续使用原 Node、实际 cwd、managed map 和 CLI 参数，不增加批准参数、授权写者或锁。`PULSE_RELAY_MAINTENANCE_BOUND` 仍只表示实例绑定，不是 grant。

19项原维护测试（含新增4项）和39项 CLI/writer测试在 umask077/022 各通过。另从原 `managed_command` 进入真实编译的 jobs CLI、原 CAS writer、独立合成 attestor 子进程，验证只读plan不创建文件/回执、19字段更新及37项禁用/全语义逆映射/owner-mode保持、完整后像NOOP不重复回执、原live lock拒绝、attestor失败精确恢复紧凑原像。编译包含源码typecheck，不包含测试typecheck或全组件CI。

证据：原工作树 `pulse-space/memory/wi184-20261010-managed-cron-consumer/`。常驻进程观察是明确注入的合成对象，attestor是合成脚本；没有 service discovery、真实 launcher/Host 准入、生产producer、网络/生产配置访问或部署。不能将本地消费者链验证说成完整维护授权链已经接通。

## 执行边界与下一动作

本次是人类独立授权的 Host 系统维护，不把 WI lifecycle 作为 OS 文件权限系统；也不借这个决定扩大其他文件、服务、身份、重启或任务范围。原 WI/startup/两份登记工作树和完成成果保留，SCL 状态未因生产文件维护而手改。

生产19字段磁盘事务、真实回执、全语义保持、完整后像NOOP及独立文件读回已成立，不能再报告为“缺少SCL生产文件准入”。当前单点为把原CLI/常驻设置绑定及限定证据通过原登记分支正常PR/CI合并。生产配置不在Git中，不提交任务内容或私有前像；合并不会重放生产事务，也不会自动部署或重启。

本次没有执行原生产事务的显式回退或独立备份恢复；精确原像、owner/mode和原回执保留于Vultr私有维护目录。回执失败时原writer的自动恢复及此前[限定的实际外置协议验证](https://github.com/bigbird-online/PULSE/pull/1089#issuecomment-6054184219)仍守各自证据边界，不当作本次生产rollback/整机恢复证据。全部37项继续禁用，不触发job或重建旧链接。磁盘更新不等于业务内存采用，reload与restart不在本次执行范围内。

原其它迁移/恢复/结算责任见[实施清单](pulse-platform-deployment-inventory.md)，已有入口现场事实见[两云核验](wi184-cloud-entrypoint-readback.md)。本步不关闭整个WI。
