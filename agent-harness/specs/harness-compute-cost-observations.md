---
feature_name: "harness-compute-cost-observations"
version: "0.4.2"
status: "in_progress"
root_work_item: "wi_20260820_568"
created: "2026-08-24T00:00:00+0800"
modified: "2026-10-11T20:04:27+08:00"
owner: "Pulse"
---

# Harness ComputeCost Observation Contract

## 1. Boundary

The installed native core's production Git journal owns WI execution authority. ComputeCost's existing UsageLedger
owns imported measurement facts; producer adapters supply bounded usage evidence. Measurement never grants,
revokes or delays execution, including entry, recovery, retries and rollover. Native original evidence and its
privacy boundary follow `../project-specs/rewrite-measurements-contract.md`.

The retained local adapter below does not replace that authority. It never infers procurement from runtime/model/surface
or writes raw provider identity, content or credentials into normalized measurement projections.

The registered normalized producer contracts are:

- `app_server_token_observation.v1`
- `app_server_turn_completion.v1`
- `app_server_epoch_token_seal.v1`

The SCL-owned managed adapters accept `pi_session_usage_snapshot/v2` and `cc_session_usage_snapshot/v2`
zero-baseline/final pairs bound to `pulse_runtime_usage_binding/v3`. They emit the normalized seal and private
`scl_pi_usage_measurement/v1` or `scl_cc_usage_measurement/v1` read projections. Historical v1 snapshots remain
readable evidence but cannot create new whole-session attribution.

For CC/CC-SDK, Relay invokes ComputeCost `cc-session-probe`; its bounded collector actually consumes
`runtime_samples.build_cc_usage_sample` (`runtime_usage_sample/v2`) with the original Host binding digest.
The CC snapshot is the immutable source-prefix envelope and conserving summary of those samples, not another
raw-Token normalization or pricing authority. SCL validates the envelope and does not parse Claude transcripts.

`execution_surface`、`runtime`、`provider/model`、`procurement_source`、`usage_producer` remain orthogonal.
ChatGPT subscription cash stays portfolio-level and is not allocated to a WI by this contract.

## 2. Historical prospective runtime lineage

Before SCL cutover, the legacy `cost-attribution record` producer consumes exactly one host-provided runtime identity. Codex uses
`CODEX_THREAD_ID`; Pi uses `PI_SESSION_ID`. Harness hashes the selected value in memory under a
runtime-specific domain and persists only `lineage_v1_<sha256>`. Missing, simultaneous, unsupported,
or empty runtime identity fails closed; model/provider/path inference is forbidden.

`@anchor symbol runtime_lineage_root_ref`
`@anchor test agent-harness/tests/test_cost_attribution.py :: test_pi_host_identity_produces_privacy_safe_runtime_lineage`
`@anchor test agent-harness/tests/test_cost_attribution.py :: test_ambiguous_or_missing_host_runtime_identity_fails_closed`

## 3. Historical producer write gate

After installed SCL cutover, or once the local kernel owns the WI, both `cost-attribution record` and
`cost-observation record` return `legacy_execution_retired` before reading any old WI or execution binding.
An intact stale binding, missing projection, or WI not yet imported cannot revive the old cost writer.
Existing original cost facts remain readable and unchanged. Ordinary lifecycle entry does not establish attribution;
explicit managed Pi/CC measurement uses `record-native-usage-local` as described below. A historical Codex
observation is not converted into a new SCL measurement or presented as a live producer result.

Only in an installation that has not cut over, for a WI not owned by a local kernel,
`harness cost-observation record <wi> --agent <agent> --input <normalized.json>` must:

1. require a non-terminal WI and active execution authority held by the caller;
2. require exact `work_item_ref + execution_epoch + phase` equality;
3. require an existing `work_item_cost_attribution/v1` with the same `measurement_epoch_ref`;
4. reject unsupported/extra fields, malformed opaque refs, non-conserving Token breakdowns and seal deltas;
5. reject raw identity/content keys recursively before write;
6. store canonical JSON append-only under `evidence/compute-cost/observations/<wi>/<schema>/<fact-id>.json`;
7. make identical retries idempotent and reject the same fact identity with different bytes;
8. append only a privacy-safe receipt event containing schema, opaque fact/epoch refs, digest and execution boundary.

Unknown/unavailable evidence is never written as measured zero. Harness does not mutate the ComputeCost usage ledger.

`@anchor test agent-harness/tests/test_cost_kernel_boundary.py :: test_old_cost_writer_cannot_use_retired_binding`
`@anchor symbol CostObservationManager`
`@anchor symbol CostObservationError`
`@anchor test agent-harness/tests/test_cost_observation.py :: test_records_exact_normalized_fact_and_event_without_raw_provider_data`
`@anchor test agent-harness/tests/test_cost_observation.py :: test_duplicate_fact_is_idempotent_and_conflicting_bytes_fail_closed`
`@anchor test agent-harness/tests/test_cost_observation.py :: test_private_nested_key_is_rejected_even_when_top_level_shape_is_valid`

## 4. Retained local measurement interface

Ordinary local entry, planning, continuation and resource reconciliation neither read nor initialize measurement.
They do not call `read_existing_epoch`, `ensure_epoch` or measurement materialization and do not project measurement
into their response. Missing, malformed, pending, failed or sealed accounting cannot change their business result.

Explicit measurement commands retain the append-only `scl_measurement_epoch/v1` boundary for the exact
`work_item_id + execution_epoch + attached Host Session`. WI, phase, runtime, surface and Agent come from the current
local transaction, not caller-supplied measurement identity. No `ExecutionManager`, `.harness/executions` or `HARNESS_ROOT`
execution binding is consulted. This retained adapter's SQLite rows are measurement evidence, not an alternative native
production WI ledger or an execution gate.

Mode-0600 artifacts under `<kernel>/measurement-artifacts/evidence/compute-cost/` remain repairable read projections.
They use `work_item_cost_attribution/v1` and `app_server_epoch_token_seal.v1`, with bounded snapshots under `native/`.
Readback exposes domain-separated refs and digests plus the opaque Session authority needed to verify a v3 binding,
never raw provider Session identity. Explicit recovery reads the original measurement boundary; ordinary runtime
recovery does not wait for it or create a replacement baseline.

`describe-measurement-local WI --agent A --session-ref S` returns `status=read_only` and the existing open
`result.measurement`. Optional `--epoch`, `--phase`, and `--surface-id` selectors must match exactly. Current WI,
Agent, attached opaque Host Session, epoch, phase, runtime, surface, measurement row, and native facts are read in one
SQLite read-only snapshot. The response retains the original `baseline_sha256`, `runtime_binding_sha256`, and opaque
`authority_ref`/`host_session_ref`; it never accepts a newly issued binding as a substitute for the immutable origin.
Missing epochs, pending baselines, sealed epochs, closed execution, and identity/boundary mismatches return explicit
errors without creating, reopening, migrating, or repairing measurement state. This command must not construct an
initializing kernel. SQLite WAL read coordination is allowed, but business rows and artifacts remain unchanged and
latest committed WAL facts must be visible; immutable SQLite mode is unsuitable because it can ignore that WAL.

`record-native-usage-local` accepts the current attached host's `pi_session_usage_snapshot/v2` or
`cc_session_usage_snapshot/v2` for the matching physical runtime (`pi`, `cc`, `cc_sdk`). The baseline must be an
attributable zero at an immutable source byte/prefix boundary. A final is accepted only when it names that exact baseline,
keeps WI, Agent, phase, epoch, physical runtime, procurement route, opaque Session authority, registered surface and v3
binding digest unchanged, preserves the baseline prefix and exclusions, and contains at least one settled
baseline-exclusive/final-inclusive usage event. A resident-Harness epoch finalizes its physical `execution_runtime=pi`
only while the attribution remains `baseline_pending`, before business usage; later changes fail closed. SCL normalizes
only final-minus-zero-baseline counters; it never attributes
a reused whole Session. Pi cache-write Tokens remain included in `input_tokens`/`total_tokens` and are preserved in the
private pair for ComputeCost per-request pricing.

Identical retries are idempotent. Same-state byte changes, prefix drift, cross-session/surface input, post-seal mutation,
raw identity/content keys, absolute paths, non-conserving totals and v1 whole-session input fail closed. The command input
and every materialized artifact are mode `0600` regular non-symlink files. The typed readback retains `surface_id` for
stable clients and adds exact `execution_surface`, v3 binding revision/digest, opaque authority ref, bounded coverage,
pair ref/digest, and terminal measurement status without exposing raw identity. The generated
`scl_pi_usage_measurement/v1` or `scl_cc_usage_measurement/v1` path and digest are directly consumable by
ComputeCost's SCL auto-discovery. CC per-request input/cache partitions retain their original binding and quantities.

ROI eligibility is an independent terminal dimension. `project_terminal_status` returns `incomplete/pending` when an
eligible WI lacks attribution or a complete bounded pair and final Token seal, `complete/satisfied` only when every
required epoch has them, and `unavailable/not_applicable` for unknown, legacy or explicitly noneligible WIs. This
projection neither weakens lifecycle, Session or cross-surface authority fences nor blocks any authorized WI execution.

`@anchor symbol SCLMeasurementManager`
`@anchor symbol SCLMeasurementEpoch`
`@anchor test agent-harness/tests/test_scl_measurement_readback.py :: test_describe_recovers_original_baseline_after_conflicting_reissue_without_writes`
`@anchor test agent-harness/tests/test_scl_measurement_readback.py :: test_readback_observes_committed_wal_and_does_not_use_stale_immutable_database`
`@anchor test agent-harness/tests/test_scl_measurement_readback.py :: test_entry_never_reads_or_initializes_measurement`
`@anchor test agent-harness/tests/test_scl_measurement.py :: test_entry_boundary_is_durable_private_and_idempotent`
`@anchor test agent-harness/tests/test_scl_measurement.py :: test_bounded_baseline_and_final_emit_compute_cost_pair_and_conserving_seal`

## 5. Lifecycle integration

Relay target preparation, canonical adoption, dispatch and result settlement use the real runtime/Session, admitted
workspace, operation and canonical execution epoch/phase, never usage identity, timestamps or digests. The saved
execution epoch/phase are recovery projections, not grants. Historical entries without them must recover the exact
canonical target through the original entry and workspace admission; measurement cannot fill those fields. A reserved
later successor does not replace an unfinished current dispatch. Unknown dispatch stays unresolved without a resend.

Normal startup and recovery do not inject or create a usage binding. Existing measurement observations remain unchanged;
a later observation is not overwritten by asynchronous preparation or repeated adoption. Explicit measurement remains
strict and may be unavailable while the authorized business path succeeds. Source and isolated tests do not demonstrate
installed adoption, channel consumption or acceptance; these are verified separately per node.

Relay contract tests: `Relay/tests/local-entry-measurement-noninterference.test.ts` and
`Relay/tests/continue-pending-successor.test.ts`.

Historical rollover completion evidence v2 keeps lifecycle and measurement status separate. Source retirement, continuation
consume/apply, target binding and live runtime readback remain mandatory. A Pi source seal may instead carry only
`status=unavailable` plus the bounded reason `capture_failed` or `producer_unavailable`; it carries no artifact identity,
epoch or zero Token. That evidence permits lifecycle completion but leaves the source cost epoch measurement-incomplete
and therefore ineligible for ComputeCost cohort acceptance. V1 remains readable with its original durable Pi
seal requirement; that historical evidence rule must not be reinstated as a live runtime retirement gate. `not_settled` is not an unavailable measurement result: the runtime must not retire a source that still has an
unsettled final turn.

Rollback disables new measurement entry/invocation while retaining append-only kernel rows and normalized projections
for audit. It does not rewrite execution bindings, checkpoints, usage ledgers, or historical facts.

`@anchor symbol RolloverCompletionEvidence`
`@anchor test agent-harness/tests/test_execution.py :: test_rollover_v2_allows_pi_lifecycle_completion_when_usage_is_unavailable`
`@anchor test agent-harness/tests/test_execution.py :: test_rollover_usage_unavailability_is_v2_only_and_bounded`
`@anchor manual real-app-server-prospective-epoch :: pending; require live baseline, final Token watermark, completed turn, durable facts, and exact WI query`
