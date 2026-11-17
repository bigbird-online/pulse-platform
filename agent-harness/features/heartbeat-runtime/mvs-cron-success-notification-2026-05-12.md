---
feature_name: "heartbeat-runtime"
change: "shorten-cron-success-notification"
created: "2026-05-12T07:10:00+0800"
tags: [mvs, bugfix, escalation, heartbeat, night-patrol]
---

# MVS: Short Cron Heartbeat Success Notifications

## Goal

When Harness dispatcher runs a scheduled heartbeat/night-patrol task through
Relay cron and the autonomous run succeeds with output, Telegram should receive
the same compact human notification style as the older Relay job path.

## Boundary

- Change only Harness escalation message formatting and tests.
- Preserve the generic escalation template for non-heartbeat
  `autonomous_success_with_output` events.
- Do not rewrite historical events or change Relay scheduling semantics.

## Context

The earlier short notification fix covered legacy events shaped as
`trigger_mode=relay_job` with `match_label=autonomous_exploration_done`.
The current dispatcher emits successful heartbeat/night-patrol output as
`trigger_mode=cron` with `match_label=autonomous_success_with_output`, so these
events fall through to the verbose generic escalation format.

## Acceptance

- A real-shaped Pulse 2026-05-12 cron heartbeat success event formats as:
  `Pulse 夜巡：无需处理` plus a one-line summary.
- A cron autonomous success event whose memo is not a `cron-heartbeat-*` memo
  still uses the generic escalation template.
- Existing Relay job short notification behavior remains unchanged.

## Risks

- Formatter detection must stay narrow enough not to hide ordinary handoff or
  violation outputs.
- Because `EscalationEvent` does not carry the dispatch `hit_type`, the
  formatter keys off the stable cron heartbeat memo path/message shape.

## Explicit Non-Goals

- No scheduler refactor.
- No escalation event schema migration.
- No backfill or mutation of prior Telegram messages.
