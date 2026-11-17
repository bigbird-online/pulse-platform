---
feature_name: "wi479-broker-rework"
version: "0.2.0"
status: "in_progress"
root_work_item: "wi_20260822_479"
modified: "2026-08-24T00:00:00Z"
owner: "Pulse"
---

# WI479 Broker-only rework decision

## Problem

The first WI479 candidate safely refused dirty or diverged shared roots, but
recurring human edits and forgotten local commits are normal inputs. Refusal
alone leaves the four-end control plane unusable. It also leaves a bootstrap
contradiction: Agents still mutate Git-resident Harness state directly even
though human branches are supposed to be Broker-only.

## Approved authority model

- Agents submit typed external mutation requests and may commit/push only the
  exact branch registered for their active WI worktree.
- `feature/doc-para` accepts only `human_checkpoint` Broker transactions.
- `dev` accepts only `promotion` Broker transactions.
- `main` accepts only `release` Broker transactions.
- The first rollout is `protocol_only`: one OS principal may still bypass local
  hooks. `user.name`, email, an empty Agent stamp, and free-form `[broker]` text
  are never treated as authority.
- A valid `BROKER_TX_REF` is external, bounded, diff-bound, expiring and
  single-use. The corresponding commit keeps an auditable trailer.

## Human-dirty settlement

Ordinary tracked changes, deletions and non-ignored untracked files are
snapshotted as one unit. Ignored files, credentials, oversized files, nested
repositories, out-of-root symlinks or a changing preimage block the whole
transaction. The Broker never publishes a safe subset and hides the remainder.

All enrolled endpoints are snapshotted before publish. A dedicated Broker
worktree applies them in fixed endpoint order, one provenance commit per
endpoint. Only a fully prepared, conflict-free chain may update
`feature/doc-para`, using one compare-and-swap fast-forward ref update. GitHub
movement invalidates the prepared chain. Content conflict leaves GitHub and all
shared roots unchanged.

Local ahead commits retain their SHA, author and history. Diverged histories
are integrated with a Broker-tagged merge commit; rebase and cherry-pick are
not allowed. Untagged commits that predate the guard require one explicit
bootstrap inventory and human confirmation.

## Harness ledger flush authority

Runtime-owned JSONL append dirt is not human dirt and MUST NOT borrow
`human_checkpoint` authority. A separate service-scoped
`harness_ledger_flush` transaction may settle it only when every dirty path is
a tracked monthly Harness event shard, the live file is the byte-for-byte Git
preimage followed by one or more complete JSON lines, and every appended line
validates as its declared Harness event schema. The transaction binds the
source base SHA, authority ref SHA, preimage size and digest, exact append size
and digest, postimage digest, ordered event ids, effective producers, expiry,
and one-time credential.

Classification is proof-based, never path-based. Rewrites, staged changes,
untracked event files, invalid or partial JSON, mixed/unknown dirt, duplicate
paths, a concurrent local append, or remote ref movement fail closed. The
service transaction publishes only to the shared service branch; it never
commits global events to the triggering business WI branch and never executes
`git commit` from `/wi`.

The Broker freezes the append proof, prepares one provenance commit from the
exact remote authority SHA in its external worktree, and performs a normal
fast-forward push whose expected remote SHA is recorded in the transaction.
After publish it reconciles the source checkout only if each live file is
exactly the captured preimage or captured postimage: exact captured append
bytes are removed, then the checkout advances with `merge --ff-only` to the
published SHA. A retry recognizes an already-published commit or an already
reconciled checkout and continues without a second commit. Any third state
requires recovery.

SCL `/wi` may synchronously request this settlement after a
`shared_root_dirty` admission result and retry admission once. It may do so
only for a mechanically proven `harness_ledger_flush`; all other dirt returns
the original fail-closed admission error.

## Runtime configuration ownership is not settlement

A runtime-managed tracked configuration such as Relay's exact `data/cron-jobs.json` is a
third authority class. It is not human dirt and is not an append-only Harness event shard.
The site-local runtime-artifact registry may admit its stable postimage only when an exact
enrollment and one exact current producer receipt bind repository/branch/remote identity,
service, producer, HEAD, authority SHA, Git preimage and live postimage. Existing runtime
dirt with no durable writer journal requires one human-authorized historical import that
preserves its producer as `unknown`; it does not retroactively fabricate writer identity.

This proof changes only workspace admission. It never invokes a Broker transaction, never
publishes or cleans the file, and never makes a convergence claim. Mixed dirt, a later
writer postimage, staged state, remote movement or an enrollment mismatch fails closed and
requires a new canonical proof or the appropriate independent settlement workflow.

## Automatic writer zero-shared-root boundary

Background services MUST NOT use a human or primary checkout as their writable Harness
root. Service-owned mutable state belongs under the OS account state directory. When a
Harness event must enter Git, the producer writes it in an enrolled, repository-isolated
service checkout outside every human/project Git root and immediately invokes the typed
`broker settle-ledger` transaction. That checkout is a projection surface, not a second
canonical ledger: before each producer run it must be clean and fast-forwarded to the exact
remote service branch; after a successful run it must be clean at the published SHA.

The service supervisor serializes producers, binds repository identity, branch, source
host, producer id and checkout path in a durable external receipt, and refuses a dirty,
diverged or mismatched checkout. The Broker still accepts only complete schema-valid
monthly event-shard appends. A WorkItem mutation, untracked file, rewrite, staged change or
unknown path cannot borrow ledger authority and leaves the isolated checkout quarantined
for recovery without touching the shared root.

Non-Git operational state such as heartbeat markers, probe incident state, scan reports and
archives remains under the OS account state directory and is never auto-committed. Service
units enforce the boundary with a read-only mount of project Git roots; environment routing
is not the sole guard. Producers that legitimately mutate WorkItems require a separate
typed transaction and stay disabled until that classifier and recovery path are installed.

## Recovery

The Git-root-external transaction journal uses:

`discovered -> snapshotted -> prepared -> pushed -> endpoints_reconciled -> converged -> completed`

Before `pushed`, retry rebuilds from the still-unchanged authority. After
`pushed`, retry reuses the exact published tree. Endpoint reconciliation is
per-endpoint and resumable. Any live preimage or journal hash mismatch becomes
`recovery_required`. Shared-root cleanup is limited to exact manifest paths and
only after the final GitHub commit is proven to contain identical content;
final movement remains `merge --ff-only`.

## UAT boundary

Independent UAT must exercise registered Agent-branch allow, protected-branch
deny, valid/invalid/expired/replayed tags, single- and multi-endpoint human
dirty, a real `CLAUDE.md` checkpoint, safety blockers, conflict atomicity,
concurrent GitHub movement, crash recovery before/after push, partial endpoint
recovery and preservation of local commit history. Producer tests and the old
Hestia handoff are not substitutes.
