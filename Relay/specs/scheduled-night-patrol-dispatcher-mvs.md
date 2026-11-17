# Scheduled Night Patrol Dispatcher MVS

Date: 2026-05-11

## Goal

Scheduled night patrol must enter through Harness dispatcher so runtime permissions come from `.harness/policies/<agent>.yaml`, not from Relay direct Agent prompts.

## Boundary

- In scope: local Harness scan/policy/dispatcher support, Relay scheduler support, local cron job configuration, live `.harness/policies/*.yaml`.
- Out of scope: running the Aki experiment, changing Aki workspace management files, completing Aki business work, mutating Harness WI state from a night-patrol sub-agent.

## Context

Current 03:00 night patrol jobs are Relay direct-spawn Agent jobs with `delivery=harness_escalation`. That bypasses Harness policy path constraints. Existing hourly heartbeat jobs ask an Agent prompt to run `harness scan/dispatch`, which is better but still unnecessary indirection and cannot trigger a no-hit patrol because `harness scan` has no scheduled patrol hit.

## Acceptance

- Relay has a non-LLM `harness_dispatch` job path that runs `harness scan` and, on hits, `harness dispatch` directly.
- Harness can emit a policy-visible `scheduled_night_patrol` hit so no-signal night patrol can still spawn through dispatcher.
- Aki scheduled patrol can use a narrow audited-action policy path without granting hourly handoff spawns blanket management-file access.
- Existing direct 03:00 night-patrol jobs are migrated to the dispatcher path.
- Targeted Harness and Relay tests pass; live Relay is rebuilt and reloaded before claiming production effect.

## Risk

Feature risk: high. This touches scheduler, policy, path constraints, and production cron configuration.

Slice risk: high. The implementation changes live 03:00 job behavior, but keeps rollback simple: revert the commits/config and reload Relay.

## Explicitly Not Doing

- No cross-workspace writes from Aki.
- No B11, briefing, weekly, scoring, or other operations authority.
- No Harness durable state mutation by the night-patrol sub-agent.
- No human final judgment substitution.
