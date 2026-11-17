# SCL 在 PULSE 同仓布局中的边界

本契约区分源码归属、状态定位与发布权。代码适配不宣告三节点迁移完成；当前现场和迁移暂停项仍以 README 及实际读回为准。

## 根解析

| 输入 | 用途 | 约束 |
| --- | --- | --- |
| HARNESS_ROOT | 当前登记的服务账本 | 显式根没有本地 WI 文件时仍交给 SCL/Git 读取，不能回退到祖先 CWA；根不可用不能创建空账本冒充恢复 |
| HARNESS_STATE_ROOT | 可选的统一私有状态基目录 | 必须是已存在的绝对目录；注册、Broker、convergence、outbox 等使用各自 namespace；不搬迁、不导入、不授予执行权 |
| SCL_KERNEL_ROOT | 已登记 kernel 的显式位置 | 优先于统一状态基目录，支持保留分阶段迁移的实际位置；不能重建 lineage 或身份 |
| SCL_HUMAN_DECISION_KEY_FILE | 原 Host 决定密钥的位置 | 保留原密钥；位置登记不代表允许生成替代信任根 |
| PULSE_HARNESS_HOME | 旧过渡部署的 Git 内私有目录放置许可 | 不是最终 PULSE_HOME，不推导 kernel 或其他状态位置 |

未声明统一状态基目录时保留既有 OS-account 默认位置。声明后目录缺失必须失败；不得静默回旧位置。具体管理器仍校验状态放置与操作授权。

开发账本路由以实际 Git top-level 为边界。目录祖先不能认领嵌套的独立仓库；SSH 与 HTTPS 仅在主机和精确仓库路径一致时视为同一远端。不同主机、端口、别名或仓库路径不猜测等价。注册摘要、唯一映射、symlink containment 和 registered linked worktree 校验继续有效。

Guard 重新安装必须展开可逐字验证的旧 Harness 包装层，并保留原项目 hook；不能把另一工作树的旧分支约束串接到当前工作树。被修改或无法验证的包装层拒绝自动展开，旧文件不删除。消费者子进程使用已选定的账本根，不能重新继承另一套根。

## 构建与部署

普通 Vultr Relay 发布不重放历史 WI136 Epoch Cutover：构建在独立的服务源码检出中进行，使用实际进程对应的已验证 Harness/SCL 安装回执，只调用原 release writer。原配置、cron、策略、系统服务及 authority 必须保持不变；同版重跑 PID 不变。历史切换脚本与测试保留，但不再是每次 Relay 更新的前置事务。Mac 服务源码稀疏检出按同一确切 main 的根模块归位，验证来源后才构建。

Actions entry、rollover 和安装态接续验收从同一 PULSE SHA 取得 agent-harness 与 Relay；不再提取历史 CWA 分支或开发者临时工作树。entry 仍验证人类请求、同一 WI、前置 admission 和执行后 attestation；CoEvolution 只保留独立的上位治理源码检查，不承担默认 Harness 账本。

部署登记扩展 state_root、kernel_root、human_decision_key_file 字段，供现有 release writer 消费。ordinary deployment 必须与实际捕获的 authority 和状态映射相同；差异需要受控迁移，不能以代码发布隐式切库。

`default_harness_venv` 是兼容字段名，登记当前经安装器验证的稳定代码入口，不是私有 Python 环境或待创建目标。两云现登记 `/home/pulse/.local/share/coevolution/tools/agent-harness`；2026-10-08现场核对新入口、Relay/reconciler/Pi及原迁移回执，精确旧BigbirdEnvironment `.venv` 路径已不存在。位置与边界见[实施盘点](pulse-platform-deployment-inventory.md)和[现场核验](wi184-cloud-entrypoint-readback.md)。Vultr19项禁用cron仍有旧CLI显式覆盖，不能把新登记当所有配置已退出；不恢复旧链接或启用任务掩盖残留。

普通Relay发布保留原库、信任与安装历史。正式稳定入口迁移经原installer锁写入精确intent/completion；paired adopter和历史升级/重试/回退只通过验证完成的旧→新映射解析原scope，旧receipt/transaction摘要必须一致。WI219已交付该能力及适用CI，WI184不另造接管协议。仅改YAML或任意同release别名仍不算采用；不得重建baseline、改旧历史或顺带迁ledger/kernel。真正kernel relocation继续是另一种有独立前置的操作，不借其名义授权本次入口或数据搬迁。

Mac 登记表达已观察到的分阶段状态：PULSE 服务账本、新 kernel 和原密钥位置；注册/Broker 仍沿既有 OS-account 位置。它不是三节点最终布局或唯一 publisher 已完成切换的声明。

发布器按 topology 登记的精确仓库身份选择源码布局：PULSE 使用 `agent-harness/`，尚未切换的 CWA 节点保留其登记来源。未知仓库或缺失的 exact Git tree 直接拒绝，不探测目录回退；这不授予旧节点新的发布权。Vultr 部署的 policy 路径来自目标版本的 site 登记。

Pi/ComputeCost 的计量解释器使用业务账户标准 Python，启动时校验真实 `sys.prefix`。不能把环境的 `bin/python` 末级链接替换成底层解释器而丢失环境身份；Host 同时校验 canonical 环境和真实二进制所在的资源范围。程序代码与解释器分别固定，已启动进程不能因 stable 链接切换而暗换代码。安装态验收实际导入声明依赖，不能以全局补包、另一份环境或版本查询代替真实消费。

已完成的 recorded relocation 与首次 baseline 是不同状态。外层安装器通过 `adopt-relocation --source-receipt ... --expected-receipt-sha256 ...` 采用原始迁移回执：验证已有内核的迁移完成记录、wrapper manifest、目标目录和封存 inventory 后，只写消费者采用回执，不执行源运行物、不激活或迁移内核。随后普通 recorded apply 与 rollback 使用这份已验证来源；原有 hold-all 继续保护所有历史。已有首次 baseline 事务、证据缺失/漂移、根别名和未知标记仍拒绝，不能以重新建立 baseline、reinit 或删除保护标记替代采用。

旧 `PULSE_SCL_LEGACY_RESUME_ENABLED=1` 入口仍是历史 CWA 协议，不作为新布局入口；受控发布使用 sealed SCL，Mac 已读回为 `0`。

## Closeout

新结算材料通过 PULSE canonical authority 发布。业务成果仓保持人审批准的仓库身份，不能把 CWA 业务仓名批量替换为 PULSE。每个业务仓的 integration、CI、publication receipt，以及每个 required consumer 的采用证据继续独立校验；发布账本本身不证明这些门已通过。

送验后当前生产者的小修改沿用原有有界计量段。计量准入检查接受未最终冻结的 `awaiting_verification`，同时保留当前执行者、epoch 和实际 Host attachment 校验；最终冻结不因此获得写入或新建计量段权限。

终态采用、Relay 解绑、Session 可结算和过程资源回收仍属于原生命周期协议。兼容性适配不得通过新 WI、reinit、清空 Session 或手改 SQLite 绕过失败。

## 验证层次

依次记录：隔离回归、正式 CI、同 SHA 安装态验收、实际运行读回、原入口 UAT。单层通过不能替代后续层。故障验证包括错误 Git 边界、远端身份不符、注册摘要漂移、指定状态根缺失、稀疏账本、失败重试和重复终态落账；真实入口仍须确认 continue/checkpoint/rollover/fresh resume/closeout 及恢复无丢失、无重复效果。
