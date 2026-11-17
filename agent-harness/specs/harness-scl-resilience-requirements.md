---
feature_name: "harness-scl-resilience"
version: "1.5.12"
status: "authorization_closure_redesign_pending"
work_item: "wi_20260827_282"
owner: "Pulse"
created: "2026-08-29T00:00:00+08:00"
tags: [requirements, scl, local-first, resilience, recovery]
---

# SCL Local-First Resilience — Requirements

## 1. Purpose and precedence

This specification corrects the availability model of the existing SCL lifecycle. It is the
normative source for continuity, degradation, recovery, authority fencing, ingress and external
effect delivery. Existing lifecycle, continuation, convergence and Golden Path specifications
remain normative for their domain semantics only where they do not conflict with this document.

The correction is based on observed product behaviour, not an attempt to preserve an invalid
paper contract:

- lifecycle state is primarily stored as Git-resident YAML and JSONL;
- accepted mutation currently waits for Broker publication and required replication;
- the Codex hook blocks the user prompt when its adapter fails;
- machine errors can be emitted only on stderr before any JSON response exists;
- worktree authority can be inferred by scanning historical registrations;
- recovery is transaction-specific and user-driven rather than a general reconciler.
- Relay's Feishu and Telegram handlers converge on one `/wi` command handler, but that shared
  handler still invokes the legacy `Scripts/scl-resume.py -> harness continue` path rather than the
  sealed `harness-scl` client;
- the three resident Relay nodes can run different immutable PULSE releases and Harness virtual
  environments, and a correct sealed runtime on one node does not install or activate it on the
  other two;
- Relay currently collapses a typed downstream rejection such as `identity_mismatch` into the
  generic user-visible `resume_failed`, leaving neither the channel response nor ordinary service
  logs with the safe machine reason.
- sealed entry and legacy rollover can read different frontiers for the same WI on one installed
  node; observed values were epoch 8/checkpoint 30 versus epoch 7/checkpoint 19;
- an imported resident-surface logical Session ref can lack a durable attachment to the current
  Host Session, while `_entry_action` turns opaque-ref inequality into the unsupported positive
  claim `source_session_active`;
- an existing canonical WI can be absent from a cold local kernel and return `work_item_not_found`
  instead of entering bounded import/reconciliation;
- Relay routes direct `/wi` through sealed SCL while checkpoint and rollover still call a legacy
  controller, leaving two active lifecycle writers behind one channel grammar.

Those facts explain current failures; they do not define the target product.

`@anchor manual current-runtime-reality-audit :: before implementation, reproduce each listed behaviour through source inspection and an isolated installed-runtime probe; absence of a reproduction must amend this section before code`

## 2. Product invariant

If the local workspace bytes and the SCL local durable kernel are readable, a user MUST be able to
enter the same WI, read its latest locally known frontier and continue ordinary domain work. When
the kernel has normal or recoverable reserved write capacity, the user MUST also be able to
checkpoint new decisions. None of these operations may depend synchronously on GitHub, network,
remote surfaces, a hook, an editable source checkout, or a verifier. External failure MUST reduce
only the capabilities that require the failed dependency.

The product MUST NOT describe a globally unpublished or unfenced fact as globally authoritative.
During a partition it MAY admit authorized isolated local progress, but MUST withhold unsupported
global publication and terminal claims. It MUST preserve valid delegation and human decisions;
unproved cross-site exclusion is a scoped realization problem, not a veto of those decisions.

Physical limits remain truthful. If the operating system cannot persist any byte and the SCL-owned
emergency reserve is exhausted, SCL cannot claim a durable lifecycle write. It MUST still permit
read-only entry and must not block the surrounding conversation or falsely claim that the WI is
invalid. Domain-file writes remain subject to actual filesystem capacity.

A readable canonical WI MUST NOT disappear merely because one site's kernel has not yet imported
it. SCL MUST either hydrate the exact frontier from its configured canonical local source, expose a
truthful reconciliation state, or prove that the WI is genuinely unknown. It MUST NOT mix reads and
writes from two different frontiers to keep a command moving.

`@anchor manual local-first-product-invariant :: fault-injection acceptance must show local entry and domain work continuing while each external dependency is independently unavailable`

## 3. Terms

- **local durable kernel**: the site-local, Git-root-external transactional journal used for
  lifecycle intent, checkpoints, authority facts, operations and pending effects.
- **global canonical state**: the latest state whose authority and publication fence have been
  accepted by the registered global authority. A local overlay is never silently promoted to it.
- **authority home**: the registered site allowed to originate globally publishable operations for
  a WI under one monotonically increasing generation.
- **fence**: the WI, authority-home identity, generation and operation identity that make an
  external effect reject stale or duplicate writers.
- **local effective state**: global canonical state plus site-local committed operations that are
  valid for continued work at that site.
- **ordinary local work**: reading, editing, testing and making recoverable local commits in the
  already available registered domain workspace. It does not include remote push, protected-branch
  movement, authority transfer or terminal claims.
- **isolated progress**: durable local work recorded without a currently provable global fence.
  It cannot create global claims or irreversible cross-boundary effects.
- **operation**: one idempotent user intent and its local transaction.
- **effect**: a separately retryable consequence such as Git publication, replication, handoff
  delivery, remote readback or audit projection.
- **capability**: a specific action whose availability is computed from local and external facts.
- **logical source**: the opaque Session identity stored in the WI binding and fenced by lifecycle
  operations. A legacy resident-surface logical source is not necessarily derived from a provider
  Session.
- **source attachment**: a durable, privacy-safe join between one logical source and one
  Host-derived opaque Session identity for the same WI/epoch/surface/runtime. It is evidence of
  source continuity, not a new WI, epoch, candidate decision or authority transfer.

## Authorization and correction requirements

The human's explicit decision and valid in-scope delegation are authorization sources. SCL records
and implements them; it does not require permission from the superseded binding. A request for
permission is not a grant. Normal conversation remains reachable when parsing, the kernel or the
recovery reader fails. A recorded boolean or third-party summary cannot manufacture human consent.

Ordinary correction within existing scope requires no new approval. Reinit applies the smallest
necessary authorization/binding change; a complete frontier rebuild is used only when necessary.
The complete-rebuild requirements below apply to that mode, not to requesting authorization or
changing a bounded grant. Existing artifacts, pending effects and immutable receipts remain intact.

Each business request has a durable stable identity independent of title and executing Session.
Changed inputs create immutable revisions of only affected, unconsumed steps. Exact replay compares
WI, action, normalized inputs and provenance; it cannot return a different request's old success.
Unknown effects are observed before retry. Successful steps are not rerun after correction/transfer.
A post-commit failure preserves all committed receipts and reduces only dependent capabilities.

A readable workspace alone is not authorization; conversely a valid authorization is not proof of
physical access. Responses distinguish both. Dirty files are work in progress, not a WI-wide veto:
protect their provenance, preserve bytes, and admit only authorized resource writes. Conflicts affect
the exact resource, not unrelated work or permission requests. Kernel durability failure prevents
checkpoint claims but does not forbid authorized isolated artifact preservation where storage works.

## 4. Required capability model

SCL MUST project capabilities individually. A single generic `blocked` result MUST NOT stand in
for unrelated failures.

| capability | local dependency | external dependency | allowed degraded result |
|---|---|---|---|
| `conversation` | none | none | always allowed; warning only |
| `local_read` | readable local kernel or compatible snapshot | none | last-known facts with freshness |
| `local_work` | readable workspace | none | isolated progress if global fence is unavailable |
| `lifecycle_write` | writable kernel or reserved emergency capacity | none | unavailable only for a real local persistence failure |
| `checkpoint` | lifecycle write + readable frontier | none | durable local checkpoint; publication may remain pending |
| `external_publish` | committed operation + valid fence | provider/credentials/network | pending with retry policy |
| `cross_agent_handoff` | globally publishable authority | delivery/readback | pending; receiver gets no authority before acknowledgement |
| `terminal_settlement` | complete local evidence | global authority, required review/readback | pending; never falsely completed |

Every command response MUST include the relevant capability states, reasons, freshness and the
operation/effect references needed for automatic or manual observation. One unavailable capability
MUST NOT erase successful local work or turn unrelated capabilities unavailable.

For kernel-owned WIs, missing ledger files or the entire ledger directory MUST NOT
disable local work or checkpoints. Workspace readability describes the physical workspace,
not existence of a WorkItem YAML projection. Checkpoint availability depends on the readable
kernel frontier and persistence, independent of domain workspace availability; execution
authorization is still checked by the operation. Read-only capability observation MUST NOT
materialize projections or change the frontier.

`@anchor test agent-harness/tests/test_scl_projection_capabilities.py :: test_discarded_projection_preserves_read_only_capabilities`
`@anchor test agent-harness/tests/test_scl_projection_capabilities.py :: test_absent_physical_workspace_remains_unavailable`

`@anchor manual capability-projection-acceptance :: table-driven tests cover every row and prove one failed dependency does not disable unrelated capabilities`

## 5. Functional requirements

### R1. Local-first entry and continuation

An observation continuation (`observe_candidate` or `observe_terminal`) MUST complete without
an execution grant or execution-consumer probe. A failed one-time business import MUST retain
its reason in the final response and MUST NOT request automatic execution initialization.
Owned history intake uses the kernel's admitted resource identity, not execution/registration
projections. After an approved producer tree is retired, intake MAY read the registered shared
repository at one immutable revision only after proving its repository identity, ancestry,
original submission and final kernel approval. An already issued candidate delivery MUST retain
its original destination while that exact frozen source remains valid; importing business policy
MUST NOT reroute it or weaken operation, request or transaction identity checks.

`@anchor test agent-harness/tests/test_scl_client.py :: test_continue_observation_does_not_require_execution_or_drop_import_failure`
`@anchor test agent-harness/tests/test_scl_business_import.py :: test_old_kernel_freeze_keeps_original_outcome_and_final_human_review`
`@anchor test agent-harness/tests/test_candidate_remote_audit_recovery.py :: test_original_candidate_recovers_unadopted_remote_audit`

Review preparation MUST retain the producer's correction capability. `ready-for-uat-local` identifies the version under verification;
it is not final freeze. Exact human approval and independent Outcome evidence precede `freeze-reviewed-candidate-local`, which
records the final review digest and disables producer writes. The responsible agent may finish that exact approved, published
candidate from a later Host Session: the existing publication proof must still bind the kernel epoch, acceptance criteria, claim,
producer and successful publication. This scope MUST NOT rebind a source, grant execution, change the candidate or replace review.
Missing publication, stale approval, a replacement in progress or a different responsible agent MUST reject without mutation.
Legacy candidates without this publication proof still require their original source. Historical state serialization MUST remain byte-compatible when
that optional digest is absent. Conversation process admission is independent of write capability for both scoped and legacy bindings.
Closeout is operationally complete only after terminal adoption, conversation detachment and exact process-resource inventory retirement.


1. Ordinary `/wi <wi>` MUST resolve from the local kernel first and return one bounded machine
   response. Remote freshness checks MAY run asynchronously or under a bounded optional budget;
   they MUST NOT be a prerequisite for local entry.
2. A valid same-site binding or checkpoint MUST be sufficient for locally authoritative execution
   while its fence is valid. When the fence cannot be proven, SCL MUST offer `local_isolated`
   continuation against the same WI rather than inventing a new WI or declaring it unusable.
3. `awaiting_verification` and absence of an active executor MUST NOT strand a WI. An explicit human
   decision to withdraw the current candidate MUST be expressible as an idempotent lifecycle
   operation even when the previously bound source Session is unavailable. If the command is issued
   by that source, its binding continues normally. Otherwise the human-facing adapter or the current
   Session Agent, after interpreting its actual conversation, MUST submit the explicit lifecycle
   decision to withdraw the candidate and continue the same WI. The kernel MUST NOT infer, attest or
   mechanically validate human intent through a boolean, token, signature-shaped string or Host
   identity. It records the asserted decision kind, submitter/session provenance and rationale, then
   validates machine facts and the effective authorization for that action/resource. Old owner,
   binding, replacement or epoch being superseded are evidence, not additional consent gates.
   A valid source-adoption operation advances the epoch, fences the old Session,
   preserves candidate/checkpoint history, binds the current Session as local executor and enters
   `local_isolated` until external authority readback converges. Ordinary `/wi` entry MUST never
   perform this adoption implicitly.
4. A replacement Session MUST not retire the source until the checkpoint is durable and the target
   has proven entry readiness. Because the future Session does not exist when the source requests rollover,
   source preparation MUST reserve a target-neutral successor slot rather than require a target
   Session identity. Exactly one fresh Session may claim that slot; activation requires target
   control-plane readiness plus live runtime and workspace readback. This evidence is not a
   runtime settled receipt or proof that business work has completed. Business execution follows
   authority activation; it is not a prerequisite that would require unauthorized target work. The canonical client MUST own claim,
   capture of that exact first result, confirmation and apply as one public entry operation. It MUST
   emit only the final machine envelope and MUST NOT expose confirmation refs or result-digest
   arguments for a Session Agent to assemble. Any failure before activation leaves the source
   recoverable and the same public operation resumable.
5. Enter, checkpoint and rollover MUST preserve the original WI, Outcome responsibility and epoch
   rules. Session replacement alone MUST NOT create a handoff or verifier.
6. The human-facing rollover command MUST NOT require the user to copy a Session, continuation,
   operation, surface or epoch identity. Host/runtime adapters derive opaque local identities, and a
   restart or retry MUST rediscover the one reserved successor from the kernel.
   A channel `/wi continue <WI>` explicitly issued while its current physical Session still owns
   that WI MUST complete checkpoint and target-neutral reservation, replace the physical Session,
   and enter the successor as one crash-resumable user request. The ordinary `/wi <WI>` entry MAY
   reuse the current Session. Before preparing a new reservation, the adapter MUST detect an
   existing reservation or an already changed physical Session; retrying the same request MUST NOT
   create a second successor after claim or consumer readback failure.
7. A checkpoint prepared for replacement MUST store the first business action to perform after the
   successor becomes executable, not an instruction to create or enter that successor. ResumePacket
   MUST project lifecycle orientation separately from that business frontier. After apply, the
   lifecycle orientation is consumed and the successor receives the preserved business action;
   the kernel MUST NOT parse or rewrite human-authored prose to achieve this separation. Every
   ResumePacket projection, including fresh continue, applied-successor Host rebind and verified
   read-only snapshot recovery, MUST also preserve the checkpoint's ordered artifact refs with the
   complete `repo + path + revision + sha256 + role` identity. A packet that restores prose but
   omits artifact refs is an incomplete continuation and MUST NOT satisfy the complete-frontier gate.
   Candidate observation MUST preserve recorded verification and review decisions. A current final
   review freeze directs continuation to existing publication/settlement, without requesting another
   human decision or granting execution/publication authority from orientation text alone.
   `@anchor test agent-harness/tests/test_scl_lifecycle.py::test_candidate_resume_preserves_recorded_review_instead_of_requesting_it_again`
8. SCL MUST distinguish a logical bound source from a Host Session observation. Opaque identity
   inequality alone proves only `source_identity_unresolved`; it MUST NOT prove that another source
   exists, is live or retains execution authority. `source_session_active` requires exact current
   readback of a distinct source or successor.
9. A current-format source operation MUST durably attach its logical source to the Host-derived
   opaque Session identity. An imported active resident-surface binding without mechanically
   joinable Host evidence remains readable and returns `source_identity_unresolved`. The
   authenticated human or contextual Session Agent MAY assert `attach_current_host_to_source` with
   a rationale; the kernel validates effective authorization for the actor and requested scope,
   exact WI, epoch, source, surface/runtime, replacement and frontier preconditions, then records
   the attachment without advancing epoch,
   withdrawing a candidate or transferring authority. This is not `reopen`. These continuity checks
   MUST NOT become preconditions for a new grant under section 0.
10. Direct entry, checkpoint, rollover, reopen and review preparation MUST consume one per-WI
    kernel frontier and its serialized journal writer; this does not restrict all business resources
    to a single Agent. Checkpoint and rollover orientation MAY precede a
    Session Agent's semantic draft, but the final mutation MUST use the canonical client and MUST
    never fall through to a legacy writer.
11. A successful replacement claim/apply MUST remain visible as a lifecycle mutation even when a
    later Host-identity or consumer readback fails. The final response MUST report the committed
    operation and safe frontier, then lower only execution readiness; it MUST NOT collapse the
    composite result to `read_only` or tell the user that no mutation occurred.
12. After replacement apply, the successor frontier is stable even while
    `replacement_stage=applied`. If the Host adapter and the standalone client present different
    opaque identities for the same fresh Session, an explicit retry of `/wi continue` MUST perform
    one bounded, audited attachment rebind without advancing epoch, changing the logical source,
    reviving the old source or repeating claim/apply. A Host identity already attached to an older
    epoch MUST be rejected as the retired source.
    A channel retry with an exact unfinished Host entry MUST continue that original successor,
    including after restart or lost acknowledgement. After canonical apply, a matching pending
    target MUST pass current workspace and tuple validation and finish Host adoption before ordinary
    source-binding restoration; a retained old source binding MUST NOT prevent that recovery.
    `execute_fenced` describes authority, not completion of the first business dispatch: it MUST NOT relabel that target as a settled source
    and trigger another rollover/checkpoint. Preserve the target, epoch, saved checkpoint and
    dispatch idempotency key. A previously sent or unknown effect still requires its original
    readback; only a settled source with no pending entry may request another fresh replacement.
    If a later target reservation already exists, an exact unfinished entry in the still-authorized
    current epoch MUST settle first. Preserve the newer checkpoint and reservation; do not reuse
    the current pending identity for the later epoch, cancel the reservation, or relax admission.
    A saved first-business-result receipt bound to the exact operation, Session and epoch permits
    finishing Host bookkeeping after restart without another send. Missing or mismatched result
    evidence remains unresolved; a local pending-state label alone cannot prove success.
    Local continuation probes remote publication only for its final frontier, not at each internal
    claim/apply/attachment stage. Capability observation bounds each remote Git read or dry-run
    to five seconds; timeout degrades publication without undoing local execution or bypassing
    consumer checks. Actual effect reconciliation keeps its separate network allowance.
13. `retired_source_session` proves that the caller's old authority is retired, not that
    a successor is live or that the caller cannot request new authorization. Old credentials
    MUST NOT authorize writes. Every caller may request a specific human decision through normal
    conversation before kernel initialization or complete-draft validation. A new explicit human
    decision or valid delegation creates new authority; retired state MUST NOT veto its application.
14. An Agent holding WI/workspace rights MAY explicitly delegate those rights within the original
    authorization, unless that authorization forbids delegation. Mere knowledge of another Session
    does not authorize unsolicited coordination. Accepted delegation MUST deliver usable resource
    and execution rights without requiring the receiver to possess them beforehand. Exclusive
    transfer retires old writes; scoped collaboration isolates resources. Owner, acceptance criteria
    and outcome responsibility do not change implicitly. Pending audit/measurement does not veto
    the decision; actual permission delivery and unresolved effects remain separately observable.
15. A `main_only` WorkItem already claimed by its owner MAY recover a pristine stale-unbound local
    snapshot without reinit. The route MUST require canonical `in_progress` owner/executor parity,
    unchanged WI birth identity and acceptance criteria, local revision zero with no checkpoint,
    candidate, replacement or authority fence, exactly one matching registration and a clean exact
    linked worktree. Its explicit prepare operation establishes epoch one and the generation-one
    fence in `transitioning`; only live target readback may apply it and atomically create the exact
    Host source attachment plus a revision-one checkpoint with no fabricated completed work. The
    recovery MUST NOT write a legacy execution binding or dirty the registered worktree. Ambiguous
    registration,
    existing unrelated binding/fence/operation, non-owner, non-main-only WI or any other mutation
    MUST remain fail-closed. Exact retries of the same operation are idempotent.
    These prerequisites belong only to this legacy bootstrap shortcut; failure MUST NOT gate
    valid delegation, new human authorization or their independent request/realization paths.

`@anchor manual local-entry-contract :: installed-runtime tests cover current source, fresh target, missing remote, expired fence and interrupted replacement without changing the WI`

### Local authorization realization and last-resort continuation

An execution epoch and a physical measurement interval are different identities. An
authorized current-Host attachment within the same execution epoch MUST select a
prospective measurement interval keyed by that Host. Same-Host retries reuse the
interval; a different attached Host MUST NOT replace the previous native baseline,
seal, attribution, or artifact. Historical open/missing intervals remain incomplete
at the terminal measurement gate; starting another interval does not close a gap.
The old logical source cannot record after its attachment has been superseded.

Native usage baseline/final recording and their idempotent readbacks MUST remain
local: they MUST validate the current Host, execution epoch, realized scope and
immutable evidence, but MUST NOT probe Git publication, remote availability or
write permission. Their response MUST NOT advertise unobserved external publication
as available. Ordinary entry/publication retains its own dependency checks. A Host
RPC timeout is an unknown command result, not a measurement fact conflict; retain
the original binding and read it back before claiming settlement. Native dialogue
and ordinary input MUST NOT await measurement, publication, or a lifecycle command.
A healthy native process MUST NOT be destroyed solely because metering failed.
Pending/failed control calls release the channel receiver queue, preserve their
original request identity and later deliver their own result. An unavailable
execution proof restricts only the affected tool capabilities, not Agent replies.
An existing validated native transcript may resume without filesystem/effect tools
when workspace admission cannot be read; this grants no WI execution authority.
Missing metering is reported as unavailable, never as zero or exact attribution.

Kernel schema 2 retains every legacy measurement reference and fact byte while
adding the Host dimension. Migration is transactional and foreign-key checked;
schema-1 writers are refused after migration rather than selecting an arbitrary
interval. A degraded response with a committed operation MUST expose that operation
in progress, independently of the resource/measurement failure.

A trusted Host MAY attach an exact signed local resource scope to the actual human decision.
Acceptance of that decision MUST precede resource readiness checks. Missing registration creates
a durable realization obligation for the same decision. Busy or inaccessible resources MUST NOT
prevent separately granted resources from becoming usable. `ready_resources` identifies that
partial realization; `ready=false` alone is not a prohibition on every resource.

When both the old kernel and the compatible reader fail, `reinit-local --isolated` MUST consume
that Host scope before opening either database or snapshot. The scope identifies the original WI,
current recipient/Session/surface, exact linked non-primary Git worktree identities, local actions,
resources and the existing resource-action lock root. The Host decision is retained as signed
input evidence, not as a second lifecycle authority database. Original kernel bytes, effects and
worktree content remain intact. No global lineage switch, fabricated epoch/checkpoint or inferred
external effect result is permitted. Real worktree launch and Git guards consume the same scope.
A running earlier local action keeps its lock until it exits; unrelated resources may continue.

A binding-only grant MUST NOT withdraw a candidate or reopen a terminal WI. A human decision that
explicitly includes withdrawal and continuation MUST select that narrow transition within the
same reinit request. Isolated continuation reports acceptance, independent resource readiness and
actual process outcomes separately; it never claims canonical withdrawal, global exclusivity,
checkpoint durability, publication or business completion without the corresponding evidence.

### R2. Durable idempotent operations

1. Every lifecycle mutation MUST receive a stable operation id and deterministic idempotency key
   before any state or external effect changes.
2. Intent, preconditions, fence and requested transition MUST be appended to the local journal in
   one transaction. A successful local commit MUST never be rolled back merely because a later
   external effect failed.
3. Retries after process crash, hook retry, Session restart or user repetition MUST resume the same
   operation. Argument or precondition drift under the same idempotency key MUST be rejected without
   replacing prior evidence.
4. Operation status and each effect status MUST be represented separately. `local_committed`,
   `globally_committed`, `effect_pending`, `reconciliation_required` and `human_decision_required`
   MUST not be collapsed into `success` or `blocked`.
5. The kernel MUST serialize conflicting operations per WI without imposing a repository-global
   lock on unrelated WIs.

`@anchor manual operation-journal-contract :: crash injection at every transaction and effect boundary proves exact replay, per-WI serialization and no duplicate domain event`

### R3. Authority and fencing

1. Overlapping exclusive resource writes MUST have one current authority; disjoint delegated
   scopes MAY execute concurrently within one WI. Each globally visible operation MUST bind the WI,
   authorization source/scope/version, authority home, relevant resource generation, base global
   revision and operation id. Outcome responsibility is not synonymous with execution rights.
2. Historical or retired registrations MUST never be scanned as interchangeable authority
   candidates. The current binding MUST name one exact registration id, generation and digest.
   The kernel-admitted original bytes, authenticated by that digest, define resource identity.
   Parsed copies and external registry files MUST NOT grant or veto execution; missing, malformed
   or divergent views are rebuildable from the original. New admissions MUST NOT persist a second
   parsed registration. Strict schema, WI/surface identity, actual Git/resource probes and execution
   grants remain enforced on the original. A legacy JSON-sealed original without raw bytes remains
   original evidence, not a disposable parsed copy. Consumer readback MUST derive missing Agent/WI/surface
   context from the resolved current operation and pass it only to the actual guard child, retaining
   the real Host Session and rejecting explicitly conflicting Host context.
   Workspace extension, replacement and publisher selection MUST compare the current kernel
   authority, not a caller's cached registry digest. An exact retry may rebuild the view after an
   unrelated checkpoint while its resource generation and source epoch remain current. Symlink or
   nonregular projection destinations and real resource changes remain rejected. Reinit historical
   bytes already retained in the kernel MUST NOT require another side archive to realize a grant.
3. A local site with a valid fence MAY continue and queue authorized publication. An unproved
   resource fence blocks the dependent external write, not a valid delegation decision or a request
   for human authorization. New authority MUST be implemented from its valid source without old
   binding approval; actual exclusion and access remain explicit implementation conditions.
   Neither isolated progress nor ordinary execution delegation grants Outcome/terminal approval.
4. Reconnection MUST compare the recorded base and fence against global authority. Compatible
   progress is replayed idempotently; incompatible competing claims require an explicit human
   resolution and preserve both histories.
5. Stale publishers and stale targets MUST be rejected by fence comparison, not by assumptions
   about checkout order, filename order or current process memory.

`@anchor manual authority-fencing-contract :: two-site partition model proves at most one global publisher while both sites retain local read/work capability and divergent history is preserved`

### R4. Ingress and machine protocol

1. The canonical entry MUST be a local versioned client/API backed by the durable kernel. Chat
   hooks, skills and model instructions are convenience adapters, not correctness-critical state
   machines.
2. A hook failure, timeout, import error or malformed adapter response MUST allow the user message
   through with a concise degraded warning. It MUST NOT return a hard prompt block merely because
   lifecycle automation is unavailable.
3. Every CLI invocation, including option parsing, authority resolution and preflight failure,
   MUST emit exactly one versioned machine envelope on a documented channel. Human diagnostics may
   accompany it but cannot replace it.
4. Machine responses MUST include operation status, capability projection, stable error code,
   retryability and next automatic action. They MUST not require parsing human stderr.
5. Literal command identity MUST survive lossless renderer presentation such as `\_` and a matching
   inline-code fence around the entire command. The adapter may unwrap only a bounded, single-line,
   matched presentation fence and normalize escapes only inside a proven command grammar; it MUST
   never reinterpret surrounding prose, mismatched fences or arbitrary user text.
6. An installed Hook MUST discover the matching client inside the same active sealed environment
   without requiring a test-only environment override. Resolving a virtual-environment interpreter
   MUST NOT escape into a shared base-interpreter prefix or mutate that prefix to manufacture a
   sibling executable.
7. After a recognized interactive command, the human-facing adapter or the Session Agent that knows
   the decision MUST be able to submit the same structured lifecycle decision through the canonical
   client. Deterministic idempotency is a retry mechanism, not evidence of intent. If the adapter
   fails mechanically after recognition, the Session Agent MUST retry without requiring the user to
   repeat the command or copy an internal reference.
8. Ordinary public entry MUST be one canonical entry operation. A host adapter MAY perform bounded
   read-only plan/probe calls before that operation so it can create and read back a real target
   runtime; exactly one mutating entry call then owns claim, canonical first-result capture and apply.
   Claim responses, replacement refs, claimed-frontier digests, first-result digests and confirmation
   commands remain private implementation details. A Hook and a contextual Session Agent consume
   the same final `scl_response/v1`; neither may replay mutation to manufacture a different "first"
   result or manually confirm a claim.
9. Every interactive Relay channel that exposes `/wi`, including Feishu and Telegram, MUST call the
   same versioned local client contract. Channel parsing, authorization and rendering MAY differ;
   lifecycle resolution, mutation, degradation and recovery semantics MUST NOT.
10. A Relay immutable release MUST declare and verify the compatible sealed SCL runtime before it is
    activated. It MUST NOT silently fall back to a release-bundled legacy Python adapter, an editable
    checkout or an unrelated Harness virtual environment when the canonical client is absent.
11. Relay MUST preserve the downstream `scl_response/v1` status, stable error code, retryability,
    capability projection and safe reason in its internal result. The default human response MUST
    explain the supported fact and one actionable next state; the exact stable code and fields MAY
    be exposed as optional diagnostics. Structured logs MUST include the node, channel, WI, request
    id and code without raw Session ids, credentials or private adapter stderr.
12. Installation and activation MUST be evaluated per registered resident node. A source test,
    wheel build, one-node install, bot health probe or shared Git SHA is not three-node delivery.
    Each node retains a verified LKG runtime and can roll back its runtime pointer without losing
    local kernel operations or pending effects.
13. If a channel's preferred client cannot load, the adapter MUST try only its verified compatible
    LKG, then return a degraded result that keeps conversation and ordinary local work available.
    It MUST NOT retry through the old synchronous Git/remote path or require the user to repeat the
    command. Once the client is restored, the same intent is retried idempotently.
14. A Relay host adapter MUST obtain the sealed read-only plan before attempting to start or resume
    a persisted conversation runtime. Dormant-runtime availability, provider health and usage
    binding are not prerequisites for a no-authority result such as positively proven
    `source_session_active` or `observe_candidate`. Exact runtime activation/readback occurs only
    after the plan grants execution or requires a successor.
15. Every lifecycle verb behind one interactive command namespace MUST route through the same
    sealed client and per-WI writer. A compatibility reader MAY observe legacy bytes, but no active
    checkpoint, rollover, reopen or review write may call a legacy controller after the WI is
    kernel-owned.
    The old execution controller MUST retire as a whole after installation cutover, including
    direct entry persistence and read-based reuse/launch decisions. Historical binding reads remain
    available only as migration evidence. The legacy terminal reconciliation writer must also
    retire before reading ledger prerequisites after cutover or WI takeover: it cannot append
    repair events or archive handoffs outside the kernel. Missing projections do not reopen it.
    Verification admission and result-publication policy MUST consume the same original frozen
    candidate binding, including an admitted legacy candidate. A current WI projection or its
    timestamp MUST NOT replace that identity or veto an otherwise valid publication.
    Managed verification assignment and ACK MUST also come from original kernel decisions;
    unpublished, missing, corrupted or forged handoff copies cannot veto or grant local
    admission. Independent source delivery and physical workspace requirements remain intact.
    Legacy cost-attribution and normalized-observation writers
    MUST share this retirement boundary; historical cost inputs remain readable, while new managed
    attribution and Pi/CC usage use the kernel measurement entry. Public execution status MUST read kernel definitions and
    terminal state before any ledger file; missing, malformed or terminal-looking copies cannot
    hide or override that state. Status must not require field-by-field projection parity.
16. A channel adapter MUST not manufacture semantic checkpoint content. It obtains canonical
    orientation, dispatches the authenticated intent and frontier to the current Session Agent, and
    submits that Agent's draft through `checkpoint-local` or `rollover-local`. Missing authority or
    frontier stops before draft construction and returns the typed result.
17. Stable result codes MUST express only facts supported by their evidence class. In particular,
    identity mismatch is `source_identity_unresolved`; positive source liveness requires runtime
    readback; kernel absence before migration is `local_frontier_unavailable`; and parity failure is
    `reconciliation_required`, not `work_item_not_found`.
18. Human-facing presentation MUST lead with mutation fact, durable-frontier safety and one
    available next action in the user's language. Raw status/capability/timing fields MAY appear as
    optional diagnostics, but a presenter MUST NOT turn unknown, absence or mismatch into a
    positive authority claim.
19. Missing input MUST be recoverable within the original business request. Semantic draft
    validation MUST name bounded field locations and error types without echoing private values;
    it MUST NOT be reported as unavailable local storage. The Agent can correct the fields and
    retry the same WI. A current Host supplies its own runtime/surface facts for first entry and
    human-authorized reinit; absence of an old execution binding is not a Host identity source.
    A resident Relay tool MUST obtain live Host observation without an Agent-authored proof file.
    Reused Host context from a process outside the tool's live ancestry is not accepted as live.
    Relay-managed CC print and interactive children MUST receive that same Host context after
    final native-session selection. Native recovery tools return their original input reference
    to the channel Host only after successful tool completion and turn settlement; a newer input
    or replaced process cancels dispatch. A native tool does not sign or issue execution authority.
20. A never-started canonical draft may amend its approved title and acceptance criteria through
    the existing audited publisher without first acquiring execution authority. The publisher
    MUST prove absence of active/retired execution and pending startup, retain draft/no executor,
    revalidate the preimage before publication, and preserve fingerprint/title concurrency checks.
    Unreadable control state is not absence; an active or frozen WI retains its existing route.
21. After a committed candidate withdrawal, an approved definition amendment MUST consume the
    current unfrozen SCL frontier, not reject it because the raw Git candidate projection is stale.
    Routing, the existing publisher and adoption MUST reuse one exact withdrawal-history proof.
    The original Git candidate must match its journal submission and committed withdrawal; a
    missing, mismatched or later-refrozen candidate remains rejected. Publication atomically
    records the corrected projection and one amendment event referencing that withdrawal, without
    another lifecycle decision, Session, epoch, checkpoint or grant. Receipt recovery verifies the
    original proof and preserves the same transaction; old candidate and event history remain.

22. Candidate publication MUST materialize the business definition and original history already
    owned by the frozen SCL candidate. An older service ledger, missing historical Broker journal
    or changed projected owner/title cannot override admitted kernel decisions. Historical
    amendments not yet admitted still require their original publication evidence at the import
    boundary; once admitted, publication does not reassemble an authority chain from every copy.
    The publisher MUST NOT merge business branches, rewrite original events, create another
    amendment or relax the current kernel owner/fingerprint/frontier guards. A withdrawn or
    superseded kernel candidate still rejects publication. Original no-effect failures recover
    through the same outbox request/transaction after independent main progress; acknowledgement
    loss/retry MUST NOT duplicate the candidate or either amendment.

`@anchor test agent-harness/tests/test_candidate_amendment_chain.py :: test_two_public_amendments_publish_one_candidate_with_original_history`
`@anchor test agent-harness/tests/test_candidate_amendment_chain.py :: test_candidate_uses_kernel_definition_and_current_candidate_authority`
`@anchor test agent-harness/tests/test_candidate_amendment_chain.py :: test_original_failed_request_recovers_after_restart_and_main_advance`

`@anchor test agent-harness/tests/test_withdrawn_candidate_amendment.py :: test_public_amendment_materializes_only_the_committed_withdrawal`
`@anchor test agent-harness/tests/test_withdrawn_candidate_amendment.py :: test_unproven_or_replaced_candidate_cannot_be_amended`

`@anchor manual ingress-machine-contract :: black-box tests invoke the installed binary and exact production Hook and Relay environments without a client override, cover parser errors, stderr diagnostics, escaped presentation, missing adapter runtime and same-intent canonical retry, and prove Feishu/Telegram parity on every registered resident node`

### R5. External effects and reconciliation

1. Git audit publication, GitHub CAS, surface replication, runtime readback, review lookup and
   notification MUST be effect adapters behind a common idempotent contract: `probe`, `plan`,
   `apply`, `readback`.
2. A provider outage, credential denial, remote movement, endpoint dirt or partial replication MUST
   mark only the affected effect pending or conflicting. It MUST NOT undo a safe local operation or
   block unrelated local work.
   GitHub review/closeout reads MUST use the Host's explicitly selected existing credential
   consumer. The default is `gh`; Muse may select its platform `custom.github` request adapter.
   Both query the live `api.github.com` repository endpoints with GET and preserve the same
   authority validation. An unavailable consumer, invalid response or redirect fails closed as
   `authority_unavailable`; it MUST NOT trigger another login, identity fallback, cached authority
   substitution or replay of a completed review. Credentials remain with their original consumer.

`@anchor test agent-harness/tests/test_github_authority.py :: test_muse_uses_request_scoped_existing_connection`
`@anchor test agent-harness/tests/test_github_authority.py :: test_failure_never_falls_back_or_exposes_credentials`
3. The reconciler MUST run after startup, after each local commit, on connectivity change and on a
   bounded periodic schedule. Commands MAY request immediate reconciliation but correctness MUST
   not depend on the user copying a recovery command.
4. Each effect MUST use its operation id and fence as the remote idempotency identity. Unknown
   result after timeout MUST be read back before a retry that could duplicate an irreversible
   effect.
5. Effects with incompatible remote truth MUST stop that effect, preserve evidence and request
   human judgment. Transient failures, known incomplete transactions and stale materialized
   projections MUST self-heal without human action.
6. A terminal WI MAY retire a Broker transaction left in `discovered` or `recovery_required`
   only when durable readback proves that it never prepared or published a commit and created no
   integration, replication or endpoint effect. Retirement MUST require explicit human
   confirmation, prove the recorded projection worktree is absent both physically and from Git's
   worktree inventory, revalidate the exact remote authority identity/base ancestry and terminal
   WI on the canonical branch, preserve the diagnostic transaction, and write an idempotent
   `git_broker_projection_retirement/v1` receipt with `projection_effect=none` before moving the
   transaction to `retired`. Unknown evidence fails closed; no journal row may be pruned or edited.
7. A terminal WI's stale site-local workspace registration MAY be retired only through a
   receipt-backed command after explicit human confirmation. Every registered worktree path and
   exact local/remote WI branch MUST be proven absent while the registered remote identity remains
   live and unchanged. The command MUST archive the byte-identical registration by digest, emit an
   idempotent `work_item_workspace_retirement/v1` receipt, remove only the exact active manifest,
   and fail closed on symlinks, digest drift, live paths/refs or unreadable remote truth.
8. A Git audit `remote_cas_moved` conflict MAY self-heal when live readback finds the exact immutable
   receipt already present. Otherwise it MAY self-heal only after proving the fence base is an
   ancestor of the exact registered worktree HEAD, that HEAD is an ancestor of the remote branch,
   and every later remote path is inside the immutable SCL audit root. The recovery MUST be durable
   before requeue. Unpublished registered product history, or remote product history absent from or
   divergent with the exact registered worktree, remains conflicting and MUST NOT be auto-merged or
   selected.
9. Git audit projection commits MUST run in an adapter-owned detached worktree with an empty local
   hooks directory because human-worktree pre-commit probes cannot observe that detached index.
   A worktree-installed pre-push hook MUST NOT be reused from that detached worktree: Git exports
   detached `GIT_DIR`/`GIT_WORK_TREE` values to the hook, so a hook that changes directory to the
   registered worktree can still validate the wrong index. The adapter itself is the pre-push
   authority guard: immediately before push it MUST revalidate the current lifecycle fence, exact
   durable registration, registered remote identity, immutable audit-only diff and remote CAS, then
   push without human-worktree hooks and without force.
10. A resident reconciler MUST resolve the authority repository from each effect's fenced WI
   registration. A process-global Harness root MAY be a compatibility hint, but MUST NOT select or
   disqualify a repository when the exact registration identifies one WI-local Harness root.
   Multiple registered product repositories MUST remain valid; zero and multiple matching
   authority roots are distinct deterministic failures.
11. `external_publish` MUST be projected from a bounded concrete adapter probe or a fresh durable
   effect receipt. A caller MUST NOT pass an unevaluated constant such as `None` and then report
   `external_dependency_unprobed` when the registered repository and remote are probeable.
12. An attempt-zero or previously `authority_unavailable` effect MUST be retried by the resident
    scheduler after dependency restoration. Recovery MUST use the original operation/effect/fence,
    require no new lifecycle command and preserve every prior attempt record.
13. Every Git command failure MUST retain bounded durable command evidence: exit code, sanitized
    stdout and stderr, original byte counts, SHA-256 digests and truncation flags. A completed
    non-zero push followed by an absent readback is a known rejected attempt, not
    `git_publish_outcome_unknown`. Only timeout, transport interruption or another result whose
    remote outcome cannot be proved may enter `unknown`, and its captured exception streams remain
    in the attempt evidence.
14. `external_publish=available` requires a bounded, non-mutating write-path probe through the same
    registered remote, branch, authentication and transport used by apply. Read-only `ls-remote`
    success is insufficient. The probe MUST exercise receive-pack and the adapter pre-push gate
    without changing the remote ref, preserve failure evidence, and distinguish credentials,
    transport, branch policy, registration drift and remote CAS.
15. A durable historical effect MUST remain replayable after the human checkout advances to a
    later legitimate branch. The adapter uses the effect's fenced registration, common Git object
    store and registered remote ref; a current checkout's symbolic branch or worktree-scoped guard
    MUST NOT silently replace that durable identity. A remote tip that linearly descends from the
    fenced authority base is compatible: apply starts from that exact remote tip and adds only the
    deterministic audit receipt under a fresh no-force CAS. A remote tip that does not descend from
    the authority base is divergent and remains conflicting. The human checkout's local registered
    ref may be ahead of the remote and MUST NOT veto this audit-only recovery.
16. A successful `ls-remote` with no matching registered branch is `remote_ref_absent`, not Git
    authority loss. If the exact registered local branch exists and its HEAD descends from the
    fenced authority base, the write-path probe MUST test a create-only publication from that HEAD.
    The Git audit adapter MAY then start its detached projection from that exact registered HEAD and
    publish the inherited registered history plus the new immutable receipt in one no-force branch
    creation. It MUST revalidate the absent ref, fence, registration, remote identity, registered
    HEAD and ancestry immediately before push. A concurrently created ref, divergent registered
    history, missing object, policy denial or identity drift MUST fail closed without selecting,
    rewriting or merging another history. Readback while the ref is still absent MUST return
    `absent`, so an unknown outcome retries apply instead of looping as `readback_failed`.

`@anchor manual effect-reconciliation-contract :: provider-neutral fault suite covers denial, timeout-before-send, timeout-after-commit, partial replication, compatible registered remote CAS recovery, incompatible product history retention and restart`

### R6. Storage pressure and runtime survival

1. The local kernel MUST live outside mutable Git worktrees and use transactional storage with
   checksums, schema migrations and crash recovery.
2. Installation MUST keep a verified last-known-good runtime and perform atomic activation of a
   candidate runtime. Import failure or mixed modules MUST fall back to last-known-good for local
   lifecycle service while exposing degraded upgrade status.
3. SCL MUST maintain a bounded, preallocated emergency journal reserve owned by SCL. Under pressure
   it MAY reclaim only that reserve and regenerable caches/projections; it MUST never delete user
   files, domain work, checkpoints, operations or audit evidence.
4. Compaction MUST preserve authoritative events, operation identities, fences and unresolved
   effects. Materialized Git views and caches may be rebuilt.
5. If durable write capacity truly cannot be recovered, the response MUST distinguish
   `local_persistence_unavailable` from an invalid WI. Read and conversation remain available.
6. The formal installer MUST activate the canonical SCL runtime pointer, run a direct handshake
   through that pointer and return its loaded revision/digest in the install receipt. Installing a
   stable Harness executable without repairing a dangling canonical SCL shim is not adoption.
7. Runtime reclamation MUST protect the outer immutable Harness releases referenced by canonical
   SCL `active` and valid `previous` pointers in addition to the stable Harness target, rollback
   target and live processes. A dangling pointer is surfaced and atomically replaced; it is never
   accepted as a successful readback.
8. Candidate handshake failure or post-switch readback mismatch MUST leave or restore the last
   verified canonical SCL pointer. Source revision metadata alone MUST NOT establish adoption.
9. Canonical SCL adoption and the general stable Harness pointer are distinct consumers. When the
   stable Harness revision contains unrelated non-ancestor work, the formal installer MUST support
   an SCL-only adoption that prepares and verifies the exact candidate release, preserves the
   stable pointer and live policy byte-for-byte, atomically advances only the canonical SCL pointer,
   retains its verified previous pointer and emits direct handshake plus preservation readback. It
   MUST NOT require merging another owner's branch or temporarily exposing a regressed stable
   runtime.

`@anchor manual storage-runtime-contract :: tests exercise ENOSPC before and after reserve release, corrupted candidate install, process kill during activation and rebuild of disposable projections`

### R7. Checkpoints and source preservation

Cross-host handoff of a kernel-owned business WI MUST carry the original business
history and admitted input versions with its sealed execution frontier. The target
MUST atomically import these facts before reconstructing views; it MUST NOT depend
on readable source ledgers or require repeated decisions. Same-name evidence from
different rounds MUST retain its original bytes and archive paths across further
transfers. Import cannot overwrite existing target originals or settle a source
external effect. Actual successor execution and old-writer refusal remain separate
from successful capsule import.

`@anchor pytest agent-harness/tests/test_cross_surface_rollover.py :: cross-host originals, replay, pristine target and actual successor authority`

1. Checkpoint content MUST record the actual execution frontier: objective, accepted requirements,
   decisions, open questions, next action, domain refs and freshness assumptions. It MUST not depend
   on the source transcript being readable.
2. Checkpoint revision allocation MUST be monotonic and append-only across every epoch in one WI
   lineage. Rollover or source adoption clears only the current checkpoint pointer; it MUST NOT
   reset or reuse the WI-wide sequence. A retry may return the same receipt but cannot overwrite a
   different checkpoint. Explicit human reinit creates a new lineage whose first checkpoint is
   revision one while the retired lineage remains immutable.
3. Checkpoint durability is local. Git audit publication and cross-surface replication are effects
   whose pending state does not invalidate the checkpoint.
4. Source retirement is a separately recorded operation after target readback. Failure to start the
   target leaves the source entry and checkpoint valid; recovery never fabricates target success.
5. Automatic draft artifacts are not authority unless explicitly adopted by the user or the
   lifecycle operation. Untracked user/Agent work remains untouched.
6. Before a long-Session rollover, the source MUST make the recovery frontier independent of the
   source transcript. Reusable requirements belong in normative Specs, stable authority and
   operating rules in the applicable workspace instructions or skill, and chronological evidence
   in a versioned memo. The final checkpoint MUST reference the merged artifacts by exact
   repository, path, default-branch revision, SHA-256 and role; record installed artifact and loaded
   runtime digests separately; retain topology/preservation receipts when authority moved; and
   replace any stale ResumePacket action with one current, executable `business_next_action`.
   A chat summary or unmerged draft alone MUST NOT satisfy durable consolidation.
7. Before first formally creating or starting a standard workflow, the Agent MUST proactively
   present its proposed route and obtain explicit normal-language continue/correction. Read-only
   investigation and a discussion sketch may prepare the proposal; they do not start the workflow.
   Generic WI handoff or WHY acceptance is not confirmation of an unseen route. Existing explicit
   confirmation of the same proposal MUST be reused across ordinary progress, retries and Sessions.
   Until alignment is recorded, checkpoint/ResumePacket next-action guidance MUST request alignment
   while retaining the proposed next action. The record is a bound Agent's coordination report, not
   a new authority grant or independently verified Host receipt. Material route or boundary changes
   return only the changed choices to the human.
8. Resume transports MUST preserve the effective `workflow_profile`, immutable
   `startup_workflow_profile`, transition reference and `workflow_alignment`. Consumer prompts
   MUST select the current method from the effective profile, preserve scoped grants, and delegate
   workflow ceremony to the installed lifecycle skill. Missing profile metadata remains unknown;
   neither old artifacts nor execution authority imply a standard workflow or human route approval.
   A canonical `observe_terminal` packet is a valid read-only result, never an instruction to
   restart execution. Local readiness requires both the packet's execution proof and available
   `local_work`; global publication does not invalidate an already committed local frontier.

`@anchor manual checkpoint-source-contract :: interruption tests prove new decisions survive, revisions remain WI-wide monotonic across rollover/adoption, explicit reinit starts only the new lineage at one, source remains usable until target proof and untracked drafts stay byte-identical`

### R8. Read model and truthfulness

1. Status MUST derive from durable operation, fence, checkpoint and effect evidence. A continuation
   id, planned target or published commit alone MUST NOT imply rollover completion.
2. Local effective state, global canonical state, projection freshness and effect coverage MUST be
   separately visible.
3. Unknown facts remain `unknown`; absence is not zero; pending is not completed; local commit is
   not global publication; remote publication is not runtime readback; visible UAT is not Outcome.
4. Read repair MAY rebuild materialized views but MUST not manufacture missing authority evidence.
5. A positive read-model label such as active, live, attached, replaced or completed MUST cite the
   evidence class that proves it. Inequality, missing linkage, an object id or a stale materialized
   enum is insufficient.
6. Capability projection MUST be action-consistent. If the current Host is known to be a retired
   source and the corresponding mutation would be rejected, `local_work`, `checkpoint` and
   `lifecycle_write` MUST NOT be reported available for that Host. A writable kernel is a storage
   fact, not Session authority.
7. Verification admission and registered-source Git proof MUST remain read-only when the producer
   repository and its Git administrative directory are read-only. They MUST NOT materialize an
   index tree or require an index lock. Git tree materialization belongs to mutation snapshots;
   source identity, published revision, frozen candidate, dirt and race checks remain required.
   Readers combining current SCL definition fields with ledger Outcome/handoff fields MUST label
   those sources separately; current definition alone does not establish candidate freshness.
   Rechecking a proven negative Outcome MUST use the same source-proof context as initial
   selection. A cleared candidate is `mismatch_candidate`, not unknown consistency; reading the
   rework source MUST NOT revive the old verification assignment or modify the SCL frontier.

`@anchor test readonly-verifier-source :: agent-harness/tests/test_registered_verification_admission.py::test_verifier_admission_does_not_need_a_writable_producer_index`

`@anchor test readonly-outcome-rework :: agent-harness/tests/test_candidate_rework_routing.py::test_admission_observes_published_rework_without_reviving_old_candidate`

`@anchor manual truthful-read-model :: state-machine tests enumerate prepared, local committed, effect pending, globally committed, applied, recovery and conflicting evidence projections`

### R9. Migration and compatibility

1. Existing Git-resident ledgers MUST be importable into the local kernel with byte digests and a
   recorded source revision. Migration MUST be per WI or bounded cohort and idempotent.
2. At every moment, one declared store is the write authority for a WI. Dual-write may be used only
   as an observed projection during migration, never as two independently accepted authorities.
3. Legacy commands and projections MAY remain as compatibility consumers, but must call the new
   operation protocol for writes. Manual YAML editing is not a recovery mechanism.
4. Rollback MUST reactivate the verified last-known-good runtime and preserve operations created by
   the candidate. It MUST not reset a worktree or discard pending effects.
5. On a kernel miss, SCL MUST perform at most one bounded, idempotent import from the exact
   configured canonical local source before returning absence. Import includes WI, binding,
   checkpoint, continuation/replacement and writer authority as one parity unit; it does not scan
   historical registrations or choose a source by file order.
6. Import of an active legacy resident-surface binding MUST preserve its logical source and either
   import an exact source attachment or mark source identity unresolved. It MUST NOT silently treat
   a Harness-generated surface token as a digest of the current provider Session.
7. Legacy binding lifecycle and continuation/replacement state MUST project one consistent stage.
   A ready or claimed continuation that disagrees with an imported `replacement_stage=none` is a
   reconciliation condition; SCL MUST NOT reserve a second successor.
8. Compatibility commands MUST read through the kernel materializer after writer cutover. Installed
   entry and rollover on one node MUST NOT report different epoch/checkpoint frontiers for the same
   WI.
9. Once a managed lineage owns a WI, the registered `worktree launch` compatibility entry MUST use
   that lineage's current Session and execution authority. It MUST NOT create, refresh or append a
   repository-resident legacy execution binding merely because the projection is absent.
10. A pre-fix launcher binding and its paired `execution.bound` append MAY be retired only by one
    typed SCL reconciliation operation. The operation MUST prove the current fenced Session,
    registration generation, repository/branch identity, tracked event baseline, exact event and
    binding digests, launcher-generated semantic shape, and an otherwise-clean registered worktree.
    Cleanup runs as a replayable effect, accepts only its two crash-intermediate states, records a
    durable receipt, and fails closed without changing any byte when other dirt or CAS drift exists.
11. After that reconciliation removes the legacy binding, a claimed, integrated `main_only` WI MUST
    still be able to freeze `ready-for-uat` on canonical service authority. This fallback is limited
    to `READY_FOR_UAT`, requires the canonical WI to remain open with `active_executor=owner`, and
    MUST NOT grant handoff, verify-result or any other claimed closeout mutation authority.
12. Once SCL is managed, canonical mutation authority resolution MUST admit only the exact surface
    and registration digest selected by the current SCL authority fence. Durable registrations and
    legacy bindings from historical surfaces remain auditable but MUST NOT participate in mutation
    routing, mask the current reconciled registration, or require deletion to make progress.
13. Independent-verification assignment, Outcome and review source resolution MUST use the
    kernel's current published freeze, producer/epoch, criteria, candidate and committed event.
    Copying a matching WI/event projection to main cannot transfer publication authority;
    missing or divergent projected fields cannot veto the kernel's candidate. Registered sources
    retain the current SCL resource fence and Git guards. Source readback rechecks the kernel
    frontier, frozen operation and successful publication effect before returning its proof.
    Unpublished or superseded kernel candidates fail without effects. Final candidate settlement
    and withdrawal retain their own contracts; imported legacy candidates with no SCL freeze retain
    the existing compatibility route.
    A never-started, unclaimed main-only Draft or Ready item has no freeze to prove: routing MUST reuse
    the prestart responsibility-transfer absence check. Current/retired lifecycle history,
    registration (including dangling paths), legacy execution material, claimed/epoch-bearing
    items and unreadable authority MUST NOT qualify. This exception MUST NOT initialize a kernel row
    or grant execution authority. The later full-responsibility ACK still
    validates its sender, bound single recipient and scope; source routing grants no execution.

`@anchor test agent-harness/tests/test_draft_handoff_transfer.py :: test_candidate_routing_only_exempts_proven_unstarted_item`
`@anchor test agent-harness/tests/test_draft_handoff_transfer.py :: test_real_canonical_publisher_transfers_draft_then_activates_receiver`

`@anchor manual migration-compatibility-contract :: legacy fixtures import twice identically, managed worktree launch reads only the kernel, exact pre-fix launcher residue retires through a receipt-backed effect, mixed-version clients preserve one writer and runtime rollback retains candidate-era operations`

`@anchor test agent-harness/tests/test_scl_lineage_reinit.py :: test_managed_reinit_worktree_launch_uses_kernel_without_writing_legacy_projection`
`@anchor test agent-harness/tests/test_scl_lineage_reinit.py :: test_managed_reinit_reconciles_only_the_exact_legacy_launcher_projection`
`@anchor test agent-harness/tests/test_canonical_mutation.py :: test_claimed_main_only_ready_for_uat_falls_back_to_service_when_registration_is_stale`
`@anchor test agent-harness/tests/test_canonical_mutation.py :: test_claimed_main_only_stale_registration_does_not_broaden_other_closeout_commands`
`@anchor test agent-harness/tests/test_canonical_mutation.py :: test_managed_scl_authority_ignores_historical_bound_registration`
`@anchor test agent-harness/tests/test_scl_lineage_reinit.py :: test_managed_reinit_worktree_launch_uses_kernel_without_writing_legacy_projection`


`@anchor test agent-harness/tests/test_scl_candidate_source_recovery.py :: test_published_skill_handoff_does_not_select_old_main`
`@anchor test agent-harness/tests/test_scl_candidate_source_recovery.py :: test_copying_projection_to_main_does_not_transfer_publication_authority`
`@anchor test agent-harness/tests/test_scl_candidate_source_recovery.py :: test_assignment_uses_committed_event_when_projection_is_old_or_missing`

### R10. Lineage cutover and human-authorized reinitialization

1. A release that cannot prove one live frontier for every previously kernel-owned WI MUST start a
   new SCL lineage. The cutover MUST atomically retire the prior lineage from scheduling and
   authority resolution before the new lineage accepts writes. Prior databases, projections,
   operations, effects, attachments and receipts remain checksum-addressed read-only audit evidence;
   they MUST NOT be retried, hydrated as current state or allowed to resurrect an old frontier.
2. A WI that has no valid state in the new lineage MUST return `reinit_required`. It MUST NOT import
   a known split-brain frontier, guess a winner, report another Session active, or expose a generic
   permission failure as the user's next action.
3. Reinit separates requesting, authorizing and implementing a change. Any caller MAY request it
   before old execution admission, complete-draft validation or active-kernel initialization.
   `/wi reinit <wi>` from an authenticated human authorizes recovery within the actual conversation
   scope; an Agent's request or self-declared human flag does not. Authorization derives
   from the authenticated human command and the current conversation, not from the executor,
   binding, Session, attachment, candidate, replacement, registration or authority fields being
   rebuilt. Those historical fields are evidence and MUST NOT gate reinitialization.
   Ordinary authenticated human messages MUST be usable through the same Host boundary. The Agent
   interprets scope and references a Host-recorded message; it does not sign its own receipt or require
   the human to repeat approval as a slash command. Host results, including correctable startup input
   and original request identity, MUST return to the current Agent after the control action settles.
   New human input received after a queued action MUST withhold that stale action and business dispatch.
   A created but unbound WI MUST resume its unique original startup request and creation receipt.
   Unknown admission effects MUST be observed before revision; a rejected disposable projection with
   no prepared/published effect and no remaining worktree permits correction of unconsumed fields.
   Replaying the same recovery decision MUST retain the original startup identity and execution epoch.
4. Reinit MUST first apply the smallest change required by the valid decision: projection repair,
   scoped grant/delegation, resource adoption or Host binding. These changes MUST preserve the
   existing business frontier. Only a necessary, authorized full rebuild replaces the effective
   current definition: objective, acceptance criteria,
   owner/executor, phase, workspaces, checkpoint/frontier and first business action. Missing semantic
   information is resolved within the existing decision; only undecided business choices require
   human input. Full-rebuild draft completeness MUST NOT gate the request, grant recording or a
   bounded repair. For canonical adoption, the authenticated human decision MUST commit before
   registration discovery, managed-worktree allocation, Git probing or consumer readback; those are
   realization steps and may leave capabilities pending/degraded without undoing authorization.
   A missing active kernel row MUST be materialized only from the surviving canonical title/owner/
   acceptance criteria; unknown progress remains unknown and no checkpoint is fabricated. Both modes
   preserve WI identity, Git-visible work, artifacts and immutable history; objective, acceptance
   criteria and responsibility change only when the decision covers them.
5. One full-rebuild transaction MUST advance beyond every superseded epoch, bind a new logical source
   durably, write checkpoint revision one, and leave candidate, replacement, continuation, handoff
   projection and authority transition empty. It MUST commit this local frontier even when no old
   Session can be resumed and no registration or authority row can be trusted. When the exact
   current workspace validates, the same transaction also installs its registration and a new
   authority generation. Old blocking effects are superseded; immutable audit evidence remains
   queryable.
   Once that new-lineage WI exists, all execution consumers, including initial worktree registration,
   re-registration, extension, checkpoint, rollover, continue and Git guards MUST resolve current
   scoped authorization. Retired YAML owner/executor fields MUST NOT act as a fallback permission
   gate. Missing registration is a product-owned realization step, not prerequisite approval.
6. Full-rebuild local commit requires one durable new frontier, an active executor, a source attachment
   and checkpoint revision one. This proves persistence, not that the user's repair succeeded.
   If installed worktree and Git-guard consumer readback also succeeds,
   the result is `reinit_succeeded + execute_fenced`. If registration, worktree, Git, guard or
   channel-consumer consistency cannot be proved, the result is
   `reinit_degraded_ready + execute_local_isolated`: local work and checkpoint remain available,
   while only dependent external operations remain unavailable; valid delegation decisions and
   human authorization requests remain reachable. A
   control-plane consistency failure MUST NOT turn a committed local frontier into
   `reinit_initializing`, automatic retry or a WI-continuity veto.
   Validation is scoped to the exact registered WI worktrees and their Git identity. Dirt in a
   separate shared primary checkout MUST be preserved byte-for-byte and MUST NOT block reinit or
   continuation merely because the registered worktree uses the same Git common directory. Dirt in
   the registered WI worktree is preserved as the WI's in-progress work and is not a reinit veto.
   All public execution consumers MUST use the shared effective permission resolver. Missing
   `HARNESS_BRANCH_REF` MUST NOT veto a valid current registered Host grant. An explicit conflicting
   branch or caller context MUST be rejected; owner/basename inference MUST NOT grant authority.
   Existing registration recovery MUST preserve staged, unstaged and untracked bytes, including the
   index during readback. Exact Git identity, safe clean bootstrap of a new worktree, candidate and
   terminal boundaries, concurrent resource writers and publication uncertainty remain enforced.
   Consumer readiness MUST execute the installed hook and selected runtime in the same actual Host
   context as normal Git, validate its semantic receipt, and require no production commit. Wrong
   runtime, failing hook or missing readback MUST NOT report ready; retries reuse the same decision.
7. Reopen remains the narrower operation for withdrawing a valid frozen candidate. Reinit MUST NOT
   be emulated by a sequence of reopen, manual database repair, registration scanning or legacy
   writer calls.
8. After a successful rollover, one fresh Session invoking `/wi continue` MUST claim, attach,
   apply, fence the old source and restore the complete checkpoint in one resumable operation.
   When that successor retains the exact current registration digest, a multi-repository workspace
   MUST select its runtime worktree by the canonical repository's exact Git identity; it MUST NOT
   require the canonical `.harness` subtree to be materialized in that sparse business worktree
   merely to continue the same authority. Independent registration adoption still requires the
   normal exact store route. Fenced consumer readback enables external effects; a failed
   control-plane readback degrades the same successor to local-isolated execution and MUST NOT
   prevent its business work from continuing.
9. When a full rebuild is necessary and authorized, reinit MAY atomically replace any current frontier of the named WI, including a
   non-empty, dependent, candidate, replacement, retired-source or terminal frontier. It advances
   the epoch, binds the exact previous local digest as transaction precondition and preserves all
   prior rows as immutable journal/audit evidence. Exact idempotent replay returns the original
   operation; only a changed request advances another epoch. When the canonical WorkItem definition
   exists, ordinary product reinit MUST prefer canonical adoption even if the active kernel row,
   executor, attachment or registration is absent. The installed client may express that operation
   as `--reuse-canonical`; this is the normal minimal-recovery implementation, not a request for new
   business semantics. Full-rebuild `--input` is reachable only from an explicit
   `business_definition_unavailable` condition or a human decision that intentionally replaces the
   business definition. Generic `work_item_not_found`, registration/resource realization failure,
   owner/executor drift or consumer incompatibility MUST NOT trigger a model-generated full draft.
10. A repair request MUST remain resumable until its requested capabilities are realized. Replaying
    the same human decision must retain completed stages and retry only unfinished realization;
    it MUST NOT merely return a prior kernel commit while registration still fails. Report grant
    accepted, implementation pending and physical denial separately. Only actual consumer/business
    proof satisfies repair completion. For stale-YAML registration failures, one reinit invocation
    must repair/adopt the necessary registration and allow the authorized next action without manual
    YAML edits, re-claim, another WI, another approval or an additional reinit ritual.
11. Human-authorized recovery MUST restore an executable business path. Configuration, environment,
    permission-projection and implementation failures in ordinary admission MUST NOT make the human
    route internal repairs or repeat authorization. The Host first restores ordinary execution; if
    that fails, it realizes the same decision through the independent guarded local path and returns
    the executable action to the original Agent. Repair, isolated continuation and necessary
    reconstruction preserve all business data, dirty/index bytes and audit history. Explicit human
    permission to reconstruct a legal successor WI allows that final recovery option; a successor
    must carry the original work and provenance rather than copy defective execution credentials.
12. Managed local process admission MUST NOT depend on mapping the canonical service ledger into a
    business worktree. The registered resource, current grant and physical Host determine process
    admission. Canonical ledger mutation and external publication retain their own validated writer
    paths. Reinit readiness MUST exercise the same installed public launcher in the actual Host
    context, including a semantic child receipt; hooks or an exit code alone are insufficient.
13. Automatic isolated realization MAY use `scl_host_recovery_admission/v1`: the immutable original
    human receipt plus a separately Host-signed, narrower local scope. This is realization evidence,
    not a new human event or authority database. It cannot broaden an existing signed scope, withdraw
    a candidate, authorize publication or overwrite the original receipt. Normal rejoin consumes the
    original decision and restores only its original authorized capabilities.
14. An authenticated maintenance request may resume a durable human recovery input after a Host
    restart through the original channel/Agent/session. It MUST resolve the original stored message
    and signed receipt, reject a superseded input or conflicting session, preserve request identity,
    and deliver the resulting executable continuation to that same conversation. A maintenance API
    MUST NOT accept caller-supplied human provenance, bypass a newer stop instruction or report
    delivery merely because canonical reinit committed.

`@anchor manual reinit-cutover-contract :: true-shape upgrade fixtures quarantine the old lineage, prove deterministic canonical adoption when the WorkItem definition survives, prove full-draft rebuild only when that definition is truly unavailable, and then prove rollover/fresh-session continue through installed worktree and Git-guard consumers`

### R11. Terminal truth and projection cleanup

1. Any WI with a legacy execution binding, `main_only=true`, or a canonical sealed-SCL audit
   receipt is terminal-controlled. Applying an accepted Outcome receipt MUST preserve
   `awaiting_verification`; it MUST NOT directly emit `work_item.completed`, archive handoffs or
   bypass human review and cleanup merely because no legacy execution binding exists.
2. Terminal-controlled completion MUST validate the exact current human approval, published
   closeout evidence, default-branch/CI/smoke authority, required consumer convergence and cleanup
   inventory. Only a real execution binding is retired through the execution subsystem; absence of
   that legacy binding MUST NOT disable the other terminal gates.
3. Cleanup inventory MUST distinguish disposable branches/worktrees/drafts from immutable Outcome,
   review, convergence, write-manifest and settlement evidence. Evidence MUST remain available
   after cleanup. A status read for a terminal canonical WI MUST return that canonical terminal
   truth before consulting retained historical workspace registrations; those audit manifests MUST
   NOT manufacture an active execution ambiguity. Unknown provenance or dirty state MUST remain
   residue, not become a guessed deletion.
4. A Harness Broker projection in `prepared` or `pushed` state MAY be retired only by a typed
   Broker operation with explicit human confirmation and a bounded reason. The operation MUST
   prove the exact worktree is clean at `prepared_commit_sha`, the current remote authority branch
   contains a terminal copy of the WI and descends from `base_sha`; a pushed transaction also
   requires `published_sha` to be an ancestor of that authority.
5. Projection retirement MUST write a durable `git_broker_projection_retirement/v1` receipt,
   remove only the exact registered projection through Git worktree machinery, and transition the
   transaction monotonically to `retired`. It MUST be idempotent and crash-recoverable across
   receipt write, worktree removal and transaction update. It MUST NOT fall back to raw filesystem
   deletion, rewrite history, delete audit evidence or treat retirement as successful while the
   registered worktree still exists.
6. An explicitly requested, single-valued `disposition --to cancelled` for a claimed,
   `main_only=true`, `in_progress` WI MAY use canonical service authority only when exactly one
   regular, readable registration and the managed SCL read-only projection agree on WI and owner
   (and on surface when SCL has one); the SCL frontier is owner-claimed and `local_isolated` with no current authority fence;
   execution status returns exact `not_found`; and the registered worktree, branch, remote and base
   ancestry all pass the normal clean authority checks. The exception is limited to cancellation,
   MUST be bound into the mutation authority fingerprint, and MUST be re-proved byte-for-byte after
   initial convergence immediately before Broker issue. It MUST NOT admit a missing, symlinked or
   ambiguous registration, owner mismatch, any current fence (including expired or conflicting),
   any execution binding, a different disposition, or another lifecycle command. Cancellation MUST
   commit before registered worktree or branch cleanup; cleanup MUST NOT be used to manufacture the
   negative-authority proof.

`@anchor manual terminal-cleanup-contract :: inject bound, main-only, sealed-audit and true-legacy Outcome shapes plus prepared/pushed Broker projections; prove only true legacy retains instant completion and only receipt-backed retirement removes exact clean residue`

`@anchor test agent-harness/tests/test_canonical_mutation.py :: test_claimed_orphan_cancel_runs_real_managed_cli_publisher_and_convergence`

`@anchor test agent-harness/tests/test_worktree.py :: test_managed_claimed_orphan_requires_owner_claim_and_no_fence`

## 6. Human intervention boundary

Human action is required only for an irreducible choice or genuinely new authorization, not because automation lacks a retry path. Existing authorization and valid in-scope delegation MUST be reused without another human approval.
Examples are incompatible competing global authority claims, ambiguous provenance for an
irreversible effect, or an explicit product decision such as withdrawing a frozen candidate.

Network outage, GitHub permissions, hook interception, installed-runtime drift, stale projections,
known partial transactions, endpoint unavailability and recoverable disk pressure are not by
themselves reasons to require the user to repair SCL or abandon a WI.

When legacy data contains no mechanical Host attachment, only the contextual human or Session
Agent can assert that the current Host Session is the existing logical source. SCL records that
bounded decision once; it does not infer it, call it `reopen`, or make the user repeat lifecycle
commands while migration and attachment machinery catches up.

An authenticated `/wi reinit <wi>` is itself the human authorization to replace an unusable current
control-plane definition. The Session Agent asks only for missing semantic WI information. It MUST
NOT ask the human to supply or approve private Session, operation, continuation, fence, attachment
or registration identifiers, and corrupt historical authority MUST NOT veto reinit.

`@anchor manual human-intervention-boundary :: fault taxonomy review demonstrates every mechanical failure maps to automatic recovery or capability degradation, with human-required reserved for value or conflict decisions`

## 7. Non-goals

- SCL does not make remote source bytes available when they were never present locally.
- SCL does not claim global consensus during a network partition.
- SCL does not auto-merge conflicting domain content or make human review decisions.
- SCL does not absorb, clean, stash, reset or commit unrelated dirty work.
- SCL does not make a skill, prompt or a particular Agent implementation part of the authority
  boundary.
- SCL does not weaken Outcome, review or terminal evidence requirements; it makes their unmet
  external effects explicit and non-blocking for ordinary local work.

## 8. Acceptance gates

Implementation is not accepted until all gates pass through a sealed installed runtime:

1. model/property tests prove authority and idempotency invariants over generated operation orders;
2. crash injection covers every local transaction and external-effect boundary;
3. provider-neutral fault injection covers network, credentials, timeout, partial success and
   stale readback;
4. local resource tests cover ENOSPC pressure, reserve recovery and runtime rollback;
5. ingress tests cover hook missing/failing, renderer escaping and machine-envelope failures;
6. registration tests cover historical, retired, duplicated and drifted registrations;
   same-authority continuation of a single registered business repository resolves that workspace
   from its verified registration digest without requiring the separate canonical control repository
   to exist inside it. This does not relax independent adoption or ambiguous multi-repository routing;
7. migration tests use real legacy ledgers and preserve dirty worktree bytes;
8. an isolated true-shape UAT demonstrates checkpoint, replacement and recovery before any real WI
   is reused;
9. the subsequent real UAT uses the original WI and verifies that no necessary decisions or domain
   artifacts were lost;
10. no implementation branch may contain WI ids, Agent names, provider names, fixed hostnames or
    repository paths as lifecycle logic.
11. installed Hook UAT uses the production discovery path with no `SCL_LOCAL_CLIENT_EXECUTABLE`
    override and proves the shared base-interpreter prefix remains untouched.
12. the exact immutable Relay release and compatible sealed SCL runtime are installed and read back
    independently on ownermac, cloud-linux-a and cloud-linux-b; all three retain a verified LKG;
13. every resident node installs and reads back both channel code paths; every actual
    post-activation Feishu or Telegram bot route runs visible `/wi` UAT and records exact
    release/runtime digests, one bounded machine result, stable safe diagnostics on a negative case,
    preserved local work, and the first real business action on a positive case. A non-resident
    route is not invented by duplicating a Telegram poller merely to fill a Cartesian matrix.
14. a cold-kernel fixture containing a real-shape canonical legacy WI imports once with exact
    binding/checkpoint/replacement parity; genuine absence and parity conflict remain distinct;
15. an active resident-surface fixture without Host attachment returns
    `source_identity_unresolved`; contextual attachment preserves epoch, candidate, source and
    responsibility, after which same-Session checkpoint and rollover use that one frontier;
16. structural and installed tests prove every Relay lifecycle verb uses sealed SCL and no direct
    channel path reaches legacy lifecycle mutation;
17. Feishu and Telegram present human-readable mutation, frontier-safety and next-action text for
    each machine status without unsupported positive claims; optional diagnostics retain the exact
    machine code and capabilities.
18. a release-level lineage-cutover fixture starts from copied WI568-, WI282- and WI479-shaped split
    states, archives all prior live state read-only and proves no old lease or pending effect runs;
19. `/wi reinit` distinguishes three internal outcomes without consulting old executor/binding
    permission: existing-frontier reauthorization preserves its business frontier; canonical adoption
    deterministically materializes a missing active row from the surviving WorkItem definition and
    creates no synthetic business checkpoint; explicit full rebuild alone creates a clean
    epoch/checkpoint/attachment from complete semantics. In every mode the human decision commits
    before resource realization, and missing/stale registration or managed allocation degrades the
    dependent capability instead of triggering another approval or model-generated draft;
    an existing target-neutral or claimed-but-unapplied Session reservation remains an unfulfilled
    lifecycle request. Reauthorization MUST rebase it above the new source epoch with a new
    target-neutral reference, retain the checkpoint, and drop any old target identity. A fresh
    Session then claims that one rebased slot; reinit MUST NOT silently erase the request.
20. the same reinitialized Session checkpoints and rolls over, then one fresh Session invokes
    `/wi continue` once and receives the complete business frontier, including every ordered
    checkpoint artifact ref, with `fenced` or truthful `local_isolated` execution;
21. installed pre-commit and pre-push guards plus a disposable local bare remote accept the new
    executor while the old source and all retired-lineage identities fail closed;
22. process-kill, retry and runtime restart at every pre-commit reinit/continue stage resume the same
    operation automatically; post-commit dependency or consumer failure returns local degraded
    readiness without retry loops, database edits or another human lifecycle command.
23. a true-shape Git-effect fixture supplies a deliberately unrelated process Harness root plus two
    registered repositories, only one of which contains the exact WI-local Harness registration;
    the existing effect settles through that registration without repository-path or WI-specific
    product logic;
24. a sealed client fixture with a reachable disposable remote projects
    `external_publish=available`; loss and restoration of that remote changes only the external
    capability and the original pending effect subsequently succeeds automatically;
25. the formal installer repairs a dangling canonical shim on an otherwise no-op install, direct
    handshake returns the installed revision, and a later reclaim retains the referenced outer
    release;
26. release acceptance requires source tests, sealed installed-runtime tests, three-node canonical
    handshake/readback, and read-only observation that an existing real-shape pending effect
    advances to `succeeded` with `external_publish=available`. A new release directory or stable
    executable alone does not satisfy runtime adoption.
27. a reinit/checkpoint/rollover/fresh-continue fixture keeps unrelated modified and untracked bytes
    in the shared primary checkout unchanged; a valid WI registration receives fenced Git
    commit/push authority, while an invalid registration still receives local execution authority.
    Shared-root dirt neither becomes WI content nor blocks the lifecycle, and no post-reinit response
    carries a retired-baseline refresh warning.
28. an installed Codex fixture runs `/wi continue` with distinct Hook-facing and tool-environment
    opaque identities for one fresh Host Session; claim/apply occurs once, the explicit retry
    rebinds only the applied successor attachment, and the final response is
    `local_committed + continue_ready + execute_fenced`.
29. the same fixture proves the old source Host identity cannot use `/wi continue` to rebind the
    applied successor, while a repeated target retry is idempotent and never creates a second
    frontier or epoch.
30. response tests inject a post-apply identity/readback failure and prove the user-visible result
    says mutation occurred, the successor frontier is safe and the sole recovery action is the
    same `/wi continue`; it never reports `read_only` or “no lifecycle mutation”.
31. a disposable Git-effect fixture makes the detached-worktree pre-push hook fail from inherited
    Git environment, proves the adapter write probe reports the exact stderr, then proves the
    adapter-owned fence/CAS gate publishes once, stores complete command evidence and reads the
    immutable receipt back as `succeeded`;
32. data-injected placeholder, active, candidate, replacement and terminal frontiers each accept one
    complete human reinit draft, preserve the exact prior precondition and advance one clean epoch.
    Exact retry is idempotent; a changed human request supersedes the prior frontier again. Invalid
    workspace authority returns `reinit_degraded_ready + execute_local_isolated` rather than rejection;
33. every workflow changed by this slice uses registered trusted runner groups explicitly. The
    retired adjudicate shell contract runs only on `pulse-linux-trusted` with
    `pulse-control-plane`; its redundant macOS matrix is removed rather than falling back to a
    billable GitHub-hosted label.
34. a long-Session fixture advances source, installed runtime, shared-root topology and UAT state
    beyond an older ResumePacket, then proves rollover restores the merged Specs/instructions/memo
    artifact refs and the current UAT business action rather than the stale deployment action.
35. after successor apply, the old Host receives `retired_source_session` with truthful unavailable
    write/checkpoint capabilities and cannot attach, checkpoint, rollover or edit a registered WI
    worktree; the response does not claim the successor is live without Host readback.
36. explicit human cross-Session coordination may deliver a non-authoritative handoff packet to the
    canonical successor, but delivery alone changes no WI state. Only the receiver's fenced
    canonical call can commit a checkpoint or rollover, and an unauthorized automatic wake is
    rejected by the Host adapter.
37. bound, main-only and sealed-SCL-audit WI fixtures remain `awaiting_verification` after accepted
    Outcome and emit no terminal event, while a truly ungated legacy fixture preserves historical
    completion semantics;
38. prepared and pushed Broker projection fixtures retire only with explicit human confirmation,
    clean exact prepared HEAD, terminal current authority and required ancestry. Dirty, nonterminal,
    divergent or unconfirmed cases preserve the worktree and transaction unchanged;
39. process interruption before and after projection worktree removal replays one immutable
    retirement receipt and one `retired` transaction, never raw-deletes the directory and never
    removes Outcome/review/convergence/settlement evidence.
40. a real managed-lineage fixture with a clean sole registration, owner-claimed in-progress
    frontier, absent fence and exact absent execution binding accepts only explicit cancellation
    through ordinary CLI, canonical publisher, Broker and endpoint convergence. Existing binding,
    current fence, owner mismatch, duplicate registration, another disposition and another command
    all fail closed; retry emits no second request or lifecycle event.

`@anchor manual resilience-acceptance-gates :: retain exact installed revision, commands, fault schedule, machine envelopes, state digests and user-visible results for all forty gates`

### Delivered terminal adoption

`adopt-terminal-local` consumes the original completed `closeout complete` transaction and its exact published, fully replicated terminal evidence. It does not replay completion, publish Git, or require a retired producer workspace/registration. A current frozen local candidate may atomically adopt `completed`, preserve its checkpoint/history, retire its authority and cancel remaining local effects. A newer reinit/freeze or mismatching claim rejects adoption without mutation. Exact retries preserve the operation and local revision; `--dry-run` only proves readiness. Terminal entry is `observe_terminal`; local work, checkpoint, external publication, cross-agent handoff and terminal settlement capabilities are unavailable even if an obsolete fence remains readable.

Lightweight completion MUST use the kernel-owned title, acceptance criteria and execution state.
Legacy lightweight admission is imported once from the original policy-default decision. After
that commit, profile queries, retries and completion MUST use its original kernel operation.
Missing, changed or damaged legacy admission files MUST NOT veto or replace the accepted decision.
An unimported admission remains required original evidence; incomplete kernel proof is rejected,
and a standard profile or a subsequent promotion still forbids lightweight completion.
Missing, malformed, stale or divergent WI files are rebuilt from the committed decision; they do
not veto completion or substitute a different definition. The kernel records the completion once
and retains its original publication request while delivery is pending. Canonical publication and
replication prove delivery of that decision, not another authority to decide completion.
`adopt-terminal-local` also consumes an original delivered historical lightweight completion;
it selects the validator from the actual transaction command, not from the command's name.

An earlier delivered acceptance amendment omitted from main may be reconciled through
`worktree integrate-event-history --amendment-transaction <original>`. Planning is read-only.
Apply requires human closeout confirmation, the exact original pending-adoption owner and registered
scope, the original delivered source definition and history, a delivered amendment predating the accepted lightweight
completion, and unchanged non-definition fields. The merge preserves every original YAML event
document and each history's order, retaining one completion event and terminal status. It creates
no new lifecycle decision. Unknown transactions, post-completion decisions, modified original
documents, unpublished source changes and unrelated definition changes reject before mutation.
Additional non-lifecycle source evidence must already match main exactly; integration cannot publish
new business work through terminal recovery.

After main contains both original publications, lightweight terminal adoption may consume the
same amendment transaction as evidence. It validates the unchanged original completion and amendment,
checks current canonical fields against that original amendment, and binds the reconciled definition
revision. It does not create a second completion or restore execution authority. The original
publications and recovery receipts remain rollback evidence; physical retirement follows adoption.
Broker Git blobs remain raw bytes for original-event validation; only the hexadecimal definition
revision is decoded to text for the local adoption proof.

`@anchor test agent-harness/tests/test_lightweight_closeout.py :: test_lightweight_completion_rebuilds_unpublished_definition`
`@anchor test agent-harness/tests/test_lightweight_closeout.py :: test_legacy_policy_default_admission_backfills_lightweight_profile`
`@anchor test agent-harness/tests/test_lightweight_closeout.py :: test_legacy_profile_rejects_incomplete_kernel_proof`
`@anchor test agent-harness/tests/test_lightweight_closeout.py :: test_lightweight_completion_uses_owned_state_and_rebuilds_wi`
`@anchor test agent-harness/tests/test_worktree_event_integration.py :: test_terminal_plan_preserves_original_amendment_and_completion`
`@anchor test agent-harness/tests/test_worktree_event_integration.py :: test_terminal_plan_rejects_unproved_or_new_business`
`@anchor test agent-harness/tests/test_worktree_event_integration.py :: test_delivered_lightweight_proof_accepts_only_original_precompletion_amendment`
`@anchor test agent-harness/tests/test_lightweight_closeout.py :: test_public_terminal_adoption_dispatches_original_lightweight_transaction`
`@anchor test agent-harness/tests/test_worktree_event_integration.py :: test_terminal_reconciliation_still_requires_each_original_path_grant`

Legacy singleton business-repository registrations MUST support immutable SCL audit publication without requiring a preexisting local `.harness` directory. Canonical WorkItem authority remains separate; unresolved multi-repository selection and redirected audit roots MUST NOT be guessed or bypassed.

### Local startup publication

Direct WI creation, first-entry admission and owner activation publish through the
existing canonical broker and read back the unique local publisher. For creation,
the canonical remote CAS MUST establish the unique WI identity before local adoption;
there is no pre-existing WI record for peer convergence to validate. An unavailable
replica cannot veto this local startup. The broker transaction and outbox remain
published with replication pending; they are not completed by a local readiness
receipt. Startup results expose `pending_publications`, retaining each original
transaction and recovery reference. A later replica recovery reuses the same
published commit and performs the unchanged all-endpoint WI replication barrier.
Other canonical commands retain their existing completion barriers.

Local startup MUST consume the already-enrolled topology manifest and validate only the unique
local service endpoint it is about to use: canonical path, branch, exact remote URL, clean state,
and pinned authority SHA. It MUST NOT re-resolve SSH aliases or probe unrelated peer endpoints
before local startup becomes usable. Full peer identity validation remains mandatory during
topology enrollment and explicit/all-endpoint convergence; this startup optimization does not
weaken those barriers.

A killed startup publisher may leave its request at issued while its Broker transaction is already pushed. Recovery must inspect and validate that original transaction before comparing source bases, adopt its existing publication locally, and never republish it.

If a birth mutation has prepared an unpublished commit and its Broker lease expires,
renewal MUST revalidate the same service startup authority used to issue the birth.
The WI registration cannot yet exist in canonical state. Renewal MUST still compare
the current publisher identity and authority payload, the original source and
prepared bytes, and the frozen remote base before pushing the original commit.
It MUST NOT create a second WI, re-execute the mutation, or relax the Git guard.

`@anchor test agent-harness/tests/test_prepared_projection_renewal.py :: test_expired_prepared_create_resumes_before_work_item_registration`
`@anchor test agent-harness/tests/test_prepared_projection_renewal.py :: test_expired_prepared_create_rejects_changed_service_authority`

Remote-authority discovery and local mutation-outbox reservation occur before any Broker transaction can be issued. A failure before successful outbox reservation is therefore a proved no-effect startup attempt: the coordinator must retain the same intent and idempotency key, report it as retryable/no-effect when the dependency is transient, and may retry that phase without entering unknown-effect recovery. Once a durable outbox exists, lost publication/readback remains fail-closed and must recover through that exact outbox/transaction identity. A legacy journal that mislabeled the old pre-outbox remote_unavailable path as unknown may be repaired only when its typed historical error matches that path and the deterministic outbox is absent; unrelated missing-receipt unknowns remain unresolved.

`@anchor test agent-harness/tests/test_canonical_mutation.py :: test_birth_create_remote_preflight_failure_is_explicit_no_effect`
`@anchor test agent-harness/tests/test_scl_start.py :: test_local_start_adapter_preserves_pre_outbox_no_effect`
`@anchor test agent-harness/tests/test_scl_start.py :: test_pre_send_create_failure_retries_same_intent_without_recovery_gate`
`@anchor test agent-harness/tests/test_scl_start.py :: test_legacy_pre_outbox_remote_failure_recovers_same_create_key`
`@anchor test agent-harness/tests/test_scl_start.py :: test_missing_outbox_stays_unknown_without_legacy_pre_outbox_proof`
`@anchor test agent-harness/tests/test_git_broker.py :: test_startup_publication_is_locally_usable_while_peer_replication_is_pending`
`@anchor test agent-harness/tests/test_canonical_mutation.py :: test_birth_create_prepares_only_local_publisher_before_issue`
`@anchor test agent-harness/tests/test_converger.py :: test_manifest_only_authority_read_skips_remote_identity_probe`
`@anchor test agent-harness/tests/test_scl_start.py :: test_startup_repository_checks_only_local_endpoint_identity`
`@anchor test agent-harness/tests/test_canonical_mutation.py :: test_local_startup_service_authority_uses_manifest_without_remote_probe`

### Shared consumer storage and installed interpreter

`HARNESS_WORKTREE_STATE_ROOT` is the same explicit Host storage configuration for
sealed clients and public worktree commands. It selects storage, never authority.
An explicit `--state-root` must match that configuration; absent configuration uses
the existing site default. External-root placement and exact action permissions
remain mandatory. A test-only root monkeypatch cannot prove installed adoption.

Reinit verifies the actual installed hooks using the invoked sealed environment's
interpreter, preserving its stable launcher path. Another Harness found earlier
on PATH must not replace that interpreter. Missing caller identity or a broken
actual consumer is reported as incomplete realization, not successful recovery;
an unrelated PATH installation must not require another human authorization.

An installed consumer check with no staged paths or proposed ref updates MUST
resolve the caller's actual permitted scope within that registered repository;
it MUST NOT demand a repository-wide grant merely to probe a narrower grant.
Every later real mutation is checked against its complete concrete path set.
Ordinary task-branch publication is limited to its registered destination ref;
a task-branch grant cannot authorize a push into a protected destination.

### Publisher continuity

- An explicit current-Host publisher adoption MUST select exactly one existing registered
  repository and prove its own Git identity/base history. It MUST preserve original resources,
  dirty business, checkpoint/history and source identity; it MUST NOT restore a retired
  canonical writer, choose a repository by file order, or require unrelated approvals.
- Fence, selected publisher and any adopted never-sent audit effects MUST commit under one
  kernel CAS. Unknown/leased/sent or non-audit unresolved effects MUST NOT be silently changed
  to a different destination. Exact replay and post-commit projection recovery MUST preserve
  subsequent progress and reject superseded input. The original resident reconciler MUST be
  able to publish original requests using this adopted destination.


### Same-WI workflow profile promotion (WI261)

An active lightweight WI MUST support a formal one-way `promote-workflow-local` operation by
its current owner/executor Host. Startup history remains immutable; the effective workflow
profile controls current planning, continuation, Stats and terminal rules. Conversion MUST NOT
create another WI, draft, startup, execution epoch, worktree, delivery round or measurement account,
and MUST preserve business definition, authorization, results, checkpoint and financial history.

The existing kernel CAS, journal and audit outbox MUST commit one portable transition proof.
Replays retain that proof and subsequent progress. Wrong Host, inactive/frozen WI, pending
replacement, unresolved earlier lightweight terminal request and concurrent frontier changes MUST
not be overridden. Canonical publication status remains separate from local conversion.

The optional state extension MUST preserve historical serialized bytes when absent. Ordinary
entry, reinit and sealed cross-node transfer MUST retain a converted standard profile without
requiring a copy of the source's complete operation ledger. Old baseline imports MUST NOT erase
it. Both newly requested lightweight completion and replay of an old lightweight proof MUST reject
a converted WI. Standard Phase2 remains the existing path, not a new terminal implementation.

Stats MUST distinguish a sealed one-way promotion from a genuinely conflicting startup identity:
old lightweight observations of the same startup cannot reverse promotion; different startup
identities, damaged proofs and contradictory transitions remain unresolved. No record absence is
silently interpreted as lightweight or as a fabricated standard startup.

`@anchor test agent-harness/tests/test_workflow_profile_conversion.py :: test_public_lightweight_to_standard_preserves_same_wi`
`@anchor test agent-harness/tests/test_workflow_profile_conversion.py :: test_promoted_wi_survives_real_independent_kernel_handoff`
`@anchor test agent-harness/tests/test_workflow_profile_conversion.py :: test_human_reinit_preserves_the_same_workflow_transition`
`@anchor test agent-harness/tests/test_workflow_profile_conversion.py :: test_pending_terminal_request_is_not_silently_overridden`
`@anchor test agent-harness/tests/test_workflow_profile_conversion.py :: test_old_startup_proof_cannot_close_a_converted_wi`
`@anchor test agent-harness/tests/test_workflow_profile_conversion.py :: test_stats_transition_overrides_only_the_same_startup`

### Publication clock and kernel-owned candidate proof

A new WorkItem transition MUST use its single `updated_at` value as the corresponding event
timestamp. Historical originals retain their bytes when imported. After admission, timestamps in
WI/event projections do not determine candidate identity and cannot veto the kernel-owned freeze.

The shared candidate reader MUST resolve the committed freeze, the current kernel's producer,
epoch, criteria and candidate meaning, its committed event, and the successful publication
effect with its receipt digest. Pending or unknown delivery does not authorize verification.
The receipt and Git delivery boundaries retain their original validation; the candidate reader
does not reconstruct another authority by comparing every projected field or Git blob.

The six verification entry points share this reader and the existing publisher lock. A changed
kernel frontier, withdrawn/superseded freeze or changed publication effect invalidates an in-flight
read. An unrelated checkpoint preserving the same candidate retains the original candidate proof.
Missing, malformed or older event files are projections to rebuild, not another candidate verdict.
This read-only proof grants no execution or registration authority. Historical candidates lacking
committed business originals require the existing one-time import; they do not acquire invented
publication or Outcome evidence.

`@anchor test agent-harness/tests/test_scl_candidate_source_recovery.py::test_transition_publication_uses_one_clock`
`@anchor test agent-harness/tests/test_scl_candidate_source_recovery.py::test_projection_clocks_cannot_change_frozen_candidate_identity`
`@anchor test agent-harness/tests/test_scl_candidate_source_recovery.py::test_assignment_requires_successful_publication`
`@anchor test agent-harness/tests/test_scl_candidate_source_recovery.py::test_published_skill_handoff_does_not_select_old_main`
`@anchor test agent-harness/tests/test_scl_candidate_source_recovery.py::test_assignment_source_detects_frontier_race`
`@anchor test agent-harness/tests/test_scl_candidate_source_recovery.py::test_assignment_source_proof_is_stable_across_same_candidate_checkpoint`
`@anchor test agent-harness/tests/test_scl_candidate_source_recovery.py::test_published_assignment_ignores_mismatched_projection`
