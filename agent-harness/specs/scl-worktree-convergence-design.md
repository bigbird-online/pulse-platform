---
feature_name: "scl-worktree-convergence"
version: "0.8.10"
status: "corrective_rework"
root_work_item: "wi_20260822_479"
created: "2026-08-22T00:00:00Z"
modified: "2026-10-05T17:35:00Z"
owner: "Pulse"
upstream_dependency: ["specs/scl-worktree-convergence-requirements.md"]
tags: [design, scl, worktree, convergence, migration]
---

# SCL Worktree Admission and GitHub-Authoritative Convergence — Design

## Resilience correction

The Broker, CAS, settlement and endpoint machines below become external effect adapters of
`harness-scl-resilience-design.md`. Their safety rules remain; their old synchronous placement in
the local lifecycle transaction does not. A safe local operation commits before these adapters run,
and the reconciler retries them without requiring user choreography. External conflict fences that
effect and any dependent global claim, not ordinary local WI work.

## Delivery state

The previous claim that only terminal evidence remained was incorrect. A live ownermac audit
showed that low-level primitives were mistaken for a wired product path. Service endpoints can
converge, and an isolated Broker transaction can preserve a synthetic dirty repository, while the
ordinary installed Harness still imports editable code from the shared human root and leaves real
Harness and human changes stranded there. Implementation must resume before terminal validation.

## Corrective gap audit

| Promised behavior | What exists | Missing product behavior / live evidence |
| --- | --- | --- |
| All Harness mutations use external authority | Typed projection primitives and a canonical create adapter | `canonical_mutation.py` covers create only; ordinary lifecycle commands can still save YAML directly. WI799 was created in the shared root with no Broker transaction. |
| Human changes are proxy-submitted all-or-nothing | Low-level issue/snapshot/prepare/publish/reconcile commands and synthetic tests | No ordinary orchestration invokes the full state machine. A human deletion under `notices/`, a workspace-file edit and Amanda evidence remained dirty. |
| Harness-owned dirt self-settles | `/wi` resume retries one append-only JSONL settlement | YAML, new WI/handoff/evidence files and non-append changes are ineligible, so the dominant real dirt remains untouched. |
| Guards prevent unauthorized shared-root mutation | pre-commit/pre-push hooks and selected execution-store checks | Hooks guard publication, not filesystem writes; same-account callers and an old CLI bypass the launcher. |
| Installed surfaces run the delivered implementation | Sealed cloud-linux-b release and exact task-local patterns | ownermac `/Users/owner/.local/bin/harness` is an editable uv tool importing `/Users/owner/Coevolution/...`; TCloud lacks an equivalent current installed Harness. |
| Four-end convergence closes the user problem | service-endpoint fixed-SHA convergence | The clean ownermac service endpoint coexists with a stale, dirty human root; no automatic bridge connects the two authority classes. |
| Tests prove the normal product path | extensive temporary-repository unit tests | No real ownermac UAT starts from human deletion/untracked/Harness dirt and finishes through Broker publication with the human root clean. |

The corrective design separates four explicit state machines and wires them through one ordinary
entry rather than treating their individual existence as completion:

1. **Mutation capture:** an installed entrypoint resolves the invocation root before mutation.
   Git-resident canonical operations are serialized to a typed, Git-external request; direct
   `HarnessStore.save_*` on a human root is rejected. The invocation root is fingerprinted before
   and after failure to prove it remained unchanged. Capture does not invoke human-dirt admission:
   unrelated dirt cannot block an operation whose bytes and idempotency journal live outside Git.
2. **Canonical projection:** the Broker materializes the request in its clean service worktree,
   validates operation-specific authority, publishes by CAS and runs the WI-authority replication
   barrier. Success returns transaction, published SHA, receipt digest and freshness boundary.
3. **Human-dirty settlement:** a single typed product command resolves the platform-owned
   `human_work_root` topology, freezes every endpoint manifest, classifies safety/provenance,
   prepares all endpoints, publishes the preservation and protected refs, and reconciles exact
   paths. Settlement is entered only by explicit settlement intent and remains fail-closed for
   stale, partial, unsafe or mixed-provenance requests. Low-level phase commands remain
   recovery/debug surfaces, not user workflow.
4. **Installed/runtime adoption:** release receipts bind immutable artifacts and executable
   readback for ownermac and required remote materializers. Editable imports and implicit source
   checkout execution fail admission. Only installed/task-local exact runtimes may produce UAT
   receipts.

The ordinary entry is now `harness sync-workspace audit|apply-safe|run`. Audit always reads the
complete Git-visible dirty set and keeps ordinary human paths, exact runtime-artifact proofs,
unattributed Git-resident Harness output, operational linked-worktree mounts and nested Git
boundaries distinct. Apply delegates one eligible manifest to `HumanSettlementManager`; it does
not reproduce Broker phases. Exact runtime proofs can be combined with human paths as one `mixed`
provenance group, so one Broker transaction commits the complete group and requires human authority.
`run` loads all routing only from an owner-only, Git-external
`workspace_sync_authorization/v1`, invokes the same apply, then the registered post-publication
service convergence. The scheduled ownermac manifest invokes this same command; removing the
authorization file revokes automatic mutation.

Legacy `160000` entries no longer block unrelated human snapshots merely by existing. A changed
gitlink still blocks. Operational worktree directories are being removed from parent tracking and
ignored by the parent so Agent writes remain visible only in the registered WI root.

Existing dirt is migration input, never test debris. The successor Session must first capture a
read-only manifest and split it by provenance: WI941/Amanda Harness output, WI799/Pulse direct
mutation, explicit human changes (including the `notices/` deletion and workspace edit), and any
unknown paths. It must not reset, stash, clean, restore or combine these groups under invented
ownership. Proven groups use independent Broker transactions; ambiguous groups pause for an exact
human decision.

The 2026-08-28 Hestia-to-WI730 adoption failure exposed a coupling regression in the ordinary
dispatcher: it called settlement admission before every canonical mutation. The same guard also
classified a registered service publisher containing unrelated WI209 files as a human root. The
correct boundary is intent-level: canonical create/update/delete/handoff traffic proceeds through
the immutable publisher and external outbox while leaving all local bytes untouched; only an
invocation carrying `HARNESS_HUMAN_INVENTORY_ID` or another settlement-specific variable enters
settlement admission. An incomplete explicit request still fails before publication.

## Authority flow

```text
/wi -> surface resolver -> worktree registration probe -> routed execution store
                              | invalid/dirty/conflict
                              +-------------------------> no active authority
                              |
                              +-> external authority store (already outside Git)
                              +-> registered linked-worktree .harness
                                      -> binding/checkpoint/surface writes

explicit WI closeout -> GitHub branch == pinned SHA -> probe every endpoint
                                                     | any failure
                                                     +-> external failure receipt, no apply
                                                     |
                                                     +-> revalidate + ff-only each endpoint
                                                          -> external converged/partial receipt

Agent mutation -> typed external request -> Broker validation + BROKER_TX_REF
                                         -> dedicated Broker worktree
                                         -> one CAS publish to human branch
                                         -> resumable endpoint reconciliation
```

The worktree registry is site-local because absolute paths and physical surface layout are
deployment facts, not portable Git truth. Its YAML digest is the durable capability bound
into `WorkItemExecutionBinding`; repository-resident records expose no checkout path.
The conflict scan and atomic registration write run under one non-blocking external-state
`flock`; contention returns `lock_conflict` without mutation.
CLI state namespaces are fixed under the effective OS account's passwd home, rather than a
per-process environment override, so separate invocations cannot silently split the locks.

For a Git-resident Harness root, the canonical shared-root path is treated as a read
projection. After live admission, Harness computes its repository-relative path and maps
it into the same registered linked worktree. The mapping is digest-bound to the registration,
rejects ambiguous repositories and symlink escapes, and verifies that the target is a real
linked worktree. Entry/resume use that routed store; the launcher also replaces the child
`HARNESS_ROOT`, so later lifecycle commands stay on the same ledger. A lower-level execution
or legacy surface mutation aimed at the primary checkout fails before it writes. External
non-Git authority stores are unchanged.

Routing is intentionally one-WI scoped. The launcher exports `HARNESS_WORK_ITEM_ID`, so
`create`/`assign` can reject nested business-object creation before allocating an ID. A
standalone resident creation owned directly by the current Agent requires `main_only`; that
is the only posture compatible with the explicit owner-claim bootstrap. `create` selects it by
default and pins the flag before canonical dispatch; Broker replay of an old flagless create
retains false. The storage model default stays unchanged so old records are not reclassified.
Existing-draft startup checks owner and main-only separately, including read-only planning;
a missing main-only restriction points to the existing audited repair, not a new WI or reinit.
Before registration,
an open `main_only` WI with no executor may resolve only the typed service-control class through
the topology-pinned canonical service authority. That class covers bounded admission, criteria,
status/disposition and execution-plan adoption records; plan adoption freezes its input bytes and
does not grant execution authority. This narrow control-plane bootstrap breaks the otherwise
circular `in_progress → claim-owner → register` dependency, permits accurate disposition and keeps
canonical-ledger governance off retired development branches; all execution-bearing and
collaboration mutations still require exact registered-WI authority. Before a linked
registration is written or validated, Harness maps the linked `.harness` path back to the
registered shared root and verifies a regular canonical WorkItem record with matching
immutable birth identity. Lifecycle fields may later advance on the WI branch, but a
branch-only birth can never obtain executable authority.

For managed WI handoff creation and ACK, the current SCL fence selects the exact sender
registration, epoch and active frontier; a missing or historical legacy binding is not an
alternate authority. Revalidate that frontier after Git readback. The registered producer
remains the publication carrier, while the ACK request preserves the dispatcher-supplied
receiver identity through transport and Broker replay. This does not authorize a receiver
to edit the sender worktree directly. Guarded ACK checks receiver, WI and purpose under the
state lock before any write; its bound caller check does not depend on the legacy rollout
flag. An already accepted guarded request revalidates those checks,
preserves owner/executor and emits no new acceptance event; its successful no-change Broker
projection retains the existing commit. Other no-change mutations remain rejected.
A never-started, unclaimed `main_only` draft has no execution authority to transfer.
Its explicit single-receiver full-responsibility ACK uses canonical service publication:
under the ledger lock, verify the bound recipient and unchanged sender/owner, the absence
of current or retired SCL state, workspace registration and execution binding, then persist
owner, ACK and audit events as one bounded transaction. Canonical Git CAS publishes the
pair against the same preimage as first activation. ACK retains `draft`, creates no executor,
Session, epoch or grant, and returns `next_action.kind=start_standard` for the new owner.
A failed write restores the original pair; a guarded accepted replay never reapplies ownership,
including after the recipient starts. Missing/unreadable authority is not proof of absence.
Published-freeze routing reuses the same prestart absence check when the active kernel
has no WI row. It does not demand a running projection for this Draft, skip a real frozen
frontier because the ledger says Draft, or absorb persistence failures as absence.
First startup reuses the recipient's existing request, including a prior owner-check
refusal. Owner checks are read-only and report `rejected_no_effect`; historical
unknown-effect markers for the existing-draft create phase are recovered by the
same fresh owner/identity read, not by requiring a nonexistent publication receipt.
For any WI with execution history, full-transfer acceptance remains rejected before writes:
changing ledger owner cannot substitute for a sealed SCL ownership transition. Ordinary
non-transferring/partial ACK remains receipt acceptance, never an implicit takeover.

`ack --json` returns `handoff_ack_result/v1`: receipt state and responsibility transfer are
separate from `execution_admission`. ACK grants no workspace authority. For accepted
non-transferring or partial collaboration, `next_action.kind=coordinate_bounded_execution`
names the existing WI owner and the actual receiver on the same handoff. The coordinator
must resolve a bounded execution carrier before business writes; this is an explicit pending
coordination action, not a fabricated worktree grant, full transfer or independent-verifier
assignment. Transport publication and registered-branch readback must complete before the
caller reports durable acceptance; a local branch record alone is not a transport receipt.

The launcher and publisher share an explicit read/write command boundary. Only commands in
the mutation classifier enter the durable publisher transport; read-only queries such as
`log` continue locally and allocate no dispatch record. Historical enum members remain
deserializable only so legacy outbox evidence can be reconciled. Transport completion is
receipt-driven: a nonzero process or timeout without a typed receipt is result-unknown,
persists bounded redacted process evidence, and stops for authority readback rather than
falling through to another endpoint. A dedicated v1 persistence decoder verifies the digest over
the untouched stored request before projection into current models. It supports the missing
optional `actor_id` encoding, the single transitional encoding whose body contains
`actor_id: null` while its digest covers the pre-`actor_id` body, and the pre-freeze absolute
`execution adopt --plan` shape. Plan projection accepts only `.harness/execution-plans/**` and
that legacy request has no frozen input, so it is reconciled to recovery-required and never
replayed. Reconciliation atomically rewrites every recognized projection with its current request
digest even if no lifecycle-state transition is needed. Other absolute paths, unknown shapes, and
digest changes remain fail-closed.

Frozen command inputs are not one interchangeable transport class. Generic evidence may use the
Git-external content bundle, while a repository-semantic reference must be reconstructed inside
the isolated projection at its allowed namespace. `execution adopt` therefore preserves an
already-safe `.harness/execution-plans/**` path or assigns an external source a content-addressed
path below `.harness/execution-plans/.inputs/`, then materializes the frozen bytes there before the
typed command runs. The Broker independently enforces that namespace. A replay of an existing
adoption still appends a canonical audit event and therefore produces a real projection commit.
The installed UAT proved that behavior, so the candidate-only adoption no-diff exception is removed:
every empty mutation projection remains a failure regardless of the command's process exit.

Completion durability includes the typed command response, not only its Git and convergence
receipts. The outbox stores bounded `command_stdout` when reconciling the completed Broker
transaction, so same-key replay can reconstruct the original CLI contract without executing again.
For older completed records the replay path reads the exact linked Broker transaction once and
atomically backfills the missing response; it never guesses from current WorkItem state.

This turns recurring execution ledgers into normal changes on the owning WI branch. They
are reviewed and merged with that WI, then distributed to clean shared roots only by the
pinned-SHA converger. There is no background task racing to commit arbitrary shared-root
changes and no `.gitignore` escape hatch for durable audit records.

The Broker rework separates request authority from Git projection. Agents write typed
requests to a fixed external state namespace. The Broker accepts only registered operation
kinds, binds each request to the exact repository/base/tree and projects Git-resident state
from a dedicated worktree. `BROKER_TX_REF` is an opaque lookup key, not bearer text: hooks
and Broker operations validate its external record, kind, expiry, preimage, staged tree and
single-use state before allowing a human-branch commit or push.

The first deployment deliberately remains `protocol_only`. Pre-commit and pre-push guards
default-deny Agent branches outside the active WI registration and require a validated
transaction on human branches, but a same-account process can bypass local hooks. A later
separately authorized account/credential rollout is required before claiming a hard wall.

## Human-dirty prepare and settlement

Before convergence, the Broker freezes every enrolled human-root preimage. Ordinary
Git-visible content is copied to an external content-addressed snapshot; safety exceptions
block the whole transaction. A dedicated settlement worktree applies all snapshots in fixed
endpoint order and creates one provenance commit per endpoint. Nothing is pushed until the
entire chain is conflict-free. The Broker then performs one compare-and-swap fast-forward
ref update. GitHub movement discards the prepared candidate and restarts from the new head.

Ahead local commits may be published after exact validation. Diverged histories are merged
without rewriting the original commits. After publish, the Broker proves each manifest path
has identical content in GitHub, removes only those exact live paths, and resumes the
existing ff-only endpoint convergence.

## Harness ledger flush settlement

`harness_ledger_flush` reuses the Broker journal and external preparation
worktree, but it is a service authority rather than a human decision. The
classifier first proves that the complete shared-root dirty set consists only
of schema-valid bytes appended to tracked monthly Harness event shards. Its
proof records the exact preimage, append, postimage, ordered event ids and
effective producers; the path alone grants nothing.

The Broker records the remote service-branch SHA before preparation, verifies
that each remote event blob still equals the local preimage, then applies the
captured append in fixed path order and creates one provenance commit. A normal
fast-forward push is the compare-and-swap: a moved remote cannot accept the
candidate. Reconciliation accepts only two restart-safe file states, exact
preimage or exact postimage. In the latter state it removes only the captured
append, then advances the shared checkout by `merge --ff-only`; in the former
it resumes the interrupted merge. The final clean published checkout is
idempotently recognized.

The resume fast path catches only `shared_root_dirty`, asks the Broker to settle
each mechanically eligible registered shared root synchronously, and retries
entry once. A proof error is returned as fail-closed admission; no SCL layer
constructs a commit or broad ignore rule.

## Runtime-artifact ownership receipts

Runtime-managed tracked configuration is neither human dirt nor an append-only Harness
ledger. Its external registry therefore separates two durable objects:

- an immutable exact-path enrollment binding repository root/slug, remote URL, branch,
  service, declared producer and artifact type; and
- a one-time receipt binding those exact enrollment bytes to the current local HEAD,
  current remote authority SHA, Git preimage, live postimage and canonical semantic form.

The ordinary producer path requires the receipt attester to equal the enrolled producer.
The bootstrap path is deliberately different: `historical_import` records the producer as
`unknown`, hashes an explicit human authorization and names the importing actor. It may
classify only that captured postimage and cannot be reused after any byte, HEAD, remote ref
or enrollment change.

The first producer-specific artifact kind is `relay_cron_jobs_v2`. Its semantic validator
requires the live JSON object to declare version 2, contain a jobs list, and give every job
a unique non-empty string id plus a boolean enabled value. Generic JSON remains available
for producers without a narrower contract, but cannot be labeled as Relay cron authority.

`WorktreeManager` consults this classifier only after its normal identity and ancestry
probes find a dirty shared primary checkout. The classifier parses the complete NUL-delimited
Git dirty set and succeeds only for unstaged modifications where every path is enrolled and
has exactly one current receipt. Its typed proofs are returned in workspace validation.
Any mixed or stale state is mapped back to `shared_root_dirty`, preserving the admission
contract. Classification performs no Git mutation: ownership admission and later
GitHub-authoritative convergence remain independent state machines.

## Transaction recovery

The external journal is written before each side effect. Recovery classifies live state as
the recorded before image, the recorded after image, or neither. Before means the step may
run; after means the step is already complete; neither becomes `recovery_required`. A push
that succeeded before process death is therefore discovered by its transaction trailer and
tree hash rather than repeated. Endpoint progress is recorded independently, so an already
converged endpoint is never rolled back to make the operation appear atomic.

The launcher is a controlled protocol entry, not a sandbox. It prints the effective boundary
strength before executing argv directly in the registered worktree. `protocol_only` means a
process running as the same OS user can bypass the launcher; the control plane surfaces that
fact and can be configured to require `hard`.
The current registrar refuses caller-declared `hard`; that value becomes writable only after
an authorized account/ACL rollout adds a durable evidence contract for executable probes.

## Topology and enrollment

One topology describes exactly one independent Git root. It contains:

- a stable topology/repo id and repository slug;
- `authority_kind: github`, exact authority remote URL and branch ref;
- an explicit applicable-WI allowlist;
- required endpoint ids;
- per endpoint: site id, `local` or `ssh` transport, exact checkout path, remote name and
  expected URL, enrollment state, and optional explicit SSH config path.

Machine identity is never inferred from hostname, current directory spelling or absolute
path. The operator populates the external manifest from the workspace-surface and
cross-machine registries, then validates the live Git facts. PULSE, Oikos and
CollaborateWithAI therefore remain separate topologies even when their physical paths share
a parent directory.

Remote identity is also live evidence: every URL path must equal the topology repository
slug, and SSH aliases are resolved with `ssh -G` on the machine where the Git remote exists.
Enrollment rejects non-GitHub or cross-repository identities before writing the topology.
The external state root is checked against every declared checkout before its private lock or
manifest directory is created.

When exactly one required local endpoint is registered, the GitHub authority-ref query runs
in that checkout's Git context. This keeps its configured credential helper available even
when Relay or another caller starts SCL from a different directory. A failed ref query still
returns `authority_unreachable`; it does not prove that a pending mutation had no effect.

### Platform topology and intent resolution

The v2 topology is a platform catalog record rather than a WI capability. It replaces the WI
allowlist with `platform_owner` and requires one `endpoint_class`. The platform owner enrolls
it once; an active WI owner later selects it by `repo_id` or repository slug plus endpoint
class. A v2 topology still describes exactly one Git root. Multiple repositories therefore
resolve to multiple topologies and never share a fixed-SHA receipt.

`harness converge sync` builds a typed request from user intent. Catalog selection first reads
only local manifest metadata. It then authorizes the active WI owner (or the narrowly scoped
WI-mutation platform owner) before endpoint identity verification, SSH alias inspection or any
GitHub probe. It resolves every topology-registered GitHub ref to a full SHA and hashes the
resolved repository/topology/target set into an opaque request id. An optional
`--business-change-ref` is only a bounded correlation token in that identity; it never overrides
the registered branch or target. The ordinary caller never supplies paths, SSH facts, topology
ids or SHAs.

A deterministic lock derived from the resolved immutable request identity serializes the complete
aggregate transaction: aggregate read, all per-topology closeouts, replay comparison and write.
Contention returns `lock_conflict` before a competing result can be written. After the winner
finishes, replay returns the durable byte-stable aggregate or fails closed on a state mismatch.

The durable aggregate receipt is a presentation and orchestration boundary, not a new Outcome
authority. For each repository it records four independent stages: GitHub publication,
Broker preservation, endpoint convergence and Outcome. It references the original fixed-SHA
receipt by path and SHA-256. `endpoint_converged` means only the selected endpoint class read
back clean at the immutable target; `business_complete` remains false and Outcome remains
`not_evaluated`.

### Post-merge deployment gate

`harness converge after-merge` is the stable merge-orchestration entry. It accepts only WI,
Agent and repository intent, fixes `endpoint_class=service_endpoint`, resolves the registered
GitHub branch after publication, and delegates to the same tested typed sync contract. Its
aggregate receipt records `trigger=post_merge`; the trigger participates in request identity,
so an explicit diagnostic sync and a deployment gate at the same SHA remain distinct receipts.
The command never selects human work roots and does not use a timer.

Admission and per-repository application share `_require_sync_authority`. Active execution
retains its exact managed fence/workspace checks. A finally approved managed candidate can
instead consume `require_frozen_delivery`, which reuses settlement's content-bound
`current_authority` and a read-only read of the current managed SCL kernel. A historical snapshot
cannot authorize delivery: a failed best-effort snapshot write must not preserve revoked approval.
Its final-review digest, criteria,
candidate and responsible Agent must agree; selected repository identities resolve through
`CloseoutRepositoryResolver` and must all belong to that approval. Final freeze never becomes
an excuse to restore producer write grants. This also admits the separately requested human
sync. A missing/stale approval, pending replacement, terminal state or unrelated repository
fails before the corresponding endpoint mutation. Readback/CI/Outcome/terminal conclusions
remain separate; the endpoint still enforces dirty/ahead/diverged preservation.

`@anchor symbol require_frozen_delivery`
`@anchor symbol test_frozen_delivery_rejects_changed_authority_before_external_probe`

This code-deployment gate is separate from hot WI control-plane replication. Accepted WI
mutations need a canonical publication barrier followed by record/ledger replication to every
surface. Reads consume a freshness-bearing canonical read model; they do not perform a
repository-wide apply. A stale local checkout is cache state, not evidence that a WI is absent.

### WI mutation publication gate

`harness converge after-wi-mutation` is the typed control-plane replication seam. A v2 service
topology may declare `control_plane_role: work_item_authority`; enrollment rejects that role on
v1 or human-work-root topology objects. Exactly one registered topology may carry the role for
the platform catalog. The command accepts only the mutated WI id and platform Agent, selects the
role-bearing topology, resolves its GitHub authority once, and records `trigger=wi_mutation`.

The registered `platform_owner` authorizes this publisher operation without borrowing the
mutated WI's active execution binding. This exception is scoped to the role-bearing v2 topology;
ordinary explicit and post-merge requests use the active-execution or exact frozen-delivery
authorization above. The
receipt's target SHA is a control-plane `as_of` boundary, and the summary names service endpoints
while stating that human roots, code deployment, release/install, runtime/UAT and Outcome were
not changed or evaluated.

The gate is intentionally a publisher seam, not a read-side Git pull. The canonical mutation
publisher invokes it after accepted publication and exposes pending/partial failure as part of
the mutation result. PULSE/canonical readers consume freshness and coverage from the replicated
read model. Terminal acceptance still requires a fresh production observation of that deployed
producer/consumer path; source tests or a manual Git pull do not prove automatic cross-surface
WI visibility.

## All-preflight then bounded apply

Preflight fetches the already-pinned SHA into `FETCH_HEAD`; this changes no checkout files or
branch ref. It classifies HEAD as `at_target`, `fast_forward`, `ahead` or `diverged`. Only the
first two pass. All required endpoints are probed before apply starts.

Distributed apply cannot be atomic. Each endpoint is re-probed immediately before its
`merge --ff-only`. On the first apply failure, later endpoints are not changed. Live readback
then records the partial shape. A retry sees earlier endpoints as `at_target` and safely
continues. There is intentionally no automatic rollback because that would overwrite a
GitHub-authoritative fast-forward and create a second destructive operation.
The complete receipt read/preflight/apply/readback/write sequence is serialized per topology
by an external non-blocking `flock`; a concurrent caller gets `lock_conflict` even when it
names a different WI authorized for the same endpoints.

## Legacy topology finalization

For v1 closeout topologies, Harness completion changes the registered ledger Git SHA.
The retained topology protocol therefore separates:

1. `converge closeout` proves the reviewed/default-head candidate can converge and produces
   a pre-completion external receipt.
2. Outcome UAT, exact human review, cleanup and canonical `harness complete` land the final
   ledger commit on GitHub.
3. `converge finalize` requires the completed event chain and the prior receipt, converges
   the final GitHub SHA, and writes no Git-resident state.

This legacy topology protocol is distinct from current Phase2 settlement/publication receipts
described in `harness-session-continuation-design.md` section 12.11; neither an endpoint receipt
nor the deployment admission above substitutes for the current terminal validator.

## Executable hard-boundary migration

The current same-principal deployment remains `protocol_only`. A future hard rollout needs
explicit user authorization for these state changes:

1. Create a low-privilege Agent OS account and a root-owned launcher/broker account.
2. Make shared roots broker-owned and non-writable by the Agent account; expose them read-only
   if they must remain visible.
3. Create/chown only registered per-WI worktrees to the Agent account, or apply equivalent
   narrowly scoped ACLs.
4. Run executable probes: Agent `touch` in each shared root must fail; creating and committing
   a file in the registered WI worktree must succeed; an unregistered worktree must fail.
5. Change the site-local registration to `hard` only after those probes and rollback tests pass.

No account, ACL, credential or production-checkout mutation is performed by this feature.
