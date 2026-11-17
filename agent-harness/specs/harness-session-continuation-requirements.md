---
feature_name: "session-continuation"
version: "0.3.4"
status: "completed"
root_work_item: "wi_20260820_272"
successor_feature: "harness-autonomous-rollover"
successor_work_item: "wi_20260822_730"
created: "2026-08-18T00:00:00+0800"
modified: "2026-10-07T17:40:00+0800"
owner: "Pulse"
upstream_dependency:
  - "project-specs/project-architecture.md"
  - "specs/harness-main-chain.md"
  - "specs/harness-store-models.md"
  - "specs/harness-runtime.md"
external_dependency:
  - "bigbird-online/PULSE:specs/pulse-session-context-lifecycle-requirements.md"
tags: [requirements, continuation, session-lifecycle, wi-execution, human-review, context-hygiene, cost-control]
---

# Harness Session Continuation — Requirements

> Resilience correction (2026-08-29): this completed feature remains the source for continuation,
> privacy and lifecycle semantics. Its availability, ingress, storage, retry and external-delivery
> clauses are superseded by `harness-scl-resilience-requirements.md`. In particular, fail-closed
> authority admission rejects only the unsafe lifecycle/global effect; it cannot block conversation,
> local read or ordinary work against available local material. Git publication and surface
> replication are asynchronous effects, not checkpoint durability.

> Terminal publication correction (2026-09-03): new closeout uses a stable
> `phase2_closeout/v4` settlement followed by `phase2_publication_receipt/v1` so
> post-commit head/CI facts never self-reference the commit that contains them.

> **Phase 1 基线（已验证）**：为同一个 Agent 的前后两代 Session 建立可持久、可认领、可消费、可恢复的 `ContinuationContract`。
> **Phase 2 已完成并由 `wi_20260820_272` 终态验收**：根 `WorkItem` 是长期执行身份；Session 是可替换执行容器，Continuation 只是一场 session-to-session 交接事件。普通用户只操作 WI，不需要管理 `ct_*` 或 raw Session ID。
> 跨 Agent 的责任交付继续使用 `HandoffContract`；整个成果仍由一个根 `WorkItem` 承载。

## 0. 问题与边界

当前 Harness 已经有 `WorkItem`、`HandoffContract`、`SignalEvent`、scan/policy/dispatcher 和独立 outcome
验收，但没有一种对象能表达“这项工作 owner 不变，只由下一代 Session 继续”。强行复用
`HandoffContract` 会违反 `from_owner != to_owner`；每次换 Session 新建 `WorkItem` 又会制造孤儿 WI 和收口熵。

`@anchor symbol WorkItem`
`@anchor symbol HandoffContract`
`@anchor symbol SignalEvent`
`@anchor symbol VALID_HANDOFF_TRANSITIONS`

本 feature 只新增继续执行契约，不改变现有完成语义：最终 `WorkItem.completed` 仍必须经过
`awaiting_verification`、canonical outcome receipt 与 evidence gate。

`@anchor symbol WorkItemStatus`
`@anchor symbol VALID_TRANSITIONS`
`@anchor symbol transition_work_item`

### In scope

- 同 owner 的 phase / Session 继续执行对象。
- 结构化 continuation bundle、摘要预算、引用和完整性校验。
- CLI 的 create / validate / claim / release / consume / fail / supersede。
- 与 Relay `/rollover` 的 JSON/CLI 边界。
- 继续执行事件与幂等、并发、恢复语义。

### Out of scope

- 不放宽 `HandoffContract` 的 receiver inequality。
- 不用 continuation 替代跨 Agent handoff。
- 不在每次 rollover 时新建一个 `WorkItem`。
- 不自动判断“探索/设计/实施”语义边界。
- `continuation_ready`、policy opt-in、`CONTINUATION_PROFILE` 与 cost/context/quality autonomous promotion 已移至 successor `harness-autonomous-rollover` / `wi_20260822_730`。
- 不在 Phase 2 根据成本、token、context 或质量预测自动触发 rollover；该能力属于 Phase 3 shadow/suggestion 之后的独立晋级。
- 不把完整 transcript、prompt、工具日志或原始 session/thread/user id 写入 bundle。
- 不在本 feature 内实现 PULSE Relay 的物理 kill/spawn/reset。

`@anchor symbol HandoffContract`
`@anchor symbol ContinuationContract`

## 1. 用户故事

| ID | 用户故事 | 锚点 |
|---|---|---|
| US-001 | 作为交互式主 Agent，我希望把当前 phase 的最小充分状态写成 continuation，使新 Session 不读取旧 transcript 也能继续。 | `@anchor symbol ContinuationBundle` |
| US-002 | 作为 Harness dispatcher，我希望只认领一次 `ready` continuation，避免两次 autonomous run 同时接班。 | `@anchor symbol acquire_lock` `@anchor symbol claim_continuation` |
| US-003 | 作为跨 Agent 协作者，我希望 owner 变化仍走 `HandoffContract`，不让 continuation 偷换责任。 | `@anchor symbol HandoffContract` |
| US-004 | 作为结果责任人，我希望 rollover 不改变根 WI 的 owner、status 或最终验收要求。 | `@anchor symbol WorkItem` `@anchor symbol transition_work_item` |
| US-005 | 作为成本观察者，我希望 continuation、phase、run 可以稳定关联，但不暴露原始 Session 身份。 | `@anchor manual privacy-safe lineage :: 检查 bundle 与事件只含 session_v1_/lineage_v1_ 单向引用，不含原始 session/thread/user id` |

## 2. 功能需求

### P0：持久契约与手工 dogfood

| ID | 契约声明 | 锚点 |
|---|---|---|
| FR-P0-001 | 系统必须新增 `ContinuationContract`，且它必须由一个现存 `WorkItem`、一个 `agent_id`、`from_phase`、`to_phase`、`bundle_ref`、`bundle_sha256` 和状态组成。 | `@anchor symbol ContinuationContract` `@anchor symbol WorkItem` |
| FR-P0-002 | `ContinuationContract` 必须只表达同 owner 接班；它不得修改 `WorkItem.owner`，也不得包含不同的 from/to owner。 | `@anchor symbol apply_handoff_acceptance` `@anchor symbol ContinuationContract` |
| FR-P0-003 | 状态必须限定为 `ready`、`claimed`、`consumed`、`failed`、`superseded`；`consumed`、`failed`、`superseded` 必须是终态。 | `@anchor symbol ContinuationState` |
| FR-P0-004 | 合法迁移必须为 `ready→claimed`、`ready→superseded`、`claimed→consumed`、`claimed→ready`、`claimed→failed`、`claimed→superseded`；其他迁移必须失败。 | `@anchor symbol VALID_CONTINUATION_TRANSITIONS` |
| FR-P0-005 | `claimed→ready` 必须增加 `attempt` 并清除 claimant；系统不得用时钟自动释放 claim。 | `@anchor symbol release_continuation` |
| FR-P0-006 | continuation 必须存放于 `.harness/continuations/{continuation_id}.yaml`，bundle 必须存放于 `.harness/evidence/continuations/{continuation_id}.yaml`。 | `@anchor symbol HarnessStore` `@anchor symbol save_continuation` |
| FR-P0-007 | bundle 必须采用 `continuation_bundle/v1`，至少包含 objective、acceptance criteria、confirmed facts、decisions、artifacts、current state、open questions、risks 与 next action。 | `@anchor symbol ContinuationBundle` |
| FR-P0-008 | bundle 的 UTF-8 原始字节必须不超过 `32768` 字节；超过预算必须失败，长材料必须改为引用。 | `@anchor symbol MAX_CONTINUATION_BUNDLE_BYTES` |
| FR-P0-009 | `bundle_sha256` 必须对 bundle 文件的精确 UTF-8 字节计算；内容改变而 contract 未更新时 validate 必须失败。 | `@anchor symbol validate_continuation_bundle` |
| FR-P0-010 | artifact 引用必须使用 `repo + path + revision + sha256`，路径必须为仓库相对路径且不得包含 `..` 或绝对路径。 | `@anchor symbol ContinuationArtifactRef` |
| FR-P0-011 | bundle 不得包含完整 transcript、prompt、原始工具日志、secret 或原始 session/thread/user id；Session 关联必须使用 `session_v1_` 或 `lineage_v1_` 单向引用。 | `@anchor manual continuation privacy scan :: 用固定 fixture 注入 transcript/prompt/绝对路径/原始 UUID/secret，validator 必须逐项拒绝；正例只保留单向引用` |
| FR-P0-012 | 系统必须提供 `harness continuation {create,validate,show,claim,release,consume,fail,supersede}`，所有写操作必须产生 `SignalEvent`。 | `@anchor symbol continuation_command` `@anchor symbol SignalEvent` |
| FR-P0-013 | create 必须拒绝不存在或已终态的根 WI；continuation 本身不得把根 WI 推进、完成或改 owner。 | `@anchor symbol HarnessStore` `@anchor symbol WorkItemStatus` `@anchor symbol create_continuation` |
| FR-P0-014 | claim 必须是原子的；同一 continuation 在任一时刻最多只能有一个 `claimed_run_ref`。 | `@anchor symbol acquire_lock` `@anchor symbol claim_continuation` |
| FR-P0-015 | 同一 `work_item_id + from_phase + to_phase + bundle_sha256 + attempt` 的重复 create 必须返回同一 continuation，不得生成第二份可执行事实。 | `@anchor symbol continuation_identity` |

### Phase 3 deferred：scan / policy / autonomous continuation（Phase 2 不启用）

| ID | 契约声明 | 锚点 |
|---|---|---|
| FR-P1-001 | `ScanHit.hit_type` 与 `ScanItemType` 必须新增 `continuation_ready`；未在 policy 声明时 collector 必须完全关闭。 | `@anchor symbol ScanHit` `@anchor symbol ScanItemType` `@anchor absent list_ready_continuations_for` |
| FR-P1-002 | collector 必须只返回 `agent_id` 匹配且 state=`ready` 的 continuation；不得读取或复制 bundle 正文到 `ScanHit`。 | `@anchor symbol ScanReport` `@anchor absent list_ready_continuations_for` |
| FR-P1-003 | 一个 autonomous spawn 必须只处理一个 continuation；同一扫描中的后续 continuation 必须记为 `deferred_serialization`。 | `@anchor symbol spawn_autonomous_session` `@anchor absent CONTINUATION_PROFILE` |
| FR-P1-004 | dispatcher 必须在 spawn 前 claim；若 spawn 未启动或在消费前失败，必须 release；若新 run 已消费 continuation，后续业务失败不得复活旧 claim。 | `@anchor symbol spawn_autonomous_session` `@anchor symbol claim_continuation` `@anchor symbol consume_continuation` |
| FR-P1-005 | `CONTINUATION_PROFILE` 必须只注入 bundle、当前 phase 验收、必要权限和结构化 consume 契约；不得注入 legacy 全量 prompt、完整近期历史或旧 transcript。 | `@anchor symbol ContextProfile` `@anchor symbol _build_prompt` `@anchor absent CONTINUATION_PROFILE` |
| FR-P1-006 | autonomous run 的第一项 durable 协作动作必须是 `harness continuation consume`；未消费却产生业务写入必须判失败。 | `@anchor symbol consume_continuation` `@anchor manual first-action ordering :: fixture 记录工具顺序，consume 必须先于业务文件写入与 WI/HO 状态修改` |
| FR-P1-007 | continuation 的 spawn、consume、release、failure 必须进入现有 `DispatchEvent` / `AutonomousExecutionEvent` 观测链，不得建立第二套调度账本。 | `@anchor symbol DispatchEvent` `@anchor symbol AutonomousExecutionEvent` |

### P2：Relay seam

| ID | 契约声明 | 锚点 |
|---|---|---|
| FR-P2-001 | Harness 必须提供稳定 JSON 输出，使 Relay 能 validate、show、consume、release continuation；Relay 不得直接编辑 `.harness` YAML。 | `@anchor symbol continuation_json_contract` |
| FR-P2-002 | Relay 物理切换成功只意味着 continuation 被消费，不意味着根 WI 或 next phase 已完成。 | `@anchor symbol WorkItemStatus` `@anchor symbol consume_continuation` |
| FR-P2-003 | owner 改变时必须停止 continuation 流程并改用 `harness handoff`。 | `@anchor symbol HandoffContract` |

## 3. 非功能需求

| ID | 契约声明 | 锚点 |
|---|---|---|
| NFR-001 | 所有 continuation 写入必须使用临时文件 + `os.replace`，并在 per-continuation lock 内完成。 | `@anchor symbol HarnessStore` `@anchor symbol save_continuation` |
| NFR-002 | 读取旧仓数据时 absence of `continuations/` 必须等价于空集合，不能破坏现有 scan/CLI。 | `@anchor symbol HarnessStore` |
| NFR-003 | scan 必须保持 metadata-only；bundle 正文不得进入 scan event、dispatch event 或 escalation message。 | `@anchor symbol ScanEvent` `@anchor symbol DispatchEvent` |
| NFR-004 | CLI 与 validator 的错误必须区分 contract invalid、state conflict、lock conflict 与 IO/system error。 | `@anchor symbol ContinuationError` |
| NFR-005 | feature 默认关闭；只有 agent policy 显式声明 `continuation_ready` 时 autonomous chaining 才能启用。 | `@anchor symbol ScanItemPolicy` |
| NFR-006 | 本 feature 不新增 pip 依赖，复用 Python、Pydantic、Click、PyYAML 和现有 lock/store。 | `@anchor symbol HarnessStore` `@anchor symbol ScanItemPolicy` |
| NFR-007 | continuation 事件必须可由 `work_item_id`、`continuation_id`、`agent_id` 与 hashed run/session refs 关联。 | `@anchor symbol SignalEvent` `@anchor symbol ContinuationContract` |

## 4. 验收标准

| ID | 验收 | 锚点 |
|---|---|---|
| AC-P0-001 | create 正例落两份文件，validate 通过；篡改 bundle 1 字节后 validate 必须失败。 | `@anchor test test_continuation_digest_tamper_fails` |
| AC-P0-002 | 不存在、completed、failed 的 WI 均不得 create continuation；open WI 正例不改变 owner/status。 | `@anchor test test_continuation_requires_open_work_item` |
| AC-P0-003 | 两个并发 claimant 只能一个成功，失败方必须得到明确 conflict，不得覆盖 claimant。 | `@anchor test test_continuation_claim_is_single_winner` |
| AC-P0-004 | 反复 create 相同事实只得到一个 ID；bundle digest 不同必须得到不同 ID 或 supersede 关系。 | `@anchor test test_continuation_identity_is_idempotent` |
| AC-P0-005 | privacy fixtures 中 transcript、prompt、绝对路径、原始 UUID 和 secret 均被拒；合法引用通过。 | `@anchor test test_continuation_bundle_rejects_private_material` |
| AC-P0-006 | 手工 dogfood 至少完成 `3` 次真实同-owner Session 接班；每次新 Session 不读旧 transcript，均能说出 objective、全部 acceptance criteria 和 first action。 | `@anchor manual three-rollover-dogfood :: 保存三次 continuation、fresh-session 回答、用户纠错与读取记录；逐次确认未访问旧 transcript` |
| AC-P1-001 | policy 未声明时 scan 0 个 continuation hit；声明后同一 ready contract 恰好产生 1 个 hit。 | `@anchor absent test_continuation_scan_is_opt_in` |
| AC-P1-002 | 两个 ready continuation 同轮出现时只 spawn 第一个，第二个 disposition 为 `deferred_serialization`。 | `@anchor absent test_continuation_dispatch_serializes` |
| AC-P1-003 | spawn 前失败会 release；consume 后业务失败不会把 contract 改回 ready。 | `@anchor absent test_continuation_release_before_consume_only` |
| AC-P1-004 | continuation profile 的 prompt fixture 不含 legacy anti-pattern 大段、recent commits 或旧 transcript，只含允许字段。 | `@anchor absent test_continuation_profile_is_minimal` |
| AC-P2-001 | Relay 通过 CLI JSON 完成 validate→consume，期间没有直接写 YAML；错误码与状态冲突可区分。 | `@anchor manual relay-harness-contract-uat :: 在 PULSE 隔离测试中替换 harness CLI，核 argv/stdout/stderr/exit code 与 `.harness` diff` |


## 5. Phase 2 — WI Execution Control Plane

### 5.1 语义不变量

| ID | 契约声明 | 锚点 |
|---|---|---|
| FR-PH2-001 | `WorkItem` 是跨 Session 的长期工作身份；Session 是可替换执行容器；`ContinuationContract` 只描述一次 source→target Session 交接事件。换 Session 不得新建 WI，也不得把 continuation 当成 WI 当前状态。 | `@anchor symbol WorkItem` `@anchor symbol ContinuationContract` |
| FR-PH2-002 | Phase 2 必须新增 durable `WorkItemExecutionBinding`（或等价模型），至少记录 `work_item_id`、`agent_id`、`epoch`、`phase`、`runtime`、opaque `session_ref`、optional `continuation_id`、`lifecycle_state`、checkpoint revision/timestamps 与 created/updated timestamps。 | `@anchor symbol WorkItemExecutionBinding` |
| FR-PH2-003 | binding 必须以 WI 为唯一当前键持久化，例如 `.harness/executions/{work_item_id}.yaml`；旧仓 absence 必须等价于“尚未绑定”，不得破坏现有 WI/HO/continuation CLI。 | `@anchor symbol WorkItemExecutionBinding` `@anchor symbol HarnessStore` |
| FR-PH2-004 | binding/checkpoint/review/event 中禁止 raw Session/thread/user ID；只允许 domain-separated opaque ref。Relay-private resume material 不得进入 Harness。 | `@anchor symbol WorkItemExecutionBinding` `@anchor manual phase2 execution privacy :: 负例注入 raw session/thread/user id、绝对路径、prompt/secret；binding/checkpoint/review/event validator 全部拒绝` |
| FR-PH2-005 | `epoch` 只在 fresh target 已通过 strict ACK 且对应 continuation 已 consume 后递增；ACK/consume 失败并恢复 source 时 epoch 不变。 | `@anchor symbol apply_execution_rollover` `@anchor symbol consume_continuation` |
| FR-PH2-006 | `checkpoint` 必须只固化当前 WI execution state：分配 **WI 内单调递增且永不回绕/复用** 的 checkpoint revision、更新时间与最小 `ExecutionCheckpoint`；不得 kill/spawn Session、不得递增 epoch、不得创建伪 continuation。rollover/adopt 只可清除 current-epoch `latest_checkpoint_*` pointer，不得把 revision 序号归零或覆盖历史 checkpoint 文件。 | `@anchor symbol ExecutionCheckpoint` `@anchor symbol checkpoint_execution` |
| FR-PH2-007 | `ContinuationContract` 只能在确实需要 fresh Session transfer 时由 checkpoint/current binding 派生；checkpoint 与 rollover 是两个独立动作。 | `@anchor absent create_execution_continuation` `@anchor symbol ContinuationContract` |
| FR-PH2-007A | WI-centric `scl_local_resume_packet/v1` 必须保真投影 checkpoint 的有序 artifact refs；fresh continue、applied-successor Host rebind 与 verified read-only projection 均须保留每项 `repo + path + revision + sha256 + role`。只恢复 objective/AC/decisions/next action 而遗漏 artifacts 不得算完整接续。 | `@anchor symbol LocalResumePacket` `@anchor test test_reinit_rollover_continue_restores_wi282_shaped_frontier_and_authority` |
| FR-PH2-008 | 每个 WI 的 binding 写操作必须 single-winner；重复 checkpoint/continue/rollover 请求必须按 durable operation identity 幂等，同一 WI 不得并发产生两个 authoritative target epoch。 | `@anchor absent execution_identity` `@anchor symbol acquire_lock` |

### 5.2 WI-centric API

| ID | 契约声明 | 锚点 |
|---|---|---|
| FR-PH2-009 | Phase 2 必须提供 WI-centric `status / continue / checkpoint / rollover / review`（CLI 或等价稳定路由）。普通用户输入只需要 WI；`ct_*` 可作为内部/诊断输出，但不得成为必填用户参数，raw Session ID 永远不得成为输入。 | `@anchor symbol register_execution_runtime_commands` `@anchor symbol review_command` |
| FR-PH2-010 | `status` 必须返回 WI lifecycle、binding epoch/phase/runtime/lifecycle、opaque session ref、latest checkpoint、pending continuation 与 review state；不得复制 checkpoint/bundle 正文。 | `@anchor absent show_execution_status` |
| FR-PH2-011 | `continue` 必须从 durable binding + runtime evidence 选择 `reuse_current`、`resume_current_epoch` 或 `fresh_from_checkpoint`；不得让模型凭记忆猜当前 Session。缺可恢复 source 且无有效 checkpoint 时必须 fail closed。仍使用旧 execution binding 的 Git-resident dormant execution，其注册 WI worktree 若含未提交变更，fresh admission 必须保持 clean gate 并返回稳定 `worktree_dirty` 与 canonical settlement：保留并 commit/push 预期 WI 变更（含 repository-resident Harness ledger），将 exact-head review 重锚到新 SHA 后重试；不得 stash/reset/clean、忽略全部 `.harness` dirty 或宣称可直接 continue。已采用 SCL 当前有效授权的正常继续及 reinit/注册恢复不适用此旧 clean gate，应按 harness-scl-resilience 保留 dirty 成果并核验实际权限。 | `@anchor symbol resolve_execution_continue` `@anchor test test_entry_checkpoint_and_dormant_keep_shared_checkout_clean` |
| FR-PH2-012 | `rollover <wi>` 必须内部解析/创建 continuation，复用 Phase 1 strict ACK + consume/recovery；不得要求普通用户先查 continuation ID。 | `@anchor symbol prepare_execution_rollover` `@anchor symbol consume_continuation` |
| FR-PH2-013 | Phase 1 `harness continuation ...` 保留为底层 primitive/诊断面；Phase 2 不得删除或改写其既有 create/validate/claim/release/consume 语义。 | `@anchor symbol continuation_command` |

### 5.3 Human review terminal gate

| ID | 契约声明 | 锚点 |
|---|---|---|
| FR-PH2-014 | Phase 2 必须新增 durable `WorkItemReviewDecision`（或等价记录），至少包含 WI、reviewer、decision=`approved|changes_requested`、AC fingerprint、candidate fingerprint、evidence refs 与 reviewed_at。Agent 自报完成、continuation consumed、tests/CI green 均不得自动生成 `approved`。 | `@anchor symbol WorkItemReviewDecision` |
| FR-PH2-015 | `candidate_fingerprint` 必须绑定送审的 exact candidate（至少 AC fingerprint + repository-relative artifact ref 与其精确文件内容 SHA-256 的 artifact-set digest + 相关 repo full head revisions）；送审后 candidate 任一 revision/AC/artifact path/content 改变，旧 approval 必须失效并要求重新 review。artifact 缺失、不可读、symlink 或无法安全归一到 repo root 时必须 fail closed。 | `@anchor symbol review_candidate_fingerprint` |
| FR-PH2-016 | Terminal-controlled WI 包括显式 Phase 2 execution binding、`main_only=true` 或存在 canonical sealed-SCL audit receipt 的 WI。其 Outcome accepted 只能保持 `awaiting_verification`，不得直接写 `completed` 或自动归档 handoff；随后必须在 canonical receipt/evidence gate 之外，机械证明 current candidate human review=`approved`、reviewed revision 已进入 integration revision 且进入当前 default-branch HEAD、固定结算发布提交的 Actions CI completed/success（后继 main 仅含其它 WI 独立账本修改时按 FR-PH2-016B2 复用）、适用 smoke 的 exact-head run PASS 或对象级 N/A、临时 branch/worktree/Broker projection 已按受支持机制结清。closeout YAML 自报 SHA/CI/smoke PASS 不能替代 authority；查询不可用时 fail closed。仅对既无 execution binding、非 main-only、也无 sealed-SCL audit 的真正 legacy WI 保持既有 instant-completion 兼容语义。 | `@anchor symbol terminal_gate_source` `@anchor symbol validate_execution_closeout` |
| FR-PH2-016A | Open Phase-2 closeout 必须从 PULSE versioned surface registry 派生 canonical、全部 resident surface 和 Stats read-model consumer；默认读取规范仓库 config/scl-surfaces.yaml，稀疏检出读取同一 HEAD 原件，外部部署可显式覆盖位置，不要求普通调用方传环境变量。settlement 不能选择本地来源，必须绑定 registry repo/path/SHA-256；Harness 对同一份捕获字节检查义务完整性与 current-default Git blob。遗漏、额外 consumer、降低 requirement、改目标、原件缺失/替换或路径畸形均拒绝，不退回 producer list。 | `@anchor symbol parse_authoritative_consumer_inventory` `@anchor symbol validate_consumer_inventory` |
| FR-PH2-016B | 新 closeout 必须使用两阶段合同：repository-relative `phase2_closeout/v4` settlement 只承载 reviewed/integration revision、consumer convergence 与 cleanup/probe/projection exact digest，不得包含依赖其自身 commit 的 resulting head 或 CI/smoke run ids；该 settlement 以相同 Git blob 发布后，repository-relative `phase2_publication_receipt/v1` 再绑定 settlement ref/SHA-256、各 repo 已验证的固定 resulting head 与 exact-head CI/smoke run ids。Harness 必须读回 settlement Git blob、实时 current head/ancestry/Actions authority 及 FR-PH2-016B2 后继输入不变证明，并把 receipt ref/digest 写入唯一 terminal event。receipt 不必预先存在于其描述的 commit；缺失、branch-local settlement、temporary/deleted/symlink/escaped evidence、digest drift、repo omission 或非 exact replay 均 fail closed。v3 仅保留读取兼容，不再作为新 closeout 生成格式。 | `@anchor symbol Phase2CloseoutEvidence` `@anchor symbol Phase2PublicationReceipt` `@anchor symbol validate_execution_closeout` |
| FR-PH2-016B1 | 已冻结且 accepted/approved 的 CWA-only main_only WI 必须可通过 registered service authority 发布 settlement，无需 executor、reopen、reinit 或新 review。Host prepare 捕获材料并生成 portable bundle；publish 仅写 exact WI evidence closure，重验 candidate/approval/Outcome/ancestry；complete 自动绑定 Host publication receipt 和现有 Outcome 后调用既有 exact-head terminal validator。缺失参数或外部 CI 必须返回可操作原因，禁止吞掉失败详情。 | `@anchor symbol SettlementBundle` `@anchor symbol publish_bundle` |
| FR-PH2-016B2 | 结算发布后，无关 WI 推进 main 不得使原精确 CI 无条件失效。允许复用的最小边界：tested publication 仍为当前 default HEAD 祖先；两端完整 Git tree 证明差异仅为其它 WI 独立命名空间内的普通 `.harness` 文件。源码、workflow、共享 policy、当前 WI、无法归属的 handoff 或不完整 tree 均拒绝复用；现有 candidate、approval、artifact 与 evidence closure 校验仍执行。CI 必须仍匹配固定 tested SHA、default branch、completed/success；允许同名仓库 workflow 的 workflow_dispatch，但必须核对该 SHA 的原 workflow 同时声明 push/无输入 dispatch、无事件分支、静态完整 job 集合全部成功。其它 probe、fork、跳过 job、动态/复用 workflow 或事件相关子集不视为等价。Host 必须选择原成功结算版本，不再循环改写为最新 main；CAS 冲突沿同一结算重读，不能另造候选或批准。 | `@anchor test test_unrelated_ledger_successor_keeps_exact_successful_ci` `@anchor test test_same_workflow_manual_run_covers_same_jobs` `@anchor test test_changed_tested_source_or_current_wi_still_rejected` |
| FR-PH2-016C | Phase-2 terminal settlement 在 WorkItem terminal write 之前，将 execution binding 单向转为 `terminal` 并绑定 settlement ref/digest。中间写失败只允许相同 settlement replay；entry/resume/checkpoint/rollover 不得重新激活。完成态不得遗留 active binding。 | `@anchor symbol retire_for_terminal_settlement` `@anchor const TERMINAL = "terminal"` |
| FR-PH2-016D | Terminal cleanup 发现 `prepared`/`pushed` Harness Broker projection 时不得 `rm`、prune transaction 或伪造 completed。只允许在人类明确确认、canonical authority branch 的 WI 已 terminal、worktree clean 且 HEAD 等于 prepared SHA、authority 继承 base，并且 pushed SHA 已进入 authority history 后，通过 Broker retirement 写 durable receipt、由 Git 移除 exact projection worktree，再将 transaction 单向置 `retired`。`discovered`/`recovery_required` 仅在机械证明从未 prepared/published、无 integration/replication/endpoint effect、登记 worktree 物理与 Git inventory 均不存在后，才可由同一命令写 `projection_effect=none` receipt 并退休；未知即 fail closed。 | `@anchor symbol retire_harness_projection` `@anchor const RETIRED = "retired"` |
| FR-PH2-016E | Terminal WI 的 stale site-local workspace registration 不得手删。仅在人类明确确认、WI terminal 且无 active executor、每个登记 worktree 与 exact local/remote WI branch 均已证明不存在、remote identity 仍精确可读后，才可通过 `worktree retire-terminal` 将 byte-identical manifest 按 digest 归档、写 `work_item_workspace_retirement/v1` receipt 并移除 exact active manifest。symlink、digest drift、路径/ref 仍存或 remote truth unknown 均 fail closed。 | `@anchor symbol retire_terminal_registration` `@anchor command worktree retire-terminal` |
| FR-PH2-016F | managed SCL WI 因错误创建而处于 `main_only=true`、`in_progress`、owner-claimed，但 execution binding 与 current SCL fence 均确证不存在时，只允许显式单值 `disposition --to cancelled` 使用 canonical service authority。入口必须机械证明唯一 regular registration、SCL/canonical owner 一致、SCL surface 非空时也与 registration 一致、exact binding `not_found`、clean registered worktree/branch/remote/base ancestry；proof 进入 authority fingerprint，并在初次 convergence 后、Broker issue 前完整重验。真实 unbound SCL frontier 的 runtime/surface/session 可为 null，不得因此要求 reinit 或伪造 attachment。existing/expired/conflicting fence、任意 binding、owner mismatch、registration ambiguity、其它 disposition 或其它 command 均 fail closed；必须先提交 cancellation，再走受支持的物理 cleanup。 | `@anchor test test_claimed_orphan_cancel_runs_real_managed_cli_publisher_and_convergence` `@anchor symbol managed_claimed_orphan` |
| FR-PH2-017 | `review` 必须是显式 human-originated action；cron/autonomous Session 不得 approve。Phase 2 只要求可审计的人类动作边界，不宣称提供密码学身份认证。 | `@anchor symbol review_command` `@anchor manual human review boundary :: autonomous/cron approve 被拒；interactive human action 可记录；任何模型输出字符串不能直接置 approved` |

### 5.4 Observe / evidence reconstructability seam

| ID | 契约声明 | 锚点 |
|---|---|---|
| FR-PH2-018 | `WorkItemExecutionBinding`、checkpoint 与 lifecycle `SignalEvent` 必须保留后续机械 join 所需的稳定键：`work_item_id`、`agent_id`、`epoch`、`phase`、`runtime`、opaque `session_ref`、optional `continuation_id` 与时间戳；不得复制 runtime/usage/review/merge 的正文事实。 | `@anchor symbol WorkItemExecutionBinding` `@anchor symbol SignalEvent` |
| FR-PH2-019 | Phase 2 不得新增独立 Execution Evidence ledger。`wi_20260820_272` UAT 只保存既有 producer 的 ref/digest/source boundary；没有权威 producer 或无法稳定 join 的 runtime model/effort、usage、route snapshot、human-attention 等事实必须标记 `unknown`，不得把 absence 写成 `0` 或从当前配置反推历史。 | `@anchor manual wi272-evidence-harvest-boundary :: 不读 transcript/参与者回忆；逐域记录 authority source/ref/digest 或 unknown，检查 absence!=zero` |
| FR-PH2-020 | execution binding 的 `lifecycle_state` 只描述“哪个 Session/epoch 当前有执行权以及是否在切换/恢复”，不得复制 `WorkItemStatus`、review verdict 或 outcome 状态；业务完成/失败仍只由根 WI + canonical outcome/review gate 决定。 | `@anchor symbol WorkItemStatus` `@anchor symbol WorkItemExecutionBinding` |
| FR-PH2-021 | UAT closeout 必须留下可供上层 `wi_20260820_079` 机械 harvest 的 source index：至少覆盖 lifecycle、runtime、continuation、candidate/review、outcome、merge/default verification；无法机械证明的域显式 `unknown`。该 index 是 evidence artifact，不是 SSOT。 | `@anchor manual wi272-observe-source-index :: 用现有 producer 生成 source/ref/digest/authority/known-status 表；不得创建第二状态机/ledger` |
| FR-PH2-022 | Phase 2 必须优雅支持 **adopt/bind existing fresh Session to WI**：当用户已经位于一个 fresh execution Session/surface、目标 WI 存在且当前无 execution binding 时，WI-centric entry 必须把这个现有 Session 采纳为 authoritative execution container，创建 epoch=1 binding，并继续 durable WI；该 adopt 不得创建 continuation、不得递增 epoch、不得读取旧 transcript。若已有匹配当前 Session 的 active binding 则幂等 reuse；若当前 binding 已显式 dormant 且存在 valid durable checkpoint，则 fresh external Session 可采纳为新的 opaque execution container，保持同一 epoch、从 source checkpoint 启动、不得创建 continuation，并清除仅属于 source Session 的 current-checkpoint pointer（历史 checkpoint 文件保留审计）；dormant 但无 checkpoint 必须 fail closed。若已有其他 runtime/active Session authority、transitioning 或 recovery-required，则必须 reconciliation/fail closed，禁止静默抢占。ChatGPT+GitHub bare `/wi <wi>` 是该路径的首个真实 dogfood adapter，且不得要求用户提供 raw Session ID。 | `@anchor symbol ExecutionSurfaceManager` `@anchor symbol surface_command` |
| FR-PH2-023 | `chatgpt_web_github` dogfood segment 必须旁路记录 `execution_surface=chatgpt_web_github`、canonical `cash_basis=same_chatgpt_pro_monthly_subscription`、entry 后 visible user/assistant turns、`visible_text_reference_v1` reference tokens、GitHub operations/commit/PR/review/CI counters、WI/epoch/phase/session/continuation lineage、productive/recovery/verification/adjudication、outcome/human-correction/rework。provider raw Session identity、actual model tokens 与 per-WI Pro cash allocation 不可见时必须显式 unavailable/unknown；不得保存可见消息正文。该 observation 是 source evidence，不是第二 execution/usage ledger。 | `@anchor symbol ExecutionSurfaceObservation` `@anchor test test_surface_observe_records_visible_reference_and_github_facts_only` |

### 5.5 Phase 2 非功能与兼容性

| ID | 契约声明 | 锚点 |
|---|---|---|
| NFR-PH2-001 | binding/checkpoint/review 必须 atomic write；每 WI lock 覆盖 checkpoint、rollover prepare/apply/recover 与 review-closeout 竞态。terminal settlement 采用显式 crash-safe ordering：先持久化 digest-bound terminal authority retirement，再写 WorkItem terminal event；普通 I/O failure 回滚，进程中断留下可重放而不可恢复执行权的合法状态。 | `@anchor symbol HarnessStore` `@anchor symbol ExecutionManager` |
| NFR-PH2-002 | Harness/Relay 任一重启后，必须用 binding + continuation truth + Relay persisted rollover state 做 reconciliation；不确定时不得同时授权两个 Session。 | `@anchor absent reconcile_execution_binding` |
| NFR-PH2-003 | 所有 Phase 2 lifecycle 事件继续写入现有 `SignalEvent` 链，至少可按 WI、epoch、continuation 与 opaque session ref 关联；不得建第二事件账本。 | `@anchor symbol SignalEvent` `@anchor symbol WorkItemExecutionBinding` |
| NFR-PH2-004 | `/new`、`/compact`、Phase 1 `/rollover <ct>` 以及真正 legacy（无 execution binding、非 main-only、无 sealed-SCL audit）的 `ready-for-uat → verify-result/complete` 必须回归等价；兼容性不得绕过 terminal-controlled WI 的 review/cleanup gate。 | `@anchor symbol continuation_command` `@anchor symbol terminal_gate_source` |
| NFR-PH2-005 | Phase 3 cost/quality-aware autonomous rollover 在本阶段只允许 lineage/shadow 观察；不得依据 token/context/cost 自动触发 rollover。 | `@anchor absent CONTINUATION_PROFILE` `@anchor manual phase3-default-off :: 核 policy/feature flag 无自动 rollover enablement` |

### 5.5 Phase 2 验收

| ID | 验收 | 锚点 |
|---|---|---|
| AC-PH2-001 | legacy 无 execution binding 的 WI 正常加载；首次 bind 后字段完整，raw Session ID/privacy fixture 全拒。 | `@anchor test test_execution_binding_is_backward_compatible_and_private` |
| AC-PH2-002 | checkpoint 前后 epoch/session ref 不变，仅 checkpoint revision/timestamp/state artifact 前进；重复同一 operation 不产生第二 checkpoint；跨 rollover 后下一 checkpoint 必须继续使用 WI-wide 下一 revision，旧 checkpoint digest 不变，即使读取到 legacy counter-rewind 状态也不得覆盖历史文件。 | `@anchor test test_execution_checkpoint_does_not_rollover` `@anchor test test_checkpoint_allocator_preserves_history_after_legacy_counter_rewind` |
| AC-PH2-003 | continue 对 live/resumable/no-session+checkpoint 三种 fixture 分别选择 reuse/resume/fresh；无 checkpoint 且不可恢复时 fail closed。仍使用旧 execution binding 的真实 Git-resident fixture 在 checkpoint+mark-dormant 产生 ledger dirty 后必须拒绝 fresh admission，CLI JSON 保留 `worktree_dirty`、专用 exit code 与 canonical settlement 文案，binding 仍为 dormant。 | `@anchor test test_execution_continue_resolves_from_durable_truth` `@anchor test test_entry_checkpoint_and_dormant_keep_shared_checkout_clean` |
| AC-PH2-004 | fresh rollover 成功后 epoch 精确 +1；ACK mismatch/consume failure 恢复 source 后 epoch 不变；restart reconciliation 不再 spawn 第二 target。 | `@anchor test test_execution_rollover_epoch_and_recovery` |
| AC-PH2-005 | 两个并发 `rollover <wi>` 只有一个创建/消费 fresh target；duplicate in-flight request 返回同一 operation/当前状态。 | `@anchor test test_prepare_rollover_concurrent_requests_have_single_winner` |
| AC-PH2-006 | human approval 绑定 exact candidate；candidate head 或 AC 改 1 项即 approval stale；无 current approval 时 Phase 2 closeout/complete 必须失败，legacy complete 回归通过。 | `@anchor test test_human_review_fingerprint_gates_closeout` |
| AC-PH2-007 | dogfood WI `wi_20260820_272` closeout 必须机械要求且仅接受一份 approval 后生成的 `s1_measurement_pilot.v1` projection；schema 对齐已 adopt 的 `wi_20260820_028` G0：result cycle 从首次真实 `execution_started_at` 到 `delivery_accepted_at`，post-approval correction 只接受成熟预声明观察窗 + authority producer，缺 producer/join/window 必须 unknown/unavailable；human attention 同时保留 raw human turns 与按 human obligation 的 HAU v1（micro=0.25/bounded=1/judgment=2/framing=4）；每项读数携带 window/as_of/eligible+observed denominator/coverage/status/value/unknown_count/source_refs。projection 不得进入 reviewed artifact set，因此不得改变 candidate fingerprint。 | `@anchor symbol S1MeasurementPilot` `@anchor manual wi272-s1-measurement-pilot :: post-approval consumer projection, non-SSOT` |
| AC-PH2-008 | 在 ChatGPT+GitHub dogfood 首次 bare `/wi wi_20260820_272` 时，真实 path 必须从 unbound WI 创建 epoch=1 `chatgpt_web` binding、返回 `reuse_current`、checkpoint absence 明确为 none，并创建 surface observation；后续可见 turn/GitHub activity 只累计 entry 后 coverage，actual model tokens/provider session/per-WI Pro cash 不得伪造。 | `@anchor test test_surface_enter_binds_unbound_wi_to_chatgpt_web_epoch_one` `@anchor test test_surface_cli_enter_then_observe_round_trip` |
| AC-PH2-009 | `wi_20260820_272` 自身至少跨 2 次 real fresh Claude Code Session rollover；target 不读旧 transcript、不需人类重述 objective/AC/first action，并记录 epoch、成本、纠错与返工 evidence。 | `@anchor manual wi272-two-fresh-rollovers :: 保存两次 binding/checkpoint/continuation/ACK、source transcript 不可读证据、first action、cost/correction/rework 读数` |
| AC-PH2-010 | restart、duplicate、concurrency、ACK/recovery、privacy 以及现有 `/new`/`/compact`/`/rollover <ct>` compatibility 都有真实执行的自动化测试，不得靠弱化 validator 或只测 helper。 | `@anchor manual phase2-real-path-test-matrix :: 保存测试命令、真实 runtime seam、失败注入与回归输出` |
| AC-PH2-011 | `wi_20260820_272` closeout evidence 在不读旧 transcript、不依赖参与者回忆的条件下，能够按现有 source refs 尝试重建 WI→epoch/phase→continuation→runtime/session-ref→candidate/review→outcome→merge/default verification；任何无法机械证明的 model/effort/usage/route/human-attention 项保持 `unknown`，并可直接作为 `wi_20260820_079` Evidence Harvest 输入。 | `@anchor manual wi272-observe-source-index :: 保存机械 source matrix seed、unknown 列与 false-zero 检查` |
| AC-PH2-012 | 两阶段 terminal publication 测试必须先把不含 future head/run ids 的 v4 settlement 提交到真实临时 Git，再用提交后取得的真实 HEAD 生成 publication receipt 并完成；缺 receipt、settlement digest 篡改与非 exact completed replay 均拒绝，且只产生一个 terminal event。 | `@anchor test test_phase2_two_stage_publication_uses_post_commit_receipt` |

## 6. 不做

- 不自动从 token 百分比宣布 phase 完成。
- 不在 P0 支持 Codex thread rollover。
- 不允许 continuation 越过 owner 边界。
- 不要求普通 rollover 运行完整七格独立 outcome UAT。
- 不删除、改写或回填现有 WI、HO、event 历史。
