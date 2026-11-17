# Production fact storage and private evidence

The production Git ref remains the only business authority. A private evidence
repository holds bytes, never grants, workflow state, counters or completion.
Pi compaction is a context-management operation, not a privacy boundary: neither
compressed transcripts nor their base64 representation belong in the Git ledger.

## Facts and evidence

The authenticated Host validates the exact bounded native range before admitting
usage. Only extracted counters, coverage, interval identity, extractor version and
the original content digest enter the fact journal. Tool results, message text and
source files are not measurement facts. Exact original bytes remain available to
their authorized custodian; they are not needed to replay admitted decisions.
Missing private evidence yields `evidence_unavailable` for an evidence request,
not corrupt WI state, lost execution authority or a disabled tool set.

Production storage v2 separates each immutable decision from its small WI head.
Large public evidence is content-addressed and split into bounded objects. Raw
human sources and artifacts marked private use an owner-only store outside the
Git worktree, with immutable SHA256 names, no-follow access and durable writes.
An inherited directory setgid bit may preserve filesystem group ownership; it
does not grant group access. Accept owned directories with mode 0700 or 02700,
while rejecting group/other access and keeping evidence files strictly 0600.
The journal retains their digests and lengths. Remote nodes can replay every WI
without these bytes; fetching private evidence requires access to its custodian.
No private-byte fallback to a Git commit is permitted.
Compatibility event files preserve the fixed legacy prefix and one current
native journal watermark. They do not duplicate the growing native event stream.

Existing v1 journals remain readable. Their entries, original content hashes,
semantic hashes, revisions, request identities and results remain unchanged when
rewritten into v2 storage. Conversion verifies all original bytes before saving
private custody and publishing the new representation. It does not replay a
business command or claim a new business decision. Already published historical
Git objects are not erased by conversion. History rewriting and credential
revocation are separate operator actions, never implicit maintenance effects.

## Transactions and isolation

A single-WI transaction loads only that WI and its actual dependencies. It does
not seed or validate unrelated legacy records. Request identity and usage-range
claims refer back to immutable journal entries; they cannot confer independent
authority. Cross-WI usage checks inspect the same native principal's claims,
not all WI reducers. Legacy native journals are consulted only until conversion
has supplied their locators. All such claims and decisions publish atomically.

On a push race, compare the transaction's read dependencies and write paths with
the newly observed ref. If only unrelated paths changed, rebuild the commit on
that ref and retry within a fixed budget. Never overwrite a changed dependency.
A real conflict remains safely retryable using the same request identity. Unknown
commit outcomes require original-request readback. Authentication/policy/size
rejections are not reported as concurrency conflicts. Git errors never echo URLs
or credentials.

## Read reuse

Read speed never changes a result. Nothing below is written to disk; each
reuse lives only in one process, the production ref stays the only authority,
and dropping any of it changes no result.

- Replay applies each decision once, in order, to a state the replay owns. An
  earlier decision is applied from a private parse of its row; the returned
  state holds only its own decision's payloads, exactly as a replay that copies
  the whole state per decision. A historical revision is copied when selected,
  so later decisions cannot change it.
- One command's reads, up to its first write or effect drain, observe one
  production revision: the first read fetches the ref, later reads reuse that
  commit. A write transaction ends the reuse; the write and every later read
  fetch again. The final observation after a command's effects is again one
  revision.
- Each fetch writes the production ref to a fresh private ref and lets Git move
  the remote-tracking ref as usual. Concurrent readers on one checkout (several
  Agents' inbox gates in the same minute) race only on that tracking ref; the
  losing fetch exits non-zero after it wrote its private ref. The private ref is
  then the observation; a fetch that wrote no private ref is
  `ledger_unavailable`.
  `@anchor test agent-harness/tests_rewrite/test_production_ledger.py :: test_concurrent_fetch_losing_the_tracking_ref_still_observes_production`
- A commit's tree listing is reused for that commit ID only; a commit's tree
  never changes.
- A v2 journal's verified decisions are reused only while its head revision and
  digest are unchanged and every Git object they were read from, each decision
  segment and chunk, still has the same path, mode and object ID in the
  transaction's tree. Any difference reads and verifies the journal again. A
  reused journal still checks its custody references against the transaction
  and records the same read dependencies. A failed read is never reused.
- Within one read transaction, which no write changes, a collection read
  replays each parent once for all of its children.
- A known set of blobs is requested from one `git cat-file --batch` process at
  once and read back in request order, with every frame and object ID checked.

## Acceptance

Exercise two real isolated Git clients: unrelated PR race, competing WI writers,
same-request/cross-WI duplicates, overlapping usage, unknown push outcome and
policy rejection. Prove private tool-result sentinels are absent from every new
Git blob, including encoded forms; owner evidence reads still return exact bytes.
A fresh node without custody must read and continue the same WI. Corruption of an
unrelated legacy WI must not block a valid write. Growth beyond 100 MiB of private
input must leave individual Git objects bounded. Existing v1 histories must retain
entry hashes and request results after conversion. Instrument blob reads to prove
routine writes do not consume unrelated originals. Production conversion, actual
consumer adoption and human UAT require separate evidence after source validation.
## Native lightweight default affiliation

The production policy's `default_portfolio_ref` applies to an unclaimed native
lightweight draft with no explicit CR/OA mapping or recorded affiliation. The
shared core evaluates this rule for every native entrypoint. Creation records
the policy decision in the same transaction; an existing native draft created
before this support records it atomically on its first successful `start_work`.
Read-only inspection derives eligibility and never writes a decision.

The event identifies `policy:<current_epoch>` and the policy digest, not a human
approval. It grants no execution authority. Explicit affiliations, invalid or
ambiguous source contracts, the legacy epoch cutoff, standard work and historical
adoption retain their existing rules. A later policy change cannot silently
replace a recorded affiliation; only an owner's human-approved
`revise_affiliation` does, as an audited `AffiliationRevised` event (see
[native operations](rewrite-native-operations.md)). Cross-client replay and request idempotency use
the same production Git journal; no host-local admission record is introduced.
