---
version: 2.1.4
owner: pulse
status: implementation_candidate
---

# Managed workspace contract

One host-owned contract covers allocation, registration, runtime admission and retirement.
It is independent of Pi, Codex, Claude, channel and service checkout placement.

## Allocation

`harness worktree allocation <wi> --agent <owner> --repository-slug <org/repo> --json`
returns a read-only `planned` destination, not a created worktree or execution claim.
Without a site placement policy, the only new destination is the sibling of the account's
canonical worktree-registration state root:
`workspaces/<org>/<repo>/<wi>/<owner>`. On the default macOS account this is
`~/.local/state/agent-harness/workspaces/<org>/<repo>/<wi>/<owner>`.

An active WI owner may bind the host to one dedicated allocation filesystem with
`harness worktree configure-placement`. The account-owned
`workspace-placement.json` records the exact real allocation root, host, effective uid,
mount point, stable volume UUID when macOS places it under `/Volumes`, authorizing WI and
configuring agent. Every later allocation and capacity admission revalidates that identity.
An absent, renamed, substituted, read-only or foreign-owned volume fails closed; it never
falls back to the system disk. The configured root, rather than the registration state
filesystem, is the capacity measurement and inventory root. Existing registrations retain
their exact old paths until an authorized CAS migration replaces them.
On an account's first start, the unconfigured default root is prepared as an empty,
account-owned private directory before capacity admission measures it. A configured volume
is never created by startup; its exact recorded path must already pass the placement check.
The installed Oikos capacity guard remains a separate required host dependency: absence
reports `capacity_unavailable`, preserving the same start journal and recovery key.

No worktree creation command accepts an ad hoc placement root. Identity components cannot
contain traversal; symlink traversal is rejected. A site placement root must remain outside
all source repositories.

The ordinary `start-local` coordinator obtains this allocation, admits capacity and publishes the complete
registration set under the first-bootstrap lock. It includes the authority repository internally.
Low-level callers obtain this allocation before creating a linked worktree. They reuse the same target
for the same repository/WI/owner, choose the least required checkout content, and obey the
existing capacity admission rules. The query does not perform clone, checkout, claim, or capacity
admission. Shared human roots, service repositories and immutable releases are not WI workspaces.

Final consumer observation is an explicit startup recovery phase. A typed readback failure
must preserve the same start journal, WI and all completed phase receipts, report `observe`
as the failed phase with its original code and retryability, and retry observation without
replaying creation, admission, allocation or apply. Completed phase receipts alone never
substitute for current execution authority.

## Materialization scope

Git identity remains repository-wide, but new worktrees do not materialize the whole repository.
`start-local` writes one Git-external `workspace_materialization/v1` manifest per registered
repository before it returns the workspace. The manifest binds WI, owner, surface, repository,
shared root and exact linked-worktree/common-dir identity, basis revision, `required_paths`,
the minimal concrete excluded-directory boundaries, scope generation/reason, estimated allocated
KiB and expansion time. Manifests
and apply receipts live below the account's canonical worktree state root; they never dirty a
business branch.

The default scope is the owner's `ssot/registry.yaml` workspace relative to the selected nested
repository. That registry is read from the one enrolled local `human_work_root`; it is not inferred
to live beside the canonical Harness authority. Repository ids in the human registry may differ
from service-topology ids, so the selected nested repository is matched by its stable aliases
(including repository slug/basename) and ambiguity fails closed. The authority repository, when
used only as control plane, materializes `.harness` and the lifecycle skill. If neither an owner
workspace nor a caller scope belongs to a business repository, only root files are materialized;
the implementation must not fall back to a full checkout. A start request may provide
`workspace_scopes[]` with one repository selector,
one or more tracked directory paths and a reason. Absolute paths, traversal, `.git`, redundant
descendants, missing directories and ambiguous repository selectors fail before checkout.
`PARA`, `Doc` and any other top-level directory have no special trash status: they are present
only when the WI scope requires them.

Capacity admission prices the planned sparse trees plus fixed worktree overhead. An empty/root-only
scope prices root blobs, not a recursive repository tree. The workspace plan is the first durable
workspace-allocation artifact. If a startup attempt fails before that atomic plan exists, recovery
may recompute the same plan only after proving there is no registration, materialization, allocated
path, or deterministic startup branch for that request; otherwise the unknown effect remains
fail-closed. Imported start journals created before this contract retain their original estimate and
checkout behavior so recovery never rewrites an already durable request.

For a clean legacy registered worktree, the active owner may establish generation 1 with
`harness worktree initialize-scope`. `compact-scope` reapplies only the recorded manifest.
Both refuse dirty/untracked content, identity drift, unavailable process-reference inventory,
or any CWD/open file below a path that would be de-materialized. They use Git sparse-checkout,
never raw recursive deletion. Scope growth is a distinct `expand-scope` CAS requiring the current
generation, explicit added directories and a reason; it only grows the required set and increments
the generation. Compaction refreshes the basis and exclusion inventory from the worktree's current
HEAD before applying the recorded scope. No command automatically broadens scope because a file
was requested at runtime.

This running-WI compaction is separate from terminal retirement. It removes reproducible tracked
material from the worktree view while retaining the worktree, branch, registration and WI. The
terminal reclaimer remains the only mechanism that removes the whole linked worktree after its
existing lifecycle and preservation proofs.

## SCL handoff publication

For `handoff` on a managed WI, the canonical publisher consumes the current fenced
SCL registration and execution frontier directly. Exact owner, executor, lifecycle,
active execution state, epoch, Session, surface, registration digest and registered
Git branch must agree; authority is checked again after Git readback. An absent or
historical `.harness/executions` projection neither blocks this route nor grants it
permission. The publisher records `authority_source=scl` with the current epoch and
Session in its authority payload and creates no compatibility binding.

An unclaimed frozen candidate retains its existing single-verifier handoff rules.
Namespace policy, prior-handoff disposition, responsibility transfer, Broker CAS and
publication checks still apply. Unmanaged WIs retain legacy binding admission; an
expired, missing, transitioning or conflicting SCL authority never falls back to it.

## Registration

The shared `scl_registration_evidence` boundary authenticates the kernel-admitted
original bytes against the current registration digest before decoding resource identity.
A stored parsed copy has no authority and is no longer written on new admissions.
Worktree readers use the original; missing, malformed or divergent registry views are
rebuilt by the existing registration/materialization path, without an extra archive ledger. Authority admission, runtime readback, workspace recovery
and extension consumers use that verified result rather than interpreting the
seal independently. Tampered original bytes, conflicting WI/surface identity and
a mismatched current fence are rejected; a different disposable view cannot veto them. Imported registrations without
`raw_b64` may provide historical resource scope only when the complete canonical
JSON matches its sealed digest; they do not acquire the current YAML identity or
repair contract. Registration verification proves resource identity. Execution
grants and live Git identity remain separate checks at their owning boundaries.
Current-resource continuation reads the admitted original even when the registry is absent.
Target description/import reads the explicit unadmitted proposal and validates its actual resources
before target apply; it cannot substitute that proposal for current execution authority. Historical
bytes observed at a reinit target are retained with the decision but cannot veto or expand the sealed
source's resource set. The consumer regressions are in `tests/test_scl_effective_consumers.py` and
`tests/test_kernel_registration_projection.py`.

New registrations and migrations to a different path must match allocation exactly, in addition
to existing owner/executor, Git common-dir, branch, remote, base, cleanliness and conflict checks.
A path containing the correct WI text is not proof. Repeated creation does not allocate another
directory. Failure leaves an explicitly unregistered worktree; it must not be used for execution.
For an independent resident target whose SCL frontier has not yet arrived, `worktree register
--reserve-for-rollover` validates the complete registration with the authority-adoption checks
instead of pretending that the absent target kernel already grants execution authority. The
reservation is not executable and must still be bound by the sealed target descriptor, imported
frontier, live target claim, runtime readback and apply.

Existing exact registrations remain readable and idempotently usable at their original paths.
This is not permission to create more worktrees under their historical parent directories.
No historical directory is moved, deleted or re-registered by policy adoption.

Before first entry, the registration must also map the canonical Harness authority repository
into a linked WI worktree. Registering only a business repository does not establish that route.
For a main-only canonical owner claim whose managed SCL snapshot is still an untouched imported
baseline (revision zero, no epoch/session/fence/binding), `worktree migrate --complete-unbound`
may CAS-append a missing repository. It preserves existing repositories and checks allocation,
owner, every worktree, and binding absence under the first-bootstrap registry lock. Replacement
of an existing repository and any already-entered frontier remain denied. An exact lost-reply retry
with either the prior append digest or current digest returns the existing manifest without writing. This preparation grants
no execution authority; genuine Host continuation must still establish and read back the fence.

After entry, the attached active owner may use `adopt-workspace-authority-local --add-repository`
with a JSON descriptor containing `repo_id`, `repository_slug`, `shared_root`, `worktree_path`,
`branch_ref` and `base_sha`. The expected authority digest must match the current kernel fence.
The retained `--expected-registration-sha256` argument identifies the original request;
it no longer compares or authorizes the disposable registry view.
Canonical-root validation uses the complete existing-plus-added registration before any authority
commit; the new repository descriptor alone is not a complete registration.
This appends one exact allocated, clean linked repository; it cannot replace existing entries,
change owner/Session/epoch/checkpoint or bypass real resource conflicts.
The kernel commits the new registration bytes and next authority generation together before
rebuilding the manifest. Current readers continue from the admitted original if that view is lost.
An exact retry rebuilds the committed view while the resource generation and epoch remain current;
an unrelated later checkpoint does not invalidate it. Changed request input, another Session,
a superseded kernel fence or a redirected/nonregular destination still rejects the operation.
The `workspace_authority_adoption` result reports `status=extended`, preserved frontier/checkpoint,
new generation/digest and `added_repo_id`. Execution requires current ResumePacket authority and
`consumer_readback.status=ready`; local durability alone does not prove consumer readiness.

An attached direct owner may relocate a complete clean workspace set with the same command's
`--replace-repositories` migration manifest. Every changed worktree path must already exist at
the current managed allocation returned for its repository, WI and owner. The manifest must
replace every registered `repo_id`; target Git identity, branch, remote, base, cleanliness and
path conflicts are validated before commit. The kernel atomically advances the authority
generation with the complete new registration bytes and remaps retained execution-grant roots
and scoped descendants to the corresponding new worktrees before projecting the external
manifest. Another Session, a partial set, dirty or unallocated target, pending durable effect,
stale authority digest or redirected projection fails closed. An interrupted projection is rebuilt
from the current kernel original through materialization or the exact idempotent retry. The old worktrees remain outside the registration after consumer
readback and are removed separately with Git-aware cleanup; this command never raw-moves or
deletes a live checkout.

## Runtime admission

An existing producer conversation remains restartable after candidate submission changes the WI
to `awaiting_verification` and clears its executor. Only the exact current source Session, owner,
epoch, live fence and registered workspace qualify; pending replacements and retired Sessions do not.
Admission inspects resources and returns `process_start_only` with `execution_authority=false`.
It neither withdraws the candidate nor restores an executor, and execution mutations still require
their existing authorization. Repeated admission must leave the frozen candidate and lifecycle unchanged.

WI-bound host-native Pi launches call sealed `admit-runtime-workspace-local` before spawn.
The read-only response must prove exact WI, active owner, host session, epoch, live authority
fence, registration digest and linked-worktree realpath. A durable target-neutral replacement reservation may receive read-only process-start admission only when the Host supplies
one exact registered target surface and worktree realpath. That admission returns its digest but grants no WI execution
authority and permits no business turn. The live target then claims the reserved epoch against the same complete registration
digest; apply revalidates surface, digest, epoch and worktree realpath from runtime readback before atomically rotating the
active surface and authority fence. For independent resident kernels, the target first emits a private descriptor of its exact
registration. The source then commits `seal_cross_surface_source`, durably removes its execution authority, and emits one
content-digested mode-0600 capsule containing the redacted frontier and immutable checkpoint. The target may hydrate an
absent WI or atomically replace one exact revision-zero, local-isolated, unbound placeholder whose birth identity matches and
which has no authority, operation, checkpoint, attachment, measurement, candidate, replacement or other local dependency.
An exact imported-capsule retry is read-only; every other existing target fails closed. The target must reproduce the descriptor
from its own registration bytes; import persists the redacted source binding as `transitioning` and grants no target execution
authority. Host surface/runtime come from the current Host observation, and workspace resolution selects an exact
WI/agent/surface/repository registration rather than a static Relay workspace or current directory. Claim, pre-turn admission
and apply remain mandatory. Raw Host Session identities must not enter the capsule or machine
response. Before source sealing, its registration/fence remains active and recoverable; after sealing it is preserved for
reconciliation but cannot execute or publish. The target becomes the sole authority only after apply, which retires the imported
source registration in the target transaction. Same-surface replacement retains its existing fence, while cross-surface
replacement advances exactly one fence generation. A valid existing registration is an exact-path capability, including
an old location outside the ordinary conversation workspace allowlist. Its parent and siblings
never become allowed. A missing client, malformed response, terminal WI, stale identity,
changed registration, symlink, or unregistered directory fails closed before process spawn.
The ordinary non-WI workspace allowlist and external sandbox profiles retain their restrictions.

## Retirement

Runtime exit is not WI completion and does not authorize deletion. The existing terminal
cleanup inventory is the sole target source: exact registered paths and branches, not a
glob over allocation roots. Preserve nonterminal WIs, unsaved/ignored work, unverified publication,
running processes and unknown ownership. Physical cleanup uses supported Git worktree removal
only after its existing preservation and lifecycle gates; missing paths never justify recreating
an empty worktree. `harness worktree retire-terminal` archives the exact registration by digest
only after terminal status and absent exact worktree/local branch/remote branch are verified,
and retains its retirement receipt. Broker scratch worktrees retain the separate broker retirement
contract. Admission of an already retired registration must fail; repeated retirement is idempotent.

## Independent Outcome evidence authority

Before ACK or dispatch to a child, independent session or managed independent process,
a Host queries each integrity-valid explicit profile with:

`harness --root <canonical-root> --site-policy <profile-policy> verify-admission <wi>
--assignment <ho> --json`, using its actual `HARNESS_AGENT_ID`.

The read-only response is `verification_publication_admission/v1` with
`schema_version`, `status=read_only`, `work_item_id`, `assignment_id`, canonical `owner`,
actual `verifier`, `assigned_verifier`, `assignment_ack_state`, `candidate_sha256`,
`candidate`, `acceptance_criteria`,
`eligible`, `reason_code`, `policy`, `canonical`, `grants_write_authority=false`, and
`requires_accepted_assignment_for_publication=true`. Unknown identities/digests are null.
`policy` contains exact resolved `path`, raw `sha256`, `effective_sha256`, `site_id`,
`operation=work_item.update`, canonical owner `namespace`, and `allowed` (boolean or null).
The effective digest binds the resolved policy and v2 registry path/bytes.
`canonical` retains the registered service `root` as the profile entry boundary and reports
the selected published authority `revision` and exact `work_item_sha256` / `assignment_sha256`.
For managed SCL, `source_root`, `branch_ref` and `frozen_ledger_source` identify the same current
frozen ledger selected by canonical HANDOFF. A profile cannot declare another root as authority.
A registered source requires the current SCL fence/owner/epoch/session/criteria, complete freeze
and unique successful publication receipt; its live HEAD must equal the exact remote branch.
The original WorkItem bytes must remain exact. For a legacy split-clock publication,
the original settled event blob must remain an exact byte prefix. Later published verification
audit documents may append only typed events for the same WI, with unique event IDs and
nondecreasing timestamps after the original events; handoff events must reference a handoff
for that WI, and no appended status transition may change the freeze. The current events
must match the clean published HEAD's mode-100644 blob and exact remote branch.
The proof retains the original publication/blob digests and separately binds the current
revision, events digest and appended IDs. Rewrites, unknown types, empty/blank suffixes,
unpublished or dirty appends, duplicate IDs, wrong WI/order/mode and concurrent movement
are unavailable. This append proof is read-only and grants neither ACK nor Outcome authority.
Source selection, frozen proof and published revision are rechecked after Git reads. Unpublished,
wrong-root, dirty ledger, missing proof, unreachable remote or concurrent movement is unavailable.
Internal registered publishers may recheck the same binding from the exact selected producer root;
this does not change the public profile service-root boundary or permit arbitrary roots.
Accepted verifier workspace binding uses this same immutable snapshot; pending assignment remains
ineligible for evidence writes. Legacy service authority and a proved integrated main remain valid.
It neither fetches/checks out a revision nor writes an ACK, lock, carrier, operation or projection.
The verifier's canonical `show` readback likewise must inspect an existing active SCL
lineage without creating directories, changing permissions, migrating schema, or setting
SQLite write-mode pragmas. A missing or incompatible kernel is an explicit unavailable
readback, never permission to reconstruct authority from a producer document. This lets
an isolated verifier inspect current status while its sandbox denies writes outside the
allocated carrier.
The read path may open a checkpointed database as an immutable SQLite snapshot when
the containing mount cannot create a WAL index. If an active WAL exists, it must use
normal read-only SQLite visibility or report unavailable; it must not silently ignore
uncheckpointed committed facts.

`candidate` contains the exact `claim`, `producer`, `change_type` and `acceptance_fingerprint`;
Ordered `acceptance_criteria` come from that same immutable work-item blob.
These fields are supplied only after candidate validation succeeds. Host consumers use this snapshot,
not a producer report, working-copy YAML or a separately sampled revision. These fields
are additive read-only review inputs, not a dispatch, acceptance or authority grant.

The verification method is runtime-neutral. A fresh native child and a separate independent
session must satisfy the same candidate, assignment, evidence and publication checks.
Changing the runtime transport neither transfers producer execution nor permits reusing the
producer's Host source. Capability/role discovery is not dispatch or identity proof. Unknown
or already-started dispatches are read back before another instance is considered; publication
failure resumes the original submitted verdict. A particular adapter's Codex prerequisite
is not a restriction on the method. Actual route support requires installed Host evidence;
a read-only route selector does not supply a missing instance-binding/publishing adapter.

`@anchor test test_candidate_and_requirements_come_from_same_immutable_read`

Only `eligible=true` with `reason_code=eligible` permits Host selection. Other reasons are
`mismatch_work_item`, `mismatch_assignment`, `mismatch_verifier`, `mismatch_candidate`,
`denied_read_policy`, `denied_publication_policy`, `unknown_canonical`, `unknown_policy`,
or `unknown_consistency`. Valid decisions exit 0; unsupported commands, parse failures,
nonzero exits and malformed responses mean unknown to the Host. No local namespace inference
or fallback bypass is allowed. A unique pending or accepted assignment can be eligible;
publication itself still requires accepted ACK and current registered evidence authority.

An admission observation is not a permission lease. Explicit policy is rechecked before
canonical verify-result dispatch and bound into `verification_source` with its raw/effective
digests. Existing Broker preparation, projected application and unpublished-result push
recheck that policy on the source host against the current owner, independently of verdict.
A changed policy rejects the unpublished request and preserves its inputs. Completed publication
recovery retains the existing receipt/replay semantics; it does not reapply a verdict.

Host intake uses `ack <handoff> --accept --as <verifier>
--expected-work-item <wi> --expected-purpose outcome_verification`.
The three context options are required together for guarded intake. Under the existing
state write lock, ack checks the current contract's WI, purpose and receiver before
any durable transition. The existing immutable Broker argv preserves these preconditions
and repeats them during projection; a prior read-only listing is not authorization.
A mismatch exits nonzero without changing the handoff or WI. These expected-target
checks do not authenticate the caller or bypass site policy. Legacy unguarded ack
retains its existing contract; formal verifier intake must use the guarded form.

An accepted, current Outcome assignment delegates evidence production to its verifier;
it does not transfer WI ownership or claim producer execution. The existing commands
`worktree allocation`, `register`, `validate`, and `launch` accept `--assignment <handoff>`.
Ordinary calls without that option retain the owner/executor contract. The assignment must
be the single live assignment for the frozen candidate, with matching acceptance fingerprint,
owner, producer, verifier and accepted acknowledgment. Owner and producer are distinct
canonical roles and need not be the same principal. Candidate or assignment changes revoke
write admission; a rejected business verdict is not itself a workspace permission.

The allocation is `workspaces/<org>/<repo>/<wi>/verification/<assignment>/<verifier>`.
Its site-local registration is under `<wi>/verification/<assignment>/<surface>.yaml` in the
existing account-owned worktree registry. This cannot overwrite a producer registration.
The query returns `ok`, `policy`, `workspace`, `state`, `created`, `error`, plus
`verification {assignment_id, host, owner, producer, verifier, candidate_sha256}` and
`write_authority=false`. `planned` remains read-only: it neither creates a worktree nor
admits capacity. Use the existing capacity and linked-worktree creation mechanisms, then
register the clean carrier before writing any verification evidence.
For an already `registered` carrier, validate/launch it instead of registering over retained
evidence. The candidate digest binds the canonical claim, change type, criteria fingerprint,
owner, producer and freeze timestamp; it does not replace UAT's check of the actual tested revision.

Registration and each admission check exact realpaths, host, registered assignment and
candidate, Git top-level/common-dir/attachment/branch/remote/base, allocation, and **every**
ancestor Git root (including nested checkouts and bare repositories). A shared subdirectory,
producer worktree, symlink or nested-Git placement cannot declare itself a verifier workspace.
The external registry also discovers ancestor Git roots independently of supplied root lists.
`allowed_write_surfaces`, scope reports and receipt front matter are not authority inputs.
Unrelated shared-root dirt is preserved; its existence alone does not attribute it to a verifier.

`validate --assignment ... --json` returns the normal validation/digest plus `verification`,
`execution_authority=false`, and `admission_scope=verification_evidence_only`.
`launch --assignment ... -- <argv>` validates before starting the child and emits
`verification`, `registration_sha256`, `workspace`,
`lifecycle_authority=verification_evidence_only`, `admission_scope=process_start_only`,
and `arbitrary_shell_write_interception=false`. It does not invoke producer claim/continue
or change owner, active executor, Session or epoch. Evidence is written in the registered
carrier; canonical ledger publication remains a separate machine operation.

### Publication, recovery and reclamation

`verify-result <wi> --assignment <handoff> --evidence <file> --outcome-receipt <file>`
retains its command grammar. The publisher discovers the exact verifier registration itself
and binds `verification_source/v1` to the existing immutable mutation envelope: source host,
surface, registration digest, original command, and exact source file/hash/size manifest.
It freezes only the declared receipt closure. Before projection, before applying the result,
and before an unpublished prepared commit is pushed, the Broker rechecks current registration,
assignment, host, source paths and frozen bytes. The projected receipt/bundle is also checked
against the frozen mutation. These checks apply to **accepted, rejected, partial and blocked**.
Git-resident direct writes cannot bypass the Broker by setting its environment flag: the
result entry requires the exact live transaction, projection root and input digests.
Non-Git ledgers use the same bounded input materializer after source admission.

Source-host assertions are not federated authority. A publisher on another host must fail
closed without local source registration/readback; passing or editing proof JSON is not a
cross-host authorization mechanism. Use the source host's existing canonical publisher.
Historical inputs without source authority remain preserved; they cannot acquire new write
authority merely because their receipt schema passes validation.

Admission success says nothing about whether the original user outcome works well or solves
the original problem. Admission failure is an application-eligibility failure, not a fabricated
business rejection. The canonical receipt verdict and publication/replication result stay separate.
The existing Broker transaction/outbox stores publication and recovery references and immutable
inputs. `recover_harness_projection` rechecks authority before retrying a pre-effect attempt;
`broker resume-projection <tx>` resumes the prepared/published tail. A published tail must not
reapply a verdict or require now-revoked producer/verifier write authority just to finish replication.
Failed publication never deletes the original verifier inputs or the durable frozen envelope.

The `verify-work-outcome` Host owns continuity of an ended independent review across a publisher
upgrade. Its `resume_failed_publication.py` accepts the original Host run and a separately installed,
reviewed publisher profile. It revalidates the original installation/source closure, ended child,
operation streams, unchanged candidate, registered carrier and submitted verdict. Policy, canonical
namespace, principal and execution/input authority must remain equal to the old installation.
It derives the original idempotency key and recovery reference from that failed run and invokes the
same typed `verify-result`; it does not recreate a child/assignment or implement a second transaction
state machine. SCL/Broker retain all current authorization, zero-effect recovery and published-tail
checks. Canonical publication, replication and the unchanged verdict must be read back together.

The Host keeps the old installation and `host-run.json` unchanged and writes a separate diagnostic
journal. A new attempt with unknown effects requires original Broker recovery/readback before any
further action; a completed repeat is observation only. This independent-process route is separate
from the existing unattempted native-provenance repair, while both share original submission and
carrier validation. The method, installer and Host tests are maintained in Coevolution at
`ssot/skills/ops/verify-work-outcome`; installed consumer adoption is a separate verification step.

#### Projection execution diagnostics

Initial typed projection and same-transaction recovery share
`GitBrokerManager._run_recorded_harness_mutation`. Before the disposable projection is
removed, it saves `git_broker_projection_execution/v1` at
`projection-executions/<tx_ref>/<content_sha256>.json` in the external Broker state root.
The private receipt (0600, parent 0700) binds transaction, recovery attempt, source
branch/base, operation and argv hashes, selected actor, verification registration and
publication policy, Python entrypoint and executor source pins. It retains separate
stdout/stderr byte lengths, SHA256 and full base64 content, plus exit/timeout status.
It records no environment values. The existing transaction-v1 error reason references
this evidence; the receipt adds no publication or lifecycle authority.

Both output streams contribute to the bounded error summary. Advisory stderr cannot
hide a stdout refusal; long or non-UTF-8 failure bytes remain intact in the receipt.
Timeout, signal termination, spawn failure, invalid UTF-8 on a zero exit, and receipt
persistence failure stop preparation/publication and cannot satisfy the strict typed
rejection no-effect proof. Successful output is decoded strictly before admission.
Evidence does not grant retry: existing live-source, actor, policy, frozen-input and
recovery checks still decide the legal next operation. Legacy missing diagnostics stay
unknown; historical verdicts and transactions are never reconstructed from a new run.

Regression anchors: `test_projection_execution_preserves_both_streams_before_cleanup`
(real CLI/root advisory, complete long bytes, timeout, signal, invalid success),
`test_projection_diagnostics_write_failure_prevents_publication`, and the existing
same-transaction recovery test that retains both attempts' immutable receipts.

After completed publication and endpoint replication,
`broker resume-projection <tx> --reclaim-verification-inputs --json` additionally returns
`verification_reclamation/v1` under `verification_reclamation`, separately from the transaction
and outbox result. It checks canonical published input bytes, exact original registration/host,
allocation, and unchanged source file hashes. It only removes explicitly frozen **untracked**
source files; Git metadata, changed/tracked files and undeclared files are preserved. Durable
reclaim intent permits retry after a partial unlink. Repeated completion is idempotent.
This operation removes neither the worktree nor its registration and does not terminalize the WI.
Physical worktree/branch retirement continues to use the existing lifecycle gates above.

### Execution-layer limit

`protocol_only` is not an OS filesystem sandbox. The launcher controls initial cwd/env and
performs mechanical admission before its child starts; arbitrary shell code sharing the OS
principal can still address a shared root directly. The Claude heartbeat path hook only covers
Write/Edit/NotebookEdit and has fail-degraded branches; Git pre-commit/pre-push guards run after
filesystem writes; the Codex hook adapts lifecycle prompts. None supplies universal shell write
interception or reliable attribution of concurrent writes. `--require-hard` continues to fail
until a real account/ACL/sandbox boundary is installed and proved. Publication refusal must
never be reported as prevention of a previous first write. Regression fixtures explicitly
exercise this remaining boundary without using production paths or WIs.

## Verification

Synthetic tests cover deterministic placement, traversal/symlinks, misplaced registration,
exact existing-path retry, terminal retirement, real managed-lineage CLI admission, identity
mismatch, and Pi consumption inside/outside its ordinary allowlist. Production UAT uses the
existing WI013 without reinit, moving its worktree, adding a one-off allowlist entry, or terminalizing
its business task. Record source, installed runtime, visible channel result and execution frontier
separately; tests alone do not prove deployment or user-facing success.

### 终态后的验收工作树退休

正常完成和取消退休均须清理生产者与验收工作树，WI 终态不等于物理清理完成。`worktree retire-terminal --assignment <handoff>` 选择原验收登记；以登记中的 owner、producer、verifier 和同一 WI 的历史交接核对身份，不要求终态 WI 重新获得执行权或重新接受已归档交接。所有目录、Git 登记、本地与远端分支缺失证明仍适用；证据和原登记按摘要归档。

验收登记的 retirement receipt 和 archive 路径必须包含 assignment，不能与生产者或另一验收轮次冲突。清理验收同时检查目录及容量、Git 登记、临时分支、生产者登记、各轮验收登记和未退休的发布投影；状态 completed/cancelled、目录已不存在、命令退出成功均不能单独代表全局清理完成。

若历史 shared root 和 Git common dir 均已物理删除，可沿原登记的 exact remote URL 验证原分支缺失后归档登记；路径不得有符号链接重定向，任一路径仍存在或远端查询失败／分支仍存在均不得退休。该路径不重建仓库、不猜测替代 shared root，也不授予写权。

### Published frozen source regression evidence

Verifier admission reads the current kernel submission, assignment and original inputs. Disposable
ledger/event copies do not create acceptance, change the claim or move its publisher. A published
assignment may append real verification facts without requiring every older snapshot to match it;
source identity and the exact remote revision remain independently verified.

`@anchor test agent-harness/tests/test_registered_verification_admission.py :: test_verifier_candidate_identity_survives_disposable_ledger_damage`
`@anchor test agent-harness/tests/test_registered_verification_admission.py :: test_admission_reads_published_registered_assignment_without_write`
`@anchor test agent-harness/tests/test_registered_verification_admission.py :: test_verifier_admission_does_not_need_a_writable_producer_index`
`@anchor test agent-harness/tests/test_registered_verification_admission.py :: test_workspace_binding_uses_same_accepted_registered_candidate`
`@anchor test agent-harness/tests/test_registered_verification_admission.py :: test_managed_admission_uses_kernel_state_and_requires_original_inputs`
`@anchor test agent-harness/tests/test_registered_verification_admission.py :: test_pending_assignment_cannot_bind_verifier_workspace`
`@anchor test agent-harness/tests/test_registered_verification_admission.py :: test_remote_revision_changed_during_git_read_is_rejected`
`@anchor test agent-harness/tests/test_registered_verification_admission.py :: test_copying_main_projection_does_not_redirect_independent_verification`
`@anchor test agent-harness/tests/test_registered_verification_admission.py :: test_registered_publisher_can_recheck_accepted_verifier_binding`
`@anchor test agent-harness/tests/test_registered_verification_admission.py :: test_foreign_root_cannot_bind_managed_verifier`
`@anchor test agent-harness/tests/test_registered_verification_admission.py :: test_published_assignment_append_reaches_verifier`
`@anchor test agent-harness/tests/test_registered_verification_admission.py :: test_projection_validates_original_source_instead_of_temporary_ledger`
`@anchor test agent-harness/tests/test_registered_verification_admission.py :: test_projected_outcome_events_cannot_create_acceptance_or_move_publisher`
