# SPEC：PULSE 全局 — 平台闸门拓扑（pre-commit 链 · 租户隔离 · 一致性闸）

> version: `0.2.1`
> status: `reverse_engineered_from_code`
> authority: 平台各道机器闸的**现行接线与失败方向**（谁在守、守什么、坏了往哪边倒）
> owner: Pulse
> modified: `2026-11-10`
> 版本史：只看 git log

**为什么单立一文**：单个闸的判据归各自脚本头注释与模块 spec；「闸与闸怎么接线、
失败方向是否一致、哪些闸其实没接上」是拓扑事实，散在各处没人持有——本文持有它。

## 1. pre-commit 链（PULSE 仓）

- [x] 闸名单与顺序由 install-hooks.sh 生成器唯一定义：spec-drift-guard →
  relay-spec-drift-guard → uppercase-file-line-guard →（本文落地后）
  global-spec-drift-guard；失败语义是**跑完全部再退**不短路
  `@anchor symbol HOOK_DIR`
  `@anchor manual 生成物与生成器同源 :: 比对 install-hooks.sh 的 heredoc 与 .git/hooks/pre-commit 应逐字节同源；hook 是本地文件不随 git 传播，换机后须重跑 install-hooks.sh`
- [x] guard 不可执行时**静默跳过**（`[ -x ] || continue`，无告警）——链上唯一的
  静默失败点，登记为债
  `@anchor manual 静默跳过复现 :: chmod -x 任一 guard 后 git commit 一个触发该闸的改动，观察无任何输出即绕过；恢复 chmod +x`
- [x] `.pre-commit-config.yaml` 是**死配置**：框架本机未装，名单与实装两处不符
  （多 atomic-commit-guard、少 relay-spec-drift-guard）
  `@anchor manual 死配置核对 :: install-hooks.sh:4-11 头注释自述框架未装；diff <(grep -o '[a-z-]*guard' .pre-commit-config.yaml) 与实装名单`
- [x] `atomic-commit-guard.sh` 在盘上但**零调用点**（自述 commit-msg 阶段，
  install-hooks 只写 pre-commit）；阈值 500 行、`[bulk]` 与单文件豁免
  `@anchor const THRESHOLD = 500`
  `@anchor manual 零接线核对 :: grep -rn atomic-commit-guard .git/hooks/ Scripts/hooks/install-hooks.sh 应仅见脚本自身与死配置引用`

## 2. 三道 spec 闸 + 本文的第四道（判据对照）

- [x] 触发面：Stats 闸 `^(Stats/|vultr-pulse-space/specs/stats-)`
  （diff-filter ACM）；Relay 闸 `^Relay/(src/|specs/relay-|tests/)`
  （ACMR，比 Stats 多 R）；两者 `--force` 全量。harness 闸在 Coevolution 主仓
  pre-commit（跨仓，manual）
  `@anchor symbol run_scan`
  `@anchor const OUT_DIR :: "relay-spec-drift"`
  `@anchor manual harness 闸接线 :: 主仓 .git/hooks/pre-commit 指向 agent-harness/scripts/spec-drift-guard.sh；触发面 ^Bigbird/Lab/BigbirdEnvironment/agent-harness/`
- [x] 失败方向**三闸不一致**（拓扑事实，非缺陷本身，但读闸输出时必须知道）：
  uppercase 闸全程 fail-closed；Stats 闸工具缺失 fail-open、工具执行失败
  fail-closed；Relay 闸除超基线外**全程 fail-open**（主仓 harness 闸同 Relay 型，
  设计理由=不拿单方工具债挡全组织提交）
  `@anchor const LIMIT = 100`
  `@anchor manual 方向核对 :: 逐脚本 grep exit 核对：uppercase :22-28 恒 exit 1；Stats :37-40 exit 0 vs :59-63 exit 1；Relay :30-56 四处 ⚠ exit 0`
- [x] 两份基线 schema **不统一**：Stats 闸读 `allowed_drift`，Relay 闸读
  `drift_count`——第三道闸（harness）用 `drift_count`，本文的第四道跟 Relay 型；
  统一 schema 登记为债
  `@anchor manual 基线键名核对 :: grep -n "allowed_drift\|drift_count" Scripts/hooks/*.sh 与两份 .spec-*-baseline.json`
- [x] 零文件扫描防假绿断言（spec 数>0）四闸口径已统一——Stats 闸原缺此层（它先于该
  教训落地），2026-08-10 ken 独立验收跑阴性对照实测到假绿后补齐，债已还
  （wi_20260810_401）
  `@anchor manual 断言核对 :: grep -n 'SPECS" -eq 0' Scripts/hooks/*.sh——relay/global/spec-drift-guard 三者均命中`
- [x] 本文与另两份全局 spec 由第四道闸把守：触发面 `specs/pulse-global-*.md` +
  `Scripts/`，globs 覆盖 Relay TS / Stats py / Scripts sh / vultr-pulse-space
  scripts 与 systemd unit；判据同 Relay 型（fail-open + drift>基线拒绝 +
  spec 数>0 断言）
  `@anchor const OUT_DIR :: "global-spec-drift"`

## 3. 租户隔离（Scripts/ 三件套）

- [x] sudoers 规则由安装脚本写入：`pulse ALL=(tenant-*) NOPASSWD: ALL`，
  chmod 440，visudo 校验失败即回滚删除
  `@anchor const SUDOERS_CONTENT :: "pulse ALL=(tenant-*) NOPASSWD: ALL"`
- [x] DAC 加固幂等脚本：要求 root+Linux（macOS 直接退出）；目录 750 / 可执行 750 /
  普通文件 640；平台根与 Tenants/ 为 711 traverse-only；`Tenants` 与 `.git`
  跳过不加固；Relay 运行时例外三级目录 711 + pty bridge 单文件 644；
  **硬红线：永不对 .claude.json 施加 chattr +i**（只清陈旧 immutable 标志）
  根目录解析固定为显式 `PULSE_ROOT`（提供时必须是有效仓根，否则 fail closed）→ 脚本所在仓根；
  已退役 `/opt/PULSE` / `/opt/pulse` 不得作为 fallback。
  `@anchor symbol require_linux`
  `@anchor symbol resolve_pulse_root`
  `@anchor manual live-root-no-retired-fallback :: grep Scripts/lib/pulse-root.sh 与两条 isolation 脚本，确认默认根由脚本位置推导，且不存在 /opt/PULSE 或 /opt/pulse fallback`
  `@anchor manual chattr 红线 :: grep -n chattr Scripts/harden-tenant-platform-isolation.sh 应只见清理分支，无施加分支`
- [x] 集成探针以租户身份跑 forbidden/allowed 断言；退出码 0 全过 / 1 有失败 /
  **2 跳过**（非 Linux 或探针用户缺失）——skip 与 pass 用不同退出码区分（这是
  「SKIP 冒充 PASS」教训的正面样板）；目标文件不存在记 skip 不记 pass
  `@anchor const TENANT_ID :: "member-0001"`
  `@anchor const PEER_TENANT_ID :: "member-0002"`
- [x] agent-harness 的路径守卫在 PULSE 侧**不接线**：无 pre-commit/settings hook 引用
  `@anchor manual path_guard 接线核对 :: grep -rn path_guard Scripts/ .claude/ 排除 memo 后应无命中；执行侧在 agent-harness 仓`

## 4. Stats 侧一致性闸（test suite 层，不在 pre-commit）

- [x] spec↔能力表一致性闸：跑**生产命令本身**（非 fixture，timeout 300），
  比对 spec 能力表与实跑 implementation_status；含自证闸（表行 ≥15、状态值须在
  四值枚举内）；**fail-open 四路**（超时/非零退出/非 JSON 一律 skipTest）
  `@anchor test Stats/exporter/tests/test_spec_capability_drift.py :: test_exporter_status_matches_spec`
  `@anchor test Stats/exporter/tests/test_spec_capability_drift.py :: test_spec_table_is_parseable`
- [x] 跨仓枚举守恒闸（wi_20260810_631）：写侧为已退役旧 Harness 的归档枚举
  （wi 十态/ack 六态，冻结于测试内），判**读侧无幻影值**
  + **写侧每值必被显式分类或登记豁免**；含自证闸（wi ≥5、ack ≥4，低于下限判探针
  失效而非契约成立）
  `@anchor test Stats/exporter/tests/test_cross_repo_enum_contract.py :: test_every_upstream_value_is_classified`
  `@anchor test Stats/exporter/tests/test_cross_repo_enum_contract.py :: test_read_side_values_all_exist_upstream`
  `@anchor test Stats/exporter/tests/test_cross_repo_enum_contract.py :: test_probe_itself_is_alive`
- [x] 外部输入契约闸（wi_20260810_631）：跨语言比对——TS 写侧 `resolveObsDir`
  与 Stats 生产读侧使用同一受管 `observabilityDir` 映射键；旧硬编码 `OBS_DIR`
  仅作历史 replay。写侧 env 名仍须在读侧被点名（读侧刻意不读别的进程的 env）；
  另守「凡 live 输入带版本字段读侧必有显式判定」。
  `@anchor test Stats/exporter/tests/test_external_input_contract.py :: test_write_side_and_stats_share_managed_observability_key`
  `@anchor test Stats/exporter/tests/test_external_input_contract.py :: test_read_side_documents_the_env_it_ignores`
  `@anchor test Stats/exporter/tests/test_external_input_contract.py :: test_versioned_inputs_are_gated_in_code`
- [x] 真实快照守恒闸 verify-live-snapshot：手动 CLI、无 hook/suite 接线；
  SKIP 与 PASS 严格分离，「All checks passed」只在零 skip 时打印；**退出码
  fail-open**（有 skip 无 fail 仍 exit 0，警告只在文案）——登记为债
  `@anchor symbol LEGAL_QUALITY`
  `@anchor manual 退出码核对 :: 见 verify-live-snapshot.py:323-335——failure exit 1、skip-only exit 0 带 INCOMPLETE 文案；接入自动化前须先把 skip-only 改非零或调用方 grep INCOMPLETE`

## 5. bigbird-pulse 主机工作负载闸

- [x] `bigbird-pulse-cwa` 的 job-start hook 在任何组织任务首个 workflow step 之前取得
  `/run/lock/pulse-bigbird/heavy.lock`，job-completed hook 释放；active lease 由独立 holder
  进程持有并记录在 checkout 外。hook 缺失、锁忙、lease identity 不可核或可用内存低于
  1024 MiB 时 fail closed，不能让任务继续挤压主机
  `@anchor test Scripts/tests/test_bigbird_workload_admission.py :: test_runner_lease_holds_until_completion_hook_releases_it`
- [x] bigbird Relay 的远端 `npm ci/build`、release admission/activation 与 rollback 使用同一
  host-local lock。固定运行于某台主机的重任务继续使用对应 host concurrency；但
  **pool-routed** 的 trusted Linux verification/default-main gate 只声明
  `pulse-linux-trusted` + `pulse-control-plane`，不得用 `bigbird-sg` label 或单一 host
  concurrency 把整个 runner group 重新串成单机队列。SG 与 CWA 的 job-start/job-complete
  hook 在各自主机上复核并持有 workload lease，因此同主机重任务仍 fail-closed 串行，
  不同主机可同时消费可信池。仅在隔离检出中执行测试的 Linux UAT 继续使用
  `pulse-linux-uat-v1`；host lock 仍覆盖 Oikos/Harness 等其它仓库的同主机任务
  `@anchor test Scripts/tests/test_development_control_plane.py :: test_isolated_uat_does_not_reserve_mac_deployment_queue`
  `@anchor test Scripts/tests/test_development_control_plane.py :: test_uat_rechecks_physical_host_lease_on_both_linux_runners`
  `@anchor test Scripts/tests/test_development_control_plane.py :: test_trusted_linux_jobs_serialize_by_actual_resource`
  `@anchor test Scripts/tests/test_development_control_plane.py :: test_sg_serialization_cannot_be_used_by_unpinned_pool`
  `@anchor test Scripts/tests/test_development_control_plane.py :: test_sg_lease_still_required_after_queue_split`
- [x] runner cgroup 将 CPU 限制为 150%，MemoryMax 固定为 2048 MiB，
  swap 最多 1024 MiB，并降低 I/O 调度优先级。CWA 的 Listener、Worker 与取消收尾共用
  cgroup，MemoryHigh=infinity 避免软限流同时阻断控制通信；OOMPolicy=kill 令 OOM 时
  整组退出并由原 Restart=always 恢复。SG 保留原 1536 MiB MemoryHigh 与原 OOM 策略。
  即使 workflow 绕过协作锁，也应让 job 失败而不是拖死 SSH、Tailscale、journald
  与 Relay。已退休的 `MemberZ/PULSE`
  repo runner 必须 disabled+inactive，避免形成第二个未受控执行槽
  `@anchor test Scripts/tests/test_install_bigbird_workload_guard.py :: test_apply_is_idempotent_and_verify_detects_drift`
- 原 holder 已退出时，completion 先核对匹配其 PID、主机、账户、租约路径、锁路径和存续时间的释放回执。
  后继任务已持锁不推翻原 holder 的释放事实；无有效回执时仍需物理锁证明，且仅删除 PID/token 均匹配的原租约。
  外来或损坏回执、仍存活的原 holder 不得据此放行，后继租约必须保留。
  `@anchor test Scripts/tests/test_bigbird_lease_release.py :: test_acknowledged_original_can_release_while_successor_holds_lock`
  `@anchor test Scripts/tests/test_bigbird_lease_release.py :: test_unconfirmed_original_does_not_delete_busy_successor`
- [x] 安装只允许从 exact-main 候选的 installer 把 admission script 与 hook 原子复制到
  `/usr/local/libexec/pulse`；生产 deploy 每次 build 前验证 source/stable digest、hook、
  tmpfiles、drop-in 与 legacy runner disposition。每次 acquisition/release/command 均写
  `pulse_bigbird_workload_admission/v1` checkout 外回执
  `@anchor test Scripts/tests/test_bigbird_workload_admission.py :: test_second_command_is_rejected_while_lock_is_held`

## 6. 自动 CI 的短检查与全量测试分流

自动 Harness 验证使用 `pulse-linux-trusted` + `pulse-control-plane`，不设置 job-level 全池或单主机
并发组；空闲可信节点可同时验证不同候选。同一物理主机的串行、可用内存检查和资源上限由 §5 的
host-local workload lease、runner hook 与 cgroup 负责，测试步骤继续复核实际主机持有的 lease。

PR 根据已声明的差异范围选择 smoke 或全套；含源码变更的 main push 保留全套。仅修改 `.harness/**`
生产事实的提交由事实写入边界验证，不为每个接续批次重新运行源码门；源码、部署和 workflow 混合变更仍须全套。
`@anchor test Scripts/tests/test_development_control_plane.py :: test_main_gate_cannot_skip_source_or_deployment_changes`
测试内容、隔离目录、标准工具链和
部署并发组不变。短契约检查和 default-main gate 继续使用同一可信池；实际等待取决于节点是否空闲，
不以全池闲置一个节点来保证短任务容量，也不承诺短任务零等待。

workflow-level supersession 只淘汰同一 PR 的旧验证，主线以 run ID 分组，不丢弃不同主线提交。
GitHub 在有空闲匹配节点时分配任务；workflow 不占用节点后用 shell sleep 等待另一台主机释放。
撤销本节调度变更仅恢复 workflow 和校验契约，不触碰主机保护、业务 WI 或生产部署。
`@anchor test Scripts/tests/test_development_control_plane.py :: test_harness_pool_can_use_idle_hosts_without_global_queue`
`@anchor test Scripts/tests/test_development_control_plane.py :: test_harness_rejects_pool_wide_or_host_pinned_queues`
`@anchor test Scripts/tests/test_development_control_plane.py :: test_harness_host_lease_required_without_pool_queue`
`@anchor test Scripts/tests/test_development_control_plane.py :: test_main_runs_cannot_share_pr_supersession_group`
`@anchor test Scripts/tests/test_development_control_plane.py :: test_short_checks_reject_pool_wide_queue`

## 已知两义与债（登记，处置另立 wi）

1. pre-commit 链 guard 不可执行时静默跳过（§1）——与「跳过必须说出口」纪律相悖。
2. `.pre-commit-config.yaml` 死配置 + atomic-commit-guard 零接线（§1）——
   删或接，别留第三种状态。
3. 基线 schema 两套键名（§2）——统一到一个键。
4. ~~Stats 闸缺 spec 数>0 断言（§2）~~ —— 已还（wi_20260810_401）：四闸口径统一，
   零 spec 时播报 ⚠ 并 fail-open，配对反转 0→1→0 已复现。
5. verify-live-snapshot skip-only 退出码 0（§4）。
6. 三闸失败方向不一致（§2）——方向本身各有理由，但**理由没写在脚本头**的
   （uppercase）补注释。

## 机检口径

锚点由 `Scripts/hooks/global-spec-drift-guard.sh` 把守。本文大量断言对象是
shell 脚本与 hook 接线——凡「零接线 / 死配置 / 静默」类否定性事实用 manual
（五类存在性锚点表达不了缺席，absent 只钉全局符号缺席）。

- 自托管 CI 的每个任务须独立检查自己的工作负载租约；其他任务的 assert-held 不能替代本任务的检查。
  @anchor test Scripts/tests/test_development_control_plane.py :: test_each_linux_job_must_verify_its_own_workload_lease

- Harness 与 Linux 集成验证使用已登记的共享可信能力池，采用当前工作流声明的完整验证和时限；
  工作项身份不形成专用 runner 或额外排队规则。Muse canary 仍经显式输入独立选择。
  @anchor test Scripts/tests/test_development_control_plane.py :: test_harness_pool_can_use_idle_hosts_without_global_queue
  @anchor test Scripts/tests/test_development_control_plane.py :: test_trusted_linux_capability_verification_is_explicit_only
