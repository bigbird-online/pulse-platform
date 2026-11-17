---
version: 1.9.3
status: candidate
work_item: wi_20260827_282
owner: Pulse
consumers: [PULSE, Relay, work-item-lifecycle]
---

# SCL WorkItem Golden Path Contract

本文件是 WI 生命周期与 Session replacement 的唯一平台契约。WorkItem 是长期协作身份；Session 只是同一 Agent 可替换的物理执行容器。runtime、skill 或 channel 文档只能引用本契约，不得另造用户流程。

本文件定义用户意图与生命周期语义；本地连续性、能力降级、authority fencing、机器协议和外部 effect
交付以 `harness-scl-resilience-requirements.md` / `harness-scl-resilience-design.md` 为准。旧实现中“任一入口或
外部依赖失败即阻断 prompt/WI”的行为不是目标契约。Fail-closed 只约束需要该证据的 mutation 或外部
effect，不得扩大为 conversation、local read 或 ordinary local work 全面停机。

## 用户入口

已进入的同一 WI 增加业务仓库时，当前 attached owner 使用
`adopt-workspace-authority-local --add-repository <descriptor.json>`，按
[managed-workspaces.md](managed-workspaces.md) 的追加契约旋转登记 authority。
不得先用 legacy migrate 修改 manifest，也不得以 reinit 或新 WI 绕过缺失写面。

| intent | 用户入口 | durable 结果 |
|---|---|---|
| enter / continue | `/wi <wi>` | local kernel 解析 identity/surface 后一次恢复；实施态决定 `reuse_current` / `fresh_from_checkpoint` / `execute_local_isolated`，送验态返回 `observe_current`，仅冲突 effect 被 fenced |
| withdraw frozen candidate and resume execution | `/wi reopen <wi>` | 明确人类意图下保留原 WI/epoch/checkpoint，原子清除旧候选并恢复 owner executor 到当前 Session；旧验真 handoff 仅保留为历史 |
| same-Agent replacement | `/wi rollover <wi>` | 同 WI、owner 与 responsibility；不创建 handoff |
| 新的独立业务目标 | 新 WI | 不复用旧 WI 的 Outcome authority；不指当前 WI 的独立验真 |
| 当前候选的独立 Outcome 验真 | 同 WI 的 verification assignment | 独立 receiver 验原目标与固定候选，不另建业务 WI |
| same Outcome next phase | 同 WI phase | 不换责任主体 |
| cross-Agent responsibility | 同 WI handoff | 只由 receiver 接受后转移执行权 |
| external wait | blocked + dormant | 不制造工作或 terminal sample |
| UAT / review / closeout | 同 WI | Session 完成不等于 WI 完成 |

普通用户不得提供 continuation id、raw provider Session identity，也不得用 `/new`、`/reset`、`/kill` 绕过 work frontier。

`/wi` host adapter 可先于模型执行：它从当前正式 workspace 解析 Agent，注入已注册 surface/runtime，
将 raw host Session identity 单向映射为 opaque ref，并只调用一次 local SCL client。成功后模型直接消费
ResumePacket；adapter timeout、import failure 或 malformed response 时必须放行原 prompt 并注入 degraded
warning，不能吞掉用户消息。此时不得冒充 lifecycle mutation 已成功；模型可重试 canonical local client，
也可在不需要缺失 authority 的范围内继续讨论与 ordinary local work。显式 `/wi status`、rollover/recovery
诊断才消费 `status.golden_path`；普通 `/wi` 不先执行通用 remote status。

`awaiting_verification` 表示已固定送验快照，不单独决定 producer 是否仍有执行权。受管 SCL
保留送验前的 active producer frontier：同一绑定 Session、active execution 和匹配 owner 时，入口可返回
`execute_fenced` / `execute_local_isolated`。旧兼容投影的 `active_executor=null`、不同 Session 或已撤销
执行权的候选则只读协调验收（SCL `observe_candidate`；legacy execution `observe_current`），不得因
`/wi` 恢复而重获执行权、改写 binding/session/epoch 或更换 WI。具体 mutation 仍核对其原 source 与能力。
ResumePacket 必须投影当前唯一的 `verification_assignment`：没有 assignment 时 producer 只创建一条 typed
`purpose=outcome_verification` handoff；pending 时只等待 ack；accepted 时只等待 exact receiver。Producer
不能兼任本候选的独立 verifier、写/替换独立 Outcome evidence 或 receipt、调用 verify-result；
合法保留的 producer 执行权用于修正和重新送验，不将其自检冒充独立验真。

上述行为只适用于用户继续当前冻结候选。用户若明确决定放弃该候选、按新规划继续同一 Outcome，必须使用
`/wi reopen <wi>`，不得等待 Duke/其他 verifier，也不得把 reopen 伪装成 rejected Outcome verdict。Codex
host adapter 在 `UserPromptSubmit` 边界先执行一次 `execution reopen`，成功后以同一 opaque Session ref 执行
一次 `continue`；任一步失败都保留同 operation 与 source frontier、放行 prompt 并报告 degraded result，
不得冒充 reopen 已成功。reopen 仅允许 human-confirmed、unstamped interactive main body，
要求原 WI 为 `main_only + awaiting_verification + active_executor=null`，caller/owner/frozen producer/binding
agent 完全一致，并有 durable checkpoint 与 workspace authority。该 mutation 原子完成：

- `awaiting_verification → in_progress`，清除旧 claim/change type/producer/fingerprint；
- claim 原 owner 为 active executor，并把同 epoch binding 换绑当前 Session；
- 将旧 checkpoint 移为 resume frontier，清除旧 continuation/completion projection；
- 持久化 `verification_reopen_receipt/v1` 与审计事件；
- 不修改、撤回或删除已 accepted handoff。由于 WI candidate 已清除且 `updated_at` 前进，旧 assignment 自动不再投影为 current。

同 operation 的 exact retry 幂等；参数漂移、cron/stamped caller、非 owner/producer、缺 checkpoint、
transitioning/recovery-required lineage 必须拒绝会产生重复 authority 的 mutation，但不得阻断 conversation、
source read 或仍安全的 local work。Hook/client 失败保持同 operation 可重试，不把 adapter failure 伪装成
reopen success。reopen 后 ResumePacket 必须为
`work_item_status=in_progress`、`execution_authority=true`、`consumer_work_mode=execute`，不得再协调旧 verifier。

Relay 消费密封 SCL ResumePacket 时，是否有待撤回候选以同一 WI 的
`work_item_status=awaiting_verification` 为准。原 producer 的执行授权仍可投影为
`execute_fenced` 或 `execute_local_isolated`，不必先变成 `observe_candidate`。
Relay 仅接受匹配 WI/Agent、非空 epoch 的候选计划；执行型计划还须有
`execution_authority=true`。执行权字段本身不能证明候选存在，普通 `in_progress`
或来源未解决的计划不得触发撤回。正式 SCL 继续裁定 source/owner/冻结操作权限，
撤回读回须为同一 WI、Agent、epoch 的 `in_progress` 执行态；不能把 epoch 变化当作 reopen 成功。

Outcome verification assignment 必须是 frozen producer 在当前 candidate freeze 之后发给单一独立 receiver
的原 WI handoff，且 `responsibility_transfer=not_transferred`；重新 freeze 后旧 typed assignment 也失效。
既有 live WI 的 pre-v2 handoff 只在 from/receiver/责任字段完全匹配且 live assignment 唯一时兼容投影；
多个候选 fail closed。
注册 WI worktree 若以 sparse-checkout 隐藏 tracked handoff，投影必须读取该 exact worktree 的 stage-0
skip-worktree index blob；普通 tracked deletion 不得用 HEAD fallback 掩盖。
`verify-result --assignment <ho_id>` 只允许 accepted assignment 的 receiver 调用，并要求
caller identity = assignment receiver = receipt.verifier，receipt 继续匹配 frozen candidate。Broker envelope
必须保留绑定 caller identity；publisher replay 不得剥离它。

独立 verifier 的 assignment 与后续 lifecycle mutation 必须经 Broker 的隔离 projection 执行。注册 WI
worktree 可以保留 verifier 已留下的 Outcome 草稿，但只限当前 WI Outcome namespace 中的未跟踪 regular
files；平台在 mutation 前绑定其 exact paths 与 byte manifest，在 mutation 后必须读回相同 paths 与 bytes。
这些草稿不进入 projection commit，不得被 Harness 或 Agent add、commit、修改、删除、stash、reset 或 clean，
也不构成已签发 Outcome。任一 tracked/staged dirt、跨 WI 路径、symlink、非 regular file 或 preimage 漂移
仍须在 mutation entry fail closed；不得以清空整个注册 worktree 作为 verifier assignment 的前置条件。

首次 entry 必须优先读取 local kernel 的 latest-known global + local frontier；不得因 human shared root 落后、
remote 不可达或 replication pending 而返回 `not_found`。只有本地从未取得 WI/kernel/snapshot material 时才返回
`local_material_unavailable`。对开放、未 claim 的 `main_only` WI，owner entry 可通过 `activate-owner` 原子完成
`draft/ready → in_progress + owner executor claim`，随后只登记已存在且 clean 的 linked WI worktree。登记和
执行写入不吸收、不清理 unrelated shared-root dirt；owner、surface、remote、branch、base 或 worktree
cleanliness 不闭合时，只拒绝依赖该事实的 authority/publication effect；存在可读 workspace 时仍允许明确标记的
`execute_local_isolated`。

若该既有 draft/ready WI 只是缺少 Platform Epoch 的 CR/OA 映射，不得再创建一个行政/bootstrap WI 来修改
准入条件。人类明确决定映射后，owner 的交互 Session 先通过 canonical `harness admit-existing` 发布独立的
`work_item_admission/v1` 记录；它不改写 WI source、状态或 executor。读回唯一 typed admission 后才执行
`activate-owner`。Agent/cron 不推断映射，重复同一决定幂等，任何 redirect 或旧映射冲突均 fail closed。

`@anchor symbol ExecutionResumeManager`
`@anchor symbol WorkItemExecutionBinding`
`@anchor symbol activate_owner`
`@anchor symbol record_existing_work_item_admission`
`@anchor test test_draft_main_only_owner_is_atomically_activated`
`@anchor test test_service_authority_root_maps_to_matching_registered_linked_tree`
`@anchor test test_awaiting_verification_resume_restores_frontier_without_execution_mutation`
`@anchor test test_awaiting_verification_resume_projects_accepted_legacy_assignment`
`@anchor test test_awaiting_verification_resume_projects_pending_typed_assignment`
`@anchor test test_awaiting_verification_resume_ignores_typed_assignment_from_prior_freeze`
`@anchor test test_sparse_skip_worktree_handoff_is_read_from_exact_index`
`@anchor test test_stale_project_folder_blocks_before_harness`
`@anchor manual failed-entry-degrades-without-blocking-prompt :: target replaces the legacy hard-block test; installed Hook must allow the prompt, surface a stable degraded result and perform no unauthorized mutation`
`@anchor test test_explicit_reopen_withdraws_candidate_then_enters_same_wi_for_execution`
`@anchor test test_explicit_reopen_invalidates_assignment_and_returns_execute_packet`
`@anchor test test_registered_wi_projection_reconciles_only_its_exact_branch`
`@anchor test test_harness_projection_rejects_other_worktree_dirt`
`@anchor test test_harness_projection_rejects_cross_wi_or_symlink_outcome_dirt`

## Rollover lifecycle

```text
active(epoch=N)
  -> checkpoint persisted
  -> prepared / transitioning(epoch=N)
  -> source usage sealed when runtime=Pi (CC records not_applicable) and source retired
  -> one fresh target starts at epoch=N+1 (Pi carries digest-bound usage authority)
  -> target continuation ACK and first settled result persist
  -> continuation consumed
  -> applied / active(epoch=N+1)
```

`prepared`、`completed`、`recovery_required` 不得混写。Controller 必须把 source retirement/usage-seal（Pi seal 绑定 exact WI/epoch）、target authority、真实成功 result 事件 digest/timestamp 与 fresh runtime readback 组成 `rollover_completion_evidence/v1`；CC readback 证明 live session/runtime，Pi 还必须证明 epoch/usage-binding digest。Harness 在 apply 时校验并持久化。只有该 evidence、consume proof 与 apply postcondition 全部成立，才是 `completed`。target failure、timeout、restart contradiction 或 source recovery 证据不足使 replacement operation 保持 pending/recovery；retry 复用同 operation，最多一个 target。target 未证明可用前 source 必须保持可继续，不能因 replacement effect 失败把 WI 整体卡死。

跨 registered resident surface 的 target claim 必须绑定完整 `managed_workspaces/v1` registration digest。target-neutral reservation 只允许 Host 在无 business turn 的 process-start 阶段，以当前 registered surface 和 exact worktree realpath 做只读 admission；该 admission 不授予 execution authority。两个 resident host 使用独立 kernel 时，target 先以 `--reserve-for-rollover` 完成 registration 并在本机生成 descriptor；source 原子退役本机 fence 后才输出 mode-0600、content-digested handoff capsule。capsule 只携带脱敏 source frontier、完整 checkpoint、authority generation 与 target descriptor，不得携带 raw Session identity。target 只允许向不存在该 WI 的 cold kernel，或无 authority、operation、checkpoint、attachment、measurement、candidate 与 replacement 的 exact pristine unbound revision-zero placeholder import；后者在同一 transaction 中按 birth identity 与完整空依赖判据替换，任一 non-pristine target 均拒绝，exact imported replay 保持只读。target 必须从本机 registration bytes 重建并匹配 descriptor；import 把脱敏 source binding 固定为 `transitioning`，不授予 target execution authority。Host surface/runtime 必须来自本次 Host 观察，managed workspace 必须按 exact WI/agent/surface/repository registration 解析，不得依赖静态 Relay workspace 或当前目录。live target claim 必须绑定同一 registration digest，Host 在 target business turn 前再次只读验证 exact surface、digest、epoch 与 worktree realpath，并把该 readback 与首个 settled control result 一起交给 apply。apply 在 target 单一 kernel transaction 中切换 canonical surface、Session/epoch 与 authority fence，退役 imported source registration；source seal 后不能执行或发布，target apply 前也不能执行 business turn，因而不产生双写窗口。same-surface rollover 不旋转未变化的 fence。legacy/local-isolated compatibility 不得成为 managed cross-surface fallback，也不得接受手填 Host identity 替代注册与 runtime readback。

`@anchor test test_execution_rollover_epoch_and_recovery`
`@anchor test test_execution_recovery_required_blocks_continue_and_new_rollover`
`@anchor test test_fenced_cross_surface_rollover_switches_one_registration_bound_fence`
`@anchor test test_independent_host_kernels_use_private_sealed_handoff_without_split_brain`
`@anchor test test_continue_local_uses_registered_host_surface_before_target_business_turn`

## Stable read model

只读 snapshot 的 content_digest 校验对象是落盘原始 JSON payload（排除摘要字段），校验在模型补默认字段、时间/枚举规范化之前完成。随后仍执行完整 schema 校验；版本演进增加可选字段不得令旧快照失效，也不得重写旧文件或为其生成新摘要。读取成功只证明该历史投影可用，当前 write authority、registration 和 runtime workspace admission 仍按各自消费者复核。

`@anchor test test_legacy_snapshot_supports_real_registered_workspace_admission`


`harness execution status --json` 的 `status.golden_path` 是 skill 与 consumer 的稳定 lifecycle projection：

- schema `work_item_lifecycle_view/v1`；
- `entry_intent=enter_or_continue`；
- `rollover_state=none|prepared|completed|recovery_required`；
- `same_agent_replacement=same_work_item_no_handoff`；
- physical Session freshness 必须由 runtime readback 补证，Harness 不推断。

它是显式状态查询、rollover 与 recovery 的稳定 read model，不是普通 `/wi` 热路径要求 Agent先执行的
preflight。`rollover_state=completed` 必须由 consumed continuation、`rollover_completion_evidence/v1` 与 apply 后 binding 的一致性共同投影，不能仅因 `continuation_id` 非空或 consume/apply 形状得出。旧 binding 缺少 completion evidence 时保持 `recovery_required`；已证明恢复 exact source 的 released continuation 投影为 `none`，不得误报 completed。

`ExecutionManager.status` 定义于 `harness/execution.py`；其已完成投影由下列行为测试约束。

`@anchor symbol ExecutionManager`
`@anchor test agent-harness/tests/test_execution.py :: test_status_does_not_project_completed_from_continuation_id_alone`
`@anchor test agent-harness/tests/test_execution.py :: test_status_requires_completion_evidence_after_consumed_apply_shape`
`@anchor test test_execution_binding_is_backward_compatible_and_private`

## Usage seal readback

Pi `/usage_seal` 必须区分首个 `sealed`、`idempotent_replay` 与稳定失败 code，并返回 artifact ref、epoch、sealed digest。成功 chat 文本不构成 receipt：唯一有效 authority 是 runtime binding 匹配、mode-0600 regular file、snapshot canonical digest 通过且一次调用只有一个 receipt。

`@anchor test ComputeCost/tests/test_pi_usage_extension.mjs :: sealed capture is durable, restart-idempotent, and concurrent single-winner`

## Invariants and boundary

- checkpoint / ResumePacket 是 fast path；只复核实际需要的 `stale_if` / `must_read`。
- resident `/wi` 的 control-plane 目标为 30 秒内完成或稳定失败；超时不得转为模型考古。
- single-winner authority；source 不在 target 可恢复前丢失恢复材料。
- raw Session、thread、user identity 与 transcript 不进入 Harness、usage artifact 或用户回执；只用 opaque refs。
- unknown 不回填；absence 不写成 zero。
- same-Agent rollover 不建 self-handoff；Outcome verification 与 responsibility transfer 保持独立。
- explicit verification reopen 不是 Outcome verdict；保留历史 handoff，但它不再约束新候选的执行计划。
- Git publication、surface replication、runtime upgrade 与 Hook 是独立 effect/adapter；其失败只降低相应 capability。
- local isolated progress 不得冒充 global canonical truth，但必须保留并可在恢复连接后 reconciliation。
- 本 contract 不启用 S1 shadow、suggestion 或 autonomous rollover，也不代替 WI730 natural terminal closeout。
- UAT 使用真实 managed install/readback 与 genuine Pi rollover；不得制造 terminal sample。


## 最终结算的发布账本

`closeout publish` 使用统一 ConvergenceManager 所解析的唯一已登记
`control_plane_role=work_item_authority`，结算模块不另维护生产仓库常量或实验仓库白名单。
生产登记指向 PULSE，正式隔离实验登记指向实验仓；bundle 中声明的发布仓库必须解析为这一身份。
缺失、歧义或不匹配的登记在写入结算证据前拒绝。实际发布仍由统一 publisher 核对远端身份、
封存 topology 与 CAS；这条合同不授权改变登记或借用另一仓库的发布权限。

冻结候选、Outcome/review、业务成果仓身份和必需消费者集合保持原证据约束。账本发布成功不证明
业务已集成或 WI 已完成；terminal 仍要求每个声明仓库的实际默认分支集成、精确 resulting head
的 CI/smoke、消费者收敛和真实资源处理证据。实验仓使用自己的 CI 证据，不继承生产 CI 结论。

`@anchor test test_frozen_service_publication_exact_ci_receipt_and_complete`
`@anchor test test_settlement_requires_the_single_registered_publication_authority`

## 候选送验与旧发布恢复

`recover-unpublished-create` 将校验过的原创建字节保存在 Git 外；归档一旦存在，正式文件由原发布器管理。
归档与发布后采用的文件同时存在不构成冲突。中断或完成后的精确重试均消费相同归档和幂等键，
不覆盖正式文件、不生成第二次创建；归档摘要不符仍拒绝。

跨进程发布恢复可用 `HARNESS_MUTATION_ERROR_FORMAT=json` 读取 stdout 的
`canonical_mutation_failure/v1`，原人工错误仍保留在 stderr。它携带精确 command argv、原 recovery ref
和发布器的 code/retryable/effect_state，不改变命令信封或授权。Host 只有在最后一次完整调用日志
匹配原命令、请求和 `rejected_no_effect` 时，才能再次调用原发布器；退出码或错误文案不证明无效果。
超时、中断、旧版非结构化错误、缺少回执或未知效果仍沿原 Broker 观察/恢复，不能删 Host 记录重来。

`@anchor test agent-harness/tests/test_canonical_mutation.py :: test_local_create_recovery_reuses_real_publication`
`@anchor test agent-harness/tests/test_canonical_mutation.py :: test_cli_preserves_publisher_failure_for_original_request`

正式 managed lineage 的 `harness ready-for-uat <wi> --claim ... --change-type ...` 必须转交
已验证的 installed SCL `ready-for-uat-local`。本地操作先原子提交 `awaiting_verification`、保存
候选声明和 AC fingerprint，并创建持久发布 effect；本地 producer/source binding 保留，canonical
Git 候选发布时清空 active executor。Git 或复制失败不回滚本地候选。
原 Session、epoch 与 checkpoint 保持不变。unsupported client 不得退回 Git-only 写入。

同轮相同请求返回原 operation；不同声明返回冲突。撤回后再次送验即使声明相同也属于新轮，不能复用
已撤回操作。外部发布沿用 operation 对应的 outbox key；Broker 在实际 push 前再次核对本地候选
revision、payload 与 authority fence。前沿改变时停止旧候选发布，报告明确 conflict。

发布来源由该冻结操作与当前 SCL 登记共同决定。旧 execution binding 缺失不能把有效登记工作树降级为
服务 main。必须核对当前 WI、owner、producer、epoch、Session、runtime、surface、manifest digest、
完整 AC fingerprint、冻结 revision/payload/fence 及 Git branch/base；Git 旧候选仅可按已提交的撤回证明
在内存中还原到合法 producer 状态。已整合的 main-only 来源仍可用，但也须匹配同一冻结和当前 AC；
服务 main 上的旧标准不能替代受管分支的新标准。
`main_only` 优先分支也必须在返回来源之前完成同一冻结检查；开放且未 claim 的旧 main 候选
不代表当前候选。该前像不匹配时继续解析当前登记来源，匹配时保留已整合 main 的合法路径。

Standard 的独立 Outcome accepted 可以保持 `awaiting_verification` 等待人审。来源校验按同一 WI、验收指纹、独立 verifier、accepted 裁决、人审要求和合法时间识别这条保持候选的事件，仍使用原送验转换；真正的状态转换和候选变化不能被忽略。该识别不替代 review/closeout 的证据校验。

独立验真的交接、Outcome 与人审记录来源必须匹配当前已发布的 SCL freeze、完整候选字段和同一送验事件；
服务 main 的旧文档/旧 AC 不再因 main_only 优先直接被选中，匹配时仍保留正式整合 main 的路径。
实际登记的 producer/epoch/surface/digest 与 Git 读回守卫继续成立；读回或发布前来源变化则零效果拒绝。
此切片不扩大最终候选结算或撤回权限，也不把已导入且无 SCL freeze 的 legacy 候选改成新合同。

旧发布器已经给同一冻结请求签发错误来源的 Broker 事务时，只有原事务明确为 typed projection 拒绝、
准备工作树和 Git inventory 均已移除，且没有 prepared/published/integrated/reconciled/replicated
产物，才允许修正来源。修正持有统一 publisher 锁，重新核对当前登记、冻结操作、同仓库/producer、
原 operation 字节、原事务和 request CAS；原请求快照先写入私有 `source-corrections/<request_ref>/`
摘要文件，再更新原 outbox 的来源 fingerprint/base，沿同一 request/idempotency key/operation 签发
新来源事务。旧失败 Broker 和快照保持可读，outbox wire/record schema 不变。未知效果、超时、请求
并发变化、已有提交或复制、候选前沿改变均拒绝；不得重新 freeze 或创建新 WI 来跳过旧失败。

已签发的同源事务若停于 `discovered` / `remote_moved`，原工作树正式快进之后，仅当完整差异
都是新增的同 WI 有效格式 SCL 审计回执、业务与控制文件不变，才允许沿原请求修正前像。
旧 authority fingerprint 必须能由当前完整 authority 仅替换旧 `source_head_sha` 精确复算；
登记、owner、epoch、Session、候选与 AC 变化不能借此恢复。发布器重新核对当前冻结和登记，
Broker 独立证明无准备工作树、提交、stdout 或任何外部效果，并持有事务锁直到原 outbox 完整
归档和原子替换结束，排除并发准备。旧 Broker 不修改，新的事务仍绑定原 operation/request/
idempotency。脏工作树、无效或其它 WI 审计、业务改动、未知效果和核对期间远端变化均拒绝。

若 producer 仍在原干净基线、只有远端审计先推进，原 canonical publisher 的 `ready-for-uat` 事务可在
同一 Broker 内恢复。必须绑定唯一合法 `--scl-candidate-operation`、当前非保护分支和有效冻结证明，
并证明没有准备目录、提交、stdout、集成、复制或端点效果。完整远端增量只能是同 WI 的新增 typed
SCL 审计回执；证明前后复核本地 Git 事实与远端 SHA。事务记录 `authority_remote_sha`，保留原
`base_sha`、operation、request 和 tx；正常投影再核对审计增量，发布前再核对候选与远端 CAS。
只有原发布成功后，正常复制才使 producer 快进；不要求 Agent 先拉分支。记录 authority 后中断的
`discovered` 事务只由匹配的原 recovery outbox 续办。业务/控制变化、脏工作树、无效/跨 WI 审计、
冻结失效及核对期间的变化均拒绝，未知效果不被解释为可重试的零效果。

`@anchor test agent-harness/tests/test_candidate_remote_audit_recovery.py :: test_original_candidate_recovers_unadopted_remote_audit`
`@anchor test agent-harness/tests/test_candidate_remote_audit_recovery.py :: test_candidate_remote_authority_requires_registered_frozen_envelope`

历史 Git 已送验而 kernel 仍开发中的 WI 使用 `recover-published-candidate-local <wi>
--transaction-ref <tx>`。系统验证 exact Broker publication、canonical main ancestry、当前 Git
候选内容、owner 与 AC 后，仅补内核候选状态；复制沿用该 transaction。恢复不 claim 原执行权，
不生成新 Git 候选，不 reinit，不代替 Outcome、human review 或 terminal settlement。
`local_committed` 与 `candidate_publication` effect 的复制成功 receipt 是两个不同结果。

复制重试以 required endpoints 的真实覆盖为前提。main 已前进时，系统必须证明原发布提交是实际收敛
head 的祖先，并保存独立 inclusion receipt，分别记录 `published_sha` 与 `converged_heads`；不能把
新 head 冒充原提交的 exact-SHA receipt，也不能为满足旧事务而回退端点。endpoint dirty、缺覆盖或
无 ancestry 证明时保留 partial，并返回具体阻塞码及 receipt 路径。

候选撤回后再次送验时，发布端可在同一个 Git projection 内先投影内核已提交的撤回，再投影新候选。必须校验 Git 旧候选与前一 freeze 完全一致，且该 freeze、撤回、新 freeze 的 journal 顺序成立；不得仅因 Git 仍为待验收就覆盖未知候选。历史验收 handoff 保留，Session、binding、checkpoint 不在此发布阶段重建。

该撤回证明同样适用于独立 Outcome 已将 canonical WI 置为 `blocked` 的重验；普通 `blocked → awaiting_verification` 仍非法。首轮失败若没有 prepared/published commit 或投影树，修复源码后的恢复继续复用原 operation、outbox 和 Broker transaction。只有当前候选 payload/fence 与撤回证明仍合法、原/当前工作树干净、canonical branch 为原基线后继，且 `.harness` 中本请求依赖的控制数据未变化时，Broker 才可采用新源码基线；独立的 `.harness/evidence/scl-operations` 审计变化不参与控制数据比较。已提交的其他 WI 的 work-item、event、admission 记录仅在前后 blob 均为普通文件、通过各自模型校验、路径与内容 WI 一致且不引用当前 WI 时可保留为 canonical 基线；发布 diff 不得改写这些记录。共享 policy、registration、assignment、receipt 及未知控制路径变化继续拒绝，当前 authority/fence 与候选证明仍实时复核。原 outbox 保留原基线，事务记录恢复次数、时间和实际新基线。候选变化、控制数据变化、脏改、非后继或已有发布效果均不得通过此路径重绑定；发布后的复制继续使用原发布恢复契约。

同一 WI / Session 多轮 `reopen` 的默认幂等键必须绑定最近一次 `freeze_candidate` operation；相同 claim / AC 的不同冻结轮次也不可复用旧 withdrawal。撤回后的精确重试继续使用该冻结锚点。没有内核冻结记录的 legacy 候选保留原默认键兼容；升级前的旧键 withdrawal 仅在其 sequence 晚于最近 freeze、WI 与 intent 正确时作为同轮精确重试保留。调用方显式 `--idempotency-key` 仍有效，应固定对应一次撤回意图，不跨冻结轮次复用；最终执行资格以当前 resume 为准。

### 验前修正后的重送

独立 Outcome 将账本置为 `blocked`、`partial/rejected → in_progress` 后，SCL 可以仍保留上一轮冻结。
原回执摘要、原冻结事件、独立 verifier/assignment、结果事件、owner/AC 和当前执行前沿全部匹配时，
同一候选证明允许返工 handoff、ACK、reassign 与 task-outcome 记账；不要求伪造状态同步、撤回或人审。
该证明只解释已发布的返工事实，不授予业务执行权，不允许旧候选继续 verify-result 或 human review。
partial/rejected 返工后送新候选时，投影用同一历史证明还原 SCL 已保留的 producer claim；不重写旧裁决或内核。

`@anchor test agent-harness/tests/test_candidate_rework_routing.py :: test_published_outcome_rework_routes_original_handoff_without_kernel_rewrite`
`@anchor test agent-harness/tests/test_candidate_rework_routing.py :: test_rework_does_not_authorize_changed_or_unverified_sources`
`@anchor test agent-harness/tests/test_candidate_rework_routing.py :: test_reworked_candidate_can_be_submitted_on_the_same_wi`

最终冻结在原 source 绑定之外，只接受已经存在的 Host 发布 grant；它必须匹配当前 Session、Agent、surface、
runtime、epoch，并覆盖全部已登记冻结资源。先持有原资源锁并复核前沿，再沿同一 grant evaluator 与
原已发布候选/Outcome/人审证明冻结。无 grant、已撤销、只读、未就绪、过期或部分范围均拒绝；
自报 `--host-agent-id` 不构成权限。该检查沿 SCL 的 protocol-only Host 边界，不声称操作系统身份隔离。

`@anchor test agent-harness/tests/test_scl_candidate_source_recovery.py :: test_final_freeze_rejects_unbound_host_even_with_owner_name`
`@anchor test agent-harness/tests/test_scl_candidate_source_recovery.py :: test_later_host_final_freeze_requires_current_complete_publication_scope`

当前生产者保留执行权、尚未最终冻结时，新 `freeze_candidate` 可以直接重送验前修正后的候选。原候选仍待验收时，必须核对旧候选字段、正式冻结事件及其原 operation；不同 claim 产生新的默认幂等请求，相同 claim 的精确重试保留原请求。发布仍须证明当前 operation/frontier、相同 owner、实际执行状态及无 final review digest；Git 候选字段已清除时，沿下节相同的原回执摘要、blocked event、前一冻结及 verifier assignment 历史证明核对，顺序为前一 freeze → 原冻结事件 → blocked event → 本次 resubmission。缺任一关联仍拒绝。投影事件记录 `scl_previous_candidate_operation`、`scl_resubmission_operation` 和三项 `scl_blocked_outcome_*` 来源；不制造 withdrawal、人类批准或旧 verifier 当前写权。原候选、原裁决和新候选分别保留；源 Session/epoch/标准不重建。已存在有效 withdrawal 的路径继续使用原撤回证明。失败后的恢复复用同一 operation、outbox 和 Broker transaction。

### blocked Outcome 后的历史候选证明

正式 `verify-result(blocked)` 会清除当前候选字段。显式撤回后重新送验的投影必须从 canonical Outcome receipt、实际 SHA-256、对应 blocked event、前一冻结事件和原 verifier assignment 恢复历史证明，并核对 claim、producer、change type、AC fingerprint 与内核前一 freeze 一致。旧 freeze、回执事件、assignment 创建、withdrawal 和新 freeze 的归属及顺序均须吻合；旧冻结事件无 operation 字段时仅允许此有界历史证明。新冻结事件记录 `scl_candidate_operation`，撤回投影事件记录 `scl_blocked_outcome_event`、`scl_blocked_outcome_receipt_sha256`、`scl_blocked_outcome_assignment` 与 `scl_withdrawal_operation`。原 assignment 只证明历史归属，不恢复当前 verifier 写权。任何缺失或错误关联均拒绝，恢复仍复用原 request / transaction。

恢复已发布候选发现 kernel 漏掉历史 acceptance amendment 时，仅允许补入原发布提交内有正式 `work_item.acceptance_criteria_amended` 事件证明的追加标准。事件必须匹配 WI、owner、完整 before/after criteria 与 fingerprint，并有审计理由；替换/移除现有标准、证据缺失或当前 Git 候选已变化均拒绝。采用记录与候选 freeze 在同一个带 frontier CAS 的内核操作中提交，保留原 Session/epoch/checkpoint；重试复用原操作与 Broker transaction。此入口不授权新的验收标准变更。

历史 receipt 在 canonical service 稀疏检出中未展开时，只可读取该精确路径的 `S`（skip-worktree）普通 stage-0 index blob，并核对其与 HEAD 普通文件对象相同，再按事件中的 SHA-256 校验。不得展开证据目录或回写快照；普通删除、符号链接、祖先重定向、未提交 index 替换不能通过 HEAD 回退被掩盖。

## Non-delivery disposition 的本地终态与复制恢复

符合现有 `unregistered_main_only_control` 条件的 unclaimed main-only WI，执行人类授权的
`disposition` 时，只需先将唯一登记的本地 canonical publisher 安全 fast-forward 到固定 GitHub
base。此步骤仍拒绝本地 dirty、ahead/diverged、authority movement 与身份/路径不匹配；它不探测、
清理或接管远端 peer 的未发布证据，也不生成全端已收敛的假 receipt。claimed orphan 的原有约束不变。

Broker 发布 exact disposition 后，先验证原 publication 被当前 GitHub head 包含，再刷新本地 canonical
reader。若该 WI 已存在于 managed SCL kernel，系统以同一 publication/transaction 为幂等键，将已核实的
`cancelled/superseded/migrated` 决定原子接纳到本地内核，清除 executor 并停止业务；原 Session、epoch、
checkpoint 和历史不变，未绑定 WI 不会因此获得 binding。终态能力投影必须为 `local_work=unavailable`，
再次业务 entry 必须拒绝。该接纳不等于交付成功、独立 Outcome 或人类验收，也不创建新 WI 或 reinit。

其他端点复制在发布后独立恢复。失败结果必须明确区分“尚未发布”和“已发布但恢复 pending”，保留原
`recovery_ref`、transaction、published SHA 与实际错误。已发布请求重试只恢复原 transaction 的尾部，
不得因 WI 已进入终态再次要求业务 execution authority，也不得重新执行 disposition。尾部重试仍取得同一
canonical publisher 排他锁；占用时保持原请求可重试，不能并发覆盖 publication/replication 状态。参数漂移、
本地已重新开启的 WI 或 publication 与当前终态不一致仍拒绝接纳。终态可支持旧 workspace/projection 的
正式退役；新的发布 transaction 与未完成复制 recovery 对象必须保留，不能随旧对象清理丢失。

Runtime process-start admission 与明确允许共享根保留无关改动的 workspace validation，只验证登记的
Git root/common-dir/worktree attachment、branch/remote/base identity 和当前 SCL frontier；不得再执行
结果不会被消费的共享根 `git status --untracked-files=all` 全量扫描。要求 cleanliness 的 publication/
settlement 路径仍执行原有检查并拒绝无法核验的 dirt。启动耗时不能取决于共享 human root 中无关文件
的数量；这不是放宽执行 authority、允许新路径或增大 timeout。

## Active definition amendment across independently published audit history

An unfrozen, active registered WI uses the existing public `amend-criteria` command for an approved
criteria/title revision. A different remote HEAD is not itself permission to rewrite either history.
The original Broker request may bind its existing remote-authority field only after proving that the
remote-only delta consists entirely of complete, added, same-WI `scl_git_audit_receipt/v1` records,
the local-only delta does not change `.harness`, and the current sealed SCL registration still selects
the exact source/owner/branch. Competing definition, non-audit remote content, changed source bytes,
frozen candidate, or changed authority remains a refusal. Pre-effect recovery preserves the original
request, transaction and frozen input bytes; lease renewal is not new human consent.

The amendment is projected on the exact remote authority and normally CAS-pushed with existing guards.
Unpublished business commits are not included. After publication, the same transaction may record a
deterministic, local-only two-parent metadata integration: its non-`.harness` tree equals the original
local tree, its complete `.harness` tree equals the original published tree, and both histories remain
ancestors. Existing exact fast-forward reconciliation adopts that object without force-push, reset,
worker impersonation, new execution grants, reinit, or lifecycle changes. Interruption after local
adoption retains the integration receipt and retries only the original publication/replication tail.
The SCL adopter verifies the original amendment event and updates only its effective definition;
Session, epoch, checkpoint, workflow transition, costs and business evidence remain intact.
