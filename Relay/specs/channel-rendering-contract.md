# Relay channel rendering contract

This contract governs user-visible assistant delivery across `cc`, `codex`,
`cc-sdk`, and `pi`, independently of Telegram or Feishu transport details.

## Semantic inputs

- `status` is an immediately visible, non-semantic busy placeholder. Every
  conversational channel uses the exact accepted-turn acknowledgement
  `▶️ 已收到，开始处理…`. The first real `delta`, `snapshot`, `commentary`, or
  `final` replaces it; status text is never copied into the answer body.
- `delta` is an append-only protocol fragment. Equal fragments from distinct
  sequence ids are both retained.
- `snapshot` is the complete body accumulated so far. A longer snapshot
  replaces its prefix; it is never sent as an additional message.
- `commentary` is visible progress. Independent commentary records are separated
  by a blank line; containment with the current body is deduplicated.
- `activity` is a non-semantic overlay for tool-only work. A shared counter
  starts at the accepted-turn boundary and advances exactly once per runtime
  `turn-effect`; events outside that boundary are ignored. Telegram and Feishu
  produce the same exact count for every event. Transport time coalescing sends
  the newest count rather than dropping non-milestone counts. A count records a
  started execution effect, not a successful business step or a liveness proof.
  The latest count remains the authoritative visible progress overlay between updates;
  later delta, snapshot, or commentary frames cannot hide it. It is replaced
  only by a newer count or terminalization, removed before the final body,
  and never cached as answer text.
- `final` is the sole authoritative terminal body. A streaming final with the
  same text is a finalization of the evolving message, not a second send.

## RichDoc compilation boundary

Every visible frame is compiled from its complete current `text`; rendered HTML
or Markdown from an earlier frame is never appended. `rich-doc.ts` alone owns
Markdown parsing, raw-HTML neutralization, URL allowlisting, canonical Markdown,
plain-text fallback, and semantic splitting. Telegram and Feishu renderers only
map the resulting AST. Incomplete emphasis, links, tables, lists, or code fences
therefore become inert text or closed structures for that frame.

The shared splitter measures the actual candidate payload using the active
channel profile. It prefers whole semantic blocks and closes/reopens oversize
code or inline containers at Unicode code-point boundaries. UTF-16 budgets do
not authorize splitting a surrogate pair.

## Delivery invariants

1. One turn owns one folded process message and one authoritative final answer.
   The existing accepted/count summary is unchanged except for its disclosure
   arrow. The process is collapsed by default; only an explicit user action
   opens it. Routine updates preserve the user-selected open state.
2. Telegram uses a native inline Rich Message callback button; definite native
   capability rejection falls back to a standard inline keyboard. The callback
   only controls the same message view and never invokes the Agent. Public
   process content is separate from the final RichDoc answer compiler.
3. Feishu uses an ordinary JSON 2.0 process card updated in place, with a compact
   disclosure button beside the unchanged summary. It does not need Card Kit
   streaming mode or keepalives. Both channels retain one latest pending view,
   bounded update retries and exact counts. Send the final independently; only
   confirmed successful delivery changes the process to the existing generated
   marker and collapses it once. Failure retains an explicit failed status.
   No default process deletion/recall or additional generated-marker message.
   RELAY_WORK_PROCESS_DETAILS=off retains the existing rendering rollback path,
   including the #524 same-card streaming expiry/update recovery contract.
4. Long bodies are split only after shared semantic compilation. Intermediate
   Telegram rendering edits the first bounded chunk; remaining chunks are sent
   once at finalization. Feishu rollover cards collectively contain each source
   semantic leaf once; close/reopen wrappers are transport structure, not copied
   answer text.
5. Tool activity and delivery failure notices remain visible but are not merged
   into the authoritative answer body after finalization. Before terminalization,
   the latest activity count must remain visible even if an adapter receives
   a later non-final semantic frame.
6. An accepted turn creates the shared visible status frame immediately.
   Typing or a reaction may supplement that frame but is not sufficient
   acknowledgement. Transport keepalives may replay the latest visible frame,
   but must not invent channel-specific elapsed-time, tool-detail, or generic
   legacy progress text.
7. A user message arriving during an active Pi turn is sent through Pi's native
   steering queue and remains part of that active turn. It must not be rejected
   as a generic channel send failure or create a second competing final.
8. Tool-only assistant records are not a silent interval. Opaque `turn-effect`
   events advance the shared accepted-turn counter; Telegram and Feishu must
   display the same activity text and exact count. Tool names, arguments, body
   text, and full-text hashes are not used as durable progress identities.

## Explicit tool-file delivery boundary

Runtime file writes are not rendering or delivery events. Channels that support
outbound runtime files (Telegram and Feishu) accept only top-level, non-hidden,
regular files explicitly placed in the session's own outbox
`{workspace}/.relay/outbox/{session}/`, where `{session}` is the pool key with
characters outside `[A-Za-z0-9._-]` replaced by `_`. Several channel sessions of
one agent may share a workspace; each session scans only its own outbox, once
after the authoritative turn settlement, so a file reaches only the conversation
that produced it. Intermediate `write`/`edit` events never trigger attachment
delivery. Files directly under `{workspace}/.relay/outbox/` have no owner: they
are never delivered or deleted, and the runtime logs one warning per path.

The emitted event carries tenant identity and the exact runtime workspace. The
channel independently revalidates lexical containment, realpath containment,
regular-file type, and tenant/channel ownership against that session's outbox. Ordinary workspace files,
directories, hidden files, nested paths, symlinks, and path escapes are neither
delivered nor deleted. Process exit, runtime kill, restart, and idle reclamation
must preserve pending outbox files. Only the channel adapter that has confirmed
delivery may remove that one validated outbox file.

A channel without this capability must ignore the event rather than infer a new
media contract. Inbound Telegram and Feishu media behavior remains independent
of outbound outbox delivery.

## Identity and recovery

Idempotency scope is `tenant/session/turn/channel` plus opaque source sequence
ids. Source text and full-text hashes are not durable identities. In-memory body
text may be retained only for the active turn. Replayed event ids and repeated
finalization are no-ops; a new accepted turn resets transient rendering state.

Runtime adapters label known commentary/final records. Channel adapters retain
only transport capability: Telegram legacy `send/edit` plus Rich Message
`send/edit`, Feishu `card start/update/close`
plus one terminal static fallback. Neither channel may re-infer delta versus
snapshot from ad hoc string concatenation. Terminalization clears active body,
rendered payload, and replay ids while retaining only a bounded idempotency
tombstone until the next turn begins.

Pi exports completed public assistant text as commentary when a following tool
or assistant message proves it is progress. Text accompanying a tool call is
commentary immediately, not an invalid answer to discard. Pending tool-free
text remains in the runtime until the next native boundary; only the last
successful candidate becomes the final at `agent_settled`. Private thinking,
unclassified streaming deltas and raw protocol frames are not public process.
Codex likewise separates public `agent_message` records from the terminal tail;
known commentary is forwarded immediately, otherwise the next item/tool boundary
classifies the previous candidate. Its final comes from the native terminal
candidate / last-message file, not concatenated progress. This does not migrate
the Codex CLI protocol to App Server or expose raw reasoning protocol frames.
Public reasoning summaries retain source=reasoning_summary and are visibly labelled
“原生推理摘要”, not conflated with assistant work explanations. The pinned Codex CLI
[`ReasoningItem.text`](https://github.com/openai/codex/blob/rust-v0.153.4/sdk/typescript/src/items.ts)
is explicitly a summary and is retained on item completion. Pi's pinned
[Responses adapter](https://github.com/earendil-works/pi/blob/914cf1472e715297caa30db4b9535d534a9eb718/packages/ai/src/api/openai-responses-shared.ts)
puts both summary and private replay data inside thinking containers. For its
known Responses APIs, select only `.text` from `thinkingSignature.summary[]`
entries with `type=summary_text`; never forward thinking text, raw content or encrypted
payload. Pi textSignature v1 `phase=commentary` is public immediately; ordinary
text remains a candidate. This preserves public summaries and commentary from
the same native message without concatenating them into the final answer.
Pi adds the bounded public-progress communication policy using its existing
repeatable append-system-prompt option. Preserve reviewed context/persona; simple
questions may answer directly. Never script phases, fabricate progress, expand
summaries into invented deliberation, or request private chain-of-thought.
Header metadata follows status inline in both native channels; no footer or
second metadata component forces duration onto a separate line. See
[scheme-one details](work-process-scheme1.md).

Channels keep exact activity counts independently of commentary. The settled
answer is delivered independently before the process is folded once and marked
`✅ 本轮回复已生成`. Reopening and duplicate settlement cannot collapse it again.
Public message identity, not equal wording, determines final exclusion. Neither
process text nor its controls become reply-context, metric or authority records.

## Rollout and acceptance

RichDoc capability, payload unit and budget, element budget, update throttle,
rollover threshold, and rollout mode are explicit per-channel configuration.
`off`, `shadow`, `canary`, and `on` are channel-local states; enabling one channel
does not migrate sessions, bindings, commands, media, or the other channel.

Release acceptance requires automated parser/sanitizer/splitter and transport
boundary tests plus real Telegram and Feishu mobile and desktop UAT. UAT covers
the accepted status, time-coalesced exact activity counts, transport recovery, representative rich structure,
over-limit semantic rollover, one authoritative final, duplicate suppression,
and any enabled media/outbox capability. Production observations persist only a
bounded case id, profile/mode, client surface/version when available, and
pass/fail outcome; complete bodies, complete-body hashes, credentials, and
high-cardinality content-derived identities are forbidden.

Fallback branches are proven with deterministic adapter tests or naturally
occurring bounded production evidence. Acceptance must not manufacture an
unsafe transport, credential, network, or production API failure. A case with
no safe trigger is recorded as `not_executed`, not silently promoted to pass.
Activation evidence must identify an exact immutable release containing the
accepted commits and a strict live process readback; CI, source merge, or a
prepared release alone is insufficient.

## Folded public work process (Issue #535)

The 2026-09-19 user-approved correction supersedes the tools-only presentation:
match the Codex App's chronological public work narrative. Preserve the exact
accepted/activity count line and default collapsed disclosure. After expansion,
show every retained public commentary paragraph at its original position,
interspersed with compact tool rows. Do not replace commentary with generated
summaries, `progress description` headings, terminal-style blocks or a tool log.
Markdown prose uses the shared rich-document compiler; code uses code blocks.
Tool input and output are hidden by default and revealed only by opening that
tool. Expanding the outer process never implicitly expands its tools.

Paragraph identities are native message ids or turn-local monotonic ids. Tool
updates retain their original row. Cumulative snapshots update their current
paragraph; a new tool boundary freezes the prior paragraph. Distinct commentary
records with identical wording remain distinct. Commentary does not increment
activity counters, change runtime authority, or become an authoritative answer.
Private prompts, thinking blocks, unclassified deltas and credentials are not
exported. Failure, unknown and interrupted tool states must remain factual.

Send the authoritative final separately and exactly, without prepending prior
commentary even if the final repeats part of it. After confirmed delivery,
collapse the process once. Preserve manual reopening after settlement and Relay
restart. The retained header includes fixed elapsed time (`用时 X 分 Y 秒`) from
the accepted turn to its terminal disposition; no timer-driven activity count.
Old records without a terminal timestamp do not invent one. Ordinary updates
preserve both outer and individual-tool choices. Hidden result-only updates
are persisted without unnecessary message edits. A callback at the publication
drain boundary must not strand the last requested display revision.

Telegram uses its existing Rich Message send/edit path, callback link rows and
normal paragraph HTML. Its existing plain-text/inline-keyboard fallback remains.
Feishu retains JSON 2.0 message cards and the existing create/patch path: auto-width
outer control, normal Markdown paragraphs and compact per-tool buttons. Valid
Feishu callbacks are acknowledged before asynchronous edits/file delivery finish.
All callbacks are scoped to the actual bot, recipient and original message.
Tool callbacks refer to a digest of the opaque event id, never a body hash.
UI actions only read retained records; they never call the Agent or rerun tools.

There is no last-12/40 selection, average per-paragraph truncation, pagination,
or extra automatically posted process message. Channel budgets select whole
chronological paragraphs and tool row headings first; even explicitly opened
tool output cannot displace the surrounding commentary. Current conservative
transport budgets (not platform hard-limit claims) are 28,000 encoded HTML bytes
/ 450 measured blocks for Telegram Rich Message, 4,000 UTF-16 units / 90 keyboard
rows for its plain fallback, and 26,000 encoded JSON bytes / 190 body elements
for Feishu. Formatting and omission notices count towards these budgets.

Only an actual capacity overflow exposes `查看完整过程`. Clicking it sends the
same recipient a complete retained, script-free HTML document, with continuous
commentary and native `details` tool blocks closed by default. This is a
read-only file snapshot, not a public URL, additional live view, new web service,
or model invocation. Its restrictive CSP permits no scripts/network requests.
Concurrent export clicks are coalesced; uncertain sends are not blindly retried.
If storage itself omitted content, both inline view and document say so. Expired
or missing historical records cannot be reconstructed by inventing commentary.

Sanitize before storage and rendering; suppression of sensitive tool targets
persists across start/end even when the latter omits the path. Per turn retain
at most 2,000 items / 2 MB; public commentary up to 512,000 characters per paragraph,
tool output up to 32,000, input up to 2,000, with explicit capacity notices.
At the first 1 MB retained-content boundary, tool bodies become explicit omitted
placeholders, reserving the upper half of the existing capacity for subsequent
public commentary rather than letting hidden logs starve the narrative.
Sanitized history remains private runtime-owned files (directory 0700, files
0600), seven-day retention, at most 512 disk records and 64 historical in-memory
records. It is never a prompt, permission or outcome authority.

Acceptance fixtures must include narrative A → tool 1 → narrative B → tool 2 →
narrative C → one independent final, more than 12/40 short paragraphs, long
outputs that do not starve narrative, identical text in separate messages,
continuous snapshot segments, Unicode/markup/credential boundaries, nested
callbacks at drain time, failure/late/restart behavior, fixed elapsed time,
recipient-bound overflow exports and failed delivery. Automated native-adapter,
real channel wiring and payload tests are not desktop/mobile client UAT.
Fixed-ref GitHub Actions and real TG/Feishu desktop/mobile interaction remain
separate gates; source merge or a rendered fixture cannot claim deployment or
production acceptance.
