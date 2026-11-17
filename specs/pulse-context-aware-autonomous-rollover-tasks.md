---
feature_name: "pulse-context-aware-autonomous-rollover"
version: "0.6.3"
status: "proposed"
root_work_item: "wi_20260822_730"
created: "2026-08-22"
modified: "2026-08-27T13:42:00+0800"
# history: git log -- specs/pulse-context-aware-autonomous-rollover-tasks.md
owner: "Pulse"
upstream_dependency:
  - "specs/pulse-context-aware-autonomous-rollover-requirements.md"
  - "specs/pulse-context-aware-autonomous-rollover-design.md"
external_dependency:
  - "bigbird-online/CollaborateWithAI:Bigbird/Lab/BigbirdEnvironment/agent-harness/specs/harness-autonomous-rollover-tasks.md"
tags: [tasks, autonomous-rollover, multi-surface, resume-fast-path, actions-fallback, shadow, canary]
---

# PULSE Context-Aware Autonomous Rollover — Tasks

Current slice: **S0R Resume Fast Path**. The bounded WI730 phase `scl-reconcile-install-uat-v1` is completed: exact managed Harness/Relay/Pi artifacts were installed and read back, the authorized seal/restart-binding repair was deployed, and a fresh genuine production rollover produced one persisted Pi result plus exactly one open and one digest-valid sealed usage snapshot with idempotent retries and cleanup. Checkpoint 29 is the durable phase frontier. Golden Path UX migration, S0R latency cohorts, S1+, and natural terminal closeout remain separate unfinished work; no cost/context/quality autonomous mutation or policy opt-in is enabled.

| ID | Task | Status | Runtime impact |
|---|---|---|---|
| P3-S0-001 | Bind cross-repo successor Specs and freeze predecessor `wi_20260820_272` | completed | none |
| P3-S0-002 | Data-driven SCL surface registry/resolver; resident vs brokered entry separation | completed | entry routing |
| P3-S0-003 | Actions capability registry/resolver; hosted -> compatible self-hosted fallback semantics | completed | broker planning |
| P3-S0-004 | Automatic ChatGPT entry fallback with idempotent durable publish | completed | brokered entry |
| P3-S0-005 | Real resident cloud entry bypasses GitHub gate + broker fallback dogfood | completed; latency defect found | UAT |
| P3-S0R-001 | `Scripts/scl-resume.py`: local registry -> one `harness continue` -> ResumePacket + timing | in progress | resident hot path |
| P3-S0R-002 | Checkpoint-first consumer contract: bounded `stale_if/must_read`, bootstrap already satisfied, no pre-work archaeology | in progress | session startup |
| P3-S0R-003 | Move Git/ledger audit replication outside first-work critical path unless business authority requires it | in progress | lifecycle/delivery boundary |
| P3-S0R-004 | Real Feishu/resident continuation dogfood; resident TTFWA P95 < 30s | in progress; Amanda WI941 still spent >9m before first action, consumer contract failed | UAT |
| P3-S0R-005 | ChatGPT brokered continuation dogfood; TTFWA P95 < 60s | in progress; atomic #175 passed in 52s and #186 passed in 33s, P95 cohort incomplete | UAT |
| P3-S0R-006 | Expose durable workflow run id and attribute broker stage queue latency before any routing change | completed; #138 isolated 256s before trusted fallback execution | observability |
| P3-S0R-007 | Prefer the existing trusted Linux pool for latency-critical preflight/publish; retain hosted 0-step fallback and rerun contention UAT | completed; PR #140, Idle and contention routes attributed | broker routing |
| P3-S0R-008 | Replace ad-hoc ChatGPT rollover reconstruction with a persistent atomic issue fast path; TTDR < 60s Idle UAT | completed; atomic #174 passed in 37s after #171 staged route took 502s | brokered rollover |
| P3-S0R-009 | Remove cold-start Agent token ambiguity with registry-derived canonical `agent_id`; reject explicit mismatch before Harness | completed; PR #187 + #186 33s entry + three-resident-surface deployment #198 | entry/resume hot path |
| P3-S0R-010 | Remove CI-only parser dependency; deploy exact startup payload to ownermac/tcloud/vultr with dirty-release isolation and idempotent retry | completed; PRs #192/#194/#197, Issue #198 run 32633696948 | resident deployment |
| P3-S0R-011 | Normalize one-agent registries; prove Actions profile/pool/workflow projection; add pre-mutation admission and post-binding attestation | completed; PR #211 + Issue #212 real default-head UAT | brokered authority |
| P3-S0R-012 | Bind ready ResumePacket to consumer `execute`; prohibit pre-work archaeology and expose first-action contract | in progress | session startup |
| P3-S0R-013 | Measure end-to-end TTFWA through first WI business action; keep adapter timing control-plane-only | in progress | observability/UAT |
| P3-S0R-014 | Implement authoritative inventory, published pre-terminal settlement, cleanup-safe evidence, real Stats effect probe, and terminal execution retirement | merged via PULSE #247/#249 and CollaborateWithAI #110/#112; managed install remains pending and natural closeout dogfood stays outside the bounded phase | terminal closeout |
| P3-S0R-015 | Reject nested/branch-only WorkItem creation; require canonical birth before linked registration/entry and preserve Stats canonical inventory | completed; merged via PULSE #254 and CollaborateWithAI #114, then exact managed install/readback passed in WI730 checkpoint 29 | SCL admission |
| P3-S0R-016 | Preserve consumed rollover frontier and durably bind/recover the physical target before first ResumePacket turn | completed; checkpoint 28 was consumed into the single epoch-3 target and a fresh production Pi first result was persisted before checkpoint 29 | resident resume/runtime |
| P3-S0R-017 | Reconcile merged truth, close superseded #248/#111, obtain paired exact-head CI, install/read back managed Harness/Relay/Pi, repair the real-channel seal/binding defect, and rerun one WI730 real rollover UAT | completed under `scl-reconcile-install-uat-v1`; exact release `e6359c9f97d9595f767b304cfd23d8895376fac9`, real rollover, single open/sealed snapshots, digest validation, retry idempotency, privacy and cleanup passed in checkpoint 29 | delivery/UAT + bounded defect repair |
| P3-S1-001 | Define matched-phase cohort query and privacy-safe input snapshot | pending | shadow only |
| P3-S1-002 | Build deterministic cost/context/quality evaluator + recommendation artifact | pending | shadow only |
| P3-S1-003 | Run >=6 matched-phase shadow samples and evaluate promotion gate | pending | shadow only |
| P3-S2-001 | Add suggestion-only surface with dismiss/no-response idempotency | pending | read-only suggestion |
| P3-S3-001 | Add explicit human promotion gate + narrow Harness policy opt-in | pending | high-risk canary |
| P3-S3-002 | Autonomous canary normal/failure/concurrency/recovery/rollback tests | pending | opt-in canary |
| P3-S4-001 | Independent outcome UAT, exact human review, default-head closeout and cleanup | pending | closeout |

Next, migrate S0R UX responsibility into one separate canonical `main_only` Golden Path Work Item and keep WI730 blocked+dormant without settling its natural terminal obligations.

Do not begin P3-S1 until S0R real-channel latency dogfood passes. A continuation system that is correct but takes minutes to regain work momentum is not ready to trigger rollover autonomously.
