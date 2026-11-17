---
feature_name: "execution-plan-adoption"
version: "0.2.0"
status: "implemented_candidate"
created: "2026-08-20T00:00:00+0800"
modified: "2026-10-07T00:00:00+0800"
owner: "Pulse"
tags: [design, execution-plan, adoption, locking, idempotency]
---

# Harness Execution Plan Adoption — Design

## 1. Authority and interface

`ExecutionPlanManager` owns admission, retry, original reads and projection. An adoption
binds an existing WI phase to its approved plan; it grants no execution authority and
does not change owner, lifecycle status, executor or completion.

The sole writer is the local lifecycle kernel. The write entry uses its existing
one-time original import, initializing the unmanaged local kernel when necessary.
A managed installation with a missing kernel never falls back to file writes.

`ExecutionPlanAdoption` retains schema `execution_plan_adoption/v1`: WI, phase,
repository-relative plan reference, SHA-256, adopter, adoption time and source references.
The adoption ID derives from `(work_item_id, phase, plan_ref, plan_sha256)`.

## 2. One decision

Under the existing per-WI lock, admission captures the kernel frontier and original plan
bytes. The kernel compares that frontier and atomically records the adoption, its
`execution.plan_adopted` event, the admitted bytes and any publication effect. A concurrent
terminal decision rejects the whole operation. Same-phase different plans conflict.

Exact retries return the first adoption, including its original actor and time. A later
terminal state does not revoke it. Missing input files do not invalidate an already
admitted plan; supplied different bytes do. Publication may attach to the existing
decision through the shared `publish_committed_decision` operation without adopting again.

There is no direct adoption-file writer or event-append repair path. After commit, the
shared projection writer renders `.harness/executions/adoptions/<wi>/<phase-hash>.yaml`
and the event ledger. Projection failure leaves the decision recoverable in the kernel.
Admitted plan bytes have immutable evidence archives; rebuilding does not overwrite a
producer's current plan draft.

## 3. Original history

Initial entry and upgrades share `capture_original_history`. It reads one selected
original source, including a named Git revision in a sparse checkout. Existing adoption
records and events are checked once, then become immutable kernel history.

A legacy record written before a process died appending its event remains the first
fact. Its original actor/time are retained, and validation reports `event_id=null`.
No original event or new human approval is invented. Original plan bytes are retained
only when available and matching the recorded digest; a newer draft is not old evidence.
Absent additive history fields preserve existing serialized baseline digests.

## 4. Read and validate

`show` and default `validate` read original kernel facts. Missing, malformed or forged
YAML projections cannot change the result. Reads never import or repair records.
`validate --plan <file>` additionally checks that explicitly supplied file against the
original reference and digest. A new draft may therefore fail the explicit comparison
without invalidating the original adoption.

Unimported historical files remain read-only original material until entry admits them.
They are not declared disposable before that admission.

## 5. Publication

The Broker resolves `execution adopt` to the committed kernel operation. Replays render
its original response without opening or re-executing its plan input. Issuance, recovery
and replication use the same kernel-owned projection inventory. A newer local decision
may add views absent from an older published commit; it does not invalidate that delivery.

The actual remote CAS and adopted Git commit prove delivery. Per-file equality of
mutable views is not a second completion gate. Non-projection input preservation,
symlink boundaries, resource authority and unknown external effects remain enforced.

## 6. Surface scope

`/adopt` accepts an explicit WI or exactly one current-WI binding supplied by the Host.
It rejects missing or ambiguous scope. The CLI JSON contract remains
`execution_plan_adoption_result/v1`; adoption does not imply phase or WI completion.
