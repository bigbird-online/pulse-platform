# ADR-0002：渠道无关 RichDoc 编译与富文本投递

- Status: accepted for implementation; production defaults remain off pending UAT
- Date: 2026-09-04
- Owner: Pulse
- Scope: Relay assistant turn text for Telegram and Feishu

## Context

Relay already uses `ChannelTurnRenderingState` to distinguish status, activity,
delta, cumulative snapshot, commentary, and the one authoritative final. The
channel adapters previously sent that text directly and each used newline or
UTF-16 string slicing at its transport boundary. That could expose incomplete
Markdown to a channel parser, split code/link/table structure, and make future
sanitization drift between Telegram and Feishu.

Commands, button cards, media, binding, routing, runtime selection, WI/SCL,
metering, and delivery receipts are outside this decision. Feishu SDK migration
is also outside this decision. Telegram Bot API 10.1 added Rich Messages after
the legacy `sendMessage(parse_mode=HTML)` contract: the new envelope accepts
Rich HTML tables while preserving HTML's explicit escaping and nesting model.

## Decision

1. `ChannelTurnRenderingState` remains the sole owner of turn-event merging and
   final idempotency. It hands each complete current frame to the compiler.
2. `rich-doc.ts` parses each authoritative Markdown snapshot from the beginning
   into a typed `RichDoc`. It never appends rendered fragments from an earlier
   frame.
3. The parser has raw HTML disabled. Link-shaped syntax is parsed centrally,
   then destinations are accepted only for `http:`, `https:`, and `mailto:`.
   Controls, relative/scheme-relative targets, unknown protocols, invalid URLs,
   and destinations over 2048 code points become inert label text.
4. A shared semantic splitter accepts a renderer and measured-unit budget. It
   prefers whole blocks, then list items/table rows/inline boundaries. Oversize
   code and inline containers are closed and reopened at Unicode code-point
   boundaries. Continuation tables use empty structural headers so source header
   text is not duplicated.
5. Telegram renders the same AST as Rich Message HTML, legacy Rich HTML,
   conservative HTML, and plain text. Rich Message HTML uses
   `sendRichMessage`/`editMessageText.rich_message` and maps tables to native
   `<table>` blocks; legacy Rich HTML retains a preformatted table fallback.
   Definite Bot API capability or formatting rejection moves monotonically
   through that order. Ambiguous network failure is not retried as another
   format because it may already have delivered. A 429 is a definite rejection
   and may be retried.
6. Telegram keeps one evolving progress message. In the new path it sends the
   fresh authoritative final first, best-effort deletes the earlier progress,
   then sends one generated marker at the chat bottom; if final delivery fails
   ambiguously, any remaining progress is terminalized as failed. Duplicate
   final/result events remain no-ops.
7. Feishu renders the same AST as CardKit 2.0 Markdown, post Markdown, and plain
   text. Long answers create one streaming card per semantic chunk. A
   start/update/server-close failure coalesces later frames and permits one
   terminal static answer. Static formatting failure moves monotonically through
   CardKit, post, and text.
8. Active body text and event ids are cleared after terminal delivery. Durable
   `channel.rendering` observations contain only channel, layer, action, mode,
   bounded counts, fallback reason, and terminal result—never body text, body
   hashes, turn scope, or other body-correlated identifiers.

## Capability profiles

Profiles are resolved independently from environment variables. Defaults below
are operational candidates inherited from the current implementation and are
**not platform hard-limit claims**. They remain subject to boundary UAT.

| Channel | Default mode | Payload budget | Unit | Rollover | Elements | Update throttle |
|---|---:|---:|---|---:|---:|---:|
| Telegram | `off` | 4096 | UTF-16 code units | 3800 | 1 | 750 ms |
| Feishu | `off` | 4000 | UTF-16 code units | 3600 | 20 | 1500 ms |

For `CHANNEL` equal to `TELEGRAM` or `FEISHU`:

- `RELAY_CHANNEL_RICHDOC_MODE=legacy|off|shadow|canary|on`
- `RELAY_CHANNEL_RICHDOC_MAX_LENGTH`
- `RELAY_CHANNEL_RICHDOC_LENGTH_UNIT=unicode_code_points|utf16_code_units`
- `RELAY_CHANNEL_RICHDOC_ROLLOVER_THRESHOLD`
- `RELAY_CHANNEL_RICHDOC_MAX_ELEMENTS`
- `RELAY_CHANNEL_RICHDOC_UPDATE_THROTTLE_MS`
- `RELAY_CHANNEL_RICHDOC_CANARY_PERCENT=0..100`

`legacy` is an alias for `off`. `shadow` compiles and validates but sends the
legacy payload. Canary selection uses only an in-memory stable bucket of the
opaque turn scope; body text and body hashes never participate.

Telegram's newer transport envelope is independently controlled by
`RELAY_TELEGRAM_RICH_MESSAGE_MODE=legacy|off|shadow|canary|on` and
`RELAY_TELEGRAM_RICH_MESSAGE_CANARY_PERCENT=0..100`; it is effective only when
the Telegram RichDoc path is selected. Its default is `off`, so adoption and
rollback do not alter the established HTML renderer or the Feishu channel.

## Rollback runbook

Rollback is channel-local and requires no session, binding, runtime, or WI
migration.

1. To roll back only Telegram native tables, set
   `RELAY_TELEGRAM_RICH_MESSAGE_MODE=off`; the established Rich HTML path stays
   active. To roll back an affected channel's whole RichDoc path, set only its
   `RELAY_<CHANNEL>_RICHDOC_MODE=off` (or `legacy`) and leave the other channel
   unchanged.
2. Restart Relay through the site's normal release/activation procedure. Do not
   edit a managed release or service definition in place.
3. Read back the loaded environment and perform one short answer plus one
   duplicate-result probe. Confirm legacy payload shape and one final.
4. If Feishu CardKit itself must also be disabled, separately set
   `RELAY_FEISHU_STREAMING_CARD=off`; that is the pre-existing transport switch,
   not a RichDoc state migration.
5. Re-enable with `shadow`, then a bounded canary, only after inspecting
   metadata-only `channel.rendering` observations.

Automated rollback proof is the legacy/off and independent-profile test set.
Production rollback proof additionally requires loaded-runtime readback and
visible desktop/mobile UAT.

Telegram authority references:

- <https://core.telegram.org/bots/api#rich-messages>
- <https://core.telegram.org/bots/api#sendrichmessage>
- <https://core.telegram.org/bots/api#inputrichblocktable>

## Consequences

- Markdown parsing, URL policy, normalization, splitting, and plain fallback
  have one implementation and one test matrix.
- Channel adapters still own transport calls, interactive UI, and media.
- Telegram keeps HTML as its primary content format. Rich Messages change the
  transport envelope, not the shared Markdown parser or the HTML safety model.
- Rendering adds CPU work per complete streaming snapshot. Update throttling and
  one-slot pending state bound transport pressure; production defaults remain
  off until real-channel calibration.
- A mathematically exact once-only guarantee is impossible after an ambiguous
  network write on APIs without an idempotency key. The chosen behavior avoids a
  speculative resend and leaves a terminal failure marker rather than risking a
  duplicate authoritative final.
