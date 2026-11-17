---
feature_name: "harness-autonomous-rollover"
version: "0.5.2"
status: "proposed"
root_work_item: "wi_20260822_730"
created: "2026-08-22T00:00:00+0800"
modified: "2026-08-27T13:42:00+0800"
owner: "Pulse"
corrective_work_item: "wi_20260820_136"
upstream_dependency: ["specs/harness-autonomous-rollover-requirements.md", "specs/harness-autonomous-rollover-design.md"]
external_dependency: ["bigbird-online/PULSE:specs/pulse-context-aware-autonomous-rollover-tasks.md"]
tags: [tasks, autonomous-rollover, multi-surface, resume-fast-path, shadow, suggestion, canary]
---

# Harness Autonomous Rollover — Tasks

Current slice: **S0R Resume Fast Path**. The bounded WI730 phase `scl-reconcile-install-uat-v1` is completed: exact managed Harness/Relay/Pi artifacts were installed and read back, the authorized seal/restart-binding repair was deployed, and a fresh genuine production rollover produced one persisted Pi result plus exactly one open and one digest-valid sealed usage snapshot with idempotent retries and cleanup. Checkpoint 29 is the durable phase frontier. Golden Path UX migration, S0R latency cohorts, S1+, and natural terminal closeout remain separate unfinished work; no cost/context/quality autonomous mutation or policy opt-in is enabled.

| ID | Task | Status | Runtime impact |
|---|---|---|---|
| H3-S0-001 | Cross-repo Specs and root-WI boundary | completed | none |
| H3-S0-002 | Generic provider-neutral `ExecutionEntryManager` + `harness entry enter` | completed | entry path only |
| H3-S0-003 | Resident-surface create/reuse/migration/fail-closed regression tests | completed | tests |
| H3-S0-004 | Update intake/dispatch consumers to resolve actual surface before choosing adapter | completed | control-plane behavior |
| H3-S0-005 | Real cloud resident entry bypasses ChatGPT bridge; brokered adapter remains isolated | completed; latency defect found | UAT |
| H3-S0X-001 | Explicit human-main-body active foreign-surface recovery; caller/main_only/source/runtime/ref fail-closed, epoch+1, checkpoint-pointer clearing, paired binding/event persistence with crash receipt, privacy-safe audit, exact retry idempotency | implemented candidate; focused manager/CLI tests pass; no live apply | corrective control-plane CLI |
| H3-S0Y-001 | Explicit human-main-body dormant-without-checkpoint recovery; exact stale source, no checkpoint/transition, epoch+1 fresh target, `unknown_unobserved` source gap, shared crash-safe receipt and exact retry idempotency | implemented candidate; focused manager/CLI tests and full checkout gate pass (`2197 passed, 1 skipped`) | corrective control-plane CLI |
| H3-S0Z-001 | Direct `main_only` owner claim bootstrap before first worktree registration; linked/external store only, shared primary denied, explicit human confirmation and privacy-safe audit | implemented candidate; focused worktree tests and full checkout gate pass (`2203 passed, 1 skipped`) | corrective worktree admission CLI |
| H3-S0R-001 | Add `ResumePacket` + one-call `harness continue` that validates checkpoint then enters/reuses/adopts | in progress; same-Session adoption retry now replays the digest-bound frontier until a new checkpoint | continuation hot path |
| H3-S0R-002 | Make consumers checkpoint-first: bounded `must_read/stale_if`, no pre-work archaeology, bootstrap clauses already satisfied | in progress | intake behavior |
| H3-S0R-003 | Keep Git/audit replication outside first-work critical path; future checkpoint `next_action` is post-entry business intent | in progress | lifecycle contract |
| H3-S0R-004 | Real resident-channel dogfood: TTFWA P95 < 30s with fail-closed invariants unchanged | in progress; Amanda WI941 >9m proves consumer-mode failure | UAT |
| H3-S0R-005 | Brokered ChatGPT dogfood: TTFWA P95 < 60s with compatible Actions fallback | in progress; entry transport passed, end-to-end first action not yet proven | UAT |
| H3-S0R-006 | ResumePacket emits `consumer_work_mode`; root consumer contract maps ready packet directly to execute | implemented candidate | intake behavior |
| H3-S0R-007 | Reject nested/branch-only WorkItem birth and require canonical birth identity before linked registration/admission | completed; merged via CollaborateWithAI #114 and PULSE #254, then exact managed install/readback passed in WI730 checkpoint 29 | worktree admission |
| H3-S0R-008 | Preserve consumed rollover source checkpoint as target resume baseline and accept one host-planned opaque Session ref | completed; checkpoint 28 was consumed into the single epoch-3 target and a fresh production Pi first result was persisted before checkpoint 29 | continuation hot path |
| H3-S0R-009 | Reconcile merged truth, close superseded #111/#248, obtain paired exact-head CI, install/read back managed Harness/Relay/Pi, and run one WI730 real rollover UAT | completed under `scl-reconcile-install-uat-v1`; exact managed artifacts, real rollover, single open/sealed snapshots, digest validation, retry idempotency, privacy and cleanup passed in checkpoint 29 | delivery/UAT only |
| H3-S0C-001 | Add terminal consumer convergence model and `phase2_closeout/v2` required-consumer gate | merged via CollaborateWithAI #110/#112 and PULSE #247/#249; managed install/readback pending | terminal lifecycle |
| H3-S0C-002 | Preserve structured closeout obligations through checkpoint, continuation and ResumePacket | merged via CollaborateWithAI #110 and PULSE #247; managed install/readback pending | session recovery |
| H3-S0C-003 | Normal/failure/optional/N-A/revision/dirty/WI136 regression plus terminal integration tests | completed in merged implementation; exact replay, stale-cache and SHA-256 readback coverage pass | tests |
| H3-S0C-004 | Real required-consumer dogfood: exact install/readback/downstream effect and stale negative | pending | live UAT |
| H3-S1-001 | Privacy-safe continuation-ready candidate + shadow evidence | pending | shadow only |
| H3-S2-001 | Suggestion surface | pending | read-only |
| H3-S3-001 | Human-promoted policy-gated autonomous canary | pending | high-risk opt-in |
| H3-S3-002 | Negative/concurrency/idempotency/recovery/rollback tests | pending | tests/canary |
| H3-S4-001 | Independent UAT + exact human terminal review | pending | closeout |

Next, migrate S0R UX responsibility into one separate canonical `main_only` Golden Path Work Item and keep WI730 blocked+dormant without settling its natural terminal obligations.

Do not begin S1 until S0R passes real-channel latency dogfood. Correctness without low TTFWA is not sufficient: automatic rollover would otherwise amplify restart overhead instead of reducing it.
