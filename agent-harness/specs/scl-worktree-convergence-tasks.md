---
feature_name: "scl-worktree-convergence"
version: "0.6.8"
status: "in_progress"
root_work_item: "wi_20260822_479"
created: "2026-08-22T00:00:00Z"
modified: "2026-09-03T00:00:00Z"
owner: "Pulse"
upstream_dependency:
  - "specs/scl-worktree-convergence-requirements.md"
  - "specs/scl-worktree-convergence-design.md"
tags: [tasks, scl, worktree, convergence]
---

# SCL Worktree Admission and GitHub-Authoritative Convergence — Tasks

`harness-scl-resilience-tasks.md` now governs lifecycle availability, local transactions,
idempotency, ingress and recovery. The tasks below remain evidence for convergence primitives and
future effect adapters; they MUST NOT reintroduce synchronous publication/replication as a
prerequisite for local entry, checkpoint or ordinary work.

The checked items below record delivered primitives and historical slices. They do not imply the
ordinary product path is complete. The 2026-08-27 ownermac audit reopened implementation and made
the corrective P0 section the governing execution order.

- [x] Add external WI/surface worktree registration with canonical Git identity probes.
- [x] Bind registration digest/repo ids/boundary strength into execution authority.
- [x] Require owner plus active executor and validate first entry, active reuse, dormant fresh
  adoption, and explicit legacy active-binding attachment.
- [x] Add no-shell controlled launcher and an honest `protocol_only`/`hard` contract.
- [x] Add external independent-repo topology enrollment and pending-endpoint semantics.
- [x] Add pinned-GitHub-SHA all-endpoint preflight, revalidation, ff-only apply, readback and
  private external receipts.
- [x] Add partial-apply receipt, idempotent retry and terminal finalization protocol.
- [x] Cover normal, dirty, ahead, diverged, identity/remote mismatch, partial apply, retry,
  missing registration, conflicting binding, branch/base mismatch and boundary strength.
- [x] Serialize registration and closeout transactions; reject in-Git state roots, unverified
  `hard` claims, non-GitHub authorities and cross-repository endpoint remotes.
- [x] Enroll live CollaborateWithAI, PULSE and Oikos topologies from current machine facts;
  leave missing Oikos tcloud checkout explicitly pending.
- [x] Attach the legacy active WI binding after the implementation worktree is clean.
- [x] Reopen WI479 after Hestia found the dirty-only refusal and Git-resident mutation bootstrap gap.
- [x] Add low-level external typed Harness mutation requests and Broker projection with validated,
  expiring, diff-bound, single-use `BROKER_TX_REF` records; the generic ordinary-command producer
  remains open below.
- [x] Install unified pre-commit/pre-push branch authority guards for WI, human-checkpoint,
  promotion and release branches; preserve an honest `protocol_only` boundary.
- [x] Add low-level all-or-nothing single/multi-endpoint human-dirty settlement primitives,
  safety blockers, CAS publish, local-commit preservation and exact-path cleanup; product
  orchestration and real-root adoption remain open below.
- [x] Add durable transaction states and restart recovery before/after push and during
  endpoint reconciliation.
- [x] Add typed `harness_ledger_flush` append proof, fixed-order prepare, CAS publish,
  exact reconciliation, SCL settlement retry and linked-worktree fail-closed coverage.
- [x] Add reusable platform-owned v2 topology, intent-only `converge sync`, immutable target
  resolution, endpoint-class selection, per-root receipts, aggregate receipt and plain failure
  guidance while preserving the v1 low-level primitive.
- [x] Add typed `converge after-merge` deployment gate that accepts only WI/Agent/repository
  intent, selects service endpoints, records a distinct post-merge trigger and preserves
  release/install/runtime/UAT/Outcome boundaries.
- [x] Add the typed `after-wi-mutation` control-plane gate, unique platform topology role,
  platform-owner authorization, immutable `as_of` target and boundary-explicit receipt.
- [x] Land the create-only canonical mutation publication slice, CAS/replication retry and the
  PULSE freshness-bearing consumer on their repository defaults. This does not cover the remaining
  mutating command set or prove ownermac installed-entry adoption.
- [x] Land the Broker Outcome sparse-projection/materialization repair, build a sealed immutable
  Harness wheel, and apply that exact release through the resident production runtime path.
- [x] Run the prior hosted source, sealed cloud runtime, PULSE deployment and Stats checks. The
  corrective audit invalidated their use as product-path or terminal acceptance evidence.
- [x] Withdraw the stale narrow Oikos verifier handoff, preserve the canonically archived HO523,
  and record human `changes_requested` against the invalidated prior terminal candidate.
- [x] Re-resolve and converge CollaborateWithAI, PULSE and Oikos service endpoints to their
  current GitHub default heads with one fixed-SHA receipt per Git root and aggregate receipts;
  do not select or change human work roots.
## Corrective P0 — complete before any new terminal candidate

- External Stats/source baseline (not WI479 lifecycle or acceptance authority): GitHub default
  `feature/doc-para` is `7f25b98aeca5a6e4f84dc47d177b51163bec9a14` after PR #136
  (`001ad7e88fae0b59c670ba9291e5e3ab2fbefc32`) and PR #138 (`7f25...`). Hosted runs
  `33036373617`, `33036943168`, `33037959122`, and `33038352336` completed successfully. The
  WI300 service-endpoint receipt records ownermac, TCloud, and cloud-linux-b clean and at `7f25...`;
  refresh runs `33038352336` and `33038883851` both succeeded at that exact SHA. The latter followed
  a fresh-success dedup skip and was dispatched by a natural 300-second LaunchAgent cycle, proving
  the fallback continued to run. The current PulseStats snapshot reads `validated/global/fresh`,
  4/4, with Harness source `7f25...`. The exact-tree task publisher uses `git archive` from the
  pinned agent-harness tree and remote `uv --locked --no-editable --project`; the service-owned
  RunAtLoad/300-second fallback dispatches when GitHub schedule events are absent and deduplicates a
  fresh successful run. WI300 checkpoint revision 8 is commit `7f450e1598...` on still-open PR #128
  and WI300 remains in progress; neither is WI479 lifecycle authority. This baseline changes neither
  WI479 authority/Outcome/handoff nor any human root, and cannot satisfy the mutation-proxy,
  settlement, installed-runtime, UAT, or terminal gates below.
- [x] Persist a read-only, content-addressed inventory of all enrolled human roots. For the current
  ownermac root record the 79-path manifest, HEAD/remote/branch, exact bytes or deletion markers,
  ignored/symlink/nested-repository safety checks, and separate provenance groups for WI941,
  WI799, explicit human changes (`notices/` deletion and workspace edit), and unknown paths.
  Evidence: `human_dirt_inventory_v1_4fa0156750ec3f281cebcdd607cbdbf059214f88eb13deedf097ade34ad32ec3`
  persisted outside Git with private permissions and immutable content objects; it records 79 paths,
  group manifests `f73e0956...` (WI941/Amanda), `78eee25a...` (WI799/Pulse), and `d1f2b10f...`
  (explicit human changes), no unknown paths, and preserves the notice as a desired absence. The
  ignored-content finding remains an explicit settlement blocker. Capture and readback left the
  human root at the same 79-path status and did not alter its bytes or index.
- [x] Replace the create-only canonical adapter with one generic mutation envelope covering every
  Git-resident mutating command: create/assign, transition/disposition, handoff/ack/withdraw,
  criteria, execution/checkpoint/continuation, review/Outcome/verify-result and completion. Reject
  unsupported operations before any local write. Evidence: `CommandMutation` validates the complete
  command path and argv before topology probes, rejects root/path escape, and replays the same
  ordinary command below Broker mode. The root Click dispatcher proxies all single-WI mutation
  families before command execution; multi-WI administrative operations remain explicitly rejected
  before write because one registered-WI authority cannot safely stand in for several independently
  owned branches. Create and assign receive Broker-only fixed IDs.
  Corrective UAT reopened this item: an ordinary `execution checkpoint` was routed to the reusable
  `feature/doc-para` WI-birth authority, whose readback has no WI479 execution binding, rather than
  to or from the registered WI branch carrying epoch 9/checkpoint 31. The envelope must bind and
  durably materialize the operation-specific registered-WI ledger authority; argv coverage alone is
  not a complete producer. Current corrective increment: existing-WI commands now resolve exactly
  one Git-external workspace registration, require its manifest digest/runtime surface to match the
  active execution binding, bind that identity into the outbox fingerprint, project only to the
  registered branch, and reconcile the registered root by exact ff-only readback with an external
  receipt. Missing or ambiguous local registration fails before Broker issue. Commits `9a10189de4`
  and `5beac8fba3` freeze every checkpoint/evidence/Outcome file into a size-bounded SHA-256/base64
  bundle, durably materialize it outside the projection worktree, and route a missing local
  registration over a fixed stdin-only RPC to an enrolled SSH surface. The RPC accepts no path,
  branch, producer or shell override; it resolves the exact external registration itself and runs
  from a tracked-only `git archive` whose Git revision and archive digest are checked in both
  directions. PR #162 integrated as `4b36d277972817af54d8a9c4cf42290fdc304fef` after all exact
  hosted gates passed. Its sealed installed entry published checkpoint 33 from a publisher root
  carrying unrelated WI209 tracked dirt as `793ab8989e...` through `broker_tx_v1_e85ab2a6...`;
  explicit settlement intent failed before outbox creation, and the WI209 status and file hashes
  remained unchanged across publication and exact same-key replay.
- [x] Add a central write invariant below the CLI layer so `HarnessStore`/execution managers cannot
  mutate a canonical human root without Broker mode, even when a caller bypasses the normal
  launcher. Prove failed projection leaves the invocation tree byte-identical. Evidence:
  `HarnessStore` classifies the canonical primary checkout separately from linked WI worktrees and
  rejects atomic YAML replacements and append-only events with `shared_root_write_forbidden` unless
  `HARNESS_BROKER_MODE=1`. Direct-save, direct-event, Broker projection, linked-worktree, ordinary
  create and lifecycle-proxy regressions prove the invocation tree remains byte-identical.
- [ ] Implement a durable Git-external mutation outbox with idempotency key, operation schema,
  authority fingerprint, base SHA, requested producer, state machine, recovery reference and
  exact publication/replication result. Remove successful requests only through journaled
  reconciliation; never infer success from local files. Evidence: private
  `harness_mutation_outbox/v1` records are persisted before Broker issue and journal
  `pending → issued → published → replicated → completed` or `recovery_required`. Explicit
  idempotency keys and recovery refs reload the exact WorkItem/envelope and reuse one Broker tx;
  conflicts fail closed. Completed requests remain durable and replay returns only their recorded
  canonical SHA and replication receipt—no request is deleted or inferred from checkout files.
  Corrective UAT proved unissued recovery can now refresh a concurrently advanced base while issued
  requests cannot be rebound, but pre-push typed-command rejection still leaves a Broker transaction
  in terminal `recovery_required`. The current corrective increment retains a bounded typed-command
  rejection code/reason, proves the frozen local and remote preimage, renews the same transaction,
  and retries it without a second transaction or commit; changed preimages remain fail-closed.
  Commit `5beac8fba3` adds a second external dispatch journal that is fsync-persisted before SSH,
  stores the exact tracked-only publisher archive as a SHA-256 object, records each endpoint attempt,
  and reuses the same request/idempotency key and immutable archive after an interrupted response.
  Only an explicit typed `publisher_registration_missing` response may fall through to another
  enrolled endpoint; network, response-shape, identity and publication failures stop with the same
  durable recovery ref. The UAT3 correction additionally makes endpoint exhaustion and an empty
  publisher set terminal `recovery_required` states rather than ownerless `pending`, omits unset
  additive fields from the v1 wire request, stores bounded redacted stdout/stderr diagnostics for
  invalid receipts, classifies a failed process without a typed receipt as result-unknown, and
  provides explicit outbox reconciliation for legacy false-pending records. The same correction
  removes read-only `log` from new mutation dispatch while retaining its historical enum value so
  existing evidence remains loadable. Installed UAT then exposed that pre-`actor_id` outbox
  records carried a valid historical v1 request digest. A second installed UAT found the earlier
  unbound absolute `--plan` record also failed current-model validation. The reader now verifies
  the untouched v1 request first, then projects only the documented former field sets into the
  current model. It also recognizes the one transitional self-written shape whose body contains
  `actor_id: null` but whose digest covers the pre-field body, while continuing to reject unrelated
  digest or path changes. Explicit reconciliation rewrites recognized historical projections to
  their current canonical digest even when the request is already in a terminal state. The next
  real WI730 adoption exposed a separate command-locality defect: its frozen plan was rewritten to
  a generic external bundle even though the typed adoption accepts only repository plans, and an
  already-existing adoption was initially assumed to have no legitimate Git diff. The envelope and
  Broker now preserve and
  enforce `.harness/execution-plans/**`, materialize the frozen bytes inside the isolated projection,
  and retain normal publication, replication and readback. Installed UAT showed that adoption replay
  correctly appends a canonical audit event and publishes a new commit, disproving the candidate
  no-diff assumption; that unused exception was removed and all empty projections still fail. A
  same-key replay against the final installed runtime then exposed that the completed outbox kept
  Git/convergence receipts but not the typed response. Completion now persists bounded command
  stdout, replay returns it without mutation, and older completed records backfill only from their
  exact Broker transaction.
  Cross-surface interruption/retry through the ordinary installed entry remains required, so this
  item stays unchecked.
- [x] Add one intent-level `human_work_root` settlement orchestrator that runs issue → snapshot →
  prepare → preservation publish → protected CAS publish → exact reconciliation. The ordinary
  Agent path must not require manual phase-by-phase Broker commands. Evidence: `broker
  settle-human` accepts a content-addressed inventory and one exact provenance group, verifies
  owner/human authorization and safety, derives the root/slug and preservation ref, then drives or
  resumes the full Broker state machine. Durable `human_settlement_receipt/v1` records and a
  transaction-bound settlement ref make retries idempotent. Protected-ref readback now proves the
  desired deletion/content itself (not only the preservation branch), while remaining provenance
  dirt is content-digest checked before and after ff-only reconciliation. The focused Broker,
  inventory and orchestrator suite passes 57 tests, including tracked deletion/edit, untracked
  content, ignored-in-place, multi-owner preservation, stale/unsafe refusal, and post-publication
  interruption recovery on the same transaction.
- [x] Wire admission, normal Harness commands and Session/checkpoint boundaries to detect unresolved
  human dirt, route eligible/user-authorized manifests to that orchestrator, and return actionable
  fail-closed ownership questions for ambiguous or unsafe paths. Do not silently skip or partially
  submit mixed dirt. Settlement evidence: `human_admission.py` resolves the single primary human root from
  either a primary checkout or linked WI worktree, performs immutable inventory readback, and
  reports exact group/source/owner choices. Explicit `HARNESS_HUMAN_SETTLEMENT_*` authority routes
  one group through the orchestrator and residual dirt remains fail-closed. Workspace admission adds current inventory/group
  guidance to `shared_root_dirty`. Tests prove primary and linked-root detection, stale inventory
  refusal, explicit automatic settlement and non-mutating `--help`.
  Corrective regression: a real Hestia request to adopt a requirement into WI730 was refused before
  proxy publication because unrelated dirt entered settlement admission; the same defect reproduced
  for create and transition, and a registered service publisher with unrelated WI209 files was also
  misclassified as a human gate. The source fix now routes isolated canonical mutations directly to
  the external outbox and invokes human admission only when any settlement-specific variable is
  present; incomplete explicit intent still fails closed. Three command-family regressions plus the
  explicit-settlement negative test pass. The sealed `4b36d277...` release repeated that contract
  against real WI209 tracked dirt: explicit settlement stopped before request creation, ordinary
  checkpoint publication succeeded, same-key recovery was byte-identical, and every dirty byte
  remained unchanged. This closes mutation-versus-settlement admission; it does not authorize or
  complete settlement of ambiguous WI209/WI825 provenance.
- [x] Build and deploy immutable Harness runtimes for ownermac and required remote materializers.
  Remove editable human-root imports from the ordinary executable path; record source revision,
  wheel/source-archive digest, executable realpath and command-surface readback per endpoint.
  Evidence: exact Git-tree build manifest `04efcc3b...` binds source `a5a959df...`, tree
  `4f383f8f...`, archive `c8646cd6...` and wheel `027a840a...`; the builder regression proves
  tracked checkout edits and untracked files cannot enter the artifact. Rollbackable sealed
  installs replaced the editable ownermac uv tool, the cloud-linux-b runtime and TCloud's missing
  ordinary entry. Their independent external receipts all report no editable artifacts, a
  read-only release, exact executable realpaths and identical five-surface command digest
  `d5f60086...`. The latest corrective candidate is source `25e892b2d825...`, wheel
  `60cb5fb747f8...`; Mac, cloud-linux-b and TCloud receipts again report the identical command digest
  `d5f60086...`, no editable artifacts and sealed read-only releases. Runtime parity does not close
  the reopened authority-routing or recovery work.
- [x] Add regression-first coverage for the exact escaped cases: tracked human deletion, tracked
  edit, untracked file, untracked Harness birth, YAML lifecycle mutation, mixed human+Harness dirt,
  local ahead/diverged history, unstable file, concurrent GitHub movement, process interruption
  before/after publish and idempotent recovery. Evidence: the corrective matrix explicitly covers
  a tracked human notice deletion, tracked WI YAML mutation, untracked WI/handoff/evidence births,
  three independent owners, unsafe mixed dirt, stale inventory/Stats materialization, local
  ahead+remote divergence, CAS movement, pre/post-publication recovery and one-transaction replay.
  The new unstable-file test first exposed a real gap; inventory now performs a complete second
  content readback before persisting any object or manifest. The combined matrix passes 96 tests.
- [x] Settle the existing ownermac dirt preservation-first using independent transactions per
  proven provenance group. Do not reset, stash, clean, restore the human deletion, overwrite
  Amanda/Pulse artifacts, rebase, cherry-pick or force-push. Obtain explicit direction only for
  paths whose ownership remains genuinely ambiguous. Evidence: the original 79-path inventory was
  recaptured as `human_dirt_inventory_v1_3494f70d...`; Amanda/WI941, Pulse/WI799 and explicit-human
  groups completed independently through `broker_tx_v1_6d798405...`, `broker_tx_v1_c030a470...` and
  `broker_tx_v1_a58f03b8...`. The notice deletion remains absent, exact Git object readback passed,
  and the ownermac root ended with zero non-ignored dirt. The bounded positive-UAT human change then
  completed separately through `broker_tx_v1_cf1b3cbd...` without reopening the original groups.
- [ ] Run real ownermac ordinary-user UAT from a newly produced bounded human change and Harness
  mutation through Broker publication and required endpoint convergence. Success must end with
  exact GitHub bytes, preserved history, all selected roots clean, one durable transaction chain
  and a freshness-bearing consumer read. Run negative mixed/unsafe cases and prove zero mutation.
  Partial evidence only: ordinary admission published the bounded human file at `b53697474d...` and
  left the human root clean. Mutation outbox `mutation_request_v1_e03389d5...` survived repeated
  lock conflicts and authority movement, refreshed its unissued base, and issued exactly one
  `broker_tx_v1_06ce3ad8...`; projection then failed before commit/push because the selected
  `feature/doc-para` service store has no WI479 execution binding. Checkpoint 32, negative UAT and
  freshness readback remain open; this failed chain is evidence, not acceptance.
- [ ] Repeat installed-entry/readback and the applicable positive/negative UAT on cloud-linux-b
  and TCloud materializer paths; distinguish protocol-only guarantees from any future hard ACL
  rollout. Current evidence: Mac, cloud-linux-b and TCloud installed sealed source
  `4b36d277972817af54d8a9c4cf42290fdc304fef`, wheel `13a3522b5d51...`, and the identical
  command-surface digest. cloud-linux-b performed the registered mutation and read back checkpoint
  34 at `53f125fabd...`; TCloud fetched only that immutable candidate object and parsed the same
  checkpoint SHA through its installed release without moving its clean service checkout. A
  TCloud-originating mutation/recovery attempt remains open where an exact WI registration and
  publisher route are applicable.

  Checkpoint 34 (`e230fa4e752c...`) is the durable dirty-root UAT summary. It keeps the prior WI300
  Stats source `7f25b98a...` as external dependency evidence only; Stats has not yet been refreshed
  for this Harness candidate, so downstream freshness and terminal acceptance remain open.
- [ ] Re-run full source tests, hosted required checks, sealed release/install, production runtime
  adoption, PULSE normal consumer and Stats four-surface freshness only after P0 passes.

## Terminal validation — blocked by corrective P0

- [ ] Re-resolve CollaborateWithAI, PULSE and Oikos exact integration identities and converge only
  the intended endpoint class with fresh fixed-SHA and aggregate receipts.
- [ ] Freeze a new exact terminal candidate binding repository SHAs, Harness artifact/install
  readbacks, PULSE source/install/runtime, human-dirty settlement receipts, required consumer
  readback, service health, downstream effect and Stats coverage/freshness.
- [ ] Create a new main-only Hestia handoff that closes the invalidated HO894; obtain a fresh mixed
  seven-grid `verify-work-outcome/v2` receipt and project `verify-result` on the identical candidate.
- [ ] Present the mechanically verifiable closeout package for exact human review. Only after human
  approval may WI completion, final ledger publication, eligible terminal convergence and
  temporary-resource cleanup run.
