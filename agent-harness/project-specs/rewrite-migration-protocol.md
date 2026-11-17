# Sealed fact-store transfer contract

Status: new-format transfer is implemented and independently tested for the
AH-034/035/076 boundary. Native transfer adoption, cross-host adapter continuity
and AH-056/077 historical-format conversion remain open. No full UAT claim.

A transfer has one source-generated migration identity and one admitted destination.
It preserves the WI namespace and identity, original request identities, ordered
journal, every referenced original byte, graph/results/candidates and uncertain
external attempts. It never imports a caller-authored replacement state.

1. Each writable store is operator-bound to a store identity and its persistent
   physical database at an exact absolute path. The database retains that binding. A copied
   database at another location is readable but cannot append facts; missing
   runtime bindings fail closed. Program activation cannot rebind an existing
   store or make a cold copy writable.
2. A current owner requests sealing for an operator-admitted destination and an
   actual successor principal, with a bounded checkpoint. Sealing is one journal
   transaction: revoke current grants, invalidate running attempts, retain their
   prior identities/results, and prohibit every new source-side business write.
   Original requests remain readable/retryable; human recovery cannot silently
   unseal the source after a transferable proof has been issued.
3. Export reads only the committed sealed revision. The deterministic package
   contains the original chain and exact referenced inputs plus source/destination
   bindings. An authenticated store-to-store proof binds every package byte.
   Credentials are operator material and never enter packages or public responses.
   The authenticated peer is inside the trusted Host boundary, not a business
   caller. Hashes alone are insufficient to authenticate a source seal.
4. Target preparation checks its actual store/location, admitted source peer,
   namespace, successor principal, proof, every chain link, footer and input hash.
   It atomically preserves the original rows, without granting execution or
   sending external actions. Any conflicting request, original byte or existing
   divergent WI aborts the entire import. Same-package retries return the original
   import; a different package cannot replace it.
5. Target activation consumes the exact prepared migration and the authenticated
   successor. One transaction restores the prior business phase under the next
   generation and current target binding. Retry does not add another generation.
   The source remains sealed; a copied prepared or activated database cannot
   activate at a different host/path. Store registration is not a WI grant.
6. Existing external attempts keep their original targets, inputs and unknown
   outcomes. Target takeover is not proof of absence and never creates a fresh
   send. Only explicitly adopted, still-matching actual adapters may read original effects; resending requires original
   idempotency/absence proof and explicit adoption of the original obligation at
   the new authority generation. Physical source resources remain owned evidence
   until their actual adoption/replacement and separate safe retirement.
7. Failures before sealing leave source authority unchanged. Failures after seal
   leave source read-only and continue the same export/preparation/activation.
   Preparation and activation each commit atomically. A lost response is handled
   by the original identity; no database restore, manual event insertion or second
   successor is a recovery path.
8. Private source packages are readable only by the original responsible owner
   or authenticated destination. Public views expose migration identity, endpoint
   aliases, phases and revision coverage, never source transcripts or keys.
9. Old-format conversion is a separate one-time boundary: retain original bytes,
   identify each supported schema, reject unknown fields/formats without loss,
   and produce a conversion report. A converted snapshot alone cannot activate;
   it needs independently authenticated source retirement. Native new-format
   transfer evidence does not claim historical-format migration coverage.
10. Acceptance uses two independent SQLite stores and real file copies. Inject
    interruption before/after seal, during import, after prepare, before/after
    activation; check source writes rejected, target waits for seal, preserved
    original requests/unknown effects, one generation, no partial rows, altered
    package/foreign peer/wrong successor/namespace/cold clone rejected. Then use
    actual admitted Host entries on the support matrix; synthetic identities are
    test fixtures only and never native takeover receipts.


## Implemented new-format boundary

The kernel owns `seal_migration`, `prepare_migration`, `activate_migration` and
`adopt_migrated_effects`. Preparation imports the original journal rows and
referenced originals inside the same transaction as its decision. It validates
the replay and committed seal before any imported row can commit. Activation
advances generation exactly once; neither phase executes work or external I/O.

The operator pairs two already-bound stores using a private HMAC-SHA256 key.
Both stores trust each other; this is not an asymmetric or nonrepudiation proof.
Peer IDs must be the complete `store.` prefix and 32 lowercase hexadecimal
digits generated by the other store. The installer rejects malformed IDs before
creating a release or changing admission. Shape alone does not authenticate the
peer: its exact ID and location still have to match the actual other binding.
Keys are owned regular 0600 files, 32–256 bytes, at real absolute paths; symlinks,
duplicate peer aliases/store IDs and more than 32 peers are rejected. Business
inputs cannot select keys, store bindings or physical adapter bindings. The
canonical package is bounded to 128 MiB and includes exact source/destination,
namespace, WI, migration identity, journal/footer and referenced original bytes.
Export is private and only available to the original sealer at the actual source.

Every inherited uncertain attempt remains blocked until the responsible native
successor supplies a real human decision adopting its exact obligation. The
persistent physical adapter roots and policy must still match the sealed source;
adoption never retargets an effect or proves absence. The executor still reads
the original attempt. A later physical binding change fences both initial sends
and mutating resource readback, including retirement intents created after
migration for inherited resources. A historical successful request receipt is
not current authority to call the adapter.

Inherited workspaces remain evidence, not destination working authority. A product
`replace_workspace` can use the destination's admitted binding for the same repo
and the exact old base or captured source. Scope work waits for actual new physical
readback. Source dirt, branches, registrations and unique commits remain in the
separate retirement inventory. Further migration waits for inherited resources
to be replaced/retired and retained effects to be adopted; it cannot launder the
original physical binding through another transfer.

Independent tests use two actual SQLite stores, concurrent preparation/activation,
faults before and after commits, original-key retries, wrong successor and peer,
canonical package tampering, request collisions and cold copies. Actual temporary
Git workspaces verify dirty source preservation, destination replacement and
retirement under adapter drift. These synthetic Host tests do not prove a real
native successor, remote source retirement, old-format conversion or an operating
cross-host adapter. Those remain explicit adoption obligations.


## Persistent store custody (location contract 2)

OS machine/boot identifiers describe a runtime incarnation, not persistent
workspace ownership. Their change alone neither proves a copied database nor a
Muse VM failure. Native principal authentication remains separate.

The installer registers custody of the actual database file. Its private
.store-custody/database is a hard link to that same file, never a second SQLite
entry point. An immutable store metadata row records the canonical database path,
anchor path and inode number. Admission checks ownership, real non-symlink paths,
the original inode and same-file identity of database and anchor. Device numbers
are compared within the current observation, not persisted across mounts.
Only the canonical database path is opened by SQLite. Supported storage must
preserve file identity and hard links across runtime replacement and honor SQLite
locking. Whole-volume cloning by a privileged operator needs external fencing;
this local Host boundary does not prove exclusion of a privileged volume clone.

A restart retaining the same file retains admission. A missing anchor, replaced
database, copied installation or changed canonical path cannot recreate custody
through ordinary install or business commands. Inspect stays read-only.

First enrollment and activation share one SQLite write transaction. New stores
need no machine ID. Contract-1 conversion retains the exact store ID and location
digest as historical endpoint identities: no immutable binding, original, journal
row, head, WI generation, grant or effect is rewritten. Conversion requires the
operator's explicit adopt_existing_store installation option, a verified existing
active release, unchanged canonical database/installation paths and matching
database writer admission. All original facts must replay before enrollment.
The operator identifies the retained installation through real management context;
the option is unavailable in the business CLI. This is installation-format
conversion, not source sealing, transfer, a new WI or platform retirement proof.

A fixed private enrollment receipt and hard link retain the original store,
database inode, predecessor release/writer and exact journal frontier. A crash
before SQLite commit permits only the original conversion retry against unchanged
facts. Pointer-ahead recovery validates the recorded predecessor and exact proposed
release. Enrollment cannot replace existing custody. Missing or divergent proof
fails without granting business authority. Old runtimes and installers lack the
new custody SQL function and cannot mutate facts or program admission.
Physical adapter binding also uses retained endpoint directories and policy,
not an OS machine ID. Replaced or unavailable directories change that binding;
old sealed effects keep their original binding and remain fenced on mismatch.

Acceptance covers real file copies, replacement at the same path, missing or
redirected anchors, runtime identity changes, altered recovery receipts, stale
predecessor/frontier, all installation crash boundaries and exact retries. Every
failure preserves original business rows. Native acceptance uses the retained
Muse stores and original Pi session; local tests are not UAT.

Journey9 exposed this application design defect before source sealing. Original
failure evidence is preserved in evidence/rewrite-native-journey-9-20261008.json.
Correction does not depend on a VM reconstruction diagnosis or platform retirement
receipt. Sealed transfer between distinct stores remains a separate protocol.
