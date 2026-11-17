# RichDoc channel UAT and rollback evidence

Status: runbook ready; real-channel execution pending deployment authority

Authority and design: `specs/adr/ADR-0002-channel-neutral-rich-document-rendering.md`.
This document is an evidence checklist, not a claim that pending rows passed.
Never paste bot tokens, user/channel identifiers, full answers, or body hashes
into this file or observability receipts.

## Preconditions

1. PR CI and default-branch verification are green.
2. The site release is installed through the capacity-controlled release writer.
3. Loaded runtime readback identifies the merged release and confirms the two
   channel modes and capability profiles. For Telegram native tables, also read
   back `RELAY_TELEGRAM_RICH_MESSAGE_MODE`; a source checkout result is not enough.
4. Start with one channel in `shadow`; leave the other `off`. For the Telegram
   Rich Message envelope, start with its independent mode in `shadow` while the
   established Telegram RichDoc mode remains unchanged. Inspect bounded
   `channel.rendering` metadata, then enable a named canary. Do not migrate or
   reset sessions, bindings, runtimes, or WI state.
5. Use a dedicated canary conversation. Record only timestamp window, client
   class, release revision, profile values, observed delivery count/mode, and
   pass/fail reason.

## Shared visible fixture

Send one prompt that requests, in order:

- one heading and two paragraphs;
- bold, italic, strikethrough, inline code, and a horizontal rule;
- ordered, unordered, and two-level nested lists;
- a quote;
- one HTTPS link and visible text containing a rejected `javascript:` link;
- a 3-column table containing Chinese and emoji;
- a fenced TypeScript block containing `<tag>`, `&`, backticks, and emoji;
- enough repeated sections and one oversize code block to force rollover.

The answer must include a unique first marker and last marker chosen for the UAT
but those markers must not be persisted in receipts. Verify visual order and
copy/paste the visible answer into a local ephemeral comparison tool; retain
only counts and pass/fail, not the body or its hash.

## Client matrix

| Channel | Desktop | Mobile | Short | Streaming long | Code/table/list/link/CJK/emoji | Rollover | Duplicate event | Result |
|---|---|---|---|---|---|---|---|---|
| Telegram | pending | pending | pending | pending | pending | pending | pending | pending |
| Feishu | pending | pending | pending | pending | pending | pending | pending | pending |

For each client/channel:

1. exactly one progress surface evolves;
2. status/activity is absent from the final answer body;
3. exactly one fresh complete final answer is visible;
4. every source character is present once and in order after ignoring only
   structural close/reopen wrappers;
5. links remain clickable only for allowlisted protocols; raw HTML is inert;
   Telegram tables with at most 20 columns render as native rows/cells rather
   than visible pipe-delimited Markdown; Rich Message headings, paragraphs,
   nested lists, thematic breaks, and inline source newlines retain visible
   block/line boundaries instead of collapsing into one line;
6. all code fences/cards/messages close cleanly; no replacement character is
   visible around emoji;
7. duplicate final/result delivery produces no additional answer;
8. ordinary Feishu answer cards contain no whole-answer or per-code copy
   buttons; fenced code remains inside the clean Markdown surface. Business
   interaction cards such as menus and question choices remain unaffected.

## Failure matrix

Use a controlled canary transport fault on the deployed artifact; never alter a
managed release tree in place.

| Channel | Fault | Expected | Desktop | Mobile | Evidence |
|---|---|---|---|---|---|
| Telegram | Rich Message capability/format rejection | legacy Rich HTML, then no duplicate | pending | pending | metadata-only fallback sequence |
| Telegram | Rich HTML definite format rejection | conservative HTML, then no duplicate | pending | pending | metadata-only fallback sequence |
| Telegram | conservative HTML definite rejection | plain text, then no duplicate | pending | pending | metadata-only fallback sequence |
| Telegram | 429 with retry-after | one accepted final; progress terminal | pending | pending | attempts/result counts only |
| Telegram | ambiguous network write | no speculative cross-format resend; failed terminal marker | pending | pending | terminal result only |
| Feishu | CardKit start failure | one complete terminal static answer | pending | pending | fallback mode/result only |
| Feishu | update/server-closed race | partial stream plus at most one complete terminal answer | pending | pending | rollover/fallback counts only |
| Feishu | static CardKit format rejection | post Markdown | pending | pending | metadata-only fallback sequence |
| Feishu | post format rejection | plain text | pending | pending | metadata-only fallback sequence |
| Feishu | repeated close/result | no second complete answer | pending | pending | terminal count only |

## Boundary calibration

Run payload lengths immediately below, at, and above each configured threshold;
include BMP text and emoji. Record accepted/rejected, visible chunk count,
rollover count, and client result. If real behavior disagrees with defaults,
change the profile and its automated boundary test—do not describe the old
constant as a platform limit.

## Rollback drill

For each channel independently:

1. capture current loaded profile and visible canary result;
2. for Telegram native-table rollback, first set only
   `RELAY_TELEGRAM_RICH_MESSAGE_MODE=off` and prove legacy Rich HTML remains
   healthy; for whole-channel rollback, set only
   `RELAY_<CHANNEL>_RICHDOC_MODE=off` in the next managed release;
3. activate through the release writer and read back loaded environment;
4. send short, long, and duplicate-result probes; confirm legacy payload and one
   final without changing any session/binding/runtime/WI state;
5. restore `shadow`, activate, and read back again.

| Channel | Off readback | Legacy visible probe | No state migration | Shadow restore | Result |
|---|---|---|---|---|---|
| Telegram Rich Message envelope | pending | pending | pending | pending | pending |
| Telegram RichDoc path | pending | pending | pending | pending | pending |
| Feishu | pending | pending | pending | pending | pending |
