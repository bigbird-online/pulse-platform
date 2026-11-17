# Harness Site Policy Design

## Problem

The current harness model assumes all agents write one shared `.harness`
directory through the same CLI. That is acceptable on one machine but unsafe
once a second machine has a writable checkout: a Vultr process can accidentally
create, ack, transition, complete, lock, or emit events for local namespaces.

The desired model is shared read visibility with partitioned durable writes.

## Control Surface

The durable write surface includes:

- `work-items/*.yaml`
- `handoffs/*.yaml`
- `events/*.events.yaml`
- `events/*_event-YYYY-MM.jsonl`
- `locks/*.lock`
- `policies/*.yaml`
- `ssot/registry.yaml` or equivalent registry writes
- `scan-reports/*.json`

The first guard lives at the harness command/API layer because Relay, cron, and
agents all eventually call harness functionality. Direct guarded CLI, scan,
dispatch, and escalation paths load the policy from `--site-policy` or
`HARNESS_SITE_POLICY`. Relay must later use that guarded runtime via
`HARNESS_BIN`, service environment, or explicit job delivery config.

## Policy Shape

```yaml
schema_version: "1.0"
site_id: vultr
read_all_harness: true
writable_namespaces:
  - asobi
  - pulse-vultr
  - vultr
  - relay-vultr
cross_site_handoff:
  enabled: true
  target_namespaces:
    - aki
    - amanda
    - reeve
    - duke
    - pulse
    - governance
  allowed_responsibility_transfer:
    - not_transferred
    - partial
```

`*` is allowed only in `cross_site_handoff.target_namespaces`. It means the site
may target any non-local namespace with a request handoff, subject to source
ownership and transfer rules.

## Decision Rules

Read:

- Allowed if `read_all_harness=true`.

Namespace write:

- Allowed only when the target namespace is listed in `writable_namespaces`.

WorkItem create/update:

- Namespace is the WorkItem `owner`.

Policy write:

- Namespace is the target policy agent.

Lock write:

- Namespace is the lock agent.

Event write:

- Namespace is the event's agent or owning WorkItem namespace. Generic global
  events must be routed through a specific responsible namespace before guarded
  mode can accept them.

Handoff create:

- Source WorkItem owner must be local.
- `from_owner` must be local.
- If `to_owner` is local, this is a same-site handoff and is allowed.
- If `to_owner` is remote, `cross_site_handoff.enabled` must be true, target
  namespace must be allowlisted, and `responsibility_transfer` must be
  `not_transferred` or `partial` unless policy expands that list.

Ack / close / transition:

- These are WorkItem updates and must pass the WorkItem owner namespace guard.
- A remote site cannot use `ack --accept` or `complete` to close another site's
  namespace. It must send evidence back through a handoff.

## Rollout Plan

1. Pure authorization module and tests.
2. Optional CLI integration for core durable write commands.
3. Guard scan, dispatch, escalation, policy, registry, and core WorkItem paths.
4. Relay scheduler delivery integration through `HARNESS_BIN` or service env.
5. Remote Vultr smoke tests and runbook.

## Residual Risk

The pure module does not protect anything until runtime paths call it. The
optional guard now protects direct guarded CLI calls plus scan, dispatch, and
escalation writes when policy loading is enabled. Remote cron and Relay delivery
remain unprotected until production config passes `HARNESS_SITE_POLICY` or uses a
guarded `HARNESS_BIN`.
