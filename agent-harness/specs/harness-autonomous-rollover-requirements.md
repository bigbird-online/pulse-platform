---
feature_name: "harness-autonomous-rollover"
version: "0.5.0"
status: "proposed"
root_work_item: "wi_20260822_730"
predecessor_work_item: "wi_20260820_272"
created: "2026-08-22T00:00:00+0800"
modified: "2026-08-26T11:30:00+0800"
owner: "Pulse"
corrective_work_item: "wi_20260820_136"
external_dependency:
  - "bigbird-online/PULSE:specs/pulse-context-aware-autonomous-rollover-requirements.md"
tags: [requirements, autonomous-rollover, continuation, multi-surface, resume-fast-path, shadow, policy]
---

# Harness Autonomous Rollover — Requirements

This successor begins only after Session Context Lifecycle / `wi_20260820_272` is terminally completed. It does not reopen the predecessor. Before cost/context/quality autonomy, WI730 hardens SCL into a multi-surface lifecycle primitive whose continuation path preserves both correctness and work momentum.

## S0 multi-surface foundation

- Harness lifecycle semantics must be provider-neutral. Cloud vendor, hostname, and runner instance names are registry facts, never lifecycle types.
- A resident-compute entry supplies a stable `runtime_class` plus opaque registered `surface_id`; binding authority must distinguish active surfaces so one resident host cannot silently steal another host's active Session.
- An unbound open WI may establish entry-forward truth on its actual supported surface; no pre-entry Session identity, usage, cash, or turn history is backfilled.
- Active matching surface is idempotent reuse; dormant + durable checkpoint may adopt a fresh Session on another registered surface; transitional, recovery-required, terminal, owner mismatch, and dormant-without-checkpoint fail closed.
- ChatGPT web is an external entry over the same durable execution truth, not the definition of SCL; it enters through `harness-native entry-request`, `entry` and `entry-confirm`.
- No surface detours through Actions merely to enter a WI.

## S0R resume fast path

- A user intent to **continue an existing WI** must have one first-class resume operation that composes durable checkpoint validation with create/reuse/fresh-adopt entry. The caller must not reconstruct that state machine by searching chat, PRs, Actions, or old memos first.
- The resume result must return a compact machine-readable work-frontier packet containing at least WI/owner/epoch/phase/surface, completed work, pending work, decisions, risks/open questions, the checkpoint next action, minimal required artifact refs, and narrow staleness predicates.
- Checkpoint acceptance authority, owner, and epoch must be validated **before** any new execution authority mutation. A stale checkpoint fails closed without stealing or changing the prior binding.
- An older checkpoint may mention bootstrap work such as “enter/reconcile this WI”. The resume packet must explicitly state which control-plane bootstrap work is already completed so the fresh Session does not repeat it.
- The default consumer behavior is **checkpoint-first trust with bounded revalidation**: only `stale_if` predicates and `must_read` refs may block the first business action. Repository/Actions archaeology is forbidden unless the checkpoint itself or a staleness predicate requires it.
- The first business action may begin only after the configured **canonical execution authority store** has durably accepted the create/reuse/adopt result with single-winner semantics. A normal source-control PR/CI is not intrinsically that authority-publication step. If a deployment uses Git to carry cross-surface authority, S0R must use a bounded atomic/CAS publication seam rather than wait for an ordinary product-review PR. Later Git/PR audit replication remains outside the first-work critical path.
- `/rollover` checkpoints must converge toward a surface-neutral **post-entry business next action**. Provider-specific bootstrap wording is compatibility input, not the future contract.
- Resume performance is a product requirement: target Time To First Work Action (TTFWA) is resident-surface P95 < 30 seconds and brokered ChatGPT+GitHub P95 < 60 seconds, measured from user continuation intent to the first WI business action. S1 autonomy is blocked until real dogfood meets the applicable target without weakening fail-closed invariants.
- A ready ResumePacket must carry `consumer_work_mode=execute`; checkpoint-free entry carries `orient`. The caller must fail closed on a ready packet that lacks a non-blank first business action or has malformed bounded-read/staleness fields. Generic work-method orientation cannot override a valid execute decision.
- Retrying `harness continue` on the same newly adopted active Session must replay the exact adopted checkpoint frontier instead of degrading to checkpoint-free `orient`. The replay pointer is digest-bound, remains read-only, and is replaced by the Session's first new checkpoint; retry must not create a second Session or duplicate authority event.
- Applying a consumed same-Agent rollover MUST move the exact source checkpoint revision/ref/digest into the target binding's read-only resume baseline before clearing the ordinary latest-checkpoint pointer. The target epoch may read an epoch-1 checkpoint only when the consumed continuation, target Session, target phase and resume digest all match; any missing or conflicting fact fails closed. The first target checkpoint replaces that baseline without deleting or reusing the historical checkpoint.
- A resident host MAY pre-plan one opaque `session_v1_` ref and pass it to `harness continue`. First bind/fresh adoption stores that exact ref; active retry with the same ref is idempotent and a different ref fails without authority mutation. Harness never receives the host's raw provider Session identity.
- ResumePacket and ContinuationBundle must carry every structured terminal consumer obligation from the checkpoint. Session replacement cannot erase target revision, current convergence state, next probe, or failure reason.
- Harness returns control-plane readiness only. It must not label its own return latency as TTFWA; end-to-end dogfood ends the metric at the consumer's first WI business action.

## S0C terminal required-consumer convergence

- Phase-2 terminal evidence uses `phase2_closeout/v2` and contains exactly one `terminal_consumer_convergence/v1` manifest bound to the same WI and candidate fingerprint.
- The manifest declares exact integrated repo revisions and a unique list of stable logical consumers. Consumer mechanism fields are ids/refs, never executable shell text.
- Each active consumer is `required` or `optional`; a `not_applicable` row requires a reason and cannot claim install/readback/effect mechanisms.
- A required consumer passes only when observed revision equals the exact target and durable install, local readback, and real downstream-effect evidence are all present. Outcome acceptance, Git publication, CI, producer self-check, or business prose cannot substitute.
- Every install/readback/effect ref is covered exactly once by a SHA-256 evidence inventory. Terminal validation re-reads each repository-relative artifact and rejects missing or changed bytes; a manifest containing plausible path strings alone cannot pass.
- Any required consumer in pending, unavailable, dirty, diverged, revision-mismatch, effect-unverified, or failed state rejects terminal closeout under the WI lock. The WorkItem remains nonterminal and the checkpoint frontier remains reusable.
- Optional failure is recorded but non-blocking. Dirty/diverged state is evidence to preserve; convergence never resets, stashes, cleans, rebases, force-pushes, or overwrites consumer work.
- Exact replay is idempotent and concurrent terminal attempts have one winner through the existing WI lock/event transition.
- The WI136 counterexample is permanent regression evidence: canonical completion with stale Vultr Stats must fail; exact install/readback plus fresh Stats exporter/App-query effect is the positive control.

## S0X explicit active foreign-surface recovery

- Active authority remains non-stealable by normal entry/resume. The only exception is an explicitly confirmed recovery operation for an open `main_only` WI whose `--agent`, binding Agent, and WI owner are identical.
- This is a human-main-body gate: manager rejects cron and every dispatcher-stamped `HARNESS_AGENT_ID`, and requires explicit `human_confirmed=true`. The opaque `human_authorization_v1_*` value is an audit join reference, not independently verified authority; authority comes from that gate plus explicit confirmation.
- The caller must exactly name the current known opaque source Session, a bounded opaque human-authorization reference, a bounded unique opaque `operation_v1_*` reference, and one complete target form: `runtime_class + surface_id` or exact `runtime + target_session_ref`. Existing continuation privacy validation plus recovery-specific guards reject UUID, path, provider-token, secret, credential, and private-like material.
- The target runtime must differ from the active source runtime. This seam cannot become a same-runtime Session swap or general preemption API.
- Harness validates runtime/surface token shape only. Registry membership and the fact that the named target is the operator-resolved intended surface are caller/platform preconditions, not claims made by Harness.
- Under the global state lock followed by one WI lock, a valid recovery increments epoch exactly once, preserves WI/owner/phase, leaves the new binding active, clears only the current checkpoint pointer, preserves append-only checkpoint history, and does not create or backfill surface coverage/usage.
- Retry is idempotent only when operation, authorization, source Session, target identity, caller, and reason digest all match. Any mismatch, wrong owner/session, terminal WI, non-active lifecycle, same runtime, or malformed/unknown opaque ref fails closed without mutation.
- Binding and audit append use a byte-snapshot paired transaction: an ordinary append failure restores both files. The binding embeds a privacy-safe recovery receipt so a crash after binding replacement but before event append can replay the exact missing audit event; incomplete rollback reports `recovery_required`, never a false restored claim. While that receipt's `event_id` is absent, only the exact receipt operation may reconcile it; a different operation cannot overwrite pending audit authority.
- The audit event retains only the necessary operation join ref, source epoch, target authority, and SHA-256 digests for authorization/source runtime/source Session/reason. Raw reason and source/auth refs must not be persisted in that recovery event.

`@anchor symbol recover_active_surface`
`@anchor test test_recover_active_surface_rebinds_epoch_without_copying_checkpoint_or_usage`
`@anchor test test_recover_active_surface_replay_parameter_mismatch_fails_closed`
`@anchor test test_recover_active_surface_audit_hashes_private_source_and_authorization_refs`
`@anchor test test_recover_active_surface_event_failure_rolls_back_and_retry_succeeds`

## S0Y explicit dormant-without-checkpoint recovery

- A dormant binding with a current valid checkpoint continues through normal fresh-session adoption; this exception must reject it.
- A `main_only` binding that is dormant, has `checkpoint_revision=0`, no current checkpoint metadata, and no pending continuation/operation may be replaced only by an unstamped non-cron main body with explicit human confirmation, exact source Session, matching owner Agent, and the same bounded recovery refs/target forms as active recovery.
- Recovery increments epoch once and activates the fresh target without synthesizing a source checkpoint, transcript, usage, or coverage. Durable audit must mark the abandoned source segment `unknown_unobserved`, retain only privacy-safe digests, and preserve active-recovery v1 replay compatibility.
- Active, transitioning, recovery-required, checkpointed dormant, wrong-owner, terminal, malformed, conflicting, or partially audited inputs fail closed without silent takeover.

`@anchor symbol recover_dormant_surface`
`@anchor test test_recover_dormant_surface_preserves_unknown_gap_and_rebinds_epoch`
`@anchor test test_recover_dormant_surface_rejects_checkpointed_or_non_dormant_authority`

## Autonomous rollover boundary

- PULSE owns cost/context/quality evaluation; Harness owns durable execution/continuation truth and policy gating.
- default off; explicit continuation remains fallback.
- same-Agent replacement stays on the same root WI and never creates a self-handoff.
- no raw provider identity, historical backfill, or false zero for unavailable usage.
- shadow and suggestion cannot mutate execution.
- autonomous canary requires the PULSE promotion gate plus explicit human promotion.
- recovery_required, conflict, unbound, stale candidate, or missing checkpoint fails closed.

`@anchor symbol ExecutionEntryManager`
`@anchor symbol ExecutionResumeManager`
`@anchor symbol ResumePacket`
`@anchor symbol claim_continuation`
`@anchor symbol release_continuation`
`@anchor symbol consume_continuation`
`@anchor absent CONTINUATION_PROFILE`
