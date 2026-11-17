# SPEC：harness 主链 — wi/ho 建单 → 派发 → 接活 → 收口

`WorkItem.status=blocked` 是业务生命周期状态，不是产品全局 dependency health bit。能力与依赖降级
必须遵守 `harness-scl-resilience-requirements.md`；Hook、GitHub、replication、installation 或 endpoint
故障不能仅为表达 external effect 不可用而把 WI 置为 `blocked`。

> version: `0.1.5`
> status: `reverse_engineered_from_code`
> authority: harness CLI 与 dispatcher 的**现行实现契约**（由代码反推，非设计前瞻）
> owner: Pulse（wi_20260804_070 · KR2b）
> modified: `2026-08-29`
> code_root: `Bigbird/Lab/BigbirdEnvironment/agent-harness/harness/`
> test_root: `Bigbird/Lab/BigbirdEnvironment/agent-harness/tests/`
> 版本史：只看 git log，不在文件头复制 changelog

**取证基础**：本文所有声明对着 2026-08-09 的代码普查核过（符号、常量值、测试名均带
file:line 落点），并经 spec-anchor 机器验证全绿后才提交。锚点按维护期形态直接写在
契约行上；散文段落提到外部库或工具自身的符号时不加反引号（防推断误报）。

**已知读数边界**：spec-anchor 按名字匹配、不按定义点匹配，同名符号（如多个枚举都有
PENDING 成员）会被当成同一个，读数方向偏乐观。

## 0. 管什么，不管什么

**管**：wi 与 handoff 的状态机（合法状态集与迁移表）、责任转移语义、三道闸
（执行权排他 / owner 一致性 / caller identity）、派发链的去重与跳过语义、
以及上述各环节的关键常数。

**不管**（另行覆盖或暂无 spec，不在本文断言）：escalation 通道、compliance 引擎、
heartbeat 运行时、site-policy 的站点级权限模型、Relay 侧 watcher。存量
features/ 目录 42 份 spec-kit 文档是历史设计稿，分类处置见 KR1，不作为现行契约引用。

**也不管——dispatcher 拉起执行层的其余合同真空（非有意豁免）**：dispatcher.py
的 prompt 组装与多数运行时故障判读——_build_prompt / _build_night_patrol_prompt 及
_night_patrol_ 前缀族、_detect_tool_denied / _is_rate_limited / _detect_wrap_missing——
目前零 spec 覆盖；_detect_runtime_blocked 已由 harness-runtime.md 接管。本文 §6 只管
派发链的跳过/去重/熔断判定，不断言其余拉起后执行层行为；该层立锚前以代码为准，
债项登记见 §7 末条。

## 1. 入口命令层

主链的唯一合法状态写入口是 harness CLI；直接编辑 YAML 台账绕过全部闸（纪律层禁止）。

建单与派发三段式（draft→ready→in_progress + 生成 handoff）由内部 helper 承担：

- [x] 建单 helper 存在且负责 assign 三段式
  `@anchor symbol _assign_work_item`
  `@anchor symbol _build_handoff_payload`
  `@anchor symbol generate_unique_id`
- [x] handoff 建新腿时有 prior-leg 闸：同一 wi 上一腿仍 accepted 时硬拒，
  须显式选择关旧腿或保留
  `@anchor test agent-harness/tests/test_cli.py :: test_new_leg_refused_while_prior_still_accepted`
  `@anchor test agent-harness/tests/test_cli.py :: test_close_prior_archives_the_named_leg`
  `@anchor test agent-harness/tests/test_cli.py :: test_keep_prior_leaves_the_leg_open`
- [x] 收口辅助：wi 到终态时自动收尾其挂着的 handoff
  `@anchor symbol _auto_finalize_handoffs_on_terminal_wi`
- [x] complete 前必须校验 outcome 回执与验收指纹
  `@anchor symbol _validate_outcome_receipt`
  `@anchor symbol _acceptance_fingerprint`
- [x] acceptance criteria 若在 Outcome UAT 冻结前需要修订，只能通过受审计的全量 replace：
  必须提供当前 expected fingerprint 与 reason；stale writer、空/重复/no-op replacement、
  `awaiting_verification` 冻结候选及终态全部 fail closed。成功写入 WI 后追加包含 before/after
  criteria 与 before/after fingerprint 的 `work_item.acceptance_criteria_amended` event。
  `@anchor symbol amend_acceptance_criteria`
  `@anchor symbol amend_criteria_command`
  活动 WI 的定义修订使用同一公开 amend-criteria 入口；无需重建执行绑定或改变 workflow profile。
  criteria-file 随请求固化，标题修订还需 expected-title。发布并复制完成后，SCL 仅从原请求、
  原提交及原 amendment event 采用精确 title/criteria；非默认分支必须仍匹配当前 fenced registration。
  普通命令 envelope 与历史 typed criteria 请求共用这一采用器。发布后中断及旧 completed 请求重试
  复用原事务，不重复事件，不重置 Session、epoch、checkpoint、grants 或 workflow transition。
  不可验证的输入、并发定义改变、冻结候选、终态及错配注册继续拒绝；pending 不冒充全部完成。
  `@anchor symbol adopt_published_acceptance`
  `@anchor test agent-harness/tests/test_public_acceptance_amendment.py :: test_public_cli_registered_branch_preserves_execution_end_to_end`
  `@anchor test agent-harness/tests/test_acceptance_criteria.py :: test_amend_cli_replaces_all_criteria_and_records_reconstructable_event`
  `@anchor test agent-harness/tests/test_acceptance_criteria.py :: test_stale_expected_fingerprint_cannot_overwrite_prior_amendment`
  `@anchor test agent-harness/tests/test_acceptance_criteria.py :: test_awaiting_verification_candidate_is_frozen_before_any_amendment_write`
- [x] criteria 修订同时取得同一 WI lock，并把 WI YAML + audit event 作为一组持久化：普通 I/O
  exception 会按写前原始字节恢复两者；event append 会循环处理 short write。该保证不声称覆盖进程
  SIGKILL / 断电发生在两次文件系统操作之间的 crash-atomicity。
  `@anchor symbol save_work_item_with_event`
  `@anchor test agent-harness/tests/test_acceptance_criteria.py :: test_persistence_failure_rolls_back_work_item_and_event_bytes`
  `@anchor test agent-harness/tests/test_store.py :: test_append_event_retries_short_os_writes`
- [x] 执行权逃生阀与三把扫帚以独立子命令存在
  `@anchor symbol release_executor_cmd`
  `@anchor symbol sweep_timeouts`
  `@anchor symbol sweep_executors`
  `@anchor symbol sweep_archive`
- [x] CLI 状态写入走全局写锁（比 per-wi 锁更强，ack 路径用它）
  `@anchor symbol _state_write_lock`
  `@anchor test agent-harness/tests/test_cli.py :: test_create_uses_state_write_lock`
  `@anchor test agent-harness/tests/test_cli.py :: test_ack_uses_state_write_lock`

## 2. wi 状态机

状态常量定义与迁移表分居两处：枚举在 models 模块，迁移表在 machine 模块。

- [x] WorkItemStatus 恰有 7 个状态，取值如下
  `@anchor symbol WorkItemStatus`
  `@anchor const DRAFT = "draft"`
  `@anchor const READY = "ready"`
  `@anchor const IN_PROGRESS = "in_progress"`
  `@anchor const AWAITING_VERIFICATION = "awaiting_verification"`
  `@anchor const BLOCKED = "blocked"`
  `@anchor const COMPLETED = "completed"`
  `@anchor const FAILED = "failed"`
- [x] 迁移表由 VALID_TRANSITIONS 唯一定义，执行函数为 transition_work_item
  `@anchor symbol VALID_TRANSITIONS`
  `@anchor symbol transition_work_item`
  `@anchor symbol TransitionError`
  `@anchor symbol PreconditionError`

迁移表（源码 machine.py，与代码普查一致）：

| 从 | 可到 |
|---|---|
| draft | ready |
| ready | in_progress |
| in_progress | blocked · awaiting_verification · failed |
| awaiting_verification | completed · in_progress · blocked · failed |
| blocked | in_progress |
| completed / failed | （终态，无出边） |

- [x] 合法迁移与非法迁移均有测试钉死
  `@anchor test agent-harness/tests/test_machine.py :: test_in_progress_to_awaiting_verification`
  `@anchor test agent-harness/tests/test_machine.py :: test_completed_to_in_progress`
  `@anchor test agent-harness/tests/test_machine.py :: test_draft_to_in_progress`
- [x] awaiting_verification → completed 有回执前置：无验证过的 outcome 回执不得完成；
  仍有 pending 且责任在身的 handoff 时同样挡下
  `@anchor test agent-harness/tests/test_machine.py :: test_awaiting_to_completed_requires_validated_outcome_receipt`
  `@anchor test agent-harness/tests/test_machine.py :: test_awaiting_to_completed_with_pending_handoff`
  `@anchor test agent-harness/tests/test_machine.py :: test_pending_not_transferred_receipt_does_not_block_completion`
  `@anchor test agent-harness/tests/test_machine.py :: test_pending_partial_handoff_still_blocks_completion`
- [x] 模型层第二道不变量：active_executor 非空 ⟹ status 必为 in_progress；
  blocked_reason / failure_reason 与状态绑定
  `@anchor symbol validate_active_executor_invariant`
  `@anchor symbol validate_reason_consistency`
  `@anchor symbol validate_outcome_candidate`

## 3. handoff ack 状态机

- [x] AckState 恰有 6 个状态（存量 features/ 文档只记了 4 个，缺 archived 与
  withdrawn——那是文档漂移，以本节为准）
  `@anchor symbol AckState`
  `@anchor const PENDING = "pending"`
  `@anchor const ACCEPTED = "accepted"`
  `@anchor const REJECTED = "rejected"`
  `@anchor const TIMED_OUT = "timed_out"`
  `@anchor const ARCHIVED = "archived"`
  `@anchor const WITHDRAWN = "withdrawn"`
- [x] 迁移表由 VALID_HANDOFF_TRANSITIONS 唯一定义，执行函数为 transition_handoff
  `@anchor symbol VALID_HANDOFF_TRANSITIONS`
  `@anchor symbol transition_handoff`

| 从 | 可到 |
|---|---|
| pending | accepted · rejected · timed_out · withdrawn |
| accepted | archived |
| rejected / timed_out / archived / withdrawn | （终态，无出边） |

- [x] pending → archived 是**故意缺席**的非法迁移（防未验收直接归档）
  `@anchor test agent-harness/tests/test_machine.py :: test_pending_to_archived_still_illegal`
  `@anchor test agent-harness/tests/test_machine.py :: test_accepted_to_archived`
- [x] withdraw 必须带理由；模型层校验 withdraw_reason 与状态绑定
  `@anchor test agent-harness/tests/test_machine.py :: test_pending_to_withdrawn`
  `@anchor test agent-harness/tests/test_machine.py :: test_withdraw_requires_reason`
  `@anchor test agent-harness/tests/test_models.py :: test_withdrawn_requires_withdraw_reason`
- [x] reassign 把 ack_state 强制回 pending 时**不走**迁移表——这是设计决定
  （重指并重挂，不是 ack 生命周期迁移），且同时原子释放执行权、拒绝 fanout
  与终态 wi 上的重指
  `@anchor symbol reassign_handoff`
  `@anchor symbol ReassignError`
  `@anchor test agent-harness/tests/test_machine.py :: test_releases_active_executor`
  `@anchor test agent-harness/tests/test_machine.py :: test_fanout_reassign_raises`
  `@anchor test agent-harness/tests/test_machine.py :: test_terminal_wi_reassign_raises`

## 4. 责任转移与 owner 传播

- [x] ResponsibilityTransfer 三档，只有 full（或缺省视同 full）才改 wi 的 owner；
  partial 只碰更新时间；not_transferred 原样返回
  `@anchor symbol ResponsibilityTransfer`
  `@anchor const NOT_TRANSFERRED = "not_transferred"`
  `@anchor const PARTIAL = "partial"`
  `@anchor const FULL = "full"`
  `@anchor symbol apply_handoff_acceptance`
  `@anchor test agent-harness/tests/test_cli.py :: test_ack_accept_full_transfer_updates_closure_owner`
  `@anchor test agent-harness/tests/test_cli.py :: test_ack_accept_partial_keeps_closure_owner`
- [x] handoff 合同自洽性由模型层校验（收发不得同人、reject/withdraw 理由绑定）
  `@anchor symbol validate_handoff_consistency`

## 5. 三道闸

### 5.1 执行权排他（active_executor）

判据字段是 wi 的 active_executor；无 TTL、无时钟过期，释放只有四条路：
状态离开 in_progress 自动置空、持有者或 owner 走逃生阀、reaper 凭死亡证明收割、
reassign 原子释放。

- [x] 抢占判据：已有他人持有且 wi 在 in_progress ⟹ 抛 ExecutionClaimError
  `@anchor symbol claim_executor`
  `@anchor symbol ExecutionClaimError`
  `@anchor test agent-harness/tests/test_machine.py :: test_claim_by_other_on_in_progress_raises`
  `@anchor test agent-harness/tests/test_machine.py :: test_claim_is_idempotent_for_self`
- [x] 反向交还合法性四条件（发起方=当前执行者 ∧ 收件人=操作者 ∧ full transfer ∧ 非 fanout）
  `@anchor symbol is_legal_reverse_handback`
  `@anchor test agent-harness/tests/test_machine.py :: test_reverse_handback_grants_to_original_owner`
  `@anchor test agent-harness/tests/test_machine.py :: test_claim_by_third_party_still_raises`
  `@anchor test agent-harness/tests/test_machine.py :: test_partial_reverse_does_not_auto_release`
  `@anchor test agent-harness/tests/test_cli.py :: test_reverse_handback_ack_unblocks_deadlock`
- [x] 逃生阀授权：只有持有者本人或 wi 的 owner 能释放
  `@anchor symbol release_executor`
  `@anchor symbol ReleaseExecutorError`
  `@anchor test agent-harness/tests/test_machine.py :: test_holder_can_release`
  `@anchor test agent-harness/tests/test_machine.py :: test_owner_can_release`
  `@anchor test agent-harness/tests/test_cli.py :: test_third_party_refused`
- [x] reaper 收割不设 operator 闸，授权来自上层活性判定
  `@anchor symbol reap_executor`
  `@anchor test agent-harness/tests/test_machine.py :: test_reap_clears_active_executor`
- [x] ack 拒绝抢占时不留半提交（handoff 保持 pending）
  `@anchor test agent-harness/tests/test_cli.py :: test_claim_denied_leaves_handoff_pending_no_half_commit`
  `@anchor test agent-harness/tests/test_cli.py :: test_flag_on_blocks_second_executor`
- [x] 文件锁层：wi 锁与 agent 锁不冲突，持有者被杀后锁可恢复
  `@anchor symbol acquire_wi_lock`
  `@anchor test agent-harness/tests/test_locks.py :: test_wi_lock_does_not_collide_with_agent_lock`
  `@anchor test agent-harness/tests/test_locks.py :: test_killed_holder_releases_wi_lock`
- [x] 灰度开关（值即环境变量名）
  `@anchor const WI_LOCK_FLAG_ENV = "HARNESS_ENABLE_WI_LOCK"`
  `@anchor const EXECUTOR_REAPER_FLAG_ENV = "HARNESS_ENABLE_EXECUTOR_REAPER"`

### 5.2 owner 一致性

- [x] ack 侧：接受时操作者必须在当前收件人列表里，否则按 stale owner 拒绝
  `@anchor test agent-harness/tests/test_cli.py :: test_flag_on_rejects_stale_owner_accept`
  `@anchor test agent-harness/tests/test_cli.py :: test_flag_off_preserves_old_behavior`
- [x] dispatcher 侧：同判据独立实现，重指过的 handoff 不再为旧收件人拉起分身
  `@anchor symbol _handoff_owner_mismatch`
  `@anchor test agent-harness/tests/test_dispatcher.py :: test_flag_on_skips_reassigned_handoff`
- [x] reassign 目标不得是现收件人或发起方本人
  `@anchor test agent-harness/tests/test_cli.py :: test_reassign_repoints_repends_and_releases`
  `@anchor test agent-harness/tests/test_cli.py :: test_reassign_is_atomic_on_validation_failure`

### 5.3 caller identity 与 cron 会话闸

- [x] ack 的 caller 闸：flag 开启时，环境注入的 agent 身份既非操作者、又不在收件人
  与决策授权名单里 ⟹ 拒绝
  `@anchor symbol _guard_ack_caller_identity`
  `@anchor const ACK_IDENTITY_FLAG_ENV = "HARNESS_ENABLE_ACK_IDENTITY_CHECK"`
- [x] withdraw 的 caller 闸**不受 flag 门控**（撤单是发起方专属权利）
  `@anchor symbol _guard_withdraw_caller_identity`
- [x] cron 会话状态写闸：cron 触发且 policy 未授 full_execution ⟹ 拒绝状态写
  `@anchor symbol _fail_if_cron_state_write`
  `@anchor test agent-harness/tests/test_cli.py :: test_create_blocked_in_cron_autonomous_session`
- [x] main_only 是硬闸不是灰度：cron 会话对 main_only 的 wi/ho 一律拒 ack，
  dispatcher 侧同判据拒绝拉起分身
  `@anchor symbol _handoff_is_main_only`
  `@anchor test agent-harness/tests/test_dispatcher.py :: test_handoff_main_only_routes_owner_and_skips_spawn`
  `@anchor test agent-harness/tests/test_dispatcher.py :: test_wi_main_only_routes_owner_and_skips_spawn`

ack 路径四层闸的判定顺序：main_only → owner 一致性 → caller identity → site policy；
校验与写入分段（先 VALIDATE-ALL 后 WRITE-ALL），任一闸拒绝不产生部分写。

`@anchor test agent-harness/tests/test_cli.py :: test_fanout_claim_denied_no_half_commit`

## 6. 派发链

触发链：scan 产出报告 → dispatch 读报告与 policy → 逐 hit 过跳过判定 → 拉起
autonomous 会话。

- [x] 链路核心符号
  `@anchor symbol run_scan`
  `@anchor symbol spawn_autonomous_session`
  `@anchor symbol list_pending_handoffs_for`
  `@anchor symbol DispatchResult`
- [x] 去重键 = hit_type + ref_id + severity；连续命中达阈值即抑制
  （autonomous 档 handoff 不受抑制）；hit 消失时计数复位
  `@anchor symbol _hit_dedup_key`
  `@anchor const DEDUP_THRESHOLD = 3`
  `@anchor const DEDUP_STATE_PATH = "scan-reports/dedup-state.json"`
  `@anchor test agent-harness/tests/test_dispatcher.py :: test_suppressed_after_threshold`
  `@anchor test agent-harness/tests/test_dispatcher.py :: test_count_resets_when_hit_disappears`
  `@anchor test agent-harness/tests/test_dispatcher.py :: test_autonomous_handoff_is_not_suppressed_by_dedup`
- [x] 熔断：同 hit 连续失败达阈值停止再拉
  `@anchor const CIRCUIT_BREAKER_THRESHOLD = 2`
  `@anchor test agent-harness/tests/test_dispatcher.py :: test_circuit_breaks_after_threshold_failures`
- [x] 执行权/终态卫生闸在派发侧同样生效：他人执行中跳过、wi 终态跳过且终态优先
  `@anchor symbol _wi_in_execution_by_other`
  `@anchor test agent-harness/tests/test_dispatcher.py :: test_flag_on_skips_when_other_executing`
  `@anchor test agent-harness/tests/test_dispatcher.py :: test_flag_on_spawns_when_unclaimed`
  `@anchor test agent-harness/tests/test_dispatcher.py :: test_wi_terminal_skips_spawn_and_marks_outcome`
  `@anchor test agent-harness/tests/test_dispatcher.py :: test_wi_terminal_wins_over_workspace_active`
- [x] 被执行权挡出的连续跳过达阈值要出声（防静默空转）
  `@anchor const EXECUTOR_HELD_SKIP_NOTICE_THRESHOLD = 3`
  `@anchor const SILENT_SUCCESS_NOTICE_THRESHOLD = 3`
  `@anchor test agent-harness/tests/test_dispatcher.py :: test_streak_emits_one_notice_past_threshold`
- [x] 陈旧 wi 扫描默认口径
  `@anchor const STALE_WI_DEFAULT_AGE_DAYS = 3`
- [x] 跨租户 handoff 通知幂等（二次扫描不重复推送）
  `@anchor test agent-harness/tests/test_dispatcher.py :: test_idempotent_second_scan_no_duplicate_push`

## 7. 已知内部不一致（登记待裁，不判定——防假绿）

以下前四条是实现里并存的两义处，本 spec 只登记，不替任何一方背书；裁掉其一后
应把对应条目升格为带机器锚点的契约行。末条是已知合同真空的登记（见 §0）。

`@anchor manual terminal-ack 两套口径 :: dispatcher 的 _TERMINAL_ACK_STATES 不含 timed_out，而 cli 终态自动收尾把 timed_out 算终态；比对两处源码确认是否已归一`
`@anchor manual reassign 绕迁移表 :: reassign_handoff 直写 ack_state=pending 不经 VALID_HANDOFF_TRANSITIONS，迁移表非唯一写路径；确认设计说明仍在其 docstring`
`@anchor manual born-stuck 判据共享 :: cli 的 born-stuck 提示判据要求与 context_hygiene.check_wi_terminal 保持一致，靠注释约定无机器强制；改任一侧须双改`
`@anchor manual ho-sla-days 双默认 :: sweep-timeouts 与 lint 各有一份 default=7 的 --ho-sla-days，靠 help 文本口头约定同源；改默认值须两处同改`
`@anchor manual dispatcher 拉起执行层待立锚 :: dispatcher.py 的 _build_prompt、_night_patrol_ 族、_detect_tool_denied/_is_rate_limited/_detect_wrap_missing 仍零 spec 覆盖，属已知合同真空非有意豁免；_detect_runtime_blocked 已由 harness-runtime.md 接管；立锚前其余符号以代码为准，观察步骤：grep -n "^def _build_prompt\|^def _night_patrol\|^def _detect_" harness/dispatcher.py 核符号族仍在`

## 8. 覆盖声明

本 spec 首次给以下此前零文档覆盖的层挂上机器锚点：执行权排他全层（claim /
reverse-handback / release / reaper）、reassign、withdraw、main_only 硬闸、
caller-identity 闸、三个灰度开关、派发去重与熔断常数。存量 features/ 42 份的
分类处置（契约 / 记事 / 退役）是 KR1 的活，不由本文承担。
