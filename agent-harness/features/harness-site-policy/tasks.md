# Harness Site Policy Tasks

## P0 Foundation

- [x] Record namespace split and MVS.
- [x] Add pure site-policy authorization module.
- [x] Add unit tests for local/Vultr namespace decisions.

## P1 CLI Guard

- [x] Add site-policy loading from `--site-policy` or `HARNESS_SITE_POLICY`.
- [x] Guard `create`, `assign`, `transition`, `complete`, `handoff`, `ack`.
- [x] Guard `policy write` and registry writes.
- [x] Decide how `scan`, `dispatch`, and escalation events derive their write
      namespace.
- [x] Add CLI smoke tests for allowed and blocked writes.

## P2 Relay / Cron Guard

- [ ] Add guarded `HARNESS_BIN` wrapper or remote env config for production.
- [ ] Update remote cron prompts/config to use the guarded entrypoint.
- [ ] Add smoke proving remote Relay blocks an unauthorized local namespace write.
- [x] Add duplicate job-id check between local and Vultr cron configs.

## P3 Runbook / Ship

- [x] Write normal write procedure.
- [x] Write cross-site handoff procedure.
- [x] Write conflict discovery procedure.
- [x] Write rollback and unlock procedure.
- [x] Record one local verification command and one Vultr verification command.
