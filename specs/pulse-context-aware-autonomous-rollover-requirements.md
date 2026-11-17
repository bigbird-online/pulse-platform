---
feature_name: "pulse-context-aware-autonomous-rollover"
version: "0.7.0"
status: "proposed"
root_work_item: "wi_20260822_730"
predecessor_work_item: "wi_20260820_272"
created: "2026-08-22"
modified: "2026-08-26"
# history: git log -- specs/pulse-context-aware-autonomous-rollover-requirements.md
owner: "Pulse"
upstream_dependency:
  - "specs/pulse-session-context-lifecycle-requirements.md"
  - "specs/pulse-compute-cost-requirements.md"
external_dependency:
  - "bigbird-online/CollaborateWithAI:Bigbird/Lab/BigbirdEnvironment/agent-harness/specs/harness-autonomous-rollover-requirements.md"
tags: [requirements, autonomous-rollover, multi-surface, resume-fast-path, actions-fallback, shadow, quality]
---

# PULSE Context-Aware Autonomous Rollover — Requirements

This successor begins only after Session Context Lifecycle / `wi_20260820_272` is terminally completed. It must not reopen or mutate the predecessor's terminal scope. WI730 first hardens the multi-surface entry boundary, then the continuation latency exposed by real resident dogfood, and only then proceeds to cost/context/quality autonomy.

## S0 — multi-surface SCL foundation

1. `/wi` resolves the current registered execution surface before choosing an adapter. Unknown/ambiguous surface fails closed; it never defaults to ChatGPT+GitHub.
2. Local machines and cloud hosts are surface instances. Vendor names, hostnames, runner names, and absolute paths are deployment facts, not SCL types.
3. `resident_harness` uses resident compute/Harness for entry and continuation. It does not require Actions merely to establish authority.
4. `actions_bridge` (ChatGPT web) enters through the native external entry (`harness-native entry-request`, `entry`, `entry-confirm`); it uses no GitHub Issue or Actions transport.
5. Runner fallback is not SCL surface migration; resident migration requires durable lifecycle truth.
6. Fallback and entry are idempotent/single-winner.
7. Each workspace route exposes one case-sensitive canonical `agent_id` from the surface registry. Entry/resume defaults to that token; an explicit mismatch fails before Harness mutation. User-facing identity names and Spec `owner` metadata are not machine identity authority.

Current surface registration is data-driven in `config/scl-surfaces.yaml`. The surface registry owns one top-level agent/principal/state profile; surface rows contain only differences.

## S0R — Resume Fast Path

Real tcloud/Feishu dogfood showed a correctness PASS but a usability failure: a fresh Session spent minutes rediscovering workspace, PR, Actions and surface facts although `/rollover` had already persisted the work frontier. SCL must therefore optimize **Time To First Work Action (TTFWA)** as a first-class requirement.

1. **One continuation hot path.** Resident consumers must resolve their descriptor locally and delegate one `harness continue` operation; they must not independently reconstruct `status -> checkpoint -> entry -> repo archaeology`.
2. **Checkpoint-first recovery.** A ResumePacket is the default source for completed work, pending work, decisions, risks/open questions and next action. Chat history and GitHub are not secondary reconstruction sources for facts already present in that packet.
3. **Bounded revalidation.** Before the first WI business action, only packet-declared `stale_if` predicates and `must_read` references may trigger additional checks. Generic “verify current project state” is not allowed in the hot path.
4. **Bootstrap is already done.** The packet must state which surface/entry/checkpoint reconciliation actions were completed by the resume operation so old checkpoint prose cannot send the new Session through them again.
5. **Post-entry next action.** New `/rollover` checkpoints should express the first business action after entry, not “read CLAUDE / inspect GitHub / discover surface / learn entry protocol”.
6. **Audit replication is asynchronous to work resumption.** Once resident Harness execution authority is durably established, Git ledger/PR replication may follow for audit/distribution and final delivery, but must not synchronously block the first business action unless that business action itself depends on repository authority.
7. **No speed-vs-correctness trade.** Stale acceptance/owner/epoch, active surface conflict, recovery-required, transitional state, or missing required checkpoint still fail closed before authority mutation.
8. **Performance gate.** Target TTFWA: resident surface P95 < 30 seconds. Autonomous S1 remains blocked until real-channel dogfood demonstrates the applicable target.
9. **Resident bootstrap is dependency-free.** Surface resolution and resident resume must run with the platform Python standard library before any task-time package installation. The canonical registry may remain YAML-compatible, but its hot-path representation must be directly JSON-readable; repository-wide validators may install richer parsers outside the resident critical path.
10. **ResumePacket changes the Agent work mode.** Once a packet has `resume_ready=true`, a fresh consumer enters `execute`, not generic `orient`. Before the first WI business action it may perform only packet-declared `must_read` reads and `stale_if` checks. Generic repository/PR/Actions archaeology, broad rule rereads, tool-routing ceremonies and speculative audit delegation are forbidden unless the packet's `next_action` requires them.
11. **Control-plane latency is not TTFWA.** Adapter timing ends when the ResumePacket is returned and must be labelled control-plane-only. TTFWA ends only when the fresh Session starts the packet's first business action. Dogfood evidence must record intent/request time, packet-ready time and first-business-action time; a fast `harness continue` receipt alone cannot satisfy the performance gate.
12. **Malformed work frontiers fail closed.** A resume adapter must reject a supposedly ready packet whose `next_action` is blank, whose `must_read/stale_if` fields are malformed, or whose consumer contract does not select `execute`. A checkpoint-free packet may select `orient`, but it cannot be reported as checkpoint-first continuation.
13. **Canonical WorkItem before runnable execution.** A resident adapter must preserve Harness `canonical_work_item_missing/conflict` failures and expose neither ResumePacket nor first-action prompt for a linked-only WI. Stats continues to consume canonical WorkItems; linked execution state cannot silently expand its business-object inventory.
14. **Rollover frontier conservation.** The first target ResumePacket after consumed rollover must be `resume_ready=true` and preserve the exact source checkpoint revision/digest plus completed, pending, decisions, risks, obligations and next action. Exact retry reuses that baseline; stale Session, continuation, phase, epoch or digest fails closed.
15. **Host binding and baseline before first turn.** Relay must durably persist a private target Session plan, pass only its opaque `session_v1_` ref to Harness, publish the runtime usage binding, and spawn the exact bound runtime for live readback. Because that spawn can precede canonical successor adoption, an early Pi baseline failure keeps the process non-runnable. After adoption Relay must invoke the host-only baseline command and independently verify the mode-0600 artifact plus canonical SCL `open` measurement before dispatching the ResumePacket prompt.
16. **Crash-safe first dispatch.** The private host plan, usage binding and baseline gate survive Relay restart. Activation reuses an already-started exact target or starts it once; a failed or pending baseline blocks all business prompts, retries the same target after authority adoption, and never backfills usage. The pending plan is cleared only after the first prompt is accepted. No raw provider identity enters Harness/Git, and a mismatched WI/runtime/binding remains non-runnable.

## S0C — Terminal required-consumer convergence

The WI136 closeout incident proved that canonical completion can still leave a declared production consumer stale. SCL terminal completion therefore needs a machine gate beyond outcome acceptance and Git publication.

1. **Authoritative inventory, not producer choice.** Required consumers are derived from the versioned `config/scl-surfaces.yaml` `terminal_closeout` projection: GitHub/default ledger, every registered `resident_harness` surface, and the real Stats in-flight read model. Settlement binds the registry repo/path/SHA-256; Harness verifies both local bytes and the current-default Git blob. A submitted manifest cannot omit, add, downgrade, retarget, or replace an inventory consumer source.
2. **Published pre-terminal settlement.** Open Phase-2 WIs use `phase2_closeout/v3`. The exact settlement file must already exist unchanged on the current GitHub default head before terminal mutation; branch-local completion YAML is not global truth.
3. **Layered truth.** Git publication, consumer installation, local revision readback, real downstream effect and business-value acceptance remain separate facts. No layer may upgrade another; unknown remains unknown.
4. **Required consumers gate completion.** Every required inventory consumer must report exact revision match plus passed install, readback and downstream-effect probes before the terminal event. An accepted outcome receipt, human review, or producer-authored completed YAML is insufficient.
5. **Cleanup-safe evidence.** Cleanup precedes the final gate. Cleanup, projection, install, readback, and effect refs are repository-relative, digest-bound, and re-read after cleanup; missing, symlinked, escaped, or changed files keep the WI nonterminal.
6. **Execution authority retirement.** Final settlement first moves the binding irreversibly to `terminal`, bound to the settlement ref/digest, then writes the WorkItem terminal event. If the second write fails, replay may finish only the same settlement; entry, checkpoint, resume, or rollover cannot reacquire authority.
7. **Explicit failure states.** Required consumer unavailable, dirty, diverged, revision-mismatched, stale-effect, evidence-unavailable or failed keeps the WI nonterminal and preserves a durable next action. SCL must not reset, stash, clean, rebase, force-push or overwrite a consumer to manufacture convergence.
8. **Checkpoint preservation and single winner.** Checkpoint, rollover, recovery and ResumePacket preserve every unfulfilled obligation and exact next probe. Exact replay emits one retirement/terminal event; concurrent writers are serialized by existing state/WI locks.
9. **Fixed negative paths.** WI136 remains the stale-consumer regression. WI938 additionally proves rejection of producer-omitted inventory, branch-local completion, cleanup-deleted receipt evidence, and a completed WorkItem with active execution authority.
10. **Real Stats effect.** PULSE exposes `probe_terminal_work_item_projection` through the same `collect_in_flight` producer path. It reports only consumer/WI/status/presence facts, treats unavailable as unknown failure, and never substitutes App cache or a mock helper for real read-model evidence.

The platform adapter `Scripts/scl-resume.py` must stay deliberately narrow: local surface-registry read + one Harness resume invocation + timing envelope. It must not query GitHub, PRs, Actions or provider metadata in the resident critical path. It must not bootstrap a package manager or depend on CI-provisioned modules.

## Responsibility boundary for later autonomy

- **PULSE** owns cost/context/quality observation, matched-phase evaluation, recommendation artifacts, promotion evidence, operator-facing suggestion, and TTFWA observation.
- **Harness** owns durable WI execution/checkpoint/ResumePacket/continuation truth, policy gating, single-winner claim/consume/recovery, and human-review/closeout gates.
- **Relay/controller** performs physical Session change and should inject the resolved surface descriptor into the fresh Session rather than asking the Agent to rediscover it.

## Required later stages

1. **shadow** — observe/evaluate only; no execution mutation.
2. **suggestion** — operator-facing recommendation; no automatic continuation.
3. **autonomous canary** — only after mechanical promotion gate plus explicit human authorization.

## Promotion gate

Autonomous promotion is blocked until all are true on at least 6 matched phases:

```text
median(observed_net_saving) > 0
AND accepted outcome quality does not decline
AND human correction / rework do not increase
AND orphan WI/HO delta = 0
AND continuation TTFWA target is satisfied
```

Unknown provider identity, hidden token usage, cash, or pre-entry coverage remains `unknown/unavailable`; never estimate/backfill/write zero. `recovery_required`, conflict, unbound, stale candidate, stale checkpoint, and missing-checkpoint paths fail closed. Cron/model text cannot manufacture human approval.

`@anchor manual scl-resume-fast-path :: local surface resolution + one Harness continue call + TTFWA envelope`
`@anchor manual autonomous-rollover-shadow-gate :: matched-phase cohort, evidence digest, no mutation`
`@anchor manual scl-surface-registry :: provider-neutral surface instances and adapters`
`@anchor manual actions-resource-registry :: capability matching and compatible-pool exhaustion`
`@anchor absent CONTINUATION_PROFILE`
