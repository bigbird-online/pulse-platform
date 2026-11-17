---
feature_name: "session-continuation"
version: "0.2.1"
status: "completed"
root_work_item: "wi_20260820_272"
successor_feature: "harness-autonomous-rollover"
successor_work_item: "wi_20260822_730"
created: "2026-08-18T00:00:00+0800"
modified: "2026-08-22T00:00:00+0800"
owner: "Pulse"
upstream_dependency:
  - "specs/harness-session-continuation-requirements.md"
  - "specs/harness-session-continuation-design.md"
external_dependency:
  - "bigbird-online/PULSE:specs/pulse-session-context-lifecycle-tasks.md"
tags: [tasks, continuation, wi-execution, checkpoint, human-review, rollout, migration]
---

# Harness Session Continuation — Tasks

## 1. 风险与当前切片

- Feature risk：**high-risk**。它最终会影响共享 `.harness` 状态、autonomous dispatcher 与人机 Session 切换。
- Phase 1：**explicit continuation + Relay rollover 已完成并合并**；3/3 real fresh CC closeout 证据保持为基线。
- 当前切片：**Phase 2 completed / feature frozen**。`wi_20260820_272` 已完成 execution binding/checkpoint/review、真实 fresh-session dogfood、human review 与 terminal closeout。
- 原 Phase 3 已拆为独立 successor `harness-autonomous-rollover` / `wi_20260822_730`；本 feature 不再承载后续 autonomous scope。
- 下文第 3–8 节保留为**历史执行记录/设计 seed**；其中旧的 `pending`、`PR CI pending`、`deferred` 等字样描述当时阶段，不再表示本 completed feature 的当前未完义务。当前状态只以上述 terminal closeout 与 successor WI730 为准。

`@anchor symbol ContinuationContract`
`@anchor absent CONTINUATION_PROFILE`
`@anchor symbol continuation_command`

## 2. 任务表

| ID | 任务 | 状态 | 生产影响 | 验收锚点 |
|---|---|---|---|---|
| T-S0-001 | authoritative `specs/` 三件套 | 完成 | 无 | `@anchor manual s0-spec-review :: 用 spec-anchor 扫新 code-level spec；确认 explicit anchor、broken、unverifiable 和 pending 边界` |
| T-S1-001 | `ContinuationState` / `ContinuationContract` / validators | 完成：本地 tests/guard + real-CC dogfood PASS；PR CI 仍以当前 HEAD check 为准 | library only | `@anchor symbol ContinuationContract` |
| T-S1-002 | continuation durable CRUD、atomic write、bundle path | 实现完成；本地 tests/guard PASS；复用 `HarnessStore` atomic/event primitives | shared ledger candidate | `@anchor symbol save_continuation` |
| T-S1-003 | continuation identity、per-contract lock 与状态迁移 | 实现完成；本地并发/状态测试 PASS | shared ledger candidate | `@anchor symbol VALID_CONTINUATION_TRANSITIONS` |
| T-S1-004 | continuation CLI JSON contract | 实现完成；跨仓真实 CLI seam 3/3 PASS | explicit CLI only | `@anchor symbol continuation_command` |
| T-S1-005 | model/store/CLI/privacy/并发测试 | 完成：29/29 relevant tests + portable guard drift=0 | tests only | `@anchor test test_continuation_claim_is_single_winner` |
| T-S2-001 | 手工 create→validate→`/new`→fresh Session consume dogfood | **完成：3/3 real fresh CC PASS**；no-old-transcript、exact first action、correction=0 | 人工显式 | `@anchor manual three-rollover-dogfood :: 三次真实接班，保存 bundle、fresh consumer 回答、读取路径和纠错记录` |
| T-S2-002 | 根据 dogfood 校准 32768-byte 预算与 bundle 字段 | 完成：real dogfood 1263/1266/1261 bytes；无需调整 32768-byte budget/schema | spec/schema revision | `@anchor manual bundle-budget-calibration :: 记录三次 bundle 字节数、引用数、恢复成本；任何阈值变更同步 Specs` |
| T-S3-001 | `continuation_ready` scan collector 与 policy schema | **moved to successor `wi_20260822_730`** | 默认关闭 | `@anchor absent list_ready_continuations_for` |
| T-S3-002 | `CONTINUATION_PROFILE` 与 minimal prompt | **moved to successor `wi_20260822_730`** | 默认关闭 | `@anchor absent CONTINUATION_PROFILE` |
| T-S3-003 | dispatcher claim/release/consume postcondition 与 disposition | **moved to successor `wi_20260822_730`** | 默认关闭 | `@anchor symbol claim_continuation` `@anchor absent test_continuation_dispatch_serializes` |
| T-S3-004 | Pulse policy 单 Agent canary | **moved to successor `wi_20260822_730`** | autonomous canary | `@anchor absent continuation_ready` `@anchor manual continuation-policy-canary :: 只允许 Pulse，验证 claim/release/consume 与关闭 policy` |
| T-S4-001 | Relay CLI seam 与 `/rollover` 集成 | 完成：PULSE implementation + real Harness CLI/real CC scratch UAT PASS | feature flag off | `@anchor manual pulse-rollover-dependency :: 对拍 PULSE 对应 Specs、commit 与隔离 UAT` |
| T-S5-001 | ComputeCost lineage / phase cost shadow | **moved to successor `wi_20260822_730`** | shadow only | `@anchor manual continuation-cost-shadow :: 对拍 work_item/task/outcome/root/parent refs 守恒与 privacy` |
| T-S6-001 | 生产推广、回滚演练与旧操作文档同步 | **N/A：Phase 2 未授权 production rollout；successor `wi_20260822_730` 承担 staged canary** | none | `@anchor manual continuation-rollout :: 真实 canary、关闭 policy/flag 回滚、未消费 contract supersede 演练` |

## 3. S1 实现与验收顺序

### S1-A：模型先行

已实现：

1. `harness/continuation.py`。
2. 五态状态机和 bundle validator。
3. P0 不接 scan 或 dispatcher。
4. 固定 fixture 覆盖正例、digest tamper、路径逃逸、隐私字段、终态 WI。

验收：

- `ContinuationContract` 与 `ContinuationBundle` 可独立验证。
- 非法状态组合 fail closed。
- 现有 WorkItem/Handoff 语义不修改。
- 原 proposed `absent` anchors 已替换为真实 symbol/test anchors。

`@anchor symbol ContinuationContract`
`@anchor symbol WorkItem`
`@anchor symbol HandoffContract`

### S1-B：Durable store facade 与并发

已实现：

1. `.harness/continuations/` 和 evidence path resolver。
2. bundle-first、contract-second atomic write，复用 `HarnessStore._atomic_write_text`。
3. per-continuation claim lock。
4. identity idempotency。
5. absence of `continuations/` 等价于空 list。

仍需通过仓级 CI/guard 验证 shared store 回归。

验收：

- 写一半不得留下新 contract 指向不存在 bundle。
- 两 claimant 并发只一人获胜。
- 相同 identity 重放 no-op。
- unrelated `.harness` dirty 不得被本次 scoped closeout 卷入。

`@anchor symbol HarnessStore`
`@anchor symbol acquire_lock`
`@anchor test test_continuation_claim_is_single_winner`

### S1-C：CLI 与事件

已实现：

1. CLI 命令全部支持 `--json`。
2. 写动作走 `ContinuationManager` 状态机，不在 CLI 直接 mutate YAML/Pydantic state。
3. 每次迁移追加 `SignalEvent`。
4. CLI exit code 区分业务 conflict 与 IO/system 类错误。
5. `cli_ext.py` 在既有 Click `main` 上注册新组，不改旧命令语义。

仍需 CI 运行 CLI fixture 和完整回归。

`@anchor symbol SignalEvent`
`@anchor symbol continuation_json_contract`

## 4. S2 手工 dogfood

选择三类真实阶段：

1. 工程：设计 → 实施。
2. 工程：实施 → 独立 UAT。
3. 管理：扫描 → 异常裁决。

流程：

```text
旧 Session
  → 创建/验证 continuation
  → 保存当前读数
  → 人工 /new
  → 新 Session 只读 bundle
  → 复述 objective/AC/first action
  → consume
  → 做第一个有效动作
```

每次必须记录：

- bundle bytes、引用数。
- 旧 Session 结束前最后一个 phase-complete 时刻。
- fresh Session 到第一有效动作的轮数与成本。
- 是否读取旧 transcript。
- 是否发生用户纠错、重复探索、漏 AC。
- 是否新增孤儿 WI/HO。

成功门：

- `3/3` continuation validate 通过。
- `3/3` fresh Session 未读取旧 transcript。
- `3/3` 正确复述全部 AC 与 first action。
- duplicate continuation=`0`。
- orphan WI/HO 增量=`0`。

`@anchor manual three-rollover-dogfood :: 保存三次真实记录与可复算命令，不能用作者口头总结代替`

## 5. Phase 3 historical seed — moved to `wi_20260822_730`

本节只保留为历史设计 seed；执行责任已移交 successor `wi_20260822_730`，不再构成本 feature 的未完成 scope。

原设计为只在 Pulse policy opt-in 后运行：

```yaml
- type: continuation_ready
  compute_profile: platform_default
  default_autonomy: autonomous_llm
```

历史验收顺序：

1. policy 不声明：ready continuation 存在但 scan 0 hit。
2. 声明：同一 continuation 1 hit。
3. 两个 ready：只 spawn 第一个，第二个 deferred。
4. spawn 前失败：release。
5. consume 前 timeout：release + attempt。
6. consume 后失败：contract 保持 consumed，沿用现有 autonomous failure。
7. 关闭 policy：不再执行，ready contract 保留。

`@anchor symbol ScanItemPolicy`
`@anchor symbol spawn_autonomous_session`
`@anchor absent CONTINUATION_PROFILE`

## 6. 历史高风险验收门

以下规则作为 successor 的输入 seed 保留；新的 promotion / canary 以 `wi_20260822_730` 及其新 Specs 为准。

- 任何 production opt-in 前，必须完成 S2 三次手工 dogfood。
- 任何 Relay 自动 switch 前，必须证明 bundle 未持久时旧 Session 不会被 kill。
- 任何 autonomous canary 前，必须证明 claim single-winner。
- 任何跨 Agent 需求必须转 Handoff，不得扩 continuation。
- 任何“降本”声明必须同时给质量、纠错、返工和孤儿账本读数。
- 任何自动 phase detection 必须另立 Spec；本 feature 最终也只交付显式/机械继续执行。

`@anchor symbol HandoffContract`
`@anchor manual high-risk-gate-review :: 逐项附证据路径、真实入口与反例；缺一项不得启用`

## 7. Phase 2 historical execution plan

以下表格是开发过程快照，不是当前任务队列；最终事实由 `wi_20260820_272=completed`、terminal closeout evidence 与本文件头部 `status: completed` 覆盖。

### 7.1 Slice table

| ID | 任务 | 当前状态 | 风险 | 验收/证据 |
|---|---|---|---|---|
| T-P2-S0-001 | Harness Requirements/Design/Tasks 升到 v0.2，明确 WI/Session/Continuation 语义、execution binding、checkpoint、human review、compatibility 与 WI272 dogfood | **local candidate complete: guard drift=0; PR CI pending** | docs only | `@anchor manual phase2-harness-spec-review :: 三件套 guard=0；planned symbol 用 absent，不伪装已实现` |
| T-P2-S0-002 | PULSE Requirements/Design/Tasks 升到 v0.2，定义 `/wi status|continue|checkpoint|rollover|review` 与 Phase 1 RolloverCoordinator 复用 | **local candidate complete: PULSE guard drift=0; PR CI pending** | docs only | `@anchor manual phase2-pulse-spec-review :: PULSE session lifecycle guard=0；legacy /rollover<ct> 契约保留` |
| T-P2-S0-003 | 把 `wi_20260820_272`、`ho_20260820_155` 与 program evidence 纳入专用变更面；核 Phase 1 两个 merge 仍为默认分支祖先 | **local evidence complete; publish pending** | ledger/docs | `@anchor manual wi272-phase1-baseline :: ancestor checks + tracked WI/HO/evidence + exact AC` |
| T-P2-S0-004 | 对拍上层 `wi_20260820_079` Observe 路线：保证 Phase2 UAT 留下可机械 harvest 的 join keys/source refs，缺失事实 unknown，且不新增第二 execution evidence 状态机/ledger | **PASS locally; source-matrix seed recorded; parent G2 still waits real UAT** | docs only | `@anchor manual wi079-alignment :: 逐条对拍 G1/G2、source matrix、unknown/false-zero、no-second-ledger 边界` |
| T-P2-S1-001 | `WorkItemExecutionBinding` / `ExecutionLifecycleState` / store / per-WI lock | **implemented candidate: checkpoint-history regression + Harness full 1999 PASS/1 skipped; PR CI pending** | shared ledger, high-risk | `@anchor symbol WorkItemExecutionBinding` |
| T-P2-S1-002 | `ExecutionCheckpoint` + checkpoint validator/store + privacy/size/idempotency tests | **implemented candidate: checkpoint-history regression + Harness full 1999 PASS/1 skipped; PR CI pending** | shared ledger, high-risk | `@anchor symbol ExecutionCheckpoint` `@anchor test test_execution_checkpoint_does_not_rollover` |
| T-P2-S1-003 | Harness execution `status/resolve-continue/prepare/apply/recover` JSON API | **implemented candidate; fresh-session review found and fixed `recovery_required → resume` bypass; manager + real CLI negative regression PASS; final synced-head CI/UAT pending** | control plane, high-risk | `@anchor symbol register_execution_runtime_commands` `@anchor test test_execution_rollover_epoch_and_recovery` |
| T-P2-S1-004 | `WorkItemReviewDecision` + exact candidate fingerprint + Phase2 closeout gate；legacy complete 不变 | **implemented locally: focused tests + legacy verify-result regression PASS; PR CI pending** | terminal gate, high-risk | `@anchor symbol WorkItemReviewDecision` `@anchor test test_human_review_fingerprint_gates_closeout` |
| T-P2-S2-001 | PULSE `WorkItemSessionController` + WI-centric `status/continue/checkpoint` | **implemented candidate; focused + cross-repo real CLI UAT PASS; PR CI pending** | flag off | `@anchor manual pulse-wi-controller-contract :: real Harness CLI seam + runtime evidence resolution` |
| T-P2-S2-002 | `/wi rollover <wi>` 内部 checkpoint→continuation→Phase1 strict ACK/consume/recovery；不要求用户 ct | **implemented + WI272 two real fresh CC rollovers PASS; exact long-term workflow-equivalent local CI + cross-repo real CLI/state UAT PASS; PR CI pending** | flag off | `@anchor manual wi-rollover-real-path :: normal + ACK failure + restart + duplicate` |
| T-P2-S2-003 | `/wi review <wi>` human-originated review surface；candidate 变化使 approval stale | **implemented candidate; exact-candidate/explicit-human tests PASS; real final human review pending** | flag off | `@anchor manual wi-human-review-boundary :: autonomous approve rejected + exact candidate human decision` |
| T-P2-S3-000 | ChatGPT+GitHub dogfood bare `/wi <wi>` external-surface entry + visible-reference/GitHub/activity observation；首次 unbound WI bind epoch=1；shared Pro 不做单 WI cash 虚构分摊 | **PASS: real ChatGPT+GitHub adopt dogfood created epoch=1 binding; checkpoint=none; reuse_current; idempotent re-entry PASS; surface manager/CLI 8/8 PASS** | dogfood evidence only | `@anchor symbol ExecutionSurfaceManager` `@anchor test test_surface_cli_enter_then_observe_round_trip` |
| T-P2-S3-001 | 本 WI 第一次 real fresh CC rollover；target 不读旧 transcript，epoch +1 | **PASS: ct_20260820_778 consumed; epoch 1→2; fresh CC strict ACK 7/7; no human restatement/correction** | isolated dogfood | `@anchor manual wi272-rollover-1 :: checkpoint/ct/ACK/epoch/cost/correction/rework` |
| T-P2-S3-002 | 本 WI 第二次 real fresh CC rollover + 至少一条 restart/duplicate/concurrency/ACK recovery 真实路径 | **PASS: ct_20260820_964 consumed; epoch 2→3; source transcript mode 000 + canary target hits=0; failed-target release/recovery + exact local CI/cross-repo matrix PASS** | isolated dogfood | `@anchor manual wi272-rollover-2 :: second epoch + failure/recovery evidence` |
| T-P2-S3-003 | self-hosting 暴露的 CR-02：durable Execution Plan Adoption 与 WI-scoped `/adopt`；把批准方案绑定到 WI272 Phase A，而不改变 root WI 生命周期 | **PASS: `wi_20260820_126` completed；PR #8 merge `e80c34f22abe80372af1f785f1071621c2fe465e`；Hestia adoption `adoption_v1_309e5c6afa74a6547c112325` validate PASS** | corrective self-hosting dependency | `@anchor symbol ExecutionPlanAdoption` `@anchor test test_execution_adopt_show_validate_json_round_trip` |
| T-P2-S3-004 | fresh ChatGPT `/wi` independent implementation review corrective：禁止 `recovery_required` 通过普通 `execution resume` 绕过 reconciliation | **PASS locally: `resume_execution` 仅允许 dormant；manager + real CLI negative regression PASS；旧 frozen WI272 claim 已回退 in_progress 等待重新冻结** | recovery integrity | `@anchor test test_resume_execution_rejects_recovery_required_without_reconciliation` |
| T-P2-S4-001 | human review exact candidate → 跨仓 merge → default CI/smoke → cleanup → root WI complete | **Amanda main consumer verifier dispatched as `wi_20260821_083` / `ho_20260821_308`; waits final synced heads + exact-pair Actions UAT + refrozen WI272 claim** | closeout | `@anchor manual wi272-human-closeout :: approval fingerprint + merge/default heads + smoke + branch/worktree cleanup` |
| T-P2-S4-002 | 生成供 `wi_20260820_079` Evidence Harvest 使用的 non-SSOT source matrix seed：lifecycle/runtime/usage/route/candidate/review/outcome/merge/human-attention 分域记录 source/ref/digest/authority 或 unknown | pending | evidence only | `@anchor manual wi272-observe-source-index :: 不读 transcript/回忆，不复制正文，absence 不得写 0` |
| T-P2-S4-003 | `S1MeasurementPilot` schema/validator + WI272 单 WI closeout projection；all-fields-present/unknown-not-zero，post-approval 且不改 candidate | **implemented locally: schema/deriver + closeout digest validation PASS; real WI272 artifact pending** | S1 consumer gate | `@anchor symbol S1MeasurementPilot` `@anchor manual wi272-s1-measurement-pilot :: validator + closeout artifact` |
| T-P3-001 | cost/quality/context-aware autonomous rollover | **not enabled / shadow only** | out of Phase 2 | `@anchor manual phase3-default-off :: no policy/flag causes automatic rollover` |

### 7.2 Phase 2 implementation MVS

Specs 通过后第一实现切片只做 Harness durable control-plane foundation：

1. `WorkItemExecutionBinding` + `ExecutionLifecycleState`。 **implemented candidate**
2. `.harness/executions/{wi}.yaml` atomic store 与 per-WI lock。 **implemented candidate**
3. `ExecutionCheckpoint` validator/store。 **implemented candidate**
4. `harness execution status/checkpoint` 最小 JSON surface。 **implemented candidate**
5. normal/failure/privacy/idempotency/concurrency/legacy-load tests。 **focused/legacy PASS; full real-topology Harness 1999 PASS/1 skipped; evidence `.harness/evidence/session-context-lifecycle/wi272-phase2-harness-review-gate-local-evidence-2026-08-20.md`**

**明确不在 S1 MVS 做**：Relay kill/spawn、`/wi rollover`、human approval、生产 policy、Phase 3 自动判断。这样先证明
“WI 能跨 Session 保存 execution truth”，再把物理 Session 控制接上，避免一次同时改 Harness ledger、Relay runtime、completion gate。

`@anchor symbol WorkItemExecutionBinding`
`@anchor symbol checkpoint_execution`

### 7.3 Dogfood discipline

`wi_20260820_272` 是 Phase 2 首个长期 WI，开发期间禁止另建平行 WI 替代。每轮 checkpoint/rollover 必须把：

- epoch / phase / runtime / opaque session ref；
- checkpoint revision 与 continuation；
- source→target first action；
- source transcript 是否可读；
- cost、human correction、rework；
- branch/head/test/CI/review evidence；
- lifecycle/runtime/usage/route/candidate/review/outcome/merge/human-attention 的 authority source/ref/digest 或 `unknown`；
- 对 ChatGPT+GitHub surface，从 bare `/wi` entry 起记录 visible user/assistant turns、`visible_text_reference_v1`、GitHub operations/commit/PR/review/CI、activity class 与 correction/rework；`actual_model_tokens/provider_session_identity/per-WI Pro cash allocation` 缺 producer 时显式 unavailable；

写回其 execution evidence。此记录只作为 source index/evidence artifact，不成为新 ledger。两次 real fresh rollover 之前不得把 Phase 2 标 completed。

`@anchor manual wi272-two-fresh-rollovers :: 两次 fresh CC target 都只读 checkpoint/continuation refs，不读 source transcript`

## 8. 回滚

Frozen post-review publication regression coverage: `tests/test_closeout_publication.py`
uses an abstract CWA-only accepted/approved WI, a real local bare Git authority and
subprocess CLI publication. Cover Host capture without canonical writes; stage-one
publication without an executor; exact-head CI rejection and successful completion;
single terminal event on replay; approval, candidate, Outcome and ancestry drift;
extra, escaped or digest-mismatched files. Only the external GitHub API is simulated.

| 切片 | 回滚 |
|---|---|
| S1 | 停用 continuation CLI；continuation 文件保持只读审计，不影响现有 WI/HO |
| S2 | 停止人工 dogfood，继续使用 `/new` + 手工 memo |
| Phase 3 autonomous | 从 policy 删除 `continuation_ready`，dispatcher 不再扫描 |
| Phase 1 Relay primitive | 关闭 PULSE feature flag，Relay 回到 `/compact`/`/new` |
| Phase 3 cost shadow | 停 shadow consumer，不删除 usage 事实 |
| Phase 2 | 停 WI-centric controller；binding/checkpoint/review 留只读审计；未消费 continuation 可 supersede，已 consumed/epoch 记录不得复活或倒写 |
