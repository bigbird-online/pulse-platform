# 四端同步：普通用户与 Agent 使用指南

> 2026-08-29 resilience correction：本指南描述 Git/endpoint effect 的操作与安全边界，不再把它们
> 当作本地 WI 可用性的同步门。以 `harness-scl-resilience-requirements.md` 为准：GitHub、权限、网络或
> endpoint 故障使 publication/replication 保持 pending 或 conflicting，但不阻断 conversation、local read、
> checkpoint 和 ordinary local work。只有 global claim、跨 Agent authority 和 terminal settlement 等依赖
> 全局证明的能力继续等待 effect readback。

工作根出现脏改时，可以先用 `harness sync-workspace audit` 只读诊断。日常自动入口是
`harness sync-workspace run --authorization-file ...`：同一次调用先处理完整的安全脏集，
再执行已登记的 GitHub-authoritative endpoint convergence。输出仍分别记录 settlement
和 convergence；只有 `complete=true` 才表示这两个同步阶段都通过。

PR 合并并更新 GitHub 权威分支后的日常入口仍是 `harness converge after-merge`。
调用者只表达当前 WorkItem、Agent 和本次发布涉及的仓库；Harness 固定选择隔离的
service endpoints，并解析 immutable GitHub target。调用者不需要填写 manifest
路径、topology id、SSH 配置、endpoint class 或 raw SHA。

## 最常用的调用

先对真实 human root 做只读完整清点：

```bash
harness sync-workspace audit \
  --repository-root /Users/owner/Coevolution \
  --repository-slug bigbird-online/CollaborateWithAI \
  --human-owner-id owner \
  --require-clean
```

`--require-clean` 专供 cron/launchd：clean 时退出 0，非 clean 时仍输出完整 JSON 并退出
30。它不启动 Agent、不 commit。ordinary human dirt 只有在 audit 明确
`apply_safe=true` 且有 bounded human authorization 后，才执行：

```bash
harness sync-workspace apply-safe \
  --repository-root /Users/owner/Coevolution \
  --repository-slug bigbird-online/CollaborateWithAI \
  --human-owner-id owner \
  --work-item-id wi_20260822_479 \
  --agent pulse \
  --surface-id surface_v1_local_macos \
  --repo-id collaboratewithai \
  --authorize-human \
  --convergence-repository collaboratewithai
```

这是一条完整的一次性四端同步命令：先保存并发布完整安全脏集，再收敛登记的 service
endpoints，只有输出 `complete=true` 才算完成。执行 Agent 不必是 WI owner 或 topology
platform owner；`--agent` 只记录实际代人类执行者。普通 `converge sync` 仍遵守 active-WI
owner 约束，不能替代此 workspace-sync trigger。

human/mixed settlement 的 preservation commit 与最终 GitHub integration commit 都会显式写入：

- `Workspace-Provenance: human|mixed`
- `Provenance-Owner: <human-owner-id>`
- `Executed-By-Agent: <agent-id>`
- `Work-Item: <wi-id>`

因此任何 Agent 都可以在同一安全契约下代为执行，但不能隐藏或冒充脏改来源。

已登记 runtime artifact 只有在 exact producer receipt 与当前 postimage 完全匹配时才
无需 human authorization。人类文件与 exact producer receipt 覆盖的 runtime artifact
可以作为一个 `mixed` provenance group 原子提交，并要求 human authorization。未归因
`.harness` 输出、credential、变化的 gitlink、nested Git root 或不明确 provenance 会阻断
整组，不会提交安全子集。

安装 standing authorization 后，日常和 launchd 都只调用这一条命令：

```bash
harness sync-workspace run \
  --authorization-file /Users/owner/.local/state/agent-harness/workspace-sync/authorization.yaml
```

每个独立 Git 根使用自己的授权。PULSE 根使用：

```bash
harness sync-workspace run \
  --authorization-file /Users/owner/.local/state/agent-harness/workspace-sync/pulse-authorization.yaml
```

授权中的 `authorized_system_paths` 是受限的 PULSE/Agent provenance 声明，不会把
整个仓库或相似前缀重新标成人类文件。已由仓库 policy ignore 的 runtime-private 内容
保留在原位，不参与提交，也不影响最终 Git clean 读回。

LaunchAgent 的 executable 必须直接绑定已经验证、只读且 revision-addressed 的 release
路径，不能经过会被其他 WI 切换的共享 `agent-harness` stable symlink。ownermac 的 paired
Harness adoption 在新 release 完成读回验证后，先完整验证两份配置，再逐份 durable rewrite 两份
workspace-sync plist 并重新 bootstrap；reclaimer 同时把已安装 plist 中的 exact release 视为 persistent reference，因此
重绑完成前不会退休仍被 launchd 使用的 release。仓库中的 plist 只是 bootstrap snapshot，
不能替代 paired adoption 的 live readback。这不会改变或撤销外部 standing authorization。

授权文件必须由当前 OS 用户持有、权限为 `0600` 或更严格，并位于目标 Git 根之外。
删除该文件会让下一次执行在任何 Git mutation 前失败，因此也是撤销自动提交授权的开关。

PR merge 后更新 Oikos 的隔离服务 checkout：

```bash
harness converge after-merge wi_20260822_479 \
  --agent pulse \
  --repository Oikos
```

同一 merge/publish 边界涉及多个独立 Git 根时，重复 `--repository`：

```bash
harness converge after-merge wi_20260822_479 \
  --agent pulse \
  --repository PULSE \
  --repository Oikos \
  --repository bigbird-online/CollaborateWithAI
```

需要在 post-merge gate 之外显式选择 endpoint class 时，使用通用 typed 请求。
例如更新真实的人类工作根：

```bash
harness converge sync wi_20260822_479 \
  --agent pulse \
  --repository Oikos \
  --endpoint-class human_work_root
```

如果需要把工单号、变更单号等稳定业务标识写入请求身份，可以补充
`--business-change-ref <token>`。它只用于业务关联与幂等请求识别，**不会**选择或
覆盖 Git branch/ref。Harness 始终使用平台 topology 中登记的 branch，并在任何
endpoint apply 前把它解析为完整 immutable SHA；普通调用者不能通过该参数改变
目标，也不提供 raw SHA。

## 何时自动、何时不自动

- PR merge/publish orchestration 必须立即调用 `converge after-merge`；这是代码
  service endpoints 的 deployment gate，不依赖 timer。
- 裸 `/wi`、checkpoint、rollover、普通 Session 结束只做 authority/freshness
  检查，不隐式部署代码。
- `human_work_root` 永远不由 post-merge gate 静默更新。
- WI 本身的创建、状态转换、criteria、handoff、Outcome 与 execution ledger 是另一条
  控制面 operation/effect 通道。accepted local operation 先进入 durable kernel，再由 valid fence
  向 canonical authority 与各 surface 发布；读取返回 local/global revision、`as_of`/coverage，不能把本地旧 checkout
  当成全局事实，也不能要求 Agent 自行猜测并 pull。

## WI 增删改后的控制面发布

普通用户或普通 Agent 仍然只调用已有的 WI typed mutation（create/update/state、
criteria、handoff、Outcome 或 execution 命令），不手工操作 Git。mutation 被 canonical
authority 接受并发布后，平台 mutation publisher 调用：

```bash
harness converge after-wi-mutation wi_20260822_479 --agent pulse
```

这个命令没有 repository、endpoint class、manifest、topology、SSH 或 SHA 参数。Harness
只选择 exact active registration 指向、登记为 `control_plane_role: work_item_authority` 的 platform topology，只更新
隔离 service endpoints，并产生 `trigger=wi_mutation` 的 aggregate receipt。回执里的
immutable `target_sha` 表示控制面副本的 `as_of` 边界，不代表代码 release、安装、runtime、
可见 UAT 或 Outcome 已完成，也不代表任何 human work root 已更新。

平台必须把这一步作为 durable effect 接在每个 accepted local operation 后；mutation API 可先返回
`local_committed`，并明确 publication/replication 的 pending/partial/conflicting 状态及 automatic retry。
只有 fenced publication 与 replication readback 完成才算完成跨 surface 发布。读取 WI 时应同时返回
local/global revision、canonical `as_of` 与 surface coverage；
读路径不执行整仓 Git apply，也不能把旧 checkout 中“找不到”解释为 WI 不存在。

Broker 的 typed `harness_projection` producer 会在 Git 根外的隔离 worktree 中执行固定
mutation argv，生成带 `Broker-Transaction` trailer 的 commit，对 canonical branch 做
fast-forward/CAS publish，再调用同一条 WI replication barrier。原 human/shared checkout
不会被拿来承载 projection postimage。若 Git 已发布但某个 surface 暂不可达，transaction
保持 `pushed` 与 `replication_status=pending`，reconciler 自动续作。以下命令仅用于诊断或立即唤醒，
不再要求用户复制执行：

```bash
harness broker resume-projection broker_tx_v1_<id>
```

它复用已经发布的 commit，不会再生成第二个 mutation commit。

在真实 `bigbird-online/CollaborateWithAI` Git 边界中，`harness create` 已接入这个
publisher：human checkout 只作为调用面；命令先使用版本化的
`topologies/work-item-authority/ownermac.yaml` 登记事实定位唯一 clean local service
endpoint，再由 Broker 分配固定 WI id、在隔离 worktree 生成 projection、CAS 发布并跨
surface replication。replication barrier 完整通过后输出 `globally_committed ...
canonical_as_of=<sha> replication=complete`。topology 缺失、service checkout 脏/旧且不能安全收敛、
GitHub CAS 冲突或 endpoint 部分失败时，外部 effect fail closed 并留在 reconciler；命令可返回
`local_committed` 或 `execute_local_isolated`，但绝不把仅本机事实冒充 canonical。其他 typed mutation
仍须逐一接入同一 operation/effect protocol，不能由 create 的接入推断为已经自动发布。

Mac publisher 的外部 topology 首次安装必须从当前 default checkout 执行一次：

```bash
harness --root /Users/owner/.local/share/pulse/service-repositories/collaboratewithai/harness/ownermac/.harness \
  converge enroll topologies/work-item-authority/ownermac.yaml --agent pulse
```

manifest 是 Git 内版本化部署输入，真正生效的 registry 位于 Git 根外的 site-canonical
state root。`enroll` 会校验 GitHub identity、三个 endpoint remote/path 与 platform owner；
现有同名 topology 与 manifest 不同则报 `topology_conflict`，不会静默覆盖现场事实。
若唯一变化是平台 topology 的 authority branch，platform owner 使用版本化 manifest
执行 `harness converge migrate-authority-branch <manifest> --agent pulse`。该命令要求 repo、
endpoint、owner 与 control-plane facts 全部不变。它先固定新 authority SHA，再逐端验证
旧/新 branch、remote、clean state 与 ancestor 关系；ahead、diverged 或 dirty 一律保留并
fail-closed。干净 endpoint 切换到新 branch 并完成固定 SHA readback 后，才按 digest 归档
旧 manifest 并提交新 topology。中途失败时 topology 保持原值；重试可同时接受仍在旧 branch
和已经落在新 branch 的 endpoint，续作剩余节点。它不能作为任意 topology 覆盖入口。

若版本化 topology 的唯一变化是 service endpoint 的 `checkout_path`（例如退休旧
worktree，改用同仓库的 service repository），platform owner 执行：

```bash
harness converge migrate-endpoint-paths <manifest> --agent pulse
```

该命令禁止同时改变 repo、branch、remote、endpoint identity、required set 或 owner；
它不会修改或清理退休 checkout。命令先固定 GitHub authority SHA，再对每个 replacement
checkout 做 canonical path、clean、branch、remote 与 fixed-SHA 校验，并只允许 ff-only
收敛。全部读回后才按 digest 归档旧 manifest 并原子采用新 topology。中途失败保持旧
topology，重放同一命令续作；不得直接编辑 Git 外 registry 或再登记第二个 topology。

普通 read-side consumer 使用 freshness-bearing read：

```bash
harness converge read-work-item wi_20260822_479
```

结果同时给出 `canonical_as_of`、required/converged surface、`coverage` 与
`record_status`。当所有 service endpoint 仍旧时，`record_status=unknown`，而不是把
本地 checkout 的缺失冒充为 canonical `missing`；read 只做固定 SHA probe/read，不执行
checkout apply。

## 两类 endpoint 不可混称

- `service_endpoint` 是给服务、自动化或验证使用的隔离 checkout。它收敛后，
  输出会明确说明 human work roots 没有改变。
- `human_work_root` 是人实际工作的 checkout。它可能有未提交或在飞内容，必须
  单独选择并接受更严格的脏目录保护。

一次请求只能选择一种 endpoint class。需要两种时分别发起请求、分别查看回执；
不能用 service endpoint 的成功回执声称 human work root 已更新。

## 怎样读结果

aggregate receipt 会逐仓引用各自的 fixed-SHA receipt，并把四个阶段分开：

| 阶段 | 含义 | 不代表什么 |
|---|---|---|
| GitHub publication | branch 已被解析为 immutable target | endpoint 已更新 |
| Broker preservation | 脏内容无需保全，或已有 Broker 精确保全证据 | endpoint 已更新 |
| endpoint convergence | 选中的 endpoint class 已在固定 SHA 读回 | human root 或另一 endpoint class 已更新；业务完成 |
| Outcome | 独立 Outcome 验收状态 | 不能由同步命令自行判定 |

`trigger=post_merge` 只说明该 aggregate receipt 属于 PR merge 后的 endpoint
deployment gate。它仍不证明 wheel/release 已构建、安装已更新、runtime 已采用、
可见 UAT 已通过或 Outcome 已验收。

多仓库请求只有在每个 Git 根都生成自己的 fixed-SHA receipt 后，才会形成 aggregate
receipt。一个 topology 永远只描述一个 Git 根。

## 失败时怎么做

- `git_index_locked`：仅在已授权、干净且可快进的共享根更新路径中检查 `index.lock`。
  普通文件、当前用户所有、单链接、空内容、至少五分钟未修改，并且两次 `lsof` 检查均
  无持有者及警告、进程清点未发现 Git 写入进程、文件身份未变化时，才将原锁移至 Git 外 inventory state 的
  `recovered-index-locks` 目录后继续更新。非空、新建、被使用、符号链接或无法确认的锁
  保持原位；工具缺失或权限警告不是“无人使用”。不以空文件或操作系统名称判定陈旧锁。
  这不是任意 Git 锁清理器，也不清理其他 Agent worktree。
- Git 命令失败保留命令名称、退出码和有界错误文本；传输 URL 脱敏。
  超时独立报告为 `git_probe_timeout`，不得全部压成无原因的检查失败。
- `topology_not_found`：确认仓库名或 `owner/repo` 拼写；若仍失败，请平台 owner
  登记该仓库与 endpoint class，然后原样重试命令。
- `topology_conflict`：不要猜 topology id；请平台 owner 修复重复登记，
  然后原样重试。
- `endpoint_dirty`：保留当前内容，不要 stash、reset、clean、
  delete、rebase 或 force-push。先通过 Broker 精确保全该工作根，再原样重试
  `harness converge sync`。
- `authority_unreachable`：确认登记的 GitHub branch 已发布且网络可达，然后原样重试；
  不要改填 raw SHA。
- `lock_conflict`：相同请求正在执行；等待它写出回执，再原样重放命令。不要并行创建
  第二套 topology 或 state root。
- aggregate `status=partial`：这不是 error code。不要回滚已收敛 endpoint；排除每仓
  回执中列出的失败 endpoint 后原样重试。
- `work_item_authority_not_found`：WI mutation 已写入 canonical authority，但跨 surface
  发布尚未完成；请平台 owner 登记唯一 WI authority，然后重试同一条
  `after-wi-mutation` 命令。不要让 Agent 自行 pull 或重做 mutation。
- `work_item_authority_ambiguous`：保留现状，请平台 owner 删除角色冲突或取消重复角色，
  再重试同一命令；不要猜 topology id。

失败输出中的 `next_action` 是给普通调用者的下一步。manifest、topology 与网络细节
由平台 owner 维护，不应转嫁给日常调用者。

## 平台 owner 的一次性职责

平台 owner 使用 `github_authoritative_topology/v2` 登记长期复用 topology。登记对象
必须指定单一 repository、单一 endpoint class、GitHub authority 和每个 endpoint
的准确身份；它不绑定具体 WI owner。普通 WI 只提交 typed convergence request。

每个登记 worktree 还要安装 worktree-local branch guard；命令会把 hooks 放在 Git 根外，
通过 `extensions.worktreeConfig` 只为该 worktree 设置 `core.hooksPath`，并在 authority
guard 通过后继续执行原有 hook：

```bash
harness broker install-guards \
  --repository-root /absolute/registered/worktree \
  --agent pulse \
  --branch-ref refs/heads/feature/wi479-github-authoritative-sync
```

`pre-commit` 与 `pre-push` 使用同一条 branch/transaction 判定；`--no-verify` 只能跳过
本地 hook，所以该边界仍如实标记为 `protocol_only`，不能声称是 OS/credential 级硬隔离。

`converge enroll` 和 `converge closeout` 仍是平台/诊断层 primitive，不是推荐的日常
入口。skill 如需提供提示，也只能薄封装这个已测试的 typed Harness contract，不能
重新实现 topology、SHA 或 SSH 解析。
