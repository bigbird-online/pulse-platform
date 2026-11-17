# Installed program and management policy admission

Status: source admission boundary implemented for AH-063/064 and native migration setup.
This slice does not claim full dispatch policy, tenant integration or platform UAT.

1. An installed execution authority binds the exact program bytes and canonical
   management policy together. Changing either selects a different immutable
   release identity and database writer admission. Business arguments cannot
   select a policy or provide an admission digest.
2. Management may install the same exact program with a revised validated policy
   through the normal installer. Each policy is retained in a separate immutable
   release. Existing policy files are never overwritten to make an update work.
   Peer secret material stays outside releases; rotation uses a new immutable
   key-file reference, never an in-place edit presented as the same policy.
3. Installation checks the proposed sealed program against all existing facts,
   holds the same database transaction for binding and pointer transition, and
   does not change any business original, request, event or head. Lost responses
   and interrupted activation retry the same exact program/policy pair.
4. Older policy instances of the same program become read-only when a successor
   is activated. The effect executor also observes that admission before actual
   I/O; returning an old request receipt does not restore an old policy's rights.
5. The database records an immutable writer-contract floor of 2. Once program-and-policy
   admission is installed, older supported installers and writers without this
   contract cannot silently restore program-only admission. SQLite checks the
   installed contract on runtime-binding changes as well as ordinary journal
   writes. This is a protocol boundary, not protection from a privileged database
   administrator who deliberately removes the guards.
6. Native launch resolves the active link to its immutable release before loading
   business modules (the existing bundle entry does this already). The CLI derives
   its admission from that program and its adjacent immutable policy. Failed
   validation, policy mismatch or retired admission cannot execute business work.
7. The native identity query exposes program hash, policy hash and admission separately;
   a management install receipt is not native business adoption or consumer UAT.
8. An existing installation keeps its namespace, surface, database, view,
   delivery and dispatch roots, human-input source, verification actor, runtime
   and Relay bindings. Changing an existing physical or identity binding needs
   migration rather than a configuration-only update, except the explicit
   verified Relay release adoption described below. The installer rereads the
   active predecessor under the database lock before enforcing this boundary.
   Previously absent optional fields can be introduced; the next update binds
   them. Repository/publication snapshots continue to retain original endpoints.

The active symlink and SQLite transaction are separate durable systems. A crash
after pointer replacement and before commit leaves the proposed pair unable to
write; retrying the exact install settles it. It never creates a business fact.
An adapter call admitted before a switch may already be in flight. Its original
attempt remains unknown until the successor reads the original physical target;
neither a new policy nor a lost reply proves absence or permits blind resending.
The effect executor checks current admission immediately before a new apply or
readback call. Management metadata and immutable prepared releases can be left by
a rejected install; this does not grant them execution authority.

Acceptance: same-program changed-policy install and exact retry; old policy read
versus rejected write; real pending effect cannot send from retired policy;
incompatible facts preserve the existing active pair; all activation fault points
recover without changing business rows; concurrent policy installs serialize;
legacy program-only runtime-binding update is rejected after the floor is raised.

Source validation: 239 isolated tests passed, strict mypy checked 26 modules,
ruff and diff checks passed. This slice includes all five activation interruption
points, two concurrent policy installations, and real-file effect recovery when
admission changes after attempt creation or after sending. Native contract-2 installation and original Pi adoption are now verified on the
original two isolated stores (candidate13 through candidate16); fixed policies,
original facts, custody and 39 guards were retained. See
[current native evidence](evidence/rewrite-native-watch-20261008.json).
Configuration changes outside that tested scope, full policy selection and
production adoption remain unverified.


Strict preflight and read-only inspection/diff now share the
[policy module](rewrite-policy-preview.md). An unchanged database's identity
changes reject before new preparation and are checked again under activation
lock; changed-store custody still owns copy/replacement failures. No additional
policy semantics or automatic permissions were added. Source suite: 430 passed;
the newly added preview/native policy surface still needs installed adoption.

## Verified same-node Relay release adoption

A Muse release changes its immutable CLI pathname. Keeping the previous
pathname in the native policy rejects the real successor Relay ancestor. This
is a runtime release change, not permission to relocate facts or change actors.

The operator may pass `--adopt-relay-release <exact release directory>` to
`policy-diff` and `install`. The normal install without that argument continues
to reject a changed `relay_entry`. The explicit path accepts only a one-field
policy change: `relay_entry` moves between exact `PULSE-<40 hex>` releases below
the same physical `pulse/releases` directory. Namespace, actors/profiles,
session roots, database, toolchain, all endpoints, notification policy and
budgets remain byte-value equivalent. No new namespace or fact store is created.

The admission module owns read-only proof: a registered dedicated Muse
experiment node, its actual launcher selection, a successful formal CD
experiment receipt matching the latest receipt and this exact release, the
same predecessor release, complete declared Relay/runtime asset integrity,
and a live PID whose CLI and Node executable match the proposed policy.
Missing, stale, redirected, tampered or mismatched proof rejects before install
preparation and is checked again under the existing database activation lock.
Only the installer selects the new immutable program/policy pair; the native
Host continues to require its exact admitted ancestor, UID and native session.

The release retains `relay-adoption.json` as immutable operator evidence.
Business facts, store identity/custody, human inputs and all guards are unchanged.
Ordinary deployment rollback does not silently rewrite this policy; adopting
another release requires its own valid formal selection and installation.
An installation receipt still does not prove native adoption: verify the original
Telegram/Pi principal and real commands after activation. This is currently a
Muse experiment adapter, not a general production/runtime trust mechanism.

Validation covers exact adoption and retry, ordinary-install rejection, changed
actor/root/policy rejection, another release root, wrong predecessor, stale
selection, absent or different process/Node, invalid receipt, added/missing/
changed runtime files, redirected files and byte preservation on refusal.

For the registered Oracle production surface, an exact managed Relay release
may first add the shared Codex OAuth binding to the existing account
`.codex/auth.json` and canonical Codex launcher. The release carries both OAuth
resources and the existing native consumer preparation seals the prior policy,
candidate policy, Relay configuration and release manifest. Later releases retain
the binding. The Relay release transaction does not remove or replace it.

## Distinct Pi and Relay Node providers

Feature and slice risk: high (native identity admission). Preserve the installed
policy and toolchain definitions; no path fallback or new runtime installation.
The policy's node field selects the canonical Pi executable for producers and
verifiers. The producer_node field pins the Relay ancestor executable, also
checked by Relay release adoption. These need not be the same file.

For a renamed Pi process, native admission checks its executable against node
and the actual context-selected Relay ancestor's executable against
producer_node, in addition to the existing UID, exact CLI, ancestry, session
reference and native transcript checks. Either executable mismatch rejects.
No permission, endpoint, business fact or runtime provider changes.

Acceptance: distinct providers admit the original session; substituting either
with the other and changing the Relay CLI all reject; the complete bounded core
gate passes, then the installed bundle is verified through the original Telegram
command. Rollback uses the retained immutable release and original policy.
