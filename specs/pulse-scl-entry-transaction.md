---
version: 1.2.1
status: implementation_candidate
owner: pulse
work_item: wi_20260827_282
upstream_contract: Bigbird/Lab/BigbirdEnvironment/agent-harness/specs/harness-scl-resilience-requirements.md
---

# Relay entry transaction

This contract governs the local-first Relay path. Legacy bound-entry adapters remain
explicitly separate. The SCL kernel owns lifecycle authority; Relay owns process
placement, its conversation projection and business delivery. These boundaries must
not be collapsed into one success or failure flag.

## Correction and authorization contract

This entry adopts the business-first contract reviewed at CollaborateWithAI PR377,
4c60763c6a55f42279bd79f4812194393448454c. Lifecycle authority, actual resource access,
business delivery, measurement and publication remain separate facts.

A rejected/degraded response MUST preserve committed operation references, failure stage,
recovery information and any freshly proven local execution. Parsing status cannot discard
those fields. Failed measurement cannot stop an otherwise admitted business turn. Resume
without fresh authority cannot invent it from a saved receipt. Unknown business delivery
remains unknown and is not resent merely because lifecycle recovery succeeded.

Ordinary authorized correction and explicit in-scope delegation require no extra human
approval. The receiver must receive usable rights, not only an accepted message. A normal
conversation can request new authorization before legacy binding, full reinit draft or
kernel checks. Such a request grants nothing; one genuine decision is applied in minimum
scope without asking the superseded owner again. Hook/command parser failure must preserve
the original user message so this conversation remains reachable.

Acceptance is the installed B01/B02/B03 business path in the upstream resilience task
specification, including original dirty artifacts, actual receiver writes, stopped old
writers, once-only effects and no extra human approval for ordinary correction.

## State and commit boundaries

1. Planning reserves one durable Host target identity. It does not replace the active
   conversation tuple or grant business authority.
2. Before planning, the sealed client resolves one exact `WI + owner + Host surface +
   repository` registration through `resolve-managed-workspace-local`. The static Agent
   workspace may identify the repository, but is never an admission or launch path.
   Missing, duplicate, changed, non-realpath or wrong-surface resolution fails closed.
3. Canonical read-only workspace admission proves WI, owner, Host Session, epoch,
   registered surface, manifest digest and the resolver-returned worktree. The exact
   returned worktree is the launch path; no routing or default directory may replace it.
4. Target preparation persists candidate material and starts an admitted process while
   preserving the complete source tuple. A live candidate is not yet the conversation.
5. Canonical continue owns claim, target readiness and exact Host runtime/workspace readback, then apply. The replacement v3 `target_readiness_sha256` is a control digest; legacy v1/v2 first-settled fields remain legacy readiness evidence, never runtime result receipts.
   A response proving local execution allows Relay to adopt the target Session, runtime,
   workspace and usage binding in one strict durable write, then update its route.
6. Business dispatch and the first business result follow adoption. They cannot grant
   or revoke lifecycle authority. Failure after apply preserves the committed fact.

`pendingWorkItemEntry.entryProtocol=local_scl` distinguishes this path from the legacy
runtime-launch adapter, even when both retain a workspace manifest digest. No default
Agent directory may fill a missing target workspace. Re-admission can replace a stale
legacy routing path only with the canonical returned worktree.

`@anchor test Relay/tests/scl-entry-transaction.test.ts :: preserves the complete source tuple until canonical apply despite a live target`
`@anchor test Relay/tests/scl-entry-transaction.test.ts :: commits the complete target tuple after apply without waiting for a model result`

## Recovery and authorization

One pool may have only one active lifecycle entry. Restart recovery and a channel retry
must not concurrently prepare or apply it. Checkpoint, reopen and sealed rollover use the same entry gate as continue. A recovery is itself a cancellable entry: newer authenticated decisions may abandon blocked reads, but every late continuation is fenced before writes or result return. Recovery authorization is tied to a durable
intent for the exact pool and WI; permission to continue a different WI is insufficient.

The durable channel-intent coordinator is the sole automatic continuation owner, including
after restart. Startup entry scanning skips any intent-owned request and may only reconcile
already-applied orphan targets without dispatching business. A transient entry-gate conflict
retains the same durable request and authorization; it is not a terminal delivered result.
Only a completed result, a deterministic failure, or an actual superseding user decision may
settle that request. Gate contention must never be interpreted as scope withdrawal.
Automatic recovery retains its per-intent single-flight guard through result delivery. Overlapping
worker ticks reread the durable intent; removed results are neither attempted nor sent again.

The latest channel input boundary is passed through recovery as well as ordinary entry. An older signed decision cannot cancel an admitted newer recovery. The latest user scope is re-read before lifecycle mutation, Host dispatch, source stop,
target spawn and business send, including after asynchronous admission or source sealing.
An explicit continuation that first observes an unbound independent target cannot orient or
bootstrap it. Relay delegates descriptor creation, source seal, content-digested mode-0600
transport and target import to one Host-owned dispatcher, accepts only an exact typed import
readback, re-resolves the unchanged target registration, and only then plans target claim.
Missing dispatcher, transport failure, import mismatch, admission drift or v2 process-readback
failure prevents target business dispatch. A stopped unapplied continuation stays parked. A
stopped already-applied target may be read back and its local projection repaired without
spawning, claiming, applying or sending work. That repair must also re-admit the exact workspace.
It does not resume the stopped work.

Ordinary text and media must verify that any live WI runtime belongs to the adopted
conversation tuple before changing the persisted Session or sending a message. Finding
a live prepared target cannot bypass execution admission. An unrelated pending WI must
not replace the active binding or inherit another WI's recovery permission.

`@anchor test Relay/tests/scl-entry-transaction.test.ts :: reads stopped pending authority without advancing lifecycle (applied=%s)`
`@anchor test Relay/tests/scl-entry-transaction.test.ts :: ordinary chat cannot bypass admission by finding an already live prepared target`
`@anchor test Relay/tests/scl-entry-transaction.test.ts :: replaces a static Agent workspace with the exact registered worktree before plan, admission, and spawn`
`@anchor test Relay/tests/scl-entry-transaction.test.ts :: never bootstraps an explicit continuation on an unbound independent target`
`@anchor test Relay/tests/scl-entry-transaction.test.ts :: recovers the same independent-kernel handoff without business dispatch after a transport failure`

## Business delivery and uncertainty

The local-first pending entry records business dispatch separately as `not_started`,
`sending`, `accepted` or `unknown`. It persists `sending` before crossing the transport
boundary and `accepted` after acknowledgement. A successful first result retires the
pending entry while retaining the active tuple and `pulse_work_item_business_result/v1` receipt. The receipt is created only from a successful runtime result with the exact Host operation key, Session and epoch. It contains the result digest and observation time; it proves runtime settlement, not business-value acceptance. A control response, uncorrelated event, legacy hash or send acknowledgement cannot create it.

An in-memory request ID is not proof of deduplication across restart. Lost send/result
acknowledgements retain unknown delivery and cannot automatically replay the business
prompt. A first-result failure must not downgrade already-applied lifecycle authority.
Current unknown-delivery recovery does not claim transcript-based automatic settlement;
it preserves a distinct diagnostic until exact result evidence or a new user decision
is available. New ordinary messages are distinct requests, not a replay authorization.

## Bounded recovery and rollover

An admitted local SCL command owns one process group. Authenticated recovery may
abort that exact group, including its local child writers, and waits for process
close before admitting a conflicting local command. An AbortError, SIGKILL request,
transport rejection or timeout never proves an external effect did not commit.
Recovery resumes the original durable request and reads back its outcome. Immediate
duplicates share the existing operation boundary; technical recovery retries reuse
the same human decision and do not request approval again. Unresolved effects retain
the guard for their dependent actions; other conversations remain available.

The legacy continuation coordinator uses one decision for foreground failure and
restart: exact committed target ownership completes forward; an exact unclaimed or
owned claimed continuation plus explicit noncommit permits compensation; unavailable, changed or conflicting
readback preserves the original operation with no kill, resume or release. A merely ready/claimed snapshot after a transport failure cannot rule out a still-running remote commit. Old consuming records without explicit noncommit evidence remain unknown. Recovery
records each physical compensation before and after sending it. An acknowledgement
loss never causes that physical action to be blindly repeated. Failure to prove a
target stopped must never start another writer. `sending` is a request to observe,
not a terminal waiting condition: every restart and same-command recovery retry
reads both the exact native target/source and the original physical invocation.
A recovery attempt records its stable action reference, ordinal, opaque Session
identity, runtime, issuer PID/start identity, and observed native child identities
before invoking the effect. The OS query is read-only and bounded; command lines
stay in memory, never in receipts/logs. Missing process-map entries or persisted
`idle` routing do not prove process absence or source readiness.

Already-stopped targets and exact live/routed sources are adopted without another
kill/spawn. A new attempt requires both physical noncompletion and a quiescent prior
issuer; unavailable/partial queries, another live issuer, starting/unowned native
processes or conflicting pool identities retain the same frontier for later readback.
Legacy `sending` without issuer evidence is observation-only: a proven completed
action may be adopted, but unproven nonexecution is not silently retried. Captured
child identities remain protected until they actually exit, even if a kill callback
has returned. Retry evidence unions prior identities with newly observed children;
reparenting cannot remove a still-live descendant from the recovery obligation.
Both native ownership and persisted/channel routing must remain within the exact
source/target pair, even when the post-restart runtime map is empty. Neither kill nor resume may evict a different Session. A lost release
acknowledgement preserves recovery material until canonical unclaimed readback.
This is a single-Relay native-process protocol, not distributed or OS sandbox authority.

The sealed channel `/wi rollover` follows preparation immediately with the existing
canonical continue/adoption and business dispatch consumer for the same WI. Preparation
remains an intermediate committed fact on failure. Users do not copy internal refs or
manually kill/reset to complete the transition. Acceptance includes two consecutive
rollovers with distinct physical Sessions, retained source output, and separately
correlated post-authority business results.

## Failure and compatibility

Control failures retain their stage, stable code, retryability, request correlation and
observed mutation. Crossing the Host dispatcher boundary is conservatively a possible
mutation because source seal can commit before a transport acknowledgement is lost; an exact
retry reuses the Host-owned durable operation and still cannot spawn or dispatch until import
and readback succeed. Runtime activation retains the underlying diagnostic in the operator
log. Public startup messages expose stable codes without raw provider output or private
paths, and say when the current message was not dispatched. They do not advertise an
automatic retry unless a durable worker actually owns it.

Older prepared entries are read through their existing identity and pending usage
binding for control recovery. They are never treated as active merely because their
Session was prematurely written into the conversation. Legacy dispatching entries with
no separate delivery field retain unknown delivery rather than being blindly replayed.

Rollout requires matched canonical workspace-admission support and independently
verified loaded Relay/SCL artifacts. Source tests, CI, service health, RPC and mocked
channel tests do not prove a visible Telegram or Feishu user round trip. Independent
Outcome, human review and terminal settlement remain separate gates.

## Last recovery and return to canonical authority

When canonical readers are unusable, the Host can interpret an explicit
`/wi reinit WI --isolated --active-work` or `--withdraw-candidate` decision.
The human supplies the business disposition; the Host derives the exact current
Session, surface and linked Git workspace identities. It does not infer active
lifecycle status from a stale runtime binding. Nested scope is signed with recursive
canonical JSON and retained under the original authenticated input identity.
No unrelated checkpoint draft is required. A changed Session, newer human boundary,
forged scope or stale decision cannot supersede a currently admitted recovery.

The isolated response is a distinct contract with per-resource availability and
explicitly unproven canonical frontier, global exclusivity and publication. It must
not fabricate a ResumePacket, epoch or usage binding. Available resources are consumed
through the existing Harness worktree launch guard using the retained exact signed
receipt. Busy resources retain their pending state. This protocol is not an OS sandbox.
A normal readable-kernel reinit continues to realize canonical authority. For an
original isolated Host input, `startupRequest.rejoin_canonical=true` retries that same
signed decision through normal reinit and real runtime binding adoption; it does not
mint a new human approval or expand scope.

When canonical reinit already committed and the original business prompt was dispatched
through isolation, the Host recovery worker instead reattaches that same consumer from
a fresh canonical plan. It does not replay reinit or the business prompt. Existing v1
recovery records discover this unfinished attachment from their retained committed and
isolated results. Attempts retain a durable identity and bounded backoff. The exact
pool, native Session, WI, workspace, human-input boundary and attempt ownership must
still match after each asynchronous step; a busy conversation is left running.
For this already-dispatched consumer maintenance, the boundary is captured from the
latest durable ingress for each attempt. A later ordinary business question does not
revoke an already committed grant. The original accepted proof is checked by official
workspace admission; it is never restored as the current human input. A new ordinary
input during an attempt postpones that attempt. Explicit Stop or session/lifecycle
controls permanently fence the old attachment, including after later ordinary input.
Undispatched recovery and business prompts retain the strict original-latest boundary.
An accepted isolated consumer is also durable without a canonical reinit commit. If
idle reclamation or a Host restart removes its child and ordinary startup fails, a
new foreground message may restore that exact consumer from its dispatched recovery
record. Official isolated restoration/admission validates the original current proof
and resource locks independently of the unavailable kernel. This path neither repeats
reinit nor replays the old prompt, and never replaces a live process. The foreground
ingress and pre-start Session/binding remain fixed across asynchronous startup failure;
Stop, pending lifecycle entry and terminal projections prevent old-consumer revival.

Attachment becomes ready only after the normal process and its usage baseline have
been independently read back. A process that spawned but cannot activate its baseline
is not ready. Failure restores the accepted isolated consumer while its original scope
remains current; it must not overwrite a new Session, new input or Stop. Temporary
configuration, transport or receipt-read failures remain retryable, rather than being
misreported as withdrawn human authorization.

Normal and isolated execution must also agree at the Harness resource guard. A Host
that stopped its exact failed normal consumer restores the original isolated admission
through `restore-isolated-workspace-local`, not by editing receipts. The original signed
decision, same resource and adoption identity are checked under the shared resource
locks; a live writer or superseding decision rejects compensation. A subsequent normal
admission may present that same accepted recovery decision to re-establish adoption of
the current canonical grant. This supports both absent and withdrawn adoption receipts,
without reinit, a new grant or an epoch change. A failed normal admission leaves isolation
usable, and a successful one must still pass the actual runtime and baseline checks.

For one unchanged execution interval, reinit reuses the existing v3 usage binding. If
a prior consumer already replaced it, an explicit canonical baseline reference joins
the valid current binding to the immutable original measurement origin. This is not a
reconstructed historical binding. Its exact tuple, open canonical baseline, private
artifact and native cumulative boundary must agree; no new baseline, reopened seal or
rewritten usage history is permitted. See the [Pi runtime contract](../Relay/specs/pi-conversation-runtime.md).

A failed usage producer records measurement as unavailable after independently
validated runtime identity and authority. It does not revoke otherwise available local
business capability or manufacture a zero baseline. A mismatched authority or usage
identity still rejects dispatch. Result evidence requires the exact Host operation,
Session and epoch; a distinct durable operation cannot be steered into another active
Pi turn. Channel error and retry presentations retain committed machine facts.

For an explicitly requested original primary/protected work area, the human may add
`--preserve-local`. Only then does Host sign `allow_primary_local_progress=true`;
actions remain read/write/test, with no commit or publication rights. If the existing
current Host decision record is damaged, a new actual human input may attest its exact
byte digest via `supersedes_unreadable_decision_sha256`. Harness preserves damaged bytes
before updating its disposable pointer. Technical retries retrieve the original signed
receipt before deriving new scope, so repair of that pointer cannot accidentally mint
or invalidate the original consent. Missing or unwritable required storage still blocks
the dependent action honestly; it is not converted into WI-wide authority denial.

### Host decision reference wire v1

`human_decision_v1_` identifies SHA-256 of compact ASCII-escaped JSON for the ordered
string tuple `[channel, event_id, human_ref, work_item_id]`. Every non-ASCII UTF-16
code unit and DEL is escaped as lowercase `\uXXXX`; non-BMP characters use a
surrogate pair. Standard JSON control/quote escapes apply; no Unicode normalization
is performed. This preserves existing Python-generated IDs exactly. The UTF-8 HMAC
payload and existing Host input receipt filenames remain separate and unchanged.
Consumers MUST reuse original signed inputs and accepted references on technical
retry; they MUST NOT re-sign, rename historical decisions, or ask for new human
approval to repair a reference serializer mismatch. JSON input is parsed as JSON;
legacy YAML input remains supported. Unicode and historical-input replay tests
must pass across the actual JavaScript issuer, Python consumer and Relay readback.

### Host workspace routing and initial rollover persistence

The validated Host `HARNESS_WORKTREE_STATE_ROOT` must reach both the native Pi
startup admission subprocess and its ordinary child tools. Validate it before
admission; passing it only to the not-yet-started Pi cannot satisfy admission.
The explicit CLI root and the child environment root must agree.

The first durable rollover record includes all target identities before claim.
For legacy validated records without any target or physical-action identity,
canonical unclaimed readback permits completing the local target identity and
observing/restoring the source through the existing recovery path. Unknown
consume outcomes, partial identities and other claims remain fenced.


## WI843 request repository and post-reinit progress correction

A shared Relay client MUST derive the business repository for each request from its
Host routing workspace, including a subdirectory of the checkout. The process-wide
`PULSE_SCL_REPOSITORY_SLUG` is not a business-repository selector. The existing remote
resolver must reject missing, conflicting or partly unparseable origin URLs rather
than dropping invalid URLs or falling back to a global repository. This is routing,
not authority: exact canonical registration and runtime admission remain mandatory.

When canonical reinit has returned and workspace resolution, admission or Host adoption
fails, preserve the prior reinit receipt independently of the failing machine envelope.
The channel must retain the failure stage and explicit mutation evidence; an execution
grant alone must not manufacture a new mutation. A post-commit `work_item_not_found` or
`reinit_required` error must not trigger semantic reconstruction or a second reinit.

The continuation after such a failure uses the existing `continue` path to read the
current canonical frontier and re-admit the exact workspace. It needs no receipt cache
or new state store, and it never obtains execution or publication rights from the old
receipt. Newly frozen/revoked authority remains denied. A prior canonical commit is not
an attached runtime or a successful user turn; no automatic retry is advertised without
an actual worker, and the channel must not schedule a fresh reinit for this condition.

`@anchor test Relay/tests/scl-repository-resolution.test.ts :: the real controller constructor and child process ignore the obsolete global business-repository pin`
`@anchor test Relay/tests/work-item-recovery-escape.test.ts :: a fresh controller continues from canonical readback without repeating reinit`
`@anchor test Relay/tests/work-item-recovery-escape.test.ts :: public /wi reinit preserves progress and never advertises success or an automatic new reinit`
