---
feature_name: "harness-autonomous-rollover"
version: "0.5.0"
status: "proposed"
root_work_item: "wi_20260822_730"
created: "2026-08-22T00:00:00+0800"
modified: "2026-08-26T11:30:00+0800"
owner: "Pulse"
corrective_work_item: "wi_20260820_136"
upstream_dependency: ["specs/harness-autonomous-rollover-requirements.md"]
external_dependency: ["bigbird-online/PULSE:specs/pulse-context-aware-autonomous-rollover-design.md"]
tags: [design, autonomous-rollover, continuation, multi-surface, resume-fast-path, policy, recovery]
---

# Harness Autonomous Rollover — Design

## S0: lifecycle core vs surface adapter

```text
/wi intent
   |
actual surface resolver (consumer/platform registry)
   |
   +-- resident compute --> Harness durable lifecycle
   |
   +-- brokered surface --> adapter/bridge --> Harness durable lifecycle
                                |
                                +-- resource resolver when compute is external
```

`ExecutionEntryManager` is provider-neutral. It composes `runtime_class@surface_id` into the existing v1 execution binding runtime field, preserving backward compatibility while making active authority surface-specific. Surface IDs are registry tokens; Harness does not interpret vendor or hostname names.

ChatGPT+GitHub remains a specialized observation/transport adapter. Its durable receipt proves that the adapter reached Harness; it is not a universal WI gate for resident surfaces. A dormant checkpoint may be adopted onto another registered surface through the existing same-Agent adoption primitive. That is explicit surface migration from durable checkpoint, not runner fallback and not a new WI/handoff.

### S0X: active foreign-surface recovery exception

Normal `entry` continues to reject an active binding on another runtime. When a human has explicitly retired that old surface, `ExecutionManager.recover_active_surface` provides one narrow exception:

```text
unstamped non-cron main body + main_only WI + explicit human confirmation
  + explicit owner Agent + exact active source Session
  + bounded opaque human authorization join ref + operation ref + hashed reason
  + (caller-resolved runtime class/surface token OR exact runtime/Session)
  -> acquire global state lock, then WI lock
  -> verify open WI owner == --agent == binding Agent
  -> require ACTIVE + exact source + different target runtime
  -> epoch + 1; preserve phase; clear current checkpoint pointer; embed replay receipt
  -> paired-save binding + privacy-safe execution.active_surface_recovered event
```

Harness does not read a registry in this method. It checks only token shape; target registry resolution remains an upstream/operator precondition. In runtime-class/surface mode it derives a deterministic opaque Session ref from the WI, operation ref, and composed runtime. That gives a retry the same target identity without retaining private provider identifiers.

`human_authorization_ref` is an opaque audit join key, not proof from an external authorization registry. The enforceable authority is the explicit human-main-body posture: no dispatcher-stamped Agent, no cron, a `main_only` WI, and required human confirmation. Bounded recovery refs reuse continuation privacy validation and add recovery-specific rejection for paths and provider-token/private markers.

Idempotency is receipt/event-bound. The request digest covers caller, source Session, target runtime/Session, human authorization ref, operation ref, and reason digest. A replay returns the existing binding only when the digest and current target authority match; any changed input conflicts. The event retains raw operation ref only as the necessary join key; authorization, source Session, source runtime, and reason are SHA-256 digests alongside `source_epoch`.

The store snapshots binding and event bytes, then performs the binding replacement and event append as a paired transaction. Caught failures restore both snapshots. The new binding also embeds the event identity and privacy-safe recovery receipt: if a process exits after the atomic binding replace but before append, an exact retry reconstructs and appends the missing event. Receipt reconciliation is checked by `event_id` before considering any requested operation; while that event is absent, a different operation returns `recovery_required` and cannot overwrite the receipt. If rollback itself is incomplete, the manager returns `recovery_required` rather than claiming bytes were restored.

Historical checkpoint files and surface-observation files remain audit evidence. Recovery clears the binding's current checkpoint pointer but neither deletes old evidence nor creates a target observation, so historical coverage cannot be mistaken for new-surface coverage.

The sibling `recover-dormant-surface` command covers one otherwise unrecoverable
legacy shape: the exact authority is already dormant and has never produced a
checkpoint. It reuses the same human-main-body, owner, privacy, locking,
transaction, receipt, and idempotency controls, but additionally requires
`checkpoint_revision=0`, no checkpoint pointer, and no continuation/operation.
It never synthesizes a source checkpoint or history. The recovery event records
`source_lifecycle=dormant` and `source_coverage=unknown_unobserved`, then creates
one fresh active target at epoch +1. Dormant bindings with checkpoint history
must use normal checkpoint adoption and are rejected by this exception.

`@anchor symbol recover_active_surface`
`@anchor symbol recover_dormant_surface`
`@anchor test test_recover_active_surface_uses_single_wi_lock`
`@anchor test test_recover_active_surface_rejects_same_runtime_session_swap`
`@anchor test test_recover_active_surface_replays_a_post_binding_crash_receipt`
`@anchor test test_recover_active_surface_blocks_new_operation_until_crash_receipt_is_audited`
`@anchor test test_recover_dormant_surface_preserves_unknown_gap_and_rebinds_epoch`

## S0R: Resume Fast Path

The consumer should not independently perform `status -> inspect checkpoint -> inspect surface -> enter -> rediscover PR/Actions -> continue`. The normal path is one operation:

```text
continue WI
  -> validate current WI + checkpoint authority
  -> enter/reuse/fresh-adopt actual surface
  -> canonical authority store durably accepts single winner
  -> emit ResumePacket
  -> validate only packet.stale_if / packet.must_read when needed
  -> FIRST WI BUSINESS ACTION

  -------------------------------------- async / later --------------------------------------
  -> repository audit/replication / normal delivery evidence
```

`ExecutionResumeManager` composes the already-authoritative `ExecutionEntryManager`; it does not create a second execution ledger. For dormant bindings it captures the checkpoint before adoption clears the current pointer, validates acceptance/owner/epoch first, then returns that exact work frontier together with the new active binding. The `HarnessStore` passed to the manager is therefore required to be the configured authority store for that execution path, or the calling adapter must complete its bounded canonical publication/CAS step before exposing the packet as runnable.

Consumed rollover apply uses the same resume mechanism explicitly: it moves the source checkpoint's exact revision/ref/digest into `resume_checkpoint_*` while incrementing the epoch and clearing `latest_checkpoint_*`. Resume accepts that one-epoch difference only after reloading the consumed continuation and matching WI, Agent, target phase and consumed target Session. A host-planned opaque Session ref may be supplied at entry; it becomes the binding ref before the host starts the target process, while a conflicting retry is rejected.

A source-control PR is not the definition of execution authority. If a deployment currently mirrors the authority store through Git, cross-surface publication needs a bounded atomic/compare-and-swap transport; waiting for an ordinary reviewed product PR and its full CI would reintroduce the latency defect. Audit/distribution replication after the authority commit can remain asynchronous to the first business action.

`ResumePacket` is deliberately small and operational:

```text
identity: WI / owner / epoch / phase / surface
frontier: completed / pending / decisions / risks / open questions
next: checkpoint next_action
bounded reads: must_read
bounded freshness: stale_if
bootstrap receipt: control_plane_completed
anti-archaeology: do_not_rediscover
```

Old checkpoints may contain surface-specific bootstrap prose. `control_plane_completed` makes those entry/reconcile clauses explicitly satisfied after resume; consumers execute the remaining business intent instead of replaying startup archaeology. New checkpoints should make `next_action` post-entry and surface-neutral.

Fresh adoption also persists a digest-bound replay pointer to the consumed source checkpoint in the active binding. A same-Session retry reloads that frontier and returns `reuse_current + execute` without creating another Session or event. The first checkpoint produced by the new Session atomically replaces the replay pointer with current checkpoint metadata; recovery or a later rollover clears it, so an old frontier cannot cross an epoch boundary.

The latency observable is TTFWA: user continuation intent -> first WI business action. Control-plane validation and canonical authority publication belong in TTFWA; later Git/PR audit replication does not. Correctness is not traded for speed: stale acceptance/owner/epoch, recovery-required, conflict, or missing required checkpoint still fail closed.

The packet makes the consumer decision explicit:

```text
resume_ready=true  -> consumer_work_mode=execute
resume_ready=false -> consumer_work_mode=orient
```

A ready consumer validates `next_action`, `must_read`, and `stale_if`, then begins that action. Generic orientation cannot silently widen the pre-work graph. Harness exposes only control-plane readiness; channel/runtime evidence supplies the later first-action timestamp for end-to-end TTFWA.

## S0C terminal required-consumer convergence

`phase2_closeout/v2` embeds a `TerminalConsumerConvergenceManifest`:

```text
accepted outcome + human exact-candidate review + default authority
        |
        v
canonical integrated revisions
        |
        +-- required consumer install evidence
        +-- exact local revision readback
        +-- real downstream effect evidence
        |
        +-- all required passed -> existing terminal transition/event
        |
        +-- any required open/failure state
                -> invalid_closeout
                -> WI remains awaiting_verification
                -> checkpoint carries closeout_obligations
                -> rollover/ResumePacket preserve next probe
```

`ConsumerConvergenceObligation` is shared by checkpoint, ContinuationBundle, ResumePacket, and terminal evidence. Stable mechanism ids describe how an external owner performs install/readback/effect probes; Harness never executes those ids. The pure validator admits a required consumer only at exact revision with all three evidence layers. The terminal manifest separately inventories every probe ref with SHA-256; closeout resolves each ref under the repository root and re-hashes the bytes, so missing, escaped, extra, or tampered evidence fails closed. Optional rows remain visible but non-blocking; N/A rows contain only identity and reason.

The existing WI lock remains the single-winner boundary. Validation precedes the terminal transition, so failure cannot emit a completion event. The terminal event binds both outcome-receipt and closeout-evidence digests; an exact completed-request replay returns the prior terminal result without another event, while changed evidence fails closed. Dirty/diverged/unavailable evidence is preserved and surfaced as the next durable probe; no destructive repository recovery is part of this state machine.


## Later autonomous control loop

```text
PULSE evaluator -> recommendation / authorized intent -> Harness policy gate -> existing continuation claim/consume/recovery
```

No second lifecycle ledger. Shadow/suggestion remain read-only. Autonomous canary reuses the Phase 2 controller, single-winner claim, and fail-closed recovery. The evaluator is not started until S0R real dogfood proves the continuation path is fast enough to make autonomous rollover economically useful.

`@anchor symbol ExecutionEntryManager`
`@anchor symbol ExecutionResumeManager`
`@anchor symbol ResumePacket`
`@anchor symbol ContinuationContract`
`@anchor symbol acquire_lock`
`@anchor symbol claim_continuation`
`@anchor symbol release_continuation`
`@anchor symbol consume_continuation`
`@anchor absent CONTINUATION_PROFILE`
