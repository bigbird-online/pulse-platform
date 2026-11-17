# Policy preflight and review

AH-064 / A-30 need policy validation, inspection and differences before an
authorized configuration installation. This slice retains existing isolated
policy meanings and program-and-policy writer admission. It introduces no
automatic mode, new tool permission, namespace/tenant grant, or dispatch choice.
The later [execution policy contract](rewrite-execution-policy.md) adds modes
and batch budgets with writer contract 3; this original preview boundary still
does not grant them.

## One boundary

The policy module owns bounded strict JSON loading, structural validation,
isolated physical preflight, immutable identity comparison and compact review.
The installer calls that same preflight before creating any directories or
prepared releases, then creates only its returned directory inventory. Physical
and identity checks are still enforced at activation; filesystem observations
are point-in-time evidence, not reservations or permanent validity guarantees.
The predecessor comparison remains under the SQLite activation lock.
For an unchanged database path it is also checked before preparation; a changed
database still reaches the existing copy/custody refusal first, preserving its
specific failure and recovery semantics.

Policy files must be regular non-symlink UTF-8 JSON, at most 1 MiB. Duplicate
members, nonfinite numbers, missing/unknown keys and malformed nested rows
reject. Accepted keys keep the current native/installer meanings; unsupported
dispatch/security settings cannot be silently ignored. Lists are bounded at
128 profiles, repositories, publication targets or consumers and 32 peers.
Profile rows require role, actor, session_root and artifact_roots (1..128).
Roles remain producer/verifier, identifiers are valid, actors are unique,
session roots cannot overlap, and exactly one verifier matches the designated
verification actor and dispatch root. The designated verifier cannot also be a
producer. Ambiguous or malformed policies cannot gain a new admission.

All mutable state/artifact/repository/publication paths remain within the real
isolated root and must not redirect through symlinks. Validate existing path
types before writes. Missing managed state and verifier directories may be
created only after all preflight checks pass. External native producer session
roots must already exist. Runtime executable bindings must exist; optional
runtime/Relay/human source fields preserve their current meaning. Peer keys are
validated by the existing migration boundary and never included in reports.
No policy inspection executes a business command, opens a mutable database,
creates files, sends a message, changes native sessions or starts a worker.

## Read surfaces

Management CLI supports policy-inspect --root ROOT --policy FILE and
policy-diff --root ROOT --policy PROPOSED --against CURRENT. Both use the same
preflight as install. Differences report changed top-level sections, exact
canonical old/new digests and which already-bound identity fields require
migration. Profile/resource/peer changes require operator review; no hash
comparison is described as proof of permission narrowing or approval.

Native inspect --view policy takes the adjacent installed immutable policy
already admitted by the actual native Host. It needs no WI and rejects WI,
revision, pagination, status/owner/text, age and artifact options. It reports
the exact installed policy digest and role/count/section digests, not raw paths,
actors, peer IDs, key references or native messages. It does not run management
preflight or claim that the filesystem has not changed since installation.

Reports are harness.policy-review/v1, read_only=true, execution_authority=false.
Management validation is labelled current_preflight; native inspection is
labelled installed_policy. No report is a receipt, policy write or permission.
Management installation is the existing explicit authorized write operation.
It re-reads the exact proposed file and revalidates; a stale preview cannot
authorize different bytes. Same-program policy rollback behavior and contract
2 remain unchanged because no new policy meanings are introduced here.

## Acceptance

Prove malformed/duplicate/unknown inputs reject before any directory or release
is created, including a late invalid runtime and invalid verifier profile.
Prove redirected/outside roots and overlapping profiles reject, a valid fresh
installation still initializes only required paths, and existing admission,
fault recovery, concurrency and fact replay tests remain green. Inspect/diff
must leave every existing byte and directory unchanged and never echo a secret
or sensitive raw field. Preserve current authority when reviewing a migration
requirement. Test exact unchanged policy and modified profile/identity diffs,
bounded input and native CLI option rejection. Native installed adoption and
operator preview are separate evidence; no full A-30 completion is implied.


Source evidence: 430 isolated tests passed in 47.99 seconds; strict mypy checked
29 modules, ruff and diff passed. Regression covers original activation fault
boundaries, concurrent installations, physical custody, exact replay and native
query adapters. The native adapter test is a synthetic Host fixture; installed
original-Muse policy review is not yet asserted by this source result.

Candidate18 management preflight was independently checked against exact package
bytes. See [evidence](evidence/rewrite-policy-preview-20261008.json), including
the retained reversed comparison and separately verified forward comparison.
The candidate is not yet installed; native adoption remains separate.
