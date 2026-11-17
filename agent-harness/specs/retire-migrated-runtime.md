# Retire the WI184 source runtime after WI219 toolchain adoption

## Goal

Remove the obsolete Mac CWA Harness Python environments and the WI184 source kernel archive after the current PULSE kernel, Harness launcher, and Relay have adopted code-only releases on the unique account Python environment. The owner has authorized retirement of this historical material.

## Boundary

`retire-migrated-runtime.py` accepts exact absolute paths and WorkItem authorization. It handles the one reviewed source state: 12 private outer releases, 114 archived inner releases, and seven Host profiles bound to that source. It never changes the current PULSE kernel or active Harness pointer. It keeps its receipts outside the retired trees.

## Acceptance

- `plan` verifies the first-baseline receipt, WI184 migration intent, source tombstone, archive fence and digests, matching archived/current baseline journals, all expected release and profile paths, and current/previous SCL outer releases outside the retired state.
- A process using any source path blocks staging. `stage` moves exact sources into quarantine and rolls back completed moves on failure.
- Current Harness, SCL and Relay are read back after staging. `purge` rechecks the staged release/profile set and live process absence before deleting quarantine; the independent retirement receipt remains.
- The Python environment audit falls by 12, with no unknown scan scope, and historical Host profiles cannot be selected as live entries afterward.

## Risk and recovery

This is a high-risk, irreversible retirement. Before purge, restore staged paths with the stage receipt if a current consumer fails. A purge intent is durable before deletion; partial deletion must remain visible and resumable rather than be reported as success. The original WI184 migration records remain historical evidence and are superseded by the WI219 retirement receipt.
