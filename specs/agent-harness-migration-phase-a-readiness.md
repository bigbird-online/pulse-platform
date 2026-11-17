# CR-02 Phase A — 准入盘点（历史，未通过）

> 已被 [PULSE 全平台布局方案](pulse-platform-layout-plan.md) 替代；下文仅保留历史证据、旧路线缺口和失败记录，不再要求实现其通用迁移能力作为新方案前置。文中辅助脚本引用指向旧提交中的材料，已从当前提案撤回。

- work_item: `wi_20260823_184`
- observed_at: `2026-09-10T06:18:12Z`
- 授权范围：按本轮用户确认执行 A 阶段盘点和隔离演练准备；不包含 C/D 生产停写、搬运或切权。
- 审核基线：`8e12333ea567b6d5c06d3b9568b672298102cbb0`。
- 本轮远端 PR #466：OPEN，HEAD `54b680bd1188c9ef8ea98e323a0475fd036ce8d3`。已完整读取该 HEAD 的方案；完整迁移范围和四项缺口仍在。未声称两个提交整树相同。
- 当前工作树 HEAD：`566b6e7db0f2eab8bf64f2f9f0b795869c578332`，原方案仍为旧稿；本轮不覆盖、不重置、不发布旧稿。

## 已执行的只读探针

全部 GitHub 查询经 `python3 Scripts/pulse-gh.py run -- ...`。

| 探针 | 本轮结果 | 可得结论 / 限制 |
|---|---|---|
| `pr view 466 --repo bigbird-online/PULSE --json headRefOid,headRefName,state` | OPEN；HEAD 如上 | 后续变更必须基于远端当前版本，不能直接发布本地旧分支 |
| `api repos/bigbird-online/CollaborateWithAI/rulesets?includes_parents=true` | HTTP 403；服务端提示需升级套餐或公开仓库 | 本入口未取得可用 ruleset 证据；不能证明旧通道拒写，也不能据此断言所有保护均不存在；不修改套餐、可见性或权限 |
| `api repos/bigbird-online/PULSE/actions/runners` | `total_count=0`, `runners=[]` | 仓库端点未返回 self-hosted runner；组织级分配/runner group 尚未查证。不得声称 ownermac Actions 入口已可验收 |
| 继承的 sealed CLI `handshake` | read_only，runtime revision `69e2942a1bc3fb3592939f2747be8f3d8a421224` | 仅证明客户端握手；不证明 WI 授权、M04 连续性或迁移执行器权限 |

未读取或输出密钥；未生成 decision；未修改 kernel、registration、生产服务或 GitHub 配置。旧 STATS 已过期，不作为平台健康证据。CLI 帮助/状态探测中出现 invalid_request，未形成 lifecycle 成功或授权证据。

## 四项准入门

| 门 | 必需的可验证结果 | 当前状态 |
|---|---|---|
| A1 旧通道拒写 | 对 publisher、reconciler、Actions、共享凭据直推路径逐个列出拒绝执行位置；隔离负例证明旧版本/旧 fence/并发均不能绕过，并证明 CWA 非 Harness 业务不受影响 | 未通过；ruleset 查询受限，其他通道待查；不得进入停写窗口 |
| A2 执行器授权 | 明确执行主体、真实认证 Host 授权来源、允许命令与 exact 路径、期限/撤销/版本失配失效条件；信任材料原位保全或受控轮换，缺失即停止，不静默新建 | 未通过；本轮用户授权不是执行器已实现的证明 |
| A3 输入分类 | 机器分类分离账本、运行策略、构建资源；未知路径 fail closed；测试 checkpoint-only 不部署，policy 或构建闭包变化必须重新验证 | 未通过；方案已有原则，分类器与反例测试待实现 |
| A4 容量与停写 | 各节点实测旧数据/附件/运行物及可用空间；按所在文件系统计算候选、备份、LKG、临时导出与增长余量，避免漏项/重复计数；隔离演练测时并确定超时退出点 | 未通过；未测量，不承诺短暂停顿 |

维护窗口请求不得返回虚假的持久化成功：明确区分未接纳、已接纳但待处理、结果未知，保留原请求身份；具体协议与可重试行为须在演练前定义并测试。

## M04 证据限制

自行签发 decision 或机器 `local_committed` 不足以证明真实 Host 授权来源。相关旧材料原样保留，但不得作为新版授权连续性通过证据；从真实认证 Host 入口另行验证，不重签旧凭据、不改写历史掩盖问题。本轮未完成该验证。

## 后续只读取证（2026-09-10）

- 组织 runners 与 runner-groups 两个 API 均返回 403，提示缺少组织管理/runner 权限。未按错误提示扩大凭据 scope。
- CWA 仓库 API 对当前控制面账户返回 `pull/push/maintain/admin=true`；这是该账户的仓库权限，不证明生产 publisher 使用同一凭据，更不证明某次直推一定成功。
- CWA `branches/main/protection` 返回 403，提示套餐/仓库可见性限制。不能依赖尚无证据的 branch protection 或 ruleset 宣称远端拒写已成立。
- PULSE run `34443475723` 的 job 读回：`ownermac`，group `pulse-macos-trusted`，labels `self-hosted/macOS/ARM64`，2026-09-10 06:03:43–06:04:20 UTC 成功。**纠正入口判断：仓库 runner 列表为空不等于 macOS Actions 路径不存在；已有当日真实执行证据。** 该部署 run 不是本轮触发，也不是 Harness 迁移 UAT。
- adoption snapshot run `33347961221` 的 job：`bigbird-pulse-cwa`，group `pulse-linux-trusted`，label `pulse-control-plane`，2026-08-31 成功；仅为历史证据，当前 Linux 可调度性仍 unknown。
- 在 exact PULSE tree `d26608036f3020ee0de0132a40a359217956a422` 完整读取 `harness-adoption-snapshot.yml`：它发布三个跨仓输入文件的快照，写 shared runner 目录并上传 artifact；**不是节点盘点或 kernel 迁移演练工具，不触发它冒充验收。**
- 分页读取 workflow registry（共 167 条）后，以同一 exact tree 的完整文件清单交叉核对。registry 标为 active 的 `deploy-control-readonly-uat.yml` 在该 tree 返回 404；tree 实际有 18 个 workflow 文件。历史 active 标签不等于当前可信分支存在可调度文件。内容读取失败后的解码错误不构成任何有效源码证据。

## A1 实施约束收敛

若旧进程能接触可直推 CWA 的共享凭据，单独撤销 Harness 专用 token 或增加本地 fence 仍不足以拒写。必须在停写前验证以下之一真实成立：远端强制准入能覆盖这些写入；或旧 Harness 执行身份无法再获得共享凭据、且全部专用写通道已撤销/约束。无法证明时 A1 保持未通过，不能通过停进程或事后 CI 宣称解决。不得为此擅自撤销其他 CWA 业务权限。

## 本机 publisher 与可信入口核验（2026-09-10，第二轮）

完整读取同一 exact tree 的 `deploy-bigbird-relay.yml`、`stats-macos-ci.yml`、`pulse-trusted-linux-fallback-verification.yml`：

- 部署工作流从 ownermac 经 SSH 操作 Linux，有 reset/clean、构建、重启和回滚效果；不适合作为只读盘点调度入口。其成功 job 也可能只是已收敛检查成功，不能解读为本轮发生了部署。
- Stats macOS workflow 使用 `pulse-macos-trusted` / `pulse-macos-runtime`，但运行源码测试和 Xcode 构建，不清点生产 Harness。
- Linux fallback 使用 `pulse-linux-trusted` / `pulse-control-plane`，先验证已有 workload lease，再安装依赖、运行测试；不是生产节点盘点。
- 因此已确认 runner 选择合同，但尚未找到可直接调度的迁移只读探针；未触发上述工作流。

本机只读元数据结果（非跨节点结论）：

| 对象 | 观察 | 证据边界 |
|---|---|---|
| 已继承 Harness managed checkout | 属主 UID 与当前进程同为 1002；origin transport 为 SSH | 未输出完整 remote 或任何认证材料 |
| Git 配置 | `credential.helper`、`core.sshCommand`、`http.extraheader` 的有效配置查询均未找到 | 不等于没有 SSH 凭据或其他认证途径 |
| 同 UID 进程 | 找到 Relay CLI 和 `harness-scl-reconciler`；二者环境均存在 `SSH_AUTH_SOCK` | 只读取环境键是否存在，不输出 socket 值；尚未证明 agent 内 key 的权限或是否可直推 |
| 当前会话环境 | 上述凭据环境键未出现；会话 HOME 与 OS 账户 home 不一致 | 不能从 Agent 环境推断 resident 服务无凭据；首次 session HOME 下文件不存在的观察不作账户凭据证据 |
| OS 账户 home | `.ssh` 存在、0700；`.config/gh/hosts.yml` 存在、0600；属主均为 1002；`.git-credentials` 不存在 | 只 stat，不读取私钥/token；不由文件存在推断凭据权限 |
| user systemd 查询 | `Failed to connect to bus: No medium found` | 当前 shell 缺少可用 bus 上下文；不证明服务停止，不绕过身份猜造连接环境 |
| checkout 所在文件系统 | 观察到 available/free bytes 13,540,970,496（`shutil.disk_usage().free`） | 瞬时剩余空间，不是峰值容量准入；未测量目标各项占用 |

**A1 风险已具体化：** resident reconciler 具有 SSH agent 通道；必须确认该通道的作用域、是否与 CWA 其他业务共享，以及旧进程退出/重启后如何撤销获取能力。不得因会话自身无 token 或 Git helper 未配置就判定旧发布器已隔离。未运行 `ssh-add`、试探 push 或撤销凭据。

## 最小探针已实现并本机验证（2026-09-10 06:30 UTC）

- `Scripts/harness-migration-metadata.py`：仅显式接收 installation/ledger/kernel/runtime 根；以 OS 账户数据库定位 home；只收集 stat/statvfs 元数据。JSON 不含路径值、进程参数、环境内容、socket 或凭据内容。不存在/不可访问/未提供明确分开；拒绝相对路径及发现的 symlink 路径。无需第三方依赖。
- 本切片主动缩小：不加入进程/SSH agent 读取器、不遍历业务文件、不建立迁移执行器、不调度 workflow。路径检查仅服务观察安全，不是无竞态的写入准入或沙箱。
- `Scripts/tests/test_harness_migration_metadata.py`：7 项 unittest 通过，覆盖正文读取禁止、路径/secret 不进入 JSON、OS home 与会话 HOME 区分、symlink/缺失/异常、未提供保持未知、显式根失败非零退出。
- 本机以继承的 HARNESS_ROOT/SCL_KERNEL_ROOT 调用：两根属主 UID 1002、mode 0700、同一 filesystem device；可用 13,540,245,504 bytes。installation/runtime 未显式提供，保持 not_supplied，未猜路径。相同 filesystem 的可用空间不得加总。
- 账户凭据仅元数据：`.ssh` 0700、`hosts.yml` 0600；不声称访问范围已验证。未输出或保存密钥。
- 复测：`PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s Scripts/tests -p test_harness_migration_metadata.py -v`。本机采样不是 macOS/resident 全面 UAT，不是容量/授权准入通过。

## 持续准备进展与权限纠正

- 工作树已通过 `git merge --ff-only` 无损快进至 PR 当前 HEAD `54b680bd1188c9ef8ea98e323a0475fd036ce8d3`；本地探针保留，旧凭据仅按原 Git 历史保全，不新增授权有效性声明。
- sealed CLI `plan-entry-local` 只读回读：local_work、checkpoint、external_publish 均 available，后者 reason 为 fence_and_dependency_available。**纠正此前解释：external_effect_fenced=true 不等于发布被禁止。** 本轮未重复 claim、reinit 或签发凭据；可以按当前能力执行已获用户授权的准备 PR 发布。
- `Scripts/harness-migration-inputs.py` 增加候选机器分类：已列明事实路径可进入 ledger-only 校验；policy、未知路径、代码/构建配置、重命名任一侧影响执行均要求重新验证。输出始终不授权部署。6 项反例/对照测试通过；连同 metadata 共 13 tests passed。尚未接入 release-writer，不能声称 M08 已通过。
- 方案 A 段补齐四项前置验收合同及 M04 错误证据排除，仍标为待验证要求。
- 本机 `du -sx --block-size=1` 观察 kernel 4,042,752 bytes、当前 ledger 48,664,576 bytes；非一致性快照，不包含外部实际 runtime、全部历史和其他节点，不可据此作完整峰值容量准入。
- 在当前 installed runtime 的 `harness/scl_effects.py` 观察到提交前重读 repository facts/fence，随后直接调用 Git push；这是客户端检查与发布链，不是远端拒写实施证明。SSH agent 作用域仍待核实。

## 三节点真实入口采样已完成

准备代码 `a19276e830a7536f701b293f087a18b68b21cfd8` 已推入 PR #466；13 项本地测试通过，独立静态审查无 candidate-tool 范围阻塞。workflow 另经只读审查，修正摘要断言可被 PYTHONOPTIMIZE 跳过的问题，并显式禁止 SSH agent forwarding、禁止复用 ControlPath、强制已有 host key 校验。

Actions run [34446523692](https://github.com/bigbird-online/PULSE/actions/runs/34446523692)，workflow HEAD `e493baaf82abc850e0afaff6cd3451561f84e55e`，在 ownermac / pulse-macos-trusted 成功。只下载 pinned probe 并校验 SHA-256；未 checkout 新仓或复制生产数据；临时脚本在 EXIT 清理。首次使用尚未证明有效的 pulse-macos-runtime 标签的排队 run 已取消，改用当日已有成功证据的 self-hosted/macOS/ARM64 标签；不能把排队断言为节点故障。

| 节点 | 采样 UTC | 明确观察的根 | UID / mode | 所在文件系统 available bytes |
|---|---|---|---|---|
| ownermac | 2026-09-10 06:44:46 | 用户指定 PULSE 安装目录、历史 kernel 候选目录 | 501 / 0700 | 47,575,842,816 |
| tcloud | 2026-09-10 06:44:49 | 历史 kernel 候选目录 | 1002 / 0700 | 13,528,444,928 |
| Vultr | 2026-09-10 06:44:51 | 历史 kernel 候选目录 | 1001 / 0700 | 12,363,198,464 |

三节点 `.ssh` 均为 0700、GH hosts 文件均为 0600、属主与账户相符；只 stat，不读取密钥。未提供的 ledger/runtime/install 根保持 not_supplied。目录存在不证明实际加载/canonical registration；这些数值不是峰值容量准入。runner 自带容量 hook 另报告 watch、inventory incomplete，但允许此次探针；它不等于本迁移容量已准入，未自动删除任何目录。探针 JSON 不输出路径值，但 workflow 固定命令及 runner 自身 hook 日志包含节点路径，不能声称整个 Actions 日志无路径。

## A1 认证主体收敛与硬约束

- 本机 proc 元数据证明 Relay 与 reconciler **同 UID、共用同一个 SSH agent socket**；socket mode 0600、属主相同。未列举 agent keys、未读取密钥正文。
- 实际 inherited ledger checkout 的 origin 是 `github-bigbird-collaborate` 别名下的 CWA；SSH auth-only 检查以实际 publisher socket和既有 SSH 配置通过认证，GitHub 返回 repository principal `bigbird-online/CollaborateWithAI`。SSH exit=1 是 GitHub 无 shell 的常见返回，此处依据明确认证消息，不把 exit 当成失败/写入结果。
- 另一次 PULSE 别名检查返回 `bigbird-online/PULSE`，**不能拿这个不同目标的结果冒充 CWA publisher 身份**。
- CWA deploy-key API 返回两项 enabled/verified 且 read_only=false 的 key；未输出 key 本文、标题或指纹。尚未把本次接受的 key 与具体 API 条目匹配，也未证明全部消费者；不能从此断言两项均为 Harness 专用。
- 当前配置能够使用 CWA repository deploy-key 身份，不能等同此前控制面 user 的 admin 身份。权限隔离设计必须涵盖 deploy key、共享 agent、账户凭据及其他节点的替代通道。
- 独立源码审查确认 `scl_effects.py` 继承环境、在客户端校验后直接 push，且使用空 hooksPath；普通 pre-push hook 无法覆盖此链。若旧代码可获取 CWA 无限制写凭据，而远端/独立凭据持有者又没有不可绕过的拒写门，则仅修改客户端不可能满足 A1。该条件性结论不等于已证明所有远端能力都不存在。

## 完整演练前仍需实现的实际能力

独立静态审查当前 installed release：

- `scl_kernel.py` 仍拒绝 Git 根内 kernel，构造器会初始化存储；不能拿打开目标当作只读导入校验。
- `scl_human_decision.py` 的 Host key 位于独立 scl-host namespace；issuer 缺钥会生成，而验证器缺钥拒绝。迁移必须使用保全与验证路径，不能调用 issuer 补钥。现有 HumanDecision action 仅 reinit，不能移作迁移授权。
- `scl_authorization.py` 的业务 grant、worktree migrate-set、baseline/frontier import 不组成跨根全量 journal/附件/effects 迁移协议；现有 pending fence adoption 也不能替代跨 repo 交付绑定。
- 需在受审源码中实现受限数据根准入、无写入目标核验、一致导出/导入及附件切点、迁移绑定、兼容 runtime/LKG 和恢复后测时。不得以普通 SQLite 文件复制演示冒充真实 Harness 演练。
- 本机 actual outer runtime 观察占用 35,753,984 bytes；它位于 kernel 之外，进一步证明只量 kernel 不构成完整容量清单。

## 当前验收结论与后续边界

A1 未通过：必须先确定并验证不可绕过的旧写拒绝位置；目前仅客户端 fence 明确不足。A2 未通过：真实迁移执行器合同与原 Host 信任保全尚未实现。A3 仅候选分类器及反例测试通过，release/CI 集成尚缺。A4 完成三节点有限采样，缺全量对象闭包、峰值预算和真实协议的隔离测时。

下一实施切片是受审的、只允许隔离根的迁移能力，而不是生产搬运。CWA additional-worktree allocation 已只读取得 planned 状态、write_authority=false；未创建额外工作树，必须先容量准入和 sealed registration，不能借 allocation 直接执行或用 reinit 代替登记。

若实际 CWA 共享直接写权限必须继续对可运行旧 Harness 的同一 OS 主体开放，且不引入远端强制约束，则 A1 无解。需要保留业务能力而改变凭据访问隔离边界，或取得经验证的远端执行边界；涉及生产身份/凭据切换、费用或业务访问模型变化时必须取得对应范围的人类裁定。不得因此缩减数据迁移范围，也不得宣称现在已经完全准备好。

## 2026-09-11：按本轮人类要求继续准备

用户再次明确全部迁移、归拢路径，包含 `~/.local/state/agent-harness/`，并要求准备到具备搬迁条件。本轮承接隔离设计和演练准备，不执行生产停写、凭据切换或数据搬迁。恢复包中的“不搬 kernel”是已被 v4 撤销的旧范围，不作为当前业务决策。当前本地工作能力已通过受审 CLI 读回；本轮未执行 reinit 或复制授权材料。

- PR #466 远端已在原业务 HEAD 后追加三份审计记录；工作树无损快进至 `d9d6b91af3623512253bb6cf313fee2ebc892802`，未改写这些记录。
- 新增 [凭据隔离设计](agent-harness-migration-credential-isolation.md)：明确仅移动 reconciler UID 不够，任意可运行旧代码的 Relay/runner 若仍有原始共享写凭据就可绕过。优先核实远端强制规则，备选为独立凭据持有者的受控发布入口；生产方案、实现和真实多身份演练尚未通过。
- `Scripts/harness-migration-inventory.py` 为只读显式根文件占用采样。只 stat、不读正文、不输出路径/文件名、不跟随 symlink；父目录逐层通过 no-follow descriptor 固定，防止扫描期间祖先软链接替换。缺失、错误、软链接、特殊文件、跨 device 均明确待核对。同 device bind mount 尚不识别，已明确限制；硬链接去重数值不是复制峰值预算。
- 独立审查发现的祖先替换风险已修复并增加确定性回归；复审在准备工具范围未发现阻塞。`PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s Scripts/tests -p 'test_harness_migration_*.py' -v`：23 项通过。这不是 macOS 实测或完整迁移验收。
- 本机 2026-09-11 14:47 UTC 对继承 kernel/ledger 根采样：kernel 普通文件 208 个、logical 3,114,716 bytes、allocated 3,792,896 bytes，**另有 2 个未跟随软链接，needs_review，命令退出 2**；ledger 普通文件 4,213 个、logical 36,970,162 bytes、allocated 47,759,360 bytes。两者同一文件系统，可用 21,132,341,248 bytes，只记一次。数据热写中，数字仅为非原子观察；未声称一致快照、全量对象闭包或容量准入。
- CWA exact allocation 再次返回 planned、write_authority=false；未创建额外 worktree，未在该位置执行。跨仓实现仍必须先取得容量准入、完成受审注册及资源授权；已有 PULSE 准入不延伸到兄弟目录。

### 本轮提交阻塞（未绕过）

本轮准备文件已暂存，但 Git commit 被现有 `pre-commit` 拒绝：`HARNESS_BRANCH_DENIED:schema_incompatible`。只读检查确认工作区 guard 固定引用旧 runtime，而当前受审 SCL launcher 使用较新 runtime；旧 guard 无法读取当前 kernel schema。未跳过 hooks、未设置绕过环境、未手改 hook 或 kernel，也未推送这些变更。

受审 CLI 提供 `adopt-workspace-authority-local`，但需当前 authority 与 registration 的准确摘要及适用的合法变更；不能猜摘要、制造 registration 漂移或把 reinit 当修复。后续须通过支持的受审接口完成 guard 兼容性修复及读回，再提交并运行当前 HEAD CI。本轮本地测试通过不替代该提交/CI 门。

经用户要求修复后，Relay 已接受一次 Host-owned 接续，但接续后的真实 `git commit` 仍返回相同 `schema_incompatible`。纠正此前判断：接续请求被接受不证明 guard 已更新，该修复尝试未成功，不重复排队接续。恢复后的 sparse 工作树仅发现新增 inventory 的 10 项测试并通过；另外 13 项测试文件仍在 Git 跟踪清单，但不在当前物化视图，不能把本次结果说成 23 项复测通过。全部准备改动仍保留，未提交、未推送。

### 提交 guard 修复与边界（2026-09-11 23:32 UTC）

进一步核验实际运行 Relay 的 `continueLocalFirst`：已授权且无待执行 replacement 的分支只 plan、复用当前会话，不调用会执行 consumer 安装/readback 的 `resume`。对实际已安装模块的隔离 mock 调用确认 `plan → commitLocalEntry`、无 resume；这解释了此前重复 Relay 接续未刷新 hook，不能以“请求已接受”作刷新证据。

对同一真实工作区、继承身份的只校验对照：旧 runtime 报 schema_incompatible，新 runtime 的 commit/publish 权限检查通过，缺少 Agent/错误分支负例拒绝。内存生成 wrapper 的逐字对照确认：显式保留 WI/kernel 时只改变解释器路径；普通 broker installer CLI 未传这两项，故不使用其覆盖生产 wrapper。

独立复核当前受审 CLI 后，使用其正常 `continue-local` 路径完成此次已授权消费者修复，继承真实 Host 环境、不传新身份、entry 或 replacement 参数。返回 consumer_readback=ready、两个 hook 已核验，local_work/checkpoint/external_publish available。前后 local_revision 均为 14、state digest 相同、registration digest 相同；返回的 replacement/effects 是此前已完成记录，不是本轮新增 replacement、重发外部效果或 reinit。没有手改 hook、registration、SQLite 或跳过检查。

恢复两份当前 HEAD 已跟踪但未物化的测试文件后，23 项迁移准备测试重新通过。上述只证明本工作区消费者修复及本地准备测试，不证明其他节点 guard 已收敛，也不解决 Relay 活动复用分支缺少运行物漂移检查的长期问题。

## 2026-09-12：跨仓实现准入与真实 schema 隔离恢复

- CWA 迁移准备 worktree 按 exact allocation 创建，先取得 Oikos `admission_allowed=true` 回执，512 MiB 开发增长预算；实际模块跟踪源码约 8.45 MB。回执明确 inventory incomplete，无删除。这是开发工作树准入，不是全量迁移容量准入。
- 使用受审 `adopt-workspace-authority-local --add-repository` 完成正式注册与资源扩展；两个工作树 consumer readback ready，原 WI/epoch/checkpoint 保留。第一次在新建 no-checkout 工作树尚未物化 index 时被 worktree_dirty 拒绝；完成该全新工作树的 checkout 初始化后重试成功。未重置历史/共享目录。
- 注册增加后 external_publish 一度 pending：两个 sparse checkout 均未物化 `.harness`，审计发布器在多仓情况下无法选择原审计目标。只恢复 PULSE 当前 HEAD 已跟踪的本 WI 审计文件，不新建/改写回执；复验 external_publish available。迁移消费矩阵必须覆盖这一 sparse-view 依赖，不能用新仓注册成功冒称发布就绪。
- CWA [PR #399](https://github.com/bigbird-online/CollaborateWithAI/pull/399)，HEAD `875c8d436`：**仅测试夹具**，真实 Kernel schema 的 SQLite backup → 独立 candidate → 接纳新 checkpoint/effect → 用最新 candidate 快照恢复。比较完整状态、operations/effects、全部 checkpoints、source references 与投影字节。缺表、版本不支持、损坏/缺失文件、摘要不匹配、投影和 symlink 反例 fail closed；构造器不能把缺表库初始化为空业务库。
- 第一次审查发现新 effect 未断言与缺表会被补建，已修正并复审。真实 pytest（不是兼容 shim）执行 fixture 与 kernel/effects/registration/workspace-extension 相关测试：66 passed。没有生产迁移 API、外部 Host 附件/私钥访问或原地回灌。这些用例不代表并发一致切点、跨机身份接续或全量演练完成。
- PULSE 新增 `Scripts/harness-migration-budget.py`：纯 JSON 分文件系统、分阶段额外峰值计算，已占用源不再扣一次，同阶段先分配再释放计重叠峰值；重复 filesystem/device、缺项、无说明零值、重用 allocation ID、异常 JSON 均 fail closed。永不签发容量准入。新增 11 项用例，PULSE 迁移准备测试累计 34 passed。
- 本机无特权 user namespace 试验被 OS 拒绝（uid_map Operation not permitted）；未扩大权限、创建生产账户或修改凭据。A1 真实多身份隔离需使用获准的隔离 runner/环境，不能用同 UID mock 冒充 OS 隔离。

### A2 可复用的只读离线快照校验组件

CWA PR #399 的 `c6e14ebee` 已通过完整 hermetic-contracts 与 packaged real-CLI persistence smoke（run 34660541767，18m13s）。等待该轮完成后再推送新组件，未为追加提交取消这轮验证。

新 HEAD `93d53bf94` 增加 `harness/scl_migration_validation.py`，从测试夹具提取实际可复用的只读校验，不调用 Kernel 构造器，也不提供生产导出/写入/切权。允许的输入是**调用方保证全程稳定的已导出离线快照**；当前 SQLite + 显式 read-only projections，未知外部附件命名空间必须拒绝。输出仅计数和摘要，不是迁移授权或容量准入。

独立审查要求修复的摘要/行数不一致风险、根祖先 symlink、JSON 重复键、深嵌套/超大清单、异常链泄漏、候选构造前验证缺失均已处理。只解析一次 manifest 并计算同源摘要；复查文件集与摘要以拒绝可观察变化，但不冒称对抗性原子性或并发冻结协议。未知 views/triggers 拒绝；SQLite 只读连接明确关闭。候选副本在初始化前重新完整验证，不能自动补建缺失事实表。

新组件/夹具与 kernel/effects/registration/workspace extension 合计 73 项本地测试通过，ruff 通过，独立复审无该离线切片阻塞。新 HEAD 全量 CI 待结果；此前通过不能冒充新 HEAD 通过。PULSE 上一 HEAD 两项 CI 已通过。

### Host 离线副本保全组件与迁移授权边界

CWA `93d53bf94` 已通过完整 hermetic-contracts（19m14s）与 real-cli-runtime-uat；PULSE 对应 HEAD 两项 CI 通过。新增 Host 组件在等待这些检查完成后推送至 CWA PR #399 `d6b9dae2a`，新 HEAD CI 待结果。

`harness/scl_host_trust_preservation.py` 只校验两份调用方保证稳定的离线副本：现存 32-byte key、Relay hash-only 与 Codex prefixed 两种输入回执、recovery history/current index、adopted resources、damaged-inputs 原始损坏材料及被动 resource-action-lock 文件。保全原字节及严格 owner/mode，拒绝漏项/多项/篡改、未知命名空间、链接/特殊文件、跨树任意文件共用 inode、缺钥/错误长度与可观察变化。返回只含计数/布尔值，migration_authorized 始终 false；不访问默认 Host 根、不生成/签发/重签任何真实回执，不验证历史来源真实性、当前性或新根上的 scope 可用性。

独立审查发现的非 key 硬链接漏洞已修复；额外按真实 producer 源码补齐 Codex 回执和 damaged-inputs/锁文件目录，不能让被损坏的历史材料静默丢失。原始 JSON 可以损坏，只比较保全字节；锁文件副本不代表活动 OS 锁或逻辑 lease 已恢复。34 项 Host 定向用例，连同 snapshot/kernel/effects/registration/workspace-extension/Host recovery 合计 120 项本地测试通过，目标 ruff 通过。

子任务曾在不含 CI 所需 ssot/workflow 文件的开发 sparse 视图直接运行全量测试，报告 3092 passed、306 failed、13 errors。此运行不能作为全量通过，也未经对照不能把所有失败一概归为既存；全量结论以具备完整测试输入的当前 HEAD Actions 为准。

当前多仓审计 probe 的 sparse-view 依赖再次出现：仅一次 restore 的审计文件可在 reapply 后消失。本工作区已将当前 WI 的已跟踪审计目录、Scripts/tests 和本地 memory 目录加入持久 sparse 视图，未改变 registration 或权限范围；重跑 sparse-checkout reapply 后 external_publish 仍 available。此修复只证明当前已登记工作树，不宣称所有消费者已改用显式审计仓合同。

迁移授权缺口已收敛到 [迁移专用授权合同](agent-harness-migration-authorization-contract.md)：现有动作只有 reinit，且旧 scope 绑定原路径；必须有来自真实 Host 输入的新迁移动作及独立旧→新映射，不能因为 key 一样就放行新根。该合同未声称接口已实现。

**当前仍不可搬迁。** 下一切片必须实现迁移专用 Host 授权/验证及路径映射，而不是再以离线校验替代授权。跨根 export/import/效果绑定/冻结协议、A1 真实身份隔离、A3 发布链分类器集成、A4 三节点容量与维护测时仍未闭合。
