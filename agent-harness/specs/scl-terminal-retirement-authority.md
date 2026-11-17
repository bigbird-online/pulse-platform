# Read-only SCL terminal retirement authority

`harness-scl terminal-retirement-authority WI --expected-snapshot-digest SHA256`
reads the existing active sealed lineage, one SQLite read transaction and its
verified cached snapshot. It constructs no lifecycle kernel, Harness store or
worktree manager, initializes no database, and rewrites no projection. The
optional `--kernel-root` identifies the original lineage container, not a retired
kernel or a service Git ledger.

## Positive proof

The current complete snapshot must bind every current WI row field, including
epoch, local/global revision, local state digest, mode, watermark and update time.
Its original wire digest must equal the caller's expected digest. Serialization
drift, a stale disposable projection and an absent projection all preserve the
resource; the command does not repair these conditions.

The latest operation for this WI, without filtering away later operations, must
be a committed, effect-free `adopt_delivered_terminal` or
`adopt_lightweight_terminal`. Its original authenticated intent/result, sequence,
post-state digest and update time must bind the current completed, terminal,
executor-free state. Delivered adoption binds the published SHA to the current
global revision. Lightweight adoption additionally permits the original explicit
`definition_reconciliation.definition_revision`; the original terminal published
SHA remains separately recorded. Unbound, cancelled and failed states do not
receive this proof.

The original authority and registration rows must both be retired in the same
adoption, share their exact ID, generation and digest, and have no authority
lease. Another non-retired generation of that same registration ID remains an
unexplained active binding and preserves the candidate. The registered raw bytes must authenticate to that digest, parse as the
original typed workspace registration, and agree with its stored typed payload,
WI, surface and effective owner. Empty or generic legacy registration payloads
do not establish this proof. Every original WI effect must be succeeded or
cancelled with no lease; the terminal adoption must have no effects. Null
authority in a cached snapshot alone proves nothing.

## Storage and drift

The original pointer, database and cached snapshot are existing owned, regular,
private, single-link files. Directory descriptors reject symlink traversal,
publicly writable parents and relocation; the original state and active kernel
are private. The sealed pointer and all read file identities are checked again
before returning. The database is opened with `mode=ro`, `query_only` and one
transaction. A checkpointed database with no WAL sidecars uses `immutable=1` to
avoid creating sidecars; it is accepted only while the database and absence of
sidecars remain unchanged. Existing WAL/SHM files return HOLD before connecting:
`mode=ro` and `query_only` do not prohibit WAL-index readmark writes. The original
writer must naturally close/checkpoint its own connection; this command never
requests a checkpoint. A missing sidecar, unknown journal, changed storage or unreadable material
returns a fixed HOLD error. No Git fetch, network, lifecycle effect or repair is
part of this command.

## Response and consumer responsibility

The `read_only` result contains the original managed `lineage` binding and
`terminal_retirement_authority` with schema
`scl_terminal_retirement_authority/v1`. The latter has fixed WI, snapshot, state,
operation, owner, surface and original retired-registration fields. Its
`content_digest` is SHA256 of UTF-8, sorted, compact, non-ASCII-escaped JSON of all
proof fields except `content_digest`. No raw registration, intent, result,
credential, session or source payload is returned.

The proof grants no execution or lifecycle authority and authorizes no deletion.
A reclaimer can use it only in the normally retired completed/terminal/null
authority branch; it cannot override an explicit valid, expired, conflicting or
unknown fence. Existing canonical terminal, full identity, registration, exact
Git preservation, path/owner/device/inode, active-reference, private staging and
failure gates remain mandatory. Immediately before retirement the consumer must
read this proof again and compare the complete proof and sealed lineage binding,
as well as its original snapshot and epoch. Drift preserves the candidate. Audit,
rollback and unknown resources retain their independent protection contracts.
