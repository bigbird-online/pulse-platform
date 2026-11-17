# agent-harness 整体迁入 PULSE：源码、运行数据、发布权与入口统一方案（历史提案）

> **已被用户确认的新方向替代，不再作为实施准入合同。** 当前方案见 [PULSE 单仓库与统一部署布局](pulse-platform-layout-plan.md)。运行数据允许位于 Git 外的固定全平台根；本轮停止扩建迁移授权系统，只提交全平台规划待独立审核。下文保留历史推理与证据，不代表当前执行范围。

> work_item: `wi_20260823_184`（CR-02；沿用关联，本轮未接管或变更生产 WI）
> status: `proposal_for_review`
> revision: `4`
> revised: `2026-09-09`
> supersedes: v2 @ `8fa7490c4811c8ac7f423403a3cad60873241ce2` 的源码单迁范围
> 本轮只修订方案。下述目录、迁移事务及验收是目标合同，不是已实施能力或验收通过声明。

## 1. 不可裁剪的目标

用户明确要求：**PULSE 应当内聚，agent-harness 的源码和运行数据必须一起迁入；只迁源码不满足需求。**

因此撤销 v2 对范围的擅自缩减：Git `.harness` 数据、数据发布权、本地 kernel 和 Harness 专属运行状态都纳入本次迁移，不再列为可选项、另案或以后才能完成的工作。分步执行只是降低切换风险，不改变最终范围。

**唯一完成结果：Agent 从原来的正常入口，在 PULSE 所拥有的代码、数据、运行物和控制面上继续原 WI；原 Harness 源码目录、CWA `.harness` 活动权威和旧独立运行状态根退出使用。后续开发、发布、运行、观测、备份、恢复与退役由 PULSE 一体承担。**

运行数据按存储特性决定是否进入 Git，但不因此分属不同仓库或不同平台。SQLite 不提交 Git，不等于继续留在 PULSE 外；节点各自保存本地事务，不等于继续保留跨仓权威。

## 2. 本轮核实的基线与必须处理的断点

本轮重新读取：PULSE main `288d420a34f69046f0221cd0ab771604b7f0d4d9`；CWA main `8773da536da77b1fb2a99ed2c28001a6d2cfccf3`；PR #466 head `8fa7490c4811c8ac7f423403a3cad60873241ce2`。实施开始和切换前重新解析 ref，记录源码 tree 与账本 frontier，不用本文 SHA 冒充未来现场。

| 已核实事实 | 本次必须完成的改动 |
|---|---|
| `external_state.os_account_state_root()` 固定 OS 账户下 `.local/state/agent-harness`；`require_outside_git_roots()` 检查实际祖先，拒绝任何 Git 根内状态 [E1] | 修改存储合同、根解析和相应测试；不能只设置环境变量、删一条检查或用外部 symlink 假迁移 |
| `scl_runtime.default_kernel_root()` 又有 `SCL_KERNEL_ROOT`/`XDG_STATE_HOME`/HOME 解析；sealed release 校验绑定文件内容与路径 [E2] | kernel、运行物、恢复读取器和普通 CLI 使用同一安装根解析；迁移后不能静默回退旧状态根 |
| `config/scl-surfaces.yaml` 的 canonical、resident 和 Stats terminal consumers 均仍指向 CWA [E3] | 改为 PULSE 数据 authority，连同发布器、收口、Stats 和复制拓扑一起验收 |
| CWA `.harness` 有 work-items、handoffs、executions、continuations、events、evidence、policies 等；PULSE `.harness` 已有 evidence [E4] | 迁移完整 Harness 数据集合并保全两边记录，不覆盖、不只筛出 Pulse 自己的 WI |
| exact-tree builder 固定旧模块路径；通用 Harness stable pointer 与 canonical SCL 可分别安装/采用 [E5] | 构建与四个 CLI、Hook、reconciler、Relay 实际绑定均迁移并回读 |
| PULSE release-writer 要求待部署 SHA 等于当前 main；现有源码归档会取整棵 Git tree [E6] | 处理同仓账本更新与代码发布的关系，不能每次 checkpoint 都触发重装，也不能把整份业务账本塞进代码 release |
| ChatGPT entry workflow 仍引用旧 `.venv`、旧 SHA/branch 和源码 wrapper；rollover CI 尚无新 Harness 路径 [E7] | 活动 Actions、加载方式及 CI 触发条件随迁，不留给用户排障 |

本轮是仓库取证，未现场读取各机器数据库、进程与安装状态。过期 STATS、历史安装记录不能作为当前证据；相关状态保持 unknown，须在实施清单中刷新。关联 Issues #251/#252/#253/#256 是历史入口单，其 closed 状态不代表本迁移完成。PR #377 当前仍 open，承接其授权原则与在飞修改，不宣称它已实现或已合并。

## 3. 统一落点：一个 PULSE 家目录

`PULSE_HOME` 是本文对每台常驻节点稳定 PULSE 安装/工作根的称呼，不是让普通 Agent 每次填写的新参数，也不是另建控制服务。在 ownermac 固定为用户给出的 `/Users/owner/Coevolution/Bigbird/PULSE`；其他节点按当前已登记的 PULSE 根解析，实施时回读。

源码目录保留原名 `agent-harness`，直接迁至 PULSE 根下；ownermac 的目标路径为 `/Users/owner/Coevolution/Bigbird/PULSE/agent-harness/`。保留现有模块内部布局、包名 `agent-harness`、Python import `harness` 和四个 CLI 名称，只更新迁移所需的路径引用，不额外改名或增加目录层级。

```text
PULSE_HOME/
├── agent-harness/                   # 源码、tests、specs、构建/安装/迁移工具
├── .harness/                        # PULSE Git 中的持久协作记录、策略及审计
│   ├── work-items/ handoffs/ ...
│   ├── evidence/
│   └── ...                          # 原 .harness 完整清单，不限于这些例子
├── data/agent-harness/              # 本节点运行数据，物理在 PULSE 内，不入 Git
│   ├── scl-kernel/                   # SQLite、journal、checkpoint、attachments、effects
│   ├── runtime/                     # wheel 安装环境、不可变发布物与 active/previous
│   ├── worktrees/                    # Harness 注册/管理元数据；不是搬走业务工作文件
│   ├── git-broker/                   # Broker 状态、事务、恢复记录
│   ├── service-repositories/         # Harness 权威数据专用 managed checkout
│   └── migrations/ backups/ ...      # 迁移回执与受控恢复材料
└── config/、Scripts/、.github/       # 同一平台的配置与交付控制面
```

沿用 PULSE 的 `data/` 本地数据约定 [E8]；上述子目录是建议落点，实施清单逐项给出旧路径→新路径及负责迁移的现有组件，不以目录示例遗漏实际 namespace。kernel 内已有 sealed wrapper 子树可保留结构；外层 venv/release 也必须迁至 PULSE 内。

### 3.1 Git 与本地数据是同一平台的两种存储，不是两套归属

- 原 CWA `.harness` 的持久记录、策略、历史证据和必要归档进入 **PULSE 仓库**。已有 PULSE 记录合并保全；不为源码迁移重新创建 WI/HO 或改业务归属。
- SQLite、WAL、运行锁、私有附件、安装环境等放入 **PULSE_HOME/data/agent-harness**。三机分别迁自己的状态，不用一台数据库覆盖另一台，不把 SQLite 当成 Git 多机同步文件。
- 保持现有 managed service checkout 与人类编辑目录的隔离。正式 publisher 使用 PULSE_HOME 内 Harness 专用 PULSE service checkout 的 `.harness`；人类根的 `.harness` 是同一 PULSE Git 数据的 checkout 视图，不是第二个写入权威。由现有同步/authority carrier 报告 freshness，不允许两个本地目录各自称为 canonical。
- 常用 `~/.local/bin` 命令可以是指向 PULSE 内实际运行物的薄 launcher；不能把 launcher 更新当成数据已迁。最终 realpath 检查必须证明工作数据、运行物及正常 LKG 恢复均不再依赖旧独立根。
- 不移动 CWA/Oikos 等仓库中的业务成果、个人知识库或其他 Agent 的工作目录。它们是 Harness 所服务的对象，不是 Harness 自己的数据存储。它们对 WI/HO 的读取入口则必须转向 PULSE。

### 3.2 让运行数据安全地位于 PULSE 内

现有“必须在所有 Git 根之外”改为“只能在登记的稳定 PULSE 数据 namespace 内，且不受源码 checkout/release 生命周期管理”。不是放开任意 Git 目录写数据库。

统一解析器由已登记安装根和 OS 账户确定目标，校验 canonical realpath、属主、权限、未被 Git 跟踪及允许的 namespace。PULSE 位于 CWA 根之下时，也要检查祖先仓库不会跟踪这批私有数据。任意 task worktree、临时候选 checkout、release 目录或 symlink 逃逸均不能成为生产数据根；从新 clone 启动也不能误建空生产 kernel。

修改并验证所有受管理的 clean、workspace reclaim、deploy、archive、同步与容量回收路径：不得删除、复制进 release 或覆盖该数据根。只写 `.gitignore` 不够；未知的手工破坏性 shell 命令也不能被声称天然安全。维护工具要保护登记的数据根，必要的整根处置必须先执行受控迁移/备份。

## 4. 数据搬运合同

### 4.1 每个对象有去处，每个冲突有结论

以冻结点的 Git 跟踪清单和各节点本地文件/数据库清单为输入，记录对象类别、旧位置、新位置、生产者、消费者、当前 authority、保留规则和验证方式。CWA `.harness` 当前其他目录如 admissions、audit-corrections、reviews、plans、family-members、reeve-space、scan-reports 和 locks 同样逐项归类，不默认忽略。

同一路径/ID 的同内容可按来源证明去重；内容不同的不可变凭据必须保留两份原始证据并解决身份冲突，不能靠文件 mtime 或“目标版本较新”覆盖。活跃 WI 的 Git 投影与本地 frontier 按现有 generation、operation sequence 和合法未发布操作核对，不能用较旧 Git YAML 倒灌覆盖较新本地 checkpoint。

源码与 Git 数据均保留相关历史：在可丢弃 clone 中过滤模块路径、`.harness` 及已确认的历史别名，非 squash 导入；纯导入、冲突处置、迁移适配分开提交。记录原仓/ref/SHA、路径与 commit map，避免夹带无关 Coevolution 历史。原签名/审批/凭据 bytes 不改写；所需旧引用以 PULSE 内的内容及迁移映射解析，旧 URL 保留出处意义，不成为日常读取必须回 CWA 的依赖。

### 4.2 本地 kernel 搬位置，不重建工作历史

迁移保留 WI/HO ID、checkpoint 内容与序号、epoch、lineage、source attachment、业务授权/委托、operation/effect ID 及已接受结果。位置、ledger repository identity、registration digest 或发布 fence 确需改变时，由专用迁移事务产生新绑定与旧→新证明，不伪称全部 digest 必须不变，也不修改旧的不可变凭据。

冻结相关写入者后，以 SQLite 支持的一致性备份方式导出，并同时记录相关文件/附件/journal 的一致切点；验证数据库完整性、关联关系、记录数与关键对象摘要 [E9]。不能在热写期间只复制 `.db` 主文件，也不能认为一次数据库 backup 已自动保证外部附件的一致性。

复制数据不等于复制进程：OS 文件锁/PID 不作为存活锁搬运；旧进程退出并关闭连接后在新根重新建立运行锁。逻辑 lease 和未结事务作为事实保留，由恢复流程观察/接续，不把迁移当成把所有 lease 清空的理由。

迁移窗口中，相关 kernel 的持久写入会有受控短暂停顿，不能一边冻结一致切点一边声称 checkpoint 仍照常持久化。正常对话、请求人类授权和不依赖该写入的已授权业务工作继续；平台明确提示维护中的能力，不扩成全局业务禁令。

### 4.3 未结 effects：保留业务身份，显式迁移发布绑定

| 原状态 | 迁移处理 |
|---|---|
| 已成功/已提交 | 保留回执及原目标证明，核对读回；不重新执行 |
| 已领取、已发出、结果 unknown | 停止新的 claim，观察原 worker/原目标并形成处置记录；unknown 不能等同“未执行” |
| 可以确认尚未发生的 pending/retry | 原 operation/effect 身份保留；通过迁移绑定明确后续目标/fence，不原地改掉原请求的摘要 |
| 冲突或无法证明外部结果 | 在 PULSE 内完整保留并明确 scoped pending/conflict；不盲重放，不伪造成功，不阻断无关本地工作 |

切换前尽可能排空旧发布队列。不能排空的对象，必须先实现并验证迁移后的 resolver/adoption 行为，保证原身份只产生一次应有结果；如果该 effect 的内容寻址身份包含旧 repo/path，应保留原 envelope，以独立迁移关联表达新的交付绑定。目标变更不是制造一个没有关联的新业务操作。

未知的第三方外部结果可以继续待核对，但旧 CWA `.harness` 不得因此继续承担活动发布权；旧 Git 发布回执、相关对象与验证材料必须先保全到 PULSE。未实现这种接续时，迁移仍未完成，不能把数据迁移从需求中删除。

## 5. 必须随迁的最小实现

这是本次迁移的实现范围，不另建通用迁移平台或常驻恢复服务。先检查已有原语能否复用；以下能力未由本轮证明全部存在，不能把下列名称当作现有可执行命令。

| 范围 | 必须完成 |
|---|---|
| 根与存储准入 | 收敛 `external_state.py`、`scl_runtime.py` 及所有 kernel/store/registry 调用者的根解析；安装声明绑定稳定 PULSE_HOME；同步修改与此冲突的 normative specs 和测试 |
| 导出/导入/切权 | 在现有 CLI/安装/Broker 事务链上补齐 `prepare → export/import → cutover → verify` 能力；一份可恢复的迁移记录，覆盖旧/新根、源/目标 repo、frontier、对象映射、阶段和读回，不另造平行权威数据库 |
| 原子性边界 | 本机文件替换可原子，跨节点、跨仓库操作不可假定原子；每一外部步骤必须先读回再重试。只用任务级维护协调和已有事务锁，不新增长期全局工作总闸门 |
| 构建/运行物 | 源码路径改为 PULSE 根下 `agent-harness/`；保留模块内部布局、包名、import 和四个 CLI；exact wheel 校验源码与包内对应文件及必需资源；在 PULSE 新根重建 venv/路径相关 wrapper，不能直接移动 venv 后宣称可运行 [E10] |
| PULSE 内可用 LKG | 在切换前准备能读新数据根、识别迁移绑定的兼容恢复版本；重建后记录自己的新摘要与原来源，不能改标签冒充原 artifact。旧位置的原 release 只作切换前保险/历史归档，不是最终正常回退依赖 |
| 数据 authority 与下游 | 改 Harness ledger repo、managed checkout、Broker publisher、terminal consumer inventory、Stats authority carrier/revision/watermark、Outcome、ComputeCost 与所有直接 `.harness` 读取者；业务 worktree 自己的 repo 身份保持原语义 |
| 原入口 | 逐项迁 CLI/Hook/Relay、ChatGPT entry/rollover、已启用 cron/dispatch、安装与恢复 launcher；不仅改 `HARNESS_ROOT`，还要改默认根、显式配置及 fallback。禁用服务不擅自启用 |
| CI/guards/文档 | Harness 改动触发真正的 Harness + Relay/Stats/ComputeCost 契约测试；数据变更触发数据/引用校验，不触发部署；更新 README、PROTOCOL、workspaces、skills、SSOT 接口引用，删除失效跨仓中转 |

### 同仓版本规则

统一在 PULSE 管理，不把实时数据 revision 误当成代码必须重发版的信号。既有 release manifest 保留 approved source SHA、构建输入闭包摘要、wheel/依赖/配置摘要；authority carrier 保留数据 repo/ref、frontier 和 watermark。两者各证明自己的对象，不另设一个数据仓库。

实现中修正 release-writer 的“必须等于随时移动的 main SHA”条件：安装的源码 SHA 必须已审核合入 main，当前 main 必须包含该来源且可执行输入闭包未变化；闭包包括真实的源码、构建脚本、依赖锁定和影响执行的配置，不仅一个 Python 子目录。只有账本变化可以免重建；代码或运行策略改变不得借数据豁免绕过审核。构建物只打包实际代码/依赖资源，不打包整份 `.harness` 或本地数据。此规则及反例测试在切换前通过，不能只放宽 equality 检查。

## 6. 同一迁移的实施顺序

### A. 盘点、固定切点与隔离演练

重新确认所有活动节点/入口，包括 ownermac、tcloud、Vultr、ChatGPT+GitHub，及动态拓扑中新出现的活动消费者。记录三类清单：源码/在飞 PR；Git 数据与各节点运行状态；生产者/消费者/凭据准入及恢复路径。只读探针绑定 exact ref/SHA，敏感内容不上传 Actions 日志。

在隔离副本上跑完整“代码导入＋账本合并＋本地状态迁移＋切权＋续做＋回退”演练。相关在飞 PR（包括 #377）记录原 head、review 状态和承接方式，不自动合并或丢弃。不能用历史入口 Issue closed 或旧测试数证明迁移通过。

#### A 阶段必须先交付的四项准入证据

以下是停写窗口的前置门，不是到 C/D 再补的文档任务。逐节点状态只能是 passed / failed / unknown；unknown 不得按通过处理。当前证据与本地探针见 [Phase A readiness](agent-harness-migration-phase-a-readiness.md)，其存在不代表迁移已准入。

1. **旧通道拒写**：清点实际 publisher/reconciler/Actions 的 OS 主体、凭据来源类别、目标 repo/ref 及拒绝执行位置；不导出密钥。证明旧进程、旧版本、旧 fence 与共享凭据直推均无法越过切权约束，且 CWA 非 Harness 业务仍可工作。远端 branch protection/ruleset 未取得证据时不能假定可用；旧身份若仍能读取共享凭据或连接共享 SSH agent，本地 fence 不足。必须先完成可执行的权限隔离/专用通道方案及隔离负例，失败不得进入停写窗口。不擅自撤销共享权限、升级套餐或公开仓库。
2. **执行器授权**：记录受审工具 exact revision、实际 OS/Host 主体、真实认证人类授权来源、对象清单摘要、允许的命令/根/repo/ref、授权期限和撤销条件。A 阶段只读盘点与隔离副本写入，不授权生产 export/import/cutover。C/D 需另行取得其明确授权，工具必须拒绝越界路径、过期/撤销授权、版本或清单失配及目标生产写权提前开放。不得自签 decision，不能把 exit=0/local_committed 当作授权来源证明。Host 密钥、信任根及关联读取器逐项登记保全或受控轮换方式，正文不进 Git/日志；缺失或无法验证时停止，不静默新建，不重签旧凭据。
3. **可执行输入闭包**：为已确认的纯事实账本路径建立窄白名单，运行 policies、构建资源、脚本、依赖与执行配置始终属于可执行输入。未知路径默认要求重新验证，不按 `.harness` 总目录豁免。同一组对照测试证明 checkpoint-only 可免代码部署、policy/混合变更不能免验；路径删除、重命名按新旧两侧分类，任一侧影响执行即重新验证。分类器只能作为 release-writer/CI 的共同输入，不能替代 approved-source ancestry、内容摘要和数据引用校验。
4. **容量与维护预算**：按实际文件系统记录旧库/附件、目标候选、临时一致性导出、备份、兼容 LKG、安装构建临时物及增长余量；已有占用与额外峰值分开，同一文件系统 free space 不重复累加。每节点容量准入后，在隔离副本实测一致导出、校验、恢复与切权阶段耗时，事先记录停写上限和逐阶段退出点，不事后提高上限使失败变通过。尚未有新写入时按 §8 读回恢复；已有新事实则不可回灌旧快照。维护响应必须区分未接纳、已接纳待处理和结果未知，不返回虚假持久化成功；重试保持原业务身份，仍保留普通对话及授权请求。

M04 另需排除历史错误授权证据：自行签发的 decision 即使返回 local_committed，也不能作为真实 Host 授权连续性的通过证据。原材料保持不变，另从真实认证 Host 入口验证，不能靠重签或重写历史修饰来源。

### B. 新路径和迁移能力先就绪，不先删除旧系统

通过 PULSE 实施 PR 完成源码/历史导入、必要适配和 migration-aware 发布物。CWA 配对 PR 准备旧写入通道退役、文档引用和源码退役；它们是同一交付，不各自声称业务完成。

先合入经验证的迁移能力，再准备 PULSE 内的正式运行物、数据候选和兼容 LKG；候选无生产写权。迁移逻辑不能要求从正在迁移的旧 SCL 获得一条不可用的准入链才能自救：由用户已确认的迁移范围、受控 GitHub 操作和最小权限执行器落实，保留审计，不制造业务 reinit。

### C. 受控停写、一致导出与物理迁移

停止受影响的旧 publisher/reconciler 接新 claim，核对在途 effects；按节点短暂停止相关 kernel/registry 写入，关闭连接，取得一致快照及文件清单。在 PULSE 内导入并只读核验，此时目标尚未开放业务写入。普通业务会话与授权请求不被整体停用。

每台节点必须按本机基线导出并验证；离线节点不能被当成空库或自动跳过。未能迁移的活动节点在写入层被明确隔离，恢复时先采用当前迁移事实；在全部所需节点完成或被用户明确退役前，不宣称全量迁移完成。

同一窗口固定 CWA `.harness` 发布切点，合并 PULSE 已有证据及期间合法新增记录。其他 CWA 业务源码无需全仓冻结；切点比较要识别是账本变化还是无关源码变化。保护人类未提交工作，不 reset/clean 共享根。

### D. 单一权威切换，再开放新写入

在所有必需目标完成只读核验后：旧 Harness 发布权退役并读回 → 更新 PULSE 目标及迁移绑定/必要 fence → 各节点加载 PULSE 内数据与运行物 → 读回 authority carrier/消费者 → 开放新写入。顺序通过一份幂等迁移记录推进，不假装两仓 Git push 是一次原子提交。

停止旧进程本身不等于防复活。旧版本、旧任务和旧 fence 重启后必须在提交外部效果之前被拒绝；验证现有 Broker/提交准入是否能执行该约束。只在旧工作流里加检查或事后 CI 不算远端拒写证明。若现有写入通道做不到，就在本迁移内补齐专用发布准入或撤销可绕过的 Harness 专用通道，并测试；不擅自撤销其他业务所需的共享 Git 权限。

源码仓、数据仓和发布通道最终均为 PULSE；业务 worktree 的仓库仍可为 CWA/Oikos。迁移引入的 ledger identity/fence 变化不能被扩大为业务 owner、有效委托或授权失效。物理搬运不重启一条新的工作历史。

### E. 真实路径验收与旧源退役

先由实施者跑通原入口和故障恢复，再由独立验收者核验第 7 节。使用同一受控 WI 证明迁移前后连续性；不拿真实在飞 WI 做破坏性故障注入。

完成验收后，退役 CWA 旧活动 `.harness` 写入口和源码，保留必要迁移指针/历史引用；同步本机 PULSE 根和现有注册端点。旧状态根可以保留有删除条件的只读安全快照，但不能被默认探测/fallback/后台任务重新当成运行根。正常 LKG 恢复必须已在 PULSE 内独立成立，再按容量与保留合同清理旧快照、临时 workflow/branch、测试 checkout 和发布候选。

正式执行与验收控制面为 GitHub PR/Actions。构建与普通测试优先 Linux runner；涉及 ownermac 的安装、盘点和真实入口验证经 GitHub Actions → 可信 self-hosted runner。临时工作流仅从可信分支调度，绑定明确 ref/SHA、最小权限，不直接运行不可信 PR 内容去接触生产数据。Desktop Commander 不可用不是任务停止条件。

## 7. 完成门：有一项未过，就不能称整体迁移完成

| 编号 | 验收结果 | 必须包含的反例/证据 |
|---|---|---|
| M01 统一归属 | 源码和完整 Harness Git 数据均在 PULSE，历史与两边证据保全 | 源/目标对象清单、冲突结论、commit/path map；不能只迁 Pulse 自己的 WI |
| M02 物理归位 | 每个活动节点 kernel、注册/Broker 状态、实际运行物及可用 LKG 均在稳定 PULSE_HOME 内 | realpath/属主/受管理保护检查；旧外部路径不可访问仍工作；外部 symlink 不算通过 |
| M03 单一根解析 | 四个 CLI、Hook、Relay、Actions、调度及恢复读同一登记根 | 从不同 cwd/task worktree/新 clone 启动不能另建生产库；默认发现不回旧根 |
| M04 数据与工作连续 | 原 WI/HO、成果、checkpoint、授权/委托、lineage 和 pending facts 保留 | 迁移前含未提交业务文件；迁后续做、checkpoint、rollover、fresh-session resume；不要求业务 reinit |
| M05 唯一发布权 | 新操作发布到 PULSE；CWA Harness 旧通道不能再写 | 旧进程/旧工作流/旧 fence 重启与并发负例；单纯关进程、exit=0 或事后 CI 不算 |
| M06 effects 守恒 | 已完成不重放，未知先观察，可迁 pending 绑定明确 | 在旧目标已经提交但客户端超时、已 claim 未返回、重复执行迁移的用例中无丢失/双发 |
| M07 消费者一致 | Stats、ComputeCost、Outcome、终态结算及适用渠道消费 PULSE 同一 authority | provenance、数据 watermark、目标 repo 与原 ID 对照；旧数据根存在也不能掩盖新配置失败；缺失保持 unknown/stale |
| M08 同仓不互相破坏 | 数据更新不强迫代码部署，代码更新不擦除状态 | 只有 `.harness` 更新时无重装；可执行闭包/策略改变必须重新验；受管理 clean/deploy/reclaim 不损伤数据根 |
| M09 可恢复 | 任意迁移阶段中断可幂等接续；新写入后回退不丢结果 | 跨仓切换部分成功、SQLite/附件切点、指针/服务切换中断；回退后仍可见新 checkpoint/授权/效果 |
| M10 退役闭环 | 原源码与旧数据根退出活动使用，PULSE 默认分支/运行物/真实入口一致 | 隔离测试禁用所有旧 Harness 根，原入口和 PULSE 内 LKG 均通过；三机/其他活动节点逐项新证据、临时资源清理 |

故障注入用隔离副本或明确授权的验收对象。kernel、权限或恢复读取器故障不取消正常对话和向人类请求授权的权利；人类决定与已持有的合法委托不因迁移 bookkeeping 失败而失效。仍需如实区分“已授权”“机器访问尚未落实”“持久化失败”，不伪造已完成。

这些完成门属于同一次迁移。只完成源码导入、某一台安装或一组 CI，均不能关闭 CR-02。迁移所需关键能力尚缺时，应在本迁移内补齐并验证，不通过删去数据范围获得通过。

## 8. 回滚与备份

**保留工作事实，恢复服务；不能为回退程序恢复旧世界。**

| 所处阶段 | 允许的恢复 |
|---|---|
| 新目标尚无被接受的业务写入，尚未切权 | 保留候选与诊断；核验原切点和实际外部效果后恢复旧服务。不把超时当作未切换，先读回再判断 |
| 只发生部分切换/迁移 bookkeeping | 按迁移记录读回两侧权威、绑定及数据状态，幂等续做或显式撤回未生效步骤；已经产生的 fence 不能直接调小 |
| PULSE 已接受新 checkpoint、授权或外部效果 | 优先回退到 PULSE 内兼容 LKG 程序，继续使用当前 PULSE 数据及发布权；不恢复旧 SQLite/YAML、不把 Git branch reset 回旧账本 |
| 确须退回旧位置/旧发布目标 | 用当前最新权威事实执行受控反向迁移和新一轮单调切权，保留新事实；作为本次已演练的恢复分支，不靠旧快照覆盖，不另建 WI 掩盖未完成 |

备份覆盖 Git authority 的确定 revision 和每个节点的一致 kernel/附件/迁移状态。清单及恢复工具归 PULSE；加密异机/离线备份可以在独立故障域保存，但那是灾备副本，不是 PULSE 外的活动数据真源。恢复测试要证明 Git clone 本身不足以重建未发布的本地事务，必须按节点恢复相应备份，不能静默初始化为空库并报告健康。

## 9. 边界与提交要求

本次包含：源码及历史、完整 Harness 数据、local kernel 与运行状态物理迁移、Git 数据 authority、活动消费者、迁移必需的存储/发布原语、备份/回退与旧路径退役。**数据不再是排除项。**

不顺带搬其他业务仓库成果，不重写全部 Relay/Stats 产品，不启用停用服务，不重建现有 WI。PR #377 的完整业务纠错重构独立保留，但迁移不能破坏其最高授权原则；迁移自身所需的权限、恢复和身份接续必须在本轮成立。

实施提交分清纯导入、程序/规范适配、数据迁移回执、旧源退役，保证审核可定位。不能为了全量数据迁移手工修改 SQLite/YAML/registration；由可审查、可重试、可验证的迁移工具执行必要转换。源码格式检查/自动 CI 不替代迁移验收。

v2 的错误范围在 Git 历史中可追溯，本稿与 PR 说明明确替代它。本轮仅修改本方案和 PR 元数据，原 `.harness/evidence/scl-operations/` 凭据保持不变；没有宣称生产数据已搬、权限已切或迁移已通过。

## 10. 证据入口

- E1：[external_state.py，OS 账户根与 Git 根排除](https://github.com/bigbird-online/CollaborateWithAI/blob/8773da536da77b1fb2a99ed2c28001a6d2cfccf3/Bigbird/Lab/BigbirdEnvironment/agent-harness/harness/external_state.py)。
- E2：[scl_runtime.py，默认根与 sealed manifest](https://github.com/bigbird-online/CollaborateWithAI/blob/8773da536da77b1fb2a99ed2c28001a6d2cfccf3/Bigbird/Lab/BigbirdEnvironment/agent-harness/harness/scl_runtime.py)；[scl_kernel.py](https://github.com/bigbird-online/CollaborateWithAI/blob/8773da536da77b1fb2a99ed2c28001a6d2cfccf3/Bigbird/Lab/BigbirdEnvironment/agent-harness/harness/scl_kernel.py)。
- E3：[PULSE terminal consumer inventory](https://github.com/bigbird-online/PULSE/blob/288d420a34f69046f0221cd0ab771604b7f0d4d9/config/scl-surfaces.yaml)。
- E4：[CWA .harness](https://github.com/bigbird-online/CollaborateWithAI/tree/8773da536da77b1fb2a99ed2c28001a6d2cfccf3/.harness)；[PULSE 已有 .harness](https://github.com/bigbird-online/PULSE/tree/288d420a34f69046f0221cd0ab771604b7f0d4d9/.harness)。
- E5：[builder](https://github.com/bigbird-online/CollaborateWithAI/blob/8773da536da77b1fb2a99ed2c28001a6d2cfccf3/Bigbird/Lab/BigbirdEnvironment/agent-harness/scripts/build-runtime-release.py)；[installer](https://github.com/bigbird-online/CollaborateWithAI/blob/8773da536da77b1fb2a99ed2c28001a6d2cfccf3/Bigbird/Lab/BigbirdEnvironment/agent-harness/scripts/install-runtime-release.py)。
- E6：[PULSE release-writer](https://github.com/bigbird-online/PULSE/blob/288d420a34f69046f0221cd0ab771604b7f0d4d9/Scripts/pulse-release-writer.py)。
- E7：[ChatGPT entry](https://github.com/bigbird-online/PULSE/blob/288d420a34f69046f0221cd0ab771604b7f0d4d9/.github/workflows/chatgpt-wi-entry.yml)；[rollover CI](https://github.com/bigbird-online/PULSE/blob/288d420a34f69046f0221cd0ab771604b7f0d4d9/.github/workflows/session-rollover-ci.yml)。
- E8：[PULSE .gitignore](https://github.com/bigbird-online/PULSE/blob/288d420a34f69046f0221cd0ab771604b7f0d4d9/.gitignore)。
- E9：[SQLite Online Backup API](https://www.sqlite.org/backup.html)；[SQLite WAL](https://www.sqlite.org/wal.html)。
- E10：[Python venv](https://docs.python.org/3/library/venv.html)；[git-filter-repo](https://github.com/newren/git-filter-repo)。
- 关联：[原提案 PR #466](https://github.com/bigbird-online/PULSE/pull/466)；[纠错权 PR #377](https://github.com/bigbird-online/CollaborateWithAI/pull/377)；[现有 shared-root authority migration 仅覆盖其声明的工作根/分支场景，不等于已具备本次跨仓 kernel 迁移](https://github.com/bigbird-online/CollaborateWithAI/blob/8773da536da77b1fb2a99ed2c28001a6d2cfccf3/Bigbird/Lab/BigbirdEnvironment/agent-harness/specs/shared-root-authority-migration.md)。