---
feature_name: "scl-worktree-convergence"
version: "0.9.9"
status: "corrective_rework"
root_work_item: "wi_20260822_479"
created: "2026-08-22T00:00:00Z"
modified: "2026-10-05T17:35:00Z"
owner: "Pulse"
tags: [requirements, scl, worktree, github-authoritative, convergence]
---

# SCL Worktree Admission and GitHub-Authoritative Convergence — Requirements

## Resilience precedence

This document defines Git publication, repository settlement and endpoint convergence safety. It
does not define local WI availability. `harness-scl-resilience-requirements.md` supersedes every
clause that made successful GitHub publication, endpoint replication, Hook execution or checkout
cleanliness a synchronous prerequisite for conversation, local read, checkpoint or ordinary local
work. In this document, `fail closed` now means **do not perform or claim the unsafe external
effect**; it does not mean strand the WI. Global canonical claims still require the authority and
readback proofs below.

## Acceptance state

A 2026-08-27 live audit invalidated the previous `implemented_candidate` classification.
The repository contains substantial low-level Broker, guard and convergence primitives, but the
ordinary ownermac Harness entry is still an editable `uv tool` that imports code from the shared
human root. It can and does write that root directly. At audit time the root had 79 Git-visible
dirty paths: 5 tracked changes (including the human deletion of
`notices/2026-03-29-sub-oa-template-ownership.md`) and 74 untracked paths, principally stranded
WI941 lifecycle/Outcome material plus a newly created WI799. No current Broker transaction bound
that dirty set. The clean service endpoint therefore proves only endpoint convergence, not the
promised mutation or human-dirty product path.

WI479 is in corrective implementation, not terminal evidence collection. No candidate may be
frozen until every barrier below passes through the installed ordinary-user entry on each
required surface.

The corrective implementation now contains `harness sync-workspace audit|apply-safe|run`, an
owner-only Git-external standing-authorization contract, one atomic human/runtime mixed provenance
group, a ownermac launchd manifest that calls the same `run` executable, and a thin
`four-end-sync` skill. This is implementation progress, not acceptance: installed-runtime release,
real human-dirty success UAT and post-publication four-end readback remain required before candidate
freeze.

## Corrective acceptance barrier

- **No direct canonical writes:** every Git-resident Harness mutating command, including create,
  transition, assign, handoff/ack/withdraw, criteria, execution/checkpoint/continuation,
  review/Outcome and completion operations, MUST write through the local durable operation protocol
  and MUST NOT directly change the invocation root. A `globally_committed` result MUST imply fenced
  canonical publication; a `local_committed` result MAY retain publication/replication as pending
  effects and MUST say so explicitly. Neither result may be inferred from a local YAML write.
- **Generic external mutation producer:** the product path MUST not be a create-only adapter.
  Requests and idempotency state MUST live outside Git; retry MUST reuse one operation and never
  duplicate an event or commit. Projection failure MUST leave the human root byte-identical,
  remain in the automatic effect queue and return its durable effect state. A recovery reference
  is observability, not a requirement for the user to drive recovery.
- **Mutation/settlement independence:** a canonical mutation that is captured before dispatch and
  published from an immutable external publisher MUST NOT be refused merely because its invocation
  root or an enrolled service repository contains unrelated dirt. Human-root settlement is a
  distinct, explicit intent. Any settlement-specific environment input, including an incomplete
  request, MUST enter the fail-closed settlement admission path; absence of that intent MUST route
  the mutation directly to the external outbox without reading, changing or reconciling local dirt.
- **Human-dirty proxy submission:** the ordinary Agent entry MUST classify the complete enrolled
  human-root dirty manifest, including tracked modifications, tracked deletions, non-ignored
  untracked files and preserved local commits. A bounded user-authorized request MUST drive
  snapshot, prepare, CAS publication and exact reconciliation without requiring the user or Agent
  to manually sequence low-level Broker subcommands.
- **Mixed ownership remains effect-fenced:** credentials, ignored content, nested repositories,
  oversized files, unstable bytes, ambiguous provenance or content conflicts MUST preserve all
  original bytes and report the exact blocking paths. The implementation MUST never auto-commit a
  safe subset while hiding unresolved dirt. The affected settlement/publication effect stops;
  unrelated local WI work remains available.
- **Installed-runtime parity:** ownermac, cloud-linux-b and every required materializer/consumer
  MUST use an immutable, revision-addressed Harness runtime or exact task-local sealed artifact.
  Editable imports from a human root are forbidden. Install/source revision, artifact digest and
  executable readback MUST be independently recorded per surface.
- **Real normal-path UAT:** acceptance MUST include the observed ownermac cases that escaped the
  previous tests: a human tracked deletion under `notices/`, a tracked modification, an untracked
  file, a Harness WI create, a lifecycle mutation, mixed human plus Harness dirt, concurrent
  GitHub movement and an interrupted publish/reconcile retry. The success cases must end with the
  intended bytes on GitHub, required endpoints at the exact SHA and the human root clean; pending
  or conflicting effect cases must end with all original dirt untouched while local WI capability
  remains independently testable.
- **No terminal substitution:** unit tests, a clean service endpoint, a fixed-SHA convergence
  receipt, a sealed Vultr install or Stats freshness cannot substitute for these product-path
  results. Only after the corrective barriers pass may the terminal candidate, independent
  Hestia Outcome and exact human review be recreated.

## R1. Write-authority admission

- Explicit source sync and post-merge convergence MUST use the current managed SCL owner,
  active executor, publishable fence and exact clean workspace registration during execution.
  A stale Git WorkItem executor projection MUST NOT veto that proof. Missing or mismatched
  managed authority MUST fail closed without falling back to a legacy binding.
  After native final review releases the executor, the responsible Agent MAY deliver only
  the exact approved candidate's repositories: the current content-bound Outcome/review,
  acceptance criteria and SCL candidate/final-review digest MUST still agree; lifecycle must
  remain awaiting verification with no pending authority or replacement transition. This
  proof MUST be rechecked before endpoint application and MUST NOT reclaim producer execution,
  change Session/epoch/grants, or authorize unrelated repositories. Human roots still require
  explicit sync; endpoint preservation and terminal gates are unchanged.
  Unmanaged WIs retain Git owner/executor/status and active binding checks.
  `@anchor symbol require_frozen_delivery`
  `@anchor symbol test_final_frozen_approval_delivers_to_service_and_human_without_reclaiming_execution`
- An open, unclaimed `main_only` WI MAY use canonical service authority for an explicit bounded
  ledger-only command class: admission/criteria/status control and execution-plan adoption.
  `execution adopt` binds the plan bytes in the mutation envelope, records governance intent, and
  MUST NOT create or consume execution authority. Canonical-ledger adoption and closeout prefer
  current service authority over a stale development registration. This exception MUST NOT
  authorize execution bind, checkpoint, continuation, handoff, review, Outcome, or any other
  execution-bearing mutation, and MUST fail once an executor is claimed or the WI is terminal.
- A direct `main_only` WI that is already `in_progress`, owned by the entering
  Agent, and has no `active_executor` MAY bootstrap its owner claim before the
  first registration. This path MUST require an unstamped non-cron main body,
  explicit human confirmation, bounded opaque authorization/operation refs,
  and a linked WI worktree or external authority store; it MUST NOT write a
  shared primary checkout or claim a non-owner/non-`main_only` WI.
- The first generic SCL entry and dormant-checkpoint fresh adoption MUST validate a
  site-local registration for the exact WI and surface before granting active authority.
- The registration MUST bind every writable repository to a canonical absolute shared
  root, canonical linked-worktree path, Git common directory, branch ref, base commit,
  remote name and exact remote URL.
- The WI owner and active executor MUST equal the entering Agent. Missing, malformed,
  conflicting or changed registration, a dirty shared root, wrong branch/remote/path, or
  unavailable/non-ancestor base MUST fail closed.
- Active reuse MAY contain in-progress worktree changes. First entry, fresh adoption and
  legacy-binding attachment MUST require a clean WI worktree.
- A binding stores only the registration digest, repo ids and boundary strength. It MUST
  NOT copy absolute paths into the repository-resident execution receipt.
- Cross-WI conflict scan and registration write MUST be one external-state transaction.
  Lock contention MUST fail closed without creating or replacing a registration.
- Re-homing an open WI from a peer-owned shared Git common directory to an isolated
  service repository MUST use a canonical registration migration, not a YAML edit. The
  migration MUST bind the exact previous manifest SHA-256, re-check active owner/executor,
  repository identity, linked-worktree membership, branch, base ancestry and cleanliness,
  then atomically replace the external registration. A stale manifest, path conflict,
  dirty candidate or failed probe MUST preserve the previous registration byte-for-byte.
  Expanding an open WI to an additional writable repository MUST use the same manifest-CAS
  migration, preserve every existing repository entry, validate the complete resulting
  registration, and fail without replacement on any duplicate identity, dirty root or stale
  digest. An existing active binding may adopt the new registration only from clean targets
  and with exact previous manifest and session CAS; the migration MUST emit a distinct
  binding event and MUST NOT reinterpret a changed or manifest-less binding as authorized.
- A launcher scoped to one `HARNESS_WORK_ITEM_ID` MUST reject creation or assignment of a
  second WorkItem before writing any business object. On a resident surface, standalone
  direct-owner creation for the current Agent MUST require `main_only=true`; otherwise the
  resulting WI cannot use the documented owner-claim bootstrap and MUST be rejected before
  ID allocation or event emission.
  New standalone `harness create` drafts MUST default to `main_only=true`, including the
  canonical publisher route. The choice MUST be frozen explicitly before new publication.
  `--no-main-only` is an explicit non-standard autonomous-handoff choice; it does not bypass
  direct-owner startup guards. Existing YAML and already frozen Broker requests retain their
  recorded or historical default; upgrading MUST NOT relabel them. An old same-owner draft
  missing the restriction reports `main_only_required`, not `owner_conflict`, before Host
  activation. Its original accepted decision and `mark-main-only` publication are reused.


Verification of new standalone defaults, historical Broker compatibility, and pre-Host admission:
`@anchor test agent-harness/tests/test_standard_draft_defaults.py :: test_new_draft_explicit_or_default_execution_mode`
`@anchor test agent-harness/tests/test_standard_draft_defaults.py :: test_old_broker_command_absence_retains_legacy_false`
`@anchor test agent-harness/tests/test_standard_draft_defaults.py :: test_start_adapter_reports_exact_condition_without_effect`
`@anchor test agent-harness/tests/test_standard_start_admission.py :: test_start_plan_returns_exact_affiliation_gap_before_host_or_journal`
`@anchor test agent-harness/tests/test_standard_start_admission.py :: test_registered_admission_is_consumed_without_replacing_original_input`
- When a Git-resident Harness root is routed through a linked WI worktree, registration,
  migration and every admission validation MUST prove that the same WorkItem already exists
  in the corresponding canonical shared-root Harness store. Canonical and linked records
  MUST agree on immutable birth identity (`id`, creation time, title, source, `main_only`,
  and tenant posture). Missing, malformed, symlinked or conflicting canonical birth fails
  closed without registration or execution mutation. A Git-root-external authority store
  is already canonical and does not acquire this duplicate projection requirement.

A registered first-entry workspace may be locally executable while its original Git audit
publication is pending. A Git-dependent mutation MUST distinguish that exact publication
from a deleted or ambiguous remote branch, await its existing effect without issuing a second
branch creator, and recheck the execution fence before proceeding. An exhausted wait MUST retain
the original input and report publication pending with no mutation sent; it MUST NOT request
new human consent or classify an expected first publication as ownership conflict.


Verification of exact first-publication identity and fail-closed recovery:
`@anchor test agent-harness/tests/test_startup_publication_race.py :: test_missing_branch_without_exact_startup_proof_is_not_recreated`
`@anchor test agent-harness/tests/test_startup_publication_race.py :: test_deleted_previously_published_branch_is_not_resurrected`
`@anchor test agent-harness/tests/test_startup_publication_race.py :: test_changed_execution_fence_stops_waiting`

## R2. Shared-root boundary

- When the configured Harness root is inside a registered repository's shared
  primary checkout, entry/resume MUST map that same repository-relative root into
  the exact registered linked WI worktree before reading or writing execution
  binding, checkpoint, continuation or surface evidence.
- Execution-ledger writes that still target a Git primary checkout MUST fail with
  `shared_root_write_forbidden`. A Harness authority store outside Git MAY remain
  external; a Harness root already inside the registered linked worktree MAY remain
  in place.
- Normal Agent commands MUST enter through a launcher that reuses SCL admission and runs
  argv directly, without a shell, from the registered WI worktree. The launcher MUST
  replace the child's `HARNESS_ROOT` with the admitted execution-store root so later
  checkpoint, rollover and dormant writes cannot fall back to the shared checkout.
- Boundary strength MUST be one of `hard` or `protocol_only`. A deployment where the same
  OS principal owns the shared root MUST be `protocol_only`; `--require-hard` MUST reject it.
- Moving to `hard` requires an explicit account/ACL rollout and executable denial/allow
  probes. Merely adding a path check MUST NOT be represented as an OS permission guarantee.
- Until that rollout and its evidence schema exist, registration MUST reject `hard` rather
  than accept a caller-supplied assertion.
- Continuously produced execution/surface ledger files are ordinary WI-branch changes;
  they MUST be committed with that WI and reach shared roots only through reviewed GitHub
  default-head convergence. Ignoring them or periodically committing a dirty shared root
  is not an admissible substitute.

## R2A. Broker-only Git and Harness mutations

- The ordinary canonical dispatcher MUST classify only state-changing commands. Read-only
  commands such as `log` MUST stay on the local read path and MUST NOT allocate a publisher
  request. A historical mutation enum value MAY remain deserializable solely to preserve old
  outbox records; it MUST NOT make the command dispatchable.
- Agents MUST submit typed mutation requests to a Git-root-external Broker authority.
  Outcome, handoff, criteria and execution-ledger projection to a human branch MUST carry
  a validated `BROKER_TX_REF`; an Agent MUST NOT directly commit or push that projection.
- Agent commit/push authority MUST default-deny every branch except the exact branch in the
  active WI registration. `feature/doc-para`, `dev` and `main` MUST respectively require
  `human_checkpoint`, `promotion` and `release` Broker transaction kinds.
- A Broker transaction MUST bind repository, branch, base SHA, dirty-manifest digest,
  staged-tree digest, source host, kind, expiry and single-use state in external durable
  storage. Invalid, expired, drifted, cross-branch or replayed transactions MUST fail closed.
- Local hooks are a protocol guard, not a hard identity boundary. The same-OS-principal
  rollout MUST remain `protocol_only`; name/email, a missing Agent stamp or free-form text
  MUST NOT be accepted as identity or authority.
- A publisher attempt without a valid typed receipt MUST end in `recovery_required`, retain
  bounded credential-redacted stdout/stderr evidence plus exit status and content digests,
  and require authority readback before retry when the process failed or timed out. Exhausted
  endpoints MUST NOT remain in an ownerless `pending` state.
- Additive optional request fields MUST preserve both wire and durable-outbox compatibility.
  A versioned reader MUST verify the digest against the original stored request before projecting
  an explicitly supported historical field set into current models. A transitional record whose
  body contains an unset optional field but whose digest was calculated before that field was
  materialized MAY be accepted only when both encodings and the exact unset value are recognized.
  Explicit reconciliation MUST rewrite every supported historical encoding to the current
  canonical request and digest even when its lifecycle state is already terminal.
  It MUST reject unrecognized legacy shapes and MUST NOT make a legacy request replayable merely
  to recover its status.
- A frozen file carrier MUST preserve the command's repository-locality contract. In particular,
  `execution adopt --plan` inputs MUST materialize only below `.harness/execution-plans/**` in the
  isolated projection worktree; generic external input bundles and namespace escape MUST be
  rejected. Adoption replay MUST retain its canonical audit event, and an empty Harness projection
  MUST remain a failure for adoption and every other mutation; the Broker MUST NOT infer business
  success from a no-diff command exit.
- A completed command request MUST durably retain its bounded typed command response alongside the
  publication and replication receipts. Same-key replay MUST return that exact response without
  re-resolving authority or re-executing the mutation. A recognized older completed record that
  lacks the response MAY backfill it only from the same immutable Broker transaction; missing or
  conflicting transaction output remains fail-closed.

## R2B. Human-dirty settlement

- Ordinary Git-visible tracked changes, deletions and non-ignored untracked files on a
  human branch MUST be snapshotted and published all-or-nothing. Ignored files, credentials,
  oversized files, nested repositories, out-of-root symlinks or an unstable preimage MUST
  block the entire transaction without hiding or cleaning a safe subset.
- All enrolled endpoint snapshots MUST be prepared together in a Git-root-external Broker
  worktree. A conflict-free result uses one provenance commit per endpoint and one atomic
  compare-and-swap fast-forward update of the GitHub branch. Concurrent authority movement
  invalidates the candidate; content conflict changes neither GitHub nor endpoint roots.
- Ahead/diverged human commits MUST preserve original SHA, author and history. Divergence
  is integrated by a Broker-tagged merge commit; rebase/cherry-pick history rewriting is
  forbidden. Pre-guard untagged history requires explicit bootstrap inventory and human
  confirmation.

## R2C. Harness-owned append settlement

- Harness JSONL event dirt MUST use a distinct service-scoped
  `harness_ledger_flush` transaction and MUST NOT masquerade as
  `human_checkpoint` authority.
- Eligibility MUST be proven from Git bytes, not inferred from an
  `.harness/events/**` pathname. Every dirty path MUST be tracked and modified
  only by an unstaged append to its exact HEAD preimage; each complete appended
  line MUST validate against its declared Harness event schema.
- The transaction MUST bind the local base SHA, remote authority SHA, path,
  preimage size/digest, exact append size/digest, postimage digest, ordered
  event ids, effective producers, expiry and single-use state. Rewrite,
  staged, untracked, mixed/unknown, invalid JSON/schema, concurrent append and
  remote movement MUST fail closed.
- The Broker MUST prepare from the recorded remote authority SHA, publish one
  provenance commit by fast-forward compare-and-swap, prove the published
  postimage, remove only the exact captured append bytes, and reconcile only
  with `merge --ff-only`. Retry MUST reuse an already-published commit and an
  already-reconciled endpoint without producing another commit.
- `/wi` MAY request and wait for this service settlement, then retry admission
  once. `/wi` MUST NOT run Git commit directly, publish global events to the
  business WI branch, ignore event paths, or route any non-proven dirt.

## R2D. Typed runtime-artifact admission

- A tracked configuration file continuously written by a named runtime service MAY be
  admitted as runtime-owned dirt only through an external exact-path enrollment plus an
  exact current producer receipt. Enrollment MUST bind the canonical repository root,
  repository/remote/branch identity, path, service, producer and artifact kind; a pathname
  pattern, directory blanket or `.gitignore` rule grants no authority.
- Artifact-kind validation MUST be semantic and producer-specific where a durable schema is
  known. Relay cron configuration, for example, MUST validate as a version-2 registry with
  unique non-empty job ids and boolean enabled state, not merely as arbitrary JSON.
- A pre-existing postimage whose exact writer has no durable journal MAY receive one
  explicit `historical_import` receipt. It MUST preserve `producer_id=unknown`, bind a
  human-authorization digest and importer, and MUST NOT be replayed for a later postimage.
- Every receipt MUST bind the enrollment bytes, local HEAD, current GitHub authority SHA,
  Git preimage digest/size, live postimage digest/size and canonical semantic digest. A
  changed postimage, HEAD, authority ref, enrollment or repository identity invalidates it.
- Workspace admission MAY classify a dirty shared root as `runtime_artifact_dirty` only
  when the complete Git-visible dirty set consists of unstaged modifications to enrolled
  tracked paths and every path has exactly one matching current receipt. Staged, untracked,
  deleted, renamed, mixed, ambiguous or unattributed dirt MUST retain the existing
  `shared_root_dirty` fail-closed result.
- Runtime-artifact admission proves ownership only. It MUST NOT commit, clean, reset,
  overwrite, reconcile or claim Git convergence; any later producer write requires a new
  one-time receipt, and GitHub-authoritative convergence remains a separate proof.

## R3. GitHub-authoritative closeout convergence

- A topology MUST declare one independent Git boundary, a GitHub authority, one exact
  branch ref, applicable WI ids, and all required machine endpoints.
- Closeout MUST accept a full target SHA and verify that the GitHub branch currently equals
  it. It MUST NOT derive a mutable target after preflight.
- The authority URL and every endpoint remote MUST resolve to `github.com` and the exact
  declared `owner/repository`; an SSH alias is valid only when live `ssh -G` resolution on
  the machine that owns that alias proves the GitHub host.
- Every required enrolled endpoint MUST pass path, Git-root identity, branch, exact remote,
  clean-state, target availability and fast-forward checks before any checkout HEAD moves.
- Apply MUST revalidate and use only `git merge --ff-only <target-sha>`. Ahead, diverged,
  dirty, missing, unreachable or identity-mismatched endpoints MUST never be reset, stashed,
  cleaned, rebased, force-pushed, archived or overwritten.
- A mid-apply failure MUST produce a `partial` external receipt and stop further apply.
  Retry MUST safely accept already-converged endpoints and continue remaining endpoints.
- Each topology's closeout receipt read/apply/write sequence MUST be externally locked across
  all WIs; lock contention MUST fail closed and leave endpoint heads and receipts unchanged.

## R4. Coverage and receipts

- Topology enrollment and convergence receipts MUST live outside every Git root with
  directories mode 0700 and files mode 0600.
- A caller-supplied state root nested in a declared shared root, WI worktree or endpoint
  checkout MUST be rejected before any lock, manifest or receipt is written.
- CLI state roots MUST come from one effective-OS-account namespace that task-controlled
  environment variables cannot redirect to a second lock or registry namespace.
- `coverage_complete=true` is legal only when GitHub is verified and every required endpoint
  reads back clean at the target SHA.
- A pending required endpoint MUST block closeout. In particular, an Oikos topology with no
  tcloud checkout MUST NOT claim four-end coverage.
- PULSE and Oikos MUST use separate topology ids and separate independent-Git-boundary
  declarations from CollaborateWithAI.

## R4A. Intent-level product entry

- The ordinary entry MUST accept only the current WI, caller, one or more repository
  selectors, and an endpoint class (`human_work_root` or `service_endpoint`). A caller MAY
  add a bounded business correlation token. That token participates in request identity only;
  it MUST NOT select or override the topology's branch ref or immutable target. The entry MUST
  NOT require a manifest path, topology id, SSH detail or raw target SHA.
- A `github_authoritative_topology/v2` object MUST be platform-owned, reusable across WIs,
  declare exactly one repository Git boundary and one endpoint class, and contain no WI
  allowlist. Legacy WI-coupled v1 topology remains a low-level compatibility contract only.
- Repository plus endpoint-class selection MUST resolve exactly one v2 topology. Missing or
  ambiguous selection MUST fail before endpoint mutation with a plain-language next action.
- Harness MUST resolve each selected GitHub branch once to a full immutable SHA before apply.
  The resolved request id MUST bind the topology digest and target SHA; callers cannot replace
  either with mutable or task-local input.
- Manifest selection MUST be a pure local catalog operation. Normal WI owner/execution-binding
  authorization, or the scoped WI-mutation platform-owner authorization, MUST succeed before
  endpoint identity verification, SSH alias inspection, GitHub resolution, or other remote probe.
- A multi-repository request MUST retain one fixed-SHA receipt per independent Git root and
  write one aggregate receipt referencing each receipt and digest. One topology MUST NOT
  represent multiple Git roots.
- One deterministic request-scoped external lock MUST cover aggregate receipt read, every
  per-topology closeout, replay comparison, and aggregate write. Contention MUST fail closed;
  a blocked concurrent caller MUST NOT replace a durable endpoint-converged receipt.
- The aggregate receipt MUST separately report GitHub publication verification, Broker
  preservation requirement, selected endpoint convergence, and Outcome evaluation. Endpoint
  convergence MUST set `business_complete=false` and `outcome_status=not_evaluated`.
- The user summary MUST name whether human work roots or service endpoints were selected and
  explicitly state that the other class was not changed. Dirty/ahead/diverged input MUST be
  preserved and return a rerunnable next action without requiring topology vocabulary.

## R5. Lifecycle ordering

- PR merge/publish orchestration MUST invoke the typed `converge after-merge` gate for the
  affected repositories. The gate MUST select only `service_endpoint`, resolve current GitHub
  branch heads to immutable SHAs, and retain per-root plus aggregate receipts.
- Checkpoint, dormant transition, ordinary `/wi` entry, rollover, launcher exit and Session end
  MUST NOT trigger code endpoint apply. They MAY perform read-only freshness checks.
- Accepted WI create/update/state/handoff/execution mutations MUST have a separate canonical
  publication and cross-surface replication effect chain. A local transaction MUST NOT wait for
  that chain to finish; global visibility and terminal/cross-Agent claims still MUST wait for the
  relevant fenced readback. A read MUST expose local/global revisions, freshness/coverage and MUST
  NOT treat one stale local checkout as global absence. Repository-wide pull on every read is not
  the control-plane contract.
- A mutating CLI invoked in the exact local service checkout of the uniquely enrolled
  `work_item_authority` topology MUST use the same canonical publisher as the PULSE ledger,
  including a separately registered test authority. Repository slug alone MUST NOT decide this
  route. Missing or mismatched endpoint identity MUST NOT borrow publication authority.
- If an older CLI left an unpublished `create` WorkItem and its sole creation event untracked in
  that checkout, recovery MUST bind their exact digests, require a pristine draft and absent
  canonical id, preserve the original bytes outside Git, and publish the same WI id through the
  ordinary Broker with stable idempotency. Interruption MUST resume the same recovery; it MUST
  NOT allocate another id, hand-edit the ledger, or describe the local draft as published.
- The typed WI replication gate MUST accept only a WI id and the registered platform Agent. It
  MUST select exactly one reusable v2 service-endpoint topology carrying
  `control_plane_role=work_item_authority`; callers MUST NOT supply repository, endpoint class,
  manifest, topology, SSH facts or SHA.
- Only the topology's registered `platform_owner` MAY invoke that gate without the mutated WI's
  active execution binding. The exception MUST NOT authorize explicit sync, post-merge deploy,
  a human work root, or a v1 WI-coupled topology.
- A WI mutation receipt MUST record `trigger=wi_mutation`, per-root fixed-SHA evidence and an
  aggregate result. Its immutable target is the control-plane `as_of` boundary; summaries MUST
  state that human work roots, code deployment, release/install, runtime/UAT and Outcome were not
  changed or evaluated.
- Canonical mutation producers MUST invoke the WI replication gate after accepted publication
  as a durable effect and return succeeded, pending, partial or conflicting replication with its
  automatic retry state. A manual rerunnable action MAY be exposed for diagnosis but MUST NOT be
  required for normal recovery. Consumers MUST expose canonical `as_of` plus surface coverage. A
  source-level gate without this producer, reconciler and consumer wiring MUST NOT be represented
  as automatic cross-surface visibility.
- A published canonical Broker projection MUST retain its business mutation actor separately
  from the registered platform service actor that performs replication. The service effect MUST
  first prove the original durable transaction, exact enrolled publisher/repository/branch and
  inclusion of its published SHA in current authority; it MUST NOT grant the producer general
  convergence or execution rights. An existing WI-mutation receipt MAY satisfy that original
  effect only when its fixed-SHA digest, current topology digest, platform actor and complete
  required-endpoint observations all match. The original transaction and event identities remain
  unchanged; incomplete evidence requires real convergence, not a fabricated completion.
- After replication, an original acceptance-amendment transaction MUST adopt its exact audited
  before/after criteria into an existing local SCL definition without replaying the amendment.
  Adoption MUST be idempotent, preserve owner, executor, session, epoch, grants and checkpoint,
  and reject mismatched definitions or a frozen/terminal candidate. Source publication,
  endpoint replication and local definition adoption remain separately inspectable facts.
- The one managed-authority fallback for an erroneous claimed WI MUST be restricted to an explicit
  `disposition --to cancelled`. It MUST prove a sole clean registration, matching owner and any
  non-null SCL surface,
  exact absent execution binding and complete absence of a current SCL fence; bind that proof into
  the authority fingerprint; then repeat the same proof after initial convergence and before Broker
  issue. The ordinary CLI, canonical publisher, Broker and endpoint convergence path MUST be used;
  a mocked authority resolver or direct store mutation is not acceptance evidence.
- The production acceptance path MUST demonstrate this contract through the ordinary producer
  and ordinary freshness-bearing consumer. Test-only projection, manual checkout movement, or an
  endpoint-convergence receipt alone MUST NOT satisfy downstream-effect acceptance.
- Legacy convergence remains an explicit `converge closeout` operation for a topology whose
  applicable WI list contains the current WI.
- Because canonical completion writes a final repository-resident ledger commit, the
  completed WI MUST use `converge finalize` after that commit reaches GitHub. Finalization
  requires a prior successful pre-completion receipt plus accepted Outcome UAT, exact human
  approval and `work_item.completed` events, and writes only an external receipt. An absent
  legacy execution binding is valid once those gates are independently proven. If a binding
  still exists it MUST already be terminal and bind the settlement ref/digest; an active or
  otherwise unretired binding MUST fail closed.

## R6. Crash-safe Broker recovery

- The external transaction journal MUST durably distinguish `discovered`, `snapshotted`,
  `prepared`, `pushed`, `endpoints_reconciled`, `converged` and `completed`.
- Retry before push MUST leave GitHub/endpoints unchanged and rebuild from the live authority.
  Retry after push MUST reuse the exact published tree; partial endpoint reconciliation MUST
  continue only missing endpoints. Any preimage/journal mismatch MUST require recovery.
- Shared-root cleanup MUST name exact manifest paths and occur only after byte identity is
  proven in the final GitHub commit. Broad reset, clean, stash, overwrite, rebase or force
  operations remain forbidden; final endpoint movement remains `merge --ff-only`.

`@anchor symbol WorktreeManager`
`@anchor symbol claim_owner_command`
`@anchor symbol attach_active_workspace`
`@anchor symbol GitEndpointRunner`
`@anchor symbol RuntimeArtifactManager`
`@anchor symbol closeout`

### Original event history integration

`harness worktree integrate-event-history` plans an exact two-parent integration
against the registered WI branch. The target must be an ancestor of the current
remote main SHA. Planning uses disposable Git objects and an alternate index:
the live worktree, index, refs and SCL state remain unchanged.

The planner accepts only unchanged, ordered common event documents and preserves
every original document byte. Duplicate IDs, rewritten events, incompatible
orders, competing branch definitions, stale main definitions and non-event
conflicts fail closed. It proves that the resulting merge commit can merge into
the bound current main without conflicts and without losing events or the current
WorkItem definition. The plan includes both working-tree writes and every path
the publication guard will inspect across newly introduced commits.

Applying binds the plan digest, rechecks exact Git identities and requires current
registered execution grants for every write, commit and publish path. It performs
a normal two-parent Git merge and commit with existing hooks enabled, then reads
back the planned tree, parents and clean worktree. Its external receipt records
only source integration; no new lifecycle decision, acceptance amendment,
publication completion or terminal settlement is asserted.
