# Harness Site Policy Runbook

## Scope

This runbook covers guarded harness CLI usage for local and Vultr sites. Direct
CLI, scan, dispatch, and escalation writes are guarded when `--site-policy` or
`HARNESS_SITE_POLICY` is present. Relay and cron must not be considered
protected until their service env or `HARNESS_BIN` path uses guarded mode and a
real entry smoke has passed.

## Policy Locations

Recommended local-only paths:

- Local: `/Users/owner/.harness-site-policy.yaml`
- Vultr: `/Users/owner/Coevolution/Bigbird/PULSE/data/harness-site-policy.yaml`

These files are site-local runtime config. Do not Git-sync them as shared truth.

## Local Policy Example

```yaml
schema_version: "1.0"
site_id: local
read_all_harness: true
writable_namespaces:
  - aki
  - amanda
  - reeve
  - duke
  - pulse
  - governance
cross_site_handoff:
  enabled: true
  target_namespaces:
    - asobi
    - pulse-vultr
    - vultr
    - relay-vultr
  allowed_responsibility_transfer:
    - not_transferred
    - partial
```

## Vultr Policy Example

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

## Normal Write

Use the guarded entrypoint:

```bash
HARNESS_SITE_POLICY=/Users/owner/Coevolution/Bigbird/PULSE/data/harness-site-policy.yaml \
/home/pulse/.venvs/agent-harness/bin/harness --root /Users/owner/Coevolution/.harness \
  create --title "Asobi task" --owner asobi --criteria "done"
```

Expected: succeeds on Vultr because `asobi` is a Vultr namespace.

## Blocked Write

```bash
HARNESS_SITE_POLICY=/Users/owner/Coevolution/Bigbird/PULSE/data/harness-site-policy.yaml \
/home/pulse/.venvs/agent-harness/bin/harness --root /Users/owner/Coevolution/.harness \
  create --title "Amanda task" --owner amanda --criteria "done"
```

Expected: exits non-zero with `site-policy denied work_item.create
namespace=amanda`.

## Cross-Site Handoff

Vultr asks Amanda to act by creating a handoff from a Vultr-owned WorkItem:

```bash
HARNESS_SITE_POLICY=/Users/owner/Coevolution/Bigbird/PULSE/data/harness-site-policy.yaml \
/home/pulse/.venvs/agent-harness/bin/harness --root /Users/owner/Coevolution/.harness \
  handoff wi_YYYYMMDD_NNN \
  --to amanda \
  --input "Vultr needs local action" \
  --output "Amanda returns evidence or decision" \
  --responsibility-transfer not_transferred
```

Expected: succeeds only when the WorkItem owner and `from_owner` are Vultr-owned
namespaces. `--responsibility-transfer full` is blocked by default.

## Closure Authority

The site that owns the source WorkItem keeps closure authority unless a later
policy explicitly allows a full transfer. The receiving site may accept/reject
the handoff and return evidence, but it must not directly complete another
site's WorkItem.

## Conflict Discovery

Check for namespace drift:

```bash
grep -REn "owner: (aki|amanda|reeve|duke|pulse|governance)" /Users/owner/Coevolution/.harness/work-items
grep -REn "to_owner: (aki|amanda|reeve|duke|pulse|governance)" /Users/owner/Coevolution/.harness/handoffs
```

On Vultr, local namespaces in WorkItems should be read-only historical state or
handoff targets, not newly created Vultr-owned writes.

Check for duplicate cron job ids before enabling both sites:

```bash
harness site check-cron-ids \
  --local /tmp/local-cron-jobs.json \
  --remote /Users/owner/Coevolution/Bigbird/PULSE/data/cron-jobs.json
```

Expected: exits zero with no duplicate ids. `/tmp/local-cron-jobs.json` should be
a fresh snapshot from the local site. If the command reports a duplicate, rename
or disable one side before enabling guarded delivery.

## Rollback

For direct CLI rollout:

1. Unset `HARNESS_SITE_POLICY` or point `HARNESS_BIN` back to the previous
   unguarded harness binary.
2. Preserve the rejected command output as evidence.
3. Inspect `.harness/work-items`, `.harness/handoffs`, and `.harness/events`
   for partial writes.
4. If a bad write landed, route cleanup to the namespace owner through a
   handoff; do not repair the other site namespace in place.

## Unlock

For agent locks:

```bash
ls -l /Users/owner/Coevolution/.harness/locks
```

Only clear a lock after checking no live process owns it. Prefer process-level
verification before deleting stale lock files.

## Verification Commands

Local:

```bash
HARNESS_SITE_POLICY=/Users/owner/.harness-site-policy.yaml \
/Users/owner/Coevolution/Bigbird/.venv/bin/harness \
  --root /Users/owner/Coevolution/.harness \
  create --title "Local policy smoke" --owner amanda --criteria "done"
```

Vultr:

```bash
HARNESS_SITE_POLICY=/Users/owner/Coevolution/Bigbird/PULSE/data/harness-site-policy.yaml \
/home/pulse/.venvs/agent-harness/bin/harness --root /Users/owner/Coevolution/.harness \
  create --title "Vultr policy smoke" --owner asobi --criteria "done"
```

Run these only in an agreed smoke window because they create durable WorkItems.
