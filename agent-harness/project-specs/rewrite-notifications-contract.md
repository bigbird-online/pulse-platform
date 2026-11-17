# Durable notifications and unresolved attention

AH-061 / A-29 require immediate delivery, daily aggregation, failed/open queries,
same-request recovery and explicit resolution. Notification delivery is an
external effect of the same journal; it is not another WI authority. Existing
`harness/escalation.py` and `harness/scl_maintenance.py` are capability references,
not dependencies or an alternative notification ledger for the replacement.

## Original facts and routing

Record a notice through execute with an authenticated caller, original request
identity, bounded content/artifact references and an installed logical route.
Retain the original notice, its source revision, current WI/task/candidate or
collaboration reference, purpose and intended audience. A notice cannot authorize
work, acknowledge a collaboration, approve a candidate or create a human decision.
The recorder must have current authority for that WI and resource scope.

The privileged route selects the channel, actual existing recipient and agent,
immediate/aggregate behavior, aggregation timezone/window and transport bindings.
The business request cannot provide URLs, credentials, executable commands,
arbitrary recipient identifiers or policy overrides. Missing/bad configuration
fails the dependent send and remains queryable; it never widens recipients.
Policy inspect/diff and formal installation review the concrete route change.
Migration must preserve the original recipient/binding; a destination cannot
retarget an outstanding notice silently.

Owner-facing attention needs an actual unresolved human decision or a confirmed
explicit decision request, independently of its severity or prose. Derive a
fingerprint from the current canonical object and decision scope, and recheck it
immediately before send. Resolution, ownership/scope transfer, cross-tenant
restrictions or an obsolete candidate suppress stale attention. Mechanical
failures and unknown activity do not manufacture a need for human authorization.
Do not bypass the existing Relay owner-attention gate by choosing another source
label. A user-visible operational notice without a decision request must be
explicitly allowed by its installed route and keep that distinction.

## Aggregation, attempts and resolution

An immediate notice and its single delivery intention commit atomically. An
aggregate notice remains pending until an explicit bounded flush captures the
eligible unresolved set for that route and original local-date window. Preserve
the exact membership, order, content and window as one immutable batch.
Late retry keeps that batch; a new notice does not alter already attempted bytes.
Backlog processing keeps each original window and does not relabel it as today.
Two flushes cannot assign one notice to multiple live batches. Bound count and
channel size before dispatch; splitting produces explicitly recorded disjoint
batches, never silent truncation. A prior delivered batch is not sent again
merely because its view or acknowledgement cache is missing.

Use the existing original effect/attempt protocol: durable intent precedes I/O,
unknown/in-flight results require readback, and only proven absence permits
another apply. A query, queued batch, successful child process, HTTP status or
Relay `ok` flag alone cannot prove delivery. Retain the actual channel/recipient,
external message ID, accepted content identity and transport observation; expose
only authorized public fields. Partial multi-channel outcomes are separate legs,
so recovery cannot resend an already delivered leg.

Resolving a notice is an explicit journal decision with reason/provenance.
It ends its unresolved-attention obligation and can suppress an unstarted send.
It does not change an uncertain send into absence, erase prior attempts,
fabricate a channel receipt, resolve another notice or finish the WI. A user
reply can support resolution only through an authenticated original input.

## Transport and evidence

Keep channel transport behind one narrow adapter. The existing Relay
`POST /internal/push` returns per-channel receipts with externalMessageId;
its ordinary requests use fresh delivery IDs and are not an idempotent readback
API. Do not invent a scheduler runId/terminal reservation to borrow scheduler
deduplication. An acknowledged receipt may be retained atomically as local
evidence; a crash after external send but before that retention remains unknown
unless supported original-channel evidence can recover the exact message.
Never infer absence from a lost receipt file or log. A missing readback
capability is reported explicitly, not replaced by another send.

Secrets stay in the original managed transport binding and are neither copied
into a notice nor exposed in arguments, exceptions or public receipts. Preserve
configured proxy/TLS behavior. Existing Relay/Telegram/Feishu processes and
conversations are consumers; notification integration does not reset them.

## Queries and acceptance

Provide authorized open, failed, unresolved-send, aggregate-backlog and resolved
queries from original facts, with bounded pagination and exact revisions.
Delivery status and issue resolution are separate fields. Views are disposable;
deleting/corrupting a view cannot resend or resolve anything.

Source acceptance covers strict route/input checks, cross-scope/privacy denials,
simultaneous immediate declarations and aggregate flushes, timezone/day rollover,
late backlog, byte/size boundaries, exact retries/conflicts, all send crash
boundaries, actual receipt validation, partial legs, stale-attention suppression,
resolution during a pending or unknown send, and view rebuild without new facts.
Fake transports establish these source semantics only.

Native acceptance requires original-channel immediate and aggregated messages,
actual per-channel IDs/content readback, preserved producer identity, an observed
failed/unknown branch without duplicate send, same-request recovery where absence
is proven, explicit resolution and the real query consumer. No production route
or existing schedule changes until the exact installed policy and target are
reviewed. Full AH-061/A-29 remain open until those observations exist.

Status: kernel semantics, native route loading and the single-attempt Relay
transport are implemented and source-tested. Candidate22 is installed in the isolated Muse target with the exact notice-only
server and the original native producer has adopted it. Its first attempted
notification remains unknown. Candidate23 is now formally installed with the
unchanged policy; the original native producer's five revision-6 status filters
return the correct pages. Three independently authorized messages have now
arrived in the original Telegram conversation: immediate, two-item aggregate
and explicit human attention. Their channel IDs, exact retries, filtered pages,
independent resolution and idle unknown readback were verified. A separate F-A
notice has now received a real product HTTP404/sendAttempted=false rejection,
then recovered along the same effect after restoring the admitted sender. Only
attempt2 sent the original text (Telegram message886); exact record retry and
idle resume added no attempts. Original M01 remains unknown with one attempt.
See the [native absence/recovery evidence](evidence/rewrite-notifications-absence-recovery-20261009.json).
The seven-condition experiment report/closeout, Feishu and the complete
AH-061/A-29 gate remain open.
The candidate21 SDK journey is a separate slice.


## Executable input contract

The kernel provides record_notice, flush_notices and resolve_notice, plus
inspect --view notices. A trusted Host supplies NotificationRoute values; a
business command selects only route_id. Native policy installation admits the
actual channel, recipient and transport. Missing routes cannot send. Source
implementation alone is not AH-061 acceptance.

record_notice accepts exactly route_id, resources, text, purpose and attention.
purpose is operational, candidate_review or explicit_decision. Operational
routes reject attention. Owner-attention routes require either the current
verified candidate waiting for review or an authenticated exact explicit human
decision request; free prose or severity cannot create that authority. Store
the original principal, generation, definition, candidate, source digest and
journal revision. The admitted clock determines the original local-date window.

flush_notices accepts exactly route_id and window (YYYY-MM-DD), capturing only
that original window, including backlog. A bounded immutable batch fixes ordered
members and exact text; disjoint batches respect installed count/UTF-8 limits.
Each channel leg is a separate route/effect, with no implicit both-channel send.
resolve_notice accepts exactly notice_id and reason with an authenticated source.
Delivery and resolution remain independent. Stale unstarted batches are cancelled;
their still-current unresolved members can join a later new batch. Unknown sends
cannot be cancelled or reset by resolution.

An adapter success must include route binding digest, exact content SHA-256,
channel, recipient digest and a nonempty real external message ID. Ordinary
HTTP/Relay success without these fields cannot settle the effect. Missing durable
receipts remain unknown. Source tests use explicit trusted test Hosts/adapters;
they do not establish native identity or actual channel delivery.

## Native query and independent-effect progress

The installed CLI must pass the requested notice status and pagination to the
kernel query, including historical queries. A filter that yields no matching
notice returns an empty page; it must not silently return every notice. Native
acceptance compares open, failed, unresolved-send, resolved and backlog pages
against canonical notice facts, not merely a successful process exit.

An unresolved optional notification retains its original send attempt and
readback obligation. It must not monopolize each bounded drain while a separate
authorized effect is ready. Defer readback of ambiguous optional notifications
until other eligible effects have had the available batch budget; preserve
their relative order and all dependency/authority checks. A later independent
notice requires its own actual intention and different content. It cannot
replace, reset or resend the ambiguous notice. When no other effect is ready,
resume still reads back the original ambiguous attempt without sending again.

## Single-attempt Relay transport slice

The existing Telegram delivery helper retries ambiguous network errors up to
two times. Calling ordinary /internal/push cannot therefore establish the
unknown-is-not-resent contract. Preserve that existing caller behavior, and
introduce a separate POST /internal/notice endpoint for replacement notices.
It accepts exactly one Telegram or Feishu text leg, a bounded original attempt
key and original route/content digests; no attachment, both-channel request,
scheduler runId or terminal reservation is accepted. It uses the original
internal authentication gate and preserves the existing harness-escalation
structured human-attention gate. Operational requests use an explicit
harness-operational source. Each admitted request invokes the channel send once;
Telegram delivery uses zero retries. Channel SDK behavior must also be checked,
not inferred from the wrapper's attempt count. The response names the
relay-notice-single-attempt/v1 contract and echoes the original request/content
binding alongside the actual delivery receipt.

This endpoint does not claim server-side deduplication. A dedicated native
adapter owns durable original attempt evidence in an installed receipt root.
An exclusive creation, flushed before I/O, permits only one concurrent caller
to send. It atomically retains the exact acknowledged receipt for later
readback. An interrupted request, channel exception, failed send with an actual
attempt, malformed/mismatched response or missing receipt remains unknown.
The absence of a marker or a log does not prove non-delivery. Only a retained
structured response proving rejection before any channel send can establish
absence. There is no automatic HTTP retry or redirect, no arbitrary URL in a
business input, and no fallback to /internal/push on an older Relay.

Installed native routes bind route semantics, actual existing agent/recipient,
loopback endpoint and original managed token reference, plus the receipt root.
Business observations expose digests and real message IDs, never secrets.
Policy preflight/describe/diff and native Host admission supply those routes.
Changing a route cannot silently retarget a captured batch. Migration binds
routes and receipt storage when configured, while policies without notification
configuration retain their existing physical adapter binding.

Acceptance adds concurrent-drain and process-recreation tests, a real HTTP
boundary with a fake channel that fails after possible send, zero downstream
resends, incompatible old-server rejection, exact receipt validation, no
redirect/token leak, native policy loading and original binding preservation.
These tests are source evidence; actual Telegram and Feishu messages and
installed route adoption remain separate native acceptance.

## Isolated transport acceptance surface

For native experiment acceptance, the Relay source also exposes a notice-only
loopback server using the same notice handler and channel delivery functions.
It exposes no pairing, reload, terminal, polling or WebSocket consumer. An
operator-provided exact set of existing agent/channel/recipient bindings rejects
other destinations before send. Startup never calls a channel. The caller reads
existing managed credentials in place, preserves that channel's admitted network
agent and TLS behavior, and supplies the existing SDK send client. No test-only
alternative delivery or fake receipt implementation is an acceptance substitute.
This server is an explicitly scoped experiment surface, not a production restart
or route/configuration change. The exact source, operator bindings, chosen port
and receipt-root policy must be read back before native business commands.
