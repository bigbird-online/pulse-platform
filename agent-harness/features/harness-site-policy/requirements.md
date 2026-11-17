# Harness Site Policy Requirements

## Minimum Viable Spec

Goal: allow multiple physical sites to share global harness state for reading
while preventing each site from writing durable state outside its owned
namespace.

Feature risk: high-risk. This feature controls cross-machine writes,
coordination state, cron delivery, lock files, and ownership closure.

Current slice risk: high-risk by surface, deployed as default-off. This slice
adds a specification, pure authorization module, optional CLI guard, guarded
scan/dispatch/escalation write paths, and a cron job id conflict checker. Guard
mode is enabled only by `--site-policy` or `HARNESS_SITE_POLICY`. It does not
change Relay, cron jobs, production config, or remote machines.

## Namespace Split

Local site writable namespaces:

- `aki`
- `amanda`
- `reeve`
- `duke`
- `pulse`
- `governance`

Vultr site writable namespaces:

- `asobi`
- `pulse-vultr`
- `vultr`
- `relay-vultr`

Both sites may read global `.harness` state.

## Functional Requirements

1. A site policy declares `site_id`, `read_all_harness`, writable namespaces,
   and cross-site handoff permissions.
2. Reads are allowed when `read_all_harness=true`.
3. WorkItem creation and updates are allowed only when the WorkItem owner is in
   the local site's writable namespace list.
4. Policy writes, lock writes, and agent-scoped event writes are allowed only
   for namespaces owned by the current site.
5. Same-site handoffs are allowed when source WorkItem owner, sender, and target
   are all writable by the site.
6. Cross-site handoffs are allowed only as request handoffs from a locally owned
   WorkItem/sender namespace to an allowlisted remote namespace.
7. Cross-site handoffs must not use `full` responsibility transfer unless a
   policy explicitly allows it.
8. A site must not transition, complete, fail, or ack another site's WorkItem as
   a substitute for a cross-site handoff.
9. Relay and cron paths must eventually call the guarded harness entrypoint,
   not a bare unguarded `harness` binary.
10. Runtime data and site-local configs stay independent. `PULSE data/`,
    `relay.json`, `cron-jobs.json`, metering, and state files are not Git-synced
    as shared truth.

## Acceptance Evidence For Full WI

- Unit tests for local and Vultr site-policy decisions.
- CLI smoke proving an unauthorized Vultr write to `amanda` is blocked.
- CLI smoke proving an authorized Vultr write to `asobi` is allowed.
- CLI smoke proving a Vultr-to-Amanda request handoff is allowed.
- Relay or cron smoke proving remote `HARNESS_BIN` or service env uses the
  guarded entrypoint.
- A duplicate cron-job-id check command proving local and Vultr will not run the
  same job id.
- Runbook with normal write, cross-site handoff, conflict discovery, rollback,
  unlock, and one verification command per site.

## Explicit Non-Goals In This Slice

- No production restart.
- No remote Vultr write.
- No remote `HARNESS_BIN`, service env, or Relay config change.
- No Git synchronization policy for runtime data.
- No closure authority migration.
