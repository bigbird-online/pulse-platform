---
feature_name: "pulse-scl-channel-ingress"
version: "0.10.0"
status: "proposed_for_complete_runtime_adoption"
root_work_item: "wi_20260827_282"
created: "2026-08-29"
owner: "Pulse"
external_dependency:
  - "bigbird-online/CollaborateWithAI:Bigbird/Lab/BigbirdEnvironment/agent-harness/specs/harness-scl-resilience-requirements.md"
tags: [requirements, scl, relay, feishu, telegram, multi-node, local-first]
---

# PULSE SCL Channel Ingress — Requirements

## 1. Purpose

This specification owns PULSE Relay's consumption of the canonical local SCL client. It does not
redefine WI lifecycle semantics. Harness owns the local kernel, operation protocol, fencing,
ResumePacket and `scl_response/v1`; Relay owns authenticated channel parsing, host-runtime
observation, one canonical client invocation, prompt dispatch and visible diagnostics.

The current production counterexample is a real Feishu `/wi` on one resident node: Harness rejected
a stale worktree registration with `identity_mismatch`, while Relay exposed only `resume_failed`.
Source inspection also showed that both supported channels share one command handler but that handler
still invokes the legacy release-bundled resume script. A sealed SCL runtime installed on another
node therefore had no effect on this consumer path.

The 2026-08-30 human UAT exposed a second, deeper counterexample. A real active WI imported into
the sealed kernel used a logical resident-surface Session identity that had no durable attachment
to the current Relay channel Session. Relay supplied its physical Session ref, SCL compared the two
opaque values and reported `source_session_active` even though no distinct live source had been
observed. On the same node, sealed entry read epoch 8/checkpoint 30 while the legacy rollover path
read epoch 7/checkpoint 19. A later Codex `/wi rollover` for an existing WI returned
`work_item_not_found` because that WI was present in the legacy ledger but absent from the local
kernel. These are authority/identity/migration defects, not user-command errors.

## 2. Product requirements

### P1. One lifecycle ingress

1. Feishu and Telegram MUST route `/wi` through one channel-neutral SCL ingress after their existing
   authentication and Agent routing.
2. The ingress MUST expose one public entry operation for one user intent. It MAY perform a bounded
   read-only plan/probe to obtain a real target runtime, but MUST make at most one mutating client
   entry call and return exactly one final `scl_response/v1` result to the channel.
3. No active `/wi` path may invoke `Scripts/scl-resume.py`, legacy `harness continue`, an editable
   source checkout or a channel-specific lifecycle implementation.
4. Channel presentation differences MUST NOT change operation identity, authority, degradation,
   recovery, ResumePacket content or result classification.
5. Relay MUST obtain the sealed client's read-only plan before it starts, resumes, kills or rejects
   a host runtime. A persisted but non-live conversation is an observation, not an admission gate.
   When the plan grants no execution authority, Relay MUST return that machine result without
   requiring the dormant runtime, its usage binding or its provider to be available.
6. Every `/wi` lifecycle verb exposed by Relay, including direct entry, continue, checkpoint,
   rollover, reopen, reinit and review preparation, MUST use the same sealed SCL truth and operation
   protocol. A channel command MUST NOT call the legacy `WorkItemSessionController` mutation path
   after entry has moved to SCL.
7. Checkpoint and rollover require semantic frontier content from the current Session. Relay MUST
   first obtain canonical orientation, then dispatch the authenticated intent and ResumePacket to
   that Session Agent to construct the draft, and finally return the canonical mutation envelope.
   A deterministic channel handler MUST NOT fabricate a checkpoint draft from status fields.
8. A local-kernel miss for an existing canonical WI MUST enter the bounded migration/import
   protocol before returning `work_item_not_found`. Relay MUST use only the configured canonical
   source selected by SCL; it MUST NOT scan worktrees, registrations or Git history to choose one.
9. Until one per-WI writer is proven, Relay MUST return `reconciliation_required` with conversation
   and safe local reads preserved. It MUST NOT combine a sealed read from one frontier with a
   legacy write against another frontier.

### P2. Local-first availability

1. Missing network, GitHub permission, remote dirt, hook failure and pending publication MUST NOT
   disable conversation or ordinary local WI work when the local kernel and workspace are readable.
2. A missing or corrupt preferred SCL runtime may select only a manifest-verified compatible LKG.
   If no compatible runtime loads, Relay returns a degraded result and leaves the channel usable; it
   never falls back to the old synchronous remote path.
3. Relay MUST durably record an authenticated continue intent before invoking SCL. A mechanical
   failure preserves that same intent in a single-flight retry queue across process restart and
   runtime repair; the user is not required to repeat `/wi` or copy a request, operation, Session or
   replacement reference. The record contains only the normalized WI/action, authenticated route,
   opaque inbound event identity, bounded attempt/delivery state and the already-sanitized result
   awaiting delivery; it MUST NOT persist raw inbound text or private runtime identity.
4. A real authority or provenance conflict remains fail-closed for unsafe publication or transfer,
   while the response truthfully projects unaffected local capabilities.
5. A stale or unrelated persisted conversation, including a Pi session without a WI usage binding,
   MUST NOT collapse a valid read-only plan into `local_entry_unavailable`. Host activation and exact
   runtime readback are required only after the plan says execution is authorized or a successor is
   required.
6. Automatic retry applies only before a valid final SCL envelope, or when that envelope explicitly
   classifies the same operation as mechanically retryable. A valid authority/provenance/human-intent
   decision is final for that attempt and MUST NOT be polled, reinterpreted or bypassed. Retry uses
   bounded exponential backoff, never runs concurrently for the same intent, and retains a final
   result until its original channel delivery succeeds.
7. A locally readable canonical WI MUST not become unavailable merely because its sealed-kernel
   cache has not yet been hydrated. Import-on-miss is idempotent and restart-safe; if exact source
   parity cannot be proven, the result distinguishes `local_frontier_unavailable` or
   `reconciliation_required` from a genuinely unknown WI.

### P3. Typed and safe diagnostics

1. Relay MUST preserve machine status, stable error code, retryability, capability projection,
   request id and a bounded safe reason from the canonical envelope.
2. Feishu and Telegram MUST show the stable code and whether local continuation, automatic retry or
   human judgment is next. `retry=automatic` is legal only after the durable intent readback proves a
   scheduled retry; a bare `retryable=true` is insufficient. A generic `resume_failed` may be used
   only for an actual unclassified internal failure.
3. Structured logs MUST identify node/surface, channel, Agent, WI, request id and stable code. They
   MUST NOT contain credentials, raw Session/thread identities, private replacement refs or adapter
   stderr.
4. A failed entry boundary MUST NOT launch later-phase verification or write lifecycle evidence as a
   fallback.
5. `source_session_active` is a positive live-observation claim. Relay/SCL MAY emit it only when a
   distinct source or successor has an exact current runtime readback. Opaque identity inequality
   alone MUST return `source_identity_unresolved` and MUST NOT claim that another Session exists or
   is working.
6. The default Feishu/Telegram response MUST explain, in the user's language, what happened, whether
   any lifecycle mutation occurred, whether the durable frontier is safe, and the one available
   next action. Protocol fields and timings are diagnostic details, not the primary response.
7. A human-facing presenter MUST derive every sentence from typed evidence. It may simplify a
   machine result but MUST NOT turn absence, mismatch or unknown into a positive authority fact.

### P4. Co-versioned three-node delivery

1. Each registered resident Relay deployment declares an immutable Relay revision plus the
   compatible sealed SCL manifest. Activation verifies both artifacts and the SCL handshake.
2. Installation, activation, bot health and channel UAT are separate per-node facts. One node cannot
   supply evidence for another.
3. Each node retains a verified LKG and a reversible local activation pointer. Rollback preserves
   local kernel operations, checkpoints and pending effects.
4. Deployment tooling discovers current registered resident nodes from data. Current acceptance
   covers ownermac, cloud-linux-a and cloud-linux-b; those names are deployment inventory, not
   product branches in lifecycle code.
5. Relay's resident reconciler MUST receive the site kernel root, not one process-global Harness
   authority root. Each effect resolves its repository from its own durable WI registration so a
   single process can settle effects spanning multiple repositories.
6. Runtime adoption requires the formal installer to activate and handshake the canonical SCL
   pointer. A stable Harness executable beside a dangling canonical shim is an installation
   failure, and the outer release referenced by a valid canonical active/previous pointer remains
   protected from reclaim.
7. A ownermac launchd reload MUST wait until the prior label is absent from the launchd domain
   before bootstrapping the canonical plist. Rollback MUST restore bytes atomically to that same
   canonical plist path before re-bootstrap; a backup path MUST NOT become a second registration
   source for the same label.
8. Re-adopting a new sealed SCL runtime on the same immutable Relay release MUST remain a normal
   managed transaction. Release reclaim derives the nearest distinct, verified rollback release
   from immutable historical writer receipts when the latest receipt points to the same requested
   release. It MUST NOT require a human to select `--retain-release`, and it MUST NOT delete the
   current, Pi-manifest, live-process or recovered rollback release.
9. Every production policy or cutover manifest that binds Harness MUST pin the exact
   CollaborateWithAI revision adopted by the canonical stable Harness runtime. A PULSE deployment
   MUST fail before source mutation or restart when the apply receipt, stable target, live policy,
   or pinned revision differs. The receipt's authorizing WI is installation provenance and MUST be
   reported, but it MUST NOT be required to equal an unrelated business policy or historical
   cutover WI. When canonical SCL has advanced independently, the stable Harness pin remains valid
   only if a separate `agent_harness_scl_runtime_apply/v1` receipt proves that the stable release and
   live policy were unchanged and exactly attests the active SCL pointer, outer release, executable,
   source revision and artifact manifest. Shipping a new SCL revision therefore includes updating
   and testing these production bindings before the PULSE release is dispatched.
10. The managed Relay writer MUST inject `HARNESS_BIN` and `PULSE_HARNESS_BIN` from the canonical
    stable Harness venv, while an explicitly adopted sealed SCL artifact MUST supply
    `PULSE_SCL_BIN` through its canonical active pointer and `PULSE_SCL_RECONCILER_BIN` from the
    exact outer release attested by that pointer. A retired host-local venv MUST NOT override any
    canonical source. Because systemd `EnvironmentFile=` values can override ordinary
    `Environment=` declarations, the writer MUST make all four binary settings authoritative at
    `ExecStart` and MUST fail activation when the allowlisted environment read back from the Relay
    process differs. Runtime adoption readback MUST also prove that the resident effect reconciler
    is a direct child of Relay and that its executable and interpreter command paths belong to the
    same exact SCL outer release. Unit text, stable symlinks, and a healthy Relay response alone are
    insufficient adoption evidence. A valid SCL-only receipt intentionally allows the stable Harness
    release and exact SCL outer release to differ; the writer MUST NOT collapse these independently
    attested consumers back into one path. When no sealed SCL artifact is being adopted, the existing
    stable-vendored SCL behavior remains the compatibility default.
11. The managed Relay writer MUST own all three production authority settings: `HARNESS_ROOT`,
    `PULSE_HARNESS_AUTHORITY_ROOT`, and `PULSE_HARNESS_CWD`. For each resident site they MUST resolve
    to that site's registered CollaborateWithAI Harness service repository and its `.harness`
    kernel root. A Codex worktree, WI worktree, Relay release, inherited unit value, or missing CWD
    MUST NOT become production authority. The writer MUST replace stale carrier values, make the
    exact triplet authoritative at process launch, and fail post-activation readback unless the
    loaded Relay reports the registered triplet. Current registered repositories are
    `.../collaboratewithai/harness/ownermac`, `.../harness/vultr`, and
    `.../harness/tcloud-node2`; these are deployment inventory and MUST NOT enter lifecycle product
    branching.

### P5. Human-authorized reinit and ready continuation

When canonical recovery and recovery in the original workspace both fail, the Host MUST advance
the same authenticated request to the independent-resource recovery contract in
`harness-scl-resilience-design` section 0.6. Repeating the same failing admission forever is not an
escape. The same legal WI and original native conversation are preferred; new local resources do
not grant remote publication or permission to replay uncertain effects. The original human receipt,
history, business frontier and attribution survive the move. A new consumer must execute through
the actual guarded launcher before business dispatch. New input, Stop, changed WI/session, altered
preservation receipts and unknown dispatch remain explicit fences at every asynchronous boundary.
The runtime workspace move is a durable Host transaction with compare-and-swap readback, not a
manual registration edit or a fabricated user message.

#### Recovery admission and interrupted entry

An authenticated recovery decision MUST be admitted independently of an earlier stuck read-only
entry. Relay verifies the existing Host receipt and current conversation scope before cancelling
anything; malformed, mismatched or withheld decisions do not evict valid work. Cancellation releases
the old caller and fences all its later lifecycle writes, physical runtime changes and local
consumer adoption. A late read/result or old cleanup MUST NOT unlock or overwrite a newer entry.

Already-admitted effects are not reads: recovery waits for their local invocation to settle before
applying the decision. A bounded drain timeout returns `recovery_effect_pending`, retains the original
request/decision, and leaves that resource guarded without blocking other conversations. A timed-out
or unknown external result is not permission to replay it. Existing canonical reinit/readback owns
resolution; the entry gate neither changes WI authority nor performs physical rollback. An
`entry_superseded` result MUST NOT trigger automatic business dispatch or retry of the old intent.

Tests MUST call the real controller, preserve a pre-existing uncommitted artifact, and complete a
new business edit after recovery. Gate-only tests and a reinit success label are insufficient.


1. `/wi reinit <wi>` MUST be treated as an authenticated human root-recovery command. Relay MUST
   deliver that decision to the current Session Agent without first requiring old WI executor,
   binding, attachment, replacement, registration or authority permission.
2. If the reinit draft lacks objective, acceptance, workspace, preserved artifacts or business
   frontier, Relay MUST keep ordinary conversation active while the Session Agent discusses the
   missing semantics with the human. It MUST NOT persist a partial live WI or ask the human for
   private SCL identifiers.
3. Reinit success MUST render only after SCL readback proves a clean active-lineage frontier,
   current Host attachment, fenced authority and successful installed worktree/Git-guard consumer
   probes. `local_committed`, `local_isolated` or a kernel executor field alone is not success.
4. After a successful rollover, one fresh Session `/wi continue` MUST resume the exact durable
   intent, claim/apply the successor privately, deliver the complete checkpoint and return success
   only after the same authority and consumer readbacks. Relay restart reuses the same intent and
   must not ask the user to repeat continue.
5. After a corrective lineage cutover, an old WI with no new-lineage state MUST be presented as
   `reinit_required` with no mutation, a safe preserved workspace and `/wi reinit <wi>` as its only
   lifecycle action. Relay MUST NOT hydrate retired effects or route to a legacy writer.

## 3. Acceptance

Every registered resident node MUST install and read back the shared Feishu/Telegram ingress code,
regardless of which bot routes currently reside there. Visible UAT is required for every active
node/channel route discovered from the post-activation runtime, not for a fictional Cartesian
product. A missing route is recorded as non-resident; deployment MUST NOT duplicate a Telegram
long-poller or move bot ownership merely to manufacture a test cell.

The installed runtime inventory on 2026-08-29 is:

| resident node | Feishu runtime | Telegram runtime | visible UAT cells |
|---|---:|---:|---|
| ownermac | 2 bots | 12 bots | Feishu, Telegram |
| cloud-linux-a | 10 bots | 1 bot | Feishu, Telegram |
| cloud-linux-b | 7 bots | 0 bots | Feishu only |

Each active-route cell needs the exact installed Relay/SCL digests, one visible positive `/wi`
result that reaches the first business action, and one typed negative result that preserves local
work. Process health, unit tests or a source SHA do not substitute for visible channel evidence. If
a non-conflicting Telegram route is later assigned to cloud-linux-b, that route becomes a required
visible cell at its activation boundary.

Before visible UAT resumes, installed engineering gates MUST reproduce both real UAT2 shapes as
data-driven fixtures: an active legacy resident-surface WI whose logical source lacks a Host Session
attachment, and an existing canonical WI missing from a cold local kernel. Both channels must prove
one writer, one frontier, truthful identity classification and canonical checkpoint/rollover
ownership before the user is asked to test again.

The corrective cutover additionally requires copied WI568-, WI282- and WI479-shaped fixtures. Both
channels must show `reinit_required` after cutover, complete a human-draft reinit without old
permission, perform same-session rollover, and complete fresh-session `/wi continue` through
installed worktree/Git-guard readback. Source or installed automation performs this UAT before the
human is asked to exercise a real channel.

Complete runtime adoption additionally requires a deliberately unrelated resident process root,
two registered repositories with one exact WI-local authority root, and an attempt-N pending Git
effect. After dependency restoration the original effect must advance automatically to `succeeded`,
the next sealed entry must project `external_publish=available`, and the canonical active shim must
handshake the exact installed revision on every resident node.

Cross-repository adoption validation MUST NOT assume that a repository-scoped Actions token or an
undeclared SSH key can read the other private repository. PULSE publishes a minimal immutable
snapshot for one exact 40-character PULSE SHA on the registered trusted Linux pool; the Harness
consumer on that same pool verifies the snapshot schema, repository, source SHA, archive digest,
member set and per-file digests before validation. Missing, stale, corrupt or differently routed
snapshots fail closed before adoption tests begin; GitHub-hosted fallback still requires an explicit
read-only cross-repository credential.

`@anchor manual pulse-scl-channel-acceptance :: every resident installs both channel paths; every actual post-activation route supplies visible typed results without duplicate pollers`

Host-native Pi startup uses the same configured execution surface as Relay channel control. Before
any WI is bound, Relay supplies `HARNESS_SURFACE_ID`, `HARNESS_RUNTIME_CLASS` and an opaque
`scl_relay_host_context/v1` to tool subprocesses. The context names the live supplying Relay process;
SCL renews observation only from a matching process ancestry. The Agent does not construct a Host
proof file or use continuation to work around missing first-entry facts. Missing Host configuration
keeps conversation available and is a product configuration gap, not evidence of a damaged WI.


### Conversation startup and replacement

- A successful `/new` or `/reset` clears restrictions belonging to the previous conversation, including its dispatch scope and terminal binding. It does not alter durable WorkItem lifecycle state or bypass an active WorkItem replacement guard.
- All channel adapters use the same startup checks. Persisted v2 usage bindings retain their original identity and remain resumable with explicit unavailable bounded measurement; v3 resumes re-read the immutable baseline before dispatch, while unadopted replacement targets remain non-runnable.
- Provider and model identity must match exactly. Pi may normalize an unsupported requested reasoning level to one of the model's advertised supported levels; Relay records the actual level. A mismatch for a supported requested level remains an error.
- Once a runtime is replaced, events or exits from the old process cannot remove, settle or deliver into the new conversation.
- Startup failures distinguish usage binding, model binding and workspace admission through safe stable codes. Channel text never exposes raw provider output or credentials.


### Ordinary human recovery input

Authenticated text messages carry a Host-owned provenance reference even when no slash command is present.
The Pi lifecycle tool may submit `action=reinit` with that reference after interpreting the real human
scope; quoted text and Agent assertions are not authorization. The Host resolves the same conversation's
record, signs the decision, runs the existing reinit controller after the turn settles, and returns the
machine result to the current Agent. An intervening human message or command withholds stale queued
control and business dispatch. Receipt capture failure preserves conversation but cannot grant recovery.

A failed first startup retains its original WI and request. Correctable startup fields and the original
request are preserved in the machine response and may be returned as `startup_request`; the Host passes
them to the same canonical client. The Agent does not reconstruct a new task, repeat consumed phases,
ask for repeated permission, or report queued intent as execution readiness.
