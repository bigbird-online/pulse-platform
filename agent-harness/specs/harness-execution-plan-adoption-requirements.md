---
feature_name: "execution-plan-adoption"
version: "0.2.0"
status: "implemented_candidate"
created: "2026-08-20T00:00:00+0800"
modified: "2026-10-07T00:00:00+0800"
owner: "Pulse"
tags: [requirements, execution-plan, adoption, work-item, audit]
---

# Harness Execution Plan Adoption — Requirements

> CR-02 / `wi_20260820_126`。本能力只回答“一个已经批准的执行计划，如何绑定到既有 WI 的某个 execution phase”。
> 它不是 Session Context Lifecycle Phase 2，也不改变根 WorkItem 的业务生命周期、owner 或最终 Human Review / Outcome UAT 语义。

## 1. Invariants

| ID | Requirement |
|---|---|
| EPA-R-001 | Adoption 必须是 durable、可审计事实，至少绑定 `work_item_id`、`execution_phase`、`plan_ref`、`plan_sha256`、`decision`、`adopter`、`adopted_at` 与 `source_refs`。 |
| EPA-R-002 | 新 Adoption 只允许现存非终态 WI，且必须接纳仓库内计划原文。缺 WI、终态、缺新计划或路径越界须拒绝；已提交决定的重试保留原结果，后来的终态或输入文件丢失不撤销它。 |
| EPA-R-003 | Adoption 不得修改根 WI `owner/status/active_executor`，不得创建 WorkItem/Handoff，不得宣布 phase 或根 WI 完成。 |
| EPA-R-004 | 同一 WI + execution phase + plan ref + plan digest 的重放必须返回同一 adoption fact；同 phase 的不同计划必须冲突，禁止原地覆盖历史。显式 supersede/version 是未来独立动作。 |
| EPA-R-005 | 每次成功 adoption 必须进入现有 WI `SignalEvent` 链，event type 为 `execution.plan_adopted` 或语义等价稳定事件；不得新建第二业务 event ledger。 |
| EPA-R-006 | `show` 与默认 `validate` 读取内核原决定，不以采用记录、事件 YAML 或当前计划草稿的一致性作为门禁。显式 `validate --plan` 比较调用者提供的文件和原采用摘要，拒绝不同内容。 |
| EPA-R-007 | 写入口只向本地内核提交：决定、原事件、计划原文及发布效果在一次 CAS 中记录，再生成可丢弃投影。投影失败后重试不重做决定；并发终态或冲突计划不能被覆盖。禁止 record/event 两次业务写入与补事件恢复路径。 |
| EPA-R-008 | durable `plan_ref/source_refs` 禁止 machine-local absolute path。plan 必须落在 Harness repository root 下，并以 repository-relative ref 持久化；canonical publisher 必须把 `--plan` 识别为 file input，在 dispatch 前冻结字节并将 absolute invocation path 改写为 projection 内的 content-addressed relative carrier。 |
| EPA-R-009 | 用户侧 `/adopt` 必须 WI-scoped：显式 WI 可转发；省略 WI 时只允许 exactly-one current-WI binding。零候选或多候选必须 fail closed，模型不得猜目标 WI。 |
| EPA-R-010 | Pulse 只交付 adoption 能力和反向 handoff；`wi_20260820_272` Phase A 的首次真实 adoption 必须由 Hestia 在交付后执行并留下 dogfood evidence。 |
| EPA-R-011 | 旧采用记录在首次接纳原件时迁入同一内核，保留采用人、时间、来源及已存在的原事件。旧写入在事件前中断时，保留原记录并返回 `event_id=null`，不补造历史事件。只有摘要吻合的计划字节才可标为原文。 |
| EPA-R-012 | 发布和复制使用内核声明的完整投影范围。已提交原请求只交付原响应；较新的本地决定和派生文件不使其失效。真实资源权限、远端 Git CAS、非投影输入及未知外部效果仍须验证。 |

## 2. Stable machine surface

```text
harness execution adopt [WORK_ITEM_ID] --phase <phase> --plan <repo-file> --adopter <actor>
  [--source-ref <ref> ...] [--current-work-item <wi> ...] [--json]
harness execution show <WORK_ITEM_ID> [--phase <phase>] [--json]
harness execution validate <WORK_ITEM_ID> --phase <phase> [--plan <repo-file>] [--json]
```

`/adopt` 是上层 adapter intent，不是模型自由推断命令。adapter 可把唯一当前 WI 作为 `--current-work-item` 传给 backend；backend 自己再做 zero/multi fail-closed 判定。

## 3. Acceptance mapping

- normal + durable/event/root-WI invariants: `tests/test_execution_plan.py::test_adopt_persists_record_event_and_preserves_root_work_item`
- idempotency/conflict: `test_adopt_is_idempotent_for_same_plan_and_phase`, `test_conflicting_plan_for_same_phase_fails_closed_without_mutation`
- terminal/missing/input/privacy: `test_missing_and_terminal_work_items_fail_closed`, `test_missing_plan_file_fails_closed`, `test_validate_explicit_plan_detects_unadopted_source_changes`, `test_validate_uses_original_event_when_event_projection_is_damaged`, `test_plan_must_be_inside_repo_and_source_refs_cannot_leak_absolute_paths`
- partial-write retry + concurrency: `test_idempotent_retry_repairs_missing_event_without_rewriting_record`, `test_concurrent_conflicting_adoptions_have_one_winner`
- `/adopt` scope: `test_resolve_adopt_scope_never_guesses_between_work_items` and CLI coverage.

## 4. Mechanical anchors

`@anchor symbol ExecutionPlanAdoption`
`@anchor symbol ExecutionPlanManager`
`@anchor symbol resolve_adopt_scope`
`@anchor test agent-harness/tests/test_execution_plan_cli.py::test_execution_adopt_show_validate_json_round_trip`
`@anchor test agent-harness/tests/test_execution_plan.py::test_concurrent_conflicting_adoptions_have_one_winner`
`@anchor test agent-harness/tests/test_execution_plan.py::test_validate_uses_original_event_when_event_projection_is_damaged`
`@anchor test agent-harness/tests/test_kernel_execution_plan.py::test_original_import_keeps_a_legacy_first_fact_including_partial_old_write`
`@anchor test agent-harness/tests/test_kernel_execution_plan.py::test_adoption_queries_and_retries_ignore_disposable_ledgers`
`@anchor test agent-harness/tests/test_kernel_execution_plan.py::test_real_broker_retries_plan_publication_without_readopting_or_reading_input`
`@anchor manual wi126-hestia-dogfood :: 首次真实 WI272 Phase-A adoption 必须由 Hestia 在默认分支能力合入后执行`
