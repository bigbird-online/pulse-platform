---
feature_name: "pulse-scl-channel-ingress"
version: "0.6.1"
status: "proposed_for_reinit_cutover"
root_work_item: "wi_20260827_282"
created: "2026-08-29"
owner: "Pulse"
dependencies:
  - "specs/pulse-scl-channel-ingress-requirements.md"
  - "bigbird-online/CollaborateWithAI:Bigbird/Lab/BigbirdEnvironment/agent-harness/specs/harness-scl-resilience-design.md"
tags: [design, scl, relay, channels, sealed-runtime]
---

# PULSE SCL Channel Ingress — Design

## 1. Boundary

```text
Feishu authorization/parser  --\
                                > SclChannelIngress -> SealedSclClient -> scl_response/v1
Telegram authorization/parser --/           |                  |
                                            |                  +-> local SCL kernel/LKG
                                            +-> host dispatch plan -> exact Agent runtime
```

Channel code owns transport only. `SclChannelIngress` owns command normalization, durable decision
delivery and result rendering for every lifecycle verb. `SealedSclClient` owns process execution
and schema validation. Harness owns lifecycle state and machine-transition admission; the human or
contextual Session Agent owns semantic decisions. The legacy Relay controller is not a second
writer or fallback lifecycle state machine.

## 2. Client invocation

The configured runtime is an immutable `harness-scl` executable plus expected manifest digest. Relay
performs a cached handshake on activation and after executable identity changes. A command runs with
explicit local roots, surface identity and an opaque host Session ref; it never derives authority
from the channel name or hostname.

The client result maps into:

```ts
type SclChannelResult = {
  schema: 'pulse_scl_channel_result/v1';
  requestId: string;
  status: string;
  operationId?: string;
  workItemId?: string;
  code?: string;
  safeReason?: string;
  retryable: boolean;
  capabilities: CapabilityView[];
  resume?: SclLocalResumePacket;
};
```

Private Session, replacement and raw diagnostic fields are not part of this type.

### 2.1 Command ownership

Direct entry is mechanically resolvable from a Host observation. Checkpoint and rollover are not:
their drafts contain decisions and the business frontier known by the current Session Agent. The
shared ingress therefore uses this sequence for those commands:

```text
authenticated command -> sealed orientation -> current Session Agent builds semantic draft
                      -> sealed checkpoint/rollover-local -> one final channel result
```

If orientation does not grant source authority or does not return a readable frontier, the sequence
stops with that typed result. No direct handler calls the legacy controller and no Agent supplies
private continuation, replacement, operation or digest parameters.

### 2.2 Logical source attachment

`session_ref` in a legacy resident-surface binding may be a Harness-generated logical token rather
than a digest of the current provider Session. Import preserves that logical source and records an
optional `scl_source_attachment/v1` that joins one WI/epoch/source ref to one Host-derived opaque
Session ref, surface, runtime class and readback state. Raw provider identity is never stored.

For a current-format entry, the attachment is created with the source operation. For an imported
active binding without mechanically joinable Host evidence, entry returns
`source_identity_unresolved`. The authenticated human or contextual Session Agent may assert that
the current Host Session is the existing logical source and provide a rationale. The kernel checks
the exact WI, owner/executor, epoch, source, surface/runtime, replacement state and frontier digest,
then records the attachment without advancing the epoch, withdrawing a candidate or transferring
authority. This `attach_source` decision is distinct from `reopen` and replacement.

Only an attachment readback or a distinct live runtime observation may support
`source_session_active`. Bare opaque-ref inequality supports no positive liveness statement.

### 2.3 Reinit discussion and apply

`/wi reinit <wi>` bypasses ordinary lifecycle permission planning because it exists to replace an
unusable control plane. Relay validates only channel authentication and command grammar, then
dispatches a reinit-orientation packet to the current Session Agent. The Agent may use retired
history and current workspace facts as evidence, discusses missing semantics with the human, and
submits one complete `reinit_draft/v1` to sealed SCL.

No partial draft becomes a live WI. After apply, Relay retains the intent in `result_ready` until
SCL's composite readback reports a clean epoch, current Host attachment, fenced authority,
worktree validation and Git-guard readiness. A pending result is retried automatically and is never
rendered as reinit success.

### 2.4 Channel aliases and Harness principals

An operator-provisioned `AgentConfig.harnessAgentId` explicitly maps a channel alias to its
canonical Harness principal. For example, `{ "id": "tcloud-pulse", "harnessAgentId": "pulse" }`
keeps the channel route and pool key unchanged while sealed `/wi` requests, launch validation,
usage bindings and pending-entry recovery consistently use `pulse`. Without this field, the
principal remains `AgentConfig.id`; no prefix stripping or inference from the requested WI owner
is permitted. Invalid explicit principals fail configuration validation and request construction.

This is trusted node configuration, not a chat-controlled identity override. It grants no WI
execution authority: SCL still validates the exact WI, principal, Session and workspace. Existing
pending usage bindings must match the configured principal; changing the mapping cannot silently
adopt another principal's persisted entry. Physical Session IDs, opaque Host identity, credentials
and channel authorization are not rewritten. This contract covers sealed WI ingress, not the
legacy standalone continuation adapter.

## 3. Host dispatch and recovery

After channel authentication and normalization, Relay first writes a `pending_scl_intent/v1` record
with a strict flush. Its identity is derived from the channel's inbound event identity plus the
resident bot route; it does not hash or retain the message body. The record state is
`recognized -> attempting -> retry_wait|result_ready -> delivered`. Duplicate channel delivery or
restart reuses the same record, and a per-intent lease prevents concurrent attempts.

The ingress records the current host observation and then requests a read-only local plan before any
runtime activation. A `resumable` persisted conversation is not started merely to obtain that plan.
If the plan grants no execution authority, including positively proven `source_session_active`,
`source_identity_unresolved` or `observe_candidate`, the plan is the final lifecycle result and
Relay performs no runtime mutation.
If the plan grants current-session execution, Relay then ensures that exact runtime is live and
reads it back. If a successor is required, Relay persists a target plan, starts the exact target
without a business turn, reads back its runtime/session identity and performs one mutating entry
with that observation. Only the mutating call may claim or apply replacement.

Relay then persists a dispatch record before sending the final ResumePacket to the selected Agent
runtime. The record binds WI, SCL request/operation, opaque host Session, packet digest and dispatch
state. Restart recovery reads this record and either confirms the same accepted dispatch or retries
that exact dispatch. It does not invoke lifecycle mutation a second time.

If client execution fails before a valid envelope, Relay tries the verified compatible LKG once. If
that also fails, it returns `degraded` with conversation/local-work projection and moves the already
durable intent to `retry_wait`. A bounded backoff worker and restart recovery retry the same intent;
once a result is final, Relay retains `result_ready` until the original Feishu or Telegram route
acknowledges delivery. If a valid envelope rejects a lifecycle action, Relay does not reinterpret or
bypass it unless that exact envelope explicitly declares the same operation mechanically retryable.

The presentation layer receives both `retryable` and `retry_scheduled`. It may render
`retry=automatic` only for `retry_scheduled=true`; otherwise it reports `human`, `none` or `unknown`
truthfully. This separates protocol retryability from an actual running recovery mechanism.

Before planning returns `work_item_not_found`, the sealed client performs one bounded
`hydrate_frontier` attempt from its configured canonical local source. Import digests the exact WI,
binding, checkpoint, continuation/replacement and writer-authority bytes in one transaction. An
exact replay is a no-op. Parity failure leaves the source untouched and returns
`reconciliation_required`; it never activates the legacy writer beside the kernel writer.

After a release-level lineage cutover, retired-lineage membership takes precedence over legacy
hydrate. A known old WI returns `reinit_required`. Once reinitialized, rollover stores the Session
Agent's complete semantic frontier, and fresh-session `/wi continue` uses the durable channel intent
to claim, attach, apply and read back the successor exactly once.

## 4. Diagnostics

The shared presenter renders a human result first:

```text
没有执行 rollover：当前节点还不能把这个 WI 的逻辑 source 与本会话可靠对应。
已有进度未被修改。修复关联后可继续，不需要 reopen 或重复命令。
```

An optional diagnostic detail contains stable code, status, capabilities, retry and timing. The
presenter uses a closed semantic mapping for mutation fact, frontier safety and next capability; it
does not interpolate `next_action` as proof or translate an identity mismatch into another live
Session. The structured event stores only allowlisted fields. Unexpected exceptions are logged
with an internal event id, not raw child-process output.

## 5. Release topology

The release manifest pairs PULSE source/release digest with SCL source/wheel/manifest digest and
minimum protocol version. Per-node deployment stages both artifacts, verifies them, activates local
pointers atomically, restarts only when the node is safe, and reads back process executable plus SCL
handshake. A failed node remains on LKG and the aggregate rollout is `partial`.

`@anchor manual pulse-scl-channel-design :: one shared ingress for all lifecycle verbs, one writer/frontier, durable logical-source attachment, bounded import-on-miss, truthful human presentation, compatible LKG and per-node activation readback`
